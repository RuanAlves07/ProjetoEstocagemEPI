import os
import re
import sqlite3

from flask import current_app, g

_TABELAS_COM_ID = (
    "usuarios",
    "epis",
    "colaboradores",
    "fornecedores",
    "movimentacoes",
    "pedidos",
    "pedido_itens",
    "colaborador_epis_solicitados",
    "obras",
    "recuperacao_senha",
    "emails_enviados",
    "push_inscricoes",
)


def _eh_postgres(uri):
    return isinstance(uri, str) and uri.startswith(("postgres://", "postgresql://"))


def _traduzir_sql_postgres(sql):
    """Converte dialeto SQLite do projeto para PostgreSQL (Supabase) de forma transparente."""
    s = sql.strip()
    if s.upper().startswith("BEGIN"):
        return "BEGIN", False

    # Collation e buscas case-insensitive
    s = re.sub(r"=\s*\?\s*COLLATE\s+NOCASE", "ILIKE ?", s, flags=re.I)
    s = re.sub(r"\bLIKE\b", "ILIKE", s, flags=re.I)
    s = re.sub(r"\bCOLLATE\s+NOCASE\b", "", s, flags=re.I)

    # INSERT OR IGNORE -> ON CONFLICT DO NOTHING
    tem_ignore = False
    if re.match(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO\b", s, flags=re.I):
        s = re.sub(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT INTO", s, flags=re.I)
        tem_ignore = True

    # Placeholders: :nome -> %(nome)s e ? -> %s (antes do to_char com HH24:MI:SS)
    s = re.sub(r"(?<![a-zA-Z0-9_:]):([a-zA-Z_][a-zA-Z0-9_]*)", r"%(\1)s", s)
    s = s.replace("?", "%s")

    # Funções de data/hora e agregação
    s = re.sub(
        r"datetime\(\s*'now'\s*,\s*'localtime'\s*\)",
        "to_char(CURRENT_TIMESTAMP AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM-DD HH24:MI:SS')",
        s,
        flags=re.I,
    )
    s = re.sub(
        r"date\(\s*'now'\s*,\s*'localtime'\s*\)",
        "to_char(CURRENT_TIMESTAMP AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM-DD')",
        s,
        flags=re.I,
    )
    s = re.sub(r"\bGROUP_CONCAT\s*\(", "STRING_AGG(", s, flags=re.I)

    if tem_ignore and "on conflict" not in s.lower():
        s = s.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"

    # Anexa RETURNING id em INSERT de tabelas com chave serial para popular cur.lastrowid
    precisa_id = False
    m_ins = re.match(r"^\s*INSERT\s+INTO\s+([a-zA-Z0-9_]+)\b", s, flags=re.I)
    if m_ins and m_ins.group(1).lower() in _TABELAS_COM_ID and "returning" not in s.lower():
        s = s.rstrip().rstrip(";") + " RETURNING id"
        precisa_id = True

    return s, precisa_id


class _PostgresCursor:
    """Empacota o DictCursor do psycopg2 expondo a mesma interface do cursor sqlite3."""

    def __init__(self, cur, lastrowid=None):
        self._cur = cur
        self.lastrowid = lastrowid

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur)


class _PostgresConn:
    """Empacota a conexão psycopg2 expondo .execute(), .executescript() e .close() como sqlite3."""

    eh_postgres = True

    def __init__(self, dsn):
        import psycopg2
        import psycopg2.extras

        self._psycopg2 = psycopg2
        self._extras = psycopg2.extras
        # Corrige prefixo postgres:// antigo e remove colchetes acidentais [SENHA] do template do Supabase
        if dsn.startswith("postgres://"):
            dsn = "postgresql://" + dsn[len("postgres://"):]
        dsn = re.sub(r":\[([^\]@]+)\]@", r":\1@", dsn)
        self._conn = psycopg2.connect(dsn)
        self._conn.autocommit = True

    def commit(self):
        if not self._conn.autocommit:
            self._conn.commit()

    def rollback(self):
        if not self._conn.autocommit:
            self._conn.rollback()

    def execute(self, sql, params=None):
        sql_pg, precisa_id = _traduzir_sql_postgres(sql)
        cur = self._conn.cursor(cursor_factory=self._extras.DictCursor)
        try:
            cur.execute(sql_pg, params)
            lastrowid = None
            if precisa_id:
                linha = cur.fetchone()
                if linha:
                    lastrowid = linha[0]
            return _PostgresCursor(cur, lastrowid=lastrowid)
        except self._psycopg2.IntegrityError as e:
            raise sqlite3.IntegrityError(str(e)) from e

    def executemany(self, sql, seq_params):
        """Execução em lote (usada na importação da Base CAEPI, ~100 mil linhas)."""
        sql_pg, _ = _traduzir_sql_postgres(sql)
        cur = self._conn.cursor()
        try:
            self._extras.execute_batch(cur, sql_pg, seq_params, page_size=1000)
        except self._psycopg2.IntegrityError as e:
            raise sqlite3.IntegrityError(str(e)) from e
        finally:
            cur.close()

    def executescript(self, script_sql):
        cur = self._conn.cursor()
        cur.execute(script_sql)
        cur.close()

    def close(self):
        self._conn.close()


def get_db():
    if "db" not in g:
        alvo = current_app.config["DATABASE"]
        if _eh_postgres(alvo):
            g.db = _PostgresConn(alvo)
        else:
            conn = sqlite3.connect(alvo, isolation_level=None, timeout=15)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            g.db = conn
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    conn = get_db()
    if getattr(conn, "eh_postgres", False):
        caminho_pg = os.path.join(os.path.dirname(__file__), "schema_postgres.sql")
        with open(caminho_pg, encoding="utf-8") as f:
            conn.executescript(f.read())
        _garantir_perfis_padrao(conn)
        _garantir_base_caepi_inicial(conn)
        _garantir_obras_iniciais(conn)
        return
    conn.execute("PRAGMA journal_mode = WAL")
    with open(os.path.join(os.path.dirname(__file__), "schema.sql"), encoding="utf-8") as f:
        conn.executescript(f.read())
    _migrar(conn)
    _garantir_perfis_padrao(conn)
    _garantir_base_caepi_inicial(conn)
    _garantir_obras_iniciais(conn)


def _garantir_obras_iniciais(conn):
    """Na primeira vez, cadastra as obras: as salvas no editor do formulário (versão anterior) ou a lista padrão."""
    if conn.execute("SELECT 1 FROM obras LIMIT 1").fetchone():
        return
    import json
    from . import pedidos
    obras = []
    linha = conn.execute("SELECT valor FROM configuracoes WHERE chave = 'forms_config'").fetchone()
    if linha and linha["valor"]:
        try:
            obras = pedidos.limpar_obras(json.loads(linha["valor"]).get("obras") or [])
        except (ValueError, AttributeError):
            obras = []
    for o in obras or pedidos.limpar_obras(pedidos.OBRAS_FORMULARIO):
        conn.execute(
            "INSERT OR IGNORE INTO obras (nome, cidade, endereco, mapa) VALUES (?, ?, ?, ?)",
            (o["nome"], o["cidade"], o["endereco"], o["mapa"]),
        )
    if hasattr(conn, "commit"):
        conn.commit()


def _garantir_base_caepi_inicial(conn):
    """Garante que a base_caepi contenha ao menos o catálogo semente de CAs reais."""
    from . import caepi
    caepi.garantir_base_inicial(conn)


def _garantir_perfis_padrao(conn):
    """Garante que os perfis base existam na tabela perfis_acesso sem sobrescrever ajustes já salvos."""
    padroes = [
        (
            "admin",
            "Administrador",
            "Acesso total ao sistema, incluindo cancelamento de lançamentos e gerenciamento de usuários e perfis.",
            "*",
            1,
        ),
        (
            "almoxarifado",
            "Almoxarifado",
            "Cadastros de EPIs, fornecedores e colaboradores, atendimento de pedidos, entradas, entregas, baixas e ajustes de estoque.",
            "ver_painel,ver_pedidos,gerenciar_pedidos,ver_epis,editar_epis,ver_fornecedores,editar_fornecedores,ver_colaboradores,editar_colaboradores,ver_movimentos,registrar_movimentos,ver_relatorios",
            1,
        ),
        (
            "consulta",
            "Consulta",
            "Apenas visualização do painel, pedidos, catálogo de EPIs, fornecedores, colaboradores, histórico e relatórios.",
            "ver_painel,ver_pedidos,ver_epis,ver_fornecedores,ver_colaboradores,ver_movimentos,ver_relatorios",
            1,
        ),
        (
            "gestor",
            "Gestor",
            "Entra pelo login do gestor e usa somente o formulário do gestor para pedir EPIs a colaboradores fora do padrão do cargo.",
            "solicitar_fora_funcao",
            1,
        ),
    ]
    # Perfil Gestor criado com o padrão antigo (sem edição do administrador) passa a ver só o formulário do gestor
    conn.execute(
        "UPDATE perfis_acesso SET permissoes = 'solicitar_fora_funcao' "
        "WHERE chave = 'gestor' AND permissoes = 'ver_pedidos,ver_colaboradores,solicitar_fora_funcao'"
    )
    for chave, nome, desc, perms, sis in padroes:
        conn.execute(
            "INSERT OR IGNORE INTO perfis_acesso (chave, nome, descricao, permissoes, sistema) VALUES (?, ?, ?, ?, ?)",
            (chave, nome, desc, perms, sis),
        )
    # Garante que perfis padrão criados antes da aba Fornecedores recebam a permissão correspondente
    for ch_padrao, novas_p in (
        ("almoxarifado", ("ver_fornecedores", "editar_fornecedores")),
        ("consulta", ("ver_fornecedores",)),
    ):
        row = conn.execute("SELECT permissoes FROM perfis_acesso WHERE chave = ?", (ch_padrao,)).fetchone()
        if row and row["permissoes"] and row["permissoes"] != "*":
            atuais = [p.strip() for p in row["permissoes"].split(",") if p.strip()]
            alterado = False
            for np in novas_p:
                if np not in atuais:
                    atuais.append(np)
                    alterado = True
            if alterado:
                conn.execute(
                    "UPDATE perfis_acesso SET permissoes = ? WHERE chave = ?",
                    (",".join(atuais), ch_padrao),
                )


def _migrar(conn):
    """Atualiza bancos SQLite criados por versões anteriores (CREATE IF NOT EXISTS não altera tabelas)."""
    colunas = {r["name"] for r in conn.execute("PRAGMA table_info(movimentacoes)")}
    if "pedido_id" not in colunas:
        conn.execute("ALTER TABLE movimentacoes ADD COLUMN pedido_id INTEGER REFERENCES pedidos (id)")
    if "local_destino" not in colunas:
        conn.execute("ALTER TABLE movimentacoes ADD COLUMN local_destino TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mov_pedido ON movimentacoes (pedido_id)")

    colunas_epis = {r["name"] for r in conn.execute("PRAGMA table_info(epis)")}
    if "nome_formulario" not in colunas_epis:
        conn.execute("ALTER TABLE epis ADD COLUMN nome_formulario TEXT COLLATE NOCASE")
    if "tamanho" not in colunas_epis:
        conn.execute("ALTER TABLE epis ADD COLUMN tamanho TEXT")
    if "ca" not in colunas_epis:
        conn.execute("ALTER TABLE epis ADD COLUMN ca TEXT")
    if "imagem_url" not in colunas_epis:
        conn.execute("ALTER TABLE epis ADD COLUMN imagem_url TEXT")
    if "exibir_no_forms" not in colunas_epis:
        conn.execute("ALTER TABLE epis ADD COLUMN exibir_no_forms INTEGER NOT NULL DEFAULT 1")

    # Garante que a tabela epis permita o mesmo nome com tamanhos ou CAs diferentes
    info_epis = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'epis'"
    ).fetchone()
    if info_epis and info_epis["sql"] and "UNIQUE" in info_epis["sql"].upper():
        conn.executescript("""
            PRAGMA foreign_keys = OFF;
            CREATE TABLE epis_novo (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                nome                  TEXT NOT NULL COLLATE NOCASE,
                nome_formulario       TEXT COLLATE NOCASE,
                tamanho               TEXT,
                categoria             TEXT,
                ca                    TEXT,
                validade_ca           TEXT,
                unidade               TEXT NOT NULL DEFAULT 'un',
                estoque_minimo        INTEGER NOT NULL DEFAULT 0 CHECK (estoque_minimo >= 0),
                prazo_reposicao_dias  INTEGER NOT NULL DEFAULT 15 CHECK (prazo_reposicao_dias >= 0),
                imagem_url            TEXT,
                exibir_no_forms       INTEGER NOT NULL DEFAULT 1,
                ativo                 INTEGER NOT NULL DEFAULT 1,
                criado_em             TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );
            INSERT INTO epis_novo (id, nome, nome_formulario, tamanho, categoria, ca, validade_ca, unidade, estoque_minimo, prazo_reposicao_dias, imagem_url, exibir_no_forms, ativo, criado_em)
            SELECT id, nome, nome_formulario, tamanho, categoria, ca, validade_ca, unidade, estoque_minimo, prazo_reposicao_dias, imagem_url, exibir_no_forms, ativo, criado_em FROM epis;
            DROP TABLE epis;
            ALTER TABLE epis_novo RENAME TO epis;
            DROP INDEX IF EXISTS idx_epis_nome_tamanho;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_epis_nome_tam_ca ON epis(nome, COALESCE(tamanho, ''), COALESCE(ca, ''));
            PRAGMA foreign_keys = ON;
        """)
    else:
        conn.execute("DROP INDEX IF EXISTS idx_epis_nome_tamanho")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_epis_nome_tam_ca ON epis(nome, COALESCE(tamanho, ''), COALESCE(ca, ''))")

    # Garante que todo colaborador possua matrícula vinculada automaticamente como seu ID principal
    usadas_mat = {
        str(r["matricula"]).strip()
        for r in conn.execute("SELECT matricula FROM colaboradores WHERE matricula IS NOT NULL AND trim(matricula) != ''")
    }
    sem_mat = conn.execute(
        "SELECT id FROM colaboradores WHERE matricula IS NULL OR trim(matricula) = '' ORDER BY id"
    ).fetchall()
    for r in sem_mat:
        cand = str(r["id"])
        n = r["id"]
        while cand in usadas_mat:
            n += 1
            cand = str(n)
        conn.execute("UPDATE colaboradores SET matricula = ? WHERE id = ?", (cand, r["id"]))
        usadas_mat.add(cand)

    colunas_pedidos = {r["name"] for r in conn.execute("PRAGMA table_info(pedidos)")}
    if "ressalva_observacao" not in colunas_pedidos:
        conn.execute("ALTER TABLE pedidos ADD COLUMN ressalva_observacao TEXT")
    if "pedido_origem_id" not in colunas_pedidos:
        conn.execute("ALTER TABLE pedidos ADD COLUMN pedido_origem_id INTEGER REFERENCES pedidos (id)")
    if "solicitante_usuario_id" not in colunas_pedidos:
        conn.execute("ALTER TABLE pedidos ADD COLUMN solicitante_usuario_id INTEGER REFERENCES usuarios (id)")
    if "solicitante_email" not in colunas_pedidos:
        conn.execute("ALTER TABLE pedidos ADD COLUMN solicitante_email TEXT")

    # Remove o CHECK fixo da coluna perfil em bancos SQLite antigos para permitir perfis personalizados
    info_usu = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'usuarios'"
    ).fetchone()
    if info_usu and info_usu["sql"] and "CHECK" in info_usu["sql"].upper():
        conn.executescript("""
            PRAGMA foreign_keys = OFF;
            CREATE TABLE usuarios_novo (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                nome                  TEXT NOT NULL,
                login                 TEXT NOT NULL UNIQUE COLLATE NOCASE,
                email                 TEXT,
                receber_notificacoes  INTEGER NOT NULL DEFAULT 1,
                senha_hash            TEXT NOT NULL,
                perfil                TEXT NOT NULL,
                ativo                 INTEGER NOT NULL DEFAULT 1,
                criado_em             TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );
            INSERT INTO usuarios_novo (id, nome, login, senha_hash, perfil, ativo, criado_em)
            SELECT id, nome, login, senha_hash, perfil, ativo, criado_em FROM usuarios;
            DROP TABLE usuarios;
            ALTER TABLE usuarios_novo RENAME TO usuarios;
            PRAGMA foreign_keys = ON;
        """)

    colunas_usu = {r["name"] for r in conn.execute("PRAGMA table_info(usuarios)")}
    if "email" not in colunas_usu:
        conn.execute("ALTER TABLE usuarios ADD COLUMN email TEXT")
    if "receber_notificacoes" not in colunas_usu:
        conn.execute("ALTER TABLE usuarios ADD COLUMN receber_notificacoes INTEGER NOT NULL DEFAULT 1")
    if "foto_perfil" not in colunas_usu:
        conn.execute("ALTER TABLE usuarios ADD COLUMN foto_perfil TEXT")

    # Tabela de inscrições de notificações Web Push mobile
    conn.execute("""
        CREATE TABLE IF NOT EXISTS push_inscricoes (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_id            INTEGER REFERENCES usuarios (id) ON DELETE CASCADE,
            endpoint              TEXT NOT NULL UNIQUE,
            p256dh                TEXT NOT NULL,
            auth                  TEXT NOT NULL,
            user_agent            TEXT,
            criado_em             TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """)
    colunas_push = {r["name"] for r in conn.execute("PRAGMA table_info(push_inscricoes)")}
    if "apelido" not in colunas_push:
        conn.execute("ALTER TABLE push_inscricoes ADD COLUMN apelido TEXT")

    # Sincroniza os 22 EPIs e 3 Uniformes oficiais do formulário público se houver catálogo carregado
    _sincronizar_catalogo_formulario_oficial(conn)

    # Migra categorias existentes para os novos nomes formais e padronizados
    _migrar_categorias_oficiais(conn)

    # Migra padrão de EPIs por cargo para suportar itens genéricos do formulário
    _migrar_cargo_epis_padrao(conn)


