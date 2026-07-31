"""Testes da fase multitabela do AgenteCompara (preparação de 2-3 tabelas)."""
from __future__ import annotations

import importlib
import io
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.agente_compara_comparison_state import (
    AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY,
    STEP_ASK_TABLE_3,
    STEP_TAXES,
    create_comparison,
    get_comparison_state,
)
from tests.cleiton_doc_fixtures import make_csv, patch_cleiton_doc_cfg, patch_cleiton_doc_store


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


def _patch_ac_cfg(monkeypatch):
    from app.services.agente_compara_config_service import (
        AgenteComparaConfig,
        DEFAULT_FALLBACK_MESSAGE,
    )

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


def _extraction_json(carrier: str = "Transp A") -> str:
    return json.dumps(
        {
            "freight_tables": [{"name": "T1", "columns": ["col"], "rows": [["1"]]}],
            "freight_routes": [],
            "accessorial_fees": [],
            "detected_carrier": carrier,
            "reading_alerts": [],
            "evidence_refs": [],
        }
    )


def _patch_extraction(monkeypatch, calls: dict):
    import app.run_agente_compara_temp_table as temp_mod

    def _fake_run(source_doc_ids, **kwargs):
        calls["count"] = calls.get("count", 0) + 1
        calls["last_table_id"] = kwargs.get("table_id")
        from app.agente_compara_doc_service import apply_temp_table_extraction_from_model_payload

        return apply_temp_table_extraction_from_model_payload(
            json.loads(_extraction_json(f"C{calls['count']}")),
            source_doc_ids=source_doc_ids,
            table_id=kwargs.get("table_id"),
            comparison_id=kwargs.get("comparison_id"),
            slot_number=kwargs.get("slot_number"),
        )

    monkeypatch.setattr(temp_mod, "run_agente_compara_temp_table_extraction", _fake_run)
    monkeypatch.setattr(
        temp_mod,
        "build_agente_compara_document_context_for_chat",
        lambda *_a, **_k: {"has_documents": True, "context_block": "ctx", "gemini_file_parts": None},
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


def test_comparison_state_creates_two_slots(app):
    app.config["SECRET_KEY"] = "test-secret"
    with app.test_request_context():
        state = create_comparison()
        assert state["comparison_id"]
        assert len(state["tables"]) == 2
        slots = sorted(t["slot_number"] for t in state["tables"].values())
        assert slots == [1, 2]
        assert state["current_step"] == "PREPARE_TABLE_1"


def test_status_returns_comparison(web_client):
    resp = web_client.get("/api/agente-compara/documents/status")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body.get("comparison") is None
    assert body["has_active_comparison"] is False
    assert body["documents"] == []
    assert body["temp_table"] is None

    with web_client.session_transaction() as sess:
        create_comparison(session_obj=sess)

    resp_active = web_client.get("/api/agente-compara/documents/status")
    assert resp_active.status_code == 200
    active = resp_active.get_json()
    assert active["ok"] is True
    assert active["has_active_comparison"] is True
    assert active["comparison"]["current_step"] == "PREPARE_TABLE_1"


def test_upload_isolated_by_slot(web_client, monkeypatch):
    calls: dict = {}
    _patch_extraction(monkeypatch, calls)

    content = make_csv([["a", "b"], ["1", "2"]])
    r1 = web_client.post(
        "/api/agente-compara/documents/upload",
        data={
            "file": (io.BytesIO(content), "t1.csv", "text/csv"),
            "slot": "1",
            "carrier_name": "Transportadora A",
        },
        content_type="multipart/form-data",
    )
    assert r1.status_code == 200
    cmp1 = r1.get_json()["comparison"]
    table2_id = cmp1["tables"][1]["table_id"]

    with web_client.session_transaction() as sess:
        state = get_comparison_state(sess)
        t1 = next(t for t in state["tables"].values() if t["slot_number"] == 1)
        t2 = next(t for t in state["tables"].values() if t["slot_number"] == 2)
        assert len(t1["doc_ids"]) == 1
        assert len(t2["doc_ids"]) == 0
        t1["temp_table_id"] = "tt-test-1"
        t1["confirmed"] = True
        t1["status"] = "confirmed"
        t2["status"] = "empty"
        state["current_step"] = "PREPARE_TABLE_2"
        state["active_table_id"] = table2_id
        sess[AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = state

    r2 = web_client.post(
        "/api/agente-compara/documents/upload",
        data={
            "file": (io.BytesIO(content), "t2.csv", "text/csv"),
            "slot": "2",
            "table_id": table2_id,
            "carrier_name": "Transportadora B",
        },
        content_type="multipart/form-data",
    )
    assert r2.status_code == 200
    assert calls.get("count") == 2
    assert calls.get("last_table_id") == table2_id


def test_proceed_two_tables_reaches_taxes(web_client):
    comparison_id = None
    with web_client.session_transaction() as sess:
        state = create_comparison(session_obj=sess)
        comparison_id = state["comparison_id"]
        for slot in (1, 2):
            entry = next(t for t in state["tables"].values() if t["slot_number"] == slot)
            entry["temp_table_id"] = f"tt-{slot}"
            entry["confirmed"] = True
            entry["status"] = "confirmed"
        state["current_step"] = STEP_ASK_TABLE_3
        sess[AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = state

    resp = web_client.post(
        "/api/agente-compara/comparison/proceed-two-tables",
        json={"comparison_id": comparison_id},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["comparison"]["current_step"] == STEP_TAXES
    assert body.get("next_step") == STEP_TAXES


def test_add_third_table_without_gemini(web_client, monkeypatch):
    import app.run_agente_compara_temp_table as temp_mod

    gemini_mock = MagicMock(side_effect=AssertionError("Gemini não deve ser chamado"))
    monkeypatch.setattr(temp_mod, "cleiton_governed_generate_content", gemini_mock)

    comparison_id = None
    with web_client.session_transaction() as sess:
        state = create_comparison(session_obj=sess)
        comparison_id = state["comparison_id"]
        for slot in (1, 2):
            entry = next(t for t in state["tables"].values() if t["slot_number"] == slot)
            entry["temp_table_id"] = f"tt-{slot}"
            entry["confirmed"] = True
        state["current_step"] = STEP_ASK_TABLE_3
        sess[AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = state

    resp = web_client.post(
        "/api/agente-compara/comparison/add-third-table",
        json={"comparison_id": comparison_id},
    )
    assert resp.status_code == 200
    assert resp.get_json()["comparison"]["desired_table_count"] == 3
    assert gemini_mock.call_count == 0


def test_gemini_call_count_two_tables(web_client, monkeypatch):
    calls: dict = {}
    _patch_extraction(monkeypatch, calls)

    content = make_csv([["a"], ["1"]])
    web_client.post(
        "/api/agente-compara/documents/upload",
        data={
            "file": (io.BytesIO(content), "a.csv", "text/csv"),
            "slot": "1",
            "carrier_name": "Transportadora A",
        },
        content_type="multipart/form-data",
    )

    with web_client.session_transaction() as sess:
        state = get_comparison_state(sess)
        e1 = next(t for t in state["tables"].values() if t["slot_number"] == 1)
        e2 = next(t for t in state["tables"].values() if t["slot_number"] == 2)
        e1["temp_table_id"] = "tt1"
        e1["confirmed"] = True
        e1["status"] = "confirmed"
        e2["status"] = "empty"
        state["current_step"] = "PREPARE_TABLE_2"
        state["active_table_id"] = e2["table_id"]
        sess[AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = state

    web_client.post(
        "/api/agente-compara/documents/upload",
        data={
            "file": (io.BytesIO(content), "b.csv", "text/csv"),
            "slot": "2",
            "carrier_name": "Transportadora B",
        },
        content_type="multipart/form-data",
    )
    assert calls.get("count") == 2


def test_cross_comparison_scope_rejected(web_client):
    with web_client.session_transaction() as sess:
        state = create_comparison(session_obj=sess)
        comparison_id = state["comparison_id"]

    resp = web_client.get(f"/api/agente-compara/documents/status?comparison_id={'deadbeef' * 4}")
    assert resp.status_code == 409
    assert resp.get_json()["ok"] is False

    resp_ok = web_client.get(f"/api/agente-compara/documents/status?comparison_id={comparison_id}")
    assert resp_ok.status_code == 200


def _agente_compara_js() -> str:
    import pathlib

    return pathlib.Path("app/static/js/agente_compara.js").read_text(encoding="utf-8")


def _agente_compara_html() -> str:
    import pathlib

    return pathlib.Path("app/templates/agente_compara.html").read_text(encoding="utf-8")


def test_agente_compara_js_declares_upload_in_flight():
    js = _agente_compara_js()
    assert "var uploadInFlight = false;" in js
    assert js.count("uploadInFlight") >= 4


def test_agente_compara_js_upload_listener_uses_persistent_file_input():
    js = _agente_compara_js()
    html = _agente_compara_html()
    assert html.count('id="agenteComparaFileInput"') == 1
    init_block = js[js.index("function initDocuments"): js.index("function init()")]
    assert "byId('agenteComparaFileInput')" in init_block
    assert "fileInput.addEventListener('change'" in init_block
    assert "beginPendingFreightTableUpload(file)" in init_block
    assert "uploadDocument(file)" not in init_block.split("fileInput.addEventListener('change'")[1].split("if (clearBtn)")[0]


def test_agente_compara_js_upload_menu_item_clicks_file_input():
    js = _agente_compara_js()
    init_block = js[js.index("function initDocuments"): js.index("function init()")]
    upload_click = init_block[init_block.index("uploadItem.addEventListener('click'"): init_block.index("fileInput.addEventListener('change'")]
    assert "freightUploadPreparationInFlight" in upload_click
    assert "uploadInFlight" in upload_click
    assert "pendingFreightTableUpload" in upload_click
    assert "isCarrierIdentificationOpen()" in upload_click
    assert "fileInput.click();" in upload_click
    assert "ReferenceError" not in js


def test_agente_compara_js_cancel_selection_does_not_set_upload_loading():
    js = _agente_compara_js()
    init_block = js[js.index("function initDocuments"): js.index("function init()")]
    change_block = init_block[init_block.index("fileInput.addEventListener('change'"): ]
    assert "if (!file) return;" in change_block
    assert change_block.index("fileInput.value = ''") < change_block.index("if (!file) return;")


def test_agente_compara_js_upload_form_data_includes_comparison_identity():
    js = _agente_compara_js()
    upload_block = js[js.index("function uploadDocument"): js.index("function removeDocument")]
    assert "formData.append('file', file, file.name)" in upload_block
    assert "formData.append('carrier_name', validation.name)" in upload_block
    assert "formData.append('comparison_id', comparisonIdAtStart)" in upload_block
    assert "formData.append('table_id', tableIdAtStart)" in upload_block
    assert "formData.append('slot', String(slot))" in upload_block
    assert "requestGenerationAtStart" in upload_block
    assert "isCurrentComparisonRequest" in upload_block


def test_agente_compara_js_carrier_identification_flow():
    js = _agente_compara_js()
    html = _agente_compara_html()
    assert 'id="agenteComparaCarrierIdentifyPanel"' in html
    assert "Por favor, informe a transportadora." in html
    assert "function beginPendingFreightTableUpload" in js
    assert "function confirmCarrierIdentification" in js
    assert "function validateCarrierNameInput" in js
    assert "pendingFreightTableUpload" in js
    assert "function initCarrierIdentificationPanel" in js
    assert js.count("function initCarrierIdentificationPanel()") == 1
    confirm_block = js[js.index("function confirmCarrierIdentification"): js.index("function initCarrierIdentificationPanel")]
    assert "carrierIdentifyInFlight" in confirm_block
    assert "uploadDocument(file, validation.name" in confirm_block
    cancel_block = js[js.index("function cancelCarrierIdentification"): js.index("function openCarrierNameEdit")]
    assert "resetFreightFileInput()" in cancel_block
    assert "fetch(" not in cancel_block


def test_agente_compara_js_carrier_continue_disabled_until_valid():
    js = _agente_compara_js()
    assert "updateCarrierIdentifyContinueButton" in js
    assert "continueBtn.disabled = !validation.ok || carrierIdentifyInFlight" in js


def test_agente_compara_js_review_header_shows_carrier():
    js = _agente_compara_js()
    header_block = js[js.index("function setComparisonWizardModalHeader"): js.index("function appendWizardConfirmedSummary")]
    assert "Transportadora:" in header_block
    assert "Revisão da" in header_block
    assert "renderModalCarrierEditLink" in header_block


def test_agente_compara_js_refresh_does_not_call_upload():
    js = _agente_compara_js()
    refresh_block = js[js.index("function refreshComparisonWizardAfterTransition"): js.index("function setActiveComparisonTable")]
    assert "uploadDocument" not in refresh_block


def test_agente_compara_js_upload_slot_resolves_from_active_table():
    js = _agente_compara_js()
    pending_block = js[js.index("function beginPendingFreightTableUpload"): js.index("function openCarrierNameEdit")]
    assert "ensureComparisonStarted()" in pending_block
    assert "activeComparisonTable()" in pending_block
    assert "active.table_id" in pending_block
    assert "active.slot_number" in pending_block
    assert "Não foi possível identificar a tabela ativa para upload." in pending_block
    assert "Não foi possível iniciar uma nova comparação. Tente novamente." in pending_block
    assert "setUploadLoading" not in pending_block
    assert "setUploadPreparationLoading(true)" in pending_block


def test_agente_compara_js_ensure_comparison_started_helper():
    js = _agente_compara_js()
    assert "var API_COMPARISON_START = '/api/agente-compara/comparison/start'" in js
    assert "function ensureComparisonStarted()" in js
    assert "var comparisonStartPromise = null" in js
    ensure_block = js[js.index("function ensureComparisonStarted"): js.index("function activeComparisonTable")]
    assert "API_COMPARISON_START" in ensure_block
    assert "comparisonStartPromise" in ensure_block
    assert "syncComparisonStateFromPayload(res.data.comparison)" in ensure_block
    wizard_block = js[js.index("function triggerComparisonWizardFileInput"): js.index("function renderComparisonWizardUploadBody")]
    assert "comparisonState.comparisonId" in wizard_block
    assert "API_COMPARISON_START" not in wizard_block


def test_agente_compara_js_sync_handles_null_comparison():
    js = _agente_compara_js()
    sync = js[js.index("function syncComparisonStateFromPayload"): js.index("function ensureComparisonStarted")]
    assert "if (comparison === null)" in sync
    assert "clearLocalComparisonState()" in sync
    assert "invalidateComparisonDerivedState()" in sync
    fetch_block = js[js.index("function fetchDocuments"): js.index("function refreshAttachmentsAfterChat")]
    assert "comparisonState.comparisonId" in fetch_block
    assert "has_active_comparison === false" in fetch_block


def test_agente_compara_js_rerender_does_not_recreate_file_input():
    js = _agente_compara_js()
    wizard_block = js[js.index("function renderComparisonWizardModal"): js.index("function shouldAutoOpenComparisonWizard")]
    assert "agenteComparaFileInput" not in wizard_block
    assert "type=\"file\"" not in wizard_block
    html = _agente_compara_html()
    assert html.count('id="agenteComparaFileInput"') == 1


def test_agente_compara_js_table_status_helpers_exist():
    js = _agente_compara_js()
    assert "function formatTableStatusShort(status, confirmed)" in js
    assert "function formatTableStatusHint(status, confirmed)" in js
    assert "Transportadora não identificada" not in js


def test_agente_compara_js_save_temp_table_keeps_modal_during_prepare():
    js = _agente_compara_js()
    save_block = js[js.index("function saveTempTableAndAdvance"): js.index("function byId(id)")]
    prepare_branch = save_block[save_block.index("if (isComparisonPrepareStep())"): save_block.index("if (isComparisonCommonParamsStep())")]
    assert "closeTempTableModal()" not in prepare_branch
    assert "refreshComparisonWizardAfterTransition()" in prepare_branch


def test_agente_compara_js_comparison_wizard_modal_renderer():
    js = _agente_compara_js()
    assert "function renderComparisonWizardModal()" in js
    assert "function resolveComparisonWizardView()" in js
    assert "function openComparisonWizardModal()" in js
    assert "Prepare a segunda tabela de frete" in js
    assert "Enviar segunda tabela" in js
    assert "Concluir preparação com duas tabelas" in js
    assert "Prosseguir com a análise" not in js


def test_agente_compara_js_no_external_slot_selector():
    js = _agente_compara_js()
    assert "agente-compara-table-tabs" not in js
    assert "agente-compara-table-tab" not in js
    assert "Tabelas de frete</h3>" not in js
    assert "Continuar preparação das tabelas" not in js
    assert "updateContinuePrepButton" not in js


def test_agente_compara_js_wizard_upload_uses_persistent_input():
    js = _agente_compara_js()
    wizard_block = js[js.index("function triggerComparisonWizardFileInput"): js.index("function renderComparisonWizardUploadBody")]
    assert "byId('agenteComparaFileInput')" in wizard_block
    assert "fileInput.click()" in wizard_block
    upload_body = js[js.index("function renderComparisonWizardUploadBody"): js.index("function renderComparisonWizardAskTable3Body")]
    assert "agenteComparaWizardUploadBtn" in upload_body
    assert "uploadDocument" not in upload_body


def test_agente_compara_js_wizard_upload_form_data_uses_active_slot():
    js = _agente_compara_js()
    upload_block = js[js.index("function uploadDocument"): js.index("function removeDocument")]
    assert "formData.append('slot', String(slot))" in upload_block
    pending_block = js[js.index("function beginPendingFreightTableUpload"): js.index("function openCarrierNameEdit")]
    assert "slot: active.slot_number" in pending_block
    assert "ensureComparisonStarted()" in pending_block


def test_agente_compara_js_wizard_ask_table3_actions():
    js = _agente_compara_js()
    ask_block = js[js.index("function renderComparisonWizardAskTable3Body"): js.index("function renderComparisonWizardTablesReadyBody")]
    assert "chooseAddThirdTable" in ask_block
    assert "chooseProceedWithTwoTables" in ask_block
    assert "Adicionar terceira tabela" in ask_block
    proceed_block = js[js.index("function chooseProceedWithTwoTables"): js.index("function chooseAddThirdTable")]
    assert "API_COMPARISON_PROCEED_TWO" in proceed_block
    assert "activateComparisonCommonParamsStep('TAXES')" in proceed_block
    add_block = js[js.index("function chooseAddThirdTable"): js.index("function isComparisonPrepareStep")]
    assert "API_COMPARISON_ADD_THIRD" in add_block
    assert "refreshComparisonWizardAfterTransition()" in add_block
    assert "cleiton_governed_generate_content" not in add_block


def test_agente_compara_js_wizard_engagement_requires_explicit_action():
    js = _agente_compara_js()
    assert "function isComparisonWizardEngaged()" in js
    assert "function markComparisonWizardEngaged()" in js
    assert "comparisonWizardEngaged" in js
    assert "comparisonWizardModalSuppressed" in js
    engaged_block = js[js.index("function isComparisonWizardEngaged"): js.index("function markComparisonWizardEngaged")]
    assert "comparisonHasUploadActivity" not in engaged_block
    status_block = js[js.index("function handleTempTableFromStatus"): js.index("function formatBytes")]
    assert "markComparisonWizardEngaged()" not in status_block
    auto_block = js[js.index("function shouldAutoOpenComparisonWizard"): js.index("function maybeOpenComparisonWizardAfterStatus")]
    assert "comparisonWizardEngaged" in auto_block
    assert "PREPARE_TABLE_2" not in auto_block
    init_block = js[js.index("function initDocuments"): js.index("function init()")]
    assert "openComparisonWizardModal()" not in init_block
    upload_block = js[js.index("function uploadDocument"): js.index("function removeDocument")]
    assert "markComparisonWizardEngaged()" in upload_block
    assert "comparisonWizardModalSuppressed = false" in upload_block


def test_agente_compara_template_no_continue_prep_panel():
    html = _agente_compara_html()
    assert 'id="agenteComparaTablesPrepPanel"' not in html
    assert "Continuar preparação das tabelas" not in html
    assert "agente-compara-continue-prep-panel" not in html
    assert ".agente-compara-wizard-panel" in html
    assert ".agente-compara-table-tabs" not in html
    assert 'data-typewriter-text="Faça o upload da tabela de frete."' in html


def test_agente_compara_js_single_persistent_file_input():
    js = _agente_compara_js()
    html = _agente_compara_html()
    assert html.count('id="agenteComparaFileInput"') == 1
    assert "type=\"file\"" not in js[js.index("function renderComparisonWizardModal"): js.index("function shouldAutoOpenComparisonWizard")]


def test_agente_compara_js_upload_finally_restores_loading():
    js = _agente_compara_js()
    upload_block = js[js.index("function uploadDocument"): js.index("function removeDocument")]
    assert "setUploadLoading(true)" in upload_block
    assert ".finally(function () {" in upload_block
    assert "setUploadLoading(false)" in upload_block
    set_loading = js[js.index("function setUploadLoading"): js.index("function docTypeLabel")]
    assert "uploadInFlight = !!on;" in set_loading


def test_agente_compara_js_upload_errors_restore_loading():
    js = _agente_compara_js()
    upload_block = js[js.index("function uploadDocument"): js.index("function removeDocument")]
    assert "setError(res.data || friendlyError(res.data))" in upload_block
    assert "setError('Não foi possível enviar o documento. Tente novamente.')" in upload_block
    assert upload_block.index("setUploadLoading(true)") < upload_block.index(".finally(function () {")


def test_agente_compara_js_init_documents_registers_once():
    js = _agente_compara_js()
    assert js.count("function initDocuments()") == 1
    assert "initDocuments();" in js.split("function init()")[1].split("function initTempTableModal")[0]


def test_agente_compara_js_auto_open_only_first_table_after_upload():
    js = _agente_compara_js()
    auto_block = js[js.index("function shouldAutoOpenComparisonWizard"): js.index("function maybeOpenComparisonWizardAfterStatus")]
    assert "step !== 'PREPARE_TABLE_1'" in auto_block or "step === 'PREPARE_TABLE_1'" in auto_block
    assert "view !== 'review'" in auto_block
    assert "isReviewReadyTempTable(currentTempTable)" in auto_block
    assert "uploadInFlight" in auto_block
    # processing NÃO dispara auto-open
    assert "view === 'review' || view === 'processing'" not in auto_block
    assert "'processing'" not in auto_block
    assert "comparisonWizardModalSuppressed" in auto_block


def test_agente_compara_js_close_modal_suppresses_auto_reopen():
    js = _agente_compara_js()
    close_block = js[js.index("function closeTempTableModal"): js.index("function initTempTableModal")]
    assert "comparisonWizardModalSuppressed = true" in close_block
    assert "updateContinuePrepButton" not in close_block


def test_agente_compara_js_artifact_click_opens_wizard_explicitly():
    js = _agente_compara_js()
    item_block = js[js.index("function renderTempTableItem"): js.index("function renderDocumentItem")]
    assert "comparisonWizardModalSuppressed = false" in item_block
    assert "markComparisonWizardEngaged()" in item_block
    assert "openTempTableModal()" in item_block


def test_agente_compara_js_refresh_after_save_keeps_modal_flow():
    js = _agente_compara_js()
    refresh_block = js[js.index("function refreshComparisonWizardAfterTransition"): js.index("function setActiveComparisonTable")]
    assert "comparisonWizardModalSuppressed = false" in refresh_block
    assert "openComparisonWizardModal()" in refresh_block
    assert "closeTempTableModal()" not in refresh_block


def test_agente_compara_js_comparison_common_params_flow():
    js = _agente_compara_js()
    assert "function isComparisonCommonParamsStep" in js
    assert "function activateComparisonCommonParamsStep" in js
    assert "CONFIGURATION_READY" in js
    assert "As configurações estão prontas. O cálculo será iniciado somente após sua confirmação." in js
    assert "function renderConfigurationReviewTabs" in js
    assert "Arquivo para Comparação" in js
    assert "Arquivo para auditoria" not in js
    proceed_block = js[js.index("function chooseProceedWithTwoTables"): js.index("function chooseAddThirdTable")]
    assert "activateComparisonCommonParamsStep('TAXES')" in proceed_block
    assert "closeTempTableModal()" not in proceed_block
    activate = js[js.index("function activateComparisonCommonParamsStep"): js.index("function appendWizardConfirmedSummary")]
    ready_branch = activate[activate.index("step === 'CONFIGURATION_READY'"):]
    assert "tempTableModalActiveTab = 'audit'" not in ready_branch
    assert "ensureConfigurationReviewDefaults" in ready_branch
    assert "forceFirstCarrier" in ready_branch


def test_agente_compara_js_cancel_selection_keeps_modal_closed():
    js = _agente_compara_js()
    init_block = js[js.index("function initDocuments"): js.index("function init()")]
    change_block = init_block[init_block.index("fileInput.addEventListener('change'"): ]
    assert "if (!file) return;" in change_block
    assert change_block.index("fileInput.value = ''") < change_block.index("if (!file) return;")
    assert "openComparisonWizardModal()" not in change_block


def test_agente_compara_js_handle_status_does_not_mark_engaged():
    js = _agente_compara_js()
    block = js[js.index("function handleTempTableFromStatus"): js.index("function formatBytes")]
    assert "markComparisonWizardEngaged()" not in block
    assert "maybeOpenComparisonWizardAfterStatus()" in block


def test_agente_compara_js_prepare_table_1_empty_does_not_auto_open_on_status():
    js = _agente_compara_js()
    maybe_block = js[js.index("function maybeOpenComparisonWizardAfterStatus"): js.index("function openComparisonWizardModal")]
    assert "shouldAutoOpenComparisonWizard()" in maybe_block
    auto_block = js[js.index("function shouldAutoOpenComparisonWizard"): js.index("function maybeOpenComparisonWizardAfterStatus")]
    assert "comparisonWizardEngaged" in auto_block


def test_agente_compara_page_status_prepare_table_1_no_gemini_on_load(web_client, monkeypatch):
    import app.run_agente_compara_temp_table as temp_mod

    gemini_mock = MagicMock(side_effect=AssertionError("Gemini não deve ser chamado no carregamento"))
    monkeypatch.setattr(temp_mod, "cleiton_governed_generate_content", gemini_mock)
    monkeypatch.setattr(temp_mod, "run_agente_compara_temp_table_extraction", gemini_mock)

    with web_client.session_transaction() as sess:
        create_comparison(session_obj=sess)

    resp = web_client.get("/api/agente-compara/documents/status")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["comparison"]["current_step"] == "PREPARE_TABLE_1"
    assert gemini_mock.call_count == 0


def test_agente_compara_refresh_prepare_table_2_does_not_imply_auto_open_in_js():
    js = _agente_compara_js()
    auto_block = js[js.index("function shouldAutoOpenComparisonWizard"): js.index("function maybeOpenComparisonWizardAfterStatus")]
    assert "PREPARE_TABLE_2" not in auto_block
    assert "ASK_TABLE_3" not in auto_block
