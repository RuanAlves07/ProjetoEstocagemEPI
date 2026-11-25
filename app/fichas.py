"""Ficha de EPI da retirada: prévia, envio pelo WhatsApp, código de validação, assinatura e impressão."""
import base64
import hashlib
import json
import os
import secrets
import threading
from datetime import date, datetime, timedelta

from flask import (Blueprint, abort, current_app, flash, g, redirect, render_template, request,
                   send_file, session, url_for)

from . import email_service, modelos, pedidos, push_service, whatsapp
from .auth import escrita_required
from .db import get_db
from .estoque import ErroValidacao
from .movimentos import consultar_movimentos
from .util import limpar, to_int

bp = Blueprint("fichas", __name__)

LINK_VALIDADE_DIAS = 7
LEMBRETE_ENTREGA_DIAS = 3        # entrega levada por outra pessoa sem confirmação: lembra o destinatário
CODIGO_VALIDADE_MIN = 15
CODIGO_MAX_TENTATIVAS = 5
CODIGO_INTERVALO_S = 60          # espera mínima entre um envio de código e outro
VALIDACAO_SESSAO_MIN = 30        # depois de validar o código, quanto tempo a ficha fica liberada
ASSINATURA_MAX_BYTES = 400 * 1024
ANEXO_MAX_BYTES = 10 * 1024 * 1024
ANEXO_TIPOS = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
               ".pdf": "application/pdf"}

FOTO_MAX_BYTES = 10 * 1024 * 1024
FOTO_TIPOS = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
# fotos obrigatórias da entrega, tiradas por quem separou (ou outra pessoa do almoxarifado)
FOTOS_ENTREGA = (
    ("foto_colaborador", "Colaborador com a sacola de EPI"),
    ("foto_sacola", "Sacola de EPI em destaque"),
)

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


def montar_documento(db, pedido, colab, data_entrega=None, modelo=None):
    """Dados e itens da ficha, congelados no momento da retirada junto com o modelo já preenchido."""
    modelo = modelo or modelos.modelo_para_ficha(db)
    config = modelo["config"]
    data_entrega = data_entrega or date.today().isoformat()
    itens = _itens_do_pedido(db, pedido)
    for it in itens:
        it["data_entrega"] = data_entrega
        it["higienizado"] = config.get("higienizado_padrao") or "SIM"
    beneficiario = None
    if pedido["colaborador_id"] and pedido["colaborador_id"] != colab["id"]:
        b = db.execute("SELECT nome, matricula FROM colaboradores WHERE id = ?", (pedido["colaborador_id"],)).fetchone()
        if b:
            beneficiario = {"nome": b["nome"], "matricula": b["matricula"]}
    doc = {
        "numero": pedidos.numero_do_pedido(db, pedido["id"]),
        "pedido_id": pedido["id"],
        "empresa": (os.environ.get("EPI_FICHA_EMPRESA") or modelos.EMPRESA_PADRAO).strip(),
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
        "motivo": pedido["motivo"] or "",
        "data_entrega": data_entrega,
        "data_extenso": data_por_extenso(data_entrega),
        "responsavel": g.usuario["nome"] if g.get("usuario") else "",
        "itens": itens,
        "modelo": {"id": modelo["id"], "nome": modelo["nome"]},
    }
    doc["empresa"] = modelos.substituir(config.get("empresa"), modelos.valores_parametros(doc)) or doc["empresa"]
    doc["layout"] = modelos.montar_layout(config, doc)
    return doc


def _documento_json(doc):
    return json.dumps(doc, ensure_ascii=False, sort_keys=True)


def fotos_faltando(f):
    """Rótulos das fotos da entrega que ainda faltam (vazio quando a ficha não exige fotos)."""
    if not f["fotos_exigidas"]:
        return []
    return [rotulo for campo, rotulo in FOTOS_ENTREGA if not f[campo]]


def pendencias(f):
    """O que falta para a ficha ficar concluída: assinatura do colaborador e/ou fotos da entrega."""
    faltam = []
    if f["status"] == "aguardando":
        faltam.append("assinatura")
    if fotos_faltando(f):
        faltam.append("fotos")
    return faltam


def em_transito(f):
    """Outra pessoa levou o pedido e o destinatário ainda não confirmou o recebimento (assinando a ficha)."""
    return bool(f["portador_id"]) and f["status"] == "aguardando"


def problema_aberto(f):
    return bool(f["problema_em"]) and not f["problema_resolvido_em"]


