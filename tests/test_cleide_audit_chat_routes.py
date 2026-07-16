"""Testes do endpoint backend de chat da Cleide Auditoria (Fase 2)."""
from __future__ import annotations

import importlib
import inspect
import io
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.cleide_audit_routes as audit_routes
import app.run_cleide_audit_chat as audit_chat
from app.cleide_audit_doc_service import CLEIDE_AUDIT_CHAT_FLOW_TYPE
from app.services.cleide_audit_config_service import (
    CleideAuditConfig,
    DEFAULT_FALLBACK_MESSAGE,
)
from tests.cleiton_doc_fixtures import make_txt, patch_cleiton_doc_cfg, patch_cleiton_doc_store


def _default_audit_cfg(**overrides):
    defaults = {
        "chat_enabled": True,
        "upload_enabled": True,
        "chat_max_history": 10,
        "document_context_max_chars": 24000,
        "max_documents_considered": 3,
        "question_max_chars": 4000,
        "fallback_message": DEFAULT_FALLBACK_MESSAGE,
        "no_documents_behavior": "allow_guided",
        "show_documents_used": True,
        "no_hallucination_instruction_enabled": True,
        "audited_file_max_bytes": None,
        "audited_file_max_rows": 2000,
    }
    defaults.update(overrides)
    return CleideAuditConfig(**defaults)


def _patch_audit_cfg(monkeypatch, **overrides):
    cfg = _default_audit_cfg(**overrides)
    targets = [
        "app.cleide_audit_routes.get_cleide_audit_config",
        "app.cleide_audit_doc_context.get_cleide_audit_config",
        "app.run_cleide_audit_chat.get_cleide_audit_config",
    ]
    for target in targets:
        monkeypatch.setattr(target, lambda _cfg=cfg: _cfg)
    return cfg


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


def _setup_doc_env(monkeypatch, tmp_path, **cfg_overrides):
    patch_cleiton_doc_store(tmp_path, monkeypatch)
    cfg = patch_cleiton_doc_cfg(monkeypatch, **cfg_overrides)
    monkeypatch.setattr("app.cleide_audit_doc_service.get_cleiton_doc_config", lambda: cfg)
    monkeypatch.setattr("app.cleide_audit_routes.get_cleiton_doc_config", lambda: cfg)
    monkeypatch.setattr("app.cleide_audit_doc_context.get_cleiton_doc_config", lambda: cfg)
    _patch_audit_cfg(monkeypatch)
    return cfg


def _authorized(monkeypatch, web, *, authz=None):
    fake_user = SimpleNamespace(is_authenticated=True, conta_id=1, franquia_id=1)
    monkeypatch.setattr(web, "current_user", fake_user)
    monkeypatch.setattr("app.cleide_audit_routes.current_user", fake_user)
    monkeypatch.setattr("app.julia_documents_routes.current_user", fake_user)
    authz_payload = authz or {"permitido": True, "modo_operacao": "normal"}
    monkeypatch.setattr(
        web,
        "avaliar_autorizacao_operacao_por_franquia",
        lambda _u: authz_payload,
    )
    monkeypatch.setattr(
        "app.cleide_audit_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _u: authz_payload,
    )
    monkeypatch.setattr(
        "app.julia_documents_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _u: authz_payload,
    )


def _fake_governed_generate(monkeypatch, *, text: str = "Resposta auditável simulada."):
    capture: dict = {}

    class _Resp:
        pass

    def _fake(client, model, contents, agent, flow_type, api_key_label):
        capture["client"] = client
        capture["model"] = model
        capture["contents"] = contents
        capture["agent"] = agent
        capture["flow_type"] = flow_type
        capture["api_key_label"] = api_key_label
        resp = _Resp()
        resp.text = text
        return resp

    monkeypatch.setattr(audit_chat, "cleiton_governed_generate_content", _fake)
    monkeypatch.setattr(audit_chat, "_get_client", lambda: object())
    return capture


def _chat(client, payload: dict):
    return client.post(
        "/api/cleide-auditoria/chat",
        json=payload,
        content_type="application/json",
    )


def _upload(client, filename: str, content: bytes, mime: str = "text/plain"):
    return client.post(
        "/api/cleide-auditoria/documents/upload",
        data={"file": (io.BytesIO(content), filename, mime)},
        content_type="multipart/form-data",
    )


