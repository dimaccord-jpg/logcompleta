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
    assert 'id="cleideAuditTempTableModalStartAudit"' in source
    assert 'id="cleideAuditTempTableModalClose"' in source
    assert 'id="cleideAuditTempTableModalCancelEdit"' in source
    assert "Salvar e Avançar" in source
    assert "Editar" in source
    assert "Iniciar Auditoria" in source
    assert "Validar e Salvar" not in source
    header_block = source[source.index("cleide-audit-temp-table-modal-header"): source.index("cleide-audit-temp-table-modal-body")]
    assert "cleide-audit-temp-table-modal-close-btn" in header_block
    assert 'aria-label="Fechar modal"' in header_block
    edit_pos = source.index('id="cleideAuditTempTableModalEdit"')
    start_audit_pos = source.index('id="cleideAuditTempTableModalStartAudit"')
    save_pos = source.index('id="cleideAuditTempTableModalSave"')
    assert edit_pos < start_audit_pos < save_pos


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
    payload_block = js[js.index("function collectTempTableSavePayload"): js.index("function accessorialFeeHasRequiredValue")]
    assert "accessorial_fees" in payload_block
    assert "populateTempTableSaveEditTarget" in payload_block


def test_cleide_auditoria_js_direct_save_preserves_temp_table_sections_in_payload():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    populate_block = js[
        js.index("function populateTempTableSaveEditTarget"): js.index("function collectTempTableSavePayload")
    ]
    payload_block = js[js.index("function collectTempTableSavePayload"): js.index("function accessorialFeeHasRequiredValue")]
    assert "deepCloneTempTable(tempTable.accessorial_fees)" in populate_block
    assert "deepCloneTempTable(tempTable.freight_tables)" in populate_block
    assert "deepCloneTempTable(tempTable.freight_routes)" in populate_block
    assert "populateTempTableSaveEditTarget(payload.edit_target, currentTempTable)" in payload_block
    assert "if (tempTableEditMode)" not in payload_block


def test_cleide_auditoria_js_temp_table_edit_features():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "cleide-audit-temp-table-modal-cell-input" in js
    assert "cleide-audit-temp-table-modal-row-delete-btn" in js
    assert "cleide-audit-temp-table-modal-col-delete-btn" in js
    assert "deepCloneTempTable" in js
    assert "tempTableEditSnapshot" in js
    assert "renderEditableFreightRoutesTable" in js
    assert "window.confirm" in js


def test_cleide_auditoria_js_base_calculo_renderiza_select():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "currentCalculationBases" in js
    assert "function appendCalculationBaseSelectCell" in js
    assert "document.createElement('select')" in js
    assert "não mapeado / revisar" in js
    assert "calculationBaseOptionLabel" in js
    assert "calculation_base_id" in js
    assert "applyCalculationBaseToAccessorialFee" in js
    assert "manual_configured_calculation_base" in js


def test_cleide_auditoria_js_bloqueia_avanco_com_base_nao_mapeada():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "function validateTempTableBeforeAdvance" in js
    assert "function collectTempTableAdvanceValidationErrors" in js
    assert "errors.push(" in js
    assert "section: 'accessorial_fees'" in js
    assert "reason_code: 'missing_calculation_base'" in js
    assert "reason_code: 'invalid_accessorial_value'" in js
    assert "reason_code: 'incompatible_accessorial_unit'" in js
    assert "reason_code: 'unsupported_or_incomplete_operation'" in js
    assert "Selecione uma base de cálculo ou exclua a linha." in js
    assert "Preencha um valor válido para esta taxa ou exclua a linha." in js
    assert "Ajuste a unidade para a base selecionada." in js
    assert "1 item precisa de revisão. Corrija os campos destacados ou exclua a linha." in js
    assert "itens precisam de revisão. Corrija os campos destacados ou exclua as linhas." in js
    save_block = js[js.index("function saveTempTableAndAdvance"): js.index("fetch(API_TEMP_TABLE_SAVE", js.index("function saveTempTableAndAdvance"))]
    assert "validateTempTableBeforeAdvance()" in save_block
    assert "accessorialFeeHasRequiredValue" in js
    assert "accessorialFeeUnitMatchesBase" in js
    assert "accessorialFeeOperationIsComplete" in js
    assert "accessorialFeeIsMinimumAmount" in js
    assert "validateLinkedMinimumAccessorialFee" in js
    assert "syncAccessorialMinimumAmountFields" in js
    assert "afterDot.length <= 4" in js
    assert "missing_minimum_base_link" in js
    assert "invalid_minimum_base_link" in js
    assert "Esta regra mínima não possui uma taxa principal válida vinculada. Corrija ou exclua a regra antes de continuar." in js
    assert "function accessorialRateConflictError" in js
    assert "reason_code: 'accessorial_rate_conflict'" in js
    assert "Valor informado no campo:" in js
    assert "Valor descrito na observação:" in js
    assert "O sistema não pode decidir qual percentual utilizar." in js
    assert "related_fields: ['value', 'notes']" in js
    assert "multiply_by_variable" in js


