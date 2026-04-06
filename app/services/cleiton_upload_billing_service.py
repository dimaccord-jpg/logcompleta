"""
Billing operacional Cleiton para uploads do Roberto (idempotente).

- Registra apropriação com chave única de idempotência.
- Persiste evento de processamento (processing_events).
- Aciona motor operacional para converter rows/ms em créditos e refletir em Franquia.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.consumo_identidade import resolve_identidade_para_persistencia
from app.extensions import db
from app.models import CleitonBillingApropriacao, Franquia
from app.run_cleiton_processing_governance import cleiton_register_processing_event

logger = logging.getLogger(__name__)

AGENT = "roberto"
FLOW_TYPE = "upload_bi"
PROCESSING_TYPE = "non_llm"
STATUS_SUCCESS = "success"


@dataclass(frozen=True)
class ResultadoApropriacaoUploadRoberto:
    duplicado: bool
    apropriado: bool
    idempotency_key: str
    processing_event_id: int | None
    creditos_apropriados: Decimal | None
    motivo: str | None
    status_franquia_novo: str | None
    consumo_acumulado_atual: Decimal | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "duplicado": self.duplicado,
            "apropriado": self.apropriado,
            "idempotency_key": self.idempotency_key,
            "processing_event_id": self.processing_event_id,
            "creditos_apropriados": (
                None if self.creditos_apropriados is None else str(self.creditos_apropriados)
            ),
            "motivo": self.motivo,
            "status_franquia_novo": self.status_franquia_novo,
            "consumo_acumulado_atual": (
                None if self.consumo_acumulado_atual is None else str(self.consumo_acumulado_atual)
            ),
        }


def _normalizar_idempotency_key(raw: str) -> str:
    key = (raw or "").strip()
    if not key:
        raise ValueError("idempotency_key é obrigatório")
    return key[:160]


def _resolver_estado_franquia(franquia_id: int | None) -> tuple[str | None, Decimal | None]:
    if franquia_id is None:
        return None, None
    fr = db.session.get(Franquia, int(franquia_id))
    if fr is None:
        return None, None
    return fr.status, Decimal(str(fr.consumo_acumulado or 0))


def apropriar_billing_upload_roberto(
    *,
    idempotency_key: str,
    rows_processed: int,
    processing_time_ms: int,
    status: str,
    error_summary: str | None = None,
    execution_id: str | None = None,
) -> dict[str, Any]:
    """
    Apropria billing do upload Roberto com idempotência forte por chave única.

    Contrato:
      - duplicado: bool
      - apropriado: bool
      - idempotency_key: str
      - processing_event_id: int | None
      - creditos_apropriados: str | None
      - motivo: str | None
      - status_franquia_novo: str | None
      - consumo_acumulado_atual: str | None
    """
    key = _normalizar_idempotency_key(idempotency_key)
    ident = resolve_identidade_para_persistencia()

    existente = CleitonBillingApropriacao.query.filter_by(idempotency_key=key).first()
    if existente is not None:
        st, consumo = _resolver_estado_franquia(existente.franquia_id)
        return ResultadoApropriacaoUploadRoberto(
            duplicado=True,
            apropriado=bool(existente.processing_event_id),
            idempotency_key=key,
            processing_event_id=existente.processing_event_id,
            creditos_apropriados=(
                None
                if existente.creditos_apropriados is None
                else Decimal(str(existente.creditos_apropriados))
            ),
            motivo=existente.motivo or "idempotencia_reutilizada",
            status_franquia_novo=st,
            consumo_acumulado_atual=consumo,
        ).to_dict()

    marcador = CleitonBillingApropriacao(
        idempotency_key=key,
        agent=AGENT,
        flow_type=FLOW_TYPE,
        status=(status or "failure")[:40],
        error_summary=error_summary,
        rows_processed=max(0, int(rows_processed)),
        processing_time_ms=max(0, int(processing_time_ms)),
        processing_event_id=None,
        creditos_apropriados=None,
        motivo="pending",
        conta_id=ident.get("conta_id"),
        franquia_id=ident.get("franquia_id"),
        usuario_id=ident.get("usuario_id"),
    )
    db.session.add(marcador)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        dup = CleitonBillingApropriacao.query.filter_by(idempotency_key=key).first()
        st, consumo = _resolver_estado_franquia(getattr(dup, "franquia_id", None))
        return ResultadoApropriacaoUploadRoberto(
            duplicado=True,
            apropriado=bool(getattr(dup, "processing_event_id", None)),
            idempotency_key=key,
            processing_event_id=getattr(dup, "processing_event_id", None),
            creditos_apropriados=(
                None
                if getattr(dup, "creditos_apropriados", None) is None
                else Decimal(str(dup.creditos_apropriados))
            ),
            motivo=(getattr(dup, "motivo", None) or "idempotencia_concorrente"),
            status_franquia_novo=st,
            consumo_acumulado_atual=consumo,
        ).to_dict()

    reg = cleiton_register_processing_event(
        agent=AGENT,
        flow_type=FLOW_TYPE,
        processing_type=PROCESSING_TYPE,
        rows_processed=max(0, int(rows_processed)),
        processing_time_ms=max(0, int(processing_time_ms)),
        status=(status or "failure"),
        error_summary=error_summary,
        execution_id=execution_id,
    ) or {}

    event_id = reg.get("processing_event_id")
    motor = reg.get("motor_result")
    creditos = getattr(motor, "creditos", None)
    apropriado = bool(getattr(motor, "abateu_franquia", False))
    motivo = getattr(motor, "motivo_nao_abateu", None)
    if status != STATUS_SUCCESS:
        motivo = motivo or "evento_nao_sucesso"
    elif not apropriado and not motivo:
        motivo = "sem_apropriacao"

    marcador = db.session.get(CleitonBillingApropriacao, marcador.id)
    if marcador is not None:
        marcador.processing_event_id = event_id
        marcador.creditos_apropriados = creditos if apropriado else Decimal("0")
        marcador.motivo = motivo if not apropriado else "apropriado"
        db.session.add(marcador)
        db.session.commit()

    st, consumo = _resolver_estado_franquia(ident.get("franquia_id"))
    return ResultadoApropriacaoUploadRoberto(
        duplicado=False,
        apropriado=apropriado,
        idempotency_key=key,
        processing_event_id=event_id,
        creditos_apropriados=(Decimal(str(creditos)) if creditos is not None else None),
        motivo=motivo,
        status_franquia_novo=st,
        consumo_acumulado_atual=consumo,
    ).to_dict()
