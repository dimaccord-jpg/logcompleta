"""Validação estrutural do documento de resolução. Não recalcula frete."""
from __future__ import annotations

from typing import Any

from app.services.semantic_resolution.models import (
    ACCEPTANCE_SOURCES,
    CONFIDENCE_VALUES,
    REASON_CODES,
    RESOLUTION_BASES,
    RESOLUTION_STATUSES,
    REVIEW_STATES,
    SCHEMA_VERSION,
)
from app.services.semantic_resolution.schema import RESOLUTION_SCHEMA


def _finding(code: str) -> dict[str, str]:
    return {"code": code, "severity": "block"}


def validate_resolution(document: dict[str, Any]) -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    if not isinstance(document, dict):
        return {"ok": False, "findings": [_finding("resolution_missing")]}
    for field in RESOLUTION_SCHEMA["required"]:
        if field not in document:
            findings.append(_finding("missing_field"))
    if document.get("schema_version") != SCHEMA_VERSION:
        findings.append(_finding("schema_version"))
    if document.get("preview_only") is not True or document.get("activated") is not False:
        findings.append(_finding("preview_flags"))
    if document.get("operational") is not False:
        findings.append(_finding("preview_flags"))
    if document.get("resolution_status") not in RESOLUTION_STATUSES:
        findings.append(_finding("resolution_status"))
    if document.get("review_state") not in REVIEW_STATES:
        findings.append(_finding("review_state"))
    if document.get("resolution_status") == "unresolved" and document.get("review_state") == "unresolved":
        findings.append(_finding("status_mixed_into_review"))
    if document.get("resolution_basis") not in RESOLUTION_BASES:
        findings.append(_finding("resolution_basis"))
    if document.get("confidence") not in CONFIDENCE_VALUES:
        findings.append(_finding("confidence"))
    acceptance = document.get("acceptance_source")
    if acceptance is not None and acceptance not in ACCEPTANCE_SOURCES:
        findings.append(_finding("acceptance_source"))
    for code in document.get("reason_codes") or []:
        if code not in REASON_CODES:
            findings.append(_finding("reason_code"))
    contract_ref = document.get("contract_ref") if isinstance(document.get("contract_ref"), dict) else {}
    for field in RESOLUTION_SCHEMA["contract_ref"]:
        if field not in contract_ref:
            findings.append(_finding("contract_ref"))
    context = document.get("context") if isinstance(document.get("context"), dict) else {}
    for field in RESOLUTION_SCHEMA["context"]:
        if field not in context:
            findings.append(_finding("context"))
    evidence = document.get("evidence") if isinstance(document.get("evidence"), list) else []
    known = {
        item.get("evidence_id")
        for item in evidence
        if isinstance(item, dict) and item.get("evidence_id")
    }
    for ref in document.get("evidence_refs") or []:
        if ref not in known:
            findings.append(_finding("evidence_ref"))
    status = document.get("resolution_status")
    review = document.get("review_state")
    matched = document.get("matched_pricing_dimension_id")
    candidates = list(document.get("candidate_dimension_ids") or [])
    if status == "resolved":
        if review != "accepted" or not matched or document.get("resolution_basis") == "unresolved":
            findings.append(_finding("resolved_inconsistent"))
        if not document.get("matched_lane_ids"):
            findings.append(_finding("resolved_without_lane"))
    if status == "conflicting":
        if review != "requires_review" or matched is not None or len(candidates) < 2:
            findings.append(_finding("conflict_inconsistent"))
    if status == "unresolved" and review == "accepted":
        findings.append(_finding("unresolved_accepted"))
    if status == "resolved" and document.get("destination_entity") is None:
        findings.append(_finding("destination_missing"))
    return {"ok": not findings, "findings": findings}
