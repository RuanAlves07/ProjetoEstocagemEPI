"""Importação de colaboradores a partir de um CSV (ex.: exportação do RH Gestor).

Colunas reconhecidas (o cabeçalho pode ter acento, maiúsculas e o prefixo "Nome"):
Matrícula, Nome, Celular/WhatsApp, Setor, Cargo/Função, Data Nascimento e CPF.
Qualquer outra coluna (Nome Empresa, Regional...) é ignorada.

Cada linha encontra o colaborador já cadastrado pela matrícula, depois pelo CPF e por último pelo
nome. Encontrado, só os campos preenchidos no arquivo são atualizados (célula vazia não apaga nada);
não encontrado, o colaborador é criado.
"""
import csv
import re
import unicodedata
from datetime import date

from . import whatsapp

TAMANHO_MAXIMO = 2 * 1024 * 1024

CAMPOS = ("matricula", "nome", "setor", "funcao", "whatsapp", "cpf", "data_nascimento")

ROTULOS = {
    "matricula": "Matrícula",
    "nome": "Nome",
    "setor": "Setor",
    "funcao": "Cargo",
    "whatsapp": "WhatsApp",
    "cpf": "CPF",
    "data_nascimento": "Nascimento",
}

_SINONIMOS = {
    "matricula": ("matricula", "matricula colaborador", "registro", "codigo", "cod"),
    "nome": ("nome", "nome colaborador", "nome do colaborador", "colaborador", "nome completo"),
    "whatsapp": ("celular", "whatsapp", "telefone", "telefone celular", "fone", "contato"),
    "setor": ("setor", "nome setor", "nome do setor", "departamento"),
    "funcao": ("cargo", "nome cargo", "nome do cargo", "funcao", "cargo funcao"),
    "data_nascimento": ("data nascimento", "data de nascimento", "nascimento", "dt nascimento"),
    "cpf": ("cpf", "numero cpf", "cpf colaborador"),
}
_COLUNA_POR_NOME = {nome: campo for campo, nomes in _SINONIMOS.items() for nome in nomes}


_MESMO = {"matrícula": "A mesma matrícula", "CPF": "O mesmo CPF", "nome": "O mesmo nome"}


class ErroArquivo(ValueError):
    """O arquivo não pode ser lido como uma lista de colaboradores."""


def _normalizar(texto):
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", texto.lower()).split())


def decodificar(conteudo):
    """Bytes do upload -> texto. Aceita UTF-8 (com ou sem BOM) e o padrão do Excel no Windows."""
    if len(conteudo) > TAMANHO_MAXIMO:
        raise ErroArquivo("O arquivo passa de 2 MB. Confira se é mesmo a lista de colaboradores.")
    try:
        return conteudo.decode("utf-8-sig")
    except UnicodeDecodeError:
        return conteudo.decode("cp1252", errors="replace")


def _data(texto):
    texto = (texto or "").strip()
    if not texto:
        return None
    m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", texto)
    try:
        if m:
            return date(int(m[3]), int(m[2]), int(m[1]))
        return date.fromisoformat(texto)
    except ValueError:
        return False


def ler_linhas(texto):
    """Devolve (linhas, colunas_ignoradas). Cada linha é {"linha": n, "dados": {...}, "avisos": [...], "erro": str|None}."""
    texto = texto.lstrip("﻿")
    primeira = texto.split("\n", 1)[0]
    separador = max(";,\t", key=primeira.count)
    leitor = csv.reader(texto.splitlines(), delimiter=separador)
    cabecalho = next(leitor, None)
    if not cabecalho:
        raise ErroArquivo("O arquivo está vazio.")

    mapa, ignoradas = {}, []
    for i, nome in enumerate(cabecalho):
        campo = _COLUNA_POR_NOME.get(_normalizar(nome))
        if campo and campo not in mapa.values():
            mapa[i] = campo
        elif nome.strip():
            ignoradas.append(nome.strip())
    if "nome" not in mapa.values():
        raise ErroArquivo("Não achei a coluna “Nome” no cabeçalho do arquivo. "
                          "A primeira linha precisa ter os títulos das colunas.")

    linhas = []
    for n, valores in enumerate(leitor, start=2):
        bruto = {campo: (valores[i].strip() if i < len(valores) else "") for i, campo in mapa.items()}
        if not any(bruto.values()):
            continue  # linha vazia (ou só com colunas ignoradas)
        dados = {c: None for c in CAMPOS}
        avisos = []
        dados["nome"] = " ".join(bruto.get("nome", "").split()) or None
        dados["matricula"] = bruto.get("matricula") or None
        dados["setor"] = bruto.get("setor") or None
        dados["funcao"] = bruto.get("funcao") or None

        zap = bruto.get("whatsapp", "")
        if zap:
            dados["whatsapp"] = whatsapp.normalizar_numero(zap)
            if not dados["whatsapp"]:
                avisos.append(f"Celular “{zap}” não tem DDD + número; ficou sem WhatsApp.")

        cpf = re.sub(r"\D", "", bruto.get("cpf", ""))
        if cpf:
            if whatsapp.cpf_valido(cpf):
                dados["cpf"] = cpf
            else:
                avisos.append(f"CPF “{bruto['cpf']}” é inválido; ficou sem CPF.")

        nasc = _data(bruto.get("data_nascimento"))
        if nasc:
            dados["data_nascimento"] = nasc.isoformat()
        elif nasc is False:
            avisos.append(f"Data de nascimento “{bruto['data_nascimento']}” inválida; ficou em branco.")

        linhas.append({"linha": n, "dados": dados, "avisos": avisos,
                       "erro": None if dados["nome"] else "Linha sem nome; não foi importada."})
    if not linhas:
        raise ErroArquivo("O arquivo não tem nenhum colaborador abaixo do cabeçalho.")
    return linhas, ignoradas


