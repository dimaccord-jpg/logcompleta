"""Correcoes assistidas da Cleide Auditoria.

Preview usa estado temporario server-side; apply persiste uma unica vez no
registro de tabela temporaria e guarda snapshot para desfazer.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4


ERROR_CORRECTION_INVALID_PAYLOAD = "cleide_audit_correction_invalid_payload"
ERROR_CORRECTION_NO_TEMP_TABLE = "cleide_audit_correction_no_temp_table"
ERROR_CORRECTION_SUGGESTION_NOT_FOUND = "cleide_audit_correction_suggestion_not_found"
ERROR_CORRECTION_CONSTRAINT_MISMATCH = "cleide_audit_correction_constraint_mismatch"
ERROR_CORRECTION_UNSUPPORTED_TRANSFORMATION = "cleide_audit_correction_unsupported_transformation"
ERROR_CORRECTION_PREVIEW_NOT_FOUND = "cleide_audit_correction_preview_not_found"
ERROR_CORRECTION_PREVIEW_EXPIRED = "cleide_audit_correction_preview_expired"
ERROR_CORRECTION_PREVIEW_NOT_SAFE = "cleide_audit_correction_preview_not_safe"
ERROR_CORRECTION_UNDO_NOT_FOUND = "cleide_audit_correction_undo_not_found"

TRANSFORMATION_SELECT_PRICING_DIMENSION = "select_pricing_dimension"
PREVIEW_TTL_MINUTES = 10
PREVIEW_SESSION_KEY = "cleide_audit_correction_previews"
MAX_SESSION_PREVIEWS = 5
CORRECTION_HISTORY_MAX_ITEMS = 5


class CleideAuditCorrectionError(Exception):
    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _json_fingerprint(payload) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def record_fingerprint(record: dict) -> str:
    if not isinstance(record, dict):
        return ""
    audit_batch = record.get("audit_batch") if isinstance(record.get("audit_batch"), dict) else {}
    return _json_fingerprint(
        {
            "version_marker": record.get("version_marker"),
            "edit_version": record.get("edit_version") or 0,
            "source_documents": list(record.get("source_documents") or []),
            "temp_table_id": record.get("temp_table_id"),
            "freight_tables": record.get("freight_tables") or [],
            "freight_routes": record.get("freight_routes") or [],
            "coverage_table": record.get("coverage_table") or {},
            "accessorial_fees": record.get("accessorial_fees") or [],
            "audit_batch_id": audit_batch.get("audit_batch_id"),
            "normalized_rows": audit_batch.get("normalized_rows") or [],
        }
    )


def _safe_strings(values, limit: int = 50) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        if value is None:
            continue
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
        if len(cleaned) >= limit:
            break
    return cleaned


def _suggestion_id(record: dict, group: dict) -> str:
    audit_batch = record.get("audit_batch") if isinstance(record.get("audit_batch"), dict) else {}
    return "sug_" + _json_fingerprint(
        {
            "fingerprint": record_fingerprint(record),
            "audit_batch_id": audit_batch.get("audit_batch_id"),
            "diagnostic_code": group.get("code"),
            "table_refs": group.get("table_refs") or [],
            "candidate_column": group.get("candidate_column"),
        }
    )[:24]


def build_audit_correction_suggestions(record: dict, audit_diagnostics: dict) -> list[dict]:
    groups = audit_diagnostics.get("groups") if isinstance(audit_diagnostics, dict) else []
    if not isinstance(groups, list):
        return []
    audit_batch = record.get("audit_batch") if isinstance(record.get("audit_batch"), dict) else {}
    suggestions: list[dict] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        if group.get("code") != "pricing_dimension_mismatch":
            continue
        table_refs = _safe_strings(group.get("table_refs"), limit=10)
        current_values = _safe_strings(group.get("available_values"))
        candidate_values = _safe_strings(group.get("candidate_values"))
        requested_values = _safe_strings(group.get("requested_values"))
        affected_rows = int(group.get("affected_rows") or 0)
        exact_match = bool(group.get("exact_candidate_match"))
        ambiguous = bool(group.get("ambiguous"))
        if (
            group.get("confidence") != "high"
            or not table_refs
            or not group.get("current_column")
            or not group.get("candidate_column")
            or not current_values
            or not candidate_values
            or not requested_values
            or not exact_match
            or ambiguous
            or affected_rows <= 0
        ):
            continue
        suggestion = {
            "suggestion_id": _suggestion_id(record, group),
            "diagnostic_code": "pricing_dimension_mismatch",
            "transformation": {
                "type": TRANSFORMATION_SELECT_PRICING_DIMENSION,
                "target": {
                    "scope": "freight_tables",
                    "table_refs": table_refs,
                },
                "parameters": {
                    "current_column": str(group.get("current_column") or "").strip(),
                    "candidate_column": str(group.get("candidate_column")).strip(),
                    "current_values": current_values,
                    "candidate_values": candidate_values,
                },
            },
            "confidence": "high",
            "evidence": {
                "requested_values": requested_values,
                "available_values": current_values,
                "affected_rows": affected_rows,
                "exact_candidate_match": True,
                "ambiguous": False,
            },
            "constraints": {
                "version_marker": record.get("version_marker"),
                "edit_version": record.get("edit_version") or 0,
                "source_documents": list(record.get("source_documents") or []),
                "audit_batch_id": audit_batch.get("audit_batch_id"),
                "record_fingerprint": record_fingerprint(record),
            },
            "allowed_actions": ["preview"],
        }
        suggestions.append(suggestion)
    return suggestions


def _require(condition: bool, error_code: str, message: str) -> None:
    if not condition:
        raise CleideAuditCorrectionError(error_code, message)


def _parse_iso(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _validate_constraints(record: dict, suggestion: dict) -> None:
    audit_batch = record.get("audit_batch") if isinstance(record.get("audit_batch"), dict) else {}
    constraints = suggestion.get("constraints") if isinstance(suggestion.get("constraints"), dict) else {}
    _require(constraints.get("record_fingerprint") == record_fingerprint(record), ERROR_CORRECTION_CONSTRAINT_MISMATCH, "A sugestão não pertence ao estado atual do artefato.")
    _require(constraints.get("version_marker") == record.get("version_marker"), ERROR_CORRECTION_CONSTRAINT_MISMATCH, "Versão do artefato alterada.")
    _require((constraints.get("edit_version") or 0) == (record.get("edit_version") or 0), ERROR_CORRECTION_CONSTRAINT_MISMATCH, "A tabela cadastrada foi editada após a sugestão.")
    _require(list(constraints.get("source_documents") or []) == list(record.get("source_documents") or []), ERROR_CORRECTION_CONSTRAINT_MISMATCH, "Documentos de origem alterados.")
    _require(constraints.get("audit_batch_id") == audit_batch.get("audit_batch_id"), ERROR_CORRECTION_CONSTRAINT_MISMATCH, "Lote auditado alterado.")


def _session_preview_store(session_obj) -> dict:
    store = session_obj.get(PREVIEW_SESSION_KEY)
    if not isinstance(store, dict):
        store = {}
    now = _utcnow()
    cleaned = {
        key: value
        for key, value in store.items()
        if isinstance(value, dict)
        and (_parse_iso(value.get("expires_at")) or now) > now
    }
    session_obj[PREVIEW_SESSION_KEY] = cleaned
    return cleaned


def _store_preview_for_session(session_obj, *, preview: dict, suggestion: dict) -> None:
    store = _session_preview_store(session_obj)
    preview_id = preview.get("preview_id")
    if not preview_id:
        return
    store[preview_id] = {
        "preview_id": preview_id,
        "suggestion_id": suggestion.get("suggestion_id"),
        "expires_at": preview.get("expires_at"),
        "stored_at": _utcnow().isoformat(),
        "safe_to_apply": bool(preview.get("safe_to_apply")),
        "constraints": copy.deepcopy(suggestion.get("constraints") or {}),
        "suggestion": copy.deepcopy(suggestion),
        "preview": copy.deepcopy(preview),
    }
    ordered = sorted(
        store.items(),
        key=lambda item: item[1].get("stored_at") or "",
        reverse=True,
    )
    session_obj[PREVIEW_SESSION_KEY] = dict(ordered[:MAX_SESSION_PREVIEWS])
    session_obj.modified = True


def _load_preview_from_session(session_obj, *, preview_id: str, suggestion_id: str) -> dict:
    store = _session_preview_store(session_obj)
    preview_ref = store.get(preview_id)
    _require(isinstance(preview_ref, dict), ERROR_CORRECTION_PREVIEW_NOT_FOUND, "Preview não encontrado. Execute uma nova simulação.")
    _require(preview_ref.get("suggestion_id") == suggestion_id, ERROR_CORRECTION_CONSTRAINT_MISMATCH, "Preview e sugestão não correspondem.")
    expires_at = _parse_iso(preview_ref.get("expires_at"))
    _require(expires_at is not None and expires_at > _utcnow(), ERROR_CORRECTION_PREVIEW_EXPIRED, "Preview expirado. Execute uma nova simulação.")
    _require(bool(preview_ref.get("safe_to_apply")), ERROR_CORRECTION_PREVIEW_NOT_SAFE, "Preview não está seguro para aplicação.")
    return preview_ref


def _table_index_from_ref(table_ref: str) -> int | None:
    if not isinstance(table_ref, str):
        return None
    prefix = "freight_tables["
    if not table_ref.startswith(prefix) or not table_ref.endswith("]"):
        return None
    raw = table_ref[len(prefix):-1]
    if not raw.isdigit():
        return None
    return int(raw)


def apply_select_pricing_dimension(record: dict, transformation: dict) -> dict:
    transformed = copy.deepcopy(record)
    target = transformation.get("target") if isinstance(transformation.get("target"), dict) else {}
    parameters = transformation.get("parameters") if isinstance(transformation.get("parameters"), dict) else {}
    candidate_column = str(parameters.get("candidate_column") or "").strip()
    table_refs = target.get("table_refs") if isinstance(target.get("table_refs"), list) else []
    _require(candidate_column != "", ERROR_CORRECTION_INVALID_PAYLOAD, "Coluna candidata ausente.")
    _require(len(table_refs) == 1, ERROR_CORRECTION_INVALID_PAYLOAD, "Transformação exige uma tabela alvo.")
    table_index = _table_index_from_ref(table_refs[0])
    freight_tables = transformed.get("freight_tables") if isinstance(transformed.get("freight_tables"), list) else []
    _require(table_index is not None and 0 <= table_index < len(freight_tables), ERROR_CORRECTION_INVALID_PAYLOAD, "Tabela alvo não encontrada.")
    table = freight_tables[table_index]
    _require(isinstance(table, dict), ERROR_CORRECTION_INVALID_PAYLOAD, "Tabela alvo inválida.")
    columns = table.get("columns") if isinstance(table.get("columns"), list) else []
    _require(candidate_column in columns, ERROR_CORRECTION_INVALID_PAYLOAD, "Coluna candidata não existe na tabela alvo.")
    table["_selected_pricing_dimension_column"] = candidate_column
    return transformed


def _diagnostic_totals(audit_diagnostics: dict | None) -> dict:
    groups = audit_diagnostics.get("groups") if isinstance(audit_diagnostics, dict) else []
    return {
        "total_errors": (audit_diagnostics or {}).get("total_errors") or 0,
        "groups": {
            str(group.get("code")): int(group.get("affected_rows") or 0)
            for group in groups or []
            if isinstance(group, dict) and group.get("code")
        },
    }


def _is_error_status(status: str | None) -> bool:
    return status not in {"ok", "divergent"}


def _rows_by_index(results: list[dict]) -> dict:
    return {row.get("row_index"): row for row in results if isinstance(row, dict)}


def _compact_row(row: dict | None) -> dict:
    row = row or {}
    return {
        "row_index": row.get("row_index"),
        "document_number": row.get("numero_documento"),
        "status": row.get("status"),
        "reason_code": row.get("reason_code"),
        "freight_region": row.get("freight_region"),
        "expected_freight": row.get("expected_freight"),
        "divergence_value": row.get("divergence_value"),
    }


def _compare_outputs(before: dict, after: dict) -> dict:
    before_rows = _rows_by_index(before.get("results") or [])
    after_rows = _rows_by_index(after.get("results") or [])
    regressions: list[dict] = []
    sample_changes: list[dict] = []
    remaining_errors: list[dict] = []
    resolved_errors = new_errors = changed_rows = new_ok = new_divergent = 0
    valid_disappeared = False
    for row_index, before_row in before_rows.items():
        after_row = after_rows.get(row_index)
        if after_row is None:
            if before_row.get("status") in {"ok", "divergent"}:
                valid_disappeared = True
            continue
        before_status = before_row.get("status")
        after_status = after_row.get("status")
        before_error = _is_error_status(before_status)
        after_error = _is_error_status(after_status)
        if before_error and not after_error:
            resolved_errors += 1
        if not before_error and after_error:
            new_errors += 1
            regressions.append({"type": "valid_row_became_error", "before": _compact_row(before_row), "after": _compact_row(after_row)})
        if before_status != after_status or before_row.get("expected_freight") != after_row.get("expected_freight"):
            changed_rows += 1
            if len(sample_changes) < 10:
                sample_changes.append({"before": _compact_row(before_row), "after": _compact_row(after_row)})
        if before_status != "ok" and after_status == "ok":
            new_ok += 1
        if before_status != "divergent" and after_status == "divergent":
            new_divergent += 1
        if after_error and len(remaining_errors) < 10:
            remaining_errors.append(_compact_row(after_row))
    if valid_disappeared:
        regressions.append({"type": "valid_row_disappeared"})
    remaining_error_count = sum(1 for row in after_rows.values() if _is_error_status(row.get("status")))
    return {
        "delta": {
            "resolved_errors": resolved_errors,
            "remaining_errors": remaining_error_count,
            "new_errors": new_errors,
            "changed_rows": changed_rows,
            "new_ok": new_ok,
            "new_divergent": new_divergent,
        },
        "regressions": regressions[:10],
        "sample_changes": sample_changes,
        "remaining_errors": remaining_errors,
    }


def _repeatable(record: dict, transformed: dict, normalized_rows: list[dict]) -> bool:
    from app.cleide_audit_doc_service import compute_audit_outputs

    first = compute_audit_outputs(transformed, normalized_rows)
    second = compute_audit_outputs(transformed, normalized_rows)
    first_projection = [(row.get("row_index"), row.get("status"), row.get("expected_freight"), row.get("divergence_value")) for row in first.get("results") or []]
    second_projection = [(row.get("row_index"), row.get("status"), row.get("expected_freight"), row.get("divergence_value")) for row in second.get("results") or []]
    return first.get("summary") == second.get("summary") and first_projection == second_projection


def preview_audit_correction(record: dict, suggestion: dict) -> dict:
    from app.cleide_audit_doc_service import compute_audit_outputs

    _require(isinstance(record, dict), ERROR_CORRECTION_NO_TEMP_TABLE, "Artefato de auditoria não encontrado.")
    _require(isinstance(suggestion, dict) and suggestion.get("suggestion_id"), ERROR_CORRECTION_INVALID_PAYLOAD, "Sugestão inválida.")
    _validate_constraints(record, suggestion)
    transformation = suggestion.get("transformation") if isinstance(suggestion.get("transformation"), dict) else {}
    _require(transformation.get("type") == TRANSFORMATION_SELECT_PRICING_DIMENSION, ERROR_CORRECTION_UNSUPPORTED_TRANSFORMATION, "Transformação não suportada nesta fase.")
    audit_batch = record.get("audit_batch") if isinstance(record.get("audit_batch"), dict) else {}
    normalized_rows = audit_batch.get("normalized_rows")
    _require(isinstance(normalized_rows, list) and bool(normalized_rows), ERROR_CORRECTION_INVALID_PAYLOAD, "Lote auditado sem linhas normalizadas.")

    before = compute_audit_outputs(record, normalized_rows)
    transformed = apply_select_pricing_dimension(record, transformation)
    after = compute_audit_outputs(transformed, normalized_rows)
    comparison = _compare_outputs(before, after)
    repeatable = _repeatable(record, transformed, normalized_rows)
    safety_reasons: list[str] = []
    if suggestion.get("confidence") != "high":
        safety_reasons.append("confidence_not_high")
    if comparison["delta"]["resolved_errors"] <= comparison["delta"]["new_errors"]:
        safety_reasons.append("no_net_error_reduction")
    if comparison["regressions"]:
        safety_reasons.append("regressions_detected")
    if not repeatable:
        safety_reasons.append("not_repeatable")
    if suggestion.get("evidence", {}).get("ambiguous"):
        safety_reasons.append("ambiguous_candidate")
    safe_to_apply = not safety_reasons
    generated_at = _utcnow()
    expires_at = generated_at + timedelta(minutes=PREVIEW_TTL_MINUTES)
    return {
        "preview_id": "prev_" + uuid4().hex,
        "suggestion_id": suggestion.get("suggestion_id"),
        "generated_at": generated_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "before": {
            "summary": before.get("summary") or {},
            "diagnostic_totals": _diagnostic_totals(before.get("audit_diagnostics")),
        },
        "after": {
            "summary": after.get("summary") or {},
            "diagnostic_totals": _diagnostic_totals(after.get("audit_diagnostics")),
        },
        **comparison,
        "safe_to_apply": safe_to_apply,
        "safety_reasons": safety_reasons,
        "transformation": transformation,
        "confidence": suggestion.get("confidence"),
    }


def _load_current_record_for_session(*, user_scope=None, franquia_scope=None, action_label: str = "preview") -> dict:
    from flask import session
    from app.cleide_audit_doc_service import (
        CleideAuditBatchError,
        ERROR_AUDIT_EXPIRED,
        ERROR_AUDIT_NO_TEMP_TABLE,
        TEMP_TABLE_STATUS_DISCARDED,
        TEMP_TABLE_STATUS_EXPIRED,
        TEMP_TABLE_STATUS_PROCESSING,
        _assert_temp_table_scope,
        clear_temp_table_session_refs,
        get_temp_table_id,
        load_temp_table_record,
        sync_temp_table_with_session_documents,
    )
    from app.services.cleiton_doc_config_service import get_cleiton_doc_config

    sync_temp_table_with_session_documents()
    active_id = get_temp_table_id(session)
    if not active_id:
        raise CleideAuditCorrectionError(ERROR_CORRECTION_NO_TEMP_TABLE, "Nenhuma tabela temporária ativa nesta sessão.")
    cfg = get_cleiton_doc_config()
    record = load_temp_table_record(active_id, ttl_hours=cfg.upload_ttl_hours)
    if record is None:
        clear_temp_table_session_refs(session)
        raise CleideAuditCorrectionError(ERROR_CORRECTION_NO_TEMP_TABLE, "Tabela temporária ativa não encontrada.")
    status = (record.get("status") or "").strip().lower()
    if status == TEMP_TABLE_STATUS_EXPIRED:
        raise CleideAuditCorrectionError(ERROR_AUDIT_EXPIRED, "A tabela temporária desta sessão expirou.")
    if status in {TEMP_TABLE_STATUS_DISCARDED, TEMP_TABLE_STATUS_PROCESSING}:
        raise CleideAuditCorrectionError(ERROR_AUDIT_NO_TEMP_TABLE, f"Tabela temporária indisponível para {action_label}.")
    try:
        _assert_temp_table_scope(record, user_scope=user_scope, franquia_scope=franquia_scope)
    except CleideAuditBatchError as exc:
        raise CleideAuditCorrectionError(exc.error_code, exc.message) from exc
    return record


def _suggestion_for_current_record(record: dict, suggestion_id: str) -> dict:
    audit_batch = record.get("audit_batch") if isinstance(record.get("audit_batch"), dict) else {}
    diagnostics = audit_batch.get("audit_diagnostics") if isinstance(audit_batch.get("audit_diagnostics"), dict) else {}
    suggestions = build_audit_correction_suggestions(record, diagnostics)
    suggestion = next((item for item in suggestions if item.get("suggestion_id") == suggestion_id), None)
    if suggestion is None:
        raise CleideAuditCorrectionError(ERROR_CORRECTION_SUGGESTION_NOT_FOUND, "Sugestão não encontrada para o artefato atual.")
    return suggestion


def preview_audit_correction_for_session(suggestion_id: str, *, user_scope=None, franquia_scope=None) -> dict:
    from flask import session

    record = _load_current_record_for_session(
        user_scope=user_scope,
        franquia_scope=franquia_scope,
        action_label="preview",
    )
    suggestion = _suggestion_for_current_record(record, suggestion_id)
    preview = preview_audit_correction(record, suggestion)
    _store_preview_for_session(session, preview=preview, suggestion=suggestion)
    return preview


def _audit_batch_with_outputs(audit_batch: dict, outputs: dict, *, now: str, expires_at) -> dict:
    updated_batch = dict(audit_batch)
    updated_batch["status"] = "processed"
    updated_batch["results"] = outputs.get("results") or []
    updated_batch["summary"] = outputs.get("summary") or {}
    updated_batch["audit_diagnostics"] = outputs.get("audit_diagnostics")
    updated_batch["updated_at"] = now
    updated_batch["processed_at"] = now
    updated_batch["expires_at"] = audit_batch.get("expires_at") or expires_at
    return updated_batch


def _correction_snapshot(record: dict) -> dict:
    return {
        "temp_table_id": record.get("temp_table_id"),
        "freight_tables": copy.deepcopy(record.get("freight_tables") or []),
        "freight_routes": copy.deepcopy(record.get("freight_routes") or []),
        "accessorial_fees": copy.deepcopy(record.get("accessorial_fees") or []),
        "coverage_table": copy.deepcopy(record.get("coverage_table") or None),
        "audit_batch": copy.deepcopy(record.get("audit_batch") or None),
        "human_review_status": record.get("human_review_status"),
        "human_edited_at": record.get("human_edited_at"),
        "human_edited_by_user_id": record.get("human_edited_by_user_id"),
        "edit_version": record.get("edit_version") or 0,
        "updated_at": record.get("updated_at"),
        "expires_at": record.get("expires_at"),
        "correction_history": copy.deepcopy(record.get("correction_history") or []),
    }


def _next_edit_version(record: dict) -> int:
    current = record.get("edit_version")
    if isinstance(current, int) and current >= 0:
        return current + 1
    if isinstance(current, str) and current.strip().isdigit():
        return int(current.strip()) + 1
    return 1


def _assert_preview_matches_recomputed_after(stored_preview: dict, after_outputs: dict) -> None:
    preview_after = stored_preview.get("after") if isinstance(stored_preview.get("after"), dict) else {}
    expected_summary = preview_after.get("summary") if isinstance(preview_after.get("summary"), dict) else {}
    _require(
        expected_summary == (after_outputs.get("summary") or {}),
        ERROR_CORRECTION_CONSTRAINT_MISMATCH,
        "O resultado mudou desde a simulação. Execute uma nova simulação.",
    )


def apply_audit_correction_for_session(
    *,
    preview_id: str,
    suggestion_id: str,
    user_scope=None,
    franquia_scope=None,
) -> dict:
    from flask import session
    from app.cleide_audit_doc_service import compute_audit_outputs, save_temp_table_record, _public_temp_table

    record = _load_current_record_for_session(
        user_scope=user_scope,
        franquia_scope=franquia_scope,
        action_label="aplicação",
    )
    preview_ref = _load_preview_from_session(
        session,
        preview_id=preview_id,
        suggestion_id=suggestion_id,
    )
    suggestion = copy.deepcopy(preview_ref.get("suggestion") or {})
    _require(suggestion.get("suggestion_id") == suggestion_id, ERROR_CORRECTION_CONSTRAINT_MISMATCH, "Sugestão inválida para este preview.")
    _validate_constraints(record, suggestion)
    current_suggestion = _suggestion_for_current_record(record, suggestion_id)
    _require(
        current_suggestion.get("transformation") == suggestion.get("transformation"),
        ERROR_CORRECTION_CONSTRAINT_MISMATCH,
        "A sugestão mudou desde a simulação. Execute uma nova simulação.",
    )

    stored_preview = preview_ref.get("preview") if isinstance(preview_ref.get("preview"), dict) else {}
    _require(bool(stored_preview.get("safe_to_apply")), ERROR_CORRECTION_PREVIEW_NOT_SAFE, "A simulação não está segura para aplicação.")
    audit_batch = record.get("audit_batch") if isinstance(record.get("audit_batch"), dict) else {}
    normalized_rows = audit_batch.get("normalized_rows")
    _require(isinstance(normalized_rows, list) and bool(normalized_rows), ERROR_CORRECTION_INVALID_PAYLOAD, "Lote auditado sem linhas normalizadas.")

    transformed = apply_select_pricing_dimension(record, suggestion.get("transformation") or {})
    after_outputs = compute_audit_outputs(transformed, normalized_rows)
    _assert_preview_matches_recomputed_after(stored_preview, after_outputs)

    now = _utcnow().isoformat()
    application_id = "corr_" + uuid4().hex
    snapshot = _correction_snapshot(record)
    history = list(record.get("correction_history") or [])
    history.append(
        {
            "application_id": application_id,
            "applied_at": now,
            "preview_id": preview_id,
            "suggestion_id": suggestion_id,
            "transformation": copy.deepcopy(suggestion.get("transformation") or {}),
            "constraints": copy.deepcopy(suggestion.get("constraints") or {}),
            "snapshot": snapshot,
        }
    )
    history = history[-CORRECTION_HISTORY_MAX_ITEMS:]

    updated = dict(transformed)
    updated["audit_batch"] = _audit_batch_with_outputs(
        audit_batch,
        after_outputs,
        now=now,
        expires_at=record.get("expires_at"),
    )
    updated["updated_at"] = now
    updated["expires_at"] = record.get("expires_at")
    updated["edit_version"] = _next_edit_version(record)
    updated["human_review_status"] = "edited"
    updated["human_edited_at"] = now
    updated["human_edited_by_user_id"] = user_scope
    updated["correction_history"] = history
    updated["last_correction_application_id"] = application_id
    updated["last_correction_applied_at"] = now

    saved = save_temp_table_record(updated)
    store = _session_preview_store(session)
    store.pop(preview_id, None)
    session[PREVIEW_SESSION_KEY] = store
    session.modified = True
    public = _public_temp_table(saved)
    _require(public is not None, ERROR_CORRECTION_NO_TEMP_TABLE, "Não foi possível retornar a tabela temporária corrigida.")
    return {
        "application_id": application_id,
        "temp_table": public,
    }


def undo_last_audit_correction_for_session(
    *,
    application_id: str | None = None,
    user_scope=None,
    franquia_scope=None,
) -> dict:
    from app.cleide_audit_doc_service import compute_audit_outputs, save_temp_table_record, _public_temp_table

    record = _load_current_record_for_session(
        user_scope=user_scope,
        franquia_scope=franquia_scope,
        action_label="desfazer",
    )
    history = list(record.get("correction_history") or [])
    _require(bool(history), ERROR_CORRECTION_UNDO_NOT_FOUND, "Não há correção assistida para desfazer.")
    last = history[-1]
    if application_id:
        _require(last.get("application_id") == application_id, ERROR_CORRECTION_CONSTRAINT_MISMATCH, "Somente a última correção aplicada pode ser desfeita.")
    snapshot = last.get("snapshot") if isinstance(last.get("snapshot"), dict) else {}
    _require(bool(snapshot), ERROR_CORRECTION_UNDO_NOT_FOUND, "Snapshot da correção não encontrado.")

    restored = dict(record)
    for key in (
        "freight_tables",
        "freight_routes",
        "accessorial_fees",
        "coverage_table",
        "human_review_status",
        "human_edited_at",
        "human_edited_by_user_id",
    ):
        if key in snapshot:
            restored[key] = copy.deepcopy(snapshot.get(key))
    restored["correction_history"] = copy.deepcopy(snapshot.get("correction_history") or history[:-1])
    restored["last_correction_application_id"] = (
        restored["correction_history"][-1].get("application_id")
        if restored["correction_history"]
        else None
    )
    restored["last_correction_applied_at"] = (
        restored["correction_history"][-1].get("applied_at")
        if restored["correction_history"]
        else None
    )

    audit_batch = snapshot.get("audit_batch") if isinstance(snapshot.get("audit_batch"), dict) else {}
    normalized_rows = audit_batch.get("normalized_rows")
    _require(isinstance(normalized_rows, list) and bool(normalized_rows), ERROR_CORRECTION_INVALID_PAYLOAD, "Snapshot sem lote auditado válido.")
    outputs = compute_audit_outputs(restored, normalized_rows)
    now = _utcnow().isoformat()
    restored["audit_batch"] = _audit_batch_with_outputs(
        audit_batch,
        outputs,
        now=now,
        expires_at=record.get("expires_at"),
    )
    restored["updated_at"] = now
    restored["expires_at"] = record.get("expires_at")
    restored["edit_version"] = _next_edit_version(record)

    saved = save_temp_table_record(restored)
    public = _public_temp_table(saved)
    _require(public is not None, ERROR_CORRECTION_NO_TEMP_TABLE, "Não foi possível retornar a tabela temporária restaurada.")
    return {
        "undone_application_id": last.get("application_id"),
        "temp_table": public,
    }
