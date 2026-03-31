"""
Roberto Intelligence: análise preditiva e BI de fretes.
Com upload ativo na sessão, o BI usa somente os dados do upload (sem misturar com FreteReal).
Sem upload, usa a base persistida (FreteReal). Indicadores visuais por UF são por destino;
origem de upload recorta a base quando há mais de uma UF de origem.
"""
import logging
import random
from collections import defaultdict
from datetime import datetime
from flask import g, has_request_context, jsonify, request
from sqlalchemy import text

from app.extensions import db
from app.models import FreteReal
from app.roberto_custo import calcular_custo_robusto_rs_kg
from app.roberto_modelo import prever as roberto_prever
from app.roberto_qualidade_base import calcular_qualidade_base
from app.roberto_recomendacoes import gerar_recomendacoes_analise
from app.upload_handler import get_dados_upload_cliente

logger = logging.getLogger(__name__)

PESO_BASE_OURO = 1.0
PESO_BASE_CLIENTE = 0.6
MESES_SERIE = 18
# Score = média de deltas de previsão (R$/kg por passo). Valores com |score| abaixo disso = neutro.
HEATMAP_NEUTRO_EPS = 1e-5


def _compactar_qualidade_heatmap(qp: dict | None) -> dict | None:
    """
    Extrai de qualidade_previsao (motor prever) o mínimo para o heatmap por UF.
    Sem lógica nova de classificação — só filtro de campos e limite de motivos.
    """
    if not qp or not isinstance(qp, dict):
        return None
    out = {
        "classificacao_confiabilidade": qp.get("classificacao_confiabilidade"),
        "n_meses_observados": qp.get("n_meses_observados"),
        "coeficiente_variacao_observado": qp.get("coeficiente_variacao_observado"),
        "meses_previstos_com_piso_zero": qp.get("meses_previstos_com_piso_zero"),
    }
    if qp.get("desvio_padrao_observado") is not None and out.get("coeficiente_variacao_observado") is None:
        out["desvio_padrao_observado"] = qp.get("desvio_padrao_observado")
    motivos = qp.get("motivos_classificacao")
    if isinstance(motivos, list) and motivos:
        out["motivos_classificacao"] = motivos[:2]
    return out


def _parse_date(d) -> datetime | None:
    """Converte data (date, datetime ou string ISO) para datetime para agregação."""
    if d is None:
        return None
    if hasattr(d, "year"):
        return datetime(d.year, d.month, 1) if not isinstance(d, datetime) else d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    try:
        s = str(d).strip()[:10]
        return datetime.strptime(s, "%Y-%m-%d").replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    except Exception:
        return None


def _buscar_base_ouro() -> list[dict]:
    """Busca dados da base ouro (historico) usando IDs de localidade."""
    try:
        registros = FreteReal.query.filter(
            FreteReal.data_emissao.isnot(None),
            FreteReal.peso_real > 0,
        ).all()
        return [
            {
                "data_emissao": r.data_emissao,
                "id_cidade_origem": r.id_cidade_origem,
                "id_cidade_destino": r.id_cidade_destino,
                "uf_origem": (r.uf_origem or "").strip().upper()[:2],
                "uf_destino": (r.uf_destino or "").strip().upper()[:2],
                "peso_real": float(r.peso_real or 0),
                "valor_frete_total": float(r.valor_frete_total or 0),
                "modal": (r.modal or "").strip().lower(),
                "peso_registro": PESO_BASE_OURO,
            }
            for r in registros
        ]
    except Exception as e:
        logger.exception("Erro ao buscar base ouro: %s", e)
        return []


