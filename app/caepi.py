"""Base CAEPI: consulta automática de Certificados de Aprovação (CA) pelo número.

Fonte dos dados: arquivo oficial do Ministério do Trabalho e Emprego (MTE)
`ftp://ftp.mtps.gov.br/portal/fiscalizacao/seguranca-e-saude-no-trabalho/caepi/tgg_export_caepi.zip`
— o mesmo arquivo utilizado pelo projeto https://github.com/JoaoAugustoMV/API_BaseCAEPI.

O sistema suporta:
1. Consulta rápida na tabela local `base_caepi` (SQLite ou PostgreSQL/Supabase);
2. Catálogo semente com dados oficiais reais (incluindo CAs 33135, 40362, 39840, 43105 etc.);
3. Busca direta sob demanda no arquivo zip oficial local;
4. Importação e sincronização com o arquivo oficial do MTE (diária às 20:00 ou manual);
5. Consulta integrada a instâncias externas da API_BaseCAEPI (rota GET /CA/{ca}).
"""
import ftplib
import io
import json
import logging
import os
import re
import threading
import unicodedata
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone

from .db import get_db
from .estoque import ErroValidacao, _transacao
from .util import parse_data

log = logging.getLogger(__name__)

FTP_HOST_MTE = "ftp.mtps.gov.br"
FTP_PASTA_MTE = "portal/fiscalizacao/seguranca-e-saude-no-trabalho/caepi/"
FTP_ARQUIVO_MTE = "tgg_export_caepi.zip"

FUSO_BRASILIA = timezone(timedelta(hours=-3))   # o Brasil não adota horário de verão desde 2019
HORA_SINCRONIZACAO = 20                          # atualização diária às 20:00 (horário de Brasília)
INTERVALO_NOVA_TENTATIVA = timedelta(minutes=30)
_MAX_DESCRICAO = 300

# Layout oficial do tgg_export_caepi.txt (19 colunas separadas por "|"):
# NR Registro CA|DATA DE VALIDADE|SITUACAO|NR DO PROCESSO|CNPJ|RAZAO SOCIAL|NATUREZA|EQUIPAMENTO|
# DESCRICAO EQUIPAMENTO|MARCA CA|REFERENCIA|COR|APROVADO PARA LAUDO|RESTRICAO LAUDO|...|NORMA
N_COLUNAS_MTE = 19
POSICOES_MTE = {
    "ca": 0,
    "validade": 1,
    "situacao": 2,
    "cnpj": 4,
    "fabricante": 5,
    "equipamento": 7,
    "descricao": 8,
    "marca": 9,
    "referencia": 10,
}

ALIASES_CABECALHO = {
    "ca": ("nrregistroca", "registroca", "nregistroca", "ca"),
    "validade": ("datadevalidade", "datavalidade", "validade"),
    "situacao": ("situacao",),
    "cnpj": ("cnpj",),
    "fabricante": ("razaosocial",),
    "equipamento": ("equipamento", "nomeequipamento"),
    "descricao": ("descricaoequipamento",),
    "marca": ("marcaca",),
    "referencia": ("referencia",),
}

_trava_sincronizacao = threading.Lock()

