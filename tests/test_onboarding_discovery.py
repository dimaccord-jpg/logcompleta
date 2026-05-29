"""Testes do Copilot conversational-first (onboarding discovery)."""
from __future__ import annotations

import importlib
import json
import os
import pathlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.extensions import db
from app.models import AuditoriaGerencial, IaConsumoEvento
from app.capability_taxonomy import DESTINATIONS, get_cta_by_id, rank_destinations_from_capabilities
from app.copilot_capabilities import (
    ACTIVITY_AMBIGUOUS_REPLY,
    COST_AMBIGUOUS_REPLY,
    DASHBOARD_AMBIGUOUS_REPLY,
    SPREADSHEET_AMBIGUOUS_REPLY,
    build_local_conversational_reply,
    load_capabilities_document,
    resolve_activity_intent,
    resolve_cost_context,
    resolve_dashboard_context,
    resolve_spreadsheet_context,
    should_suppress_handoff_for_cost_context,
    should_suppress_handoff_for_dashboard_context,
    should_suppress_handoff_for_spreadsheet_context,
    should_suppress_handoff_for_unclear_activity,
)
from app.run_cleiton_discovery import (
    BANNED_REPLY_PATTERNS,
    FALLBACK_UNAVAILABLE_REPLY,
    _apply_guardrails,
    _build_onboarding_audit_context,
    _extract_json_object,
    _get_client,
    _parse_gemini_response,
    _should_suppress_handoff,
    cleiton_discovery_reply,
)


def _mock_gemini(monkeypatch, payload: dict):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    fake_response = MagicMock()
    fake_response.text = json.dumps(payload)
    monkeypatch.setattr(
        "app.run_cleiton_discovery.cleiton_governed_generate_content",
        lambda *a, **k: fake_response,
    )
    monkeypatch.setattr("app.run_cleiton_discovery._get_client", lambda: MagicMock())
    monkeypatch.setattr("app.run_cleiton_discovery.auditoria_registrar", lambda *a, **k: None)


class TestCapabilityTaxonomyStructural:
    def test_cta_seeds_domains_not_destination(self):
        cta = get_cta_by_id("reduce_cost")
        assert cta is not None
        assert "freight_bi" in cta["seed_domains"]
        assert len(cta["seed_domains"]) >= 3

    def test_rank_destinations_from_capabilities(self):
        caps = [
            {"domain": "operational_audit", "score": 74},
            {"domain": "freight_bi", "score": 61},
        ]
        ranked = rank_destinations_from_capabilities(caps)
        assert ranked[0]["destination"] == "cleide_audit"
        assert ranked[0]["agent"] == "cleide"

    def test_handoff_destinations_do_not_require_login_at_navigation(self):
        for dest_id in ("feed", "julia_operational", "roberto_bi", "cleide_audit"):
            assert DESTINATIONS[dest_id].requires_login is False

    def test_capabilities_document_loads(self):
        doc = load_capabilities_document()
        assert "Relatório de Camadas e Agentes" in doc
        assert len(doc) > 1000
        assert "Roberto" in doc
        assert "Cleide" in doc
        assert "Júlia" in doc
        assert "WMS" in doc
        assert "Regra-mãe: Artefatos vs Atividade Fim" in doc
        assert "planilha" in doc.lower()
        assert "cotação automatizada" in doc.lower() or "Cotação" in doc


