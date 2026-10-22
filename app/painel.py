"""Painel inicial e relatórios gerenciais e de conferência de estocagem por tamanho."""
from datetime import timedelta
import csv
import io
import re

from flask import Blueprint, render_template, request

from . import estoque, pedidos
from .cadastros import _csv_response
from .db import get_db
from .movimentos import consultar_movimentos
from .util import hoje, limpar, parse_data, to_int

bp = Blueprint("painel", __name__)

ROTULOS_SUBTIPO = {
    "sapatao": "Sapatão",
    "botina": "Botina",
    "bota": "Bota",
    "camiseta": "Camiseta",
    "calca": "Calça",
}


def _subtipo_item(nome):
    tc = pedidos.tipo_calcado(nome)
    if tc:
        return tc
    tu = pedidos.tipo_uniforme(nome)
    if tu:
        return tu
    return None


def _extrair_tamanho_texto(texto, subtipo=None):
    """Extrai o tamanho de '(Tam. 40)' ou do final do próprio nome do EPI (ex.: 'Bota 40', 'Camiseta M')."""
    if not texto:
        return ""
    m = re.search(r"\(Tam\.\s*([^)]+)\)", texto, flags=re.I)
    if m:
        return m.group(1).strip().upper()
    if subtipo in ("sapatao", "botina", "bota"):
        m_num = re.search(r"\b(3[3-9]|4[0-8])\b", texto)
        if m_num:
            return m_num.group(1)
    elif subtipo in ("camiseta", "calca"):
        m_let = re.search(r"\b(PP|P|M|G|GG|XG|G1|G2|G3|G4|G5)\s*$", texto.strip(), flags=re.I)
        if m_let:
            return m_let.group(1).upper()
    return ""


def _ordem_tamanho(tam):
    """Ordena tamanhos numéricos (33..48) antes e tamanhos por letra na sequência M, G, GG, G1..G5."""
    t = (tam or "").strip().upper()
    if t.isdigit():
        return (0, int(t), t)
    ordem_letras = {"PP": 1, "P": 2, "M": 3, "G": 4, "GG": 5, "XG": 6, "G1": 7, "G2": 8, "G3": 9, "G4": 10, "G5": 11}
    if t in ordem_letras:
        return (1, ordem_letras[t], t)
    if t in ("", "NÃO INFORMADO", "—"):
        return (9, 999, t)
    return (2, 0, t)


