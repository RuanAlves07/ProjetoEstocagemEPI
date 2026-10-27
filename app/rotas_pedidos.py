"""Pedidos de EPI: API para o Power Automate, cards de novos pedidos, separação e retirada."""
import csv
import hmac
import io

from flask import (Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template,
                   request, url_for)

from . import caepi, estoque, pedidos
from .auth import admin_required, escrita_required, usuario_tem_permissao
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


def _padrao_por_colaborador(db, colaboradores):
    """Mapa {id do colaborador: ids dos EPIs padrão}, pelo cargo (ou pelo padrão individual se não tiver cargo)."""
    por_cargo = {}
    for r in db.execute("SELECT cargo, epi_id FROM cargo_epis_padrao"):
        chave_cargo = (r["cargo"] or "").strip().casefold()
        if chave_cargo:
            por_cargo.setdefault(chave_cargo, set()).add(r["epi_id"])
    individual = {}
    for r in db.execute("SELECT colaborador_id, epi_id FROM colaborador_epis_padrao"):
        individual.setdefault(r["colaborador_id"], set()).add(r["epi_id"])
    resultado = {}
    for c in colaboradores:
        cargo = (c["funcao"] or "").strip().casefold()
        resultado[c["id"]] = set(por_cargo.get(cargo, ())) if cargo else set(individual.get(c["id"], ()))
    return resultado


@bp.route("/solicitar", methods=["GET", "POST"])
def solicitar():
    """Formulário público (sem login): o colaborador só pede os EPIs do padrão do cargo dele."""
    return _formulario_solicitacao(modo_gestor=False)


@bp.route("/solicitar/gestor", methods=["GET", "POST"])
def solicitar_gestor():
    """Formulário do gestor (exige login): permite pedir EPIs fora do padrão do cargo, com justificativa."""
    if not usuario_tem_permissao("solicitar_fora_funcao"):
        abort(403)
    return _formulario_solicitacao(modo_gestor=True)


