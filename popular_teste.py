"""Popula o banco (instance/epi.sqlite) com dados de demonstração que exercitam todas as funções do sistema.

Preserva os usuários e as obras já existentes. Apaga e recria: EPIs, fornecedores, colaboradores,
movimentações, pedidos e kits por cargo. Cadastra:
  - 33 itens reais do formulário + itens extras (CA vencido, estoque por tamanho, item fora do formulário);
  - 158 colaboradores reais + Ruan Alves (um inativo e um sem cargo, com padrão individual);
  - 4 fornecedores (1 inativo) e entradas, entregas, baixas, ajustes e um lançamento estornado;
  - pedidos em todas as etapas: novo, pendente, em separação, pronto, retirado, cancelado,
    finalizado com ressalva, com item não identificado e pedido do gestor fora da função;
  - uma obra concluída (inativa) com histórico.

Usuários de teste criados (só se o login ainda não existir):
  login              senha          perfil
  almoxarifado.teste Teste@2026     Almoxarifado
  consulta.teste     Teste@2026     Consulta
  gestor.teste       Teste@2026     Gestor (entra por /login/gestor)
  seguranca.teste    Teste@2026     Técnico de Segurança (perfil personalizado)

Uso:
    .venv/bin/python popular_teste.py
"""
from datetime import timedelta

from werkzeug.security import generate_password_hash

from app import create_app, estoque, pedidos
from app.db import get_db
from app.util import hoje

COLABORADORES_FORMULARIO = [
    "ABRAHAM JOSUE BARRETO CUELLO", "ADEMIR SCHIMIT GARVAO", "ADENIR FERNANDES SIQUEIRA", "ADILSON GIESE",
    "ADONIS LIAN MACHADO", "ADRIEL RODRIGO BARONI ORTEGA", "ALDONEI FLORIANO", "ALEJANDRO JOSE HERNANDEZ MARTINEZ",
    "ALESSANDRO DA COSTA DA CRUZ", "ALEXANDRE MUSCOPF", "ALEXANDRO DA SILVA DE OLIVEIRA", "ALISON ALLAN PACHECO",
    "ALPECIO INTHURN MARQUES", "AMINADABE MIRANDA DOS SANTOS", "ANDERSON SCHROEDER", "ANDRE FERREIRA PAULINO NETO",
    "ANTHONY GABRIEL PACHECO HENS", "ANTONIO AGADIR PIRES DE OLIVEIRA", "ANTONIO ALEMAR DE ALMEIDA",
    "ANTONIO LAIRTO BAIROS", "ANTONIO LOPES PINHEIRO NETO", "BRUNO MULLER DA SILVA", "CAIO FRANCISCO DENK",
    "CAIO JUNIOR BIELESKI", "CARLOS ARTUR FRANKE", "CARLOS DUARTE ALMEIDA DOS ANJOS", "CHARLES KOSTA HEIDRICH",
    "CLAUDIO LUIZ GERONIMO", "CLEBSON SANTOS RAMOS", "CLENOIR DA ROSA", "CLEYTON RODRIGO ANTONIOLLI IVACZEK",
    "DARIO GERALDO SANTANA", "DAVID GREGORIO CORDEIRO TOLOCZKO", "DAYLON FELIPE POLICARPO DOS SANTOS",
    "DELCI LUIZ RODIO", "DEYVID ESTEVAO", "ED AMES VIEIRA TAVARES", "EDENILSON RONCHY", "EDILSON FERNANDEZ DE SOUZA",
    "EDINILSON ARBIGAUS", "EDSON LUIZ", "EDSON SCHOLZ", "EDUARDO JOSE VIEIRA", "ELEANDRO FERREIRA",
    "ERIC JARDIM DIAS", "EUNICE DO CARMO CASSABONE", "EVEDILSO CMILO BARBOSA", "EVERALDO PAULA CIQUEIRA",
    "EVERTON ALVES INACIO", "FABIO LUIS BECKER", "FABIO LUIZ FERREIRA", "FELIPE JULIANO MARTINS",
    "FLAVIO GOMES JUNIOR", "FRANCIVAN FERREIRA DA CONCEICAO", "GABRIEL CORDEIRO DOS SANTOS",
    "GABRIEL HENRIQUE PEREIRA", "GEOVANA APARECIDA SILVEIRA", "GERSON ANTONIO PITTA PEREIRA", "GILMAR ANTES",
    "GILMARQUE LOPES DA SILVA", "GILSON OSNI FERNANDES", "GIOVANE LUIGI SESTREM POLI", "GUSTAVO VICENTE",
    "HERBERT KIENBAUM", "HIDNEL DORSAINVIL", "ITHALO DA SILVA FIGUEIREDO ARAUJO", "JAIRO JUSTEM",
    "JAISON LUIZ XAVIER", "JEAN PHILIPPE VALLON", "JEFFERSSON LINO MARINHO", "JEUNG NAM LIMA", "JHEFF BADETTE",
    "JHEFF CHARLES", "JOAO CARLOS MORO NETO", "JOAO DOMINGOS CORDEIRO DOS SANTOS", "JOAO NILTON IVACZEK",
    "JOAO VITOR MARQUES DOS SANTOS", "JONATHAN POLICARPO DE OLIVEIRA", "JOSE AMARO FERNANDES", "JOSE DONIZETE LOPES",
    "JOSE JOAO DE LUCA JUNIOR", "JOSE VALDECI DOMINGUES DE OLIVEIRA", "JOSEPH VELINOR ROMELUS",
    "JUAREZ SCHIMERSKI DOS SANTOS", "JUCELIO PEREIRA MOREIRA", "JUNIOR DA SILVA MESQUITA", "KARINA BRUSKE CLAUDINO",
    "KLEYTON DE QUADROS MATOSO", "LAERCIO SCHINDA", "LARISSA DE ARAUJO", "LEONARDO DE MARCH", "LEONIDAS DA SILVA",
    "LUCIO ANDRE DE LIMA SIQUEIRA", "LUIZ DELLIPE ALVES DE LIMA", "MACSOEL MONTEIRO DA SILVA",
    "MAICON DOUGLAS DA ROCHA LEITE", "MANOEL MESSIAS RODRIGUES", "MARCELO MAIER", "MARCIO AMBROS",
    "MARCIO JOSE DE BORBA", "MARCOS ANTONIO BORTOLIN", "MARCOS NATAL GALDINO", "MARTIN CARLOS POHL",
    "MATHEUS DE OLIVEIRA", "MICHEL DE OLIVEIRA CLAUDINO", "MOACIR GARCIA", "MOISES FARIAS FONSECA",
    "NATHALIA BUSSMANN", "NELSON OLEARI", "NELSON VIEIRA SIMOES JUNIOR", "NEUTON LONARDO SOBRINHO",
    "NILSON RODRIGUES CORREA", "NIVALDO CARVALHO", "ODAIR JOSE DE OLIVEIRA", "OSCAR ALEXANDER HERRERA CARVAJAL",
    "PATRICK BURGARDT DO NASCIMENTO", "PATRICK INNOCENT", "PAULO EDSON DA SILVA", "PAULO VANDERLEI STOCK GREIN",
    "RAFAEL GONCALVES", "RAFAEL SAMPAIO", "RAQUEL PEREIRA KRIESER", "RAUL MARCARINI", "REGINALDO FIRMINO SANTOS",
    "REINALDO ADRIANO", "RENATA CORREIA", "RENATO RICARDO RAMTHUM", "RENE BERTOTTI", "RICARDO LUIS PEREIRA",
    "RIVAIL FERNANDES", "ROBERTO BRUSKE", "RODRIGO JOSE CASTILHOS DE OLIVEIRA", "ROGERIO ATAIDE DA SILVA",
    "ROSEANE DE OLIVEIRA PEREIRA", "RUAN CARLOS MIRANDA MARCOS", "RUAN VITOR DE SOUSA MOURAO",
    "RUDINEI MARCOS POTRATZ DE MELLO", "RYAN KOSTA HEIDRICH", "SAINT LOUIGENE EMILE", "SALOMAO COSTA DA SILVA",
    "SILVANO DE SOUSA", "VALDECIR GARIPUNA", "VALDINEI DA SILVA", "VALDINEI MANOEL PEREIRA DA SILVA",
    "VALDIR JESUS DOS SANTOS", "VANDERCI MARTINS", "VANDERLEI ANTONIO VICTOR", "VILMAR DA SILVA", "VILMAR DE JESUS",
    "VINICIUS MATEUS ANTONIOLLI IVACZEK", "VITOR FERREIRA", "VITOR HUGO ALONSO", "WALDEMAR LAMPUGNANI",
    "WASHINGTON ALMEIDA FERREIRA", "WEBERSON JUNIOR ELISBAO", "WILLIAN SERGIO MOREIRA", "WILSON DE MELO FAUSTIO",
]

