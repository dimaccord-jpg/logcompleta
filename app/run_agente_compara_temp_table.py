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
    alerta_usuario_extracao,
    get_agente_compara_doc_ids,
    mark_temp_table_processing,
    should_attempt_temp_table_extraction,
    split_temp_table_block_from_answer,
    ERROR_EXTRACAO_CONSUMO_INCERTO,
    ERROR_EXTRACAO_CREDITOS_INSUFICIENTES,
    ERROR_EXTRACAO_JSON_INVALIDO,
    ERROR_EXTRACAO_OPERACAO_INDISPONIVEL,
    ERROR_EXTRACAO_PROVEDOR_INDISPONIVEL,
    ERROR_EXTRACAO_RESPOSTA_TRUNCADA,
    ERROR_EXTRACAO_TETO_TECNICO,
    TEMP_TABLE_CREDIT_UNCONFIRMED,
    TEMP_TABLE_STATUS_FAILED,
    TEMP_TABLE_VERSION_MARKER,
    save_temp_table_record,
    FIELD_COMPARISON_ID,
    FIELD_TABLE_ID,
    FIELD_SLOT_NUMBER,
)
from app.agente_compara_comparison_state import (
    get_active_table,
    get_comparison_state,
    resolve_table_identity,
)
from app.agente_compara_prompt import (
    build_agente_compara_temp_table_fallback_prompt,
    build_agente_compara_temp_table_technical_prompt,
)
from app.services.agente_compara_extraction_billing import (
    ExtracaoConsumoIncerto,
    ExtracaoRecusada,
    abater_entrega_confirmada,
    cleiton_governed_generate_content,
    digest_da_entrega,
    entrega_faturavel,
)
from app.services.agente_compara_extraction_budget import (
    EXTRACAO_TETO_TECNICO_TOKENS,
    MIN_JSON_OUTPUT_TOKENS,
    capacidade_do_modelo,
    consumo_tecnico,
    montar_orcamento_tentativa,
)
from app.services.cleiton_billable_ai_call import (
    BillableAiUncertainError,
    gemini_http_options,
    usuario_operacional_da_chamada,
)
from app.services.agente_compara_config_service import get_active_calculation_bases_for_runtime
from app.cleiton_doc_escopo import (
    operational_cache_payload_is_current,
    stamp_operational_cache_payload,
)

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

def _is_eligible_free_technical_retry(
    record: dict | None,
    *,
    comparison_id: str | None,
    slot_number: int | None,
) -> bool:
    if not isinstance(record, dict):
        return False
    if not record.get("retry_of"):
        return False
    if not bool(record.get("is_technical_retry")):
        return False
    if not bool(record.get("retryable")):
        return False
    if (record.get("retry_failure_origin") or "").strip() != "platform":
        return False
    if (record.get(FIELD_COMPARISON_ID) or "").strip() != (comparison_id or "").strip():
        return False
    try:
        record_slot = int(record.get(FIELD_SLOT_NUMBER)) if record.get(FIELD_SLOT_NUMBER) is not None else None
    except (TypeError, ValueError):
        record_slot = None
    return record_slot == slot_number


