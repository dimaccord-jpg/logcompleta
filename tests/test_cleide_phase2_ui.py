import importlib
import os
from pathlib import Path
from types import SimpleNamespace

import flask_login.utils


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


def _set_current_user(monkeypatch, *, user_id="1"):
    fake_user = SimpleNamespace(
        is_authenticated=True,
        is_active=True,
        is_anonymous=False,
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


def _get_cleide_html(monkeypatch) -> str:
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", _set_current_user(monkeypatch))
    monkeypatch.setattr(
        "app.cleide_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True, "modo_operacao": "normal"},
    )
    monkeypatch.setattr("app.cleide_routes.get_cleide_config", lambda: SimpleNamespace(layout_version=2))
    client = web.app.test_client()
    resp = client.get("/cleide-bi-frete")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def test_cleide_bi_semantic_copy(monkeypatch):
    html = _get_cleide_html(monkeypatch)
    assert "BI Cleide" in html
    assert "Auditoria de Frete Operacional" not in html
    assert "Auditoria de Frete Operacional e Gerencial" not in html


def test_cleide_phase7_sections_present(monkeypatch):
    html = _get_cleide_html(monkeypatch)
    assert 'data-testid="cleide-hero"' in html
    assert 'data-testid="cleide-flow"' in html
    assert 'data-testid="cleide-upload-placeholder"' in html
    assert 'data-testid="cleide-kpis"' in html
    assert 'data-testid="cleide-quality-panel"' in html
    assert 'data-testid="cleide-dashboard-structural"' in html
    assert 'data-testid="cleide-chat-floating"' in html
    assert 'data-testid="cleide-chat-panel"' in html
    assert 'data-testid="cleide-dashboard-filter-top"' in html
    assert 'data-testid="cleide-filters-placeholder"' not in html
    assert 'data-testid="cleide-kpis-placeholder"' not in html
    assert 'data-testid="cleide-dashboard-placeholder"' not in html
    assert 'data-testid="cleide-chat-placeholder"' not in html


def test_cleide_phase7_upload_controls_present(monkeypatch):
    html = _get_cleide_html(monkeypatch)
    assert "id=\"cleideChatFloating\"" in html
    assert "id=\"cleideChatToggle\"" in html
    assert "id=\"cleideChatPanel\"" in html
    assert "id=\"cleideChatClose\"" in html
    assert "cleide-chat-floating" in html
    assert "cleide-chat-toggle" in html
    assert "cleide-chat-panel" in html
    assert "Em implantacao" not in html
    assert "Upload ativo por sessão" in html
    assert "Ciclo de upload: 1 ativo por sessão" in html
    assert "Análise operacional em evolução (sem BI/IA ativos)" in html
    assert "id=\"cleideUploadForm\"" in html
    assert "id=\"cleideUploadInput\"" in html
    assert "id=\"cleideTemplateDownloadBtn\"" in html
    assert 'data-testid="cleide-template-download"' in html
    assert 'href="/api/cleide/template"' in html
    assert "Baixar modelo" in html
    assert "id=\"cleideUploadStatus\"" in html
    assert "id=\"cleideStructuralFeedback\"" in html
    assert "id=\"cleideStructuralDetails\"" in html
    assert "id=\"cleideKpiTotalDocumentos\"" in html
    assert "id=\"cleideQualityInvalidNumeric\"" in html
    assert "id=\"cleideDashboardState\"" in html
    assert "id=\"cleideTransportadoraVisual\"" in html
    assert "id=\"cleideChartTransportadora\"" in html
    assert "id=\"cleideChartUfOrigem\"" in html
    assert "id=\"cleideChartUfDestino\"" in html
    assert "id=\"cleideChartTemporal\"" in html
    assert "id=\"cleideChartOriginCarrier\"" in html
    assert "id=\"cleideChartParetoUf\"" in html
    assert "id=\"cleideChartParetoTransportadora\"" in html
    assert "Top Transportadoras" in html
    assert "Custo por UF Destino" in html
    assert "Evolução Diária" in html
    assert "Custo por UF Origem" in html
    assert "Volume por Transportadora" in html
    assert "Ocorrências por UF Destino" in html
    assert "Ocorrências por Transportadora" in html
    assert "id=\"cleideFilterDateStart\"" in html
    assert "id=\"cleideFilterDateEnd\"" in html
    assert "id=\"cleideResetFiltersBtn\"" in html
    assert "id=\"cleideKpiScopeBadge\"" in html
    assert "id=\"cleideFilterSemanticHint\"" in html
    assert "id=\"cleideDashboardFilterTop\"" in html
    assert "id=\"cleideHiddenChartsWrap\"" in html
    assert "id=\"cleideHiddenChartsList\"" in html
    assert "id=\"cleideShowAllChartsBtn\"" in html
    assert "Nenhum gráfico oculto" in html
    assert "Reexibir todos" in html
    assert "id=\"cleideActiveFilterChips\"" in html
    assert "id=\"cleideChartCardTransportadora\"" in html
    assert "id=\"cleideChartCardUfDestino\"" in html
    assert "id=\"cleideChartCardTemporal\"" in html
    assert "id=\"cleideChartCardUfOrigem\"" in html
    assert "id=\"cleideChartCardVolumeTransportadora\"" in html
    assert "id=\"cleideChartCardParetoUf\"" in html
    assert "id=\"cleideChartCardParetoTransportadora\"" in html
    assert 'data-cleide-chart-card="transportadora"' in html
    assert 'data-cleide-chart-card="uf_destino"' in html
    assert 'data-cleide-chart-card="temporal"' in html
    assert 'data-cleide-chart-card="uf_origem"' in html
    assert 'data-cleide-chart-card="volume_transportadora"' in html
    assert 'data-cleide-chart-card="pareto_uf"' in html
    assert 'data-cleide-chart-card="pareto_transportadora"' in html
    assert 'data-cleide-hide-chart="transportadora"' in html
    assert 'data-cleide-hide-chart="uf_destino"' in html
    assert 'data-cleide-hide-chart="temporal"' in html
    assert 'data-cleide-hide-chart="uf_origem"' in html
    assert 'data-cleide-hide-chart="volume_transportadora"' in html
    assert 'data-cleide-hide-chart="pareto_uf"' in html
    assert 'data-cleide-hide-chart="pareto_transportadora"' in html
    assert "id=\"cleideSortTransportadora\"" in html
    assert "id=\"cleidePageInfoTemporal\"" in html
    assert "id=\"cleideTableTransportadora\"" in html
    assert "id=\"cleideChatForm\"" in html
    assert "id=\"cleideChatQuestion\"" in html
    assert "id=\"cleideChatSendBtn\"" in html
    assert "id=\"cleideChatCopyLastBtn\"" in html
    assert "id=\"cleideChatLoading\"" in html
    assert "id=\"cleideChatMessages\"" in html
    assert "id=\"cleideChatScopeBadge\"" in html
    assert "id=\"cleideChatScopeDetails\"" in html
    assert "id=\"cleideChatFallbackBanner\"" in html
    assert "window.CLEIDE_CHAT_MAX_HISTORY" in html
    assert "Chat operacional da Cleide" in html
    assert "Aguardando upload para validação estrutural." in html
    assert "Colunas detectadas: - | Colunas faltantes: - | Linhas: 0 | Sheet: n/a" in html
    assert "Use o modelo padrão da Cleide para preparar a planilha antes do upload." in html
    assert "analytics estrutural ativo" in html
    assert "IA contextual e insights automáticos ainda inativos" in html
    assert "sem camada analitica ativa" not in html
    assert "Aguardando upload" in html
    assert html.count("disabled") >= 5
    assert "id=\"cleideFilterTransportadora\"" not in html
    assert "id=\"cleideFilterUfOrigem\"" not in html
    assert "id=\"cleideFilterUfDestino\"" not in html
    assert "id=\"cleideApplyFiltersBtn\"" not in html


def test_cleide_phase7_sem_assets_roberto(monkeypatch):
    html = _get_cleide_html(monkeypatch)
    assert "chat_roberto_fretes.js" not in html
    assert "roberto_bi.html" not in html
    assert "agent=\"roberto\"" not in html


def test_cleide_phase7_js_filtros_paginacao_ordenacao():
    js_path = Path(__file__).resolve().parents[1] / "app" / "static" / "js" / "cleide_auditoria_frete.js"
    source = js_path.read_text(encoding="utf-8")
    assert 'data-cleide-ui", "phase-7"' in source
    assert 'data-cleide-ui", "phase-3"' not in source
    assert 'data-cleide-filters-enabled", "true"' in source
    assert "data.stale_upload" in source
    assert "Estado inconsistente detectado. Upload anterior não encontrado. Limpeza necessária." in source
    assert "clearBtn.disabled = false;" in source
    assert "renderStructuralFromPayload(data);" in source
    assert "Dataset inválido: colunas obrigatórias ausentes" in source
    assert "Colunas detectadas:" in source
    assert "renderAnalytics(data || {});" in source
    assert "ensureCleideCharts" in source
    assert "updateCleideChart" in source
    assert "buildChartRows" in source
    assert "formatChartCurrency" in source
    assert "const isHorizontal = indexAxis === \"y\";" in source
    assert "const categoricalTickFormatter = (_v, index) => labels[index] || \"\";" in source
    assert "originCarrier" in source
    assert "cleideChartOriginCarrier" in source
    assert "scaleMode: \"weight\"" in source
    assert "updateParetoChart" in source
    assert "cleideChartParetoUf" in source
    assert "cleideChartParetoTransportadora" in source
    assert "pareto_fretes_zerados_uf_destino" in source
    assert "pareto_fretes_zerados_transportadora" in source
    assert "cleideQualityInvalidNumeric" in source
    assert "cleideNumericIssueAlertBtn" in source
    assert "updateNumericIssueAlert(summary);" in source
    assert "setDashboardState(\"Dashboard ativo\"" in source
    assert "Sem agregados no fallback" in source
    assert "MAX_PAGE_SIZE = 20" in source
    assert "MIN_PAGE_SIZE = 5" in source
    assert "const hiddenCharts = new Set();" in source
    assert "const hideChart = (chartKey) => {" in source
    assert "const showChart = (chartKey) => {" in source
    assert "const showAllCharts = () => {" in source
    assert "const applyChartVisibility = () => {" in source
    assert "const renderHiddenChartsControls = () => {" in source
    assert "const resizeVisibleCharts = (chartKeys = []) => {" in source
    assert 'dashboardGrid.classList.toggle("cleide-dashboard-grid--has-hidden", hiddenCount > 0);' in source
    assert "[data-cleide-hide-chart]" in source
    assert "[data-cleide-show-chart]" in source
    assert "instance.resize();" in source
    assert 'instance.update("none");' in source
    assert "applyBackendFilters" in source
    assert "/api/cleide/dashboard/filter" in source
    assert "fetch(\"/api/cleide/dashboard/filter\"" in source
    assert "data-cleide-remove-filter" in source
    assert "bindTableControls" in source
    assert "submitChatQuestion" in source
    assert "chatCopyLastBtn" in source
    assert "renderSafeStructuredText" in source
    assert "data-cleide-copy" in source
    assert "navigator.clipboard" in source
    assert "appendUserMessage" in source
    assert "appendCleideMessage" in source
    assert "appendLoadingMessage" in source
    assert "replaceLoading" in source
    assert "scrollChatToBottom" in source
    assert "setCleideChatOpen" in source
    assert "cleide-chat-panel--open" in source
    assert "chatToggle.addEventListener(\"click\"" in source
    assert "chatClose.addEventListener(\"click\"" in source
    assert "event.key === \"Escape\"" in source
    assert "fetch(\"/api/chat_cleide\"" in source
    assert "buildChatHistory" in source
    assert "MAX_HISTORY = maxChatHistory" in source
    assert "const history = buildChatHistory();" in source
    assert "appendUserMessage(question);" in source
    assert "body: JSON.stringify({ question, history })" in source
    assert source.index("const history = buildChatHistory();") < source.index("appendUserMessage(question);")
    assert "policyBlocked" in source
    assert "errorCode === \"provider_error\"" in source
    assert "Resposta da IA bloqueada pela política de segurança: fallback governado aplicado." in source
    assert "Provedor externo indisponível: resposta entregue por fallback governado." in source
    assert "IA desligada ou não autorizada: resposta em modo controlado." in source
    assert "Contexto operacional insuficiente: resposta limitada aos dados disponíveis." in source
    assert "sem chamada efetiva ao provedor externo" not in source
    assert "context_status" in source
    assert "view_scope" in source
    assert "active_filters" in source
    assert "clampPageSize" in source
    assert "KPIs filtrados (interseção real)" in source
    assert "interseção row-level no backend" in source
    assert "nextEl.dataset.maxPages" in source
    assert "cfg.page = Math.min(maxPages, cfg.page + 1);" in source
    assert "console.warn(\"[Cleide] Falha de comunicação no upload.\");" in source
    assert "applyFiltersBtn.addEventListener" not in source
    assert "filterDateStart.addEventListener(\"change\", autoApplyDateFilters);" in source
    assert "filterDateEnd.addEventListener(\"change\", autoApplyDateFilters);" in source
    assert "const autoApplyDateFilters = async () => {" in source
    assert "filterTransportadora = document.getElementById" not in source
    assert "filterUfOrigem = document.getElementById" not in source
    assert "filterUfDestino = document.getElementById" not in source
    assert "fillSelectOptions(" not in source
    assert "rawData" not in source
    assert "frontend filtering" not in source
    assert "resetFiltersBtn.addEventListener" in source
    assert "cleideKpiTotalDocumentos" in source


def test_cleide_chat_sequencial_fluxo_basico_estatico():
    js_path = Path(__file__).resolve().parents[1] / "app" / "static" / "js" / "cleide_auditoria_frete.js"
    source = js_path.read_text(encoding="utf-8")
    assert 'getElementById("cleideChatMessages")' in source
    assert "cleideChatResponse" not in source
    assert "appendUserMessage(question);" in source
    assert "const loadingRef = appendLoadingMessage();" in source
    assert "replaceLoading(loadingRef," in source
    assert "chatMessages.appendChild(msg);" in source
    assert "const renderSafeStructuredText = (text) => {" in source
    assert "const applyInlineMarkdown = (escapedText) => {" in source
    assert 'replace(/\\*\\*([^*\\n][^*\\n]*?)\\*\\*/g, "<strong>$1</strong>")' in source
    assert 'replace(/(^|[^\\*])\\*([^*\\n]+)\\*(?!\\*)/g, "$1<em>$2</em>")' in source
    assert "const bulletMatch = /^[-*]\\s+(.+)$/.exec(trimmed);" in source
    assert "out.push(`<li>${applyInlineMarkdown(esc(bulletMatch[1]))}</li>`);" in source
    assert "out.push(applyInlineMarkdown(esc(trimmed)));" in source
    assert 'replace(/</g, "&lt;")' in source
    assert 'replace(/>/g, "&gt;")' in source
    assert "inner.innerHTML = renderSafeStructuredText(cleanText);" in source
    assert "const content = String(inner?.textContent || \"\").trim();" in source
    assert "const text = String(msgNode?.querySelector(\".cleide-chat-msg__inner\")?.textContent || \"\").trim();" in source


def test_cleide_phase7_css_sem_nth_child_layout():
    css_path = Path(__file__).resolve().parents[1] / "app" / "static" / "css" / "cleide_auditoria_frete.css"
    source = css_path.read_text(encoding="utf-8")
    assert ".cleide-dashboard-grid > .col-12.col-lg-6:nth-child(3)" not in source
    assert ".cleide-dashboard-grid {" in source
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in source
    assert ".cleide-chart-card.is-hidden {" in source
    assert ".cleide-dashboard-grid.cleide-dashboard-grid--has-hidden .cleide-chart-card--wide {" in source
    assert "grid-column: auto;" in source
    assert ".cleide-chat-floating {" in source
    assert "pointer-events: none;" in source
    assert ".cleide-chat-toggle {" in source
    assert ".cleide-chat-panel {" in source
    assert "position: absolute;" in source
    assert "bottom: calc(100% + 0.65rem);" in source
    assert "visibility: hidden;" in source
    assert ".cleide-chat-panel {" in source
    assert ".cleide-chat-panel.cleide-chat-panel--open {" in source
    assert "visibility: visible;" in source
