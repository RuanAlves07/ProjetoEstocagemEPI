"""Popula o banco (instance/epi.sqlite) com os itens e colaboradores reais do formulário UNIFORME E EPI.

Preserva os usuários já existentes e cadastra os 33 itens reais (25 EPIs + 8 Vestimentas),
os colaboradores reais das obras, entradas/saídas de estoque e pedidos em todas as etapas.

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


def popular():
    app = create_app()
    with app.app_context():
        db = get_db()
        h = hoje()

        # 1. Garante ao menos um usuário admin (preserva os que você já criou)
        u_admin = db.execute("SELECT id FROM usuarios WHERE perfil = 'admin' ORDER BY id LIMIT 1").fetchone()
        if not u_admin:
            cur = db.execute(
                "INSERT INTO usuarios (nome, login, senha_hash, perfil) VALUES (?, ?, ?, 'admin')",
                ("Administrador", "admin", generate_password_hash("admin1234")),
            )
            uid = cur.lastrowid
        else:
            uid = u_admin["id"]

        # Limpa dados anteriores de demonstração (mantendo a tabela usuarios intacta)
        db.executescript("""
            DELETE FROM colaborador_epis_solicitados;
            DELETE FROM colaborador_epis_padrao;
            DELETE FROM cargo_epis_padrao;
            DELETE FROM movimentacoes;
            DELETE FROM pedido_itens;
            DELETE FROM pedidos;
            DELETE FROM epi_apelidos;
            DELETE FROM epis;
            DELETE FROM colaboradores;
            DELETE FROM sqlite_sequence WHERE name IN ('colaborador_epis_solicitados','movimentacoes','pedido_itens','pedidos','epis','colaboradores');
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
            ("AVENTAL DE RASPA SEM EMENDA 1X0,6 (Roçada e Mecânica)", "Vestimenta", "26410", (h + timedelta(days=390)).isoformat(), "un", 8, 12, 46.00, 18),
            ("PERNEIRA DE RASPA COM VELCRO (Mecânica)", "Vestimenta", "26415", (h + timedelta(days=390)).isoformat(), "par", 6, 12, 32.00, 15),
            ("PERNEIRA DE PROTECAO BIDIN/COURO COM 3 TALAS SOLDADAS (Roçada)", "Vestimenta", "33810", (h + timedelta(days=350)).isoformat(), "par", 10, 12, 38.00, 24),
            ("JARDINEIRA DE SANEAMENTO COM BOTAS (Motorista de Caminhão Pipa)", "Vestimenta", "40988", (h + timedelta(days=460)).isoformat(), "un", 4, 20, 185.00, 8),
        ]

        epi_id = {}
        lote_entrada_1 = []
        lote_entrada_2 = []
        for idx, (nome, cat, ca, val, un, est_min, prazo, custo, qtd_ini) in enumerate(catalogo_epis):
            cur = db.execute(
                """INSERT INTO epis (nome, categoria, ca, validade_ca, unidade, estoque_minimo, prazo_reposicao_dias)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (nome, cat, ca, val, un, est_min, prazo),
            )
            eid = cur.lastrowid
            epi_id[nome] = eid
            item_ent = {"epi_id": eid, "quantidade": qtd_ini, "custo_unitario": custo}
            if idx < 17:
                lote_entrada_1.append(item_ent)
            else:
                lote_entrada_2.append(item_ent)

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
        locais = pedidos.LOCAIS_FORMULARIO
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

        # 5. Registra entradas de estoque
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

        # 6. Saídas históricas (entregas para alimentar consumo médio, alertas de reposição e relatórios)
        historico_saidas = [
            (h - timedelta(days=45), "ABRAHAM JOSUE BARRETO CUELLO", "Araquari", [
                ("LUVA DE ALGODAO MESCLA 6 FIOS", 12),
                ("OCULOS LEOPARDO INCOLOR", 8),
                ("PROTETOR AURICULAR PLUG DE SILICONE 15DB", 15),
                ("BOTINA BIDENSIDADE C/ BICO PVC (Pedreiros, Auxiliares, Serventes e Roçada)", 6),
                ("CAMISETA MANGA CURTA", 10),
                ("CALÇA AZUL MARINHO BRIM C/ FAIXA REFLETIVA", 8),
            ]),
            (h - timedelta(days=28), "ANDERSON SCHROEDER", "Mafra", [
                ("LUVAS DE POLIAMIDA COM BANHO DE PU (MECÂNICA)", 10),
                ("OCULOS LEOPARDO INCOLOR", 9),  # deixa Óculos Leopardo com saldo 5 (abaixo do mínimo 15 -> alerta de Repor!)
                ("PROTETOR SOLAR FPS 30 120ML", 10),
                ("BONE BRIM AZUL COM PROTETOR DE NUCA", 8),
                ("CAMISETA MANGA LONGA", 10),
                ("CALÇA AZUL MARINHO BRIM C/ FAIXA REFLETIVA", 8),
            ]),
            (h - timedelta(days=14), "BRUNO MULLER DA SILVA", "São Francisco do Sul", [
                ("LUVA DE ALGODAO MESCLA 6 FIOS", 10),
                ("RESPIRADOR PFF2 COM VALVULA", 12),
                ("BOTINA BIDENSIDADE C/ BICO PVC (Pedreiros, Auxiliares, Serventes e Roçada)", 5),
                ("CAPACETE DE SEGURANÇA C/ SUSPENSÃO", 6),
                ("CAPA DE CHUVA TREVIRA FORRADA AMARELA", 6),
            ]),
            (h - timedelta(days=5), "CARLOS ARTUR FRANKE", "Garuva", [
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

        # 7. PEDIDOS DO FORMULÁRIO EM CADA ETAPA DO FLUXO

        # Pedido #1: EM SEPARAÇÃO (Ruan Alves / Araquari)
        pid_ruan, _ = pedidos.criar_pedido(db, {
            "id": "forms-201",
            "nome": "Ruan Alves",
            "local": "Araquari",
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
            "observacao": "Visita técnica nas obras de Araquari e Barra Velha",
        })
        pedidos.iniciar(db, pid_ruan, uid)
        itens_ruan = db.execute("SELECT id FROM pedido_itens WHERE pedido_id = ? ORDER BY id", (pid_ruan,)).fetchall()
        for r in itens_ruan[:2]:
            pedidos.marcar_separado(db, pid_ruan, r["id"], True)

        # Pedido #2: PRONTO PARA RETIRADA (já separado e com baixa feita)
        pid_pronto, _ = pedidos.criar_pedido(db, {
            "id": "forms-202",
            "nome": "ABRAHAM JOSUE BARRETO CUELLO",
            "local": "Araquari",
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
            "local": "Mafra",
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
            "observacao": "Enviar pelo motorista da rota de Mafra",
        })
        pedidos.iniciar(db, pid_retirado, uid)
        for r in db.execute("SELECT id FROM pedido_itens WHERE pedido_id = ?", (pid_retirado,)):
            pedidos.marcar_separado(db, pid_retirado, r["id"], True)
        pedidos.finalizar(db, pid_retirado, uid)
        pedidos.retirar(
            db, pid_retirado,
            nome="CARLOS ARTUR FRANKE",
            descricao="Encarregado de obras — levou para a frente de serviço de Mafra",
            usuario_id=uid,
        )

        # Pedido #4: PENDENTE (deixado para mais tarde)
        pid_pendente, _ = pedidos.criar_pedido(db, {
            "id": "forms-204",
            "nome": "BRUNO MULLER DA SILVA",
            "local": "São Francisco do Sul",
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
            "local": "Garuva",
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
            "observacao": "Equipe de roçada da obra de Garuva",
        })
        pedidos.criar_pedido(db, {
            "id": "forms-206",
            "nome": "CHARLES KOSTA HEIDRICH",
            "local": "Guaramirim",
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
            "observacao": "Passou a atuar no apoio de manutenção mecânica em Guaramirim",
        })

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

        print("Base de demonstração com os 33 itens e 158 colaboradores reais criada com sucesso!")


if __name__ == "__main__":
    popular()
