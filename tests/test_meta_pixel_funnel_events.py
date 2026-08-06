"""Cobertura estrutural da Etapa 2 do Meta Pixel (eventos personalizados de funil)."""
from __future__ import annotations

import pathlib
import re


PIXEL_EVENTS = pathlib.Path("app/templates/partials/pixel_events.html")
PIXEL_BASE = pathlib.Path("app/templates/partials/pixel_base.html")
CLEIDE_JS = pathlib.Path("app/static/js/cleide_auditoria.js")
AGENTE_JS = pathlib.Path("app/static/js/agente_compara.js")
CONTRATE_PLANO = pathlib.Path("app/templates/contrate_plano.html")


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


def test_track_standard_events_still_use_fbq_track():
    helper = _pixel_helper()
    track_fn = helper[helper.index("function trackEvent") : helper.index("function trackCustomEvent")]
    assert 'window.fbq("track", eventName, params)' in track_fn
    assert 'window.fbq("track", eventName)' in track_fn
    assert "trackCustom" not in track_fn


def test_track_custom_uses_fbq_track_custom_only():
    helper = _pixel_helper()
    custom_fn = helper[
        helper.index("function trackCustomEvent") : helper.index("function trackFunnelEvent")
    ]
    assert 'window.fbq("trackCustom", eventName, params)' in custom_fn
    assert 'window.fbq("trackCustom", eventName)' in custom_fn
    assert 'window.fbq("track"' not in custom_fn
    assert "trackCustom: trackCustomEvent" in helper
    assert "trackFunnelEvent: trackFunnelEvent" in helper


def test_track_funnel_event_maps_file_uploaded_to_audit_started():
    helper = _pixel_helper()
    funnel_fn = helper[
        helper.index("function trackFunnelEvent") : helper.index("function trackEventOnceBySessionId")
    ]
    assert 'funnelEvent.allow_meta_pixel !== true' in funnel_fn
    assert 'funnelEvent.event_name === "file_uploaded"' in funnel_fn
    assert 'trackCustomEvent("AuditStarted", params)' in funnel_fn


def test_track_funnel_event_maps_first_audit_completed_only_when_authorized():
    helper = _pixel_helper()
    funnel_fn = helper[
        helper.index("function trackFunnelEvent") : helper.index("function trackEventOnceBySessionId")
    ]
    assert 'funnelEvent.event_name === "freight_calculated"' in funnel_fn
    assert "funnelEvent.is_first_audit === true" in funnel_fn
    assert 'trackCustomEvent("FirstAuditCompleted", params)' in funnel_fn
    # freight_calculated sem is_first_audit true não dispara FirstAuditCompleted
    assert (
        funnel_fn.index('funnelEvent.event_name === "freight_calculated"')
        < funnel_fn.index("funnelEvent.is_first_audit === true")
        < funnel_fn.index('trackCustomEvent("FirstAuditCompleted", params)')
    )


def test_track_funnel_event_rejects_unknown_and_invalid_payloads():
    helper = _pixel_helper()
    funnel_fn = helper[
        helper.index("function trackFunnelEvent") : helper.index("function trackEventOnceBySessionId")
    ]
    assert 'typeof funnelEvent !== "object"' in funnel_fn
    assert "return false;" in funnel_fn
    assert "catch (err)" in funnel_fn
    # Não há mapeamento genérico que envie event_name cru à Meta
    assert "trackCustomEvent(funnelEvent.event_name" not in funnel_fn
    assert 'trackCustomEvent(eventName' not in funnel_fn


def test_standard_pixel_events_remain_intact():
    helper = _pixel_helper()
    base = _read(PIXEL_BASE)
    contrate = _read(CONTRATE_PLANO)

    assert "fbq('track', 'PageView')" in base or 'fbq("track", "PageView")' in base
    assert 'trackEvent("CompleteRegistration")' in helper
    assert 'trackEvent("Lead")' in helper
    assert 'subscribeEvent.event_name || "Purchase"' in helper
    assert 'window.LogCompletaPixel.track("InitiateCheckout")' in contrate
    # track padrão continua exposto
    assert "track: trackEvent" in helper


