"""Cadastros: EPIs, colaboradores e usuários."""
import csv
import io
import sqlite3

from flask import (Blueprint, Response, abort, flash, g, jsonify, redirect, render_template,
                   request, url_for)
from werkzeug.security import generate_password_hash

from . import caepi, estoque, pedidos
from .auth import (
    CATALOGO_PERMISSOES,
    SENHA_MIN,
    TODAS_PERMISSOES,
    _senha_valida,
    admin_required,
    escrita_required,
    gerar_chave_perfil,
    obter_perfis_dict,
    permissoes_do_perfil,
    usuario_tem_permissao,
)
from .db import get_db
from .util import hoje, limpar, parse_data, to_int

bp = Blueprint("cadastros", __name__)


# ------------------------------------------------------------------ EPIs

@bp.get("/epis")
def epis_lista():
    db = get_db()
    caepi.verificar_sincronizacao_diaria_20h(db)
    mostrar_inativos = request.args.get("inativos") == "1"
    busca = (request.args.get("q") or "").strip().lower()
    epis = estoque.epis_com_saldo(db, somente_ativos=not mostrar_inativos)
    if busca:
        epis = [e for e in epis if busca in e["nome"].lower() or busca in (e["categoria"] or "").lower()
                or busca in (e["ca"] or "").lower()]
    return render_template(
        "epis_lista.html",
        epis=epis,
        busca=busca,
        mostrar_inativos=mostrar_inativos,
        cfg_caepi=caepi.obter_config_caepi(db),
    )


@bp.get("/api/caepi/<ca_num>")
def api_caepi(ca_num):
    """Consulta automática de um Certificado de Aprovação (CA) na API_BaseCAEPI / Base CAEPI."""
    info = caepi.consultar_ca(get_db(), ca_num)
    if not info:
        num = caepi.normalizar_ca(ca_num) or limpar(ca_num) or ""
        return jsonify(ok=False, erro=f"CA {num} não encontrado na Base CAEPI."), 404
    return jsonify(ok=True, dados=info)


@bp.post("/epis/sincronizar-caepi")
@escrita_required
def epis_sincronizar_caepi():
    """Sincroniza as validades e informações dos CAs com a API_BaseCAEPI / arquivo oficial do MTE."""
    db = get_db()
    if "api_url" in request.form:
        caepi.salvar_config_caepi(db, api_url=limpar(request.form.get("api_url")) or "")

    arq = request.files.get("arquivo_caepi")
    try:
        if arq and arq.filename:
            n_cas, n_epis = caepi.importar_arquivo_caepi(db, arq.read(), arq.filename)
            flash(f"Base CAEPI importada ({n_cas} CAs processados) e {n_epis} EPI(s) atualizado(s) automaticamente!", "ok")
        else:
            tentar_ftp = request.form.get("baixar_mte") == "1"
            total_base, n_epis = caepi.sincronizar_cas_estoque(db, tentar_ftp_mte=tentar_ftp)
            flash(f"Sincronização CAEPI concluída ({total_base} CAs na base · {n_epis} EPI(s) tiveram validade/categoria atualizada).", "ok")
    except estoque.ErroValidacao as e:
        flash(str(e), "erro")
    return redirect(url_for("cadastros.epis_lista"))


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