def situacao(f):
    """(rótulo, classe do badge) da ficha para listas e cabeçalhos."""
    if f["portador_id"] and problema_aberto(f):
        return "Destinatário avisou: não recebeu ou faltou item", "vermelho"
    if em_transito(f):
        return "Em trânsito · aguardando o destinatário confirmar" + (" · faltam as fotos" if fotos_faltando(f) else ""), "amarelo"
    faltam = pendencias(f)
    if not faltam:
        return STATUS_FICHA.get(f["status"], f["status"]) + (" · concluída" if f["fotos_exigidas"] else ""), "verde"
    if "assinatura" not in faltam:  # já assinada, só faltam as fotos
        return STATUS_FICHA.get(f["status"], f["status"]) + " · faltam as fotos", "amarelo"
    return "Pendente: " + " e ".join(faltam), "amarelo"


@bp.app_template_global("situacao_ficha")
def _situacao_ficha_global(f):
    return situacao(f)


@bp.app_template_global("entregas_em_transito")
def entregas_em_transito():
    """Pedidos levados por outra pessoa que o destinatário ainda não confirmou (mais antigos primeiro)."""
    linhas = get_db().execute(
        """SELECT f.id, f.pedido_id, f.criado_em, f.problema_texto, f.problema_em, f.problema_resolvido_em,
                  f.lembrete_em, c.nome AS colab_nome, c.whatsapp AS colab_whatsapp, pt.nome AS portador_nome
           FROM fichas_epi f
           JOIN colaboradores c ON c.id = f.colaborador_id
           JOIN colaboradores pt ON pt.id = f.portador_id
           WHERE f.status = 'aguardando'
           ORDER BY f.id""").fetchall()
    hoje_ = date.today()
    resultado = []
    for r in linhas:
        r = dict(r)
        r["dias"] = (hoje_ - date.fromisoformat(r["criado_em"][:10])).days
        r["problema"] = problema_aberto(r)
        r["atrasada"] = r["dias"] >= LEMBRETE_ENTREGA_DIAS
        resultado.append(r)
    resultado.sort(key=lambda r: (not r["problema"], not r["atrasada"], r["id"]))
    return resultado


def lembrar_entregas_atrasadas(db):
    """Reenvia, uma vez, o link para o destinatário de entregas sem confirmação há LEMBRETE_ENTREGA_DIAS dias.
    Chamado ao abrir o Painel e Pedidos; o envio pelo WhatsApp roda em segundo plano."""
    limite = (datetime.now() - timedelta(days=LEMBRETE_ENTREGA_DIAS)).strftime("%Y-%m-%d %H:%M:%S")
    ids = []
    for r in db.execute("""SELECT f.id FROM fichas_epi f JOIN colaboradores c ON c.id = f.colaborador_id
                           WHERE f.portador_id IS NOT NULL AND f.status = 'aguardando' AND f.lembrete_em IS NULL
                             AND f.criado_em <= ? AND c.whatsapp IS NOT NULL""", (limite,)).fetchall():
        # marca antes de enviar: o outro processo do servidor não manda o mesmo lembrete
        if db.execute("UPDATE fichas_epi SET lembrete_em = ? WHERE id = ? AND lembrete_em IS NULL",
                      (_agora(), r["id"])).rowcount:
            ids.append(r["id"])
    if not ids:
        return 0
    app = current_app._get_current_object()

    def enviar():
        with app.app_context():
            for ficha_id in ids:
                try:
                    ok, erro = _enviar_link(get_db(), ficha_id, lembrete=True)
                    if not ok:
                        app.logger.warning("Lembrete da ficha %s não enviado: %s", ficha_id, erro)
                except Exception:
                    app.logger.exception("Falha no lembrete da ficha %s", ficha_id)

    if app.config.get("TESTING"):
        enviar()
    else:
        threading.Thread(target=enviar, daemon=True).start()
    return len(ids)


@bp.app_template_global("fotos_pendentes")
def _fotos_pendentes_global():
    return fichas_com_fotos_pendentes(get_db())


