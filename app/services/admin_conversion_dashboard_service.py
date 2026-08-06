from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.extensions import db
from app.funnel_event_service import (
    FUNNEL_EVENT_FILE_UPLOADED,
    FUNNEL_EVENT_FIRST_AUDIT_COMPLETED,
    FUNNEL_EVENT_FREIGHT_CALCULATED,
    FUNNEL_SOURCE_AGENTE_COMPARA,
    FUNNEL_SOURCE_CLEIDE_AUDIT,
)
from app.models import FunnelEvent, utcnow_naive

_ALLOWED_SOURCES = {"all", FUNNEL_SOURCE_CLEIDE_AUDIT, FUNNEL_SOURCE_AGENTE_COMPARA}
_ALLOWED_DAYS = {7, 30, 90}
_TZ_SP = ZoneInfo("America/Sao_Paulo")


def _normalize_source(source: str | None) -> str:
    value = str(source or "all").strip().lower()
    return value if value in _ALLOWED_SOURCES else "all"


def _normalize_days(days) -> int:
    try:
        value = int(days)
    except (TypeError, ValueError):
        return 30
    return value if value in _ALLOWED_DAYS else 30


def _safe_rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return max(0.0, min(1.0, float(numerator) / float(denominator)))


def _to_iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _sp_day_label(day_utc: datetime) -> tuple[str, str]:
    local = day_utc.replace(tzinfo=UTC).astimezone(_TZ_SP)
    return local.date().isoformat(), local.strftime("%d/%m")


def _empty_payload(*, source: str, days: int, start_utc: datetime, end_utc: datetime, warning: str | None = None) -> dict:
    warnings = [warning] if warning else []
    series = []
    current = start_utc
    while current <= end_utc:
        day_key, label = _sp_day_label(current)
        series.append(
            {
                "date": day_key,
                "label": label,
                "uploaded_users": 0,
                "completed_users": 0,
                "first_audit_users": 0,
            }
        )
        current += timedelta(days=1)
    return {
        "filters": {
            "source": source,
            "days": days,
            "source_options": [
                {"value": "all", "label": "Todos"},
                {"value": FUNNEL_SOURCE_CLEIDE_AUDIT, "label": "Auditoria"},
                {"value": FUNNEL_SOURCE_AGENTE_COMPARA, "label": "Agente Compara"},
            ],
            "period_options": [
                {"value": 7, "label": "7 dias"},
                {"value": 30, "label": "30 dias"},
                {"value": 90, "label": "90 dias"},
            ],
        },
        "period": {
            "start_utc": _to_iso(start_utc),
            "end_utc": _to_iso(end_utc),
            "label": f"Ultimos {days} dias",
        },
        "kpis": {
            "uploaded_users": 0,
            "completed_users": 0,
            "first_audit_users": 0,
            "completion_rate": 0.0,
            "first_audit_rate": 0.0,
            "abandoned_users": 0,
            "upload_events": 0,
            "completion_events": 0,
        },
        "funnel": [
            {"key": "uploaded", "label": "Upload", "users": 0, "rate": 0.0, "dropoff_users": 0},
            {"key": "completed", "label": "Conclusão", "users": 0, "rate": 0.0, "dropoff_users": 0},
            {"key": "first_audit", "label": "Primeira auditoria", "users": 0, "rate": 0.0, "dropoff_users": 0},
        ],
        "series": series,
        "data_quality": {
            "has_data": False,
            "warnings": warnings,
            "service_failed": False,
        },
    }


