"""SCRUM-160: monitor minimo de qualidade de tracking e diagnostico CAPI."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests
from sqlalchemy import event
from sqlalchemy.orm import Query

from app.extensions import db
from app.funnel_event_service import (
    FUNNEL_EVENT_CHECKOUT_STARTED,
    FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
    FUNNEL_EVENT_PAGE_VIEW,
    FUNNEL_EVENT_PAID,
    FUNNEL_EVENT_PLAN_SELECTED,
    FUNNEL_EVENT_SESSION_ORIGIN_OBSERVED,
    FUNNEL_EVENT_SIGNUP_COMPLETED,
    FUNNEL_EVENT_SIGNUP_STARTED,
    FUNNEL_EVENT_TASK_COMPLETED,
    FUNNEL_SOURCE_GROWTH,
    SIGNUP_METHOD_PASSWORD,
    TASK_TYPE_CLEIDE_AUDIT,
    TASK_TYPE_ONBOARDING_DISCOVERY,
    record_funnel_event,
)
from app.privacy_marketing import PRIVACY_MARKETING_COOKIE_NAME
from app.services import growth_external_server_service as capi
from app.services.growth_external_event_service import build_external_event_token
from app.services.growth_tracking_quality_service import (
    GA4_BROWSER_CANDIDATE_EVENTS,
    KNOWN_LIMITATIONS,
    META_BROWSER_CAPI_CANDIDATE_EVENTS,
    get_growth_tracking_quality_payload,
)
from tests.conftest import seed_conta_franquia_cliente, seed_usuario

START = datetime(2026, 3, 1, 0, 0, 0)
END = datetime(2026, 3, 8, 0, 0, 0)
INSIDE = datetime(2026, 3, 3, 12, 0, 0)

SECRET_UTM = "secret-utm-source-160"
SECRET_GCLID = "gclid-secret-value-160"
SECRET_GBRAID = "gbraid-secret-value-160"
SECRET_WBRAID = "wbraid-secret-value-160"
SECRET_FBCLID = "fbclid-secret-value-160"
SECRET_EMAIL = "secret-user-160@test.com"

PIXEL_ID = "pixel-secret-160"
ACCESS_TOKEN = "capi-token-secret-160"
GRAPH_V = "v26.0"
PUBLIC_BASE = "https://www.agentefrete.com.br"
FBP_OK = "fb.1.160.secretfbpvalue"
FBC_OK = "fb.1.160.secretfbcvalue"

FORBIDDEN_PAYLOAD_KEYS = {
    "user_id",
    "conta_id",
    "franquia_id",
    "email",
    "fbp",
    "fbc",
    "event_id",
    "access_token",
    "gclid",
    "gbraid",
    "wbraid",
    "fbclid",
    "utm_source",
    "sent",
    "delivered",
    "received",
    "direct",
}


def _record(**kwargs):
    kwargs.setdefault("source", FUNNEL_SOURCE_GROWTH)
    kwargs.setdefault("occurred_at", INSIDE)
    record_funnel_event(**kwargs)


def _payload(start=START, end=END):
    return get_growth_tracking_quality_payload(start=start, end=end)


def _keys(value, found: set[str] | None = None) -> set[str]:
    if found is None:
        found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(str(key))
            _keys(item, found)
    elif isinstance(value, list):
        for item in value:
            _keys(item, found)
    return found


def _log_fields(message: str) -> dict[str, str]:
    fields = {}
    for part in message.split():
        if "=" not in part:
            continue
        key, raw = part.split("=", 1)
        fields[key] = raw
    return fields


def _capi_messages(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("growth_meta_capi ")
    ]


def _enable_capi(monkeypatch):
    monkeypatch.setenv("APP_ENV", "homolog")
    monkeypatch.setenv("META_CAPI_ENABLED", "true")
    monkeypatch.setenv("FACEBOOK_PIXEL_ID", PIXEL_ID)
    monkeypatch.setenv("META_CAPI_ACCESS_TOKEN", ACCESS_TOKEN)
    monkeypatch.setenv("META_GRAPH_API_VERSION", GRAPH_V)
    monkeypatch.setenv("PUBLIC_BASE_URL", PUBLIC_BASE)
    monkeypatch.delenv("META_CAPI_TEST_EVENT_CODE", raising=False)


def _request(app, *, privacy: str | None, fbp: str | None = None, fbc: str | None = None):
    cookies = []
    if privacy is not None:
        cookies.append(f"{PRIVACY_MARKETING_COOKIE_NAME}={privacy}")
    if fbp is not None:
        cookies.append(f"_fbp={fbp}")
    if fbc is not None:
        cookies.append(f"_fbc={fbc}")
    return app.test_request_context(
        "/",
        environ_base={"HTTP_COOKIE": "; ".join(cookies)},
    )


def _fake_event(**kwargs):
    defaults = {
        "id": 42,
        "event_name": FUNNEL_EVENT_PAGE_VIEW,
        "metadata_json": {"page": "index"},
        "occurred_at": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=5),
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _send(app, monkeypatch, caplog, *, privacy, fbp=None, fbc=None, event=None, post=None):
    _enable_capi(monkeypatch)
    app.config["SECRET_KEY"] = "test-secret-160"
    event = event or _fake_event()
    with app.app_context(), _request(app, privacy=privacy, fbp=fbp, fbc=fbc):
        with caplog.at_level(logging.INFO, logger=capi.logger.name):
            if post is None:
                with patch.object(capi.requests, "post") as mocked:
                    mocked.return_value = MagicMock(status_code=200)
                    result = capi.try_send_growth_meta_capi(event)
                    return result, _capi_messages(caplog), mocked
            with patch.object(capi.requests, "post", post):
                result = capi.try_send_growth_meta_capi(event)
                return result, _capi_messages(caplog), post


# --- PERIODO / INTERNO -------------------------------------------------------


def test_period_bounds_are_start_inclusive_end_exclusive(ctx):
    _record(
        event_name=FUNNEL_EVENT_PAGE_VIEW,
        idempotency_key="160-bound-start",
        occurred_at=START,
    )
    _record(
        event_name=FUNNEL_EVENT_PAGE_VIEW,
        idempotency_key="160-bound-inside",
        occurred_at=END - timedelta(seconds=1),
    )
    _record(
        event_name=FUNNEL_EVENT_PAGE_VIEW,
        idempotency_key="160-bound-end",
        occurred_at=END,
    )
    _record(
        event_name=FUNNEL_EVENT_PAGE_VIEW,
        idempotency_key="160-bound-before",
        occurred_at=START - timedelta(seconds=1),
    )
    payload = _payload()
    assert payload["period"]["bounds"] == "start_inclusive_end_exclusive"
    assert payload["internal"]["event_counts"]["page_view"] == 2


def test_aware_bounds_are_compared_as_naive_utc(ctx):
    _record(
        event_name=FUNNEL_EVENT_PAGE_VIEW,
        idempotency_key="160-aware",
        occurred_at=INSIDE,
    )
    payload = _payload(
        start=START.replace(tzinfo=timezone.utc),
        end=END.replace(tzinfo=timezone.utc),
    )
    assert payload["internal"]["event_counts"]["page_view"] == 1
    assert payload["period"]["start"] == START.isoformat()


def test_inverted_period_is_rejected():
    with pytest.raises(ValueError):
        get_growth_tracking_quality_payload(start=END, end=START)


def test_empty_period_is_zero_counts_without_fake_capi_metrics(ctx):
    payload = _payload()
    assert payload["internal"]["event_counts"] == {
        "page_view": 0,
        "signup_started": 0,
        "signup_completed": 0,
        "task_completed": 0,
        "first_relevant_task_completed": 0,
        "plan_selected": 0,
        "checkout_started": 0,
        "paid": 0,
    }
    assert "rates" not in payload["internal"]
    signup = payload["attribution"]["current_session_origin"]["by_event_name"][
        "signup_completed"
    ]
    assert signup["applicable_count"] == 0
    assert signup["origin_not_recorded_count"] == 0
    assert signup["rates"]["with_acquisition_evidence"] is None
    assert payload["first_relevant_quality"]["eligible_completion_count"] == 0
    assert payload["first_relevant_quality"]["completions_without_marker_count"] == 0
    assert payload["meta_capi"] == {
        "source": "process_logs",
        "historical_database_metrics_available": False,
    }
    assert payload["known_limitations"] == list(KNOWN_LIMITATIONS)
    assert payload["external_candidates"]["meta_browser_capi"]["external_candidate_count"] == 0


def test_internal_event_counts_do_not_mix_stage_rates(ctx):
    conta, franquia = seed_conta_franquia_cliente(slug="160-internal")
    user = seed_usuario(franquia.id, conta.id, email="160-internal@test.com")
    samples = [
        (FUNNEL_EVENT_PAGE_VIEW, "160-pv-1", None, None),
        (FUNNEL_EVENT_PAGE_VIEW, "160-pv-2", None, None),
        (FUNNEL_EVENT_SIGNUP_STARTED, "160-ss", None, None),
        (FUNNEL_EVENT_SIGNUP_COMPLETED, "160-sc", user.id, {"signup_method": SIGNUP_METHOD_PASSWORD}),
        (FUNNEL_EVENT_TASK_COMPLETED, "160-tc", user.id, {"task_type": TASK_TYPE_CLEIDE_AUDIT}),
        (
            FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
            "160-fr",
            user.id,
            {"task_type": TASK_TYPE_CLEIDE_AUDIT},
        ),
        (FUNNEL_EVENT_PLAN_SELECTED, "160-ps", user.id, {"plan": "starter"}),
        (FUNNEL_EVENT_CHECKOUT_STARTED, "160-co", None, {"plan": "starter"}),
        (FUNNEL_EVENT_PAID, "160-paid", None, None),
    ]
    for name, key, user_id, metadata in samples:
        kwargs = {
            "event_name": name,
            "idempotency_key": key,
            "metadata_json": metadata,
        }
        if user_id is not None:
            kwargs["user_id"] = user_id
        if name in (FUNNEL_EVENT_CHECKOUT_STARTED, FUNNEL_EVENT_PAID):
            kwargs["conta_id"] = conta.id
        _record(**kwargs)
    _record(
        event_name=FUNNEL_EVENT_PAGE_VIEW,
        idempotency_key="160-pv-out",
        occurred_at=END,
    )
    counts = _payload()["internal"]["event_counts"]
    assert counts["page_view"] == 2
    assert counts["signup_started"] == 1
    assert counts["signup_completed"] == 1
    assert counts["task_completed"] == 1
    assert counts["first_relevant_task_completed"] == 1
    assert counts["plan_selected"] == 1
    assert counts["checkout_started"] == 1
    assert counts["paid"] == 1
    assert "signup_to_paid" not in counts


# --- ATTRIBUICAO -------------------------------------------------------------


def test_attribution_separates_touches_and_does_not_call_missing_origin_direct(ctx):
    conta, franquia = seed_conta_franquia_cliente(slug="160-attr")
    user = seed_usuario(franquia.id, conta.id, email=SECRET_EMAIL)
    _record(
        event_name=FUNNEL_EVENT_SIGNUP_COMPLETED,
        user_id=user.id,
        idempotency_key="160-attr-absent",
        metadata_json={"signup_method": SIGNUP_METHOD_PASSWORD},
    )
    _record(
        event_name=FUNNEL_EVENT_SIGNUP_COMPLETED,
        user_id=user.id,
        idempotency_key="160-attr-split",
        metadata_json={
            "signup_method": SIGNUP_METHOD_PASSWORD,
            "growth_attribution": {
                "first_touch": {"source": SECRET_UTM, "medium": "cpc"},
                "current_session_origin": {"gclid": SECRET_GCLID},
            },
        },
    )
    _record(
        event_name=FUNNEL_EVENT_SIGNUP_COMPLETED,
        user_id=user.id,
        idempotency_key="160-attr-gbraid",
        metadata_json={
            "signup_method": SIGNUP_METHOD_PASSWORD,
            "growth_attribution": {
                "current_session_origin": {"gbraid": SECRET_GBRAID},
            },
        },
    )
    _record(
        event_name=FUNNEL_EVENT_SIGNUP_COMPLETED,
        user_id=user.id,
        idempotency_key="160-attr-wbraid",
        metadata_json={
            "signup_method": SIGNUP_METHOD_PASSWORD,
            "growth_attribution": {
                "current_session_origin": {"wbraid": SECRET_WBRAID},
            },
        },
    )
    _record(
        event_name=FUNNEL_EVENT_CHECKOUT_STARTED,
        conta_id=conta.id,
        idempotency_key="160-attr-fbclid",
        metadata_json={
            "plan": "starter",
            "growth_attribution": {
                "current_session_origin": {"fbclid": SECRET_FBCLID},
            },
        },
    )
    _record(
        event_name=FUNNEL_EVENT_SESSION_ORIGIN_OBSERVED,
        user_id=user.id,
        idempotency_key="160-attr-session",
        metadata_json={
            "growth_attribution": {
                "current_session_origin": {"source": "linkedin", "medium": "social"},
            },
        },
    )

    payload = _payload()
    attribution = payload["attribution"]
    assert attribution["missing_origin_means"] == "origin_not_recorded"
    assert "session_origin_observed" not in attribution["first_touch"]["by_event_name"]
    assert "session_origin_observed" in attribution["current_session_origin"]["by_event_name"]

    first_signup = attribution["first_touch"]["by_event_name"]["signup_completed"]
    current_signup = attribution["current_session_origin"]["by_event_name"]["signup_completed"]
    assert first_signup["applicable_count"] == 4
    assert first_signup["with_known_source_count"] == 1
    assert first_signup["with_utm_count"] == 1
    assert first_signup["with_any_click_id_count"] == 0
    assert first_signup["with_acquisition_evidence_count"] == 1
    assert first_signup["origin_not_recorded_count"] == 3

    assert current_signup["applicable_count"] == 4
    assert current_signup["with_known_source_count"] == 0
    assert current_signup["with_utm_count"] == 0
    assert current_signup["with_acquisition_evidence_count"] == 3
    assert current_signup["origin_not_recorded_count"] == 1
    assert current_signup["gclid_present_count"] == 1
    assert current_signup["gbraid_present_count"] == 1
    assert current_signup["wbraid_present_count"] == 1
    assert current_signup["fbclid_present_count"] == 0
    assert current_signup["with_any_click_id_count"] == 3
    assert current_signup["rates"]["with_acquisition_evidence"] == 0.75

    current_checkout = attribution["current_session_origin"]["by_event_name"][
        "checkout_started"
    ]
    first_checkout = attribution["first_touch"]["by_event_name"]["checkout_started"]
    assert current_checkout["with_known_source_count"] == 0
    assert current_checkout["with_utm_count"] == 0
    assert current_checkout["fbclid_present_count"] == 1
    assert current_checkout["with_any_click_id_count"] == 1
    assert current_checkout["with_acquisition_evidence_count"] == 1
    assert first_checkout["origin_not_recorded_count"] == 1
    assert first_checkout["with_acquisition_evidence_count"] == 0

    session = attribution["current_session_origin"]["by_event_name"][
        "session_origin_observed"
    ]
    assert session["with_known_source_count"] == 1
    assert session["with_acquisition_evidence_count"] == 1
    assert session["origin_not_recorded_count"] == 0

    current_totals = attribution["current_session_origin"]["totals"]
    assert current_totals["gclid_present_count"] == 1
    assert current_totals["gbraid_present_count"] == 1
    assert current_totals["wbraid_present_count"] == 1
    assert current_totals["fbclid_present_count"] == 1
    assert current_totals["with_any_click_id_count"] == 4

    rendered = json.dumps(payload)
    for secret in (
        SECRET_UTM,
        SECRET_GCLID,
        SECRET_GBRAID,
        SECRET_WBRAID,
        SECRET_FBCLID,
        SECRET_EMAIL,
    ):
        assert secret not in rendered
    assert FORBIDDEN_PAYLOAD_KEYS.isdisjoint(_keys(payload))


def test_external_candidates_follow_allowlists_without_claiming_dispatch(ctx):
    conta, franquia = seed_conta_franquia_cliente(slug="160-ext")
    user = seed_usuario(franquia.id, conta.id, email="160-ext@test.com")
    _record(event_name=FUNNEL_EVENT_PAGE_VIEW, idempotency_key="160-ext-pv-1")
    _record(event_name=FUNNEL_EVENT_PAGE_VIEW, idempotency_key="160-ext-pv-2")
    _record(
        event_name=FUNNEL_EVENT_SIGNUP_COMPLETED,
        user_id=user.id,
        idempotency_key="160-ext-sc",
        metadata_json={"signup_method": SIGNUP_METHOD_PASSWORD},
    )
    _record(
        event_name=FUNNEL_EVENT_CHECKOUT_STARTED,
        conta_id=conta.id,
        idempotency_key="160-ext-co",
        metadata_json={"plan": "starter"},
    )
    _record(
        event_name=FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
        user_id=user.id,
        idempotency_key="160-ext-fr",
        metadata_json={"task_type": TASK_TYPE_CLEIDE_AUDIT},
    )
    _record(
        event_name=FUNNEL_EVENT_PAID,
        conta_id=conta.id,
        idempotency_key="160-ext-paid",
    )
    _record(event_name=FUNNEL_EVENT_SIGNUP_STARTED, idempotency_key="160-ext-ss")
    _record(event_name=FUNNEL_EVENT_PLAN_SELECTED, user_id=user.id, idempotency_key="160-ext-ps", metadata_json={"plan": "pro"})

    payload = _payload()
    external = payload["external_candidates"]
    assert external["derivation"] == "event_name_allowlist"
    assert external["does_not_mean"] == "dispatched_delivered_or_received"
    for destination, allowlist in (
        ("meta_browser_capi", META_BROWSER_CAPI_CANDIDATE_EVENTS),
        ("ga4_browser", GA4_BROWSER_CANDIDATE_EVENTS),
    ):
        block = external[destination]
        assert set(block["by_event_name"]) == set(allowlist)
        assert "paid" not in block["by_event_name"]
        assert "signup_started" not in block["by_event_name"]
        assert block["by_event_name"]["page_view"]["external_candidate_count"] == 2
        assert block["by_event_name"]["signup_completed"]["external_candidate_count"] == 1
        assert block["by_event_name"]["checkout_started"]["external_candidate_count"] == 1
        assert (
            block["by_event_name"]["first_relevant_task_completed"]["external_candidate_count"]
            == 1
        )
        assert block["external_candidate_count"] == 5
    assert "external_candidate_does_not_imply_dispatch" in payload["known_limitations"]
    assert "paid_has_no_external_purchase_at_this_stage" in payload["known_limitations"]


# --- FIRST RELEVANT ----------------------------------------------------------


def test_first_relevant_gap_is_investigable_and_respects_marker_history(ctx):
    conta, franquia = seed_conta_franquia_cliente(slug="160-frq")
    user_with = seed_usuario(franquia.id, conta.id, email="160-with@test.com")
    user_without = seed_usuario(franquia.id, conta.id, email="160-without@test.com")
    user_before = seed_usuario(franquia.id, conta.id, email="160-before@test.com")
    user_recovery = seed_usuario(franquia.id, conta.id, email="160-recovery@test.com")

    def completion(user_id, key, when=INSIDE):
        _record(
            event_name=FUNNEL_EVENT_TASK_COMPLETED,
            user_id=user_id,
            idempotency_key=key,
            occurred_at=when,
            metadata_json={"task_type": TASK_TYPE_CLEIDE_AUDIT},
        )

    completion(user_with.id, "160-fr-with-1")
    completion(user_with.id, "160-fr-with-2")
    _record(
        event_name=FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
        user_id=user_with.id,
        idempotency_key="160-fr-with-marker",
        metadata_json={"task_type": TASK_TYPE_CLEIDE_AUDIT},
    )

    completion(user_without.id, "160-fr-without")
    completion(user_without.id, "160-fr-without-old", when=START - timedelta(days=2))

    completion(user_before.id, "160-fr-before-now")
    _record(
        event_name=FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
        user_id=user_before.id,
        idempotency_key="160-fr-before-marker",
        occurred_at=START - timedelta(days=3),
        metadata_json={"task_type": TASK_TYPE_CLEIDE_AUDIT},
    )

    completion(user_recovery.id, "160-fr-recovery")
    _record(
        event_name=FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
        user_id=user_recovery.id,
        idempotency_key="160-fr-recovery-marker",
        metadata_json={"task_type": TASK_TYPE_CLEIDE_AUDIT},
    )

    _record(
        event_name=FUNNEL_EVENT_TASK_COMPLETED,
        user_id=user_without.id,
        idempotency_key="160-fr-onboarding",
        metadata_json={"task_type": TASK_TYPE_ONBOARDING_DISCOVERY},
    )
    _record(
        event_name=FUNNEL_EVENT_TASK_COMPLETED,
        idempotency_key="160-fr-anon",
        metadata_json={"task_type": TASK_TYPE_CLEIDE_AUDIT},
    )

    quality = _payload()["first_relevant_quality"]
    assert quality["interpretation"] == "investigable_indicator"
    assert quality["eligible_completion_count"] == 5
    assert quality["eligible_user_count"] == 4
    assert quality["completions_with_marker_count"] == 4
    assert quality["completions_without_marker_count"] == 1
    assert quality["completions_with_marker_before_period_count"] == 1
    assert quality["users_without_marker_count"] == 1
    assert "gap_is_investigable_not_confirmed_bug" in quality["notes"]
    assert "recovery_may_create_marker_without_external_dispatch" in quality["notes"]
    assert "marker_may_occur_before_period" in quality["notes"]


def test_aggregation_does_not_materialize_page_views_or_walk_users(ctx, monkeypatch):
    def _forbidden(*_args, **_kwargs):
        raise AssertionError("materializou conclusoes por usuario")

    monkeypatch.setattr(
        "app.funnel_event_service._find_first_eligible_task_completed",
        _forbidden,
    )

    def _run():
        statements = []

        def _capture(_conn, _cursor, statement, _parameters, _context, _executemany):
            if "funnel_event" in statement.lower():
                statements.append(statement)

        event.listen(db.engine, "before_cursor_execute", _capture)
        try:
            original_all = Query.all

            def _guard(self, *args, **kwargs):
                compiled = str(self).lower()
                if "funnel_event" in compiled and "count(" not in compiled:
                    raise AssertionError(f"Query.all materializou funnel_event: {compiled}")
                return original_all(self, *args, **kwargs)

            monkeypatch.setattr(Query, "all", _guard)
            _payload()
        finally:
            event.remove(db.engine, "before_cursor_execute", _capture)
        return statements

    _record(event_name=FUNNEL_EVENT_PAGE_VIEW, idempotency_key="160-agg-1")
    first = _run()
    for index in range(24):
        _record(event_name=FUNNEL_EVENT_PAGE_VIEW, idempotency_key=f"160-agg-{index + 2}")
    second = _run()
    assert first
    assert len(first) == len(second)
    assert len(second) <= 4
    for statement in second:
        lowered = statement.lower()
        assert "count(" in lowered
        assert "select funnel_event.id, funnel_event.user_id" not in lowered
    assert _payload()["internal"]["event_counts"]["page_view"] == 25


# --- CAPI LOGS ---------------------------------------------------------------


@pytest.mark.parametrize("status_code", [200, 204])
def test_capi_log_success_2xx(app, ctx, monkeypatch, caplog, status_code):
    def _post(*_args, **_kwargs):
        return MagicMock(status_code=status_code)

    result, messages, _post_mock = _send(
        app,
        monkeypatch,
        caplog,
        privacy="v1:accepted",
        fbp=FBP_OK,
        post=_post,
    )
    assert result["attempted"] is True
    assert result["success"] is True
    assert result["status"] == capi.STATUS_SUCCESS
    assert set(result) == {"attempted", "success", "status", "http_status"}
    fields = _log_fields(messages[-1])
    assert fields["attempted"] == "true"
    assert fields["consent_state"] == "accepted"
    assert fields["status"] == "success"
    assert fields["http_status"] == str(status_code)
    assert fields["timeout"] == "False"
    assert fields["duration_ms"] != "-"
    assert fields["graph_version"] == GRAPH_V


def test_capi_log_timeout(app, ctx, monkeypatch, caplog):
    def _post(*_args, **_kwargs):
        raise requests.Timeout("timed out")

    result, messages, _post_mock = _send(
        app,
        monkeypatch,
        caplog,
        privacy="v1:accepted",
        fbp=FBP_OK,
        post=_post,
    )
    assert result["attempted"] is True
    assert result["status"] == capi.STATUS_TIMEOUT
    fields = _log_fields(messages[-1])
    assert fields["attempted"] == "true"
    assert fields["status"] == "timeout"
    assert fields["timeout"] == "True"
    assert fields["http_status"] == "-"
    assert fields["duration_ms"] != "-"
    assert fields["consent_state"] == "accepted"


def test_capi_log_http_error(app, ctx, monkeypatch, caplog):
    def _post(*_args, **_kwargs):
        return MagicMock(status_code=500)

    result, messages, _post_mock = _send(
        app,
        monkeypatch,
        caplog,
        privacy="v1:accepted",
        fbp=FBP_OK,
        post=_post,
    )
    assert result["attempted"] is True
    assert result["success"] is False
    assert result["status"] == capi.STATUS_HTTP_ERROR
    assert result["http_status"] == 500
    fields = _log_fields(messages[-1])
    assert fields["attempted"] == "true"
    assert fields["status"] == "http_error"
    assert fields["http_status"] == "500"
    assert fields["timeout"] == "False"


def test_capi_log_config_missing_reads_consent_without_changing_gate(app, ctx, monkeypatch, caplog):
    _enable_capi(monkeypatch)
    monkeypatch.delenv("META_CAPI_ACCESS_TOKEN", raising=False)
    app.config["META_CAPI_ACCESS_TOKEN"] = ""
    app.config["SECRET_KEY"] = "test-secret-160"
    with app.app_context(), _request(app, privacy="v1:accepted", fbp=FBP_OK):
        with caplog.at_level(logging.INFO, logger=capi.logger.name):
            with patch.object(capi.requests, "post") as post:
                monkeypatch.setattr(capi, "_settings_attr", lambda _name: "")
                result = capi.try_send_growth_meta_capi(_fake_event())
    assert result["attempted"] is False
    assert result["status"] == capi.STATUS_CONFIG_MISSING
    post.assert_not_called()
    fields = _log_fields(_capi_messages(caplog)[-1])
    assert fields["attempted"] == "false"
    assert fields["status"] == "config_missing"
    assert fields["consent_state"] == "accepted"
    assert fields["http_status"] == "-"
    assert fields["graph_version"] == GRAPH_V


@pytest.mark.parametrize(
    "privacy,expected_state",
    [
        ("v1:accepted", "accepted"),
        ("v1:rejected", "rejected"),
        (None, "unknown"),
    ],
)
def test_capi_log_distinguishes_consent_without_changing_send_decision(
    app, ctx, monkeypatch, caplog, privacy, expected_state
):
    fbp = FBP_OK if privacy == "v1:accepted" else None
    result, messages, post = _send(
        app,
        monkeypatch,
        caplog,
        privacy=privacy,
        fbp=fbp,
    )
    fields = _log_fields(messages[-1])
    assert fields["consent_state"] == expected_state
    if expected_state == "accepted":
        assert result["status"] == capi.STATUS_SUCCESS
        assert fields["attempted"] == "true"
        post.assert_called_once()
    else:
        assert result["status"] == capi.STATUS_CONSENT_NOT_ACCEPTED
        assert result["attempted"] is False
        assert fields["attempted"] == "false"
        assert fields["status"] == "consent_not_accepted"
        post.assert_not_called()


def test_capi_log_user_data_missing(app, ctx, monkeypatch, caplog):
    result, messages, post = _send(
        app,
        monkeypatch,
        caplog,
        privacy="v1:accepted",
        fbp=None,
    )
    assert result["attempted"] is False
    assert result["status"] == capi.STATUS_USER_DATA_MISSING
    post.assert_not_called()
    fields = _log_fields(messages[-1])
    assert fields["attempted"] == "false"
    assert fields["consent_state"] == "accepted"
    assert fields["status"] == "user_data_missing"


def test_capi_not_new_does_not_log_attempt(app, ctx, monkeypatch, caplog):
    _enable_capi(monkeypatch)
    with app.app_context(), caplog.at_level(logging.INFO, logger=capi.logger.name):
        result = capi.try_send_growth_meta_capi_if_new({"created": False, "event": _fake_event()})
    assert result["status"] == capi.STATUS_NOT_NEW
    assert result["attempted"] is False
    assert _capi_messages(caplog) == []


def test_capi_logs_omit_sensitive_values(app, ctx, monkeypatch, caplog):
    def _post(*_args, **_kwargs):
        raise Exception(ACCESS_TOKEN)

    _enable_capi(monkeypatch)
    app.config["SECRET_KEY"] = "test-secret-160"
    event = _fake_event()
    with app.app_context():
        token = build_external_event_token(
            event_name=FUNNEL_EVENT_PAGE_VIEW, funnel_event_id=42
        )
    with app.app_context(), _request(app, privacy="v1:accepted", fbp=FBP_OK, fbc=FBC_OK):
        with caplog.at_level(logging.DEBUG, logger=capi.logger.name):
            with patch.object(capi.requests, "post", _post):
                result = capi.try_send_growth_meta_capi(event)
    assert result["status"] == capi.STATUS_INTERNAL_ERROR
    assert result["attempted"] is True
    joined = " ".join(_capi_messages(caplog))
    for secret in (ACCESS_TOKEN, FBP_OK, FBC_OK, token, PIXEL_ID, PUBLIC_BASE, "event_source_url"):
        assert secret not in joined
    assert "?" not in joined
    fields = _log_fields(_capi_messages(caplog)[-1])
    assert fields["status"] == "internal_error"
    assert fields["attempted"] == "true"
    assert fields["consent_state"] == "accepted"
