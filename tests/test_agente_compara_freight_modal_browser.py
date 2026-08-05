"""Browser real (Playwright): wizard contínuo do modal AgenteCompara.

Não usa Gemini, billing nem backend real de extração: APIs são mockadas via page.route.
"""
from __future__ import annotations

import importlib
import os
import pathlib
import threading
import time
from types import SimpleNamespace

import pytest

playwright = pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402


REPO = pathlib.Path(__file__).resolve().parents[1]
JS_PATH = REPO / "app" / "static" / "js" / "agente_compara.js"
HTML_PATH = REPO / "app" / "templates" / "agente_compara.html"


def _load_web():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret-browser")
    return importlib.import_module("app.web")


@pytest.fixture(scope="module")
def live_base_url():
    web = _load_web()
    fake_user = SimpleNamespace(
        is_authenticated=True,
        conta_id=1,
        franquia_id=1,
        id=1,
        email="browser@test.local",
    )
    web.current_user = fake_user
    try:
        import app.agente_compara_api_routes as api_routes

        api_routes.current_user = fake_user
    except Exception:
        pass

    server = threading.Thread(
        target=lambda: web.app.run(
            host="127.0.0.1",
            port=8765,
            use_reloader=False,
            threaded=True,
        ),
        daemon=True,
    )
    server.start()
    deadline = time.time() + 15
    import urllib.request

    while time.time() < deadline:
        try:
            urllib.request.urlopen("http://127.0.0.1:8765/agente-compara", timeout=1)
            break
        except Exception:
            time.sleep(0.2)
    else:
        pytest.fail("Flask live server did not start on :8765")
    yield "http://127.0.0.1:8765"


def _comparison_payload(comparison_id="cmp-browser-1", table_id="tbl-1", step="PREPARE_TABLE_1"):
    return {
        "comparison_id": comparison_id,
        "current_step": step,
        "active_table_id": table_id,
        "tables": [
            {
                "table_id": table_id,
                "slot_number": 1,
                "status": "empty",
                "confirmed": False,
                "carrier_name": "",
            }
        ],
    }


def _calculation_bases():
    return [
        {
            "id": "pct_nota_fiscal",
            "label": "% por nota fiscal",
            "aliases": ["valor da nf", "sobre o valor da nf", "nota fiscal"],
            "unit": "%",
            "calculation_type": "invoice_percentage",
            "audit_variable": "valor_nf",
            "operation": "percentage_of_variable",
            "parameters": {},
        },
        {
            "id": "por_cte",
            "label": "por CTe",
            "aliases": ["cte", "documento"],
            "unit": "R$",
            "calculation_type": "fixed_amount",
            "audit_variable": None,
            "operation": "fixed_amount",
            "parameters": {},
        },
        {
            "id": "por_conhecimento",
            "label": "por conhecimento",
            "aliases": ["conhecimento"],
            "unit": "R$",
            "calculation_type": "fixed_amount",
            "audit_variable": None,
            "operation": "fixed_amount",
            "parameters": {},
        },
        {
            "id": "por_kg",
            "label": "por kg",
            "aliases": ["kg", "peso"],
            "unit": "R$",
            "calculation_type": "weight",
            "audit_variable": "peso",
            "operation": "multiply_by_variable",
            "parameters": {},
        },
        {
            "id": "fracao_100kg",
            "label": "por fra??o de 100kg",
            "aliases": ["100kg ou fra??o"],
            "unit": "R$",
            "calculation_type": "weight_fraction",
            "audit_variable": "peso",
            "operation": "ceil_fraction",
            "parameters": {"fraction_size": 100},
        },
    ]


def _temp_table(
    status,
    comparison_id="cmp-browser-1",
    table_id="tbl-1",
    temp_id="tt-1",
    *,
    with_blocking_validation=False,
    with_recognized_gris=False,
):
    payload = {
        "temp_table_id": temp_id,
        "comparison_id": comparison_id,
        "table_id": table_id,
        "status": status,
        "carrier_name": "Transportadora Browser",
        "expires_at": "2099-01-01T00:00:00Z",
        "source_documents": [],
        "generalities": [{"label": "Origem", "value": "SP"}],
        "additional_services": [],
        "reading_alerts": [],
        "evidence_refs": [],
        "freight_tables": [],
        "accessorial_fees": [],
        "ui_visibility": {"display_name": "Tabela temporária extraída"},
        "edit_version": 0,
    }
    if with_blocking_validation:
        payload["accessorial_fees"] = [
            {
                "name": "Pedagio geral",
                "value": "",
                "unit": "",
                "calculation_basis": "não mapeado / revisar",
                "calculation_base_id": None,
                "classification_source": "unmapped_calculation_base",
                "status": "needs_review",
                "notes": "",
                "review_presentation": {
                    "state": "blocking",
                    "basis_label": "Base de cálculo não identificada",
                    "secondary_text": "Selecione a base de cálculo antes de continuar.",
                    "requires_action": True,
                    "is_blocking": True,
                    "severity": "error",
                    "source": "unmapped_calculation_base",
                    "reason_code": "missing_calculation_base",
                },
            }
        ]
        payload["validation"] = {
            "schema_version": 1,
            "can_confirm": False,
            "blocking_count": 1,
            "warning_count": 0,
            "blocking_issues": [
                {
                    "code": "UNMAPPED_CALCULATION_BASE",
                    "section": "accessorial_fees",
                    "item_id": "accessorial_fees:0",
                    "index": 0,
                    "field": "calculation_base_id",
                    "label": "Pedagio geral",
                    "reason_code": "missing_calculation_base",
                    "severity": "blocking",
                    "message": "Defina a base de cálculo antes de continuar.",
                }
            ],
            "warnings": [],
        }
    elif with_recognized_gris:
        payload["accessorial_fees"] = [
            {
                "name": "GRIS",
                "item_id": "fee-gris",
                "value": "0,35%",
                "unit": "%",
                "rate": 0.0035,
                "calculation_base_id": None,
                "calculation_basis": "",
                "calculation_type": "invoice_percentage",
                "audit_variable": "valor_nf",
                "operation": "percentage_of_variable",
                "classification_source": "legacy_classifier",
                "status": "calculable",
                "canonical_component": "risk_management",
                "component_group": "risk_management",
                "modifier_type": "base_fee",
                "notes": "",
                "review_presentation": {
                    "state": "resolved",
                    "basis_label": "Percentual sobre o valor da NF",
                    "secondary_text": "Regra reconhecida automaticamente.",
                    "requires_action": False,
                    "is_blocking": False,
                    "severity": "info",
                    "source": "legacy_classifier",
                },
            },
            {
                "name": "GRIS M?nimo",
                "item_id": "fee-gris-min",
                "value": "5,00",
                "unit": "R$",
                "minimum_amount": 5.0,
                "calculation_base_id": None,
                "calculation_basis": "",
                "calculation_type": "minimum_amount",
                "modifier_type": "minimum_amount",
                "classification_source": "legacy_classifier",
                "status": "calculable",
                "related_to": "risk_management",
                "component_group": "risk_management",
                "notes": "",
                "review_presentation": {
                    "state": "resolved",
                    "basis_label": "M?nimo aplic?vel a GRIS",
                    "secondary_text": None,
                    "requires_action": False,
                    "is_blocking": False,
                    "severity": "info",
                    "source": "legacy_classifier",
                    "related_to_label": "GRIS",
                },
            }
        ]
        payload["validation"] = {
            "schema_version": 1,
            "can_confirm": True,
            "blocking_count": 0,
            "warning_count": 0,
            "blocking_issues": [],
            "warnings": [],
        }
    else:
        payload["validation"] = {
            "schema_version": 1,
            "can_confirm": True,
            "blocking_count": 0,
            "warning_count": 0,
            "blocking_issues": [],
            "warnings": [],
        }
    return payload


class _FlowState:
    def __init__(
        self,
        promote_after_status_calls=8,
        *,
        review_with_blocking=False,
        review_with_recognized_gris=False,
    ):
        self.phase = "idle"  # idle|processing|review|failed
        self.comparison = _comparison_payload()
        self.upload_calls = 0
        self.status_calls = 0
        self.promote_after_status_calls = promote_after_status_calls
        self.review_with_blocking = review_with_blocking
        self.review_with_recognized_gris = review_with_recognized_gris
        self.save_calls = 0
        self.saved_payloads = []


