"""
Adapter/contrato inicial para PDF via Gemini File API (Fase 3).

Prepara metadados técnicos sem integração com chat ou upload remoto.
Sem OCR ou parser pesado de PDF.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from app.cleiton_doc_contracts import CONTEXT_KIND_GEMINI_FILE

_PDF_PAGE_MARKER = re.compile(rb"/Type\s*/Page\b")


@dataclass(frozen=True)
class PdfGeminiPlaceholder:
    prepared_context: str
    context_kind: str
    page_count: int | None
    warnings: list[str]


def estimate_pdf_page_count(file_bytes: bytes) -> int | None:
    """
    Heurística leve sobre bytes do PDF.

    Contagem real de páginas pode divergir; validação definitiva fica para
    Gemini File API / fase posterior quando parser local não for usado.
    """
    if not file_bytes:
        return None
    matches = _PDF_PAGE_MARKER.findall(file_bytes)
    count = len(matches)
    return count if count > 0 else None


def build_pdf_gemini_placeholder(
    *,
    size_bytes: int,
    mime_type: str,
    page_count: int | None,
    max_pages: int,
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
    }
    return PdfGeminiPlaceholder(
        prepared_context=json.dumps(payload, ensure_ascii=True, sort_keys=True),
        context_kind=CONTEXT_KIND_GEMINI_FILE,
        page_count=page_count,
        warnings=warnings,
    )
