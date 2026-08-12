"""Modo de teste admin da Landing Desktop — isolamento de aquisição e analytics."""
from __future__ import annotations

import importlib
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from itsdangerous import URLSafeTimedSerializer

from app.extensions import db
from app.models import FunnelEvent, Lead, User
from app.services import admin_desktop_access_test_service as desktop_test
from app.services.lead_acquisition_service import CAMPANHA_ACESSO_DESKTOP, FONTE_LANDING
from tests.conftest import seed_conta_franquia_cliente, seed_usuario

SECRET = "test-secret-desktop-access-admin-test"


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", SECRET)
    return importlib.import_module("app.web")


def _common_dashboard_mocks(monkeypatch, *, acquisition_payload=None):
    from app.painel_admin import admin_routes

    monkeypatch.setattr(admin_routes, "verificar_acesso_admin", lambda: True)
    monkeypatch.setattr(
        "app.services.admin_dashboard_service.get_dashboard_metrics",
        lambda **_kwargs: {"total_usuarios": 1, "total_usuarios_pagantes": 1, "total_leads": 1},
    )
    monkeypatch.setattr("app.services.admin_dashboard_service.list_categorias_distintas", lambda: [])
    monkeypatch.setattr("app.services.admin_dashboard_service.list_franquia_status_distintos", lambda: [])
    monkeypatch.setattr("app.services.agent_service.obter_kpis_insight", lambda: {"recomendacoes_pendentes": 0})
    monkeypatch.setattr("app.services.agent_service.obter_recomendacoes_recentes", lambda limite=15: [])
    monkeypatch.setattr("app.services.ia_metrics_service.get_ia_dashboard_payload", lambda _ano, _mes: {})
    monkeypatch.setattr(
        "app.services.onboarding_admin_analytics_service.get_onboarding_word_cloud",
        lambda **kwargs: {
            "terms": [],
            "admin_hidden_terms": [],
            "total_raw_occurrences": 0,
            "total_filtered_occurrences": 0,
            "pareto_coverage": 0,
            "pareto_target": 0.8,
            "days": 30,
            "removed_terms": {"stopwords": {}, "admin_hidden": {}},
        },
    )
    monkeypatch.setattr(
        "app.services.admin_conversion_dashboard_service.get_conversion_dashboard_payload",
        lambda **_kwargs: {
            "filters": {
                "source": "all",
                "days": 30,
                "source_options": [],
                "period_options": [],
            },
            "period": {"start_utc": None, "end_utc": None, "label": "Ultimos 30 dias"},
            "kpis": {
                "uploaded_users": 0,
                "completed_users": 0,
                "first_audit_users": 0,
                "completion_rate": 0.0,
                "first_audit_rate": 0.0,
                "abandoned_users": 0,
                "upload_events": 0,
                "completion_events": 0,
            },
            "funnel": [],
            "series": [],
            "data_quality": {"has_data": False, "warnings": [], "service_failed": False},
        },
    )
    if acquisition_payload is not None:
        monkeypatch.setattr(
            "app.services.admin_acquisition_dashboard_service.get_acquisition_dashboard_payload",
            lambda **_kwargs: acquisition_payload,
        )
    else:
        monkeypatch.setattr(
            "app.services.admin_acquisition_dashboard_service.get_acquisition_dashboard_payload",
            lambda **_kwargs: {
                "campaign": CAMPANHA_ACESSO_DESKTOP,
                "period": {"label": "Ultimos 30 dias", "days": 30},
                "stages": {"lead": 0, "click": 0, "registration": 0, "first_use": 0, "first_audit": 0},
                "rates": {"lead_to_first_audit": 0.0},
                "funnel": [
                    {"key": "lead", "label": "Leads", "count": 0, "rate_from_previous": 1.0},
                    {"key": "click", "label": "Cliques", "count": 0, "rate_from_previous": 0.0},
                    {"key": "registration", "label": "Cadastros", "count": 0, "rate_from_previous": 0.0},
                    {"key": "first_use", "label": "Primeiro uso", "count": 0, "rate_from_previous": 0.0},
                    {"key": "first_audit", "label": "Primeira auditoria", "count": 0, "rate_from_previous": 0.0},
                ],
                "data_quality": {"has_data": False, "warnings": [], "service_failed": False},
            },
        )


def _admin_user(app, *, email="admin.teste@empresa.com", is_admin=True):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug=f"conta-{email.split('@')[0]}")
        user = seed_usuario(franquia.id, conta.id, email=email)
        user.is_admin = is_admin
        db.session.commit()
        return user.id, user.email


# --- Env / UI ---