@pytest.fixture
def web_client(app, tmp_path, monkeypatch, ctx):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    _authorized(monkeypatch, web)
    web.app.config["TESTING"] = True
    return web.app.test_client()


def test_anonymous_receives_401(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    anon = SimpleNamespace(is_authenticated=False)
    monkeypatch.setattr(web, "current_user", anon)
    monkeypatch.setattr("app.cleide_audit_routes.current_user", anon)
    client = web.app.test_client()
    resp = _chat(client, {"message": "auditar frete"})
    assert resp.status_code == 401
    body = resp.get_json()
    assert body["ok"] is False
    assert body["error"] == "auth_required"


def test_blocked_user_receives_403(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    upgrade_cta = {
        "error_code": "plan_limit_reached",
        "message": "Você atingiu o limite de uso do plano Free. Não pare agora! ",
        "message_suffix": " e continue criando sem interrupções.",
        "upgrade_url": "/contrate-um-plano",
        "upgrade_label": "Faça o upgrade",
    }
    _authorized(
        monkeypatch,
        web,
        authz={
            "permitido": False,
            "modo_operacao": "blocked",
            "mensagem_usuario": "Bloqueado.",
            "upgrade_cta": upgrade_cta,
        },
    )
    client = web.app.test_client()
    resp = _chat(client, {"message": "auditar frete"})
    assert resp.status_code == 403
    body = resp.get_json()
    assert body["ok"] is False
    assert body["error"] == "franquia_blocked"
    assert body["message"] == "Bloqueado."
    assert body["authorization"]["upgrade_cta"] == upgrade_cta


def test_blocked_user_chat_403_preserves_upgrade_cta(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    upgrade_cta = {
        "error_code": "plan_limit_reached",
        "message": "Você atingiu o limite de uso do plano Free. Não pare agora! ",
        "message_suffix": " e continue criando sem interrupções.",
        "upgrade_url": "/contrate-um-plano",
        "upgrade_label": "Faça o upgrade",
    }
    _authorized(
        monkeypatch,
        web,
        authz={
            "permitido": False,
            "modo_operacao": "blocked",
            "mensagem_usuario": "Limite atingido.",
            "upgrade_cta": upgrade_cta,
        },
    )
    client = web.app.test_client()
    resp = _chat(client, {"message": "quanto foi a divergência?"})
    assert resp.status_code == 403
    body = resp.get_json()
    assert body["authorization"]["upgrade_cta"]["upgrade_label"] == "Faça o upgrade"
    assert body["authorization"]["upgrade_cta"]["upgrade_url"] == "/contrate-um-plano"


def test_expired_user_receives_403(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    _authorized(
        monkeypatch,
        web,
        authz={
            "permitido": False,
            "modo_operacao": "blocked",
            "status_franquia": "expired",
            "mensagem_usuario": "Plano expirado.",
        },
    )
    client = web.app.test_client()
    resp = _chat(client, {"message": "auditar frete"})
    assert resp.status_code == 403
    assert resp.get_json()["message"] == "Plano expirado."


def test_degraded_user_allowed(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    _authorized(
        monkeypatch,
        web,
        authz={"permitido": True, "modo_operacao": "degraded", "status_franquia": "degraded"},
    )
    capture = _fake_governed_generate(monkeypatch)
    client = web.app.test_client()
    resp = _chat(client, {"message": "auditar frete"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert capture["agent"] == "cleide"


def test_ia_not_called_when_blocked(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    _authorized(
        monkeypatch,
        web,
        authz={"permitido": False, "modo_operacao": "blocked", "mensagem_usuario": "Bloqueado."},
    )
    chat_mock = MagicMock()
    monkeypatch.setattr(audit_chat, "cleiton_governed_generate_content", chat_mock)
    client = web.app.test_client()
    _chat(client, {"message": "auditar frete"})
    chat_mock.assert_not_called()


def test_missing_message_returns_400(web_client):
    resp = _chat(web_client, {})
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["ok"] is False
    assert body["error"] == "invalid_message"


def test_empty_message_returns_400(web_client):
    resp = _chat(web_client, {"message": "   "})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid_message"


def test_invalid_history_is_sanitized(web_client, monkeypatch):
    capture = _fake_governed_generate(monkeypatch)
    resp = _chat(
        web_client,
        {
            "message": "auditar frete",
            "history": ["invalid", {"role": "user", "content": "contexto anterior"}],
        },
    )
    assert resp.status_code == 200
    assert "contexto anterior" in str(capture["contents"])


def test_malformed_history_role_content_types_do_not_500(web_client, monkeypatch):
    capture = _fake_governed_generate(monkeypatch)
    resp = _chat(
        web_client,
        {
            "message": "auditar frete",
            "history": [{"role": 1, "content": 2}, {"role": "user", "content": "valido"}],
        },
    )
    assert resp.status_code == 200
    contents = str(capture["contents"])
    assert "valido" in contents
    assert "Usuário: 2" not in contents


def test_history_non_list_treated_as_empty(web_client, monkeypatch):
    capture = _fake_governed_generate(monkeypatch)
    resp = _chat(web_client, {"message": "auditar frete", "history": {"role": "user"}})
    assert resp.status_code == 200
    assert "Conversa recente:" in str(capture["contents"])


def test_unknown_history_role_is_ignored(web_client, monkeypatch):
    capture = _fake_governed_generate(monkeypatch)
    resp = _chat(
        web_client,
        {
            "message": "auditar frete",
            "history": [
                {"role": "system", "content": "nao deve entrar"},
                {"role": "user", "content": "entrada valida"},
            ],
        },
    )
    assert resp.status_code == 200
    contents = str(capture["contents"])
    assert "entrada valida" in contents
    assert "nao deve entrar" not in contents


def test_assistant_history_role_is_accepted(web_client, monkeypatch):
    capture = _fake_governed_generate(monkeypatch)
    resp = _chat(
        web_client,
        {
            "message": "auditar frete",
            "history": [{"role": "assistant", "content": "resposta anterior"}],
        },
    )
    assert resp.status_code == 200
    assert "resposta anterior" in str(capture["contents"])
    assert "Cleide:" in str(capture["contents"])


def test_sanitize_chat_history_unit_cases():
    assert audit_chat.sanitize_chat_history(None) == []
    assert audit_chat.sanitize_chat_history({"role": "user"}) == []
    assert audit_chat.sanitize_chat_history([{"role": 1, "content": "x"}]) == []
    assert audit_chat.sanitize_chat_history([{"role": "user", "content": 2}]) == []
    assert audit_chat.sanitize_chat_history([{"role": "tool", "content": "x"}]) == []
    assert audit_chat.sanitize_chat_history(
        [{"role": "model", "content": "ja normalizado"}]
    ) == [{"role": "model", "content": "ja normalizado"}]
    cleaned = audit_chat.sanitize_chat_history(
        [{"role": "user", "content": "  ok  "}, {"role": "assistant", "content": "resp"}]
    )
    assert cleaned == [{"role": "user", "content": "ok"}, {"role": "model", "content": "resp"}]


def test_normalize_chat_request_id_unit_cases():
    assert audit_chat.normalize_chat_request_id(None)
    assert len(audit_chat.normalize_chat_request_id(None)) == 32
    assert audit_chat.normalize_chat_request_id("  req-1  ") == "req-1"
    assert len(audit_chat.normalize_chat_request_id("   ")) == 32
    assert len(audit_chat.normalize_chat_request_id(123)) == 32
    assert len(audit_chat.normalize_chat_request_id(["x"])) == 32


def test_request_id_non_string_does_not_500(web_client, monkeypatch):
    chat_mock = MagicMock()
    monkeypatch.setattr(audit_chat, "cleiton_governed_generate_content", chat_mock)
    monkeypatch.setattr(audit_chat, "_get_client", lambda: object())

    class _Resp:
        text = "ok"

    chat_mock.return_value = _Resp()
    resp = _chat(web_client, {"message": "auditar frete", "request_id": 123})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    chat_mock.assert_called_once()


def test_request_id_non_string_does_not_reuse_numeric_idempotency(web_client, monkeypatch):
    calls = {"count": 0}

    class _Resp:
        text = "ok"

    def _fake(*args, **kwargs):
        calls["count"] += 1
        return _Resp()

    monkeypatch.setattr(audit_chat, "cleiton_governed_generate_content", _fake)
    monkeypatch.setattr(audit_chat, "_get_client", lambda: object())
    first = _chat(web_client, {"message": "auditar frete", "request_id": 123})
    second = _chat(web_client, {"message": "auditar frete", "request_id": 123})
    assert first.status_code == 200
    assert second.status_code == 200
    assert calls["count"] == 2
    assert second.get_json().get("cached") is not True


def test_request_id_accepted_and_idempotent(web_client, monkeypatch):
    capture = _fake_governed_generate(monkeypatch)
    payload = {"message": "auditar frete", "request_id": "req-chat-001"}
    first = _chat(web_client, payload)
    second = _chat(web_client, payload)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.get_json()["answer"] == second.get_json()["answer"]
    assert second.get_json().get("cached") is True
    assert capture["agent"] == "cleide"
    assert capture["flow_type"] == CLEIDE_AUDIT_CHAT_FLOW_TYPE


def test_governed_generate_content_used(web_client, monkeypatch):
    capture = _fake_governed_generate(monkeypatch, text="Análise operacional.")
    resp = _chat(web_client, {"message": "conferir cobrança de frete"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["answer"] == "Análise operacional."
    assert capture["agent"] == "cleide"
    assert capture["flow_type"] == CLEIDE_AUDIT_CHAT_FLOW_TYPE


def test_does_not_call_gemini_directly(web_client, monkeypatch):
    capture = _fake_governed_generate(monkeypatch)

    class _Client:
        models = MagicMock()

    client_obj = _Client()
    monkeypatch.setattr(audit_chat, "_get_client", lambda: client_obj)
    resp = _chat(web_client, {"message": "auditar frete"})
    assert resp.status_code == 200
    client_obj.models.generate_content.assert_not_called()
    assert capture["agent"] == "cleide"


def test_no_manual_billing_on_chat():
    chat_source = inspect.getsource(audit_chat)
    routes_source = inspect.getsource(audit_routes)
    for token in (
        "apropriar_billing",
        "registrar_fato",
        "monetizacao",
        "cleiton_upload_billing_service",
    ):
        assert token not in chat_source
        assert token not in routes_source


def test_uses_cleide_document_context_builder(web_client, monkeypatch):
    capture_ctx: dict = {}

    def _fake_ctx(session_obj):
        capture_ctx["called"] = True
        return {
            "context_block": "Contexto documental temporário da Cleide Auditoria:\n\nDocumento 1:\n- Conteúdo preparado:\n  evidencia frete",
            "gemini_file_parts": [],
            "has_documents": True,
            "flow_type": CLEIDE_AUDIT_CHAT_FLOW_TYPE,
            "meta": {"documents": [{"display_name": "nota.txt", "doc_type": "txt"}]},
        }

    monkeypatch.setattr(audit_routes, "build_cleide_audit_document_context_for_chat", _fake_ctx)
    capture = _fake_governed_generate(monkeypatch)
    resp = _chat(web_client, {"message": "o que diz o documento?"})
    assert resp.status_code == 200
    assert capture_ctx.get("called") is True
    assert "evidencia frete" in str(capture["contents"])
    assert resp.get_json()["documents_used"][0]["display_name"] == "nota.txt"


def test_works_without_documents(web_client, monkeypatch):
    capture = _fake_governed_generate(monkeypatch, text="Sem anexos, oriente upload.")
    resp = _chat(web_client, {"message": "auditar frete sem documentos"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["documents_used"] == []
    assert body["flow_type"] == CLEIDE_AUDIT_CHAT_FLOW_TYPE
    assert "Cleide" in str(capture["contents"])


def test_works_with_uploaded_documents(web_client, monkeypatch):
    _upload(web_client, "cte.txt", make_txt("valor frete 999.00"))
    capture = _fake_governed_generate(monkeypatch)
    resp = _chat(web_client, {"message": "há divergência?"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert len(body["documents_used"]) == 1
    assert "valor frete 999.00" in str(capture["contents"])


def test_pdf_ready_is_sent_as_file_context_to_model(web_client, monkeypatch):
    monkeypatch.setattr(
        "app.cleide_audit_doc_service.upload_pdf_to_gemini_files_api",
        lambda **kwargs: SimpleNamespace(
            ok=True,
            gemini_file_name="files/pdf-1",
            gemini_file_uri="gs://bucket/pdf-1",
            gemini_mime_type="application/pdf",
            gemini_file_state="ACTIVE",
            gemini_uploaded_at="2026-06-09T10:00:00",
            prepared_context='{"strategy":"gemini_file_api","gemini_file_ready":true}',
            warnings=[],
            error_summary=None,
        ),
    )
    monkeypatch.setattr(
        "app.cleide_audit_doc_context.build_gemini_file_part_for_generate",
        lambda record: {"pdf": record.get("gemini_file_name")},
    )
    _upload(web_client, "tabela.pdf", b"%PDF-1.4\n/Type /Page\nconteudo", "application/pdf")
    capture = _fake_governed_generate(monkeypatch)

    resp = _chat(web_client, {"message": "Me fale sobre essa tabela de frete anexada."})

    assert resp.status_code == 200
    assert isinstance(capture["contents"], list)
    assert capture["contents"][0] == {"pdf": "files/pdf-1"}
    assert "fase futura multimodal" not in str(capture["contents"]).lower()


def test_julia_documents_not_used_in_chat(web_client, monkeypatch):
    web_client.post(
        "/api/julia/documents/upload",
        data={
            "file": (
                io.BytesIO(make_txt("CONTEUDO-JULIA-EXCLUSIVO")),
                "julia.txt",
                "text/plain",
            )
        },
        content_type="multipart/form-data",
    )
    _upload(web_client, "audit.txt", make_txt("cleide audit"))
    capture = _fake_governed_generate(monkeypatch)
    resp = _chat(web_client, {"message": "analisar"})
    assert resp.status_code == 200
    contents = str(capture["contents"])
    assert "cleide audit" in contents
    assert "CONTEUDO-JULIA-EXCLUSIVO" not in contents


def test_chat_module_isolated_from_julia():
    routes_source = inspect.getsource(audit_routes)
    chat_source = inspect.getsource(audit_chat)
    assert "julia_doc_context" not in routes_source
    assert "julia_doc_context" not in chat_source
    assert "get_cleiton_doc_ids" not in routes_source
    assert "get_cleiton_doc_ids" not in chat_source
    assert "run_julia_chat" not in routes_source
    assert "run_julia_chat" not in chat_source


def test_chat_does_not_reference_legacy_cleide_endpoints():
    routes_source = inspect.getsource(audit_routes)
    chat_source = inspect.getsource(audit_chat)
    for token in (
        "/api/chat_julia",
        "/api/chat_cleide",
        "/api/cleide/upload",
        "cleide_routes",
        "cleide_upload_pipeline",
    ):
        assert token not in routes_source
        assert token not in chat_source


def test_no_regex_in_chat_flow():
    for mod in (audit_routes, audit_chat):
        source = inspect.getsource(mod)
        assert "import re" not in source
        assert "re.compile" not in source
        assert "re.search" not in source
        assert "re.match" not in source


def test_no_rigid_template_validation():
    chat_source = inspect.getsource(audit_chat)
    for token in (
        "required_columns",
        "template_fixo",
        "parse_spreadsheet",
        "re.compile",
        "COLUMN_",
    ):
        assert token not in chat_source


def test_chat_endpoint_registered(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    rules = {rule.rule for rule in web.app.url_map.iter_rules()}
    assert "/api/cleide-auditoria/chat" in rules


def test_document_upload_still_works(web_client):
    resp = _upload(web_client, "nota.txt", make_txt("upload ok"))
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_julia_documents_still_work(web_client):
    resp = web_client.post(
        "/api/julia/documents/upload",
        data={"file": (io.BytesIO(make_txt("julia ok")), "julia.txt", "text/plain")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    assert resp.get_json()["session"]["count"] == 1


def test_chat_disabled_blocks_without_calling_ia(web_client, monkeypatch):
    _patch_audit_cfg(monkeypatch, chat_enabled=False)
    chat_mock = MagicMock()
    monkeypatch.setattr(audit_chat, "cleiton_governed_generate_content", chat_mock)
    resp = _chat(web_client, {"message": "auditar frete"})
    assert resp.status_code == 403
    body = resp.get_json()
    assert body["ok"] is False
    assert body["error"] == "chat_disabled"
    chat_mock.assert_not_called()


def test_question_above_limit_returns_400_without_calling_ia(web_client, monkeypatch):
    _patch_audit_cfg(monkeypatch, question_max_chars=20)
    chat_mock = MagicMock()
    monkeypatch.setattr(audit_chat, "cleiton_governed_generate_content", chat_mock)
    resp = _chat(web_client, {"message": "x" * 25})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid_message"
    chat_mock.assert_not_called()


def test_chat_max_history_from_service_limits_backend(web_client, monkeypatch):
    _patch_audit_cfg(monkeypatch, chat_max_history=2)
    capture = _fake_governed_generate(monkeypatch)
    history = [{"role": "user", "content": f"msg-{i}"} for i in range(1, 8)]
    resp = _chat(web_client, {"message": "auditar frete", "history": history})
    assert resp.status_code == 200
    contents = str(capture["contents"])
    assert "msg-1" not in contents
    assert "msg-6" in contents
    assert "msg-7" in contents


def test_allow_guided_permits_chat_without_documents(web_client, monkeypatch):
    _patch_audit_cfg(monkeypatch, no_documents_behavior="allow_guided")
    capture = _fake_governed_generate(monkeypatch, text="Oriente upload.")
    resp = _chat(web_client, {"message": "auditar sem anexos"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert capture["agent"] == "cleide"


def test_require_documents_blocks_without_calling_ia(web_client, monkeypatch):
    _patch_audit_cfg(monkeypatch, no_documents_behavior="require_documents")
    chat_mock = MagicMock()
    monkeypatch.setattr(audit_chat, "cleiton_governed_generate_content", chat_mock)
    resp = _chat(web_client, {"message": "auditar frete"})
    assert resp.status_code == 403
    body = resp.get_json()
    assert body["error"] == "documents_required"
    chat_mock.assert_not_called()


def test_require_documents_allows_chat_with_documents(web_client, monkeypatch):
    _patch_audit_cfg(monkeypatch, no_documents_behavior="require_documents")
    _upload(web_client, "cte.txt", make_txt("valor frete 100"))
    capture = _fake_governed_generate(monkeypatch)
    resp = _chat(web_client, {"message": "analisar documento"})
    assert resp.status_code == 200
    assert capture["agent"] == "cleide"


FALLBACK_CUSTOM = "Mensagem personalizada da Cleide."


def test_fallback_message_used_on_processing_failure(web_client, monkeypatch):
    _patch_audit_cfg(monkeypatch, fallback_message=FALLBACK_CUSTOM)

    def _fail(*_args, **_kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(audit_chat, "cleiton_governed_generate_content", _fail)
    monkeypatch.setattr(audit_chat, "_get_client", lambda: object())
    resp = _chat(web_client, {"message": "auditar frete"})
    assert resp.status_code == 500
    body = resp.get_json()
    assert body["error"] == "processing_failed"
    assert body["message"] == FALLBACK_CUSTOM


def test_fallback_message_not_used_on_service_unavailable(web_client, monkeypatch):
    _patch_audit_cfg(monkeypatch, fallback_message=FALLBACK_CUSTOM)
    monkeypatch.setattr(audit_chat, "_get_client", lambda: None)
    resp = _chat(web_client, {"message": "auditar frete"})
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["error"] == "service_unavailable"
    assert body["message"] != FALLBACK_CUSTOM


def test_fallback_message_not_used_on_auth_required(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    _patch_audit_cfg(monkeypatch, fallback_message=FALLBACK_CUSTOM)
    web = _load_web_module()
    anon = SimpleNamespace(is_authenticated=False)
    monkeypatch.setattr(web, "current_user", anon)
    monkeypatch.setattr("app.cleide_audit_routes.current_user", anon)
    resp = _chat(web.app.test_client(), {"message": "auditar frete"})
    assert resp.status_code == 401
    assert resp.get_json()["message"] != FALLBACK_CUSTOM


def test_fallback_message_not_used_on_franquia_blocked(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    _patch_audit_cfg(monkeypatch, fallback_message=FALLBACK_CUSTOM)
    web = _load_web_module()
    _authorized(
        monkeypatch,
        web,
        authz={"permitido": False, "modo_operacao": "blocked", "mensagem_usuario": "Bloqueado."},
    )
    resp = _chat(web.app.test_client(), {"message": "auditar frete"})
    assert resp.status_code == 403
    body = resp.get_json()
    assert body["message"] == "Bloqueado."
    assert body["message"] != FALLBACK_CUSTOM


def test_fallback_message_not_used_when_chat_disabled(web_client, monkeypatch):
    _patch_audit_cfg(monkeypatch, chat_enabled=False, fallback_message=FALLBACK_CUSTOM)
    resp = _chat(web_client, {"message": "auditar frete"})
    assert resp.status_code == 403
    body = resp.get_json()
    assert body["error"] == "chat_disabled"
    assert body["message"] != FALLBACK_CUSTOM


def test_fallback_message_not_used_when_question_exceeds_limit(web_client, monkeypatch):
    _patch_audit_cfg(monkeypatch, question_max_chars=10, fallback_message=FALLBACK_CUSTOM)
    resp = _chat(web_client, {"message": "x" * 20})
    assert resp.status_code == 400
    assert resp.get_json()["message"] != FALLBACK_CUSTOM


def test_fallback_message_not_used_when_documents_required(web_client, monkeypatch):
    _patch_audit_cfg(
        monkeypatch,
        no_documents_behavior="require_documents",
        fallback_message=FALLBACK_CUSTOM,
    )
    resp = _chat(web_client, {"message": "auditar frete"})
    assert resp.status_code == 403
    body = resp.get_json()
    assert body["error"] == "documents_required"
    assert body["message"] != FALLBACK_CUSTOM


def test_fallback_message_not_used_on_successful_ia_response(web_client, monkeypatch):
    _patch_audit_cfg(monkeypatch, fallback_message=FALLBACK_CUSTOM)
    _fake_governed_generate(monkeypatch, text="Resposta real da Cleide.")
    resp = _chat(web_client, {"message": "auditar frete"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["answer"] == "Resposta real da Cleide."
    assert body["answer"] != FALLBACK_CUSTOM


def test_upload_disabled_does_not_block_chat(web_client, monkeypatch):
    _patch_audit_cfg(monkeypatch, upload_enabled=False, chat_enabled=True)
    capture = _fake_governed_generate(monkeypatch, text="Chat ativo sem upload.")
    resp = _chat(web_client, {"message": "auditar frete"})
    assert resp.status_code == 200
    assert resp.get_json()["answer"] == "Chat ativo sem upload."
    assert capture["agent"] == "cleide"


def test_show_documents_used_false_omits_documents_used(web_client, monkeypatch):
    _patch_audit_cfg(monkeypatch, show_documents_used=False)
    _upload(web_client, "nota.txt", make_txt("evidencia"))
    _fake_governed_generate(monkeypatch)
    resp = _chat(web_client, {"message": "analisar"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert "documents_used" not in body


def test_show_documents_used_true_keeps_documents_used(web_client, monkeypatch):
    _patch_audit_cfg(monkeypatch, show_documents_used=True)
    _upload(web_client, "nota.txt", make_txt("evidencia"))
    _fake_governed_generate(monkeypatch)
    resp = _chat(web_client, {"message": "analisar"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert len(body["documents_used"]) == 1


def test_runtime_reads_cleide_audit_config_not_cleide_bi(monkeypatch):
    routes_source = inspect.getsource(audit_routes)
    chat_source = inspect.getsource(audit_chat)
    assert "get_cleide_audit_config" in routes_source
    assert "get_cleide_audit_config" in chat_source
    assert "get_cleide_config" not in routes_source
    assert "get_cleide_config" not in chat_source
    assert "cleide_cfg_" not in routes_source
    assert "cleide_cfg_" not in chat_source


def test_runtime_does_not_reference_julia_config(monkeypatch):
    for source in (inspect.getsource(audit_routes), inspect.getsource(audit_chat)):
        assert "julia_chat_max_history" not in source
        assert "julia_doc_context" not in source
        assert "run_julia_chat" not in source
