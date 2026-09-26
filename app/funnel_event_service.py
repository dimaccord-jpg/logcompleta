from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Conta, FunnelEvent, MonetizacaoFato, User, utcnow_naive

logger = logging.getLogger(__name__)

# --- Event names (legado + Growth) ---
FUNNEL_EVENT_PAGE_VIEW = "page_view"
FUNNEL_EVENT_CTA_CLICKED = "cta_clicked"
FUNNEL_EVENT_SIGNUP_STARTED = "signup_started"
FUNNEL_EVENT_SIGNUP_COMPLETED = "signup_completed"
FUNNEL_EVENT_SESSION_ORIGIN_OBSERVED = "session_origin_observed"

FUNNEL_EVENT_TASK_PREPARATION_STARTED = "task_preparation_started"
FUNNEL_EVENT_FILE_UPLOADED = "file_uploaded"
FUNNEL_EVENT_TASK_PREPARATION_COMPLETED = "task_preparation_completed"
FUNNEL_EVENT_TASK_STARTED = "task_started"
FUNNEL_EVENT_TASK_COMPLETED = "task_completed"
FUNNEL_EVENT_TASK_FAILED = "task_failed"

FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED = "first_relevant_task_completed"

FUNNEL_EVENT_PLAN_SELECTED = "plan_selected"
FUNNEL_EVENT_CHECKOUT_STARTED = "checkout_started"
FUNNEL_EVENT_PAID = "paid"

# Legado (consumidores atuais)
FUNNEL_EVENT_FREIGHT_CALCULATED = "freight_calculated"
FUNNEL_EVENT_FIRST_AUDIT_COMPLETED = "first_audit_completed"

# --- Sources técnicos (nao usar para UTM/aquisicao) ---
FUNNEL_SOURCE_CLEIDE_AUDIT = "cleide_audit"
FUNNEL_SOURCE_AGENTE_COMPARA = "agente_compara"
FUNNEL_SOURCE_CLEIDE_BI = "cleide_bi"
FUNNEL_SOURCE_CLEIDE_BI_CHAT = "cleide_bi_chat"
FUNNEL_SOURCE_ROBERTO_BI = "roberto_bi"
FUNNEL_SOURCE_ROBERTO_CHAT = "roberto_chat"
FUNNEL_SOURCE_JULIA_CHAT = "julia_chat"
FUNNEL_SOURCE_FREIGHT_QUERY = "freight_query"
FUNNEL_SOURCE_CLEIDE_AUDIT_CHAT = "cleide_audit_chat"
FUNNEL_SOURCE_AGENTE_COMPARA_CHAT = "agente_compara_chat"
FUNNEL_SOURCE_ONBOARDING_DISCOVERY = "onboarding_discovery"
FUNNEL_SOURCE_GROWTH = "growth"

ALLOWED_FUNNEL_EVENTS = {
    FUNNEL_EVENT_PAGE_VIEW,
    FUNNEL_EVENT_CTA_CLICKED,
    FUNNEL_EVENT_SIGNUP_STARTED,
    FUNNEL_EVENT_SIGNUP_COMPLETED,
    FUNNEL_EVENT_SESSION_ORIGIN_OBSERVED,
    FUNNEL_EVENT_TASK_PREPARATION_STARTED,
    FUNNEL_EVENT_FILE_UPLOADED,
    FUNNEL_EVENT_TASK_PREPARATION_COMPLETED,
    FUNNEL_EVENT_TASK_STARTED,
    FUNNEL_EVENT_TASK_COMPLETED,
    FUNNEL_EVENT_TASK_FAILED,
    FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
    FUNNEL_EVENT_PLAN_SELECTED,
    FUNNEL_EVENT_CHECKOUT_STARTED,
    FUNNEL_EVENT_PAID,
    FUNNEL_EVENT_FREIGHT_CALCULATED,
    FUNNEL_EVENT_FIRST_AUDIT_COMPLETED,
}

ALLOWED_FUNNEL_SOURCES = {
    FUNNEL_SOURCE_CLEIDE_AUDIT,
    FUNNEL_SOURCE_AGENTE_COMPARA,
    FUNNEL_SOURCE_CLEIDE_BI,
    FUNNEL_SOURCE_CLEIDE_BI_CHAT,
    FUNNEL_SOURCE_ROBERTO_BI,
    FUNNEL_SOURCE_ROBERTO_CHAT,
    FUNNEL_SOURCE_JULIA_CHAT,
    FUNNEL_SOURCE_FREIGHT_QUERY,
    FUNNEL_SOURCE_CLEIDE_AUDIT_CHAT,
    FUNNEL_SOURCE_AGENTE_COMPARA_CHAT,
    FUNNEL_SOURCE_ONBOARDING_DISCOVERY,
    FUNNEL_SOURCE_GROWTH,
}

# Eventos que podem ser persistidos sem user/conta/franquia.
ANONYMOUS_ALLOWED_EVENTS = {
    FUNNEL_EVENT_PAGE_VIEW,
    FUNNEL_EVENT_CTA_CLICKED,
    FUNNEL_EVENT_SIGNUP_STARTED,
    FUNNEL_EVENT_TASK_PREPARATION_STARTED,
    FUNNEL_EVENT_TASK_PREPARATION_COMPLETED,
    FUNNEL_EVENT_TASK_STARTED,
    FUNNEL_EVENT_TASK_COMPLETED,
    FUNNEL_EVENT_TASK_FAILED,
}

# Eventos Growth que exigem user_id quando emitidos.
USER_REQUIRED_EVENTS = {
    FUNNEL_EVENT_SIGNUP_COMPLETED,
    FUNNEL_EVENT_SESSION_ORIGIN_OBSERVED,
    FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
    FUNNEL_EVENT_PLAN_SELECTED,
}

# Eventos comerciais: conta obrigatoria; user/franquia opcionais.
ACCOUNT_REQUIRED_EVENTS = {
    FUNNEL_EVENT_CHECKOUT_STARTED,
    FUNNEL_EVENT_PAID,
}

# Caminhos legados Auditoria/Compara: identidade completa obrigatoria.
LEGACY_FULL_IDENTITY_REQUIRED_EVENTS = {
    FUNNEL_EVENT_FILE_UPLOADED,
    FUNNEL_EVENT_FREIGHT_CALCULATED,
    FUNNEL_EVENT_FIRST_AUDIT_COMPLETED,
}

# Allowlist legada Meta Pixel via trackFunnelEvent (AuditStarted / FirstAuditCompleted).
# SCRUM-157B: vazia — sinais Meta legados removidos; Growth file_uploaded /
# freight_calculated / first_audit_completed permanecem first-party.
# Nao confundir com EXTERNAL_BROWSER_ALLOWED_EVENTS / AFExternalTracking.
META_PIXEL_ALLOWED_EVENTS: set[str] = set()