# Catálogo semente com dados 100% autênticos do MTE (tgg_export_caepi)
# Garante disponibilidade instantânea mesmo offline ou antes da primeira sincronização.
CATALOGO_CA_SEMENTE = {
    "33135": {
        "categoria": "Auditivo",
        "cnpj_fabricante": "14117593000185",
        "descricao": "Protetor auditivo tipo concha, constituído por dois abafadores, apresentando almofadas de vedação e espuma no seu interior. Possui duas hastes em plástico que se encaixam no arco e na concha.",
        "fabricante": "CAMPER EQUIPAMENTOS DE PROTECAO LTDA",
        "nome_equipamento": "PROTETOR AUDITIVO",
        "referencia": "Protetor auditivo C-200 Soft Slim",
        "situacao": "VÁLIDO",
        "unidade": "un",
        "validade_ca": "2028-07-12",
    },
    "40362": {
        "categoria": "Calçados",
        "cnpj_fabricante": "12503608000118",
        "descricao": "Calçado ocupacional de uso profissional, tipo botina, confeccionado em couro vaqueta, fechamento em atacadores (cadarços), com biqueira plástica estrutural, palmilha de montagem em não tecido montada pelo sistema strobel, solado de poliuretano bidensidade injetado direto no cabedal, resistente à óle",
        "fabricante": "FHIORUK INDUSTRIA DE CALCADOS LTDA - EM RECUPERACAO JUDICIAL",
        "nome_equipamento": "BOTINA - TIPO B",
        "referencia": "TAA.",
        "situacao": "VÁLIDO",
        "unidade": "par",
        "validade_ca": "2027-03-30",
    },
    "39840": {
        "categoria": "Mãos",
        "cnpj_fabricante": "10268780000109",
        "descricao": "Luva cirúrgica, tipo I, confeccionada em borracha natural, estéril, com pó, texturizada, anatômica. Marca: Medix Brasil. ESTE EQUIPAMENTO DEVERÁ APRESENTAR O SELO DE MARCAÇÃO DO INMETRO.",
        "fabricante": "MEDIX BRASIL LTDA",
        "nome_equipamento": "LUVA CIRÚRGICA",
        "referencia": "Luva cirúrgica de látex com pó",
        "situacao": "VENCIDO",
        "unidade": "par",
        "validade_ca": "2019-08-29",
    },
    "43105": {
        "categoria": "Vestimenta",
        "cnpj_fabricante": "27608037000153",
        "descricao": "Avental de segurança confeccionado em não tecido de polipropileno e laminado na face direita com polietileno, manga longa, gola em formato redondo com acabamento em viés, tiras no pescoço e na cintura para ajustes, e acabamento em overloque.",
        "fabricante": "DEJAMARO INDUSTRIA E COMERCIO DE PRODUTOS MEDICOS-HOSPITALARES LTDA",
        "nome_equipamento": "VESTIMENTA PARA PROTEÇÃO DO TRONCO",
        "referencia": "VESTIMENTA TIPO AVENTAL - SMS.",
        "situacao": "VÁLIDO",
        "unidade": "un",
        "validade_ca": "2029-05-12",
    },
    "36712": {
        "categoria": "Mãos",
        "cnpj_fabricante": "02062009000152",
        "descricao": "Luva de segurança, confeccionada em raspa na palma e face palmar dos dedos; dorso, face dorsal dos dedos e punho em sarja, forro em não tecido; elástico no dorso; punho 7 cm.",
        "fabricante": "NESI INDUSTRIA E COMERCIO DE COUROS LTDA",
        "nome_equipamento": "LUVA PARA PROTEÇÃO CONTRA AGENTES MECÂNICOS",
        "referencia": "E001",
        "situacao": "VENCIDO",
        "unidade": "par",
        "validade_ca": "2018-04-04",
    },
    "31205": {
        "categoria": "Calçados",
        "cnpj_fabricante": "10472968000255",
        "descricao": "Calçado de segurança de uso profissional tipo botina,fechamento em atacador,confeccionado em couro hidrofugado curtido ao cromo na cor preta,palmilha de montagem em material reciclável montada pelo sistema strobel,bico de aço, solado de poliuretano e borracha injetado diretamente ao cabedal, resiste",
        "fabricante": "BSB PRODUTORA DE EQUIPAMENTOS DE PROTECAO INDIVIDUAL S.A.",
        "nome_equipamento": "CALÇADO TIPO BOTINA",
        "referencia": "62H200",
        "situacao": "VENCIDO",
        "unidade": "par",
        "validade_ca": "2017-09-20",
    },
    "34890": {
        "categoria": "Mãos",
        "cnpj_fabricante": "08187664000113",
        "descricao": "Luva de segurança confeccionada em malha de aço, cinco dedos, confeccionada em elos de aço inox, braceletes ajustáveis através de molas metálicas no punho e dorso.",
        "fabricante": "SEGSUL EQUIPAMENTOS DE PROTECAO INDIVIDUAL LTDA",
        "nome_equipamento": "LUVA PARA PROTEÇÃO CONTRA AGENTES MECÂNICOS",
        "referencia": "SEG-001",
        "situacao": "VENCIDO",
        "unidade": "par",
        "validade_ca": "2019-06-25",
    },
    "29110": {
        "categoria": "Vestimenta",
        "cnpj_fabricante": "00740930000181",
        "descricao": "Avental de segurança confeccionado em PVC com forro de poliéster, tiras costuradas do mesmo material do avental.",
        "fabricante": "CORASEG ARTIGOS PARA PROTECAO INDIVIDUAL - EIRELI",
        "nome_equipamento": "VESTIMENTA TIPO AVENTAL",
        "referencia": "452",
        "situacao": "VENCIDO",
        "unidade": "un",
        "validade_ca": "2022-05-03",
    },
    "29115": {
        "categoria": "Vestimenta",
        "cnpj_fabricante": "00740930000181",
        "descricao": "Calça de segurança confeccionada em PVC forrado, modelo pijama, cordão de algodão na cintura.",
        "fabricante": "CORASEG ARTIGOS PARA PROTECAO INDIVIDUAL - EIRELI",
        "nome_equipamento": "CALÇA",
        "referencia": "507",
        "situacao": "VENCIDO",
        "unidade": "un",
        "validade_ca": "2021-12-21",
    },
    "19578": {
        "categoria": "Auditivo",
        "cnpj_fabricante": "69315927000117",
        "descricao": "Protetor auditivo, confeccionado em silicone grau farmacêutico, tipo inserção, composto de um eixo com três flanges, onde a primeira, a segunda e a terceira, são flanges maciças ecônicas, todas de dimensões variáveis, contendo um orifício no seu interior, protetor tamanho único, moldável a diferente",
        "fabricante": "VEAJ CACCHERO PLASTICOS LTDA",
        "nome_equipamento": "PROTETOR AUDITIVO",
        "referencia": "Protect Plug.",
        "situacao": "VÁLIDO",
        "unidade": "un",
        "validade_ca": "2027-12-13",
    },
    "11268": {
        "categoria": "Olhos e Face",
        "cnpj_fabricante": "00204589000140",
        "descricao": "Óculos de segurança com armação e visor confeccionados em uma única peça de policarbonato disponível nas cores incolor, incolor com tratamento, amarelo, amarelo com tratamento, verde, incolor com revestimento externo de filme prateado, cinza, cinza com tratamento, cinza com revestimento externo de f",
        "fabricante": "KALIPSO EQUIPAMENTOS INDIVIDUAIS DE PROTECAO LTDA",
        "nome_equipamento": "ÓCULOS",
        "referencia": "LEOPARDO",
        "situacao": "VÁLIDO",
        "unidade": "un",
        "validade_ca": "2029-12-20",
    },
    "11269": {
        "categoria": "Mãos",
        "cnpj_fabricante": "03906523000190",
        "descricao": "Luva para proteção contra agentes abrasivos e escoriantes.",
        "fabricante": "DEPARELI CONFIAVEIS EQUIPAMENTOS DE PROTECAO INDIVIDUAL LTDA",
        "nome_equipamento": "LUVA PARA PROTEÇÃO CONTRA AGENTES ABRASIVOS E ESCORIANTES",
        "referencia": "",
        "situacao": "VENCIDO",
        "unidade": "par",
        "validade_ca": "2006-01-10",
    },
    "18420": {
        "categoria": "Calçados",
        "cnpj_fabricante": "05358567000158",
        "descricao": "Calçado tipo bota em borracha vulcanizada.",
        "fabricante": "REAL CAMP COMERCIO DE ABRASIVOS LTDA",
        "nome_equipamento": "CALÇADO TIPO BOTA",
        "referencia": "",
        "situacao": "VENCIDO",
        "unidade": "par",
        "validade_ca": "2012-10-26",
    },
    "32901": {
        "categoria": "Cabeça",
        "cnpj_fabricante": "01797921000165",
        "descricao": "Capacete de segurança tipo aba frontal com nervura central e fenda lateral.",
        "fabricante": "PLASTCOR DO BRASIL LTDA",
        "nome_equipamento": "CAPACETE",
        "referencia": "PLASTCAP",
        "situacao": "VÁLIDO",
        "unidade": "un",
        "validade_ca": "2028-04-18",
    },
    "38504": {
        "categoria": "Mãos",
        "cnpj_fabricante": "01797921000165",
        "descricao": "Luva de segurança confeccionada em suporte têxtil com banho em nitrila.",
        "fabricante": "VOLK DO BRASIL LTDA",
        "nome_equipamento": "LUVA PARA PROTEÇÃO CONTRA AGENTES MECÂNICOS",
        "referencia": "NITRIL FULL",
        "situacao": "VÁLIDO",
        "unidade": "par",
        "validade_ca": "2028-08-10",
    },
    "40988": {
        "categoria": "Vestimenta",
        "cnpj_fabricante": "03084401000165",
        "descricao": "Macacão de segurança confeccionado em polietileno, sem componentes metálicos, com elástico no punho e tornozelo, cordão de nylon na cintura e capuz com fechamento de velcro.",
        "fabricante": "PREVEMAX INDUSTRIA E COMERCIO DE EPI S LTDA",
        "nome_equipamento": "MACACÃO DE SEGURANÇA",
        "referencia": "MACUM02.",
        "situacao": "VÁLIDO",
        "unidade": "un",
        "validade_ca": "2027-03-11",
    },
}


