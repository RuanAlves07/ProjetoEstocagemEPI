"""Regras de negócio do estoque. Tudo que altera saldo passa por aqui."""
import math
import uuid
from datetime import date, timedelta

from .util import hoje

TIPOS = {
    "entrada": "Entrada",
    "saida": "Entrega",
    "baixa": "Baixa",
    "ajuste_mais": "Ajuste (+)",
    "ajuste_menos": "Ajuste (−)",
}
TIPOS_POSITIVOS = ("entrada", "ajuste_mais")

JANELA_CONSUMO_DIAS = 90      # janela para calcular o consumo médio
COBERTURA_ALVO_DIAS = 60      # sugestão de pedido cobre este período + estoque mínimo
AVISO_CA_DIAS = 30            # avisa CAs que vencem em até N dias

# +quantidade para entradas, -quantidade para o resto
DELTA = "CASE WHEN m.tipo IN ('entrada', 'ajuste_mais') THEN m.quantidade ELSE -m.quantidade END"


class ErroValidacao(Exception):
    pass


# ---------------------------------------------------------------- consultas

def saldo(db, epi_id):
    return db.execute(
        f"SELECT COALESCE(SUM({DELTA}), 0) FROM movimentacoes m WHERE m.epi_id = ? AND m.cancelada = 0",
        (epi_id,),
    ).fetchone()[0]


def epis_com_saldo(db, somente_ativos=True):
    """EPIs com saldo atual, consumo recente e situação de reposição."""
    desde = (hoje() - timedelta(days=JANELA_CONSUMO_DIAS)).isoformat()
    where = "WHERE e.ativo = 1" if somente_ativos else ""
    linhas = db.execute(
        f"""SELECT e.*,
                   COALESCE(SUM({DELTA}), 0) AS saldo,
                   COALESCE(SUM(CASE WHEN m.tipo IN ('saida', 'baixa') AND m.data >= ?
                                     THEN m.quantidade END), 0) AS consumo
            FROM epis e
            LEFT JOIN movimentacoes m ON m.epi_id = e.id AND m.cancelada = 0
            {where}
            GROUP BY e.id
            ORDER BY e.nome COLLATE NOCASE, e.tamanho COLLATE NOCASE""",
        (desde,),
    ).fetchall()
    return [enriquecer(l) for l in linhas]


def enriquecer(linha):
    e = dict(linha)
    consumo_diario = e["consumo"] / JANELA_CONSUMO_DIAS
    e["consumo_mensal"] = e["consumo"] / (JANELA_CONSUMO_DIAS / 30)
    e["cobertura_dias"] = e["saldo"] / consumo_diario if consumo_diario > 0 else None

    if e["saldo"] <= 0:
        e["status"] = "zerado"
    elif e["saldo"] <= e["estoque_minimo"] or (
        e["cobertura_dias"] is not None and e["cobertura_dias"] <= e["prazo_reposicao_dias"]
    ):
        e["status"] = "repor"
    else:
        e["status"] = "ok"

    alvo = e["estoque_minimo"] + math.ceil(consumo_diario * COBERTURA_ALVO_DIAS)
    e["sugerido"] = max(0, alvo - e["saldo"]) if e["status"] != "ok" else 0

    e["ca_status"] = status_ca(e["ca"], e["validade_ca"])
    return e


def status_ca(ca, validade):
    if not validade:
        return "sem_validade" if ca else "sem_ca"
    dias = (date.fromisoformat(validade) - hoje()).days
    if dias < 0:
        return "vencido"
    if dias <= AVISO_CA_DIAS:
        return "vencendo"
    return "ok"



# ---------------------------------------------------------------- escrita

def _transacao(db, fn):
    db.execute("BEGIN IMMEDIATE")
    try:
        resultado = fn()
        db.execute("COMMIT")
        return resultado
    except BaseException:
        db.execute("ROLLBACK")
        raise


def registrar(db, *args, **kwargs):
    """Registra uma movimentação com vários itens, de forma atômica (abre a própria transação)."""
    return _transacao(db, lambda: registrar_na_transacao(db, *args, **kwargs))