def get_conversion_dashboard_payload(*, source: str = "all", days: int = 30, now_utc: datetime | None = None) -> dict:
    source = _normalize_source(source)
    days = _normalize_days(days)
    end_utc = (now_utc or utcnow_naive()).replace(hour=23, minute=59, second=59, microsecond=0)
    start_utc = (end_utc - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    payload = _empty_payload(source=source, days=days, start_utc=start_utc, end_utc=end_utc)

    query = FunnelEvent.query.filter(FunnelEvent.occurred_at >= start_utc, FunnelEvent.occurred_at <= end_utc)
    if source != "all":
        query = query.filter(FunnelEvent.source == source)
    events = query.order_by(FunnelEvent.occurred_at.asc(), FunnelEvent.id.asc()).all()
    if not events:
        payload["data_quality"]["warnings"].append("Nenhum evento de conversão encontrado para os filtros selecionados.")
        return payload

    upload_events = [event for event in events if event.event_name == FUNNEL_EVENT_FILE_UPLOADED and event.user_id is not None]
    completion_events = [event for event in events if event.event_name == FUNNEL_EVENT_FREIGHT_CALCULATED and event.user_id is not None]
    first_audit_events = [event for event in events if event.event_name == FUNNEL_EVENT_FIRST_AUDIT_COMPLETED and event.user_id is not None]

    cohort_first_upload = {}
    for event in upload_events:
        cohort_first_upload.setdefault(int(event.user_id), event)

    completed_users = set()
    first_audit_users = set()
    daily_uploaded = defaultdict(set)
    daily_completed = defaultdict(set)
    daily_first = defaultdict(set)

    for user_id, upload_event in cohort_first_upload.items():
        upload_time = upload_event.occurred_at
        upload_day, _ = _sp_day_label(upload_time)
        daily_uploaded[upload_day].add(user_id)

        matching_completed = [
            event for event in completion_events
            if int(event.user_id) == user_id and event.occurred_at >= upload_time
        ]
        if matching_completed:
            completed_users.add(user_id)
            complete_day, _ = _sp_day_label(matching_completed[0].occurred_at)
            daily_completed[complete_day].add(user_id)

        matching_first = [
            event for event in first_audit_events
            if int(event.user_id) == user_id and event.occurred_at >= upload_time
        ]
        if matching_first:
            first_audit_users.add(user_id)
            first_day, _ = _sp_day_label(matching_first[0].occurred_at)
            daily_first[first_day].add(user_id)

    uploaded_users = len(cohort_first_upload)
    completed_count = len(completed_users)
    first_audit_count = len(first_audit_users)
    payload["kpis"] = {
        "uploaded_users": uploaded_users,
        "completed_users": completed_count,
        "first_audit_users": first_audit_count,
        "completion_rate": _safe_rate(completed_count, uploaded_users),
        "first_audit_rate": _safe_rate(first_audit_count, uploaded_users),
        "abandoned_users": max(0, uploaded_users - completed_count),
        "upload_events": len(upload_events),
        "completion_events": len(completion_events),
    }
    payload["funnel"] = [
        {"key": "uploaded", "label": "Upload", "users": uploaded_users, "rate": 1.0 if uploaded_users else 0.0, "dropoff_users": max(0, uploaded_users - completed_count)},
        {"key": "completed", "label": "Conclusão", "users": completed_count, "rate": _safe_rate(completed_count, uploaded_users), "dropoff_users": max(0, completed_count - first_audit_count)},
        {"key": "first_audit", "label": "Primeira auditoria", "users": first_audit_count, "rate": _safe_rate(first_audit_count, uploaded_users), "dropoff_users": 0},
    ]

    series = []
    current = start_utc
    while current <= end_utc:
        day_key, label = _sp_day_label(current)
        series.append(
            {
                "date": day_key,
                "label": label,
                "uploaded_users": len(daily_uploaded.get(day_key, set())),
                "completed_users": len(daily_completed.get(day_key, set())),
                "first_audit_users": len(daily_first.get(day_key, set())),
            }
        )
        current += timedelta(days=1)
    payload["series"] = series
    payload["data_quality"] = {
        "has_data": True,
        "warnings": [],
        "service_failed": False,
    }
    if not first_audit_events:
        payload["data_quality"]["warnings"].append("Primeira auditoria pode estar subcontada em historico anterior a instrumentacao explicita.")
    if any(event.user_id is None for event in events):
        payload["data_quality"]["warnings"].append("Eventos sem usuario foram desconsiderados nas metricas de pessoas.")
    return payload