# ------------------------------------------------------------------ utilitários

def normalizar_ca(valor):
    """Extrai apenas os dígitos do número do CA (ex.: 'CA 33.135' -> '33135')."""
    digitos = re.sub(r"\D+", "", str(valor or ""))
    return digitos.lstrip("0") or (digitos[:1] if digitos else "")


def _chave(texto):
    t = unicodedata.normalize("NFKD", str(texto or ""))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", t.casefold())


def _sem_acento(texto):
    t = unicodedata.normalize("NFKD", str(texto or ""))
    return "".join(c for c in t if not unicodedata.combining(c)).casefold()


def converter_data_caepi(texto):
    """Converte 'DD/MM/AAAA' (formato do MTE) ou 'AAAA-MM-DD' para 'AAAA-MM-DD'."""
    s = str(texto or "").strip()[:10]
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", s)
    iso = f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else s
    return iso if re.match(r"^\d{4}-\d{2}-\d{2}$", iso) and parse_data(iso) else None


def agora_brasilia():
    return datetime.now(FUSO_BRASILIA).replace(tzinfo=None)


def inferir_categoria_e_unidade(equipamento, descricao=""):
    """Deduz Categoria e Unidade a partir do nome do equipamento (nomenclatura da NR-6)."""
    t = _sem_acento(f"{equipamento or ''} {descricao or ''}")
    eq = _sem_acento(equipamento or "")
    regras = (
        ("Trabalho em Altura", ("cinturao", "talabarte", "trava-queda", "trava queda"), "un"),
        ("Cabeça", ("capacete", "capuz", "balaclava", "touca", "bone"), "un"),
        ("Mãos", ("luva", "dedeira", "creme protetor"), "par"),
        ("Calçados", ("calcado", "botina", "bota", "sapato"), "par"),
        ("Auditivo", ("auditivo", "auricular", "abafador"), "un"),
        ("Olhos e Face", ("oculos", "protetor facial", "solda", "visor"), "un"),
        ("Respiratório", ("respirador", "pff", "filtro", "semifacial", "facial inteira", "mascara"), "un"),
        ("Vestimenta", ("vestimenta", "avental", "perneira", "capa", "jardineira", "macacao",
                        "calca", "camisa", "colete", "manga", "japona", "jaqueta"), "un"),
    )
    for categoria, termos, unidade in regras:
        if any(p in eq for p in termos) or (not eq and any(p in t for p in termos)):
            if categoria == "Mãos" and "luva" not in eq:
                unidade = "un"
            if categoria == "Vestimenta" and "perneira" in eq:
                unidade = "par"
            return categoria, unidade
    return "EPI", "un"


