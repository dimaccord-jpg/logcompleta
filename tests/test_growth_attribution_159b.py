"""Testes direcionados do Lote 159B SCRUM-159: current_session_origin seletivo."""
from __future__ import annotations

import json

from datetime import datetime
from types import SimpleNamespace

import pytest

from app.funnel_event_service import (
    FUNNEL_EVENT_CHECKOUT_STARTED,
    FUNNEL_EVENT_CTA_CLICKED,
    FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
    FUNNEL_EVENT_PAGE_VIEW,
    FUNNEL_EVENT_PAID,
    FUNNEL_EVENT_PLAN_SELECTED,
    FUNNEL_EVENT_TASK_COMPLETED,
    FUNNEL_SOURCE_CLEIDE_AUDIT,
    FUNNEL_SOURCE_GROWTH,
    FUNNEL_SOURCE_JULIA_CHAT,
    META_PIXEL_ALLOWED_EVENTS,
    TASK_TYPE_CLEIDE_AUDIT,
    TASK_TYPE_JULIA_CHAT,
    TASK_TYPE_ROBERTO_BI,
    TIPO_FATO_STRIPE_CHECKOUT_SESSION_CREATED,
    is_meta_pixel_allowed,
    try_record_funnel_event,
    try_record_growth_task_event,
)
from app.extensions import db
from app.growth_routes import growth_bp
from app.models import FunnelEvent, MonetizacaoFato
from app.services.growth_attribution_service import (
    ATTRIBUTION_VERSION,
    SESSION_KEY,
)
from tests.conftest import seed_conta_franquia_cliente, seed_usuario

META_PIXEL_BASELINE = frozenset()


def _origin(*, source: str, campaign: str | None = None) -> dict:
    out = {"source": source}
    if campaign is not None:
        out["campaign"] = campaign
    return out


def _session_attribution(*, first: dict, current: dict | None = None) -> dict:
    return {
        "version": ATTRIBUTION_VERSION,
        "first_touch": dict(first),
        "current_session_origin": dict(current or first),
    }


@pytest.fixture
def app_with_session(app, ctx):
    app.config["SECRET_KEY"] = "test-secret-159b"
    app.config["TESTING"] = True
    return app


@pytest.fixture
def growth_client(app_with_session):
    if "growth" not in app_with_session.blueprints:
        app_with_session.register_blueprint(growth_bp)
    return app_with_session.test_client()


def _auth_user(monkeypatch, *, email: str):
    conta, franquia = seed_conta_franquia_cliente(slug=f"159b-{email.split('@')[0]}")
    user = seed_usuario(franquia.id, conta.id, email=email)
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
    return user


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
        idempotency_key=f"test-159b-checkout:{session_id}",
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


def _marcos(user_id: int):
    return (
        FunnelEvent.query.filter_by(
            user_id=user_id,
            event_name=FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
        )
        .order_by(FunnelEvent.id.asc())
        .all()
    )


# --- FIRST_RELEVANT ---------------------------------------------------------


def test_first_relevant_receives_current_when_new_completion_is_first(
    app_with_session, monkeypatch
):
    with app_with_session.app_context():
        user = _auth_user(monkeypatch, email="159b-fr-match@test.com")
        with app_with_session.test_request_context("/"):
            from flask import session

            session[SESSION_KEY] = _session_attribution(
                first=_origin(source="google", campaign="launch"),
                current=_origin(source="linkedin", campaign="B"),
            )
            result = try_record_growth_task_event(
                event_name=FUNNEL_EVENT_TASK_COMPLETED,
                source=FUNNEL_SOURCE_CLEIDE_AUDIT,
                task_type=TASK_TYPE_CLEIDE_AUDIT,
                idempotency_key="growth:159b:fr:match",
                user=user,
            )
        assert result is not None
        assert result["created"] is True

        marcos = _marcos(user.id)
        assert len(marcos) == 1
        meta = marcos[0].metadata_json
        assert meta["task_type"] == TASK_TYPE_CLEIDE_AUDIT
        ga = meta["growth_attribution"]
        assert set(ga.keys()) == {"current_session_origin"}
        assert ga["current_session_origin"]["source"] == "linkedin"
        assert ga["current_session_origin"]["campaign"] == "B"
        assert "first_touch" not in ga
        assert "first_touch" not in meta


