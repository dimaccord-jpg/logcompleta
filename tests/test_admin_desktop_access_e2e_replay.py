"""Homologação E2E Replay — paridade visual, temporal, registration e isolamento."""
from __future__ import annotations

import importlib
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.extensions import db
from app.models import DesktopAccessE2ETestRun, FunnelEvent, Lead, User
from app.services import admin_desktop_access_test_service as e2e
from app.services import lead_campaign_email_service as campaign_email
from app.services.lead_acquisition_service import CAMPANHA_ACESSO_DESKTOP, FONTE_LANDING
from tests.conftest import seed_conta_franquia_cliente, seed_usuario

SECRET = "test-secret-desktop-access-e2e-replay"
APP_DIR = Path(__file__).resolve().parents[1] / "app"
ROOT = Path(__file__).resolve().parents[1]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", SECRET)
    return importlib.import_module("app.web")


def _admin(app, *, email="admin.e2e@empresa.com"):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug=f"conta-{email.split('@')[0]}")
        user = seed_usuario(franquia.id, conta.id, email=email)
        user.is_admin = True
        user.set_password("SenhaOriginal#1")
        db.session.commit()
        return user.id, user.email, user.password_hash, user.created_at, user.first_audit_completed_at


def _create_run(user_id: int, *, run_id: str | None = None, **kwargs) -> DesktopAccessE2ETestRun:
    now = _utcnow()
    rid = run_id or e2e.new_test_run_id()
    defaults = {
        "run_id": rid,
        "user_id": int(user_id),
        "created_at": now,
        "initial_email_sent_at": now,
    }
    defaults.update(kwargs)
    run = DesktopAccessE2ETestRun(**defaults)
    db.session.add(run)
    db.session.commit()
    return run


def _normalize_urls(text: str) -> str:
    text = re.sub(r"https?://[^\s\"'<>]+", "URL", text)
    text = re.sub(r"/acesso-desktop/(?:continuar|descadastrar)/[A-Za-z0-9._~\-]+", "/acesso-desktop/TOKEN", text)
    return text


def _url_builders():
    return (
        lambda token: f"https://example.test/acesso-desktop/continuar/{token}",
        lambda token: f"https://example.test/acesso-desktop/descadastrar/{token}",
    )


# --- Paridade de e-mail ---


def test_initial_email_builder_parity_real_vs_e2e():
    real = campaign_email.build_initial_cta_email(
        cta_url="https://x/c/REAL",
        unsubscribe_url="https://x/u/REAL",
    )
    e2e_built = campaign_email.build_initial_cta_email(
        cta_url="https://x/c/E2E",
        unsubscribe_url="https://x/u/E2E",
    )
    assert real["subject"] == e2e_built["subject"] == campaign_email.CTA_EMAIL_SUBJECT
    assert "[TESTE]" not in real["subject"]
    assert _normalize_urls(real["html"]) == _normalize_urls(e2e_built["html"])
    assert _normalize_urls(real["text"]) == _normalize_urls(e2e_built["text"])


def test_followup_email_builder_parity_real_vs_e2e():
    real = campaign_email.build_followup_email(
        cta_url="https://x/c/REAL",
        unsubscribe_url="https://x/u/REAL",
    )
    e2e_built = campaign_email.build_followup_email(
        cta_url="https://x/c/E2E",
        unsubscribe_url="https://x/u/E2E",
    )
    assert real["subject"] == e2e_built["subject"] == campaign_email.FOLLOWUP_EMAIL_SUBJECT
    assert "[TESTE]" not in real["subject"]
    assert _normalize_urls(real["html"]) == _normalize_urls(e2e_built["html"])
    assert _normalize_urls(real["text"]) == _normalize_urls(e2e_built["text"])


def test_e2e_start_uses_real_builder_and_creates_run(app):
    user_id, email, *_ = _admin(app)
    build_cta, build_unsub = _url_builders()
    sess = {}
    with app.app_context():
        admin = db.session.get(User, user_id)
        with patch("app.services.admin_desktop_access_test_service.send_email") as send:
            result = e2e.start_e2e_test_run(
                admin_user=admin,
                email=email,
                secret_key=SECRET,
                build_cta_url=build_cta,
                build_unsubscribe_url=build_unsub,
                session_obj=sess,
                app_env="dev",
            )
        assert result["status"] == "sent"
        assert send.called
        kwargs = send.call_args.kwargs
        assert kwargs["subject"] == campaign_email.CTA_EMAIL_SUBJECT
        assert "[TESTE]" not in kwargs["subject"]
        assert "Cancelar mensagens" in kwargs["html"]
        assert "teste administrativo" not in kwargs["html"].lower()
        assert Lead.query.count() == 0
        runs = DesktopAccessE2ETestRun.query.filter_by(user_id=user_id).all()
        assert len(runs) == 1
        assert runs[0].initial_email_sent_at is not None


