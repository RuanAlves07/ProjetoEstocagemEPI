"""Integração com a Base CAEPI (API_BaseCAEPI / MTE tgg_export_caepi) e consulta automática por Nº de CA.

Compatível com:
1. Instância externa do projeto `JoaoAugustoMV/API_BaseCAEPI` (FastAPI/Docker, atualização diária às 20:00);
2. Importação / download direto do arquivo oficial do MTE (`tgg_export_caepi.zip` / `tgg_export_caepi.txt`);
3. Base local `base_caepi` no banco de dados (SQLite / PostgreSQL Supabase) + catálogo semente embutido.
"""
import io
import json
import os
import re
import unicodedata
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta

from .estoque import ErroValidacao
from .util import hoje, limpar, parse_data

URL_FTP_MTE_CAEPI = (
    "ftp://ftp.mtps.gov.br/portal/fiscalizacao/seguranca-e-saude-no-trabalho/caepi/tgg_export_caepi.zip"
)

# Catálogo semente com CAs reais utilizados em obras, indústria e no catálogo padrão do sistema
CATALOGO_CA_SEMENTE = {
    "39840": {
        "validade_ca": "2028-11-15",
        "situacao": "VÁLIDO",
        "nome_equipamento": "LUVA PARA PROTEÇÃO CONTRA AGENTES MECÂNICOS",
        "nome_sugerido": "LUVAS DE POLIAMIDA COM BANHO DE PU (MECÂNICA)",
        "descricao": "Luva de segurança tricotada em fios de poliamida (nylon), revestida em poliuretano (PU) na palma e dedos.",
        "categoria": "Mãos",
        "unidade": "par",
        "fabricante": "VOLK DO BRASIL LTDA",
        "cnpj_fabricante": "01.797.921/0001-65",
        "referencia": " PU Multitato",
    },
    "36712": {
        "validade_ca": "2028-08-20",
        "situacao": "VÁLIDO",
        "nome_equipamento": "LUVA PARA PROTEÇÃO CONTRA AGENTES MECÂNICOS E ABRASIVOS",
        "nome_sugerido": "LUVAS VAQUETA PETROLEIRA",
        "descricao": "Luva de segurança confeccionada em couro bovino curtido ao cromo (vaqueta/raspa), modelo petroleira.",
        "categoria": "Mãos",
        "unidade": "par",
        "fabricante": " ZANEL EQUIPAMENTOS DE PROTEÇÃO LTDA",
        "cnpj_fabricante": "04.512.880/0001-32",
        "referencia": "Vaqueta Petroleira",
    },
    "31205": {
        "validade_ca": "2029-02-10",
        "situacao": "VÁLIDO",
        "nome_equipamento": "LUVA PARA PROTEÇÃO CONTRA AGENTES MECÂNICOS",
        "nome_sugerido": "LUVA DE ALGODAO MESCLA 6 FIOS",
        "descricao": "Luva de segurança tricotada em fios de algodão e poliéster (mescla) 6 fios.",
        "categoria": "Mãos",
        "unidade": "par",
        "fabricante": "KALIPSO EQUIPAMENTOS INDIVIDUAIS DE PROTEÇÃO LTDA",
        "cnpj_fabricante": "03.854.112/0001-90",
        "referencia": "Tricotada 6 Fios",
    },
    "34890": {
        "validade_ca": "2028-06-30",
        "situacao": "VÁLIDO",
        "nome_equipamento": "LUVA PARA PROTEÇÃO CONTRA AGENTES QUÍMICOS E UMIDADE",
        "nome_sugerido": "LUVA MULTIUSO AMARELA DE LATEX",
        "descricao": "Luva de segurança confeccionada em borracha natural (látex) amarela, interior forrado com flocos de algodão.",
        "categoria": "Mãos",
        "unidade": "par",
        "fabricante": "SANRO / DANNY EQUIPAMENTOS DE PROTEÇÃO",
        "cnpj_fabricante": "61.088.316/0001-01",
        "referencia": "Látex Multiuso",
    },
    "29110": {
        "validade_ca": "2029-05-18",
        "situacao": "VÁLIDO",
        "nome_equipamento": "PROTETOR AUDITIVO",
        "nome_sugerido": "ABAFADOR TIPO CONCHA 18DB",
        "descricao": "Protetor auditivo circum-auricular tipo concha com haste acolchoada, atenuação NRRsf 18 dB.",
        "categoria": "Auditivo",
        "unidade": "un",
        "fabricante": "3M DO BRASIL LTDA",
        "cnpj_fabricante": "45.985.371/0001-08",
        "referencia": "Peltor H6A / Muffler",
    },
    "29115": {
        "validade_ca": "2028-10-05",
        "situacao": "VÁLIDO",
        "nome_equipamento": "PROTETOR AUDITIVO",
        "nome_sugerido": "ABAFADOR TIPO CONCHA 26DB (Máquina Aberta)",
        "descricao": "Protetor auditivo tipo concha de alta atenuação (26 dB) para operadores de máquinas pesadas.",
        "categoria": "Auditivo",
        "unidade": "un",
        "fabricante": "MSA DO BRASIL EQUIPAMENTOS E INSTRUMENTOS DE SEGURANÇA LTDA",
        "cnpj_fabricante": "45.655.461/0001-30",
        "referencia": "Sordin / XLS",
    },
    "19578": {
        "validade_ca": "2028-12-12",
        "situacao": "VÁLIDO",
        "nome_equipamento": "PROTETOR AUDITIVO",
        "nome_sugerido": "PROTETOR AURICULAR PLUG DE SILICONE 15DB",
        "descricao": "Protetor auditivo de inserção pré-moldado em silicone grau farmacêutico com três flanges e cordão.",
        "categoria": "Auditivo",
        "unidade": "un",
        "fabricante": "POMP / 3M DO BRASIL LTDA",
        "cnpj_fabricante": "45.985.371/0001-08",
        "referencia": "Plug Silicone 3 Flanges",
    },
    "11269": {
        "validade_ca": "2028-09-14",
        "situacao": "VÁLIDO",
        "nome_equipamento": "ÓCULOS",
        "nome_sugerido": "OCULOS SUMMER CINZA",
        "descricao": "Óculos de segurança constituídos de arco e visor em policarbonato cinza (fumê) com proteção UV e tratamento antirrisco.",
        "categoria": "Olhos e Face",
        "unidade": "un",
        "fabricante": "KALIPSO EQUIPAMENTOS INDIVIDUAIS DE PROTEÇÃO LTDA",
        "cnpj_fabricante": "03.854.112/0001-90",
        "referencia": "Summer Cinza",
    },
    "11268": {
        "validade_ca": "2028-09-14",
        "situacao": "VÁLIDO",
        "nome_equipamento": "ÓCULOS",
        "nome_sugerido": "OCULOS LEOPARDO INCOLOR",
        "descricao": "Óculos de segurança com lente única em policarbonato incolor, apoio nasal e hastes tipo espátula.",
        "categoria": "Olhos e Face",
        "unidade": "un",
        "fabricante": "KALIPSO EQUIPAMENTOS INDIVIDUAIS DE PROTEÇÃO LTDA",
        "cnpj_fabricante": "03.854.112/0001-90",
        "referencia": "Leopardo Incolor",
    },
    "18420": {
        "validade_ca": "2028-07-22",
        "situacao": "VÁLIDO",
        "nome_equipamento": "ÓCULOS",
        "nome_sugerido": "OCULOS PANDA INCOLOR (Sobrepor Óculos de Grau)",
        "descricao": "Óculos de segurança modelo ampla visão/sobreposição em policarbonato incolor para uso sobre óculos graduados.",
        "categoria": "Olhos e Face",
        "unidade": "un",
        "fabricante": "KALIPSO EQUIPAMENTOS INDIVIDUAIS DE PROTEÇÃO LTDA",
        "cnpj_fabricante": "03.854.112/0001-90",
        "referencia": "Panda Sobrepor",
    },
    "32901": {
        "validade_ca": "2028-10-10",
        "situacao": "VÁLIDO",
        "nome_equipamento": "RESPIRADOR PURIFICADOR DE AR TIPO PEÇA SEMIFACIAL",
        "nome_sugerido": "MASCARA SEMIFACIAL JUPITER (Roçada)",
        "descricao": "Respirador purificador de ar tipo peça semifacial em elastômero termoplástico com suporte para filtros.",
        "categoria": "Respiratório",
        "unidade": "un",
        "fabricante": "PLASTCOR DO BRASIL LTDA",
        "cnpj_fabricante": "02.894.155/0001-44",
        "referencia": "Júpiter Semifacial",
    },
    "38504": {
        "validade_ca": "2028-07-19",
        "situacao": "VÁLIDO",
        "nome_equipamento": "RESPIRADOR PURIFICADOR DE AR TIPO PEÇA SEMIFACIAL FILTRANTE PARA PARTÍCULAS PFF2",
        "nome_sugerido": "RESPIRADOR PFF2 COM VALVULA",
        "descricao": "Respirador descartável tipo peça semifacial filtrante para partículas classe PFF2 (S) com válvula de exalação.",
        "categoria": "Respiratório",
        "unidade": "un",
        "fabricante": "CAMPER EQUIPAMENTOS DE PROTEÇÃO LTDA",
        "cnpj_fabricante": "05.412.660/0001-19",
        "referencia": "PFF2 Valvulada",
    },
    "32902": {
        "validade_ca": "2028-10-10",
        "situacao": "VÁLIDO",
        "nome_equipamento": "FILTRO PARA PARTÍCULAS",
        "nome_sugerido": "FILTRO COM RETENTOR PARA POEIRAS, NEVOAS E FUMOS (Roçada)",
        "descricao": "Filtro mecânico classe P2 com retentor para proteção contra poeiras, névoas e fumos.",
        "categoria": "Respiratório",
        "unidade": "par",
        "fabricante": "PLASTCOR DO BRASIL LTDA",
        "cnpj_fabricante": "02.894.155/0001-44",
        "referencia": "Filtro Mecânico P2",
    },
    "42110": {
        "validade_ca": "2029-04-15",
        "situacao": "VÁLIDO",
        "nome_equipamento": "CALÇADO TIPO BOTINA",
        "nome_sugerido": "SAPATÃO BIDENSIDADE C/ CADARCO (Topografia)",
        "descricao": "Calçado ocupacional de uso profissional tipo botina, fechamento em cadarço, confeccionado em couro, solado PU bidensidade.",
        "categoria": "Calçados",
        "unidade": "par",
        "fabricante": "MARLUVAS EQUIPAMENTOS DE SEGURANÇA LTDA",
        "cnpj_fabricante": "19.758.846/0001-11",
        "referencia": "50B22 Cadarço",
    },
    "42890": {
        "validade_ca": "2029-03-28",
        "situacao": "VÁLIDO",
        "nome_equipamento": "CALÇADO TIPO BOTINA",
        "nome_sugerido": "SAPATÃO CANO BAIXO C/ ELASTICO BICO PVC (Motoristas e Operadores)",
        "descricao": "Calçado de segurança tipo botina, fechamento em elástico lateral, biqueira de PVC/polipropileno, solado bidensidade.",
        "categoria": "Calçados",
        "unidade": "par",
        "fabricante": "BRACOL / BSB PRODUTORA DE EQUIPAMENTOS DE PROTEÇÃO INDIVIDUAL S.A.",
        "cnpj_fabricante": "08.842.690/0001-54",
        "referencia": "BEL Bico PVC",
    },
    "43105": {
        "validade_ca": "2029-03-10",
        "situacao": "VÁLIDO",
        "nome_equipamento": "CALÇADO TIPO BOTINA",
        "nome_sugerido": "BOTINA BIDENSIDADE C/ BICO PVC (Pedreiros, Auxiliares, Serventes e Roçada)",
        "descricao": "Calçado de segurança tipo botina em couro relax preto, elástico lateral recoberto, biqueira de conformação PVC e solado PU bidensidade.",
        "categoria": "Calçados",
        "unidade": "par",
        "fabricante": "MARLUVAS EQUIPAMENTOS DE SEGURANÇA LTDA",
        "cnpj_fabricante": "19.758.846/0001-11",
        "referencia": "10B19 Bico PVC",
    },
    "37400": {
        "validade_ca": "2029-01-25",
        "situacao": "VÁLIDO",
        "nome_equipamento": "CALÇADO TIPO BOTA",
        "nome_sugerido": "BOTA DE PVC PRETA CANO EXTRA LONGO",
        "descricao": "Calçado de segurança tipo bota impermeável confeccionada em policloreto de vinila (PVC) injetado preto, cano longo.",
        "categoria": "Calçados",
        "unidade": "par",
        "fabricante": "VULCABRAS / AZALEIA - BSB S.A.",
        "cnpj_fabricante": "08.842.690/0001-54",
        "referencia": "Acqua / Cano Longo PVC",
    },
    "44910": {
        "validade_ca": "2029-06-12",
        "situacao": "VÁLIDO",
        "nome_equipamento": "CALÇADO TIPO BOTINA",
        "nome_sugerido": "SAPATÃO BIDENSIDADE NOBUCK MARROM (Coordenadores e Encarregados - Masculino)",
        "descricao": "Calçado de segurança tipo botina confeccionado em couro nobuck marrom, colarinho acolchoado, solado PU bidensidade.",
        "categoria": "Calçados",
        "unidade": "par",
        "fabricante": "FUJIWARA / BSB S.A.",
        "cnpj_fabricante": "08.842.690/0001-54",
        "referencia": "Premier Nobuck",
    },
    "31469": {
        "validade_ca": "2028-12-01",
        "situacao": "VÁLIDO",
        "nome_equipamento": "CAPACETE",
        "nome_sugerido": "CAPACETE DE SEGURANÇA C/ SUSPENSÃO",
        "descricao": "Capacete de segurança para uso na indústria, classe B, aba frontal, com carneira e jugular.",
        "categoria": "Cabeça",
        "unidade": "un",
        "fabricante": "PLASTCOR DO BRASIL LTDA",
        "cnpj_fabricante": "02.894.155/0001-44",
        "referencia": "PLT Classe B",
    },
    "39012": {
        "validade_ca": "2028-10-18",
        "situacao": "VÁLIDO",
        "nome_equipamento": "CAPUZ OU BALACLAVA",
        "nome_sugerido": "BONE BRIM AZUL COM PROTETOR DE NUCA",
        "descricao": "Capuz de segurança tipo boné árabe (touca árabe) confeccionado em tecido brim azul com saia protetora de nuca e pescoço.",
        "categoria": "Cabeça",
        "unidade": "un",
        "fabricante": "LEDAN EQUIPAMENTOS DE PROTEÇÃO LTDA",
        "cnpj_fabricante": "04.129.770/0001-18",
        "referencia": "Touca Árabe Brim",
    },
    "39015": {
        "validade_ca": "2028-10-18",
        "situacao": "VÁLIDO",
        "nome_equipamento": "CAPUZ OU BALACLAVA",
        "nome_sugerido": "BONE BRIM COM PROTETOR TELA (Roçada)",
        "descricao": "Capuz de segurança tipo boné em tecido brim com protetor facial em tela de nylon/aço e saia para proteção de nuca em roçada.",
        "categoria": "Cabeça",
        "unidade": "un",
        "fabricante": "LEDAN EQUIPAMENTOS DE PROTEÇÃO LTDA",
        "cnpj_fabricante": "04.129.770/0001-18",
        "referencia": "Boné Roçador c/ Tela",
    },
    "28711": {
        "validade_ca": "2028-09-02",
        "situacao": "VÁLIDO",
        "nome_equipamento": "PROTETOR FACIAL",
        "nome_sugerido": "PROTETOR FACIAL INCOLOR ARTICULADO (Mecânica)",
        "descricao": "Protetor facial de segurança constituído de coroa e carneira plástica com regulagem e visor em policarbonato incolor 8 polegadas.",
        "categoria": "Olhos e Face",
        "unidade": "un",
        "fabricante": "PLASTCOR DO BRASIL LTDA",
        "cnpj_fabricante": "02.894.155/0001-44",
        "referencia": "Visor Policarbonato 8\"",
    },
    "35100": {
        "validade_ca": "2029-01-10",
        "situacao": "VÁLIDO",
        "nome_equipamento": "VESTIMENTA TIPO CAPA",
        "nome_sugerido": "CAPA DE CHUVA TREVIRA FORRADA AMARELA",
        "descricao": "Vestimenta de segurança tipo capa impermeável confeccionada em tecido sintético plastificado com PVC (Trevira) amarelo com capuz.",
        "categoria": "Vestimenta",
        "unidade": "un",
        "fabricante": "MAICOL DO BRASIL LTDA",
        "cnpj_fabricante": "59.104.221/0001-72",
        "referencia": "Capa Trevira Amarela",
    },
    "26410": {
        "validade_ca": "2028-11-28",
        "situacao": "VÁLIDO",
        "nome_equipamento": "AVENTAL",
        "nome_sugerido": "AVENTAL DE RASPA SEM EMENDA 1X0,6 (Roçada e Mecânica)",
        "descricao": "Avental de segurança confeccionado em raspa de couro bovino curtido ao cromo, sem emenda, medida 1,00 m x 0,60 m.",
        "categoria": "Vestimenta",
        "unidade": "un",
        "fabricante": "ZANEL EQUIPAMENTOS DE PROTEÇÃO LTDA",
        "cnpj_fabricante": "04.512.880/0001-32",
        "referencia": "Avental Raspa 1,00x0,60",
    },
    "26415": {
        "validade_ca": "2028-11-28",
        "situacao": "VÁLIDO",
        "nome_equipamento": "PERNEIRA",
        "nome_sugerido": "PERNEIRA DE RASPA COM VELCRO (Mecânica)",
        "descricao": "Perneira de segurança confeccionada em raspa de couro bovino com fechamento em velcro para proteção das pernas.",
        "categoria": "Vestimenta",
        "unidade": "par",
        "fabricante": "ZANEL EQUIPAMENTOS DE PROTEÇÃO LTDA",
        "cnpj_fabricante": "04.512.880/0001-32",
        "referencia": "Perneira Raspa Velcro",
    },
    "33810": {
        "validade_ca": "2028-10-12",
        "situacao": "VÁLIDO",
        "nome_equipamento": "PERNEIRA",
        "nome_sugerido": "PERNEIRA DE PROTECAO BIDIN/COURO COM 3 TALAS SOLDADAS (Roçada)",
        "descricao": "Perneira de segurança confeccionada em material sintético tipo couro/bidim com três talas frontais de polipropileno soldadas.",
        "categoria": "Vestimenta",
        "unidade": "par",
        "fabricante": "SAFRA EQUIPAMENTOS DE PROTEÇÃO LTDA",
        "cnpj_fabricante": "07.311.490/0001-05",
        "referencia": "Perneira 3 Talas",
    },
    "40988": {
        "validade_ca": "2029-02-19",
        "situacao": "VÁLIDO",
        "nome_equipamento": "VESTIMENTA TIPO MACACÃO / JARDINEIRA",
        "nome_sugerido": "JARDINEIRA DE SANEAMENTO COM BOTAS (Motorista de Caminhão Pipa)",
        "descricao": "Vestimenta de segurança impermeável tipo jardineira em PVC com botas acopladas por termofusão.",
        "categoria": "Vestimenta",
        "unidade": "un",
        "fabricante": "TREVIRA / BRASFORT EQUIPAMENTOS",
        "cnpj_fabricante": "03.998.120/0001-41",
        "referencia": "Jardineira PVC c/ Bota",
    },
}


