"""Testes de save/avanco da tabela temporaria do AgenteCompara (multitabela)."""
from __future__ import annotations

import importlib
import json
import os
from types import SimpleNamespace

import pytest

from app.agente_compara_comparison_state import (
    AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY,
    ERROR_COMPARISON_STEP_INVALID,
    STEP_PREPARE_TABLE_1,
    STEP_PREPARE_TABLE_2,
    TABLE_STATUS_CONFIRMED,
    create_comparison,
    get_comparison_state,
)
from app.agente_compara_doc_service import (
    AGENTE_COMPARA_DOC_IDS_SESSION_KEY,
    AGENTE_COMPARA_TEMP_TABLE_ID_SESSION_KEY,
    AGENTE_COMPARA_TEMP_TABLE_SOURCE_DOCS_SESSION_KEY,
    ERROR_TEMP_TABLE_ID_MISMATCH,
    ERROR_TEMP_TABLE_INVALID_ACCESSORIAL_FEES,
    ERROR_TEMP_TABLE_NOT_FOUND,
    TEMP_TABLE_SAVE_IDEMPOTENCY_CACHE_SESSION_KEY,
    agente_compara_temp_table_save_idempotency_key,
    extract_tax_destination_ufs_from_temp_table,
    load_temp_table_record,
)
from app.services.agente_compara_config_service import (
    AgenteComparaConfig,
    DEFAULT_FALLBACK_MESSAGE,
)
from tests.cleiton_doc_fixtures import patch_cleiton_doc_cfg, patch_cleiton_doc_store
from tests.test_cleide_audit_temp_table import (
    _manual_accessorial_fee,
    _sample_hengst_freight_tables_payload,
)


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