def _buscar_base_cliente() -> list[dict]:
    """Busca dados temporários do cliente (sessão) e aplica peso."""
    dados = get_dados_upload_cliente()
    if not dados:
        return []
    out = []
    for r in dados:
        data_emissao = r.get("data_emissao")
        if isinstance(data_emissao, str):
            try:
                data_emissao = datetime.fromisoformat(data_emissao.replace("Z", "+00:00"))
            except Exception:
                data_emissao = None
        uf_origem = (r.get("uf_origem") or "").strip().upper()[:2]
        uf_destino = (r.get("uf_destino") or "").strip().upper()[:2]
        out.append({
            "data_emissao": data_emissao,
            "id_cidade_origem": r.get("id_cidade_origem"),
            "id_cidade_destino": r.get("id_cidade_destino"),
            "id_uf_origem": r.get("id_uf_origem"),
            "id_uf_destino": r.get("id_uf_destino"),
            "uf_origem": uf_origem,
            "uf_destino": uf_destino,
            "peso_real": float(r.get("peso_real") or 0),
            "valor_frete_total": float(r.get("valor_frete_total") or 0),
            "modal": (r.get("modal") or "").strip().lower(),
            "peso_registro": PESO_BASE_CLIENTE,
        })
    return out


def _uf_origem_key(r: dict) -> str:
    return (r.get("uf_origem") or "").strip().upper()[:2]


def _registro_valido_bi(r: dict) -> bool:
    """Registro com origem/destino e peso mínimos para contagem e default de origem no upload."""
    if float(r.get("peso_real") or 0) <= 0:
        return False
    orig_ok = bool((r.get("uf_origem") or "").strip()) or bool(r.get("id_cidade_origem"))
    dest_ok = bool((r.get("uf_destino") or "").strip()) or bool(r.get("id_cidade_destino"))
    return orig_ok and dest_ok


def _filtrar_por_origem_upload(cliente: list[dict], origem_param: str | None) -> list[dict]:
    """
    Recorta upload pela UF de origem. Uma origem: mantém só linhas dessa UF.
    Várias origens: usa query origem_uf se válida; senão default (maior volume válido, empate alfabético).
    """
    if not cliente:
        return []
    valid = [r for r in cliente if _registro_valido_bi(r)]
    counts: dict[str, int] = defaultdict(int)
    for r in valid:
        k = _uf_origem_key(r)
        if k:
            counts[k] += 1
    distinct = sorted(counts.keys())
    if not distinct:
        return list(cliente)
    if len(distinct) == 1:
        chosen = distinct[0]
    else:
        if origem_param and origem_param in counts:
            chosen = origem_param
        else:
            max_c = max(counts.values())
            chosen = sorted([k for k in distinct if counts[k] == max_c])[0]
    return [r for r in cliente if _uf_origem_key(r) == chosen]


def _get_bi_dataset() -> list[dict]:
    """Base única do BI: só upload (recortado por origem) se houver sessão; senão só FreteReal."""
    cliente = _buscar_base_cliente()
    cliente = _enriquecer_ufs_cliente(cliente)
    if cliente:
        origem_param = None
        if has_request_context():
            p = request.args.get("origem_uf")
            if p:
                origem_param = str(p).strip().upper()[:2]
        return _filtrar_por_origem_upload(cliente, origem_param)
    return _buscar_base_ouro()


def bi_meta_json() -> dict:
    """Metadados do BI no contexto de upload (origens e seleção para o filtro)."""
    cliente = _buscar_base_cliente()
    cliente = _enriquecer_ufs_cliente(cliente)
    if not cliente:
        return {
            "upload_ativo": False,
            "origens": [],
            "origem_default": None,
            "origem_selecionada": None,
            "multiplas_origens": False,
        }
    valid = [r for r in cliente if _registro_valido_bi(r)]
    counts: dict[str, int] = defaultdict(int)
    for r in valid:
        k = _uf_origem_key(r)
        if k:
            counts[k] += 1
    distinct = sorted(counts.keys())
    if not distinct:
        return {
            "upload_ativo": True,
            "origens": [],
            "origem_default": None,
            "origem_selecionada": None,
            "multiplas_origens": False,
        }
    max_c = max(counts.values())
    origem_default = sorted([k for k in distinct if counts[k] == max_c])[0]
    origem_param = None
    if has_request_context():
        p = request.args.get("origem_uf")
        if p:
            origem_param = str(p).strip().upper()[:2]
    if origem_param and origem_param in counts:
        origem_sel = origem_param
    else:
        origem_sel = origem_default
    return {
        "upload_ativo": True,
        "origens": distinct,
        "origem_default": origem_default,
        "origem_selecionada": origem_sel,
        "multiplas_origens": len(distinct) > 1,
    }