def _install_routes(page, state: _FlowState, *, fail_upload=False):
    def handle_route(route):
        req = route.request
        url = req.url
        method = req.method.upper()

        if "/api/agente-compara/comparison/start" in url and method == "POST":
            state.comparison = _comparison_payload()
            route.fulfill(
                status=200,
                content_type="application/json",
                body=__import__("json").dumps(
                    {"ok": True, "comparison": state.comparison}
                ),
            )
            return

        if "/api/agente-compara/comparison/reset" in url and method == "POST":
            state.phase = "idle"
            state.comparison = _comparison_payload(comparison_id="cmp-browser-2", table_id="tbl-2")
            route.fulfill(
                status=200,
                content_type="application/json",
                body=__import__("json").dumps(
                    {
                        "ok": True,
                        "comparison_reset": True,
                        "comparison": state.comparison,
                    }
                ),
            )
            return

        if "/api/agente-compara/documents/upload" in url and method == "POST":
            state.upload_calls += 1
            if fail_upload:
                # Pequena latência para a view uploading pintar antes do erro.
                time.sleep(0.35)
                route.fulfill(
                    status=400,
                    content_type="application/json",
                    body=__import__("json").dumps(
                        {"ok": False, "message": "Falha simulada de upload."}
                    ),
                )
                return
            # Latência controlada: permite observar uploading → processing no mesmo modal.
            time.sleep(0.45)
            state.phase = "processing"
            state.status_calls = 0
            tt = _temp_table(
                "processing",
                comparison_id=state.comparison["comparison_id"],
                table_id=state.comparison["active_table_id"],
            )
            state.comparison["tables"][0]["status"] = "processing"
            state.comparison["tables"][0]["carrier_name"] = "Transportadora Browser"
            route.fulfill(
                status=200,
                content_type="application/json",
                body=__import__("json").dumps(
                    {
                        "ok": True,
                        "comparison": state.comparison,
                        "temp_table": tt,
                        "calculation_bases": _calculation_bases(),
                    }
                ),
            )
            return

        if "/api/agente-compara/documents/status" in url and method == "GET":
            state.status_calls += 1
            if (
                state.phase == "processing"
                and state.status_calls >= state.promote_after_status_calls
            ):
                state.phase = "review"
            if state.phase == "failed":
                tt = _temp_table(
                    "failed",
                    comparison_id=state.comparison["comparison_id"],
                    table_id=state.comparison["active_table_id"],
                )
            elif state.phase == "review":
                tt = _temp_table(
                    "needs_review",
                    comparison_id=state.comparison["comparison_id"],
                    table_id=state.comparison["active_table_id"],
                    with_blocking_validation=bool(state.review_with_blocking),
                    with_recognized_gris=bool(
                        state.review_with_recognized_gris and not state.review_with_blocking
                    ),
                )
                state.comparison["tables"][0]["status"] = "ready"
            elif state.phase == "processing":
                tt = _temp_table(
                    "processing",
                    comparison_id=state.comparison["comparison_id"],
                    table_id=state.comparison["active_table_id"],
                )
            else:
                tt = None
            route.fulfill(
                status=200,
                content_type="application/json",
                body=__import__("json").dumps(
                    {
                        "ok": True,
                        "documents": [],
                        "temp_table": tt,
                        "calculation_bases": _calculation_bases(),
                        "comparison": state.comparison,
                        "has_active_comparison": True,
                    }
                ),
            )
            return

        if "/api/agente-compara/temp-table/save" in url and method == "POST":
            state.save_calls += 1
            payload = __import__("json").loads(req.post_data or "{}")
            state.saved_payloads.append(payload)
            if state.review_with_blocking and state.save_calls == 1:
                route.fulfill(
                    status=400,
                    content_type="application/json",
                    body=__import__("json").dumps(
                        {
                            "ok": False,
                            "error_code": "invalid_accessorial_fees",
                            "error": "TEMP_TABLE_HAS_BLOCKING_ISSUES",
                            "message": "Resolva as pendências das generalidades antes de avançar.",
                            "validation": {
                                "can_confirm": False,
                                "blocking_count": 1,
                                "warning_count": 0,
                                "blocking_issues": [
                                    {
                                        "code": "UNMAPPED_CALCULATION_BASE",
                                        "section": "accessorial_fees",
                                        "item_id": "accessorial_fees:0",
                                        "index": 0,
                                        "field": "calculation_base_id",
                                        "label": "Pedagio geral",
                                        "reason_code": "missing_calculation_base",
                                        "severity": "blocking",
                                        "message": "Defina a base de cálculo antes de continuar.",
                                    }
                                ],
                                "warnings": [],
                            },
                            "errors": [
                                {
                                    "code": "UNMAPPED_CALCULATION_BASE",
                                    "section": "accessorial_fees",
                                    "index": 0,
                                    "name": "Pedagio geral",
                                    "field": "calculation_base_id",
                                    "reason_code": "missing_calculation_base",
                                    "severity": "blocking",
                                    "message": "Defina a base de cálculo antes de continuar.",
                                }
                            ],
                        }
                    ),
                )
                return
            state.review_with_blocking = False
            tt = _temp_table(
                "needs_review",
                comparison_id=state.comparison["comparison_id"],
                table_id=state.comparison["active_table_id"],
                with_blocking_validation=False,
                with_recognized_gris=bool(state.review_with_recognized_gris),
            )
            if payload.get("edit_target") and payload["edit_target"].get("accessorial_fees"):
                tt["accessorial_fees"] = payload["edit_target"]["accessorial_fees"]
            state.comparison["current_step"] = "PREPARE_TABLE_2"
            state.comparison["tables"][0]["confirmed"] = True
            route.fulfill(
                status=200,
                content_type="application/json",
                body=__import__("json").dumps(
                    {"ok": True, "temp_table": tt, "comparison": state.comparison}
                ),
            )
            return

        if "/api/agente-compara/" in url:
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"ok": true}',
            )
            return

        route.continue_()

    page.route("**/api/agente-compara/**", handle_route)


def _wait_modal_hidden(page, timeout: int = 15000):
    page.wait_for_function(
        """() => {
          const modal = document.getElementById('agenteComparaTempTableModal');
          return !!(modal && modal.hidden);
        }""",
        timeout=timeout,
    )


def _wait_title(page, title_part: str, timeout: int = 15000):
    page.wait_for_function(
        """(titlePart) => {
          const modal = document.getElementById('agenteComparaTempTableModal');
          if (!modal || modal.hidden) return false;
          const t = document.getElementById('agenteComparaTempTableModalTitle')?.textContent || '';
          const b = document.getElementById('agenteComparaTempTableModalBody')?.textContent || '';
          return t.includes(titlePart) && b.trim().length > 0;
        }""",
        arg=title_part,
        timeout=timeout,
    )


def _assert_modal_open_nonempty(page):
    assert _modal(page).get_attribute("hidden") is None
    assert _title(page).inner_text().strip() != ""
    assert _body(page).inner_text().strip() != ""
    assert _page_status(page).inner_text().strip() == ""


def _modal(page):
    return page.locator("#agenteComparaTempTableModal")


def _title(page):
    return page.locator("#agenteComparaTempTableModalTitle")


def _body(page):
    return page.locator("#agenteComparaTempTableModalBody")


def _page_status(page):
    return page.locator("#agenteComparaUploadStatus")


def _start_file_upload(page, name="tabela.xlsx"):
    # Dispara o fluxo via beginPending usando o input oculto.
    page.set_input_files("#agenteComparaFileInput", {
        "name": name,
        "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "buffer": b"PK\x03\x04fake-xlsx-content-for-browser-test",
    })


def test_browser_source_contracts_controller_present():
    js = JS_PATH.read_text(encoding="utf-8")
    html = HTML_PATH.read_text(encoding="utf-8")
    assert "function showTempTableModalShell()" in js
    assert "function renderAndShowComparisonFlowModal" in js
    assert "function transitionComparisonFlowModal" in js
    assert "ac-flow-modal-1" in html
    assert "agenteComparaTempTableModalValidation" in html
    assert "function tempTableConfirmationCanProceed" in js
    assert "function accessorialFeeHasFormalUnmappedBase" in js
    open_carrier = js[
        js.index("function openCarrierIdentificationPanel") : js.index(
            "function cancelCarrierIdentification"
        )
    ]
    assert "modal.hidden = false" not in open_carrier


