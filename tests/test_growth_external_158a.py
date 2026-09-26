"""Testes direcionados SCRUM-158 Lote 158A: Meta CAPI homolog-first, fail-open.

Nao chama Meta real. Google Measurement Protocol / Ads server-side / paid externo
explicitamente fora de escopo.
"""
from __future__ import annotations

import json
import logging
import pathlib
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from app.extensions import db
from app.funnel_event_service import (
    FUNNEL_EVENT_CHECKOUT_STARTED,
    FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
    FUNNEL_EVENT_PAGE_VIEW,
    FUNNEL_EVENT_SIGNUP_COMPLETED,
    FUNNEL_SOURCE_CLEIDE_AUDIT,
    SIGNUP_METHOD_GOOGLE,
    SIGNUP_METHOD_PASSWORD,
    TASK_TYPE_AGENTE_COMPARA,
    TASK_TYPE_CLEIDE_AUDIT,
    TASK_TYPE_CLEIDE_BI,
    TASK_TYPE_FREIGHT_QUERY,
    TASK_TYPE_JULIA_CHAT,
    TASK_TYPE_ROBERTO_BI,
    TIPO_FATO_STRIPE_CHECKOUT_SESSION_CREATED,
    try_record_growth_task_event,
    try_record_signup_completed,
)
from app.growth_routes import growth_bp
from app.models import FunnelEvent, MonetizacaoFato
from app.privacy_marketing import PRIVACY_MARKETING_COOKIE_NAME
from app.services.growth_external_event_service import build_external_event_token
from app.services import growth_external_server_service as capi
from tests.conftest import seed_conta_franquia_cliente, seed_usuario

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENV_EXAMPLE = ROOT / "app/.env.example"
SERVER_SVC = ROOT / "app/services/growth_external_server_service.py"
PIXEL_JS = ROOT / "app/static/js/af_external_tracking.js"

PIXEL_ID = "meta_pixel_158a"
GRAPH_V = "v26.0"
ACCESS_TOKEN = "test_capi_token_158a"
PUBLIC_BASE = "https://www.agentefrete.com.br"
FBP_OK = "fb.1.1234567890.1111111111"
FBC_OK = "fb.1.1234567890.AbCdEf"


@pytest.fixture
def growth_client(app, ctx):
    app.config["SECRET_KEY"] = "test-secret-158a"
    app.config["TESTING"] = True
    app.config["FACEBOOK_PIXEL_ID"] = PIXEL_ID
    app.config["META_CAPI_ENABLED"] = True
    app.config["META_CAPI_ACCESS_TOKEN"] = ACCESS_TOKEN
    app.config["META_GRAPH_API_VERSION"] = GRAPH_V
    app.config["META_CAPI_TEST_EVENT_CODE"] = ""
    app.config["PUBLIC_BASE_URL"] = PUBLIC_BASE
    app.config["META_CAPI_CONNECT_TIMEOUT_SECONDS"] = 0.5
    app.config["META_CAPI_READ_TIMEOUT_SECONDS"] = 1.0
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
    conta, franquia = seed_conta_franquia_cliente(slug=f"158a-{email.split('@')[0]}")
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


def _seed_checkout_fato(*, conta_id: int, session_id: str, plan: str, franquia_id=None):
    fato = MonetizacaoFato(
        tipo_fato=TIPO_FATO_STRIPE_CHECKOUT_SESSION_CREATED,
        status_tecnico="success",
        provider="stripe",
        conta_id=int(conta_id),
        franquia_id=int(franquia_id) if franquia_id is not None else None,
        correlation_key=session_id,
        external_event_id=session_id,
        idempotency_key=f"test-158a-checkout:{session_id}",
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


def _enable_capi_env(monkeypatch, *, app_env: str = "homolog", test_code: str = ""):
    monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.setenv("META_CAPI_ENABLED", "true")
    monkeypatch.setenv("FACEBOOK_PIXEL_ID", PIXEL_ID)
    monkeypatch.setenv("META_CAPI_ACCESS_TOKEN", ACCESS_TOKEN)
    monkeypatch.setenv("META_GRAPH_API_VERSION", GRAPH_V)
    monkeypatch.setenv("PUBLIC_BASE_URL", PUBLIC_BASE)
    if test_code:
        monkeypatch.setenv("META_CAPI_TEST_EVENT_CODE", test_code)
    else:
        monkeypatch.delenv("META_CAPI_TEST_EVENT_CODE", raising=False)


def _fake_event(
    *,
    event_name: str,
    event_id: int = 42,
    metadata: dict | None = None,
    occurred_at: datetime | None = None,
):
    return SimpleNamespace(
        id=event_id,
        event_name=event_name,
        metadata_json=metadata or {},
        occurred_at=occurred_at
        or datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=5),
    )


def _request_ctx(
    app,
    *,
    privacy: str | None = "v1:accepted",
    fbp: str | None = FBP_OK,
    fbc: str | None = None,
):
    cookies = {}
    if privacy:
        cookies[PRIVACY_MARKETING_COOKIE_NAME] = privacy
    if fbp is not None:
        cookies["_fbp"] = fbp
    if fbc is not None:
        cookies["_fbc"] = fbc
    return app.test_request_context(
        "/",
        environ_base={
            "HTTP_COOKIE": "; ".join(f"{k}={v}" for k, v in cookies.items())
        },
    )


# --- CONFIG ------------------------------------------------------------------


def test_disabled_zero_http(app, monkeypatch):
    monkeypatch.setenv("META_CAPI_ENABLED", "false")
    monkeypatch.setenv("FACEBOOK_PIXEL_ID", PIXEL_ID)
    monkeypatch.setenv("META_CAPI_ACCESS_TOKEN", ACCESS_TOKEN)
    monkeypatch.setenv("META_GRAPH_API_VERSION", GRAPH_V)
    with app.app_context(), _request_ctx(app), patch.object(capi.requests, "post") as post:
        result = capi.try_send_growth_meta_capi(
            _fake_event(event_name=FUNNEL_EVENT_PAGE_VIEW, metadata={"page": "index"})
        )
        assert result["attempted"] is False
        assert result["status"] == capi.STATUS_DISABLED
        post.assert_not_called()


