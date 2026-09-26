"""Testes direcionados do lote 2 SCRUM-148: page_view + signup FunnelEvent."""
from __future__ import annotations

import importlib
import os
from types import SimpleNamespace

import pytest

from app.funnel_event_service import (
    FUNNEL_EVENT_PAGE_VIEW,
    FUNNEL_EVENT_SIGNUP_COMPLETED,
    FUNNEL_EVENT_SIGNUP_STARTED,
    FUNNEL_SOURCE_GROWTH,
    SIGNUP_METHOD_GOOGLE,
    SIGNUP_METHOD_PASSWORD,
    try_record_signup_completed,
    try_record_signup_started,
)
from app.growth_routes import growth_bp
from app.models import FunnelEvent
from tests.conftest import seed_conta_franquia_cliente, seed_usuario


def _load_web():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


@pytest.fixture
def growth_client(app, ctx):
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    if "growth" not in app.blueprints:
        app.register_blueprint(growth_bp)
    return app.test_client()


def test_page_view_accepts_anonymous(growth_client, app):
    resp = growth_client.post(
        "/api/growth/page-view",
        json={"event_id": "pv-anon-1", "page": "index"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    with app.app_context():
        event = FunnelEvent.query.filter_by(idempotency_key="pv-anon-1").one()
        assert event.event_name == FUNNEL_EVENT_PAGE_VIEW
        assert event.source == FUNNEL_SOURCE_GROWTH
        assert event.user_id is None
        assert event.conta_id is None
        assert event.franquia_id is None
        assert event.metadata_json == {"page": "index"}


def test_page_view_idempotent_same_event_id(growth_client, app):
    payload = {"event_id": "pv-idem-1", "page": "login"}
    assert growth_client.post("/api/growth/page-view", json=payload).status_code == 200
    assert growth_client.post("/api/growth/page-view", json=payload).status_code == 200

    with app.app_context():
        assert FunnelEvent.query.filter_by(idempotency_key="pv-idem-1").count() == 1


def test_page_view_accepts_content_type_and_id(growth_client, app):
    resp = growth_client.post(
        "/api/growth/page-view",
        json={
            "event_id": "pv-content-1",
            "page": "detalhe_noticia",
            "content_type": "noticia",
            "content_id": 123,
        },
    )
    assert resp.status_code == 200

    with app.app_context():
        event = FunnelEvent.query.filter_by(idempotency_key="pv-content-1").one()
        assert event.metadata_json == {
            "page": "detalhe_noticia",
            "content_type": "noticia",
            "content_id": 123,
        }
        assert FunnelEvent.query.filter_by(event_name="content_view").count() == 0


def test_page_view_ignores_client_identity(growth_client, app):
    resp = growth_client.post(
        "/api/growth/page-view",
        json={
            "event_id": "pv-forge-1",
            "page": "index",
            "user_id": 999999,
            "conta_id": 888888,
            "franquia_id": 777777,
            "email": "forge@example.com",
            "source": "hacked",
        },
    )
    assert resp.status_code == 200

    with app.app_context():
        event = FunnelEvent.query.filter_by(idempotency_key="pv-forge-1").one()
        assert event.user_id is None
        assert event.conta_id is None
        assert event.franquia_id is None
        assert event.source == FUNNEL_SOURCE_GROWTH
        assert event.metadata_json == {"page": "index"}
        assert "email" not in (event.metadata_json or {})


def test_invalid_register_still_produces_signup_started(monkeypatch):
    web = _load_web()
    monkeypatch.setattr(web, "get_active_term", lambda: None)
    calls = []

    def _capture(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(web, "try_record_signup_started", _capture)
    monkeypatch.setattr(
        web,
        "try_record_signup_completed",
        lambda *a, **k: calls.append({"completed": True}),
    )

    client = web.app.test_client()
    resp = client.post(
        "/register",
        data={
            "nome": "Usuario Teste",
            "email": "sem.termos@example.com",
            "password": "senha-segura-123",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert len(calls) == 1
    assert calls[0]["signup_method"] == SIGNUP_METHOD_PASSWORD
    assert not any(c.get("completed") for c in calls)


def test_successful_register_produces_signup_completed(monkeypatch):
    web = _load_web()
    monkeypatch.setattr(web, "get_active_term", lambda: None)
    started = []
    completed = []

    monkeypatch.setattr(
        web,
        "try_record_signup_started",
        lambda **kwargs: started.append(kwargs),
    )
    monkeypatch.setattr(
        web,
        "try_record_signup_completed",
        lambda user, **kwargs: completed.append({"user_id": user.id, **kwargs}),
    )

    fake_user = SimpleNamespace(id=42, conta_id=1, franquia_id=2)
    monkeypatch.setattr(web, "register_user", lambda *a, **k: (fake_user, None))
    monkeypatch.setattr(
        "app.services.admin_desktop_access_test_service.try_registration_replay",
        lambda **_k: None,
    )

    client = web.app.test_client()
    resp = client.post(
        "/register",
        data={
            "nome": "Usuario Ok",
            "email": "ok@example.com",
            "password": "senha-segura-123",
            "accept_terms": "1",
            "job_role": "analista",
            "usage_purpose": "trabalho",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert len(started) == 1
    assert len(completed) == 1
    assert completed[0]["user_id"] == 42
    assert completed[0]["signup_method"] == SIGNUP_METHOD_PASSWORD


def test_failed_register_does_not_produce_signup_completed(monkeypatch):
    web = _load_web()
    monkeypatch.setattr(web, "get_active_term", lambda: None)
    completed = []

    monkeypatch.setattr(web, "try_record_signup_started", lambda **_k: None)
    monkeypatch.setattr(
        web,
        "try_record_signup_completed",
        lambda *a, **k: completed.append(True),
    )
    monkeypatch.setattr(
        web,
        "register_user",
        lambda *a, **k: (None, "Este e-mail já está cadastrado."),
    )
    monkeypatch.setattr(
        "app.services.admin_desktop_access_test_service.try_registration_replay",
        lambda **_k: None,
    )

    client = web.app.test_client()
    resp = client.post(
        "/register",
        data={
            "nome": "Usuario",
            "email": "ja.existe@example.com",
            "password": "senha-segura-123",
            "accept_terms": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert completed == []


def test_oauth_existing_user_does_not_produce_signup_completed(monkeypatch):
    web = _load_web()
    completed = []
    monkeypatch.setattr(
        web,
        "try_record_signup_completed",
        lambda *a, **k: completed.append(True),
    )
    existing = SimpleNamespace(
        id=7,
        conta_id=1,
        franquia_id=1,
        is_authenticated=True,
        is_active=True,
        is_anonymous=False,
        get_id=lambda: "7",
        job_role="x",
        usage_purpose="y",
    )
    monkeypatch.setattr(
        web,
        "handle_google_oauth_callback",
        lambda *a, **k: (existing, None, False, False),
    )
    monkeypatch.setattr(web, "login_user", lambda *_a, **_k: None)
    monkeypatch.setattr(web, "_post_login_redirect", lambda *_a, **_k: web.redirect("/"))

    client = web.app.test_client()
    with client.session_transaction() as sess:
        sess["oauth_state"] = "state-1"
        sess["oauth_states"] = ["state-1"]
    resp = client.get("/login/google/callback?code=abc&state=state-1", follow_redirects=False)
    assert resp.status_code in (302, 200)
    assert completed == []


def test_oauth_new_user_produces_signup_completed(monkeypatch):
    web = _load_web()
    completed = []
    monkeypatch.setattr(
        web,
        "try_record_signup_completed",
        lambda user, **kwargs: completed.append(
            {"user_id": user.id, "signup_method": kwargs.get("signup_method")}
        ),
    )
    new_user = SimpleNamespace(
        id=99,
        conta_id=3,
        franquia_id=4,
        is_authenticated=True,
        is_active=True,
        is_anonymous=False,
        get_id=lambda: "99",
        job_role="",
        usage_purpose="",
    )
    monkeypatch.setattr(
        web,
        "handle_google_oauth_callback",
        lambda *a, **k: (new_user, None, False, True),
    )
    monkeypatch.setattr(web, "login_user", lambda *_a, **_k: None)
    monkeypatch.setattr(web, "_post_login_redirect", lambda *_a, **_k: web.redirect("/"))

    client = web.app.test_client()
    with client.session_transaction() as sess:
        sess["oauth_state"] = "state-2"
        sess["oauth_states"] = ["state-2"]
    resp = client.get("/login/google/callback?code=abc&state=state-2", follow_redirects=False)
    assert resp.status_code in (302, 200)
    assert completed == [{"user_id": 99, "signup_method": SIGNUP_METHOD_GOOGLE}]


def test_signup_completed_idempotent_per_user(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="signup-idem")
        user = seed_usuario(franquia.id, conta.id, email="signup-idem@test.com")

        try_record_signup_completed(user, signup_method=SIGNUP_METHOD_PASSWORD)
        try_record_signup_completed(user, signup_method=SIGNUP_METHOD_PASSWORD)

        rows = FunnelEvent.query.filter_by(
            event_name=FUNNEL_EVENT_SIGNUP_COMPLETED,
            user_id=user.id,
        ).all()
        assert len(rows) == 1
        assert rows[0].metadata_json == {"signup_method": SIGNUP_METHOD_PASSWORD}


def test_signup_started_persists_anonymous(app):
    with app.app_context():
        before = FunnelEvent.query.filter_by(event_name=FUNNEL_EVENT_SIGNUP_STARTED).count()
        try_record_signup_started(signup_method=SIGNUP_METHOD_PASSWORD)
        after = FunnelEvent.query.filter_by(event_name=FUNNEL_EVENT_SIGNUP_STARTED).count()
        assert after == before + 1
        event = FunnelEvent.query.filter_by(event_name=FUNNEL_EVENT_SIGNUP_STARTED).order_by(
            FunnelEvent.id.desc()
        ).first()
        assert event.user_id is None
        assert event.metadata_json == {"signup_method": SIGNUP_METHOD_PASSWORD}
        assert event.source == FUNNEL_SOURCE_GROWTH


def test_signup_method_accepted_in_metadata_allowlist(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="signup-meta")
        user = seed_usuario(franquia.id, conta.id, email="signup-meta@test.com")
        try_record_signup_completed(user, signup_method=SIGNUP_METHOD_GOOGLE)
        event = FunnelEvent.query.filter_by(
            event_name=FUNNEL_EVENT_SIGNUP_COMPLETED,
            user_id=user.id,
        ).one()
        assert event.metadata_json == {"signup_method": SIGNUP_METHOD_GOOGLE}
