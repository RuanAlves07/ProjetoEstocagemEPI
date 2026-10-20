"""Pedidos de EPI (Microsoft Forms): recebimento, identificação dos itens, separação e retirada."""
import csv
import io
import json
import re
import secrets
import sqlite3
import unicodedata

from . import estoque
from .estoque import ErroValidacao, _transacao
from .util import hoje, limpar

# (chave no JSON, rótulo exibido). Cada coluna do formulário vira linhas de item.
COLUNAS_ITENS = [
    ("vestimento", "Vestimento"),
    ("epi", "EPI"),
    ("sapatao", "Sapatão"),
    ("camiseta", "Camiseta"),
    ("calca", "Calça"),
]
LOCAIS_FORMULARIO = [
    "Mafra",
    "Itaiópolis",
    "Garuva",
    "Araquari",
    "Concórdia",
    "Chapecó",
    "Guaramirim",
    "São Francisco do Sul",
    "Barra Velha",
]
MOTIVOS_FORMULARIO = [
    "Desgaste",
    "Perda",
    "Mudança de Função",
]
TAMANHOS_UNIFORME = ["M", "G", "GG", "G1", "G2", "G3", "G4", "G5"]

_TERMOS_CALCADO = ("sapat", "bota", "botina", "calcad")
_TERMOS_CAMISETA = ("camis", "blusa", "polo")
_TERMOS_CALCA = ("calca",)
STATUS = {
    "novo": "Novo",
    "pendente": "Pendente",
    "em_separacao": "Em separação",
    "pronto": "Pronto para retirada",
    "retirado": "Retirado",
    "cancelado": "Cancelado",
}
ABERTOS = ("novo", "pendente", "em_separacao", "pronto")
A_ATENDER = ("novo", "pendente", "em_separacao")

# Respostas que significam "nada pedido"
_NADA = {"nao", "n", "n/a", "na", "-", "--", "nenhum", "nenhuma", "nada", "sem", "x"}
_MAX_CAMPO = 2000


# ------------------------------------------------------------ texto

def normalizar(texto):
    """Minúsculas, sem acentos, espaços colapsados — para comparar nomes."""
    texto = unicodedata.normalize("NFKD", str(texto or ""))
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", texto).strip().casefold()


def tipo_calcado(texto):
    """Devolve 'botina', 'bota', 'sapatao' ou None conforme o nome do calçado."""
    t = normalizar(texto)
    if "jardineira" in t:
        return None
    if "botina" in t:
        return "botina"
    if "bota" in t:
        return "bota"
    if "sapat" in t or "calcad" in t:
        return "sapatao"
    return None


def tipo_uniforme(texto):
    """Devolve 'camiseta', 'calca' ou None conforme o nome da vestimenta."""
    t = normalizar(texto)
    if any(p in t for p in _TERMOS_CAMISETA):
        return "camiseta"
    if any(p in t for p in _TERMOS_CALCA):
        return "calca"
    return None


def _como_texto(valor):
    """Aceita texto, lista, ou o texto-JSON que o Forms usa para múltipla escolha ('["A","B"]')."""
    if valor is None:
        return ""
    if isinstance(valor, (list, tuple)):
        return "; ".join(_como_texto(v) for v in valor)
    texto = str(valor).strip()
    if texto.startswith("[") and texto.endswith("]"):
        try:
            lista = json.loads(texto)
            if isinstance(lista, list):
                return "; ".join(_como_texto(v) for v in lista)
        except ValueError:
            pass
    return texto[:_MAX_CAMPO]


def _extrair_qtd(parte):
    qtd = 1
    m = re.match(r"^(\d{1,3})\s*[xX]\s+(.+)$", parte)
    if m:
        qtd, parte = int(m.group(1)), m.group(2).strip()
    else:
        m = re.match(r"^(.+?)\s+[xX]\s*(\d{1,3})$", parte)
        if m:
            parte, qtd = m.group(1).strip(), int(m.group(2))
    return qtd, parte


