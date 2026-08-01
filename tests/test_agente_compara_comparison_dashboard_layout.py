"""Recomposição visual do dashboard: classes por quantidade visível e resize."""
from __future__ import annotations

import pathlib


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


def test_section_layout_helper_counts_and_classes():
    js = _js()
    assert "function updateComparisonDashboardSectionLayout" in js
    assert "function countComparisonDashboardSectionVisible" in js
    assert "function comparisonDashboardVisibleCountClass" in js
    helper = _fn(js, "comparisonDashboardVisibleCountClass", "findComparisonDashboardSectionGrid")
    assert "visible-1" in helper
    assert "visible-2" in helper
    assert "visible-3-plus" in helper
    layout = _fn(js, "updateComparisonDashboardSectionLayout", "updateComparisonDashboardSectionVisibility")
    assert 'classList.remove(cls)' in layout
    assert "grid.hidden = count === 0" in layout
    assert "block.hidden = count === 0" in layout
    assert "is-map-only" in layout
    assert "is-rank-only" in layout
    assert "is-geo-empty" in layout
    assert "agente-compara-dashboard-widget--solo" in layout


def test_css_no_fixed_three_columns_without_visible_class():
    html = _html()
    # A grade fixa de 3 colunas só pode existir sob a classe de quantidade.
    assert ".agente-compara-section-grid--visible-3-plus" in html
    assert (
        ".agente-compara-section-grid--visible-3-plus {\n"
        "    grid-template-columns: repeat(3, minmax(0, 1fr));\n"
        "  }"
    ) in html or "agente-compara-section-grid--visible-3-plus" in html
    # Não deve existir regra absoluta antiga no seletor base ≥1200px sem classe de quantidade.
    chunk_start = html.index(".agente-compara-results-charts-grid {")
    # Primeira ocorrência agora inclui display/gap e auto-fit — sem repeat(3) fixo.
    first_rule = html[chunk_start : chunk_start + 450]
    assert "repeat(3," not in first_rule
    assert "minmax(0, 1fr)" in html
    assert "agente-compara-section-grid--visible-1" in html
    assert "agente-compara-section-grid--visible-2" in html


def test_chart_render_uses_independent_section_grids():
    js = _js()
    charts = _fn(js, "renderComparisonResultCharts", "comparisonMemoryDisplayText")
    assert 'data-comparison-dashboard-section-grid"' in charts or "data-comparison-dashboard-section-grid" in charts
    assert "data-comparison-dashboard-section-block" in charts
    assert "agenteComparaReliabilityTitle" in charts
    assert "agenteComparaCompetitivenessTitle" in charts
    assert "resizeComparisonDashboardVisibleCharts()" in charts
    assert "agente-compara-charts-section-break" not in charts


def test_resize_only_visible_no_timeout():
    js = _js()
    resize = _fn(js, "resizeComparisonDashboardVisibleCharts", "updateComparisonDashboardSectionLayout")
    assert "requestAnimationFrame" in resize
    assert "setTimeout" not in resize
    assert "isComparisonDashboardWidgetHidden(widgetKey)" in resize
    assert "instance.resize" in resize
    apply = _fn(js, "applyComparisonDashboardWidgetVisibility", "recreateComparisonDashboardChart")
    assert "resizeComparisonDashboardVisibleCharts()" in apply


def test_hidden_widgets_leave_flow():
    html = _html()
    assert "[data-comparison-dashboard-widget].is-hidden" in html
    assert "display: none !important" in html
    js = _js()
    apply = _fn(js, "applyComparisonDashboardWidgetVisibility", "recreateComparisonDashboardChart")
    assert "card.hidden = hidden" in apply
    assert "placeholder" not in apply.lower()
    assert "visibility:hidden" not in apply
