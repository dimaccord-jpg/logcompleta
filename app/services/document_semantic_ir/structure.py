"""
IR estrutural local do vertical slice (SCRUM-146).

Representação interna versionada. Não é schema tarifário, não é o contrato
canônico e não infere regra comercial.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

IR_VERSION = "1"

FORMAT_PDF = "pdf"
FORMAT_XLSX = "xlsx"

KIND_PAGE = "page"
KIND_SHEET = "sheet"

BLOCK_TEXT = "text"
BLOCK_HEADER = "header"
BLOCK_TABLE = "table"
BLOCK_NOTE = "note"
BLOCK_UNKNOWN = "unknown"

REL_ABOVE = "above"
REL_BELOW = "below"
REL_ADJACENT = "adjacent"
REL_INSIDE = "inside"
REL_MERGED_OVER = "merged_over"
REL_SPANS_WIDTH = "spans_width"
REL_SAME_BLOCK = "same_block"

ALLOWED_RELATION_KINDS = frozenset(
    {
        REL_ABOVE,
        REL_BELOW,
        REL_ADJACENT,
        REL_INSIDE,
        REL_MERGED_OVER,
        REL_SPANS_WIDTH,
        REL_SAME_BLOCK,
    }
)

FORBIDDEN_SEMANTIC_KEYS = frozenset(
    {
        "note_applies_to_tariff",
        "city_belongs_to_region",
        "pricing_dimension",
        "weight_rule",
        "destination_mapping",
    }
)

LIMITATION_VISUAL = "visual_content_not_understood"
LIMITATION_TABLE = "table_not_detected_positioned_text_preserved"
LIMITATION_CACHED = "cached_value_unavailable"
LIMITATION_CELL_CAP = "structural_cell_limit"
LIMITATION_MERGE_CAP = "merge_limit"
LIMITATION_TEXT_CAP = "serialized_text_limit"
LIMITATION_GAP_CAP = "gap_span_limit"
LIMITATION_RESOURCE = "structural_resource_limit"

ALLOWED_LIMITATIONS = frozenset(
    {
        LIMITATION_VISUAL,
        LIMITATION_TABLE,
        LIMITATION_CACHED,
        LIMITATION_CELL_CAP,
        LIMITATION_MERGE_CAP,
        LIMITATION_TEXT_CAP,
        LIMITATION_GAP_CAP,
        LIMITATION_RESOURCE,
    }
)

# Acrobat rejeita página acima de 200 pol. CPF/CNPJ não cabem nesse intervalo.
PDF_MAX_USER_UNIT = 14400.0
PDF_ROTATIONS = frozenset({0, 90, 180, 270})
PDF_MAX_AXIS_INDEX = 2000
# Limites reais do OOXML, abaixo de qualquer CPF/CNPJ de 11+ dígitos.
XLSX_MAX_ROW = 1_048_576
XLSX_MAX_COLUMN = 16_384
MAX_PAGES_OR_SHEETS = 200
MAX_MERGE_AREA_ONE = 1024
MAX_MERGE_AREA_TOTAL = 8000
MAX_GAP_ITEMS = 10000
PDF_BBOX_TOLERANCE = 2.0

_BBOX_KEYS = ("x0", "y0", "x1", "y1")
_DOCUMENT_SCALAR_KEYS = frozenset({"value", "cached_value", "text", "comment", "formula"})
_COORD_RE = re.compile(r"^[A-Z]{1,3}[1-9][0-9]{0,6}$")
_RANGE_RE = re.compile(r"^[A-Z]{1,3}[1-9][0-9]{0,6}:[A-Z]{1,3}[1-9][0-9]{0,6}$")
_COLUMN_RANGE_RE = re.compile(r"^[A-Z]{1,3}(?::[A-Z]{1,3})?$")

_ID_RE = re.compile(r"^(?:p\d+-b\d+(?:-c\d+)?|s\d+(?:-b\d+(?:-c\d+)?)?)$")
_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_PATH_RE = re.compile(r"(?:[A-Za-z]:\\)|(?:/Users/)|(?:/home/)|(?:file://)")
_DATA_URI_RE = re.compile(r"data:(?:image|application)/", re.IGNORECASE)
_B64_RE = re.compile(r"^[A-Za-z0-9+/]{80,}={0,2}$")
_ID_KEYS = frozenset({"id", "sheet_id", "target_id"})
RESERVED_FINGERPRINT_KEYS = frozenset({"source_fingerprint_local"})

_ROOT_KEYS = frozenset({"document", "pages_or_sheets"})
_DOCUMENT_BASE_KEYS = frozenset({"format", "ir_version", "coverage"})
_COVERAGE_KEYS = frozenset(
    {"complete", "pages_or_sheets", "tables_detected", "visual_ununderstood_pages", "limitations"}
)
_PAGE_KEYS = frozenset({"kind", "page_number", "rotation", "dimensions", "coordinate_space", "blocks"})
_SHEET_KEYS = frozenset(
    {
        "kind",
        "sheet_id",
        "sheet_name",
        "visibility",
        "coordinate_space",
        "blocks",
        "merges",
        "hidden_rows",
        "hidden_columns",
        "gaps",
        "boundaries",
    }
)
_PDF_BLOCK_KEYS = frozenset({"id", "type", "bbox", "text", "cells", "provenance", "relationships"})
_XLSX_BLOCK_KEYS = frozenset({"id", "type", "bbox", "cells", "provenance", "relationships"})
_PDF_CELL_KEYS = frozenset({"id", "row", "column", "bbox", "text", "relationships"})
_XLSX_CELL_REQUIRED = frozenset(
    {"id", "row", "column", "coordinate", "value", "value_type", "bbox", "relationships"}
)
_XLSX_CELL_OPTIONAL = frozenset({"formula", "cached_value", "comment", "style_ref", "hidden"})
_PROVENANCE_KEYS = frozenset({"extractor", "method"})
_REL_REQUIRED = frozenset({"kind", "target_id"})
_REL_OPTIONAL = frozenset({"range", "anchor", "columns"})
_MERGE_KEYS = frozenset({"range", "anchor"})
_STYLE_KEYS = frozenset({"number_format", "bold", "italic", "fill_pattern"})
_BOUNDARY_KEYS = frozenset({"min_row", "max_row", "min_column", "max_column"})
_GAP_INDEX_KEYS = frozenset({"axis", "index"})
_GAP_COLLAPSED_KEYS = frozenset({"axis", "start", "end", "collapsed"})
_BLOCK_TYPES = frozenset({BLOCK_TEXT, BLOCK_HEADER, BLOCK_TABLE, BLOCK_NOTE, BLOCK_UNKNOWN})
_VALUE_TYPES = frozenset({"formula", "error", "empty", "boolean", "number", "datetime", "string", "unknown"})
_SHEET_VISIBILITY = frozenset({"visible", "hidden", "veryHidden"})
_PDF_METHODS = frozenset({"lines", "words", "no_accessible_text"})
_XLSX_METHODS = frozenset({"sparse_cells"})
_CELL_REF_RE = re.compile(r"^([A-Z]{1,3})([1-9][0-9]{0,6})$")
_SHEET_NAME_FORBIDDEN = set("[]:*?/\\")


class DocumentSemanticIrError(ValueError):
    """Falha local do slice. Não autoriza chamada externa."""


@dataclass(frozen=True)
class StructuralExtraction:
    ir: dict[str, Any]
    metrics: dict[str, int]


@dataclass(frozen=True)
class StructuralDispatch:
    provider_called: bool
    decision: str
    safe_json: str | None
    metrics: dict[str, Any]
    reason_codes: tuple[str, ...] = ()
    response: Any = None


def source_fingerprint(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def render_document_scalar(value: Any) -> str | None:
    """Texto estável de um escalar de célula para a classificação existente.

    Não formata CPF, moeda nem unidade. Inteiros exatos não ganham sufixo ``.0``,
    porque esse sufixo junta um dígito extra e esconde o identificador do classificador.
    ``None`` significa valor não finito: o dispatch não envia o original.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        integral = value.to_integral_value()
        if value == integral:
            return str(integral)
        return format(value, "f")
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        if value == 0.0 or value.is_integer():
            return str(int(value))
        # str() é a decimal mais curta que o próprio float já carrega.
        # Decimal + "f" evita a precisão implícita de 6 casas de format(float, "f").
        return format(Decimal(str(value)), "f")
    return None


