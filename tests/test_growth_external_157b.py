"""Testes direcionados SCRUM-157 Lote 157B: first_relevant externo + remocao Audit* Meta."""
from __future__ import annotations

import importlib
import os
import pathlib
import re
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.extensions import db
from app.funnel_event_service import (
    FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
    FUNNEL_EVENT_TASK_COMPLETED,
    FUNNEL_SOURCE_CLEIDE_AUDIT,
    META_PIXEL_ALLOWED_EVENTS,
    TASK_TYPE_CLEIDE_AUDIT,
    TASK_TYPE_JULIA_CHAT,
    _ensure_first_relevant_task_completed,
    is_meta_pixel_allowed,
    try_record_funnel_event,
    try_record_growth_task_event,
)
from app.models import FunnelEvent
from app.privacy_marketing import (
    PRIVACY_MARKETING_COOKIE_NAME,
    SESSION_EXTERNAL_EVENT_PENDING,
    SESSION_EXTERNAL_EVENT_PENDING_FIRST_RELEVANT,
    discard_pending_marketing_pixel_flags,
    pop_all_pending_external_events_if_allowed,
    store_pending_external_event,
    store_pending_first_relevant_external_event,
)
from app.services.growth_external_event_service import (
    EXTERNAL_BROWSER_ALLOWED_EVENTS,
    build_external_event_envelope,
    build_external_event_token,
    is_minimal_external_envelope,
)
from tests.conftest import seed_conta_franquia_cliente, seed_usuario

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXTERNAL_JS = ROOT / "app/static/js/af_external_tracking.js"
PIXEL_EVENTS = ROOT / "app/templates/partials/pixel_events.html"
ACESSO_DESKTOP = ROOT / "app/templates/acesso_desktop.html"
BASE_HTML = ROOT / "app/templates/base.html"


def _auth_user(monkeypatch, *, email: str):
    conta, franquia = seed_conta_franquia_cliente(slug=f"157b-{email.split('@')[0]}")
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
    return user, fake


def _pending_first_relevant(sess) -> dict | None:
    raw = sess.get(SESSION_EXTERNAL_EVENT_PENDING_FIRST_RELEVANT)
    return raw if isinstance(raw, dict) else None


def _set_privacy(client, value: str) -> None:
    try:
        client.set_cookie(PRIVACY_MARKETING_COOKIE_NAME, value)
    except TypeError:
        client.set_cookie("localhost", PRIVACY_MARKETING_COOKIE_NAME, value)


def _load_web():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret-157b")
    return importlib.import_module("app.web")


@pytest.fixture
def web(monkeypatch):
    module = _load_web()
    monkeypatch.setattr(module, "current_user", SimpleNamespace(is_authenticated=False))
    monkeypatch.setattr(module, "get_julia_chat_max_history", lambda: 10)
    monkeypatch.setattr(
        module,
        "avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True},
    )
    monkeypatch.setattr(module, "get_active_term", lambda: None)
    original = {
        "FACEBOOK_PIXEL_ID": module.app.config.get("FACEBOOK_PIXEL_ID"),
        "GOOGLE_ANALYTICS_MEASUREMENT_ID": module.app.config.get(
            "GOOGLE_ANALYTICS_MEASUREMENT_ID"
        ),
        "SECRET_KEY": module.app.config.get("SECRET_KEY"),
    }
    module.app.config["FACEBOOK_PIXEL_ID"] = "meta_pixel_157b"
    module.app.config["GOOGLE_ANALYTICS_MEASUREMENT_ID"] = "G-TEST157B"
    module.app.config["SECRET_KEY"] = "test-secret-157b"
    yield module
    for key, value in original.items():
        if value is None:
            module.app.config.pop(key, None)
        else:
            module.app.config[key] = value


# --- FIRST RELEVANT EXTERNO --------------------------------------------------


