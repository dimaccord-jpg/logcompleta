"""
Cleiton — registro centralizado de processamento analítico (não-LLM), fase 1.1.
Paralelo à medição de tokens; não mistura métricas com ia_consumo_evento.
"""
from __future__ import annotations

import logging
from flask import has_app_context

from app.extensions import db
from app.models import ProcessingEvent, utcnow_naive

logger = logging.getLogger(__name__)

STATUS_SUCCESS = "success"
STATUS_FAILURE = "failure"
PROCESSING_TYPE_NON_LLM = "non_llm"


def _truncate_err(msg: str | None, limit: int = 2000) -> str | None:
    if msg is None:
        return None
    s = str(msg).strip()
    if len(s) <= limit:
        return s
    return s[: limit - 3] + "..."


def cleiton_register_processing_event(
    *,
    agent: str,
    flow_type: str,
    processing_type: str,
    rows_processed: int,
    processing_time_ms: int,
    status: str,
    error_summary: str | None = None,
) -> None:
    """
    Persiste um único evento de processamento (append-only).
    Chamadas duplicadas no mesmo request devem ser evitadas pelo chamador.
    """
    if not has_app_context():
        logger.debug(
            "Governança processamento: sem app context; evento não persistido (%s %s).",
            agent,
            flow_type,
        )
        return
    try:
        ms = max(0, int(processing_time_ms))
        rows = max(0, int(rows_processed))
        row = ProcessingEvent(
            occurred_at=utcnow_naive(),
            agent=(agent or "")[:80],
            flow_type=(flow_type or "")[:80],
            processing_type=(processing_type or PROCESSING_TYPE_NON_LLM)[:40],
            rows_processed=rows,
            processing_time_ms=ms,
            status=(status or STATUS_FAILURE)[:40],
            error_summary=_truncate_err(error_summary, 2000),
        )
        db.session.add(row)
        db.session.commit()
    except Exception as e:
        logger.warning("Governança processamento: falha ao persistir evento (%s): %s", flow_type, e)
        try:
            db.session.rollback()
        except Exception:
            pass
