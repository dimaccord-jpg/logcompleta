"""Ciclo local da execução semântica.

Garantia: não é exactly-once.
No máximo uma tentativa ativa por pergunta, snapshot e generation.
Batches compartilham essa execução.
Resultado incerto não redispara.
Retry explícito cria outra generation.
"""
from __future__ import annotations

import copy
import secrets
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    SemanticAiJobBudget,
    SemanticHumanReview,
    SemanticQuestionExecution,
    SemanticQuestionJournal,
    utcnow_naive,
)
from app.services.semantic_freight_contract.evidence import local_fingerprint
from app.services.semantic_question_execution.constants import (
    FAKE_ENABLED_QUESTION_TYPES,
    REAL_ENABLED_QUESTION_TYPES,
    REAL_MAX_RESPONSE_BYTES,
    BudgetExhausted,
    BudgetNotConfigured,
    FencingRejected,
    ManifestImmutable,
    RealProviderDisabled,
    SemanticExecutionError,
    real_dispatch_environment_allowed,
    semantic_ai_real_dispatch_enabled,
)
from app.services.semantic_question_execution.fake_provider import (
    FakeSemanticProvider,
    FutureBillableCall,
    SimulatedUncertain,
)
from app.services.semantic_question_execution.keys import review_material_fingerprint
from app.services.semantic_question_execution.manifest import build_execution_bundle, dumps, loads
from app.services.semantic_question_execution.validator import validate_semantic_decisions

_DRIFT = {
    "queue_not_current",
    "candidate_set_changed",
    "evidence_changed",
    "dependency_changed",
    "authorization_changed",
}
_UNPROVEN = {"claimed", "dispatch_ready", "execution_started"}
_TERMINAL_VIEW = {"accepted", "rejected", "requires_review", "blocked", "failed", "stale", "uncertain"}


class _BudgetConflict(Exception):
    pass


