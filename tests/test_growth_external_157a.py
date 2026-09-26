"""Testes direcionados SCRUM-157 Lote 157A: Meta+GA4 browser curado."""
from __future__ import annotations

import importlib
import json
import os
import pathlib
import re
from types import SimpleNamespace

import pytest

from app.extensions import db
from app.funnel_event_service import (
    ALLOWED_GROWTH_PAGE_VIEW_PAGES,
    FUNNEL_EVENT_CHECKOUT_STARTED,
    FUNNEL_EVENT_PAGE_VIEW,
    FUNNEL_EVENT_SIGNUP_COMPLETED,
    META_PIXEL_ALLOWED_EVENTS,
    SIGNUP_METHOD_GOOGLE,
    SIGNUP_METHOD_PASSWORD,
    TIPO_FATO_STRIPE_CHECKOUT_SESSION_CREATED,
    try_record_signup_completed,
)
from app.growth_routes import growth_bp
from app.models import FunnelEvent, MonetizacaoFato
from app.privacy_marketing import (
    PRIVACY_MARKETING_COOKIE_NAME,
    SESSION_EXTERNAL_EVENT_PENDING,
)
from app.services.growth_external_event_service import (
    build_external_event_envelope,
    build_external_event_envelope_if_new,
    build_external_event_token,
)
from tests.conftest import seed_conta_franquia_cliente, seed_usuario

ROOT = pathlib.Path(__file__).resolve().parents[1]
PIXEL_BASE = ROOT / "app/templates/partials/pixel_base.html"
GA_BASE = ROOT / "app/templates/partials/google_analytics_base.html"
PIXEL_EVENTS = ROOT / "app/templates/partials/pixel_events.html"
EXTERNAL_JS = ROOT / "app/static/js/af_external_tracking.js"
BASE_HTML = ROOT / "app/templates/base.html"
CONTRATE = ROOT / "app/templates/contrate_plano.html"
USER_AREA = ROOT / "app/user_area.py"
WEB_PY = ROOT / "app/web.py"
ACESSO_DESKTOP = ROOT / "app/templates/acesso_desktop.html"
ENV_EXAMPLE = ROOT / "app/.env.example"

GA_MEASUREMENT_ID = "G-TEST157A"
META_PIXEL_ID = "meta_pixel_157a"


def _load_web():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret-157a")
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
        "OPENAI_ADS_PIXEL_ID": module.app.config.get("OPENAI_ADS_PIXEL_ID"),
        "SECRET_KEY": module.app.config.get("SECRET_KEY"),
    }
    module.app.config["FACEBOOK_PIXEL_ID"] = ""
    module.app.config["GOOGLE_ANALYTICS_MEASUREMENT_ID"] = ""
    module.app.config["OPENAI_ADS_PIXEL_ID"] = ""
    module.app.config["SECRET_KEY"] = "test-secret-157a"
    yield module
    for key, value in original.items():
        if value is None:
            module.app.config.pop(key, None)
        else:
            module.app.config[key] = value


@pytest.fixture
def growth_client(app, ctx):
    app.config["SECRET_KEY"] = "test-secret-157a"
    app.config["TESTING"] = True
    if "growth" not in app.blueprints:
        app.register_blueprint(growth_bp)
    return app.test_client()


def _set_privacy(client, value: str) -> None:
    try:
        client.set_cookie(PRIVACY_MARKETING_COOKIE_NAME, value)
    except TypeError:
        client.set_cookie("localhost", PRIVACY_MARKETING_COOKIE_NAME, value)


def _auth_user(monkeypatch, *, email: str):
    conta, franquia = seed_conta_franquia_cliente(slug=f"157a-{email.split('@')[0]}")
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
        idempotency_key=f"test-157a-checkout:{session_id}",
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


# --- CONSENT / LOADERS -------------------------------------------------------


def test_unknown_blocks_meta_and_google_loaders(web):
    web.app.config["FACEBOOK_PIXEL_ID"] = META_PIXEL_ID
    web.app.config["GOOGLE_ANALYTICS_MEASUREMENT_ID"] = GA_MEASUREMENT_ID
    html = web.app.test_client().get("/").get_data(as_text=True)
    assert "fbevents.js" not in html
    assert "googletagmanager.com/gtag/js" not in html
    assert "af_external_tracking.js" not in html


