from datetime import datetime, timedelta
import hashlib
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

from . import email_service
from .db import get_db
from .estoque import ErroValidacao
from . import pedidos
from .util import limpar

bp = Blueprint("auth", __name__)

PERFIS = {"admin": "Administrador", "almoxarifado": "Almoxarifado", "consulta": "Consulta", "gestor": "Gestor"}
PERFIS_ESCRITA = ("admin", "almoxarifado")

CATALOGO_PERMISSOES = [
    ("ver_painel", "Painel", "Visualizar o painel inicial com indicadores, alertas de reposição e CAs"),
    ("ver_pedidos", "Pedidos (Visualizar)", "Acessar a fila e o histórico de pedidos de EPIs e uniformes"),
    ("gerenciar_pedidos", "Pedidos (Atender / Separar)", "Iniciar separação, marcar itens, finalizar baixa e registrar retirada de pedidos"),
    ("ver_epis", "EPIs (Visualizar)", "Consultar o catálogo de EPIs, saldos e validade de CAs"),
    ("editar_epis", "EPIs (Cadastrar / Editar)", "Criar novos EPIs e alterar estoque mínimo, CA e prazos"),
    ("ver_fornecedores", "Fornecedores (Visualizar)", "Consultar o cadastro de fornecedores, CNPJ, contatos e histórico de compras"),
    ("editar_fornecedores", "Fornecedores (Cadastrar / Editar)", "Cadastrar novos fornecedores e atualizar CNPJ, endereço e condições comerciais"),
    ("ver_colaboradores", "Colaboradores, Cargos e Obras (Visualizar)", "Consultar fichas de colaboradores, kits padrão por cargo e obras"),
    ("editar_colaboradores", "Colaboradores, Cargos e Obras (Editar)", "Cadastrar colaboradores, definir EPIs padrão por cargo, lançar solicitações e cadastrar/ativar obras"),
    ("ver_movimentos", "Movimentações (Visualizar)", "Consultar histórico de entradas, saídas, baixas e imprimir comprovantes"),
    ("registrar_movimentos", "Movimentações (Lançar Estoque)", "Registrar novas entradas, entregas avulsas, baixas e ajustes de saldo"),
    ("ver_relatorios", "Relatórios", "Visualizar relatórios gerenciais por período, setor, obra e colaborador"),
    ("cancelar_estornar", "Cancelar / Estornar Lançamentos", "Cancelar movimentações do livro-razão e cancelar pedidos prontos com estorno"),
    ("solicitar_fora_funcao", "Formulário do Gestor (fora da função)", "Usar o formulário do gestor para pedir EPIs a um colaborador fora do padrão do cargo dele"),
    ("gerenciar_modelos", "Modelos de documento", "Criar e editar os modelos da ficha de EPI usados na retirada de pedidos"),
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

ROTAS_PUBLICAS = {
    "auth.login",
    "auth.login_gestor",
    "auth.esqueci_senha",
    "auth.redefinir_senha",
    "auth.redefinir_senha_token",
    "auth.setup",
    "static",
    "pedidos.api_criar",
    "pedidos.solicitar",
    "favicon",
    "pwa_manifest",
    "service_worker",
    "push.chave_publica",
    "push.inscrever",
    "push.desinscrever",
    "push.testar",
    # link da ficha de EPI enviado ao colaborador pelo WhatsApp (protegido por token e código)
    "fichas.publica",
    "fichas.publica_codigo",
    "fichas.publica_validar",
    "fichas.publica_assinar",
}
# A API usa chave própria (X-API-Key) e o formulário público pode ser enviado sem sessão prévia
CSRF_ISENTAS = {
    "pedidos.api_criar",
    "pedidos.solicitar",
    "push.inscrever",
    "push.desinscrever",
    "push.testar",
}
SENHA_MIN = 8
EXPIRACAO_RECUPERACAO_MIN = 30

# Bloqueio simples contra tentativas repetidas de senha (em memória)
_falhas = {}
MAX_FALHAS = 5
BLOQUEIO_SEG = 300


def _hash_segredo(valor):
    return hashlib.sha256((valor or "").encode("utf-8")).hexdigest()


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
            perms = {p for p in TODAS_PERMISSOES if p not in ("cancelar_estornar", "gerenciar_modelos")}
        elif perfil == "consulta":
            perms = {"ver_painel", "ver_pedidos", "ver_epis", "ver_fornecedores", "ver_colaboradores", "ver_movimentos", "ver_relatorios"}
        elif perfil == "gestor":
            perms = {"solicitar_fora_funcao"}
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
    if endpoint in ("pedidos.integracao", "pedidos.config_sla"):
        return "__admin__"
    if endpoint == "pedidos.personalizar_formulario" or endpoint.startswith("pedidos.form_"):
        return ("gerenciar_pedidos", "editar_epis")
    if endpoint == "pedidos.solicitar_gestor":
        return "solicitar_fora_funcao"
    if endpoint in ("fichas.detalhe", "fichas.imprimir", "fichas.anexo"):
        return ("ver_pedidos", "ver_colaboradores")
    if endpoint == "fichas.relatorio_colaborador":
        return "ver_colaboradores"
    if endpoint.startswith("fichas."):
        return "gerenciar_pedidos"
    if endpoint.startswith("modelos."):
        return "gerenciar_modelos"
    if endpoint in ("pedidos.lista", "pedidos.detalhe", "pedidos.exportar_csv", "pedidos.novos"):
        return "ver_pedidos"
    if endpoint.startswith("pedidos."):
        return "gerenciar_pedidos"
    if endpoint in ("cadastros.epis_lista", "cadastros.epi_detalhe", "cadastros.epis_csv", "cadastros.api_caepi"):
        return "ver_epis"
    if endpoint in ("cadastros.epi_novo", "cadastros.epi_editar", "cadastros.epis_sincronizar_caepi"):
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
    # Obras seguem as permissões de Colaboradores
    if endpoint == "cadastros.obras_lista":
        return "ver_colaboradores"
    if endpoint.startswith("cadastros.obra_"):
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
        ("solicitar_fora_funcao", "pedidos.solicitar_gestor"),
    ]
    for perm, ep in ordem:
        if usuario_tem_permissao(perm):
            return url_for(ep)
    return url_for("pedidos.solicitar")


