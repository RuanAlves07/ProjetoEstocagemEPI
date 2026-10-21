import hmac
import re
import secrets
import sqlite3
import time
import unicodedata
from functools import wraps

from flask import (Blueprint, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db
from .util import limpar

bp = Blueprint("auth", __name__)

PERFIS = {"admin": "Administrador", "almoxarifado": "Almoxarifado", "consulta": "Consulta"}
PERFIS_ESCRITA = ("admin", "almoxarifado")

CATALOGO_PERMISSOES = [
    ("ver_painel", "Painel", "Visualizar o painel inicial com indicadores, alertas de reposição e CAs"),
    ("ver_pedidos", "Pedidos (Visualizar)", "Acessar a fila e o histórico de pedidos de EPIs e uniformes"),
    ("gerenciar_pedidos", "Pedidos (Atender / Separar)", "Iniciar separação, marcar itens, finalizar baixa e registrar retirada de pedidos"),
    ("ver_epis", "EPIs (Visualizar)", "Consultar o catálogo de EPIs, saldos e validade de CAs"),
    ("editar_epis", "EPIs (Cadastrar / Editar)", "Criar novos EPIs e alterar estoque mínimo, CA e prazos"),
    ("ver_fornecedores", "Fornecedores (Visualizar)", "Consultar o cadastro de fornecedores, CNPJ, contatos e histórico de compras"),
    ("editar_fornecedores", "Fornecedores (Cadastrar / Editar)", "Cadastrar novos fornecedores e atualizar CNPJ, endereço e condições comerciais"),
    ("ver_colaboradores", "Colaboradores e Cargos (Visualizar)", "Consultar fichas de colaboradores e kits padrão por cargo"),
    ("editar_colaboradores", "Colaboradores e Cargos (Editar)", "Cadastrar colaboradores, definir EPIs padrão por cargo e lançar solicitações"),
    ("ver_movimentos", "Movimentações (Visualizar)", "Consultar histórico de entradas, saídas, baixas e imprimir comprovantes"),
    ("registrar_movimentos", "Movimentações (Lançar Estoque)", "Registrar novas entradas, entregas avulsas, baixas e ajustes de saldo"),
    ("ver_relatorios", "Relatórios", "Visualizar relatórios gerenciais por período, setor, obra e colaborador"),
    ("cancelar_estornar", "Cancelar / Estornar Lançamentos", "Cancelar movimentações do livro-razão e cancelar pedidos prontos com estorno"),
]

TODAS_PERMISSOES = tuple(chave for chave, _, _ in CATALOGO_PERMISSOES)
PERMISSOES_ESCRITA = ("gerenciar_pedidos", "editar_epis", "editar_fornecedores", "editar_colaboradores", "registrar_movimentos")

# Permissões implícitas: quem pode editar um módulo também pode visualizá-lo
_IMPLICITAS = {
    "gerenciar_pedidos": "ver_pedidos",
    "editar_epis": "ver_epis",
    "editar_fornecedores": "ver_fornecedores",
    "editar_colaboradores": "ver_colaboradores",
    "registrar_movimentos": "ver_movimentos",
}

ROTAS_PUBLICAS = {"auth.login", "auth.setup", "static", "pedidos.api_criar", "pedidos.solicitar"}
# A API usa chave própria (X-API-Key) e o formulário público pode ser enviado sem sessão prévia
CSRF_ISENTAS = {"pedidos.api_criar", "pedidos.solicitar"}
SENHA_MIN = 8

# Bloqueio simples contra tentativas repetidas de senha (em memória)
_falhas = {}
MAX_FALHAS = 5
BLOQUEIO_SEG = 300


def gerar_chave_perfil(nome):
    """Converte o nome de um perfil em identificador único limpo (slug)."""
    sem_acento = "".join(
        c for c in unicodedata.normalize("NFD", nome or "") if unicodedata.category(c) != "Mn"
    )
    slug = re.sub(r"[^a-z0-9]+", "_", sem_acento.lower()).strip("_")
    return slug or "perfil"


def obter_perfis_dict(db=None):
    """Retorna dicionário {chave: nome} de todos os perfis cadastrados."""
    db = db or get_db()
    try:
        linhas = db.execute("SELECT chave, nome FROM perfis_acesso ORDER BY sistema DESC, nome COLLATE NOCASE").fetchall()
        if linhas:
            return {r["chave"]: r["nome"] for r in linhas}
    except Exception:
        pass
    return dict(PERFIS)


def permissoes_do_perfil(perfil, db=None):
    """Retorna o conjunto (set) de permissões ativas para o perfil informado."""
    if not perfil:
        return set()
    if perfil == "admin":
        return set(TODAS_PERMISSOES)
    db = db or get_db()
    try:
        linha = db.execute(
            "SELECT permissoes FROM perfis_acesso WHERE chave = ? COLLATE NOCASE", (perfil,)
        ).fetchone()
    except Exception:
        linha = None
    if not linha:
        if perfil == "almoxarifado":
            perms = {p for p in TODAS_PERMISSOES if p != "cancelar_estornar"}
        elif perfil == "consulta":
            perms = {"ver_painel", "ver_pedidos", "ver_epis", "ver_fornecedores", "ver_colaboradores", "ver_movimentos", "ver_relatorios"}
        else:
            perms = set()
    else:
        bruto = (linha["permissoes"] or "").strip()
        if bruto == "*":
            return set(TODAS_PERMISSOES)
        perms = {p.strip() for p in bruto.split(",") if p.strip() in TODAS_PERMISSOES}
    for escrita, leitura in _IMPLICITAS.items():
        if escrita in perms:
            perms.add(leitura)
    return perms


def usuario_tem_permissao(perm, usuario=None):
    u = usuario if usuario is not None else g.get("usuario")
    if not u:
        return False
    if u["perfil"] == "admin":
        return True
    perms = g.get("permissoes_usuario")
    if perms is None:
        perms = permissoes_do_perfil(u["perfil"])
    if isinstance(perm, (tuple, list, set)):
        return any(p in perms for p in perm)
    return perm in perms


def _permissao_exigida_rota(endpoint, metodo):
    """Retorna a permissão necessária para acessar o endpoint atual (ou None se livre para logados)."""
    if not endpoint or endpoint in ROTAS_PUBLICAS or endpoint.startswith("auth."):
        return None
    if endpoint.startswith("cadastros.usuario") or endpoint.startswith("cadastros.perfil"):
        return "__admin__"
    if endpoint == "painel.inicio":
        return "ver_painel"
    if endpoint == "painel.relatorios":
        return "ver_relatorios"
    if endpoint == "pedidos.integracao":
        return "__admin__"
    if endpoint in ("pedidos.lista", "pedidos.detalhe", "pedidos.exportar_csv", "pedidos.novos"):
        return "ver_pedidos"
    if endpoint.startswith("pedidos."):
        return "gerenciar_pedidos"
    if endpoint in ("cadastros.epis_lista", "cadastros.epi_detalhe", "cadastros.epis_csv"):
        return "ver_epis"
    if endpoint in ("cadastros.epi_novo", "cadastros.epi_editar"):
        return "editar_epis"
    if endpoint in ("cadastros.fornecedores_lista", "cadastros.fornecedor_detalhe"):
        return "ver_fornecedores"
    if endpoint in ("cadastros.fornecedor_novo", "cadastros.fornecedor_editar"):
        return "editar_fornecedores"
    if endpoint in ("cadastros.colaboradores_lista", "cadastros.colaborador_ficha"):
        return "ver_colaboradores"
    if endpoint == "cadastros.cargos_padrao":
        return "editar_colaboradores" if metodo == "POST" else "ver_colaboradores"
    if endpoint.startswith("cadastros.colaborador"):
        return "editar_colaboradores"
    if endpoint in ("movimentos.lista", "movimentos.exportar_csv", "movimentos.comprovante"):
        return "ver_movimentos"
    if endpoint == "movimentos.movimentar":
        return "registrar_movimentos"
    if endpoint == "movimentos.cancelar":
        return "cancelar_estornar"
    return None


def primeira_rota_permitida():
    """Retorna a URL da primeira tela que o perfil do usuário tem permissão para visualizar."""
    ordem = [
        ("ver_painel", "painel.inicio"),
        ("ver_pedidos", "pedidos.lista"),
        ("ver_epis", "cadastros.epis_lista"),
        ("ver_fornecedores", "cadastros.fornecedores_lista"),
        ("ver_colaboradores", "cadastros.colaboradores_lista"),
        ("ver_movimentos", "movimentos.lista"),
        ("ver_relatorios", "painel.relatorios"),
    ]
    for perm, ep in ordem:
        if usuario_tem_permissao(perm):
            return url_for(ep)
    return url_for("pedidos.solicitar")


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


def permissao_required(*perms):
    def decorador(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not g.get("usuario") or not usuario_tem_permissao(perms):
                abort(403)
            return view(*args, **kwargs)
        return wrapper
    return decorador


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not g.get("usuario"):
            abort(403)
        exigida = _permissao_exigida_rota(request.endpoint, request.method)
        if exigida and exigida != "__admin__":
            if not usuario_tem_permissao(exigida):
                abort(403)
        elif g.usuario["perfil"] != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapper


def escrita_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not g.get("usuario"):
            abort(403)
        exigida = _permissao_exigida_rota(request.endpoint, request.method)
        if exigida and exigida != "__admin__":
            if not usuario_tem_permissao(exigida):
                abort(403)
        elif not usuario_tem_permissao(PERMISSOES_ESCRITA):
            abort(403)
        return view(*args, **kwargs)
    return wrapper


@bp.before_app_request
def antes_da_requisicao():
    g.usuario = None
    g.permissoes_usuario = set()
    uid = session.get("uid")
    db = get_db()
    if uid:
        g.usuario = db.execute("SELECT * FROM usuarios WHERE id = ? AND ativo = 1", (uid,)).fetchone()
        if g.usuario is None:
            session.clear()
        else:
            g.permissoes_usuario = permissoes_do_perfil(g.usuario["perfil"], db)

    if request.method == "POST" and request.endpoint not in CSRF_ISENTAS:
        # aceita o token no formulário ou no cabeçalho (requisições fetch)
        esperado = session.get("csrf", "")
        if not esperado or not hmac.compare_digest(esperado, request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")):
            abort(400, "Sessão expirada ou formulário inválido. Recarregue a página e tente de novo.")

    if g.usuario is None and request.endpoint not in ROTAS_PUBLICAS:
        if db.execute("SELECT 1 FROM usuarios LIMIT 1").fetchone() is None:
            return redirect(url_for("auth.setup"))
        return redirect(url_for("auth.login", next=request.full_path if request.method == "GET" else None))

    if g.usuario is not None:
        exigida = _permissao_exigida_rota(request.endpoint, request.method)
        if exigida == "__admin__":
            if g.usuario["perfil"] != "admin":
                abort(403)
        elif exigida and not usuario_tem_permissao(exigida):
            if request.endpoint == "painel.inicio" and request.method == "GET":
                destino = primeira_rota_permitida()
                if destino != url_for("painel.inicio"):
                    return redirect(destino)
            abort(403)


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
        return redirect(primeira_rota_permitida())
    if request.method == "POST":
        login_ = (request.form.get("login") or "").strip()
        chave = (request.remote_addr, login_.lower())
        tentativas, ate = _falhas.get(chave, (0, 0))
        if tentativas >= MAX_FALHAS and time.time() < ate:
            flash("Muitas tentativas. Aguarde alguns minutos e tente novamente.", "erro")
            return render_template("login.html"), 429
        usuario = get_db().execute(
            "SELECT * FROM usuarios WHERE login = ? COLLATE NOCASE AND ativo = 1", (login_,)
        ).fetchone()
        if usuario and check_password_hash(usuario["senha_hash"], request.form.get("senha", "")):
            _falhas.pop(chave, None)
            session.clear()
            session.permanent = True
            session["uid"] = usuario["id"]
            g.usuario = usuario
            g.permissoes_usuario = permissoes_do_perfil(usuario["perfil"], get_db())
            destino = request.args.get("next") or ""
            if not destino.startswith("/") or destino.startswith("//"):
                destino = primeira_rota_permitida()
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
    db = get_db()
    if request.method == "POST":
        novo_login = limpar(request.form.get("login")) or g.usuario["login"]
        senha_nova = request.form.get("senha", "")
        if not check_password_hash(g.usuario["senha_hash"], request.form.get("atual", "")):
            flash("Senha atual incorreta.", "erro")
        elif not novo_login:
            flash("Informe um login válido.", "erro")
        else:
            erro = _senha_valida(senha_nova, request.form.get("confirmacao", "")) if senha_nova else None
            if erro:
                flash(erro, "erro")
            else:
                try:
                    db.execute("UPDATE usuarios SET login = ? WHERE id = ?", (novo_login, g.usuario["id"]))
                    if senha_nova:
                        db.execute("UPDATE usuarios SET senha_hash = ? WHERE id = ?",
                                   (generate_password_hash(senha_nova), g.usuario["id"]))
                    flash("Dados de acesso atualizados com sucesso.", "ok")
                    return redirect(primeira_rota_permitida())
                except sqlite3.IntegrityError:
                    flash("Esse login já está em uso por outro usuário.", "erro")
    return render_template("minha_senha.html")


@bp.app_context_processor
def contexto():
    u = g.get("usuario")
    perfil = u["perfil"] if u else None
    perms = g.get("permissoes_usuario", set()) if u else set()
    a_atender = 0
    if perfil and ("ver_pedidos" in perms or "gerenciar_pedidos" in perms):
        a_atender = get_db().execute(
            "SELECT COUNT(*) FROM pedidos WHERE status IN ('novo', 'pendente', 'em_separacao')"
        ).fetchone()[0]

    ep = request.endpoint or ""
    if ep.startswith("cadastros.epi"):
        pode_editar_ctx = "editar_epis" in perms
    elif ep.startswith("cadastros.fornecedor"):
        pode_editar_ctx = "editar_fornecedores" in perms
    elif ep.startswith("cadastros.colab") or ep == "cadastros.cargos_padrao":
        pode_editar_ctx = "editar_colaboradores" in perms
    elif ep.startswith("pedidos."):
        pode_editar_ctx = "gerenciar_pedidos" in perms
    elif ep.startswith("movimentos."):
        pode_editar_ctx = "registrar_movimentos" in perms
    else:
        pode_editar_ctx = any(p in perms for p in PERMISSOES_ESCRITA)

    return {
        "pedidos_a_atender": a_atender,
        "csrf_token": csrf_token,
        "usuario": u,
        "permissoes_usuario": perms,
        "tem_perm": lambda chave: chave in perms,
        "pode_editar": pode_editar_ctx,
        "pode_movimentar": "registrar_movimentos" in perms,
        "pode_cancelar": "cancelar_estornar" in perms,
        "eh_admin": perfil == "admin",
    }