def _register_nonbillable_processing_event(
    record: dict | None,
    *,
    source_doc_ids: list[str],
    comparison_id: str | None,
    slot_number: int | None,
) -> None:
    if not isinstance(record, dict):
        return
    status = (record.get("status") or "").strip().lower()
    is_platform_failure = (
        status == TEMP_TABLE_STATUS_FAILED
        and (record.get("failure_origin") or "").strip() == "platform"
        and bool(record.get("retryable"))
    )
    is_free_retry = _is_eligible_free_technical_retry(
        record,
        comparison_id=comparison_id,
        slot_number=slot_number,
    )
    if not is_platform_failure and not is_free_retry:
        return
    from app.run_cleiton_processing_governance import cleiton_register_processing_event

    error_summary = None
    if is_platform_failure:
        error_summary = (record.get("failure_code") or "platform_processing_failed")[:2000]
    elif is_free_retry:
        error_summary = f"free_technical_retry retry_of={record.get('retry_of') or ''}"[:2000]
    cleiton_register_processing_event(
        agent="agente_compara",
        flow_type=AGENTE_COMPARA_TEMP_TABLE_EXTRACTION_FLOW_TYPE,
        processing_type="non_llm",
        rows_processed=len(source_doc_ids or []),
        processing_time_ms=0,
        status="failure" if is_platform_failure else "success",
        error_summary=error_summary,
        execution_id=str(record.get("temp_table_id") or "") or None,
        apply_operational_motor=False,
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

        timeout_ms = get_extraction_timeout_ms()
        return genai.Client(api_key=key, http_options=gemini_http_options(timeout_ms=timeout_ms))
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


def _nullable_json(*kinds: str) -> dict[str, Any]:
    return {"anyOf": [{"type": kind} for kind in kinds] + [{"type": "null"}]}


def _open_object(properties: dict[str, Any] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "additionalProperties": True}
    if properties:
        schema["properties"] = properties
    return schema


def _string_list() -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}}


def _loose_list() -> dict[str, Any]:
    """Listas legadas cujo item o backend conserva sem schema fechado."""
    return {
        "type": "array",
        "items": {
            "anyOf": [
                {"type": "string"},
                {"type": "number"},
                {"type": "boolean"},
                {"type": "null"},
                {"type": "object", "additionalProperties": True},
            ]
        },
    }


def temp_table_extraction_response_json_schema() -> dict[str, Any]:
    """JSON Schema do payload técnico aceito pela temp_table.

    Transporte via response_json_schema, com chaves camelCase. O caminho
    response_schema do SDK projeta additional_properties e a API rejeita.
    Nenhum campo é obrigatório: a extração continua partial-first.
    """
    nullable_text = _nullable_json("string")
    nullable_scalar = _nullable_json("string", "number")
    context = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "route_label": nullable_text,
            "origin": nullable_text,
            "destination": nullable_text,
            "customer": nullable_text,
            "supplier": nullable_text,
            "valid_from": nullable_text,
            "valid_to": nullable_text,
            "delivery_deadline": nullable_text,
        },
    }
    freight_table = _open_object(
        {
            "table_title": nullable_text,
            "table_type": nullable_text,
            "context": context,
            "columns": _string_list(),
            "rows": {
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
            },
            "column_meta": {"type": "object", "additionalProperties": True},
            "notes": nullable_text,
            "evidence_ref": nullable_text,
            "confidence": nullable_text,
            "supplier": nullable_text,
            "provider": nullable_text,
            "carrier": nullable_text,
            "carrier_name": nullable_text,
        }
    )
    freight_route = _open_object(
        {
            "origin": nullable_text,
            "destination": nullable_text,
            "freight_type": nullable_text,
            "weight_30": nullable_scalar,
            "weight_50": nullable_scalar,
            "weight_70": nullable_scalar,
            "weight_100": nullable_scalar,
            "boarding_fee": nullable_scalar,
            "freight_value_pct": nullable_scalar,
            "freight_weight_kg": nullable_scalar,
            "excess_threshold_kg": _nullable_json("number", "integer"),
            "pedagio": nullable_scalar,
            "column_labels": {"type": "object", "additionalProperties": True},
            "notes": nullable_text,
            "evidence_ref": nullable_text,
            "confidence": nullable_text,
        }
    )
    freight_value = _open_object(
        {
            "label": nullable_text,
            "value": nullable_scalar,
            "unit": nullable_text,
            "notes": nullable_text,
        }
    )
    accessorial_fee = _open_object(
        {
            "name": nullable_text,
            "value": nullable_scalar,
            "unit": nullable_text,
            "calculation_basis": nullable_text,
            "raw_calculation_basis": nullable_text,
            "notes": nullable_text,
            "scope": nullable_text,
            "calculation_base_id": nullable_text,
            "calculation_base_label": nullable_text,
            "related_to": nullable_text,
            "component_group": nullable_text,
            "modifier_type": nullable_text,
            "minimum_amount": nullable_scalar,
            "maximum_amount": nullable_scalar,
            "calculation_type": nullable_text,
            "canonical_component": nullable_text,
            "evidence_ref": nullable_text,
        }
    )
    weight_range = _open_object(
        {
            "label": nullable_text,
            "min_weight": nullable_scalar,
            "max_weight": nullable_scalar,
            "unit": nullable_text,
            "notes": nullable_text,
        }
    )
    properties = {
        "status": {
            "type": "string",
            "enum": [
                "needs_review",
                "failed",
                "awaiting_validation",
                "validated",
            ],
        },
        "detected_carrier": nullable_text,
        "freight_tables": {"type": "array", "items": freight_table},
        "freight_routes": {"type": "array", "items": freight_route},
        "freight_values": {"type": "array", "items": freight_value},
        "accessorial_fees": {"type": "array", "items": accessorial_fee},
        "weight_ranges": {"type": "array", "items": weight_range},
        "reading_alerts": _string_list(),
        "evidence_refs": _string_list(),
        "origins": _loose_list(),
        "destinations": _loose_list(),
        "routes": _loose_list(),
        "charge_type_detected": nullable_text,
        "extracted_items": _loose_list(),
        "uncertain_fields": _loose_list(),
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "propertyOrdering": list(properties),
        "properties": properties,
    }


