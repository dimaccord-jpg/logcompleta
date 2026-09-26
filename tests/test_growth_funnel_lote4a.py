"""Testes direcionados do Lote 4A SCRUM-148: plan_selected + checkout_started."""
from __future__ import annotations

import json
import pathlib
import re
from types import SimpleNamespace

import pytest

from app.extensions import db
from app.funnel_event_service import (
    FUNNEL_EVENT_CHECKOUT_STARTED,
    FUNNEL_EVENT_PLAN_SELECTED,
    FUNNEL_SOURCE_GROWTH,
    META_PIXEL_ALLOWED_EVENTS,
    TIPO_FATO_STRIPE_CHECKOUT_SESSION_CREATED,
    is_meta_pixel_allowed,
)
from app.growth_routes import (
    _checkout_started_idempotency_key,
    growth_bp,
)
from app.models import FunnelEvent, MonetizacaoFato
from tests.conftest import seed_conta_franquia_cliente, seed_usuario

CONTRATE_PLANO = pathlib.Path("app/templates/contrate_plano.html")
USER_AREA = pathlib.Path("app/user_area.py")
MONETIZACAO = pathlib.Path("app/services/cleiton_monetizacao_service.py")


@pytest.fixture
def growth_client(app, ctx):
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    if "growth" not in app.blueprints:
        app.register_blueprint(growth_bp)
    return app.test_client()


def _auth_user(monkeypatch, *, email: str, with_conta: bool = True):
    if with_conta:
        conta, franquia = seed_conta_franquia_cliente(slug=f"lote4a-{email.split('@')[0]}")
        user = seed_usuario(franquia.id, conta.id, email=email)
        fake = SimpleNamespace(
            is_authenticated=True,
            id=user.id,
            conta_id=conta.id,
            franquia_id=franquia.id,
        )
    else:
        conta, franquia = seed_conta_franquia_cliente(slug=f"lote4a-nc-{email.split('@')[0]}")
        user = seed_usuario(franquia.id, conta.id, email=email)
        fake = SimpleNamespace(
            is_authenticated=True,
            id=user.id,
            conta_id=None,
            franquia_id=franquia.id,
        )
    monkeypatch.setattr("flask_login.utils._get_user", lambda: fake)
    monkeypatch.setattr(
        "app.funnel_event_service._is_desktop_access_admin_test_mode",
        lambda: False,
    )
    return user, fake


def _seed_checkout_fato(*, conta_id: int, session_id: str, plan: str, franquia_id=None):
    """Espelha stripe_checkout_session_created persistido pelo backend comercial."""
    fato = MonetizacaoFato(
        tipo_fato=TIPO_FATO_STRIPE_CHECKOUT_SESSION_CREATED,
        status_tecnico="success",
        provider="stripe",
        conta_id=int(conta_id),
        franquia_id=int(franquia_id) if franquia_id is not None else None,
        correlation_key=session_id,
        external_event_id=session_id,
        idempotency_key=f"test-checkout:{session_id}",
        snapshot_normalizado_json=json.dumps(
            {
                "checkout_session_id": session_id,
                "plano_interno": plan,
                "conta_id": int(conta_id),
            }
        ),
    )
    db.session.add(fato)
    db.session.commit()
    return fato


def _anon(monkeypatch):
    monkeypatch.setattr(
        "flask_login.utils._get_user",
        lambda: SimpleNamespace(is_authenticated=False),
    )


# --- PLAN_SELECTED ---------------------------------------------------------


@pytest.mark.parametrize("plan", ["starter", "pro", "multiuser"])
def test_plan_selected_accepts_allowed_plans(growth_client, app, monkeypatch, plan):
    with app.app_context():
        user, _fake = _auth_user(monkeypatch, email=f"ps-ok-{plan}@test.com")
        event_id = f"ps-ok-{plan}-1"
        resp = growth_client.post(
            "/api/growth/plan-selected",
            json={"event_id": event_id, "plan": plan},
        )
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

        row = FunnelEvent.query.filter_by(idempotency_key=event_id).one()
        assert row.event_name == FUNNEL_EVENT_PLAN_SELECTED
        assert row.source == FUNNEL_SOURCE_GROWTH
        assert row.user_id == user.id
        assert row.metadata_json == {"plan": plan}
        assert is_meta_pixel_allowed(row.event_name) is False
        assert row.event_name not in META_PIXEL_ALLOWED_EVENTS


