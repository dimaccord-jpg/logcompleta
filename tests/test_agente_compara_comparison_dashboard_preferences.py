"""Registry, preferências e recomposição do dashboard comparativo oficial."""
from __future__ import annotations

import json
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
    return js[start : start + 16000]


def _extract_widgets(js: str) -> list[dict]:
    block_start = js.index("var COMPARISON_DASHBOARD_WIDGETS = [")
    block_end = js.index("];", block_start)
    block = js[block_start:block_end]
    widgets = []
    for match in re.finditer(
        r"key:\s*'([^']+)'[\s\S]*?title:\s*'([^']+)'[\s\S]*?section:\s*'([^']+)'"
        r"[\s\S]*?type:\s*'([^']+)'[\s\S]*?size:\s*'([^']+)'[\s\S]*?order:\s*(\d+)"
        r"[\s\S]*?hideable:\s*(true|false)",
        block,
    ):
        widgets.append(
            {
                "key": match.group(1),
                "title": match.group(2),
                "section": match.group(3),
                "type": match.group(4),
                "size": match.group(5),
                "order": int(match.group(6)),
                "hideable": match.group(7) == "true",
            }
        )
    return widgets


def test_registry_keys_unique_ordered_and_isolated():
    js = _js()
    widgets = _extract_widgets(js)
    assert len(widgets) == 9
    keys = [w["key"] for w in widgets]
    assert keys == sorted(keys, key=lambda k: next(w["order"] for w in widgets if w["key"] == k))
    assert len(keys) == len(set(keys))
    orders = [w["order"] for w in widgets]
    assert orders == sorted(orders)
    assert len(orders) == len(set(orders))

    expected = {
        "coverage_by_carrier",
        "freight_without_complete_calculation",
        "comparability",
        "carrier_wins",
        "comparable_average_cost",
        "potential_savings",
        "winner_by_uf_map",
        "uf_savings_ranking",
        "uf_comparison_matrix",
    }
    assert set(keys) == expected
    assert all(w["hideable"] is True for w in widgets)
    assert {w["section"] for w in widgets} <= {"reliability", "competitiveness", "geography"}
    assert {w["type"] for w in widgets} <= {"chart", "map", "ranking", "matrix"}
    assert {w["size"] for w in widgets} <= {"standard", "wide", "full"}
    assert any(w["key"] == "winner_by_uf_map" and w["type"] == "map" for w in widgets)
    assert any(w["key"] == "uf_savings_ranking" and w["type"] == "ranking" for w in widgets)
    assert any(w["key"] == "uf_comparison_matrix" and w["type"] == "matrix" for w in widgets)

    # Título não é identidade: chaves estáveis distintas dos labels.
    for widget in widgets:
        assert widget["key"] != widget["title"]

    block_start = js.index("var COMPARISON_DASHBOARD_WIDGETS = [")
    block_end = js.index("];", block_start)
    block = js[block_start:block_end]

    # Isolamento Cleide / chaves de auditoria no registry.
    assert "transportadora" not in keys
    assert "uf_destino" not in keys
    assert "pareto_transportadora" not in keys
    assert "temporal" not in keys
    assert "AUDIT_BI_CHART" not in block
    assert "cleide-audit-bi" not in block
    assert "cleideAuditBi" not in block
    assert "agente_compara_dashboard_preferences_v1" in js


