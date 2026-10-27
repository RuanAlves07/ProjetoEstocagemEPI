import re
from datetime import date, datetime

from .estoque import TIPOS
from .pedidos import STATUS as STATUS_PEDIDO


def _data_br(valor):
    if not valor:
        return "—"
    try:
        return date.fromisoformat(str(valor)[:10]).strftime("%d/%m/%Y")
    except ValueError:
        return valor


def _hora_br(valor):
    if not valor:
        return "—"
    try:
        d, h = str(valor).split(" ")
        return f"{_data_br(d)} {h[:5]}"
    except ValueError:
        return valor


def _num(valor, casas=0):
    if valor is None:
        return "—"
    texto = f"{valor:,.{casas}f}"
    return texto.replace(",", "X").replace(".", ",").replace("X", ".")


def _dinheiro(valor):
    return "—" if valor is None else "R$ " + _num(valor, 2)


def _tempo_atras(valor):
    try:
        seg = (datetime.now() - datetime.strptime(str(valor), "%Y-%m-%d %H:%M:%S")).total_seconds()
    except ValueError:
        return "—"
    minutos = int(seg // 60)
    if minutos < 1:
        return "agora"
    if minutos < 60:
        return f"há {minutos} min"
    if minutos < 60 * 24:
        return f"há {minutos // 60} h"
    return f"há {minutos // 1440} d"


_FORA_FUNCAO_ANTIGO = re.compile(r"\[Fora da função: [^\]]*? — Justificativa: ")


def _obs_pedido(valor):
    """Observação do pedido para exibição: pedidos antigos do gestor listavam os itens fora da função
    antes da justificativa; esses itens já aparecem com a etiqueta "analisar", então fica só a justificativa."""
    return _FORA_FUNCAO_ANTIGO.sub("[Justificativa: ", valor) if valor else valor


def _duracao(horas):
    """Horas (float) em texto curto: '45 min', '5 h', '2 d 3 h'."""
    if horas is None:
        return "—"
    minutos = int(round(abs(horas) * 60))
    if minutos < 60:
        return f"{minutos} min"
    h = minutos // 60
    if h < 24:
        return f"{h} h"
    d, h = divmod(h, 24)
    return f"{d} d {h} h" if h else f"{d} d"


def registrar(app):
    app.jinja_env.filters.update(data_br=_data_br, hora_br=_hora_br, num=_num, dinheiro=_dinheiro,
                                 tempo_atras=_tempo_atras, obs_pedido=_obs_pedido, duracao=_duracao)
    app.jinja_env.globals["TIPOS"] = TIPOS
    app.jinja_env.globals["STATUS_PEDIDO"] = STATUS_PEDIDO