# SCRUM-159: growth_attribution somente nestes produtores (nunca em paid/page_view/etc.).
GROWTH_ATTRIBUTION_ALLOWED_EVENTS = frozenset(
    {
        FUNNEL_EVENT_SIGNUP_COMPLETED,
        FUNNEL_EVENT_SESSION_ORIGIN_OBSERVED,
        FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
        FUNNEL_EVENT_PLAN_SELECTED,
        FUNNEL_EVENT_CHECKOUT_STARTED,
    }
)

# Chaves contratuais de metadata_json (sem schema sofisticado / sem PII livre).
ALLOWED_METADATA_KEYS = frozenset(
    {
        "page",
        "path",
        "content_id",
        "content_type",
        "task_type",
        "task_stage",
        "plan",
        "cta_id",
        "surface",
        "variant",
        "status",
        "error_code",
        "signup_method",
        # SCRUM-159 Lote 159A: snapshot de atribuicao first-party (somente backend/sessao)
        "growth_attribution",
        # legado ja usado em testes/instrumentacao atual
        "step",
        "count",
        "flags",
        # legado Agente Compara (upload)
        "table_id",
        "slot",
    }
)

# task_type controlados (Lote 3A+)
TASK_TYPE_CLEIDE_AUDIT = "cleide_audit"
TASK_TYPE_AGENTE_COMPARA = "agente_compara"
# task_type controlados (Lote 3B)
TASK_TYPE_CLEIDE_BI = "cleide_bi"
TASK_TYPE_CLEIDE_BI_CHAT = "cleide_bi_chat"
TASK_TYPE_ROBERTO_BI = "roberto_bi"
TASK_TYPE_ROBERTO_CHAT = "roberto_chat"
# task_type controlados (Lote 3C)
TASK_TYPE_JULIA_CHAT = "julia_chat"
TASK_TYPE_FREIGHT_QUERY = "freight_query"
TASK_TYPE_CLEIDE_AUDIT_CHAT = "cleide_audit_chat"
TASK_TYPE_AGENTE_COMPARA_CHAT = "agente_compara_chat"
TASK_TYPE_ONBOARDING_DISCOVERY = "onboarding_discovery"

# task_types elegiveis para o marco global first_relevant_task_completed (SCRUM-148).
# Explicitamente fora: onboarding_discovery.
FIRST_RELEVANT_ELIGIBLE_TASK_TYPES = frozenset(
    {
        TASK_TYPE_CLEIDE_AUDIT,
        TASK_TYPE_AGENTE_COMPARA,
        TASK_TYPE_CLEIDE_BI,
        TASK_TYPE_CLEIDE_BI_CHAT,
        TASK_TYPE_ROBERTO_BI,
        TASK_TYPE_ROBERTO_CHAT,
        TASK_TYPE_JULIA_CHAT,
        TASK_TYPE_FREIGHT_QUERY,
        TASK_TYPE_CLEIDE_AUDIT_CHAT,
        TASK_TYPE_AGENTE_COMPARA_CHAT,
    }
)

SIGNUP_METHOD_PASSWORD = "password"
SIGNUP_METHOD_GOOGLE = "google"
ALLOWED_SIGNUP_METHODS = frozenset({SIGNUP_METHOD_PASSWORD, SIGNUP_METHOD_GOOGLE})
ALLOWED_CONTENT_TYPES = frozenset({"noticia", "artigo"})

# Planos comerciais rastreaveis no Growth (SCRUM-148 Lote 4A). Sem free.
ALLOWED_GROWTH_CHECKOUT_PLANS = frozenset({"starter", "pro", "multiuser"})

# Endpoints Flask (request.endpoint) das paginas que estendem base.html e
# emitem page_view. Nao inclui /acesso-desktop (fora do shell Growth).
ALLOWED_GROWTH_PAGE_VIEW_PAGES = frozenset(
    {
        "index",
        "feed",
        "login",
        "request_password_reset",
        "reset_password",
        "complete_profile",
        "chat_julia",
        "fretes",
        "controle_estoque",
        "insights_frete",
        "detalhe_noticia",
        "newsletter_cancelar",
        "admin_promocao_confirmar",
        "admin_revogacao_confirmar",
        "user.perfil",
        "user.contrate_plano",
        "user.regularizar_pagamento",
        "agente_compara.agente_compara_page",
        "cleide.auditoria_frete",
        "cleide.cleide_auditoria",
        "multiuser_painel.gestao_multiuser",
        "multiuser_convite.visualizar_convite",
    }
)

# Cutover Growth paid (SCRUM-148 Lote 4B). Ausente/invalido => nao emite paid.
GROWTH_PAID_CUTOVER_AT_ENV = "GROWTH_PAID_CUTOVER_AT"
TIPO_FATO_STRIPE_INVOICE_PAID = "stripe_invoice_paid"
TIPO_FATO_STRIPE_CHECKOUT_SESSION_CREATED = "stripe_checkout_session_created"
PROVIDER_STRIPE_GROWTH = "stripe"

# Classificacao controlada de dominio comercial (tri-state). paid exige recurring_normal.
BILLING_DOMAIN_RECURRING_NORMAL = "recurring_normal"
BILLING_DOMAIN_MULTIUSER_EXTRAORDINARY = "multiuser_extraordinary"
BILLING_DOMAIN_UNKNOWN = "unknown"
ALLOWED_BILLING_DOMAINS = frozenset(
    {
        BILLING_DOMAIN_RECURRING_NORMAL,
        BILLING_DOMAIN_MULTIUSER_EXTRAORDINARY,
        BILLING_DOMAIN_UNKNOWN,
    }
)

def is_meta_pixel_allowed(event_name: str) -> bool:
    """Autorizacao Meta Pixel: allowlist legada apenas. Default False para novos eventos."""
    return str(event_name or "").strip().lower() in META_PIXEL_ALLOWED_EVENTS


def is_allowed_growth_page_view(page: Any) -> bool:
    """page_view so aceita endpoint Flask publico controlado (nao URL/texto livre)."""
    if not isinstance(page, str):
        return False
    return page.strip() in ALLOWED_GROWTH_PAGE_VIEW_PAGES


