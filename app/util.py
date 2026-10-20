from datetime import date


def hoje():
    return date.today()


def parse_data(texto):
    try:
        return date.fromisoformat((texto or "").strip())
    except ValueError:
        return None


def to_int(texto, padrao=None):
    try:
        return int(str(texto).strip())
    except (TypeError, ValueError):
        return padrao


def to_float(texto, padrao=None):
    try:
        return float(str(texto).strip().replace(",", "."))
    except (TypeError, ValueError):
        return padrao


def limpar(texto):
    """Remove espaços; devolve None se vazio."""
    return (texto or "").strip() or None
