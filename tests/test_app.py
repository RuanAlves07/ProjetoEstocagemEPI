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
        self.mov("entrada", self.epi, 10)
        self.mov("entrada", epi_extra, 5)
        # Define o padrão para o cargo "Montador" via /colaboradores/cargos
        r_cargo = self.post("/colaboradores/cargos", {"cargo": "Montador", "epi_id": [str(self.epi)]}, follow_redirects=True)
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


if __name__ == "__main__":
    unittest.main()