def _ler_epi(form, files=None, imagem_atual=None):
    erros = []
    ca_digitado = limpar(form.get("ca"))
    ca_norm = caepi.normalizar_ca(ca_digitado) or ca_digitado
    info_ca = caepi.consultar_ca(get_db(), ca_norm) if ca_norm else None

    nome_form = limpar(form.get("nome"))
    categoria_form = limpar(form.get("categoria"))
    unidade_form = limpar(form.get("unidade"))
    validade_form = limpar(form.get("validade_ca"))

    # Se o usuário informou apenas o Nº do CA, puxa automaticamente Nome, Categoria, Unidade e Validade da Base CAEPI
    if info_ca:
        if not nome_form:
            nome_form = limpar(info_ca.get("nome_sugerido")) or limpar(info_ca.get("nome_equipamento"))
        if not categoria_form:
            categoria_form = limpar(info_ca.get("categoria"))
        if not validade_form:
            validade_form = limpar(info_ca.get("validade_ca"))
        if (not unidade_form or unidade_form == "un") and info_ca.get("unidade"):
            unidade_form = info_ca["unidade"]

    dados = {
        "nome": nome_form,
        "categoria": categoria_form,
        "ca": ca_norm,
        "unidade": unidade_form or "un",
        "estoque_minimo": to_int(form.get("estoque_minimo"), 0),
        "prazo_reposicao_dias": to_int(form.get("prazo_reposicao_dias"), 15),
        "exibir_no_forms": 0 if form.get("exibir_no_forms") == "0" else (1 if form.get("exibir_no_forms") or "presenca_forms" not in form else 0),
    }
    if validade_form and not parse_data(validade_form):
        erros.append("Validade do CA inválida.")
    dados["validade_ca"] = validade_form
    if not dados["nome"]:
        erros.append("Informe o nome do EPI (ou digite um Nº de CA válido para preencher automaticamente).")
    if dados["estoque_minimo"] < 0 or dados["prazo_reposicao_dias"] < 0:
        erros.append("Estoque mínimo e prazo de reposição não podem ser negativos.")

    arq_img = files.get("imagem") if files else None
    url_img = form.get("imagem_url")
    if form.get("remover_imagem") == "1":
        dados["imagem_url"] = None
    elif (arq_img and getattr(arq_img, "filename", "")) or (url_img is not None and url_img.strip()):
        try:
            dados["imagem_url"] = pedidos.processar_imagem_epi(arq_img, url_img)
        except estoque.ErroValidacao as e:
            erros.append(str(e))
            dados["imagem_url"] = imagem_atual
    else:
        dados["imagem_url"] = imagem_atual
    return dados, erros


@bp.route("/epis/novo", methods=["GET", "POST"])
@escrita_required
def epi_novo():
    dados = {"unidade": "un", "estoque_minimo": 0, "prazo_reposicao_dias": 15, "exibir_no_forms": 1, "imagem_url": None}
    if request.method == "POST":
        dados, erros = _ler_epi(request.form, request.files)
        if not erros:
            try:
                cur = get_db().execute(
                    """INSERT INTO epis (nome, categoria, ca, validade_ca, unidade, estoque_minimo,
                                         prazo_reposicao_dias, imagem_url, exibir_no_forms)
                       VALUES (:nome, :categoria, :ca, :validade_ca, :unidade, :estoque_minimo,
                               :prazo_reposicao_dias, :imagem_url, :exibir_no_forms)""", dados)
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
        dados, erros = _ler_epi(request.form, request.files, imagem_atual=epi["imagem_url"])
        dados["ativo"] = 1 if request.form.get("ativo") else 0
        dados["id"] = epi_id
        if not erros:
            try:
                db.execute(
                    """UPDATE epis SET nome=:nome, categoria=:categoria, ca=:ca, validade_ca=:validade_ca,
                       unidade=:unidade, estoque_minimo=:estoque_minimo,
                       prazo_reposicao_dias=:prazo_reposicao_dias, imagem_url=:imagem_url,
                       exibir_no_forms=:exibir_no_forms, ativo=:ativo WHERE id=:id""", dados)
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


# ---------------------------------------------------------- fornecedores

def _ler_fornecedor(form):
    campos_texto = (
        "nome",
        "razao_social",
        "cnpj",
        "inscricao_estadual",
        "contato_nome",
        "telefone",
        "email",
        "endereco",
        "cidade_uf",
        "cep",
        "condicao_pagamento",
        "categorias_fornecidas",
        "observacao",
    )
    dados = {k: limpar(form.get(k)) for k in campos_texto}
    dados["prazo_entrega_dias"] = to_int(form.get("prazo_entrega_dias"), 15)
    erros = []
    if not dados["nome"]:
        erros.append("Informe o nome fantasia / nome do fornecedor.")
    if dados["prazo_entrega_dias"] is not None and dados["prazo_entrega_dias"] < 0:
        erros.append("O prazo médio de entrega não pode ser negativo.")
    return dados, erros


