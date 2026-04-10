"""
Modelo estatístico para previsão de custo de frete (R$/kg) na rota.
Produz previsão numérica, intervalo de confiança e métricas de erro (RMSE, MAE).
O LLM (Gemini) usa apenas esses resultados para explicar e contextualizar.

A regressão linear usa uma série mensal suavizada (EMA) derivada do histórico observado;
o retorno continua expondo o histórico observado real em serie_historica_*.
"""
import logging
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from app.roberto_custo import calcular_custo_robusto_rs_kg
from app.services.roberto_config_service import get_roberto_config

logger = logging.getLogger(__name__)

MESES_PREVISAO = 6
# EMA sobre a série mensal observada antes da regressão (apenas entrada do modelo; não substitui o passado exibido)
ALPHA_EMA_SERIE = 0.30


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
    Agrega histórico por mês: (ano-mês, custo_medio_rs_kg robusto).
    Cada item de historico deve ter: valor, peso, e opcionalmente data_emissao.
    O custo mensal usa a mesma função central que o BI (trimmed mean / mediana).
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
        custo = calcular_custo_robusto_rs_kg(rows)
        if custo is not None:
            out.append((mes, custo))
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


def _suavizar_serie_ema(
    valores_observados: list[float],
    alpha: float = ALPHA_EMA_SERIE,
) -> list[float]:
    """
    Suavização exponencial simples (EMA) na ordem temporal dos meses.

    s[0] = y[0]; s[t] = alpha * y[t] + (1 - alpha) * s[t-1].

    Preserva comprimento e ordem; não altera a lista de entrada.
    Usada só como entrada da regressão — o histórico exibido permanece em valores_observados.
    """
    if not valores_observados:
        return []
    if not (0.0 < alpha <= 1.0):
        alpha = ALPHA_EMA_SERIE
    out: list[float] = [float(valores_observados[0])]
    one_m = 1.0 - alpha
    for i in range(1, len(valores_observados)):
        out.append(alpha * float(valores_observados[i]) + one_m * out[i - 1])
    return out


def _rmse_mae(y_real: list[float], y_pred: list[float]) -> tuple[float, float]:
    if not y_real or len(y_real) != len(y_pred):
        return (0.0, 0.0)
    n = len(y_real)
    sq = sum((a - b) ** 2 for a, b in zip(y_real, y_pred))
    ae = sum(abs(a - b) for a, b in zip(y_real, y_pred))
    return ((sq / n) ** 0.5, ae / n)


def _volatilidade_serie_observada(
    valores_observados: list[float],
) -> tuple[float, float | None]:
    """
    Desvio padrão amostral (statistics.stdev) e coeficiente de variação (dp / média)
    da série mensal observada. CV indefinido se média ≈ 0.
    """
    if len(valores_observados) < 2:
        return (0.0, None)
    mu = statistics.mean(valores_observados)
    dp = statistics.stdev(valores_observados)
    cv = (dp / mu) if abs(mu) > 1e-9 else None
    return (dp, cv)


def _contagem_linhas_por_mes(historico: list[dict]) -> dict[str, int]:
    """Quantidade de registros válidos por mês-calendário (mesma lógica de data de _agregar_por_mes)."""
    counts: dict[str, int] = defaultdict(int)
    for r in historico:
        peso = float(r.get("peso") or 0)
        if peso <= 0:
            continue
        data = _parse_data(r.get("data_emissao"))
        if data is None:
            continue
        counts[data.strftime("%Y-%m")] += 1
    return dict(counts)


