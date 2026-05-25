from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
from typing import Any

from flask import has_request_context
from flask_login import current_user

from app.cleide_ai_flags import resolve_cleide_ai_flags
from app.cleide_formatters import (
    format_brl,
    format_date_ptbr,
    format_integer,
    format_percent,
    format_weight,
)
from app.cleide_gemini_adapter import FLOW_TYPE_CLEIDE_AI, generate_cleide_ai_reply
from app.cleide_chat_context import get_cleide_chat_context
from app.cleide_language_policy import (
    normalized_allowed_language,
    normalized_forbidden_language,
    normalized_out_of_scope_language,
)
from app.services.cleiton_operacao_autorizacao_service import (
    avaliar_autorizacao_operacao_por_franquia,
)

logger = logging.getLogger(__name__)

MAX_QUESTION_LEN = 320
MAX_RESPONSE_LEN = 1600
TOP_LIST_LIMIT = 3
HISTORY_MAX_MESSAGES = 10
HISTORY_MAX_CHARS = 900
POLICY_SAFE_FALLBACK_REPLY = "Dados insuficientes. Oportunidade de investigação. Concentração operacional."

CONVERSATIONAL_CONTRACT_VERSION = "cleide_chat_controlled.v1"
CONVERSATIONAL_PHASE = "9.1_hardening_semantic_governance"
TRANSITION_AUDIT_MARKER = "transition_501_placeholder_to_200_controlled_confirmed"
AI_MODE = "controlled_templates_gemini_supervised_phase_12"
CIRCUIT_BREAKER_COOLDOWN_SECONDS = 90
POLICY_BLOCKED_BREAKER_THRESHOLD = 3
REPLY_MIN_DOMAIN_SIGNAL_TOKENS = (
    "frete",
    "fretes",
    "operacional",
    "operacionais",
    "transportadora",
    "transportadoras",
    "uf",
    "ufs",
    "origem",
    "destino",
    "concentracao",
    "variacao",
    "ranking",
    "comparacao",
    "auditoria",
    "investigacao",
    "volume",
    "ticket",
    "periodo",
)
REPLY_BLOCKED_REFERENCES = ("roberto", "julia", "júlia")
REPLY_OUT_OF_DOMAIN_HINTS = (
    "horoscopo",
    "futebol",
    "filme",
    "receita",
    "politica partidaria",
    "fofoca",
)
CONTEXTUAL_FOLLOWUP_SHORT_TERMS = {
    "uf": "uf",
    "e uf": "uf",
    "e a uf": "uf",
    "e o uf": "uf",
    "estado": "uf",
    "e estado": "uf",
    "e o estado": "uf",
    "e por uf": "uf",
    "por uf": "uf",
    "origem": "origem",
    "e origem": "origem",
    "e a origem": "origem",
    "destino": "destino",
    "e destino": "destino",
    "e o destino": "destino",
    "segunda": "ranking_2",
    "e a segunda": "ranking_2",
    "segunda colocada": "ranking_2",
    "segundo": "ranking_2",
    "e o segundo": "ranking_2",
    "terceira": "ranking_3",
    "e terceira": "ranking_3",
    "e a terceira": "ranking_3",
    "terceiro": "ranking_3",
    "e o terceiro": "ranking_3",
    "quarta": "ranking_4",
    "e quarta": "ranking_4",
    "e a quarta": "ranking_4",
    "quarto": "ranking_4",
    "e o quarto": "ranking_4",
    "transportadora": "transportadora",
    "e transportadora": "transportadora",
    "modal": "modal",
    "e modal": "modal",
    "periodo": "periodo",
    "e periodo": "periodo",
    "filtro": "filtro",
    "e filtro": "filtro",
}

_circuit_breaker_lock = threading.Lock()
_circuit_breaker_state = {
    "open_until_monotonic": 0.0,
    "reason": "",
    "policy_blocked_streak": 0,
}


