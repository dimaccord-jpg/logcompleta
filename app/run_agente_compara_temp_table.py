"""
Extração técnica da tabela temporária da Agente Compara (Fase 1).

Pipeline pós-upload governado por Cleiton; separado do chat conversacional.
Sem regex de negócio, sem billing paralelo e sem persistência definitiva.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from flask import has_request_context, session

from app.agente_compara_doc_context import build_agente_compara_document_context_for_chat
from app.agente_compara_doc_service import (
    apply_temp_table_extraction_from_model_payload,
    agente_compara_temp_table_extraction_idempotency_key,
    get_agente_compara_doc_ids,
    mark_temp_table_processing,
    should_attempt_temp_table_extraction,
    split_temp_table_block_from_answer,
    TEMP_TABLE_STATUS_FAILED,
    TEMP_TABLE_VERSION_MARKER,
)
from app.agente_compara_prompt import (
    build_agente_compara_temp_table_fallback_prompt,
    build_agente_compara_temp_table_technical_prompt,
)
from app.run_cleiton_gemini_governance import cleiton_governed_generate_content
from app.services.agente_compara_config_service import get_active_calculation_bases_for_runtime

logger = logging.getLogger(__name__)

AGENTE_COMPARA_TEMP_TABLE_EXTRACTION_FLOW_TYPE = "agente_compara_temp_table_extraction"
TEMP_TABLE_EXTRACTION_IDEMPOTENCY_CACHE_KEY = "agente_compara_temp_table_extraction_cache"

DEFAULT_MODEL_FALLBACK = "gemini-2.5-flash-lite"
DEFAULT_EXTRACTION_TIMEOUT_MS = 60_000
AGENTE_COMPARA_TEMP_TABLE_TIMEOUT_MS_ENV = "AGENTE_COMPARA_TEMP_TABLE_TIMEOUT_MS"

READING_ALERT_PARSER_NO_JSON = (
    "A resposta técnica não continha JSON estruturado aproveitável."
)
READING_ALERT_PROVIDER_TIMEOUT = (
    "Gemini retornou timeout durante a extração técnica."
)
READING_ALERT_PARTIAL_EXTRACTION = (
    "A extração encontrou dados parciais e precisa de validação humana."
)


def _api_key_label() -> str:
    if os.getenv("GEMINI_API_KEY_1"):
        return "GEMINI_API_KEY_1"
    if os.getenv("GEMINI_API_KEY"):
        return "GEMINI_API_KEY"
    return "unknown"


def get_extraction_timeout_ms() -> int:
    """
    Timeout HTTP da extração técnica pós-upload.

    Prioridade: AGENTE_COMPARA_TEMP_TABLE_TIMEOUT_MS > DEFAULT_EXTRACTION_TIMEOUT_MS.
    Não herda GEMINI_HTTP_TIMEOUT_MS (reservado a chat/outros fluxos).
    """
    raw = (os.getenv(AGENTE_COMPARA_TEMP_TABLE_TIMEOUT_MS_ENV) or "").strip()
    if raw:
        try:
            return max(1_000, int(raw))
        except ValueError:
            pass
    return DEFAULT_EXTRACTION_TIMEOUT_MS


def _get_client():
    key = os.getenv("GEMINI_API_KEY_1") or os.getenv("GEMINI_API_KEY")
    if not key:
        return None
    try:
        from google import genai
        from google.genai import types as genai_types

        timeout_ms = get_extraction_timeout_ms()
        return genai.Client(api_key=key, http_options=genai_types.HttpOptions(timeout=timeout_ms))
    except Exception as exc:
        logger.error("Falha ao inicializar cliente Gemini para extração temp_table: %s", exc)
        return None


def _get_model_candidates() -> list[str]:
    candidates = [
        (os.getenv("GEMINI_MODEL_TEXT") or "").strip(),
        "gemini-2.5-flash",
        (os.getenv("AGENTE_COMPARA_TEMP_TABLE_MODEL_FALLBACK") or "").strip(),
        DEFAULT_MODEL_FALLBACK,
    ]
    seen: set[str] = set()
    out: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


def _normalize_source_doc_ids(source_doc_ids: list[str] | None) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in source_doc_ids or []:
        if not isinstance(item, str):
            continue
        ref = item.strip()
        if not ref or ref in seen:
            continue
        seen.add(ref)
        cleaned.append(ref)
    return cleaned


def _build_extraction_contents(
    *,
    document_context_block: str | None,
    document_file_parts: list | None,
    use_fallback_prompt: bool = False,
) -> str | list:
    if use_fallback_prompt:
        system_prompt = build_agente_compara_temp_table_fallback_prompt().strip()
    else:
        try:
            calculation_bases = get_active_calculation_bases_for_runtime()
        except Exception:
            logger.exception("Agente Compara temp_table extraction: falha ao carregar bases de cálculo ativas.")
            calculation_bases = []
        system_prompt = build_agente_compara_temp_table_technical_prompt(
            calculation_bases=calculation_bases,
        ).strip()
    parts = [system_prompt, "\n\n---\n\n"]
    doc_block = (document_context_block or "").strip()
    if doc_block:
        parts.append(doc_block)
        parts.append("\n\n---\n\n")
    parts.append("Responda exclusivamente com o JSON solicitado.")
    prompt_text = "".join(parts)
    file_parts = [part for part in (document_file_parts or []) if part is not None]
    if file_parts:
        return file_parts + [prompt_text]
    return prompt_text


def _strip_outer_markdown_code_fence(text: str) -> str:
    """Remove apenas envelope markdown externo; não interpreta dados de frete."""
    stripped = (text or "").strip()
    match = re.match(
        r"^```(?:json|JSON)?\s*\r?\n?(.*?)\r?\n?```\s*$",
        stripped,
        flags=re.DOTALL,
    )
    if match:
        return match.group(1).strip()
    return stripped


def _extract_first_balanced_json_object(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _try_parse_json_dict(raw: str) -> dict | None:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_extraction_response(text: str) -> dict | None:
    raw = (text or "").strip()
    if not raw:
        return None

    _, block_payload = split_temp_table_block_from_answer(raw)
    if isinstance(block_payload, dict):
        return block_payload

    parsed = _try_parse_json_dict(raw)
    if parsed is not None:
        return parsed

    fenced = _strip_outer_markdown_code_fence(raw)
    if fenced != raw:
        parsed = _try_parse_json_dict(fenced)
        if parsed is not None:
            return parsed

    balanced = _extract_first_balanced_json_object(raw)
    if balanced:
        parsed = _try_parse_json_dict(balanced)
        if parsed is not None:
            return parsed

    return None


def _failed_extraction_payload(*, reading_alerts: list[str] | str) -> dict:
    alerts = [reading_alerts] if isinstance(reading_alerts, str) else list(reading_alerts)
    return {
        "status": TEMP_TABLE_STATUS_FAILED,
        "reading_alerts": alerts,
    }


def _build_provider_timeout_alerts(
    *,
    models_attempted: list[str],
    timeout_ms: int,
) -> list[str]:
    alerts = [READING_ALERT_PROVIDER_TIMEOUT]
    if not models_attempted:
        alerts.append(f"Timeout efetivo: {timeout_ms} ms.")
        return alerts
    primary = models_attempted[0]
    fallback = models_attempted[-1]
    if len(models_attempted) > 1 and fallback != primary:
        detail = (
            f"Modelo: {primary}; fallback: {fallback}; timeout efetivo: {timeout_ms} ms."
        )
    else:
        detail = f"Modelo: {primary}; timeout efetivo: {timeout_ms} ms."
    alerts.append(detail)
    return alerts


def _is_provider_timeout(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    message = str(exc).upper()
    return "DEADLINE_EXCEEDED" in message or "504" in message


def _get_cached_extraction(session_obj, source_doc_ids: list[str]) -> dict | None:
    if not has_request_context():
        return None
    cache = session_obj.get(TEMP_TABLE_EXTRACTION_IDEMPOTENCY_CACHE_KEY)
    if not isinstance(cache, dict):
        return None
    key = agente_compara_temp_table_extraction_idempotency_key(source_doc_ids)
    payload = cache.get(key)
    return payload if isinstance(payload, dict) else None


def _cache_extraction_result(session_obj, source_doc_ids: list[str], record: dict) -> None:
    if not has_request_context() or not isinstance(record, dict):
        return
    cache = session_obj.get(TEMP_TABLE_EXTRACTION_IDEMPOTENCY_CACHE_KEY)
    if not isinstance(cache, dict):
        cache = {}
    key = agente_compara_temp_table_extraction_idempotency_key(source_doc_ids)
    cache[key] = {
        "temp_table_id": record.get("temp_table_id"),
        "status": record.get("status"),
        "version_marker": TEMP_TABLE_VERSION_MARKER,
    }
    session_obj[TEMP_TABLE_EXTRACTION_IDEMPOTENCY_CACHE_KEY] = cache
    session_obj.modified = True


def run_agente_compara_temp_table_extraction(
    source_doc_ids: list[str],
    *,
    session_obj=None,
    user_scope=None,
    franquia_scope=None,
) -> dict | None:
    """
    Executa extração técnica via Gemini para os documentos de origem informados.

    Falhas de provider ou parsing resultam em status failed sem propagar exceção ao caller.
    """
    sess = session_obj if session_obj is not None else session
    normalized = _normalize_source_doc_ids(source_doc_ids)
    if not normalized:
        return None

    cached = _get_cached_extraction(sess, normalized)
    if cached and cached.get("version_marker") == TEMP_TABLE_VERSION_MARKER:
        return cached

    doc_ctx = build_agente_compara_document_context_for_chat(sess)
    if not doc_ctx.get("has_documents"):
        return apply_temp_table_extraction_from_model_payload(
            _failed_extraction_payload(reading_alerts=READING_ALERT_PARSER_NO_JSON),
            source_doc_ids=normalized,
        )

    effective_timeout_ms = get_extraction_timeout_ms()
    logger.info(
        "Agente Compara temp_table extraction: timeout efetivo=%sms (nao herda GEMINI_HTTP_TIMEOUT_MS)",
        effective_timeout_ms,
    )

    client = _get_client()
    if not client:
        logger.warning(
            "Agente Compara temp_table extraction: nenhuma chave Gemini configurada."
        )
        record = apply_temp_table_extraction_from_model_payload(
            _failed_extraction_payload(reading_alerts=READING_ALERT_PARSER_NO_JSON),
            source_doc_ids=normalized,
        )
        _cache_extraction_result(sess, normalized, record)
        return record

    model_candidates = _get_model_candidates()
    last_error: Exception | None = None
    last_timeout = False
    timeout_models_attempted: list[str] = []
    for model_index, model in enumerate(model_candidates):
        contents = _build_extraction_contents(
            document_context_block=doc_ctx.get("context_block") or None,
            document_file_parts=doc_ctx.get("gemini_file_parts") or None,
            use_fallback_prompt=model_index > 0,
        )
        try:
            response = cleiton_governed_generate_content(
                client,
                model=model,
                contents=contents,
                agent="agente_compara",
                flow_type=AGENTE_COMPARA_TEMP_TABLE_EXTRACTION_FLOW_TYPE,
                api_key_label=_api_key_label(),
            )
            text = (getattr(response, "text", None) or "").strip()
            payload = _parse_extraction_response(text)
            if payload is None:
                record = apply_temp_table_extraction_from_model_payload(
                    _failed_extraction_payload(reading_alerts=READING_ALERT_PARSER_NO_JSON),
                    source_doc_ids=normalized,
                )
                _cache_extraction_result(sess, normalized, record)
                return record
            if franquia_scope is not None:
                payload["franquia_scope"] = franquia_scope
            if user_scope is not None:
                payload["user_scope"] = user_scope
            record = apply_temp_table_extraction_from_model_payload(
                payload,
                source_doc_ids=normalized,
            )
            _cache_extraction_result(sess, normalized, record)
            return record
        except Exception as exc:
            last_error = exc
            if _is_provider_timeout(exc):
                last_timeout = True
                if model not in timeout_models_attempted:
                    timeout_models_attempted.append(model)
            logger.warning(
                "Agente Compara temp_table extraction provider failure: model=%s timeout_ms=%s exc_type=%s message=%s",
                model,
                effective_timeout_ms,
                exc.__class__.__name__,
                exc,
            )

    if last_error:
        logger.exception(
            "Agente Compara temp_table extraction falhou após fallbacks: %s", last_error
        )
    if last_timeout:
        failure_alerts = _build_provider_timeout_alerts(
            models_attempted=timeout_models_attempted or model_candidates,
            timeout_ms=effective_timeout_ms,
        )
    else:
        failure_alerts = [READING_ALERT_PARSER_NO_JSON]
    record = apply_temp_table_extraction_from_model_payload(
        _failed_extraction_payload(reading_alerts=failure_alerts),
        source_doc_ids=normalized,
    )
    _cache_extraction_result(sess, normalized, record)
    return record


def trigger_temp_table_extraction_for_session(
    *,
    session_obj=None,
    user_scope=None,
    franquia_scope=None,
) -> dict | None:
    """
    Marca processing e dispara extração técnica para o conjunto atual de documentos.

    Idempotente por conjunto de source_documents + version_marker na sessão.
    """
    sess = session_obj if session_obj is not None else session
    source_doc_ids = get_agente_compara_doc_ids(sess)
    normalized = _normalize_source_doc_ids(source_doc_ids)
    if not normalized:
        return None
    if not should_attempt_temp_table_extraction(sess, normalized):
        return None

    mark_temp_table_processing(
        normalized,
        user_scope=user_scope,
        franquia_scope=franquia_scope,
    )

    try:
        return run_agente_compara_temp_table_extraction(
            normalized,
            session_obj=sess,
            user_scope=user_scope,
            franquia_scope=franquia_scope,
        )
    except Exception:
        logger.exception(
            "Agente Compara temp_table: falha inesperada na extração pós-upload; marcando failed."
        )
        try:
            return apply_temp_table_extraction_from_model_payload(
                _failed_extraction_payload(reading_alerts=READING_ALERT_PARSER_NO_JSON),
                source_doc_ids=normalized,
            )
        except Exception:
            logger.exception(
                "Agente Compara temp_table: falha ao marcar failed após erro inesperado."
            )
            return None
