"""Resolvedor determinístico.

Coleta todos os candidatos aplicáveis e só então consolida.
Não chama provedor, não calcula frete e não gera chave legada de matching.
"""
from __future__ import annotations

import json
from typing import Any

from app.services.semantic_freight_contract.evidence import local_fingerprint, normalize_identity_text
from app.services.semantic_resolution.evidence import typed_evidence
from app.services.semantic_resolution.identity import identify_place
from app.services.semantic_resolution.models import (
    CAPITAL_INTERIOR_REASONS,
    EXPLICIT_PRECEDENCE,
    IDENTITY_POLICY_VERSION,
    RESOLUTION_POLICY_VERSION,
    SCHEMA_VERSION,
    UNSAFE_DIMENSION_KINDS,
)

_SUCCESS_REASONS = {
    "exact_entity": "exact_entity_match",
    "canonical_geography": "state_dimension_match",
    "coverage": "coverage_match",
    "explicit_relation": "explicit_relation",
    "human_override": "human_override",
}


class ContextResolutionFailure(ValueError):
    """Falha classificável de um contexto. Não aborta o batch."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _require_optional_str(value: Any, reason_code: str) -> None:
    if value is None or isinstance(value, str):
        return
    raise ContextResolutionFailure(reason_code)


def capital_interior_membership(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    """Placeholder fail-closed.

    Não classifica capital nem interior. BaseLocalidades não comprova capitais,
    não há autoridade capital–UF e o contrato v1 não autoriza
    interior = estado menos capital.
    """
    return {
        "resolved": False,
        "resolution_status": "unresolved",
        "review_state": "requires_review",
        "reason_codes": list(CAPITAL_INTERIOR_REASONS),
        "matched_pricing_dimension_id": None,
        "resolution_basis": "unresolved",
    }


def _unsafe_dimension(dimension: dict[str, Any]) -> bool:
    if dimension.get("kind") in UNSAFE_DIMENSION_KINDS:
        return True
    blob = normalize_identity_text(
        f"{dimension.get('predicate') or ''} {dimension.get('membership_definition') or ''}"
    )
    return "INTERIOR" in blob and "CAPITAL" in blob


def _dimensions(contract: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in contract.get("pricing_dimensions") or [] if isinstance(item, dict)]


def _dimension_map(contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        item["dimension_id"]: item
        for item in _dimensions(contract)
        if isinstance(item.get("dimension_id"), str)
    }


def _lanes_for(contract: dict[str, Any], dimension: dict[str, Any]) -> list[dict[str, Any]]:
    dimension_id = dimension.get("dimension_id")
    entity_id = dimension.get("destination_entity_id")
    found: list[dict[str, Any]] = []
    for lane in contract.get("lanes") or []:
        if not isinstance(lane, dict):
            continue
        destination = lane.get("destination") if isinstance(lane.get("destination"), dict) else {}
        lane_dimension = destination.get("dimension_id")
        lane_entity = destination.get("entity_id")
        if lane_dimension == dimension_id:
            found.append(lane)
        elif entity_id and lane_entity == entity_id and lane_dimension in (None, dimension_id):
            found.append(lane)
    return found


def _lane_signature(lane: dict[str, Any], dimension_id: str) -> tuple[Any, ...]:
    origin = lane.get("origin") if isinstance(lane.get("origin"), dict) else {}
    refs = tuple(sorted(str(item) for item in (origin.get("refs") or [])))
    return (
        origin.get("scope"),
        origin.get("kind"),
        refs,
        lane.get("service_ref"),
        dimension_id,
    )


def _origin_status(lane: dict[str, Any], origin_entity_id: str | None) -> str:
    origin = lane.get("origin") if isinstance(lane.get("origin"), dict) else {}
    scope = origin.get("scope")
    kind = origin.get("kind")
    refs = [item for item in (origin.get("refs") or []) if isinstance(item, str) and item]
    declares_origin = scope in {"single", "multiple"} or bool(refs)
    if not declares_origin:
        if origin_entity_id:
            return "incompatible"
        return "compatible"
    if kind not in {None, "municipality"}:
        return "unsupported_origin"
    if not origin_entity_id:
        return "origin_required"
    if origin_entity_id in refs and (scope != "multiple" or len(refs) == 1):
        return "compatible"
    if origin_entity_id in refs and scope == "multiple":
        return "lane_ambiguous"
    return "incompatible"


def _service_status(lane: dict[str, Any], service_ref: str | None) -> str:
    lane_service = lane.get("service_ref")
    if not lane_service:
        if service_ref:
            return "incompatible"
        return "compatible"
    if not service_ref:
        return "service_required"
    if service_ref != lane_service:
        return "incompatible"
    return "compatible"


def _date_status(relation: dict[str, Any] | None, effective_date: str | None) -> str:
    if relation is None:
        return "compatible"
    start = relation.get("valid_from")
    end = relation.get("valid_to")
    if not start and not end:
        return "compatible"
    if not effective_date:
        return "date_missing"
    if start and effective_date < str(start):
        return "date_outside"
    if end and effective_date > str(end):
        return "date_outside"
    return "compatible"


def _subject_ids(contract: dict[str, Any], dimension: dict[str, Any], lanes: list[dict[str, Any]]) -> set[str]:
    ids = {str(dimension.get("dimension_id") or "")}
    entity_id = dimension.get("destination_entity_id")
    if isinstance(entity_id, str):
        ids.add(entity_id)
    lane_ids = {lane.get("lane_id") for lane in lanes if isinstance(lane.get("lane_id"), str)}
    ids.update(str(item) for item in lane_ids)
    for rule in contract.get("price_rules") or []:
        if isinstance(rule, dict) and rule.get("lane_id") in lane_ids and isinstance(rule.get("rule_id"), str):
            ids.add(rule["rule_id"])
    ids.discard("")
    return ids


def _assertion_gate(contract: dict[str, Any], subject_ids: set[str]) -> str | None:
    bindings = contract.get("source_bindings") if isinstance(contract.get("source_bindings"), dict) else {}
    if bindings.get("fingerprint_match") is False:
        return "stale_dependency"
    relevant: list[dict[str, Any]] = []
    for assertion in contract.get("assertions") or []:
        if not isinstance(assertion, dict):
            continue
        subject = assertion.get("subject_id") or assertion.get("subject_ref")
        if subject in subject_ids:
            relevant.append(assertion)
    if any(item.get("review_state") == "rejected" for item in relevant):
        return "rejected"
    if contract.get("review_state") == "rejected":
        return "rejected"
    if any(item.get("review_state") != "accepted" for item in relevant):
        return "dimension_not_accepted"
    if contract.get("review_state") != "accepted":
        return "dimension_not_accepted"
    for finding in (contract.get("validation") or {}).get("findings") or []:
        if not isinstance(finding, dict) or finding.get("severity") != "block":
            continue
        subject = finding.get("subject_ref")
        if subject in (None, "") or subject in subject_ids:
            return "dimension_not_accepted"
    return None


def _contract_evidence(
    contract: dict[str, Any],
    *,
    dimension_id: str | None,
    lane_ids: list[str],
    assertion_refs: list[str],
) -> dict[str, Any]:
    return typed_evidence(
        "contract",
        {
            "contract_id": contract.get("contract_id"),
            "revision": contract.get("revision"),
            "content_fingerprint": _content_fingerprint(contract),
            "dimension_id": dimension_id,
            "lane_ids": list(lane_ids),
            "assertion_refs": assertion_refs,
        },
    )


def _canon_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        records,
        key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str),
    )


def _record_id(item: dict[str, Any], *fields: str) -> Any:
    for field in fields:
        if item.get(field) not in (None, ""):
            return item.get(field)
    return None


def contract_resolution_authorization_fingerprint(
    contract: dict[str, Any] | None,
    *,
    human_overrides: list[dict[str, Any]] | None = None,
    explicit_relations: list[dict[str, Any]] | None = None,
) -> str:
    """Fingerprint local do que autoriza a resolução.

    Não substitui o fingerprint de conteúdo. Sai só do snapshot recebido:
    não consulta banco nem cache global. Ordem de lista não entra no hash.
    """
    contract = contract if isinstance(contract, dict) else {}
    bindings = contract.get("source_bindings") if isinstance(contract.get("source_bindings"), dict) else {}
    validation = contract.get("validation") if isinstance(contract.get("validation"), dict) else {}
    payload = {
        "review_state": contract.get("review_state"),
        "acceptance_source": contract.get("acceptance_source"),
        "fingerprint_match": bindings.get("fingerprint_match"),
        "validation_status": validation.get("status"),
        "blocking_findings": _blocking_finding_records(validation.get("findings")),
        "assertions": _assertion_auth_records(contract.get("assertions")),
        "dimensions": _dimension_auth_records(contract.get("pricing_dimensions")),
        "lanes": _lane_auth_records(contract.get("lanes")),
        "rule_bindings": _rule_binding_records(contract.get("price_rules")),
        "human_review_revision": _contract_human_review_revision(contract),
        "explicit_precedence": _precedence_records(contract, human_overrides, explicit_relations),
    }
    return local_fingerprint(payload)


def _blocking_finding_records(findings: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in findings or []:
        if not isinstance(item, dict) or item.get("severity") != "block":
            continue
        records.append(
            {
                "code": item.get("code"),
                "severity": "block",
                "subject_ref": item.get("subject_ref"),
            }
        )
    return _canon_records(records)


def _assertion_auth_records(assertions: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in assertions or []:
        if not isinstance(item, dict):
            continue
        records.append(
            {
                "id": _record_id(item, "assertion_id", "ref"),
                "subject": _record_id(item, "subject_id", "subject_ref"),
                "review_state": item.get("review_state"),
                "acceptance_source": item.get("acceptance_source"),
                "precedence": item.get("precedence"),
            }
        )
    return _canon_records(records)


def _dimension_auth_records(dimensions: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in dimensions or []:
        if not isinstance(item, dict):
            continue
        records.append(
            {
                "dimension_id": _record_id(item, "dimension_id", "ref"),
                "kind": item.get("kind"),
                "meaning_status": item.get("meaning_status"),
                "state": item.get("state"),
                "destination_entity_id": item.get("destination_entity_id"),
                "membership_definition": item.get("membership_definition"),
                "predicate": item.get("predicate"),
            }
        )
    return _canon_records(records)


def _lane_auth_records(lanes: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in lanes or []:
        if not isinstance(item, dict):
            continue
        origin = item.get("origin") if isinstance(item.get("origin"), dict) else {}
        destination = item.get("destination") if isinstance(item.get("destination"), dict) else {}
        refs = sorted(str(ref) for ref in (origin.get("refs") or []) if ref not in (None, ""))
        records.append(
            {
                "lane_id": _record_id(item, "lane_id", "ref"),
                "origin_scope": origin.get("scope"),
                "origin_kind": origin.get("kind"),
                "origin_refs": refs,
                "service_ref": item.get("service_ref"),
                "destination_dimension_id": destination.get("dimension_id"),
                "destination_entity_id": destination.get("entity_id"),
            }
        )
    return _canon_records(records)


def _rule_binding_records(rules: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in rules or []:
        if not isinstance(item, dict):
            continue
        records.append(
            {
                "rule_id": _record_id(item, "rule_id", "ref"),
                "lane_id": _record_id(item, "lane_id", "lane_ref"),
                "dimension_id": _record_id(item, "dimension_id", "dimension_ref"),
            }
        )
    return _canon_records(records)


def _contract_human_review_revision(contract: dict[str, Any]) -> str | None:
    for key in ("human_review_revision", "review_revision", "decision_revision"):
        value = contract.get(key)
        if value not in (None, ""):
            return str(value)
    review = contract.get("review") if isinstance(contract.get("review"), dict) else {}
    for key in ("decision_revision", "revision", "event_revision"):
        value = review.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _precedence_records(
    contract: dict[str, Any],
    human_overrides: list[dict[str, Any]] | None,
    explicit_relations: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for collection in ("assertions", "pricing_dimensions", "lanes", "price_rules"):
        for item in contract.get(collection) or []:
            if not isinstance(item, dict) or item.get("precedence") in (None, ""):
                continue
            records.append(
                {
                    "collection": collection,
                    "id": _record_id(item, "dimension_id", "lane_id", "rule_id", "assertion_id", "ref"),
                    "precedence": item.get("precedence"),
                }
            )
    for item in human_overrides or []:
        if not isinstance(item, dict):
            continue
        records.append(
            {
                "collection": "human_override",
                "destination_entity_id": item.get("destination_entity_id"),
                "origin_entity_id": item.get("origin_entity_id"),
                "dimension_id": item.get("dimension_id"),
                "lane_id": item.get("lane_id"),
                "service_ref": item.get("service_ref"),
                "review_state": item.get("review_state"),
                "acceptance_source": item.get("acceptance_source"),
                "precedence": item.get("precedence"),
                "decision_revision": item.get("decision_revision"),
            }
        )
    for item in explicit_relations or []:
        if not isinstance(item, dict):
            continue
        records.append(
            {
                "collection": "explicit_relation",
                "destination_entity_id": item.get("destination_entity_id"),
                "origin_entity_id": item.get("origin_entity_id"),
                "dimension_id": item.get("dimension_id"),
                "lane_id": item.get("lane_id"),
                "service_ref": item.get("service_ref"),
                "precedence": item.get("precedence"),
                "review_state": item.get("review_state"),
            }
        )
    return _canon_records(records)


def _content_fingerprint(contract: dict[str, Any]) -> str:
    explicit = contract.get("content_fingerprint")
    if isinstance(explicit, str) and explicit:
        return explicit
    bindings = contract.get("source_bindings") if isinstance(contract.get("source_bindings"), dict) else {}
    bound = bindings.get("source_fingerprint_local")
    if isinstance(bound, str) and bound:
        return bound
    return local_fingerprint(
        {
            "contract_id": contract.get("contract_id"),
            "revision": contract.get("revision"),
            "entities": contract.get("entities"),
            "pricing_dimensions": contract.get("pricing_dimensions"),
            "lanes": contract.get("lanes"),
            "price_rules": contract.get("price_rules"),
        }
    )


def _subject_material(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Sujeito não aceito. Não usa número de linha."""
    source = raw if isinstance(raw, dict) else {}
    city = normalize_identity_text(str(source.get("city") or "")) or None
    state_raw = source.get("state")
    state = str(state_raw).strip().upper() if state_raw not in (None, "") else None
    country = normalize_identity_text(str(source.get("country") or "")) or None
    text = normalize_identity_text(str(source.get("text") or "")) or None
    if not any((city, state, country, text)):
        return None
    return {
        "unresolved_subject": text or city,
        "constraints": {
            "city": city,
            "state": state,
            "country": country,
            "text": text,
        },
    }


