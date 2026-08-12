"""
Dashboard de aquisição — Landing Desktop (coorte por captura do Lead).

Independente do bloco operacional de Conversões (FunnelEvent por período de evento).
Aqui o período define QUEM entrou na coorte via Lead.campaign_captured_at;
etapas posteriores acompanham a mesma coorte sem recortar pelo fim do período.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.funnel_event_service import (
    ALLOWED_FUNNEL_SOURCES,
    FUNNEL_EVENT_FILE_UPLOADED,
)
from app.models import FunnelEvent, Lead, User, utcnow_naive
from app.services.lead_acquisition_service import CAMPANHA_ACESSO_DESKTOP

_ALLOWED_DAYS = {7, 30, 90}


def _normalize_days(days) -> int:
    try:
        value = int(days)
    except (TypeError, ValueError):
        return 30
    return value if value in _ALLOWED_DAYS else 30


def _normalize_campaign(campaign: str | None) -> str:
    value = (campaign or CAMPANHA_ACESSO_DESKTOP).strip()
    return value or CAMPANHA_ACESSO_DESKTOP


def _safe_rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return float(numerator) / float(denominator)


def _to_iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _empty_payload(*, campaign: str, days: int, start_utc: datetime, end_utc: datetime) -> dict:
    return {
        "campaign": campaign,
        "period": {
            "start_utc": _to_iso(start_utc),
            "end_utc": _to_iso(end_utc),
            "label": f"Ultimos {days} dias",
            "days": days,
        },
        "stages": {
            "lead": 0,
            "click": 0,
            "registration": 0,
            "first_use": 0,
            "first_audit": 0,
        },
        "rates": {
            "lead_to_click": 0.0,
            "click_to_registration": 0.0,
            "registration_to_first_use": 0.0,
            "first_use_to_first_audit": 0.0,
            "lead_to_first_audit": 0.0,
        },
        "funnel": [
            {"key": "lead", "label": "Leads", "count": 0, "rate_from_previous": 0.0},
            {"key": "click", "label": "Cliques", "count": 0, "rate_from_previous": 0.0},
            {"key": "registration", "label": "Cadastros", "count": 0, "rate_from_previous": 0.0},
            {"key": "first_use", "label": "Primeiro uso", "count": 0, "rate_from_previous": 0.0},
            {"key": "first_audit", "label": "Primeira auditoria", "count": 0, "rate_from_previous": 0.0},
        ],
        "data_quality": {
            "has_data": False,
            "warnings": [],
            "service_failed": False,
        },
    }


def _cohort_window(*, days: int, now_utc: datetime | None) -> tuple[datetime, datetime]:
    """Mesma janela UTC-naive do filtro de período do dashboard (conversion_days)."""
    end_utc = (now_utc or utcnow_naive()).replace(hour=23, minute=59, second=59, microsecond=0)
    start_utc = (end_utc - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start_utc, end_utc


def get_acquisition_dashboard_payload(
    *,
    campaign: str | None = None,
    days: int = 30,
    now_utc: datetime | None = None,
) -> dict:
    """
    Funil sequencial de aquisição para uma campanha.

    Coorte: Lead.acquisition_campaign + Lead.campaign_captured_at no período.
    First Use: FunnelEvent.file_uploaded (occurred_at) do converted_user_id após Registration.
    First Audit: User.first_audit_completed_at após First Use.

    Não aplica filtro de produto/source operacional.
    """
    campaign = _normalize_campaign(campaign)
    days = _normalize_days(days)
    start_utc, end_utc = _cohort_window(days=days, now_utc=now_utc)
    payload = _empty_payload(campaign=campaign, days=days, start_utc=start_utc, end_utc=end_utc)

    leads = (
        Lead.query.filter(
            Lead.acquisition_campaign == campaign,
            Lead.campaign_captured_at.isnot(None),
            Lead.campaign_captured_at >= start_utc,
            Lead.campaign_captured_at <= end_utc,
        )
        .all()
    )
    if not leads:
        return payload

    # Click: CTA após captura (coerência temporal).
    click_leads = [
        lead
        for lead in leads
        if lead.cta_clicked_at is not None
        and lead.campaign_captured_at is not None
        and lead.cta_clicked_at >= lead.campaign_captured_at
    ]
    click_ids = {lead.id for lead in click_leads}

    # Registration: subconjunto de Click; conta nova após captura (não User pré-existente).
    registration_leads = [
        lead
        for lead in click_leads
        if lead.converted_user_id is not None
        and lead.converted_at is not None
        and lead.campaign_captured_at is not None
        and lead.converted_at >= lead.campaign_captured_at
    ]
    registration_by_user: dict[int, Lead] = {}
    for lead in registration_leads:
        registration_by_user[int(lead.converted_user_id)] = lead

    first_use_user_ids: set[int] = set()
    first_use_at_by_user: dict[int, datetime] = {}
    first_audit_user_ids: set[int] = set()

    if registration_by_user:
        user_ids = list(registration_by_user.keys())
        # Lote: uploads dos users convertidos (sem recortar pelo fim do período).
        upload_events = (
            FunnelEvent.query.filter(
                FunnelEvent.user_id.in_(user_ids),
                FunnelEvent.event_name == FUNNEL_EVENT_FILE_UPLOADED,
                FunnelEvent.source.in_(tuple(ALLOWED_FUNNEL_SOURCES)),
            )
            .all()
        )
        uploads_by_user: dict[int, list[datetime]] = {}
        for event in upload_events:
            if event.user_id is None or event.occurred_at is None:
                continue
            uploads_by_user.setdefault(int(event.user_id), []).append(event.occurred_at)

        for user_id, lead in registration_by_user.items():
            converted_at = lead.converted_at
            if converted_at is None:
                continue
            # Conservador: First Use só após Registration (converted_at).
            qualifying = [ts for ts in uploads_by_user.get(user_id, []) if ts >= converted_at]
            if not qualifying:
                continue
            first_use_user_ids.add(user_id)
            first_use_at_by_user[user_id] = min(qualifying)

        users = User.query.filter(User.id.in_(user_ids)).all()
        user_by_id = {int(user.id): user for user in users}
        for user_id in first_use_user_ids:
            lead = registration_by_user[user_id]
            user = user_by_id.get(user_id)
            if user is None or user.first_audit_completed_at is None:
                continue
            audit_at = user.first_audit_completed_at
            first_use_at = first_use_at_by_user[user_id]
            if (
                lead.campaign_captured_at is not None
                and lead.converted_at is not None
                and audit_at >= lead.campaign_captured_at
                and audit_at >= lead.converted_at
                and audit_at >= first_use_at
            ):
                first_audit_user_ids.add(user_id)

    lead_count = len(leads)
    click_count = len(click_ids)
    registration_count = len(registration_leads)
    first_use_count = len(first_use_user_ids)
    first_audit_count = len(first_audit_user_ids)

    # Garantia monotônica do funil sequencial.
    click_count = min(click_count, lead_count)
    registration_count = min(registration_count, click_count)
    first_use_count = min(first_use_count, registration_count)
    first_audit_count = min(first_audit_count, first_use_count)

    rates = {
        "lead_to_click": _safe_rate(click_count, lead_count),
        "click_to_registration": _safe_rate(registration_count, click_count),
        "registration_to_first_use": _safe_rate(first_use_count, registration_count),
        "first_use_to_first_audit": _safe_rate(first_audit_count, first_use_count),
        "lead_to_first_audit": _safe_rate(first_audit_count, lead_count),
    }
    payload["stages"] = {
        "lead": lead_count,
        "click": click_count,
        "registration": registration_count,
        "first_use": first_use_count,
        "first_audit": first_audit_count,
    }
    payload["rates"] = rates
    payload["funnel"] = [
        {"key": "lead", "label": "Leads", "count": lead_count, "rate_from_previous": 1.0 if lead_count else 0.0},
        {
            "key": "click",
            "label": "Cliques",
            "count": click_count,
            "rate_from_previous": rates["lead_to_click"],
        },
        {
            "key": "registration",
            "label": "Cadastros",
            "count": registration_count,
            "rate_from_previous": rates["click_to_registration"],
        },
        {
            "key": "first_use",
            "label": "Primeiro uso",
            "count": first_use_count,
            "rate_from_previous": rates["registration_to_first_use"],
        },
        {
            "key": "first_audit",
            "label": "Primeira auditoria",
            "count": first_audit_count,
            "rate_from_previous": rates["first_use_to_first_audit"],
        },
    ]
    payload["data_quality"] = {
        "has_data": True,
        "warnings": [],
        "service_failed": False,
    }
    return payload
