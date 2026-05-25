import importlib
import os
from pathlib import Path
from types import SimpleNamespace

import flask_login.utils

from app.cleide_controlled_chat import _normalize_for_match


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


def _set_current_user(monkeypatch, *, is_authenticated=True, user_id="1"):
    fake_user = SimpleNamespace(
        is_authenticated=is_authenticated,
        is_active=True,
        is_anonymous=not is_authenticated,
        get_id=lambda: user_id,
        conta_id=1,
        franquia_id=1,
        categoria="pro",
        full_name="Teste Cleide",
        email="cleide@example.com",
        franquia=None,
    )
    monkeypatch.setattr(flask_login.utils, "_get_user", lambda: fake_user)
    return fake_user


def test_auditoria_frete_retornar_200_quando_autorizado(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", _set_current_user(monkeypatch))
    monkeypatch.setattr(
        web,
        "avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True, "modo_operacao": "normal"},
    )
    monkeypatch.setattr(
        "app.cleide_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True, "modo_operacao": "normal"},
    )
    monkeypatch.setattr("app.cleide_routes.get_cleide_config", lambda: SimpleNamespace(layout_version=1))

    client = web.app.test_client()
    resp = client.get("/auditoria-frete")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Agente Cleide" in html
    assert "Auditoria de Frete Operacional" in html
    assert 'data-testid="cleide-upload-placeholder"' in html
    assert "id=\"cleideUploadForm\"" in html
    assert "id=\"cleideUploadInput\"" in html
    assert 'data-testid="cleide-dashboard-filter-top"' in html
    assert 'data-testid="cleide-filters-placeholder"' not in html
    assert "id=\"cleideStructuralFeedback\"" in html
    assert "id=\"cleideStructuralDetails\"" in html
    assert 'data-testid="cleide-kpis-placeholder"' not in html
    assert 'data-testid="cleide-dashboard-placeholder"' not in html
    assert 'data-testid="cleide-chat-placeholder"' not in html


def test_auditoria_frete_publica_quando_nao_autenticado(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", _set_current_user(monkeypatch, is_authenticated=False))
    monkeypatch.setattr("app.cleide_routes.get_cleide_config", lambda: SimpleNamespace(layout_version=1))

    calls = {"authz": 0}

    def _authz_should_not_run(_u):
        calls["authz"] += 1
        return {"permitido": True, "modo_operacao": "normal"}

    monkeypatch.setattr(
        "app.cleide_routes.avaliar_autorizacao_operacao_por_franquia",
        _authz_should_not_run,
    )

    client = web.app.test_client()
    resp = client.get("/auditoria-frete")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Auditoria de Frete Operacional" in html
    assert "Faca login para enviar planilhas" in html
    assert 'data-testid="cleide-template-download"' in html
    assert 'href="/api/cleide/template"' in html
    assert calls["authz"] == 0


def test_auditoria_frete_bloqueia_quando_franquia_nao_permite(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", _set_current_user(monkeypatch))
    monkeypatch.setattr(
        "app.cleide_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {
            "permitido": False,
            "modo_operacao": "blocked",
            "mensagem_usuario": "Sua franquia atingiu o limite operacional.",
        },
    )

    client = web.app.test_client()
    resp = client.get("/auditoria-frete")
    assert resp.status_code == 403
    html = resp.get_data(as_text=True)
    assert "Sua franquia atingiu o limite operacional." in html


def test_auditoria_frete_nao_depende_da_flag_de_ia(monkeypatch):
    web = _load_web_module()
    monkeypatch.setenv("CLEIDE_AI_ENABLED", "false")
    monkeypatch.delenv("CLEIDE_AI_ENABLED_LOCAL", raising=False)
    monkeypatch.delenv("CLEIDE_GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(web, "current_user", _set_current_user(monkeypatch))
    monkeypatch.setattr(
        "app.cleide_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True, "modo_operacao": "normal"},
    )
    monkeypatch.setattr("app.cleide_routes.get_cleide_config", lambda: SimpleNamespace(layout_version=1))

    client = web.app.test_client()
    resp = client.get("/auditoria-frete")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Auditoria de Frete Operacional" in html
    assert "IA contextual e insights automáticos ainda inativos" in html


def test_chat_cleide_exige_login_sem_consumir_autorizacao(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr("app.cleide_routes.current_user", SimpleNamespace(is_authenticated=False))

    calls = {"authz": 0, "chat": 0}

    def _authz(_u):
        calls["authz"] += 1
        return {"permitido": True}

    def _chat(**_kwargs):
        calls["chat"] += 1
        return {"reply": "nao deveria rodar"}, 200

    monkeypatch.setattr("app.cleide_routes.avaliar_autorizacao_operacao_por_franquia", _authz)
    monkeypatch.setattr("app.cleide_routes.run_cleide_controlled_chat", _chat)

    client = web.app.test_client()
    resp = client.post("/api/chat_cleide", json={"question": "teste"})
    assert resp.status_code == 401
    assert calls["authz"] == 0
    assert calls["chat"] == 0


def test_chat_cleide_placeholder_expoe_contexto_seguro_sem_ia(monkeypatch):
    web = _load_web_module()
    monkeypatch.delenv("CLEIDE_AI_ENABLED", raising=False)
    monkeypatch.delenv("CLEIDE_AI_ENABLED_LOCAL", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY_ROBERTO", raising=False)
    monkeypatch.setattr("app.cleide_routes.current_user", SimpleNamespace(is_authenticated=True))
    monkeypatch.setattr(
        "app.cleide_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True, "modo_operacao": "normal"},
    )
    monkeypatch.setattr(
        "app.cleide_controlled_chat.get_cleide_chat_context",
        lambda _session: {
            "chat_context_version": "cleide_chat_context.v1",
            "chat_ready_context": True,
            "safe_operational_context": {
                "schema_version": "cleide_contexto_operacional.v1",
                "session_scope": {"dataset_validado": True},
                "kpis": {
                    "total_documentos": 120,
                    "valor_total_frete": 20000,
                    "peso_total": 5000,
                    "ticket_medio_frete": 166.66,
                    "periodo_dataset": {"inicio": "2026-01-01", "fim": "2026-01-31"},
                },
                "aggregate_tables": {
                    "transportadora": [
                        {"chave": "XP", "quantidade": 80},
                        {"chave": "YZ", "quantidade": 20},
                    ],
                    "uf_origem": [{"chave": "SP", "quantidade": 60}],
                    "uf_destino": [{"chave": "RJ", "quantidade": 70}],
                },
                "dataset_summary": {
                    "invalid_numeric_rows": 0,
                    "invalid_date_rows": 0,
                    "negative_value_rows": 0,
                },
                "quality_flags": {"has_sparse_aggregates": False},
                "filter_context": {"filter_mode": "aggregate_approximation", "kpi_scope": "global_session"},
                "semantic_limits": {
                    "no_row_level_intersection": True,
                    "multi_dimension_filters_are_approximate": True,
                    "kpis_are_global_session_scope": True,
                    "no_accusatory_financial_conclusion": True,
                },
            },
            "exposure_controls": {"max_items_per_table": 10, "truncated": False},
        },
    )
    client = web.app.test_client()
    resp = client.post("/api/chat_cleide", json={"question": "resumo operacional"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["intent"] == "resumo_operacional"
    assert body["ai_enabled"] is False
    assert body["mode"] == "controlled_templates_no_ai_phase_9"
    assert body["contract_version"] == "cleide_chat_controlled.v1"
    assert body["phase"] == "9.1_hardening_semantic_governance"
    assert body["audit_transition_marker"] == "transition_501_placeholder_to_200_controlled_confirmed"
    assert body["audit_notes"]["legacy_chat_status"] == 501
    assert body["audit_notes"]["current_chat_status"] == 200
    reply_norm = _normalize_for_match(body["reply"])
    assert "resumo operacional da sessao" in reply_norm
    assert "ticket medio" in reply_norm
    assert body["chat_context_version"] == "cleide_chat_context.v1"
    assert body["chat_ready_context"] is True
    assert body["semantic_limits"]["kpis_are_global_session_scope"] is True
    assert body["filter_mode"] in {"aggregate_approximation", "row_level_intersection_backend"}
    assert body["kpi_scope"] in {"global_session", "filtered_session_intersection"}
    assert body["view_scope"] in {"global", "filtered"}
    assert isinstance(body["active_filters"], dict)
    assert body["context_status"] in {"ready", "insufficient", "stale"}


def test_chat_cleide_sanitiza_history_e_preserva_question(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr("app.cleide_routes.current_user", SimpleNamespace(is_authenticated=True))
    monkeypatch.setattr(
        "app.cleide_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True, "modo_operacao": "normal"},
    )
    captured = {}

    def _fake_chat(**kwargs):
        captured.update(kwargs)
        return {"reply": "ok"}, 200

    monkeypatch.setattr("app.cleide_routes.run_cleide_controlled_chat", _fake_chat)
    client = web.app.test_client()
    payload = {
        "question": "Quem esta em terceiro?",
        "history": [
            {"role": "user", "content": "Qual transportadora lidera?", "extra": "x"},
            {"role": "assistant", "content": "XP lidera."},
            {"role": "cleide", "content": "YZ esta em segundo."},
            {"role": "system", "content": "ignorar"},
            {"role": "user", "content": "a" * 1000},
        ],
    }
    resp = client.post("/api/chat_cleide", json=payload)
    assert resp.status_code == 200
    assert captured["question"] == "Quem esta em terceiro?"
    assert isinstance(captured["history"], list)
    assert len(captured["history"]) == 4
    assert captured["history"][0] == {"role": "user", "content": "Qual transportadora lidera?"}
    assert captured["history"][2]["role"] == "assistant"
    assert len(captured["history"][-1]["content"]) == 900


def test_cleide_template_download_publico_sem_login(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr("app.cleide_routes.current_user", SimpleNamespace(is_authenticated=False))

    client = web.app.test_client()
    resp = client.get("/api/cleide/template")

    assert resp.status_code == 200
    assert resp.mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    disposition = resp.headers.get("Content-Disposition", "")
    assert "attachment;" in disposition
    assert "template_cleide_auditoria_frete.xlsx" in disposition
    expected = (
        Path(web.app.root_path)
        / "protected_files"
        / "templates"
        / "template_cleide_auditoria_frete.xlsx"
    ).read_bytes()
    assert resp.data == expected
