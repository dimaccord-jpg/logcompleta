"""Testes de UI/contrato frontend documental da Júlia (Fase 5)."""
from __future__ import annotations

import importlib
import os
import pathlib
from types import SimpleNamespace

import pytest
from flask_login import UserMixin

from app.cleiton_doc_contracts import ERROR_MAX_FILES, FIELD_PREPARED_CONTEXT


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


class _AuthUser(UserMixin):
    def __init__(self, user_id: str = "123", is_admin: bool = False):
        self.id = user_id
        self.is_admin = is_admin
        self.conta_id = 1
        self.franquia_id = 1
        self.email = "tester@example.com"
        self.full_name = "Tester User"


def _force_login(client, web, monkeypatch, *, is_admin: bool = False):
    user = _AuthUser(is_admin=is_admin)
    monkeypatch.setattr(web, "load_user_for_flask_login", lambda _user_id: user)
    with client.session_transaction() as sess:
        sess["_user_id"] = user.get_id()
        sess["_fresh"] = True
    return user


def _operational_client(monkeypatch, *, is_admin: bool = False, force_login: bool = False):
    web = _load_web_module()
    monkeypatch.setattr(
        web,
        "current_user",
        SimpleNamespace(is_authenticated=True, is_admin=is_admin),
    )
    monkeypatch.setattr(web, "get_julia_chat_max_history", lambda: 10)
    monkeypatch.setattr(
        web,
        "avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True},
    )
    client = web.app.test_client()
    if force_login:
        _force_login(client, web, monkeypatch, is_admin=is_admin)
    return client


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
    assert 'id="juliaChatComposer"' in html
    assert 'class="julia-chat-attach-icon"' in html
    assert ">+<" in html.replace(" ", "") or 'julia-chat-attach-icon' in html
    assert 'data-julia-documents="true"' in html
    assert 'aria-label="Abrir menu de ações"' in html
    assert 'aria-expanded="false"' in html
    assert 'id="juliaChatActionsMenu"' in html
    assert 'accept=".txt,.xml,.csv,.xlsx,.docx,.pdf"' in html
    assert "julia_documents.js" in html
    assert "window.JULIA_DOCUMENTS_UI = true" in html
    assert html.count('<span class="af-text-gradient">Agentefrete</span>') == 1


def test_attach_button_inside_composer(monkeypatch):
    client = _operational_client(monkeypatch)
    html = client.get("/chat_julia?mode=operational").get_data(as_text=True)
    composer_start = html.index('id="juliaChatComposer"')
    composer_chunk = html[composer_start:composer_start + 1200]
    assert 'id="juliaChatAttachBtn"' in composer_chunk
    assert 'id="juliaChatInput"' in composer_chunk
    assert 'id="juliaChatSend"' in composer_chunk


def test_home_discovery_does_not_render_attach_button(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=False))
    monkeypatch.setattr(web, "get_julia_chat_max_history", lambda: 10)
    monkeypatch.setattr(web, "avaliar_autorizacao_operacao_por_franquia", lambda _u: {"permitido": True})
    resp = web.app.test_client().get("/")
    html = resp.get_data(as_text=True)
    assert 'id="juliaChatAttachBtn"' not in html
    assert 'id="juliaChatActionsMenu"' not in html
    assert "julia_documents.js" not in html
    assert "ONBOARDING_DISCOVERY_MODE = true" in html


def test_authenticated_home_renders_attach_button(monkeypatch):
    client = _operational_client(monkeypatch)
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'id="juliaChatAttachBtn"' in html
    assert 'id="juliaChatComposer"' in html
    assert 'class="julia-chat-attach-icon"' in html
    assert 'data-julia-documents="true"' in html
    assert 'aria-label="Abrir menu de ações"' in html
    assert 'aria-expanded="false"' in html
    assert 'id="juliaChatActionsMenu"' in html
    assert 'accept=".txt,.xml,.csv,.xlsx,.docx,.pdf"' in html
    assert "julia_documents.js" in html
    assert "window.JULIA_DOCUMENTS_UI = true" in html
    assert "ONBOARDING_DISCOVERY_MODE = true" not in html


def test_authenticated_home_get_root_validates_documents_ui_contract(monkeypatch):
    client = _operational_client(monkeypatch)
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert 'aria-label="Abrir menu de ações"' in html
    assert 'aria-expanded="false"' in html
    assert 'data-julia-documents="true"' in html
    assert 'accept=".txt,.xml,.csv,.xlsx,.docx,.pdf"' in html
    assert "julia_documents.js" in html
    assert "window.JULIA_DOCUMENTS_UI = true" in html

    composer_start = html.index('id="juliaChatComposerWrap"')
    composer_chunk = html[composer_start:composer_start + 2800]
    assert 'id="juliaChatAttachBtn"' in composer_chunk
    assert ">+<" in composer_chunk.replace(" ", "") or 'julia-chat-attach-icon' in composer_chunk