def validate_growth_checkout_for_account(
    conta_id: Any,
    checkout_session_id: Any,
    plan: Any,
) -> bool:
    """
    Comprova checkout Stripe real ja persistido pelo backend para a conta.

    Fonte: MonetizacaoFato tipo stripe_checkout_session_created
    (correlation_key / external_event_id = session id; snapshot.plano_interno).

    Nao confia no browser; nao chama Stripe; nao inventa por proximidade temporal.
    """
    try:
        cid = int(conta_id)
    except (TypeError, ValueError):
        return False
    if cid <= 0:
        return False
    if not isinstance(checkout_session_id, str) or not isinstance(plan, str):
        return False
    session_id = checkout_session_id.strip()
    plan_n = plan.strip().lower()
    if not session_id or plan_n not in ALLOWED_GROWTH_CHECKOUT_PLANS:
        return False
    try:
        fato = (
            MonetizacaoFato.query.filter(
                MonetizacaoFato.tipo_fato == TIPO_FATO_STRIPE_CHECKOUT_SESSION_CREATED,
                MonetizacaoFato.conta_id == cid,
                db.or_(
                    MonetizacaoFato.correlation_key == session_id,
                    MonetizacaoFato.external_event_id == session_id,
                ),
            )
            .order_by(MonetizacaoFato.id.desc())
            .first()
        )
    except Exception:
        logger.debug(
            "growth checkout validate: consulta MonetizacaoFato falhou",
            exc_info=True,
        )
        return False
    if fato is None:
        return False
    snap = _json_object_or_none(fato.snapshot_normalizado_json)
    if not isinstance(snap, dict):
        return False
    plano_fato = str(snap.get("plano_interno") or "").strip().lower()
    return plano_fato == plan_n


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


def _normalize_optional_id(value: Any, field_name: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} invalido") from exc


def _validate_identity_for_event(
    event_name: str,
    *,
    user_id: int | None,
    conta_id: int | None,
    franquia_id: int | None,
) -> None:
    if event_name in LEGACY_FULL_IDENTITY_REQUIRED_EVENTS:
        if user_id is None:
            raise ValueError("user_id e obrigatorio")
        if conta_id is None:
            raise ValueError("conta_id e obrigatorio")
        if franquia_id is None:
            raise ValueError("franquia_id e obrigatorio")
        return

    if event_name in USER_REQUIRED_EVENTS and user_id is None:
        raise ValueError("user_id e obrigatorio")

    if event_name in ACCOUNT_REQUIRED_EVENTS and conta_id is None:
        raise ValueError("conta_id e obrigatorio")


