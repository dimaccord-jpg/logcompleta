"""Persistência isolada no JSON técnico da temp table.

O formulário de frete não grava nem apaga este campo. Revisão humana
permanece no contrato anterior quando um novo attach chega.
O TTL é o da temp table; este módulo não estende a expiração.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import Any

from app.services.semantic_freight_contract.compiler import compile_pricing_preview
from app.services.semantic_freight_contract.evidence import normalize_identity_text, parse_decimal
from app.services.semantic_freight_contract.models import TECHNICAL_JSON_KEY
from app.services.semantic_freight_contract.validator import dimension_meaning_key, entity_identity

_SUBJECT_COLLECTIONS = (
    ("entities", "entity_id"),
    ("pricing_dimensions", "dimension_id"),
    ("lanes", "lane_id"),
    ("price_rules", "rule_id"),
    ("commercial_conditions", "condition_id"),
)

class SemanticFreightPersistenceError(ValueError):
    """Falha local de persistência. Não altera o pricing_contract operacional."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def align_contract_ttl(contract: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    updated = copy.deepcopy(contract)
    bindings = updated.get("source_bindings") if isinstance(updated.get("source_bindings"), dict) else {}
    bindings["expires_at"] = record.get("expires_at")
    bindings["ttl_follows"] = "temp_table"
    bindings["source_document_ids"] = list(record.get("source_documents") or [])
    updated["source_bindings"] = bindings
    return updated