def test_preferences_contract_load_save_normalize():
    js = _js()
    assert "agente_compara_dashboard_preferences_v1" in js
    assert "COMPARISON_DASHBOARD_PREFERENCES_VERSION = 1" in js
    assert "function loadComparisonDashboardPreferences" in js
    assert "function saveComparisonDashboardPreferences" in js
    assert "function normalizeComparisonDashboardPreferences" in js
    assert "function hideComparisonDashboardWidget" in js
    assert "function showComparisonDashboardWidget" in js
    assert "function showAllComparisonDashboardWidgets" in js

    normalize = _fn(js, "normalizeComparisonDashboardPreferences", "loadComparisonDashboardPreferences")
    assert "Number(raw.version) !== COMPARISON_DASHBOARD_PREFERENCES_VERSION" in normalize
    assert "getComparisonDashboardWidget(key)" in normalize
    assert "hideable !== true" in normalize

    load = _fn(js, "loadComparisonDashboardPreferences", "saveComparisonDashboardPreferences")
    assert "JSON.parse" in load
    assert "defaultComparisonDashboardPreferences()" in load

    save = _fn(js, "saveComparisonDashboardPreferences", "isComparisonDashboardWidgetHidden")
    assert "localStorage.setItem" in save
    assert "hidden: normalized.hidden.slice()" in save
    assert "analytics" not in save
    assert "comparative_rows" not in save

    # Preferências sobrevivem ao reset operacional.
    reset_fn = _fn(js, "resetAgenteComparaFrontendState", "cacheReviewTempTableIfOwned")
    assert "destroyComparisonResultCharts()" in reset_fn
    assert "localStorage.removeItem" not in reset_fn
    assert "comparisonDashboardPreferences.hidden = []" not in reset_fn

    invalidate = _fn(js, "invalidateComparisonDerivedState", "syncComparisonStateFromPayload")
    assert "localStorage.removeItem" not in invalidate


def test_preferences_schema_example_is_visual_only():
    schema = {"version": 1, "hidden": ["coverage_by_carrier", "winner_by_uf_map"]}
    encoded = json.dumps(schema)
    parsed = json.loads(encoded)
    assert set(parsed.keys()) == {"version", "hidden"}
    assert all(isinstance(k, str) for k in parsed["hidden"])


def test_grid_and_ui_contracts_css_first():
    js = _js()
    html = _html()
    assert "agente-compara-dashboard-widget--standard" in html
    assert "agente-compara-dashboard-widget--wide" in html
    assert "agente-compara-dashboard-widget--full" in html
    assert "agente-compara-section-grid--visible-1" in html
    assert "agente-compara-section-grid--visible-2" in html
    assert "agente-compara-section-grid--visible-3-plus" in html
    assert "repeat(3, minmax(0, 1fr))" in html
    assert "repeat(2, minmax(0, 1fr))" in html
    assert "display: none !important" in html
    assert "is-map-only" in html
    assert "is-rank-only" in html
    assert "is-geo-empty" in html
    assert "max-width: 1600px" in html
    assert "position: absolute" in html  # menu popover only
    assert "Personalizar gráficos" in html
    assert "Reexibir todos" in html
    assert "Todos os gráficos estão ocultos." in html
    assert "Reexibir gráficos" in html
    assert 'id="agenteComparaComparisonDashboardCustomize"' in html
    assert 'id="agenteComparaComparisonDashboardLive"' in html
    assert "aria-expanded" in html
    assert "aria-controls" in html

    assert "function updateComparisonDashboardSectionLayout" in js
    assert "function resizeComparisonDashboardVisibleCharts" in js
    assert "requestAnimationFrame" in _fn(js, "resizeComparisonDashboardVisibleCharts", "updateComparisonDashboardSectionLayout")
    assert "setTimeout" not in _fn(js, "resizeComparisonDashboardVisibleCharts", "updateComparisonDashboardSectionLayout")

    layout = _fn(js, "updateComparisonDashboardSectionLayout", "updateComparisonDashboardSectionVisibility")
    assert "comparisonDashboardVisibleCountClass" in layout
    assert "agente-compara-section-grid--visible-1" in layout
    assert "agente-compara-section-grid--visible-2" in layout
    assert "agente-compara-section-grid--visible-3-plus" in layout
    assert "count === 0" in layout

    apply = _fn(js, "applyComparisonDashboardWidgetVisibility", "recreateComparisonDashboardChart")
    assert "card.hidden = hidden" in apply
    assert "destroyComparisonResultChartByWidgetKey" in apply
    assert "resizeComparisonDashboardVisibleCharts()" in apply
    assert "placeholder" not in apply.lower()

    charts = _fn(js, "renderComparisonResultCharts", "comparisonMemoryDisplayText")
    assert "data-comparison-dashboard-section-grid" in charts
    assert "data-comparison-dashboard-section-block" in charts
    assert "ensureSection" in charts

    hide = _fn(js, "hideComparisonDashboardWidget", "showComparisonDashboardWidget")
    assert "saveComparisonDashboardPreferences" in hide
    assert "announceComparisonDashboardPreference" in hide
    assert "focusCandidate.focus()" in hide

    show_all = _fn(js, "showAllComparisonDashboardWidgets", "bindComparisonDashboardCustomizeControls")
    assert "Todos os gráficos foram reexibidos." in show_all
    assert "hidden = []" in show_all
    assert "resizeComparisonDashboardVisibleCharts()" in show_all