def test_cleide_auditoria_js_destaca_base_calculo_invalida():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    source = pathlib.Path("app/templates/cleide_auditoria.html").read_text(encoding="utf-8")
    assert "tempTableValidationErrors" in js
    assert "getAccessorialFeeValidationError(feeIndex" in js
    assert "accessorial-row--invalid" in js
    assert "data-accessorial-fee-index" in js
    assert "field-invalid" in js
    assert "aria-invalid" in js
    assert "data-field', 'calculation_base_id'" in js
    assert "field: 'value'" in js
    assert "field: 'unit'" in js
    assert "field: 'notes'" in js
    assert "error.related_fields.indexOf(field)" in js
    assert "accessorial-field-error" in js
    assert "accessorial-field-error-icon" in js
    assert "âš " in js
    assert ".accessorial-row--invalid" in source
    assert ".cleide-audit-temp-table-modal-cell-input.field-invalid" in source
    assert ".accessorial-field-error" in source


def test_cleide_auditoria_js_foca_primeiro_erro_base_calculo():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "function focusFirstTempTableValidationError" in js
    focus_block = js[js.index("function focusFirstTempTableValidationError"): js.index("function ensureTempTableEditModeForValidation")]
    assert "scrollIntoView({ block: 'center', behavior: 'smooth' })" in focus_block
    assert "first.field || 'calculation_base_id'" in focus_block
    assert "field.focus()" in focus_block
    assert "ensureTempTableEditModeForValidation()" in js
    edit_block = js[js.index("function ensureTempTableEditModeForValidation"): js.index("function validateTempTableBeforeAdvance")]
    assert "tempTableEditMode = true" in edit_block
    assert "tempTableModalActiveTab = 'freight'" in edit_block


def test_cleide_auditoria_js_limpa_erro_ao_corrigir_ou_excluir_base_calculo():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "function refreshTempTableValidationErrorsAfterAccessorialEdit" in js
    select_block = js[js.index("appendCalculationBaseSelectCell(tr, item, function (baseId)"): js.index("appendAccessorialFieldCell(tr, item.notes")]
    delete_block = js[js.index("appendRowDeleteCell(tr, function () {", js.index("function renderEditableAccessorialFeesSection")): js.index("appendRowDeleteCell(tr, function () {", js.index("function renderEditableAccessorialFeesSection")) + 500]
    assert "refreshTempTableValidationErrorsAfterAccessorialEdit()" in select_block
    assert "refreshTempTableValidationErrorsAfterAccessorialEdit()" in delete_block
    assert "setTempTableValidationErrors(collectTempTableAdvanceValidationErrors())" in js
    assert "setTempTableModalError('')" in js


def test_cleide_auditoria_js_aplica_errors_backend_base_calculo():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    handler_block = js[js.index("function handleBackendTempTableValidationErrors"): js.index("function saveTempTableAndAdvance")]
    response_block = js[js.index(".then(function (res) {", js.index("function saveTempTableAndAdvance")): js.index("if (res.data.temp_table)", js.index("function saveTempTableAndAdvance"))]
    assert "Array.isArray(data.errors)" in handler_block
    assert "setTempTableValidationErrors(data.errors)" in handler_block
    assert "ensureTempTableEditModeForValidation()" in handler_block
    assert "focusFirstTempTableValidationError()" in handler_block
    assert "handleBackendTempTableValidationErrors(res.data)" in response_block


def test_cleide_auditoria_js_readonly_base_calculo_diferencia_resolvida_de_extraida():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "function appendReadonlyCalculationBasisCell" in js
    assert "calculation_base_label" in js
    assert "raw_calculation_basis" in js
    assert "texto extraído:" in js
    assert "accessorial-basis-extracted-text" in js
    readonly_block = js[js.index("function appendReadonlyCalculationBasisCell"): js.index("function calculationBaseOptionLabel")]
    assert "getCalculationBaseById(baseId)" in readonly_block
    assert "não mapeado / revisar" in readonly_block


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
    assert "Iniciar Auditoria" in source
    assert "cleide-audit-temp-table-modal-start-audit-btn" in source
    assert "checklist" not in source.lower()


def test_cleide_auditoria_js_start_audit_step():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "handleStartAudit" in js
    assert "cleideAuditTempTableModalStartAudit" in js
    assert "Arquivo para auditoria" in js
    assert "/api/cleide-auditoria/audit-template" in js
    assert "/api/cleide-auditoria/audit/upload" in js
    assert "Baixe o modelo, preencha com os fretes cobrados e envie o arquivo para auditoria." in js
    assert "Baixar modelo" in js
    assert "Enviar arquivo preenchido" in js
    assert "Arquivo recebido para auditoria" in js
    assert "A auditoria será iniciada na próxima etapa." not in js
    start_block = js[js.index("function handleStartAudit"): js.index("function appendOperationalMessage")]
    assert "auditFileStepActive = true" in start_block
    assert "tempTableModalActiveTab = 'audit'" in start_block
    assert "appendOperationalMessage" not in start_block
    init_block = js[js.index("function initTempTableModal"): js.index("function initDocuments")]
    assert "handleStartAudit()" in init_block
    footer_block = js[js.index("function updateTempTableModalFooter"): js.index("function canEditFreightTables")]
    assert "cleideAuditTempTableModalStartAudit" in footer_block
    assert "hasAuditBatch(currentTempTable)" in footer_block
    assert "canStartAudit" in footer_block
    assert "!onCoverageTab" in footer_block


