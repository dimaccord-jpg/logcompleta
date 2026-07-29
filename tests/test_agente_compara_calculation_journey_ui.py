"""Contratos de UI da jornada de cálculo (Etapas 6–8) do AgenteCompara."""
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
    return js[start : start + 8000]


def test_configuration_ready_shows_confirmation_cta():
    js = _js()
    assert "function renderConfigurationReadyConfirmation" in js
    assert "As configurações estão prontas. O cálculo será iniciado somente após sua confirmação." in js
    assert "agenteComparaProcessCalculationsButton" in js
    assert "Processar Cálculos" in js
    assert "próxima etapa" not in _fn(js, "setComparisonCommonParamsModalHeader", "activateComparisonCommonParamsStep")


def test_upload_success_does_not_auto_calculate():
    js = _js()
    upload_fn = _fn(js, "uploadAuditFile", "ensureCoverageTableShell")
    assert "comparison/calculate" not in upload_fn
    assert "processComparisonCalculations()" not in upload_fn
    assert "API_AUDIT_RUN" not in upload_fn


def test_process_calculations_posts_once_with_guard():
    js = _js()
    process_fn = _fn(js, "processComparisonCalculations", "clearCalculationFileSummary")
    assert "if (comparisonCalculationInFlight) return;" in process_fn
    assert "API_COMPARISON_CALCULATE" in process_fn
    assert "comparison/calculate" in js
    assert process_fn.count("fetch(API_COMPARISON_CALCULATE") == 1
    assert "API_AUDIT_RUN" not in process_fn
    assert "/audit/run" not in process_fn


def test_running_pending_failed_ready_and_stale_messages():
    js = _js()
    status_fn = _fn(js, "renderComparisonCalculationStatus", "comparisonRowIssueDate")
    assert "Processando cálculos comparativos..." in status_fn
    assert "Finalizando processamento..." in status_fn
    assert "Não foi possível concluir a regularização da execução." in status_fn
    assert "Cálculos concluídos" in status_fn
    assert "As configurações foram alteradas. Processe novamente para atualizar os resultados." in status_fn


def test_button_labels_for_billing_failed_and_calculation_failed():
    js = _js()
    btn_fn = _fn(js, "setProcessCalculationsButtonState", "bindProcessCalculationsButton")
    assert "Tentar novamente" in btn_fn
    assert "Processar novamente" in btn_fn


def test_results_tab_and_review_mode_cover_calculation_steps():
    js = _js()
    assert "function isComparisonReviewMode" in js
    assert "isComparisonPostConfigStep()" in _fn(js, "isComparisonReviewMode", "shouldEnableResultsReviewTab")
    assert "agenteComparaReviewTabResults" in js
    assert "Resultados" in js
    assert "configurationReviewTab = 'results'" in _fn(js, "processComparisonCalculations", "clearCalculationFileSummary")
    assert "shouldEnableResultsReviewTab" in js


def test_pending_and_running_do_not_render_result_table():
    js = _js()
    render_fn = _fn(js, "renderComparisonCalculationResults", "applyComparisonCalculationPayload")
    assert "billing === 'pending' || billing === 'failed'" in render_fn
    assert "CALCULATION_RUNNING" in render_fn
    assert "comparisonCalculationInFlight" in render_fn


def test_analytics_summary_and_neutral_language():
    js = _js()
    assert "function renderComparisonAnalyticsSummary" in js
    assert "Resumo Comparativo" in js
    analytics_fn = _fn(js, "renderComparisonAnalyticsSummary", "createFilterField")
    for forbidden in (
        "vencedor",
        "winner",
        "ranking",
        "economia",
        "recommendation",
        "melhor transportadora",
        "troféu",
        "medalha",
    ):
        assert forbidden not in analytics_fn.lower()


def test_filters_pagination_and_charts_helpers_exist():
    js = _js()
    assert "function filterComparativeRows" in js
    assert "function paginateRows" in js
    assert "function renderComparisonResultsFilters" in js
    assert "function renderComparisonResultsPagination" in js
    assert "function renderComparisonResultCharts" in js
    assert "agenteComparaResultsPageSize" in js
    assert "25" in _fn(js, "renderComparisonResultsPagination", "renderComparisonResultCharts")
    assert "50" in _fn(js, "renderComparisonResultsPagination", "renderComparisonResultCharts")
    assert "100" in _fn(js, "renderComparisonResultsPagination", "renderComparisonResultCharts")
    assert "Exibindo " in js
    assert "window.Chart" in _fn(js, "renderComparisonResultCharts", "renderComparisonResultsTable")


def test_results_table_uses_text_content_and_no_storage_path():
    js = _js()
    table_fn = _fn(js, "renderComparisonResultsTable", "refreshComparisonResultsView")
    assert "textContent" in table_fn
    assert "innerHTML" not in table_fn
    assert "result_storage_key" not in table_fn
    assert "Não calculado" in table_fn
    process_fn = _fn(js, "processComparisonCalculations", "clearCalculationFileSummary")
    assert "result_storage_key" not in process_fn
    assert "storage_path" not in process_fn


def test_html_has_chartjs_and_results_css():
    html = _html()
    assert "chart.js@4.4.1" in html
    assert "agente-compara-configuration-ready-confirmation" in html
    assert "agente-compara-analytics-grid" in html
    assert "agente-compara-results-filters-grid" in html
    assert "agente-compara-results-charts-grid" in html


def test_no_forbidden_comparative_fields_in_new_ui_helpers():
    js = _js()
    chunk = js[
        js.index("function comparisonRowIssueDate") : js.index("function applyComparisonCalculationPayload")
    ]
    forbidden = [
        "valor_frete",
        "charged_freight",
        "expected_freight",
        "overcharged",
        "undercharged",
        "winning_carrier",
        "cheapest_carrier",
        "savings",
        "economy",
        "recommendation",
    ]
    for key in forbidden:
        assert key not in chunk
    # "ranking" como conceito de vencedor continua proibido como identificador/campo.
    assert "winning_carrier" not in chunk
    assert re.search(r"\branking\b", chunk) is None or "sem ordenação automática" in chunk


def test_same_carrier_disambiguation_preserved():
    js = _js()
    assert "function disambiguateCarrierColumnTitle" in js
    assert "— Tabela " in _fn(js, "disambiguateCarrierColumnTitle", "clearComparisonCalculationResults")


def test_restore_prefers_results_tab_when_calculation_active():
    js = _js()
    restore = _fn(js, "restoreComparisonCalculationFromStatus", "processComparisonCalculations")
    assert "shouldEnableResultsReviewTab()" in restore
    assert "configurationReviewTab = 'results'" in restore
    assert "applyComparisonCalculationPayload" in restore