def _eh_so_gestor(usuario, perms):
    """Usuário que só tem acesso ao formulário do gestor (perfil Gestor padrão)."""
    return bool(usuario) and usuario["perfil"] != "admin" and set(perms or ()) == {"solicitar_fora_funcao"}


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
        if request.endpoint == "pedidos.solicitar_gestor":
            return redirect(url_for("auth.login_gestor"))
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
        nome = limpar(request.form.get("nome"))
        login = limpar(request.form.get("login"))
        email = (limpar(request.form.get("email")) or "").lower() or None
        senha = request.form.get("senha", "")
        erro = _senha_valida(senha, request.form.get("confirmacao", ""))
        if not nome or not login:
            erro = "Informe nome e login."
        elif email and not email_service.email_valido(email):
            erro = "Informe um endereço de e-mail válido."
        if erro:
            flash(erro, "erro")
        else:
            db.execute(
                "INSERT INTO usuarios (nome, login, email, receber_notificacoes, senha_hash, perfil) VALUES (?, ?, ?, 1, ?, 'admin')",
                (nome, login, email, generate_password_hash(senha)),
            )
            flash("Administrador criado. Entre com o login e a senha cadastrados.", "ok")
            return redirect(url_for("auth.login"))
    return render_template("setup.html")


def _verificar_credenciais():
    """Confere login e senha do formulário. Devolve (usuario, chave_bloqueio, bloqueado)."""
    login_ = (request.form.get("login") or "").strip()
    chave = (request.remote_addr, login_.lower())
    tentativas, ate = _falhas.get(chave, (0, 0))
    if tentativas >= MAX_FALHAS and time.time() < ate:
        return None, chave, True
    usuario = get_db().execute(
        "SELECT * FROM usuarios WHERE login = ? COLLATE NOCASE AND ativo = 1", (login_,)
    ).fetchone()
    if usuario and check_password_hash(usuario["senha_hash"], request.form.get("senha", "")):
        return usuario, chave, False
    _falhas[chave] = (tentativas + 1, time.time() + BLOQUEIO_SEG)
    return None, chave, False


def _abrir_sessao(usuario, chave):
    _falhas.pop(chave, None)
    session.clear()
    session.permanent = True
    session["uid"] = usuario["id"]
    g.usuario = usuario
    g.permissoes_usuario = permissoes_do_perfil(usuario["perfil"], get_db())


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.usuario:
        return redirect(primeira_rota_permitida())
    if request.method == "POST":
        usuario, chave, bloqueado = _verificar_credenciais()
        if bloqueado:
            flash("Muitas tentativas. Aguarde alguns minutos e tente novamente.", "erro")
            return render_template("login.html"), 429
        if usuario:
            _abrir_sessao(usuario, chave)
            destino = request.args.get("next") or ""
            if not destino.startswith("/") or destino.startswith("//"):
                destino = primeira_rota_permitida()
            return redirect(destino)
        flash("Login ou senha incorretos.", "erro")
    return render_template("login.html")


