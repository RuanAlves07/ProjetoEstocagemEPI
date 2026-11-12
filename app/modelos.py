"""Modelos de documento: a ficha de EPI segue o layout do RH Gestor e o modelo define textos, campos e colunas."""
import json
import os
import re
from datetime import datetime

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .auth import permissao_required
from .db import get_db
from .util import limpar, to_int

bp = Blueprint("modelos", __name__)

TIPOS_MODELO = {"ficha_epi": "Ficha de EPI"}

EMPRESA_PADRAO = "ABECKER EMPREENDIMENTOS IMOBILIARIOS LTDA"

# Parâmetros que podem ser usados nos textos ({chave}) e nos campos do cabeçalho
PARAMETROS = [
    ("pedido.numero", "Número do pedido"),
    ("pedido.obra", "Obra / local de destino"),
    ("pedido.motivo", "Motivo do pedido"),
    ("pedido.solicitante", "Solicitante do pedido"),
    ("colaborador.nome", "Nome de quem retirou"),
    ("colaborador.matricula", "Matrícula"),
    ("colaborador.setor", "Setor"),
    ("colaborador.funcao", "Cargo / função"),
    ("colaborador.cpf", "CPF"),
    ("colaborador.data_nascimento", "Data de nascimento"),
    ("beneficiario.nome", "Para quem foi o pedido (quando outra pessoa retirou)"),
    ("beneficiario.matricula", "Matrícula de para quem foi o pedido"),
    ("empresa", "Empresa"),
    ("data_entrega", "Data da entrega (12/11/2026)"),
    ("data_extenso", "Data por extenso (quinta-feira, 12 de novembro de 2026)"),
    ("responsavel", "Quem registrou a retirada"),
]
ROTULOS_PARAMETROS = dict(PARAMETROS)

COLUNAS_ITENS = [
    ("ca", "Código CA"),
    ("validade_ca", "Validade CA"),
    ("descricao", "Descrição"),
    ("quantidade", "Qtde"),
    ("unidade", "Unidade"),
    ("higienizado", "Higienizado?"),
    ("data_entrega", "Data Entrega"),
    ("data_devolucao", "Data Devolução"),
    ("rubrica", "Rubrica"),
]
COLUNAS_OBRIGATORIAS = ("descricao",)

DECLARACAO_PADRAO = """Declaro para todos efeitos legais que recebi da empresa acima mencionada, os Equipamentos de Proteção Individual constantes da lista descrita neste documento, em perfeitas condições de uso, e que estou ciente das obrigações descritas na NR 06, baixada pela Portaria MTb 3214/78, subitem 6.7.1, sendo:
a) Usar, utilizando-o apenas para a finalidade a que se destina;
b) Responsabilizar-se pela guarda e conservação;
c) Comunicar ao empregador qualquer alteração que o torne impróprio para uso;
d) Cumprir as determinações do empregador sobre o uso adequado.
e) Fico proibido de dar ou emprestar o equipamento que estiver sob minha responsabilidade, só podendo fazê-lo se receber ordem por escrito da pessoa autorizada para tal fim.

Declaro, também, que estou ciente das disposições do Art. 462 e § 1º da CLT, e autorizo o desconto salarial proporcional ao custo de reparação do dano que os EPIs aos meus cuidados venham apresentar.

Declaro ainda que estou ciente das disposições do artigo 158, alínea “a”, da CLT, e do item 1.4 da NR 01, em especial daquela do subitem 1.4.2, de que constitui ato faltoso a recusa injustificada de usar EPI fornecido pela empresa, incorrendo nas penas da Lei cabíveis que irão desde simples advertências até a dispensa por justa causa (Art. 482 da C.L.T)."""


