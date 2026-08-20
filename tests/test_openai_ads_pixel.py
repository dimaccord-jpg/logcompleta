"""Cobertura do OpenAI Ads Measurement Pixel (somente browser Pixel)."""
from __future__ import annotations

import importlib
import os
import pathlib
import re
from types import SimpleNamespace

import pytest


SDK_URL = "https://bzrcdn.openai.com/sdk/oaiq.min.js"
PIXEL_ID = "px_test_openai_ads"
FORBIDDEN_CONVERSIONS = (
    "trial_started",
    "subscription_created",
    "order_created",
    "lead_created",
    "checkout_started",
    "page_viewed",
    "home_viewed",
    "landing_view",
    "ad_visit",
)
FORBIDDEN_PII_MARKERS = (
    "email_sha256",
    "external_id_sha256",
    "external_id",
    "OPENAI_ADS_CAPI",
    "bzr.openai.com/v1/events",
)

PIXEL_BASE = pathlib.Path("app/templates/partials/openai_ads_pixel.html")
PIXEL_EVENTS = pathlib.Path("app/templates/partials/openai_ads_pixel_events.html")
META_EVENTS = pathlib.Path("app/templates/partials/pixel_events.html")
WEB_PY = pathlib.Path("app/web.py")
SETTINGS_PY = pathlib.Path("app/settings.py")


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


def _openai_script_blocks(html: str) -> str:
    blocks = []
    for match in re.finditer(
        r"<script>\s*\(function \(w, d, s, u\)[\s\S]*?</script>",
        html,
    ):
        blocks.append(match.group(0))
    for match in re.finditer(
        r"<script>\s*\(\(\) => \{[\s\S]*?window\.oaiq\([\s\S]*?</script>",
        html,
    ):
        blocks.append(match.group(0))
    return "\n".join(blocks)


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
    yield module
    for key, value in original.items():
        if value is None:
            module.app.config.pop(key, None)
        else:
            module.app.config[key] = value


def _client(web):
    return web.app.test_client()


def _accept_marketing(client):
    try:
        client.set_cookie("af_privacy_marketing", "v1:accepted")
    except TypeError:
        client.set_cookie("localhost", "af_privacy_marketing", "v1:accepted")


def _reject_marketing(client):
    try:
        client.set_cookie("af_privacy_marketing", "v1:rejected")
    except TypeError:
        client.set_cookie("localhost", "af_privacy_marketing", "v1:rejected")


def test_home_anonima_sem_pixel_id_nao_inclui_sdk(web):
    resp = _client(web).get("/")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert SDK_URL not in html
    assert "oaiq(" not in html
    assert "oaiq.min.js" not in html


def test_home_com_pixel_id_sem_consentimento_nao_inclui_sdk(web):
    web.app.config["OPENAI_ADS_PIXEL_ID"] = PIXEL_ID
    html = _client(web).get("/").get_data(as_text=True)
    assert SDK_URL not in html
    assert "oaiq(" not in html
    assert "oaiq.min.js" not in html
    assert "bzrcdn.openai.com" not in html


def test_home_com_pixel_id_rejected_nao_inclui_sdk(web):
    web.app.config["OPENAI_ADS_PIXEL_ID"] = PIXEL_ID
    client = _client(web)
    _reject_marketing(client)
    html = client.get("/").get_data(as_text=True)
    assert SDK_URL not in html
    assert "oaiq(" not in html
    assert "bzrcdn.openai.com" not in html


def test_home_com_pixel_id_inclui_init_oficial(web):
    web.app.config["OPENAI_ADS_PIXEL_ID"] = PIXEL_ID
    client = _client(web)
    _accept_marketing(client)
    html = client.get("/").get_data(as_text=True)
    assert SDK_URL in html
    assert f'pixelId: "{PIXEL_ID}"' in html
    assert 'oaiq("init"' in html
    assert "debug: true" not in html
    assert "user:" not in _openai_script_blocks(html)


def test_home_com_debug_habilitado_usa_parametro_documentado(web):
    web.app.config["OPENAI_ADS_PIXEL_ID"] = PIXEL_ID
    web.app.config["OPENAI_ADS_DEBUG"] = True
    client = _client(web)
    _accept_marketing(client)
    html = client.get("/").get_data(as_text=True)
    assert 'oaiq("init"' in html
    assert "debug: true" in html


def test_home_com_oppref_continua_200(web):
    web.app.config["OPENAI_ADS_PIXEL_ID"] = PIXEL_ID
    client = _client(web)
    _accept_marketing(client)
    resp = client.get("/?oppref=TESTE_OPACO")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert SDK_URL in html
    assert "__oppref" not in html


def test_segunda_pagina_publica_login_recebe_pixel_global(web):
    web.app.config["OPENAI_ADS_PIXEL_ID"] = PIXEL_ID
    client = _client(web)
    _accept_marketing(client)
    resp = client.get("/login")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert SDK_URL in html
    assert f'pixelId: "{PIXEL_ID}"' in html


