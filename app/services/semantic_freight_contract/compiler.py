"""Compiler experimental: Semantic Freight Contract → preview do pricing_contract v1.

Não grava o preview no contrato operacional, não altera o snapshot de
confirmação e não escolhe precedência entre dimensões sobrepostas.
"""
from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

from app.services.semantic_freight_contract.evidence import local_fingerprint, parse_decimal
from app.services.semantic_freight_contract.models import (
    BOUNDARY_POLICY_LEGACY_HALF_OPEN,
    COMPILER_VERSION,
    PRICING_TYPES,
)
from app.services.semantic_freight_contract.validator import blocking_findings

_PROJECTABLE_ORIGIN_KINDS = frozenset({"municipality"})


def _by_id(items: list[Any], field: str) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for item in items:
        if isinstance(item, dict) and isinstance(item.get(field), str):
            found[item[field]] = item
    return found


def _declared_pairs(rule: dict[str, Any]) -> list[tuple[Decimal, Decimal, Decimal]]:
    pairs: list[tuple[Decimal, Decimal, Decimal]] = []
    for raw in rule.get("declared_intervals") or []:
        if not isinstance(raw, dict):
            return []
        start = parse_decimal(raw.get("declared_min"))
        end = parse_decimal(raw.get("declared_max"))
        amount = parse_decimal(raw.get("amount"))
        if start is None or end is None or amount is None or start > end:
            return []
        pairs.append((start, end, amount))
    pairs.sort(key=lambda item: (item[0], item[1]))
    return pairs


def _execution_from_declared(
    pairs: list[tuple[Decimal, Decimal, Decimal]],
) -> tuple[str | None, list[dict[str, Any]] | None, str | None]:
    """Converte só quando a política legada autoriza. O declarado não muda."""
    if not pairs:
        return None, None, "price_missing"
    if pairs[0][0] != Decimal("0"):
        return None, None, "boundary_policy_not_authorized"
    for index in range(1, len(pairs)):
        if pairs[index][0] <= pairs[index - 1][1]:
            return None, None, "overlap"
        if pairs[index][0] - pairs[index - 1][1] != Decimal("1"):
            return None, None, "declared_gap"
    intervals: list[dict[str, Any]] = []
    previous_max: Decimal | None = None
    for index, (start, end, amount) in enumerate(pairs):
        if index == 0:
            exec_min = start
            min_inclusive = True
            policy = None
        else:
            exec_min = previous_max if previous_max is not None else start
            min_inclusive = False
            policy = BOUNDARY_POLICY_LEGACY_HALF_OPEN
        intervals.append(
            {
                "min": format(exec_min, "f"),
                "max": format(end, "f"),
                "min_inclusive": min_inclusive,
                "max_inclusive": True,
                "amount": format(amount, "f"),
                "boundary_policy": policy,
            }
        )
        previous_max = end
    policy = BOUNDARY_POLICY_LEGACY_HALF_OPEN if len(pairs) > 1 else None
    return policy, intervals, None


def _legacy_lookup_for_entity(entity: dict[str, Any]) -> str | None:
    if entity.get("kind") == "state":
        state = str(entity.get("state") or "").upper()
        return state or None
    if entity.get("kind") != "municipality":
        return None
    from app.agente_compara_doc_service import _coverage_lookup_key

    return _coverage_lookup_key(entity.get("state"), entity.get("name"))


def _origin_block(lane: dict[str, Any], entities: dict[str, dict[str, Any]]) -> str | None:
    origin = lane.get("origin") if isinstance(lane.get("origin"), dict) else {}
    scope = origin.get("scope")
    if scope == "unspecified":
        return "origin_unspecified"
    if scope == "multiple":
        return "multiple_origins_not_projectable"
    kind = origin.get("kind")
    if kind not in _PROJECTABLE_ORIGIN_KINDS:
        return "origin_not_projectable"
    refs = list(origin.get("refs") or [])
    if len(refs) != 1:
        return "origin_not_projectable"
    entity = entities.get(refs[0])
    if entity is not None and entity.get("kind") == "municipality":
        return None
    return "origin_not_projectable"