def _enriquecer_ufs_cliente(registros_cliente: list[dict]) -> list[dict]:
    """
    Fallback: preenche uf_origem/uf_destino a partir de id_cidade em base_localidades
    quando o payload ainda não trouxer UF (ex.: sessões antigas ou dados parciais).
    Usa o mesmo banco padrão da aplicação (DATABASE_URL).
    """
    try:
        engine = db.get_engine()
        with engine.connect() as conn:
            for r in registros_cliente:
                if not r.get("uf_origem") and r.get("id_cidade_origem"):
                    row = conn.execute(
                        text("SELECT uf_nome FROM base_localidades WHERE id_cidade = :id LIMIT 1"),
                        {"id": r["id_cidade_origem"]},
                    ).fetchone()
                    if row:
                        r["uf_origem"] = (row[0] or "").strip().upper()[:2]
                if not r.get("uf_destino") and r.get("id_cidade_destino"):
                    row = conn.execute(
                        text("SELECT uf_nome FROM base_localidades WHERE id_cidade = :id LIMIT 1"),
                        {"id": r["id_cidade_destino"]},
                    ).fetchone()
                    if row:
                        r["uf_destino"] = (row[0] or "").strip().upper()[:2]
    except Exception as e:
        logger.debug("Enriquecer UFs cliente: %s", e)
    return registros_cliente


def calcular_custo_medio() -> dict:
    """Custo robusto R$/KG e R$/T para o período (mesma régua da série, ranking e modelo)."""
    unidos = _get_bi_dataset()
    if not unidos:
        return {"custo_rs_kg": None, "custo_rs_t": None, "registros": 0}

    custo_kg = calcular_custo_robusto_rs_kg(unidos)
    if custo_kg is None:
        return {"custo_rs_kg": None, "custo_rs_t": None, "registros": len(unidos)}
    return {
        "custo_rs_kg": custo_kg,
        "custo_rs_t": round(custo_kg * 1000, 2),
        "registros": len(unidos),
    }


def _montar_contexto_bi_roberto() -> dict:
    """
    Camada canônica do BI analítico: uma leitura da base ativa, um prever() quando há dados,
    qualidade da base, série e recomendações derivadas dos mesmos objetos.
    Não mistura origens (upload vs base ouro) — só consolida o fluxo sobre _get_bi_dataset().
    """
    unidos = _get_bi_dataset()
    qualidade_base = calcular_qualidade_base(unidos)
    resultado = None
    qualidade_previsao: dict | None = None
    meses_ord: list[str] = []
    valores: list[float] = []
    previsao_meses: list[str] = []
    previsao_valores: list[float] = []

    if unidos:
        historico = _historico_roberto_por_linhas(unidos)
        historico = _filtrar_historico_ultimos_meses(historico, MESES_SERIE)
        resultado = roberto_prever(historico, None)
        meses_ord = list(resultado.get("serie_historica_meses") or [])
        valores = [float(v) for v in (resultado.get("serie_historica_valores") or [])]
        pn = resultado.get("previsao_numerica") if isinstance(resultado, dict) else None
        if pn and isinstance(pn, dict):
            previsao_meses = list(pn.get("meses") or [])
            previsao_valores = [float(v) for v in (pn.get("valores_rs_kg") or [])]
        qp = resultado.get("qualidade_previsao")
        qualidade_previsao = qp if isinstance(qp, dict) else None

    recomendacoes = gerar_recomendacoes_analise(qualidade_base, qualidade_previsao)

    serie_temporal = {
        "meses": meses_ord,
        "valores": valores,
        "previsao_meses": previsao_meses,
        "previsao_valores": previsao_valores,
    }
    if qualidade_previsao is not None:
        serie_temporal["qualidade_previsao"] = qualidade_previsao

    return {
        "unidos": unidos,
        "qualidade_base": qualidade_base,
        "qualidade_previsao": qualidade_previsao,
        "resultado_prever": resultado,
        "recomendacoes_analise": recomendacoes,
        "serie_temporal": serie_temporal,
    }


