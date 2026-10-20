"""
Backend do chat inteligente do AgenteCompara baseado na comparação vigente.

Pergunta válida após READY exige usuário e franquia antes de qualquer geração.
Com usuário operacional autenticado: uma seleção tipada e, quando o plano é
válido, uma resposta fundamentada na publicação da ponte. No máximo essas duas
gerações. Sem identidade válida não há chamada ao Gemini nem evento financeiro.
Falhas de provider retornam erro estruturado (nunca HTTP 200 falso).
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
    authorize_comparison_snapshot_identity,
    build_comparison_chat_context,
    evaluate_comparison_chat_availability,
    resolve_chat_capability,
)
from app.agente_compara_chat_prompt import (
    build_comparison_chat_selection_prompt,
    build_comparison_chat_user_prompt,
)
from app.agente_compara_chat_semantic_selection import (
    MSG_COMPARISON_NOT_AUTHORIZED,
    MSG_COMPARISON_UNAVAILABLE,
    authorized_tables_from_context,
    copy_publication,
    load_confirmed_referents,
    operational_identity,
    publication_is_answerable,
    referents_for_prompt,
    remember_confirmed_referents,
    resolve_semantic_selection,
    selection_catalog_document,
    selection_response_json_schema,
    selective_chat_required,
    user_message_for_bridge_error,
)
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
from app.services.cleiton_billable_ai_call import (
    gemini_http_options,
)
from app.services.agente_compara_config_service import get_agente_compara_config
from app.cleiton_doc_escopo import (
    operational_cache_payload_is_current,
    stamp_operational_cache_payload,
)

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

        timeout_ms = 30_000
        raw = (os.getenv("GEMINI_HTTP_TIMEOUT_MS") or "").strip()
        if raw:
            try:
                timeout_ms = max(1_000, int(raw))
            except ValueError:
                pass
        return genai.Client(api_key=key, http_options=gemini_http_options(timeout_ms=timeout_ms))
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
    if not isinstance(payload, dict):
        return None
    if not operational_cache_payload_is_current(payload):
        return None
    return payload


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
    entry = stamp_operational_cache_payload({
        "answer": safe_answer,
        "flow_type": payload.get("flow_type") or AGENTE_COMPARA_COMPARISON_CHAT_FLOW_TYPE,
        "deterministic": bool(payload.get("deterministic")),
        "scope": payload.get("scope"),
        "basis": payload.get("basis") if isinstance(payload.get("basis"), dict) else {},
        "warnings": list(payload.get("warnings") or []),
        "scope_key": scope_key,
    })
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


_MOTIVOS_FRANQUIA_ESGOTADA = frozenset(
    {
        "degraded_saldo_insuficiente",
        "limite_atingido_free",
        "limite_atingido_starter_pro",
        "limite_atingido_plano_indefinido",
        "limite_atingido_fallback",
    }
)
_MOTIVOS_PLANO_EXPIRADO = frozenset(
    {
        "vigencia_expirada",
        "limite_ou_vigencia_avulso",
    }
)

ERROR_FRANCHISE_EXHAUSTED = "comparison_chat_franchise_exhausted"
ERROR_PLAN_BLOCKED = "comparison_chat_plan_blocked"
ERROR_PLAN_EXPIRED = "comparison_chat_plan_expired"
ERROR_ADMISSION_BLOCKED = "comparison_chat_admission_blocked"

MSG_PLAN_BLOCKED = "Sua franquia está bloqueada para operação no momento."
MSG_PLAN_BLOCKED_MANUAL = (
    "Sua franquia está temporariamente bloqueada por ação administrativa."
)
MSG_PLAN_EXPIRED = "A vigência operacional da sua franquia expirou."
MSG_ADMISSION_BLOCKED = "Não foi possível autorizar esta consulta no momento."


def _answer_response_config():
    """Teto executável da resposta analítica no Gemini 2.5.

    Nesse modelo o thinking entra em max_output_tokens. thinking_budget=0 e um
    candidato fazem a reserva usar o mesmo teto enviado ao provider. O padrão
    global das outras habilidades permanece o da fachada.
    """

    limites = {
        "candidate_count": 1,
        "max_output_tokens": 8192,
        "thinking_config": {"thinking_budget": 0},
    }
    try:
        from google.genai import types

        return types.GenerateContentConfig(
            candidate_count=1,
            max_output_tokens=8192,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
    except Exception as exc:
        logger.info(
            "comparison_chat_answer_config_sdk_unavailable exc_class=%s",
            exc.__class__.__name__,
        )
        return limites


def _selection_response_config():
    """JSON Schema do envelope e thinking desligado. Só a seleção usa esta config.

    response_json_schema preserva as chaves camelCase que a API aceita.
    Se o SDK recusar o conjunto, a chamada segue sem config nova: não há
    segundo transporte nem terceira geração.
    """

    try:
        from google.genai import types

        return types.GenerateContentConfig(
            temperature=0,
            candidate_count=1,
            max_output_tokens=2048,
            response_mime_type="application/json",
            response_json_schema=selection_response_json_schema(),
            tools=[],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
    except Exception as exc:
        logger.info(
            "comparison_chat_selection_config_unavailable exc_class=%s",
            exc.__class__.__name__,
        )
        return None


def _plano_codigo_do_usuario(usuario) -> str | None:
    from flask import has_app_context

    if usuario is None or not has_app_context():
        return None
    identity = operational_identity(usuario)
    if identity is None:
        return None
    try:
        from app.services.cleiton_plano_resolver import resolver_plano_operacional_para_franquia

        plano = resolver_plano_operacional_para_franquia(identity[2])
    except Exception:
        return None
    if plano is None:
        return None
    codigo = getattr(plano, "codigo", None)
    if not isinstance(codigo, str) or not codigo.strip():
        return None
    return codigo.strip()


def _classificar_recusa_financeira(motivo: str, plano_codigo: str | None) -> tuple[str, str] | None:
    """Mensagem segura. Não devolve crédito, token, saldo nem chave interna."""

    if motivo in _MOTIVOS_FRANQUIA_ESGOTADA:
        from app.services.cleiton_mensageria_operacao_service import (
            montar_mensagem_limite_plano_texto,
        )
        from app.services.plano_service import obter_nome_exibivel_plano

        return ERROR_FRANCHISE_EXHAUSTED, montar_mensagem_limite_plano_texto(
            obter_nome_exibivel_plano(plano_codigo)
        )
    if motivo == "bloqueio_manual":
        return ERROR_PLAN_BLOCKED, MSG_PLAN_BLOCKED_MANUAL
    if motivo == "franquia_bloqueada":
        return ERROR_PLAN_BLOCKED, MSG_PLAN_BLOCKED
    if motivo in _MOTIVOS_PLANO_EXPIRADO:
        return ERROR_PLAN_EXPIRED, MSG_PLAN_EXPIRED
    return None


_SEM_VINCULO_DE_IDENTIDADE = object()


def _erro_identidade_ausente(stage: str) -> ComparisonChatProviderError:
    """Recusa antes do Gemini. Não reserva crédito e não grava evento."""

    return ComparisonChatProviderError(
        ERROR_ADMISSION_BLOCKED,
        MSG_ADMISSION_BLOCKED,
        retryable=False,
        http_status=403,
        stage=stage,
    )


def _franquia_persistida(usuario) -> bool:
    """A franquia indicada pelo operador existe nesta base. Não cria linha."""

    tripla = operational_identity(usuario)
    if tripla is None:
        return False
    from app.extensions import db
    from app.models import Franquia

    return db.session.get(Franquia, tripla[2]) is not None


def _recusa_consulta_indisponivel(stage: str) -> ComparisonChatProviderError:
    logger.info("comparison_chat_operational_auth_unread")
    return ComparisonChatProviderError(
        ERROR_ADMISSION_BLOCKED,
        MSG_ADMISSION_BLOCKED,
        retryable=False,
        http_status=403,
        stage=stage,
    )


def _recusar_operacao_bloqueada(usuario, *, stage: str) -> ComparisonChatProviderError | None:
    """Autorização operacional da Júlia. Não reserva crédito nem lê teto de tokens.

    usuario=None não herda o usuário da requisição e não vira origem de sistema.
    A origem de sistema explícita permanece na fachada global, identificada.
    Franquia ausente, inválida ou ilegível recusa antes do Gemini.
    """

    from flask import has_app_context

    from app.models import User

    if usuario is None or getattr(usuario, "is_authenticated", False) is not True:
        return _erro_identidade_ausente(stage)
    if not isinstance(usuario, User):
        if operational_identity(usuario) is None or not has_app_context():
            return _erro_identidade_ausente(stage)
        try:
            franquia_existe = _franquia_persistida(usuario)
        except Exception:
            return _recusa_consulta_indisponivel(stage)
        if not franquia_existe:
            return _erro_identidade_ausente(stage)
    elif not has_app_context():
        return _erro_identidade_ausente(stage)
    try:
        from app.services.cleiton_operacao_autorizacao_service import (
            avaliar_autorizacao_operacao_por_franquia,
        )

        authz = avaliar_autorizacao_operacao_por_franquia(usuario)
        if not isinstance(authz, dict):
            return _recusa_consulta_indisponivel(stage)
        permitido = authz.get("permitido") is True
    except Exception:
        return _recusa_consulta_indisponivel(stage)
    if permitido:
        if operational_identity(usuario) is None:
            return _erro_identidade_ausente(stage)
        return None
    classe = _classificar_recusa_financeira(
        str(authz.get("motivo") or ""),
        _plano_codigo_do_usuario(usuario),
    )
    if classe is None:
        mensagem = authz.get("mensagem_usuario") or MSG_ADMISSION_BLOCKED
        return ComparisonChatProviderError(
            ERROR_ADMISSION_BLOCKED,
            mensagem,
            retryable=False,
            http_status=403,
            stage=stage,
        )
    codigo, mensagem = classe
    return ComparisonChatProviderError(
        codigo,
        mensagem,
        retryable=False,
        http_status=403,
        stage=stage,
    )


def _abrir_identidade_da_chamada(usuario, *, stage: str):
    """Grava a franquia do operador para o evento e o abatimento. Restaura depois."""

    from flask import has_app_context

    from app.consumo_identidade import (
        TIPO_ORIGEM_HTTP_USUARIO,
        get_consumo_identidade,
        identidade_de_usuario,
        set_consumo_identidade,
    )
    from app.models import User

    if usuario is None or getattr(usuario, "is_authenticated", False) is not True:
        raise _erro_identidade_ausente(stage)
    if isinstance(usuario, User):
        try:
            ident = identidade_de_usuario(usuario, TIPO_ORIGEM_HTTP_USUARIO)
        except Exception as exc:
            raise ComparisonChatProviderError(
                ERROR_ADMISSION_BLOCKED,
                MSG_ADMISSION_BLOCKED,
                retryable=False,
                http_status=403,
                stage=stage,
                cause=exc,
            ) from exc
        if ident.get("usuario_id") is None or ident.get("franquia_id") is None:
            raise _erro_identidade_ausente(stage)
    else:
        tripla = operational_identity(usuario)
        if tripla is None or not has_app_context():
            raise _erro_identidade_ausente(stage)
        conta_id, usuario_id, franquia_id = tripla
        ident = {
            "conta_id": conta_id,
            "franquia_id": franquia_id,
            "usuario_id": usuario_id,
            "tipo_origem": TIPO_ORIGEM_HTTP_USUARIO,
            "origem_sistema": False,
        }
    anterior = get_consumo_identidade()
    set_consumo_identidade(ident)
    return anterior


def _fechar_identidade_da_chamada(anterior) -> None:
    from app.consumo_identidade import clear_consumo_identidade, set_consumo_identidade

    if anterior is _SEM_VINCULO_DE_IDENTIDADE:
        return
    if anterior is None:
        clear_consumo_identidade()
        return
    set_consumo_identidade(anterior)


def _safe_finish_reason(response: Any) -> str | None:
    """Nome do finish_reason. Não devolve texto da resposta."""

    candidates = getattr(response, "candidates", None)
    if not isinstance(candidates, (list, tuple)) or not candidates:
        return None
    candidate = candidates[0]
    reason = getattr(candidate, "finish_reason", None)
    if reason is None and isinstance(candidate, dict):
        reason = candidate.get("finish_reason") or candidate.get("finishReason")
    if reason is None:
        return None
    name = getattr(reason, "name", None)
    if isinstance(name, str) and name.strip() and name.strip().lower() != "none":
        token = name.strip()
    elif isinstance(reason, str):
        token = reason.strip()
    else:
        token = str(reason).strip()
        if "." in token:
            token = token.rsplit(".", 1)[-1].strip()
    if not token or any(char.isspace() for char in token) or len(token) > 64:
        return None
    if not all(char.isalnum() or char == "_" for char in token):
        return None
    return token


def _generate_governed_text(
    *,
    prompt: str,
    usuario=None,
    config=None,
    billable_stage: str = "billable_ai_gate",
    generation_meta: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Autoriza a franquia, gera com a governança da Júlia e debita o consumo real."""

    from app.services.cleiton_ai_data_governance import CleitonAiGovernanceBlockedError

    client = _get_client()
    model = resolve_comparison_chat_model()
    recusa = _recusar_operacao_bloqueada(usuario, stage=billable_stage)
    if recusa is not None:
        raise recusa
    vinculo = _SEM_VINCULO_DE_IDENTIDADE
    try:
        vinculo = _abrir_identidade_da_chamada(usuario, stage=billable_stage)
        response = cleiton_governed_generate_content(
            client,
            model=model,
            contents=prompt,
            config=config,
            agent="agente_compara",
            flow_type=AGENTE_COMPARA_COMPARISON_CHAT_FLOW_TYPE,
            api_key_label=_api_key_label(),
        )
    except ComparisonChatProviderError:
        raise
    except CleitonAiGovernanceBlockedError as exc:
        raise ComparisonChatProviderError(
            ERROR_PROVIDER_REQUEST_FAILED,
            MSG_PROVIDER_REQUEST_FAILED,
            retryable=False,
            http_status=503,
            stage=billable_stage,
            cause=exc,
        ) from exc
    except Exception as exc:
        if _is_provider_timeout(exc):
            raise ComparisonChatProviderTimeout(cause=exc) from exc
        logger.warning(
            "comparison chat provider failure model=%s exc_type=%s",
            model,
            exc.__class__.__name__,
        )
        raise ComparisonChatProviderRequestError(cause=exc) from exc
    finally:
        _fechar_identidade_da_chamada(vinculo)
    if generation_meta is not None:
        generation_meta["finish_reason"] = _safe_finish_reason(response)
    return _extract_response_text(response), model


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
    usuario=None,
    config=None,
    billable_stage: str = "billable_ai_gate",
) -> dict:
    text, model = _generate_governed_text(
        prompt=prompt,
        usuario=usuario,
        config=_answer_response_config() if config is None else config,
        billable_stage=billable_stage,
    )
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


