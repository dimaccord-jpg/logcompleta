"""Testes direcionados do Lote 159C SCRUM-159: click IDs accepted-only."""
from __future__ import annotations

import importlib
import logging
import os

import pytest

from app.funnel_event_service import (
    FUNNEL_EVENT_CHECKOUT_STARTED,
    FUNNEL_EVENT_CTA_CLICKED,
    FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
    FUNNEL_EVENT_PAGE_VIEW,
    FUNNEL_EVENT_PAID,
    FUNNEL_EVENT_PLAN_SELECTED,
    FUNNEL_EVENT_SESSION_ORIGIN_OBSERVED,
    FUNNEL_EVENT_SIGNUP_COMPLETED,
    FUNNEL_EVENT_TASK_COMPLETED,
    FUNNEL_SOURCE_GROWTH,
    GROWTH_ATTRIBUTION_ALLOWED_EVENTS,
    META_PIXEL_ALLOWED_EVENTS,
    is_meta_pixel_allowed,
    record_funnel_event,
)
from app.models import FunnelEvent
from app.privacy_marketing import (
    PRIVACY_MARKETING_COOKIE_NAME,
    PRIVACY_MARKETING_STATE_REJECTED,
)
from app.services.growth_attribution_service import (
    ATTRIBUTION_VERSION,
    CLICK_ID_PARAMS,
    SESSION_KEY,
    apply_growth_attribution_before_request,
    purge_click_ids_from_session_attribution,
    sanitize_growth_attribution_snapshot,
    sanitize_next_url_for_consent,
    sanitize_post_login_next_in_session,
    snapshot_growth_attribution_for_persist,
    strip_click_ids_from_relative_url,
    validate_click_id,
)
from tests.conftest import seed_conta_franquia_cliente, seed_usuario

CONSENT_ENDPOINT = "/api/privacy/marketing-consent"
META_PIXEL_BASELINE = frozenset()


def _set_privacy_cookie(client, decision: str) -> None:
    value = f"v1:{decision}"
    try:
        client.set_cookie(PRIVACY_MARKETING_COOKIE_NAME, value)
    except TypeError:
        client.set_cookie("localhost", PRIVACY_MARKETING_COOKIE_NAME, value)


def _load_web():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


@pytest.fixture
def attr_app(app):
    app.config["SECRET_KEY"] = "test-secret-159c"
    app.config["TESTING"] = True
    app.config["PRIVACY_MARKETING_COOKIE_NAME"] = PRIVACY_MARKETING_COOKIE_NAME

    @app.route("/")
    def index():
        return "ok"

    @app.route("/login")
    def login():
        return "login"

    @app.route("/fretes")
    def fretes():
        return "fretes"

    @app.before_request
    def _attr():
        apply_growth_attribution_before_request()

    return app


@pytest.fixture
def attr_client(attr_app):
    return attr_app.test_client()


# --- CAPTURA ---


def test_accepted_utm_and_gclid_both_captured(attr_client):
    _set_privacy_cookie(attr_client, "accepted")
    attr_client.get("/?utm_source=google&gclid=AbC_123")
    with attr_client.session_transaction() as sess:
        origin = sess[SESSION_KEY]["current_session_origin"]
        assert origin["source"] == "google"
        assert origin["gclid"] == "AbC_123"
        assert "medium" not in origin


def test_unknown_utm_captured_gclid_ignored(attr_client):
    attr_client.get("/?utm_source=google&gclid=SHOULD_IGNORE")
    with attr_client.session_transaction() as sess:
        origin = sess[SESSION_KEY]["current_session_origin"]
        assert origin["source"] == "google"
        assert "gclid" not in origin
        assert "SHOULD_IGNORE" not in str(origin)


def test_rejected_utm_captured_gclid_ignored(attr_client):
    _set_privacy_cookie(attr_client, "rejected")
    attr_client.get("/?utm_source=linkedin&gclid=NOPE")
    with attr_client.session_transaction() as sess:
        origin = sess[SESSION_KEY]["current_session_origin"]
        assert origin["source"] == "linkedin"
        assert "gclid" not in origin


def test_accepted_gclid_only_no_source_inference(attr_client):
    _set_privacy_cookie(attr_client, "accepted")
    attr_client.get("/?gclid=OnlyClick")
    with attr_client.session_transaction() as sess:
        origin = sess[SESSION_KEY]["current_session_origin"]
        assert origin == {"gclid": "OnlyClick", "landing_page": "index"}
        assert "source" not in origin
        assert "medium" not in origin
        assert "campaign" not in origin


