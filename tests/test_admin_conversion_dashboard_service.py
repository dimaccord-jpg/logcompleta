from __future__ import annotations

import json
from datetime import datetime

from app.extensions import db
from app.funnel_event_service import (
    FUNNEL_EVENT_FILE_UPLOADED,
    FUNNEL_EVENT_FIRST_AUDIT_COMPLETED,
    FUNNEL_EVENT_FREIGHT_CALCULATED,
    FUNNEL_SOURCE_AGENTE_COMPARA,
    FUNNEL_SOURCE_CLEIDE_AUDIT,
    record_funnel_event,
)
from app.models import FunnelEvent
from app.services.admin_conversion_dashboard_service import get_conversion_dashboard_payload
from tests.conftest import seed_conta_franquia_cliente, seed_usuario


def _seed_user(email: str):
    conta, franquia = seed_conta_franquia_cliente(slug=f"conta-{email.split('@')[0]}")
    user = seed_usuario(franquia.id, conta.id, email=email)
    return user


def _event(user, *, event_name: str, source: str, key: str, occurred_at: datetime):
    return record_funnel_event(
        event_name=event_name,
        source=source,
        user_id=user.id,
        conta_id=user.conta_id,
        franquia_id=user.franquia_id,
        idempotency_key=key,
        occurred_at=occurred_at,
    )


def test_empty_payload_is_safe_and_json_serializable(app):
    with app.app_context():
        payload = get_conversion_dashboard_payload(source="invalid", days="999", now_utc=datetime(2026, 8, 6, 12, 0, 0))
        assert payload["filters"]["source"] == "all"
        assert payload["filters"]["days"] == 30
        assert payload["kpis"]["uploaded_users"] == 0
        assert len(payload["series"]) == 30
        assert payload["data_quality"]["has_data"] is False
        json.dumps(payload)


def test_all_source_deduplicates_users_and_respects_upload_cohort(app):
    with app.app_context():
        user_a = _seed_user("conv-a@test.com")
        user_b = _seed_user("conv-b@test.com")
        user_c = _seed_user("conv-c@test.com")

        _event(user_a, event_name=FUNNEL_EVENT_FILE_UPLOADED, source=FUNNEL_SOURCE_CLEIDE_AUDIT, key="a-up-1", occurred_at=datetime(2026, 8, 2, 12, 0, 0))
        _event(user_a, event_name=FUNNEL_EVENT_FILE_UPLOADED, source=FUNNEL_SOURCE_AGENTE_COMPARA, key="a-up-2", occurred_at=datetime(2026, 8, 2, 13, 0, 0))
        _event(user_a, event_name=FUNNEL_EVENT_FREIGHT_CALCULATED, source=FUNNEL_SOURCE_AGENTE_COMPARA, key="a-comp-1", occurred_at=datetime(2026, 8, 3, 9, 0, 0))
        _event(user_a, event_name=FUNNEL_EVENT_FIRST_AUDIT_COMPLETED, source=FUNNEL_SOURCE_AGENTE_COMPARA, key="a-first-1", occurred_at=datetime(2026, 8, 3, 9, 0, 0))

        _event(user_b, event_name=FUNNEL_EVENT_FREIGHT_CALCULATED, source=FUNNEL_SOURCE_CLEIDE_AUDIT, key="b-comp-before", occurred_at=datetime(2026, 8, 2, 8, 0, 0))
        _event(user_b, event_name=FUNNEL_EVENT_FILE_UPLOADED, source=FUNNEL_SOURCE_CLEIDE_AUDIT, key="b-up-1", occurred_at=datetime(2026, 8, 2, 10, 0, 0))
        _event(user_b, event_name=FUNNEL_EVENT_FREIGHT_CALCULATED, source=FUNNEL_SOURCE_CLEIDE_AUDIT, key="b-comp-after", occurred_at=datetime(2026, 8, 4, 11, 0, 0))

        _event(user_c, event_name=FUNNEL_EVENT_FILE_UPLOADED, source=FUNNEL_SOURCE_CLEIDE_AUDIT, key="c-up-1", occurred_at=datetime(2026, 8, 1, 10, 0, 0))
        _event(user_c, event_name=FUNNEL_EVENT_FILE_UPLOADED, source=FUNNEL_SOURCE_CLEIDE_AUDIT, key="c-up-2", occurred_at=datetime(2026, 8, 1, 11, 0, 0))

        db.session.commit()

        payload = get_conversion_dashboard_payload(source="all", days=7, now_utc=datetime(2026, 8, 6, 12, 0, 0))

        assert payload["kpis"]["uploaded_users"] == 3
        assert payload["kpis"]["completed_users"] == 2
        assert payload["kpis"]["first_audit_users"] == 1
        assert payload["kpis"]["upload_events"] == 5
        assert payload["kpis"]["completion_events"] == 3
        assert payload["kpis"]["abandoned_users"] == 1
        assert payload["kpis"]["completion_rate"] <= 1.0
        assert payload["kpis"]["first_audit_rate"] <= 1.0
        assert payload["funnel"][0]["users"] == 3
        assert payload["funnel"][1]["users"] == 2
        assert payload["funnel"][2]["users"] == 1
        assert payload["data_quality"]["has_data"] is True
        assert all("metadata_json" not in json.dumps(item) for item in payload["series"])


