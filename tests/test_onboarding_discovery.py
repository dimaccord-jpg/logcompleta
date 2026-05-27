"""Testes do MVP de onboarding inteligente (Cleiton Discovery)."""
from __future__ import annotations

import importlib
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.capability_taxonomy import (
    DESTINATIONS,
    compute_confidence_level,
    decide_next_action,
    get_cta_by_id,
    is_clear_editorial_market_intent,
    rank_destinations_from_capabilities,
)
from app.run_cleiton_discovery import (
    _extract_json_object,
    _normalize_candidates,
    cleiton_discovery_reply,
)


class TestCapabilityTaxonomy:
    def test_cta_seeds_domains_not_destination(self):
        cta = get_cta_by_id("reduce_cost")
        assert cta is not None
        assert "freight_bi" in cta["seed_domains"]
        assert "operational_audit" in cta["seed_domains"]
        assert len(cta["seed_domains"]) >= 3

    def test_rank_destinations_from_capabilities(self):
        caps = [
            {"domain": "operational_audit", "score": 74},
            {"domain": "freight_bi", "score": 61},
        ]
        ranked = rank_destinations_from_capabilities(caps)
        assert ranked
        assert ranked[0]["destination"] == "cleide_audit"
        assert ranked[0]["agent"] == "cleide"

    def test_handoff_destinations_do_not_require_login_at_navigation(self):
        for dest_id in ("feed", "julia_operational", "roberto_bi", "cleide_audit"):
            spec = DESTINATIONS[dest_id]
            assert spec.requires_login is False

    def test_julia_operational_has_internal_handoff_action(self):
        spec = DESTINATIONS["julia_operational"]
        assert spec.handoff_action == "start_julia"
        assert spec.url == "/chat_julia?mode=operational"

    def test_confidence_low_when_scores_close(self):
        caps = [
            {"domain": "freight_bi", "score": 62},
            {"domain": "operational_audit", "score": 58},
        ]
        assert compute_confidence_level(caps) in ("low", "medium")

    def test_handoff_requires_refinement_or_confirmation(self):
        assert decide_next_action("high", user_confirmed=False, history_turns=1) == "refine"
        assert decide_next_action("high", user_confirmed=True, history_turns=1) == "handoff"
        assert decide_next_action("high", user_confirmed=False, history_turns=3) == "handoff"

    def test_editorial_clear_intent_detected(self):
        assert is_clear_editorial_market_intent("quero notícias")
        assert is_clear_editorial_market_intent("tendências")
        assert is_clear_editorial_market_intent("", cta_id="market_news")

    def test_feed_immediate_handoff_first_turn(self):
        assert (
            decide_next_action(
                "low",
                user_confirmed=False,
                history_turns=1,
                top_capability_domain="editorial_market",
                user_message="quero notícias",
            )
            == "handoff"
        )


class TestCleitonDiscoveryParsing:
    def test_extract_json_object_from_markdown_fence(self):
        raw = '```json\n{"reply": "ok", "capability_candidates": []}\n```'
        parsed = _extract_json_object(raw)
        assert parsed is not None
        assert parsed.get("reply") == "ok"

    def test_normalize_candidates_filters_invalid_domains(self):
        raw = [
            {"domain": "freight_bi", "mode": "analyze", "score": 80},
            {"domain": "invalid_domain", "score": 90},
        ]
        out = _normalize_candidates(raw)
        assert len(out) == 1
        assert out[0]["domain"] == "freight_bi"
        assert out[0]["mode"] == "analyze"