def run_cleide_controlled_chat(
    *,
    question: Any,
    session_obj: Any,
    history: Any = None,
) -> tuple[dict[str, Any], int]:
    flags = resolve_cleide_ai_flags()
    chat_ctx = get_cleide_chat_context(session_obj)
    safe_context = chat_ctx.get("safe_operational_context") if isinstance(chat_ctx, dict) else None
    if not isinstance(safe_context, dict) or not safe_context:
        logger.warning("Cleide chat: contexto indisponivel para resposta controlada.")
        return _fallback(
            "fallback_contexto_indisponivel",
            "Contexto operacional indisponivel no momento. Foco em dados insuficientes.",
            observability=_observability(
                flags=flags,
                ai_used=False,
                fallback_used=True,
                reason="unsafe_context",
                error_code="unsafe_context",
            ),
        ), 200

    raw_question = question if isinstance(question, str) else ""
    normalized_question = _normalize_whitespace(raw_question)
    normalized_history = _normalize_history_entries(history)
    if not normalized_question:
        logger.info("Cleide chat: pergunta invalida (vazia).")
        return _fallback(
            "fallback_pergunta_invalida",
            "Não consegui interpretar a pergunta. Reescreva de forma objetiva com foco em oportunidade de investigação.",
            observability=_observability(
                flags=flags,
                ai_used=False,
                fallback_used=True,
                reason="invalid_question",
                error_code="invalid_question",
            ),
        ), 200
    if len(normalized_question) > MAX_QUESTION_LEN:
        logger.info("Cleide chat: pergunta acima do limite (%s).", len(normalized_question))
        return _fallback(
            "fallback_pergunta_muito_longa",
            f"Pergunta acima do limite de {MAX_QUESTION_LEN} caracteres. Resuma para continuarmos.",
            observability=_observability(
                flags=flags,
                ai_used=False,
                fallback_used=True,
                reason="question_too_long",
                error_code="question_too_long",
            ),
        ), 200

    normalized = _normalize_for_match(normalized_question)
    guardrail_sets = _build_guardrail_sets()
    if _contains_forbidden(normalized, guardrail_sets["forbidden"]):
        logger.warning("Cleide chat: bloqueio semantico por linguagem proibida.")
        return _fallback(
            "fallback_bloqueio_semantico",
            "Não posso responder nessa formulação. Posso ajudar com concentração operacional, variação relevante e oportunidade de investigação.",
            observability=_observability(
                flags=flags,
                ai_used=False,
                fallback_used=True,
                reason="semantic_block_question",
                error_code="semantic_block",
            ),
        ), 200
    if any(ref in normalized for ref in REPLY_BLOCKED_REFERENCES):
        logger.warning("Cleide chat: referencia bloqueada a outro agente.")
        return _fallback(
            "fallback_fora_de_escopo",
            "Estou limitada ao contexto operacional seguro da Cleide e não respondo por Roberto ou Julia. Posso seguir com concentração operacional.",
            observability=_observability(
                flags=flags,
                ai_used=False,
                fallback_used=True,
                reason="blocked_agent_reference",
                error_code="blocked_agent_reference",
            ),
        ), 200
    if any(word in normalized for word in guardrail_sets["out_of_scope"]):
        logger.warning("Cleide chat: pergunta fora de escopo (juridico/financeiro acusatorio).")
        return _fallback(
            "fallback_fora_de_escopo",
            "Estou limitada a contexto operacional seguro e não respondo perguntas jurídicas ou acusatórias. Posso seguir com concentração operacional.",
            observability=_observability(
                flags=flags,
                ai_used=False,
                fallback_used=True,
                reason="out_of_scope_question",
                error_code="out_of_scope",
            ),
        ), 200

    intent = _classify_intent(normalized)
    contextual_followup_reply = ""
    contextual_followup_resolved = False
    if intent != "unknown" and _is_dataset_insufficient(safe_context):
        return _build_success(
            intent="dados_insuficientes",
            reply="Dados insuficientes para leitura operacional consistente neste momento e oportunidade de investigação.",
            chat_ctx=chat_ctx,
            observability=_observability(
                flags=flags,
                ai_used=False,
                fallback_used=False,
                reason="dataset_insufficient",
                error_code="",
            ),
        ), 200
    if intent == "unknown":
        contextual_followup = _resolve_contextual_followup(
            normalized_question=normalized_question,
            history=normalized_history,
            safe_operational_context=safe_context,
        )
        if isinstance(contextual_followup, dict):
            resolved_intent = str(contextual_followup.get("intent") or "").strip()
            if resolved_intent:
                intent = resolved_intent
                contextual_followup_reply = _normalize_whitespace(str(contextual_followup.get("reply") or ""))
                contextual_followup_resolved = True

    authz = _resolve_authz()
    if flags.ai_enabled and not bool(authz.get("permitido")):
        logger.info("Cleide chat: IA desativada por autorizacao/franquia.")
        flags = flags.__class__(
            ai_enabled=False,
            environment=flags.environment,
            selected_flag=flags.selected_flag,
            reason="franquia_not_authorized",
            has_api_key=flags.has_api_key,
            api_key_label=flags.api_key_label,
        )

    if flags.ai_enabled and not contextual_followup_resolved:
        breaker_open, breaker_reason = _breaker_is_open()
        if breaker_open:
            logger.warning("Cleide chat: circuit breaker aberto (%s).", breaker_reason)
            flags = flags.__class__(
                ai_enabled=False,
                environment=flags.environment,
                selected_flag=flags.selected_flag,
                reason="circuit_breaker_open",
                has_api_key=flags.has_api_key,
                api_key_label=flags.api_key_label,
            )

    if intent == "unknown" and not _unknown_intent_can_use_supervised_ai(
        normalized_question=normalized_question,
        history=normalized_history,
        safe_operational_context=safe_context,
        allowed_terms=guardrail_sets["allowed"],
        flags=flags,
    ):
        logger.info("Cleide chat: intent desconhecida sem elegibilidade para IA supervisionada.")
        return _fallback(
            "fallback_intent_desconhecida",
            "Não reconheci essa intent. Posso ajudar com concentração operacional, variação relevante, tendência operacional e dados insuficientes.",
            observability=_observability(
                flags=flags,
                ai_used=False,
                fallback_used=True,
                reason="unknown_intent",
                error_code="unknown_intent",
            ),
        ), 200

    ai_failure_observability: dict[str, Any] | None = None

    if flags.ai_enabled and not contextual_followup_resolved:
        ai_kwargs: dict[str, Any] = {
            "question": normalized_question,
            "safe_operational_context": safe_context,
        }
        if normalized_history:
            ai_kwargs["history"] = normalized_history
        ai_result = generate_cleide_ai_reply(
            **ai_kwargs,
        )
        if ai_result.get("ok"):
            raw_ai_reply = str(ai_result.get("reply") or "")
            safe_reply, truncated = _safe_reply(raw_ai_reply)
            reply_norm = _normalize_for_match(safe_reply)
            policy_eval = _evaluate_reply_policy(
                reply_norm,
                allowed_terms=guardrail_sets["allowed"],
                forbidden_terms=guardrail_sets["forbidden"],
                out_of_scope_terms=guardrail_sets["out_of_scope"],
                safe_operational_context=safe_context,
            )
            if not bool(policy_eval.get("ok")):
                reason_code = str(policy_eval.get("reason_code") or "other")
                _breaker_mark_policy_blocked()
                logger.warning("Cleide chat: resposta IA bloqueada por policy (%s).", reason_code)
                return _fallback(
                    "fallback_bloqueio_semantico",
                    POLICY_SAFE_FALLBACK_REPLY,
                    observability=_observability(
                        flags=flags,
                        ai_used=True,
                        fallback_used=True,
                        policy_blocked=True,
                        policy_block_reason_code=reason_code,
                        policy_warning_reason_code="",
                        provider=str(ai_result.get("provider") or "gemini"),
                        model=str(ai_result.get("model") or ""),
                        latency_ms=int(ai_result.get("latency_ms") or 0),
                        usage=dict(ai_result.get("usage") or {}),
                        error_code="policy_blocked",
                        reason=f"policy_blocked_{reason_code}",
                    ),
                ), 200
            warning_code = str(policy_eval.get("warning_reason_code") or "")
            if warning_code:
                logger.warning("Cleide chat: resposta IA aprovada com warning de policy (%s).", warning_code)
            _breaker_mark_success()
            return _build_success(
                intent=intent,
                reply=safe_reply,
                chat_ctx=chat_ctx,
                response_truncated=truncated,
                mode=AI_MODE,
                observability=_observability(
                    flags=flags,
                    ai_used=True,
                    fallback_used=False,
                    policy_warning_reason_code=warning_code,
                    provider=str(ai_result.get("provider") or "gemini"),
                    model=str(ai_result.get("model") or ""),
                    latency_ms=int(ai_result.get("latency_ms") or 0),
                    usage=dict(ai_result.get("usage") or {}),
                    error_code="",
                    reason="gemini_success",
                ),
            ), 200

        error_code = str(ai_result.get("error_code") or "provider_error")
        reason = str(ai_result.get("reason") or error_code)
        _breaker_mark_failure(error_code=error_code, reason=reason)
        ai_failure_observability = _observability(
            flags=flags,
            ai_used=True,
            fallback_used=True,
            provider=str(ai_result.get("provider") or "gemini"),
            model=str(ai_result.get("model") or ""),
            latency_ms=int(ai_result.get("latency_ms") or 0),
            usage=dict(ai_result.get("usage") or {}),
            error_code=error_code,
            reason=reason,
        )
        logger.warning("Cleide chat: Gemini falhou (%s): %s", error_code, reason)

    if intent == "unknown":
        logger.info("Cleide chat: fallback por intent desconhecida apos tentativa de IA.")
        return _fallback(
            "fallback_intent_desconhecida",
            "Não reconheci essa intent. Posso ajudar com concentração operacional, variação relevante, tendência operacional e dados insuficientes.",
            observability=(
                ai_failure_observability
                if isinstance(ai_failure_observability, dict)
                else _observability(
                    flags=flags,
                    ai_used=False,
                    fallback_used=True,
                    reason="unknown_intent",
                    error_code="unknown_intent",
                )
            ),
        ), 200

    reply = contextual_followup_reply or _reply_for_intent(intent, safe_context)
    if not reply:
        logger.info("Cleide chat: fallback por intent sem dados suficientes (%s).", intent)
        return _build_success(
            intent="dados_insuficientes",
            reply="Dados insuficientes para responder essa pergunta com segurança e oportunidade de investigação.",
            chat_ctx=chat_ctx,
            observability=_observability(
                flags=flags,
                ai_used=False,
                fallback_used=False,
                reason="deterministic_intent_without_reply",
                error_code="",
            ),
        ), 200

    safe_reply, truncated = _safe_reply(reply)
    if _contains_forbidden(_normalize_for_match(safe_reply), guardrail_sets["forbidden"]):
        logger.warning("Cleide chat: resposta bloqueada por linguagem proibida.")
        return _fallback(
            "fallback_bloqueio_semantico",
            "Resposta bloqueada por política de linguagem. Reformule para concentração operacional.",
            observability=_observability(
                flags=flags,
                ai_used=False,
                fallback_used=True,
                reason="deterministic_forbidden_reply",
                error_code="semantic_block",
            ),
        ), 200
    policy_eval = _evaluate_reply_policy(
        _normalize_for_match(safe_reply),
        allowed_terms=guardrail_sets["allowed"],
        forbidden_terms=guardrail_sets["forbidden"],
        out_of_scope_terms=guardrail_sets["out_of_scope"],
        safe_operational_context=safe_context,
    )
    if not bool(policy_eval.get("ok")):
        reason_code = str(policy_eval.get("reason_code") or "other")
        logger.warning("Cleide chat: resposta bloqueada por drift de policy permitida (%s).", reason_code)
        return _fallback(
            "fallback_bloqueio_semantico",
            "Resposta bloqueada por política de linguagem. Reformule para oportunidade de investigação.",
            observability=_observability(
                flags=flags,
                ai_used=False,
                fallback_used=True,
                policy_block_reason_code=reason_code,
                policy_warning_reason_code="",
                reason=f"deterministic_policy_drift_{reason_code}",
                error_code="policy_drift",
            ),
        ), 200
    warning_code = str(policy_eval.get("warning_reason_code") or "")
    if warning_code:
        logger.warning("Cleide chat: resposta deterministica aprovada com warning de policy (%s).", warning_code)
    return _build_success(
        intent=intent,
        reply=safe_reply,
        chat_ctx=chat_ctx,
        response_truncated=truncated,
        observability=_observability(
            flags=flags,
            ai_used=bool(ai_failure_observability),
            fallback_used=bool(ai_failure_observability),
            policy_warning_reason_code=warning_code,
            reason=(
                str(ai_failure_observability.get("reason") or "")
                if isinstance(ai_failure_observability, dict)
                else "deterministic_success"
            ),
            error_code=(
                str(ai_failure_observability.get("error_code") or "")
                if isinstance(ai_failure_observability, dict)
                else ""
            ),
            provider=(
                str(ai_failure_observability.get("provider") or "gemini")
                if isinstance(ai_failure_observability, dict)
                else "gemini"
            ),
            model=(
                str(ai_failure_observability.get("model") or "")
                if isinstance(ai_failure_observability, dict)
                else ""
            ),
            latency_ms=(
                int(ai_failure_observability.get("latency_ms") or 0)
                if isinstance(ai_failure_observability, dict)
                else 0
            ),
            usage=(
                dict(ai_failure_observability.get("token_usage") or {})
                if isinstance(ai_failure_observability, dict)
                else None
            ),
        ),
    ), 200


