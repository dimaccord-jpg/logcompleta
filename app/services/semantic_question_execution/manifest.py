"""Manifesto imutável e evidência mínima recuperável. Sem documento inteiro."""
from __future__ import annotations

import json
from typing import Any

from app.services.semantic_freight_contract.evidence import local_fingerprint
from app.services.semantic_question_execution.constants import (
    DEFAULT_MAX_CANDIDATES,
    DEFAULT_MAX_EVIDENCE_BYTES,
    DEFAULT_MAX_EVIDENCE_ITEMS,
    DEFAULT_MAX_PAYLOAD_BYTES,
    EXECUTION_SCHEMA_VERSION,
    IGNORED_SPEC_KEYS,
    MAX_QUESTIONS_PER_REQUEST,
    MODEL_PLACEHOLDER,
    OUTPUT_SCHEMA_VERSION,
    POLICY_VERSION,
    PROMPT_VERSION,
    PROVIDER_PLACEHOLDER,
    MINIMIZATION_POLICY_VERSION,
)
from app.services.semantic_question_execution.keys import (
    ai_execution_fingerprint,
    batch_request_key,
    future_financial_attempt_key,
    question_attempt_key,
)


def dumps(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def loads(raw: str | None) -> Any:
    if not raw:
        return None
    return json.loads(raw)


def _limits(spec: dict[str, Any]) -> dict[str, int]:
    raw = spec.get("limits") if isinstance(spec.get("limits"), dict) else {}
    return {
        "max_candidates_per_question": int(raw.get("max_candidates_per_question", DEFAULT_MAX_CANDIDATES)),
        "max_evidence_items_per_question": int(raw.get("max_evidence_items_per_question", DEFAULT_MAX_EVIDENCE_ITEMS)),
        "max_evidence_bytes_per_question": int(raw.get("max_evidence_bytes_per_question", DEFAULT_MAX_EVIDENCE_BYTES)),
        "max_payload_bytes": int(raw.get("max_payload_bytes", DEFAULT_MAX_PAYLOAD_BYTES)),
        "max_questions_per_request": MAX_QUESTIONS_PER_REQUEST,
    }


def _identity(spec: dict[str, Any]) -> dict[str, Any]:
    generation = int(spec.get("generation") or 1)
    dependency = str(spec["decision_dependency_fingerprint"])
    fingerprint = ai_execution_fingerprint(
        decision_dependency_fingerprint=dependency,
        prompt_version=str(spec.get("prompt_version") or PROMPT_VERSION),
        output_schema_version=str(spec.get("output_schema_version") or OUTPUT_SCHEMA_VERSION),
        provider_id=str(spec.get("provider_id") or PROVIDER_PLACEHOLDER),
        model_id=str(spec.get("model_id") or MODEL_PLACEHOLDER),
        policy_version=str(spec.get("policy_version") or POLICY_VERSION),
        minimization_policy_version=str(spec.get("minimization_policy_version") or MINIMIZATION_POLICY_VERSION),
        config_material=dict(spec.get("config_material") or {}),
    )
    attempt_key = question_attempt_key(
        tenant_scope=str(spec["tenant_scope"]),
        semantic_question_key=str(spec["semantic_question_key"]),
        decision_dependency_fingerprint=dependency,
        ai_execution_fingerprint=fingerprint,
        generation=generation,
    )
    return {
        "generation": generation,
        "decision_dependency_fingerprint": dependency,
        "ai_execution_fingerprint": fingerprint,
        "question_attempt_key": attempt_key,
    }


def _blocked(reason: str, identity: dict[str, Any], **extra: Any) -> dict[str, Any]:
    body = {"blocked": True, "reason": reason, **extra}
    return {
        "ok": False,
        "reason": reason,
        "manifest": body,
        "evidence_manifest": {"items": [], "blocked": True, "reason": reason},
        "snapshot": {},
        "manifest_digest": local_fingerprint(body),
        "batch_request_key": None,
        "financial_attempt_key": None,
        "evidence_hashes": [],
        **identity,
    }


def build_execution_bundle(spec: dict[str, Any]) -> dict[str, Any]:
    clean = {key: value for key, value in spec.items() if key not in IGNORED_SPEC_KEYS}
    identity = _identity(clean)
    limits = _limits(clean)
    question_ids = list(clean.get("question_ids") or [clean.get("question_id")])
    if len(question_ids) > limits["max_questions_per_request"]:
        return _blocked("max_questions_per_request", identity, count=len(question_ids))
    candidate_ids = [item for item in clean.get("candidate_ids") or [] if isinstance(item, str) and item]
    if len(candidate_ids) > limits["max_candidates_per_question"]:
        return _blocked("max_candidates_per_question", identity, count=len(candidate_ids))
    raw_items = [item for item in clean.get("evidence_items") or [] if isinstance(item, dict)]
    if len(raw_items) > limits["max_evidence_items_per_question"]:
        return _blocked("max_evidence_items_per_question", identity, count=len(raw_items))
    materials = [str(item.get("material") or "") for item in raw_items]
    evidence_bytes = sum(len(item.encode("utf-8")) for item in materials)
    if evidence_bytes > limits["max_evidence_bytes_per_question"]:
        return _blocked("max_evidence_bytes_per_question", identity, bytes=evidence_bytes)
    evidence_records = []
    for item, material in zip(raw_items, materials):
        evidence_records.append(
            {
                "evidence_id": str(item.get("evidence_id") or ""),
                "candidate_id": item.get("candidate_id"),
                "material_hash": local_fingerprint(material),
                "excerpt": material,
                "source_kind": item.get("source_kind"),
                "scope": item.get("scope"),
            }
        )
    question_alias = "q1"
    question_id = str(question_ids[0])
    candidate_aliases = {
        f"c{index}": candidate_id for index, candidate_id in enumerate(sorted(set(candidate_ids)), start=1)
    }
    ordered_evidence = sorted(evidence_records, key=lambda item: (item["evidence_id"], item["material_hash"]))
    evidence_aliases: dict[str, dict[str, str]] = {}
    evidence_manifest_items = []
    for index, item in enumerate(ordered_evidence, start=1):
        alias = f"e{index}"
        evidence_aliases[alias] = {
            "evidence_id": item["evidence_id"],
            "material_hash": item["material_hash"],
        }
        evidence_manifest_items.append(
            {
                "alias": alias,
                "evidence_id": item["evidence_id"],
                "material_hash": item["material_hash"],
                "excerpt": item["excerpt"],
                "source_kind": item["source_kind"],
                "scope": item["scope"],
                "question_alias": question_alias,
                "candidate_id": item["candidate_id"],
            }
        )
    alias_map = {
        "questions": {question_alias: question_id},
        "candidates": {question_alias: candidate_aliases},
        "evidence": {question_alias: evidence_aliases},
    }
    evidence_manifest = {
        "items": evidence_manifest_items,
        "question_alias": question_alias,
    }
    generation = identity["generation"]
    dependency = identity["decision_dependency_fingerprint"]
    fingerprint = identity["ai_execution_fingerprint"]
    attempt_key = identity["question_attempt_key"]
    constraints = dict(clean.get("constraints") or {})
    contract_snapshot = dict(clean.get("contract_snapshot") or {})
    effective = {
        "schema_version": EXECUTION_SCHEMA_VERSION,
        "batch_id": clean.get("batch_id"),
        "tenant_scope": clean["tenant_scope"],
        "job_ref": clean["job_ref"],
        "contract_ref": contract_snapshot,
        "question_ids": [question_id],
        "question_aliases": [question_alias],
        "semantic_question_keys": [clean["semantic_question_key"]],
        "dependency_fingerprints": [dependency],
        "question_attempt_keys": [attempt_key],
        "prompt_version": clean.get("prompt_version") or PROMPT_VERSION,
        "output_schema_version": clean.get("output_schema_version") or OUTPUT_SCHEMA_VERSION,
        "policy_version": clean.get("policy_version") or POLICY_VERSION,
        "minimization_policy_version": clean.get("minimization_policy_version") or MINIMIZATION_POLICY_VERSION,
        "provider": clean.get("provider_id") or PROVIDER_PLACEHOLDER,
        "model_id": clean.get("model_id") or MODEL_PLACEHOLDER,
        "ai_execution_fingerprint": fingerprint,
        "evidence_manifest": evidence_manifest,
        "alias_map": alias_map,
        "request_limits": limits,
        "constraints": constraints,
        "candidate_ids": sorted(set(candidate_ids)),
    }
    effective_digest = local_fingerprint(effective)
    request_key = batch_request_key(
        tenant_scope=str(clean["tenant_scope"]),
        contract_snapshot=contract_snapshot,
        question_attempt_keys=[attempt_key],
        manifest_digest=effective_digest,
    )
    financial_key = future_financial_attempt_key(
        question_attempt_key=attempt_key,
        batch_request_key=request_key,
        generation=generation,
    )
    manifest = {
        **effective,
        "effective_payload_digest": effective_digest,
        "batch_request_key": request_key,
        "financial_attempt_key": financial_key,
    }
    encoded = dumps(manifest)
    if len(encoded.encode("utf-8")) > limits["max_payload_bytes"]:
        return _blocked("max_payload_bytes", identity, bytes=len(encoded.encode("utf-8")))
    snapshot = {
        "queue_status": clean.get("queue_status") or "current",
        "eligibility": clean.get("eligibility") or "ai_eligible",
        "question_type": clean.get("question_type"),
        "candidate_ids": sorted(set(candidate_ids)),
        "evidence_fingerprint": local_fingerprint(evidence_manifest_items),
        "dependency_fingerprint": dependency,
        "authorization_fingerprint": clean.get("authorization_fingerprint"),
        "generation": generation,
        "constraints": constraints,
    }
    public = clean.get("candidate_public")
    if isinstance(public, dict):
        snapshot["candidate_public"] = public
    ambiguous = clean.get("ambiguous_text")
    if isinstance(ambiguous, str) and ambiguous.strip():
        snapshot["ambiguous_text"] = ambiguous.strip()
    return {
        "ok": True,
        "reason": None,
        "manifest": manifest,
        "evidence_manifest": evidence_manifest,
        "snapshot": snapshot,
        "manifest_digest": local_fingerprint(manifest),
        "question_attempt_key": attempt_key,
        "batch_request_key": request_key,
        "financial_attempt_key": financial_key,
        "decision_dependency_fingerprint": dependency,
        "ai_execution_fingerprint": fingerprint,
        "generation": generation,
        "evidence_hashes": [item["material_hash"] for item in evidence_records],
    }