def _destination_projection(
    lane: dict[str, Any],
    rule: dict[str, Any],
    entities: dict[str, dict[str, Any]],
    dimensions: dict[str, dict[str, Any]],
) -> tuple[list[str], str, str | None, str | None]:
    destination = lane.get("destination") if isinstance(lane.get("destination"), dict) else {}
    dimension_id = destination.get("dimension_id") or rule.get("dimension_id")
    entity_id = destination.get("entity_id")
    dimension = dimensions.get(dimension_id) if isinstance(dimension_id, str) else None
    entity = entities.get(entity_id) if isinstance(entity_id, str) else None
    if dimension and dimension.get("kind") in {"state_capital", "state_interior"}:
        return [], "blocked", dimension.get("dimension_id"), "capital_interior_not_executable"
    if dimension and dimension.get("kind") == "carrier_defined_region":
        label = dimension.get("source_label")
        keys = [str(label)] if isinstance(label, str) and label else []
        return keys, "partial", dimension.get("dimension_id"), "membership_unresolved"
    if entity is not None:
        key = _legacy_lookup_for_entity(entity)
        if not key:
            return [], "blocked", dimension_id, "destination_not_projectable"
        return [key], "projected", dimension_id, None
    if dimension and dimension.get("kind") == "state":
        state = str(dimension.get("state") or "").upper()
        if not state:
            return [], "blocked", dimension.get("dimension_id"), "destination_not_projectable"
        return [state], "projected", dimension.get("dimension_id"), None
    return [], "blocked", dimension_id, "destination_not_projectable"


def compile_pricing_preview(contract: dict[str, Any]) -> dict[str, Any]:
    """Preview isolado. activated permanece falso e o snapshot operacional não muda."""
    entities = _by_id(list(contract.get("entities") or []), "entity_id")
    dimensions = _by_id(list(contract.get("pricing_dimensions") or []), "dimension_id")
    lanes = _by_id(list(contract.get("lanes") or []), "lane_id")
    findings = list((contract.get("validation") or {}).get("findings") or [])
    blocked_globally = {
        item.get("subject_ref")
        for item in blocking_findings(findings)
        if item.get("subject_ref")
    }
    precedence = any(item.get("code") == "precedence_unresolved" for item in blocking_findings(findings))
    review_blocks_preview = contract.get("review_state") == "requires_review"

    preview_rules: list[dict[str, Any]] = []
    rule_projection: list[dict[str, Any]] = []
    dimension_projection: list[dict[str, Any]] = []
    blocked_parts: list[dict[str, Any]] = []
    active_fees: list[dict[str, Any]] = []
    not_applied: list[str] = []
    tax_treatment: dict[str, str] = {}

    for condition in contract.get("commercial_conditions") or []:
        if not isinstance(condition, dict):
            continue
        if condition.get("applicability") == "explicitly_not_applied" and condition.get("presence") == "observed":
            not_applied.append(str(condition.get("name")))
            continue
        if condition.get("applicability") != "applied":
            continue
        parameters = condition.get("parameters") if isinstance(condition.get("parameters"), dict) else {}
        if condition.get("kind") == "tax" and parameters.get("treatment"):
            tax_treatment[str(condition.get("name"))] = str(parameters.get("treatment"))
            continue
        active_fees.append(
            {
                "condition_id": condition.get("condition_id"),
                "kind": condition.get("kind"),
                "name": condition.get("name"),
            }
        )

    for rule in contract.get("price_rules") or []:
        if not isinstance(rule, dict):
            continue
        rule_id = rule.get("rule_id")
        lane = lanes.get(rule.get("lane_id")) or {}
        origin_reason = _origin_block(lane, entities)
        keys, destination_status, dimension_id, destination_reason = _destination_projection(
            lane, rule, entities, dimensions
        )
        pricing_type = rule.get("pricing_type")
        reason = None
        if pricing_type not in PRICING_TYPES:
            reason = "pricing_type_not_executable"
        elif rule_id in blocked_globally or rule.get("temp_ref") in blocked_globally:
            reason = "semantic_blocked"
        elif precedence and destination_reason == "capital_interior_not_executable":
            reason = "precedence_unresolved"
        elif origin_reason:
            reason = origin_reason
        elif destination_status == "blocked":
            reason = destination_reason or "destination_not_projectable"
        elif review_blocks_preview:
            reason = "requires_review"

        execution_policy = None
        execution_intervals = None
        brackets: list[dict[str, Any]] = []
        if pricing_type in {"fixed_range", "range_plus_excess_per_kg"}:
            pairs = _declared_pairs(rule)
            execution_policy, execution_intervals, interval_reason = _execution_from_declared(pairs)
            if interval_reason and reason is None:
                reason = interval_reason
            if reason is None and execution_intervals is not None:
                for item in execution_intervals:
                    brackets.append(
                        {
                            "min_kg": float(Decimal(item["min"])),
                            "max_kg": float(Decimal(item["max"])),
                            "value": round(float(Decimal(item["amount"])), 2),
                            "label": f"{item['min']}–{item['max']}",
                        }
                    )
        elif pricing_type == "direct_weight_rate":
            rate = parse_decimal(rule.get("rate_amount"))
            if rate is None and reason is None:
                reason = "price_missing"

        rule["execution"] = {
            "boundary_policy": execution_policy,
            "intervals": execution_intervals,
            "authorized": reason is None,
        }

        if dimension_id:
            dimension_projection.append(
                {
                    "pricing_dimension_id": dimension_id,
                    "legacy_lookup_keys": list(keys),
                    "status": "blocked" if destination_reason else destination_status,
                    "reason": destination_reason,
                }
            )
            if destination_reason and reason is None:
                blocked_parts.append(
                    {"pricing_dimension_id": dimension_id, "reason": destination_reason}
                )
        if reason is not None:
            blocked_parts.append({"semantic_rule_id": rule_id, "reason": reason})
            rule_projection.append(
                {
                    "semantic_rule_id": rule_id,
                    "executable_rule_ref": None,
                    "status": "blocked",
                    "reason": reason,
                }
            )
            continue

        executable_ref = f"preview:{len(preview_rules)}"
        preview_rule: dict[str, Any] = {
            "pricing_type": pricing_type,
            "unit": rule.get("unit"),
            "brackets": brackets,
            "excess": None,
            "lookup_keys": list(keys),
            "preview_only": True,
            "semantic_rule_id": rule_id,
            "label": keys[0] if keys else None,
        }
        if pricing_type == "range_plus_excess_per_kg":
            excess = parse_decimal(rule.get("excess_rate_per_kg"))
            preview_rule["excess"] = {
                "rate_per_kg": round(float(excess), 2) if excess is not None else None
            }
        if pricing_type == "direct_weight_rate":
            rate = parse_decimal(rule.get("rate_amount"))
            if rule.get("unit") == "ton":
                preview_rule["value_per_ton"] = round(float(rate), 2) if rate is not None else None
            else:
                preview_rule["value_per_kg"] = round(float(rate), 2) if rate is not None else None
        preview_rules.append(preview_rule)
        rule_projection.append(
            {
                "semantic_rule_id": rule_id,
                "executable_rule_ref": executable_ref,
                "status": "partial" if destination_status == "partial" else "projected",
                "reason": destination_reason,
            }
        )

    if blocked_parts and preview_rules:
        status = "partial"
    elif blocked_parts or not preview_rules:
        status = "blocked"
    else:
        status = "complete"

    preview = {
        "compiler_version": COMPILER_VERSION,
        "activated": False,
        "status": status,
        "pricing_contract_preview": {
            "schema_version": 1,
            "preview_only": True,
            "operational": False,
            "rules": preview_rules,
        },
        "compatibility_snapshot": {
            "kind": "semantic_freight_preview_snapshot",
            "isolated": True,
            "operational": False,
            "compiler_version": COMPILER_VERSION,
            "rules_signature": local_fingerprint(preview_rules),
        },
        "rule_projection": rule_projection,
        "dimension_projection": dimension_projection,
        "blocked_parts": blocked_parts,
        "commercial_projection": {
            "active_fees": active_fees,
            "explicitly_not_applied": not_applied,
            "tax_treatment": tax_treatment,
        },
    }
    return preview