def _money(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def _loads_response(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return loads(raw)
    except Exception:
        return None


def _get(execution_id: int) -> SemanticQuestionExecution:
    row = db.session.get(SemanticQuestionExecution, int(execution_id))
    if row is None:
        raise SemanticExecutionError("execution_missing")
    return row


def _view(row: SemanticQuestionExecution) -> dict[str, Any]:
    return {
        "id": row.id,
        "tenant_scope": row.tenant_scope,
        "semantic_question_key": row.semantic_question_key,
        "decision_dependency_fingerprint": row.decision_dependency_fingerprint,
        "ai_execution_fingerprint": row.ai_execution_fingerprint,
        "generation": row.generation,
        "question_attempt_key": row.question_attempt_key,
        "status": row.status,
        "claim_token": row.claim_token,
        "fencing_version": row.fencing_version,
        "claimed_at": row.claimed_at,
        "claim_expires_at": row.claim_expires_at,
        "job_ref": row.job_ref,
        "question_type": row.question_type,
        "question_id": row.question_id,
        "manifest_digest": row.manifest_digest,
        "manifest": loads(row.manifest_payload),
        "evidence_manifest": loads(row.evidence_manifest),
        "snapshot": loads(row.snapshot_payload) or {},
        "batch_request_key": row.batch_request_key,
        "financial_attempt_key": row.financial_attempt_key,
        "execution_started_at": row.execution_started_at,
        "response_observed_at": row.response_observed_at,
        "response": _loads_response(row.response_payload),
        "validation_status": row.validation_status,
        "validation_findings": loads(row.validation_findings) or [],
        "proposal_ref": row.proposal_ref,
        "proposal": loads(row.proposal_payload),
        "proposal_applicable": bool(row.proposal_applicable),
        "stale_reason": row.stale_reason,
        "budget_state": row.budget_state,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _journal(
    row: SemanticQuestionExecution,
    checkpoint: str,
    *,
    payload: Any = None,
    applicable: bool = False,
    stale_reason: str | None = None,
    response_digest: str | None = None,
) -> SemanticQuestionJournal:
    return SemanticQuestionJournal(
        execution_id=row.id,
        checkpoint=checkpoint,
        manifest_digest=row.manifest_digest,
        response_digest=response_digest,
        payload=None if payload is None else dumps(payload),
        claim_token=row.claim_token,
        fencing_version=row.fencing_version,
        applicable=applicable,
        stale_reason=stale_reason,
        dependency_fingerprint=row.decision_dependency_fingerprint,
        created_at=utcnow_naive(),
    )


def _has_checkpoint(execution_id: int, checkpoint: str) -> bool:
    found = (
        db.session.query(SemanticQuestionJournal.id)
        .filter_by(execution_id=int(execution_id), checkpoint=checkpoint)
        .first()
    )
    return found is not None


def _executor_update(
    execution_id: int,
    *,
    claim_token: str,
    fencing_version: int,
    values: dict[str, Any],
    journal: SemanticQuestionJournal | None = None,
) -> None:
    values = dict(values)
    values["updated_at"] = utcnow_naive()
    result = db.session.execute(
        update(SemanticQuestionExecution)
        .where(
            SemanticQuestionExecution.id == int(execution_id),
            SemanticQuestionExecution.claim_token == claim_token,
            SemanticQuestionExecution.fencing_version == int(fencing_version),
        )
        .values(**values)
    )
    if result.rowcount != 1:
        db.session.rollback()
        raise FencingRejected()
    if journal is not None:
        db.session.add(journal)
    db.session.commit()


def _supersede(
    row: SemanticQuestionExecution,
    values: dict[str, Any],
    *,
    journal_checkpoint: str | None = None,
    journal_payload: Any = None,
    applicable: bool = False,
) -> None:
    token = secrets.token_hex(16)
    version = int(row.fencing_version)
    body = dict(values)
    body["claim_token"] = token
    body["fencing_version"] = version + 1
    body["updated_at"] = utcnow_naive()
    result = db.session.execute(
        update(SemanticQuestionExecution)
        .where(
            SemanticQuestionExecution.id == row.id,
            SemanticQuestionExecution.fencing_version == version,
        )
        .values(**body)
    )
    if result.rowcount != 1:
        db.session.rollback()
        raise FencingRejected()
    if journal_checkpoint:
        db.session.add(
            SemanticQuestionJournal(
                execution_id=row.id,
                checkpoint=journal_checkpoint,
                manifest_digest=row.manifest_digest,
                payload=None if journal_payload is None else dumps(journal_payload),
                claim_token=token,
                fencing_version=version + 1,
                applicable=applicable,
                stale_reason=body.get("stale_reason"),
                dependency_fingerprint=row.decision_dependency_fingerprint,
                created_at=utcnow_naive(),
            )
        )
    db.session.commit()


def _budget_row(tenant_scope: str, job_ref: str) -> SemanticAiJobBudget | None:
    return (
        db.session.query(SemanticAiJobBudget)
        .filter_by(tenant_scope=tenant_scope, job_ref=job_ref)
        .one_or_none()
    )


def _budget_view(row: SemanticAiJobBudget) -> dict[str, Any]:
    return {
        "tenant_scope": row.tenant_scope,
        "job_ref": row.job_ref,
        "max_requests": row.max_requests,
        "max_billable_credits": None if row.max_billable_credits is None else _money(row.max_billable_credits),
        "reserved_requests": int(row.reserved_requests),
        "completed_requests": int(row.completed_requests),
        "reserved_credits": _money(row.reserved_credits),
        "settled_credits": _money(row.settled_credits),
        "uncertain_credits": _money(row.uncertain_credits),
        "version": int(row.version),
    }


def configure_job_budget(
    tenant_scope: str,
    job_ref: str,
    *,
    max_requests: int | None,
    max_billable_credits: Decimal | int | str | None,
) -> dict[str, Any]:
    row = _budget_row(tenant_scope, job_ref)
    credits = None if max_billable_credits is None else _money(max_billable_credits)
    if row is None:
        row = SemanticAiJobBudget(
            tenant_scope=tenant_scope,
            job_ref=job_ref,
            max_requests=max_requests,
            max_billable_credits=credits,
            reserved_requests=0,
            completed_requests=0,
            reserved_credits=Decimal("0"),
            settled_credits=Decimal("0"),
            uncertain_credits=Decimal("0"),
            version=0,
        )
        db.session.add(row)
    else:
        row.max_requests = max_requests
        row.max_billable_credits = credits
        row.version = int(row.version) + 1
        row.updated_at = utcnow_naive()
    db.session.commit()
    return _budget_view(row)


def real_dispatch_decision(tenant_scope: str, job_ref: str) -> dict[str, Any]:
    row = _budget_row(tenant_scope, job_ref)
    reasons: list[str] = []
    if not semantic_ai_real_dispatch_enabled():
        reasons.append("provider_disabled")
    if not real_dispatch_environment_allowed():
        reasons.append("app_env_blocked")
    if not REAL_ENABLED_QUESTION_TYPES:
        reasons.append("no_question_type_enabled")
    if row is None or row.max_requests is None or row.max_billable_credits is None:
        reasons.append("budget_not_configured")
    return {"allowed": not reasons, "reasons": reasons}


def _reserve_in_session(tenant_scope: str, job_ref: str, credits: Decimal) -> None:
    row = _budget_row(tenant_scope, job_ref)
    if row is None or row.max_requests is None or row.max_billable_credits is None:
        raise BudgetNotConfigured()
    amount = _money(credits)
    if amount <= 0:
        raise BudgetNotConfigured()
    used_requests = int(row.reserved_requests) + int(row.completed_requests)
    used_credits = _money(row.reserved_credits) + _money(row.settled_credits) + _money(row.uncertain_credits)
    if used_requests + 1 > int(row.max_requests) or used_credits + amount > _money(row.max_billable_credits):
        raise BudgetExhausted()
    result = db.session.execute(
        update(SemanticAiJobBudget)
        .where(SemanticAiJobBudget.id == row.id, SemanticAiJobBudget.version == int(row.version))
        .values(
            reserved_requests=int(row.reserved_requests) + 1,
            reserved_credits=_money(row.reserved_credits) + amount,
            version=int(row.version) + 1,
            updated_at=utcnow_naive(),
        )
    )
    if result.rowcount != 1:
        raise _BudgetConflict()


def reserve_claimed_execution(
    execution_id: int,
    *,
    claim_token: str,
    fencing_version: int,
    credits: Decimal | None,
) -> dict[str, Any]:
    row = _get(execution_id)
    if row.claim_token != claim_token or int(row.fencing_version) != int(fencing_version):
        raise FencingRejected()
    if row.budget_state == "reserved":
        return _view(row)
    if row.status != "claimed":
        raise SemanticExecutionError("not_claimed")
    if credits is None:
        return _close_without_call(
            row,
            claim_token=claim_token,
            fencing_version=fencing_version,
            findings=["budget_not_configured"],
        )
    amount = _money(credits)
    if amount <= 0:
        return _close_without_call(
            row,
            claim_token=claim_token,
            fencing_version=fencing_version,
            findings=["budget_not_configured"],
        )
    reserved = False
    for _ in range(8):
        try:
            _reserve_in_session(row.tenant_scope, row.job_ref, amount)
            reserved = True
            break
        except _BudgetConflict:
            db.session.rollback()
            row = _get(execution_id)
        except BudgetNotConfigured:
            db.session.rollback()
            return _close_without_call(
                _get(execution_id),
                claim_token=claim_token,
                fencing_version=fencing_version,
                findings=["budget_not_configured"],
            )
        except BudgetExhausted:
            db.session.rollback()
            return _close_without_call(
                _get(execution_id),
                claim_token=claim_token,
                fencing_version=fencing_version,
                findings=["budget_exhausted"],
            )
    if not reserved:
        db.session.rollback()
        return _close_without_call(
            _get(execution_id),
            claim_token=claim_token,
            fencing_version=fencing_version,
            findings=["budget_exhausted"],
        )
    _executor_update(
        execution_id,
        claim_token=claim_token,
        fencing_version=fencing_version,
        values={"budget_state": "reserved", "reserved_credit_amount": amount},
        journal=_journal(_get(execution_id), "budget_reserved", payload={"credits": str(amount)}),
    )
    return _view(_get(execution_id))


def mark_real_execution_started(
    execution_id: int,
    *,
    claim_token: str,
    fencing_version: int,
) -> dict[str, Any]:
    row = _get(execution_id)
    if row.claim_token != claim_token or int(row.fencing_version) != int(fencing_version):
        raise FencingRejected()
    if row.status == "execution_started":
        return _view(row)
    if row.status != "claimed" or row.budget_state != "reserved":
        raise SemanticExecutionError("not_ready")
    _executor_update(
        execution_id,
        claim_token=claim_token,
        fencing_version=fencing_version,
        values={"status": "execution_started", "execution_started_at": utcnow_naive()},
        journal=_journal(_get(execution_id), "execution_started"),
    )
    return _view(_get(execution_id))


def reserve_job_budget(tenant_scope: str, job_ref: str, *, requests: int, credits: Decimal) -> dict[str, Any]:
    if int(requests) != 1:
        raise SemanticExecutionError("max_questions_per_request")
    for _ in range(8):
        try:
            _reserve_in_session(tenant_scope, job_ref, _money(credits))
            db.session.commit()
            return _budget_view(_budget_row(tenant_scope, job_ref))
        except _BudgetConflict:
            db.session.rollback()
        except (BudgetExhausted, BudgetNotConfigured):
            db.session.rollback()
            raise
    raise BudgetExhausted()


def _finish_budget_in_session(row: SemanticQuestionExecution, outcome: str) -> None:
    if row.budget_state != "reserved":
        return
    budget = _budget_row(row.tenant_scope, row.job_ref)
    if budget is None:
        raise BudgetNotConfigured()
    amount = _money(row.reserved_credit_amount)
    values: dict[str, Any] = {
        "reserved_requests": int(budget.reserved_requests) - 1,
        "completed_requests": int(budget.completed_requests) + 1,
        "reserved_credits": _money(budget.reserved_credits) - amount,
        "version": int(budget.version) + 1,
        "updated_at": utcnow_naive(),
    }
    if outcome == "settled":
        values["settled_credits"] = _money(budget.settled_credits) + amount
    else:
        values["uncertain_credits"] = _money(budget.uncertain_credits) + amount
    result = db.session.execute(
        update(SemanticAiJobBudget)
        .where(
            SemanticAiJobBudget.id == budget.id,
            SemanticAiJobBudget.version == int(budget.version),
            SemanticAiJobBudget.reserved_requests >= 1,
        )
        .values(**values)
    )
    if result.rowcount != 1:
        raise _BudgetConflict()


def _budget_outcome_values(
    budget: SemanticAiJobBudget,
    execution: SemanticQuestionExecution,
    outcome: str,
    actual_credits: Decimal | None,
) -> dict[str, Any]:
    amount = _money(execution.reserved_credit_amount)
    values: dict[str, Any] = {
        "reserved_requests": int(budget.reserved_requests) - 1,
        "reserved_credits": _money(budget.reserved_credits) - amount,
        "version": int(budget.version) + 1,
        "updated_at": utcnow_naive(),
    }
    if outcome == "released":
        return values
    if outcome == "settled_actual":
        if actual_credits is None:
            raise SemanticExecutionError("actual_credits_missing")
        values["completed_requests"] = int(budget.completed_requests) + 1
        values["settled_credits"] = _money(budget.settled_credits) + _money(actual_credits)
        return values
    if outcome == "uncertain":
        values["completed_requests"] = int(budget.completed_requests) + 1
        values["uncertain_credits"] = _money(budget.uncertain_credits) + amount
        return values
    raise SemanticExecutionError("budget_outcome")


def _apply_reserved_budget(
    execution: SemanticQuestionExecution,
    outcome: str,
    actual_credits: Decimal | None,
) -> str:
    """Move a reserva local. Não liquida o teto reservado como se fosse custo real."""
    if execution.budget_state != "reserved":
        return execution.budget_state
    budget = _budget_row(execution.tenant_scope, execution.job_ref)
    if budget is None:
        raise BudgetNotConfigured()
    amount = _money(execution.reserved_credit_amount)
    state = {"released": "released", "settled_actual": "settled", "uncertain": "uncertain"}[outcome]
    result = db.session.execute(
        update(SemanticAiJobBudget)
        .where(
            SemanticAiJobBudget.id == budget.id,
            SemanticAiJobBudget.version == int(budget.version),
            SemanticAiJobBudget.reserved_requests >= 1,
            SemanticAiJobBudget.reserved_credits >= amount,
        )
        .values(**_budget_outcome_values(budget, execution, outcome, actual_credits))
    )
    if result.rowcount != 1:
        raise _BudgetConflict()
    return state


def move_reserved_budget(
    execution_id: int,
    *,
    outcome: str,
    actual_credits: Decimal | None = None,
) -> dict[str, Any]:
    for _ in range(8):
        row = _get(execution_id)
        if row.budget_state != "reserved":
            return _budget_view(_budget_row(row.tenant_scope, row.job_ref)) if _budget_row(row.tenant_scope, row.job_ref) else {}
        try:
            state = _apply_reserved_budget(row, outcome, actual_credits)
            row.budget_state = state
            row.updated_at = utcnow_naive()
            db.session.commit()
            fresh = _budget_row(row.tenant_scope, row.job_ref)
            return _budget_view(fresh) if fresh is not None else {}
        except _BudgetConflict:
            db.session.rollback()
        except SemanticExecutionError:
            db.session.rollback()
            raise
    raise BudgetExhausted()


def _evidence_hashes(spec: dict[str, Any]) -> list[str]:
    hashes = []
    for item in spec.get("evidence_items") or []:
        if isinstance(item, dict):
            hashes.append(local_fingerprint(str(item.get("material") or "")))
    return hashes


def _material(spec: dict[str, Any]) -> str:
    return review_material_fingerprint(
        candidate_ids=list(spec.get("candidate_ids") or []),
        evidence_hashes=_evidence_hashes(spec),
    )


def _evidence_material(row: SemanticQuestionExecution) -> list[dict[str, str]]:
    items = (loads(row.evidence_manifest) or {}).get("items") or []
    material: list[dict[str, str]] = []
    for item in items:
        if isinstance(item, dict):
            material.append(
                {
                    "evidence_id": str(item.get("evidence_id") or ""),
                    "material_hash": str(item.get("material_hash") or ""),
                }
            )
    return material


def _execution_material(row: SemanticQuestionExecution) -> str:
    snapshot = loads(row.snapshot_payload) or {}
    return review_material_fingerprint(
        candidate_ids=list(snapshot.get("candidate_ids") or []),
        evidence_hashes=[item["material_hash"] for item in _evidence_material(row)],
    )


def _vigente_human(row: SemanticQuestionExecution) -> SemanticHumanReview | None:
    review = (
        db.session.query(SemanticHumanReview)
        .filter_by(
            tenant_scope=row.tenant_scope,
            semantic_question_key=row.semantic_question_key,
            decision_dependency_fingerprint=row.decision_dependency_fingerprint,
            material_fingerprint=_execution_material(row),
        )
        .order_by(SemanticHumanReview.review_revision.desc())
        .first()
    )
    if review is None or review.review_state not in {"accepted", "rejected"}:
        return None
    return review


def _max_generation(row: SemanticQuestionExecution) -> int:
    current = (
        db.session.query(db.func.max(SemanticQuestionExecution.generation))
        .filter_by(
            tenant_scope=row.tenant_scope,
            semantic_question_key=row.semantic_question_key,
            decision_dependency_fingerprint=row.decision_dependency_fingerprint,
            ai_execution_fingerprint=row.ai_execution_fingerprint,
        )
        .scalar()
    )
    return int(current or row.generation)


def _proposal_generation_current(row: SemanticQuestionExecution) -> bool:
    if row.stale_reason == "superseded":
        return False
    return int(row.generation) == _max_generation(row)


def _claim_is_expired(row: SemanticQuestionExecution, now: datetime) -> bool:
    expires = row.claim_expires_at
    if expires is None:
        return False
    return expires <= now


def _latest_human(tenant_scope: str, question_key: str, dependency: str) -> SemanticHumanReview | None:
    rows = (
        db.session.query(SemanticHumanReview)
        .filter_by(
            tenant_scope=tenant_scope,
            semantic_question_key=question_key,
            decision_dependency_fingerprint=dependency,
        )
        .all()
    )
    latest: dict[str, SemanticHumanReview] = {}
    for row in rows:
        current = latest.get(row.material_fingerprint)
        if current is None or int(row.review_revision) > int(current.review_revision):
            latest[row.material_fingerprint] = row
    blocking = [row for row in latest.values() if row.review_state in {"accepted", "rejected"}]
    if not blocking:
        return None
    return sorted(blocking, key=lambda item: int(item.review_revision), reverse=True)[0]


def _review_view(row: SemanticHumanReview | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row.id,
        "tenant_scope": row.tenant_scope,
        "semantic_question_key": row.semantic_question_key,
        "decision_dependency_fingerprint": row.decision_dependency_fingerprint,
        "material_fingerprint": row.material_fingerprint,
        "selected_candidate_ref": row.selected_candidate_ref,
        "review_state": row.review_state,
        "reviewer_ref": row.reviewer_ref,
        "review_revision": row.review_revision,
        "evidence_refs": loads(row.evidence_refs) or [],
        "proposal_ref": row.proposal_ref,
        "provenance": loads(row.provenance) or {},
        "execution_id": row.execution_id,
        "created_at": row.created_at,
    }


def lookup_human_review(tenant_scope: str, question_key: str, dependency: str) -> dict[str, Any] | None:
    return _review_view(_latest_human(tenant_scope, question_key, dependency))


def _next_revision(tenant_scope: str, question_key: str, dependency: str, material: str) -> int:
    current = (
        db.session.query(db.func.max(SemanticHumanReview.review_revision))
        .filter_by(
            tenant_scope=tenant_scope,
            semantic_question_key=question_key,
            decision_dependency_fingerprint=dependency,
            material_fingerprint=material,
        )
        .scalar()
    )
    return int(current or 0) + 1


def _insert_review(spec: dict[str, Any], *, review_state: str, reviewer_ref: str, execution_id: int | None, selected: str | None, evidence_refs: list[str], proposal_ref: str | None) -> SemanticHumanReview:
    material = _material(spec)
    revision = _next_revision(spec["tenant_scope"], spec["semantic_question_key"], spec["decision_dependency_fingerprint"], material)
    row = SemanticHumanReview(
        tenant_scope=spec["tenant_scope"],
        semantic_question_key=spec["semantic_question_key"],
        decision_dependency_fingerprint=spec["decision_dependency_fingerprint"],
        material_fingerprint=material,
        selected_candidate_ref=selected,
        review_state=review_state,
        reviewer_ref=reviewer_ref,
        review_revision=revision,
        evidence_refs=dumps(list(evidence_refs)),
        proposal_ref=proposal_ref,
        provenance=dumps(
            {
                "kind": "human",
                "proposal_ref": proposal_ref,
                "execution_id": execution_id,
                "review_state": review_state,
            }
        ),
        execution_id=execution_id,
        created_at=utcnow_naive(),
    )
    db.session.add(row)
    return row


def prepare_execution(spec: dict[str, Any]) -> dict[str, Any]:
    bundle = build_execution_bundle(spec)
    existing = (
        db.session.query(SemanticQuestionExecution)
        .filter_by(question_attempt_key=bundle["question_attempt_key"])
        .one_or_none()
    )
    if existing is not None:
        return _view(existing)
    status = "prepared"
    reason = None
    if not bundle["ok"]:
        status = "blocked"
        reason = bundle["reason"]
    else:
        human = _latest_human(
            str(spec["tenant_scope"]),
            str(spec["semantic_question_key"]),
            str(spec["decision_dependency_fingerprint"]),
        )
        if human is not None and human.material_fingerprint == _material(spec):
            status = "blocked"
            reason = "human_rejection_tombstone" if human.review_state == "rejected" else "human_decision_exists"
    row = SemanticQuestionExecution(
        tenant_scope=str(spec["tenant_scope"]),
        semantic_question_key=str(spec["semantic_question_key"]),
        decision_dependency_fingerprint=bundle["decision_dependency_fingerprint"],
        ai_execution_fingerprint=bundle["ai_execution_fingerprint"],
        generation=int(bundle["generation"]),
        question_attempt_key=bundle["question_attempt_key"],
        status=status,
        fencing_version=0,
        job_ref=str(spec["job_ref"]),
        question_type=str(spec.get("question_type") or ""),
        question_id=str(spec.get("question_id") or ""),
        manifest_digest=bundle.get("manifest_digest"),
        manifest_payload=dumps(bundle.get("manifest")),
        evidence_manifest=dumps(bundle.get("evidence_manifest")),
        snapshot_payload=dumps(bundle.get("snapshot") or {}),
        batch_request_key=bundle.get("batch_request_key"),
        financial_attempt_key=bundle.get("financial_attempt_key"),
        stale_reason=reason,
        proposal_applicable=False,
        budget_state="none",
        created_at=utcnow_naive(),
        updated_at=utcnow_naive(),
    )
    db.session.add(row)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = (
            db.session.query(SemanticQuestionExecution)
            .filter_by(question_attempt_key=bundle["question_attempt_key"])
            .one()
        )
        return _view(existing)
    return _view(row)


def replay_execution(spec: dict[str, Any]) -> dict[str, Any]:
    replayed = dict(spec)
    replayed["generation"] = int(spec.get("generation") or 1)
    return prepare_execution(replayed)


def open_explicit_generation(spec: dict[str, Any]) -> dict[str, Any]:
    if spec.get("explicit_retry") is not True:
        raise SemanticExecutionError("retry_not_explicit")
    human = _latest_human(
        str(spec["tenant_scope"]),
        str(spec["semantic_question_key"]),
        str(spec["decision_dependency_fingerprint"]),
    )
    if human is not None and human.material_fingerprint == _material(spec):
        code = "human_rejection_tombstone" if human.review_state == "rejected" else "human_decision_exists"
        raise SemanticExecutionError(code)
    current = (
        db.session.query(db.func.max(SemanticQuestionExecution.generation))
        .filter_by(
            tenant_scope=str(spec["tenant_scope"]),
            semantic_question_key=str(spec["semantic_question_key"]),
            decision_dependency_fingerprint=str(spec["decision_dependency_fingerprint"]),
            ai_execution_fingerprint=build_execution_bundle(spec)["ai_execution_fingerprint"],
        )
        .scalar()
    )
    opened = dict(spec)
    opened["generation"] = int(current or 0) + 1
    opened.pop("explicit_retry", None)
    created = prepare_execution(opened)
    _retire_prior_generations(int(created["id"]))
    return _view(_get(int(created["id"])))


def _retire_prior_generations(execution_id: int) -> None:
    current = _get(execution_id)
    rows = (
        db.session.query(SemanticQuestionExecution.id)
        .filter(
            SemanticQuestionExecution.tenant_scope == current.tenant_scope,
            SemanticQuestionExecution.semantic_question_key == current.semantic_question_key,
            SemanticQuestionExecution.decision_dependency_fingerprint == current.decision_dependency_fingerprint,
            SemanticQuestionExecution.ai_execution_fingerprint == current.ai_execution_fingerprint,
            SemanticQuestionExecution.generation < int(current.generation),
        )
        .all()
    )
    for (prior_id,) in rows:
        row = _get(int(prior_id))
        if row.status in {"accepted", "rejected"}:
            continue
        if row.status not in {"requires_review", "proposal_recorded", "validated", "response_observed"} and not row.proposal_applicable:
            continue
        _align_superseded_generation(row)


def claim_execution(execution_id: int, *, now: datetime | None = None, ttl_seconds: int = 30) -> dict[str, Any]:
    moment = now or utcnow_naive()
    token = secrets.token_hex(16)
    result = db.session.execute(
        update(SemanticQuestionExecution)
        .where(
            SemanticQuestionExecution.id == int(execution_id),
            SemanticQuestionExecution.status == "prepared",
        )
        .values(
            status="claimed",
            claim_token=token,
            fencing_version=SemanticQuestionExecution.fencing_version + 1,
            claimed_at=moment,
            claim_expires_at=moment + timedelta(seconds=int(ttl_seconds)),
            updated_at=moment,
        )
    )
    db.session.commit()
    row = _get(execution_id)
    if result.rowcount != 1:
        return {"won": False, "id": row.id, "status": row.status, "generation": row.generation}
    view = _view(row)
    view["won"] = True
    view["claim_token"] = token
    return view


def reconcile_expired_claim(execution_id: int, *, now: datetime) -> dict[str, Any]:
    row = _get(execution_id)
    if row.claim_expires_at is None or now < row.claim_expires_at:
        raise SemanticExecutionError("claim_not_expired")
    if row.status not in _UNPROVEN:
        return _view(row)
    _finish_later = row.budget_state == "reserved"
    _supersede(
        row,
        {"status": "uncertain", "stale_reason": "claim_expired_unproven"},
        journal_checkpoint="uncertain",
        journal_payload={"reason": "claim_expired_unproven"},
    )
    if _finish_later:
        _finish_with_retry(_get(execution_id), "uncertain")
    return _view(_get(execution_id))


def _finish_with_retry(row: SemanticQuestionExecution, outcome: str) -> None:
    for _ in range(8):
        current = _get(row.id)
        if current.budget_state != "reserved":
            return
        try:
            _finish_budget_in_session(current, outcome)
            current.budget_state = "uncertain" if outcome == "uncertain" else "settled"
            current.updated_at = utcnow_naive()
            db.session.commit()
            return
        except _BudgetConflict:
            db.session.rollback()
    raise BudgetExhausted()


def mutate_manifest(execution_id: int, _payload: dict[str, Any]) -> None:
    row = _get(execution_id)
    if row.manifest_digest or row.status != "prepared":
        raise ManifestImmutable()
    raise ManifestImmutable()


def _precheck(
    row: SemanticQuestionExecution,
    observation: dict[str, Any],
    *,
    enabled_types: frozenset[str] | None = None,
) -> list[str]:
    findings: list[str] = []
    allowed = FAKE_ENABLED_QUESTION_TYPES if enabled_types is None else enabled_types
    snapshot = loads(row.snapshot_payload) or {}
    if observation.get("queue_status") != "current":
        findings.append("queue_not_current")
    if observation.get("eligibility") != "ai_eligible":
        findings.append("not_ai_eligible")
    if "question_type" in observation and observation.get("question_type") != row.question_type:
        findings.append("question_type_mismatch")
    if row.question_type not in allowed:
        findings.append("question_type_disabled")
    if "question_id" in observation and observation.get("question_id") != row.question_id:
        findings.append("question_identity_mismatch")
    if "semantic_question_key" in observation and observation.get("semantic_question_key") != row.semantic_question_key:
        findings.append("question_identity_mismatch")
    if "manifest_digest" in observation and observation.get("manifest_digest") != row.manifest_digest:
        findings.append("manifest_digest_mismatch")
    if sorted(observation.get("candidate_ids") or []) != list(snapshot.get("candidate_ids") or []):
        findings.append("candidate_set_changed")
    if observation.get("evidence_fingerprint") != snapshot.get("evidence_fingerprint"):
        findings.append("evidence_changed")
    if observation.get("dependency_fingerprint") != row.decision_dependency_fingerprint:
        findings.append("dependency_changed")
    if observation.get("authorization_fingerprint") != snapshot.get("authorization_fingerprint"):
        findings.append("authorization_changed")
    if int(observation.get("generation") or 0) != int(row.generation):
        findings.append("generation_invalid")
    human = _latest_human(row.tenant_scope, row.semantic_question_key, row.decision_dependency_fingerprint)
    if human is not None:
        findings.append("human_decision")
    return findings


def execution_findings(
    execution_id: int,
    observation: dict[str, Any],
    *,
    enabled_types: frozenset[str] | None = None,
) -> list[str]:
    return _precheck(_get(execution_id), observation, enabled_types=enabled_types)


def close_claimed_execution(
    execution_id: int,
    *,
    claim_token: str,
    fencing_version: int,
    findings: list[str],
) -> dict[str, Any]:
    row = _get(execution_id)
    if row.claim_token != claim_token or int(row.fencing_version) != int(fencing_version):
        raise FencingRejected()
    return _close_without_call(
        row,
        claim_token=claim_token,
        fencing_version=fencing_version,
        findings=findings or ["blocked"],
    )


def _close_without_call(
    row: SemanticQuestionExecution,
    *,
    claim_token: str,
    fencing_version: int,
    findings: list[str],
) -> dict[str, Any]:
    status = "stale" if any(item in _DRIFT for item in findings) else "blocked"
    _executor_update(
        row.id,
        claim_token=claim_token,
        fencing_version=fencing_version,
        values={"status": status, "stale_reason": findings[0], "proposal_applicable": False},
        journal=_journal(row, status, payload={"findings": findings}, stale_reason=findings[0]),
    )
    return _view(_get(row.id))


def mark_execution_started(
    execution_id: int,
    *,
    claim_token: str,
    fencing_version: int,
    observation: dict[str, Any],
    credits: Decimal | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    row = _get(execution_id)
    if row.claim_token != claim_token or int(row.fencing_version) != int(fencing_version):
        raise FencingRejected()
    if row.status != "claimed":
        return _view(row)
    checked_at = now or utcnow_naive()
    if _claim_is_expired(row, checked_at):
        return reconcile_expired_claim(execution_id, now=checked_at)
    findings = _precheck(row, observation)
    if findings:
        return _close_without_call(row, claim_token=claim_token, fencing_version=fencing_version, findings=findings)
    if credits is None:
        return _close_without_call(
            row,
            claim_token=claim_token,
            fencing_version=fencing_version,
            findings=["budget_not_configured"],
        )
    amount = _money(credits)
    reserved = False
    for _ in range(8):
        try:
            _reserve_in_session(row.tenant_scope, row.job_ref, amount)
            reserved = True
            break
        except _BudgetConflict:
            db.session.rollback()
        except BudgetNotConfigured:
            db.session.rollback()
            return _close_without_call(
                _get(execution_id),
                claim_token=claim_token,
                fencing_version=fencing_version,
                findings=["budget_not_configured"],
            )
        except BudgetExhausted:
            db.session.rollback()
            return _close_without_call(
                _get(execution_id),
                claim_token=claim_token,
                fencing_version=fencing_version,
                findings=["budget_exhausted"],
            )
    if not reserved:
        db.session.rollback()
        return _close_without_call(
            _get(execution_id),
            claim_token=claim_token,
            fencing_version=fencing_version,
            findings=["budget_exhausted"],
        )
    moment = utcnow_naive()
    _executor_update(
        execution_id,
        claim_token=claim_token,
        fencing_version=fencing_version,
        values={
            "status": "dispatch_ready",
            "budget_state": "reserved",
            "reserved_credit_amount": amount,
        },
        journal=_journal(_get(execution_id), "dispatch_ready"),
    )
    _executor_update(
        execution_id,
        claim_token=claim_token,
        fencing_version=fencing_version,
        values={"status": "execution_started", "execution_started_at": moment},
        journal=_journal(_get(execution_id), "execution_started"),
    )
    return _view(_get(execution_id))


def _archive_response(row: SemanticQuestionExecution, response: dict[str, Any], reason: str) -> None:
    if _has_checkpoint(row.id, "response_observed"):
        return
    digest = local_fingerprint(response)
    db.session.add(
        _journal(
            row,
            "response_observed",
            payload={"response": response, "historical": True},
            applicable=False,
            stale_reason=reason,
            response_digest=digest,
        )
    )
    if row.response_payload is None:
        row.response_payload = dumps(response)
        row.response_observed_at = utcnow_naive()
    row.proposal_applicable = False
    row.stale_reason = reason
    row.updated_at = utcnow_naive()
    db.session.commit()
    if row.budget_state == "reserved":
        _finish_with_retry(_get(row.id), "settled")


def observe_response(
    execution_id: int,
    *,
    claim_token: str,
    fencing_version: int,
    response: dict[str, Any],
) -> dict[str, Any]:
    row = _get(execution_id)
    if _has_checkpoint(execution_id, "response_observed"):
        raise FencingRejected()
    human = _latest_human(row.tenant_scope, row.semantic_question_key, row.decision_dependency_fingerprint)
    if human is not None or row.status in {"accepted", "rejected"}:
        _archive_response(row, response, "human_decision_prevails")
        return _view(_get(execution_id))
    digest = local_fingerprint(response)
    try:
        _executor_update(
            execution_id,
            claim_token=claim_token,
            fencing_version=fencing_version,
            values={
                "status": "response_observed",
                "response_payload": dumps(response),
                "response_observed_at": utcnow_naive(),
            },
            journal=_journal(
                row,
                "response_observed",
                payload={"response": response},
                response_digest=digest,
            ),
        )
    except FencingRejected:
        current = _get(execution_id)
        human = _latest_human(current.tenant_scope, current.semantic_question_key, current.decision_dependency_fingerprint)
        if human is not None or current.status in {"accepted", "rejected"}:
            _archive_response(current, response, "human_decision_prevails")
            return _view(_get(execution_id))
        raise
    return _view(_get(execution_id))


def _financial_journal(raw_response: str | None, financial: dict[str, Any], *, omitted: bool = False, byte_length: int | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"financial": financial}
    if omitted:
        body["raw_response_omitted"] = True
        body["byte_length"] = byte_length
    else:
        body["raw_response"] = raw_response
    return body


def _write_raw_and_budget(
    execution_id: int,
    *,
    claim_token: str,
    fencing_version: int,
    values: dict[str, Any],
    checkpoint: str,
    payload: dict[str, Any],
    response_digest: str,
    applicable: bool,
    stale_reason: str | None,
    budget_outcome: str | None,
    actual_credits: Decimal | None,
) -> None:
    row = _get(execution_id)
    for _ in range(8):
        current = _get(execution_id)
        try:
            state = current.budget_state
            if budget_outcome is not None and current.budget_state == "reserved":
                state = _apply_reserved_budget(current, budget_outcome, actual_credits)
            stored = dict(values)
            if budget_outcome is not None:
                stored["budget_state"] = state
            stored["updated_at"] = utcnow_naive()
            result = db.session.execute(
                update(SemanticQuestionExecution)
                .where(
                    SemanticQuestionExecution.id == int(execution_id),
                    SemanticQuestionExecution.claim_token == claim_token,
                    SemanticQuestionExecution.fencing_version == int(fencing_version),
                )
                .values(**stored)
            )
            if result.rowcount != 1:
                db.session.rollback()
                raise FencingRejected()
            db.session.add(
                _journal(
                    current,
                    checkpoint,
                    payload=payload,
                    applicable=applicable,
                    stale_reason=stale_reason,
                    response_digest=response_digest,
                )
            )
            db.session.commit()
            return
        except FencingRejected:
            raise
        except _BudgetConflict:
            db.session.rollback()
        except SemanticExecutionError:
            db.session.rollback()
            raise
    raise BudgetExhausted()


def commit_raw_observation(
    execution_id: int,
    *,
    claim_token: str,
    fencing_version: int,
    raw_response: str,
    financial: dict[str, Any],
    actual_credits: Decimal | None,
    oversized: bool = False,
) -> dict[str, Any]:
    """Journal da resposta bruta e liquidação do custo real no mesmo commit."""
    row = _get(execution_id)
    digest = local_fingerprint(raw_response)
    human = _latest_human(row.tenant_scope, row.semantic_question_key, row.decision_dependency_fingerprint)
    byte_length = len(raw_response.encode("utf-8"))
    omitted = oversized or byte_length > REAL_MAX_RESPONSE_BYTES
    payload = _financial_journal(
        None if omitted else raw_response,
        financial,
        omitted=omitted,
        byte_length=byte_length,
    )
    if human is not None or row.status in {"accepted", "rejected"}:
        _write_raw_and_budget(
            execution_id,
            claim_token=row.claim_token or claim_token,
            fencing_version=int(row.fencing_version),
            values={
                "response_payload": None if omitted else raw_response,
                "response_observed_at": utcnow_naive(),
                "proposal_applicable": False,
                "stale_reason": "human_decision_prevails",
            },
            checkpoint="response_observed",
            payload={**payload, "historical": True},
            response_digest=digest,
            applicable=False,
            stale_reason="human_decision_prevails",
            budget_outcome=None if actual_credits is None else "settled_actual",
            actual_credits=actual_credits,
        )
        return _view(_get(execution_id))
    if omitted:
        _write_raw_and_budget(
            execution_id,
            claim_token=claim_token,
            fencing_version=fencing_version,
            values={
                "status": "failed",
                "response_payload": None,
                "response_observed_at": utcnow_naive(),
                "validation_status": "invalid",
                "validation_findings": dumps(["response_too_large"]),
                "proposal_applicable": False,
                "stale_reason": "response_too_large",
            },
            checkpoint="failed",
            payload=payload,
            response_digest=digest,
            applicable=False,
            stale_reason="response_too_large",
            budget_outcome="settled_actual",
            actual_credits=actual_credits,
        )
        return _view(_get(execution_id))
    _write_raw_and_budget(
        execution_id,
        claim_token=claim_token,
        fencing_version=fencing_version,
        values={
            "status": "response_observed",
            "response_payload": raw_response,
            "response_observed_at": utcnow_naive(),
        },
        checkpoint="response_observed",
        payload=payload,
        response_digest=digest,
        applicable=False,
        stale_reason=None,
        budget_outcome="settled_actual",
        actual_credits=actual_credits,
    )
    return _view(_get(execution_id))


def commit_terminal_budget(
    execution_id: int,
    *,
    claim_token: str,
    fencing_version: int,
    status: str,
    reason: str,
    budget_outcome: str,
    actual_credits: Decimal | None = None,
    financial: dict[str, Any] | None = None,
    raw_response: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"reason": reason}
    digest = None
    if financial is not None:
        omitted = raw_response is not None and len(raw_response.encode("utf-8")) > REAL_MAX_RESPONSE_BYTES
        payload = _financial_journal(
            None if omitted else raw_response,
            financial,
            omitted=omitted,
            byte_length=None if raw_response is None else len(raw_response.encode("utf-8")),
        )
        payload["reason"] = reason
        if raw_response is not None and not omitted:
            digest = local_fingerprint(raw_response)
    _write_raw_and_budget(
        execution_id,
        claim_token=claim_token,
        fencing_version=fencing_version,
        values={
            "status": status,
            "stale_reason": reason,
            "proposal_applicable": False,
            "response_payload": raw_response if raw_response and status == "response_observed" else None,
        },
        checkpoint=status,
        payload=payload,
        response_digest=digest or "",
        applicable=False,
        stale_reason=reason,
        budget_outcome=budget_outcome,
        actual_credits=actual_credits,
    )
    return _view(_get(execution_id))


def _proposal_from_validation(row: SemanticQuestionExecution, proposal: dict[str, Any]) -> dict[str, Any]:
    decision = (proposal.get("decisions") or [None])[0] or {}
    digest = local_fingerprint(loads(row.response_payload))
    ref = "spr:" + local_fingerprint({"attempt": row.question_attempt_key, "response": digest}).split(":", 1)[1][:24]
    snapshot = loads(row.snapshot_payload) or {}
    body = dict(proposal)
    body["proposal_ref"] = ref
    body["selected_candidate_id"] = decision.get("selected_candidate_id")
    body["evidence_refs"] = list(decision.get("evidence_refs") or [])
    body["evidence_fingerprint"] = snapshot.get("evidence_fingerprint")
    body["evidence_material"] = _evidence_material(row)
    body["question_id"] = row.question_id
    body["generation"] = int(row.generation)
    body["accepted"] = False
    body["review_state"] = "requires_review"
    body["resolution_source"] = "semantic_ai"
    body["question_status"] = "pending"
    return body


def validate_observed(
    execution_id: int,
    *,
    claim_token: str,
    fencing_version: int,
    observation: dict[str, Any],
) -> dict[str, Any]:
    row = _get(execution_id)
    if row.claim_token != claim_token or int(row.fencing_version) != int(fencing_version):
        raise FencingRejected()
    if row.status in {"accepted", "rejected"}:
        return _view(row)
    if row.status != "response_observed":
        return _view(row)
    findings = [item for item in _precheck(row, observation) if item in _DRIFT or item == "human_decision"]
    manifest = loads(row.manifest_payload) or {}
    result = validate_semantic_decisions(
        row.response_payload,
        manifest,
        dependency_expected=row.decision_dependency_fingerprint,
        dependency_current=str(observation.get("dependency_fingerprint") or ""),
    )
    if findings or not result["ok"]:
        drifted = bool(findings) or "stale_dependency" in result["findings"]
        status = "stale" if drifted else "failed"
        reason = (findings or result["findings"] or ["invalid"])[0]
        _executor_update(
            execution_id,
            claim_token=claim_token,
            fencing_version=fencing_version,
            values={
                "status": status,
                "validation_status": "invalid",
                "validation_findings": dumps(findings or result["findings"]),
                "proposal_applicable": False,
                "stale_reason": reason,
            },
            journal=_journal(row, status, payload={"findings": findings or result["findings"]}, stale_reason=reason),
        )
        _finish_with_retry(_get(execution_id), "settled")
        return _view(_get(execution_id))
    stored = _proposal_from_validation(row, result["proposal"])
    _executor_update(
        execution_id,
        claim_token=claim_token,
        fencing_version=fencing_version,
        values={"status": "validated", "validation_status": "valid", "validation_findings": dumps([])},
        journal=_journal(row, "validated", payload=stored, applicable=False),
    )
    return _view(_get(execution_id))


def record_proposal(
    execution_id: int,
    *,
    claim_token: str,
    fencing_version: int,
    stop_at: str = "requires_review",
) -> dict[str, Any]:
    row = _get(execution_id)
    if row.claim_token != claim_token or int(row.fencing_version) != int(fencing_version):
        raise FencingRejected()
    if row.status != "validated":
        return _view(row)
    journal_row = (
        db.session.query(SemanticQuestionJournal)
        .filter_by(execution_id=row.id, checkpoint="validated")
        .one()
    )
    proposal = loads(journal_row.payload)
    if not _proposal_generation_current(row):
        _executor_update(
            execution_id,
            claim_token=claim_token,
            fencing_version=fencing_version,
            values={
                "status": "stale",
                "proposal_payload": dumps(proposal),
                "proposal_ref": proposal.get("proposal_ref"),
                "proposal_applicable": False,
                "stale_reason": "superseded",
            },
            journal=_journal(row, "superseded", payload=proposal, applicable=False, stale_reason="superseded"),
        )
        _finish_with_retry(_get(execution_id), "settled")
        return _view(_get(execution_id))
    _executor_update(
        execution_id,
        claim_token=claim_token,
        fencing_version=fencing_version,
        values={
            "status": "proposal_recorded",
            "proposal_payload": dumps(proposal),
            "proposal_ref": proposal.get("proposal_ref"),
            "proposal_applicable": True,
        },
        journal=_journal(_get(execution_id), "proposal_recorded", payload=proposal, applicable=True),
    )
    if stop_at == "proposal_recorded":
        return _view(_get(execution_id))
    _executor_update(
        execution_id,
        claim_token=claim_token,
        fencing_version=fencing_version,
        values={"status": "requires_review"},
        journal=_journal(_get(execution_id), "requires_review", payload=proposal, applicable=True),
    )
    _finish_with_retry(_get(execution_id), "settled")
    return _view(_get(execution_id))


def _mark_uncertain_call(execution_id: int, *, claim_token: str, fencing_version: int) -> dict[str, Any]:
    row = _get(execution_id)
    _executor_update(
        execution_id,
        claim_token=claim_token,
        fencing_version=fencing_version,
        values={"status": "uncertain", "stale_reason": "simulated_uncertain", "proposal_applicable": False},
        journal=_journal(row, "uncertain", payload={"reason": "simulated_uncertain"}, stale_reason="simulated_uncertain"),
    )
    _finish_with_retry(_get(execution_id), "uncertain")
    return _view(_get(execution_id))


def run_fake_execution(
    execution_id: int,
    *,
    claim_token: str,
    fencing_version: int,
    observation: dict[str, Any],
    provider: FakeSemanticProvider,
    credits: Decimal | None,
    dispatch: str = "fake",
    now: datetime | None = None,
) -> dict[str, Any]:
    if dispatch != "fake":
        raise RealProviderDisabled()
    current = _get(execution_id)
    if current.claim_token != claim_token or int(current.fencing_version) != int(fencing_version):
        raise FencingRejected()
    if current.status != "claimed":
        raise SemanticExecutionError("redispatch_refused")
    checked_at = now or utcnow_naive()
    if _claim_is_expired(current, checked_at):
        return reconcile_expired_claim(execution_id, now=checked_at)
    adapter = FutureBillableCall(provider)
    started = mark_execution_started(
        execution_id,
        claim_token=claim_token,
        fencing_version=fencing_version,
        observation=observation,
        credits=credits,
        now=checked_at,
    )
    if started["status"] != "execution_started":
        return started
    manifest = started["manifest"]
    try:
        response = adapter.execute(manifest)
    except SimulatedUncertain:
        return _mark_uncertain_call(execution_id, claim_token=claim_token, fencing_version=fencing_version)
    except RealProviderDisabled:
        raise
    observed = observe_response(
        execution_id,
        claim_token=claim_token,
        fencing_version=fencing_version,
        response=response,
    )
    if observed["status"] in {"accepted", "rejected"}:
        return observed
    validated = validate_observed(
        execution_id,
        claim_token=claim_token,
        fencing_version=fencing_version,
        observation=observation,
    )
    if validated["status"] != "validated":
        return validated
    return record_proposal(
        execution_id,
        claim_token=claim_token,
        fencing_version=fencing_version,
    )


def _local_proposal(row: SemanticQuestionExecution) -> dict[str, Any] | None:
    journal_row = (
        db.session.query(SemanticQuestionJournal)
        .filter_by(execution_id=row.id, checkpoint="validated")
        .one_or_none()
    )
    if journal_row is not None and journal_row.payload:
        return loads(journal_row.payload)
    if row.status != "response_observed" or not row.response_payload:
        return None
    manifest = loads(row.manifest_payload) or {}
    result = validate_semantic_decisions(
        row.response_payload,
        manifest,
        dependency_expected=row.decision_dependency_fingerprint,
        dependency_current=row.decision_dependency_fingerprint,
    )
    if not result["ok"]:
        return None
    return _proposal_from_validation(row, result["proposal"])


_REPUBLISH_STATUSES = {"response_observed", "validated", "proposal_recorded", "requires_review"}


def _historical_proposal(row: SemanticQuestionExecution) -> dict[str, Any] | None:
    if row.proposal_payload:
        stored = loads(row.proposal_payload)
        if isinstance(stored, dict):
            return stored
    return _local_proposal(row)


def _align_human_reject(row: SemanticQuestionExecution) -> dict[str, Any]:
    if row.status == "rejected" and not row.proposal_applicable:
        return _view(row)
    proposal = _historical_proposal(row)
    values: dict[str, Any] = {
        "status": "rejected",
        "proposal_applicable": False,
        "stale_reason": "human_reject_prevails",
    }
    if proposal and not row.proposal_payload:
        values["proposal_payload"] = dumps(proposal)
        values["proposal_ref"] = proposal.get("proposal_ref")
    had_budget = row.budget_state == "reserved"
    _supersede(
        row,
        values,
        journal_checkpoint="rejected",
        journal_payload={"reason": "human_reject_prevails"},
    )
    if had_budget:
        _finish_with_retry(_get(row.id), "settled")
    return _view(_get(row.id))


def _align_human_accept(row: SemanticQuestionExecution, human: SemanticHumanReview) -> dict[str, Any]:
    if row.status == "accepted" and not row.proposal_applicable:
        return _view(row)
    had_budget = row.budget_state == "reserved"
    if human.execution_id == row.id:
        _supersede(
            row,
            {"status": "accepted", "proposal_applicable": False, "stale_reason": None},
            journal_checkpoint="accepted",
        )
    elif row.proposal_applicable or row.status in _REPUBLISH_STATUSES:
        _supersede(
            row,
            {"status": "blocked", "proposal_applicable": False, "stale_reason": "human_decision_exists"},
            journal_checkpoint="blocked",
            journal_payload={"reason": "human_decision_exists"},
        )
    else:
        return _view(row)
    if had_budget:
        _finish_with_retry(_get(row.id), "settled")
    return _view(_get(row.id))


def _align_superseded_generation(row: SemanticQuestionExecution) -> dict[str, Any]:
    if row.status == "stale" and row.stale_reason == "superseded" and not row.proposal_applicable:
        return _view(row)
    if row.status in {"accepted", "rejected"}:
        return _view(row)
    had_budget = row.budget_state == "reserved"
    _supersede(
        row,
        {"status": "stale", "proposal_applicable": False, "stale_reason": "superseded"},
        journal_checkpoint="superseded",
        journal_payload={"reason": "superseded", "generation": int(row.generation)},
    )
    if had_budget:
        _finish_with_retry(_get(row.id), "settled")
    return _view(_get(row.id))


def recover_execution(execution_id: int) -> dict[str, Any]:
    row = _get(execution_id)
    human = _vigente_human(row)
    if human is not None and human.review_state == "rejected":
        if row.status in _REPUBLISH_STATUSES or row.proposal_applicable:
            return _align_human_reject(row)
        if row.status in _TERMINAL_VIEW:
            return _view(row)
    elif human is not None and human.review_state == "accepted":
        if row.status in _REPUBLISH_STATUSES or row.proposal_applicable or row.status == "accepted":
            return _align_human_accept(row, human)
    if row.status in _REPUBLISH_STATUSES and not _proposal_generation_current(row):
        return _align_superseded_generation(row)
    if row.status in _TERMINAL_VIEW:
        return _view(row)
    if row.status == "proposal_recorded":
        proposal = loads(row.proposal_payload)
        _supersede(
            row,
            {"status": "requires_review", "proposal_applicable": True},
            journal_checkpoint="requires_review",
            journal_payload=proposal,
            applicable=True,
        )
        _finish_with_retry(_get(execution_id), "settled")
        return _view(_get(execution_id))
    if row.status in {"validated", "response_observed"}:
        proposal = _local_proposal(row)
        if proposal is None:
            _supersede(
                row,
                {"status": "failed", "validation_status": "invalid", "proposal_applicable": False, "stale_reason": "invalid"},
                journal_checkpoint="failed",
            )
            if _get(execution_id).budget_state == "reserved":
                _finish_with_retry(_get(execution_id), "settled")
            return _view(_get(execution_id))
        _supersede(
            row,
            {
                "status": "requires_review",
                "validation_status": "valid",
                "proposal_payload": dumps(proposal),
                "proposal_ref": proposal.get("proposal_ref"),
                "proposal_applicable": True,
            },
            journal_checkpoint="proposal_recorded",
            journal_payload=proposal,
            applicable=True,
        )
        fresh = _get(execution_id)
        _supersede(
            fresh,
            {"status": "requires_review"},
            journal_checkpoint="requires_review",
            journal_payload=proposal,
            applicable=True,
        )
        _finish_with_retry(_get(execution_id), "settled")
        return _view(_get(execution_id))
    if row.status in _UNPROVEN:
        had_budget = row.budget_state == "reserved"
        _supersede(
            row,
            {"status": "uncertain", "stale_reason": "crash_unproven_no_redispatch", "proposal_applicable": False},
            journal_checkpoint="uncertain",
            journal_payload={"reason": "crash_unproven_no_redispatch"},
        )
        if had_budget:
            _finish_with_retry(_get(execution_id), "uncertain")
        return _view(_get(execution_id))
    return _view(row)


def _require_reviewable(row: SemanticQuestionExecution, observation: dict[str, Any], selected: str, evidence_refs: list[str]) -> dict[str, Any]:
    if not _proposal_generation_current(row):
        raise SemanticExecutionError("superseded")
    if row.validation_status != "valid" or not row.proposal_applicable:
        raise SemanticExecutionError("proposal_not_reviewable")
    if row.status not in {"requires_review", "proposal_recorded"}:
        raise SemanticExecutionError("proposal_not_reviewable")
    proposal = loads(row.proposal_payload) or {}
    if selected != proposal.get("selected_candidate_id"):
        raise SemanticExecutionError("candidate")
    if selected is not None and selected not in set((loads(row.snapshot_payload) or {}).get("candidate_ids") or []):
        raise SemanticExecutionError("candidate")
    if list(evidence_refs) != list(proposal.get("evidence_refs") or []):
        raise SemanticExecutionError("evidence")
    snapshot = loads(row.snapshot_payload) or {}
    if observation.get("dependency_fingerprint") != row.decision_dependency_fingerprint:
        raise SemanticExecutionError("dependency")
    if observation.get("authorization_fingerprint") != snapshot.get("authorization_fingerprint"):
        raise SemanticExecutionError("authorization")
    if observation.get("queue_status") != "current":
        raise SemanticExecutionError("queue")
    if sorted(observation.get("candidate_ids") or []) != list(snapshot.get("candidate_ids") or []):
        raise SemanticExecutionError("candidate_set")
    expected_fingerprint = proposal.get("evidence_fingerprint") or snapshot.get("evidence_fingerprint")
    if observation.get("evidence_fingerprint") != expected_fingerprint:
        raise SemanticExecutionError("evidence")
    expected_material = proposal.get("evidence_material")
    if not isinstance(expected_material, list):
        expected_material = _evidence_material(row)
    expected_hashes = {
        str(item.get("evidence_id")): str(item.get("material_hash"))
        for item in expected_material
        if isinstance(item, dict)
    }
    if "evidence_material" in observation:
        observed_material = observation.get("evidence_material")
        if not isinstance(observed_material, list):
            raise SemanticExecutionError("evidence")
        current_hashes: dict[str, str] = {}
        for item in observed_material:
            if not isinstance(item, dict):
                raise SemanticExecutionError("evidence")
            current_hashes[str(item.get("evidence_id"))] = str(item.get("material_hash") or "")
        if current_hashes != expected_hashes:
            raise SemanticExecutionError("evidence")
    known = {
        item.get("evidence_id")
        for item in (loads(row.evidence_manifest) or {}).get("items") or []
        if isinstance(item, dict)
    }
    if any(item not in known for item in evidence_refs):
        raise SemanticExecutionError("evidence")
    if "question_id" in observation and observation.get("question_id") != row.question_id:
        raise SemanticExecutionError("question_identity")
    if "semantic_question_key" in observation and observation.get("semantic_question_key") != row.semantic_question_key:
        raise SemanticExecutionError("question_identity")
    if "question_type" in observation and observation.get("question_type") != row.question_type:
        raise SemanticExecutionError("question_type")
    if "manifest_digest" in observation and observation.get("manifest_digest") != row.manifest_digest:
        raise SemanticExecutionError("manifest")
    if "generation" in observation and int(observation.get("generation") or 0) != int(row.generation):
        raise SemanticExecutionError("generation")
    return proposal


def accept_human_review(
    execution_id: int,
    *,
    reviewer_ref: str,
    selected_candidate_id: str,
    evidence_refs: list[str],
    observation: dict[str, Any],
) -> dict[str, Any]:
    row = _get(execution_id)
    proposal = _require_reviewable(row, observation, selected_candidate_id, evidence_refs)
    spec = {
        "tenant_scope": row.tenant_scope,
        "semantic_question_key": row.semantic_question_key,
        "decision_dependency_fingerprint": row.decision_dependency_fingerprint,
        "candidate_ids": (loads(row.snapshot_payload) or {}).get("candidate_ids") or [],
        "evidence_items": [
            {"material": item.get("excerpt") or ""}
            for item in (loads(row.evidence_manifest) or {}).get("items") or []
            if isinstance(item, dict)
        ],
    }
    _insert_review(
        spec,
        review_state="accepted",
        reviewer_ref=reviewer_ref,
        execution_id=row.id,
        selected=selected_candidate_id,
        evidence_refs=evidence_refs,
        proposal_ref=proposal.get("proposal_ref"),
    )
    _supersede(row, {"status": "accepted", "proposal_applicable": False, "stale_reason": None}, journal_checkpoint="accepted")
    return _review_view(_latest_human(row.tenant_scope, row.semantic_question_key, row.decision_dependency_fingerprint))


def reject_human_review(
    spec: dict[str, Any],
    *,
    reviewer_ref: str,
    execution_id: int | None = None,
    selected_candidate_id: str | None = None,
    evidence_refs: list[str] | None = None,
) -> dict[str, Any]:
    row = _get(execution_id) if execution_id is not None else None
    proposal_ref = row.proposal_ref if row is not None else None
    _insert_review(
        spec,
        review_state="rejected",
        reviewer_ref=reviewer_ref,
        execution_id=execution_id,
        selected=selected_candidate_id,
        evidence_refs=list(evidence_refs or []),
        proposal_ref=proposal_ref,
    )
    if row is not None and row.status not in {"accepted"}:
        _supersede(
            row,
            {"status": "rejected", "proposal_applicable": False, "stale_reason": "human_reject_prevails"},
            journal_checkpoint="rejected",
            journal_payload={"reason": "human_reject_prevails"},
        )
    else:
        db.session.commit()
    return _review_view(
        _latest_human(spec["tenant_scope"], spec["semantic_question_key"], spec["decision_dependency_fingerprint"])
    )


def reopen_human_review(spec: dict[str, Any], *, reviewer_ref: str) -> dict[str, Any]:
    human = _latest_human(spec["tenant_scope"], spec["semantic_question_key"], spec["decision_dependency_fingerprint"])
    if human is None or human.review_state != "rejected":
        raise SemanticExecutionError("reopen_without_rejection")
    _insert_review(
        spec,
        review_state="reopened",
        reviewer_ref=reviewer_ref,
        execution_id=human.execution_id,
        selected=human.selected_candidate_ref,
        evidence_refs=loads(human.evidence_refs) or [],
        proposal_ref=human.proposal_ref,
    )
    db.session.commit()
    latest = (
        db.session.query(SemanticHumanReview)
        .filter_by(
            tenant_scope=spec["tenant_scope"],
            semantic_question_key=spec["semantic_question_key"],
            decision_dependency_fingerprint=spec["decision_dependency_fingerprint"],
            review_state="reopened",
        )
        .order_by(SemanticHumanReview.review_revision.desc())
        .first()
    )
    return _review_view(latest)


def current_ai_proposal(
    tenant_scope: str,
    question_key: str,
    dependency: str,
    ai_fingerprint: str,
) -> dict[str, Any] | None:
    row = (
        db.session.query(SemanticQuestionExecution)
        .filter_by(
            tenant_scope=tenant_scope,
            semantic_question_key=question_key,
            decision_dependency_fingerprint=dependency,
            ai_execution_fingerprint=ai_fingerprint,
            proposal_applicable=True,
        )
        .order_by(SemanticQuestionExecution.generation.desc())
        .first()
    )
    if row is None or row.status not in {"requires_review", "proposal_recorded"}:
        return None
    if not _proposal_generation_current(row):
        return None
    if _vigente_human(row) is not None:
        return None
    proposal = loads(row.proposal_payload)
    if not isinstance(proposal, dict):
        return None
    proposal["execution_id"] = row.id
    return proposal


def apply_ai_proposal_view(question: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any]:
    if proposal.get("accepted") is True:
        raise SemanticExecutionError("semantic_ai_not_acceptance")
    updated = copy.deepcopy(question)
    updated["resolution_source"] = "semantic_ai"
    updated["review_state"] = "requires_review"
    updated["status"] = "pending"
    updated["decision_ref"] = None
    return updated


def human_selection_for_rebuild(review: dict[str, Any]) -> dict[str, Any]:
    if review.get("review_state") != "accepted":
        raise SemanticExecutionError("human_selection_not_accepted")
    return {
        "kind": "validated_human_selection",
        "selected_candidate_id": review.get("selected_candidate_ref"),
        "evidence_refs": list(review.get("evidence_refs") or []),
        "acceptance_source": "human",
        "resolution_source": "human",
        "preview_only": True,
        "activated": False,
        "operational": False,
        "writes_matched_pricing_dimension_id": False,
    }


def annotate_queue(queue: dict[str, Any], execution_id: int) -> dict[str, Any]:
    row = _get(execution_id)
    updated = copy.deepcopy(queue)
    attempts = list(updated.get("attempts") or [])
    attempts.append(
        {
            "execution_id": row.id,
            "question_attempt_key": row.question_attempt_key,
            "journal_authority": True,
            "json_is_authority": False,
        }
    )
    updated["attempts"] = attempts
    return updated


def load_execution(execution_id: int) -> dict[str, Any]:
    return _view(_get(execution_id))


def journal_checkpoints(execution_id: int) -> list[str]:
    rows = (
        db.session.query(SemanticQuestionJournal)
        .filter_by(execution_id=int(execution_id))
        .order_by(SemanticQuestionJournal.id.asc())
        .all()
    )
    return [row.checkpoint for row in rows]