def test_chart_lifecycle_respects_hidden_preferences():
    js = _js()
    charts = _fn(js, "renderComparisonResultCharts", "comparisonMemoryDisplayText")
    assert "loadComparisonDashboardPreferences()" in charts
    assert "isComparisonDashboardWidgetHidden(ref.widgetKey)" in charts
    assert "paintComparisonDashboardChart" in charts
    assert "coverage_by_carrier" in charts
    assert "'coverage'" in charts or '"coverage"' in charts
    assert "agenteComparaChart_" in charts
    assert "data-comparison-dashboard-hide-widget" in charts

    paint = _fn(js, "paintComparisonDashboardChart", "renderComparisonResultCharts")
    assert "isComparisonDashboardWidgetHidden(widgetKey)" in paint
    assert "registerComparisonResultChart(widgetKey, chart)" in paint
    assert "comparisonResultChartInstances.push" in js

    destroy = _fn(js, "destroyComparisonResultCharts", "comparisonDashboardWidgetSizeClass")
    assert "comparisonResultChartInstancesByWidgetKey" in destroy

    dash = _fn(js, "renderComparisonResultsDashboard", "refreshComparisonDashboardView")
    assert "loadComparisonDashboardPreferences()" in dash
    assert "bindComparisonDashboardCustomizeControls()" in dash
    assert "updateComparisonDashboardCustomizeMenu()" in dash


def test_geography_widgets_registered_and_hideable():
    js = _js()
    geo = _fn(js, "renderComparisonGeographySection", "ensureComparisonGeographyWidgetsMounted")
    assert 'data-comparison-dashboard-widget", "winner_by_uf_map"' in geo or "winner_by_uf_map" in geo
    assert "uf_savings_ranking" in geo
    assert "uf_comparison_matrix" in geo
    assert "Ocultar gráfico Mapa de vencedora por UF" in geo
    assert "Ocultar gráfico Ranking geográfico" in geo
    assert "Ocultar gráfico Matriz geográfica" in geo
    assert "isComparisonDashboardWidgetHidden('winner_by_uf_map')" in geo
    assert "mountComparisonBrasilMap" in geo

    hide = _fn(js, "hideComparisonDashboardWidget", "showComparisonDashboardWidget")
    assert "agenteComparaGeoMap" in hide
    assert "agenteComparaGeoMapDetail" in hide


def test_official_dashboard_only_not_legacy_bi():
    html = _html()
    dash = html[html.index('id="agenteComparaComparisonDashboard"') : html.index('id="agenteComparaBiSection"')]
    assert "agenteComparaComparisonDashboardCustomize" in dash
    assert "data-audit-bi-hide-chart" not in dash
    bi = html[html.index('id="agenteComparaBiSection"') : html.index('id="agenteComparaBiSection"') + 2500]
    assert "agenteComparaComparisonDashboardCustomize" not in bi


def test_no_backend_preference_routes():
    js = _js()
    assert "/api/agente-compara/dashboard/preferences" not in js
    assert "dashboard_preferences" not in js or "agente_compara_dashboard_preferences_v1" in js
    html = _html()
    assert "/api/agente-compara/dashboard/preferences" not in html
