"""SCRUM-149 lote 2: aquisição first-party e qualidade na tela /admin/growth."""
from __future__ import annotations

import json
from datetime import datetime

from app.extensions import db
from app.funnel_event_service import (
    FUNNEL_EVENT_CHECKOUT_STARTED,
    FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
    FUNNEL_EVENT_PAGE_VIEW,
    FUNNEL_EVENT_PAID,
    FUNNEL_EVENT_PLAN_SELECTED,
    FUNNEL_EVENT_SESSION_ORIGIN_OBSERVED,
    FUNNEL_EVENT_SIGNUP_COMPLETED,
    TASK_TYPE_CLEIDE_AUDIT,
)
from app.models import FunnelEvent
from app.services import admin_growth_dashboard_service as growth_dashboard
from app.services.admin_growth_dashboard_service import (
    CURRENT_SESSION_ACQUISITION_EVENTS,
    FIRST_TOUCH_ACQUISITION_EVENTS,
    NOT_RECORDED_LABEL,
    get_admin_growth_dashboard_payload,
)
from app.services.growth_tracking_quality_service import (
    get_growth_tracking_quality_payload,
)
from tests.test_admin_growth_dashboard import (
    NOW,
    _build_admin_client,
    _event,
    _login,
    _metric,
    _seed_user,
    _stage,
)

INSIDE = datetime(2026, 9, 27, 12, 0, 0)
OUTSIDE = datetime(2026, 8, 1, 12, 0, 0)
GCLID_SECRET = "gclidvalor149lote"
FBCLID_SECRET = "fbclidvalor149lote"


def _touch(payload: dict, key: str) -> dict:
    return next(item for item in payload["acquisition"]["touches"] if item["key"] == key)


def _row(touch: dict, source: str, medium: str, campaign: str) -> dict:
    return next(
        item
        for item in touch["rows"]
        if item["source"] == source
        and item["medium"] == medium
        and item["campaign"] == campaign
    )


def _summary(touch: dict, label: str) -> dict:
    return next(item for item in touch["summary"] if item["label"] == label)


def _quality_item(payload: dict, key: str) -> dict:
    for block in payload["tracking_quality"]["blocks"]:
        for item in block.get("items") or []:
            if item.get("key") == key:
                return item
    raise AssertionError(key)


def _origin(**fields: str) -> dict[str, str]:
    return dict(fields)


def _signup_metadata(
    *,
    method: str = "password",
    first: dict | None = None,
    current: dict | None = None,
) -> dict:
    metadata: dict = {"signup_method": method}
    attribution = {}
    if first is not None:
        attribution["first_touch"] = first
    if current is not None:
        attribution["current_session_origin"] = current
    if attribution:
        metadata["growth_attribution"] = attribution
    return metadata


def test_acquisition_events_do_not_include_paid_or_page_view():
    excluded = {FUNNEL_EVENT_PAID, FUNNEL_EVENT_PAGE_VIEW}
    assert excluded.isdisjoint(FIRST_TOUCH_ACQUISITION_EVENTS)
    assert excluded.isdisjoint(CURRENT_SESSION_ACQUISITION_EVENTS)
    assert FUNNEL_EVENT_SESSION_ORIGIN_OBSERVED not in FIRST_TOUCH_ACQUISITION_EVENTS
    assert FUNNEL_EVENT_SIGNUP_COMPLETED in FIRST_TOUCH_ACQUISITION_EVENTS
    assert FUNNEL_EVENT_PLAN_SELECTED in CURRENT_SESSION_ACQUISITION_EVENTS
    assert FUNNEL_EVENT_CHECKOUT_STARTED in CURRENT_SESSION_ACQUISITION_EVENTS