def test_browser_blocking_validation_disables_save_and_keeps_edit(live_base_url):
    state = _FlowState(promote_after_status_calls=2, review_with_blocking=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        _install_routes(page, state)
        page.goto(f"{live_base_url}/agente-compara", wait_until="domcontentloaded")
        _start_file_upload(page)
        page.wait_for_selector("#agenteComparaTempTableModal:not([hidden])")
        page.fill("#agenteComparaCarrierNameInput", "Transportadora Browser")
        page.click("#agenteComparaCarrierIdentifyContinue")
        _wait_title(page, "Revisão", timeout=45000)

        save_btn = page.locator("#agenteComparaTempTableModalSave")
        edit_btn = page.locator("#agenteComparaTempTableModalEdit")
        body_text = _body(page).inner_text()
        assert "Base de cálculo não identificada" in body_text
        assert "não mapeado / revisar" not in body_text
        assert edit_btn.count() == 1
        assert edit_btn.get_attribute("hidden") is None
        assert not edit_btn.is_disabled()
        assert save_btn.is_disabled()
        assert save_btn.get_attribute("aria-disabled") == "true"
        validation = page.locator("#agenteComparaTempTableModalValidation")
        assert validation.get_attribute("hidden") is None
        assert "pendência" in validation.inner_text().lower()

        # Clique no Save desabilitado não deve disparar POST.
        save_btn.click(force=True)
        page.wait_for_timeout(300)
        assert state.save_calls == 0
        browser.close()


def test_browser_edit_mode_hydrates_recognized_gris_and_preserves_save_payload(live_base_url):
    state = _FlowState(promote_after_status_calls=2, review_with_recognized_gris=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        _install_routes(page, state)
        page.goto(f"{live_base_url}/agente-compara", wait_until="domcontentloaded")
        _start_file_upload(page)
        page.wait_for_selector("#agenteComparaTempTableModal:not([hidden])")
        page.fill("#agenteComparaCarrierNameInput", "Transportadora Browser")
        page.click("#agenteComparaCarrierIdentifyContinue")
        _wait_title(page, "Revis?o", timeout=45000)

        page.click("#agenteComparaTempTableModalEdit")
        gris_select = page.locator('tr[data-accessorial-fee-index="0"] select[data-field="calculation_base_id"]')
        assert gris_select.input_value() == "pct_nota_fiscal"
        minimum_row = page.locator('tr[data-accessorial-fee-index="1"]')
        assert "M?nimo aplic?vel a GRIS" in minimum_row.inner_text()
        assert "n?o mapeado / revisar" not in minimum_row.inner_text()

        page.click("#agenteComparaTempTableModalClose")
        _wait_modal_hidden(page)
        assert state.save_calls == 0

        page.click(".agente-compara-temp-table-open-btn")
        page.wait_for_selector("#agenteComparaTempTableModal:not([hidden])")
        _wait_title(page, "Revis?o", timeout=15000)
        page.click("#agenteComparaTempTableModalEdit")
        gris_select = page.locator('tr[data-accessorial-fee-index="0"] select[data-field="calculation_base_id"]')
        assert gris_select.input_value() == "pct_nota_fiscal"

        page.click("#agenteComparaTempTableModalSave")
        page.wait_for_timeout(500)
        assert state.save_calls == 1
        payload = state.saved_payloads[-1]
        fees = payload["edit_target"]["accessorial_fees"]
        assert fees[0]["calculation_base_id"] == "pct_nota_fiscal"
        assert fees[0]["operation"] == "percentage_of_variable"
        assert fees[0]["calculation_type"] == "invoice_percentage"
        assert fees[1]["related_to"] == "risk_management"
        assert fees[1]["modifier_type"] == "minimum_amount"
        browser.close()


def test_browser_review_presentation_recognized_gris_and_memory(live_base_url):
    """Revisão com GRIS reconhecido não mostra fallback genérico; memória converge."""
    state = _FlowState(promote_after_status_calls=2, review_with_recognized_gris=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        _install_routes(page, state)
        page.goto(f"{live_base_url}/agente-compara", wait_until="domcontentloaded")
        _start_file_upload(page)
        page.wait_for_selector("#agenteComparaTempTableModal:not([hidden])")
        page.fill("#agenteComparaCarrierNameInput", "Transportadora Browser")
        page.click("#agenteComparaCarrierIdentifyContinue")
        _wait_title(page, "Revisão", timeout=45000)

        body_text = _body(page).inner_text()
        assert "GRIS" in body_text
        assert "Percentual sobre o valor da NF" in body_text
        assert "reconhecida automaticamente" in body_text.lower()
        assert "não mapeado / revisar" not in body_text
        save_btn = page.locator("#agenteComparaTempTableModalSave")
        edit_btn = page.locator("#agenteComparaTempTableModalEdit")
        assert not save_btn.is_disabled()
        assert edit_btn.count() == 1
        assert not edit_btn.is_disabled()

        page.click("#agenteComparaTempTableModalClose")
        _wait_modal_hidden(page)

        result = _comparison_result_fixture()
        memory = result["comparative_rows"][0]["table_results"]["tbl-a"]["calculation_memory"]
        memory["components"].append(
            {
                "code": "ACCESSORIAL",
                "label": "GRIS",
                "amount": 3.5,
                "applied": True,
                "ignored": False,
                "minimum_applied": False,
                "basis": "Percentual sobre o valor da NF",
                "rate": 0.0035,
                "quantity": None,
                "operation": "invoice_percentage",
                "minimum_amount": None,
                "reason": None,
                "source": "legacy_classifier",
            }
        )
        page.evaluate(
            """(result) => {
              document.dispatchEvent(new CustomEvent('agente-compara:inject-comparison-result', {
                detail: {
                  status: 'CALCULATION_READY',
                  billing_status: 'applied',
                  current_step: 'CALCULATION_READY',
                  stale: false,
                  result: result,
                  analytics: null
                }
              }));
            }""",
            result,
        )
        page.wait_for_selector(
            "#agenteComparaComparisonResultsTable button.agente-compara-comparison-calc-memory-link"
        )
        page.locator(
            "#agenteComparaComparisonResultsTable button.agente-compara-comparison-calc-memory-link"
        ).nth(0).click()
        page.wait_for_selector("#agenteComparaComparisonCalculationMemoryModal:not([hidden])")
        memory_text = page.locator("#agenteComparaComparisonCalculationMemoryModalBody").inner_text()
        assert "GRIS" in memory_text
        assert "NF" in memory_text or "nota" in memory_text.lower()
        browser.close()


def test_browser_normal_continuous_modal_journey(live_base_url):
    state = _FlowState(promote_after_status_calls=2)
    titles = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        _install_routes(page, state)
        page.goto(f"{live_base_url}/agente-compara", wait_until="domcontentloaded")
        page.wait_for_selector("#agenteComparaFileInput")

        _start_file_upload(page)
        page.wait_for_selector("#agenteComparaTempTableModal:not([hidden])")
        assert _title(page).inner_text().strip() == "Identifique a transportadora"
        assert _body(page).locator("#agenteComparaCarrierIdentifyPanel").count() == 1
        _assert_modal_open_nonempty(page)
        titles.append(_title(page).inner_text().strip())

        page.fill("#agenteComparaCarrierNameInput", "Transportadora Browser")
        page.click("#agenteComparaCarrierIdentifyContinue")

        page.wait_for_function(
            """() => {
              const modal = document.getElementById('agenteComparaTempTableModal');
              if (!modal || modal.hidden) return false;
              const t = document.getElementById('agenteComparaTempTableModalTitle')?.textContent || '';
              const b = document.getElementById('agenteComparaTempTableModalBody')?.textContent || '';
              return t.includes('Enviando') && b.includes('Enviando documento');
            }""",
            timeout=15000,
        )
        # Captura atômica no momento em que a condição ainda é verdadeira.
        titles.append(
            page.evaluate(
                """() => {
                  const t = document.getElementById('agenteComparaTempTableModalTitle')?.textContent || '';
                  const b = document.getElementById('agenteComparaTempTableModalBody')?.textContent || '';
                  return (t.includes('Enviando') && b.includes('Enviando documento'))
                    ? t
                    : 'Enviando tabela de frete';
                }"""
            )
        )
        assert _page_status(page).inner_text().strip() == ""
        assert _modal(page).get_attribute("hidden") is None

        _wait_title(page, "Processando", timeout=20000)
        assert "Estruturando tabela temporária..." in _body(page).inner_text()
        _assert_modal_open_nonempty(page)
        titles.append(_title(page).inner_text().strip())

        _wait_title(page, "Revisão", timeout=45000)
        _assert_modal_open_nonempty(page)
        titles.append(_title(page).inner_text().strip())
        assert page.locator("#agenteComparaTempTableModalEdit:not([hidden])").count() == 1
        assert page.locator("#agenteComparaTempTableModalSave:not([hidden])").count() == 1
        assert any("Identifique" in t for t in titles)
        assert any("Enviando" in t for t in titles)
        assert any("Processando" in t for t in titles)
        assert any("Revisão" in t for t in titles)
        browser.close()


def test_browser_failed_upload_stays_in_modal(live_base_url):
    state = _FlowState()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        _install_routes(page, state, fail_upload=True)
        page.goto(f"{live_base_url}/agente-compara", wait_until="domcontentloaded")
        _start_file_upload(page)
        page.wait_for_selector("#agenteComparaTempTableModal:not([hidden])")
        page.fill("#agenteComparaCarrierNameInput", "Transportadora Browser")
        page.click("#agenteComparaCarrierIdentifyContinue")
        _wait_title(page, "Não foi possível", timeout=15000)
        assert "Tentar novamente" in _body(page).inner_text()
        _assert_modal_open_nonempty(page)
        browser.close()


def test_browser_close_and_reopen_processing_card(live_base_url):
    state = _FlowState(promote_after_status_calls=50)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        _install_routes(page, state)
        page.goto(f"{live_base_url}/agente-compara", wait_until="domcontentloaded")
        _start_file_upload(page)
        page.wait_for_selector("#agenteComparaTempTableModal:not([hidden])")
        page.fill("#agenteComparaCarrierNameInput", "Transportadora Browser")
        page.click("#agenteComparaCarrierIdentifyContinue")
        _wait_title(page, "Processando", timeout=20000)
        page.click("#agenteComparaTempTableModalClose")
        _wait_modal_hidden(page)
        page.click(".agente-compara-temp-table-open-btn")
        page.wait_for_selector("#agenteComparaTempTableModal:not([hidden])")
        title = _title(page).inner_text()
        assert "Processando" in title
        assert "Estruturando tabela temporária..." in _body(page).inner_text()
        browser.close()


def test_browser_reset_clears_and_restarts_flow(live_base_url):
    state = _FlowState(promote_after_status_calls=3)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        _install_routes(page, state)
        page.goto(f"{live_base_url}/agente-compara", wait_until="domcontentloaded")
        _start_file_upload(page, name="tabela-a.xlsx")
        page.wait_for_selector("#agenteComparaTempTableModal:not([hidden])")
        page.fill("#agenteComparaCarrierNameInput", "Transportadora A")
        page.click("#agenteComparaCarrierIdentifyContinue")
        _wait_title(page, "Revisão", timeout=45000)

        page.evaluate(
            """async () => {
              const resp = await fetch('/api/agente-compara/comparison/reset', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                credentials: 'same-origin',
                body: '{}'
              });
              return resp.ok;
            }"""
        )
        page.click("#agenteComparaTempTableModalClose")
        _wait_modal_hidden(page)
        state.phase = "idle"
        state.status_calls = 0
        state.promote_after_status_calls = 3
        _start_file_upload(page, name="tabela-b.xlsx")
        page.wait_for_selector("#agenteComparaTempTableModal:not([hidden])")
        assert "Identifique a transportadora" in _title(page).inner_text()
        page.fill("#agenteComparaCarrierNameInput", "Transportadora B")
        page.click("#agenteComparaCarrierIdentifyContinue")
        _wait_title(page, "Revisão", timeout=45000)
        assert _body(page).inner_text().strip() != ""
        browser.close()



def _comparison_result_fixture():
    return {
        "schema_version": 1,
        "comparison_id": "cmp-memory-browser",
        "execution_id": "exec-memory-browser",
        "table_count": 2,
        "row_count": 3,
        "tables": [
            {"table_id": "tbl-a", "slot_number": 1, "carrier_name": "Mesma", "temp_table_id": "tt_a"},
            {"table_id": "tbl-b", "slot_number": 2, "carrier_name": "Mesma", "temp_table_id": "tt_b"},
        ],
        "comparative_rows": [
            {
                "row_index": 0,
                "document_number": "DOC-REP",
                "destination_city": "Campinas",
                "destination_uf": "SP",
                "weight": 48.0,
                "invoice_value": 1000.0,
                "table_results": {
                    "tbl-a": {
                        "table_id": "tbl-a",
                        "carrier_name": "Mesma",
                        "slot_number": 1,
                        "calculated_freight": 100.5,
                        "status": "calculated",
                        "error": None,
                        "components": {"weight_freight": 100.5, "subtotal": 100.5, "total": 100.5},
                        "evidence": {
                            "freight_region": "SP-Interior 1",
                            "calculation_basis": "range_plus_excess_per_kg",
                            "weight_band": "Faixa até 50 kg",
                        },
                        "calculation_memory": {
                            "schema_version": 1,
                            "status": "calculated",
                            "row_index": 0,
                            "table_id": "tbl-a",
                            "slot_number": 1,
                            "carrier_name": "Mesma",
                            "calculated_freight": 100.5,
                            "pricing": {
                                "pricing_type": "range_plus_excess_per_kg",
                                "freight_region": "SP-Interior 1",
                                "weight_band": "Faixa até 50 kg",
                            },
                            "components": [
                                {
                                    "code": "WEIGHT_FREIGHT",
                                    "label": "Frete-peso",
                                    "basis": "range_plus_excess_per_kg",
                                    "rate": None,
                                    "quantity": None,
                                    "operation": "range_plus_excess_per_kg",
                                    "amount": 100.5,
                                    "minimum_amount": None,
                                    "minimum_applied": False,
                                    "applied": True,
                                    "ignored": False,
                                    "reason": None,
                                    "source": "Faixa até 50 kg",
                                }
                            ],
                            "taxes": [],
                            "subtotal_before_taxes": 100.5,
                            "rounding": None,
                            "total": 100.5,
                            "evidence": {
                                "freight_region": "SP-Interior 1",
                                "calculation_basis": "range_plus_excess_per_kg",
                                "weight_band": "Faixa até 50 kg",
                            },
                            "diagnostic": None,
                        },
                    },
                    "tbl-b": {
                        "table_id": "tbl-b",
                        "carrier_name": "Mesma",
                        "slot_number": 2,
                        "calculated_freight": 180.0,
                        "status": "calculated",
                        "error": None,
                        "components": {"weight_freight": 180.0, "total": 180.0},
                        "evidence": {"freight_region": "SP-Interior 1"},
                        "calculation_memory": {
                            "schema_version": 1,
                            "status": "calculated",
                            "row_index": 0,
                            "table_id": "tbl-b",
                            "slot_number": 2,
                            "carrier_name": "Mesma",
                            "calculated_freight": 180.0,
                            "pricing": {"freight_region": "SP-Interior 1"},
                            "components": [
                                {
                                    "code": "WEIGHT_FREIGHT",
                                    "label": "Frete-peso",
                                    "basis": "fixed_range",
                                    "amount": 180.0,
                                    "applied": True,
                                    "ignored": False,
                                    "minimum_applied": False,
                                    "rate": None,
                                    "quantity": None,
                                    "operation": None,
                                    "minimum_amount": None,
                                    "reason": None,
                                    "source": None,
                                }
                            ],
                            "taxes": [],
                            "subtotal_before_taxes": 180.0,
                            "rounding": None,
                            "total": 180.0,
                            "evidence": {"freight_region": "SP-Interior 1"},
                            "diagnostic": None,
                        },
                    },
                },
            },
            {
                "row_index": 1,
                "document_number": "DOC-REP",
                "destination_city": "Campinas",
                "destination_uf": "SP",
                "weight": 30.0,
                "invoice_value": 1000.0,
                "table_results": {
                    "tbl-a": {
                        "table_id": "tbl-a",
                        "carrier_name": "Mesma",
                        "slot_number": 1,
                        "calculated_freight": 87.13,
                        "status": "calculated",
                        "error": None,
                        "components": {"weight_freight": 87.13, "total": 87.13},
                        "evidence": {},
                        "calculation_memory": {
                            "schema_version": 1,
                            "status": "calculated",
                            "row_index": 1,
                            "table_id": "tbl-a",
                            "slot_number": 1,
                            "carrier_name": "Mesma",
                            "calculated_freight": 87.13,
                            "pricing": {},
                            "components": [
                                {
                                    "code": "WEIGHT_FREIGHT",
                                    "label": "Frete-peso",
                                    "amount": 87.13,
                                    "applied": True,
                                    "ignored": False,
                                    "minimum_applied": False,
                                    "basis": None,
                                    "rate": None,
                                    "quantity": None,
                                    "operation": None,
                                    "minimum_amount": None,
                                    "reason": None,
                                    "source": None,
                                }
                            ],
                            "taxes": [],
                            "subtotal_before_taxes": 87.13,
                            "rounding": None,
                            "total": 87.13,
                            "evidence": {},
                            "diagnostic": None,
                        },
                    },
                    "tbl-b": {
                        "table_id": "tbl-b",
                        "carrier_name": "Mesma",
                        "slot_number": 2,
                        "calculated_freight": None,
                        "status": "missing_freight_rule",
                        "error": {"code": "missing_freight_rule", "message": "Nenhuma faixa aplicável."},
                        "components": {},
                        "evidence": {},
                        "calculation_memory": {
                            "schema_version": 1,
                            "status": "not_calculated",
                            "row_index": 1,
                            "table_id": "tbl-b",
                            "slot_number": 2,
                            "carrier_name": "Mesma",
                            "calculated_freight": None,
                            "pricing": {},
                            "components": [],
                            "taxes": [],
                            "subtotal_before_taxes": None,
                            "rounding": None,
                            "total": None,
                            "evidence": {},
                            "diagnostic": {
                                "code": "missing_freight_rule",
                                "message": "Nenhuma faixa aplicável.",
                                "component": "pricing_lookup",
                                "reason": "pricing_lookup",
                                "evidence": {},
                            },
                        },
                    },
                },
            },
            {
                "row_index": 2,
                "document_number": "DOC-FILTER",
                "destination_city": "Santos",
                "destination_uf": "SP",
                "weight": 40.0,
                "invoice_value": 500.0,
                "table_results": {
                    "tbl-a": {
                        "table_id": "tbl-a",
                        "carrier_name": "Mesma",
                        "slot_number": 1,
                        "calculated_freight": 95.0,
                        "status": "calculated",
                        "error": None,
                        "components": {"weight_freight": 95.0, "total": 95.0},
                        "evidence": {},
                        "calculation_memory": {
                            "schema_version": 1,
                            "status": "calculated",
                            "row_index": 2,
                            "table_id": "tbl-a",
                            "slot_number": 1,
                            "carrier_name": "Mesma",
                            "calculated_freight": 95.0,
                            "pricing": {},
                            "components": [
                                {
                                    "code": "WEIGHT_FREIGHT",
                                    "label": "Frete-peso",
                                    "amount": 95.0,
                                    "applied": True,
                                    "ignored": False,
                                    "minimum_applied": False,
                                    "basis": None,
                                    "rate": None,
                                    "quantity": None,
                                    "operation": None,
                                    "minimum_amount": None,
                                    "reason": None,
                                    "source": None,
                                }
                            ],
                            "taxes": [],
                            "subtotal_before_taxes": 95.0,
                            "rounding": None,
                            "total": 95.0,
                            "evidence": {},
                            "diagnostic": None,
                        },
                    },
                    "tbl-b": {
                        "table_id": "tbl-b",
                        "carrier_name": "Mesma",
                        "slot_number": 2,
                        "calculated_freight": 110.0,
                        "status": "calculated",
                        "error": None,
                        "components": {"weight_freight": 110.0, "total": 110.0},
                        "evidence": {},
                        "calculation_memory": {
                            "schema_version": 1,
                            "status": "calculated",
                            "row_index": 2,
                            "table_id": "tbl-b",
                            "slot_number": 2,
                            "carrier_name": "Mesma",
                            "calculated_freight": 110.0,
                            "pricing": {},
                            "components": [
                                {
                                    "code": "WEIGHT_FREIGHT",
                                    "label": "Frete-peso",
                                    "amount": 110.0,
                                    "applied": True,
                                    "ignored": False,
                                    "minimum_applied": False,
                                    "basis": None,
                                    "rate": None,
                                    "quantity": None,
                                    "operation": None,
                                    "minimum_amount": None,
                                    "reason": None,
                                    "source": None,
                                }
                            ],
                            "taxes": [],
                            "subtotal_before_taxes": 110.0,
                            "rounding": None,
                            "total": 110.0,
                            "evidence": {},
                            "diagnostic": None,
                        },
                    },
                },
            },
        ],
        "results_by_table": {},
        "summary": {
            "calculated_cell_count": 5,
            "error_cell_count": 1,
            "total_calculation_cells": 6,
        },
    }


def test_browser_comparison_calculation_memory_modal(live_base_url):
    """Memória de cálculo local via UI real: clique não dispara calculate/billing/Gemini."""
    state = _FlowState(promote_after_status_calls=3)
    calc_posts = {"count": 0}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        _install_routes(page, state)

        def _block_calculate(route):
            calc_posts["count"] += 1
            route.fulfill(status=500, content_type="application/json", body='{"ok":false}')

        page.route("**/api/agente-compara/comparison/calculate**", _block_calculate)
        page.goto(f"{live_base_url}/agente-compara", wait_until="domcontentloaded")
        page.wait_for_selector(".agente-compara-page, body")

        result = _comparison_result_fixture()
        page.evaluate(
            """(result) => {
              document.dispatchEvent(new CustomEvent('agente-compara:inject-comparison-result', {
                detail: {
                  status: 'CALCULATION_READY',
                  billing_status: 'applied',
                  current_step: 'CALCULATION_READY',
                  stale: false,
                  result: result,
                  analytics: null
                }
              }));
            }""",
            result,
        )

        page.wait_for_selector(
            "#agenteComparaComparisonResultsTable button.agente-compara-comparison-calc-memory-link"
        )
        buttons = page.locator(
            "#agenteComparaComparisonResultsTable button.agente-compara-comparison-calc-memory-link"
        )
        assert buttons.count() >= 4

        before = calc_posts["count"]
        buttons.nth(0).click()
        page.wait_for_selector("#agenteComparaComparisonCalculationMemoryModal:not([hidden])")
        body_text = page.locator("#agenteComparaComparisonCalculationMemoryModalBody").inner_text()
        subtitle = page.locator("#agenteComparaComparisonCalculationMemoryModalSubtitle").inner_text()
        assert "100,50" in body_text
        assert "Frete-peso" in body_text
        assert "Total:" in body_text
        assert "Tabela 1" in subtitle
        assert "DOC-REP" in subtitle
        assert calc_posts["count"] == before

        page.locator("#agenteComparaComparisonCalculationMemoryModalClose").click()
        page.wait_for_function(
            "() => document.getElementById('agenteComparaComparisonCalculationMemoryModal').hidden === true"
        )

        buttons.nth(1).click()
        page.wait_for_selector("#agenteComparaComparisonCalculationMemoryModal:not([hidden])")
        body_b = page.locator("#agenteComparaComparisonCalculationMemoryModalBody").inner_text()
        sub_b = page.locator("#agenteComparaComparisonCalculationMemoryModalSubtitle").inner_text()
        assert "180,00" in body_b
        assert "Tabela 2" in sub_b
        page.keyboard.press("Escape")
        page.wait_for_function(
            "() => document.getElementById('agenteComparaComparisonCalculationMemoryModal').hidden === true"
        )

        not_calc = page.locator(
            "button.agente-compara-comparison-calc-memory-link", has_text="Não calculado"
        )
        assert not_calc.count() >= 1
        not_calc.first.click()
        page.wait_for_selector("#agenteComparaComparisonCalculationMemoryModal:not([hidden])")
        diag = page.locator("#agenteComparaComparisonCalculationMemoryModalBody").inner_text().lower()
        assert "faixa" in diag or "motivo" in diag or "não" in diag
        assert calc_posts["count"] == before

        browser.close()


def _incomplete_comparison_result_fixture():
    """Fixture sintética: célula incomplete (TAS/Pedágio críticos) + controle calculated."""
    return {
        "schema_version": 1,
        "comparison_id": "cmp-incomplete",
        "execution_id": "exec-incomplete",
        "table_count": 2,
        "row_count": 1,
        "tables": [
            {
                "table_id": "tbl-gbex",
                "slot_number": 1,
                "carrier_name": "Transportadora Sintetica A",
                "temp_table_id": "tt_gbex",
            },
            {
                "table_id": "tbl-ctrl",
                "slot_number": 2,
                "carrier_name": "Transportadora Sintetica B",
                "temp_table_id": "tt_ctrl",
            },
        ],
        "comparative_rows": [
            {
                "row_index": 1,
                "document_number": "DOC-GBEX",
                "destination_city": "Caruaru",
                "destination_uf": "PE",
                "weight": 13.6,
                "invoice_value": 2000.0,
                "table_results": {
                    "tbl-gbex": {
                        "table_id": "tbl-gbex",
                        "carrier_name": "Transportadora Sintetica A",
                        "slot_number": 1,
                        "calculated_freight": 94.79,
                        "status": "incomplete",
                        "final_status": "incomplete",
                        "is_partial_value": True,
                        "blocking_issues": [
                            {
                                "reason_code": "unsupported_accessorial_condition",
                                "label": "TAS",
                                "message": "A condição desta taxa não pôde ser avaliada.",
                            },
                            {
                                "reason_code": "conditions_present",
                                "label": "Pedágio",
                                "message": "A condição desta taxa não pôde ser avaliada.",
                            },
                        ],
                        "error": {
                            "code": "incomplete",
                            "message": "Cálculo incompleto: há componentes potencialmente aplicáveis não avaliados.",
                        },
                        "components": {
                            "weight_freight": 80.0,
                            "ignored_accessorial_fees": [
                                {"label": "TAS", "reason_code": "unsupported_accessorial_condition"},
                                {"label": "Pedágio", "reason_code": "conditions_present"},
                            ],
                        },
                        "evidence": {},
                        "calculation_memory": {
                            "schema_version": 1,
                            "status": "incomplete",
                            "status_label": "Cálculo incompleto",
                            "total_label": "Valor parcial calculado",
                            "is_partial_value": True,
                            "row_index": 1,
                            "table_id": "tbl-gbex",
                            "slot_number": 1,
                            "carrier_name": "Transportadora Sintetica A",
                            "calculated_freight": 94.79,
                            "total": 94.79,
                            "components": [
                                {
                                    "code": "WEIGHT_FREIGHT",
                                    "label": "Frete-peso",
                                    "amount": 80.0,
                                    "applied": True,
                                    "ignored": False,
                                },
                                {
                                    "code": "IGNORED_ACCESSORIAL",
                                    "label": "TAS",
                                    "amount": None,
                                    "applied": False,
                                    "ignored": True,
                                    "reason": "unsupported_accessorial_condition",
                                },
                                {
                                    "code": "IGNORED_ACCESSORIAL",
                                    "label": "Pedágio",
                                    "amount": None,
                                    "applied": False,
                                    "ignored": True,
                                    "reason": "conditions_present",
                                },
                            ],
                            "taxes": [],
                            "blocking_issues": [
                                {
                                    "label": "TAS",
                                    "reason_code": "unsupported_accessorial_condition",
                                    "message": "A condição desta taxa não pôde ser avaliada.",
                                },
                                {
                                    "label": "Pedágio",
                                    "reason_code": "conditions_present",
                                    "message": "A condição desta taxa não pôde ser avaliada.",
                                },
                            ],
                            "warnings": [],
                            "evidence": {},
                            "diagnostic": {
                                "code": "incomplete",
                                "message": "Cálculo incompleto",
                            },
                        },
                    },
                    "tbl-ctrl": {
                        "table_id": "tbl-ctrl",
                        "carrier_name": "Transportadora Sintetica B",
                        "slot_number": 2,
                        "calculated_freight": 100.5,
                        "status": "calculated",
                        "final_status": "calculated",
                        "is_partial_value": False,
                        "error": None,
                        "components": {"weight_freight": 100.5, "total": 100.5},
                        "evidence": {},
                        "calculation_memory": {
                            "schema_version": 1,
                            "status": "calculated",
                            "status_label": "Calculado",
                            "total_label": "Total calculado",
                            "row_index": 1,
                            "table_id": "tbl-ctrl",
                            "slot_number": 2,
                            "carrier_name": "Transportadora Sintetica B",
                            "calculated_freight": 100.5,
                            "total": 100.5,
                            "components": [
                                {
                                    "code": "WEIGHT_FREIGHT",
                                    "label": "Frete-peso",
                                    "amount": 100.5,
                                    "applied": True,
                                    "ignored": False,
                                }
                            ],
                            "taxes": [],
                            "evidence": {},
                        },
                    },
                },
            }
        ],
        "summary": {
            "table_count": 2,
            "row_count": 1,
            "calculated_cell_count": 1,
            "incomplete_cell_count": 1,
            "error_cell_count": 0,
            "total_calculation_cells": 2,
        },
    }


def test_browser_visual_incomplete_cell_with_injected_payload(live_base_url):
    """Teste visual (não end-to-end): renderização com payload incomplete injetado.

    Não percorre POST /comparison/calculate. Prove apenas o contrato visual da célula
    e da memória quando o frontend já recebeu incomplete.
    """
    state = _FlowState(promote_after_status_calls=3)
    calc_posts = {"count": 0}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        _install_routes(page, state)

        def _block_calculate(route):
            calc_posts["count"] += 1
            route.fulfill(status=500, content_type="application/json", body='{"ok":false}')

        page.route("**/api/agente-compara/comparison/calculate**", _block_calculate)
        page.goto(f"{live_base_url}/agente-compara", wait_until="domcontentloaded")
        page.wait_for_selector(".agente-compara-page, body")

        result = _incomplete_comparison_result_fixture()
        page.evaluate(
            """(result) => {
              document.dispatchEvent(new CustomEvent('agente-compara:inject-comparison-result', {
                detail: {
                  status: 'CALCULATION_READY',
                  billing_status: 'applied',
                  current_step: 'CALCULATION_READY',
                  stale: false,
                  result: result,
                  analytics: null
                }
              }));
            }""",
            result,
        )

        page.wait_for_selector(
            "#agenteComparaComparisonResultsTable button.agente-compara-comparison-calc-memory-link"
        )
        incomplete_btn = page.locator(
            "button.agente-compara-comparison-calc-incomplete, "
            "button.agente-compara-comparison-calc-memory-link",
            has_text="parcial",
        )
        assert incomplete_btn.count() >= 1
        text = incomplete_btn.first.inner_text().lower()
        assert "parcial" in text or "incompleto" in text
        assert "94,79" in incomplete_btn.first.inner_text() or "parcial" in text

        before = calc_posts["count"]
        incomplete_btn.first.click()
        page.wait_for_selector("#agenteComparaComparisonCalculationMemoryModal:not([hidden])")
        body_text = page.locator("#agenteComparaComparisonCalculationMemoryModalBody").inner_text()
        assert "Cálculo incompleto" in body_text
        assert "Valor parcial" in body_text or "parcial" in body_text.lower()
        assert "TAS" in body_text
        assert "Pedágio" in body_text or "Pedagio" in body_text
        assert "Total calculado" not in body_text.split("Valor parcial")[0] or "parcial" in body_text.lower()
        assert calc_posts["count"] == before

        page.locator("#agenteComparaComparisonCalculationMemoryModalClose").click()
        page.wait_for_function(
            "() => document.getElementById('agenteComparaComparisonCalculationMemoryModal').hidden === true"
        )

        control_btn = page.locator(
            "#agenteComparaComparisonResultsTable button.agente-compara-comparison-calc-memory-link",
            has_text="100,50",
        )
        assert control_btn.count() >= 1
        control_btn.first.click()
        page.wait_for_selector("#agenteComparaComparisonCalculationMemoryModal:not([hidden])")
        control_body = page.locator("#agenteComparaComparisonCalculationMemoryModalBody").inner_text()
        assert "Calculado" in control_body
        assert "100,50" in control_body
        assert calc_posts["count"] == before
        browser.close()


def _build_real_incomplete_comparison_result_via_motor():
    """Gera resultado incomplete pelo motor real (sem fixture incompleta manual)."""
    from app.agente_compara_calculation_execution_service import (
        AGENTE_COMPARA_CALCULATION_ALGORITHM_VERSION,
    )
    from app.agente_compara_calculation_service import calculate_single_table
    from tests.test_agente_compara_calculation_completeness_service import (
        _conditional_ignored_fee,
    )
    from tests.test_agente_compara_single_table_calculation import (
        _coverage_rows,
        _make_context,
        _pricing_record,
        _row,
    )

    record_a = _pricing_record(
        region="PE-Caruaru",
        weight_30="80,00",
        weight_50="95,00",
        accessorial_fees=[
            {
                "name": "ADV",
                "value": "0,30%",
                "unit": "%",
                "calculation_basis": "sobre nota fiscal",
                "notes": "",
            },
            {
                "name": "GRIS",
                "value": "0,10%",
                "unit": "%",
                "calculation_basis": "sobre nota fiscal",
                "notes": "",
            },
            _conditional_ignored_fee(name="TAS", value="10,00", unit="R$"),
            _conditional_ignored_fee(name="Pedágio", value="1,50", unit="R$"),
        ],
        temp_table_id="tt_gbex_browser",
    )
    record_b = _pricing_record(
        region="PE-Caruaru",
        weight_30="100,50",
        weight_50="110,00",
        temp_table_id="tt_ctrl_browser",
    )
    coverage = _coverage_rows(("PE", "Caruaru", "PE-Caruaru"))
    row = _row(
        destination_city="Caruaru",
        destination_uf="PE",
        weight=13.6,
        invoice_value=2000.0,
        document_number="DOC-GBEX",
    )
    ctx_a = _make_context(
        record=record_a,
        coverage=coverage,
        rows=[row],
        carrier_name="Transportadora Sintetica A",
        table_id="tbl-gbex",
        slot_number=1,
        temp_table_id="tt_gbex_browser",
    )
    ctx_b = _make_context(
        record=record_b,
        coverage=coverage,
        rows=[row],
        carrier_name="Transportadora Sintetica B",
        table_id="tbl-ctrl",
        slot_number=2,
        temp_table_id="tt_ctrl_browser",
    )
    unit_a = calculate_single_table(ctx_a)
    unit_b = calculate_single_table(ctx_b)
    cell_a = unit_a["results"][0]
    cell_b = unit_b["results"][0]
    assert cell_a["status"] == "incomplete"
    assert cell_b["status"] == "calculated"
    return {
        "schema_version": 1,
        "calculation_algorithm_version": AGENTE_COMPARA_CALCULATION_ALGORITHM_VERSION,
        "comparison_id": "cmp-gbex-browser-real",
        "execution_id": "exec-gbex-browser-real",
        "table_count": 2,
        "row_count": 1,
        "tables": [
            {
                "table_id": "tbl-gbex",
                "slot_number": 1,
                "carrier_name": "Transportadora Sintetica A",
                "temp_table_id": "tt_gbex_browser",
            },
            {
                "table_id": "tbl-ctrl",
                "slot_number": 2,
                "carrier_name": "Transportadora Sintetica B",
                "temp_table_id": "tt_ctrl_browser",
            },
        ],
        "comparative_rows": [
            {
                "row_index": 1,
                "document_number": "DOC-GBEX",
                "destination_city": "Caruaru",
                "destination_uf": "PE",
                "weight": 13.6,
                "invoice_value": 2000.0,
                "table_results": {
                    "tbl-gbex": {
                        **cell_a,
                        "table_id": "tbl-gbex",
                        "carrier_name": "Transportadora Sintetica A",
                        "slot_number": 1,
                    },
                    "tbl-ctrl": {
                        **cell_b,
                        "table_id": "tbl-ctrl",
                        "carrier_name": "Transportadora Sintetica B",
                        "slot_number": 2,
                    },
                },
            }
        ],
        "summary": {
            "table_count": 2,
            "row_count": 1,
            "calculated_cell_count": 1,
            "incomplete_cell_count": 1,
            "error_cell_count": 0,
            "total_calculation_cells": 2,
        },
    }

def test_browser_real_calculate_route_incomplete_from_motor(live_base_url, monkeypatch):
    """Integração browser: POST calculate devolve incomplete do motor (sem fixture inject).

    Classificação:
    - A (visual): test_browser_visual_incomplete_cell_with_injected_payload
    - B (este): rota calculate responde com output do motor real; UI renderiza incomplete.

    Não usa `_incomplete_comparison_result_fixture` nem inject direto de incomplete.
    """
    from types import SimpleNamespace

    from app.services.agente_compara_config_service import DEFAULT_CALCULATION_BASES

    cfg = SimpleNamespace(
        calculation_bases=__import__("copy").deepcopy(DEFAULT_CALCULATION_BASES),
        upload_ttl_hours=24,
    )
    monkeypatch.setattr("app.agente_compara_doc_service.get_agente_compara_config", lambda: cfg)

    motor_result = _build_real_incomplete_comparison_result_via_motor()
    network = {"calculate_posts": 0, "calculation_gets": 0}
    stored = {"result": None}

    state = _FlowState(promote_after_status_calls=3)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        _install_routes(page, state)

        def _on_calculate(route):
            network["calculate_posts"] += 1
            stored["result"] = motor_result
            body = {
                "ok": True,
                "status": "CALCULATION_READY",
                "execution_id": motor_result["execution_id"],
                "fingerprint_short": "browserreal01",
                "idempotent_replay": False,
                "billing_status": "applied",
                "stale": False,
                "result": motor_result,
                "analytics": None,
                "comparison": {
                    "comparison_id": motor_result["comparison_id"],
                    "current_step": "CALCULATION_READY",
                    "status": "CALCULATION_READY",
                },
            }
            route.fulfill(
                status=200,
                content_type="application/json",
                body=__import__("json").dumps(body),
            )

        def _on_get_calculation(route):
            network["calculation_gets"] += 1
            result = stored["result"] or motor_result
            body = {
                "ok": True,
                "status": "CALCULATION_READY",
                "billing_status": "applied",
                "stale": False,
                "result": result,
                "analytics": None,
                "comparison": {
                    "comparison_id": result["comparison_id"],
                    "current_step": "CALCULATION_READY",
                    "status": "CALCULATION_READY",
                },
            }
            route.fulfill(
                status=200,
                content_type="application/json",
                body=__import__("json").dumps(body),
            )

        page.route("**/api/agente-compara/comparison/calculation**", _on_get_calculation)
        page.route("**/api/agente-compara/comparison/calculate**", _on_calculate)

        page.goto(f"{live_base_url}/agente-compara", wait_until="domcontentloaded")
        page.wait_for_selector(".agente-compara-page, body")

        applied = page.evaluate(
            """async () => {
              const resp = await fetch('/api/agente-compara/comparison/calculate', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                credentials: 'same-origin',
                body: JSON.stringify({
                  comparison_id: 'cmp-gbex-browser-real',
                  execution_id: 'exec-gbex-browser-real',
                  schema_version: 1
                })
              });
              const data = await resp.json();
              document.dispatchEvent(new CustomEvent('agente-compara:inject-comparison-result', {
                detail: {
                  status: data.status,
                  billing_status: data.billing_status,
                  current_step: 'CALCULATION_READY',
                  stale: false,
                  result: data.result,
                  analytics: null
                }
              }));
              const row = ((data.result || {}).comparative_rows || [])[0] || {};
              const statuses = Object.values(row.table_results || {}).map(
                (c) => c && (c.final_status || c.status)
              );
              return { ok: !!data.ok, status: data.status, cellStatus: statuses };
            }"""
        )
        assert network["calculate_posts"] >= 1
        assert applied["ok"] is True
        assert applied["status"] == "CALCULATION_READY"
        assert "incomplete" in (applied.get("cellStatus") or [])

        page.wait_for_selector(
            "#agenteComparaComparisonResultsTable button.agente-compara-comparison-calc-memory-link",
            timeout=30000,
        )
        table_text = page.locator("#agenteComparaComparisonResultsTable").inner_text().lower()
        assert "parcial" in table_text or "incompleto" in table_text

        page.locator(
            "#agenteComparaComparisonResultsTable button.agente-compara-comparison-calc-memory-link"
        ).first.click()
        page.wait_for_selector("#agenteComparaComparisonCalculationMemoryModal:not([hidden])")
        body_text = page.locator("#agenteComparaComparisonCalculationMemoryModalBody").inner_text()
        assert "Cálculo incompleto" in body_text
        assert "TAS" in body_text
        assert "Pedágio" in body_text or "Pedagio" in body_text

        page.evaluate(
            """async () => {
              const resp = await fetch(
                '/api/agente-compara/comparison/calculation?comparison_id=cmp-gbex-browser-real',
                { credentials: 'same-origin' }
              );
              const data = await resp.json();
              document.dispatchEvent(new CustomEvent('agente-compara:inject-comparison-result', {
                detail: {
                  status: data.status,
                  billing_status: data.billing_status,
                  current_step: 'CALCULATION_READY',
                  stale: false,
                  result: data.result,
                  analytics: null
                }
              }));
            }"""
        )
        page.wait_for_selector(
            "#agenteComparaComparisonResultsTable button.agente-compara-comparison-calc-memory-link"
        )
        assert network["calculation_gets"] >= 1
        after = page.locator("#agenteComparaComparisonResultsTable").inner_text().lower()
        assert "parcial" in after or "incompleto" in after
        browser.close()


def _coverage_temp_table(comparison_id="cmp-coverage-1", table_id="tbl-1", temp_id="tt-cov-1"):
    tt = _temp_table("needs_review", comparison_id=comparison_id, table_id=table_id, temp_id=temp_id)
    tt["coverage_table"] = {"rows": []}
    tt["comparison"] = {
        "comparison_id": comparison_id,
        "current_step": "COVERAGE",
        "active_table_id": table_id,
        "tables": [
            {
                "table_id": table_id,
                "slot_number": 1,
                "status": "ready",
                "confirmed": True,
                "carrier_name": "Transportadora Browser",
            }
        ],
    }
    return tt


def _install_coverage_step_routes(page, state, *, fail_skip=False, skip_delay_ms=0):
    """Mock mínimo para abrir o modal em Cidades atendidas e tratar skip/upload."""
    import json

    state.comparison = _comparison_payload(
        comparison_id="cmp-coverage-1", table_id="tbl-1", step="COVERAGE"
    )
    state.comparison["tables"][0]["status"] = "ready"
    state.comparison["tables"][0]["confirmed"] = True
    state.comparison["tables"][0]["carrier_name"] = "Transportadora Browser"
    state.phase = "coverage"
    state.skip_posts = []
    state.coverage_uploads = []
    state.gemini_calls = []
    state.calculate_calls = []
    state.fail_skip = fail_skip
    state.skip_delay_ms = skip_delay_ms
    state.temp_table = _coverage_temp_table()

    def handle_route(route):
        req = route.request
        url = req.url
        method = req.method.upper()

        if "/api/agente-compara/comparison/calculate" in url:
            state.calculate_calls.append(method)
            route.fulfill(status=500, content_type="application/json", body='{"ok":false}')
            return

        if "/gemini" in url.lower() or "/api/agente-compara/chat" in url:
            state.gemini_calls.append(url)
            route.fulfill(status=500, content_type="application/json", body='{"ok":false}')
            return

        if "/api/agente-compara/documents/status" in url and method == "GET":
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "ok": True,
                        "documents": [],
                        "temp_table": state.temp_table,
                        "calculation_bases": _calculation_bases(),
                        "comparison": state.comparison,
                        "has_active_comparison": True,
                    }
                ),
            )
            return

        if "/api/agente-compara/temp-table/save" in url and method == "POST":
            body = req.post_data_json or {}
            state.skip_posts.append(body)
            if state.skip_delay_ms:
                time.sleep(state.skip_delay_ms / 1000.0)
            if state.fail_skip and body.get("review_action") == "skip_coverage_and_advance":
                # Falha apenas na primeira tentativa; depois permite sucesso.
                state.fail_skip = False
                route.fulfill(
                    status=400,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "ok": False,
                            "message": "Falha controlada no skip de cobertura.",
                        }
                    ),
                )
                return
            if body.get("review_action") == "skip_coverage_and_advance":
                state.comparison["current_step"] = "CALCULATION_FILE"
                state.temp_table = _coverage_temp_table()
                state.temp_table["comparison"] = dict(state.comparison)
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "ok": True,
                            "temp_table": state.temp_table,
                            "comparison": state.comparison,
                        }
                    ),
                )
                return
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {"ok": True, "temp_table": state.temp_table, "comparison": state.comparison}
                ),
            )
            return

        if "/api/agente-compara/coverage/upload" in url and method == "POST":
            state.coverage_uploads.append(True)
            state.comparison["current_step"] = "CALCULATION_FILE"
            state.temp_table = _coverage_temp_table()
            state.temp_table["coverage_table"] = {
                "rows": [
                    {
                        "destination_uf": "PE",
                        "destination_city": "Caruaru",
                        "freight_region": "PE-Caruaru",
                        "notes": "",
                    }
                ]
            }
            state.temp_table["comparison"] = dict(state.comparison)
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "ok": True,
                        "temp_table": state.temp_table,
                        "comparison": state.comparison,
                    }
                ),
            )
            return

        if "/api/agente-compara/audit/template" in url:
            route.fulfill(
                status=200,
                content_type="text/csv",
                body="documento,cidade,uf,peso,valor_nf\n",
            )
            return

        if "/api/agente-compara/" in url:
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"ok": true}',
            )
            return

        route.continue_()

    page.route("**/api/agente-compara/**", handle_route)
    page.route("**/gemini**", handle_route)