SENHA_TESTE = "Teste@2026"
USUARIOS_TESTE = [
    ("Almoxarifado (Teste)", "almoxarifado.teste", "almoxarifado"),
    ("Consulta (Teste)", "consulta.teste", "consulta"),
    ("Gestor de Obras (Teste)", "gestor.teste", "gestor"),
    ("Técnico de Segurança (Teste)", "seguranca.teste", "tecnico_seguranca"),
]
PERFIL_PERSONALIZADO = (
    "tecnico_seguranca",
    "Técnico de Segurança",
    "Acompanha EPIs, CAs, colaboradores, obras e relatórios, sem lançar estoque.",
    "ver_painel,ver_pedidos,ver_epis,ver_colaboradores,ver_movimentos,ver_relatorios",
)
OBRA_CONCLUIDA = ("Residencial Vale Verde", "Joinville", "Rua das Palmeiras, 300, Joinville - SC", "",
                  "Obra concluída — mantida só para histórico (demonstração).")


def popular():
    app = create_app()
    with app.app_context():
        db = get_db()
        h = hoje()

        # 1. Se estiver populando Postgres e houver usuários locais no SQLite, sincroniza os logins/senhas
        import os
        import sqlite3
        caminho_sqlite_local = os.path.join(os.path.dirname(__file__), "instance", "epi.sqlite")
        if getattr(db, "eh_postgres", False) and os.path.exists(caminho_sqlite_local):
            conn_local = sqlite3.connect(caminho_sqlite_local)
            conn_local.row_factory = sqlite3.Row
            for u in conn_local.execute("SELECT nome, login, senha_hash, perfil, ativo FROM usuarios").fetchall():
                existe = db.execute("SELECT id FROM usuarios WHERE LOWER(login) = LOWER(?)", (u["login"],)).fetchone()
                if not existe:
                    db.execute(
                        "INSERT INTO usuarios (nome, login, senha_hash, perfil, ativo) VALUES (?, ?, ?, ?, ?)",
                        (u["nome"], u["login"], u["senha_hash"], u["perfil"], u["ativo"]),
                    )
            conn_local.close()

        # Garante ao menos um usuário admin (preserva os que você já criou)
        u_admin = db.execute("SELECT id FROM usuarios WHERE perfil = 'admin' ORDER BY id LIMIT 1").fetchone()
        if not u_admin:
            cur = db.execute(
                "INSERT INTO usuarios (nome, login, senha_hash, perfil) VALUES (?, ?, ?, 'admin')",
                ("Administrador", "admin", generate_password_hash("admin1234")),
            )
            uid = cur.lastrowid
        else:
            uid = u_admin["id"]

        # Perfil personalizado + um usuário de teste por perfil (não altera usuários existentes)
        chave_p, nome_p, desc_p, perms_p = PERFIL_PERSONALIZADO
        db.execute(
            "INSERT OR IGNORE INTO perfis_acesso (chave, nome, descricao, permissoes, sistema) VALUES (?, ?, ?, ?, 0)",
            (chave_p, nome_p, desc_p, perms_p),
        )
        for nome_u, login_u, perfil_u in USUARIOS_TESTE:
            if not db.execute("SELECT 1 FROM usuarios WHERE LOWER(login) = LOWER(?)", (login_u,)).fetchone():
                db.execute(
                    "INSERT INTO usuarios (nome, login, senha_hash, perfil) VALUES (?, ?, ?, ?)",
                    (nome_u, login_u, generate_password_hash(SENHA_TESTE), perfil_u),
                )

        # Obra concluída (inativa): some do formulário, mas fica no cadastro e no histórico
        db.execute(
            "INSERT OR IGNORE INTO obras (nome, cidade, endereco, mapa, observacao, ativo) VALUES (?, ?, ?, ?, ?, 0)",
            OBRA_CONCLUIDA,
        )

        # Limpa dados anteriores de demonstração (mantendo a tabela usuarios intacta)
        if getattr(db, "eh_postgres", False):
            db.executescript("""
                TRUNCATE TABLE
                    colaborador_tamanhos,
                    colaborador_epis_solicitados,
                    colaborador_epis_padrao,
                    cargo_epis_padrao,
                    movimentacoes,
                    pedido_itens,
                    pedidos,
                    epi_apelidos,
                    fornecedores,
                    epis,
                    colaboradores
                RESTART IDENTITY CASCADE;
            """)
        else:
            db.executescript("""
                DELETE FROM colaborador_tamanhos;
                DELETE FROM colaborador_epis_solicitados;
                DELETE FROM colaborador_epis_padrao;
                DELETE FROM cargo_epis_padrao;
                DELETE FROM movimentacoes;
                DELETE FROM pedido_itens;
                DELETE FROM pedidos;
                DELETE FROM epi_apelidos;
                DELETE FROM fornecedores;
                DELETE FROM epis;
                DELETE FROM colaboradores;
                DELETE FROM sqlite_sequence WHERE name IN ('colaborador_epis_solicitados','movimentacoes','pedido_itens','pedidos','fornecedores','epis','colaboradores');
            """)

        # 2. Cadastra os 25 EPIs e as 8 Vestimentas reais do formulário UNIFORME E EPI
        catalogo_epis = [
            # --- 25 EPIs (Questão 5 do Formulário) ---
            ("LUVAS DE POLIAMIDA COM BANHO DE PU (MECÂNICA)", "Mãos", "39840", (h + timedelta(days=340)).isoformat(), "par", 15, 10, 8.90, 45),
            ("LUVAS VAQUETA PETROLEIRA", "Mãos", "36712", (h + timedelta(days=290)).isoformat(), "par", 12, 10, 19.50, 38),
            ("LUVA DE ALGODAO MESCLA 6 FIOS", "Mãos", "31205", (h + timedelta(days=410)).isoformat(), "par", 20, 7, 5.20, 60),
            ("LUVA MULTIUSO AMARELA DE LATEX", "Mãos", "34890", (h + timedelta(days=260)).isoformat(), "par", 10, 7, 7.40, 25),
            ("PROTETOR SOLAR FPS 30 120ML", "Proteção Cutânea", None, None, "un", 15, 10, 22.00, 40),
            ("REPELENTE DE INSETOS 100ML", "Proteção Cutânea", None, None, "un", 12, 10, 18.50, 30),
            ("ABAFADOR TIPO CONCHA 18DB", "Auditivo", "29110", (h + timedelta(days=480)).isoformat(), "un", 8, 12, 34.00, 20),
            ("ABAFADOR TIPO CONCHA 26DB (Máquina Aberta)", "Auditivo", "29115", (h + timedelta(days=14)).isoformat(), "un", 6, 15, 49.00, 16),  # CA vencendo em 14 dias
            ("PROTETOR AURICULAR PLUG DE SILICONE 15DB", "Auditivo", "19578", (h + timedelta(days=380)).isoformat(), "un", 25, 7, 2.90, 80),
            ("OCULOS SUMMER CINZA", "Olhos e Face", "11269", (h + timedelta(days=320)).isoformat(), "un", 15, 10, 8.20, 36),
            ("OCULOS LEOPARDO INCOLOR", "Olhos e Face", "11268", (h + timedelta(days=320)).isoformat(), "un", 15, 10, 7.80, 22),
            ("OCULOS PANDA INCOLOR (Sobrepor Óculos de Grau)", "Olhos e Face", "18420", (h + timedelta(days=250)).isoformat(), "un", 5, 12, 14.90, 12),
            ("MASCARA SEMIFACIAL JUPITER (Roçada)", "Respiratório", "32901", (h + timedelta(days=300)).isoformat(), "un", 6, 15, 42.00, 14),
            ("RESPIRADOR PFF2 COM VALVULA", "Respiratório", "38504", (h + timedelta(days=270)).isoformat(), "un", 20, 10, 4.80, 50),
            ("FILTRO COM RETENTOR PARA POEIRAS, NEVOAS E FUMOS (Roçada)", "Respiratório", "32902", (h + timedelta(days=300)).isoformat(), "par", 10, 15, 16.00, 24),
            ("SAPATÃO BIDENSIDADE C/ CADARCO (Topografia)", "Calçados", "42110", (h + timedelta(days=520)).isoformat(), "par", 6, 15, 92.00, 18),
            ("SAPATÃO CANO BAIXO C/ ELASTICO BICO PVC (Motoristas e Operadores)", "Calçados", "42890", (h + timedelta(days=500)).isoformat(), "par", 10, 15, 89.90, 28),
            ("BOTINA BIDENSIDADE C/ BICO PVC (Pedreiros, Auxiliares, Serventes e Roçada)", "Calçados", "43105", (h + timedelta(days=490)).isoformat(), "par", 15, 15, 86.00, 42),
            ("BOTA DE PVC PRETA CANO EXTRA LONGO", "Calçados", "37400", (h + timedelta(days=440)).isoformat(), "par", 8, 12, 64.00, 20),
            ("SAPATÃO BIDENSIDADE NOBUCK MARROM (Coordenadores e Encarregados - Masculino)", "Calçados", "44910", (h + timedelta(days=540)).isoformat(), "par", 5, 15, 138.00, 12),
            ("CAPACETE DE SEGURANÇA C/ SUSPENSÃO", "Cabeça", "31469", (h + timedelta(days=400)).isoformat(), "un", 12, 12, 34.50, 32),
            ("CARNEIRA SUSPENSÃO PLÁSTICA", "Cabeça", None, None, "un", 10, 10, 11.00, 24),
            ("BONE BRIM AZUL COM PROTETOR DE NUCA", "Cabeça", "39012", (h + timedelta(days=360)).isoformat(), "un", 15, 12, 24.00, 35),
            ("BONE BRIM COM PROTETOR TELA (Roçada)", "Cabeça", "39015", (h + timedelta(days=360)).isoformat(), "un", 10, 12, 28.00, 22),
            ("PROTETOR FACIAL INCOLOR ARTICULADO (Mecânica)", "Olhos e Face", "28711", (h + timedelta(days=310)).isoformat(), "un", 5, 15, 39.00, 10),

            # --- 8 Vestimentas (Questão 6 do Formulário) ---
            ("CAPA DE CHUVA TREVIRA FORRADA AMARELA", "Vestimenta", "35100", (h + timedelta(days=420)).isoformat(), "un", 12, 12, 48.00, 28),
            ("CAMISETA MANGA CURTA", "Vestimenta", None, None, "un", 25, 15, 29.00, 65),
            ("CAMISETA MANGA LONGA", "Vestimenta", None, None, "un", 25, 15, 34.00, 60),
            ("CALÇA AZUL MARINHO BRIM C/ FAIXA REFLETIVA", "Vestimenta", None, None, "un", 25, 15, 58.00, 62),
            ("BERMUDA BRIM AZUL MARINHO C/ FAIXA REFLETIVA", "Vestimenta", None, None, "un", 12, 15, 44.00, 30),
            ("AVENTAL DE RASPA SEM EMENDA 1X0,6 (Roçada e Mecânica)", "Vestimenta", "26410", (h + timedelta(days=390)).isoformat(), "un", 8, 12, 46.00, 18),
            ("PERNEIRA DE RASPA COM VELCRO (Mecânica)", "Vestimenta", "26415", (h + timedelta(days=390)).isoformat(), "par", 6, 12, 32.00, 15),
            ("PERNEIRA DE PROTECAO BIDIN/COURO COM 3 TALAS SOLDADAS (Roçada)", "Vestimenta", "33810", (h + timedelta(days=350)).isoformat(), "par", 10, 12, 38.00, 24),
            ("JARDINEIRA DE SANEAMENTO COM BOTAS (Motorista de Caminhão Pipa)", "Vestimenta", "40988", (h + timedelta(days=460)).isoformat(), "un", 4, 20, 185.00, 8),

            # --- Extras para validar alertas: CA vencido (bloqueia entrega) ---
            ("LUVA ISOLANTE DE BORRACHA CLASSE 0", "Mãos", "30117", (h - timedelta(days=10)).isoformat(), "par", 4, 20, 210.00, 6),
        ]
        # Itens que ficam só no estoque (não aparecem no formulário de solicitação)
        fora_do_formulario = {"LUVA ISOLANTE DE BORRACHA CLASSE 0", "CARNEIRA SUSPENSÃO PLÁSTICA"}

        epi_id = {}
        lote_entrada_1 = []
        lote_entrada_2 = []
        for idx, (nome, cat, ca, val, un, est_min, prazo, custo, qtd_ini) in enumerate(catalogo_epis):
            img_ilustrativa = pedidos.gerar_svg_ilustrativo_epi(nome)
            cur = db.execute(
                """INSERT INTO epis (nome, categoria, ca, validade_ca, unidade, estoque_minimo,
                                     prazo_reposicao_dias, imagem_url, exibir_no_forms)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (nome, cat, ca, val, un, est_min, prazo, img_ilustrativa, 0 if nome in fora_do_formulario else 1),
            )
            eid = cur.lastrowid
            epi_id[nome] = eid
            item_ent = {"epi_id": eid, "quantidade": qtd_ini, "custo_unitario": custo}
            if idx < 17:
                lote_entrada_1.append(item_ent)
            else:
                lote_entrada_2.append(item_ent)

        # Estoque por tamanho individual: mesmo item em P, M e G (filtro de tamanho na aba EPIs)
        lote_tamanhos = []
        for tam, qtd_t in (("P", 10), ("M", 24), ("G", 3)):   # G abaixo do mínimo -> alerta de reposição
            cur = db.execute(
                """INSERT INTO epis (nome, tamanho, categoria, ca, validade_ca, unidade, estoque_minimo,
                                     prazo_reposicao_dias, imagem_url, exibir_no_forms)
                   VALUES (?, ?, 'Mãos', '37988', ?, 'par', 6, 10, ?, 0)""",
                ("LUVA NITRÍLICA VERDE", tam, (h + timedelta(days=365)).isoformat(),
                 pedidos.gerar_svg_ilustrativo_epi("LUVA NITRÍLICA VERDE")),
            )
            epi_id[f"LUVA NITRÍLICA VERDE {tam}"] = cur.lastrowid
            lote_tamanhos.append({"epi_id": cur.lastrowid, "quantidade": qtd_t, "custo_unitario": 6.40})

        # 3. Ensina apelidos úteis caso alguém preencha apenas o tamanho na coluna Sapatão/Camiseta/Calça
        apelidos = [
            *(("Camiseta", t.lower(), "CAMISETA MANGA CURTA") for t in pedidos.TAMANHOS_UNIFORME),
            *(("Calça", t.lower(), "CALÇA AZUL MARINHO BRIM C/ FAIXA REFLETIVA") for t in pedidos.TAMANHOS_UNIFORME),
            ("Sapatão", "40", "BOTINA BIDENSIDADE C/ BICO PVC (Pedreiros, Auxiliares, Serventes e Roçada)"),
            ("Sapatão", "41", "BOTINA BIDENSIDADE C/ BICO PVC (Pedreiros, Auxiliares, Serventes e Roçada)"),
            ("Sapatão", "42", "BOTINA BIDENSIDADE C/ BICO PVC (Pedreiros, Auxiliares, Serventes e Roçada)"),
        ]
        for col, txt, nome_epi in apelidos:
            db.execute(
                "INSERT INTO epi_apelidos (chave, epi_id) VALUES (?, ?)",
                (pedidos.chave_apelido(col, txt), epi_id[nome_epi]),
            )

        # 4. Cadastra os colaboradores reais do formulário + Ruan Alves
        locais = sorted({o["cidade"] for o in pedidos.OBRAS_FORMULARIO})   # setor = cidade das obras
        funcoes_ciclo = [
            "Pedreiro", "Servente de Obras", "Operador de Roçadeira", "Motorista",
            "Operador de Máquinas", "Mecânico", "Topógrafo", "Encarregado de Obras",
        ]
        colab_id = {}

        # Ruan Alves como primeiro colaborador para facilitar testes
        cur = db.execute(
            "INSERT INTO colaboradores (nome, matricula, setor, funcao) VALUES (?, ?, ?, ?)",
            ("Ruan Alves", "1000", "Araquari", "Gestão / TI"),
        )
        colab_id["Ruan Alves"] = cur.lastrowid

        for idx, nome in enumerate(COLABORADORES_FORMULARIO, start=1001):
            setor = locais[idx % len(locais)]
            funcao = funcoes_ciclo[idx % len(funcoes_ciclo)]
            cur = db.execute(
                "INSERT INTO colaboradores (nome, matricula, setor, funcao) VALUES (?, ?, ?, ?)",
                (nome, str(idx), setor, funcao),
            )
            colab_id[nome] = cur.lastrowid

        # Colaborador desligado (inativo): não aparece no formulário nem em novas entregas
        db.execute("UPDATE colaboradores SET ativo = 0 WHERE id = ?", (colab_id["WALDEMAR LAMPUGNANI"],))
        # Colaboradora sem cargo definido: usa o padrão individual da ficha (cadastrado mais abaixo)
        db.execute("UPDATE colaboradores SET funcao = NULL WHERE id = ?", (colab_id["GEOVANA APARECIDA SILVEIRA"],))

        # 5. Cadastra fornecedores e registra entradas de estoque
        fornecedores_demo = [
            (
                "ProtegeSul EPIs Ltda",
                "ProtegeSul Comércio de Equipamentos de Proteção Ltda",
                "14.289.410/0001-52",
                "256.819.044",
                "Marcos Andrade",
                "(47) 99142-3310",
                "comercial@protegesul.com.br",
                "Rua Dona Francisca, 4120, Distrito Industrial",
                "Joinville / SC",
                "89219-600",
                7,
                "Boleto 28/56 dias",
                "Luvas, Óculos, Auditivo, Respiratório",
                "Entrega própria sem custo de frete para Joinville e Araquari.",
            ),
            (
                "Casa do EPI Joinville",
                "Casa do EPI e Suprimentos Industriais S.A.",
                "08.741.920/0001-18",
                "254.110.982",
                "Fernanda Koster",
                "(47) 3433-8900",
                "pedidos@casadoepijlle.com.br",
                "Av. Getúlio Vargas, 980, Anita Garibaldi",
                "Joinville / SC",
                "89202-002",
                10,
                "Boleto 30 dias / PIX",
                "Calçados, Capacetes, Vestimentas, Impermeáveis",
                "Fornecedor principal de calçados de segurança e capas de chuva.",
            ),
            (
                "Confecções Catarinense Uniformes",
                "Indústria Têxtil e Confecções Catarinense Ltda",
                "21.605.330/0001-80",
                "258.442.190",
                "Roberto Schulz",
                "(47) 99610-7740",
                "vendas@confeccoescatarinense.com.br",
                "Rua Blumenau, 1540, América",
                "Joinville / SC",
                "89204-250",
                15,
                "50% pedido / 50% entrega",
                "Camisetas Manga Curta/Longa, Calças Brim c/ Refletivo",
                "Personalização com silk e faixa refletiva padrão das obras.",
            ),
            (
                "Segurança Total Distribuidora",
                "Segurança Total Distribuidora de EPIs Eireli",
                "33.118.702/0001-09",
                None,
                "Paula Mendes",
                "(41) 3022-1180",
                "paula@segurancatotal.com.br",
                "Rua XV de Novembro, 210, Centro",
                "Curitiba / PR",
                "80020-310",
                20,
                "Antecipado",
                "Luvas isolantes, Cintos de segurança",
                "Fornecedor descontinuado (prazo longo) — mantido inativo para histórico.",
            ),
        ]
        for fn in fornecedores_demo:
            db.execute(
                """INSERT INTO fornecedores (
                       nome, razao_social, cnpj, inscricao_estadual, contato_nome,
                       telefone, email, endereco, cidade_uf, cep, prazo_entrega_dias,
                       condicao_pagamento, categorias_fornecidas, observacao
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                fn,
            )
        db.execute("UPDATE fornecedores SET ativo = 0 WHERE nome = 'Segurança Total Distribuidora'")

        estoque.registrar(
            db, "entrada", lote_entrada_1,
            h - timedelta(days=58), uid,
            fornecedor="ProtegeSul EPIs Ltda", nota_fiscal="NF-4821",
        )
        estoque.registrar(
            db, "entrada", lote_entrada_2,
            h - timedelta(days=35), uid,
            fornecedor="Casa do EPI Joinville", nota_fiscal="NF-5190",
        )
        estoque.registrar(
            db, "entrada", lote_tamanhos,
            h - timedelta(days=30), uid,
            fornecedor="ProtegeSul EPIs Ltda", nota_fiscal="NF-5233",
        )
        # Reposição de uniformes pela confecção
        estoque.registrar(
            db, "entrada",
            [{"epi_id": epi_id["CAMISETA MANGA CURTA"], "quantidade": 30, "custo_unitario": 27.50},
             {"epi_id": epi_id["CALÇA AZUL MARINHO BRIM C/ FAIXA REFLETIVA"], "quantidade": 20, "custo_unitario": 55.00}],
            h - timedelta(days=20), uid,
            fornecedor="Confecções Catarinense Uniformes", nota_fiscal="NF-0781",
        )
        # Entrada lançada em duplicidade e estornada (fica no histórico como cancelada)
        estoque.registrar(
            db, "entrada",
            [{"epi_id": epi_id["RESPIRADOR PFF2 COM VALVULA"], "quantidade": 50, "custo_unitario": 4.80}],
            h - timedelta(days=18), uid,
            fornecedor="ProtegeSul EPIs Ltda", nota_fiscal="NF-4821",
        )
        mov_dup = db.execute("SELECT MAX(id) AS id FROM movimentacoes").fetchone()["id"]
        estoque.cancelar(db, mov_dup, "Lançamento em duplicidade da NF-4821", uid)

        # 6. Saídas históricas (entregas para alimentar consumo médio, alertas de reposição e relatórios)
        historico_saidas = [
            (h - timedelta(days=45), "ABRAHAM JOSUE BARRETO CUELLO", "Kasulke", [
                ("LUVA DE ALGODAO MESCLA 6 FIOS", 12),
                ("OCULOS LEOPARDO INCOLOR", 8),
                ("PROTETOR AURICULAR PLUG DE SILICONE 15DB", 15),
                ("BOTINA BIDENSIDADE C/ BICO PVC (Pedreiros, Auxiliares, Serventes e Roçada)", 6),
                ("CAMISETA MANGA CURTA", 10),
                ("CALÇA AZUL MARINHO BRIM C/ FAIXA REFLETIVA", 8),
            ]),
            (h - timedelta(days=28), "ANDERSON SCHROEDER", "Residencial Vale Verde", [
                ("LUVAS DE POLIAMIDA COM BANHO DE PU (MECÂNICA)", 10),
                ("OCULOS LEOPARDO INCOLOR", 9),  # deixa Óculos Leopardo com saldo 5 (abaixo do mínimo 15 -> alerta de Repor!)
                ("PROTETOR SOLAR FPS 30 120ML", 10),
                ("BONE BRIM AZUL COM PROTETOR DE NUCA", 8),
                ("CAMISETA MANGA LONGA", 10),
                ("CALÇA AZUL MARINHO BRIM C/ FAIXA REFLETIVA", 8),
            ]),
            (h - timedelta(days=14), "BRUNO MULLER DA SILVA", "Porto do Sol", [
                ("LUVA DE ALGODAO MESCLA 6 FIOS", 10),
                ("RESPIRADOR PFF2 COM VALVULA", 12),
                ("BOTINA BIDENSIDADE C/ BICO PVC (Pedreiros, Auxiliares, Serventes e Roçada)", 5),
                ("CAPACETE DE SEGURANÇA C/ SUSPENSÃO", 6),
                ("CAPA DE CHUVA TREVIRA FORRADA AMARELA", 6),
            ]),
            (h - timedelta(days=5), "CARLOS ARTUR FRANKE", "Villa Dona Francisca", [
                ("LUVAS VAQUETA PETROLEIRA", 8),
                ("BONE BRIM COM PROTETOR TELA (Roçada)", 6),
                ("PERNEIRA DE PROTECAO BIDIN/COURO COM 3 TALAS SOLDADAS (Roçada)", 5),
                ("MASCARA SEMIFACIAL JUPITER (Roçada)", 4),
            ]),
        ]
        for data_s, nome_c, local, lista_it in historico_saidas:
            db.execute("BEGIN IMMEDIATE")
            estoque.registrar_na_transacao(
                db, "saida",
                [{"epi_id": epi_id[n], "quantidade": q} for n, q in lista_it],
                data_s, uid, colaborador_id=colab_id[nome_c],
                motivo="Desgaste", local_destino=local,
            )
            db.execute("COMMIT")

        # Baixas (perda/avaria) e ajustes de inventário
        estoque.registrar(db, "baixa", [{"epi_id": epi_id["CAPACETE DE SEGURANÇA C/ SUSPENSÃO"], "quantidade": 2}],
                          h - timedelta(days=12), uid, motivo="Avaria",
                          observacao="Cascos trincados após queda de material na obra Kasulke")
        estoque.registrar(db, "baixa", [{"epi_id": epi_id["CAPA DE CHUVA TREVIRA FORRADA AMARELA"], "quantidade": 1}],
                          h - timedelta(days=9), uid, motivo="Perda",
                          observacao="Extraviada no transporte para Porto do Sol")
        estoque.registrar(db, "ajuste_mais", [{"epi_id": epi_id["PROTETOR AURICULAR PLUG DE SILICONE 15DB"], "quantidade": 5}],
                          h - timedelta(days=7), uid, motivo="Inventário",
                          observacao="Contagem física encontrou 5 unidades a mais na prateleira B")
        estoque.registrar(db, "ajuste_menos", [{"epi_id": epi_id["LUVA MULTIUSO AMARELA DE LATEX"], "quantidade": 3}],
                          h - timedelta(days=7), uid, motivo="Inventário",
                          observacao="Diferença na contagem física mensal")

        # 7. PEDIDOS DO FORMULÁRIO EM CADA ETAPA DO FLUXO

        # Pedido #1: EM SEPARAÇÃO (Ruan Alves / Araquari)
        pid_ruan, _ = pedidos.criar_pedido(db, {
            "id": "forms-201",
            "nome": "Ruan Alves",
            "local": "Recanto da Fazenda",
            "motivo": "Desgaste",
            "epi": [
                "OCULOS SUMMER CINZA",
                "PROTETOR AURICULAR PLUG DE SILICONE 15DB",
                "SAPATÃO BIDENSIDADE NOBUCK MARROM (Coordenadores e Encarregados - Masculino)",
            ],
            "vestimento": [
                "CAMISETA MANGA CURTA",
                "CALÇA AZUL MARINHO BRIM C/ FAIXA REFLETIVA",
            ],
            "sapatao": "41",
            "camiseta": "M",
            "calca": "G",
            "observacao": "Visita técnica nas obras Recanto da Fazenda e Porto do Sol",
        })
        pedidos.iniciar(db, pid_ruan, uid)
        itens_ruan = db.execute("SELECT id FROM pedido_itens WHERE pedido_id = ? ORDER BY id", (pid_ruan,)).fetchall()
        for r in itens_ruan[:2]:
            pedidos.marcar_separado(db, pid_ruan, r["id"], True)

        # Pedido #2: PRONTO PARA RETIRADA (já separado e com baixa feita)
        pid_pronto, _ = pedidos.criar_pedido(db, {
            "id": "forms-202",
            "nome": "ABRAHAM JOSUE BARRETO CUELLO",
            "local": "Kasulke",
            "motivo": "Desgaste",
            "epi": [
                "LUVA DE ALGODAO MESCLA 6 FIOS",
                "OCULOS LEOPARDO INCOLOR",
                "BOTINA BIDENSIDADE C/ BICO PVC (Pedreiros, Auxiliares, Serventes e Roçada)",
            ],
            "vestimento": [
                "CAMISETA MANGA CURTA",
                "CALÇA AZUL MARINHO BRIM C/ FAIXA REFLETIVA",
            ],
            "sapatao": "41",
            "camiseta": "G",
            "calca": "G",
            "observacao": "Encarregado vai passar à tarde para retirar",
        })
        pedidos.iniciar(db, pid_pronto, uid)
        for r in db.execute("SELECT id FROM pedido_itens WHERE pedido_id = ?", (pid_pronto,)):
            pedidos.marcar_separado(db, pid_pronto, r["id"], True)
        pedidos.finalizar(db, pid_pronto, uid)

        # Pedido #3: JÁ RETIRADO (histórico na aba Retirados)
        pid_retirado, _ = pedidos.criar_pedido(db, {
            "id": "forms-203",
            "nome": "ANDERSON SCHROEDER",
            "local": "Residencial Vale Verde",
            "motivo": "Desgaste",
            "epi": [
                "LUVAS DE POLIAMIDA COM BANHO DE PU (MECÂNICA)",
                "PROTETOR SOLAR FPS 30 120ML",
                "SAPATÃO CANO BAIXO C/ ELASTICO BICO PVC (Motoristas e Operadores)",
            ],
            "vestimento": [
                "CAMISETA MANGA LONGA",
            ],
            "sapatao": "42",
            "camiseta": "GG",
            "observacao": "Enviar pelo motorista da rota de Joinville",
        })
        pedidos.iniciar(db, pid_retirado, uid)
        for r in db.execute("SELECT id FROM pedido_itens WHERE pedido_id = ?", (pid_retirado,)):
            pedidos.marcar_separado(db, pid_retirado, r["id"], True)
        pedidos.finalizar(db, pid_retirado, uid)
        pedidos.retirar(
            db, pid_retirado,
            nome="CARLOS ARTUR FRANKE",
            descricao="Encarregado de obras — levou para a frente de serviço do Residencial Vale Verde",
            usuario_id=uid,
        )

        # Pedido #4: PENDENTE (deixado para mais tarde)
        pid_pendente, _ = pedidos.criar_pedido(db, {
            "id": "forms-204",
            "nome": "BRUNO MULLER DA SILVA",
            "local": "Porto do Sol",
            "motivo": "Perda",
            "epi": [
                "CAPACETE DE SEGURANÇA C/ SUSPENSÃO",
                "BONE BRIM AZUL COM PROTETOR DE NUCA",
            ],
            "vestimento": [
                "CAPA DE CHUVA TREVIRA FORRADA AMARELA",
            ],
            "observacao": "Retirada prevista para sexta-feira",
        })
        pedidos.adiar(db, pid_pendente)

        # Pedido #5 e #6: NOVOS PEDIDOS (aparecem no alerta vermelho de novos pedidos)
        pedidos.criar_pedido(db, {
            "id": "forms-205",
            "nome": "CAIO FRANCISCO DENK",
            "local": "Villa Dona Francisca",
            "motivo": "Desgaste",
            "epi": [
                "LUVAS VAQUETA PETROLEIRA",
                "MASCARA SEMIFACIAL JUPITER (Roçada)",
                "BONE BRIM COM PROTETOR TELA (Roçada)",
                "BOTINA BIDENSIDADE C/ BICO PVC (Pedreiros, Auxiliares, Serventes e Roçada)",
            ],
            "vestimento": [
                "PERNEIRA DE PROTECAO BIDIN/COURO COM 3 TALAS SOLDADAS (Roçada)",
                "CAMISETA MANGA LONGA",
            ],
            "sapatao": "40",
            "camiseta": "G",
            "observacao": "Equipe de roçada da Villa Dona Francisca",
        })
        pedidos.criar_pedido(db, {
            "id": "forms-206",
            "nome": "CHARLES KOSTA HEIDRICH",
            "local": "Amizade",
            "motivo": "Mudança de Função",
            "epi": [
                "LUVAS DE POLIAMIDA COM BANHO DE PU (MECÂNICA)",
                "PROTETOR FACIAL INCOLOR ARTICULADO (Mecânica)",
                "SAPATÃO CANO BAIXO C/ ELASTICO BICO PVC (Motoristas e Operadores)",
            ],
            "vestimento": [
                "AVENTAL DE RASPA SEM EMENDA 1X0,6 (Roçada e Mecânica)",
                "CALÇA AZUL MARINHO BRIM C/ FAIXA REFLETIVA",
            ],
            "sapatao": "42",
            "calca": "GG",
            "observacao": "Passou a atuar no apoio de manutenção mecânica na obra Amizade",
        })

        # Pedido #7: CANCELADO (duplicidade)
        pid_cancelado, _ = pedidos.criar_pedido(db, {
            "id": "forms-207",
            "nome": "VILMAR DA SILVA",
            "local": "Revoar",
            "motivo": "Desgaste",
            "epi": ["OCULOS SUMMER CINZA", "PROTETOR SOLAR FPS 30 120ML"],
            "observacao": "Enviado duas vezes pelo celular",
        })
        pedidos.cancelar(db, pid_cancelado, "Pedido em duplicidade — o colaborador enviou o formulário duas vezes",
                         uid, pode_estornar=True)

        # Abafador 26DB zerado (lote com defeito devolvido): o pedido #8 sai com ressalva e gera pendência
        id_abafador = epi_id["ABAFADOR TIPO CONCHA 26DB (Máquina Aberta)"]
        estoque.registrar(db, "baixa", [{"epi_id": id_abafador, "quantidade": estoque.saldo(db, id_abafador)}],
                          h - timedelta(days=3), uid, motivo="Avaria",
                          observacao="Lote com defeito devolvido à Casa do EPI")

        # Pedido #8: FINALIZADO COM RESSALVA (item sem saldo ficou de fora e virou pendência “aguardando estoque”)
        pid_ressalva, _ = pedidos.criar_pedido(db, {
            "id": "forms-208",
            "nome": "JOSE AMARO FERNANDES",
            "local": "Condomínio Lucena",
            "motivo": "Desgaste",
            "epi": ["LUVA DE ALGODAO MESCLA 6 FIOS", "ABAFADOR TIPO CONCHA 26DB (Máquina Aberta)"],
            "vestimento": ["CAMISETA MANGA LONGA"],
            "camiseta": "G",
            "observacao": "Operador da escavadeira",
        })
        pedidos.iniciar(db, pid_ressalva, uid)
        for r in db.execute("SELECT id, texto_original FROM pedido_itens WHERE pedido_id = ?", (pid_ressalva,)).fetchall():
            if "ABAFADOR" not in r["texto_original"].upper():
                pedidos.marcar_separado(db, pid_ressalva, r["id"], True)
        pedidos.finalizar(db, pid_ressalva, uid, com_ressalva=True,
                          ressalva_obs="Abafador 26DB em falta — aguardando reposição da Casa do EPI (previsão 5 dias)")

        # Pedido #9: NOVO com item não identificado (texto livre que não existe no estoque -> “analisar”)
        pedidos.criar_pedido(db, {
            "id": "forms-209",
            "nome": "MARCOS NATAL GALDINO",
            "local": "Acordes",
            "motivo": "Mudança de Função",
            "epi": ["LUVA DE ALGODAO MESCLA 6 FIOS", "CINTO DE SEGURANÇA PARAQUEDISTA"],
            "observacao": "Vai trabalhar em altura na montagem do telhado",
        })

        # Pedido #10: NOVO feito pelo GESTOR, com itens fora da função do colaborador (Pedreiro em teste na roçada)
        pedreiro = db.execute(
            "SELECT nome FROM colaboradores WHERE funcao = 'Pedreiro' AND ativo = 1 ORDER BY nome LIMIT 1"
        ).fetchone()["nome"]
        fora = ["MASCARA SEMIFACIAL JUPITER (Roçada)", "PERNEIRA DE PROTECAO BIDIN/COURO COM 3 TALAS SOLDADAS (Roçada)"]
        pedidos.criar_pedido(db, {
            "nome": pedreiro,
            "local": "Terra de Açores",
            "motivo": "Mudança de Função",
            "epi": ["LUVA DE ALGODAO MESCLA 6 FIOS", fora[0]],
            "vestimento": [fora[1]],
            "observacao": (
                "[Solicitado pelo gestor: Gestor de Obras (Teste)] "
                "[Justificativa: período de teste de 30 dias na equipe de roçada] "
                "| Começa na segunda-feira"
            ),
        })

        # Datas escalonadas (chegada, separação, pronto, retirada) para o SLA mostrar todas as situações
        def quando(dias, hora):
            return f"{(h - timedelta(days=dias)).isoformat()} {hora}"

        for pid_d, dias, hora in ((pid_retirado, 20, "07:42:10"), (pid_cancelado, 9, "16:05:33"),
                                  (pid_pronto, 4, "08:15:02"), (pid_ressalva, 2, "10:31:47"),
                                  (pid_pendente, 0, "06:20:09"), (pid_ruan, 0, "07:03:55")):
            db.execute("UPDATE pedidos SET recebido_em = ? WHERE id = ?", (quando(dias, hora), pid_d))
        # Retirado: pronto em ~6 h (SLA cumprido) e retirado no dia seguinte
        db.execute("UPDATE pedidos SET iniciado_em = ?, finalizado_em = ?, retirado_em = ? WHERE id = ?",
                   (quando(20, "08:10:00"), quando(20, "13:20:00"), quando(19, "16:00:00"), pid_retirado))
        # Pronto: atendido no prazo, mas ninguém retirou há 4 dias (retirada atrasada)
        db.execute("UPDATE pedidos SET iniciado_em = ?, finalizado_em = ? WHERE id = ?",
                   (quando(4, "09:00:00"), quando(4, "15:40:00"), pid_pronto))
        # Com ressalva: ficou pronto em ~28 h (fora do prazo); a pendência gerada aguarda estoque
        db.execute("UPDATE pedidos SET iniciado_em = ?, finalizado_em = ? WHERE id = ?",
                   (quando(2, "11:00:00"), quando(1, "14:45:00"), pid_ressalva))
        db.execute("UPDATE pedidos SET recebido_em = ? WHERE pedido_origem_id = ?", (quando(1, "14:45:00"), pid_ressalva))
        db.execute("UPDATE pedidos SET iniciado_em = ? WHERE id = ?", (quando(0, "07:30:00"), pid_ruan))
        # Novos: um atrasado (30 h), um em risco (21 h) e os demais recentes (no prazo)
        db.execute("UPDATE pedidos SET recebido_em = datetime('now', 'localtime', '-30 hours') WHERE origem_id = 'forms-205'")
        db.execute("UPDATE pedidos SET recebido_em = datetime('now', 'localtime', '-21 hours') WHERE origem_id = 'forms-206'")

        # 8. Padronização de EPIs por Cargo + Equipamentos solicitados fora do padrão do cargo
        padroes_por_cargo = {
            "Gestão / TI": [
                "OCULOS SUMMER CINZA",
                "PROTETOR AURICULAR PLUG DE SILICONE 15DB",
                "SAPATÃO BIDENSIDADE NOBUCK MARROM (Coordenadores e Encarregados - Masculino)",
                "CAMISETA MANGA CURTA",
                "CALÇA AZUL MARINHO BRIM C/ FAIXA REFLETIVA",
            ],
            "Encarregado de Obras": [
                "OCULOS SUMMER CINZA",
                "PROTETOR AURICULAR PLUG DE SILICONE 15DB",
                "SAPATÃO BIDENSIDADE NOBUCK MARROM (Coordenadores e Encarregados - Masculino)",
                "CAPACETE DE SEGURANÇA C/ SUSPENSÃO",
                "CAMISETA MANGA CURTA",
                "CALÇA AZUL MARINHO BRIM C/ FAIXA REFLETIVA",
            ],
            "Pedreiro": [
                "LUVA DE ALGODAO MESCLA 6 FIOS",
                "OCULOS LEOPARDO INCOLOR",
                "PROTETOR AURICULAR PLUG DE SILICONE 15DB",
                "BOTINA BIDENSIDADE C/ BICO PVC (Pedreiros, Auxiliares, Serventes e Roçada)",
                "CAPACETE DE SEGURANÇA C/ SUSPENSÃO",
                "BONE BRIM AZUL COM PROTETOR DE NUCA",
                "CAMISETA MANGA CURTA",
                "CALÇA AZUL MARINHO BRIM C/ FAIXA REFLETIVA",
                "BERMUDA BRIM AZUL MARINHO C/ FAIXA REFLETIVA",
            ],
            "Servente de Obras": [
                "LUVA DE ALGODAO MESCLA 6 FIOS",
                "OCULOS LEOPARDO INCOLOR",
                "PROTETOR AURICULAR PLUG DE SILICONE 15DB",
                "BOTINA BIDENSIDADE C/ BICO PVC (Pedreiros, Auxiliares, Serventes e Roçada)",
                "CAPACETE DE SEGURANÇA C/ SUSPENSÃO",
                "BONE BRIM AZUL COM PROTETOR DE NUCA",
                "CAMISETA MANGA CURTA",
                "CALÇA AZUL MARINHO BRIM C/ FAIXA REFLETIVA",
                "BERMUDA BRIM AZUL MARINHO C/ FAIXA REFLETIVA",
            ],
            "Operador de Roçadeira": [
                "LUVAS VAQUETA PETROLEIRA",
                "PROTETOR SOLAR FPS 30 120ML",
                "REPELENTE DE INSETOS 100ML",
                "ABAFADOR TIPO CONCHA 18DB",
                "OCULOS SUMMER CINZA",
                "MASCARA SEMIFACIAL JUPITER (Roçada)",
                "FILTRO COM RETENTOR PARA POEIRAS, NEVOAS E FUMOS (Roçada)",
                "BOTINA BIDENSIDADE C/ BICO PVC (Pedreiros, Auxiliares, Serventes e Roçada)",
                "BONE BRIM COM PROTETOR TELA (Roçada)",
                "AVENTAL DE RASPA SEM EMENDA 1X0,6 (Roçada e Mecânica)",
                "PERNEIRA DE PROTECAO BIDIN/COURO COM 3 TALAS SOLDADAS (Roçada)",
                "CAMISETA MANGA LONGA",
                "CALÇA AZUL MARINHO BRIM C/ FAIXA REFLETIVA",
            ],
            "Motorista": [
                "LUVA DE ALGODAO MESCLA 6 FIOS",
                "OCULOS SUMMER CINZA",
                "SAPATÃO CANO BAIXO C/ ELASTICO BICO PVC (Motoristas e Operadores)",
                "CAPA DE CHUVA TREVIRA FORRADA AMARELA",
                "CAMISETA MANGA CURTA",
                "CALÇA AZUL MARINHO BRIM C/ FAIXA REFLETIVA",
            ],
            "Operador de Máquinas": [
                "LUVA DE ALGODAO MESCLA 6 FIOS",
                "ABAFADOR TIPO CONCHA 26DB (Máquina Aberta)",
                "OCULOS SUMMER CINZA",
                "SAPATÃO CANO BAIXO C/ ELASTICO BICO PVC (Motoristas e Operadores)",
                "BONE BRIM AZUL COM PROTETOR DE NUCA",
                "CAMISETA MANGA LONGA",
                "CALÇA AZUL MARINHO BRIM C/ FAIXA REFLETIVA",
            ],
            "Mecânico": [
                "LUVAS DE POLIAMIDA COM BANHO DE PU (MECÂNICA)",
                "PROTETOR AURICULAR PLUG DE SILICONE 15DB",
                "OCULOS LEOPARDO INCOLOR",
                "PROTETOR FACIAL INCOLOR ARTICULADO (Mecânica)",
                "SAPATÃO CANO BAIXO C/ ELASTICO BICO PVC (Motoristas e Operadores)",
                "AVENTAL DE RASPA SEM EMENDA 1X0,6 (Roçada e Mecânica)",
                "PERNEIRA DE RASPA COM VELCRO (Mecânica)",
                "CAMISETA MANGA CURTA",
                "CALÇA AZUL MARINHO BRIM C/ FAIXA REFLETIVA",
            ],
            "Topógrafo": [
                "PROTETOR SOLAR FPS 30 120ML",
                "REPELENTE DE INSETOS 100ML",
                "OCULOS SUMMER CINZA",
                "SAPATÃO BIDENSIDADE C/ CADARCO (Topografia)",
                "BONE BRIM AZUL COM PROTETOR DE NUCA",
                "CAMISETA MANGA LONGA",
                "CALÇA AZUL MARINHO BRIM C/ FAIXA REFLETIVA",
            ],
        }
        for cargo_nome, lista_nomes_epi in padroes_por_cargo.items():
            for nome_e in lista_nomes_epi:
                db.execute(
                    "INSERT INTO cargo_epis_padrao (cargo, epi_id) VALUES (?, ?)",
                    (cargo_nome, epi_id[nome_e]),
                )

        # Padrão individual para a colaboradora sem cargo definido
        for nome_e in ("LUVA DE ALGODAO MESCLA 6 FIOS", "OCULOS LEOPARDO INCOLOR", "CAMISETA MANGA CURTA"):
            db.execute(
                "INSERT INTO colaborador_epis_padrao (colaborador_id, epi_id) VALUES (?, ?)",
                (colab_id["GEOVANA APARECIDA SILVEIRA"], epi_id[nome_e]),
            )

        # Define numeração de calçados e tamanho de uniformes nos perfis dos colaboradores para relatórios de estocagem
        numeros_ciclo = ["39", "40", "41", "42", "43", "44"]
        tams_unif_ciclo = ["M", "G", "GG", "G1", "G2"]
        for idx_c, (nome_c, cid) in enumerate(colab_id.items()):
            num_c = "41" if nome_c == "Ruan Alves" else numeros_ciclo[idx_c % len(numeros_ciclo)]
            tam_cam = "M" if nome_c == "Ruan Alves" else tams_unif_ciclo[idx_c % len(tams_unif_ciclo)]
            tam_cal = "G" if nome_c == "Ruan Alves" else tams_unif_ciclo[(idx_c + 1) % len(tams_unif_ciclo)]
            for ch_t, val_t in (
                ("tipo:sapatao", num_c),
                ("tipo:botina", num_c),
                ("tipo:bota", num_c),
                ("tipo:camiseta", tam_cam),
                ("tipo:calca", tam_cal),
            ):
                db.execute(
                    "INSERT INTO colaborador_tamanhos (colaborador_id, chave, tamanho) VALUES (?, ?, ?)",
                    (cid, ch_t, val_t),
                )

        # Exemplos de solicitações fora do padrão com observação (teste em outro cargo / necessidade específica)
        db.execute(
            """INSERT INTO colaborador_epis_solicitados
               (colaborador_id, epi_id, quantidade, data, observacao, usuario_id)
               VALUES (?, ?, 1, ?, ?, ?)""",
            (
                colab_id["ABRAHAM JOSUE BARRETO CUELLO"],
                epi_id["MASCARA SEMIFACIAL JUPITER (Roçada)"],
                (h - timedelta(days=4)).isoformat(),
                "Colaborador em período de teste na equipe de Roçada visando possível promoção de função.",
                uid,
            ),
        )
        db.execute(
            """INSERT INTO colaborador_epis_solicitados
               (colaborador_id, epi_id, quantidade, data, observacao, usuario_id)
               VALUES (?, ?, 1, ?, ?, ?)""",
            (
                colab_id["Ruan Alves"],
                epi_id["CAPACETE DE SEGURANÇA C/ SUSPENSÃO"],
                (h - timedelta(days=3)).isoformat(),
                "Solicitado para acompanhamento de vistoria técnica em frente de obra.",
                uid,
            ),
        )
        db.commit()

        resumo = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("epis", "colaboradores", "fornecedores", "obras", "movimentacoes", "pedidos", "usuarios")}
        print("Base de demonstração criada com sucesso:", ", ".join(f"{k}={v}" for k, v in resumo.items()))
        print("Logins de teste: veja o topo do arquivo popular_teste.py")


if __name__ == "__main__":
    import os
    import sys
    if len(sys.argv) > 1 and sys.argv[1].strip():
        os.environ["DATABASE_URL"] = sys.argv[1].strip()
    popular()
