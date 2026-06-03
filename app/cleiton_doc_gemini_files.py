"""
Adapter governado para PDF via Gemini Files API (Fase PDF real).

Upload/remoção de arquivos no Gemini e montagem de partes para generate_content.
Sem OCR, parser pesado de PDF ou exposição de binário em logs.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.cleiton_doc_contracts import (
    CONTEXT_KIND_GEMINI_FILE,
    FIELD_CONTEXT_KIND,
    FIELD_GEMINI_FILE_NAME,
    FIELD_GEMINI_FILE_STATE,
    FIELD_GEMINI_FILE_URI,
    FIELD_GEMINI_MIME_TYPE,
    GEMINI_FILE_STATE_ACTIVE,
    GEMINI_FILE_STATE_FAILED,
    GEMINI_FILE_STATE_PROCESSING,
)

logger = logging.getLogger(__name__)

_PDF_PAGE_MARKER = re.compile(rb"/Type\s*/Page\b")

_GEMINI_FILE_POLL_ATTEMPTS = 12
_GEMINI_FILE_POLL_DELAY_S = 0.5


def _safe_gemini_file_name(value: Any) -> str:
    raw = str(value or "").strip()
    return raw[:120] if raw else ""


@dataclass(frozen=True)
class PdfGeminiPlaceholder:
    prepared_context: str
    context_kind: str
    page_count: int | None
    warnings: list[str]


@dataclass(frozen=True)
class GeminiPdfUploadResult:
    ok: bool
    gemini_file_name: str | None = None
    gemini_file_uri: str | None = None
    gemini_mime_type: str | None = None
    gemini_file_state: str | None = None
    gemini_uploaded_at: str | None = None
    prepared_context: str | None = None
    warnings: list[str] | None = None
    error_summary: str | None = None


def estimate_pdf_page_count(file_bytes: bytes) -> int | None:
    """
    Heurística leve sobre bytes do PDF.

    Contagem real de páginas pode divergir; validação definitiva fica para
    Gemini File API quando parser local não for usado.
    """
    if not file_bytes:
        return None
    matches = _PDF_PAGE_MARKER.findall(file_bytes)
    count = len(matches)
    return count if count > 0 else None


def _utcnow_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


def build_pdf_gemini_placeholder(
    *,
    size_bytes: int,
    mime_type: str,
    page_count: int | None,
    max_pages: int,
    gemini_ready: bool = False,
    gemini_error: bool = False,
) -> PdfGeminiPlaceholder:
    warnings: list[str] = []
    if page_count is None:
        warnings.append(
            "page_count_indeterminate_local: validação de páginas adiada para Gemini File API."
        )

    payload = {
        "strategy": "gemini_file_api",
        "mime_type": mime_type,
        "size_bytes": size_bytes,
        "page_count": page_count,
        "page_count_source": "local_heuristic" if page_count is not None else None,
        "max_pages_configured": max_pages,
        "local_text_extraction": False,
        "ocr": False,
        "gemini_file_ready": gemini_ready,
        "gemini_file_error": gemini_error,
    }
    return PdfGeminiPlaceholder(
        prepared_context=json.dumps(payload, ensure_ascii=True, sort_keys=True),
        context_kind=CONTEXT_KIND_GEMINI_FILE,
        page_count=page_count,
        warnings=warnings,
    )


def get_cleiton_gemini_client() -> Any | None:
    """Cliente Gemini compartilhado para operações documentais governadas."""
    key = os.getenv("GEMINI_API_KEY_1") or os.getenv("GEMINI_API_KEY")
    if not key:
        return None
    try:
        from google import genai
        from google.genai import types as genai_types

        timeout_ms = 30_000
        raw = (os.getenv("GEMINI_HTTP_TIMEOUT_MS") or "").strip()
        if raw:
            try:
                timeout_ms = max(1_000, int(raw))
            except ValueError:
                pass
        return genai.Client(api_key=key, http_options=genai_types.HttpOptions(timeout=timeout_ms))
    except Exception as exc:
        logger.warning("Cleiton doc Gemini: falha ao inicializar cliente: %s", exc)
        return None


def _file_attr(file_obj: Any, *names: str) -> Any:
    for name in names:
        if isinstance(file_obj, dict):
            if name in file_obj:
                return file_obj[name]
        else:
            val = getattr(file_obj, name, None)
            if val is not None:
                return val
    return None


def normalize_gemini_file_state(state: Any) -> str:
    """
    Normaliza estado de arquivo Gemini (string, enum SDK ou None) para token uppercase.

    Ex.: ACTIVE, FileState.ACTIVE, enum com .name -> ACTIVE.
    Não infere ACTIVE por substring (ex.: INACTIVE permanece INACTIVE).
    """
    if state is None:
        return ""
    raw: Any = state
    if not isinstance(state, str):
        name = getattr(state, "name", None)
        if isinstance(name, str) and name.strip():
            raw = name
        else:
            raw = str(state)
    text = str(raw).strip()
    if not text:
        return ""
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text.strip().upper()


def is_gemini_file_active_state(state: Any) -> bool:
    return normalize_gemini_file_state(state) == GEMINI_FILE_STATE_ACTIVE


def is_gemini_file_failed_state(state: Any) -> bool:
    return normalize_gemini_file_state(state) == GEMINI_FILE_STATE_FAILED


def is_gemini_file_processing_state(state: Any) -> bool:
    return normalize_gemini_file_state(state) == GEMINI_FILE_STATE_PROCESSING


def _wait_for_gemini_file_active(client: Any, file_name: str) -> tuple[str | None, str | None]:
    state = GEMINI_FILE_STATE_PROCESSING
    uri = None
    for _ in range(_GEMINI_FILE_POLL_ATTEMPTS):
        try:
            info = client.files.get(name=file_name)
        except Exception as exc:
            logger.warning(
                "Cleiton doc Gemini: falha ao consultar estado do arquivo (name=%s): %s",
                file_name,
                exc,
            )
            return None, None
        state = normalize_gemini_file_state(_file_attr(info, "state")) or GEMINI_FILE_STATE_PROCESSING
        uri = _file_attr(info, "uri")
        if is_gemini_file_active_state(state):
            return state, uri
        if is_gemini_file_failed_state(state):
            return state, uri
        time.sleep(_GEMINI_FILE_POLL_DELAY_S)
    return state, uri


def upload_pdf_to_gemini_files_api(
    *,
    file_bytes: bytes,
    mime_type: str = "application/pdf",
    display_name: str | None = None,
    page_count: int | None = None,
    max_pages: int = 50,
    client: Any | None = None,
) -> GeminiPdfUploadResult:
    """
    Envia PDF para Gemini Files API e aguarda estado ACTIVE quando possível.

    Não persiste binário localmente; apenas retorna referências temporárias do Gemini.
    """
    placeholder = build_pdf_gemini_placeholder(
        size_bytes=len(file_bytes),
        mime_type=mime_type,
        page_count=page_count,
        max_pages=max_pages,
    )
    gemini_client = client or get_cleiton_gemini_client()
    if gemini_client is None:
        return GeminiPdfUploadResult(
            ok=False,
            prepared_context=placeholder.prepared_context,
            warnings=list(placeholder.warnings),
            error_summary="gemini_client_unavailable",
        )

    safe_display = (display_name or "documento.pdf").strip() or "documento.pdf"
    try:
        doc_io = io.BytesIO(file_bytes)
        uploaded = gemini_client.files.upload(
            file=doc_io,
            config={"mime_type": mime_type, "display_name": safe_display[:200]},
        )
    except Exception as exc:
        logger.warning(
            "Cleiton doc Gemini: upload Files API falhou (bytes=%s, display=%s): %s",
            len(file_bytes),
            safe_display[:80],
            exc,
        )
        err_placeholder = build_pdf_gemini_placeholder(
            size_bytes=len(file_bytes),
            mime_type=mime_type,
            page_count=page_count,
            max_pages=max_pages,
            gemini_error=True,
        )
        return GeminiPdfUploadResult(
            ok=False,
            prepared_context=err_placeholder.prepared_context,
            warnings=list(placeholder.warnings),
            error_summary="gemini_upload_failed",
        )

    file_name = _file_attr(uploaded, "name")
    file_uri = _file_attr(uploaded, "uri")
    file_mime = _file_attr(uploaded, "mime_type", "mimeType") or mime_type
    initial_state = normalize_gemini_file_state(_file_attr(uploaded, "state"))

    if not file_name:
        err_placeholder = build_pdf_gemini_placeholder(
            size_bytes=len(file_bytes),
            mime_type=mime_type,
            page_count=page_count,
            max_pages=max_pages,
            gemini_error=True,
        )
        return GeminiPdfUploadResult(
            ok=False,
            prepared_context=err_placeholder.prepared_context,
            warnings=list(placeholder.warnings),
            error_summary="gemini_upload_missing_name",
        )

    state = initial_state or GEMINI_FILE_STATE_PROCESSING
    if is_gemini_file_processing_state(state):
        polled_state, polled_uri = _wait_for_gemini_file_active(gemini_client, file_name)
        if polled_state:
            state = polled_state
        if polled_uri:
            file_uri = polled_uri

    if is_gemini_file_failed_state(state) or not is_gemini_file_active_state(state):
        err_placeholder = build_pdf_gemini_placeholder(
            size_bytes=len(file_bytes),
            mime_type=mime_type,
            page_count=page_count,
            max_pages=max_pages,
            gemini_error=True,
        )
        return GeminiPdfUploadResult(
            ok=False,
            gemini_file_name=file_name,
            gemini_file_uri=file_uri,
            gemini_mime_type=file_mime,
            gemini_file_state=state,
            prepared_context=err_placeholder.prepared_context,
            warnings=list(placeholder.warnings),
            error_summary="gemini_file_not_active",
        )

    ready_placeholder = build_pdf_gemini_placeholder(
        size_bytes=len(file_bytes),
        mime_type=mime_type,
        page_count=page_count,
        max_pages=max_pages,
        gemini_ready=True,
    )
    return GeminiPdfUploadResult(
        ok=True,
        gemini_file_name=file_name,
        gemini_file_uri=file_uri,
        gemini_mime_type=file_mime,
        gemini_file_state=GEMINI_FILE_STATE_ACTIVE,
        gemini_uploaded_at=_utcnow_iso(),
        prepared_context=ready_placeholder.prepared_context,
        warnings=list(placeholder.warnings),
    )


def delete_gemini_file_safe(file_name: str | None, *, client: Any | None = None) -> bool:
    """Remove arquivo no Gemini; tolera ausência ou falha sem propagar exceção."""
    ref = (file_name or "").strip()
    if not ref:
        return False
    gemini_client = client or get_cleiton_gemini_client()
    if gemini_client is None:
        return False
    try:
        gemini_client.files.delete(name=ref)
        return True
    except Exception as exc:
        logger.warning(
            "Cleiton doc Gemini: falha ao remover arquivo remoto (name=%s): %s",
            ref,
            exc,
        )
        return False


def cleanup_gemini_file_for_record(record: dict | None) -> None:
    if not isinstance(record, dict):
        return
    delete_gemini_file_safe(record.get(FIELD_GEMINI_FILE_NAME))


def pdf_context_ready_from_record(record: dict) -> bool:
    kind = (record.get(FIELD_CONTEXT_KIND) or "").strip()
    if kind != CONTEXT_KIND_GEMINI_FILE:
        return False
    return (
        is_gemini_file_active_state(record.get(FIELD_GEMINI_FILE_STATE))
        and bool((record.get(FIELD_GEMINI_FILE_NAME) or "").strip())
    )


def build_gemini_file_part_for_generate(record: dict, *, client: Any | None = None) -> Any | None:
    """
    Retorna objeto de arquivo do SDK para inclusão em generate_content.

    Requer registro com referência Gemini ACTIVE; não expõe binário.
    """
    if not pdf_context_ready_from_record(record):
        return None
    file_name = (record.get(FIELD_GEMINI_FILE_NAME) or "").strip()
    if not file_name:
        return None
    gemini_client = client or get_cleiton_gemini_client()
    if gemini_client is None:
        return None
    try:
        file_obj = gemini_client.files.get(name=file_name)
    except Exception as exc:
        logger.warning(
            "Cleiton doc Gemini: falha ao obter arquivo para generate_content (name=%s): %s",
            file_name,
            exc,
        )
        return None
    state = normalize_gemini_file_state(_file_attr(file_obj, "state"))
    logger.info(
        "Cleiton doc Gemini: files.get pre-generate name=%s state=%s mime_type=%s",
        _safe_gemini_file_name(file_name),
        state or "<empty>",
        (_file_attr(file_obj, "mime_type", "mimeType") or record.get(FIELD_GEMINI_MIME_TYPE) or "").strip() or "<empty>",
    )
    if is_gemini_file_failed_state(state):
        return None
    uri = _file_attr(file_obj, "uri") or record.get(FIELD_GEMINI_FILE_URI)
    mime = _file_attr(file_obj, "mime_type", "mimeType") or record.get(FIELD_GEMINI_MIME_TYPE)
    if uri and mime:
        try:
            from google.genai import types as genai_types

            return genai_types.Part.from_uri(file_uri=uri, mime_type=mime)
        except Exception:
            pass
    return file_obj