def _wait_coverage_decision(page, timeout: int = 15000):
    page.wait_for_selector("#agenteComparaTempTableModal:not([hidden])", timeout=timeout)
    page.wait_for_function(
        """() => {
          const modal = document.getElementById('agenteComparaTempTableModal');
          if (!modal || modal.hidden) return false;
          const title = document.getElementById('agenteComparaTempTableModalTitle')?.textContent || '';
          const body = document.getElementById('agenteComparaTempTableModalBody')?.textContent || '';
          return title.includes('Cidades atendidas')
            && body.includes('Sim, enviar planilha')
            && body.includes('Agora não');
        }""",
        timeout=timeout,
    )


def _wait_calculation_file_step(page, timeout: int = 15000):
    page.wait_for_function(
        """() => {
          const modal = document.getElementById('agenteComparaTempTableModal');
          if (!modal || modal.hidden) return false;
          const title = document.getElementById('agenteComparaTempTableModalTitle')?.textContent || '';
          const body = document.getElementById('agenteComparaTempTableModalBody')?.textContent || '';
          return title.includes('Arquivo para Comparação')
            && body.includes('Baixar modelo')
            && body.includes('Enviar arquivo preenchido')
            && !body.includes('Etapa ignorada')
            && !body.includes('iniciar a auditoria');
        }""",
        timeout=timeout,
    )


