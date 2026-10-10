"""Validação estrutural e semântica determinística. Não chama a IA de volta."""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from app.services.semantic_freight_contract.evidence import (
    evidence_pointer_errors,
    local_fingerprint,
    normalize_identity_text,
    parse_decimal,
)
from app.services.semantic_freight_contract.models import (
    ACCEPTANCE_SOURCES,
    ACCESSORIAL_NAMES,
    BR_UFS,
    CONDITION_KINDS,
    REVIEW_STATES,
    TAX_NAMES,
)
from app.services.semantic_freight_contract.schema import INTERPRETATION_SCHEMA

_UNSAFE_MEMBERSHIP = frozenset(
    {
        "STATE EXCEPT CAPITAL",
        "TODO O ESTADO MENOS A CAPITAL",
        "TODO ESTADO EXCETO CAPITAL",
        "INTERIOR IGUAL ESTADO MENOS CAPITAL",
    }
)


def _types(schema: dict[str, Any]) -> list[str]:
    expected = schema.get("type")
    if isinstance(expected, list):
        return list(expected)
    if isinstance(expected, str):
        return [expected]
    return []


def _type_matches(value: Any, types: list[str]) -> bool:
    if value is None:
        return "null" in types
    if isinstance(value, bool):
        return "boolean" in types
    if "integer" in types and isinstance(value, int):
        return True
    if "string" in types and isinstance(value, str):
        return True
    if "object" in types and isinstance(value, dict):
        return True
    if "array" in types and isinstance(value, list):
        return True
    if "boolean" in types and isinstance(value, bool):
        return True
    return False


def schema_errors(instance: Any, schema: dict[str, Any] | None = None, path: str = "$") -> list[str]:
    """Percorre INTERPRETATION_SCHEMA. Forma válida não é verdade documental."""
    schema = INTERPRETATION_SCHEMA if schema is None else schema
    errors: list[str] = []
    types = _types(schema)
    if not _type_matches(instance, types):
        return [f"{path}: tipo invalido"]
    if instance is None:
        return []
    if "object" in types and isinstance(instance, dict):
        properties = schema.get("properties") or {}
        if schema.get("additionalProperties") is False:
            for key in sorted(set(instance) - set(properties)):
                errors.append(f"{path}.{key}: propriedade nao permitida")
        for key in schema.get("required") or []:
            if key not in instance:
                errors.append(f"{path}.{key}: obrigatorio")
        for key, sub in properties.items():
            if key in instance:
                errors.extend(schema_errors(instance[key], sub, f"{path}.{key}"))
        return errors
    if "array" in types and isinstance(instance, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(instance):
                errors.extend(schema_errors(item, item_schema, f"{path}[{index}]"))
        return errors
    if "string" in types and isinstance(instance, str):
        if "minLength" in schema and len(instance) < int(schema["minLength"]):
            errors.append(f"{path}: curto")
        if "maxLength" in schema and len(instance) > int(schema["maxLength"]):
            errors.append(f"{path}: longo")
        if "pattern" in schema and not re.match(schema["pattern"], instance):
            errors.append(f"{path}: decimal invalido")
        if "enum" in schema and instance not in schema["enum"]:
            errors.append(f"{path}: enum invalido")
        return errors
    if "integer" in types and isinstance(instance, int) and not isinstance(instance, bool):
        return errors
    return errors


def _finding(code: str, *, severity: str, subject_ref: str | None = None, detail: str | None = None) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "subject_ref": subject_ref,
        "detail": detail,
    }


def _index_refs(items: list[Any], key: str = "ref") -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    found: dict[str, dict[str, Any]] = {}
    findings: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ref = item.get(key)
        if not isinstance(ref, str) or not ref:
            findings.append(_finding("missing_temp_ref", severity="block"))
            continue
        if ref in found:
            findings.append(_finding("duplicate_temp_ref", severity="block", subject_ref=ref))
            continue
        found[ref] = item
    return found, findings


