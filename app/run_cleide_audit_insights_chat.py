"""
Backend do chat analítico pós-BI da Cleide Auditoria.

Fluxo isolado do chat documental. Usa lote processado como fonte de verdade.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

from flask import has_app_context

from app.cleide_audit_doc_service import (
    CLEIDE_AUDIT_INSIGHTS_CHAT_FLOW_TYPE,
    cleide_audit_insights_chat_idempotency_key,
)
from app.cleide_audit_insights_context import (
    clear_conversation_focus,
    get_conversation_focus,
    insights_batch_scope,
    load_audit_insights_bundle,
    set_conversation_focus,
)
from app.cleide_audit_insights_prompt import build_insights_user_prompt
from app.cleide_audit_insights_query import (
    GEMINI_ANALYTICAL_INTENTS,
    INTENT_DOCUMENT_FOLLOWUP,
    INTENT_EXPLAIN_CALCULATION,
    INTENT_LOCATE_DOCUMENT,
    INTENT_OVERCHARGED,
    INTENT_SMALLEST_DIVERGENCES,
    INTENT_TOP_DIVERGENCES,
    INTENT_UNDERCHARGED,
    build_analytical_package,
    build_compact_context_for_gemini,
    classify_intent,
    format_managerial_fallback,
    judgment_prudence_note,
    resolve_document_target,
    resolve_ranking_limit,
    try_deterministic_response,
)
from app.run_cleide_audit_chat import (
    DEFAULT_MODEL_FALLBACK,
    PROCESSING_ERROR_MESSAGE,
    SERVICE_UNAVAILABLE_MESSAGE,
    sanitize_chat_history,
)
from app.run_cleiton_gemini_governance import cleiton_governed_generate_content
from app.services.cleide_audit_config_service import get_cleide_audit_config

logger = logging.getLogger(__name__)

INSIGHTS_CHAT_IDEMPOTENCY_CACHE_SESSION_KEY = "cleide_audit_insights_chat_idempotency_cache"
INSIGHTS_CHAT_CACHE_MAX_ENTRIES = 50

_JUDGMENT_DISCLAIMER_HINTS = (
    "validação",
    "validacao",
    "confirmação final",
    "confirmacao final",
    "revisão dos documentos",
    "revisao dos documentos",
    "hipótese",
    "hipotese",
    "recomendo validar",
)

_CATEGORICAL_PATTERNS = (
    r"\bestá errad[oa]\b",
    r"\bé indevid[oa]\b",
    r"\bcobrança indevida\b",
    r"\bcobranca indevida\b",
    r"\bcobrança é ilícita\b",
    r"\bcobranca e ilicita\b",
    r"\bcobrou indevidamente\b",
    r"\bhouve fraude\b",
    r"\bé fraude\b",
    r"\ba transportadora fraudou\b",
    r"\ba transportadora cobrou indevidamente\b",
    r"\ba empresa é culpada\b",
    r"\ba decisão é\b",
    r"\bconfirmo que\b",
    r"\bcom certeza está errado\b",
    r"\bresponsabilidade (?:é|da)\b",
    r"\bcobrança irregular\b",
    r"\bcobranca irregular\b",
)

_REPLACEMENTS = (
    (r"\bestá errad[oa]\b", "há divergência calculada que pode indicar diferença"),
    (r"\bé indevid[oa]\b", "pode estar relacionado a divergência; recomendo validar se"),
    (r"\bcobrança indevida\b", "divergência calculada"),
    (r"\bcobranca indevida\b", "divergencia calculada"),
    (r"\bcobrança é ilícita\b", "pode haver divergência calculada; recomendo validar"),
    (r"\bcobranca e ilicita\b", "pode haver divergencia calculada; recomendo validar"),
    (r"\bcobrou indevidamente\b", "registrou cobrança acima do esperado calculado"),
    (r"\bhouve fraude\b", "não há base calculada para afirmar fraude"),
    (r"\bé fraude\b", "não há base calculada para afirmar fraude"),
    (r"\ba transportadora fraudou\b", "com base nos dados disponíveis, pode haver indício de divergência"),
    (r"\ba transportadora cobrou indevidamente\b", "a transportadora registrou valor acima do esperado calculado"),
    (r"\ba empresa é culpada\b", "não há base calculada para atribuir culpa; recomendo validar"),
    (r"\ba decisão é\b", "a decisão final é do usuário; a análise indica"),
    (r"\bconfirmo que\b", "com base nos dados disponíveis, há indício de que"),
    (r"\bcom certeza está errado\b", "pode haver divergência calculada"),
    (r"\bresponsabilidade (?:é|da)\b", "a responsabilidade pela decisão final é do usuário; quanto a"),
    (r"\bcobrança irregular\b", "divergência calculada"),
    (r"\bcobranca irregular\b", "divergencia calculada"),
)

# Identidade indevida apenas no fechamento da resposta (não no corpo).
_FORBIDDEN_SIGNATORY = (
    r"(?:\*{0,2})\s*(?:"
    r"Cleide|"
    r"Agente\s*Frete|"
    r"Analista\s+de\s+Frete(?:\s+Experiente)?|"
    r"Auditora\s+Virtual|"
    r"Auditoria\s+Cleide"
    r")\s*(?:\*{0,2})"
)
_CLOSING_SALUTATION = r"(?:\*{0,2})\s*(?:Atenciosamente|Cordialmente|Abs\.?|Atte\.?)\s*(?:\*{0,2})\s*,?"
_SENDER_SIGNATURE_WITH_SALUTATION_RE = re.compile(
    rf"(?is)(?:^|\n)\s*(?P<salutation>{_CLOSING_SALUTATION})\s*"
    rf"(?:\n+\s*{_FORBIDDEN_SIGNATORY}\s*)+\s*$"
)
_TRAILING_FORBIDDEN_IDENTITY_RE = re.compile(
    rf"(?is)(?:\n+\s*{_FORBIDDEN_SIGNATORY}\s*){{1,6}}\s*$"
)
_SAFE_PLACEHOLDER_SIGNATURE = "Atenciosamente,\n\n[Seu nome]"


def sanitize_cleide_sender_signature(text: str) -> str:
    """
    Remove assinatura indevida da Cleide/AgenteFrete no fechamento da resposta.
    Não altera menções legítimas no corpo do texto.
    """
    cleaned = (text or "").rstrip()
    if not cleaned:
        return cleaned

    match = _SENDER_SIGNATURE_WITH_SALUTATION_RE.search(cleaned)
    if match:
        prefix = cleaned[: match.start()].rstrip()
        if prefix:
            return f"{prefix}\n\n{_SAFE_PLACEHOLDER_SIGNATURE}"
        return _SAFE_PLACEHOLDER_SIGNATURE

    # Fechamento sem saudação explícita, só bloco de identidade no final.
    trailing = _TRAILING_FORBIDDEN_IDENTITY_RE.search(cleaned)
    if trailing:
        # Só remove se houver conteúdo antes (evita apagar resposta inteira curta).
        prefix = cleaned[: trailing.start()].rstrip()
        if not prefix:
            return cleaned
        # Exige que o trecho final seja só identidade (sem frases longas).
        block = trailing.group(0)
        identity_lines = [
            re.sub(r"\*+", "", line).strip()
            for line in block.splitlines()
            if line.strip()
        ]
        if identity_lines and all(
            re.fullmatch(
                r"(?i)Cleide|Agente\s*Frete|Analista\s+de\s+Frete(?:\s+Experiente)?"
                r"|Auditora\s+Virtual|Auditoria\s+Cleide",
                line or "",
            )
            for line in identity_lines
        ):
            return f"{prefix}\n\n{_SAFE_PLACEHOLDER_SIGNATURE}"
    return cleaned


def soften_insights_gemini_output(text: str) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return cleaned
    # Never leak the internal label into user-facing replies.
    cleaned = re.sub(r"(?i)\*?Regra de ouro:\s*", "", cleaned)
    cleaned = re.sub(r"(?im)^\s*---\s*$", "", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    lowered = cleaned.lower()
    has_categorical = any(re.search(pattern, lowered) for pattern in _CATEGORICAL_PATTERNS)
    result = cleaned
    for pattern, replacement in _REPLACEMENTS:
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
    if has_categorical and not result.lower().startswith("**análise preliminar"):
        result = "**Análise preliminar (hipóteses, não conclusão definitiva):**\n\n" + result
    if has_categorical:
        result_lower = result.lower()
        if not any(hint in result_lower for hint in _JUDGMENT_DISCLAIMER_HINTS):
            result = result.rstrip() + judgment_prudence_note()
    return sanitize_cleide_sender_signature(result)


def finalize_insights_answer(text: str, *, soften: bool = False) -> str:
    """Normaliza a resposta final antes de cache/retorno (assinatura indevida etc.)."""
    if soften:
        return soften_insights_gemini_output(text)
    return sanitize_cleide_sender_signature((text or "").strip())


def _api_key_label() -> str:
    if os.getenv("GEMINI_API_KEY_1"):
        return "GEMINI_API_KEY_1"
    if os.getenv("GEMINI_API_KEY"):
        return "GEMINI_API_KEY"
    return "unknown"


def _get_client():
    key = os.getenv("GEMINI_API_KEY_1") or os.getenv("GEMINI_API_KEY")
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
        return genai.Client(api_key=key, http_options=genai_types.HttpOptions(timeout=timeout_ms))
    except Exception as exc:
        logger.error("Falha ao inicializar cliente Gemini para Cleide Insights: %s", exc)
        return None


def _get_model_candidates() -> list[str]:
    candidates = [
        (os.getenv("GEMINI_MODEL_TEXT") or "").strip(),
        "gemini-2.5-flash",
        (os.getenv("CLEIDE_AUDIT_INSIGHTS_CHAT_MODEL_FALLBACK") or "").strip(),
        DEFAULT_MODEL_FALLBACK,
    ]
    seen: set[str] = set()
    out: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


def _get_max_history(*, max_history: int | None = None) -> int:
    """Janela de histórico controlada pelo ADM (`chat_max_history`), sem número fixo local."""
    if max_history is not None:
        return max(0, int(max_history))
    if not has_app_context():
        return max(0, int(get_cleide_audit_config().chat_max_history))
    return max(0, int(get_cleide_audit_config().chat_max_history))


def get_cached_insights_chat_response(session_obj, request_id: str, *, batch_scope: str = "") -> dict | None:
    ref = (request_id or "").strip()
    if not ref:
        return None
    cache = session_obj.get(INSIGHTS_CHAT_IDEMPOTENCY_CACHE_SESSION_KEY)
    if not isinstance(cache, dict):
        return None
    payload = cache.get(cleide_audit_insights_chat_idempotency_key(ref, batch_scope))
    return payload if isinstance(payload, dict) else None


def cache_insights_chat_response(
    session_obj,
    request_id: str,
    payload: dict,
    *,
    batch_scope: str = "",
) -> None:
    ref = (request_id or "").strip()
    if not ref or not isinstance(payload, dict):
        return
    cache = session_obj.get(INSIGHTS_CHAT_IDEMPOTENCY_CACHE_SESSION_KEY)
    if not isinstance(cache, dict):
        cache = {}
    cache_key = cleide_audit_insights_chat_idempotency_key(ref, batch_scope)
    # Guardrail terminal: resposta em cache já sem assinatura indevida.
    safe_answer = finalize_insights_answer(payload.get("answer") or "")
    payload["answer"] = safe_answer
    cache_entry = {
        "answer": safe_answer,
        "flow_type": payload.get("flow_type") or CLEIDE_AUDIT_INSIGHTS_CHAT_FLOW_TYPE,
        "deterministic": bool(payload.get("deterministic")),
        "batch_scope": batch_scope,
    }
    if cache_key in cache:
        del cache[cache_key]
    cache[cache_key] = cache_entry
    while len(cache) > INSIGHTS_CHAT_CACHE_MAX_ENTRIES:
        oldest_key = next(iter(cache))
        del cache[oldest_key]
    session_obj[INSIGHTS_CHAT_IDEMPOTENCY_CACHE_SESSION_KEY] = cache
    session_obj.modified = True


def _normalize_visual_focus(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    cleaned: dict[str, Any] = {}
    for key in ("chart_key", "carrier", "origin_uf", "destination_uf", "issue_date"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            cleaned[key] = value.strip()
    return cleaned or None


def _infer_last_metric(message: str, intent: str) -> str | None:
    text = " ".join((message or "").lower().split())
    for token in ("diverg", "imposto", "peso", "taxa", "transportadora", "cidade"):
        if token in text:
            return token
    return intent or None


_RANKING_INTENTS = {
    INTENT_TOP_DIVERGENCES,
    INTENT_OVERCHARGED,
    INTENT_UNDERCHARGED,
    INTENT_SMALLEST_DIVERGENCES,
}


def _update_focus_after_response(
    session_obj,
    *,
    batch_scope: str,
    intent: str,
    message: str,
    context_rows: list[dict] | None,
    visual_focus: dict | None,
    conversation_focus: dict | None,
) -> None:
    if intent in _RANKING_INTENTS:
        limit, _explicit = resolve_ranking_limit(message, conversation_focus=conversation_focus)
        set_conversation_focus(
            session_obj,
            batch_scope=batch_scope,
            document_number=(conversation_focus or {}).get("document_number"),
            row_index=(conversation_focus or {}).get("row_index"),
            last_intent=intent,
            last_metric=f"ranking:{intent}:{limit}",
            visual_focus=visual_focus or (conversation_focus or {}).get("visual_focus"),
            last_ranking_limit=limit,
            last_ranking_intent=intent,
            preserve_document=True,
        )
        return

    if intent not in {INTENT_EXPLAIN_CALCULATION, INTENT_LOCATE_DOCUMENT, INTENT_DOCUMENT_FOLLOWUP}:
        return
    if not context_rows or len(context_rows) != 1:
        # Duplicidade ou ausência: não grava foco ambíguo.
        return
    row = context_rows[0]
    set_conversation_focus(
        session_obj,
        batch_scope=batch_scope,
        document_number=str(row.get("document_number") or "") or None,
        row_index=row.get("row_index"),
        last_intent=intent,
        last_metric=_infer_last_metric(message, intent),
        visual_focus=visual_focus or (conversation_focus or {}).get("visual_focus"),
    )


def _run_gemini_analytical_reply(
    *,
    bundle: dict,
    intent: str,
    clean_message: str,
    history_slice: list,
    focus: dict | None,
    session_obj,
    request_id: str | None,
    batch_scope: str,
    processing_message: str,
) -> dict:
    package = build_analytical_package(bundle, visual_focus=focus)
    compact = build_compact_context_for_gemini(bundle, intent, visual_focus=focus)
    fallback_answer = format_managerial_fallback(
        bundle,
        intent,
        package=package,
        visual_focus=focus,
        user_message=clean_message,
    )
    client = _get_client()
    if not client:
        result_payload = {
            "answer": finalize_insights_answer(fallback_answer),
            "flow_type": CLEIDE_AUDIT_INSIGHTS_CHAT_FLOW_TYPE,
            "deterministic": True,
            "intent": intent,
        }
        cache_insights_chat_response(
            session_obj,
            request_id or "",
            result_payload,
            batch_scope=batch_scope,
        )
        return result_payload

    prompt = build_insights_user_prompt(
        user_message=clean_message,
        history_slice=history_slice,
        context_payload=compact,
        intent=intent,
    )
    last_error: Exception | None = None
    for model in _get_model_candidates():
        try:
            response = cleiton_governed_generate_content(
                client,
                model=model,
                contents=prompt,
                agent="cleide",
                flow_type=CLEIDE_AUDIT_INSIGHTS_CHAT_FLOW_TYPE,
                api_key_label=_api_key_label(),
            )
            text = (getattr(response, "text", None) or "").strip()
            if text:
                text = finalize_insights_answer(text, soften=True)
                result_payload = {
                    "answer": text,
                    "flow_type": CLEIDE_AUDIT_INSIGHTS_CHAT_FLOW_TYPE,
                    "deterministic": False,
                    "intent": intent,
                }
                cache_insights_chat_response(
                    session_obj,
                    request_id or "",
                    result_payload,
                    batch_scope=batch_scope,
                )
                return result_payload
            last_error = ValueError("Resposta vazia do modelo")
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Cleide insights chat provider failure: model=%s exc_type=%s message=%s",
                model,
                exc.__class__.__name__,
                exc,
            )
    if last_error:
        logger.exception("Cleide insights chat falhou após fallbacks: %s", last_error)
    # Preserva resposta determinística útil; não mascara falha do provedor no conteúdo.
    result_payload = {
        "answer": finalize_insights_answer(fallback_answer),
        "flow_type": CLEIDE_AUDIT_INSIGHTS_CHAT_FLOW_TYPE,
        "deterministic": True,
        "intent": intent,
    }
    cache_insights_chat_response(
        session_obj,
        request_id or "",
        result_payload,
        batch_scope=batch_scope,
    )
    return result_payload


def chat_cleide_audit_insights_reply(
    user_message: str,
    history: list,
    *,
    session_obj,
    request_id: str | None = None,
    visual_focus: dict | None = None,
    max_history: int | None = None,
    question_max_chars: int | None = None,
    fallback_message: str | None = None,
) -> dict:
    audit_cfg = get_cleide_audit_config()
    message_limit = (
        int(question_max_chars)
        if question_max_chars is not None
        else int(audit_cfg.question_max_chars)
    )
    processing_message = (
        (fallback_message or audit_cfg.fallback_message or PROCESSING_ERROR_MESSAGE).strip()
        or PROCESSING_ERROR_MESSAGE
    )
    # Memória/contexto textual: controlado pelo ADM via chat_max_history (sem hardcode).
    history_limit = _get_max_history(max_history=max_history)

    clean_message = (user_message or "").strip()
    if not clean_message:
        return {
            "answer": "",
            "flow_type": CLEIDE_AUDIT_INSIGHTS_CHAT_FLOW_TYPE,
            "deterministic": True,
            "error": "invalid_message",
            "message": "Mensagem vazia.",
        }
    if len(clean_message) > message_limit:
        return {
            "answer": "",
            "flow_type": CLEIDE_AUDIT_INSIGHTS_CHAT_FLOW_TYPE,
            "deterministic": True,
            "error": "invalid_message",
            "message": f"Mensagem excede o limite de {message_limit} caracteres.",
        }

    loaded = load_audit_insights_bundle(session_obj, require_unlock=True)
    if not loaded.get("ok"):
        return {
            "answer": "",
            "flow_type": CLEIDE_AUDIT_INSIGHTS_CHAT_FLOW_TYPE,
            "deterministic": True,
            "error": loaded.get("error_code") or "insights_unavailable",
            "message": loaded.get("message") or "Consulta analítica indisponível.",
        }

    bundle = loaded["bundle"]
    batch_scope = insights_batch_scope(bundle)
    cached = get_cached_insights_chat_response(session_obj, request_id or "", batch_scope=batch_scope)
    if cached is not None:
        return {
            "answer": cached.get("answer") or "",
            "flow_type": cached.get("flow_type") or CLEIDE_AUDIT_INSIGHTS_CHAT_FLOW_TYPE,
            "deterministic": bool(cached.get("deterministic")),
            "cached": True,
        }

    focus = _normalize_visual_focus(visual_focus)
    conversation_focus = get_conversation_focus(session_obj, batch_scope)
    if conversation_focus is None:
        # Lote mudou ou não há foco — garante invalidação residual.
        existing = session_obj.get("cleide_audit_insights_conversation_focus")
        if isinstance(existing, dict) and str(existing.get("batch_scope") or "") != batch_scope:
            clear_conversation_focus(session_obj)

    intent = classify_intent(
        clean_message,
        visual_focus=focus,
        conversation_focus=conversation_focus,
    )
    history_slice = sanitize_chat_history(history, max_history=history_limit)

    # Documento explícito com múltiplas linhas: não gravar foco ambíguo.
    if intent in {INTENT_EXPLAIN_CALCULATION, INTENT_LOCATE_DOCUMENT}:
        target = resolve_document_target(bundle, clean_message)
        if target["kind"] == "duplicate":
            from app.cleide_audit_insights_query import format_duplicate_document_options

            answer = finalize_insights_answer(
                format_duplicate_document_options(bundle, target["rows"], str(target["reference"]))
            )
            result_payload = {
                "answer": answer,
                "flow_type": CLEIDE_AUDIT_INSIGHTS_CHAT_FLOW_TYPE,
                "deterministic": True,
                "intent": intent,
            }
            cache_insights_chat_response(
                session_obj,
                request_id or "",
                result_payload,
                batch_scope=batch_scope,
            )
            return result_payload

    deterministic_answer, context_rows, fully_deterministic = try_deterministic_response(
        bundle,
        intent,
        clean_message,
        visual_focus=focus,
        conversation_focus=conversation_focus,
    )
    if fully_deterministic and deterministic_answer:
        _update_focus_after_response(
            session_obj,
            batch_scope=batch_scope,
            intent=intent,
            message=clean_message,
            context_rows=context_rows,
            visual_focus=focus,
            conversation_focus=conversation_focus,
        )
        result_payload = {
            "answer": finalize_insights_answer(deterministic_answer),
            "flow_type": CLEIDE_AUDIT_INSIGHTS_CHAT_FLOW_TYPE,
            "deterministic": True,
            "intent": intent,
        }
        cache_insights_chat_response(
            session_obj,
            request_id or "",
            result_payload,
            batch_scope=batch_scope,
        )
        return result_payload

    if intent in GEMINI_ANALYTICAL_INTENTS:
        return _run_gemini_analytical_reply(
            bundle=bundle,
            intent=intent,
            clean_message=clean_message,
            history_slice=history_slice,
            focus=focus,
            session_obj=session_obj,
            request_id=request_id,
            batch_scope=batch_scope,
            processing_message=processing_message,
        )

    return {
        "answer": finalize_insights_answer(deterministic_answer or format_ambiguity_safe(bundle)),
        "flow_type": CLEIDE_AUDIT_INSIGHTS_CHAT_FLOW_TYPE,
        "deterministic": True,
        "intent": intent,
    }


def format_ambiguity_safe(bundle: dict) -> str:
    from app.cleide_audit_insights_query import format_ambiguity

    return format_ambiguity(bundle)
