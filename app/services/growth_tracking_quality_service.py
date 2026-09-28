"""Monitor minimo de qualidade de tracking (SCRUM-160).

Somente leitura sobre FunnelEvent. Sem migration, sem persistencia nova,
sem endpoint e sem reconciliacao externa.

Agregacao SQL (COUNT / GROUP BY / EXISTS / CASE). Nao materializa
page_view nem percorre usuarios em Python.

Ausencia de origem e "origin_not_recorded": nao e direct, falha ou
rejeicao de consentimento. Candidato externo nao e envio.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.orm import aliased

from app.extensions import db
from app.funnel_event_service import (
    FIRST_RELEVANT_ELIGIBLE_TASK_TYPES,
    FUNNEL_EVENT_CHECKOUT_STARTED,
    FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
    FUNNEL_EVENT_PAGE_VIEW,
    FUNNEL_EVENT_PAID,
    FUNNEL_EVENT_PLAN_SELECTED,
    FUNNEL_EVENT_SESSION_ORIGIN_OBSERVED,
    FUNNEL_EVENT_SIGNUP_COMPLETED,
    FUNNEL_EVENT_SIGNUP_STARTED,
    FUNNEL_EVENT_TASK_COMPLETED,
    GROWTH_ATTRIBUTION_ALLOWED_EVENTS,
)
from app.models import FunnelEvent
from app.services.growth_attribution_service import CLICK_ID_PARAMS, UTM_ACQUISITION_KEYS
from app.services.growth_external_event_service import EXTERNAL_BROWSER_ALLOWED_EVENTS
from app.services.growth_external_server_service import META_CAPI_ALLOWED_GROWTH_EVENTS

# Contagens internas por evento. Sem taxa entre etapas (unidades distintas).
INTERNAL_MONITOR_EVENT_NAMES = (
    FUNNEL_EVENT_PAGE_VIEW,
    FUNNEL_EVENT_SIGNUP_STARTED,
    FUNNEL_EVENT_SIGNUP_COMPLETED,
    FUNNEL_EVENT_TASK_COMPLETED,
    FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
    FUNNEL_EVENT_PLAN_SELECTED,
    FUNNEL_EVENT_CHECKOUT_STARTED,
    FUNNEL_EVENT_PAID,
)

# first_touch nao e gravado em session_origin_observed (schema current-only).
FIRST_TOUCH_EVENT_NAMES = frozenset(
    GROWTH_ATTRIBUTION_ALLOWED_EVENTS - {FUNNEL_EVENT_SESSION_ORIGIN_OBSERVED}
)
CURRENT_SESSION_EVENT_NAMES = frozenset(GROWTH_ATTRIBUTION_ALLOWED_EVENTS)

# GA4 browser usa o mesmo conjunto do envelope browser (espelha GA4_MAP).
GA4_BROWSER_CANDIDATE_EVENTS = EXTERNAL_BROWSER_ALLOWED_EVENTS
META_BROWSER_CAPI_CANDIDATE_EVENTS = META_CAPI_ALLOWED_GROWTH_EVENTS

_UTM_FIELDS = tuple(sorted(UTM_ACQUISITION_KEYS))
_CLICK_FIELDS = tuple(sorted(CLICK_ID_PARAMS))

_COUNT_KEYS = (
    "applicable_count",
    "with_acquisition_evidence_count",
    "origin_not_recorded_count",
    "with_known_source_count",
    "with_utm_count",
    "with_any_click_id_count",
    "gclid_present_count",
    "gbraid_present_count",
    "wbraid_present_count",
    "fbclid_present_count",
)

KNOWN_LIMITATIONS = (
    "browser_dispatch_has_no_persisted_confirmation",
    "ga4_accounted_not_provable_locally",
    "meta_accounted_not_provable_from_funnel_event_alone",
    "historical_consent_not_stored_on_funnel_event",
    "missing_origin_does_not_imply_direct",
    "external_candidate_does_not_imply_dispatch",
    "paid_has_no_external_purchase_at_this_stage",
    "meta_http_2xx_is_adapter_technical_success_only",
)

FIRST_RELEVANT_QUALITY_NOTES = (
    "gap_is_investigable_not_confirmed_bug",
    "marker_is_at_most_one_per_user",
    "marker_may_occur_before_period",
    "recovery_may_create_marker_without_external_dispatch",
    "anonymous_task_completed_is_out_of_scope",
)


def _as_naive_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{name} deve ser datetime")
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _as_int(value: Any) -> int:
    if value is None:
        return 0
    return int(value)


def _rates(counts: dict[str, int]) -> dict[str, float | None]:
    """Taxas evento/evento. Denominador zero permanece ausente (None)."""
    denominator = counts["applicable_count"]
    rates: dict[str, float | None] = {}
    for key in _COUNT_KEYS:
        if key == "applicable_count":
            continue
        rate_name = key[: -len("_count")]
        if denominator <= 0:
            rates[rate_name] = None
        else:
            rates[rate_name] = counts[key] / denominator
    return rates


def _empty_touch_metrics() -> dict[str, Any]:
    counts = {key: 0 for key in _COUNT_KEYS}
    return {**counts, "rates": _rates(counts)}


def _metrics_from_counts(counts: dict[str, int]) -> dict[str, Any]:
    applicable = counts["applicable_count"]
    evidence = counts["with_acquisition_evidence_count"]
    normalized = dict(counts)
    normalized["origin_not_recorded_count"] = max(0, applicable - evidence)
    return {**normalized, "rates": _rates(normalized)}


def _json_text(touch: str, field: str):
    return (
        FunnelEvent.metadata_json["growth_attribution"][touch][field].as_string()
    )


def _present(touch: str, field: str):
    extracted = _json_text(touch, field)
    return and_(extracted.is_not(None), extracted != "")


def _any_present(touch: str, fields: tuple[str, ...]):
    return or_(*(_present(touch, field) for field in fields))


def _sum_when(condition, label: str):
    return func.coalesce(func.sum(case((condition, 1), else_=0)), 0).label(label)


def _period_filter(start: datetime, end: datetime):
    return and_(FunnelEvent.occurred_at >= start, FunnelEvent.occurred_at < end)


def _internal_event_counts(start: datetime, end: datetime) -> dict[str, int]:
    counted = tuple(
        sorted(
            set(INTERNAL_MONITOR_EVENT_NAMES)
            | set(META_BROWSER_CAPI_CANDIDATE_EVENTS)
            | set(GA4_BROWSER_CANDIDATE_EVENTS)
        )
    )
    stmt = (
        select(FunnelEvent.event_name, func.count(FunnelEvent.id))
        .where(FunnelEvent.event_name.in_(counted), _period_filter(start, end))
        .group_by(FunnelEvent.event_name)
    )
    found = {
        str(name): _as_int(total)
        for name, total in db.session.execute(stmt).all()
    }
    return {name: found.get(name, 0) for name in counted}


def _touch_block(
    rows_by_event: dict[str, dict[str, int]],
    *,
    touch: str,
    applicable_events: frozenset[str],
) -> dict[str, Any]:
    by_event: dict[str, Any] = {}
    for name in sorted(applicable_events):
        raw = rows_by_event.get(name)
        if raw is None:
            by_event[name] = _empty_touch_metrics()
            continue
        by_event[name] = _metrics_from_counts(raw[touch])
    totals = _metrics_from_counts(
        {
            key: sum(item[key] for item in by_event.values())
            for key in _COUNT_KEYS
            if key != "origin_not_recorded_count"
        }
        | {"origin_not_recorded_count": 0}
    )
    return {
        "applicable_event_names": sorted(applicable_events),
        "unit": "funnel_event",
        "missing_origin_means": "origin_not_recorded",
        "by_event_name": by_event,
        "totals": totals,
    }


def _attribution(start: datetime, end: datetime) -> dict[str, Any]:
    columns: list[Any] = [
        FunnelEvent.event_name,
        func.count(FunnelEvent.id).label("applicable_count"),
    ]
    touches = ("first_touch", "current_session_origin")
    for touch in touches:
        utm = _any_present(touch, _UTM_FIELDS)
        click = _any_present(touch, _CLICK_FIELDS)
        columns.append(_sum_when(or_(utm, click), f"{touch}__evidence"))
        columns.append(_sum_when(_present(touch, "source"), f"{touch}__source"))
        columns.append(_sum_when(utm, f"{touch}__utm"))
        columns.append(_sum_when(click, f"{touch}__click"))
        for field in _CLICK_FIELDS:
            columns.append(_sum_when(_present(touch, field), f"{touch}__{field}"))

    stmt = (
        select(*columns)
        .where(
            FunnelEvent.event_name.in_(tuple(sorted(CURRENT_SESSION_EVENT_NAMES))),
            _period_filter(start, end),
        )
        .group_by(FunnelEvent.event_name)
    )
    rows_by_event: dict[str, dict[str, dict[str, int]]] = {}
    for row in db.session.execute(stmt).all():
        mapping = row._mapping
        event_name = str(mapping["event_name"])
        applicable = _as_int(mapping["applicable_count"])
        per_touch: dict[str, dict[str, int]] = {}
        for touch in touches:
            per_touch[touch] = {
                "applicable_count": applicable,
                "with_acquisition_evidence_count": _as_int(mapping[f"{touch}__evidence"]),
                "origin_not_recorded_count": 0,
                "with_known_source_count": _as_int(mapping[f"{touch}__source"]),
                "with_utm_count": _as_int(mapping[f"{touch}__utm"]),
                "with_any_click_id_count": _as_int(mapping[f"{touch}__click"]),
                "gclid_present_count": _as_int(mapping[f"{touch}__gclid"]),
                "gbraid_present_count": _as_int(mapping[f"{touch}__gbraid"]),
                "wbraid_present_count": _as_int(mapping[f"{touch}__wbraid"]),
                "fbclid_present_count": _as_int(mapping[f"{touch}__fbclid"]),
            }
        rows_by_event[event_name] = per_touch

    return {
        "unit": "funnel_event",
        "missing_origin_means": "origin_not_recorded",
        "first_touch": _touch_block(
            rows_by_event,
            touch="first_touch",
            applicable_events=FIRST_TOUCH_EVENT_NAMES,
        ),
        "current_session_origin": _touch_block(
            rows_by_event,
            touch="current_session_origin",
            applicable_events=CURRENT_SESSION_EVENT_NAMES,
        ),
    }


def _candidate_block(counts: dict[str, int], allowlist: frozenset[str]) -> dict[str, Any]:
    by_event = {
        name: {"external_candidate_count": counts.get(name, 0)}
        for name in sorted(allowlist)
    }
    return {
        "external_candidate_count": sum(
            item["external_candidate_count"] for item in by_event.values()
        ),
        "by_event_name": by_event,
    }


def _first_relevant_quality(start: datetime, end: datetime) -> dict[str, Any]:
    """Conclusao elegivel no periodo sem marco correspondente.

    O marco e buscado em qualquer data (no maximo um por usuario, e pode
    ser anterior ao periodo). Gap e indicador investigavel, nao bug confirmado.
    """
    completion = FunnelEvent
    marker_any = aliased(FunnelEvent)
    marker_before = aliased(FunnelEvent)
    task_type = completion.metadata_json["task_type"].as_string()
    has_marker = exists(
        select(marker_any.id).where(
            marker_any.user_id == completion.user_id,
            marker_any.event_name == FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
        )
    )
    has_marker_before = exists(
        select(marker_before.id).where(
            marker_before.user_id == completion.user_id,
            marker_before.event_name == FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
            marker_before.occurred_at < start,
        )
    )
    eligible = and_(
        completion.event_name == FUNNEL_EVENT_TASK_COMPLETED,
        completion.occurred_at >= start,
        completion.occurred_at < end,
        completion.user_id.is_not(None),
        task_type.in_(tuple(sorted(FIRST_RELEVANT_ELIGIBLE_TASK_TYPES))),
    )
    stmt = select(
        func.count(completion.id),
        func.count(func.distinct(completion.user_id)),
        func.coalesce(func.sum(case((has_marker, 1), else_=0)), 0),
        func.coalesce(func.sum(case((~has_marker, 1), else_=0)), 0),
        func.coalesce(func.sum(case((has_marker_before, 1), else_=0)), 0),
        func.count(func.distinct(case((~has_marker, completion.user_id), else_=None))),
    ).where(eligible)
    row = db.session.execute(stmt).one()
    return {
        "unit": "eligible_task_completed_event",
        "user_unit": "distinct_user_id",
        "interpretation": "investigable_indicator",
        "eligible_completion_count": _as_int(row[0]),
        "eligible_user_count": _as_int(row[1]),
        "completions_with_marker_count": _as_int(row[2]),
        "completions_without_marker_count": _as_int(row[3]),
        "completions_with_marker_before_period_count": _as_int(row[4]),
        "users_without_marker_count": _as_int(row[5]),
        "notes": list(FIRST_RELEVANT_QUALITY_NOTES),
    }


def get_growth_tracking_quality_payload(
    *,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Payload agregado de qualidade para [start, end).

    start/end aware sao convertidos para UTC naive, o contrato de
    FunnelEvent.occurred_at. start > end e invalido.
    """
    start_n = _as_naive_utc(start, "start")
    end_n = _as_naive_utc(end, "end")
    if start_n > end_n:
        raise ValueError("start deve ser anterior ou igual a end")

    internal_counts = _internal_event_counts(start_n, end_n)
    return {
        "period": {
            "start": start_n.isoformat(),
            "end": end_n.isoformat(),
            "bounds": "start_inclusive_end_exclusive",
            "occurred_at_comparison": "naive_utc",
        },
        "internal": {
            "unit": "funnel_event",
            "event_counts": {
                name: internal_counts.get(name, 0)
                for name in INTERNAL_MONITOR_EVENT_NAMES
            },
        },
        "attribution": _attribution(start_n, end_n),
        "external_candidates": {
            "derivation": "event_name_allowlist",
            "does_not_mean": "dispatched_delivered_or_received",
            "meta_browser_capi": _candidate_block(
                internal_counts, META_BROWSER_CAPI_CANDIDATE_EVENTS
            ),
            "ga4_browser": _candidate_block(
                internal_counts, GA4_BROWSER_CANDIDATE_EVENTS
            ),
        },
        "first_relevant_quality": _first_relevant_quality(start_n, end_n),
        "meta_capi": {
            "source": "process_logs",
            "historical_database_metrics_available": False,
        },
        "known_limitations": list(KNOWN_LIMITATIONS),
    }