@pytest.mark.parametrize("env", ["dev", "homolog"])
def test_ui_aparece_em_dev_e_homolog(monkeypatch, env):
    web = _load_web_module()
    from app.painel_admin import admin_routes

    _common_dashboard_mocks(monkeypatch)
    monkeypatch.setattr(desktop_test, "resolve_app_env", lambda: env)
    monkeypatch.setattr(
        "app.services.admin_desktop_access_test_service.resolve_app_env",
        lambda: env,
    )
    monkeypatch.setattr(
        "app.services.admin_desktop_access_test_service.build_latest_run_status_payload",
        lambda _user_id: None,
    )

    with web.app.test_request_context("/admin/dashboard"):
        from flask import session
        from flask_login import login_user

        # current_user email for template
        fake = SimpleNamespace(
            is_authenticated=True,
            id=1,
            email="admin@test.com",
            is_admin=True,
        )
        monkeypatch.setattr(admin_routes, "current_user", fake)
        html = admin_routes.admin_dashboard.__wrapped__()

    assert "Homologação E2E" in html
    assert "Iniciar novo teste E2E" in html
    assert "Verificar follow-up agora" in html
    assert "Enviar CTA de teste" not in html
    assert "Clique " in html and "para visualizar a página." in html
    assert 'href="/acesso-desktop"' in html or "acesso-desktop" in html


def test_ui_nao_aparece_em_prod(monkeypatch):
    web = _load_web_module()
    from app.painel_admin import admin_routes

    _common_dashboard_mocks(monkeypatch)
    monkeypatch.setattr(
        "app.services.admin_desktop_access_test_service.resolve_app_env",
        lambda: "prod",
    )
    monkeypatch.setattr(admin_routes, "current_user", SimpleNamespace(
        is_authenticated=True, id=1, email="admin@test.com", is_admin=True,
    ))

    with web.app.test_request_context("/admin/dashboard"):
        html = admin_routes.admin_dashboard.__wrapped__()

    assert "Homologação E2E" not in html
    assert "Iniciar novo teste E2E" not in html
    assert "Clique " in html  # link da landing permanece


def test_landing_link_usa_url_for_acesso_desktop(monkeypatch):
    web = _load_web_module()
    from app.painel_admin import admin_routes
    from flask import url_for

    _common_dashboard_mocks(monkeypatch)
    monkeypatch.setattr(
        "app.services.admin_desktop_access_test_service.resolve_app_env",
        lambda: "prod",
    )
    monkeypatch.setattr(admin_routes, "current_user", SimpleNamespace(
        is_authenticated=True, id=1, email="admin@test.com", is_admin=True,
    ))

    with web.app.test_request_context("/admin/dashboard"):
        html = admin_routes.admin_dashboard.__wrapped__()
        expected = url_for("acesso_desktop")

    assert "Clique " in html
    assert f'href="{expected}"' in html
    assert ">aqui</a>" in html


def test_post_prod_bloqueado(monkeypatch, app):
    web = _load_web_module()
    from app.painel_admin import admin_routes

    _common_dashboard_mocks(monkeypatch)
    monkeypatch.setattr(
        "app.services.admin_desktop_access_test_service.resolve_app_env",
        lambda: "prod",
    )
    monkeypatch.setattr(admin_routes, "current_user", SimpleNamespace(
        is_authenticated=True, id=1, email="admin@test.com", is_admin=True,
    ))

    with patch.object(desktop_test, "send_email") as send_mock:
        with web.app.test_request_context(
            "/admin/dashboard",
            method="POST",
            data={
                "action": desktop_test.ACTION_START_E2E,
                "test_email": "admin@test.com",
                "desktop_access_test_csrf": "x",
            },
        ):
            from flask import session

            session[desktop_test.SESSION_CSRF_KEY] = "x"
            result = admin_routes.admin_dashboard.__wrapped__()

    assert result.status_code == 302
    send_mock.assert_not_called()


def test_nao_admin_bloqueado(monkeypatch):
    web = _load_web_module()
    from app.painel_admin import admin_routes

    monkeypatch.setattr(admin_routes, "verificar_acesso_admin", lambda: False)
    with web.app.test_request_context("/admin/dashboard"):
        result = admin_routes.admin_dashboard.__wrapped__()
    assert result == ("Acesso Negado", 403)


# --- Tokens ---


def test_token_valido_e_sem_email_no_payload(app):
    with app.app_context():
        token = desktop_test.generate_test_cta_token(user_id=42, run_id="abc123", secret_key=SECRET)
        payload = desktop_test.loads_test_cta_payload(token, secret_key=SECRET)
        assert payload is not None
        assert payload["purpose"] == desktop_test.PURPOSE_TEST_CTA
        assert payload["user_id"] == 42
        assert payload["run_id"] == "abc123"
        assert "email" not in payload


def test_token_expirado(app):
    with app.app_context():
        token = desktop_test.generate_test_cta_token(user_id=1, run_id="r1", secret_key=SECRET)
        assert (
            desktop_test.loads_test_cta_payload(token, secret_key=SECRET, max_age=-1) is None
        )


def test_token_purpose_errado(app):
    with app.app_context():
        bad = URLSafeTimedSerializer(SECRET, salt="desktop-access-e2e-cta-salt").dumps(
            {"purpose": "desktop_access_cta", "run_id": "r", "user_id": 1}
        )
        assert desktop_test.loads_test_cta_payload(bad, secret_key=SECRET) is None


def test_token_adulterado(app):
    with app.app_context():
        token = desktop_test.generate_test_cta_token(user_id=1, run_id="r1", secret_key=SECRET)
        assert desktop_test.loads_test_cta_payload(token + "x", secret_key=SECRET) is None