def _lembrar_resposta_comparacao(usuario, comparison_id: str | None, texto: str) -> None:
    if usuario is None or not isinstance(texto, str) or not texto.strip():
        return
    from app.models import SolicitacaoEntregaCanal
    from app.services.canal_resposta_compartilhavel_service import (
        montar_contexto,
        registrar_resposta_compartilhavel,
    )

    user_id = getattr(usuario, "id", None)
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        return
    identificador = (comparison_id or "").strip() or None
    registrar_resposta_compartilhavel(
        usuario=usuario,
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_COMPARACAO,
        contexto_conversa=montar_contexto(
            superficie=SolicitacaoEntregaCanal.SUPERFICIE_COMPARACAO,
            user_id=user_id,
            comparison_id=identificador,
        ),
        texto=texto,
        comparison_id=identificador,
    )


def responder_entrega_whatsapp_se_pedida(
    user_message: str,
    history: list,
    *,
    usuario,
    identidade_requisicao: str | None = None,
    contexto_conversa: str | None = None,
    comparison_id: str | None = None,
) -> dict | None:
    """None segue a comparação atual. Dict é a confirmação determinística."""
    from app.models import SolicitacaoEntregaCanal
    from app.services.canal_entrega_web_whatsapp_service import (
        avaliar_pedido_de_entrega,
        mensagem_da_entrega,
    )
    from app.services.cleiton_ai_data_governance import (
        USER_SAFE_PREPARATION_FAILED,
        CleitonAiGovernanceBlockedError,
    )

    try:
        client = _get_client()
        model = resolve_comparison_chat_model()
    except ComparisonChatProviderError:
        return None
    try:
        resultado = avaliar_pedido_de_entrega(
            usuario=usuario,
            superficie=SolicitacaoEntregaCanal.SUPERFICIE_COMPARACAO,
            mensagem=user_message,
            historico=history,
            agent="agente_compara",
            flow_type=AGENTE_COMPARA_COMPARISON_CHAT_FLOW_TYPE,
            client=client,
            model=model,
            api_key_label=_api_key_label(),
            identidade_requisicao=identidade_requisicao,
            contexto_conversa=contexto_conversa,
            comparison_id=comparison_id,
        )
    except CleitonAiGovernanceBlockedError:
        return {
            "answer": USER_SAFE_PREPARATION_FAILED,
            "flow_type": AGENTE_COMPARA_COMPARISON_CHAT_FLOW_TYPE,
            "deterministic": True,
            "chat_available": True,
        }
    if resultado is None:
        return None
    return {
        "answer": mensagem_da_entrega(resultado),
        "flow_type": AGENTE_COMPARA_COMPARISON_CHAT_FLOW_TYPE,
        "deterministic": True,
        "chat_available": True,
    }


