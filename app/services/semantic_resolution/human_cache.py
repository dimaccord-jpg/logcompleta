"""Cache em memória de decisão humana.

Otimização local da montagem de perguntas. Não é a autoridade final:
a revisão durável vive em SemanticHumanReview.
A chave é a pergunta semântica mais o fingerprint material da dependência.
"""
from __future__ import annotations

import copy
from typing import Any

from app.services.semantic_freight_contract.evidence import local_fingerprint


class HumanDecisionCache:
    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}

    @staticmethod
    def cache_key(question_key: str, dependency_fingerprint: str) -> str:
        return local_fingerprint(
            {
                "question_key": question_key,
                "dependency_fingerprint": dependency_fingerprint,
            }
        )

    def get(self, question_key: str, dependency_fingerprint: str) -> dict[str, Any] | None:
        found = self._store.get(self.cache_key(question_key, dependency_fingerprint))
        if found is None:
            return None
        return copy.deepcopy(found)

    def put(self, question_key: str, dependency_fingerprint: str, decision: dict[str, Any]) -> None:
        review = decision.get("review_state")
        if review not in {"accepted", "rejected"}:
            raise ValueError("human_decision_review_state")
        stored = copy.deepcopy(decision)
        stored["review_state"] = review
        self._store[self.cache_key(question_key, dependency_fingerprint)] = stored
