"""
Autoridade operacional de destinatários da newsletter.

Não é Lead, campanha desktop, opt-out de ativação nem CommunicationSuppression.
Não guarda IP, User-Agent, nome, Conta, Franquia nem JSON livre.

commit=True: este helper conclui a transação (endpoint público).
commit=False: apenas muta/flush na sessão; o caller controla o commit
(cadastro/perfil, LGPD-R1, encerramento operacional).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from flask import current_app, has_app_context
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import NewsletterSubscription, User, utcnow_naive

logger = logging.getLogger(__name__)

SOURCE_PUBLIC_NEWSLETTER = NewsletterSubscription.SOURCE_PUBLIC_NEWSLETTER
SOURCE_USER_PREFERENCE = NewsletterSubscription.SOURCE_USER_PREFERENCE
SOURCE_USER_PREFERENCE_BACKFILL = NewsletterSubscription.SOURCE_USER_PREFERENCE_BACKFILL

STATUS_CREATED = "created"
STATUS_REACTIVATED = "reactivated"
STATUS_ALREADY_ACTIVE = "already_active"
STATUS_INVALID = "invalid"
STATUS_UNSUBSCRIBED = "unsubscribed"
STATUS_ALREADY_UNSUBSCRIBED = "already_unsubscribed"
STATUS_NOT_FOUND = "not_found"

PURPOSE_UNSUBSCRIBE = "newsletter_unsubscribe"
_UNSUBSCRIBE_SALT = "newsletter-unsubscribe-salt"

_ALLOWED_SOURCES = frozenset(
    {
        SOURCE_PUBLIC_NEWSLETTER,
        SOURCE_USER_PREFERENCE,
        SOURCE_USER_PREFERENCE_BACKFILL,
    }
)


@dataclass(frozen=True)
class SubscribeResult:
    status: str
    subscription: NewsletterSubscription | None = None


@dataclass(frozen=True)
class UnsubscribeResult:
    status: str
    subscription: NewsletterSubscription | None = None


def normalize_email(email: str) -> str:
    """Trim + lower — identidade única de NewsletterSubscription."""
    return (email or "").strip().lower()


def lookup(email: str) -> NewsletterSubscription | None:
    email_normalized = normalize_email(email)
    if not email_normalized:
        return None
    return NewsletterSubscription.query.filter_by(email=email_normalized).first()


def is_active_subscription(email: str) -> bool:
    row = lookup(email)
    return bool(row is not None and row.is_active)


def subscribe(
    email: str,
    *,
    source: str,
    commit: bool = True,
    now: datetime | None = None,
    nested: bool = True,
) -> SubscribeResult:
    """
    Cria ou reativa inscrição. Idempotente se já ativa.

    subscribed_at:
    - nova: agora
    - reinscrição após unsubscribe: agora da nova vigência
    - já ativa: inalterado
    """
    email_normalized = normalize_email(email)
    if not email_normalized:
        return SubscribeResult(status=STATUS_INVALID)
    if source not in _ALLOWED_SOURCES:
        logger.error("newsletter subscribe recusado: source nao permitido")
        return SubscribeResult(status=STATUS_INVALID)

    stamp = now if now is not None else utcnow_naive()
    existing = lookup(email_normalized)
    if existing is not None:
        return _apply_existing_subscribe(
            existing, source=source, stamp=stamp, commit=commit
        )

    row = NewsletterSubscription(
        email=email_normalized,
        subscribed_at=stamp,
        unsubscribed_at=None,
        source=source,
        created_at=stamp,
        updated_at=stamp,
    )
    try:
        if nested:
            with db.session.begin_nested():
                db.session.add(row)
                db.session.flush()
        else:
            db.session.add(row)
            db.session.flush()
    except IntegrityError:
        raced = lookup(email_normalized)
        if raced is not None:
            return _apply_existing_subscribe(
                raced, source=source, stamp=stamp, commit=commit
            )
        if commit:
            db.session.rollback()
        logger.error("newsletter subscribe IntegrityError sem row existente")
        raise
    except Exception:
        if commit:
            db.session.rollback()
        raise

    if commit:
        db.session.commit()
    logger.info("newsletter_subscription status=%s source=%s", STATUS_CREATED, source)
    return SubscribeResult(status=STATUS_CREATED, subscription=row)


def unsubscribe(
    email: str,
    *,
    commit: bool = True,
    sync_user_flag: bool = False,
    now: datetime | None = None,
) -> UnsubscribeResult:
    """
    Marca unsubscribed_at. Mantém o row. Não cria CommunicationSuppression nem Lead opt-out.

    sync_user_flag=True: se houver exatamente um User com o e-mail atual,
    User.subscribes_to_newsletter=False. Múltiplos matches: não inventa.
    """
    email_normalized = normalize_email(email)
    if not email_normalized:
        return UnsubscribeResult(status=STATUS_INVALID)

    stamp = now if now is not None else utcnow_naive()
    row = lookup(email_normalized)
    if row is None:
        if sync_user_flag:
            _sync_user_flag_off_if_unambiguous(email_normalized)
            if commit:
                db.session.commit()
        return UnsubscribeResult(status=STATUS_NOT_FOUND)

    if row.unsubscribed_at is None:
        row.unsubscribed_at = stamp
        row.updated_at = stamp
        status = STATUS_UNSUBSCRIBED
    else:
        status = STATUS_ALREADY_UNSUBSCRIBED

    if sync_user_flag:
        _sync_user_flag_off_if_unambiguous(email_normalized)

    db.session.flush()
    if commit:
        db.session.commit()
    logger.info("newsletter_subscription status=%s", status)
    return UnsubscribeResult(status=status, subscription=row)


def unsubscribe_for_user_before_deidentify(
    user: User,
    *,
    commit: bool = False,
) -> UnsubscribeResult:
    """
    Encerra inscrição ativa do e-mail atual do User.

    Deve rodar ANTES da desidentificação: depois o e-mail original some.
    Não commita. Não cria CommunicationSuppression. Não altera Lead.
    """
    return unsubscribe(user.email, commit=commit, sync_user_flag=False)


def generate_unsubscribe_token(subscription_id: int, *, secret_key: str) -> str:
    payload = {"sid": int(subscription_id), "purpose": PURPOSE_UNSUBSCRIBE}
    return _serializer(secret_key).dumps(payload)


def loads_unsubscribe_payload(token: str, *, secret_key: str) -> dict | None:
    try:
        data = _serializer(secret_key).loads(token)
    except BadSignature:
        return None
    except Exception:
        logger.warning("newsletter unsubscribe token load failed")
        return None
    if not isinstance(data, dict):
        return None
    if data.get("purpose") != PURPOSE_UNSUBSCRIBE:
        return None
    sid = data.get("sid")
    if not isinstance(sid, int):
        return None
    return {"sid": sid, "purpose": PURPOSE_UNSUBSCRIBE}


def resolve_subscription_for_unsubscribe_token(
    token: str,
    *,
    secret_key: str,
) -> NewsletterSubscription | None:
    payload = loads_unsubscribe_payload(token, secret_key=secret_key)
    if payload is None:
        return None
    return db.session.get(NewsletterSubscription, payload["sid"])


def signing_secret_key() -> str:
    """SECRET_KEY institucionalizado. Não reutiliza suppression HMAC. Sem fallback silencioso."""
    if has_app_context():
        secret = current_app.config.get("SECRET_KEY") or ""
        if isinstance(secret, bytes):
            secret = secret.decode("utf-8", errors="replace")
        secret = str(secret).strip()
        if secret:
            return secret
    raise RuntimeError(
        "SECRET_KEY ausente. Token de newsletter recusa operar sem secret."
    )


def _apply_existing_subscribe(
    row: NewsletterSubscription,
    *,
    source: str,
    stamp: datetime,
    commit: bool,
) -> SubscribeResult:
    if row.unsubscribed_at is None:
        if commit:
            db.session.commit()
        return SubscribeResult(status=STATUS_ALREADY_ACTIVE, subscription=row)

    row.subscribed_at = stamp
    row.unsubscribed_at = None
    row.source = source
    row.updated_at = stamp
    db.session.flush()
    if commit:
        db.session.commit()
    logger.info("newsletter_subscription status=%s source=%s", STATUS_REACTIVATED, source)
    return SubscribeResult(status=STATUS_REACTIVATED, subscription=row)


def _sync_user_flag_off_if_unambiguous(email_normalized: str) -> None:
    matches = (
        User.query.filter(func.lower(User.email) == email_normalized)
        .order_by(User.id.asc())
        .all()
    )
    if len(matches) != 1:
        if len(matches) > 1:
            logger.warning(
                "newsletter unsubscribe user_flag skip: ambiguous_user_count=%s",
                len(matches),
            )
        return
    matches[0].subscribes_to_newsletter = False


def _serializer(secret_key: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key, salt=_UNSUBSCRIBE_SALT)
