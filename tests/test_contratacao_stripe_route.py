import logging
import os
import time
from datetime import datetime, timezone

import pytest

# Permite importar app.web em ambiente de teste sem depender da shell externa.
os.environ.setdefault("APP_ENV", "dev")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg2://test_user:test_pass@localhost:5432/test_db",
)

from app import web as web_app  # noqa: E402
from app.models import User  # noqa: E402


class _FakeCurrentUser:
    def __init__(self, user):
        self._user = user

    def _get_current_object(self):
        return self._user


@pytest.fixture
def client():
    app = web_app.app
    app.config["TESTING"] = True
    original_login_disabled = app.config.get("LOGIN_DISABLED", False)
    app.config["LOGIN_DISABLED"] = True
    try:
        yield app.test_client()
    finally:
        app.config["LOGIN_DISABLED"] = original_login_disabled


def _user_teste() -> User:
    return User(
        id=101,
        email="cliente@test.com",
        full_name="Cliente Teste",
        categoria="starter",
        conta_id=10,
        franquia_id=20,
    )


def test_iniciar_contratacao_route_json_contrato_compat(client, monkeypatch):
    user = _user_teste()
    monkeypatch.setattr("app.user_area.current_user", _FakeCurrentUser(user))

    def _fake_start(*, user, plano_codigo, site_origin):
        assert plano_codigo == "starter"
        assert user.conta_id == 10
        assert site_origin.startswith("http")
        return {
            "checkout_session_id": "cs_test_route",
            "checkout_client_secret": "secret_route",
            "publishable_key": "pk_test_route",
            "plano_codigo": "starter",
        }

    monkeypatch.setattr("app.user_area.iniciar_jornada_assinatura_stripe", _fake_start)

    resp = client.post(
        "/api/contratacao/stripe/iniciar",
        json={"plano_codigo": " STARTER "},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["checkout_session_id"] == "cs_test_route"
    assert body["plano_codigo"] == "starter"


def test_iniciar_contratacao_route_form_fallback_para_cliente_nao_frontend(client, monkeypatch):
    user = _user_teste()
    monkeypatch.setattr("app.user_area.current_user", _FakeCurrentUser(user))

    monkeypatch.setattr(
        "app.user_area.iniciar_jornada_assinatura_stripe",
        lambda **_kwargs: {
            "checkout_session_id": "cs_form",
            "checkout_client_secret": "secret_form",
            "publishable_key": "pk_form",
            "plano_codigo": "pro",
        },
    )

    resp = client.post(
        "/api/contratacao/stripe/iniciar",
        data={"plano_codigo": "PRO"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["plano_codigo"] == "pro"


def test_iniciar_contratacao_route_mantem_erro_estavel_quando_plano_ausente(client, monkeypatch):
    monkeypatch.setattr("app.user_area.current_user", _FakeCurrentUser(_user_teste()))
    resp = client.post("/api/contratacao/stripe/iniciar", json={})
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["ok"] is False
    assert body["erro"] == "plano_codigo_obrigatorio"
    assert body["codigo_erro"] == "contratacao_stripe_plano_codigo_obrigatorio"
    assert body["error"] == "plano_codigo_obrigatorio"


def test_iniciar_contratacao_route_exige_confirmacao_para_downgrade(client, monkeypatch):
    user = _user_teste()
    user.categoria = "pro"
    monkeypatch.setattr("app.user_area.current_user", _FakeCurrentUser(user))
    resp = client.post(
        "/api/contratacao/stripe/iniciar",
        json={"plano_codigo": "starter"},
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["ok"] is False
    assert body["erro"] == "confirmacao_downgrade_obrigatoria"


def test_iniciar_contratacao_route_aceita_downgrade_com_confirmacao(client, monkeypatch):
    user = _user_teste()
    user.categoria = "pro"
    monkeypatch.setattr("app.user_area.current_user", _FakeCurrentUser(user))
    monkeypatch.setattr(
        "app.user_area.iniciar_jornada_assinatura_stripe",
        lambda **_kwargs: {
            "checkout_session_id": "cs_downgrade",
            "checkout_client_secret": "secret_downgrade",
            "publishable_key": "pk_downgrade",
            "plano_codigo": "starter",
        },
    )
    resp = client.post(
        "/api/contratacao/stripe/iniciar",
        json={"plano_codigo": "starter", "confirmar_downgrade": True},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["plano_codigo"] == "starter"


def test_contrate_plano_exibe_feedback_no_retorno_sucesso(client, monkeypatch):
    monkeypatch.setattr("app.user_area.current_user", _FakeCurrentUser(_user_teste()))
    monkeypatch.setattr(
        "app.user_area.listar_planos_contratacao_publica",
        lambda: [],
    )
    monkeypatch.setattr(
        "app.user_area.conciliar_checkout_session_stripe",
        lambda _session_id: {"efeito_operacional_aplicado": True},
    )

    resp = client.get("/contrate-um-plano?checkout=success&session_id=cs_test_sync")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Contratacao confirmada com sucesso" in html


def test_contrate_plano_success_sem_session_id_exibe_processamento(client, monkeypatch):
    monkeypatch.setattr("app.user_area.current_user", _FakeCurrentUser(_user_teste()))
    monkeypatch.setattr(
        "app.user_area.listar_planos_contratacao_publica",
        lambda: [],
    )

    def _nao_deve_chamar(_session_id):
        raise AssertionError("conciliar_checkout_session_stripe nao deveria ser chamado sem session_id")

    monkeypatch.setattr("app.user_area.conciliar_checkout_session_stripe", _nao_deve_chamar)

    resp = client.get("/contrate-um-plano?checkout=success")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Pagamento recebido com sucesso e ativacao em processamento" in html


def test_contrate_plano_success_sem_session_id_exibe_downgrade_pendente_do_vinculo(client, monkeypatch):
    user = _user_teste()
    user.categoria = "pro"
    monkeypatch.setattr("app.user_area.current_user", _FakeCurrentUser(user))
    monkeypatch.setattr(
        "app.user_area.listar_planos_contratacao_publica",
        lambda: [],
    )

    def _nao_deve_chamar(_session_id):
        raise AssertionError("conciliar_checkout_session_stripe nao deveria ser chamado sem session_id")

    monkeypatch.setattr("app.user_area.conciliar_checkout_session_stripe", _nao_deve_chamar)
    monkeypatch.setattr(
        "app.user_area.obter_pendencia_downgrade_conta_ativa",
        lambda _cid: {
            "mudanca_pendente": True,
            "plano_pendente": "starter",
            "efetivar_em": "2099-12-31T00:00:00",
            "atualizado_em": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        },
    )

    resp = client.get("/contrate-um-plano?checkout=success")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "alteracao para o plano starter foi registrada" in html.lower()
    assert "2099-12-31" in html


def test_contrate_plano_success_conciliacao_generica_usa_pendencia_vinculo(client, monkeypatch):
    user = _user_teste()
    user.categoria = "pro"
    monkeypatch.setattr("app.user_area.current_user", _FakeCurrentUser(user))
    monkeypatch.setattr(
        "app.user_area.listar_planos_contratacao_publica",
        lambda: [],
    )
    monkeypatch.setattr(
        "app.user_area.conciliar_checkout_session_stripe",
        lambda _sid: {"ok": True, "conciliado": False, "motivo": "checkout_ainda_nao_confirmado"},
    )
    monkeypatch.setattr(
        "app.user_area.obter_pendencia_downgrade_conta_ativa",
        lambda _cid: {
            "mudanca_pendente": True,
            "plano_pendente": "starter",
            "efetivar_em": "2099-06-01T00:00:00",
            "atualizado_em": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        },
    )

    resp = client.get("/contrate-um-plano?checkout=success&session_id=cs_test_pend")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "alteracao para o plano starter foi registrada" in html.lower()


def test_contrate_plano_success_pendencia_antiga_nao_gera_mensagem_downgrade(client, monkeypatch):
    """Pendencia ativa fora de contexto (sem sessao de checkout nem atualizado_em recente)."""
    user = _user_teste()
    user.categoria = "pro"
    monkeypatch.setattr("app.user_area.current_user", _FakeCurrentUser(user))
    monkeypatch.setattr(
        "app.user_area.listar_planos_contratacao_publica",
        lambda: [],
    )

    def _nao_deve_chamar(_session_id):
        raise AssertionError("conciliar_checkout_session_stripe nao deveria ser chamado sem session_id")

    monkeypatch.setattr("app.user_area.conciliar_checkout_session_stripe", _nao_deve_chamar)
    monkeypatch.setattr(
        "app.user_area.obter_pendencia_downgrade_conta_ativa",
        lambda _cid: {
            "mudanca_pendente": True,
            "plano_pendente": "starter",
            "efetivar_em": "2099-12-31T00:00:00",
            "atualizado_em": "2020-01-01T00:00:00",
        },
    )

    resp = client.get("/contrate-um-plano?checkout=success")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Pagamento recebido com sucesso e ativacao em processamento" in html
    assert "alteracao para o plano starter" not in html.lower()


def test_contrate_plano_success_pendencia_com_sessao_embed_compativel_mostra_mensagem(
    client, monkeypatch
):
    from app.user_area import _SESSION_CONTRATACAO_EMBED_EPOCH, _SESSION_CONTRATACAO_EMBED_PLANO

    user = _user_teste()
    user.categoria = "pro"
    monkeypatch.setattr("app.user_area.current_user", _FakeCurrentUser(user))
    monkeypatch.setattr(
        "app.user_area.listar_planos_contratacao_publica",
        lambda: [],
    )

    def _nao_deve_chamar(_session_id):
        raise AssertionError("conciliar_checkout_session_stripe nao deveria ser chamado sem session_id")

    monkeypatch.setattr("app.user_area.conciliar_checkout_session_stripe", _nao_deve_chamar)
    monkeypatch.setattr(
        "app.user_area.obter_pendencia_downgrade_conta_ativa",
        lambda _cid: {
            "mudanca_pendente": True,
            "plano_pendente": "starter",
            "efetivar_em": "2099-08-01T00:00:00",
            "atualizado_em": "2020-01-01T00:00:00",
        },
    )

    with client.session_transaction() as sess:
        sess[_SESSION_CONTRATACAO_EMBED_PLANO] = "starter"
        sess[_SESSION_CONTRATACAO_EMBED_EPOCH] = int(time.time())

    resp = client.get("/contrate-um-plano?checkout=success")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "alteracao para o plano starter foi registrada" in html.lower()
    assert "2099-08-01" in html


def test_contrate_plano_obter_pendencia_excecao_nao_derruba_pagina(caplog, client, monkeypatch):
    caplog.set_level(logging.ERROR)

    def _explode(_cid):
        raise RuntimeError("simula falha interna")

    monkeypatch.setattr("app.user_area.current_user", _FakeCurrentUser(_user_teste()))
    monkeypatch.setattr(
        "app.user_area.listar_planos_contratacao_publica",
        lambda: [],
    )

    def _nao_deve_chamar(_session_id):
        raise AssertionError("conciliar_checkout_session_stripe nao deveria ser chamado sem session_id")

    monkeypatch.setattr("app.user_area.conciliar_checkout_session_stripe", _nao_deve_chamar)
    monkeypatch.setattr("app.user_area.obter_pendencia_downgrade_conta_ativa", _explode)

    resp = client.get("/contrate-um-plano?checkout=success")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Pagamento recebido com sucesso e ativacao em processamento" in html
    assert any(
        "obter_pendencia_downgrade_conta_ativa levantou excecao" in r.message for r in caplog.records
    )


def test_contrate_plano_success_com_placeholder_literal_nao_concilia(client, monkeypatch):
    monkeypatch.setattr("app.user_area.current_user", _FakeCurrentUser(_user_teste()))
    monkeypatch.setattr(
        "app.user_area.listar_planos_contratacao_publica",
        lambda: [],
    )

    def _nao_deve_chamar(_session_id):
        raise AssertionError(
            "conciliar_checkout_session_stripe nao deveria ser chamado com placeholder literal"
        )

    monkeypatch.setattr("app.user_area.conciliar_checkout_session_stripe", _nao_deve_chamar)

    resp = client.get("/contrate-um-plano?checkout=success&session_id={CHECKOUT_SESSION_ID}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Pagamento recebido com sucesso e ativacao em processamento" in html