def _bootstrap_temp_table(web_client, payload: dict) -> dict:
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

    with web_client.session_transaction() as sess:
        state = create_comparison(session_obj=sess)
        table_1 = next(t for t in state["tables"].values() if t["slot_number"] == 1)
        comparison_id = state["comparison_id"]
        table_id = table_1["table_id"]
        table_1["doc_ids"] = ["doc-1"]
        table_1["status"] = "processing"
        sess[AGENTE_COMPARA_DOC_IDS_SESSION_KEY] = ["doc-1"]
        sess[AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = state

    with web_client.application.app_context():
        with web_client.application.test_request_context():
            record = _coerce_temp_table_payload(payload, source_doc_ids=["doc-1"])
    record[FIELD_COMPARISON_ID] = comparison_id
    record[FIELD_TABLE_ID] = table_id
    record[FIELD_SLOT_NUMBER] = 1
    record["temp_table_id"] = uuid4().hex
    record["status"] = record.get("status") or TEMP_TABLE_STATUS_NEEDS_REVIEW
    with web_client.application.app_context():
        _write_temp_table_atomic(_temp_table_path(record["temp_table_id"]), record)

    with web_client.session_transaction() as sess:
        state = get_comparison_state(sess)
        table_1 = next(t for t in state["tables"].values() if t["slot_number"] == 1)
        table_1["temp_table_id"] = record["temp_table_id"]
        table_1["status"] = "needs_review"
        sess[AGENTE_COMPARA_TEMP_TABLE_ID_SESSION_KEY] = record["temp_table_id"]
        sess[AGENTE_COMPARA_TEMP_TABLE_SOURCE_DOCS_SESSION_KEY] = ["doc-1"]
        sess[AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = state

    return record


def _apply_payload(web_client, payload: dict) -> dict:
    return _bootstrap_temp_table(web_client, payload)


def _save_payload_for_record(record: dict, **overrides) -> dict:
    payload = {
        "temp_table_id": record["temp_table_id"],
        "comparison_id": record.get("comparison_id"),
        "table_id": record.get("table_id"),
        "slot": record.get("slot_number") or 1,
        "edit_version": record.get("edit_version") if record.get("edit_version") is not None else 0,
        "edit_target": {
            "freight_tables": record.get("freight_tables") or [],
            "freight_routes": record.get("freight_routes") or [],
            "accessorial_fees": record.get("accessorial_fees") or [],
        },
        "review_action": "save_and_advance",
    }
    payload.update(overrides)
    return payload


def _post_temp_table_save(web_client, payload: dict, *, execution_id: str | None = None):
    headers = {"Content-Type": "application/json"}
    if execution_id:
        headers["X-Execution-ID"] = execution_id
        payload = dict(payload)
        payload["execution_id"] = execution_id
    return web_client.post(
        "/api/agente-compara/temp-table/save",
        json=payload,
        headers=headers,
    )


def test_temp_table_save_without_active_temp_table(web_client):
    resp = _post_temp_table_save(
        web_client,
        {
            "temp_table_id": "missing",
            "edit_target": {"freight_tables": [], "freight_routes": [], "accessorial_fees": []},
            "review_action": "save_and_advance",
        },
    )
    assert resp.status_code == 404
    assert resp.get_json()["error_code"] == ERROR_TEMP_TABLE_NOT_FOUND


def test_temp_table_save_id_mismatch(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    resp = _post_temp_table_save(
        web_client,
        {
            "temp_table_id": "wrong-id",
            "edit_target": {"freight_tables": [], "freight_routes": [], "accessorial_fees": []},
            "review_action": "save_and_advance",
        },
    )
    assert resp.status_code == 409
    assert resp.get_json()["error_code"] == ERROR_TEMP_TABLE_ID_MISMATCH
    assert saved["temp_table_id"] != "wrong-id"


def test_temp_table_save_valid_without_changes_advances_to_prepare_table_2(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload(accessorial_fees=[]))
    resp = _post_temp_table_save(web_client, _save_payload_for_record(saved))
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["comparison"]["current_step"] == STEP_PREPARE_TABLE_2
    with web_client.session_transaction() as sess:
        state = sess.get(AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY)
    table_1 = next(t for t in state["tables"].values() if t["slot_number"] == 1)
    assert table_1["confirmed"] is True


def _sample_route_matrix_payload_with_positional_rows() -> dict:
    return {
        "status": "needs_review",
        "freight_tables": [
            {
                "name": "Tabela Alfa",
                "type": "route_matrix",
                "supplier": "Alfa",
                "headers": ["Regi?o", "UF", "AT? 30KG", "AT? 50KG", "AT? 70KG", "AT? 100KG", "Excedente"],
                "rows": [
                    ["Capital", "DF", "127.51", "149.69", "186.28", "210.67", "1.66"],
                    ["Capital", "ES", "155.23", "186.28", "208.45", "227.3", "1.44"],
                ],
                "evidence_ref": "Tabela Alfa.xlsx",
            }
        ],
        "freight_routes": [],
        "freight_values": [],
        "accessorial_fees": [],
        "weight_ranges": [],
        "reading_alerts": [],
        "evidence_refs": [],
    }


def test_temp_table_save_pt_br_values_do_not_block(web_client):
    payload = _sample_hengst_freight_tables_payload(
        accessorial_fees=[
            _manual_accessorial_fee(name="Pedagio", value="R$ 89,25", unit="R$"),
            _manual_accessorial_fee(name="GRIS", value="0,35%", unit="%"),
        ]
    )
    saved = _apply_payload(web_client, payload)
    edited = _save_payload_for_record(saved)
    edited["edit_target"]["accessorial_fees"][0].update(
        {
            "calculation_base_id": "por_cte",
            "calculation_basis": "por CTe",
            "classification_source": "manual_configured_calculation_base",
            "operation": "fixed_amount",
            "calculation_type": "fixed_amount",
        }
    )
    edited["edit_target"]["accessorial_fees"][1].update(
        {
            "calculation_base_id": "pct_nota_fiscal",
            "calculation_basis": "% por nota fiscal",
            "classification_source": "manual_configured_calculation_base",
            "operation": "percentage_of_variable",
            "audit_variable": "valor_nf",
        }
    )
    resp = _post_temp_table_save(web_client, edited)
    assert resp.status_code == 200
    assert resp.get_json()["comparison"]["current_step"] == STEP_PREPARE_TABLE_2


def test_temp_table_save_por_cte_missing_value_returns_detailed_error(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[_manual_accessorial_fee(value="", unit="R$")]
        ),
    )
    edited = _save_payload_for_record(saved)
    edited["edit_target"]["accessorial_fees"][0].update(
        {
            "calculation_base_id": "por_cte",
            "calculation_basis": "por CTe",
            "classification_source": "manual_configured_calculation_base",
            "operation": "fixed_amount",
            "calculation_type": "fixed_amount",
        }
    )
    resp = _post_temp_table_save(web_client, edited)
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error_code"] == ERROR_TEMP_TABLE_INVALID_ACCESSORIAL_FEES
    assert body["message"] == "Revise as generalidades antes de avançar."
    assert body["errors"][0]["field"] == "value"
    assert body["errors"][0]["reason_code"] == "invalid_accessorial_value"
    with web_client.session_transaction() as sess:
        state = sess.get(AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY)
    assert state["current_step"] == STEP_PREPARE_TABLE_1
    table_1 = next(t for t in state["tables"].values() if t["slot_number"] == 1)
    assert table_1["confirmed"] is not True


def test_temp_table_save_route_returns_400_with_errors(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[_manual_accessorial_fee(value="", unit="R$")]
        ),
    )
    edited = _save_payload_for_record(saved)
    edited["edit_target"]["accessorial_fees"][0].update(
        {
            "calculation_base_id": "por_cte",
            "calculation_basis": "por CTe",
            "classification_source": "manual_configured_calculation_base",
            "operation": "fixed_amount",
            "calculation_type": "fixed_amount",
        }
    )
    resp = _post_temp_table_save(web_client, edited)
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error_code"] == ERROR_TEMP_TABLE_INVALID_ACCESSORIAL_FEES
    assert body["message"] == "Revise as generalidades antes de avançar."
    assert body["errors"][0]["field"] == "value"
    assert body["errors"][0]["reason_code"] == "invalid_accessorial_value"
    assert body["message"] != "Não foi possível salvar a revisão da tabela temporária."


def test_temp_table_save_incompatible_unit_identifies_field(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[_manual_accessorial_fee(value="10,24", unit="%")]
        ),
    )
    edited = _save_payload_for_record(saved)
    edited["edit_target"]["accessorial_fees"][0].update(
        {
            "calculation_base_id": "por_cte",
            "calculation_basis": "por CTe",
            "classification_source": "manual_configured_calculation_base",
            "operation": "fixed_amount",
            "calculation_type": "fixed_amount",
        }
    )
    resp = _post_temp_table_save(web_client, edited)
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["errors"][0]["field"] == "unit"
    assert body["errors"][0]["reason_code"] == "incompatible_accessorial_unit"


