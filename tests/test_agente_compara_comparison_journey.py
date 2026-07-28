"""Testes da jornada comparativa: tabelas → impostos → coverage → arquivo operacional."""
from __future__ import annotations

import importlib
import io
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.agente_compara_comparison_state import (
    AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY,
    STEP_ASK_TABLE_3,
    STEP_CALCULATION_FILE,
    STEP_CONFIGURATION_READY,
    STEP_COVERAGE,
    STEP_PREPARE_TABLE_3,
    STEP_TAXES,
    create_comparison,
    get_comparison_state,
    get_table_by_slot,
)
from app.agente_compara_doc_service import (
    AGENTE_COMPARA_TEMP_TABLE_ID_SESSION_KEY,
)
from app.services.agente_compara_config_service import (
    AgenteComparaConfig,
    DEFAULT_FALLBACK_MESSAGE,
)
from tests.cleiton_doc_fixtures import make_csv, patch_cleiton_doc_cfg, patch_cleiton_doc_store
from tests.test_agente_compara_temp_table_save import (
    _bootstrap_temp_table,
    _post_temp_table_save,
)
from tests.test_cleide_audit_temp_table import _sample_audit_csv, _sample_coverage_csv


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


def _patch_ac_cfg(monkeypatch):
    cfg = AgenteComparaConfig(
        chat_enabled=True,
        upload_enabled=True,
        chat_max_history=10,
        document_context_max_chars=24000,
        max_documents_considered=3,
        question_max_chars=4000,
        fallback_message=DEFAULT_FALLBACK_MESSAGE,
        no_documents_behavior="allow_guided",
        show_documents_used=True,
        no_hallucination_instruction_enabled=True,
        audited_file_max_bytes=None,
        audited_file_max_rows=2000,
    )
    for target in (
        "app.agente_compara_api_routes.get_agente_compara_config",
        "app.agente_compara_doc_service.get_agente_compara_config",
        "app.agente_compara_doc_context.get_agente_compara_config",
    ):
        monkeypatch.setattr(target, lambda _cfg=cfg: _cfg)
    return cfg


def _setup_env(monkeypatch, tmp_path):
    patch_cleiton_doc_store(tmp_path, monkeypatch)
    cfg = patch_cleiton_doc_cfg(monkeypatch)
    monkeypatch.setattr("app.agente_compara_doc_service.get_cleiton_doc_config", lambda: cfg)
    monkeypatch.setattr("app.agente_compara_api_routes.get_cleiton_doc_config", lambda: cfg)
    monkeypatch.setattr("app.agente_compara_doc_context.get_cleiton_doc_config", lambda: cfg)
    _patch_ac_cfg(monkeypatch)
    return cfg


def _authorized(monkeypatch, web):
    fake_user = SimpleNamespace(is_authenticated=True, conta_id=1, franquia_id=1, id=1)
    monkeypatch.setattr(web, "current_user", fake_user)
    monkeypatch.setattr("app.agente_compara_api_routes.current_user", fake_user)
    monkeypatch.setattr(
        web,
        "avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True, "modo_operacao": "normal"},
    )
    monkeypatch.setattr(
        "app.agente_compara_api_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True, "modo_operacao": "normal"},
    )


@pytest.fixture
def web_client(app, tmp_path, monkeypatch, ctx):
    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
    web = _load_web_module()
    _authorized(monkeypatch, web)
    web.app.config["TESTING"] = True
    web.app.config["SECRET_KEY"] = "test-secret"
    return web.app.test_client()


def _sample_payload() -> dict:
    return {
        "status": "needs_review",
        "freight_tables": [
            {
                "name": "T1",
                "columns": ["Região", "Valor"],
                "rows": [["R1", "10"]],
            }
        ],
        "freight_routes": [],
        "accessorial_fees": [],
    }