def test_cleide_auditoria_js_audit_upload_status():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "uploadAuditFile" in js
    assert "setAuditUploadStatus" in js
    assert "cleideAuditAuditUploadStatus" in js
    upload_block = js[js.index("function uploadAuditFile"): js.index("function ensureCoverageTableShell")]
    assert "formData.append('file', file)" in upload_block
    assert "'success'" in upload_block
    assert "'error'" in upload_block
    assert "resultado" not in upload_block.lower()
    assert "diverg" not in upload_block.lower()


def test_cleide_auditoria_js_audit_upload_refreshes_documents_for_bi_button():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    upload_block = js[js.index("function uploadAuditFile"): js.index("function ensureCoverageTableShell")]
    assert "return fetchDocuments().then(function (statusData) {" in upload_block
    assert "if (statusData) return statusData;" in upload_block
    assert "if (res.data.temp_table) {" in upload_block
    assert "setCurrentTempTable(res.data.temp_table)" in upload_block
    assert "setAuditUploadStatus('Arquivo recebido para auditoria.', 'success');" in upload_block


def test_cleide_auditoria_js_audit_run_step():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "/api/cleide-auditoria/audit/run" in js
    assert "Processar auditoria" in js
    assert "cleideAuditRunButton" in js
    assert "runAuditProcessing" in js
    run_block = js[js.index("function runAuditProcessing"): js.index("function renderCoverageUploadHint")]
    assert "fetch(API_AUDIT_RUN" in run_block
    assert "method: 'POST'" in run_block
    assert "Processando auditoria..." in run_block
    assert "Auditoria processada." in run_block
    assert "toler" not in run_block.lower()
    assert "generalidade" not in run_block.lower()


def test_cleide_auditoria_js_audit_run_summary_and_results():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    summary_block = js[js.index("function renderAuditRunSummary"): js.index("function renderAuditRunResults")]
    assert "Resumo da auditoria" in summary_block
    assert "Total de linhas" in summary_block
    assert "Ok" in summary_block
    assert "Divergentes" in summary_block
    assert "Sem mapeamento" in summary_block
    assert "Sem regra" in summary_block
    assert "Inválidas" in summary_block
    assert "function renderAuditDiagnostics" in summary_block
    assert "Diagnóstico da auditoria" in summary_block
    assert "Dimensão tarifária incompatível" in summary_block
    assert "Nenhuma correção automática será aplicada nesta fase." in summary_block
    assert "Corrigir tabela cadastrada" in summary_block
    assert "openAuditCorrectionExplanation" in summary_block
    assert "fetch(" not in summary_block
    results_block = js[js.index("function renderAuditRunResults"): js.index("function runAuditProcessing")]
    assert "Resultados por linha" in results_block
    assert "cleide-audit-run-results-table" in results_block
    assert "Esperado" in results_block
    assert "Diferença" in results_block
    assert "Ações" in results_block
    assert "appendAuditRowActionCell" in results_block
    assert "appendExpectedFreightCell" in results_block


def test_cleide_auditoria_js_audit_diagnostics_actions_are_local():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    diagnostics_gate_block = js[js.index("function auditDiagnosticsHasErrors"): js.index("function auditCorrectionSuggestionForGroup")]
    scroll_block = js[js.index("function scrollToAuditDiagnostics"): js.index("function renderAuditGlobalErrorButton")]
    global_block = js[js.index("function renderAuditGlobalErrorButton"): js.index("function renderLegacyAuditDiagnosticsNotice")]
    assert "diagnostics.has_errors === true" in diagnostics_gate_block
    assert "Number(diagnostics.total_errors || 0) > 0" in diagnostics_gate_block
    assert "Ver erros da auditoria" in global_block
    assert "batch.audit_diagnostics" in js
    assert "scrollToAuditDiagnostics" in global_block
    assert "scrollIntoView" in scroll_block
    assert "fetch(" not in global_block

    action_block = js[js.index("function appendAuditRowActionCell"): js.index("function renderAuditRunResults")]
    assert "auditRowHasFailure(row)" in action_block
    assert "Ver erro" in action_block
    assert "openLineErrorDetail(row, diagnostics)" in action_block
    assert "fetch(" not in action_block