def test_new_eligible_task_stages_first_relevant_envelope(app, ctx, monkeypatch):
    with app.app_context():
        app.config["SECRET_KEY"] = "test-secret-157b"
        user, _ = _auth_user(monkeypatch, email="157b-stage@test.com")
        with app.test_request_context("/chat_julia"):
            from flask import session

            session.clear()
            result = try_record_growth_task_event(
                event_name=FUNNEL_EVENT_TASK_COMPLETED,
                source=FUNNEL_SOURCE_CLEIDE_AUDIT,
                task_type=TASK_TYPE_CLEIDE_AUDIT,
                idempotency_key="growth:157b:stage:task_completed",
                user=user,
                execution_id="exec-157b-stage",
            )
            assert result is not None
            assert result["created"] is True
            pending = _pending_first_relevant(session)
            assert pending is not None
            assert pending["event"] == FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED
            assert pending["params"] == {}
            assert "task_type" not in pending
            assert "task_type" not in pending["params"]
            marco = FunnelEvent.query.filter_by(
                user_id=user.id,
                event_name=FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
            ).one()
            expected = build_external_event_token(
                event_name=FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
                funnel_event_id=marco.id,
            )
            assert pending["token"] == expected
            task_token = build_external_event_token(
                event_name=FUNNEL_EVENT_TASK_COMPLETED,
                funnel_event_id=result["event"].id,
            )
            assert pending["token"] != task_token


def test_token_from_first_relevant_not_task_completed(app, ctx, monkeypatch):
    with app.app_context():
        app.config["SECRET_KEY"] = "test-secret-157b"
        user, _ = _auth_user(monkeypatch, email="157b-token@test.com")
        with app.test_request_context("/"):
            from flask import session

            session.clear()
            result = try_record_growth_task_event(
                event_name=FUNNEL_EVENT_TASK_COMPLETED,
                source=FUNNEL_SOURCE_CLEIDE_AUDIT,
                task_type=TASK_TYPE_CLEIDE_AUDIT,
                idempotency_key="growth:157b:token:task_completed",
                user=user,
                execution_id="exec-157b-token",
            )
            marco = FunnelEvent.query.filter_by(
                user_id=user.id,
                event_name=FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
            ).one()
            pending = _pending_first_relevant(session)
            assert pending["token"] == build_external_event_token(
                event_name=marco.event_name, funnel_event_id=marco.id
            )
            assert result["event"].id != marco.id


def test_existing_marco_no_external_envelope(app, ctx, monkeypatch):
    with app.app_context():
        app.config["SECRET_KEY"] = "test-secret-157b"
        user, _ = _auth_user(monkeypatch, email="157b-existing@test.com")
        with app.test_request_context("/"):
            from flask import session

            session.clear()
            first = try_record_growth_task_event(
                event_name=FUNNEL_EVENT_TASK_COMPLETED,
                source=FUNNEL_SOURCE_CLEIDE_AUDIT,
                task_type=TASK_TYPE_CLEIDE_AUDIT,
                idempotency_key="growth:157b:exist:a",
                user=user,
                execution_id="exec-157b-exist-a",
            )
            assert first["created"] is True
            session.pop(SESSION_EXTERNAL_EVENT_PENDING_FIRST_RELEVANT, None)

            second = try_record_growth_task_event(
                event_name=FUNNEL_EVENT_TASK_COMPLETED,
                source=FUNNEL_SOURCE_CLEIDE_AUDIT,
                task_type=TASK_TYPE_JULIA_CHAT,
                idempotency_key="growth:157b:exist:b",
                user=user,
                execution_id="exec-157b-exist-b",
            )
            assert second["created"] is True
            assert _pending_first_relevant(session) is None
            assert (
                FunnelEvent.query.filter_by(
                    user_id=user.id,
                    event_name=FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
                ).count()
                == 1
            )


def test_task_created_false_no_external(app, ctx, monkeypatch):
    with app.app_context():
        app.config["SECRET_KEY"] = "test-secret-157b"
        user, _ = _auth_user(monkeypatch, email="157b-created-false@test.com")
        with app.test_request_context("/"):
            from flask import session

            session.clear()
            key = "growth:157b:created-false:task_completed"
            first = try_record_growth_task_event(
                event_name=FUNNEL_EVENT_TASK_COMPLETED,
                source=FUNNEL_SOURCE_CLEIDE_AUDIT,
                task_type=TASK_TYPE_CLEIDE_AUDIT,
                idempotency_key=key,
                user=user,
                execution_id="exec-157b-cf",
            )
            assert first["created"] is True
            session.pop(SESSION_EXTERNAL_EVENT_PENDING_FIRST_RELEVANT, None)

            replay = try_record_growth_task_event(
                event_name=FUNNEL_EVENT_TASK_COMPLETED,
                source=FUNNEL_SOURCE_CLEIDE_AUDIT,
                task_type=TASK_TYPE_CLEIDE_AUDIT,
                idempotency_key=key,
                user=user,
                execution_id="exec-157b-cf",
            )
            assert replay["created"] is False
            assert _pending_first_relevant(session) is None