def get_contexto_bi_roberto() -> dict:
    """Memoização por request (Flask g): múltiplos handlers/helpers no mesmo ciclo compartilham o contexto."""
    if has_request_context():
        cached = getattr(g, "_roberto_bi_contexto", None)
        if cached is not None:
            return cached
        ctx = _montar_contexto_bi_roberto()
        g._roberto_bi_contexto = ctx
        return ctx
    return _montar_contexto_bi_roberto()


def serie_temporal_json() -> dict:
    """Endpoint: série dos últimos 18 meses + previsão 6 meses + metadados de confiabilidade."""
    ctx = get_contexto_bi_roberto()
    return dict(ctx["serie_temporal"])


def contexto_analitico_json() -> dict:
    """Payload agregado: série + qualidade da base + recomendações (mesmo cálculo que endpoints isolados)."""
    ctx = get_contexto_bi_roberto()
    return {
        "serie_temporal": ctx["serie_temporal"],
        "qualidade_base": ctx["qualidade_base"],
        "recomendacoes_analise": ctx["recomendacoes_analise"],
    }


def ranking_ufs() -> dict:
    """Ranking de UFs por custo médio (R$/KG), agrupado por UF de destino."""
    unidos = _get_bi_dataset()
    if not unidos:
        return {"ufs": [], "custos": [], "labels": []}

    por_uf: dict[str, list[dict]] = defaultdict(list)
    for r in unidos:
        uf = (r.get("uf_destino") or "").strip().upper()[:2]
        if uf:
            por_uf[uf].append(r)

    ranking = []
    for uf, rows in por_uf.items():
        custo = calcular_custo_robusto_rs_kg(rows)
        ranking.append((uf, custo if custo is not None else 0.0))
    ranking.sort(key=lambda t: t[1], reverse=True)
    ufs = [t[0] for t in ranking]
    custos = [t[1] for t in ranking]
    return {"ufs": ufs, "custos": custos, "labels": ufs}


def _historico_roberto_por_linhas(rows: list[dict]) -> list[dict]:
    """Monta histórico no formato de roberto_modelo.prever (valor, peso, data_emissao)."""
    out = []
    for r in rows:
        peso = float(r.get("peso_real") or 0)
        if peso <= 0:
            continue
        out.append(
            {
                "valor": float(r.get("valor_frete_total") or 0),
                "peso": peso,
                "data_emissao": r.get("data_emissao"),
            }
        )
    return out


def _filtrar_historico_ultimos_meses(historico: list[dict], max_meses: int) -> list[dict]:
    """Mantém apenas linhas cujo mês está entre os últimos `max_meses` meses-calendário observados."""
    if not historico:
        return []
    meses_unicos: set[str] = set()
    for r in historico:
        d = _parse_date(r.get("data_emissao"))
        if d:
            meses_unicos.add(d.strftime("%Y-%m"))
    meses_ord = sorted(meses_unicos)
    if not meses_ord:
        return historico
    if len(meses_ord) <= max_meses:
        return historico
    permitidos = set(meses_ord[-max_meses:])
    filtrado = []
    for r in historico:
        d = _parse_date(r.get("data_emissao"))
        if d and d.strftime("%Y-%m") in permitidos:
            filtrado.append(r)
    return filtrado


def _score_medio_variacoes_previsao(valores_rs_kg: list[float]) -> float | None:
    """Média das variações mês a mês na série prevista (R$/kg); proxy de tendência dos próximos meses."""
    if not valores_rs_kg or len(valores_rs_kg) < 2:
        return None
    deltas = [valores_rs_kg[i + 1] - valores_rs_kg[i] for i in range(len(valores_rs_kg) - 1)]
    return sum(deltas) / len(deltas)