def test_access_token_missing_zero_http(app, monkeypatch):
    _enable_capi_env(monkeypatch)
    monkeypatch.delenv("META_CAPI_ACCESS_TOKEN", raising=False)
    app.config["META_CAPI_ACCESS_TOKEN"] = ""
    with app.app_context(), _request_ctx(app), patch.object(capi.requests, "post") as post:
        monkeypatch.setattr(capi, "_settings_attr", lambda _n: "")
        result = capi.try_send_growth_meta_capi(
            _fake_event(event_name=FUNNEL_EVENT_PAGE_VIEW, metadata={"page": "index"})
        )
        assert result["status"] == capi.STATUS_CONFIG_MISSING
        post.assert_not_called()


@pytest.mark.parametrize("version", ["", "26.0", "v26", "vX.Y", "vv26.0"])
def test_graph_version_invalid_zero_http(app, monkeypatch, version):
    _enable_capi_env(monkeypatch)
    monkeypatch.setenv("META_GRAPH_API_VERSION", version)
    app.config["META_GRAPH_API_VERSION"] = version
    with app.app_context(), _request_ctx(app), patch.object(capi.requests, "post") as post:
        def _attr(n):
            if n == "meta_graph_api_version":
                return version
            if n == "meta_capi_access_token":
                return ACCESS_TOKEN
            if n == "facebook_pixel_id":
                return PIXEL_ID
            if n == "public_base_url":
                return PUBLIC_BASE
            return ""

        monkeypatch.setattr(capi, "_settings_attr", _attr)
        result = capi.try_send_growth_meta_capi(
            _fake_event(event_name=FUNNEL_EVENT_PAGE_VIEW, metadata={"page": "index"})
        )
        assert result["status"] == capi.STATUS_CONFIG_MISSING
        post.assert_not_called()


def test_pixel_missing_zero_http(app, monkeypatch):
    _enable_capi_env(monkeypatch)
    monkeypatch.delenv("FACEBOOK_PIXEL_ID", raising=False)
    app.config["FACEBOOK_PIXEL_ID"] = ""
    with app.app_context(), _request_ctx(app), patch.object(capi.requests, "post") as post:
        def _attr(n):
            if n == "meta_capi_access_token":
                return ACCESS_TOKEN
            if n == "meta_graph_api_version":
                return GRAPH_V
            return ""

        monkeypatch.setattr(capi, "_settings_attr", _attr)
        result = capi.try_send_growth_meta_capi(
            _fake_event(event_name=FUNNEL_EVENT_PAGE_VIEW, metadata={"page": "index"})
        )
        assert result["status"] == capi.STATUS_CONFIG_MISSING
        post.assert_not_called()


@pytest.mark.parametrize(
    "test_code",
    [
        "TEST12345",
        "x" * 129,
        "TEST\nabc",
        " ",
    ],
)
def test_test_event_code_in_prod_zero_http(app, monkeypatch, test_code):
    """Prod + META_CAPI_TEST_EVENT_CODE presente (valido ou invalido) → zero HTTP."""
    _enable_capi_env(monkeypatch, app_env="prod", test_code=test_code)
    with app.app_context(), _request_ctx(app), patch.object(capi.requests, "post") as post:
        result = capi.try_send_growth_meta_capi(
            _fake_event(event_name=FUNNEL_EVENT_PAGE_VIEW, metadata={"page": "index"})
        )
        assert result["status"] == capi.STATUS_TEST_CODE_IN_PROD
        assert result["attempted"] is False
        post.assert_not_called()


def test_test_event_code_in_prod_controls_via_config_zero_http(app, monkeypatch):
    """Prod + codigo com controles (via config; NUL nao cabe em os.environ)."""
    _enable_capi_env(monkeypatch, app_env="prod", test_code="")
    monkeypatch.delenv("META_CAPI_TEST_EVENT_CODE", raising=False)
    app.config["META_CAPI_TEST_EVENT_CODE"] = "TEST\x00CTRL"
    with app.app_context(), _request_ctx(app), patch.object(capi.requests, "post") as post:
        result = capi.try_send_growth_meta_capi(
            _fake_event(event_name=FUNNEL_EVENT_PAGE_VIEW, metadata={"page": "index"})
        )
        assert result["status"] == capi.STATUS_TEST_CODE_IN_PROD
        assert result["attempted"] is False
        post.assert_not_called()


def test_test_event_code_absent_in_prod_allows_normal_gates(app, monkeypatch):
    """Prod sem test_event_code → segue gates normais (nao test_code_in_prod)."""
    _enable_capi_env(monkeypatch, app_env="prod", test_code="")
    app.config["SECRET_KEY"] = "test-secret-158a"
    app.config["META_CAPI_TEST_EVENT_CODE"] = ""
    with app.app_context(), _request_ctx(app), patch.object(capi.requests, "post") as post:
        post.return_value = MagicMock(status_code=200)
        result = capi.try_send_growth_meta_capi(
            _fake_event(event_name=FUNNEL_EVENT_PAGE_VIEW, metadata={"page": "index"})
        )
        assert result["status"] != capi.STATUS_TEST_CODE_IN_PROD
        assert result["success"] is True
        post.assert_called_once()
        assert "test_event_code" not in post.call_args.kwargs["data"]


def test_test_event_code_homolog_included_in_form(app, monkeypatch):
    _enable_capi_env(monkeypatch, app_env="homolog", test_code="TEST_HOMOLOG_158A")
    app.config["SECRET_KEY"] = "test-secret-158a"
    with app.app_context(), _request_ctx(app), patch.object(capi.requests, "post") as post:
        post.return_value = MagicMock(status_code=200)
        result = capi.try_send_growth_meta_capi(
            _fake_event(event_name=FUNNEL_EVENT_PAGE_VIEW, metadata={"page": "index"})
        )
        assert result["success"] is True
        post.assert_called_once()
        kwargs = post.call_args.kwargs
        assert kwargs["data"]["test_event_code"] == "TEST_HOMOLOG_158A"
        assert "access_token" in kwargs["data"]
        assert "data" in kwargs["data"]