def test_unknown_gclid_only_creates_no_context(attr_client):
    attr_client.get("/?gclid=Alone")
    with attr_client.session_transaction() as sess:
        assert SESSION_KEY not in sess


@pytest.mark.parametrize("param", sorted(CLICK_ID_PARAMS))
def test_each_click_id_works_under_accepted(attr_client, param):
    _set_privacy_cookie(attr_client, "accepted")
    value = f"Val_{param}_XyZ"
    attr_client.get(f"/?{param}={value}")
    with attr_client.session_transaction() as sess:
        origin = sess[SESSION_KEY]["current_session_origin"]
        assert origin[param] == value
        assert set(origin.keys()) <= {param, "landing_page"}


# --- VALIDACAO ---


def test_click_id_over_512_discarded():
    assert validate_click_id("x" * 513) is None
    assert validate_click_id("x" * 512) == "x" * 512


def test_click_id_control_chars_discarded_before_strip():
    assert validate_click_id("\x00abc") is None
    assert validate_click_id("abc\x1f") is None
    assert validate_click_id("  ok  ") == "ok"


def test_click_id_at_sign_discarded():
    assert validate_click_id("user@meta") is None


def test_click_id_url_discarded():
    assert validate_click_id("https://evil.example/x") is None
    assert validate_click_id("//evil.example") is None


@pytest.mark.parametrize(
    "payload",
    [
        "data:text/plain,ABC",
        "javascript:alert(1)",
        "JAVASCRIPT:foo",
        "file:/tmp/test",
        "blob:abc",
        "custom-scheme:value",
        "https://example.com",
        "http://example.com",
    ],
)
def test_click_id_uri_scheme_rejected(payload):
    assert validate_click_id(payload) is None


@pytest.mark.parametrize(
    "payload",
    [
        "ABC123_xyz-987",
        "EAIaIQobChMI-test_value",
        "abc.def-ghi_123",
    ],
)
def test_click_id_legitimate_values_accepted(payload):
    assert validate_click_id(payload) == payload


def test_accepted_capture_does_not_persist_uri_scheme(attr_client):
    _set_privacy_cookie(attr_client, "accepted")
    attr_client.get(
        "/?utm_source=google"
        "&gclid=data:text/plain,ABC"
        "&gbraid=javascript:alert(1)"
        "&wbraid=custom-scheme:value"
        "&fbclid=ABC123_xyz-987"
    )
    with attr_client.session_transaction() as sess:
        origin = sess[SESSION_KEY]["current_session_origin"]
        assert origin["source"] == "google"
        assert origin["fbclid"] == "ABC123_xyz-987"
        assert "gclid" not in origin
        assert "gbraid" not in origin
        assert "wbraid" not in origin
        assert "data:" not in str(origin).lower()
        assert "javascript:" not in str(origin).lower()


def test_snapshot_does_not_persist_uri_scheme_click_ids():
    stripped = sanitize_growth_attribution_snapshot(
        {
            "current_session_origin": {
                "source": "google",
                "gclid": "javascript:alert(1)",
            }
        }
    )
    assert stripped is not None
    assert stripped["current_session_origin"]["source"] == "google"
    assert "gclid" not in stripped["current_session_origin"]

    only_bad = sanitize_growth_attribution_snapshot(
        {"current_session_origin": {"gclid": "data:text/plain,ABC"}}
    )
    assert only_bad is None

    ok = sanitize_growth_attribution_snapshot(
        {
            "current_session_origin": {
                "source": "google",
                "gclid": "EAIaIQobChMI-test_value",
            }
        }
    )
    assert ok is not None
    assert ok["current_session_origin"]["gclid"] == "EAIaIQobChMI-test_value"


def test_click_id_html_and_payload_discarded():
    assert validate_click_id("<script>x</script>") is None
    assert validate_click_id('{"a":1}') is None
    assert validate_click_id("a=1&b=2") is None


def test_click_id_non_string_discarded():
    assert validate_click_id(12345) is None
    assert validate_click_id({"gclid": "x"}) is None
    assert validate_click_id(["x"]) is None


def test_repeated_click_id_param_discarded(attr_client):
    _set_privacy_cookie(attr_client, "accepted")
    attr_client.get("/?gclid=A&gclid=B&utm_source=google")
    with attr_client.session_transaction() as sess:
        origin = sess[SESSION_KEY]["current_session_origin"]
        assert origin["source"] == "google"
        assert "gclid" not in origin


def test_valid_click_id_preserves_casing():
    assert validate_click_id("AbC_deF-99") == "AbC_deF-99"


# --- FIRST / CURRENT ---