def validate_compilation_preview(preview: dict[str, Any]) -> dict[str, Any]:
    """Helper isolado. Não recompila freight_tables nem chama o gate operacional."""
    findings: list[dict[str, Any]] = []
    if not isinstance(preview, dict):
        findings.append({"code": "preview_missing", "severity": "block"})
        return {"ok": False, "findings": findings}
    if preview.get("activated") is not False:
        findings.append({"code": "preview_activated", "severity": "block"})
    snapshot = preview.get("compatibility_snapshot") if isinstance(preview.get("compatibility_snapshot"), dict) else {}
    if snapshot.get("isolated") is not True or snapshot.get("operational") is not False:
        findings.append({"code": "snapshot_not_isolated", "severity": "block"})
    if snapshot.get("kind") != "semantic_freight_preview_snapshot":
        findings.append({"code": "snapshot_kind", "severity": "block"})
    body = preview.get("pricing_contract_preview") if isinstance(preview.get("pricing_contract_preview"), dict) else {}
    if body.get("preview_only") is not True or body.get("operational") is not False:
        findings.append({"code": "preview_marked_operational", "severity": "block"})
    if "source_fingerprint" in body:
        findings.append({"code": "operational_fingerprint_present", "severity": "block"})
    for rule in body.get("rules") or []:
        if not isinstance(rule, dict) or rule.get("pricing_type") not in PRICING_TYPES:
            findings.append({"code": "preview_pricing_type", "severity": "block"})
        if rule.get("preview_only") is not True:
            findings.append({"code": "preview_rule_not_flagged", "severity": "block"})
    return {"ok": not findings, "findings": findings}


def preview_without_operational_write(temp_table: dict[str, Any], preview: dict[str, Any]) -> dict[str, Any]:
    """Devolve a temp table com o preview só dentro do contrato semântico."""
    updated = copy.deepcopy(temp_table)
    operational = copy.deepcopy(temp_table.get("pricing_contract")) if isinstance(temp_table, dict) else None
    updated["pricing_contract"] = operational
    return updated