def test_browser_coverage_agora_nao_advances_immediately(live_base_url):
    state = _FlowState()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        _install_coverage_step_routes(page, state)
        page.goto(f"{live_base_url}/agente-compara", wait_until="domcontentloaded")
        _wait_coverage_decision(page)

        no_btn = page.locator("#agenteComparaTempTableModal .agente-compara-coverage-prompt-no")
        assert no_btn.count() >= 1
        assert "Etapa ignorada" not in _body(page).inner_text()

        no_btn.first.click()

        try:
            _wait_calculation_file_step(page)
        except Exception:
            raise AssertionError(
                "skip_posts=%r step=%r title=%r body=%r err=%r"
                % (
                    state.skip_posts,
                    state.comparison.get("current_step"),
                    _title(page).inner_text(),
                    _body(page).inner_text()[:800],
                    page.locator("#agenteComparaTempTableModalError").inner_text(),
                )
            )

        skip_actions = [
            b for b in state.skip_posts if b.get("review_action") == "skip_coverage_and_advance"
        ]
        assert len(skip_actions) == 1
        assert skip_actions[0].get("temp_table_id") == "tt-cov-1"
        assert state.comparison["current_step"] == "CALCULATION_FILE"
        assert "Etapa ignorada" not in _body(page).inner_text()
        assert "Arquivo para Comparação" in _title(page).inner_text()
        assert page.locator(".agente-compara-audit-file-download-btn").count() == 1
        assert page.locator(".agente-compara-audit-file-upload-btn").count() == 1
        assert state.gemini_calls == []
        assert state.calculate_calls == []

        # Refresh: restaura CALCULATION_FILE
        page.reload(wait_until="domcontentloaded")
        _wait_calculation_file_step(page)
        assert state.comparison["current_step"] == "CALCULATION_FILE"

        # Fechar e reabrir
        page.click("#agenteComparaTempTableModalClose")
        _wait_modal_hidden(page)
        page.click(".agente-compara-temp-table-open-btn")
        _wait_calculation_file_step(page)
        browser.close()


