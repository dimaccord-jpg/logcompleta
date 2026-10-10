"""Fila técnica local. Sem worker, sem fila assíncrona e sem execução externa."""
from __future__ import annotations

import json
from typing import Any

from app.services.semantic_freight_contract.evidence import local_fingerprint
from app.services.semantic_resolution.models import QUESTION_SCHEMA_VERSION, QUEUE_SCHEMA_VERSION
from app.services.semantic_resolution.questions import validate_question

# Dependências que invalidam a fila. A comparação usa o fingerprint deste conjunto,
# não um subconjunto manual de content/coverage/authorization.
QUEUE_DEPENDENCY_FIELDS = (
    "contract_id",
    "revision",
    "contract_content_fingerprint",
    "authorization_fingerprint",
    "coverage_fingerprint",
    "geography_revision",
    "human_decision_revision",
    "policy_version",
    "identity_policy_version",
    "resolution_policy_version",
    "question_schema_version",
)


QUEUE_SCHEMA: dict[str, Any] = {
    "schema_version": QUEUE_SCHEMA_VERSION,
    "required": [
        "schema_version",
        "job_ref",
        "tenant_scope",
        "dependency_snapshot",
        "questions",
        "context_question_refs",
        "decisions",
        "attempts",
        "review_events",
        "shared_evidence",
        "stats",
        "preview_only",
        "activated",
        "operational",
    ],
}


def _canonical_dependency_value(value: Any) -> Any:
    """Ordem de chave ou de lista não é mudança material."""
    if isinstance(value, dict):
        return {key: _canonical_dependency_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        items = [_canonical_dependency_value(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False, default=str))
    return value


def queue_dependency_material(source: dict[str, Any] | None) -> dict[str, Any]:
    payload = source if isinstance(source, dict) else {}
    return {
        field: _canonical_dependency_value(payload.get(field))
        for field in QUEUE_DEPENDENCY_FIELDS
    }


def queue_dependency_fingerprint(source: dict[str, Any] | None) -> str:
    return local_fingerprint(queue_dependency_material(source))


def shared_coverage_evidence(snapshot: Any) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    coverage = getattr(snapshot, "coverage", {}) or {}
    for relation in coverage.get("relations") or []:
        for item in relation.get("evidence") or []:
            if isinstance(item, dict) and isinstance(item.get("evidence_id"), str):
                found[item["evidence_id"]] = item
    return [found[key] for key in sorted(found)]


def build_resolution_queue(
    *,
    job_ref: str | None,
    tenant_scope: str | None,
    snapshot: Any,
    built: dict[str, Any],
    stats: dict[str, Any],
    context_failures: list[dict[str, Any]],
) -> dict[str, Any]:
    dependency_snapshot = {
        "contract_id": snapshot.contract.get("contract_id"),
        "revision": snapshot.contract.get("revision"),
        "contract_content_fingerprint": snapshot.content_fingerprint,
        "authorization_fingerprint": snapshot.authorization_fingerprint,
        "coverage_fingerprint": snapshot.coverage_fingerprint,
        "geography_revision": snapshot.geography_revision,
        "human_decision_revision": snapshot.human_revision,
        "policy_version": snapshot.policy_version,
        "identity_policy_version": snapshot.identity_policy_version,
        "resolution_policy_version": snapshot.policy_version,
        "question_schema_version": QUESTION_SCHEMA_VERSION,
    }
    dependency_snapshot["dependency_fingerprint"] = queue_dependency_fingerprint(dependency_snapshot)
    queue_stats = dict(stats)
    queue_stats["context_failures"] = list(context_failures)
    queue_stats["ai_eligible_count"] = sum(1 for item in built["questions"] if item.get("eligibility") == "ai_eligible")
    queue_stats["question_count"] = len(built["questions"])
    queue_stats["shared_evidence_count"] = len(shared_coverage_evidence(snapshot))
    return {
        "schema_version": QUEUE_SCHEMA_VERSION,
        "job_ref": job_ref,
        "tenant_scope": tenant_scope,
        "dependency_snapshot": dependency_snapshot,
        "dependency_status": "current",
        "questions": built["questions"],
        "context_question_refs": built["context_question_refs"],
        "question_consumers": built["question_consumers"],
        "decisions": built["decisions"],
        "attempts": [],
        "review_events": [],
        "shared_evidence": shared_coverage_evidence(snapshot),
        "stats": queue_stats,
        "preview_only": True,
        "activated": False,
        "operational": False,
    }


def validate_queue(document: dict[str, Any]) -> dict[str, Any]:
    findings: list[str] = []
    if not isinstance(document, dict):
        return {"ok": False, "findings": ["queue_missing"]}
    for field in QUEUE_SCHEMA["required"]:
        if field not in document:
            findings.append("missing_field")
    if document.get("schema_version") != QUEUE_SCHEMA_VERSION:
        findings.append("schema_version")
    if document.get("preview_only") is not True or document.get("activated") is not False or document.get("operational") is not False:
        findings.append("preview_flags")
    snapshot = document.get("dependency_snapshot") if isinstance(document.get("dependency_snapshot"), dict) else {}
    for field in ("contract_content_fingerprint", "authorization_fingerprint", "coverage_fingerprint"):
        if field not in snapshot:
            findings.append("dependency_snapshot")
    if not isinstance(document.get("questions"), list):
        findings.append("questions")
    else:
        for question in document["questions"]:
            if not validate_question(question)["ok"]:
                findings.append("question")
                break
    if not isinstance(document.get("attempts"), list):
        findings.append("attempts")
    if not isinstance(document.get("shared_evidence"), list):
        findings.append("shared_evidence")
    if document.get("dependency_status") not in {None, "current", "stale"}:
        findings.append("dependency_status")
    return {"ok": not findings, "findings": findings}


def queue_serialized_size(queue: dict[str, Any]) -> int:
    raw = json.dumps(queue, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)
    return len(raw.encode("utf-8"))
