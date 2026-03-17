"""
Roberto Intelligence: análise preditiva e BI de fretes.
Unifica base ouro (historico_frete.db) e base temporária do cliente (sessão),
aplica pesos, calcula métricas, séries temporais, rankings e expõe endpoints JSON.
"""
import logging
import random
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from flask import jsonify
from sqlalchemy import text

from app.extensions import db
from app.models import FreteReal
from app.upload_handler import get_dados_upload_cliente

logger = logging.getLogger(__name__)

PESO_BASE_OURO = 1.0
PESO_BASE_CLIENTE = 0.6
MESES_SERIE = 18
MESES_PREVISAO = 6


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
        # Upload não guarda uf no dict; podemos derivar do id se necessário. Por simplicidade deixamos vazios e usamos só IDs para join.
        out.append({
            "data_emissao": data_emissao,
            "id_cidade_origem": r.get("id_cidade_origem"),
            "id_cidade_destino": r.get("id_cidade_destino"),
            "uf_origem": uf_origem,
            "uf_destino": uf_destino,
            "peso_real": float(r.get("peso_real") or 0),
            "valor_frete_total": float(r.get("valor_frete_total") or 0),
            "modal": (r.get("modal") or "").strip().lower(),
            "peso_registro": PESO_BASE_CLIENTE,
        })
    return out


def _enriquecer_ufs_cliente(registros_cliente: list[dict]) -> list[dict]:
    """Preenche uf_origem/uf_destino a partir de id_cidade no banco de localidades oficial (base_localidades)."""
    try:
        engine = db.engines.get("localidades")
        if not engine:
            return registros_cliente
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


def _unir_bases() -> tuple[list[dict], bool]:
    """
    Unifica base ouro e base cliente com pesos.
    Retorna (lista de registros unificados, usou_apenas_cliente).
    """
    ouro = _buscar_base_ouro()
    cliente = _buscar_base_cliente()
    cliente = _enriquecer_ufs_cliente(cliente)

    if not ouro and not cliente:
        return [], True
    if not ouro:
        return cliente, True
    # Merge: ouro com peso 1.0, cliente com peso 0.6
    unidos = ouro + cliente
    return unidos, False


def calcular_custo_medio() -> dict:
    """Custo médio R$/KG e R$/T para o período (todos os dados unidos)."""
    unidos, _ = _unir_bases()
    if not unidos:
        return {"custo_rs_kg": None, "custo_rs_t": None, "registros": 0}

    soma_v_peso = sum(r["valor_frete_total"] * r["peso_registro"] for r in unidos)
    soma_p_peso = sum(r["peso_real"] * r["peso_registro"] for r in unidos)
    if soma_p_peso <= 0:
        return {"custo_rs_kg": None, "custo_rs_t": None, "registros": len(unidos)}
    custo_kg = soma_v_peso / soma_p_peso
    return {
        "custo_rs_kg": round(custo_kg, 4),
        "custo_rs_t": round(custo_kg * 1000, 2),
        "registros": len(unidos),
    }


def _serie_temporal_dados() -> tuple[list[dict], list[str], list[float], list[str], list[float]]:
    """Gera série dos últimos 18 meses e previsão 6 meses (regressão linear simples)."""
    unidos, _ = _unir_bases()
    if not unidos:
        return [], [], [], [], []

    # Agrupar por mês (chave ano-mês)
    por_mes: dict[str, list[dict]] = defaultdict(list)
    for r in unidos:
        dt = _parse_date(r.get("data_emissao"))
        if dt:
            chave = dt.strftime("%Y-%m")
            por_mes[chave].append(r)

    # Ordenar meses e calcular custo médio ponderado por mês
    meses_ord = sorted(por_mes.keys())
    if len(meses_ord) > MESES_SERIE:
        meses_ord = meses_ord[-MESES_SERIE:]
    valores = []
    for m in meses_ord:
        rows = por_mes[m]
        s_v = sum(r["valor_frete_total"] * r["peso_registro"] for r in rows)
        s_p = sum(r["peso_real"] * r["peso_registro"] for r in rows)
        valores.append(round(s_v / s_p, 4) if s_p > 0 else 0.0)

    # Previsão: regressão linear nos últimos 12 meses (ou todos se menos)
    n = len(valores)
    if n < 2:
        previsao_meses = []
        previsao_valores = []
    else:
        x = list(range(n))
        y = valores
        sx, sy = sum(x), sum(y)
        sx2 = sum(xi * xi for xi in x)
        sxy = sum(xi * yi for xi, yi in zip(x, y))
        den = n * sx2 - sx * sx
        if den == 0:
            b, a = 0, (sy / n)
        else:
            b = (n * sxy - sx * sy) / den
            a = (sy - b * sx) / n
        ultimo_mes = datetime.strptime(meses_ord[-1], "%Y-%m")
        previsao_meses = []
        previsao_valores = []
        for i in range(1, MESES_PREVISAO + 1):
            prox = ultimo_mes + timedelta(days=32 * i)
            prox = prox.replace(day=1)
            previsao_meses.append(prox.strftime("%Y-%m"))
            previsao_valores.append(round(a + b * (n + i - 1), 4))
    return unidos, meses_ord, valores, previsao_meses, previsao_valores