@bp.route("/login/gestor", methods=["GET", "POST"])
def login_gestor():
    """Login separado do gestor: só entra quem tem acesso ao formulário do gestor."""
    if g.usuario and usuario_tem_permissao("solicitar_fora_funcao"):
        return redirect(url_for("pedidos.solicitar_gestor"))
    if request.method == "POST":
        usuario, chave, bloqueado = _verificar_credenciais()
        if bloqueado:
            flash("Muitas tentativas. Aguarde alguns minutos e tente novamente.", "erro")
            return render_template("login_gestor.html"), 429
        if usuario and "solicitar_fora_funcao" not in permissoes_do_perfil(usuario["perfil"]):
            flash("Este usuário não tem acesso ao formulário do gestor.", "erro")
        elif usuario:
            _abrir_sessao(usuario, chave)
            return redirect(url_for("pedidos.solicitar_gestor"))
        else:
            flash("Login ou senha incorretos.", "erro")
    return render_template("login_gestor.html")


# --------------------------------------------------------- Esqueceu a senha? / Redefinir senha

@bp.route("/esqueci-senha", methods=["GET", "POST"])
def esqueci_senha():
    """Passo 1 da recuperação de senha: pede o login do usuário e envia o código/link para o e-mail."""
    origem = (request.args.get("origem") or request.form.get("origem") or "").strip()
    login_digitado = (request.args.get("login") or "").strip()

    if request.method == "POST":
        login_digitado = (limpar(request.form.get("login")) or "").strip()
        if not login_digitado:
            flash("Informe o seu login de usuário.", "erro")
            return render_template("esqueci_senha.html", login=login_digitado, origem=origem)

        db = get_db()
        usuario = db.execute(
            "SELECT * FROM usuarios WHERE login = ? COLLATE NOCASE AND ativo = 1",
            (login_digitado,),
        ).fetchone()

        if not usuario:
            flash("Usuário não encontrado ou inativo. Verifique o login informado.", "erro")
            return render_template("esqueci_senha.html", login=login_digitado, origem=origem)

        email_cad = (usuario["email"] or "").strip()
        if not email_service.email_valido(email_cad):
            flash(
                "Este usuário ainda não possui um e-mail cadastrado. "
                "Peça ao administrador do sistema para cadastrar seu e-mail na aba Usuários.",
                "erro",
            )
            return render_template("esqueci_senha.html", login=login_digitado, origem=origem)

        # Invalida códigos anteriores ainda não usados deste usuário
        agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        expira_str = (datetime.now() + timedelta(minutes=EXPIRACAO_RECUPERACAO_MIN)).strftime("%Y-%m-%d %H:%M:%S")
        db.execute(
            "UPDATE recuperacao_senha SET usado_em = ? WHERE usuario_id = ? AND usado_em IS NULL",
            (agora_str, usuario["id"]),
        )

        token = secrets.token_urlsafe(24)
        codigo = f"{secrets.randbelow(900000) + 100000}"
        db.execute(
            """INSERT INTO recuperacao_senha
               (usuario_id, token_hash, codigo_hash, expira_em, ip_solicitante)
               VALUES (?, ?, ?, ?, ?)""",
            (
                usuario["id"],
                _hash_segredo(token),
                _hash_segredo(codigo),
                expira_str,
                request.remote_addr or "",
            ),
        )

        ok, erro_envio = email_service.enviar_email_recuperacao_senha(usuario, token, codigo)
        email_masc = email_service.mascarar_email(email_cad)
        if ok:
            flash(
                f"Enviamos as instruções e o código de recuperação para o e-mail cadastrado: {email_masc}",
                "ok",
            )
        else:
            flash(
                f"Solicitação registrada para o e-mail {email_masc}, mas houve um alerta no disparo ({erro_envio}). "
                "Verifique com o administrador caso o e-mail não chegue em instantes.",
                "erro",
            )
        return redirect(url_for("auth.redefinir_senha", login=usuario["login"], origem=origem))

    return render_template("esqueci_senha.html", login=login_digitado, origem=origem)


