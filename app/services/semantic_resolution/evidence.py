"""Evidência tipada da resolução. Não copia o documento e não finge célula de IR."""
from __future__ import annotations

from typing import Any

from app.services.semantic_freight_contract.evidence import local_fingerprint
from app.services.semantic_resolution.models import EVIDENCE_SOURCE_TYPES

__all__ = ["evidence_id", "local_fingerprint", "typed_evidence"]


def evidence_id(source_type: str, payload: dict[str, Any]) -> str:
    digest = local_fingerprint(payload).split(":", 1)[1][:20]
    return f"ev:{source_type}:{digest}"


def typed_evidence(source_type: str, fields: dict[str, Any]) -> dict[str, Any]:
    """A assinatura não inclui o próprio id. row_index, quando vier, é auxiliar."""
    if source_type not in EVIDENCE_SOURCE_TYPES:
        raise ValueError(f"unknown_evidence_source:{source_type}")
    body = {"source_type": source_type}
    body.update(fields)
    body["evidence_id"] = evidence_id(source_type, body)
    return body