def test_first_relevant_recovery_does_not_attach_b_origin(app_with_session, monkeypatch):
    """A existe sem marco; B dispara ensure escolhendo A — sem attribution de B."""
    with app_with_session.app_context():
        user = _auth_user(monkeypatch, email="159b-fr-recovery@test.com")
        t0 = datetime(2026, 1, 1, 12, 0, 0)
        a = try_record_funnel_event(
            event_name=FUNNEL_EVENT_TASK_COMPLETED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            user_id=user.id,
            conta_id=user.conta_id,
            franquia_id=user.franquia_id,
            idempotency_key="growth:159b:fr:recovery:a",
            occurred_at=t0,
            metadata_json={"task_type": TASK_TYPE_CLEIDE_AUDIT},
        )
        assert a is not None
        assert len(_marcos(user.id)) == 0

        with app_with_session.test_request_context("/"):
            from flask import session

            session[SESSION_KEY] = _session_attribution(
                first=_origin(source="old"),
                current=_origin(source="session-b", campaign="from-b"),
            )
            b = try_record_growth_task_event(
                event_name=FUNNEL_EVENT_TASK_COMPLETED,
                source=FUNNEL_SOURCE_JULIA_CHAT,
                task_type=TASK_TYPE_JULIA_CHAT,
                idempotency_key="growth:159b:fr:recovery:b",
                user=user,
            )
        assert b is not None
        assert b["created"] is True

        marcos = _marcos(user.id)
        assert len(marcos) == 1
        assert marcos[0].metadata_json == {"task_type": TASK_TYPE_CLEIDE_AUDIT}
        assert marcos[0].occurred_at == t0
        assert "growth_attribution" not in marcos[0].metadata_json


def test_created_false_recovery_without_current_attribution(app_with_session, monkeypatch):
    with app_with_session.app_context():
        user = _auth_user(monkeypatch, email="159b-fr-created-false@test.com")
        t0 = datetime(2026, 2, 1, 10, 0, 0)
        seeded = try_record_funnel_event(
            event_name=FUNNEL_EVENT_TASK_COMPLETED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            user_id=user.id,
            conta_id=user.conta_id,
            franquia_id=user.franquia_id,
            idempotency_key="growth:159b:fr:retry:seed",
            occurred_at=t0,
            metadata_json={"task_type": TASK_TYPE_ROBERTO_BI},
        )
        assert seeded is not None
        assert len(_marcos(user.id)) == 0

        with app_with_session.test_request_context("/"):
            from flask import session

            session[SESSION_KEY] = _session_attribution(
                first=_origin(source="replay-src"),
                current=_origin(source="replay-src", campaign="should-not-attach"),
            )
            replay = try_record_growth_task_event(
                event_name=FUNNEL_EVENT_TASK_COMPLETED,
                source=FUNNEL_SOURCE_CLEIDE_AUDIT,
                task_type=TASK_TYPE_ROBERTO_BI,
                idempotency_key="growth:159b:fr:retry:seed",
                user=user,
            )
        assert replay is not None
        assert replay["created"] is False

        marcos = _marcos(user.id)
        assert len(marcos) == 1
        assert marcos[0].metadata_json == {"task_type": TASK_TYPE_ROBERTO_BI}
        assert "growth_attribution" not in marcos[0].metadata_json


def test_first_relevant_without_request_context_omits_attribution(app, ctx, monkeypatch):
    with app.app_context():
        user = _auth_user(monkeypatch, email="159b-fr-noreq@test.com")
        result = try_record_growth_task_event(
            event_name=FUNNEL_EVENT_TASK_COMPLETED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            task_type=TASK_TYPE_CLEIDE_AUDIT,
            idempotency_key="growth:159b:fr:noreq",
            user=user,
        )
        assert result is not None
        assert result["created"] is True
        marcos = _marcos(user.id)
        assert len(marcos) == 1
        assert marcos[0].metadata_json == {"task_type": TASK_TYPE_CLEIDE_AUDIT}