@pytest.mark.parametrize(
    "bad_code",
    [
        "x" * 129,
        "BAD\nCODE",
    ],
)
def test_test_event_code_homolog_invalid_never_sent(app, monkeypatch, bad_code):
    """Homolog + codigo invalido → nunca inclui test_event_code invalido no body."""
    _enable_capi_env(monkeypatch, app_env="homolog", test_code=bad_code)
    app.config["SECRET_KEY"] = "test-secret-158a"
    with app.app_context(), _request_ctx(app), patch.object(capi.requests, "post") as post:
        post.return_value = MagicMock(status_code=200)
        result = capi.try_send_growth_meta_capi(
            _fake_event(event_name=FUNNEL_EVENT_PAGE_VIEW, metadata={"page": "index"})
        )
        if post.called:
            form = post.call_args.kwargs["data"]
            assert "test_event_code" not in form
            assert bad_code not in form.values()
        else:
            assert result["attempted"] is False
            assert result["status"] != capi.STATUS_SUCCESS


def test_test_event_code_homolog_nul_invalid_never_sent(app, monkeypatch):
    """Homolog + codigo com NUL via config → nunca envia codigo invalido."""
    _enable_capi_env(monkeypatch, app_env="homolog", test_code="")
    monkeypatch.delenv("META_CAPI_TEST_EVENT_CODE", raising=False)
    app.config["SECRET_KEY"] = "test-secret-158a"
    app.config["META_CAPI_TEST_EVENT_CODE"] = "BAD\x00CODE"
    with app.app_context(), _request_ctx(app), patch.object(capi.requests, "post") as post:
        post.return_value = MagicMock(status_code=200)
        result = capi.try_send_growth_meta_capi(
            _fake_event(event_name=FUNNEL_EVENT_PAGE_VIEW, metadata={"page": "index"})
        )
        if post.called:
            form = post.call_args.kwargs["data"]
            assert "test_event_code" not in form
        else:
            assert result["attempted"] is False
            assert result["status"] != capi.STATUS_SUCCESS


# --- CONSENT -----------------------------------------------------------------


@pytest.mark.parametrize(
    "privacy,expected",
    [
        ("", capi.STATUS_CONSENT_NOT_ACCEPTED),
        ("v1:rejected", capi.STATUS_CONSENT_NOT_ACCEPTED),
        ("v1:accepted", capi.STATUS_SUCCESS),
    ],
)
def test_consent_gate(app, monkeypatch, privacy, expected):
    _enable_capi_env(monkeypatch)
    app.config["SECRET_KEY"] = "test-secret-158a"
    with app.app_context(), patch.object(capi.requests, "post") as post:
        post.return_value = MagicMock(status_code=200)
        if privacy == "":
            ctx = app.test_request_context(
                "/", environ_base={"HTTP_COOKIE": f"_fbp={FBP_OK}"}
            )
        else:
            ctx = _request_ctx(app, privacy=privacy)
        with ctx:
            result = capi.try_send_growth_meta_capi(
                _fake_event(
                    event_name=FUNNEL_EVENT_PAGE_VIEW, metadata={"page": "index"}
                )
            )
        assert result["status"] == expected
        if expected == capi.STATUS_SUCCESS:
            post.assert_called_once()
        else:
            post.assert_not_called()


# --- USER_DATA ---------------------------------------------------------------


def test_fbp_valid_allowed(app, monkeypatch):
    _enable_capi_env(monkeypatch)
    app.config["SECRET_KEY"] = "test-secret-158a"
    with app.app_context(), _request_ctx(app, fbp=FBP_OK, fbc=None), patch.object(
        capi.requests, "post"
    ) as post:
        post.return_value = MagicMock(status_code=200)
        result = capi.try_send_growth_meta_capi(
            _fake_event(event_name=FUNNEL_EVENT_PAGE_VIEW, metadata={"page": "index"})
        )
        assert result["success"] is True
        data = json.loads(post.call_args.kwargs["data"]["data"])
        assert data[0]["user_data"] == {"fbp": FBP_OK}
        assert "fbc" not in data[0]["user_data"]


def test_fbc_valid_allowed(app, monkeypatch):
    _enable_capi_env(monkeypatch)
    app.config["SECRET_KEY"] = "test-secret-158a"
    with app.app_context(), _request_ctx(app, fbp=None, fbc=FBC_OK), patch.object(
        capi.requests, "post"
    ) as post:
        post.return_value = MagicMock(status_code=200)
        result = capi.try_send_growth_meta_capi(
            _fake_event(event_name=FUNNEL_EVENT_PAGE_VIEW, metadata={"page": "index"})
        )
        assert result["success"] is True
        data = json.loads(post.call_args.kwargs["data"]["data"])
        assert data[0]["user_data"] == {"fbc": FBC_OK}


def test_both_fbp_fbc_allowed(app, monkeypatch):
    _enable_capi_env(monkeypatch)
    app.config["SECRET_KEY"] = "test-secret-158a"
    with app.app_context(), _request_ctx(app, fbp=FBP_OK, fbc=FBC_OK), patch.object(
        capi.requests, "post"
    ) as post:
        post.return_value = MagicMock(status_code=200)
        result = capi.try_send_growth_meta_capi(
            _fake_event(event_name=FUNNEL_EVENT_PAGE_VIEW, metadata={"page": "index"})
        )
        assert result["success"] is True
        data = json.loads(post.call_args.kwargs["data"]["data"])
        assert data[0]["user_data"] == {"fbp": FBP_OK, "fbc": FBC_OK}


def test_no_fbp_fbc_skips(app, monkeypatch):
    _enable_capi_env(monkeypatch)
    with app.app_context(), _request_ctx(app, fbp=None, fbc=None), patch.object(
        capi.requests, "post"
    ) as post:
        result = capi.try_send_growth_meta_capi(
            _fake_event(event_name=FUNNEL_EVENT_PAGE_VIEW, metadata={"page": "index"})
        )
        assert result["status"] == capi.STATUS_USER_DATA_MISSING
        post.assert_not_called()


def test_both_fbp_fbc_invalid_user_data_missing(app, monkeypatch):
    """Ambos invalidos → user_data_missing, zero HTTP (sem normalizar)."""
    _enable_capi_env(monkeypatch)
    # Unitario: casos que strip normalizaria erroneamente.
    assert capi.read_meta_click_cookies_from_request(
        {"_fbp": "\x85abc", "_fbc": (" " * 1024) + "abc"}
    ) == {}
    # E2E via request: whitespace-only / vazio (portaveis no header Cookie).
    with app.app_context(), _request_ctx(app, fbp="   ", fbc=""), patch.object(
        capi.requests, "post"
    ) as post:
        result = capi.try_send_growth_meta_capi(
            _fake_event(event_name=FUNNEL_EVENT_PAGE_VIEW, metadata={"page": "index"})
        )
        assert result["status"] == capi.STATUS_USER_DATA_MISSING
        post.assert_not_called()