def _formulario_solicitacao(modo_gestor):
    db = get_db()
    endpoint_form = "pedidos.solicitar_gestor" if modo_gestor else "pedidos.solicitar"
    cfg_form = pedidos.obter_config_formulario(db)
    colaboradores = db.execute(
        "SELECT id, nome, matricula, setor, funcao FROM colaboradores WHERE ativo = 1 ORDER BY nome COLLATE NOCASE"
    ).fetchall()
    epis_ativos = db.execute(
        "SELECT id, nome, categoria, imagem_url, COALESCE(exibir_no_forms, 1) AS exibir_no_forms "
        "FROM epis WHERE ativo = 1 AND COALESCE(exibir_no_forms, 1) = 1 ORDER BY id"
    ).fetchall()
    epis_protecao, vestimentas = _separar_epis_e_vestimentas(epis_ativos)

    # O formulário envia o nome do item; o mesmo nome pode existir em mais de um tamanho
    ids_por_nome = {}
    for e in epis_ativos:
        ids_por_nome.setdefault(e["nome"], set()).add(e["id"])

    padrao_colab = _padrao_por_colaborador(db, colaboradores)

    obras = pedidos.obras_ativas(db)   # só as obras ativas na aba Obras
    motivos = list(cfg_form.get("motivos") or pedidos.MOTIVOS_FORMULARIO)
    tamanhos = list(cfg_form.get("tamanhos_uniforme") or pedidos.TAMANHOS_UNIFORME)
    perguntas_extras = list(cfg_form.get("perguntas_extras") or [])

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
        nome = limpar(request.form.get("nome"))
        colab = next((c for c in colaboradores if c["nome"] == nome), None)
        local = limpar(request.form.get("local_outro")) or limpar(request.form.get("local"))
        motivo = limpar(request.form.get("motivo"))
        lista_epi = [v.strip() for v in request.form.getlist("epi") if v and v.strip()]
        lista_vest = [v.strip() for v in request.form.getlist("vestimento") if v and v.strip()]
        observacao = limpar(request.form.get("observacao"))
        justificativa = limpar(request.form.get("justificativa")) if modo_gestor else ""

        todos_marcados = lista_epi + lista_vest
        calcados_sel = {pedidos.tipo_calcado(x) for x in todos_marcados if pedidos.tipo_calcado(x)}
        uniformes_sel = {pedidos.tipo_uniforme(x) for x in todos_marcados if pedidos.tipo_uniforme(x)}

        permitidos = padrao_colab.get(colab["id"], set()) if colab else set()
        nao_disponiveis = [x for x in todos_marcados if x not in ids_por_nome]
        fora_funcao = [x for x in todos_marcados if x in ids_por_nome and not (ids_por_nome[x] & permitidos)]

        # Só considera tamanhos dos produtos efetivamente selecionados
        tam_sapatao = (limpar(request.form.get("tam_sapatao")) or limpar(request.form.get("sapatao")) or "") if "sapatao" in calcados_sel else ""
        tam_botina = (limpar(request.form.get("tam_botina")) or limpar(request.form.get("sapatao")) or "") if "botina" in calcados_sel else ""
        tam_bota = (limpar(request.form.get("tam_bota")) or limpar(request.form.get("sapatao")) or "") if "bota" in calcados_sel else ""
        camiseta = (limpar(request.form.get("camiseta")) or "") if "camiseta" in uniformes_sel else ""
        calca = (limpar(request.form.get("calca")) or "") if "calca" in uniformes_sel else ""

        respostas_extras = {}
        erro_extra = None
        for p_ex in perguntas_extras:
            ch_campo = f"extra_{p_ex['id']}"
            if p_ex.get("tipo") == "multipla":
                vals_ex = [v.strip() for v in request.form.getlist(ch_campo) if v and v.strip()]
                resp_str = ", ".join(vals_ex)
                respostas_extras[ch_campo] = set(vals_ex)
            else:
                resp_str = limpar(request.form.get(ch_campo)) or ""
                respostas_extras[ch_campo] = resp_str
            if p_ex.get("obrigatoria") and not resp_str and not erro_extra:
                erro_extra = f"Responda a pergunta obrigatória: {p_ex['titulo']}"
            if resp_str:
                complemento = f"[{p_ex['titulo']}: {resp_str}]"
                observacao = f"{observacao} | {complemento}" if observacao else complemento

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
            "observacao": limpar(request.form.get("observacao")) or "",
            "justificativa": justificativa or "",
            "extras": respostas_extras,
        }

        erro = None
        if not nome:
            erro = "Selecione o seu nome completo." if not modo_gestor else "Selecione o colaborador."
        elif not colab:
            erro = "Selecione um nome da lista de colaboradores cadastrados."
        elif not local:
            erro = "Informe a obra onde os EPIs solicitados devem ser entregues."
        elif local not in obras and not (cfg_form.get("permitir_outro_local") and limpar(request.form.get("local_outro"))):
            erro = "Selecione uma das obras da lista."
        elif not motivo:
            erro = "Informe o motivo da solicitação."
        elif not (lista_epi or lista_vest):
            erro = "Selecione ao menos um equipamento de proteção individual (EPI) ou vestimenta."
        elif nao_disponiveis:
            erro = f"Item não disponível no formulário: {', '.join(nao_disponiveis)}."
        elif fora_funcao and not modo_gestor:
            erro = (f"Estes itens não fazem parte do padrão do seu cargo: {', '.join(fora_funcao)}. "
                    "Para pedidos fora da sua função, procure o seu gestor.")
        elif fora_funcao and not justificativa:
            erro = f"Informe a justificativa para os itens fora da função do colaborador: {', '.join(fora_funcao)}."
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
        elif erro_extra:
            erro = erro_extra

        if modo_gestor:
            # Registra no pedido quem pediu e por quê, para o almoxarifado e para a ficha do colaborador
            marcas = [f"[Solicitado pelo gestor: {g.usuario['nome']}]"]
            if fora_funcao:
                marcas.append(f"[Fora da função: {', '.join(fora_funcao)} — Justificativa: {justificativa}]")
            observacao = " | ".join(x for x in (" ".join(marcas), observacao) if x)

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
                flash(f"Solicitação enviada com sucesso! O pedido de número #{pedido_id} já foi encaminhado ao Almoxarifado.", "ok")
                return redirect(url_for(endpoint_form, enviado=pedido_id))
            except ErroValidacao as e:
                flash(str(e), "erro")

    colab_sel = next((c for c in colaboradores if c["nome"] == form_dados.get("nome", "")), None)
    padrao_ids_sel = padrao_colab.get(colab_sel["id"], set()) if colab_sel else set()

    return render_template(
        "solicitar.html",
        modo_gestor=modo_gestor,
        endpoint_form=endpoint_form,
        cfg_form=cfg_form,
        colaboradores=colaboradores,
        epis_protecao=epis_protecao,
        vestimentas=vestimentas,
        obras=obras,
        motivos=motivos,
        tamanhos=tamanhos,
        perguntas_extras=perguntas_extras,
        pedido_enviado=pedido_enviado,
        form_dados=form_dados,
        colab_sel_id=colab_sel["id"] if colab_sel else None,
        padrao_por_colab={str(cid): sorted(ids) for cid, ids in padrao_colab.items()},
        padrao_ids_sel=padrao_ids_sel,
        tipo_calcado=pedidos.tipo_calcado,
        tipo_uniforme=pedidos.tipo_uniforme,
    )