def _classificar_confiabilidade_previsao(
    *,
    n_meses: int,
    cv_observado: float | None,
    rmse: float,
    media_observada: float,
    meses_com_piso_zero: int,
    min_linhas_em_um_mes: int,
    min_linhas_mes_modelo: int,
) -> tuple[str, list[str], dict[str, Any]]:
    """
    Classificação explícita alta | media | baixa.

    Regras (ordem de severidade; motivos listam o que piorou o nível):
    - baixa: n_meses < 5; ou min_linhas_em_um_mes < min_linhas_mes_modelo; ou meses_com_piso_zero >= 4;
             ou CV > 0,55 quando definido; ou (rmse / max(media,1e-9)) > 0,45
    - alta: n_meses >= 12 e min_linhas >= min_linhas_mes_modelo e meses_com_piso_zero <= 1
            e CV definido e CV <= 0,28 e razão_rmse <= 0,22
    - media: demais casos com previsão válida
    """
    motivos: list[str] = []
    media_safe = max(abs(media_observada), 1e-9)
    razao_rmse = rmse / media_safe

    # Baixa: qualquer condição forte
    if n_meses < 5:
        motivos.append("Menos de 5 meses com custo agregado (histórico curto).")
    if min_linhas_em_um_mes < min_linhas_mes_modelo:
        motivos.append(
            f"Algum mês tem menos de {min_linhas_mes_modelo} registros "
            "(base mensal muito esparsa para o modelo)."
        )
    if meses_com_piso_zero >= 4:
        motivos.append("Piso zero aplicado na maioria dos meses previstos (projeção instável ou tendência negativa).")
    if cv_observado is not None and cv_observado > 0.55:
        motivos.append("Coeficiente de variação da série observada acima de 0,55 (alta volatilidade relativa).")
    if razao_rmse > 0.45:
        motivos.append("RMSE do ajuste à série suavizada elevado em relação à média observada (ajuste fraco).")

    if motivos:
        return ("baixa", motivos, {"razao_rmse_ajuste": round(razao_rmse, 6)})

    # Alta: todas as condições simultâneas
    if (
        n_meses >= 12
        and min_linhas_em_um_mes >= min_linhas_mes_modelo
        and meses_com_piso_zero <= 1
        and cv_observado is not None
        and cv_observado <= 0.28
        and razao_rmse <= 0.22
    ):
        return (
            "alta",
            [
                "Histórico com pelo menos 12 meses, registros mínimos por mês adequados, "
                "baixa volatilidade relativa, RMSE contido e pouco uso de piso zero na projeção."
            ],
            {"razao_rmse_ajuste": round(razao_rmse, 6)},
        )

    # Média
    motivos_media: list[str] = []
    if n_meses < 12:
        motivos_media.append("Menos de 12 meses de histórico.")
    if min_linhas_em_um_mes < min_linhas_mes_modelo:
        motivos_media.append(
            f"Algum mês com menos de {min_linhas_mes_modelo} registros."
        )
    if meses_com_piso_zero > 1:
        motivos_media.append("Piso zero em mais de um mês previsto.")
    if cv_observado is not None and cv_observado > 0.28:
        motivos_media.append("Volatilidade relativa acima do limiar para classificação alta.")
    if razao_rmse > 0.22:
        motivos_media.append("RMSE relativo acima do limiar para classificação alta.")
    if not motivos_media:
        motivos_media.append("Critérios intermediários entre alta e baixa.")
    return ("media", motivos_media, {"razao_rmse_ajuste": round(razao_rmse, 6)})


def _valor_previsao_rs_kg_final(valor_modelo: float) -> float:
    """
    Domínio de negócio: frete previsto em R$/kg não pode ser negativo.
    Aplica-se somente à projeção futura (não à série histórica observada).
    """
    return max(0.0, float(valor_modelo))


def _limitar_historico_por_mes(
    historico: list[dict],
    max_linhas_mes: int,
) -> list[dict]:
    limite = max(1, int(max_linhas_mes))
    por_mes: dict[str, list[dict]] = defaultdict(list)
    for r in historico:
        data = _parse_data(r.get("data_emissao"))
        if data is None:
            continue
        por_mes[data.strftime("%Y-%m")].append(r)
    if not por_mes:
        return historico

    out: list[dict] = []
    for mes in sorted(por_mes.keys()):
        rows = por_mes[mes]
        rows_ordenadas = sorted(
            rows,
            key=lambda x: (_parse_data(x.get("data_emissao")) or datetime.min),
            reverse=True,
        )
        out.extend(rows_ordenadas[:limite])
    return out


