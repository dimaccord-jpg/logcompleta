"""Cobertura estrutural Meta Pixel apos SCRUM-157B (legados Audit* removidos)."""
from __future__ import annotations

import pathlib
import re

from app.funnel_event_service import META_PIXEL_ALLOWED_EVENTS, is_meta_pixel_allowed


PIXEL_EVENTS = pathlib.Path("app/templates/partials/pixel_events.html")
PIXEL_BASE = pathlib.Path("app/templates/partials/pixel_base.html")
CLEIDE_JS = pathlib.Path("app/static/js/cleide_auditoria.js")
AGENTE_JS = pathlib.Path("app/static/js/agente_compara.js")
CONTRATE_PLANO = pathlib.Path("app/templates/contrate_plano.html")
EXTERNAL_JS = pathlib.Path("app/static/js/af_external_tracking.js")


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def _fn(js: str, name: str, next_name: str | None = None) -> str:
    start = js.index(f"function {name}")
    if next_name:
        end = js.index(f"function {next_name}", start + 1)
        return js[start:end]
    return js[start : start + 12000]


def _pixel_helper() -> str:
    return _read(PIXEL_EVENTS)


def test_pixel_sdk_requires_privacy_marketing_allowed():
    base = _read(PIXEL_BASE)
    events = _read(PIXEL_EVENTS)
    assert "privacy_marketing_allowed" in base
    assert "privacy_marketing_allowed" in events
    assert "connect.facebook.net" in base
    assert "fbevents.js" in base


def test_track_standard_events_still_use_fbq_track():
    helper = _pixel_helper()
    track_fn = helper[helper.index("function trackEvent") : helper.index("function trackCustomEvent")]
    assert 'window.fbq("track", eventName, params)' in track_fn
    assert 'window.fbq("track", eventName)' in track_fn
    assert "trackCustom" not in track_fn


def test_track_custom_helper_preserved_without_legacy_funnel_map():
    helper = _pixel_helper()
    assert "function trackCustomEvent" in helper
    assert "trackCustom: trackCustomEvent" in helper
    assert "trackFunnelEvent" not in helper
    assert 'trackCustomEvent("AuditStarted"' not in helper
    assert 'trackCustomEvent("FirstAuditCompleted"' not in helper


def test_standard_pixel_events_curated_by_157():
    helper = _pixel_helper()
    base = _read(PIXEL_BASE)
    contrate = _read(CONTRATE_PLANO)

    assert "fbq('track', 'PageView')" not in base
    assert 'fbq("track", "PageView")' not in base
    assert "fbq('init'" in base or 'fbq("init"' in base

    assert 'trackEvent("CompleteRegistration")' not in helper
    assert 'trackEvent("Lead")' not in helper
    assert "Purchase" not in helper
    assert "trackEventOnceBySessionId" not in helper
    assert 'window.LogCompletaPixel.track("InitiateCheckout")' not in contrate

    assert 'trackCustomEvent("AuditStarted"' not in helper
    assert 'trackCustomEvent("FirstAuditCompleted"' not in helper
    assert "track: trackEvent" in helper


def test_meta_pixel_allowed_events_empty_after_157b():
    assert META_PIXEL_ALLOWED_EVENTS == set()
    assert is_meta_pixel_allowed("file_uploaded") is False
    assert is_meta_pixel_allowed("freight_calculated") is False
    assert is_meta_pixel_allowed("first_audit_completed") is False


def test_cleide_no_longer_calls_track_funnel_event():
    js = _read(CLEIDE_JS)
    upload_fn = _fn(js, "uploadDocument", "removeDocument")
    assert "window.LogCompletaPixel.trackFunnelEvent" not in upload_fn
    assert "AuditStarted" not in upload_fn
    audit_fn = _fn(js, "runAuditProcessing", "normalizeTaxLocationText")
    assert "window.LogCompletaPixel.trackFunnelEvent" not in audit_fn
    assert "FirstAuditCompleted" not in audit_fn


def test_agente_compara_no_longer_calls_track_funnel_event():
    js = _read(AGENTE_JS)
    upload_fn = _fn(js, "uploadDocument", "removeDocument")
    assert "window.LogCompletaPixel.trackFunnelEvent" not in upload_fn
    assert "AuditStarted" not in upload_fn
    process_fn = _fn(js, "processComparisonCalculations", "clearCalculationFileSummary")
    assert "window.LogCompletaPixel.trackFunnelEvent" not in process_fn
    assert "FirstAuditCompleted" not in process_fn


def test_cleide_and_agente_never_call_fbq_directly():
    cleide = _read(CLEIDE_JS)
    agente = _read(AGENTE_JS)
    assert "window.fbq" not in cleide
    assert "window.fbq" not in agente
    assert "fbq(" not in cleide
    assert "fbq(" not in agente


def test_af_external_tracking_first_relevant_meta_ga4():
    src = _read(EXTERNAL_JS)
    assert "first_relevant_task_completed: true" in src
    assert 'FirstRelevantTaskCompleted"' in src or "FirstRelevantTaskCompleted" in src
    assert 'trackCustom"' in src or "trackCustom" in src
    assert re.search(
        r'first_relevant_task_completed:\s*"first_relevant_task_completed"',
        src,
    )
    assert "eventID" in src
