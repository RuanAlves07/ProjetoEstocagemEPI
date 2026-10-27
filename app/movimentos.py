"""Entradas, entregas, baixas, ajustes, histórico e comprovantes."""
import csv
import io

from flask import (Blueprint, abort, current_app, flash, g, redirect, render_template, request,
                   url_for)

from . import estoque
from .auth import admin_required, escrita_required
from .cadastros import _csv_response
from .db import get_db
from .estoque import ErroValidacao
from .util import hoje, limpar, parse_data, to_float, to_int

bp = Blueprint("movimentos", __name__)

TITULOS = {
    "entrada": "Registrar entrada de estoque",
    "saida": "Registrar entrega de EPI",
    "baixa": "Registrar baixa (perda, avaria, descarte)",
    "ajuste": "Ajuste de estoque (inventário / saldo inicial)",
}


# ------------------------------------------------------------ consulta

def consultar_movimentos(db, f, limite=50, offset=0):
    sql = """SELECT m.*, e.nome AS epi_nome, e.tamanho AS epi_tamanho, e.unidade, c.nome AS colab_nome, c.matricula,
                    c.funcao AS colab_funcao, u.nome AS usuario_nome, uc.nome AS cancelada_por_nome
             FROM movimentacoes m
             JOIN epis e ON e.id = m.epi_id
             JOIN usuarios u ON u.id = m.usuario_id
             LEFT JOIN usuarios uc ON uc.id = m.cancelada_por
             LEFT JOIN colaboradores c ON c.id = m.colaborador_id
             WHERE 1 = 1"""
    params = []
    if not f.get("incluir_canceladas"):
        sql += " AND m.cancelada = 0"
    for campo, coluna in (("epi_id", "m.epi_id"), ("colaborador_id", "m.colaborador_id"),
                          ("tipo", "m.tipo"), ("grupo", "m.grupo"), ("setor", "m.setor")):
        if f.get(campo):
            sql += f" AND {coluna} = ?"
            params.append(f[campo])
    if f.get("de"):
        sql += " AND m.data >= ?"
        params.append(f["de"])
    if f.get("ate"):
        sql += " AND m.data <= ?"
        params.append(f["ate"])
    sql += " ORDER BY m.data DESC, m.id DESC"
    if limite is not None:
        sql += " LIMIT ? OFFSET ?"
        params += [limite, offset]
    return db.execute(sql, params).fetchall()


def _filtros_da_url():
    a = request.args
    de, ate = parse_data(a.get("de")), parse_data(a.get("ate"))
    tipo = a.get("tipo")
    return {
        "tipo": tipo if tipo in estoque.TIPOS else None,
        "epi_id": to_int(a.get("epi_id")),
        "colaborador_id": to_int(a.get("colaborador_id")),
        "setor": limpar(a.get("setor")),
        "de": de.isoformat() if de else None,
        "ate": ate.isoformat() if ate else None,
        "incluir_canceladas": a.get("canceladas") == "1",
    }


@bp.get("/movimentos")
def lista():
    db = get_db()
    f = _filtros_da_url()
    pagina = max(1, to_int(request.args.get("pagina"), 1))
    por_pagina = current_app.config["POR_PAGINA"]
    movs = consultar_movimentos(db, f, limite=por_pagina + 1, offset=(pagina - 1) * por_pagina)
    args = {k: v for k, v in request.args.items() if k != "pagina"}
    return render_template(
        "mov_lista.html", movs=movs[:por_pagina], f=request.args, pagina=pagina,
        tem_proxima=len(movs) > por_pagina, args=args,
        epis=db.execute("SELECT id, nome FROM epis ORDER BY nome COLLATE NOCASE").fetchall(),
        colaboradores=db.execute("SELECT id, nome FROM colaboradores ORDER BY nome COLLATE NOCASE").fetchall(),
        setores=[r[0] for r in db.execute(
            "SELECT DISTINCT setor FROM movimentacoes WHERE setor IS NOT NULL ORDER BY setor COLLATE NOCASE")],
    )


@bp.get("/movimentos/exportar.csv")
def exportar_csv():
    movs = consultar_movimentos(get_db(), _filtros_da_url(), limite=None)
    saida = io.StringIO()
    w = csv.writer(saida, delimiter=";")
    w.writerow(["Data", "Tipo", "EPI", "CA", "Quantidade", "Colaborador", "Matrícula", "Setor",
                "Local de destino", "Pedido", "Fornecedor", "Nota fiscal", "Custo unitário", "Motivo", "Observação", "Registrado por",
                "Registrado em", "Cancelada", "Motivo do cancelamento"])
    for m in movs:
        w.writerow([m["data"], estoque.TIPOS[m["tipo"]], m["epi_nome"], m["ca"] or "", m["quantidade"],
                    m["colab_nome"] or "", m["matricula"] or "", m["setor"] or "",
                    m["local_destino"] or "", m["pedido_id"] or "", m["fornecedor"] or "",
                    m["nota_fiscal"] or "",
                    "" if m["custo_unitario"] is None else str(m["custo_unitario"]).replace(".", ","),
                    m["motivo"] or "", m["observacao"] or "", m["usuario_nome"], m["criado_em"],
                    "Sim" if m["cancelada"] else "", m["cancelada_motivo"] or ""])
    return _csv_response(saida, "movimentacoes.csv")