def normalizar_ca(valor):
    """Extrai apenas os dígitos do número do CA (ex.: 'CA 39.840' -> '39840')."""
    digitos = re.sub(r"\D+", "", str(valor or ""))
    return digitos.lstrip("0") or (digitos[:1] if digitos else "")


def _sem_acento(texto):
    t = unicodedata.normalize("NFKD", str(texto or ""))
    return "".join(c for c in t if not unicodedata.combining(c)).strip().casefold()


def converter_data_caepi(texto):
    """Converte datas da API_BaseCAEPI / MTE ('DD/MM/YYYY', 'YYYY-MM-DD' ou ISO) para 'YYYY-MM-DD'."""
    if not texto:
        return None
    s = str(texto).strip()[:10]
    m_br = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", s)
    if m_br:
        dia, mes, ano = m_br.groups()
        iso = f"{ano}-{mes}-{dia}"
        return iso if parse_data(iso) else None
    m_iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    if m_iso:
        return s if parse_data(s) else None
    return None


def inferir_categoria_e_unidade(nome_equipamento, descricao=""):
    """Deduz a Categoria e a Unidade ('par' ou 'un') a partir da nomenclatura NR-6 da Base CAEPI."""
    t = _sem_acento(f"{nome_equipamento or ''} {descricao or ''}")
    if any(p in t for p in ("luva", "manga", "dedeira", "braçadeira", "bracadeira")):
        return "Mãos", ("par" if "luva" in t else "un")
    if any(p in t for p in ("calcado", "botina", "bota", "sapato", "tenis")):
        return "Calçados", "par"
    if any(p in t for p in ("perneira", "avental", "vestimenta", "capa", "jardineira", "macacao", "calca", "camisa", "blusa", "colete")):
        return "Vestimenta", ("par" if "perneira" in t else "un")
    if any(p in t for p in ("oculos", "protetor facial", "mascara de solda", "visor", "lente")):
        return "Olhos e Face", "un"
    if any(p in t for p in ("auditivo", "auricular", "abafador", "plug")):
        return "Auditivo", "un"
    if any(p in t for p in ("respirador", "mascara", "pff", "filtro", "purificador")):
        return "Respiratório", ("par" if "filtro" in t else "un")
    if any(p in t for p in ("capacete", "capuz", "balaclava", "touca", "bone")):
        return "Cabeça", "un"
    if any(p in t for p in ("creme", "protetor solar", "repelente")):
        return "Proteção Cutânea", "un"
    if any(p in t for p in ("cinturao", "talabarte", "trava-quedas", "trava quedas")):
        return "Trabalho em Altura", "un"
    return "EPI", "un"