def _origin_raw(context: dict[str, Any] | None) -> dict[str, Any]:
    source = context if isinstance(context, dict) else {}
    return {
        "city": source.get("origin_city"),
        "state": source.get("origin_state"),
        "country": source.get("origin_country"),
        "text": source.get("origin_text"),
    }


def _assertion_refs(contract: dict[str, Any], subject_ids: set[str]) -> list[str]:
    refs: list[str] = []
    for assertion in contract.get("assertions") or []:
        if not isinstance(assertion, dict):
            continue
        subject = assertion.get("subject_id") or assertion.get("subject_ref")
        if subject in subject_ids:
            ref = assertion.get("assertion_id") or assertion.get("ref")
            if isinstance(ref, str):
                refs.append(ref)
    return sorted(refs)


def _context_view(context: dict[str, Any], origin_entity_id: str | None) -> dict[str, Any]:
    view = {
        "origin_entity_id": origin_entity_id,
        "carrier_scope_ref": context.get("carrier_scope_ref"),
        "service_ref": context.get("service_ref"),
        "effective_date": context.get("effective_date"),
    }
    for field in ("origin_city", "origin_state", "origin_country", "origin_text"):
        if context.get(field) not in (None, ""):
            view[field] = context.get(field)
    return view


def _scope_matches(relation: dict[str, Any], context: dict[str, Any], origin_entity_id: str | None) -> str:
    if relation.get("origin_entity_id") != origin_entity_id:
        if relation.get("origin_entity_id") and not origin_entity_id:
            return "origin_required"
        return "scope_mismatch"
    if relation.get("service_ref") != context.get("service_ref"):
        return "scope_mismatch"
    if relation.get("carrier_scope_ref") != context.get("carrier_scope_ref"):
        return "scope_mismatch"
    return _date_status(relation, context.get("effective_date"))


