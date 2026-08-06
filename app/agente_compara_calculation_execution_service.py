"""
Serviço de execução do cálculo comparativo do AgenteCompara (Etapa 5).

Responsável por fingerprint, idempotência, estados, persistência temporária,
billing idempotente e chamada ao orquestrador multitabela aprovado.

Não contém matemática de frete. Não importa Cleide.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from flask import has_request_context, session
from flask_login import current_user

from app.agente_compara_comparison_calculation_service import (
    compact_comparison_result_for_storage,
    MULTI_TABLE_CALCULATION_SCHEMA_VERSION,
    AgenteComparaMultiTableCalculationError,
    InvalidMultiTableCalculationContextError,
    MultiTableCalculationInvariantError,
    UnexpectedMultiTableCalculationError,
    build_multi_table_calculation_context,
    calculate_comparison_in_memory,
    hydrate_memory_item,
    validate_comparison_result_serializable,
)
from app.agente_compara_calculation_lock import (
    AgenteComparaCalculationLockError,
    acquire_comparison_calculation_lock,
)
from app.agente_compara_calculation_result_storage import (
    AgenteComparaCalculationResultStorageError,
    ERROR_RESULT_CORRUPT,
    ERROR_RESULT_MISSING,
    delete_comparison_calculation_result,
    load_comparison_calculation_result,
    load_comparison_calculation_memory_payload,
    save_comparison_calculation_memory_payload,
    save_comparison_calculation_result,
    delete_comparison_calculation_memories,
    ERROR_MEMORY_MISSING,
    ERROR_MEMORY_TOO_LARGE,
    ERROR_RESULT_TOO_LARGE,
)
from app.agente_compara_comparison_state import (
    COMPARISON_STATUS_CALCULATION_FAILED,
    COMPARISON_STATUS_CALCULATION_READY,
    COMPARISON_STATUS_CALCULATION_RUNNING,
    COMPARISON_STATUS_CONFIGURATION_READY,
    STEP_CALCULATION_FAILED,
    STEP_CALCULATION_READY,
    STEP_CALCULATION_RUNNING,
    STEP_CONFIGURATION_READY,
    AgenteComparaComparisonError,
    ERROR_COMPARISON_NOT_FOUND,
    ERROR_COMPARISON_SCOPE_MISMATCH,
    ERROR_COMPARISON_STEP_INVALID,
    get_comparison_state,
    get_comparison_tax_config,
    get_table_by_slot,
    persist_comparison_state,
    public_comparison_calculation_summary,
)
from app.extensions import db
from app.funnel_event_service import (
    FUNNEL_EVENT_FREIGHT_CALCULATED,
    FUNNEL_SOURCE_AGENTE_COMPARA,
    record_funnel_event,
)
from app.models import User, utcnow_naive

logger = logging.getLogger(__name__)

CALCULATION_EXECUTION_SCHEMA_VERSION = 1

# Semântica do motor/completeza/statusos que afeta o significado do resultado.
# Incrementar quando a lógica de cálculo ou o contrato de completeza mudar
# de forma incompatível com resultados anteriores (invalida replay via fingerprint).
# Não confundir com CALCULATION_EXECUTION_SCHEMA_VERSION, MULTI_TABLE_CALCULATION_SCHEMA_VERSION
# nem RESULT_STORAGE_SCHEMA_VERSION.
AGENTE_COMPARA_CALCULATION_ALGORITHM_VERSION = 2

BILLING_STATUS_NOT_STARTED = "not_started"
BILLING_STATUS_PENDING = "pending"
BILLING_STATUS_APPLIED = "applied"
BILLING_STATUS_FAILED = "failed"

ERROR_EXECUTION_ID_REQUIRED = "agente_compara_calculation_execution_id_required"
ERROR_EXECUTION_ID_INVALID = "agente_compara_calculation_execution_id_invalid"
ERROR_EXECUTION_CONFLICT = "agente_compara_calculation_execution_conflict"
ERROR_EXECUTION_IN_PROGRESS = "agente_compara_calculation_execution_in_progress"
ERROR_CALCULATION_INPUT_CHANGED = "calculation_input_changed"
ERROR_CALCULATION_FAILED = "agente_compara_calculation_failed"
ERROR_CALCULATION_NOT_READY = "agente_compara_calculation_not_ready"
ERROR_RESULT_MISSING = "calculation_result_missing"
ERROR_RESULT_CORRUPT_PUBLIC = "calculation_result_corrupt"
ERROR_BILLING_PENDING = "agente_compara_calculation_billing_pending"
ERROR_BILLING_FAILED = "agente_compara_calculation_billing_failed"

FLOW_TYPE_COMPARISON_CALCULATION = "agente_compara_comparison_calculation"

_FORBIDDEN_PUBLIC_RESULT_FIELDS = frozenset(
    {
        "valor_frete",
        "charged_freight",
        "expected_freight",
        "freight_charged",
        "difference",
        "divergence",
        "overcharged",
        "undercharged",
        "winner",
        "winning_carrier",
        "cheapest_carrier",
        "savings",
        "economy",
        "ranking",
        "recommendation",
    }
)


class AgenteComparaCalculationExecutionError(Exception):
    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        http_status: int = 400,
        error_stage: str | None = None,
        retryable: bool = False,
        artifact_type: str | None = None,
        failed_table_name: str | None = None,
        failed_table_id: str | None = None,
        failed_slot: int | None = None,
        failure_origin: str | None = None,
        failure_code: str | None = None,
        credit_disposition: str | None = None,
        retry_of: str | None = None,
        is_free_retry: bool = False,
        safe_message: str | None = None,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.http_status = http_status
        self.error_stage = error_stage
        self.retryable = bool(retryable)
        self.artifact_type = artifact_type
        self.failed_table_name = failed_table_name
        self.failed_table_id = failed_table_id
        self.failed_slot = failed_slot
        self.failure_origin = failure_origin
        self.failure_code = failure_code
        self.credit_disposition = credit_disposition
        self.retry_of = retry_of
        self.is_free_retry = bool(is_free_retry)
        self.safe_message = safe_message or message


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _build_calculation_funnel_idempotency_key(*, user_id: int, comparison_id: str, execution_id: str) -> str:
    payload = {
        "source": FUNNEL_SOURCE_AGENTE_COMPARA,
        "event_name": FUNNEL_EVENT_FREIGHT_CALCULATED,
        "user_id": int(user_id),
        "comparison_id": (comparison_id or "").strip(),
        "execution_id": (execution_id or "").strip(),
    }
    return f"funnel:ac:calculation:{_sha256_hex(_canonical_json(payload))}"


def _record_calculation_funnel_event(
    *,
    comparison_id: str,
    execution_id: str,
    idempotent_replay: bool,
    calc: dict,
) -> tuple[dict | None, bool]:
    payload = {"is_first_audit": False}
    if idempotent_replay:
        return payload, False
    if calc.get("status") != STEP_CALCULATION_READY:
        return payload, False
    if calc.get("billing_status") != BILLING_STATUS_APPLIED:
        return payload, False
    if calc.get("stale"):
        return payload, False
    user_id = getattr(current_user, "id", None)
    conta_id = getattr(current_user, "conta_id", None)
    franquia_id = getattr(current_user, "franquia_id", None)
    if user_id is None or conta_id is None or franquia_id is None:
        return payload, False
    started_funnel_tx = False
    try:
        orm_session = db.session()
        if not orm_session.in_transaction():
            orm_session.begin()
            started_funnel_tx = True
        with db.session.begin_nested():
            user = (
                db.session.query(User)
                .filter(User.id == int(user_id))
                .with_for_update()
                .one()
            )
            funnel_result = record_funnel_event(
                event_name=FUNNEL_EVENT_FREIGHT_CALCULATED,
                source=FUNNEL_SOURCE_AGENTE_COMPARA,
                user_id=int(user_id),
                conta_id=int(conta_id),
                franquia_id=int(franquia_id),
                idempotency_key=_build_calculation_funnel_idempotency_key(
                    user_id=int(user_id),
                    comparison_id=comparison_id,
                    execution_id=execution_id,
                ),
                comparison_id=comparison_id,
                execution_id=execution_id,
                correlation_id=None,
                metadata_json=None,
            )
            is_first_audit = False
            if funnel_result.get("created") is True and user.first_audit_completed_at is None:
                user.first_audit_completed_at = utcnow_naive()
                is_first_audit = True
            db.session.flush()
        if funnel_result.get("created") is not True:
            if started_funnel_tx:
                db.session.rollback()
            return payload, False
        db.session.commit()
        payload["is_first_audit"] = is_first_audit
        payload["funnel_event"] = {
            "event_name": FUNNEL_EVENT_FREIGHT_CALCULATED,
            "source": FUNNEL_SOURCE_AGENTE_COMPARA,
            "allow_meta_pixel": True,
            "is_first_audit": is_first_audit,
        }
        return payload, True
    except Exception as exc:
        db.session.rollback()
        logger.exception(
            "agente_compara_funnel_calculation_failed event=%s source=%s user_id=%s comparison_id=%s execution_id=%s failure_type=%s",
            FUNNEL_EVENT_FREIGHT_CALCULATED,
            FUNNEL_SOURCE_AGENTE_COMPARA,
            user_id,
            comparison_id,
            execution_id,
            exc.__class__.__name__,
        )
    return payload, False


def _short_fingerprint(fingerprint: str) -> str:
    return (fingerprint or "")[:12]


def agente_compara_comparison_calculation_idempotency_key(
    *,
    comparison_id: str,
    fingerprint: str,
) -> str:
    cmp_id = (comparison_id or "").strip()
    fp = (fingerprint or "").strip()
    return f"agente-compara-comparison-calculation:{cmp_id}:{fp}"


def _normalize_execution_id(raw: str | None) -> str:
    value = (raw or "").strip()
    if not value:
        raise AgenteComparaCalculationExecutionError(
            ERROR_EXECUTION_ID_REQUIRED,
            "Informe execution_id para processar os cálculos comparativos.",
            http_status=400,
        )
    if len(value) > 120:
        raise AgenteComparaCalculationExecutionError(
            ERROR_EXECUTION_ID_INVALID,
            "execution_id inválido.",
            http_status=400,
        )
    return value


def _require_session_obj(session_obj=None):
    if session_obj is not None:
        return session_obj
    if not has_request_context():
        raise RuntimeError("Execução de cálculo AgenteCompara requer request context Flask.")
    return session


def _empty_calculation_object() -> dict:
    return {
        "schema_version": CALCULATION_EXECUTION_SCHEMA_VERSION,
        "calculation_algorithm_version": AGENTE_COMPARA_CALCULATION_ALGORITHM_VERSION,
        "execution_id": None,
        "request_fingerprint": None,
        "fingerprint_short": None,
        "status": None,
        "stale": False,
        "started_at": None,
        "finished_at": None,
        "failed_at": None,
        "table_ids": [],
        "temp_table_ids": [],
        "slot_numbers": [],
        "source_file_identity": None,
        "source_row_count": 0,
        "calculated_table_count": 0,
        "calculated_cell_count": 0,
        "error_cell_count": 0,
        "result_storage_key": None,
        "result_size_bytes": None,
        "result_envelope_size_bytes": None,
        "result_checksum": None,
        "result_schema_version": None,
        "memory_storage_key": None,
        "memory_size_bytes": None,
        "memory_envelope_size_bytes": None,
        "memory_checksum": None,
        "raw_result_size_bytes": None,
        "compact_result_size_bytes": None,
        "memory_payload_size_bytes": None,
        "compaction_ratio": None,
        "serialization_duration_ms": None,
        "memory_save_duration_ms": None,
        "result_save_duration_ms": None,
        "table_count": 0,
        "cell_count": 0,
        "last_completed_stage": None,
        "failed_stage": None,
        "failed_artifact": None,
        "retryable": False,
        "safe_message": None,
        "error": None,
        "billing_status": BILLING_STATUS_NOT_STARTED,
        "billing_idempotency_key": None,
        "billing_applied_at": None,
        "attempt_count": 0,
    }


def _lightweight_calc_for_session(calc: dict) -> dict:
    """Garante que a sessão nunca receba results_by_table/comparative_rows."""
    payload = {k: v for k, v in dict(calc).items() if not str(k).startswith("_")}
    payload.pop("result", None)
    payload.pop("results_by_table", None)
    payload.pop("comparative_rows", None)
    # Analytics completo (com agregações) pode ser regenerado do storage; sessão fica leve.
    analytics = payload.get("analytics")
    if isinstance(analytics, dict):
        # Mantém apenas contagens leves se já existirem; remove blocos grandes acidentais.
        payload["analytics"] = {
            "schema_version": analytics.get("schema_version"),
            "comparison_id": analytics.get("comparison_id"),
            "table_count": analytics.get("table_count"),
            "row_count": analytics.get("row_count"),
            "global_summary": analytics.get("global_summary"),
            "tables": [
                {
                    "table_id": t.get("table_id"),
                    "slot_number": t.get("slot_number"),
                    "carrier_name": t.get("carrier_name"),
                    "display_name": t.get("display_name"),
                    "calculated_freight_total": t.get("calculated_freight_total"),
                    "calculated_freight_average": t.get("calculated_freight_average"),
                    "calculated_freight_per_kg": t.get("calculated_freight_per_kg"),
                    "calculated_rows": t.get("calculated_rows"),
                    "error_rows": t.get("error_rows"),
                    "coverage_percentage": t.get("coverage_percentage"),
                }
                for t in (analytics.get("tables") or [])
                if isinstance(t, dict)
            ],
        }
    return payload


def cleanup_comparison_calculation_storage(state: dict | None) -> bool:
    """Remove arquivo de resultado apontado pelo estado (reset/clear)."""
    calc = get_comparison_calculation(state)
    if not isinstance(calc, dict):
        return False
    key = calc.get("result_storage_key")
    mem_key = calc.get("memory_storage_key")
    removed_result = delete_comparison_calculation_result(key if isinstance(key, str) else None)
    removed_memory = delete_comparison_calculation_memories(mem_key if isinstance(mem_key, str) else None)
    return removed_result or removed_memory


def _billing_allows_result_release(calc: dict | None) -> bool:
    if not isinstance(calc, dict):
        return False
    return (calc.get("billing_status") or "").strip() == BILLING_STATUS_APPLIED


def _load_stored_result_or_raise(calc: dict, *, comparison_id: str) -> dict:
    key = (calc.get("result_storage_key") or "").strip()
    fingerprint = (calc.get("request_fingerprint") or "").strip()
    if not key or not fingerprint:
        raise AgenteComparaCalculationExecutionError(
            ERROR_RESULT_MISSING,
            "Resultado comparativo indisponível.",
            http_status=409,
        )
    try:
        result = load_comparison_calculation_result(
            storage_key=key,
            comparison_id=comparison_id,
            fingerprint=fingerprint,
            expected_checksum=(calc.get("result_checksum") or None),
        )
        memory_payload = None
        memory_key = (calc.get("memory_storage_key") or "").strip()
        if memory_key:
            memory_payload = load_comparison_calculation_memory_payload(
                storage_key=memory_key,
                comparison_id=comparison_id,
                fingerprint=fingerprint,
                expected_checksum=(calc.get("memory_checksum") or None),
            )
        result = _rehydrate_compact_result(result, memory_payload)
    except AgenteComparaCalculationResultStorageError as exc:
        code = ERROR_RESULT_MISSING if exc.error_code in {ERROR_RESULT_MISSING, ERROR_MEMORY_MISSING} else ERROR_RESULT_CORRUPT_PUBLIC
        raise AgenteComparaCalculationExecutionError(
            code,
            "Não foi possível recuperar o resultado comparativo.",
            http_status=409,
        ) from exc
    return _public_result(result) or {}


def _rehydrate_compact_result(result: dict | None, memory_payload: dict | None) -> dict | None:
    if not isinstance(result, dict):
        return None
    if not isinstance(memory_payload, dict):
        return copy.deepcopy(result)
    items = memory_payload.get("items") if isinstance(memory_payload.get("items"), dict) else {}
    hydrated = copy.deepcopy(result)
    for row in hydrated.get("comparative_rows") or []:
        if not isinstance(row, dict):
            continue
        table_results = row.get("table_results") if isinstance(row.get("table_results"), dict) else {}
        for cell in table_results.values():
            if not isinstance(cell, dict):
                continue
            memory_ref = (cell.get("memory_ref") or "").strip() if isinstance(cell.get("memory_ref"), str) else ""
            item = items.get(memory_ref) if memory_ref else None
            if not isinstance(item, dict):
                continue
            resolved_item = hydrate_memory_item(item, memory_payload)
            if isinstance(resolved_item.get("calculation_memory"), dict):
                cell["calculation_memory"] = copy.deepcopy(resolved_item.get("calculation_memory"))
            cell["components"] = copy.deepcopy(resolved_item.get("components") or {})
            cell["evidence"] = copy.deepcopy(resolved_item.get("evidence") or {})
            cell["warnings"] = copy.deepcopy(resolved_item.get("warnings") or [])
            cell["blocking_issues"] = copy.deepcopy(resolved_item.get("blocking_issues") or [])
    if int(hydrated.get("schema_version") or 0) == 2:
        hydrated["schema_version"] = 1
    return hydrated


def _build_analytics_for_released_result(result: dict | None) -> dict | None:
    """Analytics leve somente a partir do result já validado (sem motor/billing/Gemini)."""
    if not isinstance(result, dict):
        return None
    try:
        from app.agente_compara_comparison_analytics_service import (
            AgenteComparaComparisonAnalyticsError,
            build_comparison_analytics,
        )

        return build_comparison_analytics(result)
    except AgenteComparaComparisonAnalyticsError:
        logger.exception("agente_compara_analytics_invalid_result")
        return None
    except Exception:
        logger.exception("agente_compara_analytics_generation_failed")
        return None


def _build_ready_response(
    *,
    state: dict,
    calc: dict,
    idempotent_replay: bool,
    result: dict | None = None,
) -> dict:
    billing = (calc.get("billing_status") or BILLING_STATUS_NOT_STARTED).strip()
    release = _billing_allows_result_release(calc)
    stale = bool(calc.get("stale"))
    released_result = result if (release and not stale) else None
    payload = {
        "ok": bool(release),
        "status": STEP_CALCULATION_READY,
        "execution_id": calc.get("execution_id"),
        "fingerprint_short": calc.get("fingerprint_short"),
        "idempotent_replay": bool(idempotent_replay),
        "billing_status": billing,
        "stale": stale,
        "result": released_result,
        "analytics": _build_analytics_for_released_result(released_result) if released_result else None,
        "comparison": {
            "comparison_id": state.get("comparison_id"),
            "current_step": state.get("current_step"),
            "status": state.get("status"),
            "comparison_calculation": public_comparison_calculation_summary(calc, include_result=False),
        },
        "is_first_audit": False,
    }
    if billing == BILLING_STATUS_PENDING:
        payload["error_code"] = ERROR_BILLING_PENDING
        payload["message"] = "Cálculo concluído. Finalizando processamento..."
    elif billing == BILLING_STATUS_FAILED:
        payload["error_code"] = ERROR_BILLING_FAILED
        payload["message"] = "Cálculo concluído, mas a regularização da execução falhou. Tente novamente."
    if release and not stale:
        funnel_payload, _created = _record_calculation_funnel_event(
            comparison_id=str(state.get("comparison_id") or ""),
            execution_id=str(calc.get("execution_id") or ""),
            idempotent_replay=bool(idempotent_replay),
            calc=calc,
        )
        if funnel_payload:
            payload.update(funnel_payload)
    return payload


def _build_success_response(
    *,
    state: dict,
    calc: dict,
    idempotent_replay: bool,
) -> dict:
    # Compat: sucesso pleno somente com billing applied.
    result = None
    if _billing_allows_result_release(calc) and not calc.get("stale"):
        result = _load_stored_result_or_raise(calc, comparison_id=str(state.get("comparison_id") or ""))
    return _build_ready_response(
        state=state,
        calc=calc,
        idempotent_replay=idempotent_replay,
        result=result,
    )


def get_comparison_calculation(state: dict | None) -> dict | None:
    if not isinstance(state, dict):
        return None
    raw = state.get("comparison_calculation")
    return raw if isinstance(raw, dict) else None


def _confirmed_table_entries(state: dict) -> list[dict]:
    entries: list[dict] = []
    for slot in (1, 2, 3):
        entry = get_table_by_slot(state, slot)
        if entry is None:
            continue
        confirmed = bool(entry.get("confirmed")) or str(entry.get("status") or "") == "confirmed"
        if slot in (1, 2) and not confirmed:
            raise AgenteComparaCalculationExecutionError(
                ERROR_CALCULATION_NOT_READY,
                f"A tabela {slot} precisa estar confirmada antes do cálculo.",
                http_status=409,
            )
        if slot == 3:
            if not confirmed:
                continue
        if confirmed:
            entries.append(entry)
    if len(entries) < 2:
        raise AgenteComparaCalculationExecutionError(
            ERROR_CALCULATION_NOT_READY,
            "São necessárias pelo menos duas tabelas confirmadas.",
            http_status=409,
        )
    return entries


def _load_primary_operational_inputs(
    state: dict,
    *,
    ttl_hours: int,
    load_temp_table_record: Callable[..., dict | None],
) -> tuple[dict, list[dict], dict | list | None, dict]:
    primary_tt = (state.get("primary_temp_table_id") or "").strip()
    if not primary_tt:
        # Fallback: first confirmed temp table hosts shared operational file.
        entries = _confirmed_table_entries(state)
        primary_tt = (entries[0].get("temp_table_id") or "").strip()
    if not primary_tt:
        raise AgenteComparaCalculationExecutionError(
            ERROR_CALCULATION_NOT_READY,
            "Arquivo operacional indisponível para o cálculo comparativo.",
            http_status=409,
        )
    record = load_temp_table_record(primary_tt, ttl_hours=ttl_hours)
    if not isinstance(record, dict):
        raise AgenteComparaCalculationExecutionError(
            ERROR_CALCULATION_NOT_READY,
            "Tabela temporária do arquivo operacional não encontrada.",
            http_status=409,
        )
    audit_batch = record.get("audit_batch")
    if not isinstance(audit_batch, dict):
        raise AgenteComparaCalculationExecutionError(
            ERROR_CALCULATION_NOT_READY,
            "Arquivo operacional ainda não foi enviado.",
            http_status=409,
        )
    status = str(audit_batch.get("status") or "").strip().lower()
    if status not in {"uploaded", "ready", "processed", "stale"}:
        # Accept uploaded/ready-like statuses used by the product.
        if status not in {"uploaded", "ready"}:
            # Still allow if normalized_rows exist (tests / partial statuses).
            if not isinstance(audit_batch.get("normalized_rows"), list):
                raise AgenteComparaCalculationExecutionError(
                    ERROR_CALCULATION_NOT_READY,
                    "Arquivo operacional inválido ou incompleto.",
                    http_status=409,
                )
    rows = audit_batch.get("normalized_rows")
    if not isinstance(rows, list) or not rows:
        raise AgenteComparaCalculationExecutionError(
            ERROR_CALCULATION_NOT_READY,
            "Arquivo operacional sem linhas normalizadas.",
            http_status=409,
        )
    coverage = record.get("coverage_table")
    source_identity = {
        "audit_batch_id": audit_batch.get("audit_batch_id") or audit_batch.get("id"),
        "source_file_name": audit_batch.get("source_file_name"),
        "sheet_name": audit_batch.get("sheet_name"),
        "row_count": int(audit_batch.get("row_count") or len(rows)),
        "input_schema_version": audit_batch.get("input_schema_version"),
        "temp_table_id": primary_tt,
    }
    return record, list(rows), coverage, source_identity


def _validate_confirmed_table_record(entry: dict, record: dict, *, comparison_id: str) -> dict | None:
    temp_table_id = (entry.get("temp_table_id") or "").strip()
    slot_number = int(entry.get("slot_number") or 0)
    table_id = (entry.get("table_id") or "").strip()
    source_documents = [doc for doc in (record.get("source_documents") or []) if isinstance(doc, str) and doc.strip()]
    status = (record.get("status") or "").strip().lower()
    base = {
        "error_code": "comparison_table_preparation_failed",
        "error_stage": "table_preflight_validated",
        "retryable": bool(record.get("retryable")),
        "failed_table_id": table_id,
        "failed_table_name": entry.get("carrier_name"),
        "failed_slot": slot_number,
        "failed_document_id": record.get("active_document_id") or (source_documents[0] if source_documents else None),
        "failure_origin": record.get("failure_origin"),
        "failure_code": record.get("failure_code"),
        "credit_disposition": record.get("credit_disposition") or "preserved",
        "retry_of": record.get("retry_of"),
        "is_free_retry": bool(record.get("is_free_retry")),
    }
    if status != "needs_review":
        return {
            **base,
            "source_error_code": record.get("failure_code") or status or "table_not_ready",
            "safe_message": record.get("safe_message") or f"A tabela {entry.get('carrier_name') or slot_number} ainda n?o est? pronta. Processe novamente essa tabela antes de iniciar a compara??o.",
        }
    if record.get("failure_origin") or record.get("failure_code"):
        return {
            **base,
            "source_error_code": record.get("failure_code") or "table_has_residual_failure",
            "safe_message": record.get("safe_message") or f"A tabela {entry.get('carrier_name') or slot_number} ainda n?o est? pronta. Processe novamente essa tabela antes de iniciar a compara??o.",
        }
    if (record.get("comparison_id") or "").strip() not in {"", comparison_id}:
        return {**base, "retryable": False, "source_error_code": "comparison_scope_mismatch", "safe_message": "A tabela confirmada n?o pertence mais ? compara??o ativa."}
    if (record.get("table_id") or "").strip() not in {"", table_id}:
        return {**base, "retryable": False, "source_error_code": "table_identity_mismatch", "safe_message": "A tabela confirmada ficou inconsistente e precisa ser preparada novamente."}
    if not temp_table_id or not source_documents:
        return {**base, "source_error_code": "active_document_missing", "safe_message": f"A tabela {entry.get('carrier_name') or slot_number} ainda n?o est? pronta. Processe novamente essa tabela antes de iniciar a compara??o."}
    return None


def _raise_preflight_failure(metadata: dict) -> None:
    raise AgenteComparaCalculationExecutionError(
        metadata.get("error_code") or ERROR_CALCULATION_NOT_READY,
        metadata.get("safe_message") or "A compara??o n?o pode ser iniciada porque existe tabela inv?lida.",
        http_status=409,
        error_stage=metadata.get("error_stage"),
        retryable=bool(metadata.get("retryable")),
        failed_table_name=metadata.get("failed_table_name"),
        failed_table_id=metadata.get("failed_table_id"),
        failed_slot=metadata.get("failed_slot"),
        failure_origin=metadata.get("failure_origin"),
        failure_code=metadata.get("failure_code") or metadata.get("source_error_code"),
        credit_disposition=metadata.get("credit_disposition"),
        retry_of=metadata.get("retry_of"),
        is_free_retry=bool(metadata.get("is_free_retry")),
        safe_message=metadata.get("safe_message"),
    )


def _load_table_records(
    entries: list[dict],
    *,
    ttl_hours: int,
    load_temp_table_record: Callable[..., dict | None],
) -> dict[str, dict]:
    records: dict[str, dict] = {}
    for entry in entries:
        temp_id = (entry.get("temp_table_id") or "").strip()
        if not temp_id:
            raise AgenteComparaCalculationExecutionError(
                ERROR_CALCULATION_NOT_READY,
                "Tabela confirmada sem temp_table_id.",
                http_status=409,
            )
        record = load_temp_table_record(temp_id, ttl_hours=ttl_hours)
        if not isinstance(record, dict):
            raise AgenteComparaCalculationExecutionError(
                ERROR_CALCULATION_NOT_READY,
                f"Registro da tabela {entry.get('slot_number')} indisponível.",
                http_status=409,
            )
        records[temp_id] = record
        table_id = (entry.get("table_id") or "").strip()
        if table_id:
            records[table_id] = record
    return records


def build_calculation_fingerprint_payload(
    *,
    comparison_id: str,
    state: dict,
    normalized_rows: list[dict],
    table_records: dict[str, dict],
    tax_config: dict | None,
    coverage_table: dict | list | None,
    source_file_identity: dict,
    schema_version: int = MULTI_TABLE_CALCULATION_SCHEMA_VERSION,
) -> dict:
    entries = _confirmed_table_entries(state)
    tables_payload = []
    for entry in entries:
        temp_id = (entry.get("temp_table_id") or "").strip()
        record = table_records.get(temp_id) or table_records.get(entry.get("table_id") or "")
        record = record if isinstance(record, dict) else {}
        tables_payload.append(
            {
                "table_id": entry.get("table_id"),
                "temp_table_id": temp_id,
                "slot_number": int(entry.get("slot_number") or 0),
                "edit_version": int(record.get("edit_version") or 0),
                "updated_at": record.get("updated_at"),
                "human_review_status": record.get("human_review_status"),
            }
        )
    rows_digest = _sha256_hex(_canonical_json(normalized_rows))
    coverage_digest = _sha256_hex(_canonical_json(coverage_table if coverage_table is not None else {}))
    tax_digest_payload = copy.deepcopy(tax_config) if isinstance(tax_config, dict) else None
    return {
        "comparison_id": (comparison_id or "").strip(),
        "schema_version": int(schema_version),
        "calculation_algorithm_version": int(AGENTE_COMPARA_CALCULATION_ALGORITHM_VERSION),
        "source_file_identity": {
            "audit_batch_id": source_file_identity.get("audit_batch_id"),
            "source_file_name": source_file_identity.get("source_file_name"),
            "sheet_name": source_file_identity.get("sheet_name"),
            "row_count": int(source_file_identity.get("row_count") or len(normalized_rows)),
            "input_schema_version": source_file_identity.get("input_schema_version"),
            "temp_table_id": source_file_identity.get("temp_table_id"),
            "rows_digest": rows_digest,
        },
        "tables": tables_payload,
        "table_count": len(tables_payload),
        "tax_config": tax_digest_payload,
        "coverage_digest": coverage_digest,
    }


def compute_calculation_fingerprint(payload: dict) -> str:
    return _sha256_hex(_canonical_json(payload))


def _assert_step_allows_start(state: dict, *, fingerprint: str) -> None:
    step = state.get("current_step")
    calc = get_comparison_calculation(state) or {}
    stored_fp = (calc.get("request_fingerprint") or "").strip()
    calc_status = (calc.get("status") or "").strip()

    if step == STEP_CONFIGURATION_READY:
        return
    if step == STEP_CALCULATION_FAILED:
        return
    if step == STEP_CALCULATION_READY:
        if calc.get("stale") or (stored_fp and stored_fp != fingerprint):
            return
        # Same fingerprint READY is handled as replay before start; reaching here is ok.
        return
    if step == STEP_CALCULATION_RUNNING:
        raise AgenteComparaCalculationExecutionError(
            ERROR_EXECUTION_IN_PROGRESS,
            "Já existe um cálculo comparativo em andamento.",
            http_status=409,
        )
    raise AgenteComparaCalculationExecutionError(
        ERROR_COMPARISON_STEP_INVALID,
        "O cálculo comparativo só pode ser iniciado com a configuração concluída.",
        http_status=409,
    )


def _public_result(result: dict | None) -> dict | None:
    if not isinstance(result, dict):
        return None
    cloned = copy.deepcopy(result)

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in list(node.keys()):
                if key in _FORBIDDEN_PUBLIC_RESULT_FIELDS:
                    raise AgenteComparaCalculationExecutionError(
                        ERROR_CALCULATION_FAILED,
                        "Resultado comparativo contém campo proibido.",
                        http_status=500,
                    )
                _walk(node[key])
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(cloned)
    return cloned


def _build_running_response(*, state: dict, calc: dict) -> dict:
    return {
        "ok": True,
        "status": STEP_CALCULATION_RUNNING,
        "execution_id": calc.get("execution_id"),
        "fingerprint_short": calc.get("fingerprint_short"),
        "idempotent_replay": True,
        "started_at": calc.get("started_at"),
        "result": None,
        "comparison": {
            "comparison_id": state.get("comparison_id"),
            "current_step": state.get("current_step"),
            "status": state.get("status"),
            "comparison_calculation": public_comparison_calculation_summary(calc, include_result=False),
        },
    }


def get_comparison_calculation_status(
    *,
    comparison_id: str | None = None,
    session_obj=None,
    ttl_hours: int | None = None,
) -> dict:
    """GET read model — never recalculates, never bills."""
    sess = _require_session_obj(session_obj)
    state = get_comparison_state(sess)
    if state is None:
        raise AgenteComparaCalculationExecutionError(
            ERROR_COMPARISON_NOT_FOUND,
            "Nenhuma comparação ativa nesta sessão.",
            http_status=404,
        )
    cmp_id = (comparison_id or "").strip()
    if cmp_id and cmp_id != state.get("comparison_id"):
        raise AgenteComparaCalculationExecutionError(
            ERROR_COMPARISON_SCOPE_MISMATCH,
            "comparison_id não pertence à sessão atual.",
            http_status=409,
        )

    calc = get_comparison_calculation(state)
    if not isinstance(calc, dict) or not calc.get("status"):
        return {
            "ok": True,
            "status": "not_started",
            "execution_id": None,
            "result": None,
            "stale": False,
            "billing_status": BILLING_STATUS_NOT_STARTED,
            "comparison_id": state.get("comparison_id"),
            "current_step": state.get("current_step"),
        }

    # Detect stale against current fingerprint when possible.
    stale = bool(calc.get("stale"))
    if calc.get("status") == STEP_CALCULATION_READY and not stale:
        try:
            current_fp = _compute_current_fingerprint_for_state(state, session_obj=sess, ttl_hours=ttl_hours)
            if current_fp and calc.get("request_fingerprint") and current_fp != calc.get("request_fingerprint"):
                stale = True
                calc = dict(calc)
                calc["stale"] = True
                state = dict(state)
                state["comparison_calculation"] = calc
                if state.get("current_step") == STEP_CALCULATION_READY:
                    state["current_step"] = STEP_CONFIGURATION_READY
                    state["status"] = COMPARISON_STATUS_CONFIGURATION_READY
                persist_comparison_state(state, session_obj=sess)
        except Exception:
            # Read path must remain safe; ignore fingerprint rebuild failures.
            logger.debug("Falha ao avaliar stale do cálculo comparativo; mantendo estado persistido.", exc_info=True)

    status = calc.get("status")
    billing = calc.get("billing_status") or BILLING_STATUS_NOT_STARTED
    payload = {
        "ok": True,
        "status": status,
        "execution_id": calc.get("execution_id"),
        "fingerprint_short": calc.get("fingerprint_short"),
        "started_at": calc.get("started_at"),
        "finished_at": calc.get("finished_at"),
        "failed_at": calc.get("failed_at"),
        "stale": stale,
        "billing_status": billing,
        "error": calc.get("error") if status == STEP_CALCULATION_FAILED else None,
        "result": None,
        "analytics": None,
        "previous_result_available": bool(
            stale and calc.get("result_storage_key") and calc.get("status") == STEP_CALCULATION_READY
        ),
        "comparison_id": state.get("comparison_id"),
        "current_step": state.get("current_step"),
    }
    if status == STEP_CALCULATION_RUNNING:
        payload["result"] = None
        payload["analytics"] = None
        return payload

    if status == STEP_CALCULATION_READY and not stale:
        if billing == BILLING_STATUS_APPLIED:
            try:
                loaded = _load_stored_result_or_raise(
                    calc,
                    comparison_id=str(state.get("comparison_id") or ""),
                )
                payload["result"] = loaded
                payload["analytics"] = _build_analytics_for_released_result(loaded)
            except AgenteComparaCalculationExecutionError as exc:
                payload["ok"] = False
                payload["error_code"] = exc.error_code
                payload["message"] = exc.message
                payload["result"] = None
                payload["analytics"] = None
        elif billing == BILLING_STATUS_PENDING:
            payload["message"] = "Cálculo concluído. Finalizando processamento..."
            payload["error_code"] = ERROR_BILLING_PENDING
        elif billing == BILLING_STATUS_FAILED:
            payload["message"] = "Cálculo concluído, mas a regularização da execução falhou."
            payload["error_code"] = ERROR_BILLING_FAILED
    return payload


def _compute_current_fingerprint_for_state(state: dict, *, session_obj=None, ttl_hours: int | None = None) -> str | None:
    from app.agente_compara_doc_service import load_temp_table_record
    from app.services.cleiton_doc_config_service import get_cleiton_doc_config

    cfg = get_cleiton_doc_config()
    effective_ttl = int(ttl_hours if ttl_hours is not None else cfg.upload_ttl_hours)
    _primary, rows, coverage, source_identity = _load_primary_operational_inputs(
        state,
        ttl_hours=effective_ttl,
        load_temp_table_record=load_temp_table_record,
    )
    entries = _confirmed_table_entries(state)
    table_records = _load_table_records(
        entries,
        ttl_hours=effective_ttl,
        load_temp_table_record=load_temp_table_record,
    )
    payload = build_calculation_fingerprint_payload(
        comparison_id=state["comparison_id"],
        state=state,
        normalized_rows=rows,
        table_records=table_records,
        tax_config=get_comparison_tax_config(state),
        coverage_table=coverage,
        source_file_identity=source_identity,
    )
    return compute_calculation_fingerprint(payload)


def mark_comparison_calculation_stale(state: dict, *, session_obj=None) -> dict:
    """Marks persisted READY result as stale when inputs change."""
    calc = get_comparison_calculation(state)
    if not isinstance(calc, dict):
        return state
    if calc.get("status") not in {STEP_CALCULATION_READY, STEP_CALCULATION_FAILED, STEP_CALCULATION_RUNNING}:
        return state
    updated = dict(calc)
    updated["stale"] = True
    if updated.get("status") == STEP_CALCULATION_READY:
        # Keep last technical result, but demote journey so UI can re-run.
        state["current_step"] = STEP_CONFIGURATION_READY
        state["status"] = COMPARISON_STATUS_CONFIGURATION_READY
    state["comparison_calculation"] = updated
    return persist_comparison_state(state, session_obj=session_obj)


def _apply_billing(
    *,
    calc: dict,
    rows_processed: int,
    started_perf: float,
    execution_id: str,
    emit_billing: Callable[..., Any] | None,
) -> tuple[dict, bool]:
    """Returns (updated_calc, billing_ok). Never recalculates."""
    if calc.get("billing_status") == BILLING_STATUS_APPLIED:
        return calc, True

    key = calc.get("billing_idempotency_key")
    if not key:
        key = agente_compara_comparison_calculation_idempotency_key(
            comparison_id=str(calc.get("comparison_id") or ""),
            fingerprint=str(calc.get("request_fingerprint") or ""),
        )

    updated = dict(calc)
    updated["billing_status"] = BILLING_STATUS_PENDING
    updated["billing_idempotency_key"] = key

    if emit_billing is None:
        from app.agente_compara_doc_service import _emit_agente_compara_operational_billing

        emitted = [False]
        try:
            _emit_agente_compara_operational_billing(
                emitted=emitted,
                started_at=started_perf,
                flow_type=FLOW_TYPE_COMPARISON_CALCULATION,
                idempotency_key=key,
                rows_processed=max(0, int(rows_processed)),
                status="success",
                execution_id=execution_id,
            )
            updated["billing_status"] = BILLING_STATUS_APPLIED
            updated["billing_applied_at"] = _utcnow_iso()
            return updated, True
        except Exception:
            logger.exception("Falha ao apropriar billing do cálculo comparativo.")
            updated["billing_status"] = BILLING_STATUS_FAILED
            return updated, False

    try:
        emit_billing(
            flow_type=FLOW_TYPE_COMPARISON_CALCULATION,
            idempotency_key=key,
            rows_processed=max(0, int(rows_processed)),
            execution_id=execution_id,
            started_at=started_perf,
        )
        updated["billing_status"] = BILLING_STATUS_APPLIED
        updated["billing_applied_at"] = _utcnow_iso()
        return updated, True
    except Exception:
        logger.exception("Falha ao apropriar billing do cálculo comparativo (hook).")
        updated["billing_status"] = BILLING_STATUS_FAILED
        return updated, False


def execute_comparison_calculation(
    *,
    comparison_id: str | None,
    execution_id: str | None,
    schema_version: int | None = None,
    session_obj=None,
    ttl_hours: int | None = None,
    calculate_fn: Callable[..., dict] | None = None,
    emit_billing: Callable[..., Any] | None = None,
    load_temp_table_record: Callable[..., dict | None] | None = None,
    after_running_hook: Callable[[dict], None] | None = None,
) -> dict:
    """
    Orchestrates a synchronous comparison calculation with idempotency and billing.

    Order:
    1. validate ownership/schema
    2. acquire comparison lock
    3. reload state + fingerprint; resolve idempotency
    4. persist RUNNING (lightweight / no result payload)
    5. run orchestrator + validate serialization
    6. post-check fingerprint (fail before save if changed)
    7. save result to storage; persist READY metadata only
    8. apply billing; update billing_status
    """
    sess = _require_session_obj(session_obj)
    exec_id = _normalize_execution_id(execution_id)
    started_perf = time.perf_counter()

    state = get_comparison_state(sess)
    if state is None:
        raise AgenteComparaCalculationExecutionError(
            ERROR_COMPARISON_NOT_FOUND,
            "Nenhuma comparação ativa nesta sessão.",
            http_status=404,
        )

    cmp_id = (comparison_id or "").strip()
    if not cmp_id:
        raise AgenteComparaCalculationExecutionError(
            ERROR_COMPARISON_NOT_FOUND,
            "comparison_id é obrigatório.",
            http_status=400,
        )
    if cmp_id != state.get("comparison_id"):
        raise AgenteComparaCalculationExecutionError(
            ERROR_COMPARISON_SCOPE_MISMATCH,
            "comparison_id não pertence à sessão atual.",
            http_status=409,
        )

    if schema_version is not None and int(schema_version) != MULTI_TABLE_CALCULATION_SCHEMA_VERSION:
        raise AgenteComparaCalculationExecutionError(
            ERROR_CALCULATION_NOT_READY,
            "schema_version de cálculo não suportado.",
            http_status=400,
        )

    from app.services.cleiton_doc_config_service import get_cleiton_doc_config

    cfg = get_cleiton_doc_config()
    effective_ttl = int(ttl_hours if ttl_hours is not None else cfg.upload_ttl_hours)
    loader = load_temp_table_record
    if loader is None:
        from app.agente_compara_doc_service import load_temp_table_record as _loader

        loader = _loader

    try:
        with acquire_comparison_calculation_lock(cmp_id):
            state = get_comparison_state(sess)
            if state is None:
                raise AgenteComparaCalculationExecutionError(
                    ERROR_COMPARISON_NOT_FOUND,
                    "Nenhuma comparação ativa nesta sessão.",
                    http_status=404,
                )
            if cmp_id != state.get("comparison_id"):
                raise AgenteComparaCalculationExecutionError(
                    ERROR_COMPARISON_SCOPE_MISMATCH,
                    "comparison_id não pertence à sessão atual.",
                    http_status=409,
                )

            entries = _confirmed_table_entries(state)
            _primary, rows, coverage, source_identity = _load_primary_operational_inputs(
                state,
                ttl_hours=effective_ttl,
                load_temp_table_record=loader,
            )
            table_records = _load_table_records(
                entries, ttl_hours=effective_ttl, load_temp_table_record=loader
            )
            for entry in entries:
                temp_id = (entry.get("temp_table_id") or "").strip()
                record = table_records.get(temp_id) or table_records.get((entry.get("table_id") or "").strip())
                if not isinstance(record, dict):
                    _raise_preflight_failure({
                        "error_code": "comparison_table_preparation_failed",
                        "safe_message": f"A tabela {entry.get('carrier_name') or entry.get('slot_number')} ainda n?o est? pronta. Processe novamente essa tabela antes de iniciar a compara??o.",
                    })
                preflight_error = _validate_confirmed_table_record(entry, record, comparison_id=cmp_id)
                if preflight_error is not None:
                    _raise_preflight_failure(preflight_error)
            tax_config = get_comparison_tax_config(state)
            fp_payload = build_calculation_fingerprint_payload(
                comparison_id=cmp_id,
                state=state,
                normalized_rows=rows,
                table_records=table_records,
                tax_config=tax_config,
                coverage_table=coverage,
                source_file_identity=source_identity,
            )
            fingerprint = compute_calculation_fingerprint(fp_payload)
            fingerprint_short = _short_fingerprint(fingerprint)

            existing = get_comparison_calculation(state) or {}
            existing_status = (existing.get("status") or "").strip()
            existing_fp = (existing.get("request_fingerprint") or "").strip()
            existing_exec = (existing.get("execution_id") or "").strip()
            existing_billing = (existing.get("billing_status") or BILLING_STATUS_NOT_STARTED).strip()

            # --- Idempotency resolution ---
            if (
                existing_status == STEP_CALCULATION_READY
                and existing_fp == fingerprint
                and not existing.get("stale")
            ):
                if existing_billing in {BILLING_STATUS_PENDING, BILLING_STATUS_FAILED}:
                    calc_for_billing = dict(existing)
                    calc_for_billing["comparison_id"] = cmp_id
                    calc_for_billing, _ok = _apply_billing(
                        calc=calc_for_billing,
                        rows_processed=int(existing.get("source_row_count") or len(rows)),
                        started_perf=started_perf,
                        execution_id=str(existing.get("execution_id") or exec_id),
                        emit_billing=emit_billing,
                    )
                    state = dict(state)
                    state["comparison_calculation"] = _lightweight_calc_for_session(calc_for_billing)
                    persist_comparison_state(state, session_obj=sess)
                    existing = calc_for_billing

                logger.info(
                    "agente_compara_comparison_calc replay comparison_id=%s execution_id=%s fingerprint=%s "
                    "table_count=%s row_count=%s status=%s idempotent_replay=true "
                    "calculation_algorithm_version=%s replay_version_match=true",
                    cmp_id,
                    existing.get("execution_id"),
                    fingerprint_short,
                    existing.get("calculated_table_count"),
                    existing.get("source_row_count"),
                    existing.get("status"),
                    AGENTE_COMPARA_CALCULATION_ALGORITHM_VERSION,
                )
                # Loads from storage when billing applied; never uses session result.
                # Missing/corrupt storage raises ERROR_RESULT_MISSING (no silent recalculate).
                return _build_success_response(state=state, calc=existing, idempotent_replay=True)

            existing_algo = existing.get("calculation_algorithm_version")
            if (
                existing_status == STEP_CALCULATION_READY
                and existing_fp
                and existing_fp != fingerprint
                and (
                    existing_algo is None
                    or int(existing_algo) != int(AGENTE_COMPARA_CALCULATION_ALGORITHM_VERSION)
                )
            ):
                logger.info(
                    "agente_compara_comparison_calc replay_rejected_version_mismatch "
                    "comparison_id=%s execution_id=%s fingerprint=%s "
                    "existing_algorithm_version=%s current_algorithm_version=%s "
                    "replay_rejected_version_mismatch=true",
                    cmp_id,
                    exec_id,
                    fingerprint_short,
                    existing_algo,
                    AGENTE_COMPARA_CALCULATION_ALGORITHM_VERSION,
                )

            if existing_status == STEP_CALCULATION_RUNNING and existing_fp == fingerprint:
                if existing_exec == exec_id:
                    return _build_running_response(state=state, calc=existing)
                raise AgenteComparaCalculationExecutionError(
                    ERROR_EXECUTION_IN_PROGRESS,
                    "Já existe um cálculo comparativo em andamento para esta configuração.",
                    http_status=409,
                )

            if existing_exec == exec_id and existing_fp and existing_fp != fingerprint:
                raise AgenteComparaCalculationExecutionError(
                    ERROR_EXECUTION_CONFLICT,
                    "execution_id já foi usado com outra configuração de entrada.",
                    http_status=409,
                )

            if existing_status == STEP_CALCULATION_RUNNING and existing_fp != fingerprint:
                raise AgenteComparaCalculationExecutionError(
                    ERROR_EXECUTION_IN_PROGRESS,
                    "Já existe um cálculo comparativo em andamento.",
                    http_status=409,
                )

            _assert_step_allows_start(state, fingerprint=fingerprint)

            previous_storage_key = (
                (existing.get("result_storage_key") or "").strip() or None
            )

            # --- Persist RUNNING before motor (no result payload in session) ---
            attempt_count = int(existing.get("attempt_count") or 0) + 1
            billing_key = agente_compara_comparison_calculation_idempotency_key(
                comparison_id=cmp_id,
                fingerprint=fingerprint,
            )
            running_calc = _empty_calculation_object()
            running_calc.update(
                {
                    "execution_id": exec_id,
                    "request_fingerprint": fingerprint,
                    "fingerprint_short": fingerprint_short,
                    "calculation_algorithm_version": AGENTE_COMPARA_CALCULATION_ALGORITHM_VERSION,
                    "status": STEP_CALCULATION_RUNNING,
                    "stale": False,
                    "started_at": _utcnow_iso(),
                    "table_ids": [e.get("table_id") for e in entries],
                    "temp_table_ids": [e.get("temp_table_id") for e in entries],
                    "slot_numbers": [int(e.get("slot_number") or 0) for e in entries],
                    "source_file_identity": source_identity,
                    "source_row_count": len(rows),
                    "result_storage_key": None,
                    "result_size_bytes": None,
                    "result_checksum": None,
                    "result_schema_version": None,
                    "billing_status": BILLING_STATUS_NOT_STARTED,
                    "billing_idempotency_key": billing_key,
                    "attempt_count": attempt_count,
                    "comparison_id": cmp_id,
                }
            )
            running_calc.pop("result", None)

            state = dict(state)
            state["current_step"] = STEP_CALCULATION_RUNNING
            state["status"] = COMPARISON_STATUS_CALCULATION_RUNNING
            state["comparison_calculation"] = _lightweight_calc_for_session(running_calc)
            try:
                persist_comparison_state(state, session_obj=sess)
            except Exception as exc:
                raise AgenteComparaCalculationExecutionError(
                    ERROR_CALCULATION_FAILED,
                    "Não foi possível iniciar o cálculo comparativo.",
                    http_status=500,
                ) from exc

            if after_running_hook is not None:
                after_running_hook(copy.deepcopy(state))

            logger.info(
                "agente_compara_comparison_calc start comparison_id=%s execution_id=%s fingerprint=%s "
                "table_count=%s row_count=%s status=%s idempotent_replay=false "
                "calculation_algorithm_version=%s",
                cmp_id,
                exec_id,
                fingerprint_short,
                len(entries),
                len(rows),
                STEP_CALCULATION_RUNNING,
                AGENTE_COMPARA_CALCULATION_ALGORITHM_VERSION,
            )

            motor = calculate_fn or calculate_comparison_in_memory
            saved_storage_key: str | None = None
            saved_memory_key: str | None = None
            try:
                context = build_multi_table_calculation_context(
                    comparison_id=cmp_id,
                    comparison_state=state,
                    normalized_rows=rows,
                    table_records=table_records,
                    tax_config=tax_config,
                    coverage_table=coverage,
                    schema_version=MULTI_TABLE_CALCULATION_SCHEMA_VERSION,
                    execution_id=exec_id,
                    ttl_hours=effective_ttl,
                )
                result = motor(context)
                if isinstance(result, dict):
                    result = dict(result)
                    result["calculation_algorithm_version"] = AGENTE_COMPARA_CALCULATION_ALGORITHM_VERSION
                validate_comparison_result_serializable(
                    result, comparison_id=cmp_id, execution_id=exec_id
                )
                public_result = _public_result(result)
                raw_result_size_bytes = len(
                    json.dumps(
                        result,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                        default=str,
                    ).encode("utf-8")
                )

                # Post-check fingerprint BEFORE save — fail without writing storage.
                live_state = get_comparison_state(sess) or state
                try:
                    live_fp = _compute_current_fingerprint_for_state(
                        live_state, session_obj=sess, ttl_hours=effective_ttl
                    )
                except Exception:
                    live_fp = None
                if live_fp and live_fp != fingerprint:
                    raise AgenteComparaCalculationExecutionError(
                        ERROR_CALCULATION_INPUT_CHANGED,
                        "A configuração mudou durante o cálculo. Tente novamente.",
                        http_status=409,
                    )

                compacted = compact_comparison_result_for_storage(result)
                compact_result = compacted["compact_result"]
                memory_payload = compacted["memory_payload"]
                public_result = _public_result(_rehydrate_compact_result(compact_result, memory_payload))
                try:
                    memory_storage_meta = save_comparison_calculation_memory_payload(
                        comparison_id=cmp_id,
                        fingerprint=fingerprint,
                        memory_payload=memory_payload,
                    )
                    saved_memory_key = (memory_storage_meta.get("memory_storage_key") or "").strip() or None
                    storage_meta = save_comparison_calculation_result(
                        comparison_id=cmp_id,
                        fingerprint=fingerprint,
                        result=compact_result,
                        memory_storage_meta=memory_storage_meta,
                    )
                except AgenteComparaCalculationResultStorageError as exc:
                    logger.exception(
                        "agente_compara_comparison_calc storage_save_failed "
                        "comparison_id=%s execution_id=%s fingerprint=%s artifact=%s stage=%s operation=%s exc_class=%s",
                        cmp_id,
                        exec_id,
                        fingerprint_short,
                        exc.artifact_type,
                        exc.error_stage,
                        exc.operation,
                        exc.exc_class,
                    )
                    cleanup = {}
                    if saved_storage_key:
                        cleanup["result_cleanup_ok"] = delete_comparison_calculation_result(saved_storage_key)
                    if saved_memory_key and (exc.artifact_type == "result" or saved_storage_key):
                        cleanup["memory_cleanup_ok"] = delete_comparison_calculation_memories(saved_memory_key)
                    return _persist_failure(
                        session_obj=sess,
                        state=state,
                        running_calc=running_calc,
                        error_code=exc.error_code,
                        message=exc.safe_message,
                        started_perf=started_perf,
                        fingerprint_short=fingerprint_short,
                        error_stage=exc.error_stage,
                        artifact_type=exc.artifact_type,
                        retryable=exc.retryable,
                        failed_artifact=exc.artifact_type,
                        failure_metrics={**dict(exc.metrics or {}), **cleanup},
                    )

                saved_storage_key = (storage_meta.get("result_storage_key") or "").strip() or None

            except AgenteComparaCalculationExecutionError as exc:
                if saved_storage_key:
                    delete_comparison_calculation_result(saved_storage_key)
                if saved_memory_key:
                    delete_comparison_calculation_memories(saved_memory_key)
                return _persist_failure(
                    session_obj=sess,
                    state=state,
                    running_calc=running_calc,
                    error_code=exc.error_code,
                    message=exc.message,
                    started_perf=started_perf,
                    fingerprint_short=fingerprint_short,
                )
            except (InvalidMultiTableCalculationContextError, MultiTableCalculationInvariantError) as exc:
                if saved_storage_key:
                    delete_comparison_calculation_result(saved_storage_key)
                if saved_memory_key:
                    delete_comparison_calculation_memories(saved_memory_key)
                return _persist_failure(
                    session_obj=sess,
                    state=state,
                    running_calc=running_calc,
                    error_code=getattr(exc, "error_code", ERROR_CALCULATION_FAILED),
                    message=getattr(exc, "message", str(exc)),
                    started_perf=started_perf,
                    fingerprint_short=fingerprint_short,
                )
            except UnexpectedMultiTableCalculationError as exc:
                if saved_storage_key:
                    delete_comparison_calculation_result(saved_storage_key)
                if saved_memory_key:
                    delete_comparison_calculation_memories(saved_memory_key)
                logger.exception(
                    "agente_compara_comparison_calc systemic_error comparison_id=%s execution_id=%s fingerprint=%s",
                    cmp_id,
                    exec_id,
                    fingerprint_short,
                )
                return _persist_failure(
                    session_obj=sess,
                    state=state,
                    running_calc=running_calc,
                    error_code=getattr(exc, "error_code", ERROR_CALCULATION_FAILED),
                    message="Não foi possível concluir o cálculo comparativo.",
                    started_perf=started_perf,
                    fingerprint_short=fingerprint_short,
                    raise_as_http=True,
                )
            except AgenteComparaMultiTableCalculationError as exc:
                if saved_storage_key:
                    delete_comparison_calculation_result(saved_storage_key)
                if saved_memory_key:
                    delete_comparison_calculation_memories(saved_memory_key)
                return _persist_failure(
                    session_obj=sess,
                    state=state,
                    running_calc=running_calc,
                    error_code=getattr(exc, "error_code", ERROR_CALCULATION_FAILED),
                    message=getattr(exc, "message", "Falha no cálculo comparativo."),
                    started_perf=started_perf,
                    fingerprint_short=fingerprint_short,
                )
            except Exception:
                if saved_storage_key:
                    delete_comparison_calculation_result(saved_storage_key)
                if saved_memory_key:
                    delete_comparison_calculation_memories(saved_memory_key)
                logger.exception(
                    "agente_compara_comparison_calc unexpected comparison_id=%s execution_id=%s fingerprint=%s",
                    cmp_id,
                    exec_id,
                    fingerprint_short,
                )
                return _persist_failure(
                    session_obj=sess,
                    state=state,
                    running_calc=running_calc,
                    error_code=ERROR_CALCULATION_FAILED,
                    message="Não foi possível concluir o cálculo comparativo.",
                    started_perf=started_perf,
                    fingerprint_short=fingerprint_short,
                    raise_as_http=True,
                )

            summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
            ready_calc = dict(running_calc)
            ready_calc.update(
                {
                    "status": STEP_CALCULATION_READY,
                    "finished_at": _utcnow_iso(),
                    "failed_at": None,
                    "error": None,
                    "result_storage_key": storage_meta.get("result_storage_key"),
                    "result_checksum": storage_meta.get("result_checksum"),
                    "result_size_bytes": storage_meta.get("result_size_bytes"),
                    "result_schema_version": storage_meta.get("result_schema_version"),
                    "memory_storage_key": storage_meta.get("memory_storage_key"),
                    "memory_checksum": storage_meta.get("memory_checksum"),
                    "memory_size_bytes": storage_meta.get("memory_size_bytes"),
                    "result_envelope_size_bytes": storage_meta.get("result_envelope_size_bytes"),
                    "memory_envelope_size_bytes": storage_meta.get("memory_envelope_size_bytes"),
                    "raw_result_size_bytes": raw_result_size_bytes,
                    "compact_result_size_bytes": storage_meta.get("result_size_bytes"),
                    "memory_payload_size_bytes": storage_meta.get("memory_size_bytes"),
                    "compaction_ratio": round(float(storage_meta.get("result_size_bytes") or 0) / float(raw_result_size_bytes or 1), 6),
                    "serialization_duration_ms": int((memory_storage_meta.get("memory_serialization_duration_ms") or 0) + (storage_meta.get("result_serialization_duration_ms") or 0)) or None,
                    "memory_save_duration_ms": memory_storage_meta.get("memory_save_duration_ms"),
                    "result_save_duration_ms": storage_meta.get("result_save_duration_ms"),
                    "table_count": int(result.get("table_count") or len(entries)),
                    "cell_count": int(summary.get("total_calculation_cells") or 0),
                    "last_completed_stage": "session_pointer_updated",
                    "failed_stage": None,
                    "failed_artifact": None,
                    "retryable": False,
                    "safe_message": None,
                    "calculated_table_count": int(result.get("table_count") or len(entries)),
                    "calculated_cell_count": int(summary.get("calculated_cell_count") or 0),
                    "error_cell_count": int(summary.get("error_cell_count") or 0),
                    "billing_status": BILLING_STATUS_PENDING,
                    "stale": False,
                }
            )
            ready_calc.pop("result", None)

            if previous_storage_key and previous_storage_key != ready_calc.get("result_storage_key"):
                delete_comparison_calculation_result(previous_storage_key)

            state = dict(get_comparison_state(sess) or state)
            current_calc = get_comparison_calculation(state) or {}
            if (
                current_calc.get("status") == STEP_CALCULATION_READY
                and current_calc.get("request_fingerprint")
                and current_calc.get("request_fingerprint") != fingerprint
                and current_calc.get("started_at")
                and ready_calc.get("started_at")
                and str(current_calc.get("started_at")) > str(ready_calc.get("started_at"))
            ):
                if saved_storage_key:
                    delete_comparison_calculation_result(saved_storage_key)
                if saved_memory_key:
                    delete_comparison_calculation_memories(saved_memory_key)
                raise AgenteComparaCalculationExecutionError(
                    ERROR_EXECUTION_CONFLICT,
                    "Resultado mais recente já foi persistido para outra configuração.",
                    http_status=409,
                )

            state["current_step"] = STEP_CALCULATION_READY
            state["status"] = COMPARISON_STATUS_CALCULATION_READY
            state["comparison_calculation"] = _lightweight_calc_for_session(ready_calc)
            try:
                persist_comparison_state(state, session_obj=sess)
            except Exception:
                if saved_storage_key:
                    delete_comparison_calculation_result(saved_storage_key)
                if saved_memory_key:
                    delete_comparison_calculation_memories(saved_memory_key)
                logger.exception(
                    "agente_compara_comparison_calc ready_persist_failed "
                    "comparison_id=%s execution_id=%s fingerprint=%s",
                    cmp_id,
                    exec_id,
                    fingerprint_short,
                )
                return _persist_failure(
                    session_obj=sess,
                    state=state,
                    running_calc=running_calc,
                    error_code=ERROR_CALCULATION_FAILED,
                    message="Não foi possível concluir o cálculo comparativo.",
                    started_perf=started_perf,
                    fingerprint_short=fingerprint_short,
                )

            ready_calc, billing_ok = _apply_billing(
                calc=ready_calc,
                rows_processed=len(rows),
                started_perf=started_perf,
                execution_id=exec_id,
                emit_billing=emit_billing,
            )
            state = dict(get_comparison_state(sess) or state)
            state["comparison_calculation"] = _lightweight_calc_for_session(ready_calc)
            # Keep READY even if billing failed — do not conflate with math failure.
            state["current_step"] = STEP_CALCULATION_READY
            state["status"] = COMPARISON_STATUS_CALCULATION_READY
            persist_comparison_state(state, session_obj=sess)

            duration_ms = int((time.perf_counter() - started_perf) * 1000)
            logger.info(
                "agente_compara_comparison_calc done comparison_id=%s execution_id=%s fingerprint=%s "
                "table_count=%s row_count=%s total_calculation_cells=%s calculated_cell_count=%s "
                "error_cell_count=%s duration_ms=%s status=%s idempotent_replay=false billing_status=%s "
                "calculation_algorithm_version=%s",
                cmp_id,
                exec_id,
                fingerprint_short,
                ready_calc.get("calculated_table_count"),
                ready_calc.get("source_row_count"),
                summary.get("total_calculation_cells"),
                ready_calc.get("calculated_cell_count"),
                ready_calc.get("error_cell_count"),
                duration_ms,
                STEP_CALCULATION_READY,
                ready_calc.get("billing_status"),
                AGENTE_COMPARA_CALCULATION_ALGORITHM_VERSION,
            )

            return _build_ready_response(
                state=state,
                calc=ready_calc,
                idempotent_replay=False,
                result=public_result if billing_ok else None,
            )
    except AgenteComparaCalculationLockError as exc:
        raise AgenteComparaCalculationExecutionError(
            exc.error_code,
            exc.message,
            http_status=exc.http_status,
        ) from exc


def _persist_failure(
    *,
    session_obj,
    state: dict,
    running_calc: dict,
    error_code: str,
    message: str,
    started_perf: float,
    fingerprint_short: str,
    raise_as_http: bool = False,
    error_stage: str | None = None,
    artifact_type: str | None = None,
    retryable: bool = False,
    failed_artifact: str | None = None,
    failure_metrics: dict | None = None,
) -> dict:
    failed = dict(running_calc)
    failure_metrics = dict(failure_metrics or {})
    failed.update(
        {
            "status": STEP_CALCULATION_FAILED,
            "failed_at": _utcnow_iso(),
            "finished_at": None,
            "error": {
                "code": error_code,
                "message": message,
                "stage": error_stage,
                "artifact_type": artifact_type,
                "retryable": bool(retryable),
            },
            "billing_status": BILLING_STATUS_NOT_STARTED,
            "stale": False,
            "result_storage_key": None,
            "result_checksum": None,
            "result_schema_version": None,
            "memory_storage_key": None,
            "memory_checksum": None,
            "failed_artifact": failed_artifact or artifact_type,
            "failed_stage": error_stage,
            "retryable": bool(retryable),
            "safe_message": message,
            "last_completed_stage": failure_metrics.get("last_completed_stage"),
            "raw_result_size_bytes": failure_metrics.get("raw_result_size_bytes", failed.get("raw_result_size_bytes")),
            "compact_result_size_bytes": failure_metrics.get("compact_result_size_bytes", failed.get("compact_result_size_bytes")),
            "memory_payload_size_bytes": failure_metrics.get("memory_payload_size_bytes", failed.get("memory_payload_size_bytes")),
            "result_envelope_size_bytes": failure_metrics.get("result_envelope_size_bytes", failed.get("result_envelope_size_bytes")),
            "memory_envelope_size_bytes": failure_metrics.get("memory_envelope_size_bytes", failed.get("memory_envelope_size_bytes")),
            "compaction_ratio": failure_metrics.get("compaction_ratio", failed.get("compaction_ratio")),
            "serialization_duration_ms": failure_metrics.get("serialization_duration_ms", failed.get("serialization_duration_ms")),
            "memory_save_duration_ms": failure_metrics.get("memory_save_duration_ms", failed.get("memory_save_duration_ms")),
            "result_save_duration_ms": failure_metrics.get("result_save_duration_ms", failed.get("result_save_duration_ms")),
            "table_count": failure_metrics.get("table_count", failed.get("table_count") or failed.get("calculated_table_count") or 0),
            "cell_count": failure_metrics.get("cell_count", failed.get("cell_count") or 0),
            "result_size_bytes": failure_metrics.get("result_size_bytes", failed.get("result_size_bytes")),
            "memory_size_bytes": failure_metrics.get("memory_size_bytes", failed.get("memory_size_bytes")),
            "calculated_table_count": failure_metrics.get("table_count", failed.get("calculated_table_count") or 0),
            "calculated_cell_count": failure_metrics.get("cell_count", failed.get("calculated_cell_count") or 0),
            "error_cell_count": failed.get("error_cell_count") or 0,
        }
    )
    failed.pop("result", None)
    failed.pop("_previous_result", None)
    new_state = dict(get_comparison_state(session_obj) or state)
    new_state["current_step"] = STEP_CALCULATION_FAILED
    new_state["status"] = COMPARISON_STATUS_CALCULATION_FAILED
    new_state["comparison_calculation"] = _lightweight_calc_for_session(failed)
    persist_comparison_state(new_state, session_obj=session_obj)

    duration_ms = int((time.perf_counter() - started_perf) * 1000)
    logger.info(
        "agente_compara_comparison_calc failed comparison_id=%s execution_id=%s fingerprint=%s "
        "duration_ms=%s status=%s failure_code=%s idempotent_replay=false",
        new_state.get("comparison_id"),
        failed.get("execution_id"),
        fingerprint_short,
        duration_ms,
        STEP_CALCULATION_FAILED,
        error_code,
    )

    payload = {
        "ok": False,
        "status": STEP_CALCULATION_FAILED,
        "execution_id": failed.get("execution_id"),
        "fingerprint_short": fingerprint_short,
        "idempotent_replay": False,
        "error_code": error_code,
        "error_stage": error_stage,
        "artifact_type": artifact_type,
        "retryable": bool(retryable),
        "message": message,
        "result": None,
        "billing_status": BILLING_STATUS_NOT_STARTED,
        "failed_artifact": failed.get("failed_artifact"),
    }
    if raise_as_http:
        raise AgenteComparaCalculationExecutionError(error_code, message, http_status=500)
    return payload