@pytest.mark.parametrize(
    "bad",
    [
        "has\x00nul",
        "has\rcr",
        "has\nlf",
        "has\x1fctrl",
        "has\x7fdel",
        "has\x85c1",
        "\x85abc",
        "abc\x00",
        "abc\r\n",
        (" " * 1024) + "abc",
        "x" * 1025,
        12345,
        "",
        "   ",
        " ",
    ],
)
def test_cookie_validation_rejects(bad):
    assert capi.is_valid_meta_click_cookie(bad) is False


def test_cookie_validation_accepts_exact_original():
    assert capi.is_valid_meta_click_cookie("abc") is True
    assert capi.is_valid_meta_click_cookie(FBP_OK) is True
    # Whitespace interno permitido: preservar exatamente, sem strip.
    assert capi.is_valid_meta_click_cookie("ab c") is True


def test_cookie_read_preserves_original_no_normalize():
    """Valido → mesmo valor original; invalido (ex. \\x85) → ausente, nao 'abc'."""
    assert capi.read_meta_click_cookies_from_request(
        {"_fbp": "abc", "_fbc": FBC_OK}
    ) == {"fbp": "abc", "fbc": FBC_OK}
    assert capi.read_meta_click_cookies_from_request({"_fbp": "\x85abc"}) == {}
    assert capi.read_meta_click_cookies_from_request(
        {"_fbp": (" " * 1024) + "abc"}
    ) == {}
    assert capi.read_meta_click_cookies_from_request({"_fbp": "ab c"}) == {"fbp": "ab c"}


def test_cookie_values_never_logged(app, monkeypatch, caplog):
    _enable_capi_env(monkeypatch)
    app.config["SECRET_KEY"] = "test-secret-158a"
    with app.app_context(), _request_ctx(app), patch.object(capi.requests, "post") as post:
        post.return_value = MagicMock(status_code=200)
        with caplog.at_level(logging.DEBUG, logger=capi.logger.name):
            capi.try_send_growth_meta_capi(
                _fake_event(
                    event_name=FUNNEL_EVENT_PAGE_VIEW, metadata={"page": "index"}
                )
            )
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert FBP_OK not in joined
        assert ACCESS_TOKEN not in joined


# --- URL ---------------------------------------------------------------------


def test_page_allowlisted_canonical_url(app, monkeypatch):
    _enable_capi_env(monkeypatch)
    app.config["SECRET_KEY"] = "test-secret-158a"
    with app.app_context(), _request_ctx(app), patch.object(capi.requests, "post") as post:
        post.return_value = MagicMock(status_code=200)
        capi.try_send_growth_meta_capi(
            _fake_event(event_name=FUNNEL_EVENT_PAGE_VIEW, metadata={"page": "fretes"})
        )
        data = json.loads(post.call_args.kwargs["data"]["data"])
        assert data[0]["event_source_url"] == f"{PUBLIC_BASE}/fretes"


def test_page_outside_capi_allowlist_skips(app, monkeypatch):
    _enable_capi_env(monkeypatch)
    with app.app_context(), _request_ctx(app), patch.object(capi.requests, "post") as post:
        result = capi.try_send_growth_meta_capi(
            _fake_event(
                event_name=FUNNEL_EVENT_PAGE_VIEW,
                metadata={"page": "reset_password"},
            )
        )
        assert result["status"] == capi.STATUS_URL_UNAVAILABLE
        post.assert_not_called()


def test_detalhe_noticia_skips_capi(app, monkeypatch):
    _enable_capi_env(monkeypatch)
    with app.app_context(), _request_ctx(app), patch.object(capi.requests, "post") as post:
        result = capi.try_send_growth_meta_capi(
            _fake_event(
                event_name=FUNNEL_EVENT_PAGE_VIEW,
                metadata={"page": "detalhe_noticia"},
            )
        )
        assert result["status"] == capi.STATUS_URL_UNAVAILABLE
        post.assert_not_called()


def test_token_paths_never_in_capi_map():
    assert "reset_password" not in capi.META_CAPI_PAGE_VIEW_PATHS
    assert "newsletter_cancelar" not in capi.META_CAPI_PAGE_VIEW_PATHS
    assert "multiuser_convite.visualizar_convite" not in capi.META_CAPI_PAGE_VIEW_PATHS
    for path in capi.META_CAPI_PAGE_VIEW_PATHS.values():
        assert "?" not in path
        assert "token" not in path.lower()


@pytest.mark.parametrize(
    "bad_base",
    [
        "http://www.agentefrete.com.br",
        "https://user:pass@www.agentefrete.com.br",
        "https://www.agentefrete.com.br?x=1",
        "https://www.agentefrete.com.br#frag",
        "https://www.agentefrete.com.br/path",
        "",
    ],
)
def test_insecure_public_base_url_skips(app, monkeypatch, bad_base):
    _enable_capi_env(monkeypatch)
    monkeypatch.setenv("PUBLIC_BASE_URL", bad_base)
    with app.app_context(), _request_ctx(app), patch.object(capi.requests, "post") as post:
        monkeypatch.setattr(
            capi,
            "_settings_attr",
            lambda n: bad_base if n == "public_base_url" else "",
        )
        result = capi.try_send_growth_meta_capi(
            _fake_event(event_name=FUNNEL_EVENT_PAGE_VIEW, metadata={"page": "index"})
        )
        assert result["status"] == capi.STATUS_URL_UNAVAILABLE
        post.assert_not_called()


