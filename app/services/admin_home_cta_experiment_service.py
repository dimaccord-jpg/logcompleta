"""Read-model administrativo do experimento de CTA da Home.

Consulta somente home_cta_experiment_event. Não usa Lead nem FunnelEvent.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func

from app.models import HomeCtaExperimentEvent, utcnow_naive
from app.services.home_cta_experiment_service import (
    EVENT_CONVERSION,
    EVENT_IMPRESSION,
    HOME_CTA_EXPERIMENT,
    HOME_CTA_VARIANT_IDS,
    HOME_CTA_VARIANTS,
    get_variant_text,
)

_ALLOWED_DAYS = {7, 30, 90}


def _normalize_days(days) -> int:
    try:
        value = int(days)
    except (TypeError, ValueError):
        return 30
    return value if value in _ALLOWED_DAYS else 30


def _cohort_window(*, days: int, now_utc: datetime | None) -> tuple[datetime, datetime]:
    end_utc = (now_utc or utcnow_naive()).replace(hour=23, minute=59, second=59, microsecond=0)
    start_utc = (end_utc - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start_utc, end_utc


def _to_iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def empty_home_cta_experiment_dashboard_payload(
    *,
    days: int = 30,
    start_utc: datetime | None = None,
    end_utc: datetime | None = None,
    warning: str | None = None,
    service_failed: bool = False,
) -> dict:
    days_n = _normalize_days(days)
    if start_utc is None or end_utc is None:
        start_utc, end_utc = _cohort_window(days=days_n, now_utc=None)
    variants = []
    for variant_id in HOME_CTA_VARIANT_IDS:
        variants.append(
            {
                "id": variant_id,
                "label": variant_id.replace("cta_", "").upper(),
                "text": get_variant_text(variant_id),
                "impressions": 0,
                "conversions": 0,
                "conversion_rate": 0.0,
                "uplift_pp_vs_a": None,
            }
        )
    warnings = [warning] if warning else []
    return {
        "experiment": HOME_CTA_EXPERIMENT,
        "period": {
            "start_utc": _to_iso(start_utc),
            "end_utc": _to_iso(end_utc),
            "label": f"Ultimos {days_n} dias",
            "days": days_n,
        },
        "variants": variants,
        "data_quality": {
            "has_data": False,
            "warnings": warnings,
            "service_failed": service_failed,
            "inconsistent_variants": [],
        },
    }


def get_home_cta_experiment_dashboard_payload(
    *,
    days: int = 30,
    now_utc: datetime | None = None,
) -> dict:
    days_n = _normalize_days(days)
    start_utc, end_utc = _cohort_window(days=days_n, now_utc=now_utc)
    payload = empty_home_cta_experiment_dashboard_payload(
        days=days_n,
        start_utc=start_utc,
        end_utc=end_utc,
    )

    rows = (
        HomeCtaExperimentEvent.query.with_entities(
            HomeCtaExperimentEvent.variant,
            HomeCtaExperimentEvent.event_type,
            func.count(HomeCtaExperimentEvent.id),
        )
        .filter(
            HomeCtaExperimentEvent.experiment == HOME_CTA_EXPERIMENT,
            HomeCtaExperimentEvent.occurred_at >= start_utc,
            HomeCtaExperimentEvent.occurred_at <= end_utc,
        )
        .group_by(HomeCtaExperimentEvent.variant, HomeCtaExperimentEvent.event_type)
        .all()
    )

    counts: dict[str, dict[str, int]] = defaultdict(lambda: {EVENT_IMPRESSION: 0, EVENT_CONVERSION: 0})
    for variant, event_type, total in rows:
        if variant not in HOME_CTA_VARIANTS:
            continue
        if event_type not in {EVENT_IMPRESSION, EVENT_CONVERSION}:
            continue
        counts[variant][event_type] = int(total or 0)

    inconsistent: list[str] = []
    variants_out = []
    rate_by_id: dict[str, float] = {}
    for variant_id in HOME_CTA_VARIANT_IDS:
        impressions = counts[variant_id][EVENT_IMPRESSION]
        conversions_raw = counts[variant_id][EVENT_CONVERSION]
        if conversions_raw > impressions:
            inconsistent.append(variant_id)
        conversions_capped = min(conversions_raw, impressions)
        if impressions <= 0:
            conversion_rate = 0.0
        else:
            conversion_rate = conversions_capped / impressions * 100.0
        rate_by_id[variant_id] = conversion_rate
        variants_out.append(
            {
                "id": variant_id,
                "label": variant_id.replace("cta_", "").upper(),
                "text": get_variant_text(variant_id),
                "impressions": impressions,
                "conversions": conversions_capped,
                "conversions_raw": conversions_raw,
                "conversion_rate": conversion_rate,
                "uplift_pp_vs_a": None,
            }
        )

    rate_a = rate_by_id.get("cta_a", 0.0)
    for item in variants_out:
        if item["id"] == "cta_a":
            item["uplift_pp_vs_a"] = None
        else:
            item["uplift_pp_vs_a"] = round(item["conversion_rate"] - rate_a, 1)
        item["conversion_rate"] = round(item["conversion_rate"], 1)

    payload["variants"] = variants_out
    payload["data_quality"]["has_data"] = any(
        item["impressions"] > 0 or item["conversions"] > 0 for item in variants_out
    )
    payload["data_quality"]["inconsistent_variants"] = inconsistent
    if inconsistent:
        names = ", ".join(inconsistent)
        payload["data_quality"]["warnings"].append(
            f"Qualidade de dados: conversions > impressions em {names}."
        )
    return payload
