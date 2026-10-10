"""Executor simulado. Não importa cliente externo e não passa por billing."""
from __future__ import annotations

from typing import Any

from app.services.semantic_question_execution.constants import OUTPUT_SCHEMA_VERSION, RealProviderDisabled, SemanticExecutionError


class SimulatedUncertain(SemanticExecutionError):
    def __init__(self) -> None:
        super().__init__("simulated_uncertain")


def _decision(**overrides: Any) -> dict[str, Any]:
    body = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "question_id": "q1",
        "status": "selected",
        "selected_candidate_id": None,
        "evidence_refs": [],
        "rationale_code": "evidence_selects_candidate",
        "confidence": "high",
    }
    body.update(overrides)
    return body


class FakeSemanticProvider:
    def __init__(self, mode: str = "valid") -> None:
        self.mode = mode
        self.calls = 0

    def execute(self, manifest: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        alias = manifest["question_aliases"][0]
        candidates = sorted(manifest["alias_map"]["candidates"][alias])
        evidence = sorted(manifest["alias_map"]["evidence"][alias])
        first_candidate = candidates[0]
        first_evidence = evidence[0] if evidence else None
        mode = self.mode
        if mode == "uncertain":
            raise SimulatedUncertain()
        if mode == "valid":
            return {"decisions": [_decision(question_id=alias, selected_candidate_id=first_candidate, evidence_refs=[first_evidence])]}
        if mode == "unresolved":
            return {
                "decisions": [
                    _decision(
                        question_id=alias,
                        status="unresolved",
                        selected_candidate_id=None,
                        evidence_refs=[],
                        rationale_code="insufficient_evidence",
                        confidence="none",
                    )
                ]
            }
        if mode == "malformed":
            return {"decisions": [{"foo": 1}]}
        if mode == "invalid_candidate":
            return {"decisions": [_decision(question_id=alias, selected_candidate_id="c-unknown", evidence_refs=[first_evidence])]}
        if mode == "invalid_evidence":
            return {"decisions": [_decision(question_id=alias, selected_candidate_id=first_candidate, evidence_refs=["e-unknown"])]}
        if mode == "partial_batch":
            return {"decisions": []}
        if mode == "duplicate":
            decision = _decision(question_id=alias, selected_candidate_id=first_candidate, evidence_refs=[first_evidence])
            return {"decisions": [decision, dict(decision)]}
        if mode == "injection":
            poisoned = _decision(question_id=alias, selected_candidate_id="X", evidence_refs=[first_evidence])
            poisoned["override"] = "accepted"
            return {"decisions": [poisoned]}
        raise SemanticExecutionError("fake_mode")


class FutureBillableCall:
    """Encaixe da fachada futura. Neste lote só aceita o executor simulado."""

    def __init__(self, provider: FakeSemanticProvider) -> None:
        if not isinstance(provider, FakeSemanticProvider):
            raise RealProviderDisabled()
        self.provider = provider

    def execute(self, manifest: dict[str, Any]) -> dict[str, Any]:
        return self.provider.execute(manifest)
