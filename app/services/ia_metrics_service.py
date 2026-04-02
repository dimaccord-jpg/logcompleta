"""
Agregacoes gerenciais sobre ia_consumo_evento e snapshot de custo (fase 1).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func

from app.extensions import db
from app.models import IaBillingCostSnapshot, IaConsumoEvento, ProcessingEvent


def _month_datetime_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    """UTC naive half-open interval [start, end) for occurred_at filters."""
    start = datetime(year, month, 1, 0, 0, 0)
    if month == 12:
        end = datetime(year + 1, 1, 1, 0, 0, 0)
    else:
        end = datetime(year, month + 1, 1, 0, 0, 0)
    return start, end


def aggregate_month_metrics(year: int, month: int) -> dict[str, Any]:
    """
    Retorna totais de tokens no mes, por api_key_label, e totais parciais por status.
    """
    start, end = _month_datetime_bounds(year, month)

    q_sum_total = (
        db.session.query(func.coalesce(func.sum(IaConsumoEvento.total_tokens), 0))
        .filter(
            and_(
                IaConsumoEvento.occurred_at >= start,
                IaConsumoEvento.occurred_at < end,
                IaConsumoEvento.total_tokens.isnot(None),
            )
        )
        .scalar()
    )
    total_tokens = int(q_sum_total or 0)

    by_key_rows = (
        db.session.query(
            IaConsumoEvento.api_key_label,
            func.coalesce(func.sum(IaConsumoEvento.total_tokens), 0),
        )
        .filter(
            and_(
                IaConsumoEvento.occurred_at >= start,
                IaConsumoEvento.occurred_at < end,
                IaConsumoEvento.total_tokens.isnot(None),
            )
        )
        .group_by(IaConsumoEvento.api_key_label)
        .all()
    )
    tokens_by_api_key = {str(row[0]): int(row[1] or 0) for row in by_key_rows}

    event_count = (
        db.session.query(func.count(IaConsumoEvento.id))
        .filter(
            and_(
                IaConsumoEvento.occurred_at >= start,
                IaConsumoEvento.occurred_at < end,
            )
        )
        .scalar()
    )

    competence = f"{year:04d}-{month:02d}"

    return {
        "year": year,
        "month": month,
        "month_competence": competence,
        "total_tokens_month": total_tokens,
        "tokens_by_api_key": tokens_by_api_key,
        "event_count_month": int(event_count or 0),
    }


def aggregate_processing_metrics_month(year: int, month: int) -> dict[str, Any]:
    """
    Metricas mensais de processamento analitico (nao-LLM), ex.: upload Roberto + BI.
    Filtra agent=roberto e flow_type=upload_bi para alinhar ao instrumentado na fase 1.1.
    """
    start, end = _month_datetime_bounds(year, month)

    base_filter = and_(
        ProcessingEvent.occurred_at >= start,
        ProcessingEvent.occurred_at < end,
        ProcessingEvent.agent == "roberto",
        ProcessingEvent.flow_type == "upload_bi",
    )

    total_events = (
        db.session.query(func.count(ProcessingEvent.id)).filter(base_filter).scalar()
    )
    total_events = int(total_events or 0)

    sum_rows = (
        db.session.query(func.coalesce(func.sum(ProcessingEvent.rows_processed), 0))
        .filter(base_filter)
        .filter(ProcessingEvent.status == "success")
        .scalar()
    )
    total_rows = int(sum_rows or 0)

    avg_ms = (
        db.session.query(func.avg(ProcessingEvent.processing_time_ms))
        .filter(base_filter)
        .filter(ProcessingEvent.status == "success")
        .scalar()
    )
    avg_processing_time_ms = float(avg_ms) if avg_ms is not None else None

    last_row = (
        ProcessingEvent.query.filter(base_filter)
        .order_by(ProcessingEvent.occurred_at.desc())
        .first()
    )
    last_at = last_row.occurred_at.isoformat() if last_row and last_row.occurred_at else None
    last_status = last_row.status if last_row else None

    return {
        "total_processing_events_month": total_events,
        "total_rows_processed_month": total_rows,
        "avg_processing_time_ms": avg_processing_time_ms,
        "last_processing_at": last_at,
        "last_processing_status": last_status,
    }


def cost_per_token(cost: Decimal | None, total_tokens: int) -> float | None:
    if cost is None or total_tokens <= 0:
        return None
    try:
        return float(cost / Decimal(total_tokens))
    except Exception:
        return None


def get_ia_dashboard_payload(year: int, month: int) -> dict[str, Any]:
    """
    Metricas do mes + ultimo snapshot de custo GCP + custo/token (quando houver tokens e custo).
    """
    from app.services.billing_bigquery_service import latest_snapshot_for_month

    agg = aggregate_month_metrics(year, month)
    comp = agg["month_competence"]
    snap: IaBillingCostSnapshot | None = latest_snapshot_for_month(comp)
    cost = snap.cost_total_month_to_date if snap else None
    cur = snap.currency if snap else None
    snap_at = snap.snapshot_at.isoformat() if snap and snap.snapshot_at else None
    total_tok = agg["total_tokens_month"]
    cpt = cost_per_token(cost, total_tok) if cost is not None else None
    proc = aggregate_processing_metrics_month(year, month)
    from app.services.cleiton_cost_service import total_processing_estimated_cost_month

    proc_cost = total_processing_estimated_cost_month(year, month)
    return {
        **agg,
        "cost_total_month": float(cost) if cost is not None else None,
        "currency": cur,
        "cost_snapshot_at": snap_at,
        "cost_per_token": cpt,
        **proc,
        **proc_cost,
    }
