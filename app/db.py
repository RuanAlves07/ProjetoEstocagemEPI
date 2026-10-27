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
    if "tamanho" not in colunas_epis:
        conn.execute("ALTER TABLE epis ADD COLUMN tamanho TEXT")
    if "imagem_url" not in colunas_epis:
        conn.execute("ALTER TABLE epis ADD COLUMN imagem_url TEXT")
    if "exibir_no_forms" not in colunas_epis:
        conn.execute("ALTER TABLE epis ADD COLUMN exibir_no_forms INTEGER NOT NULL DEFAULT 1")

    # Garante que a tabela epis permita o mesmo nome com tamanhos diferentes (ex.: Botina 40 e Botina 41)
    info_epis = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'epis'"
    ).fetchone()
    if info_epis and info_epis["sql"] and "UNIQUE" in info_epis["sql"].upper():
        conn.executescript("""
            PRAGMA foreign_keys = OFF;
            CREATE TABLE epis_novo (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                nome                  TEXT NOT NULL COLLATE NOCASE,
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
            INSERT INTO epis_novo (id, nome, tamanho, categoria, ca, validade_ca, unidade, estoque_minimo, prazo_reposicao_dias, imagem_url, exibir_no_forms, ativo, criado_em)
            SELECT id, nome, tamanho, categoria, ca, validade_ca, unidade, estoque_minimo, prazo_reposicao_dias, imagem_url, exibir_no_forms, ativo, criado_em FROM epis;
            DROP TABLE epis;
            ALTER TABLE epis_novo RENAME TO epis;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_epis_nome_tamanho ON epis(nome, COALESCE(tamanho, ''));
            PRAGMA foreign_keys = ON;
        """)
    else:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_epis_nome_tamanho ON epis(nome, COALESCE(tamanho, ''))")

    colunas_pedidos = {r["name"] for r in conn.execute("PRAGMA table_info(pedidos)")}
    if "ressalva_observacao" not in colunas_pedidos:
        conn.execute("ALTER TABLE pedidos ADD COLUMN ressalva_observacao TEXT")
    if "pedido_origem_id" not in colunas_pedidos:
        conn.execute("ALTER TABLE pedidos ADD COLUMN pedido_origem_id INTEGER REFERENCES pedidos (id)")

    # Remove o CHECK fixo da coluna perfil em bancos SQLite antigos para permitir perfis personalizados
    info_usu = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'usuarios'"
    ).fetchone()
    if info_usu and info_usu["sql"] and "CHECK" in info_usu["sql"].upper():
        conn.executescript("""
            PRAGMA foreign_keys = OFF;
            CREATE TABLE usuarios_novo (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                nome        TEXT NOT NULL,
                login       TEXT NOT NULL UNIQUE COLLATE NOCASE,
                senha_hash  TEXT NOT NULL,
                perfil      TEXT NOT NULL,
                ativo       INTEGER NOT NULL DEFAULT 1,
                criado_em   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );
            INSERT INTO usuarios_novo (id, nome, login, senha_hash, perfil, ativo, criado_em)
            SELECT id, nome, login, senha_hash, perfil, ativo, criado_em FROM usuarios;
            DROP TABLE usuarios;
            ALTER TABLE usuarios_novo RENAME TO usuarios;
            PRAGMA foreign_keys = ON;
        """)


def init_app(app):
    app.teardown_appcontext(close_db)
    # Em ambiente serverless (Vercel), inicializa o schema sob demanda ou se não estiver desativado
    if not os.environ.get("EPI_SKIP_INIT_DB"):
        with app.app_context():
            init_db()
