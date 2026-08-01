"""
Backend do chat inteligente do AgenteCompara baseado na comparação vigente.

Uma pergunta válida após READY => exatamente uma chamada ao Gemini.
Falhas de provider/configuração retornam erro estruturado (nunca HTTP 200 falso).
Pré-READY é bloqueado pela rota (zero Gemini).
Isolado de Cleide/audit_bi.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

from app.agente_compara_chat_context_service import (
    CAPABILITY_LOCKED,
    CAPABILITY_READY,
    CHAT_NOT_READY_MESSAGE,
    ERROR_COMPARISON_CHAT_NOT_READY,
    SCOPE_DECISION,
    SCOPE_OVERVIEW,
    AgenteComparaChatContextError,
    build_comparison_chat_context,
    evaluate_comparison_chat_availability,
    resolve_chat_capability,
)
from app.agente_compara_chat_prompt import build_comparison_chat_user_prompt
from app.agente_compara_comparison_state import get_comparison_state
from app.agente_compara_doc_service import (
    AGENTE_COMPARA_COMPARISON_CHAT_FLOW_TYPE,
    agente_compara_comparison_chat_idempotency_key,
)
from app.run_agente_compara_chat import (
    DEFAULT_MODEL_FALLBACK,
    PROCESSING_ERROR_MESSAGE,
    sanitize_chat_history,
)
from app.run_agente_compara_insights_chat import (
    finalize_insights_answer,
    sanitize_agente_compara_sender_signature,
)
from app.run_cleiton_gemini_governance import cleiton_governed_generate_content
from app.services.agente_compara_config_service import get_agente_compara_config

logger = logging.getLogger(__name__)

COMPARISON_CHAT_IDEMPOTENCY_CACHE_SESSION_KEY = "agente_compara_comparison_chat_idempotency_cache"
COMPARISON_CHAT_CACHE_MAX_ENTRIES = 50

ERROR_PROVIDER_NOT_CONFIGURED = "provider_not_configured"
ERROR_PROVIDER_INITIALIZATION_FAILED = "provider_initialization_failed"
ERROR_PROVIDER_REQUEST_FAILED = "provider_request_failed"
ERROR_PROVIDER_TIMEOUT = "provider_timeout"
ERROR_PROVIDER_EMPTY_RESPONSE = "provider_empty_response"
ERROR_PROVIDER_INVALID_RESPONSE = "provider_invalid_response"
ERROR_CONTEXT_BUILD_FAILED = "context_build_failed"
ERROR_PROMPT_BUILD_FAILED = "prompt_build_failed"

MSG_PROVIDER_NOT_CONFIGURED = (
    "O serviço de inteligência artificial não está configurado neste ambiente."
)
MSG_PROVIDER_INITIALIZATION_FAILED = (
    "Não foi possível iniciar o serviço de inteligência artificial."
)
MSG_PROVIDER_REQUEST_FAILED = (
    "O serviço de inteligência artificial está indisponível no momento. Tente novamente em instantes."
)
MSG_PROVIDER_TIMEOUT = "A resposta demorou mais que o esperado. Tente novamente."
MSG_PROVIDER_EMPTY_RESPONSE = (
    "O serviço não conseguiu gerar uma resposta válida. Tente novamente."
)
MSG_PROVIDER_INVALID_RESPONSE = MSG_PROVIDER_EMPTY_RESPONSE
MSG_CONTEXT_BUILD_FAILED = "Não foi possível montar o contexto da comparação. Tente novamente."
MSG_PROMPT_BUILD_FAILED = "Não foi possível preparar a consulta. Tente novamente."

_DECISION_PATTERNS = (
    r"\bescolha\b",
    r"\bcontrate\b",
    r"\bcontratar\b",
    r"\bdecida\b",
    r"\ba decis[aã]o correta\b",
    r"\bvoc[eê] deve contratar\b",
    r"\bfeche com\b",
    r"\ba melhor (?:op[cç][aã]o|transportadora) [eé]\b",
)

_DECISION_REPLACEMENTS = (
    (r"\bescolha\b", "os dados indicam avaliar"),
    (r"\bcontrate\b", "considere avaliar"),
    (r"\bcontratar\b", "avaliar a contratação de"),
    (r"\bdecida\b", "a decisão final permanece com o usuário; os dados indicam"),
    (r"\ba decis[aã]o correta\b", "a decisão final é do usuário; a análise indica"),
    (r"\bvoc[eê] deve contratar\b", "a escolha final é do usuário; os dados mostram"),
    (r"\bfeche com\b", "avalie fechar com"),
)


class ComparisonChatProviderError(Exception):
    """Erro tipado da fronteira Gemini do chat comparativo."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        retryable: bool = True,
        http_status: int = 503,
        stage: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.retryable = bool(retryable)
        self.http_status = int(http_status)
        self.stage = stage
        self.__cause__ = cause