def num(value: float) -> float:
    return round(float(value), 2)


def bbox_from_edges(x0: float, y0: float, x1: float, y1: float) -> dict[str, float]:
    return {"x0": num(x0), "y0": num(y0), "x1": num(x1), "y1": num(y1)}


def union_bbox(boxes: list[dict[str, float]]) -> dict[str, float]:
    return {
        "x0": min(box["x0"] for box in boxes),
        "y0": min(box["y0"] for box in boxes),
        "x1": max(box["x1"] for box in boxes),
        "y1": max(box["y1"] for box in boxes),
    }


def coverage(
    *,
    pages_or_sheets: int,
    tables_detected: int,
    visual_ununderstood_pages: list[int],
    limitations: list[str],
) -> dict[str, Any]:
    unique: list[str] = []
    for item in limitations:
        if item not in unique:
            unique.append(item)
    complete = not visual_ununderstood_pages and not unique
    return {
        "complete": complete,
        "pages_or_sheets": pages_or_sheets,
        "tables_detected": tables_detected,
        "visual_ununderstood_pages": list(visual_ununderstood_pages),
        "limitations": unique,
    }


def document_shell(*, file_format: str, fingerprint: str, coverage_doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "document": {
            "format": file_format,
            "ir_version": IR_VERSION,
            "source_fingerprint_local": fingerprint,
            "coverage": coverage_doc,
        },
        "pages_or_sheets": [],
    }


def assign_block_ids(prefix: str, blocks: list[dict[str, Any]]) -> None:
    ordered = sorted(
        enumerate(blocks),
        key=lambda item: (item[1]["bbox"]["y0"], item[1]["bbox"]["x0"], item[0]),
    )
    blocks[:] = [block for _, block in ordered]
    for index, block in enumerate(blocks, start=1):
        block_id = f"{prefix}-b{index}"
        block["id"] = block_id
        cells = list(block.get("cells") or [])
        cells.sort(
            key=lambda cell: (
                cell.get("row") or 0,
                cell.get("column") or 0,
                cell["bbox"]["y0"],
                cell["bbox"]["x0"],
            )
        )
        block["cells"] = cells
        for cell_index, cell in enumerate(cells, start=1):
            cell["id"] = f"{block_id}-c{cell_index}"


def _add_rel(block: dict[str, Any], kind: str, target_id: str, **extra: Any) -> None:
    rel: dict[str, Any] = {"kind": kind, "target_id": target_id}
    rel.update(extra)
    rels = block.setdefault("relationships", [])
    if rel not in rels:
        rels.append(rel)


