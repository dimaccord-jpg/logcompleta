"""Testes das rotas admin de ocultação da nuvem de termos do onboarding."""
from __future__ import annotations

import importlib
import os
from types import SimpleNamespace

import pytest


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


def test_hide_term_route_requires_admin(monkeypatch):
    web = _load_web_module()
    from app.painel_admin import admin_routes

    monkeypatch.setattr(admin_routes, "verificar_acesso_admin", lambda: False)
    with web.app.test_request_context(
        "/admin/onboarding-word-cloud/hidden-terms",
        method="POST",
        data={"term": "ola"},
    ):
        resp = admin_routes.onboarding_word_cloud_hide_term.__wrapped__()
    assert resp[1] == 403


def test_restore_term_route_requires_admin(monkeypatch):
    web = _load_web_module()
    from app.painel_admin import admin_routes

    monkeypatch.setattr(admin_routes, "verificar_acesso_admin", lambda: False)
    with web.app.test_request_context(
        "/admin/onboarding-word-cloud/hidden-terms/1/restore",
        method="POST",
    ):
        resp = admin_routes.onboarding_word_cloud_restore_term.__wrapped__(1)
    assert resp[1] == 403


def test_hide_term_route_rejects_invalid_term(monkeypatch):
    web = _load_web_module()
    from app.painel_admin import admin_routes

    monkeypatch.setattr(admin_routes, "verificar_acesso_admin", lambda: True)
    monkeypatch.setattr(
        admin_routes,
        "current_user",
        SimpleNamespace(is_authenticated=True, is_admin=True, id=1, email="admin@example.com"),
    )
    calls = {"hide": 0}

    def _hide(raw_term, **kwargs):
        calls["hide"] += 1
        from app.services.onboarding_word_cloud_hidden_terms_service import InvalidHiddenTermError

        raise InvalidHiddenTermError("Informe um termo valido para ocultar.")

    monkeypatch.setattr(
        "app.services.onboarding_word_cloud_hidden_terms_service.hide_term",
        _hide,
    )

    with web.app.test_request_context(
        "/admin/onboarding-word-cloud/hidden-terms",
        method="POST",
        data={"term": "   "},
    ):
        resp = admin_routes.onboarding_word_cloud_hide_term.__wrapped__()

    assert resp.status_code == 302
    assert calls["hide"] == 1


def test_hide_term_route_redirects_on_success(monkeypatch):
    web = _load_web_module()
    from app.painel_admin import admin_routes

    monkeypatch.setattr(admin_routes, "verificar_acesso_admin", lambda: True)
    monkeypatch.setattr(
        admin_routes,
        "current_user",
        SimpleNamespace(is_authenticated=True, is_admin=True, id=1, email="admin@example.com"),
    )
    monkeypatch.setattr(
        "app.services.onboarding_word_cloud_hidden_terms_service.hide_term",
        lambda raw_term, **kwargs: SimpleNamespace(term_normalized="ola"),
    )

    with web.app.test_request_context(
        "/admin/onboarding-word-cloud/hidden-terms",
        method="POST",
        data={"term": "ola"},
    ):
        resp = admin_routes.onboarding_word_cloud_hide_term.__wrapped__()

    assert resp.status_code == 302
    assert "/admin/dashboard" in resp.location


def test_invalid_hidden_term_not_saved(app):
    from app.services.onboarding_word_cloud_hidden_terms_service import (
        InvalidHiddenTermError,
        hide_term,
    )
    from app.models import OnboardingWordCloudHiddenTerm

    with app.app_context():
        with pytest.raises(InvalidHiddenTermError):
            hide_term("")
        assert OnboardingWordCloudHiddenTerm.query.count() == 0