def test_first_touch_and_current_session_stay_separate(app, monkeypatch):
    with app.app_context():
        alice, conta_alice = _seed_user("acq-alice@test.com", "conta-acq-alice")
        bruno, _conta_bruno = _seed_user("acq-bruno@test.com", "conta-acq-bruno")
        carla, _conta_carla = _seed_user("acq-carla@test.com", "conta-acq-carla")
        fabio, _conta_fabio = _seed_user("acq-fabio@test.com", "conta-acq-fabio")
        eduardo, _conta_eduardo = _seed_user("acq-eduardo@test.com", "conta-acq-eduardo")
        diego, _conta_diego = _seed_user("acq-diego@test.com", "conta-acq-diego")
        olga, _conta_olga = _seed_user("acq-olga@test.com", "conta-acq-olga")

        _event(
            event_name=FUNNEL_EVENT_SIGNUP_COMPLETED,
            key="acq-signup-alice",
            occurred_at=INSIDE,
            user_id=alice.id,
            metadata_json=_signup_metadata(
                first=_origin(source="google", medium="cpc", campaign="brand"),
                current=_origin(source="newsletter", medium="email", campaign="sept"),
            ),
        )
        _event(
            event_name=FUNNEL_EVENT_SIGNUP_COMPLETED,
            key="acq-signup-bruno",
            occurred_at=INSIDE,
            user_id=bruno.id,
            metadata_json=_signup_metadata(
                first=_origin(source="google", medium="cpc", campaign="brand"),
                current=_origin(source="google", medium="cpc", campaign="brand"),
            ),
        )
        _event(
            event_name=FUNNEL_EVENT_SIGNUP_COMPLETED,
            key="acq-signup-fabio",
            occurred_at=INSIDE,
            user_id=fabio.id,
            metadata_json=_signup_metadata(
                first=_origin(source="google", medium="cpc", campaign="generic"),
            ),
        )
        _event(
            event_name=FUNNEL_EVENT_SIGNUP_COMPLETED,
            key="acq-signup-eduardo",
            occurred_at=INSIDE,
            user_id=eduardo.id,
            metadata_json=_signup_metadata(first=_origin(source="google")),
        )
        _event(
            event_name=FUNNEL_EVENT_SIGNUP_COMPLETED,
            key="acq-signup-carla",
            occurred_at=INSIDE,
            user_id=carla.id,
            metadata_json=_signup_metadata(),
        )
        _event(
            event_name=FUNNEL_EVENT_SIGNUP_COMPLETED,
            key="acq-signup-olga-outside",
            occurred_at=OUTSIDE,
            user_id=olga.id,
            metadata_json=_signup_metadata(
                first=_origin(source="oldcampaign", medium="cpc", campaign="old"),
            ),
        )
        _event(
            event_name=FUNNEL_EVENT_PLAN_SELECTED,
            key="acq-plan-alice",
            occurred_at=INSIDE,
            user_id=alice.id,
            metadata_json={
                "plan": "pro",
                "growth_attribution": {
                    "current_session_origin": _origin(
                        source="google", medium="cpc", campaign="brand"
                    ),
                },
            },
        )
        _event(
            event_name=FUNNEL_EVENT_CHECKOUT_STARTED,
            key="acq-checkout-alice",
            occurred_at=INSIDE,
            user_id=alice.id,
            conta_id=conta_alice.id,
            metadata_json={
                "plan": "pro",
                "growth_attribution": {
                    "current_session_origin": _origin(
                        source="google", medium="cpc", campaign="brand"
                    ),
                },
            },
        )
        _event(
            event_name=FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
            key="acq-first-alice",
            occurred_at=INSIDE,
            user_id=alice.id,
            metadata_json={
                "task_type": TASK_TYPE_CLEIDE_AUDIT,
                "growth_attribution": {
                    "current_session_origin": _origin(
                        source="meta", medium="paid", campaign="retarget"
                    ),
                },
            },
        )
        _event(
            event_name=FUNNEL_EVENT_SESSION_ORIGIN_OBSERVED,
            key="acq-session-diego",
            occurred_at=INSIDE,
            user_id=diego.id,
            metadata_json={
                "growth_attribution": {
                    "current_session_origin": _origin(
                        source="meta", medium="paid", campaign="retarget"
                    ),
                },
            },
        )
        _event(
            event_name=FUNNEL_EVENT_PAID,
            key="acq-paid-alice",
            occurred_at=INSIDE,
            conta_id=conta_alice.id,
            metadata_json={"plan": "pro"},
        )
        _event(
            event_name=FUNNEL_EVENT_PAGE_VIEW,
            key="acq-page",
            occurred_at=INSIDE,
            metadata_json={"page": "index"},
        )
        db.session.commit()

        def forbidden_all(*_args, **_kwargs):
            raise AssertionError("FunnelEvent.query.all não deve agregar a aquisição")

        monkeypatch.setattr(FunnelEvent.query.__class__, "all", forbidden_all)
        calls: list[tuple[datetime, datetime]] = []
        real_quality = get_growth_tracking_quality_payload

        def spy_quality(*, start, end):
            calls.append((start, end))
            return real_quality(start=start, end=end)

        monkeypatch.setattr(
            growth_dashboard,
            "get_growth_tracking_quality_payload",
            spy_quality,
        )
        payload = get_admin_growth_dashboard_payload(days=30, now_utc=NOW)

        assert payload["service_failed"] is False
        assert _metric(_stage(payload, "paid"), "distinct_contas") == 1
        assert _metric(_stage(payload, "page_view"), "occurrences") == 1
        assert len(calls) == 1
        assert calls[0][0].isoformat() == payload["period"]["start_utc"]
        assert calls[0][1].isoformat() == payload["period"]["end_utc"]

        first = _touch(payload, "first_touch")
        current = _touch(payload, "current_session_origin")
        assert [column["key"] for column in first["columns"]] == ["signup_users"]
        assert [column["key"] for column in current["columns"]] == [
            "signup_users",
            "first_relevant_users",
            "plan_users",
            "checkout_contas",
            "session_observations",
        ]
        assert "checkout_contas" not in first["rows"][0]

        brand = _row(first, "google", "cpc", "brand")
        generic = _row(first, "google", "cpc", "generic")
        partial = _row(first, "google", NOT_RECORDED_LABEL, NOT_RECORDED_LABEL)
        missing_first = _row(first, NOT_RECORDED_LABEL, NOT_RECORDED_LABEL, NOT_RECORDED_LABEL)
        assert brand["signup_users"] == 2
        assert generic["signup_users"] == 1
        assert partial["signup_users"] == 1
        assert missing_first["signup_users"] == 1
        assert _summary(first, "Cadastros") == {
            "label": "Cadastros",
            "unit": "usuários",
            "known_source": 4,
            "not_recorded": 1,
        }
        first_sources = {row["source"] for row in first["rows"]}
        assert "newsletter" not in first_sources
        assert "meta" not in first_sources
        assert "oldcampaign" not in first_sources
        assert "direct" not in first_sources

        current_brand = _row(current, "google", "cpc", "brand")
        assert current_brand["signup_users"] == 1
        assert current_brand["plan_users"] == 1
        assert current_brand["checkout_contas"] == 1
        assert current_brand["first_relevant_users"] == 0
        assert current_brand["session_observations"] == 0
        newsletter = _row(current, "newsletter", "email", "sept")
        assert newsletter["signup_users"] == 1
        assert newsletter["plan_users"] == 0
        assert newsletter["checkout_contas"] == 0
        retarget = _row(current, "meta", "paid", "retarget")
        assert retarget["first_relevant_users"] == 1
        assert retarget["session_observations"] == 1
        assert retarget["signup_users"] == 0
        missing_current = _row(
            current, NOT_RECORDED_LABEL, NOT_RECORDED_LABEL, NOT_RECORDED_LABEL
        )
        assert missing_current["signup_users"] == 3
        assert missing_current["checkout_contas"] == 0
        assert not any(
            row["source"] == "google" and row["medium"] == NOT_RECORDED_LABEL
            for row in current["rows"]
        )
        assert not any(row["source"] == "generic" for row in current["rows"])
        assert not any(row["campaign"] == "old" for row in current["rows"])
        assert sum(row["checkout_contas"] for row in current["rows"]) == 1
        for touch in (first, current):
            for row in touch["rows"]:
                assert row["source"] != "direct"
                assert row["medium"] != "direct"
                assert row["campaign"] != "direct"
                assert "direct" not in row["source"].lower()

        confirmation = payload["tracking_quality"]["external_confirmation"]
        assert confirmation["historical_database_metrics_available"] is False
        assert confirmation["kind"] == "unavailable"
        assert confirmation["value"] is None
        assert confirmation["display"] == "Confirmação externa histórica indisponível"
        assert _quality_item(payload, "current_session_origin.origin_not_recorded_count")[
            "value"
        ] >= 1
        rendered = json.dumps(payload["tracking_quality"], ensure_ascii=False)
        for phrase in ("Meta recebeu", "GA4 recebeu", "Meta registrou", "Google confirmou"):
            assert phrase not in rendered