def carregar_ficha(db, ficha_id):
    f = db.execute(
        """SELECT f.*, c.nome AS colab_nome, c.whatsapp AS colab_whatsapp, b.nome AS benef_nome,
                  u.nome AS criado_por_nome, up.nome AS papel_por_nome, uf.nome AS fotos_por_nome,
                  p.finalizado_por AS separado_por, us.nome AS separado_por_nome,
                  pt.nome AS portador_nome, pt.matricula AS portador_matricula, pt.cpf AS portador_cpf,
                  pt.setor AS portador_setor, pt.funcao AS portador_funcao
           FROM fichas_epi f
           JOIN colaboradores c ON c.id = f.colaborador_id
           LEFT JOIN colaboradores b ON b.id = f.beneficiario_id
           LEFT JOIN colaboradores pt ON pt.id = f.portador_id
           JOIN usuarios u ON u.id = f.criado_por
           LEFT JOIN usuarios up ON up.id = f.papel_por
           LEFT JOIN usuarios uf ON uf.id = f.fotos_por
           LEFT JOIN pedidos p ON p.id = f.pedido_id
           LEFT JOIN usuarios us ON us.id = p.finalizado_por
           WHERE f.id = ?""", (ficha_id,)).fetchone()
    if not f:
        return None
    f = dict(f)
    f["doc"] = json.loads(f["documento"])
    f["fotos_faltando"] = fotos_faltando(f)
    f["pendencias"] = pendencias(f)
    f["situacao"] = situacao(f)
    f["em_transito"] = em_transito(f)
    return f


def fichas_com_fotos_pendentes(db):
    """Fichas de retiradas que ainda não têm as 2 fotos da entrega (card do almoxarifado)."""
    linhas = db.execute(
        """SELECT f.id, f.pedido_id, f.status, f.criado_em, f.fotos_exigidas, f.foto_colaborador, f.foto_sacola,
                  c.nome AS colab_nome, p.finalizado_por AS separado_por, us.nome AS separado_por_nome
           FROM fichas_epi f
           JOIN colaboradores c ON c.id = f.colaborador_id
           LEFT JOIN pedidos p ON p.id = f.pedido_id
           LEFT JOIN usuarios us ON us.id = p.finalizado_por
           WHERE f.fotos_exigidas = 1 AND (f.foto_colaborador IS NULL OR f.foto_sacola IS NULL)
           ORDER BY f.id""").fetchall()
    resultado = []
    for r in linhas:
        r = dict(r)
        r["fotos_faltando"] = fotos_faltando(r)
        resultado.append(r)
    return resultado


def fichas_do_colaborador(db, colab_id):
    """Fichas em que o colaborador retirou ou em que o pedido era para ele (retirado por outra pessoa)."""
    linhas = db.execute(
        """SELECT f.id, f.pedido_id, f.status, f.criado_em, f.assinado_em, f.documento,
                  f.fotos_exigidas, f.foto_colaborador, f.foto_sacola, f.portador_id, f.problema_em,
                  f.problema_resolvido_em, pt.nome AS portador_nome,
                  f.colaborador_id, f.beneficiario_id, c.nome AS colab_nome, b.nome AS benef_nome
           FROM fichas_epi f
           JOIN colaboradores c ON c.id = f.colaborador_id
           LEFT JOIN colaboradores b ON b.id = f.beneficiario_id
           LEFT JOIN colaboradores pt ON pt.id = f.portador_id
           WHERE f.colaborador_id = ? OR f.beneficiario_id = ? OR f.portador_id = ?
           ORDER BY f.id DESC""", (colab_id, colab_id, colab_id)).fetchall()
    resultado = []
    for r in linhas:
        r = dict(r)
        doc = json.loads(r.pop("documento"))
        r["numero"] = doc.get("numero")
        r["qtd_itens"] = sum(int(i.get("quantidade") or 0) for i in doc.get("itens", []))
        r["retirou"] = r["colaborador_id"] == colab_id
        r["levou"] = r["portador_id"] == colab_id
        r["situacao"] = situacao(r)
        resultado.append(r)
    return resultado


# ------------------------------------------------------------ revisão e emissão (equipe)

def _pedido_pronto(db, pedido_id):
    encontrados = pedidos.carregar(db, pedido_id=pedido_id)
    if not encontrados:
        abort(404)
    return encontrados[0]


def _aplicar_higienizado(doc):
    """Coluna "Higienizado?" escolhida item a item na tela de revisão (sem escolha, vale o padrão do modelo)."""
    for it in doc["itens"]:
        escolha = request.values.get(f"hig_{it['mov_id']}")
        if escolha:
            it["higienizado"] = "NÃO" if escolha == "NAO" else "SIM"


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
    modelo = modelos.modelo_para_ficha(db, to_int(request.args.get("modelo_id")))
    doc = None
    if colab:
        doc = montar_documento(db, pedido, colab, modelo=modelo)
        _aplicar_higienizado(doc)
    return render_template("ficha_preparar.html", p=pedido, colaboradores=colaboradores, colab=colab, doc=doc,
                           modelo=modelo, modelos_ficha=modelos.modelos_ativos(db),
                           whatsapp_ativo=whatsapp.configurado() or current_app.config.get("TESTING"))