def _classificar_intensidade_em_lado(
    ordenado_mais_intenso_primeiro: list[tuple[str, float]],
    nivel_forte: str,
    nivel_moderado: str,
) -> dict[str, str]:
    """
    Dentro de um lado (só negativos ou só positivos), metade mais intensa → nível forte,
    restante → moderado. Ordem do argumento: do efeito mais forte ao mais fraco nesse lado.
    """
    out: dict[str, str] = {}
    n = len(ordenado_mais_intenso_primeiro)
    if n == 0:
        return out
    if n == 1:
        out[ordenado_mais_intenso_primeiro[0][0]] = nivel_forte
        return out
    corte = (n + 1) // 2
    for i, (uf, _) in enumerate(ordenado_mais_intenso_primeiro):
        out[uf] = nivel_forte if i < corte else nivel_moderado
    return out


def _classificar_niveis_temperatura_por_direcao(scores_uf: list[tuple[str, float]]) -> dict[str, str]:
    """
    Direção do score primeiro (negativo = frio, |score| pequeno = neutro, positivo = quente);
    dentro dos negativos ou dos positivos, moderado vs forte é relativo só àquele grupo.
    """
    if not scores_uf:
        return {}
    eps = HEATMAP_NEUTRO_EPS
    neg = [(u, s) for u, s in scores_uf if s < -eps]
    pos = [(u, s) for u, s in scores_uf if s > eps]
    neut = [(u, s) for u, s in scores_uf if -eps <= s <= eps]

    out: dict[str, str] = {}
    for u, _ in neut:
        out[u] = "neutro"

    neg_ord = sorted(neg, key=lambda x: x[1])
    out.update(_classificar_intensidade_em_lado(neg_ord, "muito_frio", "frio"))

    pos_ord = sorted(pos, key=lambda x: x[1], reverse=True)
    out.update(_classificar_intensidade_em_lado(pos_ord, "muito_quente", "quente"))

    return out


def heatmap_brasil() -> dict:
    """
    Heatmap por UF destino: previsão Roberto (6 meses) sobre até 18 meses de histórico,
    score = média das variações mês a mês previstas; níveis por sinal do score e intensidade relativa em cada lado.
    """
    unidos = _get_bi_dataset()
    if not unidos:
        return {
            "ufs": [],
            "valores": [],
            "nivel_temperatura": [],
            "tendencia_alta": [],
            "qualidade_uf": [],
        }

    por_uf: dict[str, list[dict]] = defaultdict(list)
    for r in unidos:
        uf = (r.get("uf_destino") or "").strip().upper()[:2]
        if uf:
            por_uf[uf].append(r)

    itens: list[dict] = []
    for uf, rows in por_uf.items():
        historico = _historico_roberto_por_linhas(rows)
        historico = _filtrar_historico_ultimos_meses(historico, MESES_SERIE)
        if not historico:
            itens.append({"uf": uf, "score": None, "valor_vis": 0.0, "qualidade": None})
            continue
        try:
            resultado = roberto_prever(historico, None)
        except Exception as e:
            logger.warning("heatmap_brasil: prever falhou para UF %s: %s", uf, e)
            itens.append({"uf": uf, "score": None, "valor_vis": 0.0, "qualidade": None})
            continue
        qp_raw = resultado.get("qualidade_previsao") if isinstance(resultado, dict) else None
        qualidade = _compactar_qualidade_heatmap(qp_raw if isinstance(qp_raw, dict) else None)
        pn = resultado.get("previsao_numerica") if isinstance(resultado, dict) else None
        vals = (pn or {}).get("valores_rs_kg") or []
        score = _score_medio_variacoes_previsao(vals)
        if score is None:
            itens.append({"uf": uf, "score": None, "valor_vis": 0.0, "qualidade": qualidade})
        else:
            itens.append({"uf": uf, "score": score, "valor_vis": round(abs(score), 6), "qualidade": qualidade})

    com_score = [(x["uf"], x["score"]) for x in itens if x.get("score") is not None]
    nivel_por_uf = _classificar_niveis_temperatura_por_direcao(com_score)

    itens.sort(key=lambda x: (x["score"] is None, -(x["score"] or 0.0)))

    ufs: list[str] = []
    valores: list[float] = []
    nivel_temperatura: list[str] = []
    tendencia_alta: list[bool] = []
    qualidade_uf: list[dict | None] = []
    for x in itens:
        u = x["uf"]
        ufs.append(u)
        valores.append(float(x["valor_vis"]))
        q = x.get("qualidade")
        qualidade_uf.append(q if isinstance(q, dict) else None)
        sc = x.get("score")
        if sc is None:
            nivel_temperatura.append("neutro")
            tendencia_alta.append(False)
        else:
            nv = nivel_por_uf.get(u, "neutro")
            nivel_temperatura.append(nv)
            tendencia_alta.append(nv in ("quente", "muito_quente"))

    return {
        "ufs": ufs,
        "valores": valores,
        "nivel_temperatura": nivel_temperatura,
        "tendencia_alta": tendencia_alta,
        "qualidade_uf": qualidade_uf,
    }


