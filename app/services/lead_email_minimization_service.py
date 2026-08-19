"""
Minimização controlada de Lead.email (LEAD-R2).

Substitui plaintext por placeholder somente em Leads convertidos e
inequivocamente terminais, persistindo Lead.email_hmac do e-mail original.

Não cria CommunicationSuppression. Não minimiza newsletter. Não acopla LGPD-R1.
Default operacional: DRY RUN. Apply somente com --apply.

Elegibilidade (conservadora — falso negativo preferível a falso positivo):

1. converted_user_id obrigatório.
2. Activation terminal por estado persistido inequívoco:
   A) activation_ended_at e activation_ended_for_user_id == converted_user_id
   B) opt-out real com suppression correspondente já materializada
   C) User convertido operacionalmente encerrado (helper existente)
3. Opt-out histórico sem suppression correspondente: NÃO elegível (4C-B).

Deliberadamente fora do primeiro pacote (não são terminalidade persistida
inequívoca segundo os helpers atuais):
- first upload isolado (FunnelEvent sem activation_ended)
- somente activation_email_2_sent_at
- somente activation_email_1_sent_at
- follow-up esgotado em Lead não convertido
"""
from __future__ import annotations

import hmac
import logging
import time
from dataclasses import dataclass

from app.extensions import db
from app.models import Lead, User
from app.services.communication_suppression_service import (
    PURPOSE_ACTIVATION,
    PURPOSE_PRE_REGISTRATION,
    derive_email_hmac,
    has_email_hmac_suppression,
    is_suppression_enabled,
    is_valid_email_hmac,
    normalize_email_hmac,
)
from app.services.desktop_access_activation_email_service import (
    is_activation_journey_ended,
)
from app.services.lead_email_state import (
    is_lead_email_minimized,
    lead_minimized_email,
)
from app.services.user_operational_state import is_user_operationally_closed

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 100

STATUS_ELIGIBLE = "eligible"
STATUS_ALREADY_MINIMIZED = "already_minimized"
STATUS_CONFLICT = "conflict"
STATUS_SKIPPED_NOT_CONVERTED = "skipped_not_converted"
STATUS_SKIPPED_ACTIVATION_ACTIVE = "skipped_activation_active"
STATUS_SKIPPED_HISTORICAL_SUPPRESSION_PENDING = (
    "skipped_historical_suppression_pending"
)
STATUS_ERROR = "error"

REASON_ELIGIBLE = STATUS_ELIGIBLE
REASON_ALREADY_MINIMIZED = STATUS_ALREADY_MINIMIZED
REASON_CONFLICT_PLACEHOLDER_WITHOUT_HMAC = "placeholder_without_hmac"
REASON_CONFLICT_EMAIL_HMAC_INVALID = "conflict_email_hmac_invalid"
REASON_CONFLICT_EMAIL_HMAC_MISMATCH = "conflict_email_hmac_mismatch"
REASON_NOT_CONVERTED = STATUS_SKIPPED_NOT_CONVERTED
REASON_ACTIVATION_ACTIVE = STATUS_SKIPPED_ACTIVATION_ACTIVE
REASON_HISTORICAL_SUPPRESSION_PENDING = (
    STATUS_SKIPPED_HISTORICAL_SUPPRESSION_PENDING
)
REASON_ERROR = STATUS_ERROR


class LeadEmailMinimizationAborted(RuntimeError):
    """Abort controlado: o batch atual deve ser revertido e a operação parar."""


@dataclass(frozen=True)
class EligibilityDecision:
    status: str
    reason: str


@dataclass
class MinimizationReport:
    mode: str
    scanned: int = 0
    eligible: int = 0
    would_minimize: int = 0
    minimized: int = 0
    skipped_not_converted: int = 0
    skipped_activation_active: int = 0
    skipped_historical_suppression_pending: int = 0
    already_minimized: int = 0
    conflicts: int = 0
    errors: int = 0
    batches: int = 0
    elapsed_ms: int = 0
    suppression_hmac_unavailable: bool = False
    aborted: bool = False
    abort_reason: str | None = None