def _montar_relatorio_tamanhos(db, de_iso, ate_iso, epis_saldo, filtro_subtipo="", filtro_epi_id=None, filtro_tam=""):
    """Agrupa o uso (funcionários e quantidades) de Botas, Botinas, Sapatões, Camisetas e Calças por tamanho."""
    mapa_epis = {e["id"]: e for e in epis_saldo}

    # 1. Tamanhos salvos no perfil dos colaboradores
    tamanhos_perfil = {}
    for r in db.execute("SELECT colaborador_id, chave, tamanho FROM colaborador_tamanhos"):
        tamanhos_perfil[(r["colaborador_id"], r["chave"].lower())] = (r["tamanho"] or "").strip().upper()

    # Colaboradores ativos e cargos
    colabs = {
        r["id"]: dict(r)
        for r in db.execute("SELECT id, nome, setor, funcao, ativo FROM colaboradores")
    }

    # Tamanhos registrados nos itens de pedidos
    tamanhos_pedido_item = {}
    for r in db.execute("SELECT pedido_id, epi_id, texto_original FROM pedido_itens WHERE epi_id IS NOT NULL AND ignorado = 0"):
        epi_info = mapa_epis.get(r["epi_id"])
        sub = _subtipo_item(epi_info["nome"]) if epi_info else None
        tam = _extrair_tamanho_texto(r["texto_original"], sub)
        if tam:
            tamanhos_pedido_item[(r["pedido_id"], r["epi_id"])] = tam

    def _descobrir_tamanho(epi_id, colab_id=None, pedido_id=None, texto_orig=None):
        epi_info = mapa_epis.get(epi_id)
        if not epi_info:
            return ""
        if epi_info.get("tamanho"):
            return epi_info["tamanho"]
        sub = _subtipo_item(epi_info["nome"])
        if texto_orig:
            t = _extrair_tamanho_texto(texto_orig, sub)
            if t:
                return t
        if pedido_id and (pedido_id, epi_id) in tamanhos_pedido_item:
            return tamanhos_pedido_item[(pedido_id, epi_id)]
        if colab_id:
            t = tamanhos_perfil.get((colab_id, f"epi:{epi_id}")) or (
                tamanhos_perfil.get((colab_id, f"tipo:{sub}")) if sub else ""
            )
            if t:
                return t
        return _extrair_tamanho_texto(epi_info["nome"], sub)

    # Estrutura agregadora por (epi_id, tamanho)
    # Cada variação (Produto + Tamanho) funciona como um item separado para conferência de estocagem
    grupos = {}

    def _garantir_grupo(epi_id, tam):
        epi_info = mapa_epis.get(epi_id)
        if not epi_info:
            return None
        sub = _subtipo_item(epi_info["nome"])
        if not sub:
            return None
        tam_limpo = (tam or "").strip().upper() or "Não informado"
        chave = (epi_id, tam_limpo)
        if chave not in grupos:
            grupos[chave] = {
                "epi_id": epi_id,
                "epi_nome": epi_info["nome"],
                "categoria": epi_info["categoria"] or "",
                "unidade": epi_info["unidade"] or "un",
                "saldo_geral_epi": epi_info["saldo"],
                "subtipo": sub,
                "subtipo_rotulo": ROTULOS_SUBTIPO.get(sub, sub.capitalize()),
                "tamanho": tam_limpo,
                "qtd_entregue": 0,
                "qtd_em_pedidos": 0,
                "funcionarios_usaram": {},   # colab_key -> {"id": id, "nome": nome, "setor": setor, "qtd": qtd}
                "funcionarios_perfil": {},   # colab_id -> nome (quem usa por padrão esse tamanho no perfil)
            }
        return grupos[chave]

    # 2. Contabiliza saídas/entregas reais de estoque no período (movimentacoes tipo='saida')
    saidas = db.execute(
        """SELECT m.epi_id, m.quantidade, m.colaborador_id, m.pedido_id, m.data,
                  c.nome AS colab_nome, c.setor AS colab_setor
           FROM movimentacoes m
           LEFT JOIN colaboradores c ON c.id = m.colaborador_id
           WHERE m.cancelada = 0 AND m.tipo = 'saida' AND m.data BETWEEN ? AND ?""",
        (de_iso, ate_iso),
    ).fetchall()
    for m in saidas:
        epi_info = mapa_epis.get(m["epi_id"])
        if not epi_info or not _subtipo_item(epi_info["nome"]):
            continue
        tam = _descobrir_tamanho(m["epi_id"], colab_id=m["colaborador_id"], pedido_id=m["pedido_id"])
        g = _garantir_grupo(m["epi_id"], tam)
        if not g:
            continue
        g["qtd_entregue"] += m["quantidade"]
        chave_c = m["colaborador_id"] or f"avulso:{m['colab_nome'] or 'Sem identificação'}"
        reg_c = g["funcionarios_usaram"].setdefault(
            chave_c,
            {
                "id": m["colaborador_id"],
                "nome": m["colab_nome"] or "Sem colaborador vinculado",
                "setor": m["colab_setor"] or "",
                "qtd": 0,
            },
        )
        reg_c["qtd"] += m["quantidade"]

    # 3. Contabiliza pedidos abertos (ainda não baixados no estoque) dentro do período
    itens_abertos = db.execute(
        """SELECT i.epi_id, i.quantidade, i.texto_original, p.id AS pedido_id,
                  p.colaborador_id, p.solicitante, p.local_destino
           FROM pedido_itens i
           JOIN pedidos p ON p.id = i.pedido_id
           WHERE i.ignorado = 0 AND i.epi_id IS NOT NULL
             AND p.status IN ('novo', 'pendente', 'em_separacao')
             AND substr(p.recebido_em, 1, 10) BETWEEN ? AND ?""",
        (de_iso, ate_iso),
    ).fetchall()
    for it in itens_abertos:
        epi_info = mapa_epis.get(it["epi_id"])
        if not epi_info or not _subtipo_item(epi_info["nome"]):
            continue
        tam = _descobrir_tamanho(
            it["epi_id"], colab_id=it["colaborador_id"], pedido_id=it["pedido_id"], texto_orig=it["texto_original"]
        )
        g = _garantir_grupo(it["epi_id"], tam)
        if not g:
            continue
        g["qtd_em_pedidos"] += it["quantidade"]
        chave_c = it["colaborador_id"] or f"sol:{it['solicitante']}"
        colab_obj = colabs.get(it["colaborador_id"])
        reg_c = g["funcionarios_usaram"].setdefault(
            chave_c,
            {
                "id": it["colaborador_id"],
                "nome": colab_obj["nome"] if colab_obj else it["solicitante"],
                "setor": (colab_obj["setor"] if colab_obj else None) or it["local_destino"] or "",
                "qtd": 0,
            },
        )
        reg_c["qtd"] += it["quantidade"]

    # 4. Contabiliza quantos colaboradores ativos têm esse produto + tamanho configurado no seu perfil/cargo
    padrao_cargo = {}
    for r in db.execute("SELECT cargo, epi_id FROM cargo_epis_padrao"):
        padrao_cargo.setdefault((r["cargo"] or "").strip().casefold(), set()).add(r["epi_id"])

    for cid, c in colabs.items():
        if not c["ativo"]:
            continue
        cargo_c = (c["funcao"] or "").strip().casefold()
        epis_do_colab = padrao_cargo.get(cargo_c, set())
        for eid in epis_do_colab:
            epi_info = mapa_epis.get(eid)
            if not epi_info:
                continue
            sub = _subtipo_item(epi_info["nome"])
            if not sub:
                continue
            tam = tamanhos_perfil.get((cid, f"epi:{eid}")) or tamanhos_perfil.get((cid, f"tipo:{sub}"))
            if tam:
                g = _garantir_grupo(eid, tam)
                if g:
                    g["funcionarios_perfil"][cid] = c["nome"]

    # Monta lista final ordenada e aplica filtros opcionais
    linhas = []
    resumo_por_subtipo = {k: {} for k in ROTULOS_SUBTIPO}
    tamanhos_disponiveis = set()

    for g in grupos.values():
        g["total_unidades"] = g["qtd_entregue"] + g["qtd_em_pedidos"]
        g["qtd_funcionarios_usaram"] = len(g["funcionarios_usaram"])
        g["qtd_funcionarios_perfil"] = len(g["funcionarios_perfil"])
        g["lista_funcionarios"] = sorted(
            g["funcionarios_usaram"].values(), key=lambda x: (-x["qtd"], x["nome"].casefold())
        )
        tamanhos_disponiveis.add(g["tamanho"])

        # Alimenta a grade consolidada de tamanhos por tipo (Sapatão, Botina, Bota, Camiseta, Calça)
        st = g["subtipo"]
        tam_k = g["tamanho"]
        if tam_k != "Não informado":
            atual = resumo_por_subtipo[st].setdefault(
                tam_k, {"tamanho": tam_k, "unid_usadas": 0, "funcionarios_usaram": set(), "funcionarios_perfil": set()}
            )
            atual["unid_usadas"] += g["total_unidades"]
            atual["funcionarios_usaram"].update(g["funcionarios_usaram"].keys())
            atual["funcionarios_perfil"].update(g["funcionarios_perfil"].keys())

        if filtro_subtipo and g["subtipo"] != filtro_subtipo:
            continue
        if filtro_epi_id and g["epi_id"] != filtro_epi_id:
            continue
        if filtro_tam and g["tamanho"].casefold() != filtro_tam.casefold():
            continue
        linhas.append(g)

    linhas.sort(
        key=lambda x: (
            list(ROTULOS_SUBTIPO.keys()).index(x["subtipo"]) if x["subtipo"] in ROTULOS_SUBTIPO else 99,
            x["epi_nome"].casefold(),
            _ordem_tamanho(x["tamanho"]),
        )
    )

    grades_subtipo = []
    for ch_sub, rot_sub in ROTULOS_SUBTIPO.items():
        itens_grade = []
        for tam_k, info_t in sorted(resumo_por_subtipo[ch_sub].items(), key=lambda par: _ordem_tamanho(par[0])):
            itens_grade.append({
                "tamanho": tam_k,
                "unid_usadas": info_t["unid_usadas"],
                "qtd_usaram": len(info_t["funcionarios_usaram"]),
                "qtd_perfil": len(info_t["funcionarios_perfil"]),
            })
        if itens_grade:
            grades_subtipo.append({"chave": ch_sub, "rotulo": rot_sub, "tamanhos": itens_grade})

    tamanhos_ordenados = sorted(tamanhos_disponiveis, key=_ordem_tamanho)
    return linhas, grades_subtipo, tamanhos_ordenados