def proporcao_modal() -> dict:
    """Proporção de fretes por tipo de modal (gráfico de rosca)."""
    unidos = _get_bi_dataset()
    if not unidos:
        return {"labels": [], "values": []}

    por_modal: dict[str, float] = defaultdict(float)
    for r in unidos:
        modal = (r.get("modal") or "outros").strip() or "outros"
        por_modal[modal] += r["peso_real"] * r["peso_registro"]
    total = sum(por_modal.values())
    if total <= 0:
        return {"labels": [], "values": []}
    labels = list(por_modal.keys())
    values = [round(100 * por_modal[m] / total, 2) for m in labels]
    return {"labels": labels, "values": values}


def dispersao_volume_custo() -> dict:
    """Dados para gráfico de dispersão/barras: volume (peso) x custo (R$/KG)."""
    unidos = _get_bi_dataset()
    if not unidos:
        return {"pontos": []}

    pontos = []
    for r in unidos:
        peso = r.get("peso_real") or 0
        valor = r.get("valor_frete_total") or 0
        if peso > 0:
            custo_kg = valor / peso
            pontos.append({"peso": round(peso, 2), "custo_kg": round(custo_kg, 4), "valor_frete": round(valor, 2)})
    # Limitar pontos para não sobrecarregar o gráfico
    if len(pontos) > 500:
        random.shuffle(pontos)
        pontos = pontos[:500]
    return {"pontos": pontos}


# --- Endpoints (retornam JSON para o frontend) ---

def api_bi_meta():
    return jsonify(bi_meta_json())


def api_custo_medio():
    return jsonify(calcular_custo_medio())


def api_serie_temporal():
    return jsonify(serie_temporal_json())


def api_ranking_ufs():
    return jsonify(ranking_ufs())


def api_heatmap():
    return jsonify(heatmap_brasil())


def api_modal():
    return jsonify(proporcao_modal())


def api_dispersao():
    return jsonify(dispersao_volume_custo())


def qualidade_base_json() -> dict:
    """Metadados de qualidade da base de entrada (mesma origem que o restante do BI)."""
    return get_contexto_bi_roberto()["qualidade_base"]


def api_qualidade_base():
    return jsonify(qualidade_base_json())


def recomendacoes_analise_json() -> dict:
    """Recomendações derivadas de qualidade_base + qualidade_previsao (mesma origem que o BI)."""
    return {"recomendacoes_analise": get_contexto_bi_roberto()["recomendacoes_analise"]}


def api_recomendacoes():
    return jsonify(recomendacoes_analise_json())


def api_contexto_analitico():
    return jsonify(contexto_analitico_json())
