"""Integração: commit comercial de stripe_checkout_session_created → Growth.

Reproduz o bloqueante de homologação: o fato precisa estar commitado antes da
resposta comercial, senão /api/growth/checkout-started não encontra a prova.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.extensions import db
from app.funnel_event_service import (
    FUNNEL_EVENT_CHECKOUT_STARTED,
    FUNNEL_SOURCE_GROWTH,
    TIPO_FATO_STRIPE_CHECKOUT_SESSION_CREATED,
)
from app.growth_routes import _checkout_started_idempotency_key, growth_bp
from app.models import FunnelEvent, MonetizacaoFato
from app.privacy_marketing import PRIVACY_MARKETING_COOKIE_NAME
from app.services import cleiton_monetizacao_service as monetizacao_service
from app.services import growth_external_server_service as capi
from app.services.cleiton_monetizacao_service import iniciar_jornada_assinatura_stripe
from tests.conftest import seed_conta_franquia_cliente, seed_usuario

PIXEL_ID = "meta_pixel_checkout_commit"
ACCESS_TOKEN = "test_capi_token_checkout_commit"
GRAPH_V = "v26.0"
PUBLIC_BASE = "https://www.agentefrete.com.br"
FBP_OK = "fb.1.1234567890.2222222222"


@pytest.fixture
def growth_client(app, ctx):
    app.config["SECRET_KEY"] = "test-secret-checkout-commit"
    app.config["TESTING"] = True
    app.config["FACEBOOK_PIXEL_ID"] = PIXEL_ID
    app.config["META_CAPI_ENABLED"] = True
    app.config["META_CAPI_ACCESS_TOKEN"] = ACCESS_TOKEN
    app.config["META_GRAPH_API_VERSION"] = GRAPH_V
    app.config["META_CAPI_TEST_EVENT_CODE"] = ""
    app.config["PUBLIC_BASE_URL"] = PUBLIC_BASE
    if "growth" not in app.blueprints:
        app.register_blueprint(growth_bp)
    return app.test_client()


def _set_privacy(client, value: str) -> None:
    try:
        client.set_cookie(PRIVACY_MARKETING_COOKIE_NAME, value)
    except TypeError:
        client.set_cookie("localhost", PRIVACY_MARKETING_COOKIE_NAME, value)


def _set_cookie(client, name: str, value: str) -> None:
    try:
        client.set_cookie(name, value)
    except TypeError:
        client.set_cookie("localhost", name, value)


def _auth_user(monkeypatch, *, email: str):
    conta, franquia = seed_conta_franquia_cliente(slug=f"cs-commit-{email.split('@')[0]}")
    user = seed_usuario(franquia.id, conta.id, email=email, categoria="free")
    fake = SimpleNamespace(
        is_authenticated=True,
        id=user.id,
        conta_id=conta.id,
        franquia_id=franquia.id,
    )
    monkeypatch.setattr("flask_login.utils._get_user", lambda: fake)
    monkeypatch.setattr(
        "app.funnel_event_service._is_desktop_access_admin_test_mode",
        lambda: False,
    )
    return user, fake


def _mock_stripe_checkout_real(monkeypatch, *, session_id: str = "cs_comercial_real_1"):
    monkeypatch.setattr(
        monetizacao_service.plano_service,
        "obter_configuracao_gateway_plano_admin",
        lambda plano: {"configuracao_valida": True, "price_id": f"price_{plano}"},
    )
    monkeypatch.setattr(
        monetizacao_service, "_obter_publishable_key_stripe", lambda: "pk_test_commit"
    )
    monkeypatch.setattr(
        monetizacao_service, "_obter_assinatura_stripe_ativa", lambda conta_id: None
    )

    def _fake_post(path, payload, idempotency_key=None):  # noqa: ARG001
        assert path == "/checkout/sessions"
        return {
            "id": session_id,
            "client_secret": f"{session_id}_secret",
            "status": "open",
            "payment_status": "unpaid",
            "customer": None,
            "subscription": None,
            "client_reference_id": payload.get("client_reference_id"),
        }

    monkeypatch.setattr(monetizacao_service, "_stripe_post", _fake_post)
    return session_id


def _enable_capi_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "homolog")
    monkeypatch.setenv("META_CAPI_ENABLED", "true")
    monkeypatch.setenv("FACEBOOK_PIXEL_ID", PIXEL_ID)
    monkeypatch.setenv("META_CAPI_ACCESS_TOKEN", ACCESS_TOKEN)
    monkeypatch.setenv("META_GRAPH_API_VERSION", GRAPH_V)
    monkeypatch.setenv("PUBLIC_BASE_URL", PUBLIC_BASE)
    monkeypatch.delenv("META_CAPI_TEST_EVENT_CODE", raising=False)


def test_produtor_comercial_commit_visivel_em_transacao_independente_e_growth(
    growth_client, app, monkeypatch
):
    """Fluxo real: Stripe mock → iniciar_jornada → commit → nova sessão → Growth+CAPI."""
    _enable_capi_env(monkeypatch)
    session_id = "cs_comercial_real_commit_1"

    with app.app_context():
        user, _fake = _auth_user(monkeypatch, email="cs-commit-ok@test.com")
        conta_id = int(user.conta_id)
        franquia_id = int(user.franquia_id)
        _mock_stripe_checkout_real(monkeypatch, session_id=session_id)

        out = iniciar_jornada_assinatura_stripe(user=user, plano_codigo="starter")
        assert out["checkout_session_id"] == session_id
        assert out["checkout_client_secret"]
        assert out["publishable_key"]

        # Fim da request comercial: sessão ORM descartada (sem commit corretivo aqui).
        db.session.remove()

        # Transação/sessão independente deve enxergar o MonetizacaoFato.
        fato = (
            MonetizacaoFato.query.filter(
                MonetizacaoFato.tipo_fato == TIPO_FATO_STRIPE_CHECKOUT_SESSION_CREATED,
                MonetizacaoFato.conta_id == conta_id,
                db.or_(
                    MonetizacaoFato.correlation_key == session_id,
                    MonetizacaoFato.external_event_id == session_id,
                ),
            )
            .order_by(MonetizacaoFato.id.desc())
            .first()
        )
        assert fato is not None
        assert fato.franquia_id == franquia_id

        _set_privacy(growth_client, "v1:accepted")
        _set_cookie(growth_client, "_fbp", FBP_OK)
        event_id = "cs-commit-occ-1"
        with patch.object(capi.requests, "post") as post:
            post.return_value = MagicMock(status_code=200)
            resp = growth_client.post(
                "/api/growth/checkout-started",
                json={
                    "event_id": event_id,
                    "plan": "starter",
                    "checkout_session_id": session_id,
                },
            )
            assert resp.status_code == 200
            body = resp.get_json()
            assert body["ok"] is True
            ext = body.get("external_event")
            assert ext is not None
            assert ext["event"] == FUNNEL_EVENT_CHECKOUT_STARTED
            assert ext["params"] == {"plan": "starter"}
            post.assert_called_once()

        key = _checkout_started_idempotency_key(conta_id=conta_id, event_id=event_id)
        row = FunnelEvent.query.filter_by(idempotency_key=key).one()
        assert row.event_name == FUNNEL_EVENT_CHECKOUT_STARTED
        assert row.source == FUNNEL_SOURCE_GROWTH
        assert row.conta_id == conta_id
        assert row.correlation_id == session_id


def test_commit_fato_falha_nao_retorna_checkout(app, monkeypatch):
    """Falha no commit comercial: não devolve checkout; não cria 2ª sessão Stripe."""
    from sqlalchemy.orm import Session

    with app.app_context():
        user, _ = _auth_user(monkeypatch, email="cs-commit-fail@test.com")
        session_id = "cs_comercial_commit_fail"
        stripe_calls = {"n": 0}

        monkeypatch.setattr(
            monetizacao_service.plano_service,
            "obter_configuracao_gateway_plano_admin",
            lambda plano: {"configuracao_valida": True, "price_id": f"price_{plano}"},
        )
        monkeypatch.setattr(
            monetizacao_service, "_obter_publishable_key_stripe", lambda: "pk_test_commit"
        )
        monkeypatch.setattr(
            monetizacao_service, "_obter_assinatura_stripe_ativa", lambda conta_id: None
        )

        def _fake_post(path, payload, idempotency_key=None):  # noqa: ARG001
            stripe_calls["n"] += 1
            assert path == "/checkout/sessions"
            return {
                "id": session_id,
                "client_secret": f"{session_id}_secret",
                "status": "open",
                "payment_status": "unpaid",
            }

        monkeypatch.setattr(monetizacao_service, "_stripe_post", _fake_post)

        real_rollback = Session.rollback

        def _failing_commit(self):
            real_rollback(self)
            raise RuntimeError("commit_monetizacao_fato_falhou")

        monkeypatch.setattr(Session, "commit", _failing_commit)

        with pytest.raises(RuntimeError, match="commit_monetizacao_fato_falhou") as exc_info:
            out = iniciar_jornada_assinatura_stripe(user=user, plano_codigo="starter")
            # Se chegar aqui, o checkout teria sido devolvido indevidamente.
            assert False, f"checkout retornado apos falha de commit: {out!r}"

        assert "commit_monetizacao_fato_falhou" in str(exc_info.value)
        assert stripe_calls["n"] == 1