def test_temp_table_save_missing_calculation_base_identifies_field(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload(accessorial_fees=[]))
    edited = _save_payload_for_record(saved)
    edited["edit_target"]["accessorial_fees"] = [
        {
            **_manual_accessorial_fee(value="0,20", unit="R$"),
            "classification_source": "manual_configured_calculation_base",
            "operation": "fixed_amount",
            "calculation_type": "fixed_amount",
        }
    ]
    resp = _post_temp_table_save(web_client, edited)
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["errors"][0]["field"] == "calculation_base_id"
    assert body["errors"][0]["reason_code"] == "missing_calculation_base"


def test_temp_table_save_removed_optional_accessorial_does_not_block(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[
                _manual_accessorial_fee(value="", unit="R$"),
                _manual_accessorial_fee(name="Valida", value="10,00", unit="R$"),
            ]
        ),
    )
    edited = _save_payload_for_record(saved)
    valid_fee = edited["edit_target"]["accessorial_fees"][1]
    valid_fee.update(
        {
            "calculation_base_id": "por_cte",
            "calculation_basis": "por CTe",
            "classification_source": "manual_configured_calculation_base",
            "operation": "fixed_amount",
            "calculation_type": "fixed_amount",
        }
    )
    edited["edit_target"]["accessorial_fees"] = [valid_fee]
    resp = _post_temp_table_save(web_client, edited)
    assert resp.status_code == 200
    assert resp.get_json()["comparison"]["current_step"] == STEP_PREPARE_TABLE_2


