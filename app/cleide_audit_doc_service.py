"""
Wrapper documental fino do domínio Cleide Auditoria (Fase 1).

Reaproveita preparação, store e configuração governados do Cleiton.
Opera exclusivamente sobre session keys `cleide_audit_*`; não toca sessão da Júlia.
Sem rotas, chat, IA ou billing.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from flask import has_request_context, session
from werkzeug.utils import secure_filename

from app.cleiton_doc_contracts import (
    CONTEXT_KIND_GEMINI_FILE,
    CONTEXT_KIND_PLACEHOLDER,
    CONTEXT_KIND_TEXT,
    DOC_TYPE_CSV,
    DOC_TYPE_DOCX,
    DOC_TYPE_PDF,
    DOC_TYPE_TXT,
    DOC_TYPE_XLSX,
    DOC_TYPE_XML,
    ERROR_DOC_NOT_FOUND,
    ERROR_GEMINI_FILE_UPLOAD,
    ERROR_INVALID_SIZE,
    ERROR_MAX_FILES,
    ERROR_SESSION_BYTES,
    FIELD_CHAR_COUNT,
    FIELD_COLUMN_COUNT,
    FIELD_CONTEXT_KIND,
    FIELD_CONTEXT_REF,
    FIELD_CREATED_AT,
    FIELD_DISPLAY_NAME,
    FIELD_DOC_ID,
    FIELD_DOC_TYPE,
    FIELD_ERROR_CODE,
    FIELD_EXPIRES_AT,
    FIELD_EXTENSION,
    FIELD_GEMINI_FILE_NAME,
    FIELD_GEMINI_FILE_STATE,
    FIELD_GEMINI_FILE_URI,
    FIELD_GEMINI_MIME_TYPE,
    FIELD_GEMINI_UPLOADED_AT,
    FIELD_MAX_DEPTH,
    FIELD_MIME_TYPE,
    FIELD_NODE_COUNT,
    FIELD_PAGE_COUNT,
    FIELD_PDF_CONTEXT_READY,
    FIELD_PREPARED_CONTEXT,
    FIELD_ROW_COUNT,
    FIELD_SAFE_NAME,
    FIELD_SESSION_KEY,
    FIELD_SIZE_BYTES,
    FIELD_SOURCE_AGENT,
    FIELD_STATUS,
    FIELD_TRUNCATED,
    FIELD_WARNINGS,
    STATUS_ACTIVE,
    STATUS_ERROR,
)
from app.cleiton_doc_gemini_files import (
    pdf_context_ready_from_record,
    upload_pdf_to_gemini_files_api,
)
from app.cleiton_doc_prepare import prepare_document
from app.cleiton_doc_service import CleitonDocSessionError, maybe_cleanup_expired_cleiton_docs
from app.cleiton_doc_store import (
    get_cleiton_doc_tmp_dir,
    load_document_record,
    remove_document_record,
    save_document_record,
)
from app.services.cleiton_doc_config_service import get_cleiton_doc_config

logger = logging.getLogger(__name__)

CLEIDE_AUDIT_DOMAIN = "cleide_audit"

CLEIDE_AUDIT_DOC_IDS_SESSION_KEY = "cleide_audit_doc_ids"
CLEIDE_AUDIT_DOC_CONTEXT_SESSION_KEY = "cleide_audit_doc_context"
CLEIDE_AUDIT_CHAT_HISTORY_SESSION_KEY = "cleide_audit_chat_history"
CLEIDE_AUDIT_UPLOAD_LOCK_SESSION_KEY = "cleide_audit_upload_lock"
CLEIDE_AUDIT_LAST_REQUEST_ID_SESSION_KEY = "cleide_audit_last_request_id"
CLEIDE_AUDIT_UPLOAD_IN_PROGRESS_SESSION_KEY = "cleide_audit_upload_in_progress"
CLEIDE_AUDIT_TEMP_TABLE_ID_SESSION_KEY = "cleide_audit_temp_table_id"
CLEIDE_AUDIT_TEMP_TABLE_SOURCE_DOCS_SESSION_KEY = "cleide_audit_temp_table_source_doc_ids"

TEMP_TABLE_STATUS_PROCESSING = "processing"
TEMP_TABLE_STATUS_AWAITING_VALIDATION = "awaiting_validation"
TEMP_TABLE_STATUS_VALIDATED = "validated"
TEMP_TABLE_STATUS_NEEDS_REVIEW = "needs_review"
TEMP_TABLE_STATUS_FAILED = "failed"
TEMP_TABLE_STATUS_EXPIRED = "expired"
TEMP_TABLE_STATUS_DISCARDED = "discarded"

TEMP_TABLE_VERSION_MARKER = "cleide_audit_temp_table_v1"
TEMP_TABLE_OPERATIONAL_OWNER = "cleiton"
TEMP_TABLE_UI_DISPLAY_NAME = "Tabela temporária extraída"
TEMP_TABLE_JSON_BEGIN = "---CLEIDE_TEMP_TABLE---"
TEMP_TABLE_JSON_END = "---END_CLEIDE_TEMP_TABLE---"

CLEIDE_AUDIT_DOCUMENT_UPLOAD_FLOW_TYPE = "cleide_audit_document_upload"
CLEIDE_AUDIT_DOCUMENT_PREPARE_FLOW_TYPE = "cleide_audit_document_prepare"
CLEIDE_AUDIT_CHAT_FLOW_TYPE = "cleide_audit_chat"
CLEIDE_AUDIT_TEMP_TABLE_EXTRACTION_FLOW_TYPE = "cleide_audit_temp_table_extraction"

SOURCE_AGENT_CLEIDE_AUDIT = "cleide_audit"


def cleide_audit_upload_idempotency_key(request_id: str) -> str:
    return f"cleide-audit-upload:{(request_id or '').strip()}"


def cleide_audit_upload_doc_idempotency_key(doc_id: str) -> str:
    return f"cleide-audit-upload-doc:{(doc_id or '').strip()}"


def cleide_audit_chat_idempotency_key(request_id: str) -> str:
    return f"cleide-audit-chat:{(request_id or '').strip()}"


def cleide_audit_temp_table_extraction_idempotency_key(source_doc_ids: list[str]) -> str:
    normalized = _normalize_source_doc_ids(source_doc_ids)
    joined = ":".join(normalized)
    return f"cleide-audit-temp-table:{TEMP_TABLE_VERSION_MARKER}:{joined}"


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _require_session() -> None:
    if not has_request_context():
        raise RuntimeError("Sessão documental da Cleide Auditoria requer request context Flask.")


def _mark_session_modified() -> None:
    session.modified = True


def get_cleide_audit_doc_ids(session_obj) -> list[str]:
    raw = session_obj.get(CLEIDE_AUDIT_DOC_IDS_SESSION_KEY)
    if not isinstance(raw, list):
        return []
    ids: list[str] = []
    for item in raw:
        if isinstance(item, str):
            ref = item.strip()
            if ref:
                ids.append(ref)
    return ids


def set_cleide_audit_doc_ids(session_obj, doc_ids: list[str]) -> None:
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in doc_ids or []:
        if not isinstance(item, str):
            continue
        ref = item.strip()
        if not ref or ref in seen:
            continue
        seen.add(ref)
        cleaned.append(ref)
    session_obj[CLEIDE_AUDIT_DOC_IDS_SESSION_KEY] = cleaned


def clear_cleide_audit_doc_ids(session_obj) -> None:
    session_obj.pop(CLEIDE_AUDIT_DOC_IDS_SESSION_KEY, None)


def append_cleide_audit_doc_id(session_obj, doc_id: str) -> None:
    ref = (doc_id or "").strip()
    if not ref:
        raise ValueError("doc_id inválido para sessão documental da Cleide Auditoria.")
    ids = get_cleide_audit_doc_ids(session_obj)
    if ref not in ids:
        ids.append(ref)
    set_cleide_audit_doc_ids(session_obj, ids)


def remove_cleide_audit_doc_id(session_obj, doc_id: str) -> None:
    ref = (doc_id or "").strip()
    if not ref:
        return
    ids = [item for item in get_cleide_audit_doc_ids(session_obj) if item != ref]
    if ids:
        set_cleide_audit_doc_ids(session_obj, ids)
    else:
        clear_cleide_audit_doc_ids(session_obj)


def _parse_size_bytes(size_bytes) -> int:
    if size_bytes is None:
        raise CleitonDocSessionError(
            ERROR_INVALID_SIZE,
            "size_bytes ausente ou inválido para documento Cleide Auditoria.",
        )
    if isinstance(size_bytes, bool):
        raise CleitonDocSessionError(
            ERROR_INVALID_SIZE,
            "size_bytes não numérico para documento Cleide Auditoria.",
        )
    try:
        parsed = int(size_bytes)
    except (TypeError, ValueError):
        raise CleitonDocSessionError(
            ERROR_INVALID_SIZE,
            "size_bytes não numérico para documento Cleide Auditoria.",
        ) from None
    if parsed <= 0:
        raise CleitonDocSessionError(
            ERROR_INVALID_SIZE,
            "size_bytes deve ser maior que zero para documento Cleide Auditoria.",
        )
    return parsed


def _normalize_extension(extension: str) -> str:
    raw = (extension or "").strip().lower()
    if not raw:
        return ""
    return raw if raw.startswith(".") else f".{raw}"


def _build_safe_display_name(display_name: str, extension: str) -> tuple[str, str]:
    raw = (display_name or "").strip() or "documento"
    safe = secure_filename(raw) or "documento"
    ext = _normalize_extension(extension)
    if ext and not safe.lower().endswith(ext):
        safe_name = f"{safe}{ext}"
    else:
        safe_name = safe
        if not ext and "." in safe_name:
            ext = "." + safe_name.rsplit(".", 1)[-1].lower()
    return raw, safe_name


def _context_ref_for_kind(context_kind: str, doc_id: str) -> str:
    kind = (context_kind or CONTEXT_KIND_PLACEHOLDER).strip() or CONTEXT_KIND_PLACEHOLDER
    if kind == CONTEXT_KIND_TEXT:
        return f"text:{doc_id}"
    if kind == CONTEXT_KIND_GEMINI_FILE:
        return f"gemini_file:{doc_id}"
    return f"placeholder:{doc_id}"


def _public_record(record: dict) -> dict:
    pdf_ready = pdf_context_ready_from_record(record)
    return {
        FIELD_DOC_ID: record.get(FIELD_DOC_ID),
        FIELD_DOC_TYPE: record.get(FIELD_DOC_TYPE),
        FIELD_DISPLAY_NAME: record.get(FIELD_DISPLAY_NAME),
        FIELD_SAFE_NAME: record.get(FIELD_SAFE_NAME),
        FIELD_EXTENSION: record.get(FIELD_EXTENSION),
        FIELD_MIME_TYPE: record.get(FIELD_MIME_TYPE),
        FIELD_SIZE_BYTES: record.get(FIELD_SIZE_BYTES),
        FIELD_CREATED_AT: record.get(FIELD_CREATED_AT),
        FIELD_EXPIRES_AT: record.get(FIELD_EXPIRES_AT),
        FIELD_STATUS: record.get(FIELD_STATUS),
        FIELD_TRUNCATED: record.get(FIELD_TRUNCATED),
        FIELD_CONTEXT_KIND: record.get(FIELD_CONTEXT_KIND),
        FIELD_CONTEXT_REF: record.get(FIELD_CONTEXT_REF),
        FIELD_CHAR_COUNT: record.get(FIELD_CHAR_COUNT),
        FIELD_ROW_COUNT: record.get(FIELD_ROW_COUNT),
        FIELD_COLUMN_COUNT: record.get(FIELD_COLUMN_COUNT),
        FIELD_PAGE_COUNT: record.get(FIELD_PAGE_COUNT),
        FIELD_NODE_COUNT: record.get(FIELD_NODE_COUNT),
        FIELD_MAX_DEPTH: record.get(FIELD_MAX_DEPTH),
        FIELD_WARNINGS: record.get(FIELD_WARNINGS) or [],
        FIELD_SESSION_KEY: record.get(FIELD_SESSION_KEY),
        FIELD_ERROR_CODE: record.get(FIELD_ERROR_CODE),
        FIELD_PDF_CONTEXT_READY: pdf_ready,
    }


def get_allowed_document_formats() -> list[dict]:
    """Retorna formatos habilitados a partir da config central do Cleiton."""
    cfg = get_cleiton_doc_config()
    catalog = [
        (DOC_TYPE_TXT, ".txt", cfg.txt_enabled),
        (DOC_TYPE_XML, ".xml", cfg.xml_enabled),
        (DOC_TYPE_CSV, ".csv", cfg.csv_enabled),
        (DOC_TYPE_XLSX, ".xlsx", cfg.excel_enabled),
        (DOC_TYPE_DOCX, ".docx", cfg.docx_enabled),
        (DOC_TYPE_PDF, ".pdf", cfg.pdf_enabled),
    ]
    return [
        {"doc_type": doc_type, "extension": extension, "enabled": bool(enabled)}
        for doc_type, extension, enabled in catalog
        if enabled
    ]


def cleanup_expired_documents_for_session() -> int:
    _require_session()
    cfg = get_cleiton_doc_config()
    removed = 0
    stale_ids: list[str] = []

    for doc_id in get_cleide_audit_doc_ids(session):
        record = load_document_record(doc_id, ttl_hours=cfg.upload_ttl_hours)
        if record is None:
            stale_ids.append(doc_id)
            removed += 1

    for doc_id in stale_ids:
        remove_document_record(doc_id)
        remove_cleide_audit_doc_id(session, doc_id)

    if stale_ids:
        _mark_session_modified()
    return removed


def get_active_documents_for_session() -> list[dict]:
    _require_session()
    cfg = get_cleiton_doc_config()
    active: list[dict] = []
    stale_ids: list[str] = []

    for doc_id in get_cleide_audit_doc_ids(session):
        record = load_document_record(doc_id, ttl_hours=cfg.upload_ttl_hours)
        if record is None:
            stale_ids.append(doc_id)
            continue
        active.append(_public_record(record))

    if stale_ids:
        for doc_id in stale_ids:
            remove_cleide_audit_doc_id(session, doc_id)
        _mark_session_modified()

    return active


def get_document_session_totals() -> dict:
    _require_session()
    cfg = get_cleiton_doc_config()
    cleanup_expired_documents_for_session()
    active = get_active_documents_for_session()
    total_bytes = sum(int(item.get(FIELD_SIZE_BYTES) or 0) for item in active)
    active_count = len(active)
    max_files = int(cfg.max_files_per_session)
    max_bytes = int(cfg.session_max_bytes)
    return {
        "active_count": active_count,
        "total_bytes": total_bytes,
        "max_files_per_session": max_files,
        "session_max_bytes": max_bytes,
        "remaining_files": max(0, max_files - active_count),
        "remaining_bytes": max(0, max_bytes - total_bytes),
    }


def assert_session_can_accept_document(size_bytes) -> None:
    _require_session()
    cfg = get_cleiton_doc_config()
    incoming = _parse_size_bytes(size_bytes)

    if incoming > int(cfg.session_max_bytes):
        raise CleitonDocSessionError(
            ERROR_INVALID_SIZE,
            "size_bytes excede o limite total configurado da sessão documental.",
        )

    totals = get_document_session_totals()

    if totals["active_count"] >= int(cfg.max_files_per_session):
        raise CleitonDocSessionError(
            ERROR_MAX_FILES,
            "Limite de arquivos documentais por sessão atingido.",
        )

    projected = int(totals["total_bytes"]) + incoming
    if projected > int(cfg.session_max_bytes):
        raise CleitonDocSessionError(
            ERROR_SESSION_BYTES,
            "Limite total de bytes documentais da sessão excedido.",
        )


def _register_document_record(
    *,
    display_name: str,
    extension: str,
    mime_type: str,
    size_bytes: int,
    context_kind: str = CONTEXT_KIND_PLACEHOLDER,
    context_ref: str | None = None,
    truncated: bool = False,
    doc_type: str | None = None,
    prepared_context: str | None = None,
    char_count: int | None = None,
    row_count: int | None = None,
    column_count: int | None = None,
    page_count: int | None = None,
    node_count: int | None = None,
    max_depth: int | None = None,
    warnings: list[str] | None = None,
    status: str = STATUS_ACTIVE,
    error_code: str | None = None,
    gemini_file_name: str | None = None,
    gemini_file_uri: str | None = None,
    gemini_mime_type: str | None = None,
    gemini_file_state: str | None = None,
    gemini_uploaded_at: str | None = None,
) -> dict:
    _require_session()
    cfg = get_cleiton_doc_config()
    validated_size = _parse_size_bytes(size_bytes)
    assert_session_can_accept_document(validated_size)

    doc_id = uuid4().hex
    display, safe_name = _build_safe_display_name(display_name, extension)
    ext = _normalize_extension(extension) or (
        f".{safe_name.rsplit('.', 1)[-1].lower()}" if "." in safe_name else ""
    )
    created_at = _utcnow()
    expires_at = created_at + timedelta(hours=max(1, int(cfg.upload_ttl_hours)))
    resolved_kind = (context_kind or CONTEXT_KIND_PLACEHOLDER).strip() or CONTEXT_KIND_PLACEHOLDER

    record = {
        FIELD_DOC_ID: doc_id,
        FIELD_DOC_TYPE: doc_type,
        FIELD_DISPLAY_NAME: display,
        FIELD_SAFE_NAME: safe_name,
        FIELD_EXTENSION: ext,
        FIELD_MIME_TYPE: (mime_type or "application/octet-stream").strip(),
        FIELD_SIZE_BYTES: validated_size,
        FIELD_CREATED_AT: created_at.isoformat(),
        FIELD_EXPIRES_AT: expires_at.isoformat(),
        FIELD_STATUS: (status or STATUS_ACTIVE).strip() or STATUS_ACTIVE,
        FIELD_TRUNCATED: bool(truncated),
        FIELD_CONTEXT_KIND: resolved_kind,
        FIELD_CONTEXT_REF: context_ref or _context_ref_for_kind(resolved_kind, doc_id),
        FIELD_PREPARED_CONTEXT: prepared_context,
        FIELD_CHAR_COUNT: char_count,
        FIELD_ROW_COUNT: row_count,
        FIELD_COLUMN_COUNT: column_count,
        FIELD_PAGE_COUNT: page_count,
        FIELD_NODE_COUNT: node_count,
        FIELD_MAX_DEPTH: max_depth,
        FIELD_WARNINGS: list(warnings or []),
        FIELD_SOURCE_AGENT: SOURCE_AGENT_CLEIDE_AUDIT,
        FIELD_SESSION_KEY: CLEIDE_AUDIT_DOC_IDS_SESSION_KEY,
        FIELD_ERROR_CODE: error_code,
        FIELD_GEMINI_FILE_NAME: gemini_file_name,
        FIELD_GEMINI_FILE_URI: gemini_file_uri,
        FIELD_GEMINI_MIME_TYPE: gemini_mime_type,
        FIELD_GEMINI_FILE_STATE: gemini_file_state,
        FIELD_GEMINI_UPLOADED_AT: gemini_uploaded_at,
    }

    save_document_record(record)
    append_cleide_audit_doc_id(session, doc_id)
    _mark_session_modified()
    return _public_record(record)


def prepare_and_register_document(
    *,
    display_name: str,
    file_bytes: bytes,
    mime_type: str | None = None,
    extension: str | None = None,
) -> dict:
    """
    Valida, prepara e registra documento na sessão Cleide Auditoria após sucesso.

    Delega validação/preparação ao Cleiton; persiste IDs em `cleide_audit_doc_ids`.
    """
    prepared = prepare_document(
        display_name=display_name,
        file_bytes=file_bytes,
        mime_type=mime_type,
        extension=extension,
    )

    register_kwargs = {
        "display_name": prepared["display_name"],
        "extension": prepared[FIELD_EXTENSION],
        "mime_type": prepared[FIELD_MIME_TYPE],
        "size_bytes": prepared[FIELD_SIZE_BYTES],
        "context_kind": prepared[FIELD_CONTEXT_KIND],
        "truncated": prepared[FIELD_TRUNCATED],
        "doc_type": prepared[FIELD_DOC_TYPE],
        "prepared_context": prepared[FIELD_PREPARED_CONTEXT],
        "char_count": prepared[FIELD_CHAR_COUNT],
        "row_count": prepared[FIELD_ROW_COUNT],
        "column_count": prepared[FIELD_COLUMN_COUNT],
        "page_count": prepared[FIELD_PAGE_COUNT],
        "node_count": prepared[FIELD_NODE_COUNT],
        "max_depth": prepared[FIELD_MAX_DEPTH],
        "warnings": prepared[FIELD_WARNINGS],
    }

    if prepared[FIELD_CONTEXT_KIND] == CONTEXT_KIND_GEMINI_FILE:
        cfg = get_cleiton_doc_config()
        upload_result = upload_pdf_to_gemini_files_api(
            file_bytes=file_bytes,
            mime_type=prepared[FIELD_MIME_TYPE],
            display_name=prepared["display_name"],
            page_count=prepared[FIELD_PAGE_COUNT],
            max_pages=int(cfg.pdf_max_pages),
        )
        register_kwargs["prepared_context"] = upload_result.prepared_context or prepared[FIELD_PREPARED_CONTEXT]
        register_kwargs["warnings"] = list(prepared[FIELD_WARNINGS]) + list(upload_result.warnings or [])
        if upload_result.ok:
            register_kwargs.update(
                {
                    "status": STATUS_ACTIVE,
                    "gemini_file_name": upload_result.gemini_file_name,
                    "gemini_file_uri": upload_result.gemini_file_uri,
                    "gemini_mime_type": upload_result.gemini_mime_type,
                    "gemini_file_state": upload_result.gemini_file_state,
                    "gemini_uploaded_at": upload_result.gemini_uploaded_at,
                }
            )
        else:
            register_kwargs.update(
                {
                    "status": STATUS_ERROR,
                    "error_code": ERROR_GEMINI_FILE_UPLOAD,
                    "gemini_file_name": upload_result.gemini_file_name,
                    "gemini_file_uri": upload_result.gemini_file_uri,
                    "gemini_mime_type": upload_result.gemini_mime_type,
                    "gemini_file_state": upload_result.gemini_file_state,
                }
            )
            logger.warning(
                "Cleide audit doc: upload Gemini Files API falhou para PDF (summary=%s).",
                upload_result.error_summary,
            )

    return _register_document_record(**register_kwargs)


def remove_document_from_session(doc_id: str) -> dict:
    _require_session()
    ref = (doc_id or "").strip()
    if not ref:
        return {
            "ok": False,
            "doc_id": doc_id,
            "removed_from_store": False,
            "removed_from_session": False,
            "error_code": ERROR_DOC_NOT_FOUND,
        }

    store_result = remove_document_record(ref)
    had_session_ref = ref in get_cleide_audit_doc_ids(session)
    if had_session_ref:
        remove_cleide_audit_doc_id(session, ref)
        _mark_session_modified()

    invalidate_temp_table_if_source_changed(
        reason=TEMP_TABLE_STATUS_DISCARDED,
        removed_doc_id=ref,
    )

    return {
        "ok": True,
        "doc_id": ref,
        "removed_from_store": bool(store_result.get("removed")),
        "removed_from_session": had_session_ref,
        "error_code": None if had_session_ref or store_result.get("removed") else ERROR_DOC_NOT_FOUND,
    }


def clear_documents_for_session() -> dict:
    _require_session()
    ids = get_cleide_audit_doc_ids(session)
    removed_store = 0
    removed_session = 0
    for doc_id in ids:
        result = remove_document_record(doc_id)
        if result.get("removed"):
            removed_store += 1
        removed_session += 1
    clear_cleide_audit_doc_ids(session)
    invalidate_temp_table_for_session(reason=TEMP_TABLE_STATUS_DISCARDED)
    _mark_session_modified()
    return {
        "ok": True,
        "requested": len(ids),
        "removed_from_store": removed_store,
        "removed_from_session": removed_session,
    }


def _temp_table_filename(temp_table_id: str) -> str:
    ref = (temp_table_id or "").strip()
    if not ref:
        raise ValueError("temp_table_id inválido.")
    return f"tt_{ref}.json"


def _temp_table_path(temp_table_id: str):
    from pathlib import Path

    safe_name = _temp_table_filename(temp_table_id)
    base = Path(get_cleiton_doc_tmp_dir()).resolve()
    candidate = (base / safe_name).resolve()
    if candidate.parent != base:
        raise ValueError("temp_table path inválido.")
    return candidate


def _write_temp_table_atomic(path, payload: dict) -> None:
    from app.cleiton_doc_store import _write_json_atomic

    _write_json_atomic(path, payload)


def _normalize_source_doc_ids(doc_ids: list[str] | None) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in doc_ids or []:
        if not isinstance(item, str):
            continue
        ref = item.strip()
        if not ref or ref in seen:
            continue
        seen.add(ref)
        cleaned.append(ref)
    return cleaned


def get_temp_table_id(session_obj) -> str | None:
    raw = session_obj.get(CLEIDE_AUDIT_TEMP_TABLE_ID_SESSION_KEY)
    if not isinstance(raw, str):
        return None
    ref = raw.strip()
    return ref or None


def set_temp_table_id(session_obj, temp_table_id: str | None) -> None:
    ref = (temp_table_id or "").strip()
    if ref:
        session_obj[CLEIDE_AUDIT_TEMP_TABLE_ID_SESSION_KEY] = ref
    else:
        session_obj.pop(CLEIDE_AUDIT_TEMP_TABLE_ID_SESSION_KEY, None)


def get_temp_table_source_doc_ids(session_obj) -> list[str]:
    raw = session_obj.get(CLEIDE_AUDIT_TEMP_TABLE_SOURCE_DOCS_SESSION_KEY)
    if not isinstance(raw, list):
        return []
    return _normalize_source_doc_ids(raw)


def set_temp_table_source_doc_ids(session_obj, doc_ids: list[str]) -> None:
    session_obj[CLEIDE_AUDIT_TEMP_TABLE_SOURCE_DOCS_SESSION_KEY] = _normalize_source_doc_ids(doc_ids)


def clear_temp_table_session_refs(session_obj) -> None:
    session_obj.pop(CLEIDE_AUDIT_TEMP_TABLE_ID_SESSION_KEY, None)
    session_obj.pop(CLEIDE_AUDIT_TEMP_TABLE_SOURCE_DOCS_SESSION_KEY, None)


def _public_temp_table(record: dict | None) -> dict | None:
    if not record:
        return None
    ui = record.get("ui_visibility") if isinstance(record.get("ui_visibility"), dict) else {}
    return {
        "temp_table_id": record.get("temp_table_id"),
        "status": record.get("status"),
        "source_documents": list(record.get("source_documents") or []),
        "detected_carrier": record.get("detected_carrier"),
        "origins": list(record.get("origins") or []),
        "destinations": list(record.get("destinations") or []),
        "routes": list(record.get("routes") or []),
        "freight_tables": list(record.get("freight_tables") or []),
        "freight_routes": list(record.get("freight_routes") or []),
        "weight_ranges": list(record.get("weight_ranges") or []),
        "freight_values": list(record.get("freight_values") or []),
        "accessorial_fees": list(record.get("accessorial_fees") or []),
        "charge_type_detected": record.get("charge_type_detected"),
        "extracted_items": list(record.get("extracted_items") or []),
        "uncertain_fields": list(record.get("uncertain_fields") or []),
        "reading_alerts": list(record.get("reading_alerts") or []),
        "evidence_refs": list(record.get("evidence_refs") or []),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "expires_at": record.get("expires_at"),
        "session_scope": record.get("session_scope"),
        "franquia_scope": record.get("franquia_scope"),
        "user_scope": record.get("user_scope"),
        "operational_owner": record.get("operational_owner"),
        "ui_visibility": {
            "display_name": ui.get("display_name") or TEMP_TABLE_UI_DISPLAY_NAME,
            "readonly": True,
        },
        "version_marker": record.get("version_marker"),
    }


def load_temp_table_record(temp_table_id: str, *, ttl_hours: int) -> dict | None:
    ref = (temp_table_id or "").strip()
    if not ref:
        return None
    try:
        path = _temp_table_path(ref)
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            remove_temp_table_record(ref)
            return None
    except Exception:
        remove_temp_table_record(ref)
        return None

    expires_at = _parse_iso(payload.get(FIELD_EXPIRES_AT))
    if expires_at is None:
        created_at = _parse_iso(payload.get(FIELD_CREATED_AT))
        if created_at is None:
            remove_temp_table_record(ref)
            return None
        expires_at = created_at + timedelta(hours=max(1, int(ttl_hours)))
    if _utcnow() >= expires_at:
        payload["status"] = TEMP_TABLE_STATUS_EXPIRED
        payload["updated_at"] = _utcnow().isoformat()
        try:
            _write_temp_table_atomic(path, payload)
        except Exception:
            remove_temp_table_record(ref)
            return None
        return payload
    return payload


def save_temp_table_record(record: dict) -> dict:
    _require_session()
    temp_table_id = (record.get("temp_table_id") or uuid4().hex).strip()
    record = dict(record)
    record["temp_table_id"] = temp_table_id
    path = _temp_table_path(temp_table_id)
    _write_temp_table_atomic(path, record)
    set_temp_table_id(session, temp_table_id)
    set_temp_table_source_doc_ids(session, list(record.get("source_documents") or []))
    _mark_session_modified()
    return record


def remove_temp_table_record(temp_table_id: str) -> bool:
    ref = (temp_table_id or "").strip()
    if not ref:
        return False
    try:
        path = _temp_table_path(ref)
    except ValueError:
        return False
    if path.is_file():
        try:
            path.unlink()
        except Exception:
            return False
    return True


def invalidate_temp_table_for_session(*, reason: str = TEMP_TABLE_STATUS_DISCARDED) -> None:
    _require_session()
    temp_table_id = get_temp_table_id(session)
    clear_temp_table_session_refs(session)
    if temp_table_id:
        remove_temp_table_record(temp_table_id)
    _mark_session_modified()


def invalidate_temp_table_if_source_changed(
    *,
    reason: str = TEMP_TABLE_STATUS_DISCARDED,
    removed_doc_id: str | None = None,
) -> None:
    _require_session()
    temp_table_id = get_temp_table_id(session)
    if not temp_table_id:
        return
    cfg = get_cleiton_doc_config()
    record = load_temp_table_record(temp_table_id, ttl_hours=cfg.upload_ttl_hours)
    if record is None:
        clear_temp_table_session_refs(session)
        _mark_session_modified()
        return
    source_docs = list(record.get("source_documents") or [])
    active_ids = set(get_cleide_audit_doc_ids(session))
    if removed_doc_id and removed_doc_id in source_docs:
        invalidate_temp_table_for_session(reason=reason)
        return
    if source_docs and not all(doc_id in active_ids for doc_id in source_docs):
        invalidate_temp_table_for_session(reason=reason)


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo:
            off = dt.utcoffset()
            dt = (dt.replace(tzinfo=None) - off) if off else dt.replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _temp_table_expires_at(source_doc_ids: list[str]) -> str:
    cfg = get_cleiton_doc_config()
    latest: datetime | None = None
    for doc_id in source_doc_ids:
        record = load_document_record(doc_id, ttl_hours=cfg.upload_ttl_hours)
        if record is None:
            continue
        candidate = _parse_iso(record.get(FIELD_EXPIRES_AT))
        if candidate and (latest is None or candidate > latest):
            latest = candidate
    if latest is None:
        latest = _utcnow() + timedelta(hours=max(1, int(cfg.upload_ttl_hours)))
    return latest.isoformat()


def get_active_temp_table_for_session() -> dict | None:
    _require_session()
    sync_temp_table_with_session_documents()
    temp_table_id = get_temp_table_id(session)
    if not temp_table_id:
        return None
    cfg = get_cleiton_doc_config()
    record = load_temp_table_record(temp_table_id, ttl_hours=cfg.upload_ttl_hours)
    if record is None:
        clear_temp_table_session_refs(session)
        _mark_session_modified()
        return None
    status = (record.get("status") or "").strip()
    if status in {TEMP_TABLE_STATUS_DISCARDED, TEMP_TABLE_STATUS_EXPIRED}:
        return _public_temp_table(record)
    return _public_temp_table(record)


def sync_temp_table_with_session_documents() -> None:
    _require_session()
    temp_table_id = get_temp_table_id(session)
    if not temp_table_id:
        return
    cfg = get_cleiton_doc_config()
    record = load_temp_table_record(temp_table_id, ttl_hours=cfg.upload_ttl_hours)
    if record is None:
        clear_temp_table_session_refs(session)
        _mark_session_modified()
        return
    active_ids = set(get_cleide_audit_doc_ids(session))
    source_docs = list(record.get("source_documents") or [])
    if source_docs and not all(doc_id in active_ids for doc_id in source_docs):
        invalidate_temp_table_for_session(reason=TEMP_TABLE_STATUS_DISCARDED)


def should_attempt_temp_table_extraction(session_obj, source_doc_ids: list[str]) -> bool:
    normalized = _normalize_source_doc_ids(source_doc_ids)
    if not normalized:
        return False
    sync_temp_table_with_session_documents()
    temp_table_id = get_temp_table_id(session_obj)
    if not temp_table_id:
        return True
    cfg = get_cleiton_doc_config()
    record = load_temp_table_record(temp_table_id, ttl_hours=cfg.upload_ttl_hours)
    if record is None:
        return True
    bound_sources = _normalize_source_doc_ids(list(record.get("source_documents") or []))
    if bound_sources != normalized:
        return True
    raw_status = record.get("status")
    status = raw_status.strip().lower() if isinstance(raw_status, str) else ""
    if status == TEMP_TABLE_STATUS_PROCESSING:
        return False
    return False


def mark_temp_table_processing(source_doc_ids: list[str], *, user_scope=None, franquia_scope=None) -> dict:
    _require_session()
    normalized = _normalize_source_doc_ids(source_doc_ids)
    now = _utcnow()
    temp_table_id = get_temp_table_id(session) or uuid4().hex
    record = {
        "temp_table_id": temp_table_id,
        "status": TEMP_TABLE_STATUS_PROCESSING,
        "source_documents": normalized,
        "detected_carrier": None,
        "origins": [],
        "destinations": [],
        "routes": [],
        "freight_tables": [],
        "freight_routes": [],
        "weight_ranges": [],
        "freight_values": [],
        "accessorial_fees": [],
        "charge_type_detected": None,
        "extracted_items": [],
        "uncertain_fields": [],
        "reading_alerts": [],
        "evidence_refs": [],
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "expires_at": _temp_table_expires_at(normalized),
        "session_scope": CLEIDE_AUDIT_DOC_IDS_SESSION_KEY,
        "franquia_scope": franquia_scope,
        "user_scope": user_scope,
        "operational_owner": TEMP_TABLE_OPERATIONAL_OWNER,
        "ui_visibility": {
            "display_name": TEMP_TABLE_UI_DISPLAY_NAME,
            "readonly": True,
        },
        "version_marker": TEMP_TABLE_VERSION_MARKER,
    }
    return save_temp_table_record(record)


def temp_table_status_message(status: str) -> str:
    mapping = {
        TEMP_TABLE_STATUS_PROCESSING: (
            "Recebi os anexos e iniciei a estruturação da tabela temporária de frete."
        ),
        TEMP_TABLE_STATUS_AWAITING_VALIDATION: (
            "A tabela temporária foi estruturada e está aguardando sua validação."
        ),
        TEMP_TABLE_STATUS_NEEDS_REVIEW: (
            "A tabela temporária foi gerada. Revise os dados antes de continuar."
        ),
        TEMP_TABLE_STATUS_FAILED: (
            "Não foi possível estruturar a tabela temporária a partir dos anexos enviados."
        ),
        TEMP_TABLE_STATUS_EXPIRED: (
            "A tabela temporária desta sessão expirou."
        ),
        TEMP_TABLE_STATUS_DISCARDED: (
            "Os documentos de origem foram alterados ou removidos, "
            "então a tabela temporária anterior foi invalidada."
        ),
    }
    return mapping.get((status or "").strip(), "")


def _normalize_temp_table_status(raw_status) -> str:
    allowed = {
        TEMP_TABLE_STATUS_AWAITING_VALIDATION,
        TEMP_TABLE_STATUS_NEEDS_REVIEW,
        TEMP_TABLE_STATUS_FAILED,
        TEMP_TABLE_STATUS_VALIDATED,
    }
    if not isinstance(raw_status, str):
        return TEMP_TABLE_STATUS_FAILED
    candidate = raw_status.strip().lower()
    if not candidate or candidate not in allowed:
        return TEMP_TABLE_STATUS_FAILED
    return candidate


def _list_field_from_raw(raw: dict, name: str) -> list:
    value = raw.get(name)
    return list(value) if isinstance(value, list) else []


def _is_useful_freight_value(item) -> bool:
    if isinstance(item, dict):
        label = item.get("label")
        if isinstance(label, str) and label.strip():
            return True
        return item.get("value") is not None
    if isinstance(item, str):
        return bool(item.strip())
    return False


def _is_useful_accessorial_fee(item) -> bool:
    if isinstance(item, dict):
        name = item.get("name")
        if isinstance(name, str) and name.strip():
            return True
        return item.get("value") is not None
    if isinstance(item, str):
        return bool(item.strip())
    return False


def _is_useful_weight_range(item) -> bool:
    if isinstance(item, dict):
        label = item.get("label")
        if isinstance(label, str) and label.strip():
            return True
        if item.get("min_weight") is not None or item.get("max_weight") is not None:
            return True
    if isinstance(item, str):
        return bool(item.strip())
    return False


def _optional_normalized_str(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        candidate = value.strip()
        return candidate or None
    candidate = str(value).strip()
    return candidate or None


def _freight_route_field(item: dict, primary: str, *aliases: str) -> str | None:
    keys = (primary, *aliases)
    for key in keys:
        if key not in item:
            continue
        normalized = _optional_normalized_str(item.get(key))
        if normalized is not None:
            return normalized
    return None


def _normalize_freight_route_item(item) -> dict | None:
    if not isinstance(item, dict):
        return None
    return {
        "origin": _freight_route_field(item, "origin"),
        "destination": _freight_route_field(item, "destination"),
        "freight_type": _freight_route_field(item, "freight_type", "type"),
        "weight_30": _freight_route_field(item, "weight_30", "weight_30kg"),
        "weight_50": _freight_route_field(item, "weight_50", "weight_50kg"),
        "weight_70": _freight_route_field(item, "weight_70", "weight_70kg"),
        "weight_100": _freight_route_field(item, "weight_100", "weight_100kg"),
        "boarding_fee": _freight_route_field(item, "boarding_fee", "taxa_embarque_kg"),
        "freight_value_pct": _freight_route_field(item, "freight_value_pct", "frete_valor_pct"),
        "freight_weight_kg": _freight_route_field(item, "freight_weight_kg", "frete_peso_kg"),
        "notes": _freight_route_field(item, "notes", "observations", "observacoes") or "",
        "evidence_ref": _freight_route_field(item, "evidence_ref"),
        "confidence": _freight_route_field(item, "confidence"),
    }


def _is_useful_freight_route(item) -> bool:
    normalized = _normalize_freight_route_item(item) if isinstance(item, dict) else None
    if not normalized:
        return False
    useful_fields = (
        "origin",
        "destination",
        "freight_type",
        "weight_30",
        "weight_50",
        "weight_70",
        "weight_100",
        "boarding_fee",
        "freight_value_pct",
        "freight_weight_kg",
    )
    return any(normalized.get(field) is not None for field in useful_fields)


def _normalize_freight_routes(raw_routes) -> list[dict]:
    if not isinstance(raw_routes, list):
        return []
    normalized: list[dict] = []
    for item in raw_routes:
        route = _normalize_freight_route_item(item)
        if route is not None:
            normalized.append(route)
    return normalized


def _normalize_freight_table_context(raw_context) -> dict:
    if not isinstance(raw_context, dict):
        return {
            "route_label": None,
            "origin": None,
            "destination": None,
            "customer": None,
            "supplier": None,
            "valid_from": None,
            "valid_to": None,
            "delivery_deadline": None,
        }
    return {
        "route_label": _optional_normalized_str(raw_context.get("route_label")),
        "origin": _optional_normalized_str(raw_context.get("origin")),
        "destination": _optional_normalized_str(raw_context.get("destination")),
        "customer": _optional_normalized_str(raw_context.get("customer")),
        "supplier": _optional_normalized_str(raw_context.get("supplier")),
        "valid_from": _optional_normalized_str(raw_context.get("valid_from")),
        "valid_to": _optional_normalized_str(raw_context.get("valid_to")),
        "delivery_deadline": _optional_normalized_str(raw_context.get("delivery_deadline")),
    }


def _normalize_freight_table_row(item, columns: list[str]) -> dict:
    if not isinstance(item, dict):
        return {}
    normalized: dict = {}
    for col in columns:
        if col in item:
            val = item.get(col)
            if val is None:
                normalized[col] = None
            elif isinstance(val, str):
                normalized[col] = val
            else:
                normalized[col] = str(val)
    for key, val in item.items():
        if key not in normalized and isinstance(key, str) and key.strip():
            if val is None:
                normalized[key] = None
            elif isinstance(val, str):
                normalized[key] = val
            else:
                normalized[key] = str(val)
    return normalized


def _normalize_freight_table_item(item) -> dict | None:
    if not isinstance(item, dict):
        return None
    raw_columns = item.get("columns")
    columns: list[str] = []
    if isinstance(raw_columns, list):
        for col in raw_columns:
            if isinstance(col, str):
                candidate = col.strip()
                if candidate:
                    columns.append(candidate)
    raw_rows = item.get("rows")
    rows: list[dict] = []
    if isinstance(raw_rows, list):
        for row in raw_rows:
            normalized_row = _normalize_freight_table_row(row, columns)
            if normalized_row:
                rows.append(normalized_row)
    return {
        "table_title": _optional_normalized_str(item.get("table_title")),
        "table_type": _optional_normalized_str(item.get("table_type")),
        "context": _normalize_freight_table_context(item.get("context")),
        "columns": columns,
        "rows": rows,
        "notes": _optional_normalized_str(item.get("notes")) or "",
        "evidence_ref": _optional_normalized_str(item.get("evidence_ref")),
        "confidence": _optional_normalized_str(item.get("confidence")),
    }


def _row_has_any_value(row: dict) -> bool:
    if not isinstance(row, dict):
        return False
    for val in row.values():
        if val is None:
            continue
        if isinstance(val, str) and not val.strip():
            continue
        return True
    return False


def _is_useful_freight_table(item) -> bool:
    normalized = _normalize_freight_table_item(item) if isinstance(item, dict) else None
    if not normalized:
        return False
    if normalized.get("table_title"):
        return True
    if normalized.get("columns"):
        return True
    for row in normalized.get("rows") or []:
        if _row_has_any_value(row):
            return True
    return False


def _normalize_freight_tables(raw_tables) -> list[dict]:
    if not isinstance(raw_tables, list):
        return []
    normalized: list[dict] = []
    for item in raw_tables:
        table = _normalize_freight_table_item(item)
        if table is not None:
            normalized.append(table)
    return normalized


def _has_useful_partial_extraction_data(raw: dict) -> bool:
    for item in _list_field_from_raw(raw, "freight_tables"):
        if _is_useful_freight_table(item):
            return True
    for item in _list_field_from_raw(raw, "freight_routes"):
        if _is_useful_freight_route(item):
            return True
    for item in _list_field_from_raw(raw, "freight_values"):
        if _is_useful_freight_value(item):
            return True
    for item in _list_field_from_raw(raw, "accessorial_fees"):
        if _is_useful_accessorial_fee(item):
            return True
    for item in _list_field_from_raw(raw, "weight_ranges"):
        if _is_useful_weight_range(item):
            return True
    return False


def _has_legacy_useful_extraction_data(raw: dict) -> bool:
    for name in ("origins", "destinations", "routes", "extracted_items", "uncertain_fields"):
        items = _list_field_from_raw(raw, name)
        if items:
            return True
    carrier = raw.get("detected_carrier")
    if isinstance(carrier, str) and carrier.strip():
        return True
    charge = raw.get("charge_type_detected")
    if isinstance(charge, str) and charge.strip():
        return True
    return False


def _resolve_extraction_status(raw: dict, expanded: dict) -> str:
    """Resolve status final após normalização partial-first."""
    has_partial = _has_useful_partial_extraction_data(expanded)
    has_legacy = _has_legacy_useful_extraction_data(expanded)
    candidate = _normalize_temp_table_status(raw.get("status"))

    if has_partial:
        return TEMP_TABLE_STATUS_NEEDS_REVIEW

    if candidate == TEMP_TABLE_STATUS_AWAITING_VALIDATION:
        return TEMP_TABLE_STATUS_AWAITING_VALIDATION

    if candidate == TEMP_TABLE_STATUS_VALIDATED:
        return TEMP_TABLE_STATUS_VALIDATED

    if has_legacy and candidate == TEMP_TABLE_STATUS_FAILED:
        return TEMP_TABLE_STATUS_NEEDS_REVIEW

    if has_legacy:
        return candidate

    if candidate == TEMP_TABLE_STATUS_NEEDS_REVIEW:
        return TEMP_TABLE_STATUS_FAILED

    return TEMP_TABLE_STATUS_FAILED


def normalize_partial_first_extraction_to_temp_table(raw: dict) -> dict:
    """
    Normaliza a resposta partial-first (Etapa A) para o contrato interno da temp_table.

    A Etapa A retorna apenas custos brutos detectados; o backend completa campos
    opcionais do contrato interno sem exigir fechamento de rotas ou transportadora.
    """
    alerts = [
        str(item).strip()
        for item in _list_field_from_raw(raw, "reading_alerts")
        if isinstance(item, str) and str(item).strip()
    ]
    expanded = {
        "status": raw.get("status"),
        "detected_carrier": raw.get("detected_carrier"),
        "origins": _list_field_from_raw(raw, "origins"),
        "destinations": _list_field_from_raw(raw, "destinations"),
        "routes": _list_field_from_raw(raw, "routes"),
        "freight_tables": _normalize_freight_tables(_list_field_from_raw(raw, "freight_tables")),
        "freight_routes": _normalize_freight_routes(_list_field_from_raw(raw, "freight_routes")),
        "weight_ranges": _list_field_from_raw(raw, "weight_ranges"),
        "freight_values": _list_field_from_raw(raw, "freight_values"),
        "accessorial_fees": _list_field_from_raw(raw, "accessorial_fees"),
        "charge_type_detected": raw.get("charge_type_detected"),
        "extracted_items": _list_field_from_raw(raw, "extracted_items"),
        "uncertain_fields": _list_field_from_raw(raw, "uncertain_fields"),
        "reading_alerts": alerts,
        "evidence_refs": _list_field_from_raw(raw, "evidence_refs"),
        "franquia_scope": raw.get("franquia_scope"),
        "user_scope": raw.get("user_scope"),
    }
    expanded["status"] = _resolve_extraction_status(raw, expanded)
    return expanded


def _coerce_temp_table_payload(raw: dict, *, source_doc_ids: list[str]) -> dict:
    now = _utcnow().isoformat()
    normalized = normalize_partial_first_extraction_to_temp_table(raw)
    status = _resolve_extraction_status(raw, normalized)
    uncertain = (
        normalized.get("uncertain_fields")
        if isinstance(normalized.get("uncertain_fields"), list)
        else []
    )
    alerts = [
        str(item).strip()
        for item in (
            normalized.get("reading_alerts")
            if isinstance(normalized.get("reading_alerts"), list)
            else []
        )
        if isinstance(item, str) and str(item).strip()
    ]

    if _has_useful_partial_extraction_data(normalized):
        status = TEMP_TABLE_STATUS_NEEDS_REVIEW
    elif status == TEMP_TABLE_STATUS_NEEDS_REVIEW and not _has_legacy_useful_extraction_data(
        normalized
    ):
        status = TEMP_TABLE_STATUS_FAILED

    if status == TEMP_TABLE_STATUS_AWAITING_VALIDATION and (uncertain or alerts):
        status = TEMP_TABLE_STATUS_NEEDS_REVIEW
    if status == TEMP_TABLE_STATUS_NEEDS_REVIEW and not alerts and not uncertain:
        alerts = [
            "A extração encontrou dados parciais e precisa de validação humana."
        ]
    if status == TEMP_TABLE_STATUS_FAILED and not alerts:
        alerts = [
            "Não foi possível estruturar a tabela temporária a partir dos anexos enviados."
        ]

    temp_table_id = get_temp_table_id(session) or uuid4().hex
    existing = load_temp_table_record(temp_table_id, ttl_hours=get_cleiton_doc_config().upload_ttl_hours)
    created_at = (existing or {}).get("created_at") or now
    return {
        "temp_table_id": temp_table_id,
        "status": status,
        "source_documents": _normalize_source_doc_ids(source_doc_ids),
        "detected_carrier": normalized.get("detected_carrier"),
        "origins": _list_field_from_raw(normalized, "origins"),
        "destinations": _list_field_from_raw(normalized, "destinations"),
        "routes": _list_field_from_raw(normalized, "routes"),
        "freight_tables": _normalize_freight_tables(_list_field_from_raw(normalized, "freight_tables")),
        "freight_routes": _normalize_freight_routes(_list_field_from_raw(normalized, "freight_routes")),
        "weight_ranges": _list_field_from_raw(normalized, "weight_ranges"),
        "freight_values": _list_field_from_raw(normalized, "freight_values"),
        "accessorial_fees": _list_field_from_raw(normalized, "accessorial_fees"),
        "charge_type_detected": normalized.get("charge_type_detected"),
        "extracted_items": _list_field_from_raw(normalized, "extracted_items"),
        "uncertain_fields": uncertain,
        "reading_alerts": alerts,
        "evidence_refs": _list_field_from_raw(normalized, "evidence_refs"),
        "created_at": created_at,
        "updated_at": now,
        "expires_at": _temp_table_expires_at(_normalize_source_doc_ids(source_doc_ids)),
        "session_scope": CLEIDE_AUDIT_DOC_IDS_SESSION_KEY,
        "franquia_scope": normalized.get("franquia_scope"),
        "user_scope": normalized.get("user_scope"),
        "operational_owner": TEMP_TABLE_OPERATIONAL_OWNER,
        "ui_visibility": {
            "display_name": TEMP_TABLE_UI_DISPLAY_NAME,
            "readonly": True,
        },
        "version_marker": TEMP_TABLE_VERSION_MARKER,
    }


def apply_temp_table_extraction_from_model_payload(
    payload: dict | None,
    *,
    source_doc_ids: list[str],
) -> dict | None:
    _require_session()
    normalized = _normalize_source_doc_ids(source_doc_ids)
    if not normalized:
        return None
    if not isinstance(payload, dict):
        record = _coerce_temp_table_payload({"status": TEMP_TABLE_STATUS_FAILED}, source_doc_ids=normalized)
        return save_temp_table_record(record)
    record = _coerce_temp_table_payload(payload, source_doc_ids=normalized)
    return save_temp_table_record(record)


def split_temp_table_block_from_answer(answer_text: str) -> tuple[str, dict | None]:
    text = answer_text or ""
    begin = text.find(TEMP_TABLE_JSON_BEGIN)
    if begin < 0:
        return text.strip(), None
    end = text.find(TEMP_TABLE_JSON_END, begin)
    if end < 0:
        return text.strip(), None
    json_chunk = text[begin + len(TEMP_TABLE_JSON_BEGIN) : end].strip()
    visible = (text[:begin] + text[end + len(TEMP_TABLE_JSON_END) :]).strip()
    if not json_chunk:
        return visible, None
    try:
        parsed = json.loads(json_chunk)
    except (TypeError, ValueError, json.JSONDecodeError):
        return visible, None
    return visible, parsed if isinstance(parsed, dict) else None


def build_document_status_metadata() -> dict:
    """Metadados básicos para futuro endpoint de status da Cleide Auditoria."""
    _require_session()
    maybe_cleanup_expired_cleiton_docs()
    sync_temp_table_with_session_documents()
    totals = get_document_session_totals()
    temp_table = get_active_temp_table_for_session()
    return {
        "domain": CLEIDE_AUDIT_DOMAIN,
        "flow_types": {
            "upload": CLEIDE_AUDIT_DOCUMENT_UPLOAD_FLOW_TYPE,
            "prepare": CLEIDE_AUDIT_DOCUMENT_PREPARE_FLOW_TYPE,
            "chat": CLEIDE_AUDIT_CHAT_FLOW_TYPE,
            "temp_table_extraction": CLEIDE_AUDIT_TEMP_TABLE_EXTRACTION_FLOW_TYPE,
        },
        "documents": get_active_documents_for_session(),
        "temp_table": temp_table,
        "allowed_formats": get_allowed_document_formats(),
        "session": {
            "count": totals["active_count"],
            "max_files": totals["max_files_per_session"],
            "total_bytes": totals["total_bytes"],
            "session_max_bytes": totals["session_max_bytes"],
        },
    }
