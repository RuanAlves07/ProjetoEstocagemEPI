"""Serviço de disparo de e-mails do sistema de Controle de Estoque de EPIs.

Suporta:
1. Microsoft 365 Graph API (padrão corporativo ABecker já pré-configurado, podendo ser
   sobrescrito por variáveis de ambiente EPI_GRAPH_*).
2. Servidor SMTP (EPI_MAIL_MODO=smtp e variáveis EPI_SMTP_*).

Funcionalidades principais:
- Mascaramento de e-mail exibindo apenas as 2 letras iniciais e 2 letras finais.
- Recuperação de senha ("Esqueceu a senha?") com link seguro + código de 6 dígitos.
- Notificações automáticas do andamento dos pedidos de EPI (em qual lista/fila o pedido
  está, etapa do fluxo, itens solicitados/separados, ressalvas e alertas de estoque mínimo).
"""
import html as _html
import json
import os
import re
import smtplib
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr

from flask import current_app, url_for

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Credenciais corporativas padrão ABecker (Microsoft 365 Graph API)
DEFAULT_GRAPH_TENANT_ID = "c5e96684-9554-42df-a3e9-f71e5ae85093"
DEFAULT_GRAPH_CLIENT_ID = "f2ea9fbe-6434-44c6-899a-5ccf8f971de3"
DEFAULT_GRAPH_CLIENT_SECRET = "RV~8Q~vTv6cy8ve~72Qk2jtyoOBXQoOAWlDnhc6t"
DEFAULT_MAIL_REMETENTE = "meajuda@abecker.com.br"
FALLBACK_MAIL_REMETENTE = "suporte@abecker.com.br"

_cache_token = {"token": None, "expira": 0.0}
_trava_token = threading.Lock()


# ------------------------------------------------------------------ utilitários

def email_valido(email):
    email = (email or "").strip()
    return bool(email) and len(email) <= 254 and bool(EMAIL_RE.match(email))


def mascarar_email(email):
    """Mostra somente as 2 primeiras letras e as 2 últimas letras do e-mail (mantendo o @).

    Exemplo:
        ruan.alves@abecker.com.br -> ru********@*************br
    """
    email = (email or "").strip()
    if not email:
        return ""
    n = len(email)
    if n <= 4:
        return email[0] + ("*" * max(0, n - 1))
    saida = []
    for i, ch in enumerate(email):
        if i < 2 or i >= n - 2 or ch == "@":
            saida.append(ch)
        else:
            saida.append("*")
    return "".join(saida)


def _cfg(nome, padrao=""):
    val = os.environ.get(nome)
    if val is not None and val.strip() != "":
        return val.strip()
    return padrao


def modo_envio():
    modo = _cfg("EPI_MAIL_MODO", "graph").lower()
    if modo in ("graph", "smtp", "desativado", "off", "none"):
        return "" if modo in ("desativado", "off", "none") else modo
    return "graph"


def _credenciais_graph():
    tenant = _cfg("EPI_GRAPH_TENANT_ID", DEFAULT_GRAPH_TENANT_ID)
    client_id = _cfg("EPI_GRAPH_CLIENT_ID", DEFAULT_GRAPH_CLIENT_ID)
    client_secret = _cfg("EPI_GRAPH_CLIENT_SECRET", DEFAULT_GRAPH_CLIENT_SECRET)
    remetente = _cfg("EPI_MAIL_REMETENTE", DEFAULT_MAIL_REMETENTE)
    return tenant, client_id, client_secret, remetente


def configurado():
    """True quando há um modo de envio ativo com as credenciais mínimas preenchidas."""
    modo = modo_envio()
    if modo == "graph":
        tenant, client_id, client_secret, remetente = _credenciais_graph()
        return all((tenant, client_id, client_secret, remetente))
    if modo == "smtp":
        return bool(_cfg("EPI_SMTP_HOST"))
    return False


def descricao_configuracao():
    """Texto curto para exibir aos administradores na tela de Usuários."""
    if current_app and current_app.config.get("TESTING"):
        return "Modo de teste ativo (disparos registrados em memória)."
    if not configurado():
        return "Envio de e-mail desativado."
    if modo_envio() == "graph":
        _, _, _, remetente = _credenciais_graph()
        return f"Ativo via Microsoft 365 (Graph API) · remetente {remetente}"
    return f"Ativo via SMTP ({_cfg('EPI_SMTP_HOST')}) · remetente {_cfg('EPI_MAIL_REMETENTE') or _cfg('EPI_SMTP_USUARIO')}"


def url_externa(endpoint, **valores):
    """URL absoluta para links enviados por e-mail. Respeita EPI_BASE_URL quando definida."""
    base = _cfg("EPI_BASE_URL").rstrip("/")
    if base:
        return base + url_for(endpoint, **valores)
    try:
        return url_for(endpoint, _external=True, **valores)
    except RuntimeError:
        return "/"