def test_recovery_selects_historical_no_external(app, ctx, monkeypatch):
    with app.app_context():
        app.config["SECRET_KEY"] = "test-secret-157b"
        user, _ = _auth_user(monkeypatch, email="157b-recovery@test.com")
        t0 = datetime(2026, 1, 1, 12, 0, 0)
        older = try_record_funnel_event(
            event_name=FUNNEL_EVENT_TASK_COMPLETED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            user_id=user.id,
            conta_id=user.conta_id,
            franquia_id=user.franquia_id,
            idempotency_key="growth:157b:recovery:a",
            execution_id="exec-a",
            metadata_json={"task_type": TASK_TYPE_CLEIDE_AUDIT},
            occurred_at=t0,
        )
        assert older is not None
        assert older["created"] is True
        assert (
            FunnelEvent.query.filter_by(
                user_id=user.id,
                event_name=FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
            ).count()
            == 0
        )

        with app.test_request_context("/"):
            from flask import session

            session.clear()
            b = try_record_growth_task_event(
                event_name=FUNNEL_EVENT_TASK_COMPLETED,
                source=FUNNEL_SOURCE_CLEIDE_AUDIT,
                task_type=TASK_TYPE_JULIA_CHAT,
                idempotency_key="growth:157b:recovery:b",
                user=user,
                execution_id="exec-b",
            )
            assert b["created"] is True
            marco = FunnelEvent.query.filter_by(
                user_id=user.id,
                event_name=FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
            ).one()
            assert marco.occurred_at == t0
            assert marco.metadata_json.get("task_type") == TASK_TYPE_CLEIDE_AUDIT
            assert _pending_first_relevant(session) is None


def test_ensure_commit_failure_no_external(app, ctx, monkeypatch):
    with app.app_context():
        app.config["SECRET_KEY"] = "test-secret-157b"
        user, _ = _auth_user(monkeypatch, email="157b-commit-fail@test.com")
        with app.test_request_context("/"):
            from flask import session

            session.clear()
            with patch(
                "app.funnel_event_service._ensure_first_relevant_task_completed",
                return_value=None,
            ):
                result = try_record_growth_task_event(
                    event_name=FUNNEL_EVENT_TASK_COMPLETED,
                    source=FUNNEL_SOURCE_CLEIDE_AUDIT,
                    task_type=TASK_TYPE_CLEIDE_AUDIT,
                    idempotency_key="growth:157b:commit-fail",
                    user=user,
                    execution_id="exec-commit-fail",
                )
                assert result is not None
                assert result["created"] is True
                assert _pending_first_relevant(session) is None


def test_without_request_context_growth_ok_no_external(app, ctx, monkeypatch):
    with app.app_context():
        user, _ = _auth_user(monkeypatch, email="157b-noreq@test.com")
        result = try_record_growth_task_event(
            event_name=FUNNEL_EVENT_TASK_COMPLETED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            task_type=TASK_TYPE_CLEIDE_AUDIT,
            idempotency_key="growth:157b:noreq",
            user=user,
            execution_id="exec-noreq",
        )
        assert result is not None
        assert result["created"] is True
        assert (
            FunnelEvent.query.filter_by(
                user_id=user.id,
                event_name=FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
            ).count()
            == 1
        )


def test_allow_external_false_growth_ok_no_envelope(app, ctx, monkeypatch):
    with app.app_context():
        app.config["SECRET_KEY"] = "test-secret-157b"
        user, _ = _auth_user(monkeypatch, email="157b-block@test.com")
        with app.test_request_context("/"):
            from flask import session

            session.clear()
            result = try_record_growth_task_event(
                event_name=FUNNEL_EVENT_TASK_COMPLETED,
                source=FUNNEL_SOURCE_CLEIDE_AUDIT,
                task_type=TASK_TYPE_CLEIDE_AUDIT,
                idempotency_key="growth:157b:block",
                user=user,
                execution_id="exec-block",
                allow_external_first_relevant=False,
            )
            assert result["created"] is True
            assert (
                FunnelEvent.query.filter_by(
                    user_id=user.id,
                    event_name=FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
                ).count()
                == 1
            )
            assert _pending_first_relevant(session) is None


