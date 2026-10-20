from datetime import date, datetime

from .estoque import TIPOS


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


def registrar(app):
    app.jinja_env.filters.update(data_br=_data_br, hora_br=_hora_br, num=_num, dinheiro=_dinheiro,
                                 tempo_atras=_tempo_atras)
    app.jinja_env.globals["TIPOS"] = TIPOS
