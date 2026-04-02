"""
Parâmetros de custo operacional Cleiton (MVP) e custo estimado de processamento Roberto.
Não persiste custo por evento — calcula na leitura com parâmetros atuais.
"""
from __future__ import annotations

import calendar
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import and_

from app.extensions import db
from app.models import CleitonCostConfig, ProcessingEvent, utcnow_naive

logger = logging.getLogger(__name__)

SINGLETON_ID = 1


def get_or_create_config() -> CleitonCostConfig:
    row = db.session.get(CleitonCostConfig, SINGLETON_ID)
    if row is None:
        row = CleitonCostConfig(id=SINGLETON_ID)
        db.session.add(row)
        try:
            db.session.commit()
        except Exception as e:
            logger.warning("cleiton_cost_config: falha ao criar singleton: %s", e)
            db.session.rollback()
            row = db.session.get(CleitonCostConfig, SINGLETON_ID)
            if row is None:
                raise
    return row


def compute_cost_per_second(cfg: CleitonCostConfig | None) -> float | None:
    """
    custo_por_segundo =
    (runtime_monthly_cost * allocation_percent * overhead_factor) / month_seconds
    """
    if cfg is None:
        return None
    r = cfg.runtime_monthly_cost
    if r is None:
        return None
    try:
        ms = max(1, int(cfg.month_seconds or 2592000))
        alloc = float(cfg.allocation_percent if cfg.allocation_percent is not None else 1.0)
        oh = float(cfg.overhead_factor if cfg.overhead_factor is not None else 1.0)
        return (float(r) * alloc * oh) / float(ms)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1, 0, 0, 0)
    last = calendar.monthrange(year, month)[1]
    end = datetime(year, month, last, 23, 59, 59)
    return start, end


def total_processing_estimated_cost_month(year: int, month: int) -> dict[str, Any]:
    """
    Soma (processing_time_ms / 1000) * custo_por_segundo para eventos roberto/upload_bi no mês.
    """
    cfg = get_or_create_config()
    cps = compute_cost_per_second(cfg)
    if cps is None:
        return {
            "total_processing_estimated_cost_month": None,
            "processing_cost_configured": False,
            "cost_per_second": None,
        }

    start, end = _month_bounds(year, month)
    filt = and_(
        ProcessingEvent.occurred_at >= start,
        ProcessingEvent.occurred_at <= end,
        ProcessingEvent.agent == "roberto",
        ProcessingEvent.flow_type == "upload_bi",
    )
    events = ProcessingEvent.query.filter(filt).all()
    total = 0.0
    for ev in events:
        sec = (ev.processing_time_ms or 0) / 1000.0
        total += sec * cps

    return {
        "total_processing_estimated_cost_month": round(total, 6),
        "processing_cost_configured": True,
        "cost_per_second": cps,
    }


def save_config(
    *,
    runtime_monthly_cost: float | None,
    month_seconds: int,
    allocation_percent: float,
    overhead_factor: float,
    cost_per_million_tokens: float | None,
) -> CleitonCostConfig:
    row = get_or_create_config()
    row.runtime_monthly_cost = runtime_monthly_cost
    row.month_seconds = max(1, int(month_seconds))
    row.allocation_percent = float(allocation_percent)
    row.overhead_factor = float(overhead_factor)
    row.cost_per_million_tokens = cost_per_million_tokens
    row.updated_at = utcnow_naive()
    db.session.add(row)
    db.session.commit()
    return row
