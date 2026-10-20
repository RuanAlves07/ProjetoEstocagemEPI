import hashlib
import os
import secrets
from datetime import timedelta

from flask import Flask, render_template

from . import db


def _chave_secreta(instance_path):
    """Usa EPI_SECRET_KEY / SECRET_KEY, ou chave determinística na Vercel, ou arquivo local em instance/."""
    env = os.environ.get("EPI_SECRET_KEY") or os.environ.get("SECRET_KEY")
    if env:
        return env
    db_url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if os.environ.get("VERCEL") or db_url:
        semente = f"epi-estoque-secret-{db_url or 'vercel'}"
        return hashlib.sha256(semente.encode("utf-8")).hexdigest()
    caminho = os.path.join(instance_path, "secret_key")
    if not os.path.exists(caminho):
        with open(caminho, "w") as f:
            f.write(secrets.token_hex(32))
        os.chmod(caminho, 0o600)
    with open(caminho) as f:
        return f.read().strip()


def create_app(config=None):
    em_serverless = bool(os.environ.get("VERCEL"))
    pasta_instancia = "/tmp/epi_instance" if em_serverless else None
    app = Flask(__name__, instance_path=pasta_instancia, instance_relative_config=True)
    try:
        os.makedirs(app.instance_path, exist_ok=True)
    except OSError:
        app.instance_path = "/tmp/epi_instance"
        os.makedirs(app.instance_path, exist_ok=True)

    banco_padrao = (
        os.environ.get("DATABASE_URL")
        or os.environ.get("POSTGRES_URL")
        or os.path.join(app.instance_path, "epi.sqlite")
    )

    app.config.update(
        DATABASE=banco_padrao,
        SECRET_KEY=_chave_secreta(app.instance_path),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        POR_PAGINA=50,
        MAX_CONTENT_LENGTH=512 * 1024,
        # em produção atrás de HTTPS (ou Vercel): cookie seguro
        SESSION_COOKIE_SECURE=os.environ.get("EPI_COOKIE_SECURE") == "1" or em_serverless,
    )
    if config:
        app.config.update(config)

    if os.environ.get("EPI_PROXY") == "1" or em_serverless:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)

    from . import auth, cadastros, movimentos, painel, filtros, rotas_pedidos
    filtros.registrar(app)
    app.register_blueprint(auth.bp)
    app.register_blueprint(painel.bp)
    app.register_blueprint(cadastros.bp)
    app.register_blueprint(movimentos.bp)
    app.register_blueprint(rotas_pedidos.bp)

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(413)
    def erro(e):
        return render_template("erro.html", erro=e), e.code

    return app