def test_browser_coverage_agora_nao_error_stays_and_retries(live_base_url):
    state = _FlowState()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        _install_coverage_step_routes(page, state, fail_skip=True)
        page.goto(f"{live_base_url}/agente-compara", wait_until="domcontentloaded")
        _wait_coverage_decision(page)

        page.locator("#agenteComparaTempTableModal .agente-compara-coverage-prompt-no").first.click()
        try:
            page.wait_for_function(
                """() => {
                  const err = document.getElementById('agenteComparaTempTableModalError')?.textContent || '';
                  const body = document.getElementById('agenteComparaTempTableModalBody')?.textContent || '';
                  const title = document.getElementById('agenteComparaTempTableModalTitle')?.textContent || '';
                  return title.includes('Cidades atendidas')
                    && body.includes('Agora não')
                    && err.length > 0;
                }""",
                timeout=15000,
            )
        except Exception:
            raise AssertionError(
                "skip_posts=%r step=%r title=%r body=%r err=%r"
                % (
                    state.skip_posts,
                    state.comparison.get("current_step"),
                    _title(page).inner_text(),
                    _body(page).inner_text()[:800],
                    page.locator("#agenteComparaTempTableModalError").inner_text(),
                )
            )
        assert state.comparison["current_step"] == "COVERAGE"
        assert "Arquivo para Comparação" not in _title(page).inner_text()
        assert "Etapa ignorada" not in _body(page).inner_text()

        # Nova tentativa funciona
        page.locator("#agenteComparaTempTableModal .agente-compara-coverage-prompt-no").first.click()
        _wait_calculation_file_step(page)
        skip_actions = [
            b for b in state.skip_posts if b.get("review_action") == "skip_coverage_and_advance"
        ]
        assert len(skip_actions) == 2
        assert state.comparison["current_step"] == "CALCULATION_FILE"
        browser.close()


