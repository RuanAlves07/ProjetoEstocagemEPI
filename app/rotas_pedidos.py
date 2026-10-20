"""Pedidos de EPI: API para o Power Automate, cards de novos pedidos, separação e retirada."""
import csv
import hmac
import io

from flask import (Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template,
                   request, url_for)

from . import estoque, pedidos
from .auth import admin_required, escrita_required
from .cadastros import _csv_response
from .db import get_db
from .estoque import ErroValidacao
from .movimentos import _destino_seguro, consultar_movimentos
from .util import hoje, limpar, to_int

bp = Blueprint("pedidos", __name__)

ROTULOS_GRUPOS = {
    "novo": "Novos pedidos",
    "pendente": "Pendentes (deixados para mais tarde)",
    "em_separacao": "Em separação",
    "pronto": "Prontos para retirada",
}


# ------------------------------------------------------------------ API

@bp.post("/api/pedidos")
def api_criar():
    """Recebe um pedido do Power Automate. Autenticação: cabeçalho X-API-Key."""
    db = get_db()
    enviada = request.headers.get("X-API-Key", "")
    if not enviada or not hmac.compare_digest(enviada, pedidos.obter_api_key(db)):
        return jsonify(erro="Chave de API inválida."), 401
    dados = request.get_json(silent=True)
    if not isinstance(dados, dict):
        return jsonify(erro="Envie um JSON (Content-Type: application/json)."), 400
    try:
        pedido_id, criado = pedidos.criar_pedido(db, dados)
    except ErroValidacao as e:
        return jsonify(erro=str(e)), 400
    return jsonify(id=pedido_id, criado=criado), 201 if criado else 200


@bp.route("/integracao", methods=["GET", "POST"])
@admin_required
def integracao():
    db = get_db()
    if request.method == "POST":
        pedidos.obter_api_key(db, regenerar=True)
        flash("Nova chave gerada. Atualize o fluxo do Power Automate com a nova chave.", "ok")
        return redirect(url_for("pedidos.integracao"))
    return render_template("integracao.html", chave=pedidos.obter_api_key(db),
                           url_api=url_for("pedidos.api_criar", _external=True))


@bp.post("/pedidos/simular")
@escrita_required
def simular():
    """Cria um pedido manual ou de teste (mesmos campos das colunas H a P do Forms)."""
    dados = {}
    for c in ("nome", "local", "motivo", "vestimento", "epi", "sapatao", "camiseta", "calca", "observacao"):
        valores = [v.strip() for v in request.form.getlist(c) if v and v.strip()]
        dados[c] = "; ".join(valores)
    try:
        pedido_id, _ = pedidos.criar_pedido(get_db(), dados)
        flash(f"Pedido #{pedido_id} criado e enviado para a fila de novos pedidos.", "ok")
    except ErroValidacao as e:
        flash(str(e), "erro")
    return redirect(_destino_seguro(request.form.get("voltar") or url_for("pedidos.lista")))


@bp.post("/pedidos/importar-csv")
@escrita_required
def importar_csv():
    """Importa pedidos a partir de um CSV exportado da planilha do Forms (colunas H a P)."""
    arq = request.files.get("arquivo")
    if not arq or not arq.filename:
        flash("Selecione um arquivo CSV exportado da planilha.", "erro")
        return redirect(url_for("pedidos.lista"))
    bruto = arq.read()
    try:
        texto = bruto.decode("utf-8-sig")
    except UnicodeDecodeError:
        texto = bruto.decode("latin-1", errors="replace")
    try:
        criados, duplicados = pedidos.importar_csv_forms(get_db(), texto)
        msg = f"{criados} pedido(s) importado(s)."
        if duplicados:
            msg += f" ({duplicados} já existente(s) ignorado(s).)"
        flash(msg, "ok")
    except ErroValidacao as e:
        flash(str(e), "erro")
    return redirect(url_for("pedidos.lista"))


# ------------------------------------------------------------ formulário público (/solicitar)