@pytest.mark.parametrize("plan", ["free", "enterprise", "premium", "basic"])
def test_plan_selected_rejects_free_and_other(growth_client, app, monkeypatch, plan):
    with app.app_context():
        _auth_user(monkeypatch, email=f"ps-bad-{plan}@test.com")
        before = FunnelEvent.query.filter_by(event_name=FUNNEL_EVENT_PLAN_SELECTED).count()
        resp = growth_client.post(
            "/api/growth/plan-selected",
            json={"event_id": f"ps-bad-{plan}", "plan": plan},
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "plan_invalid"
        assert (
            FunnelEvent.query.filter_by(event_name=FUNNEL_EVENT_PLAN_SELECTED).count()
            == before
        )


def test_plan_selected_ignores_client_identity(growth_client, app, monkeypatch):
    with app.app_context():
        user, _fake = _auth_user(monkeypatch, email="ps-forge@test.com")
        resp = growth_client.post(
            "/api/growth/plan-selected",
            json={
                "event_id": "ps-forge-1",
                "plan": "pro",
                "user_id": 999999,
                "conta_id": 888888,
                "franquia_id": 777777,
                "email": "forge@example.com",
                "source": "hacked",
            },
        )
        assert resp.status_code == 200
        row = FunnelEvent.query.filter_by(idempotency_key="ps-forge-1").one()
        assert row.user_id == user.id
        assert row.conta_id == user.conta_id
        assert row.franquia_id == user.franquia_id
        assert row.source == FUNNEL_SOURCE_GROWTH
        assert row.metadata_json == {"plan": "pro"}
        assert "email" not in (row.metadata_json or {})


def test_plan_selected_same_event_id_does_not_duplicate(growth_client, app, monkeypatch):
    with app.app_context():
        _auth_user(monkeypatch, email="ps-idem@test.com")
        payload = {"event_id": "ps-idem-1", "plan": "starter"}
        assert growth_client.post("/api/growth/plan-selected", json=payload).status_code == 200
        assert growth_client.post("/api/growth/plan-selected", json=payload).status_code == 200
        assert FunnelEvent.query.filter_by(idempotency_key="ps-idem-1").count() == 1


def test_plan_selected_requires_auth(growth_client, app, monkeypatch):
    with app.app_context():
        _anon(monkeypatch)
        before = FunnelEvent.query.count()
        resp = growth_client.post(
            "/api/growth/plan-selected",
            json={"event_id": "ps-anon-1", "plan": "starter"},
        )
        assert resp.status_code == 401
        assert FunnelEvent.query.count() == before


def test_multiuser_plan_selected_js_fires_without_checkout():
    """Abertura do formulario Multiuser conta como plan_selected; nao espera checkout."""
    src = CONTRATE_PLANO.read_text(encoding="utf-8")
    assert "function trackPlanSelected" in src
    assert "urlGrowthPlanSelected" in src
    click = src[
        src.index("botoes.forEach((btn) => {") : src.index(
            "if (formMultiuser) {\n        formMultiuser.addEventListener"
        )
    ]
    assert "trackPlanSelected(planoNormalizado)" in click
    assert click.index("trackPlanSelected(planoNormalizado)") < click.index(
        'if (planoNormalizado === "multiuser")'
    )
    multiuser = click[
        click.index('if (planoNormalizado === "multiuser")') : click.index(
            "if (formMultiuser) {\n                formMultiuser.classList.add"
        )
    ]
    assert "iniciarCheckout(" not in multiuser
    assert "invalidarCheckoutPendente()" in multiuser
    assert "trackCheckoutStarted" not in multiuser


# --- CHECKOUT_STARTED ------------------------------------------------------


def test_checkout_started_accepts_valid_payload(growth_client, app, monkeypatch):
    with app.app_context():
        user, _fake = _auth_user(monkeypatch, email="cs-ok@test.com")
        event_id = "cs-ok-occ-1"
        session_id = "cs_test_abc123"
        _seed_checkout_fato(
            conta_id=user.conta_id,
            session_id=session_id,
            plan="starter",
            franquia_id=user.franquia_id,
        )
        resp = growth_client.post(
            "/api/growth/checkout-started",
            json={
                "event_id": event_id,
                "plan": "starter",
                "checkout_session_id": session_id,
            },
        )
        assert resp.status_code == 200
        key = _checkout_started_idempotency_key(conta_id=user.conta_id, event_id=event_id)
        row = FunnelEvent.query.filter_by(idempotency_key=key).one()
        assert row.event_name == FUNNEL_EVENT_CHECKOUT_STARTED
        assert row.source == FUNNEL_SOURCE_GROWTH
        assert row.user_id == user.id
        assert row.conta_id == user.conta_id
        assert row.correlation_id == session_id
        assert row.metadata_json == {"plan": "starter"}
        assert "checkout_session_id" not in (row.metadata_json or {})
        assert is_meta_pixel_allowed(row.event_name) is False


def test_checkout_started_invented_session_no_event(growth_client, app, monkeypatch):
    with app.app_context():
        user, _fake = _auth_user(monkeypatch, email="cs-invented@test.com")
        before = FunnelEvent.query.filter_by(event_name=FUNNEL_EVENT_CHECKOUT_STARTED).count()
        resp = growth_client.post(
            "/api/growth/checkout-started",
            json={
                "event_id": "cs-invented-1",
                "plan": "starter",
                "checkout_session_id": "cs_invented_no_fato",
            },
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body == {"ok": True}
        assert (
            FunnelEvent.query.filter_by(event_name=FUNNEL_EVENT_CHECKOUT_STARTED).count()
            == before
        )


def test_checkout_started_other_account_rejected(growth_client, app, monkeypatch):
    with app.app_context():
        other_conta, other_fr = seed_conta_franquia_cliente(slug="lote4a-other-cs")
        _seed_checkout_fato(
            conta_id=other_conta.id,
            session_id="cs_other_acct",
            plan="pro",
            franquia_id=other_fr.id,
        )
        user, _fake = _auth_user(monkeypatch, email="cs-other@test.com")
        before = FunnelEvent.query.filter_by(
            event_name=FUNNEL_EVENT_CHECKOUT_STARTED,
            conta_id=user.conta_id,
        ).count()
        resp = growth_client.post(
            "/api/growth/checkout-started",
            json={
                "event_id": "cs-other-1",
                "plan": "pro",
                "checkout_session_id": "cs_other_acct",
            },
        )
        assert resp.status_code == 200
        assert resp.get_json() == {"ok": True}
        assert (
            FunnelEvent.query.filter_by(
                event_name=FUNNEL_EVENT_CHECKOUT_STARTED,
                conta_id=user.conta_id,
            ).count()
            == before
        )


def test_checkout_started_plan_mismatch_rejected(growth_client, app, monkeypatch):
    with app.app_context():
        user, _fake = _auth_user(monkeypatch, email="cs-plan-mismatch@test.com")
        _seed_checkout_fato(
            conta_id=user.conta_id,
            session_id="cs_plan_mismatch",
            plan="starter",
            franquia_id=user.franquia_id,
        )
        before = FunnelEvent.query.filter_by(event_name=FUNNEL_EVENT_CHECKOUT_STARTED).count()
        resp = growth_client.post(
            "/api/growth/checkout-started",
            json={
                "event_id": "cs-plan-mm-1",
                "plan": "pro",
                "checkout_session_id": "cs_plan_mismatch",
            },
        )
        assert resp.status_code == 200
        assert resp.get_json() == {"ok": True}
        assert (
            FunnelEvent.query.filter_by(event_name=FUNNEL_EVENT_CHECKOUT_STARTED).count()
            == before
        )


def test_checkout_started_reused_session_still_records(growth_client, app, monkeypatch):
    """Mesma Session Stripe em duas disponibilizacoes = duas ocorrencias."""
    with app.app_context():
        user, _fake = _auth_user(monkeypatch, email="cs-reuse@test.com")
        session_id = "cs_reused_same"
        _seed_checkout_fato(
            conta_id=user.conta_id,
            session_id=session_id,
            plan="multiuser",
            franquia_id=user.franquia_id,
        )
        for occ in ("occ-a", "occ-b"):
            resp = growth_client.post(
                "/api/growth/checkout-started",
                json={
                    "event_id": occ,
                    "plan": "multiuser",
                    "checkout_session_id": session_id,
                },
            )
            assert resp.status_code == 200

        rows = (
            FunnelEvent.query.filter_by(
                event_name=FUNNEL_EVENT_CHECKOUT_STARTED,
                conta_id=user.conta_id,
            )
            .order_by(FunnelEvent.id.asc())
            .all()
        )
        assert len(rows) == 2
        assert rows[0].correlation_id == session_id
        assert rows[1].correlation_id == session_id
        assert rows[0].idempotency_key != rows[1].idempotency_key


def test_checkout_started_same_occurrence_idempotent(growth_client, app, monkeypatch):
    with app.app_context():
        user, _fake = _auth_user(monkeypatch, email="cs-idem@test.com")
        payload = {
            "event_id": "cs-idem-occ",
            "plan": "pro",
            "checkout_session_id": "cs_idem_1",
        }
        _seed_checkout_fato(
            conta_id=user.conta_id,
            session_id="cs_idem_1",
            plan="pro",
            franquia_id=user.franquia_id,
        )
        assert growth_client.post("/api/growth/checkout-started", json=payload).status_code == 200
        assert growth_client.post("/api/growth/checkout-started", json=payload).status_code == 200
        key = _checkout_started_idempotency_key(
            conta_id=user.conta_id, event_id="cs-idem-occ"
        )
        assert FunnelEvent.query.filter_by(idempotency_key=key).count() == 1


def test_checkout_started_rejects_invalid_plan(growth_client, app, monkeypatch):
    with app.app_context():
        _auth_user(monkeypatch, email="cs-plan@test.com")
        resp = growth_client.post(
            "/api/growth/checkout-started",
            json={
                "event_id": "cs-plan-1",
                "plan": "free",
                "checkout_session_id": "cs_x",
            },
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "plan_invalid"


def test_checkout_started_ignores_client_identity_and_secrets(growth_client, app, monkeypatch):
    with app.app_context():
        user, _fake = _auth_user(monkeypatch, email="cs-forge@test.com")
        _seed_checkout_fato(
            conta_id=user.conta_id,
            session_id="cs_forge",
            plan="starter",
            franquia_id=user.franquia_id,
        )
        resp = growth_client.post(
            "/api/growth/checkout-started",
            json={
                "event_id": "cs-forge-1",
                "plan": "starter",
                "checkout_session_id": "cs_forge",
                "user_id": 1,
                "conta_id": 2,
                "franquia_id": 3,
                "client_secret": "secret_should_never_persist",
                "publishable_key": "pk_test_x",
                "amount": 9900,
                "email": "x@y.com",
            },
        )
        assert resp.status_code == 200
        key = _checkout_started_idempotency_key(conta_id=user.conta_id, event_id="cs-forge-1")
        row = FunnelEvent.query.filter_by(idempotency_key=key).one()
        assert row.user_id == user.id
        assert row.conta_id == user.conta_id
        assert row.metadata_json == {"plan": "starter"}
        blob = str(row.metadata_json) + str(row.correlation_id)
        assert "secret" not in blob
        assert "pk_test" not in blob
        assert "9900" not in blob
        assert "x@y.com" not in blob


def test_checkout_started_requires_conta(growth_client, app, monkeypatch):
    with app.app_context():
        _auth_user(monkeypatch, email="cs-noconta@test.com", with_conta=False)
        before = FunnelEvent.query.filter_by(event_name=FUNNEL_EVENT_CHECKOUT_STARTED).count()
        resp = growth_client.post(
            "/api/growth/checkout-started",
            json={
                "event_id": "cs-noconta-1",
                "plan": "pro",
                "checkout_session_id": "cs_nc",
            },
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "conta_required"
        assert (
            FunnelEvent.query.filter_by(event_name=FUNNEL_EVENT_CHECKOUT_STARTED).count()
            == before
        )


def test_js_checkout_started_only_on_valid_commercial_response():
    src = CONTRATE_PLANO.read_text(encoding="utf-8")
    assert "function trackCheckoutStarted" in src
    assert "function respostaCheckoutEmbeddedDisponivel" in src
    assert "urlGrowthCheckoutStarted" in src

    fn = src[
        src.index("async function iniciarCheckout") : src.index("function coletarDadosMultiuser")
    ]
    assert "trackCheckoutStarted(planoCodigo, payload.checkout_session_id)" in fn
    assert "respostaCheckoutEmbeddedDisponivel(payload)" in fn

    # Condicao exige os 3 campos e exclui downgrade / sem checkout
    cond = src[
        src.index("function respostaCheckoutEmbeddedDisponivel") : src.index(
            "function limparCheckoutEmbedded"
        )
    ]
    assert "checkout_session_id" in cond
    assert "checkout_client_secret" in cond
    assert "publishable_key" in cond
    assert "downgrade_agendado" in cond
    assert "assinatura_atualizada_sem_checkout" in cond

    # Disparo apos early-returns de downgrade / sem checkout
    assert fn.index("if (payload.downgrade_agendado)") < fn.index("trackCheckoutStarted")
    assert fn.index("if (payload.assinatura_atualizada_sem_checkout)") < fn.index(
        "trackCheckoutStarted"
    )
    # Meta InitiateCheckout antecipado removido (157A): so via Growth external_event
    assert 'window.LogCompletaPixel.track("InitiateCheckout")' not in fn
    assert "external_event" in src
    assert "AFExternalTracking.dispatch" in src


def test_js_growth_fail_open_silent():
    src = CONTRATE_PLANO.read_text(encoding="utf-8")
    send = src[
        src.index("function sendGrowthEvent") : src.index("function trackPlanSelected")
    ]
    assert ".catch(function () {})" in send
    assert "keepalive: true" in send


def test_growth_not_inside_commercial_transaction():
    """Nenhum tracking Growth dentro do endpoint/servico comercial Stripe."""
    user_area = USER_AREA.read_text(encoding="utf-8")
    monet = MONETIZACAO.read_text(encoding="utf-8")
    assert "try_record_funnel_event" not in user_area
    assert "plan_selected" not in user_area
    assert "checkout_started" not in user_area
    assert "FUNNEL_EVENT_CHECKOUT_STARTED" not in monet
    assert "FUNNEL_EVENT_PLAN_SELECTED" not in monet
    assert "try_record_funnel_event" not in monet


def test_growth_fail_open_does_not_alter_checkout_contract(growth_client, app, monkeypatch):
    """Falha de persistencia Growth ainda responde ok e nao propaga."""
    with app.app_context():
        user, _fake = _auth_user(monkeypatch, email="cs-failopen@test.com")
        _seed_checkout_fato(
            conta_id=user.conta_id,
            session_id="cs_fail",
            plan="starter",
            franquia_id=user.franquia_id,
        )
        monkeypatch.setattr(
            "app.growth_routes.try_record_funnel_event",
            lambda **_k: None,
        )
        resp = growth_client.post(
            "/api/growth/checkout-started",
            json={
                "event_id": "cs-failopen-1",
                "plan": "starter",
                "checkout_session_id": "cs_fail",
            },
        )
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True


def test_meta_pixel_allowlist_unchanged():
    assert META_PIXEL_ALLOWED_EVENTS == set() or META_PIXEL_ALLOWED_EVENTS == frozenset()
    assert is_meta_pixel_allowed("plan_selected") is False
    assert is_meta_pixel_allowed("checkout_started") is False
    assert is_meta_pixel_allowed("paid") is False
    assert is_meta_pixel_allowed("file_uploaded") is False
    assert is_meta_pixel_allowed("freight_calculated") is False


def test_paid_not_implemented_in_lote4a():
    routes = pathlib.Path("app/growth_routes.py").read_text(encoding="utf-8")
    assert "/api/growth/paid" not in routes
    assert "FUNNEL_EVENT_PAID" not in routes
    src = CONTRATE_PLANO.read_text(encoding="utf-8")
    assert "trackPaid" not in src
    assert "paid" not in re.findall(r"track\w*Paid|growth/paid", src)