def obter_config_caepi(db):
    """Retorna a configuração de integração com a API_BaseCAEPI salva no banco."""
    padrao = {
        "api_url": os.environ.get("CAEPI_API_URL", "").strip(),
        "horario_atualizacao": "20:00",
        "ultima_sincronizacao": None,
        "ultimo_resultado": None,
    }
    linha = db.execute("SELECT valor FROM configuracoes WHERE chave = 'caepi_config'").fetchone()
    if linha and linha["valor"]:
        try:
            salvo = json.loads(linha["valor"])
            if isinstance(salvo, dict):
                for k in padrao:
                    if k in salvo and salvo[k] is not None:
                        padrao[k] = salvo[k]
        except ValueError:
            pass
    if not padrao["api_url"] and os.environ.get("CAEPI_API_URL"):
        padrao["api_url"] = os.environ["CAEPI_API_URL"].strip()
    return padrao


def salvar_config_caepi(db, **novos_campos):
    """Atualiza a configuração da integração com a API_BaseCAEPI na tabela configuracoes."""
    cfg = obter_config_caepi(db)
    cfg.update(novos_campos)
    db.execute(
        "INSERT INTO configuracoes (chave, valor) VALUES ('caepi_config', ?) "
        "ON CONFLICT (chave) DO UPDATE SET valor = excluded.valor",
        (json.dumps(cfg, ensure_ascii=False),),
    )
    return cfg