def test_checkout_canonical_url(app, monkeypatch):
    _enable_capi_env(monkeypatch)
    app.config["SECRET_KEY"] = "test-secret-158a"
    with app.app_context(), _request_ctx(app), patch.object(capi.requests, "post") as post:
        post.return_value = MagicMock(status_code=200)
        capi.try_send_growth_meta_capi(
            _fake_event(
                event_name=FUNNEL_EVENT_CHECKOUT_STARTED, metadata={"plan": "starter"}
            )
        )
        data = json.loads(post.call_args.kwargs["data"]["data"])
        assert data[0]["event_name"] == "InitiateCheckout"
        assert data[0]["event_source_url"] == f"{PUBLIC_BASE}/contrate-um-plano"
        assert "custom_data" not in data[0]
        assert "plan" not in data[0]


def test_signup_canonical_url(app, monkeypatch):
    _enable_capi_env(monkeypatch)
    app.config["SECRET_KEY"] = "test-secret-158a"
    with app.app_context(), _request_ctx(app), patch.object(capi.requests, "post") as post:
        post.return_value = MagicMock(status_code=200)
        capi.try_send_growth_meta_capi(
            _fake_event(
                event_name=FUNNEL_EVENT_SIGNUP_COMPLETED,
                metadata={"signup_method": "password"},
            )
        )
        data = json.loads(post.call_args.kwargs["data"]["data"])
        assert data[0]["event_name"] == "CompleteRegistration"
        assert data[0]["event_source_url"] == f"{PUBLIC_BASE}/login"
        assert "signup_method" not in data[0]


@pytest.mark.parametrize(
    "task_type,path",
    [
        (TASK_TYPE_CLEIDE_AUDIT, "/auditoria-frete"),
        ("cleide_audit_chat", "/auditoria-frete"),
        (TASK_TYPE_AGENTE_COMPARA, "/agente-compara"),
        ("agente_compara_chat", "/agente-compara"),
        (TASK_TYPE_CLEIDE_BI, "/cleide-bi-frete"),
        ("cleide_bi_chat", "/cleide-bi-frete"),
        (TASK_TYPE_ROBERTO_BI, "/fretes"),
        ("roberto_chat", "/fretes"),
        (TASK_TYPE_FREIGHT_QUERY, "/fretes"),
    ],
)
def test_first_relevant_paths(app, monkeypatch, task_type, path):
    _enable_capi_env(monkeypatch)
    app.config["SECRET_KEY"] = "test-secret-158a"
    with app.app_context(), _request_ctx(app), patch.object(capi.requests, "post") as post:
        post.return_value = MagicMock(status_code=200)
        capi.try_send_growth_meta_capi(
            _fake_event(
                event_name=FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
                metadata={"task_type": task_type},
            )
        )
        data = json.loads(post.call_args.kwargs["data"]["data"])
        assert data[0]["event_name"] == "FirstRelevantTaskCompleted"
        assert data[0]["event_source_url"] == f"{PUBLIC_BASE}{path}"
        assert "task_type" not in data[0]
        assert "custom_data" not in data[0]


def test_julia_chat_first_relevant_skips_capi(app, monkeypatch):
    _enable_capi_env(monkeypatch)
    with app.app_context(), _request_ctx(app), patch.object(capi.requests, "post") as post:
        result = capi.try_send_growth_meta_capi(
            _fake_event(
                event_name=FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
                metadata={"task_type": TASK_TYPE_JULIA_CHAT},
            )
        )
        assert result["status"] == capi.STATUS_URL_UNAVAILABLE
        post.assert_not_called()


# --- EVENT TIME --------------------------------------------------------------


def test_event_time_naive_utc(app, monkeypatch):
    _enable_capi_env(monkeypatch)
    app.config["SECRET_KEY"] = "test-secret-158a"
    naive = datetime(2024, 6, 15, 12, 0, 0)
    with app.app_context(), _request_ctx(app), patch.object(capi.requests, "post") as post:
        post.return_value = MagicMock(status_code=200)
        capi.try_send_growth_meta_capi(
            _fake_event(
                event_name=FUNNEL_EVENT_PAGE_VIEW,
                metadata={"page": "index"},
                occurred_at=naive,
            )
        )
        data = json.loads(post.call_args.kwargs["data"]["data"])
        expected = int(naive.replace(tzinfo=timezone.utc).timestamp())
        assert data[0]["event_time"] == expected


def test_event_time_aware_to_utc(app, monkeypatch):
    _enable_capi_env(monkeypatch)
    app.config["SECRET_KEY"] = "test-secret-158a"
    aware = datetime(2024, 6, 15, 9, 0, 0, tzinfo=timezone(timedelta(hours=-3)))
    with app.app_context(), _request_ctx(app), patch.object(capi.requests, "post") as post:
        post.return_value = MagicMock(status_code=200)
        capi.try_send_growth_meta_capi(
            _fake_event(
                event_name=FUNNEL_EVENT_PAGE_VIEW,
                metadata={"page": "index"},
                occurred_at=aware,
            )
        )
        data = json.loads(post.call_args.kwargs["data"]["data"])
        assert data[0]["event_time"] == int(aware.astimezone(timezone.utc).timestamp())


def test_event_time_future_skips(app, monkeypatch):
    _enable_capi_env(monkeypatch)
    future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=2)
    with app.app_context(), _request_ctx(app), patch.object(capi.requests, "post") as post:
        result = capi.try_send_growth_meta_capi(
            _fake_event(
                event_name=FUNNEL_EVENT_PAGE_VIEW,
                metadata={"page": "index"},
                occurred_at=future,
            )
        )
        assert result["status"] == capi.STATUS_EVENT_TIME_INVALID
        post.assert_not_called()


def test_event_time_missing_skips(app, monkeypatch):
    _enable_capi_env(monkeypatch)
    evt = _fake_event(event_name=FUNNEL_EVENT_PAGE_VIEW, metadata={"page": "index"})
    evt.occurred_at = None
    with app.app_context(), _request_ctx(app), patch.object(capi.requests, "post") as post:
        result = capi.try_send_growth_meta_capi(evt)
        assert result["status"] == capi.STATUS_EVENT_TIME_INVALID
        post.assert_not_called()


# --- TOKEN -------------------------------------------------------------------


