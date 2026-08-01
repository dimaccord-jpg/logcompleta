"""Separação definitiva: dashboard gerencial na página × detalhe operacional no modal."""
from __future__ import annotations

import pathlib
import re


def _js() -> str:
    return pathlib.Path("app/static/js/agente_compara.js").read_text(encoding="utf-8")


def _html() -> str:
    return pathlib.Path("app/templates/agente_compara.html").read_text(encoding="utf-8")


def _fn(js: str, name: str, next_name: str | None = None) -> str:
    start = js.index(f"function {name}")
    if next_name:
        end = js.index(f"function {next_name}", start + 1)
        return js[start:end]
    return js[start : start + 12000]


def test_template_comparison_dashboard_below_chat_outside_modal():
    html = _html()
    dash_pos = html.index('id="agenteComparaComparisonDashboard"')
    chat_pos = html.index('id="agenteComparaMessages"')
    docs_pos = html.index('id="agenteComparaDocumentsPanel"')
    modal_pos = html.index('id="agenteComparaTempTableModal"')
    bi_pos = html.index('id="agenteComparaBiSection"')

    assert chat_pos < docs_pos < dash_pos < bi_pos < modal_pos

    modal_chunk = html[modal_pos : modal_pos + 2500]
    assert 'id="agenteComparaComparisonDashboard"' not in modal_chunk
    assert 'id="agenteComparaComparisonKpis"' not in modal_chunk
    assert 'id="agenteComparaComparisonCharts"' not in modal_chunk
    assert "agenteComparaChart_" not in modal_chunk


def test_template_dashboard_exclusive_ids_and_idle_cta():
    html = _html()
    for element_id in (
        "agenteComparaComparisonDashboard",
        "agenteComparaComparisonDashboardStatus",
        "agenteComparaComparisonKpis",
        "agenteComparaComparisonCharts",
        "agenteComparaComparisonDashboardUnavailable",
        "agenteComparaComparisonDashboardIdle",
        "agenteComparaComparisonDashboardOpenDetailsBtn",
        "agenteComparaComparisonDashboardTitle",
        "agenteComparaComparisonDashboardCustomize",
        "agenteComparaComparisonDashboardCustomizeBtn",
        "agenteComparaComparisonDashboardCustomizeMenu",
        "agenteComparaComparisonDashboardAllHidden",
        "agenteComparaComparisonDashboardLive",
    ):
        assert html.count(f'id="{element_id}"') == 1

    dash_start = html.index('id="agenteComparaComparisonDashboard"')
    dash_chunk = html[dash_start - 120 : dash_start + 220]
    assert "hidden" in dash_chunk
    assert "Ver cálculos detalhados" in html
    assert "Personalizar gráficos" in html
    assert 'aria-labelledby="agenteComparaComparisonDashboardTitle"' in html
    assert "agente-compara-comparison-dashboard-section" in html
    assert "cleide-audit-bi" not in html
    assert "agente_compara_dashboard_preferences_v1" not in html  # namespace só no JS


def test_dashboard_hidden_until_ready_and_after_reset():
    js = _js()
    clear_fn = _fn(js, "clearComparisonDashboardPanels", "openComparisonResultsDetailFromDashboard")
    assert "root.hidden = true" in clear_fn
    assert "idleEl.hidden = true" in clear_fn or "idleEl) idleEl.hidden = true" in clear_fn

    dash_fn = _fn(js, "renderComparisonResultsDashboard", "refreshComparisonDashboardView")
    assert "root.hidden = false" in dash_fn
    assert "clearComparisonDashboardPanels()" in dash_fn
    assert "isComparisonDashboardReady(vm)" in dash_fn


