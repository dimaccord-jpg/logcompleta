import importlib
import os
import pathlib
import re
from types import SimpleNamespace


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


def _file_input_tag(html: str) -> str:
    match = re.search(r'<input[^>]*id="cleideAuditFileInput"[^>]*>', html, re.DOTALL)
    assert match, 'input id="cleideAuditFileInput" não encontrado'
    return match.group(0)


def _upload_item_tag(html: str) -> str:
    match = re.search(
        r'<button[^>]*id="cleideAuditoriaUploadItem"[^>]*>',
        html,
        re.DOTALL,
    )
    assert match, 'botão id="cleideAuditoriaUploadItem" não encontrado'
    return match.group(0)


def _input_tag(html: str) -> str:
    match = re.search(r'<textarea[^>]*id="cleideAuditoriaInput"[^>]*>', html, re.DOTALL)
    assert match, 'textarea id="cleideAuditoriaInput" não encontrado'
    return match.group(0)


def _send_button_tag(html: str) -> str:
    match = re.search(r'<button[^>]*id="cleideAuditoriaSend"[^>]*>', html, re.DOTALL)
    assert match, 'botão id="cleideAuditoriaSend" não encontrado'
    return match.group(0)


def test_cleide_auditoria_welcome_typewriter_contract(monkeypatch):
    web = _load_web_module()
    html = web.app.test_client().get("/auditoria-frete").get_data(as_text=True)
    assert html.count('<span class="af-text-gradient">Agentefrete</span>') == 1
    assert "Cleide, Auditora Virtual de AgenteFrete" in html
    assert "Atenção: a Cleide é uma IA e pode cometer erros." in html
    assert "Envie arquivos de apoio para a Cleide analisar nesta conversa." in html
    assert "A conexão com upload assistido será ativada na próxima etapa." not in html
    assert 'id="cleideAuditoriaWelcome"' in html
    assert 'data-typewriter-text="Faça o upload da tabela de frete."' in html
    assert "cleide_auditoria.js" in html
    assert 'id="cleideAuditoriaInput"' in html
    assert 'id="cleideAuditoriaSend"' in html
    assert "disabled" not in _input_tag(html)
    assert "disabled" not in _send_button_tag(html)
    file_input = _file_input_tag(html)
    assert "disabled" not in file_input
    upload_item = _upload_item_tag(html)
    assert "disabled" not in upload_item


def test_cleide_auditoria_js_documentos_conectados():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "runWelcomeTypewriter" in js
    assert "data-typewriter-text" in js
    assert "prefers-reduced-motion" in js
    assert "matchMedia" in js
    assert "fetch(" in js
    assert "FormData" in js
    assert "/api/cleide-auditoria/documents/upload" in js
    assert "/api/cleide-auditoria/documents/status" in js
    assert "/api/cleide-auditoria/documents/clear" in js
    assert "/api/cleide-auditoria/documents/" in js
    assert "/api/julia/documents" not in js
    assert "/api/cleide/upload" not in js
    assert "/api/chat_cleide" not in js
    assert "gemini" not in js.lower()
    assert "toggleActionsMenu" in js
    assert "Escape" in js
    assert "cleideAuditFileInput" in js
    assert "cleideAuditDocumentsList" in js
    assert "cleideAuditClearDocuments" in js
    assert "disponível como contexto da conversa." in js
    assert "parts.join(' · ')" in js
    assert "parts.join(' - ')" not in js


def test_cleide_auditoria_js_chat_conectado():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "/api/cleide-auditoria/chat" in js
    assert "/api/chat_cleide" not in js
    assert "/api/chat_julia" not in js
    assert "/api/cleide/upload" not in js
    assert "/api/julia/documents" not in js
    assert "sendChatMessage" in js
    assert "initChat" in js
    assert "request_id" in js
    assert "history" in js
    assert "MAX_CHAT_HISTORY" in js
    assert "res.status === 401" in js
    assert "res.status === 403" in js
    assert "res.data.answer" in js
    assert "CHAT_FIXED_ERRORS" in js
    assert "appendChatBubble" in js
    assert "setChatLoading" in js
    assert "uploadDocument" in js
    assert "renderCleideMarkdown" in js
    assert "inner.innerHTML = renderCleideMarkdown(text);" in js
    assert "inner.textContent = text;" in js
    assert "gemini" not in js.lower()


