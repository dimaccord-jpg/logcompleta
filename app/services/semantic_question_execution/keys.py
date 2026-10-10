"""Chaves determinísticas da execução semântica.

Não entram refresh, processo, id de linha nem request incidental.
"""
from __future__ import annotations

from typing import Any

from app.services.semantic_freight_contract.evidence import local_fingerprint
from app.services.semantic_question_execution.constants import SemanticExecutionError


def ai_execution_fingerprint(
    *,
    decision_dependency_fingerprint: str,
    prompt_version: str,
    output_schema_version: str,
    provider_id: str,
    model_id: str,
    policy_version: str,
    minimization_policy_version: str,
    config_material: dict[str, Any],
) -> str:
    return local_fingerprint(
        {
            "decision_dependency_fingerprint": decision_dependency_fingerprint,
            "prompt_version": prompt_version,
            "output_schema_version": output_schema_version,
            "provider_id": provider_id,
            "model_id": model_id,
            "policy_version": policy_version,
            "minimization_policy_version": minimization_policy_version,
            "config_material": config_material,
        }
    )


def question_attempt_key(
    *,
    tenant_scope: str,
    semantic_question_key: str,
    decision_dependency_fingerprint: str,
    ai_execution_fingerprint: str,
    generation: int,
) -> str:
    return local_fingerprint(
        {
            "tenant_scope": tenant_scope,
            "semantic_question_key": semantic_question_key,
            "decision_dependency_fingerprint": decision_dependency_fingerprint,
            "ai_execution_fingerprint": ai_execution_fingerprint,
            "generation": int(generation),
        }
    )


def batch_request_key(
    *,
    tenant_scope: str,
    contract_snapshot: dict[str, Any],
    question_attempt_keys: list[str],
    manifest_digest: str,
) -> str:
    return local_fingerprint(
        {
            "tenant_scope": tenant_scope,
            "contract_snapshot": contract_snapshot,
            "question_attempt_keys": sorted(question_attempt_keys),
            "manifest_digest": manifest_digest,
        }
    )


def future_financial_attempt_key(
    *,
    question_attempt_key: str,
    batch_request_key: str,
    generation: int,
) -> str:
    """Chave local estável, no tamanho da attempt_key financeira futura. Não reserva franquia."""
    digest = local_fingerprint(
        {
            "question_attempt_key": question_attempt_key,
            "batch_request_key": batch_request_key,
            "generation": int(generation),
        }
    )
    if not 1 <= len(digest) <= 160:
        raise SemanticExecutionError("financial_attempt_key_size")
    return digest


def review_material_fingerprint(*, candidate_ids: list[str], evidence_hashes: list[str]) -> str:
    """Material da pergunta. O candidato escolhido fica na revisão, fora deste hash."""
    return local_fingerprint(
        {
            "candidate_ids": sorted(candidate_ids),
            "evidence_hashes": sorted(evidence_hashes),
        }
    )