def config_padrao():
    """Modelo igual à ficha do RH Gestor (usado quando não há modelo cadastrado e nas fichas antigas)."""
    return {
        "titulo": "Controle de Entrega de EPI, Uniformes e outros materiais #{pedido.numero}",
        "subtitulo": "{colaborador.nome|maiusculas} - {colaborador.matricula}",
        "empresa": (os.environ.get("EPI_FICHA_EMPRESA") or EMPRESA_PADRAO).strip(),
        "cabecalho": [
            {"campo": "empresa", "rotulo": "Empresa"},
            {"campo": "colaborador.setor", "rotulo": "Setor"},
            {"campo": "colaborador.funcao", "rotulo": "Cargo"},
            {"campo": "colaborador.cpf", "rotulo": "CPF"},
            {"campo": "colaborador.data_nascimento", "rotulo": "Data Nascimento"},
            {"campo": "pedido.obra", "rotulo": "Local de destino"},
        ],
        "colunas": [{"coluna": c, "rotulo": r} for c, r in COLUNAS_ITENS if c != "unidade"],
        "higienizado_padrao": "SIM",
        "texto_beneficiario": "Retirado em nome de {beneficiario.nome} ({beneficiario.matricula}), solicitante do pedido.",
        "declaracao": DECLARACAO_PADRAO,
        "linha_data": "{data_extenso}",
        "assinatura_nome": "{colaborador.nome|maiusculas}",
    }


# ------------------------------------------------------------ parâmetros e montagem

_PARAM_RE = re.compile(r"\{([a-z_.]+)(\|maiusculas)?\}")