def _horiz_overlap(a: dict[str, float], b: dict[str, float]) -> float:
    return max(0.0, min(a["x1"], b["x1"]) - max(a["x0"], b["x0"]))


def _vert_overlap(a: dict[str, float], b: dict[str, float]) -> float:
    return max(0.0, min(a["y1"], b["y1"]) - max(a["y0"], b["y0"]))


def relate_blocks(blocks: list[dict[str, Any]], *, edge_mode: str) -> None:
    """Fatos geométricos entre blocos. Sem leitura comercial."""
    for block in blocks:
        block.setdefault("relationships", [])
        for cell in block.get("cells") or []:
            cell["relationships"] = [
                {"kind": REL_SAME_BLOCK, "target_id": block["id"]},
                {"kind": REL_INSIDE, "target_id": block["id"]},
            ]
        below = _nearest_below(block, blocks, edge_mode=edge_mode)
        if below is not None:
            _add_rel(block, REL_ABOVE, below["id"])
            _add_rel(below, REL_BELOW, block["id"])
        lateral = _nearest_lateral(block, blocks)
        if lateral is not None:
            _add_rel(block, REL_ADJACENT, lateral["id"])
            _add_rel(lateral, REL_ADJACENT, block["id"])


def _nearest_below(block: dict[str, Any], blocks: list[dict[str, Any]], *, edge_mode: str) -> dict[str, Any] | None:
    best: tuple[float, dict[str, Any]] | None = None
    bb = block["bbox"]
    for other in blocks:
        if other is block:
            continue
        ob = other["bbox"]
        if edge_mode == "inclusive":
            if not (bb["y1"] < ob["y0"] and _inclusive_span(bb["x0"], bb["x1"], ob["x0"], ob["x1"]) >= 1):
                continue
            gap = float(ob["y0"] - bb["y1"])
        else:
            overlap = _horiz_overlap(bb, ob)
            gap = float(ob["y0"] - bb["y1"])
            if overlap <= 0 or gap < -0.8:
                continue
        if best is None or gap < best[0]:
            best = (gap, other)
    return best[1] if best else None


def _nearest_lateral(block: dict[str, Any], blocks: list[dict[str, Any]]) -> dict[str, Any] | None:
    best: tuple[float, dict[str, Any]] | None = None
    bb = block["bbox"]
    for other in blocks:
        if other is block:
            continue
        ob = other["bbox"]
        if _vert_overlap(bb, ob) <= 2:
            continue
        if _horiz_overlap(bb, ob) > 2:
            continue
        if bb["x0"] >= ob["x1"]:
            gap = bb["x0"] - ob["x1"]
        elif ob["x0"] >= bb["x1"]:
            gap = ob["x0"] - bb["x1"]
        else:
            continue
        if gap > 80:
            continue
        if best is None or gap < best[0]:
            best = (gap, other)
    return best[1] if best else None


def _inclusive_span(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0) + 1)


def classify_free_block(block: dict[str, Any], blocks: list[dict[str, Any]]) -> str:
    if _is_lateral_note(block, blocks):
        return BLOCK_NOTE
    if _is_geometric_header(block, blocks):
        return BLOCK_HEADER
    return BLOCK_TEXT


def _is_lateral_note(block: dict[str, Any], blocks: list[dict[str, Any]]) -> bool:
    bb = block["bbox"]
    width = bb["x1"] - bb["x0"]
    for other in blocks:
        if other is block:
            continue
        ob = other["bbox"]
        other_width = ob["x1"] - ob["x0"]
        wider_anchor = other.get("type") == BLOCK_TABLE or other_width >= width * 1.4
        if not wider_anchor:
            continue
        if _vert_overlap(bb, ob) <= 2:
            continue
        if _horiz_overlap(bb, ob) > 2:
            continue
        if bb["x0"] >= ob["x1"]:
            gap = bb["x0"] - ob["x1"]
        elif ob["x0"] >= bb["x1"]:
            gap = ob["x0"] - bb["x1"]
        else:
            continue
        if gap <= 80:
            return True
    return False


def _is_geometric_header(block: dict[str, Any], blocks: list[dict[str, Any]]) -> bool:
    bb = block["bbox"]
    if bb["y1"] - bb["y0"] > 30:
        return False
    if len(block.get("cells") or []) > 2:
        return False
    width = bb["x1"] - bb["x0"]
    center = (bb["x0"] + bb["x1"]) / 2
    for other in blocks:
        if other is block:
            continue
        ob = other["bbox"]
        if bb["y1"] > ob["y0"] + 1:
            continue
        if not (ob["x0"] <= center <= ob["x1"]):
            continue
        other_width = ob["x1"] - ob["x0"]
        if other_width > 0 and width <= other_width * 0.6:
            return True
    return False


def metrics_for_ir(ir: dict[str, Any], *, elapsed_ms: int) -> dict[str, int]:
    pages = ir.get("pages_or_sheets") or []
    blocks = 0
    cells = 0
    tables = 0
    merges = 0
    comments = 0
    for page in pages:
        merges += len(page.get("merges") or [])
        for block in page.get("blocks") or []:
            blocks += 1
            if block.get("type") == BLOCK_TABLE:
                tables += 1
            for cell in block.get("cells") or []:
                cells += 1
                if cell.get("comment"):
                    comments += 1
    return {
        "pages_or_sheets": len(pages),
        "blocks": blocks,
        "cells": cells,
        "tables": tables,
        "merges": merges,
        "comments": comments,
        "elapsed_ms": int(elapsed_ms),
    }