# ------------------------------------------------------------ personalização do formulário (estilo Microsoft Forms)

def _ler_lista_opcoes_form(chave_lista, chave_novo=None):
    """Lê uma lista de opções vinda de múltiplos inputs ou de um campo adicional."""
    itens = []
    for valor in request.form.getlist(chave_lista):
        for parte in str(valor or "").splitlines():
            txt = limpar(parte)
            if txt:
                itens.append(txt)
    if chave_novo:
        extra = limpar(request.form.get(chave_novo))
        if extra:
            itens.append(extra)
    return itens


@bp.route("/solicitar/personalizar", methods=["GET", "POST"])
def personalizar_formulario():
    """Editor interno estilo Microsoft Forms para personalizar perguntas, EPIs e imagens (obras ficam na aba Obras)."""
    db = get_db()
    if request.method == "POST":
        motivos = _ler_lista_opcoes_form("motivos", "novo_motivo")
        tamanhos = _ler_lista_opcoes_form("tamanhos_uniforme", "novo_tamanho")

        # Perguntas extras personalizadas
        extras = []
        titulos_ex = request.form.getlist("extra_titulo")
        tipos_ex = request.form.getlist("extra_tipo")
        obrig_ex = set(request.form.getlist("extra_obrigatoria_idx"))
        opcoes_ex = request.form.getlist("extra_opcoes")
        ids_ex = request.form.getlist("extra_id")
        for idx, tit in enumerate(titulos_ex):
            tit_limpo = limpar(tit)
            if not tit_limpo:
                continue
            tipo_p = tipos_ex[idx] if idx < len(tipos_ex) else "texto"
            bruto_opcoes = opcoes_ex[idx] if idx < len(opcoes_ex) else ""
            lista_op = [limpar(x) for x in bruto_opcoes.replace(";", "\n").splitlines() if limpar(x)]
            id_p = (limpar(ids_ex[idx]) if idx < len(ids_ex) else None) or f"qextra_{idx + 1}"
            extras.append({
                "id": id_p,
                "titulo": tit_limpo,
                "tipo": tipo_p,
                "obrigatoria": str(idx) in obrig_ex,
                "opcoes": lista_op,
            })

        # Também permite adicionar uma nova pergunta extra rápida se preenchida no bloco de nova pergunta
        novo_extra_tit = limpar(request.form.get("novo_extra_titulo"))
        if novo_extra_tit:
            novo_extra_tipo = request.form.get("novo_extra_tipo") or "texto"
            novo_extra_opcoes = [
                limpar(x)
                for x in (request.form.get("novo_extra_opcoes") or "").replace(";", "\n").splitlines()
                if limpar(x)
            ]
            extras.append({
                "id": f"qextra_{len(extras) + 1}",
                "titulo": novo_extra_tit,
                "tipo": novo_extra_tipo,
                "obrigatoria": request.form.get("novo_extra_obrigatoria") == "1",
                "opcoes": novo_extra_opcoes,
            })

        nova_cfg = {
            "titulo": request.form.get("titulo"),
            "descricao": request.form.get("descricao"),
            "pergunta_1_titulo": request.form.get("pergunta_1_titulo"),
            "pergunta_2_titulo": request.form.get("pergunta_2_titulo"),
            "permitir_outro_local": request.form.get("permitir_outro_local") == "1",
            "pergunta_3_titulo": request.form.get("pergunta_3_titulo"),
            "pergunta_4_titulo": request.form.get("pergunta_4_titulo"),
            "pergunta_4_subtitulo": request.form.get("pergunta_4_subtitulo"),
            "pergunta_calcado_titulo": request.form.get("pergunta_calcado_titulo"),
            "pergunta_calcado_subtitulo": request.form.get("pergunta_calcado_subtitulo"),
            "pergunta_5_titulo": request.form.get("pergunta_5_titulo"),
            "pergunta_5_subtitulo": request.form.get("pergunta_5_subtitulo"),
            "pergunta_uniforme_titulo": request.form.get("pergunta_uniforme_titulo"),
            "pergunta_uniforme_subtitulo": request.form.get("pergunta_uniforme_subtitulo"),
            "pergunta_6_titulo": request.form.get("pergunta_6_titulo"),
            "motivos": motivos,
            "tamanhos_uniforme": tamanhos,
            "perguntas_extras": extras,
        }

        try:
            pedidos.salvar_config_formulario(db, nova_cfg)

            # Atualiza visibilidade no formulário, nome e imagens das opções de EPIs e Vestimentas
            if request.form.get("atualizar_opcoes_epi") == "1":
                visiveis_ids = {to_int(x) for x in request.form.getlist("epi_visivel") if to_int(x)}
                todos_ativos = db.execute("SELECT id, nome, imagem_url FROM epis WHERE ativo = 1").fetchall()
                for item in todos_ativos:
                    eid = item["id"]
                    visivel = 1 if eid in visiveis_ids else 0
                    novo_nome = limpar(request.form.get(f"nome_epi_{eid}")) or item["nome"]
                    nova_img = item["imagem_url"]
                    if request.form.get(f"remover_img_epi_{eid}") == "1":
                        nova_img = None
                    else:
                        arq = request.files.get(f"imagem_epi_{eid}")
                        b64_ou_url = request.form.get(f"imagem_url_epi_{eid}")
                        if (arq and getattr(arq, "filename", "")) or (b64_ou_url and b64_ou_url.strip()):
                            nova_img = pedidos.processar_imagem_epi(arq, b64_ou_url)
                    db.execute(
                        "UPDATE epis SET nome = ?, exibir_no_forms = ?, imagem_url = ? WHERE id = ?",
                        (novo_nome, visivel, nova_img, eid),
                    )
            flash("Personalizações do formulário salvas com sucesso!", "ok")
            return redirect(url_for("pedidos.personalizar_formulario"))
        except (ErroValidacao, sqlite3.IntegrityError) as e:
            flash(str(e), "erro")

    cfg_form = pedidos.obter_config_formulario(db)
    todos_epis = db.execute(
        "SELECT id, nome, categoria, ca, unidade, imagem_url, COALESCE(exibir_no_forms, 1) AS exibir_no_forms "
        "FROM epis WHERE ativo = 1 ORDER BY id"
    ).fetchall()
    epis_protecao, vestimentas = _separar_epis_e_vestimentas(todos_epis)
    return render_template(
        "solicitar_personalizar.html",
        cfg_form=cfg_form,
        obras_ativas=pedidos.obras_ativas(db),
        epis_protecao=epis_protecao,
        vestimentas=vestimentas,
    )


