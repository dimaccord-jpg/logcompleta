"""
Orquestração de validação e preparação documental do Cleiton (Fase 3).

Combina segurança técnica, conversão por formato e metadados padronizados.
Sem rotas públicas, chat da Júlia ou interpretação de negócio.
"""
from __future__ import annotations

from app.cleiton_doc_converters import convert_document
from app.cleiton_doc_contracts import (
    FIELD_CHAR_COUNT,
    FIELD_COLUMN_COUNT,
    FIELD_CONTEXT_KIND,
    FIELD_DOC_TYPE,
    FIELD_ERROR_CODE,
    FIELD_EXTENSION,
    FIELD_MAX_DEPTH,
    FIELD_MIME_TYPE,
    FIELD_NODE_COUNT,
    FIELD_PAGE_COUNT,
    FIELD_PREPARED_CONTEXT,
    FIELD_ROW_COUNT,
    FIELD_SIZE_BYTES,
    FIELD_TRUNCATED,
    FIELD_WARNINGS,
)
from app.cleiton_doc_security import CleitonDocSecurityError, validate_upload_security
from app.services.cleiton_doc_config_service import CleitonDocConfig, get_cleiton_doc_config


def _empty_result() -> dict:
    return {
        FIELD_DOC_TYPE: None,
        FIELD_EXTENSION: None,
        FIELD_MIME_TYPE: None,
        FIELD_SIZE_BYTES: None,
        FIELD_PREPARED_CONTEXT: None,
        FIELD_CONTEXT_KIND: None,
        FIELD_TRUNCATED: False,
        FIELD_CHAR_COUNT: None,
        FIELD_ROW_COUNT: None,
        FIELD_COLUMN_COUNT: None,
        FIELD_PAGE_COUNT: None,
        FIELD_NODE_COUNT: None,
        FIELD_MAX_DEPTH: None,
        FIELD_ERROR_CODE: None,
        FIELD_WARNINGS: [],
    }


def prepare_document(
    *,
    display_name: str,
    file_bytes: bytes,
    mime_type: str | None = None,
    extension: str | None = None,
    cfg: CleitonDocConfig | None = None,
) -> dict:
    """
    Valida tecnicamente e prepara contexto documental temporário.

    Retorna dict padronizado com metadados técnicos ou levanta
    CleitonDocSecurityError com error_code específico.
    """
    cfg = cfg or get_cleiton_doc_config()
    security = validate_upload_security(
        display_name=display_name,
        file_bytes=file_bytes,
        mime_type=mime_type,
        extension=extension,
        cfg=cfg,
    )
    conversion = convert_document(security.doc_type, file_bytes, cfg)

    return {
        FIELD_DOC_TYPE: security.doc_type,
        FIELD_EXTENSION: security.extension,
        FIELD_MIME_TYPE: security.mime_type,
        FIELD_SIZE_BYTES: security.size_bytes,
        FIELD_PREPARED_CONTEXT: conversion.prepared_context,
        FIELD_CONTEXT_KIND: conversion.context_kind,
        FIELD_TRUNCATED: conversion.truncated,
        FIELD_CHAR_COUNT: conversion.char_count,
        FIELD_ROW_COUNT: conversion.row_count,
        FIELD_COLUMN_COUNT: conversion.column_count,
        FIELD_PAGE_COUNT: conversion.page_count,
        FIELD_NODE_COUNT: conversion.node_count,
        FIELD_MAX_DEPTH: conversion.max_depth,
        FIELD_ERROR_CODE: None,
        FIELD_WARNINGS: list(conversion.warnings),
        "display_name": security.display_name,
        "safe_name": security.safe_name,
    }


def prepare_document_safe(
    *,
    display_name: str,
    file_bytes: bytes,
    mime_type: str | None = None,
    extension: str | None = None,
    cfg: CleitonDocConfig | None = None,
) -> dict:
    """Variante que captura CleitonDocSecurityError e retorna error_code no dict."""
    result = _empty_result()
    try:
        return prepare_document(
            display_name=display_name,
            file_bytes=file_bytes,
            mime_type=mime_type,
            extension=extension,
            cfg=cfg,
        )
    except CleitonDocSecurityError as exc:
        result[FIELD_ERROR_CODE] = exc.error_code
        return result
