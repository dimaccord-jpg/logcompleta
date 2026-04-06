"""
Leitura operacional de consumo por franquia e período (ciclo), sem billing nem bloqueio.

Agrega:
- `ia_consumo_evento` (IA / tokens)
- `processing_events` (processamento não-LLM)

Extensível para novos tipos de evento no mesmo padrão (franquia_id + occurred_at).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func

from app.extensions import db
from app.models import IaConsumoEvento, ProcessingEvent


def consumo_acumulado_por_franquia_no_periodo(
    franquia_id: int,
    inicio: datetime,
    fim: datetime,
) -> dict[str, Any]:
    """
    Retorna totais no intervalo [inicio, fim) em `occurred_at`.

    :param franquia_id: ID da franquia operacional
    :param inicio: início inclusive (UTC naive, alinhado ao restante do app)
    :param fim: fim exclusivo
    """
    fid = int(franquia_id)
    base_ia = (
        db.session.query(
            func.count(IaConsumoEvento.id),
            func.coalesce(func.sum(IaConsumoEvento.total_tokens), 0),
        )
        .filter(
            IaConsumoEvento.franquia_id == fid,
            IaConsumoEvento.occurred_at >= inicio,
            IaConsumoEvento.occurred_at < fim,
        )
        .one()
    )
    n_ia = int(base_ia[0] or 0)
    tokens = int(base_ia[1] or 0)

    base_pe = (
        db.session.query(
            func.count(ProcessingEvent.id),
            func.coalesce(func.sum(ProcessingEvent.rows_processed), 0),
            func.coalesce(func.sum(ProcessingEvent.processing_time_ms), 0),
        )
        .filter(
            ProcessingEvent.franquia_id == fid,
            ProcessingEvent.occurred_at >= inicio,
            ProcessingEvent.occurred_at < fim,
        )
        .one()
    )
    n_pe = int(base_pe[0] or 0)
    rows = int(base_pe[1] or 0)
    ms_pe = int(base_pe[2] or 0)

    return {
        "franquia_id": fid,
        "periodo": {
            "inicio": inicio.isoformat(sep=" ", timespec="seconds"),
            "fim": fim.isoformat(sep=" ", timespec="seconds"),
            "inicio_exclusive_fim": True,
        },
        "ia": {
            "eventos": n_ia,
            "total_tokens": tokens,
            "fonte": "ia_consumo_evento",
        },
        "processamento": {
            "eventos": n_pe,
            "rows_processed": rows,
            "processing_time_ms_total": ms_pe,
            "fonte": "processing_events",
        },
    }


def listar_franquias_ids_com_consumo_no_periodo(
    inicio: datetime,
    fim: datetime,
) -> list[int]:
    """Lista distinct franquia_id que aparecem em qualquer evento de consumo no período."""
    q1 = (
        db.session.query(IaConsumoEvento.franquia_id)
        .filter(
            IaConsumoEvento.franquia_id.isnot(None),
            IaConsumoEvento.occurred_at >= inicio,
            IaConsumoEvento.occurred_at < fim,
        )
        .distinct()
    )
    q2 = (
        db.session.query(ProcessingEvent.franquia_id)
        .filter(
            ProcessingEvent.franquia_id.isnot(None),
            ProcessingEvent.occurred_at >= inicio,
            ProcessingEvent.occurred_at < fim,
        )
        .distinct()
    )
    ids = {row[0] for row in q1.all() if row[0] is not None}
    ids.update(row[0] for row in q2.all() if row[0] is not None)
    return sorted(ids)
