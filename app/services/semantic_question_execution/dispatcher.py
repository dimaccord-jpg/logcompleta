"""Dispatch real da pergunta semântica. A flag geral nasce desligada.

Nenhuma chamada sai sem flag, ambiente, tipo, claim, orçamento e attempt key.
A fachada billable é o único transporte. O validador local continua obrigatório.
"""
from __future__ import annotations

import json
import logging
import time
from decimal import Decimal
from typing import Any, Callable

from app.extensions import db
from app.models import IaChamadaTentativa, IaConsumoEvento, SemanticQuestionExecution
from app.services.cleiton_ai_data_governance import (
    DECISION_BLOCK,
    govern_outbound_content,
)
from app.services.semantic_question_execution.billable_adapter import call_municipality_disambiguation
from app.services.semantic_question_execution.constants import (
    AGENT_SEMANTIC_QUESTION_EXECUTION,
    CONSTRAINT_ALLOWLIST,
    OUTPUT_SCHEMA_VERSION,
    POLICY_VERSION,
    PURPOSE_SEMANTIC_MUNICIPALITY_DISAMBIGUATION,
    REAL_ENABLED_QUESTION_TYPES,
    REAL_MAX_OUTPUT_TOKENS,
    REAL_MAX_RESPONSE_BYTES,
    REAL_PROMPT_VERSION,
    SYSTEM_INSTRUCTION,
    MINIMIZATION_POLICY_VERSION,
    SemanticExecutionError,
    real_config_material,
    real_dispatch_environment_allowed,
    semantic_ai_real_dispatch_enabled,
)
from app.services.semantic_question_execution.keys import (
    ai_execution_fingerprint,
    future_financial_attempt_key,
)
from app.services.semantic_question_execution.schema import decision_response_schema
from app.services.semantic_question_execution.service import (
    claim_execution,
    close_claimed_execution,
    commit_raw_observation,
    commit_terminal_budget,
    execution_findings,
    load_execution,
    mark_real_execution_started,
    open_explicit_generation,
    record_proposal,
    reserve_claimed_execution,
    validate_observed,
)

logger = logging.getLogger(__name__)

DEFAULT_REAL_MODEL = "gemini-2.5-flash"
_RECONCILED_ATTEMPT = frozenset(
    {"settled", "provider_failed", "blocked", "governance_blocked", "released"}
)
_TERMINAL_DISPATCH = frozenset(
    {
        "requires_review",
        "proposal_recorded",
        "validated",
        "response_observed",
        "accepted",
        "rejected",
        "blocked",
        "failed",
        "stale",
        "uncertain",
    }
)
_UNRECONCILED_ATTEMPT = frozenset({"uncertain", "reserved", "calling"})


def resolve_real_model(explicit: str | None = None) -> str:
    import os

    if explicit is not None:
        return explicit.strip()
    for key in ("SEMANTIC_MUNICIPALITY_MODEL", "GEMINI_MODEL_TEXT"):
        value = (os.getenv(key) or "").strip()
        if value:
            return value
    return DEFAULT_REAL_MODEL


def with_real_execution_config(spec: dict[str, Any], *, model: str | None = None) -> dict[str, Any]:
    """Identidade material da execução real. Mudança de modelo ou prompt é outra execução."""
    updated = dict(spec)
    updated["provider_id"] = "gemini"
    updated["model_id"] = resolve_real_model(model)
    updated["prompt_version"] = REAL_PROMPT_VERSION
    updated["output_schema_version"] = OUTPUT_SCHEMA_VERSION
    updated["policy_version"] = POLICY_VERSION
    updated["minimization_policy_version"] = MINIMIZATION_POLICY_VERSION
    updated["config_material"] = real_config_material()
    return updated