def test_token_com_email_no_payload_rejeitado(app):
    with app.app_context():
        bad = URLSafeTimedSerializer(SECRET, salt="desktop-access-e2e-cta-salt").dumps(
            {
                "purpose": desktop_test.PURPOSE_TEST_CTA,
                "run_id": "r",
                "user_id": 1,
                "email": "x@y.com",
            }
        )
        assert desktop_test.loads_test_cta_payload(bad, secret_key=SECRET) is None


def test_token_real_nao_e_interpretado_como_teste(app):
    from app.services import lead_campaign_email_service as campaign_email

    with app.app_context():
        real = campaign_email.generate_cta_token(99, secret_key=SECRET)
        assert desktop_test.resolve_admin_test_token(real, secret_key=SECRET) is None


# --- Envio admin / Lead isolation ---


def test_email_diferente_do_admin_bloqueado(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-admin-email")
        admin = seed_usuario(franquia.id, conta.id, email="admin.ok@empresa.com")
        admin.is_admin = True
        db.session.commit()
        ok, msg, user = desktop_test.validate_admin_test_email(
            admin_user=admin,
            email="outro@empresa.com",
        )
        assert ok is False
        assert user is None


def test_email_do_admin_aceito(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-admin-ok")
        admin = seed_usuario(franquia.id, conta.id, email="Admin.OK@empresa.com")
        admin.is_admin = True
        db.session.commit()
        ok, msg, user = desktop_test.validate_admin_test_email(
            admin_user=admin,
            email="admin.ok@empresa.com",
        )
        assert ok is True
        assert user.id == admin.id


def test_cta_e_followup_enviam_e_nao_criam_lead(app):
    from app.services import lead_campaign_email_service as campaign_email

    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-send")
        admin = seed_usuario(franquia.id, conta.id, email="send.admin@empresa.com")
        admin.is_admin = True
        db.session.commit()
        leads_before = Lead.query.count()
        sess = {}
        build_cta = lambda t: f"https://example.test/c/{t}"
        build_unsub = lambda t: f"https://example.test/u/{t}"

        with patch.object(desktop_test, "send_email") as send_mock:
            r1 = desktop_test.start_e2e_test_run(
                admin_user=admin,
                email="send.admin@empresa.com",
                secret_key=SECRET,
                build_cta_url=build_cta,
                build_unsubscribe_url=build_unsub,
                session_obj=sess,
                app_env="dev",
            )
            assert r1["status"] == "sent"
            assert send_mock.call_args.kwargs["subject"] == campaign_email.CTA_EMAIL_SUBJECT
            assert "[TESTE]" not in send_mock.call_args.kwargs["subject"]
            assert "Cancelar mensagens" in send_mock.call_args.kwargs["html"]
            assert "teste administrativo" not in send_mock.call_args.kwargs["html"].lower()

        assert Lead.query.count() == leads_before
        from app.models import DesktopAccessE2ETestRun

        assert DesktopAccessE2ETestRun.query.filter_by(user_id=admin.id).count() == 1


def test_mesmo_email_pode_repetir_apos_cooldown(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-repeat")
        admin = seed_usuario(franquia.id, conta.id, email="repeat@empresa.com")
        admin.is_admin = True
        db.session.commit()
        sess = {}
        build_cta = lambda t: f"https://x/{t}"
        build_unsub = lambda t: f"https://x/u/{t}"
        with patch.object(desktop_test, "send_email"):
            first = desktop_test.start_e2e_test_run(
                admin_user=admin,
                email="repeat@empresa.com",
                secret_key=SECRET,
                build_cta_url=build_cta,
                build_unsubscribe_url=build_unsub,
                session_obj=sess,
                app_env="dev",
            )
            second = desktop_test.start_e2e_test_run(
                admin_user=admin,
                email="repeat@empresa.com",
                secret_key=SECRET,
                build_cta_url=build_cta,
                build_unsubscribe_url=build_unsub,
                session_obj=sess,
                app_env="dev",
            )
            assert first["status"] == "sent"
            assert second["status"] == "cooldown"
            sess.pop(desktop_test.SESSION_START_LAST_SENT_KEY, None)
            third = desktop_test.start_e2e_test_run(
                admin_user=admin,
                email="repeat@empresa.com",
                secret_key=SECRET,
                build_cta_url=build_cta,
                build_unsubscribe_url=build_unsub,
                session_obj=sess,
                app_env="dev",
            )
            assert third["status"] == "sent"
            assert third["run_id"] != first["run_id"]


def test_envio_nao_altera_lead_existente(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-lead-exist")
        admin = seed_usuario(franquia.id, conta.id, email="lead.exist@empresa.com")
        admin.is_admin = True
        db.session.commit()
        captured = datetime(2026, 1, 1, 12, 0, 0)
        lead = Lead(
            email="lead.exist@empresa.com",
            acquisition_campaign=CAMPANHA_ACESSO_DESKTOP,
            acquisition_source=FONTE_LANDING,
            campaign_captured_at=captured,
            cta_email_sent_at=captured,
            followup_count=0,
        )
        db.session.add(lead)
        db.session.commit()
        lead_id = lead.id
        snapshot = {
            "acquisition_campaign": lead.acquisition_campaign,
            "acquisition_source": lead.acquisition_source,
            "campaign_captured_at": lead.campaign_captured_at,
            "cta_email_sent_at": lead.cta_email_sent_at,
            "cta_clicked_at": lead.cta_clicked_at,
            "converted_user_id": lead.converted_user_id,
            "converted_at": lead.converted_at,
            "followup_count": lead.followup_count,
            "last_followup_sent_at": lead.last_followup_sent_at,
            "opt_out_at": lead.opt_out_at,
        }
        sess = {}
        with patch.object(desktop_test, "send_email"):
            desktop_test.start_e2e_test_run(
                admin_user=admin,
                email="lead.exist@empresa.com",
                secret_key=SECRET,
                build_cta_url=lambda t: f"https://x/{t}",
                build_unsubscribe_url=lambda t: f"https://x/u/{t}",
                session_obj=sess,
                app_env="dev",
            )
        refreshed = db.session.get(Lead, lead_id)
        for key, value in snapshot.items():
            assert getattr(refreshed, key) == value


def test_csrf_invalido_bloqueia(monkeypatch, app):
    web = _load_web_module()
    from app.painel_admin import admin_routes

    _common_dashboard_mocks(monkeypatch)
    monkeypatch.setattr(
        "app.services.admin_desktop_access_test_service.resolve_app_env",
        lambda: "dev",
    )
    user_id, email = _admin_user(app, email="csrf.admin@empresa.com")
    monkeypatch.setattr(
        admin_routes,
        "current_user",
        SimpleNamespace(is_authenticated=True, id=user_id, email=email, is_admin=True),
    )

    with patch.object(desktop_test, "send_email") as send_mock:
        with web.app.test_request_context(
            "/admin/dashboard",
            method="POST",
            data={
                "action": desktop_test.ACTION_START_E2E,
                "test_email": email,
                "desktop_access_test_csrf": "token-errado",
            },
        ):
            from flask import session

            session[desktop_test.SESSION_CSRF_KEY] = "token-certo"
            result = admin_routes.admin_dashboard.__wrapped__()

    assert result.status_code == 302
    send_mock.assert_not_called()


def test_prg_apos_post_sucesso(monkeypatch, app):
    web = _load_web_module()
    from app.painel_admin import admin_routes

    _common_dashboard_mocks(monkeypatch)
    monkeypatch.setattr(
        "app.services.admin_desktop_access_test_service.resolve_app_env",
        lambda: "dev",
    )
    monkeypatch.setattr(
        admin_routes,
        "current_user",
        SimpleNamespace(is_authenticated=True, id=1, email="prg.admin@empresa.com", is_admin=True),
    )
    monkeypatch.setattr(
        "app.services.admin_desktop_access_test_service.start_e2e_test_run",
        lambda **_kwargs: {"status": "sent", "run_id": "r1", "user_id": 1},
    )

    with web.app.test_request_context(
        "/admin/dashboard",
        method="POST",
        data={
            "action": desktop_test.ACTION_START_E2E,
            "test_email": "prg.admin@empresa.com",
            "desktop_access_test_csrf": "ok-token",
        },
    ):
        from flask import session

        session[desktop_test.SESSION_CSRF_KEY] = "ok-token"
        result = admin_routes.admin_dashboard.__wrapped__()

    assert result.status_code == 302
    assert "/admin" in (result.location or "")


# --- CTA GET/POST ---


from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "app"


@pytest.fixture
def continuar_client(app):
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
        "login": ("/login", lambda: ("login", 200), ["GET", "POST"]),
        "cleide.cleide_auditoria": ("/auditoria-frete", lambda: ("ok", 200), ["GET"]),
    }
    for endpoint, (rule, view, methods) in rules.items():
        if endpoint not in app.view_functions:
            app.add_url_rule(rule, endpoint=endpoint, view_func=view, methods=methods)
    return app.test_client()


def test_cta_get_nao_ativa_test_mode(continuar_client, app):
    from app.models import DesktopAccessE2ETestRun

    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-cta-get")
        user = seed_usuario(franquia.id, conta.id, email="cta.get@empresa.com")
        user.is_admin = True
        db.session.add(
            DesktopAccessE2ETestRun(
                run_id="run-get",
                user_id=user.id,
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
                initial_email_sent_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
        db.session.commit()
        token = desktop_test.generate_test_cta_token(
            user_id=user.id, run_id="run-get", secret_key=SECRET
        )

    resp = continuar_client.get(f"/acesso-desktop/continuar/{token}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Continue no computador." in html
    assert "Continuar para o cadastro Free" in html
    assert "teste administrativo" not in html.lower()
    with continuar_client.session_transaction() as sess:
        assert desktop_test.SESSION_TEST_MODE_KEY not in sess


def test_cta_post_ativa_test_mode_user_correto_ja_logado(continuar_client, app, monkeypatch):
    from app.models import DesktopAccessE2ETestRun

    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-cta-post")
        user = seed_usuario(franquia.id, conta.id, email="cta.post@empresa.com")
        user.is_admin = True
        db.session.add(
            DesktopAccessE2ETestRun(
                run_id="run-post",
                user_id=user.id,
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
                initial_email_sent_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
        db.session.commit()
        user_id = user.id
        token = desktop_test.generate_test_cta_token(
            user_id=user_id, run_id="run-post", secret_key=SECRET
        )

    fake = SimpleNamespace(is_authenticated=True, id=user_id, email="cta.post@empresa.com")
    with patch("app.web.current_user", fake):
        resp = continuar_client.post(
            f"/acesso-desktop/continuar/{token}", follow_redirects=False
        )

    assert resp.status_code == 302
    assert "mode=register" in (resp.location or "")
    with continuar_client.session_transaction() as sess:
        ctx = desktop_test.get_test_mode_context(sess)
        assert ctx is not None
        assert ctx["test_user_id"] == user_id
        assert ctx["run_id"] == "run-post"


def test_cta_post_user_diferente_nao_ativa(continuar_client, app, monkeypatch):
    from app.models import DesktopAccessE2ETestRun

    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-cta-diff")
        target = seed_usuario(franquia.id, conta.id, email="target@empresa.com")
        target.is_admin = True
        other = seed_usuario(franquia.id, conta.id, email="other@empresa.com")
        db.session.add(
            DesktopAccessE2ETestRun(
                run_id="run-diff",
                user_id=target.id,
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
                initial_email_sent_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
        db.session.commit()
        token = desktop_test.generate_test_cta_token(
            user_id=target.id, run_id="run-diff", secret_key=SECRET
        )
        # E2E CTA POST always redirects to register (same as real); session activates for target run.
        other_id = other.id

    fake = SimpleNamespace(is_authenticated=True, id=other_id, email="other@empresa.com")
    with patch("app.web.current_user", fake):
        resp = continuar_client.post(
            f"/acesso-desktop/continuar/{token}", follow_redirects=False
        )

    assert resp.status_code == 302
    assert "mode=register" in (resp.location or "")
    with continuar_client.session_transaction() as sess:
        ctx = desktop_test.get_test_mode_context(sess)
        assert ctx is not None
        assert ctx["run_id"] == "run-diff"


def test_cta_post_nao_logado_ativa_e_vai_para_login(continuar_client, app, monkeypatch):
    from app.models import DesktopAccessE2ETestRun

    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-cta-anon")
        user = seed_usuario(franquia.id, conta.id, email="anon@empresa.com")
        user.is_admin = True
        db.session.add(
            DesktopAccessE2ETestRun(
                run_id="run-anon",
                user_id=user.id,
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
                initial_email_sent_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
        db.session.commit()
        token = desktop_test.generate_test_cta_token(
            user_id=user.id, run_id="run-anon", secret_key=SECRET
        )
        user_id = user.id

    fake = SimpleNamespace(is_authenticated=False, id=None)
    with patch("app.web.current_user", fake):
        resp = continuar_client.post(
            f"/acesso-desktop/continuar/{token}", follow_redirects=False
        )

    assert resp.status_code == 302
    assert "mode=register" in (resp.location or "")
    with continuar_client.session_transaction() as sess:
        ctx = desktop_test.get_test_mode_context(sess)
        assert ctx is not None
        assert ctx["test_user_id"] == user_id


# --- Contexto / Funnel / First audit / Meta ---


def test_test_mode_helper_respeita_user_e_ttl(app):
    from app.models import DesktopAccessE2ETestRun

    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-ttl")
        user = seed_usuario(franquia.id, conta.id, email="ttl@empresa.com")
        user.is_admin = True
        db.session.add(
            DesktopAccessE2ETestRun(
                run_id="r",
                user_id=user.id,
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
        db.session.commit()
        sess = {}
        desktop_test.activate_test_mode(user_id=user.id, run_id="r", session_obj=sess)
        assert desktop_test.is_desktop_access_admin_test_mode_for_user(user.id, sess) is True
        assert desktop_test.is_desktop_access_admin_test_mode_for_user(user.id + 1, sess) is False

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        sess[desktop_test.SESSION_TEST_MODE_KEY] = {
            "run_id": "r",
            "user_id": user.id,
            "test_user_id": user.id,
            "started_at": (now - timedelta(hours=2)).isoformat(timespec="seconds"),
            "expires_at": (now - timedelta(minutes=1)).isoformat(timespec="seconds"),
        }
        assert desktop_test.is_desktop_access_admin_test_mode_for_user(user.id, sess, now=now) is False
        assert desktop_test.SESSION_TEST_MODE_KEY not in sess


def test_cleide_upload_em_test_mode_nao_grava_funnel(app, monkeypatch, tmp_path):
    from tests.cleiton_doc_fixtures import make_txt
    from tests.test_cleide_audit_doc_routes import _build_local_client, _upload

    client, user = _build_local_client(
        app, monkeypatch, tmp_path, email="cleide-testmode@test.com"
    )
    monkeypatch.setattr(
        "app.cleide_audit_routes.trigger_temp_table_extraction_for_session",
        lambda **_k: None,
    )
    monkeypatch.setattr(
        "app.services.admin_desktop_access_test_service.is_desktop_access_admin_test_mode_for_current_user",
        lambda session_obj=None, now=None: True,
    )

    with app.app_context():
        before = FunnelEvent.query.count()
        first_before = db.session.get(User, user.id).first_audit_completed_at

    resp = _upload(client, "nota.txt", make_txt("conteudo seguro"))
    assert resp.status_code == 200
    body = resp.get_json()
    assert "funnel_event" not in body

    with app.app_context():
        db.session.remove()
        assert FunnelEvent.query.count() == before
        assert db.session.get(User, user.id).first_audit_completed_at == first_before


def test_agente_compara_upload_em_test_mode_nao_grava_funnel(app, monkeypatch, tmp_path, ctx):
    from tests.cleiton_doc_fixtures import make_csv
    from tests.test_agente_compara_doc_upload import (
        _setup_doc_env,
        _upload,
    )
    from app.agente_compara_comparison_state import clear_comparison_state, create_comparison
    from app.agente_compara_api_routes import agente_compara_api_bp

    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
        app.config["TESTING"] = True
        app.config["SECRET_KEY"] = SECRET
        if "agente_compara_api" not in app.blueprints:
            app.register_blueprint(agente_compara_api_bp)
        conta, franquia = seed_conta_franquia_cliente(slug="conta-ac-testmode")
        user = seed_usuario(franquia.id, conta.id, email="ac-testmode@test.com")
        fake_user = SimpleNamespace(
            is_authenticated=True,
            id=user.id,
            conta_id=user.conta_id,
            franquia_id=user.franquia_id,
        )
        monkeypatch.setattr("app.agente_compara_api_routes.current_user", fake_user)
        monkeypatch.setattr(
            "app.agente_compara_api_routes.avaliar_autorizacao_operacao_por_franquia",
            lambda _u: {"permitido": True, "modo_operacao": "normal"},
        )
        before = FunnelEvent.query.count()

    monkeypatch.setattr(
        "app.services.admin_desktop_access_test_service.is_desktop_access_admin_test_mode_for_current_user",
        lambda session_obj=None, now=None: True,
    )
    monkeypatch.setattr(
        "app.agente_compara_api_routes.trigger_temp_table_extraction_for_session",
        lambda **_k: None,
    )

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["agente_compara_doc_ids"] = []
        clear_comparison_state(session_obj=sess)
        create_comparison(session_obj=sess)

    content = make_csv([["col_a", "col_b"], ["1", "2"]])
    resp = _upload(client, "dados.csv", content, slot="1")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "funnel_event" not in body
    with app.app_context():
        db.session.remove()
        assert FunnelEvent.query.count() == before


def test_completion_em_test_mode_nao_grava_freight_nem_first_audit(app, monkeypatch):
    from app.agente_compara_calculation_execution_service import _record_calculation_funnel_event
    from app.agente_compara_calculation_execution_service import (
        BILLING_STATUS_APPLIED,
        STEP_CALCULATION_READY,
    )

    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-calc-testmode")
        user = seed_usuario(franquia.id, conta.id, email="calc-testmode@test.com")
        assert user.first_audit_completed_at is None
        user_id = user.id
        fake_user = SimpleNamespace(
            is_authenticated=True,
            id=user.id,
            conta_id=user.conta_id,
            franquia_id=user.franquia_id,
        )
        monkeypatch.setattr(
            "app.agente_compara_calculation_execution_service.current_user",
            fake_user,
        )
        monkeypatch.setattr(
            "app.services.admin_desktop_access_test_service.is_desktop_access_admin_test_mode_for_current_user",
            lambda session_obj=None, now=None: True,
        )
        cleared = {"ok": False}

        def _clear(session_obj=None):
            cleared["ok"] = True
            return True

        monkeypatch.setattr(
            "app.services.admin_desktop_access_test_service.complete_test_mode_after_successful_audit",
            _clear,
        )
        before = FunnelEvent.query.count()
        payload, created = _record_calculation_funnel_event(
            comparison_id="cmp-tm-1",
            execution_id="exec-tm-1",
            idempotent_replay=False,
            calc={
                "status": STEP_CALCULATION_READY,
                "billing_status": BILLING_STATUS_APPLIED,
                "stale": False,
            },
        )
        assert created is False
        assert "funnel_event" not in payload
        assert payload.get("is_first_audit") is False
        assert cleared["ok"] is True
        db.session.remove()
        assert FunnelEvent.query.count() == before
        refreshed = db.session.get(User, user_id)
        assert refreshed.first_audit_completed_at is None


def test_first_audit_preenchido_permanece_em_test_mode(app, monkeypatch):
    from app.agente_compara_calculation_execution_service import (
        BILLING_STATUS_APPLIED,
        STEP_CALCULATION_READY,
        _record_calculation_funnel_event,
    )

    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-calc-fa")
        user = seed_usuario(franquia.id, conta.id, email="calc-fa@test.com")
        stamp = datetime(2026, 3, 1, 10, 0, 0)
        user.first_audit_completed_at = stamp
        db.session.commit()
        user_id = user.id
        fake_user = SimpleNamespace(
            is_authenticated=True,
            id=user.id,
            conta_id=user.conta_id,
            franquia_id=user.franquia_id,
        )
        monkeypatch.setattr(
            "app.agente_compara_calculation_execution_service.current_user",
            fake_user,
        )
        monkeypatch.setattr(
            "app.services.admin_desktop_access_test_service.is_desktop_access_admin_test_mode_for_current_user",
            lambda session_obj=None, now=None: True,
        )
        monkeypatch.setattr(
            "app.services.admin_desktop_access_test_service.complete_test_mode_after_successful_audit",
            lambda session_obj=None: True,
        )
        _record_calculation_funnel_event(
            comparison_id="cmp-fa",
            execution_id="exec-fa",
            idempotent_replay=False,
            calc={
                "status": STEP_CALCULATION_READY,
                "billing_status": BILLING_STATUS_APPLIED,
                "stale": False,
            },
        )
        db.session.remove()
        assert db.session.get(User, user_id).first_audit_completed_at == stamp


def test_fora_de_test_mode_completion_grava_normal(app, monkeypatch):
    from app.agente_compara_calculation_execution_service import (
        BILLING_STATUS_APPLIED,
        STEP_CALCULATION_READY,
        _record_calculation_funnel_event,
    )

    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-calc-normal")
        user = seed_usuario(franquia.id, conta.id, email="calc-normal@test.com")
        fake_user = SimpleNamespace(
            is_authenticated=True,
            id=user.id,
            conta_id=user.conta_id,
            franquia_id=user.franquia_id,
        )
        monkeypatch.setattr(
            "app.agente_compara_calculation_execution_service.current_user",
            fake_user,
        )
        monkeypatch.setattr(
            "app.services.admin_desktop_access_test_service.is_desktop_access_admin_test_mode_for_current_user",
            lambda session_obj=None, now=None: False,
        )
        payload, created = _record_calculation_funnel_event(
            comparison_id="cmp-n",
            execution_id="exec-n",
            idempotent_replay=False,
            calc={
                "status": STEP_CALCULATION_READY,
                "billing_status": BILLING_STATUS_APPLIED,
                "stale": False,
            },
        )
        assert created is True
        assert payload["funnel_event"]["allow_meta_pixel"] is True
        assert payload["is_first_audit"] is True


def test_contexto_expirado_nao_suprime(app, monkeypatch):
    with app.app_context():
        sess = {
            desktop_test.SESSION_TEST_MODE_KEY: {
                "run_id": "old",
                "test_user_id": 1,
                "started_at": "2020-01-01T00:00:00",
                "expires_at": "2020-01-01T01:00:00",
            }
        }
        assert (
            desktop_test.is_desktop_access_admin_test_mode_for_user(
                1, sess, now=datetime(2026, 1, 1)
            )
            is False
        )


def test_limpeza_apos_conclusao(app):
    from app.models import DesktopAccessE2ETestRun

    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-limpeza")
        user = seed_usuario(franquia.id, conta.id, email="limpeza@empresa.com")
        user.is_admin = True
        db.session.add(
            DesktopAccessE2ETestRun(
                run_id="done",
                user_id=user.id,
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
        db.session.commit()
        sess = {}
        desktop_test.activate_test_mode(user_id=user.id, run_id="done", session_obj=sess)
        assert desktop_test.SESSION_TEST_MODE_KEY in sess
        desktop_test.clear_test_mode(sess)
        assert desktop_test.SESSION_TEST_MODE_KEY not in sess


def test_falha_envio_nao_marca_sucesso(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-fail-send")
        admin = seed_usuario(franquia.id, conta.id, email="fail.send@empresa.com")
        admin.is_admin = True
        db.session.commit()
        sess = {}
        with patch.object(
            desktop_test, "send_email", side_effect=RuntimeError("resend down")
        ):
            result = desktop_test.start_e2e_test_run(
                admin_user=admin,
                email="fail.send@empresa.com",
                secret_key=SECRET,
                build_cta_url=lambda t: f"https://x/{t}",
                build_unsubscribe_url=lambda t: f"https://x/u/{t}",
                session_obj=sess,
                app_env="dev",
            )
        assert result["status"] == "failed"
        assert desktop_test.SESSION_START_LAST_SENT_KEY not in sess
        assert desktop_test.SESSION_START_IN_FLIGHT_KEY not in sess


# --- Correções de auditoria: consumo em prod, admin revogado, CSRF replay ---


@pytest.mark.parametrize("origin_env", ["dev", "homolog"])
def test_token_gerado_fora_de_prod_recusado_em_prod(continuar_client, app, monkeypatch, origin_env):
    """Bloqueador: consumo em prod deve falhar mesmo com token válido emitido antes."""
    from app.models import DesktopAccessE2ETestRun

    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug=f"conta-prod-{origin_env}")
        user = seed_usuario(franquia.id, conta.id, email=f"prod.{origin_env}@empresa.com")
        user.is_admin = True
        run_id = f"run-prod-{origin_env}"
        db.session.add(
            DesktopAccessE2ETestRun(
                run_id=run_id,
                user_id=user.id,
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
                initial_email_sent_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
        db.session.commit()
        token = desktop_test.generate_test_cta_token(
            user_id=user.id, run_id=run_id, secret_key=SECRET
        )
        # Prova emissão válida no ambiente de origem.
        assert (
            desktop_test.resolve_admin_test_token(
                token, secret_key=SECRET, app_env=origin_env
            )
            is not None
        )

    monkeypatch.setattr(
        "app.services.admin_desktop_access_test_service.resolve_app_env",
        lambda: "prod",
    )
    monkeypatch.setattr(desktop_test, "resolve_app_env", lambda: "prod")

    assert (
        desktop_test.resolve_admin_test_token(token, secret_key=SECRET, app_env="prod")
        is None
    )

    fake = SimpleNamespace(is_authenticated=False, id=None)
    with patch("app.web.current_user", fake):
        get_resp = continuar_client.get(
            f"/acesso-desktop/continuar/{token}", follow_redirects=False
        )
        post_resp = continuar_client.post(
            f"/acesso-desktop/continuar/{token}", follow_redirects=False
        )

    assert get_resp.status_code == 302
    assert "/acesso-desktop" in (get_resp.location or "")
    assert post_resp.status_code == 302
    assert "/acesso-desktop" in (post_resp.location or "")
    assert "/login" not in (post_resp.location or "")
    assert "/auditoria-frete" not in (post_resp.location or "")

    with continuar_client.session_transaction() as sess:
        assert desktop_test.SESSION_TEST_MODE_KEY not in sess
        assert sess.get("post_login_next") in (None, "")
        assert desktop_test.get_test_mode_context(sess) is None


def test_admin_revogado_nao_ativa_test_mode(continuar_client, app):
    from app.infra import user_is_admin
    from app.models import DesktopAccessE2ETestRun

    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-revoked")
        user = seed_usuario(franquia.id, conta.id, email="revoked.admin@empresa.com")
        user.is_admin = True
        db.session.add(
            DesktopAccessE2ETestRun(
                run_id="run-revoked",
                user_id=user.id,
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
                initial_email_sent_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
        db.session.commit()
        user_id = user.id
        token = desktop_test.generate_test_cta_token(
            user_id=user_id, run_id="run-revoked", secret_key=SECRET
        )
        assert user_is_admin(user) is True

        # Revoga autorização administrativa canônica.
        user.is_admin = False
        db.session.commit()
        assert user_is_admin(db.session.get(User, user_id)) is False
        assert desktop_test.get_authorized_admin_test_user(user_id) is None

    fake = SimpleNamespace(is_authenticated=False, id=None)
    with patch("app.web.current_user", fake):
        get_resp = continuar_client.get(
            f"/acesso-desktop/continuar/{token}", follow_redirects=False
        )
        post_resp = continuar_client.post(
            f"/acesso-desktop/continuar/{token}", follow_redirects=False
        )

    assert get_resp.status_code == 302
    assert post_resp.status_code == 302
    with continuar_client.session_transaction() as sess:
        assert desktop_test.SESSION_TEST_MODE_KEY not in sess
        assert sess.get("post_login_next") in (None, "")


def test_csrf_replay_sequencial_bloqueia_segundo_envio(app):
    sess = {}
    token = desktop_test.issue_csrf_token(sess)
    assert desktop_test.validate_and_rotate_csrf(submitted=token, session_obj=sess) is True
    assert desktop_test.SESSION_CSRF_KEY not in sess
    # Replay sequencial do mesmo nonce.
    assert desktop_test.validate_and_rotate_csrf(submitted=token, session_obj=sess) is False


def test_mesmo_nonce_nao_permite_dois_envios(app):
    """
    Simula duplo submit sequencial com o mesmo nonce no mesmo session_obj.

    Não prova concorrência real entre workers (Flask-Session filesystem não é
    atômico cross-process); prova que o nonce consumido impede segundo envio
    e que send_email ocorre uma única vez neste cenário.
    """
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-csrf-double")
        admin = seed_usuario(franquia.id, conta.id, email="double.csrf@empresa.com")
        admin.is_admin = True
        db.session.commit()
        sess = {}
        nonce = desktop_test.issue_csrf_token(sess)
        build_cta = lambda t: f"https://x/{t}"
        build_unsub = lambda t: f"https://x/u/{t}"

        with patch.object(desktop_test, "send_email") as send_mock:
            assert desktop_test.validate_and_rotate_csrf(
                submitted=nonce, session_obj=sess
            ) is True
            first = desktop_test.start_e2e_test_run(
                admin_user=admin,
                email="double.csrf@empresa.com",
                secret_key=SECRET,
                build_cta_url=build_cta,
                build_unsubscribe_url=build_unsub,
                session_obj=sess,
                app_env="dev",
            )
            # Segundo POST reutilizando o mesmo nonce já consumido.
            assert desktop_test.validate_and_rotate_csrf(
                submitted=nonce, session_obj=sess
            ) is False
            # Mesmo se alguém chamasse send de novo sem novo CSRF, cooldown/slot bloqueia.
            second = desktop_test.start_e2e_test_run(
                admin_user=admin,
                email="double.csrf@empresa.com",
                secret_key=SECRET,
                build_cta_url=build_cta,
                build_unsubscribe_url=build_unsub,
                session_obj=sess,
                app_env="dev",
            )

        assert first["status"] == "sent"
        assert second["status"] == "cooldown"
        assert send_mock.call_count == 1


def test_activate_test_mode_recusa_em_prod():
    sess = {}
    ctx = desktop_test.activate_test_mode(
        user_id=1, run_id="x", session_obj=sess, app_env="prod"
    )
    assert ctx is None
    assert desktop_test.SESSION_TEST_MODE_KEY not in sess
