"""
Metadados de qualidade dos dados de entrada (upload ou base ouro), separados da
confiabilidade da previsão (qualidade_previsao no motor).

Não descarta registros: apenas mede, classifica e expõe sinais auditáveis.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime
from typing import Any

# Peso abaixo deste valor (kg) conta como "muito baixo" para percentual de alerta
PESO_MUITO_BAIXO_KG = 1.0


def _parse_mes_emissao(d: Any) -> str | None:
    """Retorna 'YYYY-MM' ou None."""
    if d is None:
        return None
    if hasattr(d, "year"):
        dt = datetime(d.year, d.month, 1) if not isinstance(d, datetime) else d.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        return dt.strftime("%Y-%m")
    try:
        s = str(d).strip()[:10]
        dt = datetime.strptime(s, "%Y-%m-%d").replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return dt.strftime("%Y-%m")
    except Exception:
        return None


def _percentil(sorted_vals: list[float], p: float) -> float | None:
    """Percentil linear (p em [0,100])."""
    if not sorted_vals:
        return None
    x = sorted_vals
    n = len(x)
    if n == 1:
        return float(x[0])
    k = (n - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, n - 1)
    return float(x[f] + (x[c] - x[f]) * (k - f))


def _classificar_qualidade_base(
    *,
    n_reg: int,
    n_meses: int,
    min_linhas_mes: int,
    max_linhas_mes: int,
    pct_peso_baixo: float,
    pct_top3: float,
    linhas_com_data: int,
    linhas_sem_data: int,
    mediana_rs: float | None,
    p95_rs: float | None,
) -> tuple[str, list[str]]:
    """
    Regras explícitas:

    baixa — se qualquer:
      - menos de 30 registros (peso > 0)
      - menos de 3 meses distintos com emissão
      - algum mês com menos de 2 embarques (quando há pelo menos 1 mês)
      - mais de 20% com peso < PESO_MUITO_BAIXO_KG
      - mais de 85% das emissões com data nos 3 meses mais frequentes
      - P95/mediana > 3,5 (dispersão muito alta entre embarques), mediana > 0
      - mais de 15% sem data de emissão válida (entre registros com peso > 0)

    alta — se simultaneamente:
      - n_reg >= 200, n_meses >= 12, min_linhas_mes >= 5
      - pct_peso_baixo <= 5
      - pct_top3 <= 60 (linhas com data)
      - P95/mediana <= 2,5 quando mediana > 0 e p95 definido
      - pct_sem_data <= 5%

    media — demais casos.
    """
    motivos_baixa: list[str] = []
    pct_sem_data = (100.0 * linhas_sem_data / n_reg) if n_reg > 0 else 0.0

    if n_reg < 30:
        motivos_baixa.append("Menos de 30 registros com peso > 0.")
    if n_meses < 3:
        motivos_baixa.append("Menos de 3 meses distintos com data de emissão.")
    if n_meses >= 1 and min_linhas_mes < 2:
        motivos_baixa.append("Algum mês com menos de 2 embarques (base mensal muito esparsa).")
    if pct_peso_baixo > 20.0:
        motivos_baixa.append(
            f"Mais de 20% dos registros com peso abaixo de {PESO_MUITO_BAIXO_KG:g} kg."
        )
    if linhas_com_data > 0 and pct_top3 > 85.0:
        motivos_baixa.append(
            "Mais de 85% das emissões com data concentradas nos 3 meses com mais linhas."
        )
    if mediana_rs is not None and mediana_rs > 1e-9 and p95_rs is not None and p95_rs > 0:
        if (p95_rs / mediana_rs) > 3.5:
            motivos_baixa.append(
                "Dispersão muito alta entre embarques (P95 de R$/kg muito acima da mediana)."
            )
    if pct_sem_data > 15.0:
        motivos_baixa.append("Mais de 15% dos registros sem data de emissão válida.")

    if motivos_baixa:
        return ("baixa", motivos_baixa[:6])

    if (
        n_reg >= 200
        and n_meses >= 12
        and min_linhas_mes >= 5
        and pct_peso_baixo <= 5.0
        and (linhas_com_data == 0 or pct_top3 <= 60.0)
        and pct_sem_data <= 5.0
        and (
            mediana_rs is None
            or mediana_rs <= 1e-9
            or p95_rs is None
            or (p95_rs / mediana_rs) <= 2.5
        )
    ):
        return (
            "alta",
            [
                "Volume e cobertura temporal amplos, pouca concentração temporal, "
                "poucos alertas de peso e dispersão entre embarques contidos."
            ],
        )

    motivos_m: list[str] = []
    if n_reg < 200:
        motivos_m.append("Volume de registros abaixo do limiar para classificação alta.")
    if n_meses < 12:
        motivos_m.append("Menos de 12 meses distintos com emissão.")
    if min_linhas_mes < 5:
        motivos_m.append("Algum mês com menos de 5 embarques.")
    if pct_peso_baixo > 5.0:
        motivos_m.append("Mais de 5% dos registros com peso muito baixo.")
    if linhas_com_data > 0 and pct_top3 > 60.0:
        motivos_m.append("Concentração temporal acima do limiar para classificação alta.")
    if mediana_rs is not None and mediana_rs > 1e-9 and p95_rs is not None and (p95_rs / mediana_rs) > 2.5:
        motivos_m.append("Dispersão entre embarques (P95 vs mediana) acima do limiar para alta.")
    if pct_sem_data > 5.0:
        motivos_m.append("Parcela de registros sem data acima do limiar para alta.")
    if not motivos_m:
        motivos_m.append("Critérios intermediários entre alta e baixa.")
    return ("media", motivos_m[:6])


def calcular_qualidade_base(registros: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Calcula métricas descritivas e classificação da base (entrada), sem remover linhas.

    Usa apenas a lista já filtrada pela origem ativa (upload ou frete_real).
    """
    if not registros:
        return {
            "n_registros_validos": 0,
            "n_meses_cobertos": 0,
            "min_linhas_por_mes": 0,
            "max_linhas_por_mes": 0,
            "mediana_rs_kg_embarque": None,
            "percentil_95_rs_kg_embarque": None,
            "pct_peso_muito_baixo": 0.0,
            "limiar_peso_muito_baixo_kg": PESO_MUITO_BAIXO_KG,
            "pct_concentracao_top3_meses": 0.0,
            "linhas_com_data": 0,
            "linhas_sem_data": 0,
            "classificacao": "baixa",
            "motivos_classificacao": ["Nenhum registro com peso > 0 na base atual."],
        }

    custos: list[float] = []
    por_mes: dict[str, int] = defaultdict(int)
    n_reg = 0
    n_peso_muito_baixo = 0
    linhas_com_data = 0
    linhas_sem_data = 0

    for r in registros:
        peso = float(r.get("peso_real") or 0)
        if peso <= 0:
            continue
        n_reg += 1
        if peso < PESO_MUITO_BAIXO_KG:
            n_peso_muito_baixo += 1
        v = float(r.get("valor_frete_total") or 0)
        custos.append(v / peso)
        ch = _parse_mes_emissao(r.get("data_emissao"))
        if ch:
            por_mes[ch] += 1
            linhas_com_data += 1
        else:
            linhas_sem_data += 1

    if n_reg == 0:
        return {
            "n_registros_validos": 0,
            "n_meses_cobertos": 0,
            "min_linhas_por_mes": 0,
            "max_linhas_por_mes": 0,
            "mediana_rs_kg_embarque": None,
            "percentil_95_rs_kg_embarque": None,
            "pct_peso_muito_baixo": 0.0,
            "limiar_peso_muito_baixo_kg": PESO_MUITO_BAIXO_KG,
            "pct_concentracao_top3_meses": 0.0,
            "linhas_com_data": 0,
            "linhas_sem_data": 0,
            "classificacao": "baixa",
            "motivos_classificacao": ["Nenhum registro com peso > 0 na base atual."],
        }

    counts = list(por_mes.values())
    n_meses = len(por_mes)
    min_linhas = min(counts) if counts else 0
    max_linhas = max(counts) if counts else 0

    custos_ord = sorted(custos)
    mediana_rs = statistics.median(custos_ord)
    p95_rs = _percentil(custos_ord, 95.0)

    pct_peso_baixo = 100.0 * n_peso_muito_baixo / n_reg

    if linhas_com_data > 0:
        top3 = sum(sorted(counts, reverse=True)[:3])
        pct_top3 = 100.0 * top3 / linhas_com_data
    else:
        pct_top3 = 0.0

    cls, motivos = _classificar_qualidade_base(
        n_reg=n_reg,
        n_meses=n_meses,
        min_linhas_mes=min_linhas,
        max_linhas_mes=max_linhas,
        pct_peso_baixo=pct_peso_baixo,
        pct_top3=pct_top3,
        linhas_com_data=linhas_com_data,
        linhas_sem_data=linhas_sem_data,
        mediana_rs=mediana_rs,
        p95_rs=p95_rs,
    )

    return {
        "n_registros_validos": n_reg,
        "n_meses_cobertos": n_meses,
        "min_linhas_por_mes": min_linhas,
        "max_linhas_por_mes": max_linhas,
        "mediana_rs_kg_embarque": round(mediana_rs, 6),
        "percentil_95_rs_kg_embarque": None if p95_rs is None else round(p95_rs, 6),
        "pct_peso_muito_baixo": round(pct_peso_baixo, 2),
        "limiar_peso_muito_baixo_kg": PESO_MUITO_BAIXO_KG,
        "pct_concentracao_top3_meses": round(pct_top3, 2),
        "linhas_com_data": linhas_com_data,
        "linhas_sem_data": linhas_sem_data,
        "classificacao": cls,
        "motivos_classificacao": motivos,
    }
