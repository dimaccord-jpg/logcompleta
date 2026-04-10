"""
Chat Roberto (tela /fretes): resposta analítica governada pelo Cleiton.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from uuid import uuid4

from app.roberto_bi import get_contexto_bi_roberto_upload_only, heatmap_brasil_upload_only
from app.roberto_custo import calcular_custo_robusto_rs_kg
from app.run_cleiton_processing_governance import cleiton_register_processing_event
from app.run_cleiton_gemini_governance import cleiton_governed_generate_content

logger = logging.getLogger(__name__)

SUGGESTION_META_PREFIX = "[[ROBERTO_SUGGESTION::"
FLOW_TYPE_ROBERTO_CHAT = "roberto_chat_fretes"
API_KEY_LABEL_ROBERTO = "GEMINI_API_KEY_ROBERTO"
FLOW_TYPE_ROBERTO_CHAT_SNAPSHOT = "roberto_chat_snapshot"

ROBERTO_CHAT_SYSTEM_PROMPT = """
Você é Roberto Santos, gerente de análises e logística do Agentefrete.

Perfil:
- Profissional maduro, sereno, objetivo e preciso.
- Especialista em BI, fretes, logística, mercado financeiro aplicado à logística e modelos preditivos.
- Fala com clareza, calma e assertividade.
- Não usa gírias, diminutivos, exageros ou linguagem informal.

Missão:
Responder somente com base no contexto analítico fornecido nesta conversa sobre a base de fretes do usuário.
Seu foco é interpretar os dados, destacar padrões, riscos, oportunidades, desvios relevantes e resumir cenários com clareza executiva.
Quando solicitado, redigir um e-mail executivo com base na análise.

Regras:
- Responda apenas com base nos dados e análises fornecidos.
- Não invente números, fatos, causas ou certezas.
- Se faltar dado, diga isso de forma objetiva.
- Não refaça cálculos técnicos no texto.
- Não trate temas fora de fretes, logística, supply chain e análise correlata.
- Seja econômico em tokens.
- Priorize conclusão analítica primeiro, sustentação depois.
- Não faça recomendações de decisão ao usuário.
- Não prescreva ação como se a decisão fosse sua.
- Não use linguagem impositiva.
- Quando mencionar caminhos possíveis, apresente-os apenas como opções de avaliação.
- Quando o usuário pedir um e-mail executivo, devolva exatamente nestes blocos:
  Assunto:
  Saudação:
  Corpo:
  Encerramento:

