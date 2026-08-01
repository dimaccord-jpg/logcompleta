"""Browser: dashboard executivo com cobertura parcial (sem base comparável)."""
from __future__ import annotations

import pytest

playwright = pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402

pytest_plugins = ["tests.test_agente_compara_freight_modal_browser"]

from tests.test_agente_compara_freight_modal_browser import (  # noqa: E402
    _FlowState,
    _comparison_analytics_fixture,
    _install_routes,
)


def _partial_coverage_result_fixture():
    return {
        "schema_version": 1,
        "comparison_id": "cmp-partial-browser",
        "execution_id": "exec-partial-browser",
        "table_count": 2,
        "row_count": 2,
        "tables": [
            {"table_id": "tbl-a", "slot_number": 1, "carrier_name": "Alpha", "temp_table_id": "tt_a"},
            {"table_id": "tbl-b", "slot_number": 2, "carrier_name": "Beta", "temp_table_id": "tt_b"},
        ],
        "comparative_rows": [
            {
                "row_index": 0,
                "document_number": "P1",
                "destination_city": "Campinas",
                "destination_uf": "SP",
                "weight": 10.0,
                "table_results": {
                    "tbl-a": {
                        "table_id": "tbl-a",
                        "carrier_name": "Alpha",
                        "slot_number": 1,
                        "calculated_freight": 50.0,
                        "status": "calculated",
                        "final_status": "calculated",
                        "error": None,
                    },
                    "tbl-b": {
                        "table_id": "tbl-b",
                        "carrier_name": "Beta",
                        "slot_number": 2,
                        "calculated_freight": None,
                        "status": "not_calculated",
                        "final_status": "not_calculated",
                        "error": {"code": "not_calculated"},
                    },
                },
            },
            {
                "row_index": 1,
                "document_number": "P2",
                "destination_city": "Niteroi",
                "destination_uf": "RJ",
                "weight": 8.0,
                "table_results": {
                    "tbl-a": {
                        "table_id": "tbl-a",
                        "carrier_name": "Alpha",
                        "slot_number": 1,
                        "calculated_freight": None,
                        "status": "incomplete",
                        "final_status": "incomplete",
                        "is_partial_value": True,
                        "error": {"code": "incomplete"},
                    },
                    "tbl-b": {
                        "table_id": "tbl-b",
                        "carrier_name": "Beta",
                        "slot_number": 2,
                        "calculated_freight": None,
                        "status": "not_calculated",
                        "final_status": "not_calculated",
                        "error": {"code": "not_calculated"},
                    },
                },
            },
        ],
    }


def test_browser_executive_dashboard_partial_coverage_no_false_leader(live_base_url):
    result = _partial_coverage_result_fixture()
    analytics = _comparison_analytics_fixture(result)
    assert analytics["comparability"]["fully_comparable_rows"] == 0
    assert analytics["executive_summary"]["lead_table_id"] is None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        state = _FlowState(promote_after_status_calls=2)
        _install_routes(page, state)
        page.goto(f"{live_base_url}/agente-compara", wait_until="domcontentloaded")
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
        page.wait_for_selector("#agenteComparaComparisonDashboard:not([hidden])")
        page.wait_for_selector("#agenteComparaChart_coverage")
        page.wait_for_selector("#agenteComparaChart_comparability")
        page.wait_for_selector("#agenteComparaResultsGeography")
        page.wait_for_selector("#agenteComparaGeoMapLegend")
        kpis = page.locator("#agenteComparaComparisonKpis").inner_text()
        assert "Resumo executivo" in kpis
        assert "Sem base decisiva" in kpis
        assert "Documentos comparáveis" in kpis
        charts = page.locator("#agenteComparaComparisonCharts").inner_text()
        assert "Fretes sem cálculo completo" in charts
        assert "Comparáveis" in charts
        geo = page.locator("#agenteComparaResultsGeography").inner_text()
        assert "Sem base comparável" in geo
        assert "Mapa do Brasil" in geo
        assert "Tabela geográfica" in geo
        assert page.locator("#agenteComparaChart_wins").count() == 1
        assert page.locator("#agenteComparaGeoMap").count() == 1
        page.wait_for_function(
            """() => {
              const map = document.querySelector('#agenteComparaGeoMap svg path[id]');
              return !!map;
            }"""
        )
        browser.close()
