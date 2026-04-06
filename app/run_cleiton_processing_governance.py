"""
Cleiton — registro centralizado de processamento analítico (não-LLM), fase 1.1.
Paralelo à medição de tokens; não mistura métricas com ia_consumo_evento.
"""
from __future__ import annotations

import logging
from flask import has_app_context

from app.consumo_identidade import resolve_identidade_para_persistencia
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
    execution_id: str | None = None,
) -> dict:
    """
    Persiste um único evento de processamento (append-only).
    Chamadas duplicadas no mesmo request devem ser evitadas pelo chamador.
    execution_id é aceito para correlação do fluxo de execução.
    """
    if not has_app_context():
        logger.debug(
            "Governança processamento: sem app context; evento não persistido (%s %s, execution_id=%s).",
            agent,
            flow_type,
            execution_id,
        )
        return {"persisted": False, "reason": "sem_app_context"}
    try:
        ms = max(0, int(processing_time_ms))
        rows = max(0, int(rows_processed))
        ident = resolve_identidade_para_persistencia()
        row = ProcessingEvent(
            occurred_at=utcnow_naive(),
            agent=(agent or "")[:80],
            flow_type=(flow_type or "")[:80],
            processing_type=(processing_type or PROCESSING_TYPE_NON_LLM)[:40],
            rows_processed=rows,
            processing_time_ms=ms,
            status=(status or STATUS_FAILURE)[:40],
            error_summary=_truncate_err(error_summary, 2000),
            conta_id=ident.get("conta_id"),
            franquia_id=ident.get("franquia_id"),
            usuario_id=ident.get("usuario_id"),
            tipo_origem=(ident.get("tipo_origem") or "")[:80] or None,
            origem_sistema=ident.get("origem_sistema"),
        )
        db.session.add(row)
        db.session.commit()
        motor_result = None
        try:
            from app.services.cleiton_franquia_operacional_service import (
                aplicar_motor_apos_processing_event,
            )

            motor_result = aplicar_motor_apos_processing_event(row.id)
        except Exception as ex:
            logger.warning(
                "Governança processamento: motor operacional Cleiton após evento falhou (id=%s): %s",
                getattr(row, "id", None),
                ex,
            )
            try:
                db.session.rollback()
            except Exception:
                pass
        return {
            "persisted": True,
            "processing_event_id": row.id,
            "motor_result": motor_result,
        }
    except Exception as e:
        logger.warning(
            "Governança processamento: falha ao persistir evento (%s, execution_id=%s): %s",
            flow_type,
            execution_id,
            e,
        )
        try:
            db.session.rollback()
        except Exception:
            pass
        return {"persisted": False, "reason": "falha_persistencia"}