def test_dashboard_and_detail_renderers_are_separated():
    js = _js()
    assert "function renderComparisonResultsDashboard" in js
    assert "function renderComparisonResultsDetailTable" in js
    assert "function refreshComparisonDashboardView" in js
    assert "function refreshComparisonResultsDetailView" in js
    assert "function refreshComparisonCalculationViews" in js
    assert "function buildComparisonDashboardViewModel" in js
    assert "function buildComparisonDetailViewModel" in js
    assert "function openComparisonResultsDetailFromDashboard" in js

    detail_fn = _fn(js, "renderComparisonResultsDetailTable", "renderComparisonCalculationResults")
    assert "renderComparisonAnalyticsSummary" not in detail_fn
    assert "renderComparisonResultCharts" not in detail_fn
    assert "renderComparisonResultsFilters" in detail_fn
    assert "renderComparisonResultsTable" in detail_fn
    assert "renderComparisonResultsPagination" in detail_fn

    modal_fn = _fn(js, "renderComparisonCalculationResults", "applyComparisonCalculationPayload")
    assert "renderComparisonResultsDetailTable" in modal_fn
    assert "renderComparisonAnalyticsSummary" not in modal_fn
    assert "renderComparisonResultCharts" not in modal_fn

    dash_fn = _fn(js, "renderComparisonResultsDashboard", "refreshComparisonDashboardView")
    assert "renderComparisonAnalyticsSummary" in dash_fn
    assert "renderComparisonResultCharts" in dash_fn
    assert "renderComparisonResultsFilters" not in dash_fn
    assert "renderComparisonResultsTable" not in dash_fn
    assert "renderComparisonResultsPagination" not in dash_fn
    assert "Os indicadores deste resultado não estão disponíveis." in dash_fn
    assert "agenteComparaComparisonKpis" in dash_fn
    assert "agenteComparaComparisonCharts" in dash_fn


def test_filters_clear_do_not_destroy_charts():
    js = _js()
    reset_filters = _fn(js, "resetComparisonResultsUiState", "destroyComparisonResultCharts")
    assert "destroyComparisonResultCharts" not in reset_filters
    assert "comparisonResultsUiState.page = 1" in reset_filters
    detail_fn = _fn(js, "renderComparisonResultsDetailTable", "renderComparisonCalculationResults")
    assert "paint()" in detail_fn
    assert "renderComparisonResultCharts" not in detail_fn
    assert "renderComparisonAnalyticsSummary" not in detail_fn


def test_chart_lifecycle_contracts_in_source():
    js = _js()
    charts_fn = _fn(js, "renderComparisonResultCharts", "comparisonMemoryDisplayText")
    assert "destroyComparisonResultCharts()" in charts_fn
    assert "agenteComparaChart_" in charts_fn
    assert "addChartCard(\n        'coverage'" in charts_fn or "addChartCard(\n        \"coverage\"" in charts_fn or "'coverage'" in charts_fn
    assert "'without_complete'" in charts_fn
    assert "'comparability'" in charts_fn
    assert "'wins'" in charts_fn
    assert "'avg_cost'" in charts_fn
    assert "'potential_savings'" in charts_fn
    assert "agenteComparaChart_total" not in charts_fn
    assert "agenteComparaChart_mix" not in charts_fn
    assert "paintComparisonDashboardChart" in charts_fn
    assert "registerComparisonResultChart" in js
    assert "comparisonResultChartInstances.push" in js
    assert "renderComparisonGeographySection" in js
    assert "hasExecutiveComparisonAnalytics" in js

    reset_fn = _fn(js, "resetAgenteComparaFrontendState", "cacheReviewTempTableIfOwned")
    assert "destroyComparisonResultCharts()" in reset_fn
    assert "refreshComparisonDashboardView()" in reset_fn
    assert "closeComparisonCalculationMemory()" in reset_fn
    assert "resetComparisonResultsUiState()" in reset_fn

    close_modal = _fn(js, "closeTempTableModal", "handleTempTableModalSaveClick")
    assert "destroyComparisonResultCharts" not in close_modal


def test_payload_restore_and_process_refresh_dashboard():
    js = _js()
    restore = _fn(js, "restoreComparisonCalculationFromStatus", "processComparisonCalculations")
    assert "refreshComparisonDashboardView()" in restore
    assert "applyComparisonCalculationPayload" in restore

    process = _fn(js, "processComparisonCalculations", "clearCalculationFileSummary")
    assert "refreshComparisonDashboardView()" in process
    assert process.count("refreshComparisonDashboardView()") >= 2

    inject = js[js.index("agente-compara:inject-comparison-result") :]
    assert "refreshComparisonCalculationViews()" in inject


def test_cta_opens_existing_modal_without_fetch():
    js = _js()
    cta = _fn(js, "openComparisonResultsDetailFromDashboard", "bindComparisonDashboardDetailsButton")
    assert "fetch(" not in cta
    assert "API_COMPARISON_CALCULATE" not in cta
    assert "showTempTableModalShell()" in cta
    assert "configurationReviewTab = 'results'" in cta
    assert "destroyComparisonResultCharts" not in cta


def test_cleide_files_untouched_by_dashboard_split():
    root = pathlib.Path(".")
    cleide_hits = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        name = path.name.lower()
        if "cleide" in name and path.suffix in {".py", ".js", ".html", ".xlsx", ".md"}:
            # Apenas garante que esta suíte não altera Cleide: existência ok.
            cleide_hits.append(str(path).replace("\\", "/"))
    assert any("app/static/js/cleide_auditoria.js" in p for p in cleide_hits)
    assert "cleide_auditoria.js" not in _html()
    assert "/api/cleide-auditoria" not in _js()
    assert "cleide-audit-bi" not in _js()


