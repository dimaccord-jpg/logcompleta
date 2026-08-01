"""Browser: personalização do dashboard comparativo oficial (desktop/tablet/mobile)."""
from __future__ import annotations

import json

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
CLEIDE_KEY_PROBE = "cleide_audit_bi_hidden_charts_v1"


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


def _chart_js_count(page):
    return page.evaluate(
        """() => {
          if (!(window.Chart && Chart.getChart)) return null;
          return ['coverage','without_complete','comparability','wins','avg_cost','potential_savings']
            .map((k) => Chart.getChart('agenteComparaChart_' + k))
            .filter(Boolean).length;
        }"""
    )


def _prefs(page):
    return page.evaluate(
        """(key) => {
          try {
            const raw = window.localStorage.getItem(key);
            return raw ? JSON.parse(raw) : null;
          } catch (e) {
            return null;
          }
        }""",
        STORAGE_KEY,
    )


def test_browser_comparison_dashboard_personalization_desktop_refresh_reset(live_base_url):
    result = _comparison_result_fixture()
    analytics = _comparison_analytics_fixture(result)
    state = _FlowState(promote_after_status_calls=2)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        _install_routes(page, state)
        page.goto(f"{live_base_url}/agente-compara", wait_until="domcontentloaded")
        page.evaluate(
            """({ key, cleide }) => {
              window.localStorage.removeItem(key);
              window.localStorage.setItem(cleide, JSON.stringify({ transportadora: true }));
            }""",
            {"key": STORAGE_KEY, "cleide": CLEIDE_KEY_PROBE},
        )

        assert page.locator("#agenteComparaComparisonDashboardCustomize").get_attribute("hidden") is not None

        _inject(page, result, analytics)
        page.wait_for_selector("#agenteComparaComparisonDashboard:not([hidden])")
        page.wait_for_selector("#agenteComparaComparisonDashboardCustomize:not([hidden])")
        page.wait_for_selector("#agenteComparaChart_coverage")
        page.wait_for_selector("#agenteComparaResultsGeography")

        assert _chart_js_count(page) == 6
        assert page.locator('[data-comparison-dashboard-widget="coverage_by_carrier"]:not([hidden])').count() == 1

        page.click('[data-comparison-dashboard-hide-widget="coverage_by_carrier"]')
        page.wait_for_function(
            """() => {
              const card = document.querySelector('[data-comparison-dashboard-widget="coverage_by_carrier"]');
              return !!(card && card.hidden);
            }"""
        )
        assert page.locator('[data-comparison-dashboard-widget="coverage_by_carrier"][hidden]').count() == 1
        assert _chart_js_count(page) == 5
        prefs = _prefs(page)
        assert prefs == {"version": 1, "hidden": ["coverage_by_carrier"]}

        layout = page.evaluate(
            """() => {
              const grid = document.querySelector(
                '[data-comparison-dashboard-section-grid=\"reliability\"]'
              );
              const hidden = Array.from(grid.querySelectorAll('[data-comparison-dashboard-widget]'))
                .filter((el) => el.hidden || el.classList.contains('is-hidden'));
              return {
                hasPlaceholder: !!grid.querySelector('.placeholder, [data-placeholder]'),
                hiddenCount: hidden.length,
                displayNone: hidden.every((el) => getComputedStyle(el).display === 'none'),
                className: grid.className
              };
            }"""
        )
        assert layout["hasPlaceholder"] is False
        assert layout["hiddenCount"] == 1
        assert layout["displayNone"] is True
        assert "visible-2" in layout["className"]

        page.click('[data-comparison-dashboard-hide-widget="winner_by_uf_map"]')
        page.wait_for_function(
            """() => {
              const map = document.querySelector('[data-comparison-dashboard-widget="winner_by_uf_map"]');
              return !!(map && map.hidden);
            }"""
        )
        assert page.locator('[data-comparison-dashboard-widget="uf_savings_ranking"]:not([hidden])').count() == 1
        assert page.locator('[data-comparison-dashboard-widget="uf_comparison_matrix"]:not([hidden])').count() == 1
        assert page.locator("#agenteComparaGeoMap svg").count() == 0
        prefs = _prefs(page)
        assert set(prefs["hidden"]) == {"coverage_by_carrier", "winner_by_uf_map"}

        page.click("#agenteComparaComparisonDashboardCustomizeBtn")
        page.wait_for_selector("#agenteComparaComparisonDashboardCustomizeMenu:not([hidden])")
        assert "Gráficos ocultos (2)" in page.locator("#agenteComparaComparisonDashboardCustomizeBtn").inner_text()
        page.click('[data-comparison-dashboard-show-widget="coverage_by_carrier"]')
        page.wait_for_selector('[data-comparison-dashboard-widget="coverage_by_carrier"]:not([hidden])')
        order = page.evaluate(
            """() => Array.from(
              document.querySelectorAll('#agenteComparaResultsCharts [data-comparison-dashboard-widget]:not([hidden])')
            ).map((el) => el.getAttribute('data-comparison-dashboard-widget'))"""
        )
        assert order[0] == "coverage_by_carrier"
        assert _chart_js_count(page) == 6

        page.reload(wait_until="domcontentloaded")
        _install_routes(page, state)
        page.wait_for_selector("#agenteComparaShell")
        prefs_after_reload = _prefs(page)
        assert prefs_after_reload["hidden"] == ["winner_by_uf_map"]
        _inject(page, result, analytics)
        page.wait_for_selector("#agenteComparaComparisonDashboard:not([hidden])")
        page.wait_for_selector('[data-comparison-dashboard-widget="coverage_by_carrier"]:not([hidden])')
        page.wait_for_function(
            """() => {
              const map = document.querySelector('[data-comparison-dashboard-widget="winner_by_uf_map"]');
              return !!(map && map.hidden);
            }"""
        )
        assert _chart_js_count(page) == 6
        assert "1" in page.locator("#agenteComparaComparisonDashboardCustomizeBtn").inner_text()

        page.click("#agenteComparaComparisonDashboardCustomizeBtn")
        page.click("#agenteComparaComparisonDashboardShowAllBtn")
        page.wait_for_selector('[data-comparison-dashboard-widget="winner_by_uf_map"]:not([hidden])')
        assert _prefs(page)["hidden"] == []

        for key in (
            "coverage_by_carrier",
            "freight_without_complete_calculation",
            "comparability",
            "carrier_wins",
            "comparable_average_cost",
            "potential_savings",
            "winner_by_uf_map",
            "uf_savings_ranking",
            "uf_comparison_matrix",
        ):
            page.click(f'[data-comparison-dashboard-hide-widget="{key}"]')
        page.wait_for_selector("#agenteComparaComparisonDashboardAllHidden:not([hidden])")
        assert page.locator("#agenteComparaComparisonKpis:not([hidden])").count() == 1
        assert page.locator("#agenteComparaComparisonDashboardOpenDetailsBtn:not([hidden])").count() == 1
        assert _chart_js_count(page) == 0
        page.click("#agenteComparaComparisonDashboardRestoreChartsBtn")
        page.wait_for_selector("#agenteComparaComparisonDashboardAllHidden[hidden]", state="attached")
        page.wait_for_selector('[data-comparison-dashboard-widget="coverage_by_carrier"]:not([hidden])')
        assert _chart_js_count(page) == 6

        page.click("#agenteComparaComparisonDashboardOpenDetailsBtn")
        page.wait_for_selector("#agenteComparaTempTableModal:not([hidden])")
        modal = page.locator("#agenteComparaTempTableModal")
        assert modal.locator("canvas[id^='agenteComparaChart_']").count() == 0
        assert modal.locator("#agenteComparaResultsFilters").count() == 1
        page.click("#agenteComparaTempTableModalClose")

        isolation = page.evaluate(
            """({ key, cleide }) => ({
              ours: window.localStorage.getItem(key),
              cleide: window.localStorage.getItem(cleide)
            })""",
            {"key": STORAGE_KEY, "cleide": CLEIDE_KEY_PROBE},
        )
        assert json.loads(isolation["cleide"]) == {"transportadora": True}
        assert "coverage_by_carrier" not in (isolation["cleide"] or "")
        assert isolation["ours"] is not None
        assert "version" in json.loads(isolation["ours"])

        page.click('[data-comparison-dashboard-hide-widget="coverage_by_carrier"]')
        page.wait_for_function(
            """() => {
              const raw = window.localStorage.getItem('agente_compara_dashboard_preferences_v1');
              return !!(raw && raw.indexOf('coverage_by_carrier') !== -1);
            }"""
        )
        page.evaluate(
            """() => {
              const btn = document.getElementById('agenteComparaClearDocuments');
              if (btn) {
                btn.style.display = 'inline-block';
                btn.click();
              }
            }"""
        )
        page.wait_for_selector("#agenteComparaResetConfirmModal:not([hidden])", timeout=10000)
        page.click("#agenteComparaResetConfirmSubmit")
        page.wait_for_function(
            """() => {
              const root = document.getElementById('agenteComparaComparisonDashboard');
              return !!(root && root.hidden);
            }""",
            timeout=15000,
        )
        prefs_after_reset = _prefs(page)
        assert "coverage_by_carrier" in prefs_after_reset["hidden"]
        assert page.locator("#agenteComparaComparisonDashboardCustomize").get_attribute("hidden") is not None

        _inject(page, result, analytics)
        page.wait_for_selector("#agenteComparaComparisonDashboard:not([hidden])")
        page.wait_for_function(
            """() => {
              const card = document.querySelector('[data-comparison-dashboard-widget="coverage_by_carrier"]');
              return !!(card && card.hidden);
            }"""
        )
        assert _chart_js_count(page) == 5

        browser.close()


