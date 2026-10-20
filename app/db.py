import os
import sqlite3

from flask import current_app, g


def get_db():
    if "db" not in g:
        # isolation_level=None: autocommit; transações são abertas explicitamente (BEGIN IMMEDIATE)
        conn = sqlite3.connect(current_app.config["DATABASE"], isolation_level=None, timeout=15)
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
    conn.execute("PRAGMA journal_mode = WAL")
    with open(os.path.join(os.path.dirname(__file__), "schema.sql"), encoding="utf-8") as f:
        conn.executescript(f.read())
    _migrar(conn)


def _migrar(conn):
    """Atualiza bancos criados por versões anteriores (CREATE IF NOT EXISTS não altera tabelas)."""
    colunas = {r["name"] for r in conn.execute("PRAGMA table_info(movimentacoes)")}
    if "pedido_id" not in colunas:
        conn.execute("ALTER TABLE movimentacoes ADD COLUMN pedido_id INTEGER REFERENCES pedidos (id)")
    if "local_destino" not in colunas:
        conn.execute("ALTER TABLE movimentacoes ADD COLUMN local_destino TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mov_pedido ON movimentacoes (pedido_id)")


def init_app(app):
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db()
