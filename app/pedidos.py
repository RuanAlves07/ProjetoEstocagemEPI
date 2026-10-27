"""Pedidos de EPI (Microsoft Forms): recebimento, identificação dos itens, separação e retirada."""
import base64
import csv
import io
import json
import re
import secrets
import sqlite3
import unicodedata
from datetime import datetime, timedelta

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
# Obras atendidas (local de entrega do formulário), agrupadas por cidade
OBRAS_FORMULARIO = [
    {"cidade": "Araquari", "nome": "Refúgio dos Pássaros / Guimbala",
     "endereco": "Rod. Senador Luiz Henrique da Silveira - Colégio Agrícola, Araquari - SC, 89245-000",
     "mapa": "https://maps.app.goo.gl/fFNa3S9bjhw4JFPd9"},
    {"cidade": "Araquari", "nome": "Kasulke",
     "endereco": "Rua Adolar Kasulke, Araquari - SC",
     "mapa": "https://maps.app.goo.gl/2WTfj5JJHEq4zNhKA"},
    {"cidade": "Araquari", "nome": "Raízes / Ziel",
     "endereco": "Rua Terezinha de Jesus da Silva, Araquari - SC",
     "mapa": "https://maps.app.goo.gl/6bkK2kV5EpkVgHhB7"},
    {"cidade": "Araquari", "nome": "Recanto da Fazenda",
     "endereco": "Rod. Carlos Frederico Adolfo Schneider, Araquari - SC, 89245-000",
     "mapa": "https://maps.app.goo.gl/Rardh4MPgYD6LZgZ8"},
    {"cidade": "Barra do Sul", "nome": "Costa do Encanto / Hamilton",
     "endereco": "",
     "mapa": "https://maps.app.goo.gl/F5vcrFSbykXLfDCc7"},
    {"cidade": "Barra Velha", "nome": "Porto do Sol",
     "endereco": "Rua Maristela Mendes Amorin, Barra Velha - SC",
     "mapa": "https://maps.app.goo.gl/9fQeKC7DF9tzuCiD8"},
    {"cidade": "Barra Velha", "nome": "Terra de Açores",
     "endereco": "Rua 2040, Barra Velha - SC",
     "mapa": "https://maps.app.goo.gl/QjjXgbAysGQJCda16"},
    {"cidade": "Canoinhas", "nome": "Canoinhas",
     "endereco": "",
     "mapa": ""},
    {"cidade": "Chapecó", "nome": "Florescer e Matterroir",
     "endereco": "EMC-364, Chapecó - SC",
     "mapa": "https://www.google.com/maps?q=-27.153944,-52.591306"},
    {"cidade": "Chapecó", "nome": "Revoar",
     "endereco": "Acesso Florenal Ribeiro, Chapecó - SC",
     "mapa": "https://www.google.com/maps?q=-27.137417,-52.626806"},
    {"cidade": "Garuva", "nome": "Acordes",
     "endereco": "",
     "mapa": "https://www.google.com/maps?q=-26.0345233,-48.740875"},
    {"cidade": "Garuva", "nome": "Villa Dona Francisca",
     "endereco": "Estr. Palmeira - Três Barras, Garuva - SC, 89248-000",
     "mapa": "https://maps.app.goo.gl/fEoQKCztD9A38mn59"},
    {"cidade": "Garuva", "nome": "Condomínio Mirante da Serra",
     "endereco": "Estrada Caovi, 574, Garuva - SC, 89248-000",
     "mapa": "https://maps.app.goo.gl/heWQ1oeNk3JxZggF6"},
    {"cidade": "Garuva", "nome": "Meneghesso",
     "endereco": "Avenida Getúlio Dornelles Vargas ou Rua Ladislau Ostrowski, Garuva - SC",
     "mapa": "https://maps.app.goo.gl/LvXdUV4jQt6ycKGZ7"},
    {"cidade": "Guaramirim", "nome": "Amizade",
     "endereco": "",
     "mapa": "https://www.google.com/maps?q=-26.4689681,-48.9876709"},
    {"cidade": "Guaramirim", "nome": "Green Village",
     "endereco": "",
     "mapa": "https://www.google.com/maps?q=-26.4802222,-48.9574167"},
    {"cidade": "Itaiópolis", "nome": "Condomínio Lucena",
     "endereco": "BR-116, KM 24 - Bom Jesus, Itaiópolis - SC, 89340-000",
     "mapa": "https://maps.app.goo.gl/RoNiKqhgB23xtoVJ9"},
    {"cidade": "Rio Negrinho", "nome": "Biosul",
     "endereco": "",
     "mapa": ""},
    {"cidade": "São José dos Pinhais", "nome": "Condomínio Cortona",
     "endereco": "",
     "mapa": "https://www.google.com/maps?q=-26.2633642,-49.5445806"},
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


# ------------------------------------------------------------ personalização do formulário (/solicitar)

CONFIG_FORMULARIO_PADRAO = {
    "titulo": "UNIFORME E EPI",
    "descricao": (
        "Este formulário é destinado à solicitação de novos uniformes e Equipamentos de "
        "Proteção Individual (EPIs) para os colaboradores das obras."
    ),
    "pergunta_1_titulo": "1. Selecione o seu nome completo.",
    "pergunta_2_titulo": "2. Informe a obra onde os EPIs solicitados devem ser entregues.",
    "permitir_outro_local": False,
    "pergunta_3_titulo": "3. Informe o motivo da solicitação.",
    "pergunta_4_titulo": "4. Selecione o equipamento de proteção individual necessário.",
    "pergunta_4_subtitulo": "Marque todos os EPIs que você precisa solicitar.",
    "pergunta_calcado_titulo": "Informe a numeração do calçado selecionado",
    "pergunta_calcado_subtitulo": (
        "Digite somente números. Será fornecida uma unidade de cada item solicitado "
        "(caso necessite de mais unidades, informe no campo de observações)."
    ),
    "pergunta_5_titulo": "5. Selecione a vestimenta necessária abaixo",
    "pergunta_5_subtitulo": "",
    "pergunta_uniforme_titulo": "Informe o tamanho de uniforme necessário",
    "pergunta_uniforme_subtitulo": (
        "Será fornecida uma unidade de cada item solicitado. Caso necessite de mais unidades "
        "ou apenas um item específico, informe no campo de observações."
    ),
    "pergunta_6_titulo": "6. Observações",
    "locais": list(LOCAIS_FORMULARIO),
    "motivos": list(MOTIVOS_FORMULARIO),
    "tamanhos_uniforme": list(TAMANHOS_UNIFORME),
    "perguntas_extras": [],
}

# Título antigo da pergunta 2 (quando o local era a cidade): trocado pelo novo padrão
_TITULO_P2_ANTIGO = "2. Informe o local qual os EPIs solicitados devem ser entregues."

_MAX_IMAGEM_BYTES = 2 * 1024 * 1024  # 2 MB


def limpar_mapa(mapa):
    """Aceita só links web (evita javascript: e afins)."""
    mapa = (limpar(mapa) or "")[:2000]
    return mapa if mapa.lower().startswith(("https://", "http://")) else ""


def limpar_obras(lista):
    """Valida uma lista de obras [{cidade, nome, endereco, mapa}], sem repetir o nome da obra."""
    obras, vistas = [], set()
    for o in lista or []:
        if not isinstance(o, dict):
            continue
        nome = (limpar(o.get("nome")) or "")[:120]
        if not nome or normalizar(nome) in vistas:
            continue
        vistas.add(normalizar(nome))
        obras.append({"cidade": (limpar(o.get("cidade")) or "")[:80], "nome": nome,
                      "endereco": (limpar(o.get("endereco")) or "")[:300], "mapa": limpar_mapa(o.get("mapa"))})
    return obras


def obras_ativas(db):
    """Nomes das obras ativas, em ordem alfabética (opções do formulário)."""
    return [r["nome"] for r in db.execute("SELECT nome FROM obras WHERE ativo = 1 ORDER BY nome COLLATE NOCASE")]


def obter_config_formulario(db):
    """Retorna o dicionário de configuração do formulário público mesclado com os padrões."""
    cfg = {
        k: ([dict(x) if isinstance(x, dict) else x for x in v] if isinstance(v, list) else v)
        for k, v in CONFIG_FORMULARIO_PADRAO.items()
    }
    linha = db.execute("SELECT valor FROM configuracoes WHERE chave = 'forms_config'").fetchone()
    if linha and linha["valor"]:
        try:
            salvo = json.loads(linha["valor"])
            if isinstance(salvo, dict):
                for ch, padrao in CONFIG_FORMULARIO_PADRAO.items():
                    if ch not in salvo:
                        continue
                    val = salvo[ch]
                    if ch in ("locais", "motivos", "tamanhos_uniforme"):
                        if isinstance(val, list):
                            limpos = [limpar(x) for x in val if limpar(x)]
                            if limpos:
                                cfg[ch] = limpos
                    elif ch == "perguntas_extras":
                        if isinstance(val, list):
                            cfg[ch] = [p for p in val if isinstance(p, dict) and limpar(p.get("titulo"))]
                    elif isinstance(padrao, bool):
                        cfg[ch] = bool(val)
                    elif isinstance(padrao, str) and val is not None:
                        texto = limpar(str(val))
                        if texto or ch.endswith("_subtitulo"):
                            cfg[ch] = texto
        except ValueError:
            pass
    if cfg["pergunta_2_titulo"] == _TITULO_P2_ANTIGO:
        cfg["pergunta_2_titulo"] = CONFIG_FORMULARIO_PADRAO["pergunta_2_titulo"]
    return cfg


def salvar_config_formulario(db, nova_cfg):
    """Salva o dicionário de configuração do formulário na tabela configuracoes."""
    atual = obter_config_formulario(db)
    for ch, padrao in CONFIG_FORMULARIO_PADRAO.items():
        if ch not in nova_cfg:
            continue
        val = nova_cfg[ch]
        if ch in ("locais", "motivos", "tamanhos_uniforme"):
            if isinstance(val, list):
                limpos = []
                vistos = set()
                for item in val:
                    txt = limpar(item)
                    if txt and normalizar(txt) not in vistos:
                        vistos.add(normalizar(txt))
                        limpos.append(txt[:120])
                if not limpos:
                    raise ErroValidacao(f"A lista de '{ch}' precisa ter ao menos uma opção.")
                atual[ch] = limpos
        elif ch == "perguntas_extras":
            if isinstance(val, list):
                extras_limpas = []
                for idx, p in enumerate(val[:20], start=1):
                    if not isinstance(p, dict):
                        continue
                    titulo_p = limpar(p.get("titulo"))
                    if not titulo_p:
                        continue
                    tipo_p = p.get("tipo") if p.get("tipo") in ("texto", "unica", "multipla") else "texto"
                    opcoes_p = [limpar(o)[:120] for o in (p.get("opcoes") or []) if limpar(o)]
                    if tipo_p in ("unica", "multipla") and not opcoes_p:
                        tipo_p = "texto"
                    extras_limpas.append({
                        "id": limpar(p.get("id")) or f"extra_{idx}",
                        "titulo": titulo_p[:240],
                        "tipo": tipo_p,
                        "obrigatoria": bool(p.get("obrigatoria")),
                        "opcoes": opcoes_p,
                    })
                atual[ch] = extras_limpas
        elif isinstance(padrao, bool):
            atual[ch] = bool(val)
        elif isinstance(padrao, str):
            texto = limpar(str(val or ""))
            if not texto and not ch.endswith("_subtitulo"):
                texto = padrao
            atual[ch] = (texto or "")[:500]

    payload = json.dumps(atual, ensure_ascii=False)
    db.execute(
        "INSERT INTO configuracoes (chave, valor) VALUES ('forms_config', ?) "
        "ON CONFLICT (chave) DO UPDATE SET valor = excluded.valor",
        (payload,),
    )
    return atual


def restaurar_config_formulario(db):
    """Remove customizações de texto/listas, voltando o formulário ao padrão original."""
    db.execute("DELETE FROM configuracoes WHERE chave = 'forms_config'")


def processar_imagem_epi(arquivo=None, texto_imagem=None):
    """Converte um upload de imagem (FileStorage) ou Data URI/URL em string segura para epis.imagem_url."""
    if arquivo and getattr(arquivo, "filename", ""):
        bruto = arquivo.read()
        if bruto:
            if len(bruto) > _MAX_IMAGEM_BYTES:
                raise ErroValidacao("A imagem enviada excede o limite de 2 MB.")
            nome_arq = (arquivo.filename or "").lower()
            if bruto.startswith(b"\x89PNG\r\n\x1a\n"):
                mime = "image/png"
            elif bruto.startswith(b"\xff\xd8\xff"):
                mime = "image/jpeg"
            elif bruto.startswith(b"RIFF") and bruto[8:12] == b"WEBP":
                mime = "image/webp"
            elif bruto.startswith((b"GIF87a", b"GIF89a")):
                mime = "image/gif"
            elif b"<svg" in bruto[:400].lower() or nome_arq.endswith(".svg"):
                mime = "image/svg+xml"
            else:
                raise ErroValidacao("Formato de imagem inválido. Envie PNG, JPG, WEBP, GIF ou SVG.")
            b64 = base64.b64encode(bruto).decode("ascii")
            return f"data:{mime};base64,{b64}"

    if texto_imagem is not None:
        txt = str(texto_imagem).strip()
        if not txt:
            return None
        if len(txt) > _MAX_IMAGEM_BYTES * 2:
            raise ErroValidacao("A imagem informada é muito grande.")
        if txt.startswith(("data:image/", "http://", "https://", "/static/")):
            return txt
        raise ErroValidacao("Informe uma URL de imagem válida (https://...) ou envie um arquivo de imagem.")

    return None


def gerar_svg_ilustrativo_epi(nome):
    """Gera uma ilustração vetorial (Data URI SVG) clara e amigável para identificação visual do EPI."""
    t = normalizar(nome)
    if "luva" in t:
        cor_fundo, cor_borda = "#eff6ff", "#2563eb"
        titulo_curto = "LUVA"
        icone = (
            '<path d="M36 68V34a5 5 0 0 1 10 0v14M46 44V26a5 5 0 0 1 10 0v20M56 44V29a5 5 0 0 1 10 0v21'
            'M66 48V37a5 5 0 0 1 10 0v24c0 14-10 23-24 23H44c-11 0-19-8-19-19V51a5 5 0 0 1 10 0v9" '
            'fill="#93c5fd" stroke="#1d4ed8" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>'
            '<rect x="31" y="76" width="38" height="10" rx="3" fill="#1d4ed8"/>'
        )
    elif "oculos" in t or "lente" in t or "visao" in t:
        cor_fundo, cor_borda = "#f0f9ff", "#0284c7"
        titulo_curto = "ÓCULOS"
        icone = (
            '<path d="M16 48h18a10 10 0 0 1 10 10v3a12 12 0 0 1-24 0v-5a8 8 0 0 1 8-8z'
            'M56 58v3a12 12 0 0 0 24 0v-5a8 8 0 0 0-8-8H56a10 10 0 0 0-10 10z" '
            'fill="#bae6fd" stroke="#0369a1" stroke-width="4"/>'
            '<path d="M44 53h12M16 50l-6-6M80 50l8-6" stroke="#0369a1" stroke-width="4" stroke-linecap="round"/>'
        )
    elif "auricular" in t or "ouvido" in t or "plug" in t or "abafador" in t:
        cor_fundo, cor_borda = "#fefce8", "#ca8a04"
        titulo_curto = "PROTETOR"
        icone = (
            '<path d="M28 56V44a22 22 0 0 1 44 0v12" fill="none" stroke="#a16207" stroke-width="5" stroke-linecap="round"/>'
            '<rect x="18" y="48" width="14" height="26" rx="7" fill="#facc15" stroke="#a16207" stroke-width="4"/>'
            '<rect x="68" y="48" width="14" height="26" rx="7" fill="#facc15" stroke="#a16207" stroke-width="4"/>'
        )
    elif "botina" in t or "bota" in t or "sapat" in t or "calcad" in t:
        cor_fundo, cor_borda = "#fef3c7", "#b45309"
        titulo_curto = "CALÇADO"
        icone = (
            '<path d="M26 26h22v28c0 6 5 10 14 12l16 3a8 8 0 0 1 6 8v5H18V66c0-6 4-12 8-16V26z" '
            'fill="#d97706" stroke="#78350f" stroke-width="4" stroke-linejoin="round"/>'
            '<rect x="16" y="78" width="70" height="8" rx="3" fill="#451a03"/>'
        )
    elif "mascara" in t or "respirador" in t or "pff" in t:
        cor_fundo, cor_borda = "#ecfdf5", "#059669"
        titulo_curto = "MÁSCARA"
        icone = (
            '<path d="M24 48c0-12 12-20 26-20s26 8 26 20c0 16-11 28-26 28S24 64 24 48z" '
            'fill="#a7f3d0" stroke="#047857" stroke-width="4"/>'
            '<circle cx="58" cy="56" r="7" fill="#047857"/>'
            '<path d="M24 44l-10-6M24 58l-10 6M76 44l10-6M76 58l10 6" stroke="#047857" stroke-width="3.5" stroke-linecap="round"/>'
        )
    elif "capacete" in t or "touca" in t or "bone" in t:
        cor_fundo, cor_borda = "#fff7ed", "#ea580c"
        titulo_curto = "CABEÇA"
        icone = (
            '<path d="M20 60a30 30 0 0 1 60 0H20z" fill="#fb923c" stroke="#c2410c" stroke-width="4"/>'
            '<path d="M14 60h72a4 4 0 0 1 0 8H14a4 4 0 0 1 0-8z" fill="#ea580c" stroke="#9a3412" stroke-width="3.5"/>'
            '<path d="M45 30v16M55 30v16" stroke="#9a3412" stroke-width="3.5" stroke-linecap="round"/>'
        )
    elif "creme" in t or "solar" in t or "protetor" in t:
        cor_fundo, cor_borda = "#fdf4ff", "#9333ea"
        titulo_curto = "CREME"
        icone = (
            '<path d="M34 26h32l-4 44H38L34 26z" fill="#e9d5ff" stroke="#7e22ce" stroke-width="4" stroke-linejoin="round"/>'
            '<rect x="40" y="70" width="20" height="12" rx="3" fill="#7e22ce"/>'
            '<path d="M50 40v16M42 48h16" stroke="#7e22ce" stroke-width="4" stroke-linecap="round"/>'
        )
    elif "perneira" in t:
        cor_fundo, cor_borda = "#f1f5f9", "#475569"
        titulo_curto = "PERNEIRA"
        icone = (
            '<path d="M34 22h30l-4 54H38L34 22z" fill="#cbd5e1" stroke="#334155" stroke-width="4" stroke-linejoin="round"/>'
            '<path d="M36 36h26M37 50h24M38 64h22" stroke="#1e293b" stroke-width="3.5"/>'
        )
    elif "capa" in t or "chuva" in t or "impermeavel" in t:
        cor_fundo, cor_borda = "#fef9c3", "#ca8a04"
        titulo_curto = "CAPA"
        icone = (
            '<path d="M36 26l14-8 14 8 14 14-8 10-8-6v34H38V44l-8 6-8-10 14-14z" '
            'fill="#fde047" stroke="#a16207" stroke-width="4" stroke-linejoin="round"/>'
        )
    elif "camis" in t or "blusa" in t or "uniforme" in t or "colete" in t:
        cor_fundo, cor_borda = "#e0f2fe", "#0369a1"
        titulo_curto = "CAMISETA"
        icone = (
            '<path d="M35 24l15 8 15-8 15 12-8 12-9-6v36H37V42l-9 6-8-12 15-12z" '
            'fill="#7dd3fc" stroke="#0369a1" stroke-width="4" stroke-linejoin="round"/>'
        )
    elif "calca" in t or "jardineira" in t:
        cor_fundo, cor_borda = "#e0e7ff", "#3730a3"
        titulo_curto = "CALÇA"
        icone = (
            '<path d="M32 22h36l6 58H56l-6-36-6 36H26l6-58z" '
            'fill="#a5b4fc" stroke="#312e81" stroke-width="4" stroke-linejoin="round"/>'
        )
    else:
        cor_fundo, cor_borda = "#f0fdf4", "#15803d"
        titulo_curto = "EPI"
        icone = (
            '<path d="M50 18l28 10v20c0 18-12 31-28 36-16-5-28-18-28-36V28l28-10z" '
            'fill="#bbf7d0" stroke="#166534" stroke-width="4" stroke-linejoin="round"/>'
            '<path d="M38 50l8 8 16-16" fill="none" stroke="#166534" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>'
        )

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 108" width="100" height="108">'
        f'<rect x="3" y="3" width="94" height="102" rx="14" fill="{cor_fundo}" stroke="{cor_borda}" stroke-width="3"/>'
        f'{icone}'
        f'<text x="50" y="99" text-anchor="middle" font-family="system-ui,sans-serif" font-size="10.5" '
        f'font-weight="800" fill="{cor_borda}">{titulo_curto}</text>'
        f'</svg>'
    )
    b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}"


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
    sql, params = (
        "SELECT p.*, c.nome AS colaborador_nome, c.funcao AS colaborador_funcao, c.setor AS colaborador_setor "
        "FROM pedidos p LEFT JOIN colaboradores c ON c.id = p.colaborador_id WHERE 1 = 1",
        [],
    )
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

    # Kits padrão por cargo e tamanhos registrados na ficha do colaborador para sinalizar "Analisar"
    padrao_por_cargo = {}
    for r in db.execute("SELECT cargo, epi_id FROM cargo_epis_padrao"):
        ch_cargo = (r["cargo"] or "").strip().casefold()
        if ch_cargo:
            padrao_por_cargo.setdefault(ch_cargo, set()).add(r["epi_id"])

    padrao_por_colab = {}
    for r in db.execute("SELECT colaborador_id, epi_id FROM colaborador_epis_padrao"):
        padrao_por_colab.setdefault(r["colaborador_id"], set()).add(r["epi_id"])

    tamanhos_por_colab = {}
    for r in db.execute("SELECT colaborador_id, chave, tamanho FROM colaborador_tamanhos"):
        tamanhos_por_colab[(r["colaborador_id"], (r["chave"] or "").strip().casefold())] = r["tamanho"]

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
        p["nao_separados"] = len(validos) - p["separados"]
        p["nao_identificados"] = sum(1 for i in validos if i["epi_id"] is None)
        p["pronto_para_finalizar"] = bool(validos) and p["separados"] == len(validos) and p["nao_identificados"] == 0
        p["pode_finalizar_com_ressalva"] = bool(validos) and p["separados"] < len(validos)
        p["tem_ressalva"] = bool(p.get("ressalva_observacao"))

        # Quantidade total pedida por EPI vs saldo (aviso de falta)
        pedido_por_epi = {}
        for i in validos:
            if i["epi_id"]:
                pedido_por_epi[i["epi_id"]] = pedido_por_epi.get(i["epi_id"], 0) + i["quantidade"]

        colab_id = p.get("colaborador_id")
        cargo_colab = (p.get("colaborador_funcao") or "").strip()
        kit_cargo = padrao_por_cargo.get(cargo_colab.casefold()) if cargo_colab else None
        kit_ind = padrao_por_colab.get(colab_id) if colab_id else None

        for i in validos:
            i["falta"] = bool(i["epi_id"]) and i["saldo"] is not None and pedido_por_epi[i["epi_id"]] > i["saldo"]
            alertas = []
            i["fora_do_cargo"] = False
            i["tamanho_divergente"] = False
            i["tamanho_ficha"] = None

            if i["epi_id"]:
                if kit_cargo is not None:
                    if i["epi_id"] not in kit_cargo:
                        i["fora_do_cargo"] = True
                        alertas.append(f"Fora do padrão do cargo ({cargo_colab})")
                elif kit_ind is not None:
                    if i["epi_id"] not in kit_ind:
                        i["fora_do_cargo"] = True
                        alertas.append("Fora do kit padrão do colaborador")
                elif colab_id and not cargo_colab:
                    alertas.append("Colaborador sem cargo definido para validar kit")

                if colab_id and i.get("tamanho"):
                    nome_ref = i.get("epi_nome") or i.get("texto_original") or ""
                    ch_tipo = tipo_calcado(nome_ref) or tipo_uniforme(nome_ref)
                    tam_ficha = (
                        tamanhos_por_colab.get((colab_id, f"epi:{i['epi_id']}"))
                        or (tamanhos_por_colab.get((colab_id, ch_tipo)) if ch_tipo else None)
                    )
                    i["tamanho_ficha"] = tam_ficha
                    if tam_ficha and normalizar(tam_ficha) != normalizar(i["tamanho"]):
                        i["tamanho_divergente"] = True
                        alertas.append(f"Tamanho diverge da ficha (ficha: {tam_ficha})")

            if i["quantidade"] > 1:
                alertas.append(f"Quantidade acima de 1 un. ({i['quantidade']}×)")
            if i["falta"]:
                alertas.append(f"Estoque insuficiente (saldo: {i['saldo']} {i['unidade']})")

            i["alertas_analise"] = alertas
            i["requer_analise"] = len(alertas) > 0

        p["itens_para_analisar"] = [i for i in validos if i.get("requer_analise")]
        p["total_analise"] = len(p["itens_para_analisar"])

        # Pendência de ressalva ("aguardando estoque"): complemento ainda não iniciado
        p["eh_complemento"] = bool(p.get("pedido_origem_id"))
        p["aguardando_estoque"] = p["eh_complemento"] and p["status"] == "pendente"
        p["itens_com_saldo"] = sum(1 for i in validos if i["epi_id"] and not i["falta"])
        p["estoque_disponivel"] = bool(validos) and p["itens_com_saldo"] == len(validos)

    # Pendências geradas a partir de cada pedido (para o histórico do pedido de origem)
    filhos = {}
    for r in db.execute(
        f"SELECT id, status, pedido_origem_id FROM pedidos WHERE pedido_origem_id IN ({','.join('?' * len(ids))}) ORDER BY id",
        ids,
    ):
        filhos.setdefault(r["pedido_origem_id"], []).append({"id": r["id"], "status": r["status"]})
    for p in pedidos:
        p["complementos"] = filhos.get(p["id"], [])

    _calcular_sla(db, pedidos)
    return pedidos


