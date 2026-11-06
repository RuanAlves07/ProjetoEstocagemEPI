"""Rotas para gerenciar inscrições de notificações Web Push."""
from flask import Blueprint, jsonify, request, g
from .db import get_db
from . import push_service

bp = Blueprint("push", __name__, url_prefix="/api/push")


@bp.get("/chave-publica")
def chave_publica():
    """Retorna a chave pública VAPID para o navegador assinar a inscrição."""
    db = get_db()
    chaves = push_service.obter_ou_criar_chaves_vapid(db)
    return jsonify(ok=True, publicKey=chaves["public_key"])


@bp.post("/inscrever")
def inscrever():
    """Registra uma inscrição de push recebida do navegador móvel ou desktop."""
    db = get_db()
    dados = request.get_json(silent=True) or {}
    endpoint = dados.get("endpoint")
    keys = dados.get("keys") or {}
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")

    if not endpoint or not p256dh or not auth:
        return jsonify(ok=False, erro="Dados de inscrição inválidos."), 400

    usuario_id = g.usuario["id"] if g.get("usuario") else None
    user_agent = request.headers.get("User-Agent", "")

    push_service.salvar_inscricao(db, endpoint, p256dh, auth, usuario_id, user_agent)
    return jsonify(ok=True, mensagem="Dispositivo inscrito com sucesso para notificações.")


@bp.post("/desinscrever")
def desinscrever():
    """Remove a inscrição do dispositivo atual."""
    db = get_db()
    dados = request.get_json(silent=True) or {}
    endpoint = dados.get("endpoint")
    if endpoint:
        push_service.remover_inscricao(db, endpoint)
    return jsonify(ok=True)


@bp.post("/testar")
def testar():
    """Dispara uma notificação push de teste para o dispositivo que solicitou."""
    db = get_db()
    dados = request.get_json(silent=True) or {}
    endpoint = dados.get("endpoint")
    keys = dados.get("keys") or {}

    payload = {
        "title": "📲 Teste de Notificação Mobile",
        "body": "Sucesso! Seu celular está configurado e vibrará sempre que um novo pedido de EPI for solicitado.",
        "icon": "/static/icone.png",
        "badge": "/static/icone.png",
        "url": "/pedidos",
        "tag": "teste-push",
        "vibrate": [100, 50, 100, 50, 200],
    }

    if endpoint and keys.get("p256dh") and keys.get("auth"):
        chaves = push_service.obter_ou_criar_chaves_vapid(db)
        sub_info = {
            "endpoint": endpoint,
            "keys": {
                "p256dh": keys["p256dh"],
                "auth": keys["auth"],
            },
        }
        ok, status = push_service.enviar_push_dispositivo(sub_info, payload, chaves)
        if ok:
            return jsonify(ok=True, mensagem="Notificação de teste enviada com sucesso para o seu celular!")
        return jsonify(ok=False, erro=f"Falha ao enviar push (código {status})."), 500

    usuario_id = g.usuario["id"] if g.get("usuario") else None
    enviados = push_service.enviar_para_todos(db, payload, usuario_id=usuario_id)
    return jsonify(ok=True, enviados=enviados, mensagem=f"Notificação de teste enviada para {enviados} dispositivo(s).")