def test_first_relevant_snapshot_failure_still_creates_marco(app_with_session, monkeypatch):
    with app_with_session.app_context():
        user = _auth_user(monkeypatch, email="159b-fr-failopen@test.com")

        def _boom():
            raise RuntimeError("snapshot boom")

        monkeypatch.setattr(
            "app.services.growth_attribution_service.snapshot_current_session_origin_for_persist",
            _boom,
        )
        with app_with_session.test_request_context("/"):
            from flask import session

            session[SESSION_KEY] = _session_attribution(
                first=_origin(source="google"),
            )
            result = try_record_growth_task_event(
                event_name=FUNNEL_EVENT_TASK_COMPLETED,
                source=FUNNEL_SOURCE_CLEIDE_AUDIT,
                task_type=TASK_TYPE_CLEIDE_AUDIT,
                idempotency_key="growth:159b:fr:failopen",
                user=user,
            )
        assert result is not None
        assert result["created"] is True
        marcos = _marcos(user.id)
        assert len(marcos) == 1
        assert marcos[0].metadata_json == {"task_type": TASK_TYPE_CLEIDE_AUDIT}


# --- PLAN_SELECTED ----------------------------------------------------------


def test_plan_selected_with_valid_origin(growth_client, app_with_session, monkeypatch):
    with app_with_session.app_context():
        user = _auth_user(monkeypatch, email="159b-ps-ok@test.com")
        with growth_client.session_transaction() as sess:
            sess[SESSION_KEY] = _session_attribution(
                first=_origin(source="google", campaign="ft"),
                current=_origin(source="linkedin", campaign="ps"),
            )
        resp = growth_client.post(
            "/api/growth/plan-selected",
            json={"event_id": "159b-ps-ok-1", "plan": "pro"},
        )
        assert resp.status_code == 200
        row = FunnelEvent.query.filter_by(idempotency_key="159b-ps-ok-1").one()
        assert row.event_name == FUNNEL_EVENT_PLAN_SELECTED
        assert row.user_id == user.id
        meta = row.metadata_json
        assert meta["plan"] == "pro"
        assert set(meta["growth_attribution"].keys()) == {"current_session_origin"}
        assert meta["growth_attribution"]["current_session_origin"]["source"] == "linkedin"
        assert "first_touch" not in meta["growth_attribution"]


def test_plan_selected_without_origin_keeps_plan_only(
    growth_client, app_with_session, monkeypatch
):
    with app_with_session.app_context():
        _auth_user(monkeypatch, email="159b-ps-noorigin@test.com")
        resp = growth_client.post(
            "/api/growth/plan-selected",
            json={"event_id": "159b-ps-noorigin-1", "plan": "starter"},
        )
        assert resp.status_code == 200
        row = FunnelEvent.query.filter_by(idempotency_key="159b-ps-noorigin-1").one()
        assert row.metadata_json == {"plan": "starter"}


def test_plan_selected_ignores_browser_forged_attribution(
    growth_client, app_with_session, monkeypatch
):
    with app_with_session.app_context():
        _auth_user(monkeypatch, email="159b-ps-forge@test.com")
        with growth_client.session_transaction() as sess:
            sess[SESSION_KEY] = _session_attribution(
                first=_origin(source="real-ft"),
                current=_origin(source="real-current"),
            )
        resp = growth_client.post(
            "/api/growth/plan-selected",
            json={
                "event_id": "159b-ps-forge-1",
                "plan": "pro",
                "growth_attribution": {
                    "first_touch": {"source": "forged-ft"},
                    "current_session_origin": {"source": "forged-cur"},
                },
                "first_touch": {"source": "forged-top"},
                "current_session_origin": {"source": "forged-top-cur"},
            },
        )
        assert resp.status_code == 200
        row = FunnelEvent.query.filter_by(idempotency_key="159b-ps-forge-1").one()
        ga = row.metadata_json["growth_attribution"]
        assert ga["current_session_origin"]["source"] == "real-current"
        assert "first_touch" not in ga
        assert "forged" not in str(row.metadata_json)


# --- CHECKOUT_STARTED -------------------------------------------------------