# ------------------------------------------------------------- registro

def _ler_linhas():
    """Linhas do formulário como texto (para reexibir em caso de erro). Ignora linhas em branco."""
    ids = request.form.getlist("epi_id")
    qtds = request.form.getlist("quantidade")
    custos = request.form.getlist("custo_unitario")
    linhas = []
    for i, bruto_id in enumerate(ids):
        qtd = qtds[i].strip() if i < len(qtds) else ""
        custo = custos[i].strip() if i < len(custos) else ""
        if not bruto_id and not qtd:
            continue
        linhas.append({"epi_id": to_int(bruto_id), "quantidade": qtd, "custo_unitario": custo})
    return linhas


def _converter_itens(linhas, tipo):
    itens = []
    for linha in linhas:
        if linha["epi_id"] is None:
            raise ErroValidacao("Selecione o EPI em todas as linhas.")
        item = {"epi_id": linha["epi_id"], "quantidade": to_int(linha["quantidade"], 0)}
        if tipo == "entrada" and linha["custo_unitario"]:
            custo = to_float(linha["custo_unitario"])
            if custo is None or custo < 0:
                raise ErroValidacao("Custo unitário inválido.")
            item["custo_unitario"] = custo
        itens.append(item)
    return itens


@bp.route("/movimentar/<tipo>", methods=["GET", "POST"])
@escrita_required
def movimentar(tipo):
    if tipo not in TITULOS:
        abort(404)
    db = get_db()
    form = {"data": hoje().isoformat(), "colaborador_id": to_int(request.args.get("colaborador_id")),
            "fornecedor": limpar(request.args.get("fornecedor")), "sentido": "mais"}
    linhas = [{"epi_id": to_int(request.args.get("epi_id")), "quantidade": "", "custo_unitario": ""}]

    if request.method == "POST":
        form = {
            "data": request.form.get("data", ""),
            "colaborador_id": to_int(request.form.get("colaborador_id")),
            "fornecedor": limpar(request.form.get("fornecedor")),
            "nota_fiscal": limpar(request.form.get("nota_fiscal")),
            "motivo": limpar(request.form.get("motivo")),
            "observacao": limpar(request.form.get("observacao")),
            "sentido": request.form.get("sentido", "mais"),
        }
        linhas = _ler_linhas() or linhas
        try:
            data = parse_data(form["data"])
            if not data:
                raise ErroValidacao("Data inválida.")
            tipo_real = tipo
            if tipo == "ajuste":
                tipo_real = "ajuste_menos" if form["sentido"] == "menos" else "ajuste_mais"
            grupo = estoque.registrar(
                db, tipo_real, _converter_itens(linhas, tipo), data, g.usuario["id"],
                colaborador_id=form["colaborador_id"], fornecedor=form["fornecedor"],
                nota_fiscal=form["nota_fiscal"], motivo=form["motivo"], observacao=form["observacao"])
            if tipo == "saida":
                flash("Entrega registrada. Imprima o comprovante para a assinatura do colaborador.", "ok")
                return redirect(url_for("movimentos.comprovante", grupo=grupo))
            flash("Movimentação registrada.", "ok")
            if tipo_real in ("entrada", "ajuste_mais"):
                from . import pedidos   # import local: pedidos depende de estoque
                prontas = pedidos.pendencias_com_estoque(db)
                if prontas:
                    flash("📦 Chegou estoque para " + ", ".join(f"#{i}" for i in prontas)
                          + " — pendência(s) de ressalva que já podem ser separadas (Pedidos → Aguardando estoque).", "ok")
            return redirect(url_for("movimentos.lista"))
        except ErroValidacao as e:
            flash(str(e), "erro")

    return render_template(
        "mov_form.html", tipo=tipo, titulo=TITULOS[tipo], form=form, itens=linhas,
        epis=estoque.epis_com_saldo(db),
        colaboradores=db.execute(
            "SELECT * FROM colaboradores WHERE ativo = 1 ORDER BY nome COLLATE NOCASE").fetchall(),
        fornecedores_lista=db.execute(
            "SELECT id, nome, cnpj FROM fornecedores WHERE ativo = 1 ORDER BY nome COLLATE NOCASE").fetchall(),
    )


def _destino_seguro(destino):
    if destino and destino.startswith("/") and not destino.startswith("//"):
        return destino
    return url_for("movimentos.lista")


@bp.post("/movimentos/<int:mov_id>/cancelar")
@admin_required
def cancelar(mov_id):
    try:
        estoque.cancelar(get_db(), mov_id, limpar(request.form.get("motivo")), g.usuario["id"])
        flash("Movimentação cancelada; o saldo foi recalculado.", "ok")
    except ErroValidacao as e:
        flash(str(e), "erro")
    return redirect(_destino_seguro(request.form.get("voltar")))


@bp.get("/entregas/<grupo>/comprovante")
def comprovante(grupo):
    linhas = consultar_movimentos(get_db(), {"grupo": grupo, "tipo": "saida", "incluir_canceladas": True},
                                  limite=None)
    if not linhas:
        abort(404)
    pedido = get_db().execute("SELECT * FROM pedidos WHERE grupo = ?", (grupo,)).fetchone()
    return render_template("comprovante.html", linhas=linhas, topo=linhas[0], pedido=pedido)
