"""SCRUM-191 — identidade pública AgenteFrete no chat operacional."""
from __future__ import annotations

import importlib
import inspect
import os
from types import SimpleNamespace

import pytest

from app.prompts import JULIA_CHAT_SYSTEM_PROMPT, PERSONA
from app.run_julia_chat import (
    GENERIC_REPLY_FALLBACK,
    DOCUMENTAL_DEADLINE_REPLY,
    _build_contents_with_history,
)


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


def test_operational_system_prompt_uses_agentefrete_identity():
    prompt = JULIA_CHAT_SYSTEM_PROMPT
    assert "AgenteFrete" in prompt
    assert "LogCompleta" in prompt
    assert "Você é o AgenteFrete" in prompt
    assert "assistente virtual especializado em logística da LogCompleta" in prompt


def test_operational_system_prompt_rejects_julia_public_identity():
    prompt = JULIA_CHAT_SYSTEM_PROMPT
    assert "Você é Júlia" not in prompt
    assert "Como Júlia" not in prompt
    assert "Apresente-se como Júlia" not in prompt
    assert "Nunca se apresente como Júlia" in prompt
    assert "nunca diga que seu nome é Júlia" in prompt
    assert "Ignore apresentações antigas incompatíveis" in prompt


def test_editorial_persona_still_julia():
    assert "Júlia" in PERSONA
    assert "Editora-Chefe" in PERSONA
    assert "Você é Júlia, Editora-Chefe" in PERSONA


def test_build_contents_uses_agentefrete_labels():
    contents = _build_contents_with_history(
        [
            {"role": "user", "content": "Olá"},
            {
                "role": "model",
                "content": "Sou Júlia, assistente especializada em logística e supply chain do Agentefrete.",
            },
        ],
        "Quem é você?",
    )
    assert isinstance(contents, str)
    assert contents.startswith(JULIA_CHAT_SYSTEM_PROMPT.strip())
    assert "AgenteFrete: Sou Júlia," in contents
    assert contents.rstrip().endswith("AgenteFrete:")
    labeled_lines = [
        line for line in contents.splitlines() if line.startswith("Júlia:") or line.startswith("AgenteFrete:")
    ]
    assert labeled_lines
    assert all(line.startswith("AgenteFrete:") for line in labeled_lines)
    assert "Nunca se apresente como Júlia" in contents
    assert "Ignore apresentações antigas incompatíveis" in contents


def test_fallbacks_do_not_present_as_julia():
    for text in (
        GENERIC_REPLY_FALLBACK,
        DOCUMENTAL_DEADLINE_REPLY,
        "Envie uma mensagem sobre logistica, fretes ou supply chain que eu respondo com prazer.",
        "Assistente temporariamente indisponível. Verifique a configuração do serviço.",
    ):
        assert "Júlia" not in text
        assert "Julia" not in text


def test_chat_julia_operational_route_still_works(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(
        web,
        "current_user",
        SimpleNamespace(is_authenticated=True, is_admin=False),
    )
    monkeypatch.setattr(web, "get_julia_chat_max_history", lambda: 10)
    monkeypatch.setattr(
        web,
        "avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True},
    )
    monkeypatch.setattr(web, "_pop_onboarding_julia_context", lambda: None)
    resp = web.app.test_client().get("/chat_julia?mode=operational")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "AgenteFrete" in html
    assert "Assistente virtual especializado em logística" in html


def test_discovery_prompt_unchanged_agentefrete():
    from app.run_cleiton_discovery import _build_system_prompt

    source = inspect.getsource(_build_system_prompt)
    prompt = _build_system_prompt()
    assert "AgenteFrete" in prompt
    assert "nunca Júlia" in prompt
    assert "JULIA_CHAT_SYSTEM_PROMPT" not in source
    assert "Você é o Copilot do AgenteFrete" in prompt