def test_first_touch_unknown_utm_not_enriched_after_accept(attr_client):
    attr_client.get("/?utm_source=google&gclid=AAA")
    with attr_client.session_transaction() as sess:
        first = dict(sess[SESSION_KEY]["first_touch"])
        assert first["source"] == "google"
        assert "gclid" not in first

    _set_privacy_cookie(attr_client, "accepted")
    attr_client.get("/?utm_source=google&gclid=AAA")
    with attr_client.session_transaction() as sess:
        ctx = sess[SESSION_KEY]
        assert ctx["first_touch"] == first
        assert "gclid" not in ctx["first_touch"]
        assert ctx["current_session_origin"]["gclid"] == "AAA"
        assert ctx["current_session_origin"]["source"] == "google"


def test_reload_accepted_updates_current_with_id(attr_client):
    attr_client.get("/?utm_source=google&gclid=AAA")
    _set_privacy_cookie(attr_client, "accepted")
    attr_client.get("/?utm_source=google&gclid=AAA")
    with attr_client.session_transaction() as sess:
        current = sess[SESSION_KEY]["current_session_origin"]
        assert current["gclid"] == "AAA"
        assert current["source"] == "google"


def test_origin_b_without_id_replaces_origin_a_with_id(attr_client):
    _set_privacy_cookie(attr_client, "accepted")
    attr_client.get("/?utm_source=google&gclid=AAA")
    attr_client.get("/?utm_source=linkedin")
    with attr_client.session_transaction() as sess:
        current = sess[SESSION_KEY]["current_session_origin"]
        assert current["source"] == "linkedin"
        assert "gclid" not in current
        assert sess[SESSION_KEY]["first_touch"]["source"] == "google"
        assert sess[SESSION_KEY]["first_touch"]["gclid"] == "AAA"


# --- REJEICAO ---


def test_reject_removes_gclid_keeps_utms(attr_app):
    web = _load_web()
    client = web.app.test_client()
    _set_privacy_cookie(client, "accepted")
    with client.session_transaction() as sess:
        sess[SESSION_KEY] = {
            "version": ATTRIBUTION_VERSION,
            "first_touch": {
                "source": "google",
                "gclid": "KEEP_UTM_DROP_ID",
                "landing_page": "index",
            },
            "current_session_origin": {
                "source": "google",
                "campaign": "c1",
                "gclid": "KEEP_UTM_DROP_ID",
                "landing_page": "index",
            },
        }
        sess["post_login_next"] = "/fretes?mode=1&gclid=XYZ&utm_source=google"

    resp = client.post(CONSENT_ENDPOINT, json={"decision": "rejected"})
    assert resp.status_code == 200
    with client.session_transaction() as sess:
        ctx = sess[SESSION_KEY]
        assert ctx["first_touch"]["source"] == "google"
        assert "gclid" not in ctx["first_touch"]
        assert ctx["current_session_origin"]["source"] == "google"
        assert ctx["current_session_origin"]["campaign"] == "c1"
        assert "gclid" not in ctx["current_session_origin"]
        assert "gclid" not in sess["post_login_next"]
        assert "utm_source=google" in sess["post_login_next"]
        assert sess["post_login_next"].startswith("/fretes?")


def test_reject_removes_landing_only_origin():
    from flask import Flask, session as flask_session

    app = Flask(__name__)
    app.secret_key = "t"
    with app.test_request_context("/"):
        flask_session[SESSION_KEY] = {
            "version": ATTRIBUTION_VERSION,
            "first_touch": {"gclid": "ONLY", "landing_page": "index"},
            "current_session_origin": {"gclid": "ONLY", "landing_page": "index"},
        }
        purge_click_ids_from_session_attribution()
        assert SESSION_KEY not in flask_session


