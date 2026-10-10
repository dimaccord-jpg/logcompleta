"""Schema local do Semantic Resolution v1.

Não é enviado a provider. O validador percorre este contrato de campos.
"""
from __future__ import annotations

from typing import Any

from app.services.semantic_resolution.models import (
    ACCEPTANCE_SOURCES,
    CONFIDENCE_VALUES,
    EVIDENCE_SOURCE_TYPES,
    REASON_CODES,
    RESOLUTION_BASES,
    RESOLUTION_STATUSES,
    RESERVED_INACTIVE_BASES,
    REVIEW_STATES,
    SCHEMA_VERSION,
)

RESOLUTION_SCHEMA: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "preview_only": True,
    "activated": False,
    "operational": False,
    "required": [
        "schema_version",
        "resolution_id",
        "contract_ref",
        "destination_entity",
        "context",
        "matched_pricing_dimension_id",
        "matched_lane_ids",
        "resolution_basis",
        "evidence_refs",
        "confidence",
        "resolution_status",
        "review_state",
        "acceptance_source",
        "policy_version",
        "reason_codes",
        "candidate_dimension_ids",
        "preview_only",
        "activated",
        "operational",
    ],
    "contract_ref": ["contract_id", "revision", "content_fingerprint"],
    "context": ["origin_entity_id", "carrier_scope_ref", "service_ref", "effective_date"],
    "resolution_status": list(RESOLUTION_STATUSES),
    "review_state": list(REVIEW_STATES),
    "resolution_basis": list(RESOLUTION_BASES),
    "reserved_inactive_bases": list(RESERVED_INACTIVE_BASES),
    "evidence_source_types": list(EVIDENCE_SOURCE_TYPES),
    "acceptance_source": list(ACCEPTANCE_SOURCES) + [None],
    "confidence": list(CONFIDENCE_VALUES),
    "reason_codes": list(REASON_CODES),
}