def test_browser_comparison_dashboard_personalization_tablet_mobile(live_base_url):
    result = _comparison_result_fixture()
    analytics = _comparison_analytics_fixture(result)
    state = _FlowState(promote_after_status_calls=2)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        page = browser.new_page(viewport={"width": 820, "height": 1180})
        _install_routes(page, state)
        page.goto(f"{live_base_url}/agente-compara", wait_until="domcontentloaded")
        page.evaluate("(key) => window.localStorage.removeItem(key)", STORAGE_KEY)
        _inject(page, result, analytics)
        page.wait_for_selector("#agenteComparaComparisonDashboard:not([hidden])")
        page.click('[data-comparison-dashboard-hide-widget="comparable_average_cost"]')
        page.wait_for_selector(
            '[data-comparison-dashboard-widget="comparable_average_cost"][hidden]',
            state="attached",
        )
        page.click("#agenteComparaComparisonDashboardCustomizeBtn")
        page.wait_for_selector("#agenteComparaComparisonDashboardCustomizeMenu:not([hidden])")
        menu_box = page.locator("#agenteComparaComparisonDashboardCustomizeMenu").bounding_box()
        assert menu_box is not None
        assert menu_box["x"] >= -1
        assert menu_box["x"] + menu_box["width"] <= 820 + 2
        page.click("#agenteComparaComparisonDashboardShowAllBtn")
        page.wait_for_selector('[data-comparison-dashboard-widget="comparable_average_cost"]:not([hidden])')
        page.close()

        page = browser.new_page(viewport={"width": 390, "height": 844})
        _install_routes(page, state)
        page.goto(f"{live_base_url}/agente-compara", wait_until="domcontentloaded")
        page.evaluate("(key) => window.localStorage.removeItem(key)", STORAGE_KEY)
        _inject(page, result, analytics)
        page.wait_for_selector("#agenteComparaComparisonDashboard:not([hidden])")
        page.click('[data-comparison-dashboard-hide-widget="uf_comparison_matrix"]')
        page.wait_for_selector(
            '[data-comparison-dashboard-widget="uf_comparison_matrix"][hidden]',
            state="attached",
        )
        page.click("#agenteComparaComparisonDashboardCustomizeBtn")
        page.wait_for_selector("#agenteComparaComparisonDashboardCustomizeMenu:not([hidden])")
        overflow = page.evaluate(
            """() => {
              const doc = document.documentElement;
              const menu = document.getElementById('agenteComparaComparisonDashboardCustomizeMenu');
              const rect = menu.getBoundingClientRect();
              return {
                overflowX: doc.scrollWidth > doc.clientWidth + 2,
                menuInside: rect.left >= -2 && rect.right <= window.innerWidth + 2,
                oneColumn: getComputedStyle(
                  document.querySelector('[data-comparison-dashboard-section-grid=\"reliability\"]')
                ).gridTemplateColumns.split(' ').length === 1
              };
            }"""
        )
        assert overflow["overflowX"] is False
        assert overflow["menuInside"] is True
        assert overflow["oneColumn"] is True
        page.keyboard.press("Escape")
        page.wait_for_selector("#agenteComparaComparisonDashboardCustomizeMenu[hidden]", state="attached")
        page.click('[data-comparison-dashboard-hide-widget="winner_by_uf_map"]')
        page.wait_for_selector(
            '[data-comparison-dashboard-widget="winner_by_uf_map"][hidden]',
            state="attached",
        )
        assert page.locator('[data-comparison-dashboard-widget="uf_savings_ranking"]:not([hidden])').count() == 1

        browser.close()
