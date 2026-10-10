"""Perguntas semânticas locais. Nenhuma é enviada a um modelo.

ai_eligible registra elegibilidade futura. Este módulo não executa a pergunta.
"""
from __future__ import annotations

from typing import Any

from app.services.semantic_freight_contract.evidence import local_fingerprint, normalize_identity_text
from app.services.semantic_resolution.identity import municipality_entity_id, place_candidates
from app.services.semantic_resolution.models import (
    ELIGIBILITY_VALUES,
    QUESTION_POLICY_VERSION,
    QUESTION_REVIEW_STATES,
    QUESTION_SCHEMA_VERSION,
    QUESTION_STATUSES,
    QUESTION_TYPES,
    RESOLUTION_SOURCES,
)

HUMAN_ONLY_REASONS = frozenset(
    {
        "carrier_region_membership_missing",
        "missing_capital_authority",
        "unsafe_contract_predicate",
        "requires_geographic_authority",
        "structured_text_conflict",
        "dimension_not_accepted",
        "overlapping_dimensions",
        "unsupported_country",
        "invalid_state",
        "unsupported_origin_scope",
    }
)
MISSING_EVIDENCE_REASONS = frozenset(
    {
        "origin_required",
        "ambiguous_municipality",
        "lane_ambiguous",
        "contract_entity_not_found",
        "dimension_not_found",
    }
)

QUESTION_SCHEMA: dict[str, Any] = {
    "schema_version": QUESTION_SCHEMA_VERSION,
    "required": [
        "schema_version",
        "question_id",
        "semantic_question_key",
        "tenant_scope",
        "contract_ref",
        "context_refs",
        "question_type",
        "subject",
        "constraints",
        "candidate_ids",
        "evidence_refs",
        "dependency_fingerprints",
        "eligibility",
        "reason_codes",
        "status",
        "review_state",
        "resolution_source",
        "decision_ref",
        "attempt_refs",
        "cache_ref",
        "provenance",
    ],
    "question_type": [None, *QUESTION_TYPES],
    "eligibility": list(ELIGIBILITY_VALUES),
    "status": list(QUESTION_STATUSES),
    "review_state": list(QUESTION_REVIEW_STATES),
    "resolution_source": [None, *RESOLUTION_SOURCES],
}