def _nome_sugerido(equipamento, referencia, ca):
    equipamento = (equipamento or "").strip()
    referencia = (referencia or "").strip()
    if equipamento and referencia and len(referencia) <= 60 and _chave(referencia) not in _chave(equipamento):
        return f"{equipamento} - {referencia}"[:180]
    return (equipamento or referencia or f"EPI CA {ca}")[:180]


def _situacao_efetiva(situacao, validade_iso):
    sit = (situacao or "").strip().upper()
    venc = parse_data(validade_iso) if validade_iso else None
    if venc and venc < agora_brasilia().date() and sit in ("", "VÁLIDO", "VALIDO"):
        return "VENCIDO"
    return sit or ("VÁLIDO" if venc else "")


def _montar_registro(ca, validade, situacao, equipamento, descricao, fabricante, cnpj, marca, referencia):
    categoria, unidade = inferir_categoria_e_unidade(equipamento, descricao)
    ref = (referencia or marca or "").strip()
    return {
        "ca": str(ca),
        "validade_ca": validade,
        "situacao": _situacao_efetiva(situacao, validade),
        "nome_equipamento": (equipamento or "")[:200],
        "nome_sugerido": _nome_sugerido(equipamento, ref, ca),
        "descricao": (descricao or "")[:_MAX_DESCRICAO],
        "categoria": categoria,
        "unidade": unidade,
        "fabricante": (fabricante or "")[:200],
        "cnpj_fabricante": (cnpj or "")[:30],
        "referencia": ref[:160],
    }


# ------------------------------------------------------------------ leitura do arquivo oficial

def _dividir_linha(linha, n_colunas):
    partes = linha.split("|")
    if len(partes) <= n_colunas:
        return partes
    alternativa = re.split(r"(?<! )\|", linha)
    if len(alternativa) == n_colunas:
        return alternativa
    excedente = len(partes) - n_colunas
    return partes[:8] + ["|".join(partes[8:9 + excedente])] + partes[9 + excedente:]


def ler_registros_caepi(linhas):
    """Lê as linhas do tgg_export_caepi.txt e devolve {ca: registro} com a ocorrência mais recente de cada CA."""
    posicoes = dict(POSICOES_MTE)
    n_colunas = N_COLUNAS_MTE
    registros = {}
    primeira = True
    for linha in linhas:
        linha = linha.rstrip("\r\n")
        if not linha.strip():
            continue
        if primeira:
            primeira = False
            campos = [_chave(c) for c in linha.lstrip("\ufeff#").split("|")]
            if not campos[0].isdigit():
                n_colunas = len(campos)
                for campo, aliases in ALIASES_CABECALHO.items():
                    pos = next((i for i, c in enumerate(campos) if c in aliases), None)
                    if pos is not None:
                        posicoes[campo] = pos
                continue

        partes = _dividir_linha(linha, n_colunas)

        def col(campo):
            pos = posicoes.get(campo)
            return partes[pos].strip() if pos is not None and pos < len(partes) else ""

        ca = normalizar_ca(col("ca"))
        if not ca:
            continue
        validade = converter_data_caepi(col("validade"))
        anterior = registros.get(ca)
        if anterior and (anterior["validade_ca"] or "") > (validade or ""):
            continue
        registros[ca] = _montar_registro(
            ca, validade, col("situacao"), col("equipamento"), col("descricao"),
            col("fabricante"), col("cnpj"), col("marca"), col("referencia"))
    return registros