def build_generation_config():
    """Config explícita: tools vazios, AFC desligado, thinking mínimo, schema do contrato."""
    try:
        from google.genai import types

        thinking = types.ThinkingConfig(thinking_budget=0)
        afc = types.AutomaticFunctionCallingConfig(disable=True)
        schema = decision_response_schema()
        return types.GenerateContentConfig(
            temperature=0,
            candidate_count=1,
            max_output_tokens=REAL_MAX_OUTPUT_TOKENS,
            response_mime_type="application/json",
            response_schema=schema,
            tools=[],
            automatic_function_calling=afc,
            thinking_config=thinking,
        )
    except SemanticExecutionError:
        raise
    except Exception as exc:
        raise SemanticExecutionError("generation_config_unsupported") from exc


def project_external_payload(execution: dict[str, Any]) -> dict[str, Any]:
    """Projeção mínima. Aliases locais não saem como IDs internos."""
    manifest = execution.get("manifest") or {}
    snapshot = execution.get("snapshot") or {}
    evidence = execution.get("evidence_manifest") or {}
    alias = "q1"
    candidate_map = ((manifest.get("alias_map") or {}).get("candidates") or {}).get(alias) or {}
    public = snapshot.get("candidate_public") if isinstance(snapshot.get("candidate_public"), dict) else {}
    ambiguous = snapshot.get("ambiguous_text")
    if not isinstance(ambiguous, str) or not ambiguous.strip():
        raise SemanticExecutionError("ambiguous_text_missing")
    if not isinstance(candidate_map, dict) or not candidate_map:
        raise SemanticExecutionError("candidates_missing")
    candidates = []
    for alias_name in sorted(candidate_map):
        internal_id = candidate_map[alias_name]
        label = public.get(internal_id)
        if not isinstance(label, dict):
            raise SemanticExecutionError("candidate_label_missing")
        municipality = label.get("municipality")
        state = label.get("state")
        if not isinstance(municipality, str) or not municipality.strip():
            raise SemanticExecutionError("candidate_label_missing")
        if not isinstance(state, str) or not state.strip():
            raise SemanticExecutionError("candidate_label_missing")
        candidates.append(
            {
                "alias": alias_name,
                "municipality": municipality.strip(),
                "state": state.strip(),
            }
        )
    items = []
    for item in evidence.get("items") or []:
        if not isinstance(item, dict):
            continue
        text = item.get("excerpt")
        item_alias = item.get("alias")
        if not isinstance(item_alias, str) or not isinstance(text, str):
            raise SemanticExecutionError("evidence_projection")
        items.append({"alias": item_alias, "text": text})
    if not items:
        raise SemanticExecutionError("evidence_projection")
    constraints_in = manifest.get("constraints") if isinstance(manifest.get("constraints"), dict) else {}
    constraints: dict[str, str] = {}
    for key in sorted(CONSTRAINT_ALLOWLIST):
        value = constraints_in.get(key)
        if isinstance(value, str) and value.strip():
            constraints[key] = value.strip()
    return {
        "question_alias": alias,
        "ambiguous_text": ambiguous.strip(),
        "constraints": constraints,
        "candidates": candidates,
        "evidence": items,
    }


def _geographic_tokens(payload: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for candidate in payload.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        for key in ("municipality", "state"):
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                tokens.add(value.strip().lower())
    return tokens


def evidence_keeps_geography(original: dict[str, Any], safe: dict[str, Any]) -> bool:
    geo = _geographic_tokens(original)
    safe_items = {
        item.get("alias"): item.get("text")
        for item in safe.get("evidence") or []
        if isinstance(item, dict)
    }
    survived = False
    had_geo = False
    for item in original.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "")
        hits = {token for token in geo if token and token in text.lower()}
        if not hits:
            continue
        had_geo = True
        safe_text = str(safe_items.get(item.get("alias")) or "")
        if hits & {token for token in geo if token in safe_text.lower()}:
            survived = True
    return had_geo and survived


def _attempt_key_valid(execution: dict[str, Any]) -> bool:
    key = execution.get("financial_attempt_key") or ""
    if not isinstance(key, str) or not key.startswith("sha256:") or not 1 <= len(key) <= 160:
        return False
    expected = future_financial_attempt_key(
        question_attempt_key=str(execution["question_attempt_key"]),
        batch_request_key=str(execution["batch_request_key"]),
        generation=int(execution["generation"]),
    )
    return key == expected


