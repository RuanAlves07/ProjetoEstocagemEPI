import re

from app import estoque, pedidos
from app.db import get_db
from tests.test_app import Base


class BasePedidos(Base):
    def setUp(self):
        super().setUp()
        self.setup_admin()
        self.luva = self.criar_epi("Luva G")
        self.bota = self.criar_epi("Bota 40")
        self.camiseta = self.criar_epi("Camiseta M")
        self.joao = self.criar_colab("João da Silva")
        for epi in (self.luva, self.bota, self.camiseta):
            self.mov("entrada", epi, 10)
        with self.app.app_context():
            self.key = pedidos.obter_api_key(get_db())

    def api(self, dados, chave=None, **kw):
        return self.app.test_client().post(
            "/api/pedidos", json=dados, headers={"X-API-Key": self.key if chave is None else chave}, **kw)

    def novo_pedido(self, **extra):
        dados = {"id": "resp-1", "nome": "João da Silva", "local": "Obra A", "motivo": "Reposição",
                 "epi": "2x Luva G", "sapatao": "Bota 40", "camiseta": "M", "observacao": "Urgente", **extra}
        r = self.api(dados)
        self.assertIn(r.status_code, (200, 201), r.get_data(as_text=True))
        return r.get_json()["id"]

    def itens(self, pedido_id):
        with self.app.app_context():
            return [dict(r) for r in get_db().execute(
                "SELECT * FROM pedido_itens WHERE pedido_id = ? ORDER BY id", (pedido_id,))]

    def pedido(self, pedido_id):
        with self.app.app_context():
            return dict(get_db().execute("SELECT * FROM pedidos WHERE id = ?", (pedido_id,)).fetchone())

    def definir_cargo(self, colab_id, cargo, epi_ids):
        """Define o cargo do colaborador e o kit padrão (EPIs) desse cargo."""
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE colaboradores SET funcao = ? WHERE id = ?", (cargo, colab_id))
            db.execute("DELETE FROM cargo_epis_padrao WHERE cargo = ?", (cargo,))
            for eid in epi_ids:
                db.execute("INSERT INTO cargo_epis_padrao (cargo, epi_id) VALUES (?, ?)", (cargo, eid))

    def separar_todos(self, pedido_id):
        for it in self.itens(pedido_id):
            if not it["ignorado"]:
                r = self.post(f"/pedidos/{pedido_id}/itens/{it['id']}/separar", {"separado": "1"})
                self.assertEqual(r.status_code, 200, r.get_data(as_text=True))


class TestApi(BasePedidos):
    def test_exige_chave(self):
        self.assertEqual(self.api({"nome": "X"}, chave="errada").status_code, 401)
        self.assertEqual(self.app.test_client().post("/api/pedidos", json={"nome": "X"}).status_code, 401)

    def test_validacoes(self):
        self.assertEqual(self.api({"local": "Obra"}).status_code, 400)
        r = self.app.test_client().post("/api/pedidos", data="não é json", headers={"X-API-Key": self.key})
        self.assertEqual(r.status_code, 400)

    def test_cria_pedido_e_identifica_itens(self):
        pid = self.novo_pedido()
        p = self.pedido(pid)
        self.assertEqual((p["status"], p["local_destino"], p["colaborador_id"]), ("novo", "Obra A", self.joao))
        por_texto = {i["texto_original"]: i for i in self.itens(pid)}
        self.assertEqual(por_texto["Luva G"]["epi_id"], self.luva)
        self.assertEqual(por_texto["Luva G"]["quantidade"], 2)
        self.assertEqual(por_texto["Bota 40"]["epi_id"], self.bota)
        self.assertEqual(por_texto["M"]["epi_id"], self.camiseta)    # "Camiseta" + "M"

    def test_idempotente_pelo_id_da_resposta(self):
        a = self.novo_pedido()
        r = self.api({"id": "resp-1", "nome": "João da Silva", "epi": "Luva G"})
        self.assertEqual((r.status_code, r.get_json()["id"], r.get_json()["criado"]), (200, a, False))
        self.assertEqual(len(self.itens(a)), 3)

    def test_multipla_escolha_do_forms_e_respostas_nao(self):
        pid = self.novo_pedido(epi='["Luva G","Óculos"]', calca="Não", vestimento="")
        textos = [i["texto_original"] for i in self.itens(pid)]
        self.assertIn("Óculos", textos)
        self.assertNotIn("Não", textos)
        oculos = next(i for i in self.itens(pid) if i["texto_original"] == "Óculos")
        self.assertIsNone(oculos["epi_id"])

    def test_colaborador_nao_cadastrado(self):
        pid = self.novo_pedido(id="x", nome="Maria Nova")
        self.assertIsNone(self.pedido(pid)["colaborador_id"])

    def test_nome_com_acento_e_caixa_diferentes_casa(self):
        pid = self.novo_pedido(id="y", nome="  JOAO DA SILVA ")
        self.assertEqual(self.pedido(pid)["colaborador_id"], self.joao)