def test_cleide_auditoria_js_audit_error_modal_and_choices_are_readonly():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    modal_block = js[js.index("var auditDiagnosticModalEl"): js.index("function renderAuditRunResults")]
    assert "Documento" in modal_block
    assert "UF destino" in modal_block
    assert "Cidade destino" in modal_block
    assert "Classificação de cobertura identificada" in modal_block
    assert "Etapa da falha" in modal_block
    assert "Critérios usados na busca" in modal_block
    assert "Tentativas de correspondência" in modal_block
    assert "Valores da dimensão tarifária atual" in modal_block
    assert "Coluna candidata" in modal_block
    assert "Valores da coluna candidata" in modal_block
    assert "Causa relacionada" in modal_block
    assert "diagnostic_group_code" in js
    assert "Chave procurada" not in js
    assert "Grupo global" not in js
    assert "Sem grupo vinculado" not in js
    assert "Nenhuma alteração foi aplicada." in modal_block
    assert "Simular correção" in modal_block
    assert "fetch(API_AUDIT_CORRECTION_PREVIEW" in js
    assert "fetch(API_AUDIT_CORRECTION_APPLY" in js
    assert "fetch(API_AUDIT_CORRECTION_UNDO" in js
    assert "Aplicar correção" in js
    assert "applyBtn.disabled = !preview.safe_to_apply" in js
    assert "A correção será aplicada somente na tabela temporária e poderá ser desfeita." in js
    assert "Desfazer correção" in js
    assert "setCurrentTempTable(res.data.temp_table)" in js
    assert "Prefiro corrigir os arquivos e refazer o upload" in modal_block
    assert "Nenhum documento será removido e nenhum estado será alterado." in modal_block
    assert "currentTempTable =" not in modal_block
    assert "removeDocument" not in modal_block
    assert "runAuditProcessing" not in modal_block


def test_cleide_auditoria_js_audit_legacy_diagnostics_notice():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    legacy_block = js[js.index("function renderLegacyAuditDiagnosticsNotice"): js.index("function appendAuditSummaryItem")]
    assert "Este lote foi processado antes da geração do diagnóstico detalhado." in legacy_block
    assert "Atualizar diagnóstico" in legacy_block
    assert "runAuditProcessing()" in legacy_block
    assert "batch.audit_diagnostics" in legacy_block
    assert "auditBatchHasFailureResults(batch)" in legacy_block


def test_cleide_auditoria_js_audit_calculation_memory():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    source = pathlib.Path("app/templates/cleide_auditoria.html").read_text(encoding="utf-8")
    memory_block = js[js.index("function buildAuditCalculationMemoryRows"): js.index("function auditStatusLabel")]
    assert "openAuditCalculationMemory" in js
    assert "buildAuditCalculationMemoryRows" in js
    assert "renderAuditCalculationMemoryContent" in js
    assert "calculation_components" in memory_block
    assert "Memória de cálculo detalhada não disponível para esta linha." in memory_block
    assert "fetch(" not in memory_block
    assert "cleide-audit-calculation-memory-modal" in source
    assert "cleide-audit-run-expected-link" in source


def test_cleide_auditoria_html_audit_file_styles():
    source = pathlib.Path("app/templates/cleide_auditoria.html").read_text(encoding="utf-8")
    assert "cleide-audit-audit-file-card" in source
    assert "cleide-audit-audit-file-status.is-success" in source
    assert "cleide-audit-audit-file-summary" in source
    assert "cleide-audit-run-btn" in source
    assert "cleide-audit-run-status.is-loading" in source
    assert "cleide-audit-run-summary" in source
    assert "cleide-audit-diagnostics" in source
    assert "cleide-audit-diagnostic-card" in source
    assert "cleide-audit-error-global-btn" in source
    assert "cleide-audit-run-row-error-btn" in source
    assert "cleide-audit-diagnostic-modal" in source
    assert "cleide-audit-correction-primary-btn:disabled" in source
    assert ".cleide-audit-run-results-table th:last-child" in source
    assert ".cleide-audit-run-results-table td:last-child" in source
    assert "scrollbar-gutter: stable" in source
    assert "cleide-audit-run-results-table" in source


def test_cleide_auditoria_js_temp_table_modal_close_button():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    source = pathlib.Path("app/templates/cleide_auditoria.html").read_text(encoding="utf-8")
    assert "cleideAuditTempTableModalClose" in source
    assert "cleide-audit-temp-table-modal-close-btn" in source
    init_block = js[js.index("function initTempTableModal"): js.index("function initDocuments")]
    assert "cleideAuditTempTableModalClose" in init_block
    assert "closeTempTableModal()" in init_block
    close_block = js[js.index("function closeTempTableModal"): js.index("function initTempTableModal")]
    assert "tempTableEditMode = false" in close_block
    assert "resetFreightTableOpenState()" in close_block


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


def test_cleide_auditoria_js_coverage_prompt_after_save():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    save_block = js[js.index("function saveTempTableAndAdvance"): js.index("function byId")]
    assert "coverageStepActive = true" in save_block
    assert "tempTableModalActiveTab = 'coverage'" in save_block
    assert "renderTempTableModalContent(currentTempTable)" in save_block
    assert "closeTempTableModal" not in save_block
    assert "appendCoveragePromptCTA" not in save_block
    assert "Deseja informar a relação de cidades atendidas?" in js


def test_cleide_auditoria_js_coverage_step_state_resets_without_persisting():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "var coverageStepActive = false" in js
    reset_block = js[js.index("function resetCoveragePromptState"): js.index("function resetAuditFileStepState")]
    assert "coverageStepActive = false" in reset_block
    status_block = js[js.index("function handleTempTableFromStatus"): js.index("function formatBytes")]
    assert "previousTempTableId" in status_block
    assert "nextTempTableId" in status_block
    assert "resetCoveragePromptState()" in status_block
    payload_block = js[js.index("function collectTempTableSavePayload"): js.index("function saveTempTableAndAdvance")]
    assert "coverageStepActive" not in payload_block