def _config_fingerprint(execution: dict[str, Any], model: str) -> str:
    return ai_execution_fingerprint(
        decision_dependency_fingerprint=str(execution["decision_dependency_fingerprint"]),
        prompt_version=REAL_PROMPT_VERSION,
        output_schema_version=OUTPUT_SCHEMA_VERSION,
        provider_id="gemini",
        model_id=model,
        policy_version=POLICY_VERSION,
        minimization_policy_version=MINIMIZATION_POLICY_VERSION,
        config_material=real_config_material(),
    )


def _rows_for_question(execution: dict[str, Any]) -> list[SemanticQuestionExecution]:
    return (
        db.session.query(SemanticQuestionExecution)
        .filter_by(
            tenant_scope=execution["tenant_scope"],
            semantic_question_key=execution["semantic_question_key"],
            decision_dependency_fingerprint=execution["decision_dependency_fingerprint"],
        )
        .all()
    )


def _attempt_row(attempt_key: str | None) -> IaChamadaTentativa | None:
    if not attempt_key:
        return None
    return db.session.query(IaChamadaTentativa).filter_by(attempt_key=attempt_key).one_or_none()


def real_financial_unreconciled(execution: dict[str, Any]) -> bool:
    """Tentativa real incerta bloqueia outra generation mesmo com outro modelo."""
    for row in _rows_for_question(execution):
        attempt = _attempt_row(row.financial_attempt_key)
        if attempt is None:
            continue
        if attempt.status in _UNRECONCILED_ATTEMPT:
            return True
        if row.status == "uncertain" and attempt.status not in _RECONCILED_ATTEMPT:
            return True
        if row.status == "uncertain" and attempt.status == "uncertain":
            return True
    return False


def open_real_generation(spec: dict[str, Any]) -> dict[str, Any]:
    probe = {
        "tenant_scope": spec["tenant_scope"],
        "semantic_question_key": spec["semantic_question_key"],
        "decision_dependency_fingerprint": spec["decision_dependency_fingerprint"],
    }
    if real_financial_unreconciled(probe):
        raise SemanticExecutionError("unreconciled_financial_attempt")
    opened = dict(spec)
    opened["explicit_retry"] = True
    return open_explicit_generation(with_real_execution_config(opened))


def _financial_view(attempt: IaChamadaTentativa | None, result: Any | None = None) -> dict[str, Any]:
    event = None
    if attempt is not None and attempt.ia_consumo_evento_id is not None:
        event = db.session.get(IaConsumoEvento, int(attempt.ia_consumo_evento_id))
    actual = None
    reserved = None
    if result is not None:
        actual = getattr(result, "actual_credits", None)
        reserved = getattr(result, "reserved_credits", None)
    if attempt is not None:
        if actual is None:
            actual = attempt.actual_credits
        if reserved is None:
            reserved = attempt.reserved_credits
    return {
        "attempt_key": None if attempt is None else attempt.attempt_key,
        "attempt_id": None if attempt is None else attempt.id,
        "evento_id": None if attempt is None else attempt.ia_consumo_evento_id,
        "input_tokens": None if event is None else event.input_tokens,
        "output_tokens": None if event is None else event.output_tokens,
        "total_tokens": None if event is None else event.total_tokens,
        "reserved_credits": None if reserved is None else str(reserved),
        "actual_credits": None if actual is None else str(actual),
        "financial_status": None if attempt is None else attempt.status,
        "persist_status": None if result is None else getattr(result, "persist_status", None),
    }


def _raw_text(result: Any) -> str | None:
    response = getattr(result, "response", None)
    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text
    return None


def _result(execution: dict[str, Any], *, dispatched: bool, reason: str | None) -> dict[str, Any]:
    body = dict(execution)
    body["dispatched"] = dispatched
    body["reason"] = reason
    return body