@bp.post("/pedidos/<int:pedido_id>/ficha")
@escrita_required
def emitir(pedido_id):
    db = get_db()
    pedido = _pedido_pronto(db, pedido_id)
    acao = request.form.get("acao")
    colab = db.execute("SELECT * FROM colaboradores WHERE id = ? AND ativo = 1",
                       (to_int(request.form.get("colaborador_id")),)).fetchone()
    modelo = modelos.modelo_para_ficha(db, to_int(request.form.get("modelo_id")))
    voltar = redirect(url_for("fichas.preparar", pedido_id=pedido_id, modelo_id=modelo["id"],
                              colaborador_id=request.form.get("colaborador_id") or None))
    if not colab:
        flash("Selecione para quem é o pedido (quem vai assinar a ficha).", "erro")
        return voltar
    portador, termo = None, None
    if request.form.get("quem_retira") == "outro":
        portador = db.execute("SELECT * FROM colaboradores WHERE id = ? AND ativo = 1",
                              (to_int(request.form.get("portador_id")),)).fetchone()
        if not portador:
            flash("Selecione quem está levando o pedido.", "erro")
            return voltar
        if portador["id"] == colab["id"]:
            flash(f"Quem está levando é {colab['nome']}, o destinatário: marque “{colab['nome']} está retirando”.", "erro")
            return voltar
        termo = _ler_assinatura(request.form.get("termo_assinatura"))
        if not termo:
            flash(f"{portador['nome']} precisa assinar o termo de responsabilidade antes de levar o pedido.", "erro")
            return voltar
    if acao == "enviar" and not colab["whatsapp"]:
        flash(f"{colab['nome']} não tem WhatsApp no cadastro. Cadastre o número ou use Imprimir ficha.", "erro")
        return voltar
    doc = montar_documento(db, pedido, colab, modelo=modelo)
    _aplicar_higienizado(doc)
    try:
        pedidos.retirar(db, pedido_id, (portador or colab)["nome"], limpar(request.form.get("retirado_descricao")),
                        g.usuario["id"])
    except ErroValidacao as e:
        flash(str(e), "erro")
        return redirect(url_for("pedidos.detalhe", pedido_id=pedido_id))
    texto = _documento_json(doc)
    cur = db.execute(
        """INSERT INTO fichas_epi (pedido_id, colaborador_id, beneficiario_id, documento, documento_hash, criado_por,
                                  fotos_exigidas, portador_id, termo_assinatura, termo_assinado_em)
           VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
        (pedido_id, colab["id"], pedido["colaborador_id"], texto, _hash(texto), g.usuario["id"],
         portador["id"] if portador else None, termo, _agora() if termo else None))
    ficha_id = cur.lastrowid
    erros_foto = gravar_fotos(db, ficha_id, request.files)
    for e in erros_foto:
        flash(e, "erro")
    faltam_fotos = fotos_faltando(carregar_ficha(db, ficha_id))
    if faltam_fotos:
        flash("Faltam as fotos da entrega (" + "; ".join(faltam_fotos) + "). "
              "Elas ficam pendentes no card “Fotos da entrega” para quem separou o pedido.", "alerta")
    try:
        email_service.notificar_evento_pedido(db, pedido_id, "retirado", ator_nome=g.usuario["nome"])
    except Exception:
        current_app.logger.exception("Falha ao notificar a retirada do pedido %s", pedido_id)
    if acao == "enviar":
        ok, erro = _enviar_link(db, ficha_id)
        if ok and portador:
            flash(f"Retirada registrada: {portador['nome']} levou o pedido e assinou o termo. A ficha foi enviada para o "
                  f"WhatsApp de {colab['nome']}, que assina quando receber.", "ok")
        elif ok:
            flash(f"Retirada registrada e ficha de EPI enviada para o WhatsApp de {colab['nome']}.", "ok")
        else:
            flash(f"Retirada registrada, mas a ficha não foi enviada: {erro} Tente reenviar ou imprima a ficha.", "erro")
        return redirect(url_for("fichas.detalhe", ficha_id=ficha_id))
    flash("Retirada registrada. Imprima a ficha para colher a assinatura"
          + (f" de {colab['nome']} na entrega dos itens (a ficha vai junto com {portador['nome']})." if portador else "."), "ok")
    return redirect(url_for("fichas.imprimir", ficha_id=ficha_id, auto=1))


def _enviar_link(db, ficha_id, lembrete=False):
    """Gera um link novo (o anterior deixa de valer) e manda pelo WhatsApp do colaborador."""
    f = carregar_ficha(db, ficha_id)
    numero = f["colab_whatsapp"]
    if not numero:
        return False, "o colaborador não tem WhatsApp no cadastro."
    token = secrets.token_urlsafe(24)
    expira = (datetime.now() + timedelta(days=LINK_VALIDADE_DIAS)).strftime("%Y-%m-%d %H:%M:%S")
    link = email_service.url_externa("fichas.publica", token=token)
    if f["portador_id"] and lembrete:
        corpo = (f"O pedido #{f['doc']['numero']} foi retirado por {f['portador_nome']} para você em "
                 f"{f['criado_em'][8:10]}/{f['criado_em'][5:7]} e a entrega ainda não foi confirmada.\n\n"
                 f"Se você já recebeu os itens, assine a ficha de EPI pelo link abaixo. "
                 f"Se não recebeu ou se faltou algum item, avise pelo mesmo link:\n{link}\n\n")
    elif f["portador_id"]:
        corpo = (f"O seu pedido de EPI #{f['doc']['numero']} foi retirado por {f['portador_nome']}, "
                 f"que vai levar os itens até você.\n\n"
                 f"Quando receber, abra o link abaixo para conferir e assinar a ficha de EPI. "
                 f"Se não receber ou se faltar algum item, avise pelo mesmo link:\n{link}\n\n")
    else:
        corpo = (f"Você está recebendo esta mensagem para assinar eletronicamente a ficha de entrega de EPI "
                 f"do pedido #{f['doc']['numero']}.\n\n"
                 f"Para acessar o documento, clique no link abaixo:\n{link}\n\n")
    texto = (f"Olá {f['colab_nome']},\n\n" + corpo +
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
    return render_template("ficha_detalhe.html", f=f, STATUS_FICHA=STATUS_FICHA, FOTOS_ENTREGA=FOTOS_ENTREGA,
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


def _salvar_foto(ficha_id, campo, arquivo):
    """Grava a foto em instance/fichas/ e devolve o nome do arquivo. Levanta ErroValidacao se for inválida."""
    ext = os.path.splitext(arquivo.filename or "")[1].lower()
    if ext not in FOTO_TIPOS:
        raise ErroValidacao("Envie as fotos da entrega em JPG, PNG ou WEBP.")
    conteudo = arquivo.read(FOTO_MAX_BYTES + 1)
    if len(conteudo) > FOTO_MAX_BYTES:
        raise ErroValidacao("A foto passa de 10 MB. Tire outra foto ou reduza o tamanho.")
    if not conteudo:
        raise ErroValidacao("A foto chegou vazia. Tente de novo.")
    nome = f"ficha_{ficha_id}_{campo}_{secrets.token_hex(6)}{ext}"
    with open(os.path.join(_pasta_anexos(), nome), "wb") as saida:
        saida.write(conteudo)
    return nome


def gravar_fotos(db, ficha_id, arquivos):
    """Grava as fotos da entrega enviadas (uma ou as duas). Uma foto nova substitui a anterior. Devolve os erros."""
    f = db.execute("SELECT * FROM fichas_epi WHERE id = ?", (ficha_id,)).fetchone()
    erros, novas = [], {}
    for campo, rotulo in FOTOS_ENTREGA:
        arquivo = arquivos.get(campo)
        if not arquivo or not arquivo.filename:
            continue
        try:
            novas[campo] = _salvar_foto(ficha_id, campo, arquivo)
        except ErroValidacao as e:
            erros.append(f"{rotulo}: {e}")
    if novas:
        sets = ", ".join(f"{campo} = ?" for campo in novas)
        db.execute(f"UPDATE fichas_epi SET {sets}, fotos_por = ?, fotos_em = ? WHERE id = ?",
                   (*novas.values(), g.usuario["id"], _agora(), ficha_id))
        for campo in novas:
            if f[campo]:
                try:
                    os.remove(os.path.join(_pasta_anexos(), os.path.basename(f[campo])))
                except OSError:
                    pass
    return erros


@bp.post("/fichas/<int:ficha_id>/fotos")
@escrita_required
def fotos(ficha_id):
    db = get_db()
    carregar_ficha(db, ficha_id) or abort(404)
    if not any(request.files.get(campo) and request.files[campo].filename for campo, _ in FOTOS_ENTREGA):
        flash("Escolha ou tire as fotos da entrega antes de enviar.", "erro")
        return redirect(url_for("fichas.detalhe", ficha_id=ficha_id))
    for e in gravar_fotos(db, ficha_id, request.files):
        flash(e, "erro")
    f = carregar_ficha(db, ficha_id)
    if f["fotos_faltando"]:
        flash("Foto salva. Ainda falta: " + "; ".join(f["fotos_faltando"]) + ".", "alerta")
    elif not f["pendencias"]:
        flash("Fotos da entrega salvas. A ficha está concluída.", "ok")
    else:
        flash("Fotos da entrega salvas. Falta a assinatura do colaborador.", "ok")
    return redirect(url_for("fichas.detalhe", ficha_id=ficha_id))


@bp.get("/fichas/<int:ficha_id>/foto/<campo>")
def foto(ficha_id, campo):
    if campo not in dict(FOTOS_ENTREGA):
        abort(404)
    f = carregar_ficha(get_db(), ficha_id) or abort(404)
    if not f[campo]:
        abort(404)
    caminho = os.path.join(_pasta_anexos(), os.path.basename(f[campo]))
    if not os.path.exists(caminho):
        abort(404)
    return send_file(caminho, mimetype=FOTO_TIPOS.get(os.path.splitext(caminho)[1].lower()))


@bp.post("/fichas/<int:ficha_id>/problema/resolver")
@escrita_required
def resolver_problema(ficha_id):
    db = get_db()
    f = carregar_ficha(db, ficha_id) or abort(404)
    if problema_aberto(f):
        db.execute("UPDATE fichas_epi SET problema_resolvido_em = ? WHERE id = ?", (_agora(), ficha_id))
        flash("Aviso do destinatário marcado como resolvido.", "ok")
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
    recebidas = [f for f in fichas_do_colaborador(db, colab_id) if not f["retirou"] and not f["levou"]]
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


@bp.post("/assinar/<token>/problema")
def publica_problema(token):
    db = get_db()
    f = _ficha_pelo_token(token)
    if not f:
        abort(404)
    if f["status"] != "aguardando" or not f["portador_id"]:
        return redirect(url_for("fichas.publica", token=token))
    if not _liberada(f["id"]):
        flash("Confirme o código enviado pelo WhatsApp antes de continuar.", "erro")
        return redirect(url_for("fichas.publica", token=token))
    motivo = {"nao_recebi": "Não recebi os itens", "faltou": "Faltou item"}.get(request.form.get("motivo"))
    detalhe = limpar(request.form.get("detalhe"))
    if not motivo:
        flash("Escolha o que aconteceu.", "erro")
        return redirect(url_for("fichas.publica", token=token))
    texto = motivo + (f": {detalhe[:500]}" if detalhe else "")
    db.execute("UPDATE fichas_epi SET problema_texto = ?, problema_em = ?, problema_resolvido_em = NULL WHERE id = ?",
               (texto, _agora(), f["id"]))
    _avisar_equipe_problema(f, texto)
    flash("Avisamos o RH. Quando receber os itens, você pode voltar a este link e assinar.", "ok")
    return redirect(url_for("fichas.publica", token=token))


def _avisar_equipe_problema(f, texto):
    """Notificação no navegador/celular da equipe (quem ativou as notificações). Falha não impede o aviso."""
    if current_app.config.get("TESTING"):
        return
    app = current_app._get_current_object()
    payload = {"title": f"⚠️ Entrega do pedido #{f['doc']['numero']} com problema",
               "body": f"{f['colab_nome']}: {texto} (levado por {f['portador_nome']})",
               "icon": "/static/icone.png", "url": f"/fichas/{f['id']}", "tag": f"ficha-problema-{f['id']}"}

    def enviar():
        try:
            with app.app_context():
                push_service.enviar_para_todos(get_db(), payload)
        except Exception:
            app.logger.exception("Falha no aviso de problema da ficha %s", f["id"])

    threading.Thread(target=enviar, daemon=True).start()


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
                  assinado_aparelho = ?,
                  problema_resolvido_em = CASE WHEN problema_em IS NOT NULL AND problema_resolvido_em IS NULL
                                               THEN ? ELSE problema_resolvido_em END
                  WHERE id = ? AND status = 'aguardando'""",
               (assinatura, _agora(), (request.remote_addr or "")[:64],
                (request.headers.get("User-Agent") or "")[:300], _agora(), f["id"]))
    return redirect(url_for("fichas.publica", token=token))