def test_agente_compara_idempotent_replay_blocks_external(app, ctx, monkeypatch):
    """Espelha o gate allow_external_first_relevant=False do _build_ready_response."""
    with app.app_context():
        app.config["SECRET_KEY"] = "test-secret-157b"
        user, _ = _auth_user(monkeypatch, email="157b-replay@test.com")
        with app.test_request_context("/agente-compara"):
            from flask import session

            session.clear()
            result = try_record_growth_task_event(
                event_name=FUNNEL_EVENT_TASK_COMPLETED,
                source="agente_compara",
                task_type="agente_compara",
                idempotency_key="growth:157b:ac-replay:task_completed",
                user=user,
                comparison_id="cmp-157b",
                execution_id="exec-157b-replay",
                allow_external_first_relevant=False,
            )
            assert result["created"] is True
            assert _pending_first_relevant(session) is None


def test_agente_compara_functional_new_can_stage(app, ctx, monkeypatch):
    with app.app_context():
        app.config["SECRET_KEY"] = "test-secret-157b"
        user, _ = _auth_user(monkeypatch, email="157b-ac-new@test.com")
        with app.test_request_context("/agente-compara"):
            from flask import session

            session.clear()
            result = try_record_growth_task_event(
                event_name=FUNNEL_EVENT_TASK_COMPLETED,
                source="agente_compara",
                task_type="agente_compara",
                idempotency_key="growth:157b:ac-new:task_completed",
                user=user,
                comparison_id="cmp-157b-new",
                execution_id="exec-157b-new",
                allow_external_first_relevant=True,
            )
            assert result["created"] is True
            pending = _pending_first_relevant(session)
            assert pending is not None
            assert pending["event"] == FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED


# --- SESSION / CONSENT -------------------------------------------------------


def test_unknown_stages_without_dispatch(web):
    client = web.app.test_client()
    envelope = {
        "event": FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
        "token": "v1_" + ("d" * 32),
        "params": {},
    }
    with client.session_transaction() as sess:
        store_pending_first_relevant_external_event(sess, envelope)
    html = client.get("/login").get_data(as_text=True)
    # unknown: nao consome; dispatch de pendencia server-side ausente
    assert "pending_external_events" not in html or "first_relevant_task_completed" not in html
    assert '"event": "first_relevant_task_completed"' not in html
    assert "'event': 'first_relevant_task_completed'" not in html
    with client.session_transaction() as sess:
        assert sess.get(SESSION_EXTERNAL_EVENT_PENDING_FIRST_RELEVANT) == envelope


def test_accepted_makes_pending_available(web):
    client = web.app.test_client()
    envelope = {
        "event": FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
        "token": "v1_" + ("e" * 32),
        "params": {},
    }
    with client.session_transaction() as sess:
        store_pending_first_relevant_external_event(sess, envelope)
    _set_privacy(client, "v1:accepted")
    html = client.get("/login").get_data(as_text=True)
    assert "AFExternalTracking.dispatch" in html
    assert FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED in html
    with client.session_transaction() as sess:
        assert SESSION_EXTERNAL_EVENT_PENDING_FIRST_RELEVANT not in sess


def test_rejected_discards_first_relevant_pending(web):
    client = web.app.test_client()
    with client.session_transaction() as sess:
        store_pending_first_relevant_external_event(
            sess,
            {
                "event": FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
                "token": "v1_" + ("f" * 32),
                "params": {},
            },
        )
    resp = client.post(
        "/api/privacy/marketing-consent",
        json={"decision": "rejected"},
    )
    assert resp.status_code == 200
    with client.session_transaction() as sess:
        assert SESSION_EXTERNAL_EVENT_PENDING_FIRST_RELEVANT not in sess


def test_signup_and_first_relevant_slots_coexist(web):
    client = web.app.test_client()
    signup = {
        "event": "signup_completed",
        "token": "v1_" + ("a" * 32),
        "params": {"signup_method": "password"},
    }
    first = {
        "event": FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
        "token": "v1_" + ("b" * 32),
        "params": {},
    }
    with client.session_transaction() as sess:
        store_pending_external_event(sess, signup)
        store_pending_first_relevant_external_event(sess, first)
        assert sess.get(SESSION_EXTERNAL_EVENT_PENDING) == signup
        assert sess.get(SESSION_EXTERNAL_EVENT_PENDING_FIRST_RELEVANT) == first

    _set_privacy(client, "v1:accepted")
    html = client.get("/login").get_data(as_text=True)
    assert "signup_completed" in html
    assert FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED in html
    assert html.count("AFExternalTracking.dispatch") >= 1
    with client.session_transaction() as sess:
        assert SESSION_EXTERNAL_EVENT_PENDING not in sess
        assert SESSION_EXTERNAL_EVENT_PENDING_FIRST_RELEVANT not in sess


