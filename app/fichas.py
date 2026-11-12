"""Ficha de EPI da retirada: prévia, envio pelo WhatsApp, código de validação, assinatura e impressão."""
import base64
import hashlib
import json
import os
import secrets
from datetime import date, datetime, timedelta

from flask import (Blueprint, abort, current_app, flash, g, redirect, render_template, request,
                   send_file, session, url_for)

from . import email_service, pedidos, whatsapp
from .auth import escrita_required
from .db import get_db
from .estoque import ErroValidacao
from .movimentos import consultar_movimentos
from .util import limpar, to_int

bp = Blueprint("fichas", __name__)

EMPRESA_PADRAO = "ABECKER EMPREENDIMENTOS IMOBILIARIOS LTDA"
LINK_VALIDADE_DIAS = 7
CODIGO_VALIDADE_MIN = 15
CODIGO_MAX_TENTATIVAS = 5
CODIGO_INTERVALO_S = 60          # espera mínima entre um envio de código e outro
VALIDACAO_SESSAO_MIN = 30        # depois de validar o código, quanto tempo a ficha fica liberada
ASSINATURA_MAX_BYTES = 400 * 1024
ANEXO_MAX_BYTES = 10 * 1024 * 1024
ANEXO_TIPOS = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
               ".pdf": "application/pdf"}

STATUS_FICHA = {
    "aguardando": "Aguardando assinatura",
    "assinada": "Assinada pelo WhatsApp",
    "assinada_papel": "Assinada em papel",
}

DIAS_SEMANA = ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo")
MESES = ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro",
         "outubro", "novembro", "dezembro")


def _agora():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _hash(texto):
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def data_por_extenso(iso):
    """2026-11-12 -> quinta-feira, 12 de novembro de 2026"""
    try:
        d = date.fromisoformat((iso or "")[:10])
    except ValueError:
        return iso or ""
    return f"{DIAS_SEMANA[d.weekday()]}, {d.day} de {MESES[d.month - 1]} de {d.year}"


# ------------------------------------------------------------ montagem do documento

def _itens_do_pedido(db, pedido):
    """Saídas de estoque do pedido (o que de fato foi entregue), com CA e validade do CA."""
    if not pedido["grupo"]:
        return []
    movs = consultar_movimentos(db, {"grupo": pedido["grupo"], "tipo": "saida"}, limite=None)
    validades = {r["id"]: r["validade_ca"] for r in db.execute(
        "SELECT id, validade_ca FROM epis WHERE id IN (SELECT epi_id FROM movimentacoes WHERE grupo = ?)",
        (pedido["grupo"],))}
    itens = []
    for m in sorted(movs, key=lambda x: x["id"]):
        descricao = m["epi_nome"]
        if m["epi_tamanho"] and m["epi_tamanho"] not in descricao:
            descricao += f" ({m['epi_tamanho']})"
        itens.append({
            "mov_id": m["id"],
            "ca": m["ca"] or "",
            "validade_ca": validades.get(m["epi_id"]) or "",
            "descricao": descricao,
            "quantidade": m["quantidade"],
            "unidade": m["unidade"],
            "higienizado": "SIM",
        })
    return itens


def montar_documento(db, pedido, colab, data_entrega=None):
    """Dados e itens da ficha, congelados no momento da retirada."""
    data_entrega = data_entrega or date.today().isoformat()
    itens = _itens_do_pedido(db, pedido)
    for it in itens:
        it["data_entrega"] = data_entrega
    beneficiario = None
    if pedido["colaborador_id"] and pedido["colaborador_id"] != colab["id"]:
        b = db.execute("SELECT nome, matricula FROM colaboradores WHERE id = ?", (pedido["colaborador_id"],)).fetchone()
        if b:
            beneficiario = {"nome": b["nome"], "matricula": b["matricula"]}
    return {
        "numero": pedidos.numero_do_pedido(db, pedido["id"]),
        "pedido_id": pedido["id"],
        "empresa": (os.environ.get("EPI_FICHA_EMPRESA") or EMPRESA_PADRAO).strip(),
        "colaborador": {
            "nome": colab["nome"],
            "matricula": colab["matricula"] or str(colab["id"]),
            "setor": colab["setor"] or "",
            "funcao": colab["funcao"] or "",
            "cpf": whatsapp.formatar_cpf(colab["cpf"]) if colab["cpf"] else "",
            "data_nascimento": colab["data_nascimento"] or "",
        },
        "solicitante": pedido["solicitante"],
        "beneficiario": beneficiario,
        "local_destino": pedido["local_destino"] or "",
        "data_entrega": data_entrega,
        "data_extenso": data_por_extenso(data_entrega),
        "responsavel": g.usuario["nome"] if g.get("usuario") else "",
        "itens": itens,
    }