def prever(
    historico: list[dict],
    indices_completos: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Previsão numérica de custo médio (R$/kg) para os próximos MESES_PREVISAO meses.

    :param historico: lista de dicts com valor, peso e opcionalmente data_emissao (e modal).
    :param indices_completos: dict com chave 'historico' (lista de {data, dolar, petroleo, bdi, fbx}).
                              Usado no futuro para modelos multivariados; hoje a previsão é só série temporal.
    :return: dict com previsao_numerica, intervalo_confianca, metrica_erro, tendencia_macro,
             serie_historica_meses, serie_historica_valores e qualidade_previsao (metadados auditáveis;
             não alteram valores previstos).

    Previsão futura (valores_rs_kg e IC) passa por piso zero centralizado: frete previsto ≥ 0.

    Regressão e projeção usam série mensal suavizada (EMA); serie_historica_* permanece observada.
    """
    cfg = get_roberto_config()
    historico = _limitar_historico_por_mes(historico, cfg.max_linhas_mes_modelo)
    serie = _agregar_por_mes(historico)
    meses_serie = [s[0] for s in serie]
    valores_serie = [s[1] for s in serie]
    contagem_mes = _contagem_linhas_por_mes(historico)
    min_linhas_mes = min(contagem_mes.values()) if contagem_mes else 0
    max_linhas_mes = max(contagem_mes.values()) if contagem_mes else 0

    if len(serie) < 2:
        dp_obs, cv_obs = _volatilidade_serie_observada(valores_serie)
        media_obs = statistics.mean(valores_serie) if valores_serie else None
        return {
            "previsao_numerica": None,
            "intervalo_confianca": None,
            "metrica_erro": {"rmse": None, "mae": None, "n_amostras": len(historico), "n_meses": len(serie)},
            "tendencia_macro": "Estabilidade",
            "serie_historica_meses": meses_serie,
            "serie_historica_valores": [round(v, 4) for v in valores_serie],
            "qualidade_previsao": {
                "n_meses_observados": len(serie),
                "n_meses_regressao": len(serie),
                "n_meses_previstos": 0,
                "n_registros_historico": len(historico),
                "desvio_padrao_observado": round(dp_obs, 6),
                "coeficiente_variacao_observado": None if cv_obs is None else round(cv_obs, 6),
                "media_observada": None if media_obs is None else round(float(media_obs), 6),
                "rmse_ajuste_suavizado": None,
                "mae_ajuste_suavizado": None,
                "razao_rmse_media": None,
                "min_linhas_em_um_mes": min_linhas_mes,
                "max_linhas_em_um_mes": max_linhas_mes,
                "meses_previstos_com_piso_zero": 0,
                "inclinacao_regressao_mensal": None,
                "classificacao_confiabilidade": "baixa",
                "motivos_classificacao": [
                    "Menos de 2 meses com custo agregado — previsão não gerada."
                ],
                "detalhes_regras": {},
            },
        }

    n = len(valores_serie)
    x = list(range(n))
    valores_observados = valores_serie
    valores_modelo = _suavizar_serie_ema(valores_observados)
    intercept, slope = _regressao_linear(x, valores_modelo)
    y_pred_in = [intercept + slope * xi for xi in x]
    rmse, mae = _rmse_mae(valores_modelo, y_pred_in)

    # Intervalo de confiança (aproximado): ± 1.96 * RMSE para cada previsão
    residual_std = rmse if rmse > 0 else (sum((a - b) ** 2 for a, b in zip(valores_modelo, y_pred_in)) / max(1, n - 2)) ** 0.5
    ic_half = 1.96 * residual_std

    ultimo_mes = datetime.strptime(meses_serie[-1], "%Y-%m")
    previsao_meses = []
    previsao_valores = []
    ic_inferior = []
    ic_superior = []
    meses_com_piso_zero = 0
    for i in range(1, MESES_PREVISAO + 1):
        prox = ultimo_mes + timedelta(days=32 * i)
        prox = prox.replace(day=1)
        previsao_meses.append(prox.strftime("%Y-%m"))
        val_bruto = intercept + slope * (n + i - 1)
        if val_bruto < 0:
            meses_com_piso_zero += 1
        val = _valor_previsao_rs_kg_final(val_bruto)
        previsao_valores.append(round(val, 4))
        # IC coerente com o valor reportado (após piso); limite inferior não fica abaixo de zero
        ic_inferior.append(round(max(0.0, val - ic_half), 4))
        ic_superior.append(round(val + ic_half, 4))

    # Tendência a partir do slope (em R$/kg por mês)
    if slope > 0.002:
        tendencia_macro = "Tendência de Alta"
    elif slope < -0.002:
        tendencia_macro = "Tendência de Baixa"
    else:
        tendencia_macro = "Estabilidade"

    dp_obs, cv_obs = _volatilidade_serie_observada(valores_observados)
    media_obs = statistics.mean(valores_observados)
    cls, motivos_cls, det_regras = _classificar_confiabilidade_previsao(
        n_meses=n,
        cv_observado=cv_obs,
        rmse=rmse,
        media_observada=media_obs,
        meses_com_piso_zero=meses_com_piso_zero,
        min_linhas_em_um_mes=min_linhas_mes,
        min_linhas_mes_modelo=cfg.min_linhas_mes_modelo,
    )

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
        "qualidade_previsao": {
            "n_meses_observados": n,
            "n_meses_regressao": n,
            "n_meses_previstos": MESES_PREVISAO,
            "n_registros_historico": len(historico),
            "desvio_padrao_observado": round(dp_obs, 6),
            "coeficiente_variacao_observado": None if cv_obs is None else round(cv_obs, 6),
            "media_observada": round(float(media_obs), 6),
            "rmse_ajuste_suavizado": round(rmse, 6),
            "mae_ajuste_suavizado": round(mae, 6),
            "razao_rmse_media": det_regras.get("razao_rmse_ajuste"),
            "min_linhas_em_um_mes": min_linhas_mes,
            "max_linhas_em_um_mes": max_linhas_mes,
            "meses_previstos_com_piso_zero": meses_com_piso_zero,
            "inclinacao_regressao_mensal": round(float(slope), 6),
            "classificacao_confiabilidade": cls,
            "motivos_classificacao": motivos_cls,
            "detalhes_regras": det_regras,
        },
    }