def test_pop_all_keeps_independent_slots():
    sess = {}
    store_pending_external_event(
        sess,
        {
            "event": "signup_completed",
            "token": "v1_" + ("1" * 32),
            "params": {"signup_method": "google"},
        },
    )
    store_pending_first_relevant_external_event(
        sess,
        {
            "event": FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
            "token": "v1_" + ("2" * 32),
            "params": {},
        },
    )
    popped = pop_all_pending_external_events_if_allowed(sess, marketing_allowed=True)
    assert len(popped) == 2
    assert popped[0]["event"] == "signup_completed"
    assert popped[1]["event"] == FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED
    assert SESSION_EXTERNAL_EVENT_PENDING not in sess
    assert SESSION_EXTERNAL_EVENT_PENDING_FIRST_RELEVANT not in sess


def test_rejected_staging_clears_slot(app, ctx, monkeypatch):
    with app.app_context():
        app.config["SECRET_KEY"] = "test-secret-157b"
        user, _ = _auth_user(monkeypatch, email="157b-rej-stage@test.com")
        with app.test_request_context(
            "/",
            headers={"Cookie": f"{PRIVACY_MARKETING_COOKIE_NAME}=v1:rejected"},
        ):
            from flask import session

            session.clear()
            store_pending_first_relevant_external_event(
                session,
                {
                    "event": FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
                    "token": "v1_" + ("c" * 32),
                    "params": {},
                },
            )
            try_record_growth_task_event(
                event_name=FUNNEL_EVENT_TASK_COMPLETED,
                source=FUNNEL_SOURCE_CLEIDE_AUDIT,
                task_type=TASK_TYPE_CLEIDE_AUDIT,
                idempotency_key="growth:157b:rej-stage",
                user=user,
                execution_id="exec-rej",
            )
            assert _pending_first_relevant(session) is None


def test_first_relevant_accepted_by_external_helper(app, ctx, monkeypatch):
    with app.app_context():
        app.config["SECRET_KEY"] = "test-secret-157b"
        user, _ = _auth_user(monkeypatch, email="157b-helper@test.com")
        with app.test_request_context("/"):
            result = try_record_growth_task_event(
                event_name=FUNNEL_EVENT_TASK_COMPLETED,
                source=FUNNEL_SOURCE_CLEIDE_AUDIT,
                task_type=TASK_TYPE_CLEIDE_AUDIT,
                idempotency_key="growth:157b:helper",
                user=user,
                execution_id="exec-helper",
            )
            assert result["created"] is True
        marco = FunnelEvent.query.filter_by(
            user_id=user.id,
            event_name=FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
        ).one()
        envelope = build_external_event_envelope(marco)
        assert envelope is not None
        assert envelope["event"] == FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED
        assert envelope["params"] == {}
        assert is_minimal_external_envelope(envelope) is True
        assert FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED in EXTERNAL_BROWSER_ALLOWED_EVENTS


def test_extra_params_rejected():
    bad = {
        "event": FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
        "token": "v1_" + ("a" * 32),
        "params": {"task_type": "cleide_audit"},
    }
    assert is_minimal_external_envelope(bad) is False
    bad2 = {
        "event": FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
        "token": "v1_" + ("a" * 32),
        "params": {"user_id": 1},
    }
    assert is_minimal_external_envelope(bad2) is False


def test_js_meta_track_custom_and_ga4_mapping():
    src = EXTERNAL_JS.read_text(encoding="utf-8")
    assert "first_relevant_task_completed" in src
    assert "FirstRelevantTaskCompleted" in src
    assert 'trackCustom"' in src or "trackCustom" in src
    assert re.search(
        r'first_relevant_task_completed:\s*"first_relevant_task_completed"',
        src,
    )
    # GA4 nao usa token como event_id
    ga_block = src[src.index("function dispatchGoogle") : src.index("function dispatch(")]
    assert "event_id" not in ga_block.lower() or "eventID" not in ga_block
    assert "alreadySent" in src
    assert 'dedupeKey(destination, eventName, token)' in src or "destination + \"|\" + eventName" in src