def evaluate_lead_email_minimization_eligibility(lead: Lead) -> EligibilityDecision:
    """Decisão consultiva. Lê DB (User/suppression); não grava."""
    if is_lead_email_minimized(lead):
        if is_valid_email_hmac(getattr(lead, "email_hmac", None)):
            return EligibilityDecision(
                status=STATUS_ALREADY_MINIMIZED,
                reason=REASON_ALREADY_MINIMIZED,
            )
        return EligibilityDecision(
            status=STATUS_CONFLICT,
            reason=REASON_CONFLICT_PLACEHOLDER_WITHOUT_HMAC,
        )

    if getattr(lead, "converted_user_id", None) is None:
        return EligibilityDecision(
            status=STATUS_SKIPPED_NOT_CONVERTED,
            reason=REASON_NOT_CONVERTED,
        )

    digest = _identity_hmac_for_plaintext_lead(lead)
    if _historical_suppression_pending(lead, digest):
        return EligibilityDecision(
            status=STATUS_SKIPPED_HISTORICAL_SUPPRESSION_PENDING,
            reason=REASON_HISTORICAL_SUPPRESSION_PENDING,
        )

    if not _activation_is_terminal(lead, digest):
        return EligibilityDecision(
            status=STATUS_SKIPPED_ACTIVATION_ACTIVE,
            reason=REASON_ACTIVATION_ACTIVE,
        )

    hmac_conflict = _plaintext_existing_hmac_conflict(lead)
    if hmac_conflict is not None:
        return hmac_conflict

    return EligibilityDecision(status=STATUS_ELIGIBLE, reason=REASON_ELIGIBLE)


def minimize_lead_email(lead: Lead) -> EligibilityDecision:
    """
    Aplica HMAC + placeholder no Lead da sessão. Sem commit.
    Idempotente se já minimizado corretamente.
    Estado inconsistente: conflict, sem mutação.
    """
    decision = evaluate_lead_email_minimization_eligibility(lead)
    if decision.status == STATUS_ALREADY_MINIMIZED:
        return decision
    if decision.status != STATUS_ELIGIBLE:
        return decision

    original = (lead.email or "")
    digest = derive_email_hmac(original)
    existing = normalize_email_hmac(getattr(lead, "email_hmac", None))
    placeholder = lead_minimized_email(int(lead.id))
    if existing is None:
        lead.email_hmac = digest
    lead.email = placeholder
    db.session.flush()
    return decision


