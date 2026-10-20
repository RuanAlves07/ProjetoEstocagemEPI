import os
import re
import sqlite3

from flask import current_app, g

_TABELAS_COM_ID = (
    "usuarios",
    "epis",
    "colaboradores",
    "movimentacoes",
    "pedidos",
    "pedido_itens",
    "colaborador_epis_solicitados",
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
        # Corrige prefixo postgres:// antigo caso necessário
        if dsn.startswith("postgres://"):
            dsn = "postgresql://" + dsn[len("postgres://"):]
        self._conn = psycopg2.connect(dsn)
        self._conn.autocommit = True

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
        return
    conn.execute("PRAGMA journal_mode = WAL")
    with open(os.path.join(os.path.dirname(__file__), "schema.sql"), encoding="utf-8") as f:
        conn.executescript(f.read())
    _migrar(conn)


def _migrar(conn):
    """Atualiza bancos SQLite criados por versões anteriores (CREATE IF NOT EXISTS não altera tabelas)."""
    colunas = {r["name"] for r in conn.execute("PRAGMA table_info(movimentacoes)")}
    if "pedido_id" not in colunas:
        conn.execute("ALTER TABLE movimentacoes ADD COLUMN pedido_id INTEGER REFERENCES pedidos (id)")
    if "local_destino" not in colunas:
        conn.execute("ALTER TABLE movimentacoes ADD COLUMN local_destino TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mov_pedido ON movimentacoes (pedido_id)")


def init_app(app):
    app.teardown_appcontext(close_db)
    # Em ambiente serverless (Vercel), inicializa o schema sob demanda ou se não estiver desativado
    if not os.environ.get("EPI_SKIP_INIT_DB"):
        with app.app_context():
            init_db()