def _classify_intent(normalized: str) -> str:
    if _contains_any(normalized, ("dados insuficientes", "base insuficiente", "insuficiente")):
        return "dados_insuficientes"
    if _contains_any(normalized, ("resumo operacional", "resumo", "visao geral", "geral da operacao")):
        return "resumo_operacional"
    if _contains_any(normalized, ("top transportadoras", "maiores transportadoras", "ranking transportadoras", "transportadoras")):
        return "top_transportadoras"
    if _contains_any(
        normalized,
        (
            "cidade de embarque",
            "origem do embarque",
            "uf de embarque",
            "embarque",
        ),
    ):
        return "uf_origem"
    if normalized in {"uf", "ufs", "estado", "estados"}:
        return "uf_origem"
    if _contains_any(normalized, ("uf origem", "origem", "estado de origem", "ufs origem")):
        return "uf_origem"
    if _contains_any(normalized, ("uf destino", "destino", "estado de destino", "ufs destino")):
        return "uf_destino"
    if _contains_any(normalized, ("periodo", "periodo dataset", "inicio e fim", "janela temporal")):
        return "periodo_dataset"
    if _contains_any(normalized, ("qualidade dataset", "qualidade da base", "qualidade", "dados invalidos")):
        return "qualidade_dataset"
    if _contains_any(normalized, ("quantidade documentos", "total documentos", "documentos")):
        return "quantidade_documentos"
    if _contains_any(normalized, ("ticket medio", "ticket médio", "ticket")):
        return "ticket_medio"
    if _contains_any(normalized, ("peso total", "peso")):
        return "peso_total"
    if _contains_any(normalized, ("fretes zerados", "percentual fretes zerados", "frete zerado")):
        return "fretes_zerados"
    return "unknown"