def test_multiple_runs_same_email(app):
    user_id, email, *_ = _admin(app)
    build_cta, build_unsub = _url_builders()
    with app.app_context():
        admin = db.session.get(User, user_id)
        for i in range(3):
            sess = {}
            with patch("app.services.admin_desktop_access_test_service.send_email"):
                # Bypass cooldown between iterations
                result = e2e.start_e2e_test_run(
                    admin_user=admin,
                    email=email,
                    secret_key=SECRET,
                    build_cta_url=build_cta,
                    build_unsubscribe_url=build_unsub,
                    session_obj=sess,
                    app_env="dev",
                    now=_utcnow() + timedelta(seconds=i * (e2e.SEND_COOLDOWN_SECONDS + 1)),
                )
            assert result["status"] == "sent"
        assert DesktopAccessE2ETestRun.query.filter_by(user_id=user_id).count() == 3


# --- Temporal follow-up ---


@pytest.mark.parametrize(
    "delta, expect_send",
    [
        (timedelta(hours=23, minutes=59, seconds=59), False),
        (timedelta(hours=24), True),
        (timedelta(hours=24, minutes=1), True),
    ],
)
def test_e2e_followup_spacing(app, delta, expect_send):
    user_id, *_ = _admin(app, email=f"tempo.{int(delta.total_seconds())}@empresa.com")
    build_cta, build_unsub = _url_builders()
    with app.app_context():
        sent_at = _utcnow() - delta
        run = _create_run(user_id, initial_email_sent_at=sent_at, created_at=sent_at)
        with patch("app.services.admin_desktop_access_test_service.send_email") as send:
            status = e2e.maybe_send_e2e_followup_email(
                run,
                secret_key=SECRET,
                build_cta_url=build_cta,
                build_unsubscribe_url=build_unsub,
                now=_utcnow(),
                app_env="dev",
            )
        if expect_send:
            assert status == "sent"
            assert send.called
            assert send.call_args.kwargs["subject"] == campaign_email.FOLLOWUP_EMAIL_SUBJECT
            db.session.refresh(run)
            assert run.followup_sent_at is not None
        else:
            assert status == "not_eligible"
            assert not send.called


def test_e2e_followup_not_resent_and_opt_out_blocks(app):
    user_id, *_ = _admin(app, email="follow.opt@empresa.com")
    build_cta, build_unsub = _url_builders()
    with app.app_context():
        sent_at = _utcnow() - timedelta(hours=campaign_email.FOLLOWUP_DELAY_HOURS, minutes=5)
        run = _create_run(user_id, initial_email_sent_at=sent_at, created_at=sent_at)
        with patch("app.services.admin_desktop_access_test_service.send_email"):
            assert (
                e2e.maybe_send_e2e_followup_email(
                    run,
                    secret_key=SECRET,
                    build_cta_url=build_cta,
                    build_unsubscribe_url=build_unsub,
                    app_env="dev",
                )
                == "sent"
            )
        with patch("app.services.admin_desktop_access_test_service.send_email") as send2:
            assert (
                e2e.maybe_send_e2e_followup_email(
                    run,
                    secret_key=SECRET,
                    build_cta_url=build_cta,
                    build_unsubscribe_url=build_unsub,
                    app_env="dev",
                )
                == "skipped"
            )
            assert not send2.called

        run2 = _create_run(
            user_id,
            run_id=e2e.new_test_run_id(),
            initial_email_sent_at=sent_at,
            created_at=sent_at,
            opt_out_at=_utcnow(),
        )
        with patch("app.services.admin_desktop_access_test_service.send_email") as send3:
            assert (
                e2e.maybe_send_e2e_followup_email(
                    run2,
                    secret_key=SECRET,
                    build_cta_url=build_cta,
                    build_unsubscribe_url=build_unsub,
                    app_env="dev",
                )
                == "skipped_opt_out"
            )
            assert not send3.called