def test_missing_origin_is_not_recorded_and_click_ids_stay_out_of_dimensions(app):
    with app.app_context():
        user, conta = _seed_user("acq-click@test.com", "conta-acq-click")
        _event(
            event_name=FUNNEL_EVENT_SIGNUP_COMPLETED,
            key="acq-click-signup",
            occurred_at=INSIDE,
            user_id=user.id,
            metadata_json=_signup_metadata(
                first=_origin(
                    source="google",
                    medium="cpc",
                    campaign="brand",
                    gclid=GCLID_SECRET,
                ),
            ),
        )
        _event(
            event_name=FUNNEL_EVENT_SESSION_ORIGIN_OBSERVED,
            key="acq-click-session",
            occurred_at=INSIDE,
            user_id=user.id,
            metadata_json={
                "growth_attribution": {
                    "current_session_origin": {"fbclid": FBCLID_SECRET},
                },
            },
        )
        _event(
            event_name=FUNNEL_EVENT_CHECKOUT_STARTED,
            key="acq-click-checkout-empty",
            occurred_at=INSIDE,
            conta_id=conta.id,
            metadata_json={"plan": "starter"},
        )
        db.session.commit()

        payload = get_admin_growth_dashboard_payload(days=30, now_utc=NOW)
        encoded = json.dumps(payload, ensure_ascii=False)
        assert GCLID_SECRET not in encoded
        assert FBCLID_SECRET not in encoded

        first = _touch(payload, "first_touch")
        current = _touch(payload, "current_session_origin")
        assert _row(first, "google", "cpc", "brand")["signup_users"] == 1
        assert set(first["rows"][0]) == {"source", "medium", "campaign", "signup_users"}
        click_only = _row(current, NOT_RECORDED_LABEL, NOT_RECORDED_LABEL, NOT_RECORDED_LABEL)
        assert click_only["session_observations"] == 1
        assert click_only["checkout_contas"] == 1
        assert click_only["source"] == NOT_RECORDED_LABEL
        assert _quality_item(payload, "first_touch.gclid_present_count")["value"] == 1
        assert _quality_item(payload, "current_session_origin.fbclid_present_count")["value"] == 1
        assert "gclid" not in click_only


