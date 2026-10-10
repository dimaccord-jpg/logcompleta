"""SCRUM-146 COMP-01A: resposta sem JSON segue para o próximo model candidate."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import app.run_agente_compara_temp_table as temp_mod

TECHNICAL_MARKER = "extrator tecnico de custos de frete"
FALLBACK_MARKER = "Extraia apenas dados brutos do anexo"
VALID_JSON = (
    '{"status":"needs_review","freight_tables":[{"columns":["origem"]}],'
    '"freight_routes":[],"accessorial_fees":[],"reading_alerts":[],"evidence_refs":[]}'
)
SECRET_RESPONSE = "RESPOSTA_SIGILOSA_NAO_LOGAR_9f3a sem json aproveitavel"


def _install_extraction_stubs(monkeypatch, *, responses, models):
    calls = []
    applied = []
    cached = []
    registered = []

    def _fake_governed(client, *, model, contents, agent, flow_type, api_key_label=None, **_kw):
        calls.append(
            {
                "model": model,
                "contents": contents,
                "agent": agent,
                "flow_type": flow_type,
            }
        )
        outcome = responses[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(text=outcome)

    def _apply(payload, source_doc_ids=None, **kwargs):
        applied.append({"payload": payload, "source_doc_ids": source_doc_ids, **kwargs})
        return {
            "status": (payload or {}).get("status"),
            "reading_alerts": list((payload or {}).get("reading_alerts") or []),
            "temp_table_id": "tt-parser-fallback",
            "version_marker": temp_mod.TEMP_TABLE_VERSION_MARKER,
            "payload": payload,
        }

    def _cache(session_obj, source_doc_ids, record, **kwargs):
        cached.append({"record": record, "source_doc_ids": source_doc_ids, **kwargs})

    def _register(record, **kwargs):
        registered.append({"record": record, **kwargs})

    monkeypatch.setattr(temp_mod, "_get_model_candidates", lambda: list(models))
    monkeypatch.setattr(temp_mod, "_get_client", lambda: object())
    monkeypatch.setattr(temp_mod, "cleiton_governed_generate_content", _fake_governed)
    monkeypatch.setattr(temp_mod, "_get_cached_extraction", lambda *_a, **_k: None)
    monkeypatch.setattr(temp_mod, "_cache_extraction_result", _cache)
    monkeypatch.setattr(temp_mod, "_register_nonbillable_processing_event", _register)
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
    return calls, applied, cached, registered


def _run():
    return temp_mod.run_agente_compara_temp_table_extraction(["doc-1"], session_obj={})


def _assert_prompt_sequence(calls):
    assert calls, "provider deve ser chamado"
    assert TECHNICAL_MARKER in calls[0]["contents"]
    assert FALLBACK_MARKER not in calls[0]["contents"]
    for call in calls[1:]:
        assert FALLBACK_MARKER in call["contents"]
        assert TECHNICAL_MARKER not in call["contents"]


def test_a_unparseable_first_model_falls_through_to_valid_second(monkeypatch, caplog):
    models = ["gemini-primary", "gemini-fallback"]
    calls, applied, cached, registered = _install_extraction_stubs(
        monkeypatch,
        responses=[SECRET_RESPONSE, VALID_JSON],
        models=models,
    )

    with caplog.at_level(logging.WARNING, logger=temp_mod.logger.name):
        result = _run()

    assert [call["model"] for call in calls] == models
    assert len(calls) == 2
    _assert_prompt_sequence(calls)
    assert len(applied) == 1
    assert applied[0]["payload"]["status"] == "needs_review"
    assert temp_mod.READING_ALERT_PARSER_NO_JSON not in applied[0]["payload"].get("reading_alerts", [])
    assert result["status"] == "needs_review"
    assert len(cached) == 1
    assert len(registered) == 1
    assert "parser_no_json" in caplog.text
    assert "model=gemini-primary" in caplog.text
    assert "model_index=0" in caplog.text
    assert "motivo=parser_no_json" in caplog.text
    assert "fallback_following=True" in caplog.text
    assert SECRET_RESPONSE not in caplog.text
    assert VALID_JSON not in caplog.text


def test_b_third_model_succeeds_after_two_unparseable_responses(monkeypatch):
    models = ["gemini-primary", "gemini-fallback", "gemini-lite"]
    calls, applied, cached, _registered = _install_extraction_stubs(
        monkeypatch,
        responses=[SECRET_RESPONSE, "ainda sem json", VALID_JSON],
        models=models,
    )

    result = _run()

    assert [call["model"] for call in calls] == models
    assert len(calls) == 3
    _assert_prompt_sequence(calls)
    assert len(applied) == 1
    assert applied[0]["payload"]["freight_tables"][0]["columns"] == ["origem"]
    assert result["status"] == "needs_review"
    assert len(cached) == 1


def test_c_all_unparseable_responses_fail_once_with_parser_alert(monkeypatch, caplog):
    models = ["gemini-primary", "gemini-fallback", "gemini-lite"]
    secret_responses = [
        SECRET_RESPONSE,
        "SEGUNDO_SEGREDO_SEM_JSON",
        "TERCEIRO_SEGREDO_SEM_JSON",
    ]
    calls, applied, cached, registered = _install_extraction_stubs(
        monkeypatch,
        responses=secret_responses,
        models=models,
    )

    with caplog.at_level(logging.WARNING, logger=temp_mod.logger.name):
        result = _run()

    assert [call["model"] for call in calls] == models
    assert len(calls) == 3
    _assert_prompt_sequence(calls)
    assert len(applied) == 1
    assert applied[0]["payload"]["status"] == temp_mod.TEMP_TABLE_STATUS_FAILED
    assert applied[0]["payload"]["reading_alerts"] == [temp_mod.READING_ALERT_PARSER_NO_JSON]
    assert result["status"] == "failed"
    assert result["reading_alerts"] == [temp_mod.READING_ALERT_PARSER_NO_JSON]
    assert len(cached) == 1
    assert len(registered) == 1
    assert "fallback_following=True" in caplog.text
    assert "fallback_following=False" in caplog.text
    assert "model_index=2" in caplog.text
    for secret in secret_responses:
        assert secret not in caplog.text


def test_d_provider_exception_still_falls_through_to_valid_json(monkeypatch):
    models = ["gemini-primary", "gemini-fallback"]
    calls, applied, _cached, _registered = _install_extraction_stubs(
        monkeypatch,
        responses=[RuntimeError("provider down"), VALID_JSON],
        models=models,
    )

    result = _run()

    assert [call["model"] for call in calls] == models
    assert len(calls) == 2
    _assert_prompt_sequence(calls)
    assert len(applied) == 1
    assert applied[0]["payload"]["status"] == "needs_review"
    assert result["status"] == "needs_review"


def test_e_valid_json_on_first_model_does_not_call_fallback(monkeypatch, caplog):
    models = ["gemini-primary", "gemini-fallback", "gemini-lite"]
    calls, applied, cached, _registered = _install_extraction_stubs(
        monkeypatch,
        responses=[VALID_JSON, SECRET_RESPONSE, SECRET_RESPONSE],
        models=models,
    )

    with caplog.at_level(logging.WARNING, logger=temp_mod.logger.name):
        result = _run()

    assert len(calls) == 1
    assert calls[0]["model"] == "gemini-primary"
    assert TECHNICAL_MARKER in calls[0]["contents"]
    assert FALLBACK_MARKER not in calls[0]["contents"]
    assert len(applied) == 1
    assert result["status"] == "needs_review"
    assert len(cached) == 1
    assert "parser_no_json" not in caplog.text


def test_f_provider_call_count_matches_attempts_until_valid_payload(monkeypatch):
    models = ["gemini-primary", "gemini-fallback"]
    calls, applied, _cached, _registered = _install_extraction_stubs(
        monkeypatch,
        responses=[SECRET_RESPONSE, VALID_JSON],
        models=models,
    )

    _run()

    assert len(calls) == 2
    assert [call["model"] for call in calls] == ["gemini-primary", "gemini-fallback"]
    assert len(applied) == 1
    assert calls[0]["model"] != calls[1]["model"]
