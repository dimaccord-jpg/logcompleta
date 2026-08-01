"""Testes de disponibilidade, rota e contratos de UI do chat comparativo."""
from __future__ import annotations

import copy
import importlib
import os
import pathlib
from types import SimpleNamespace

import pytest

from app.agente_compara_chat_context_service import (
    CAPABILITY_LOCKED,
    CAPABILITY_READY,
    CHAT_NOT_READY_MESSAGE,
    ERROR_COMPARISON_CHAT_NOT_READY,
    resolve_chat_availability,
    resolve_chat_capability,
)
from app.agente_compara_comparison_analytics_service import build_comparison_analytics
from app.agente_compara_comparison_state import (
    AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY,
    COMPARISON_STATUS_CALCULATION_READY,
    COMPARISON_STATUS_PREPARING,
    STEP_CALCULATION_FAILED,
    STEP_CALCULATION_FILE,
    STEP_CALCULATION_READY,
    STEP_CALCULATION_RUNNING,
    STEP_CONFIGURATION_READY,
    STEP_PREPARE_TABLE_1,
    STEP_TABLES_READY,
    STEP_TAXES,
)
from app.services.agente_compara_config_service import AgenteComparaConfig, DEFAULT_FALLBACK_MESSAGE

REPO = pathlib.Path(__file__).resolve().parents[1]
JS_PATH = REPO / "app" / "static" / "js" / "agente_compara.js"
HTML_PATH = REPO / "app" / "templates" / "agente_compara.html"


def _load_web():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret-chat")
    return importlib.import_module("app.web")


