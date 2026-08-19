"""
Backfill controlado de NewsletterSubscription a partir de User.subscribes_to_newsletter.

Default: DRY RUN. Apply somente com --apply explícito.

Fonte permitida: User.subscribes_to_newsletter == True.
Não consulta Lead. Não infere consentimento de campanha/opt-out.
Não cria CommunicationSuppression.
Não reativa NewsletterSubscription já cancelada: unsubscribed_at vence a flag User.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import inspect as sa_inspect

from app.extensions import db
from app.models import NewsletterSubscription, User
from app.services.newsletter_subscription_service import (
    SOURCE_USER_PREFERENCE_BACKFILL,
    STATUS_CREATED,
    normalize_email,
    subscribe,
)

logger = logging.getLogger(__name__)

TABLE_NAME = "newsletter_subscription"

STATUS_WOULD_CREATE = "would_create"
STATUS_EXISTING_ACTIVE = "existing_active"
STATUS_SKIPPED_EXISTING_UNSUBSCRIBED = "skipped_existing_unsubscribed"


class NewsletterBackfillAborted(RuntimeError):
    """Abort controlado: nenhum write deve permanecer."""


@dataclass
class NewsletterBackfillReport:
    mode: str
    users_subscribed_true: int = 0
    unique_emails: int = 0
    duplicate_email_groups: int = 0
    would_create: int = 0
    created: int = 0
    existing_active: int = 0
    skipped_existing_unsubscribed: int = 0
    errors: int = 0
    elapsed_ms: int = 0
    aborted: bool = False
    abort_reason: str | None = None


def run_backfill(*, apply: bool = False) -> NewsletterBackfillReport:
    """
    apply=False (default): somente contagens, zero writes.
    apply=True: cria faltantes por e-mail único, uma transação, idempotente.
    Não reativa row com unsubscribed_at; dry-run e apply usam a mesma decisão.
    """
    started = time.monotonic()
    mode = "APPLY" if apply else "DRY_RUN"
    report = NewsletterBackfillReport(mode=mode)
    logger.info("newsletter subscription backfill MODE=%s", mode)

    try:
        _require_table()
        groups = _group_subscribed_users()
        report.users_subscribed_true = sum(len(ids) for ids in groups.values())
        report.unique_emails = len(groups)
        report.duplicate_email_groups = sum(1 for ids in groups.values() if len(ids) > 1)

        for email_normalized in groups:
            outcome = _classify_or_persist(email_normalized, apply=apply)
            _count_outcome(report, outcome)

        if apply:
            db.session.commit()
        else:
            db.session.rollback()
    except NewsletterBackfillAborted as exc:
        db.session.rollback()
        report.aborted = True
        report.abort_reason = str(exc)
        report.elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.error(
            "newsletter subscription backfill STATUS=ABORTED MODE=%s",
            mode,
        )
        raise
    except Exception as exc:
        db.session.rollback()
        report.errors += 1
        report.aborted = True
        report.abort_reason = "Erro inesperado. Backfill abortado."
        report.elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.error(
            "newsletter subscription backfill STATUS=ABORTED MODE=%s error_type=%s",
            mode,
            type(exc).__name__,
        )
        raise NewsletterBackfillAborted(report.abort_reason) from exc

    report.elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "newsletter subscription backfill STATUS=OK MODE=%s elapsed_ms=%s",
        mode,
        report.elapsed_ms,
    )
    return report


def format_report(report: NewsletterBackfillReport) -> str:
    """Resumo agregado sem PII."""
    lines = [
        f"MODE={report.mode}",
        f"users_subscribed_true={report.users_subscribed_true}",
        f"unique_emails={report.unique_emails}",
        f"duplicate_email_groups={report.duplicate_email_groups}",
        f"would_create={report.would_create}",
        f"created={report.created}",
        f"existing_active={report.existing_active}",
        f"skipped_existing_unsubscribed={report.skipped_existing_unsubscribed}",
        f"errors={report.errors}",
        f"elapsed_ms={report.elapsed_ms}",
        f"aborted={str(report.aborted).lower()}",
    ]
    if report.abort_reason:
        lines.append(f"abort_reason={report.abort_reason}")
    return "\n".join(lines)


def emit_backfill_cli(*, apply: bool, echo) -> int:
    echo("MODE=APPLY" if apply else "MODE=DRY_RUN")
    try:
        report = run_backfill(apply=apply)
    except NewsletterBackfillAborted as exc:
        echo("STATUS=ABORTED")
        echo(str(exc))
        return 1
    echo(format_report(report))
    echo("STATUS=OK")
    return 0


def register_newsletter_subscription_backfill_command(app) -> None:
    """Registra o comando Flask CLI. Default: dry-run."""
    import click

    @app.cli.command("newsletter-subscription-backfill")
    @click.option(
        "--apply",
        is_flag=True,
        default=False,
        help="Persiste inscricoes a partir de User.subscribes_to_newsletter=True. Default: dry-run.",
    )
    def _newsletter_subscription_backfill(apply: bool) -> None:
        """
        Backfill idempotente de NewsletterSubscription.

        Fonte unica: User.subscribes_to_newsletter == True.
        Nao consulta Lead. Default: DRY RUN.

        DRY RUN:
          flask --app app.web newsletter-subscription-backfill

        APPLY:
          flask --app app.web newsletter-subscription-backfill --apply
        """
        exit_code = emit_backfill_cli(apply=apply, echo=click.echo)
        if exit_code:
            raise SystemExit(exit_code)


def _group_subscribed_users() -> dict[str, list[int]]:
    """Agrupa Users com flag True por e-mail normalizado. Não lê Lead."""
    rows = (
        User.query.with_entities(User.id, User.email)
        .filter(User.subscribes_to_newsletter.is_(True))
        .order_by(User.id.asc())
        .all()
    )
    groups: dict[str, list[int]] = defaultdict(list)
    for user_id, email in rows:
        normalized = normalize_email(email)
        if not normalized:
            continue
        groups[normalized].append(int(user_id))
    return dict(groups)


def _classify_or_persist(email_normalized: str, *, apply: bool) -> str:
    existing = NewsletterSubscription.query.filter_by(email=email_normalized).first()
    if existing is None:
        if not apply:
            return STATUS_WOULD_CREATE
        result = subscribe(
            email_normalized,
            source=SOURCE_USER_PREFERENCE_BACKFILL,
            commit=False,
            nested=False,
        )
        if result.status != STATUS_CREATED:
            raise NewsletterBackfillAborted(
                "Falha ao criar newsletter subscription no backfill. Transacao revertida."
            )
        return STATUS_CREATED

    if existing.unsubscribed_at is None:
        return STATUS_EXISTING_ACTIVE
    return STATUS_SKIPPED_EXISTING_UNSUBSCRIBED


def _count_outcome(report: NewsletterBackfillReport, outcome: str) -> None:
    if outcome == STATUS_WOULD_CREATE:
        report.would_create += 1
    elif outcome == STATUS_CREATED:
        report.created += 1
    elif outcome == STATUS_EXISTING_ACTIVE:
        report.existing_active += 1
    elif outcome == STATUS_SKIPPED_EXISTING_UNSUBSCRIBED:
        report.skipped_existing_unsubscribed += 1


def _require_table() -> None:
    if not _table_exists():
        raise NewsletterBackfillAborted(
            "Tabela newsletter_subscription indisponivel. "
            "A migration precisa estar aplicada antes do backfill."
        )


def _table_exists() -> bool:
    inspector = sa_inspect(db.engine)
    return TABLE_NAME in inspector.get_table_names()