def _column_index(letters: str) -> int:
    value = 0
    for char in letters:
        value = value * 26 + (ord(char) - 64)
    return value


def range_cell_area(ref: str) -> int | None:
    """Área em células de um intervalo ``A1:C1``. ``None`` se o ref não for fechado."""
    if not isinstance(ref, str) or not _RANGE_RE.match(ref):
        return None
    corners: list[tuple[int, int]] = []
    for part in ref.split(":"):
        match = _CELL_REF_RE.match(part)
        if match is None:
            return None
        column = _column_index(match.group(1))
        row = int(match.group(2))
        if not 1 <= column <= XLSX_MAX_COLUMN or not 1 <= row <= XLSX_MAX_ROW:
            return None
        corners.append((column, row))
    (column0, row0), (column1, row1) = corners
    return (abs(row1 - row0) + 1) * (abs(column1 - column0) + 1)


def _index_in(value: Any, limit: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= limit


def _finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def _positive_page_size(value: Any) -> bool:
    if not _finite_number(value):
        return False
    number = float(value)
    return 0 < number <= PDF_MAX_USER_UNIT


def _extra_and_reserved(keys: set[str], allowed: frozenset[str], *, outbound: bool) -> list[str]:
    errors: list[str] = []
    if outbound and keys & RESERVED_FINGERPRINT_KEYS:
        errors.append("fingerprint_external")
    extra = keys - allowed - RESERVED_FINGERPRINT_KEYS
    if extra & FORBIDDEN_SEMANTIC_KEYS:
        errors.append("semantic_inference")
    if extra - FORBIDDEN_SEMANTIC_KEYS:
        errors.append("unknown_field")
    return errors


def _document_number(value: Any, *, outbound: bool) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return "schema"
    if isinstance(value, Decimal) and not value.is_finite():
        return "schema"
    if outbound:
        return "document_scalar"
    return None


def _bounded_text(value: Any, *, outbound: bool, limit: int = 200_000) -> str | None:
    numeric = _document_number(value, outbound=outbound)
    if numeric:
        return numeric
    if not isinstance(value, str) or len(value) > limit:
        return "schema"
    return None


def _walk_strings(node: Any):
    if isinstance(node, dict):
        for key, value in node.items():
            yield ("key", key)
            yield from _walk_strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_strings(item)
    elif isinstance(node, str):
        yield ("value", node)


def ir_invariant_errors(ir: Any, *, outbound: bool = False) -> list[str]:
    """Invariantes estruturais. Não inspecionam semântica tarifária.

    ``outbound`` descreve a cópia que pode sair do processo: sem fingerprint
    local e sem escalar numérico em campo documental de célula.
    """
    errors: list[str] = []
    if not isinstance(ir, dict):
        return ["ir_not_object"]
    if set(ir) - _ROOT_KEYS:
        errors.append("dynamic_document_key")
    errors.extend(_extra_and_reserved(set(ir), _ROOT_KEYS, outbound=outbound))
    document = ir.get("document")
    pages = ir.get("pages_or_sheets")
    if not isinstance(document, dict):
        return ["missing_document"]
    if not isinstance(pages, list):
        errors.append("pages_or_sheets")
        pages = []
    if len(pages) > MAX_PAGES_OR_SHEETS:
        errors.append("pages_or_sheets")
    errors.extend(_document_errors(document, pages, outbound=outbound))
    file_format = document.get("format")
    block_count = 0
    table_count = 0
    for page in pages:
        if not isinstance(page, dict):
            errors.append("schema")
            continue
        kind = page.get("kind")
        if file_format == FORMAT_PDF and kind == KIND_PAGE:
            added, tables = _pdf_page_errors(page, page_count=len(pages), outbound=outbound)
        elif file_format == FORMAT_XLSX and kind == KIND_SHEET:
            added, tables = _xlsx_sheet_errors(page, outbound=outbound)
        else:
            added, tables = ["schema"], 0
        errors.extend(added)
        block_count += sum(1 for block in page.get("blocks") or [] if isinstance(block, dict))
        table_count += tables
    errors.extend(_coverage_errors(document.get("coverage"), page_count=len(pages), block_count=block_count, table_count=table_count))
    if _contains_binary(ir):
        errors.append("binary_content")
    for event in _walk_strings(ir):
        if event[0] == "key" and event[1] in FORBIDDEN_SEMANTIC_KEYS:
            errors.append("semantic_inference")
        elif event[0] == "key" and outbound and event[1] in RESERVED_FINGERPRINT_KEYS:
            errors.append("fingerprint_external")
        elif event[0] == "value" and (
            _PATH_RE.search(event[1]) or _DATA_URI_RE.search(event[1]) or _B64_RE.match(event[1])
        ):
            errors.append("local_or_encoded_payload")
    return list(dict.fromkeys(errors))


def _document_errors(document: dict[str, Any], pages: list[Any], *, outbound: bool) -> list[str]:
    allowed = set(_DOCUMENT_BASE_KEYS)
    if not outbound:
        allowed.add("source_fingerprint_local")
    errors = _extra_and_reserved(set(document), frozenset(allowed), outbound=outbound)
    if not _DOCUMENT_BASE_KEYS <= set(document):
        errors.append("schema")
    if document.get("ir_version") != IR_VERSION:
        errors.append("ir_version")
    if document.get("format") not in {FORMAT_PDF, FORMAT_XLSX}:
        errors.append("format")
    if outbound:
        if "source_fingerprint_local" in document:
            errors.append("fingerprint_external")
    else:
        fingerprint = document.get("source_fingerprint_local")
        if not isinstance(fingerprint, str) or not _FINGERPRINT_RE.match(fingerprint):
            errors.append("fingerprint")
    if not isinstance(document.get("coverage"), dict):
        errors.append("coverage")
    if not isinstance(pages, list):
        errors.append("pages_or_sheets")
    return errors


def _coverage_errors(coverage: Any, *, page_count: int, block_count: int, table_count: int) -> list[str]:
    if not isinstance(coverage, dict):
        return ["coverage"]
    errors = _extra_and_reserved(set(coverage), _COVERAGE_KEYS, outbound=False)
    if set(coverage) != _COVERAGE_KEYS:
        if not _COVERAGE_KEYS <= set(coverage):
            errors.append("coverage")
        return errors
    complete = coverage.get("complete")
    declared_pages = coverage.get("pages_or_sheets")
    tables = coverage.get("tables_detected")
    visual = coverage.get("visual_ununderstood_pages")
    limitations = coverage.get("limitations")
    if not isinstance(complete, bool):
        errors.append("coverage")
    if (
        isinstance(declared_pages, bool)
        or not isinstance(declared_pages, int)
        or declared_pages != page_count
        or declared_pages < 0
        or declared_pages > MAX_PAGES_OR_SHEETS
    ):
        errors.append("coverage")
    if isinstance(tables, bool) or not isinstance(tables, int) or tables < 0 or tables > block_count:
        errors.append("coverage")
    if not isinstance(visual, list):
        errors.append("coverage")
        visual = []
    else:
        limit = page_count if page_count else 0
        if any(not _index_in(item, limit) for item in visual):
            errors.append("coverage")
    if not isinstance(limitations, list) or any(item not in ALLOWED_LIMITATIONS for item in limitations):
        errors.append("coverage")
        limitations = []
    if isinstance(complete, bool) and complete is not (not visual and not limitations):
        errors.append("coverage")
    if isinstance(tables, int) and not isinstance(tables, bool) and tables != table_count and "coverage" not in errors:
        errors.append("coverage")
    return errors


def _pdf_page_errors(page: dict[str, Any], *, page_count: int, outbound: bool) -> tuple[list[str], int]:
    errors = _extra_and_reserved(set(page), _PAGE_KEYS, outbound=outbound)
    if not _PAGE_KEYS <= set(page):
        errors.append("schema")
    if page.get("kind") != KIND_PAGE or page.get("coordinate_space") != "top_origin":
        errors.append("schema")
    page_number = page.get("page_number")
    if not _index_in(page_number, page_count):
        errors.append("page_number")
    if type(page.get("rotation")) is not int or page.get("rotation") not in PDF_ROTATIONS:
        errors.append("rotation")
    dimensions = page.get("dimensions")
    width_limit = PDF_MAX_USER_UNIT
    height_limit = PDF_MAX_USER_UNIT
    if not isinstance(dimensions, dict) or set(dimensions) != {"width", "height"}:
        errors.append("dimensions")
    elif not _positive_page_size(dimensions.get("width")) or not _positive_page_size(dimensions.get("height")):
        errors.append("dimensions")
    else:
        width_limit = float(dimensions["width"])
        height_limit = float(dimensions["height"])
    blocks = page.get("blocks")
    tables = 0
    if not isinstance(blocks, list):
        errors.append("schema")
        return errors, 0
    for block in blocks:
        if not isinstance(block, dict):
            errors.append("schema")
            continue
        errors.extend(_pdf_block_errors(block, width_limit=width_limit, height_limit=height_limit, outbound=outbound))
        if block.get("type") == BLOCK_TABLE:
            tables += 1
    return errors, tables


def _pdf_bbox_ok(box: Any, *, width: float, height: float) -> bool:
    if not isinstance(box, dict) or set(box) != set(_BBOX_KEYS):
        return False
    if any(not _finite_number(box[key]) for key in _BBOX_KEYS):
        return False
    x0, y0, x1, y1 = (float(box[key]) for key in _BBOX_KEYS)
    if x0 > x1 + 0.05 or y0 > y1 + 0.05:
        return False
    tol = PDF_BBOX_TOLERANCE
    return (
        -tol <= x0 <= width + tol
        and -tol <= x1 <= width + tol
        and -tol <= y0 <= height + tol
        and -tol <= y1 <= height + tol
    )


def _inside_bbox(inner: dict[str, Any], outer: dict[str, Any], *, tolerance: float) -> bool:
    try:
        return (
            float(inner["x0"]) >= float(outer["x0"]) - tolerance
            and float(inner["y0"]) >= float(outer["y0"]) - tolerance
            and float(inner["x1"]) <= float(outer["x1"]) + tolerance
            and float(inner["y1"]) <= float(outer["y1"]) + tolerance
        )
    except (TypeError, ValueError, KeyError):
        return False


def _pdf_block_errors(block: dict[str, Any], *, width_limit: float, height_limit: float, outbound: bool) -> list[str]:
    errors = _extra_and_reserved(set(block), _PDF_BLOCK_KEYS, outbound=outbound)
    if not _PDF_BLOCK_KEYS <= set(block):
        errors.append("schema")
    if block.get("type") not in _BLOCK_TYPES:
        errors.append("schema")
    if not isinstance(block.get("id"), str) or not _ID_RE.match(block.get("id") or ""):
        errors.append("technical_id")
    text_error = _bounded_text(block.get("text"), outbound=outbound)
    if text_error:
        errors.append(text_error)
    bbox = block.get("bbox")
    if not _pdf_bbox_ok(bbox, width=width_limit, height=height_limit):
        errors.append("bbox")
    errors.extend(_provenance_errors(block.get("provenance"), extractor="pdfplumber", methods=_PDF_METHODS))
    errors.extend(_relationship_list_errors(block.get("relationships"), outbound=outbound))
    cells = block.get("cells")
    if not isinstance(cells, list):
        errors.append("schema")
        return errors
    for cell in cells:
        if not isinstance(cell, dict):
            errors.append("schema")
            continue
        errors.extend(
            _pdf_cell_errors(
                cell,
                block_bbox=bbox if isinstance(bbox, dict) else None,
                width_limit=width_limit,
                height_limit=height_limit,
                outbound=outbound,
            )
        )
    return errors


def _pdf_cell_errors(
    cell: dict[str, Any],
    *,
    block_bbox: dict[str, Any] | None,
    width_limit: float,
    height_limit: float,
    outbound: bool,
) -> list[str]:
    errors = _extra_and_reserved(set(cell), _PDF_CELL_KEYS, outbound=outbound)
    if not _PDF_CELL_KEYS <= set(cell):
        errors.append("schema")
    if not isinstance(cell.get("id"), str) or not _ID_RE.match(cell.get("id") or ""):
        errors.append("technical_id")
    if not _index_in(cell.get("row"), PDF_MAX_AXIS_INDEX) or not _index_in(cell.get("column"), PDF_MAX_AXIS_INDEX):
        errors.append("row_column")
    text_error = _bounded_text(cell.get("text"), outbound=outbound)
    if text_error:
        errors.append(text_error)
    bbox = cell.get("bbox")
    if not _pdf_bbox_ok(bbox, width=width_limit, height=height_limit):
        errors.append("bbox")
    elif block_bbox is not None and not _inside_bbox(bbox, block_bbox, tolerance=PDF_BBOX_TOLERANCE):
        errors.append("bbox")
    errors.extend(_relationship_list_errors(cell.get("relationships"), outbound=outbound))
    return errors


def _xlsx_sheet_errors(sheet: dict[str, Any], *, outbound: bool) -> tuple[list[str], int]:
    errors = _extra_and_reserved(set(sheet), _SHEET_KEYS, outbound=outbound)
    if not _SHEET_KEYS <= set(sheet):
        errors.append("schema")
    if sheet.get("kind") != KIND_SHEET or sheet.get("coordinate_space") != "cell_index_inclusive":
        errors.append("schema")
    if not isinstance(sheet.get("sheet_id"), str) or not _ID_RE.match(sheet.get("sheet_id") or ""):
        errors.append("technical_id")
    if not _sheet_name_ok(sheet.get("sheet_name")):
        errors.append("sheet_name_value")
    if sheet.get("visibility") not in _SHEET_VISIBILITY:
        errors.append("schema")
    boundary_error = _boundaries_error(sheet.get("boundaries"))
    if boundary_error:
        errors.append(boundary_error)
    hidden_rows = sheet.get("hidden_rows")
    if not isinstance(hidden_rows, list) or len(hidden_rows) > MAX_GAP_ITEMS or any(not _index_in(item, XLSX_MAX_ROW) for item in hidden_rows or []):
        errors.append("hidden_rows")
    hidden_columns = sheet.get("hidden_columns")
    if (
        not isinstance(hidden_columns, list)
        or len(hidden_columns) > XLSX_MAX_COLUMN
        or any(not _hidden_column_ok(item) for item in hidden_columns or [])
    ):
        errors.append("hidden_columns")
    merges = sheet.get("merges")
    if not isinstance(merges, list):
        errors.append("range")
        merges = []
    area_total = 0
    for merge in merges:
        merge_errors, area = _merge_errors(merge)
        errors.extend(merge_errors)
        if area is not None:
            area_total += area
    if area_total > MAX_MERGE_AREA_TOTAL:
        errors.append("range")
    gaps = sheet.get("gaps")
    if not isinstance(gaps, list) or len(gaps) > MAX_GAP_ITEMS:
        errors.append("schema")
    else:
        for gap in gaps:
            gap_error = _gap_error(gap)
            if gap_error:
                errors.append(gap_error)
    blocks = sheet.get("blocks")
    tables = 0
    cell_count = 0
    if not isinstance(blocks, list):
        errors.append("schema")
        return errors, 0
    for block in blocks:
        if not isinstance(block, dict):
            errors.append("schema")
            continue
        errors.extend(_xlsx_block_errors(block, boundaries=sheet.get("boundaries"), outbound=outbound))
        if block.get("type") == BLOCK_TABLE:
            tables += 1
        cell_count += sum(1 for cell in block.get("cells") or [] if isinstance(cell, dict))
    if cell_count and sheet.get("boundaries") is None:
        errors.append("schema")
    return errors, tables


def _sheet_name_ok(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 31:
        return False
    if any(ord(char) < 32 or char in _SHEET_NAME_FORBIDDEN for char in value):
        return False
    return True


def _hidden_column_ok(value: Any) -> bool:
    if not isinstance(value, str) or not _COLUMN_RANGE_RE.match(value):
        return False
    indexes = [_column_index(part) for part in value.split(":")]
    return all(1 <= index <= XLSX_MAX_COLUMN for index in indexes) and indexes[0] <= indexes[-1]


def _boundaries_error(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        return "schema"
    if set(value) != _BOUNDARY_KEYS:
        return "unknown_field" if set(value) - _BOUNDARY_KEYS else "schema"
    min_row = value.get("min_row")
    max_row = value.get("max_row")
    min_column = value.get("min_column")
    max_column = value.get("max_column")
    if not all(
        (
            _index_in(min_row, XLSX_MAX_ROW),
            _index_in(max_row, XLSX_MAX_ROW),
            _index_in(min_column, XLSX_MAX_COLUMN),
            _index_in(max_column, XLSX_MAX_COLUMN),
        )
    ):
        return "row_column"
    if min_row > max_row or min_column > max_column:
        return "row_column"
    return None


def _gap_error(gap: Any) -> str | None:
    if not isinstance(gap, dict):
        return "schema"
    keys = set(gap) - RESERVED_FINGERPRINT_KEYS
    axis = gap.get("axis")
    if axis not in {"row", "column"}:
        return "schema"
    limit = XLSX_MAX_ROW if axis == "row" else XLSX_MAX_COLUMN
    if keys == _GAP_INDEX_KEYS:
        if not _index_in(gap.get("index"), limit):
            return "row_column"
        return None
    if keys == _GAP_COLLAPSED_KEYS:
        if gap.get("collapsed") is not True:
            return "schema"
        if not _index_in(gap.get("start"), limit) or not _index_in(gap.get("end"), limit):
            return "row_column"
        if gap["start"] > gap["end"]:
            return "row_column"
        return None
    if keys - (_GAP_INDEX_KEYS | _GAP_COLLAPSED_KEYS):
        return "unknown_field"
    return "schema"


def _merge_errors(merge: Any) -> tuple[list[str], int | None]:
    if not isinstance(merge, dict):
        return ["range"], None
    errors = _extra_and_reserved(set(merge), _MERGE_KEYS, outbound=False)
    if not _MERGE_KEYS <= set(merge):
        errors.append("range")
    ref = merge.get("range")
    area = range_cell_area(ref) if isinstance(ref, str) else None
    if area is None or area < 2 or area > MAX_MERGE_AREA_ONE:
        errors.append("range")
    anchor = merge.get("anchor")
    if not isinstance(anchor, str) or not _COORD_RE.match(anchor):
        errors.append("coordinate")
    return errors, area


def _xlsx_block_errors(block: dict[str, Any], *, boundaries: Any, outbound: bool) -> list[str]:
    errors = _extra_and_reserved(set(block), _XLSX_BLOCK_KEYS, outbound=outbound)
    if not _XLSX_BLOCK_KEYS <= set(block):
        errors.append("schema")
    if block.get("type") not in _BLOCK_TYPES:
        errors.append("schema")
    if not isinstance(block.get("id"), str) or not _ID_RE.match(block.get("id") or ""):
        errors.append("technical_id")
    bbox = block.get("bbox")
    if not _xlsx_bbox_ok(bbox):
        errors.append("bbox")
    elif isinstance(boundaries, dict) and not _xlsx_bbox_inside_boundaries(bbox, boundaries):
        errors.append("bbox")
    errors.extend(_provenance_errors(block.get("provenance"), extractor="openpyxl", methods=_XLSX_METHODS))
    errors.extend(_relationship_list_errors(block.get("relationships"), outbound=outbound))
    cells = block.get("cells")
    if not isinstance(cells, list):
        errors.append("schema")
        return errors
    for cell in cells:
        if not isinstance(cell, dict):
            errors.append("schema")
            continue
        errors.extend(_xlsx_cell_errors(cell, block_bbox=bbox if isinstance(bbox, dict) else None, boundaries=boundaries, outbound=outbound))
    return errors


def _xlsx_bbox_ok(box: Any) -> bool:
    if not isinstance(box, dict) or set(box) != set(_BBOX_KEYS):
        return False
    return (
        _index_in(box.get("x0"), XLSX_MAX_COLUMN)
        and _index_in(box.get("x1"), XLSX_MAX_COLUMN)
        and _index_in(box.get("y0"), XLSX_MAX_ROW)
        and _index_in(box.get("y1"), XLSX_MAX_ROW)
        and box["x0"] <= box["x1"]
        and box["y0"] <= box["y1"]
    )


def _xlsx_bbox_inside_boundaries(box: dict[str, Any], boundaries: dict[str, Any]) -> bool:
    try:
        return (
            boundaries["min_column"] <= box["x0"] <= box["x1"] <= boundaries["max_column"]
            and boundaries["min_row"] <= box["y0"] <= box["y1"] <= boundaries["max_row"]
        )
    except (TypeError, KeyError):
        return False


def _xlsx_cell_errors(
    cell: dict[str, Any],
    *,
    block_bbox: dict[str, Any] | None,
    boundaries: Any,
    outbound: bool,
) -> list[str]:
    allowed = _XLSX_CELL_REQUIRED | _XLSX_CELL_OPTIONAL
    errors = _extra_and_reserved(set(cell), allowed, outbound=outbound)
    if not _XLSX_CELL_REQUIRED <= set(cell):
        errors.append("schema")
    if not isinstance(cell.get("id"), str) or not _ID_RE.match(cell.get("id") or ""):
        errors.append("technical_id")
    row = cell.get("row")
    column = cell.get("column")
    if not _index_in(row, XLSX_MAX_ROW) or not _index_in(column, XLSX_MAX_COLUMN):
        errors.append("row_column")
    elif isinstance(boundaries, dict):
        min_row = boundaries.get("min_row")
        max_row = boundaries.get("max_row")
        min_column = boundaries.get("min_column")
        max_column = boundaries.get("max_column")
        if type(min_row) is int and type(max_row) is int and not min_row <= row <= max_row:
            errors.append("row_column")
        if type(min_column) is int and type(max_column) is int and not min_column <= column <= max_column:
            errors.append("row_column")
    coordinate = cell.get("coordinate")
    if not isinstance(coordinate, str) or not _COORD_RE.match(coordinate):
        errors.append("coordinate")
    if cell.get("value_type") not in _VALUE_TYPES:
        errors.append("schema")
    value_error = _cell_scalar_error(cell.get("value"), outbound=outbound)
    if value_error:
        errors.append(value_error)
    bbox = cell.get("bbox")
    if not _xlsx_bbox_ok(bbox):
        errors.append("bbox")
    elif _index_in(row, XLSX_MAX_ROW) and _index_in(column, XLSX_MAX_COLUMN):
        if bbox["x0"] != column or bbox["x1"] != column or bbox["y0"] != row or bbox["y1"] != row:
            errors.append("bbox")
        elif block_bbox is not None and not _inside_bbox(bbox, block_bbox, tolerance=0):
            errors.append("bbox")
    for field in ("formula", "comment"):
        if field not in cell:
            continue
        field_error = _bounded_text(cell.get(field), outbound=outbound, limit=8000)
        if field_error:
            errors.append(field_error)
    if "cached_value" in cell:
        cached_error = _cell_scalar_error(cell.get("cached_value"), outbound=outbound)
        if cached_error:
            errors.append(cached_error)
    if "style_ref" in cell:
        style_error = _style_error(cell.get("style_ref"))
        if style_error:
            errors.append(style_error)
    if "hidden" in cell and cell.get("hidden") is not True:
        errors.append("schema")
    errors.extend(_relationship_list_errors(cell.get("relationships"), outbound=outbound))
    return errors


def _cell_scalar_error(value: Any, *, outbound: bool) -> str | None:
    if value is None or isinstance(value, bool) or isinstance(value, str):
        if isinstance(value, str) and len(value) > 200_000:
            return "schema"
        return None
    if isinstance(value, (int, float, Decimal)):
        return _document_number(value, outbound=outbound)
    return "schema"


def _style_error(value: Any) -> str | None:
    if not isinstance(value, dict) or not value:
        return "unknown_field"
    if set(value) - _STYLE_KEYS:
        return "unknown_field"
    number_format = value.get("number_format")
    if "number_format" in value and (
        not isinstance(number_format, str) or not number_format or len(number_format) > 80 or any(ord(char) < 32 for char in number_format)
    ):
        return "schema"
    if "bold" in value and not isinstance(value.get("bold"), bool):
        return "schema"
    if "italic" in value and not isinstance(value.get("italic"), bool):
        return "schema"
    fill = value.get("fill_pattern")
    if "fill_pattern" in value and (not isinstance(fill, str) or not fill or len(fill) > 40):
        return "schema"
    return None


def _provenance_errors(value: Any, *, extractor: str, methods: frozenset[str]) -> list[str]:
    if not isinstance(value, dict):
        return ["schema"]
    errors = _extra_and_reserved(set(value), _PROVENANCE_KEYS, outbound=False)
    if set(value) != _PROVENANCE_KEYS:
        if not _PROVENANCE_KEYS <= set(value):
            errors.append("schema")
        return errors
    if value.get("extractor") != extractor or value.get("method") not in methods:
        errors.append("schema")
    return errors


def _relationship_list_errors(value: Any, *, outbound: bool) -> list[str]:
    if not isinstance(value, list):
        return ["relationship_kind"]
    errors: list[str] = []
    for rel in value:
        errors.extend(_relationship_errors(rel, outbound=outbound))
    return errors


def _relationship_errors(rel: Any, *, outbound: bool) -> list[str]:
    if not isinstance(rel, dict):
        return ["relationship_kind"]
    errors = _extra_and_reserved(set(rel), _REL_REQUIRED | _REL_OPTIONAL, outbound=outbound)
    if not _REL_REQUIRED <= set(rel):
        errors.append("relationship_kind")
    if rel.get("kind") not in ALLOWED_RELATION_KINDS:
        errors.append("relationship_kind")
    target = rel.get("target_id")
    if not isinstance(target, str) or not _ID_RE.match(target):
        errors.append("technical_id")
    if "range" in rel:
        area = range_cell_area(rel.get("range")) if isinstance(rel.get("range"), str) else None
        if area is None or area < 2 or area > MAX_MERGE_AREA_ONE:
            errors.append("range")
    if "anchor" in rel:
        anchor = rel.get("anchor")
        if not isinstance(anchor, str) or not _COORD_RE.match(anchor):
            errors.append("coordinate")
    if "columns" in rel and not _index_in(rel.get("columns"), XLSX_MAX_COLUMN):
        errors.append("row_column")
    return errors


def _contains_binary(node: Any) -> bool:
    if isinstance(node, (bytes, bytearray, memoryview)):
        return True
    if isinstance(node, dict):
        return any(_contains_binary(key) or _contains_binary(value) for key, value in node.items())
    if isinstance(node, (list, tuple)):
        return any(_contains_binary(item) for item in node)
    return False