def test_source_filters_and_period_boundaries_work(app):
    with app.app_context():
        user = _seed_user("conv-source@test.com")
        _event(user, event_name=FUNNEL_EVENT_FILE_UPLOADED, source=FUNNEL_SOURCE_CLEIDE_AUDIT, key="source-up-1", occurred_at=datetime(2026, 7, 8, 0, 0, 0))
        _event(user, event_name=FUNNEL_EVENT_FREIGHT_CALCULATED, source=FUNNEL_SOURCE_CLEIDE_AUDIT, key="source-comp-1", occurred_at=datetime(2026, 7, 8, 23, 59, 59))
        _event(user, event_name=FUNNEL_EVENT_FIRST_AUDIT_COMPLETED, source=FUNNEL_SOURCE_AGENTE_COMPARA, key="source-first-ac", occurred_at=datetime(2026, 7, 9, 12, 0, 0))
        db.session.commit()

        cleide = get_conversion_dashboard_payload(source="cleide_audit", days=30, now_utc=datetime(2026, 8, 6, 12, 0, 0))
        ac = get_conversion_dashboard_payload(source="agente_compara", days=30, now_utc=datetime(2026, 8, 6, 12, 0, 0))

        assert cleide["kpis"]["uploaded_users"] == 1
        assert cleide["kpis"]["completed_users"] == 1
        assert cleide["kpis"]["first_audit_users"] == 0
        assert ac["kpis"]["uploaded_users"] == 0
        assert len(cleide["series"]) == 30
        assert cleide["series"][0]["uploaded_users"] == 1


def test_period_is_limited_and_users_are_counted_once_per_day_series(app):
    with app.app_context():
        user = _seed_user("conv-series@test.com")
        _event(user, event_name=FUNNEL_EVENT_FILE_UPLOADED, source=FUNNEL_SOURCE_AGENTE_COMPARA, key="series-up-1", occurred_at=datetime(2026, 8, 5, 3, 0, 0))
        _event(user, event_name=FUNNEL_EVENT_FILE_UPLOADED, source=FUNNEL_SOURCE_AGENTE_COMPARA, key="series-up-2", occurred_at=datetime(2026, 8, 5, 4, 0, 0))
        db.session.commit()

        payload = get_conversion_dashboard_payload(source="agente_compara", days=999, now_utc=datetime(2026, 8, 6, 12, 0, 0))

        assert payload["filters"]["days"] == 30
        assert len(payload["series"]) == 30
        assert sum(item["uploaded_users"] for item in payload["series"]) == 1
        json.dumps(payload)


def test_service_uses_single_query_without_n_plus_one(app, monkeypatch):
    with app.app_context():
        user = _seed_user("conv-query@test.com")
        _event(user, event_name=FUNNEL_EVENT_FILE_UPLOADED, source=FUNNEL_SOURCE_CLEIDE_AUDIT, key="query-up-1", occurred_at=datetime(2026, 8, 6, 12, 0, 0))
        db.session.commit()

        calls = {"count": 0}
        real_all = FunnelEvent.query.__class__.all

        def tracked_all(query):
            calls["count"] += 1
            return real_all(query)

        monkeypatch.setattr(FunnelEvent.query.__class__, "all", tracked_all)
        payload = get_conversion_dashboard_payload(source="all", days=7, now_utc=datetime(2026, 8, 6, 12, 0, 0))
        assert payload["kpis"]["uploaded_users"] == 1
        assert calls["count"] == 1