def test_cleide_auditoria_js_chat_sem_parser_ou_simulacao_ia():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "JSON.parse" not in js
    assert "eval(" not in js
    assert "new RegExp" not in js
    forbidden_ready_replies = [
        "Resposta auditável simulada",
        "auditoria concluída com sucesso",
        "sua tabela está correta",
    ]
    for phrase in forbidden_ready_replies:
        assert phrase not in js


def test_cleide_auditoria_js_chat_historico_limitado():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "trimChatHistory" in js
    assert "slice(" in js
    assert "role: 'user'" in js
    assert "role: 'assistant'" in js


def test_cleide_auditoria_painel_documental_no_template():
    source = pathlib.Path("app/templates/cleide_auditoria.html").read_text(encoding="utf-8")
    assert 'id="cleideAuditDocumentsPanel"' in source
    assert 'id="cleideAuditDocumentsList"' in source
    assert 'id="cleideAuditClearDocuments"' in source
    assert 'id="cleideAuditUploadStatus"' in source
    assert 'id="cleideAuditDocumentsError"' in source
    assert 'accept=".txt,.xml,.csv,.xlsx,.docx,.pdf"' in source
    assert 'id="cleideAuditFileInput"' in source
    assert "Documentos anexados nesta conversa." in source
    assert "Limpar documentos" in source


def test_cleide_auditoria_documentos_ficam_abaixo_do_composer():
    source = pathlib.Path("app/templates/cleide_auditoria.html").read_text(encoding="utf-8")
    composer_index = source.index('id="cleideAuditoriaComposer"')
    docs_index = source.index('id="cleideAuditDocumentsPanel"')
    assert composer_index < docs_index


def test_cleide_auditoria_toolbar_documentos_nao_some_do_template():
    source = pathlib.Path("app/templates/cleide_auditoria.html").read_text(encoding="utf-8")
    toolbar_tag = re.search(r'<div[^>]*id="cleideAuditDocumentsToolbar"[^>]*>', source, re.DOTALL)
    assert toolbar_tag
    assert 'display: none' not in toolbar_tag.group(0)


def test_cleide_auditoria_nao_conecta_apis_julia_ou_bi(monkeypatch):
    web = _load_web_module()
    resp = web.app.test_client().get("/auditoria-frete")
    html = resp.get_data(as_text=True)
    assert "/api/julia/documents" not in html
    assert "/api/cleide/upload" not in html
    assert "/api/chat_cleide" not in html
    assert "julia_documents.js" not in html


def test_cleide_auditoria_menu_tem_atalhos_esperados(monkeypatch):
    web = _load_web_module()
    with web.app.test_request_context("/auditoria-frete"):
        home_href = web.url_for("index")
    html = web.app.test_client().get("/auditoria-frete").get_data(as_text=True)
    menu_start = html.index('id="cleideAuditoriaActionsMenu"')
    menu_chunk = html[menu_start:menu_start + 1400]
    assert "Enviar arquivos" in menu_chunk
    assert "Home" in menu_chunk
    assert f'href="{home_href}"' in menu_chunk
    assert "Previsibilidade Frete" in menu_chunk
    assert "BI Cleide" in menu_chunk
    assert "/cleide-bi-frete" in menu_chunk
    assert "Feed" in menu_chunk
    assert menu_chunk.index("Enviar arquivos") < menu_chunk.index("Home")
    assert menu_chunk.index("Home") < menu_chunk.index("Previsibilidade Frete")
    assert menu_chunk.index("Previsibilidade Frete") < menu_chunk.index("BI Cleide")
    assert menu_chunk.index("BI Cleide") < menu_chunk.index("Feed")