def test_reject_does_not_mutate_historical_funnel_events(app, ctx):
    conta, franquia = seed_conta_franquia_cliente(slug="159c-hist")
    user = seed_usuario(franquia.id, conta.id, email="159c-hist@test.com")
    record_funnel_event(
        event_name=FUNNEL_EVENT_SESSION_ORIGIN_OBSERVED,
        source=FUNNEL_SOURCE_GROWTH,
        user_id=user.id,
        idempotency_key="growth:session_origin_observed:159c:hist",
        metadata_json={
            "growth_attribution": {
                "current_session_origin": {
                    "source": "google",
                    "gclid": "HISTORIC_ID",
                }
            }
        },
    )
    event = FunnelEvent.query.filter_by(
        idempotency_key="growth:session_origin_observed:159c:hist"
    ).one()
    assert event.metadata_json["growth_attribution"]["current_session_origin"]["gclid"] == (
        "HISTORIC_ID"
    )

    web = _load_web()
    client = web.app.test_client()
    with client.session_transaction() as sess:
        sess[SESSION_KEY] = {
            "version": ATTRIBUTION_VERSION,
            "first_touch": {"source": "google", "gclid": "SESSION_ID"},
            "current_session_origin": {"source": "google", "gclid": "SESSION_ID"},
        }
    client.post(CONSENT_ENDPOINT, json={"decision": "rejected"})

    refreshed = FunnelEvent.query.filter_by(
        idempotency_key="growth:session_origin_observed:159c:hist"
    ).one()
    assert refreshed.metadata_json["growth_attribution"]["current_session_origin"][
        "gclid"
    ] == "HISTORIC_ID"


# --- SNAPSHOT ---


def test_snapshot_accepted_may_include_click_id(attr_app):
    client = attr_app.test_client()
    _set_privacy_cookie(client, "accepted")
    with attr_app.test_request_context(
        "/?utm_source=google&gclid=SNAP1",
        headers={"Cookie": f"{PRIVACY_MARKETING_COOKIE_NAME}=v1:accepted"},
    ):
        # Cookie via environ for request context
        pass

    with attr_app.test_request_context("/"):
        from flask import session as flask_session

        flask_session[SESSION_KEY] = {
            "version": ATTRIBUTION_VERSION,
            "first_touch": {"source": "google", "gclid": "SNAP1"},
            "current_session_origin": {"source": "google", "gclid": "SNAP1"},
        }
        # Sem cookie accepted neste contexto → snapshot omite (defesa).
        snap_unknown = snapshot_growth_attribution_for_persist()
        assert snap_unknown is not None
        assert "gclid" not in snap_unknown["current_session_origin"]

    with attr_app.test_request_context(
        "/",
        environ_base={"HTTP_COOKIE": f"{PRIVACY_MARKETING_COOKIE_NAME}=v1:accepted"},
    ):
        from flask import session as flask_session

        flask_session[SESSION_KEY] = {
            "version": ATTRIBUTION_VERSION,
            "first_touch": {"source": "google", "gclid": "SNAP1"},
            "current_session_origin": {"source": "google", "gclid": "SNAP1"},
        }
        snap = snapshot_growth_attribution_for_persist()
        assert snap["current_session_origin"]["gclid"] == "SNAP1"
        assert snap["first_touch"]["gclid"] == "SNAP1"


def test_snapshot_unknown_omits_residual_ids(attr_app):
    with attr_app.test_request_context("/"):
        from flask import session as flask_session

        flask_session[SESSION_KEY] = {
            "version": ATTRIBUTION_VERSION,
            "first_touch": {"source": "google", "gclid": "RESIDUAL"},
            "current_session_origin": {"source": "google", "gclid": "RESIDUAL"},
        }
        snap = snapshot_growth_attribution_for_persist()
        assert snap is not None
        assert "gclid" not in snap["first_touch"]
        assert "gclid" not in snap["current_session_origin"]
        assert snap["current_session_origin"]["source"] == "google"


# --- FUNNELEVENT ---


def test_click_ids_only_inside_attribution_structure():
    ok = sanitize_growth_attribution_snapshot(
        {"current_session_origin": {"gclid": "NestedOk", "source": "google"}}
    )
    assert ok is not None
    assert ok["current_session_origin"]["gclid"] == "NestedOk"


def test_growth_attribution_only_on_five_events(app, ctx):
    assert GROWTH_ATTRIBUTION_ALLOWED_EVENTS == frozenset(
        {
            FUNNEL_EVENT_SIGNUP_COMPLETED,
            FUNNEL_EVENT_SESSION_ORIGIN_OBSERVED,
            FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
            FUNNEL_EVENT_PLAN_SELECTED,
            FUNNEL_EVENT_CHECKOUT_STARTED,
        }
    )
    conta, franquia = seed_conta_franquia_cliente(slug="159c-ga-events")
    user = seed_usuario(franquia.id, conta.id, email="159c-ga-events@test.com")
    ga = {
        "growth_attribution": {
            "current_session_origin": {"source": "google", "gclid": "X"}
        }
    }
    for blocked in (
        FUNNEL_EVENT_PAGE_VIEW,
        FUNNEL_EVENT_CTA_CLICKED,
        FUNNEL_EVENT_TASK_COMPLETED,
        FUNNEL_EVENT_PAID,
    ):
        with pytest.raises(ValueError, match="growth_attribution"):
            record_funnel_event(
                event_name=blocked,
                source=FUNNEL_SOURCE_GROWTH,
                user_id=user.id if blocked != FUNNEL_EVENT_PAGE_VIEW else None,
                conta_id=conta.id if blocked == FUNNEL_EVENT_PAID else None,
                idempotency_key=f"growth:159c:block:{blocked}",
                metadata_json=dict(ga),
            )