def _normalizar_payload_api(ca_num, payload):
    """Interpreta o JSON retornado pela API_BaseCAEPI (JoaoAugustoMV/API_BaseCAEPI) ou formato equivalente."""
    if isinstance(payload, dict):
        # Caso venha envelopado em {"data": [...]} ou {"resultado": {...}}
        for ch_env in ("data", "resultado", "ca", "registro", "items", "registros"):
            if ch_env in payload and isinstance(payload[ch_env], (dict, list)):
                payload = payload[ch_env]
                break
        else:
            # Caso seja um dicionário indexado pelo número ou índice {"0": {...}}
            if payload and all(isinstance(v, dict) for v in payload.values()):
                payload = list(payload.values())

    if isinstance(payload, list):
        if not payload:
            return None
        # No API_BaseCAEPI, um CA pode ter histórico de renovações; escolhemos o registro mais recente
        melhor = payload[-1]
        melhor_data = ""
        for item in payload:
            if not isinstance(item, dict):
                continue
            dt = converter_data_caepi(
                item.get("DataDeValidade")
                or item.get("DataValidade")
                or item.get("data_validade")
                or item.get("validade_ca")
                or item.get("validade")
            ) or ""
            if dt >= melhor_data:
                melhor_data = dt
                melhor = item
        payload = melhor

    if not isinstance(payload, dict):
        return None

    def _pegar(*chaves):
        for c in chaves:
            for k_real, v_real in payload.items():
                if k_real.lower().replace("_", "") == c.lower().replace("_", "") and v_real is not None:
                    txt = str(v_real).strip()
                    if txt and txt.lower() not in ("nan", "none", "null"):
                        return txt
        return ""

    validade_iso = converter_data_caepi(
        _pegar("DataDeValidade", "DataValidade", "validade_ca", "validade")
    )
    situacao = _pegar("Situacao", "status") or (
        "VENCIDO" if validade_iso and parse_data(validade_iso) < hoje() else "VÁLIDO"
    )
    nome_eq = _pegar("NomeEquipamento", "equipamento", "nome_equipamento", "nome")
    descricao = _pegar("DescricaoEquipamento", "descricao", "Descricao")
    fabricante = _pegar("RazaoSocial", "Fabricante", "razao_social")
    cnpj_fab = _pegar("CNPJ", "cnpj_fabricante")
    referencia = _pegar("Referencia", "MarcaCA", "modelo")

    if not (validade_iso or nome_eq or descricao):
        return None

    cat_sug, un_sug = inferir_categoria_e_unidade(nome_eq, descricao)
    categoria = _pegar("categoria") or cat_sug
    unidade = _pegar("unidade") or un_sug
    nome_sugerido = _pegar("nome_sugerido")
    if not nome_sugerido:
        if nome_eq and referencia and referencia.lower() not in nome_eq.lower():
            nome_sugerido = f"{nome_eq} ({referencia})"
        else:
            nome_sugerido = nome_eq or referencia or f"EPI CA {ca_num}"

    return {
        "ca": ca_num,
        "validade_ca": validade_iso,
        "situacao": situacao.upper(),
        "nome_equipamento": nome_eq or nome_sugerido,
        "nome_sugerido": nome_sugerido[:180],
        "descricao": descricao[:1000],
        "categoria": categoria,
        "unidade": unidade,
        "fabricante": fabricante[:200],
        "cnpj_fabricante": cnpj_fab[:30],
        "referencia": referencia[:160],
    }