def test_journey_ui_still_keeps_modal_detail_helpers():
    js = _js()
    assert "function renderComparisonResultsFilters" in js
    assert "function renderComparisonResultsPagination" in js
    assert "Não calculado — Ver motivo" in js
    assert "openComparisonCalculationMemory" in js
    assert re.search(r"function renderComparisonAnalyticsSummary\s*\(\s*container", js)
    assert re.search(r"function renderComparisonResultCharts\s*\(\s*container", js)


def test_executive_dashboard_sections_and_sample_base_copy():
    js = _js()
    summary = _fn(js, "renderComparisonAnalyticsSummary", "createFilterField")
    assert "Resumo executivo" in summary
    assert "Documentos comparáveis" in summary
    assert "Líder em vitórias" in summary
    assert "Economia potencial" in summary
    assert "Fretes sem cálculo completo" in summary
    assert "UFs com base comparável" in summary
    assert "menor tarifa calculada e a segunda menor" in summary
    assert "hasExecutiveComparisonAnalytics" in summary

    charts = _fn(js, "renderComparisonResultCharts", "comparisonMemoryDisplayText")
    assert "Confiabilidade da análise" in charts
    assert "Competitividade de custo" in charts
    assert "universo comparável" in charts
    assert "não representa economia realizada" in charts
    assert "renderComparisonGeographySection" in charts

    geo = _fn(js, "renderComparisonGeographySection", "renderComparisonResultCharts")
    assert "Visão geográfica" in geo
    assert "Baixa amostra" in geo
    assert "Sem base comparável" in geo
    assert "Ranking de UFs por economia potencial" in geo
    assert "Mapa do Brasil por transportadora vencedora" in geo
    assert "agenteComparaGeoMap" in geo
    assert "agenteComparaGeoMapLegend" in geo
    assert "Tabela geográfica por UF" in geo
    assert "mountComparisonBrasilMap" in geo
    assert "createElement" in geo
    assert "innerHTML" not in geo


def test_legacy_analytics_fallback_without_executive_fields():
    js = _js()
    summary = _fn(js, "renderComparisonAnalyticsSummary", "createFilterField")
    assert "Indicadores por transportadora" in summary
    assert "Totais brutos não substituem a análise no universo comparável" in summary
    charts = _fn(js, "renderComparisonResultCharts", "comparisonMemoryDisplayText")
    assert "Total calculado por cobertura individual" in charts
    assert "Não substitui a análise no universo comparável" in charts


def test_template_executive_subtitle_and_geo_styles():
    html = _html()
    assert "Visão executiva para escolha entre tabelas" in html
    assert "agente-compara-layout" in html
    assert "container-fluid agente-compara-layout" in html
    assert 'data-brasil-ufs-svg-url=' in html
    assert "agente-compara-geo-matrix" in html
    assert "agente-compara-geo-ranking-list" in html
    assert "agente-compara-geo-layout" in html
    assert "agente-compara-geo-map-wrap" in html
    assert "agente-compara-geo-legend" in html
    assert "agente-compara-analytics-note" in html
    assert "@media (min-width: 1200px)" in html
    assert "@media (max-width: 767px)" in html
    # Evita colisão com o seletor JS `.agente-compara-page` usado pelo host de detalhe/modal.
    assert 'class="container-fluid agente-compara-page' not in html
    assert "id=\"agenteComparaShell\" class=\"agente-compara-page\"" not in html


def test_brasil_map_helpers_use_local_svg_and_keyboard():
    js = _js()
    assert "function mountComparisonBrasilMap" in js
    assert "function paintComparisonBrasilMap" in js
    assert "function comparisonBrasilMapSvgUrl" in js
    assert "data-brasil-ufs-svg-url" in js
    assert "DOMParser" in js
    assert "tabindex" in js
    assert "aria-label" in js
    paint = _fn(js, "paintComparisonBrasilMap", "mountComparisonBrasilMap")
    assert "Enter" in paint
    assert "is-low-sample" in paint
    assert "comparisonUfMapFillColor" in paint
    mount = _fn(js, "mountComparisonBrasilMap", "renderComparisonResultCharts")
    assert "credentials: 'same-origin'" in mount or 'credentials: "same-origin"' in mount
    assert "importNode" in mount
    assert "innerHTML" not in mount