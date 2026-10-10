"""Schema único da interpretação semântica.

A fachada billable recebe este objeto em response_schema. A validação
estrutural local percorre o mesmo objeto. O envelope persistido
(IDs, revisão, preview) é montado pelo backend e não entra no schema
enviado ao provider.
"""
from __future__ import annotations

from typing import Any

from app.services.semantic_freight_contract.models import (
    ACCESSORIAL_NAMES,
    APPLICABILITY_VALUES,
    ASSERTION_KINDS,
    CONDITION_KINDS,
    CONFIDENCE_VALUES,
    CURRENCIES,
    DESTINATION_KINDS,
    DIMENSION_KINDS,
    ENTITY_KINDS,
    EVIDENCE_ROLES,
    MAX_SNIPPET_CHARS,
    MAX_STATEMENT_CHARS,
    MEANING_STATUSES,
    ORIGIN_KINDS,
    ORIGIN_SCOPES,
    OUTPUT_SCHEMA_VERSION,
    PRESENCE_VALUES,
    PRICING_TYPES,
    TAX_NAMES,
    UNITS,
)

_DECIMAL = {"type": "string", "pattern": r"^(?:0|[1-9]\d*)(?:\.\d+)?$"}
_REF = {"type": "string", "minLength": 1, "maxLength": 40}


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    expected = schema.get("type")
    types = [expected, "null"] if isinstance(expected, str) else list(expected) + ["null"]
    copied = dict(schema)
    copied["type"] = types
    return copied


_ENTITY = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ref", "kind", "country", "labels", "aliases"],
    "properties": {
        "ref": _REF,
        "kind": {"type": "string", "enum": list(ENTITY_KINDS)},
        "country": {"type": "string", "minLength": 2, "maxLength": 2},
        "state": _nullable({"type": "string", "minLength": 2, "maxLength": 2}),
        "name": _nullable({"type": "string", "minLength": 1, "maxLength": 80}),
        "labels": {"type": "array", "items": {"type": "string", "maxLength": 120}},
        "aliases": {"type": "array", "items": {"type": "string", "maxLength": 120}},
    },
}

_DIMENSION = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ref", "kind", "meaning_status"],
    "properties": {
        "ref": _REF,
        "kind": {"type": "string", "enum": list(DIMENSION_KINDS)},
        "destination_entity_ref": _nullable(_REF),
        "source_label": _nullable({"type": "string", "maxLength": 120}),
        "meaning_status": {"type": "string", "enum": list(MEANING_STATUSES)},
        "membership_definition": _nullable({"type": "string", "maxLength": 200}),
        "state": _nullable({"type": "string", "minLength": 2, "maxLength": 2}),
        "declared_min": _nullable(_DECIMAL),
        "declared_max": _nullable(_DECIMAL),
        "unit": _nullable({"type": "string", "enum": list(UNITS)}),
    },
}

_ORIGIN = {
    "type": "object",
    "additionalProperties": False,
    "required": ["scope", "refs"],
    "properties": {
        "scope": {"type": "string", "enum": list(ORIGIN_SCOPES)},
        "kind": _nullable({"type": "string", "enum": list(ORIGIN_KINDS)}),
        "refs": {"type": "array", "items": _REF},
    },
}

_DESTINATION = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind"],
    "properties": {
        "kind": {"type": "string", "enum": list(DESTINATION_KINDS)},
        "entity_ref": _nullable(_REF),
        "dimension_ref": _nullable(_REF),
    },
}

_LANE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ref", "origin", "destination"],
    "properties": {
        "ref": _REF,
        "origin": _ORIGIN,
        "destination": _DESTINATION,
    },
}

_DECLARED_INTERVAL = {
    "type": "object",
    "additionalProperties": False,
    "required": ["declared_min", "declared_max", "amount"],
    "properties": {
        "declared_min": _DECIMAL,
        "declared_max": _DECIMAL,
        "amount": _DECIMAL,
        "weight_band_ref": _nullable(_REF),
    },
}