def test_same_funnel_event_same_token_browser_and_capi(app, monkeypatch):
    _enable_capi_env(monkeypatch)
    app.config["SECRET_KEY"] = "test-secret-158a"
    evt = _fake_event(
        event_name=FUNNEL_EVENT_PAGE_VIEW,
        event_id=99158,
        metadata={"page": "index"},
    )
    with app.app_context(), _request_ctx(app), patch.object(capi.requests, "post") as post:
        expected = build_external_event_token(
            event_name=FUNNEL_EVENT_PAGE_VIEW, funnel_event_id=99158
        )
        post.return_value = MagicMock(status_code=200)
        capi.try_send_growth_meta_capi(evt)
        data = json.loads(post.call_args.kwargs["data"]["data"])
        assert data[0]["event_id"] == expected
        meta_named = build_external_event_token(
            event_name="PageView", funnel_event_id=99158
        )
        assert data[0]["event_id"] != meta_named


def test_first_relevant_token_from_marco_not_task(app, ctx, monkeypatch):
    _enable_capi_env(monkeypatch)
    app.config["SECRET_KEY"] = "test-secret-158a"
    with app.app_context():
        user, _ = _auth_user(monkeypatch, email="158a-token@test.com")
        with app.test_request_context(
            "/",
            environ_base={
                "HTTP_COOKIE": (
                    f"{PRIVACY_MARKETING_COOKIE_NAME}=v1:accepted; _fbp={FBP_OK}"
                )
            },
        ), patch.object(capi.requests, "post") as post:
            post.return_value = MagicMock(status_code=200)
            result = try_record_growth_task_event(
                event_name="task_completed",
                source=FUNNEL_SOURCE_CLEIDE_AUDIT,
                task_type=TASK_TYPE_CLEIDE_AUDIT,
                idempotency_key="growth:158a:token:task",
                user=user,
                execution_id="exec-158a-token",
            )
            marco = FunnelEvent.query.filter_by(
                user_id=user.id,
                event_name=FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
            ).one()
            assert result["event"].id != marco.id
            post.assert_called_once()
            data = json.loads(post.call_args.kwargs["data"]["data"])
            assert data[0]["event_id"] == build_external_event_token(
                event_name=FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
                funnel_event_id=marco.id,
            )
            assert data[0]["event_id"] != build_external_event_token(
                event_name="task_completed",
                funnel_event_id=result["event"].id,
            )


# --- CALL SITES --------------------------------------------------------------


def test_page_view_created_attempts_capi(growth_client, app, monkeypatch):
    _enable_capi_env(monkeypatch)
    with app.app_context(), patch.object(capi.requests, "post") as post:
        post.return_value = MagicMock(status_code=200)
        _set_privacy(growth_client, "v1:accepted")
        _set_cookie(growth_client, "_fbp", FBP_OK)
        resp = growth_client.post(
            "/api/growth/page-view",
            json={"event_id": "pv-158a-new", "page": "index"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True
        assert "external_event" in resp.get_json()
        post.assert_called_once()
        data = json.loads(post.call_args.kwargs["data"]["data"])
        assert data[0]["event_name"] == "PageView"


def test_page_view_replay_no_capi(growth_client, app, monkeypatch):
    _enable_capi_env(monkeypatch)
    with app.app_context(), patch.object(capi.requests, "post") as post:
        post.return_value = MagicMock(status_code=200)
        _set_privacy(growth_client, "v1:accepted")
        _set_cookie(growth_client, "_fbp", FBP_OK)
        payload = {"event_id": "pv-158a-replay", "page": "index"}
        growth_client.post("/api/growth/page-view", json=payload)
        post.reset_mock()
        resp = growth_client.post("/api/growth/page-view", json=payload)
        assert resp.status_code == 200
        assert "external_event" not in resp.get_json()
        post.assert_not_called()


def test_signup_password_new_attempts_capi(app, monkeypatch):
    _enable_capi_env(monkeypatch)
    app.config["SECRET_KEY"] = "test-secret-158a"
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="158a-signup-pw")
        user = seed_usuario(franquia.id, conta.id, email="158a-pw@test.com")
        with app.test_request_context(
            "/register",
            environ_base={
                "HTTP_COOKIE": (
                    f"{PRIVACY_MARKETING_COOKIE_NAME}=v1:accepted; _fbp={FBP_OK}"
                )
            },
        ), patch.object(capi.requests, "post") as post:
            post.return_value = MagicMock(status_code=200)
            result = try_record_signup_completed(
                user, signup_method=SIGNUP_METHOD_PASSWORD
            )
            assert result is not None and result["created"] is True
            out = capi.try_send_growth_meta_capi_if_new(result)
            assert out["success"] is True
            post.assert_called_once()
            data = json.loads(post.call_args.kwargs["data"]["data"])
            assert data[0]["event_name"] == "CompleteRegistration"
            assert data[0]["event_source_url"].endswith("/login")


def test_signup_oauth_new_attempts_capi(app, monkeypatch):
    _enable_capi_env(monkeypatch)
    app.config["SECRET_KEY"] = "test-secret-158a"
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="158a-signup-oauth")
        user = seed_usuario(franquia.id, conta.id, email="158a-oauth@test.com")
        with app.test_request_context(
            "/login/google/callback",
            environ_base={
                "HTTP_COOKIE": (
                    f"{PRIVACY_MARKETING_COOKIE_NAME}=v1:accepted; _fbp={FBP_OK}"
                )
            },
        ), patch.object(capi.requests, "post") as post:
            post.return_value = MagicMock(status_code=200)
            result = try_record_signup_completed(
                user, signup_method=SIGNUP_METHOD_GOOGLE
            )
            out = capi.try_send_growth_meta_capi_if_new(result)
            assert out["success"] is True
            post.assert_called_once()


def test_oauth_existing_no_signup_capi(app, monkeypatch):
    _enable_capi_env(monkeypatch)
    with app.app_context(), patch.object(capi.requests, "post") as post:
        out = capi.try_send_growth_meta_capi_if_new(None)
        assert out["status"] == capi.STATUS_NOT_NEW
        post.assert_not_called()