def test_check_followup_now_does_not_force(app):
    user_id, email, *_ = _admin(app, email="check.fu@empresa.com")
    build_cta, build_unsub = _url_builders()
    with app.app_context():
        admin = db.session.get(User, user_id)
        run = _create_run(user_id, initial_email_sent_at=_utcnow())
        with patch("app.services.admin_desktop_access_test_service.send_email") as send:
            result = e2e.check_and_maybe_send_latest_followup(
                admin_user=admin,
                secret_key=SECRET,
                build_cta_url=build_cta,
                build_unsubscribe_url=build_unsub,
                session_obj={},
                app_env="dev",
            )
        assert result["status"] == "not_eligible"
        assert "não elegível" in (result.get("message") or "").lower()
        assert not send.called
        db.session.refresh(run)
        assert run.followup_sent_at is None


# --- CTA / Unsubscribe page parity ---


@pytest.fixture
def journey_client(app):
    app.config["SECRET_KEY"] = SECRET
    app.config["TESTING"] = True
    app.config["SERVER_NAME"] = "localhost"
    app.template_folder = str(APP_DIR / "templates")
    app.static_folder = str(APP_DIR / "static")
    web = _load_web_module()
    rules = {
        "acesso_desktop": ("/acesso-desktop", web.acesso_desktop, ["GET", "POST"]),
        "acesso_desktop_continuar": (
            "/acesso-desktop/continuar/<token>",
            web.acesso_desktop_continuar,
            ["GET", "POST"],
        ),
        "acesso_desktop_descadastrar": (
            "/acesso-desktop/descadastrar/<token>",
            web.acesso_desktop_descadastrar,
            ["GET", "POST"],
        ),
        "login": ("/login", lambda: ("login-ok", 200), ["GET", "POST"]),
        "register": ("/register", web.register, ["POST"]),
    }
    for endpoint, (rule, view, methods) in rules.items():
        if endpoint not in app.view_functions:
            app.add_url_rule(rule, endpoint=endpoint, view_func=view, methods=methods)
    return app.test_client()


def test_cta_page_parity_real_vs_e2e(journey_client, app, monkeypatch):
    monkeypatch.setattr(e2e, "resolve_app_env", lambda: "dev")
    with app.app_context():
        user_id, email, *_ = _admin(app, email="parity.cta@empresa.com")
        run = _create_run(user_id)
        lead = Lead(
            email="lead.parity@empresa.com",
            acquisition_campaign=CAMPANHA_ACESSO_DESKTOP,
            acquisition_source=FONTE_LANDING,
            campaign_captured_at=_utcnow(),
            cta_email_sent_at=_utcnow(),
            followup_count=0,
        )
        db.session.add(lead)
        db.session.commit()
        real_token = campaign_email.generate_cta_token(lead.id, secret_key=SECRET)
        e2e_token = e2e.generate_e2e_cta_token(
            run_id=run.run_id, user_id=user_id, secret_key=SECRET
        )

    real_html = journey_client.get(f"/acesso-desktop/continuar/{real_token}").get_data(as_text=True)
    e2e_html = journey_client.get(f"/acesso-desktop/continuar/{e2e_token}").get_data(as_text=True)
    for needle in (
        "Continue no computador.",
        "criar sua conta Free",
        "Continuar para o cadastro Free",
        "Sem cartão de crédito",
    ):
        assert needle in real_html
        assert needle in e2e_html
    assert "teste administrativo" not in e2e_html.lower()
    assert "Continuar o teste" not in e2e_html
    assert "[TESTE]" not in e2e_html


def test_unsubscribe_page_parity_real_vs_e2e(journey_client, app, monkeypatch):
    monkeypatch.setattr(e2e, "resolve_app_env", lambda: "dev")
    with app.app_context():
        user_id, *_ = _admin(app, email="parity.unsub@empresa.com")
        run = _create_run(user_id)
        lead = Lead(
            email="lead.unsub@empresa.com",
            acquisition_campaign=CAMPANHA_ACESSO_DESKTOP,
            acquisition_source=FONTE_LANDING,
            campaign_captured_at=_utcnow(),
            followup_count=0,
        )
        db.session.add(lead)
        db.session.commit()
        real_token = campaign_email.generate_unsubscribe_token(lead.id, secret_key=SECRET)
        e2e_token = e2e.generate_e2e_unsubscribe_token(
            run_id=run.run_id, user_id=user_id, secret_key=SECRET
        )

    real_html = journey_client.get(f"/acesso-desktop/descadastrar/{real_token}").get_data(as_text=True)
    e2e_html = journey_client.get(f"/acesso-desktop/descadastrar/{e2e_token}").get_data(as_text=True)
    for needle in ("Cancelar mensagens desta jornada?", "Cancelar mensagens"):
        assert needle in real_html
        assert needle in e2e_html


