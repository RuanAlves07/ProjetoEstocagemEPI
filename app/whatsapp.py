"""Disparo de mensagens de WhatsApp pela Evolution API (a mesma usada pelo Nexo RH).

Configuração por variáveis de ambiente (no .env do servidor):
  EVOLUTION_API_URL      ex.: http://192.168.230.253:8080
  EVOLUTION_API_KEY      chave da instância (ou a chave global)
  EVOLUTION_INSTANCIA    nome da instância conectada ao número da empresa
"""

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from flask import current_app

logger = logging.getLogger(__name__)


def _cfg(nome):
    return (os.environ.get(nome) or "").strip()


def configurado():
    return bool(_cfg("EVOLUTION_API_URL") and _cfg("EVOLUTION_API_KEY") and _cfg("EVOLUTION_INSTANCIA"))


def normalizar_numero(valor):
    """Deixa só os dígitos e acrescenta o DDI 55 em números brasileiros com DDD (10 ou 11 dígitos).
    Devolve None quando o número não tem um tamanho válido."""
    digitos = re.sub(r"\D", "", valor or "")
    if not digitos:
        return None
    if len(digitos) in (10, 11):
        digitos = "55" + digitos
    if len(digitos) not in (12, 13) or not digitos.startswith("55"):
        return None
    return digitos


def formatar_numero(digitos):
    """5547999998888 -> (47) 99999-8888"""
    d = re.sub(r"\D", "", digitos or "")
    if d.startswith("55") and len(d) in (12, 13):
        d = d[2:]
    if len(d) == 11:
        return f"({d[:2]}) {d[2:7]}-{d[7:]}"
    if len(d) == 10:
        return f"({d[:2]}) {d[2:6]}-{d[6:]}"
    return digitos or ""


def mascarar_numero(digitos):
    """Mostra só o final do número na tela pública: (47) *****-8888"""
    d = re.sub(r"\D", "", digitos or "")
    if len(d) < 4:
        return "seu WhatsApp"
    ddd = d[2:4] if d.startswith("55") and len(d) >= 12 else ""
    return (f"({ddd}) " if ddd else "") + "*****-" + d[-4:]


def enviar_texto(numero, texto):
    """Envia uma mensagem de texto. Devolve (ok, erro)."""
    if current_app and current_app.config.get("TESTING"):
        current_app.config.setdefault("WHATSAPP_ENVIADOS", []).append({"numero": numero, "texto": texto})
        return True, None
    if not configurado():
        return False, "Envio por WhatsApp não configurado (EVOLUTION_API_URL, EVOLUTION_API_KEY e EVOLUTION_INSTANCIA)."
    url = (_cfg("EVOLUTION_API_URL").rstrip("/") + "/message/sendText/"
           + urllib.parse.quote(_cfg("EVOLUTION_INSTANCIA"), safe=""))
    corpo = json.dumps({"number": numero, "text": texto}).encode("utf-8")
    req = urllib.request.Request(url, data=corpo, method="POST", headers={
        "Content-Type": "application/json",
        "apikey": _cfg("EVOLUTION_API_KEY"),
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            if 200 <= r.status < 300:
                return True, None
            return False, f"Evolution API respondeu HTTP {r.status}."
    except urllib.error.HTTPError as e:
        detalhe = e.read()[:300].decode("utf-8", "replace")
        logger.warning("Evolution API recusou o envio (HTTP %s): %s", e.code, detalhe)
        if e.code in (400, 404) and "exists" in detalhe.lower():
            return False, "Esse número não tem WhatsApp. Confira o cadastro do colaborador."
        return False, f"Evolution API respondeu HTTP {e.code}."
    except Exception as e:  # rede, DNS, timeout
        logger.warning("Falha ao falar com a Evolution API: %s", e)
        return False, "Não foi possível conectar à Evolution API."


def cpf_valido(cpf):
    d = re.sub(r"\D", "", cpf or "")
    if len(d) != 11 or d == d[0] * 11:
        return False
    for tamanho in (9, 10):
        soma = sum(int(d[i]) * (tamanho + 1 - i) for i in range(tamanho))
        if (soma * 10 % 11) % 10 != int(d[tamanho]):
            return False
    return True


def formatar_cpf(cpf):
    d = re.sub(r"\D", "", cpf or "")
    return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}" if len(d) == 11 else (cpf or "")