def _prepare_two_tables_at_ask_table_3(web_client) -> str:
    comparison_id = None
    with web_client.session_transaction() as sess:
        state = create_comparison(session_obj=sess)
        comparison_id = state["comparison_id"]
        for slot in (1, 2):
            entry = next(t for t in state["tables"].values() if t["slot_number"] == slot)
            entry["temp_table_id"] = f"slot{slot}ref" if slot == 1 else None
            entry["confirmed"] = True
            entry["status"] = "confirmed"
            entry["carrier_name"] = f"Transportadora {chr(64 + slot)}"
        state["current_step"] = STEP_ASK_TABLE_3
        sess[AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = state
    return comparison_id


def _set_comparison_at_step(web_client, step: str, *, table_count: int = 2) -> dict:
    saved = _bootstrap_temp_table(web_client, _sample_payload())
    with web_client.session_transaction() as sess:
        state = get_comparison_state(sess)
        table_1 = get_table_by_slot(state, 1)
        table_2 = get_table_by_slot(state, 2)
        table_1["confirmed"] = True
        table_1["status"] = "confirmed"
        table_1["carrier_name"] = "Transportadora A"
        table_2["temp_table_id"] = None
        table_2["confirmed"] = True
        table_2["status"] = "confirmed"
        table_2["carrier_name"] = "Transportadora B"
        state["primary_temp_table_id"] = saved["temp_table_id"]
        state["desired_table_count"] = table_count
        state["current_step"] = step
        if table_count >= 3:
            from app.agente_compara_comparison_state import add_third_table

            state = add_third_table(state, session_obj=sess)
            table_3 = get_table_by_slot(state, 3)
            table_3["confirmed"] = True
            table_3["status"] = "confirmed"
            table_3["carrier_name"] = "Transportadora C"
            state["current_step"] = step
        sess[AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = state
        sess[AGENTE_COMPARA_TEMP_TABLE_ID_SESSION_KEY] = saved["temp_table_id"]
    return saved


def test_proceed_two_tables_advances_to_taxes(web_client, monkeypatch):
    import app.run_agente_compara_temp_table as temp_mod

    gemini_mock = MagicMock(side_effect=AssertionError("Gemini não deve ser chamado"))
    monkeypatch.setattr(temp_mod, "cleiton_governed_generate_content", gemini_mock)

    comparison_id = _prepare_two_tables_at_ask_table_3(web_client)
    resp = web_client.post(
        "/api/agente-compara/comparison/proceed-two-tables",
        json={"comparison_id": comparison_id},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["comparison"]["current_step"] == STEP_TAXES
    assert body["comparison"]["desired_table_count"] == 2
    assert body["next_step"] == STEP_TAXES
    assert gemini_mock.call_count == 0
    with web_client.session_transaction() as sess:
        state = get_comparison_state(sess)
        t1 = get_table_by_slot(state, 1)
        t2 = get_table_by_slot(state, 2)
        assert t1["confirmed"] and t2["confirmed"]
        assert t1["carrier_name"] == "Transportadora A"
        assert t2["carrier_name"] == "Transportadora B"
        assert state["primary_temp_table_id"] == "slot1ref"


def test_table_three_confirmation_advances_to_taxes(web_client, monkeypatch):
    import app.run_agente_compara_temp_table as temp_mod

    gemini_mock = MagicMock(side_effect=AssertionError("Gemini não deve ser chamado"))
    monkeypatch.setattr(temp_mod, "cleiton_governed_generate_content", gemini_mock)

    comparison_id = _prepare_two_tables_at_ask_table_3(web_client)
    add_resp = web_client.post(
        "/api/agente-compara/comparison/add-third-table",
        json={"comparison_id": comparison_id},
    )
    assert add_resp.status_code == 200

    with web_client.session_transaction() as sess:
        state = get_comparison_state(sess)
        state["current_step"] = STEP_PREPARE_TABLE_3
        table_3 = get_table_by_slot(state, 3)
        table_3["temp_table_id"] = "tt-3"
        table_3["confirmed"] = False
        table_3["status"] = "needs_review"
        sess[AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = state

    from app.agente_compara_comparison_state import confirm_table_and_advance

    with web_client.session_transaction() as sess:
        state = get_comparison_state(sess)
        table_3 = get_table_by_slot(state, 3)
        state = confirm_table_and_advance(state, table_3["table_id"], session_obj=sess)
        assert state["current_step"] == STEP_TAXES
        assert state["desired_table_count"] == 3


def test_global_tax_save_enables_advance_with_partial_selection(web_client):
    boot = _bootstrap_taxes_comparison_for_journey(web_client)
    with web_client.session_transaction() as sess:
        state = get_comparison_state(sess)
        table_ids = [get_table_by_slot(state, 1)["table_id"]]
    resp = web_client.post(
        "/api/agente-compara/comparison/taxes",
        json={
            "comparison_id": boot["comparison_id"],
            "tax_config": {
                "include_taxes": True,
                "origin_uf": "SP",
                "origin_city": "Campinas",
                "selected_table_ids": table_ids,
                "destination_ufs": [{"uf": "BA", "source": "manual"}],
                "icms_rates": [{"destination_uf": "BA", "applied_rate": 7}],
                "manual_added_ufs": [],
                "manual_removed_ufs": [],
            },
        },
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["comparison"]["current_step"] == STEP_TAXES
    assert body["can_advance_to_coverage"] is True


def _bootstrap_taxes_comparison_for_journey(web_client) -> dict:
    from uuid import uuid4

    from app.agente_compara_doc_service import (
        FIELD_COMPARISON_ID,
        FIELD_SLOT_NUMBER,
        FIELD_TABLE_ID,
        TEMP_TABLE_STATUS_NEEDS_REVIEW,
        _coerce_temp_table_payload,
        _temp_table_path,
        _write_temp_table_atomic,
    )

    payload = _sample_payload()
    records = {}
    with web_client.session_transaction() as sess:
        state = create_comparison(session_obj=sess)
        comparison_id = state["comparison_id"]
        for slot in (1, 2):
            entry = get_table_by_slot(state, slot)
            with web_client.application.app_context():
                with web_client.application.test_request_context():
                    record = _coerce_temp_table_payload(payload, source_doc_ids=[f"doc-{slot}"])
            record[FIELD_COMPARISON_ID] = comparison_id
            record[FIELD_TABLE_ID] = entry["table_id"]
            record[FIELD_SLOT_NUMBER] = slot
            record["temp_table_id"] = uuid4().hex
            record["status"] = TEMP_TABLE_STATUS_NEEDS_REVIEW
            with web_client.application.app_context():
                _write_temp_table_atomic(_temp_table_path(record["temp_table_id"]), record)
            entry["temp_table_id"] = record["temp_table_id"]
            entry["confirmed"] = True
            entry["status"] = "confirmed"
            entry["carrier_name"] = f"Transportadora {chr(64 + slot)}"
            entry["doc_ids"] = [f"doc-{slot}"]
            records[slot] = record
        state["primary_temp_table_id"] = records[1]["temp_table_id"]
        state["desired_table_count"] = 2
        state["current_step"] = STEP_TAXES
        sess[AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = state
        sess[AGENTE_COMPARA_TEMP_TABLE_ID_SESSION_KEY] = records[1]["temp_table_id"]
    return {"comparison_id": comparison_id, "records": records}


def test_skip_coverage_advances_to_calculation_file(web_client):
    primary = _set_comparison_at_step(web_client, STEP_COVERAGE)
    resp = _post_temp_table_save(
        web_client,
        {
            "temp_table_id": primary["temp_table_id"],
            "review_action": "skip_coverage_and_advance",
        },
    )
    assert resp.status_code == 200
    assert resp.get_json()["comparison"]["current_step"] == STEP_CALCULATION_FILE


def test_coverage_upload_advances_to_calculation_file(web_client):
    primary = _set_comparison_at_step(web_client, STEP_COVERAGE)
    resp = web_client.post(
        "/api/agente-compara/coverage/upload",
        data={"file": (io.BytesIO(_sample_coverage_csv()), "cidades.csv", "text/csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["ok"] is True
    assert body["temp_table"]["comparison"]["current_step"] == STEP_CALCULATION_FILE


def test_audit_upload_reaches_configuration_ready_without_run(web_client, monkeypatch):
    run_mock = MagicMock(side_effect=AssertionError("audit/run não deve ser chamado"))
    monkeypatch.setattr(
        "app.agente_compara_doc_service.run_audit_batch_for_session",
        run_mock,
    )
    primary = _set_comparison_at_step(web_client, STEP_CALCULATION_FILE)
    resp = web_client.post(
        "/api/agente-compara/audit/upload",
        data={"file": (io.BytesIO(_sample_audit_csv()), "auditado.csv", "text/csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["temp_table"]["comparison"]["current_step"] == STEP_CONFIGURATION_READY
    assert body["temp_table"]["audit_batch"]["status"] == "uploaded"
    assert not body["temp_table"]["audit_batch"].get("results")
    run_mock.assert_not_called()


def test_audit_run_blocked_on_configuration_ready(web_client):
    primary = _set_comparison_at_step(web_client, STEP_CONFIGURATION_READY)
    with web_client.session_transaction() as sess:
        state = get_comparison_state(sess)
        state["primary_temp_table_id"] = primary["temp_table_id"]
        sess[AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = state

    resp = web_client.post("/api/agente-compara/audit/run", json={})
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "agente_compara_comparison_not_ready"


def test_taxes_only_at_taxes_step(web_client):
    primary = _set_comparison_at_step(web_client, STEP_COVERAGE)
    resp = _post_temp_table_save(
        web_client,
        {
            "temp_table_id": primary["temp_table_id"],
            "edit_target": {"tax_config": {"include_taxes": False}},
            "review_action": "save_draft",
        },
    )
    assert resp.status_code in {400, 409}
    assert resp.get_json()["error_code"] == "agente_compara_comparison_step_invalid"


def test_e2e_two_tables_to_configuration_ready(web_client, monkeypatch):
    import app.run_agente_compara_temp_table as temp_mod

    gemini_mock = MagicMock(side_effect=AssertionError("Gemini não deve ser chamado"))
    run_mock = MagicMock(side_effect=AssertionError("audit/run não deve ser chamado"))
    monkeypatch.setattr(temp_mod, "cleiton_governed_generate_content", gemini_mock)
    monkeypatch.setattr("app.agente_compara_doc_service.run_audit_batch_for_session", run_mock)

    boot = _bootstrap_taxes_comparison_for_journey(web_client)
    save_resp = web_client.post(
        "/api/agente-compara/comparison/taxes",
        json={
            "comparison_id": boot["comparison_id"],
            "tax_config": {
                "include_taxes": False,
                "origin_uf": "SP",
                "origin_city": "Campinas",
                "selected_table_ids": [],
                "destination_ufs": [],
                "icms_rates": [],
                "manual_added_ufs": [],
                "manual_removed_ufs": [],
            },
        },
    )
    assert save_resp.status_code == 200
    advance = _post_temp_table_save(
        web_client,
        {
            "temp_table_id": boot["records"][2]["temp_table_id"],
            "comparison_id": boot["records"][2].get("comparison_id"),
            "review_action": "advance_to_coverage",
        },
    )
    assert advance.get_json()["comparison"]["current_step"] == STEP_COVERAGE

    coverage = web_client.post(
        "/api/agente-compara/coverage/upload",
        data={"file": (io.BytesIO(_sample_coverage_csv()), "cidades.csv", "text/csv")},
        content_type="multipart/form-data",
    )
    assert coverage.get_json()["temp_table"]["comparison"]["current_step"] == STEP_CALCULATION_FILE

    audit = web_client.post(
        "/api/agente-compara/audit/upload",
        data={"file": (io.BytesIO(_sample_audit_csv()), "auditado.csv", "text/csv")},
        content_type="multipart/form-data",
    )
    final = audit.get_json()["temp_table"]["comparison"]
    assert final["current_step"] == STEP_CONFIGURATION_READY
    assert final["desired_table_count"] == 2
    carriers = {t["slot_number"]: t["carrier_name"] for t in final["tables"]}
    assert carriers[1] == "Transportadora A"
    assert carriers[2] == "Transportadora B"
    run_mock.assert_not_called()
    assert gemini_mock.call_count == 0


def test_primary_temp_table_id_does_not_trigger_audit_run(web_client):
    primary = _set_comparison_at_step(web_client, STEP_TAXES)
    with web_client.session_transaction() as sess:
        state = get_comparison_state(sess)
        assert state["primary_temp_table_id"] == primary["temp_table_id"]

    resp = web_client.post("/api/agente-compara/audit/run", json={})
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "agente_compara_comparison_not_ready"