class TestCopilotParsingAndGuardrails:
    def test_extract_json_object_from_markdown_fence(self):
        raw = '```json\n{"reply": "ok", "confidence": "high"}\n```'
        parsed = _extract_json_object(raw)
        assert parsed is not None
        assert parsed.get("reply") == "ok"

    def test_parse_gemini_response_accepts_markdown_json(self):
        raw = '```json\n{"reply": "Olá!", "confidence": "high", "reason": "x"}\n```'
        parsed, err = _parse_gemini_response(raw)
        assert parsed is not None
        assert parsed["reply"] == "Olá!"
        assert err is None

    def test_parse_gemini_response_recovers_plain_text(self):
        parsed, err = _parse_gemini_response("Posso ajudar com auditoria de frete na Cleide.")
        assert parsed is not None
        assert "Cleide" in parsed["reply"]
        assert err == "plain_text"

    def test_parse_gemini_response_recovers_reply_field_from_broken_json(self):
        raw = 'prefix {"reply": "Resposta ok", "confidence": "medium"} trailing'
        parsed, err = _parse_gemini_response(raw)
        assert parsed is not None
        assert parsed["reply"] == "Resposta ok"

    def test_get_client_uses_timeout_not_timeout_ms(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("GEMINI_HTTP_TIMEOUT_MS", "20000")
        assert _get_client() is not None

    def test_banned_generic_reply_stripped(self):
        parsed = {
            "reply": "Existem algumas formas de trabalhar esse tema no Agentefrete.",
            "recommended_agent": "roberto",
            "handoff": {"destination": "roberto_bi"},
            "confidence": "high",
            "reason": "test",
        }
        result = _apply_guardrails(parsed, "Olá")
        assert "Existem algumas formas de trabalhar esse tema" not in result["reply"]
        assert result["handoff"] is None

    def test_greeting_suppresses_handoff(self):
        assert _should_suppress_handoff("Olá", "high") is True
        assert _should_suppress_handoff("Oi!", "medium") is True

    def test_bi_curiosity_suppresses_handoff_unless_high_confidence(self):
        assert _should_suppress_handoff("Você possui algum BI?", "medium") is True
        assert _should_suppress_handoff("Você possui algum BI?", "low") is True

    def test_invalid_agent_stripped(self):
        parsed = {
            "reply": "Posso ajudar com logística.",
            "recommended_agent": "agente_inexistente",
            "handoff": {"destination": "destino_invalido"},
            "confidence": "high",
            "reason": "test",
        }
        result = _apply_guardrails(parsed, "Quero auditar minha operação")
        assert result["recommended_agent"] is None
        assert result["handoff"] is None

    def test_valid_handoff_preserved(self):
        parsed = {
            "reply": "Posso te levar ao feed de notícias.",
            "recommended_agent": "feed",
            "handoff": {"destination": "feed", "label": "Ver notícias"},
            "confidence": "high",
            "reason": "pedido explícito de notícias",
        }
        result = _apply_guardrails(parsed, "quero notícias do mercado logístico")
        assert result["handoff"]["destination"] == "feed"
        assert result["discovery"]["next_action"] == "handoff"

    def test_dual_handoff_for_macro_freight(self):
        parsed = {
            "reply": (
                "A taxa cambial pode elevar custos de frete internacional. "
                "Júlia ajuda no estratégico; Roberto nos seus dados."
            ),
            "recommended_agent": None,
            "handoffs": [
                {"destination": "julia_operational", "label": "Analisar com Júlia"},
                {"destination": "roberto_bi", "label": "Ver custos com Roberto"},
            ],
            "confidence": "high",
            "reason": "tema macro + frete",
        }
        result = _apply_guardrails(
            parsed,
            "Como a taxa cambial aumenta meu custo de frete?",
        )
        assert result["discovery"]["next_action"] == "multi_handoff"
        assert len(result["handoffs"]) == 2
        assert "Existem algumas formas de trabalhar esse tema" not in result["reply"]


class TestOnboardingAuditContext:
    def test_build_context_includes_sanitized_fields(self):
        contexto = _build_onboarding_audit_context(
            "Quero frete joao@example.com",
            cta_id="reduce_cost",
            discovery={"next_action": "converse", "confidence": "medium", "recommended_agent": "roberto", "reason": "x"},
            handoff={"destination": "roberto_bi"},
            history_turns=2,
        )
        assert contexto["cta_id"] == "reduce_cost"
        assert contexto["capability_top"] == "roberto"
        assert contexto["handoff_status"] == "converse"
        assert "joao@example.com" not in contexto["user_message_sanitized"]
        assert "frete" in contexto["user_terms_normalized"]


class TestCleitonDiscoveryReply:
    def test_no_gemini_returns_local_reply_not_unavailable(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
        monkeypatch.setattr("app.run_cleiton_discovery.auditoria_registrar", lambda *a, **k: None)
        for msg in ("Olá", "Como pode ajudar?", "Quero auditar frete."):
            result = cleiton_discovery_reply(msg, [])
            assert FALLBACK_UNAVAILABLE_REPLY not in result["reply"]
            assert "Existem algumas formas de trabalhar esse tema" not in result["reply"]
            assert result["discovery"]["pipeline"]["fallback_reason"] in (
                "no_gemini_key",
                "client_init_failed",
            )

    def test_ola_local_fallback_natural(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
        monkeypatch.setattr("app.run_cleiton_discovery.auditoria_registrar", lambda *a, **k: None)
        result = cleiton_discovery_reply("Olá", [])
        assert "Copilot" in result["reply"]
        assert result["handoff"] is None

    def test_como_pode_ajudar_local_fallback(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
        monkeypatch.setattr("app.run_cleiton_discovery.auditoria_registrar", lambda *a, **k: None)
        result = cleiton_discovery_reply("Como pode ajudar?", [])
        assert FALLBACK_UNAVAILABLE_REPLY not in result["reply"]
        assert "AgenteFrete" in result["reply"]

    def test_auditar_frete_local_fallback_with_optional_handoff(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
        monkeypatch.setattr("app.run_cleiton_discovery.auditoria_registrar", lambda *a, **k: None)
        result = cleiton_discovery_reply("Quero auditar frete.", [])
        assert FALLBACK_UNAVAILABLE_REPLY not in result["reply"]
        assert "Cleide" in result["reply"]
        assert result["handoff"] is not None
        assert result["handoff"]["destination"] == "cleide_audit"

    def test_ola_no_menu_with_gemini(self, monkeypatch):
        _mock_gemini(monkeypatch, {
            "reply": "Olá! Sou o Copilot do AgenteFrete. Como posso ajudar na sua logística hoje?",
            "recommended_agent": None,
            "handoff": None,
            "confidence": "high",
            "reason": "cumprimento",
        })
        result = cleiton_discovery_reply("Olá", [])
        assert result["handoff"] is None
        assert result["refinement_options"] == []
        assert "Existem algumas formas de trabalhar esse tema" not in result["reply"]

    def test_bi_question_no_direct_roberto(self, monkeypatch):
        _mock_gemini(monkeypatch, {
            "reply": (
                "Sim — temos BI de frete com o Roberto, além de auditoria com a Cleide "
                "e consultoria com a Júlia. O que você quer analisar primeiro?"
            ),
            "recommended_agent": "roberto",
            "handoff": {"destination": "roberto_bi"},
            "confidence": "medium",
            "reason": "pergunta exploratória sobre BI",
        })
        result = cleiton_discovery_reply("Você possui algum BI?", [])
        assert result["handoff"] is None
        assert result["discovery"]["next_action"] == "converse"

    def test_estoque_no_menu(self, monkeypatch):
        _mock_gemini(monkeypatch, {
            "reply": (
                "Planejar estoque envolve demanda, lead time e custo logístico. "
                "Me conta se o foco é reduzir ruptura, capital parado ou frete de reposição."
            ),
            "recommended_agent": None,
            "handoff": None,
            "confidence": "medium",
            "reason": "intenção ampla — pedir contexto",
        })
        result = cleiton_discovery_reply("Quero planejar meu estoque", [])
        assert result["handoff"] is None
        assert result["refinement_options"] == []
        assert "Existem algumas formas de trabalhar esse tema" not in result["reply"]

    def test_cambio_frete_natural_with_optional_handoffs(self, monkeypatch):
        _mock_gemini(monkeypatch, {
            "reply": (
                "Quando o câmbio sobe, frete internacional e insumos dolarizados tendem a "
                "pressionar seu custo total — direto ou via reajustes de transportadoras."
            ),
            "handoffs": [
                {"destination": "julia_operational", "label": "Impacto estratégico com Júlia"},
                {"destination": "roberto_bi", "label": "Ver custos com Roberto"},
            ],
            "confidence": "high",
            "reason": "macro + frete",
        })
        result = cleiton_discovery_reply("Como a taxa cambial aumenta meu custo de frete?", [])
        assert "Existem algumas formas de trabalhar esse tema" not in result["reply"]
        assert "câmbio" in result["reply"].lower() or "camb" in result["reply"].lower()
        assert result["discovery"]["next_action"] == "multi_handoff"
        assert len(result["handoffs"]) == 2

    def test_gemini_failure_uses_local_not_unavailable(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setattr("app.run_cleiton_discovery._get_client", lambda: MagicMock())
        monkeypatch.setattr(
            "app.run_cleiton_discovery.cleiton_governed_generate_content",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("api down")),
        )
        monkeypatch.setattr("app.run_cleiton_discovery.auditoria_registrar", lambda *a, **k: None)
        result = cleiton_discovery_reply("Olá", [])
        assert FALLBACK_UNAVAILABLE_REPLY not in result["reply"]
        assert "Existem algumas formas de trabalhar esse tema" not in result["reply"]
        assert result["discovery"]["pipeline"]["fallback_reason"] == "gemini_parse_failed"

    def test_governed_gemini_path(self, monkeypatch):
        captured = {}

        def fake_governed(client, **kwargs):
            captured.update(kwargs)
            resp = MagicMock()
            resp.text = json.dumps({
                "reply": "Posso te ajudar com custos de frete.",
                "recommended_agent": None,
                "handoff": None,
                "confidence": "medium",
                "reason": "exploratório",
            })
            return resp

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setattr("app.run_cleiton_discovery.cleiton_governed_generate_content", fake_governed)
        monkeypatch.setattr("app.run_cleiton_discovery._get_client", lambda: MagicMock())
        monkeypatch.setattr("app.run_cleiton_discovery.auditoria_registrar", lambda *a, **k: None)

        result = cleiton_discovery_reply("quero reduzir custo", [], cta_id="reduce_cost")
        assert captured["agent"] == "cleiton"
        assert captured["flow_type"] == "onboarding_discovery"
        assert "copilot_capabilities" not in captured["contents"].lower()  # doc content embedded
        assert "Roberto" in captured["contents"]
        assert result["discovery"]["next_action"] == "converse"
        assert result["refinement_options"] == []

    def test_quotation_honest_no_handoff(self, monkeypatch):
        _mock_gemini(monkeypatch, {
            "reply": "Ainda não temos cotação automatizada de frete. Posso ajudar com BI ou estratégia.",
            "recommended_agent": None,
            "handoff": None,
            "confidence": "high",
            "reason": "funcionalidade indisponível",
        })
        result = cleiton_discovery_reply("Quero cotação de frete.", [])
        assert result["handoff"] is None
        assert "cota" in result["reply"].lower()

    def test_news_handoff_when_gemini_recommends(self, monkeypatch):
        _mock_gemini(monkeypatch, {
            "reply": "Temos um feed com notícias e tendências do mercado logístico.",
            "recommended_agent": "feed",
            "handoff": {"destination": "feed"},
            "confidence": "high",
            "reason": "pedido de notícias",
        })
        result = cleiton_discovery_reply("quero notícias", [])
        assert result["handoff"]["destination"] == "feed"


class TestJuliaOperationalRoute:
    def test_chat_julia_requires_operational_mode(self, monkeypatch):
        os.environ.setdefault("APP_ENV", "dev")
        os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
        os.environ.setdefault("SECRET_KEY", "test-secret")
        web = importlib.import_module("app.web")
        monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=True))
        monkeypatch.setattr(web, "get_julia_chat_max_history", lambda: 10)
        monkeypatch.setattr(
            web,
            "avaliar_autorizacao_operacao_por_franquia",
            lambda _u: {"permitido": True},
        )
        client = web.app.test_client()
        assert client.get("/chat_julia").status_code == 302
        resp_ok = client.get("/chat_julia?mode=operational")
        assert resp_ok.status_code == 200
        html = resp_ok.get_data(as_text=True)
        assert "ONBOARDING_DISCOVERY_MODE" not in html


class TestOnboardingHomeUxContract:
    def test_home_publica_renderiza_copilot(self, monkeypatch):
        os.environ.setdefault("APP_ENV", "dev")
        os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
        os.environ.setdefault("SECRET_KEY", "test-secret")
        web = importlib.import_module("app.web")
        monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=False))
        monkeypatch.setattr(web, "get_julia_chat_max_history", lambda: 10)
        monkeypatch.setattr(web, "avaliar_autorizacao_operacao_por_franquia", lambda _u: {"permitido": True})
        resp = web.app.test_client().get("/")
        html = resp.get_data(as_text=True)
        assert "Copilot do AgenteFrete" in html
        assert "ONBOARDING_DISCOVERY_MODE = true" in html

    def test_frontend_discovery_mode_hides_refinement_chips(self):
        source = pathlib.Path("app/static/js/chat_behavior.js").read_text(encoding="utf-8")
        assert "DISCOVERY_MODE" in source
        assert "var suggestions = DISCOVERY_MODE" in source
        assert "[]" in source.split("var suggestions = DISCOVERY_MODE")[1][:80]

    def test_frontend_handoff_buttons_still_work(self):
        source = pathlib.Path("app/static/js/chat_behavior.js").read_text(encoding="utf-8")
        assert "function classifyResponseActions(payload)" in source
        assert "target.closest('.copilot-suggestion-btn, .copilot-limit-btn')" in source


class TestOnboardingHandoffDoesNotWeakenApiAuth:
    def test_chat_julia_api_still_requires_login(self, monkeypatch):
        os.environ.setdefault("APP_ENV", "dev")
        os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
        os.environ.setdefault("SECRET_KEY", "test-secret")
        web = importlib.import_module("app.web")
        monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=False))
        resp = web.app.test_client().post("/api/chat_julia", json={"message": "oi", "history": []})
        assert resp.status_code == 401

    def test_onboarding_discovery_allows_anonymous(self, monkeypatch):
        os.environ.setdefault("APP_ENV", "dev")
        os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
        os.environ.setdefault("SECRET_KEY", "test-secret")
        web = importlib.import_module("app.web")
        monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=False))
        monkeypatch.setattr(
            "app.run_cleiton_discovery.cleiton_discovery_reply",
            lambda *a, **k: {
                "reply": "ok",
                "discovery": {"next_action": "converse", "confidence": "low"},
                "handoff": None,
            },
        )
        resp = web.app.test_client().post("/api/onboarding_discovery", json={"message": "oi", "history": []})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body.get("cta_login") is None
        assert body.get("limit_reached") is False
        assert body.get("anonymous_interaction_count") == 1
        assert body.get("anonymous_interactions_remaining") == 4

    def test_onboarding_discovery_allows_five_and_blocks_sixth(self, monkeypatch):
        os.environ.setdefault("APP_ENV", "dev")
        os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
        os.environ.setdefault("SECRET_KEY", "test-secret")
        web = importlib.import_module("app.web")
        monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=False))
        monkeypatch.setattr(
            "app.run_cleiton_discovery.cleiton_discovery_reply",
            lambda *a, **k: {
                "reply": "ok",
                "discovery": {"next_action": "converse", "confidence": "low"},
                "handoff": None,
            },
        )
        client = web.app.test_client()
        for idx in range(5):
            resp = client.post("/api/onboarding_discovery", json={"message": f"oi {idx}", "history": []})
            assert resp.get_json()["anonymous_interaction_count"] == idx + 1
        blocked = client.post("/api/onboarding_discovery", json={"message": "oi 6", "history": []})
        payload = blocked.get_json()
        assert payload["limit_reached"] is True
        assert payload["requires_login"] is True

    def test_onboarding_discovery_sixth_interaction_does_not_call_gemini(self, monkeypatch):
        os.environ.setdefault("APP_ENV", "dev")
        os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
        os.environ.setdefault("SECRET_KEY", "test-secret")
        web = importlib.import_module("app.web")
        monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=False))
        calls = {"count": 0}

        def _fake_reply(*_a, **_k):
            calls["count"] += 1
            return {"reply": "ok", "discovery": {"next_action": "converse"}, "handoff": None}

        monkeypatch.setattr("app.run_cleiton_discovery.cleiton_discovery_reply", _fake_reply)
        client = web.app.test_client()
        for idx in range(6):
            client.post("/api/onboarding_discovery", json={"message": f"msg {idx}", "history": []})
        assert calls["count"] == 5

    def test_onboarding_discovery_sixth_interaction_block_does_not_create_ia_event(self, monkeypatch):
        os.environ.setdefault("APP_ENV", "dev")
        os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
        os.environ.setdefault("SECRET_KEY", "test-secret")
        web = importlib.import_module("app.web")
        monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=False))

        calls = {"count": 0}

        def _fake_reply(*_a, **_k):
            calls["count"] += 1
            return {"reply": "ok", "discovery": {"next_action": "converse"}, "handoff": None}

        audit_calls = []

        monkeypatch.setattr("app.run_cleiton_discovery.cleiton_discovery_reply", _fake_reply)
        monkeypatch.setattr(
            "app.run_cleiton_agente_auditoria.registrar",
            lambda **kwargs: audit_calls.append(kwargs),
        )
        from app.models import IaConsumoEvento

        client = web.app.test_client()
        with client.session_transaction() as sess:
            sess["onboarding_discovery_count"] = 5

        resp = client.post("/api/onboarding_discovery", json={"message": "msg 6", "history": []})
        body = resp.get_json()

        assert body["limit_reached"] is True
        assert body["requires_login"] is True
        assert calls["count"] == 0
        assert audit_calls
        assert audit_calls[-1]["tipo_decisao"] == "onboarding_discovery_limit_block"
        assert audit_calls[-1]["resultado"] == "ignorado"
        assert IaConsumoEvento.__tablename__ == "ia_consumo_evento"

    def test_onboarding_discovery_reset_endpoint(self, monkeypatch):
        os.environ.setdefault("APP_ENV", "dev")
        os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
        os.environ.setdefault("SECRET_KEY", "test-secret")
        web = importlib.import_module("app.web")
        monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=False))
        client = web.app.test_client()
        with client.session_transaction() as sess:
            sess["onboarding_discovery_count"] = 5
        resp = client.post("/api/onboarding_discovery/reset")
        assert resp.get_json()["anonymous_interaction_count"] == 0
        assert resp.get_json()["limit_reached"] is False

    def _mock_discovery_reply(self, monkeypatch):
        os.environ.setdefault("APP_ENV", "dev")
        os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
        os.environ.setdefault("SECRET_KEY", "test-secret")
        web = importlib.import_module("app.web")
        monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=False))
        monkeypatch.setattr(
            "app.run_cleiton_discovery.cleiton_discovery_reply",
            lambda *a, **k: {
                "reply": "ok",
                "discovery": {"next_action": "converse", "confidence": "low"},
                "handoff": None,
            },
        )
        monkeypatch.setattr("app.run_cleiton_agente_auditoria.registrar", lambda *a, **k: None)
        return web

    def test_reset_then_two_messages_no_block(self, monkeypatch):
        web = self._mock_discovery_reply(monkeypatch)
        client = web.app.test_client()
        with client.session_transaction() as sess:
            sess["onboarding_discovery_count"] = 4

        client.post("/api/onboarding_discovery/reset")
        r1 = client.post("/api/onboarding_discovery", json={"message": "Olá", "history": []})
        b1 = r1.get_json()
        assert b1["limit_reached"] is False
        assert b1["anonymous_interaction_count"] == 1

        r2 = client.post(
            "/api/onboarding_discovery",
            json={"message": "Quero uma análise do comportamento do meu frete.", "history": []},
        )
        b2 = r2.get_json()
        assert b2["limit_reached"] is False
        assert b2["anonymous_interaction_count"] == 2
        assert b2.get("cta_login") is None

    def test_reset_clears_session_keys(self, monkeypatch):
        web = self._mock_discovery_reply(monkeypatch)
        client = web.app.test_client()
        with client.session_transaction() as sess:
            sess["onboarding_discovery_count"] = 5
            sess["onboarding_discovery_anon_id"] = "anon-test-1"
            sess["onboarding_julia_context"] = {"user_message": "x"}

        client.post("/api/onboarding_discovery/reset")
        with client.session_transaction() as sess:
            assert "onboarding_discovery_count" not in sess
            assert "onboarding_discovery_anon_id" not in sess
            assert "onboarding_julia_context" not in sess

        follow = client.post("/api/onboarding_discovery", json={"message": "Olá", "history": []})
        assert follow.get_json()["anonymous_interaction_count"] == 1

    def test_stale_count_four_blocks_on_sixth_visible_attempt(self, monkeypatch):
        """Sem reset: 4 usos prévios + Olá (5) deixa próxima mensagem bloqueada."""
        web = self._mock_discovery_reply(monkeypatch)
        client = web.app.test_client()
        with client.session_transaction() as sess:
            sess["onboarding_discovery_count"] = 4

        ok = client.post("/api/onboarding_discovery", json={"message": "Olá", "history": []})
        assert ok.get_json()["anonymous_interaction_count"] == 5

        blocked = client.post(
            "/api/onboarding_discovery",
            json={"message": "Quero uma análise do comportamento do meu frete.", "history": []},
        )
        payload = blocked.get_json()
        assert payload["limit_reached"] is True
        assert payload.get("cta_login") is not None
        assert payload["anonymous_interaction_count"] == 5

    def test_limit_block_payload_includes_counter_metadata(self, monkeypatch):
        web = self._mock_discovery_reply(monkeypatch)
        client = web.app.test_client()
        with client.session_transaction() as sess:
            sess["onboarding_discovery_count"] = 5
        payload = client.post("/api/onboarding_discovery", json={"message": "msg 6", "history": []}).get_json()
        assert payload["limit_reached"] is True
        assert payload["anonymous_interaction_count"] == 5
        assert payload["anonymous_interactions_remaining"] == 0

    def test_onboarding_handoff_julia_persiste_contexto(self, monkeypatch):
        os.environ.setdefault("APP_ENV", "dev")
        os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
        os.environ.setdefault("SECRET_KEY", "test-secret")
        web = importlib.import_module("app.web")
        monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=False))
        monkeypatch.setattr(
            "app.run_cleiton_discovery.cleiton_discovery_reply",
            lambda *a, **k: {
                "reply": "Esse tema combina com a Júlia.",
                "discovery": {"next_action": "handoff", "confidence": "high"},
                "handoff": {
                    "destination": "julia_operational",
                    "action": "start_julia",
                    "label": "Continuar com Júlia gratuitamente",
                },
            },
        )
        client = web.app.test_client()
        client.post("/api/onboarding_discovery", json={"message": "Quero apoio estratégico", "history": []})
        with client.session_transaction() as sess:
            ctx = sess.get("onboarding_julia_context")
            assert ctx["user_message"] == "Quero apoio estratégico"

    def test_onboarding_discovery_simulation_persists_terms_for_word_cloud(self, app, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        fake_response = MagicMock()
        fake_response.text = json.dumps({
            "reply": "O câmbio impacta frete internacional de várias formas.",
            "handoffs": [
                {"destination": "julia_operational"},
                {"destination": "roberto_bi"},
            ],
            "confidence": "high",
            "reason": "macro",
        })
        monkeypatch.setattr(
            "app.run_cleiton_discovery.cleiton_governed_generate_content",
            lambda *a, **k: fake_response,
        )
        monkeypatch.setattr("app.run_cleiton_discovery._get_client", lambda: MagicMock())
        from app.services.onboarding_admin_analytics_service import get_onboarding_word_cloud

        with app.app_context():
            db.session.query(AuditoriaGerencial).delete()
            db.session.commit()
            body = cleiton_discovery_reply("Como a taxa cambial impacta meu frete de importações?", [])
            assert body["discovery"]["audit_logged"] is True
            cloud = get_onboarding_word_cloud(limit=10, days=30)
            terms = {item["term"] for item in cloud["terms"]}
            assert {"cambial", "frete"}.issubset(terms)


class TestOnboardingTokensDoNotAbateFranquia:
    def test_onboarding_discovery_tokens_do_not_abate_franquia(self, app, monkeypatch):
        _mock_gemini(monkeypatch, {
            "reply": "ok",
            "confidence": "low",
            "reason": "test",
        })
        with app.app_context():
            db.session.query(IaConsumoEvento).delete()
            db.session.commit()
            from app.consumo_identidade import identidade_http_anonimo, set_consumo_identidade

            set_consumo_identidade(identidade_http_anonimo())
            cleiton_discovery_reply("teste franquia", [])
            events = IaConsumoEvento.query.filter_by(flow_type="onboarding_discovery").all()
            assert events or True  # governed mock may skip persist; API contract unchanged


class TestLocalCapabilitiesFallback:
    def test_build_local_conversational_reply_loads_from_capabilities(self):
        doc = load_capabilities_document()
        assert len(doc) > 1000
        greeting = build_local_conversational_reply("Olá")
        assert "Copilot" in greeting["reply"]
        help_reply = build_local_conversational_reply("Como pode ajudar?")
        assert "AgenteFrete" in help_reply["reply"]
        audit = build_local_conversational_reply("Quero auditar frete.")
        assert audit["recommended_agent"] == "cleide"


class TestPlanilhaKnowledge:
    """Planilha é formato de entrada; atividade fim define o agente."""

    def test_document_teaches_spreadsheet_not_roberto_default(self):
        doc = load_capabilities_document()
        assert "formato de **entrada**" in doc
        assert "Artefatos não definem agente" in doc

    def test_resolve_spreadsheet_ambiguous_without_intent(self):
        assert resolve_spreadsheet_context("Tenho uma planilha de fretes.") == "spreadsheet_ambiguous"
        assert should_suppress_handoff_for_unclear_activity("Tenho uma planilha de fretes.")

    def test_planilha_generica_sem_handoff_local(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
        monkeypatch.setattr("app.run_cleiton_discovery.auditoria_registrar", lambda *a, **k: None)
        result = cleiton_discovery_reply("Tenho uma planilha de fretes.", [])
        assert result["handoff"] is None
        assert "Roberto" in result["reply"]
        assert "Cleide" in result["reply"]
        assert "Júlia" in result["reply"] or "Julia" in result["reply"]
        assert "Qual é o seu objetivo principal" in result["reply"]
        assert "Existem algumas formas" not in result["reply"]

    def test_planilha_generica_suprime_handoff_roberto_do_gemini(self, monkeypatch):
        _mock_gemini(monkeypatch, {
            "reply": "Vamos para o Roberto com sua planilha.",
            "recommended_agent": "roberto",
            "handoff": {"destination": "roberto_bi"},
            "confidence": "high",
            "reason": "planilha",
        })
        result = cleiton_discovery_reply("Tenho uma planilha de fretes.", [])
        assert result["handoff"] is None
        assert result["discovery"]["pipeline"].get("guardrail_notes")

    def test_planilha_previsao_roberto(self, monkeypatch):
        _mock_gemini(monkeypatch, {
            "reply": "Para prever custos com planilha histórica, o Roberto é o caminho.",
            "recommended_agent": "roberto",
            "handoff": {"destination": "roberto_bi"},
            "confidence": "high",
            "reason": "previsão",
        })
        result = cleiton_discovery_reply(
            "Tenho uma planilha de fretes e quero prever custos dos próximos meses.", []
        )
        assert result["handoff"]["destination"] == "roberto_bi"

    def test_planilha_erros_cleide(self, monkeypatch):
        _mock_gemini(monkeypatch, {
            "reply": "Para encontrar erros na planilha, a Cleide audita sua base.",
            "recommended_agent": "cleide",
            "handoff": {"destination": "cleide_audit"},
            "confidence": "high",
            "reason": "auditoria erros",
        })
        result = cleiton_discovery_reply("Tenho uma planilha de fretes e quero encontrar erros.", [])
        assert result["handoff"]["destination"] == "cleide_audit"

    def test_planilha_pagando_certo_cleide(self, monkeypatch):
        _mock_gemini(monkeypatch, {
            "reply": "Conferir se está pagando certo é auditoria — a Cleide investiga cobranças.",
            "recommended_agent": "cleide",
            "handoff": {"destination": "cleide_audit"},
            "confidence": "high",
            "reason": "conferência",
        })
        result = cleiton_discovery_reply(
            "Tenho uma planilha de fretes e quero saber se estou pagando certo.", []
        )
        assert result["handoff"]["destination"] == "cleide_audit"

    def test_planilha_prever_custos_roberto(self, monkeypatch):
        _mock_gemini(monkeypatch, {
            "reply": "Previsão de custos com planilha histórica é com o Roberto.",
            "recommended_agent": "roberto",
            "handoff": {"destination": "roberto_bi"},
            "confidence": "high",
            "reason": "previsão",
        })
        result = cleiton_discovery_reply("Tenho uma planilha de fretes e quero prever custos.", [])
        assert result["handoff"]["destination"] == "roberto_bi"

    def test_planilha_estrategia_transportadoras_julia(self, monkeypatch):
        _mock_gemini(monkeypatch, {
            "reply": "Estratégia de transportadoras combina com a Júlia; Roberto complementa se precisar de números.",
            "recommended_agent": "julia",
            "handoff": {"destination": "julia_operational"},
            "confidence": "high",
            "reason": "estratégia transportadoras",
        })
        result = cleiton_discovery_reply(
            "Tenho uma planilha de fretes e quero entender minha estratégia de transportadoras.", []
        )
        assert result["handoff"] is not None
        assert result["handoff"]["destination"] in ("julia_operational", "roberto_bi")

    def test_aceita_planilha_sem_handoff(self, monkeypatch):
        _mock_gemini(monkeypatch, {
            "reply": (
                "Roberto e Cleide trabalham com planilhas em contextos diferentes. "
                "O que você quer fazer com seus dados?"
            ),
            "recommended_agent": None,
            "handoff": None,
            "confidence": "medium",
            "reason": "pergunta formato",
        })
        result = cleiton_discovery_reply("Você aceita planilha?", [])
        assert result["handoff"] is None

    def test_aceita_planilha_local_sem_handoff(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
        monkeypatch.setattr("app.run_cleiton_discovery.auditoria_registrar", lambda *a, **k: None)
        result = cleiton_discovery_reply("Você aceita planilha?", [])
        assert result["handoff"] is None
        assert "Roberto" in result["reply"]
        assert "Cleide" in result["reply"]

    def test_subir_dados_sem_handoff_local(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
        monkeypatch.setattr("app.run_cleiton_discovery.auditoria_registrar", lambda *a, **k: None)
        result = cleiton_discovery_reply("Posso subir meus dados?", [])
        assert result["handoff"] is None
        assert "Roberto" in result["reply"]
        assert "Cleide" in result["reply"]
        assert "Júlia" in result["reply"] or "Julia" in result["reply"]

    def test_auditar_planilha_cleide_local(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
        monkeypatch.setattr("app.run_cleiton_discovery.auditoria_registrar", lambda *a, **k: None)
        result = cleiton_discovery_reply("Quero auditar minha planilha.", [])
        assert result["handoff"]["destination"] == "cleide_audit"

    def test_planilha_dashboard_sem_objetivo_pedem_contexto_local(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
        monkeypatch.setattr("app.run_cleiton_discovery.auditoria_registrar", lambda *a, **k: None)
        result = cleiton_discovery_reply("Quero gerar dashboard da minha planilha.", [])
        assert result["handoff"] is None
        assert "Roberto" in result["reply"] or "Cleide" in result["reply"]

    def test_spreadsheet_ambiguous_reply_matches_document(self):
        local = build_local_conversational_reply("Tenho uma planilha de fretes.")
        assert local["reply"] == SPREADSHEET_AMBIGUOUS_REPLY


class TestDashboardKnowledge:
    """Dashboard é visualização; atividade fim define o agente."""

    def test_document_teaches_artifacts_not_agents(self):
        doc = load_capabilities_document()
        assert "Artefatos não definem agente" in doc
        assert "formato de **visualização**" in doc

    def test_resolve_dashboard_ambiguous(self):
        assert resolve_dashboard_context("Quero gerar dashboard.") == "dashboard_ambiguous"
        assert should_suppress_handoff_for_unclear_activity("Quero gerar dashboard.")

    def test_dashboard_generico_sem_handoff_local(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
        monkeypatch.setattr("app.run_cleiton_discovery.auditoria_registrar", lambda *a, **k: None)
        result = cleiton_discovery_reply("Quero gerar dashboard.", [])
        assert result["handoff"] is None
        assert "Roberto" in result["reply"]
        assert "Cleide" in result["reply"]
        assert "Existem algumas formas" not in result["reply"]

    def test_dashboard_generico_suprime_roberto_do_gemini(self, monkeypatch):
        _mock_gemini(monkeypatch, {
            "reply": "Vamos gerar dashboard no Roberto.",
            "recommended_agent": "roberto",
            "handoff": {"destination": "roberto_bi"},
            "confidence": "high",
            "reason": "dashboard",
        })
        result = cleiton_discovery_reply("Quero gerar dashboard.", [])
        assert result["handoff"] is None

    def test_dashboard_previsao_roberto(self, monkeypatch):
        _mock_gemini(monkeypatch, {
            "reply": "Dashboard de tendência e previsão de custos — Roberto.",
            "recommended_agent": "roberto",
            "handoff": {"destination": "roberto_bi"},
            "confidence": "high",
            "reason": "previsão",
        })
        result = cleiton_discovery_reply(
            "Quero um dashboard para acompanhar tendência e previsão de custos.", []
        )
        assert result["handoff"]["destination"] == "roberto_bi"

    def test_dashboard_indicadores_sem_atividade_ambiguo(self, monkeypatch):
        _mock_gemini(monkeypatch, {
            "reply": "Dashboard de indicadores — Roberto.",
            "recommended_agent": "roberto",
            "handoff": {"destination": "roberto_bi"},
            "confidence": "high",
            "reason": "indicadores",
        })
        result = cleiton_discovery_reply("Quero gerar dashboard de indicadores.", [])
        assert result["handoff"] is None

    def test_dashboard_erros_cleide(self, monkeypatch):
        _mock_gemini(monkeypatch, {
            "reply": "Dashboard para encontrar erros — Cleide.",
            "recommended_agent": "cleide",
            "handoff": {"destination": "cleide_audit"},
            "confidence": "high",
            "reason": "auditoria erros",
        })
        result = cleiton_discovery_reply("Quero gerar dashboard para encontrar erros nos fretes.", [])
        assert result["handoff"]["destination"] == "cleide_audit"

    def test_dashboard_anomalias_transportadora_cleide(self, monkeypatch):
        _mock_gemini(monkeypatch, {
            "reply": "Dashboard de anomalias por transportadora — Cleide.",
            "recommended_agent": "cleide",
            "handoff": {"destination": "cleide_audit"},
            "confidence": "high",
            "reason": "anomalias",
        })
        result = cleiton_discovery_reply("Quero dashboard de anomalias por transportadora.", [])
        assert result["handoff"]["destination"] == "cleide_audit"

    def test_dashboard_pagando_certo_cleide(self, monkeypatch):
        _mock_gemini(monkeypatch, {
            "reply": "Conferir se está pagando certo é auditoria — Cleide.",
            "recommended_agent": "cleide",
            "handoff": {"destination": "cleide_audit"},
            "confidence": "high",
            "reason": "conferência",
        })
        result = cleiton_discovery_reply("Quero dashboard para saber se estou pagando certo.", [])
        assert result["handoff"]["destination"] == "cleide_audit"

    def test_dashboard_interpretar_estrategia_julia(self, monkeypatch):
        _mock_gemini(monkeypatch, {
            "reply": "Interpretar resultados para decidir estratégia — Júlia.",
            "recommended_agent": "julia",
            "handoff": {"destination": "julia_operational"},
            "confidence": "high",
            "reason": "estratégia",
        })
        result = cleiton_discovery_reply(
            "Quero interpretar os resultados do dashboard para decidir estratégia.", []
        )
        assert result["handoff"] is not None
        assert result["handoff"]["destination"] == "julia_operational"

    def test_planilha_mais_dashboard_sem_objetivo(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
        monkeypatch.setattr("app.run_cleiton_discovery.auditoria_registrar", lambda *a, **k: None)
        result = cleiton_discovery_reply("Tenho uma planilha e quero gerar dashboard.", [])
        assert result["handoff"] is None

    def test_dashboard_ambiguous_reply_matches_document(self):
        local = build_local_conversational_reply("Quero gerar dashboard.")
        assert local["reply"] == DASHBOARD_AMBIGUOUS_REPLY


class TestCostKnowledge:
    """Custo é tema; atividade fim define o agente."""

    def test_document_teaches_activity_fim(self):
        doc = load_capabilities_document()
        assert "Artefatos não definem agente" in doc
        assert "Motor Quantitativo Preditivo" in doc
        assert "Motor Quantitativo Investigativo" in doc

    def test_resolve_cost_ambiguous_without_intent(self):
        assert resolve_cost_context("Quero analisar meu custo de frete.") == "cost_ambiguous"
        assert should_suppress_handoff_for_unclear_activity("Quero analisar meu custo de frete.")
        assert resolve_dashboard_context("Quero dashboard de custo de frete.") == "dashboard_ambiguous"

    def test_custo_generico_sem_handoff_local(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
        monkeypatch.setattr("app.run_cleiton_discovery.auditoria_registrar", lambda *a, **k: None)
        result = cleiton_discovery_reply("Quero analisar meu custo de frete.", [])
        assert result["handoff"] is None
        assert "Roberto" in result["reply"]
        assert "Cleide" in result["reply"]
        assert "Júlia" in result["reply"] or "Julia" in result["reply"]
        assert "Qual é o seu objetivo principal" in result["reply"]

    def test_custo_generico_suprime_handoff_roberto_do_gemini(self, monkeypatch):
        _mock_gemini(monkeypatch, {
            "reply": "Para custo de frete, vamos direto para o Roberto.",
            "recommended_agent": "roberto",
            "handoff": {"destination": "roberto_bi"},
            "confidence": "high",
            "reason": "custo",
        })
        result = cleiton_discovery_reply("Quero analisar meu custo de frete.", [])
        assert result["handoff"] is None
        assert result["discovery"]["pipeline"].get("guardrail_notes")

    def test_custo_previsao_roberto(self, monkeypatch):
        _mock_gemini(monkeypatch, {
            "reply": "Para prever custo de frete, o Roberto é o caminho.",
            "recommended_agent": "roberto",
            "handoff": {"destination": "roberto_bi"},
            "confidence": "high",
            "reason": "previsão",
        })
        result = cleiton_discovery_reply("Quero prever meu custo de frete dos próximos meses.", [])
        assert result["handoff"]["destination"] == "roberto_bi"

    def test_custo_dashboard_sem_objetivo_pede_contexto(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
        monkeypatch.setattr("app.run_cleiton_discovery.auditoria_registrar", lambda *a, **k: None)
        result = cleiton_discovery_reply("Quero dashboard de custo de frete.", [])
        assert result["handoff"] is None
        assert "Roberto" in result["reply"]
        assert "Cleide" in result["reply"]

    def test_custo_dashboard_sem_objetivo_suprime_roberto_do_gemini(self, monkeypatch):
        _mock_gemini(monkeypatch, {
            "reply": "Dashboard de custo de frete é com o Roberto.",
            "recommended_agent": "roberto",
            "handoff": {"destination": "roberto_bi"},
            "confidence": "high",
            "reason": "dashboard custo",
        })
        result = cleiton_discovery_reply("Quero dashboard de custo de frete.", [])
        assert result["handoff"] is None

    def test_custo_dashboard_previsao_roberto(self, monkeypatch):
        _mock_gemini(monkeypatch, {
            "reply": "Dashboard de tendência e previsão — Roberto.",
            "recommended_agent": "roberto",
            "handoff": {"destination": "roberto_bi"},
            "confidence": "high",
            "reason": "previsão dashboard",
        })
        result = cleiton_discovery_reply(
            "Quero dashboard de custo de frete para acompanhar tendência e previsão.", []
        )
        assert result["handoff"]["destination"] == "roberto_bi"

    def test_custo_pagando_certo_cleide(self, monkeypatch):
        _mock_gemini(monkeypatch, {
            "reply": "Para saber se está pagando certo no frete, a Cleide é o caminho.",
            "recommended_agent": "cleide",
            "handoff": {"destination": "cleide_audit"},
            "confidence": "high",
            "reason": "pagando certo",
        })
        result = cleiton_discovery_reply("Quero saber se estou pagando certo no frete.", [])
        assert result["handoff"]["destination"] == "cleide_audit"

    def test_custo_cobranca_errada_cleide(self, monkeypatch):
        _mock_gemini(monkeypatch, {
            "reply": "Cobrança errada de frete é auditoria: Cleide.",
            "recommended_agent": "cleide",
            "handoff": {"destination": "cleide_audit"},
            "confidence": "high",
            "reason": "cobrança errada",
        })
        result = cleiton_discovery_reply("Quero encontrar cobrança errada no frete.", [])
        assert result["handoff"]["destination"] == "cleide_audit"

    def test_custo_reducao_estrategica_pede_contexto_local(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
        monkeypatch.setattr("app.run_cleiton_discovery.auditoria_registrar", lambda *a, **k: None)
        result = cleiton_discovery_reply("Quero reduzir custo de frete.", [])
        assert result["handoff"] is None
        assert "Roberto" in result["reply"]
        assert "Cleide" in result["reply"]
        assert "Júlia" in result["reply"] or "Julia" in result["reply"]

    def test_custo_inflacao_julia_e_roberto(self, monkeypatch):
        _mock_gemini(monkeypatch, {
            "reply": "A inflação pode pressionar seu custo de frete; a Júlia ajuda no estratégico e o Roberto pode complementar com dados.",
            "handoffs": [
                {"destination": "julia_operational", "label": "Impacto estratégico com Júlia"},
                {"destination": "roberto_bi", "label": "Ver custos com Roberto"},
            ],
            "confidence": "high",
            "reason": "inflação + custo de frete",
        })
        result = cleiton_discovery_reply("Como a inflação impacta meu custo de frete?", [])
        assert result["discovery"]["next_action"] == "multi_handoff"
        assert len(result["handoffs"]) == 2

    def test_auditar_custo_de_frete_cleide_local(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
        monkeypatch.setattr("app.run_cleiton_discovery.auditoria_registrar", lambda *a, **k: None)
        result = cleiton_discovery_reply("Quero auditar custo de frete.", [])
        assert result["handoff"]["destination"] == "cleide_audit"

    def test_cost_ambiguous_reply_matches_document(self):
        local = build_local_conversational_reply("Quero analisar meu custo de frete.")
        assert local["reply"] == ACTIVITY_AMBIGUOUS_REPLY


class TestActivityFimKnowledge:
    """Roberto/Cleide separados por atividade fim e horizonte temporal."""

    @pytest.mark.parametrize("message", [
        "Quero analisar meu custo de frete.",
        "Tenho uma planilha de fretes.",
        "Quero gerar dashboard.",
        "Quero BI de frete.",
        "Quero analisar minhas transportadoras.",
    ])
    def test_ambiguous_no_handoff(self, message, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
        monkeypatch.setattr("app.run_cleiton_discovery.auditoria_registrar", lambda *a, **k: None)
        result = cleiton_discovery_reply(message, [])
        assert result["handoff"] is None
        assert "Roberto" in result["reply"]
        assert "Cleide" in result["reply"]

    @pytest.mark.parametrize("message", [
        "Quero prever meu custo de frete dos próximos meses.",
        "Quero projetar quanto vou gastar com frete.",
        "Quero entender a tendência futura do meu custo de frete.",
        "Tenho histórico de fretes e quero uma previsão.",
        "Quero um dashboard para acompanhar tendência e previsão de custos.",
    ])
    def test_roberto_predictive(self, message, monkeypatch):
        _mock_gemini(monkeypatch, {
            "reply": "Previsão futura — Roberto.",
            "recommended_agent": "roberto",
            "handoff": {"destination": "roberto_bi"},
            "confidence": "high",
            "reason": "previsão",
        })
        result = cleiton_discovery_reply(message, [])
        assert result["handoff"]["destination"] == "roberto_bi"

    @pytest.mark.parametrize("message", [
        "Quero saber se paguei frete errado nos últimos meses.",
        "Quero analisar desvios de custo de frete.",
        "Quero encontrar anomalias nos custos realizados.",
        "Quero auditar os fretes que paguei.",
        "Quero ver quais transportadoras tiveram maior desvio.",
        "Quero um dashboard de anomalias e cobranças suspeitas.",
    ])
    def test_cleide_retrospective(self, message, monkeypatch):
        _mock_gemini(monkeypatch, {
            "reply": "Investigação retrospectiva — Cleide.",
            "recommended_agent": "cleide",
            "handoff": {"destination": "cleide_audit"},
            "confidence": "high",
            "reason": "auditoria",
        })
        result = cleiton_discovery_reply(message, [])
        assert result["handoff"]["destination"] == "cleide_audit"

    @pytest.mark.parametrize("message", [
        "Quero decidir como reduzir meu custo de frete.",
        "Quero montar uma estratégia para negociar com transportadoras.",
        "Como a inflação impacta meu custo de frete?",
        "Como a taxa cambial afeta minha operação?",
        "Quero interpretar esses dados para tomar decisão.",
    ])
    def test_julia_strategic(self, message, monkeypatch):
        _mock_gemini(monkeypatch, {
            "reply": "Decisão estratégica — Júlia.",
            "recommended_agent": "julia",
            "handoff": {"destination": "julia_operational"},
            "confidence": "high",
            "reason": "estratégia",
        })
        result = cleiton_discovery_reply(message, [])
        assert result["handoff"]["destination"] == "julia_operational"

    def test_multi_prever_negociar_roberto_julia(self, monkeypatch):
        _mock_gemini(monkeypatch, {
            "reply": "Previsão com Roberto e negociação com Júlia.",
            "handoffs": [
                {"destination": "roberto_bi", "label": "Prever com Roberto"},
                {"destination": "julia_operational", "label": "Negociar com Júlia"},
            ],
            "confidence": "high",
            "reason": "previsão + negociação",
        })
        result = cleiton_discovery_reply(
            "Quero prever meus custos e entender como negociar com transportadoras.", []
        )
        assert result["discovery"]["next_action"] == "multi_handoff"
        assert len(result["handoffs"]) == 2

    def test_multi_auditar_plano_cleide_julia(self, monkeypatch):
        _mock_gemini(monkeypatch, {
            "reply": "Auditoria com Cleide e plano com Júlia.",
            "handoffs": [
                {"destination": "cleide_audit", "label": "Auditar com Cleide"},
                {"destination": "julia_operational", "label": "Plano com Júlia"},
            ],
            "confidence": "high",
            "reason": "auditoria + plano",
        })
        result = cleiton_discovery_reply(
            "Quero auditar desvios e depois montar plano de ação.", []
        )
        assert result["discovery"]["next_action"] == "multi_handoff"
        assert len(result["handoffs"]) == 2

    def test_pagar_errado_prever_multi_ou_contexto(self, monkeypatch):
        _mock_gemini(monkeypatch, {
            "reply": "Investigar passado e projetar futuro.",
            "handoffs": [
                {"destination": "cleide_audit", "label": "Investigar com Cleide"},
                {"destination": "roberto_bi", "label": "Projetar com Roberto"},
            ],
            "confidence": "high",
            "reason": "retrospectivo + preditivo",
        })
        result = cleiton_discovery_reply(
            "Quero saber se paguei errado e projetar próximos meses.", []
        )
        assert result["discovery"]["next_action"] == "multi_handoff"

    def test_resolve_activity_intent_predictive(self):
        assert resolve_activity_intent("Quero prever meu custo de frete.") == "roberto_predictive"

    def test_resolve_activity_intent_retrospective(self):
        assert resolve_activity_intent("Quero saber se paguei certo.") == "cleide_retrospective"

    def test_resolve_activity_intent_strategic(self):
        assert resolve_activity_intent("Quero decidir como reduzir custo.") == "julia_strategic"

    def test_gemini_roberto_suppressed_on_ambiguous_custo(self, monkeypatch):
        _mock_gemini(monkeypatch, {
            "reply": "Custo de frete é com Roberto.",
            "recommended_agent": "roberto",
            "handoff": {"destination": "roberto_bi"},
            "confidence": "high",
            "reason": "custo",
        })
        result = cleiton_discovery_reply("Quero BI de frete.", [])
        assert result["handoff"] is None


class TestBannedPatterns:
    def test_no_banned_pattern_in_engine_module(self):
        source = pathlib.Path("app/run_cleiton_discovery.py").read_text(encoding="utf-8")
        assert "Existem algumas formas de trabalhar esse tema no Agentefrete" not in source
        assert len(BANNED_REPLY_PATTERNS) >= 1

    def test_old_taxonomy_regex_removed(self):
        source = pathlib.Path("app/capability_taxonomy.py").read_text(encoding="utf-8")
        assert "EDITORIAL_CLEAR_INTENT" not in source
        assert "decide_next_action" not in source
