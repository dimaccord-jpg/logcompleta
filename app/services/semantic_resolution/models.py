"""Semantic Resolution v1.

Aplicação de um Semantic Freight Contract a uma entidade operacional.
Não é o contrato tarifário, não é o pricing_contract e não autoriza cálculo.
"""
from __future__ import annotations

SCHEMA_VERSION = "1"
RESOLUTION_POLICY_VERSION = "sr-policy-v1"
IDENTITY_POLICY_VERSION = "sr-identity-v1"
TECHNICAL_JSON_KEY = "semantic_resolution_preview"
QUEUE_JSON_KEY = "semantic_resolution_queue"
QUESTION_SCHEMA_VERSION = "1"
QUEUE_SCHEMA_VERSION = "1"
QUESTION_POLICY_VERSION = "sq-policy-v1"

QUESTION_TYPES = (
    "municipality_text_disambiguation",
    "dimension_label_disambiguation",
    "explicit_documentary_relation_extraction",
)
ELIGIBILITY_VALUES = (
    "deterministically_resolvable",
    "ai_eligible",
    "human_only",
    "unresolved_missing_evidence",
    "conflicting_authoritative_evidence",
)
QUESTION_STATUSES = ("pending", "in_progress", "resolved", "blocked", "failed")
QUESTION_REVIEW_STATES = ("not_required", "requires_review", "accepted", "rejected")
# semantic_ai é só a origem de uma proposta. Não aceita, não resolve e não entra em RESOLUTION_BASES.
RESOLUTION_SOURCES = ("deterministic", "cache", "human", "semantic_ai")

# Tetos locais da preparação. Não são franquia, billing nem SLA.
# max_rows cabe no teste sintético de 10_000 linhas repetidas.
# Valor ausente ou inválido falha fechado.
DEFAULT_MAX_ROWS = 10_000
DEFAULT_MAX_UNIQUE_IDENTITIES = 512
DEFAULT_MAX_RESOLUTION_CONTEXTS = 512
DEFAULT_MAX_QUESTIONS = 512
DEFAULT_MAX_SHARED_EVIDENCE_ITEMS = 2_048
DEFAULT_MAX_QUEUE_SERIALIZED_BYTES = 4_000_000

RESOLUTION_STATUSES = ("resolved", "unresolved", "conflicting")
REVIEW_STATES = ("accepted", "requires_review", "rejected")

# Bases com lógica neste lote. As reservadas existem no schema e não são emitidas.
RESOLUTION_BASES = (
    "exact_entity",
    "explicit_relation",
    "coverage",
    "canonical_geography",
    "human_override",
    "unresolved",
)
RESERVED_INACTIVE_BASES = (
    "semantic_ai",
    "contract_predicate",
    "documentary_relation",
)

EVIDENCE_SOURCE_TYPES = (
    "contract",
    "coverage",
    "operational",
    "geography",
    "human_review",
)

ACCEPTANCE_SOURCES = ("deterministic_policy", "human")
CONFIDENCE_VALUES = ("none", "low", "medium", "high")

REASON_CODES = (
    "invalid_state",
    "ambiguous_municipality",
    "structured_text_conflict",
    "unsupported_country",
    "unsupported_origin_scope",
    "contract_entity_not_found",
    "dimension_not_found",
    "dimension_not_accepted",
    "exact_entity_match",
    "state_dimension_match",
    "coverage_match",
    "ambiguous_coverage",
    "conflicting_coverage",
    "origin_required",
    "lane_ambiguous",
    "lane_incompatible",
    "carrier_region_membership_missing",
    "missing_capital_authority",
    "unsafe_contract_predicate",
    "requires_geographic_authority",
    "overlapping_dimensions",
    "stale_dependency",
)

CAPITAL_INTERIOR_REASONS = (
    "missing_capital_authority",
    "unsafe_contract_predicate",
    "requires_geographic_authority",
)

UNSAFE_DIMENSION_KINDS = frozenset({"state_capital", "state_interior"})
EXPLICIT_PRECEDENCE = "overrides_applicable"

CACHE_KEY_FIELDS = (
    "tenant_scope",
    "contract_id",
    "contract_revision",
    "contract_content_fingerprint",
    "contract_authorization_fingerprint",
    "destination_identity",
    "origin_identity",
    "carrier_scope",
    "service_scope",
    "effective_date",
    "coverage_content_fingerprint",
    "geography_revision",
    "complementary_evidence_fingerprint",
    "human_decision_revision",
    "identity_policy_version",
    "resolution_policy_version",
)