def build_temp_table_extraction_generation_config():
    """Structured Output JSON. Não usa response_schema tipado."""
    from google.genai import types

    return types.GenerateContentConfig(
        response_mime_type="application/json",
        response_json_schema=temp_table_extraction_response_json_schema(),
    )


def _safe_finish_reason(response: Any) -> str | None:
    """Nome do finish_reason, sem texto da resposta."""
    candidates = getattr(response, "candidates", None)
    if not isinstance(candidates, (list, tuple)) or not candidates:
        return None
    reason = getattr(candidates[0], "finish_reason", None)
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
    return token


def _failed_extraction_payload(*, reading_alerts: list[str] | str, error_code: str | None = None) -> dict:
    alerts = [reading_alerts] if isinstance(reading_alerts, str) else list(reading_alerts)
    payload = {
        "status": TEMP_TABLE_STATUS_FAILED,
        "reading_alerts": alerts,
    }
    if error_code:
        payload["error_code"] = error_code
    return payload


def _teto_restante(consumido: int) -> int:
    return EXTRACAO_TETO_TECNICO_TOKENS - max(0, int(consumido))


def _codigo_da_recusa(codigo: str) -> str:
    if codigo == "creditos":
        return ERROR_EXTRACAO_CREDITOS_INSUFICIENTES
    if codigo == "teto":
        return ERROR_EXTRACAO_TETO_TECNICO
    return ERROR_EXTRACAO_OPERACAO_INDISPONIVEL


def _falha_final_da_extracao(
    causa: str | None,
    *,
    last_timeout: bool,
    timeout_alerts: list[str],
) -> tuple[str | None, list[str]]:
    """Escolhe o código visto pelo usuário. Timeout confirmado preserva o alerta atual."""
    terminais = {
        ERROR_EXTRACAO_TETO_TECNICO,
        ERROR_EXTRACAO_CREDITOS_INSUFICIENTES,
        ERROR_EXTRACAO_CONSUMO_INCERTO,
        ERROR_EXTRACAO_OPERACAO_INDISPONIVEL,
        ERROR_EXTRACAO_PROVEDOR_INDISPONIVEL,
        ERROR_EXTRACAO_RESPOSTA_TRUNCADA,
    }
    if causa in terminais:
        mensagem = alerta_usuario_extracao(causa)
        return causa, [mensagem or READING_ALERT_PARSER_NO_JSON]
    if last_timeout:
        return None, timeout_alerts
    if causa == ERROR_EXTRACAO_JSON_INVALIDO:
        return causa, [READING_ALERT_PARSER_NO_JSON]
    return None, [READING_ALERT_PARSER_NO_JSON]


