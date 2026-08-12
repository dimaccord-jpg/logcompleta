"""Testes do dashboard de aquisição — Landing Desktop (Etapa 5)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from app.extensions import db
from app.funnel_event_service import (
    FUNNEL_EVENT_FILE_UPLOADED,
    FUNNEL_EVENT_FREIGHT_CALCULATED,
    FUNNEL_SOURCE_AGENTE_COMPARA,
    FUNNEL_SOURCE_CLEIDE_AUDIT,
    record_funnel_event,
)
from app.models import Lead, User
from app.services.admin_acquisition_dashboard_service import get_acquisition_dashboard_payload
from app.services.lead_acquisition_service import CAMPANHA_ACESSO_DESKTOP, FONTE_LANDING
from tests.conftest import seed_conta_franquia_cliente, seed_usuario

_NOW = datetime(2026, 8, 10, 12, 0, 0)
_DAYS = 30


def _seed_user(email: str, *, created_at: datetime | None = None) -> User:
    conta, franquia = seed_conta_franquia_cliente(slug=f"acq-{email.split('@')[0]}")
    user = seed_usuario(franquia.id, conta.id, email=email)
    if created_at is not None:
        user.created_at = created_at
        db.session.commit()
    return user


_lead_seq = 0


def _make_lead(**kwargs) -> Lead:
    global _lead_seq
    _lead_seq += 1
    defaults = {
        "email": f"lead-{_lead_seq}@empresa.com",
        "acquisition_campaign": CAMPANHA_ACESSO_DESKTOP,
        "acquisition_source": FONTE_LANDING,
        "campaign_captured_at": datetime(2026, 8, 1, 10, 0, 0),
        "followup_count": 0,
    }
    defaults.update(kwargs)
    lead = Lead(**defaults)
    db.session.add(lead)
    db.session.commit()
    return lead


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


def _payload(**kwargs):
    defaults = {"days": _DAYS, "now_utc": _NOW}
    defaults.update(kwargs)
    return get_acquisition_dashboard_payload(**defaults)


def test_empty_state_all_zeros(app):
    with app.app_context():
        payload = _payload()
        assert payload["stages"] == {
            "lead": 0,
            "click": 0,
            "registration": 0,
            "first_use": 0,
            "first_audit": 0,
        }
        assert payload["rates"]["lead_to_click"] == 0.0
        assert payload["rates"]["lead_to_first_audit"] == 0.0
        assert payload["data_quality"]["has_data"] is False
        json.dumps(payload)


def test_cohort_by_campaign_captured_at_only(app):
    with app.app_context():
        _make_lead(email="before@t.com", campaign_captured_at=datetime(2026, 7, 1, 10, 0, 0))
        inside = _make_lead(email="inside@t.com", campaign_captured_at=datetime(2026, 8, 5, 10, 0, 0))
        _make_lead(email="after@t.com", campaign_captured_at=datetime(2026, 8, 20, 10, 0, 0))
        # data_inscricao antiga não deve atrair lead capturado fora do período
        _make_lead(
            email="inscricao-trap@t.com",
            campaign_captured_at=datetime(2026, 7, 1, 10, 0, 0),
            data_inscricao=datetime(2026, 8, 5, 10, 0, 0),
        )

        payload = _payload(days=7, now_utc=datetime(2026, 8, 10, 12, 0, 0))
        assert payload["stages"]["lead"] == 1
        assert inside.id is not None


def test_event_after_period_end_still_counts_in_cohort(app):
    with app.app_context():
        user = _seed_user("late-reg@t.com", created_at=datetime(2026, 8, 2, 9, 0, 0))
        lead = _make_lead(
            email="late-reg@t.com",
            campaign_captured_at=datetime(2026, 7, 20, 10, 0, 0),
            cta_clicked_at=datetime(2026, 7, 21, 10, 0, 0),
            converted_user_id=user.id,
            converted_at=datetime(2026, 8, 2, 9, 0, 0),
        )
        _event(
            user,
            event_name=FUNNEL_EVENT_FILE_UPLOADED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            key="late-up",
            occurred_at=datetime(2026, 8, 2, 10, 0, 0),
        )
        user.first_audit_completed_at = datetime(2026, 8, 2, 11, 0, 0)
        db.session.commit()

        # Coorte julho (últimos 30 dias a partir de 31/jul → inclui 20/jul).
        payload = _payload(days=30, now_utc=datetime(2026, 7, 31, 12, 0, 0))
        assert payload["stages"]["lead"] == 1
        assert payload["stages"]["click"] == 1
        assert payload["stages"]["registration"] == 1
        assert payload["stages"]["first_use"] == 1
        assert payload["stages"]["first_audit"] == 1
        assert lead.converted_at > datetime(2026, 7, 31, 23, 59, 59)


def test_event_before_capture_not_registration(app):
    with app.app_context():
        user = _seed_user("preexist@t.com", created_at=datetime(2026, 1, 10, 9, 0, 0))
        _make_lead(
            email="preexist@t.com",
            campaign_captured_at=datetime(2026, 8, 5, 10, 0, 0),
            cta_clicked_at=datetime(2026, 8, 5, 11, 0, 0),
            converted_user_id=user.id,
            converted_at=datetime(2026, 1, 10, 9, 0, 0),
        )
        _event(
            user,
            event_name=FUNNEL_EVENT_FILE_UPLOADED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            key="pre-up",
            occurred_at=datetime(2026, 2, 1, 10, 0, 0),
        )
        user.first_audit_completed_at = datetime(2026, 2, 2, 10, 0, 0)
        db.session.commit()

        payload = _payload()
        assert payload["stages"]["lead"] == 1
        assert payload["stages"]["click"] == 1
        assert payload["stages"]["registration"] == 0
        assert payload["stages"]["first_use"] == 0
        assert payload["stages"]["first_audit"] == 0


def test_full_funnel_100_percent(app):
    with app.app_context():
        user = _seed_user("full@t.com", created_at=datetime(2026, 8, 2, 9, 0, 0))
        _make_lead(
            email="full@t.com",
            campaign_captured_at=datetime(2026, 8, 1, 10, 0, 0),
            cta_clicked_at=datetime(2026, 8, 1, 12, 0, 0),
            converted_user_id=user.id,
            converted_at=datetime(2026, 8, 2, 9, 0, 0),
        )
        _event(
            user,
            event_name=FUNNEL_EVENT_FILE_UPLOADED,
            source=FUNNEL_SOURCE_AGENTE_COMPARA,
            key="full-up",
            occurred_at=datetime(2026, 8, 2, 10, 0, 0),
        )
        user.first_audit_completed_at = datetime(2026, 8, 2, 11, 0, 0)
        db.session.commit()

        payload = _payload()
        assert payload["stages"] == {
            "lead": 1,
            "click": 1,
            "registration": 1,
            "first_use": 1,
            "first_audit": 1,
        }
        assert payload["rates"]["lead_to_click"] == 1.0
        assert payload["rates"]["click_to_registration"] == 1.0
        assert payload["rates"]["registration_to_first_use"] == 1.0
        assert payload["rates"]["first_use_to_first_audit"] == 1.0
        assert payload["rates"]["lead_to_first_audit"] == 1.0


def test_dropoff_each_stage_monotonic(app):
    with app.app_context():
        # Lead sem click
        _make_lead(email="no-click@t.com", campaign_captured_at=datetime(2026, 8, 1, 10, 0, 0))

        # Click sem cadastro
        _make_lead(
            email="click-only@t.com",
            campaign_captured_at=datetime(2026, 8, 1, 11, 0, 0),
            cta_clicked_at=datetime(2026, 8, 1, 12, 0, 0),
        )

        # Cadastro sem uso
        u_reg = _seed_user("reg-only@t.com", created_at=datetime(2026, 8, 2, 9, 0, 0))
        _make_lead(
            email="reg-only@t.com",
            campaign_captured_at=datetime(2026, 8, 1, 13, 0, 0),
            cta_clicked_at=datetime(2026, 8, 1, 14, 0, 0),
            converted_user_id=u_reg.id,
            converted_at=datetime(2026, 8, 2, 9, 0, 0),
        )

        # Uso sem auditoria
        u_use = _seed_user("use-only@t.com", created_at=datetime(2026, 8, 2, 9, 0, 0))
        _make_lead(
            email="use-only@t.com",
            campaign_captured_at=datetime(2026, 8, 1, 15, 0, 0),
            cta_clicked_at=datetime(2026, 8, 1, 16, 0, 0),
            converted_user_id=u_use.id,
            converted_at=datetime(2026, 8, 2, 9, 0, 0),
        )
        _event(
            u_use,
            event_name=FUNNEL_EVENT_FILE_UPLOADED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            key="use-only-up",
            occurred_at=datetime(2026, 8, 3, 10, 0, 0),
        )

        # Completo
        u_full = _seed_user("drop-full@t.com", created_at=datetime(2026, 8, 2, 9, 0, 0))
        _make_lead(
            email="drop-full@t.com",
            campaign_captured_at=datetime(2026, 8, 1, 17, 0, 0),
            cta_clicked_at=datetime(2026, 8, 1, 18, 0, 0),
            converted_user_id=u_full.id,
            converted_at=datetime(2026, 8, 2, 9, 0, 0),
        )
        _event(
            u_full,
            event_name=FUNNEL_EVENT_FILE_UPLOADED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            key="drop-full-up",
            occurred_at=datetime(2026, 8, 3, 10, 0, 0),
        )
        u_full.first_audit_completed_at = datetime(2026, 8, 3, 11, 0, 0)
        db.session.commit()

        payload = _payload()
        s = payload["stages"]
        assert s == {
            "lead": 5,
            "click": 4,
            "registration": 3,
            "first_use": 2,
            "first_audit": 1,
        }
        assert s["lead"] >= s["click"] >= s["registration"] >= s["first_use"] >= s["first_audit"]


def test_registration_without_click_excluded(app):
    with app.app_context():
        user = _seed_user("noclick-reg@t.com", created_at=datetime(2026, 8, 2, 9, 0, 0))
        _make_lead(
            email="noclick-reg@t.com",
            campaign_captured_at=datetime(2026, 8, 1, 10, 0, 0),
            cta_clicked_at=None,
            converted_user_id=user.id,
            converted_at=datetime(2026, 8, 2, 9, 0, 0),
        )
        payload = _payload()
        assert payload["stages"]["lead"] == 1
        assert payload["stages"]["click"] == 0
        assert payload["stages"]["registration"] == 0


def test_multiple_uploads_count_once(app):
    with app.app_context():
        user = _seed_user("multi-up@t.com", created_at=datetime(2026, 8, 2, 9, 0, 0))
        _make_lead(
            email="multi-up@t.com",
            campaign_captured_at=datetime(2026, 8, 1, 10, 0, 0),
            cta_clicked_at=datetime(2026, 8, 1, 11, 0, 0),
            converted_user_id=user.id,
            converted_at=datetime(2026, 8, 2, 9, 0, 0),
        )
        for i in range(5):
            _event(
                user,
                event_name=FUNNEL_EVENT_FILE_UPLOADED,
                source=FUNNEL_SOURCE_CLEIDE_AUDIT,
                key=f"multi-up-{i}",
                occurred_at=datetime(2026, 8, 3, 10, 0, 0) + timedelta(hours=i),
            )
        db.session.commit()
        payload = _payload()
        assert payload["stages"]["first_use"] == 1


def test_both_sources_count_as_first_use(app):
    with app.app_context():
        u1 = _seed_user("src-cleide@t.com", created_at=datetime(2026, 8, 2, 9, 0, 0))
        u2 = _seed_user("src-ac@t.com", created_at=datetime(2026, 8, 2, 9, 0, 0))
        _make_lead(
            email="src-cleide@t.com",
            campaign_captured_at=datetime(2026, 8, 1, 10, 0, 0),
            cta_clicked_at=datetime(2026, 8, 1, 11, 0, 0),
            converted_user_id=u1.id,
            converted_at=datetime(2026, 8, 2, 9, 0, 0),
        )
        _make_lead(
            email="src-ac@t.com",
            campaign_captured_at=datetime(2026, 8, 1, 12, 0, 0),
            cta_clicked_at=datetime(2026, 8, 1, 13, 0, 0),
            converted_user_id=u2.id,
            converted_at=datetime(2026, 8, 2, 9, 0, 0),
        )
        _event(
            u1,
            event_name=FUNNEL_EVENT_FILE_UPLOADED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            key="src-cleide-up",
            occurred_at=datetime(2026, 8, 3, 10, 0, 0),
        )
        _event(
            u2,
            event_name=FUNNEL_EVENT_FILE_UPLOADED,
            source=FUNNEL_SOURCE_AGENTE_COMPARA,
            key="src-ac-up",
            occurred_at=datetime(2026, 8, 3, 11, 0, 0),
        )
        db.session.commit()
        payload = _payload()
        assert payload["stages"]["first_use"] == 2


def test_freight_calculated_alone_is_not_first_use(app):
    with app.app_context():
        user = _seed_user("freight-only@t.com", created_at=datetime(2026, 8, 2, 9, 0, 0))
        _make_lead(
            email="freight-only@t.com",
            campaign_captured_at=datetime(2026, 8, 1, 10, 0, 0),
            cta_clicked_at=datetime(2026, 8, 1, 11, 0, 0),
            converted_user_id=user.id,
            converted_at=datetime(2026, 8, 2, 9, 0, 0),
        )
        _event(
            user,
            event_name=FUNNEL_EVENT_FREIGHT_CALCULATED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            key="freight-only",
            occurred_at=datetime(2026, 8, 3, 10, 0, 0),
        )
        db.session.commit()
        payload = _payload()
        assert payload["stages"]["registration"] == 1
        assert payload["stages"]["first_use"] == 0


def test_other_campaign_excluded(app):
    with app.app_context():
        _make_lead(
            email="other-camp@t.com",
            acquisition_campaign="newsletter_promo",
            campaign_captured_at=datetime(2026, 8, 1, 10, 0, 0),
            cta_clicked_at=datetime(2026, 8, 1, 11, 0, 0),
        )
        payload = _payload()
        assert payload["stages"]["lead"] == 0


def test_opt_out_does_not_remove_historical_metrics(app):
    with app.app_context():
        user = _seed_user("optout@t.com", created_at=datetime(2026, 8, 2, 9, 0, 0))
        _make_lead(
            email="optout@t.com",
            campaign_captured_at=datetime(2026, 8, 1, 10, 0, 0),
            cta_clicked_at=datetime(2026, 8, 1, 11, 0, 0),
            converted_user_id=user.id,
            converted_at=datetime(2026, 8, 2, 9, 0, 0),
            opt_out_at=datetime(2026, 8, 4, 10, 0, 0),
        )
        _event(
            user,
            event_name=FUNNEL_EVENT_FILE_UPLOADED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            key="optout-up",
            occurred_at=datetime(2026, 8, 3, 10, 0, 0),
        )
        user.first_audit_completed_at = datetime(2026, 8, 3, 11, 0, 0)
        db.session.commit()
        payload = _payload()
        assert payload["stages"]["first_audit"] == 1


def test_followup_fields_do_not_affect_stages(app):
    with app.app_context():
        _make_lead(
            email="follow@t.com",
            campaign_captured_at=datetime(2026, 8, 1, 10, 0, 0),
            cta_clicked_at=datetime(2026, 8, 1, 11, 0, 0),
            followup_count=3,
            last_followup_sent_at=datetime(2026, 8, 5, 10, 0, 0),
        )
        payload = _payload()
        assert payload["stages"]["lead"] == 1
        assert payload["stages"]["click"] == 1
        assert payload["stages"]["registration"] == 0


def test_rates_zero_half_full_and_fractional(app):
    with app.app_context():
        # 2 leads, 1 click → 50%; registration/first_use/audit 0 → 0% com denom zero safe
        _make_lead(email="rate-a@t.com", campaign_captured_at=datetime(2026, 8, 1, 10, 0, 0))
        _make_lead(
            email="rate-b@t.com",
            campaign_captured_at=datetime(2026, 8, 1, 11, 0, 0),
            cta_clicked_at=datetime(2026, 8, 1, 12, 0, 0),
        )
        payload = _payload()
        assert payload["rates"]["lead_to_click"] == 0.5
        assert payload["rates"]["click_to_registration"] == 0.0
        assert payload["rates"]["registration_to_first_use"] == 0.0
        assert payload["rates"]["first_use_to_first_audit"] == 0.0

        # 3 leads / 1 click → ~33.3%
        _make_lead(email="rate-c@t.com", campaign_captured_at=datetime(2026, 8, 1, 13, 0, 0))
        payload2 = _payload()
        assert abs(payload2["rates"]["lead_to_click"] - (1 / 3)) < 1e-9


def test_click_before_capture_excluded(app):
    with app.app_context():
        _make_lead(
            email="click-before@t.com",
            campaign_captured_at=datetime(2026, 8, 5, 10, 0, 0),
            cta_clicked_at=datetime(2026, 8, 1, 10, 0, 0),
        )
        payload = _payload()
        assert payload["stages"]["lead"] == 1
        assert payload["stages"]["click"] == 0


def test_campaign_argument_ready_for_future(app):
    with app.app_context():
        _make_lead(
            email="camp-arg@t.com",
            acquisition_campaign="future_campaign",
            campaign_captured_at=datetime(2026, 8, 1, 10, 0, 0),
        )
        default_payload = _payload()
        other_payload = _payload(campaign="future_campaign")
        assert default_payload["stages"]["lead"] == 0
        assert other_payload["campaign"] == "future_campaign"
        assert other_payload["stages"]["lead"] == 1
