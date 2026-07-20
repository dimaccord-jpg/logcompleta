"""
Agregacoes gerenciais sobre ia_consumo_evento e snapshot de custo (fase 1).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, or_

from app.extensions import db
from app.models import IaBillingCostSnapshot, IaConsumoEvento, ProcessingEvent

# Excluído do consolidado administrativo mensal de IA produtiva (permanece em IaConsumoEvento).
FLOW_TYPE_ONBOARDING_DISCOVERY = "onboarding_discovery"
FAILURE_STATUSES = ("failure", "error")


def _operational_ia_month_scope(*conditions):
    """
    Escopo analítico do painel admin mensal: todos os eventos do mês,
    exceto onboarding discovery (separação analítica, mesma persistência).
    """
    return and_(
        or_(
            IaConsumoEvento.flow_type.is_(None),
            IaConsumoEvento.flow_type != FLOW_TYPE_ONBOARDING_DISCOVERY,
        ),
        *conditions,
    )


def _month_datetime_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    """UTC naive half-open interval [start, end) for occurred_at filters."""
    start = datetime(year, month, 1, 0, 0, 0)
    if month == 12:
        end = datetime(year + 1, 1, 1, 0, 0, 0)
    else:
        end = datetime(year, month + 1, 1, 0, 0, 0)
    return start, end


def aggregate_onboarding_discovery_metrics(year: int, month: int) -> dict[str, Any]:
    """
    Tokens de onboarding discovery (controle interno/admin).
    Separado do consolidado operacional produtivo do cliente.
    """
    start, end = _month_datetime_bounds(year, month)
    month_bounds = and_(
        IaConsumoEvento.occurred_at >= start,
        IaConsumoEvento.occurred_at < end,
        IaConsumoEvento.flow_type == FLOW_TYPE_ONBOARDING_DISCOVERY,
    )

    total_tokens = int(
        db.session.query(func.coalesce(func.sum(IaConsumoEvento.total_tokens), 0))
        .filter(month_bounds, IaConsumoEvento.total_tokens.isnot(None))
        .scalar()
        or 0
    )

    by_key_rows = (
        db.session.query(
            IaConsumoEvento.api_key_label,
            func.coalesce(func.sum(IaConsumoEvento.total_tokens), 0),
        )
        .filter(month_bounds, IaConsumoEvento.total_tokens.isnot(None))
        .group_by(IaConsumoEvento.api_key_label)
        .all()
    )
    tokens_by_api_key = {str(row[0]): int(row[1] or 0) for row in by_key_rows}

    event_count = (
        db.session.query(func.count(IaConsumoEvento.id))
        .filter(month_bounds)
        .scalar()
    )
    success_events = (
        db.session.query(func.count(IaConsumoEvento.id))
        .filter(month_bounds, IaConsumoEvento.status == "success")
        .scalar()
    )
    events_without_metrics = (
        db.session.query(func.count(IaConsumoEvento.id))
        .filter(month_bounds, IaConsumoEvento.total_tokens.is_(None))
        .scalar()
    )
    failure_events = (
        db.session.query(func.count(IaConsumoEvento.id))
        .filter(month_bounds, IaConsumoEvento.status.in_(FAILURE_STATUSES))
        .scalar()
    )

    return {
        "total_tokens_month": total_tokens,
        "tokens_by_api_key": tokens_by_api_key,
        "event_count_month": int(event_count or 0),
        "event_count_with_metrics_month": int(success_events or 0),
        "event_count_without_metrics_month": int(events_without_metrics or 0),
        "failure_event_count_month": int(failure_events or 0),
    }


def aggregate_month_metrics(year: int, month: int) -> dict[str, Any]:
    """
    Retorna totais de tokens no mes, por api_key_label, e totais parciais por status.
    Consolidação operacional produtiva: exclui flow_type=onboarding_discovery.
    """
    start, end = _month_datetime_bounds(year, month)
    month_bounds = and_(
        IaConsumoEvento.occurred_at >= start,
        IaConsumoEvento.occurred_at < end,
    )

    q_sum_total = (
        db.session.query(func.coalesce(func.sum(IaConsumoEvento.total_tokens), 0))
        .filter(
            _operational_ia_month_scope(
                month_bounds,
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
            _operational_ia_month_scope(
                month_bounds,
                IaConsumoEvento.total_tokens.isnot(None),
            )
        )
        .group_by(IaConsumoEvento.api_key_label)
        .all()
    )
    tokens_by_api_key = {str(row[0]): int(row[1] or 0) for row in by_key_rows}

    event_count = (
        db.session.query(func.count(IaConsumoEvento.id))
        .filter(_operational_ia_month_scope(month_bounds))
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


def _aggregate_processing_metrics_month_by_agent_flow(
    year: int,
    month: int,
    *,
    agent: str,
    flow_type: str | None = None,
    flow_types: list[str] | None = None,
) -> dict[str, Any]:
    """
    Metricas mensais de processamento analitico (nao-LLM) por agente/fluxo.
    """
    start, end = _month_datetime_bounds(year, month)

    flow_filter: Any
    if flow_types:
        flow_filter = ProcessingEvent.flow_type.in_(flow_types)
    elif flow_type is not None:
        flow_filter = ProcessingEvent.flow_type == flow_type
    else:
        raise ValueError("flow_type ou flow_types é obrigatório")

    base_filter = and_(
        ProcessingEvent.occurred_at >= start,
        ProcessingEvent.occurred_at < end,
        ProcessingEvent.agent == agent,
        flow_filter,
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


def aggregate_processing_metrics_month(year: int, month: int) -> dict[str, Any]:
    """
    Metricas mensais de processamento analitico do Roberto.
    Mantem compatibilidade com o bloco historico: agent=roberto e flow_type=upload_bi.
    """
    return _aggregate_processing_metrics_month_by_agent_flow(
        year,
        month,
        agent="roberto",
        flow_type="upload_bi",
    )


CLEIDE_PROCESSING_FLOW_TYPES = (
    "upload_fretes",
    "cleide_audit_coverage_upload",
    "cleide_audit_batch_upload",
)

# Reprocessamentos explícitos (cleide_audit_batch_processed) debitam franquia,
# mas não entram no consolidado de linhas faturadas do dashboard Cleide.
CLEIDE_PROCESSING_REPROCESS_FLOW_TYPES = ("cleide_audit_batch_processed",)

AGENTE_COMPARA_PROCESSING_FLOW_TYPES = (
    "agente_compara_coverage_upload",
    "agente_compara_batch_upload",
)

# Reprocessamentos explícitos debitam franquia, mas não somam novamente
# no consolidado de linhas faturadas do dashboard AgenteCompara.
AGENTE_COMPARA_PROCESSING_REPROCESS_FLOW_TYPES = ("agente_compara_batch_processed",)


def aggregate_cleide_processing_metrics_month(year: int, month: int) -> dict[str, Any]:
    """
    Metricas mensais de processamento analitico da Cleide.
    Agrega upload legado e fluxos operacionais da Auditoria Cleide.
    Linhas da planilha auditada entram via cleide_audit_batch_upload; reprocessamentos
    não somam novamente no consolidado de linhas faturadas.
    """
    metrics = _aggregate_processing_metrics_month_by_agent_flow(
        year,
        month,
        agent="cleide",
        flow_types=list(CLEIDE_PROCESSING_FLOW_TYPES),
    )
    reprocess_events = _aggregate_processing_metrics_month_by_agent_flow(
        year,
        month,
        agent="cleide",
        flow_types=list(CLEIDE_PROCESSING_REPROCESS_FLOW_TYPES),
    )
    metrics["total_processing_events_month"] = int(metrics.get("total_processing_events_month") or 0) + int(
        reprocess_events.get("total_processing_events_month") or 0
    )
    return metrics


def aggregate_agente_compara_processing_metrics_month(year: int, month: int) -> dict[str, Any]:
    """
    Metricas mensais de processamento analitico do AgenteCompara.
    Linhas da planilha entram via agente_compara_batch_upload; reprocessamentos
    não somam novamente no consolidado de linhas faturadas.
    """
    metrics = _aggregate_processing_metrics_month_by_agent_flow(
        year,
        month,
        agent="agente_compara",
        flow_types=list(AGENTE_COMPARA_PROCESSING_FLOW_TYPES),
    )
    reprocess_events = _aggregate_processing_metrics_month_by_agent_flow(
        year,
        month,
        agent="agente_compara",
        flow_types=list(AGENTE_COMPARA_PROCESSING_REPROCESS_FLOW_TYPES),
    )
    metrics["total_processing_events_month"] = int(metrics.get("total_processing_events_month") or 0) + int(
        reprocess_events.get("total_processing_events_month") or 0
    )
    return metrics


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
    cleide_proc = aggregate_cleide_processing_metrics_month(year, month)
    agente_compara_proc = aggregate_agente_compara_processing_metrics_month(year, month)
    onboarding_ia = aggregate_onboarding_discovery_metrics(year, month)
    from app.services.cleiton_cost_service import total_processing_estimated_cost_month

    proc_cost = total_processing_estimated_cost_month(year, month)
    cleide_proc_cost = total_processing_estimated_cost_month(
        year,
        month,
        agent="cleide",
        flow_types=list(CLEIDE_PROCESSING_FLOW_TYPES),
    )
    agente_compara_proc_cost = total_processing_estimated_cost_month(
        year,
        month,
        agent="agente_compara",
        flow_types=list(AGENTE_COMPARA_PROCESSING_FLOW_TYPES),
    )
    total_internal_tokens_month = int((agg.get("total_tokens_month") or 0) + (onboarding_ia.get("total_tokens_month") or 0))
    return {
        **agg,
        "cost_total_month": float(cost) if cost is not None else None,
        "currency": cur,
        "cost_snapshot_at": snap_at,
        "cost_per_token": cpt,
        "operational_tokens_month": int(agg.get("total_tokens_month") or 0),
        "onboarding_tokens_month": int(onboarding_ia.get("total_tokens_month") or 0),
        "total_internal_tokens_month": total_internal_tokens_month,
        **proc,
        **proc_cost,
        "cleide_processing": {
            **cleide_proc,
            **cleide_proc_cost,
        },
        "agente_compara_processing": {
            **agente_compara_proc,
            **agente_compara_proc_cost,
        },
        "onboarding_discovery_ia": onboarding_ia,
    }
