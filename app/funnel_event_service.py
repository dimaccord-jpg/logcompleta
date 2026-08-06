from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import FunnelEvent, utcnow_naive

FUNNEL_EVENT_FILE_UPLOADED = "file_uploaded"
FUNNEL_EVENT_FREIGHT_CALCULATED = "freight_calculated"

FUNNEL_SOURCE_CLEIDE_AUDIT = "cleide_audit"
FUNNEL_SOURCE_AGENTE_COMPARA = "agente_compara"

ALLOWED_FUNNEL_EVENTS = {
    FUNNEL_EVENT_FILE_UPLOADED,
    FUNNEL_EVENT_FREIGHT_CALCULATED,
}
ALLOWED_FUNNEL_SOURCES = {
    FUNNEL_SOURCE_CLEIDE_AUDIT,
    FUNNEL_SOURCE_AGENTE_COMPARA,
}


def _normalize_required_text(value: Any, field_name: str, *, limit: int | None = None) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} e obrigatorio")
    if limit is not None:
        _validate_text_limit(text, field_name, limit=limit)
    return text


def _normalize_optional_text(value: Any, field_name: str, *, limit: int) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    _validate_text_limit(text, field_name, limit=limit)
    return text


def _validate_text_limit(value: str, field_name: str, *, limit: int) -> None:
    if len(value) > limit:
        raise ValueError(f"{field_name} excede limite de {limit} caracteres")


def _validate_metadata_json(metadata_json: Any) -> Any:
    if metadata_json is None:
        return None
    try:
        json.dumps(metadata_json, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("metadata_json deve ser compativel com JSON") from exc
    return metadata_json


def _has_same_idempotent_payload(
    existing: FunnelEvent,
    *,
    event_name: str,
    source: str,
    user_id: int,
    conta_id: int,
    franquia_id: int,
) -> bool:
    return (
        existing.event_name == event_name
        and existing.source == source
        and existing.user_id == user_id
        and existing.conta_id == conta_id
        and existing.franquia_id == franquia_id
    )


def record_funnel_event(
    *,
    event_name: str,
    source: str,
    user_id: int,
    conta_id: int,
    franquia_id: int,
    idempotency_key: str,
    occurred_at: datetime | None = None,
    correlation_id: str | None = None,
    document_id: str | None = None,
    audit_batch_id: str | None = None,
    comparison_id: str | None = None,
    execution_id: str | None = None,
    metadata_json: Any = None,
) -> dict[str, Any]:
    """
    Registra um evento append-only de funil.

    Contrato transacional:
    - faz add() + flush() dentro de savepoint local;
    - nao faz commit();
    - em colisao de idempotencia, reaproveita a linha existente sem rollback global.
    """
    event_name_n = _normalize_required_text(event_name, "event_name", limit=40).lower()
    source_n = _normalize_required_text(source, "source", limit=40).lower()
    key_n = _normalize_required_text(idempotency_key, "idempotency_key", limit=160)

    if event_name_n not in ALLOWED_FUNNEL_EVENTS:
        raise ValueError("event_name invalido")
    if source_n not in ALLOWED_FUNNEL_SOURCES:
        raise ValueError("source invalida")

    try:
        user_id_i = int(user_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("user_id e obrigatorio") from exc
    try:
        conta_id_i = int(conta_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("conta_id e obrigatorio") from exc
    try:
        franquia_id_i = int(franquia_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("franquia_id e obrigatorio") from exc

    row: FunnelEvent | None = None
    try:
        with db.session.begin_nested():
            row = FunnelEvent(
                user_id=user_id_i,
                conta_id=conta_id_i,
                franquia_id=franquia_id_i,
                event_name=event_name_n,
                source=source_n,
                occurred_at=occurred_at or utcnow_naive(),
                idempotency_key=key_n,
                correlation_id=_normalize_optional_text(correlation_id, "correlation_id", limit=200),
                document_id=_normalize_optional_text(document_id, "document_id", limit=120),
                audit_batch_id=_normalize_optional_text(audit_batch_id, "audit_batch_id", limit=120),
                comparison_id=_normalize_optional_text(comparison_id, "comparison_id", limit=120),
                execution_id=_normalize_optional_text(execution_id, "execution_id", limit=120),
                metadata_json=_validate_metadata_json(metadata_json),
            )
            db.session.add(row)
            db.session.flush()
    except IntegrityError as exc:
        existing = FunnelEvent.query.filter_by(idempotency_key=key_n).first()
        if existing is None:
            raise ValueError(
                "Nao foi possivel registrar evento de funil: colisao de idempotencia sem estado reaproveitavel."
            ) from exc
        if not _has_same_idempotent_payload(
            existing,
            event_name=event_name_n,
            source=source_n,
            user_id=user_id_i,
            conta_id=conta_id_i,
            franquia_id=franquia_id_i,
        ):
            raise ValueError("idempotency_key reutilizada com dados divergentes") from exc
        return {"created": False, "event": existing}

    if row is None:
        raise ValueError("Nao foi possivel registrar evento de funil.")
    return {"created": True, "event": row}
