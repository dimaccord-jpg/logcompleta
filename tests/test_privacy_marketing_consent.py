"""Consentimento first-party de marketing (Meta Pixel e OpenAI Ads Measurement)."""
from __future__ import annotations

import importlib
import os
import pathlib
from types import SimpleNamespace

import pytest

from app.privacy_marketing import (
    PRIVACY_MARKETING_COOKIE_MAX_AGE_SECONDS,
    PRIVACY_MARKETING_COOKIE_NAME,
    parse_privacy_marketing_cookie,
)


CONSENT_ENDPOINT = "/api/privacy/marketing-consent"
META_PIXEL_ID = "meta_pixel_test"
OPENAI_PIXEL_ID = "px_test_openai_ads"
META_MARKERS = (
    "connect.facebook.net",
    "fbevents.js",
    "fbq('init'",
    "PageView",
)
OPENAI_MARKERS = (
    "bzrcdn.openai.com",
    "oaiq.min.js",
    'oaiq("init"',
)
BANNER_COPY = (
    "Utilizamos tecnologias necessárias para o funcionamento do AgenteFrete"
)
PRIVACY_JS = pathlib.Path("app/static/js/privacy_consent.js")
PIXEL_EVENTS = pathlib.Path("app/templates/partials/pixel_events.html")


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


@pytest.fixture
def web(monkeypatch):
    module = _load_web_module()
    monkeypatch.setattr(module, "current_user", SimpleNamespace(is_authenticated=False))
    monkeypatch.setattr(module, "get_julia_chat_max_history", lambda: 10)
    monkeypatch.setattr(
        module,
        "avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True},
    )
    monkeypatch.setattr(module, "get_active_term", lambda: None)
    original = {
        "OPENAI_ADS_PIXEL_ID": module.app.config.get("OPENAI_ADS_PIXEL_ID"),
        "OPENAI_ADS_DEBUG": module.app.config.get("OPENAI_ADS_DEBUG"),
        "FACEBOOK_PIXEL_ID": module.app.config.get("FACEBOOK_PIXEL_ID"),
    }
    module.app.config["OPENAI_ADS_PIXEL_ID"] = ""
    module.app.config["OPENAI_ADS_DEBUG"] = False
    module.app.config["FACEBOOK_PIXEL_ID"] = ""
    yield module
    for key, value in original.items():
        if value is None:
            module.app.config.pop(key, None)
        else:
            module.app.config[key] = value


def _client(web):
    return web.app.test_client()


def _set_privacy_cookie(client, value: str) -> None:
    try:
        client.set_cookie(PRIVACY_MARKETING_COOKIE_NAME, value)
    except TypeError:
        client.set_cookie("localhost", PRIVACY_MARKETING_COOKIE_NAME, value)


def _enable_pixels(web) -> None:
    web.app.config["FACEBOOK_PIXEL_ID"] = META_PIXEL_ID
    web.app.config["OPENAI_ADS_PIXEL_ID"] = OPENAI_PIXEL_ID


def _cookie_header(resp, name: str) -> str:
    headers = resp.headers.getlist("Set-Cookie")
    matches = [header for header in headers if header.startswith(f"{name}=")]
    assert matches, f"cookie {name} ausente em Set-Cookie: {headers}"
    return matches[0]


def _assert_marketing_blocked(html: str) -> None:
    for marker in META_MARKERS + OPENAI_MARKERS:
        assert marker not in html
    assert "window.LogCompletaPixel" not in html
    assert "trackEventOnceBySessionId" not in html
    assert 'storageKey = "fb_pixel_"' not in html


def _assert_marketing_present(html: str) -> None:
    for marker in META_MARKERS + OPENAI_MARKERS:
        assert marker in html


def test_parse_privacy_marketing_cookie_unknown_and_invalid():
    assert parse_privacy_marketing_cookie(None) == "unknown"
    assert parse_privacy_marketing_cookie("") == "unknown"
    assert parse_privacy_marketing_cookie("accepted") == "unknown"
    assert parse_privacy_marketing_cookie("v1:maybe") == "unknown"
    assert parse_privacy_marketing_cookie("v2:accepted") == "unknown"
    assert parse_privacy_marketing_cookie("v1:ACCEPTED") == "unknown"
    assert parse_privacy_marketing_cookie("v1:accepted") == "accepted"
    assert parse_privacy_marketing_cookie("v1:rejected") == "rejected"


def test_privacy_cookie_ttl_is_independent_from_flask_session(web):
    assert web.settings.privacy_marketing_cookie_name == PRIVACY_MARKETING_COOKIE_NAME
    assert (
        web.settings.privacy_marketing_cookie_max_age_seconds
        == PRIVACY_MARKETING_COOKIE_MAX_AGE_SECONDS
    )
    assert web.settings.privacy_marketing_cookie_max_age_seconds == 180 * 24 * 3600
    assert (
        web.settings.privacy_marketing_cookie_max_age_seconds
        != web.settings.session_lifetime_seconds
    )


