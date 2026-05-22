import app.cleide_gemini_adapter as adapter


def _safe_context():
    return {
        "schema_version": "cleide_contexto_operacional.v1",
        "security_guards": {
            "contains_raw_dataset": False,
            "contains_full_rows": False,
            "contains_roberto_payload": False,
        },
        "dataset_summary": {"linhas_processadas": 123},
        "kpis": {"total_documentos": 10},
        "semantic_limits": {"no_row_level_intersection": True},
    }


def test_adapter_recebe_apenas_safe_operational_context(monkeypatch):
    monkeypatch.setenv("CLEIDE_AI_ENABLED_LOCAL", "true")
    monkeypatch.setenv("GEMINI_API_KEY_ROBERTO", "k")

    captured = {}

    class _Resp:
        text = "concentracao operacional variacao relevante oportunidade de investigacao"
        usage_metadata = type("U", (), {"prompt_token_count": 1, "candidates_token_count": 1, "total_token_count": 2})()

    def _fake_governed(_client, *, model, contents, agent, flow_type, api_key_label):
        captured["contents"] = contents
        captured["agent"] = agent
        captured["flow_type"] = flow_type
        captured["api_key_label"] = api_key_label
        return _Resp()

    monkeypatch.setattr(adapter, "cleiton_governed_generate_content", _fake_governed)
    monkeypatch.setattr(adapter, "_get_client", lambda _k: object())
    monkeypatch.setattr(adapter, "_model_candidates", lambda: ["gemini-2.5-flash"])

    ctx = _safe_context()
    result = adapter.generate_cleide_ai_reply(
        question="resumo",
        safe_operational_context=ctx,
        history=[{"role": "user", "content": "Qual transportadora lidera?"}],
    )

    assert result["ok"] is True
    assert captured["agent"] == "cleide"
    assert captured["flow_type"] == "cleide_chat_auditoria_frete_ai"
    assert captured["api_key_label"] == "GEMINI_API_KEY_ROBERTO"
    assert "safe_operational_context" in captured["contents"]
    assert "CONVERSA RECENTE" in captured["contents"]
    assert "dataset_bruto" not in captured["contents"].lower()
    assert "run_roberto_chat" not in captured["contents"].lower()


def test_adapter_nao_aceita_contexto_inseguro(monkeypatch):
    monkeypatch.setenv("CLEIDE_AI_ENABLED_LOCAL", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    ctx = _safe_context()
    ctx["security_guards"]["contains_raw_dataset"] = True
    out = adapter.generate_cleide_ai_reply(question="resumo", safe_operational_context=ctx)
    assert out["ok"] is False
    assert out["error_code"] == "unsafe_context"


def test_adapter_sem_chave_desliga_fail_closed(monkeypatch):
    monkeypatch.setenv("CLEIDE_AI_ENABLED_LOCAL", "true")
    monkeypatch.delenv("GEMINI_API_KEY_ROBERTO", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY_2", raising=False)
    monkeypatch.delenv("CLEIDE_GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY_CLEIDE", raising=False)
    out = adapter.generate_cleide_ai_reply(question="resumo", safe_operational_context=_safe_context())
    assert out["ok"] is False
    assert out["error_code"] == "ai_disabled"


def test_adapter_usa_fallback_gemini_api_key_2_quando_anteriores_ausentes(monkeypatch):
    monkeypatch.setenv("CLEIDE_AI_ENABLED_LOCAL", "true")
    monkeypatch.delenv("GEMINI_API_KEY_ROBERTO", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY_2", "k2")

    captured = {}

    class _Resp:
        text = "concentracao operacional variacao relevante oportunidade de investigacao"
        usage_metadata = type("U", (), {"prompt_token_count": 1, "candidates_token_count": 1, "total_token_count": 2})()

    def _fake_governed(_client, *, model, contents, agent, flow_type, api_key_label):
        captured["api_key_label"] = api_key_label
        captured["agent"] = agent
        captured["flow_type"] = flow_type
        return _Resp()

    monkeypatch.setattr(adapter, "cleiton_governed_generate_content", _fake_governed)
    monkeypatch.setattr(adapter, "_get_client", lambda _k: object())
    monkeypatch.setattr(adapter, "_model_candidates", lambda: ["gemini-2.5-flash"])

    out = adapter.generate_cleide_ai_reply(question="resumo", safe_operational_context=_safe_context())
    assert out["ok"] is True
    assert captured["api_key_label"] == "GEMINI_API_KEY_2"
    assert captured["agent"] == "cleide"
    assert captured["flow_type"] == "cleide_chat_auditoria_frete_ai"
