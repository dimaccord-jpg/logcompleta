"""Smoke de upload documental do AgenteCompara (sessão isolada da Cleide)."""
from __future__ import annotations

import importlib
import io
import os
from types import SimpleNamespace

import pytest

from app.agente_compara_doc_service import AGENTE_COMPARA_DOC_IDS_SESSION_KEY
from app.cleide_audit_doc_service import CLEIDE_AUDIT_DOC_IDS_SESSION_KEY
from app.services.agente_compara_config_service import (
    AgenteComparaConfig,
    DEFAULT_FALLBACK_MESSAGE,
)
from tests.cleiton_doc_fixtures import make_csv, patch_cleiton_doc_cfg, patch_cleiton_doc_store


def _default_ac_cfg(**overrides):
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
    return AgenteComparaConfig(**defaults)


def _patch_ac_cfg(monkeypatch, **overrides):
    cfg = _default_ac_cfg(**overrides)
    for target in (
        "app.agente_compara_api_routes.get_agente_compara_config",
        "app.agente_compara_doc_service.get_agente_compara_config",
    ):
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


def _upload(client, filename: str, content: bytes, mime: str = "text/csv"):
    return client.post(
        "/api/agente-compara/documents/upload",
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


def test_csv_upload_lands_in_agente_compara_session_key(web_client, app):
    content = make_csv([["col_a", "col_b"], ["1", "2"]])
    with web_client.session_transaction() as sess:
        sess[CLEIDE_AUDIT_DOC_IDS_SESSION_KEY] = ["cleide-keep"]

    resp = _upload(web_client, "dados.csv", content, "text/csv")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    doc_id = body["document"]["doc_id"]
    assert body["document"]["doc_type"] == "csv"

    with web_client.session_transaction() as sess:
        assert doc_id in (sess.get(AGENTE_COMPARA_DOC_IDS_SESSION_KEY) or [])
        assert CLEIDE_AUDIT_DOC_IDS_SESSION_KEY in sess
        assert sess[CLEIDE_AUDIT_DOC_IDS_SESSION_KEY] == ["cleide-keep"]
        assert doc_id not in (sess.get(CLEIDE_AUDIT_DOC_IDS_SESSION_KEY) or [])
