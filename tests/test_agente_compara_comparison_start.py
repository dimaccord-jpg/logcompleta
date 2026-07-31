"""Contrato SEM_COMPARACAO → PREPARE_TABLE_1 via POST /comparison/start."""
from __future__ import annotations

import importlib
import io
import os
import pathlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.agente_compara_comparison_state import (
    AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY,
    STEP_CONFIGURATION_READY,
    STEP_PREPARE_TABLE_1,
    STEP_PREPARE_TABLE_2,
    STEP_TAXES,
    TABLE_STATUS_EMPTY,
    TABLE_STATUS_LOCKED,
    create_comparison,
    get_comparison_state,
    get_table_by_slot,
    start_comparison_for_session,
)
from app.agente_compara_doc_service import (
    AGENTE_COMPARA_TEMP_TABLE_ID_SESSION_KEY,
)
from app.services.agente_compara_config_service import (
    AgenteComparaConfig,
    DEFAULT_FALLBACK_MESSAGE,
)
from tests.cleiton_doc_fixtures import make_csv, patch_cleiton_doc_cfg, patch_cleiton_doc_store


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


def _js() -> str:
    return pathlib.Path("app/static/js/agente_compara.js").read_text(encoding="utf-8")


def _assert_prepare_table_1_shape(comparison: dict) -> None:
    assert comparison["current_step"] == STEP_PREPARE_TABLE_1
    assert comparison["desired_table_count"] == 2
    assert comparison.get("primary_temp_table_id") in (None, "")
    assert "tax_config" not in comparison
    tables = sorted(comparison["tables"], key=lambda t: t["slot_number"])
    assert len(tables) == 2
    assert tables[0]["slot_number"] == 1
    assert tables[0]["status"] == TABLE_STATUS_EMPTY
    assert tables[0]["doc_count"] == 0
    assert tables[0].get("temp_table_id") in (None, "")
    assert tables[0].get("carrier_name") in (None, "")
    assert tables[0]["confirmed"] is False
    assert tables[1]["slot_number"] == 2
    assert tables[1]["status"] == TABLE_STATUS_LOCKED
    assert comparison["active_table_id"] == tables[0]["table_id"]


def test_start_empty_session_creates_prepare_table_1(web_client, monkeypatch):
    import app.run_agente_compara_temp_table as temp_mod

    gemini_mock = MagicMock(side_effect=AssertionError("Gemini não deve ser chamado"))
    monkeypatch.setattr(temp_mod, "cleiton_governed_generate_content", gemini_mock)
    monkeypatch.setattr(temp_mod, "run_agente_compara_temp_table_extraction", gemini_mock)

    resp = web_client.post("/api/agente-compara/comparison/start", json={})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["comparison_started"] is True
    assert body["idempotent_replay"] is False
    assert body["has_active_comparison"] is True
    assert body["documents"] == []
    assert body["temp_table"] is None
    assert body["current_step"] == STEP_PREPARE_TABLE_1
    _assert_prepare_table_1_shape(body["comparison"])
    assert gemini_mock.call_count == 0

    with web_client.session_transaction() as sess:
        state = get_comparison_state(sess)
        assert state is not None
        assert state["comparison_id"] == body["comparison"]["comparison_id"]
        assert AGENTE_COMPARA_TEMP_TABLE_ID_SESSION_KEY not in sess


def test_start_is_idempotent_same_session(web_client):
    r1 = web_client.post("/api/agente-compara/comparison/start", json={})
    r2 = web_client.post("/api/agente-compara/comparison/start", json={})
    assert r1.status_code == 200
    assert r2.status_code == 200
    b1 = r1.get_json()
    b2 = r2.get_json()
    assert b1["comparison"]["comparison_id"] == b2["comparison"]["comparison_id"]
    assert b2["comparison_started"] is False
    assert b2["idempotent_replay"] is True
    _assert_prepare_table_1_shape(b2["comparison"])


def test_start_helper_unit_idempotent():
    sess: dict = {}
    first = start_comparison_for_session(session_obj=sess)
    second = start_comparison_for_session(session_obj=sess)
    assert first["comparison_started"] is True
    assert second["idempotent_replay"] is True
    assert first["state"]["comparison_id"] == second["state"]["comparison_id"]
    assert AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY in sess