def _cache_deterministic_answer(
    *,
    answer: str,
    session_obj,
    request_id: str | None,
    scope_key: str,
    scope: str,
    basis: dict,
    warnings: list[str],
    usuario,
    comparison_id: str | None,
    started: float,
    merged_ui: dict,
    stage: str,
) -> dict:
    payload = {
        "answer": finalize_comparison_chat_answer(answer, decision_request=False),
        "flow_type": AGENTE_COMPARA_COMPARISON_CHAT_FLOW_TYPE,
        "deterministic": True,
        "scope": scope,
        "basis": basis,
        "warnings": warnings,
        "chat_available": True,
        "capability": CAPABILITY_READY,
    }
    cache_comparison_chat_response(session_obj, request_id or "", payload, scope_key=scope_key)
    _lembrar_resposta_comparacao(usuario, comparison_id, payload.get("answer") or "")
    _safe_observability(
        comparison_id=comparison_id,
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
        stage=stage,
        retryable=False,
    )
    return payload


def _reply_from_semantic_selection(
    *,
    user_message: str,
    history_slice: list[dict],
    context: dict,
    session_obj,
    usuario,
    request_id: str | None,
    scope_key: str,
    scope: str,
    decision_request: bool,
    basis: dict,
    warnings: list[str],
    started: float,
    merged_ui: dict,
) -> dict:
    """Uma seleção e, no máximo, uma resposta. Sem terceira geração."""

    comparison = context.get("comparison") if isinstance(context.get("comparison"), dict) else {}
    comparison_id = str(comparison.get("comparison_id") or "").strip() or None
    identity = operational_identity(usuario)
    if identity is None:
        return _cache_deterministic_answer(
            answer=MSG_COMPARISON_NOT_AUTHORIZED,
            session_obj=session_obj,
            request_id=request_id,
            scope_key=scope_key,
            scope=scope,
            basis=basis,
            warnings=warnings,
            usuario=usuario,
            comparison_id=comparison_id,
            started=started,
            merged_ui=merged_ui,
            stage="semantic_not_authorized",
        )
    conta_id, usuario_id, franquia_id = identity
    authorization = authorize_comparison_snapshot_identity(
        session_obj=session_obj,
        conta_id=conta_id,
        usuario_id=usuario_id,
        franquia_id=franquia_id,
        comparison_id=comparison_id,
    )
    if authorization.get("error_code") or not authorization.get("comparison_id"):
        return _cache_deterministic_answer(
            answer=MSG_COMPARISON_NOT_AUTHORIZED,
            session_obj=session_obj,
            request_id=request_id,
            scope_key=scope_key,
            scope=scope,
            basis=basis,
            warnings=warnings,
            usuario=usuario,
            comparison_id=comparison_id,
            started=started,
            merged_ui=merged_ui,
            stage="semantic_not_authorized",
        )
    authorized_comparison_id = str(authorization["comparison_id"])
    tables = authorized_tables_from_context(context)
    referents = referents_for_prompt(load_confirmed_referents(session_obj, authorized_comparison_id))
    try:
        selection_prompt = build_comparison_chat_selection_prompt(
            user_message=user_message,
            history_slice=history_slice,
            referents=referents,
            authorized_tables=tables,
            catalog=selection_catalog_document(),
        )
    except Exception as exc:
        logger.exception("comparison chat selection prompt failed: %s", exc.__class__.__name__)
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
    generation_meta: dict[str, Any] = {}
    try:
        selection_text, _model = _generate_governed_text(
            prompt=selection_prompt,
            usuario=usuario,
            config=_selection_response_config(),
            billable_stage="semantic_selection",
            generation_meta=generation_meta,
        )
    except ComparisonChatProviderError as exc:
        return _provider_failure(
            exc,
            comparison_id=authorized_comparison_id,
            request_id=request_id,
            scope=scope,
            basis=basis,
            warnings=warnings,
            started=started,
            merged_ui=merged_ui,
            observability_stage="semantic_selection",
        )
    selection = resolve_semantic_selection(
        selection_text,
        authorized_tables=tables,
        finish_reason=generation_meta.get("finish_reason"),
    )
    if selection.action != "query" or not isinstance(selection.plan, dict):
        return _cache_deterministic_answer(
            answer=selection.clarification or MSG_COMPARISON_UNAVAILABLE,
            session_obj=session_obj,
            request_id=request_id,
            scope_key=scope_key,
            scope=scope,
            basis=basis,
            warnings=warnings,
            usuario=usuario,
            comparison_id=authorized_comparison_id,
            started=started,
            merged_ui=merged_ui,
            stage="semantic_clarification" if selection.action == "clarify" else "semantic_malformed",
        )
    published = execute_comparison_chat_analytical_plan(
        session_obj=session_obj,
        usuario=usuario,
        plan=selection.plan,
        comparison_id=authorized_comparison_id,
    )
    publication = published.get("publication") if isinstance(published, dict) else None
    if not isinstance(published, dict) or published.get("status") != "ok" or not publication_is_answerable(publication):
        return _cache_deterministic_answer(
            answer=user_message_for_bridge_error(published.get("error_code") if isinstance(published, dict) else None),
            session_obj=session_obj,
            request_id=request_id,
            scope_key=scope_key,
            scope=scope,
            basis=basis,
            warnings=warnings,
            usuario=usuario,
            comparison_id=authorized_comparison_id,
            started=started,
            merged_ui=merged_ui,
            stage="semantic_execution",
        )
    safe_publication = copy_publication(publication)
    try:
        answer_prompt = build_comparison_chat_user_prompt(
            user_message=user_message,
            history_slice=history_slice,
            context_payload={},
            scope=scope,
            evidence_publication=safe_publication,
            confirmed_referents=referents,
            authorized_tables=tables,
        )
    except Exception as exc:
        logger.exception("comparison chat answer prompt failed: %s", exc.__class__.__name__)
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
            prompt=answer_prompt,
            session_obj=session_obj,
            request_id=request_id,
            scope_key=scope_key,
            scope=scope,
            basis=basis,
            warnings=warnings,
            decision_request=decision_request,
            usuario=usuario,
            billable_stage="answer_generation",
        )
    except ComparisonChatProviderError as exc:
        return _provider_failure(
            exc,
            comparison_id=authorized_comparison_id,
            request_id=request_id,
            scope=scope,
            basis=basis,
            warnings=warnings,
            started=started,
            merged_ui=merged_ui,
            observability_stage="answer_generation",
        )
    remember_confirmed_referents(
        session_obj,
        authorized_comparison_id,
        safe_publication,
        tables,
        referents,
        referent_criterion=selection.referent_criterion,
    )
    result["chat_available"] = True
    result["capability"] = CAPABILITY_READY
    _safe_observability(
        comparison_id=authorized_comparison_id,
        request_id=request_id,
        scope=scope,
        table_count=int(basis.get("table_count") or 0),
        row_count=int(basis.get("row_count_selected") or 0),
        memory_count=int(basis.get("memory_count_selected") or 0),
        latency_ms=int((time.perf_counter() - started) * 1000),
        model=result.get("model"),
        success=True,
        fallback=False,
        error=None,
        selected_widget=(merged_ui.get("selected_widget") if isinstance(merged_ui, dict) else None),
        stage="provider_response",
        retryable=False,
    )
    _lembrar_resposta_comparacao(usuario, authorized_comparison_id, result.get("answer") or "")
    return result