def test_checkout_started_with_valid_origin(growth_client, app_with_session, monkeypatch):
    with app_with_session.app_context():
        user = _auth_user(monkeypatch, email="159b-cs-ok@test.com")
        _seed_checkout_fato(
            conta_id=user.conta_id,
            franquia_id=user.franquia_id,
            session_id="cs_test_159b_ok",
            plan="starter",
        )
        with growth_client.session_transaction() as sess:
            sess[SESSION_KEY] = _session_attribution(
                first=_origin(source="google"),
                current=_origin(source="bing", campaign="chk"),
            )
        resp = growth_client.post(
            "/api/growth/checkout-started",
            json={
                "event_id": "159b-cs-ok-1",
                "plan": "starter",
                "checkout_session_id": "cs_test_159b_ok",
            },
        )
        assert resp.status_code == 200
        row = FunnelEvent.query.filter_by(
            event_name=FUNNEL_EVENT_CHECKOUT_STARTED,
            user_id=user.id,
        ).one()
        assert row.correlation_id == "cs_test_159b_ok"
        meta = row.metadata_json
        assert meta["plan"] == "starter"
        assert set(meta["growth_attribution"].keys()) == {"current_session_origin"}
        assert meta["growth_attribution"]["current_session_origin"]["source"] == "bing"
        assert "first_touch" not in meta["growth_attribution"]


def test_checkout_started_without_origin(growth_client, app_with_session, monkeypatch):
    with app_with_session.app_context():
        user = _auth_user(monkeypatch, email="159b-cs-noorigin@test.com")
        _seed_checkout_fato(
            conta_id=user.conta_id,
            franquia_id=user.franquia_id,
            session_id="cs_test_159b_noorigin",
            plan="pro",
        )
        resp = growth_client.post(
            "/api/growth/checkout-started",
            json={
                "event_id": "159b-cs-noorigin-1",
                "plan": "pro",
                "checkout_session_id": "cs_test_159b_noorigin",
            },
        )
        assert resp.status_code == 200
        row = FunnelEvent.query.filter_by(
            correlation_id="cs_test_159b_noorigin"
        ).one()
        assert row.metadata_json == {"plan": "pro"}
        assert row.event_name == FUNNEL_EVENT_CHECKOUT_STARTED


def test_checkout_started_ignores_browser_forged_attribution(
    growth_client, app_with_session, monkeypatch
):
    with app_with_session.app_context():
        user = _auth_user(monkeypatch, email="159b-cs-forge@test.com")
        _seed_checkout_fato(
            conta_id=user.conta_id,
            franquia_id=user.franquia_id,
            session_id="cs_test_159b_forge",
            plan="multiuser",
        )
        with growth_client.session_transaction() as sess:
            sess[SESSION_KEY] = _session_attribution(
                first=_origin(source="sess-ft"),
                current=_origin(source="sess-cur"),
            )
        resp = growth_client.post(
            "/api/growth/checkout-started",
            json={
                "event_id": "159b-cs-forge-1",
                "plan": "multiuser",
                "checkout_session_id": "cs_test_159b_forge",
                "growth_attribution": {
                    "current_session_origin": {"source": "forged-cs"},
                },
                "current_session_origin": {"source": "forged-top"},
            },
        )
        assert resp.status_code == 200
        row = FunnelEvent.query.filter_by(correlation_id="cs_test_159b_forge").one()
        assert row.correlation_id == "cs_test_159b_forge"
        ga = row.metadata_json["growth_attribution"]
        assert ga["current_session_origin"]["source"] == "sess-cur"
        assert "forged" not in str(row.metadata_json)


def test_plan_and_checkout_keep_independent_origins(
    growth_client, app_with_session, monkeypatch
):
    with app_with_session.app_context():
        user = _auth_user(monkeypatch, email="159b-indep@test.com")
        _seed_checkout_fato(
            conta_id=user.conta_id,
            franquia_id=user.franquia_id,
            session_id="cs_test_159b_indep",
            plan="pro",
        )
        with growth_client.session_transaction() as sess:
            sess[SESSION_KEY] = _session_attribution(
                first=_origin(source="ft"),
                current=_origin(source="origin-a", campaign="plan"),
            )
        assert (
            growth_client.post(
                "/api/growth/plan-selected",
                json={"event_id": "159b-indep-ps", "plan": "pro"},
            ).status_code
            == 200
        )
        with growth_client.session_transaction() as sess:
            sess[SESSION_KEY] = _session_attribution(
                first=_origin(source="ft"),
                current=_origin(source="origin-b", campaign="checkout"),
            )
        assert (
            growth_client.post(
                "/api/growth/checkout-started",
                json={
                    "event_id": "159b-indep-cs",
                    "plan": "pro",
                    "checkout_session_id": "cs_test_159b_indep",
                },
            ).status_code
            == 200
        )

        ps = FunnelEvent.query.filter_by(idempotency_key="159b-indep-ps").one()
        cs = FunnelEvent.query.filter_by(correlation_id="cs_test_159b_indep").one()
        assert ps.metadata_json["growth_attribution"]["current_session_origin"]["source"] == (
            "origin-a"
        )
        assert cs.metadata_json["growth_attribution"]["current_session_origin"]["source"] == (
            "origin-b"
        )
        assert "first_touch" not in ps.metadata_json["growth_attribution"]
        assert "first_touch" not in cs.metadata_json["growth_attribution"]