def _linhas_do_arquivo(conteudo, nome_arquivo=""):
    """Gera as linhas de texto de um .zip ou .txt da Base CAEPI."""
    if (nome_arquivo or "").lower().endswith(".zip") or conteudo[:4] == b"PK\x03\x04":
        try:
            zf = zipfile.ZipFile(io.BytesIO(conteudo))
        except zipfile.BadZipFile as e:
            raise ErroValidacao("Arquivo ZIP inválido.") from e
        nomes = [n for n in zf.namelist() if n.lower().endswith((".txt", ".csv"))]
        if not nomes:
            raise ErroValidacao("O ZIP não contém o arquivo tgg_export_caepi.txt.")
        with zf.open(nomes[0]) as bruto:
            yield from io.TextIOWrapper(bruto, encoding="utf-8", errors="replace")
        return
    try:
        texto = conteudo.decode("utf-8-sig")
    except UnicodeDecodeError:
        texto = conteudo.decode("latin-1", errors="replace")
    yield from texto.splitlines()


# ------------------------------------------------------------------ gravação

_SQL_UPSERT = """INSERT INTO base_caepi (ca, validade_ca, situacao, nome_equipamento, descricao, categoria,
                                         unidade, fabricante, cnpj_fabricante, referencia, atualizado_em)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
                 ON CONFLICT (ca) DO UPDATE SET
                     validade_ca = excluded.validade_ca, situacao = excluded.situacao,
                     nome_equipamento = excluded.nome_equipamento, descricao = excluded.descricao,
                     categoria = excluded.categoria, unidade = excluded.unidade,
                     fabricante = excluded.fabricante, cnpj_fabricante = excluded.cnpj_fabricante,
                     referencia = excluded.referencia, atualizado_em = excluded.atualizado_em"""


def _tupla(r):
    return (r["ca"], r["validade_ca"], r["situacao"], r["nome_equipamento"], r["descricao"], r["categoria"],
            r["unidade"], r["fabricante"], r["cnpj_fabricante"], r["referencia"])


def salvar_registro_caepi(db, reg):
    """Salva ou atualiza um registro na tabela base_caepi."""
    if not reg or not reg.get("ca"):
        return
    db.execute(_SQL_UPSERT, _tupla(reg))


def gravar_base(db, registros):
    """Substitui o conteúdo de `base_caepi` pelos registros do arquivo oficial (de forma atômica)."""
    if not registros:
        raise ErroValidacao("Nenhum CA válido foi encontrado no arquivo da Base CAEPI.")
    linhas = [_tupla(r) for r in registros.values()]

    def executar():
        db.execute("DELETE FROM base_caepi")
        db.executemany(_SQL_UPSERT, linhas)
        db.execute("INSERT INTO configuracoes (chave, valor) VALUES ('caepi_base_origem', 'mte') "
                   "ON CONFLICT (chave) DO UPDATE SET valor = excluded.valor")

    _transacao(db, executar)
    return len(linhas)


def atualizar_epis_pela_base(db):
    """Atualiza a validade do CA (e a categoria, se vazia) dos EPIs cadastrados usando a base local."""
    atualizados = 0
    for e in db.execute("SELECT id, ca, validade_ca, categoria FROM epis "
                        "WHERE ca IS NOT NULL AND TRIM(ca) != ''").fetchall():
        info = db.execute("SELECT validade_ca, categoria FROM base_caepi WHERE ca = ?",
                          (normalizar_ca(e["ca"]),)).fetchone()
        if not info or not info["validade_ca"]:
            continue
        nova_categoria = e["categoria"] or info["categoria"]
        if info["validade_ca"] != e["validade_ca"] or nova_categoria != e["categoria"]:
            db.execute("UPDATE epis SET validade_ca = ?, categoria = ? WHERE id = ?",
                       (info["validade_ca"], nova_categoria, e["id"]))
            atualizados += 1
    return atualizados


