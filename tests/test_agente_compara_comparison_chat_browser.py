"""Browser controlado: chat inteligente do AgenteCompara (sem Gemini real)."""
from __future__ import annotations

import json
import pathlib

import pytest

playwright = pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402

pytest_plugins = ["tests.test_agente_compara_freight_modal_browser"]

REPO = pathlib.Path(__file__).resolve().parents[1]


def _ready_calc_payload():
    return {
        "ok": True,
        "status": "CALCULATION_READY",
        "execution_id": "exec-chat-browser",
        "fingerprint_short": "fpb1",
        "stale": False,
        "billing_status": "applied",
        "current_step": "CALCULATION_READY",
        "comparison_id": "cmp-chat-browser",
        "result": {
            "schema_version": 1,
            "comparison_id": "cmp-chat-browser",
            "execution_id": "exec-chat-browser",
            "table_count": 2,
            "row_count": 1,
            "tables": [
                {"table_id": "tbl-1", "slot_number": 1, "carrier_name": "Alpha", "temp_table_id": "tt1"},
                {"table_id": "tbl-2", "slot_number": 2, "carrier_name": "Beta", "temp_table_id": "tt2"},
            ],
            "results_by_table": {},
            "comparative_rows": [
                {
                    "row_index": 1,
                    "document_number": "1001",
                    "destination_city": "Campinas",
                    "destination_uf": "SP",
                    "weight": 10,
                    "invoice_value": 100,
                    "table_results": {
                        "tbl-1": {
                            "table_id": "tbl-1",
                            "carrier_name": "Alpha",
                            "slot_number": 1,
                            "calculated_freight": 40,
                            "status": "calculated",
                        },
                        "tbl-2": {
                            "table_id": "tbl-2",
                            "carrier_name": "Beta",
                            "slot_number": 2,
                            "calculated_freight": 55,
                            "status": "calculated",
                        },
                    },
                }
            ],
            "summary": {},
        },
        "analytics": {
            "schema_version": 1,
            "comparison_id": "cmp-chat-browser",
            "table_count": 2,
            "row_count": 1,
            "global_summary": {"row_count": 1},
            "executive_summary": {
                "row_count": 1,
                "fully_comparable_rows": 1,
                "fully_comparable_percentage": 100,
                "total_potential_savings": 15,
                "rows_without_complete_calculation": 0,
                "lead_display_name": "Alpha",
                "lead_wins": 1,
                "lead_win_percentage": 100,
            },
            "comparability": {"fully_comparable_rows": 1, "fully_comparable_percentage": 100},
            "tables": [],
            "carrier_competitiveness": [],
            "geography": {"destination_ufs": [], "uf_potential_ranking": []},
        },
    }


