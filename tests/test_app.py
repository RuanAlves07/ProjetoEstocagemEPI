import os
import re
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

from app import create_app, estoque, push_service
from app.db import get_db
from app.util import hoje


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.app = create_app({"DATABASE": os.path.join(self.dir.name, "t.sqlite"), "TESTING": True})
        self.c = self.app.test_client()

    def tearDown(self):
        try:
            with self.app.app_context():
                from app.db import close_db
                close_db()
        except Exception:
            pass
        try:
            self.dir.cleanup()
        except Exception:
            pass

    def post(self, url, data=None, client=None, **kw):
        client = client or self.c
        client.get("/login")  # garante o token CSRF na sessão
        with client.session_transaction() as s:
            token = s.setdefault("csrf", "tok")
        return client.post(url, data={**(data or {}), "csrf_token": token}, **kw)

    def setup_admin(self):
        self.post("/configuracao-inicial", {"nome": "Admin", "login": "admin", "senha": "senha1234",
                                           "confirmacao": "senha1234"})
        return self.post("/login", {"login": "admin", "senha": "senha1234"})

    def criar_epi(self, nome="Luva G", **extra):
        dados = {"nome": nome, "unidade": "par", "estoque_minimo": "5", "prazo_reposicao_dias": "15", **extra}
        r = self.post("/epis/novo", dados)
        return int(re.search(r"/epis/(\d+)", r.headers["Location"]).group(1))

    def criar_colab(self, nome="João", setor="Produção"):
        r = self.post("/colaboradores/novo", {"nome": nome, "matricula": "1", "setor": setor})
        return int(re.search(r"/colaboradores/(\d+)", r.headers["Location"]).group(1))

    def mov(self, tipo, epi_id, qtd, **extra):
        return self.post(f"/movimentar/{tipo}", {"data": hoje().isoformat(), "epi_id": str(epi_id),
                                                  "quantidade": str(qtd), **extra}, follow_redirects=False)

    def saldo(self, epi_id):
        with self.app.app_context():
            return estoque.saldo(get_db(), epi_id)


class TestAcesso(Base):
    def test_redireciona_para_setup_e_login(self):
        self.assertIn("/configuracao-inicial", self.c.get("/").headers["Location"])
        self.setup_admin()
        self.post("/sair")
        self.assertEqual(self.c.get("/epis").status_code, 302)

    def test_post_sem_csrf_rejeitado(self):
        self.assertEqual(self.c.post("/login", data={"login": "a", "senha": "b"}).status_code, 400)

    def test_consulta_nao_escreve(self):
        self.setup_admin()
        self.post("/usuarios/novo", {"nome": "Ver", "login": "ver", "perfil": "consulta",
                                     "senha": "senha1234", "confirmacao": "senha1234"})
        outro = self.app.test_client()
        self.post("/login", {"login": "ver", "senha": "senha1234"}, client=outro)
        self.assertEqual(outro.get("/epis").status_code, 200)
        self.assertEqual(outro.get("/movimentar/saida").status_code, 403)
        self.assertEqual(self.post("/epis/novo", {"nome": "X"}, client=outro).status_code, 403)
        self.assertEqual(outro.get("/usuarios").status_code, 403)

    def _criar_usuario(self, login, perfil="consulta"):
        self.post("/usuarios/novo", {"nome": login.title(), "login": login, "perfil": perfil,
                                     "senha": "senha1234", "confirmacao": "senha1234"})
        with self.app.app_context():
            return get_db().execute("SELECT id FROM usuarios WHERE login = ?", (login,)).fetchone()["id"]

    def test_usuario_inativar_e_reativar(self):
        self.setup_admin()
        uid = self._criar_usuario("ver")
        outro = self.app.test_client()
        self.post("/login", {"login": "ver", "senha": "senha1234"}, client=outro)
        self.assertEqual(outro.get("/epis").status_code, 200)

        self.post(f"/usuarios/{uid}/situacao")
        self.assertEqual(outro.get("/epis").status_code, 302)  # a sessão aberta cai na hora
        self.post("/login", {"login": "ver", "senha": "senha1234"}, client=outro)
        self.assertEqual(outro.get("/epis").status_code, 302)
        self.assertIn("Reativar", self.c.get("/usuarios").get_data(as_text=True))

        self.post(f"/usuarios/{uid}/situacao")
        self.post("/login", {"login": "ver", "senha": "senha1234"}, client=outro)
        self.assertEqual(outro.get("/epis").status_code, 200)

    def test_usuario_excluir_sem_historico(self):
        self.setup_admin()
        uid = self._criar_usuario("ver")
        with self.app.app_context():
            db = get_db()
            db.execute("INSERT INTO push_inscricoes (usuario_id, endpoint, p256dh, auth) VALUES (?, 'e1', 'p', 'a')", (uid,))
            db.execute("INSERT INTO recuperacao_senha (usuario_id, token_hash, codigo_hash, expira_em) "
                       "VALUES (?, 't', 'c', '2030-01-01')", (uid,))
            db.commit()
        self.post(f"/usuarios/{uid}/excluir")
        with self.app.app_context():
            db = get_db()
            self.assertIsNone(db.execute("SELECT 1 FROM usuarios WHERE id = ?", (uid,)).fetchone())
            self.assertIsNone(db.execute("SELECT 1 FROM push_inscricoes WHERE endpoint = 'e1'").fetchone())
            self.assertIsNone(db.execute("SELECT 1 FROM recuperacao_senha WHERE token_hash = 't'").fetchone())

    def test_usuario_com_historico_nao_e_excluido(self):
        self.setup_admin()
        uid = self._criar_usuario("oper", perfil="almoxarifado")
        epi = self.criar_epi()
        with self.app.app_context():
            db = get_db()
            db.execute("INSERT INTO movimentacoes (grupo, epi_id, tipo, quantidade, data, usuario_id) "
                       "VALUES ('g1', ?, 'entrada', 1, '2026-01-01', ?)", (epi, uid))
            db.commit()
        r = self.post(f"/usuarios/{uid}/excluir", follow_redirects=True)
        self.assertIn("não pode ser excluído", r.get_data(as_text=True))
        with self.app.app_context():
            self.assertIsNotNone(get_db().execute("SELECT 1 FROM usuarios WHERE id = ?", (uid,)).fetchone())

    def test_usuario_nao_inativa_nem_exclui_a_si_mesmo(self):
        self.setup_admin()
        self.assertNotIn("/usuarios/1/excluir", self.c.get("/usuarios").get_data(as_text=True))
        self.post("/usuarios/1/situacao")
        self.post("/usuarios/1/excluir")
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT ativo FROM usuarios WHERE id = 1").fetchone()["ativo"], 1)

    def test_usuario_inativar_e_excluir_so_admin(self):
        self.setup_admin()
        self._criar_usuario("ver")
        alvo = self._criar_usuario("alvo")
        outro = self.app.test_client()
        self.post("/login", {"login": "ver", "senha": "senha1234"}, client=outro)
        self.assertEqual(self.post(f"/usuarios/{alvo}/situacao", client=outro).status_code, 403)
        self.assertEqual(self.post(f"/usuarios/{alvo}/excluir", client=outro).status_code, 403)

    def test_push_geral_ignora_usuario_inativo(self):
        self.setup_admin()
        uid = self._criar_usuario("ver")
        self.post(f"/usuarios/{uid}/situacao")
        with self.app.app_context():
            db = get_db()
            db.execute("INSERT INTO push_inscricoes (usuario_id, endpoint, p256dh, auth) VALUES (?, 'inativo', 'p', 'a')", (uid,))
            db.execute("INSERT INTO push_inscricoes (usuario_id, endpoint, p256dh, auth) VALUES (1, 'ativo', 'p', 'a')")
            enviados = []
            with mock.patch("app.push_service.enviar_push_dispositivo",
                            side_effect=lambda sub, *_: enviados.append(sub["endpoint"]) or (True, None)):
                push_service.enviar_para_todos(db, {"title": "x"})
        self.assertEqual(enviados, ["ativo"])

    def test_login_errado(self):
        self.setup_admin()
        self.post("/sair")
        r = self.post("/login", {"login": "admin", "senha": "errada"})
        self.assertIn("incorretos", r.get_data(as_text=True))

    def test_criar_perfil_ajustar_permissoes_e_alterar_login(self):
        self.setup_admin()
        # 1. Admin cria um novo perfil personalizado "RH" com acesso apenas a colaboradores (ver e editar) e EPIs (ver)
        r_pf = self.post(
            "/usuarios/perfis/novo",
            {
                "nome": "RH",
                "descricao": "Gestão de colaboradores",
                "permissoes": ["ver_epis", "ver_colaboradores", "editar_colaboradores"],
            },
            follow_redirects=True,
        )
        self.assertEqual(r_pf.status_code, 200)
        self.assertIn("Perfil “RH” criado", r_pf.get_data(as_text=True))

        # 2. Cria usuário com o novo perfil "rh"
        self.post(
            "/usuarios/novo",
            {"nome": "Maria RH", "login": "maria.rh", "perfil": "rh", "senha": "senha1234", "confirmacao": "senha1234"},
        )
        with self.app.app_context():
            uid = get_db().execute("SELECT id FROM usuarios WHERE login = 'maria.rh'").fetchone()["id"]

        # 3. Admin altera o Login da Maria para "maria.gestao"
        r_ed = self.post(
            f"/usuarios/{uid}/editar",
            {"nome": "Maria RH", "login": "maria.gestao", "perfil": "rh", "ativo": "1"},
            follow_redirects=True,
        )
        self.assertEqual(r_ed.status_code, 200)
        self.assertIn("maria.gestao", r_ed.get_data(as_text=True))

        # 4. Maria loga com o novo login "maria.gestao"
        cli = self.app.test_client()
        r_log = self.post("/login", {"login": "maria.gestao", "senha": "senha1234"}, client=cli)
        # Como o perfil RH não tem 'ver_painel', o sistema redireciona direto para a primeira rota permitida (/epis)
        self.assertIn("/epis", r_log.headers["Location"])
        self.assertEqual(cli.get("/epis").status_code, 200)
        self.assertEqual(cli.get("/colaboradores/novo").status_code, 200)
        # Não tem acesso a Relatórios nem a criar EPIs nem a gerenciar Perfis
        self.assertEqual(cli.get("/relatorios").status_code, 403)
        self.assertEqual(cli.get("/epis/novo").status_code, 403)
        self.assertEqual(self.post("/usuarios/perfis/novo", {"nome": "Invasor"}, client=cli).status_code, 403)

        # 5. Admin ajusta as permissões do perfil "rh" liberando 'ver_relatorios'
        self.post(
            "/usuarios/perfis/rh/editar",
            {
                "nome": "RH",
                "descricao": "Gestão de colaboradores e relatórios",
                "permissoes": ["ver_epis", "ver_colaboradores", "editar_colaboradores", "ver_relatorios"],
            },
        )
        self.assertEqual(cli.get("/relatorios").status_code, 200)