def test_ensure_return_contract(app, ctx, monkeypatch):
    with app.app_context():
        user, _ = _auth_user(monkeypatch, email="157b-ensure@test.com")
        task = try_record_funnel_event(
            event_name=FUNNEL_EVENT_TASK_COMPLETED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            user_id=user.id,
            conta_id=user.conta_id,
            franquia_id=user.franquia_id,
            idempotency_key="growth:157b:ensure-task",
            execution_id="exec-ensure",
            metadata_json={"task_type": TASK_TYPE_CLEIDE_AUDIT},
        )
        marker = _ensure_first_relevant_task_completed(
            user.id,
            triggering_completion_id=task["event"].id,
        )
        assert marker is not None
        assert marker["created"] is True
        assert marker["selected_completion_id"] == task["event"].id
        assert marker["event"].event_name == FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED

        again = _ensure_first_relevant_task_completed(user.id)
        assert again is not None
        assert again["created"] is False
        assert again["event"].id == marker["event"].id


# --- LEGADOS -----------------------------------------------------------------


def test_audit_started_and_first_audit_completed_removed_from_pixel():
    text = PIXEL_EVENTS.read_text(encoding="utf-8")
    assert 'trackCustomEvent("AuditStarted"' not in text
    assert 'trackCustomEvent("FirstAuditCompleted"' not in text
    assert "trackFunnelEvent" not in text
    assert 'event_name === "file_uploaded"' not in text
    assert "is_first_audit" not in text


def test_meta_pixel_allowed_events_empty():
    assert META_PIXEL_ALLOWED_EVENTS == set()
    assert is_meta_pixel_allowed("file_uploaded") is False
    assert is_meta_pixel_allowed("freight_calculated") is False


def test_growth_file_uploaded_and_freight_still_allowed_internally(app, ctx, monkeypatch):
    with app.app_context():
        user, _ = _auth_user(monkeypatch, email="157b-legacy-growth@test.com")
        up = try_record_funnel_event(
            event_name="file_uploaded",
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            user_id=user.id,
            conta_id=user.conta_id,
            franquia_id=user.franquia_id,
            idempotency_key="growth:157b:file",
            document_id="doc-157b",
        )
        assert up is not None
        assert up["created"] is True
        assert up["event"].event_name == "file_uploaded"

        from app.funnel_event_service import record_completion_with_first_audit

        result = record_completion_with_first_audit(
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            user_id=user.id,
            conta_id=user.conta_id,
            franquia_id=user.franquia_id,
            freight_idempotency_key="growth:157b:freight",
            first_audit_idempotency_key="growth:157b:first-audit",
            execution_id="exec-legacy",
        )
        db.session.commit()
        assert result["freight_calculated"]["created"] is True
        assert result["is_first_audit"] is True
        refreshed = db.session.get(type(user), user.id)
        # User model
        from app.models import User

        refreshed = db.session.get(User, user.id)
        assert refreshed.first_audit_completed_at is not None


def test_157a_events_still_in_allowlist_js():
    src = EXTERNAL_JS.read_text(encoding="utf-8")
    for name in ("page_view", "signup_completed", "checkout_started"):
        assert f"{name}: true" in src
    assert "Purchase" not in src
    assert "Lead" not in PIXEL_EVENTS.read_text(encoding="utf-8")


def test_acesso_desktop_untouched():
    src = ACESSO_DESKTOP.read_text(encoding="utf-8")
    assert "AFExternalTracking" not in src
    assert "first_relevant" not in src
    assert "fbq" not in src


def test_base_html_dispatches_pending_list():
    src = BASE_HTML.read_text(encoding="utf-8")
    assert "pending_external_events" in src
    assert "AFExternalTracking.dispatch" in src


def test_discard_includes_first_relevant_slot():
    sess = {
        SESSION_EXTERNAL_EVENT_PENDING: {"event": "signup_completed"},
        SESSION_EXTERNAL_EVENT_PENDING_FIRST_RELEVANT: {
            "event": FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED
        },
    }
    discard_pending_marketing_pixel_flags(sess)
    assert SESSION_EXTERNAL_EVENT_PENDING not in sess
    assert SESSION_EXTERNAL_EVENT_PENDING_FIRST_RELEVANT not in sess