def _is_provider_service_error(exc: Exception) -> bool:
    """Erro de serviço do Gemini. Timeout continua no tratamento já existente."""
    if _is_provider_timeout(exc):
        return False
    for nome in ("code", "status_code"):
        valor = getattr(exc, nome, None)
        if valor in {429, 500, 502, 503}:
            return True
    status = getattr(exc, "status", None)
    if isinstance(status, str) and status.strip().upper() in {
        "UNAVAILABLE",
        "INTERNAL",
        "RESOURCE_EXHAUSTED",
        "UNKNOWN",
    }:
        return True
    message = str(exc).upper()
    marcadores = (
        "UNAVAILABLE",
        "SERVICE UNAVAILABLE",
        "INTERNAL ERROR",
        "RESOURCE_EXHAUSTED",
        "HTTP 500",
        "HTTP 502",
        "HTTP 503",
        "HTTP 429",
    )
    return any(marcador in message for marcador in marcadores)


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


def _superficie_da_resposta(response: Any) -> tuple[Any, int | None]:
    """Separa a resposta do provedor do evento de consumo observado."""
    evento_id = getattr(response, "evento_id", None)
    if isinstance(evento_id, bool) or not isinstance(evento_id, int):
        evento_id = None
    inner = getattr(response, "response", None)
    if inner is not None and getattr(response, "text", None) is None:
        return inner, evento_id
    return response, evento_id