def test_cleide_auditoria_js_coverage_cta_is_inside_modal_not_save_chat():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    coverage_block = js[js.index("function renderCoverageTabContent"): js.index("function renderFreightTabContent")]
    assert "renderCoverageDecisionCard(section)" in coverage_block
    assert "renderCoverageUploadCard(section, 'cleideAuditCoverageModal')" in coverage_block
    save_block = js[js.index("function saveTempTableAndAdvance"): js.index("function byId")]
    assert "appendCoveragePromptCTA" not in save_block
    assert "showCoverageUploadArea" not in save_block


def test_cleide_auditoria_js_coverage_prompt_buttons():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "handleCoveragePromptAnswer" in js
    assert "cleide-audit-coverage-prompt-yes" in js
    assert "cleide-audit-coverage-prompt-no" in js
    assert "renderCoverageDecisionCard" in js
    prompt_block = js[js.index("function renderCoverageDecisionCard"): js.index("function renderCoverageSkippedState")]
    assert "cleide-audit-coverage-prompt-card" in prompt_block
    assert "cleide-audit-coverage-prompt-title" in prompt_block
    assert "cleide-audit-coverage-prompt-description" in prompt_block
    assert "cleide-audit-coverage-prompt-actions" in prompt_block
    assert "Deseja informar a relação de cidades atendidas?" in prompt_block
    assert "handleCoveragePromptAnswer(true)" in prompt_block
    assert "handleCoveragePromptAnswer(false)" in prompt_block
    assert "innerHTML" not in prompt_block


def test_cleide_auditoria_js_coverage_prompt_card_does_not_touch_upload():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    upload_area_block = js[js.index("function renderCoverageUploadCard"): js.index("function setCoverageUploadFileName")]
    upload_block = js[js.index("function uploadCoverageFile"): js.index("function collectCoverageSavePayload")]
    assert "cleide-audit-coverage-upload-card" in upload_area_block
    assert "formData.append('file', file)" in upload_block
    prompt_block = js[js.index("function renderCoverageDecisionCard"): js.index("function renderCoverageSkippedState")]
    assert "showCoverageUploadArea" not in prompt_block
    assert "uploadCoverageFile" not in prompt_block
    assert "formData" not in prompt_block


def test_cleide_auditoria_html_coverage_prompt_card_styles():
    web = _load_web_module()
    html = web.app.test_client().get("/auditoria-frete").get_data(as_text=True)
    assert "cleide-audit-coverage-prompt-card" in html
    assert "cleide-audit-coverage-prompt-title" in html
    assert "cleide-audit-coverage-prompt-description" in html
    assert "cleide-audit-coverage-prompt-btn-primary" in html
    assert "cleide-audit-coverage-prompt-btn-secondary" in html


def test_cleide_auditoria_js_modal_tabs_coverage():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "renderTempTableModalTabs" in js
    assert "Tabela de frete" in js
    assert "Cidades atendidas" in js
    assert "renderCoverageTabContent" in js
    assert "renderFreightTabContent" in js


def test_cleide_auditoria_js_coverage_table_columns():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "destination_uf" in js
    assert "destination_city" in js
    assert "freight_region" in js
    assert "renderEditableCoverageTable" in js


def test_cleide_auditoria_js_coverage_edit_features():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "saveCoverageTableEdit" in js
    assert "collectCoverageSavePayload" in js
    cancel_block = js[js.index("function cancelTempTableEdit"): js.index("function cancelTempTableEdit") + 500]
    assert "tempTableEditSnapshot" in cancel_block


def test_cleide_auditoria_js_coverage_save_payload():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    payload_block = js[js.index("function collectCoverageSavePayload"): js.index("function collectCoverageSavePayload") + 500]
    assert "coverage_table" in payload_block
    assert "tempTableModalActiveTab" not in payload_block
    assert "openFreightTableKeys" not in payload_block


def test_cleide_auditoria_js_coverage_upload_api():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "/api/cleide-auditoria/coverage/upload" in js
    assert "uploadCoverageFile" in js


def test_cleide_auditoria_js_coverage_modal_upload_uses_unique_ids():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "renderCoverageUploadCard(section, 'cleideAuditCoverageModal')" in js
    upload_area_block = js[js.index("function renderCoverageUploadCard"): js.index("function showCoverageUploadArea")]
    assert "fileInputId = idPrefix + 'FileInput'" in upload_area_block
    assert "idPrefix + 'UploadFileName'" in upload_area_block
    assert "idPrefix + 'UploadStatus'" in upload_area_block
    assert "cleideAuditCoverageFileInput" not in upload_area_block


def test_cleide_auditoria_js_coverage_prompt_yes_does_not_open_modal():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    prompt_block = js[js.index("function handleCoveragePromptAnswer"): js.index("function uploadCoverageFile")]
    assert "coverageStepActive" in prompt_block
    assert "renderTempTableModalContent(currentTempTable)" in prompt_block
    assert "openTempTableModal()" not in prompt_block
    assert "tempTableModalActiveTab = 'coverage'" in prompt_block