def _separar_epis_e_vestimentas(epis):
    """Separa os itens ativos do estoque em (epis_protecao, vestimentas) como no formulário oficial."""
    termos_vest = ("vestim", "uniforme", "camis", "calca", "capa de chuva", "avental", "perneira", "jardineira", "colete")
    vestimentas = []
    epis_protecao = []
    for e in epis:
        cat_norm = pedidos.normalizar(e["categoria"] or "")
        nome_norm = pedidos.normalizar(e["nome"] or "")
        if cat_norm in ("vestimenta", "vestimento", "uniforme") or any(t in nome_norm for t in termos_vest):
            vestimentas.append(e)
        else:
            epis_protecao.append(e)
    return epis_protecao, vestimentas


@bp.route("/solicitar", methods=["GET", "POST"])
def solicitar():
    """Formulário público de solicitação de Uniforme e EPI (acessível sem login)."""
    db = get_db()
    colaboradores = db.execute(
        "SELECT id, nome, matricula, setor, funcao FROM colaboradores WHERE ativo = 1 ORDER BY nome COLLATE NOCASE"
    ).fetchall()
    epis_ativos = db.execute(
        "SELECT id, nome, categoria FROM epis WHERE ativo = 1 ORDER BY id"
    ).fetchall()
    epis_protecao, vestimentas = _separar_epis_e_vestimentas(epis_ativos)

    padrao_por_cargo = {}
    for r in db.execute("SELECT cargo, epi_id FROM cargo_epis_padrao"):
        chave_cargo = (r["cargo"] or "").strip().casefold()
        if chave_cargo:
            padrao_por_cargo.setdefault(chave_cargo, []).append(r["epi_id"])

    locais = list(pedidos.LOCAIS_FORMULARIO)
    for (loc_extra,) in db.execute("SELECT DISTINCT local_destino FROM pedidos WHERE local_destino IS NOT NULL ORDER BY 1"):
        if loc_extra and loc_extra not in locais:
            locais.append(loc_extra)

    pedido_enviado = None
    enviado_id = to_int(request.args.get("enviado"))
    if enviado_id:
        encontrados = pedidos.carregar(db, pedido_id=enviado_id)
        if encontrados:
            pedido_enviado = encontrados[0]

    form_dados = {}
    if request.method == "POST":
        # Data do pedido gerada automaticamente no momento em que o colaborador finaliza o envio
        data_pedido = hoje().isoformat()
        nome = limpar(request.form.get("nome_outro")) or limpar(request.form.get("nome"))
        local = limpar(request.form.get("local_outro")) or limpar(request.form.get("local"))
        motivo = limpar(request.form.get("motivo"))
        lista_epi = [v.strip() for v in request.form.getlist("epi") if v and v.strip()]
        lista_vest = [v.strip() for v in request.form.getlist("vestimento") if v and v.strip()]
        observacao = limpar(request.form.get("observacao"))

        todos_marcados = lista_epi + lista_vest
        calcados_sel = {pedidos.tipo_calcado(x) for x in todos_marcados if pedidos.tipo_calcado(x)}
        uniformes_sel = {pedidos.tipo_uniforme(x) for x in todos_marcados if pedidos.tipo_uniforme(x)}

        # Só considera tamanhos dos produtos efetivamente selecionados
        tam_sapatao = (limpar(request.form.get("tam_sapatao")) or limpar(request.form.get("sapatao")) or "") if "sapatao" in calcados_sel else ""
        tam_botina = (limpar(request.form.get("tam_botina")) or limpar(request.form.get("sapatao")) or "") if "botina" in calcados_sel else ""
        tam_bota = (limpar(request.form.get("tam_bota")) or limpar(request.form.get("sapatao")) or "") if "bota" in calcados_sel else ""
        camiseta = (limpar(request.form.get("camiseta")) or "") if "camiseta" in uniformes_sel else ""
        calca = (limpar(request.form.get("calca")) or "") if "calca" in uniformes_sel else ""

        form_dados = {
            "nome": nome,
            "local": local,
            "motivo": motivo,
            "epi": set(lista_epi),
            "vestimento": set(lista_vest),
            "tam_sapatao": tam_sapatao,
            "tam_botina": tam_botina,
            "tam_bota": tam_bota,
            "camiseta": camiseta,
            "calca": calca,
            "observacao": observacao or "",
        }

        erro = None
        if not nome:
            erro = "Selecione o seu nome completo."
        elif not local:
            erro = "Informe o local onde os EPIs solicitados devem ser entregues."
        elif not motivo:
            erro = "Informe o motivo da solicitação."
        elif not (lista_epi or lista_vest):
            erro = "Selecione ao menos um equipamento de proteção individual (EPI) ou vestimenta."
        elif "sapatao" in calcados_sel and (not tam_sapatao or not tam_sapatao.isdigit()):
            erro = "Informe a numeração do Sapatão (somente números)."
        elif "botina" in calcados_sel and (not tam_botina or not tam_botina.isdigit()):
            erro = "Informe a numeração da Botina (somente números)."
        elif "bota" in calcados_sel and (not tam_bota or not tam_bota.isdigit()):
            erro = "Informe a numeração da Bota (somente números)."
        elif "camiseta" in uniformes_sel and not camiseta:
            erro = "Selecione o tamanho da Camiseta."
        elif "calca" in uniformes_sel and not calca:
            erro = "Selecione o tamanho da Calça."

        if erro:
            flash(erro, "erro")
        else:
            try:
                pedido_id, _ = pedidos.criar_pedido(db, {
                    "data_pedido": data_pedido,
                    "nome": nome,
                    "local": local,
                    "motivo": motivo,
                    "epi": lista_epi,
                    "vestimento": lista_vest,
                    "tam_sapatao": tam_sapatao,
                    "tam_botina": tam_botina,
                    "tam_bota": tam_bota,
                    "camiseta": camiseta,
                    "calca": calca,
                    "observacao": observacao,
                })
                flash(f"Solicitação enviada com sucesso! Seu pedido de número #{pedido_id} já foi encaminhado ao Almoxarifado.", "ok")
                return redirect(url_for("pedidos.solicitar", enviado=pedido_id))
            except ErroValidacao as e:
                flash(str(e), "erro")

    cargo_sel = ""
    nome_sel = form_dados.get("nome", "")
    if nome_sel:
        for c in colaboradores:
            if c["nome"] == nome_sel:
                cargo_sel = (c["funcao"] or "").strip()
                break
    padrao_ids_sel = set(padrao_por_cargo.get(cargo_sel.casefold(), ())) if cargo_sel else set()

    return render_template(
        "solicitar.html",
        colaboradores=colaboradores,
        epis_protecao=epis_protecao,
        vestimentas=vestimentas,
        locais=locais,
        motivos=pedidos.MOTIVOS_FORMULARIO,
        tamanhos=pedidos.TAMANHOS_UNIFORME,
        pedido_enviado=pedido_enviado,
        form_dados=form_dados,
        padrao_por_cargo=padrao_por_cargo,
        padrao_ids_sel=padrao_ids_sel,
        tipo_calcado=pedidos.tipo_calcado,
        tipo_uniforme=pedidos.tipo_uniforme,
    )