class TestFluxo(BasePedidos):
    def test_fluxo_completo_com_baixa_no_estoque(self):
        pid = self.novo_pedido()
        # mais tarde -> pendente; aparece na lista
        self.post(f"/pedidos/{pid}/mais-tarde", {"voltar": "/pedidos"})
        self.assertEqual(self.pedido(pid)["status"], "pendente")
        self.assertIn("Pendentes", self.c.get("/pedidos").get_data(as_text=True))
        # iniciar
        self.post(f"/pedidos/{pid}/iniciar")
        self.assertEqual(self.pedido(pid)["status"], "em_separacao")
        # finalizar sem separar tudo: bloqueado e sem baixa
        r = self.post(f"/pedidos/{pid}/finalizar", follow_redirects=True)
        self.assertIn("separados", r.get_data(as_text=True))
        self.assertEqual(self.saldo(self.luva), 10)
        self.separar_todos(pid)
        self.post(f"/pedidos/{pid}/finalizar")
        p = self.pedido(pid)
        self.assertEqual(p["status"], "pronto")
        self.assertEqual((self.saldo(self.luva), self.saldo(self.bota), self.saldo(self.camiseta)), (8, 9, 9))
        with self.app.app_context():
            mov = get_db().execute("SELECT * FROM movimentacoes WHERE pedido_id = ? AND tipo = 'saida'",
                                   (pid,)).fetchall()
        self.assertEqual(len(mov), 3)
        self.assertTrue(all(m["local_destino"] == "Obra A" and m["colaborador_id"] == self.joao for m in mov))
        # retirada por terceiro
        self.post(f"/pedidos/{pid}/retirar", {"retirado_por_nome": "Carlos", "retirado_descricao": "Encarregado"})
        p = self.pedido(pid)
        self.assertEqual((p["status"], p["retirado_por_nome"]), ("retirado", "Carlos"))
        comp = self.c.get(f"/entregas/{p['grupo']}/comprovante").get_data(as_text=True)
        self.assertIn("Carlos", comp)
        self.assertIn("Obra A", self.c.get("/relatorios").get_data(as_text=True))
        self.assertIn("Carlos", self.c.get("/pedidos/exportar.csv").get_data(as_text=True))

    def test_item_nao_identificado_bloqueia_ate_identificar_e_aprende(self):
        pid = self.novo_pedido(epi="Protetor auricular", sapatao="", camiseta="")
        self.post(f"/pedidos/{pid}/iniciar")
        item = next(i for i in self.itens(pid) if i["texto_original"] == "Protetor auricular")
        # não dá para marcar sem identificar
        r = self.post(f"/pedidos/{pid}/itens/{item['id']}/separar", {"separado": "1"})
        self.assertEqual(r.status_code, 400)
        prot = self.criar_epi("Protetor auricular tipo plug")
        self.mov("entrada", prot, 5)
        self.post(f"/pedidos/{pid}/itens/{item['id']}/editar",
                  {"epi_id": str(prot), "quantidade": "1", "lembrar": "1"})
        # próximo pedido já reconhece sozinho
        novo = self.novo_pedido(id="outro", epi="protetor AURICULAR", sapatao="", camiseta="")
        self.assertEqual(self.itens(novo)[0]["epi_id"], prot)

    def test_ignorar_sempre(self):
        pid = self.novo_pedido(vestimento="Sim", epi="", sapatao="", camiseta="")
        item = self.itens(pid)[0]
        self.post(f"/pedidos/{pid}/iniciar")
        self.post(f"/pedidos/{pid}/itens/{item['id']}/ignorar", {"sempre": "1"})
        novo = self.novo_pedido(id="z", vestimento="Sim", epi="", sapatao="", camiseta="")
        self.assertEqual(self.itens(novo)[0]["ignorado"], 1)

    def test_sem_saldo_reverte_tudo(self):
        pid = self.novo_pedido(epi="20x Luva G", sapatao="Bota 40", camiseta="")
        self.post(f"/pedidos/{pid}/iniciar")
        self.separar_todos(pid)
        r = self.post(f"/pedidos/{pid}/finalizar", follow_redirects=True)
        self.assertIn("Saldo insuficiente", r.get_data(as_text=True))
        self.assertEqual(self.pedido(pid)["status"], "em_separacao")
        self.assertEqual((self.saldo(self.luva), self.saldo(self.bota)), (10, 10))

    def test_finalizar_exige_colaborador_vinculado(self):
        pid = self.novo_pedido(nome="Maria Nova", sapatao="", camiseta="")
        self.post(f"/pedidos/{pid}/iniciar")
        self.separar_todos(pid)
        r = self.post(f"/pedidos/{pid}/finalizar", follow_redirects=True)
        self.assertIn("Vincule", r.get_data(as_text=True))
        self.post(f"/pedidos/{pid}/colaborador", {"criar": "1"})
        self.post(f"/pedidos/{pid}/finalizar")
        self.assertEqual(self.pedido(pid)["status"], "pronto")

    def test_nao_finaliza_duas_vezes(self):
        pid = self.novo_pedido(sapatao="", camiseta="")
        self.post(f"/pedidos/{pid}/iniciar")
        self.separar_todos(pid)
        self.post(f"/pedidos/{pid}/finalizar")
        self.post(f"/pedidos/{pid}/finalizar")
        self.assertEqual(self.saldo(self.luva), 8)

    def test_trocar_epi_invalida_separacao(self):
        pid = self.novo_pedido(sapatao="", camiseta="")
        self.post(f"/pedidos/{pid}/iniciar")
        self.separar_todos(pid)
        item = self.itens(pid)[0]
        self.post(f"/pedidos/{pid}/itens/{item['id']}/editar", {"epi_id": str(self.bota), "quantidade": "1"})
        self.assertEqual(self.itens(pid)[0]["separado"], 0)

    def test_cancelar_pronto_estorna_somente_admin(self):
        pid = self.novo_pedido(sapatao="", camiseta="")
        self.post(f"/pedidos/{pid}/iniciar")
        self.separar_todos(pid)
        self.post(f"/pedidos/{pid}/finalizar")
        self.assertEqual(self.saldo(self.luva), 8)
        # almoxarifado não estorna
        self.post("/usuarios/novo", {"nome": "Alm", "login": "alm", "perfil": "almoxarifado",
                                     "senha": "senha1234", "confirmacao": "senha1234"})
        alm = self.app.test_client()
        self.post("/login", {"login": "alm", "senha": "senha1234"}, client=alm)
        self.post(f"/pedidos/{pid}/cancelar", {"motivo": "teste"}, client=alm)
        self.assertEqual(self.pedido(pid)["status"], "pronto")
        self.post(f"/pedidos/{pid}/cancelar", {"motivo": "Desistiu"})
        self.assertEqual(self.pedido(pid)["status"], "cancelado")
        self.assertEqual(self.saldo(self.luva), 10)

    def test_cancelar_antes_da_baixa_nao_mexe_no_estoque(self):
        pid = self.novo_pedido()
        self.post(f"/pedidos/{pid}/cancelar", {"motivo": "Duplicado"})
        self.assertEqual(self.pedido(pid)["status"], "cancelado")
        self.assertEqual(self.saldo(self.luva), 10)

    def test_consulta_nao_age(self):
        pid = self.novo_pedido()
        self.post("/usuarios/novo", {"nome": "Ver", "login": "ver", "perfil": "consulta",
                                     "senha": "senha1234", "confirmacao": "senha1234"})
        ver = self.app.test_client()
        self.post("/login", {"login": "ver", "senha": "senha1234"}, client=ver)
        self.assertEqual(self.post(f"/pedidos/{pid}/iniciar", client=ver).status_code, 403)
        self.assertEqual(ver.get(f"/pedidos/{pid}").status_code, 200)
        self.assertEqual(ver.get("/pedidos/novos").get_json()["novos"], 0)
        self.assertEqual(ver.get("/integracao").status_code, 403)

    def test_sinalizacao_analisar_e_finalizar_com_ressalva(self):
        # Define o cargo "Pedreiro" para João da Silva, com kit padrão contendo apenas "Luva G" e tamanho de Bota = 40
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE colaboradores SET funcao = 'Pedreiro' WHERE id = ?", (self.joao,))
            db.execute("INSERT INTO cargo_epis_padrao (cargo, epi_id) VALUES ('Pedreiro', ?)", (self.luva,))
            db.execute(
                "INSERT INTO colaborador_tamanhos (colaborador_id, chave, tamanho) VALUES (?, 'bota', '40')",
                (self.joao,),
            )

        # Cria pedido onde Bota 40 é pedida com tamanho 42 (diverge da ficha e está fora do padrão do cargo Pedreiro)
        pid = self.novo_pedido(epi=["Luva G", "Bota 40"], tam_bota="42", sapatao="", camiseta="M")
        self.post(f"/pedidos/{pid}/iniciar")

        html_detalhe = self.c.get(f"/pedidos/{pid}").get_data(as_text=True)
        self.assertIn("Fora do padrão do cargo (Pedreiro)", html_detalhe)
        self.assertIn("Tamanho diverge da ficha (ficha: 40)", html_detalhe)
        self.assertIn("Finalizar com ressalva", html_detalhe)
        self.assertNotIn("Remover do pedido", html_detalhe)
        self.assertNotIn("+ Adicionar item que não veio no formulário", html_detalhe)

        # Marca apenas a Luva G como separada (deixa Bota 40 e Camiseta M desmarcadas)
        itens = self.itens(pid)
        item_luva = next(i for i in itens if i["epi_id"] == self.luva)
        self.post(f"/pedidos/{pid}/itens/{item_luva['id']}/separar", {"separado": "1"})

        # Tentativa de finalizar sem observação na ressalva é bloqueada
        r_sem_obs = self.post(f"/pedidos/{pid}/finalizar", {
            "com_ressalva": "1",
            "ressalva_observacao": "   ",
        }, follow_redirects=True)
        self.assertIn("Informe a observação/motivo da ressalva", r_sem_obs.get_data(as_text=True))
        self.assertEqual(self.pedido(pid)["status"], "em_separacao")

        # Finaliza com ressalva informando a justificativa
        r_ok = self.post(f"/pedidos/{pid}/finalizar", {
            "com_ressalva": "1",
            "ressalva_observacao": "Bota Tam. 42 e Camiseta fora do padrão do cargo de Pedreiro não autorizadas.",
        }, follow_redirects=True)
        self.assertEqual(r_ok.status_code, 200)
        html_pronto = r_ok.get_data(as_text=True)
        self.assertIn("finalizado com ressalva", html_pronto.lower())
        self.assertIn("Bota Tam. 42 e Camiseta fora do padrão do cargo", html_pronto)
        self.assertIn("não atendido (ressalva)", html_pronto)

        # Verifica que apenas a Luva G teve baixa no estoque (10 -> 9); Bota e Camiseta continuam com 10
        self.assertEqual((self.saldo(self.luva), self.saldo(self.bota), self.saldo(self.camiseta)), (9, 10, 10))
        p_atual = self.pedido(pid)
        self.assertEqual(p_atual["status"], "pronto")
        self.assertIn("fora do padrão do cargo", p_atual["ressalva_observacao"])


