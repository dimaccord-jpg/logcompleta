"""
Governança do IR antes do provider.

O dict é minimizado no caminho estruturado. Escalares de célula viram texto
antes dessa classificação: geometria continua numérica e não segue o mesmo
caminho. A cópia segura serializada segue pelo wrapper já existente.
Falha local não chama o provider.
"""
from __future__ import annotations

import copy
import json
from decimal import Decimal
from typing import Any

from app.services.cleiton_billable_ai_call import (
    BillableAiAdmissionBlocked,
    BillableAiGovernanceBlocked,
    BillableAiUncertainError,
    cleiton_governed_billable_ai_call,
)
from app.services.cleiton_ai_data_governance import (
    CONTENT_TYPE_DOCUMENT,
    DECISION_BLOCK,
    PURPOSE_EXTRACAO_TABELA,
    CleitonAiGovernanceBlockedError,
    govern_outbound_content,
)
from app.services.document_semantic_ir.structure import (
    DocumentSemanticIrError,
    StructuralDispatch,
    ir_invariant_errors,
    render_document_scalar,
)

_DOCUMENT_FIELDS = ("value", "cached_value", "text", "comment", "formula")

_PATH_MARKERS = ("data:image", "data:application", "%PDF-", "file://")


def _plain_label(cell: dict[str, Any]) -> str:
    if cell.get("value_type") == "formula":
        return ""
    for key in ("text", "value"):
        raw = cell.get(key)
        if isinstance(raw, str) and raw.strip():
            return " ".join(raw.split())
    return ""


def _context_label(sheet: dict[str, Any], cell: dict[str, Any]) -> str:
    """Rótulo já extraído ao lado ou acima, para a janela do classificador existente."""
    row = cell.get("row")
    column = cell.get("column")
    if not isinstance(row, int) or isinstance(row, bool) or not isinstance(column, int) or isinstance(column, bool):
        return ""
    left: tuple[int, str] | None = None
    above: tuple[int, str] | None = None
    for block in sheet.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        for other in block.get("cells") or []:
            if not isinstance(other, dict) or other is cell:
                continue
            label = _plain_label(other)
            if not label:
                continue
            other_row = other.get("row")
            other_column = other.get("column")
            if other_row == row and isinstance(other_column, int) and not isinstance(other_column, bool) and other_column < column:
                if left is None or other_column > left[0]:
                    left = (other_column, label)
            if other_column == column and isinstance(other_row, int) and not isinstance(other_row, bool) and other_row < row:
                if above is None or other_row > above[0]:
                    above = (other_row, label)
    chosen = left[1] if left else (above[1] if above else "")
    return chosen[:40]


def _classify_document_text(text: str, context: str, *, agent: str) -> tuple[str | None, list[str]]:
    probe = f"{context}\n{text}" if context else text
    governed = govern_outbound_content(
        probe,
        purpose=PURPOSE_EXTRACAO_TABELA,
        content_type=CONTENT_TYPE_DOCUMENT,
        agent=agent,
        provider="structural_ir_local",
    )
    if governed.decision == DECISION_BLOCK or not isinstance(governed.safe_content, str):
        return None, list(governed.reason_codes) or ["governance_blocked"]
    safe = governed.safe_content
    if safe == text or (context and safe.endswith("\n" + text)):
        return text, []
    return safe.rsplit("\n", 1)[-1], []


def _render_document_fields(external: dict[str, Any], *, agent: str) -> list[str]:
    for sheet in external.get("pages_or_sheets") or []:
        if not isinstance(sheet, dict):
            continue
        cells: list[dict[str, Any]] = []
        for block in sheet.get("blocks") or []:
            if not isinstance(block, dict):
                continue
            for cell in block.get("cells") or []:
                if isinstance(cell, dict):
                    cells.append(cell)
        labels = {id(cell): _context_label(sheet, cell) for cell in cells}
        for cell in cells:
            context = labels[id(cell)]
            for field in _DOCUMENT_FIELDS:
                raw = cell.get(field)
                if isinstance(raw, bool) or not isinstance(raw, (int, float, Decimal)):
                    continue
                rendered = render_document_scalar(raw)
                if rendered is None:
                    return ["non_finite_document_number"]
                safe, reasons = _classify_document_text(rendered, context, agent=agent)
                if safe is None:
                    return reasons
                cell[field] = safe
    return []


def _strip_reserved_fingerprints(node: Any) -> None:
    """Remove o fingerprint do slice em qualquer profundidade. Não mexe em chave genérica."""
    if isinstance(node, dict):
        for key in list(node):
            if key == "source_fingerprint_local":
                node.pop(key, None)
            else:
                _strip_reserved_fingerprints(node[key])
    elif isinstance(node, list):
        for item in node:
            _strip_reserved_fingerprints(item)


