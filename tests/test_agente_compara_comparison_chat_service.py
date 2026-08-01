"""Testes de prompt, serviço, decisão e fronteira Gemini do chat comparativo."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from app.agente_compara_chat_prompt import (
    build_agente_compara_comparison_chat_system_prompt,
    build_comparison_chat_user_prompt,
)
from app.agente_compara_comparison_analytics_service import build_comparison_analytics
from app.agente_compara_comparison_state import (
    AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY,
    COMPARISON_STATUS_CALCULATION_READY,
    STEP_CALCULATION_READY,
    STEP_PREPARE_TABLE_1,
)
from app.run_agente_compara_comparison_chat import (
    ERROR_PROVIDER_EMPTY_RESPONSE,
    ERROR_PROVIDER_INITIALIZATION_FAILED,
    ERROR_PROVIDER_NOT_CONFIGURED,
    ERROR_PROVIDER_REQUEST_FAILED,
    ERROR_PROVIDER_TIMEOUT,
    MSG_PROVIDER_NOT_CONFIGURED,
    ComparisonChatProviderNotConfigured,
    build_deterministic_comparison_fallback,
    chat_agente_compara_comparison_reply,
    resolve_comparison_chat_model,
    _get_client,
)
from app.services.agente_compara_config_service import AgenteComparaConfig, DEFAULT_FALLBACK_MESSAGE


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


def _session(data=None):
    class _S(dict):
        modified = False

    sess = _S()
    if data:
        sess.update(data)
    return sess


def _ready_bundle():
    state = {
        "comparison_id": "cmp-svc-1",
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
    result = {
        "schema_version": 1,
        "comparison_id": "cmp-svc-1",
        "execution_id": "exec-1",
        "table_count": 2,
        "row_count": 2,
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
                        "calculation_memory": {
                            "status": "calculated",
                            "calculated_freight": 40,
                            "components": [{"code": "TOTAL", "label": "Total", "amount": 40}],
                            "warnings": [],
                            "blocking_issues": [],
                        },
                    },
                    "t2": {
                        "table_id": "t2",
                        "carrier_name": "Beta",
                        "slot_number": 2,
                        "calculated_freight": 55,
                        "status": "calculated",
                    },
                },
            },
            {
                "row_index": 2,
                "document_number": "1002",
                "destination_city": "Santos",
                "destination_uf": "SP",
                "weight": 12,
                "invoice_value": 120,
                "table_results": {
                    "t1": {
                        "table_id": "t1",
                        "carrier_name": "Alpha",
                        "slot_number": 1,
                        "calculated_freight": 50,
                        "status": "calculated",
                    },
                    "t2": {
                        "table_id": "t2",
                        "carrier_name": "Beta",
                        "slot_number": 2,
                        "calculated_freight": None,
                        "status": "incomplete",
                        "is_partial_value": True,
                    },
                },
            },
        ],
        "summary": {},
    }
    analytics = build_comparison_analytics(copy.deepcopy(result))
    return state, result, analytics


class StrictGeminiModels:
    def __init__(self, handler):
        self._handler = handler
        self.calls = []

    def generate_content(self, *, model, contents, config=None):
        if not model:
            raise AssertionError("model ausente na chamada generate_content")
        if contents is None or (isinstance(contents, str) and not contents.strip()):
            raise AssertionError("contents ausente/vazio na chamada generate_content")
        # config pode ser None (padrão do runtime atual)
        call = {"model": model, "contents": contents, "config": config}
        self.calls.append(call)
        return self._handler(call)


class StrictGeminiClient:
    def __init__(self, handler):
        self.models = StrictGeminiModels(handler)


def _patch_ready_status(monkeypatch, result, analytics):
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


def _install_strict_client(monkeypatch, handler):
    client = StrictGeminiClient(handler)
    monkeypatch.setattr(
        "app.run_agente_compara_comparison_chat._get_client",
        lambda: client,
    )
    return client


def test_prompt_contracts():
    prompt = build_agente_compara_comparison_chat_system_prompt().lower()
    assert "analista" in prompt
    assert "decisão final" in prompt or "decisao final" in prompt
    user_prompt = build_comparison_chat_user_prompt(
        user_message="Resuma",
        history_slice=[],
        context_payload={"comparison": {"comparison_id": "x"}},
        scope="overview",
    )
    assert "Contexto comparativo oficial" in user_prompt


def test_get_client_missing_key_raises_typed(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ComparisonChatProviderNotConfigured) as exc:
        _get_client()
    assert exc.value.error_code == ERROR_PROVIDER_NOT_CONFIGURED


def test_get_client_init_failure_raises_typed(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY_1", "test-key-not-real")

    import app.run_agente_compara_comparison_chat as mod

    real_get_client = mod._get_client.__wrapped__ if hasattr(mod._get_client, "__wrapped__") else None

    class BoomClient:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("client-init-boom")

    class FakeTypes:
        @staticmethod
        def HttpOptions(**kwargs):
            return object()

    class FakeGenai:
        Client = BoomClient

    def patched_get_client():
        key = (__import__("os").getenv("GEMINI_API_KEY_1") or __import__("os").getenv("GEMINI_API_KEY") or "").strip()
        if not key:
            raise mod.ComparisonChatProviderNotConfigured()
        try:
            genai = FakeGenai()
            types = FakeTypes()
            return genai.Client(api_key=key, http_options=types.HttpOptions(timeout=1000))
        except mod.ComparisonChatProviderError:
            raise
        except Exception as exc:
            raise mod.ComparisonChatProviderInitializationError(cause=exc) from exc

    monkeypatch.setattr(mod, "_get_client", patched_get_client)
    with pytest.raises(mod.ComparisonChatProviderInitializationError) as exc:
        mod._get_client()
    assert exc.value.error_code == ERROR_PROVIDER_INITIALIZATION_FAILED


def test_resolve_model_not_empty():
    model = resolve_comparison_chat_model()
    assert isinstance(model, str) and model.strip()


def test_provider_not_configured_returns_503_payload(ctx, monkeypatch):
    state, result, analytics = _ready_bundle()
    sess = _session({AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY: state})
    monkeypatch.setattr("app.run_agente_compara_comparison_chat.get_agente_compara_config", _cfg)
    monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    _patch_ready_status(monkeypatch, result, analytics)
    calls = {"n": 0}

    def boom(*args, **kwargs):
        calls["n"] += 1
        raise AssertionError("generate_content não deve ser chamado sem chave")

    monkeypatch.setattr(
        "app.run_agente_compara_comparison_chat.cleiton_governed_generate_content",
        boom,
    )
    reply = chat_agente_compara_comparison_reply(
        "Olá",
        [],
        session_obj=sess,
        comparison_id="cmp-svc-1",
        request_id="req-no-key",
    )
    assert reply.get("error") == ERROR_PROVIDER_NOT_CONFIGURED
    assert reply.get("http_status") == 503
    assert reply.get("ok") is not True
    assert reply.get("chat_available") is True
    assert reply.get("retryable") is False
    assert MSG_PROVIDER_NOT_CONFIGURED in (reply.get("message") or "")
    assert calls["n"] == 0
    assert not reply.get("answer")


@pytest.mark.parametrize(
    "question,expected_snippet",
    [
        (
            "Olá",
            "Olá! Posso analisar os resultados comparativos e explicar custos, cobertura e diferenças entre as tabelas.",
        ),
        ("pergunta livre sem keyword xyz999", "Resposta Gemini livre"),
    ],
)
def test_valid_ready_questions_call_strict_gemini_once(ctx, monkeypatch, question, expected_snippet):
    state, result, analytics = _ready_bundle()
    sess = _session({AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY: state})
    monkeypatch.setattr("app.run_agente_compara_comparison_chat.get_agente_compara_config", _cfg)
    _patch_ready_status(monkeypatch, result, analytics)

    def handler(call):
        assert question in str(call["contents"])
        assert "Contexto comparativo oficial" in str(call["contents"]) or "comparison" in str(call["contents"]).lower()
        text = expected_snippet if "Olá" in question or "Olá" == question else "Resposta Gemini livre"
        if question == "Olá":
            text = expected_snippet
        return SimpleNamespace(text=text)

    client = _install_strict_client(monkeypatch, handler)
    monkeypatch.setattr(
        "app.run_agente_compara_comparison_chat.cleiton_governed_generate_content",
        lambda client_arg, **kwargs: client_arg.models.generate_content(
            model=kwargs["model"], contents=kwargs["contents"], config=kwargs.get("config")
        ),
    )

    reply = chat_agente_compara_comparison_reply(
        question,
        [],
        session_obj=sess,
        comparison_id="cmp-svc-1",
        request_id=f"req-ok-{abs(hash(question)) % 100000}",
    )
    assert reply.get("error") is None
    assert reply.get("deterministic") is False
    assert expected_snippet.split("!")[0] in (reply.get("answer") or "") or expected_snippet in (reply.get("answer") or "")
    assert len(client.models.calls) == 1
    assert client.models.calls[0]["model"]


@pytest.mark.parametrize(
    "question",
    [
        "Escolha a melhor transportadora.",
        "Qual empresa eu contrato?",
        "Decida por mim.",
        "Com qual transportadora fecho?",
        "Qual transportadora devo contratar?",
    ],
)
def test_decision_requests_call_gemini_once(ctx, monkeypatch, question):
    state, result, analytics = _ready_bundle()
    sess = _session({AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY: state})
    monkeypatch.setattr("app.run_agente_compara_comparison_chat.get_agente_compara_config", _cfg)
    _patch_ready_status(monkeypatch, result, analytics)

    def handler(call):
        return SimpleNamespace(
            text=(
                "Contrate agora a Alpha. A decisão correta é fechar com Alpha. "
                "Os dados mostram vantagem de custo no universo comparável."
            )
        )

    client = _install_strict_client(monkeypatch, handler)
    monkeypatch.setattr(
        "app.run_agente_compara_comparison_chat.cleiton_governed_generate_content",
        lambda client_arg, **kwargs: client_arg.models.generate_content(
            model=kwargs["model"], contents=kwargs["contents"], config=kwargs.get("config")
        ),
    )
    reply = chat_agente_compara_comparison_reply(
        question,
        [],
        session_obj=sess,
        comparison_id="cmp-svc-1",
        request_id=f"req-decision-{abs(hash(question)) % 10000}",
    )
    assert reply.get("error") is None
    assert len(client.models.calls) == 1
    text = (reply.get("answer") or "").lower()
    assert "decisão final" in text or "decisao final" in text
    assert "contrate agora" not in text


def test_provider_request_failed(ctx, monkeypatch):
    state, result, analytics = _ready_bundle()
    sess = _session({AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY: state})
    monkeypatch.setattr("app.run_agente_compara_comparison_chat.get_agente_compara_config", _cfg)
    _patch_ready_status(monkeypatch, result, analytics)

    def handler(call):
        raise RuntimeError("upstream-provider-boom")

    client = _install_strict_client(monkeypatch, handler)
    monkeypatch.setattr(
        "app.run_agente_compara_comparison_chat.cleiton_governed_generate_content",
        lambda client_arg, **kwargs: client_arg.models.generate_content(
            model=kwargs["model"], contents=kwargs["contents"], config=kwargs.get("config")
        ),
    )
    reply = chat_agente_compara_comparison_reply(
        "Olá",
        [],
        session_obj=sess,
        comparison_id="cmp-svc-1",
        request_id="req-provider-fail",
    )
    assert reply.get("error") == ERROR_PROVIDER_REQUEST_FAILED
    assert reply.get("http_status") == 503
    assert reply.get("retryable") is True
    assert reply.get("chat_available") is True
    assert not reply.get("answer")
    assert len(client.models.calls) == 1


def test_provider_timeout(ctx, monkeypatch):
    state, result, analytics = _ready_bundle()
    sess = _session({AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY: state})
    monkeypatch.setattr("app.run_agente_compara_comparison_chat.get_agente_compara_config", _cfg)
    _patch_ready_status(monkeypatch, result, analytics)

    def handler(call):
        raise TimeoutError("DEADLINE_EXCEEDED")

    client = _install_strict_client(monkeypatch, handler)
    monkeypatch.setattr(
        "app.run_agente_compara_comparison_chat.cleiton_governed_generate_content",
        lambda client_arg, **kwargs: client_arg.models.generate_content(
            model=kwargs["model"], contents=kwargs["contents"], config=kwargs.get("config")
        ),
    )
    reply = chat_agente_compara_comparison_reply(
        "Olá",
        [],
        session_obj=sess,
        comparison_id="cmp-svc-1",
        request_id="req-timeout",
    )
    assert reply.get("error") == ERROR_PROVIDER_TIMEOUT
    assert reply.get("http_status") == 503
    assert reply.get("retryable") is True
    assert len(client.models.calls) == 1


@pytest.mark.parametrize("response_factory", [lambda: None, lambda: SimpleNamespace(), lambda: SimpleNamespace(text=""), lambda: SimpleNamespace(text="   ")])
def test_provider_empty_or_invalid_response(ctx, monkeypatch, response_factory):
    state, result, analytics = _ready_bundle()
    sess = _session({AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY: state})
    monkeypatch.setattr("app.run_agente_compara_comparison_chat.get_agente_compara_config", _cfg)
    _patch_ready_status(monkeypatch, result, analytics)

    def handler(call):
        return response_factory()

    client = _install_strict_client(monkeypatch, handler)
    monkeypatch.setattr(
        "app.run_agente_compara_comparison_chat.cleiton_governed_generate_content",
        lambda client_arg, **kwargs: client_arg.models.generate_content(
            model=kwargs["model"], contents=kwargs["contents"], config=kwargs.get("config")
        ),
    )
    reply = chat_agente_compara_comparison_reply(
        "Olá",
        [],
        session_obj=sess,
        comparison_id="cmp-svc-1",
        request_id=f"req-empty-{id(response_factory)}",
    )
    assert reply.get("error") in {ERROR_PROVIDER_EMPTY_RESPONSE, "provider_invalid_response"}
    assert reply.get("http_status") == 503
    assert not reply.get("answer")
    assert len(client.models.calls) == 1


def test_one_call_per_question_and_cache(ctx, monkeypatch):
    state, result, analytics = _ready_bundle()
    sess = _session({AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY: state})
    monkeypatch.setattr("app.run_agente_compara_comparison_chat.get_agente_compara_config", _cfg)
    _patch_ready_status(monkeypatch, result, analytics)

    def handler(call):
        return SimpleNamespace(text="Cobertura maior na Alpha no universo total disponível.")

    client = _install_strict_client(monkeypatch, handler)
    monkeypatch.setattr(
        "app.run_agente_compara_comparison_chat.cleiton_governed_generate_content",
        lambda client_arg, **kwargs: client_arg.models.generate_content(
            model=kwargs["model"], contents=kwargs["contents"], config=kwargs.get("config")
        ),
    )
    first = chat_agente_compara_comparison_reply(
        "Qual transportadora teve maior cobertura?",
        [],
        session_obj=sess,
        comparison_id="cmp-svc-1",
        request_id="req-one-1",
    )
    second = chat_agente_compara_comparison_reply(
        "Qual transportadora teve maior cobertura?",
        [],
        session_obj=sess,
        comparison_id="cmp-svc-1",
        request_id="req-one-1",
    )
    assert first.get("answer")
    assert second.get("cached") is True
    assert len(client.models.calls) == 1


def test_pre_ready_blocks_without_gemini(ctx, monkeypatch):
    state, result, analytics = _ready_bundle()
    state["current_step"] = STEP_PREPARE_TABLE_1
    sess = _session({AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY: state})
    calls = {"n": 0}
    monkeypatch.setattr("app.run_agente_compara_comparison_chat.get_agente_compara_config", _cfg)

    def boom(*args, **kwargs):
        calls["n"] += 1
        raise AssertionError("Gemini não deve ser chamado pré-READY")

    monkeypatch.setattr(
        "app.run_agente_compara_comparison_chat.cleiton_governed_generate_content",
        boom,
    )
    monkeypatch.setattr(
        "app.agente_compara_calculation_execution_service.get_comparison_calculation_status",
        lambda **kwargs: {"status": "not_started", "result": None, "analytics": None, "stale": False},
    )
    reply = chat_agente_compara_comparison_reply(
        "oi",
        [],
        session_obj=sess,
        comparison_id="cmp-svc-1",
        request_id="req-pre",
    )
    assert reply.get("error") == "COMPARISON_CHAT_NOT_READY"
    assert reply.get("chat_available") is False
    assert calls["n"] == 0


def test_deterministic_fallback_is_not_provider_success_mask():
    state, result, analytics = _ready_bundle()
    from app.agente_compara_chat_context_service import build_comparison_chat_context

    sess = _session({AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY: state})
    ctx_payload = build_comparison_chat_context(
        comparison_id="cmp-svc-1",
        question="Olá",
        session_obj=sess,
        result=result,
        analytics=analytics,
        calc_status={
            "status": STEP_CALCULATION_READY,
            "result": result,
            "analytics": analytics,
            "stale": False,
            "billing_status": "applied",
        },
        load_temp_table_record=lambda *a, **k: None,
    )
    answer = build_deterministic_comparison_fallback(ctx_payload, question="Olá")
    assert answer == ""


def test_empty_and_invalid_message(ctx, monkeypatch):
    monkeypatch.setattr("app.run_agente_compara_comparison_chat.get_agente_compara_config", _cfg)
    sess = _session()
    empty = chat_agente_compara_comparison_reply("", [], session_obj=sess)
    assert empty["error"] == "invalid_message"
    too_long = chat_agente_compara_comparison_reply("x" * 20000, [], session_obj=sess, question_max_chars=100)
    assert too_long["error"] == "invalid_message"