def test_temp_table_save_extraction_hypothesis_does_not_block_advance(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[
                {
                    "name": "Pedagio geral",
                    "value": "",
                    "unit": "",
                    "calculation_basis": "não mapeado / revisar",
                    "calculation_base_id": None,
                    "classification_source": "legacy_classifier",
                    "status": "needs_review",
                    "notes": "",
                }
            ]
        ),
    )
    resp = _post_temp_table_save(web_client, _save_payload_for_record(saved))
    assert resp.status_code == 200
    assert resp.get_json()["comparison"]["current_step"] == STEP_PREPARE_TABLE_2


def test_temp_table_save_failure_does_not_confirm_table(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[_manual_accessorial_fee(value="", unit="R$")]
        ),
    )
    edited = _save_payload_for_record(saved)
    edited["edit_target"]["accessorial_fees"][0].update(
        {
            "calculation_base_id": "por_cte",
            "calculation_basis": "por CTe",
            "classification_source": "manual_configured_calculation_base",
            "operation": "fixed_amount",
        }
    )
    before = load_temp_table_record(saved["temp_table_id"], ttl_hours=24)
    resp = _post_temp_table_save(web_client, edited)
    assert resp.status_code == 400
    after = load_temp_table_record(saved["temp_table_id"], ttl_hours=24)
    assert after == before
    with web_client.session_transaction() as sess:
        state = sess.get(AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY)
    table_1 = next(t for t in state["tables"].values() if t["slot_number"] == 1)
    assert table_1["confirmed"] is not True
    assert state["current_step"] == STEP_PREPARE_TABLE_1


def test_temp_table_save_invalid_payload_returns_400_not_500(web_client):
    resp = _post_temp_table_save(web_client, {"temp_table_id": "", "edit_target": {}})
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "agente_compara_temp_table_invalid_payload"
    assert resp.get_json()["message"] != "Não foi possível salvar a revisão da tabela temporária."


def test_agente_compara_js_save_error_helpers():
    js = open("app/static/js/agente_compara.js", encoding="utf-8").read()
    assert "function resolveTempTableSaveErrorMessage" in js
    assert "function accessorialValidationSummaryMessage" in js
    assert "function accessorialFeeShouldBlockAdvance" in js
    assert "function accessorialFeeIsExtractionHypothesis" in js
    save_block = js[js.index("function saveTempTableAndAdvance"): js.index("function byId(id)")]
    assert "handleBackendTempTableValidationErrors(res.data)" in save_block
    assert "resolveTempTableSaveErrorMessage(res.data)" in save_block
    assert "res.status === 500" in save_block
    assert "setTempTableModalError(res.data ||" not in save_block


def test_agente_compara_js_polling_respects_edit_mode():
    js = open("app/static/js/agente_compara.js", encoding="utf-8").read()
    poll_block = js[js.index("function startTempTablePollingIfNeeded"): js.index("function handleTempTableFromStatus")]
    assert "tempTableEditMode || tempTableSaveInFlight" in poll_block
    status_block = js[js.index("function handleTempTableFromStatus"): js.index("function formatBytes")]
    assert "tempTableEditMode" in status_block
    assert "tempTableSaveInFlight" in status_block


def test_temp_table_save_idempotency_key_format():
    key = agente_compara_temp_table_save_idempotency_key(
        comparison_id="cmp-1",
        table_id="tbl-1",
        temp_table_id="tt-1",
        edit_version=0,
        review_action="save_and_advance",
        execution_id="exec-1",
    )
    assert key == "agente-compara-temp-table-save:cmp-1:tbl-1:tt-1:0:save_and_advance:exec-1"