def _provider_failure(
    exc: ComparisonChatProviderError,
    *,
    comparison_id: str | None,
    request_id: str | None,
    scope: str,
    basis: dict,
    warnings: list[str],
    started: float,
    merged_ui: dict,
    observability_stage: str,
) -> dict:
    payload = _provider_error_payload(exc, scope=scope, basis=basis, warnings=warnings)
    _safe_observability(
        comparison_id=comparison_id,
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
        stage=observability_stage or exc.stage,
        retryable=exc.retryable,
        exception_class=(exc.__cause__.__class__.__name__ if exc.__cause__ else exc.__class__.__name__),
    )
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
    usuario=None,
    entrega_ja_avaliada: bool = False,
    identidade_requisicao: str | None = None,
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

    if usuario is not None and not entrega_ja_avaliada:
        from app.models import SolicitacaoEntregaCanal
        from app.services.canal_resposta_compartilhavel_service import montar_contexto

        user_id = getattr(usuario, "id", None)
        contexto_comparacao = (
            montar_contexto(
                superficie=SolicitacaoEntregaCanal.SUPERFICIE_COMPARACAO,
                user_id=int(user_id),
                comparison_id=comparison_id,
            )
            if isinstance(user_id, int) and not isinstance(user_id, bool)
            else None
        )
        entrega = responder_entrega_whatsapp_se_pedida(
            clean_message,
            list(history) if isinstance(history, list) else [],
            usuario=usuario,
            identidade_requisicao=identidade_requisicao or request_id,
            contexto_conversa=contexto_comparacao,
            comparison_id=comparison_id,
        )
        if entrega is not None:
            return entrega

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
        _lembrar_resposta_comparacao(usuario, comparison.get("comparison_id"), payload.get("answer") or "")
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

    if selective_chat_required(usuario):
        return _reply_from_semantic_selection(
            user_message=clean_message,
            history_slice=history_slice,
            context=context,
            session_obj=session_obj,
            usuario=usuario,
            request_id=request_id,
            scope_key=scope_key,
            scope=scope,
            decision_request=decision_request,
            basis=basis,
            warnings=warnings,
            started=started,
            merged_ui=merged_ui,
        )

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
            usuario=usuario,
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
    _lembrar_resposta_comparacao(usuario, comparison.get("comparison_id"), result.get("answer") or "")
    return result


def execute_comparison_chat_analytical_plan(
    *,
    session_obj,
    usuario,
    plan,
    comparison_id: str | None = None,
    operation_id: str | None = None,
) -> dict[str, Any]:
    """Executa um plano já validado pela ponte analítica.

    Não escolhe consultas, não chama Gemini e não grava operação.
    """

    from app.agente_compara_chat_analytical_bridge import execute_authorized_analytical_plan

    return execute_authorized_analytical_plan(
        session_obj=session_obj,
        usuario=usuario,
        plan=plan,
        comparison_id=comparison_id,
        operation_id=operation_id,
    )
