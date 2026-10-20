"""Painel inicial e relatórios gerenciais."""
from datetime import timedelta

from flask import Blueprint, render_template, request

from . import estoque
from .db import get_db
from .movimentos import consultar_movimentos
from .util import hoje, parse_data

bp = Blueprint("painel", __name__)


@bp.get("/")
def inicio():
    db = get_db()
    epis = estoque.epis_com_saldo(db)
    ordem = {"zerado": 0, "repor": 1}
    repor = sorted((e for e in epis if e["status"] != "ok"),
                   key=lambda e: (ordem[e["status"]], e["cobertura_dias"] if e["cobertura_dias"] is not None else 1e9))
    ca_alertas = sorted((e for e in epis if e["ca_status"] in ("vencido", "vencendo")),
                        key=lambda e: e["validade_ca"])
    mais_saem = sorted((e for e in epis if e["consumo"] > 0), key=lambda e: -e["consumo"])[:5]

    inicio_mes = hoje().replace(day=1).isoformat()
    totais = {r["tipo"]: r["total"] for r in db.execute(
        """SELECT tipo, SUM(quantidade) AS total FROM movimentacoes
           WHERE cancelada = 0 AND data >= ? GROUP BY tipo""", (inicio_mes,))}
    return render_template(
        "painel.html", epis=epis, repor=repor, ca_alertas=ca_alertas, mais_saem=mais_saem,
        total_entradas=totais.get("entrada", 0), total_entregas=totais.get("saida", 0),
        ultimas=consultar_movimentos(db, {}, limite=8), janela=estoque.JANELA_CONSUMO_DIAS,
    )


@bp.get("/relatorios")
def relatorios():
    db = get_db()
    ate = parse_data(request.args.get("ate")) or hoje()
    de = parse_data(request.args.get("de")) or (ate - timedelta(days=89))
    if de > ate:
        de, ate = ate, de
    p = (de.isoformat(), ate.isoformat())

    ranking = db.execute(
        """SELECT e.id, e.nome, e.unidade,
                  SUM(CASE WHEN m.tipo = 'entrada' THEN m.quantidade ELSE 0 END) AS entradas,
                  SUM(CASE WHEN m.tipo = 'saida' THEN m.quantidade ELSE 0 END) AS entregas,
                  SUM(CASE WHEN m.tipo = 'baixa' THEN m.quantidade ELSE 0 END) AS baixas,
                  SUM(CASE WHEN m.tipo IN ('saida', 'baixa') THEN m.quantidade ELSE 0 END) AS total_saidas
           FROM movimentacoes m JOIN epis e ON e.id = m.epi_id
           WHERE m.cancelada = 0 AND m.data BETWEEN ? AND ? AND m.tipo IN ('entrada', 'saida', 'baixa')
           GROUP BY e.id, e.nome, e.unidade
           ORDER BY total_saidas DESC, e.nome COLLATE NOCASE""", p).fetchall()

    por_setor = db.execute(
        """SELECT COALESCE(setor, '(sem setor)') AS nome, SUM(quantidade) AS total,
                  COUNT(DISTINCT colaborador_id) AS colaboradores
           FROM movimentacoes WHERE cancelada = 0 AND tipo = 'saida' AND data BETWEEN ? AND ?
           GROUP BY COALESCE(setor, '(sem setor)') ORDER BY total DESC""", p).fetchall()

    por_local = db.execute(
        """SELECT COALESCE(local_destino, '(sem local)') AS nome, SUM(quantidade) AS total,
                  COUNT(DISTINCT grupo) AS entregas
           FROM movimentacoes WHERE cancelada = 0 AND tipo = 'saida' AND data BETWEEN ? AND ?
           GROUP BY COALESCE(local_destino, '(sem local)') ORDER BY total DESC""", p).fetchall()

    por_colaborador = db.execute(
        """SELECT c.id, c.nome, c.setor, SUM(m.quantidade) AS total, COUNT(DISTINCT m.grupo) AS entregas
           FROM movimentacoes m JOIN colaboradores c ON c.id = m.colaborador_id
           WHERE m.cancelada = 0 AND m.tipo = 'saida' AND m.data BETWEEN ? AND ?
           GROUP BY c.id, c.nome, c.setor ORDER BY total DESC LIMIT 15""", p).fetchall()

    por_mes = db.execute(
        """SELECT substr(data, 1, 7) AS mes,
                  SUM(CASE WHEN tipo = 'entrada' THEN quantidade ELSE 0 END) AS entradas,
                  SUM(CASE WHEN tipo IN ('saida', 'baixa') THEN quantidade ELSE 0 END) AS saidas
           FROM movimentacoes
           WHERE cancelada = 0 AND tipo IN ('entrada', 'saida', 'baixa') AND data BETWEEN ? AND ?
           GROUP BY substr(data, 1, 7) ORDER BY mes""", p).fetchall()

    maximos = {
        "ranking": max([r["entregas"] + r["baixas"] for r in ranking] or [1]) or 1,
        "setor": max([r["total"] for r in por_setor] or [1]) or 1,
        "local": max([r["total"] for r in por_local] or [1]) or 1,
        "colaborador": max([r["total"] for r in por_colaborador] or [1]) or 1,
        "mes": max([max(r["entradas"], r["saidas"]) for r in por_mes] or [1]) or 1,
    }
    hoje_ = hoje()
    atalhos = {
        "Últimos 30 dias": (hoje_ - timedelta(days=29), hoje_),
        "Últimos 90 dias": (hoje_ - timedelta(days=89), hoje_),
        "Este mês": (hoje_.replace(day=1), hoje_),
        "Este ano": (hoje_.replace(month=1, day=1), hoje_),
    }
    return render_template("relatorios.html", de=de, ate=ate, ranking=ranking, por_setor=por_setor, por_local=por_local,
                           por_colaborador=por_colaborador, por_mes=por_mes, maximos=maximos,
                           atalhos=atalhos)
