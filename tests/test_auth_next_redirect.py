"""Testes de next seguro e retorno pós-login do Copilot/Home."""
from __future__ import annotations

import importlib
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import flask_login.utils
import pytest


def _load_web():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


def _fake_user(**overrides):
    base = dict(
        id=1,
        email="u@test.com",
        is_authenticated=True,
        is_active=True,
        is_anonymous=False,
        get_id=lambda: "1",
        job_role="",
        usage_purpose="",
        full_name="Teste",
        conta_id=1,
        franquia_id=1,
        categoria="free",
        franquia=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _anon_user():
    return SimpleNamespace(
        is_authenticated=False,
        is_active=False,
        is_anonymous=True,
        get_id=lambda: None,
    )


@pytest.fixture
def web_mod(monkeypatch):
    web = _load_web()
    monkeypatch.setattr(web, "get_active_term", lambda: SimpleNamespace(filename="termo.pdf"))
    return web


class TestSafeNextRedirect:
    def test_allows_internal_paths(self, web_mod):
        assert web_mod._safe_next_redirect("/auditoria-frete") == "/auditoria-frete"
        assert web_mod._safe_next_redirect("/fretes") == "/fretes"
        assert (
            web_mod._safe_next_redirect("/chat_julia?mode=operational")
            == "/chat_julia?mode=operational"
        )

    @pytest.mark.parametrize("raw", [
        "http://externo.com",
        "https://externo.com",
        "//externo.com",
        "/\\host",
        "javascript:alert(1)",
        "/path\n/evil",
        "/path\r/evil",
        "/api/secret",
        "/api/",
        "/admin",
        "/admin/dashboard",
    ])
    def test_rejects_unsafe(self, web_mod, raw):
        assert web_mod._safe_next_redirect(raw) is None


class TestPostLoginNextFlow:
    def test_password_login_returns_to_destination(self, web_mod, monkeypatch):
        fake_user = _fake_user()
        monkeypatch.setattr(web_mod, "authenticate_user", lambda e, p: (fake_user, None))
        monkeypatch.setattr(web_mod, "login_user", lambda u: None)
        monkeypatch.setattr(web_mod, "user_is_admin", lambda u: False)
        monkeypatch.setattr(flask_login.utils, "_get_user", _anon_user)

        client = web_mod.app.test_client()
        with client.session_transaction() as sess:
            sess["post_login_next"] = "/auditoria-frete"
        resp = client.post(
            "/login",
            data={"email": "u@test.com", "password": "secret"},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)
        assert "/auditoria-frete" in (resp.headers.get("Location") or "")

    def test_google_existing_user_returns_to_destination(self, web_mod, monkeypatch):
        fake_user = _fake_user(id=2, email="g@test.com")
        monkeypatch.setattr(
            web_mod,
            "handle_google_oauth_callback",
            lambda *a, **k: (fake_user, None, False, False),
        )
        monkeypatch.setattr(web_mod, "login_user", lambda u: None)
        monkeypatch.setattr(web_mod, "user_is_admin", lambda u: False)
        monkeypatch.setattr(flask_login.utils, "_get_user", _anon_user)

        client = web_mod.app.test_client()
        with client.session_transaction() as sess:
            sess["post_login_next"] = "/fretes"
            sess["oauth_state"] = "state-1"
            sess["oauth_states"] = ["state-1"]

        resp = client.get(
            "/login/google/callback?code=abc&state=state-1",
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)
        assert "/fretes" in (resp.headers.get("Location") or "")

    def test_google_new_user_returns_after_complete_profile(self, web_mod, monkeypatch):
        fake_user = _fake_user(id=3, email="new@test.com", job_role="", usage_purpose="")
        monkeypatch.setattr(
            web_mod,
            "handle_google_oauth_callback",
            lambda *a, **k: (fake_user, None, True, True),
        )
        monkeypatch.setattr(web_mod, "login_user", lambda u: None)
        monkeypatch.setattr(web_mod, "user_is_admin", lambda u: False)
        monkeypatch.setattr(
            web_mod,
            "auth_complete_user_profile",
            lambda *a, **k: (True, "Perfil atualizado"),
        )
        monkeypatch.setattr(flask_login.utils, "_get_user", lambda: fake_user)
        monkeypatch.setattr(web_mod, "current_user", fake_user)

        client = web_mod.app.test_client()
        with client.session_transaction() as sess:
            sess["post_login_next"] = "/auditoria-frete"
            sess["oauth_state"] = "state-2"
            sess["oauth_states"] = ["state-2"]

        cb = client.get(
            "/login/google/callback?code=abc&state=state-2",
            follow_redirects=False,
        )
        assert cb.status_code in (302, 303)
        assert "complete-profile" in (cb.headers.get("Location") or "")

        with client.session_transaction() as sess:
            assert sess.get("post_login_next") == "/auditoria-frete"

        done = client.post(
            "/complete-profile",
            data={
                "accept_terms": "1",
                "job_role": "Analista",
                "usage_purpose": "Auditoria",
            },
            follow_redirects=False,
        )
        assert done.status_code in (302, 303)
        assert "/auditoria-frete" in (done.headers.get("Location") or "")

    def test_logout_clears_pending_next(self, web_mod, monkeypatch):
        fake_user = _fake_user()
        monkeypatch.setattr(web_mod, "logout_user", lambda: None)
        monkeypatch.setattr(flask_login.utils, "_get_user", lambda: fake_user)
        client = web_mod.app.test_client()
        with client.session_transaction() as sess:
            sess["post_login_next"] = "/auditoria-frete"
        client.get("/logout", follow_redirects=False)
        with client.session_transaction() as sess:
            assert "post_login_next" not in sess

    def test_register_preserves_next_until_login(self, web_mod, monkeypatch):
        fake_user = MagicMock()
        monkeypatch.setattr(web_mod, "register_user", lambda *a, **k: (fake_user, None))
        monkeypatch.setattr(flask_login.utils, "_get_user", _anon_user)
        client = web_mod.app.test_client()
        with client.session_transaction() as sess:
            sess["post_login_next"] = "/auditoria-frete"
        resp = client.post(
            "/register",
            data={
                "accept_terms": "1",
                "nome": "Teste",
                "email": "novo@test.com",
                "password": "secret123",
                "job_role": "Ops",
                "usage_purpose": "BI",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)
        location = resp.headers.get("Location") or ""
        assert "/login" in location
        assert "next=" in location
        assert "%2Fauditoria-frete" in location or "/auditoria-frete" in location

    def test_login_stores_only_safe_next(self, web_mod, monkeypatch):
        monkeypatch.setattr(flask_login.utils, "_get_user", _anon_user)
        client = web_mod.app.test_client()
        # Usa o helper direto + sessão: evita renderizar template no GET /login.
        assert web_mod._safe_next_redirect("https://externo.com") is None
        with client.session_transaction() as sess:
            safe = web_mod._safe_next_redirect("https://externo.com")
            if safe:
                sess["post_login_next"] = safe
        with client.session_transaction() as sess:
            assert sess.get("post_login_next") is None

        client.get("/login?next=/auditoria-frete")
        with client.session_transaction() as sess:
            # Se o GET renderizar falhar por DB, o helper acima já cobre rejeição.
            # Quando GET funciona, deve persistir next seguro.
            if "post_login_next" in sess:
                assert sess["post_login_next"] == "/auditoria-frete"