def dividir_itens(texto, conhecidos=()):
    """'2x Luva G; Óculos, Bota 40' -> [(2, 'Luva G'), (1, 'Óculos'), (1, 'Bota 40')]"""
    itens = []
    for bloco in re.split(r"[;\n\r|]+", texto or ""):
        bloco = bloco.strip()
        if not bloco:
            continue
        qtd_b, nome_b = _extrair_qtd(bloco)
        if conhecidos and normalizar(nome_b) in conhecidos:
            if qtd_b > 0:
                itens.append((qtd_b, nome_b))
            continue
        # Divide por vírgula apenas fora de parênteses e quando não for número decimal (ex.: 0,6)
        for parte in re.split(r"(?<!\d),(?!\d)(?![^()]*\))", bloco):
            parte = parte.strip()
            if not parte:
                continue
            qtd, nome_p = _extrair_qtd(parte)
            if qtd > 0:
                itens.append((qtd, nome_p))
    return itens


def chave_apelido(rotulo, texto):
    return f"{rotulo}|{normalizar(texto)}"


# ------------------------------------------------------------ integração (API)

def obter_api_key(db, regenerar=False):
    linha = db.execute("SELECT valor FROM configuracoes WHERE chave = 'api_key'").fetchone()
    if linha and not regenerar:
        return linha[0]
    chave = secrets.token_urlsafe(32)
    db.execute("INSERT INTO configuracoes (chave, valor) VALUES ('api_key', ?) "
               "ON CONFLICT (chave) DO UPDATE SET valor = excluded.valor", (chave,))
    return chave


def _obter_campo(campos, *chaves):
    for c in chaves:
        v = campos.get(c)
        if v is not None and _como_texto(v).strip():
            return _como_texto(v)
    return ""