def test_rejected_blocks_meta_and_google_loaders(web):
    web.app.config["FACEBOOK_PIXEL_ID"] = META_PIXEL_ID
    web.app.config["GOOGLE_ANALYTICS_MEASUREMENT_ID"] = GA_MEASUREMENT_ID
    client = web.app.test_client()
    _set_privacy(client, "v1:rejected")
    html = client.get("/").get_data(as_text=True)
    assert "fbevents.js" not in html
    assert "googletagmanager.com/gtag/js" not in html


def test_accepted_with_ids_loads_meta_and_google(web):
    web.app.config["FACEBOOK_PIXEL_ID"] = META_PIXEL_ID
    web.app.config["GOOGLE_ANALYTICS_MEASUREMENT_ID"] = GA_MEASUREMENT_ID
    client = web.app.test_client()
    _set_privacy(client, "v1:accepted")
    html = client.get("/").get_data(as_text=True)
    assert "fbevents.js" in html
    assert f"fbq('init', '{META_PIXEL_ID}')" in html or META_PIXEL_ID in html
    assert "googletagmanager.com/gtag/js" in html
    assert GA_MEASUREMENT_ID in html
    assert "af_external_tracking.js" in html


def test_google_absent_without_measurement_id(web):
    web.app.config["FACEBOOK_PIXEL_ID"] = META_PIXEL_ID
    web.app.config["GOOGLE_ANALYTICS_MEASUREMENT_ID"] = ""
    client = web.app.test_client()
    _set_privacy(client, "v1:accepted")
    html = client.get("/").get_data(as_text=True)
    assert "fbevents.js" in html
    assert "googletagmanager.com/gtag/js" not in html
    assert "gtag('config'" not in html


def test_google_config_send_page_view_false():
    src = GA_BASE.read_text(encoding="utf-8")
    assert "send_page_view: false" in src
    assert "analytics_storage: 'denied'" in src
    assert "analytics_storage: 'granted'" in src
    assert "googletagmanager.com/gtm.js" not in src
    assert "GTM-" not in src


def test_pixel_loader_does_not_auto_pageview():
    src = PIXEL_BASE.read_text(encoding="utf-8")
    assert "fbq('init'" in src
    assert "fbq('track', 'PageView')" not in src
    assert 'fbq("track", "PageView")' not in src


# --- PAGE VIEW ---------------------------------------------------------------