def test_temp_table_save_first_advance_slot1_confirmed_consistent(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload(accessorial_fees=[]))
    payload = _save_payload_for_record(saved, execution_id="exec-first")
    resp = _post_temp_table_save(web_client, payload, execution_id="exec-first")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["comparison"]["current_step"] == STEP_PREPARE_TABLE_2
    assert body.get("idempotent_replay") is False
    with web_client.session_transaction() as sess:
        state = sess.get(AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY)
    table_1 = next(t for t in state["tables"].values() if t["slot_number"] == 1)
    assert table_1["confirmed"] is True
    assert table_1["status"] == TABLE_STATUS_CONFIRMED
    assert table_1["status"] != "needs_review"


def test_temp_table_save_replay_same_execution_id_is_idempotent(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload(accessorial_fees=[]))
    payload = _save_payload_for_record(saved)
    first = _post_temp_table_save(web_client, payload, execution_id="exec-replay")
    assert first.status_code == 200
    after_first = load_temp_table_record(saved["temp_table_id"], ttl_hours=24)
    first_version = after_first.get("edit_version")
    first_edited_at = after_first.get("human_edited_at")

    second = _post_temp_table_save(web_client, payload, execution_id="exec-replay")
    assert second.status_code == 200
    second_body = second.get_json()
    assert second_body.get("idempotent_replay") is True
    assert second_body["comparison"]["current_step"] == STEP_PREPARE_TABLE_2

    after_second = load_temp_table_record(saved["temp_table_id"], ttl_hours=24)
    assert after_second.get("edit_version") == first_version
    assert after_second.get("human_edited_at") == first_edited_at

    with web_client.session_transaction() as sess:
        state = sess.get(AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY)
    table_1 = next(t for t in state["tables"].values() if t["slot_number"] == 1)
    assert table_1["confirmed"] is True
    assert table_1["status"] == TABLE_STATUS_CONFIRMED
    assert state["current_step"] == STEP_PREPARE_TABLE_2


def test_temp_table_save_replay_does_not_rewrite_temp_table_file(web_client, tmp_path, monkeypatch):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload(accessorial_fees=[]))
    payload = _save_payload_for_record(saved)
    _post_temp_table_save(web_client, payload, execution_id="exec-file")

    from app.agente_compara_doc_service import _temp_table_path

    path = _temp_table_path(saved["temp_table_id"])
    mtime_after_first = path.stat().st_mtime

    _post_temp_table_save(web_client, payload, execution_id="exec-file")
    assert path.stat().st_mtime == mtime_after_first


def test_temp_table_save_invalid_step_different_execution_id_returns_400_without_mutation(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload(accessorial_fees=[]))
    payload = _save_payload_for_record(saved)
    _post_temp_table_save(web_client, payload, execution_id="exec-a")

    before = load_temp_table_record(saved["temp_table_id"], ttl_hours=24)
    before_version = before.get("edit_version")
    before_edited_at = before.get("human_edited_at")

    resp = _post_temp_table_save(web_client, payload, execution_id="exec-b")
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error_code"] == ERROR_COMPARISON_STEP_INVALID
    assert body["message"] == "Confirmação não permitida nesta etapa."

    after = load_temp_table_record(saved["temp_table_id"], ttl_hours=24)
    assert after.get("edit_version") == before_version
    assert after.get("human_edited_at") == before_edited_at

    with web_client.session_transaction() as sess:
        state = sess.get(AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY)
    table_1 = next(t for t in state["tables"].values() if t["slot_number"] == 1)
    assert table_1["confirmed"] is True
    assert table_1["status"] == TABLE_STATUS_CONFIRMED
    assert state["current_step"] == STEP_PREPARE_TABLE_2


def test_temp_table_save_double_post_integration(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload(accessorial_fees=[]))
    payload = _save_payload_for_record(saved)

    resp1 = _post_temp_table_save(web_client, payload, execution_id="exec-double")
    resp2 = _post_temp_table_save(web_client, payload, execution_id="exec-double")
    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert resp1.get_json()["comparison"]["current_step"] == STEP_PREPARE_TABLE_2
    assert resp2.get_json()["comparison"]["current_step"] == STEP_PREPARE_TABLE_2
    assert resp2.get_json().get("idempotent_replay") is True

    record = load_temp_table_record(saved["temp_table_id"], ttl_hours=24)
    assert record.get("edit_version") == 1

    resp3 = _post_temp_table_save(web_client, payload, execution_id="exec-other")
    assert resp3.status_code == 400
    assert load_temp_table_record(saved["temp_table_id"], ttl_hours=24).get("edit_version") == 1