def run_minimization(
    *,
    apply: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    lead_id: int | None = None,
) -> MinimizationReport:
    """
    Percorre Leads, avalia elegibilidade e, se apply, minimiza em batches.

    apply=False (default): zero writes, rollback defensivo.
    apply=True: exige secret; uma transação por batch; HMAC+placeholder atômicos.
    """
    started = time.monotonic()
    mode = "APPLY" if apply else "DRY_RUN"
    report = MinimizationReport(mode=mode)
    hmac_available = is_suppression_enabled()
    report.suppression_hmac_unavailable = not hmac_available
    logger.info(
        "Lead email minimization MODE=%s batch_size=%s hmac_available=%s",
        mode,
        batch_size,
        hmac_available,
    )

    try:
        if batch_size < 1:
            raise LeadEmailMinimizationAborted("batch_size invalido")
        if apply and not hmac_available:
            raise LeadEmailMinimizationAborted(
                "COMMUNICATION_SUPPRESSION_HMAC_SECRET ausente ou vazio. "
                "Minimização recusa operar sem secret. Sem fallback."
            )
        if lead_id is not None:
            _process_single_lead(
                int(lead_id), apply=apply, report=report, hmac_available=hmac_available
            )
        else:
            last_id = 0
            while True:
                candidates = _fetch_batch(after_id=last_id, batch_size=batch_size)
                if not candidates:
                    break
                report.batches += 1
                logger.info(
                    "Lead email minimization batch MODE=%s batch=%s size=%s",
                    mode,
                    report.batches,
                    len(candidates),
                )
                _close_read_transaction()
                try:
                    if apply:
                        _begin_batch_transaction()
                    _process_batch(
                        candidates,
                        apply=apply,
                        report=report,
                        hmac_available=hmac_available,
                    )
                    if apply:
                        db.session.commit()
                    else:
                        db.session.rollback()
                except LeadEmailMinimizationAborted:
                    db.session.rollback()
                    db.session.expunge_all()
                    raise
                except Exception as exc:
                    db.session.rollback()
                    db.session.expunge_all()
                    report.errors += 1
                    logger.error(
                        "Lead email minimization aborting batch: error_type=%s",
                        type(exc).__name__,
                    )
                    raise LeadEmailMinimizationAborted(
                        "Erro inesperado no batch. Transacao revertida. "
                        "Minimização abortada."
                    ) from exc
                last_id = candidates[-1].id
    except LeadEmailMinimizationAborted as exc:
        report.aborted = True
        report.abort_reason = str(exc)
        report.elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.error(
            "Lead email minimization STATUS=ABORTED MODE=%s reason=%s",
            mode,
            str(exc),
        )
        raise
    except Exception as exc:
        db.session.rollback()
        report.errors += 1
        report.aborted = True
        report.abort_reason = "Erro inesperado. Minimização abortada."
        report.elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.error(
            "Lead email minimization STATUS=ABORTED MODE=%s error_type=%s",
            mode,
            type(exc).__name__,
        )
        raise LeadEmailMinimizationAborted(report.abort_reason) from exc

    report.elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "Lead email minimization STATUS=OK MODE=%s batches=%s elapsed_ms=%s",
        mode,
        report.batches,
        report.elapsed_ms,
    )
    return report


def format_report(report: MinimizationReport) -> str:
    """Resumo agregado sem PII, HMAC, User id ou Lead id."""
    lines = [
        f"MODE={report.mode}",
        f"scanned={report.scanned}",
        f"eligible={report.eligible}",
        f"would_minimize={report.would_minimize}",
        f"minimized={report.minimized}",
        f"skipped_not_converted={report.skipped_not_converted}",
        f"skipped_activation_active={report.skipped_activation_active}",
        f"skipped_historical_suppression_pending="
        f"{report.skipped_historical_suppression_pending}",
        f"already_minimized={report.already_minimized}",
        f"conflicts={report.conflicts}",
        f"errors={report.errors}",
        f"batches={report.batches}",
        f"elapsed_ms={report.elapsed_ms}",
        f"suppression_hmac_unavailable="
        f"{str(report.suppression_hmac_unavailable).lower()}",
        f"aborted={str(report.aborted).lower()}",
    ]
    if report.abort_reason:
        lines.append(f"abort_reason={report.abort_reason}")
    return "\n".join(lines)


def emit_minimization_cli(
    *,
    apply: bool,
    batch_size: int,
    lead_id: int | None,
    echo,
) -> int:
    """Entrypoint de CLI: MODE no início, sem prompt, sem PII. 0=ok, 1=abort."""
    echo("MODE=APPLY" if apply else "MODE=DRY_RUN")
    try:
        report = run_minimization(
            apply=apply, batch_size=batch_size, lead_id=lead_id
        )
    except LeadEmailMinimizationAborted as exc:
        echo("STATUS=ABORTED")
        echo(str(exc))
        return 1
    echo(format_report(report))
    echo("STATUS=OK")
    return 0