def _subject_index(contract: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    found: dict[str, tuple[str, dict[str, Any]]] = {}
    for collection, id_field in _SUBJECT_COLLECTIONS:
        for item in contract.get(collection) or []:
            if not isinstance(item, dict):
                continue
            for key in (id_field, "ref", "temp_ref"):
                value = item.get(key)
                if isinstance(value, str) and value and value not in found:
                    found[value] = (collection, item)
    return found


def _entity_semantic(item: dict[str, Any]) -> tuple[Any, ...]:
    identity = entity_identity(item)
    if identity is not None:
        return identity
    return (
        item.get("kind"),
        str(item.get("country") or "").upper(),
        str(item.get("state") or "").upper(),
        normalize_identity_text(str(item.get("name") or "")),
    )


def _resolve_entity(index: dict[str, tuple[str, dict[str, Any]]], ref: Any) -> tuple[Any, ...] | None:
    if not isinstance(ref, str) or not ref:
        return None
    found = index.get(ref)
    if found is not None and found[0] == "entities":
        return _entity_semantic(found[1])
    return ("ref", ref)


def _dimension_semantic(index: dict[str, tuple[str, dict[str, Any]]], item: dict[str, Any]) -> tuple[Any, ...]:
    entity_ref = item.get("destination_entity_id") or item.get("destination_entity_ref") or item.get("entity_ref")
    return (
        "dimension",
        item.get("kind"),
        normalize_identity_text(str(item.get("source_label") or "")),
        _resolve_entity(index, entity_ref),
        dimension_meaning_key(item),
        str(item.get("state") or "").upper(),
    )


def _lane_semantic(index: dict[str, tuple[str, dict[str, Any]]], lane: dict[str, Any]) -> tuple[Any, ...]:
    origin = lane.get("origin") if isinstance(lane.get("origin"), dict) else {}
    destination = lane.get("destination") if isinstance(lane.get("destination"), dict) else {}
    origin_refs = tuple(
        sorted(
            (str(_resolve_entity(index, ref)) for ref in (origin.get("refs") or [])),
        )
    )
    dest_entity = _resolve_entity(index, destination.get("entity_id") or destination.get("entity_ref"))
    dimension_ref = destination.get("dimension_id") or destination.get("dimension_ref")
    found = index.get(dimension_ref) if isinstance(dimension_ref, str) else None
    dimension = _dimension_semantic(index, found[1]) if found is not None and found[0] == "pricing_dimensions" else dimension_ref
    return (
        "lane",
        origin.get("scope"),
        origin.get("kind"),
        origin_refs,
        destination.get("kind"),
        dest_entity,
        dimension,
    )


def _norm_measure(value: Any) -> Any:
    """Decimal canônico. 32, 32.0 e 32.00 são o mesmo conteúdo."""
    if isinstance(value, bool) or value is None:
        return None
    parsed = parse_decimal(value)
    if parsed is not None:
        return parsed
    return value


def _sortable(value: Any) -> tuple[Any, ...]:
    if hasattr(value, "normalize") and hasattr(value, "as_tuple"):
        return ("d", format(value.normalize(), "f"))
    if isinstance(value, tuple):
        return tuple(_sortable(item) for item in value)
    if isinstance(value, list):
        return tuple(_sortable(item) for item in value)
    return ("v", "" if value is None else str(value))


def _structured_value(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((str(key), _structured_value(item)) for key, item in value.items()))
    if isinstance(value, list):
        return tuple(_structured_value(item) for item in value)
    return _norm_measure(value) if not isinstance(value, bool) else value


def _interval_band_slot(
    index: dict[str, tuple[str, dict[str, Any]]],
    interval: dict[str, Any],
) -> tuple[Any, ...]:
    """Faixa estrutural e o campo afirmado. O amount fica de fora."""
    band_ref = interval.get("weight_band_id") or interval.get("weight_band_ref")
    found = index.get(band_ref) if isinstance(band_ref, str) else None
    structural = (
        _dimension_semantic(index, found[1])
        if found is not None and found[0] == "pricing_dimensions"
        else None
    )
    return (
        _norm_measure(interval.get("declared_min")),
        _norm_measure(interval.get("declared_max")),
        structural,
        "amount",
    )


def _rule_slot(index: dict[str, tuple[str, dict[str, Any]]], rule: dict[str, Any]) -> tuple[Any, ...]:
    lane_ref = rule.get("lane_id") or rule.get("lane_ref")
    found = index.get(lane_ref) if isinstance(lane_ref, str) else None
    lane = _lane_semantic(index, found[1]) if found is not None and found[0] == "lanes" else ("unresolved", lane_ref)
    dimension_ref = rule.get("dimension_id") or rule.get("dimension_ref")
    dimension_found = index.get(dimension_ref) if isinstance(dimension_ref, str) else None
    dimension = (
        _dimension_semantic(index, dimension_found[1])
        if dimension_found is not None and dimension_found[0] == "pricing_dimensions"
        else ("unresolved", dimension_ref)
    )
    bands = tuple(
        sorted(
            (
                _interval_band_slot(index, item)
                for item in (rule.get("declared_intervals") or [])
                if isinstance(item, dict)
            ),
            key=_sortable,
        )
    )
    return (
        rule.get("pricing_type"),
        rule.get("currency"),
        rule.get("unit"),
        lane,
        dimension,
        bands,
    )


def _rule_content(rule: dict[str, Any]) -> tuple[Any, ...]:
    rows = [
        (
            _norm_measure(item.get("declared_min")),
            _norm_measure(item.get("declared_max")),
            _norm_measure(item.get("amount")),
        )
        for item in (rule.get("declared_intervals") or [])
        if isinstance(item, dict)
    ]
    rows.sort(key=_sortable)
    return (
        tuple(rows),
        _norm_measure(rule.get("rate_amount")),
        _norm_measure(rule.get("excess_rate_per_kg")),
    )


def _condition_slot(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        item.get("kind"),
        normalize_identity_text(str(item.get("name") or "")),
        "applicability",
    )


def _condition_content(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        item.get("presence"),
        item.get("applicability"),
        _structured_value(item.get("parameters")),
    )


def _located_subject(
    index: dict[str, tuple[str, dict[str, Any]]],
    assertion: dict[str, Any],
) -> tuple[Any, tuple[str, dict[str, Any]] | None]:
    subject_ref = assertion.get("subject_id") or assertion.get("subject_ref")
    found = index.get(subject_ref) if isinstance(subject_ref, str) else None
    return subject_ref, found


def _semantic_assertion_slot(
    index: dict[str, tuple[str, dict[str, Any]]],
    assertion: dict[str, Any],
) -> tuple[Any, ...]:
    """O que está sendo afirmado. Amount, taxa, statement, evidência e ids ficam de fora."""
    kind = assertion.get("assertion_kind")
    subject_ref, found = _located_subject(index, assertion)
    if found is None:
        return ("missing", kind, subject_ref)
    collection, item = found
    if collection == "entities":
        return ("entity", kind, _entity_semantic(item))
    if collection == "pricing_dimensions":
        return ("pricing_dimension", kind, _dimension_semantic(index, item))
    if collection == "lanes":
        return ("lane", kind, _lane_semantic(index, item))
    if collection == "price_rules":
        return ("price_rule", kind, _rule_slot(index, item))
    if collection == "commercial_conditions":
        return ("commercial_condition", kind, _condition_slot(item))
    return (collection, kind, subject_ref)


def _asserted_content(
    index: dict[str, tuple[str, dict[str, Any]]],
    assertion: dict[str, Any],
) -> tuple[Any, ...]:
    """Valor afirmado no slot. Não identifica o slot."""
    _subject_ref, found = _located_subject(index, assertion)
    if found is None:
        return ("missing",)
    collection, item = found
    if collection == "price_rules":
        return _rule_content(item)
    if collection == "commercial_conditions":
        return _condition_content(item)
    return ()


def _evidence_index(contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for item in contract.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        for key in ("evidence_id", "ref", "temp_ref"):
            value = item.get(key)
            if isinstance(value, str) and value and value not in found:
                found[value] = item
    return found


def _evidence_signatures(contract: dict[str, Any], assertion: dict[str, Any]) -> frozenset[tuple[Any, ...]]:
    index = _evidence_index(contract)
    signatures: set[tuple[Any, ...]] = set()
    refs = list(assertion.get("evidence_ids") or []) + list(assertion.get("evidence_refs") or [])
    for ref in refs:
        if not isinstance(ref, str):
            continue
        item = index.get(ref)
        if item is None:
            continue
        signatures.add(
            (
                item.get("cell_id"),
                item.get("block_id"),
                item.get("sheet_id"),
                item.get("page_number"),
                item.get("role"),
                normalize_identity_text(str(item.get("snippet") or "")),
                item.get("coordinate"),
                item.get("range"),
            )
        )
    return frozenset(signatures)


def _correspondence(
    _previous: dict[str, Any],
    previous_index: dict[str, tuple[str, dict[str, Any]]],
    _incoming: dict[str, Any],
    incoming_index: dict[str, tuple[str, dict[str, Any]]],
    human: dict[str, Any],
    candidate: dict[str, Any],
) -> str:
    """Slot igual com conteúdo equivalente reaproveita a decisão.

    Statement e evidência não separam o slot. Conteúdo diferente no mesmo
    slot é outra hipótese, não uma assertion independente.
    """
    human_slot = _semantic_assertion_slot(previous_index, human)
    candidate_slot = _semantic_assertion_slot(incoming_index, candidate)
    human_resolved = human_slot[0] != "missing"
    candidate_resolved = candidate_slot[0] != "missing"
    same_content = _asserted_content(previous_index, human) == _asserted_content(incoming_index, candidate)
    if human_resolved and candidate_resolved:
        if human_slot != candidate_slot:
            return "none"
        return "same" if same_content else "incompatible"
    same_id = bool(human.get("assertion_id")) and human.get("assertion_id") == candidate.get("assertion_id")
    if same_id:
        return "same" if same_content else "incompatible"
    return "none"


def _note_material_substitution(contract: dict[str, Any], assertion: dict[str, Any]) -> None:
    validation = contract.get("validation") if isinstance(contract.get("validation"), dict) else {}
    validation = copy.deepcopy(validation)
    findings = [item for item in (validation.get("findings") or []) if isinstance(item, dict)]
    subject = assertion.get("subject_id") or assertion.get("subject_ref")
    marker = {
        "code": "semantic_conflict",
        "severity": "block",
        "subject_ref": subject if isinstance(subject, str) else None,
        "detail": "material_slot_substitution",
    }
    if marker not in findings:
        findings.append(marker)
    validation["findings"] = findings
    validation["status"] = "requires_review"
    contract["validation"] = validation


def _hypothesis_id(assertion_id: str, used: set[Any]) -> str:
    candidate = f"{assertion_id}:hypothesis"
    suffix = 2
    while candidate in used:
        candidate = f"{assertion_id}:hypothesis:{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _aggregate_review_state(assertions: list[Any]) -> str:
    states = [item.get("review_state") for item in assertions if isinstance(item, dict)]
    if any(state in {"rejected", "requires_review"} for state in states):
        return "requires_review"
    if states and all(state == "accepted" for state in states):
        return "accepted"
    return "proposed"


def _reapply_human_decisions(previous: dict[str, Any], merged: dict[str, Any]) -> bool:
    """Decisão humana volta ao mesmo slot quando o conteúdo é equivalente.

    Valor materialmente novo no mesmo slot permanece requires_review.
    """
    humans = [
        item
        for item in (previous.get("assertions") or [])
        if isinstance(item, dict) and item.get("acceptance_source") == "human"
    ]
    if not humans:
        return False
    incoming = [item for item in (merged.get("assertions") or []) if isinstance(item, dict)]
    previous_index = _subject_index(previous)
    incoming_index = _subject_index(merged)
    used_ids = {item.get("assertion_id") for item in incoming if item.get("assertion_id")}
    consumed: set[int] = set()
    extra: list[dict[str, Any]] = []
    for human in humans:
        kind = "none"
        match: dict[str, Any] | None = None
        best_rank: tuple[int, int, int] | None = None
        for candidate in incoming:
            if id(candidate) in consumed:
                continue
            candidate_kind = _correspondence(previous, previous_index, merged, incoming_index, human, candidate)
            if candidate_kind == "none":
                continue
            same_id = bool(human.get("assertion_id")) and candidate.get("assertion_id") == human.get("assertion_id")
            evidence_overlap = len(
                _evidence_signatures(previous, human) & _evidence_signatures(merged, candidate)
            )
            rank = (0 if candidate_kind == "same" else 1, 0 if same_id else 1, -evidence_overlap)
            if best_rank is None or rank < best_rank:
                best_rank = rank
                kind = candidate_kind
                match = candidate
        if kind == "same" and match is not None:
            match["review_state"] = human.get("review_state")
            match["acceptance_source"] = "human"
            if "override" in human:
                match["override"] = copy.deepcopy(human.get("override"))
            consumed.add(id(match))
            continue
        if kind == "incompatible" and match is not None:
            same_slot = _semantic_assertion_slot(previous_index, human) == _semantic_assertion_slot(
                incoming_index, match
            )
            hypothesis = copy.deepcopy(match)
            hypothesis["review_state"] = "requires_review"
            hypothesis["acceptance_source"] = None
            hypothesis.pop("override", None)
            hypothesis_assertion_id = hypothesis.get("assertion_id")
            if hypothesis_assertion_id and hypothesis_assertion_id == human.get("assertion_id"):
                hypothesis["assertion_id"] = _hypothesis_id(str(hypothesis_assertion_id), used_ids)
            match.clear()
            match.update(copy.deepcopy(human))
            extra.append(hypothesis)
            consumed.add(id(match))
            if same_slot and _semantic_assertion_slot(previous_index, human)[0] != "missing":
                _note_material_substitution(merged, hypothesis)
            continue
        preserved = copy.deepcopy(human)
        if preserved.get("assertion_id") not in used_ids:
            extra.append(preserved)
            used_ids.add(preserved.get("assertion_id"))
    if extra:
        assertions = merged.get("assertions")
        if not isinstance(assertions, list):
            assertions = []
            merged["assertions"] = assertions
        assertions.extend(extra)
    return True


def _sync_review_state_and_preview(contract: dict[str, Any]) -> None:
    contract["review_state"] = _aggregate_review_state(list(contract.get("assertions") or []))
    validation = contract.get("validation") if isinstance(contract.get("validation"), dict) else {}
    if contract["review_state"] != "accepted" and validation.get("status") == "valid":
        validation = copy.deepcopy(validation)
        validation["status"] = "requires_review"
        contract["validation"] = validation
    contract["compilation_preview"] = compile_pricing_preview(contract)


def merge_preserving_human_review(previous: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """A hipótese original fica no evento. Decisão humana vigente não volta ao automático."""
    merged = copy.deepcopy(incoming)
    prior_events = [item for item in (previous.get("review_events") or []) if isinstance(item, dict)]
    incoming_events = [item for item in (merged.get("review_events") or []) if isinstance(item, dict)]
    seen = {item.get("event_id") for item in incoming_events if item.get("event_id")}
    for event in prior_events:
        if event.get("event_id") not in seen:
            incoming_events.append(copy.deepcopy(event))
            seen.add(event.get("event_id"))
    merged["review_events"] = incoming_events
    if prior_events and not incoming.get("review_events"):
        merged["revision"] = max(int(previous.get("revision") or 1), int(incoming.get("revision") or 1))
    if _reapply_human_decisions(previous, merged):
        _sync_review_state_and_preview(merged)
    return merged


def attach_semantic_freight_contract(record: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(record, dict) or not isinstance(contract, dict):
        raise SemanticFreightPersistenceError("record_or_contract_invalid")
    status = str(record.get("status") or "").strip().lower()
    if status == "expired":
        raise SemanticFreightPersistenceError("temp_table_expired")
    updated = copy.deepcopy(record)
    stored = align_contract_ttl(contract, updated)
    previous = updated.get(TECHNICAL_JSON_KEY)
    if isinstance(previous, dict):
        stored = merge_preserving_human_review(previous, stored)
    updated[TECHNICAL_JSON_KEY] = stored
    return updated


def preserve_semantic_freight_contract(*, stored: dict[str, Any], updated: dict[str, Any], payload: dict[str, Any]) -> None:
    """O save do formulário não substitui o JSON técnico semântico."""
    existing = stored.get(TECHNICAL_JSON_KEY) if isinstance(stored, dict) else None
    payload_mentions = isinstance(payload, dict) and TECHNICAL_JSON_KEY in payload
    if isinstance(existing, dict):
        updated[TECHNICAL_JSON_KEY] = copy.deepcopy(existing)
        return
    if payload_mentions:
        updated.pop(TECHNICAL_JSON_KEY, None)


def apply_human_review(
    contract: dict[str, Any],
    *,
    assertion_id: str,
    decision: str,
    user_id: str,
    reason: str | None = None,
    replacement: dict[str, Any] | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    if decision not in {"accepted", "rejected", "requires_review"}:
        raise SemanticFreightPersistenceError("review_decision_invalid")
    if not user_id:
        raise SemanticFreightPersistenceError("review_user_required")
    updated = copy.deepcopy(contract)
    assertion = None
    for item in updated.get("assertions") or []:
        if isinstance(item, dict) and item.get("assertion_id") == assertion_id:
            assertion = item
            break
    if assertion is None:
        raise SemanticFreightPersistenceError("assertion_not_found")
    original = copy.deepcopy(assertion)
    event_id = f"rev:{assertion_id}:{len(updated.get('review_events') or []) + 1}"
    event = {
        "event_id": event_id,
        "assertion_id": assertion_id,
        "decision": decision,
        "user_id": user_id,
        "timestamp": timestamp or _utcnow(),
        "reason": reason,
        "original_assertion": original,
        "replacement": copy.deepcopy(replacement) if replacement is not None else None,
    }
    events = list(updated.get("review_events") or [])
    events.append(event)
    updated["review_events"] = events
    assertion["review_state"] = decision
    assertion["acceptance_source"] = "human"
    if replacement:
        assertion["override"] = copy.deepcopy(replacement)
    updated["revision"] = int(updated.get("revision") or 1) + 1
    if decision in {"rejected", "requires_review"}:
        updated["review_state"] = "requires_review"
        preview = updated.get("compilation_preview") if isinstance(updated.get("compilation_preview"), dict) else {}
        preview["activated"] = False
        preview["status"] = "blocked"
        body = preview.get("pricing_contract_preview") if isinstance(preview.get("pricing_contract_preview"), dict) else {}
        body["rules"] = []
        body["preview_only"] = True
        body["operational"] = False
        preview["pricing_contract_preview"] = body
        updated["compilation_preview"] = preview
    return updated


def round_trip_technical_json(record: dict[str, Any]) -> dict[str, Any]:
    raw = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise SemanticFreightPersistenceError("round_trip_invalid")
    return parsed