def serie_temporal_json() -> dict:
    """Endpoint: série dos últimos 18 meses + previsão 6 meses."""
    _, meses_ord, valores, previsao_meses, previsao_valores = _serie_temporal_dados()
    return {
        "meses": meses_ord,
        "valores": valores,
        "previsao_meses": previsao_meses,
        "previsao_valores": previsao_valores,
    }


def ranking_ufs() -> dict:
    """Ranking de UFs por custo médio (R$/KG). Usa UF de origem."""
    unidos, _ = _unir_bases()
    if not unidos:
        return {"ufs": [], "custos": [], "labels": []}

    por_uf: dict[str, list[dict]] = defaultdict(list)
    for r in unidos:
        uf = (r.get("uf_origem") or r.get("uf_destino") or "??").strip().upper()[:2]
        if uf:
            por_uf[uf].append(r)

    ranking = []
    for uf, rows in por_uf.items():
        s_v = sum(x["valor_frete_total"] * x["peso_registro"] for x in rows)
        s_p = sum(x["peso_real"] * x["peso_registro"] for x in rows)
        custo = round(s_v / s_p, 4) if s_p > 0 else 0
        ranking.append((uf, custo))
    ranking.sort(key=lambda t: t[1], reverse=True)
    ufs = [t[0] for t in ranking]
    custos = [t[1] for t in ranking]
    return {"ufs": ufs, "custos": custos, "labels": ufs}


def heatmap_brasil() -> dict:
    """Dados para heatmap do Brasil: UFs com tendência de alta (slope positivo na série)."""
    unidos, _ = _unir_bases()
    if not unidos:
        return {"ufs": [], "tendencia_alta": [], "valores": []}

    por_uf: dict[str, list[dict]] = defaultdict(list)
    for r in unidos:
        uf = (r.get("uf_origem") or r.get("uf_destino") or "??").strip().upper()[:2]
        if uf:
            por_uf[uf].append(r)

    resultado = []
    for uf, rows in por_uf.items():
        por_mes_uf: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            dt = _parse_date(r.get("data_emissao"))
            if dt:
                por_mes_uf[dt.strftime("%Y-%m")].append(r)
        meses_uf = sorted(por_mes_uf.keys())
        if len(meses_uf) < 2:
            resultado.append({"uf": uf, "tendencia_alta": False, "valor": 0.0})
            continue
        custos_mes = []
        for m in meses_uf[-12:]:
            rs = por_mes_uf[m]
            s_v = sum(x["valor_frete_total"] * x["peso_registro"] for x in rs)
            s_p = sum(x["peso_real"] * x["peso_registro"] for x in rs)
            custos_mes.append(s_v / s_p if s_p > 0 else 0)
        n = len(custos_mes)
        x = list(range(n))
        y = custos_mes
        sx, sy = sum(x), sum(y)
        sx2 = sum(xi * xi for xi in x)
        sxy = sum(xi * yi for xi, yi in zip(x, y))
        den = n * sx2 - sx * sx
        slope = (n * sxy - sx * sy) / den if den else 0
        media = sum(y) / n if n else 0
        resultado.append({"uf": uf, "tendencia_alta": slope > 0, "valor": round(media, 4), "slope": round(slope, 6)})
    resultado.sort(key=lambda t: t["valor"], reverse=True)
    return {
        "ufs": [t["uf"] for t in resultado],
        "tendencia_alta": [t["tendencia_alta"] for t in resultado],
        "valores": [t["valor"] for t in resultado],
        "slopes": [t["slope"] for t in resultado],
    }


def proporcao_modal() -> dict:
    """Proporção de fretes por tipo de modal (gráfico de rosca)."""
    unidos, _ = _unir_bases()
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
    unidos, _ = _unir_bases()
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
