"""Tetos locais do batch semântico.

Não têm relação com franquia nem com consumo. Os defaults cabem no
teste sintético de 10_000 linhas repetidas e poucos contextos.
Se um teto vier ausente ou inválido, a preparação falha fechada.
"""
from __future__ import annotations

from app.services.semantic_resolution.models import (
    DEFAULT_MAX_QUESTIONS,
    DEFAULT_MAX_QUEUE_SERIALIZED_BYTES,
    DEFAULT_MAX_RESOLUTION_CONTEXTS,
    DEFAULT_MAX_ROWS,
    DEFAULT_MAX_SHARED_EVIDENCE_ITEMS,
    DEFAULT_MAX_UNIQUE_IDENTITIES,
)


class BatchLimitExceeded(RuntimeError):
    """Teto local ausente, inválido ou estourado. Aborta o batch inteiro."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class BatchStructuralError(RuntimeError):
    """Erro invariante da entrada. Aborta o batch inteiro."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class BatchLimits:
    def __init__(
        self,
        *,
        max_rows: int = DEFAULT_MAX_ROWS,
        max_unique_identities: int = DEFAULT_MAX_UNIQUE_IDENTITIES,
        max_resolution_contexts: int = DEFAULT_MAX_RESOLUTION_CONTEXTS,
        max_questions: int = DEFAULT_MAX_QUESTIONS,
        max_shared_evidence_items: int = DEFAULT_MAX_SHARED_EVIDENCE_ITEMS,
        max_queue_serialized_bytes: int = DEFAULT_MAX_QUEUE_SERIALIZED_BYTES,
    ) -> None:
        configured = {
            "max_rows": max_rows,
            "max_unique_identities": max_unique_identities,
            "max_resolution_contexts": max_resolution_contexts,
            "max_questions": max_questions,
            "max_shared_evidence_items": max_shared_evidence_items,
            "max_queue_serialized_bytes": max_queue_serialized_bytes,
        }
        for name, value in configured.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise BatchLimitExceeded(f"unconfigured_limit:{name}")
            setattr(self, name, value)