def prepare_outbound_ir(ir: dict[str, Any], *, agent: str) -> tuple[dict[str, Any] | None, list[str]]:
    """Cópia externa: sem fingerprint e com valores de célula já submetidos à governança."""
    external = copy.deepcopy(ir)
    _strip_reserved_fingerprints(external)
    reasons = _render_document_fields(external, agent=agent)
    if reasons:
        return None, reasons
    return external, []


def serialize_safe_ir(safe: Any) -> str:
    if ir_invariant_errors(safe, outbound=True):
        raise DocumentSemanticIrError("safe_ir_invariants")
    text = json.dumps(
        safe,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    lowered = text.lower()
    if any(marker.lower() in lowered for marker in _PATH_MARKERS):
        raise DocumentSemanticIrError("safe_ir_invariants")
    return text


def dispatch_structural_ir(
    client: Any,
    ir: dict[str, Any],
    *,
    model: str,
    agent: str = "agente_compara",
    flow_type: str = "agente_compara_document_semantic_ir",
    api_key_label: str = "structural_ir",
    extraction_metrics: dict[str, Any] | None = None,
    usuario: Any = None,
    conta_id: int | None = None,
    franquia_id: int | None = None,
    usuario_id: int | None = None,
    origem_sistema: bool = False,
    attempt_key: str | None = None,
) -> StructuralDispatch:
    metrics: dict[str, Any] = dict(extraction_metrics or {})
    errors = ir_invariant_errors(ir)
    if errors:
        return _blocked(metrics, errors)
    external, prepare_reasons = prepare_outbound_ir(ir, agent=agent)
    if external is None:
        return _blocked(metrics, prepare_reasons or ["governance_blocked"])
    outbound_errors = ir_invariant_errors(external, outbound=True)
    if outbound_errors:
        return _blocked(metrics, outbound_errors)
    governed = govern_outbound_content(
        external,
        purpose=PURPOSE_EXTRACAO_TABELA,
        content_type=CONTENT_TYPE_DOCUMENT,
        agent=agent,
        provider="structural_ir_local",
    )
    if governed.decision == DECISION_BLOCK or governed.safe_content is None:
        return _blocked(metrics, list(governed.reason_codes) or ["governance_blocked"])
    try:
        safe_json = serialize_safe_ir(governed.safe_content)
    except DocumentSemanticIrError:
        return _blocked(metrics, ["safe_ir_invariants"])
    metrics["chars_safe"] = len(safe_json)
    metrics["serialized_bytes"] = len(safe_json.encode("utf-8"))
    try:
        result = cleiton_governed_billable_ai_call(
            client,
            model=model,
            contents=safe_json,
            config=None,
            agent=agent,
            flow_type=flow_type,
            api_key_label=api_key_label,
            purpose=PURPOSE_EXTRACAO_TABELA,
            usuario=usuario,
            conta_id=conta_id,
            franquia_id=franquia_id,
            usuario_id=usuario_id,
            origem_sistema=origem_sistema,
            attempt_key=attempt_key,
        )
    except BillableAiGovernanceBlocked:
        return _blocked(metrics, ["governance_blocked"])
    except CleitonAiGovernanceBlockedError as exc:
        return _blocked(metrics, list(exc.reason_codes) or ["governance_blocked"])
    except BillableAiAdmissionBlocked as exc:
        return _blocked(metrics, [exc.motivo or "admission_blocked"])
    except BillableAiUncertainError as exc:
        metrics["ia_call_status"] = "uncertain"
        return StructuralDispatch(
            provider_called=bool(exc.provider_called),
            decision=governed.decision,
            safe_json=None,
            metrics=metrics,
            reason_codes=tuple([*governed.reason_codes, "consumo_incerto"]),
            response=None,
        )
    if not result.complete:
        metrics["ia_call_status"] = result.status
        return StructuralDispatch(
            provider_called=True,
            decision=governed.decision,
            safe_json=None,
            metrics=metrics,
            reason_codes=tuple([*governed.reason_codes, "consumo_incerto"]),
            response=None,
        )
    return StructuralDispatch(
        provider_called=True,
        decision=governed.decision,
        safe_json=safe_json,
        metrics=metrics,
        reason_codes=tuple(governed.reason_codes),
        response=result.response,
    )


def _blocked(metrics: dict[str, Any], reason_codes: list[str]) -> StructuralDispatch:
    return StructuralDispatch(
        provider_called=False,
        decision=DECISION_BLOCK,
        safe_json=None,
        metrics=metrics,
        reason_codes=tuple(reason_codes),
        response=None,
    )
