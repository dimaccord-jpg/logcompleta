"""Contratos de página/UI do AgenteCompara (Compare Tabelas)."""
from __future__ import annotations

import importlib
import os
import pathlib
from types import SimpleNamespace


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


def test_agente_compara_page_returns_200(monkeypatch):
    web = _load_web_module()
    resp = web.app.test_client().get("/agente-compara")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "agente_compara.js" in html
    assert "cleide_auditoria.js" not in html
    assert 'id="agenteComparaShell"' in html
    assert 'id="agenteComparaActionsMenu"' in html
    assert "/api/cleide-auditoria" not in html


def test_agente_compara_template_uses_own_js_and_ids():
    source = pathlib.Path("app/templates/agente_compara.html").read_text(encoding="utf-8")
    assert "agente_compara.js" in source
    assert "cleide_auditoria.js" not in source
    assert 'id="agenteComparaShell"' in source
    assert 'id="agenteComparaActionsMenu"' in source
    assert 'id="agenteComparaInput"' in source
    assert 'id="agenteComparaSend"' in source
    assert 'id="agenteComparaTablesPrepPanel"' not in source
    assert "Continuar preparação das tabelas" not in source
    assert "agente-compara-continue-prep-panel" not in source
    assert 'data-typewriter-text="Faça o upload da tabela de frete."' in source
    assert "/api/cleide-auditoria" not in source
    input_pos = source.index('class="agente-compara-input-area"')
    docs_pos = source.index('id="agenteComparaDocumentsPanel"')
    assert input_pos < docs_pos


def test_agente_compara_template_save_button_starts_enabled_without_disabled_attr():
    source = pathlib.Path("app/templates/agente_compara.html").read_text(encoding="utf-8")
    button_start = source.index('id="agenteComparaTempTableModalSave"')
    button_chunk = source[button_start - 120: button_start + 220]
    assert 'type="button"' in button_chunk
    assert 'disabled' not in button_chunk
    assert 'aria-disabled' not in button_chunk


def test_agente_compara_js_tax_cta_not_blocked_by_dirty_state():
    js = pathlib.Path("app/static/js/agente_compara.js").read_text(encoding="utf-8")
    footer_block = js[js.index("function updateTempTableModalFooter()"): js.index("function canEditFreightTables")]
    assert "saveBtn.disabled = saveBtn.disabled || !comparisonState.canAdvanceToCoverage || taxConfigDirty" not in footer_block
    assert "Salvando e continuando..." in footer_block
    assert "function saveTaxesAndAdvanceToCoverage" in js


def test_agente_compara_js_has_no_cleide_auditoria_api():
    js = pathlib.Path("app/static/js/agente_compara.js").read_text(encoding="utf-8")
    assert "/api/agente-compara/" in js
    assert "/api/cleide-auditoria" not in js
    assert "agenteComparaShell" in js or "agenteComparaActionsMenu" in js




def test_agente_compara_preparation_failure_ui_contract():
    js = pathlib.Path("app/static/js/agente_compara.js").read_text(encoding="utf-8")

    assert "function resolveComparisonPreparationFailureUi" in js
    assert "Falha temporária ao preparar a tabela" in js
    assert "Nenhum crédito foi consumido por esta tentativa." in js
    assert "Esta nova tentativa não consumirá outro crédito." in js
    assert "failure_origin === 'platform'" in js
    assert "error.credit_disposition === 'preserved'" in js
    assert "error.is_free_retry === true" in js

def test_compare_tabelas_in_julia_plus_menu_on_operational_home(monkeypatch):
    client = _operational_client(monkeypatch)
    html = client.get("/").get_data(as_text=True)
    menu_start = html.index('id="juliaChatActionsMenu"')
    menu_chunk = html[menu_start : menu_start + 2800]
    assert "Compare Tabelas" in menu_chunk
    assert "/agente-compara" in menu_chunk


def test_compare_tabelas_in_cleide_auditoria_actions_menu(monkeypatch):
    web = _load_web_module()
    html = web.app.test_client().get("/auditoria-frete").get_data(as_text=True)
    menu_start = html.index('id="cleideAuditoriaActionsMenu"')
    menu_chunk = html[menu_start : menu_start + 1800]
    assert "Compare Tabelas" in menu_chunk
    assert "/agente-compara" in menu_chunk


