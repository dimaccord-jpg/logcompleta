"""Validador local da proposta. Independente de modelo e de confidence."""
from __future__ import annotations

import json
from typing import Any

from app.services.semantic_question_execution.constants import (
    CONFIDENCE_VALUES,
    DECISION_FIELDS,
    DECISION_STATUSES,
    ENVELOPE_FIELDS,
    OUTPUT_SCHEMA_VERSION,
    RATIONALE_CODES,
)


class _DuplicateKey(Exception):
    pass


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    body: dict[str, Any] = {}
    for key, value in pairs:
        if key in body:
            raise _DuplicateKey(key)
        body[key] = value
    return body


def _parse_payload(payload: Any) -> tuple[Any, list[str]]:
    if not isinstance(payload, str):
        return payload, []
    try:
        return json.loads(payload, object_pairs_hook=_object_without_duplicates), []
    except _DuplicateKey:
        return None, ["duplicate_key"]
    except json.JSONDecodeError:
        return None, ["envelope_type"]


def _validate_envelope(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["envelope_type"]
    findings: list[str] = []
    if set(payload) - ENVELOPE_FIELDS:
        findings.append("additional_properties")
    if not isinstance(payload.get("decisions"), list):
        findings.append("decisions_missing")
    return findings


def validate_semantic_decisions(
    payload: Any,
    manifest: dict[str, Any],
    *,
    dependency_expected: str,
    dependency_current: str,
) -> dict[str, Any]:
    findings: list[str] = []
    if dependency_expected != dependency_current:
        findings.append("stale_dependency")
    payload, parse_findings = _parse_payload(payload)
    findings.extend(parse_findings)
    if parse_findings:
        return {"ok": False, "findings": findings, "proposal": None}
    envelope_findings = _validate_envelope(payload)
    findings.extend(envelope_findings)
    if any(item in {"envelope_type", "decisions_missing"} for item in envelope_findings):
        return {"ok": False, "findings": findings, "proposal": None}
    decisions = payload["decisions"]
    alias_map = manifest.get("alias_map") if isinstance(manifest.get("alias_map"), dict) else {}
    questions = alias_map.get("questions") if isinstance(alias_map.get("questions"), dict) else {}
    candidate_map = alias_map.get("candidates") if isinstance(alias_map.get("candidates"), dict) else {}
    evidence_map = alias_map.get("evidence") if isinstance(alias_map.get("evidence"), dict) else {}
    expected = list(manifest.get("question_aliases") or [])
    if len(decisions) != len(expected):
        findings.append("question_response_count")
    seen: list[str] = []
    mapped: list[dict[str, Any]] = []
    for decision in decisions:
        if not isinstance(decision, dict):
            findings.append("decision_type")
            continue
        if set(decision) - DECISION_FIELDS:
            findings.append("additional_properties")
        if DECISION_FIELDS - set(decision):
            findings.append("missing_field")
        if decision.get("schema_version") != OUTPUT_SCHEMA_VERSION:
            findings.append("schema_version")
        alias = decision.get("question_id")
        if not isinstance(alias, str) or alias not in questions:
            findings.append("question_alias")
            continue
        if alias in seen:
            findings.append("duplicate_question")
        else:
            seen.append(alias)
        status = decision.get("status")
        if status not in DECISION_STATUSES:
            findings.append("status")
        rationale = decision.get("rationale_code")
        if rationale not in RATIONALE_CODES:
            findings.append("rationale_code")
        confidence = decision.get("confidence")
        if confidence not in CONFIDENCE_VALUES:
            findings.append("confidence")
        known_candidates = candidate_map.get(alias) if isinstance(candidate_map.get(alias), dict) else {}
        known_evidence = evidence_map.get(alias) if isinstance(evidence_map.get(alias), dict) else {}
        candidate_alias = decision.get("selected_candidate_id")
        evidence_aliases = decision.get("evidence_refs")
        if not isinstance(evidence_aliases, list):
            findings.append("evidence_refs")
            evidence_aliases = []
        if status == "selected":
            if not isinstance(candidate_alias, str) or candidate_alias not in known_candidates:
                findings.append("candidate")
            if not evidence_aliases or any(item not in known_evidence for item in evidence_aliases):
                findings.append("evidence")
            if rationale != "evidence_selects_candidate":
                findings.append("rationale_selected")
        if status == "unresolved":
            if candidate_alias is not None:
                findings.append("unresolved_candidate")
            if confidence != "none":
                findings.append("unresolved_confidence")
            if any(item not in known_evidence for item in evidence_aliases):
                findings.append("evidence")
            if rationale not in {"insufficient_evidence", "ambiguous_evidence"}:
                findings.append("rationale_unresolved")
        if status == "selected" and isinstance(candidate_alias, str) and candidate_alias in known_candidates:
            mapped.append(
                {
                    "question_id": questions[alias],
                    "question_alias": alias,
                    "status": status,
                    "selected_candidate_id": known_candidates[candidate_alias],
                    "evidence_refs": [
                        known_evidence[item]["evidence_id"]
                        for item in evidence_aliases
                        if item in known_evidence and isinstance(known_evidence[item], dict)
                    ],
                    "rationale_code": rationale,
                    "confidence": confidence,
                    "confidence_ignored": True,
                }
            )
        elif status == "unresolved" and alias in questions:
            mapped.append(
                {
                    "question_id": questions[alias],
                    "question_alias": alias,
                    "status": status,
                    "selected_candidate_id": None,
                    "evidence_refs": [],
                    "rationale_code": rationale,
                    "confidence": confidence,
                    "confidence_ignored": True,
                }
            )
    if seen and set(seen) != set(expected):
        findings.append("question_coverage")
    if findings:
        return {"ok": False, "findings": findings, "proposal": None}
    return {
        "ok": True,
        "findings": [],
        "proposal": {
            "resolution_source": "semantic_ai",
            "review_state": "requires_review",
            "question_status": "pending",
            "accepted": False,
            "decisions": mapped,
        },
    }
