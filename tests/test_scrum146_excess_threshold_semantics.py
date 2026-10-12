"""SCRUM-146: a extração semântica entrega excess_threshold_kg já numérico.

O compilador não lê o rótulo. Esta camada só conserva o número que a IA
preenche quando a evidência é inequívoca.
"""
from __future__ import annotations

import inspect

from app.agente_compara_doc_service import (
    _preserve_structured_excess_threshold_kg,
    normalize_partial_first_extraction_to_temp_table,
)
from app.agente_compara_prompt import (
    build_agente_compara_temp_table_fallback_prompt,
    build_agente_compara_temp_table_technical_prompt,
)
from app.run_agente_compara_temp_table import temp_table_extraction_response_json_schema

_ROUTE = {
    "origin": "Sao Paulo/SP - capital",
    "destination": "Curitiba/PR",
    "freight_weight_kg": "0.54",
    "excess_threshold_kg": 500,
    "column_labels": {"freight_weight_kg": ">500 kg"},
}


def _normalize_route(route: dict) -> dict:
    normalized = normalize_partial_first_extraction_to_temp_table(
        {
            "status": "needs_review",
            "freight_tables": [
                {
                    "columns": ["Destino"],
                    "rows": [{"Destino": "Curitiba/PR"}],
                }
            ],
            "freight_routes": [route],
        }
    )
    assert len(normalized["freight_routes"]) == 1
    return normalized["freight_routes"][0]


def test_schema_exposes_nullable_numeric_threshold():
    schema = temp_table_extraction_response_json_schema()
    field = schema["properties"]["freight_routes"]["items"]["properties"]["excess_threshold_kg"]
    kinds = {item["type"] for item in field["anyOf"]}
    assert {"number", "integer", "null"} <= kinds


def test_prompts_ask_the_model_for_an_unequivocal_threshold():
    technical = build_agente_compara_temp_table_technical_prompt()
    fallback = build_agente_compara_temp_table_fallback_prompt()
    for prompt in (technical, fallback):
        assert "excess_threshold_kg" in prompt
        assert ">500 kg" in prompt
        assert "excess_threshold_kg = 500" in prompt or 'excess_threshold_kg com o numero em kg (ex.: ">500 kg" -> 500)' in prompt
        assert "null" in prompt
    assert 'rotulo ">500 kg" -> excess_threshold_kg = 500' in technical
    assert "excess_threshold_kg = null" in technical
    assert '"excess_threshold_kg": null' in technical


def test_extraction_preserves_structured_threshold_and_label_evidence():
    route = _normalize_route(dict(_ROUTE))
    assert route["freight_weight_kg"] == "0.54"
    assert route["excess_threshold_kg"] == 500
    assert route["column_labels"] == {"freight_weight_kg": ">500 kg"}

    again = _normalize_route(route)
    assert again["excess_threshold_kg"] == 500
    assert again["column_labels"]["freight_weight_kg"] == ">500 kg"


def test_label_alone_does_not_invent_a_threshold():
    payload = dict(_ROUTE)
    payload.pop("excess_threshold_kg")
    route = _normalize_route(payload)
    assert route["column_labels"]["freight_weight_kg"] == ">500 kg"
    assert route.get("excess_threshold_kg") is None


def test_ambiguous_null_stays_without_a_number():
    payload = dict(_ROUTE)
    payload["excess_threshold_kg"] = None
    payload["column_labels"] = {"freight_weight_kg": "Excedente por kg"}
    route = _normalize_route(payload)
    assert route.get("excess_threshold_kg") is None
    assert route["column_labels"]["freight_weight_kg"] == "Excedente por kg"


def test_phrase_in_the_threshold_field_is_not_parsed():
    payload = dict(_ROUTE)
    payload["excess_threshold_kg"] = ">500 kg"
    route = _normalize_route(payload)
    assert route.get("excess_threshold_kg") is None
    assert _preserve_structured_excess_threshold_kg(">500 kg") is None
    assert _preserve_structured_excess_threshold_kg("500") is None
    assert _preserve_structured_excess_threshold_kg(500) == 500
    assert _preserve_structured_excess_threshold_kg(500.0) == 500


def test_semantic_preservation_does_not_parse_text():
    source = inspect.getsource(_preserve_structured_excess_threshold_kg)
    for token in ("column_labels", "re.compile", "re.sub", ".split(", ".startswith(", ".endswith("):
        assert token not in source
