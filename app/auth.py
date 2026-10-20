import hmac
import secrets
import time
from functools import wraps

from flask import (Blueprint, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db
from .util import limpar

bp = Blueprint("auth", __name__)

PERFIS = {"admin": "Administrador", "almoxarifado": "Almoxarifado", "consulta": "Consulta"}
PERFIS_ESCRITA = ("admin", "almoxarifado")
ROTAS_PUBLICAS = {"auth.login", "auth.setup", "static", "pedidos.api_criar", "pedidos.solicitar"}
# A API usa chave própria (X-API-Key) e o formulário público pode ser enviado sem sessão prévia
CSRF_ISENTAS = {"pedidos.api_criar", "pedidos.solicitar"}
SENHA_MIN = 8

# Bloqueio simples contra tentativas repetidas de senha (em memória)
_falhas = {}
MAX_FALHAS = 5
BLOQUEIO_SEG = 300


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


def perfil_required(*perfis):
    def decorador(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if g.usuario["perfil"] not in perfis:
                abort(403)
            return view(*args, **kwargs)
        return wrapper
    return decorador


escrita_required = perfil_required(*PERFIS_ESCRITA)
admin_required = perfil_required("admin")


@bp.before_app_request
def antes_da_requisicao():
    g.usuario = None
    uid = session.get("uid")
    db = get_db()
    if uid:
        g.usuario = db.execute("SELECT * FROM usuarios WHERE id = ? AND ativo = 1", (uid,)).fetchone()
        if g.usuario is None:
            session.clear()

    if request.method == "POST" and request.endpoint not in CSRF_ISENTAS:
        # aceita o token no formulário ou no cabeçalho (requisições fetch)
        esperado = session.get("csrf", "")
        if not esperado or not hmac.compare_digest(esperado, request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")):
            abort(400, "Sessão expirada ou formulário inválido. Recarregue a página e tente de novo.")

    if g.usuario is None and request.endpoint not in ROTAS_PUBLICAS:
        if db.execute("SELECT 1 FROM usuarios LIMIT 1").fetchone() is None:
            return redirect(url_for("auth.setup"))
        return redirect(url_for("auth.login", next=request.full_path if request.method == "GET" else None))


def _senha_valida(senha, confirmacao):
    if len(senha) < SENHA_MIN:
        return f"A senha deve ter ao menos {SENHA_MIN} caracteres."
    if senha != confirmacao:
        return "A confirmação não confere com a senha."
    return None


@bp.route("/configuracao-inicial", methods=["GET", "POST"])
def setup():
    db = get_db()
    if db.execute("SELECT 1 FROM usuarios LIMIT 1").fetchone():
        return redirect(url_for("auth.login"))
    if request.method == "POST":
        nome, login = limpar(request.form.get("nome")), limpar(request.form.get("login"))
        senha = request.form.get("senha", "")
        erro = _senha_valida(senha, request.form.get("confirmacao", ""))
        if not nome or not login:
            erro = "Informe nome e login."
        if erro:
            flash(erro, "erro")
        else:
            db.execute(
                "INSERT INTO usuarios (nome, login, senha_hash, perfil) VALUES (?, ?, ?, 'admin')",
                (nome, login, generate_password_hash(senha)),
            )
            flash("Administrador criado. Entre com o login e a senha cadastrados.", "ok")
            return redirect(url_for("auth.login"))
    return render_template("setup.html")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.usuario:
        return redirect(url_for("painel.inicio"))
    if request.method == "POST":
        login_ = (request.form.get("login") or "").strip()
        chave = (request.remote_addr, login_.lower())
        tentativas, ate = _falhas.get(chave, (0, 0))
        if tentativas >= MAX_FALHAS and time.time() < ate:
            flash("Muitas tentativas. Aguarde alguns minutos e tente novamente.", "erro")
            return render_template("login.html"), 429
        usuario = get_db().execute(
            "SELECT * FROM usuarios WHERE login = ? AND ativo = 1", (login_,)
        ).fetchone()
        if usuario and check_password_hash(usuario["senha_hash"], request.form.get("senha", "")):
            _falhas.pop(chave, None)
            session.clear()
            session.permanent = True
            session["uid"] = usuario["id"]
            destino = request.args.get("next") or ""
            if not destino.startswith("/") or destino.startswith("//"):
                destino = url_for("painel.inicio")
            return redirect(destino)
        _falhas[chave] = (tentativas + 1, time.time() + BLOQUEIO_SEG)
        flash("Login ou senha incorretos.", "erro")
    return render_template("login.html")


@bp.post("/sair")
def sair():
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/minha-senha", methods=["GET", "POST"])
def minha_senha():
    if request.method == "POST":
        if not check_password_hash(g.usuario["senha_hash"], request.form.get("atual", "")):
            flash("Senha atual incorreta.", "erro")
        else:
            erro = _senha_valida(request.form.get("senha", ""), request.form.get("confirmacao", ""))
            if erro:
                flash(erro, "erro")
            else:
                get_db().execute("UPDATE usuarios SET senha_hash = ? WHERE id = ?",
                                 (generate_password_hash(request.form["senha"]), g.usuario["id"]))
                flash("Senha alterada.", "ok")
                return redirect(url_for("painel.inicio"))
    return render_template("minha_senha.html")


@bp.app_context_processor
def contexto():
    perfil = g.usuario["perfil"] if g.get("usuario") else None
    a_atender = 0
    if perfil:
        a_atender = get_db().execute(
            "SELECT COUNT(*) FROM pedidos WHERE status IN ('novo', 'pendente', 'em_separacao')").fetchone()[0]
    return {
        "pedidos_a_atender": a_atender,
        "csrf_token": csrf_token,
        "usuario": g.get("usuario"),
        "pode_editar": perfil in PERFIS_ESCRITA,
        "eh_admin": perfil == "admin",
    }