def consultar_api_externa(url_base, numero_ca, timeout=5):
    """Consulta uma instância da API_BaseCAEPI (JoaoAugustoMV/API_BaseCAEPI)."""
    ca = normalizar_ca(numero_ca)
    url_base = (url_base or "").strip().rstrip("/")
    if not ca or not url_base:
        return None

    if "{ca}" in url_base:
        rotas_tentativa = [url_base.format(ca=ca)]
    else:
        rotas_tentativa = [
            f"{url_base}/ca/{ca}",
            f"{url_base}/consultar-ca/{ca}",
            f"{url_base}/consultar_ca/{ca}",
            f"{url_base}/api/ca/{ca}",
            f"{url_base}/?ca={ca}",
        ]

    for url in rotas_tentativa:
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "EstoqueEPI/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    continue
                bruto = resp.read().decode("utf-8", errors="replace")
                dados = json.loads(bruto)
                normalizado = _normalizar_payload_api(ca, dados)
                if normalizado:
                    normalizado["fonte"] = "API_BaseCAEPI"
                    return normalizado
        except Exception:
            continue
    return None


def salvar_registro_caepi(db, reg):
    """Grava ou atualiza um registro de CA na tabela local `base_caepi`."""
    ca = normalizar_ca(reg.get("ca"))
    if not ca:
        return
    db.execute(
        """INSERT INTO base_caepi (ca, validade_ca, situacao, nome_equipamento, descricao,
                                   categoria, unidade, fabricante, cnpj_fabricante, referencia, atualizado_em)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
           ON CONFLICT (ca) DO UPDATE SET
               validade_ca = excluded.validade_ca,
               situacao = excluded.situacao,
               nome_equipamento = excluded.nome_equipamento,
               descricao = excluded.descricao,
               categoria = excluded.categoria,
               unidade = excluded.unidade,
               fabricante = excluded.fabricante,
               cnpj_fabricante = excluded.cnpj_fabricante,
               referencia = excluded.referencia,
               atualizado_em = datetime('now', 'localtime')""",
        (
            ca,
            reg.get("validade_ca"),
            reg.get("situacao"),
            reg.get("nome_equipamento"),
            reg.get("descricao"),
            reg.get("categoria"),
            reg.get("unidade") or "un",
            reg.get("fabricante"),
            reg.get("cnpj_fabricante"),
            reg.get("referencia"),
        ),
    )