def test_checkout_valid_new_attempts_capi(growth_client, app, monkeypatch):
    _enable_capi_env(monkeypatch)
    with app.app_context():
        user, _ = _auth_user(monkeypatch, email="158a-cs@test.com")
        _seed_checkout_fato(
            conta_id=user.conta_id,
            session_id="cs_158a_ok",
            plan="starter",
            franquia_id=user.franquia_id,
        )
        _set_privacy(growth_client, "v1:accepted")
        _set_cookie(growth_client, "_fbp", FBP_OK)
        with patch.object(capi.requests, "post") as post:
            post.return_value = MagicMock(status_code=200)
            resp = growth_client.post(
                "/api/growth/checkout-started",
                json={
                    "event_id": "cs-158a-ok",
                    "plan": "starter",
                    "checkout_session_id": "cs_158a_ok",
                },
            )
            assert resp.status_code == 200
            assert resp.get_json().get("external_event")
            post.assert_called_once()
            data = json.loads(post.call_args.kwargs["data"]["data"])
            assert data[0]["event_name"] == "InitiateCheckout"
            assert data[0]["event_source_url"].endswith("/contrate-um-plano")


def test_checkout_invalid_or_replay_no_capi(growth_client, app, monkeypatch):
    _enable_capi_env(monkeypatch)
    with app.app_context():
        _auth_user(monkeypatch, email="158a-cs-bad@test.com")
        _set_privacy(growth_client, "v1:accepted")
        _set_cookie(growth_client, "_fbp", FBP_OK)
        with patch.object(capi.requests, "post") as post:
            resp = growth_client.post(
                "/api/growth/checkout-started",
                json={
                    "event_id": "cs-158a-bad",
                    "plan": "starter",
                    "checkout_session_id": "cs_invented",
                },
            )
            assert resp.status_code == 200
            assert resp.get_json() == {"ok": True}
            post.assert_not_called()


def test_first_relevant_real_attempts_capi(app, ctx, monkeypatch):
    _enable_capi_env(monkeypatch)
    app.config["SECRET_KEY"] = "test-secret-158a"
    with app.app_context():
        user, _ = _auth_user(monkeypatch, email="158a-fr@test.com")
        with app.test_request_context(
            "/auditoria-frete",
            environ_base={
                "HTTP_COOKIE": (
                    f"{PRIVACY_MARKETING_COOKIE_NAME}=v1:accepted; _fbp={FBP_OK}"
                )
            },
        ), patch.object(capi.requests, "post") as post:
            post.return_value = MagicMock(status_code=200)
            try_record_growth_task_event(
                event_name="task_completed",
                source=FUNNEL_SOURCE_CLEIDE_AUDIT,
                task_type=TASK_TYPE_CLEIDE_AUDIT,
                idempotency_key="growth:158a:fr:task",
                user=user,
                execution_id="exec-158a-fr",
            )
            post.assert_called_once()


def test_first_relevant_recovery_no_capi(app, ctx, monkeypatch):
    _enable_capi_env(monkeypatch)
    app.config["SECRET_KEY"] = "test-secret-158a"
    with app.app_context():
        user, _ = _auth_user(monkeypatch, email="158a-rec@test.com")
        with app.test_request_context(
            "/",
            environ_base={
                "HTTP_COOKIE": (
                    f"{PRIVACY_MARKETING_COOKIE_NAME}=v1:accepted; _fbp={FBP_OK}"
                )
            },
        ), patch.object(capi.requests, "post") as post:
            post.return_value = MagicMock(status_code=200)
            first = try_record_growth_task_event(
                event_name="task_completed",
                source=FUNNEL_SOURCE_CLEIDE_AUDIT,
                task_type=TASK_TYPE_CLEIDE_AUDIT,
                idempotency_key="growth:158a:rec:task",
                user=user,
                execution_id="exec-158a-rec",
            )
            assert first["created"] is True
            post.reset_mock()
            second = try_record_growth_task_event(
                event_name="task_completed",
                source=FUNNEL_SOURCE_CLEIDE_AUDIT,
                task_type=TASK_TYPE_CLEIDE_AUDIT,
                idempotency_key="growth:158a:rec:task",
                user=user,
                execution_id="exec-158a-rec",
            )
            assert second["created"] is False
            post.assert_not_called()


def test_agente_compara_idempotent_replay_no_capi(app, ctx, monkeypatch):
    _enable_capi_env(monkeypatch)
    app.config["SECRET_KEY"] = "test-secret-158a"
    with app.app_context():
        user, _ = _auth_user(monkeypatch, email="158a-idem@test.com")
        with app.test_request_context(
            "/",
            environ_base={
                "HTTP_COOKIE": (
                    f"{PRIVACY_MARKETING_COOKIE_NAME}=v1:accepted; _fbp={FBP_OK}"
                )
            },
        ), patch.object(capi.requests, "post") as post:
            post.return_value = MagicMock(status_code=200)
            try_record_growth_task_event(
                event_name="task_completed",
                source="agente_compara",
                task_type=TASK_TYPE_AGENTE_COMPARA,
                idempotency_key="growth:158a:idem:task",
                user=user,
                execution_id="exec-158a-idem",
                allow_external_first_relevant=False,
            )
            post.assert_not_called()


# --- HTTP --------------------------------------------------------------------


def test_http_form_and_timeouts(app, monkeypatch):
    _enable_capi_env(monkeypatch)
    app.config["SECRET_KEY"] = "test-secret-158a"
    monkeypatch.setenv("META_CAPI_CONNECT_TIMEOUT_SECONDS", "0.4")
    monkeypatch.setenv("META_CAPI_READ_TIMEOUT_SECONDS", "0.9")
    with app.app_context(), _request_ctx(app), patch.object(capi.requests, "post") as post:
        post.return_value = MagicMock(status_code=200)
        capi.try_send_growth_meta_capi(
            _fake_event(event_name=FUNNEL_EVENT_PAGE_VIEW, metadata={"page": "index"})
        )
        kwargs = post.call_args.kwargs
        assert kwargs["allow_redirects"] is False
        assert kwargs["timeout"] == (0.4, 0.9)
        form = kwargs["data"]
        assert form["access_token"] == ACCESS_TOKEN
        payload = json.loads(form["data"])
        assert isinstance(payload, list) and len(payload) == 1
        assert "test_event_code" not in form
        url = post.call_args.args[0] if post.call_args.args else post.call_args[0][0]
        assert ACCESS_TOKEN not in url
        assert f"graph.facebook.com/{GRAPH_V}/{PIXEL_ID}/events" in url


