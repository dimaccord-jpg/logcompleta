"""Testes da mensagem única de limite de plano no chat da Júlia."""
from __future__ import annotations

import importlib
import os
import pathlib
from types import SimpleNamespace

import pytest

from app.models import Franquia
from app.services import cleiton_mensageria_operacao_service as mensageria
from app.services import cleiton_operacao_autorizacao_service as authz_svc


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


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


def test_mensagem_limite_plano_sem_markdown():
    texto = mensageria.montar_mensagem_limite_plano_texto("Free")
    assert "[" not in texto
    assert "]" not in texto
    assert "/contrate-um-plano" not in texto
    assert "Faça o upgrade" in texto
    assert texto.count("Faça o upgrade") == 1


def test_upgrade_cta_estruturado():
    cta = mensageria.montar_upgrade_cta_operacao("free")
    assert cta["error_code"] == "plan_limit_reached"
    assert cta["upgrade_url"] == "/contrate-um-plano"
    assert cta["upgrade_label"] == "Faça o upgrade"
    assert "Free" in cta["message"]
    assert cta["message_suffix"].startswith(" e continue")


def test_authz_blocked_inclui_upgrade_cta(monkeypatch):
    monkeypatch.setattr(
        authz_svc,
        "ler_franquia_operacional_cleiton",
        lambda _fid, sincronizar_ciclo=True: _leitura_blocked_free(),
    )
    user = SimpleNamespace(is_authenticated=True, franquia_id=10)
    out = authz_svc.avaliar_autorizacao_operacao_por_franquia(user)
    assert out["permitido"] is False
    assert out["upgrade_cta"]["error_code"] == "plan_limit_reached"
    assert "[Faça o upgrade]" not in (out["mensagem_usuario"] or "")


def test_api_chat_julia_limite_unica_mensagem(monkeypatch):
    monkeypatch.setattr(
        authz_svc,
        "ler_franquia_operacional_cleiton",
        lambda _fid, sincronizar_ciclo=True: _leitura_blocked_free(),
    )
    blocked_authz = authz_svc.avaliar_autorizacao_operacao_por_franquia(
        SimpleNamespace(is_authenticated=True, franquia_id=10)
    )
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=True))
    monkeypatch.setattr(web, "get_julia_chat_max_history", lambda: 10)
    monkeypatch.setattr(
        web,
        "avaliar_autorizacao_operacao_por_franquia",
        lambda _u: blocked_authz,
    )

    client = web.app.test_client()
    resp = client.post("/api/chat_julia", json={"message": "oi", "history": []})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["limit_reached"] is True
    assert body["ok"] is False
    assert body["error_code"] == "plan_limit_reached"
    assert body["upgrade_url"] == "/contrate-um-plano"
    reply = body["reply"] or ""
    assert reply.count("Você atingiu o limite de uso do plano") == 1
    assert "[Faça o upgrade]" not in reply
    assert reply.count("Faça o upgrade") == 1


@pytest.fixture
def chat_js_source():
    return pathlib.Path("app/static/js/chat_behavior.js").read_text(encoding="utf-8")


def test_chat_behavior_nao_duplica_mensagem_no_chat(chat_js_source):
    assert "isJuliaPlanLimitResponse(data)" in chat_js_source
    assert "showPlanLimitBanner" in chat_js_source
    assert "fillLimitMessageElement" in chat_js_source
    assert "createElement('a')" in chat_js_source
    assert "removePlanLimitBotMessages" in chat_js_source
    idx = chat_js_source.find("isJuliaPlanLimitResponse(data)")
    append_idx = chat_js_source.find("appendMessage('bot', data.reply")
    assert idx >= 0 and append_idx > idx


def test_chat_behavior_nao_usa_innerhtml_no_limite(chat_js_source):
    chunk = chat_js_source.split("function fillLimitMessageElement")[1].split("function removePlanLimitBotMessages")[0]
    assert "innerHTML" not in chunk


def test_chat_julia_template_limit_link_style():
    html = pathlib.Path("app/templates/chat_julia.html").read_text(encoding="utf-8")
    assert ".julia-chat-limit-msg a" in html


def test_julia_documents_nao_duplica_mensagem_franquia():
    js_source = pathlib.Path("app/static/js/julia_documents.js").read_text(encoding="utf-8")
    assert "error_code === 'franquia_blocked'" in js_source
    chunk = js_source.split("error_code === 'franquia_blocked'")[1][:200]
    assert "setError('')" in chunk