def test_cleide_auditoria_js_coverage_prompt_yes_shows_upload_area():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    prompt_block = js[js.index("function handleCoveragePromptAnswer"): js.index("function uploadCoverageFile")]
    assert "coveragePromptAccepted = !!accepted" in prompt_block
    assert "coveragePromptAnswered = true" in prompt_block
    assert "renderTempTableModalContent(currentTempTable)" in prompt_block
    assert "showCoverageUploadArea()" in prompt_block
    assert "appendOperationalMessage" in prompt_block


def test_cleide_auditoria_js_coverage_no_answer_releases_start_audit():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    skipped_block = js[js.index("function renderCoverageSkippedState"): js.index("function renderEditableCoverageTable")]
    assert "Etapa ignorada. Você poderá iniciar a auditoria" in skipped_block
    footer_block = js[js.index("function updateTempTableModalFooter"): js.index("function canEditFreightTables")]
    assert "coveragePromptAnswered && !coveragePromptAccepted" in footer_block
    prompt_block = js[js.index("function handleCoveragePromptAnswer"): js.index("function uploadCoverageFile")]
    assert "coveragePromptAccepted = !!accepted" in prompt_block


def test_cleide_auditoria_js_coverage_yes_requires_upload_before_start_audit():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    footer_block = js[js.index("function updateTempTableModalFooter"): js.index("function canEditFreightTables")]
    assert "coverageHasRows || (coveragePromptAnswered && !coveragePromptAccepted)" in footer_block
    assert "!canStartAudit" in footer_block
    coverage_block = js[js.index("function renderCoverageTabContent"): js.index("function renderFreightTabContent")]
    assert "coveragePromptAccepted" in coverage_block
    assert "renderCoverageUploadCard(section, 'cleideAuditCoverageModal')" in coverage_block


def test_cleide_auditoria_js_coverage_upload_opens_modal_only_with_rows():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    upload_block = js[js.index("function uploadCoverageFile"): js.index("function collectCoverageSavePayload")]
    assert "formData.append('file', file)" in upload_block
    assert "hasCoverageRows(currentTempTable)" in upload_block
    assert upload_block.index("hasCoverageRows(currentTempTable)") < upload_block.index("renderTempTableModalContent(currentTempTable)")
    assert "Nenhuma cidade foi identificada" in upload_block


def test_cleide_auditoria_js_coverage_state_helpers():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "function hasCoverageRows(tempTable)" in js
    assert "function hasLoadedCoverageTable(tempTable)" in js
    should_show_block = js[js.index("function shouldShowCoverageTab"): js.index("function canEditCoverageTable")]
    assert "coverageStepActive" in should_show_block
    assert "hasCoverageRows(tempTable)" in should_show_block
    assert "coveragePromptAccepted" in should_show_block
    assert "hasLoadedCoverageTable(tempTable)" in should_show_block
    edit_block = js[js.index("function canEditCoverageTable"): js.index("function resetCoveragePromptState")]
    assert "hasCoverageRows(tempTable)" in edit_block


def test_cleide_auditoria_js_coverage_empty_tab_hides_edit():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    footer_block = js[js.index("function updateTempTableModalFooter"): js.index("function canEditFreightTables")]
    assert "hideEditOnEmptyCoverage" in footer_block
    assert "hasCoverageRows(currentTempTable)" in footer_block
    hint_block = js[js.index("function renderCoverageUploadHint"): js.index("function renderEditableCoverageTable")]
    assert "Faça upload do arquivo complementar CSV ou XLSX" in hint_block


def test_cleide_auditoria_js_coverage_upload_custom_card():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    upload_area_block = js[js.index("function renderCoverageUploadCard"): js.index("function setCoverageUploadFileName")]
    assert "cleide-audit-coverage-upload-card" in upload_area_block
    assert "fileInputId" in upload_area_block
    assert "visually-hidden" in upload_area_block
    assert "cleide-audit-coverage-upload-button" in upload_area_block
    assert "setAttribute('for', fileInputId)" in upload_area_block
    assert "Selecionar arquivo" in upload_area_block
    assert "Formatos aceitos: CSV ou XLSX." in upload_area_block
    assert "idPrefix + 'UploadFileName'" in upload_area_block
    assert "idPrefix + 'UploadStatus'" in upload_area_block
    assert "setAttribute('role', 'status')" in upload_area_block
    assert "setCoverageUploadFileName" in upload_area_block
    assert "textContent" in upload_area_block
    assert "innerHTML" not in upload_area_block


def test_cleide_auditoria_js_coverage_upload_file_name_helper():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    name_block = js[js.index("function setCoverageUploadFileName"): js.index("function setCoverageUploadStatus")]
    assert "activeCoverageUploadPrefix + 'UploadFileName'" in name_block
    assert "cleideAuditCoverageModalUploadFileName" in name_block
    assert "textContent" in name_block
    assert "Nenhum arquivo selecionado" in name_block