def test_timeout_fail_open(app, monkeypatch):
    _enable_capi_env(monkeypatch)
    app.config["SECRET_KEY"] = "test-secret-158a"
    with app.app_context(), _request_ctx(app), patch.object(capi.requests, "post") as post:
        post.side_effect = requests.Timeout()
        result = capi.try_send_growth_meta_capi(
            _fake_event(event_name=FUNNEL_EVENT_PAGE_VIEW, metadata={"page": "index"})
        )
        assert result["attempted"] is True
        assert result["success"] is False
        assert result["status"] == capi.STATUS_TIMEOUT


def test_http_error_fail_open(app, monkeypatch):
    _enable_capi_env(monkeypatch)
    app.config["SECRET_KEY"] = "test-secret-158a"
    with app.app_context(), _request_ctx(app), patch.object(capi.requests, "post") as post:
        post.return_value = MagicMock(status_code=500, text="SECRET BODY SHOULD NOT LOG")
        result = capi.try_send_growth_meta_capi(
            _fake_event(event_name=FUNNEL_EVENT_PAGE_VIEW, metadata={"page": "index"})
        )
        assert result["attempted"] is True
        assert result["success"] is False
        assert result["status"] == capi.STATUS_HTTP_ERROR
        assert result["http_status"] == 500


def test_response_body_not_logged(app, monkeypatch, caplog):
    _enable_capi_env(monkeypatch)
    app.config["SECRET_KEY"] = "test-secret-158a"
    secret_body = "META_RESPONSE_SECRET_158A"
    with app.app_context(), _request_ctx(app), patch.object(capi.requests, "post") as post:
        post.return_value = MagicMock(status_code=400, text=secret_body)
        with caplog.at_level(logging.DEBUG, logger=capi.logger.name):
            capi.try_send_growth_meta_capi(
                _fake_event(
                    event_name=FUNNEL_EVENT_PAGE_VIEW, metadata={"page": "index"}
                )
            )
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert secret_body not in joined
        assert ACCESS_TOKEN not in joined
        assert FBP_OK not in joined


# --- PRIVACY / SCOPE ---------------------------------------------------------


def test_payload_has_no_pii_ip_ua_custom_data(app, monkeypatch):
    _enable_capi_env(monkeypatch)
    app.config["SECRET_KEY"] = "test-secret-158a"
    with app.app_context(), _request_ctx(app, fbp=FBP_OK, fbc=FBC_OK), patch.object(
        capi.requests, "post"
    ) as post:
        post.return_value = MagicMock(status_code=200)
        capi.try_send_growth_meta_capi(
            _fake_event(
                event_name=FUNNEL_EVENT_CHECKOUT_STARTED,
                metadata={
                    "plan": "pro",
                    "signup_method": "password",
                    "task_type": "x",
                },
            )
        )
        data = json.loads(post.call_args.kwargs["data"]["data"])[0]
        ud = data["user_data"]
        assert set(ud.keys()) <= {"fbp", "fbc"}
        for forbidden in (
            "client_ip_address",
            "client_user_agent",
            "em",
            "ph",
            "fn",
            "ln",
            "external_id",
            "email",
            "phone",
        ):
            assert forbidden not in ud
        assert "custom_data" not in data
        assert "plan" not in data
        assert "signup_method" not in data
        assert "task_type" not in data


def test_paid_google_mp_ads_openai_out_of_scope():
    text = SERVER_SVC.read_text(encoding="utf-8")
    assert "Measurement Protocol" in text
    # Mencao documental de NAO implementar; sem env/config operacional.
    assert "os.getenv(\"GOOGLE_ANALYTICS_API_SECRET\")" not in text
    assert "Purchase" in text
    assert "requests.post" in text
    assert re.search(r"paid", text, re.I)
    env = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "META_CAPI_ENABLED=false" in env
    # Comentario pode citar o nome; assignment operacional e proibido.
    assert not re.search(r"^GOOGLE_ANALYTICS_API_SECRET=", env, re.M)
    assert "Measurement Protocol" in env


def test_acesso_desktop_not_in_capi_page_map():
    assert "acesso_desktop" not in capi.META_CAPI_PAGE_VIEW_PATHS
    for path in capi.META_CAPI_PAGE_VIEW_PATHS.values():
        assert path != "/acesso-desktop"


def test_default_meta_capi_enabled_false_in_env_example():
    env = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert re.search(r"^META_CAPI_ENABLED=false\s*$", env, re.M)
    assert "META_CAPI_ACCESS_TOKEN=" in env
    assert "META_GRAPH_API_VERSION=" in env
    assert "META_CAPI_TEST_EVENT_CODE=" in env


def test_event_name_map_matches_browser_js():
    js = PIXEL_JS.read_text(encoding="utf-8")
    assert 'page_view: "PageView"' in js
    assert 'signup_completed: "CompleteRegistration"' in js
    assert 'checkout_started: "InitiateCheckout"' in js
    assert 'first_relevant_task_completed: "FirstRelevantTaskCompleted"' in js
    assert capi.META_CAPI_EVENT_NAME_MAP[FUNNEL_EVENT_PAGE_VIEW] == "PageView"
    assert (
        capi.META_CAPI_EVENT_NAME_MAP[FUNNEL_EVENT_SIGNUP_COMPLETED]
        == "CompleteRegistration"
    )
    assert (
        capi.META_CAPI_EVENT_NAME_MAP[FUNNEL_EVENT_CHECKOUT_STARTED]
        == "InitiateCheckout"
    )
    assert (
        capi.META_CAPI_EVENT_NAME_MAP[FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED]
        == "FirstRelevantTaskCompleted"
    )


def test_unsupported_growth_events_never_capi(app, monkeypatch):
    _enable_capi_env(monkeypatch)
    with app.app_context(), _request_ctx(app), patch.object(capi.requests, "post") as post:
        for name in (
            "paid",
            "plan_selected",
            "cta_clicked",
            "signup_started",
            "task_completed",
        ):
            result = capi.try_send_growth_meta_capi(
                _fake_event(event_name=name, metadata={})
            )
            assert result["status"] == capi.STATUS_UNSUPPORTED_EVENT
        post.assert_not_called()


def test_graph_version_validator():
    assert capi.is_valid_meta_graph_api_version("v26.0") is True
    assert capi.is_valid_meta_graph_api_version("v1.0") is True
    assert capi.is_valid_meta_graph_api_version("v26") is False
    assert capi.is_valid_meta_graph_api_version(None) is False