def _log(execution: dict[str, Any], *, outcome: str, started: float, **fields: Any) -> None:
    logger.info(
        "semantic_real_dispatch execution_id=%s question_type=%s attempt_ref=%s generation=%s "
        "fencing_version=%s outcome=%s governance=%s billing_status=%s billing_reason=%s "
        "validation_status=%s validation_reasons=%s review_state=%s latency_ms=%s",
        execution.get("id"),
        execution.get("question_type"),
        fields.get("attempt_ref"),
        execution.get("generation"),
        execution.get("fencing_version"),
        outcome,
        fields.get("governance"),
        fields.get("billing_status"),
        fields.get("billing_reason"),
        execution.get("validation_status"),
        fields.get("validation_reasons"),
        (execution.get("proposal") or {}).get("review_state") if isinstance(execution.get("proposal"), dict) else None,
        int((time.monotonic() - started) * 1000),
    )


def _guard_payload(payload: dict[str, Any]) -> tuple[str, str]:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    governed = govern_outbound_content(
        encoded,
        purpose=PURPOSE_SEMANTIC_MUNICIPALITY_DISAMBIGUATION,
        content_type="text",
        agent=AGENT_SEMANTIC_QUESTION_EXECUTION,
        provider="gemini",
    )
    if governed.decision == DECISION_BLOCK:
        raise SemanticExecutionError("governance_blocked")
    safe_text = governed.safe_content
    if not isinstance(safe_text, str):
        raise SemanticExecutionError("governance_blocked")
    try:
        safe_payload = json.loads(safe_text)
    except json.JSONDecodeError as exc:
        raise SemanticExecutionError("decisive_evidence_minimized") from exc
    if not isinstance(safe_payload, dict) or not evidence_keeps_geography(payload, safe_payload):
        raise SemanticExecutionError("decisive_evidence_minimized")
    return safe_text, governed.decision