def test_cleide_auditoria_js_coverage_upload_status_states():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    status_block = js[js.index("function setCoverageUploadStatus"): js.index("function handleCoveragePromptAnswer")]
    assert "is-loading" in status_block
    assert "is-success" in status_block
    assert "is-error" in status_block
    upload_block = js[js.index("function uploadCoverageFile"): js.index("function collectCoverageSavePayload")]
    assert "setCoverageUploadStatus('Enviando arquivo...', 'loading')" in upload_block
    assert "'success'" in upload_block
    assert "'error'" in upload_block
    assert "formData.append('file', file)" in upload_block


def test_cleide_auditoria_html_coverage_upload_card_styles():
    web = _load_web_module()
    html = web.app.test_client().get("/auditoria-frete").get_data(as_text=True)
    assert "cleide-audit-coverage-upload-card" in html
    assert "cleide-audit-coverage-upload-button" in html
    assert "cleide-audit-coverage-upload-status.is-loading" in html
    assert "cleide-audit-coverage-upload-status.is-success" in html
    assert "cleide-audit-coverage-upload-status.is-error" in html
    assert "cleide-audit-coverage-upload-file-name" in html


def test_cleide_auditoria_template_bi_section_below_documents():
    source = pathlib.Path("app/templates/cleide_auditoria.html").read_text(encoding="utf-8")
    docs_pos = source.index('id="cleideAuditDocumentsPanel"')
    bi_pos = source.index('id="cleideAuditBiSection"')
    modal_pos = source.index('id="cleideAuditTempTableModal"')
    assert docs_pos < bi_pos < modal_pos
    assert 'id="cleideAuditBiDashboard"' in source
    assert 'id="cleideAuditBiChartsGrid"' in source
    assert "BI Executivo da Auditoria de Frete" in source
    assert "Impacto Financeiro por Transportadora" in source
    assert "Divergência Financeira por UF Destino" in source
    assert "Evolução da Divergência no Período" in source
    assert "Pareto do Valor Cobrado a Mais" in source
    assert 'data-audit-bi-chart-card="transportadora"' in source
    assert 'data-audit-bi-chart-card="uf_destino"' in source
    assert 'data-audit-bi-chart-card="temporal"' in source
    assert 'data-audit-bi-chart-card="pareto_transportadora"' in source
    assert 'data-audit-bi-chart-card="uf_origem"' not in source
    assert 'data-audit-bi-chart-card="volume_transportadora"' not in source
    assert 'data-audit-bi-chart-card="pareto_uf"' not in source
    assert "chart.js@4.4.1" in source

def test_cleide_auditoria_js_bi_generate_button_and_section():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "Gerar Gráficos" in js
    assert "showAuditBiSection" in js
    assert "initAuditBiDashboard" in js
    assert "auditBiRenderDashboard" in js
    assert "cleide-audit-bi-generate-btn" in js
    assert "auditBi.ready === true" in js
    assert "auditBi.ready === false" in js
    assert "audit_bi" in js
    assert "auditBiFilteredRows" in js
    assert "auditBiClearFilters" in js
    assert "auditBiHideChart" in js
    assert "auditBiShowChart" in js
    assert "auditBiShowAllCharts" in js
    assert "Campo indisponível no lote auditado atual." in js
    assert "function setCurrentTempTable" in js
    assert "function refreshAuditBiDashboardFromCurrentTempTable" in js
    assert "initAuditBiDashboard((currentTempTable && currentTempTable.audit_bi) || auditBi)" in js
    assert "auditBiDestroyAllCharts()" in js
    assert "auditBiDashboardState.activeFilters = {" in js


def test_cleide_auditoria_js_bi_four_executive_chart_cards_and_cross_filter():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    chart_contract = js[
        js.index("var AUDIT_BI_CHART_LABELS"):
        js.index("var AUDIT_BI_FIELD_REQUIREMENTS")
    ]
    for chart_key in (
        "transportadora",
        "uf_destino",
        "temporal",
        "pareto_transportadora",
    ):
        assert chart_key in chart_contract
    assert "uf_origem" not in chart_contract
    assert "volume_transportadora" not in chart_contract
    assert "pareto_uf" not in chart_contract
    assert "Impacto Financeiro por Transportadora" in chart_contract
    assert "Divergência Financeira por UF Destino" in chart_contract
    assert "Evolução da Divergência no Período" in chart_contract
    assert "Pareto do Valor Cobrado a Mais" in chart_contract
    assert "auditBiHandleChartClick" in js
    assert "auditBiApplyFilterToggle" in js
    assert "auditBiBuildOverchargeParetoRows" in js
    assert "auditBiFilteredRows" in js
    assert "applyBackendFilters" not in js