class ComparisonChatProviderNotConfigured(ComparisonChatProviderError):
    def __init__(self, message: str = MSG_PROVIDER_NOT_CONFIGURED, *, cause: Exception | None = None) -> None:
        super().__init__(
            ERROR_PROVIDER_NOT_CONFIGURED,
            message,
            retryable=False,
            http_status=503,
            stage="client_init",
            cause=cause,
        )


class ComparisonChatProviderInitializationError(ComparisonChatProviderError):
    def __init__(
        self,
        message: str = MSG_PROVIDER_INITIALIZATION_FAILED,
        *,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(
            ERROR_PROVIDER_INITIALIZATION_FAILED,
            message,
            retryable=True,
            http_status=503,
            stage="client_init",
            cause=cause,
        )


class ComparisonChatProviderRequestError(ComparisonChatProviderError):
    def __init__(
        self,
        message: str = MSG_PROVIDER_REQUEST_FAILED,
        *,
        cause: Exception | None = None,
        stage: str = "provider_request",
    ) -> None:
        super().__init__(
            ERROR_PROVIDER_REQUEST_FAILED,
            message,
            retryable=True,
            http_status=503,
            stage=stage,
            cause=cause,
        )


class ComparisonChatProviderTimeout(ComparisonChatProviderError):
    def __init__(
        self,
        message: str = MSG_PROVIDER_TIMEOUT,
        *,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(
            ERROR_PROVIDER_TIMEOUT,
            message,
            retryable=True,
            http_status=503,
            stage="provider_request",
            cause=cause,
        )


class ComparisonChatProviderEmptyResponse(ComparisonChatProviderError):
    def __init__(
        self,
        message: str = MSG_PROVIDER_EMPTY_RESPONSE,
        *,
        cause: Exception | None = None,
        error_code: str = ERROR_PROVIDER_EMPTY_RESPONSE,
    ) -> None:
        super().__init__(
            error_code,
            message,
            retryable=True,
            http_status=503,
            stage="response_parse",
            cause=cause,
        )


def _api_key_label() -> str:
    if os.getenv("GEMINI_API_KEY_1"):
        return "GEMINI_API_KEY_1"
    if os.getenv("GEMINI_API_KEY"):
        return "GEMINI_API_KEY"
    return "unknown"


def _is_provider_timeout(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    message = str(exc).upper()
    name = exc.__class__.__name__.upper()
    return (
        "DEADLINE_EXCEEDED" in message
        or "TIMEOUT" in message
        or "TIMEOUT" in name
        or "504" in message
    )


def _get_client():
    """
    Obtém client Gemini.

    - chave ausente → ComparisonChatProviderNotConfigured
    - import/init falhou → ComparisonChatProviderInitializationError
    - sucesso → client válido
    """
    key = (os.getenv("GEMINI_API_KEY_1") or os.getenv("GEMINI_API_KEY") or "").strip()
    if not key:
        logger.warning(
            "comparison chat: nenhuma chave Gemini configurada "
            "(GEMINI_API_KEY_1 ou GEMINI_API_KEY)."
        )
        raise ComparisonChatProviderNotConfigured()
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
        return genai.Client(api_key=key, http_options=genai_types.HttpOptions(timeout=timeout_ms))
    except ComparisonChatProviderError:
        raise
    except Exception as exc:
        logger.error(
            "comparison chat: falha ao inicializar client Gemini exc_type=%s",
            exc.__class__.__name__,
        )
        raise ComparisonChatProviderInitializationError(cause=exc) from exc


def _get_model_candidates() -> list[str]:
    candidates = [
        (os.getenv("GEMINI_MODEL_TEXT") or "").strip(),
        "gemini-2.5-flash",
        (os.getenv("AGENTE_COMPARA_COMPARISON_CHAT_MODEL_FALLBACK") or "").strip(),
        DEFAULT_MODEL_FALLBACK,
    ]
    seen: set[str] = set()
    out: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


def resolve_comparison_chat_model() -> str:
    candidates = _get_model_candidates()
    if not candidates:
        raise ComparisonChatProviderInitializationError(
            "Nenhum modelo Gemini configurado para o chat comparativo."
        )
    return candidates[0]


def _comparison_chat_limits(cfg=None) -> dict[str, int]:
    audit_cfg = cfg or get_agente_compara_config()
    return {
        "question_max_chars": int(
            getattr(audit_cfg, "comparison_chat_question_max_chars", None) or audit_cfg.question_max_chars
        ),
        "history_max_items": int(
            getattr(audit_cfg, "comparison_chat_history_max_items", None) or audit_cfg.chat_max_history
        ),
        "context_max_chars": int(getattr(audit_cfg, "comparison_chat_context_max_chars", 48000) or 48000),
        "max_rows": int(getattr(audit_cfg, "comparison_chat_max_rows", 12) or 12),
        "max_memories": int(getattr(audit_cfg, "comparison_chat_max_memories", 6) or 6),
        "max_table_rules": int(getattr(audit_cfg, "comparison_chat_max_table_rules", 24) or 24),
        "max_ranked_items": int(getattr(audit_cfg, "comparison_chat_max_ranked_items", 8) or 8),
    }


def comparison_chat_scope_key(context: dict | None) -> str:
    comparison = (context or {}).get("comparison") if isinstance(context, dict) else None
    if not isinstance(comparison, dict):
        return "none"
    cmp_id = str(comparison.get("comparison_id") or "").strip() or "none"
    fp = str(comparison.get("fingerprint_short") or "").strip() or "na"
    stale = "1" if comparison.get("stale") else "0"
    return f"{cmp_id}:{fp}:{stale}"


def get_cached_comparison_chat_response(session_obj, request_id: str, *, scope_key: str = "") -> dict | None:
    ref = (request_id or "").strip()
    if not ref:
        return None
    cache = session_obj.get(COMPARISON_CHAT_IDEMPOTENCY_CACHE_SESSION_KEY)
    if not isinstance(cache, dict):
        return None
    payload = cache.get(agente_compara_comparison_chat_idempotency_key(ref, scope_key))
    return payload if isinstance(payload, dict) else None


def cache_comparison_chat_response(
    session_obj,
    request_id: str,
    payload: dict,
    *,
    scope_key: str = "",
) -> None:
    ref = (request_id or "").strip()
    if not ref or not isinstance(payload, dict):
        return
    # Nunca cachear payloads de erro técnico.
    if payload.get("error"):
        return
    cache = session_obj.get(COMPARISON_CHAT_IDEMPOTENCY_CACHE_SESSION_KEY)
    if not isinstance(cache, dict):
        cache = {}
    cache_key = agente_compara_comparison_chat_idempotency_key(ref, scope_key)
    safe_answer = finalize_comparison_chat_answer(payload.get("answer") or "")
    entry = {
        "answer": safe_answer,
        "flow_type": payload.get("flow_type") or AGENTE_COMPARA_COMPARISON_CHAT_FLOW_TYPE,
        "deterministic": bool(payload.get("deterministic")),
        "scope": payload.get("scope"),
        "basis": payload.get("basis") if isinstance(payload.get("basis"), dict) else {},
        "warnings": list(payload.get("warnings") or []),
        "scope_key": scope_key,
    }
    if cache_key in cache:
        del cache[cache_key]
    cache[cache_key] = entry
    while len(cache) > COMPARISON_CHAT_CACHE_MAX_ENTRIES:
        oldest = next(iter(cache))
        del cache[oldest]
    session_obj[COMPARISON_CHAT_IDEMPOTENCY_CACHE_SESSION_KEY] = cache
    session_obj.modified = True


def soften_comparison_decision_language(text: str) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return cleaned
    result = cleaned
    for pattern, replacement in _DECISION_REPLACEMENTS:
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
    lowered = result.lower()
    if any(re.search(pattern, lowered) for pattern in _DECISION_PATTERNS):
        if "decisão final" not in lowered and "decisao final" not in lowered:
            result = (
                result.rstrip()
                + "\n\nA decisão final sobre transportadoras e tabelas permanece com o usuário."
            )
    return result


def finalize_comparison_chat_answer(text: str, *, decision_request: bool = False) -> str:
    cleaned = finalize_insights_answer(text or "")
    cleaned = sanitize_agente_compara_sender_signature(cleaned)
    if decision_request:
        cleaned = soften_comparison_decision_language(cleaned)
    return cleaned.strip()


def resolve_objective_absence_message(context: dict, *, question: str = "") -> str | None:
    """
    Mensagens técnicas legítimas sem Gemini (dado objetivamente ausente).
    Não produz análise logística.
    """
    scope_meta = context.get("selected_scope") if isinstance(context.get("selected_scope"), dict) else {}
    if scope_meta.get("document_number") and scope_meta.get("document_match_count") == 0:
        return (
            f"Não encontrei o documento '{scope_meta.get('document_number')}' no resultado comparativo vigente. "
            "Verifique o número ou o recorte da comparação."
        )
    if scope_meta.get("document_ambiguous"):
        return (
            f"O documento '{scope_meta.get('document_number')}' aparece em mais de uma linha. "
            "Informe o row_index para eu explicar a ocorrência correta."
        )
    return None


def build_deterministic_comparison_fallback(context: dict, *, question: str = "") -> str:
    """
    Fallback técnico apenas para ausência objetiva de dado.
    Não substitui resposta do Gemini em falha de provider.
    """
    message = resolve_objective_absence_message(context, question=question)
    if message:
        return message
    capability = ""
    scope_meta = context.get("selected_scope") if isinstance(context.get("selected_scope"), dict) else {}
    capability = str(scope_meta.get("capability") or CAPABILITY_LOCKED)
    comparison = context.get("comparison") if isinstance(context.get("comparison"), dict) else {}
    if capability != CAPABILITY_READY or comparison.get("stale"):
        return CHAT_NOT_READY_MESSAGE
    data_quality = context.get("data_quality") if isinstance(context.get("data_quality"), dict) else {}
    if not data_quality.get("has_result") or not data_quality.get("has_analytics"):
        return "O contexto oficial da comparação não está disponível no momento. Tente novamente após o cálculo."
    return ""


def _provider_error_payload(
    exc: ComparisonChatProviderError,
    *,
    scope: str | None = None,
    basis: dict | None = None,
    warnings: list[str] | None = None,
    model: str | None = None,
) -> dict:
    return {
        "answer": "",
        "flow_type": AGENTE_COMPARA_COMPARISON_CHAT_FLOW_TYPE,
        "deterministic": True,
        "error": exc.error_code,
        "error_code": exc.error_code,
        "message": exc.message,
        "http_status": exc.http_status,
        "retryable": exc.retryable,
        "chat_available": True,
        "capability": CAPABILITY_READY,
        "scope": scope,
        "basis": basis or {},
        "warnings": list(warnings or []),
        "model": model,
        "stage": exc.stage,
    }


def _safe_observability(
    *,
    comparison_id: str | None,
    request_id: str | None,
    scope: str | None,
    table_count: int,
    row_count: int,
    memory_count: int,
    latency_ms: int,
    model: str | None,
    success: bool,
    fallback: bool,
    error: str | None,
    selected_widget: str | None,
    stage: str | None = None,
    retryable: bool | None = None,
    exception_class: str | None = None,
) -> None:
    logger.info(
        "agente_compara_comparison_chat agent=agente_compara comparison_ref=%s request_id=%s "
        "scope=%s table_count=%s context_rows=%s memories=%s latency_ms=%s model=%s "
        "success=%s fallback=%s error=%s stage=%s retryable=%s exc_class=%s selected_widget=%s",
        (comparison_id or "")[:12],
        (request_id or "")[:32],
        scope,
        table_count,
        row_count,
        memory_count,
        latency_ms,
        model or "",
        success,
        fallback,
        error or "",
        stage or "",
        "" if retryable is None else bool(retryable),
        exception_class or "",
        selected_widget or "",
    )


def _extract_response_text(response: Any) -> str:
    if response is None:
        raise ComparisonChatProviderEmptyResponse(
            error_code=ERROR_PROVIDER_INVALID_RESPONSE,
            message=MSG_PROVIDER_INVALID_RESPONSE,
        )
    if not hasattr(response, "text"):
        raise ComparisonChatProviderEmptyResponse(
            error_code=ERROR_PROVIDER_INVALID_RESPONSE,
            message=MSG_PROVIDER_INVALID_RESPONSE,
        )
    text = getattr(response, "text", None)
    if text is None:
        raise ComparisonChatProviderEmptyResponse()
    cleaned = str(text).strip()
    if not cleaned:
        raise ComparisonChatProviderEmptyResponse()
    return cleaned


def _run_one_gemini_call(
    *,
    prompt: str,
    session_obj,
    request_id: str | None,
    scope_key: str,
    scope: str,
    basis: dict,
    warnings: list[str],
    decision_request: bool,
) -> dict:
    client = _get_client()
    model = resolve_comparison_chat_model()
    try:
        response = cleiton_governed_generate_content(
            client,
            model=model,
            contents=prompt,
            agent="agente_compara",
            flow_type=AGENTE_COMPARA_COMPARISON_CHAT_FLOW_TYPE,
            api_key_label=_api_key_label(),
        )
    except ComparisonChatProviderError:
        raise
    except Exception as exc:
        if _is_provider_timeout(exc):
            raise ComparisonChatProviderTimeout(cause=exc) from exc
        logger.warning(
            "comparison chat provider failure model=%s exc_type=%s",
            model,
            exc.__class__.__name__,
        )
        raise ComparisonChatProviderRequestError(cause=exc) from exc

    try:
        text = _extract_response_text(response)
    except ComparisonChatProviderError:
        raise

    payload = {
        "answer": finalize_comparison_chat_answer(text, decision_request=decision_request),
        "flow_type": AGENTE_COMPARA_COMPARISON_CHAT_FLOW_TYPE,
        "deterministic": False,
        "scope": scope,
        "basis": basis,
        "warnings": warnings,
        "model": model,
    }
    cache_comparison_chat_response(session_obj, request_id or "", payload, scope_key=scope_key)
    return payload


def chat_agente_compara_comparison_reply(
    user_message: str,
    history: list,
    *,
    session_obj,
    comparison_id: str | None = None,
    request_id: str | None = None,
    ui_context: dict | None = None,
    visual_focus: dict | None = None,
    max_history: int | None = None,
    question_max_chars: int | None = None,
    fallback_message: str | None = None,
    skip_availability_gate: bool = False,
) -> dict:
    started = time.perf_counter()
    audit_cfg = get_agente_compara_config()
    limits = _comparison_chat_limits(audit_cfg)
    if question_max_chars is not None:
        limits["question_max_chars"] = int(question_max_chars)
    if max_history is not None:
        limits["history_max_items"] = int(max_history)

    clean_message = (user_message or "").strip()
    if not clean_message:
        return {
            "answer": "",
            "flow_type": AGENTE_COMPARA_COMPARISON_CHAT_FLOW_TYPE,
            "deterministic": True,
            "error": "invalid_message",
            "message": "Mensagem vazia.",
            "chat_available": False,
            "retryable": False,
        }
    if len(clean_message) > limits["question_max_chars"]:
        return {
            "answer": "",
            "flow_type": AGENTE_COMPARA_COMPARISON_CHAT_FLOW_TYPE,
            "deterministic": True,
            "error": "invalid_message",
            "message": f"Mensagem excede o limite de {limits['question_max_chars']} caracteres.",
            "chat_available": False,
            "retryable": False,
        }

    if not skip_availability_gate:
        try:
            availability = evaluate_comparison_chat_availability(
                session_obj=session_obj,
                comparison_id=comparison_id,
            )
        except AgenteComparaChatContextError as exc:
            return {
                "answer": "",
                "flow_type": AGENTE_COMPARA_COMPARISON_CHAT_FLOW_TYPE,
                "deterministic": True,
                "error": exc.error_code,
                "message": exc.message,
                "http_status": exc.http_status,
                "chat_available": False,
                "capability": CAPABILITY_LOCKED,
                "retryable": False,
            }
        if not availability.get("chat_available"):
            return {
                "answer": "",
                "flow_type": AGENTE_COMPARA_COMPARISON_CHAT_FLOW_TYPE,
                "deterministic": True,
                "error": ERROR_COMPARISON_CHAT_NOT_READY,
                "error_code": ERROR_COMPARISON_CHAT_NOT_READY,
                "message": CHAT_NOT_READY_MESSAGE,
                "http_status": 409,
                "chat_available": False,
                "capability": CAPABILITY_LOCKED,
                "reason": availability.get("reason") or "comparison_not_ready",
                "retryable": False,
            }

    merged_ui = dict(ui_context or {})
    if isinstance(visual_focus, dict) and visual_focus:
        merged_ui.setdefault("visual_focus", visual_focus)
        if visual_focus.get("destination_uf") and not merged_ui.get("selected_uf"):
            merged_ui["selected_uf"] = visual_focus.get("destination_uf")

    try:
        context = build_comparison_chat_context(
            comparison_id=comparison_id,
            question=clean_message,
            session_obj=session_obj,
            ui_context=merged_ui,
            limits=limits,
        )
    except AgenteComparaChatContextError as exc:
        return {
            "answer": "",
            "flow_type": AGENTE_COMPARA_COMPARISON_CHAT_FLOW_TYPE,
            "deterministic": True,
            "error": exc.error_code,
            "message": exc.message,
            "http_status": exc.http_status,
            "chat_available": False,
            "retryable": False,
        }
    except Exception as exc:
        logger.exception("comparison chat context build failed: %s", exc.__class__.__name__)
        return {
            "answer": "",
            "flow_type": AGENTE_COMPARA_COMPARISON_CHAT_FLOW_TYPE,
            "deterministic": True,
            "error": ERROR_CONTEXT_BUILD_FAILED,
            "error_code": ERROR_CONTEXT_BUILD_FAILED,
            "message": MSG_CONTEXT_BUILD_FAILED,
            "http_status": 500,
            "chat_available": True,
            "capability": CAPABILITY_READY,
            "retryable": False,
        }

    scope_meta = context.get("selected_scope") if isinstance(context.get("selected_scope"), dict) else {}
    scope = str(scope_meta.get("scope") or SCOPE_OVERVIEW)
    decision_request = bool(scope_meta.get("decision_request") or scope == SCOPE_DECISION)
    capability = str(scope_meta.get("capability") or resolve_chat_capability(get_comparison_state(session_obj)))
    if capability != CAPABILITY_READY:
        return {
            "answer": "",
            "flow_type": AGENTE_COMPARA_COMPARISON_CHAT_FLOW_TYPE,
            "deterministic": True,
            "error": ERROR_COMPARISON_CHAT_NOT_READY,
            "error_code": ERROR_COMPARISON_CHAT_NOT_READY,
            "message": CHAT_NOT_READY_MESSAGE,
            "http_status": 409,
            "chat_available": False,
            "capability": CAPABILITY_LOCKED,
            "reason": "comparison_not_ready",
            "retryable": False,
        }

    scope_key = comparison_chat_scope_key(context)
    cached = get_cached_comparison_chat_response(session_obj, request_id or "", scope_key=scope_key)
    if cached is not None:
        return {
            "answer": cached.get("answer") or "",
            "flow_type": cached.get("flow_type") or AGENTE_COMPARA_COMPARISON_CHAT_FLOW_TYPE,
            "deterministic": bool(cached.get("deterministic")),
            "scope": cached.get("scope") or scope,
            "basis": cached.get("basis") or {},
            "warnings": list(cached.get("warnings") or []),
            "cached": True,
            "chat_available": True,
            "capability": CAPABILITY_READY,
        }

    history_slice = sanitize_chat_history(history, max_history=limits["history_max_items"])
    comparison = context.get("comparison") if isinstance(context.get("comparison"), dict) else {}
    data_quality = context.get("data_quality") if isinstance(context.get("data_quality"), dict) else {}
    basis = {
        "comparable_rows": (context.get("comparability") or {}).get("fully_comparable_rows"),
        "table_count": comparison.get("table_count") or len(context.get("tables") or []),
        "selected_uf": scope_meta.get("selected_uf"),
        "capability": CAPABILITY_READY,
        "row_count_selected": data_quality.get("row_count_selected"),
        "memory_count_selected": data_quality.get("memory_count_selected"),
    }
    warnings = list(context.get("limitations") or [])[:5]

    absence_message = resolve_objective_absence_message(context, question=clean_message)
    if absence_message:
        payload = {
            "answer": finalize_comparison_chat_answer(absence_message, decision_request=False),
            "flow_type": AGENTE_COMPARA_COMPARISON_CHAT_FLOW_TYPE,
            "deterministic": True,
            "scope": scope,
            "basis": basis,
            "warnings": warnings,
            "chat_available": True,
            "capability": CAPABILITY_READY,
        }
        cache_comparison_chat_response(session_obj, request_id or "", payload, scope_key=scope_key)
        _safe_observability(
            comparison_id=comparison.get("comparison_id"),
            request_id=request_id,
            scope=scope,
            table_count=int(basis.get("table_count") or 0),
            row_count=int(basis.get("row_count_selected") or 0),
            memory_count=int(basis.get("memory_count_selected") or 0),
            latency_ms=int((time.perf_counter() - started) * 1000),
            model=None,
            success=True,
            fallback=True,
            error=None,
            selected_widget=(merged_ui.get("selected_widget") if isinstance(merged_ui, dict) else None),
            stage="objective_absence",
            retryable=False,
        )
        return payload

    try:
        prompt = build_comparison_chat_user_prompt(
            user_message=clean_message,
            history_slice=history_slice,
            context_payload=context,
            scope=scope,
        )
    except Exception as exc:
        logger.exception("comparison chat prompt build failed: %s", exc.__class__.__name__)
        return {
            "answer": "",
            "flow_type": AGENTE_COMPARA_COMPARISON_CHAT_FLOW_TYPE,
            "deterministic": True,
            "error": ERROR_PROMPT_BUILD_FAILED,
            "error_code": ERROR_PROMPT_BUILD_FAILED,
            "message": MSG_PROMPT_BUILD_FAILED,
            "http_status": 500,
            "chat_available": True,
            "capability": CAPABILITY_READY,
            "retryable": False,
            "scope": scope,
            "basis": basis,
        }

    if not isinstance(prompt, str) or not prompt.strip():
        return {
            "answer": "",
            "flow_type": AGENTE_COMPARA_COMPARISON_CHAT_FLOW_TYPE,
            "deterministic": True,
            "error": ERROR_PROMPT_BUILD_FAILED,
            "error_code": ERROR_PROMPT_BUILD_FAILED,
            "message": MSG_PROMPT_BUILD_FAILED,
            "http_status": 500,
            "chat_available": True,
            "capability": CAPABILITY_READY,
            "retryable": False,
            "scope": scope,
            "basis": basis,
        }

    try:
        result = _run_one_gemini_call(
            prompt=prompt,
            session_obj=session_obj,
            request_id=request_id,
            scope_key=scope_key,
            scope=scope,
            basis=basis,
            warnings=warnings,
            decision_request=decision_request,
        )
    except ComparisonChatProviderError as exc:
        payload = _provider_error_payload(exc, scope=scope, basis=basis, warnings=warnings)
        _safe_observability(
            comparison_id=comparison.get("comparison_id"),
            request_id=request_id,
            scope=scope,
            table_count=int(basis.get("table_count") or 0),
            row_count=int(basis.get("row_count_selected") or 0),
            memory_count=int(basis.get("memory_count_selected") or 0),
            latency_ms=int((time.perf_counter() - started) * 1000),
            model=None,
            success=False,
            fallback=False,
            error=exc.error_code,
            selected_widget=(merged_ui.get("selected_widget") if isinstance(merged_ui, dict) else None),
            stage=exc.stage,
            retryable=exc.retryable,
            exception_class=(exc.__cause__.__class__.__name__ if exc.__cause__ else exc.__class__.__name__),
        )
        return payload

    result["chat_available"] = True
    result["capability"] = CAPABILITY_READY
    _safe_observability(
        comparison_id=comparison.get("comparison_id"),
        request_id=request_id,
        scope=scope,
        table_count=int(basis.get("table_count") or 0),
        row_count=int(basis.get("row_count_selected") or 0),
        memory_count=int(basis.get("memory_count_selected") or 0),
        latency_ms=int((time.perf_counter() - started) * 1000),
        model=result.get("model"),
        success=True,
        fallback=bool(result.get("deterministic")),
        error=None,
        selected_widget=(merged_ui.get("selected_widget") if isinstance(merged_ui, dict) else None),
        stage="provider_response",
        retryable=False,
    )
    return result