@bp.get("/fornecedores")
def fornecedores_lista():
    db = get_db()
    mostrar_inativos = request.args.get("inativos") == "1"
    busca = (request.args.get("q") or "").strip()
    sql = "SELECT * FROM fornecedores WHERE 1=1"
    params = []
    if not mostrar_inativos:
        sql += " AND ativo = 1"
    if busca:
        sql += (
            " AND (nome LIKE ? OR razao_social LIKE ? OR cnpj LIKE ? "
            "OR cidade_uf LIKE ? OR contato_nome LIKE ? OR categorias_fornecidas LIKE ?)"
        )
        params += [f"%{busca}%"] * 6
    fornecedores = db.execute(sql + " ORDER BY nome COLLATE NOCASE", params).fetchall()

    resumo_entradas = {}
    for r in db.execute(
        """SELECT LOWER(trim(fornecedor)) AS chave,
                  COUNT(DISTINCT grupo) AS entradas,
                  SUM(quantidade) AS itens_comprados,
                  SUM(COALESCE(custo_unitario, 0) * quantidade) AS valor_total
           FROM movimentacoes
           WHERE tipo = 'entrada' AND cancelada = 0 AND fornecedor IS NOT NULL AND trim(fornecedor) != ''
           GROUP BY LOWER(trim(fornecedor))"""
    ):
        resumo_entradas[r["chave"]] = r

    return render_template(
        "fornecedores_lista.html",
        fornecedores=fornecedores,
        resumo_entradas=resumo_entradas,
        busca=busca,
        mostrar_inativos=mostrar_inativos,
    )


@bp.route("/fornecedores/novo", methods=["GET", "POST"])
@escrita_required
def fornecedor_novo():
    dados = {"prazo_entrega_dias": 15}
    if request.method == "POST":
        dados, erros = _ler_fornecedor(request.form)
        if not erros:
            try:
                cur = get_db().execute(
                    """INSERT INTO fornecedores (
                           nome, razao_social, cnpj, inscricao_estadual,
                           contato_nome, telefone, email, endereco, cidade_uf, cep,
                           prazo_entrega_dias, condicao_pagamento, categorias_fornecidas, observacao
                       ) VALUES (
                           :nome, :razao_social, :cnpj, :inscricao_estadual,
                           :contato_nome, :telefone, :email, :endereco, :cidade_uf, :cep,
                           :prazo_entrega_dias, :condicao_pagamento, :categorias_fornecidas, :observacao
                       )""",
                    dados,
                )
                flash("Fornecedor cadastrado com sucesso.", "ok")
                return redirect(url_for("cadastros.fornecedor_detalhe", forn_id=cur.lastrowid))
            except sqlite3.IntegrityError:
                erros.append("Já existe um fornecedor cadastrado com esse nome ou CNPJ.")
        for e in erros:
            flash(e, "erro")
    return render_template("fornecedor_form.html", f=dados, novo=True)


@bp.route("/fornecedores/<int:forn_id>/editar", methods=["GET", "POST"])
@escrita_required
def fornecedor_editar(forn_id):
    db = get_db()
    forn = db.execute("SELECT * FROM fornecedores WHERE id = ?", (forn_id,)).fetchone() or abort(404)
    if request.method == "POST":
        dados, erros = _ler_fornecedor(request.form)
        dados["ativo"] = 1 if request.form.get("ativo") else 0
        dados["id"] = forn_id
        if not erros:
            try:
                db.execute(
                    """UPDATE fornecedores SET
                           nome=:nome, razao_social=:razao_social, cnpj=:cnpj,
                           inscricao_estadual=:inscricao_estadual, contato_nome=:contato_nome,
                           telefone=:telefone, email=:email, endereco=:endereco,
                           cidade_uf=:cidade_uf, cep=:cep, prazo_entrega_dias=:prazo_entrega_dias,
                           condicao_pagamento=:condicao_pagamento,
                           categorias_fornecidas=:categorias_fornecidas,
                           observacao=:observacao, ativo=:ativo
                       WHERE id=:id""",
                    dados,
                )
                flash("Dados do fornecedor atualizados.", "ok")
                return redirect(url_for("cadastros.fornecedor_detalhe", forn_id=forn_id))
            except sqlite3.IntegrityError:
                erros.append("Já existe outro fornecedor cadastrado com esse nome ou CNPJ.")
        for e in erros:
            flash(e, "erro")
        forn = dados
    return render_template("fornecedor_form.html", f=forn, novo=False, forn_id=forn_id)