class TestTelas(BasePedidos):
    def test_novos_devolve_card_e_contagem(self):
        pid = self.novo_pedido()
        d = self.c.get("/pedidos/novos").get_json()
        self.assertEqual((d["novos"], d["a_atender"]), (1, 1))
        self.assertIn("Iniciar separação", d["html"])
        self.assertIn("Mais tarde", d["html"])
        self.post(f"/pedidos/{pid}/mais-tarde")
        d = self.c.get("/pedidos/novos").get_json()
        self.assertEqual((d["novos"], d["a_atender"]), (0, 1))

    def test_paginas_renderizam_em_todos_os_estados(self):
        pid = self.novo_pedido()
        urls = ["/pedidos", "/pedidos?aba=retirados", "/pedidos?aba=cancelados", f"/pedidos/{pid}", "/integracao"]
        for u in urls:
            self.assertEqual(self.c.get(u).status_code, 200, u)
        self.post(f"/pedidos/{pid}/iniciar")
        self.assertEqual(self.c.get(f"/pedidos/{pid}").status_code, 200)
        self.separar_todos(pid)
        self.post(f"/pedidos/{pid}/finalizar")
        html = self.c.get(f"/pedidos/{pid}").get_data(as_text=True)
        self.assertIn("Pronto para retirada", html)
        self.assertIn("Marcar como retirado", html)

    def test_xss_no_pedido_escapado(self):
        pid = self.novo_pedido(id="xss", nome="<img src=x onerror=1>", observacao="<script>1</script>")
        for u in (f"/pedidos/{pid}", "/pedidos", "/pedidos/novos"):
            r = self.c.get(u)
            texto = r.get_data(as_text=True) if u != "/pedidos/novos" else r.get_json()["html"]
            self.assertNotIn("<script>1</script>", texto)
            self.assertNotIn("<img src=x", texto)

    def test_banco_antigo_e_migrado(self):
        import sqlite3
        import tempfile, os
        from app import create_app
        d = tempfile.mkdtemp()
        caminho = os.path.join(d, "velho.sqlite")
        con = sqlite3.connect(caminho)
        con.executescript("""CREATE TABLE usuarios (id INTEGER PRIMARY KEY, nome TEXT, login TEXT, senha_hash TEXT,
            perfil TEXT, ativo INTEGER DEFAULT 1, criado_em TEXT);
            CREATE TABLE epis (id INTEGER PRIMARY KEY, nome TEXT);
            CREATE TABLE movimentacoes (id INTEGER PRIMARY KEY, grupo TEXT, tipo TEXT, epi_id INTEGER,
            quantidade INTEGER, data TEXT, colaborador_id INTEGER, setor TEXT, ca TEXT, fornecedor TEXT,
            nota_fiscal TEXT, custo_unitario REAL, motivo TEXT, observacao TEXT, usuario_id INTEGER,
            criado_em TEXT, cancelada INTEGER DEFAULT 0, cancelada_motivo TEXT, cancelada_por INTEGER,
            cancelada_em TEXT);
            INSERT INTO epis VALUES (1, 'Luva');""")
        con.commit(); con.close()
        app = create_app({"DATABASE": caminho, "TESTING": True})
        with app.app_context():
            cols = {r["name"] for r in get_db().execute("PRAGMA table_info(movimentacoes)")}
            self.assertTrue({"pedido_id", "local_destino"} <= cols)
            self.assertEqual(get_db().execute("SELECT COUNT(*) FROM pedidos").fetchone()[0], 0)