def _reply_for_intent(intent: str, safe_context: dict[str, Any]) -> str:
    kpis = safe_context.get("kpis") if isinstance(safe_context.get("kpis"), dict) else {}
    dataset_summary = (
        safe_context.get("dataset_summary") if isinstance(safe_context.get("dataset_summary"), dict) else {}
    )
    quality_flags = safe_context.get("quality_flags") if isinstance(safe_context.get("quality_flags"), dict) else {}
    tables = safe_context.get("aggregate_tables") if isinstance(safe_context.get("aggregate_tables"), dict) else {}

    if intent == "dados_insuficientes":
        return "Dados insuficientes. Oportunidade de investigação. Concentração operacional."
    if intent == "resumo_operacional":
        top_origem = _top_row_label(tables.get("uf_origem"))
        top_destino = _top_row_label(tables.get("uf_destino"))
        return (
            "Resumo operacional da sessao:\n"
            f"- Valor total de frete: {format_brl(kpis.get('valor_total_frete'))}.\n"
            f"- Volume processado: {format_integer(kpis.get('total_documentos'))} documentos e {format_weight(kpis.get('peso_total'))}.\n"
            f"- Ticket medio: {format_brl(kpis.get('ticket_medio_frete'))}.\n"
            f"- UF origem com maior custo: {top_origem}.\n"
            f"- UF destino com maior custo: {top_destino}.\n\n"
            "Concentracao operacional com variacao relevante e oportunidade de investigacao."
        )
    if intent == "top_transportadoras":
        return _build_top_reply(
            rows=tables.get("transportadora"),
            label="transportadoras",
            fallback="Não há dados suficientes para top transportadoras.",
        )
    if intent == "uf_origem":
        return _build_uf_scope_reply(
            origem_rows=tables.get("uf_origem"),
            destino_rows=tables.get("uf_destino"),
            primary_axis="origem",
        )
    if intent == "uf_destino":
        return _build_uf_scope_reply(
            origem_rows=tables.get("uf_origem"),
            destino_rows=tables.get("uf_destino"),
            primary_axis="destino",
        )
    if intent == "periodo_dataset":
        periodo = kpis.get("periodo_dataset") if isinstance(kpis.get("periodo_dataset"), dict) else {}
        inicio = format_date_ptbr(periodo.get("inicio")) if periodo.get("inicio") else "não informado"
        fim = format_date_ptbr(periodo.get("fim")) if periodo.get("fim") else "não informado"
        return (
            f"Tendência operacional {inicio} {fim}. Variação relevante. Oportunidade de investigação."
        )
    if intent == "modal_operacional":
        return _build_top_reply(
            rows=tables.get("modal"),
            label="modal",
            fallback="Dados insuficientes. Oportunidade de investigação. Concentração operacional.",
        )
    if intent == "filtro_operacional":
        filter_context = safe_context.get("filter_context") if isinstance(safe_context.get("filter_context"), dict) else {}
        active_filters = _safe_active_filters(filter_context.get("active_filters"))
        if active_filters:
            return (
                f"Concentração operacional com {format_integer(len(active_filters))} filtros ativos. "
                "Variação relevante. Oportunidade de investigação."
            )
        return "Concentração operacional em visão global sem filtro ativo. Variação relevante. Oportunidade de investigação."
    if intent == "qualidade_dataset":
        invalid_numeric = format_integer(dataset_summary.get("invalid_numeric_rows"))
        invalid_date = format_integer(dataset_summary.get("invalid_date_rows"))
        negative = format_integer(dataset_summary.get("negative_value_rows"))
        sparse = "1" if bool(quality_flags.get("has_sparse_aggregates")) else "0"
        return (
            f"Oportunidade de investigação {invalid_numeric} {invalid_date} {negative} {sparse}. "
            "Variação relevante. Concentração operacional."
        )
    if intent == "quantidade_documentos":
        return (
            f"Tendência operacional {format_integer(kpis.get('total_documentos'))}. "
            "Concentração operacional. Oportunidade de investigação."
        )
    if intent == "ticket_medio":
        return (
            f"Tendência operacional {format_brl(kpis.get('ticket_medio_frete'))}. "
            "Concentração operacional. Oportunidade de investigação."
        )
    if intent == "peso_total":
        return (
            f"Tendência operacional {format_weight(kpis.get('peso_total'))}. "
            "Concentração operacional. Oportunidade de investigação."
        )
    if intent == "fretes_zerados":
        return (
            f"Tendência operacional {format_percent(kpis.get('percentual_fretes_zerados'))}. "
            "Variação relevante. Oportunidade de investigação."
        )
    return ""


def _build_top_reply(*, rows: Any, label: str, fallback: str) -> str:
    if not isinstance(rows, list) or not rows:
        return fallback
    dim_label = _dimension_label_from_table(label)
    parts: list[str] = []
    for idx, row in enumerate(rows[:TOP_LIST_LIMIT], start=1):
        if not isinstance(row, dict):
            continue
        item = _format_rank_item(row=row, dimension_label=dim_label, position=idx)
        if item:
            parts.append(item)
    if not parts:
        return fallback
    return "\n".join(parts) + "\n\nConcentracao operacional com variacao relevante e oportunidade de investigacao."


def _build_uf_scope_reply(
    *,
    origem_rows: Any,
    destino_rows: Any,
    primary_axis: str,
) -> str:
    top_origem = _format_rank_item(
        row=_safe_row_at(origem_rows, 0),
        dimension_label="UF origem",
        position=1,
    )
    top_destino = _format_rank_item(
        row=_safe_row_at(destino_rows, 0),
        dimension_label="UF destino",
        position=1,
    )
    if not top_origem and not top_destino:
        return "Dados insuficientes para leitura de UF origem e UF destino neste momento."
    intro = "Leitura de UFs com foco em custo total do dashboard:"
    if primary_axis == "destino":
        ordered = [top_destino, top_origem]
    else:
        ordered = [top_origem, top_destino]
    body = [item for item in ordered if item]
    return f"{intro}\n" + "\n".join(body) + "\n\nConcentracao operacional com variacao relevante e oportunidade de investigacao."


def _is_dataset_insufficient(safe_context: dict[str, Any]) -> bool:
    session_scope = safe_context.get("session_scope") if isinstance(safe_context.get("session_scope"), dict) else {}
    kpis = safe_context.get("kpis") if isinstance(safe_context.get("kpis"), dict) else {}
    total_docs = _to_int(kpis.get("total_documentos"))
    if not bool(session_scope.get("dataset_validado")):
        return True
    return total_docs <= 0


def _build_success(
    *,
    intent: str,
    reply: str,
    chat_ctx: dict[str, Any],
    response_truncated: bool = False,
    mode: str = "controlled_templates_no_ai_phase_9",
    observability: dict[str, Any] | None = None,
) -> dict[str, Any]:
    safe_context = chat_ctx.get("safe_operational_context") if isinstance(chat_ctx, dict) else {}
    semantic_limits = safe_context.get("semantic_limits") if isinstance(safe_context, dict) else {}
    filter_context = safe_context.get("filter_context") if isinstance(safe_context, dict) else {}
    view_scope = _resolve_view_scope(filter_context)
    context_status = _resolve_context_status(safe_context)
    safe_reply, effective_truncated = _enforce_policy_safe_reply(reply)
    obs = _observability_payload(observability)
    ai_flow_type = FLOW_TYPE_CLEIDE_AI if bool(obs.get("ai_used")) else ""
    return {
        "agent": "cleide",
        "flow_type": "cleide_chat_auditoria_frete",
        "ai_flow_type": ai_flow_type,
        "mode": mode,
        "contract_version": CONVERSATIONAL_CONTRACT_VERSION,
        "phase": CONVERSATIONAL_PHASE,
        "audit_transition_marker": TRANSITION_AUDIT_MARKER,
        "audit_notes": _audit_notes(),
        "intent": intent,
        "reply": safe_reply,
        "response_truncated": bool(response_truncated or effective_truncated),
        "chat_context_version": chat_ctx.get("chat_context_version"),
        "chat_ready_context": bool(chat_ctx.get("chat_ready_context")),
        "semantic_limits": dict(semantic_limits) if isinstance(semantic_limits, dict) else {},
        "filter_mode": str(filter_context.get("filter_mode") or ""),
        "kpi_scope": str(filter_context.get("kpi_scope") or ""),
        "view_scope": view_scope,
        "active_filters": _safe_active_filters(filter_context.get("active_filters")),
        "context_status": context_status,
        **obs,
    }


