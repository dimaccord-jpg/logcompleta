"""
Wrapper documental fino do domínio Cleide Auditoria (Fase 1).

Reaproveita preparação, store e configuração governados do Cleiton.
Opera exclusivamente sobre session keys `cleide_audit_*`; não toca sessão da Júlia.
Sem rotas, chat, IA ou billing.
"""
from __future__ import annotations

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

CLEIDE_AUDIT_DOCUMENT_UPLOAD_FLOW_TYPE = "cleide_audit_document_upload"
CLEIDE_AUDIT_DOCUMENT_PREPARE_FLOW_TYPE = "cleide_audit_document_prepare"
CLEIDE_AUDIT_CHAT_FLOW_TYPE = "cleide_audit_chat"

SOURCE_AGENT_CLEIDE_AUDIT = "cleide_audit"


def cleide_audit_upload_idempotency_key(request_id: str) -> str:
    return f"cleide-audit-upload:{(request_id or '').strip()}"


def cleide_audit_upload_doc_idempotency_key(doc_id: str) -> str:
    return f"cleide-audit-upload-doc:{(doc_id or '').strip()}"


def cleide_audit_chat_idempotency_key(request_id: str) -> str:
    return f"cleide-audit-chat:{(request_id or '').strip()}"


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
    _mark_session_modified()
    return {
        "ok": True,
        "requested": len(ids),
        "removed_from_store": removed_store,
        "removed_from_session": removed_session,
    }


def build_document_status_metadata() -> dict:
    """Metadados básicos para futuro endpoint de status da Cleide Auditoria."""
    _require_session()
    maybe_cleanup_expired_cleiton_docs()
    totals = get_document_session_totals()
    return {
        "domain": CLEIDE_AUDIT_DOMAIN,
        "flow_types": {
            "upload": CLEIDE_AUDIT_DOCUMENT_UPLOAD_FLOW_TYPE,
            "prepare": CLEIDE_AUDIT_DOCUMENT_PREPARE_FLOW_TYPE,
            "chat": CLEIDE_AUDIT_CHAT_FLOW_TYPE,
        },
        "documents": get_active_documents_for_session(),
        "allowed_formats": get_allowed_document_formats(),
        "session": {
            "count": totals["active_count"],
            "max_files": totals["max_files_per_session"],
            "total_bytes": totals["total_bytes"],
            "session_max_bytes": totals["session_max_bytes"],
        },
    }