def test_start_does_not_overwrite_prepare_table_2(web_client):
    with web_client.session_transaction() as sess:
        state = create_comparison(session_obj=sess)
        state["current_step"] = STEP_PREPARE_TABLE_2
        table_2 = get_table_by_slot(state, 2)
        table_2["status"] = TABLE_STATUS_EMPTY
        state["active_table_id"] = table_2["table_id"]
        sess[AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = state
        old_id = state["comparison_id"]

    resp = web_client.post("/api/agente-compara/comparison/start", json={})
    body = resp.get_json()
    assert body["ok"] is True
    assert body["comparison_started"] is False
    assert body["idempotent_replay"] is True
    assert body["comparison"]["comparison_id"] == old_id
    assert body["comparison"]["current_step"] == STEP_PREPARE_TABLE_2


def test_start_does_not_overwrite_taxes(web_client):
    with web_client.session_transaction() as sess:
        state = create_comparison(session_obj=sess)
        state["current_step"] = STEP_TAXES
        sess[AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = state
        old_id = state["comparison_id"]

    resp = web_client.post("/api/agente-compara/comparison/start", json={})
    body = resp.get_json()
    assert body["comparison"]["comparison_id"] == old_id
    assert body["comparison"]["current_step"] == STEP_TAXES
    assert body["comparison_started"] is False


def test_start_does_not_overwrite_configuration_ready(web_client):
    with web_client.session_transaction() as sess:
        state = create_comparison(session_obj=sess)
        state["current_step"] = STEP_CONFIGURATION_READY
        sess[AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = state
        old_id = state["comparison_id"]

    resp = web_client.post("/api/agente-compara/comparison/start", json={})
    body = resp.get_json()
    assert body["comparison"]["comparison_id"] == old_id
    assert body["comparison"]["current_step"] == STEP_CONFIGURATION_READY


def test_reset_then_start_creates_new_comparison_id(web_client):
    first = web_client.post("/api/agente-compara/comparison/start", json={}).get_json()
    old_id = first["comparison"]["comparison_id"]
    reset = web_client.post("/api/agente-compara/comparison/reset", json={"comparison_id": old_id})
    assert reset.status_code == 200
    assert reset.get_json()["comparison"] is None

    status = web_client.get("/api/agente-compara/documents/status").get_json()
    assert status["comparison"] is None
    assert status["has_active_comparison"] is False

    second = web_client.post("/api/agente-compara/comparison/start", json={}).get_json()
    new_id = second["comparison"]["comparison_id"]
    assert new_id != old_id
    assert second["comparison_started"] is True
    _assert_prepare_table_1_shape(second["comparison"])


def test_start_ignores_external_identity_fields(web_client):
    resp = web_client.post(
        "/api/agente-compara/comparison/start",
        json={
            "comparison_id": "external-cmp",
            "table_id": "external-table",
            "slot": 99,
            "carrier_name": "Hack",
            "temp_table_id": "tt-external",
        },
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["comparison"]["comparison_id"] != "external-cmp"
    _assert_prepare_table_1_shape(body["comparison"])


def test_start_route_registered_and_method_guard(web_client):
    web = _load_web_module()
    rules = {rule.rule for rule in web.app.url_map.iter_rules()}
    assert "/api/agente-compara/comparison/start" in rules
    assert web_client.get("/api/agente-compara/comparison/start").status_code == 405


def test_start_requires_auth(app, tmp_path, monkeypatch, ctx):
    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
    web = _load_web_module()
    anon = SimpleNamespace(is_authenticated=False)
    monkeypatch.setattr(web, "current_user", anon)
    monkeypatch.setattr("app.agente_compara_api_routes.current_user", anon)
    client = web.app.test_client()
    assert client.post("/api/agente-compara/comparison/start", json={}).status_code == 401


def test_upload_with_invalid_ids_does_not_create_comparison(web_client, monkeypatch):
    import app.run_agente_compara_temp_table as temp_mod

    gemini_mock = MagicMock(side_effect=AssertionError("Gemini não deve ser chamado"))
    monkeypatch.setattr(temp_mod, "cleiton_governed_generate_content", gemini_mock)

    resp = web_client.post(
        "/api/agente-compara/documents/upload",
        data={
            "file": (io.BytesIO(make_csv([["a"], ["1"]])), "tabela.csv", "text/csv"),
            "carrier_name": "Transportadora Teste",
            "comparison_id": "deadbeef" * 4,
            "table_id": "missing-table",
            "slot": "1",
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code in (404, 409)
    body = resp.get_json()
    assert body["ok"] is False
    with web_client.session_transaction() as sess:
        assert get_comparison_state(sess) is None


def test_legacy_upload_without_ids_still_auto_creates(web_client, monkeypatch):
    import app.run_agente_compara_temp_table as temp_mod

    monkeypatch.setattr(
        temp_mod,
        "trigger_temp_table_extraction_for_session",
        lambda **_kwargs: None,
    )
    resp = web_client.post(
        "/api/agente-compara/documents/upload",
        data={
            "file": (io.BytesIO(make_csv([["Região", "Valor"], ["R1", "10"]])), "tabela.csv", "text/csv"),
            "carrier_name": "Transportadora Legada",
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["comparison"] is not None
    assert body["comparison"]["current_step"] == STEP_PREPARE_TABLE_1


def test_empty_status_then_start_then_upload_slot_1(web_client, monkeypatch):
    import app.run_agente_compara_temp_table as temp_mod

    monkeypatch.setattr(
        temp_mod,
        "trigger_temp_table_extraction_for_session",
        lambda **_kwargs: None,
    )

    status = web_client.get("/api/agente-compara/documents/status").get_json()
    assert status["comparison"] is None

    start = web_client.post("/api/agente-compara/comparison/start", json={}).get_json()
    comparison = start["comparison"]
    table_1 = next(t for t in comparison["tables"] if t["slot_number"] == 1)

    upload = web_client.post(
        "/api/agente-compara/documents/upload",
        data={
            "file": (io.BytesIO(make_csv([["Região", "Valor"], ["R1", "10"]])), "frete.csv", "text/csv"),
            "carrier_name": "Intercargo",
            "comparison_id": comparison["comparison_id"],
            "table_id": table_1["table_id"],
            "slot": "1",
        },
        content_type="multipart/form-data",
    )
    assert upload.status_code == 200
    body = upload.get_json()
    assert body["ok"] is True
    assert body["document"] is not None
    assert body["comparison"]["comparison_id"] == comparison["comparison_id"]
    slot1 = next(t for t in body["comparison"]["tables"] if t["slot_number"] == 1)
    assert slot1["table_id"] == table_1["table_id"]
    assert slot1["doc_count"] >= 1
    assert slot1.get("carrier_name") == "Intercargo"


def test_reset_followed_by_start_and_upload_uses_new_id(web_client, monkeypatch):
    import app.run_agente_compara_temp_table as temp_mod

    monkeypatch.setattr(
        temp_mod,
        "trigger_temp_table_extraction_for_session",
        lambda **_kwargs: None,
    )

    first = web_client.post("/api/agente-compara/comparison/start", json={}).get_json()
    old_id = first["comparison"]["comparison_id"]
    with web_client.session_transaction() as sess:
        state = get_comparison_state(sess)
        state["current_step"] = STEP_CONFIGURATION_READY
        sess[AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = state
        sess[AGENTE_COMPARA_TEMP_TABLE_ID_SESSION_KEY] = "legacy-from-a"

    web_client.post("/api/agente-compara/comparison/reset", json={"comparison_id": old_id})
    status = web_client.get("/api/agente-compara/documents/status").get_json()
    assert status["comparison"] is None

    second = web_client.post("/api/agente-compara/comparison/start", json={}).get_json()
    new_id = second["comparison"]["comparison_id"]
    assert new_id != old_id
    table_1 = next(t for t in second["comparison"]["tables"] if t["slot_number"] == 1)

    with web_client.session_transaction() as sess:
        # Chave legada antiga não deve substituir o slot criado pelo start.
        sess[AGENTE_COMPARA_TEMP_TABLE_ID_SESSION_KEY] = "legacy-from-a"

    upload = web_client.post(
        "/api/agente-compara/documents/upload",
        data={
            "file": (io.BytesIO(make_csv([["Região", "Valor"], ["R1", "10"]])), "frete.csv", "text/csv"),
            "carrier_name": "Nova Carrier",
            "comparison_id": new_id,
            "table_id": table_1["table_id"],
            "slot": "1",
        },
        content_type="multipart/form-data",
    )
    assert upload.status_code == 200
    body = upload.get_json()
    assert body["comparison"]["comparison_id"] == new_id
    slot1 = next(t for t in body["comparison"]["tables"] if t["slot_number"] == 1)
    assert slot1["table_id"] == table_1["table_id"]
    assert slot1.get("carrier_name") == "Nova Carrier"


def test_two_clients_same_session_share_comparison(web_client):
    """Simula duas abas: mesma sessão Flask cookie → mesma comparison."""
    a = web_client.post("/api/agente-compara/comparison/start", json={}).get_json()
    b = web_client.post("/api/agente-compara/comparison/start", json={}).get_json()
    assert a["comparison"]["comparison_id"] == b["comparison"]["comparison_id"]
    assert b["idempotent_replay"] is True

    old_id = a["comparison"]["comparison_id"]
    web_client.post("/api/agente-compara/comparison/reset", json={"comparison_id": old_id})
    c = web_client.post("/api/agente-compara/comparison/start", json={}).get_json()
    assert c["comparison"]["comparison_id"] != old_id


def test_js_empty_journey_uses_start_before_pending_identity():
    js = _js()
    assert "var API_COMPARISON_START = '/api/agente-compara/comparison/start'" in js
    assert "function ensureComparisonStarted()" in js
    pending = js[js.index("function beginPendingFreightTableUpload"): js.index("function openCarrierNameEdit")]
    assert pending.index("ensureComparisonStarted()") < pending.index("pendingFreightTableUpload = {")
    assert "comparisonId: comparisonState.comparisonId" in pending
    assert "openCarrierIdentificationPanel" in pending
    assert "Não foi possível identificar a tabela ativa para upload." in pending
    assert "setUploadLoading" not in pending
    assert "setUploadPreparationLoading(true)" in pending
    assert "freightUploadPreparationInFlight = true" in pending

    init = js[js.index("function initDocuments"): js.index("function init()")]
    upload_click = init[init.index("uploadItem.addEventListener('click'"): init.index("fileInput.addEventListener('change'")]
    assert "fileInput.click();" in upload_click
    assert "ensureComparisonStarted" not in upload_click
    assert "freightUploadPreparationInFlight" in upload_click
    change = init[init.index("fileInput.addEventListener('change'"): ]
    assert "beginPendingFreightTableUpload(file)" in change
    assert "if (!file) return;" in change


def test_js_upload_requires_explicit_identity_after_start():
    js = _js()
    upload = js[js.index("function uploadDocument"): js.index("function removeDocument")]
    assert "Identidade da comparação inconsistente" in upload
    assert "formData.append('comparison_id', comparisonIdAtStart)" in upload
    assert "formData.append('table_id', tableIdAtStart)" in upload
    assert "formData.append('slot', String(slot))" in upload
    assert "if (comparisonId) formData.append" not in upload
    assert "if (comparisonIdAtStart) formData.append" not in upload
    assert "requestGenerationAtStart" in upload
    assert "isCurrentComparisonRequest" in upload


def test_js_stale_empty_status_preserved_after_start():
    js = _js()
    fetch_block = js[js.index("function fetchDocuments"): js.index("function refreshAttachmentsAfterChat")]
    assert "!expectedComparisonId" in fetch_block
    assert "comparisonState.comparisonId" in fetch_block
    assert "return null;" in fetch_block
    sync = js[js.index("function syncComparisonStateFromPayload"): js.index("function ensureComparisonStarted")]
    assert "bumpComparisonRequestGeneration()" in sync
    assert "comparison === null" in sync


def test_js_double_click_reuses_start_promise():
    js = _js()
    ensure = js[js.index("function ensureComparisonStarted"): js.index("function activeComparisonTable")]
    assert "if (comparisonStartPromise)" in ensure
    assert "return comparisonStartPromise;" in ensure
    pending = js[js.index("function beginPendingFreightTableUpload"): js.index("function openCarrierNameEdit")]
    assert "if (comparisonStartPromise) return;" in pending
    init = js[js.index("function initDocuments"): js.index("function init()")]
    assert "if (comparisonStartPromise) return;" in init


def test_page_get_and_status_remain_passive(web_client):
    page = web_client.get("/agente-compara")
    assert page.status_code == 200
    assert b"agente_compara.js" in page.data
    status = web_client.get("/api/agente-compara/documents/status").get_json()
    assert status["comparison"] is None
    assert status["has_active_comparison"] is False
    with web_client.session_transaction() as sess:
        assert get_comparison_state(sess) is None
