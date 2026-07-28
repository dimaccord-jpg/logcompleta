"""Gate de autenticação/franquia das APIs do AgenteCompara."""
from __future__ import annotations

import importlib
import io
import os
from types import SimpleNamespace

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
    monkeypatch.setattr("app.agente_compara_api_routes.get_agente_compara_config", lambda: cfg)
    monkeypatch.setattr("app.agente_compara_doc_service.get_agente_compara_config", lambda: cfg)
    return cfg


def _setup_doc_env(monkeypatch, tmp_path, **cfg_overrides):
    patch_cleiton_doc_store(tmp_path, monkeypatch)
    cfg = patch_cleiton_doc_cfg(monkeypatch, **cfg_overrides)
    monkeypatch.setattr("app.agente_compara_doc_service.get_cleiton_doc_config", lambda: cfg)
    monkeypatch.setattr("app.agente_compara_api_routes.get_cleiton_doc_config", lambda: cfg)
    _patch_ac_cfg(monkeypatch)
    return cfg


def _authorized(monkeypatch, web, *, authz=None):
    fake_user = SimpleNamespace(is_authenticated=True, conta_id=1, franquia_id=1)
    monkeypatch.setattr(web, "current_user", fake_user)
    monkeypatch.setattr("app.agente_compara_api_routes.current_user", fake_user)
    authz_payload = authz or {"permitido": True, "modo_operacao": "normal"}
    monkeypatch.setattr(
        web,
        "avaliar_autorizacao_operacao_por_franquia",
        lambda _u: authz_payload,
    )
    monkeypatch.setattr(
        "app.agente_compara_api_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _u: authz_payload,
    )


def _upload(client, filename: str, content: bytes, mime: str = "text/csv", *, carrier_name: str = "Transportadora Teste"):
    return client.post(
        "/api/agente-compara/documents/upload",
        data={
            "file": (io.BytesIO(content), filename, mime),
            "carrier_name": carrier_name,
        },
        content_type="multipart/form-data",
    )


_OPERATIONAL_ENDPOINTS = (
    ("GET", "/api/agente-compara/documents/status"),
    ("POST", "/api/agente-compara/chat"),
    ("POST", "/api/agente-compara/coverage/upload"),
    ("POST", "/api/agente-compara/audit/upload"),
    ("POST", "/api/agente-compara/audit/run"),
    ("POST", "/api/agente-compara/audit-chat"),
    ("POST", "/api/agente-compara/comparison/reset"),
    ("POST", "/api/agente-compara/comparison/start"),
)


def test_anonymous_receives_401_on_sample_endpoints(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    anon = SimpleNamespace(is_authenticated=False)
    monkeypatch.setattr(web, "current_user", anon)
    monkeypatch.setattr("app.agente_compara_api_routes.current_user", anon)
    client = web.app.test_client()

    assert _upload(client, "a.csv", make_csv([["a"], ["1"]])).status_code == 401
    assert client.get("/api/agente-compara/documents/status").status_code == 401

    for method, path in _OPERATIONAL_ENDPOINTS:
        if method == "GET":
            resp = client.get(path)
        else:
            resp = client.post(path, json={}, content_type="application/json")
        assert resp.status_code == 401, path
        body = resp.get_json()
        assert body["error_code"] == "auth_required"


def test_authorized_user_passes_auth_gate(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    _authorized(monkeypatch, web, authz={"permitido": True, "modo_operacao": "normal"})
    client = web.app.test_client()

    status = client.get("/api/agente-compara/documents/status")
    assert status.status_code not in (401, 403)
    assert status.status_code == 200

    # Sem arquivo / sem lote: pode falhar depois do auth — só não pode ser 401/403 de franquia.
    for method, path in _OPERATIONAL_ENDPOINTS:
        if path.endswith("/documents/status"):
            continue
        if method == "GET":
            resp = client.get(path)
        else:
            resp = client.post(path, json={}, content_type="application/json")
        assert resp.status_code not in (401, 403), path


def test_blocked_user_receives_403(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    _authorized(
        monkeypatch,
        web,
        authz={
            "permitido": False,
            "modo_operacao": "blocked",
            "mensagem_usuario": "Bloqueado.",
        },
    )
    client = web.app.test_client()
    resp = client.get("/api/agente-compara/documents/status")
    assert resp.status_code == 403
    assert resp.get_json()["error_code"] == "franquia_blocked"
    assert _upload(client, "a.csv", make_csv([["a"], ["1"]])).status_code == 403


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
            "mensagem_usuario": "Expirado.",
        },
    )
    client = web.app.test_client()
    resp = client.get("/api/agente-compara/documents/status")
    assert resp.status_code == 403
    assert resp.get_json()["error_code"] == "franquia_blocked"


def test_not_permitido_receives_403(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    _authorized(
        monkeypatch,
        web,
        authz={"permitido": False, "modo_operacao": "blocked", "mensagem_usuario": "Negado."},
    )
    client = web.app.test_client()
    for method, path in _OPERATIONAL_ENDPOINTS:
        if method == "GET":
            resp = client.get(path)
        else:
            resp = client.post(path, json={}, content_type="application/json")
        assert resp.status_code == 403, path