def _is_provider_timeout(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    message = str(exc).upper()
    return "DEADLINE_EXCEEDED" in message or "504" in message


def _get_cached_extraction(
    session_obj,
    source_doc_ids: list[str],
    *,
    comparison_id: str | None = None,
    table_id: str | None = None,
) -> dict | None:
    if not has_request_context():
        return None
    cache = session_obj.get(TEMP_TABLE_EXTRACTION_IDEMPOTENCY_CACHE_KEY)
    if not isinstance(cache, dict):
        return None
    key = agente_compara_temp_table_extraction_idempotency_key(
        source_doc_ids,
        comparison_id=comparison_id,
        table_id=table_id,
    )
    payload = cache.get(key)
    if not isinstance(payload, dict):
        return None
    if not operational_cache_payload_is_current(payload):
        return None
    return payload


def _cache_extraction_result(
    session_obj,
    source_doc_ids: list[str],
    record: dict,
    *,
    comparison_id: str | None = None,
    table_id: str | None = None,
) -> None:
    if not has_request_context() or not isinstance(record, dict):
        return
    cache = session_obj.get(TEMP_TABLE_EXTRACTION_IDEMPOTENCY_CACHE_KEY)
    if not isinstance(cache, dict):
        cache = {}
    key = agente_compara_temp_table_extraction_idempotency_key(
        source_doc_ids,
        comparison_id=comparison_id,
        table_id=table_id,
    )
    cache[key] = stamp_operational_cache_payload({
        "temp_table_id": record.get("temp_table_id"),
        "status": record.get("status"),
        "version_marker": TEMP_TABLE_VERSION_MARKER,
        "comparison_id": comparison_id,
        "table_id": table_id,
    })
    session_obj[TEMP_TABLE_EXTRACTION_IDEMPOTENCY_CACHE_KEY] = cache
    session_obj.modified = True


def run_agente_compara_temp_table_extraction(
    source_doc_ids: list[str],
    *,
    session_obj=None,
    user_scope=None,
    franquia_scope=None,
    comparison_id: str | None = None,
    table_id: str | None = None,
    slot_number: int | None = None,
) -> dict | None:
    """
    Executa extração técnica via Gemini para os documentos de origem informados.

    Falhas de provider ou parsing resultam em status failed sem propagar exceção ao caller.
    """
    sess = session_obj if session_obj is not None else session
    normalized = _normalize_source_doc_ids(source_doc_ids)
    if not normalized:
        return None

    cached = _get_cached_extraction(
        sess,
        normalized,
        comparison_id=comparison_id,
        table_id=table_id,
    )
    if cached and cached.get("version_marker") == TEMP_TABLE_VERSION_MARKER:
        return cached

    doc_ctx = build_agente_compara_document_context_for_chat(sess, table_id=table_id)
    if not doc_ctx.get("has_documents"):
        record = apply_temp_table_extraction_from_model_payload(
            _failed_extraction_payload(reading_alerts=READING_ALERT_PARSER_NO_JSON),
            source_doc_ids=normalized,
            comparison_id=comparison_id,
            table_id=table_id,
            slot_number=slot_number,
        )
        _register_nonbillable_processing_event(
            record,
            source_doc_ids=normalized,
            comparison_id=comparison_id,
            slot_number=slot_number,
        )
        return record

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
            comparison_id=comparison_id,
            table_id=table_id,
            slot_number=slot_number,
        )
        _register_nonbillable_processing_event(
            record,
            source_doc_ids=normalized,
            comparison_id=comparison_id,
            slot_number=slot_number,
        )
        _cache_extraction_result(
            sess,
            normalized,
            record,
            comparison_id=comparison_id,
            table_id=table_id,
        )
        return record

    model_candidates = _get_model_candidates()
    generation_config = build_temp_table_extraction_generation_config()
    last_error: Exception | None = None
    last_timeout = False
    timeout_models_attempted: list[str] = []
    consumido = 0
    causa_falha: str | None = None
    digest_entrega = digest_da_entrega(
        normalized,
        comparison_id=comparison_id,
        table_id=table_id,
    )

    def _devolver(record: dict | None, *, preservar_entrega_confirmada: bool = False) -> dict | None:
        try:
            _register_nonbillable_processing_event(
                record,
                source_doc_ids=normalized,
                comparison_id=comparison_id,
                slot_number=slot_number,
            )
        except Exception as exc:
            if not preservar_entrega_confirmada:
                raise
            logger.warning(
                "Agente Compara extracao observabilidade falhou; tabela util preservada tipo=%s",
                exc.__class__.__name__,
            )
        try:
            _cache_extraction_result(
                sess,
                normalized,
                record,
                comparison_id=comparison_id,
                table_id=table_id,
            )
        except Exception as exc:
            if not preservar_entrega_confirmada:
                raise
            logger.warning(
                "Agente Compara extracao cache falhou; tabela util preservada tipo=%s",
                exc.__class__.__name__,
            )
        return record

    def _avisar_consumo_incerto(record: dict) -> None:
        """Pendência financeira na tabela útil. Não altera status e não cria reserva."""
        mensagem = alerta_usuario_extracao(ERROR_EXTRACAO_CONSUMO_INCERTO)
        record["error_code"] = ERROR_EXTRACAO_CONSUMO_INCERTO
        record["failure_code"] = ERROR_EXTRACAO_CONSUMO_INCERTO
        record["safe_message"] = mensagem
        record["credit_disposition"] = TEMP_TABLE_CREDIT_UNCONFIRMED
        record["retryable"] = False
        record.pop("upgrade_cta", None)

    def _persistir_aviso_consumo_incerto(record: dict) -> None:
        try:
            save_temp_table_record(record, table_id=record.get(FIELD_TABLE_ID))
        except Exception as exc:
            logger.warning(
                "Agente Compara extracao aviso de consumo incerto nao persistido tipo=%s",
                exc.__class__.__name__,
            )

    def _cobrar_entrega(evento_id: int | None, record: dict) -> dict:
        confirmada = False
        if evento_id:
            try:
                desfecho = abater_entrega_confirmada(int(evento_id), digest_entrega)
            except (BillableAiUncertainError, ExtracaoConsumoIncerto):
                logger.info(
                    "Agente Compara extracao consumo incerto na liquidacao; debito nao confirmado"
                )
                if isinstance(record, dict):
                    _avisar_consumo_incerto(record)
                    _persistir_aviso_consumo_incerto(record)
                return _devolver(record, preservar_entrega_confirmada=True)
            else:
                confirmada = desfecho in {"debitada", "ja_faturada"}
        return _devolver(record, preservar_entrega_confirmada=confirmada)

    def _somar_consumo(origem: Any) -> bool:
        nonlocal consumido
        gasto = consumo_tecnico(origem)
        if gasto is None:
            logger.info(
                "Agente Compara extracao consumo tecnico indisponivel model=%s acumulado=%s",
                model,
                consumido,
            )
            return False
        consumido += gasto
        logger.info(
            "Agente Compara extracao consumo tecnico model=%s tokens=%s acumulado=%s teto_tecnico=%s",
            model,
            gasto,
            consumido,
            EXTRACAO_TETO_TECNICO_TOKENS,
        )
        return True

    for model_index, model in enumerate(model_candidates):
        usuario = usuario_operacional_da_chamada()
        orcamento = montar_orcamento_tentativa(
            model=model,
            consumido=consumido,
        )
        restante = _teto_restante(consumido)
        capacidade = capacidade_do_modelo(model)
        logger.info(
            "Agente Compara extracao orcamento model=%s autorizado=%s teto_tecnico=%s "
            "capacidade_modelo=%s consumido=%s",
            model,
            orcamento.autorizado,
            orcamento.teto_tecnico,
            orcamento.capacidade_modelo,
            consumido,
        )
        if restante < MIN_JSON_OUTPUT_TOKENS or orcamento.autorizado < 1:
            causa_falha = ERROR_EXTRACAO_TETO_TECNICO
            logger.info(
                "Agente Compara extracao tentativa nao iniciada model=%s autorizado=%s causa=%s",
                model,
                orcamento.autorizado,
                causa_falha,
            )
            break
        contents = _build_extraction_contents(
            document_context_block=doc_ctx.get("context_block") or None,
            document_file_parts=doc_ctx.get("gemini_file_parts") or None,
            use_fallback_prompt=model_index > 0,
        )
        contabilizada = False
        evento_id: int | None = None
        resposta_recebida = False
        try:
            response = cleiton_governed_generate_content(
                client,
                model=model,
                contents=contents,
                config=generation_config,
                agent="agente_compara",
                flow_type=AGENTE_COMPARA_TEMP_TABLE_EXTRACTION_FLOW_TYPE,
                api_key_label=_api_key_label(),
                usuario=usuario,
                output_token_cap=capacidade.max_output_tokens,
                thinking_token_cap=capacidade.max_thinking_tokens,
                operation_token_budget=orcamento.autorizado,
            )
            superficie, evento_id = _superficie_da_resposta(response)
            contabilizada = _somar_consumo(superficie)
            resposta_recebida = True
            raw_text = getattr(superficie, "text", None) or ""
            if not isinstance(raw_text, str):
                raw_text = ""
            text = raw_text.strip()
            finish_reason = _safe_finish_reason(superficie)
            payload = _parse_extraction_response(text)
            if finish_reason == "MAX_TOKENS" or payload is None:
                if payload is None:
                    fallback_following = model_index + 1 < len(model_candidates)
                    logger.warning(
                        "Agente Compara temp_table extraction parser_no_json: "
                        "model=%s model_index=%s motivo=parser_no_json "
                        "response_text_length=%s finish_reason=%s fallback_following=%s",
                        model,
                        model_index,
                        len(raw_text),
                        finish_reason or "unavailable",
                        fallback_following,
                    )
                if not contabilizada:
                    logger.info(
                        "Agente Compara extracao consumo indeterminado model=%s; fallback bloqueado",
                        model,
                    )
                    break
                causa_falha = (
                    ERROR_EXTRACAO_RESPOSTA_TRUNCADA
                    if finish_reason == "MAX_TOKENS"
                    else ERROR_EXTRACAO_JSON_INVALIDO
                )
                continue
            if franquia_scope is not None:
                payload["franquia_scope"] = franquia_scope
            if user_scope is not None:
                payload["user_scope"] = user_scope
            record = apply_temp_table_extraction_from_model_payload(
                payload,
                source_doc_ids=normalized,
                comparison_id=comparison_id,
                table_id=table_id,
                slot_number=slot_number,
            )
            if not isinstance(record, dict):
                continue
            if not entrega_faturavel(record):
                return _devolver(record)
            return _cobrar_entrega(evento_id, record)
        except ExtracaoRecusada as exc:
            causa_falha = _codigo_da_recusa(exc.codigo)
            logger.info(
                "Agente Compara extracao tentativa nao iniciada model=%s autorizado=%s causa=%s",
                model,
                orcamento.autorizado,
                causa_falha,
            )
            break
        except (BillableAiUncertainError, ExtracaoConsumoIncerto) as exc:
            last_error = exc
            causa_falha = ERROR_EXTRACAO_CONSUMO_INCERTO
            break
        except Exception as exc:
            last_error = exc
            if not contabilizada:
                contabilizada = _somar_consumo(exc)
            if _is_provider_timeout(exc):
                last_timeout = True
                if model not in timeout_models_attempted:
                    timeout_models_attempted.append(model)
            elif _is_provider_service_error(exc):
                causa_falha = ERROR_EXTRACAO_PROVEDOR_INDISPONIVEL
            logger.warning(
                "Agente Compara temp_table extraction %s: model=%s timeout_ms=%s exc_type=%s message=%s",
                "persistencia falhou" if resposta_recebida else "provider failure",
                model,
                effective_timeout_ms,
                exc.__class__.__name__,
                exc,
            )
            if not contabilizada:
                logger.info(
                    "Agente Compara extracao consumo indeterminado model=%s; fallback bloqueado",
                    model,
                )
                break

    if last_error:
        logger.exception(
            "Agente Compara temp_table extraction falhou após fallbacks: %s", last_error
        )
    causa_final, failure_alerts = _falha_final_da_extracao(
        causa_falha,
        last_timeout=last_timeout,
        timeout_alerts=_build_provider_timeout_alerts(
            models_attempted=timeout_models_attempted or model_candidates,
            timeout_ms=effective_timeout_ms,
        ),
    )
    record = apply_temp_table_extraction_from_model_payload(
        _failed_extraction_payload(reading_alerts=failure_alerts, error_code=causa_final),
        source_doc_ids=normalized,
        comparison_id=comparison_id,
        table_id=table_id,
        slot_number=slot_number,
    )
    return _devolver(record)