def test_cleide_auditoria_layout_alinhado_julia_embedded():
    source = pathlib.Path("app/templates/cleide_auditoria.html").read_text(encoding="utf-8")
    assert 'class="container mb-5"' in source
    assert "cleide-auditoria-embedded" in source
    assert "cleide-auditoria-form-with-attach" in source
    assert "cleide-auditoria-composer-wrap" in source
    assert "cleide-auditoria-shell" not in source
    assert "cleide-auditoria-page" not in source
    embedded_block = source.split(".cleide-auditoria-wrapper.cleide-auditoria-embedded {", 1)[1].split("}", 1)[0]
    assert "border: none" in embedded_block
    assert "box-shadow: none" in embedded_block
    assert "overflow: visible" in embedded_block
    messages_block = source.split(".cleide-auditoria-embedded .cleide-auditoria-messages {", 1)[1].split("}", 1)[0]
    assert "min-height: 0" in messages_block
    assert "padding: 0 0 1rem" in messages_block
    welcome_block = source.split(".cleide-auditoria-welcome {", 1)[1].split("}", 1)[0]
    assert "max-width: 760px" in welcome_block
    composer_block = source.split(".cleide-auditoria-composer {", 1)[1].split("}", 1)[0]
    assert "border-radius: 1.75rem" in composer_block
    menu_block = source.split(".cleide-auditoria-actions-menu {", 1)[1].split("}", 1)[0]
    assert "position: fixed" in menu_block
    assert "z-index: 2000" in menu_block
    badge_block = source.split(".cleide-audit-doc-item-badge-ready {", 1)[1].split("}", 1)[0]
    assert "color: #00c48c" in badge_block
    empty_docs_block = source.split(".cleide-audit-documents-area-empty {", 1)[1].split("}", 1)[0]
    assert "padding-top: 0.35rem" in empty_docs_block
    assert "border-top-color: rgba(0, 191, 255, 0.08)" in empty_docs_block


def test_cleide_auditoria_markdown_render_contract():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "function escapeHtml(text)" in js
    assert "function renderCleideMarkdown(text)" in js
    assert "replace(/&/g, '&amp;')" in js
    assert "replace(/</g, '&lt;')" in js
    assert "replace(/>/g, '&gt;')" in js
    assert "replace(/\\*\\*([^*\\n][^*\\n]*?)\\*\\*/g, '<strong>$1</strong>')" in js
    assert "line.match(/^\\s*\\*\\s+(.+)$/)" in js
    assert "target=\"_blank\" rel=\"noopener noreferrer\"" in js


def test_cleide_auditoria_template_suporta_listas_e_links_no_chat():
    source = pathlib.Path("app/templates/cleide_auditoria.html").read_text(encoding="utf-8")
    assert ".cleide-auditoria-chat-msg-inner ul" in source
    assert ".cleide-auditoria-chat-msg-inner li" in source
    assert ".cleide-auditoria-chat-msg-inner a" in source


def test_cleide_auditoria_pagina_mantem_contratos_visuais(monkeypatch):
    web = _load_web_module()
    html = web.app.test_client().get("/auditoria-frete").get_data(as_text=True)
    assert html.count('<span class="af-text-gradient">Agentefrete</span>') == 1
    assert "cleide-auditoria-embedded" in html
    assert 'id="cleideAuditoriaComposer"' in html
    assert 'id="cleideAuditoriaAttachBtn"' in html
    assert 'id="cleideAuditoriaWelcome"' in html
    assert 'placeholder="Mensagem para a Cleide..."' in html
    assert "Últimas da Logística" not in html


def test_cleide_auditoria_js_isolado_de_julia():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    julia_js = pathlib.Path("app/static/js/julia_documents.js").read_text(encoding="utf-8")
    chat_behavior_js = pathlib.Path("app/static/js/chat_behavior.js").read_text(encoding="utf-8")
    assert "julia_documents" not in js
    assert "JULIA_DOCUMENTS_UI" not in js
    assert "cleiton_doc_ids" not in js.lower()
    assert "/api/julia/documents" not in js
    assert "/api/chat_julia" not in js
    assert "juliaDocuments" not in js
    assert "juliaChat" not in js
    assert "chat_behavior" not in js
    assert "fetch(" in julia_js
    assert "/api/julia/documents" in julia_js
    assert "/api/chat_julia" in chat_behavior_js
    assert "/api/chat_julia" not in js