def test_agente_compara_menu_comum_mostra_apenas_opcoes_aprovadas(monkeypatch):
    web = _load_web_module()
    with web.app.test_request_context("/agente-compara"):
        home_href = web.url_for("index")
    html = web.app.test_client().get("/agente-compara").get_data(as_text=True)
    menu_start = html.index('id="agenteComparaActionsMenu"')
    menu_chunk = html[menu_start : menu_start + 2200]
    assert "Enviar arquivos" in menu_chunk
    assert "Home" in menu_chunk
    assert f'href="{home_href}"' in menu_chunk
    assert "Auditoria de Frete" in menu_chunk
    assert "/auditoria-frete" in menu_chunk
    assert "Previsibilidade Frete" not in menu_chunk
    assert "BI Gerencial" not in menu_chunk
    assert "Feed" in menu_chunk
    assert menu_chunk.index("Enviar arquivos") < menu_chunk.index("Home")
    assert menu_chunk.index("Home") < menu_chunk.index("Auditoria de Frete")
    assert menu_chunk.index("Auditoria de Frete") < menu_chunk.index("Compare Tabelas")
    assert menu_chunk.index("Compare Tabelas") < menu_chunk.index("Feed")


def test_agente_compara_menu_admin_mostra_atalhos_completos(monkeypatch):
    from flask_login import UserMixin

    class _AuthUser(UserMixin):
        def __init__(self):
            self.id = "admin-1"
            self.is_admin = True
            self.conta_id = 1
            self.franquia_id = 1
            self.email = "admin@example.com"
            self.full_name = "Admin User"

    web = _load_web_module()
    user = _AuthUser()
    monkeypatch.setattr(web, "get_user_by_id", lambda _user_id: user)
    client = web.app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = user.get_id()
        sess["_fresh"] = True
    with web.app.test_request_context("/agente-compara"):
        home_href = web.url_for("index")
    html = client.get("/agente-compara").get_data(as_text=True)
    menu_start = html.index('id="agenteComparaActionsMenu"')
    menu_end = html.index('id="agenteComparaComparisonDashboard"', menu_start)
    menu_chunk = html[menu_start:menu_end]
    assert "Enviar arquivos" in menu_chunk
    assert "Home" in menu_chunk
    assert f'href="{home_href}"' in menu_chunk
    assert "Auditoria de Frete" in menu_chunk
    assert "/auditoria-frete" in menu_chunk
    assert "Compare Tabelas" in menu_chunk
    assert "Feed" in menu_chunk
    assert "Previsibilidade Frete" in menu_chunk
    assert "BI Gerencial" in menu_chunk
    assert menu_chunk.index("Enviar arquivos") < menu_chunk.index("Home")
    assert menu_chunk.index("Home") < menu_chunk.index("Auditoria de Frete")
    assert menu_chunk.index("Auditoria de Frete") < menu_chunk.index("Compare Tabelas")
    assert menu_chunk.index("Compare Tabelas") < menu_chunk.index("Feed")


def test_agente_compara_not_in_base_html_main_nav():
    base = pathlib.Path("app/templates/base.html").read_text(encoding="utf-8")
    source = pathlib.Path("app/templates/agente_compara.html").read_text(encoding="utf-8")
    assert "Compare Tabelas" in base
    assert "agente-compara" in base
    assert "agente_compara" in base
    assert 'class="container-fluid agente-compara-layout mb-5"' in source