def consultar_ca(db, numero_ca):
    """Busca todas as informações de um CA pelo número (API_BaseCAEPI -> Base local -> Semente)."""
    ca = normalizar_ca(numero_ca)
    if not ca:
        return None

    cfg = obter_config_caepi(db)
    # 1. Tenta consultar a API_BaseCAEPI externa se configurada
    if cfg.get("api_url"):
        externo = consultar_api_externa(cfg["api_url"], ca)
        if externo:
            salvar_registro_caepi(db, externo)
            return externo

    # 2. Tenta na tabela base_caepi do banco de dados
    linha = db.execute("SELECT * FROM base_caepi WHERE ca = ?", (ca,)).fetchone()
    if linha:
        d = dict(linha)
        nome_eq = d.get("nome_equipamento") or ""
        ref = d.get("referencia") or ""
        d["nome_sugerido"] = (
            f"{nome_eq} ({ref})" if nome_eq and ref and ref.lower() not in nome_eq.lower() else (nome_eq or f"EPI CA {ca}")
        )
        d["fonte"] = "Base CAEPI Local"
        return d

    # 3. Tenta no catálogo semente embutido
    if ca in CATALOGO_CA_SEMENTE:
        semente = dict(CATALOGO_CA_SEMENTE[ca])
        semente["ca"] = ca
        semente["fonte"] = "Base CAEPI Integrada"
        salvar_registro_caepi(db, semente)
        return semente

    # 4. Verifica se já existe algum EPI cadastrado no estoque com esse CA
    epi_local = db.execute(
        "SELECT nome, categoria, ca, validade_ca, unidade FROM epis WHERE ca = ? LIMIT 1", (ca,)
    ).fetchone()
    if epi_local:
        val = epi_local["validade_ca"]
        return {
            "ca": ca,
            "validade_ca": val,
            "situacao": "VENCIDO" if val and parse_data(val) and parse_data(val) < hoje() else "VÁLIDO",
            "nome_equipamento": epi_local["nome"],
            "nome_sugerido": epi_local["nome"],
            "descricao": "",
            "categoria": epi_local["categoria"] or "EPI",
            "unidade": epi_local["unidade"] or "un",
            "fabricante": "",
            "cnpj_fabricante": "",
            "referencia": "",
            "fonte": "Catálogo Interno",
        }

    return None