@bp.route("/redefinir-senha", methods=["GET", "POST"])
@bp.route("/redefinir-senha/<token>", methods=["GET", "POST"], endpoint="redefinir_senha_token")
def redefinir_senha(token=None):
    """Passo 2 da recuperação de senha: exibe o e-mail mascarado (2 letras iniciais e 2 finais)
    e permite redefinir a senha via link direto (token) ou código de 6 dígitos enviado por e-mail.
    """
    db = get_db()
    origem = (request.args.get("origem") or request.form.get("origem") or "").strip()
    token = (token or request.form.get("token") or "").strip()
    login_param = (request.args.get("login") or request.form.get("login") or "").strip()

    agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    usuario = None
    reg_token = None

    if token:
        reg_token = db.execute(
            """SELECT r.*, u.login, u.nome, u.email, u.perfil
               FROM recuperacao_senha r
               JOIN usuarios u ON u.id = r.usuario_id
               WHERE r.token_hash = ? AND r.usado_em IS NULL AND r.expira_em >= ? AND u.ativo = 1""",
            (_hash_segredo(token), agora_str),
        ).fetchone()
        if not reg_token:
            flash("Este link de recuperação de senha é inválido ou já expirou. Solicite um novo código.", "erro")
            return redirect(url_for("auth.esqueci_senha", origem=origem))
        login_param = reg_token["login"]
        usuario = db.execute("SELECT * FROM usuarios WHERE id = ?", (reg_token["usuario_id"],)).fetchone()
    elif login_param:
        usuario = db.execute(
            "SELECT * FROM usuarios WHERE login = ? COLLATE NOCASE AND ativo = 1",
            (login_param,),
        ).fetchone()

    email_mascarado = email_service.mascarar_email(usuario["email"]) if (usuario and usuario["email"]) else ""

    if request.method == "POST":
        senha_nova = request.form.get("senha", "")
        confirmacao = request.form.get("confirmacao", "")
        codigo_digitado = (request.form.get("codigo") or "").strip()

        erro = _senha_valida(senha_nova, confirmacao)
        registro_valido = reg_token

        if not erro and not registro_valido:
            if not usuario:
                erro = "Informe um login de usuário válido."
            elif not codigo_digitado:
                erro = "Digite o código de 6 dígitos enviado para o seu e-mail."
            else:
                registro_valido = db.execute(
                    """SELECT * FROM recuperacao_senha
                       WHERE usuario_id = ? AND codigo_hash = ?
                         AND usado_em IS NULL AND expira_em >= ?
                       ORDER BY id DESC LIMIT 1""",
                    (usuario["id"], _hash_segredo(codigo_digitado), agora_str),
                ).fetchone()
                if not registro_valido:
                    erro = "Código de verificação incorreto ou expirado. Verifique o e-mail ou solicite um novo código."

        if erro:
            flash(erro, "erro")
        else:
            uid_alvo = registro_valido["usuario_id"]
            db.execute(
                "UPDATE usuarios SET senha_hash = ? WHERE id = ?",
                (generate_password_hash(senha_nova), uid_alvo),
            )
            db.execute(
                "UPDATE recuperacao_senha SET usado_em = ? WHERE usuario_id = ? AND usado_em IS NULL",
                (agora_str, uid_alvo),
            )
            u_alvo = db.execute("SELECT * FROM usuarios WHERE id = ?", (uid_alvo,)).fetchone()
            if u_alvo:
                _falhas.pop((request.remote_addr, u_alvo["login"].lower()), None)
            flash("Sua senha foi redefinida com sucesso! Entre com a nova senha.", "ok")
            perms_alvo = permissoes_do_perfil(u_alvo["perfil"], db) if u_alvo else set()
            if origem == "gestor" or _eh_so_gestor(u_alvo, perms_alvo):
                return redirect(url_for("auth.login_gestor"))
            return redirect(url_for("auth.login"))

    return render_template(
        "redefinir_senha.html",
        token=token,
        login=login_param,
        email_mascarado=email_mascarado,
        origem=origem,
        SENHA_MIN=SENHA_MIN,
    )


@bp.post("/sair")
def sair():
    so_gestor = _eh_so_gestor(g.get("usuario"), g.get("permissoes_usuario"))
    session.clear()
    return redirect(url_for("auth.login_gestor" if so_gestor else "auth.login"))


@bp.route("/meu-perfil", methods=["GET", "POST"])
@bp.route("/minha-senha", methods=["GET", "POST"])
def conta_endereco_antigo():
    # 308 mantém o método, para não perder um formulário aberto no endereço antigo
    return redirect(url_for("auth.conta"), code=308)


@bp.post("/conta/aparelhos/<int:aparelho_id>/remover")
def conta_remover_aparelho(aparelho_id):
    get_db().execute("DELETE FROM push_inscricoes WHERE id = ? AND usuario_id = ?", (aparelho_id, g.usuario["id"]))
    flash("Aparelho removido das notificações.", "ok")
    return redirect(url_for("auth.conta"))