def test_external_confirmation_unavailable_is_not_zero(app, monkeypatch):
    def stub(*, start, end):
        assert start < end
        return {
            "internal": {"event_counts": {"page_view": 3, "paid": 0}},
            "attribution": {},
            "external_candidates": {
                "meta_browser_capi": {
                    "external_candidate_count": 4,
                    "by_event_name": {
                        "page_view": {"external_candidate_count": 4},
                    },
                },
                "ga4_browser": {
                    "external_candidate_count": 2,
                    "by_event_name": {
                        "page_view": {"external_candidate_count": 2},
                    },
                },
            },
            "first_relevant_quality": {
                "eligible_completion_count": 0,
                "notes": ["gap_is_investigable_not_confirmed_bug"],
            },
            "meta_capi": {
                "source": "process_logs",
                "historical_database_metrics_available": False,
            },
            "known_limitations": ["external_candidate_does_not_imply_dispatch"],
        }

    monkeypatch.setattr(
        growth_dashboard,
        "get_growth_tracking_quality_payload",
        stub,
    )
    with app.app_context():
        payload = get_admin_growth_dashboard_payload(days=7, now_utc=NOW)

    confirmation = payload["tracking_quality"]["external_confirmation"]
    assert confirmation["historical_database_metrics_available"] is False
    assert confirmation["value"] is None
    assert confirmation["kind"] == "unavailable"
    assert "indisponível" in confirmation["display"]
    assert _quality_item(payload, "candidates.meta_browser_capi")["value"] == 4
    assert _quality_item(payload, "candidates.ga4_browser")["value"] == 2
    assert "Candidatos Meta browser" in _quality_item(
        payload, "candidates.meta_browser_capi"
    )["label"]
    assert "Candidatos Meta CAPI" in _quality_item(
        payload, "candidates.meta_browser_capi"
    )["label"]
    assert _quality_item(payload, "internal.page_view")["value"] == 3
    assert _quality_item(payload, "internal.paid")["value"] == 0
    assert "Não dá para afirmar discrepância" in payload["tracking_quality"]["discrepancy_note"]


