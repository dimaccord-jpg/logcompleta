"""Testes fiscais globais do AgenteCompara (TAXES por cenário)."""
from __future__ import annotations

import importlib
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.agente_compara_comparison_state import (
    AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY,
    STEP_COVERAGE,
    STEP_TAXES,
    TAX_FISCAL_STATUS_CONFIGURED,
    TAX_FISCAL_STATUS_NO_TAXES,
    TAX_FISCAL_STATUS_PENDING,
    create_comparison,
    derive_tax_fiscal_status,
    evaluate_can_advance_to_coverage,
    get_comparison_state,
    get_comparison_tax_config,
    get_table_by_slot,
    is_saved_tax_config_complete,
    set_comparison_state,
    set_comparison_tax_config,
    validate_selected_table_ids_for_tax,
)
from app.agente_compara_doc_service import (
    AGENTE_COMPARA_TEMP_TABLE_ID_SESSION_KEY,
    ERROR_TAX_CONFIG_PENDING,
    ERROR_TAX_CONFIG_USE_GLOBAL_ENDPOINT,
    ERROR_TAX_SELECTED_TABLES_REQUIRED,
    ERROR_TEMP_TABLE_ID_MISMATCH,
    ERROR_TEMP_TABLE_SCOPE_MISMATCH,
    _build_tax_table_ufs_preview,
    _tax_destination_field_kind,
    build_tax_config_for_comparison,
    build_tax_config_for_temp_table,
    consolidate_selected_tables_tax_ufs,
    consolidate_tax_destination_ufs,
    extract_tax_destination_ufs_from_temp_table,
    load_temp_table_record,
    suggested_icms_interstate_rate,
    _validate_tax_config_for_save,
)
from app.services.agente_compara_config_service import (
    AgenteComparaConfig,
    DEFAULT_FALLBACK_MESSAGE,
)
from tests.cleiton_doc_fixtures import patch_cleiton_doc_cfg, patch_cleiton_doc_store
from tests.test_agente_compara_temp_table_save import _post_temp_table_save


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
        patch_cleiton_doc_store(tmp_path, monkeypatch)
        cfg = patch_cleiton_doc_cfg(monkeypatch)
        monkeypatch.setattr("app.agente_compara_doc_service.get_cleiton_doc_config", lambda: cfg)
        monkeypatch.setattr("app.agente_compara_api_routes.get_cleiton_doc_config", lambda: cfg)
        _patch_ac_cfg(monkeypatch)
    web = _load_web_module()
    _authorized(monkeypatch, web)
    web.app.config["TESTING"] = True
    web.app.config["SECRET_KEY"] = "test-secret"
    return web.app.test_client()


def _payload_with_destinations(ufs: list[str]) -> dict:
    return {
        "status": "needs_review",
        "freight_tables": [
            {
                "name": "Tabela",
                "columns": ["UF destino", "Valor"],
                "rows": [[uf, "10"] for uf in ufs],
            }
        ],
        "freight_routes": [],
        "accessorial_fees": [],
    }


def _write_temp_table_record(
    web_client,
    *,
    comparison_id: str,
    table_id: str,
    slot_number: int,
    carrier_name: str,
    payload: dict,
) -> dict:
    from app.agente_compara_doc_service import (
        FIELD_COMPARISON_ID,
        FIELD_SLOT_NUMBER,
        FIELD_TABLE_ID,
        TEMP_TABLE_STATUS_NEEDS_REVIEW,
        _coerce_temp_table_payload,
        _temp_table_path,
        _write_temp_table_atomic,
    )

    with web_client.application.app_context():
        with web_client.application.test_request_context():
            record = _coerce_temp_table_payload(payload, source_doc_ids=[f"doc-{slot_number}"])
    record[FIELD_COMPARISON_ID] = comparison_id
    record[FIELD_TABLE_ID] = table_id
    record[FIELD_SLOT_NUMBER] = slot_number
    record["temp_table_id"] = uuid4().hex
    record["status"] = record.get("status") or TEMP_TABLE_STATUS_NEEDS_REVIEW
    with web_client.application.app_context():
        _write_temp_table_atomic(_temp_table_path(record["temp_table_id"]), record)
    return record


def _bootstrap_taxes_comparison(
    web_client,
    carriers: list[dict],
    *,
    desired_table_count: int | None = None,
    payload_builder=None,
) -> dict:
    from app.agente_compara_comparison_state import STEP_ASK_TABLE_3

    records: dict[int, dict] = {}
    with web_client.session_transaction() as sess:
        state = create_comparison(session_obj=sess)
        comparison_id = state["comparison_id"]
        count = desired_table_count or len(carriers)
        if count >= 3 and get_table_by_slot(state, 3) is None:
            state["current_step"] = STEP_ASK_TABLE_3
            from app.agente_compara_comparison_state import add_third_table

            state = add_third_table(state, session_obj=sess)
        for spec in carriers:
            slot = int(spec["slot"])
            entry = get_table_by_slot(state, slot)
            assert entry is not None, f"slot {slot} missing"
            record = _write_temp_table_record(
                web_client,
                comparison_id=comparison_id,
                table_id=entry["table_id"],
                slot_number=slot,
                carrier_name=spec["name"],
                payload=(
                    payload_builder(spec)
                    if payload_builder is not None
                    else _payload_with_destinations(spec.get("ufs") or [])
                ),
            )
            entry["temp_table_id"] = record["temp_table_id"]
            entry["confirmed"] = True
            entry["status"] = "confirmed"
            entry["carrier_name"] = spec["name"]
            entry["doc_ids"] = [f"doc-{slot}"]
            records[slot] = record
        state["current_step"] = STEP_TAXES
        state["primary_temp_table_id"] = records[min(records.keys())]["temp_table_id"]
        state["active_table_id"] = get_table_by_slot(state, min(records.keys()))["table_id"]
        set_comparison_state(state, session_obj=sess)
        sess[AGENTE_COMPARA_TEMP_TABLE_ID_SESSION_KEY] = records[min(records.keys())]["temp_table_id"]
        table_ids = {slot: get_table_by_slot(state, slot)["table_id"] for slot in records}
    return {"comparison_id": comparison_id, "records": records, "table_ids": table_ids}


