"""
Coleta custo month-to-date a partir do export do Cloud Billing no BigQuery e persiste snapshot interno.
Requer tabela de export configurada (GCP_BILLING_EXPORT_TABLE) e credenciais com permissão de leitura.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from app.extensions import db
from app.models import IaBillingCostSnapshot, utcnow_naive

logger = logging.getLogger(__name__)

SOURCE_BQ = "bigquery_billing_export"


def _month_bounds_utc(now: datetime | None = None) -> tuple[datetime, datetime, str]:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    competence = f"{now.year:04d}-{now.month:02d}"
    return month_start, now, competence


def fetch_month_to_date_cost_from_bigquery() -> dict[str, Any] | None:
    """
    Retorna {"cost": Decimal, "currency": str} ou None se não configurado / erro.
    """
    table = (os.getenv("GCP_BILLING_EXPORT_TABLE") or "").strip()
    if not table:
        logger.info("GCP_BILLING_EXPORT_TABLE não configurado; snapshot de custo ignorado.")
        return None

    month_start, now_utc, _ = _month_bounds_utc()

    try:
        from google.cloud import bigquery
    except ImportError as e:
        logger.warning("google-cloud-bigquery não instalado: %s", e)
        return None

    # Tabela no formato `projeto.dataset.tabela`
    sql = f"""
    SELECT
      SUM(cost) AS total_cost,
      ANY_VALUE(currency) AS currency
    FROM `{table}`
    WHERE usage_start_time >= @month_start
      AND usage_start_time <= @now_ts
    """

    try:
        client = bigquery.Client()
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("month_start", "TIMESTAMP", month_start),
                bigquery.ScalarQueryParameter("now_ts", "TIMESTAMP", now_utc),
            ]
        )
        job = client.query(sql, job_config=job_config)
        rows = list(job.result())
        if not rows:
            return {"cost": Decimal("0"), "currency": "USD"}
        row = rows[0]
        total = row.get("total_cost")
        cur = (row.get("currency") or "USD").strip() or "USD"
        if total is None:
            cost = Decimal("0")
        else:
            cost = Decimal(str(total))
        return {"cost": cost, "currency": cur}
    except Exception as e:
        logger.exception("Falha ao consultar BigQuery billing: %s", e)
        return None


def collect_and_persist_billing_snapshot() -> IaBillingCostSnapshot | None:
    """
    Busca custo month-to-date no BigQuery e grava ia_billing_cost_snapshot.
    """
    data = fetch_month_to_date_cost_from_bigquery()
    if data is None:
        return None

    _, now_utc, competence = _month_bounds_utc()
    ref_date = now_utc.date()

    snap = IaBillingCostSnapshot(
        snapshot_at=utcnow_naive(),
        reference_date=ref_date,
        month_competence=competence,
        cost_total_month_to_date=data["cost"],
        currency=data["currency"][:12],
        source=SOURCE_BQ,
    )
    try:
        db.session.add(snap)
        db.session.commit()
        logger.info(
            "Snapshot billing: competence=%s cost=%s %s",
            competence,
            snap.cost_total_month_to_date,
            snap.currency,
        )
        return snap
    except Exception as e:
        logger.exception("Falha ao persistir snapshot billing: %s", e)
        try:
            db.session.rollback()
        except Exception:
            pass
        return None


def latest_snapshot_for_month(month_competence: str) -> IaBillingCostSnapshot | None:
    return (
        IaBillingCostSnapshot.query.filter_by(month_competence=month_competence)
        .order_by(IaBillingCostSnapshot.snapshot_at.desc())
        .first()
    )