# ------------------------------------------------------------ listas

def _opcoes_inventario(epis):
    """Separa os EPIs ativos por tipo de campo do formulário (mantendo os demais como opção)."""
    regras = {
        "vestimento": ("colete", "vestim", "avent", "jaqueta", "capa", "macac"),
        "sapatao": ("sapat", "bota", "botina", "calcad"),
        "camiseta": ("camis", "blusa", "polo"),
        "calca": ("calca",),
    }
    todas_palavras = sum(regras.values(), ())
    opcoes = {}
    for campo, termos in regras.items():
        principais = [e for e in epis if any(t in pedidos.normalizar(f"{e['nome']} {e['categoria'] or ''}") for t in termos)]
        outros = [e for e in epis if e not in principais]
        opcoes[campo] = (principais, outros)
    principais_epi = [
        e for e in epis
        if not any(t in pedidos.normalizar(f"{e['nome']} {e['categoria'] or ''}") for t in todas_palavras)
    ]
    outros_epi = [e for e in epis if e not in principais_epi]
    opcoes["epi"] = (principais_epi, outros_epi)
    return opcoes


@bp.get("/pedidos")
def lista():
    db = get_db()
    aba = request.args.get("aba", "abertos")
    if aba not in ("abertos", "retirados", "cancelados"):
        aba = "abertos"
    epis = estoque.epis_com_saldo(db, somente_ativos=True)
    colaboradores = db.execute(
        "SELECT id, nome, matricula, setor FROM colaboradores WHERE ativo = 1 ORDER BY nome COLLATE NOCASE"
    ).fetchall()
    locais = [r[0] for r in db.execute(
        "SELECT DISTINCT local_destino FROM pedidos WHERE local_destino IS NOT NULL "
        "UNION SELECT DISTINCT setor FROM colaboradores WHERE setor IS NOT NULL ORDER BY 1"
    )]
    contexto_form = {
        "opcoes_inv": _opcoes_inventario(epis),
        "colaboradores": colaboradores,
        "locais": locais,
    }
    if aba == "abertos":
        todos = pedidos.carregar(db, status=pedidos.ABERTOS)
        grupos = [(chave, rotulo, [p for p in todos if p["status"] == chave])
                  for chave, rotulo in ROTULOS_GRUPOS.items()]
        return render_template("pedidos_lista.html", aba=aba, grupos=grupos, historico=None, **contexto_form)
    status = ("retirado",) if aba == "retirados" else ("cancelado",)
    return render_template("pedidos_lista.html", aba=aba, grupos=None,
                           historico=pedidos.carregar(db, status=status, limite=200), **contexto_form)


