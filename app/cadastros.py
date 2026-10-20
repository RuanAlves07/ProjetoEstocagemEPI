"""Cadastros: EPIs, colaboradores e usuários."""
import csv
import io
import sqlite3

from flask import (Blueprint, Response, abort, flash, g, jsonify, redirect, render_template,
                   request, url_for)
from werkzeug.security import generate_password_hash

from . import estoque
from .auth import PERFIS, SENHA_MIN, _senha_valida, admin_required, escrita_required
from .db import get_db
from .util import hoje, limpar, parse_data, to_int

bp = Blueprint("cadastros", __name__)


# ------------------------------------------------------------------ EPIs

@bp.get("/epis")
def epis_lista():
    mostrar_inativos = request.args.get("inativos") == "1"
    busca = (request.args.get("q") or "").strip().lower()
    epis = estoque.epis_com_saldo(get_db(), somente_ativos=not mostrar_inativos)
    if busca:
        epis = [e for e in epis if busca in e["nome"].lower() or busca in (e["categoria"] or "").lower()
                or busca in (e["ca"] or "").lower()]
    return render_template("epis_lista.html", epis=epis, busca=busca, mostrar_inativos=mostrar_inativos)


@bp.get("/epis/exportar.csv")
def epis_csv():
    epis = estoque.epis_com_saldo(get_db(), somente_ativos=False)
    saida = io.StringIO()
    w = csv.writer(saida, delimiter=";")
    w.writerow(["EPI", "Categoria", "CA", "Validade CA", "Unidade", "Saldo", "Estoque mínimo",
                "Consumo médio mensal", "Cobertura (dias)", "Situação", "Sugestão de pedido", "Ativo"])
    rotulos = {"ok": "OK", "repor": "Repor", "zerado": "Sem estoque"}
    for e in epis:
        w.writerow([e["nome"], e["categoria"] or "", e["ca"] or "", e["validade_ca"] or "", e["unidade"],
                    e["saldo"], e["estoque_minimo"], f"{e['consumo_mensal']:.1f}".replace(".", ","),
                    "" if e["cobertura_dias"] is None else round(e["cobertura_dias"]),
                    rotulos[e["status"]], e["sugerido"], "Sim" if e["ativo"] else "Não"])
    return _csv_response(saida, "estoque-epis.csv")


