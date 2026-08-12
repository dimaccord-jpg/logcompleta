"""
Etapa 4 — reconciliação Lead (campanha) → User por e-mail.

Observação posterior ao cadastro: não acopla auth/register/OAuth.
converted_at usa User.created_at (timestamp canônico de criação da conta).
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from sqlalchemy import func

from app.extensions import db
from app.models import Lead, User, utcnow_naive
from app.services.lead_acquisition_service import CAMPANHA_ACESSO_DESKTOP

logger = logging.getLogger(__name__)

STATUS_CONVERTED = "converted"
STATUS_ALREADY = "already_converted"
STATUS_NO_USER = "no_user"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_SKIPPED_CAMPAIGN = "skipped_wrong_campaign"


def _normalize_email(email: str) -> str:
    """Trim + lower; não reescreve e-mails já persistidos."""
    return (email or "").strip().lower()


def _canonical_converted_at(user: User):
    """
    Semântica de converted_at:
    User.created_at é o timestamp canônico de criação (default na coluna,
    sem onupdate; preenchido no insert local e OAuth).
    Se ausente em registro anômalo, cai para o momento da detecção.
    """
    if user.created_at is not None:
        return user.created_at
    return utcnow_naive()


def find_users_by_normalized_email(email_normalized: str) -> list[User]:
    """Match case-insensitive; pode retornar >1 em anomalia histórica."""
    if not email_normalized:
        return []
    return (
        User.query.filter(func.lower(User.email) == email_normalized)
        .order_by(User.id.asc())
        .all()
    )


def _users_grouped_by_normalized_email(emails: set[str]) -> dict[str, list[User]]:
    """Consulta Users em lote para os e-mails normalizados informados."""
    grouped: dict[str, list[User]] = defaultdict(list)
    if not emails:
        return grouped
    rows = (
        User.query.filter(func.lower(User.email).in_(list(emails)))
        .order_by(User.id.asc())
        .all()
    )
    for user in rows:
        grouped[_normalize_email(user.email)].append(user)
    return grouped


def reconcile_lead(
    lead: Lead,
    *,
    users_for_email: list[User] | None = None,
) -> str:
    """
    Associa Lead → User por e-mail se ainda não convertido.

    Não faz commit (caller/lote é dono da transação).
    Retorna: converted | already_converted | no_user | ambiguous | skipped_wrong_campaign
    """
    if lead.acquisition_campaign != CAMPANHA_ACESSO_DESKTOP:
        return STATUS_SKIPPED_CAMPAIGN

    if lead.converted_user_id is not None:
        return STATUS_ALREADY

    email_norm = _normalize_email(lead.email)
    if users_for_email is None:
        users_for_email = find_users_by_normalized_email(email_norm)

    if not users_for_email:
        return STATUS_NO_USER

    if len(users_for_email) > 1:
        # Anomalia histórica: não escolher User arbitrário; sem PII no log.
        logger.warning(
            "Reconciliação desktop_access: e-mail ambíguo (múltiplos Users). "
            "lead_id=%s user_count=%s — Lead ignorado nesta execução.",
            lead.id,
            len(users_for_email),
        )
        return STATUS_AMBIGUOUS

    user = users_for_email[0]
    lead.converted_user_id = user.id
    lead.converted_at = _canonical_converted_at(user)
    return STATUS_CONVERTED


def reconcile_desktop_access_leads() -> dict[str, Any]:
    """
    Reconcilia Leads da campanha ainda sem converted_user_id.

    Transaction ownership: este lote faz um único commit ao final.
    Em falha de commit, rollback da sessão.
    Inclui opt-out (conversão é métrica, não e-mail).
    """
    stats = {
        "examined": 0,
        "converted": 0,
        "already_converted": 0,
        "no_user": 0,
        "ambiguous": 0,
        "skipped_wrong_campaign": 0,
    }

    candidates = (
        Lead.query.filter(
            Lead.acquisition_campaign == CAMPANHA_ACESSO_DESKTOP,
            Lead.converted_user_id.is_(None),
        )
        .order_by(Lead.id.asc())
        .all()
    )
    stats["examined"] = len(candidates)
    if not candidates:
        return stats

    email_set = {_normalize_email(lead.email) for lead in candidates}
    grouped = _users_grouped_by_normalized_email(email_set)

    changed = False
    for lead in candidates:
        status = reconcile_lead(
            lead,
            users_for_email=grouped.get(_normalize_email(lead.email), []),
        )
        if status == STATUS_CONVERTED:
            stats["converted"] += 1
            changed = True
        elif status == STATUS_ALREADY:
            stats["already_converted"] += 1
        elif status == STATUS_NO_USER:
            stats["no_user"] += 1
        elif status == STATUS_AMBIGUOUS:
            stats["ambiguous"] += 1
        elif status == STATUS_SKIPPED_CAMPAIGN:
            stats["skipped_wrong_campaign"] += 1

    if changed:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception(
                "Falha ao gravar reconciliação desktop_access (examined=%s)",
                stats["examined"],
            )
            raise

    logger.info(
        "Reconciliação desktop_access: examined=%s converted=%s no_user=%s "
        "ambiguous=%s already=%s",
        stats["examined"],
        stats["converted"],
        stats["no_user"],
        stats["ambiguous"],
        stats["already_converted"],
    )
    return stats