@bp.get("/pedidos/novos")
def novos():
    """Consultado de tempos em tempos pelo navegador: cards dos pedidos recém-chegados."""
    db = get_db()
    cont = pedidos.contagens(db)
    lista_novos = pedidos.carregar(db, status=("novo",)) if g.usuario["perfil"] != "consulta" else []
    return jsonify(
        novos=len(lista_novos),
        a_atender=sum(cont.get(s, 0) for s in pedidos.A_ATENDER),
        html=render_template("_dock_cards.html", pedidos=lista_novos),
    )


@bp.get("/pedidos/exportar.csv")
def exportar_csv():
    linhas = get_db().execute(
        """SELECT p.id, p.recebido_em, p.solicitante, p.local_destino, p.motivo, p.observacao, p.status,
                  c.nome AS colaborador, p.retirado_por_nome, p.retirado_descricao, p.retirado_em,
                  uf.nome AS separado_por, ue.nome AS entregue_por,
                  (SELECT GROUP_CONCAT(i.quantidade || 'x ' || COALESCE(e.nome, i.texto_original), '; ')
                   FROM pedido_itens i LEFT JOIN epis e ON e.id = i.epi_id
                   WHERE i.pedido_id = p.id AND i.ignorado = 0) AS itens
           FROM pedidos p
           LEFT JOIN colaboradores c ON c.id = p.colaborador_id
           LEFT JOIN usuarios uf ON uf.id = p.finalizado_por
           LEFT JOIN usuarios ue ON ue.id = p.entregue_por
           ORDER BY p.id DESC""").fetchall()
    saida = io.StringIO()
    w = csv.writer(saida, delimiter=";")
    w.writerow(["Pedido", "Recebido em", "Solicitante", "Local", "Motivo", "Observação", "Situação",
                "Colaborador cadastrado", "Itens", "Separado por", "Retirado por", "Descrição de quem retirou",
                "Retirado em", "Entregue por"])
    for p in linhas:
        w.writerow([p["id"], p["recebido_em"], p["solicitante"], p["local_destino"] or "", p["motivo"] or "",
                    p["observacao"] or "", pedidos.STATUS[p["status"]], p["colaborador"] or "", p["itens"] or "",
                    p["separado_por"] or "", p["retirado_por_nome"] or "", p["retirado_descricao"] or "",
                    p["retirado_em"] or "", p["entregue_por"] or ""])
    return _csv_response(saida, "pedidos.csv")


# ------------------------------------------------------------ tela do pedido

@bp.get("/pedidos/<int:pedido_id>")
def detalhe(pedido_id):
    db = get_db()
    encontrados = pedidos.carregar(db, pedido_id=pedido_id)
    if not encontrados:
        abort(404)
    pedido = encontrados[0]
    movs = consultar_movimentos(db, {"grupo": pedido["grupo"], "incluir_canceladas": True}, limite=None) \
        if pedido["grupo"] else []
    return render_template(
        "pedido.html", p=pedido, movs=movs, STATUS=pedidos.STATUS,
        epis=estoque.epis_com_saldo(db),
        colaboradores=db.execute(
            "SELECT id, nome, matricula FROM colaboradores WHERE ativo = 1 ORDER BY nome COLLATE NOCASE").fetchall(),
    )