def test_agente_compara_page_initial_state_no_forced_wizard(monkeypatch):
    """Página inicial não força wizard: modal fechado, sem botão de retomada."""
    html = pathlib.Path("app/templates/agente_compara.html").read_text(encoding="utf-8")
    js = pathlib.Path("app/static/js/agente_compara.js").read_text(encoding="utf-8")
    assert "Continuar preparação das tabelas" not in html
    assert "Continuar preparação das tabelas" not in js
    assert 'id="agenteComparaTempTableModal"' in html
    modal_start = html.index('id="agenteComparaTempTableModal"')
    modal_chunk = html[modal_start: modal_start + 400]
    assert "hidden" in modal_chunk
    init_block = js[js.index("function initDocuments"): js.index("function init()")]
    assert "openComparisonWizardModal()" not in init_block
    assert "shouldAutoOpenComparisonWizard()" not in init_block
    auto_block = js[js.index("function shouldAutoOpenComparisonWizard"): js.index("function maybeOpenComparisonWizardAfterStatus")]
    assert "PREPARE_TABLE_2" not in auto_block
    assert "ASK_TABLE_3" not in auto_block
    assert "PREPARE_TABLE_3" not in auto_block
    assert "comparisonWizardModalSuppressed" in auto_block


def test_agente_compara_advertised_in_copilot_onboarding():
    """Onboarding reconhece AgenteCompara como capability e destination oficiais."""
    caps = pathlib.Path("app/copilot_capabilities.md").read_text(encoding="utf-8")
    taxonomy = pathlib.Path("app/capability_taxonomy.py").read_text(encoding="utf-8")
    assert "AgenteCompara" in caps
    assert "/agente-compara" in caps
    assert "agente_compara" in caps
    assert "freight_table_comparison" in caps
    assert "agente_compara" in taxonomy
    assert "freight_table_comparison" in taxonomy
    assert "/agente-compara" in taxonomy
    assert "Iniciar comparação de tabelas" in taxonomy


def test_agente_compara_calculation_file_summary_ui_contract():
    """Etapa 5: resumo do arquivo + botão Processar Cálculos com wiring dedicado."""
    js = pathlib.Path("app/static/js/agente_compara.js").read_text(encoding="utf-8")
    html = pathlib.Path("app/templates/agente_compara.html").read_text(encoding="utf-8")

    assert "function renderCalculationFileSummary" in js
    assert "agenteComparaCalculationFileSummary" in js
    assert "agenteComparaProcessCalculationsButton" in js
    assert "Processar Cálculos" in js
    assert "Arquivo recebido para comparação" in js
    assert "Arquivo para Comparação" in js
    assert "function processComparisonCalculations" in js
    assert "comparisonCalculationInFlight" in js

    assert "agente-compara-process-calculations-hint" in html
    assert "agente-compara-run-btn.is-loading" in html
    assert "agente-compara-comparison-calculation-scroll" in html

    assert "cleideAuditRunButton" not in js
    assert "Arquivo recebido para auditoria" not in js
    assert "Processar auditoria" not in js
    assert "Arquivo para auditoria" not in js
    assert "/api/cleide-auditoria" not in js
    assert "comparison/calculate" in js
    process_fn = js[
        js.index("function processComparisonCalculations") : js.index(
            "function clearCalculationFileSummary"
        )
    ]
    assert "API_AUDIT_RUN" not in process_fn
    assert "runAuditProcessing" not in process_fn


def test_agente_compara_template_layout_allows_wide_data_surfaces():
    source = pathlib.Path("app/templates/agente_compara.html").read_text(encoding="utf-8")
    shell_start = source.index('.agente-compara-page-shell {')
    shell_block = source[shell_start: shell_start + 260]
    assert '--agente-compara-shell-max: 1320px' in shell_block
    assert '--agente-compara-text-max: 880px' in shell_block
    assert 'max-width: var(--agente-compara-shell-max);' in shell_block
    assert 'min-width: 0;' in shell_block

    docs_start = source.index('.agente-compara-documents-area {')
    docs_block = source[docs_start: docs_start + 120]
    assert 'max-width: 100%;' in docs_block

    table_start = source.index('.agente-compara-temp-table-modal-data-table {')
    table_block = source[table_start: table_start + 420]
    assert 'min-width: 0;' in table_block
    assert '.agente-compara-temp-table-modal-freight-scroll {' in table_block
    assert 'overflow-x: auto;' in table_block

    td_start = source.index('.agente-compara-temp-table-modal-data-table tbody td {')
    td_block = source[td_start: td_start + 320]
    assert 'word-break: normal;' in td_block
    assert 'overflow-wrap: break-word;' in td_block
    assert 'word-break: break-word;' not in td_block