def test_temp_table_save_generalidade_failure_before_persist(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[_manual_accessorial_fee(value="", unit="R$")]
        ),
    )
    edited = _save_payload_for_record(saved)
    edited["edit_target"]["accessorial_fees"][0].update(
        {
            "calculation_base_id": "por_cte",
            "calculation_basis": "por CTe",
            "classification_source": "manual_configured_calculation_base",
            "operation": "fixed_amount",
        }
    )
    before = load_temp_table_record(saved["temp_table_id"], ttl_hours=24)
    resp = _post_temp_table_save(web_client, edited, execution_id="exec-gen-fail")
    assert resp.status_code == 400
    after = load_temp_table_record(saved["temp_table_id"], ttl_hours=24)
    assert after == before


def test_temp_table_save_idempotency_cache_stored_in_session(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload(accessorial_fees=[]))
    payload = _save_payload_for_record(saved)
    _post_temp_table_save(web_client, payload, execution_id="exec-cache")

    expected_key = agente_compara_temp_table_save_idempotency_key(
        comparison_id=saved.get("comparison_id"),
        table_id=saved.get("table_id"),
        temp_table_id=saved["temp_table_id"],
        edit_version=0,
        review_action="save_and_advance",
        execution_id="exec-cache",
    )
    with web_client.session_transaction() as sess:
        cache = sess.get(TEMP_TABLE_SAVE_IDEMPOTENCY_CACHE_SESSION_KEY)
    assert isinstance(cache, dict)
    assert expected_key in cache


def test_agente_compara_js_save_guard_and_execution_id():
    js = open("app/static/js/agente_compara.js", encoding="utf-8").read()
    save_block = js[js.index("function saveTempTableAndAdvance"): js.index("function byId(id)")]
    assert save_block.index("tempTableSaveInFlight = true") < save_block.index("validateTempTableBeforeAdvance()")
    assert "if (!saveSucceeded)" not in save_block
    assert "resetTempTableSaveExecutionId()" in save_block
    assert "ensureTempTableSaveExecutionId()" in js
    assert "X-Execution-ID" in save_block
    assert "dataset.agenteComparaSaveBound" in js
    assert "function handleTempTableModalSaveClick" in js
    refresh_block = js[js.index("function refreshComparisonWizardAfterTransition"): js.index("function setActiveComparisonTable")]
    assert "saveTempTableAndAdvance" not in refresh_block
    assert js.count("function initTempTableModal()") == 1
    assert "addEventListener('click', handleTempTableModalSaveClick)" in js
    assert js.count("addEventListener('click', handleTempTableModalSaveClick)") == 1


def test_agente_compara_js_save_footer_allows_confirmation_without_edit_mode():
    js = open("app/static/js/agente_compara.js", encoding="utf-8").read()
    footer_block = js[js.index("function updateTempTableModalFooter()"): js.index("function canEditFreightTables")]
    assert "saveBtn.disabled = !!(tempTableSaveInFlight || taxSaveInFlight || taxContinueInFlight || coverageSaveInFlight);" in footer_block
    assert "saveBtn.hidden = false;" in footer_block
    assert "saveBtn.disabled = !tempTableEditMode" not in footer_block
    assert "saveBtn.hidden = !tempTableEditMode;" in footer_block