def _global_tax_payload(
    boot: dict,
    *,
    include_taxes: bool = True,
    selected_slots: list[int] | None = None,
    destination_ufs: list[str] | None = None,
    manual_added_ufs: list[str] | None = None,
    manual_removed_ufs: list[str] | None = None,
) -> dict:
    selected_table_ids = []
    if selected_slots:
        selected_table_ids = [boot["table_ids"][slot] for slot in selected_slots]
    dest_entries = [{"uf": uf, "source": "manual"} for uf in (destination_ufs or ["BA"])]
    icms = [{"destination_uf": entry["uf"], "applied_rate": 7} for entry in dest_entries]
    tax_config: dict = {
        "include_taxes": include_taxes,
        "origin_uf": "SP",
        "origin_city": "Campinas",
        "iss_rate": 5,
        "selected_table_ids": selected_table_ids,
        "destination_ufs": dest_entries if include_taxes else [],
        "icms_rates": icms if include_taxes else [],
        "manual_added_ufs": manual_added_ufs or [],
        "manual_removed_ufs": manual_removed_ufs or [],
    }
    if not include_taxes:
        tax_config = {
            "include_taxes": False,
            "origin_uf": "SP",
            "origin_city": "Campinas",
            "selected_table_ids": [],
            "destination_ufs": [],
            "icms_rates": [],
            "manual_added_ufs": [],
            "manual_removed_ufs": [],
        }
    return {"comparison_id": boot["comparison_id"], "tax_config": tax_config}


def _post_global_tax_save(web_client, payload: dict):
    return web_client.post("/api/agente-compara/comparison/taxes", json=payload)


def test_derive_tax_fiscal_status_matrix():
    assert derive_tax_fiscal_status(None) == TAX_FISCAL_STATUS_PENDING
    assert derive_tax_fiscal_status({"include_taxes": False}) == TAX_FISCAL_STATUS_NO_TAXES
    assert derive_tax_fiscal_status({"include_taxes": True, "origin_uf": "SP"}) == TAX_FISCAL_STATUS_CONFIGURED


def test_suggested_icms_rates_agente_compara():
    assert suggested_icms_interstate_rate("SP", "BA") == 7.0
    assert suggested_icms_interstate_rate("SP", "PR") == 12.0


def test_build_tax_config_intermunicipal_and_interstate():
    record = {"freight_tables": []}
    validated = _validate_tax_config_for_save(
        {
            "include_taxes": True,
            "origin_uf": "SP",
            "origin_city": "Campinas",
            "iss_rate": 5,
            "destination_ufs": [{"uf": "SP", "source": "manual"}, {"uf": "BA", "source": "manual"}],
            "icms_rates": [
                {"destination_uf": "SP", "applied_rate": 2},
                {"destination_uf": "BA", "applied_rate": None},
            ],
        }
    )
    built = build_tax_config_for_temp_table(record, validated)
    by_uf = {row["destination_uf"]: row for row in built["icms_rates"]}
    assert by_uf["SP"]["operation_type"] == "intermunicipal"
    assert by_uf["BA"]["suggested_rate"] == 7.0
    assert by_uf["BA"]["user_edited"] is True


def test_consolidate_selected_tables_only_alfa(web_client):
    boot = _bootstrap_taxes_comparison(
        web_client,
        [
            {"slot": 1, "name": "Alfa", "ufs": ["BA", "PE", "CE"]},
            {"slot": 2, "name": "Intercar", "ufs": ["AL", "SE", "PB"]},
        ],
    )
    from app.agente_compara_doc_service import get_cleiton_doc_config

    cfg = get_cleiton_doc_config()
    with web_client.session_transaction() as sess:
        state = get_comparison_state(sess)

        def _load(tid):
            return load_temp_table_record(tid, ttl_hours=cfg.upload_ttl_hours)

        alfa_id = boot["table_ids"][1]
        consolidated = consolidate_selected_tables_tax_ufs(state, [alfa_id], load_record_for_temp_table_id=_load)
        assert {entry["uf"] for entry in consolidated} == {"BA", "CE", "PE"}


def test_consolidate_selected_tables_both_carriers(web_client):
    boot = _bootstrap_taxes_comparison(
        web_client,
        [
            {"slot": 1, "name": "Alfa", "ufs": ["BA", "PE", "CE"]},
            {"slot": 2, "name": "Intercar", "ufs": ["AL", "SE", "PB"]},
        ],
    )
    from app.agente_compara_doc_service import get_cleiton_doc_config

    cfg = get_cleiton_doc_config()
    with web_client.session_transaction() as sess:
        state = get_comparison_state(sess)

        def _load(tid):
            return load_temp_table_record(tid, ttl_hours=cfg.upload_ttl_hours)

        ids = [boot["table_ids"][1], boot["table_ids"][2]]
        consolidated = consolidate_selected_tables_tax_ufs(state, ids, load_record_for_temp_table_id=_load)
        assert {entry["uf"] for entry in consolidated} == {"AL", "BA", "CE", "PB", "PE", "SE"}


def test_consolidate_overlapping_ufs_deduplicated(web_client):
    boot = _bootstrap_taxes_comparison(
        web_client,
        [
            {"slot": 1, "name": "Alfa", "ufs": ["BA", "PE"]},
            {"slot": 2, "name": "Intercar", "ufs": ["BA", "SE"]},
        ],
    )
    from app.agente_compara_doc_service import get_cleiton_doc_config

    cfg = get_cleiton_doc_config()
    with web_client.session_transaction() as sess:
        state = get_comparison_state(sess)

        def _load(tid):
            return load_temp_table_record(tid, ttl_hours=cfg.upload_ttl_hours)

        ids = [boot["table_ids"][1], boot["table_ids"][2]]
        consolidated = consolidate_selected_tables_tax_ufs(state, ids, load_record_for_temp_table_id=_load)
        ufs = [entry["uf"] for entry in consolidated]
        assert ufs.count("BA") == 1
        assert set(ufs) == {"BA", "PE", "SE"}