def analisar(db, texto):
    """Lê o CSV e decide, linha a linha, se cria, atualiza ou ignora. Não grava nada."""
    linhas, ignoradas = ler_linhas(texto)
    existentes = [dict(r) for r in db.execute("SELECT * FROM colaboradores")]
    por_matricula = {str(c["matricula"]).strip(): c for c in existentes if c["matricula"]}
    por_cpf = {c["cpf"]: c for c in existentes if c["cpf"]}
    por_nome = {}
    for c in existentes:
        por_nome.setdefault(_normalizar(c["nome"]), []).append(c)

    vistos = {}  # chave (matrícula/CPF/nome) -> número da linha no arquivo, para achar repetidos
    usados = set()  # ids de colaboradores já atribuídos a uma linha
    for ln in linhas:
        ln.update(acao="erro" if ln["erro"] else None, colab=None, mudancas=[])
        if ln["erro"]:
            continue
        d = ln["dados"]
        chaves = [("matrícula", d["matricula"]), ("CPF", d["cpf"])]
        if not d["matricula"] and not d["cpf"]:
            chaves.append(("nome", _normalizar(d["nome"])))
        repetida = next((f"{_MESMO[rot]} está na linha {vistos[(rot, v)]}" for rot, v in chaves
                         if v and (rot, v) in vistos), None)
        if repetida:
            ln.update(acao="erro", erro=f"{repetida}; esta linha não foi importada.")
            continue
        for rot, v in chaves:
            if v:
                vistos[(rot, v)] = ln["linha"]

        colab = None
        if d["matricula"]:
            colab = por_matricula.get(d["matricula"])
        if colab is None and d["cpf"]:
            colab = por_cpf.get(d["cpf"])
        if colab is None:
            mesmos = [c for c in por_nome.get(_normalizar(d["nome"]), []) if c["id"] not in usados]
            if len(mesmos) == 1:
                colab = mesmos[0]
        if colab is not None and colab["id"] in usados:
            ln.update(acao="erro", erro="Outra linha do arquivo já corresponde a este colaborador; "
                                        "esta linha não foi importada.")
            continue

        if colab is None:
            ln["acao"] = "novo"
            continue

        usados.add(colab["id"])
        ln["colab"] = colab
        for campo in CAMPOS:
            novo = d[campo]
            if not novo or str(colab[campo] or "") == novo:
                continue
            if campo == "nome" and _normalizar(colab["nome"]) == _normalizar(novo):
                continue  # só muda maiúsculas/acentos: mantém o nome como está
            if campo == "matricula" and novo in por_matricula and por_matricula[novo]["id"] != colab["id"]:
                ln["avisos"].append(f"Matrícula {novo} já é de outro colaborador; a matrícula não foi trocada.")
                continue
            if campo == "cpf" and novo in por_cpf and por_cpf[novo]["id"] != colab["id"]:
                ln["avisos"].append("CPF já é de outro colaborador; o CPF não foi trocado.")
                continue
            ln["mudancas"].append((campo, colab[campo], novo))
        ln["acao"] = "atualizar" if ln["mudancas"] else "igual"

    resumo = {k: sum(1 for ln in linhas if ln["acao"] == k) for k in ("novo", "atualizar", "igual", "erro")}
    resumo["avisos"] = sum(1 for ln in linhas if ln["avisos"])
    return {"linhas": linhas, "ignoradas": ignoradas, "resumo": resumo}


def aplicar(db, analise):
    """Grava o resultado de analisar(). Devolve o resumo do que foi feito."""
    for ln in analise["linhas"]:
        if ln["acao"] == "novo":
            db.execute(
                "INSERT INTO colaboradores (nome, matricula, setor, funcao, whatsapp, cpf, data_nascimento) "
                "VALUES (:nome, :matricula, :setor, :funcao, :whatsapp, :cpf, :data_nascimento)", ln["dados"])
        elif ln["acao"] == "atualizar":
            sets = ", ".join(f"{campo} = ?" for campo, _, _ in ln["mudancas"])
            db.execute(f"UPDATE colaboradores SET {sets} WHERE id = ?",
                       [novo for _, _, novo in ln["mudancas"]] + [ln["colab"]["id"]])
    return analise["resumo"]