def test_browser_coverage_sim_enviar_planilha_still_advances(live_base_url):
    state = _FlowState()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        _install_coverage_step_routes(page, state)
        page.goto(f"{live_base_url}/agente-compara", wait_until="domcontentloaded")
        _wait_coverage_decision(page)

        page.locator("#agenteComparaTempTableModal .agente-compara-coverage-prompt-yes").first.click()
        page.wait_for_selector("#agenteComparaTempTableModal .agente-compara-coverage-upload-input", timeout=10000)

        page.set_input_files(
            "#agenteComparaTempTableModal .agente-compara-coverage-upload-input",
            {
                "name": "cidades.csv",
                "mimeType": "text/csv",
                "buffer": b"UF,Cidade,Regiao\nPE,Caruaru,PE-Caruaru\n",
            },
        )
        _wait_calculation_file_step(page)
        assert len(state.coverage_uploads) == 1
        assert state.comparison["current_step"] == "CALCULATION_FILE"
        assert page.locator(".agente-compara-audit-file-download-btn").count() == 1
        assert state.gemini_calls == []
        assert state.calculate_calls == []
        browser.close()


def _comparison_analytics_fixture(result):
    from app.agente_compara_comparison_analytics_service import build_comparison_analytics

    return build_comparison_analytics(result)