def _fallback(code: str, reply: str, *, observability: dict[str, Any] | None = None) -> dict[str, Any]:
    safe_reply, truncated = _enforce_policy_safe_reply(reply)
    view_scope = "global"
    context_status = "insufficient" if code in {"fallback_contexto_indisponivel", "fallback_pergunta_invalida"} else "ready"
    obs = _observability_payload(observability)
    ai_flow_type = FLOW_TYPE_CLEIDE_AI if bool(obs.get("ai_used")) else ""
    return {
        "agent": "cleide",
        "flow_type": "cleide_chat_auditoria_frete",
        "ai_flow_type": ai_flow_type,
        "mode": "controlled_templates_no_ai_phase_9",
        "contract_version": CONVERSATIONAL_CONTRACT_VERSION,
        "phase": CONVERSATIONAL_PHASE,
        "audit_transition_marker": TRANSITION_AUDIT_MARKER,
        "audit_notes": _audit_notes(),
        "intent": "fallback_seguro",
        "fallback_code": code,
        "reply": safe_reply,
        "response_truncated": bool(truncated),
        "view_scope": view_scope,
        "active_filters": {},
        "context_status": context_status,
        **obs,
    }


def _safe_reply(reply: str) -> tuple[str, bool]:
    clean = _normalize_reply_text(reply)
    if len(clean) <= MAX_RESPONSE_LEN:
        return clean, False
    truncated = clean[: MAX_RESPONSE_LEN - 3].rstrip()
    if not truncated:
        return "...", True
    return truncated + "...", True


def _contains_forbidden(normalized: str, forbidden_terms: set[str]) -> bool:
    return any(term in normalized for term in forbidden_terms)


def _build_guardrail_sets() -> dict[str, set[str]]:
    allowed = normalized_allowed_language()
    forbidden = normalized_forbidden_language()
    out_of_scope = normalized_out_of_scope_language().union(forbidden)
    return {
        "allowed": allowed,
        "forbidden": forbidden,
        "out_of_scope": out_of_scope,
    }


def _reply_respects_allowed_policy(
    normalized_reply: str,
    *,
    allowed_terms: set[str],
    forbidden_terms: set[str],
    out_of_scope_terms: set[str] | None = None,
    safe_operational_context: dict[str, Any] | None = None,
) -> bool:
    return bool(
        _evaluate_reply_policy(
            normalized_reply,
            allowed_terms=allowed_terms,
            forbidden_terms=forbidden_terms,
            out_of_scope_terms=out_of_scope_terms,
            safe_operational_context=safe_operational_context,
        ).get("ok")
    )


