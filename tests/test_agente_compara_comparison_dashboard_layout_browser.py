"""Browser: widgets restantes preenchem a largura após ocultar/reexibir."""
from __future__ import annotations

import pytest

playwright = pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402

pytest_plugins = ["tests.test_agente_compara_freight_modal_browser"]

from tests.test_agente_compara_freight_modal_browser import (  # noqa: E402
    _FlowState,
    _comparison_analytics_fixture,
    _comparison_result_fixture,
    _install_routes,
)

STORAGE_KEY = "agente_compara_dashboard_preferences_v1"


def _inject(page, result, analytics):
    page.evaluate(
        """({ result, analytics }) => {
          document.dispatchEvent(new CustomEvent('agente-compara:inject-comparison-result', {
            detail: {
              status: 'CALCULATION_READY',
              billing_status: 'applied',
              current_step: 'CALCULATION_READY',
              stale: false,
              result,
              analytics
            }
          }));
        }""",
        {"result": result, "analytics": analytics},
    )


def _section_metrics(page, section_key):
    return page.evaluate(
        """(sectionKey) => {
          const grid = document.querySelector(
            '[data-comparison-dashboard-section-grid=\"' + sectionKey + '\"]'
          );
          if (!grid) return null;
          const cards = Array.from(
            grid.querySelectorAll('[data-comparison-dashboard-widget]:not([hidden])')
          );
          const gridBox = grid.getBoundingClientRect();
          const cardBoxes = cards.map((el) => {
            const b = el.getBoundingClientRect();
            return {
              width: b.width,
              left: b.left,
              right: b.right,
              key: el.getAttribute('data-comparison-dashboard-widget')
            };
          });
          const occupied = cardBoxes.reduce((sum, b) => sum + b.width, 0);
          const span = cardBoxes.length
            ? Math.max(...cardBoxes.map((b) => b.right)) - Math.min(...cardBoxes.map((b) => b.left))
            : 0;
          return {
            count: cards.length,
            className: grid.className,
            gridWidth: gridBox.width,
            occupied,
            span,
            fillRatio: gridBox.width ? span / gridBox.width : 0,
            cardWidths: cardBoxes.map((b) => b.width)
          };
        }""",
        section_key,
    )


def test_browser_dashboard_layout_reflow_desktop(live_base_url):
    result = _comparison_result_fixture()
    analytics = _comparison_analytics_fixture(result)
    state = _FlowState(promote_after_status_calls=2)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        _install_routes(page, state)
        page.goto(f"{live_base_url}/agente-compara", wait_until="domcontentloaded")
        page.evaluate("(key) => window.localStorage.removeItem(key)", STORAGE_KEY)
        _inject(page, result, analytics)
        page.wait_for_selector("#agenteComparaComparisonDashboard:not([hidden])")
        page.wait_for_selector('[data-comparison-dashboard-section-grid="reliability"]')

        m3 = _section_metrics(page, "reliability")
        assert m3["count"] == 3
        assert "visible-3-plus" in m3["className"]
        assert m3["fillRatio"] >= 0.88
        assert min(m3["cardWidths"]) > 250

        page.click('[data-comparison-dashboard-hide-widget="comparability"]')
        page.wait_for_function(
            """() => {
              const grid = document.querySelector('[data-comparison-dashboard-section-grid=\"reliability\"]');
              return grid && grid.classList.contains('agente-compara-section-grid--visible-2');
            }"""
        )
        m2 = _section_metrics(page, "reliability")
        assert m2["count"] == 2
        assert "visible-2" in m2["className"]
        assert m2["fillRatio"] >= 0.90
        assert abs(m2["cardWidths"][0] - m2["cardWidths"][1]) < 80
        assert m2["occupied"] / m2["gridWidth"] >= 0.88

        page.click('[data-comparison-dashboard-hide-widget="freight_without_complete_calculation"]')
        page.wait_for_function(
            """() => {
              const grid = document.querySelector('[data-comparison-dashboard-section-grid=\"reliability\"]');
              return grid && grid.classList.contains('agente-compara-section-grid--visible-1');
            }"""
        )
        m1 = _section_metrics(page, "reliability")
        assert m1["count"] == 1
        assert "visible-1" in m1["className"]
        assert m1["fillRatio"] >= 0.92
        assert m1["cardWidths"][0] >= m1["gridWidth"] * 0.90

        page.click("#agenteComparaComparisonDashboardCustomizeBtn")
        page.click("#agenteComparaComparisonDashboardShowAllBtn")
        page.wait_for_function(
            """() => {
              const grid = document.querySelector('[data-comparison-dashboard-section-grid=\"reliability\"]');
              return grid && grid.classList.contains('agente-compara-section-grid--visible-3-plus');
            }"""
        )
        assert _section_metrics(page, "reliability")["count"] == 3

        page.click('[data-comparison-dashboard-hide-widget="potential_savings"]')
        page.wait_for_function(
            """() => document.querySelector('[data-comparison-dashboard-section-grid=\"competitiveness\"]')
              .classList.contains('agente-compara-section-grid--visible-2')"""
        )
        c2 = _section_metrics(page, "competitiveness")
        assert c2["count"] == 2
        assert c2["fillRatio"] >= 0.90

        page.click('[data-comparison-dashboard-hide-widget="uf_savings_ranking"]')
        page.wait_for_function(
            """() => document.querySelector('.agente-compara-geo-layout').classList.contains('is-map-only')"""
        )
        geo = page.evaluate(
            """() => {
              const layout = document.querySelector('.agente-compara-geo-layout');
              const map = document.querySelector('[data-comparison-dashboard-widget=\"winner_by_uf_map\"]');
              const lb = layout.getBoundingClientRect();
              const mb = map.getBoundingClientRect();
              return { fill: mb.width / lb.width, mapOnly: layout.classList.contains('is-map-only') };
            }"""
        )
        assert geo["mapOnly"] is True
        assert geo["fill"] >= 0.90

        chart_count = page.evaluate(
            """() => {
              if (!(window.Chart && Chart.getChart)) return null;
              return ['coverage','without_complete','comparability','wins','avg_cost','potential_savings']
                .map((k) => Chart.getChart('agenteComparaChart_' + k))
                .filter(Boolean).length;
            }"""
        )
        assert chart_count == 5

        browser.close()