# --- EXCLUSIONS -------------------------------------------------------------


def test_task_completed_does_not_receive_generic_attribution(app_with_session, monkeypatch):
    with app_with_session.app_context():
        user = _auth_user(monkeypatch, email="159b-ex-task@test.com")
        with app_with_session.test_request_context("/"):
            from flask import session

            session[SESSION_KEY] = _session_attribution(
                first=_origin(source="google"),
                current=_origin(source="linkedin"),
            )
            result = try_record_growth_task_event(
                event_name=FUNNEL_EVENT_TASK_COMPLETED,
                source=FUNNEL_SOURCE_CLEIDE_AUDIT,
                task_type=TASK_TYPE_CLEIDE_AUDIT,
                idempotency_key="growth:159b:ex:task",
                user=user,
            )
        assert result is not None
        task = result["event"]
        assert task.event_name == FUNNEL_EVENT_TASK_COMPLETED
        assert task.metadata_json == {"task_type": TASK_TYPE_CLEIDE_AUDIT}
        assert "growth_attribution" not in task.metadata_json


def test_page_view_and_cta_clicked_not_enriched(
    growth_client, app_with_session, monkeypatch
):
    with app_with_session.app_context():
        _auth_user(monkeypatch, email="159b-ex-pv@test.com")
        with growth_client.session_transaction() as sess:
            sess[SESSION_KEY] = _session_attribution(
                first=_origin(source="google"),
                current=_origin(source="linkedin"),
            )
        assert (
            growth_client.post(
                "/api/growth/page-view",
                json={"event_id": "159b-ex-pv-1", "page": "index"},
            ).status_code
            == 200
        )
        assert (
            growth_client.post(
                "/api/growth/cta-clicked",
                json={
                    "event_id": "159b-ex-cta-1",
                    "cta_id": "nav_login_cadastro",
                },
            ).status_code
            == 200
        )
        pv = FunnelEvent.query.filter_by(idempotency_key="159b-ex-pv-1").one()
        cta = FunnelEvent.query.filter_by(idempotency_key="159b-ex-cta-1").one()
        assert pv.event_name == FUNNEL_EVENT_PAGE_VIEW
        assert pv.metadata_json == {"page": "index"}
        assert cta.event_name == FUNNEL_EVENT_CTA_CLICKED
        assert cta.metadata_json == {"cta_id": "nav_login_cadastro"}


def test_paid_path_unchanged_no_attribution_in_ensure(app, ctx, monkeypatch):
    """paid continua sem growth_attribution neste lote (ensure nao enriquecido)."""
    import inspect

    from app import funnel_event_service as fes

    src = inspect.getsource(fes.try_ensure_growth_paid_for_conta)
    assert "growth_attribution" not in src
    assert "snapshot_growth_attribution" not in src
    assert "current_session_origin" not in src
    assert FUNNEL_EVENT_PAID not in META_PIXEL_ALLOWED_EVENTS
    assert is_meta_pixel_allowed(FUNNEL_EVENT_PAID) is False


def test_meta_pixel_allowlist_unchanged():
    assert META_PIXEL_ALLOWED_EVENTS == META_PIXEL_BASELINE
    assert FUNNEL_EVENT_PLAN_SELECTED not in META_PIXEL_ALLOWED_EVENTS
    assert FUNNEL_EVENT_CHECKOUT_STARTED not in META_PIXEL_ALLOWED_EVENTS
    assert FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED not in META_PIXEL_ALLOWED_EVENTS
    assert is_meta_pixel_allowed(FUNNEL_EVENT_PLAN_SELECTED) is False
    assert is_meta_pixel_allowed(FUNNEL_EVENT_CHECKOUT_STARTED) is False