@bp.post("/solicitar/personalizar/epi/novo")
def form_adicionar_epi():
    """Adiciona rapidamente um novo EPI ou Vestimenta ao formulário (e ao catálogo de estoque)."""
    db = get_db()
    nome = limpar(request.form.get("nome"))
    tipo_secao = request.form.get("tipo_secao") or "epi"
    ca = caepi.normalizar_ca(request.form.get("ca")) or limpar(request.form.get("ca"))
    info_ca = caepi.consultar_ca(db, ca) if ca else None
    validade_ca = info_ca.get("validade_ca") if info_ca else None
    if not nome and info_ca:
        nome = limpar(info_ca.get("nome_sugerido")) or limpar(info_ca.get("nome_equipamento"))
    categoria = (
        "Vestimenta"
        if tipo_secao == "vestimenta"
        else (limpar(request.form.get("categoria")) or (info_ca.get("categoria") if info_ca else None) or "EPI")
    )
    unidade = limpar(request.form.get("unidade")) or (info_ca.get("unidade") if info_ca else None) or "un"
    ancora = "#secao-q5" if tipo_secao == "vestimenta" else "#secao-q4"

    if not nome:
        flash("Informe o nome do equipamento/vestimenta ou um Nº de CA válido para adicionar ao formulário.", "erro")
        return redirect(url_for("pedidos.personalizar_formulario") + ancora)

    try:
        arq = request.files.get("imagem")
        url_img = request.form.get("imagem_url")
        img_final = pedidos.processar_imagem_epi(arq, url_img)
        if not img_final and request.form.get("gerar_ilustracao") == "1":
            img_final = pedidos.gerar_svg_ilustrativo_epi(nome)

        existente = db.execute(
            "SELECT id, imagem_url FROM epis WHERE nome = ? COLLATE NOCASE", (nome,)
        ).fetchone()
        if existente:
            db.execute(
                "UPDATE epis SET ativo = 1, exibir_no_forms = 1, categoria = COALESCE(?, categoria), "
                "ca = COALESCE(?, ca), validade_ca = COALESCE(?, validade_ca), "
                "imagem_url = COALESCE(?, imagem_url) WHERE id = ?",
                (categoria, ca, validade_ca, img_final, existente["id"]),
            )
            flash(f"A opção “{nome}” já constava no catálogo e foi ativada no formulário.", "ok")
        else:
            db.execute(
                """INSERT INTO epis (nome, categoria, ca, validade_ca, unidade, estoque_minimo, prazo_reposicao_dias,
                                     imagem_url, exibir_no_forms, ativo)
                   VALUES (?, ?, ?, ?, ?, 0, 15, ?, 1, 1)""",
                (nome, categoria, ca, validade_ca, unidade, img_final),
            )
            flash(f"Opção “{nome}” adicionada ao formulário e cadastrada no catálogo de EPIs!", "ok")
    except (ErroValidacao, sqlite3.IntegrityError) as e:
        flash(str(e), "erro")

    return redirect(url_for("pedidos.personalizar_formulario") + ancora)