def test_authenticated_home_attach_button_inside_composer(monkeypatch):
    client = _operational_client(monkeypatch)
    html = client.get("/").get_data(as_text=True)
    composer_start = html.index('id="juliaChatComposer"')
    composer_chunk = html[composer_start:composer_start + 1200]
    assert 'id="juliaChatAttachBtn"' in composer_chunk
    assert 'id="juliaChatInput"' in composer_chunk
    assert 'id="juliaChatSend"' in composer_chunk


def test_authenticated_home_has_documents_area(monkeypatch):
    client = _operational_client(monkeypatch)
    html = client.get("/").get_data(as_text=True)
    assert 'id="juliaDocumentsClearBtn"' in html
    assert 'id="juliaDocumentsArea"' in html
    input_pos = html.index('id="juliaChatInput"')
    docs_pos = html.index('id="juliaDocumentsArea"')
    assert input_pos < docs_pos


def test_operational_home_sem_ctas_onboarding(monkeypatch):
    client = _operational_client(monkeypatch)
    html = client.get("/").get_data(as_text=True)
    assert 'id="onboardingCtaGrid"' not in html
    assert 'id="juliaChatDiscoverySuggestions"' not in html


def test_operational_home_julia_welcome_typewriter_contract(monkeypatch):
    client = _operational_client(monkeypatch)
    html = client.get("/").get_data(as_text=True)
    assert 'id="juliaWelcomeMessage"' in html
    assert 'data-typewriter-enabled="true"' in html
    assert 'data-typewriter-text="Faça uma pergunta sobre logística..."' in html
    assert "chat_behavior.js" in html
    assert "Como estruturar um plano de redução de custo logístico" not in html


def test_authenticated_home_renders_actions_submenu(monkeypatch):
    client = _operational_client(monkeypatch, force_login=True)
    html = client.get("/").get_data(as_text=True)
    assert 'id="juliaChatActionsMenu"' in html
    assert 'id="juliaChatUploadItem"' in html
    assert "Enviar arquivos" in html
    assert 'class="julia-actions-divider"' in html
    assert "Auditoria de Frete" in html
    assert "Compare Tabelas" in html
    menu_start = html.index('id="juliaChatActionsMenu"')
    menu_chunk = html[menu_start:menu_start + 2800]
    assert "Previsibilidade Frete" not in menu_chunk
    assert "BI Cleide" not in menu_chunk
    assert "Controle de Estoque" not in menu_chunk
    assert "Feed" in menu_chunk
    assert "/cleide-bi-frete" not in menu_chunk
    assert "/auditoria-frete" in menu_chunk
    assert "/agente-compara" in menu_chunk
    assert "Área do Usuário" not in menu_chunk
    assert ">Sair<" not in menu_chunk.replace(" ", "")
    assert "Home / Notícias" not in menu_chunk


def test_authenticated_home_admin_renders_full_actions_submenu(monkeypatch):
    client = _operational_client(monkeypatch, is_admin=True, force_login=True)
    html = client.get("/").get_data(as_text=True)
    menu_start = html.index('id="juliaChatActionsMenu"')
    menu_chunk = html[menu_start:menu_start + 2800]
    assert "Enviar arquivos" in menu_chunk
    assert "Home" in menu_chunk
    assert "Auditoria de Frete" in menu_chunk
    assert "Compare Tabelas" in menu_chunk
    assert "Feed" in menu_chunk
    assert "Previsibilidade Frete" not in menu_chunk
    assert "BI Cleide" not in menu_chunk
    assert "Controle de Estoque" not in menu_chunk
    assert "/cleide-bi-frete" not in menu_chunk
    assert "/auditoria-frete" in menu_chunk
    assert "/agente-compara" in menu_chunk


def test_composer_css_has_no_vertical_attach_divider():
    source = pathlib.Path("app/templates/chat_julia.html").read_text(encoding="utf-8")
    attach_block = source.split(".julia-chat-attach-btn {", 1)[1].split("}", 1)[0]
    assert "border-right" not in attach_block
    send_block = source.split(".julia-chat-form-with-attach .julia-chat-send {", 1)[1].split("}", 1)[0]
    assert "border-left" not in send_block


def test_embedded_julia_layout_class_on_authenticated_home(monkeypatch):
    client = _operational_client(monkeypatch)
    html = client.get("/").get_data(as_text=True)
    assert "julia-chat-embedded" in html
    assert "overflow: visible" in html
    embedded_block = html.split(".julia-chat-wrapper.julia-chat-embedded {", 1)[1].split("}", 1)[0]
    assert "border: none" in embedded_block
    assert "box-shadow: none" in embedded_block


def test_public_home_copilot_uses_embedded_layout(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=False))
    monkeypatch.setattr(web, "get_julia_chat_max_history", lambda: 10)
    monkeypatch.setattr(web, "avaliar_autorizacao_operacao_por_franquia", lambda _u: {"permitido": True})
    html = web.app.test_client().get("/").get_data(as_text=True)
    assert "julia-chat-embedded" in html
    assert 'data-copilot-surface="true"' in html


def test_actions_menu_uses_fixed_layering():
    source = pathlib.Path("app/templates/chat_julia.html").read_text(encoding="utf-8")
    menu_block = source.split(".julia-chat-actions-menu {", 1)[1].split("}", 1)[0]
    assert "position: fixed" in menu_block
    assert "z-index: 2000" in menu_block