def _validate_metadata_json(metadata_json: Any, *, event_name: str) -> Any:
    if metadata_json is None:
        metadata = None
    else:
        try:
            json.dumps(metadata_json, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("metadata_json deve ser compativel com JSON") from exc
        if not isinstance(metadata_json, dict):
            raise ValueError("metadata_json deve ser um objeto")
        unknown = set(metadata_json.keys()) - ALLOWED_METADATA_KEYS
        if unknown:
            raise ValueError("metadata_json contem chaves nao permitidas")
        metadata = dict(metadata_json)
        if "growth_attribution" in metadata:
            if event_name not in GROWTH_ATTRIBUTION_ALLOWED_EVENTS:
                raise ValueError(
                    "growth_attribution nao permitido para este evento"
                )
            from app.services.growth_attribution_service import (
                sanitize_growth_attribution_snapshot,
            )

            require_current_only = event_name == FUNNEL_EVENT_SESSION_ORIGIN_OBSERVED
            sanitized = sanitize_growth_attribution_snapshot(
                metadata.get("growth_attribution"),
                require_current_only=require_current_only,
            )
            if sanitized is None:
                raise ValueError("growth_attribution invalido")
            metadata["growth_attribution"] = sanitized

    if event_name == FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED:
        raw_task_type = metadata.get("task_type") if isinstance(metadata, dict) else None
        if not isinstance(raw_task_type, str) or not raw_task_type.strip():
            raise ValueError("task_type e obrigatorio em metadata_json")

    if event_name == FUNNEL_EVENT_SESSION_ORIGIN_OBSERVED:
        if not isinstance(metadata, dict) or "growth_attribution" not in metadata:
            raise ValueError("growth_attribution e obrigatorio")

    return metadata


def _has_same_idempotent_payload(
    existing: FunnelEvent,
    *,
    event_name: str,
    source: str,
    user_id: int | None,
    conta_id: int | None,
    franquia_id: int | None,
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
    user_id: int | None = None,
    conta_id: int | None = None,
    franquia_id: int | None = None,
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
    Registra um evento append-only de funil (Growth interno + legado).

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

    user_id_i = _normalize_optional_id(user_id, "user_id")
    conta_id_i = _normalize_optional_id(conta_id, "conta_id")
    franquia_id_i = _normalize_optional_id(franquia_id, "franquia_id")
    _validate_identity_for_event(
        event_name_n,
        user_id=user_id_i,
        conta_id=conta_id_i,
        franquia_id=franquia_id_i,
    )

    metadata_n = _validate_metadata_json(metadata_json, event_name=event_name_n)

    row: FunnelEvent | None = None
    orm_session = db.session()
    started_outer_tx = False
    try:
        if not orm_session.in_transaction():
            orm_session.begin()
            started_outer_tx = True
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
                metadata_json=metadata_n,
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


def _safe_rollback() -> None:
    try:
        db.session.rollback()
    except Exception:
        logger.exception("funnel_event rollback failed")


def try_record_funnel_event(**kwargs: Any) -> dict[str, Any] | None:
    """Fail-open: falha de analytics nao interrompe o produto."""
    try:
        result = record_funnel_event(**kwargs)
        db.session.commit()
        return result
    except Exception:
        _safe_rollback()
        logger.exception(
            "funnel_event persist failed event_name=%s source=%s",
            kwargs.get("event_name"),
            kwargs.get("source"),
        )
        return None


def _is_desktop_access_admin_test_mode() -> bool:
    try:
        from app.services.admin_desktop_access_test_service import (
            is_desktop_access_admin_test_mode_for_current_user,
        )

        return bool(is_desktop_access_admin_test_mode_for_current_user())
    except Exception:
        return False


def _eligible_task_type_from_metadata(metadata_json: Any) -> str | None:
    if not isinstance(metadata_json, dict):
        return None
    raw = metadata_json.get("task_type")
    if not isinstance(raw, str):
        return None
    task_type = raw.strip()
    if task_type in FIRST_RELEVANT_ELIGIBLE_TASK_TYPES:
        return task_type
    return None


def _first_relevant_idempotency_key(user_id: int) -> str:
    return f"growth:first_relevant_task_completed:user:{int(user_id)}"


def _find_first_eligible_task_completed(user_id: int) -> FunnelEvent | None:
    rows = (
        FunnelEvent.query.filter_by(
            user_id=int(user_id),
            event_name=FUNNEL_EVENT_TASK_COMPLETED,
        )
        .order_by(FunnelEvent.occurred_at.asc(), FunnelEvent.id.asc())
        .all()
    )
    for row in rows:
        if _eligible_task_type_from_metadata(row.metadata_json):
            return row
    return None


def _first_relevant_conversion_metadata(
    *,
    task_type: str,
    selected_completion_id: int | None,
    triggering_completion_id: int | None,
    conversion_snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    Metadata do marco first_relevant (SCRUM-159 Lote 159B).

    Anexa growth_attribution.current_session_origin somente quando a conclusao
    selecionada e a mesma recem-criada que disparou o ensure e o snapshot e
    valido (somente current; nunca first_touch). Fail-open: snapshot invalido
    nao impede o marco.
    """
    metadata: dict[str, Any] = {"task_type": task_type}
    if (
        triggering_completion_id is None
        or selected_completion_id is None
        or int(selected_completion_id) != int(triggering_completion_id)
        or not conversion_snapshot
    ):
        return metadata
    try:
        from app.services.growth_attribution_service import (
            sanitize_growth_attribution_snapshot,
        )

        snap = sanitize_growth_attribution_snapshot(
            conversion_snapshot,
            require_current_only=True,
        )
        if snap and "current_session_origin" in snap and "first_touch" not in snap:
            metadata["growth_attribution"] = snap
    except Exception:
        logger.debug(
            "first_relevant: snapshot attribution ignorado selected=%s",
            selected_completion_id,
            exc_info=True,
        )
    return metadata


def _ensure_first_relevant_task_completed(
    user_id: int,
    *,
    triggering_completion_id: int | None = None,
    conversion_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """
    Cria no maximo um first_relevant_task_completed global por usuario.

    Ordem: lock User -> checa marco existente -> primeira conclusao elegivel
    -> cria marco -> commit. Fail-open: rollback so desta transacao; nao propaga.

    Attribution (159B): so anexa current_session_origin quando
    selected.id == triggering_completion_id e o snapshot e valido.
    Recovery / created=False passam snapshot=None e nao atribuem sessao atual.

    Retorno apos commit bem-sucedido (SCRUM-157B):
      {"event", "created", "selected_completion_id"}
    Sem candidato / falha: None.
    """
    try:
        db.session.query(User).filter(User.id == int(user_id)).with_for_update().one()

        existing = FunnelEvent.query.filter_by(
            user_id=int(user_id),
            event_name=FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
        ).first()
        if existing is not None:
            db.session.commit()
            return {
                "event": existing,
                "created": False,
                "selected_completion_id": None,
            }

        first = _find_first_eligible_task_completed(int(user_id))
        if first is None:
            db.session.commit()
            return None

        task_type = _eligible_task_type_from_metadata(first.metadata_json)
        if task_type is None:
            db.session.commit()
            return None

        selected_completion_id = getattr(first, "id", None)
        metadata = _first_relevant_conversion_metadata(
            task_type=task_type,
            selected_completion_id=selected_completion_id,
            triggering_completion_id=triggering_completion_id,
            conversion_snapshot=conversion_snapshot,
        )

        recorded = record_funnel_event(
            event_name=FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
            source=FUNNEL_SOURCE_GROWTH,
            user_id=int(user_id),
            idempotency_key=_first_relevant_idempotency_key(int(user_id)),
            occurred_at=first.occurred_at,
            metadata_json=metadata,
        )
        db.session.commit()
        return {
            "event": recorded.get("event"),
            "created": bool(recorded.get("created") is True),
            "selected_completion_id": (
                int(selected_completion_id) if selected_completion_id is not None else None
            ),
        }
    except Exception:
        _safe_rollback()
        logger.exception(
            "first_relevant_task_completed ensure failed user_id=%s",
            user_id,
        )
        return None


def _is_external_first_relevant_eligible(
    *,
    task_result: dict[str, Any],
    marker_result: dict[str, Any] | None,
    allow_external_first_relevant: bool,
) -> bool:
    """
    As cinco condicoes absolutas para stagear first_relevant externo (157B).

    Qualquer falha: Growth segue; nenhum external_event.
    """
    if allow_external_first_relevant is not True:
        return False
    if task_result.get("created") is not True:
        return False
    if not isinstance(marker_result, dict):
        return False
    if marker_result.get("created") is not True:
        return False
    marker_event = marker_result.get("event")
    if marker_event is None or getattr(marker_event, "id", None) is None:
        return False
    selected = marker_result.get("selected_completion_id")
    task_event = task_result.get("event")
    if selected is None or task_event is None:
        return False
    task_id = getattr(task_event, "id", None)
    if task_id is None:
        return False
    try:
        if int(selected) != int(task_id):
            return False
    except (TypeError, ValueError):
        return False
    try:
        from flask import has_request_context

        if not has_request_context():
            return False
    except Exception:
        return False
    return True


def _try_stage_first_relevant_external(marker_event: Any) -> None:
    """Stage envelope minimo na session; fail-open. Rejected nao mantem pendencia."""
    try:
        from flask import has_request_context, request, session

        if not has_request_context():
            return
        from app.privacy_marketing import (
            PRIVACY_MARKETING_COOKIE_NAME,
            PRIVACY_MARKETING_STATE_REJECTED,
            parse_privacy_marketing_cookie,
            store_pending_first_relevant_external_event,
        )
        from app.services.growth_external_event_service import (
            build_external_event_envelope,
        )

        privacy_state = parse_privacy_marketing_cookie(
            request.cookies.get(PRIVACY_MARKETING_COOKIE_NAME)
        )
        if privacy_state == PRIVACY_MARKETING_STATE_REJECTED:
            store_pending_first_relevant_external_event(session, None)
            return

        envelope = build_external_event_envelope(marker_event)
        if envelope is None:
            return
        store_pending_first_relevant_external_event(session, envelope)
    except Exception:
        logger.debug(
            "first_relevant externo: stage falhou (fail-open)",
            exc_info=True,
        )


def try_record_growth_task_event(
    *,
    event_name: str,
    source: str,
    task_type: str,
    idempotency_key: str,
    task_stage: str | None = None,
    error_code: str | None = None,
    user: Any = None,
    user_id: int | None = None,
    conta_id: int | None = None,
    franquia_id: int | None = None,
    document_id: str | None = None,
    audit_batch_id: str | None = None,
    comparison_id: str | None = None,
    execution_id: str | None = None,
    correlation_id: str | None = None,
    allow_external_first_relevant: bool = True,
) -> dict[str, Any] | None:
    """
    Fail-open para marcos task_* do Growth (SCRUM-148 Lote 3A+).

    Apos task_completed persistido (inclusive created=False), tenta ensure do
    marco global first_relevant_task_completed.

    SCRUM-157B: se elegivel, stageia envelope externo do marco (nao do task)
    em slot de session dedicado. Retorno principal permanece {event, created}.
    allow_external_first_relevant=False bloqueia so o browser externo (ex.:
    idempotent_replay do Agente Compara); Growth/recovery seguem.
    """
    if _is_desktop_access_admin_test_mode():
        return None

    event_n = str(event_name or "").strip().lower()
    if event_n not in {
        FUNNEL_EVENT_TASK_PREPARATION_STARTED,
        FUNNEL_EVENT_TASK_PREPARATION_COMPLETED,
        FUNNEL_EVENT_TASK_STARTED,
        FUNNEL_EVENT_TASK_COMPLETED,
        FUNNEL_EVENT_TASK_FAILED,
    }:
        logger.warning("try_record_growth_task_event ignorado: event_name invalido=%s", event_name)
        return None

    task_type_n = str(task_type or "").strip()
    if not task_type_n:
        logger.warning("try_record_growth_task_event ignorado: task_type ausente")
        return None

    if user is not None:
        identity = resolve_funnel_identity_from_user(user)
    elif user_id is not None or conta_id is not None or franquia_id is not None:
        try:
            identity = {
                "user_id": _normalize_optional_id(user_id, "user_id"),
                "conta_id": _normalize_optional_id(conta_id, "conta_id"),
                "franquia_id": _normalize_optional_id(franquia_id, "franquia_id"),
            }
        except ValueError:
            identity = {"user_id": None, "conta_id": None, "franquia_id": None}
    else:
        try:
            from flask_login import current_user as _current_user

            identity = resolve_funnel_identity_from_user(_current_user)
        except Exception:
            identity = {"user_id": None, "conta_id": None, "franquia_id": None}

    metadata: dict[str, Any] = {"task_type": task_type_n}
    stage_n = str(task_stage or "").strip()
    if stage_n:
        metadata["task_stage"] = stage_n
    if event_n == FUNNEL_EVENT_TASK_FAILED:
        code_n = str(error_code or "").strip()
        if code_n:
            metadata["error_code"] = code_n

    result = try_record_funnel_event(
        event_name=event_n,
        source=source,
        user_id=identity.get("user_id"),
        conta_id=identity.get("conta_id"),
        franquia_id=identity.get("franquia_id"),
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
        document_id=document_id,
        audit_batch_id=audit_batch_id,
        comparison_id=comparison_id,
        execution_id=execution_id,
        metadata_json=metadata,
    )

    if (
        result is not None
        and event_n == FUNNEL_EVENT_TASK_COMPLETED
        and result.get("event") is not None
    ):
        event = result["event"]
        uid = getattr(event, "user_id", None)
        if uid is not None and _eligible_task_type_from_metadata(
            getattr(event, "metadata_json", None)
        ):
            triggering_completion_id: int | None = None
            conversion_snapshot: dict[str, Any] | None = None
            # Attribution so quando task_completed foi RECÉM-CRIADO nesta execucao.
            # created=False: recovery sem snapshot da sessao atual.
            if result.get("created") is True:
                eid = getattr(event, "id", None)
                if eid is not None:
                    triggering_completion_id = int(eid)
                try:
                    from app.services.growth_attribution_service import (
                        snapshot_current_session_origin_for_persist,
                    )

                    conversion_snapshot = snapshot_current_session_origin_for_persist()
                except Exception:
                    logger.debug(
                        "task_completed: snapshot attribution indisponivel user_id=%s",
                        uid,
                        exc_info=True,
                    )
                    conversion_snapshot = None
            marker_result = _ensure_first_relevant_task_completed(
                int(uid),
                triggering_completion_id=triggering_completion_id,
                conversion_snapshot=conversion_snapshot,
            )
            if _is_external_first_relevant_eligible(
                task_result=result,
                marker_result=marker_result,
                allow_external_first_relevant=allow_external_first_relevant,
            ):
                _try_stage_first_relevant_external(marker_result.get("event"))
                # CAPI do marco (nao do task); mesmas 5 condicoes + gates CAPI.
                # Recovery / replay / idempotent_replay: elegibilidade false => zero HTTP.
                try:
                    from app.services.growth_external_server_service import (
                        try_send_growth_meta_capi,
                    )

                    try_send_growth_meta_capi(marker_result.get("event"))
                except Exception:
                    logger.debug(
                        "first_relevant meta capi: falha fail-open",
                        exc_info=True,
                    )

    return result


def resolve_funnel_identity_from_user(user: Any) -> dict[str, int | None]:
    """Identidade interna a partir do usuario autenticado; anonimo = nulls."""
    if user is None or not getattr(user, "is_authenticated", False):
        return {"user_id": None, "conta_id": None, "franquia_id": None}
    try:
        return {
            "user_id": _normalize_optional_id(getattr(user, "id", None), "user_id"),
            "conta_id": _normalize_optional_id(getattr(user, "conta_id", None), "conta_id"),
            "franquia_id": _normalize_optional_id(
                getattr(user, "franquia_id", None), "franquia_id"
            ),
        }
    except ValueError:
        return {"user_id": None, "conta_id": None, "franquia_id": None}


def try_record_signup_started(*, signup_method: str = SIGNUP_METHOD_PASSWORD) -> None:
    method = str(signup_method or "").strip().lower()
    if method not in ALLOWED_SIGNUP_METHODS:
        method = SIGNUP_METHOD_PASSWORD
    try_record_funnel_event(
        event_name=FUNNEL_EVENT_SIGNUP_STARTED,
        source=FUNNEL_SOURCE_GROWTH,
        user_id=None,
        conta_id=None,
        franquia_id=None,
        idempotency_key=f"growth:signup_started:{method}:{uuid4()}",
        metadata_json={"signup_method": method},
    )


def try_record_signup_completed(user: Any, *, signup_method: str) -> dict[str, Any] | None:
    method = str(signup_method or "").strip().lower()
    if method not in ALLOWED_SIGNUP_METHODS:
        logger.warning("signup_completed ignorado: signup_method invalido=%s", signup_method)
        return None
    user_id = getattr(user, "id", None) if user is not None else None
    if user_id is None:
        logger.warning("signup_completed ignorado: user_id ausente")
        return None
    metadata: dict[str, Any] = {"signup_method": method}
    try:
        from app.services.growth_attribution_service import (
            snapshot_growth_attribution_for_persist,
        )

        # Snapshot somente da sessao/backend; nunca do browser.
        # Usa new_user.id (objeto persistido), nao current_user.
        snap = snapshot_growth_attribution_for_persist(
            include_first_touch=True,
            include_current=True,
        )
        if snap:
            metadata["growth_attribution"] = snap
    except Exception:
        logger.debug(
            "signup_completed: snapshot attribution indisponivel user_id=%s",
            user_id,
            exc_info=True,
        )
    # Usuario acabou de ser persistido; identidade a partir do objeto (nao exige login).
    return try_record_funnel_event(
        event_name=FUNNEL_EVENT_SIGNUP_COMPLETED,
        source=FUNNEL_SOURCE_GROWTH,
        user_id=int(user_id),
        conta_id=_normalize_optional_id(getattr(user, "conta_id", None), "conta_id"),
        franquia_id=_normalize_optional_id(getattr(user, "franquia_id", None), "franquia_id"),
        idempotency_key=f"growth:signup_completed:{method}:{int(user_id)}",
        metadata_json=metadata,
    )


def try_record_session_origin_observed(user: Any) -> None:
    """
    Emite session_origin_observed para usuario autenticado com origem conhecida.

    Metadata contem apenas growth_attribution.current_session_origin.
    Nao exporta Meta/GA4/Ads. Fail-open.
    """
    user_id = getattr(user, "id", None) if user is not None else None
    if user_id is None:
        return
    try:
        from app.services.growth_attribution_service import (
            snapshot_growth_attribution_for_persist,
        )

        snap = snapshot_growth_attribution_for_persist(
            include_first_touch=False,
            include_current=True,
        )
        if not snap or "current_session_origin" not in snap:
            return
        try_record_funnel_event(
            event_name=FUNNEL_EVENT_SESSION_ORIGIN_OBSERVED,
            source=FUNNEL_SOURCE_GROWTH,
            user_id=int(user_id),
            conta_id=_normalize_optional_id(getattr(user, "conta_id", None), "conta_id"),
            franquia_id=_normalize_optional_id(
                getattr(user, "franquia_id", None), "franquia_id"
            ),
            idempotency_key=(
                f"growth:session_origin_observed:user:{int(user_id)}:occurrence:{uuid4()}"
            ),
            metadata_json={"growth_attribution": snap},
        )
    except Exception:
        logger.exception(
            "session_origin_observed persist failed user_id=%s",
            user_id,
        )


def record_completion_with_first_audit(
    *,
    source: str,
    user_id: int,
    conta_id: int,
    franquia_id: int,
    freight_idempotency_key: str,
    first_audit_idempotency_key: str,
    occurred_at: datetime | None = None,
    correlation_id: str | None = None,
    document_id: str | None = None,
    audit_batch_id: str | None = None,
    comparison_id: str | None = None,
    execution_id: str | None = None,
    metadata_json: Any = None,
) -> dict[str, Any]:
    """Registra conclusao e primeira auditoria global de forma atomica."""
    current_occurred_at = occurred_at or utcnow_naive()
    user = (
        db.session.query(User)
        .filter(User.id == int(user_id))
        .with_for_update()
        .one()
    )
    freight_result = record_funnel_event(
        event_name=FUNNEL_EVENT_FREIGHT_CALCULATED,
        source=source,
        user_id=int(user_id),
        conta_id=int(conta_id),
        franquia_id=int(franquia_id),
        idempotency_key=freight_idempotency_key,
        occurred_at=current_occurred_at,
        correlation_id=correlation_id,
        document_id=document_id,
        audit_batch_id=audit_batch_id,
        comparison_id=comparison_id,
        execution_id=execution_id,
        metadata_json=metadata_json,
    )

    first_audit_result: dict[str, Any] | None = None
    if user.first_audit_completed_at is None:
        first_audit_result = record_funnel_event(
            event_name=FUNNEL_EVENT_FIRST_AUDIT_COMPLETED,
            source=source,
            user_id=int(user_id),
            conta_id=int(conta_id),
            franquia_id=int(franquia_id),
            idempotency_key=first_audit_idempotency_key,
            occurred_at=current_occurred_at,
            correlation_id=correlation_id,
            document_id=document_id,
            audit_batch_id=audit_batch_id,
            comparison_id=comparison_id,
            execution_id=execution_id,
            metadata_json=metadata_json,
        )
        if first_audit_result.get("created") is True:
            user.first_audit_completed_at = current_occurred_at

    db.session.flush()
    return {
        "freight_calculated": freight_result,
        "first_audit_completed": first_audit_result,
        "is_first_audit": bool(first_audit_result and first_audit_result.get("created") is True),
    }


# --- Growth paid (SCRUM-148 Lote 4B) -----------------------------------------


@dataclass(frozen=True)
class _GrowthPaidInvoiceCandidate:
    invoice_id: str
    created_at: datetime
    plan: str | None
    user_id: int | None
    franquia_id: int | None


def _growth_paid_idempotency_key(conta_id: int) -> str:
    return f"growth:paid:conta:{int(conta_id)}"


def _parse_growth_paid_cutover_at(raw: str) -> datetime | None:
    """Parse ISO-8601 UTC explicito. Sem timezone => invalido. Sem fallback para agora."""
    txt = (raw or "").strip()
    if not txt:
        return None
    try:
        if txt.endswith("Z"):
            txt = txt[:-1] + "+00:00"
        dt = datetime.fromisoformat(txt)
    except Exception:
        logger.warning(
            "GROWTH_PAID_CUTOVER_AT invalido: parse falhou value=%r",
            (raw or "")[:80],
        )
        return None
    if dt.tzinfo is None:
        logger.warning("GROWTH_PAID_CUTOVER_AT invalido: timezone ausente")
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def resolve_growth_paid_cutover_at() -> datetime | None:
    """
    Cutover Growth paid a partir do ambiente.

    Ausente ou invalido => None (fail-closed: nao emitir paid).
    Nunca usa horario atual como fallback.
    """
    raw = (os.getenv(GROWTH_PAID_CUTOVER_AT_ENV) or "").strip()
    if not raw:
        return None
    return _parse_growth_paid_cutover_at(raw)


def _json_object_or_none(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _invoice_object_from_fato_payload(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if isinstance(data, dict):
        obj = data.get("object")
        if isinstance(obj, dict):
            return obj
    # Alguns fatos podem guardar o objeto invoice direto.
    if str(payload.get("object") or "").strip().lower() == "invoice":
        return payload
    return None


def _norm_stripe_id(value: Any) -> str | None:
    """Fail-closed: apenas IDs Stripe textuais (ou dict com id textual)."""
    if isinstance(value, dict):
        value = value.get("id")
    if value is None or value == "":
        return None
    # bool e numerico generico viram texto enganoso ("True", "1"); IDs Stripe sao str.
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _invoice_created_at(obj: dict[str, Any]) -> datetime | None:
    if "created" not in obj:
        return None
    value = obj.get("created")
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    if isinstance(value, (int, float)):
        num = float(value)
        if not math.isfinite(num) or num <= 0:
            return None
        try:
            return datetime.fromtimestamp(num, tz=timezone.utc).replace(tzinfo=None)
        except Exception:
            return None
    if isinstance(value, str):
        txt = value.strip()
        if not txt:
            return None
        try:
            if txt.endswith("Z"):
                txt = txt[:-1] + "+00:00"
            dt = datetime.fromisoformat(txt)
            if dt.tzinfo is None:
                return None
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        except Exception:
            return None
    return None


def _amount_paid_positive(obj: dict[str, Any]) -> bool:
    raw = obj.get("amount_paid")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return False
    num = float(raw)
    if not math.isfinite(num):
        return False
    return num > 0


def _pagamento_invoice_comprovado(obj: dict[str, Any]) -> bool:
    status = str(obj.get("status") or "").strip().lower()
    if status == "paid":
        return True
    paid_flag = obj.get("paid")
    return paid_flag is True


def _billing_reason_subscription_create(obj: dict[str, Any]) -> bool:
    reason = obj.get("billing_reason")
    if not isinstance(reason, str):
        return False
    return reason.strip().lower() == "subscription_create"


def _subscription_id_from_invoice_obj(obj: dict[str, Any], fato: MonetizacaoFato) -> str | None:
    sub = _norm_stripe_id(obj.get("subscription"))
    if sub:
        return sub
    parent = obj.get("parent")
    if isinstance(parent, dict):
        details = parent.get("subscription_details")
        if isinstance(details, dict):
            sub = _norm_stripe_id(details.get("subscription"))
            if sub:
                return sub
    lines = obj.get("lines")
    data = lines.get("data") if isinstance(lines, dict) else lines if isinstance(lines, list) else None
    if isinstance(data, list):
        for line in data:
            if not isinstance(line, dict):
                continue
            sub = _norm_stripe_id(line.get("subscription"))
            if sub:
                return sub
    return _norm_stripe_id(fato.subscription_id)


def normalize_billing_domain(value: Any) -> str:
    """Aceita somente enum controlado; qualquer outro valor => unknown."""
    if not isinstance(value, str):
        return BILLING_DOMAIN_UNKNOWN
    text = value.strip().lower()
    if text in ALLOWED_BILLING_DOMAINS:
        return text
    return BILLING_DOMAIN_UNKNOWN


def billing_domain_from_fato(fato: MonetizacaoFato) -> str:
    """Lê dominio do snapshot tecnico. Ausencia/invalido => unknown."""
    snap = _json_object_or_none(fato.snapshot_normalizado_json)
    if not isinstance(snap, dict):
        return BILLING_DOMAIN_UNKNOWN
    return normalize_billing_domain(snap.get("dominio"))


def _flow_type_meta_controlado(*sources: Any) -> str | None:
    """Extrai flow_type somente se for string tecnica; ignora objetos livres."""
    for source in sources:
        if not isinstance(source, dict):
            continue
        raw = source.get("flow_type")
        if isinstance(raw, str):
            text = raw.strip().lower()
            if text:
                return text
    return None


def _evidencia_positiva_recorrente_normal(
    evento: dict[str, Any] | None,
    obj: dict[str, Any],
) -> bool:
    """Prova positiva de fluxo recorrente; nao usa ausencia de extraordinary."""
    reason = obj.get("billing_reason")
    if isinstance(reason, str) and reason.strip().lower().startswith("subscription_"):
        return True
    mode = obj.get("mode")
    if isinstance(mode, str) and mode.strip().lower() == "subscription":
        return True
    obj_type = obj.get("object")
    if isinstance(obj_type, str) and obj_type.strip().lower() == "subscription":
        return True
    event_type = ""
    if isinstance(evento, dict):
        raw_type = evento.get("type")
        if isinstance(raw_type, str):
            event_type = raw_type.strip().lower()
    if event_type.startswith("customer.subscription."):
        return True
    return False


def classificar_billing_domain_ingestao(
    evento: dict[str, Any] | None,
    object_data: dict[str, Any] | None,
) -> str:
    """
    Classificacao tri-state no momento da ingestao comercial.

    - multiuser_extraordinary: prova positiva extraordinaria
    - recurring_normal: prova positiva de fluxo recorrente E nao extraordinario
    - unknown: ausencia, excecao ou conflito de evidencias

    Nao infere recurring_normal apenas pela ausencia de marcador extraordinary.
    """
    obj = object_data if isinstance(object_data, dict) else {}
    ev = evento if isinstance(evento, dict) else {}
    meta_sources = (
        obj.get("metadata") if isinstance(obj.get("metadata"), dict) else None,
        ev.get("metadata") if isinstance(ev.get("metadata"), dict) else None,
    )
    flow_meta = _flow_type_meta_controlado(*meta_sources)
    try:
        from app.services.conta_multiuser_cobranca_extraordinaria_service import (
            evento_eh_cobranca_extraordinaria_multiuser,
        )

        is_extra = bool(evento_eh_cobranca_extraordinaria_multiuser(ev, obj))
    except Exception:
        logger.exception("billing domain: classificador extraordinario falhou")
        return BILLING_DOMAIN_UNKNOWN

    has_recurring = _evidencia_positiva_recorrente_normal(ev, obj)

    if flow_meta == BILLING_DOMAIN_MULTIUSER_EXTRAORDINARY and not is_extra:
        return BILLING_DOMAIN_UNKNOWN
    if is_extra and has_recurring and flow_meta not in (
        None,
        BILLING_DOMAIN_MULTIUSER_EXTRAORDINARY,
    ):
        return BILLING_DOMAIN_UNKNOWN
    if is_extra:
        return BILLING_DOMAIN_MULTIUSER_EXTRAORDINARY
    if has_recurring:
        return BILLING_DOMAIN_RECURRING_NORMAL
    return BILLING_DOMAIN_UNKNOWN


def _price_id_from_invoice_obj(obj: dict[str, Any], fato: MonetizacaoFato) -> str | None:
    price = _norm_stripe_id(fato.price_id)
    if price:
        return price
    lines = obj.get("lines")
    data = lines.get("data") if isinstance(lines, dict) else lines if isinstance(lines, list) else None
    if not isinstance(data, list):
        return None
    for line in data:
        if not isinstance(line, dict):
            continue
        p = line.get("price")
        pid = _norm_stripe_id(p.get("id") if isinstance(p, dict) else p)
        if pid:
            return pid
        pricing = line.get("pricing")
        if isinstance(pricing, dict):
            details = pricing.get("price_details")
            if isinstance(details, dict):
                pid = _norm_stripe_id(details.get("price"))
                if pid:
                    return pid
    return None


def _plan_from_invoice_only(obj: dict[str, Any], fato: MonetizacaoFato) -> str | None:
    """Resolve plan so a partir da invoice/fato. Sem plano atual da conta."""
    meta = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
    plano_meta = str(meta.get("plano_interno") or "").strip().lower()
    if plano_meta in ALLOWED_GROWTH_CHECKOUT_PLANS:
        return plano_meta

    price_id = _price_id_from_invoice_obj(obj, fato)
    if not price_id:
        return None
    try:
        from app.services import plano_service

        resolved = plano_service.resolver_plano_por_gateway_price_id_admin(
            provider=PROVIDER_STRIPE_GROWTH,
            price_id=price_id,
        )
    except Exception:
        return None
    if not resolved:
        return None
    codigo = str(resolved.get("plano_codigo") or "").strip().lower()
    if codigo in ALLOWED_GROWTH_CHECKOUT_PLANS:
        return codigo
    return None


def _resolve_invoice_id_for_growth(fato: MonetizacaoFato, obj: dict[str, Any]) -> str | None:
    invoice_id = _norm_stripe_id(fato.invoice_id)
    if invoice_id:
        return invoice_id
    if str(obj.get("object") or "").strip().lower() == "invoice":
        return _norm_stripe_id(obj.get("id"))
    return _norm_stripe_id(obj.get("invoice")) or _norm_stripe_id(obj.get("id"))


def _candidate_from_stripe_invoice_paid_fato(
    fato: MonetizacaoFato,
    *,
    cutover_at: datetime,
) -> _GrowthPaidInvoiceCandidate | None:
    """Fail-closed: qualquer ambiguidade/ausencia => None."""
    if (fato.provider or "").strip().lower() != PROVIDER_STRIPE_GROWTH:
        return None
    if (fato.tipo_fato or "").strip() != TIPO_FATO_STRIPE_INVOICE_PAID:
        return None
    if fato.conta_id is None:
        return None

    payload = _json_object_or_none(fato.payload_bruto_sanitizado_json)
    obj = _invoice_object_from_fato_payload(payload)
    if obj is None:
        return None

    # paid exige dominio explicitamente recorrente normal no snapshot.
    if billing_domain_from_fato(fato) != BILLING_DOMAIN_RECURRING_NORMAL:
        return None

    invoice_id = _resolve_invoice_id_for_growth(fato, obj)
    if not invoice_id:
        return None

    if not _pagamento_invoice_comprovado(obj):
        return None
    if not _amount_paid_positive(obj):
        return None
    if not _billing_reason_subscription_create(obj):
        return None
    if not _subscription_id_from_invoice_obj(obj, fato):
        return None

    created_at = _invoice_created_at(obj)
    if created_at is None:
        return None
    if created_at < cutover_at:
        return None

    plan = _plan_from_invoice_only(obj, fato)
    user_id = int(fato.usuario_id) if fato.usuario_id is not None else None
    franquia_id = int(fato.franquia_id) if fato.franquia_id is not None else None
    return _GrowthPaidInvoiceCandidate(
        invoice_id=invoice_id,
        created_at=created_at,
        plan=plan,
        user_id=user_id,
        franquia_id=franquia_id,
    )


def _invoice_identity_probe(
    fato: MonetizacaoFato,
) -> tuple[str | None, datetime | None]:
    """Extrai (invoice_id, created) sem filtros de elegibilidade/cutover."""
    if (fato.provider or "").strip().lower() != PROVIDER_STRIPE_GROWTH:
        return None, None
    if (fato.tipo_fato or "").strip() != TIPO_FATO_STRIPE_INVOICE_PAID:
        return None, None
    payload = _json_object_or_none(fato.payload_bruto_sanitizado_json)
    obj = _invoice_object_from_fato_payload(payload)
    if obj is None:
        invoice_id = _norm_stripe_id(fato.invoice_id)
        return invoice_id, None
    invoice_id = _resolve_invoice_id_for_growth(fato, obj)
    return invoice_id, _invoice_created_at(obj)


def _find_first_growth_paid_invoice(
    conta_id: int,
    *,
    cutover_at: datetime,
) -> _GrowthPaidInvoiceCandidate | None:
    """
    Deduplica por invoice_id e escolhe a primeira elegivel por:
    1) invoice.created ASC
    2) invoice_id deterministico

    Divergencia de created (incl. fatos pre-cutover) descarta a invoice
    de forma definitiva para todos os fatos restantes.
    """
    fatos = (
        MonetizacaoFato.query.filter_by(
            conta_id=int(conta_id),
            provider=PROVIDER_STRIPE_GROWTH,
            tipo_fato=TIPO_FATO_STRIPE_INVOICE_PAID,
        )
        .all()
    )

    created_by_invoice: dict[str, datetime] = {}
    ambiguous_invoices: set[str] = set()
    for fato in fatos:
        invoice_id, created_at = _invoice_identity_probe(fato)
        if not invoice_id or created_at is None:
            continue
        if invoice_id in ambiguous_invoices:
            continue
        known = created_by_invoice.get(invoice_id)
        if known is None:
            created_by_invoice[invoice_id] = created_at
            continue
        if known != created_at:
            ambiguous_invoices.add(invoice_id)
            created_by_invoice.pop(invoice_id, None)

    by_invoice: dict[str, _GrowthPaidInvoiceCandidate] = {}
    for fato in fatos:
        candidate = _candidate_from_stripe_invoice_paid_fato(fato, cutover_at=cutover_at)
        if candidate is None:
            continue
        if candidate.invoice_id in ambiguous_invoices:
            continue
        existing = by_invoice.get(candidate.invoice_id)
        if existing is None:
            by_invoice[candidate.invoice_id] = candidate
            continue
        # Mesma invoice com evidencias divergentes de created => ambigua, definitiva.
        if existing.created_at != candidate.created_at:
            by_invoice.pop(candidate.invoice_id, None)
            ambiguous_invoices.add(candidate.invoice_id)
            continue
        # Preferir plan/user/franquia quando a nova evidencia preenche lacunas.
        plan = existing.plan or candidate.plan
        user_id = existing.user_id if existing.user_id is not None else candidate.user_id
        franquia_id = (
            existing.franquia_id if existing.franquia_id is not None else candidate.franquia_id
        )
        by_invoice[candidate.invoice_id] = _GrowthPaidInvoiceCandidate(
            invoice_id=existing.invoice_id,
            created_at=existing.created_at,
            plan=plan,
            user_id=user_id,
            franquia_id=franquia_id,
        )

    if not by_invoice:
        return None
    return sorted(
        by_invoice.values(),
        key=lambda c: (c.created_at, c.invoice_id),
    )[0]


def try_ensure_growth_paid_for_conta(conta_id: int) -> dict[str, Any] | None:
    """
    Avalia e, se elegivel, registra no maximo um FunnelEvent paid por conta.

    paid representa a primeira invoice elegível observada por Growth desde
    GROWTH_PAID_CUTOVER_AT; não representa necessariamente a primeira compra
    histórica da conta.

    Deve ser chamado somente apos commit comercial explicito. Fail-open:
    falha faz rollback so desta avaliacao Growth e nao propaga.
    """
    try:
        cid = int(conta_id)
    except (TypeError, ValueError):
        return None

    cutover_at = resolve_growth_paid_cutover_at()
    if cutover_at is None:
        return None

    try:
        db.session.query(Conta).filter(Conta.id == cid).with_for_update().one()

        existing = FunnelEvent.query.filter_by(
            conta_id=cid,
            event_name=FUNNEL_EVENT_PAID,
            source=FUNNEL_SOURCE_GROWTH,
        ).first()
        if existing is not None:
            db.session.commit()
            return {"created": False, "event": existing}

        first = _find_first_growth_paid_invoice(cid, cutover_at=cutover_at)
        if first is None:
            db.session.commit()
            return None

        metadata: dict[str, Any] | None = None
        if first.plan:
            metadata = {"plan": first.plan}

        result = record_funnel_event(
            event_name=FUNNEL_EVENT_PAID,
            source=FUNNEL_SOURCE_GROWTH,
            user_id=first.user_id,
            conta_id=cid,
            franquia_id=first.franquia_id,
            idempotency_key=_growth_paid_idempotency_key(cid),
            occurred_at=first.created_at,
            correlation_id=first.invoice_id,
            metadata_json=metadata,
        )
        db.session.commit()
        return result
    except Exception:
        _safe_rollback()
        logger.exception("growth paid ensure failed conta_id=%s", conta_id)
        return None