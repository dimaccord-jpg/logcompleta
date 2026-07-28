"""Revisão navegável em CONFIGURATION_READY (tabs por transportadora + impostos + cidades + arquivo)."""
from __future__ import annotations

import importlib
import os
import pathlib
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.agente_compara_comparison_state import (
    AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY,
    STEP_ASK_TABLE_3,
    STEP_CONFIGURATION_READY,
    STEP_COVERAGE,
    STEP_TAXES,
    create_comparison,
    get_comparison_state,
    get_table_by_slot,
    public_comparison_summary,
    set_comparison_state,
    set_comparison_tax_config,
)
from app.agente_compara_doc_service import AGENTE_COMPARA_TEMP_TABLE_ID_SESSION_KEY
from app.services.agente_compara_config_service import (
    AgenteComparaConfig,
    DEFAULT_FALLBACK_MESSAGE,
)
from tests.cleiton_doc_fixtures import patch_cleiton_doc_cfg, patch_cleiton_doc_store


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


def _agente_compara_js() -> str:
    return pathlib.Path("app/static/js/agente_compara.js").read_text(encoding="utf-8")


def _sample_payload(*, title: str = "T1", region: str = "R1") -> dict:
    return {
        "status": "needs_review",
        "freight_tables": [
            {
                "name": title,
                "table_title": title,
                "columns": ["Região", "Valor"],
                "rows": [[region, "10"]],
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
            record = _coerce_temp_table_payload(payload, source_doc_ids=[])
    record[FIELD_COMPARISON_ID] = comparison_id
    record[FIELD_TABLE_ID] = table_id
    record[FIELD_SLOT_NUMBER] = slot_number
    record["temp_table_id"] = uuid4().hex
    record["status"] = record.get("status") or TEMP_TABLE_STATUS_NEEDS_REVIEW
    with web_client.application.app_context():
        _write_temp_table_atomic(_temp_table_path(record["temp_table_id"]), record)
    return record


def _attach_shared_coverage_and_audit(web_client, primary_temp_table_id: str) -> None:
    """Coverage e arquivo operacional ficam no record do primary_temp_table_id."""
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


def _bootstrap_ready_comparison(
    web_client,
    *,
    table_count: int = 2,
    carriers: list[str] | None = None,
    include_taxes: bool = True,
) -> dict:
    """Prepara comparação em CONFIGURATION_READY com temp tables distintas por slot."""
    if carriers is None:
        carriers = ["Intercargo", "Alfa", "Braspress"][:table_count]

    records: dict[int, dict] = {}
    table_ids: list[str] = []
    comparison_id = None

    with web_client.session_transaction() as sess:
        state = create_comparison(session_obj=sess)
        comparison_id = state["comparison_id"]
        if table_count >= 3 and get_table_by_slot(state, 3) is None:
            state["current_step"] = STEP_ASK_TABLE_3
            from app.agente_compara_comparison_state import add_third_table

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
            entry["doc_ids"] = []
            records[slot] = record
            table_ids.append(entry["table_id"])

        state["desired_table_count"] = table_count
        state["primary_temp_table_id"] = records[1]["temp_table_id"]
        state["active_table_id"] = table_ids[0]
        state["current_step"] = STEP_CONFIGURATION_READY
        tax_config = {
            "include_taxes": include_taxes,
            "origin_uf": "SP",
            "origin_city": "Campinas",
            "iss_rate": 5.0 if include_taxes else None,
            "selected_table_ids": table_ids[:1] if include_taxes else [],
            "destination_ufs": [{"uf": "RJ", "source": "manual", "evidence": []}] if include_taxes else [],
            "icms_rates": [
                {
                    "destination_uf": "RJ",
                    "applied_rate": 12.0,
                    "suggested_rate": 12.0,
                    "is_active": True,
                    "user_edited": False,
                }
            ]
            if include_taxes
            else [],
            "manual_added_ufs": [],
            "manual_removed_ufs": [],
            "confirmed": True,
        }
        set_comparison_tax_config(state, tax_config)
        set_comparison_state(state, session_obj=sess)
        sess[AGENTE_COMPARA_TEMP_TABLE_ID_SESSION_KEY] = records[1]["temp_table_id"]

    _attach_shared_coverage_and_audit(web_client, records[1]["temp_table_id"])

    return {
        "comparison_id": comparison_id,
        "table_ids": table_ids,
        "records": records,
        "carriers": carriers,
        "coverage_ok": True,
    }


# ---------------------------------------------------------------------------
# Frontend: contratos de revisão
# ---------------------------------------------------------------------------


def test_js_configuration_review_helpers_exist():
    js = _agente_compara_js()
    assert "function confirmedComparisonTablesForReview()" in js
    assert "function reviewTableTabId(tableId)" in js
    assert "function renderConfigurationReviewTabs(container)" in js
    assert "function selectConfigurationReviewTab(tabId, options)" in js
    assert "function loadReviewTempTable(tableId)" in js
    assert "function renderConfigurationReviewContent(panel)" in js
    assert "function resetConfigurationReviewState()" in js
    assert "configurationReviewTab" in js
    assert "reviewTempTablesById" in js
    assert "reviewLoadToken" in js


def test_js_configuration_ready_tabs_order_and_labels():
    js = _agente_compara_js()
    review_tabs = js[js.index("function renderConfigurationReviewTabs"): js.index("function loadReviewTempTable")]
    assert "Impostos" in review_tabs
    assert "Cidades Atendidas" in review_tabs
    assert "Arquivo para Comparação" in review_tabs
    assert "Tabela de frete" not in review_tabs
    assert "reviewCarrierLabel(tableMeta)" in review_tabs
    assert "confirmedComparisonTablesForReview()" in review_tabs
    assert "data-table-id" in review_tabs
    assert "data-comparison-id" in review_tabs
    # Ordem: carriers -> taxes -> coverage -> file
    assert review_tabs.index("confirmed.forEach") < review_tabs.index("'Impostos'")
    assert review_tabs.index("'Impostos'") < review_tabs.index("'Cidades Atendidas'")
    assert review_tabs.index("'Cidades Atendidas'") < review_tabs.index("'Arquivo para Comparação'")


def test_js_carrier_label_fallback_by_slot():
    js = _agente_compara_js()
    block = js[js.index("function reviewCarrierLabel"): js.index("function reviewTableTabId")]
    assert "Transportadora " in block
    assert "tableCarrierDisplay(tableMeta)" in block
    assert "slot_number" in block


def test_js_default_tab_is_first_confirmed_carrier():
    js = _agente_compara_js()
    block = js[js.index("function defaultConfigurationReviewTab"): js.index("function ensureConfigurationReviewDefaults")]
    assert "confirmedComparisonTablesForReview()" in block
    assert "reviewTableTabId(confirmed[0].table_id)" in block
    assert "comparison_file" in block


def test_js_ensure_review_defaults_can_force_or_preserve_tab():
    js = _agente_compara_js()
    block = js[js.index("function ensureConfigurationReviewDefaults"): js.index("function prepareConfigurationReviewRender")]
    assert "forceFirstCarrier" in block
    assert "configurationReviewTab = defaultConfigurationReviewTab()" in block
    activate = js[js.index("function activateComparisonCommonParamsStep"): js.index("function appendWizardConfirmedSummary")]
    ready_branch = activate[activate.index("step === 'CONFIGURATION_READY'"):]
    assert "forceFirstCarrier" in ready_branch
    open_block = js[js.index("function openTempTableModal"): js.index("function closeTempTableModal")]
    assert "forceFirstCarrier: true" in open_block


def test_js_activate_configuration_ready_opens_first_carrier_not_audit():
    js = _agente_compara_js()
    activate = js[js.index("function activateComparisonCommonParamsStep"): js.index("function appendWizardConfirmedSummary")]
    assert "CONFIGURATION_READY" in activate
    assert "ensureConfigurationReviewDefaults" in activate
    assert "selectConfigurationReviewTab" in activate
    # Não força mais aba audit ao entrar em CONFIGURATION_READY
    ready_branch = activate[activate.index("step === 'CONFIGURATION_READY'"):]
    assert "tempTableModalActiveTab = 'audit'" not in ready_branch


def test_js_lazy_load_uses_status_by_table_id_not_set_active():
    js = _agente_compara_js()
    load_block = js[js.index("function loadReviewTempTable"): js.index("function selectConfigurationReviewTab")]
    assert "API_STATUS" in load_block
    assert "table_id=" in load_block
    assert "comparison_id=" in load_block
    assert "API_COMPARISON_SET_ACTIVE" not in load_block
    assert "cleiton_governed_generate_content" not in load_block


def test_js_race_condition_guard_in_review_load():
    js = _agente_compara_js()
    load_block = js[js.index("function loadReviewTempTable"): js.index("function selectConfigurationReviewTab")]
    assert "reviewLoadToken" in load_block
    assert "stale" in load_block
    assert "tab_changed" in load_block
    select_block = js[js.index("function selectConfigurationReviewTab"): js.index("function renderTempTableModalTabs")]
    assert "parseReviewTableTabId(configurationReviewTab) !== tableId" in select_block
    # Loading discreto antes do fetch: não exibe dados da aba anterior como se fossem da nova.
    assert "reviewLoadInFlightTableId = tableId" in select_block
    assert select_block.index("reviewLoadInFlightTableId = tableId") < select_block.index("loadReviewTempTable(tableId)")


def test_js_review_mode_is_read_only():
    js = _agente_compara_js()
    tax_review = js[js.index("function renderConfigurationReviewTaxContent"): js.index("function renderConfigurationReviewAuditContent")]
    assert "saveGlobalTaxConfig" not in tax_review
    assert "markTaxConfigDirty" not in tax_review
    assert "Continuar para cidades" not in tax_review
    coverage_review = js[js.index("function renderConfigurationReviewContent"): js.index("function renderConfigurationReviewTabs")]
    assert "renderReadonlyCoverageTable" in coverage_review
    assert "renderEditableCoverageTable" not in coverage_review
    assert "renderCoverageUploadCard" not in coverage_review
    audit_review = js[js.index("function renderConfigurationReviewAuditContent"): js.index("function renderConfigurationReviewCarrierContent")]
    assert "Iniciar Auditoria" not in audit_review
    assert "Enviar arquivo preenchido" not in audit_review
    assert "Arquivo para Comparação" in audit_review


def test_js_no_visual_arquivo_para_auditoria_in_bid():
    js = _agente_compara_js()
    assert "Arquivo para Comparação" in js
    # Texto visual antigo não deve aparecer no domínio BID.
    assert "Arquivo para auditoria" not in js
    assert "Arquivo recebido para auditoria" not in js


def test_js_footer_hides_actions_in_configuration_ready():
    js = _agente_compara_js()
    footer = js[js.index("function updateTempTableModalFooter()"): js.index("function canEditFreightTables")]
    ready = footer[footer.index("isComparisonConfigurationReady()"): footer.index("if (wizardNonReview)")]
    assert "editBtn.hidden = true" in ready
    assert "saveBtn.hidden = true" in ready
    assert "taxSaveBtn.hidden = true" in ready
    assert "startAuditBtn.hidden = true" in ready


def test_js_review_cache_invalidates_on_comparison_change():
    js = _agente_compara_js()
    sync = js[js.index("function syncComparisonStateFromPayload"): js.index("function activeComparisonTable")]
    assert "resetConfigurationReviewState()" in sync
    assert "reviewComparisonId" in sync


def test_js_review_cache_seeds_from_owned_temp_table():
    js = _agente_compara_js()
    assert "function cacheReviewTempTableIfOwned(tempTable)" in js
    capture = js[js.index("function captureReviewSharedTempTable"): js.index("function defaultConfigurationReviewTab")]
    assert "cacheReviewTempTableIfOwned(tempTable)" in capture


# ---------------------------------------------------------------------------
# Backend: tax_config + ownership + refresh
# ---------------------------------------------------------------------------


def test_public_summary_exposes_tax_config_in_configuration_ready():
    state = {
        "comparison_id": "cmp-1",
        "status": "configuration_ready",
        "current_step": STEP_CONFIGURATION_READY,
        "active_table_id": "t1",
        "desired_table_count": 2,
        "primary_temp_table_id": "tt1",
        "tables": {
            "t1": {
                "table_id": "t1",
                "slot_number": 1,
                "status": "confirmed",
                "confirmed": True,
                "carrier_name": "Intercargo",
                "temp_table_id": "tt1",
                "doc_ids": [],
            },
            "t2": {
                "table_id": "t2",
                "slot_number": 2,
                "status": "confirmed",
                "confirmed": True,
                "carrier_name": "Alfa",
                "temp_table_id": "tt2",
                "doc_ids": [],
            },
        },
        "tax_config": {
            "include_taxes": True,
            "origin_uf": "SP",
            "origin_city": "Campinas",
            "iss_rate": 5.0,
            "selected_table_ids": ["t1"],
            "destination_ufs": [],
            "icms_rates": [],
            "confirmed": True,
        },
    }
    payload = public_comparison_summary(state)
    assert payload["current_step"] == STEP_CONFIGURATION_READY
    assert payload["tax_config"]["origin_uf"] == "SP"
    assert payload["tax_config"]["iss_rate"] == 5.0
    assert payload["tax_config"]["selected_table_ids"] == ["t1"]
    assert "can_advance_to_coverage" not in payload


def test_public_summary_keeps_tax_config_in_taxes_step():
    state = {
        "comparison_id": "cmp-1",
        "status": "preparing",
        "current_step": STEP_TAXES,
        "active_table_id": "t1",
        "desired_table_count": 2,
        "primary_temp_table_id": None,
        "tables": {},
        "tax_config": {"include_taxes": False, "confirmed": True},
    }
    payload = public_comparison_summary(state)
    assert payload["tax_config"]["include_taxes"] is False
    assert "can_advance_to_coverage" in payload


def test_public_summary_hides_tax_config_in_coverage_step():
    state = {
        "comparison_id": "cmp-1",
        "status": "preparing",
        "current_step": STEP_COVERAGE,
        "active_table_id": "t1",
        "desired_table_count": 2,
        "primary_temp_table_id": "tt1",
        "tables": {},
        "tax_config": {"include_taxes": False, "confirmed": True},
    }
    payload = public_comparison_summary(state)
    assert "tax_config" not in payload


def test_status_configuration_ready_exposes_tax_config_and_tables(web_client, monkeypatch):
    import app.run_agente_compara_temp_table as temp_mod

    gemini_mock = MagicMock(side_effect=AssertionError("Gemini não deve ser chamado"))
    monkeypatch.setattr(temp_mod, "cleiton_governed_generate_content", gemini_mock)
    monkeypatch.setattr(temp_mod, "run_agente_compara_temp_table_extraction", gemini_mock)

    boot = _bootstrap_ready_comparison(web_client, table_count=2, carriers=["Intercargo", "Alfa"])
    resp = web_client.get(
        f"/api/agente-compara/documents/status?comparison_id={boot['comparison_id']}"
    )
    assert resp.status_code == 200
    body = resp.get_json()
    comparison = body["comparison"]
    assert comparison["current_step"] == STEP_CONFIGURATION_READY
    assert comparison["tax_config"]["confirmed"] is True
    assert comparison["tax_config"]["origin_uf"] == "SP"
    tables = sorted(comparison["tables"], key=lambda t: t["slot_number"])
    assert [t["carrier_name"] for t in tables if t.get("confirmed")] == ["Intercargo", "Alfa"]
    assert body["temp_table"] is not None
    assert body["temp_table"].get("coverage_table") or boot["coverage_ok"]
    assert body["temp_table"]["audit_batch"]["status"] == "uploaded"
    assert gemini_mock.call_count == 0


def test_status_by_table_id_loads_correct_temp_table_without_step_change(web_client, monkeypatch):
    import app.run_agente_compara_temp_table as temp_mod

    gemini_mock = MagicMock(side_effect=AssertionError("Gemini não deve ser chamado"))
    monkeypatch.setattr(temp_mod, "cleiton_governed_generate_content", gemini_mock)

    boot = _bootstrap_ready_comparison(web_client, table_count=2, carriers=["Intercargo", "Alfa"])
    table_1, table_2 = boot["table_ids"]

    resp1 = web_client.get(
        f"/api/agente-compara/documents/status?comparison_id={boot['comparison_id']}&table_id={table_1}"
    )
    resp2 = web_client.get(
        f"/api/agente-compara/documents/status?comparison_id={boot['comparison_id']}&table_id={table_2}"
    )
    assert resp1.status_code == 200
    assert resp2.status_code == 200
    body1 = resp1.get_json()
    body2 = resp2.get_json()
    assert body1["comparison"]["current_step"] == STEP_CONFIGURATION_READY
    assert body2["comparison"]["current_step"] == STEP_CONFIGURATION_READY
    assert body1["temp_table"]["temp_table_id"] == boot["records"][1]["temp_table_id"]
    assert body2["temp_table"]["temp_table_id"] == boot["records"][2]["temp_table_id"]
    assert body1["temp_table"]["temp_table_id"] != body2["temp_table"]["temp_table_id"]
    # Isolamento: títulos/linhas não se misturam
    ft1 = body1["temp_table"]["freight_tables"][0]
    ft2 = body2["temp_table"]["freight_tables"][0]
    assert (ft1.get("table_title") or ft1.get("name")) == "Tabela 1"
    assert (ft2.get("table_title") or ft2.get("name")) == "Tabela 2"
    rows1 = ft1.get("rows") or []
    rows2 = ft2.get("rows") or []
    assert rows1 and rows2
    cell1 = rows1[0][0] if isinstance(rows1[0], (list, tuple)) else rows1[0]
    cell2 = rows2[0][0] if isinstance(rows2[0], (list, tuple)) else rows2[0]
    assert "R1" in str(cell1)
    assert "R2" in str(cell2)
    assert gemini_mock.call_count == 0


def test_three_tables_status_exposes_all_confirmed_slots(web_client, monkeypatch):
    import app.run_agente_compara_temp_table as temp_mod

    gemini_mock = MagicMock(side_effect=AssertionError("Gemini não deve ser chamado"))
    monkeypatch.setattr(temp_mod, "cleiton_governed_generate_content", gemini_mock)

    boot = _bootstrap_ready_comparison(
        web_client,
        table_count=3,
        carriers=["Zulu", "Alfa", "Braspress"],
    )
    resp = web_client.get(
        f"/api/agente-compara/documents/status?comparison_id={boot['comparison_id']}"
    )
    assert resp.status_code == 200
    tables = sorted(resp.get_json()["comparison"]["tables"], key=lambda t: t["slot_number"])
    confirmed = [t for t in tables if t.get("confirmed")]
    assert [t["carrier_name"] for t in confirmed] == ["Zulu", "Alfa", "Braspress"]
    assert [t["slot_number"] for t in confirmed] == [1, 2, 3]
    assert gemini_mock.call_count == 0


def test_unconfirmed_slot_not_exposed_as_ready_carrier(web_client):
    boot = _bootstrap_ready_comparison(web_client, table_count=2, carriers=["Intercargo", "Alfa"])
    with web_client.session_transaction() as sess:
        state = get_comparison_state(sess)
        t2 = get_table_by_slot(state, 2)
        t2["confirmed"] = False
        t2["status"] = "needs_review"
        sess[AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = state

    resp = web_client.get(
        f"/api/agente-compara/documents/status?comparison_id={boot['comparison_id']}"
    )
    confirmed = [t for t in resp.get_json()["comparison"]["tables"] if t.get("confirmed")]
    assert len(confirmed) == 1
    assert confirmed[0]["carrier_name"] == "Intercargo"


def test_ownership_blocks_foreign_table_id(web_client):
    boot = _bootstrap_ready_comparison(web_client, table_count=2)
    resp = web_client.get(
        f"/api/agente-compara/documents/status?comparison_id={boot['comparison_id']}&table_id=foreign-table"
    )
    assert resp.status_code in {400, 403, 404, 409}
    body = resp.get_json()
    assert body["ok"] is False


def test_ownership_blocks_foreign_comparison_id(web_client):
    boot = _bootstrap_ready_comparison(web_client, table_count=2)
    resp = web_client.get(
        f"/api/agente-compara/documents/status?comparison_id=other-cmp&table_id={boot['table_ids'][0]}"
    )
    assert resp.status_code in {400, 403, 404, 409}
    assert resp.get_json()["ok"] is False


def test_refresh_preserves_configuration_ready_artifacts(web_client, monkeypatch):
    import app.run_agente_compara_temp_table as temp_mod

    gemini_mock = MagicMock(side_effect=AssertionError("Gemini não deve ser chamado no refresh"))
    monkeypatch.setattr(temp_mod, "cleiton_governed_generate_content", gemini_mock)
    monkeypatch.setattr(temp_mod, "run_agente_compara_temp_table_extraction", gemini_mock)

    boot = _bootstrap_ready_comparison(web_client, table_count=2, carriers=["Intercargo", "Alfa"])
    first = web_client.get(
        f"/api/agente-compara/documents/status?comparison_id={boot['comparison_id']}"
    ).get_json()
    second = web_client.get(
        f"/api/agente-compara/documents/status?comparison_id={boot['comparison_id']}"
    ).get_json()

    assert first["comparison"]["current_step"] == STEP_CONFIGURATION_READY
    assert second["comparison"]["current_step"] == STEP_CONFIGURATION_READY
    assert second["comparison"]["tax_config"]["origin_uf"] == "SP"
    assert second["temp_table"]["audit_batch"]["source_file_name"]
    assert len([t for t in second["comparison"]["tables"] if t.get("confirmed")]) == 2
    assert gemini_mock.call_count == 0


def test_empty_carrier_name_still_returns_slot_in_summary(web_client):
    boot = _bootstrap_ready_comparison(web_client, table_count=2, carriers=["Intercargo", "Alfa"])
    with web_client.session_transaction() as sess:
        state = get_comparison_state(sess)
        get_table_by_slot(state, 2)["carrier_name"] = ""
        sess[AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = state

    resp = web_client.get(
        f"/api/agente-compara/documents/status?comparison_id={boot['comparison_id']}"
    )
    tables = sorted(resp.get_json()["comparison"]["tables"], key=lambda t: t["slot_number"])
    assert tables[0]["carrier_name"] == "Intercargo"
    assert tables[1]["carrier_name"] in ("", None)
    assert tables[1]["slot_number"] == 2


def test_duplicate_carrier_names_remain_distinct_by_table_id(web_client):
    boot = _bootstrap_ready_comparison(web_client, table_count=2, carriers=["Mesma", "Mesma"])
    tables = sorted(
        web_client.get(
            f"/api/agente-compara/documents/status?comparison_id={boot['comparison_id']}"
        ).get_json()["comparison"]["tables"],
        key=lambda t: t["slot_number"],
    )
    assert tables[0]["carrier_name"] == tables[1]["carrier_name"] == "Mesma"
    assert tables[0]["table_id"] != tables[1]["table_id"]


# ---------------------------------------------------------------------------
# Etapa 2: resumo visual do arquivo + botão Processar Cálculos (disabled)
# ---------------------------------------------------------------------------


def test_js_calculation_file_summary_helpers_and_ids():
    js = _agente_compara_js()
    assert "function getCalculationFileMetadata(tempTable)" in js
    assert "function renderCalculationFileSummary(container, tempTable, options)" in js
    assert "function clearCalculationFileSummary(container)" in js
    assert "function setProcessCalculationsButtonState(button)" in js
    assert "function calculationFileStatusLabel(status)" in js
    assert "function shouldShowProcessCalculationsButton(tempTable)" in js

    summary_fn = js[
        js.index("function renderCalculationFileSummary") : js.index(
            "function shouldShowAuditTab"
        )
    ]
    assert "agenteComparaCalculationFileSummary" in summary_fn
    assert "agenteComparaCalculationFileName" in summary_fn
    assert "agenteComparaCalculationFileRows" in summary_fn
    assert "agenteComparaCalculationFileLimit" in summary_fn
    assert "agenteComparaCalculationFileStatus" in summary_fn
    assert "agenteComparaProcessCalculationsButton" in summary_fn
    assert "Arquivo recebido para comparação" in summary_fn
    assert "Processar Cálculos" in summary_fn
    # Sem listener inline no renderer do resumo (binding dedicado fora do bloco de markup).
    assert "fetch(" not in summary_fn
    assert "runAuditProcessing" not in summary_fn
    assert "function processComparisonCalculations" in js
    assert "comparison/calculate" in js
    assert "Arquivo recebido para auditoria" not in js
    assert "Processar auditoria" not in js
    assert "Iniciar auditoria" not in js


def test_js_summary_hidden_before_upload_and_shown_after_batch():
    js = _agente_compara_js()
    upload_tab = js[
        js.index("function renderAuditFileTabContent") : js.index(
            "function setAuditUploadFileName"
        )
    ]
    assert "if (hasAuditBatch(tempTable))" in upload_tab
    assert "renderCalculationFileSummary(card, tempTable" in upload_tab
    review = js[
        js.index("function renderConfigurationReviewAuditContent") : js.index(
            "function renderConfigurationReviewCarrierContent"
        )
    ]
    assert "if (hasAuditBatch(tempTable))" in review
    assert "renderCalculationFileSummary(card, tempTable" in review
    assert "Nenhum arquivo operacional disponível para revisão." in review


def test_js_upload_success_opens_file_summary_tab_without_calculation():
    js = _agente_compara_js()
    upload_fn = js[
        js.index("function uploadAuditFile(file)") : js.index(
            "function ensureCoverageTableShell"
        )
    ]
    assert "selectConfigurationReviewTab('comparison_file'" in upload_fn
    assert "activateComparisonCommonParamsStep('CONFIGURATION_READY')" in upload_fn
    assert "runAuditProcessing()" not in upload_fn
    assert "API_AUDIT_RUN" not in upload_fn
    assert "comparison/calculate" not in upload_fn
    assert "compute_audit_outputs" not in upload_fn


def test_js_process_calculations_button_enabled_contract():
    js = _agente_compara_js()
    btn_state = js[
        js.index("function setProcessCalculationsButtonState") : js.index(
            "function bindProcessCalculationsButton"
        )
    ]
    assert "aria-disabled" in btn_state
    assert "aria-busy" in btn_state
    assert "canEnableProcessCalculationsButton" in js
    assert "bindProcessCalculationsButton" in js
    assert "function processComparisonCalculations" in js

    show_btn = js[
        js.index("function shouldShowProcessCalculationsButton") : js.index(
            "function canEnableProcessCalculationsButton"
        )
    ]
    assert "isComparisonPostConfigStep()" in show_btn
    assert "confirmed.length < 2" in show_btn
    assert "meta.status === 'uploaded'" in show_btn or "meta.status === 'processed'" in show_btn


def test_js_file_replacement_clears_previous_summary_card():
    js = _agente_compara_js()
    clear_fn = js[
        js.index("function clearCalculationFileSummary") : js.index(
            "function appendCalculationFileDetailRow"
        )
    ]
    render_fn = js[
        js.index("function renderCalculationFileSummary") : js.index(
            "function shouldShowAuditTab"
        )
    ]
    assert "querySelector('#agenteComparaCalculationFileSummary')" in clear_fn
    assert "existing.remove()" in clear_fn
    assert "clearCalculationFileSummary(container)" in render_fn
    # Um único card/botão por render.
    assert render_fn.count("agenteComparaCalculationFileSummary") >= 1
    assert render_fn.count("agenteComparaProcessCalculationsButton") == 1


def test_js_upload_error_path_does_not_render_success_summary():
    js = _agente_compara_js()
    upload_fn = js[
        js.index("function uploadAuditFile(file)") : js.index(
            "function ensureCoverageTableShell"
        )
    ]
    error_branch = upload_fn[
        upload_fn.index("if (!res.data || res.data.ok !== true)") : upload_fn.index(
            "return fetchDocuments()"
        )
    ]
    assert "setAuditUploadStatus" in error_branch
    assert "'error'" in error_branch
    assert "renderCalculationFileSummary" not in error_branch
    assert "selectConfigurationReviewTab('comparison_file'" not in error_branch
    assert "auditUploadInFlight = false" in upload_fn


def test_refresh_restores_calculation_file_summary_metadata(web_client, monkeypatch):
    import app.run_agente_compara_temp_table as temp_mod
    from app.agente_compara_doc_service import (
        load_temp_table_record,
        _temp_table_path,
        _write_temp_table_atomic,
    )

    gemini_mock = MagicMock(side_effect=AssertionError("Gemini não deve ser chamado"))
    run_mock = MagicMock(side_effect=AssertionError("cálculo não deve rodar"))
    monkeypatch.setattr(temp_mod, "cleiton_governed_generate_content", gemini_mock)
    monkeypatch.setattr("app.agente_compara_doc_service.run_audit_batch_for_session", run_mock)
    monkeypatch.setattr(
        "app.agente_compara_doc_service.compute_audit_outputs",
        MagicMock(side_effect=AssertionError("compute_audit_outputs não deve rodar")),
    )

    boot = _bootstrap_ready_comparison(web_client, table_count=2, carriers=["Intercargo", "Alfa"])
    primary_id = boot["records"][1]["temp_table_id"]
    with web_client.application.app_context():
        record = load_temp_table_record(primary_id, ttl_hours=24)
        assert record is not None
        record["audit_batch"]["source_file_name"] = "arquivo.xlsx"
        record["audit_batch"]["row_count"] = 285
        record["audit_batch"]["max_rows"] = 2000
        record["audit_batch"]["status"] = "uploaded"
        record["audit_batch"]["results"] = []
        record["audit_batch"]["summary"] = None
        _write_temp_table_atomic(_temp_table_path(primary_id), record)

    body = web_client.get(
        f"/api/agente-compara/documents/status?comparison_id={boot['comparison_id']}"
    ).get_json()
    assert body["ok"] is True
    assert body["comparison"]["current_step"] == STEP_CONFIGURATION_READY
    batch = body["temp_table"]["audit_batch"]
    assert batch["status"] == "uploaded"
    assert batch["source_file_name"] == "arquivo.xlsx"
    assert batch["row_count"] == 285
    assert batch["max_rows"] == 2000
    assert batch.get("results") in (None, [])
    assert batch.get("summary") is None
    assert gemini_mock.call_count == 0
    run_mock.assert_not_called()

    js = _agente_compara_js()
    meta_fn = js[
        js.index("function getCalculationFileMetadata") : js.index(
            "function shouldShowProcessCalculationsButton"
        )
    ]
    assert "batch.source_file_name" in meta_fn
    assert "batch.row_count" in meta_fn
    assert "batch.max_rows" in meta_fn
    assert "calculationFileStatusLabel(status)" in meta_fn
    assert "Arquivo recebido para comparação" in js[
        js.index("function calculationFileStatusLabel") : js.index(
            "function getCalculationFileMetadata"
        )
    ]


def test_status_after_upload_exposes_summary_fields_without_calculation(web_client, monkeypatch):
    """Upload válido devolve metadados do resumo; nenhum cálculo/Gemini."""
    import io

    from openpyxl import Workbook

    from app.agente_compara_comparison_state import STEP_CALCULATION_FILE
    from app.agente_compara_doc_service import AUDIT_BATCH_SHEET_NAME
    from tests.test_agente_compara_comparison_file_contract import (
        NEW_CONTRACT_HEADERS,
        _sample_row,
    )
    from tests.test_agente_compara_comparison_journey import _set_comparison_at_step

    run_mock = MagicMock(side_effect=AssertionError("cálculo não deve rodar"))
    gemini_mock = MagicMock(side_effect=AssertionError("Gemini não deve ser chamado"))
    monkeypatch.setattr("app.agente_compara_doc_service.run_audit_batch_for_session", run_mock)
    monkeypatch.setattr(
        "app.run_agente_compara_temp_table.cleiton_governed_generate_content",
        gemini_mock,
    )
    monkeypatch.setattr(
        "app.agente_compara_doc_service.compute_audit_outputs",
        MagicMock(side_effect=AssertionError("compute_audit_outputs não deve rodar")),
    )

    _set_comparison_at_step(web_client, STEP_CALCULATION_FILE)
    wb = Workbook()
    ws = wb.active
    ws.title = AUDIT_BATCH_SHEET_NAME
    ws.append(NEW_CONTRACT_HEADERS)
    ws.append(_sample_row())
    ws.append(_sample_row())
    buf = io.BytesIO()
    wb.save(buf)

    resp = web_client.post(
        "/api/agente-compara/audit/upload",
        data={"file": (io.BytesIO(buf.getvalue()), "arquivo_b.xlsx")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    batch = body["temp_table"]["audit_batch"]
    assert batch["status"] == "uploaded"
    assert batch["source_file_name"] == "arquivo_b.xlsx"
    assert batch["row_count"] == 2
    assert batch["max_rows"] == 2000
    assert body["temp_table"]["comparison"]["current_step"] == STEP_CONFIGURATION_READY
    assert not batch.get("results")
    assert batch.get("summary") is None
    run_mock.assert_not_called()
    assert gemini_mock.call_count == 0


def test_file_substitution_updates_summary_metadata(web_client, monkeypatch):
    import io

    from openpyxl import Workbook

    from app.agente_compara_comparison_state import STEP_CALCULATION_FILE
    from app.agente_compara_doc_service import AUDIT_BATCH_SHEET_NAME
    from tests.test_agente_compara_comparison_file_contract import (
        NEW_CONTRACT_HEADERS,
        _sample_row,
    )
    from tests.test_agente_compara_comparison_journey import _set_comparison_at_step

    monkeypatch.setattr(
        "app.agente_compara_doc_service.run_audit_batch_for_session",
        MagicMock(side_effect=AssertionError("cálculo não deve rodar")),
    )

    def _xlsx(rows: int, name: str) -> tuple[io.BytesIO, str]:
        wb = Workbook()
        ws = wb.active
        ws.title = AUDIT_BATCH_SHEET_NAME
        ws.append(NEW_CONTRACT_HEADERS)
        for _ in range(rows):
            ws.append(_sample_row())
        buf = io.BytesIO()
        wb.save(buf)
        return io.BytesIO(buf.getvalue()), name

    _set_comparison_at_step(web_client, STEP_CALCULATION_FILE)
    resp1 = web_client.post(
        "/api/agente-compara/audit/upload",
        data={"file": _xlsx(1, "arquivo_a.xlsx")},
        content_type="multipart/form-data",
    )
    assert resp1.status_code == 200
    batch1 = resp1.get_json()["temp_table"]["audit_batch"]
    assert batch1["source_file_name"] == "arquivo_a.xlsx"
    assert batch1["row_count"] == 1

    # Contrato visual: um único card/botão no JS (re-render limpa o anterior).
    js = _agente_compara_js()
    assert "clearCalculationFileSummary(container)" in js
    assert js.count("id = 'agenteComparaCalculationFileSummary'") == 1
    assert js.count("id = 'agenteComparaProcessCalculationsButton'") == 1

    # Reentrada em CALCULATION_FILE permite substituir o arquivo operacional.
    with web_client.session_transaction() as sess:
        state = get_comparison_state(sess)
        state["current_step"] = STEP_CALCULATION_FILE
        sess[AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = state

    resp2 = web_client.post(
        "/api/agente-compara/audit/upload",
        data={"file": _xlsx(2, "arquivo_b.xlsx")},
        content_type="multipart/form-data",
    )
    assert resp2.status_code == 200, resp2.get_json()
    batch2 = resp2.get_json()["temp_table"]["audit_batch"]
    assert batch2["source_file_name"] == "arquivo_b.xlsx"
    assert batch2["row_count"] == 2
    assert batch1["source_file_name"] != batch2["source_file_name"]


def test_upload_contract_error_keeps_summary_absent(web_client, monkeypatch):
    import io

    from app.agente_compara_comparison_state import STEP_CALCULATION_FILE
    from tests.cleiton_doc_fixtures import make_csv
    from tests.test_agente_compara_comparison_file_contract import (
        NEW_CONTRACT_HEADERS,
        _sample_row,
    )
    from tests.test_agente_compara_comparison_journey import _set_comparison_at_step

    monkeypatch.setattr(
        "app.agente_compara_doc_service.run_audit_batch_for_session",
        MagicMock(side_effect=AssertionError("cálculo não deve rodar")),
    )
    _set_comparison_at_step(web_client, STEP_CALCULATION_FILE)
    headers = [h for h in NEW_CONTRACT_HEADERS if h != "peso"]
    row = [v for h, v in zip(NEW_CONTRACT_HEADERS, _sample_row()) if h != "peso"]
    resp = web_client.post(
        "/api/agente-compara/audit/upload",
        data={"file": (io.BytesIO(make_csv([headers, row])), "sem_peso.csv", "text/csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code >= 400 or resp.get_json().get("ok") is False
    body = resp.get_json()
    assert body.get("ok") is False
    # Sem sucesso: não há audit_batch uploaded no payload de erro.
    temp = body.get("temp_table") or {}
    batch = temp.get("audit_batch") if isinstance(temp, dict) else None
    if batch:
        assert batch.get("status") != "uploaded" or batch.get("source_file_name") != "sem_peso.csv"
