import os
import re
import tempfile
import unittest
from datetime import timedelta

from app import create_app, estoque
from app.db import get_db
from app.util import hoje


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.app = create_app({"DATABASE": os.path.join(self.dir.name, "t.sqlite"), "TESTING": True})
        self.c = self.app.test_client()

    def tearDown(self):
        self.dir.cleanup()

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
                "/minha-senha", "/epis/novo", "/colaboradores/novo", comprovante,
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
        self.assertEqual(j_33135["dados"]["categoria"], "Auditivo")
        self.assertEqual(j_33135["dados"]["validade_ca"], "2028-07-12")

        r_40362 = self.c.get("/api/caepi/40362")
        self.assertEqual(r_40362.status_code, 200)
        j_40362 = r_40362.get_json()
        self.assertTrue(j_40362["ok"])
        self.assertEqual(j_40362["dados"]["ca"], "40362")
        self.assertEqual(j_40362["dados"]["categoria"], "Calçados")
        self.assertEqual(j_40362["dados"]["validade_ca"], "2027-03-30")

        r_api = self.c.get("/api/caepi/39840")
        self.assertEqual(r_api.status_code, 200)
        j = r_api.get_json()
        self.assertTrue(j["ok"])
        self.assertEqual(j["dados"]["ca"], "39840")
        self.assertEqual(j["dados"]["categoria"], "Mãos")
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
        self.assertIn("Calçados", html_det)
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


if __name__ == "__main__":
    unittest.main()