def test_agente_compara_js_temp_table_save_flag_resets_after_success_close_and_table_switch():
    js = open("app/static/js/agente_compara.js", encoding="utf-8").read()
    save_block = js[js.index("function saveTempTableAndAdvance"): js.index("function byId(id)")]
    assert ".finally(function () {" in save_block
    assert "tempTableSaveInFlight = false;" in save_block
    open_block = js[js.index("function openTempTableModal()"): js.index("function closeTempTableModal()")]
    assert "tempTableSaveInFlight = false;" in open_block
    close_block = js[js.index("function closeTempTableModal()"): js.index("function handleTempTableModalSaveClick")]
    assert "tempTableSaveInFlight = false;" in close_block
    status_block = js[js.index("function handleTempTableFromStatus"): js.index("function formatBytes")]
    assert "previousTempTableId && nextTempTableId && previousTempTableId !== nextTempTableId" in status_block
    assert status_block.count("tempTableSaveInFlight = false;") >= 2


def test_route_matrix_payload_recovers_headers_title_type_and_supplier(web_client):
    saved = _apply_payload(web_client, _sample_route_matrix_payload_with_positional_rows())
    table = saved["freight_tables"][0]
    assert table["table_title"] == "Tabela Alfa"
    assert table["table_type"] == "route_matrix"
    assert table["context"]["supplier"] == "Alfa"
    assert table["columns"] == ["Regi?o", "UF", "AT? 30KG", "AT? 50KG", "AT? 70KG", "AT? 100KG", "Excedente"]
    assert table["rows"][0] == {
        "Regi?o": "Capital",
        "UF": "DF",
        "AT? 30KG": "127.51",
        "AT? 50KG": "149.69",
        "AT? 70KG": "186.28",
        "AT? 100KG": "210.67",
        "Excedente": "1.66",
    }


def test_route_matrix_agent_compare_matches_cleide_normalization(web_client):
    from app import agente_compara_doc_service, cleide_audit_doc_service

    payload = _sample_route_matrix_payload_with_positional_rows()

    with web_client.application.app_context():
        with web_client.application.test_request_context():
            agente_record = agente_compara_doc_service._coerce_temp_table_payload(
                payload,
                source_doc_ids=["doc-1"],
                table_id="table-1",
            )
            cleide_record = cleide_audit_doc_service.normalize_partial_first_extraction_to_temp_table(
                {
                    "status": "needs_review",
                    "freight_tables": [
                        {
                            "table_title": "Tabela Alfa",
                            "table_type": "route_matrix",
                            "context": {"supplier": "Alfa"},
                            "columns": ["Regi?o", "UF", "AT? 30KG", "AT? 50KG", "AT? 70KG", "AT? 100KG", "Excedente"],
                            "rows": [
                                {
                                    "Regi?o": "Capital",
                                    "UF": "DF",
                                    "AT? 30KG": "127.51",
                                    "AT? 50KG": "149.69",
                                    "AT? 70KG": "186.28",
                                    "AT? 100KG": "210.67",
                                    "Excedente": "1.66",
                                },
                                {
                                    "Regi?o": "Capital",
                                    "UF": "ES",
                                    "AT? 30KG": "155.23",
                                    "AT? 50KG": "186.28",
                                    "AT? 70KG": "208.45",
                                    "AT? 100KG": "227.3",
                                    "Excedente": "1.44",
                                },
                            ],
                            "evidence_ref": "Tabela Alfa.xlsx",
                        }
                    ],
                    "freight_routes": [],
                    "freight_values": [],
                    "accessorial_fees": [],
                    "weight_ranges": [],
                    "reading_alerts": [],
                    "evidence_refs": [],
                }
            )

    agente_table = agente_record["freight_tables"][0]
    cleide_table = cleide_record["freight_tables"][0]
    assert agente_table["table_type"] == cleide_table["table_type"] == "route_matrix"
    assert agente_table["table_title"] == cleide_table["table_title"] == "Tabela Alfa"
    assert agente_table["context"]["supplier"] == cleide_table["context"]["supplier"] == "Alfa"
    assert agente_table["columns"] == cleide_table["columns"]
    assert agente_table["rows"] == cleide_table["rows"]


def test_route_matrix_literal_uf_column_extracts_tax_destinations(web_client):
    saved = _apply_payload(web_client, _sample_route_matrix_payload_with_positional_rows())
    assert extract_tax_destination_ufs_from_temp_table(saved) == ["DF", "ES"]