@bp.post("/solicitar/personalizar/epi/<int:epi_id>/imagem")
def form_imagem_epi(epi_id):
    """Atualiza, remove ou gera ilustração da imagem de uma opção específica do formulário."""
    db = get_db()
    epi = db.execute("SELECT id, nome, categoria, imagem_url FROM epis WHERE id = ?", (epi_id,)).fetchone()
    if not epi:
        abort(404)
    acao = request.form.get("acao") or "salvar"
    ancora = f"#opcao-epi-{epi_id}"
    try:
        if acao == "remover":
            db.execute("UPDATE epis SET imagem_url = NULL WHERE id = ?", (epi_id,))
            flash(f"Imagem de “{epi['nome']}” removida.", "ok")
        elif acao == "ilustrar":
            svg_uri = pedidos.gerar_svg_ilustrativo_epi(epi["nome"])
            db.execute("UPDATE epis SET imagem_url = ? WHERE id = ?", (svg_uri, epi_id))
            flash(f"Ilustração visual aplicada para “{epi['nome']}”!", "ok")
        else:
            arq = request.files.get("imagem")
            url_img = request.form.get("imagem_url")
            nova_img = pedidos.processar_imagem_epi(arq, url_img)
            if not nova_img:
                raise ErroValidacao("Selecione um arquivo de imagem ou informe uma URL válida.")
            db.execute("UPDATE epis SET imagem_url = ? WHERE id = ?", (nova_img, epi_id))
            flash(f"Imagem de “{epi['nome']}” atualizada no formulário!", "ok")
    except ErroValidacao as e:
        flash(str(e), "erro")
    return redirect(url_for("pedidos.personalizar_formulario") + ancora)


