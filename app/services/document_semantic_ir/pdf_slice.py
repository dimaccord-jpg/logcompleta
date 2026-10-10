"""Extração espacial local de PDF via pdfplumber. Sem OCR e sem render."""
from __future__ import annotations

import io
import logging
import threading
from typing import Any

from app.services.document_semantic_ir.structure import (
    BLOCK_TABLE,
    BLOCK_UNKNOWN,
    KIND_PAGE,
    LIMITATION_TABLE,
    LIMITATION_VISUAL,
    DocumentSemanticIrError,
    assign_block_ids,
    bbox_from_edges,
    classify_free_block,
    coverage,
    relate_blocks,
    union_bbox,
)

_LINE_TABLE_SETTINGS = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "lines",
}
_PDF_LOGGER_PREFIXES = ("pdfminer", "pdfplumber")
_PDF_LOG_ATTRS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "taskName",
        "asctime",
    }
)


def _is_pdf_extractor_logger(name: str) -> bool:
    return any(name == prefix or name.startswith(prefix + ".") for prefix in _PDF_LOGGER_PREFIXES)


class _PdfSafeLogFilter(logging.Filter):
    """Redige pdfminer/pdfplumber de forma permanente. Sem janela entra/sai."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not _is_pdf_extractor_logger(record.name):
            return True
        exc_type = ""
        info = record.exc_info
        if info and info[0] is not None:
            exc_type = f" exc_type={getattr(info[0], '__name__', 'Exception')}"
        for key in list(record.__dict__):
            if key not in _PDF_LOG_ATTRS:
                record.__dict__.pop(key, None)
        record.msg = f"pdf_extract level={record.levelname}{exc_type}"
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        record.__dict__.pop("message", None)
        return True


_PDF_LOG_FILTER = _PdfSafeLogFilter()
_PDF_LOG_INSTALL_LOCK = threading.Lock()


def _attach_pdf_log_filter(logger: logging.Logger) -> None:
    if not any(existing is _PDF_LOG_FILTER for existing in logger.filters):
        logger.addFilter(_PDF_LOG_FILTER)


class _PdfGuardedLogger(logging.getLoggerClass()):  # type: ignore[misc]
    def __init__(self, name: str, level: int = logging.NOTSET) -> None:
        super().__init__(name, level)
        if isinstance(name, str) and _is_pdf_extractor_logger(name):
            _attach_pdf_log_filter(self)


def _install_pdf_log_guard() -> None:
    """Filtro idempotente nos loggers do extrator, inclusive nos criados depois.

    O filtro entra no ``__init__`` da classe, antes do logger ser publicado.
    Não há troca temporária de ``Logger.handle``, root, handlers ou níveis.
    """
    with _PDF_LOG_INSTALL_LOCK:
        if logging.getLoggerClass() is not _PdfGuardedLogger:
            logging.setLoggerClass(_PdfGuardedLogger)
        for name, logger in list(logging.Logger.manager.loggerDict.items()):
            if isinstance(logger, logging.Logger) and _is_pdf_extractor_logger(name):
                _attach_pdf_log_filter(logger)
        for prefix in _PDF_LOGGER_PREFIXES:
            _attach_pdf_log_filter(logging.getLogger(prefix))


_install_pdf_log_guard()

import pdfplumber  # noqa: E402  filtro já instalado antes de qualquer logger do extrator


def extract_pdf_pages(data: bytes) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    limitations: list[str] = []
    visual_pages: list[int] = []
    pages: list[dict[str, Any]] = []
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages:
                pages.append(_extract_page(page, limitations, visual_pages))
    except DocumentSemanticIrError:
        raise
    except Exception as exc:
        raise DocumentSemanticIrError("pdf_unreadable") from exc
    tables_detected = sum(1 for page in pages for block in page["blocks"] if block.get("type") == BLOCK_TABLE)
    return pages, coverage(
        pages_or_sheets=len(pages),
        tables_detected=tables_detected,
        visual_ununderstood_pages=visual_pages,
        limitations=limitations,
    )


def _extract_page(page: Any, limitations: list[str], visual_pages: list[int]) -> dict[str, Any]:
    page_number = int(page.page_number)
    rotation = int(page.rotation or 0)
    dimensions = {"width": round(float(page.width), 2), "height": round(float(page.height), 2)}
    prefix = f"p{page_number}"
    if not page.chars or _ununderstood_visual(page):
        if page_number not in visual_pages:
            visual_pages.append(page_number)
            limitations.append(LIMITATION_VISUAL)
    if not page.chars:
        block = {
            "type": BLOCK_UNKNOWN,
            "bbox": bbox_from_edges(0, 0, page.width, page.height),
            "text": "",
            "cells": [],
            "provenance": {"extractor": "pdfplumber", "method": "no_accessible_text"},
            "relationships": [],
        }
        assign_block_ids(prefix, [block])
        return _page_shell(page_number, rotation, dimensions, [block])

    tables = page.find_tables(table_settings=_LINE_TABLE_SETTINGS) or []
    table_bboxes = [table.bbox for table in tables]
    if _isolated_graphic_line(page, table_bboxes):
        if page_number not in visual_pages:
            visual_pages.append(page_number)
            limitations.append(LIMITATION_VISUAL)
    words = [
        word
        for word in (page.extract_words(use_text_flow=False, keep_blank_chars=False) or [])
        if str(word.get("text") or "").strip() and not _word_inside_any(word, table_bboxes)
    ]
    blocks: list[dict[str, Any]] = [_table_block(table) for table in tables]
    blocks.extend(_word_blocks(words))
    if _columnar_lines(words) and not tables:
        limitations.append(LIMITATION_TABLE)
    for block in blocks:
        if block["type"] != BLOCK_TABLE:
            block["type"] = classify_free_block(block, blocks)
    assign_block_ids(prefix, blocks)
    relate_blocks(blocks, edge_mode="edges")
    return _page_shell(page_number, rotation, dimensions, blocks)


def _page_shell(page_number: int, rotation: int, dimensions: dict[str, float], blocks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "kind": KIND_PAGE,
        "page_number": page_number,
        "rotation": rotation,
        "dimensions": dimensions,
        "coordinate_space": "top_origin",
        "blocks": blocks,
    }


def _isolated_graphic_line(page: Any, table_bboxes: list[tuple[float, float, float, float]]) -> bool:
    """Linha longa fora de tabela detectada. Bordas de grade continuam dentro do bbox."""
    for item in list(getattr(page, "lines", None) or []) + list(getattr(page, "rects", None) or []):
        width = abs(float(item.get("width") or 0))
        height = abs(float(item.get("height") or 0))
        if max(width, height) < 36 or min(width, height) > 3:
            continue
        x0 = float(item.get("x0") or 0)
        top = float(item.get("top") or 0)
        if _word_inside_any({"x0": x0, "x1": x0, "top": top, "bottom": top}, table_bboxes):
            continue
        return True
    return False


def _ununderstood_visual(page: Any) -> bool:
    """Imagem, curva ou área pintada que este slice não interpreta. Linhas de tabela não contam."""
    if list(getattr(page, "images", None) or []) or list(getattr(page, "curves", None) or []):
        return True
    for rect in list(getattr(page, "rects", None) or []):
        width = abs(float(rect.get("width") or 0))
        height = abs(float(rect.get("height") or 0))
        if min(width, height) <= 2:
            continue
        if width >= 12 and height >= 12:
            return True
    return False


def _word_inside_any(word: dict[str, Any], bboxes: list[tuple[float, float, float, float]]) -> bool:
    cx = (float(word["x0"]) + float(word["x1"])) / 2
    cy = (float(word["top"]) + float(word["bottom"])) / 2
    for x0, top, x1, bottom in bboxes:
        if (x0 - 1) <= cx <= (x1 + 1) and (top - 1) <= cy <= (bottom + 1):
            return True
    return False


def _table_block(table: Any) -> dict[str, Any]:
    extracted = table.extract() or []
    cells: list[dict[str, Any]] = []
    row_texts: list[str] = []
    for row_index, row in enumerate(table.rows, start=1):
        extracted_row = extracted[row_index - 1] if row_index - 1 < len(extracted) else []
        pieces: list[str] = []
        for column_index, cell_bbox in enumerate(row.cells, start=1):
            if cell_bbox is None:
                continue
            raw = extracted_row[column_index - 1] if column_index - 1 < len(extracted_row) else ""
            text = "" if raw is None else str(raw).strip()
            pieces.append(text)
            x0, top, x1, bottom = cell_bbox
            cells.append(
                {
                    "row": row_index,
                    "column": column_index,
                    "bbox": bbox_from_edges(x0, top, x1, bottom),
                    "text": text,
                    "relationships": [],
                }
            )
        row_texts.append(" | ".join(pieces))
    bbox = bbox_from_edges(*table.bbox)
    return {
        "type": BLOCK_TABLE,
        "bbox": bbox,
        "text": "\n".join(row_texts),
        "cells": cells,
        "provenance": {"extractor": "pdfplumber", "method": "lines"},
        "relationships": [],
    }


def _word_blocks(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for line in _group_lines(words):
        cells = []
        for column_index, word in enumerate(line, start=1):
            text = str(word["text"]).strip()
            cells.append(
                {
                    "row": 1,
                    "column": column_index,
                    "bbox": bbox_from_edges(word["x0"], word["top"], word["x1"], word["bottom"]),
                    "text": text,
                    "relationships": [],
                }
            )
        blocks.append(
            {
                "type": "text",
                "bbox": union_bbox([cell["bbox"] for cell in cells]),
                "text": " ".join(cell["text"] for cell in cells if cell["text"]),
                "cells": cells,
                "provenance": {"extractor": "pdfplumber", "method": "words"},
                "relationships": [],
            }
        )
    return blocks


def _group_lines(words: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    ordered = sorted(words, key=lambda word: (float(word["top"]), float(word["x0"])))
    lines: list[list[dict[str, Any]]] = []
    for word in ordered:
        if lines and abs(float(word["top"]) - _line_top(lines[-1])) <= 3:
            lines[-1].append(word)
        else:
            lines.append([word])
    for line in lines:
        line.sort(key=lambda word: float(word["x0"]))
    return lines


def _line_top(line: list[dict[str, Any]]) -> float:
    return sum(float(word["top"]) for word in line) / len(line)


def _columnar_lines(words: list[dict[str, Any]]) -> bool:
    for line in _group_lines(words):
        if len(line) < 3:
            continue
        gaps = [float(line[index + 1]["x0"]) - float(line[index]["x1"]) for index in range(len(line) - 1)]
        if sum(1 for gap in gaps if gap > 15) >= 2:
            return True
    return False