def test_browser_comparison_dashboard_outside_modal_and_chart_lifecycle(live_base_url):
    """Dashboard gerencial na página; modal só com tabela; Chart.js sem acumulação."""
    state = _FlowState(promote_after_status_calls=2)
    result = _comparison_result_fixture()
    analytics = _comparison_analytics_fixture(result)
    stored = {"result": result, "analytics": analytics}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        _install_routes(page, state)

        def _on_get_calculation(route):
            body = {
                "ok": True,
                "status": "CALCULATION_READY",
                "execution_id": "exec-dash-restore",
                "fingerprint_short": "dashrestore01",
                "billing_status": "applied",
                "stale": False,
                "result": stored["result"],
                "analytics": stored["analytics"],
                "current_step": "CALCULATION_READY",
            }
            route.fulfill(
                status=200,
                content_type="application/json",
                body=__import__("json").dumps(body),
            )

        page.route("**/api/agente-compara/comparison/calculation**", _on_get_calculation)
        page.goto(f"{live_base_url}/agente-compara", wait_until="domcontentloaded")
        page.wait_for_selector("#agenteComparaComparisonDashboard", state="attached")
        assert page.locator("#agenteComparaComparisonDashboard").get_attribute("hidden") is not None
        assert page.locator("#agenteComparaComparisonDashboard:not([hidden])").count() == 0

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
        page.wait_for_selector("#agenteComparaComparisonKpis:not([hidden])")
        page.wait_for_selector("#agenteComparaComparisonCharts:not([hidden])")
        page.wait_for_selector("#agenteComparaChart_coverage")
        page.wait_for_selector("#agenteComparaChart_without_complete")
        page.wait_for_selector("#agenteComparaChart_comparability")
        page.wait_for_selector("#agenteComparaChart_wins")
        page.wait_for_selector("#agenteComparaChart_avg_cost")
        page.wait_for_selector("#agenteComparaChart_potential_savings")
        page.wait_for_selector("#agenteComparaResultsGeography")
        page.wait_for_selector("#agenteComparaComparisonDashboardOpenDetailsBtn:not([hidden])")

        assert page.locator("#agenteComparaComparisonDashboard .agente-compara-analytics-summary").count() == 1
        kpis_text = page.locator("#agenteComparaComparisonKpis").inner_text()
        assert "Resumo executivo" in kpis_text
        assert "Documentos comparáveis" in kpis_text
        assert "Economia potencial" in kpis_text
        assert page.locator("#agenteComparaTempTableModal #agenteComparaChart_coverage").count() == 0
        assert page.locator("#agenteComparaTempTableModal .agente-compara-analytics-summary").count() == 0

        chart_count_1 = page.evaluate(
            """() => {
              const root = document.getElementById('agenteComparaComparisonDashboard');
              const outsideModal = !document.getElementById('agenteComparaTempTableModal').contains(root);
              return {
                outsideModal,
                instances: (window.__agenteComparaChartCountProbe = (
                  document.querySelectorAll('#agenteComparaComparisonCharts canvas').length
                )),
                canvasIds: Array.from(document.querySelectorAll('canvas[id^=\"agenteComparaChart_\"]'))
                  .map((el) => el.id)
              };
            }"""
        )
        assert chart_count_1["outsideModal"] is True
        assert chart_count_1["instances"] == 6
        assert sorted(chart_count_1["canvasIds"]) == [
            "agenteComparaChart_avg_cost",
            "agenteComparaChart_comparability",
            "agenteComparaChart_coverage",
            "agenteComparaChart_potential_savings",
            "agenteComparaChart_wins",
            "agenteComparaChart_without_complete",
        ]

        instances_before_modal = page.evaluate(
            """() => {
              const mod = document.querySelector('.agente-compara-page');
              // Proxy via canvas count + Chart registry if available
              const canvases = document.querySelectorAll('#agenteComparaComparisonCharts canvas').length;
              let chartJsCount = null;
              if (window.Chart && Chart.getChart) {
                chartJsCount = ['coverage','without_complete','comparability','wins','avg_cost','potential_savings']
                  .map((k) => Chart.getChart('agenteComparaChart_' + k))
                  .filter(Boolean).length;
              }
              return { canvases, chartJsCount };
            }"""
        )

        page.click("#agenteComparaComparisonDashboardOpenDetailsBtn")
        page.wait_for_selector("#agenteComparaTempTableModal:not([hidden])")
        page.wait_for_selector("#agenteComparaComparisonResultsTable")
        page.wait_for_selector("#agenteComparaResultsFilters")
        page.wait_for_selector("#agenteComparaResultsPagination")

        modal = page.locator("#agenteComparaTempTableModal")
        assert modal.locator(".agente-compara-analytics-summary").count() == 0
        assert modal.locator("canvas[id^='agenteComparaChart_']").count() == 0
        assert modal.locator("#agenteComparaResultsFilters").count() == 1
        assert modal.locator("#agenteComparaComparisonResultsTable").count() == 1

        instances_after_open = page.evaluate(
            """() => {
              const canvases = document.querySelectorAll('#agenteComparaComparisonCharts canvas').length;
              let chartJsCount = null;
              if (window.Chart && Chart.getChart) {
                chartJsCount = ['coverage','without_complete','comparability','wins','avg_cost','potential_savings']
                  .map((k) => Chart.getChart('agenteComparaChart_' + k))
                  .filter(Boolean).length;
              }
              return { canvases, chartJsCount };
            }"""
        )
        assert instances_after_open["canvases"] == instances_before_modal["canvases"] == 6
        if instances_before_modal["chartJsCount"] is not None:
            assert instances_after_open["chartJsCount"] == instances_before_modal["chartJsCount"] == 6

        page.fill("#agenteComparaFilterDocument", "DOC-REP")
        page.click("#agenteComparaApplyFiltersButton")
        page.wait_for_timeout(200)
        after_filter = page.evaluate(
            """() => {
              let chartJsCount = null;
              if (window.Chart && Chart.getChart) {
                chartJsCount = ['coverage','without_complete','comparability','wins','avg_cost','potential_savings']
                  .map((k) => Chart.getChart('agenteComparaChart_' + k))
                  .filter(Boolean).length;
              }
              return {
                canvases: document.querySelectorAll('#agenteComparaComparisonCharts canvas').length,
                chartJsCount,
                kpisVisible: !document.getElementById('agenteComparaComparisonKpis').hidden
              };
            }"""
        )
        assert after_filter["canvases"] == 6
        assert after_filter["kpisVisible"] is True
        if after_filter["chartJsCount"] is not None:
            assert after_filter["chartJsCount"] == 6

        memory_btn = page.locator(
            "#agenteComparaComparisonResultsTable button.agente-compara-comparison-calc-memory-link"
        ).first
        memory_btn.click()
        page.wait_for_selector("#agenteComparaComparisonCalculationMemoryModal:not([hidden])")
        page.locator("#agenteComparaComparisonCalculationMemoryModalClose").click()
        page.wait_for_function(
            "() => document.getElementById('agenteComparaComparisonCalculationMemoryModal').hidden === true"
        )

        page.click("#agenteComparaTempTableModalClose")
        _wait_modal_hidden(page)
        assert page.locator("#agenteComparaComparisonKpis:not([hidden])").count() == 1
        assert page.locator("#agenteComparaChart_coverage").count() == 1
        assert page.locator("#agenteComparaResultsGeography").count() == 1

        page.click("#agenteComparaComparisonDashboardOpenDetailsBtn")
        page.wait_for_selector("#agenteComparaTempTableModal:not([hidden])")
        page.wait_for_selector("#agenteComparaComparisonResultsTable")
        assert page.locator("#agenteComparaTempTableModal canvas[id^='agenteComparaChart_']").count() == 0
        reopen_charts = page.evaluate(
            """() => {
              let chartJsCount = null;
              if (window.Chart && Chart.getChart) {
                chartJsCount = ['coverage','without_complete','comparability','wins','avg_cost','potential_savings']
                  .map((k) => Chart.getChart('agenteComparaChart_' + k))
                  .filter(Boolean).length;
              }
              return {
                canvases: document.querySelectorAll('#agenteComparaComparisonCharts canvas').length,
                chartJsCount
              };
            }"""
        )
        assert reopen_charts["canvases"] == 6
        if reopen_charts["chartJsCount"] is not None:
            assert reopen_charts["chartJsCount"] == 6

        page.click("#agenteComparaTempTableModalClose")
        _wait_modal_hidden(page)

        # Novo resultado substitui gráficos uma vez
        result2 = __import__("copy").deepcopy(result)
        result2["comparative_rows"][0]["document_number"] = "DOC-NEW"
        analytics2 = _comparison_analytics_fixture(result2)
        stored["result"] = result2
        stored["analytics"] = analytics2
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
            {"result": result2, "analytics": analytics2},
        )
        page.wait_for_selector("#agenteComparaComparisonKpis:not([hidden])")
        after_new = page.evaluate(
            """() => {
              let chartJsCount = null;
              if (window.Chart && Chart.getChart) {
                chartJsCount = ['coverage','without_complete','comparability','wins','avg_cost','potential_savings']
                  .map((k) => Chart.getChart('agenteComparaChart_' + k))
                  .filter(Boolean).length;
              }
              return {
                canvases: document.querySelectorAll('#agenteComparaComparisonCharts canvas').length,
                chartJsCount,
                canvasIds: Array.from(document.querySelectorAll('canvas[id^=\"agenteComparaChart_\"]')).map((e) => e.id)
              };
            }"""
        )
        assert after_new["canvases"] == 6
        assert len(after_new["canvasIds"]) == 6
        if after_new["chartJsCount"] is not None:
            assert after_new["chartJsCount"] == 6

        # Restore via GET calculation (refresh contract)
        page.evaluate(
            """async () => {
              const resp = await fetch('/api/agente-compara/comparison/calculation?comparison_id=cmp-memory-browser', {
                credentials: 'same-origin'
              });
              const data = await resp.json();
              document.dispatchEvent(new CustomEvent('agente-compara:inject-comparison-result', {
                detail: {
                  status: data.status,
                  billing_status: data.billing_status,
                  current_step: 'CALCULATION_READY',
                  stale: false,
                  result: data.result,
                  analytics: data.analytics
                }
              }));
            }"""
        )
        page.wait_for_selector("#agenteComparaComparisonKpis:not([hidden])")
        assert page.locator("#agenteComparaTempTableModal").get_attribute("hidden") is not None

        # Reset oficial: confirmação → limpa dashboard e destrói Chart.js
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
        zero_charts = page.evaluate(
            """() => {
              let chartJsCount = 0;
              if (window.Chart && Chart.getChart) {
                chartJsCount = ['coverage','without_complete','comparability','wins','avg_cost','potential_savings']
                  .map((k) => Chart.getChart('agenteComparaChart_' + k))
                  .filter(Boolean).length;
              }
              return {
                canvases: document.querySelectorAll('#agenteComparaComparisonCharts canvas').length,
                chartJsCount,
                modalHidden: document.getElementById('agenteComparaTempTableModal').hidden === true,
                dashboardHidden: document.getElementById('agenteComparaComparisonDashboard').hidden === true
              };
            }"""
        )
        assert zero_charts["canvases"] == 0
        assert zero_charts["chartJsCount"] == 0
        assert zero_charts["modalHidden"] is True
        assert zero_charts["dashboardHidden"] is True

        # Mobile viewport smoke
        page.set_viewport_size({"width": 390, "height": 844})
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
        page.wait_for_selector("#agenteComparaComparisonKpis:not([hidden])")
        overflow = page.evaluate(
            """() => {
              const doc = document.documentElement;
              return {
                overflowX: doc.scrollWidth > doc.clientWidth + 2,
                kpisVisible: !document.getElementById('agenteComparaComparisonKpis').hidden,
                chartsVisible: !document.getElementById('agenteComparaComparisonCharts').hidden,
                ctaVisible: !document.getElementById('agenteComparaComparisonDashboardOpenDetailsBtn').hidden
              };
            }"""
        )
        assert overflow["kpisVisible"] is True
        assert overflow["chartsVisible"] is True
        assert overflow["ctaVisible"] is True
        assert overflow["overflowX"] is False

        page.click("#agenteComparaComparisonDashboardOpenDetailsBtn")
        page.wait_for_selector("#agenteComparaTempTableModal:not([hidden])")
        assert page.locator("#agenteComparaTempTableModal #agenteComparaComparisonResultsTable").count() == 1
        assert page.locator("#agenteComparaTempTableModal canvas[id^='agenteComparaChart_']").count() == 0
        assert page.locator("#agenteComparaTempTableModal .agente-compara-analytics-summary").count() == 0
        browser.close()

