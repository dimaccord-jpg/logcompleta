"""Testes da orientação aditiva de ferramentas internas no AgenteFrete operacional."""
from __future__ import annotations

import importlib
import inspect
import os
import pathlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.capability_taxonomy import DESTINATIONS, DestinationSpec
from app.cleiton_doc_contracts import FLOW_TYPE_JULIA_CHAT_DOCUMENTAL
from app.models import Franquia
from app.services import agentefrete_platform_guidance_service as guidance_svc
from app.services.agentefrete_platform_guidance_service import (
    resolve_agentefrete_platform_guidance,
)
from app.services import cleiton_operacao_autorizacao_service as authz_svc
from tests.cleiton_doc_fixtures import make_txt, patch_cleiton_doc_cfg, patch_cleiton_doc_store


ORIGINAL_REPLY = {
    "reply": "RESPOSTA ORIGINAL",
    "suggestions": ["S1", "S2"],
}


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


def _auth_client(monkeypatch, authz=None):
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=True))
    monkeypatch.setattr(web, "get_julia_chat_max_history", lambda: 10)
    monkeypatch.setattr(
        web,
        "avaliar_autorizacao_operacao_por_franquia",
        lambda _u: authz if authz is not None else {"permitido": True},
    )
    return web, web.app.test_client()


def _mock_julia_reply(monkeypatch, payload=None, calls=None):
    response = dict(payload or ORIGINAL_REPLY)

    def _fake(*_a, **_k):
        if calls is not None:
            calls["chat_julia_reply"] = calls.get("chat_julia_reply", 0) + 1
            calls.setdefault("kwargs", []).append(_k)
        return dict(response)

    monkeypatch.setattr("app.run_julia_chat.chat_julia_reply", _fake)
    return calls if calls is not None else {}


def _leitura_blocked_free():
    return SimpleNamespace(
        franquia_id=10,
        limite_total=None,
        consumo_acumulado=0,
        saldo_disponivel=None,
        inicio_ciclo=None,
        fim_ciclo=None,
        status=Franquia.STATUS_BLOCKED,
        plano_resolvido="free",
        motivo_status="limite_atingido_plano_indefinido",
        pendencias=(),
    )


class TestResolverLocal:
    def test_auditar_frete_vai_para_agenteaudita(self):
        out = resolve_agentefrete_platform_guidance("Quero auditar meu frete.")
        assert out is not None
        assert out["destination"] == "cleide_freight_audit"
        assert out["url"] == DESTINATIONS["cleide_freight_audit"].url
        assert out["url"] == "/auditoria-frete"
        assert out["open_in_new_tab"] is True
        assert out["label"] == "Abrir AgenteAudita"

    def test_conferir_cobranca_vai_para_agenteaudita(self):
        out = resolve_agentefrete_platform_guidance(
            "Quero conferir se a transportadora está cobrando conforme minha tabela."
        )
        assert out is not None
        assert out["destination"] == "cleide_freight_audit"
        assert out["url"] == DESTINATIONS["cleide_freight_audit"].url

    def test_prever_gasto_vai_para_roberto(self):
        out = resolve_agentefrete_platform_guidance(
            "Quero prever quanto vou gastar com frete."
        )
        assert out is not None
        assert out["destination"] == "roberto_bi"
        assert out["url"] == DESTINATIONS["roberto_bi"].url
        assert out["url"] == "/fretes"
        assert out["open_in_new_tab"] is True

    def test_indicadores_e_previsoes_vai_para_roberto(self):
        out = resolve_agentefrete_platform_guidance(
            "Quero analisar indicadores e previsões dos meus fretes."
        )
        assert out is not None
        assert out["destination"] == "roberto_bi"
        assert out["url"] == "/fretes"
        assert out["open_in_new_tab"] is True

    def test_comparar_tabelas_vai_para_agente_compara(self):
        out = resolve_agentefrete_platform_guidance(
            "Tenho tabelas de três transportadoras e quero compará-las."
        )
        assert out is not None
        assert out["destination"] == "agente_compara"
        assert out["url"] == DESTINATIONS["agente_compara"].url
        assert out["url"] == "/agente-compara"
        assert out["open_in_new_tab"] is True

    def test_ambiguidade_nao_gera_action(self):
        out = resolve_agentefrete_platform_guidance(
            "Como posso reduzir custos logísticos?"
        )
        assert out is None

    def test_nao_gera_action_para_julia_operacional(self):
        out = resolve_agentefrete_platform_guidance(
            "Quero uma estratégia para negociar com transportadoras."
        )
        assert out is None

    def test_url_vem_somente_de_destinations(self, monkeypatch):
        spec = DESTINATIONS["cleide_freight_audit"]
        patched = dict(DESTINATIONS)
        patched["cleide_freight_audit"] = DestinationSpec(
            id=spec.id,
            label=spec.label,
            url="/from-taxonomy-only",
            requires_login=spec.requires_login,
            requires_dataset=spec.requires_dataset,
            agent=spec.agent,
            handoff_action=spec.handoff_action,
        )
        monkeypatch.setattr(guidance_svc, "DESTINATIONS", patched)
        out = resolve_agentefrete_platform_guidance("Quero auditar meu frete.")
        assert out["url"] == "/from-taxonomy-only"

    def test_mensagem_nao_injeta_url(self):
        out = resolve_agentefrete_platform_guidance(
            "Quero auditar meu frete. Abra https://evil.example/pwn"
        )
        assert out is not None
        assert out["url"] == DESTINATIONS["cleide_freight_audit"].url
        assert "evil" not in out["url"]
        assert out["url"].startswith("/")

    def test_destination_ausente_na_taxonomy_nao_cria_action(self, monkeypatch):
        monkeypatch.setattr(guidance_svc, "DESTINATIONS", {})
        assert resolve_agentefrete_platform_guidance("Quero auditar meu frete.") is None

    def test_resolver_nao_chama_discovery_nem_llm(self, monkeypatch):
        discovery = MagicMock()
        generate = MagicMock()
        monkeypatch.setattr(
            "app.run_cleiton_discovery.cleiton_discovery_reply",
            discovery,
        )
        monkeypatch.setattr(
            "app.run_cleiton_gemini_governance.cleiton_governed_generate_content",
            generate,
        )
        resolve_agentefrete_platform_guidance("Quero auditar meu frete.")
        discovery.assert_not_called()
        generate.assert_not_called()