def _csv_response(buffer, nome):
    # utf-8-sig: o Excel abre acentos corretamente
    return Response(buffer.getvalue().encode("utf-8-sig"), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={nome}"})


def _ler_epi(form):
    erros = []
    dados = {
        "nome": limpar(form.get("nome")),
        "categoria": limpar(form.get("categoria")),
        "ca": limpar(form.get("ca")),
        "unidade": limpar(form.get("unidade")) or "un",
        "estoque_minimo": to_int(form.get("estoque_minimo"), 0),
        "prazo_reposicao_dias": to_int(form.get("prazo_reposicao_dias"), 15),
    }
    validade = limpar(form.get("validade_ca"))
    if validade and not parse_data(validade):
        erros.append("Validade do CA inválida.")
    dados["validade_ca"] = validade
    if not dados["nome"]:
        erros.append("Informe o nome do EPI.")
    if dados["estoque_minimo"] < 0 or dados["prazo_reposicao_dias"] < 0:
        erros.append("Estoque mínimo e prazo de reposição não podem ser negativos.")
    return dados, erros


@bp.route("/epis/novo", methods=["GET", "POST"])
@escrita_required
def epi_novo():
    dados = {"unidade": "un", "estoque_minimo": 0, "prazo_reposicao_dias": 15}
    if request.method == "POST":
        dados, erros = _ler_epi(request.form)
        if not erros:
            try:
                cur = get_db().execute(
                    """INSERT INTO epis (nome, categoria, ca, validade_ca, unidade, estoque_minimo,
                                         prazo_reposicao_dias)
                       VALUES (:nome, :categoria, :ca, :validade_ca, :unidade, :estoque_minimo,
                               :prazo_reposicao_dias)""", dados)
                flash("EPI cadastrado. Para lançar o saldo atual, use Movimentar → Ajuste (+).", "ok")
                return redirect(url_for("cadastros.epi_detalhe", epi_id=cur.lastrowid))
            except sqlite3.IntegrityError:
                erros.append("Já existe um EPI com esse nome. Inclua o tamanho/modelo no nome para diferenciar.")
        for e in erros:
            flash(e, "erro")
    return render_template("epi_form.html", epi=dados, novo=True)


@bp.route("/epis/<int:epi_id>/editar", methods=["GET", "POST"])
@escrita_required
def epi_editar(epi_id):
    db = get_db()
    epi = db.execute("SELECT * FROM epis WHERE id = ?", (epi_id,)).fetchone() or abort(404)
    if request.method == "POST":
        dados, erros = _ler_epi(request.form)
        dados["ativo"] = 1 if request.form.get("ativo") else 0
        dados["id"] = epi_id
        if not erros:
            try:
                db.execute(
                    """UPDATE epis SET nome=:nome, categoria=:categoria, ca=:ca, validade_ca=:validade_ca,
                       unidade=:unidade, estoque_minimo=:estoque_minimo,
                       prazo_reposicao_dias=:prazo_reposicao_dias, ativo=:ativo WHERE id=:id""", dados)
                flash("EPI atualizado.", "ok")
                return redirect(url_for("cadastros.epi_detalhe", epi_id=epi_id))
            except sqlite3.IntegrityError:
                erros.append("Já existe um EPI com esse nome.")
        for e in erros:
            flash(e, "erro")
        epi = dados
    return render_template("epi_form.html", epi=epi, novo=False, epi_id=epi_id)


@bp.get("/epis/<int:epi_id>")
def epi_detalhe(epi_id):
    db = get_db()
    existe = db.execute("SELECT 1 FROM epis WHERE id = ?", (epi_id,)).fetchone()
    if not existe:
        abort(404)
    epi = next(e for e in estoque.epis_com_saldo(db, somente_ativos=False) if e["id"] == epi_id)
    from .movimentos import consultar_movimentos
    movs = consultar_movimentos(db, {"epi_id": epi_id}, limite=100)
    return render_template("epi_detalhe.html", epi=epi, movs=movs)


# ---------------------------------------------------------- colaboradores

def _setores():
    return [r[0] for r in get_db().execute(
        "SELECT DISTINCT setor FROM colaboradores WHERE setor IS NOT NULL AND trim(setor) != '' ORDER BY setor COLLATE NOCASE")]


def _cargos(db=None):
    db = db or get_db()
    linhas = db.execute(
        """SELECT DISTINCT trim(funcao) AS cargo FROM colaboradores WHERE funcao IS NOT NULL AND trim(funcao) != ''
           UNION
           SELECT DISTINCT trim(cargo) AS cargo FROM cargo_epis_padrao WHERE cargo IS NOT NULL AND trim(cargo) != ''
           ORDER BY 1 COLLATE NOCASE"""
    ).fetchall()
    return [r[0] for r in linhas if r[0]]


def _padrao_ids_cargo(db, cargo):
    cargo = (cargo or "").strip()
    if not cargo:
        return set()
    return {
        r["epi_id"]
        for r in db.execute("SELECT epi_id FROM cargo_epis_padrao WHERE cargo = ? COLLATE NOCASE", (cargo,))
    }


def _padrao_ids_colaborador(db, colab):
    """Puxa os EPIs padrão pelo cargo do colaborador (ou fallback individual caso não tenha cargo definido)."""
    cargo = (colab["funcao"] or "").strip()
    if cargo:
        return _padrao_ids_cargo(db, cargo)
    return {
        r["epi_id"]
        for r in db.execute("SELECT epi_id FROM colaborador_epis_padrao WHERE colaborador_id = ?", (colab["id"],))
    }


@bp.get("/colaboradores")
def colaboradores_lista():
    db = get_db()
    mostrar_inativos = request.args.get("inativos") == "1"
    busca = (request.args.get("q") or "").strip()
    cargo_filtro = (request.args.get("cargo") or "").strip()
    sql = "SELECT * FROM colaboradores WHERE 1=1"
    params = []
    if not mostrar_inativos:
        sql += " AND ativo = 1"
    if busca:
        sql += " AND (nome LIKE ? OR matricula LIKE ? OR setor LIKE ? OR funcao LIKE ?)"
        params += [f"%{busca}%"] * 4
    if cargo_filtro:
        sql += " AND funcao = ? COLLATE NOCASE"
        params.append(cargo_filtro)
    colaboradores = db.execute(sql + " ORDER BY nome COLLATE NOCASE", params).fetchall()
    contagem_padrao = {
        r["cargo"].casefold(): r["n"]
        for r in db.execute("SELECT cargo, COUNT(*) AS n FROM cargo_epis_padrao GROUP BY cargo COLLATE NOCASE")
    }
    return render_template(
        "colaboradores_lista.html",
        colaboradores=colaboradores,
        busca=busca,
        cargo_filtro=cargo_filtro,
        cargos=_cargos(db),
        contagem_padrao=contagem_padrao,
        mostrar_inativos=mostrar_inativos,
    )


@bp.route("/colaboradores/cargos", methods=["GET", "POST"])
def cargos_padrao():
    """Lista todos os cargos ou abre a lista de equipamentos padrão de um cargo selecionado."""
    db = get_db()
    cargos = _cargos(db)
    cargo_atual = limpar(request.values.get("cargo"))

    if request.method == "POST":
        if not g.usuario or g.usuario["perfil"] not in ("admin", "almoxarifado"):
            abort(403)
        if not cargo_atual:
            if request.form.get("ajax") == "1":
                return jsonify(erro="Selecione ou informe um cargo."), 400
            flash("Informe ou selecione o nome do cargo.", "erro")
            return redirect(url_for("cadastros.cargos_padrao"))

        # Modo 1: clique individual via AJAX
        if request.form.get("ajax") == "1":
            epi_id = to_int(request.form.get("epi_id"))
            if not epi_id or not db.execute("SELECT 1 FROM epis WHERE id = ?", (epi_id,)).fetchone():
                return jsonify(erro="EPI inválido."), 400
            if request.form.get("marcado") == "1":
                db.execute(
                    "INSERT OR IGNORE INTO cargo_epis_padrao (cargo, epi_id) VALUES (?, ?)",
                    (cargo_atual, epi_id),
                )
            else:
                db.execute(
                    "DELETE FROM cargo_epis_padrao WHERE cargo = ? COLLATE NOCASE AND epi_id = ?",
                    (cargo_atual, epi_id),
                )
            total = db.execute(
                "SELECT COUNT(*) FROM cargo_epis_padrao WHERE cargo = ? COLLATE NOCASE", (cargo_atual,)
            ).fetchone()[0]
            return jsonify(ok=True, total=total)

        # Modo 2: envio do formulário completo
        ids = {to_int(x) for x in request.form.getlist("epi_id") if to_int(x)}
        validos = {r["id"] for r in db.execute("SELECT id FROM epis")}
        ids &= validos
        db.execute("DELETE FROM cargo_epis_padrao WHERE cargo = ? COLLATE NOCASE", (cargo_atual,))
        for eid in ids:
            db.execute("INSERT INTO cargo_epis_padrao (cargo, epi_id) VALUES (?, ?)", (cargo_atual, eid))
        flash(f"Equipamentos padrão do cargo “{cargo_atual}” salvos para todos os colaboradores da função.", "ok")
        return redirect(url_for("cadastros.cargos_padrao", cargo=cargo_atual))

    epis = estoque.epis_com_saldo(db, somente_ativos=True) if cargo_atual else []
    padrao_ids = _padrao_ids_cargo(db, cargo_atual) if cargo_atual else set()

    resumo_cargos = []
    for c_nome in cargos:
        n_colabs = db.execute(
            "SELECT COUNT(*) FROM colaboradores WHERE ativo = 1 AND funcao = ? COLLATE NOCASE", (c_nome,)
        ).fetchone()[0]
        nomes_epis = [
            r["nome"]
            for r in db.execute(
                """SELECT e.nome FROM cargo_epis_padrao cp
                   JOIN epis e ON e.id = cp.epi_id
                   WHERE cp.cargo = ? COLLATE NOCASE
                   ORDER BY e.nome COLLATE NOCASE""",
                (c_nome,),
            )
        ]
        resumo_cargos.append({
            "nome": c_nome,
            "colaboradores": n_colabs,
            "epis": len(nomes_epis),
            "nomes_epis": nomes_epis,
        })

    return render_template(
        "cargos_padrao.html",
        cargos=cargos,
        resumo_cargos=resumo_cargos,
        cargo_atual=cargo_atual,
        epis=epis,
        padrao_ids=padrao_ids,
    )


def _ler_colaborador(form):
    dados = {k: limpar(form.get(k)) for k in ("nome", "matricula", "setor", "funcao")}
    erros = [] if dados["nome"] else ["Informe o nome do colaborador."]
    return dados, erros


@bp.route("/colaboradores/novo", methods=["GET", "POST"])
@escrita_required
def colaborador_novo():
    dados = {}
    if request.method == "POST":
        dados, erros = _ler_colaborador(request.form)
        if not erros:
            try:
                cur = get_db().execute(
                    "INSERT INTO colaboradores (nome, matricula, setor, funcao) "
                    "VALUES (:nome, :matricula, :setor, :funcao)", dados)
                flash("Colaborador cadastrado.", "ok")
                return redirect(url_for("cadastros.colaborador_ficha", colab_id=cur.lastrowid))
            except sqlite3.IntegrityError:
                erros.append("Já existe um colaborador com essa matrícula.")
        for e in erros:
            flash(e, "erro")
    return render_template("colaborador_form.html", colab=dados, novo=True,
                           setores=_setores(), cargos=_cargos())


@bp.route("/colaboradores/<int:colab_id>/editar", methods=["GET", "POST"])
@escrita_required
def colaborador_editar(colab_id):
    db = get_db()
    colab = db.execute("SELECT * FROM colaboradores WHERE id = ?", (colab_id,)).fetchone() or abort(404)
    if request.method == "POST":
        dados, erros = _ler_colaborador(request.form)
        dados["ativo"] = 1 if request.form.get("ativo") else 0
        dados["id"] = colab_id
        if not erros:
            try:
                db.execute("UPDATE colaboradores SET nome=:nome, matricula=:matricula, setor=:setor, "
                           "funcao=:funcao, ativo=:ativo WHERE id=:id", dados)
                flash("Colaborador atualizado.", "ok")
                return redirect(url_for("cadastros.colaborador_ficha", colab_id=colab_id))
            except sqlite3.IntegrityError:
                erros.append("Já existe um colaborador com essa matrícula.")
        for e in erros:
            flash(e, "erro")
        colab = dados
    return render_template("colaborador_form.html", colab=colab, novo=False, colab_id=colab_id,
                           setores=_setores(), cargos=_cargos(db))


def _solicitados_fora_do_padrao(db, colab_id, padrao_ids):
    """Lista equipamentos pedidos/entregues ao colaborador que não fazem parte do padrão dele,
    mais os lançados manualmente na aba 'Equipamentos solicitados', com suas observações."""
    salvos = {
        r["origem_chave"]: dict(r)
        for r in db.execute(
            "SELECT * FROM colaborador_epis_solicitados WHERE colaborador_id = ? AND origem_chave IS NOT NULL",
            (colab_id,),
        )
    }
    itens = []

    # 1. Itens vindos de Pedidos (Forms / simulação) que não estão no padrão do colaborador
    linhas_ped = db.execute(
        """SELECT i.id AS item_id, i.epi_id, i.quantidade, e.nome AS epi_nome, e.categoria, e.ca, e.unidade,
                  p.id AS pedido_id, substr(p.recebido_em, 1, 10) AS data, p.motivo, p.observacao AS ped_obs
           FROM pedido_itens i
           JOIN pedidos p ON p.id = i.pedido_id
           JOIN epis e ON e.id = i.epi_id
           WHERE p.colaborador_id = ? AND p.status != 'cancelado' AND i.ignorado = 0
           ORDER BY p.id DESC, i.id DESC""",
        (colab_id,),
    ).fetchall()
    for r in linhas_ped:
        if r["epi_id"] in padrao_ids:
            continue
        chave = f"pedido_item:{r['item_id']}"
        reg = salvos.get(chave)
        sugestao = " — ".join(x for x in (r["motivo"], r["ped_obs"]) if x)
        itens.append({
            "reg_id": reg["id"] if reg else None,
            "origem_chave": chave,
            "origem_rotulo": f"Pedido #{r['pedido_id']}",
            "pedido_id": r["pedido_id"],
            "epi_id": r["epi_id"],
            "epi_nome": r["epi_nome"],
            "categoria": r["categoria"],
            "ca": r["ca"],
            "unidade": r["unidade"],
            "quantidade": r["quantidade"],
            "data": r["data"],
            "observacao": reg["observacao"] if reg and reg["observacao"] is not None else sugestao,
            "manual": False,
        })

    # 2. Entregas avulsas (sem pedido vinculado) cujo EPI não está no padrão
    linhas_mov = db.execute(
        """SELECT m.id AS mov_id, m.epi_id, m.quantidade, m.data, m.motivo, m.observacao AS mov_obs,
                  e.nome AS epi_nome, e.categoria, e.ca, e.unidade
           FROM movimentacoes m
           JOIN epis e ON e.id = m.epi_id
           WHERE m.colaborador_id = ? AND m.tipo = 'saida' AND m.cancelada = 0 AND m.pedido_id IS NULL
           ORDER BY m.data DESC, m.id DESC""",
        (colab_id,),
    ).fetchall()
    for r in linhas_mov:
        if r["epi_id"] in padrao_ids:
            continue
        chave = f"mov:{r['mov_id']}"
        reg = salvos.get(chave)
        sugestao = " — ".join(x for x in (r["motivo"], r["mov_obs"]) if x)
        itens.append({
            "reg_id": reg["id"] if reg else None,
            "origem_chave": chave,
            "origem_rotulo": "Entrega avulsa",
            "pedido_id": None,
            "epi_id": r["epi_id"],
            "epi_nome": r["epi_nome"],
            "categoria": r["categoria"],
            "ca": r["ca"],
            "unidade": r["unidade"],
            "quantidade": r["quantidade"],
            "data": r["data"],
            "observacao": reg["observacao"] if reg and reg["observacao"] is not None else sugestao,
            "manual": False,
        })

    # 3. Registros adicionados manualmente na aba "Equipamentos solicitados"
    manuais = db.execute(
        """SELECT s.*, e.nome AS epi_nome, e.categoria, e.ca, e.unidade
           FROM colaborador_epis_solicitados s
           JOIN epis e ON e.id = s.epi_id
           WHERE s.colaborador_id = ? AND s.origem_chave IS NULL
           ORDER BY s.data DESC, s.id DESC""",
        (colab_id,),
    ).fetchall()
    for r in manuais:
        itens.append({
            "reg_id": r["id"],
            "origem_chave": None,
            "origem_rotulo": "Registro no perfil",
            "pedido_id": None,
            "epi_id": r["epi_id"],
            "epi_nome": r["epi_nome"],
            "categoria": r["categoria"],
            "ca": r["ca"],
            "unidade": r["unidade"],
            "quantidade": r["quantidade"],
            "data": r["data"],
            "observacao": r["observacao"] or "",
            "manual": True,
        })

    itens.sort(key=lambda x: (x["data"] or "", x["reg_id"] or 0), reverse=True)
    return itens


@bp.get("/colaboradores/<int:colab_id>")
def colaborador_ficha(colab_id):
    db = get_db()
    colab = db.execute("SELECT * FROM colaboradores WHERE id = ?", (colab_id,)).fetchone() or abort(404)
    aba = request.args.get("aba", "padrao")
    if aba not in ("padrao", "solicitados", "ficha"):
        aba = "padrao"
    epis = estoque.epis_com_saldo(db, somente_ativos=True)
    padrao_ids = _padrao_ids_colaborador(db, colab)
    cargo_colab = (colab["funcao"] or "").strip()
    qtd_no_cargo = (
        db.execute(
            "SELECT COUNT(*) FROM colaboradores WHERE ativo = 1 AND funcao = ? COLLATE NOCASE", (cargo_colab,)
        ).fetchone()[0]
        if cargo_colab
        else 0
    )
    solicitados = _solicitados_fora_do_padrao(db, colab_id, padrao_ids)
    from .movimentos import consultar_movimentos
    entregas = consultar_movimentos(db, {"colaborador_id": colab_id, "tipo": "saida"}, limite=None)
    return render_template(
        "colaborador_ficha.html",
        colab=colab,
        aba=aba,
        epis=epis,
        padrao_ids=padrao_ids,
        qtd_no_cargo=qtd_no_cargo,
        cargos=_cargos(db),
        solicitados=solicitados,
        entregas=entregas,
        hoje_iso=hoje().isoformat(),
    )


@bp.post("/colaboradores/<int:colab_id>/cargo")
@escrita_required
def colaborador_cargo(colab_id):
    """Aloca ou altera rapidamente o cargo do colaborador direto no perfil, puxando o padrão daquele cargo."""
    db = get_db()
    db.execute("SELECT 1 FROM colaboradores WHERE id = ?", (colab_id,)).fetchone() or abort(404)
    novo_cargo = limpar(request.form.get("funcao"))
    db.execute("UPDATE colaboradores SET funcao = ? WHERE id = ?", (novo_cargo, colab_id))
    if novo_cargo:
        flash(f"Cargo atualizado para “{novo_cargo}”. O padrão de EPIs deste cargo já foi aplicado automaticamente.", "ok")
    else:
        flash("Cargo removido do colaborador.", "ok")
    return redirect(url_for("cadastros.colaborador_ficha", colab_id=colab_id, aba="padrao"))


@bp.post("/colaboradores/<int:colab_id>/padrao")
@escrita_required
def colaborador_padrao(colab_id):
    db = get_db()
    colab = db.execute("SELECT * FROM colaboradores WHERE id = ?", (colab_id,)).fetchone() or abort(404)
    cargo = (colab["funcao"] or "").strip()

    # Modo 1: clique individual de checkbox via fetch (AJAX)
    if request.form.get("ajax") == "1":
        epi_id = to_int(request.form.get("epi_id"))
        if not epi_id or not db.execute("SELECT 1 FROM epis WHERE id = ?", (epi_id,)).fetchone():
            return jsonify(erro="EPI inválido."), 400
        if cargo:
            if request.form.get("marcado") == "1":
                db.execute("INSERT OR IGNORE INTO cargo_epis_padrao (cargo, epi_id) VALUES (?, ?)", (cargo, epi_id))
            else:
                db.execute("DELETE FROM cargo_epis_padrao WHERE cargo = ? COLLATE NOCASE AND epi_id = ?", (cargo, epi_id))
            total = db.execute("SELECT COUNT(*) FROM cargo_epis_padrao WHERE cargo = ? COLLATE NOCASE", (cargo,)).fetchone()[0]
        else:
            if request.form.get("marcado") == "1":
                db.execute(
                    "INSERT OR IGNORE INTO colaborador_epis_padrao (colaborador_id, epi_id) VALUES (?, ?)",
                    (colab_id, epi_id),
                )
            else:
                db.execute(
                    "DELETE FROM colaborador_epis_padrao WHERE colaborador_id = ? AND epi_id = ?",
                    (colab_id, epi_id),
                )
            total = db.execute(
                "SELECT COUNT(*) FROM colaborador_epis_padrao WHERE colaborador_id = ?", (colab_id,)
            ).fetchone()[0]
        return jsonify(ok=True, total=total)

    # Modo 2: envio do formulário completo
    ids = {to_int(x) for x in request.form.getlist("epi_id") if to_int(x)}
    validos = {r["id"] for r in db.execute("SELECT id FROM epis")}
    ids &= validos
    if cargo:
        db.execute("DELETE FROM cargo_epis_padrao WHERE cargo = ? COLLATE NOCASE", (cargo,))
        for eid in ids:
            db.execute("INSERT INTO cargo_epis_padrao (cargo, epi_id) VALUES (?, ?)", (cargo, eid))
        flash(f"Padrão de equipamentos do cargo “{cargo}” salvo.", "ok")
    else:
        db.execute("DELETE FROM colaborador_epis_padrao WHERE colaborador_id = ?", (colab_id,))
        for eid in ids:
            db.execute(
                "INSERT INTO colaborador_epis_padrao (colaborador_id, epi_id) VALUES (?, ?)",
                (colab_id, eid),
            )
        flash("Equipamentos utilizados por padrão salvos.", "ok")
    return redirect(url_for("cadastros.colaborador_ficha", colab_id=colab_id, aba="padrao"))


@bp.post("/colaboradores/<int:colab_id>/solicitados/novo")
@escrita_required
def colaborador_solicitado_novo(colab_id):
    db = get_db()
    db.execute("SELECT 1 FROM colaboradores WHERE id = ?", (colab_id,)).fetchone() or abort(404)
    epi_id = to_int(request.form.get("epi_id"))
    qtd = to_int(request.form.get("quantidade"), 1)
    data = parse_data(request.form.get("data")) or hoje()
    obs = limpar(request.form.get("observacao"))
    if not epi_id or not db.execute("SELECT 1 FROM epis WHERE id = ?", (epi_id,)).fetchone():
        flash("Selecione um equipamento válido do inventário.", "erro")
    elif not qtd or qtd <= 0:
        flash("Informe uma quantidade válida.", "erro")
    elif not obs:
        flash("Descreva na observação o motivo pelo qual este equipamento fora do padrão foi solicitado.", "erro")
    else:
        db.execute(
            """INSERT INTO colaborador_epis_solicitados
               (colaborador_id, epi_id, quantidade, data, observacao, usuario_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (colab_id, epi_id, qtd, data.isoformat(), obs, g.usuario["id"]),
        )
        flash("Equipamento solicitado fora do padrão registrado com sua observação.", "ok")
    return redirect(url_for("cadastros.colaborador_ficha", colab_id=colab_id, aba="solicitados"))


@bp.post("/colaboradores/<int:colab_id>/solicitados/observacao")
@escrita_required
def colaborador_solicitado_obs(colab_id):
    db = get_db()
    db.execute("SELECT 1 FROM colaboradores WHERE id = ?", (colab_id,)).fetchone() or abort(404)
    reg_id = to_int(request.form.get("reg_id"))
    origem_chave = limpar(request.form.get("origem_chave"))
    obs = limpar(request.form.get("observacao")) or ""
    if reg_id:
        db.execute(
            "UPDATE colaborador_epis_solicitados SET observacao = ?, usuario_id = ? "
            "WHERE id = ? AND colaborador_id = ?",
            (obs, g.usuario["id"], reg_id, colab_id),
        )
        flash("Observação atualizada.", "ok")
    elif origem_chave:
        epi_id = to_int(request.form.get("epi_id"))
        qtd = to_int(request.form.get("quantidade"), 1) or 1
        data = (parse_data(request.form.get("data")) or hoje()).isoformat()
        if epi_id:
            db.execute(
                """INSERT INTO colaborador_epis_solicitados
                   (colaborador_id, epi_id, quantidade, data, origem_chave, observacao, usuario_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (origem_chave) DO UPDATE SET observacao = excluded.observacao,
                                                           usuario_id = excluded.usuario_id""",
                (colab_id, epi_id, qtd, data, origem_chave, obs, g.usuario["id"]),
            )
            flash("Observação salva.", "ok")
    return redirect(url_for("cadastros.colaborador_ficha", colab_id=colab_id, aba="solicitados"))


@bp.post("/colaboradores/<int:colab_id>/solicitados/<int:reg_id>/remover")
@escrita_required
def colaborador_solicitado_remover(colab_id, reg_id):
    get_db().execute(
        "DELETE FROM colaborador_epis_solicitados WHERE id = ? AND colaborador_id = ? AND origem_chave IS NULL",
        (reg_id, colab_id),
    )
    flash("Registro removido.", "ok")
    return redirect(url_for("cadastros.colaborador_ficha", colab_id=colab_id, aba="solicitados"))


# --------------------------------------------------------------- usuários

@bp.get("/usuarios")
@admin_required
def usuarios_lista():
    usuarios = get_db().execute("SELECT * FROM usuarios ORDER BY nome COLLATE NOCASE").fetchall()
    return render_template("usuarios_lista.html", usuarios=usuarios, PERFIS=PERFIS)


@bp.route("/usuarios/novo", methods=["GET", "POST"])
@admin_required
def usuario_novo():
    dados = {"perfil": "almoxarifado"}
    if request.method == "POST":
        dados = {"nome": limpar(request.form.get("nome")), "login": limpar(request.form.get("login")),
                 "perfil": request.form.get("perfil")}
        senha = request.form.get("senha", "")
        erro = _senha_valida(senha, request.form.get("confirmacao", ""))
        if not dados["nome"] or not dados["login"]:
            erro = "Informe nome e login."
        elif dados["perfil"] not in PERFIS:
            erro = "Perfil inválido."
        if erro:
            flash(erro, "erro")
        else:
            try:
                get_db().execute(
                    "INSERT INTO usuarios (nome, login, senha_hash, perfil) VALUES (?, ?, ?, ?)",
                    (dados["nome"], dados["login"], generate_password_hash(senha), dados["perfil"]))
                flash("Usuário criado.", "ok")
                return redirect(url_for("cadastros.usuarios_lista"))
            except sqlite3.IntegrityError:
                flash("Esse login já está em uso.", "erro")
    return render_template("usuario_form.html", u=dados, novo=True, PERFIS=PERFIS, SENHA_MIN=SENHA_MIN)


@bp.route("/usuarios/<int:uid>/editar", methods=["GET", "POST"])
@admin_required
def usuario_editar(uid):
    db = get_db()
    u = db.execute("SELECT * FROM usuarios WHERE id = ?", (uid,)).fetchone() or abort(404)
    if request.method == "POST":
        perfil = request.form.get("perfil")
        ativo = 1 if request.form.get("ativo") else 0
        nome = limpar(request.form.get("nome"))
        senha = request.form.get("senha", "")
        erro = None
        if not nome or perfil not in PERFIS:
            erro = "Informe o nome e um perfil válido."
        elif uid == g.usuario["id"] and (perfil != "admin" or not ativo):
            erro = "Você não pode remover seu próprio acesso de administrador."
        elif senha:
            erro = _senha_valida(senha, request.form.get("confirmacao", ""))
        if erro:
            flash(erro, "erro")
        else:
            db.execute("UPDATE usuarios SET nome = ?, perfil = ?, ativo = ? WHERE id = ?",
                       (nome, perfil, ativo, uid))
            if senha:
                db.execute("UPDATE usuarios SET senha_hash = ? WHERE id = ?",
                           (generate_password_hash(senha), uid))
            flash("Usuário atualizado.", "ok")
            return redirect(url_for("cadastros.usuarios_lista"))
    return render_template("usuario_form.html", u=u, novo=False, PERFIS=PERFIS, SENHA_MIN=SENHA_MIN)