@bp.get("/fornecedores/<int:forn_id>")
def fornecedor_detalhe(forn_id):
    db = get_db()
    forn = db.execute("SELECT * FROM fornecedores WHERE id = ?", (forn_id,)).fetchone() or abort(404)
    entradas = db.execute(
        """SELECT m.*, e.nome AS epi_nome, e.unidade, u.nome AS usuario_nome, uc.nome AS cancelada_por_nome
           FROM movimentacoes m
           JOIN epis e ON e.id = m.epi_id
           JOIN usuarios u ON u.id = m.usuario_id
           LEFT JOIN usuarios uc ON uc.id = m.cancelada_por
           WHERE m.tipo = 'entrada' AND m.fornecedor = ? COLLATE NOCASE
           ORDER BY m.data DESC, m.id DESC""",
        (forn["nome"],),
    ).fetchall()
    ativas = [m for m in entradas if not m["cancelada"]]
    total_itens = sum(m["quantidade"] for m in ativas)
    total_valor = sum((m["custo_unitario"] or 0) * m["quantidade"] for m in ativas)
    total_notas = len({m["grupo"] for m in ativas})
    return render_template(
        "fornecedor_detalhe.html",
        f=forn,
        entradas=entradas,
        total_itens=total_itens,
        total_valor=total_valor,
        total_notas=total_notas,
    )


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
        if not usuario_tem_permissao("editar_colaboradores"):
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


NUMEROS_CALCADO = tuple(str(n) for n in range(33, 49))


def _obter_tamanhos_colaborador(db, colab_id):
    return {
        r["chave"].lower(): r["tamanho"]
        for r in db.execute(
            "SELECT chave, tamanho FROM colaborador_tamanhos WHERE colaborador_id = ?", (colab_id,)
        )
    }


def _salvar_tamanho_item(db, colab_id, epi_row, valor):
    valor = (valor or "").strip().upper()
    tc = pedidos.tipo_calcado(epi_row["nome"])
    tu = pedidos.tipo_uniforme(epi_row["nome"])
    if not tc and not tu:
        return None
    if tc and valor and not valor.isdigit():
        return f"A numeração de {epi_row['nome']} deve conter somente números."
    if tu and valor and valor not in pedidos.TAMANHOS_UNIFORME:
        return f"Tamanho inválido para {epi_row['nome']}."

    chaves = [f"epi:{epi_row['id']}", f"tipo:{tc or tu}"]
    for ch in chaves:
        db.execute("DELETE FROM colaborador_tamanhos WHERE colaborador_id = ? AND chave = ? COLLATE NOCASE", (colab_id, ch))
        if valor:
            db.execute(
                "INSERT INTO colaborador_tamanhos (colaborador_id, chave, tamanho) VALUES (?, ?, ?)",
                (colab_id, ch, valor),
            )
    return None


@bp.get("/colaboradores/<int:colab_id>")
def colaborador_ficha(colab_id):
    db = get_db()
    colab = db.execute("SELECT * FROM colaboradores WHERE id = ?", (colab_id,)).fetchone() or abort(404)
    aba = request.args.get("aba", "padrao")
    if aba not in ("padrao", "solicitados", "ficha"):
        aba = "padrao"
    epis = estoque.epis_com_saldo(db, somente_ativos=True)
    padrao_ids = _padrao_ids_colaborador(db, colab)
    tamanhos_salvos = _obter_tamanhos_colaborador(db, colab_id)

    epis_padrao = []
    for e in epis:
        if e["id"] not in padrao_ids:
            continue
        item = dict(e)
        tc = pedidos.tipo_calcado(e["nome"])
        tu = pedidos.tipo_uniforme(e["nome"])
        if tc:
            item["tipo_seletor"] = "numero"
            item["subtipo_tam"] = {"sapatao": "Sapatão", "botina": "Botina", "bota": "Bota"}.get(tc, "Calçado")
            item["tamanho_atual"] = tamanhos_salvos.get(f"epi:{e['id']}", tamanhos_salvos.get(f"tipo:{tc}", ""))
        elif tu:
            item["tipo_seletor"] = "letra"
            item["subtipo_tam"] = {"camiseta": "Camiseta", "calca": "Calça"}.get(tu, "Uniforme")
            item["tamanho_atual"] = tamanhos_salvos.get(f"epi:{e['id']}", tamanhos_salvos.get(f"tipo:{tu}", ""))
        else:
            item["tipo_seletor"] = None
            item["subtipo_tam"] = None
            item["tamanho_atual"] = ""
        epis_padrao.append(item)

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
        epis_padrao=epis_padrao,
        padrao_ids=padrao_ids,
        numeros_calcado=NUMEROS_CALCADO,
        tamanhos_uniforme=pedidos.TAMANHOS_UNIFORME,
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