def register_lead_email_minimization_command(app) -> None:
    """Registra o comando Flask CLI. Default: dry-run."""
    import click

    @app.cli.command("lead-email-minimization")
    @click.option(
        "--apply",
        is_flag=True,
        default=False,
        help="Persiste HMAC + placeholder. Default: dry-run (nao grava).",
    )
    @click.option(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        show_default=True,
        help="Tamanho do lote (cursor por Lead.id).",
    )
    @click.option(
        "--lead-id",
        type=int,
        default=None,
        help="Processa um único Lead.id. Nao aceita e-mail.",
    )
    def _lead_email_minimization(apply: bool, batch_size: int, lead_id: int | None) -> None:
        """
        Minimização controlada de Lead.email (convertidos terminais).

        Default: DRY RUN. Nao cria CommunicationSuppression. Nao altera newsletter.
        Exige COMMUNICATION_SUPPRESSION_HMAC_SECRET no apply.

        DRY RUN:
          flask --app app.web lead-email-minimization

        APPLY:
          flask --app app.web lead-email-minimization --apply
        """
        exit_code = emit_minimization_cli(
            apply=apply,
            batch_size=batch_size,
            lead_id=lead_id,
            echo=click.echo,
        )
        if exit_code:
            raise SystemExit(exit_code)


def _process_single_lead(
    lead_id: int,
    *,
    apply: bool,
    report: MinimizationReport,
    hmac_available: bool,
) -> None:
    lead = db.session.get(Lead, lead_id)
    if lead is None:
        report.errors += 1
        raise LeadEmailMinimizationAborted("lead_id nao encontrado")
    report.batches = 1
    if apply:
        _begin_batch_transaction()
    try:
        _process_batch(
            [lead],
            apply=apply,
            report=report,
            hmac_available=hmac_available,
        )
        if apply:
            db.session.commit()
        else:
            db.session.rollback()
    except LeadEmailMinimizationAborted:
        db.session.rollback()
        db.session.expunge_all()
        raise
    except Exception as exc:
        db.session.rollback()
        db.session.expunge_all()
        report.errors += 1
        raise LeadEmailMinimizationAborted(
            "Erro inesperado no batch. Transacao revertida. Minimização abortada."
        ) from exc


def _process_batch(
    candidates: list[Lead],
    *,
    apply: bool,
    report: MinimizationReport,
    hmac_available: bool,
) -> None:
    for lead in candidates:
        report.scanned += 1
        if apply:
            db.session.refresh(lead)
        decision = evaluate_lead_email_minimization_eligibility(lead)
        _count_decision(report, decision, apply=apply, hmac_available=hmac_available)
        if not apply:
            continue
        if decision.status == STATUS_CONFLICT:
            raise LeadEmailMinimizationAborted(
                f"Conflito tecnico de email_hmac ({decision.reason}). "
                "Transacao revertida."
            )
        if decision.status != STATUS_ELIGIBLE:
            continue
        result = minimize_lead_email(lead)
        if result.status != STATUS_ELIGIBLE:
            # Elegibilidade mudou entre avaliação e mutação.
            _recount_after_revalidation(report, result)
            continue
        report.minimized += 1


def _count_decision(
    report: MinimizationReport,
    decision: EligibilityDecision,
    *,
    apply: bool,
    hmac_available: bool,
) -> None:
    if decision.status == STATUS_ELIGIBLE:
        report.eligible += 1
        if hmac_available and not apply:
            report.would_minimize += 1
        return
    if decision.status == STATUS_ALREADY_MINIMIZED:
        report.already_minimized += 1
        return
    if decision.status == STATUS_SKIPPED_NOT_CONVERTED:
        report.skipped_not_converted += 1
        return
    if decision.status == STATUS_SKIPPED_ACTIVATION_ACTIVE:
        report.skipped_activation_active += 1
        return
    if decision.status == STATUS_SKIPPED_HISTORICAL_SUPPRESSION_PENDING:
        report.skipped_historical_suppression_pending += 1
        return
    if decision.status == STATUS_CONFLICT:
        report.conflicts += 1
        return
    report.errors += 1


def _recount_after_revalidation(
    report: MinimizationReport, decision: EligibilityDecision
) -> None:
    """Aplica já tinha contado eligible; reclassifica após revalidação."""
    report.eligible -= 1
    if report.eligible < 0:
        report.eligible = 0
    _count_decision(report, decision, apply=True, hmac_available=True)