def test_unknown_without_cookie_blocks_marketing_and_shows_banner(web):
    _enable_pixels(web)
    resp = _client(web).get("/")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    _assert_marketing_blocked(html)
    assert BANNER_COPY in html
    assert 'id="af-privacy-banner"' in html
    assert "Preferências de privacidade" in html
    assert "Aceitar" in html
    assert "Rejeitar não necessários" in html
    assert "Gerenciar preferências" in html


def test_invalid_cookie_treated_as_unknown_and_blocks_marketing(web):
    _enable_pixels(web)
    client = _client(web)
    _set_privacy_cookie(client, "garbage")
    html = client.get("/").get_data(as_text=True)
    _assert_marketing_blocked(html)
    assert 'id="af-privacy-banner"' in html


def test_endpoint_accept_sets_cookie_and_next_get_renders_sdks(web):
    _enable_pixels(web)
    client = _client(web)
    resp = client.post(CONSENT_ENDPOINT, json={"decision": "accepted"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body == {"ok": True, "decision": "accepted"}

    header = _cookie_header(resp, PRIVACY_MARKETING_COOKIE_NAME)
    header_l = header.lower()
    assert "v1:accepted" in header
    assert "path=/" in header_l
    assert "samesite=lax" in header_l
    assert f"max-age={PRIVACY_MARKETING_COOKIE_MAX_AGE_SECONDS}" in header_l
    assert "httponly" in header_l
    assert "domain=" not in header_l
    if web.settings.session_cookie_secure:
        assert "secure" in header_l
    else:
        assert "secure" not in header_l

    html = client.get("/").get_data(as_text=True)
    _assert_marketing_present(html)
    assert 'id="af-privacy-banner"' not in html
    assert "Preferências de privacidade" in html


def test_endpoint_reject_sets_cookie_and_keeps_marketing_blocked(web):
    _enable_pixels(web)
    client = _client(web)
    resp = client.post(CONSENT_ENDPOINT, json={"decision": "rejected"})
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "decision": "rejected"}
    header = _cookie_header(resp, PRIVACY_MARKETING_COOKIE_NAME)
    assert "v1:rejected" in header

    html = client.get("/").get_data(as_text=True)
    _assert_marketing_blocked(html)
    assert 'id="af-privacy-banner"' not in html
    assert resp.status_code == 200
    home = client.get("/")
    assert home.status_code == 200


def test_endpoint_invalid_decision_returns_400(web):
    resp = _client(web).post(CONSENT_ENDPOINT, json={"decision": "maybe"})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_endpoint_rejects_json_with_extra_keys(web):
    client = _client(web)
    accepted_extra = client.post(
        CONSENT_ENDPOINT,
        json={"decision": "accepted", "extra": "x"},
    )
    assert accepted_extra.status_code == 400
    assert accepted_extra.get_json()["ok"] is False

    rejected_extra = client.post(
        CONSENT_ENDPOINT,
        json={"decision": "rejected", "extra": "x"},
    )
    assert rejected_extra.status_code == 400
    assert rejected_extra.get_json()["ok"] is False

    empty = client.post(CONSENT_ENDPOINT, json={})
    assert empty.status_code == 400
    assert empty.get_json()["ok"] is False


def test_endpoint_requires_json_not_form_or_query(web):
    client = _client(web)
    form_resp = client.post(CONSENT_ENDPOINT, data={"decision": "accepted"})
    assert form_resp.status_code == 400

    query_resp = client.post(f"{CONSENT_ENDPOINT}?decision=accepted")
    assert query_resp.status_code == 400

    empty_json = client.post(
        CONSENT_ENDPOINT,
        data="not-json",
        content_type="application/json",
    )
    assert empty_json.status_code == 400


def test_get_does_not_change_consent(web):
    resp = _client(web).get(CONSENT_ENDPOINT)
    assert resp.status_code == 405
    assert not any(
        header.startswith(f"{PRIVACY_MARKETING_COOKIE_NAME}=")
        for header in resp.headers.getlist("Set-Cookie")
    )


def test_can_change_from_accepted_to_rejected_and_back(web):
    _enable_pixels(web)
    client = _client(web)

    accepted = client.post(CONSENT_ENDPOINT, json={"decision": "accepted"})
    assert accepted.status_code == 200
    html_accepted = client.get("/").get_data(as_text=True)
    _assert_marketing_present(html_accepted)

    rejected = client.post(CONSENT_ENDPOINT, json={"decision": "rejected"})
    assert rejected.status_code == 200
    html_rejected = client.get("/").get_data(as_text=True)
    _assert_marketing_blocked(html_rejected)

    accepted_again = client.post(CONSENT_ENDPOINT, json={"decision": "accepted"})
    assert accepted_again.status_code == 200
    html_again = client.get("/").get_data(as_text=True)
    _assert_marketing_present(html_again)


def test_rejected_cookie_does_not_block_onboarding_session(web, monkeypatch):
    monkeypatch.setattr(
        "app.run_cleiton_discovery.cleiton_discovery_reply",
        lambda *a, **k: {
            "reply": "ok",
            "discovery": {"next_action": "converse", "confidence": "low"},
            "handoff": None,
        },
    )
    client = _client(web)
    _set_privacy_cookie(client, "v1:rejected")
    resp = client.post("/api/onboarding_discovery", json={"message": "oi", "history": []})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body.get("anonymous_interaction_count") == 1
    assert body.get("limit_reached") is False

    set_cookie_headers = resp.headers.getlist("Set-Cookie")
    assert any(header.lower().startswith("session=") for header in set_cookie_headers) or any(
        "session=" in header.lower() for header in set_cookie_headers
    )

    with client.session_transaction() as sess:
        assert sess.get("onboarding_discovery_anon_id")
        assert sess.get("onboarding_discovery_count") == 1


def test_registration_flag_stays_pending_while_unknown(web):
    _enable_pixels(web)
    client = _client(web)
    with client.session_transaction() as sess:
        sess["pixel_event_complete_registration_once"] = True
    html = client.get("/login").get_data(as_text=True)
    assert "CompleteRegistration" not in html
    assert "registration_completed" not in html
    with client.session_transaction() as sess:
        assert sess.get("pixel_event_complete_registration_once") is True


def test_registration_flag_fires_once_after_accepted(web):
    _enable_pixels(web)
    client = _client(web)
    with client.session_transaction() as sess:
        sess["pixel_event_complete_registration_once"] = True
    client.post(CONSENT_ENDPOINT, json={"decision": "accepted"})
    html = client.get("/login").get_data(as_text=True)
    assert 'trackEvent("CompleteRegistration")' in html
    assert "const completeRegistrationEnabled = true" in html
    assert 'window.oaiq("measure", "registration_completed"' in html
    with client.session_transaction() as sess:
        assert sess.get("pixel_event_complete_registration_once") is not True


def test_rejected_discards_pending_registration_flag(web):
    _enable_pixels(web)
    client = _client(web)
    with client.session_transaction() as sess:
        sess["pixel_event_complete_registration_once"] = True
    resp = client.post(CONSENT_ENDPOINT, json={"decision": "rejected"})
    assert resp.status_code == 200
    with client.session_transaction() as sess:
        assert "pixel_event_complete_registration_once" not in sess
    html = client.get("/login").get_data(as_text=True)
    assert "CompleteRegistration" not in html
    assert "registration_completed" not in html


def test_lead_flag_stays_pending_while_unknown_then_fires_when_accepted(web):
    _enable_pixels(web)
    client = _client(web)
    with client.session_transaction() as sess:
        sess["pixel_event_lead_once"] = True
    html_unknown = client.get("/login").get_data(as_text=True)
    assert 'trackEvent("Lead")' not in html_unknown
    with client.session_transaction() as sess:
        assert sess.get("pixel_event_lead_once") is True

    client.post(CONSENT_ENDPOINT, json={"decision": "accepted"})
    html_accepted = client.get("/login").get_data(as_text=True)
    assert 'trackEvent("Lead")' in html_accepted
    assert "const leadEnabled = true" in html_accepted
    with client.session_transaction() as sess:
        assert sess.get("pixel_event_lead_once") is not True


def test_rejected_discards_pending_lead_flag(web):
    _enable_pixels(web)
    client = _client(web)
    with client.session_transaction() as sess:
        sess["pixel_event_lead_once"] = True
    client.post(CONSENT_ENDPOINT, json={"decision": "rejected"})
    with client.session_transaction() as sess:
        assert "pixel_event_lead_once" not in sess
    html = client.get("/login").get_data(as_text=True)
    assert 'trackEvent("Lead")' not in html


def test_html_without_consent_does_not_include_meta_dedupe_logic(web):
    _enable_pixels(web)
    html = _client(web).get("/").get_data(as_text=True)
    pixel_events = PIXEL_EVENTS.read_text(encoding="utf-8")
    assert 'storageKey = "fb_pixel_"' in pixel_events
    assert 'storageKey = "fb_pixel_"' not in html
    assert "window.LogCompletaPixel" not in html
    assert "trackEventOnceBySessionId" not in html


def test_privacy_js_cleans_own_fb_pixel_storage_only():
    source = PRIVACY_JS.read_text(encoding="utf-8")
    assert 'key.indexOf("fb_pixel_") === 0' in source
    assert "removeItem" in source
    assert "try {" in source
    assert "setItem" not in source
    assert "location.reload()" in source
    assert 'credentials: "same-origin"' in source


def test_accept_and_reject_buttons_have_equivalent_visual_weight():
    template = pathlib.Path("app/templates/partials/privacy_consent.html").read_text(
        encoding="utf-8"
    )
    assert template.count('class="af-privacy-btn" data-af-privacy-decision="accepted"') >= 1
    assert template.count('class="af-privacy-btn" data-af-privacy-decision="rejected"') >= 1
    assert 'class="af-privacy-btn" data-af-privacy-decision="accepted"' in template
    assert 'class="af-privacy-btn" data-af-privacy-decision="rejected"' in template