@bp.get("/")
def inicio():
    db = get_db()
    epis = estoque.epis_com_saldo(db)
    ordem = {"zerado": 0, "repor": 1}
    repor = sorted((e for e in epis if e["status"] != "ok"),
                   key=lambda e: (ordem[e["status"]], e["cobertura_dias"] if e["cobertura_dias"] is not None else 1e9))
    ca_alertas = sorted((e for e in epis if e["ca_status"] in ("vencido", "vencendo")),
                        key=lambda e: e["validade_ca"])
    mais_saem = sorted((e for e in epis if e["consumo"] > 0), key=lambda e: -e["consumo"])[:8]

    inicio_mes = hoje().replace(day=1).isoformat()
    totais = {r["tipo"]: r["total"] for r in db.execute(
        """SELECT tipo, SUM(quantidade) AS total FROM movimentacoes
           WHERE cancelada = 0 AND data >= ? GROUP BY tipo""", (inicio_mes,))}
    pedidos_abertos = pedidos.carregar(db, status=pedidos.ABERTOS, limite=6)
    colaboradores_ativos = db.execute("SELECT COUNT(*) FROM colaboradores WHERE ativo = 1").fetchone()[0]
    return render_template(
        "painel.html", epis=epis, repor=repor, ca_alertas=ca_alertas, mais_saem=mais_saem,
        total_entradas=totais.get("entrada", 0), total_entregas=totais.get("saida", 0),
        pedidos_abertos=pedidos_abertos, colaboradores_ativos=colaboradores_ativos,
        STATUS_PEDIDO=pedidos.STATUS,
        ultimas=consultar_movimentos(db, {}, limite=10), janela=estoque.JANELA_CONSUMO_DIAS,
    )