# ------------------------------------------------------------ SLA (prazos de atendimento)

SLA_PADRAO = {
    "horas_atendimento": 24,   # da chegada do pedido até ficar "pronto para retirada"
    "horas_alerta": 4,         # quanto antes do vencimento o pedido passa a "em risco"
    "dias_retirada": 3,        # depois de pronto, prazo para o colaborador/encarregado retirar
}
_LIMITES_SLA = {"horas_atendimento": (1, 720), "horas_alerta": (0, 168), "dias_retirada": (1, 60)}


def obter_config_sla(db):
    cfg = dict(SLA_PADRAO)
    linha = db.execute("SELECT valor FROM configuracoes WHERE chave = 'sla_pedidos'").fetchone()
    if linha and linha["valor"]:
        try:
            salvo = json.loads(linha["valor"])
            for ch, (minimo, maximo) in _LIMITES_SLA.items():
                if isinstance(salvo.get(ch), (int, float)) and minimo <= salvo[ch] <= maximo:
                    cfg[ch] = int(salvo[ch])
        except (ValueError, AttributeError):
            pass
    return cfg


def salvar_config_sla(db, dados):
    cfg = {}
    for ch, (minimo, maximo) in _LIMITES_SLA.items():
        try:
            valor = int(str(dados.get(ch, "")).strip())
        except ValueError:
            raise ErroValidacao("Informe os prazos do SLA em números inteiros.")
        if not minimo <= valor <= maximo:
            raise ErroValidacao(f"Valor fora do limite para o SLA ({minimo} a {maximo}).")
        cfg[ch] = valor
    if cfg["horas_alerta"] >= cfg["horas_atendimento"]:
        raise ErroValidacao("O alerta precisa ser menor que o prazo de atendimento.")
    db.execute(
        "INSERT INTO configuracoes (chave, valor) VALUES ('sla_pedidos', ?) "
        "ON CONFLICT (chave) DO UPDATE SET valor = excluded.valor",
        (json.dumps(cfg),),
    )
    return cfg


