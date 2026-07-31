"""Reset completo da comparação (AgenteCompara) — backend + contrato UI."""
from __future__ import annotations

import importlib
import io
import os
import pathlib
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.agente_compara_comparison_state import (
    AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY,
    STEP_CONFIGURATION_READY,
    STEP_PREPARE_TABLE_1,
    create_comparison,
    get_comparison_state,
    get_table_by_slot,
    set_comparison_state,
    set_comparison_tax_config,
)
from app.agente_compara_doc_service import (
    AGENTE_COMPARA_DOC_IDS_SESSION_KEY,
    AGENTE_COMPARA_TEMP_TABLE_ID_SESSION_KEY,
    AGENTE_COMPARA_TEMP_TABLE_SOURCE_DOCS_SESSION_KEY,
    TEMP_TABLE_SAVE_IDEMPOTENCY_CACHE_SESSION_KEY,
    TEMP_TABLE_VERSION_MARKER,
    get_temp_table_id,
    load_temp_table_record,
    reset_comparison_for_session,
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


def _sample_payload(*, title: str = "Tabela", region: str = "R1") -> dict:
    return {
        "freight_tables": [
            {
                "name": title,
                "columns": ["Região", "Valor"],
                "rows": [[region, "10"]],
            }
        ],
        "freight_routes": [],
        "accessorial_fees": [],
        "destinations": [{"uf": "SP", "city": "Campinas"}],
    }


def _write_temp_table_record(web_client, *, comparison_id, table_id, slot_number, payload):
    from app.agente_compara_doc_service import (
        FIELD_COMPARISON_ID,
        FIELD_SLOT_NUMBER,
        FIELD_TABLE_ID,
        TEMP_TABLE_STATUS_NEEDS_REVIEW,
        _coerce_temp_table_payload,
        _temp_table_path,
        _write_temp_table_atomic,
        AGENTE_COMPARA_DOC_IDS_SESSION_KEY,
    )

    with web_client.application.app_context():
        with web_client.application.test_request_context():
            record = _coerce_temp_table_payload(payload, source_doc_ids=[f"doc-{slot_number}"])
    record[FIELD_COMPARISON_ID] = comparison_id
    record[FIELD_TABLE_ID] = table_id
    record[FIELD_SLOT_NUMBER] = slot_number
    record["temp_table_id"] = uuid4().hex
    record["status"] = TEMP_TABLE_STATUS_NEEDS_REVIEW
    record["session_scope"] = AGENTE_COMPARA_DOC_IDS_SESSION_KEY
    record["version_marker"] = TEMP_TABLE_VERSION_MARKER
    with web_client.application.app_context():
        _write_temp_table_atomic(_temp_table_path(record["temp_table_id"]), record)
    return record


def _attach_shared_coverage_and_audit(web_client, primary_temp_table_id: str) -> None:
    from app.agente_compara_doc_service import (
        AUDIT_BATCH_STATUS_UPLOADED,
        load_temp_table_record,
        _temp_table_path,
        _write_temp_table_atomic,
    )

    with web_client.application.app_context():
        record = load_temp_table_record(primary_temp_table_id, ttl_hours=24)
        assert record is not None
        record["coverage_table"] = {
            "rows": [
                {
                    "destination_uf": "SP",
                    "destination_city": "Campinas",
                    "freight_region": "Interior",
                    "notes": "Cobertura teste",
                }
            ],
            "validation_warnings": [],
        }
        record["audit_batch"] = {
            "status": AUDIT_BATCH_STATUS_UPLOADED,
            "audit_batch_id": uuid4().hex,
            "temp_table_id": primary_temp_table_id,
            "source_file_name": "operacional.csv",
            "row_count": 3,
            "max_rows": 2000,
            "uploaded_at": "2026-07-24T12:00:00+00:00",
            "results": [],
            "summary": None,
        }
        _write_temp_table_atomic(_temp_table_path(primary_temp_table_id), record)


def _bootstrap_ready_comparison(web_client, *, table_count: int = 2) -> dict:
    carriers = ["Intercargo", "Alfa", "Braspress"][:table_count]
    records: dict[int, dict] = {}
    table_ids: list[str] = []
    comparison_id = None

    with web_client.session_transaction() as sess:
        state = create_comparison(session_obj=sess)
        comparison_id = state["comparison_id"]
        if table_count >= 3 and get_table_by_slot(state, 3) is None:
            from app.agente_compara_comparison_state import STEP_ASK_TABLE_3, add_third_table

            state["current_step"] = STEP_ASK_TABLE_3
            state = add_third_table(state, session_obj=sess)

        for idx in range(table_count):
            slot = idx + 1
            entry = get_table_by_slot(state, slot)
            assert entry is not None
            record = _write_temp_table_record(
                web_client,
                comparison_id=comparison_id,
                table_id=entry["table_id"],
                slot_number=slot,
                payload=_sample_payload(title=f"Tabela {slot}", region=f"R{slot}"),
            )
            entry["temp_table_id"] = record["temp_table_id"]
            entry["confirmed"] = True
            entry["status"] = "confirmed"
            entry["carrier_name"] = carriers[idx]
            entry["doc_ids"] = [f"doc-{slot}"]
            records[slot] = record
            table_ids.append(entry["table_id"])

        state["desired_table_count"] = table_count
        state["primary_temp_table_id"] = records[1]["temp_table_id"]
        state["active_table_id"] = table_ids[0]
        state["current_step"] = STEP_CONFIGURATION_READY
        tax_config = {
            "include_taxes": True,
            "origin_uf": "SP",
            "origin_city": "Campinas",
            "iss_rate": 5.0,
            "selected_table_ids": table_ids[:1],
            "destination_ufs": [{"uf": "RJ", "source": "manual", "evidence": []}],
            "icms_rates": [
                {
                    "destination_uf": "RJ",
                    "applied_rate": 12.0,
                    "suggested_rate": 12.0,
                    "is_active": True,
                    "user_edited": False,
                }
            ],
            "manual_added_ufs": [],
            "manual_removed_ufs": [],
            "confirmed": True,
        }
        set_comparison_tax_config(state, tax_config)
        set_comparison_state(state, session_obj=sess)
        sess[AGENTE_COMPARA_TEMP_TABLE_ID_SESSION_KEY] = records[1]["temp_table_id"]
        sess[AGENTE_COMPARA_TEMP_TABLE_SOURCE_DOCS_SESSION_KEY] = ["doc-1"]
        sess[AGENTE_COMPARA_DOC_IDS_SESSION_KEY] = [f"doc-{i}" for i in range(1, table_count + 1)]
        sess[TEMP_TABLE_SAVE_IDEMPOTENCY_CACHE_SESSION_KEY] = {
            f"agente-compara-temp-table-save:{comparison_id}:x": {"ok": True}
        }

    _attach_shared_coverage_and_audit(web_client, records[1]["temp_table_id"])
    return {
        "comparison_id": comparison_id,
        "table_ids": table_ids,
        "records": records,
        "carriers": carriers,
    }


def _assert_temp_gone(tmp_path, temp_table_id: str) -> None:
    assert not (tmp_path / f"tt_{temp_table_id}.json").exists()


def test_status_empty_does_not_auto_create_comparison(web_client):
    resp = web_client.get("/api/agente-compara/documents/status")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["comparison"] is None
    assert body["has_active_comparison"] is False
    assert body["documents"] == []
    assert body["temp_table"] is None
    assert body.get("current_step") is None
    with web_client.session_transaction() as sess:
        assert get_comparison_state(sess) is None
        assert AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY not in sess


def test_reset_configuration_ready_two_tables(web_client, tmp_path):
    boot = _bootstrap_ready_comparison(web_client, table_count=2)
    old_id = boot["comparison_id"]
    temp_ids = [boot["records"][1]["temp_table_id"], boot["records"][2]["temp_table_id"]]

    resp = web_client.post(
        "/api/agente-compara/comparison/reset",
        json={"comparison_id": old_id},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["comparison_reset"] is True
    assert body["previous_comparison_id"] == old_id
    assert body["comparison"] is None
    assert body["documents"] == []
    assert body["temp_table"] is None
    assert body["current_step"] is None
    assert body["has_active_comparison"] is False

    with web_client.session_transaction() as sess:
        assert get_comparison_state(sess) is None
        assert AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY not in sess
        assert AGENTE_COMPARA_DOC_IDS_SESSION_KEY not in sess
        assert AGENTE_COMPARA_TEMP_TABLE_ID_SESSION_KEY not in sess
        assert AGENTE_COMPARA_TEMP_TABLE_SOURCE_DOCS_SESSION_KEY not in sess
        assert TEMP_TABLE_SAVE_IDEMPOTENCY_CACHE_SESSION_KEY not in sess
        assert get_temp_table_id(sess) is None

    for tid in temp_ids:
        _assert_temp_gone(tmp_path, tid)


def test_reset_three_tables_removes_all_slots(web_client, tmp_path):
    boot = _bootstrap_ready_comparison(web_client, table_count=3)
    temp_ids = [boot["records"][s]["temp_table_id"] for s in (1, 2, 3)]
    resp = web_client.post(
        "/api/agente-compara/comparison/reset",
        json={"comparison_id": boot["comparison_id"]},
    )
    assert resp.status_code == 200
    with web_client.session_transaction() as sess:
        assert get_comparison_state(sess) is None
    for tid in temp_ids:
        _assert_temp_gone(tmp_path, tid)


def test_reset_is_idempotent(web_client, tmp_path):
    boot = _bootstrap_ready_comparison(web_client, table_count=2)
    old_id = boot["comparison_id"]
    r1 = web_client.post("/api/agente-compara/comparison/reset", json={"comparison_id": old_id})
    assert r1.status_code == 200
    r2 = web_client.post("/api/agente-compara/comparison/reset", json={"comparison_id": old_id})
    assert r2.status_code == 200
    body = r2.get_json()
    assert body["ok"] is True
    assert body["comparison_reset"] is True
    assert body["comparison"] is None
    with web_client.session_transaction() as sess:
        assert get_comparison_state(sess) is None


def test_status_after_reset_stays_empty(web_client):
    boot = _bootstrap_ready_comparison(web_client, table_count=2)
    web_client.post("/api/agente-compara/comparison/reset", json={"comparison_id": boot["comparison_id"]})
    resp = web_client.get("/api/agente-compara/documents/status")
    body = resp.get_json()
    assert body["comparison"] is None
    assert body["has_active_comparison"] is False
    assert body["documents"] == []
    assert body["temp_table"] is None


def test_refresh_after_reset_does_not_rehydrate(web_client):
    boot = _bootstrap_ready_comparison(web_client, table_count=2)
    web_client.post("/api/agente-compara/comparison/reset", json={"comparison_id": boot["comparison_id"]})
    # Simula novo request do client (refresh) na mesma sessão.
    for _ in range(2):
        body = web_client.get("/api/agente-compara/documents/status").get_json()
        assert body["comparison"] is None
        assert body["has_active_comparison"] is False


def test_new_upload_after_reset_creates_new_comparison_id(web_client, monkeypatch):
    boot = _bootstrap_ready_comparison(web_client, table_count=2)
    old_id = boot["comparison_id"]
    web_client.post("/api/agente-compara/comparison/reset", json={"comparison_id": old_id})

    monkeypatch.setattr(
        "app.agente_compara_api_routes.trigger_temp_table_extraction_for_session",
        lambda **_k: None,
    )
    content = make_csv([["a", "b"], ["1", "2"]])
    up = web_client.post(
        "/api/agente-compara/documents/upload",
        data={
            "file": (io.BytesIO(content), "nova.csv", "text/csv"),
            "carrier_name": "Nova Transportadora",
        },
        content_type="multipart/form-data",
    )
    assert up.status_code == 200
    new_cmp = up.get_json()["comparison"]
    assert new_cmp["comparison_id"] != old_id
    assert new_cmp["current_step"] == STEP_PREPARE_TABLE_1
    assert "tax_config" not in new_cmp
    with web_client.session_transaction() as sess:
        state = get_comparison_state(sess)
        assert state["comparison_id"] != old_id
        assert state.get("tax_config") is None
        assert state.get("primary_temp_table_id") is None


def test_reset_rejects_foreign_comparison_id(web_client):
    boot = _bootstrap_ready_comparison(web_client, table_count=2)
    resp = web_client.post(
        "/api/agente-compara/comparison/reset",
        json={"comparison_id": "deadbeef" * 4},
    )
    assert resp.status_code == 409
    assert resp.get_json()["ok"] is False
    with web_client.session_transaction() as sess:
        assert get_comparison_state(sess)["comparison_id"] == boot["comparison_id"]


def test_reset_preserves_cleide_session_keys(web_client, monkeypatch, tmp_path):
    from app.cleide_audit_doc_service import (
        CLEIDE_AUDIT_DOC_IDS_SESSION_KEY,
        CLEIDE_AUDIT_TEMP_TABLE_ID_SESSION_KEY,
    )

    boot = _bootstrap_ready_comparison(web_client, table_count=2)
    with web_client.session_transaction() as sess:
        sess[CLEIDE_AUDIT_DOC_IDS_SESSION_KEY] = ["cleide-doc-1"]
        sess[CLEIDE_AUDIT_TEMP_TABLE_ID_SESSION_KEY] = "cleide-tt-1"

    resp = web_client.post(
        "/api/agente-compara/comparison/reset",
        json={"comparison_id": boot["comparison_id"]},
    )
    assert resp.status_code == 200
    with web_client.session_transaction() as sess:
        assert sess.get(CLEIDE_AUDIT_DOC_IDS_SESSION_KEY) == ["cleide-doc-1"]
        assert sess.get(CLEIDE_AUDIT_TEMP_TABLE_ID_SESSION_KEY) == "cleide-tt-1"
        assert get_comparison_state(sess) is None


def test_reset_zero_gemini_calls(web_client, monkeypatch):
    boot = _bootstrap_ready_comparison(web_client, table_count=2)
    calls = {"n": 0}

    def _boom(*_a, **_k):
        calls["n"] += 1
        raise AssertionError("Gemini não deve ser chamado no reset")

    monkeypatch.setattr(
        "app.run_cleiton_gemini_governance.cleiton_governed_generate_content",
        _boom,
        raising=False,
    )
    resp = web_client.post(
        "/api/agente-compara/comparison/reset",
        json={"comparison_id": boot["comparison_id"]},
    )
    assert resp.status_code == 200
    assert calls["n"] == 0
    status = web_client.get("/api/agente-compara/documents/status")
    assert status.status_code == 200
    assert calls["n"] == 0


def test_two_tabs_status_after_reset(web_client):
    boot = _bootstrap_ready_comparison(web_client, table_count=2)
    old_id = boot["comparison_id"]
    # Aba A reinicia
    assert web_client.post("/api/agente-compara/comparison/reset", json={"comparison_id": old_id}).status_code == 200
    # Aba B (mesma sessão) consulta status / tenta identidade antiga
    status = web_client.get(f"/api/agente-compara/documents/status?comparison_id={old_id}").get_json()
    assert status["comparison"] is None
    assert status["has_active_comparison"] is False
    # Acesso ao table_id antigo não reidrata
    status2 = web_client.get(
        f"/api/agente-compara/documents/status?comparison_id={old_id}&table_id={boot['table_ids'][0]}"
    ).get_json()
    assert status2["comparison"] is None


def test_slot_clear_demotes_from_configuration_ready(web_client):
    boot = _bootstrap_ready_comparison(web_client, table_count=2)
    table_2 = boot["table_ids"][1]
    resp = web_client.post(
        "/api/agente-compara/documents/clear",
        json={
            "comparison_id": boot["comparison_id"],
            "table_id": table_2,
            "slot": 2,
        },
    )
    assert resp.status_code == 200
    with web_client.session_transaction() as sess:
        state = get_comparison_state(sess)
        assert state is not None
        assert state["comparison_id"] == boot["comparison_id"]
        assert state["current_step"] == "PREPARE_TABLE_2"
        assert state.get("tax_config") is None
        entry2 = get_table_by_slot(state, 2)
        assert entry2["confirmed"] is False
        assert not entry2.get("temp_table_id")
        entry1 = get_table_by_slot(state, 1)
        assert entry1["confirmed"] is True


def test_reset_service_helper_without_state(app):
    app.config["SECRET_KEY"] = "test-secret"
    with app.test_request_context("/"):
        from flask import session

        session[AGENTE_COMPARA_TEMP_TABLE_ID_SESSION_KEY] = "orphan-tt"
        result = reset_comparison_for_session()
        assert result["comparison_reset"] is True
        assert result["comparison"] is None
        assert AGENTE_COMPARA_TEMP_TABLE_ID_SESSION_KEY not in session


def test_frontend_reset_contract_strings():
    js = pathlib.Path("app/static/js/agente_compara.js").read_text(encoding="utf-8")
    html = pathlib.Path("app/templates/agente_compara.html").read_text(encoding="utf-8")
    assert "API_COMPARISON_RESET" in js
    assert "/api/agente-compara/comparison/reset" in js
    assert "function resetAgenteComparaFrontendState" in js
    assert "function requestRestartComparison" in js
    assert "function executeComparisonReset" in js
    assert "comparisonRequestGeneration" in js
    assert "Reiniciar comparação" in html
    assert 'id="agenteComparaResetConfirmModal"' in html
    assert "Remover arquivos desta tabela" in html
    assert "Limpar documentos" not in html
    # Não limpa frontend antes do sucesso
    exec_block = js[js.index("function executeComparisonReset"): js.index("function requestRestartComparison")]
    assert exec_block.index("res.data.comparison_reset !== true") < exec_block.index("resetAgenteComparaFrontendState()")
    fetch_block = js[js.index("function fetchDocuments"): js.index("function refreshAttachmentsAfterChat")]
    assert "comparisonRequestGeneration" in fetch_block
    assert "generation !== comparisonRequestGeneration" in fetch_block
    assert "API_COMPARISON_START" in js or "/api/agente-compara/comparison/start" in js
    assert "function ensureComparisonStarted" in js


def _frontend_js() -> str:
    return pathlib.Path("app/static/js/agente_compara.js").read_text(encoding="utf-8")


def _teardown_block(js: str) -> str:
    return js[
        js.index("function teardownTempTableModal") : js.index(
            "function resetAgenteComparaFrontendState"
        )
    ]


def _reset_frontend_block(js: str) -> str:
    return js[
        js.index("function resetAgenteComparaFrontendState") : js.index(
            "function cacheReviewTempTableIfOwned"
        )
    ]


def test_teardown_temp_table_modal_clears_visual_shell_and_content():
    """Contrato estrutural: teardown destrói conteúdo visual do modal (sem jsdom)."""
    js = _frontend_js()
    assert "function teardownTempTableModal" in js
    teardown = _teardown_block(js)
    assert "hideTempTableModalShell()" in teardown
    assert "agenteComparaTempTableModalBody" in teardown
    assert "replaceChildren()" in teardown
    assert "agenteComparaTempTableModalTitle" in teardown
    assert "titleEl.textContent = ''" in teardown
    assert "agenteComparaTempTableModalSubtitle" in teardown
    assert "subtitleEl.replaceChildren()" in teardown
    assert "setTempTableModalError('')" in teardown
    assert "agenteComparaTempTableModalEdit" in teardown
    assert "agenteComparaTempTableModalSave" in teardown
    assert "agenteComparaTempTableModalClearSlot" in teardown
    assert "removeAttribute('aria-busy')" in teardown
    assert "agenteComparaCarrierIdentifyPanel" in teardown
    # Idempotente: não cria listeners nem assume nós existentes
    assert "addEventListener" not in teardown
    assert "if (body)" in teardown
    assert "if (titleEl)" in teardown
    assert "if (subtitleEl)" in teardown
    assert "if (modal)" in teardown


def test_reset_frontend_calls_teardown_not_only_shell_hide():
    js = _frontend_js()
    reset = _reset_frontend_block(js)
    assert "teardownTempTableModal()" in reset
    assert "hideTempTableModalShell()" not in reset
    assert "bumpComparisonRequestGeneration()" in reset
    assert "stopTempTablePolling()" in reset
    assert "clearPendingFreightTableUpload()" in reset
    assert "setCurrentTempTable(null)" in reset
    assert "clearLocalComparisonState()" in reset
    assert "resetConfigurationReviewState()" in reset
    assert "reviewLoadToken += 1" in reset
    # Ordem: invalidar geração antes do teardown; estado local limpo antes do teardown
    assert reset.index("bumpComparisonRequestGeneration()") < reset.index("teardownTempTableModal()")
    assert reset.index("setCurrentTempTable(null)") < reset.index("teardownTempTableModal()")
    assert reset.index("clearLocalComparisonState()") < reset.index("teardownTempTableModal()")
    assert reset.index("resetConfigurationReviewState()") < reset.index("teardownTempTableModal()")
    # Sem chamada backend adicional no reset frontend
    assert "fetch(" not in reset
    assert "API_COMPARISON_RESET" not in reset


def test_reset_frontend_clears_upload_inflight_and_review_caches():
    js = _frontend_js()
    reset = _reset_frontend_block(js)
    assert "uploadInFlight" in reset
    assert "setUploadLoading(false)" in reset or "uploadInFlight = false" in reset
    review_reset = js[
        js.index("function resetConfigurationReviewState") : js.index(
            "function bumpComparisonRequestGeneration"
        )
    ]
    assert "reviewTempTablesById = {}" in review_reset
    assert "reviewSharedTempTable = null" in review_reset
    assert "reviewLoadToken += 1" in review_reset
    assert "configurationReviewTab = null" in review_reset


def test_reset_then_status_empty_then_start_new_id(web_client):
    boot = _bootstrap_ready_comparison(web_client, table_count=2)
    old_id = boot["comparison_id"]
    web_client.post("/api/agente-compara/comparison/reset", json={"comparison_id": old_id})
    status = web_client.get("/api/agente-compara/documents/status").get_json()
    assert status["comparison"] is None
    assert status["has_active_comparison"] is False
    start = web_client.post("/api/agente-compara/comparison/start", json={}).get_json()
    assert start["ok"] is True
    assert start["comparison_started"] is True
    assert start["comparison"]["comparison_id"] != old_id
    assert start["comparison"]["current_step"] == STEP_PREPARE_TABLE_1
    tables = sorted(start["comparison"]["tables"], key=lambda t: t["slot_number"])
    assert tables[0]["slot_number"] == 1
    assert start["comparison"]["active_table_id"] == tables[0]["table_id"]
