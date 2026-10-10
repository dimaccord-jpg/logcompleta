"""Tipos e constantes do Semantic Freight Contract v1.

Camada anterior ao pricing_contract. Não é o contrato operacional e não
autoriza o motor de cálculo. O projeto não depende de Pydantic; estes
valores são a fonte única reutilizada pelo schema JSON.
"""
from __future__ import annotations

SEMANTIC_CONTRACT_VERSION = "1"
OUTPUT_SCHEMA_VERSION = "1"
PROMPT_VERSION = "sfc-builder-v1"
COMPILER_VERSION = "sfc-compiler-experimental-v1"
TECHNICAL_JSON_KEY = "semantic_freight_contract"

# Política de execução já existente no motor, registrada como convenção.
# Não reescreve o intervalo declarado no documento.
BOUNDARY_POLICY_LEGACY_HALF_OPEN = "legacy_contiguous_half_open_v1"

MAX_CHUNKS_PER_BUILD = 4
MAX_CELLS_PER_CHUNK = 36
MAX_SNIPPET_CHARS = 80
MAX_STATEMENT_CHARS = 300

ENTITY_KINDS = ("state", "municipality")
DIMENSION_KINDS = (
    "state",
    "municipality",
    "state_capital",
    "state_interior",
    "postal_code",
    "postal_code_range",
    "carrier_defined_region",
    "zone",
    "group",
    "origin_destination_lane",
    "weight_band",
    "weight_rate",
)
PRICING_TYPES = ("fixed_range", "direct_weight_rate", "range_plus_excess_per_kg")
ORIGIN_SCOPES = ("single", "multiple", "unspecified")
ORIGIN_KINDS = ("municipality", "state", "carrier_defined_region", "unspecified")
DESTINATION_KINDS = ("municipality", "state", "pricing_dimension")
CONDITION_KINDS = (
    "accessorial",
    "freight_minimum",
    "cubage",
    "tax",
    "delivery_term",
    "validity",
)
ACCESSORIAL_NAMES = (
    "gris",
    "ad_valorem",
    "toll",
    "tde",
    "tda",
    "insurance",
    "administrative_fee",
    "operational_fee",
)
TAX_NAMES = ("icms",)
PRESENCE_VALUES = ("observed", "absent_in_examined_scope")
APPLICABILITY_VALUES = (
    "applied",
    "explicitly_not_applied",
    "not_informed",
    "unknown",
)
ASSERTION_KINDS = ("FACT", "INTERPRETATION")
REVIEW_STATES = ("proposed", "accepted", "requires_review", "rejected")
ACCEPTANCE_SOURCES = ("deterministic_policy", "human")
CONFIDENCE_VALUES = ("low", "medium", "high")
MEANING_STATUSES = ("explicit", "unresolved")
UNITS = ("kg", "ton")
CURRENCIES = ("BRL",)
EVIDENCE_ROLES = ("header", "cell", "note", "origin", "unit", "condition", "table")

BR_UFS = frozenset(
    {
        "AC",
        "AL",
        "AP",
        "AM",
        "BA",
        "CE",
        "DF",
        "ES",
        "GO",
        "MA",
        "MT",
        "MS",
        "MG",
        "PA",
        "PB",
        "PR",
        "PE",
        "PI",
        "RJ",
        "RN",
        "RS",
        "RO",
        "RR",
        "SC",
        "SP",
        "SE",
        "TO",
    }
)

FINGERPRINT_KEYS = frozenset(
    {
        "source_fingerprint_local",
        "evidence_fingerprint_local",
        "preview_fingerprint_local",
    }
)

# Frase fechada, depois de normalizar acentos. Não é parser de região.
EXPLICIT_NOT_APPLIED_VALUES = frozenset(
    {
        "NAO APLICADO",
        "NAO SE APLICA",
        "EXPLICITAMENTE NAO APLICADO",
    }
)

# Cabeçalho já reconhecido no domínio atual. Fast-path auxiliar, não identidade.
KNOWN_CONDITION_HEADERS = {
    "GRIS": ("accessorial", "gris"),
    "AD VALOREM": ("accessorial", "ad_valorem"),
    "ADV": ("accessorial", "ad_valorem"),
    "PEDAGIO": ("accessorial", "toll"),
    "TDE": ("accessorial", "tde"),
    "TDA": ("accessorial", "tda"),
    "CUBAGEM": ("cubage", "cubage"),
    "FRETE MINIMO": ("freight_minimum", "freight_minimum"),
    "ICMS": ("tax", "icms"),
}