def importar_texto_tgg_caepi(db, conteudo_texto, apenas_cas=None):
    """Importa registros do arquivo oficial `tgg_export_caepi.txt` (separado por `|`) do MTE / API_BaseCAEPI."""
    linhas = (conteudo_texto or "").splitlines()
    if not linhas:
        raise ErroValidacao("Arquivo da base CAEPI vazio.")

    cab = [c.strip().lstrip("#") for c in linhas[0].split("|")]
    idx_map = {c.lower(): i for i, c in enumerate(cab)}
    if "nrregistroca" not in idx_map:
        # Layout posicional padrão do tgg_export_caepi.txt do MTE
        idx_map = {
            "nrregistroca": 0,
            "datadevalidade": 1,
            "situacao": 2,
            "cnpj": 4,
            "razaosocial": 5,
            "nomeequipamento": 7,
            "descricaoequipamento": 8,
            "marcaca": 9,
            "referencia": 10,
        }
        inicio = 0
    else:
        inicio = 1

    filtro_cas = {normalizar_ca(x) for x in apenas_cas if normalizar_ca(x)} if apenas_cas else None
    agrupados = {}

    def _col(partes, chave):
        pos = idx_map.get(chave)
        return partes[pos].strip() if pos is not None and pos < len(partes) else ""

    for linha in linhas[inicio:]:
        if "|" not in linha:
            continue
        partes = linha.split("|")
        ca = normalizar_ca(_col(partes, "nrregistroca"))
        if not ca:
            continue
        if filtro_cas is not None and ca not in filtro_cas:
            continue
        dt_iso = converter_data_caepi(_col(partes, "datadevalidade"))
        ant = agrupados.get(ca)
        if ant and (ant.get("validade_ca") or "") > (dt_iso or ""):
            continue
        nome_eq = _col(partes, "nomeequipamento")
        desc = _col(partes, "descricaoequipamento")
        cat_sug, un_sug = inferir_categoria_e_unidade(nome_eq, desc)
        agrupados[ca] = {
            "ca": ca,
            "validade_ca": dt_iso,
            "situacao": (_col(partes, "situacao") or "VÁLIDO").upper(),
            "nome_equipamento": nome_eq,
            "descricao": desc[:1000],
            "categoria": cat_sug,
            "unidade": un_sug,
            "fabricante": _col(partes, "razaosocial")[:200],
            "cnpj_fabricante": _col(partes, "cnpj")[:30],
            "referencia": (_col(partes, "referencia") or _col(partes, "marcaca"))[:160],
        }

    for reg in agrupados.values():
        salvar_registro_caepi(db, reg)

    atualizados = atualizar_epis_pela_base(db)
    salvar_config_caepi(
        db,
        ultima_sincronizacao=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ultimo_resultado=f"{len(agrupados)} CA(s) processado(s) da base CAEPI; {atualizados} EPI(s) atualizado(s).",
    )
    return len(agrupados), atualizados