@bp.post("/colaboradores/<int:colab_id>/tamanhos")
@escrita_required
def colaborador_tamanhos(colab_id):
    """Salva a numeração de calçados (Sapatão, Botina, Bota) e tamanho de uniformes (Camiseta, Calça) do colaborador."""
    db = get_db()
    db.execute("SELECT 1 FROM colaboradores WHERE id = ?", (colab_id,)).fetchone() or abort(404)

    if request.form.get("ajax") == "1":
        epi_id = to_int(request.form.get("epi_id"))
        epi_row = db.execute("SELECT id, nome FROM epis WHERE id = ?", (epi_id,)).fetchone() if epi_id else None
        if not epi_row:
            return jsonify(erro="Equipamento inválido."), 400
        erro = _salvar_tamanho_item(db, colab_id, epi_row, request.form.get("tamanho"))
        if erro:
            return jsonify(erro=erro), 400
        return jsonify(ok=True)

    erros = []
    for chave_form, valor in request.form.items():
        if not chave_form.startswith("tam_epi_"):
            continue
        epi_id = to_int(chave_form[len("tam_epi_"):])
        epi_row = db.execute("SELECT id, nome FROM epis WHERE id = ?", (epi_id,)).fetchone() if epi_id else None
        if epi_row:
            erro = _salvar_tamanho_item(db, colab_id, epi_row, valor)
            if erro:
                erros.append(erro)
    if erros:
        for er in erros:
            flash(er, "erro")
    else:
        flash("Tamanhos e numerações do colaborador salvos com sucesso.", "ok")
    return redirect(url_for("cadastros.colaborador_ficha", colab_id=colab_id, aba="padrao"))


@bp.post("/colaboradores/<int:colab_id>/padrao")
@escrita_required
def colaborador_padrao(colab_id):
    """A alteração de quais produtos fazem parte do padrão é feita somente por Cargo (/colaboradores/cargos)."""
    if request.form.get("ajax") == "1":
        return jsonify(erro="A alteração dos equipamentos padrão é feita somente na aba de Cargos."), 403
    flash("A alteração dos equipamentos utilizados por padrão deve ser feita diretamente no Cargo.", "erro")
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


# --------------------------------------------------------------- usuários e perfis

def _listar_perfis_detalhados(db):
    contagem = {
        r["perfil"].casefold(): r["n"]
        for r in db.execute("SELECT perfil, COUNT(*) AS n FROM usuarios GROUP BY perfil")
    }
    perfis = []
    for r in db.execute("SELECT * FROM perfis_acesso ORDER BY sistema DESC, nome COLLATE NOCASE"):
        perms = permissoes_do_perfil(r["chave"], db)
        perfis.append({
            "chave": r["chave"],
            "nome": r["nome"],
            "descricao": r["descricao"] or "",
            "sistema": bool(r["sistema"]),
            "permissoes": perms,
            "qtd_usuarios": contagem.get(r["chave"].casefold(), 0),
        })
    return perfis


