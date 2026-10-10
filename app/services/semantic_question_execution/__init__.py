"""Infraestrutura durável da pergunta semântica. Provider real desligado.

Garantia: não é exactly-once.
No máximo uma tentativa ativa por pergunta, snapshot e generation.
A mesma pergunta em outro batch reutiliza essa execução.
Resultado incerto não redispara.
Retry explícito cria outra generation.
"""
from app.services.semantic_question_execution.constants import (
    FAKE_ENABLED_QUESTION_TYPES,
    MAX_QUESTIONS_PER_REQUEST,
    REAL_ENABLED_QUESTION_TYPES,
    BudgetExhausted,
    BudgetNotConfigured,
    FencingRejected,
    ManifestImmutable,
    RealProviderDisabled,
    SemanticExecutionError,
)
from app.services.semantic_question_execution.fake_provider import FakeSemanticProvider, FutureBillableCall
from app.services.semantic_question_execution.keys import (
    ai_execution_fingerprint,
    batch_request_key,
    future_financial_attempt_key,
    question_attempt_key,
)
from app.services.semantic_question_execution.service import (
    accept_human_review,
    annotate_queue,
    apply_ai_proposal_view,
    claim_execution,
    configure_job_budget,
    current_ai_proposal,
    human_selection_for_rebuild,
    journal_checkpoints,
    load_execution,
    lookup_human_review,
    mark_execution_started,
    mutate_manifest,
    observe_response,
    open_explicit_generation,
    prepare_execution,
    real_dispatch_decision,
    reconcile_expired_claim,
    record_proposal,
    recover_execution,
    reject_human_review,
    reopen_human_review,
    replay_execution,
    reserve_job_budget,
    run_fake_execution,
    validate_observed,
)
from app.services.semantic_question_execution.validator import validate_semantic_decisions

__all__ = [
    "FAKE_ENABLED_QUESTION_TYPES",
    "MAX_QUESTIONS_PER_REQUEST",
    "REAL_ENABLED_QUESTION_TYPES",
    "BudgetExhausted",
    "BudgetNotConfigured",
    "FakeSemanticProvider",
    "FencingRejected",
    "FutureBillableCall",
    "ManifestImmutable",
    "RealProviderDisabled",
    "SemanticExecutionError",
    "accept_human_review",
    "ai_execution_fingerprint",
    "annotate_queue",
    "apply_ai_proposal_view",
    "batch_request_key",
    "claim_execution",
    "configure_job_budget",
    "current_ai_proposal",
    "future_financial_attempt_key",
    "human_selection_for_rebuild",
    "journal_checkpoints",
    "load_execution",
    "lookup_human_review",
    "mark_execution_started",
    "mutate_manifest",
    "observe_response",
    "open_explicit_generation",
    "prepare_execution",
    "question_attempt_key",
    "real_dispatch_decision",
    "reconcile_expired_claim",
    "record_proposal",
    "recover_execution",
    "reject_human_review",
    "reopen_human_review",
    "replay_execution",
    "reserve_job_budget",
    "run_fake_execution",
    "validate_observed",
    "validate_semantic_decisions",
]
