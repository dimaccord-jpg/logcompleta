"""
Recomendações acionáveis derivadas exclusivamente de qualidade_base e
qualidade_previsao (sem novo diagnóstico paralelo).

Orientativas, não bloqueantes. Regras rastreáveis no código.
"""
from __future__ import annotations

from typing import Any


def _rec(
    tipo: str,
    nivel: str,
    mensagem: str,
    motivo_tecnico: str,
    acao_sugerida: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "tipo": tipo,
        "nivel": nivel,
        "mensagem": mensagem,
        "motivo_tecnico": motivo_tecnico,
    }
    if acao_sugerida:
        out["acao_sugerida"] = acao_sugerida
    return out


def gerar_recomendacoes_analise(
    qualidade_base: dict[str, Any],
    qualidade_previsao: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """
    Gera lista ordenada de recomendações a partir dos dicts já calculados.

    Tipos: base | previsao | dados (dados = cruzamento / leitura integrada).
    Níveis: info | atencao | critica
    """
    out: list[dict[str, Any]] = []
    qb = qualidade_base or {}
    qp = qualidade_previsao if isinstance(qualidade_previsao, dict) else None

    n_reg = int(qb.get("n_registros_validos") or 0)
    n_meses = int(qb.get("n_meses_cobertos") or 0)
    pct_peso_baixo = float(qb.get("pct_peso_muito_baixo") or 0)
    pct_top3 = float(qb.get("pct_concentracao_top3_meses") or 0)
    cls_b = str(qb.get("classificacao") or "").lower()
    med = qb.get("mediana_rs_kg_embarque")
    p95 = qb.get("percentil_95_rs_kg_embarque")
    limiar = float(qb.get("limiar_peso_muito_baixo_kg") or 1.0)
    min_lm = int(qb.get("min_linhas_por_mes") or 0)
    sem_data = int(qb.get("linhas_sem_data") or 0)
    com_data = int(qb.get("linhas_com_data") or 0)

    # --- Base (métricas de qualidade_base) ---
    if n_reg > 0 and n_reg < 30:
        out.append(
            _rec(
                "base",
                "atencao",
                "Amplie a base de fretes analisada antes de decisões fortes.",
                f"n_registros_validos={n_reg} (< 30, regra de recomendação ETAPA 9)",
                "Incluir mais embarques históricos ou agregar novos períodos na mesma origem (upload ou base ouro).",
            )
        )

    if n_reg > 0 and n_meses < 6:
        out.append(
            _rec(
                "base",
                "atencao",
                "A janela temporal coberta é curta; tendências podem não ser representativas.",
                f"n_meses_cobertos={n_meses} (< 6)",
                "Ampliar o intervalo de datas (mais meses com emissão) na mesma origem de dados.",
            )
        )

    if n_meses >= 1 and min_lm < 2:
        out.append(
            _rec(
                "base",
                "critica",
                "Há mês(es) com volume muito baixo de embarques.",
                f"min_linhas_por_mes={min_lm} (< 2)",
                "Priorizar meses com mais dados ou consolidar análises por recorte (ex.: UF/modal) quando fizer sentido.",
            )
        )

    if pct_top3 > 75 and com_data > 0:
        nivel = "critica" if pct_top3 > 85 else "atencao"
        out.append(
            _rec(
                "base",
                nivel,
                "A base está muito concentrada em poucos meses de emissão.",
                f"pct_concentracao_top3_meses={pct_top3:.1f}% (> 75%; regra crítica se > 85%)",
                "Buscar distribuição mais uniforme no tempo ou interpretar indicadores no contexto desse pico sazonal.",
            )
        )

    if pct_peso_baixo > 10:
        nivel = "critica" if pct_peso_baixo > 20 else "atencao"
        out.append(
            _rec(
                "base",
                nivel,
                "Muitos embarques com peso muito baixo (risco de ruído ou unidade divergente).",
                f"pct_peso_muito_baixo={pct_peso_baixo:.1f}% (limiar peso < {limiar:g} kg)",
                "Revisar cadastro de peso (kg), arredondamentos e unidade de medida nos sistemas de origem.",
            )
        )

    if med is not None and p95 is not None and float(med) > 1e-9:
        ratio = float(p95) / float(med)
        if ratio > 3.0:
            out.append(
                _rec(
                    "base",
                    "atencao",
                    "Grande dispersão de R$/kg entre embarques (caudas longas).",
                    f"P95/mediana={ratio:.2f} (mediana={med}, P95={p95})",
                    "Segmentar por rota, cliente, modal ou faixa de peso antes de conclusões agregadas.",
                )
            )

    if n_reg > 0 and com_data > 0 and (100.0 * sem_data / n_reg) > 15:
        out.append(
            _rec(
                "base",
                "atencao",
                "Parte relevante dos registros não tem data de emissão válida.",
                f"linhas_sem_data={sem_data} sobre n_registros_validos={n_reg}",
                "Corrigir preenchimento de data_emissao na origem para melhor série mensal e previsão.",
            )
        )

    if cls_b == "baixa" and n_reg > 0:
        out.append(
            _rec(
                "base",
                "critica",
                "A qualidade global da base está classificada como baixa.",
                f"classificacao_qualidade_base={cls_b}",
                "Tratar indicadores como exploratórios; priorizar as ações listadas acima conforme o caso.",
            )
        )

    # --- Previsão (métricas de qualidade_previsao do motor) ---
    if qp:
        cls_p = str(qp.get("classificacao_confiabilidade") or "").lower()
        piso = int(qp.get("meses_previstos_com_piso_zero") or 0)
        cv = qp.get("coeficiente_variacao_observado")
        razao_rmse = qp.get("razao_rmse_media")

        if cls_p == "baixa":
            out.append(
                _rec(
                    "previsao",
                    "critica",
                    "Confiabilidade da previsão classificada como baixa: use projeção com cautela.",
                    "classificacao_confiabilidade=baixa (motor; ETAPA 5)",
                    "Cruzar com contexto de mercado e, se possível, enriquecer a base antes de decisões.",
                )
            )
        elif cls_p == "media":
            out.append(
                _rec(
                    "previsao",
                    "atencao",
                    "Confiabilidade da previsão intermediária: valide tendências com outras fontes.",
                    "classificacao_confiabilidade=media",
                    "Preferir intervalos e cenários, não ponto único, em decisões sensíveis.",
                )
            )

        if piso >= 3:
            nivel = "critica" if piso >= 4 else "atencao"
            out.append(
                _rec(
                    "previsao",
                    nivel,
                    "Vários meses futuros da projeção encostaram no piso zero (domínio R$/kg).",
                    f"meses_previstos_com_piso_zero={piso}",
                    "Revisar consistência histórica, tendência forte negativa ou necessidade de recorte de período na base.",
                )
            )

        if cv is not None and float(cv) > 0.45:
            out.append(
                _rec(
                    "previsao",
                    "atencao",
                    "Alta volatilidade na série observada usada pelo modelo.",
                    f"coeficiente_variacao_observado={float(cv):.4f} (> 0.45)",
                    "Considerar segmentação ou suavização interpretativa; evitar extrapolação além do necessário.",
                )
            )

        if razao_rmse is not None and float(razao_rmse) > 0.35:
            out.append(
                _rec(
                    "previsao",
                    "atencao",
                    "Ajuste da reta à série suavizada apresenta erro relativo elevado.",
                    f"razao_rmse_media={float(razao_rmse):.4f} (> 0.35)",
                    "Interpretar previsão como tendência aproximada, não valor pontual preciso.",
                )
            )

    # --- Cruzamento ---
    if qp and cls_b == "baixa" and str(qp.get("classificacao_confiabilidade") or "").lower() == "baixa":
        out.append(
            _rec(
                "dados",
                "critica",
                "Base frágil e previsão pouco confiável ao mesmo tempo: risco analítico elevado.",
                "classificacao_qualidade_base=baixa e classificacao_confiabilidade=baixa",
                "Postergar decisões irreversíveis ou exigir dados adicionais / validação externa.",
            )
        )

    if not out:
        if n_reg == 0:
            out.append(
                _rec(
                    "base",
                    "atencao",
                    "Não há registros com peso > 0 na origem atual para recomendações de base.",
                    "n_registros_validos=0",
                    "Carregar dados (upload) ou garantir disponibilidade da base histórica (frete_real).",
                )
            )
        else:
            out.append(
                _rec(
                    "dados",
                    "info",
                    "Nenhum alerta adicional além dos indicadores já exibidos; mantenha monitoramento periódico.",
                    "nenhuma condição de recomendação ETAPA 9 acionada",
                )
            )

    return out