def _documento_json(doc):
    return json.dumps(doc, ensure_ascii=False, sort_keys=True)


def carregar_ficha(db, ficha_id):
    f = db.execute(
        """SELECT f.*, c.nome AS colab_nome, c.whatsapp AS colab_whatsapp, b.nome AS benef_nome,
                  u.nome AS criado_por_nome, up.nome AS papel_por_nome
           FROM fichas_epi f
           JOIN colaboradores c ON c.id = f.colaborador_id
           LEFT JOIN colaboradores b ON b.id = f.beneficiario_id
           JOIN usuarios u ON u.id = f.criado_por
           LEFT JOIN usuarios up ON up.id = f.papel_por
           WHERE f.id = ?""", (ficha_id,)).fetchone()
    if not f:
        return None
    f = dict(f)
    f["doc"] = json.loads(f["documento"])
    return f


def fichas_do_colaborador(db, colab_id):
    """Fichas em que o colaborador retirou ou em que o pedido era para ele (retirado por outra pessoa)."""
    linhas = db.execute(
        """SELECT f.id, f.pedido_id, f.status, f.criado_em, f.assinado_em, f.documento,
                  f.colaborador_id, f.beneficiario_id, c.nome AS colab_nome, b.nome AS benef_nome
           FROM fichas_epi f
           JOIN colaboradores c ON c.id = f.colaborador_id
           LEFT JOIN colaboradores b ON b.id = f.beneficiario_id
           WHERE f.colaborador_id = ? OR f.beneficiario_id = ?
           ORDER BY f.id DESC""", (colab_id, colab_id)).fetchall()
    resultado = []
    for r in linhas:
        r = dict(r)
        doc = json.loads(r.pop("documento"))
        r["numero"] = doc.get("numero")
        r["qtd_itens"] = sum(int(i.get("quantidade") or 0) for i in doc.get("itens", []))
        r["retirou"] = r["colaborador_id"] == colab_id
        resultado.append(r)
    return resultado


# ------------------------------------------------------------ revisão e emissão (equipe)

def _pedido_pronto(db, pedido_id):
    encontrados = pedidos.carregar(db, pedido_id=pedido_id)
    if not encontrados:
        abort(404)
    return encontrados[0]


def _aplicar_higienizado(doc):
    """Coluna "Higienizado?" escolhida item a item na tela de revisão (padrão SIM)."""
    for it in doc["itens"]:
        it["higienizado"] = "NÃO" if request.values.get(f"hig_{it['mov_id']}") == "NAO" else "SIM"


@bp.get("/pedidos/<int:pedido_id>/ficha")
@escrita_required
def preparar(pedido_id):
    db = get_db()
    pedido = _pedido_pronto(db, pedido_id)
    if pedido["status"] != "pronto":
        flash("A ficha de EPI é montada na retirada de um pedido pronto.", "erro")
        return redirect(url_for("pedidos.detalhe", pedido_id=pedido_id))
    colaboradores = db.execute(
        "SELECT * FROM colaboradores WHERE ativo = 1 ORDER BY nome COLLATE NOCASE").fetchall()
    colab_id = to_int(request.args.get("colaborador_id")) or pedido["colaborador_id"]
    colab = next((c for c in colaboradores if c["id"] == colab_id), None)
    doc = None
    if colab:
        doc = montar_documento(db, pedido, colab)
        _aplicar_higienizado(doc)
    return render_template("ficha_preparar.html", p=pedido, colaboradores=colaboradores, colab=colab, doc=doc,
                           whatsapp_ativo=whatsapp.configurado() or current_app.config.get("TESTING"))