@bp.get("/usuarios")
@admin_required
def usuarios_lista():
    db = get_db()
    aba = request.args.get("aba", "usuarios")
    perfil_chave = limpar(request.args.get("perfil"))
    novo_perfil = request.args.get("novo") == "1"
    if perfil_chave or novo_perfil:
        aba = "perfis"
    if aba not in ("usuarios", "perfis"):
        aba = "usuarios"
    usuarios = db.execute("SELECT * FROM usuarios ORDER BY nome COLLATE NOCASE").fetchall()
    perfis_dict = obter_perfis_dict(db)
    perfis_detalhados = _listar_perfis_detalhados(db)
    perfil_aberto = None
    if perfil_chave:
        perfil_aberto = next(
            (p for p in perfis_detalhados if p["chave"].casefold() == perfil_chave.casefold()),
            None,
        )
    return render_template(
        "usuarios_lista.html",
        usuarios=usuarios,
        PERFIS=perfis_dict,
        perfis_detalhados=perfis_detalhados,
        catalogo_permissoes=CATALOGO_PERMISSOES,
        aba=aba,
        perfil_aberto=perfil_aberto,
        novo_perfil=novo_perfil,
    )


@bp.post("/usuarios/perfis/novo")
@admin_required
def perfil_novo():
    db = get_db()
    nome = limpar(request.form.get("nome"))
    descricao = limpar(request.form.get("descricao")) or ""
    perms_marcadas = [p for p in request.form.getlist("permissoes") if p in TODAS_PERMISSOES]
    if not nome:
        flash("Informe o nome do novo perfil.", "erro")
        return redirect(url_for("cadastros.usuarios_lista", aba="perfis", novo="1"))
    chave = gerar_chave_perfil(nome)
    existe = db.execute(
        "SELECT 1 FROM perfis_acesso WHERE chave = ? COLLATE NOCASE OR nome = ? COLLATE NOCASE",
        (chave, nome),
    ).fetchone()
    if existe:
        flash(f"Já existe um perfil chamado “{nome}”.", "erro")
        return redirect(url_for("cadastros.usuarios_lista", aba="perfis", novo="1"))
    if not perms_marcadas:
        perms_marcadas = ["ver_painel"]
    db.execute(
        "INSERT INTO perfis_acesso (chave, nome, descricao, permissoes, sistema) VALUES (?, ?, ?, ?, 0)",
        (chave, nome, descricao, ",".join(perms_marcadas)),
    )
    flash(f"Perfil “{nome}” criado com sucesso! Você já pode atribuí-lo aos usuários.", "ok")
    return redirect(url_for("cadastros.usuarios_lista", aba="perfis", perfil=chave))


@bp.post("/usuarios/perfis/<chave>/editar")
@admin_required
def perfil_editar(chave):
    db = get_db()
    perfil_row = db.execute("SELECT * FROM perfis_acesso WHERE chave = ? COLLATE NOCASE", (chave,)).fetchone() or abort(404)
    chave_real = perfil_row["chave"]
    if chave_real.lower() == "admin":
        flash("O perfil Administrador possui acesso total permanente ao sistema.", "erro")
        return redirect(url_for("cadastros.usuarios_lista", aba="perfis", perfil=chave_real))
    nome = limpar(request.form.get("nome")) or perfil_row["nome"]
    descricao = limpar(request.form.get("descricao")) if "descricao" in request.form else (perfil_row["descricao"] or "")
    perms_marcadas = [p for p in request.form.getlist("permissoes") if p in TODAS_PERMISSOES]
    db.execute(
        "UPDATE perfis_acesso SET nome = ?, descricao = ?, permissoes = ? WHERE chave = ? COLLATE NOCASE",
        (nome, descricao or "", ",".join(perms_marcadas), chave_real),
    )
    flash(f"Permissões do perfil “{nome}” atualizadas com sucesso.", "ok")
    return redirect(url_for("cadastros.usuarios_lista", aba="perfis", perfil=chave_real))