def test_html_nao_inclui_capi_nem_pii_da_integracao(web):
    web.app.config["OPENAI_ADS_PIXEL_ID"] = PIXEL_ID
    client = _client(web)
    _accept_marketing(client)
    html = client.get("/").get_data(as_text=True)
    snippet = _openai_script_blocks(html)
    assert snippet
    for marker in FORBIDDEN_PII_MARKERS:
        assert marker not in snippet
        assert marker not in html
    assert "email_sha256" not in html.lower()
    assert "user id" not in snippet.lower()


def test_registration_completed_somente_no_mesmo_fato_do_complete_registration(web):
    web.app.config["OPENAI_ADS_PIXEL_ID"] = PIXEL_ID
    client = _client(web)
    _accept_marketing(client)
    with client.session_transaction() as sess:
        sess["pixel_event_complete_registration_once"] = True
    html = client.get("/login").get_data(as_text=True)
    assert "const completeRegistrationEnabled = true" in html
    assert 'window.oaiq("measure", "registration_completed"' in html
    assert 'type: "customer_action"' in html
    for event_name in FORBIDDEN_CONVERSIONS:
        assert f'"{event_name}"' not in html
        assert f"'{event_name}'" not in html


def test_ausencia_do_pixel_nao_quebra_o_fato_de_registro(web):
    web.app.config["OPENAI_ADS_PIXEL_ID"] = ""
    client = _client(web)
    with client.session_transaction() as sess:
        sess["pixel_event_complete_registration_once"] = True
    resp = client.get("/login")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "oaiq(" not in html
    assert "registration_completed" not in html


def test_pixel_sem_fato_de_registro_nao_emite_conversao(web):
    web.app.config["OPENAI_ADS_PIXEL_ID"] = PIXEL_ID
    client = _client(web)
    _accept_marketing(client)
    html = client.get("/").get_data(as_text=True)
    assert 'oaiq("init"' in html
    assert "const completeRegistrationEnabled = false" in html
    assert "if (completeRegistrationEnabled && hasOaiq())" in html
    assert 'window.oaiq("measure", "registration_completed"' in html


def test_openai_e_meta_permanecem_independentes(web):
    web.app.config["OPENAI_ADS_PIXEL_ID"] = PIXEL_ID
    web.app.config["FACEBOOK_PIXEL_ID"] = "meta_pixel_test"
    client = _client(web)
    _accept_marketing(client)
    with client.session_transaction() as sess:
        sess["pixel_event_complete_registration_once"] = True
    html = client.get("/login").get_data(as_text=True)
    assert 'trackEvent("CompleteRegistration")' in html
    assert 'window.oaiq("measure", "registration_completed"' in html
    assert "fbq('init'" in html or 'fbq("init"' in html or "fbq('init', 'meta_pixel_test')" in html


def test_robots_autoriza_oai_adsbot_e_searchbot_na_raiz(web):
    resp = _client(web).get("/robots.txt")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "User-agent: OAI-AdsBot" in body
    assert "User-agent: OAI-SearchBot" in body
    ads_idx = body.index("User-agent: OAI-AdsBot")
    search_idx = body.index("User-agent: OAI-SearchBot")
    allow_idx = body.index("Allow: /")
    star_idx = body.index("User-agent: *")
    assert ads_idx < search_idx < allow_idx < star_idx


def test_partials_estruturais_nao_expandem_escopo():
    base = PIXEL_BASE.read_text(encoding="utf-8")
    events = PIXEL_EVENTS.read_text(encoding="utf-8")
    meta = META_EVENTS.read_text(encoding="utf-8")
    web_src = WEB_PY.read_text(encoding="utf-8")
    settings_src = SETTINGS_PY.read_text(encoding="utf-8")

    assert SDK_URL in base
    assert "privacy_marketing_allowed" in base
    assert 'oaiq("init"' in base
    assert "pixelId:" in base
    assert "debug: true" in base
    assert "user:" not in base
    assert "email_sha256" not in base
    assert "oppref" not in base.lower()

    assert "privacy_marketing_allowed" in events
    assert 'pixel_event_complete_registration' in events
    assert 'window.oaiq("measure", "registration_completed"' in events
    assert 'type: "customer_action"' in events
    assert "privacy_marketing_allowed" in meta
    assert 'trackEvent("CompleteRegistration")' in meta
    for event_name in FORBIDDEN_CONVERSIONS:
        assert event_name not in events
        assert event_name not in base

    assert "OPENAI_ADS_CAPI" not in web_src
    assert "OPENAI_ADS_CAPI" not in settings_src
    assert "bzr.openai.com/v1/events" not in web_src
    assert "request.args" not in events
    assert 'request.args["oppref"]' not in web_src.split("OPENAI_ADS_PIXEL_ID")[1][:400]
    assert "journey_id" not in events
    assert "FunnelEvent" not in events
    assert "invoice.paid" not in events