def test_e2e_cta_post_redirects_to_register_and_marks_click(journey_client, app, monkeypatch):
    monkeypatch.setattr(e2e, "resolve_app_env", lambda: "dev")
    with app.app_context():
        user_id, *_ = _admin(app, email="cta.post.e2e@empresa.com")
        run = _create_run(user_id, run_id="runcta01")
        token = e2e.generate_e2e_cta_token(run_id=run.run_id, user_id=user_id, secret_key=SECRET)

    resp = journey_client.post(f"/acesso-desktop/continuar/{token}", follow_redirects=False)
    assert resp.status_code == 302
    assert "mode=register" in (resp.location or "")
    with journey_client.session_transaction() as sess:
        ctx = e2e.get_test_mode_context(sess)
        assert ctx is not None
        assert ctx["run_id"] == "runcta01"
    with app.app_context():
        persisted = DesktopAccessE2ETestRun.query.filter_by(run_id="runcta01").first()
        assert persisted.cta_clicked_at is not None


def test_e2e_opt_out_marks_run_only(journey_client, app, monkeypatch):
    monkeypatch.setattr(e2e, "resolve_app_env", lambda: "dev")
    with app.app_context():
        user_id, email, *_ = _admin(app, email="opt.e2e@empresa.com")
        lead = Lead(
            email=email,
            acquisition_campaign=CAMPANHA_ACESSO_DESKTOP,
            acquisition_source=FONTE_LANDING,
            campaign_captured_at=_utcnow(),
            followup_count=0,
        )
        db.session.add(lead)
        db.session.commit()
        lead_id = lead.id
        run = _create_run(user_id, run_id="runopt01")
        token = e2e.generate_e2e_unsubscribe_token(
            run_id=run.run_id, user_id=user_id, secret_key=SECRET
        )

    resp = journey_client.post(f"/acesso-desktop/descadastrar/{token}", follow_redirects=False)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Preferência atualizada." in html
    with app.app_context():
        run = DesktopAccessE2ETestRun.query.filter_by(run_id="runopt01").first()
        assert run.opt_out_at is not None
        lead = db.session.get(Lead, lead_id)
        assert lead.opt_out_at is None


def test_new_run_after_opt_out_is_clean(app):
    user_id, email, *_ = _admin(app, email="new.after.opt@empresa.com")
    build_cta, build_unsub = _url_builders()
    with app.app_context():
        admin = db.session.get(User, user_id)
        old = _create_run(user_id, opt_out_at=_utcnow())
        with patch("app.services.admin_desktop_access_test_service.send_email"):
            result = e2e.start_e2e_test_run(
                admin_user=admin,
                email=email,
                secret_key=SECRET,
                build_cta_url=build_cta,
                build_unsubscribe_url=build_unsub,
                session_obj={},
                app_env="dev",
            )
        assert result["status"] == "sent"
        new_run = DesktopAccessE2ETestRun.query.filter_by(run_id=result["run_id"]).first()
        assert new_run.opt_out_at is None
        assert new_run.run_id != old.run_id


# --- Registration replay ---


def test_real_registration_still_creates_user(journey_client, app, monkeypatch):
    monkeypatch.setattr(e2e, "resolve_app_env", lambda: "dev")
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="reg-real")
        with patch("app.auth_services._get_free_franquia_limite_onboarding", return_value=10):
            with patch(
                "app.services.conta_franquia_service.criar_conta_franquia_para_cadastro",
                return_value=(conta, franquia),
            ):
                resp = journey_client.post(
                    "/register",
                    data={
                        "nome": "Novo Lead",
                        "email": "novo.lead.reg@empresa.com",
                        "password": "SenhaNova#99",
                        "accept_terms": "on",
                        "job_role": "ops",
                        "usage_purpose": "test",
                    },
                    follow_redirects=False,
                )
        assert resp.status_code == 302
        assert "/login" in (resp.location or "")
        assert User.query.filter(User.email == "novo.lead.reg@empresa.com").count() == 1