def entity_identity(entity: dict[str, Any]) -> tuple[str, str, str, str] | None:
    kind = entity.get("kind")
    country = str(entity.get("country") or "").upper()
    state = str(entity.get("state") or "").upper()
    name = normalize_identity_text(str(entity.get("name") or ""))
    if kind not in {"state", "municipality"} or country != "BR":
        return None
    if kind == "state":
        if state not in BR_UFS:
            return None
        return ("state", "BR", state, state)
    if state not in BR_UFS or not name:
        return None
    return ("municipality", "BR", state, name)


def _normalized_membership(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = normalize_identity_text(value)
    return text or None


def _dimension_postal_range(dimension: dict[str, Any]) -> str | None:
    """Faixa postal quando o objeto a traz. declared_min/max seguem no eixo de peso."""
    postal = dimension.get("postal_range")
    if postal not in (None, ""):
        return str(postal)
    start = dimension.get("postal_start")
    end = dimension.get("postal_end")
    if start in (None, "") and end in (None, ""):
        return None
    return f"{start}:{end}"


def dimension_nominal_key(dimension: dict[str, Any]) -> tuple[Any, ...] | None:
    """Identidade nominal. Sem rótulo não há a mesma dimensão para conflitar."""
    label = normalize_identity_text(str(dimension.get("source_label") or ""))
    if not label:
        return None
    return (
        dimension.get("kind"),
        label,
        dimension.get("destination_entity_ref") or dimension.get("entity_ref"),
        str(dimension.get("state") or "").upper(),
    )


def dimension_meaning_key(dimension: dict[str, Any]) -> tuple[Any, ...]:
    """Campos que mudam o significado tarifário, além do rótulo."""
    return (
        dimension.get("meaning_status"),
        _normalized_membership(dimension.get("membership_definition")),
        dimension.get("selector"),
        dimension.get("predicate"),
        _dimension_postal_range(dimension),
        dimension.get("declared_min"),
        dimension.get("declared_max"),
        dimension.get("unit"),
    )


def dimension_equivalence_key(dimension: dict[str, Any]) -> tuple[Any, ...]:
    """Dois objetos só são o mesmo quando kind, rótulo e significado coincidem."""
    nominal = dimension_nominal_key(dimension)
    if nominal is None:
        nominal = (
            dimension.get("kind"),
            "",
            dimension.get("destination_entity_ref") or dimension.get("entity_ref"),
            str(dimension.get("state") or "").upper(),
        )
    return nominal + dimension_meaning_key(dimension)


def _interval_pairs(rule: dict[str, Any]) -> tuple[list[tuple[Decimal, Decimal, Decimal]], list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    pairs: list[tuple[Decimal, Decimal, Decimal]] = []
    for index, raw in enumerate(rule.get("declared_intervals") or []):
        if not isinstance(raw, dict):
            findings.append(_finding("invalid_range", severity="block", subject_ref=rule.get("ref")))
            continue
        start = parse_decimal(raw.get("declared_min"))
        end = parse_decimal(raw.get("declared_max"))
        amount = parse_decimal(raw.get("amount"))
        if start is None or end is None or amount is None:
            findings.append(
                _finding("invalid_decimal", severity="block", subject_ref=rule.get("ref"), detail=str(index))
            )
            continue
        if start > end:
            findings.append(_finding("min_greater_than_max", severity="block", subject_ref=rule.get("ref")))
            continue
        if amount <= 0:
            findings.append(_finding("price_missing", severity="block", subject_ref=rule.get("ref")))
            continue
        pairs.append((start, end, amount))
    pairs.sort(key=lambda item: (item[0], item[1]))
    return pairs, findings


def declared_interval_findings(rule: dict[str, Any]) -> list[dict[str, Any]]:
    """Intervalo declarado permanece fechado. A convenção de execução é outro eixo."""
    pairs, findings = _interval_pairs(rule)
    for index in range(1, len(pairs)):
        previous_end = pairs[index - 1][1]
        current_start = pairs[index][0]
        if current_start <= previous_end:
            findings.append(
                _finding(
                    "overlap",
                    severity="block",
                    subject_ref=rule.get("ref"),
                    detail=f"{previous_end}->{current_start}",
                )
            )
        elif current_start > previous_end:
            gap = current_start - previous_end
            code = "declared_boundary_gap" if gap == Decimal("1") else "declared_gap"
            severity = "info" if gap == Decimal("1") else "block"
            findings.append(
                _finding(
                    code,
                    severity=severity,
                    subject_ref=rule.get("ref"),
                    detail=f"{previous_end}->{current_start}",
                )
            )
    return findings


def _condition_parameter_findings(condition: dict[str, Any]) -> list[dict[str, Any]]:
    if condition.get("applicability") != "applied":
        return []
    parameters = condition.get("parameters") if isinstance(condition.get("parameters"), dict) else {}
    kind = condition.get("kind")
    name = condition.get("name")
    if kind == "tax" and name == "icms" and parameters.get("treatment") == "included_in_price":
        return []
    if kind == "delivery_term" and parameters.get("treatment"):
        return []
    if kind == "validity" and parameters.get("treatment"):
        return []
    if parse_decimal(parameters.get("rate")) is None and parse_decimal(parameters.get("amount")) is None:
        if not parameters.get("treatment"):
            return [
                _finding(
                    "applied_without_parameters",
                    severity="block",
                    subject_ref=condition.get("ref"),
                )
            ]
    return []


def validate_interpretation(
    payload: dict[str, Any],
    *,
    ir_index: dict[str, Any],
    document_id: str | None,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    structural = schema_errors(payload)
    if structural:
        return [_finding("invalid_structured_output", severity="block", detail=item) for item in structural]

    entities, entity_ref_findings = _index_refs(list(payload.get("entities") or []))
    dimensions, dimension_ref_findings = _index_refs(list(payload.get("pricing_dimensions") or []))
    lanes, lane_ref_findings = _index_refs(list(payload.get("lanes") or []))
    rules, rule_ref_findings = _index_refs(list(payload.get("price_rules") or []))
    conditions, condition_ref_findings = _index_refs(list(payload.get("commercial_conditions") or []))
    assertions, assertion_ref_findings = _index_refs(list(payload.get("assertions") or []))
    evidence, evidence_ref_findings = _index_refs(list(payload.get("evidence") or []))
    findings.extend(entity_ref_findings)
    findings.extend(dimension_ref_findings)
    findings.extend(lane_ref_findings)
    findings.extend(rule_ref_findings)
    findings.extend(condition_ref_findings)
    findings.extend(assertion_ref_findings)
    findings.extend(evidence_ref_findings)

    seen_entities: dict[tuple[str, str, str, str], str] = {}
    for ref, entity in entities.items():
        identity = entity_identity(entity)
        if identity is None:
            findings.append(_finding("invalid_entity", severity="block", subject_ref=ref))
            continue
        if identity in seen_entities:
            findings.append(
                _finding(
                    "semantic_duplicate",
                    severity="block",
                    subject_ref=ref,
                    detail=seen_entities[identity],
                )
            )
        else:
            seen_entities[identity] = ref
        if entity.get("kind") == "municipality" and str(entity.get("state") or "").upper() not in BR_UFS:
            findings.append(_finding("municipality_without_state", severity="block", subject_ref=ref))

    for ref, dimension in dimensions.items():
        entity_ref = dimension.get("destination_entity_ref")
        if entity_ref is not None and entity_ref not in entities:
            findings.append(_finding("dangling_dimension_entity", severity="block", subject_ref=ref))
        if dimension.get("kind") in {"state_capital", "state_interior"}:
            membership = normalize_identity_text(str(dimension.get("membership_definition") or ""))
            if membership in _UNSAFE_MEMBERSHIP or (
                dimension.get("meaning_status") == "explicit" and not membership
            ):
                findings.append(_finding("unsafe_capital_interior", severity="block", subject_ref=ref))
            if dimension.get("meaning_status") != "explicit":
                findings.append(_finding("capital_interior_unresolved", severity="block", subject_ref=ref))
        if dimension.get("kind") == "carrier_defined_region":
            if dimension.get("membership_definition") not in {None, ""}:
                findings.append(_finding("invented_membership", severity="block", subject_ref=ref))
            if dimension.get("meaning_status") != "unresolved":
                findings.append(_finding("carrier_region_overclaimed", severity="block", subject_ref=ref))
        state = dimension.get("state")
        if isinstance(state, str) and state.upper() not in BR_UFS:
            findings.append(_finding("invalid_dimension_state", severity="block", subject_ref=ref))

    nominal_groups: dict[tuple[Any, ...], list[str]] = {}
    for ref, dimension in dimensions.items():
        nominal = dimension_nominal_key(dimension)
        if nominal is None:
            continue
        nominal_groups.setdefault(nominal, []).append(ref)
    for refs in nominal_groups.values():
        meanings: dict[tuple[Any, ...], list[str]] = {}
        for ref in refs:
            meanings.setdefault(dimension_meaning_key(dimensions[ref]), []).append(ref)
        if len(meanings) <= 1:
            continue
        detail = "|".join(
            sorted(
                str(dimension_meaning_key(dimensions[ref]))
                for ref in refs
            )
        )
        for ref in sorted(refs):
            findings.append(_finding("semantic_conflict", severity="block", subject_ref=ref, detail=detail))

    for ref, lane in lanes.items():
        origin = lane.get("origin") if isinstance(lane.get("origin"), dict) else {}
        destination = lane.get("destination") if isinstance(lane.get("destination"), dict) else {}
        scope = origin.get("scope")
        origin_refs = list(origin.get("refs") or [])
        if scope == "unspecified" and origin_refs:
            findings.append(_finding("unspecified_origin_has_refs", severity="block", subject_ref=ref))
        if scope == "single" and len(origin_refs) != 1:
            findings.append(_finding("origin_scope_mismatch", severity="block", subject_ref=ref))
        if scope == "multiple" and len(origin_refs) < 2:
            findings.append(_finding("origin_scope_mismatch", severity="block", subject_ref=ref))
        for origin_ref in origin_refs:
            if origin_ref not in entities and origin_ref not in dimensions:
                findings.append(_finding("dangling_origin_ref", severity="block", subject_ref=ref, detail=origin_ref))
        entity_ref = destination.get("entity_ref")
        dimension_ref = destination.get("dimension_ref")
        if destination.get("kind") == "municipality" and entity_ref not in entities:
            findings.append(_finding("dangling_destination_entity", severity="block", subject_ref=ref))
        if destination.get("kind") == "state" and entity_ref not in entities:
            findings.append(_finding("dangling_destination_entity", severity="block", subject_ref=ref))
        if destination.get("kind") == "pricing_dimension" and dimension_ref not in dimensions:
            findings.append(_finding("dangling_destination_dimension", severity="block", subject_ref=ref))
        if entity_ref and dimension_ref:
            dimension = dimensions.get(dimension_ref) or {}
            linked = dimension.get("destination_entity_ref")
            if linked is not None and linked != entity_ref:
                findings.append(_finding("entity_dimension_conflict", severity="block", subject_ref=ref))

    rule_signatures: dict[tuple[Any, ...], str] = {}
    for ref, rule in rules.items():
        if rule.get("lane_ref") not in lanes:
            findings.append(_finding("dangling_lane_ref", severity="block", subject_ref=ref))
        dimension_ref = rule.get("dimension_ref")
        if dimension_ref is not None and dimension_ref not in dimensions:
            findings.append(_finding("dangling_rule_dimension", severity="block", subject_ref=ref))
        pricing_type = rule.get("pricing_type")
        if pricing_type in {"fixed_range", "range_plus_excess_per_kg"} and not rule.get("declared_intervals"):
            findings.append(_finding("price_missing", severity="block", subject_ref=ref))
        if pricing_type == "direct_weight_rate" and parse_decimal(rule.get("rate_amount")) is None:
            findings.append(_finding("price_missing", severity="block", subject_ref=ref))
        if pricing_type == "range_plus_excess_per_kg" and parse_decimal(rule.get("excess_rate_per_kg")) is None:
            findings.append(_finding("excess_missing", severity="block", subject_ref=ref))
        if rule.get("declared_intervals") or rule.get("rate_amount") is not None:
            if rule.get("currency") is None:
                findings.append(_finding("currency_missing", severity="block", subject_ref=ref))
            if rule.get("unit") is None:
                findings.append(_finding("unit_missing", severity="block", subject_ref=ref))
        findings.extend(declared_interval_findings(rule))
        lane = lanes.get(rule.get("lane_ref")) or {}
        origin = lane.get("origin") if isinstance(lane.get("origin"), dict) else {}
        destination = lane.get("destination") if isinstance(lane.get("destination"), dict) else {}
        for interval in rule.get("declared_intervals") or []:
            if not isinstance(interval, dict):
                continue
            band_ref = interval.get("weight_band_ref")
            if band_ref is not None and band_ref not in dimensions:
                findings.append(_finding("dangling_weight_band", severity="block", subject_ref=ref))
        pairs, _pair_findings = _interval_pairs(rule)
        signature = (
            rule.get("lane_ref"),
            tuple(origin.get("refs") or []),
            destination.get("entity_ref"),
            destination.get("dimension_ref"),
            pricing_type,
            tuple((str(item[0]), str(item[1])) for item in pairs),
        )
        amounts = tuple(str(item[2]) for item in pairs)
        previous = rule_signatures.get(signature)
        if previous is not None:
            previous_rule = rules[previous]
            previous_pairs, _ignored = _interval_pairs(previous_rule)
            previous_amounts = tuple(str(item[2]) for item in previous_pairs)
            code = "semantic_duplicate" if previous_amounts == amounts else "semantic_conflict"
            findings.append(_finding(code, severity="block", subject_ref=ref, detail=previous))
        else:
            rule_signatures[signature] = ref

    if not ir_index.get("coverage_complete"):
        for ref, condition in conditions.items():
            if condition.get("presence") == "absent_in_examined_scope":
                findings.append(
                    _finding(
                        "incomplete_ir_not_global_absence",
                        severity="block",
                        subject_ref=ref,
                    )
                )
    seen_conditions: dict[tuple[str, str], str] = {}
    for ref, condition in conditions.items():
        if condition.get("kind") not in CONDITION_KINDS:
            findings.append(_finding("unknown_condition_kind", severity="block", subject_ref=ref))
        if condition.get("kind") == "accessorial" and condition.get("name") not in ACCESSORIAL_NAMES:
            findings.append(_finding("unknown_accessorial", severity="block", subject_ref=ref))
        if condition.get("kind") == "tax" and condition.get("name") not in TAX_NAMES:
            findings.append(_finding("unknown_tax", severity="block", subject_ref=ref))
        findings.extend(_condition_parameter_findings(condition))
        key = (str(condition.get("kind")), str(condition.get("name")))
        previous = seen_conditions.get(key)
        if previous is not None:
            previous_condition = conditions[previous]
            same = (
                previous_condition.get("presence") == condition.get("presence")
                and previous_condition.get("applicability") == condition.get("applicability")
            )
            findings.append(
                _finding(
                    "semantic_duplicate" if same else "semantic_conflict",
                    severity="block",
                    subject_ref=ref,
                    detail=previous,
                )
            )
        else:
            seen_conditions[key] = ref
        if (
            condition.get("presence") == "observed"
            and condition.get("applicability") == "explicitly_not_applied"
        ):
            parameters = condition.get("parameters") if isinstance(condition.get("parameters"), dict) else {}
            if parse_decimal(parameters.get("amount")) == Decimal("0"):
                findings.append(_finding("zero_fee_for_not_applied", severity="block", subject_ref=ref))

    subjects = set(entities) | set(dimensions) | set(lanes) | set(rules) | set(conditions)
    covered: set[str] = set()
    for ref, assertion in assertions.items():
        if assertion.get("assertion_kind") not in {"FACT", "INTERPRETATION"}:
            findings.append(_finding("resolution_not_in_scope", severity="block", subject_ref=ref))
        subject = assertion.get("subject_ref")
        if subject not in subjects:
            findings.append(_finding("dangling_assertion_subject", severity="block", subject_ref=ref))
        else:
            covered.add(subject)
        evidence_refs = list(assertion.get("evidence_refs") or [])
        if not evidence_refs:
            findings.append(_finding("assertion_without_evidence", severity="block", subject_ref=ref))
        for evidence_ref in evidence_refs:
            pointer = evidence.get(evidence_ref)
            if pointer is None:
                findings.append(
                    _finding("evidence_not_in_ir", severity="block", subject_ref=ref, detail=str(evidence_ref))
                )
                continue
            for code in evidence_pointer_errors(pointer, ir_index, document_id=document_id):
                findings.append(_finding("evidence_not_in_ir", severity="block", subject_ref=ref, detail=code))
    for subject in sorted(subjects):
        if subject not in covered:
            findings.append(_finding("assertion_required", severity="block", subject_ref=subject))

    precedence_groups: dict[tuple[Any, ...], list[str]] = {}
    for ref, rule in rules.items():
        lane = lanes.get(rule.get("lane_ref")) or {}
        destination = lane.get("destination") if isinstance(lane.get("destination"), dict) else {}
        dimension = dimensions.get(destination.get("dimension_ref") or rule.get("dimension_ref")) or {}
        if dimension.get("kind") not in {"state", "state_capital", "state_interior"}:
            continue
        state = dimension.get("state")
        pairs, _ignored = _interval_pairs(rule)
        band = tuple((str(item[0]), str(item[1])) for item in pairs)
        precedence_groups.setdefault((state, band, rule.get("pricing_type")), []).append(dimension.get("kind"))
    for key, kinds in precedence_groups.items():
        if len(set(kinds)) > 1:
            findings.append(
                _finding(
                    "precedence_unresolved",
                    severity="block",
                    detail="|".join(str(part) for part in key),
                )
            )
    return findings


def blocking_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in findings if item.get("severity") == "block"]


def deterministic_acceptance_allowed(findings: list[dict[str, Any]], *, ir_index: dict[str, Any]) -> bool:
    """confidence alta não entra aqui. Só a política determinística aceita."""
    if blocking_findings(findings):
        return False
    if not ir_index.get("coverage_complete"):
        return False
    return True


def apply_review_policy(
    assertions: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    *,
    ir_index: dict[str, Any],
) -> str:
    allowed = deterministic_acceptance_allowed(findings, ir_index=ir_index)
    blocked_subjects = {
        item.get("subject_ref")
        for item in blocking_findings(findings)
        if item.get("subject_ref")
    }
    global_block = any(item.get("subject_ref") in {None, ""} for item in blocking_findings(findings))
    for assertion in assertions:
        if assertion.get("acceptance_source") == "human":
            continue
        subject = assertion.get("subject_ref")
        if allowed and subject not in blocked_subjects and not global_block:
            assertion["review_state"] = "accepted"
            assertion["acceptance_source"] = "deterministic_policy"
        elif subject in blocked_subjects or global_block or not allowed:
            assertion["review_state"] = "requires_review"
            assertion["acceptance_source"] = None
        else:
            assertion["review_state"] = "proposed"
            assertion["acceptance_source"] = None
    if any(item.get("review_state") == "requires_review" for item in assertions):
        return "requires_review"
    if assertions and all(item.get("review_state") == "accepted" for item in assertions):
        return "accepted"
    return "proposed"


def assign_canonical_ids(payload: dict[str, Any], *, contract_id: str) -> dict[str, str]:
    """A IA fica com ref temporário. O backend grava a identidade estável."""
    mapping: dict[str, str] = {}
    entities = [item for item in payload.get("entities") or [] if isinstance(item, dict)]
    entities.sort(key=lambda item: (str(item.get("kind")), str(item.get("state")), str(item.get("name")), str(item.get("ref"))))
    for entity in entities:
        identity = entity_identity(entity)
        if identity is None:
            canonical = f"sem:{contract_id}:entity:{entity.get('ref')}"
        elif identity[0] == "state":
            canonical = f"geo:BR:state:{identity[2]}"
        else:
            slug = identity[3].replace(" ", "_")
            canonical = f"sem:BR:municipality:{identity[2]}:{slug}"
        entity["temp_ref"] = entity.get("ref")
        entity["entity_id"] = canonical
        mapping[str(entity.get("ref"))] = canonical

    dimension_rows: list[tuple[dict[str, Any], str, Any]] = []
    for dimension in payload.get("pricing_dimensions") or []:
        if not isinstance(dimension, dict):
            continue
        kind = str(dimension.get("kind") or "dimension")
        entity_ref = dimension.get("destination_entity_ref")
        entity_id = mapping.get(str(entity_ref)) if entity_ref else None
        if entity_id and kind == "municipality":
            base = f"dim:{contract_id}:municipality:{entity_id}"
        elif kind == "carrier_defined_region":
            label = normalize_identity_text(str(dimension.get("source_label") or dimension.get("ref")))
            base = f"dim:{contract_id}:carrier_defined_region:{label.replace(' ', '_')}"
        elif kind in {"state", "state_capital", "state_interior"}:
            state = str(dimension.get("state") or "").upper()
            base = f"dim:{contract_id}:{kind}:{state}"
        elif kind == "weight_band":
            base = (
                f"dim:{contract_id}:weight_band:"
                f"{dimension.get('declared_min')}:{dimension.get('declared_max')}"
            )
        else:
            base = f"dim:{contract_id}:{kind}:{dimension.get('ref')}"
        dimension_rows.append((dimension, base, entity_ref))
    base_counts: dict[str, int] = {}
    for _dimension, base, _entity_ref in dimension_rows:
        base_counts[base] = base_counts.get(base, 0) + 1
    for dimension, base, entity_ref in dimension_rows:
        canonical = base
        if base_counts[base] > 1:
            digest = local_fingerprint(list(dimension_meaning_key(dimension))).split(":", 1)[1][:10]
            canonical = f"{base}:m:{digest}"
        dimension["temp_ref"] = dimension.get("ref")
        dimension["dimension_id"] = canonical
        mapping[str(dimension.get("ref"))] = canonical
        if entity_ref:
            dimension["destination_entity_id"] = mapping.get(str(entity_ref))

    for collection, id_field in (
        ("lanes", "lane_id"),
        ("price_rules", "rule_id"),
        ("commercial_conditions", "condition_id"),
        ("assertions", "assertion_id"),
        ("evidence", "evidence_id"),
    ):
        items = [item for item in payload.get(collection) or [] if isinstance(item, dict)]
        items.sort(key=lambda item: str(item.get("ref")))
        width = max(4, len(str(len(items))))
        for index, item in enumerate(items, start=1):
            prefix = id_field.split("_", 1)[0]
            canonical = f"{prefix}:{contract_id}:{index:0{width}d}"
            item["temp_ref"] = item.get("ref")
            item[id_field] = canonical
            mapping[str(item.get("ref"))] = canonical

    def rewrite(value: Any) -> Any:
        if isinstance(value, str) and value in mapping:
            return mapping[value]
        return value

    for lane in payload.get("lanes") or []:
        if not isinstance(lane, dict):
            continue
        origin = lane.get("origin") if isinstance(lane.get("origin"), dict) else {}
        origin["refs"] = [rewrite(item) for item in origin.get("refs") or []]
        destination = lane.get("destination") if isinstance(lane.get("destination"), dict) else {}
        if destination.get("entity_ref"):
            destination["entity_id"] = rewrite(destination.get("entity_ref"))
        if destination.get("dimension_ref"):
            destination["dimension_id"] = rewrite(destination.get("dimension_ref"))
    for rule in payload.get("price_rules") or []:
        if not isinstance(rule, dict):
            continue
        rule["lane_id"] = rewrite(rule.get("lane_ref"))
        if rule.get("dimension_ref"):
            rule["dimension_id"] = rewrite(rule.get("dimension_ref"))
        for interval in rule.get("declared_intervals") or []:
            if isinstance(interval, dict) and interval.get("weight_band_ref"):
                interval["weight_band_id"] = rewrite(interval.get("weight_band_ref"))
    for assertion in payload.get("assertions") or []:
        if not isinstance(assertion, dict):
            continue
        assertion["subject_id"] = rewrite(assertion.get("subject_ref"))
        assertion["evidence_ids"] = [rewrite(item) for item in assertion.get("evidence_refs") or []]
        assertion.setdefault("review_state", "proposed")
        assertion.setdefault("acceptance_source", None)
    return mapping


def review_state_is_known(value: Any) -> bool:
    return value in REVIEW_STATES


def acceptance_source_is_known(value: Any) -> bool:
    return value in ACCEPTANCE_SOURCES or value is None
