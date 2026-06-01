"""
Contratos técnicos do núcleo documental governado do Cleiton (Fases 2–3).

Centraliza chaves de sessão, status, campos padronizados e códigos de erro
para evitar hardcode espalhado. Sem taxonomia de negócio ou intenção.
"""
from __future__ import annotations

TMP_DIR_NAME = "cleiton_doc_tmp"
CLEANUP_META_FILENAME = ".cleanup_meta.json"

SESSION_KEY_CLEITON_DOC_IDS = "cleiton_doc_ids"
SESSION_KEY_CLEITON_DOC_LOCK = "cleiton_doc_lock"

SOURCE_AGENT_CLEITON = "cleiton"

STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_EXPIRED = "expired"
STATUS_REMOVED = "removed"
STATUS_ERROR = "error"

DOC_TYPE_TXT = "txt"
DOC_TYPE_XML = "xml"
DOC_TYPE_CSV = "csv"
DOC_TYPE_XLSX = "xlsx"
DOC_TYPE_DOCX = "docx"
DOC_TYPE_PDF = "pdf"

SUPPORTED_DOC_TYPES = frozenset(
    {DOC_TYPE_TXT, DOC_TYPE_XML, DOC_TYPE_CSV, DOC_TYPE_XLSX, DOC_TYPE_DOCX, DOC_TYPE_PDF}
)

CONTEXT_KIND_PLACEHOLDER = "placeholder"
CONTEXT_KIND_TEXT = "text"
CONTEXT_KIND_GEMINI_FILE = "gemini_file"

FIELD_DOC_ID = "doc_id"
FIELD_DOC_TYPE = "doc_type"
FIELD_DISPLAY_NAME = "display_name"
FIELD_SAFE_NAME = "safe_name"
FIELD_EXTENSION = "extension"
FIELD_MIME_TYPE = "mime_type"
FIELD_SIZE_BYTES = "size_bytes"
FIELD_CREATED_AT = "created_at"
FIELD_EXPIRES_AT = "expires_at"
FIELD_STATUS = "status"
FIELD_TRUNCATED = "truncated"
FIELD_CONTEXT_KIND = "context_kind"
FIELD_CONTEXT_REF = "context_ref"
FIELD_PREPARED_CONTEXT = "prepared_context"
FIELD_CHAR_COUNT = "char_count"
FIELD_ROW_COUNT = "row_count"
FIELD_COLUMN_COUNT = "column_count"
FIELD_PAGE_COUNT = "page_count"
FIELD_NODE_COUNT = "node_count"
FIELD_MAX_DEPTH = "max_depth"
FIELD_WARNINGS = "warnings"
FIELD_SOURCE_AGENT = "source_agent"
FIELD_SESSION_KEY = "session_key"
FIELD_ERROR_CODE = "error_code"

ERROR_DOC_ID_INVALID = "cleiton_doc_id_invalid"
ERROR_DOC_NOT_FOUND = "cleiton_doc_not_found"
ERROR_DOC_REMOVE_FAILED = "cleiton_doc_remove_failed"
ERROR_MAX_FILES = "cleiton_doc_max_files"
ERROR_INVALID_SIZE = "cleiton_doc_invalid_size"
ERROR_SESSION_BYTES = "cleiton_doc_session_bytes"
ERROR_STORE_PATH = "cleiton_doc_store_path"
ERROR_STORE_WRITE = "cleiton_doc_store_write"
ERROR_STORE_READ = "cleiton_doc_store_read"
ERROR_INVALID_EXTENSION = "cleiton_doc_invalid_extension"
ERROR_INVALID_MIME = "cleiton_doc_invalid_mime"
ERROR_DISABLED_TYPE = "cleiton_doc_disabled_type"
ERROR_EMPTY_FILE = "cleiton_doc_empty_file"
ERROR_FILE_TOO_LARGE = "cleiton_doc_file_too_large"
ERROR_TOO_MANY_ROWS = "cleiton_doc_too_many_rows"
ERROR_TOO_MANY_COLUMNS = "cleiton_doc_too_many_columns"
ERROR_TOO_MANY_PAGES = "cleiton_doc_too_many_pages"
# Reservado para fase futura: Fase 3 trunca texto acima de *_max_chars (truncated=True).
ERROR_TOO_MANY_CHARS = "cleiton_doc_too_many_chars"
ERROR_TOO_MANY_PARAGRAPHS = "cleiton_doc_too_many_paragraphs"
ERROR_TOO_MANY_NODES = "cleiton_doc_too_many_nodes"
ERROR_TOO_DEEP_XML = "cleiton_doc_too_deep_xml"
ERROR_CORRUPTED_FILE = "cleiton_doc_corrupted_file"
ERROR_UNSUPPORTED_TYPE = "cleiton_doc_unsupported_type"
ERROR_CONVERSION_FAILED = "cleiton_doc_conversion_failed"
ERROR_UPLOAD_DISABLED = "cleiton_doc_upload_disabled"
ERROR_UNSAFE_FILENAME = "cleiton_doc_unsafe_filename"
ERROR_MISSING_FILE = "cleiton_doc_missing_file"
ERROR_UPLOAD_FAILED = "cleiton_doc_upload_failed"

FLOW_TYPE_UPLOAD = "upload"
FLOW_TYPE_CONTEXT_PREP = "context_prep"
FLOW_TYPE_PROMPT_ATTACH = "prompt_attach"
FLOW_TYPE_JULIA_CHAT = "julia_chat"
FLOW_TYPE_JULIA_CHAT_DOCUMENTAL = "julia_chat_documental"


def get_cleiton_doc_ids(session_obj) -> list[str]:
    raw = session_obj.get(SESSION_KEY_CLEITON_DOC_IDS)
    if not isinstance(raw, list):
        return []
    ids: list[str] = []
    for item in raw:
        if isinstance(item, str):
            ref = item.strip()
            if ref:
                ids.append(ref)
    return ids


def set_cleiton_doc_ids(session_obj, doc_ids: list[str]) -> None:
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
    session_obj[SESSION_KEY_CLEITON_DOC_IDS] = cleaned


def clear_cleiton_doc_ids(session_obj) -> None:
    session_obj.pop(SESSION_KEY_CLEITON_DOC_IDS, None)


def append_cleiton_doc_id(session_obj, doc_id: str) -> None:
    ref = (doc_id or "").strip()
    if not ref:
        raise ValueError("doc_id inválido para sessão documental do Cleiton.")
    ids = get_cleiton_doc_ids(session_obj)
    if ref not in ids:
        ids.append(ref)
    set_cleiton_doc_ids(session_obj, ids)


def remove_cleiton_doc_id(session_obj, doc_id: str) -> None:
    ref = (doc_id or "").strip()
    if not ref:
        return
    ids = [item for item in get_cleiton_doc_ids(session_obj) if item != ref]
    if ids:
        set_cleiton_doc_ids(session_obj, ids)
    else:
        clear_cleiton_doc_ids(session_obj)