def test_global_save_requires_selected_table_when_include_taxes(web_client):
    boot = _bootstrap_taxes_comparison(
        web_client,
        [
            {"slot": 1, "name": "Alfa", "ufs": ["BA"]},
            {"slot": 2, "name": "Intercar", "ufs": ["AL"]},
        ],
    )
    payload = _global_tax_payload(boot, include_taxes=True, selected_slots=[])
    payload["tax_config"]["selected_table_ids"] = []
    resp = _post_global_tax_save(web_client, payload)
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == ERROR_TAX_SELECTED_TABLES_REQUIRED


def test_global_save_include_taxes_false_no_selection_required(web_client):
    boot = _bootstrap_taxes_comparison(
        web_client,
        [
            {"slot": 1, "name": "Alfa", "ufs": ["BA"]},
            {"slot": 2, "name": "Intercar", "ufs": ["AL"]},
        ],
    )
    resp = _post_global_tax_save(web_client, _global_tax_payload(boot, include_taxes=False))
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["tax_config"]["include_taxes"] is False
    assert body["can_advance_to_coverage"] is True


def test_global_save_one_carrier_enables_partial_advance(web_client):
    boot = _bootstrap_taxes_comparison(
        web_client,
        [
            {"slot": 1, "name": "Alfa", "ufs": ["BA", "PE", "CE"]},
            {"slot": 2, "name": "Intercar", "ufs": ["AL", "SE", "PB"]},
        ],
    )
    resp = _post_global_tax_save(
        web_client,
        _global_tax_payload(boot, include_taxes=True, selected_slots=[1], destination_ufs=["BA", "PE", "CE"]),
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["comparison"]["current_step"] == STEP_TAXES
    assert body["can_advance_to_coverage"] is True
    assert set(body["tax_config"]["selected_table_ids"]) == {boot["table_ids"][1]}


def test_global_save_does_not_write_temp_table_tax_config(web_client):
    boot = _bootstrap_taxes_comparison(
        web_client,
        [
            {"slot": 1, "name": "Alfa", "ufs": ["BA"]},
            {"slot": 2, "name": "Intercar", "ufs": ["AL"]},
        ],
    )
    _post_global_tax_save(web_client, _global_tax_payload(boot, include_taxes=True, selected_slots=[1, 2]))
    alfa = load_temp_table_record(boot["records"][1]["temp_table_id"], ttl_hours=24)
    beta = load_temp_table_record(boot["records"][2]["temp_table_id"], ttl_hours=24)
    assert alfa.get("tax_config") in (None, {})
    assert beta.get("tax_config") in (None, {})


def test_temp_table_tax_save_blocked_at_taxes_step(web_client):
    boot = _bootstrap_taxes_comparison(
        web_client,
        [
            {"slot": 1, "name": "Alfa", "ufs": ["BA"]},
            {"slot": 2, "name": "Intercar", "ufs": ["AL"]},
        ],
    )
    alfa = boot["records"][1]
    resp = _post_temp_table_save(
        web_client,
        {
            "temp_table_id": alfa["temp_table_id"],
            "comparison_id": alfa["comparison_id"],
            "table_id": alfa["table_id"],
            "edit_target": {"tax_config": {"include_taxes": False}},
            "review_action": "save_draft",
        },
    )
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == ERROR_TAX_CONFIG_USE_GLOBAL_ENDPOINT


def test_advance_to_coverage_requires_global_save(web_client):
    boot = _bootstrap_taxes_comparison(
        web_client,
        [
            {"slot": 1, "name": "Alfa", "ufs": ["BA"]},
            {"slot": 2, "name": "Intercar", "ufs": ["AL"]},
        ],
    )
    alfa = boot["records"][1]
    resp = _post_temp_table_save(
        web_client,
        {
            "temp_table_id": alfa["temp_table_id"],
            "comparison_id": alfa["comparison_id"],
            "review_action": "advance_to_coverage",
        },
    )
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == ERROR_TAX_CONFIG_PENDING


def test_advance_to_coverage_after_global_save(web_client, monkeypatch):
    import app.run_agente_compara_temp_table as temp_mod

    gemini_mock = MagicMock(side_effect=AssertionError("Gemini não deve ser chamado"))
    monkeypatch.setattr(temp_mod, "cleiton_governed_generate_content", gemini_mock)

    boot = _bootstrap_taxes_comparison(
        web_client,
        [
            {"slot": 1, "name": "Alfa", "ufs": ["BA", "PE", "CE"]},
            {"slot": 2, "name": "Intercar", "ufs": ["AL", "SE", "PB"]},
        ],
    )
    save_resp = _post_global_tax_save(
        web_client,
        _global_tax_payload(boot, include_taxes=True, selected_slots=[1, 2], destination_ufs=["BA", "AL"]),
    )
    assert save_resp.status_code == 200
    resp = _post_temp_table_save(
        web_client,
        {
            "temp_table_id": boot["records"][1]["temp_table_id"],
            "comparison_id": boot["comparison_id"],
            "review_action": "advance_to_coverage",
        },
    )
    assert resp.status_code == 200
    assert resp.get_json()["comparison"]["current_step"] == STEP_COVERAGE
    with web_client.session_transaction() as sess:
        state = get_comparison_state(sess)
        tax_config = get_comparison_tax_config(state)
        assert tax_config is not None
        assert tax_config.get("confirmed") is True
    assert gemini_mock.call_count == 0


def test_global_tax_config_persisted_in_comparison_state(web_client):
    boot = _bootstrap_taxes_comparison(
        web_client,
        [
            {"slot": 1, "name": "Alfa", "ufs": ["BA"]},
            {"slot": 2, "name": "Intercar", "ufs": ["AL"]},
        ],
    )
    _post_global_tax_save(web_client, _global_tax_payload(boot, include_taxes=True, selected_slots=[1]))
    with web_client.session_transaction() as sess:
        state = get_comparison_state(sess)
        tax_config = get_comparison_tax_config(state)
        assert tax_config is not None
        assert tax_config["selected_table_ids"] == [boot["table_ids"][1]]
        assert evaluate_can_advance_to_coverage(state) is True


def test_validate_selected_table_id_ownership(web_client):
    boot = _bootstrap_taxes_comparison(
        web_client,
        [
            {"slot": 1, "name": "Alfa", "ufs": ["BA"]},
            {"slot": 2, "name": "Intercar", "ufs": ["AL"]},
        ],
    )
    with web_client.session_transaction() as sess:
        state = get_comparison_state(sess)
        with pytest.raises(Exception):
            validate_selected_table_ids_for_tax(state, [uuid4().hex])


def test_agente_compara_js_global_tax_checkboxes():
    import re

    js = open("app/static/js/agente_compara.js", encoding="utf-8").read()
    assert "renderTaxCarrierCheckboxes" in js
    assert "saveGlobalTaxConfig" in js
    assert "advanceTaxesToCoverage" in js
    assert "taxSelectedTableIds" in js
    assert "canAdvanceToCoverage" in js
    assert "API_COMPARISON_TAXES" in js
    assert "renderTaxCarrierSelector" not in js
    assert not re.search(r"\btaxSelectedTableId\b", js)
    assert "selectTaxCarrier" not in js


def test_agente_compara_html_global_tax_save_button():
    html = open("app/templates/agente_compara.html", encoding="utf-8").read()
    js = open("app/static/js/agente_compara.js", encoding="utf-8").read()
    assert 'id="agenteComparaTempTableModalTaxSave"' in html
    assert "Salvar configuração de impostos" in html
    assert "Salvar impostos da transportadora" not in html
    footer_block = js[js.index("function updateTempTableModalFooter()"): js.index("function canEditFreightTables")]
    assert "taxSaveBtn.hidden = true;" in footer_block


def test_documents_status_includes_tax_table_ufs_preview_at_taxes(web_client):
    boot = _bootstrap_taxes_comparison(
        web_client,
        [
            {"slot": 1, "name": "Alfa", "ufs": ["BA"]},
            {"slot": 2, "name": "Intercar", "ufs": ["AL"]},
        ],
    )
    resp = web_client.get("/api/agente-compara/documents/status")
    body = resp.get_json()
    assert body["comparison"]["current_step"] == STEP_TAXES
    assert "tax_table_ufs_preview" in body["comparison"]
    assert len(body["comparison"]["tax_table_ufs_preview"]) == 2


def test_is_saved_tax_config_complete():
    assert is_saved_tax_config_complete({"include_taxes": False})
    assert is_saved_tax_config_complete({"include_taxes": True, "origin_uf": "SP"})
    assert not is_saved_tax_config_complete({"include_taxes": True})


def test_build_tax_config_for_comparison_manual_adjustments(web_client):
    boot = _bootstrap_taxes_comparison(
        web_client,
        [
            {"slot": 1, "name": "Alfa", "ufs": ["BA", "PE"]},
            {"slot": 2, "name": "Intercar", "ufs": ["BA", "SE"]},
        ],
    )
    with web_client.session_transaction() as sess:
        state = get_comparison_state(sess)
        validated = _validate_tax_config_for_save(
            {
                "include_taxes": True,
                "origin_uf": "SP",
                "origin_city": "Campinas",
                "icms_rates": [
                    {"destination_uf": "BA", "applied_rate": 7},
                    {"destination_uf": "RJ", "applied_rate": 12},
                ],
            }
        )
        built = build_tax_config_for_comparison(
            state,
            validated,
            selected_table_ids=[boot["table_ids"][1]],
            manual_added_ufs=["RJ"],
            manual_removed_ufs=["PE"],
        )
        ufs = {entry["uf"] for entry in built["destination_ufs"]}
        assert "RJ" in ufs
        assert "PE" not in ufs
        assert "BA" in ufs


def test_three_carriers_partial_selection_valid(web_client):
    boot = _bootstrap_taxes_comparison(
        web_client,
        [
            {"slot": 1, "name": "Alfa", "ufs": ["BA"]},
            {"slot": 2, "name": "Intercar", "ufs": ["AL"]},
            {"slot": 3, "name": "Gama", "ufs": ["CE"]},
        ],
        desired_table_count=3,
    )
    resp = _post_global_tax_save(
        web_client,
        _global_tax_payload(boot, include_taxes=True, selected_slots=[1, 2, 3], destination_ufs=["BA", "AL", "CE"]),
    )
    assert resp.status_code == 200
    assert resp.get_json()["can_advance_to_coverage"] is True


def test_e2e_global_tax_journey(web_client, monkeypatch):
    import app.run_agente_compara_temp_table as temp_mod

    gemini_mock = MagicMock(side_effect=AssertionError("Gemini não deve ser chamado"))
    monkeypatch.setattr(temp_mod, "cleiton_governed_generate_content", gemini_mock)

    boot = _bootstrap_taxes_comparison(
        web_client,
        [
            {"slot": 1, "name": "Alfa", "ufs": ["BA", "PE", "CE"]},
            {"slot": 2, "name": "Intercar", "ufs": ["AL", "SE", "PB"]},
        ],
    )
    save_resp = _post_global_tax_save(
        web_client,
        _global_tax_payload(
            boot,
            include_taxes=True,
            selected_slots=[1, 2],
            destination_ufs=["BA", "AL"],
            manual_added_ufs=["RJ"],
            manual_removed_ufs=["PE"],
        ),
    )
    assert save_resp.status_code == 200
    saved = save_resp.get_json()["tax_config"]
    assert "RJ" in saved.get("manual_added_ufs", [])
    assert set(saved["selected_table_ids"]) == {boot["table_ids"][1], boot["table_ids"][2]}

    status = web_client.get("/api/agente-compara/documents/status").get_json()
    assert status["comparison"]["tax_config"]["confirmed"] is True

    advance = _post_temp_table_save(
        web_client,
        {
            "temp_table_id": boot["records"][1]["temp_table_id"],
            "comparison_id": boot["comparison_id"],
            "review_action": "advance_to_coverage",
        },
    )
    assert advance.status_code == 200
    assert advance.get_json()["comparison"]["current_step"] == STEP_COVERAGE
    assert gemini_mock.call_count == 0


def _route_matrix_weight_columns() -> list[str]:
    return ["Região", "UF", "ATÉ 30KG", "ATÉ 50KG", "ATÉ 70KG", "ATÉ 100KG", "Excedente"]


def _sample_alfa_route_matrix_payload(*, rows_as_dicts: bool = True) -> dict:
    columns = _route_matrix_weight_columns()
    row_specs = [
        ("Capital", "DF"),
        ("Capital", "ES"),
        ("Capital", "GO"),
        ("Capital", "MG"),
        ("Capital", "MS"),
        ("Capital", "MT"),
        ("Capital", "PR"),
        ("Capital", "RS"),
        ("Capital", "SC"),
    ]
    if rows_as_dicts:
        rows = [
            {
                columns[0]: region,
                columns[1]: uf,
                columns[2]: 127.51,
                columns[3]: 149.69,
                columns[4]: 186.28,
                columns[5]: 210.67,
                columns[6]: 1.66,
            }
            for region, uf in row_specs
        ]
    else:
        rows = [
            [region, uf, 127.51, 149.69, 186.28, 210.67, 1.66]
            for region, uf in row_specs
        ]
    return {
        "status": "needs_review",
        "freight_tables": [
            {
                "table_title": "Tabela Alfa teste",
                "table_type": "route_matrix",
                "context": {"supplier": "Alfa"},
                "columns": columns,
                "rows": rows,
            }
        ],
        "freight_routes": [],
        "accessorial_fees": [],
    }


def _sample_route_matrix_payload_with_ufs(
    *,
    ufs: list[str],
    uf_column: str = "UF",
    table_type: str = "route_matrix",
    rows_as_dicts: bool = True,
    extra_columns: list[str] | None = None,
) -> dict:
    columns = list(extra_columns or ["Região", uf_column, "ATÉ 30KG", "Excedente"])
    uf_index = columns.index(uf_column)
    if rows_as_dicts:
        rows = []
        for uf in ufs:
            row = {col: None for col in columns}
            row[columns[0]] = "Capital"
            row[uf_column] = uf
            row[columns[2]] = 10.0
            row[columns[3]] = 1.0
            rows.append(row)
    else:
        rows = []
        for uf in ufs:
            row = [None] * len(columns)
            row[0] = "Capital"
            row[uf_index] = uf
            row[2] = 10.0
            row[3] = 1.0
            rows.append(row)
    return {
        "status": "needs_review",
        "freight_tables": [
            {
                "table_type": table_type,
                "columns": columns,
                "rows": rows,
            }
        ],
        "freight_routes": [],
        "accessorial_fees": [],
    }


def _sample_table_without_identifiable_uf() -> dict:
    return {
        "status": "needs_review",
        "freight_tables": [
            {
                "table_type": "accessorial",
                "columns": ["Região", "Valor", "Observação"],
                "rows": [{"Região": "Capital", "Valor": "10", "Observação": "Taxa fixa"}],
            }
        ],
        "freight_routes": [],
        "accessorial_fees": [],
    }


def test_tax_destination_field_kind_uf_outside_route_matrix_context():
    assert _tax_destination_field_kind("UF") is None
    assert _tax_destination_field_kind("uf") is None
    record = {
        "extracted_items": [{"UF": "SP", "Valor": "10"}],
        "freight_tables": [
            {
                "table_type": "generic",
                "columns": ["UF", "Valor"],
                "rows": [{"UF": "SP", "Valor": "10"}],
            }
        ],
    }
    assert extract_tax_destination_ufs_from_temp_table(record) == []


def test_tax_destination_field_kind_route_matrix_aliases():
    assert _tax_destination_field_kind("uf_destino") == "uf"
    assert _tax_destination_field_kind("destination_uf") == "uf"
    assert _tax_destination_field_kind("UF destino") == "uf"
    assert _tax_destination_field_kind("Região") is None
    assert _tax_destination_field_kind("Observação inválida") is None


def test_route_matrix_literal_uf_extracts_destinations_object_rows():
    record = _sample_route_matrix_payload_with_ufs(ufs=["DF", "ES", "GO", "MG"], rows_as_dicts=True)
    assert extract_tax_destination_ufs_from_temp_table(record) == ["DF", "ES", "GO", "MG"]
    entries = consolidate_tax_destination_ufs(record)
    assert len(entries) == 4
    assert all(entry["source"] == "automatic" for entry in entries)
    assert all(entry["evidence"] for entry in entries)


def test_route_matrix_lowercase_uf_column_extracts_destinations():
    record = _sample_route_matrix_payload_with_ufs(
        ufs=["DF", "ES"],
        uf_column="uf",
        rows_as_dicts=True,
    )
    assert extract_tax_destination_ufs_from_temp_table(record) == ["DF", "ES"]


def test_route_matrix_positional_rows_use_columns_order_not_fixed_index():
    columns = ["ATÉ 30KG", "Excedente", "Região", "UF"]
    rows = [
        [10.0, 1.0, "Capital", "DF"],
        [11.0, 1.1, "Capital", "ES"],
    ]
    record = {
        "status": "needs_review",
        "freight_tables": [
            {
                "table_type": "route_matrix",
                "columns": columns,
                "rows": rows,
            }
        ],
    }
    assert extract_tax_destination_ufs_from_temp_table(record) == ["DF", "ES"]


def test_route_matrix_positional_rows_reject_non_uf_values():
    columns = ["Região", "UF", "ATÉ 30KG", "Excedente"]
    rows = [
        ["Capital", "DF", 127.51, 1.66],
        ["Capital", "Capital", 155.23, 1.44],
        ["Capital", "999", 160.0, 1.5],
    ]
    record = {
        "status": "needs_review",
        "freight_tables": [
            {
                "table_type": "route_matrix",
                "columns": columns,
                "rows": rows,
            }
        ],
    }
    assert extract_tax_destination_ufs_from_temp_table(record) == ["DF"]


def test_alfa_route_matrix_case_extracts_all_destination_ufs():
    record = _sample_alfa_route_matrix_payload(rows_as_dicts=True)
    assert extract_tax_destination_ufs_from_temp_table(record) == [
        "DF",
        "ES",
        "GO",
        "MG",
        "MS",
        "MT",
        "PR",
        "RS",
        "SC",
    ]


def test_alfa_route_matrix_positional_rows_extracts_all_destination_ufs():
    record = _sample_alfa_route_matrix_payload(rows_as_dicts=False)
    assert extract_tax_destination_ufs_from_temp_table(record) == [
        "DF",
        "ES",
        "GO",
        "MG",
        "MS",
        "MT",
        "PR",
        "RS",
        "SC",
    ]


def test_table_without_identifiable_uf_returns_empty_preview_fields():
    record = _sample_table_without_identifiable_uf()
    assert extract_tax_destination_ufs_from_temp_table(record) == []
    entries = consolidate_tax_destination_ufs(record)
    assert entries == []


def test_build_tax_table_ufs_preview_route_matrix_alfa(web_client):
    boot = _bootstrap_taxes_comparison(
        web_client,
        [{"slot": 1, "name": "Alfa"}],
        payload_builder=lambda _spec: _sample_alfa_route_matrix_payload(rows_as_dicts=True),
    )
    with web_client.session_transaction() as sess:
        state = get_comparison_state(sess)
        preview = _build_tax_table_ufs_preview(state)
    alfa_preview = next(item for item in preview if item["table_id"] == boot["table_ids"][1])
    assert alfa_preview["carrier_name"] == "Alfa"
    assert alfa_preview["uf_count"] > 0
    assert set(alfa_preview["destination_ufs"]) == {
        "DF",
        "ES",
        "GO",
        "MG",
        "MS",
        "MT",
        "PR",
        "RS",
        "SC",
    }


def test_consolidate_route_matrix_alfa_intercargo_ufs(web_client):
    alfa_payload = _sample_route_matrix_payload_with_ufs(ufs=["DF", "ES", "GO", "MG"])
    intercargo_payload = _sample_route_matrix_payload_with_ufs(ufs=["SP", "MG", "PR"])
    boot = _bootstrap_taxes_comparison(
        web_client,
        [
            {"slot": 1, "name": "Alfa", "ufs": []},
            {"slot": 2, "name": "Intercargo", "ufs": []},
        ],
    )
    alfa_record = _write_temp_table_record(
        web_client,
        comparison_id=boot["comparison_id"],
        table_id=boot["table_ids"][1],
        slot_number=1,
        carrier_name="Alfa",
        payload=alfa_payload,
    )
    intercargo_record = _write_temp_table_record(
        web_client,
        comparison_id=boot["comparison_id"],
        table_id=boot["table_ids"][2],
        slot_number=2,
        carrier_name="Intercargo",
        payload=intercargo_payload,
    )
    from app.agente_compara_doc_service import get_cleiton_doc_config

    cfg = get_cleiton_doc_config()
    with web_client.session_transaction() as sess:
        state = get_comparison_state(sess)
        get_table_by_slot(state, 1)["temp_table_id"] = alfa_record["temp_table_id"]
        get_table_by_slot(state, 2)["temp_table_id"] = intercargo_record["temp_table_id"]

        def _load(tid):
            return load_temp_table_record(tid, ttl_hours=cfg.upload_ttl_hours)

        alfa_only = consolidate_selected_tables_tax_ufs(
            state, [boot["table_ids"][1]], load_record_for_temp_table_id=_load
        )
        intercargo_only = consolidate_selected_tables_tax_ufs(
            state, [boot["table_ids"][2]], load_record_for_temp_table_id=_load
        )
        both = consolidate_selected_tables_tax_ufs(
            state,
            [boot["table_ids"][1], boot["table_ids"][2]],
            load_record_for_temp_table_id=_load,
        )
    assert {entry["uf"] for entry in alfa_only} == {"DF", "ES", "GO", "MG"}
    assert {entry["uf"] for entry in intercargo_only} == {"MG", "PR", "SP"}
    assert {entry["uf"] for entry in both} == {"DF", "ES", "GO", "MG", "PR", "SP"}
    assert [entry["uf"] for entry in both].count("MG") == 1


def test_route_matrix_journey_preview_and_tax_config(web_client, monkeypatch):
    import app.run_agente_compara_temp_table as temp_mod

    gemini_mock = MagicMock(side_effect=AssertionError("Gemini não deve ser chamado"))
    monkeypatch.setattr(temp_mod, "cleiton_governed_generate_content", gemini_mock)

    boot = _bootstrap_taxes_comparison(
        web_client,
        [
            {"slot": 1, "name": "Alfa"},
            {"slot": 2, "name": "Intercargo"},
        ],
        payload_builder=lambda spec: (
            _sample_alfa_route_matrix_payload(rows_as_dicts=True)
            if spec["slot"] == 1
            else _sample_route_matrix_payload_with_ufs(ufs=["SP", "MG", "PR"])
        ),
    )
    alfa_record = boot["records"][1]

    with web_client.session_transaction() as sess:
        state = get_comparison_state(sess)
        preview = _build_tax_table_ufs_preview(state)
        alfa_preview = next(item for item in preview if item["table_id"] == boot["table_ids"][1])
        assert alfa_preview["uf_count"] > 0
        assert alfa_preview["destination_ufs"]

    save_resp = _post_global_tax_save(
        web_client,
        {
            "comparison_id": boot["comparison_id"],
            "tax_config": {
                "include_taxes": True,
                "origin_uf": "SP",
                "origin_city": "Campinas",
                "selected_table_ids": [boot["table_ids"][1]],
                "icms_rates": [],
                "manual_added_ufs": [],
                "manual_removed_ufs": [],
            },
        },
    )
    assert save_resp.status_code == 200
    saved = save_resp.get_json()["tax_config"]
    saved_ufs = {entry["uf"] for entry in saved["destination_ufs"]}
    assert saved_ufs == {"DF", "ES", "GO", "MG", "MS", "MT", "PR", "RS", "SC"}
    assert len(saved["icms_rates"]) == len(saved_ufs)
    assert gemini_mock.call_count == 0
    assert load_temp_table_record(alfa_record["temp_table_id"], ttl_hours=24) is not None


def _read_agente_compara_js() -> str:
    return open("app/static/js/agente_compara.js", encoding="utf-8").read()


def _tax_continue_block(js: str) -> str:
    return js[js.index("function saveTaxesAndAdvanceToCoverage"): js.index("function renderCoverageUploadHint")]


def _tax_footer_block(js: str) -> str:
    return js[js.index("function updateTempTableModalFooter()"): js.index("function canEditFreightTables")]


def _tax_payload_block(js: str) -> str:
    return js[js.index("function collectGlobalTaxConfigPayload"): js.index("function handleGlobalTaxConfigSaveResponse")]


def test_tax_cta_enabled_with_dirty_draft_without_manual_save():
    js = _read_agente_compara_js()
    footer = _tax_footer_block(js)
    assert "saveBtn.disabled = saveBtn.disabled || !comparisonState.canAdvanceToCoverage || taxConfigDirty" not in footer
    assert "taxContinueInFlight" in footer


def test_tax_cta_calls_save_then_advance_when_dirty():
    js = _read_agente_compara_js()
    block = _tax_continue_block(js)
    assert "function saveTaxesAndAdvanceToCoverage" in js
    assert "saveGlobalTaxConfig({ partOfContinueFlow: true })" in block
    assert "advanceTaxesToCoverage({ partOfContinueFlow: true, skipPreconditions: true })" in block
    assert "var needsSave = taxConfigDirty || !comparisonState.canAdvanceToCoverage;" in block


def test_tax_cta_skips_save_when_already_confirmed():
    js = _read_agente_compara_js()
    block = _tax_continue_block(js)
    assert ": Promise.resolve();" in block
    assert "skipPreconditions: true" in block


def test_tax_cta_loading_text_and_restore():
    js = _read_agente_compara_js()
    footer = _tax_footer_block(js)
    assert "Salvando e continuando..." in footer
    assert "Continuar para cidades" in footer


def test_tax_cta_single_listener():
    js = _read_agente_compara_js()
    init_block = js[js.index("function initTempTableModal"): js.index("function initDocuments")]
    assert "saveTaxConfigAndContinue()" in init_block or "handleTempTableModalSaveClick" in init_block
    assert js.count("addEventListener('click', handleTempTableModalSaveClick)") == 1
    assert "saveTaxesAndAdvanceToCoverage();" in js


def test_tax_cta_double_click_guard():
    js = _read_agente_compara_js()
    block = _tax_continue_block(js)
    assert "if (taxContinueInFlight || taxSaveInFlight || tempTableSaveInFlight) return;" in block
    click_block = js[js.index("function handleTempTableModalSaveClick"): js.index("function initTempTableModal")]
    assert "taxContinueInFlight" in click_block


def test_tax_cta_orientation_message():
    js = _read_agente_compara_js()
    carrier_block = js[js.index("function renderTaxCarrierCheckboxes"): js.index("function rebuildTaxIcmsRates")]
    assert "A configuração será salva ao continuar para cidades." in carrier_block
    assert "Salve a configuração de impostos do cenário antes de continuar." not in carrier_block


def test_tax_local_validation_messages():
    js = _read_agente_compara_js()
    payload = _tax_payload_block(js)
    assert "Selecione ao menos uma transportadora." in payload
    assert "UF origem é obrigatória para incluir impostos." in payload
    assert "Informe a alíquota aplicada para cada UF marcada para uso no cálculo." in payload
    assert "if (!activeRow || !activeRow.is_active) continue;" in payload


def test_tax_save_global_returns_structured_result():
    js = _read_agente_compara_js()
    save_block = js[js.index("function saveGlobalTaxConfig"): js.index("function advanceTaxesToCoverage")]
    assert "return Promise.resolve({ ok: false" in save_block
    assert "can_advance_to_coverage:" in save_block
    assert "partOfContinueFlow" in save_block


def test_tax_advance_returns_promise():
    js = _read_agente_compara_js()
    advance_block = js[js.index("function advanceTaxesToCoverage"): js.index("function saveTaxesAndAdvanceToCoverage")]
    assert "return Promise.resolve({ ok: false" in advance_block
    assert "return { ok: true" in advance_block


def test_tax_encadeamento_backend_dirty_to_coverage(web_client, monkeypatch):
    import app.run_agente_compara_temp_table as temp_mod

    gemini_mock = MagicMock(side_effect=AssertionError("Gemini não deve ser chamado"))
    monkeypatch.setattr(temp_mod, "cleiton_governed_generate_content", gemini_mock)

    boot = _bootstrap_taxes_comparison(
        web_client,
        [
            {"slot": 1, "name": "Alfa", "ufs": ["BA"]},
            {"slot": 2, "name": "Intercar", "ufs": ["AL"]},
        ],
    )
    save_resp = _post_global_tax_save(
        web_client,
        _global_tax_payload(boot, include_taxes=True, selected_slots=[1], destination_ufs=["BA"]),
    )
    assert save_resp.status_code == 200
    body = save_resp.get_json()
    assert body["tax_config"]["confirmed"] is True
    assert body["can_advance_to_coverage"] is True

    advance = _post_temp_table_save(
        web_client,
        {
            "temp_table_id": boot["records"][1]["temp_table_id"],
            "comparison_id": boot["comparison_id"],
            "review_action": "advance_to_coverage",
        },
    )
    assert advance.status_code == 200
    assert advance.get_json()["comparison"]["current_step"] == STEP_COVERAGE
    assert gemini_mock.call_count == 0


def test_tax_encadeamento_include_taxes_false(web_client, monkeypatch):
    import app.run_agente_compara_temp_table as temp_mod

    gemini_mock = MagicMock(side_effect=AssertionError("Gemini não deve ser chamado"))
    monkeypatch.setattr(temp_mod, "cleiton_governed_generate_content", gemini_mock)

    boot = _bootstrap_taxes_comparison(
        web_client,
        [
            {"slot": 1, "name": "Alfa", "ufs": ["BA"]},
            {"slot": 2, "name": "Intercar", "ufs": ["AL"]},
        ],
    )
    save_resp = _post_global_tax_save(
        web_client,
        _global_tax_payload(boot, include_taxes=False),
    )
    assert save_resp.status_code == 200
    body = save_resp.get_json()
    assert body["tax_config"]["include_taxes"] is False
    assert body["tax_config"]["confirmed"] is True
    assert body["can_advance_to_coverage"] is True

    advance = _post_temp_table_save(
        web_client,
        {
            "temp_table_id": boot["records"][1]["temp_table_id"],
            "comparison_id": boot["comparison_id"],
            "review_action": "advance_to_coverage",
        },
    )
    assert advance.status_code == 200
    assert advance.get_json()["comparison"]["current_step"] == STEP_COVERAGE
    assert gemini_mock.call_count == 0


def test_tax_encadeamento_intermunicipal_inactive_valid(web_client):
    boot = _bootstrap_taxes_comparison(
        web_client,
        [{"slot": 1, "name": "Alfa", "ufs": ["SP"]}],
    )
    resp = _post_global_tax_save(
        web_client,
        {
            "comparison_id": boot["comparison_id"],
            "tax_config": {
                "include_taxes": True,
                "origin_uf": "SP",
                "origin_city": "Campinas",
                "selected_table_ids": [boot["table_ids"][1]],
                "destination_ufs": [{"uf": "SP", "source": "automatic", "label": "", "manual": False}],
                "icms_rates": [
                    {
                        "destination_uf": "SP",
                        "operation_type": "intermunicipal",
                        "suggested_rate": None,
                        "applied_rate": None,
                        "is_active": False,
                    }
                ],
            },
        },
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["tax_config"]["confirmed"] is True
    assert body["can_advance_to_coverage"] is True


def test_tax_save_failure_does_not_advance(web_client):
    boot = _bootstrap_taxes_comparison(
        web_client,
        [
            {"slot": 1, "name": "Alfa", "ufs": ["BA"]},
            {"slot": 2, "name": "Intercar", "ufs": ["AL"]},
        ],
    )
    bad_save = _post_global_tax_save(
        web_client,
        {
            "comparison_id": boot["comparison_id"],
            "tax_config": {
                "include_taxes": True,
                "origin_uf": "SP",
                "selected_table_ids": [],
            },
        },
    )
    assert bad_save.status_code == 400
    with web_client.session_transaction() as sess:
        state = get_comparison_state(sess)
        assert state["current_step"] == STEP_TAXES
        assert evaluate_can_advance_to_coverage(state) is False

    blocked = _post_temp_table_save(
        web_client,
        {
            "temp_table_id": boot["records"][1]["temp_table_id"],
            "comparison_id": boot["comparison_id"],
            "review_action": "advance_to_coverage",
        },
    )
    assert blocked.status_code in {400, 409}
    with web_client.session_transaction() as sess:
        assert get_comparison_state(sess)["current_step"] == STEP_TAXES


def test_tax_advance_failure_preserves_saved_config(web_client, monkeypatch):
    from app.agente_compara_comparison_state import AgenteComparaComparisonError

    def _boom_advance(_state):
        raise AgenteComparaComparisonError("comparison_step_invalid", "falha simulada no avanço")

    monkeypatch.setattr("app.agente_compara_doc_service.advance_to_coverage", _boom_advance)

    boot = _bootstrap_taxes_comparison(
        web_client,
        [
            {"slot": 1, "name": "Alfa", "ufs": ["BA"]},
            {"slot": 2, "name": "Intercar", "ufs": ["AL"]},
        ],
    )
    save_resp = _post_global_tax_save(
        web_client,
        _global_tax_payload(boot, include_taxes=True, selected_slots=[1], destination_ufs=["BA"]),
    )
    assert save_resp.status_code == 200
    saved_ids = save_resp.get_json()["tax_config"]["selected_table_ids"]

    blocked = _post_temp_table_save(
        web_client,
        {
            "temp_table_id": boot["records"][1]["temp_table_id"],
            "comparison_id": boot["comparison_id"],
            "review_action": "advance_to_coverage",
        },
    )
    assert blocked.status_code in {400, 409}
    with web_client.session_transaction() as sess:
        state = get_comparison_state(sess)
        assert state["current_step"] == STEP_TAXES
        assert get_comparison_tax_config(state)["selected_table_ids"] == saved_ids
        assert evaluate_can_advance_to_coverage(state) is True