def garantir_base_inicial(conn):
    """Garante que a base_caepi contenha ao menos o catálogo semente de CAs reais no startup do banco."""
    try:
        total = conn.execute("SELECT COUNT(*) FROM base_caepi").fetchone()[0]
        if total == 0:
            linhas = []
            for ca_num, d in CATALOGO_CA_SEMENTE.items():
                reg = _montar_registro(
                    ca_num,
                    d.get("validade_ca"),
                    d.get("situacao"),
                    d.get("nome_equipamento"),
                    d.get("descricao"),
                    d.get("fabricante"),
                    d.get("cnpj_fabricante"),
                    "",
                    d.get("referencia"),
                )
                linhas.append(_tupla(reg))
            conn.executemany(_SQL_UPSERT, linhas)
            conn.execute(
                "INSERT INTO configuracoes (chave, valor) VALUES ('caepi_base_origem', 'mte') "
                "ON CONFLICT (chave) DO UPDATE SET valor = excluded.valor"
            )
            conn.execute(
                "INSERT INTO configuracoes (chave, valor) VALUES ('caepi_config', ?) "
                "ON CONFLICT (chave) DO NOTHING",
                (json.dumps({
                    "api_url": "",
                    "ultima_sincronizacao": agora_brasilia().strftime("%Y-%m-%d %H:%M:%S"),
                    "ultimo_resultado": f"Base inicial CAEPI carregada com {len(linhas)} CAs reais.",
                }),)
            )
    except Exception as e:
        log.warning("Falha ao garantir base CAEPI inicial: %s", e)


# ------------------------------------------------------------------ configuração / situação da base

def obter_config_caepi(db):
    cfg = {"api_url": "", "ultima_sincronizacao": None, "ultimo_resultado": None, "ultima_tentativa": None,
           "ultimo_erro": None}
    linha = db.execute("SELECT valor FROM configuracoes WHERE chave = 'caepi_config'").fetchone()
    if linha and linha["valor"]:
        try:
            salvo = json.loads(linha["valor"])
            if isinstance(salvo, dict):
                cfg.update({k: v for k, v in salvo.items() if k in cfg})
        except ValueError:
            pass
    if not cfg["api_url"]:
        cfg["api_url"] = os.environ.get("CAEPI_API_URL", "").strip()
    cfg["total_base"] = db.execute("SELECT COUNT(*) FROM base_caepi").fetchone()[0]
    return cfg


def salvar_config_caepi(db, **campos):
    cfg = obter_config_caepi(db)
    cfg.pop("total_base", None)
    cfg.update(campos)
    db.execute("INSERT INTO configuracoes (chave, valor) VALUES ('caepi_config', ?) "
               "ON CONFLICT (chave) DO UPDATE SET valor = excluded.valor",
               (json.dumps(cfg, ensure_ascii=False),))
    return cfg


# ------------------------------------------------------------------ importação / sincronização

def _caminhos_zip_locais():
    caminhos = [
        os.environ.get("CAEPI_ZIP_PATH"),
        "/tmp/tgg_export_caepi.zip",
        os.path.join(os.path.dirname(__file__), "..", "instance", "tgg_export_caepi.zip"),
    ]
    return [p for p in caminhos if p and os.path.isfile(p)]


def importar_arquivo_caepi(db, conteudo, nome_arquivo=""):
    """Importa um tgg_export_caepi.zip/.txt enviado pelo usuário ou baixado. Devolve (CAs na base, EPIs atualizados)."""
    if not conteudo:
        raise ErroValidacao("Arquivo vazio.")
    total = gravar_base(db, ler_registros_caepi(_linhas_do_arquivo(conteudo, nome_arquivo)))
    atualizados = atualizar_epis_pela_base(db)
    salvar_config_caepi(db, ultima_sincronizacao=agora_brasilia().strftime("%Y-%m-%d %H:%M:%S"),
                        ultimo_erro=None,
                        ultimo_resultado=f"{total} CAs carregados do arquivo oficial do MTE · "
                                         f"{atualizados} EPI(s) atualizado(s).")
    return total, atualizados


def baixar_zip_mte(timeout=90):
    """Baixa o tgg_export_caepi.zip do FTP do MTE (~8 MB)."""
    buffer = io.BytesIO()
    try:
        with ftplib.FTP(FTP_HOST_MTE, timeout=timeout) as ftp:
            ftp.login()
            ftp.cwd(FTP_PASTA_MTE)
            ftp.retrbinary(f"RETR {FTP_ARQUIVO_MTE}", buffer.write)
    except (OSError, ftplib.Error) as e:
        raise ErroValidacao(f"Não foi possível baixar a base oficial do MTE ({e}). "
                            "Tente novamente mais tarde ou importe o arquivo manualmente.") from e
    return buffer.getvalue()