def _text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def _pairs(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        raw = row.get("candidate_evidence") or row.get("discriminating_evidence") or row.get("documentary_evidence") or []
        if not isinstance(raw, list):
            continue
        for item in raw:
            if not isinstance(item, dict):
                continue
            candidate_id = _text(item.get("candidate_id"))
            material = normalize_identity_text(str(item.get("material") or ""))
            if not candidate_id or not material:
                continue
            key = (candidate_id, material)
            if key in seen:
                continue
            seen.add(key)
            found.append({"candidate_id": candidate_id, "material": material})
    found.sort(key=lambda item: (item["candidate_id"], item["material"]))
    return found


def _discriminates(pairs: list[dict[str, str]], candidate_ids: list[str]) -> bool:
    if len(candidate_ids) < 2 or not pairs:
        return False
    grouped: dict[str, tuple[str, ...]] = {}
    for item in pairs:
        if item["candidate_id"] not in candidate_ids:
            continue
        current = grouped.get(item["candidate_id"], ())
        if item["material"] not in current:
            grouped[item["candidate_id"]] = tuple(sorted((*current, item["material"])))
    if set(grouped) != set(candidate_ids):
        return False
    return len(set(grouped.values())) > 1


def _catalog_candidates(row: dict[str, Any], catalog: Any) -> list[str]:
    if catalog is None:
        return []
    names: list[str] = []
    city = normalize_identity_text(str(row.get("city") or ""))
    if city:
        names.append(city)
    for name, _uf in place_candidates(str(row.get("text") or "")):
        normalized = normalize_identity_text(name)
        if normalized and normalized not in names:
            names.append(normalized)
    state = _text(row.get("state"))
    state_norm = state.upper() if state else None
    found: list[str] = []
    for name in names:
        for locality in catalog.lookup(name, None):
            if state_norm and locality.get("state") != state_norm:
                continue
            entity_id = municipality_entity_id(locality["state"], locality["city"])
            if entity_id and entity_id not in found:
                found.append(entity_id)
    return sorted(found)


def _contract_dimension_ids(contract: dict[str, Any]) -> set[str]:
    return {
        item["dimension_id"]
        for item in contract.get("pricing_dimensions") or []
        if isinstance(item, dict) and isinstance(item.get("dimension_id"), str) and item.get("dimension_id")
    }


def _subject(resolution: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    entity = resolution.get("destination_entity") if isinstance(resolution.get("destination_entity"), dict) else {}
    entity_id = entity.get("entity_id")
    return {
        "destination_entity_id": entity_id,
        "city": normalize_identity_text(str(row.get("city") or "")) or None,
        "state": (_text(row.get("state")) or "").upper() or None,
        "country": normalize_identity_text(str(row.get("country") or "")) or None,
        "text": normalize_identity_text(str(row.get("text") or "")) or None,
    }


def _constraints(row: dict[str, Any], resolution: dict[str, Any]) -> dict[str, Any]:
    context = resolution.get("context") if isinstance(resolution.get("context"), dict) else {}
    return {
        "country": normalize_identity_text(str(row.get("country") or "")) or None,
        "state": (_text(row.get("state")) or "").upper() or None,
        "origin_entity_id": context.get("origin_entity_id"),
        "origin_text": normalize_identity_text(str(row.get("origin_text") or "")) or None,
        "service_ref": context.get("service_ref"),
        "carrier_scope_ref": context.get("carrier_scope_ref"),
        "effective_date": context.get("effective_date"),
    }


def semantic_question_key(material: dict[str, Any]) -> str:
    return "sqk:" + local_fingerprint(material).split(":", 1)[1]


def decision_dependency_fingerprint(material: dict[str, Any]) -> str:
    return local_fingerprint(material)


def ai_execution_slot(dependency_fingerprint: str) -> dict[str, Any]:
    """Espaço reservado. Os campos de execução ficam vazios de propósito."""
    return {
        "decision_dependency_fingerprint": dependency_fingerprint,
        "prompt_version": None,
        "output_schema_version": None,
        "model_id": None,
    }


def _locally_valid(candidate_ids: list[str], *, catalog_ids: set[str], dimension_ids: set[str]) -> bool:
    if len(candidate_ids) < 2:
        return False
    known = catalog_ids | dimension_ids
    return all(candidate_id in known for candidate_id in candidate_ids)


def _ai_gates(
    *,
    question_type: str | None,
    candidate_ids: list[str],
    discriminates: bool,
    scope_known: bool,
    authoritative_conflict: bool,
    human_blocked: bool,
    locally_valid: bool,
    creates_dimension: bool,
) -> bool:
    return bool(
        question_type in QUESTION_TYPES
        and len(candidate_ids) >= 2
        and discriminates
        and scope_known
        and not authoritative_conflict
        and not human_blocked
        and locally_valid
        and not creates_dimension
    )


def classify_pendency(
    resolution: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    contract: dict[str, Any],
    catalog: Any,
) -> dict[str, Any] | None:
    if resolution.get("resolution_status") == "resolved":
        return None
    representative = next((row for row in rows if isinstance(row, dict)), {})
    reasons = sorted({str(code) for code in resolution.get("reason_codes") or []})
    reason_set = set(reasons)
    dimension_ids = _contract_dimension_ids(contract)
    listed = [item for item in resolution.get("candidate_dimension_ids") or [] if isinstance(item, str) and item]
    pairs = _pairs(rows)
    catalog_candidates = _catalog_candidates(representative, catalog)
    question_type: str | None = None
    candidate_ids: list[str] = sorted(set(listed))
    authoritative = "conflicting_coverage" in reason_set
    creates_dimension = False

    if authoritative:
        eligibility = "conflicting_authoritative_evidence"
    elif reason_set & HUMAN_ONLY_REASONS:
        eligibility = "human_only"
    elif "ambiguous_coverage" in reason_set and candidate_ids:
        question_type = "dimension_label_disambiguation"
        eligibility = "pending_type"
    elif "ambiguous_municipality" in reason_set and "structured_text_conflict" not in reason_set:
        candidate_ids = catalog_candidates
        question_type = "municipality_text_disambiguation" if len(candidate_ids) >= 2 else None
        eligibility = "pending_type"
    elif _text(representative.get("documentary_excerpt")) and representative.get("documentary_candidate_ids"):
        requested = [
            item
            for item in representative.get("documentary_candidate_ids") or []
            if isinstance(item, str) and item
        ]
        candidate_ids = sorted(set(requested))
        creates_dimension = any(item not in dimension_ids for item in candidate_ids)
        question_type = None if creates_dimension else "explicit_documentary_relation_extraction"
        eligibility = "human_only" if creates_dimension or not candidate_ids else "pending_type"
    elif reason_set & MISSING_EVIDENCE_REASONS or not reasons:
        eligibility = "unresolved_missing_evidence"
    else:
        eligibility = "human_only"

    discriminates = _discriminates(pairs, candidate_ids)
    if eligibility == "pending_type":
        if question_type == "municipality_text_disambiguation" and len(candidate_ids) >= 2 and not discriminates:
            eligibility = "human_only" if pairs else "unresolved_missing_evidence"
            question_type = None
        elif question_type == "dimension_label_disambiguation" and not discriminates:
            eligibility = "human_only" if pairs else "unresolved_missing_evidence"
            question_type = None
        elif question_type == "explicit_documentary_relation_extraction" and not discriminates:
            eligibility = "unresolved_missing_evidence"
            question_type = None
        else:
            known_catalog = set(catalog_candidates)
            scope_known = bool(contract.get("contract_id")) and contract.get("revision") not in (None, "")
            locally_valid = _locally_valid(
                candidate_ids,
                catalog_ids=known_catalog,
                dimension_ids=dimension_ids,
            )
            if question_type == "municipality_text_disambiguation":
                locally_valid = _locally_valid(candidate_ids, catalog_ids=known_catalog, dimension_ids=set())
            if _ai_gates(
                question_type=question_type,
                candidate_ids=candidate_ids,
                discriminates=discriminates,
                scope_known=scope_known,
                authoritative_conflict=authoritative,
                human_blocked=resolution.get("review_state") == "rejected",
                locally_valid=locally_valid,
                creates_dimension=creates_dimension,
            ):
                eligibility = "ai_eligible"
            elif not discriminates or len(candidate_ids) < 2:
                eligibility = "unresolved_missing_evidence"
                question_type = None
            else:
                eligibility = "human_only"
                question_type = None

    if resolution.get("review_state") == "rejected" and eligibility == "ai_eligible":
        eligibility = "human_only"
        question_type = None

    evidence_refs = sorted(
        {
            item.get("evidence_id")
            for item in resolution.get("evidence") or []
            if isinstance(item, dict)
            and item.get("source_type") != "operational"
            and isinstance(item.get("evidence_id"), str)
        }
    )
    return {
        "question_type": question_type,
        "eligibility": eligibility,
        "candidate_ids": candidate_ids,
        "reason_codes": reasons,
        "subject": _subject(resolution, representative),
        "constraints": _constraints(representative, resolution),
        "evidence_pairs": pairs,
        "evidence_refs": evidence_refs,
        "authoritative_conflict": authoritative,
    }


def _relevant_scope(
    classified: dict[str, Any],
    contract_ref: dict[str, Any],
    *,
    tenant_scope: str | None,
    authorization_fingerprint: str,
    coverage_fingerprint: str | None,
) -> dict[str, Any]:
    """Identidade material da pergunta. Escopo que não altera candidato, evidência ou autorização fica de fora."""
    constraints = classified["constraints"]
    origin_entity_id = constraints.get("origin_entity_id")
    origin_scope: dict[str, Any] = {"origin_entity_id": origin_entity_id}
    if not origin_entity_id:
        origin_scope["origin_text"] = constraints.get("origin_text")
    return {
        "contract_id": contract_ref.get("contract_id"),
        "revision": contract_ref.get("revision"),
        "content_fingerprint": contract_ref.get("content_fingerprint"),
        "origin": origin_scope,
        "service_ref": constraints.get("service_ref"),
        "effective_date": constraints.get("effective_date"),
        "carrier_scope_ref": constraints.get("carrier_scope_ref"),
        "tenant_scope": tenant_scope,
        "authorization_fingerprint": authorization_fingerprint,
        "coverage_fingerprint": coverage_fingerprint,
    }


def _question_material(
    classified: dict[str, Any],
    contract_ref: dict[str, Any],
    catalog: Any,
    *,
    tenant_scope: str | None,
    authorization_fingerprint: str,
    coverage_fingerprint: str | None,
) -> dict[str, Any]:
    authority = None if catalog is None else {"fingerprint": catalog.fingerprint, "revision": catalog.revision, "official": False}
    constraints = classified["constraints"]
    return {
        "question_type": classified["question_type"],
        "subject": classified["subject"],
        "scope": {
            "contract_id": contract_ref.get("contract_id"),
            "revision": contract_ref.get("revision"),
            "content_fingerprint": contract_ref.get("content_fingerprint"),
        },
        "relevant_scope": _relevant_scope(
            classified,
            contract_ref,
            tenant_scope=tenant_scope,
            authorization_fingerprint=authorization_fingerprint,
            coverage_fingerprint=coverage_fingerprint,
        ),
        "candidate_ids": list(classified["candidate_ids"]),
        "evidence": list(classified["evidence_pairs"]),
        "evidence_refs": list(classified["evidence_refs"]),
        "authority": authority,
        "constraints": {
            "country": constraints.get("country"),
            "state": constraints.get("state"),
            "origin_entity_id": constraints.get("origin_entity_id"),
            "service_ref": constraints.get("service_ref"),
            "carrier_scope_ref": constraints.get("carrier_scope_ref"),
            "effective_date": constraints.get("effective_date"),
        },
        "policy_version": QUESTION_POLICY_VERSION,
        "reason_codes": list(classified["reason_codes"]),
    }


def _dedup_identity(question_key: str, dependency: str, classified: dict[str, Any]) -> tuple[Any, ...]:
    """Fusão só com pergunta, fingerprint, candidatos, evidência e restrições equivalentes."""
    constraints = classified["constraints"]
    evidence = tuple((item["candidate_id"], item["material"]) for item in classified["evidence_pairs"])
    return (
        question_key,
        dependency,
        tuple(classified["candidate_ids"]),
        evidence,
        constraints.get("country"),
        constraints.get("state"),
        constraints.get("origin_entity_id"),
        constraints.get("service_ref"),
        constraints.get("carrier_scope_ref"),
        constraints.get("effective_date"),
    )


def _dependency_material(
    classified: dict[str, Any],
    *,
    tenant_scope: str | None,
    contract_ref: dict[str, Any],
    authorization_fingerprint: str,
    coverage_fingerprint: str | None,
    human_revision: str | None,
) -> dict[str, Any]:
    return {
        "tenant_scope": tenant_scope,
        "contract_id": contract_ref.get("contract_id"),
        "revision": contract_ref.get("revision"),
        "content_fingerprint": contract_ref.get("content_fingerprint"),
        "authorization_fingerprint": authorization_fingerprint,
        "subject": classified["subject"],
        "scope": {
            "origin_entity_id": classified["constraints"].get("origin_entity_id"),
            "service_ref": classified["constraints"].get("service_ref"),
            "carrier_scope_ref": classified["constraints"].get("carrier_scope_ref"),
            "effective_date": classified["constraints"].get("effective_date"),
        },
        "coverage_fingerprint": coverage_fingerprint,
        "evidence": list(classified["evidence_pairs"]),
        "evidence_refs": list(classified["evidence_refs"]),
        "candidate_ids": list(classified["candidate_ids"]),
        "question_schema_version": QUESTION_SCHEMA_VERSION,
        "policy_version": QUESTION_POLICY_VERSION,
        "human_decision_revision": human_revision,
    }


def _apply_human_decision(question: dict[str, Any], decision: dict[str, Any] | None) -> None:
    if not decision:
        return
    question["cache_ref"] = decision.get("cache_ref") or decision.get("decision_ref")
    question["decision_ref"] = decision.get("decision_ref")
    question["resolution_source"] = "cache"
    if decision.get("review_state") == "accepted":
        question["status"] = "resolved"
        question["review_state"] = "accepted"
        question["eligibility"] = "deterministically_resolvable"
        question["reason_codes"] = sorted({*question["reason_codes"], "human_decision_reused"})
        return
    question["status"] = "blocked"
    question["review_state"] = "rejected"
    question["eligibility"] = "human_only"
    question["question_type"] = None
    question["reason_codes"] = sorted({*question["reason_codes"], "human_decision_rejected"})


def _status_for(eligibility: str) -> tuple[str, str]:
    if eligibility == "ai_eligible":
        return "pending", "requires_review"
    if eligibility == "unresolved_missing_evidence":
        return "pending", "requires_review"
    if eligibility == "deterministically_resolvable":
        return "resolved", "not_required"
    return "blocked", "requires_review"


def build_questions(
    contexts: list[dict[str, Any]],
    *,
    contract: dict[str, Any],
    catalog: Any,
    tenant_scope: str | None,
    authorization_fingerprint: str,
    coverage_fingerprint: str | None,
    human_revision: str | None,
    human_cache: Any = None,
    policy_version: str,
    identity_policy_version: str,
) -> dict[str, Any]:
    contract_ref = {
        "contract_id": contract.get("contract_id"),
        "revision": contract.get("revision"),
        "content_fingerprint": contract.get("content_fingerprint"),
    }
    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    context_question_refs: dict[str, list[str]] = {}
    decisions: list[dict[str, Any]] = []
    for context in contexts:
        resolution = context.get("resolution")
        if not isinstance(resolution, dict):
            continue
        rows = [item.get("source_row") for item in context.get("rows") or [] if isinstance(item.get("source_row"), dict)]
        classified = classify_pendency(resolution, rows, contract=contract, catalog=catalog)
        if classified is None:
            continue
        material = _question_material(
            classified,
            contract_ref,
            catalog,
            tenant_scope=tenant_scope,
            authorization_fingerprint=authorization_fingerprint,
            coverage_fingerprint=coverage_fingerprint,
        )
        question_key = semantic_question_key(material)
        dependency = decision_dependency_fingerprint(
            _dependency_material(
                classified,
                tenant_scope=tenant_scope,
                contract_ref=contract_ref,
                authorization_fingerprint=authorization_fingerprint,
                coverage_fingerprint=coverage_fingerprint,
                human_revision=human_revision,
            )
        )
        identity = _dedup_identity(question_key, dependency, classified)
        question = merged.get(identity)
        if question is None:
            status, review = _status_for(classified["eligibility"])
            question_id = "sq:" + local_fingerprint(
                {"key": question_key, "dependency": dependency, "schema": QUESTION_SCHEMA_VERSION}
            ).split(":", 1)[1][:24]
            question = {
                "schema_version": QUESTION_SCHEMA_VERSION,
                "question_id": question_id,
                "semantic_question_key": question_key,
                "tenant_scope": tenant_scope,
                "contract_ref": contract_ref,
                "context_refs": [],
                "question_type": classified["question_type"],
                "subject": classified["subject"],
                "constraints": classified["constraints"],
                "candidate_ids": list(classified["candidate_ids"]),
                "evidence_refs": list(classified["evidence_refs"]),
                "dependency_fingerprints": {"decision": dependency, "coverage": coverage_fingerprint},
                "eligibility": classified["eligibility"],
                "reason_codes": list(classified["reason_codes"]),
                "status": status,
                "review_state": review,
                "resolution_source": None,
                "decision_ref": None,
                "attempt_refs": [],
                "cache_ref": None,
                "provenance": {
                    "question_schema_version": QUESTION_SCHEMA_VERSION,
                    "policy_version": QUESTION_POLICY_VERSION,
                    "resolution_policy_version": policy_version,
                    "identity_policy_version": identity_policy_version,
                },
                "ai_execution": ai_execution_slot(dependency),
                "consumer_refs": [],
            }
            cached = human_cache.get(question_key, dependency) if human_cache is not None else None
            _apply_human_decision(question, cached)
            if cached:
                decisions.append(
                    {
                        "decision_ref": cached.get("decision_ref"),
                        "question_id": question_id,
                        "semantic_question_key": question_key,
                        "dependency_fingerprint": dependency,
                        "review_state": cached.get("review_state"),
                        "resolution_source": "cache",
                    }
                )
            merged[identity] = question
        context_ref = context["context_ref"]
        if context_ref not in question["context_refs"]:
            question["context_refs"].append(context_ref)
        question["context_refs"].sort()
        for row in context.get("rows") or []:
            ref = row.get("row_ref")
            if ref and ref not in question["consumer_refs"]:
                question["consumer_refs"].append(ref)
        question["consumer_refs"].sort()
        context_question_refs.setdefault(context_ref, [])
        if question["question_id"] not in context_question_refs[context_ref]:
            context_question_refs[context_ref].append(question["question_id"])
    questions = sorted(merged.values(), key=lambda item: item["question_id"])
    consumers = {item["question_id"]: list(item["consumer_refs"]) for item in questions}
    return {
        "questions": questions,
        "context_question_refs": context_question_refs,
        "question_consumers": consumers,
        "decisions": decisions,
    }


def validate_question(document: dict[str, Any]) -> dict[str, Any]:
    findings: list[str] = []
    if not isinstance(document, dict):
        return {"ok": False, "findings": ["question_missing"]}
    for field in QUESTION_SCHEMA["required"]:
        if field not in document:
            findings.append("missing_field")
    if document.get("schema_version") != QUESTION_SCHEMA_VERSION:
        findings.append("schema_version")
    if document.get("question_type") not in QUESTION_SCHEMA["question_type"]:
        findings.append("question_type")
    if document.get("eligibility") not in ELIGIBILITY_VALUES:
        findings.append("eligibility")
    if document.get("status") not in QUESTION_STATUSES:
        findings.append("status")
    if document.get("review_state") not in QUESTION_REVIEW_STATES:
        findings.append("review_state")
    if document.get("resolution_source") not in QUESTION_SCHEMA["resolution_source"]:
        findings.append("resolution_source")
    proposal_source = next(item for item in RESOLUTION_SOURCES if item.startswith("semantic_"))
    if document.get("resolution_source") == proposal_source and (
        document.get("review_state") == "accepted" or document.get("status") == "resolved"
    ):
        findings.append("proposal_source_not_acceptance")
    if document.get("eligibility") == "ai_eligible" and document.get("question_type") not in QUESTION_TYPES:
        findings.append("ai_eligible_without_type")
    return {"ok": not findings, "findings": findings}