@bp.post("/solicitar/personalizar/ilustrar-todos")
def form_ilustrar_todos():
    """Gera ilustrações visuais automáticas para todas as opções do formulário que ainda não têm foto."""
    db = get_db()
    sobrescrever = request.form.get("sobrescrever") == "1"
    ativos = db.execute("SELECT id, nome, imagem_url FROM epis WHERE ativo = 1").fetchall()
    atualizados = 0
    for item in ativos:
        if sobrescrever or not item["imagem_url"]:
            svg_uri = pedidos.gerar_svg_ilustrativo_epi(item["nome"])
            db.execute("UPDATE epis SET imagem_url = ? WHERE id = ?", (svg_uri, item["id"]))
            atualizados += 1
    flash(f"{atualizados} opção(ões) receberam ilustrações visuais de fácil identificação!", "ok")
    return redirect(url_for("pedidos.personalizar_formulario") + "#secao-q4")


@bp.post("/solicitar/personalizar/restaurar")
def form_restaurar():
    """Restaura os textos, cidades, motivos e tamanhos padrão do formulário."""
    pedidos.restaurar_config_formulario(get_db())
    flash("Textos, cidades, motivos e tamanhos restaurados para o padrão original.", "ok")
    return redirect(url_for("pedidos.personalizar_formulario"))


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
    lista_novos = pedidos.carregar(db, status=("novo",)) if usuario_tem_permissao("gerenciar_pedidos") else []
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
                  (SELECT GROUP_CONCAT(CAST(i.quantidade AS TEXT) || 'x ' || COALESCE(e.nome, i.texto_original), '; ')
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
@bp.post("/pedidos/<int:pedido_id>/finalizar-ressalva")
@escrita_required
def finalizar(pedido_id):
    com_ressalva = (
        request.form.get("com_ressalva") == "1"
        or request.path.endswith("/finalizar-ressalva")
    )
    obs_ressalva = request.form.get("ressalva_observacao")
    msg = (
        "Pedido finalizado com ressalva: baixa registrada para os itens separados e justificativa gravada."
        if com_ressalva
        else "Pedido finalizado: baixa feita no estoque e pronto para retirada."
    )
    return _executar(
        lambda: pedidos.finalizar(
            get_db(), pedido_id, g.usuario["id"],
            com_ressalva=com_ressalva, ressalva_obs=obs_ressalva,
        ),
        pedido_id,
        msg,
    )


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
        pode_estornar=usuario_tem_permissao("cancelar_estornar")), pedido_id, "Pedido cancelado.")
