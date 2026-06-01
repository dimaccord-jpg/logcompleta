"""
Validação técnica de uploads documentais do Cleiton (Fase 3).

Responsável por extensão, MIME, tamanho real, tipo habilitado e nome seguro.
Sem interpretação de conteúdo ou taxonomia de negócio.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from werkzeug.utils import secure_filename

from app.cleiton_doc_contracts import (
    DOC_TYPE_CSV,
    DOC_TYPE_DOCX,
    DOC_TYPE_PDF,
    DOC_TYPE_TXT,
    DOC_TYPE_XLSX,
    DOC_TYPE_XML,
    ERROR_CORRUPTED_FILE,
    ERROR_DISABLED_TYPE,
    ERROR_EMPTY_FILE,
    ERROR_FILE_TOO_LARGE,
    ERROR_INVALID_EXTENSION,
    ERROR_INVALID_MIME,
    ERROR_UNSAFE_FILENAME,
    ERROR_UNSUPPORTED_TYPE,
    ERROR_UPLOAD_DISABLED,
)
from app.services.cleiton_doc_config_service import CleitonDocConfig, get_cleiton_doc_config

EXTENSION_TO_DOC_TYPE: dict[str, str] = {
    ".txt": DOC_TYPE_TXT,
    ".xml": DOC_TYPE_XML,
    ".csv": DOC_TYPE_CSV,
    ".xlsx": DOC_TYPE_XLSX,
    ".docx": DOC_TYPE_DOCX,
    ".pdf": DOC_TYPE_PDF,
}

DEFAULT_MIME_BY_EXTENSION: dict[str, str] = {
    ".txt": "text/plain",
    ".xml": "application/xml",
    ".csv": "text/csv",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
}

ALLOWED_MIMES_BY_EXTENSION: dict[str, frozenset[str]] = {
    ".txt": frozenset({"text/plain", "application/octet-stream"}),
    ".xml": frozenset(
        {
            "application/xml",
            "text/xml",
            "application/octet-stream",
        }
    ),
    ".csv": frozenset({"text/csv", "text/plain", "application/csv", "application/octet-stream"}),
    ".xlsx": frozenset(
        {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/octet-stream",
        }
    ),
    ".docx": frozenset(
        {
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/octet-stream",
        }
    ),
    ".pdf": frozenset({"application/pdf", "application/octet-stream"}),
}


class CleitonDocSecurityError(ValueError):
    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


@dataclass(frozen=True)
class SecurityValidationResult:
    doc_type: str
    extension: str
    mime_type: str
    size_bytes: int
    display_name: str
    safe_name: str


def _normalize_extension(extension: str) -> str:
    raw = (extension or "").strip().lower()
    if not raw:
        return ""
    return raw if raw.startswith(".") else f".{raw}"


def _extension_from_name(display_name: str) -> str:
    name = (display_name or "").strip()
    if not name or "." not in name:
        return ""
    return _normalize_extension(name.rsplit(".", 1)[-1])


def _assert_safe_display_name(display_name: str) -> str:
    raw = (display_name or "").strip()
    if not raw:
        raise CleitonDocSecurityError(
            ERROR_UNSAFE_FILENAME,
            "Nome de arquivo ausente ou inválido.",
        )
    if any(sep in raw for sep in ("/", "\\")):
        raise CleitonDocSecurityError(
            ERROR_UNSAFE_FILENAME,
            "Nome de arquivo contém separador de caminho.",
        )
    if Path(raw).name != raw or raw in {".", ".."}:
        raise CleitonDocSecurityError(
            ERROR_UNSAFE_FILENAME,
            "Nome de arquivo contém referência de path inválida.",
        )
    safe = secure_filename(raw)
    if not safe:
        raise CleitonDocSecurityError(
            ERROR_UNSAFE_FILENAME,
            "Nome de arquivo não pôde ser sanitizado.",
        )
    return raw


def _build_safe_name(display_name: str, extension: str) -> str:
    raw = _assert_safe_display_name(display_name)
    safe = secure_filename(raw) or "documento"
    ext = _normalize_extension(extension) or _extension_from_name(raw)
    if ext and not safe.lower().endswith(ext):
        return f"{safe}{ext}"
    return safe


def _resolve_doc_type(extension: str) -> str:
    ext = _normalize_extension(extension)
    doc_type = EXTENSION_TO_DOC_TYPE.get(ext)
    if not doc_type:
        raise CleitonDocSecurityError(
            ERROR_INVALID_EXTENSION,
            "Extensão de arquivo não suportada para contexto documental.",
        )
    return doc_type


def _type_enabled(cfg: CleitonDocConfig, doc_type: str) -> bool:
    mapping = {
        DOC_TYPE_TXT: cfg.txt_enabled,
        DOC_TYPE_XML: cfg.xml_enabled,
        DOC_TYPE_CSV: cfg.csv_enabled,
        DOC_TYPE_XLSX: cfg.excel_enabled,
        DOC_TYPE_DOCX: cfg.docx_enabled,
        DOC_TYPE_PDF: cfg.pdf_enabled,
    }
    return bool(mapping.get(doc_type))


def _max_bytes_for_type(cfg: CleitonDocConfig, doc_type: str) -> int:
    mapping = {
        DOC_TYPE_TXT: cfg.txt_max_bytes,
        DOC_TYPE_XML: cfg.xml_max_bytes,
        DOC_TYPE_CSV: cfg.csv_max_bytes,
        DOC_TYPE_XLSX: cfg.excel_max_bytes,
        DOC_TYPE_DOCX: cfg.docx_max_bytes,
        DOC_TYPE_PDF: cfg.pdf_max_bytes,
    }
    limit = mapping.get(doc_type)
    if limit is None:
        raise CleitonDocSecurityError(
            ERROR_UNSUPPORTED_TYPE,
            "Tipo documental não suportado.",
        )
    return int(limit)


def _normalize_mime(mime_type: str | None, extension: str) -> str:
    ext = _normalize_extension(extension)
    raw = (mime_type or "").strip().lower()
    if not raw:
        return DEFAULT_MIME_BY_EXTENSION.get(ext, "application/octet-stream")
    return raw.split(";", 1)[0].strip()


def _validate_mime_for_extension(mime_type: str, extension: str) -> None:
    ext = _normalize_extension(extension)
    allowed = ALLOWED_MIMES_BY_EXTENSION.get(ext)
    if allowed is None:
        return
    if mime_type not in allowed:
        raise CleitonDocSecurityError(
            ERROR_INVALID_MIME,
            "MIME informado não corresponde à extensão do arquivo.",
        )


def validate_pdf_magic(file_bytes: bytes) -> None:
    if not file_bytes.startswith(b"%PDF-"):
        raise CleitonDocSecurityError(
            ERROR_CORRUPTED_FILE,
            "Arquivo PDF inválido ou corrompido.",
        )


def validate_upload_security(
    *,
    display_name: str,
    file_bytes: bytes,
    mime_type: str | None = None,
    extension: str | None = None,
    cfg: CleitonDocConfig | None = None,
) -> SecurityValidationResult:
    cfg = cfg or get_cleiton_doc_config()

    if not cfg.upload_enabled:
        raise CleitonDocSecurityError(
            ERROR_UPLOAD_DISABLED,
            "Upload documental desabilitado pela configuração.",
        )

    display = _assert_safe_display_name(display_name)
    ext = _normalize_extension(extension) if extension else _extension_from_name(display)
    if not ext:
        raise CleitonDocSecurityError(
            ERROR_INVALID_EXTENSION,
            "Extensão de arquivo ausente ou inválida.",
        )

    doc_type = _resolve_doc_type(ext)
    if not _type_enabled(cfg, doc_type):
        raise CleitonDocSecurityError(
            ERROR_DISABLED_TYPE,
            "Tipo de arquivo desabilitado pela configuração.",
        )

    if file_bytes is None or len(file_bytes) == 0:
        raise CleitonDocSecurityError(
            ERROR_EMPTY_FILE,
            "Arquivo vazio não é aceito.",
        )

    size_bytes = len(file_bytes)
    max_bytes = _max_bytes_for_type(cfg, doc_type)
    if size_bytes > max_bytes:
        raise CleitonDocSecurityError(
            ERROR_FILE_TOO_LARGE,
            "Arquivo excede o limite de bytes configurado para o tipo.",
        )

    normalized_mime = _normalize_mime(mime_type, ext)
    if mime_type and str(mime_type).strip():
        _validate_mime_for_extension(normalized_mime, ext)

    if doc_type == DOC_TYPE_PDF:
        validate_pdf_magic(file_bytes)

    safe_name = _build_safe_name(display, ext)
    return SecurityValidationResult(
        doc_type=doc_type,
        extension=ext,
        mime_type=normalized_mime,
        size_bytes=size_bytes,
        display_name=display,
        safe_name=safe_name,
    )