def test_empty_period_measures_zero_and_keeps_confirmation_unavailable(app):
    with app.app_context():
        payload = get_admin_growth_dashboard_payload(days=30, now_utc=NOW)
        assert payload["has_data"] is False
        assert payload["acquisition"]["available"] is True
        first = _touch(payload, "first_touch")
        assert first["rows"] == []
        assert _summary(first, "Cadastros")["known_source"] == 0
        assert _summary(first, "Cadastros")["not_recorded"] == 0
        confirmation = payload["tracking_quality"]["external_confirmation"]
        assert confirmation["value"] is None
        assert _quality_item(payload, "internal.page_view")["value"] == 0
        assert _quality_item(payload, "candidates.ga4_browser")["value"] == 0


def test_section_failure_does_not_drop_the_funnel(app, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("leitura indisponivel")

    monkeypatch.setattr(growth_dashboard, "_build_acquisition", boom)
    monkeypatch.setattr(growth_dashboard, "get_growth_tracking_quality_payload", boom)
    with app.app_context():
        payload = get_admin_growth_dashboard_payload(days=30, now_utc=NOW)
        assert payload["service_failed"] is False
        assert _metric(_stage(payload, "page_view"), "occurrences") == 0
        assert payload["acquisition"]["available"] is False
        assert payload["acquisition"]["touches"] == []
        quality = payload["tracking_quality"]
        assert quality["available"] is False
        assert quality["external_confirmation"]["value"] is None
        assert quality["blocks"] == []


def test_admin_growth_route_renders_acquisition_and_quality(app):
    with app.app_context():
        admin, _conta = _seed_user("acq-admin@test.com", "conta-acq-admin")
        admin.is_admin = True
        db.session.add(admin)
        outsider, _conta_out = _seed_user("acq-outsider@test.com", "conta-acq-outsider")
        from datetime import timedelta

        from app.models import utcnow_naive

        moment = utcnow_naive() - timedelta(minutes=1)
        _event(
            event_name=FUNNEL_EVENT_PAGE_VIEW,
            key="acq-route-page",
            occurred_at=moment,
            metadata_json={"page": "index"},
        )
        _event(
            event_name=FUNNEL_EVENT_SIGNUP_COMPLETED,
            key="acq-route-signup",
            occurred_at=moment,
            user_id=outsider.id,
            metadata_json=_signup_metadata(),
        )
        db.session.commit()
        admin_id = admin.id
        outsider_id = outsider.id
        client = _build_admin_client(app)

        anonymous = client.get("/admin/growth")
        assert anonymous.status_code == 302

        _login(client, outsider_id)
        denied = client.get("/admin/growth")
        assert denied.status_code == 403

        _login(client, admin_id)
        response = client.get("/admin/growth?days=7")
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert "Page views — ocorrências" in html
        assert 'data-growth-section="acquisition"' in html
        assert 'data-growth-touch="first_touch"' in html
        assert 'data-growth-touch="current_session_origin"' in html
        assert "<td>Não registrado</td>" in html
        assert 'data-growth-section="tracking-quality"' in html
        assert "Candidatos Meta browser" in html
        assert "Candidatos Meta CAPI" in html
        assert "Candidatos GA4 browser" in html
        assert "Origem não registrada" in html
        assert 'data-growth-external-confirmation="unavailable"' in html
        marker = html.index('data-growth-external-confirmation="unavailable"')
        snippet = html[marker:marker + 280]
        assert "Confirmação externa histórica indisponível" in snippet
        assert ">0<" not in snippet
        folded = html.lower()
        for phrase in ("meta recebeu", "ga4 recebeu", "meta registrou", "google confirmou"):
            assert phrase not in folded
        assert 'href="/admin/growth"' in html