def importar_arquivo_caepi(db, arquivo_bytes, nome_arquivo=""):
    """Aceita upload de `tgg_export_caepi.zip` ou `tgg_export_caepi.txt`."""
    if not arquivo_bytes:
        raise ErroValidacao("Arquivo vazio.")
    if (nome_arquivo or "").lower().endswith(".zip") or arquivo_bytes[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(arquivo_bytes)) as zf:
                nomes_txt = [n for n in zf.namelist() if n.lower().endswith((".txt", ".csv"))]
                if not nomes_txt:
                    raise ErroValidacao("O arquivo ZIP não contém nenhum arquivo .txt da base CAEPI.")
                bruto = zf.read(nomes_txt[0])
        except zipfile.BadZipFile as e:
            raise ErroValidacao("Arquivo ZIP inválido.") from e
    else:
        bruto = arquivo_bytes

    try:
        texto = bruto.decode("utf-8-sig")
    except UnicodeDecodeError:
        texto = bruto.decode("latin-1", errors="replace")
    return importar_texto_tgg_caepi(db, texto)


def atualizar_epis_pela_base(db):
    """Sincroniza a validade (e categoria se vazia) de todos os EPIs cadastrados no estoque usando a Base CAEPI."""
    epis_com_ca = db.execute(
        "SELECT id, nome, ca, validade_ca, categoria FROM epis WHERE ca IS NOT NULL AND TRIM(ca) != ''"
    ).fetchall()
    atualizados = 0
    for e in epis_com_ca:
        info = consultar_ca(db, e["ca"])
        if not info or not info.get("validade_ca"):
            continue
        nova_val = info["validade_ca"]
        nova_cat = e["categoria"] or info.get("categoria")
        if nova_val != e["validade_ca"] or nova_cat != e["categoria"]:
            db.execute(
                "UPDATE epis SET validade_ca = ?, categoria = COALESCE(categoria, ?) WHERE id = ?",
                (nova_val, nova_cat, e["id"]),
            )
            atualizados += 1
    return atualizados


def sincronizar_cas_estoque(db, tentar_ftp_mte=False):
    """Executa a sincronização dos CAs cadastrados (como a rotina das 20:00 da API_BaseCAEPI)."""
    # Garante que o catálogo semente esteja carregado na base_caepi
    for ca_num, dados_sem in CATALOGO_CA_SEMENTE.items():
        existente = db.execute("SELECT 1 FROM base_caepi WHERE ca = ?", (ca_num,)).fetchone()
        if not existente:
            reg = dict(dados_sem)
            reg["ca"] = ca_num
            salvar_registro_caepi(db, reg)

    if tentar_ftp_mte:
        try:
            cas_ativos = [
                r["ca"]
                for r in db.execute("SELECT DISTINCT ca FROM epis WHERE ca IS NOT NULL AND TRIM(ca) != ''")
            ]
            req = urllib.request.Request(URL_FTP_MTE_CAEPI, headers={"User-Agent": "EstoqueEPI/1.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                bruto_zip = resp.read()
                with zipfile.ZipFile(io.BytesIO(bruto_zip)) as zf:
                    nomes_txt = [n for n in zf.namelist() if n.lower().endswith(".txt")]
                    if nomes_txt:
                        texto = zf.read(nomes_txt[0]).decode("latin-1", errors="replace")
                        return importar_texto_tgg_caepi(db, texto, apenas_cas=cas_ativos or None)
        except Exception:
            pass

    atualizados = atualizar_epis_pela_base(db)
    total_base = db.execute("SELECT COUNT(*) FROM base_caepi").fetchone()[0]
    salvar_config_caepi(
        db,
        ultima_sincronizacao=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ultimo_resultado=f"Base CAEPI ativa ({total_base} CAs em cache) · {atualizados} EPI(s) atualizado(s).",
    )
    return total_base, atualizados


def verificar_sincronizacao_diaria_20h(db):
    """Verifica se já passou da janela diária das 20:00 sem sincronizar e atualiza automaticamente."""
    try:
        cfg = obter_config_caepi(db)
        agora = datetime.now()
        alvo_hoje_20h = agora.replace(hour=20, minute=0, second=0, microsecond=0)
        marco_esperado = alvo_hoje_20h if agora >= alvo_hoje_20h else (alvo_hoje_20h - timedelta(days=1))
        ult_str = cfg.get("ultima_sincronizacao")
        precisa = True
        if ult_str:
            try:
                ult_dt = datetime.strptime(ult_str[:19], "%Y-%m-%d %H:%M:%S")
                if ult_dt >= marco_esperado:
                    precisa = False
            except ValueError:
                precisa = True
        if precisa:
            sincronizar_cas_estoque(db, tentar_ftp_mte=False)
    except Exception:
        pass