def test_page_view_new_returns_external_event(growth_client, app):
    with app.app_context():
        resp = growth_client.post(
            "/api/growth/page-view",
            json={"event_id": "pv-157a-new-1", "page": "index"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        ext = body.get("external_event")
        assert isinstance(ext, dict)
        assert ext["event"] == FUNNEL_EVENT_PAGE_VIEW
        assert ext["token"].startswith("v1_")
        assert re.fullmatch(r"v1_[0-9a-f]{32}", ext["token"])
        assert ext["token"] != str(
            FunnelEvent.query.filter_by(idempotency_key="pv-157a-new-1").one().id
        )
        assert ext["params"] == {"page": "index"}
        assert "correlation_id" not in ext
        assert "user_id" not in ext
        assert "idempotency_key" not in ext


def test_page_view_growth_failure_no_external(growth_client, app, monkeypatch):
    with app.app_context():
        monkeypatch.setattr("app.growth_routes.try_record_funnel_event", lambda **_k: None)
        resp = growth_client.post(
            "/api/growth/page-view",
            json={"event_id": "pv-157a-fail-1", "page": "index"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert "external_event" not in body


def test_page_view_replay_no_new_external(growth_client, app):
    with app.app_context():
        payload = {"event_id": "pv-157a-replay-1", "page": "index"}
        first = growth_client.post("/api/growth/page-view", json=payload).get_json()
        second = growth_client.post("/api/growth/page-view", json=payload).get_json()
        assert first.get("external_event")
        assert "external_event" not in second


def test_base_page_view_dispatches_external_event():
    src = BASE_HTML.read_text(encoding="utf-8")
    assert "external_event" in src
    assert "AFExternalTracking.dispatch" in src
    assert 'url_for("growth.growth_page_view")' in src


# --- SIGNUP ------------------------------------------------------------------


def test_password_signup_stores_pending_envelope(app, ctx):
    with app.app_context():
        app.config["SECRET_KEY"] = "test-secret-157a"
        conta, franquia = seed_conta_franquia_cliente(slug="157a-signup-pw")
        user = seed_usuario(franquia.id, conta.id, email="signup-pw-157a@test.com")
        result = try_record_signup_completed(user, signup_method=SIGNUP_METHOD_PASSWORD)
        assert result and result.get("created") is True
        envelope = build_external_event_envelope_if_new(result)
        assert envelope is not None
        assert envelope["event"] == FUNNEL_EVENT_SIGNUP_COMPLETED
        assert envelope["params"] == {"signup_method": "password"}


def test_oauth_existing_no_signup_envelope():
    """OAuth existente nao emite signup_completed; so o branch created_new_user o faz."""
    web_src = WEB_PY.read_text(encoding="utf-8")
    oauth_cb = web_src[
        web_src.index("def google_callback") : web_src.index("def complete_profile")
    ]
    assert "if created_new_user:" in oauth_cb
    assert "try_record_signup_completed" in oauth_cb
    assert oauth_cb.count("try_record_signup_completed") == 1
    assert "store_pending_external_event" in oauth_cb
    assert "try_record_session_origin_observed(user)" in oauth_cb
    # signup_completed so dentro do if created_new_user
    assert oauth_cb.index("if created_new_user:") < oauth_cb.index(
        "try_record_signup_completed"
    )
    assert oauth_cb.index("try_record_signup_completed") < oauth_cb.index(
        "try_record_session_origin_observed(user)"
    )


def test_complete_registration_boolean_flag_removed():
    web_src = WEB_PY.read_text(encoding="utf-8")
    privacy = (ROOT / "app/privacy_marketing.py").read_text(encoding="utf-8")
    events = PIXEL_EVENTS.read_text(encoding="utf-8")
    assert "pixel_event_complete_registration_once" not in web_src
    assert "SESSION_PIXEL_EVENT_COMPLETE_REGISTRATION" not in privacy
    assert 'trackEvent("CompleteRegistration")' not in events
    assert SESSION_EXTERNAL_EVENT_PENDING in privacy


def test_helper_maps_signup_and_checkout_and_page_view():
    js = EXTERNAL_JS.read_text(encoding="utf-8")
    assert 'page_view: "PageView"' in js
    assert 'signup_completed: "CompleteRegistration"' in js
    assert 'checkout_started: "InitiateCheckout"' in js
    assert 'page_view: "page_view"' in js
    assert 'signup_completed: "sign_up"' in js
    assert 'checkout_started: "begin_checkout"' in js
    assert "eventID: token" in js
    assert "page_location" in js
    assert "window.location.origin + window.location.pathname" in js
    # Token nao vai ao GA4 como dedupe
    assert "event_id" not in js.lower() or "eventID: token" in js
    assert "gtag(\"event\"" in js or "gtag('event'" in js
    # Nao passa token para gtag
    gtag_calls = [
        line for line in js.splitlines() if "gtag(" in line and "event" in line
    ]
    for line in gtag_calls:
        assert "token" not in line


def test_helper_rejects_unknown_and_extra_params_structurally():
    js = EXTERNAL_JS.read_text(encoding="utf-8")
    assert "ALLOWED_EVENTS" in js
    assert "sanitizeParams" in js
    assert "Object.prototype.hasOwnProperty.call" in js
    assert "ownHas(ALLOWED_EVENTS, eventName)" in js
    assert "sanitizePageViewParams" in js
    assert "sanitizeSignupParams" in js
    assert "sanitizeCheckoutParams" in js
    assert "PAGE_ALLOW" in js
    assert "PLAN_ALLOW" in js
    assert "SIGNUP_METHOD_ALLOW" in js
    assert "disable" in js
    assert "dedupeKey" in js
    assert "alreadySent" in js


def test_helper_rejects_inherited_event_names():
    js = EXTERNAL_JS.read_text(encoding="utf-8")
    # Nao usa ALLOWED_EVENTS[eventName] puro (aceitaria constructor/toString/__proto__).
    assert "if (!ALLOWED_EVENTS[eventName])" not in js
    assert "ownHas(ALLOWED_EVENTS, eventName)" in js
    for forbidden in ("constructor", "toString", "__proto__"):
        assert f"{forbidden}:" not in js.split("ALLOWED_EVENTS")[1].split("};")[0]


def test_helper_param_value_allowlists_reject_objects():
    js = EXTERNAL_JS.read_text(encoding="utf-8")
    assert 'typeof page !== "string"' in js
    assert 'typeof method !== "string"' in js
    assert 'typeof plan !== "string"' in js
    assert "Array.isArray(raw)" in js
    assert "String(params.page)" not in js
    assert "String(params.plan)" not in js
    assert "String(params.signup_method)" not in js


# --- CHECKOUT ----------------------------------------------------------------


def test_checkout_started_new_returns_external(growth_client, app, monkeypatch):
    with app.app_context():
        user, _ = _auth_user(monkeypatch, email="cs-157a@test.com")
        _seed_checkout_fato(
            conta_id=user.conta_id,
            session_id="cs_test_157a_1",
            plan="pro",
            franquia_id=user.franquia_id,
        )
        resp = growth_client.post(
            "/api/growth/checkout-started",
            json={
                "event_id": "cs-157a-1",
                "plan": "pro",
                "checkout_session_id": "cs_test_157a_1",
            },
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        ext = body["external_event"]
        assert ext["event"] == FUNNEL_EVENT_CHECKOUT_STARTED
        assert ext["params"] == {"plan": "pro"}
        assert "correlation_id" not in ext
        assert "checkout_session_id" not in ext
        assert "cs_test_157a_1" not in str(ext)


def test_checkout_started_invented_no_external(growth_client, app, monkeypatch):
    with app.app_context():
        _auth_user(monkeypatch, email="cs-157a-inv@test.com")
        before = FunnelEvent.query.filter_by(event_name=FUNNEL_EVENT_CHECKOUT_STARTED).count()
        resp = growth_client.post(
            "/api/growth/checkout-started",
            json={
                "event_id": "cs-157a-inv",
                "plan": "starter",
                "checkout_session_id": "cs_invented_157a",
            },
        )
        assert resp.status_code == 200
        assert resp.get_json() == {"ok": True}
        assert "external_event" not in resp.get_json()
        assert (
            FunnelEvent.query.filter_by(event_name=FUNNEL_EVENT_CHECKOUT_STARTED).count()
            == before
        )


def test_checkout_started_fail_no_external(growth_client, app, monkeypatch):
    with app.app_context():
        user, _ = _auth_user(monkeypatch, email="cs-157a-fail@test.com")
        _seed_checkout_fato(
            conta_id=user.conta_id,
            session_id="cs_fail_157a",
            plan="starter",
            franquia_id=user.franquia_id,
        )
        monkeypatch.setattr("app.growth_routes.try_record_funnel_event", lambda **_k: None)
        resp = growth_client.post(
            "/api/growth/checkout-started",
            json={
                "event_id": "cs-157a-fail",
                "plan": "starter",
                "checkout_session_id": "cs_fail_157a",
            },
        )
        assert resp.status_code == 200
        assert resp.get_json() == {"ok": True}


def test_checkout_started_replay_no_external(growth_client, app, monkeypatch):
    with app.app_context():
        user, _ = _auth_user(monkeypatch, email="cs-157a-replay@test.com")
        payload = {
            "event_id": "cs-157a-replay",
            "plan": "starter",
            "checkout_session_id": "cs_replay_157a",
        }
        _seed_checkout_fato(
            conta_id=user.conta_id,
            session_id="cs_replay_157a",
            plan="starter",
            franquia_id=user.franquia_id,
        )
        first = growth_client.post("/api/growth/checkout-started", json=payload).get_json()
        second = growth_client.post("/api/growth/checkout-started", json=payload).get_json()
        assert first.get("external_event")
        assert "external_event" not in second


def test_initiate_checkout_legacy_removed_from_contrate():
    src = CONTRATE.read_text(encoding="utf-8")
    assert 'LogCompletaPixel.track("InitiateCheckout")' not in src
    assert "AFExternalTracking.dispatch" in src
    assert "external_event" in src


# --- PAGE ALLOWLIST ----------------------------------------------------------


@pytest.mark.parametrize(
    "bad_page",
    [
        "11999999999",
        "user@example.com",
        "https://evil.example/path?q=1",
        "/fretes?x=1",
        "arbitrary_free_text",
        "acesso_desktop",
        "home",
    ],
)
def test_page_view_rejects_non_allowlisted(growth_client, app, bad_page):
    with app.app_context():
        before = FunnelEvent.query.filter_by(event_name=FUNNEL_EVENT_PAGE_VIEW).count()
        resp = growth_client.post(
            "/api/growth/page-view",
            json={"event_id": f"pv-bad-{abs(hash(bad_page)) % 10_000}", "page": bad_page},
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "page_invalid"
        assert (
            FunnelEvent.query.filter_by(event_name=FUNNEL_EVENT_PAGE_VIEW).count() == before
        )


def test_page_view_allowlist_matches_backend_constant():
    assert "index" in ALLOWED_GROWTH_PAGE_VIEW_PAGES
    assert "detalhe_noticia" in ALLOWED_GROWTH_PAGE_VIEW_PAGES
    assert "user.contrate_plano" in ALLOWED_GROWTH_PAGE_VIEW_PAGES
    assert "acesso_desktop" not in ALLOWED_GROWTH_PAGE_VIEW_PAGES
    js = EXTERNAL_JS.read_text(encoding="utf-8")
    for page in ALLOWED_GROWTH_PAGE_VIEW_PAGES:
        assert page in js
    assert "acesso_desktop" not in js.split("PAGE_ALLOW")[1].split("};")[0]


# --- PURCHASE / LEAD ---------------------------------------------------------


def test_purchase_browser_legacy_removed():
    user_area = USER_AREA.read_text(encoding="utf-8")
    events = PIXEL_EVENTS.read_text(encoding="utf-8")
    assert "pixel_subscribe_event" not in user_area
    assert "Purchase" not in events
    assert "trackEventOnceBySessionId" not in events
    assert 'storageKey = "fb_pixel_"' not in events


def test_lead_removed_from_complete_profile():
    web_src = WEB_PY.read_text(encoding="utf-8")
    events = PIXEL_EVENTS.read_text(encoding="utf-8")
    assert "pixel_event_lead_once" not in web_src
    assert 'trackEvent("Lead")' not in events
    assert "SESSION_PIXEL_EVENT_LEAD" not in (
        ROOT / "app/privacy_marketing.py"
    ).read_text(encoding="utf-8")


# --- TOKEN -------------------------------------------------------------------


def test_token_stable_and_opaque(app, ctx):
    with app.app_context():
        app.config["SECRET_KEY"] = "test-secret-157a"
        conta, franquia = seed_conta_franquia_cliente(slug="157a-token")
        user = seed_usuario(franquia.id, conta.id, email="token-157a@test.com")
        result = try_record_signup_completed(user, signup_method=SIGNUP_METHOD_GOOGLE)
        event = result["event"]
        t1 = build_external_event_token(
            event_name=event.event_name, funnel_event_id=event.id
        )
        t2 = build_external_event_token(
            event_name=event.event_name, funnel_event_id=event.id
        )
        assert t1 == t2
        assert re.fullmatch(r"v1_[0-9a-f]{32}", t1)
        assert t1 != str(event.id)
        assert event.idempotency_key not in t1
        assert "growth:signup_completed" not in t1

        from app.funnel_event_service import FUNNEL_SOURCE_GROWTH, try_record_funnel_event

        r2 = try_record_funnel_event(
            event_name=FUNNEL_EVENT_PAGE_VIEW,
            source=FUNNEL_SOURCE_GROWTH,
            user_id=None,
            conta_id=None,
            franquia_id=None,
            idempotency_key="token-diff-pv-157a",
            metadata_json={"page": "index"},
        )
        t3 = build_external_event_token(
            event_name=r2["event"].event_name, funnel_event_id=r2["event"].id
        )
        assert t3 != t1


def test_env_example_documents_secrets():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "GROWTH_EXTERNAL_EVENT_TOKEN_SECRET" in text
    assert "GOOGLE_ANALYTICS_MEASUREMENT_ID" in text
    assert "FACEBOOK_PIXEL_ID" in text
    assert "fallback" in text.lower() or "SECRET_KEY" in text


# --- ESCOPO ------------------------------------------------------------------


def test_audit_legacy_meta_signals_removed_from_pixel_helper():
    from app.funnel_event_service import is_meta_pixel_allowed

    events = PIXEL_EVENTS.read_text(encoding="utf-8")
    assert 'trackCustomEvent("AuditStarted"' not in events
    assert 'trackCustomEvent("FirstAuditCompleted"' not in events
    assert "trackFunnelEvent" not in events
    assert META_PIXEL_ALLOWED_EVENTS == set()
    assert is_meta_pixel_allowed("file_uploaded") is False
    assert is_meta_pixel_allowed("freight_calculated") is False


def test_acesso_desktop_not_instrumented():
    src = ACESSO_DESKTOP.read_text(encoding="utf-8")
    assert "pixel_base" not in src
    assert "google_analytics" not in src
    assert "fbq" not in src
    assert "gtag" not in src
    assert "AFExternalTracking" not in src
    assert "base.html" not in src


def test_no_capi_gtm_measurement_protocol():
    for path in (
        WEB_PY,
        ROOT / "app/growth_routes.py",
        ROOT / "app/services/growth_external_event_service.py",
        EXTERNAL_JS,
        GA_BASE,
    ):
        text = path.read_text(encoding="utf-8")
        assert "graph.facebook.com" not in text
        assert "/mp/collect" not in text
        assert "GTM-" not in text
        assert "Measurement Protocol" not in text
        assert "Enhanced Conversions" not in text
        assert "advanced matching" not in text.lower()