@bp.post("/pedidos/<int:pedido_id>/ficha")
@escrita_required
def emitir(pedido_id):
    db = get_db()
    pedido = _pedido_pronto(db, pedido_id)
    acao = request.form.get("acao")
    colab = db.execute("SELECT * FROM colaboradores WHERE id = ? AND ativo = 1",
                       (to_int(request.form.get("colaborador_id")),)).fetchone()
    voltar = redirect(url_for("fichas.preparar", pedido_id=pedido_id,
                              colaborador_id=request.form.get("colaborador_id") or None))
    if not colab:
        flash("Selecione o colaborador que retirou o pedido.", "erro")
        return voltar
    if acao == "enviar" and not colab["whatsapp"]:
        flash(f"{colab['nome']} não tem WhatsApp no cadastro. Cadastre o número ou use Imprimir ficha.", "erro")
        return voltar
    doc = montar_documento(db, pedido, colab)
    if not doc["itens"]:
        flash("Este pedido não tem itens baixados no estoque para montar a ficha.", "erro")
        return redirect(url_for("pedidos.detalhe", pedido_id=pedido_id))
    _aplicar_higienizado(doc)
    try:
        pedidos.retirar(db, pedido_id, colab["nome"], limpar(request.form.get("retirado_descricao")),
                        g.usuario["id"])
    except ErroValidacao as e:
        flash(str(e), "erro")
        return redirect(url_for("pedidos.detalhe", pedido_id=pedido_id))
    texto = _documento_json(doc)
    cur = db.execute(
        """INSERT INTO fichas_epi (pedido_id, colaborador_id, beneficiario_id, documento, documento_hash, criado_por)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (pedido_id, colab["id"], pedido["colaborador_id"], texto, _hash(texto), g.usuario["id"]))
    ficha_id = cur.lastrowid
    try:
        email_service.notificar_evento_pedido(db, pedido_id, "retirado", ator_nome=g.usuario["nome"])
    except Exception:
        current_app.logger.exception("Falha ao notificar a retirada do pedido %s", pedido_id)
    if acao == "enviar":
        ok, erro = _enviar_link(db, ficha_id)
        if ok:
            flash(f"Retirada registrada e ficha de EPI enviada para o WhatsApp de {colab['nome']}.", "ok")
        else:
            flash(f"Retirada registrada, mas a ficha não foi enviada: {erro} Tente reenviar ou imprima a ficha.", "erro")
        return redirect(url_for("fichas.detalhe", ficha_id=ficha_id))
    flash("Retirada registrada. Imprima a ficha para colher a assinatura.", "ok")
    return redirect(url_for("fichas.imprimir", ficha_id=ficha_id, auto=1))


def _enviar_link(db, ficha_id):
    """Gera um link novo (o anterior deixa de valer) e manda pelo WhatsApp do colaborador."""
    f = carregar_ficha(db, ficha_id)
    numero = f["colab_whatsapp"]
    if not numero:
        return False, "o colaborador não tem WhatsApp no cadastro."
    token = secrets.token_urlsafe(24)
    expira = (datetime.now() + timedelta(days=LINK_VALIDADE_DIAS)).strftime("%Y-%m-%d %H:%M:%S")
    link = email_service.url_externa("fichas.publica", token=token)
    texto = (f"Olá {f['colab_nome']},\n\n"
             f"Você está recebendo esta mensagem para assinar eletronicamente a ficha de entrega de EPI "
             f"do pedido #{f['doc']['numero']}.\n\n"
             f"Para acessar o documento, clique no link abaixo:\n{link}\n\n"
             f"O link vale por {LINK_VALIDADE_DIAS} dias. Caso tenha alguma dúvida, estamos à disposição.\n\n"
             f"Atenciosamente,\nA. Becker")
    ok, erro = whatsapp.enviar_texto(numero, texto)
    if ok:
        db.execute("""UPDATE fichas_epi SET token_hash = ?, link_expira_em = ?, codigo_hash = NULL,
                      codigo_expira_em = NULL, codigo_tentativas = 0, whatsapp_numero = ?, whatsapp_enviado_em = ?
                      WHERE id = ?""", (_hash(token), expira, numero, _agora(), ficha_id))
    return ok, erro


@bp.get("/fichas/<int:ficha_id>")
def detalhe(ficha_id):
    f = carregar_ficha(get_db(), ficha_id) or abort(404)
    return render_template("ficha_detalhe.html", f=f, STATUS_FICHA=STATUS_FICHA,
                           numero_mascarado=whatsapp.formatar_numero(f["colab_whatsapp"]))


@bp.get("/fichas/<int:ficha_id>/imprimir")
def imprimir(ficha_id):
    f = carregar_ficha(get_db(), ficha_id) or abort(404)
    return render_template("ficha_imprimir.html", fichas=[f], rubrica=f["assinatura"],
                           titulo=f"Ficha de EPI #{f['doc']['numero']}", colab_nome=f["colab_nome"],
                           matricula=f["doc"]["colaborador"]["matricula"], auto=request.args.get("auto") == "1")


@bp.post("/fichas/<int:ficha_id>/enviar")
@escrita_required
def reenviar(ficha_id):
    db = get_db()
    f = carregar_ficha(db, ficha_id) or abort(404)
    if f["status"] != "aguardando":
        flash("Esta ficha já foi assinada.", "erro")
    else:
        ok, erro = _enviar_link(db, ficha_id)
        if ok:
            flash(f"Link da ficha reenviado para o WhatsApp de {f['colab_nome']}. O link anterior deixou de valer.", "ok")
        else:
            flash(f"Não foi possível enviar: {erro}", "erro")
    return redirect(url_for("fichas.detalhe", ficha_id=ficha_id))


def _pasta_anexos():
    pasta = current_app.config.get("FICHAS_ANEXOS") or os.path.join(current_app.instance_path, "fichas")
    os.makedirs(pasta, exist_ok=True)
    return pasta


@bp.post("/fichas/<int:ficha_id>/papel")
@escrita_required
def papel(ficha_id):
    db = get_db()
    f = carregar_ficha(db, ficha_id) or abort(404)
    if f["status"] == "assinada":
        flash("Esta ficha já foi assinada pelo WhatsApp.", "erro")
        return redirect(url_for("fichas.detalhe", ficha_id=ficha_id))
    arquivo = request.files.get("anexo")
    nome_arquivo, nome_original = f["anexo_arquivo"], f["anexo_nome"]
    if arquivo and arquivo.filename:
        ext = os.path.splitext(arquivo.filename)[1].lower()
        if ext not in ANEXO_TIPOS:
            flash("Envie uma foto (JPG, PNG, WEBP) ou um PDF da ficha assinada.", "erro")
            return redirect(url_for("fichas.detalhe", ficha_id=ficha_id))
        conteudo = arquivo.read(ANEXO_MAX_BYTES + 1)
        if len(conteudo) > ANEXO_MAX_BYTES:
            flash("O arquivo passa de 10 MB. Tire uma foto menor ou reduza o PDF.", "erro")
            return redirect(url_for("fichas.detalhe", ficha_id=ficha_id))
        nome_arquivo = f"ficha_{ficha_id}_{secrets.token_hex(6)}{ext}"
        with open(os.path.join(_pasta_anexos(), nome_arquivo), "wb") as saida:
            saida.write(conteudo)
        if f["anexo_arquivo"]:
            try:
                os.remove(os.path.join(_pasta_anexos(), f["anexo_arquivo"]))
            except OSError:
                pass
        nome_original = os.path.basename(arquivo.filename)[:150]
    db.execute("""UPDATE fichas_epi SET status = 'assinada_papel', papel_por = ?, assinado_em = COALESCE(assinado_em, ?),
                  anexo_arquivo = ?, anexo_nome = ?, token_hash = NULL WHERE id = ?""",
               (g.usuario["id"], _agora(), nome_arquivo, nome_original, ficha_id))
    flash("Ficha marcada como assinada em papel." + (" Arquivo anexado." if nome_arquivo else ""), "ok")
    return redirect(url_for("fichas.detalhe", ficha_id=ficha_id))


@bp.get("/fichas/<int:ficha_id>/anexo")
def anexo(ficha_id):
    f = carregar_ficha(get_db(), ficha_id) or abort(404)
    if not f["anexo_arquivo"]:
        abort(404)
    caminho = os.path.join(_pasta_anexos(), os.path.basename(f["anexo_arquivo"]))
    if not os.path.exists(caminho):
        abort(404)
    ext = os.path.splitext(caminho)[1].lower()
    return send_file(caminho, mimetype=ANEXO_TIPOS.get(ext), download_name=f["anexo_nome"] or os.path.basename(caminho))


@bp.get("/colaboradores/<int:colab_id>/fichas/relatorio")
def relatorio_colaborador(colab_id):
    """Todas as fichas que o colaborador retirou, para controle interno, com a rubrica dele em todas as páginas."""
    db = get_db()
    colab = db.execute("SELECT * FROM colaboradores WHERE id = ?", (colab_id,)).fetchone() or abort(404)
    ids = [r["id"] for r in db.execute(
        "SELECT id FROM fichas_epi WHERE colaborador_id = ? ORDER BY id", (colab_id,))]
    fichas = [carregar_ficha(db, i) for i in ids]
    rubrica = next((f["assinatura"] for f in reversed(fichas) if f["assinatura"]), None)
    recebidas = [f for f in fichas_do_colaborador(db, colab_id) if not f["retirou"]]
    return render_template("ficha_imprimir.html", fichas=fichas, rubrica=rubrica, relatorio=True,
                           titulo=f"Relatório de fichas de EPI · {colab['nome']}", colab=colab,
                           colab_nome=colab["nome"], matricula=colab["matricula"] or colab["id"],
                           recebidas=recebidas, STATUS_FICHA=STATUS_FICHA, auto=False)


# ------------------------------------------------------------ página pública do colaborador

def _ficha_pelo_token(token):
    db = get_db()
    f = db.execute("SELECT id, link_expira_em FROM fichas_epi WHERE token_hash = ?", (_hash(token),)).fetchone()
    if not f or (f["link_expira_em"] or "") < _agora():
        return None
    return carregar_ficha(db, f["id"])


def _liberada(ficha_id):
    validade = (session.get("fichas_liberadas") or {}).get(str(ficha_id))
    return bool(validade and validade >= _agora())


def _liberar(ficha_id):
    liberadas = {k: v for k, v in (session.get("fichas_liberadas") or {}).items() if v >= _agora()}
    liberadas[str(ficha_id)] = (datetime.now() + timedelta(minutes=VALIDACAO_SESSAO_MIN)).strftime("%Y-%m-%d %H:%M:%S")
    session["fichas_liberadas"] = liberadas


def _tela_publica(f, token, **extra):
    return render_template("ficha_publica.html", f=f, token=token, liberada=_liberada(f["id"]) if f else False,
                           numero_mascarado=whatsapp.mascarar_numero(f["whatsapp_numero"] if f else ""),
                           codigo_enviado=bool(f and f["codigo_hash"] and (f["codigo_expira_em"] or "") >= _agora()),
                           **extra)


@bp.get("/assinar/<token>")
def publica(token):
    f = _ficha_pelo_token(token)
    if not f:
        return render_template("ficha_publica.html", f=None, token=token), 404
    return _tela_publica(f, token)


@bp.post("/assinar/<token>/codigo")
def publica_codigo(token):
    db = get_db()
    f = _ficha_pelo_token(token)
    if not f:
        abort(404)
    if f["status"] != "aguardando":
        return redirect(url_for("fichas.publica", token=token))
    enviado = f["codigo_enviado_em"]
    if enviado and (datetime.now() - datetime.strptime(enviado, "%Y-%m-%d %H:%M:%S")).total_seconds() < CODIGO_INTERVALO_S:
        flash("Aguarde um minuto antes de pedir um novo código.", "erro")
        return redirect(url_for("fichas.publica", token=token))
    codigo = f"{secrets.randbelow(100000):05d}"
    ok, erro = whatsapp.enviar_texto(
        f["whatsapp_numero"],
        f"Seu código para assinar a ficha de EPI é: {codigo}\n\n"
        f"Ele vale por {CODIGO_VALIDADE_MIN} minutos. Se não foi você que solicitou, apenas ignore.")
    if not ok:
        current_app.logger.warning("Código da ficha %s não enviado: %s", f["id"], erro)
        flash("Não conseguimos enviar o código agora. Tente de novo em alguns minutos.", "erro")
        return redirect(url_for("fichas.publica", token=token))
    expira = (datetime.now() + timedelta(minutes=CODIGO_VALIDADE_MIN)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute("UPDATE fichas_epi SET codigo_hash = ?, codigo_expira_em = ?, codigo_tentativas = 0, "
               "codigo_enviado_em = ? WHERE id = ?", (_hash(f"{f['id']}:{codigo}"), expira, _agora(), f["id"]))
    flash("Código enviado para o seu WhatsApp.", "ok")
    return redirect(url_for("fichas.publica", token=token))


@bp.post("/assinar/<token>/validar")
def publica_validar(token):
    db = get_db()
    f = _ficha_pelo_token(token)
    if not f:
        abort(404)
    if f["status"] != "aguardando":
        return redirect(url_for("fichas.publica", token=token))
    codigo = "".join(ch for ch in (request.form.get("codigo") or "") if ch.isdigit())
    if not f["codigo_hash"] or (f["codigo_expira_em"] or "") < _agora():
        flash("O código expirou. Peça um novo código.", "erro")
    elif f["codigo_tentativas"] >= CODIGO_MAX_TENTATIVAS:
        flash("Muitas tentativas erradas. Peça um novo código.", "erro")
    elif _hash(f"{f['id']}:{codigo}") != f["codigo_hash"]:
        db.execute("UPDATE fichas_epi SET codigo_tentativas = codigo_tentativas + 1 WHERE id = ?", (f["id"],))
        restantes = CODIGO_MAX_TENTATIVAS - f["codigo_tentativas"] - 1
        flash(f"Código incorreto. {restantes} tentativa(s) restante(s)." if restantes > 0
              else "Código incorreto. Peça um novo código.", "erro")
    else:
        db.execute("UPDATE fichas_epi SET codigo_hash = NULL, codigo_expira_em = NULL WHERE id = ?", (f["id"],))
        _liberar(f["id"])
    return redirect(url_for("fichas.publica", token=token))


def _ler_assinatura(valor):
    """Confere se veio uma imagem PNG válida do quadro de assinatura e devolve o data URL."""
    prefixo = "data:image/png;base64,"
    if not valor or not valor.startswith(prefixo):
        return None
    try:
        dados = base64.b64decode(valor[len(prefixo):], validate=True)
    except ValueError:
        return None
    if not dados.startswith(b"\x89PNG\r\n\x1a\n") or len(dados) > ASSINATURA_MAX_BYTES:
        return None
    return prefixo + base64.b64encode(dados).decode("ascii")


@bp.post("/assinar/<token>/assinar")
def publica_assinar(token):
    db = get_db()
    f = _ficha_pelo_token(token)
    if not f:
        abort(404)
    if f["status"] != "aguardando":
        return redirect(url_for("fichas.publica", token=token))
    if not _liberada(f["id"]):
        flash("Confirme o código enviado pelo WhatsApp antes de assinar.", "erro")
        return redirect(url_for("fichas.publica", token=token))
    assinatura = _ler_assinatura(request.form.get("assinatura"))
    if not assinatura:
        flash("Faça a sua assinatura na área indicada antes de enviar.", "erro")
        return redirect(url_for("fichas.publica", token=token))
    db.execute("""UPDATE fichas_epi SET status = 'assinada', assinatura = ?, assinado_em = ?, assinado_ip = ?,
                  assinado_aparelho = ? WHERE id = ? AND status = 'aguardando'""",
               (assinatura, _agora(), (request.remote_addr or "")[:64],
                (request.headers.get("User-Agent") or "")[:300], f["id"]))
    return redirect(url_for("fichas.publica", token=token))