def test_cleide_auditoria_js_bi_transportadora_agrega_impacto_financeiro():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    aggregate_block = js[
        js.index("function auditBiAggregateCarrierDivergence"):
        js.index("function auditBiAggregateByDate")
    ]
    assert "auditBiAggregateByField(rows, 'carrier')" in aggregate_block
    assert "'impacto_total', 'desc'" in aggregate_block

    generic_block = js[
        js.index("function auditBiAggregateByField"):
        js.index("function auditBiHasDivergenceValue")
    ]
    assert "valor_cobrado: 0" in generic_block
    assert "valor_esperado: 0" in generic_block
    assert "divergencia_liquida: 0" in generic_block
    assert "cobrado_a_mais: 0" in generic_block
    assert "cobrado_a_menor: 0" in generic_block
    assert "impacto_total: 0" in generic_block
    assert "grouped[key].valor_cobrado += auditBiGetNumeric(row.charged_freight)" in generic_block
    assert "grouped[key].divergencia_liquida += divergence" in generic_block
    assert "grouped[key].cobrado_a_mais += divergence" in generic_block
    assert "grouped[key].cobrado_a_menor += Math.abs(divergence)" in generic_block

def test_cleide_auditoria_js_bi_transportadora_usa_tres_datasets_executivos():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    render_block = js[
        js.index("function auditBiRenderCarrierDivergenceChart"):
        js.index("function auditBiRenderFilterUi")
    ]
    assert "type: 'bar'" in render_block
    assert "indexAxis: 'y'" in render_block
    assert "label: 'Cobrado a mais'" in render_block
    assert "label: 'Cobrado a menor'" in render_block
    assert "label: 'Divergência líquida'" in render_block
    assert "auditBiFormatCurrency(parsed.x)" in render_block
    assert "auditBiHandleChartClick(chartKey, labels[elements[0].index])" in render_block

def test_cleide_auditoria_js_bi_transportadora_indisponivel_sem_base_financeira():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    render_start = js.index("function auditBiRenderChartCard")
    chart_block = js[
        js.index("if (chartKey === 'transportadora')", render_start):
        js.index("if (chartKey === 'uf_destino')", render_start)
    ]
    assert "Divergência financeira indisponível no lote auditado atual." in js
    assert "Transportadora indisponível no lote auditado atual." in js
    assert "auditBiDashboardState.fieldPresence.carrier === false" in chart_block
    assert "filteredRows.length > 0 && !auditBiHasCarrierValue(filteredRows)" in chart_block
    assert "filteredRows.length > 0 && !auditBiHasDivergenceValue(filteredRows)" in chart_block
    assert "auditBiComputeDivergence(row) !== null" in js
    assert "auditBiHasNumericValue(row.divergence_value)" in js
    assert chart_block.index("AUDIT_BI_CARRIER_UNAVAILABLE_MESSAGE") < chart_block.index("AUDIT_BI_DIVERGENCE_UNAVAILABLE_MESSAGE")
    assert "auditBiRenderCarrierDivergenceChart(carrierRows)" in chart_block
    assert "auditBiRenderSimpleChart" not in chart_block

def test_cleide_auditoria_js_bi_transportadora_filtro_preservado():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    click_block = js[js.index("function auditBiHandleChartClick"): js.index("function auditBiRenderSimpleChart")]
    assert "chartKey === 'transportadora'" in click_block
    assert "auditBiApplyFilterToggle('carrier', selected)" in click_block
    render_block = js[
        js.index("function auditBiRenderCarrierDivergenceChart"):
        js.index("function auditBiRenderFilterUi")
    ]
    assert "labels[elements[0].index]" in render_block
    assert "datasetIndex" not in render_block


def test_cleide_auditoria_js_bi_does_not_alter_chat_upload_modal():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "openTempTableModal" in js
    assert "/api/cleide-auditoria/chat" in js
    assert "/api/cleide-auditoria/documents/upload" in js
    assert "renderTempTableModalContent" in js
    bi_block = js[js.index("var AUDIT_BI_CHART_LABELS"): js.index("function renderTempTableItem")]
    assert "openTempTableModal" not in bi_block
    assert "renderTempTableModalContent" not in bi_block
    assert "auditBiFilteredRows" in bi_block
    assert "/api/cleide-auditoria/chat" not in bi_block


def test_cleide_auditoria_js_bi_old_bi_not_touched():
    old_js = pathlib.Path("app/static/js/cleide_auditoria_frete.js").read_text(encoding="utf-8")
    assert "applyBackendFilters" in old_js
    assert "initAuditBiDashboard" not in old_js


def test_cleide_auditoria_template_bi_grid_has_hidden_layout_rule():
    source = pathlib.Path("app/templates/cleide_auditoria.html").read_text(encoding="utf-8")
    assert "cleide-audit-bi-charts-grid--has-hidden" in source
    assert ".cleide-audit-bi-charts-grid--has-hidden .cleide-audit-bi-chart-card--wide" in source
    assert "grid-column: auto" in source
    assert 'id="cleideAuditBiChartsGrid"' in source


def test_cleide_auditoria_js_bi_grid_hidden_state_toggle():
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    hidden_block = js[js.index("function auditBiRenderHiddenChartsUi"): js.index("function auditBiHideChart")]
    assert "cleideAuditBiChartsGrid" in hidden_block
    assert "cleide-audit-bi-charts-grid--has-hidden" in hidden_block
    assert "hiddenKeys.length > 0" in hidden_block
    assert "auditBiShowAllCharts" in js
    assert "auditBiResizeVisibleCharts" in js
    assert "Gerar Gráficos" in js
    assert "auditBiClearFilters" in js
    assert "openTempTableModal" in js