def _sincronizar_catalogo_formulario_oficial(conn):
    """Remove da tabela epis quaisquer itens genéricos que tenham sido inseridos automaticamente sem estoque,
    e vincula automaticamente o nome_formulario nos EPIs reais já cadastrados."""
    try:
        colunas = {r["name"] for r in conn.execute("PRAGMA table_info(epis)")}
        if "categoria" not in colunas or "nome_formulario" not in colunas:
            return
        tem_epis = conn.execute("SELECT COUNT(*) FROM epis").fetchone()[0]
    except Exception:
        return
    if tem_epis == 0:
        return

    from .pedidos import TODOS_ITENS_GENERICOS_FORMULARIO, normalizar

    # 1. Remove linhas genéricas inseridas automaticamente na versão anterior (sem CA, sem tamanho, estoque_minimo=0 e sem movimentações/pedidos)
    nomes_gen_upper = {n.strip().upper() for n in TODOS_ITENS_GENERICOS_FORMULARIO}
    for r in conn.execute(
        "SELECT id, nome, ca, tamanho, estoque_minimo FROM epis WHERE ca IS NULL AND tamanho IS NULL AND COALESCE(estoque_minimo, 0) = 0"
    ).fetchall():
        if (r["nome"] or "").strip().upper() in nomes_gen_upper:
            eid = r["id"]
            em_uso = (
                conn.execute("SELECT 1 FROM movimentacoes WHERE epi_id = ? LIMIT 1", (eid,)).fetchone()
                or conn.execute("SELECT 1 FROM pedido_itens WHERE epi_id = ? LIMIT 1", (eid,)).fetchone()
                or conn.execute("SELECT 1 FROM cargo_epis_padrao WHERE epi_id = ? LIMIT 1", (eid,)).fetchone()
                or conn.execute("SELECT 1 FROM colaborador_epis_padrao WHERE epi_id = ? LIMIT 1", (eid,)).fetchone()
                or conn.execute("SELECT 1 FROM colaborador_epis_solicitados WHERE epi_id = ? LIMIT 1", (eid,)).fetchone()
            )
            if not em_uso:
                conn.execute("DELETE FROM epis WHERE id = ?", (eid,))

    # 2. Se houver EPIs reais cujo nome bata com um item genérico e ainda estejam sem nome_formulario, preenche o vínculo
    mapa_termos = [
        ("ABAFADOR TIPO CONCHA 18DB", ["ABAFADOR TIPO CONCHA 18DB"]),
        ("ABAFADOR TIPO CONCHA 26DB", ["ABAFADOR TIPO CONCHA 26DB"]),
        ("AVENTAL DE RASPA SEM EMENDA 1X0,6", ["AVENTAL DE RASPA"]),
        ("BONE BRIM AZUL COM PROTETOR DE NUCA", ["BONE BRIM AZUL COM PROTETOR DE NUCA"]),
        ("BONE BRIM COM PROTETOR TELA", ["BONE BRIM COM PROTETOR TELA"]),
        ("BOTA DE PVC CANO EXTRA LONGO", ["BOTA DE PVC PRETA CANO EXTRA LONGO", "BOTA DE PVC"]),
        ("CAPA DE CHUVA TREVIRA FORRADA AMARELA", ["CAPA DE CHUVA TREVIRA FORRADA AMARELA", "CAPA DE CHUVA"]),
        ("CAPACETE DE SEGURANÇA C/ SUSPENSÃO", ["CAPACETE DE SEGURANÇA C/ SUSPENSÃO"]),
        ("CARNEIRA SUSPENSÃO PLÁSTICA", ["CARNEIRA SUSPENSÃO PLÁSTICA"]),
        ("LUVA DE ALGODAO MESCLA 6 FIOS", ["LUVA DE ALGODAO MESCLA 6 FIOS"]),
        ("LUVA MULTIUSO AMARELA DE LATEX", ["LUVA MULTIUSO AMARELA DE LATEX"]),
        ("LUVAS VAQUETA PETROLEIRA", ["LUVAS VAQUETA PETROLEIRA"]),
        ("OCULOS LEOPARDO INCOLOR", ["OCULOS LEOPARDO INCOLOR"]),
        ("OCULOS PANDA INCOLOR", ["OCULOS PANDA INCOLOR"]),
        ("OCULOS SUMMER CINZA", ["OCULOS SUMMER CINZA"]),
        ("PERNEIRA DE PROTECAO BIDIN/COURO COM 3 TALAS SOLDADAS", ["PERNEIRA DE PROTECAO BIDIN/COURO COM 3 TALAS SOLDADAS"]),
        ("PROTETOR AURICULAR PLUG DE SILICONE 15DB", ["PROTETOR AURICULAR PLUG DE SILICONE 15DB"]),
        ("PROTETOR SOLAR FPS 30 COM REPELENTE 120ML", ["PROTETOR SOLAR FPS 30 120ML", "PROTETOR SOLAR"]),
        ("RESPIRADOR PFF2 COM VALVULA", ["RESPIRADOR PFF2 COM VALVULA"]),
        ("SAPATÃO BIDENSIDADE NOBUCK", ["SAPATÃO BIDENSIDADE NOBUCK"]),
        ("SAPATÃO CANO BAIXO C/ ELASTICO BICO PVC", ["SAPATÃO CANO BAIXO C/ ELASTICO BICO PVC"]),
        ("BOTINA CANO MÉDIO BIDENSIDADE C/ BICO PVC", ["BOTINA BIDENSIDADE C/ BICO PVC", "BOTINA CANO MÉDIO"]),
        ("CALÇA AZUL MARINHO BRIM C/ FAIXA REFLETIVA", ["CALÇA AZUL MARINHO BRIM C/ FAIXA REFLETIVA"]),
        ("CAMISETA MANGA CURTA", ["CAMISETA MANGA CURTA"]),
        ("CAMISETA MANGA LONGA", ["CAMISETA MANGA LONGA"]),
    ]
    for nome_form, termos in mapa_termos:
        for termo in termos:
            t_norm = normalizar(termo)
            for r in conn.execute("SELECT id, nome, nome_formulario FROM epis").fetchall():
                if not (r["nome_formulario"] or "").strip() and t_norm in normalizar(r["nome"]):
                    conn.execute("UPDATE epis SET nome_formulario = ? WHERE id = ?", (nome_form, r["id"]))

    # 3. Apenas EPIs vinculados a um nome_formulario ficam marcados com exibir_no_forms = 1
    for r in conn.execute("SELECT id, nome_formulario FROM epis").fetchall():
        nf = (r["nome_formulario"] or "").strip()
        conn.execute("UPDATE epis SET exibir_no_forms = ? WHERE id = ?", (1 if nf else 0, r["id"]))