def test_paid_rejects_growth_attribution(app, ctx):
    conta, franquia = seed_conta_franquia_cliente(slug="159c-paid")
    with pytest.raises(ValueError, match="growth_attribution"):
        record_funnel_event(
            event_name=FUNNEL_EVENT_PAID,
            source=FUNNEL_SOURCE_GROWTH,
            conta_id=conta.id,
            idempotency_key="growth:paid:159c:1",
            metadata_json={
                "plan": "starter",
                "growth_attribution": {
                    "current_session_origin": {"gclid": "NO"}
                },
            },
        )


# --- POST_LOGIN_NEXT ---


def test_post_login_next_strips_ids_when_unknown():
    url = "/fretes?utm_source=google&gclid=A&gbraid=B&mode=op"
    cleaned = sanitize_next_url_for_consent(
        url, state="unknown"
    )
    assert "gclid" not in cleaned
    assert "gbraid" not in cleaned
    assert "utm_source=google" in cleaned
    assert "mode=op" in cleaned


def test_post_login_next_strips_ids_when_rejected():
    url = "/auditoria-frete?fbclid=Z&wbraid=W&x=1"
    cleaned = sanitize_next_url_for_consent(url, state="rejected")
    assert cleaned == "/auditoria-frete?x=1"


def test_post_login_next_preserved_when_accepted():
    url = "/fretes?gclid=KEEP&utm_source=google"
    assert sanitize_next_url_for_consent(url, state="accepted") == url


def test_reject_sanitizes_existing_post_login_next():
    from flask import Flask, session as flask_session

    app = Flask(__name__)
    app.secret_key = "t"
    with app.test_request_context("/"):
        flask_session["post_login_next"] = "/chat_julia?mode=operational&gclid=X&fbclid=Y"
        sanitize_post_login_next_in_session(state=PRIVACY_MARKETING_STATE_REJECTED)
        assert flask_session["post_login_next"] == "/chat_julia?mode=operational"


def test_login_stores_sanitized_next_when_unknown():
    web = _load_web()
    client = web.app.test_client()
    resp = client.get(
        "/login?next=%2Ffretes%3Fgclid%3DABC%26utm_source%3Dgoogle%26mode%3D1"
    )
    assert resp.status_code == 200
    with client.session_transaction() as sess:
        nxt = sess.get("post_login_next")
        assert nxt is not None
        assert "gclid" not in nxt
        assert "utm_source=google" in nxt
        assert "mode=1" in nxt


# --- REFERER ---


def test_unauthorized_referer_log_strips_query_and_fragment(caplog):
    web = _load_web()
    assert web._safe_referer_for_log(
        "https://ads.example/landing?gclid=SECRET&fbclid=X#frag"
    ) == "https://ads.example/landing"
    assert web._safe_referer_for_log("") == ""
    assert "?" not in web._safe_referer_for_log(
        "https://x.test/path?a=1#z"
    )
    assert "#" not in web._safe_referer_for_log(
        "https://x.test/path?a=1#z"
    )

    with caplog.at_level(logging.WARNING):
        client = web.app.test_client()
        client.get(
            "/fretes",
            headers={
                "Referer": "https://evil.example/p?gclid=LEAK&fbclid=L2#section"
            },
        )
    joined = " | ".join(r.message for r in caplog.records)
    assert "gclid=LEAK" not in joined
    assert "fbclid=L2" not in joined
    assert "#section" not in joined


# --- EXTERNO ---


def test_meta_pixel_allowed_events_unchanged():
    assert META_PIXEL_ALLOWED_EVENTS == META_PIXEL_BASELINE
    assert is_meta_pixel_allowed("signup_completed") is False
    assert is_meta_pixel_allowed("checkout_started") is False
    assert is_meta_pixel_allowed("file_uploaded") is False
    assert is_meta_pixel_allowed("freight_calculated") is False


def test_strip_helper_and_click_id_set():
    assert CLICK_ID_PARAMS == {"gclid", "gbraid", "wbraid", "fbclid"}
    assert strip_click_ids_from_relative_url("/x?gclid=1&a=2") == "/x?a=2"
    assert strip_click_ids_from_relative_url("/x") == "/x"
