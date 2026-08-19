"""
Backfill controlado e idempotente de CommunicationSuppression a partir de Lead.

Não executa sozinho: o caller precisa pedir APPLY explicitamente.
Default operacional: DRY RUN (zero writes).

Não altera Lead, User nem newsletter.
Não cria purpose newsletter.
Não duplica HMAC: consulta/persistência passam pelo service 4C-A.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import inspect as sa_inspect, or_

from app.extensions import db
from app.models import Lead
from app.services.communication_suppression_service import (
    PURPOSE_ACTIVATION,
    PURPOSE_PRE_REGISTRATION,
    SOURCE_ACTIVATION_UNSUBSCRIBE,
    SOURCE_CAMPAIGN_UNSUBSCRIBE,
    SOURCE_HISTORICAL_ACTIVATION_OPT_OUT,
    SOURCE_HISTORICAL_CAMPAIGN_OPT_OUT,
    get_suppression_snapshot,
    get_suppression_snapshot_for_hmac,
    is_suppression_enabled,
    normalize_email_hmac,
    suppress_email,
    suppress_email_hmac,
)
from app.services.lead_email_state import is_lead_email_minimized

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 100
TABLE_NAME = "communication_suppression"

STATUS_CREATED = "created"
STATUS_EXISTING = "existing"
STATUS_CONFLICT = "conflict"
STATUS_WOULD_CREATE = "would_create"
STATUS_WOULD_CONFLICT = "would_conflict"

# Source histórica vs source viva do 4A/4C-A: mesmo evento semântico.
# Timestamp igual + source compatível → EXISTING (não CONFLICT).
_COMPATIBLE_SOURCES = {
    SOURCE_HISTORICAL_CAMPAIGN_OPT_OUT: frozenset(
        {
            SOURCE_HISTORICAL_CAMPAIGN_OPT_OUT,
            SOURCE_CAMPAIGN_UNSUBSCRIBE,
        }
    ),
    SOURCE_HISTORICAL_ACTIVATION_OPT_OUT: frozenset(
        {
            SOURCE_HISTORICAL_ACTIVATION_OPT_OUT,
            SOURCE_ACTIVATION_UNSUBSCRIBE,
        }
    ),
}


class SuppressionBackfillAborted(RuntimeError):
    """Abort controlado: o batch atual deve ser revertido e a operação parar."""


@dataclass(frozen=True)
class ExpectedSuppression:
    purpose: str
    suppressed_at: datetime
    source: str


@dataclass(frozen=True)
class _LeadOptOut:
    id: int
    email: str
    email_hmac: str | None
    opt_out_at: datetime | None
    activation_opt_out_at: datetime | None


@dataclass
class BackfillReport:
    mode: str
    leads_scanned: int = 0
    leads_with_opt_out: int = 0
    expected_pre_registration: int = 0
    expected_activation: int = 0
    created_pre_registration: int = 0
    created_activation: int = 0
    existing_pre_registration: int = 0
    existing_activation: int = 0
    conflicts_pre_registration: int = 0
    conflicts_activation: int = 0
    would_create_pre_registration: int = 0
    would_create_activation: int = 0
    would_conflict_pre_registration: int = 0
    would_conflict_activation: int = 0
    errors: int = 0
    batches: int = 0
    elapsed_ms: int = 0
    aborted: bool = False
    abort_reason: str | None = None


def expected_suppressions_for_lead(lead) -> list[ExpectedSuppression]:
    """Mapeamento histórico oficial. Não interpreta newsletter."""
    expected: list[ExpectedSuppression] = []
    if lead.opt_out_at is not None:
        expected.append(
            ExpectedSuppression(
                purpose=PURPOSE_PRE_REGISTRATION,
                suppressed_at=lead.opt_out_at,
                source=SOURCE_HISTORICAL_CAMPAIGN_OPT_OUT,
            )
        )
    activation = _activation_historical(lead.opt_out_at, lead.activation_opt_out_at)
    if activation is not None:
        expected.append(
            ExpectedSuppression(
                purpose=PURPOSE_ACTIVATION,
                suppressed_at=activation[0],
                source=activation[1],
            )
        )
    return expected


def run_backfill(*, apply: bool = False, batch_size: int = DEFAULT_BATCH_SIZE) -> BackfillReport:
    """
    Percorre Leads com opt-out histórico e sincroniza CommunicationSuppression.

    apply=False (default): somente calcula/relata.
    apply=True: persiste faltantes, first-write-wins, uma transação por batch.
    """
    started = time.monotonic()
    mode = "APPLY" if apply else "DRY_RUN"
    report = BackfillReport(mode=mode)
    logger.info(
        "Communication suppression backfill MODE=%s batch_size=%s",
        mode,
        batch_size,
    )

    try:
        _require_secret()
        _require_table()
        if batch_size < 1:
            raise SuppressionBackfillAborted("batch_size invalido")

        last_id = 0
        while True:
            candidates = _fetch_batch(after_id=last_id, batch_size=batch_size)
            if not candidates:
                break
            report.batches += 1
            logger.info(
                "Communication suppression backfill batch MODE=%s batch=%s size=%s",
                mode,
                report.batches,
                len(candidates),
            )
            _close_read_transaction()
            try:
                if apply:
                    _begin_batch_transaction()
                _process_batch(candidates, apply=apply, report=report)
                if apply:
                    db.session.commit()
                else:
                    db.session.rollback()
            except SuppressionBackfillAborted:
                db.session.rollback()
                db.session.expunge_all()
                raise
            except Exception as exc:
                db.session.rollback()
                db.session.expunge_all()
                report.errors += 1
                logger.error(
                    "Communication suppression backfill aborting batch: error_type=%s",
                    type(exc).__name__,
                )
                raise SuppressionBackfillAborted(
                    "Erro inesperado no batch. Transacao revertida. Backfill abortado."
                ) from exc
            last_id = candidates[-1].id
    except SuppressionBackfillAborted as exc:
        report.aborted = True
        report.abort_reason = str(exc)
        report.elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.error(
            "Communication suppression backfill STATUS=ABORTED MODE=%s reason=%s",
            mode,
            str(exc),
        )
        raise
    except Exception as exc:
        db.session.rollback()
        report.errors += 1
        report.aborted = True
        report.abort_reason = "Erro inesperado. Backfill abortado."
        report.elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.error(
            "Communication suppression backfill STATUS=ABORTED MODE=%s error_type=%s",
            mode,
            type(exc).__name__,
        )
        raise SuppressionBackfillAborted(report.abort_reason) from exc

    report.elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "Communication suppression backfill STATUS=OK MODE=%s batches=%s elapsed_ms=%s",
        mode,
        report.batches,
        report.elapsed_ms,
    )
    return report


def format_report(report: BackfillReport) -> str:
    """Resumo agregado sem PII."""
    lines = [
        f"MODE={report.mode}",
        f"leads_scanned={report.leads_scanned}",
        f"leads_with_opt_out={report.leads_with_opt_out}",
        f"expected_pre_registration={report.expected_pre_registration}",
        f"expected_activation={report.expected_activation}",
        f"created_pre_registration={report.created_pre_registration}",
        f"created_activation={report.created_activation}",
        f"existing_pre_registration={report.existing_pre_registration}",
        f"existing_activation={report.existing_activation}",
        f"conflicts_pre_registration={report.conflicts_pre_registration}",
        f"conflicts_activation={report.conflicts_activation}",
        f"would_create_pre_registration={report.would_create_pre_registration}",
        f"would_create_activation={report.would_create_activation}",
        f"would_conflict_pre_registration={report.would_conflict_pre_registration}",
        f"would_conflict_activation={report.would_conflict_activation}",
        f"errors={report.errors}",
        f"batches={report.batches}",
        f"elapsed_ms={report.elapsed_ms}",
        f"aborted={str(report.aborted).lower()}",
    ]
    if report.abort_reason:
        lines.append(f"abort_reason={report.abort_reason}")
    return "\n".join(lines)


def emit_backfill_cli(*, apply: bool, batch_size: int, echo) -> int:
    """Entrypoint de CLI: MODE no início, sem prompt, sem PII. 0=ok, 1=abort."""
    echo("MODE=APPLY" if apply else "MODE=DRY_RUN")
    try:
        report = run_backfill(apply=apply, batch_size=batch_size)
    except SuppressionBackfillAborted as exc:
        echo("STATUS=ABORTED")
        echo(str(exc))
        return 1
    echo(format_report(report))
    echo("STATUS=OK")
    return 0


def _activation_historical(
    opt_out_at: datetime | None,
    activation_opt_out_at: datetime | None,
) -> tuple[datetime, str] | None:
    """Timestamp mais antigo; empate → source histórica de campaign opt-out."""
    candidates: list[tuple[datetime, str, int]] = []
    if opt_out_at is not None:
        candidates.append((opt_out_at, SOURCE_HISTORICAL_CAMPAIGN_OPT_OUT, 0))
    if activation_opt_out_at is not None:
        candidates.append((activation_opt_out_at, SOURCE_HISTORICAL_ACTIVATION_OPT_OUT, 1))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[2]))
    chosen = candidates[0]
    return chosen[0], chosen[1]


def _process_batch(candidates: list[_LeadOptOut], *, apply: bool, report: BackfillReport) -> None:
    for lead in candidates:
        report.leads_scanned += 1
        report.leads_with_opt_out += 1
        expected_rows = expected_suppressions_for_lead(lead)
        for expected in expected_rows:
            _count_expected(report, expected.purpose)
            outcome = _classify_or_persist(lead, expected, apply=apply)
            _count_outcome(report, expected.purpose, outcome)


def _classify_or_persist(
    lead: _LeadOptOut, expected: ExpectedSuppression, *, apply: bool
) -> str:
    if is_lead_email_minimized(lead):
        digest = normalize_email_hmac(lead.email_hmac)
        if digest is None:
            return STATUS_CONFLICT if apply else STATUS_WOULD_CONFLICT
        snapshot = get_suppression_snapshot_for_hmac(digest, expected.purpose)
        if snapshot is None:
            if not apply:
                return STATUS_WOULD_CREATE
            ok = suppress_email_hmac(
                digest,
                expected.purpose,
                expected.source,
                suppressed_at=expected.suppressed_at,
                commit=False,
                nested=False,
            )
            if not ok:
                raise SuppressionBackfillAborted(
                    "Falha ao persistir suppression no backfill. Transacao revertida."
                )
            return STATUS_CREATED
    else:
        snapshot = get_suppression_snapshot(lead.email, expected.purpose)
        if snapshot is None:
            if not apply:
                return STATUS_WOULD_CREATE
            ok = suppress_email(
                lead.email,
                expected.purpose,
                expected.source,
                suppressed_at=expected.suppressed_at,
                commit=False,
                nested=False,
            )
            if not ok:
                raise SuppressionBackfillAborted(
                    "Falha ao persistir suppression no backfill. Transacao revertida."
                )
            return STATUS_CREATED

    if _matches_expected(snapshot.suppressed_at, snapshot.source, expected):
        return STATUS_EXISTING
    if apply:
        return STATUS_CONFLICT
    return STATUS_WOULD_CONFLICT


def _matches_expected(suppressed_at, source: str, expected: ExpectedSuppression) -> bool:
    return _timestamps_match(suppressed_at, expected.suppressed_at) and _source_compatible(
        source, expected.source
    )


def _timestamps_match(left, right) -> bool:
    if left == right:
        return True
    if left is None or right is None:
        return False
    try:
        return _as_naive(left) == _as_naive(right)
    except TypeError:
        return False


def _as_naive(value: datetime) -> datetime:
    if getattr(value, "tzinfo", None) is None:
        return value
    return value.replace(tzinfo=None)


def _source_compatible(existing_source: str, expected_source: str) -> bool:
    allowed = _COMPATIBLE_SOURCES.get(expected_source)
    if allowed is None:
        return existing_source == expected_source
    return existing_source in allowed


def _count_expected(report: BackfillReport, purpose: str) -> None:
    if purpose == PURPOSE_PRE_REGISTRATION:
        report.expected_pre_registration += 1
    elif purpose == PURPOSE_ACTIVATION:
        report.expected_activation += 1


def _count_outcome(report: BackfillReport, purpose: str, outcome: str) -> None:
    attr = {
        (PURPOSE_PRE_REGISTRATION, STATUS_CREATED): "created_pre_registration",
        (PURPOSE_ACTIVATION, STATUS_CREATED): "created_activation",
        (PURPOSE_PRE_REGISTRATION, STATUS_EXISTING): "existing_pre_registration",
        (PURPOSE_ACTIVATION, STATUS_EXISTING): "existing_activation",
        (PURPOSE_PRE_REGISTRATION, STATUS_CONFLICT): "conflicts_pre_registration",
        (PURPOSE_ACTIVATION, STATUS_CONFLICT): "conflicts_activation",
        (PURPOSE_PRE_REGISTRATION, STATUS_WOULD_CREATE): "would_create_pre_registration",
        (PURPOSE_ACTIVATION, STATUS_WOULD_CREATE): "would_create_activation",
        (PURPOSE_PRE_REGISTRATION, STATUS_WOULD_CONFLICT): "would_conflict_pre_registration",
        (PURPOSE_ACTIVATION, STATUS_WOULD_CONFLICT): "would_conflict_activation",
    }.get((purpose, outcome))
    if attr is None:
        raise SuppressionBackfillAborted("Outcome de backfill nao reconhecido")
    setattr(report, attr, getattr(report, attr) + 1)


def _fetch_batch(*, after_id: int, batch_size: int) -> list[_LeadOptOut]:
    rows = (
        Lead.query.filter(
            or_(
                Lead.opt_out_at.isnot(None),
                Lead.activation_opt_out_at.isnot(None),
            ),
            Lead.id > after_id,
        )
        .with_entities(
            Lead.id,
            Lead.email,
            Lead.email_hmac,
            Lead.opt_out_at,
            Lead.activation_opt_out_at,
        )
        .order_by(Lead.id.asc())
        .limit(batch_size)
        .all()
    )
    return [
        _LeadOptOut(
            id=row[0],
            email=row[1],
            email_hmac=row[2],
            opt_out_at=row[3],
            activation_opt_out_at=row[4],
        )
        for row in rows
    ]


def _close_read_transaction() -> None:
    """Fecha transação de leitura do cursor. Candidatos já estão em tuples."""
    db.session.rollback()


def _begin_batch_transaction() -> None:
    session = db.session()
    if not session.in_transaction():
        session.begin()


def _require_secret() -> None:
    if not is_suppression_enabled():
        raise SuppressionBackfillAborted(
            "COMMUNICATION_SUPPRESSION_HMAC_SECRET ausente ou vazio. "
            "Backfill recusa operar sem secret. Sem fallback legado."
        )


def _require_table() -> None:
    if not _table_exists():
        raise SuppressionBackfillAborted(
            "Tabela communication_suppression indisponivel. "
            "A migration precisa estar aplicada antes do backfill. "
            "Este comando nao cria a tabela nem executa Alembic."
        )


def _table_exists() -> bool:
    inspector = sa_inspect(db.engine)
    return TABLE_NAME in inspector.get_table_names()


def register_communication_suppression_backfill_command(app) -> None:
    """Registra o comando Flask CLI. Default: dry-run."""
    import click

    @app.cli.command("communication-suppression-backfill")
    @click.option(
        "--apply",
        is_flag=True,
        default=False,
        help="Persiste suppressions historicas. Default: dry-run (nao grava).",
    )
    @click.option(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        show_default=True,
        help="Tamanho do lote (cursor por Lead.id).",
    )
    def _communication_suppression_backfill(apply: bool, batch_size: int) -> None:
        """
        Backfill idempotente de CommunicationSuppression a partir de Lead.

        Default: DRY RUN. Nao altera Lead, User nem newsletter.
        Exige COMMUNICATION_SUPPRESSION_HMAC_SECRET. Nao executa migration.

        DRY RUN:
          flask --app app.web communication-suppression-backfill

        APPLY:
          flask --app app.web communication-suppression-backfill --apply
        """
        exit_code = emit_backfill_cli(
            apply=apply,
            batch_size=batch_size,
            echo=click.echo,
        )
        if exit_code:
            raise SystemExit(exit_code)