def test_js_positions_actions_menu(js_source):
    assert "function positionActionsMenu" in js_source
    assert "document.body.appendChild(menu)" in js_source


def test_actions_submenu_excludes_home_link(monkeypatch):
    client = _operational_client(monkeypatch)
    html = client.get("/").get_data(as_text=True)
    menu_start = html.index('id="juliaChatActionsMenu"')
    menu_end = html.index("</div>", menu_start)
    menu_chunk = html[menu_start:menu_start + 2200]
    assert "Home / Notícias" not in menu_chunk
    assert 'bi-house-door' in menu_chunk


def test_js_actions_menu_toggle(js_source):
    assert "function toggleActionsMenu" in js_source
    assert "function closeActionsMenu" in js_source
    assert "function positionActionsMenu" in js_source
    assert "aria-expanded" in js_source
    assert "Fechar menu de ações" in js_source
    assert "Abrir menu de ações" in js_source
    assert "e.key === 'Escape'" in js_source
    assert "juliaChatUploadItem" in js_source


def test_sidebar_still_renders_on_authenticated_home(monkeypatch):
    client = _operational_client(monkeypatch)
    html = client.get("/").get_data(as_text=True)
    assert 'id="sidebar"' in html
    assert "Home" in html
    assert "Auditoria de Frete" in html
    assert "Compare Tabelas" in html
    assert "Feed" in html
    assert "Previsibilidade Frete" not in html


def test_sidebar_guest_private_links_point_to_login_with_next(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=False))
    monkeypatch.setattr(web, "get_julia_chat_max_history", lambda: 10)
    monkeypatch.setattr(web, "avaliar_autorizacao_operacao_por_franquia", lambda _u: {"permitido": True})
    html = web.app.test_client().get("/").get_data(as_text=True)
    assert 'href="/login?next=/auditoria-frete"' in html
    assert 'href="/login?next=/agente-compara"' in html
    assert 'href="/feed"' in html



def test_sidebar_authenticated_private_links_remain_direct(monkeypatch):
    client = _operational_client(monkeypatch, force_login=True)
    html = client.get("/").get_data(as_text=True)
    assert 'href="/auditoria-frete"' in html
    assert 'href="/agente-compara"' in html
    assert 'href="/login?next=/auditoria-frete"' not in html
    assert 'href="/login?next=/agente-compara"' not in html


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


def test_admin_agentes_cleiton_pdf_texto_atualizado():
    import pathlib

    html = pathlib.Path("app/painel_admin/template_admin/agentes_cleiton.html").read_text(encoding="utf-8")
    assert "será ativada em etapa posterior" not in html
    assert "Gemini Files API" in html
    assert "sem parser local pesado" in html


def test_js_pdf_ready_message(js_source):
    assert "PDF disponível como contexto da conversa." in js_source
    assert "pdf_context_ready" in js_source
    assert "Gemini File API será ativada em etapa posterior" not in js_source


def test_js_pdf_badge_classes(js_source):
    assert "function pdfBadgeClass" in js_source
    assert "julia-doc-item-badge-ready" in js_source
    assert "julia-doc-item-badge-preparing" in js_source
    assert "julia-doc-item-badge-error" in js_source
    assert "gemini_file_uri" not in js_source
    assert "gemini_file_name" not in js_source


def test_template_pdf_badge_ready_is_green():
    source = pathlib.Path("app/templates/chat_julia.html").read_text(encoding="utf-8")
    assert ".julia-doc-item-badge-ready" in source
    assert "rgba(0, 196, 140" in source
    assert ".julia-doc-item-badge-preparing" in source
    assert "rgba(255, 193, 7" in source
    assert ".julia-doc-item-badge-error" in source


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


def test_operational_template_has_documents_area(monkeypatch):
    client = _operational_client(monkeypatch)
    html = client.get("/chat_julia?mode=operational").get_data(as_text=True)
    assert 'id="juliaDocumentsClearBtn"' in html
    assert 'id="juliaChatActionsMenu"' in html
    input_pos = html.index('id="juliaChatInput"')
    docs_pos = html.index('id="juliaDocumentsArea"')
    assert input_pos < docs_pos


def test_cleide_page_does_not_render_attach_button(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=True, conta_id=1, franquia_id=1))
    monkeypatch.setattr(
        "app.cleide_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True, "modo_operacao": "normal"},
    )
    monkeypatch.setattr("app.cleide_routes.get_cleide_config", lambda: SimpleNamespace(layout_version=1))
    resp = web.app.test_client().get("/cleide-bi-frete")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'id="juliaChatAttachBtn"' not in html
    assert 'id="juliaChatActionsMenu"' not in html
    assert "julia_documents.js" not in html


def test_roberto_templates_do_not_reference_julia_attach_ui():
    root = pathlib.Path("app/templates")
    roberto_sources = "\n".join(
        p.read_text(encoding="utf-8")
        for p in root.rglob("*")
        if p.is_file() and "roberto" in p.name.lower()
    )
    assert "juliaChatAttachBtn" not in roberto_sources
    assert "julia_documents.js" not in roberto_sources