@bp.get("/relatorios")
def relatorios():
    db = get_db()
    ate = parse_data(request.args.get("ate")) or hoje()
    de = parse_data(request.args.get("de")) or (ate - timedelta(days=89))
    if de > ate:
        de, ate = ate, de
    p = (de.isoformat(), ate.isoformat())

    filtro_subtipo = limpar(request.args.get("subtipo")) or ""
    filtro_epi_id = to_int(request.args.get("epi_id"))
    filtro_tam = limpar(request.args.get("tamanho")) or ""
    filtro_categoria = limpar(request.args.get("categoria")) or ""

    epis_saldo = estoque.epis_com_saldo(db, somente_ativos=False)
    linhas_tamanhos, grades_subtipo, tamanhos_disponiveis = _montar_relatorio_tamanhos(
        db, p[0], p[1], epis_saldo,
        filtro_subtipo=filtro_subtipo, filtro_epi_id=filtro_epi_id, filtro_tam=filtro_tam,
    )

    # Conferência completa de estocagem (todos os EPIs com saldo atual, entradas, saídas e qtd de funcionários)
    movs_por_epi = {
        r["epi_id"]: r
        for r in db.execute(
            """SELECT epi_id,
                      SUM(CASE WHEN tipo = 'entrada' THEN quantidade ELSE 0 END) AS entradas,
                      SUM(CASE WHEN tipo = 'saida' THEN quantidade ELSE 0 END) AS entregas,
                      SUM(CASE WHEN tipo = 'baixa' THEN quantidade ELSE 0 END) AS baixas,
                      COUNT(DISTINCT CASE WHEN tipo = 'saida' THEN colaborador_id END) AS funcionarios
               FROM movimentacoes
               WHERE cancelada = 0 AND data BETWEEN ? AND ?
               GROUP BY epi_id""",
            p,
        )
    }
    conferencia_estoque = []
    categorias_estoque = sorted({(e["categoria"] or "Sem categoria") for e in epis_saldo}, key=str.casefold)
    for e in epis_saldo:
        if not e["ativo"] and e["saldo"] == 0:
            continue
        cat_e = e["categoria"] or "Sem categoria"
        if filtro_categoria and cat_e.casefold() != filtro_categoria.casefold():
            continue
        if filtro_epi_id and e["id"] != filtro_epi_id:
            continue
        mv = movs_por_epi.get(e["id"])
        item_conf = dict(e)
        item_conf["entradas_periodo"] = mv["entradas"] if mv else 0
        item_conf["entregas_periodo"] = mv["entregas"] if mv else 0
        item_conf["baixas_periodo"] = mv["baixas"] if mv else 0
        item_conf["funcionarios_periodo"] = mv["funcionarios"] if mv else 0
        conferencia_estoque.append(item_conf)

    ranking = db.execute(
        """SELECT e.id, e.nome, e.unidade,
                  SUM(CASE WHEN m.tipo = 'entrada' THEN m.quantidade ELSE 0 END) AS entradas,
                  SUM(CASE WHEN m.tipo = 'saida' THEN m.quantidade ELSE 0 END) AS entregas,
                  SUM(CASE WHEN m.tipo = 'baixa' THEN m.quantidade ELSE 0 END) AS baixas,
                  SUM(CASE WHEN m.tipo IN ('saida', 'baixa') THEN m.quantidade ELSE 0 END) AS total_saidas
           FROM movimentacoes m JOIN epis e ON e.id = m.epi_id
           WHERE m.cancelada = 0 AND m.data BETWEEN ? AND ? AND m.tipo IN ('entrada', 'saida', 'baixa')
           GROUP BY e.id, e.nome, e.unidade
           ORDER BY total_saidas DESC, e.nome COLLATE NOCASE""", p).fetchall()

    por_setor = db.execute(
        """SELECT COALESCE(setor, '(sem setor)') AS nome, SUM(quantidade) AS total,
                  COUNT(DISTINCT colaborador_id) AS colaboradores
           FROM movimentacoes WHERE cancelada = 0 AND tipo = 'saida' AND data BETWEEN ? AND ?
           GROUP BY COALESCE(setor, '(sem setor)') ORDER BY total DESC""", p).fetchall()

    por_local = db.execute(
        """SELECT COALESCE(local_destino, '(sem local)') AS nome, SUM(quantidade) AS total,
                  COUNT(DISTINCT grupo) AS entregas
           FROM movimentacoes WHERE cancelada = 0 AND tipo = 'saida' AND data BETWEEN ? AND ?
           GROUP BY COALESCE(local_destino, '(sem local)') ORDER BY total DESC""", p).fetchall()

    por_colaborador = db.execute(
        """SELECT c.id, c.nome, c.setor, SUM(m.quantidade) AS total, COUNT(DISTINCT m.grupo) AS entregas
           FROM movimentacoes m JOIN colaboradores c ON c.id = m.colaborador_id
           WHERE m.cancelada = 0 AND m.tipo = 'saida' AND m.data BETWEEN ? AND ?
           GROUP BY c.id, c.nome, c.setor ORDER BY total DESC LIMIT 15""", p).fetchall()

    por_mes = db.execute(
        """SELECT substr(data, 1, 7) AS mes,
                  SUM(CASE WHEN tipo = 'entrada' THEN quantidade ELSE 0 END) AS entradas,
                  SUM(CASE WHEN tipo IN ('saida', 'baixa') THEN quantidade ELSE 0 END) AS saidas
           FROM movimentacoes
           WHERE cancelada = 0 AND tipo IN ('entrada', 'saida', 'baixa') AND data BETWEEN ? AND ?
           GROUP BY substr(data, 1, 7) ORDER BY mes""", p).fetchall()

    maximos = {
        "ranking": max([r["entregas"] + r["baixas"] for r in ranking] or [1]) or 1,
        "setor": max([r["total"] for r in por_setor] or [1]) or 1,
        "local": max([r["total"] for r in por_local] or [1]) or 1,
        "colaborador": max([r["total"] for r in por_colaborador] or [1]) or 1,
        "mes": max([max(r["entradas"], r["saidas"]) for r in por_mes] or [1]) or 1,
        "tamanhos": max([r["total_unidades"] for r in linhas_tamanhos] or [1]) or 1,
    }
    hoje_ = hoje()
    atalhos = {
        "Últimos 30 dias": (hoje_ - timedelta(days=29), hoje_),
        "Últimos 90 dias": (hoje_ - timedelta(days=89), hoje_),
        "Este mês": (hoje_.replace(day=1), hoje_),
        "Este ano": (hoje_.replace(month=1, day=1), hoje_),
    }
    epis_com_tamanho = [e for e in epis_saldo if _subtipo_item(e["nome"])]
    return render_template(
        "relatorios.html",
        de=de,
        ate=ate,
        ranking=ranking,
        por_setor=por_setor,
        por_local=por_local,
        por_colaborador=por_colaborador,
        por_mes=por_mes,
        maximos=maximos,
        atalhos=atalhos,
        linhas_tamanhos=linhas_tamanhos,
        grades_subtipo=grades_subtipo,
        tamanhos_disponiveis=tamanhos_disponiveis,
        conferencia_estoque=conferencia_estoque,
        categorias_estoque=categorias_estoque,
        epis_com_tamanho=epis_com_tamanho,
        ROTULOS_SUBTIPO=ROTULOS_SUBTIPO,
        filtro_subtipo=filtro_subtipo,
        filtro_epi_id=filtro_epi_id,
        filtro_tam=filtro_tam,
        filtro_categoria=filtro_categoria,
    )