Estilo:
- Direto ao ponto.
- Tom executivo, claro e profissional.
- Frases curtas e úteis.
- Sem apresentações repetidas.
""".strip()


def _get_client():
    key = (os.getenv("GEMINI_API_KEY_ROBERTO") or "").strip()
    if not key:
        return None
    try:
        from google import genai
        from google.genai import types as genai_types

        timeout_ms = 30_000
        raw = (os.getenv("GEMINI_HTTP_TIMEOUT_MS") or "").strip()
        if raw:
            try:
                timeout_ms = max(1_000, int(raw))
            except ValueError:
                pass
        return genai.Client(
            api_key=key,
            http_options=genai_types.HttpOptions(timeout=timeout_ms),
        )
    except Exception as e:
        logger.error("Chat Roberto: falha ao inicializar cliente Gemini: %s", e)
        return None


def _get_model_candidates() -> list[str]:
    candidates = [
        (os.getenv("GEMINI_MODEL_FRETE") or "").strip(),
        (os.getenv("GEMINI_MODEL_TEXT") or "").strip(),
        "gemini-2.5-flash",
        "gemini-1.5-flash",
    ]
    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _safe_float(val) -> float | None:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _aggregate_top_uf(unidos: list[dict], limit: int = 3) -> list[dict]:
    groups: dict[str, dict] = defaultdict(lambda: {"peso": 0.0, "valor": 0.0, "linhas": 0})
    for r in unidos:
        uf = (r.get("uf_destino") or "").strip().upper()[:2]
        peso = _safe_float(r.get("peso_real")) or 0.0
        valor = _safe_float(r.get("valor_frete_total")) or 0.0
        if not uf or peso <= 0:
            continue
        groups[uf]["peso"] += peso
        groups[uf]["valor"] += valor
        groups[uf]["linhas"] += 1
    out = []
    for uf, g in groups.items():
        custo = (g["valor"] / g["peso"]) if g["peso"] > 0 else None
        out.append(
            {
                "uf": uf,
                "custo_rs_kg": round(custo, 4) if custo is not None else None,
                "linhas": g["linhas"],
            }
        )
    out.sort(key=lambda x: (x["custo_rs_kg"] is None, -(x["custo_rs_kg"] or 0.0)))
    return out[:limit]


def _aggregate_top_modal(unidos: list[dict], limit: int = 3) -> list[dict]:
    groups: dict[str, dict] = defaultdict(lambda: {"peso": 0.0, "valor": 0.0, "linhas": 0})
    for r in unidos:
        modal = (r.get("modal") or "outros").strip().lower() or "outros"
        peso = _safe_float(r.get("peso_real")) or 0.0
        valor = _safe_float(r.get("valor_frete_total")) or 0.0
        if peso <= 0:
            continue
        groups[modal]["peso"] += peso
        groups[modal]["valor"] += valor
        groups[modal]["linhas"] += 1
    out = []
    for modal, g in groups.items():
        custo = (g["valor"] / g["peso"]) if g["peso"] > 0 else None
        out.append(
            {
                "modal": modal,
                "custo_rs_kg": round(custo, 4) if custo is not None else None,
                "linhas": g["linhas"],
            }
        )
    out.sort(key=lambda x: (x["custo_rs_kg"] is None, -(x["custo_rs_kg"] or 0.0)))
    return out[:limit]


def _aggregate_modal_share(unidos: list[dict], limit: int = 5) -> list[dict]:
    weighted_by_modal: dict[str, float] = defaultdict(float)
    total = 0.0
    for r in unidos:
        modal = (r.get("modal") or "outros").strip().lower() or "outros"
        peso_real = _safe_float(r.get("peso_real")) or 0.0
        peso_registro = _safe_float(r.get("peso_registro")) or 0.0
        weighted = peso_real * peso_registro
        if weighted <= 0:
            continue
        weighted_by_modal[modal] += weighted
        total += weighted
    if total <= 0:
        return []
    out = [
        {
            "modal": modal,
            "percentual": round((value / total) * 100.0, 2),
        }
        for modal, value in weighted_by_modal.items()
    ]
    out.sort(key=lambda x: -x["percentual"])
    return out[:limit]


def _aggregate_critical_periods(serie: dict, limit: int = 3) -> list[dict]:
    meses = list(serie.get("meses") or [])
    vals = list(serie.get("valores") or [])
    items = []
    for i, m in enumerate(meses):
        try:
            v = float(vals[i])
        except (TypeError, ValueError, IndexError):
            continue
        items.append({"mes": m, "custo_rs_kg": round(v, 4)})
    items.sort(key=lambda x: -x["custo_rs_kg"])
    return items[:limit]


def _infer_trend(serie: dict) -> str:
    vals = [v for v in (serie.get("valores") or []) if isinstance(v, (int, float))]
    if len(vals) < 2:
        return "insuficiente"
    first = float(vals[0])
    last = float(vals[-1])
    if abs(last - first) < 1e-6:
        return "estavel"
    return "alta" if last > first else "queda"


def _resolve_execution_id(execution_id: str | None = None) -> str:
    raw = (execution_id or "").strip()
    if not raw:
        raw = str(uuid4())
    return raw[:120]


def _summarize_dispersion(unidos: list[dict]) -> dict:
    pontos = []
    for r in unidos:
        peso = _safe_float(r.get("peso_real")) or 0.0
        valor = _safe_float(r.get("valor_frete_total")) or 0.0
        if peso <= 0:
            continue
        pontos.append({"peso": peso, "custo_rs_kg": valor / peso})
    if not pontos:
        return {"quantidade_pontos": 0}
    pesos = sorted(p["peso"] for p in pontos)
    custos = sorted(p["custo_rs_kg"] for p in pontos)
    mid = len(pontos) // 2
    return {
        "quantidade_pontos": len(pontos),
        "peso_kg_min": round(pesos[0], 2),
        "peso_kg_mediana": round(pesos[mid], 2),
        "peso_kg_max": round(pesos[-1], 2),
        "custo_rs_kg_min": round(custos[0], 4),
        "custo_rs_kg_mediana": round(custos[mid], 4),
        "custo_rs_kg_max": round(custos[-1], 4),
    }


def _summarize_heatmap_upload_only() -> dict:
    data = heatmap_brasil_upload_only() or {}
    ufs = list(data.get("ufs") or [])
    valores = list(data.get("valores") or [])
    niveis = list(data.get("nivel_temperatura") or [])
    tendencias = list(data.get("tendencia_alta") or [])
    itens = []
    for idx, uf in enumerate(ufs):
        itens.append(
            {
                "uf": uf,
                "nivel_temperatura": niveis[idx] if idx < len(niveis) else "neutro",
                "tendencia_alta": bool(tendencias[idx]) if idx < len(tendencias) else False,
                "intensidade": valores[idx] if idx < len(valores) else None,
            }
        )
    ufs_em_alta = [x["uf"] for x in itens if x["tendencia_alta"]]
    ufs_sem_alta = [x["uf"] for x in itens if not x["tendencia_alta"]]
    return {
        "ufs_em_alta": ufs_em_alta,
        "ufs_sem_alta": ufs_sem_alta[:5],
        "detalhes_top_ufs": itens[:8],
    }


def _build_roberto_snapshot(ctx: dict) -> dict:
    serie = dict(ctx.get("serie_temporal") or {})
    qualidade_base = dict(ctx.get("qualidade_base") or {})
    recomendacoes = list(ctx.get("recomendacoes_analise") or [])
    unidos = list(ctx.get("unidos") or [])
    custo_medio = calcular_custo_robusto_rs_kg(unidos) if unidos else None
    pontos_uf = _aggregate_top_uf(unidos)
    pontos_modal = _aggregate_top_modal(unidos)
    modal_share = _aggregate_modal_share(unidos)
    pontos_periodo = _aggregate_critical_periods(serie)
    dispersao = _summarize_dispersion(unidos)
    heatmap = _summarize_heatmap_upload_only()

    rec_compacto = []
    for r in recomendacoes[:3]:
        rec_compacto.append(
            {
                "nivel": r.get("nivel"),
                "tipo": r.get("tipo"),
                "mensagem": r.get("mensagem"),
            }
        )

    return {
        "upload_ativo": True,
        "origem_upload_selecionada": None,
        "custo_medio_rs_kg": round(float(custo_medio), 4) if custo_medio is not None else None,
        "tendencia_serie_observada": _infer_trend(serie),
        "qualidade_base": {
            "classificacao": qualidade_base.get("classificacao"),
            "n_registros_validos": qualidade_base.get("n_registros_validos"),
            "n_meses_cobertos": qualidade_base.get("n_meses_cobertos"),
            "pct_peso_muito_baixo": qualidade_base.get("pct_peso_muito_baixo"),
        },
        "graficos_tela": {
            "custo_medio_periodo": {
                "apresenta": "custo medio robusto do periodo analisado em R$/kg",
                "valor_rs_kg": round(float(custo_medio), 4) if custo_medio is not None else None,
            },
            "serie_temporal_previsao": {
                "apresenta": "evolucao do custo medio observado por mes e previsao futura da mesma serie",
                "meses_observados": list(serie.get("meses") or []),
                "previsao_meses": list(serie.get("previsao_meses") or []),
                "tendencia_observada": _infer_trend(serie),
            },
            "ranking_ufs_destino": {
                "apresenta": "ranking de UFs de destino por custo medio em R$/kg nesta base",
                "top_ufs": pontos_uf,
            },
            "heatmap_brasil_ufs_destino": {
                "apresenta": (
                    "mapa do Brasil por UF de destino com tendencia prevista para os proximos 6 meses; "
                    "a leitura vai de frio para quente e e relativa as UFs desta propria base"
                ),
                "ufs_em_alta": heatmap.get("ufs_em_alta"),
                "ufs_sem_alta": heatmap.get("ufs_sem_alta"),
                "detalhes_top_ufs": heatmap.get("detalhes_top_ufs"),
            },
            "proporcao_por_modal": {
                "apresenta": "proporcao percentual dos fretes por modal nesta base, em grafico de rosca",
                "top_modais_percentual": modal_share,
            },
            "dispersao_peso_x_custo": {
                "apresenta": "dispersao dos embarques por peso em kg no eixo x e custo em R$/kg no eixo y",
                "resumo": dispersao,
            },
            "qualidade_base": {
                "apresenta": "qualidade da base analisada, cobrindo volume valido, meses cobertos e sinais de distorcao",
                "classificacao": qualidade_base.get("classificacao"),
            },
            "recomendacoes_analise": {
                "apresenta": "alertas e leituras analiticas derivadas da qualidade da base e da previsao",
                "itens": rec_compacto,
            },
        },
        "recomendacoes_analiticas": rec_compacto,
        "pontos_criticos": {
            "periodo": pontos_periodo,
            "uf_destino": pontos_uf,
            "modal": pontos_modal,
        },
    }


def _register_snapshot_processing_event(
    *,
    status: str,
    rows_processed: int,
    processing_time_ms: int,
    execution_id: str,
    error_summary: str | None = None,
) -> None:
    try:
        cleiton_register_processing_event(
            agent="roberto",
            flow_type=FLOW_TYPE_ROBERTO_CHAT_SNAPSHOT,
            processing_type="non_llm",
            rows_processed=max(0, int(rows_processed)),
            processing_time_ms=max(0, int(processing_time_ms)),
            status=status,
            error_summary=error_summary,
            execution_id=execution_id,
        )
    except Exception as e:
        logger.warning("Chat Roberto: falha ao registrar processing event do snapshot: %s", e)


def _extract_suggestion_metadata(user_message: str) -> tuple[str, dict]:
    text = (user_message or "").strip()
    if not text.startswith(SUGGESTION_META_PREFIX):
        return text, {}
    end = text.find("]]")
    if end < 0:
        return text, {}
    raw_meta = text[len(SUGGESTION_META_PREFIX):end].strip()
    clean_message = text[end + 2 :].strip()
    meta: dict = {}
    for fragment in raw_meta.split(";"):
        if "=" not in fragment:
            continue
        key, val = fragment.split("=", 1)
        k = key.strip().lower()
        v = val.strip().lower()
        if k:
            meta[k] = v
    return clean_message, meta


def _build_follow_up_suggestions(user_message: str) -> list[str]:
    txt = (user_message or "").lower()
    suggestions = []
    if any(k in txt for k in ("custo", "tend", "desvio", "risco")):
        suggestions.append("Quer que eu compare os principais desvios por UF e período?")
    if any(k in txt for k in ("modal", "rodovi", "aereo", "marit")):
        suggestions.append("Quer um resumo executivo por modal com riscos e oportunidades?")
    if any(k in txt for k in ("email", "e-mail", "diretoria", "executivo")):
        suggestions.append("Posso gerar um e-mail executivo com a análise consolidada.")
    suggestions.append("Deseja continuidade da análise focando os 3 pontos críticos da base?")
    # Remove duplicados, mantém até 3.
    uniq = []
    for s in suggestions:
        if s not in uniq:
            uniq.append(s)
        if len(uniq) >= 3:
            break
    return uniq


def _build_prompt_contents(history_slice: list, user_message: str, snapshot: dict, meta: dict) -> str:
    parts = [
        ROBERTO_CHAT_SYSTEM_PROMPT,
        "\n\nContexto analítico canônico do Roberto (resumo compacto):\n",
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
        "\n\nConversa recente:\n",
    ]
    for msg in history_slice:
        role = (msg.get("role") or "user").lower()
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        label = "Usuário" if role == "user" else "Roberto"
        parts.append(f"{label}: {content}\n")
    if meta.get("source") == "proactive_chip":
        parts.append(
            "\nInstrução de interação: a última entrada veio de sugestão proativa do chat; "
            "responda direto sem pedir reconfirmação desnecessária.\n"
        )
    parts.append(f"\nUsuário: {user_message}\nRoberto:")
    return "".join(parts)


def chat_roberto_reply(
    user_message: str,
    history: list,
    max_history: int = 10,
    *,
    execution_id: str | None = None,
) -> dict:
    fallback = "Não consegui concluir a análise agora. Tente novamente em instantes."
    exec_id = _resolve_execution_id(execution_id)
    clean_user_message, meta = _extract_suggestion_metadata(user_message)
    if not clean_user_message.strip():
        return {
            "reply": "Envie sua pergunta sobre fretes e análise logística para eu interpretar o cenário.",
            "suggestions": _build_follow_up_suggestions(""),
        }

    t0_snapshot = time.perf_counter()
    try:
        ctx_upload_only = get_contexto_bi_roberto_upload_only()
    except Exception as e:
        _register_snapshot_processing_event(
            status="failure",
            rows_processed=0,
            processing_time_ms=int((time.perf_counter() - t0_snapshot) * 1000),
            execution_id=exec_id,
            error_summary=f"Falha ao montar contexto upload-only: {e}",
        )
        return {
            "reply": "Não foi possível montar o contexto analítico do upload agora. Tente novamente em instantes.",
            "suggestions": _build_follow_up_suggestions(clean_user_message),
        }
    if not ctx_upload_only:
        return {
            "reply": (
                "Para iniciar o Chat Roberto, envie sua base em Excel no upload desta tela. "
                "Sem upload ativo, não há contexto analítico do seu frete para eu interpretar."
            ),
            "suggestions": [
                "Envie o Excel de fretes para iniciar a análise.",
                "Depois do upload, posso destacar riscos e desvios por período, UF e modal.",
            ],
            "requires_upload": True,
            "snapshot": {"upload_ativo": False},
        }
    _register_snapshot_processing_event(
        status="success",
        # O chat do Roberto consome o snapshot analitico do BI; nao deve reapropriar
        # as linhas do upload como custo operacional do fluxo conversacional.
        rows_processed=0,
        processing_time_ms=int((time.perf_counter() - t0_snapshot) * 1000),
        execution_id=exec_id,
        error_summary=None,
    )

    client = _get_client()
    if not client:
        return {
            "reply": "Chat Roberto indisponível. Verifique a configuração da GEMINI_API_KEY_ROBERTO.",
            "suggestions": _build_follow_up_suggestions(clean_user_message),
        }

    max_hist = max(0, int(max_history or 0))
    history_list = list(history) if isinstance(history, list) else []
    history_slice = history_list[-max_hist:] if max_hist > 0 else []
    snapshot = _build_roberto_snapshot(ctx_upload_only)
    contents = _build_prompt_contents(history_slice, clean_user_message, snapshot, meta)

    last_error = None
    for model in _get_model_candidates():
        try:
            response = cleiton_governed_generate_content(
                client,
                model=model,
                contents=contents,
                agent="roberto",
                flow_type=FLOW_TYPE_ROBERTO_CHAT,
                api_key_label=API_KEY_LABEL_ROBERTO,
            )
            text = (response.text or "").strip()
            if text:
                return {
                    "reply": text,
                    "suggestions": _build_follow_up_suggestions(clean_user_message),
                    "snapshot": {
                        "upload_ativo": snapshot.get("upload_ativo"),
                        "custo_medio_rs_kg": snapshot.get("custo_medio_rs_kg"),
                    },
                }
            last_error = ValueError("Resposta vazia do modelo")
        except Exception as e:
            last_error = e
            logger.warning("Chat Roberto modelo %s: %s", model, e)

    if last_error:
        logger.exception("Chat Roberto falhou após fallbacks: %s", last_error)
    return {"reply": fallback, "suggestions": _build_follow_up_suggestions(clean_user_message)}
