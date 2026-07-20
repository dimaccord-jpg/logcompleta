"""
Orquestração da sessão documental governada do Cleiton (Fases 2–3).

Usa configuração administrativa, sessão Flask e store temporário em disco.
Preparação por formato integrada internamente; sem chat da Júlia.
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
    SOURCE_AGENT_CLEITON,
    STATUS_ACTIVE,
    STATUS_ERROR,
    append_cleiton_doc_id,
    clear_cleiton_doc_ids,
    get_cleiton_doc_ids,
    remove_cleiton_doc_id,
)
from app.cleiton_doc_gemini_files import (
    pdf_context_ready_from_record,
    upload_pdf_to_gemini_files_api,
)
from app.cleiton_doc_prepare import prepare_document
from app.cleiton_doc_store import (
    cleanup_expired_document_records,
    load_document_record,
    maybe_cleanup_expired_document_records,
    peek_document_record,
    remove_document_record,
    save_document_record,
)
from app.services.cleiton_doc_config_service import get_cleiton_doc_config

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class CleitonDocSessionError(ValueError):
    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


def _require_session():
    if not has_request_context():
        raise RuntimeError("Sessão documental do Cleiton requer request context Flask.")


def _mark_session_modified() -> None:
    session.modified = True


def _parse_size_bytes(size_bytes) -> int:
    if size_bytes is None:
        raise CleitonDocSessionError(
            ERROR_INVALID_SIZE,
            "size_bytes ausente ou inválido para documento Cleiton.",
        )
    if isinstance(size_bytes, bool):
        raise CleitonDocSessionError(
            ERROR_INVALID_SIZE,
            "size_bytes não numérico para documento Cleiton.",
        )
    try:
        parsed = int(size_bytes)
    except (TypeError, ValueError):
        raise CleitonDocSessionError(
            ERROR_INVALID_SIZE,
            "size_bytes não numérico para documento Cleiton.",
        ) from None
    if parsed <= 0:
        raise CleitonDocSessionError(
            ERROR_INVALID_SIZE,
            "size_bytes deve ser maior que zero para documento Cleiton.",
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
        FIELD_SOURCE_AGENT: record.get(FIELD_SOURCE_AGENT),
        FIELD_SESSION_KEY: record.get(FIELD_SESSION_KEY),
        FIELD_ERROR_CODE: record.get(FIELD_ERROR_CODE),
        FIELD_PDF_CONTEXT_READY: pdf_ready,
    }


def _context_ref_for_kind(context_kind: str, doc_id: str) -> str:
    kind = (context_kind or CONTEXT_KIND_PLACEHOLDER).strip() or CONTEXT_KIND_PLACEHOLDER
    if kind == CONTEXT_KIND_TEXT:
        return f"text:{doc_id}"
    if kind == CONTEXT_KIND_GEMINI_FILE:
        return f"gemini_file:{doc_id}"
    return f"placeholder:{doc_id}"


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


def get_active_documents_for_session() -> list[dict]:
    _require_session()
    cfg = get_cleiton_doc_config()
    active: list[dict] = []
    stale_ids: list[str] = []

    for doc_id in get_cleiton_doc_ids(session):
        record = load_document_record(doc_id, ttl_hours=cfg.upload_ttl_hours)
        if record is None:
            stale_ids.append(doc_id)
            continue
        active.append(_public_record(record))

    if stale_ids:
        for doc_id in stale_ids:
            remove_cleiton_doc_id(session, doc_id)
        _mark_session_modified()

    return active


def _remove_document_record_with_cleanup(doc_id: str) -> dict:
    return remove_document_record(doc_id)


def _record_belongs_to_julia_domain(record: dict | None) -> bool:
    """
    Domínio documental Júlia/Cleiton legado: source_agent cleiton e sem session_key
    dedicada. Registros de AgenteCompara/Cleide Auditoria usam source_agent e
    session_key explícitos e nunca passam por aqui.
    """
    if not isinstance(record, dict):
        return False
    source = str(record.get(FIELD_SOURCE_AGENT) or "").strip()
    session_key = record.get(FIELD_SESSION_KEY)
    session_key_str = "" if session_key is None else str(session_key).strip()
    if session_key_str in {"agente_compara_doc_ids", "cleide_audit_doc_ids"}:
        return False
    if source in {"agente_compara", "cleide_audit"}:
        return False
    if session_key_str:
        return False
    return source in {"", SOURCE_AGENT_CLEITON}


def _cleiton_document_owned_by_session(doc_id: str) -> bool:
    ref = (doc_id or "").strip()
    if not ref or ref not in get_cleiton_doc_ids(session):
        return False
    record = peek_document_record(ref)
    return _record_belongs_to_julia_domain(record)


def register_document_placeholder(
    *,
    display_name: str,
    extension: str,
    mime_type: str,
    size_bytes: int,
    context_kind: str = CONTEXT_KIND_PLACEHOLDER,
    context_ref: str | None = None,
    source_agent: str = SOURCE_AGENT_CLEITON,
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
        FIELD_SOURCE_AGENT: (source_agent or SOURCE_AGENT_CLEITON).strip() or SOURCE_AGENT_CLEITON,
        FIELD_SESSION_KEY: None,
        FIELD_ERROR_CODE: error_code,
        FIELD_GEMINI_FILE_NAME: gemini_file_name,
        FIELD_GEMINI_FILE_URI: gemini_file_uri,
        FIELD_GEMINI_MIME_TYPE: gemini_mime_type,
        FIELD_GEMINI_FILE_STATE: gemini_file_state,
        FIELD_GEMINI_UPLOADED_AT: gemini_uploaded_at,
    }

    save_document_record(record)
    append_cleiton_doc_id(session, doc_id)
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
    Valida, prepara e registra documento na sessão somente após sucesso.

    Levanta CleitonDocSecurityError em falha técnica antes de gravar no store.
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
                "Cleiton doc: upload Gemini Files API falhou para PDF (summary=%s).",
                upload_result.error_summary,
            )

    return register_document_placeholder(**register_kwargs)


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

    if not _cleiton_document_owned_by_session(ref):
        return {
            "ok": True,
            "doc_id": ref,
            "removed_from_store": False,
            "removed_from_session": False,
            "error_code": ERROR_DOC_NOT_FOUND,
        }

    store_result = _remove_document_record_with_cleanup(ref)
    remove_cleiton_doc_id(session, ref)
    _mark_session_modified()

    return {
        "ok": True,
        "doc_id": ref,
        "removed_from_store": bool(store_result.get("removed")),
        "removed_from_session": True,
        "error_code": None,
    }


def clear_documents_for_session() -> dict:
    _require_session()
    ids = list(get_cleiton_doc_ids(session))
    removed_store = 0
    removed_session = 0
    session_changed = False
    for doc_id in ids:
        if not _cleiton_document_owned_by_session(doc_id):
            remove_cleiton_doc_id(session, doc_id)
            session_changed = True
            continue
        result = _remove_document_record_with_cleanup(doc_id)
        if result.get("removed"):
            removed_store += 1
        remove_cleiton_doc_id(session, doc_id)
        removed_session += 1
        session_changed = True
    if not get_cleiton_doc_ids(session):
        clear_cleiton_doc_ids(session)
    if session_changed:
        _mark_session_modified()
    return {
        "ok": True,
        "requested": len(ids),
        "removed_from_store": removed_store,
        "removed_from_session": removed_session,
    }


def cleanup_expired_documents_for_session() -> int:
    _require_session()
    cfg = get_cleiton_doc_config()
    removed = 0
    stale_ids: list[str] = []

    for doc_id in get_cleiton_doc_ids(session):
        record = load_document_record(doc_id, ttl_hours=cfg.upload_ttl_hours)
        if record is None:
            stale_ids.append(doc_id)
            removed += 1

    for doc_id in stale_ids:
        _remove_document_record_with_cleanup(doc_id)
        remove_cleiton_doc_id(session, doc_id)

    if stale_ids:
        _mark_session_modified()
    return removed


def maybe_cleanup_expired_cleiton_docs(*, min_interval_seconds: int = 300) -> int:
    cfg = get_cleiton_doc_config()
    return maybe_cleanup_expired_document_records(
        cfg.upload_ttl_hours,
        cleanup_enabled=cfg.cleanup_enabled,
        min_interval_seconds=min_interval_seconds,
    )


def cleanup_all_expired_cleiton_docs() -> int:
    cfg = get_cleiton_doc_config()
    return cleanup_expired_document_records(cfg.upload_ttl_hours)