def test_browser_dashboard_layout_reflow_responsive(live_base_url):
    result = _comparison_result_fixture()
    analytics = _comparison_analytics_fixture(result)
    state = _FlowState(promote_after_status_calls=2)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        page = browser.new_page(viewport={"width": 1100, "height": 800})
        _install_routes(page, state)
        page.goto(f"{live_base_url}/agente-compara", wait_until="domcontentloaded")
        page.evaluate("(key) => window.localStorage.removeItem(key)", STORAGE_KEY)
        _inject(page, result, analytics)
        page.wait_for_selector('[data-comparison-dashboard-section-grid="reliability"]')
        page.click('[data-comparison-dashboard-hide-widget="comparability"]')
        page.wait_for_function(
            """() => document.querySelector('[data-comparison-dashboard-section-grid=\"reliability\"]')
              .classList.contains('agente-compara-section-grid--visible-2')"""
        )
        m = _section_metrics(page, "reliability")
        assert m["count"] == 2
        assert m["fillRatio"] >= 0.88
        page.close()

        page = browser.new_page(viewport={"width": 820, "height": 1100})
        _install_routes(page, state)
        page.goto(f"{live_base_url}/agente-compara", wait_until="domcontentloaded")
        page.evaluate("(key) => window.localStorage.removeItem(key)", STORAGE_KEY)
        _inject(page, result, analytics)
        page.wait_for_selector('[data-comparison-dashboard-section-grid="reliability"]')
        page.click('[data-comparison-dashboard-hide-widget="comparability"]')
        page.wait_for_function(
            """() => document.querySelector('[data-comparison-dashboard-section-grid=\"reliability\"]')
              .classList.contains('agente-compara-section-grid--visible-2')"""
        )
        assert _section_metrics(page, "reliability")["fillRatio"] >= 0.85
        page.close()

        page = browser.new_page(viewport={"width": 390, "height": 844})
        _install_routes(page, state)
        page.goto(f"{live_base_url}/agente-compara", wait_until="domcontentloaded")
        page.evaluate("(key) => window.localStorage.removeItem(key)", STORAGE_KEY)
        _inject(page, result, analytics)
        page.wait_for_selector('[data-comparison-dashboard-section-grid="reliability"]')
        mobile = page.evaluate(
            """() => {
              const grid = document.querySelector('[data-comparison-dashboard-section-grid=\"reliability\"]');
              const cards = Array.from(grid.querySelectorAll('[data-comparison-dashboard-widget]:not([hidden])'));
              const gb = grid.getBoundingClientRect();
              const widths = cards.map((c) => c.getBoundingClientRect().width);
              const doc = document.documentElement;
              return {
                oneColumn: getComputedStyle(grid).gridTemplateColumns.split(' ').length === 1,
                fillMin: Math.min(...widths) / gb.width,
                overflowX: doc.scrollWidth > doc.clientWidth + 2
              };
            }"""
        )
        assert mobile["oneColumn"] is True
        assert mobile["fillMin"] >= 0.92
        assert mobile["overflowX"] is False

        browser.close()
