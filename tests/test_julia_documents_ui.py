"""Testes de UI/contrato frontend documental da Júlia (Fase 5)."""
from __future__ import annotations

import importlib
import os
import pathlib
from types import SimpleNamespace

import pytest

from app.cleiton_doc_contracts import ERROR_MAX_FILES, FIELD_PREPARED_CONTEXT


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


def _operational_client(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=True))
    monkeypatch.setattr(web, "get_julia_chat_max_history", lambda: 10)
    monkeypatch.setattr(
        web,
        "avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True},
    )
    return web.app.test_client()


@pytest.fixture
def js_source():
    return pathlib.Path("app/static/js/julia_documents.js").read_text(encoding="utf-8")


@pytest.fixture
def chat_js_source():
    return pathlib.Path("app/static/js/chat_behavior.js").read_text(encoding="utf-8")


def test_operational_page_renders_attach_button(monkeypatch):
    client = _operational_client(monkeypatch)
    resp = client.get("/chat_julia?mode=operational")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'id="juliaChatAttachBtn"' in html
    assert 'aria-label="Anexar documento à conversa"' in html
    assert "julia_documents.js" in html
    assert "window.JULIA_DOCUMENTS_UI = true" in html


def test_home_discovery_does_not_render_attach_button(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=False))
    monkeypatch.setattr(web, "get_julia_chat_max_history", lambda: 10)
    monkeypatch.setattr(web, "avaliar_autorizacao_operacao_por_franquia", lambda _u: {"permitido": True})
    resp = web.app.test_client().get("/")
    html = resp.get_data(as_text=True)
    assert 'id="juliaChatAttachBtn"' not in html
    assert "julia_documents.js" not in html
    assert "ONBOARDING_DISCOVERY_MODE = true" in html


def test_authenticated_home_without_documents_ui(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=True))
    monkeypatch.setattr(web, "get_julia_chat_max_history", lambda: 10)
    monkeypatch.setattr(web, "avaliar_autorizacao_operacao_por_franquia", lambda _u: {"permitido": True})
    resp = web.app.test_client().get("/")
    html = resp.get_data(as_text=True)
    assert 'id="juliaChatAttachBtn"' not in html
    assert "julia_documents.js" not in html


def test_js_upload_calls_correct_endpoint(js_source):
    assert "var API_UPLOAD = '/api/julia/documents/upload'" in js_source
    assert "fetch(API_UPLOAD" in js_source
    assert "formData.append('file', file" in js_source


def test_js_list_refresh_after_success(js_source):
    assert "fetchDocuments()" in js_source
    assert "renderDocuments(res.data.documents" in js_source or "renderDocuments(documents)" in js_source


def test_js_friendly_error_messages(js_source):
    assert "cleiton_doc_max_files" in js_source
    assert "Você atingiu o limite de documentos desta sessão." in js_source
    assert "function friendlyError" in js_source


def test_js_list_does_not_render_prepared_context(js_source):
    assert FIELD_PREPARED_CONTEXT not in js_source
    assert "prepared_context" not in js_source


def test_js_remove_calls_delete_endpoint(js_source):
    assert "method: 'DELETE'" in js_source
    assert "encodeURIComponent(docId)" in js_source


def test_js_clear_calls_correct_endpoint(js_source):
    assert "var API_CLEAR = '/api/julia/documents/clear'" in js_source
    assert "fetch(API_CLEAR" in js_source


def test_js_pdf_placeholder_not_analyzed(js_source):
    assert "Gemini File API será ativada em etapa posterior" in js_source
    assert "analisado" not in js_source.lower()


def test_chat_js_does_not_send_document_context(chat_js_source):
    assert "document_context" not in chat_js_source
    assert "prepared_context" not in chat_js_source
    payload_section = chat_js_source.split("var payload = { message: text, history: history }")[1][:400]
    assert "document" not in payload_section.lower()


def test_js_does_not_read_file_content(js_source):
    assert "FileReader" not in js_source
    assert "readAsText" not in js_source
    assert "readAsDataURL" not in js_source


def test_js_attach_button_accessibility(js_source):
    assert "aria-label" in js_source
    assert "aria-busy" in js_source


def test_js_only_loads_when_flag_set(js_source):
    assert "window.JULIA_DOCUMENTS_UI" in js_source
    assert "window.JULIA_DOCUMENTS_UI !== true" in js_source
    assert "(function ()" in js_source


def test_max_files_error_message_in_js(js_source):
    assert ERROR_MAX_FILES in js_source


def test_operational_template_has_documents_hint(monkeypatch):
    client = _operational_client(monkeypatch)
    html = client.get("/chat_julia?mode=operational").get_data(as_text=True)
    assert "Contexto documental temporário" in html
    assert 'id="juliaDocumentsClearBtn"' in html