def criar_pedido(db, dados):
    """Cria um pedido a partir do JSON do formulário. Devolve (pedido_id, criado).

    Se 'id' (id da resposta do Forms) já existir, devolve o pedido existente sem duplicar.
    """
    if not isinstance(dados, dict):
        raise ErroValidacao("Corpo da requisição inválido.")
    campos = {normalizar(k).replace(" ", "_"): v for k, v in dados.items()}
    nome = limpar(_obter_campo(campos, "nome", "solicitante", "colaborador", "coluna_h", "h"))
    if not nome:
        raise ErroValidacao("O campo 'nome' é obrigatório.")
    origem_id = (limpar(_obter_campo(campos, "id", "resposta_id", "id_da_resposta", "coluna_a", "a")) or "")[:200] or None
    local = limpar(_obter_campo(campos, "local", "local_destino", "obra", "coluna_i", "i"))
    motivo = limpar(_obter_campo(campos, "motivo", "coluna_j", "j"))
    observacao = limpar(_obter_campo(campos, "observacao", "obersvacao", "observacoes", "obs", "coluna_p", "p"))
    valores_itens = {
        "vestimento": _obter_campo(campos, "vestimento", "vestimenta", "coluna_k", "k"),
        "epi": _obter_campo(campos, "epi", "epis", "coluna_l", "l"),
        "sapatao": _obter_campo(campos, "tam_sapatao", "sapatao", "sapato", "bota", "calcado", "coluna_m", "m"),
        "tam_botina": _obter_campo(campos, "tam_botina", "botina"),
        "tam_bota": _obter_campo(campos, "tam_bota"),
        "camiseta": _obter_campo(campos, "camiseta", "camisa", "coluna_n", "n"),
        "calca": _obter_campo(campos, "calca", "coluna_o", "o"),
    }

    def executar():
        if origem_id:
            existente = db.execute("SELECT id FROM pedidos WHERE origem_id = ?", (origem_id,)).fetchone()
            if existente:
                return existente[0], False

        correspondentes = [r for r in db.execute("SELECT id, nome FROM colaboradores WHERE ativo = 1")
                           if normalizar(r["nome"]) == normalizar(nome)]
        colaborador_id = correspondentes[0]["id"] if len(correspondentes) == 1 else None

        cur = db.execute(
            """INSERT INTO pedidos (origem_id, solicitante, local_destino, motivo, observacao,
                                    colaborador_id, dados_brutos)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (origem_id, nome[:200], local, motivo, observacao,
             colaborador_id, json.dumps(dados, ensure_ascii=False)[:20000]),
        )
        pedido_id = cur.lastrowid

        epis = {normalizar(e["nome"]): e["id"] for e in db.execute("SELECT id, nome FROM epis WHERE ativo = 1")}

        # Verifica se calçado, camiseta ou calça já foram marcados nas colunas de EPI / Vestimento
        itens_vest_epi = []
        for chave, rotulo in (("vestimento", "Vestimento"), ("epi", "EPI")):
            for qtd, texto in dividir_itens(valores_itens.get(chave, ""), conhecidos=epis):
                if normalizar(texto) not in _NADA:
                    itens_vest_epi.append((chave, rotulo, qtd, texto))

        tem_calcado = any(tipo_calcado(txt) is not None for _, _, _, txt in itens_vest_epi)
        tem_camiseta = any(tipo_uniforme(txt) == "camiseta" for _, _, _, txt in itens_vest_epi)
        tem_calca = any(tipo_uniforme(txt) == "calca" for _, _, _, txt in itens_vest_epi)

        def _limpar_tam(chave):
            v = limpar(valores_itens.get(chave, ""))
            return "" if normalizar(v) in _NADA else v

        tams_calcado = {
            "sapatao": _limpar_tam("sapatao"),
            "botina": _limpar_tam("tam_botina") or _limpar_tam("sapatao"),
            "bota": _limpar_tam("tam_bota") or _limpar_tam("sapatao"),
        }
        tam_camiseta = _limpar_tam("camiseta")
        tam_calca = _limpar_tam("calca")

        for chave, rotulo, qtd, texto in itens_vest_epi:
            epi_id, ignorado = _identificar(db, epis, rotulo, texto)
            texto_exibicao = texto
            tc = tipo_calcado(texto)
            tu = tipo_uniforme(texto)
            if tc and tams_calcado.get(tc):
                texto_exibicao = f"{texto} (Tam. {tams_calcado[tc]})"
            elif tu == "camiseta" and tam_camiseta:
                texto_exibicao = f"{texto} (Tam. {tam_camiseta})"
            elif tu == "calca" and tam_calca:
                texto_exibicao = f"{texto} (Tam. {tam_calca})"
            db.execute(
                """INSERT INTO pedido_itens (pedido_id, coluna, texto_original, quantidade, epi_id, ignorado)
                   VALUES (?, ?, ?, ?, ?, ?)""", (pedido_id, rotulo, texto_exibicao[:300], qtd, epi_id, ignorado))

        colunas_extras = []
        if not tem_calcado:
            colunas_extras.append(("sapatao", "Sapatão"))
        if not tem_camiseta:
            colunas_extras.append(("camiseta", "Camiseta"))
        if not tem_calca:
            colunas_extras.append(("calca", "Calça"))

        for chave, rotulo in colunas_extras:
            for qtd, texto in dividir_itens(valores_itens.get(chave, ""), conhecidos=epis):
                if normalizar(texto) in _NADA:
                    continue
                epi_id, ignorado = _identificar(db, epis, rotulo, texto)
                db.execute(
                    """INSERT INTO pedido_itens (pedido_id, coluna, texto_original, quantidade, epi_id, ignorado)
                       VALUES (?, ?, ?, ?, ?, ?)""", (pedido_id, rotulo, texto[:300], qtd, epi_id, ignorado))
        return pedido_id, True

    try:
        return _transacao(db, executar)
    except sqlite3.IntegrityError:
        # duas requisições simultâneas com o mesmo id: a outra venceu
        existente = db.execute("SELECT id FROM pedidos WHERE origem_id = ?", (origem_id,)).fetchone()
        if existente:
            return existente[0], False
        raise


def importar_csv_forms(db, texto_csv):
    """Importa linhas de um CSV exportado da planilha do Microsoft Forms.

    Reconhece tanto pelos nomes dos cabeçalhos quanto pelas posições fixas das colunas
    do Forms (Coluna A = ID, H = Nome, I = Local, J = Motivo, K = Vestimento, L = EPI,
    M = Sapatão, N = Camiseta, O = Calça, P = Observação).
    Devolve (criados, ignorados_duplicados).
    """
    texto_csv = (texto_csv or "").lstrip("\ufeff").strip()
    if not texto_csv:
        raise ErroValidacao("Arquivo vazio.")
    primeira = texto_csv.splitlines()[0]
    delim = ";" if primeira.count(";") >= primeira.count(",") and primeira.count(";") >= primeira.count("\t") else (
        "\t" if primeira.count("\t") > primeira.count(",") else ","
    )
    leitor = list(csv.reader(io.StringIO(texto_csv), delimiter=delim))
    if len(leitor) < 2:
        raise ErroValidacao("A planilha não possui linhas de dados.")
    cab = [normalizar(c) for c in leitor[0]]
    tem_nome_cab = any(c in ("nome", "solicitante", "colaborador") or c.startswith("nome") for c in cab)
    criados = duplicados = 0
    for linha in leitor[1:]:
        if not any(c.strip() for c in linha):
            continue
        dados = {}
        if len(linha) >= 16 and not tem_nome_cab:
            dados = {
                "id": linha[0], "nome": linha[7], "local": linha[8], "motivo": linha[9],
                "vestimento": linha[10], "epi": linha[11], "sapatao": linha[12],
                "camiseta": linha[13], "calca": linha[14], "observacao": linha[15],
            }
        else:
            for idx, valor in enumerate(linha):
                if idx < len(cab) and cab[idx]:
                    dados[cab[idx]] = valor
            if len(linha) >= 16 and not dados.get("nome"):
                dados.update({
                    "id": dados.get("id") or linha[0], "nome": linha[7], "local": linha[8],
                    "motivo": linha[9], "vestimento": linha[10], "epi": linha[11],
                    "sapatao": linha[12], "camiseta": linha[13], "calca": linha[14],
                    "observacao": linha[15],
                })
        if not limpar(_como_texto(dados.get("nome"))):
            continue
        _, novo = criar_pedido(db, dados)
        if novo:
            criados += 1
        else:
            duplicados += 1
    return criados, duplicados


def _identificar(db, epis, rotulo, texto):
    """Devolve (epi_id | None, ignorado) para um texto do formulário."""
    apelido = db.execute("SELECT epi_id FROM epi_apelidos WHERE chave = ?", (chave_apelido(rotulo, texto),)).fetchone()
    if apelido:
        return (apelido["epi_id"], 0) if apelido["epi_id"] else (None, 1)
    for candidato in (texto, f"{rotulo} {texto}", f"{texto} {rotulo}"):
        if normalizar(candidato) in epis:
            return epis[normalizar(candidato)], 0
    return None, 0


# ------------------------------------------------------------ consulta

def carregar(db, status=None, pedido_id=None, limite=None):
    """Pedidos com seus itens (e saldo atual de cada EPI) já prontos para exibir."""
    sql, params = "SELECT p.*, c.nome AS colaborador_nome FROM pedidos p " \
                  "LEFT JOIN colaboradores c ON c.id = p.colaborador_id WHERE 1 = 1", []
    if pedido_id is not None:
        sql += " AND p.id = ?"
        params.append(pedido_id)
    if status:
        sql += f" AND p.status IN ({','.join('?' * len(status))})"
        params += list(status)
    historico = bool(status) and set(status) <= {"retirado", "cancelado"}
    sql += " ORDER BY p.id DESC" if historico else " ORDER BY p.recebido_em, p.id"
    if limite:
        sql += f" LIMIT {int(limite)}"
    pedidos = [dict(p) for p in db.execute(sql, params)]
    if not pedidos:
        return pedidos

    ids = [p["id"] for p in pedidos]
    saldos = {e["id"]: e for e in estoque.epis_com_saldo(db, somente_ativos=False)}
    itens = db.execute(
        f"SELECT * FROM pedido_itens WHERE pedido_id IN ({','.join('?' * len(ids))}) ORDER BY id", ids).fetchall()
    por_pedido = {i: [] for i in ids}
    for it in itens:
        it = dict(it)
        epi = saldos.get(it["epi_id"])
        it["epi_nome"] = epi["nome"] if epi else None
        m_tam = re.search(r"\(Tam\.\s*([^)]+)\)$", it["texto_original"] or "")
        it["tamanho"] = m_tam.group(1).strip() if m_tam else None
        it["unidade"] = epi["unidade"] if epi else ""
        it["saldo"] = epi["saldo"] if epi else None
        it["epi_ativo"] = bool(epi["ativo"]) if epi else None
        por_pedido[it["pedido_id"]].append(it)
    for p in pedidos:
        p["itens"] = por_pedido[p["id"]]
        validos = [i for i in p["itens"] if not i["ignorado"]]
        p["total_itens"] = len(validos)
        p["separados"] = sum(1 for i in validos if i["separado"])
        p["nao_identificados"] = sum(1 for i in validos if i["epi_id"] is None)
        p["pronto_para_finalizar"] = bool(validos) and p["separados"] == len(validos) and p["nao_identificados"] == 0
        # quantidade total pedida por EPI vs saldo (aviso de falta)
        pedido_por_epi = {}
        for i in validos:
            if i["epi_id"]:
                pedido_por_epi[i["epi_id"]] = pedido_por_epi.get(i["epi_id"], 0) + i["quantidade"]
        for i in validos:
            i["falta"] = bool(i["epi_id"]) and i["saldo"] is not None and pedido_por_epi[i["epi_id"]] > i["saldo"]
    return pedidos


def contagens(db):
    return {r["status"]: r["n"] for r in db.execute("SELECT status, COUNT(*) AS n FROM pedidos GROUP BY status")}


# ------------------------------------------------------------ ações

def _pedido(db, pedido_id, *status_validos):
    p = db.execute("SELECT * FROM pedidos WHERE id = ?", (pedido_id,)).fetchone()
    if not p:
        raise ErroValidacao("Pedido não encontrado.")
    if status_validos and p["status"] not in status_validos:
        raise ErroValidacao(f"Esta ação não é possível com o pedido “{STATUS[p['status']]}”.")
    return p


def adiar(db, pedido_id):
    """'Mais tarde': tira o pedido dos novos e o deixa na lista de pendentes."""
    _pedido(db, pedido_id, "novo", "pendente")
    db.execute("UPDATE pedidos SET status = 'pendente' WHERE id = ? AND status = 'novo'", (pedido_id,))


def iniciar(db, pedido_id, usuario_id):
    p = _pedido(db, pedido_id, "novo", "pendente", "em_separacao")
    if p["status"] != "em_separacao":
        db.execute("UPDATE pedidos SET status = 'em_separacao', iniciado_por = ?, "
                   "iniciado_em = datetime('now', 'localtime') WHERE id = ?", (usuario_id, pedido_id))


def _item(db, pedido_id, item_id):
    _pedido(db, pedido_id, "novo", "pendente", "em_separacao")
    it = db.execute("SELECT * FROM pedido_itens WHERE id = ? AND pedido_id = ?", (item_id, pedido_id)).fetchone()
    if not it:
        raise ErroValidacao("Item não encontrado.")
    return it


def marcar_separado(db, pedido_id, item_id, separado):
    it = _item(db, pedido_id, item_id)
    if separado and (it["epi_id"] is None or it["ignorado"]):
        raise ErroValidacao("Identifique o EPI antes de marcar como separado.")
    db.execute("UPDATE pedido_itens SET separado = ? WHERE id = ?", (1 if separado else 0, item_id))


def editar_item(db, pedido_id, item_id, epi_id, quantidade, lembrar):
    it = _item(db, pedido_id, item_id)
    if not isinstance(quantidade, int) or not 0 < quantidade <= 999:
        raise ErroValidacao("Quantidade inválida.")
    if epi_id is not None and not db.execute("SELECT 1 FROM epis WHERE id = ? AND ativo = 1", (epi_id,)).fetchone():
        raise ErroValidacao("EPI inválido ou inativo.")
    # trocar o EPI invalida a separação anterior
    separado = it["separado"] if epi_id == it["epi_id"] and epi_id is not None else 0
    db.execute("UPDATE pedido_itens SET epi_id = ?, quantidade = ?, separado = ?, ignorado = 0 WHERE id = ?",
               (epi_id, quantidade, separado, item_id))
    if lembrar and epi_id is not None and it["coluna"] != "Adicionado":
        _lembrar(db, it, epi_id)


def ignorar_item(db, pedido_id, item_id, ignorar, sempre=False):
    it = _item(db, pedido_id, item_id)
    db.execute("UPDATE pedido_itens SET ignorado = ?, separado = 0 WHERE id = ?", (1 if ignorar else 0, item_id))
    if ignorar and sempre and it["coluna"] != "Adicionado":
        _lembrar(db, it, None)


def adicionar_item(db, pedido_id, epi_id, quantidade):
    _pedido(db, pedido_id, "novo", "pendente", "em_separacao")
    if not isinstance(quantidade, int) or not 0 < quantidade <= 999:
        raise ErroValidacao("Quantidade inválida.")
    epi = db.execute("SELECT nome FROM epis WHERE id = ? AND ativo = 1", (epi_id,)).fetchone()
    if not epi:
        raise ErroValidacao("Selecione um EPI.")
    db.execute("INSERT INTO pedido_itens (pedido_id, coluna, texto_original, quantidade, epi_id) "
               "VALUES (?, 'Adicionado', ?, ?, ?)", (pedido_id, epi["nome"], quantidade, epi_id))


def _lembrar(db, item, epi_id):
    db.execute("INSERT INTO epi_apelidos (chave, epi_id) VALUES (?, ?) "
               "ON CONFLICT (chave) DO UPDATE SET epi_id = excluded.epi_id",
               (chave_apelido(item["coluna"], item["texto_original"]), epi_id))


def vincular_colaborador(db, pedido_id, colaborador_id=None, criar_novo=False):
    p = _pedido(db, pedido_id, "novo", "pendente", "em_separacao")
    if criar_novo:
        cur = db.execute("INSERT INTO colaboradores (nome) VALUES (?)", (p["solicitante"],))
        colaborador_id = cur.lastrowid
    elif not db.execute("SELECT 1 FROM colaboradores WHERE id = ? AND ativo = 1", (colaborador_id,)).fetchone():
        raise ErroValidacao("Selecione um colaborador ativo.")
    db.execute("UPDATE pedidos SET colaborador_id = ? WHERE id = ?", (colaborador_id, pedido_id))


def finalizar(db, pedido_id, usuario_id):
    """Dá baixa no estoque (uma entrega) e deixa o pedido 'pronto para retirada'. Tudo ou nada."""
    def executar():
        p = _pedido(db, pedido_id, "em_separacao")
        if not p["colaborador_id"]:
            raise ErroValidacao("Vincule o pedido a um colaborador cadastrado antes de finalizar.")
        itens = db.execute("SELECT * FROM pedido_itens WHERE pedido_id = ? AND ignorado = 0", (pedido_id,)).fetchall()
        if not itens:
            raise ErroValidacao("O pedido não tem itens.")
        if any(i["epi_id"] is None for i in itens):
            raise ErroValidacao("Há itens sem EPI identificado.")
        if not all(i["separado"] for i in itens):
            raise ErroValidacao("Marque todos os itens como separados.")
        grupo = estoque.registrar_na_transacao(
            db, "saida", [{"epi_id": i["epi_id"], "quantidade": i["quantidade"]} for i in itens], hoje(),
            usuario_id, colaborador_id=p["colaborador_id"], motivo=p["motivo"],
            observacao=f"Pedido #{pedido_id}", pedido_id=pedido_id, local_destino=p["local_destino"])
        db.execute("UPDATE pedidos SET status = 'pronto', grupo = ?, finalizado_por = ?, "
                   "finalizado_em = datetime('now', 'localtime') WHERE id = ?", (grupo, usuario_id, pedido_id))
        return grupo

    return _transacao(db, executar)


def retirar(db, pedido_id, nome, descricao, usuario_id):
    if not nome:
        raise ErroValidacao("Informe quem está retirando.")

    def executar():
        _pedido(db, pedido_id, "pronto")
        db.execute("UPDATE pedidos SET status = 'retirado', retirado_por_nome = ?, retirado_descricao = ?, "
                   "retirado_em = datetime('now', 'localtime'), entregue_por = ? WHERE id = ?",
                   (nome[:200], (descricao or "")[:500] or None, usuario_id, pedido_id))

    _transacao(db, executar)


def cancelar(db, pedido_id, motivo, usuario_id, pode_estornar):
    """Cancela o pedido. Se já deu baixa (pronto), estorna as movimentações — só administrador."""
    if not motivo:
        raise ErroValidacao("Informe o motivo do cancelamento.")

    def executar():
        p = _pedido(db, pedido_id, "novo", "pendente", "em_separacao", "pronto")
        if p["status"] == "pronto":
            if not pode_estornar:
                raise ErroValidacao("Só o administrador pode cancelar um pedido que já deu baixa no estoque.")
            for m in db.execute("SELECT id FROM movimentacoes WHERE pedido_id = ? AND cancelada = 0", (pedido_id,)):
                estoque.cancelar_na_transacao(db, m["id"], f"Pedido #{pedido_id} cancelado: {motivo}", usuario_id)
        db.execute("UPDATE pedidos SET status = 'cancelado', cancelado_motivo = ?, cancelado_por = ?, "
                   "cancelado_em = datetime('now', 'localtime') WHERE id = ?", (motivo, usuario_id, pedido_id))

    _transacao(db, executar)