def _evaluate_lanes(
    contract: dict[str, Any],
    dimension: dict[str, Any],
    *,
    origin_entity_id: str | None,
    service_ref: str | None,
    precomputed_lanes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    lanes = list(precomputed_lanes) if precomputed_lanes is not None else _lanes_for(contract, dimension)
    if not lanes:
        return {"status": "lane_incompatible", "lane_ids": []}
    classified: list[tuple[dict[str, Any], str]] = []
    for lane in lanes:
        origin_status = _origin_status(lane, origin_entity_id)
        service_status = _service_status(lane, service_ref)
        if origin_status != "compatible":
            classified.append((lane, origin_status))
        elif service_status != "compatible":
            classified.append((lane, service_status))
        else:
            classified.append((lane, "compatible"))
    compatible = [lane for lane, status in classified if status == "compatible"]
    if not compatible:
        statuses = {status for _lane, status in classified}
        if "origin_required" in statuses:
            return {"status": "origin_required", "lane_ids": []}
        if "unsupported_origin" in statuses:
            return {"status": "unsupported_origin_scope", "lane_ids": []}
        if "service_required" in statuses or "lane_ambiguous" in statuses:
            return {"status": "lane_ambiguous", "lane_ids": []}
        return {"status": "lane_incompatible", "lane_ids": []}
    signatures = {_lane_signature(lane, str(dimension.get("dimension_id"))) for lane in compatible}
    if len(signatures) > 1:
        return {"status": "lane_ambiguous", "lane_ids": []}
    lane_ids = sorted(str(lane.get("lane_id")) for lane in compatible if lane.get("lane_id"))
    return {"status": "compatible", "lane_ids": lane_ids}


def _document(
    *,
    contract: dict[str, Any],
    destination: dict[str, Any] | None,
    context: dict[str, Any],
    origin_entity_id: str | None,
    status: str,
    review: str,
    basis: str,
    dimension_id: str | None,
    lane_ids: list[str],
    candidate_ids: list[str],
    reasons: list[str],
    evidence: list[dict[str, Any]],
    confidence: str,
    acceptance: str | None,
    policy_version: str,
    identity_policy_version: str,
    destination_input: dict[str, Any] | None = None,
    origin_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    contract_ref = {
        "contract_id": contract.get("contract_id"),
        "revision": contract.get("revision"),
        "content_fingerprint": _content_fingerprint(contract),
    }
    if isinstance(destination, dict) and destination.get("entity_id"):
        destination_identity: Any = destination.get("entity_id")
    else:
        destination_identity = _subject_material(destination_input)
    stated_origin = _subject_material(_origin_raw(origin_input))
    if origin_entity_id and stated_origin is None:
        origin_identity: Any = origin_entity_id
    elif origin_entity_id and stated_origin is not None:
        origin_identity = {"entity_id": origin_entity_id, "stated": stated_origin}
    else:
        origin_identity = stated_origin
    resolution_id = "sr:" + local_fingerprint(
        {
            "contract_ref": contract_ref,
            "destination": destination_identity,
            "origin": origin_identity,
            "carrier": context.get("carrier_scope_ref"),
            "service": context.get("service_ref"),
            "effective_date": context.get("effective_date"),
            "policy": policy_version,
        }
    ).split(":", 1)[1][:24]
    ordered_evidence = sorted(evidence, key=lambda item: str(item.get("evidence_id")))
    unique_evidence: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in ordered_evidence:
        evidence_id = item.get("evidence_id")
        if not isinstance(evidence_id, str) or evidence_id in seen:
            continue
        seen.add(evidence_id)
        unique_evidence.append(item)
    return {
        "schema_version": SCHEMA_VERSION,
        "resolution_id": resolution_id,
        "preview_only": True,
        "activated": False,
        "operational": False,
        "contract_ref": contract_ref,
        "destination_entity": destination,
        "context": _context_view(context, origin_entity_id),
        "matched_pricing_dimension_id": dimension_id,
        "matched_lane_ids": list(lane_ids),
        "resolution_basis": basis,
        "evidence_refs": [item["evidence_id"] for item in unique_evidence],
        "evidence": unique_evidence,
        "confidence": confidence,
        "resolution_status": status,
        "review_state": review,
        "acceptance_source": acceptance,
        "policy_version": policy_version,
        "identity_policy_version": identity_policy_version,
        "reason_codes": sorted(set(reasons)),
        "candidate_dimension_ids": list(candidate_ids),
    }


def _fail(
    contract: dict[str, Any],
    destination: dict[str, Any] | None,
    context: dict[str, Any],
    origin_entity_id: str | None,
    evidence: list[dict[str, Any]],
    reasons: list[str],
    *,
    review: str = "requires_review",
    status: str = "unresolved",
    candidate_ids: list[str] | None = None,
    policy_version: str,
    identity_policy_version: str,
    confidence: str = "none",
    destination_input: dict[str, Any] | None = None,
    origin_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _document(
        contract=contract,
        destination=destination,
        context=context,
        origin_entity_id=origin_entity_id,
        status=status,
        review=review,
        basis="unresolved",
        dimension_id=None,
        lane_ids=[],
        candidate_ids=sorted({item for item in (candidate_ids or []) if item}),
        reasons=reasons,
        evidence=evidence,
        confidence=confidence if status == "conflicting" else "none",
        acceptance=None,
        policy_version=policy_version,
        identity_policy_version=identity_policy_version,
        destination_input=destination_input,
        origin_input=origin_input,
    )


def _identify(identity_memo: Any, **kwargs: Any) -> dict[str, Any]:
    if identity_memo is None:
        allowed = {
            "city",
            "state",
            "country",
            "text",
            "catalog",
            "row_ref",
            "document_ref",
            "job_ref",
            "field",
        }
        return identify_place(**{key: value for key, value in kwargs.items() if key in allowed})
    return identity_memo.identify(**kwargs)


def resolve_context(
    *,
    contract: dict[str, Any],
    destination: dict[str, Any],
    context: dict[str, Any] | None = None,
    coverage_rows: list[dict[str, Any]] | None = None,
    catalog: Any = None,
    explicit_relations: list[dict[str, Any]] | None = None,
    human_overrides: list[dict[str, Any]] | None = None,
    policy_version: str | None = None,
    identity_policy_version: str | None = None,
    snapshot: Any = None,
    identity_memo: Any = None,
) -> dict[str, Any]:
    policy = policy_version or RESOLUTION_POLICY_VERSION
    identity_policy = identity_policy_version or IDENTITY_POLICY_VERSION
    if not isinstance(destination, dict):
        raise ContextResolutionFailure("invalid_destination")
    destination_input = dict(destination)
    context = dict(context or {})
    for field in ("city", "state", "country", "text"):
        _require_optional_str(destination_input.get(field), "invalid_context_value")
    for field in ("origin_city", "origin_state", "origin_country", "origin_text", "origin_entity_id", "service_ref", "carrier_scope_ref", "effective_date"):
        _require_optional_str(context.get(field), "invalid_context_value")
    if snapshot is None:
        from app.services.semantic_resolution.snapshot import prepare_resolution_snapshot

        snapshot = prepare_resolution_snapshot(
            contract=contract,
            coverage_rows=coverage_rows,
            catalog=catalog,
            explicit_relations=explicit_relations,
            human_overrides=human_overrides,
            policy_version=policy,
            identity_policy_version=identity_policy,
        )
    else:
        contract = snapshot.contract
        if explicit_relations is None:
            explicit_relations = snapshot.explicit_relations
        if human_overrides is None:
            human_overrides = snapshot.human_overrides
        if catalog is None:
            catalog = snapshot.catalog

    def fail_context(*args: Any, **kwargs: Any) -> dict[str, Any]:
        kwargs["destination_input"] = destination_input
        kwargs["origin_input"] = context
        return _fail(*args, **kwargs)

    identified = _identify(
        identity_memo,
        city=destination_input.get("city"),
        state=destination_input.get("state"),
        country=destination_input.get("country"),
        text=destination_input.get("text"),
        catalog=catalog,
        row_ref=destination_input.get("row_ref"),
        external_row_id=destination_input.get("external_row_id"),
        document_ref=destination_input.get("document_ref"),
        job_ref=destination_input.get("job_ref"),
        field=destination_input.get("field") or "destination",
    )
    evidence = list(identified["evidence"])
    origin_entity_id = context.get("origin_entity_id")
    origin_reasons: list[str] = []
    if any(context.get(key) not in (None, "") for key in ("origin_city", "origin_state", "origin_text")):
        origin = _identify(
            identity_memo,
            city=context.get("origin_city"),
            state=context.get("origin_state"),
            country=context.get("origin_country"),
            text=context.get("origin_text"),
            catalog=catalog,
            row_ref=context.get("row_ref"),
            external_row_id=context.get("external_row_id"),
            document_ref=context.get("document_ref"),
            job_ref=context.get("job_ref"),
            field="origin",
        )
        evidence.extend(origin["evidence"])
        if not origin["ok"]:
            origin_reasons.extend(origin["reason_codes"])
            if "invalid_state" in origin["reason_codes"]:
                origin_reasons.append("unsupported_origin_scope")
            elif "ambiguous_municipality" in origin["reason_codes"] and not context.get("origin_city"):
                origin_reasons.append("unsupported_origin_scope")
        else:
            computed = origin["entity"]["entity_id"]
            if origin_entity_id and origin_entity_id != computed:
                origin_reasons.append("structured_text_conflict")
            else:
                origin_entity_id = computed
    if not identified["ok"] or origin_reasons:
        reasons = list(identified["reason_codes"]) + origin_reasons
        return fail_context(
            contract,
            identified.get("entity") if identified.get("ok") else None,
            context,
            origin_entity_id,
            evidence,
            reasons,
            policy_version=policy,
            identity_policy_version=identity_policy,
        )

    destination_entity = identified["entity"]
    assert isinstance(destination_entity, dict)
    entity_id = destination_entity["entity_id"]
    state = destination_entity["state"]
    dimensions = snapshot.dimension_map

    def lanes_of(dimension: dict[str, Any]) -> list[dict[str, Any]]:
        return list(snapshot.lanes_by_dimension.get(dimension.get("dimension_id")) or [])

    relations = list(snapshot.relations_by_destination.get(entity_id) or [])
    for relation in relations:
        if _scope_matches(relation, context, origin_entity_id) == "scope_mismatch":
            continue
        evidence.extend(relation["evidence"])

    raw: list[dict[str, Any]] = []
    for dimension in dimensions.values():
        if dimension.get("kind") != "municipality":
            continue
        if dimension.get("meaning_status") == "unresolved":
            continue
        if dimension.get("destination_entity_id") == entity_id:
            raw.append(
                {
                    "basis": "exact_entity",
                    "dimension_id": dimension["dimension_id"],
                    "precedence": None,
                    "relation": None,
                }
            )

    for dimension in dimensions.values():
        if dimension.get("kind") != "state" or _unsafe_dimension(dimension):
            continue
        if dimension.get("meaning_status") == "unresolved":
            continue
        if str(dimension.get("state") or "").upper() != state:
            continue
        raw.append(
            {
                "basis": "canonical_geography",
                "dimension_id": dimension["dimension_id"],
                "precedence": None,
                "relation": None,
            }
        )

    scope_failures: list[str] = []
    for relation in relations:
        match = _scope_matches(relation, context, origin_entity_id)
        if match == "date_outside" or match == "date_missing":
            scope_failures.append("stale_dependency")
            continue
        if match == "origin_required":
            scope_failures.append("origin_required")
            continue
        if match != "compatible":
            continue
        if relation["status"] == "unique" and relation.get("pricing_dimension_id"):
            raw.append(
                {
                    "basis": "coverage",
                    "dimension_id": relation["pricing_dimension_id"],
                    "precedence": None,
                    "relation": relation,
                }
            )
        elif relation["status"] in {"ambiguous", "conflicting"}:
            raw.append(
                {
                    "basis": "coverage",
                    "dimension_id": None,
                    "precedence": None,
                    "relation": relation,
                    "blocked": relation["reason_codes"],
                    "candidate_dimension_ids": relation["candidate_dimension_ids"],
                }
            )

    for relation in explicit_relations or []:
        if not isinstance(relation, dict):
            continue
        if relation.get("destination_entity_id") != entity_id:
            continue
        if relation.get("origin_entity_id") != origin_entity_id:
            continue
        if relation.get("service_ref") != context.get("service_ref"):
            continue
        raw.append(
            {
                "basis": "explicit_relation",
                "dimension_id": relation.get("dimension_id"),
                "precedence": relation.get("precedence"),
                "relation": relation,
                "lane_id": relation.get("lane_id"),
            }
        )

    for override in human_overrides or []:
        if not isinstance(override, dict):
            continue
        if override.get("destination_entity_id") != entity_id:
            continue
        if override.get("origin_entity_id") != origin_entity_id:
            continue
        if override.get("service_ref") != context.get("service_ref"):
            continue
        evidence.append(
            typed_evidence(
                "human_review",
                {
                    "decision_revision": override.get("decision_revision"),
                    "dimension_id": override.get("dimension_id"),
                    "review_state": override.get("review_state"),
                    "precedence": override.get("precedence"),
                },
            )
        )
        if override.get("review_state") != "accepted":
            continue
        raw.append(
            {
                "basis": "human_override",
                "dimension_id": override.get("dimension_id"),
                "precedence": override.get("precedence"),
                "relation": override,
                "lane_id": override.get("lane_id"),
            }
        )

    unsafe = [
        dimension
        for dimension in dimensions.values()
        if _unsafe_dimension(dimension) and str(dimension.get("state") or "").upper() == state
    ]
    if unsafe:
        blocked = capital_interior_membership(destination_entity, unsafe)
        evidence.append(
            typed_evidence(
                "geography",
                {
                    "source_name": "capital_interior_placeholder",
                    "source_revision": None,
                    "official_authority": False,
                    "geography_revision_unavailable": True,
                    "limitation": (
                        "Sem autoridade de capital/interior. "
                        "O placeholder não classifica Campinas como interior nem São Paulo como capital."
                    ),
                    "reason_codes": blocked["reason_codes"],
                    "dimension_ids": sorted(item["dimension_id"] for item in unsafe if item.get("dimension_id")),
                },
            )
        )

    authorized: list[dict[str, Any]] = []
    blocked_reasons: list[str] = list(scope_failures)
    conflict_ids: list[str] = []
    conflict_reasons: list[str] = []
    rejected_hit = False
    for candidate in raw:
        relation = candidate.get("relation") if isinstance(candidate.get("relation"), dict) else None
        if candidate.get("blocked"):
            conflict_ids.extend(candidate.get("candidate_dimension_ids") or [])
            conflict_reasons.extend(candidate.get("blocked") or [])
            continue
        dimension = dimensions.get(candidate.get("dimension_id"))
        if dimension is None:
            blocked_reasons.append("dimension_not_found")
            continue
        lane_eval = _evaluate_lanes(
            contract,
            dimension,
            origin_entity_id=origin_entity_id,
            service_ref=context.get("service_ref"),
            precomputed_lanes=lanes_of(dimension),
        )
        if candidate.get("lane_id") and lane_eval["status"] == "compatible":
            if candidate["lane_id"] not in lane_eval["lane_ids"]:
                lane_eval = {"status": "lane_incompatible", "lane_ids": []}
            else:
                lane_eval = {"status": "compatible", "lane_ids": [candidate["lane_id"]]}
        if lane_eval["status"] != "compatible":
            blocked_reasons.append(lane_eval["status"])
            continue
        lanes = [lane for lane in lanes_of(dimension) if lane.get("lane_id") in set(lane_eval["lane_ids"])]
        subjects = _subject_ids(contract, dimension, lanes)
        gate = _assertion_gate(contract, subjects)
        if gate == "rejected":
            rejected_hit = True
            blocked_reasons.append("dimension_not_accepted")
            continue
        if gate is not None:
            blocked_reasons.append("dimension_not_accepted" if gate != "stale_dependency" else "stale_dependency")
            continue
        if relation is not None and _date_status(relation, context.get("effective_date")) in {"date_outside", "date_missing"}:
            blocked_reasons.append("stale_dependency")
            continue
        authorized.append({**candidate, "lane_ids": lane_eval["lane_ids"], "subjects": subjects})

    unsafe_ids = [item["dimension_id"] for item in unsafe if item.get("dimension_id")]
    overrides = [
        item
        for item in authorized
        if item.get("precedence") == EXPLICIT_PRECEDENCE and item["basis"] in {"human_override", "explicit_relation"}
    ]
    if len({item["dimension_id"] for item in overrides}) > 1:
        return fail_context(
            contract,
            destination_entity,
            context,
            origin_entity_id,
            evidence,
            ["overlapping_dimensions"],
            status="conflicting",
            candidate_ids=[item["dimension_id"] for item in overrides],
            policy_version=policy,
            identity_policy_version=identity_policy,
            confidence="low",
        )
    if len({item["dimension_id"] for item in overrides}) == 1:
        authorized = [item for item in authorized if item["dimension_id"] == overrides[0]["dimension_id"]]
        conflict_ids = []
    elif conflict_ids:
        return fail_context(
            contract,
            destination_entity,
            context,
            origin_entity_id,
            evidence,
            conflict_reasons or ["overlapping_dimensions"],
            status="conflicting",
            candidate_ids=list(conflict_ids) + [item["dimension_id"] for item in authorized],
            policy_version=policy,
            identity_policy_version=identity_policy,
            confidence="low",
        )
    elif unsafe_ids and authorized:
        return fail_context(
            contract,
            destination_entity,
            context,
            origin_entity_id,
            evidence,
            ["overlapping_dimensions", *CAPITAL_INTERIOR_REASONS],
            status="conflicting",
            candidate_ids=[item["dimension_id"] for item in authorized] + unsafe_ids,
            policy_version=policy,
            identity_policy_version=identity_policy,
            confidence="low",
        )

    distinct = sorted({item["dimension_id"] for item in authorized})
    if len(distinct) > 1:
        reasons = ["overlapping_dimensions"]
        bases = {item["basis"] for item in authorized}
        if "coverage" in bases and bases - {"coverage"}:
            reasons.append("conflicting_coverage")
        elif bases == {"coverage"}:
            reasons.append("conflicting_coverage")
        return fail_context(
            contract,
            destination_entity,
            context,
            origin_entity_id,
            evidence,
            reasons,
            status="conflicting",
            candidate_ids=distinct,
            policy_version=policy,
            identity_policy_version=identity_policy,
            confidence="low",
        )

    if len(distinct) == 1:
        chosen_id = distinct[0]
        chosen = next(item for item in authorized if item["dimension_id"] == chosen_id)
        basis_order = ("human_override", "explicit_relation", "exact_entity", "coverage", "canonical_geography")
        same_dimension = [item for item in authorized if item["dimension_id"] == chosen_id]
        basis = next(item for item in basis_order if any(candidate["basis"] == item for candidate in same_dimension))
        if basis == "canonical_geography":
            evidence.append(
                typed_evidence(
                    "geography",
                    {
                        "source_name": "structured_state_component",
                        "source_revision": None if catalog is None else catalog.revision,
                        "source_fingerprint": None if catalog is None else catalog.fingerprint,
                        "official_authority": False,
                        "geography_revision_unavailable": catalog is None or catalog.revision is None,
                        "method": "structured_state_component",
                        "relation": {
                            "from_entity_id": entity_id,
                            "to_state": state,
                        },
                        "limitation": (
                            "Vínculo município→UF pelo componente estadual da identidade estruturada. "
                            "Não é matching de label e não classifica capital ou interior."
                        ),
                    },
                )
            )
        lane_ids = sorted({lane_id for item in same_dimension for lane_id in item["lane_ids"]})
        subjects: set[str] = set()
        for item in same_dimension:
            subjects.update(item["subjects"])
        evidence.append(
            _contract_evidence(
                contract,
                dimension_id=chosen_id,
                lane_ids=lane_ids,
                assertion_refs=_assertion_refs(contract, subjects),
            )
        )
        acceptance = "human" if basis == "human_override" else "deterministic_policy"
        reason = _SUCCESS_REASONS.get(basis, basis)
        return _document(
            contract=contract,
            destination=destination_entity,
            context=context,
            origin_entity_id=origin_entity_id,
            status="resolved",
            review="accepted",
            basis=basis,
            dimension_id=chosen_id,
            lane_ids=lane_ids,
            candidate_ids=[],
            reasons=[reason] if reason in {"exact_entity_match", "state_dimension_match", "coverage_match"} else [],
            evidence=evidence,
            confidence=identified["confidence"] if identified["confidence"] != "none" else "high",
            acceptance=acceptance,
            policy_version=policy,
            identity_policy_version=identity_policy,
        )

    if unsafe_ids:
        return fail_context(
            contract,
            destination_entity,
            context,
            origin_entity_id,
            evidence,
            list(CAPITAL_INTERIOR_REASONS),
            policy_version=policy,
            identity_policy_version=identity_policy,
        )

    reasons = list(dict.fromkeys(blocked_reasons))
    carrier_regions = [item for item in dimensions.values() if item.get("kind") == "carrier_defined_region"]
    coverage_hit = any(item.get("basis") == "coverage" for item in raw)
    if carrier_regions and not coverage_hit and "dimension_not_accepted" not in reasons and "stale_dependency" not in reasons:
        if not any(code in reasons for code in ("origin_required", "lane_ambiguous", "lane_incompatible")):
            reasons.append("carrier_region_membership_missing")
    if not reasons:
        municipality_dimensions = [item for item in dimensions.values() if item.get("kind") == "municipality"]
        if municipality_dimensions:
            reasons.append("dimension_not_found")
        else:
            reasons.append("contract_entity_not_found")
    review = "rejected" if rejected_hit else "requires_review"
    return fail_context(
        contract,
        destination_entity,
        context,
        origin_entity_id,
        evidence,
        reasons,
        review=review,
        policy_version=policy,
        identity_policy_version=identity_policy,
    )