class TestParser(BasePedidos):
    def test_dividir_itens(self):
        self.assertEqual(pedidos.dividir_itens("2x Luva G; Óculos, 3 x Bota 40\nCapacete x2"),
                         [(2, "Luva G"), (1, "Óculos"), (3, "Bota 40"), (2, "Capacete")])
        self.assertEqual(pedidos.dividir_itens("  ;; "), [])

    def test_simular_e_importar_csv_colunas_h_a_p(self):
        import io
        r = self.post("/pedidos/simular", {
            "nome": "João da Silva", "local": "Obra B", "motivo": "Admissão",
            "epi": "Luva G", "sapatao": "Bota 40", "camiseta": "M", "observacao": "Teste manual"
        }, follow_redirects=True)
        self.assertIn("Pedido #", r.get_data(as_text=True))

        # Planilha com colunas A..P do Microsoft Forms (H=Nome, I=Local, J=Motivo, K=Vestimento, L=EPI, M=Sapatão, N=Camiseta, O=Calça, P=Obersvação)
        cab = ["ID", "Início", "Conclusão", "Email", "Nome MS", "Modificado", "Extra",
               "Nome", "Local", "Motivo", "Vestimento", "EPI", "Sapatão", "Camiseta", "Calça", "Obersvação"]
        lin = ["99", "", "", "", "", "", "",
               "João da Silva", "Obra Sul", "Troca", "Não", "Luva G", "Bota 40", "M", "Não", "Obs Forms"]
        conteudo = (";".join(cab) + "\n" + ";".join(lin) + "\n").encode("utf-8")
        r = self.post("/pedidos/importar-csv", {"arquivo": (io.BytesIO(conteudo), "forms.csv")},
                      follow_redirects=True, content_type="multipart/form-data")
        self.assertIn("1 pedido(s) importado(s)", r.get_data(as_text=True))
        # Reimportar a mesma planilha ignora o ID 99 duplicado
        r2 = self.post("/pedidos/importar-csv", {"arquivo": (io.BytesIO(conteudo), "forms.csv")},
                       follow_redirects=True, content_type="multipart/form-data")
        self.assertIn("0 pedido(s) importado(s)", r2.get_data(as_text=True))

    def test_formulario_publico_sem_login_e_tamanhos(self):
        sapatao_epi = self.criar_epi("Sapatão Bidensidade")
        botina_epi = self.criar_epi("Botina Bico PVC")
        self.mov("entrada", sapatao_epi, 5)
        self.mov("entrada", botina_epi, 5)
        self.definir_cargo(self.joao, "Pedreiro", [self.luva, self.bota, self.camiseta, sapatao_epi, botina_epi])

        # Cliente anônimo (sem login) acessa /solicitar
        anon = self.app.test_client()
        r_get = anon.get("/solicitar")
        self.assertEqual(r_get.status_code, 200)
        html_form = r_get.get_data(as_text=True)
        self.assertIn("UNIFORME E EPI", html_form)
        self.assertNotIn("Informe a data do pedido", html_form)
        self.assertNotIn("filtro-colaborador", html_form)
        self.assertIn("João da Silva", html_form)

        # Enviar só tamanho de uniforme sem marcar nenhum item é bloqueado
        r_invalido = anon.post("/solicitar", data={
            "nome": "João da Silva", "local": "Araquari", "motivo": "Desgaste", "camiseta": "G",
        }, follow_redirects=True)
        self.assertIn("Selecione ao menos um equipamento", r_invalido.get_data(as_text=True))

        # Restrição somente números no tamanho do calçado
        r_letra = anon.post("/solicitar", data={
            "nome": "João da Silva", "local": "Araquari", "motivo": "Desgaste",
            "epi": ["Sapatão Bidensidade"], "tam_sapatao": "41A",
        }, follow_redirects=True)
        self.assertIn("somente números", r_letra.get_data(as_text=True))

        # Envio completo marcando Sapatão (41) + Botina (40) + Bota (42) + Camiseta (G) com tamanhos distintos
        r_post = anon.post("/solicitar", data={
            "nome": "João da Silva",
            "local": "Araquari",
            "motivo": "Desgaste",
            "epi": ["Luva G", "Sapatão Bidensidade", "Botina Bico PVC", "Bota 40"],
            "vestimento": ["Camiseta M"],
            "tam_sapatao": "41",
            "tam_botina": "40",
            "tam_bota": "42",
            "camiseta": "G",
            "calca": "GG",  # deve ser ignorado pois nenhuma calça foi selecionada
            "observacao": "Entregar na obra de Araquari",
        }, follow_redirects=True)
        self.assertEqual(r_post.status_code, 200)
        html_ok = r_post.get_data(as_text=True)
        self.assertIn("enviada com sucesso", html_ok)
        self.assertIn("Tam. 41", html_ok)
        self.assertIn("Tam. 40", html_ok)
        self.assertIn("Tam. 42", html_ok)
        self.assertIn("Tam. G", html_ok)
        self.assertNotIn("Tam. GG", html_ok)

        # Confere se o pedido entrou na fila como 'novo' com todos os 5 itens identificados e numerações distintas
        with self.app.app_context():
            carregados = pedidos.carregar(get_db(), status=("novo",))
            self.assertEqual(len(carregados), 1)
            p = carregados[0]
            self.assertEqual(p["solicitante"], "João da Silva")
            self.assertEqual(p["local_destino"], "Araquari")
            self.assertEqual(p["motivo"], "Desgaste")
            self.assertEqual(p["colaborador_id"], self.joao)
            self.assertEqual(p["total_itens"], 5)
            self.assertEqual(p["nao_identificados"], 0)
            por_epi = {i["epi_nome"]: i["tamanho"] for i in p["itens"]}
            self.assertEqual(por_epi["Sapatão Bidensidade"], "41")
            self.assertEqual(por_epi["Botina Bico PVC"], "40")
            self.assertEqual(por_epi["Bota 40"], "42")
            self.assertEqual(por_epi["Camiseta M"], "G")

    def test_personalizar_formulario_cidades_equipamentos_e_imagens(self):
        import io
        # 1x1 PNG transparente válido para testar upload de foto do EPI
        png_1x1 = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
            b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )

        # Tela de personalização abre para administrador
        r_ed = self.c.get("/solicitar/personalizar")
        self.assertEqual(r_ed.status_code, 200)
        self.assertIn("MODO EDITOR", r_ed.get_data(as_text=True))

        # Salva nova cidade ("Joinville - Obra Norte"), novo motivo, pergunta extra e foto no EPI Luva G
        r_save = self.post("/solicitar/personalizar", {
            "atualizar_opcoes_epi": "1",
            "titulo": "FORMULÁRIO PERSONALIZADO DE EPI",
            "descricao": "Solicite seus equipamentos com identificação por imagem.",
            "pergunta_1_titulo": "1. Selecione o seu nome completo.",
            "pergunta_2_titulo": "2. Informe a cidade da obra.",
            "pergunta_3_titulo": "3. Informe o motivo.",
            "pergunta_4_titulo": "4. Selecione o equipamento de proteção individual necessário.",
            "pergunta_4_subtitulo": "Veja a imagem de cada equipamento antes de marcar.",
            "pergunta_5_titulo": "5. Selecione a vestimenta necessária abaixo",
            "pergunta_6_titulo": "6. Observações",
            "locais": ["Mafra", "Araquari"],
            "novo_local": "Joinville - Obra Norte",
            "motivos": ["Desgaste", "Perda"],
            "novo_motivo": "Admissão Inicial",
            "tamanhos_uniforme": ["P", "M", "G", "GG"],
            "novo_extra_titulo": "Qual o turno de trabalho?",
            "novo_extra_tipo": "unica",
            "novo_extra_opcoes": "Diurno; Noturno",
            "epi_visivel": [str(self.luva), str(self.bota), str(self.camiseta)],
            f"nome_epi_{self.luva}": "Luva G",
            f"imagem_epi_{self.luva}": (io.BytesIO(png_1x1), "luva.png"),
        }, follow_redirects=True, content_type="multipart/form-data")
        self.assertEqual(r_save.status_code, 200)
        self.assertIn("Personalizações do formulário salvas", r_save.get_data(as_text=True))

        # Adiciona um novo equipamento direto pelo editor do formulário com ilustração automática
        r_novo_epi = self.post("/solicitar/personalizar/epi/novo", {
            "nome": "Protetor Facial Incolor",
            "tipo_secao": "epi",
            "unidade": "un",
            "gerar_ilustracao": "1",
        }, follow_redirects=True)
        self.assertEqual(r_novo_epi.status_code, 200)
        self.assertIn("Protetor Facial Incolor", r_novo_epi.get_data(as_text=True))

        # Verifica que o formulário público reflete todas as personalizações e exibe as imagens nas opções
        anon = self.app.test_client()
        html_pub = anon.get("/solicitar").get_data(as_text=True)
        self.assertIn("FORMULÁRIO PERSONALIZADO DE EPI", html_pub)
        self.assertIn("Joinville - Obra Norte", html_pub)
        self.assertIn("Admissão Inicial", html_pub)
        self.assertIn("Protetor Facial Incolor", html_pub)
        self.assertIn("Qual o turno de trabalho?", html_pub)
        self.assertIn("data:image/png;base64,", html_pub)
        self.assertIn("data:image/svg+xml;base64,", html_pub)
        self.assertIn("img-opcao-epi", html_pub)

        # Submete um pedido pelo formulário público usando a nova cidade, o novo EPI e a pergunta extra
        with self.app.app_context():
            protetor = get_db().execute("SELECT id FROM epis WHERE nome = 'Protetor Facial Incolor'").fetchone()["id"]
        self.definir_cargo(self.joao, "Pedreiro", [self.luva, protetor])
        r_envio = anon.post("/solicitar", data={
            "nome": "João da Silva",
            "local": "Joinville - Obra Norte",
            "motivo": "Admissão Inicial",
            "epi": ["Protetor Facial Incolor", "Luva G"],
            "extra_qextra_1": "Diurno",
            "observacao": "Preciso para segunda-feira",
        }, follow_redirects=True)
        self.assertEqual(r_envio.status_code, 200)
        html_envio = r_envio.get_data(as_text=True)
        self.assertIn("enviada com sucesso", html_envio)
        self.assertIn("Joinville - Obra Norte", html_envio)
        self.assertIn("Protetor Facial Incolor", html_envio)
        self.assertIn("Qual o turno de trabalho?: Diurno", html_envio)