_PRICE_RULE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ref", "lane_ref", "pricing_type", "declared_intervals", "confidence"],
    "properties": {
        "ref": _REF,
        "lane_ref": _REF,
        "dimension_ref": _nullable(_REF),
        "pricing_type": {"type": "string", "enum": list(PRICING_TYPES)},
        "currency": _nullable({"type": "string", "enum": list(CURRENCIES)}),
        "unit": _nullable({"type": "string", "enum": list(UNITS)}),
        "declared_intervals": {"type": "array", "items": _DECLARED_INTERVAL},
        "rate_amount": _nullable(_DECIMAL),
        "excess_rate_per_kg": _nullable(_DECIMAL),
        "confidence": {"type": "string", "enum": list(CONFIDENCE_VALUES)},
    },
}

_CONDITION_NAMES = list(ACCESSORIAL_NAMES) + list(TAX_NAMES) + [
    "freight_minimum",
    "cubage",
    "delivery_term",
    "validity",
]

_PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "treatment": _nullable({"type": "string", "maxLength": 40}),
        "rate": _nullable(_DECIMAL),
        "amount": _nullable(_DECIMAL),
        "currency": _nullable({"type": "string", "enum": list(CURRENCIES)}),
        "unit": _nullable({"type": "string", "enum": list(UNITS)}),
    },
}

_CONDITION = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ref", "kind", "name", "presence", "applicability", "confidence"],
    "properties": {
        "ref": _REF,
        "kind": {"type": "string", "enum": list(CONDITION_KINDS)},
        "name": {"type": "string", "enum": _CONDITION_NAMES},
        "presence": {"type": "string", "enum": list(PRESENCE_VALUES)},
        "applicability": {"type": "string", "enum": list(APPLICABILITY_VALUES)},
        "parameters": _nullable(_PARAMETERS),
        "confidence": {"type": "string", "enum": list(CONFIDENCE_VALUES)},
    },
}

_ASSERTION = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ref", "assertion_kind", "subject_ref", "statement", "evidence_refs", "confidence"],
    "properties": {
        "ref": _REF,
        "assertion_kind": {"type": "string", "enum": list(ASSERTION_KINDS)},
        "subject_ref": _REF,
        "statement": {"type": "string", "minLength": 1, "maxLength": MAX_STATEMENT_CHARS},
        "evidence_refs": {"type": "array", "items": _REF},
        "confidence": {"type": "string", "enum": list(CONFIDENCE_VALUES)},
    },
}

_EVIDENCE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ref", "role"],
    "properties": {
        "ref": _REF,
        "document_id": _nullable({"type": "string", "maxLength": 80}),
        "ir_revision_ref": _nullable({"type": "string", "maxLength": 80}),
        "page_number": _nullable({"type": "integer"}),
        "sheet_id": _nullable({"type": "string", "maxLength": 40}),
        "block_id": _nullable({"type": "string", "maxLength": 40}),
        "cell_id": _nullable({"type": "string", "maxLength": 40}),
        "coordinate": _nullable({"type": "string", "maxLength": 20}),
        "range": _nullable({"type": "string", "maxLength": 40}),
        "role": {"type": "string", "enum": list(EVIDENCE_ROLES)},
        "snippet": _nullable({"type": "string", "maxLength": MAX_SNIPPET_CHARS}),
    },
}

INTERPRETATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "entities",
        "pricing_dimensions",
        "lanes",
        "price_rules",
        "commercial_conditions",
        "assertions",
        "evidence",
    ],
    "properties": {
        "entities": {"type": "array", "items": _ENTITY},
        "pricing_dimensions": {"type": "array", "items": _DIMENSION},
        "lanes": {"type": "array", "items": _LANE},
        "price_rules": {"type": "array", "items": _PRICE_RULE},
        "commercial_conditions": {"type": "array", "items": _CONDITION},
        "assertions": {"type": "array", "items": _ASSERTION},
        "evidence": {"type": "array", "items": _EVIDENCE},
    },
}

RESPONSE_MIME_TYPE = "application/json"


def interpretation_schema() -> dict[str, Any]:
    """O mesmo objeto usado na resposta estruturada e na validação local."""
    return INTERPRETATION_SCHEMA


def schema_version() -> str:
    return OUTPUT_SCHEMA_VERSION