@bp.post("/conta/aparelhos/<int:aparelho_id>/apelido")
def conta_apelidar_aparelho(aparelho_id):
    apelido = (limpar(request.form.get("apelido")) or "")[:40] or None
    get_db().execute("UPDATE push_inscricoes SET apelido = ? WHERE id = ? AND usuario_id = ?",
                     (apelido, aparelho_id, g.usuario["id"]))
    flash("Apelido do aparelho salvo." if apelido else "Apelido do aparelho removido.", "ok")
    return redirect(url_for("auth.conta"))


@bp.route("/conta", methods=["GET", "POST"])
def conta():
    db = get_db()
    if request.method == "POST":
        novo_login = limpar(request.form.get("login")) or g.usuario["login"]
        novo_email = (limpar(request.form.get("email")) or "").lower() or None
        receber_notif = 1 if request.form.get("receber_notificacoes") else 0
        senha_nova = request.form.get("senha", "")
        remover_foto = request.form.get("remover_foto") == "1"
        altera_acesso = (
            bool(senha_nova)
            or novo_login != g.usuario["login"]
            or novo_email != ((g.usuario["email"] or "").lower() or None)
        )

        if altera_acesso and not check_password_hash(g.usuario["senha_hash"], request.form.get("atual", "")):
            flash("Para alterar login, e-mail ou senha, informe corretamente sua senha atual.", "erro")
        elif not novo_login:
            flash("Informe um login válido.", "erro")
        elif novo_email and not email_service.email_valido(novo_email):
            flash("Informe um endereço de e-mail válido.", "erro")
        else:
            erro = _senha_valida(senha_nova, request.form.get("confirmacao", "")) if senha_nova else None
            if erro:
                flash(erro, "erro")
            else:
                try:
                    foto_final = g.usuario["foto_perfil"] if "foto_perfil" in g.usuario.keys() else None
                    if remover_foto:
                        foto_final = None
                    else:
                        arq_foto = request.files.get("foto_perfil")
                        b64_foto = request.form.get("foto_perfil_b64")
                        if (arq_foto and getattr(arq_foto, "filename", "")) or (b64_foto and b64_foto.strip()):
                            foto_final = pedidos.processar_imagem_epi(arq_foto, b64_foto)

                    db.execute(
                        "UPDATE usuarios SET login = ?, email = ?, receber_notificacoes = ?, foto_perfil = ? WHERE id = ?",
                        (novo_login, novo_email, receber_notif, foto_final, g.usuario["id"]),
                    )
                    if senha_nova:
                        db.execute("UPDATE usuarios SET senha_hash = ? WHERE id = ?",
                                   (generate_password_hash(senha_nova), g.usuario["id"]))
                    g.usuario = db.execute("SELECT * FROM usuarios WHERE id = ?", (g.usuario["id"],)).fetchone()
                    flash("Conta atualizada com sucesso!", "ok")
                    return redirect(url_for("auth.conta"))
                except sqlite3.IntegrityError:
                    flash("Esse login já está em uso por outro usuário.", "erro")
                except ErroValidacao as e:
                    flash(str(e), "erro")
    aparelhos = db.execute(
        "SELECT id, endpoint, user_agent, apelido, criado_em FROM push_inscricoes WHERE usuario_id = ? ORDER BY id DESC",
        (g.usuario["id"],),
    ).fetchall()
    return render_template(
        "conta.html",
        aparelhos=aparelhos,
        nome_perfil=obter_perfis_dict(db).get(g.usuario["perfil"], g.usuario["perfil"]),
        catalogo_permissoes=CATALOGO_PERMISSOES,
    )


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
    elif ep.startswith(("cadastros.colab", "cadastros.obra")) or ep == "cadastros.cargos_padrao":
        pode_editar_ctx = "editar_colaboradores" in perms
    elif ep.startswith(("pedidos.", "fichas.")):
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
        "pode_personalizar_forms": ("gerenciar_pedidos" in perms) or ("editar_epis" in perms) or (perfil == "admin"),
        "pode_movimentar": "registrar_movimentos" in perms,
        "pode_cancelar": "cancelar_estornar" in perms,
        "pode_solicitar_gestor": "solicitar_fora_funcao" in perms,
        "so_gestor": _eh_so_gestor(u, perms),
        "eh_admin": perfil == "admin",
        "mascarar_email": email_service.mascarar_email,
    }
