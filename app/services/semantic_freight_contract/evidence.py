"""Evidência e fingerprint local. Ponteiro para o IR, sem cópia do documento."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from decimal import Decimal
from typing import Any

from app.services.semantic_freight_contract.models import FINGERPRINT_KEYS, MAX_SNIPPET_CHARS

_DECIMAL_RE = re.compile(r"^(?:0|[1-9]\d*)(?:\.\d+)?$")


def normalize_identity_text(value: str) -> str:
    """Normaliza um nome já estruturado. Não separa rótulos compostos."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.upper()
    text = re.sub(r"[^A-Z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def local_fingerprint(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def strip_local_fingerprints(node: Any) -> Any:
    """Cópia sem fingerprints. O provider não recebe esses campos."""
    if isinstance(node, dict):
        return {
            key: strip_local_fingerprints(value)
            for key, value in node.items()
            if key not in FINGERPRINT_KEYS
        }
    if isinstance(node, list):
        return [strip_local_fingerprints(item) for item in node]
    return node


def parse_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return None
    if isinstance(value, str) and _DECIMAL_RE.match(value.strip()):
        return Decimal(value.strip())
    return None


def index_ir(ir: dict[str, Any]) -> dict[str, Any]:
    pages: set[int] = set()
    sheets: set[str] = set()
    blocks: set[str] = set()
    cells: set[str] = set()
    coordinates: dict[str, set[str]] = {}
    cell_parent: dict[str, str] = {}
    block_sheet: dict[str, str] = {}
    block_page: dict[str, int] = {}
    for surface in ir.get("pages_or_sheets") or []:
        if not isinstance(surface, dict):
            continue
        page_number = surface.get("page_number")
        sheet_id = surface.get("sheet_id")
        if isinstance(page_number, int) and not isinstance(page_number, bool):
            pages.add(page_number)
        if isinstance(sheet_id, str) and sheet_id:
            sheets.add(sheet_id)
        for block in surface.get("blocks") or []:
            if not isinstance(block, dict):
                continue
            block_id = block.get("id")
            if not isinstance(block_id, str) or not block_id:
                continue
            blocks.add(block_id)
            if isinstance(sheet_id, str):
                block_sheet[block_id] = sheet_id
            if isinstance(page_number, int) and not isinstance(page_number, bool):
                block_page[block_id] = page_number
            for cell in block.get("cells") or []:
                if not isinstance(cell, dict):
                    continue
                cell_id = cell.get("id")
                if not isinstance(cell_id, str) or not cell_id:
                    continue
                cells.add(cell_id)
                cell_parent[cell_id] = block_id
                coordinate = cell.get("coordinate")
                if isinstance(coordinate, str) and coordinate:
                    coordinates.setdefault(block_id, set()).add(coordinate)
    document = ir.get("document") if isinstance(ir.get("document"), dict) else {}
    coverage = document.get("coverage") if isinstance(document.get("coverage"), dict) else {}
    return {
        "pages": pages,
        "sheets": sheets,
        "blocks": blocks,
        "cells": cells,
        "coordinates": coordinates,
        "cell_parent": cell_parent,
        "block_sheet": block_sheet,
        "block_page": block_page,
        "fingerprint": document.get("source_fingerprint_local"),
        "ir_version": document.get("ir_version"),
        "coverage_complete": coverage.get("complete") is True and not coverage.get("limitations"),
        "limitations": list(coverage.get("limitations") or []),
    }


def evidence_pointer_errors(pointer: dict[str, Any], ir_index: dict[str, Any], *, document_id: str | None) -> list[str]:
    errors: list[str] = []
    snippet = pointer.get("snippet")
    if isinstance(snippet, str) and len(snippet) > MAX_SNIPPET_CHARS:
        errors.append("snippet_too_long")
    if document_id and pointer.get("document_id") not in {None, document_id}:
        errors.append("document_id_mismatch")
    located = False
    cell_id = pointer.get("cell_id")
    block_id = pointer.get("block_id")
    sheet_id = pointer.get("sheet_id")
    page_number = pointer.get("page_number")
    coordinate = pointer.get("coordinate")
    if isinstance(cell_id, str) and cell_id:
        located = True
        if cell_id not in ir_index["cells"]:
            errors.append("cell_not_in_ir")
        else:
            parent = ir_index["cell_parent"].get(cell_id)
            if isinstance(block_id, str) and block_id and parent != block_id:
                errors.append("cell_block_mismatch")
            block_id = parent or block_id
    if isinstance(block_id, str) and block_id:
        located = True
        if block_id not in ir_index["blocks"]:
            errors.append("block_not_in_ir")
    if isinstance(sheet_id, str) and sheet_id:
        located = True
        if sheet_id not in ir_index["sheets"]:
            errors.append("sheet_not_in_ir")
        elif isinstance(block_id, str) and block_id and ir_index["block_sheet"].get(block_id) not in {None, sheet_id}:
            errors.append("block_sheet_mismatch")
    if isinstance(page_number, int) and not isinstance(page_number, bool):
        located = True
        if page_number not in ir_index["pages"]:
            errors.append("page_not_in_ir")
    if isinstance(coordinate, str) and coordinate and isinstance(block_id, str) and block_id in ir_index["blocks"]:
        known = ir_index["coordinates"].get(block_id) or set()
        if known and coordinate not in known:
            errors.append("coordinate_not_in_ir")
    if not located:
        errors.append("evidence_without_locator")
    return errors


def evidence_fingerprint(evidence: list[dict[str, Any]]) -> str:
    projection = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        projection.append(
            {
                "ref": item.get("ref") or item.get("evidence_id"),
                "document_id": item.get("document_id"),
                "block_id": item.get("block_id"),
                "cell_id": item.get("cell_id"),
                "sheet_id": item.get("sheet_id"),
                "page_number": item.get("page_number"),
                "coordinate": item.get("coordinate"),
                "role": item.get("role"),
                "snippet": item.get("snippet"),
            }
        )
    return local_fingerprint(projection)