@bp.post("/usuarios/perfis/<chave>/remover")
@admin_required
def perfil_remover(chave):
    db = get_db()
    perfil_row = db.execute("SELECT * FROM perfis_acesso WHERE chave = ? COLLATE NOCASE", (chave,)).fetchone() or abort(404)
    if perfil_row["sistema"]:
        flash("Perfis padrão do sistema não podem ser excluídos (você pode ajustar as permissões deles).", "erro")
        return redirect(url_for("cadastros.usuarios_lista", aba="perfis"))
    em_uso = db.execute("SELECT COUNT(*) FROM usuarios WHERE perfil = ? COLLATE NOCASE", (perfil_row["chave"],)).fetchone()[0]
    if em_uso:
        flash(f"Não é possível excluir o perfil “{perfil_row['nome']}” pois há {em_uso} usuário(s) vinculado(s) a ele.", "erro")
        return redirect(url_for("cadastros.usuarios_lista", aba="perfis"))
    db.execute("DELETE FROM perfis_acesso WHERE chave = ? COLLATE NOCASE", (perfil_row["chave"],))
    flash(f"Perfil “{perfil_row['nome']}” removido.", "ok")
    return redirect(url_for("cadastros.usuarios_lista", aba="perfis"))


@bp.route("/usuarios/novo", methods=["GET", "POST"])
@admin_required
def usuario_novo():
    db = get_db()
    perfis_dict = obter_perfis_dict(db)
    dados = {"perfil": "almoxarifado"}
    if request.method == "POST":
        dados = {"nome": limpar(request.form.get("nome")), "login": limpar(request.form.get("login")),
                 "perfil": request.form.get("perfil")}
        senha = request.form.get("senha", "")
        erro = _senha_valida(senha, request.form.get("confirmacao", ""))
        if not dados["nome"] or not dados["login"]:
            erro = "Informe nome e login."
        elif dados["perfil"] not in perfis_dict:
            erro = "Perfil inválido."
        if erro:
            flash(erro, "erro")
        else:
            try:
                db.execute(
                    "INSERT INTO usuarios (nome, login, senha_hash, perfil) VALUES (?, ?, ?, ?)",
                    (dados["nome"], dados["login"], generate_password_hash(senha), dados["perfil"]))
                flash("Usuário criado.", "ok")
                return redirect(url_for("cadastros.usuarios_lista"))
            except sqlite3.IntegrityError:
                flash("Esse login já está em uso.", "erro")
    return render_template("usuario_form.html", u=dados, novo=True, PERFIS=perfis_dict, SENHA_MIN=SENHA_MIN)


@bp.route("/usuarios/<int:uid>/editar", methods=["GET", "POST"])
@admin_required
def usuario_editar(uid):
    db = get_db()
    u = db.execute("SELECT * FROM usuarios WHERE id = ?", (uid,)).fetchone() or abort(404)
    perfis_dict = obter_perfis_dict(db)
    if request.method == "POST":
        perfil = request.form.get("perfil")
        ativo = 1 if request.form.get("ativo") else 0
        nome = limpar(request.form.get("nome"))
        novo_login = limpar(request.form.get("login")) or u["login"]
        senha = request.form.get("senha", "")
        erro = None
        if not nome or not novo_login or perfil not in perfis_dict:
            erro = "Informe o nome, o login e um perfil válido."
        elif uid == g.usuario["id"] and (perfil != "admin" or not ativo):
            erro = "Você não pode remover seu próprio acesso de administrador."
        elif senha:
            erro = _senha_valida(senha, request.form.get("confirmacao", ""))
        if erro:
            flash(erro, "erro")
            u = {"id": uid, "nome": nome, "login": novo_login, "perfil": perfil, "ativo": ativo}
        else:
            try:
                db.execute("UPDATE usuarios SET nome = ?, login = ?, perfil = ?, ativo = ? WHERE id = ?",
                           (nome, novo_login, perfil, ativo, uid))
                if senha:
                    db.execute("UPDATE usuarios SET senha_hash = ? WHERE id = ?",
                               (generate_password_hash(senha), uid))
                flash("Usuário atualizado com sucesso.", "ok")
                return redirect(url_for("cadastros.usuarios_lista"))
            except sqlite3.IntegrityError:
                flash("Esse login já está em uso por outro usuário.", "erro")
                u = {"id": uid, "nome": nome, "login": novo_login, "perfil": perfil, "ativo": ativo}
    return render_template("usuario_form.html", u=u, novo=False, PERFIS=perfis_dict, SENHA_MIN=SENHA_MIN)