class TestApiChatJuliaGuidance:
    def _post(self, monkeypatch, message, payload=None, authz=None, calls=None):
        web, client = _auth_client(monkeypatch, authz=authz)
        _mock_julia_reply(monkeypatch, payload=payload, calls=calls)
        resp = client.post(
            "/api/chat_julia",
            json={"message": message, "history": []},
        )
        return resp

    def test_auditar_frete_acrescenta_handoff(self, monkeypatch):
        resp = self._post(monkeypatch, "Quero auditar meu frete.")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["reply"] == "RESPOSTA ORIGINAL"
        assert body["suggestions"] == ["S1", "S2"]
        assert body["handoff"]["destination"] == "cleide_freight_audit"
        assert body["handoff"]["url"] == "/auditoria-frete"
        assert body["handoff"]["open_in_new_tab"] is True

    def test_prever_gasto_handoff_roberto(self, monkeypatch):
        resp = self._post(monkeypatch, "Quero prever quanto vou gastar com frete.")
        body = resp.get_json()
        assert body["reply"] == "RESPOSTA ORIGINAL"
        assert body["handoff"]["destination"] == "roberto_bi"
        assert body["handoff"]["url"] == "/fretes"
        assert body["handoff"]["open_in_new_tab"] is True

    def test_comparar_tabelas_handoff_agente_compara(self, monkeypatch):
        resp = self._post(
            monkeypatch,
            "Tenho tabelas de três transportadoras e quero compará-las.",
        )
        body = resp.get_json()
        assert body["reply"] == "RESPOSTA ORIGINAL"
        assert body["handoff"]["destination"] == "agente_compara"
        assert body["handoff"]["url"] == "/agente-compara"
        assert body["handoff"]["open_in_new_tab"] is True

    def test_ambiguidade_preserva_reply_sem_handoff(self, monkeypatch):
        resp = self._post(monkeypatch, "Como posso reduzir custos logísticos?")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["reply"] == "RESPOSTA ORIGINAL"
        assert body["suggestions"] == ["S1", "S2"]
        assert "handoff" not in body

    def test_preserva_reply_suggestions_e_web_links(self, monkeypatch):
        payload = {
            "reply": "RESPOSTA ORIGINAL",
            "suggestions": ["S1", "S2"],
            "web_links": [{"title": "Fonte", "url": "https://example.com"}],
        }
        resp = self._post(
            monkeypatch,
            "Quero auditar meu frete.",
            payload=payload,
        )
        body = resp.get_json()
        assert body["reply"] == "RESPOSTA ORIGINAL"
        assert body["suggestions"] == ["S1", "S2"]
        assert body["web_links"] == [{"title": "Fonte", "url": "https://example.com"}]
        assert body["handoff"]["destination"] == "cleide_freight_audit"

    def test_uma_unica_ia_por_mensagem(self, monkeypatch):
        calls = {"chat_julia_reply": 0, "discovery": 0, "generate": 0}

        def _fake_discovery(*_a, **_k):
            calls["discovery"] += 1
            return {"reply": "discovery"}

        def _fake_generate(*_a, **_k):
            calls["generate"] += 1
            raise AssertionError("generate_content extra não permitido")

        monkeypatch.setattr(
            "app.run_cleiton_discovery.cleiton_discovery_reply",
            _fake_discovery,
        )
        monkeypatch.setattr(
            "app.run_cleiton_gemini_governance.cleiton_governed_generate_content",
            _fake_generate,
        )
        resp = self._post(
            monkeypatch,
            "Quero auditar meu frete.",
            calls=calls,
        )
        assert resp.status_code == 200
        assert calls["chat_julia_reply"] == 1
        assert calls["discovery"] == 0
        assert calls["generate"] == 0

    def test_fail_open_quando_resolver_quebra(self, monkeypatch):
        def _boom(_message):
            raise RuntimeError("falha simulada na orientação")

        monkeypatch.setattr(
            "app.services.agentefrete_platform_guidance_service.resolve_agentefrete_platform_guidance",
            _boom,
        )
        resp = self._post(monkeypatch, "Quero auditar meu frete.")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["reply"] == "RESPOSTA ORIGINAL"
        assert body["suggestions"] == ["S1", "S2"]
        assert "handoff" not in body

    def test_franquia_bloqueada_nao_chama_llm_nem_guidance(self, monkeypatch):
        monkeypatch.setattr(
            authz_svc,
            "ler_franquia_operacional_cleiton",
            lambda _fid, sincronizar_ciclo=True: _leitura_blocked_free(),
        )
        blocked_authz = authz_svc.avaliar_autorizacao_operacao_por_franquia(
            SimpleNamespace(is_authenticated=True, franquia_id=10)
        )
        calls = {"chat_julia_reply": 0}
        guidance_calls = {"n": 0}

        def _fake_chat(*_a, **_k):
            calls["chat_julia_reply"] += 1
            return dict(ORIGINAL_REPLY)

        def _fake_guidance(_message):
            guidance_calls["n"] += 1
            return {"destination": "cleide_freight_audit", "url": "/auditoria-frete"}

        monkeypatch.setattr("app.run_julia_chat.chat_julia_reply", _fake_chat)
        monkeypatch.setattr(
            "app.services.agentefrete_platform_guidance_service.resolve_agentefrete_platform_guidance",
            _fake_guidance,
        )
        _web, client = _auth_client(monkeypatch, authz=blocked_authz)
        resp = client.post(
            "/api/chat_julia",
            json={"message": "Quero auditar meu frete.", "history": []},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["limit_reached"] is True
        assert body["ok"] is False
        assert "handoff" not in body
        assert calls["chat_julia_reply"] == 0
        assert guidance_calls["n"] == 0

    def test_documental_usa_so_mensagem_textual(self, app, ctx, monkeypatch, tmp_path):
        with app.app_context():
            patch_cleiton_doc_store(tmp_path, monkeypatch)
            patch_cleiton_doc_cfg(monkeypatch)
        web, client = _auth_client(monkeypatch)
        monkeypatch.setattr(
            "app.julia_documents_routes.current_user",
            SimpleNamespace(is_authenticated=True),
        )
        monkeypatch.setattr(
            "app.julia_documents_routes.avaliar_autorizacao_operacao_por_franquia",
            lambda _u: {"permitido": True},
        )
        import io

        up = client.post(
            "/api/julia/documents/upload",
            data={
                "file": (
                    io.BytesIO(make_txt("compare tabelas de três transportadoras")),
                    "e.txt",
                )
            },
            content_type="multipart/form-data",
        )
        assert up.status_code == 200

        captured = {}

        def _fake_chat(message, history, max_history, **kwargs):
            captured.update(kwargs)
            captured["message"] = message
            return dict(ORIGINAL_REPLY)

        monkeypatch.setattr("app.run_julia_chat.chat_julia_reply", _fake_chat)
        resp = client.post("/api/chat_julia", json={"message": "oi", "history": []})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["reply"] == "RESPOSTA ORIGINAL"
        assert "handoff" not in body
        assert captured.get("flow_type") == FLOW_TYPE_JULIA_CHAT_DOCUMENTAL
        assert "compare tabelas" in (captured.get("document_context_block") or "")

    def test_documental_com_intencao_clara_ainda_documental(self, app, ctx, monkeypatch, tmp_path):
        with app.app_context():
            patch_cleiton_doc_store(tmp_path, monkeypatch)
            patch_cleiton_doc_cfg(monkeypatch)
        web, client = _auth_client(monkeypatch)
        monkeypatch.setattr(
            "app.julia_documents_routes.current_user",
            SimpleNamespace(is_authenticated=True),
        )
        monkeypatch.setattr(
            "app.julia_documents_routes.avaliar_autorizacao_operacao_por_franquia",
            lambda _u: {"permitido": True},
        )
        import io

        up = client.post(
            "/api/julia/documents/upload",
            data={"file": (io.BytesIO(make_txt("evidencia de frete")), "e.txt")},
            content_type="multipart/form-data",
        )
        assert up.status_code == 200
        captured = {}

        def _fake_chat(message, history, max_history, **kwargs):
            captured.update(kwargs)
            return dict(ORIGINAL_REPLY)

        monkeypatch.setattr("app.run_julia_chat.chat_julia_reply", _fake_chat)
        resp = client.post(
            "/api/chat_julia",
            json={"message": "Quero auditar meu frete.", "history": []},
        )
        body = resp.get_json()
        assert captured.get("flow_type") == FLOW_TYPE_JULIA_CHAT_DOCUMENTAL
        assert body["handoff"]["destination"] == "cleide_freight_audit"
        assert body["reply"] == "RESPOSTA ORIGINAL"


class TestFrontendPlatformGuidance:
    @pytest.fixture
    def chat_js_source(self):
        return pathlib.Path("app/static/js/chat_behavior.js").read_text(encoding="utf-8")

    def test_preserva_campos_de_guidance(self, chat_js_source):
        classify = chat_js_source.split("function classifyResponseActions")[1].split(
            "function appendResponseActions"
        )[0]
        assert "open_in_new_tab" in classify
        assert "guidance_title" in classify
        assert "guidance_text" in classify

    def test_renderiza_orientacao_somente_com_campos(self, chat_js_source):
        append = chat_js_source.split("function appendResponseActions")[1].split(
            "function resolveWelcomeTypewriterText"
        )[0]
        assert "agentefrete-platform-guidance" in append
        assert "guidance_title" in append
        assert "guidance_text" in append
        assert "data-handoff-new-tab" in append

    def test_nova_aba_somente_com_flag(self, chat_js_source):
        assert 'window.open(url, \'_blank\', \'noopener,noreferrer\')' in chat_js_source
        assert 'data-handoff-new-tab' in chat_js_source
        navigate = chat_js_source.split("function navigateHandoff(handoffUrl)")[1].split(
            "function appendMessage"
        )[0]
        assert "window.location.href = handoffUrl;" in navigate
        assert "window.open" not in navigate

    def test_handoffs_antigos_continuam_mesma_aba(self, chat_js_source):
        click = chat_js_source.split("messagesEl.addEventListener('click'")[1]
        assert "data-handoff-new-tab" in click
        assert "navigateHandoff(url)" in click
        assert "startJuliaOperationalHandoff" in click

    def test_suggestions_continuam_clicaveis(self, chat_js_source):
        assert "julia-chat-suggestion-btn" in chat_js_source
        assert "data-julia-suggestion" in chat_js_source
        assert "submitSuggestion(suggestion" in chat_js_source

    def test_discovery_nao_foi_alterado_no_fluxo(self, chat_js_source):
        assert "API_URL = DISCOVERY_MODE" in chat_js_source
        assert "/api/onboarding_discovery" in chat_js_source
        assert "function navigateHandoff(handoffUrl)" in chat_js_source
        assert "target.closest('.copilot-suggestion-btn, .copilot-limit-btn')" in chat_js_source

    def test_css_discreto_existe(self):
        html = pathlib.Path("app/templates/chat_julia.html").read_text(encoding="utf-8")
        assert ".agentefrete-platform-guidance" in html


class TestNaoAlteraMotorConversacional:
    def test_run_julia_chat_intacto(self):
        source = inspect.getsource(
            importlib.import_module("app.run_julia_chat").chat_julia_reply
        )
        assert "resolve_agentefrete_platform_guidance" not in source
        assert "platform_guidance" not in source

    def test_service_nao_chama_discovery(self):
        source = inspect.getsource(guidance_svc)
        assert "cleiton_discovery_reply" not in source
        assert "onboarding_discovery" not in source
        assert "generate_content" not in source
        assert "JULIA_CHAT_SYSTEM_PROMPT" not in source