def _migrar_categorias_oficiais(conn):
    """Atualiza as categorias de EPIs cadastrados e da base_caepi para os novos nomes formais padronizados."""
    try:
        colunas_epis = {r["name"] for r in conn.execute("PRAGMA table_info(epis)")}
        if "categoria" not in colunas_epis:
            return
    except Exception:
        return

    mapa = {
        "CALÇADOS": "Calçados de Segurança",
        "CALCADOS": "Calçados de Segurança",
        "VESTIMENTA": "Vestimentas e Uniformes",
        "VESTIMENTAS": "Vestimentas e Uniformes",
        "UNIFORME": "Vestimentas e Uniformes",
        "UNIFORMES": "Vestimentas e Uniformes",
        "MÃOS": "Proteção das Mãos",
        "MAOS": "Proteção das Mãos",
        "CABEÇA": "Proteção da Cabeça",
        "CABECA": "Proteção da Cabeça",
        "OLHOS E FACE": "Proteção dos Olhos e Face",
        "AUDITIVO": "Proteção Auditiva",
        "RESPIRATÓRIO": "Proteção Respiratória",
        "RESPIRATORIO": "Proteção Respiratória",
        "TRABALHO EM ALTURA": "Trabalho em Altura e Cintos",
        "OUTROS / EPI": "Outros / Diversos",
        "OUTROS": "Outros / Diversos",
        "EPI": "Outros / Diversos",
    }
    for cat_antiga, cat_nova in mapa.items():
        conn.execute("UPDATE epis SET categoria = ? WHERE UPPER(TRIM(categoria)) = ?", (cat_nova, cat_antiga))
        try:
            conn.execute("UPDATE base_caepi SET categoria = ? WHERE UPPER(TRIM(categoria)) = ?", (cat_nova, cat_antiga))
        except Exception:
            pass

    # Perneiras e itens de pele / protetor solar que estivessem em categorias genéricas
    conn.execute(
        "UPDATE epis SET categoria = 'Perneiras de Proteção' WHERE LOWER(nome) LIKE '%perneira%' AND (categoria IS NULL OR categoria IN ('Vestimentas e Uniformes', 'Outros / Diversos'))"
    )
    conn.execute(
        "UPDATE epis SET categoria = 'Proteção da Pele e Dermatológicos' WHERE (LOWER(nome) LIKE '%protetor solar%' OR LOWER(nome) LIKE '%repelente%') AND (categoria IS NULL OR categoria IN ('Outros / Diversos', 'Proteção das Mãos'))"
    )