class TestCleitonDiscoveryReply:
    def test_fallback_without_gemini(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
        result = cleiton_discovery_reply("quero reduzir custo", [])
        assert result["reply"]
        assert result["discovery"]["next_action"] == "refine"
        assert result["handoff"] is None
        assert len(result["discovery"]["capability_candidates"]) >= 2

    def test_feed_handoff_first_turn_without_gemini(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
        result = cleiton_discovery_reply("quero notícias", [])
        assert result["discovery"]["next_action"] == "handoff"
        assert result["handoff"] is not None
        assert result["handoff"]["destination"] == "feed"
        assert result["handoff"]["url"] == "/feed"
        assert result["handoff"]["requires_login"] is False

    def test_governed_gemini_path(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")

        fake_response = MagicMock()
        fake_response.text = json.dumps({
            "reply": "Existem algumas formas de trabalhar redução de custo.",
            "capability_candidates": [
                {"domain": "operational_audit", "mode": "audit", "score": 74},
                {"domain": "freight_bi", "mode": "analyze", "score": 61},
            ],
            "refinement_options": ["BI operacional", "Auditoria operacional"],
            "user_confirmed": False,
        })

        def fake_governed(client, **kwargs):
            assert kwargs["agent"] == "cleiton"
            assert kwargs["flow_type"] == "onboarding_discovery"
            return fake_response

        monkeypatch.setattr(
            "app.run_cleiton_discovery.cleiton_governed_generate_content",
            fake_governed,
        )
        monkeypatch.setattr(
            "app.run_cleiton_discovery._get_client",
            lambda: MagicMock(),
        )
        monkeypatch.setattr(
            "app.run_cleiton_discovery.auditoria_registrar",
            lambda *a, **k: None,
        )

        result = cleiton_discovery_reply("quero reduzir custo", [], cta_id="reduce_cost")
        assert "redução" in result["reply"].lower() or "formas" in result["reply"].lower()
        assert result["discovery"]["next_action"] == "refine"
        assert result["refinement_options"]
        assert result["destination_candidates"]

    def test_handoff_after_confirmation(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")

        fake_response = MagicMock()
        fake_response.text = json.dumps({
            "reply": "Perfeito, vamos para BI operacional.",
            "capability_candidates": [
                {"domain": "freight_bi", "mode": "analyze", "score": 88},
                {"domain": "operational_audit", "mode": "audit", "score": 55},
            ],
            "refinement_options": [],
            "user_confirmed": True,
        })

        monkeypatch.setattr(
            "app.run_cleiton_discovery.cleiton_governed_generate_content",
            lambda *a, **k: fake_response,
        )
        monkeypatch.setattr(
            "app.run_cleiton_discovery._get_client",
            lambda: MagicMock(),
        )
        monkeypatch.setattr(
            "app.run_cleiton_discovery.auditoria_registrar",
            lambda *a, **k: None,
        )

        history = [
            {"role": "user", "content": "quero reduzir custo"},
            {"role": "model", "content": "opções..."},
        ]
        result = cleiton_discovery_reply("prefiro BI operacional", history)
        assert result["discovery"]["next_action"] == "handoff"
        assert result["handoff"] is not None
        assert result["handoff"]["destination"] == "roberto_bi"
        assert result["handoff"]["url"] == "/fretes"
        assert result["handoff"]["requires_login"] is False

    def test_handoff_julia_internal_not_external_url(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")

        fake_response = MagicMock()
        fake_response.text = json.dumps({
            "reply": "Vamos para consultoria estratégica.",
            "capability_candidates": [
                {"domain": "strategic_logistics", "mode": "explain", "score": 90},
                {"domain": "forecast_planning", "mode": "forecast", "score": 55},
            ],
            "refinement_options": [],
            "user_confirmed": True,
        })

        monkeypatch.setattr(
            "app.run_cleiton_discovery.cleiton_governed_generate_content",
            lambda *a, **k: fake_response,
        )
        monkeypatch.setattr(
            "app.run_cleiton_discovery._get_client",
            lambda: MagicMock(),
        )
        monkeypatch.setattr(
            "app.run_cleiton_discovery.auditoria_registrar",
            lambda *a, **k: None,
        )

        history = [
            {"role": "user", "content": "apoio estratégico"},
            {"role": "model", "content": "opções..."},
        ]
        result = cleiton_discovery_reply("prefiro estratégia logística", history)
        assert result["handoff"] is not None
        assert result["handoff"]["destination"] == "julia_operational"
        assert result["handoff"]["action"] == "start_julia"
        assert result["handoff"]["url"] is None
        assert result["handoff"]["requires_login"] is False


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
        resp = client.get("/chat_julia")
        assert resp.status_code == 302

        resp_ok = client.get("/chat_julia?mode=operational")
        assert resp_ok.status_code == 200
        html = resp_ok.get_data(as_text=True)
        assert "ONBOARDING_DISCOVERY_MODE" not in html
        assert "Consultoria logística" in html
        assert "descoberta inteligente" not in html


class TestOnboardingHandoffDoesNotWeakenApiAuth:
    def test_chat_julia_api_still_requires_login(self, monkeypatch):
        os.environ.setdefault("APP_ENV", "dev")
        os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
        os.environ.setdefault("SECRET_KEY", "test-secret")
        web = importlib.import_module("app.web")
        monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=False))
        client = web.app.test_client()
        resp = client.post("/api/chat_julia", json={"message": "oi", "history": []})
        assert resp.status_code == 401
        body = resp.get_json()
        assert body["require_login"] is True

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
                "discovery": {"next_action": "refine", "confidence": "low", "capability_candidates": []},
                "handoff": None,
            },
        )
        client = web.app.test_client()
        resp = client.post("/api/onboarding_discovery", json={"message": "oi", "history": []})
        assert resp.status_code == 200