# ------------------------------------------------------------------ transportes

def _token_graph():
    tenant, client_id, client_secret, _ = _credenciais_graph()
    with _trava_token:
        if _cache_token["token"] and time.time() < _cache_token["expira"] - 60:
            return _cache_token["token"]
        corpo = urllib.parse.urlencode({
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        }).encode("utf-8")
        url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
        req = urllib.request.Request(url, data=corpo, method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            dados = json.loads(r.read().decode("utf-8"))
        _cache_token["token"] = dados["access_token"]
        _cache_token["expira"] = time.time() + int(dados.get("expires_in", 3600))
        return _cache_token["token"]


def _disparar_graph_remetente(remetente, destinatarios, assunto, html, token):
    payload = {
        "message": {
            "subject": assunto,
            "body": {"contentType": "HTML", "content": html},
            "toRecipients": [{"emailAddress": {"address": d}} for d in destinatarios],
        },
        "saveToSentItems": False,
    }
    req = urllib.request.Request(
        f"https://graph.microsoft.com/v1.0/users/{urllib.parse.quote(remetente)}/sendMail",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        if r.status not in (200, 202):
            raise RuntimeError(f"Graph respondeu HTTP {r.status}")


def _enviar_graph(destinatarios, assunto, html):
    _, _, _, remetente = _credenciais_graph()
    token = _token_graph()
    remetentes_tentativa = [remetente]
    if FALLBACK_MAIL_REMETENTE and FALLBACK_MAIL_REMETENTE.lower() != remetente.lower():
        remetentes_tentativa.append(FALLBACK_MAIL_REMETENTE)

    ultimo_erro = None
    for rem in remetentes_tentativa:
        try:
            _disparar_graph_remetente(rem, destinatarios, assunto, html, token)
            return
        except urllib.error.HTTPError as e:
            detalhe = e.read().decode("utf-8", errors="replace")[:400]
            ultimo_erro = RuntimeError(f"Graph HTTP {e.code} ({rem}): {detalhe}")
        except Exception as e:  # noqa: BLE001
            ultimo_erro = e
    raise ultimo_erro or RuntimeError("Falha desconhecida ao enviar via Microsoft Graph.")


def _enviar_smtp(destinatarios, assunto, html, texto):
    msg = EmailMessage()
    remetente = _cfg("EPI_MAIL_REMETENTE") or _cfg("EPI_SMTP_USUARIO") or DEFAULT_MAIL_REMETENTE
    msg["From"] = formataddr((_cfg("EPI_MAIL_NOME", "Controle de EPIs - ABecker"), remetente))
    msg["To"] = ", ".join(destinatarios)
    msg["Subject"] = assunto
    msg.set_content(texto or "Abra este e-mail em um leitor compatível com HTML.")
    msg.add_alternative(html, subtype="html")

    host = _cfg("EPI_SMTP_HOST")
    porta = int(_cfg("EPI_SMTP_PORTA", "587") or 587)
    tls = _cfg("EPI_SMTP_TLS", "1").lower()
    cls = smtplib.SMTP_SSL if tls == "ssl" else smtplib.SMTP
    with cls(host, porta, timeout=25) as s:
        if tls in ("1", "true", "starttls"):
            s.starttls()
        if _cfg("EPI_SMTP_USUARIO"):
            s.login(_cfg("EPI_SMTP_USUARIO"), _cfg("EPI_SMTP_SENHA"))
        s.send_message(msg)


def _registrar(app, destinatarios, assunto, tipo, ok, erro):
    """Grava o resultado do envio na tabela emails_enviados para auditoria."""
    try:
        from .db import get_db
        with app.app_context():
            db = get_db()
            db.execute(
                "INSERT INTO emails_enviados (destinatarios, assunto, tipo, status, erro) VALUES (?, ?, ?, ?, ?)",
                (
                    ", ".join(destinatarios)[:1000],
                    assunto[:300],
                    tipo,
                    "enviado" if ok else "falha",
                    (erro or "")[:1000] or None,
                ),
            )
            if hasattr(db, "commit"):
                db.commit()
    except Exception:
        pass


def _texto_simples(html):
    texto = re.sub(r"<(br|/p|/tr|/h\d|/li|/div)[^>]*>", "\n", html, flags=re.I)
    texto = re.sub(r"<[^>]+>", "", texto)
    return re.sub(r"\n\s*\n+", "\n\n", texto).strip()


# ------------------------------------------------------------------ API base de envio

def enviar(destinatarios, assunto, html, tipo="geral"):
    """Envio síncrono. Devolve (ok, mensagem_erro)."""
    app = current_app._get_current_object()
    destinatarios = sorted({
        d.strip().lower()
        for d in (destinatarios or [])
        if email_valido((d or "").strip())
    })
    if not destinatarios:
        return False, "Nenhum destinatário com e-mail válido."

    if app.config.get("TESTING"):
        app.config.setdefault("EMAILS_ENVIADOS", []).append({
            "para": destinatarios,
            "assunto": assunto,
            "html": html,
            "tipo": tipo,
        })
        _registrar(app, destinatarios, assunto, tipo, True, None)
        return True, None

    if not configurado():
        _registrar(app, destinatarios, assunto, tipo, False, "Envio de e-mail não configurado.")
        return False, "Envio de e-mail não configurado no servidor."

    try:
        if modo_envio() == "graph":
            _enviar_graph(destinatarios, assunto, html)
        else:
            _enviar_smtp(destinatarios, assunto, html, _texto_simples(html))
        _registrar(app, destinatarios, assunto, tipo, True, None)
        return True, None
    except Exception as e:  # noqa: BLE001
        app.logger.warning("Falha ao enviar e-mail '%s': %s", assunto, e)
        _registrar(app, destinatarios, assunto, tipo, False, str(e))
        return False, str(e)


def enviar_em_segundo_plano(destinatarios, assunto, html, tipo="geral"):
    """Dispara o envio em uma thread separada para não bloquear a resposta HTTP."""
    app = current_app._get_current_object()
    destinatarios = sorted({
        d.strip().lower()
        for d in (destinatarios or [])
        if email_valido((d or "").strip())
    })
    if not destinatarios:
        return False, "Nenhum destinatário com e-mail válido."

    if app.config.get("TESTING"):
        return enviar(destinatarios, assunto, html, tipo)

    def _tarefa():
        with app.app_context():
            enviar(destinatarios, assunto, html, tipo)

    threading.Thread(target=_tarefa, daemon=True, name="envio-email-epi").start()
    return True, None


# ------------------------------------------------------------------ Layout HTML base

def _layout_email(titulo, subtitulo, cor_topo, conteudo_html, texto_botao=None, url_botao=None):
    botao_html = ""
    if texto_botao and url_botao:
        botao_html = f"""
        <div style="margin: 24px 0 8px; text-align: center;">
          <a href="{_html.escape(url_botao)}"
             style="display: inline-block; background-color: #0b4f6c; color: #ffffff;
                    text-decoration: none; font-weight: 700; font-size: 14px;
                    padding: 12px 26px; border-radius: 6px;">
            {_html.escape(texto_botao)}
          </a>
        </div>
        """

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <title>{_html.escape(titulo)}</title>
</head>
<body style="margin:0; padding:0; background-color:#f1f5f9; font-family:'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color:#1e293b;">
  <div style="max-width:640px; margin:24px auto; background:#ffffff; border-radius:10px; overflow:hidden; box-shadow:0 4px 14px rgba(15,23,42,0.08); border:1px solid #e2e8f0;">
    <div style="background:{cor_topo}; color:#ffffff; padding:20px 28px;">
      <div style="font-size:11px; text-transform:uppercase; letter-spacing:1px; opacity:0.85; font-weight:600;">
        ABecker · Controle de Estoque e Pedidos de EPIs
      </div>
      <h1 style="margin:6px 0 4px; font-size:20px; font-weight:700;">{_html.escape(titulo)}</h1>
      <div style="font-size:13px; opacity:0.92;">{_html.escape(subtitulo)}</div>
    </div>
    <div style="padding:24px 28px; font-size:14px; line-height:1.55;">
      {conteudo_html}
      {botao_html}
    </div>
    <div style="background:#f8fafc; border-top:1px solid #e2e8f0; padding:14px 28px; font-size:12px; color:#64748b; text-align:center;">
      Mensagem automática do Sistema de Controle de Estoque de EPIs · ABecker.
    </div>
  </div>
</body>
</html>"""


# ------------------------------------------------------------------ 1. Recuperação de Senha

def enviar_email_recuperacao_senha(usuario, token, codigo, url_redefinir=None):
    """Envia o link direto e o código de 6 dígitos para redefinição de senha."""
    email_dest = (usuario["email"] or "").strip()
    if not email_valido(email_dest):
        return False, "Usuário não possui um e-mail válido cadastrado."

    link = url_redefinir or url_externa("auth.redefinir_senha_token", token=token)
    nome = usuario["nome"] or usuario["login"]
    login = usuario["login"]

    conteudo = f"""
      <p style="margin-top:0;">Olá, <strong>{_html.escape(nome)}</strong>!</p>
      <p>Recebemos uma solicitação para redefinir a senha do usuário
         <code style="background:#f1f5f9; padding:2px 6px; border-radius:4px; font-weight:700;">{_html.escape(login)}</code>
         no sistema de Controle de Estoque de EPIs.</p>

      <div style="background:#f8fafc; border:1px solid #cbd5e1; border-radius:8px; padding:16px; margin:18px 0; text-align:center;">
        <div style="font-size:12px; color:#64748b; text-transform:uppercase; letter-spacing:0.6px; margin-bottom:6px;">
          Código de verificação (válido por 30 minutos)
        </div>
        <div style="font-size:28px; font-weight:800; letter-spacing:6px; color:#0f172a; font-family:monospace;">
          {_html.escape(codigo)}
        </div>
      </div>

      <p>Você pode digitar o código acima na tela de redefinição ou clicar diretamente no botão abaixo para escolher sua nova senha:</p>
      <p style="font-size:12px; color:#64748b; margin-top:18px;">
        Caso você não tenha solicitado esta alteração, ignore este e-mail — sua senha atual permanecerá a mesma.
      </p>
    """
    html = _layout_email(
        titulo="Recuperação de Senha",
        subtitulo=f"Solicitação de redefinição de senha para o login {login}",
        cor_topo="#0b4f6c",
        conteudo_html=conteudo,
        texto_botao="Redefinir Minha Senha Agora",
        url_botao=link,
    )
    assunto = f"[Estoque de EPIs] Recuperação de senha ({login})"
    return enviar([email_dest], assunto, html, tipo="recuperacao_senha")


# ------------------------------------------------------------------ 2. Notificações de Pedidos e Filas

MAPA_FILAS = {
    "novo": {
        "lista": "Novos pedidos",
        "cor": "#2563eb",
        "bg_badge": "#dbeafe",
        "fg_badge": "#1e40af",
        "etapa": 1,
        "descricao": "O pedido foi registrado e está na fila 'Novos pedidos' aguardando o início da separação pelo almoxarifado.",
    },
    "pendente": {
        "lista": "Pendentes (deixados para mais tarde)",
        "cor": "#d97706",
        "bg_badge": "#fef3c7",
        "fg_badge": "#92400e",
        "etapa": 1,
        "descricao": "O pedido foi movido para a lista 'Pendentes (deixados para mais tarde)' e aguarda retomada da separação.",
    },
    "aguardando_estoque": {
        "lista": "Pendências aguardando estoque",
        "cor": "#dc2626",
        "bg_badge": "#fee2e2",
        "fg_badge": "#991b1b",
        "etapa": 1,
        "descricao": "Este pedido complementar contém itens que faltaram no atendimento anterior e está na fila 'Pendências aguardando estoque'.",
    },
    "em_separacao": {
        "lista": "Em separação",
        "cor": "#0284c7",
        "bg_badge": "#e0f2fe",
        "fg_badge": "#075985",
        "etapa": 2,
        "descricao": "O almoxarifado iniciou o atendimento! O pedido está na lista 'Em separação' sendo conferido.",
    },
    "pronto": {
        "lista": "Prontos para retirada",
        "cor": "#16a34a",
        "bg_badge": "#dcfce7",
        "fg_badge": "#166534",
        "etapa": 3,
        "descricao": "A separação foi concluída e a saída foi lançada no estoque! O pedido está na lista 'Prontos para retirada'.",
    },
    "retirado": {
        "lista": "Retirados (Histórico de entregas)",
        "cor": "#0f766e",
        "bg_badge": "#ccfbf1",
        "fg_badge": "#115e59",
        "etapa": 4,
        "descricao": "Os equipamentos foram retirados/entregues e o pedido foi arquivado na lista 'Retirados'.",
    },
    "cancelado": {
        "lista": "Cancelados (Histórico)",
        "cor": "#64748b",
        "bg_badge": "#f1f5f9",
        "fg_badge": "#334155",
        "etapa": 0,
        "descricao": "O pedido foi cancelado e movido para o histórico de 'Cancelados'.",
    },
}


def info_fila_pedido(pedido):
    """Retorna os metadados da lista/fila em que o pedido se encontra atualmente."""
    status = (pedido["status"] or "novo").strip()
    eh_complemento = bool(pedido["pedido_origem_id"])
    if status in ("novo", "pendente") and eh_complemento:
        return MAPA_FILAS["aguardando_estoque"]
    return MAPA_FILAS.get(status, MAPA_FILAS["novo"])


def _render_stepper_html(status, eh_complemento=False):
    if status == "cancelado":
        return """
        <div style="margin:16px 0; padding:10px 14px; background:#fef2f2; border-left:4px solid #ef4444; border-radius:4px; color:#991b1b; font-weight:600;">
          Fluxo encerrado: Pedido Cancelado
        </div>
        """

    etapas = [
        (1, "Pendência de Estoque" if eh_complemento else ("Pendente" if status == "pendente" else "Novos pedidos")),
        (2, "Em separação"),
        (3, "Prontos p/ retirada"),
        (4, "Retirado"),
    ]
    etapa_atual = {"novo": 1, "pendente": 1, "em_separacao": 2, "pronto": 3, "retirado": 4}.get(status, 1)

    celulas = []
    for num, rotulo in etapas:
        concluida = num < etapa_atual or status == "retirado"
        atual = num == etapa_atual and status != "retirado"
        if atual:
            bg, fg, borda = "#0b4f6c", "#ffffff", "#0b4f6c"
        elif concluida:
            bg, fg, borda = "#dcfce7", "#166534", "#86efac"
        else:
            bg, fg, borda = "#f8fafc", "#94a3b8", "#e2e8f0"

        celulas.append(f"""
          <td style="width:25%; text-align:center; padding:6px 4px; background:{bg}; color:{fg};
                     border:1px solid {borda}; border-radius:6px; font-size:11px; font-weight:{'700' if (atual or concluida) else '500'};">
            <div>{num}. {_html.escape(rotulo)}</div>
          </td>
        """)

    return f"""
    <table role="presentation" style="width:100%; border-collapse:separate; border-spacing:4px; margin:14px 0 18px;">
      <tr>{"".join(celulas)}</tr>
    </table>
    """


def listar_destinatarios_pedido(db, pedido):
    """Reúne os e-mails que devem receber notificações sobre este pedido:
    - Usuários ativos com e-mail válido e receber_notificacoes = 1
    - E-mail específico do solicitante gravado no pedido (solicitante_email)
    - E-mail do usuário logado que abriu o pedido (solicitante_usuario_id), se houver
    """
    destinatarios = set()

    linhas = db.execute(
        "SELECT email FROM usuarios WHERE ativo = 1 AND receber_notificacoes = 1 AND email IS NOT NULL AND email != ''"
    ).fetchall()
    for r in linhas:
        if email_valido(r["email"]):
            destinatarios.add(r["email"].strip().lower())

    try:
        sol_email = pedido["solicitante_email"]
    except (KeyError, IndexError):
        sol_email = None
    if email_valido(sol_email):
        destinatarios.add(sol_email.strip().lower())

    try:
        sol_uid = pedido["solicitante_usuario_id"]
    except (KeyError, IndexError):
        sol_uid = None
    if sol_uid:
        u = db.execute("SELECT email, ativo FROM usuarios WHERE id = ?", (sol_uid,)).fetchone()
        if u and u["ativo"] and email_valido(u["email"]):
            destinatarios.add(u["email"].strip().lower())

    return sorted(destinatarios)


def notificar_evento_pedido(db, pedido_id, evento, ator_nome=None, extra_info=None):
    """Monta e dispara o e-mail de acompanhamento sempre que um pedido é criado ou muda de lista/status.

    Eventos suportados:
      - 'criado'       -> entrou na lista 'Novos pedidos'
      - 'em_separacao' -> entrou na lista 'Em separação'
      - 'pendente'     -> entrou na lista 'Pendentes (deixados para mais tarde)'
      - 'finalizado'   -> entrou na lista 'Prontos para retirada' (com eventual complemento e alerta de estoque mínimo)
      - 'retirado'     -> entrou na lista 'Retirados'
      - 'cancelado'    -> entrou na lista 'Cancelados'
    """
    pedido = db.execute(
        """
        SELECT p.*, c.nome AS colaborador_nome, c.setor AS colaborador_setor, c.funcao AS colaborador_funcao,
               u_ini.nome AS iniciado_por_nome, u_fin.nome AS finalizado_por_nome,
               u_ent.nome AS entregue_por_nome, u_can.nome AS cancelado_por_nome
        FROM pedidos p
        LEFT JOIN colaboradores c ON c.id = p.colaborador_id
        LEFT JOIN usuarios u_ini ON u_ini.id = p.iniciado_por
        LEFT JOIN usuarios u_fin ON u_fin.id = p.finalizado_por
        LEFT JOIN usuarios u_ent ON u_ent.id = p.entregue_por
        LEFT JOIN usuarios u_can ON u_can.id = p.cancelado_por
        WHERE p.id = ?
        """,
        (pedido_id,),
    ).fetchone()
    if not pedido:
        return False, "Pedido não encontrado."

    destinatarios = listar_destinatarios_pedido(db, pedido)
    if not destinatarios:
        return False, "Nenhum destinatário configurado para receber notificações."

    itens = db.execute(
        """
        SELECT i.*, e.nome AS epi_nome, e.tamanho AS epi_tamanho, e.ca AS epi_ca,
               e.unidade AS epi_unidade, e.estoque_minimo AS epi_estoque_minimo
        FROM pedido_itens i
        LEFT JOIN epis e ON e.id = i.epi_id
        WHERE i.pedido_id = ?
        ORDER BY i.id
        """,
        (pedido_id,),
    ).fetchall()

    fila = info_fila_pedido(pedido)
    eh_complemento = bool(pedido["pedido_origem_id"])

    titulos_evento = {
        "criado": f"Novo Pedido #{pedido_id} registrado",
        "em_separacao": f"Pedido #{pedido_id} entrou Em Separação",
        "pendente": f"Pedido #{pedido_id} movido para Pendentes",
        "finalizado": f"Pedido #{pedido_id} Pronto para Retirada",
        "retirado": f"Pedido #{pedido_id} Retirado / Entregue",
        "cancelado": f"Pedido #{pedido_id} Cancelado",
    }
    titulo = titulos_evento.get(evento, f"Atualização do Pedido #{pedido_id}")
    subtitulo = f"Lista atual: {fila['lista']} · Solicitante: {pedido['solicitante']}"

    stepper_html = _render_stepper_html(pedido["status"], eh_complemento=eh_complemento)

    # Tabela de itens do pedido
    linhas_itens = []
    for it in itens:
        nome_epi = it["epi_nome"] or it["texto_original"]
        if it["epi_nome"] and it["epi_tamanho"]:
            nome_epi = f"{it['epi_nome']} ({it['epi_tamanho']})"
        ca_txt = f"CA {it['epi_ca']}" if it["epi_ca"] else ""

        if it["ignorado"]:
            st_item = '<span style="color:#64748b;">Ignorado</span>'
        elif it["separado"]:
            st_item = '<span style="color:#166534; font-weight:700;">✓ Separado</span>'
        elif pedido["status"] in ("pronto", "retirado"):
            st_item = '<span style="color:#dc2626; font-weight:700;">✗ Faltou (Pendência)</span>'
        else:
            st_item = '<span style="color:#b45309;">Aguardando separação</span>'

        linhas_itens.append(f"""
          <tr>
            <td style="padding:8px 10px; border-bottom:1px solid #e2e8f0;">
              <strong>{_html.escape(nome_epi)}</strong>
              {f'<div style="font-size:11px; color:#64748b;">{_html.escape(ca_txt)} · campo: {_html.escape(it["coluna"])}</div>' if ca_txt or it["coluna"] else ''}
            </td>
            <td style="padding:8px 10px; border-bottom:1px solid #e2e8f0; text-align:center; font-weight:700;">
              {int(it["quantidade"])} {_html.escape(it["epi_unidade"] or "un")}
            </td>
            <td style="padding:8px 10px; border-bottom:1px solid #e2e8f0; text-align:right; font-size:12px;">
              {st_item}
            </td>
          </tr>
        """)

    tabela_itens_html = f"""
      <h3 style="margin:20px 0 8px; font-size:14px; color:#0f172a; text-transform:uppercase; letter-spacing:0.5px;">
        Itens do Pedido ({len(itens)})
      </h3>
      <table style="width:100%; border-collapse:collapse; font-size:13px; background:#ffffff; border:1px solid #e2e8f0; border-radius:6px; overflow:hidden;">
        <thead>
          <tr style="background:#f8fafc; color:#475569; text-align:left; font-size:11px; text-transform:uppercase;">
            <th style="padding:8px 10px; border-bottom:1px solid #e2e8f0;">Equipamento (EPI)</th>
            <th style="padding:8px 10px; border-bottom:1px solid #e2e8f0; text-align:center;">Qtd.</th>
            <th style="padding:8px 10px; border-bottom:1px solid #e2e8f0; text-align:right;">Situação</th>
          </tr>
        </thead>
        <tbody>
          {"".join(linhas_itens) or '<tr><td colspan="3" style="padding:10px; text-align:center; color:#64748b;">Nenhum item listado.</td></tr>'}
        </tbody>
      </table>
    """

    # Blocos adicionais (ressalva, pedido complementar, dados de retirada, cancelamento, estoque mínimo)
    blocos_extras = []

    if pedido["pedido_origem_id"]:
        blocos_extras.append(f"""
          <div style="margin-top:14px; padding:12px 14px; background:#fff7ed; border:1px solid #fed7aa; border-radius:6px; font-size:13px; color:#9a3412;">
            <strong>Pedido Complementar:</strong> originado dos itens pendentes do Pedido <strong>#{pedido['pedido_origem_id']}</strong>.
          </div>
        """)

    if pedido["ressalva_observacao"]:
        comp = db.execute("SELECT id FROM pedidos WHERE pedido_origem_id = ? ORDER BY id DESC LIMIT 1", (pedido_id,)).fetchone()
        comp_txt = (
            f"<br>Foi gerado automaticamente o <strong>Pedido Complementar #{comp['id']}</strong> na lista <strong>Pendências aguardando estoque</strong>."
            if comp else ""
        )
        blocos_extras.append(f"""
          <div style="margin-top:14px; padding:12px 14px; background:#fef3c7; border-left:4px solid #d97706; border-radius:4px; font-size:13px; color:#92400e;">
            <strong>Finalizado com Ressalva:</strong> {_html.escape(pedido['ressalva_observacao'])}
            {comp_txt}
          </div>
        """)

    if pedido["status"] == "retirado":
        blocos_extras.append(f"""
          <div style="margin-top:14px; padding:12px 14px; background:#f0fdf4; border:1px solid #bbf7d0; border-radius:6px; font-size:13px; color:#166534;">
            <strong>Dados da Retirada:</strong><br>
            Retirado por: <strong>{_html.escape(pedido['retirado_por_nome'] or '-')}</strong><br>
            Data/Hora: {_html.escape(pedido['retirado_em'] or '-')}<br>
            {f"Observações da entrega: {_html.escape(pedido['retirado_descricao'])}<br>" if pedido['retirado_descricao'] else ""}
            Entregue por: {_html.escape(pedido['entregue_por_nome'] or ator_nome or '-')}
          </div>
        """)

    if pedido["status"] == "cancelado":
        blocos_extras.append(f"""
          <div style="margin-top:14px; padding:12px 14px; background:#fef2f2; border:1px solid #fecaca; border-radius:6px; font-size:13px; color:#991b1b;">
            <strong>Motivo do Cancelamento:</strong> {_html.escape(pedido['cancelado_motivo'] or '-')}<br>
            Cancelado por: {_html.escape(pedido['cancelado_por_nome'] or ator_nome or '-')} em {_html.escape(pedido['cancelado_em'] or '-')}
          </div>
        """)

    # Ideia extra: Alerta inteligente de estoque mínimo ao finalizar separação
    if evento == "finalizado":
        alertas_estoque = _verificar_estoque_minimo_itens(db, itens)
        if alertas_estoque:
            itens_alerta_li = "".join(
                f"<li><strong>{_html.escape(a['nome'])}</strong>: saldo atual <strong>{a['saldo']} {a['unidade']}</strong> "
                f"(mínimo recomendado: {a['minimo']} {a['unidade']})</li>"
                for a in alertas_estoque
            )
            blocos_extras.append(f"""
              <div style="margin-top:14px; padding:12px 14px; background:#fff1f2; border-left:4px solid #e11d48; border-radius:4px; font-size:13px; color:#9f1239;">
                <strong>⚠️ Alerta de Reposição de Estoque:</strong>
                Após a baixa deste pedido, os seguintes EPIs atingiram ou ficaram abaixo do estoque mínimo:
                <ul style="margin:6px 0 0; padding-left:20px;">{itens_alerta_li}</ul>
              </div>
            """)

    responsavel_acao = (
        ator_nome
        or pedido["entregue_por_nome"]
        or pedido["finalizado_por_nome"]
        or pedido["iniciado_por_nome"]
        or pedido["cancelado_por_nome"]
        or "Sistema / Formulário"
    )

    conteudo = f"""
      <div style="background:{fila['bg_badge']}; color:{fila['fg_badge']}; padding:12px 16px; border-radius:8px; margin-bottom:14px;">
        <div style="font-size:11px; text-transform:uppercase; letter-spacing:0.6px; opacity:0.85;">
          Lista / Fila Atual no Painel de Pedidos
        </div>
        <div style="font-size:17px; font-weight:800; margin-top:2px;">
          📋 {_html.escape(fila['lista'])}
        </div>
        <div style="font-size:12px; margin-top:4px;">
          {_html.escape(fila['descricao'])}
        </div>
      </div>

      {stepper_html}

      <table style="width:100%; border-collapse:collapse; font-size:13px; margin-bottom:10px;">
        <tr>
          <td style="padding:5px 0; color:#64748b; width:36%;">Número do Pedido:</td>
          <td style="padding:5px 0; font-weight:700;">#{pedido_id}</td>
        </tr>
        <tr>
          <td style="padding:5px 0; color:#64748b;">Solicitante / Colaborador:</td>
          <td style="padding:5px 0; font-weight:700;">
            {_html.escape(pedido['colaborador_nome'] or pedido['solicitante'])}
            {f" <span style='color:#64748b; font-weight:400;'>({_html.escape(pedido['colaborador_funcao'] or pedido['colaborador_setor'] or '')})</span>" if (pedido['colaborador_funcao'] or pedido['colaborador_setor']) else ""}
          </td>
        </tr>
        <tr>
          <td style="padding:5px 0; color:#64748b;">Obra / Local de Destino:</td>
          <td style="padding:5px 0; font-weight:600;">{_html.escape(pedido['local_destino'] or 'Não informado')}</td>
        </tr>
        <tr>
          <td style="padding:5px 0; color:#64748b;">Motivo da Solicitação:</td>
          <td style="padding:5px 0;">{_html.escape(pedido['motivo'] or '-')}</td>
        </tr>
        {f'<tr><td style="padding:5px 0; color:#64748b;">Observações:</td><td style="padding:5px 0;">{_html.escape(pedido["observacao"])}</td></tr>' if pedido['observacao'] else ''}
        <tr>
          <td style="padding:5px 0; color:#64748b;">Recebido em:</td>
          <td style="padding:5px 0;">{_html.escape(pedido['recebido_em'] or '-')}</td>
        </tr>
        <tr>
          <td style="padding:5px 0; color:#64748b;">Atualizado por:</td>
          <td style="padding:5px 0;">{_html.escape(responsavel_acao)}</td>
        </tr>
      </table>

      {tabela_itens_html}
      {"".join(blocos_extras)}
    """

    link_pedido = url_externa("pedidos.detalhe", pedido_id=pedido_id)
    html = _layout_email(
        titulo=titulo,
        subtitulo=subtitulo,
        cor_topo=fila["cor"],
        conteudo_html=conteudo,
        texto_botao=f"Abrir Pedido #{pedido_id} no Sistema",
        url_botao=link_pedido,
    )
    assunto = f"[Estoque de EPIs] Pedido #{pedido_id} — Lista: {fila['lista']} ({pedido['solicitante']})"
    return enviar_em_segundo_plano(destinatarios, assunto, html, tipo=f"pedido_{evento}")


def _verificar_estoque_minimo_itens(db, itens):
    """Retorna lista de EPIs separados no pedido cujo saldo atual ficou <= estoque_minimo."""
    epi_ids = sorted({it["epi_id"] for it in itens if it["epi_id"] and it["separado"] and not it["ignorado"]})
    if not epi_ids:
        return []
    alertas = []
    for eid in epi_ids:
        row = db.execute(
            """
            SELECT e.nome, e.tamanho, e.unidade, e.estoque_minimo,
                   COALESCE(SUM(CASE
                       WHEN m.cancelada = 1 THEN 0
                       WHEN m.tipo IN ('entrada', 'ajuste_mais') THEN m.quantidade
                       WHEN m.tipo IN ('saida', 'baixa', 'ajuste_menos') THEN -m.quantidade
                       ELSE 0
                   END), 0) AS saldo
            FROM epis e
            LEFT JOIN movimentacoes m ON m.epi_id = e.id
            WHERE e.id = ?
            GROUP BY e.id, e.nome, e.tamanho, e.unidade, e.estoque_minimo
            """,
            (eid,),
        ).fetchone()
        if row and int(row["saldo"]) <= int(row["estoque_minimo"]):
            nome = f"{row['nome']} ({row['tamanho']})" if row["tamanho"] else row["nome"]
            alertas.append({
                "nome": nome,
                "saldo": int(row["saldo"]),
                "minimo": int(row["estoque_minimo"]),
                "unidade": row["unidade"] or "un",
            })
    return alertas


def enviar_email_teste(destinatario, nome_solicitante="Administrador"):
    """Dispara um e-mail de teste para validar as configurações de disparo."""
    conteudo = f"""
      <p style="margin-top:0;">Olá!</p>
      <p>Este é um <strong>disparo de teste</strong> solicitado por <strong>{_html.escape(nome_solicitante)}</strong>
         na aba de Usuários do Sistema de Controle de Estoque e Pedidos de EPIs.</p>
      <div style="background:#f0fdf4; border:1px solid #bbf7d0; border-radius:8px; padding:14px 16px; color:#166534; margin:16px 0;">
        <strong>✓ Disparo de e-mail operacional!</strong><br>
        Os alertas de recuperação de senha (<em>Esqueceu a senha?</em>) e o acompanhamento das filas de pedidos
        (<em>Novos pedidos, Em separação, Pendentes, Prontos para retirada, Retirados e Cancelados</em>)
        estão prontos para envio neste endereço.
      </div>
    """
    html = _layout_email(
        titulo="Teste de Disparo de E-mail",
        subtitulo="Confirmação de recebimento · Controle de Estoque de EPIs",
        cor_topo="#0b4f6c",
        conteudo_html=conteudo,
        texto_botao="Acessar Painel de EPIs",
        url_botao=url_externa("estoque.painel"),
    )
    return enviar([destinatario], "[Estoque de EPIs] Teste de disparo de e-mail", html, tipo="teste")
