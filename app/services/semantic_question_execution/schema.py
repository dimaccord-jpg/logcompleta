"""JSON Schema puro da decisão semântica. O validador local continua obrigatório."""
from __future__ import annotations

from typing import Any

from app.services.semantic_question_execution.constants import (
    CONFIDENCE_VALUES,
    DECISION_STATUSES,
    OUTPUT_SCHEMA_VERSION,
    RATIONALE_CODES,
)

_DECISION_FIELDS = (
    "schema_version",
    "question_id",
    "status",
    "selected_candidate_id",
    "evidence_refs",
    "rationale_code",
    "confidence",
)


def decision_response_json_schema() -> dict[str, Any]:
    """Contrato em JSON Schema, com chaves aceitas em responseJsonSchema.

    O caminho response_schema do SDK projeta additional_properties,
    property_ordering e min_items/max_items em snake_case. A API rejeita isso.
    """
    decision = {
        "type": "object",
        "additionalProperties": False,
        "propertyOrdering": list(_DECISION_FIELDS),
        "required": list(_DECISION_FIELDS),
        "properties": {
            "schema_version": {"type": "string", "enum": [OUTPUT_SCHEMA_VERSION]},
            "question_id": {"type": "string"},
            "status": {"type": "string", "enum": list(DECISION_STATUSES)},
            "selected_candidate_id": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "null"},
                ]
            },
            "evidence_refs": {
                "type": "array",
                "items": {"type": "string"},
            },
            "rationale_code": {"type": "string", "enum": list(RATIONALE_CODES)},
            "confidence": {"type": "string", "enum": list(CONFIDENCE_VALUES)},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["decisions"],
        "properties": {
            "decisions": {
                "type": "array",
                "minItems": 1,
                "maxItems": 1,
                "items": decision,
            }
        },
    }