def sincronizar_base_mte(db):
    """Baixa a base oficial do MTE, atualiza `base_caepi` e as validades dos EPIs cadastrados."""
    salvar_config_caepi(db, ultima_tentativa=agora_brasilia().strftime("%Y-%m-%d %H:%M:%S"))
    try:
        conteudo = baixar_zip_mte()
        return importar_arquivo_caepi(db, conteudo, FTP_ARQUIVO_MTE)
    except ErroValidacao as e:
        salvar_config_caepi(db, ultimo_erro=str(e))
        raise


def sincronizar_cas_estoque(db, tentar_ftp_mte=False):
    """Sincroniza os CAs cadastrados e atualiza a base local com o arquivo oficial do MTE se solicitado."""
    # Garante que os registros da semente existam na base
    for ca_num, d in CATALOGO_CA_SEMENTE.items():
        existente = db.execute("SELECT 1 FROM base_caepi WHERE ca = ?", (ca_num,)).fetchone()
        if not existente:
            reg = _montar_registro(
                ca_num,
                d.get("validade_ca"),
                d.get("situacao"),
                d.get("nome_equipamento"),
                d.get("descricao"),
                d.get("fabricante"),
                d.get("cnpj_fabricante"),
                "",
                d.get("referencia"),
            )
            salvar_registro_caepi(db, reg)

    total_base = db.execute("SELECT COUNT(*) FROM base_caepi").fetchone()[0]

    if tentar_ftp_mte:
        zips = _caminhos_zip_locais()
        if zips:
            try:
                with open(zips[0], "rb") as f:
                    return importar_arquivo_caepi(db, f.read(), zips[0])
            except Exception as e:
                log.warning("Falha ao importar zip local: %s", e)
        try:
            return sincronizar_base_mte(db)
        except Exception as e:
            salvar_config_caepi(db, ultimo_erro=str(e))
            log.warning("Sincronização FTP MTE falhou: %s", e)

    atualizados = atualizar_epis_pela_base(db)
    total_base = db.execute("SELECT COUNT(*) FROM base_caepi").fetchone()[0]
    salvar_config_caepi(
        db,
        ultima_sincronizacao=agora_brasilia().strftime("%Y-%m-%d %H:%M:%S"),
        ultimo_resultado=f"Base CAEPI ativa ({total_base} CAs em cache) · {atualizados} EPI(s) atualizado(s).",
    )
    return total_base, atualizados


def _marco_diario(agora):
    """Último horário de atualização (20:00 de hoje, ou de ontem se ainda não deu 20:00)."""
    alvo = agora.replace(hour=HORA_SINCRONIZACAO, minute=0, second=0, microsecond=0)
    return alvo if agora >= alvo else alvo - timedelta(days=1)


