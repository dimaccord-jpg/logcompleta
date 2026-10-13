"""SCRUM-146 COMP-01B: extração da temp_table usa Structured Output JSON."""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import app.run_agente_compara_temp_table as temp_mod
from app.agente_compara_doc_service import normalize_partial_first_extraction_to_temp_table

_SNAKE_SCHEMA_KEYS = {
    "additional_properties",
    "property_ordering",
    "min_items",
    "max_items",
}
PARTIAL_PAYLOAD = {
    "freight_tables": [
        {
            "columns": ["Origem"],
            "rows": [{"Origem": "Joinville"}],
        }
    ]
}
PARTIAL_JSON = json.dumps(PARTIAL_PAYLOAD, ensure_ascii=False)
VALID_JSON = (
    '{"status":"needs_review","freight_tables":[{"columns":["origem"]}],'
    '"freight_routes":[],"accessorial_fees":[],"reading_alerts":[],"evidence_refs":[]}'
)
SECRET_RESPONSE = "RESPOSTA_SIGILOSA_NAO_LOGAR_9f3a sem json aproveitavel"
_USO_CONHECIDO = SimpleNamespace(
    prompt_token_count=10,
    candidates_token_count=10,
    thoughts_token_count=0,
    total_token_count=20,
)


def _com_consumo_conhecido(origem):
    if getattr(origem, "usage_metadata", None) is not None:
        return origem
    if getattr(origem, "response", None) is not None or getattr(origem, "result", None) is not None:
        return origem
    origem.usage_metadata = _USO_CONHECIDO
    return origem