def _evaluate_reply_policy(
    normalized_reply: str,
    *,
    allowed_terms: set[str],
    forbidden_terms: set[str],
    out_of_scope_terms: set[str] | None = None,
    safe_operational_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    semantic_text = _semantic_text(normalized_reply)
    if not semantic_text:
        return {"ok": False, "reason_code": "other", "warning_reason_code": ""}
    if not allowed_terms:
        return {"ok": False, "reason_code": "other", "warning_reason_code": ""}
    if _contains_forbidden(normalized_reply, forbidden_terms):
        return {"ok": False, "reason_code": "forbidden_terms", "warning_reason_code": ""}
    if any(ref in semantic_text for ref in REPLY_BLOCKED_REFERENCES):
        return {"ok": False, "reason_code": "roberto_julia", "warning_reason_code": ""}
    scoped_terms = set(out_of_scope_terms or set())
    scoped_terms.discard("")
    if any(term in semantic_text for term in scoped_terms):
        return {"ok": False, "reason_code": "out_of_scope", "warning_reason_code": ""}
    if any(term in semantic_text for term in REPLY_OUT_OF_DOMAIN_HINTS):
        return {"ok": False, "reason_code": "out_of_domain_hint", "warning_reason_code": ""}
    if _looks_like_raw_or_row_data(semantic_text):
        return {"ok": False, "reason_code": "raw_dataset_or_row_pattern", "warning_reason_code": ""}
    warning_reason_code = ""
    if not _has_operational_domain_signal(semantic_text, allowed_terms):
        warning_reason_code = "domain_signal_missing"
    elif _mentions_unknown_context_entity(semantic_text, safe_operational_context):
        warning_reason_code = "entity_unknown"
    return {"ok": True, "reason_code": "", "warning_reason_code": warning_reason_code}


def _enforce_policy_safe_reply(reply: str) -> tuple[str, bool]:
    allowed_terms = normalized_allowed_language()
    forbidden_terms = normalized_forbidden_language()
    safe_reply, truncated = _safe_reply(reply)
    normalized_reply = _normalize_for_match(safe_reply)
    if _reply_respects_allowed_policy(
        normalized_reply,
        allowed_terms=allowed_terms,
        forbidden_terms=forbidden_terms,
        out_of_scope_terms=normalized_out_of_scope_language(),
    ):
        return safe_reply, truncated
    safe_reply, safe_truncated = _safe_reply(POLICY_SAFE_FALLBACK_REPLY)
    return safe_reply, bool(truncated or safe_truncated)


def _normalized_policy_terms(terms: set[str]) -> tuple[str, ...]:
    normalized_terms = {_semantic_text(_normalize_for_match(term)) for term in terms}
    return tuple(sorted((term for term in normalized_terms if term), key=len, reverse=True))


def _semantic_text(value: str) -> str:
    return _normalize_whitespace(re.sub(r"[^a-z0-9]+", " ", value))


def _tokenize_semantic(value: str) -> tuple[str, ...]:
    semantic_text = _semantic_text(value)
    if not semantic_text:
        return ()
    return tuple(token for token in semantic_text.split(" ") if token)


def _policy_allowed_token_universe(allowed_terms: tuple[str, ...]) -> set[str]:
    universe: set[str] = set()
    for term in allowed_terms:
        for token in _tokenize_semantic(term):
            if token and not token.isdigit():
                universe.add(token)
    return universe


def _semantic_text_is_policy_composed(semantic_text: str, allowed_terms: tuple[str, ...]) -> bool:
    residual = f" {semantic_text} "
    for term in allowed_terms:
        residual = residual.replace(f" {term} ", " ")
    return not any(token for token in _tokenize_semantic(residual) if not token.isdigit())


def _looks_like_raw_or_row_data(semantic_text: str) -> bool:
    if re.search(r"\blinha\s*\d{1,6}\b", semantic_text):
        return True
    if re.search(r"\b(cpf|cnpj|chave de acesso|xml)\b", semantic_text):
        return True
    if re.search(r"\b\d{9,}\b", semantic_text):
        return True
    if semantic_text.count(" ; ") >= 2 or semantic_text.count(" , ") >= 3:
        return True
    return False


def _has_operational_domain_signal(semantic_text: str, allowed_terms: set[str]) -> bool:
    normalized_allowed_terms = _normalized_policy_terms(allowed_terms)
    if any(term in semantic_text for term in normalized_allowed_terms):
        return True
    tokens = set(_tokenize_semantic(semantic_text))
    return any(token in tokens for token in REPLY_MIN_DOMAIN_SIGNAL_TOKENS)


def _unknown_intent_can_use_supervised_ai(
    *,
    normalized_question: str,
    history: list[dict[str, str]] | None,
    safe_operational_context: dict[str, Any],
    allowed_terms: set[str],
    flags: Any,
) -> bool:
    if not bool(getattr(flags, "ai_enabled", False)):
        return False
    if _resolve_context_status(safe_operational_context) != "ready":
        return False
    semantic_question = _semantic_text(normalized_question)
    if not semantic_question:
        return False
    if any(ref in semantic_question for ref in REPLY_BLOCKED_REFERENCES):
        return False
    if _looks_like_raw_or_row_data(semantic_question):
        return False
    if _has_operational_domain_signal(semantic_question, allowed_terms):
        return True
    return _history_has_operational_domain_signal(history, allowed_terms)


def _history_has_operational_domain_signal(
    history: list[dict[str, str]] | None,
    allowed_terms: set[str],
) -> bool:
    if not isinstance(history, list) or not history:
        return False
    for item in history[-HISTORY_MAX_MESSAGES:]:
        if not isinstance(item, dict):
            continue
        content = _normalize_whitespace(str(item.get("content") or ""))
        if not content:
            continue
        semantic = _semantic_text(_normalize_for_match(content))
        if not semantic:
            continue
        if _has_operational_domain_signal(semantic, allowed_terms):
            return True
    return False


def _resolve_contextual_followup(
    *,
    normalized_question: str,
    history: list[dict[str, str]] | None,
    safe_operational_context: dict[str, Any],
) -> dict[str, str] | None:
    if _resolve_context_status(safe_operational_context) not in {"ready", "stale"}:
        return None
    semantic_question = _semantic_text(_normalize_for_match(normalized_question))
    followup_kind = CONTEXTUAL_FOLLOWUP_SHORT_TERMS.get(semantic_question)
    if not followup_kind:
        return None
    conversation_state = _derive_conversation_state(history)
    recent_topic = str(conversation_state.get("last_topic") or "")
    recent_intent = str(conversation_state.get("last_intent") or "")
    recent_scope = str(conversation_state.get("last_scope") or "")
    recent_dimension = str(conversation_state.get("last_dimension") or "")
    if not recent_topic and not recent_intent:
        return None
    if followup_kind == "uf":
        if recent_intent == "uf_origem" or recent_topic == "uf_origem":
            return {"intent": "uf_origem"}
        if recent_intent == "uf_destino" or recent_topic == "uf_destino":
            return {"intent": "uf_destino"}
        if recent_topic in {"embarque_origem", "cidade_embarque", "cidade_operacional"}:
            if recent_scope == "destino":
                return {"intent": "uf_destino"}
            return {"intent": "uf_origem"}
        if recent_dimension == "cidade" and recent_topic == "frete_operacional":
            if recent_scope == "destino":
                return {"intent": "uf_destino"}
            # Regra segura para ambiguidade cidade+frete: assumir origem.
            return {"intent": "uf_origem"}
        return None
    if followup_kind == "origem":
        return {"intent": "uf_origem"}
    if followup_kind == "destino":
        return {"intent": "uf_destino"}
    if followup_kind == "transportadora":
        return {"intent": "top_transportadoras"}
    if followup_kind == "modal":
        return {"intent": "modal_operacional"}
    if followup_kind == "periodo":
        return {"intent": "periodo_dataset"}
    if followup_kind == "filtro":
        return {"intent": "filtro_operacional"}
    if followup_kind.startswith("ranking_"):
        rank_position = _to_int(followup_kind.replace("ranking_", ""))
        if rank_position <= 0:
            return None
        rank_map = {
            "top_transportadoras": "transportadora",
            "uf_origem": "uf_origem",
            "uf_destino": "uf_destino",
            "modal_operacional": "modal",
        }
        topic_or_intent = recent_topic or recent_intent
        table_key = rank_map.get(topic_or_intent) or rank_map.get(recent_intent)
        if not table_key:
            return None
        tables = (
            safe_operational_context.get("aggregate_tables")
            if isinstance(safe_operational_context.get("aggregate_tables"), dict)
            else {}
        )
        reply = _build_rank_position_reply(rows=tables.get(table_key), position=rank_position)
        if not reply:
            return {"intent": "dados_insuficientes"}
        return {"intent": topic_or_intent or "unknown", "reply": reply}
    return None


def _resolve_recent_history_topic(history: list[dict[str, str]] | None) -> str:
    if not isinstance(history, list) or not history:
        return ""
    for item in reversed(history[-HISTORY_MAX_MESSAGES:]):
        if not isinstance(item, dict):
            continue
        content = _normalize_whitespace(str(item.get("content") or ""))
        if not content:
            continue
        semantic = _semantic_text(_normalize_for_match(content))
        if not semantic:
            continue
        topic = _topic_from_semantic_text(semantic)
        if topic:
            return topic
    return ""


def _derive_conversation_state(history: list[dict[str, str]] | None) -> dict[str, str]:
    state = {"last_intent": "", "last_topic": "", "last_dimension": "", "last_scope": ""}
    if not isinstance(history, list) or not history:
        return state
    items = [item for item in history[-HISTORY_MAX_MESSAGES:] if isinstance(item, dict)]
    if not items:
        return state
    for preferred_role in ("user", "assistant"):
        for item in reversed(items):
            role = _normalize_for_match(str(item.get("role") or ""))
            if role != preferred_role:
                continue
            content = _normalize_whitespace(str(item.get("content") or ""))
            if not content:
                continue
            normalized_content = _normalize_for_match(content)
            semantic = _semantic_text(normalized_content)
            if not semantic:
                continue
            inferred_topic = _topic_from_semantic_text(semantic)
            inferred_intent = _classify_intent(normalized_content)
            inferred_dimension = _dimension_from_semantic_text(semantic)
            inferred_scope = _scope_from_semantic_text(semantic)
            if inferred_intent == "unknown":
                inferred_intent = ""
            if inferred_topic or inferred_intent or inferred_dimension or inferred_scope:
                return {
                    "last_intent": inferred_intent,
                    "last_topic": inferred_topic,
                    "last_dimension": inferred_dimension,
                    "last_scope": inferred_scope,
                }
    return state


def _topic_from_semantic_text(semantic_text: str) -> str:
    has_cidade = "cidade" in semantic_text
    has_frete = "frete" in semantic_text
    has_embarque = "embarque" in semantic_text
    has_volume = "volume" in semantic_text
    has_origem = "origem" in semantic_text
    has_destino = "destino" in semantic_text
    if has_cidade and (has_frete or has_embarque or has_volume or has_origem or has_destino):
        return "cidade_operacional"
    if "maior volume de frete" in semantic_text:
        return "cidade_operacional"
    if "cidade" in semantic_text and "embarque" in semantic_text:
        return "cidade_embarque"
    if "embarque" in semantic_text:
        return "embarque_origem"
    if has_frete:
        return "frete_operacional"
    if "uf origem" in semantic_text or "origem" in semantic_text:
        return "uf_origem"
    if "uf destino" in semantic_text or "destino" in semantic_text:
        return "uf_destino"
    if "transportadora" in semantic_text:
        return "top_transportadoras"
    if "modal" in semantic_text:
        return "modal_operacional"
    if "periodo" in semantic_text:
        return "periodo_dataset"
    if "filtro" in semantic_text:
        return "filtro_operacional"
    return ""


def _build_rank_position_reply(*, rows: Any, position: int) -> str:
    if not isinstance(rows, list) or not rows:
        return ""
    idx = max(1, position) - 1
    if idx >= len(rows):
        return ""
    row = rows[idx]
    if not isinstance(row, dict):
        return ""
    rank_item = _format_rank_item(row=row, dimension_label=_infer_dimension_from_row(row), position=position)
    if not rank_item:
        return ""
    return f"{rank_item}\n\nConcentracao operacional com variacao relevante e oportunidade de investigacao."


def _normalize_reply_text(value: str) -> str:
    raw = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    previous_blank = False
    for line in raw.split("\n"):
        normalized = _normalize_whitespace(line)
        if not normalized:
            if not previous_blank:
                lines.append("")
            previous_blank = True
            continue
        lines.append(normalized)
        previous_blank = False
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _safe_row_at(rows: Any, idx: int) -> dict[str, Any] | None:
    if not isinstance(rows, list):
        return None
    if idx < 0 or idx >= len(rows):
        return None
    row = rows[idx]
    return row if isinstance(row, dict) else None


def _top_row_label(rows: Any) -> str:
    row = _safe_row_at(rows, 0)
    if not row:
        return "dados insuficientes"
    chave = _normalize_whitespace(str(row.get("chave") or ""))
    if not chave:
        return "dados insuficientes"
    valor = _to_float(row.get("valor_total"))
    if valor > 0:
        return f"{chave} ({format_brl(valor)})"
    quantidade = _to_int(row.get("quantidade"))
    return f"{chave} ({format_integer(quantidade)} documentos)"


def _dimension_label_from_table(label: str) -> str:
    normalized = _normalize_for_match(label)
    if "uf de origem" in normalized or "ufs de origem" in normalized or "uf origem" in normalized:
        return "UF origem"
    if "uf de destino" in normalized or "ufs de destino" in normalized or "uf destino" in normalized:
        return "UF destino"
    if "transportadora" in normalized:
        return "transportadora"
    if "modal" in normalized:
        return "modal"
    return "categoria"


def _infer_dimension_from_row(row: dict[str, Any]) -> str:
    chave = _normalize_whitespace(str(row.get("chave") or ""))
    if len(chave) == 2 and chave.isalpha():
        return "UF"
    return "categoria"


def _format_rank_item(*, row: dict[str, Any] | None, dimension_label: str, position: int) -> str:
    if not isinstance(row, dict):
        return ""
    chave = _normalize_whitespace(str(row.get("chave") or ""))
    if not chave:
        return ""
    metrics: list[str] = []
    quantidade = _to_int(row.get("quantidade"))
    if quantidade > 0:
        metrics.append(f"{format_integer(quantidade)} documentos")
    valor_total = _to_float(row.get("valor_total"))
    if valor_total > 0:
        metrics.append(f"{format_brl(valor_total)} em custo total")
    peso_total = _to_float(row.get("peso_total"))
    if peso_total > 0:
        metrics.append(format_weight(peso_total))
    metrics_text = "; ".join(metrics) if metrics else "sem metricas suficientes"
    return f"{position}o lugar em {dimension_label}: {chave} - {metrics_text}."


def _dimension_from_semantic_text(semantic_text: str) -> str:
    if "cidade" in semantic_text:
        return "cidade"
    if "uf" in semantic_text or "estado" in semantic_text:
        return "uf"
    if "transportadora" in semantic_text:
        return "transportadora"
    if "modal" in semantic_text:
        return "modal"
    if "periodo" in semantic_text:
        return "periodo"
    if "filtro" in semantic_text:
        return "filtro"
    return ""


def _scope_from_semantic_text(semantic_text: str) -> str:
    if "destino" in semantic_text:
        return "destino"
    if "origem" in semantic_text or "embarque" in semantic_text:
        return "origem"
    if "cidade" in semantic_text and ("frete" in semantic_text or "volume" in semantic_text):
        # Regra segura documentada: cidade+frete sem escopo explicito resolve para origem.
        return "origem"
    return ""


def _normalize_history_entries(history: Any) -> list[dict[str, str]]:
    if not isinstance(history, list):
        return []
    out: list[dict[str, str]] = []
    role_alias = {"user": "user", "assistant": "assistant", "model": "assistant", "cleide": "assistant"}
    for item in history:
        if not isinstance(item, dict):
            continue
        role_raw = _normalize_for_match(str(item.get("role") or ""))
        role = role_alias.get(role_raw)
        if not role:
            continue
        content = _normalize_whitespace(str(item.get("content") or ""))[:HISTORY_MAX_CHARS]
        if not content:
            continue
        normalized_content = _normalize_for_match(content)
        semantic_content = _semantic_text(normalized_content)
        if not semantic_content:
            continue
        if any(ref in semantic_content for ref in REPLY_BLOCKED_REFERENCES):
            continue
        if _looks_like_raw_or_row_data(semantic_content):
            continue
        scoped_terms = set(normalized_out_of_scope_language())
        scoped_terms.discard("")
        if any(term in semantic_content for term in scoped_terms):
            continue
        if any(term in semantic_content for term in REPLY_OUT_OF_DOMAIN_HINTS):
            continue
        out.append({"role": role, "content": content})
    return out[-HISTORY_MAX_MESSAGES:]


def _mentions_unknown_context_entity(
    semantic_text: str,
    safe_operational_context: dict[str, Any] | None,
) -> bool:
    if not isinstance(safe_operational_context, dict):
        return False
    tables = safe_operational_context.get("aggregate_tables")
    if not isinstance(tables, dict):
        return False
    known_transportadoras: set[str] = set()
    for row in tables.get("transportadora") or []:
        if isinstance(row, dict):
            chave = _normalize_for_match(str(row.get("chave") or ""))
            if chave:
                known_transportadoras.add(chave)
    mentioned = re.findall(r"\btransportadora\s+([a-z0-9_-]{2,30})\b", semantic_text)
    generic_tokens = {
        "de",
        "da",
        "do",
        "das",
        "dos",
        "para",
        "com",
        "sem",
        "na",
        "no",
        "nas",
        "nos",
    }
    for raw_name in mentioned:
        cleaned = _normalize_for_match(raw_name).strip(" -_")
        if not cleaned or cleaned in generic_tokens:
            continue
        if cleaned.isdigit():
            continue
        if known_transportadoras and cleaned not in known_transportadoras:
            return True
    return False


def _audit_notes() -> dict[str, Any]:
    return {
        "legacy_chat_status": 501,
        "legacy_chat_mode": "placeholder_noop_phase_8_2",
        "current_chat_status": 200,
        "current_chat_mode": "controlled_templates_no_ai",
        "policy_source": "app.cleide_language_policy",
    }


def _observability(
    *,
    flags: Any,
    ai_used: bool,
    fallback_used: bool,
    policy_blocked: bool = False,
    policy_block_reason_code: str = "",
    policy_warning_reason_code: str = "",
    provider: str = "gemini",
    model: str = "",
    latency_ms: int = 0,
    usage: dict[str, Any] | None = None,
    error_code: str = "",
    reason: str = "",
) -> dict[str, Any]:
    data = dict(usage or {})
    return {
        "ai_enabled": bool(getattr(flags, "ai_enabled", False)),
        "ai_used": bool(ai_used),
        "fallback_used": bool(fallback_used),
        "policy_blocked": bool(policy_blocked),
        "policy_block_reason_code": str(policy_block_reason_code or ""),
        "policy_warning_reason_code": str(policy_warning_reason_code or ""),
        "provider": provider,
        "model": model,
        "latency_ms": max(0, int(latency_ms or 0)),
        "token_usage": {
            "input_tokens": data.get("input_tokens"),
            "output_tokens": data.get("output_tokens"),
            "total_tokens": data.get("total_tokens"),
        },
        "error_code": str(error_code or ""),
        "reason": str(reason or ""),
    }


def _observability_payload(observability: dict[str, Any] | None) -> dict[str, Any]:
    defaults = {
        "ai_enabled": False,
        "ai_used": False,
        "fallback_used": False,
        "policy_blocked": False,
        "policy_block_reason_code": "",
        "policy_warning_reason_code": "",
        "provider": "gemini",
        "model": "",
        "latency_ms": 0,
        "token_usage": {"input_tokens": None, "output_tokens": None, "total_tokens": None},
        "error_code": "",
        "reason": "",
    }
    if not isinstance(observability, dict):
        return defaults
    out = dict(defaults)
    out.update(observability)
    return out


def _resolve_authz() -> dict[str, Any]:
    if not has_request_context():
        return {"permitido": True, "motivo": "no_request_context"}
    try:
        if not getattr(current_user, "is_authenticated", False):
            return {"permitido": True, "motivo": "not_authenticated"}
        return avaliar_autorizacao_operacao_por_franquia(current_user, sincronizar_ciclo=False)
    except Exception:
        return {"permitido": False, "motivo": "authz_error"}


def _breaker_is_open() -> tuple[bool, str]:
    now = time.monotonic()
    with _circuit_breaker_lock:
        open_until = float(_circuit_breaker_state.get("open_until_monotonic") or 0.0)
        if now < open_until:
            return True, str(_circuit_breaker_state.get("reason") or "open")
        return False, ""


def _breaker_open(reason: str) -> None:
    with _circuit_breaker_lock:
        _circuit_breaker_state["open_until_monotonic"] = time.monotonic() + CIRCUIT_BREAKER_COOLDOWN_SECONDS
        _circuit_breaker_state["reason"] = str(reason or "error")


def _breaker_mark_success() -> None:
    with _circuit_breaker_lock:
        _circuit_breaker_state["policy_blocked_streak"] = 0


def _breaker_mark_policy_blocked() -> None:
    with _circuit_breaker_lock:
        streak = int(_circuit_breaker_state.get("policy_blocked_streak") or 0) + 1
        _circuit_breaker_state["policy_blocked_streak"] = streak
        if streak >= POLICY_BLOCKED_BREAKER_THRESHOLD:
            _circuit_breaker_state["open_until_monotonic"] = (
                time.monotonic() + CIRCUIT_BREAKER_COOLDOWN_SECONDS
            )
            _circuit_breaker_state["reason"] = "policy_blocked_repeated"


def _breaker_mark_failure(*, error_code: str, reason: str) -> None:
    code = str(error_code or "").lower()
    why = str(reason or "").lower()
    if code in {"missing_key", "provider_error", "empty_response", "unsafe_context"}:
        _breaker_open(code)
        return
    if "timeout" in why:
        _breaker_open("timeout")


def _contains_any(text: str, words: tuple[str, ...]) -> bool:
    return any(_normalize_for_match(word) in text for word in words)


def _normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _normalize_for_match(value: str) -> str:
    base = _normalize_whitespace(value).lower()
    normalized = unicodedata.normalize("NFD", base)
    without_accents = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    return re.sub(r"[\W_]+$", "", without_accents).strip()


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _fmt_int(value: Any) -> str:
    return str(max(0, _to_int(value)))


def _fmt_float(value: Any) -> str:
    return f"{max(0.0, _to_float(value)):.2f}"


def _safe_active_filters(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for key in ("transportadora", "uf_origem", "uf_destino", "data_inicio", "data_fim"):
        text = str(value.get(key) or "").strip()
        if text:
            out[key] = text
    return out


def _resolve_view_scope(filter_context: dict[str, Any]) -> str:
    active = _safe_active_filters(filter_context.get("active_filters"))
    return "filtered" if active else "global"


def _resolve_context_status(safe_context: dict[str, Any]) -> str:
    if _is_dataset_insufficient(safe_context):
        return "insufficient"
    session_scope = safe_context.get("session_scope") if isinstance(safe_context.get("session_scope"), dict) else {}
    if bool(session_scope.get("stale_upload")):
        return "stale"
    return "ready"