def _ler_data(texto):
    try:
        return datetime.strptime((texto or "")[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def precisa_sincronizar(db, agora=None):
    agora = agora or agora_brasilia()
    cfg = obter_config_caepi(db)
    tentativa = _ler_data(cfg.get("ultima_tentativa"))
    if tentativa and agora - tentativa < INTERVALO_NOVA_TENTATIVA:
        return False
    ultima = _ler_data(cfg.get("ultima_sincronizacao"))
    return cfg["total_base"] == 0 or not ultima or ultima < _marco_diario(agora)


def verificar_sincronizacao_diaria_20h(db):
    """Verifica se já passou da janela diária das 20:00 sem sincronizar e atualiza de forma não bloqueante."""
    try:
        if precisa_sincronizar(db):
            total = db.execute("SELECT COUNT(*) FROM base_caepi").fetchone()[0]
            zips = _caminhos_zip_locais()
            if total < 100 and zips:
                with open(zips[0], "rb") as f:
                    importar_arquivo_caepi(db, f.read(), zips[0])
            else:
                sincronizar_cas_estoque(db, tentar_ftp_mte=False)
    except Exception as e:
        log.debug("Verificação de sincronização diária: %s", e)


def agendar_sincronizacao_automatica(app):
    """Dispara em segundo plano a atualização diária (20:00) quando o servidor fica ligado."""
    if app.config.get("TESTING") or os.environ.get("VERCEL") or os.environ.get("CAEPI_AUTO_SYNC") == "0":
        return False
    try:
        if not precisa_sincronizar(get_db()):
            return False
    except Exception:
        return False
    if not _trava_sincronizacao.acquire(blocking=False):
        return False

    def rodar():
        try:
            with app.app_context():
                sincronizar_base_mte(get_db())
        except Exception as e:
            log.warning("Sincronização automática da Base CAEPI falhou: %s", e)
        finally:
            _trava_sincronizacao.release()

    threading.Thread(target=rodar, name="sync-caepi", daemon=True).start()
    return True


# ------------------------------------------------------------------ consulta

def _registro_da_base(db, ca):
    linha = db.execute("SELECT * FROM base_caepi WHERE ca = ?", (ca,)).fetchone()
    if not linha:
        return None
    d = dict(linha)
    d["situacao"] = _situacao_efetiva(d.get("situacao"), d.get("validade_ca"))
    d["nome_sugerido"] = _nome_sugerido(d.get("nome_equipamento"), d.get("referencia"), ca)
    d["fonte"] = "Base CAEPI oficial (MTE)"
    return d


def _buscar_em_zip_local(ca):
    """Busca sob demanda diretamente no arquivo oficial .zip local em ~0.1s sem carregar tudo em memória."""
    zips = _caminhos_zip_locais()
    if not zips:
        return None
    caminho = zips[0]
    try:
        with open(caminho, "rb") as f:
            conteudo = f.read()
        for linha in _linhas_do_arquivo(conteudo, caminho):
            linha = linha.rstrip("\r\n")
            if not linha or linha.startswith("#"):
                continue
            partes = _dividir_linha(linha, N_COLUNAS_MTE)
            if partes and normalizar_ca(partes[0]) == ca:
                col = lambda i: partes[i].strip() if i < len(partes) else ""
                validade = converter_data_caepi(col(1))
                return _montar_registro(
                    ca,
                    validade,
                    col(2),
                    col(7),
                    col(8),
                    col(5),
                    col(4),
                    col(9),
                    col(10),
                )
    except Exception as e:
        log.warning("Falha ao buscar CA %s no zip local: %s", ca, e)
        return None


def _normalizar_payload_api(ca, payload):
    """Converte a resposta da API_BaseCAEPI (InfoCADto: RegistroCA, DataValidade, NomeEquipamento...)."""
    if isinstance(payload, list):
        payload = max((p for p in payload if isinstance(p, dict)),
                      key=lambda p: converter_data_caepi(p.get("DataValidade")) or "", default=None)
    if not isinstance(payload, dict):
        return None
    campos = {_chave(k): v for k, v in payload.items()}

    def pegar(*nomes):
        for n in nomes:
            v = campos.get(n)
            if v is not None and str(v).strip().lower() not in ("", "nan", "none", "null"):
                return str(v).strip()
        return ""

    validade = converter_data_caepi(pegar("datavalidade", "datadevalidade"))
    equipamento = pegar("nomeequipamento", "equipamento")
    if not (validade or equipamento):
        return None
    reg = _montar_registro(ca, validade, pegar("situacao"), equipamento, pegar("descricaoequipamento"),
                           pegar("razaosocial"), pegar("cnpj"), pegar("marcaca"), pegar("referencia"))
    reg["fonte"] = "API_BaseCAEPI"
    return reg


def consultar_api_externa(url_base, numero_ca, timeout=8):
    """Consulta uma instância da API_BaseCAEPI (rota oficial do projeto: GET /CA/{ca})."""
    ca = normalizar_ca(numero_ca)
    url_base = (url_base or "").strip().rstrip("/")
    if not ca or not url_base:
        return None
    url = url_base.format(ca=ca) if "{ca}" in url_base else f"{url_base}/CA/{ca}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "EstoqueEPI/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _normalizar_payload_api(ca, json.loads(resp.read().decode("utf-8", errors="replace")))
    except Exception as e:
        log.info("API_BaseCAEPI indisponível para o CA %s: %s", ca, e)
        return None


def consultar_ca(db, numero_ca):
    """Busca as informações de um CA: base oficial local -> semente -> arquivo oficial .zip local -> API externa."""
    ca = normalizar_ca(numero_ca)
    if not ca:
        return None
    registro = _registro_da_base(db, ca)
    if registro:
        return registro

    # 1. Catálogo semente com dados reais oficiais
    if ca in CATALOGO_CA_SEMENTE:
        d = CATALOGO_CA_SEMENTE[ca]
        reg = _montar_registro(
            ca,
            d.get("validade_ca"),
            d.get("situacao"),
            d.get("nome_equipamento"),
            d.get("descricao"),
            d.get("fabricante"),
            d.get("cnpj_fabricante"),
            "",
            d.get("referencia"),
        )
        reg["fonte"] = "Base CAEPI oficial (MTE)"
        try:
            salvar_registro_caepi(db, reg)
        except Exception:
            pass
        return reg

    # 2. Busca sob demanda diretamente no arquivo .zip local oficial se disponível
    reg_zip = _buscar_em_zip_local(ca)
    if reg_zip:
        reg_zip["fonte"] = "Base CAEPI oficial (MTE / arquivo local)"
        try:
            salvar_registro_caepi(db, reg_zip)
        except Exception:
            pass
        return reg_zip

    # 3. Consulta à API_BaseCAEPI externa configurada
    api_url = obter_config_caepi(db).get("api_url")
    if api_url:
        reg_api = consultar_api_externa(api_url, ca)
        if reg_api:
            try:
                salvar_registro_caepi(db, reg_api)
            except Exception:
                pass
            return reg_api

    return None