def _data_hora(valor):
    try:
        return datetime.strptime(str(valor)[:19], "%Y-%m-%d %H:%M:%S") if valor else None
    except ValueError:
        return None


def _horas(delta):
    return delta.total_seconds() / 3600


def _calcular_sla(db, pedidos):
    """Preenche p['sla'] com o prazo e a situação de cada pedido.

    O relógio começa na chegada do pedido; numa pendência de ressalva ele só começa quando a separação
    é iniciada (enquanto aguarda estoque, o SLA fica pausado). Contagem em horas corridas.
    """
    cfg = obter_config_sla(db)
    agora = _data_hora(db.execute("SELECT datetime('now', 'localtime') AS agora").fetchone()["agora"])
    prazo_h = cfg["horas_atendimento"]
    for p in pedidos:
        sla = {"estado": None, "prazo": None, "restante_h": None, "tempo_atendimento_h": None,
               "retirada": None, "retirada_h": None, "horas_atendimento": prazo_h}
        inicio = _data_hora(p["iniciado_em"] if p["eh_complemento"] else p["recebido_em"])
        prazo = inicio + timedelta(hours=prazo_h) if inicio else None
        sla["prazo"] = prazo.strftime("%Y-%m-%d %H:%M:%S") if prazo else None

        if p["status"] in ("novo", "pendente", "em_separacao"):
            if p["aguardando_estoque"] or not prazo:
                sla["estado"] = "pausado"
            else:
                restante = _horas(prazo - agora)
                sla["restante_h"] = restante
                sla["estado"] = ("atrasado" if restante < 0
                                 else "em_risco" if restante <= cfg["horas_alerta"] else "no_prazo")
        elif p["status"] in ("pronto", "retirado"):
            fim = _data_hora(p["finalizado_em"])
            if inicio and fim:
                sla["tempo_atendimento_h"] = _horas(fim - inicio)
                sla["estado"] = "cumprido" if fim <= prazo else "fora_do_prazo"
            if fim and p["status"] == "pronto":
                espera = _horas(agora - fim)
                sla["retirada_h"] = espera
                sla["retirada"] = "atrasada" if espera > cfg["dias_retirada"] * 24 else "aguardando"
            elif fim and p["status"] == "retirado":
                retirado = _data_hora(p["retirado_em"])
                if retirado:
                    sla["retirada_h"] = _horas(retirado - fim)
                    sla["retirada"] = "no_prazo" if sla["retirada_h"] <= cfg["dias_retirada"] * 24 else "fora_do_prazo"
        p["sla"] = sla