class TestEstoque(Base):
    def setUp(self):
        super().setUp()
        self.setup_admin()
        self.epi = self.criar_epi()
        self.colab = self.criar_colab()

    def test_entrada_saida_saldo(self):
        self.mov("entrada", self.epi, 10)
        r = self.mov("saida", self.epi, 3, colaborador_id=self.colab)
        self.assertIn("/comprovante", r.headers["Location"])
        self.assertEqual(self.saldo(self.epi), 7)

    def test_saida_maior_que_saldo_bloqueada(self):
        self.mov("entrada", self.epi, 2)
        r = self.mov("saida", self.epi, 3, colaborador_id=self.colab)
        self.assertIn("Saldo insuficiente", r.get_data(as_text=True))
        self.assertEqual(self.saldo(self.epi), 2)

    def test_saida_exige_colaborador_e_atomicidade(self):
        outro = self.criar_epi("Bota 40")
        self.mov("entrada", self.epi, 5)
        self.mov("entrada", outro, 1)
        # segunda linha estoura o saldo: nada deve ser gravado
        r = self.post("/movimentar/saida", {"data": hoje().isoformat(), "colaborador_id": str(self.colab),
                                            "epi_id": [str(self.epi), str(outro)], "quantidade": ["2", "9"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.saldo(self.epi), 5)
        r = self.mov("saida", self.epi, 1)
        self.assertIn("colaborador", r.get_data(as_text=True).lower())

    def test_mesmo_epi_em_duas_linhas_soma(self):
        self.mov("entrada", self.epi, 5)
        r = self.post("/movimentar/saida", {"data": hoje().isoformat(), "colaborador_id": str(self.colab),
                                            "epi_id": [str(self.epi)] * 2, "quantidade": ["3", "3"]})
        self.assertIn("Saldo insuficiente", r.get_data(as_text=True))

    def test_ca_vencido_bloqueia_entrega(self):
        vencido = (hoje() - timedelta(days=1)).isoformat()
        epi = self.criar_epi("Capacete", ca="123", validade_ca=vencido)
        self.mov("entrada", epi, 5)
        r = self.mov("saida", epi, 1, colaborador_id=self.colab)
        self.assertIn("vencido", r.get_data(as_text=True))
        self.assertEqual(self.saldo(epi), 5)

    def test_data_futura_e_quantidade_invalida(self):
        futuro = (hoje() + timedelta(days=2)).isoformat()
        r = self.post("/movimentar/entrada", {"data": futuro, "epi_id": str(self.epi), "quantidade": "1"})
        self.assertIn("futuro", r.get_data(as_text=True))
        for q in ("0", "-3", "abc"):
            r = self.mov("entrada", self.epi, q)
            self.assertEqual(r.status_code, 200)
        self.assertEqual(self.saldo(self.epi), 0)

    def test_baixa_e_ajuste_exigem_motivo(self):
        self.mov("entrada", self.epi, 5)
        self.assertEqual(self.mov("baixa", self.epi, 1).status_code, 200)
        self.assertEqual(self.saldo(self.epi), 5)
        self.mov("baixa", self.epi, 1, motivo="Avaria")
        self.mov("ajuste", self.epi, 10, motivo="Saldo inicial", sentido="mais")
        self.assertEqual(self.saldo(self.epi), 14)
        self.mov("ajuste", self.epi, 4, motivo="Inventário", sentido="menos")
        self.assertEqual(self.saldo(self.epi), 10)

    def test_cancelar_recalcula_saldo(self):
        self.mov("entrada", self.epi, 5)
        self.mov("saida", self.epi, 2, colaborador_id=self.colab)
        with self.app.app_context():
            mov_id = get_db().execute("SELECT id FROM movimentacoes WHERE tipo='saida'").fetchone()[0]
            entrada_id = get_db().execute("SELECT id FROM movimentacoes WHERE tipo='entrada'").fetchone()[0]
        # sem motivo: recusado
        self.post(f"/movimentos/{mov_id}/cancelar", {"motivo": ""})
        self.assertEqual(self.saldo(self.epi), 3)
        self.post(f"/movimentos/{mov_id}/cancelar", {"motivo": "Lançado errado"})
        self.assertEqual(self.saldo(self.epi), 5)
        # duplo cancelamento não duplica
        self.post(f"/movimentos/{mov_id}/cancelar", {"motivo": "de novo"})
        self.assertEqual(self.saldo(self.epi), 5)
        # cancelar entrada que já foi consumida deixaria saldo negativo
        self.mov("saida", self.epi, 4, colaborador_id=self.colab)
        self.post(f"/movimentos/{entrada_id}/cancelar", {"motivo": "erro"})
        self.assertEqual(self.saldo(self.epi), 1)

    def test_status_reposicao(self):
        self.mov("entrada", self.epi, 20)
        self.mov("saida", self.epi, 16, colaborador_id=self.colab)   # saldo 4 <= mínimo 5
        with self.app.app_context():
            e = next(x for x in estoque.epis_com_saldo(get_db()) if x["id"] == self.epi)
        self.assertEqual(e["status"], "repor")
        self.assertGreater(e["sugerido"], 0)
        self.mov("saida", self.epi, 4, colaborador_id=self.colab)
        with self.app.app_context():
            e = next(x for x in estoque.epis_com_saldo(get_db()) if x["id"] == self.epi)
        self.assertEqual(e["status"], "zerado")

    def test_paginas_renderizam(self):
        self.mov("entrada", self.epi, 20, fornecedor="ACME", nota_fiscal="99", custo_unitario="12,5")
        r = self.mov("saida", self.epi, 2, colaborador_id=self.colab)
        comprovante = r.headers["Location"]
        urls = ["/", "/epis", f"/epis/{self.epi}", f"/epis/{self.epi}/editar", "/colaboradores",
                f"/colaboradores/{self.colab}", f"/colaboradores/{self.colab}/editar", "/movimentos",
                "/movimentos?tipo=saida&canceladas=1", "/relatorios", "/usuarios", "/usuarios/novo",
                "/conta", "/epis/novo", "/colaboradores/novo", comprovante,
                "/movimentar/entrada", "/movimentar/saida", "/movimentar/baixa", "/movimentar/ajuste"]
        for u in urls:
            r = self.c.get(u)
            self.assertEqual(r.status_code, 200, u)
        csv_ = self.c.get("/movimentos/exportar.csv")
        self.assertIn("ACME", csv_.get_data(as_text=True))
        self.assertEqual(self.c.get("/epis/exportar.csv").status_code, 200)
        rel = self.c.get("/relatorios").get_data(as_text=True)
        self.assertIn("Luva G", rel)
        self.assertIn("Produção", rel)

    def test_setor_fica_gravado_na_epoca(self):
        self.mov("entrada", self.epi, 5)
        self.mov("saida", self.epi, 1, colaborador_id=self.colab)
        self.post(f"/colaboradores/{self.colab}/editar", {"nome": "João", "matricula": "1",
                                                          "setor": "Logística", "ativo": "1"})
        with self.app.app_context():
            setor = get_db().execute("SELECT setor FROM movimentacoes WHERE tipo='saida'").fetchone()[0]
        self.assertEqual(setor, "Produção")

    def test_nome_duplicado_e_xss_escapado(self):
        r = self.post("/epis/novo", {"nome": "luva g", "estoque_minimo": "1", "prazo_reposicao_dias": "1"})
        self.assertIn("Já existe", r.get_data(as_text=True))
        self.criar_epi("<script>alert(1)</script>")
        self.assertNotIn("<script>alert(1)</script>", self.c.get("/epis").get_data(as_text=True))

    def test_perfil_colaborador_padrao_e_solicitados(self):
        epi_extra = self.criar_epi("Cinto Paraquedista")
        epi_sapatao = self.criar_epi("Sapatão Bidensidade")
        epi_camiseta = self.criar_epi("Camiseta Manga Curta", categoria="Vestimenta")
        self.mov("entrada", self.epi, 10)
        self.mov("entrada", epi_extra, 5)
        # Define o padrão para o cargo "Montador" via /colaboradores/cargos
        r_cargo = self.post(
            "/colaboradores/cargos",
            {"cargo": "Montador", "epi_id": [str(self.epi), str(epi_sapatao), str(epi_camiseta)]},
            follow_redirects=True,
        )
        self.assertEqual(r_cargo.status_code, 200)
        # Lista geral de cargos mostra "Montador" e clicar no nome do cargo abre os equipamentos padrão dele
        html_lista_cargos = self.c.get("/colaboradores/cargos").get_data(as_text=True)
        self.assertIn("Montador", html_lista_cargos)
        html_detalhe_cargo = self.c.get("/colaboradores/cargos?cargo=Montador").get_data(as_text=True)
        self.assertIn("Equipamentos definidos por padrão para: Montador", html_detalhe_cargo)
        self.assertIn("Luva G", html_detalhe_cargo)
        # Aloca o cargo "Montador" ao colaborador: ele já puxa automaticamente o padrão do cargo!
        self.post(f"/colaboradores/{self.colab}/cargo", {"funcao": "Montador"}, follow_redirects=True)
        html_padrao = self.c.get(f"/colaboradores/{self.colab}?aba=padrao").get_data(as_text=True)
        self.assertIn("Montador", html_padrao)
        self.assertIn("Luva G", html_padrao)
        # Cinto Paraquedista não faz parte do cargo Montador, então não aparece na lista de utilizados por padrão
        self.assertNotIn("Cinto Paraquedista", html_padrao)
        # No perfil do colaborador é possível definir a numeração do calçado e o tamanho do uniforme
        r_tam = self.post(
            f"/colaboradores/{self.colab}/tamanhos",
            {f"tam_epi_{epi_sapatao}": "41", f"tam_epi_{epi_camiseta}": "GG"},
            follow_redirects=True,
        )
        self.assertEqual(r_tam.status_code, 200)
        html_tam = r_tam.get_data(as_text=True)
        self.assertIn('value="41" selected', html_tam)
        self.assertIn('value="GG" selected', html_tam)
        # Entrega tanto o padrão do cargo quanto o fora do padrão
        self.mov("saida", self.epi, 1, colaborador_id=self.colab)
        self.mov("saida", epi_extra, 1, colaborador_id=self.colab)
        # Na aba "solicitados" aparece o Cinto Paraquedista (fora do padrão do cargo), mas não a Luva G (padrão do cargo)
        html = self.c.get(f"/colaboradores/{self.colab}?aba=solicitados").get_data(as_text=True)
        self.assertIn("Cinto Paraquedista", html)
        # Registra observação manual de teste em outro cargo / promoção
        r2 = self.post(
            f"/colaboradores/{self.colab}/solicitados/novo",
            {"epi_id": str(epi_extra), "quantidade": "1", "observacao": "Em teste para promoção"},
            follow_redirects=True,
        )
        self.assertIn("Em teste para promoção", r2.get_data(as_text=True))

    def test_traducao_sql_postgres(self):
        from app.db import _traduzir_sql_postgres
        s1, p1 = _traduzir_sql_postgres("SELECT * FROM epis WHERE nome LIKE ? ORDER BY nome COLLATE NOCASE")
        self.assertIn("ILIKE %s", s1)
        self.assertNotIn("COLLATE NOCASE", s1)
        self.assertFalse(p1)
        s2, p2 = _traduzir_sql_postgres("INSERT OR IGNORE INTO cargo_epis_padrao (cargo, epi_id) VALUES (?, ?)")
        self.assertTrue(s2.startswith("INSERT INTO"))
        self.assertTrue(s2.endswith("ON CONFLICT DO NOTHING"))
        self.assertFalse(p2)
        s3, p3 = _traduzir_sql_postgres(
            "INSERT INTO movimentacoes (epi_id, criado_em) VALUES (:id, datetime('now','localtime'))"
        )
        self.assertIn("%(id)s", s3)
        self.assertIn("HH24:MI:SS", s3)
        self.assertTrue(s3.endswith("RETURNING id"))
        self.assertTrue(p3)

    def test_cadastro_fornecedores(self):
        r_novo = self.post(
            "/fornecedores/novo",
            {
                "nome": "ProtegeSul EPIs",
                "razao_social": "ProtegeSul Ltda",
                "cnpj": "14.289.410/0001-52",
                "contato_nome": "Carlos",
                "telefone": "(47) 99999-0000",
                "email": "carlos@protegesul.com.br",
                "endereco": "Rua XV, 100",
                "cidade_uf": "Joinville / SC",
                "cep": "89200-000",
                "prazo_entrega_dias": "7",
                "condicao_pagamento": "Boleto 28 dias",
                "categorias_fornecidas": "Luvas, Calçados",
            },
            follow_redirects=True,
        )
        self.assertEqual(r_novo.status_code, 200)
        html_det = r_novo.get_data(as_text=True)
        self.assertIn("ProtegeSul EPIs", html_det)
        self.assertIn("14.289.410/0001-52", html_det)
        self.assertIn("Joinville / SC", html_det)

        # Lista de fornecedores exibe o fornecedor cadastrado
        r_lista = self.c.get("/fornecedores")
        self.assertEqual(r_lista.status_code, 200)
        self.assertIn("ProtegeSul EPIs", r_lista.get_data(as_text=True))

    def test_relatorio_estocagem_e_tamanhos(self):
        bota_epi = self.criar_epi("Botina Bidensidade Bico PVC", categoria="Calçados")
        cam_epi = self.criar_epi("Camiseta Manga Curta", categoria="Vestimenta")
        self.mov("entrada", bota_epi, 20)
        self.mov("entrada", cam_epi, 20)
        # Define tamanho 41 para a botina e G para a camiseta no perfil do colaborador e registra entrega
        self.post(
            f"/colaboradores/{self.colab}/tamanhos",
            {f"tam_epi_{bota_epi}": "41", f"tam_epi_{cam_epi}": "G"},
        )
        self.mov("saida", bota_epi, 2, colaborador_id=self.colab)
        self.mov("saida", cam_epi, 3, colaborador_id=self.colab)

        r_rel = self.c.get("/relatorios")
        self.assertEqual(r_rel.status_code, 200)
        html_rel = r_rel.get_data(as_text=True)
        self.assertIn("Conferência por Produto e Tamanho", html_rel)
        self.assertIn("Botina Bidensidade Bico PVC", html_rel)
        self.assertIn("Nº 41", html_rel)
        self.assertIn("Tam. G", html_rel)
        self.assertIn("João", html_rel)

        # Exportação CSV do relatório por tamanho
        r_csv = self.c.get("/relatorios/estocagem-tamanhos.csv")
        self.assertEqual(r_csv.status_code, 200)
        txt_csv = r_csv.get_data(as_text=True)
        self.assertIn("Botina Bidensidade Bico PVC", txt_csv)
        self.assertIn(";41;", txt_csv)
        self.assertIn("João (2x)", txt_csv)

    def test_caepi_consulta_automatica_e_sincronizacao(self):
        import io
        # 1. Consulta direta via endpoint JSON (/api/caepi/<ca>) para CAs solicitados pelo usuário
        r_33135 = self.c.get("/api/caepi/33135")
        self.assertEqual(r_33135.status_code, 200)
        j_33135 = r_33135.get_json()
        self.assertTrue(j_33135["ok"])
        self.assertEqual(j_33135["dados"]["ca"], "33135")
        self.assertEqual(j_33135["dados"]["categoria"], "Proteção Auditiva")
        self.assertEqual(j_33135["dados"]["validade_ca"], "2028-07-12")

        r_40362 = self.c.get("/api/caepi/40362")
        self.assertEqual(r_40362.status_code, 200)
        j_40362 = r_40362.get_json()
        self.assertTrue(j_40362["ok"])
        self.assertEqual(j_40362["dados"]["ca"], "40362")
        self.assertEqual(j_40362["dados"]["categoria"], "Calçados de Segurança")
        self.assertEqual(j_40362["dados"]["validade_ca"], "2027-03-30")

        r_api = self.c.get("/api/caepi/39840")
        self.assertEqual(r_api.status_code, 200)
        j = r_api.get_json()
        self.assertTrue(j["ok"])
        self.assertEqual(j["dados"]["ca"], "39840")
        self.assertEqual(j["dados"]["categoria"], "Proteção das Mãos")
        self.assertEqual(j["dados"]["unidade"], "par")
        self.assertTrue(j["dados"]["validade_ca"].startswith("20"))

        # 2. Cadastra novo EPI informando SOMENTE o Nº do CA (sem preencher nome, categoria ou validade)
        r_novo = self.post(
            "/epis/novo",
            {
                "ca": "40362",
                "nome": "",
                "categoria": "",
                "validade_ca": "",
                "unidade": "un",
                "estoque_minimo": "5",
                "prazo_reposicao_dias": "10",
            },
            follow_redirects=True,
        )
        self.assertEqual(r_novo.status_code, 200)
        html_det = r_novo.get_data(as_text=True)
        self.assertIn("BOTINA", html_det)
        self.assertIn("Calçados de Segurança", html_det)
        self.assertIn("40362", html_det)

        # 3. Importa arquivo no layout oficial tgg_export_caepi.txt (pipe-delimited) atualizando a validade do CA 40362
        conteudo_tgg = (
            "#NRRegistroCA|DataDeValidade|Situacao|NRProcesso|CNPJ|RazaoSocial|Natureza|NomeEquipamento|DescricaoEquipamento|MarcaCA|Referencia\n"
            "40362|31/12/2030|VÁLIDO|12345|12.503.608/0001-18|FHIORUK INDUSTRIA|Nacional|BOTINA - TIPO B|Calçado ocupacional tipo botina|FHIORUK|TAA.\n"
        ).encode("utf-8")
        r_sync = self.post(
            "/epis/sincronizar-caepi",
            {"arquivo_caepi": (io.BytesIO(conteudo_tgg), "tgg_export_caepi.txt")},
            follow_redirects=True,
            content_type="multipart/form-data",
        )
        self.assertEqual(r_sync.status_code, 200)
        self.assertIn("Base CAEPI importada", r_sync.get_data(as_text=True))

        with self.app.app_context():
            from app.db import get_db
            row = get_db().execute("SELECT validade_ca FROM epis WHERE ca = '40362'").fetchone()
            self.assertEqual(row["validade_ca"], "2030-12-31")

    def test_ca_ausente_do_mte_fica_guardado_ao_cadastrar_a_mao(self):
        import io
        self.assertEqual(self.c.get("/api/caepi/999123").status_code, 404)
        self.post("/epis/novo", {"ca": "999123", "nome": "Luva Nova Teste", "categoria": "Proteção das Mãos",
                                 "validade_ca": "2031-05-20", "unidade": "par",
                                 "estoque_minimo": "0", "prazo_reposicao_dias": "10"})
        j = self.c.get("/api/caepi/999123").get_json()
        self.assertTrue(j["ok"])
        self.assertEqual((j["dados"]["validade_ca"], j["dados"]["fonte"]), ("2031-05-20", "Cadastro manual"))

        # Importar o arquivo do MTE (sem esse CA) não apaga o cadastro manual
        tgg = ("#NRRegistroCA|DataDeValidade|Situacao|NRProcesso|CNPJ|RazaoSocial|Natureza|NomeEquipamento|"
               "DescricaoEquipamento|MarcaCA|Referencia\n"
               "40362|31/12/2030|VÁLIDO|1|1|X|Nacional|BOTINA|Botina|X|X\n").encode("utf-8")
        self.post("/epis/sincronizar-caepi", {"arquivo_caepi": (io.BytesIO(tgg), "tgg_export_caepi.txt")},
                  content_type="multipart/form-data")
        self.assertTrue(self.c.get("/api/caepi/999123").get_json()["ok"])

        # Quando o CA passa a constar no arquivo oficial, os dados oficiais substituem os manuais
        tgg = tgg + "999123|01/01/2032|VÁLIDO|1|1|FABRICANTE|Nacional|LUVA|Luva|X|X\n".encode("utf-8")
        self.post("/epis/sincronizar-caepi", {"arquivo_caepi": (io.BytesIO(tgg), "tgg_export_caepi.txt")},
                  content_type="multipart/form-data")
        d = self.c.get("/api/caepi/999123").get_json()["dados"]
        self.assertEqual((d["validade_ca"], d["fonte"]), ("2032-01-01", "Base CAEPI oficial (MTE)"))

    def test_cadastro_epi_tamanho_numeracao_e_letra(self):
        # 1. Cadastra Botina com Numeração 40 e outra com Numeração 41 (mesmo modelo, produtos separados)
        r40 = self.post("/epis/novo", {
            "nome": "Botina Nobuck Marrom",
            "tamanho": "40",
            "categoria": "Calçados",
            "unidade": "par",
            "estoque_minimo": "5",
            "prazo_reposicao_dias": "10",
        }, follow_redirects=True)
        self.assertEqual(r40.status_code, 200)
        self.assertIn("Botina Nobuck Marrom", r40.get_data(as_text=True))
        self.assertIn("40", r40.get_data(as_text=True))

        r41 = self.post("/epis/novo", {
            "nome": "Botina Nobuck Marrom",
            "tamanho": "41",
            "categoria": "Calçados",
            "unidade": "par",
            "estoque_minimo": "5",
            "prazo_reposicao_dias": "10",
        }, follow_redirects=True)
        self.assertEqual(r41.status_code, 200)
        self.assertIn("Botina Nobuck Marrom", r41.get_data(as_text=True))
        self.assertIn("41", r41.get_data(as_text=True))

        # 2. Tentar cadastrar duplicata exata (mesmo modelo e mesmo tamanho) deve ser barrado
        r_dup = self.post("/epis/novo", {
            "nome": "Botina Nobuck Marrom",
            "tamanho": "40",
            "categoria": "Calçados",
            "unidade": "par",
            "estoque_minimo": "5",
            "prazo_reposicao_dias": "10",
        }, follow_redirects=True)
        self.assertIn("Já existe", r_dup.get_data(as_text=True))

        # 3. Cadastra Camiseta com Letra M e Letra G
        r_m = self.post("/epis/novo", {
            "nome": "Camiseta Gola Polo",
            "tamanho": "M",
            "categoria": "Vestimenta",
            "unidade": "un",
            "estoque_minimo": "10",
            "prazo_reposicao_dias": "15",
        }, follow_redirects=True)
        self.assertEqual(r_m.status_code, 200)

        r_g = self.post("/epis/novo", {
            "nome": "Camiseta Gola Polo",
            "tamanho": "G",
            "categoria": "Vestimenta",
            "unidade": "un",
            "estoque_minimo": "10",
            "prazo_reposicao_dias": "15",
        }, follow_redirects=True)
        self.assertEqual(r_g.status_code, 200)

        # 4. Cadastra Calça também por letra (M e G)
        r_calca_m = self.post("/epis/novo", {
            "nome": "Calça Brim Cinza",
            "tamanho": "M",
            "categoria": "Vestimenta",
            "unidade": "un",
            "estoque_minimo": "8",
            "prazo_reposicao_dias": "15",
        }, follow_redirects=True)
        self.assertEqual(r_calca_m.status_code, 200)

        r_calca_g = self.post("/epis/novo", {
            "nome": "Calça Brim Cinza",
            "tamanho": "G",
            "categoria": "Vestimenta",
            "unidade": "un",
            "estoque_minimo": "8",
            "prazo_reposicao_dias": "15",
        }, follow_redirects=True)
        self.assertEqual(r_calca_g.status_code, 200)

        with self.app.app_context():
            from app.db import get_db
            db = get_db()
            tams_calca = [r[0] for r in db.execute("SELECT tamanho FROM epis WHERE nome = 'Calça Brim Cinza' ORDER BY tamanho").fetchall()]
            self.assertEqual(tams_calca, ["G", "M"])

        # 5. Lista de EPIs e filtro por tamanho
        r_lista_tam = self.c.get("/epis?tamanho=40")
        self.assertEqual(r_lista_tam.status_code, 200)
        html_lista = r_lista_tam.get_data(as_text=True)
        self.assertIn("Botina Nobuck Marrom", html_lista)
        self.assertIn("Nº 40", html_lista)

    def test_mascarar_email_duas_iniciais_e_duas_finais(self):
        from app.email_service import mascarar_email
        masc = mascarar_email("ruan.alves@abecker.com.br")
        self.assertTrue(masc.startswith("ru"))
        self.assertTrue(masc.endswith("br"))
        self.assertIn("@", masc)
        # Tudo entre as 2 primeiras e 2 últimas (exceto @) deve ser '*'
        for i, ch in enumerate(masc):
            if i < 2 or i >= len(masc) - 2 or ch == "@":
                continue
            self.assertEqual(ch, "*")

    def test_esqueci_senha_e_redefinicao_por_codigo_e_token(self):
        import re
        # Cadastra usuário com e-mail na aba Usuários
        r_novo = self.post("/usuarios/novo", {
            "nome": "Ruan Alves",
            "login": "ruan.alves",
            "email": "ruan.alves@abecker.com.br",
            "receber_notificacoes": "1",
            "perfil": "almoxarifado",
            "senha": "senha_antiga_123",
            "confirmacao": "senha_antiga_123",
        }, follow_redirects=True)
        self.assertEqual(r_novo.status_code, 200)
        self.assertIn("ruan.alves@abecker.com.br", r_novo.get_data(as_text=True))

        # Faz logout para testar tela de login e "Esqueceu a senha?"
        self.post("/sair")
        html_login = self.c.get("/login").get_data(as_text=True)
        self.assertIn("Esqueceu a senha?", html_login)

        # Solicita recuperação de senha informando o login
        self.app.config["EMAILS_ENVIADOS"] = []
        r_esq = self.post("/esqueci-senha", {"login": "ruan.alves"}, follow_redirects=True)
        self.assertEqual(r_esq.status_code, 200)
        html_red = r_esq.get_data(as_text=True)

        # Verifica se o e-mail mascarado (2 letras iniciais e 2 letras finais) aparece na tela
        self.assertIn("ru********@************br", html_red)
        self.assertEqual(len(self.app.config["EMAILS_ENVIADOS"]), 1)
        email_rec = self.app.config["EMAILS_ENVIADOS"][0]
        self.assertEqual(email_rec["para"], ["ruan.alves@abecker.com.br"])
        self.assertEqual(email_rec["tipo"], "recuperacao_senha")

        # Extrai o código de 6 dígitos enviado no e-mail e redefine a senha
        m_cod = re.search(r"\b(\d{6})\b", email_rec["html"])
        self.assertIsNotNone(m_cod)
        codigo = m_cod.group(1)

        r_reset = self.post("/redefinir-senha", {
            "login": "ruan.alves",
            "codigo": codigo,
            "senha": "nova_senha_forte_99",
            "confirmacao": "nova_senha_forte_99",
        }, follow_redirects=True)
        self.assertEqual(r_reset.status_code, 200)
        self.assertIn("redefinida com sucesso", r_reset.get_data(as_text=True))

        # Faz login com a nova senha
        r_log = self.post("/login", {"login": "ruan.alves", "senha": "nova_senha_forte_99"}, follow_redirects=True)
        self.assertEqual(r_log.status_code, 200)
        self.assertIn("Ruan Alves", r_log.get_data(as_text=True))

    def test_notificacoes_email_andamento_listas_pedidos(self):
        # Configura e-mail no admin logado
        self.post("/usuarios/1/editar", {
            "nome": "Admin",
            "login": "admin",
            "email": "admin@abecker.com.br",
            "receber_notificacoes": "1",
            "perfil": "admin",
            "ativo": "1",
        })
        # Cria colaborador, EPI e saldo
        self.post("/colaboradores/novo", {"nome": "Carlos Obra", "matricula": "901", "setor": "Obras", "funcao": "Pedreiro"})
        self.post("/epis/novo", {"nome": "Capacete Branco", "categoria": "Cabeça", "unidade": "un", "estoque_minimo": "5", "prazo_reposicao_dias": "10"})
        with self.app.app_context():
            from app.db import get_db
            epi_id = get_db().execute("SELECT id FROM epis WHERE nome = 'Capacete Branco'").fetchone()["id"]
        self.mov("entrada", epi_id, 6)

        self.app.config["EMAILS_ENVIADOS"] = []

        # 1. Criação do pedido -> Lista: Novos pedidos
        r_sim = self.post("/pedidos/simular", {
            "nome": "Carlos Obra",
            "local": "Acordes",
            "motivo": "Desgaste",
            "epi": "Capacete Branco",
        }, follow_redirects=True)
        self.assertEqual(r_sim.status_code, 200)
        self.assertEqual(len(self.app.config["EMAILS_ENVIADOS"]), 1)
        self.assertIn("Novos pedidos", self.app.config["EMAILS_ENVIADOS"][-1]["assunto"])

        with self.app.app_context():
            from app.db import get_db
            ped_id = get_db().execute("SELECT id FROM pedidos ORDER BY id DESC LIMIT 1").fetchone()["id"]
            item_id = get_db().execute("SELECT id FROM pedido_itens WHERE pedido_id = ?", (ped_id,)).fetchone()["id"]

        # 2. Deixar para mais tarde -> Lista: Pendentes (deixados para mais tarde)
        self.post(f"/pedidos/{ped_id}/mais-tarde")
        self.assertIn("Pendentes (deixados para mais tarde)", self.app.config["EMAILS_ENVIADOS"][-1]["assunto"])

        # 3. Iniciar separação -> Lista: Em separação
        self.post(f"/pedidos/{ped_id}/iniciar")
        self.assertIn("Em separação", self.app.config["EMAILS_ENVIADOS"][-1]["assunto"])

        # 4. Marcar item e finalizar -> Lista: Prontos para retirada + Alerta de Reposição (saldo cai de 6 para 5 <= estoque_minimo 5)
        self.post(f"/pedidos/{ped_id}/itens/{item_id}/separar", {"separado": "1"})
        self.post(f"/pedidos/{ped_id}/finalizar")
        ultimo = self.app.config["EMAILS_ENVIADOS"][-1]
        self.assertIn("Prontos para retirada", ultimo["assunto"])
        self.assertIn("Alerta de Reposição de Estoque", ultimo["html"])

        # 5. Registrar retirada -> Lista: Retirados
        self.post(f"/pedidos/{ped_id}/retirar", {"retirado_por_nome": "Carlos Obra", "retirado_descricao": "Entregue em mãos"})
        ultimo_ret = self.app.config["EMAILS_ENVIADOS"][-1]
        self.assertIn("Retirados", ultimo_ret["assunto"])
        self.assertIn("Entregue em mãos", ultimo_ret["html"])

    def test_cadastro_em_massa_multiplos_tamanhos_e_aviso_zerado_numeracao(self):
        # 1. Tenta cadastrar 100 unidades de Botina em 36, 37 e 42 mas com soma errada (30 + 30 + 20 = 80 != 100)
        r_erro = self.post("/epis/novo", {
            "nome": "Botina Segurança Premier",
            "categoria": "Calçados",
            "unidade": "par",
            "quantidade_inicial": "100",
            "tamanho": "36, 37, 42",
            "qtd_tam_36": "30",
            "qtd_tam_37": "30",
            "qtd_tam_42": "20",
            "estoque_minimo": "5",
            "prazo_reposicao_dias": "15",
        }, follow_redirects=True)
        self.assertEqual(r_erro.status_code, 200)
        self.assertIn("precisa bater exatamente com o total de unidades informado (100)", r_erro.get_data(as_text=True))

        # 2. Cadastra batendo o total (30 + 30 + 40 = 100)
        r_ok = self.post("/epis/novo", {
            "nome": "Botina Segurança Premier",
            "categoria": "Calçados",
            "unidade": "par",
            "quantidade_inicial": "100",
            "tamanho": "36, 37, 42",
            "qtd_tam_36": "30",
            "qtd_tam_37": "30",
            "qtd_tam_42": "40",
            "estoque_minimo": "5",
            "prazo_reposicao_dias": "15",
        }, follow_redirects=True)
        self.assertEqual(r_ok.status_code, 200)
        html_ok = r_ok.get_data(as_text=True)
        self.assertIn("3 numerações/tamanhos", html_ok)
        self.assertIn("total de 100 par lançado(s) no estoque", html_ok)

        # Verifica saldos criados automaticamente para 36, 37 e 42
        with self.app.app_context():
            from app.db import get_db
            db = get_db()
            rows = db.execute(
                "SELECT id, tamanho FROM epis WHERE nome = 'Botina Segurança Premier' ORDER BY tamanho"
            ).fetchall()
            self.assertEqual([r["tamanho"] for r in rows], ["36", "37", "42"])
            saldos_map = {r["tamanho"]: self.saldo(r["id"]) for r in rows}
            self.assertEqual(saldos_map, {"36": 30, "37": 30, "42": 40})

        # 3. Pedido pedindo tamanho 40 (que não tem estoque, enquanto 36, 37 e 42 têm)
        self.post("/pedidos/simular", {
            "nome": "João",
            "local": "Acordes",
            "motivo": "Desgaste",
            "epi": "Botina Segurança Premier",
            "sapatao": "40",
        }, follow_redirects=True)
        with self.app.app_context():
            from app.db import get_db
            ped_id = get_db().execute("SELECT id FROM pedidos ORDER BY id DESC LIMIT 1").fetchone()["id"]
        html_ped = self.c.get(f"/pedidos/{ped_id}").get_data(as_text=True)
        self.assertIn("Zerado por conta da numeração", html_ped)
        self.assertIn("Zerado no nº/tam. 40", html_ped)

    def test_matricula_auto_setor_vinculo_formulario_multiplos_cas_e_excluir_pedido(self):
        # 1. Colaborador: formulário exibe "Setor" (sem "/ Obra") e gera matrícula automática
        html_form_colab = self.c.get("/colaboradores/novo").get_data(as_text=True)
        self.assertIn("Setor", html_form_colab)
        self.assertNotIn("Setor / Obra", html_form_colab)

        r_col = self.post("/colaboradores/novo", {
            "nome": "Marcos Mecânico",
            "matricula": "",  # sem preencher manualmente -> deve gerar automático
            "setor": "Oficina Central",
            "funcao": "Mecânico",
        }, follow_redirects=True)
        self.assertEqual(r_col.status_code, 200)
        with self.app.app_context():
            from app.db import get_db
            colab = get_db().execute("SELECT id, matricula, setor FROM colaboradores WHERE nome = 'Marcos Mecânico'").fetchone()
            self.assertTrue(colab["matricula"] and colab["matricula"].strip() != "")
            self.assertEqual(colab["setor"], "Oficina Central")

        # 2. EPIs: vínculo generalizado nome_formulario ("Sapatão (mecânica)") + múltiplos modelos e CAs diferentes para o mesmo produto/tamanho
        self.post("/epis/novo", {
            "nome": "Sapatão modelo X (mecânica)",
            "nome_formulario": "Sapatão (mecânica)",
            "categoria": "Calçados",
            "ca": "11111",
            "tamanho": "41",
            "quantidade_inicial": "5",
            "qtd_tam_41": "5",
            "unidade": "par",
            "estoque_minimo": "1",
            "prazo_reposicao_dias": "10",
        }, follow_redirects=True)
        self.post("/epis/novo", {
            "nome": "Sapatão modelo Z (mecânica)",
            "nome_formulario": "Sapatão (mecânica)",
            "categoria": "Calçados",
            "ca": "22222",
            "tamanho": "41",
            "quantidade_inicial": "3",
            "qtd_tam_41": "3",
            "unidade": "par",
            "estoque_minimo": "1",
            "prazo_reposicao_dias": "10",
        }, follow_redirects=True)
        # Mesmo modelo Z e tamanho 41, porém lote com revisão/CA diferente (33333)
        self.post("/epis/novo", {
            "nome": "Sapatão modelo Z (mecânica)",
            "nome_formulario": "Sapatão (mecânica)",
            "categoria": "Calçados",
            "ca": "33333",
            "tamanho": "41",
            "quantidade_inicial": "7",
            "qtd_tam_41": "7",
            "unidade": "par",
            "estoque_minimo": "1",
            "prazo_reposicao_dias": "10",
        }, follow_redirects=True)

        with self.app.app_context():
            from app.db import get_db
            db = get_db()
            epis_z = db.execute("SELECT id, ca FROM epis WHERE nome = 'Sapatão modelo Z (mecânica)' ORDER BY ca").fetchall()
            self.assertEqual([r["ca"] for r in epis_z], ["22222", "33333"])
            epi_z_33333_id = epis_z[1]["id"]

        # Formulário /solicitar agrupa e exibe o nome generalizado "Sapatão (mecânica)"
        html_sol = self.c.get("/solicitar").get_data(as_text=True)
        self.assertIn("Sapatão (mecânica)", html_sol)

        # 3. Pedido solicitando o nome generalizado "Sapatão (mecânica)" nº 41
        self.post("/pedidos/simular", {
            "nome": "Marcos Mecânico",
            "local": "Acordes",
            "motivo": "Reposição",
            "epi": "Sapatão (mecânica)",
            "sapatao": "41",
        }, follow_redirects=True)
        with self.app.app_context():
            from app.db import get_db
            db = get_db()
            ped = db.execute("SELECT id FROM pedidos ORDER BY id DESC LIMIT 1").fetchone()
            ped_id = ped["id"]
            item = db.execute("SELECT id, epi_id FROM pedido_itens WHERE pedido_id = ?", (ped_id,)).fetchone()
            item_id = item["id"]

        html_det = self.c.get(f"/pedidos/{ped_id}").get_data(as_text=True)
        # Verifica se as possibilidades e os CAs aparecem na faixa do pedido
        self.assertIn("Sapatão modelo X (mecânica)", html_det)
        self.assertIn("Sapatão modelo Z (mecânica)", html_det)
        self.assertIn("11111", html_det)
        self.assertIn("22222", html_det)
        self.assertIn("33333", html_det)
        self.assertIn("Excluir pedido", html_det)

        # Troca para o Sapatão modelo Z do CA 33333 após conferência física
        r_ed = self.post(f"/pedidos/{ped_id}/itens/{item_id}/editar", {
            "epi_id": str(epi_z_33333_id),
            "quantidade": "1",
        }, follow_redirects=True)
        self.assertEqual(r_ed.status_code, 200)
        with self.app.app_context():
            from app.db import get_db
            it_atual = get_db().execute("SELECT epi_id FROM pedido_itens WHERE id = ?", (item_id,)).fetchone()
            self.assertEqual(it_atual["epi_id"], epi_z_33333_id)

        # 4. Excluir pedido permanentemente
        r_del = self.post(f"/pedidos/{ped_id}/excluir", follow_redirects=True)
        self.assertEqual(r_del.status_code, 200)
        self.assertIn("excluído permanentemente", r_del.get_data(as_text=True))
        with self.app.app_context():
            from app.db import get_db
            self.assertIsNone(get_db().execute("SELECT id FROM pedidos WHERE id = ?", (ped_id,)).fetchone())

    def test_conta_enderecos_antigos_redirecionam(self):
        for antigo in ("/meu-perfil", "/minha-senha"):
            r = self.c.get(antigo)
            self.assertEqual(r.status_code, 308, antigo)
            self.assertTrue(r.headers["Location"].endswith("/conta"), antigo)

    def test_conta_senha_atual_so_para_dados_de_acesso(self):
        dados = {"login": "admin", "email": ""}
        r = self.post("/conta", dados, follow_redirects=True)
        self.assertIn("Conta atualizada com sucesso", r.get_data(as_text=True))
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT receber_notificacoes FROM usuarios WHERE login = 'admin'")
                             .fetchone()[0], 0)

        for campo, valor in (("email", "novo@empresa.com"), ("login", "outro")):
            r = self.post("/conta", {**dados, campo: valor}, follow_redirects=True)
            self.assertIn("informe corretamente sua senha atual", r.get_data(as_text=True), campo)
        r = self.post("/conta", {**dados, "senha": "novasenha123", "confirmacao": "novasenha123"},
                      follow_redirects=True)
        self.assertIn("informe corretamente sua senha atual", r.get_data(as_text=True))
        with self.app.app_context():
            u = get_db().execute("SELECT login, email FROM usuarios WHERE login = 'admin'").fetchone()
            self.assertIsNone(u["email"])

    def test_conta_aparelhos_e_acessos(self):
        with self.app.app_context():
            db = get_db()
            admin = db.execute("SELECT id FROM usuarios WHERE login = 'admin'").fetchone()[0]
            outro = db.execute("INSERT INTO usuarios (nome, login, senha_hash, perfil) "
                               "VALUES ('Outro', 'outro', 'x', 'almoxarifado')").lastrowid
            ua = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/130.0 Mobile Safari/537.36"
            for dono, sufixo in ((admin, "meu"), (outro, "alheio")):
                db.execute("INSERT INTO push_inscricoes (endpoint, p256dh, auth, usuario_id, user_agent) "
                           "VALUES (?, 'p', 'a', ?, ?)", (f"https://push.test/{sufixo}", dono, ua))
            db.commit()
            meu = db.execute("SELECT id FROM push_inscricoes WHERE usuario_id = ?", (admin,)).fetchone()[0]
            alheio = db.execute("SELECT id FROM push_inscricoes WHERE usuario_id = ?", (outro,)).fetchone()[0]

        html = self.c.get("/conta").get_data(as_text=True)
        self.assertIn("Chrome no Android", html)
        self.assertNotIn("https://push.test/alheio", html)
        self.assertIn("Perfil: Administrador", html)
        self.assertIn("O que posso acessar", html)

        self.post(f"/conta/aparelhos/{alheio}/remover")
        self.post(f"/conta/aparelhos/{meu}/remover")
        with self.app.app_context():
            restantes = [r[0] for r in get_db().execute("SELECT id FROM push_inscricoes").fetchall()]
        self.assertEqual(restantes, [alheio])

    def test_conta_apelido_do_aparelho(self):
        from app import push_service
        with self.app.app_context():
            db = get_db()
            admin = db.execute("SELECT id FROM usuarios WHERE login = 'admin'").fetchone()[0]
            outro = db.execute("INSERT INTO usuarios (nome, login, senha_hash, perfil) "
                               "VALUES ('Outro', 'outro', 'x', 'almoxarifado')").lastrowid
            ua = "Mozilla/5.0 (Windows NT 10.0) Firefox/130.0"
            push_service.salvar_inscricao(db, "https://push.test/meu", "p", "a", admin, ua)
            push_service.salvar_inscricao(db, "https://push.test/alheio", "p", "a", outro, ua)
            db.commit()
            meu = db.execute("SELECT id FROM push_inscricoes WHERE usuario_id = ?", (admin,)).fetchone()[0]
            alheio = db.execute("SELECT id FROM push_inscricoes WHERE usuario_id = ?", (outro,)).fetchone()[0]

        def apelido_de(aparelho):
            with self.app.app_context():
                return get_db().execute("SELECT apelido FROM push_inscricoes WHERE id = ?", (aparelho,)).fetchone()[0]

        r = self.post(f"/conta/aparelhos/{meu}/apelido", {"apelido": "  Notebook <b>RH</b> "}, follow_redirects=True)
        html = r.get_data(as_text=True)
        self.assertIn("Notebook &lt;b&gt;RH&lt;/b&gt;", html)
        self.assertIn("Firefox no Windows", html)

        self.post(f"/conta/aparelhos/{alheio}/apelido", {"apelido": "Invasor"})
        self.assertIsNone(apelido_de(alheio))

        # O navegador renova a inscrição e o apelido continua
        with self.app.app_context():
            db = get_db()
            push_service.salvar_inscricao(db, "https://push.test/meu", "p2", "a2", admin, ua)
            db.commit()
        self.assertEqual(apelido_de(meu), "Notebook <b>RH</b>")

        self.post(f"/conta/aparelhos/{meu}/apelido", {"apelido": "x" * 60})
        self.assertEqual(len(apelido_de(meu)), 40)
        self.post(f"/conta/aparelhos/{meu}/apelido", {"apelido": "   "})
        self.assertIsNone(apelido_de(meu))

    def test_conta_foto_e_notificacoes(self):
        import io
        # 1. Acessa /conta
        r_perfil = self.c.get("/conta")
        self.assertEqual(r_perfil.status_code, 200)
        html = r_perfil.get_data(as_text=True)
        self.assertIn("Minha Conta", html)
        self.assertIn("Foto do Perfil", html)
        self.assertIn("Notificações de navegador", html)
        self.assertIn("Ativar Notificações neste Aparelho", html)

        # No header não deve haver o botão solto "🔔 Notificações"
        self.assertNotIn('class="btn-push-toggle btn pequeno nao-imprimir"', html)

        # 2. Faz upload de uma foto de perfil válida (PNG)
        png_1x1 = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
            b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        r_post = self.post("/conta", {
            "login": "admin",
            "email": "admin@empresa.com",
            "receber_notificacoes": "1",
            "atual": "senha1234",
            "foto_perfil": (io.BytesIO(png_1x1), "foto.png"),
        }, follow_redirects=True, content_type="multipart/form-data")
        self.assertEqual(r_post.status_code, 200)
        html_salvo = r_post.get_data(as_text=True)
        self.assertIn("Conta atualizada com sucesso", html_salvo)
        self.assertIn("avatar-header", html_salvo)
        self.assertIn("data:image/png;base64,", html_salvo)

        # 3. Remover foto de perfil
        r_rem = self.post("/conta", {
            "login": "admin",
            "email": "admin@empresa.com",
            "receber_notificacoes": "1",
            "atual": "senha1234",
            "remover_foto": "1",
        }, follow_redirects=True)
        self.assertEqual(r_rem.status_code, 200)
        with self.app.app_context():
            from app.db import get_db
            u = get_db().execute("SELECT foto_perfil FROM usuarios WHERE login = 'admin'").fetchone()
            self.assertIsNone(u["foto_perfil"])

    def test_cargos_padrao_itens_genericos_formulario(self):
        # 1. Carrega tela de cargo e valida presença dos itens genéricos oficiais do formulário
        r_cargo = self.c.get("/colaboradores/cargos?cargo=Pedreiro")
        self.assertEqual(r_cargo.status_code, 200)
        html = r_cargo.get_data(as_text=True)
        self.assertIn("CAPACETE DE SEGURANÇA C/ SUSPENSÃO", html)
        self.assertIn("CALÇA AZUL MARINHO BRIM C/ FAIXA REFLETIVA", html)
        self.assertIn("Item do Formulário", html)

        # 2. Marcação via AJAX com item genérico (ex: 90000)
        r_ajax = self.post("/colaboradores/cargos", {
            "cargo": "Pedreiro",
            "epi_id": "90000",
            "marcado": "1",
            "ajax": "1",
        })
        self.assertEqual(r_ajax.status_code, 200)
        with self.app.app_context():
            from app.db import get_db
            row = get_db().execute(
                "SELECT * FROM cargo_epis_padrao WHERE cargo = 'Pedreiro' AND epi_id = 90000"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertIn("ABAFADOR", row["nome_generico"])

        # 3. Formulário completo salvando EPIs e Uniformes genéricos
        r_salvar = self.post("/colaboradores/cargos", {
            "cargo": "Pedreiro",
            "epi_id": ["90011", "91001"],  # Luva vaqueta e Camiseta manga curta
        }, follow_redirects=True)
        self.assertEqual(r_salvar.status_code, 200)
        html_pedreiro = r_salvar.get_data(as_text=True)
        self.assertIn("LUVAS VAQUETA PETROLEIRA", html_pedreiro)
        self.assertIn("CAMISETA MANGA CURTA", html_pedreiro)

        # 4. Ficha do colaborador com cargo Pedreiro exibe os itens genéricos na aba padrão
        self.post(f"/colaboradores/{self.colab}/cargo", {"funcao": "Pedreiro"}, follow_redirects=True)
        r_ficha = self.c.get(f"/colaboradores/{self.colab}?aba=padrao")
        html_ficha = r_ficha.get_data(as_text=True)
        self.assertIn("LUVAS VAQUETA PETROLEIRA", html_ficha)
        self.assertIn("CAMISETA MANGA CURTA", html_ficha)


if __name__ == "__main__":
    unittest.main()




def _png_assinatura():
    import struct
    import zlib

    def bloco(tipo, dados):
        return struct.pack(">I", len(dados)) + tipo + dados + struct.pack(">I", zlib.crc32(tipo + dados) & 0xFFFFFFFF)
    cru = b"".join(b"\x00" + b"\x00\x00\x00\xff" * 4 for _ in range(4))
    png = (b"\x89PNG\r\n\x1a\n" + bloco(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 6, 0, 0, 0))
           + bloco(b"IDAT", zlib.compress(cru)) + bloco(b"IEND", b""))
    import base64
    return "data:image/png;base64," + base64.b64encode(png).decode()


class BaseFicha(Base):
    """Admin, dois colaboradores e um EPI com estoque, para montar pedidos prontos com ficha de EPI."""
    def setUp(self):
        super().setUp()
        self.setup_admin()
        self.app.config["WHATSAPP_ENVIADOS"] = []
        self.app.config["FICHAS_ANEXOS"] = os.path.join(self.dir.name, "fichas")
        self.post("/colaboradores/novo", {"nome": "Carlos Obra", "setor": "Obras", "funcao": "Pedreiro",
                                          "whatsapp": "(47) 99999-8888", "cpf": "529.982.247-25",
                                          "data_nascimento": "1990-04-12"})
        self.post("/colaboradores/novo", {"nome": "Ana Encarregada", "setor": "Obras", "funcao": "Encarregada"})
        self.epi = self.criar_epi("Capacete Branco")
        self.mov("entrada", self.epi, 5)
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE epis SET ca = '987654', validade_ca = '2031-05-01' WHERE id = ?", (self.epi,))
            self.carlos = db.execute("SELECT id FROM colaboradores WHERE nome = 'Carlos Obra'").fetchone()["id"]
            self.ana = db.execute("SELECT id FROM colaboradores WHERE nome = 'Ana Encarregada'").fetchone()["id"]

    def pedido_pronto(self):
        self.post("/pedidos/simular", {"nome": "Carlos Obra", "local": "Acordes", "motivo": "Desgaste",
                                       "epi": "Capacete Branco"})
        with self.app.app_context():
            db = get_db()
            ped = db.execute("SELECT id FROM pedidos ORDER BY id DESC LIMIT 1").fetchone()["id"]
            item = db.execute("SELECT id FROM pedido_itens WHERE pedido_id = ?", (ped,)).fetchone()["id"]
            db.execute("UPDATE pedidos SET colaborador_id = ? WHERE id = ?", (self.carlos, ped))
            db.execute("UPDATE pedido_itens SET epi_id = ? WHERE id = ?", (self.epi, item))
        self.post(f"/pedidos/{ped}/iniciar")
        self.post(f"/pedidos/{ped}/itens/{item}/separar", {"separado": "1"})
        self.post(f"/pedidos/{ped}/finalizar")
        return ped

    def ficha(self, ficha_id=None):
        with self.app.app_context():
            sql = "SELECT * FROM fichas_epi " + ("WHERE id = ?" if ficha_id else "ORDER BY id DESC LIMIT 1")
            return dict(get_db().execute(sql, (ficha_id,) if ficha_id else ()).fetchone())


class TestFichaEPI(BaseFicha):
    def test_cadastro_whatsapp_cpf_e_nascimento(self):
        with self.app.app_context():
            c = get_db().execute("SELECT whatsapp, cpf, data_nascimento FROM colaboradores WHERE id = ?",
                                 (self.carlos,)).fetchone()
        self.assertEqual((c["whatsapp"], c["cpf"], c["data_nascimento"]), ("5547999998888", "52998224725", "1990-04-12"))
        r = self.post("/colaboradores/novo", {"nome": "X", "cpf": "111.111.111-11", "whatsapp": "123"},
                      follow_redirects=True)
        html = r.get_data(as_text=True)
        self.assertIn("CPF inválido", html)
        self.assertIn("WhatsApp com DDD", html)

    def test_previa_mostra_ficha_do_colaborador(self):
        ped = self.pedido_pronto()
        html = self.c.get(f"/pedidos/{ped}/ficha").get_data(as_text=True)
        self.assertIn("Controle de Entrega de EPI", html)
        self.assertIn("CARLOS OBRA", html)
        self.assertIn("529.982.247-25", html)
        self.assertIn("987654", html)  # CA do item
        self.assertIn("01/05/2031", html)  # validade do CA
        self.assertIn("Enviar ficha de EPI", html)

    def test_envio_codigo_e_assinatura_pelo_whatsapp(self):
        ped = self.pedido_pronto()
        r = self.post(f"/pedidos/{ped}/ficha", {"colaborador_id": str(self.carlos), "acao": "enviar",
                                                 f"hig_{self.ficha_mov_id(ped)}": "NAO"})
        self.assertEqual(r.status_code, 302)
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT status FROM pedidos WHERE id = ?", (ped,)).fetchone()["status"],
                             "retirado")
        f = self.ficha()
        self.assertEqual(f["status"], "aguardando")
        self.assertIn('"higienizado": "NÃO"', f["documento"])
        msg = self.app.config["WHATSAPP_ENVIADOS"][-1]
        self.assertEqual(msg["numero"], "5547999998888")
        token = re.search(r"/assinar/([\w-]+)", msg["texto"]).group(1)

        cel = self.app.test_client()
        pagina = cel.get(f"/assinar/{token}").get_data(as_text=True)
        self.assertIn("Enviar código pelo WhatsApp", pagina)
        self.assertNotIn("529.982.247-25", pagina)  # dados só depois do código
        self.assertEqual(len(self.app.config["WHATSAPP_ENVIADOS"]), 1)  # abrir o link não dispara nada

        # assinar sem validar o código é recusado
        self.post(f"/assinar/{token}/assinar", {"assinatura": _png_assinatura()}, client=cel)
        self.assertEqual(self.ficha()["status"], "aguardando")

        self.post(f"/assinar/{token}/codigo", client=cel)
        codigo = re.search(r"\b(\d{5})\b", self.app.config["WHATSAPP_ENVIADOS"][-1]["texto"]).group(1)
        errado = "00000" if codigo != "00000" else "11111"
        r = self.post(f"/assinar/{token}/validar", {"codigo": errado}, client=cel, follow_redirects=True)
        self.assertIn("Código incorreto", r.get_data(as_text=True))
        r = self.post(f"/assinar/{token}/validar", {"codigo": codigo}, client=cel, follow_redirects=True)
        html = r.get_data(as_text=True)
        self.assertIn("Faça sua assinatura abaixo", html)
        self.assertIn("529.982.247-25", html)

        self.post(f"/assinar/{token}/assinar", {"assinatura": "data:image/png;base64,naoeimagem"}, client=cel)
        self.assertEqual(self.ficha()["status"], "aguardando")
        r = self.post(f"/assinar/{token}/assinar", {"assinatura": _png_assinatura()}, client=cel, follow_redirects=True)
        self.assertIn("Ficha assinada", r.get_data(as_text=True))
        f = self.ficha()
        self.assertEqual(f["status"], "assinada")
        self.assertTrue(f["assinatura"].startswith("data:image/png;base64,"))
        self.assertTrue(f["assinado_em"])

        perfil = self.c.get(f"/colaboradores/{self.carlos}?aba=fichas").get_data(as_text=True)
        self.assertIn("Assinada pelo WhatsApp", perfil)
        relatorio = self.c.get(f"/colaboradores/{self.carlos}/fichas/relatorio").get_data(as_text=True)
        self.assertIn('alt="Rubrica"', relatorio)
        self.assertIn("rodape-rubrica", relatorio)

    def ficha_mov_id(self, ped):
        with self.app.app_context():
            return get_db().execute("SELECT m.id FROM movimentacoes m JOIN pedidos p ON p.grupo = m.grupo "
                                    "WHERE p.id = ?", (ped,)).fetchone()["id"]

    def test_codigo_esgota_tentativas_e_link_invalido(self):
        ped = self.pedido_pronto()
        self.post(f"/pedidos/{ped}/ficha", {"colaborador_id": str(self.carlos), "acao": "enviar"})
        token = re.search(r"/assinar/([\w-]+)", self.app.config["WHATSAPP_ENVIADOS"][-1]["texto"]).group(1)
        cel = self.app.test_client()
        self.assertEqual(cel.get("/assinar/token-que-nao-existe").status_code, 404)
        self.post(f"/assinar/{token}/codigo", client=cel)
        codigo = re.search(r"\b(\d{5})\b", self.app.config["WHATSAPP_ENVIADOS"][-1]["texto"]).group(1)
        errado = "00000" if codigo != "00000" else "11111"
        for _ in range(5):
            self.post(f"/assinar/{token}/validar", {"codigo": errado}, client=cel)
        r = self.post(f"/assinar/{token}/validar", {"codigo": codigo}, client=cel, follow_redirects=True)
        self.assertIn("Muitas tentativas", r.get_data(as_text=True))
        # reenviar a ficha gera link novo e invalida o anterior
        self.post(f"/fichas/{self.ficha()['id']}/enviar")
        self.assertEqual(cel.get(f"/assinar/{token}").status_code, 404)

    def test_outra_pessoa_retira_e_assina_em_papel(self):
        ped = self.pedido_pronto()
        r = self.post(f"/pedidos/{ped}/ficha", {"colaborador_id": str(self.ana), "acao": "imprimir"})
        self.assertIn("/imprimir", r.headers["Location"])
        f = self.ficha()
        self.assertEqual((f["colaborador_id"], f["beneficiario_id"]), (self.ana, self.carlos))
        self.assertIn("Carlos Obra", self.c.get(r.headers["Location"]).get_data(as_text=True))

        # Ana não tem WhatsApp: o envio é recusado antes de registrar qualquer coisa
        ped2 = self.pedido_pronto()
        r = self.post(f"/pedidos/{ped2}/ficha", {"colaborador_id": str(self.ana), "acao": "enviar"},
                      follow_redirects=True)
        self.assertIn("não tem WhatsApp", r.get_data(as_text=True))

        import io
        self.post(f"/fichas/{f['id']}/papel", {"anexo": (io.BytesIO(b"%PDF-1.4 teste"), "ficha.pdf")},
                  content_type="multipart/form-data")
        f = self.ficha(f["id"])
        self.assertEqual(f["status"], "assinada_papel")
        self.assertEqual(self.c.get(f"/fichas/{f['id']}/anexo").data, b"%PDF-1.4 teste")

        perfil_carlos = self.c.get(f"/colaboradores/{self.carlos}?aba=fichas").get_data(as_text=True)
        self.assertIn("retirado por <strong>Ana Encarregada</strong>", perfil_carlos)
        perfil_ana = self.c.get(f"/colaboradores/{self.ana}?aba=fichas").get_data(as_text=True)
        self.assertIn("Retirou para <strong>Carlos Obra</strong>", perfil_ana)

    def test_consulta_nao_emite_ficha(self):
        ped = self.pedido_pronto()
        self.post("/usuarios/novo", {"nome": "Ver", "login": "ver", "perfil": "consulta",
                                     "senha": "senha1234", "confirmacao": "senha1234"})
        outro = self.app.test_client()
        self.post("/login", {"login": "ver", "senha": "senha1234"}, client=outro)
        self.assertEqual(outro.get(f"/pedidos/{ped}/ficha").status_code, 403)
        self.assertEqual(self.post(f"/pedidos/{ped}/ficha", {"colaborador_id": str(self.carlos), "acao": "imprimir"},
                                   client=outro).status_code, 403)