def registrar_na_transacao(db, tipo, itens, data, usuario_id, colaborador_id=None, fornecedor=None,
                           nota_fiscal=None, motivo=None, observacao=None, pedido_id=None,
                           local_destino=None):
    """Valida e grava a movimentação. Deve ser chamada DENTRO de uma transação (BEGIN IMMEDIATE).

    itens: lista de dicts {epi_id, quantidade, custo_unitario(opcional)}.
    Devolve o id do grupo (comprovante).
    """
    if tipo not in TIPOS:
        raise ErroValidacao("Tipo de movimentação inválido.")
    if data > hoje():
        raise ErroValidacao("A data não pode estar no futuro.")
    if not itens:
        raise ErroValidacao("Informe ao menos um item.")
    if tipo in ("baixa", "ajuste_mais", "ajuste_menos") and not motivo:
        raise ErroValidacao("Informe o motivo.")

    colaborador = None
    if tipo == "saida":
        colaborador = db.execute(
            "SELECT * FROM colaboradores WHERE id = ? AND ativo = 1", (colaborador_id,)
        ).fetchone()
        if not colaborador:
            raise ErroValidacao("Selecione um colaborador ativo.")

    necessario = {}   # epi_id -> quantidade total pedida (mesmo EPI pode repetir em linhas)
    epis = {}
    for item in itens:
        qtd = item["quantidade"]
        if not isinstance(qtd, int) or qtd <= 0:
            raise ErroValidacao("A quantidade deve ser um número inteiro maior que zero.")
        epi = db.execute("SELECT * FROM epis WHERE id = ? AND ativo = 1", (item["epi_id"],)).fetchone()
        if not epi:
            raise ErroValidacao("EPI inválido ou inativo.")
        if tipo == "saida" and epi["validade_ca"] and epi["validade_ca"] < data.isoformat():
            raise ErroValidacao(
                f"O CA {epi['ca'] or ''} de “{epi['nome']}” está vencido "
                f"(validade {epi['validade_ca']}). Atualize o CA no cadastro antes de entregar."
            )
        epis[epi["id"]] = epi
        necessario[epi["id"]] = necessario.get(epi["id"], 0) + qtd

    if tipo not in TIPOS_POSITIVOS:
        for epi_id, qtd in necessario.items():
            disponivel = saldo(db, epi_id)
            if qtd > disponivel:
                raise ErroValidacao(
                    f"Saldo insuficiente de “{epis[epi_id]['nome']}”: "
                    f"disponível {disponivel}, solicitado {qtd}."
                )

    grupo = uuid.uuid4().hex
    for item in itens:
        epi = epis[item["epi_id"]]
        db.execute(
            """INSERT INTO movimentacoes
               (grupo, tipo, epi_id, quantidade, data, colaborador_id, setor, ca, fornecedor,
                nota_fiscal, custo_unitario, motivo, observacao, usuario_id, pedido_id,
                local_destino)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (grupo, tipo, epi["id"], item["quantidade"], data.isoformat(),
             colaborador["id"] if colaborador else None,
             colaborador["setor"] if colaborador else None,
             epi["ca"], fornecedor, nota_fiscal, item.get("custo_unitario"),
             motivo, observacao, usuario_id, pedido_id, local_destino),
        )
    return grupo


def cancelar(db, mov_id, motivo, usuario_id):
    """Cancela (estorna) uma movimentação. O registro permanece para auditoria."""
    _transacao(db, lambda: cancelar_na_transacao(db, mov_id, motivo, usuario_id))


def cancelar_na_transacao(db, mov_id, motivo, usuario_id):
    if not motivo:
        raise ErroValidacao("Informe o motivo do cancelamento.")

    mov = db.execute("SELECT * FROM movimentacoes WHERE id = ?", (mov_id,)).fetchone()
    if not mov:
        raise ErroValidacao("Movimentação não encontrada.")
    if mov["cancelada"]:
        raise ErroValidacao("Esta movimentação já foi cancelada.")
    if mov["tipo"] in TIPOS_POSITIVOS and saldo(db, mov["epi_id"]) - mov["quantidade"] < 0:
        raise ErroValidacao(
            "Não é possível cancelar: parte dessa quantidade já foi entregue/baixada "
            "e o saldo ficaria negativo."
        )
    db.execute(
        """UPDATE movimentacoes
           SET cancelada = 1, cancelada_motivo = ?, cancelada_por = ?,
               cancelada_em = datetime('now', 'localtime')
           WHERE id = ?""",
        (motivo, usuario_id, mov_id),
    )