def _install(monkeypatch, *, responses, models):
    calls = []
    applied = []

    def _fake_governed(client, *, model, contents, agent, flow_type, api_key_label=None, **kwargs):
        calls.append({"model": model, "config": kwargs.get("config"), "contents": contents})
        outcome = responses[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise _com_consumo_conhecido(outcome)
        if isinstance(outcome, str):
            return SimpleNamespace(text=outcome, usage_metadata=_USO_CONHECIDO)
        return _com_consumo_conhecido(outcome)

    def _apply(payload, source_doc_ids=None, **kwargs):
        applied.append({"payload": payload, "source_doc_ids": source_doc_ids, **kwargs})
        return {
            "status": (payload or {}).get("status") or "needs_review",
            "reading_alerts": list((payload or {}).get("reading_alerts") or []),
            "temp_table_id": "tt-structured",
            "version_marker": temp_mod.TEMP_TABLE_VERSION_MARKER,
            "payload": payload,
        }

    monkeypatch.setattr(temp_mod, "_get_model_candidates", lambda: list(models))
    monkeypatch.setattr(temp_mod, "_get_client", lambda: object())
    monkeypatch.setattr(temp_mod, "cleiton_governed_generate_content", _fake_governed)
    monkeypatch.setattr(temp_mod, "_get_cached_extraction", lambda *_a, **_k: None)
    monkeypatch.setattr(temp_mod, "_cache_extraction_result", lambda *_a, **_k: None)
    monkeypatch.setattr(temp_mod, "_register_nonbillable_processing_event", lambda *_a, **_k: None)
    monkeypatch.setattr(temp_mod, "get_active_calculation_bases_for_runtime", lambda: [])
    monkeypatch.setattr(
        temp_mod,
        "build_agente_compara_document_context_for_chat",
        lambda *_a, **_k: {
            "has_documents": True,
            "context_block": "ctx",
            "gemini_file_parts": None,
        },
    )
    monkeypatch.setattr(temp_mod, "apply_temp_table_extraction_from_model_payload", _apply)
    return calls, applied


def _run():
    return temp_mod.run_agente_compara_temp_table_extraction(["doc-1"], session_obj={})


def _walk(node):
    if isinstance(node, dict):
        for key, value in node.items():
            assert key not in _SNAKE_SCHEMA_KEYS
            if key == "required":
                assert value in (None, [])
            _walk(value)
    elif isinstance(node, list):
        for item in node:
            _walk(item)


def _assert_config(config):
    assert config is not None
    dumped = config.model_dump(exclude_none=True)
    assert dumped["response_mime_type"] == "application/json"
    assert "response_schema" not in dumped
    schema = dumped["response_json_schema"]
    assert schema == temp_mod.temp_table_extraction_response_json_schema()
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert "required" not in schema
    for field in (
        "status",
        "detected_carrier",
        "freight_tables",
        "freight_routes",
        "freight_values",
        "accessorial_fees",
        "weight_ranges",
        "evidence_refs",
        "reading_alerts",
        "origins",
        "destinations",
        "routes",
        "charge_type_detected",
        "extracted_items",
        "uncertain_fields",
    ):
        assert field in schema["properties"]
    _walk(schema)
    assert set(config.model_fields_set) <= {"response_mime_type", "response_json_schema"}


def test_a_generate_content_receives_json_schema(monkeypatch):
    calls, _applied = _install(
        monkeypatch,
        responses=[VALID_JSON, SECRET_RESPONSE],
        models=["gemini-primary", "gemini-fallback"],
    )

    result = _run()

    assert len(calls) == 1
    _assert_config(calls[0]["config"])
    assert result["status"] == "needs_review"


def test_b_valid_json_on_first_model_is_a_single_call(monkeypatch):
    calls, applied = _install(
        monkeypatch,
        responses=[VALID_JSON, SECRET_RESPONSE],
        models=["gemini-primary", "gemini-fallback"],
    )

    result = _run()

    assert [call["model"] for call in calls] == ["gemini-primary"]
    _assert_config(calls[0]["config"])
    assert len(applied) == 1
    assert applied[0]["payload"]["status"] == "needs_review"
    assert result["status"] == "needs_review"


def test_c_invalid_first_response_calls_the_next_candidate(monkeypatch, caplog):
    response = SimpleNamespace(
        text=SECRET_RESPONSE,
        candidates=[SimpleNamespace(finish_reason="MAX_TOKENS")],
    )
    calls, applied = _install(
        monkeypatch,
        responses=[response, VALID_JSON],
        models=["gemini-primary", "gemini-fallback"],
    )

    with caplog.at_level(logging.WARNING, logger=temp_mod.logger.name):
        result = _run()

    assert [call["model"] for call in calls] == ["gemini-primary", "gemini-fallback"]
    for call in calls:
        _assert_config(call["config"])
    assert len(applied) == 1
    assert result["status"] == "needs_review"
    assert f"response_text_length={len(SECRET_RESPONSE)}" in caplog.text
    assert "finish_reason=MAX_TOKENS" in caplog.text
    assert "fallback_following=True" in caplog.text
    assert "model=gemini-primary" in caplog.text
    assert "model_index=0" in caplog.text
    assert SECRET_RESPONSE not in caplog.text
    assert VALID_JSON not in caplog.text


def test_d_all_invalid_responses_fail_once_with_parser_alert(monkeypatch):
    calls, applied = _install(
        monkeypatch,
        responses=["sem json", "ainda sem json"],
        models=["gemini-primary", "gemini-fallback"],
    )

    result = _run()

    assert len(calls) == 2
    for call in calls:
        _assert_config(call["config"])
    assert len(applied) == 1
    assert applied[0]["payload"]["status"] == temp_mod.TEMP_TABLE_STATUS_FAILED
    assert applied[0]["payload"]["reading_alerts"] == [temp_mod.READING_ALERT_PARSER_NO_JSON]
    assert result["status"] == "failed"
    assert result["reading_alerts"] == [temp_mod.READING_ALERT_PARSER_NO_JSON]


def test_e_provider_error_then_valid_candidate_succeeds(monkeypatch):
    calls, applied = _install(
        monkeypatch,
        responses=[RuntimeError("provider down"), VALID_JSON],
        models=["gemini-primary", "gemini-fallback"],
    )

    result = _run()

    assert [call["model"] for call in calls] == ["gemini-primary", "gemini-fallback"]
    for call in calls:
        _assert_config(call["config"])
    assert len(applied) == 1
    assert applied[0]["payload"]["status"] == "needs_review"
    assert result["status"] == "needs_review"


def test_f_partial_payload_remains_accepted(monkeypatch):
    calls, applied = _install(
        monkeypatch,
        responses=[PARTIAL_JSON, SECRET_RESPONSE],
        models=["gemini-primary", "gemini-fallback"],
    )

    result = _run()

    assert len(calls) == 1
    _assert_config(calls[0]["config"])
    assert applied[0]["payload"]["freight_tables"][0]["columns"] == ["Origem"]
    assert "status" not in applied[0]["payload"]
    assert temp_mod.READING_ALERT_PARSER_NO_JSON not in applied[0]["payload"].get("reading_alerts", [])
    normalized = normalize_partial_first_extraction_to_temp_table(applied[0]["payload"])
    assert normalized["status"] == "needs_review"
    assert normalized["freight_tables"][0]["columns"] == ["Origem"]
    assert normalized["freight_tables"][0]["rows"][0]["Origem"] == "Joinville"
    assert result["status"] == "needs_review"