def dispatch_real_question(
    execution_id: int,
    *,
    observation_loader: Callable[[], dict[str, Any]],
    client: Any,
    usuario: Any,
    max_reserved_credits: Decimal | None,
    model: str | None = None,
    after_journal: Callable[[], None] | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    execution = load_execution(execution_id)
    outcome = "blocked"
    governance = None
    billing_status = None
    billing_reason = None
    validation_reasons = None
    attempt_ref = execution.get("financial_attempt_key")
    try:
        if execution["status"] in _TERMINAL_DISPATCH or execution["response"] is not None:
            outcome = "replay"
            return _result(execution, dispatched=False, reason="replay")
        if real_financial_unreconciled(execution):
            outcome = "unreconciled"
            return _result(execution, dispatched=False, reason="unreconciled_financial_attempt")
        if not semantic_ai_real_dispatch_enabled():
            outcome = "flag_disabled"
            return _result(execution, dispatched=False, reason="semantic_ai_real_dispatch_disabled")
        if not real_dispatch_environment_allowed():
            outcome = "env_blocked"
            return _result(execution, dispatched=False, reason="app_env_blocked")
        if execution["question_type"] not in REAL_ENABLED_QUESTION_TYPES:
            outcome = "type_blocked"
            return _result(execution, dispatched=False, reason="question_type_disabled")
        resolved_model = resolve_real_model(model)
        if client is None or not resolved_model:
            outcome = "config_blocked"
            return _result(execution, dispatched=False, reason="client_or_model_missing")
        if execution["ai_execution_fingerprint"] != _config_fingerprint(execution, resolved_model):
            outcome = "config_mismatch"
            return _result(execution, dispatched=False, reason="execution_config_mismatch")
        if not _attempt_key_valid(execution):
            outcome = "attempt_key_invalid"
            return _result(execution, dispatched=False, reason="financial_attempt_key_invalid")
        if _attempt_row(execution["financial_attempt_key"]) is not None:
            outcome = "replay"
            return _result(execution, dispatched=False, reason="financial_attempt_exists")

        observation = observation_loader()
        findings = execution_findings(
            execution_id,
            observation,
            enabled_types=REAL_ENABLED_QUESTION_TYPES,
        )
        if execution["status"] == "prepared":
            claimed = claim_execution(execution_id)
            if not claimed.get("won"):
                outcome = "claim_lost"
                return _result(load_execution(execution_id), dispatched=False, reason="claim_lost")
            execution = load_execution(execution_id)
        elif execution["status"] != "claimed":
            outcome = "replay"
            return _result(execution, dispatched=False, reason="replay")
        claim_token = execution["claim_token"]
        fencing_version = int(execution["fencing_version"])
        observation = observation_loader()
        findings = execution_findings(
            execution_id,
            observation,
            enabled_types=REAL_ENABLED_QUESTION_TYPES,
        )
        if findings:
            closed = close_claimed_execution(
                execution_id,
                claim_token=claim_token,
                fencing_version=fencing_version,
                findings=findings,
            )
            outcome = closed["status"]
            return _result(closed, dispatched=False, reason=closed.get("stale_reason"))

        reserved = reserve_claimed_execution(
            execution_id,
            claim_token=claim_token,
            fencing_version=fencing_version,
            credits=max_reserved_credits,
        )
        if reserved["status"] != "claimed" or reserved["budget_state"] != "reserved":
            outcome = reserved["status"]
            return _result(reserved, dispatched=False, reason=reserved.get("stale_reason"))
        execution = reserved

        try:
            payload = project_external_payload(execution)
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if len(encoded) > int((execution["manifest"].get("request_limits") or {}).get("max_payload_bytes") or 8192):
                raise SemanticExecutionError("max_payload_bytes")
            safe_text, governance = _guard_payload(payload)
            # A Gemini API não aceita system_instruction em countTokens.
            # A instrução vai no conteúdo contado, fora do JSON documental.
            safe_text = SYSTEM_INSTRUCTION + "\n" + safe_text
            config = build_generation_config()
        except SemanticExecutionError as exc:
            billing_reason = exc.code
            closed = commit_terminal_budget(
                execution_id,
                claim_token=claim_token,
                fencing_version=fencing_version,
                status="blocked",
                reason=exc.code,
                budget_outcome="released",
            )
            outcome = "blocked"
            return _result(closed, dispatched=False, reason=exc.code)

        started_execution = mark_real_execution_started(
            execution_id,
            claim_token=claim_token,
            fencing_version=fencing_version,
        )
        execution = started_execution
        try:
            result = call_municipality_disambiguation(
                client,
                model=resolved_model,
                contents=safe_text,
                config=config,
                attempt_key=str(execution["financial_attempt_key"]),
                usuario=usuario,
                max_reserved_credits=Decimal(str(max_reserved_credits)),
            )
        except Exception as exc:
            return _map_provider_failure(
                execution_id,
                claim_token=claim_token,
                fencing_version=fencing_version,
                attempt_key=str(execution["financial_attempt_key"]),
                exc=exc,
            )

        attempt = _attempt_row(execution["financial_attempt_key"])
        financial = _financial_view(attempt, result)
        billing_status = financial.get("financial_status")
        actual = getattr(result, "actual_credits", None)
        if actual is None:
            closed = commit_terminal_budget(
                execution_id,
                claim_token=claim_token,
                fencing_version=fencing_version,
                status="uncertain",
                reason="actual_credits_missing",
                budget_outcome="uncertain",
                financial=financial,
                raw_response=_raw_text(result),
            )
            outcome = "uncertain"
            return _result(closed, dispatched=True, reason="actual_credits_missing")
        raw = _raw_text(result)
        if raw is None:
            closed = commit_terminal_budget(
                execution_id,
                claim_token=claim_token,
                fencing_version=fencing_version,
                status="failed",
                reason="response_text_missing",
                budget_outcome="settled_actual",
                actual_credits=Decimal(str(actual)),
                financial=financial,
            )
            outcome = "failed"
            return _result(closed, dispatched=True, reason="response_text_missing")
        observed = commit_raw_observation(
            execution_id,
            claim_token=claim_token,
            fencing_version=fencing_version,
            raw_response=raw,
            financial=financial,
            actual_credits=Decimal(str(actual)),
            oversized=len(raw.encode("utf-8")) > REAL_MAX_RESPONSE_BYTES,
        )
        if after_journal is not None:
            after_journal()
        if observed["status"] != "response_observed":
            outcome = observed["status"]
            return _result(observed, dispatched=True, reason=observed.get("stale_reason"))
        current_observation = observation_loader()
        validated = validate_observed(
            execution_id,
            claim_token=claim_token,
            fencing_version=fencing_version,
            observation=current_observation,
        )
        validation_reasons = validated.get("validation_findings")
        if validated["status"] != "validated":
            outcome = validated["status"]
            return _result(validated, dispatched=True, reason=validated.get("stale_reason"))
        recorded = record_proposal(
            execution_id,
            claim_token=claim_token,
            fencing_version=fencing_version,
        )
        outcome = recorded["status"]
        return _result(recorded, dispatched=True, reason=None)
    finally:
        fresh = load_execution(execution_id)
        _log(
            fresh,
            outcome=outcome,
            started=started,
            attempt_ref=attempt_ref,
            governance=governance,
            billing_status=billing_status,
            billing_reason=billing_reason,
            validation_reasons=validation_reasons,
        )


def _map_provider_failure(
    execution_id: int,
    *,
    claim_token: str,
    fencing_version: int,
    attempt_key: str,
    exc: BaseException,
) -> dict[str, Any]:
    from app.services.cleiton_ai_data_governance import CleitonAiGovernanceBlockedError
    from app.services.cleiton_billable_ai_call import (
        BillableAiAdmissionBlocked,
        BillableAiGovernanceBlocked,
        BillableAiUncertainError,
    )

    db.session.rollback()
    attempt = _attempt_row(attempt_key)
    financial = _financial_view(attempt)
    if isinstance(exc, (BillableAiGovernanceBlocked, CleitonAiGovernanceBlockedError)):
        closed = commit_terminal_budget(
            execution_id,
            claim_token=claim_token,
            fencing_version=fencing_version,
            status="blocked",
            reason="governance_blocked",
            budget_outcome="released",
            financial=financial,
        )
        return _result(closed, dispatched=False, reason="governance_blocked")
    if isinstance(exc, BillableAiAdmissionBlocked):
        closed = commit_terminal_budget(
            execution_id,
            claim_token=claim_token,
            fencing_version=fencing_version,
            status="blocked",
            reason=str(getattr(exc, "motivo", None) or "admission_blocked"),
            budget_outcome="released",
            financial=financial,
        )
        return _result(closed, dispatched=False, reason=closed.get("stale_reason"))
    if isinstance(exc, BillableAiUncertainError):
        raw = None
        response = getattr(exc, "response", None)
        text = getattr(response, "text", None)
        if isinstance(text, str):
            raw = text
        closed = commit_terminal_budget(
            execution_id,
            claim_token=claim_token,
            fencing_version=fencing_version,
            status="uncertain",
            reason=str(getattr(exc, "motivo", None) or "uncertain"),
            budget_outcome="uncertain",
            financial=financial,
            raw_response=raw,
        )
        return _result(closed, dispatched=True, reason="uncertain")
    if attempt is not None and attempt.status in {"settled", "provider_failed"} and attempt.actual_credits is not None:
        closed = commit_terminal_budget(
            execution_id,
            claim_token=claim_token,
            fencing_version=fencing_version,
            status="failed",
            reason="provider_failed",
            budget_outcome="settled_actual",
            actual_credits=Decimal(str(attempt.actual_credits)),
            financial=financial,
        )
        return _result(closed, dispatched=True, reason="provider_failed")
    closed = commit_terminal_budget(
        execution_id,
        claim_token=claim_token,
        fencing_version=fencing_version,
        status="uncertain",
        reason="provider_exception_unreconciled",
        budget_outcome="uncertain",
        financial=financial,
    )
    return _result(closed, dispatched=True, reason="provider_exception_unreconciled")