def trigger_temp_table_extraction_for_session(
    *,
    session_obj=None,
    user_scope=None,
    franquia_scope=None,
    comparison_id: str | None = None,
    table_id: str | None = None,
    slot: int | None = None,
) -> dict | None:
    """
    Marca processing e dispara extração técnica para o conjunto atual de documentos.

    Idempotente por conjunto de source_documents + version_marker na sessão.
    """
    sess = session_obj if session_obj is not None else session
    cmp_state, table_entry = resolve_table_identity(
        comparison_id=comparison_id,
        table_id=table_id,
        slot=slot,
        auto_create=True,
    )
    resolved_table_id = table_entry["table_id"]
    resolved_comparison_id = cmp_state.get("comparison_id")
    resolved_slot = int(table_entry.get("slot_number") or 1)
    source_doc_ids = get_agente_compara_doc_ids(sess, table_id=resolved_table_id)
    normalized = _normalize_source_doc_ids(source_doc_ids)
    if not normalized:
        return None
    if not should_attempt_temp_table_extraction(sess, normalized, table_id=resolved_table_id):
        return None

    mark_temp_table_processing(
        normalized,
        user_scope=user_scope,
        franquia_scope=franquia_scope,
        comparison_id=resolved_comparison_id,
        table_id=resolved_table_id,
        slot_number=resolved_slot,
    )

    try:
        return run_agente_compara_temp_table_extraction(
            normalized,
            session_obj=sess,
            user_scope=user_scope,
            franquia_scope=franquia_scope,
            comparison_id=resolved_comparison_id,
            table_id=resolved_table_id,
            slot_number=resolved_slot,
        )
    except Exception:
        logger.exception(
            "Agente Compara temp_table: falha inesperada na extração pós-upload; marcando failed."
        )
        try:
            return apply_temp_table_extraction_from_model_payload(
                _failed_extraction_payload(reading_alerts=READING_ALERT_PARSER_NO_JSON),
                source_doc_ids=normalized,
                comparison_id=resolved_comparison_id,
                table_id=resolved_table_id,
                slot_number=resolved_slot,
            )
        except Exception:
            logger.exception(
                "Agente Compara temp_table: falha ao marcar failed após erro inesperado."
            )
            return None