def test_cleide_calls_track_funnel_event_after_successful_upload():
    js = _read(CLEIDE_JS)
    upload_fn = _fn(js, "uploadDocument", "removeDocument")
    assert "res.data.ok !== true" in upload_fn
    assert "window.LogCompletaPixel.trackFunnelEvent" in upload_fn
    assert "res.data.funnel_event" in upload_fn
    assert "Auditoria da Cleide" in upload_fn
    # Disparo só depois do ok === true
    ok_idx = upload_fn.index("res.data.ok !== true")
    track_idx = upload_fn.index("window.LogCompletaPixel.trackFunnelEvent")
    assert ok_idx < track_idx
    assert "window.fbq" not in upload_fn


def test_cleide_calls_track_funnel_event_after_successful_audit():
    js = _read(CLEIDE_JS)
    audit_fn = _fn(js, "runAuditProcessing", "normalizeTaxLocationText")
    assert "API_AUDIT_RUN" in audit_fn
    assert "res.data.ok !== true" in audit_fn
    assert "window.LogCompletaPixel.trackFunnelEvent" in audit_fn
    assert "res.data.funnel_event" in audit_fn
    assert "Auditoria da Cleide" in audit_fn
    ok_idx = audit_fn.index("res.data.ok !== true")
    track_idx = audit_fn.index("window.LogCompletaPixel.trackFunnelEvent")
    assert ok_idx < track_idx
    assert "window.fbq" not in audit_fn


def test_agente_compara_calls_track_funnel_event_after_validated_upload():
    js = _read(AGENTE_JS)
    upload_fn = _fn(js, "uploadDocument", "removeDocument")
    assert "uploadAttemptStillActive()" in upload_fn
    assert "responseMatchesUploadAttempt(res.data)" in upload_fn
    assert "res.data.ok !== true" in upload_fn
    assert "window.LogCompletaPixel.trackFunnelEvent" in upload_fn
    assert "res.data.funnel_event" in upload_fn
    assert "Agente Compara" in upload_fn

    match_idx = upload_fn.index("responseMatchesUploadAttempt(res.data)")
    track_idx = upload_fn.index("window.LogCompletaPixel.trackFunnelEvent")
    assert match_idx < track_idx
    assert "window.fbq" not in upload_fn


def test_agente_compara_calls_track_funnel_event_in_calculation_ready_block():
    js = _read(AGENTE_JS)
    process_fn = _fn(js, "processComparisonCalculations", "clearCalculationFileSummary")
    ready_blocks = list(
        re.finditer(r"data\.status === ['\"]CALCULATION_READY['\"]", process_fn)
    )
    assert ready_blocks, "Bloco CALCULATION_READY não encontrado"
    ready_idx = ready_blocks[0].start()
    track_idx = process_fn.index("window.LogCompletaPixel.trackFunnelEvent", ready_idx)
    failed_idx = process_fn.index("CALCULATION_FAILED", ready_idx)
    assert ready_idx < track_idx < failed_idx
    assert "data.funnel_event" in process_fn[ready_idx:failed_idx]
    assert "Agente Compara" in process_fn[ready_idx:failed_idx]


def test_cleide_and_agente_never_call_fbq_directly():
    cleide = _read(CLEIDE_JS)
    agente = _read(AGENTE_JS)
    assert "window.fbq" not in cleide
    assert "window.fbq" not in agente
    assert "fbq(" not in cleide
    assert "fbq(" not in agente


def test_pixel_degradation_guards_present_in_integrations():
    cleide = _read(CLEIDE_JS)
    agente = _read(AGENTE_JS)
    guard = "typeof window.LogCompletaPixel.trackFunnelEvent === 'function'"
    assert cleide.count(guard) >= 2
    assert agente.count(guard) >= 2
    assert "catch (pixelErr)" in cleide
    assert "catch (pixelErr)" in agente
