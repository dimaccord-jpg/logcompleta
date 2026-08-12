"""
Captura idempotente de Lead para campanha de aquisição.

Escopo mínimo da Etapa 1: persistir atribuição de campanha sem landing, e-mail CTA
ou reconciliação com User.
"""
from __future__ import annotations

import logging

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Lead, utcnow_naive

logger = logging.getLogger(__name__)

# Identidade estável da Landing Desktop (definida só no servidor).
CAMPANHA_ACESSO_DESKTOP = "desktop_access"
FONTE_LANDING = "landing"

_STATUS_CREATED = "created"
_STATUS_ASSIGNED = "assigned"
_STATUS_ALREADY_IN_CAMPAIGN = "already_in_campaign"
_STATUS_CAMPAIGN_MISMATCH = "campaign_mismatch"

_MAX_CAMPAIGN_LEN = 80
_MAX_SOURCE_LEN = 50


def _normalize_email(email: str) -> str:
    """Trim + lower para lookup e novos registros; não reescreve e-mails já persistidos."""
    return (email or "").strip().lower()


def _normalize_campaign(campaign: str) -> str:
    return (campaign or "").strip()[:_MAX_CAMPAIGN_LEN]


def _normalize_source(source: str | None) -> str | None:
    value = (source or "").strip()
    if not value:
        return None
    return value[:_MAX_SOURCE_LEN]


def _find_lead_by_email(email_normalized: str) -> Lead | None:
    return (
        Lead.query.filter(func.lower(Lead.email) == email_normalized)
        .order_by(Lead.id.asc())
        .first()
    )


def _apply_first_campaign_capture(lead: Lead, *, campaign: str, source: str | None) -> None:
    now = utcnow_naive()
    lead.acquisition_campaign = campaign
    lead.acquisition_source = source
    if lead.campaign_captured_at is None:
        lead.campaign_captured_at = now
    if lead.followup_count is None:
        lead.followup_count = 0


def capturar_lead_para_campanha(
    email: str,
    campaign: str,
    source: str | None = None,
) -> dict:
    """
    Captura idempotente de um Lead para uma campanha.

    Retorno:
      lead, status (created | assigned | already_in_campaign | campaign_mismatch)
    """
    email_normalized = _normalize_email(email)
    campaign_normalized = _normalize_campaign(campaign)
    source_normalized = _normalize_source(source)

    if not email_normalized:
        raise ValueError("E-mail é obrigatório.")
    if not campaign_normalized:
        raise ValueError("Campanha é obrigatória.")

    existing = _find_lead_by_email(email_normalized)
    if existing is None:
        lead = Lead(
            email=email_normalized,
            acquisition_campaign=campaign_normalized,
            acquisition_source=source_normalized,
            campaign_captured_at=utcnow_naive(),
            followup_count=0,
        )
        db.session.add(lead)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            existing = _find_lead_by_email(email_normalized)
            if existing is None:
                raise
            return capturar_lead_para_campanha(
                existing.email,
                campaign_normalized,
                source_normalized,
            )
        logger.info(
            "Lead aquisição criado: lead_id=%s campaign=%s",
            lead.id,
            campaign_normalized,
        )
        return {"lead": lead, "status": _STATUS_CREATED}

    if (
        existing.acquisition_campaign
        and existing.acquisition_campaign != campaign_normalized
    ):
        logger.info(
            "Lead já possui atribuição de campanha diferente: lead_id=%s existing=%s requested=%s",
            existing.id,
            existing.acquisition_campaign,
            campaign_normalized,
        )
        return {"lead": existing, "status": _STATUS_CAMPAIGN_MISMATCH}

    if (
        existing.acquisition_campaign == campaign_normalized
        and existing.campaign_captured_at is not None
    ):
        return {"lead": existing, "status": _STATUS_ALREADY_IN_CAMPAIGN}

    _apply_first_campaign_capture(
        existing,
        campaign=campaign_normalized,
        source=source_normalized,
    )
    db.session.commit()
    logger.info(
        "Lead aquisição atribuído: lead_id=%s campaign=%s",
        existing.id,
        campaign_normalized,
    )
    return {"lead": existing, "status": _STATUS_ASSIGNED}