# ------------------------------------------------------------ ações

def _executar(acao, pedido_id, sucesso=None, destino=None):
    """Roda uma ação do serviço, trata erros de validação e volta para a tela do pedido."""
    try:
        acao()
        if sucesso:
            flash(sucesso, "ok")
    except ErroValidacao as e:
        flash(str(e), "erro")
    return redirect(destino or url_for("pedidos.detalhe", pedido_id=pedido_id))


@bp.post("/pedidos/<int:pedido_id>/iniciar")
@escrita_required
def iniciar(pedido_id):
    return _executar(lambda: pedidos.iniciar(get_db(), pedido_id, g.usuario["id"]), pedido_id)


@bp.post("/pedidos/<int:pedido_id>/mais-tarde")
@escrita_required
def mais_tarde(pedido_id):
    return _executar(lambda: pedidos.adiar(get_db(), pedido_id), pedido_id,
                     "Pedido guardado em Pendentes.",
                     destino=_destino_seguro(request.form.get("voltar") or url_for("pedidos.lista")))


@bp.post("/pedidos/<int:pedido_id>/itens/<int:item_id>/separar")
@escrita_required
def separar(pedido_id, item_id):
    """Chamado pelo checkbox (fetch); responde JSON."""
    try:
        pedidos.marcar_separado(get_db(), pedido_id, item_id, request.form.get("separado") == "1")
    except ErroValidacao as e:
        return jsonify(erro=str(e)), 400
    return jsonify(ok=True)


@bp.post("/pedidos/<int:pedido_id>/itens/<int:item_id>/editar")
@escrita_required
def editar_item(pedido_id, item_id):
    return _executar(lambda: pedidos.editar_item(
        get_db(), pedido_id, item_id, to_int(request.form.get("epi_id")),
        to_int(request.form.get("quantidade")), request.form.get("lembrar") == "1"), pedido_id)


@bp.post("/pedidos/<int:pedido_id>/itens/<int:item_id>/ignorar")
@escrita_required
def ignorar_item(pedido_id, item_id):
    return _executar(lambda: pedidos.ignorar_item(
        get_db(), pedido_id, item_id, True, request.form.get("sempre") == "1"), pedido_id)


@bp.post("/pedidos/<int:pedido_id>/itens/novo")
@escrita_required
def adicionar_item(pedido_id):
    return _executar(lambda: pedidos.adicionar_item(
        get_db(), pedido_id, to_int(request.form.get("epi_id")), to_int(request.form.get("quantidade"))),
        pedido_id)


@bp.post("/pedidos/<int:pedido_id>/colaborador")
@escrita_required
def colaborador(pedido_id):
    return _executar(lambda: pedidos.vincular_colaborador(
        get_db(), pedido_id, to_int(request.form.get("colaborador_id")),
        criar_novo=request.form.get("criar") == "1"), pedido_id, "Colaborador vinculado.")


@bp.post("/pedidos/<int:pedido_id>/finalizar")
@escrita_required
def finalizar(pedido_id):
    return _executar(lambda: pedidos.finalizar(get_db(), pedido_id, g.usuario["id"]), pedido_id,
                     "Pedido finalizado: baixa feita no estoque e pronto para retirada.")


@bp.post("/pedidos/<int:pedido_id>/retirar")
@escrita_required
def retirar(pedido_id):
    return _executar(lambda: pedidos.retirar(
        get_db(), pedido_id, limpar(request.form.get("retirado_por_nome")),
        limpar(request.form.get("retirado_descricao")), g.usuario["id"]), pedido_id,
        "Retirada registrada.")


@bp.post("/pedidos/<int:pedido_id>/cancelar")
@escrita_required
def cancelar(pedido_id):
    return _executar(lambda: pedidos.cancelar(
        get_db(), pedido_id, limpar(request.form.get("motivo")), g.usuario["id"],
        pode_estornar=g.usuario["perfil"] == "admin"), pedido_id, "Pedido cancelado.")