def _migrar_cargo_epis_padrao(conn):
    """Garante que cargo_epis_padrao não dependa da chave estrangeira restritiva de epis,
    possua a coluna nome_generico e converta itens legados (< 90000) para os IDs sintéticos oficiais."""
    try:
        colunas_cep = {r["name"] for r in conn.execute("PRAGMA table_info(cargo_epis_padrao)")}
    except Exception:
        return

    info_cargo_epis = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'cargo_epis_padrao'"
    ).fetchone()
    precisa_recriar = False
    if info_cargo_epis and info_cargo_epis["sql"]:
        sql_cep = info_cargo_epis["sql"].upper()
        if "REFERENCES EPIS" in sql_cep or "REFERENCES \"EPIS\"" in sql_cep:
            precisa_recriar = True
    if "nome_generico" not in colunas_cep:
        precisa_recriar = True

    if precisa_recriar:
        conn.executescript("""
            PRAGMA foreign_keys = OFF;
            CREATE TABLE IF NOT EXISTS cargo_epis_padrao_novo (
                cargo          TEXT NOT NULL,
                epi_id         INTEGER NOT NULL,
                nome_generico  TEXT,
                criado_em      TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                PRIMARY KEY (cargo, epi_id)
            );
        """)
        if "nome_generico" in colunas_cep:
            conn.execute("""
                INSERT OR IGNORE INTO cargo_epis_padrao_novo (cargo, epi_id, nome_generico, criado_em)
                SELECT cargo, epi_id, nome_generico, criado_em FROM cargo_epis_padrao
            """)
        else:
            conn.execute("""
                INSERT OR IGNORE INTO cargo_epis_padrao_novo (cargo, epi_id, criado_em)
                SELECT cargo, epi_id, criado_em FROM cargo_epis_padrao
            """)
        conn.executescript("""
            DROP TABLE cargo_epis_padrao;
            ALTER TABLE cargo_epis_padrao_novo RENAME TO cargo_epis_padrao;
            PRAGMA foreign_keys = ON;
        """)

    from .pedidos import (
        ITENS_GENERICOS_EPI_FORMULARIO,
        ITENS_GENERICOS_UNIFORME_FORMULARIO,
        normalizar,
    )

    # Constrói mapeamento de nome normalizado para (id_sintético, nome_oficial)
    mapa_sintetico = {}
    for idx, nome_gen in enumerate(ITENS_GENERICOS_EPI_FORMULARIO):
        mapa_sintetico[normalizar(nome_gen)] = (90000 + idx, nome_gen)
    for idx, nome_gen in enumerate(ITENS_GENERICOS_UNIFORME_FORMULARIO):
        mapa_sintetico[normalizar(nome_gen)] = (91000 + idx, nome_gen)

    # 1. Se houver registros legados com epi_id < 90000, converte para o id sintético
    registros_legados = conn.execute(
        "SELECT cp.cargo, cp.epi_id, e.nome, e.nome_formulario FROM cargo_epis_padrao cp "
        "LEFT JOIN epis e ON e.id = cp.epi_id WHERE cp.epi_id < 90000"
    ).fetchall()
    for reg in registros_legados:
        cargo = reg["cargo"]
        eid_antigo = reg["epi_id"]
        nf = reg["nome_formulario"] or ""
        nm = reg["nome"] or ""
        par = mapa_sintetico.get(normalizar(nf)) or mapa_sintetico.get(normalizar(nm))
        if par:
            novo_id, nome_oficial = par
            conn.execute("DELETE FROM cargo_epis_padrao WHERE cargo = ? AND epi_id = ?", (cargo, eid_antigo))
            conn.execute(
                "INSERT OR REPLACE INTO cargo_epis_padrao (cargo, epi_id, nome_generico) VALUES (?, ?, ?)",
                (cargo, novo_id, nome_oficial),
            )

    # 2. Se houver registros com epi_id >= 90000 sem nome_generico, preenche
    for r in conn.execute(
        "SELECT cargo, epi_id FROM cargo_epis_padrao WHERE epi_id >= 90000 AND (nome_generico IS NULL OR trim(nome_generico) = '')"
    ).fetchall():
        eid = r["epi_id"]
        nome_oficial = None
        if 90000 <= eid < 90000 + len(ITENS_GENERICOS_EPI_FORMULARIO):
            nome_oficial = ITENS_GENERICOS_EPI_FORMULARIO[eid - 90000]
        elif 91000 <= eid < 91000 + len(ITENS_GENERICOS_UNIFORME_FORMULARIO):
            nome_oficial = ITENS_GENERICOS_UNIFORME_FORMULARIO[eid - 91000]
        if nome_oficial:
            conn.execute(
                "UPDATE cargo_epis_padrao SET nome_generico = ? WHERE cargo = ? AND epi_id = ?",
                (nome_oficial, r["cargo"], eid),
            )


def init_app(app):
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db()