@bp.get("/relatorios/estocagem-tamanhos.csv")
def exportar_estocagem_tamanhos_csv():
    """Exporta planilha CSV com o detalhamento por Produto + Tamanho e a conferência de estocagem."""
    db = get_db()
    ate = parse_data(request.args.get("ate")) or hoje()
    de = parse_data(request.args.get("de")) or (ate - timedelta(days=89))
    if de > ate:
        de, ate = ate, de
    epis_saldo = estoque.epis_com_saldo(db, somente_ativos=False)
    linhas_tamanhos, _, _ = _montar_relatorio_tamanhos(
        db, de.isoformat(), ate.isoformat(), epis_saldo,
        filtro_subtipo=limpar(request.args.get("subtipo")) or "",
        filtro_epi_id=to_int(request.args.get("epi_id")),
        filtro_tam=limpar(request.args.get("tamanho")) or "",
    )
    saida = io.StringIO()
    w = csv.writer(saida, delimiter=";")
    w.writerow([
        "Produto (EPI / Uniforme)",
        "Tipo",
        "Tamanho / Numeração",
        "Funcionários que Usaram no Período",
        "Unidades Entregues",
        "Unidades em Pedidos Abertos",
        "Total Unidades (Período)",
        "Funcionários com este Tamanho no Perfil",
        "Nominal de Funcionários (Período)",
    ])
    for r in linhas_tamanhos:
        nomes_func = ", ".join(f"{f['nome']} ({f['qtd']}x)" for f in r["lista_funcionarios"])
        w.writerow([
            r["epi_nome"],
            r["subtipo_rotulo"],
            r["tamanho"],
            r["qtd_funcionarios_usaram"],
            r["qtd_entregue"],
            r["qtd_em_pedidos"],
            r["total_unidades"],
            r["qtd_funcionarios_perfil"],
            nomes_func,
        ])
    return _csv_response(saida, f"relatorio-tamanhos-estocagem-{de.isoformat()}-a-{ate.isoformat()}.csv")