class TestModelosDocumento(BaseFicha):
    def form_modelo(self, **extra):
        dados = {
            "nome": "Ficha obras", "tipo": "ficha_epi", "padrao": "1",
            "titulo": "Entrega de EPI do pedido #{pedido.numero} · {pedido.obra}",
            "subtitulo": "{colaborador.nome|maiusculas}", "empresa": "Empresa Teste LTDA",
            "cab_campo": ["colaborador.funcao", "pedido.motivo"], "cab_rotulo": ["Função", "Motivo"],
            "col_ativa": ["quantidade", "higienizado", "rubrica"], "col_rotulo_quantidade": "Quant.",
            "higienizado_padrao": "NAO", "texto_beneficiario": "",
            "declaracao": "Recebi os itens acima.\n\nSegundo parágrafo.", "linha_data": "Joinville, {data_extenso}",
            "assinatura_nome": "{colaborador.nome}",
        }
        dados.update(extra)
        return dados

    def test_modelo_padrao_criado_e_aba_so_com_permissao(self):
        html = self.c.get("/modelos").get_data(as_text=True)
        self.assertIn("Ficha de EPI (padrão RH Gestor)", html)
        self.assertIn("Padrão na retirada", html)
        self.post("/usuarios/novo", {"nome": "Almox", "login": "almox", "perfil": "almoxarifado",
                                     "senha": "senha1234", "confirmacao": "senha1234"})
        outro = self.app.test_client()
        self.post("/login", {"login": "almox", "senha": "senha1234"}, client=outro)
        self.assertEqual(outro.get("/modelos").status_code, 403)
        self.assertNotIn('href="/modelos"', outro.get("/pedidos").get_data(as_text=True))

    def test_retirada_usa_modelo_padrao_e_congela_na_ficha(self):
        r = self.post("/modelos/novo", self.form_modelo())
        self.assertEqual(r.status_code, 302)
        ped = self.pedido_pronto()
        html = self.c.get(f"/pedidos/{ped}/ficha").get_data(as_text=True)
        self.assertIn("Entrega de EPI do pedido #", html)
        self.assertIn("· Acordes", html)
        self.assertIn("Joinville, ", html)
        self.assertIn("Desgaste", html)          # motivo no cabeçalho
        self.assertIn("Quant.", html)
        self.assertNotIn("Código CA", html)      # coluna desmarcada
        self.assertIn('<option value="NAO" selected>', html)  # higienizado padrão do modelo
        self.post(f"/pedidos/{ped}/ficha", {"colaborador_id": str(self.carlos), "acao": "imprimir"})
        f = self.ficha()
        with self.app.app_context():
            modelo_id = get_db().execute("SELECT id FROM modelos_documento WHERE nome = 'Ficha obras'").fetchone()["id"]
        # editar o modelo depois não muda a ficha emitida
        self.post(f"/modelos/{modelo_id}", self.form_modelo(titulo="Outro título"))
        html = self.c.get(f"/fichas/{f['id']}").get_data(as_text=True)
        self.assertIn("Entrega de EPI do pedido #", html)
        self.assertNotIn("Outro título", html)

    def test_parametro_inexistente_e_previa(self):
        r = self.post("/modelos/novo", self.form_modelo(titulo="Ficha {colaborador.apelido}"))
        self.assertEqual(r.status_code, 400)
        self.assertIn("{colaborador.apelido}", r.get_data(as_text=True))
        previa = self.post("/modelos/previa", self.form_modelo(titulo="Ficha · {empresa}")).get_data(as_text=True)
        self.assertIn("JOÃO DA SILVA", previa)
        self.assertIn("Empresa Teste LTDA", previa)

    def test_pedido_sem_baixa_tambem_gera_ficha(self):
        self.post("/pedidos/simular", {"nome": "Carlos Obra", "local": "Acordes", "motivo": "Desgaste",
                                       "epi": "Item que não existe"})
        with self.app.app_context():
            db = get_db()
            ped = db.execute("SELECT id FROM pedidos ORDER BY id DESC LIMIT 1").fetchone()["id"]
            db.execute("UPDATE pedidos SET colaborador_id = ?, status = 'pronto' WHERE id = ?", (self.carlos, ped))
        html = self.c.get(f"/pedidos/{ped}/ficha").get_data(as_text=True)
        self.assertIn("Nenhum item foi baixado do estoque", html)
        self.post(f"/pedidos/{ped}/ficha", {"colaborador_id": str(self.carlos), "acao": "imprimir"})
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT status FROM pedidos WHERE id = ?", (ped,)).fetchone()[0],
                             "retirado")
        self.assertEqual(self.ficha()["pedido_id"], ped)
