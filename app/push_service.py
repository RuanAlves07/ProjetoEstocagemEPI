"""Serviço de Notificações Web Push para dispositivos móveis (Android e iOS)."""
import base64
import json
import logging
import threading
from py_vapid import Vapid
from pywebpush import webpush, WebPushException

from .db import get_db

logger = logging.getLogger(__name__)

VAPID_SUBJECT = "mailto:contato@abecker.com.br"


def obter_ou_criar_chaves_vapid(db):
    """Retorna as chaves VAPID salvas na tabela configuracoes ou gera novas chaves padrão."""
    linha = db.execute("SELECT valor FROM configuracoes WHERE chave = 'vapid_keys'").fetchone()
    if linha and linha["valor"]:
        try:
            chaves = json.loads(linha["valor"])
            if chaves.get("public_key") and chaves.get("private_key"):
                return chaves
        except Exception:
            pass

    # Gera novas chaves VAPID (par de chaves ECDSA P-256)
    v = Vapid()
    v.generate_keys()
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    raw_pub = v.public_key.public_bytes(
        encoding=Encoding.X962,
        format=PublicFormat.UncompressedPoint
    )
    pub_b64 = base64.urlsafe_b64encode(raw_pub).rstrip(b'=').decode('ascii')
    priv_pem = v.private_pem().decode('ascii')

    dados = {
        "public_key": pub_b64,
        "private_key": priv_pem,
        "subject": VAPID_SUBJECT,
    }
    payload = json.dumps(dados, ensure_ascii=False)
    db.execute(
        "INSERT INTO configuracoes (chave, valor) VALUES ('vapid_keys', ?) "
        "ON CONFLICT (chave) DO UPDATE SET valor = excluded.valor",
        (payload,),
    )
    return dados


def salvar_inscricao(db, endpoint, p256dh, auth, usuario_id=None, user_agent=None):
    """Salva ou atualiza uma inscrição de dispositivo móvel/desktop."""
    if not endpoint or not p256dh or not auth:
        return False
    db.execute(
        """INSERT INTO push_inscricoes (usuario_id, endpoint, p256dh, auth, user_agent)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (endpoint) DO UPDATE SET
             usuario_id = excluded.usuario_id,
             p256dh = excluded.p256dh,
             auth = excluded.auth,
             user_agent = excluded.user_agent,
             criado_em = CURRENT_TIMESTAMP""",
        (usuario_id, endpoint, p256dh, auth, (user_agent or "")[:300]),
    )
    return True


def remover_inscricao(db, endpoint):
    """Remove uma inscrição que expirou ou foi cancelada pelo usuário."""
    db.execute("DELETE FROM push_inscricoes WHERE endpoint = ?", (endpoint,))


_cache_vapid = {}


def _objeto_vapid(chaves_vapid):
    """Carrega a chave privada (PEM) em um objeto Vapid.

    O pywebpush interpreta strings como DER em base64; passar o PEM direto gera
    "Could not deserialize key data". Entregamos o objeto já carregado.
    """
    pem = chaves_vapid["private_key"]
    obj = _cache_vapid.get(pem)
    if obj is None:
        if "-----BEGIN" in pem:
            obj = Vapid.from_pem(pem.encode("ascii"))
        else:
            obj = Vapid.from_string(private_key=pem)
        _cache_vapid.clear()
        _cache_vapid[pem] = obj
    return obj


def enviar_push_dispositivo(subscription_info, payload_dict, chaves_vapid):
    """Envia um push para um dispositivo específico."""
    try:
        webpush(
            subscription_info=subscription_info,
            data=json.dumps(payload_dict, ensure_ascii=False),
            vapid_private_key=_objeto_vapid(chaves_vapid),
            vapid_claims={"sub": chaves_vapid.get("subject", VAPID_SUBJECT)},
            timeout=10,
        )
        return True, None
    except WebPushException as ex:
        status_code = getattr(getattr(ex, "response", None), "status_code", None)
        logger.warning("Web push recusado (HTTP %s): %s", status_code, ex)
        return False, status_code
    except Exception as e:
        logger.warning("Falha ao enviar web push: %s", e)
        return False, None


def enviar_para_todos(db, payload_dict, usuario_id=None):
    """Dispara a notificação para todos os dispositivos inscritos (ou para um usuário específico)."""
    chaves = obter_ou_criar_chaves_vapid(db)
    if usuario_id:
        linhas = db.execute(
            "SELECT id, endpoint, p256dh, auth FROM push_inscricoes WHERE usuario_id = ?",
            (usuario_id,),
        ).fetchall()
    else:
        linhas = db.execute("SELECT id, endpoint, p256dh, auth FROM push_inscricoes").fetchall()

    expirados = []
    enviados = 0
    for r in linhas:
        sub_info = {
            "endpoint": r["endpoint"],
            "keys": {
                "p256dh": r["p256dh"],
                "auth": r["auth"],
            },
        }
        ok, status = enviar_push_dispositivo(sub_info, payload_dict, chaves)
        if ok:
            enviados += 1
        elif status in (404, 410):
            expirados.append(r["endpoint"])

    if expirados:
        for ep in expirados:
            remover_inscricao(db, ep)

    return enviados


def notificar_novo_pedido_push(app, pedido_id, solicitante, local, itens_resumo=None):
    """Dispara a notificação de novo pedido em segundo plano para não travar a requisição do usuário."""
    def _trabalho():
        try:
            with app.app_context():
                db = get_db()
                payload = {
                    "title": "🚨 Novo Pedido de EPI!",
                    "body": f"{solicitante} ({local}): {itens_resumo or 'EPI(s) solicitado(s)'}",
                    "icon": "/static/icone.png",
                    "badge": "/static/icone.png",
                    "url": f"/pedidos/{pedido_id}",
                    "tag": f"pedido-{pedido_id}",
                    "vibrate": [200, 100, 200, 100, 200],
                }
                enviar_para_todos(db, payload)
        except Exception as e:
            logger.error("Erro no envio em segundo plano do push: %s", e)

    t = threading.Thread(target=_trabalho, daemon=True)
    t.start()