def _plaintext_existing_hmac_conflict(lead: Lead) -> EligibilityDecision | None:
    """Fail-closed se plaintext convive com email_hmac inconsistente.

    Case A: email_hmac is None → sem conflito (derive na mutação).
    Case B: digest válido == derive(plaintext) → sem conflito (manter existente).
    Case C: inválido ou divergente → conflito; não auto-reparar.
    """
    raw = getattr(lead, "email_hmac", None)
    if raw is None:
        return None
    existing = normalize_email_hmac(raw)
    if existing is None:
        return EligibilityDecision(
            status=STATUS_CONFLICT,
            reason=REASON_CONFLICT_EMAIL_HMAC_INVALID,
        )
    if not is_suppression_enabled():
        return None
    try:
        expected = derive_email_hmac(lead.email)
    except (RuntimeError, ValueError):
        return EligibilityDecision(
            status=STATUS_CONFLICT,
            reason=REASON_CONFLICT_EMAIL_HMAC_INVALID,
        )
    if not hmac.compare_digest(existing, expected):
        return EligibilityDecision(
            status=STATUS_CONFLICT,
            reason=REASON_CONFLICT_EMAIL_HMAC_MISMATCH,
        )
    return None


def _identity_hmac_for_plaintext_lead(lead: Lead) -> str | None:
    """HMAC do plaintext atual. Nunca deriva do placeholder."""
    if is_lead_email_minimized(lead):
        return None
    if not is_suppression_enabled():
        return None
    try:
        return derive_email_hmac(lead.email)
    except (RuntimeError, ValueError):
        return None


def _historical_suppression_pending(lead: Lead, digest: str | None) -> bool:
    """Opt-out persistido sem suppression correspondente: 4C-B ainda precisa do plaintext."""
    has_opt_out = getattr(lead, "opt_out_at", None) is not None
    has_act_opt_out = getattr(lead, "activation_opt_out_at", None) is not None
    if not has_opt_out and not has_act_opt_out:
        return False
    if digest is None:
        return True
    if has_opt_out:
        if not has_email_hmac_suppression(digest, PURPOSE_PRE_REGISTRATION):
            return True
        if not has_email_hmac_suppression(digest, PURPOSE_ACTIVATION):
            return True
    if has_act_opt_out:
        if not has_email_hmac_suppression(digest, PURPOSE_ACTIVATION):
            return True
    return False


def _activation_is_terminal(lead: Lead, digest: str | None) -> bool:
    if is_activation_journey_ended(lead):
        return True
    if _converted_user_is_operationally_closed(lead):
        return True
    return _activation_terminal_via_opt_out(lead, digest)


def _activation_terminal_via_opt_out(lead: Lead, digest: str | None) -> bool:
    if digest is None:
        return False
    if getattr(lead, "opt_out_at", None) is not None:
        return (
            has_email_hmac_suppression(digest, PURPOSE_PRE_REGISTRATION)
            and has_email_hmac_suppression(digest, PURPOSE_ACTIVATION)
        )
    if getattr(lead, "activation_opt_out_at", None) is not None:
        return has_email_hmac_suppression(digest, PURPOSE_ACTIVATION)
    return False


def _converted_user_is_operationally_closed(lead: Lead) -> bool:
    """Resolve pelo converted_user_id. Nunca busca User por Lead.email."""
    if lead.converted_user_id is None:
        return False
    user = db.session.get(User, int(lead.converted_user_id))
    if user is None:
        return False
    return is_user_operationally_closed(user)


def _fetch_batch(*, after_id: int, batch_size: int) -> list[Lead]:
    return (
        Lead.query.filter(Lead.id > after_id)
        .order_by(Lead.id.asc())
        .limit(batch_size)
        .all()
    )


def _close_read_transaction() -> None:
    db.session.rollback()


def _begin_batch_transaction() -> None:
    session = db.session()
    if not session.in_transaction():
        session.begin()