def resumo_sla(pedidos_abertos):
    """Contagem para o painel da aba Pedidos: atrasados, em risco, no prazo, pausados e retiradas atrasadas."""
    resumo = {"atrasado": 0, "em_risco": 0, "no_prazo": 0, "pausado": 0, "retirada_atrasada": 0}
    for p in pedidos_abertos:
        estado = p["sla"]["estado"]
        if estado in resumo:
            resumo[estado] += 1
        if p["sla"]["retirada"] == "atrasada":
            resumo["retirada_atrasada"] += 1
    return resumo


def relatorio_sla(db, de, ate):
    """Indicadores de SLA dos pedidos recebidos no período (datas ISO 'AAAA-MM-DD')."""
    todos = [p for p in carregar(db, status=("pronto", "retirado", "novo", "pendente", "em_separacao"))
             if de <= (p["recebido_em"] or "")[:10] <= ate]
    atendidos = [p for p in todos if p["sla"]["estado"] in ("cumprido", "fora_do_prazo")]
    no_prazo = [p for p in atendidos if p["sla"]["estado"] == "cumprido"]
    retirados = [p for p in todos if p["status"] == "retirado" and p["sla"]["retirada_h"] is not None]

    def media(valores):
        valores = [v for v in valores if v is not None]
        return sum(valores) / len(valores) if valores else None

    por_obra = {}
    for p in atendidos:
        o = por_obra.setdefault(p["local_destino"] or "(sem obra)", {"total": 0, "no_prazo": 0, "horas": []})
        o["total"] += 1
        o["no_prazo"] += p["sla"]["estado"] == "cumprido"
        o["horas"].append(p["sla"]["tempo_atendimento_h"])
    obras = sorted(
        ({"nome": k, "total": v["total"], "no_prazo": v["no_prazo"],
          "pct": round(100 * v["no_prazo"] / v["total"]), "media_h": media(v["horas"])} for k, v in por_obra.items()),
        key=lambda o: (o["pct"], -o["total"]),
    )
    return {
        "cfg": obter_config_sla(db),
        "recebidos": len(todos),
        "atendidos": len(atendidos),
        "no_prazo": len(no_prazo),
        "pct_no_prazo": round(100 * len(no_prazo) / len(atendidos)) if atendidos else None,
        "media_atendimento_h": media([p["sla"]["tempo_atendimento_h"] for p in atendidos]),
        "media_retirada_h": media([p["sla"]["retirada_h"] for p in retirados]),
        "abertos_atrasados": [p for p in todos if p["sla"]["estado"] == "atrasado"],
        "fora_do_prazo": [p for p in atendidos if p["sla"]["estado"] == "fora_do_prazo"][:15],
        "por_obra": obras,
    }