def test_cleide_auditoria_js_mantem_endpoints_e_badges():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "/api/cleide-auditoria/documents/upload" in js
    assert "/api/cleide-auditoria/documents/status" in js
    assert "/api/cleide-auditoria/documents/clear" in js
    assert "/api/cleide-auditoria/chat" in js
    assert "cleide-audit-doc-item-badge-ready" in js
    assert "cleide-audit-doc-item-badge-preparing" in js
    assert "cleide-audit-doc-item-badge-error" in js
    assert "renderTempTableItem" in js
    assert "temp_table" in js
    assert "Tabela temporária extraída" in js
    assert "TEMP_TABLE_OPERATIONAL_MESSAGES" in js
    assert "announceTempTableStatusIfNeeded" in js


def test_cleide_bi_continua_separado(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(
        "app.cleide_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True, "modo_operacao": "normal"},
    )
    monkeypatch.setattr("app.cleide_routes.get_cleide_config", lambda: SimpleNamespace(layout_version=1))
    bi_html = web.app.test_client().get("/cleide-bi-frete").get_data(as_text=True)
    assert "BI Cleide" in bi_html
    assert "Cleide, Auditora Virtual de AgenteFrete" not in bi_html


def test_cleide_auditoria_js_needs_review_message_atualizada():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "A tabela temporária foi gerada. Revise os dados antes de continuar." in js
    assert "leitura incerta" not in js


def test_cleide_auditoria_temp_table_card_clicavel():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "cleide-audit-temp-table-open-btn" in js
    assert 'aria-label' in js
    assert "Abrir dados da tabela temporária" in js
    assert "openTempTableModal" in js
    assert "currentTempTable" in js


def test_cleide_auditoria_temp_table_modal_no_template():
    source = pathlib.Path("app/templates/cleide_auditoria.html").read_text(encoding="utf-8")
    assert 'id="cleideAuditTempTableModal"' in source
    assert "Tabela temporária gerada" in source
    assert "Revise os dados extraídos antes de continuar." in source
    assert 'id="cleideAuditTempTableModalSave"' in source
    assert 'id="cleideAuditTempTableModalEdit"' in source
    assert 'id="cleideAuditTempTableModalCancelEdit"' in source
    assert "Salvar e Avançar" in source
    assert "Editar" in source
    assert "Validar e Salvar" not in source


def test_cleide_auditoria_js_temp_table_modal_renderizacao():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "renderTempTableModalContent" in js
    assert "freight_tables" in js
    assert "freight_values" in js
    assert "freight_routes" in js
    assert "accessorial_fees" in js
    assert "weight_ranges" in js
    assert "reading_alerts" in js
    assert "evidence_refs" in js
    assert "Nenhum item identificado nesta seção." in js
    assert "Processamento em andamento. Os dados aparecerão aqui quando a extração terminar." in js
    assert "Validar e Salvar" not in js
    assert "checklist" not in js.lower()
    assert "createElement('input')" in js or 'createElement("input")' in js
    assert "document.createElement" in js
    assert "textContent" in js
    assert "não informado" in js


def test_cleide_auditoria_js_temp_table_modal_blocos_operacionais():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "Tabelas de frete identificadas" in js
    assert "Frete por rota" in js
    assert "Generalidades e serviços adicionais" in js
    assert "Informações adicionais" in js
    assert "Alertas de leitura" in js
    assert "Evidências/referências" in js
    assert "renderFreightTablesSection" in js
    assert "renderMainFreightSection" in js
    assert "renderFreightRoutesSection" in js
    assert "renderAdditionalInfoSection" in js


def test_cleide_auditoria_js_temp_table_freight_route_columns():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    expected_columns = [
        "Origem",
        "Destino",
        "Tipo",
        "Até 30 kg",
        "Até 50 kg",
        "Até 70 kg",
        "Até 100 kg",
        "Taxa Embarque Kg",
        "Frete Valor %",
        "Frete Peso Kg",
        "Observações",
    ]
    for column in expected_columns:
        assert column in js


def test_cleide_auditoria_js_temp_table_modal_prioriza_freight_tables():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "freight_tables" in js
    assert "hasUsefulFreightTables" in js
    assert "renderMainFreightSection" in js
    assert "renderFreightTablesSection" in js
    assert "renderDynamicFreightTable" in js
    assert "renderFreightTableCard" in js
    assert "renderFreightTableContext" in js
    assert "Tabelas de frete identificadas" in js
    assert "hasUsefulFreightTables(tempTable)" in js
    assert "freightRoutes.length" in js
    assert "resolveFreightRouteRows" in js
    assert "buildStructuredFreightRows" in js
    assert "buildPartialFreightRows" in js
    assert "isPartial: false" in js
    assert "isPartial: true" in js
    assert "route.freight_type" in js
    assert "extração parcial" in js
    assert "não identificado" in js
    assert "isPrimaryFreightAccessorialFee" in js
    assert "getGeneralAccessorialFees" in js
    assert (
        "Alguns vínculos de origem, destino ou tipo de frete ainda precisam de validação humana."
        in js
    )
    assert "Nenhuma rota de frete identificada nesta extração." in js
    main_block_start = js.index("function renderMainFreightSection")
    main_block = js[main_block_start:main_block_start + 600]
    assert "hasUsefulFreightTables(tempTable)" in main_block
    assert "renderFreightTablesSection" in main_block
    assert "renderFreightRoutesSection" in main_block
    tables_call = main_block.index("renderFreightTablesSection")
    routes_call = main_block.index("renderFreightRoutesSection")
    assert tables_call < routes_call


def test_cleide_auditoria_js_temp_table_additional_info_empty_message():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert (
        "Informações adicionais não identificadas no artefato atual."
        in js
    )


def test_cleide_auditoria_js_temp_table_modal_somente_leitura():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "saveTempTableAndAdvance" in js
    assert "enterTempTableEditMode" in js
    assert "cancelTempTableEdit" in js
    assert "API_TEMP_TABLE_SAVE" in js
    assert "checklist" not in js.lower()
    assert "Validar e Salvar" not in js
    assert "contentEditable" not in js
    assert "collectTempTableSavePayload" in js
    assert "accessorial_fees" in js
    payload_block = js[js.index("function collectTempTableSavePayload"): js.index("function collectTempTableSavePayload") + 900]
    assert "accessorial_fees" in payload_block


def test_cleide_auditoria_js_temp_table_edit_features():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "cleide-audit-temp-table-modal-cell-input" in js
    assert "cleide-audit-temp-table-modal-row-delete-btn" in js
    assert "cleide-audit-temp-table-modal-col-delete-btn" in js
    assert "deepCloneTempTable" in js
    assert "tempTableEditSnapshot" in js
    assert "renderEditableFreightRoutesTable" in js
    assert "window.confirm" in js


def test_cleide_auditoria_template_temp_table_modal_layout():
    source = pathlib.Path("app/templates/cleide_auditoria.html").read_text(encoding="utf-8")
    assert "cleide-audit-temp-table-modal-freight-scroll" in source
    assert "cleide-audit-temp-table-modal-freight-table" in source
    assert "cleide-audit-temp-table-modal-freight-table-card" in source
    assert "overflow-x: auto" in source
    assert "min-width: 860px" in source
    assert "position: sticky" in source
    assert "Salvar e Avançar" in source
    assert "Editar" in source
    assert "checklist" not in source.lower()


def test_cleide_auditoria_js_painel_anexos_mantem_temp_table():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "renderDocuments" in js
    assert "renderTempTableItem" in js
    assert "handleTempTableFromStatus" in js
    assert "renderDocumentItem" in js


def test_cleide_auditoria_js_temp_table_modal_edita_accessorial_fees():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    source = pathlib.Path("app/templates/cleide_auditoria.html").read_text(encoding="utf-8")
    assert "canEditAccessorialFees" in js
    assert "renderEditableAccessorialFeesSection" in js
    assert "Adicionar item" in js
    assert "currentTempTable.accessorial_fees.push" in js
    assert "cleide-audit-temp-table-modal-add-btn" in source
    assert "min-width: 1120px" in source


def test_cleide_auditoria_js_freight_table_open_state_variables():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "openFreightTableKeys" in js
    assert "hasUserTouchedFreightTableOpenState" in js
    assert "new Set()" in js


def test_cleide_auditoria_js_freight_table_stable_key():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "function getFreightTableKey" in js
    assert "table.evidence_ref" in js
    assert "table.table_title" in js
    assert "table.table_type" in js
    assert "context.route_label" in js
    key_block = js[js.index("function getFreightTableKey"): js.index("function getFreightTableKey") + 700]
    assert "index:" in key_block


def test_cleide_auditoria_js_freight_table_first_open_by_default():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    card_block = js[js.index("function renderFreightTableCard"): js.index("function renderFreightTableCard") + 900]
    assert "index === 0" in card_block
    assert "hasUserTouchedFreightTableOpenState" in card_block


def test_cleide_auditoria_js_freight_table_preserves_open_after_rerender():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    card_block = js[js.index("function renderFreightTableCard"): js.index("function renderFreightTableCard") + 900]
    assert "openFreightTableKeys.has(tableKey)" in card_block
    assert "openFreightTableKeys.add(tableKey)" in card_block
    assert "openFreightTableKeys.delete(tableKey)" in card_block
    assert "addEventListener('toggle'" in card_block or 'addEventListener("toggle"' in card_block


def test_cleide_auditoria_js_freight_table_row_delete_keeps_open_state():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    delete_block = js[js.index("appendRowDeleteCell(tr, function () {", js.index("function renderDynamicFreightTable")): js.index("appendRowDeleteCell(tr, function () {", js.index("function renderDynamicFreightTable")) + 400]
    assert "renderTempTableModalContent(currentTempTable)" in delete_block
    assert "resetFreightTableOpenState" not in delete_block


def test_cleide_auditoria_js_freight_table_col_delete_keeps_open_state():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    col_start = js.index("colBtn.addEventListener('click', function () {", js.index("function renderDynamicFreightTable"))
    col_block = js[col_start:col_start + 900]
    assert "renderTempTableModalContent(currentTempTable)" in col_block
    assert "resetFreightTableOpenState" not in col_block


def test_cleide_auditoria_js_freight_table_accessorial_edit_keeps_open_state():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    add_block = js[js.index("addBtn.addEventListener('click', function () {", js.index("function renderEditableAccessorialFeesSection")): js.index("addBtn.addEventListener('click', function () {", js.index("function renderEditableAccessorialFeesSection")) + 500]
    delete_block = js[js.index("appendRowDeleteCell(tr, function () {", js.index("function renderEditableAccessorialFeesSection")): js.index("appendRowDeleteCell(tr, function () {", js.index("function renderEditableAccessorialFeesSection")) + 400]
    assert "renderTempTableModalContent(currentTempTable)" in add_block
    assert "renderTempTableModalContent(currentTempTable)" in delete_block
    assert "resetFreightTableOpenState" not in add_block
    assert "resetFreightTableOpenState" not in delete_block


def test_cleide_auditoria_js_freight_table_open_state_reset_on_modal_close():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "function resetFreightTableOpenState" in js
    close_block = js[js.index("function closeTempTableModal"): js.index("function closeTempTableModal") + 500]
    assert "resetFreightTableOpenState()" in close_block
    reset_block = js[js.index("function resetFreightTableOpenState"): js.index("function resetFreightTableOpenState") + 300]
    assert "openFreightTableKeys.clear()" in reset_block
    assert "hasUserTouchedFreightTableOpenState = false" in reset_block


def test_cleide_auditoria_js_freight_table_open_state_not_in_save_payload():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    payload_block = js[js.index("function collectTempTableSavePayload"): js.index("function collectTempTableSavePayload") + 900]
    assert "openFreightTableKeys" not in payload_block
    assert "hasUserTouchedFreightTableOpenState" not in payload_block
    assert "getFreightTableKey" not in payload_block
    assert "resetFreightTableOpenState" not in payload_block


def test_cleide_auditoria_js_freight_table_open_state_does_not_affect_other_sections():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    save_block = js[js.index("function saveTempTableAndAdvance"): js.index("function saveTempTableAndAdvance") + 1200]
    assert "freight_routes" in save_block or "collectTempTableSavePayload" in save_block
    payload_block = js[js.index("function collectTempTableSavePayload"): js.index("function collectTempTableSavePayload") + 900]
    assert "freight_routes" in payload_block
    assert "accessorial_fees" in payload_block
    assert "save_and_advance" in payload_block
    routes_block = js[js.index("function renderFreightRoutesSection"): js.index("function renderFreightRoutesSection") + 1200]
    assert "openFreightTableKeys" not in routes_block
    accessorial_block = js[js.index("function renderAccessorialFeesSection"): js.index("function renderAccessorialFeesSection") + 1200]
    assert "openFreightTableKeys" not in accessorial_block
