"""
Modelo estatístico para previsão de custo de frete (R$/kg) na rota.
Produz previsão numérica, intervalo de confiança e métricas de erro (RMSE, MAE).
O LLM (Gemini) usa apenas esses resultados para explicar e contextualizar.
"""
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

MESES_PREVISAO = 6


def _parse_data(d) -> datetime | None:
    """Converte data (date, datetime ou string) para datetime."""
    if d is None:
        return None
    if hasattr(d, "year"):
        return datetime(d.year, d.month, 1) if not isinstance(d, datetime) else d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    try:
        s = str(d).strip()[:10]
        return datetime.strptime(s, "%Y-%m-%d").replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    except Exception:
        return None


def _agregar_por_mes(historico: list[dict]) -> list[tuple[str, float]]:
    """
    Agrega histórico por mês: (ano-mês, custo_medio_rs_kg).
    Cada item de historico deve ter: valor, peso, e opcionalmente data_emissao.
    """
    por_mes: dict[str, list[dict]] = defaultdict(list)
    for r in historico:
        valor = float(r.get("valor") or 0)
        peso = float(r.get("peso") or 0)
        if peso <= 0:
            continue
        data = _parse_data(r.get("data_emissao"))
        if data is None:
            # Sem data: coloca em um mês fictício "sem_data" e ignoramos na série temporal
            continue
        chave = data.strftime("%Y-%m")
        por_mes[chave].append({"valor": valor, "peso": peso})

    out = []
    for mes in sorted(por_mes.keys()):
        rows = por_mes[mes]
        soma_v = sum(x["valor"] for x in rows)
        soma_p = sum(x["peso"] for x in rows)
        if soma_p > 0:
            out.append((mes, soma_v / soma_p))
    return out


def _regressao_linear(x: list[float], y: list[float]) -> tuple[float, float]:
    """Retorna (intercept, slope)."""
    n = len(x)
    if n < 2:
        return (y[0], 0.0) if n == 1 else (0.0, 0.0)
    sx = sum(x)
    sy = sum(y)
    sx2 = sum(xi * xi for xi in x)
    sxy = sum(xi * yi for xi, yi in zip(x, y))
    den = n * sx2 - sx * sx
    if den == 0:
        return (sy / n, 0.0)
    slope = (n * sxy - sx * sy) / den
    intercept = (sy - slope * sx) / n
    return (intercept, slope)


def _rmse_mae(y_real: list[float], y_pred: list[float]) -> tuple[float, float]:
    if not y_real or len(y_real) != len(y_pred):
        return (0.0, 0.0)
    n = len(y_real)
    sq = sum((a - b) ** 2 for a, b in zip(y_real, y_pred))
    ae = sum(abs(a - b) for a, b in zip(y_real, y_pred))
    return ((sq / n) ** 0.5, ae / n)


def prever(
    historico: list[dict],
    indices_completos: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Previsão numérica de custo médio (R$/kg) para os próximos MESES_PREVISAO meses.

    :param historico: lista de dicts com valor, peso e opcionalmente data_emissao (e modal).
    :param indices_completos: dict com chave 'historico' (lista de {data, dolar, petroleo, bdi, fbx}).
                              Usado no futuro para modelos multivariados; hoje a previsão é só série temporal.
    :return: dict com previsao_numerica, intervalo_confianca, metrica_erro, tendencia_macro.
    """
    serie = _agregar_por_mes(historico)
    if len(serie) < 2:
        return {
            "previsao_numerica": None,
            "intervalo_confianca": None,
            "metrica_erro": {"rmse": None, "mae": None, "n_amostras": len(historico), "n_meses": len(serie)},
            "tendencia_macro": "Estabilidade",
        }

    meses_serie = [s[0] for s in serie]
    valores_serie = [s[1] for s in serie]
    n = len(valores_serie)
    x = list(range(n))
    intercept, slope = _regressao_linear(x, valores_serie)
    y_pred_in = [intercept + slope * xi for xi in x]
    rmse, mae = _rmse_mae(valores_serie, y_pred_in)

    # Intervalo de confiança (aproximado): ± 1.96 * RMSE para cada previsão
    residual_std = rmse if rmse > 0 else (sum((a - b) ** 2 for a, b in zip(valores_serie, y_pred_in)) / max(1, n - 2)) ** 0.5
    ic_half = 1.96 * residual_std

    ultimo_mes = datetime.strptime(meses_serie[-1], "%Y-%m")
    previsao_meses = []
    previsao_valores = []
    ic_inferior = []
    ic_superior = []
    for i in range(1, MESES_PREVISAO + 1):
        prox = ultimo_mes + timedelta(days=32 * i)
        prox = prox.replace(day=1)
        previsao_meses.append(prox.strftime("%Y-%m"))
        val = intercept + slope * (n + i - 1)
        previsao_valores.append(round(val, 4))
        ic_inferior.append(round(max(0, val - ic_half), 4))
        ic_superior.append(round(val + ic_half, 4))

    # Tendência a partir do slope (em R$/kg por mês)
    if slope > 0.002:
        tendencia_macro = "Tendência de Alta"
    elif slope < -0.002:
        tendencia_macro = "Tendência de Baixa"
    else:
        tendencia_macro = "Estabilidade"

    return {
        "previsao_numerica": {
            "meses": previsao_meses,
            "valores_rs_kg": previsao_valores,
        },
        "intervalo_confianca": {
            "meses": previsao_meses,
            "inferior": ic_inferior,
            "superior": ic_superior,
        },
        "metrica_erro": {
            "rmse": round(rmse, 6),
            "mae": round(mae, 6),
            "n_amostras": len(historico),
            "n_meses": n,
        },
        "tendencia_macro": tendencia_macro,
        "serie_historica_meses": meses_serie,
        "serie_historica_valores": [round(v, 4) for v in valores_serie],
    }