def _data_br(iso):
    try:
        return datetime.strptime((iso or "")[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return iso or ""


def valores_parametros(doc):
    """Valor de cada parâmetro a partir do documento congelado da ficha."""
    c = doc.get("colaborador") or {}
    b = doc.get("beneficiario") or {}
    return {
        "pedido.numero": str(doc.get("numero") or ""),
        "pedido.obra": doc.get("local_destino") or "",
        "pedido.motivo": doc.get("motivo") or "",
        "pedido.solicitante": doc.get("solicitante") or "",
        "colaborador.nome": c.get("nome") or "",
        "colaborador.matricula": c.get("matricula") or "",
        "colaborador.setor": c.get("setor") or "",
        "colaborador.funcao": c.get("funcao") or "",
        "colaborador.cpf": c.get("cpf") or "",
        "colaborador.data_nascimento": _data_br(c.get("data_nascimento")),
        "beneficiario.nome": b.get("nome") or "",
        "beneficiario.matricula": b.get("matricula") or "",
        "empresa": doc.get("empresa") or "",
        "data_entrega": _data_br(doc.get("data_entrega")),
        "data_extenso": doc.get("data_extenso") or "",
        "responsavel": doc.get("responsavel") or "",
    }


def substituir(texto, valores):
    def troca(m):
        if m.group(1) not in valores:
            return m.group(0)
        valor = valores[m.group(1)]
        return valor.upper() if m.group(2) else valor
    # parênteses que ficaram vazios porque o parâmetro não tinha valor, ex.: "Fulano ()"
    return re.sub(r"\s*\(\s*\)", "", _PARAM_RE.sub(troca, texto or ""))


def parametros_desconhecidos(config):
    textos = [config.get(k) or "" for k in ("titulo", "subtitulo", "empresa", "texto_beneficiario",
                                             "declaracao", "linha_data", "assinatura_nome")]
    return sorted({m.group(1) for t in textos for m in _PARAM_RE.finditer(t)} - set(ROTULOS_PARAMETROS))


def montar_layout(config, doc):
    """Textos e campos do modelo já preenchidos com os dados da ficha (congelado junto com o documento)."""
    v = valores_parametros(doc)
    v["empresa"] = substituir(config.get("empresa"), v) or v["empresa"]
    campos = [{"rotulo": c["rotulo"], "valor": v.get(c["campo"], "") or "—"} for c in config.get("cabecalho") or []]
    return {
        "titulo": substituir(config.get("titulo"), v),
        "subtitulo": substituir(config.get("subtitulo"), v),
        "cabecalho": [campos[i:i + 3] for i in range(0, len(campos), 3)],
        "colunas": config.get("colunas") or [],
        "beneficiario": substituir(config.get("texto_beneficiario"), v) if doc.get("beneficiario") else "",
        "declaracao": [p.strip().split("\n") for p in re.split(r"\n\s*\n", substituir(config.get("declaracao"), v))
                       if p.strip()],
        "linha_data": substituir(config.get("linha_data"), v),
        "assinatura_nome": substituir(config.get("assinatura_nome"), v),
    }


@bp.app_template_global()
def layout_ficha(doc):
    """Fichas emitidas antes dos modelos não têm o layout congelado: usam o modelo padrão."""
    if doc.get("layout"):
        return doc["layout"]
    config = config_padrao()
    config["empresa"] = doc.get("empresa") or config["empresa"]
    return montar_layout(config, doc)


def documento_exemplo():
    return {
        "numero": "123", "pedido_id": 0, "local_destino": "Acordes • Garuva", "motivo": "Admissão",
        "solicitante": "Maria Souza", "empresa": EMPRESA_PADRAO, "responsavel": "Almoxarifado",
        "colaborador": {"nome": "João da Silva", "matricula": "1234", "setor": "Obras", "funcao": "Pedreiro",
                        "cpf": "123.456.789-09", "data_nascimento": "1990-05-20"},
        "beneficiario": {"nome": "Maria Souza", "matricula": "5678"},
        "data_entrega": "2026-11-12", "data_extenso": "quinta-feira, 12 de novembro de 2026",
        "itens": [
            {"mov_id": 1, "ca": "40377", "validade_ca": "2029-03-10", "descricao": "Luva nitrílica (M)",
             "quantidade": 2, "unidade": "par", "higienizado": "SIM", "data_entrega": "2026-11-12"},
            {"mov_id": 2, "ca": "", "validade_ca": "", "descricao": "Camiseta manga longa (G)",
             "quantidade": 1, "unidade": "un", "higienizado": "SIM", "data_entrega": "2026-11-12"},
        ],
    }


# ------------------------------------------------------------ consultas

def _linha_para_modelo(r):
    m = dict(r)
    config = config_padrao()
    try:
        config.update(json.loads(m["config"]))
    except (TypeError, ValueError):
        pass
    m["config"] = config
    return m


def carregar_modelo(db, modelo_id):
    r = db.execute("SELECT * FROM modelos_documento WHERE id = ?", (modelo_id,)).fetchone()
    return _linha_para_modelo(r) if r else None


def modelos_ativos(db, tipo="ficha_epi"):
    linhas = db.execute("SELECT * FROM modelos_documento WHERE tipo = ? AND ativo = 1 ORDER BY padrao DESC, nome",
                        (tipo,)).fetchall()
    return [_linha_para_modelo(r) for r in linhas]


def modelo_para_ficha(db, modelo_id=None):
    """Modelo escolhido (se ativo), senão o padrão; sem nenhum cadastrado, o layout do RH Gestor."""
    ativos = modelos_ativos(db)
    escolhido = next((m for m in ativos if m["id"] == modelo_id), None)
    if escolhido or ativos:
        return escolhido or ativos[0]
    return {"id": None, "nome": "Padrão do sistema", "config": config_padrao(), "padrao": 1}


def garantir_modelo_padrao(conn):
    if conn.execute("SELECT 1 FROM modelos_documento WHERE tipo = 'ficha_epi' LIMIT 1").fetchone():
        return
    conn.execute("INSERT INTO modelos_documento (nome, tipo, config, padrao) VALUES (?, 'ficha_epi', ?, 1)",
                 ("Ficha de EPI (padrão RH Gestor)", json.dumps(config_padrao(), ensure_ascii=False)))
    if hasattr(conn, "commit"):
        conn.commit()


# ------------------------------------------------------------ leitura do formulário do editor

def ler_config(form):
    """Monta a configuração a partir do formulário do editor. Devolve (config, erros)."""
    erros = []
    cabecalho = []
    for campo, rotulo in zip(form.getlist("cab_campo"), form.getlist("cab_rotulo")):
        if campo in ROTULOS_PARAMETROS:
            cabecalho.append({"campo": campo, "rotulo": (rotulo or "").strip() or ROTULOS_PARAMETROS[campo]})
    marcadas = set(form.getlist("col_ativa")) | set(COLUNAS_OBRIGATORIAS)
    colunas = [{"coluna": c, "rotulo": (form.get(f"col_rotulo_{c}") or "").strip() or r}
               for c, r in COLUNAS_ITENS if c in marcadas]
    config = {
        "titulo": (form.get("titulo") or "").strip(),
        "subtitulo": (form.get("subtitulo") or "").strip(),
        "empresa": (form.get("empresa") or "").strip(),
        "cabecalho": cabecalho,
        "colunas": colunas,
        "higienizado_padrao": "NÃO" if form.get("higienizado_padrao") == "NAO" else "SIM",
        "texto_beneficiario": (form.get("texto_beneficiario") or "").strip(),
        "declaracao": (form.get("declaracao") or "").replace("\r\n", "\n").strip(),
        "linha_data": (form.get("linha_data") or "").strip(),
        "assinatura_nome": (form.get("assinatura_nome") or "").strip(),
    }
    if not config["titulo"]:
        erros.append("Informe o título do documento.")
    desconhecidos = parametros_desconhecidos(config)
    if desconhecidos:
        erros.append("Parâmetro(s) que não existem: " + ", ".join("{" + p + "}" for p in desconhecidos)
                     + ". Use os botões de parâmetros para inserir.")
    return config, erros


def _agora():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------ rotas

@bp.get("/modelos")
@permissao_required("gerenciar_modelos")
def lista():
    db = get_db()
    linhas = db.execute(
        """SELECT m.*, u.nome AS criado_por_nome FROM modelos_documento m
           LEFT JOIN usuarios u ON u.id = m.criado_por
           ORDER BY m.ativo DESC, m.padrao DESC, m.nome""").fetchall()
    return render_template("modelos_lista.html", modelos=linhas, TIPOS_MODELO=TIPOS_MODELO)


def _tela_editor(modelo, config, erros=()):
    return render_template("modelo_form.html", m=modelo, config=config, erros=erros, TIPOS_MODELO=TIPOS_MODELO,
                           PARAMETROS=PARAMETROS, COLUNAS_ITENS=COLUNAS_ITENS,
                           COLUNAS_OBRIGATORIAS=COLUNAS_OBRIGATORIAS,
                           colunas_ativas={c["coluna"]: c["rotulo"] for c in config["colunas"]},
                           doc=_doc_previa(config))


def _doc_previa(config):
    doc = documento_exemplo()
    for it in doc["itens"]:
        it["higienizado"] = config["higienizado_padrao"]
    doc["layout"] = montar_layout(config, doc)
    return doc


@bp.route("/modelos/novo", methods=["GET", "POST"])
@permissao_required("gerenciar_modelos")
def novo():
    base = carregar_modelo(get_db(), to_int(request.args.get("copiar"))) if request.args.get("copiar") else None
    modelo = {"id": None, "nome": f"Cópia de {base['nome']}" if base else "", "tipo": "ficha_epi",
              "padrao": 0, "ativo": 1}
    return _salvar(modelo, base["config"] if base else config_padrao())


@bp.route("/modelos/<int:modelo_id>", methods=["GET", "POST"])
@permissao_required("gerenciar_modelos")
def editar(modelo_id):
    modelo = carregar_modelo(get_db(), modelo_id) or abort(404)
    return _salvar(modelo, modelo["config"])


def _salvar(modelo, config):
    if request.method == "GET":
        return _tela_editor(modelo, config)
    db = get_db()
    config, erros = ler_config(request.form)
    nome = limpar(request.form.get("nome"))
    tipo = request.form.get("tipo") if request.form.get("tipo") in TIPOS_MODELO else "ficha_epi"
    padrao = 1 if request.form.get("padrao") else 0
    modelo = {**modelo, "nome": nome or "", "tipo": tipo, "padrao": padrao}
    if not nome:
        erros.insert(0, "Informe o nome do modelo.")
    if erros:
        return _tela_editor(modelo, config, erros), 400
    texto = json.dumps(config, ensure_ascii=False)
    if modelo["id"]:
        db.execute("UPDATE modelos_documento SET nome = ?, tipo = ?, config = ?, atualizado_em = ? WHERE id = ?",
                   (nome, tipo, texto, _agora(), modelo["id"]))
        modelo_id = modelo["id"]
    else:
        cur = db.execute("INSERT INTO modelos_documento (nome, tipo, config, criado_por) VALUES (?, ?, ?, ?)",
                         (nome, tipo, texto, g.usuario["id"]))
        modelo_id = cur.lastrowid
    if padrao:
        _tornar_padrao(db, modelo_id, tipo)
    elif not db.execute("SELECT 1 FROM modelos_documento WHERE tipo = ? AND padrao = 1 AND ativo = 1",
                        (tipo,)).fetchone():
        _tornar_padrao(db, modelo_id, tipo)
    flash(f"Modelo “{nome}” salvo. As fichas já emitidas não mudam; o modelo vale para as próximas retiradas.", "ok")
    return redirect(url_for("modelos.editar", modelo_id=modelo_id))


def _tornar_padrao(db, modelo_id, tipo):
    db.execute("UPDATE modelos_documento SET padrao = CASE WHEN id = ? THEN 1 ELSE 0 END WHERE tipo = ?",
               (modelo_id, tipo))
    db.execute("UPDATE modelos_documento SET ativo = 1 WHERE id = ?", (modelo_id,))


@bp.post("/modelos/previa")
@permissao_required("gerenciar_modelos")
def previa():
    config, erros = ler_config(request.form)
    return render_template("_modelo_previa.html", doc=_doc_previa(config), erros=erros)


@bp.post("/modelos/<int:modelo_id>/padrao")
@permissao_required("gerenciar_modelos")
def tornar_padrao(modelo_id):
    db = get_db()
    m = carregar_modelo(db, modelo_id) or abort(404)
    _tornar_padrao(db, modelo_id, m["tipo"])
    flash(f"“{m['nome']}” agora é o modelo usado na retirada de pedidos.", "ok")
    return redirect(url_for("modelos.lista"))


@bp.post("/modelos/<int:modelo_id>/situacao")
@permissao_required("gerenciar_modelos")
def situacao(modelo_id):
    db = get_db()
    m = carregar_modelo(db, modelo_id) or abort(404)
    if m["ativo"] and m["padrao"]:
        flash("Este é o modelo padrão. Escolha outro modelo como padrão antes de inativar.", "erro")
        return redirect(url_for("modelos.lista"))
    db.execute("UPDATE modelos_documento SET ativo = ? WHERE id = ?", (0 if m["ativo"] else 1, modelo_id))
    flash(f"Modelo “{m['nome']}” {'inativado' if m['ativo'] else 'reativado'}.", "ok")
    return redirect(url_for("modelos.lista"))


@bp.post("/modelos/<int:modelo_id>/excluir")
@permissao_required("gerenciar_modelos")
def excluir(modelo_id):
    db = get_db()
    m = carregar_modelo(db, modelo_id) or abort(404)
    if m["padrao"]:
        flash("O modelo padrão não pode ser excluído. Escolha outro modelo como padrão antes.", "erro")
        return redirect(url_for("modelos.lista"))
    # as fichas emitidas guardam uma cópia do modelo, então excluir não altera nenhuma ficha
    db.execute("DELETE FROM modelos_documento WHERE id = ?", (modelo_id,))
    flash(f"Modelo “{m['nome']}” excluído.", "ok")
    return redirect(url_for("modelos.lista"))