class TestFormularioPorCargo(BasePedidos):
    def setUp(self):
        super().setUp()
        # Pedreiro: kit padrão é só Luva G e Bota 40 (Camiseta M fica fora da função)
        self.definir_cargo(self.joao, "Pedreiro", [self.luva, self.bota])
        self.pedido_base = {"nome": "João da Silva", "local": "Araquari", "motivo": "Desgaste"}

    def criar_usuario(self, login, perfil):
        self.post("/usuarios/novo", {"nome": login.title(), "login": login, "perfil": perfil,
                                     "senha": "senha1234", "confirmacao": "senha1234"})
        cliente = self.app.test_client()
        self.post("/login", {"login": login, "senha": "senha1234"}, client=cliente)
        return cliente

    def test_publico_aceita_somente_itens_do_cargo(self):
        anon = self.app.test_client()
        html = anon.get("/solicitar").get_data(as_text=True)
        self.assertIn("somente os equipamentos do padrão do seu cargo", html)
        self.assertNotIn("Justificativa dos itens fora da função", html)

        r_fora = anon.post("/solicitar", data={**self.pedido_base, "epi": ["Luva G"],
                                               "vestimento": ["Camiseta M"], "camiseta": "G"},
                           follow_redirects=True)
        texto = r_fora.get_data(as_text=True)
        self.assertIn("não fazem parte do padrão do seu cargo", texto)
        self.assertIn("procure o seu gestor", texto)

        r_ok = anon.post("/solicitar", data={**self.pedido_base, "epi": ["Luva G"]}, follow_redirects=True)
        self.assertIn("enviada com sucesso", r_ok.get_data(as_text=True))
        with self.app.app_context():
            self.assertEqual(len(pedidos.carregar(get_db(), status=("novo",))), 1)

    def test_publico_recusa_nome_fora_da_lista_e_item_inexistente(self):
        anon = self.app.test_client()
        r = anon.post("/solicitar", data={**self.pedido_base, "nome": "Fulano Inventado", "epi": ["Luva G"]},
                      follow_redirects=True)
        self.assertIn("colaboradores cadastrados", r.get_data(as_text=True))
        r = anon.post("/solicitar", data={**self.pedido_base, "epi": ["Item Que Não Existe"]}, follow_redirects=True)
        self.assertIn("não disponível no formulário", r.get_data(as_text=True))

    def test_publico_cargo_sem_padrao_nao_pede_nada(self):
        self.definir_cargo(self.joao, "Servente", [])
        anon = self.app.test_client()
        r = anon.post("/solicitar", data={**self.pedido_base, "epi": ["Luva G"]}, follow_redirects=True)
        self.assertIn("não fazem parte do padrão do seu cargo", r.get_data(as_text=True))

    def test_formulario_gestor_exige_login_e_permissao(self):
        anon = self.app.test_client()
        r = anon.get("/solicitar/gestor")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])
        r_post = anon.post("/solicitar/gestor", data={**self.pedido_base, "epi": ["Luva G"]})
        self.assertIn(r_post.status_code, (302, 400))   # sem sessão: barrado pelo CSRF ou mandado ao login
        with self.app.app_context():
            self.assertEqual(pedidos.carregar(get_db(), status=("novo",)), [])

        consulta = self.criar_usuario("ver", "consulta")
        self.assertEqual(consulta.get("/solicitar/gestor").status_code, 403)

        # Administrador tem acesso
        self.assertEqual(self.c.get("/solicitar/gestor").status_code, 200)

    def test_gestor_pede_fora_da_funcao_com_justificativa(self):
        gestor = self.criar_usuario("gestor1", "gestor")
        html = gestor.get("/solicitar/gestor").get_data(as_text=True)
        self.assertIn("Formulário do Gestor", html)
        self.assertIn("Justificativa dos itens fora da função", html)
        self.assertIn("Camiseta M", html)

        dados = {**self.pedido_base, "epi": ["Luva G"], "vestimento": ["Camiseta M"], "camiseta": "G"}
        r_sem = self.post("/solicitar/gestor", dados, client=gestor, follow_redirects=True)
        self.assertIn("Informe a justificativa", r_sem.get_data(as_text=True))

        r_ok = self.post("/solicitar/gestor", {**dados, "justificativa": "Teste como encarregado"},
                         client=gestor, follow_redirects=True)
        self.assertEqual(r_ok.status_code, 200)
        self.assertIn("enviada com sucesso", r_ok.get_data(as_text=True))
        with self.app.app_context():
            p = pedidos.carregar(get_db(), status=("novo",))[0]
        self.assertEqual(p["colaborador_id"], self.joao)
        self.assertIn("Solicitado pelo gestor: Gestor1", p["observacao"])
        self.assertIn("Fora da função: Camiseta M", p["observacao"])
        self.assertIn("Teste como encarregado", p["observacao"])

    def test_gestor_dentro_da_funcao_dispensa_justificativa(self):
        gestor = self.criar_usuario("gestor2", "gestor")
        r = self.post("/solicitar/gestor", {**self.pedido_base, "epi": ["Luva G"]}, client=gestor,
                      follow_redirects=True)
        self.assertIn("enviada com sucesso", r.get_data(as_text=True))
        with self.app.app_context():
            p = pedidos.carregar(get_db(), status=("novo",))[0]
        self.assertNotIn("Fora da função", p["observacao"])