def pendencias_com_estoque(db):
    """Ids das pendências de ressalva que já têm saldo para todos os itens (podem ser separadas)."""
    return [p["id"] for p in carregar(db, status=("pendente",)) if p["aguardando_estoque"] and p["estoque_disponivel"]]


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


def finalizar(db, pedido_id, usuario_id, com_ressalva=False, ressalva_obs=None, gerar_pendencia=True):
    """Dá baixa no estoque (uma entrega) e deixa o pedido 'pronto para retirada'.

    Se `com_ressalva=True`, permite finalizar mesmo com itens não marcados como separados,
    exigindo `ressalva_obs` e dando baixa no estoque apenas dos itens efetivamente separados.
    Com `gerar_pendencia`, os itens que faltaram viram um pedido complementar "aguardando estoque"
    (status pendente, ligado ao original por pedido_origem_id). Devolve o id desse complemento ou None.
    """
    obs_ressalva = limpar(ressalva_obs) if com_ressalva else None
    if com_ressalva and not obs_ressalva:
        raise ErroValidacao("Informe a observação/motivo da ressalva para explicar os itens não atendidos.")

    def executar():
        p = _pedido(db, pedido_id, "em_separacao")
        itens = db.execute("SELECT * FROM pedido_itens WHERE pedido_id = ? AND ignorado = 0", (pedido_id,)).fetchall()
        if not itens:
            raise ErroValidacao("O pedido não tem itens.")

        if com_ressalva:
            itens_baixa = [i for i in itens if i["separado"]]
            if len(itens_baixa) == len(itens):
                raise ErroValidacao("Todos os itens já estão marcados como separados. Use o botão normal “Finalizar pedido”.")
            if any(i["epi_id"] is None for i in itens_baixa):
                raise ErroValidacao("Há itens marcados como separados sem EPI identificado.")
            if itens_baixa and not p["colaborador_id"]:
                raise ErroValidacao("Vincule o pedido a um colaborador cadastrado antes de finalizar.")
        else:
            if not p["colaborador_id"]:
                raise ErroValidacao("Vincule o pedido a um colaborador cadastrado antes de finalizar.")
            if any(i["epi_id"] is None for i in itens):
                raise ErroValidacao("Há itens sem EPI identificado.")
            if not all(i["separado"] for i in itens):
                raise ErroValidacao("Marque todos os itens como separados (ou utilize “Finalizar com ressalva”).")
            itens_baixa = list(itens)

        grupo = None
        if itens_baixa:
            obs_mov = f"Pedido #{pedido_id}"
            if obs_ressalva:
                obs_mov = f"Pedido #{pedido_id} (Ressalva: {obs_ressalva})"
            grupo = estoque.registrar_na_transacao(
                db, "saida", [{"epi_id": i["epi_id"], "quantidade": i["quantidade"]} for i in itens_baixa], hoje(),
                usuario_id, colaborador_id=p["colaborador_id"], motivo=p["motivo"],
                observacao=obs_mov[:500], pedido_id=pedido_id, local_destino=p["local_destino"])

        db.execute(
            "UPDATE pedidos SET status = 'pronto', grupo = ?, finalizado_por = ?, "
            "finalizado_em = datetime('now', 'localtime'), ressalva_observacao = ? WHERE id = ?",
            (grupo, usuario_id, obs_ressalva[:1000] if obs_ressalva else None, pedido_id),
        )

        faltantes = [i for i in itens if not i["separado"]] if com_ressalva and gerar_pendencia else []
        if not faltantes:
            return None
        cur = db.execute(
            """INSERT INTO pedidos (solicitante, local_destino, motivo, observacao, colaborador_id, status,
                                    pedido_origem_id, dados_brutos)
               VALUES (?, ?, ?, ?, ?, 'pendente', ?, ?)""",
            (p["solicitante"], p["local_destino"], p["motivo"],
             f"Ressalva: {obs_ressalva}"[:1000],
             p["colaborador_id"], pedido_id, json.dumps({"complemento_de": pedido_id})),
        )
        novo_id = cur.lastrowid
        for i in faltantes:
            db.execute(
                """INSERT INTO pedido_itens (pedido_id, coluna, texto_original, quantidade, epi_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (novo_id, i["coluna"], i["texto_original"], i["quantidade"], i["epi_id"]),
            )
        return novo_id

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