def test_comparison_chat_ready_gate_browser(live_base_url):
    chat_calls = {"n": 0, "bodies": []}
    phase = {"value": "pre_ready", "provider_error": False}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})

        def handle_route(route):
            req = route.request
            url = req.url
            if "/api/agente-compara/comparison-chat" in url and req.method == "POST":
                chat_calls["n"] += 1
                body = req.post_data_json or {}
                chat_calls["bodies"].append(body)
                if phase["value"] != "ready":
                    route.fulfill(
                        status=409,
                        content_type="application/json",
                        body=json.dumps(
                            {
                                "ok": False,
                                "error": True,
                                "error_code": "COMPARISON_CHAT_NOT_READY",
                                "message": "Faça o upload da tabela de frete.",
                                "chat_available": False,
                                "capability": "locked",
                                "retryable": False,
                            }
                        ),
                    )
                    return
                if phase.get("provider_error"):
                    route.fulfill(
                        status=503,
                        content_type="application/json",
                        body=json.dumps(
                            {
                                "ok": False,
                                "error": True,
                                "error_code": "provider_request_failed",
                                "message": (
                                    "O serviço de inteligência artificial está indisponível "
                                    "no momento. Tente novamente em instantes."
                                ),
                                "chat_available": True,
                                "capability": "ready",
                                "retryable": True,
                                "request_id": body.get("request_id"),
                            }
                        ),
                    )
                    return
                question = (body.get("message") or body.get("question") or "").lower()
                if "escolha" in question or "contratar" in question or "decida" in question or "fecho" in question:
                    answer = (
                        "Não posso decidir. Os dados indicam trade-off entre custo e cobertura. "
                        "A decisão final é do usuário."
                    )
                    scope = "decision_request"
                elif "e-mail" in question or "email" in question or "relatório" in question or "relatorio" in question:
                    answer = (
                        "Rascunho (não enviado)\nAssunto: Resumo da comparação\n"
                        "Prezados,\nSegue panorama.\nAtenciosamente,\n[Seu nome]"
                    )
                    scope = "executive_draft"
                elif "xyz" in question or "desconhecida" in question:
                    answer = "Resposta Gemini para pergunta livre desconhecida."
                    scope = "overview"
                elif question.strip() in {"oi", "olá", "ola"}:
                    answer = "Olá! Posso ajudar com a comparação vigente."
                    scope = "overview"
                else:
                    answer = "Resposta Gemini mock da comparação."
                    scope = "overview"
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "ok": True,
                            "answer": answer,
                            "scope": scope,
                            "deterministic": False,
                            "basis": {"table_count": 2},
                            "warnings": [],
                            "chat_available": True,
                            "capability": "ready",
                            "request_id": body.get("request_id"),
                        }
                    ),
                )
                return
            if "/api/agente-compara/comparison/calculation" in url and req.method == "GET":
                if phase["value"] == "ready":
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(_ready_calc_payload()),
                    )
                elif phase["value"] == "running":
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(
                            {
                                "ok": True,
                                "status": "CALCULATION_RUNNING",
                                "result": None,
                                "analytics": None,
                                "stale": False,
                                "billing_status": "not_started",
                                "current_step": "CALCULATION_RUNNING",
                                "comparison_id": "cmp-chat-browser",
                            }
                        ),
                    )
                else:
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(
                            {
                                "ok": True,
                                "status": "not_started",
                                "result": None,
                                "analytics": None,
                                "stale": False,
                                "billing_status": "not_started",
                                "current_step": "PREPARE_TABLE_1",
                                "comparison_id": "cmp-chat-browser",
                            }
                        ),
                    )
                return
            if "/api/agente-compara/comparison/start" in url:
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "ok": True,
                            "comparison": {
                                "comparison_id": "cmp-chat-browser",
                                "current_step": "PREPARE_TABLE_1",
                                "active_table_id": "tbl-1",
                                "desired_table_count": 2,
                                "tables": [
                                    {
                                        "table_id": "tbl-1",
                                        "slot_number": 1,
                                        "status": "empty",
                                        "confirmed": False,
                                        "carrier_name": "",
                                    }
                                ],
                            },
                        }
                    ),
                )
                return
            if "/api/agente-compara/documents/status" in url:
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "ok": True,
                            "documents": [],
                            "comparison": {
                                "comparison_id": "cmp-chat-browser",
                                "current_step": "PREPARE_TABLE_1" if phase["value"] != "ready" else "CALCULATION_READY",
                                "active_table_id": "tbl-1",
                                "desired_table_count": 2,
                                "tables": [
                                    {
                                        "table_id": "tbl-1",
                                        "slot_number": 1,
                                        "status": "empty",
                                        "confirmed": False,
                                        "carrier_name": "",
                                    }
                                ],
                            },
                        }
                    ),
                )
                return
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"ok": True}),
            )

        page.route("**/api/agente-compara/**", handle_route)
        page.goto(f"{live_base_url}/agente-compara", wait_until="domcontentloaded")
        page.wait_for_timeout(400)

        # Pré-READY: composer utilizável; envio local com orientação fixa; zero POST.
        input_el = page.locator("#agenteComparaInput")
        send_el = page.locator("#agenteComparaSend")
        assert not input_el.is_disabled()
        assert not send_el.is_disabled()
        assert input_el.get_attribute("aria-disabled") in (None, "false")
        assert "Faça o upload da tabela de frete." in (input_el.get_attribute("placeholder") or "")
        assert page.locator("#agenteComparaChatResponsibility").is_hidden()
        assert page.locator("#agenteComparaChatSuggestions").is_hidden()
        assert page.locator("#agenteComparaChatClearBtn").is_hidden()
        assert page.locator("#agenteComparaChatScope").inner_text().strip() == ""

        before = chat_calls["n"]
        guidance_expected = 0
        for question in (
            "Olá",
            "Qual transportadora devo escolher?",
            "Explique o cálculo",
            "texto aleatório xyz999",
        ):
            page.fill("#agenteComparaInput", question)
            page.click("#agenteComparaSend")
            guidance_expected += 1
            page.wait_for_function(
                """(expected) => {
                  const users = Array.from(document.querySelectorAll(
                    '#agenteComparaMessages .agente-compara-chat-msg-user[data-chat-flow-guidance="true"]'
                  ));
                  const bots = Array.from(document.querySelectorAll(
                    '#agenteComparaMessages [data-chat-blocked-guidance="true"] .agente-compara-chat-msg-inner'
                  ));
                  const hasUser = users.some((el) => (el.textContent || '').includes(expected.q));
                  const fullBots = bots.filter(
                    (el) => (el.textContent || '').trim() === expected.msg
                  ).length;
                  return hasUser && fullBots >= expected.n;
                }""",
                arg={"q": question, "msg": "Faça o upload da tabela de frete.", "n": guidance_expected},
                timeout=8000,
            )
            assert chat_calls["n"] == before

        # Enter também aciona o fluxo local
        page.fill("#agenteComparaInput", "via Enter")
        page.locator("#agenteComparaInput").press("Enter")
        guidance_expected += 1
        page.wait_for_function(
            """(expected) => {
              const users = Array.from(document.querySelectorAll(
                '#agenteComparaMessages .agente-compara-chat-msg-user[data-chat-flow-guidance="true"]'
              ));
              const bots = Array.from(document.querySelectorAll(
                '#agenteComparaMessages [data-chat-blocked-guidance="true"] .agente-compara-chat-msg-inner'
              ));
              const hasUser = users.some((el) => (el.textContent || '').includes('via Enter'));
              const fullBots = bots.filter(
                (el) => (el.textContent || '').trim() === expected.msg
              ).length;
              return hasUser && fullBots >= expected.n;
            }""",
            arg={"msg": "Faça o upload da tabela de frete.", "n": guidance_expected},
            timeout=8000,
        )
        assert chat_calls["n"] == before
        assert (
            page.locator("#agenteComparaMessages [data-chat-flow-guidance='true']").count()
            >= guidance_expected
        )

        # Simula READY via hook oficial de inject
        phase["value"] = "ready"
        page.evaluate(
            """(payload) => {
              document.dispatchEvent(new CustomEvent('agente-compara:inject-comparison-result', {
                detail: payload
              }));
            }""",
            _ready_calc_payload(),
        )
        page.wait_for_timeout(400)

        assert not input_el.is_disabled()
        assert not send_el.is_disabled()
        assert page.locator("#agenteComparaChatResponsibility").is_visible()
        assert page.locator("#agenteComparaChatSuggestions").is_visible()
        assert page.locator("#agenteComparaChatClearBtn").is_visible()
        # Mensagens transitórias pré-READY são limpas na transição.
        assert page.locator("#agenteComparaMessages [data-chat-flow-guidance='true']").count() == 0
        assert page.locator("#agenteComparaMessages [data-chat-blocked-guidance='true']").count() == 0

        # Suggestion não auto-envia
        before = chat_calls["n"]
        suggestion = page.locator("[data-chat-suggestion]").first
        if suggestion.count():
            suggestion.click()
            page.wait_for_timeout(200)
            assert chat_calls["n"] == before

        page.fill("#agenteComparaInput", "oi")
        page.click("#agenteComparaSend")
        page.wait_for_timeout(400)
        assert chat_calls["n"] == before + 1
        assert chat_calls["bodies"][-1].get("message") == "oi"
        # Histórico enviado ao Gemini não inclui orientação pré-READY.
        hist = chat_calls["bodies"][-1].get("history") or []
        assert all(
            "Faça o upload da tabela de frete." not in str(item.get("content") or "")
            for item in hist
        )
        content = page.locator("#agenteComparaMessages").inner_text().lower()
        assert "olá" in content or "ola" in content.replace("á", "a")
        # Resposta analítica não é a orientação fixa de upload.
        bot_texts = page.locator(
            "#agenteComparaMessages .agente-compara-chat-msg-bot .agente-compara-chat-msg-inner"
        ).all_inner_texts()
        analytical_bots = [
            t.strip()
            for t in bot_texts
            if t.strip() and t.strip() != "Faça o upload da tabela de frete."
        ]
        assert analytical_bots, "esperava resposta inteligente pós-READY"
        assert all("faça o upload da tabela de frete." not in t.lower() for t in analytical_bots)

        page.fill("#agenteComparaInput", "pergunta desconhecida xyz123")
        page.click("#agenteComparaSend")
        page.wait_for_timeout(300)
        assert chat_calls["n"] == before + 2

        page.fill("#agenteComparaInput", "Crie um e-mail com o resumo")
        page.click("#agenteComparaSend")
        page.wait_for_timeout(300)
        assert chat_calls["n"] == before + 3

        page.fill("#agenteComparaInput", "Escolha a melhor transportadora.")
        page.click("#agenteComparaSend")
        page.wait_for_selector("#agenteComparaMessages .agente-compara-chat-msg-bot", timeout=5000)
        page.wait_for_timeout(300)
        content = page.locator("#agenteComparaMessages").inner_text().lower()
        assert (
            "nao posso decidir" in content.replace("ã", "a").replace("ç", "c")
            or "decisao final" in content.replace("ã", "a").replace("ç", "c")
            or "não posso decidir" in content
            or "decisão final" in content
        )
        assert "contrate agora" not in content
        assert chat_calls["n"] == before + 4

        # Provider 503: mensagem correta, input reabilita, retry funciona
        phase["provider_error"] = True
        page.fill("#agenteComparaInput", "nova tentativa")
        page.click("#agenteComparaSend")
        page.wait_for_timeout(400)
        content = page.locator("#agenteComparaMessages").inner_text().lower()
        assert "inteligência artificial" in content or "inteligencia artificial" in content.replace("ê", "e")
        assert "verifique sua conexão" not in content
        assert not input_el.is_disabled()
        assert page.locator("#agenteComparaChatSuggestions").is_visible()

        phase["provider_error"] = False
        page.fill("#agenteComparaInput", "Olá")
        page.click("#agenteComparaSend")
        page.wait_for_timeout(400)
        content = page.locator("#agenteComparaMessages").inner_text().lower()
        assert "olá" in content or "ola" in content.replace("á", "a")

        # Reset / nova comparação: volta ao modo de orientação local (composer utilizável)
        page.evaluate(
            """() => {
              document.dispatchEvent(new CustomEvent('agente-compara:test-lock-chat'));
            }"""
        )
        page.wait_for_timeout(200)
        phase["value"] = "pre_ready"
        assert not input_el.is_disabled()
        assert not send_el.is_disabled()
        assert page.locator("#agenteComparaChatResponsibility").is_hidden()
        assert page.locator("#agenteComparaChatSuggestions").is_hidden()
        assert page.locator("#agenteComparaChatClearBtn").is_hidden()

        after_reset = chat_calls["n"]
        page.fill("#agenteComparaInput", "oi depois do reset")
        page.click("#agenteComparaSend")
        page.wait_for_function(
            """() => {
              const users = Array.from(document.querySelectorAll(
                '#agenteComparaMessages .agente-compara-chat-msg-user[data-chat-flow-guidance="true"]'
              ));
              const bots = Array.from(document.querySelectorAll(
                '#agenteComparaMessages [data-chat-blocked-guidance="true"] .agente-compara-chat-msg-inner'
              ));
              const hasUser = users.some((el) => (el.textContent || '').includes('oi depois do reset'));
              const hasBot = bots.some(
                (el) => (el.textContent || '').trim() === 'Faça o upload da tabela de frete.'
              );
              return hasUser && hasBot;
            }""",
            timeout=8000,
        )
        assert chat_calls["n"] == after_reset

        browser.close()