def test_e2e_registration_replay_no_duplicate_user(journey_client, app, monkeypatch):
    monkeypatch.setattr(e2e, "resolve_app_env", lambda: "dev")
    with app.app_context():
        user_id, email, pwd_hash, created_at, first_audit = _admin(
            app, email="replay.reg@empresa.com"
        )
        run = _create_run(user_id, run_id="runreg01")
        token = e2e.generate_e2e_cta_token(run_id=run.run_id, user_id=user_id, secret_key=SECRET)

    journey_client.post(f"/acesso-desktop/continuar/{token}", follow_redirects=False)
    resp = journey_client.post(
        "/register",
        data={
            "nome": "Admin Replay",
            "email": email,
            "password": "QualquerSenhaForm#1",
            "accept_terms": "on",
            "job_role": "ops",
            "usage_purpose": "test",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/login" in (resp.location or "")
    with journey_client.session_transaction() as sess:
        assert sess.get("pixel_event_complete_registration_once") is not True
    with app.app_context():
        users = User.query.filter(User.email == email).all()
        assert len(users) == 1
        user = users[0]
        assert user.password_hash == pwd_hash
        assert user.created_at == created_at
        assert user.first_audit_completed_at == first_audit
        run = DesktopAccessE2ETestRun.query.filter_by(run_id="runreg01").first()
        assert run.registration_completed_at is not None


def test_e2e_registration_rejects_different_email(journey_client, app, monkeypatch):
    monkeypatch.setattr(e2e, "resolve_app_env", lambda: "dev")
    with app.app_context():
        user_id, email, *_ = _admin(app, email="replay.diff@empresa.com")
        run = _create_run(user_id, run_id="runreg02")
        token = e2e.generate_e2e_cta_token(run_id=run.run_id, user_id=user_id, secret_key=SECRET)
    journey_client.post(f"/acesso-desktop/continuar/{token}", follow_redirects=False)
    before = 0
    with app.app_context():
        before = User.query.count()
    resp = journey_client.post(
        "/register",
        data={
            "nome": "Outro",
            "email": "outro.inesperado@empresa.com",
            "password": "SenhaForm#1",
            "accept_terms": "on",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with app.app_context():
        assert User.query.count() == before
        assert User.query.filter_by(email="outro.inesperado@empresa.com").first() is None


# --- Env / prod ---


def test_e2e_blocked_in_prod(app):
    user_id, email, *_ = _admin(app, email="prod.block@empresa.com")
    build_cta, build_unsub = _url_builders()
    with app.app_context():
        admin = db.session.get(User, user_id)
        with patch("app.services.admin_desktop_access_test_service.send_email") as send:
            result = e2e.start_e2e_test_run(
                admin_user=admin,
                email=email,
                secret_key=SECRET,
                build_cta_url=build_cta,
                build_unsubscribe_url=build_unsub,
                session_obj={},
                app_env="prod",
            )
        assert result["status"] == "rejected"
        assert not send.called
        assert DesktopAccessE2ETestRun.query.count() == 0


def test_e2e_token_rejected_in_prod(app):
    with app.app_context():
        user_id, *_ = _admin(app, email="tok.prod@empresa.com")
        run = _create_run(user_id)
        token = e2e.generate_e2e_cta_token(
            run_id=run.run_id, user_id=user_id, secret_key=SECRET
        )
        assert e2e.resolve_admin_test_token(token, secret_key=SECRET, app_env="dev") is not None
        assert e2e.resolve_admin_test_token(token, secret_key=SECRET, app_env="prod") is None


# --- Runner ---


def test_runner_processes_e2e_separately_in_dev(app, monkeypatch):
    monkeypatch.setattr(e2e, "resolve_app_env", lambda: "dev")
    monkeypatch.setattr(
        "app.services.admin_desktop_access_test_service.resolve_app_env",
        lambda: "dev",
    )
    from app.run_desktop_access_followup import executar_desktop_access_followup

    user_id, *_ = _admin(app, email="runner.e2e@empresa.com")
    with app.app_context():
        sent_at = _utcnow() - timedelta(hours=campaign_email.FOLLOWUP_DELAY_HOURS, minutes=5)
        _create_run(user_id, initial_email_sent_at=sent_at, created_at=sent_at)
        app.config["SECRET_KEY"] = SECRET
        with patch("app.services.lead_campaign_conversion_service.reconcile_desktop_access_leads") as recon:
            recon.return_value = {"examined": 0, "converted": 0}
            with patch("app.services.lead_campaign_email_service.process_eligible_followups") as real_fu:
                real_fu.return_value = {"candidates": 0, "sent": 0, "skipped_converted": 0, "skipped_opt_out": 0, "failed": 0}
                with patch("app.services.admin_desktop_access_test_service.send_email") as send:
                    with patch("app.run_desktop_access_followup.url_for", side_effect=lambda *a, **k: "https://x/t"):
                        summary = executar_desktop_access_followup(app)
        assert summary["followup"]["sent"] == 0
        assert summary["e2e_followup"]["sent"] == 1
        assert send.called


def test_runner_skips_e2e_in_prod(app, monkeypatch):
    monkeypatch.setattr(e2e, "resolve_app_env", lambda: "prod")
    monkeypatch.setattr(
        "app.services.admin_desktop_access_test_service.resolve_app_env",
        lambda: "prod",
    )
    from app.run_desktop_access_followup import executar_desktop_access_followup

    user_id, *_ = _admin(app, email="runner.prod@empresa.com")
    with app.app_context():
        sent_at = _utcnow() - timedelta(hours=25)
        _create_run(user_id, initial_email_sent_at=sent_at, created_at=sent_at)
        app.config["SECRET_KEY"] = SECRET
        with patch("app.services.lead_campaign_conversion_service.reconcile_desktop_access_leads") as recon:
            recon.return_value = {"examined": 0, "converted": 0}
            with patch("app.services.lead_campaign_email_service.process_eligible_followups") as real_fu:
                real_fu.return_value = {"candidates": 0, "sent": 0, "skipped_converted": 0, "skipped_opt_out": 0, "failed": 0}
                with patch("app.services.admin_desktop_access_test_service.send_email") as send:
                    summary = executar_desktop_access_followup(app)
        assert summary["e2e_followup"] == {}
        assert not send.called


# --- Migration ---


def test_migration_revision_chain_and_model():
    cfg = Config(str(ROOT / "migrations" / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert "u5v6w7x8y9z0" in heads or script.get_revision("u5v6w7x8y9z0") is not None
    rev = script.get_revision("t4u5v6w7x8y9")
    assert rev is not None
    assert rev.down_revision == "s3t4u5v6w7x8"
    # Model mapeia a tabela esperada
    assert DesktopAccessE2ETestRun.__tablename__ == "desktop_access_e2e_test_run"
    cols = {c.name for c in DesktopAccessE2ETestRun.__table__.columns}
    for required in (
        "id",
        "run_id",
        "user_id",
        "created_at",
        "initial_email_sent_at",
        "cta_clicked_at",
        "registration_completed_at",
        "followup_sent_at",
        "opt_out_at",
        "first_use_seen_at",
        "first_audit_seen_at",
        "completed_at",
        "activation_email_1_sent_at",
        "activation_email_2_sent_at",
        "activation_opt_out_at",
        "activation_sequence_started_at",
    ):
        assert required in cols


def test_migration_upgrade_downgrade_sqlite(tmp_path):
    """Valida upgrade/downgrade aditivos em SQLite isolado (não compartilhado)."""
    import importlib.util

    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine, inspect, text

    db_path = tmp_path / "e2e_mig.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE user (
                    id INTEGER PRIMARY KEY,
                    email VARCHAR(150) NOT NULL
                )
                """
            )
        )

    mig_path = ROOT / "migrations" / "versions" / "t4u5v6w7x8y9_desktop_access_e2e_test_run.py"
    spec = importlib.util.spec_from_file_location("e2e_mig_mod", mig_path)
    mig = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mig)

    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        ops = Operations(context)
        original_op = mig.op
        try:
            mig.op = ops
            mig.upgrade()
            assert "desktop_access_e2e_test_run" in inspect(engine).get_table_names()
            mig.downgrade()
            assert "desktop_access_e2e_test_run" not in inspect(engine).get_table_names()
            mig.upgrade()
            assert "desktop_access_e2e_test_run" in inspect(engine).get_table_names()
        finally:
            mig.op = original_op


# --- UI ---


def test_admin_ui_homologacao_e2e():
    template = (APP_DIR / "painel_admin" / "template_admin" / "dashboard.html").read_text(
        encoding="utf-8"
    )
    assert "Homologação E2E" in template
    assert "Iniciar novo teste E2E" in template
    assert "Verificar follow-up agora" in template
    assert "Follow-up elegível em" in template
    assert "Enviar E-mail 1 para inspeção" in template
    assert "Enviar E-mail 2 para inspeção" in template
    assert "Iniciar sequência E2E temporizada" in template
    assert "Envio para inspeção de conteúdo. Não valida a cadência de 24h/48h." in template
    assert "Ativação pós-cadastro" in template
    assert "Enviar CTA de teste" not in template
    assert "Enviar follow-up de teste" not in template
    assert "Envie a jornada real para seu próprio e-mail" in template