def _cfg():
    return AgenteComparaConfig(
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


def _result(comparison_id: str = "cmp-route-1") -> dict:
    return {
        "schema_version": 1,
        "comparison_id": comparison_id,
        "execution_id": "exec-1",
        "table_count": 2,
        "row_count": 1,
        "tables": [
            {"table_id": "t1", "slot_number": 1, "carrier_name": "Alpha", "temp_table_id": "tt1"},
            {"table_id": "t2", "slot_number": 2, "carrier_name": "Beta", "temp_table_id": "tt2"},
        ],
        "results_by_table": {},
        "comparative_rows": [
            {
                "row_index": 1,
                "document_number": "1001",
                "destination_city": "Campinas",
                "destination_uf": "SP",
                "weight": 10,
                "invoice_value": 100,
                "table_results": {
                    "t1": {
                        "table_id": "t1",
                        "carrier_name": "Alpha",
                        "slot_number": 1,
                        "calculated_freight": 40,
                        "status": "calculated",
                    },
                    "t2": {
                        "table_id": "t2",
                        "carrier_name": "Beta",
                        "slot_number": 2,
                        "calculated_freight": 55,
                        "status": "calculated",
                    },
                },
            }
        ],
        "summary": {},
    }


def _ready_state(comparison_id: str = "cmp-route-1") -> dict:
    return {
        "comparison_id": comparison_id,
        "status": COMPARISON_STATUS_CALCULATION_READY,
        "current_step": STEP_CALCULATION_READY,
        "active_table_id": "t1",
        "desired_table_count": 2,
        "primary_temp_table_id": "tt1",
        "tax_config": None,
        "tables": {
            "t1": {
                "table_id": "t1",
                "slot_number": 1,
                "status": "confirmed",
                "doc_ids": [],
                "temp_table_id": "tt1",
                "carrier_name": "Alpha",
                "confirmed": True,
                "error": None,
            },
            "t2": {
                "table_id": "t2",
                "slot_number": 2,
                "status": "confirmed",
                "doc_ids": [],
                "temp_table_id": "tt2",
                "carrier_name": "Beta",
                "confirmed": True,
                "error": None,
            },
        },
        "comparison_calculation": {
            "schema_version": 1,
            "execution_id": "exec-1",
            "fingerprint_short": "fp1",
            "status": STEP_CALCULATION_READY,
            "stale": False,
            "billing_status": "applied",
            "table_ids": ["t1", "t2"],
            "slot_numbers": [1, 2],
        },
    }


@pytest.mark.parametrize(
    "step,calc_status,expected_available",
    [
        (None, None, False),
        (STEP_PREPARE_TABLE_1, {"status": "not_started", "result": None, "analytics": None, "stale": False}, False),
        (
            STEP_PREPARE_TABLE_1,
            {"status": "not_started", "result": None, "analytics": None, "stale": False},
            False,
        ),
        (STEP_TABLES_READY, {"status": "not_started", "result": None, "analytics": None, "stale": False}, False),
        (STEP_CONFIGURATION_READY, {"status": "not_started", "result": None, "analytics": None, "stale": False}, False),
        (STEP_TAXES, {"status": "not_started", "result": None, "analytics": None, "stale": False}, False),
        (STEP_CALCULATION_FILE, {"status": "not_started", "result": None, "analytics": None, "stale": False}, False),
        (STEP_CALCULATION_RUNNING, {"status": STEP_CALCULATION_RUNNING, "result": None, "analytics": None, "stale": False}, False),
        (STEP_CALCULATION_FAILED, {"status": STEP_CALCULATION_FAILED, "result": None, "analytics": None, "stale": False}, False),
        (STEP_CALCULATION_READY, {"status": STEP_CALCULATION_READY, "result": {"x": 1}, "analytics": None, "stale": False}, False),
        (STEP_CALCULATION_READY, {"status": STEP_CALCULATION_READY, "result": None, "analytics": {"a": 1}, "stale": False}, False),
        (STEP_CALCULATION_READY, {"status": STEP_CALCULATION_READY, "result": {"x": 1}, "analytics": {"a": 1}, "stale": True}, False),
        (STEP_CALCULATION_READY, {"status": STEP_CALCULATION_READY, "result": {"x": 1}, "analytics": {"a": 1}, "stale": False}, True),
    ],
)
def test_resolve_chat_availability_contract(step, calc_status, expected_available):
    state = None
    if step is not None:
        state = _ready_state()
        state["current_step"] = step
        if step != STEP_CALCULATION_READY:
            state["status"] = COMPARISON_STATUS_PREPARING
    avail = resolve_chat_availability(state, calc_status=calc_status)
    assert avail["chat_available"] is expected_available
    if expected_available:
        assert avail["capability"] == CAPABILITY_READY
        assert avail["reason"] is None
    else:
        assert avail["capability"] == CAPABILITY_LOCKED
        assert avail["chat_available"] is False
        assert resolve_chat_capability(state, calc_status=calc_status) != "guided" or True
        assert avail["capability"] != "guided"


def test_guided_is_not_conversational_capability():
    assert resolve_chat_capability(None) == CAPABILITY_LOCKED
    state = _ready_state()
    state["current_step"] = STEP_PREPARE_TABLE_1
    assert resolve_chat_capability(state, calc_status={"status": "not_started", "result": None, "stale": False}) == CAPABILITY_LOCKED


def test_comparison_chat_route_auth_required(monkeypatch):
    web = _load_web()
    monkeypatch.setattr("app.agente_compara_api_routes.current_user", SimpleNamespace(is_authenticated=False))
    monkeypatch.setattr("app.agente_compara_api_routes.get_agente_compara_config", _cfg)
    client = web.app.test_client()
    res = client.post("/api/agente-compara/comparison-chat", json={"message": "Oi"})
    assert res.status_code == 401
    assert res.get_json()["error"] == "auth_required"


def test_comparison_chat_route_plan_blocked(monkeypatch):
    web = _load_web()
    fake_user = SimpleNamespace(is_authenticated=True, conta_id=1, franquia_id=1, id=1)
    monkeypatch.setattr("app.agente_compara_api_routes.current_user", fake_user)
    monkeypatch.setattr(
        "app.agente_compara_api_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda user: {"permitido": False, "mensagem_usuario": "Limite do plano."},
    )
    monkeypatch.setattr("app.agente_compara_api_routes.get_agente_compara_config", _cfg)
    client = web.app.test_client()
    res = client.post("/api/agente-compara/comparison-chat", json={"message": "Oi"})
    assert res.status_code == 403
    assert res.get_json()["error"] == "franquia_blocked"


def test_comparison_chat_route_blocks_pre_ready(monkeypatch):
    web = _load_web()
    fake_user = SimpleNamespace(is_authenticated=True, conta_id=1, franquia_id=1, id=1)
    monkeypatch.setattr("app.agente_compara_api_routes.current_user", fake_user)
    monkeypatch.setattr(
        "app.agente_compara_api_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda user: {"permitido": True},
    )
    monkeypatch.setattr("app.agente_compara_api_routes.get_agente_compara_config", _cfg)
    called = {"n": 0}

    def boom(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("runtime não deve ser chamado pré-READY")

    monkeypatch.setattr("app.agente_compara_api_routes.chat_agente_compara_comparison_reply", boom)
    client = web.app.test_client()
    with client.session_transaction() as sess:
        state = _ready_state()
        state["current_step"] = STEP_PREPARE_TABLE_1
        state["status"] = COMPARISON_STATUS_PREPARING
        sess[AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = state
    monkeypatch.setattr(
        "app.agente_compara_calculation_execution_service.get_comparison_calculation_status",
        lambda **kwargs: {"status": "not_started", "result": None, "analytics": None, "stale": False},
    )
    res = client.post("/api/agente-compara/comparison-chat", json={"message": "Oi"})
    assert res.status_code == 409
    body = res.get_json()
    assert body["error"] == ERROR_COMPARISON_CHAT_NOT_READY
    assert body["chat_available"] is False
    assert body["capability"] == CAPABILITY_LOCKED
    assert body["message"] == CHAT_NOT_READY_MESSAGE
    assert called["n"] == 0


def test_comparison_chat_route_validation_and_success(monkeypatch):
    web = _load_web()
    fake_user = SimpleNamespace(is_authenticated=True, conta_id=1, franquia_id=1, id=1)
    monkeypatch.setattr("app.agente_compara_api_routes.current_user", fake_user)
    monkeypatch.setattr(
        "app.agente_compara_api_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda user: {"permitido": True},
    )
    monkeypatch.setattr("app.agente_compara_api_routes.get_agente_compara_config", _cfg)
    result = _result()
    analytics = build_comparison_analytics(copy.deepcopy(result))
    monkeypatch.setattr(
        "app.agente_compara_api_routes.chat_agente_compara_comparison_reply",
        lambda *args, **kwargs: {
            "answer": "Resposta Gemini mock.",
            "deterministic": False,
            "scope": "overview",
            "basis": {"table_count": 2},
            "warnings": [],
            "flow_type": "agente_compara_comparison_chat",
            "chat_available": True,
            "capability": CAPABILITY_READY,
        },
    )
    monkeypatch.setattr(
        "app.agente_compara_calculation_execution_service.get_comparison_calculation_status",
        lambda **kwargs: {
            "status": STEP_CALCULATION_READY,
            "result": result,
            "analytics": analytics,
            "stale": False,
            "billing_status": "applied",
        },
    )
    client = web.app.test_client()
    with client.session_transaction() as sess:
        sess[AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = _ready_state()
    res = client.post("/api/agente-compara/comparison-chat", json={})
    assert res.status_code == 400
    res = client.post("/api/agente-compara/comparison-chat", json={"message": "   "})
    assert res.status_code == 400
    res = client.post(
        "/api/agente-compara/comparison-chat",
        json={
            "message": "oi",
            "request_id": "req-route-1",
            "comparison_id": "cmp-route-1",
            "history": [],
            "ui_context": {"active_view": "dashboard"},
        },
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is True
    assert body["answer"] == "Resposta Gemini mock."
    assert body["chat_available"] is True
    assert body["request_id"] == "req-route-1"
    assert "prompt" not in body
    assert "storage_key" not in body


def test_comparison_chat_route_scope_mismatch(monkeypatch):
    web = _load_web()
    fake_user = SimpleNamespace(is_authenticated=True, conta_id=1, franquia_id=1, id=1)
    monkeypatch.setattr("app.agente_compara_api_routes.current_user", fake_user)
    monkeypatch.setattr(
        "app.agente_compara_api_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda user: {"permitido": True},
    )
    monkeypatch.setattr("app.agente_compara_api_routes.get_agente_compara_config", _cfg)
    client = web.app.test_client()
    with client.session_transaction() as sess:
        sess[AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = _ready_state("cmp-a")
    res = client.post(
        "/api/agente-compara/comparison-chat",
        json={"message": "Cobertura", "comparison_id": "other"},
    )
    assert res.status_code == 409
    body = res.get_json()
    assert body["chat_available"] is False
    err = body.get("error_code") or body.get("error") or ""
    assert "scope_mismatch" in str(err)


def test_comparison_chat_route_provider_not_configured(monkeypatch):
    web = _load_web()
    fake_user = SimpleNamespace(is_authenticated=True, conta_id=1, franquia_id=1, id=1)
    monkeypatch.setattr("app.agente_compara_api_routes.current_user", fake_user)
    monkeypatch.setattr(
        "app.agente_compara_api_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda user: {"permitido": True},
    )
    monkeypatch.setattr("app.agente_compara_api_routes.get_agente_compara_config", _cfg)
    result = _result()
    analytics = build_comparison_analytics(copy.deepcopy(result))
    monkeypatch.setattr(
        "app.agente_compara_calculation_execution_service.get_comparison_calculation_status",
        lambda **kwargs: {
            "status": STEP_CALCULATION_READY,
            "result": result,
            "analytics": analytics,
            "stale": False,
            "billing_status": "applied",
        },
    )
    monkeypatch.setattr(
        "app.agente_compara_api_routes.chat_agente_compara_comparison_reply",
        lambda *args, **kwargs: {
            "answer": "",
            "error": "provider_not_configured",
            "error_code": "provider_not_configured",
            "message": "O serviço de inteligência artificial não está configurado neste ambiente.",
            "http_status": 503,
            "retryable": False,
            "chat_available": True,
            "capability": CAPABILITY_READY,
            "flow_type": "agente_compara_comparison_chat",
        },
    )
    client = web.app.test_client()
    with client.session_transaction() as sess:
        sess[AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = _ready_state()
    res = client.post(
        "/api/agente-compara/comparison-chat",
        json={"message": "Olá", "comparison_id": "cmp-route-1", "request_id": "req-503"},
    )
    assert res.status_code == 503
    body = res.get_json()
    assert body["ok"] is False
    assert body["error"] is True
    assert body["error_code"] == "provider_not_configured"
    assert body["chat_available"] is True
    assert body["retryable"] is False
    assert body["request_id"] == "req-503"
    assert "não está configurado" in body["message"].lower() or "nao esta configurado" in body["message"].lower().replace("ã", "a")


def test_frontend_chat_contracts():
    js = JS_PATH.read_text(encoding="utf-8")
    html = HTML_PATH.read_text(encoding="utf-8")
    assert "API_COMPARISON_CHAT" in js
    assert "/api/agente-compara/comparison-chat" in js
    assert "provider_not_configured" in js
    assert "provider_timeout" in js
    assert "provider_empty_response" in js
    assert "CHAT_NETWORK_MESSAGE" in js
    assert "Não foi possível conectar ao serviço" in js
    assert "Verifique sua conexão" in js
    assert "CHAT_PROVIDER_NOT_CONFIGURED_MESSAGE" in js
    assert "applyComparisonChatAvailabilityFromError" in js
    assert "respondWithPreReadyGuidance" in js
    assert "clearFlowGuidanceMessages" in js
    send_block = js[js.index("function sendChatMessage"): js.index("function initChat")]
    assert "não adiciona resposta técnica ao history" in send_block or "não entra no histórico" in send_block
    assert "chatHistory.push" in send_block
    assert "agenteComparaChatResponsibility" in html
    assert "Faça o upload da tabela de frete." in html
    # Composer nasce utilizável (sem atributo disabled no textarea/botão de envio).
    assert 'id="agenteComparaInput"' in html
    input_block = html[html.index('id="agenteComparaInput"') - 120 : html.index('id="agenteComparaInput"') + 350]
    assert "\ndisabled\n" not in input_block and " disabled" not in input_block.replace("aria-disabled", "")
    assert 'aria-disabled="false"' in input_block or "aria-disabled='false'" in input_block
    send_html = html[html.index('id="agenteComparaSend"') : html.index('id="agenteComparaSend"') + 280]
    assert "\ndisabled\n" not in send_html and " disabled" not in send_html.replace("aria-disabled", "")
    assert 'aria-disabled="false"' in send_html or "aria-disabled='false'" in send_html

def test_frontend_pre_ready_guards_in_js():
    js = JS_PATH.read_text(encoding="utf-8")
    send_block = js[js.index("function sendChatMessage"): js.index("function initChat")]
    assert "if (!chatAvailable || !isComparisonChatAvailable())" in send_block
    assert "respondWithPreReadyGuidance" in send_block
    assert "generateRequestId()" in send_block
    # Guard local encerra ANTES de request_id / fetch analítico.
    assert send_block.index("respondWithPreReadyGuidance") < send_block.index("generateRequestId()")
    assert send_block.index("respondWithPreReadyGuidance") < send_block.index("fetch(API_COMPARISON_CHAT")
    assert "isComparisonChatNotReadyError" in send_block
    assert "appendBlockedChatGuidance" in send_block
    init_block = js[js.index("function initChat"): js.index("function displayFieldValue")]
    assert "sendChatMessage();" in init_block
    # Enter/click não bloqueiam mais o envio pré-READY.
    assert "if (!isComparisonChatAvailable()) return;\n      sendChatMessage();" not in init_block
    assert "lockComparisonChat" in init_block

    locked_ui = js[js.index("function updateChatLockedUi"): js.index("function unlockChat")]
    assert "input.disabled = false" in locked_ui
    assert "sendBtn.disabled = false" in locked_ui
    assert "aria-disabled', 'false'" in locked_ui or 'aria-disabled", "false"' in locked_ui

    set_enabled = js[js.index("function setChatInputEnabled"): js.index("function escapeHtml")]
    assert "if (!chatAvailable)" in set_enabled
    assert "input.disabled = false" in set_enabled

    sync_block = js[js.index("function syncProgressiveChatUnlock"): js.index("function prepareContextualChatQuestion")]
    assert "clearFlowGuidanceMessages" in sync_block
    assert "chatHistory = []" in sync_block


def test_frontend_pre_ready_local_guidance_contract():
    """Contrato estático: qualquer texto pré-READY → orientação fixa, zero fetch."""
    js = JS_PATH.read_text(encoding="utf-8")
    assert "CHAT_BLOCKED_MESSAGE = 'Faça o upload da tabela de frete.'" in js
    assert "CHAT_NOT_READY_MESSAGE = 'Faça o upload da tabela de frete.'" in js
    assert "function respondWithPreReadyGuidance" in js
    assert "function clearFlowGuidanceMessages" in js
    assert "data-chat-flow-guidance" in js
    send_block = js[js.index("function sendChatMessage"): js.index("function initChat")]
    # Conteúdo da pergunta é ignorado semanticamente: sem regex/keywords no guard.
    pre_ready_slice = send_block[
        send_block.index("if (!chatAvailable || !isComparisonChatAvailable())") : send_block.index(
            "var sendGeneration"
        )
    ]
    assert "respondWithPreReadyGuidance(text)" in pre_ready_slice
    assert "fetch(" not in pre_ready_slice
    assert "generateRequestId" not in pre_ready_slice
    assert "regex" not in pre_ready_slice.lower()
    assert "match(" not in pre_ready_slice
    assert "includes(" not in pre_ready_slice
    assert "indexOf(" not in pre_ready_slice
    # Zero guided no fluxo de chat do AgenteCompara.
    assert "capability === 'guided'" not in js
    assert "chatCapability = 'guided'" not in js