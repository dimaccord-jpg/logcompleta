"""Entrada local do vertical slice. O arquivo original não sai deste processo."""
from __future__ import annotations

import time

from app.services.document_semantic_ir.pdf_slice import extract_pdf_pages
from app.services.document_semantic_ir.structure import (
    FORMAT_PDF,
    FORMAT_XLSX,
    DocumentSemanticIrError,
    StructuralExtraction,
    document_shell,
    ir_invariant_errors,
    metrics_for_ir,
    source_fingerprint,
)
from app.services.document_semantic_ir.xlsx_slice import extract_xlsx_sheets


def extract_document_semantic_ir(data: bytes, *, file_format: str) -> StructuralExtraction:
    if isinstance(data, bytearray):
        data = bytes(data)
    if not isinstance(data, (bytes, memoryview)):
        raise DocumentSemanticIrError("empty_source")
    raw = bytes(data)
    if not raw:
        raise DocumentSemanticIrError("empty_source")
    fmt = (file_format or "").strip().lower()
    if fmt not in {FORMAT_PDF, FORMAT_XLSX}:
        raise DocumentSemanticIrError("unsupported_format")
    started = time.perf_counter()
    fingerprint = source_fingerprint(raw)
    if fmt == FORMAT_PDF:
        pages, coverage_doc = extract_pdf_pages(raw)
    else:
        pages, coverage_doc = extract_xlsx_sheets(raw)
    ir = document_shell(file_format=fmt, fingerprint=fingerprint, coverage_doc=coverage_doc)
    ir["pages_or_sheets"] = pages
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    errors = ir_invariant_errors(ir)
    if errors:
        raise DocumentSemanticIrError("ir_invariants:" + ",".join(errors))
    return StructuralExtraction(ir=ir, metrics=metrics_for_ir(ir, elapsed_ms=elapsed_ms))
