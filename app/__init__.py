import os
import secrets
from datetime import timedelta

from flask import Flask, render_template

from . import db


def _chave_secreta(instance_path):
    """Usa EPI_SECRET_KEY, ou gera uma chave e a guarda em instance/secret_key."""
    env = os.environ.get("EPI_SECRET_KEY")
    if env:
        return env
    caminho = os.path.join(instance_path, "secret_key")
    if not os.path.exists(caminho):
        with open(caminho, "w") as f:
            f.write(secrets.token_hex(32))
        os.chmod(caminho, 0o600)
    with open(caminho) as f:
        return f.read().strip()


def create_app(config=None):
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)
    app.config.update(
        DATABASE=os.path.join(app.instance_path, "epi.sqlite"),
        SECRET_KEY=_chave_secreta(app.instance_path),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        POR_PAGINA=50,
        MAX_CONTENT_LENGTH=512 * 1024,
        # em produção atrás de HTTPS: EPI_COOKIE_SECURE=1
        SESSION_COOKIE_SECURE=os.environ.get("EPI_COOKIE_SECURE") == "1",
    )
    if config:
        app.config.update(config)

    if os.environ.get("EPI_PROXY") == "1":
        # atrás de um proxy reverso (nginx/Caddy): confia nos cabeçalhos X-Forwarded-*
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
