"""Catálogo fechado das consultas analíticas do chat do Agente Compara.

SCRUM-245. Valida o plano de consultas. Não interpreta a pergunta do usuário,
não executa análise e não lê armazenamento.

Cursores opacos assinados não fazem parte deste catálogo. O armazenamento
durável da operação ainda não verifica assinatura. A página é só offset e
limite, ligados ao plano já validado.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

CATALOG_VERSION = "comparison_chat_query_catalog_v1"
WEIGHT_BANDS_VERSION = "weight_bands_v1"
CURSOR_CONTRACT = "unsigned_offset_only"

MAX_QUERIES_PER_PLAN = 4
MAX_PLAN_BYTES = 16 * 1024
MAX_PAGE_SIZE = 50
MAX_EVIDENCE_REFS = 100
MAX_CONTEXT_CHARS = 48_000
MAX_POPULATION = 100_000
MAX_PROCESSING_SECONDS = 30.0

DEFAULT_MAX_POPULATION = 50_000
DEFAULT_MAX_PROCESSING_SECONDS = 10.0

QUERY_OFFICIAL_SUMMARY = "official_summary"
QUERY_FREIGHT_COMPONENTS = "freight_components"
QUERY_CARRIER_COMPARISON = "carrier_comparison"
QUERY_TOP_DIFFERENCES = "top_differences"
QUERY_BY_UF = "by_uf"
QUERY_BY_WEIGHT = "by_weight"
QUERY_INCOMPLETE_FREIGHTS = "incomplete_freights"
QUERY_NON_CALCULATION_DIAGNOSTICS = "non_calculation_diagnostics"

QUERY_TYPES = frozenset(
    {
        QUERY_OFFICIAL_SUMMARY,
        QUERY_FREIGHT_COMPONENTS,
        QUERY_CARRIER_COMPARISON,
        QUERY_TOP_DIFFERENCES,
        QUERY_BY_UF,
        QUERY_BY_WEIGHT,
        QUERY_INCOMPLETE_FREIGHTS,
        QUERY_NON_CALCULATION_DIAGNOSTICS,
    }
)

PAGINATED_QUERY_TYPES = frozenset(
    {
        QUERY_FREIGHT_COMPONENTS,
        QUERY_TOP_DIFFERENCES,
        QUERY_BY_UF,
        QUERY_BY_WEIGHT,
        QUERY_INCOMPLETE_FREIGHTS,
        QUERY_NON_CALCULATION_DIAGNOSTICS,
    }
)

SORT_ROW_INDEX_ASC = "row_index_asc"
SORT_ABSOLUTE_GAP_DESC = "absolute_gap_desc_row_index_asc"
SORT_UF_ASC = "uf_asc"
SORT_BAND_ASC = "band_asc"

SORTS_BY_TYPE: dict[str, frozenset[str]] = {
    QUERY_OFFICIAL_SUMMARY: frozenset(),
    QUERY_FREIGHT_COMPONENTS: frozenset({SORT_ROW_INDEX_ASC}),
    QUERY_CARRIER_COMPARISON: frozenset(),
    QUERY_TOP_DIFFERENCES: frozenset({SORT_ABSOLUTE_GAP_DESC}),
    QUERY_BY_UF: frozenset({SORT_UF_ASC}),
    QUERY_BY_WEIGHT: frozenset({SORT_BAND_ASC}),
    QUERY_INCOMPLETE_FREIGHTS: frozenset({SORT_ROW_INDEX_ASC}),
    QUERY_NON_CALCULATION_DIAGNOSTICS: frozenset({SORT_ROW_INDEX_ASC}),
}

DEFAULT_SORT_BY_TYPE = {
    QUERY_FREIGHT_COMPONENTS: SORT_ROW_INDEX_ASC,
    QUERY_TOP_DIFFERENCES: SORT_ABSOLUTE_GAP_DESC,
    QUERY_BY_UF: SORT_UF_ASC,
    QUERY_BY_WEIGHT: SORT_BAND_ASC,
    QUERY_INCOMPLETE_FREIGHTS: SORT_ROW_INDEX_ASC,
    QUERY_NON_CALCULATION_DIAGNOSTICS: SORT_ROW_INDEX_ASC,
}

# Faixas versionadas. Limite inferior inclusivo e superior exclusivo.
# A última faixa não tem teto: vale para todo peso válido a partir do piso.
WEIGHT_BANDS_V1: tuple[tuple[str, str | None], ...] = (
    ("0", "10"),
    ("10", "30"),
    ("30", "50"),
    ("50", "100"),
    ("100", "300"),
    ("300", "1000"),
    ("1000", None),
)

OFFICIAL_UF_CODES = frozenset(
    {
        "AC",
        "AL",
        "AM",
        "AP",
        "BA",
        "CE",
        "DF",
        "ES",
        "GO",
        "MA",
        "MG",
        "MS",
        "MT",
        "PA",
        "PB",
        "PE",
        "PI",
        "PR",
        "RJ",
        "RN",
        "RO",
        "RR",
        "RS",
        "SC",
        "SE",
        "SP",
        "TO",
    }
)

COMPONENT_CODES = frozenset(
    {
        "weight_freight",
        "freight_value",
        "toll",
        "icms",
        "iss",
        "gris",
        "ad_valorem",
        "accessorial",
        "subtotal",
        "taxes",
        "total",
    }
)

# Códigos canônicos explícitos. Rótulo livre não entra nesta tabela.
# GRIS e Ad Valorem permanecem distintos. risk_management é o código oficial
# já usado para GRIS; não é Ad Valorem.
CANONICAL_COMPONENT_CODES = {
    "gris": "gris",
    "risk_management": "gris",
    "ad_valorem": "ad_valorem",
}

OFFICIAL_DIAGNOSTIC_CODES = frozenset(
    {
        "missing_coverage_mapping",
        "ambiguous_coverage_mapping",
        "missing_freight_rule",
        "invalid_weight",
        "invalid_invoice_value",
        "unsupported_pricing_model",
        "not_calculated",
    }
)

# Contrato público de publicação. Independente de CONTRACT_VERSION do Lote 1.
PUBLICATION_VERSION = "comparison_chat_publication_v1"

PUBLIC_COMPONENT_LABELS = {
    "weight_freight": "Frete peso",
    "freight_value": "Frete valor",
    "toll": "Pedágio",
    "icms": "ICMS",
    "iss": "ISS",
    "gris": "GRIS",
    "ad_valorem": "Ad Valorem",
    "accessorial": "Acessório",
    "subtotal": "Subtotal",
    "taxes": "Impostos",
    "total": "Total",
}

PUBLIC_COMPONENT_SOURCES = frozenset(
    {
        "none",
        "memory_catalog_dict",
        "memory_catalog_missing",
        "memory_catalog_list",
        "stripped_memory",
        "rehydrated_component_list",
        "rehydrated_component_dict",
        "cell_component_list",
        "cell_component_dict",
        "memory_component_list",
        "memory_component_dict",
    }
)

PUBLIC_AVAILABILITY = frozenset(
    {
        "preserved",
        "absent",
        "unavailable",
        "partial",
        "not_applicable",
        "aggregated_without_detail",
        "unsupported_decimal",
    }
)

PUBLIC_AMOUNT_STATES = frozenset(
    {"present", "zero", "absent", "partial", "unavailable", "unsupported", "withheld", "not_applicable"}
)

PUBLIC_COMPLETENESS = frozenset({"complete", "partial", "not_calculated", "mixed"})

PUBLIC_UNIVERSES = frozenset(
    {
        "all_operational_rows",
        "fully_comparable_rows",
        "selected_tables_fully_comparable",
        "filtered_rows",
        "rows_with_partial_cells",
        "rows_with_not_calculated_cells",
        "rows_with_numeric_weight",
        "all_operational_rows_by_preserved_uf",
        "rows_matching_preserved_uf_filters",
        "complete_cells_of_this_table",
        "complete_cells_of_each_table",
        "preserved_group",
        "all_rows_of_this_table",
    }
)

PUBLIC_BUCKET_CODES = frozenset({"gris_ad_valorem_bucket", "dispatch"})

PUBLIC_ALIGNMENT_REASONS = frozenset(
    {
        "official_analytics_unavailable",
        "unusable_uf_kept_separate",
        "official_uf_missing",
        "savings_differ",
        "absent_count_differs",
        "comparable_savings_match_official_geography",
    }
)

PUBLIC_INDICATOR_SOURCES = frozenset({"official_analytics", "selected_tables_fully_comparable"})

PUBLIC_CLAIM_CODES = frozenset(
    {
        "bounded_best_freight",
        "component_value",
        "component_provenance",
        "potential_savings",
        "official_diagnostic",
        "aggregate_total",
        "coverage",
        "page_ranking",
        "population_count",
        "partial_freight",
        "weight_band_population",
        "geography_group",
        "undifferentiated_bucket",
    }
)

PUBLIC_LOSS_CODES = frozenset(
    {
        "evidence_omitted",
        "context_omitted",
        "page_incomplete",
        "monetary_withheld",
        "carrier_identity_withheld",
        "universe_withheld",
        "alignment_withheld",
        "component_conflict",
        "schema_rejected",
    }
)

PUBLIC_ERROR_CODES = frozenset(
    {
        "population_limit_exceeded",
        "processing_limit_exceeded",
        "official_analytics_unavailable",
        "invalid_table_reference",
        "execution_failed",
        "context_limit_exceeded",
        "invalid_evidence_binding",
    }
)

PUBLIC_GROUP_IDS = frozenset(
    {"uf_absent", "uf_unusable", "weight_absent", "weight_invalid", "weight_unsupported_decimal"}
)

PUBLIC_MAP_STATUSES = frozenset({"no_comparable_base", "tie", "winner", "withheld"})

PUBLIC_RECORD_ROLES = frozenset({"summary", "detail", "special_group", "official_geography"})

PUBLIC_SUBJECT_KINDS = frozenset({"query", "row", "group"})

DIAGNOSTIC_CONSTRAINTS = {
    "missing_coverage_mapping": {
        "preserved_meaning": "destination_without_sufficient_coverage_classification",
        "must_not_assert": ["route_not_served"],
    },
    "ambiguous_coverage_mapping": {
        "preserved_meaning": "ambiguous_classification",
        "must_not_assert": ["route_not_served"],
    },
    "missing_freight_rule": {
        "preserved_meaning": "no_applicable_tariff_rule",
        "must_not_assert": [],
    },
    "invalid_weight": {
        "preserved_meaning": "invalid_weight",
        "must_not_assert": [],
    },
    "invalid_invoice_value": {
        "preserved_meaning": "invalid_invoice_value",
        "must_not_assert": [],
    },
    "unsupported_pricing_model": {
        "preserved_meaning": "unsupported_pricing_model_or_incompatible_data",
        "must_not_assert": ["price_is_zero"],
    },
    "not_calculated": {
        "preserved_meaning": "calculation_not_performed",
        "must_not_assert": ["cause_known"],
    },
}

BODY_FIELDS: dict[str, frozenset[str]] = {
    QUERY_OFFICIAL_SUMMARY: frozenset(
        {
            "population",
            "completeness",
            "coverage",
            "official_values",
            "official_differences",
            "universes",
        }
    ),
    QUERY_FREIGHT_COMPONENTS: frozenset({"rows", "availability_counts"}),
    QUERY_CARRIER_COMPARISON: frozenset(
        {
            "universe",
            "carriers",
            "differences",
            "coverage",
            "exclusions",
            "superiority_limit",
            "indicator_source",
        }
    ),
    QUERY_TOP_DIFFERENCES: frozenset(
        {"ordered_differences", "gap_total", "sort", "difference_definition"}
    ),
    QUERY_BY_UF: frozenset(
        {
            "groups",
            "absent",
            "unusable",
            "exclusions",
            "official_analytics_geography",
            "alignment",
        }
    ),
    QUERY_BY_WEIGHT: frozenset({"bands", "absent", "invalid", "band_rule", "exclusions"}),
    QUERY_INCOMPLETE_FREIGHTS: frozenset({"rows", "excluded_complete_with_warnings"}),
    QUERY_NON_CALCULATION_DIAGNOSTICS: frozenset(
        {"by_code", "rows", "constraints", "preserved_accessorial_codes"}
    ),
}

FILTER_FIELDS: dict[str, frozenset[str]] = {
    QUERY_OFFICIAL_SUMMARY: frozenset(),
    QUERY_FREIGHT_COMPONENTS: frozenset({"table_ids", "row_indexes", "component_codes"}),
    QUERY_CARRIER_COMPARISON: frozenset({"table_ids"}),
    QUERY_TOP_DIFFERENCES: frozenset({"table_ids", "minimum_gap"}),
    QUERY_BY_UF: frozenset({"ufs", "include_absent", "include_unusable"}),
    QUERY_BY_WEIGHT: frozenset({"bands_version"}),
    QUERY_INCOMPLETE_FREIGHTS: frozenset({"table_ids", "row_indexes"}),
    QUERY_NON_CALCULATION_DIAGNOSTICS: frozenset({"table_ids", "codes"}),
}

PLAN_FIELDS = frozenset({"catalog_version", "queries"})
QUERY_FIELDS = frozenset({"query_id", "type", "filters", "fields", "page", "sort"})
PAGE_FIELDS = frozenset({"offset", "limit"})

FORBIDDEN_PLAN_KEYS = frozenset(
    {
        "conta_id",
        "usuario_id",
        "franquia_id",
        "franquia_operacional_id",
        "user_id",
        "account_id",
        "tenant_id",
        "connection",
        "dsn",
        "database",
        "storage_key",
        "storage_path",
        "path",
        "file",
        "filename",
        "sql",
        "statement",
        "query_sql",
        "code",
        "source_code",
        "exec",
        "eval",
        "function",
        "module",
        "import",
        "model",
        "gemini",
        "prompt",
        "operation_id",
        "comparison_id",
        "result_checksum",
        "cursor",
        "signed_cursor",
    }
)

_FORBIDDEN_VALUE_FRAGMENTS = (
    "select ",
    "insert ",
    "update ",
    "delete ",
    "drop ",
    "alter ",
    "__import__",
    "eval(",
    "exec(",
    "open(",
    "../",
    "..\\",
    "://",
)

_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
_TOKEN_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")
_QUERY_ID_MAX = 64
_TABLE_ID_MAX = 64
_MAX_FILTER_ITEMS = 500
_MAX_DEPTH = 8
_MAX_MONEY_TEXT = 64
_MAX_MONEY_DIGITS = 18
_MAX_MONEY_EXPONENT = 12
_MIN_MONEY_EXPONENT = -8


class QueryCatalogError(Exception):
    """Falha de validação do plano. A mensagem pública é só o código."""

    def __init__(self, code: str, *, field: str | None = None):
        self.code = code if code and all(char in _TOKEN_CHARS for char in code) else "invalid_query"
        self.field = field
        super().__init__(self.code)

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True)
class QueryExecutionLimits:
    """Tetos configuráveis. Nenhum teto acima do máximo deste lote vira sucesso."""

    max_queries: int = MAX_QUERIES_PER_PLAN
    max_plan_bytes: int = MAX_PLAN_BYTES
    max_page_size: int = MAX_PAGE_SIZE
    max_evidence_refs: int = MAX_EVIDENCE_REFS
    max_context_chars: int = MAX_CONTEXT_CHARS
    max_population: int = DEFAULT_MAX_POPULATION
    max_processing_seconds: float = DEFAULT_MAX_PROCESSING_SECONDS

    def __post_init__(self) -> None:
        _bounded_int(self.max_queries, field="max_queries", low=1, high=MAX_QUERIES_PER_PLAN)
        _bounded_int(self.max_plan_bytes, field="max_plan_bytes", low=1, high=MAX_PLAN_BYTES)
        _bounded_int(self.max_page_size, field="max_page_size", low=1, high=MAX_PAGE_SIZE)
        _bounded_int(
            self.max_evidence_refs,
            field="max_evidence_refs",
            low=1,
            high=MAX_EVIDENCE_REFS,
        )
        _bounded_int(
            self.max_context_chars,
            field="max_context_chars",
            low=1,
            high=MAX_CONTEXT_CHARS,
        )
        _bounded_int(self.max_population, field="max_population", low=1, high=MAX_POPULATION)
        if (
            type(self.max_processing_seconds) not in {int, float}
            or isinstance(self.max_processing_seconds, bool)
            or not 0 < float(self.max_processing_seconds) <= MAX_PROCESSING_SECONDS
        ):
            raise QueryCatalogError("invalid_limit", field="max_processing_seconds")


@dataclass(frozen=True)
class ValidatedQuery:
    query_id: str
    query_type: str
    filters: dict[str, Any]
    fields: tuple[str, ...] | None
    page_offset: int | None
    page_limit: int | None
    sort: str | None


@dataclass(frozen=True)
class ValidatedQueryPlan:
    catalog_version: str
    queries: tuple[ValidatedQuery, ...]


def _typed_canonical_default(value: Any) -> str:
    """Converte só os tipos que o armazenamento oficial já reduz com ``str``.

    O checksum oficial usa ``default=str``. Aqui o mesmo texto vale apenas para
    Decimal, data e UUID. Qualquer outro objeto continua inválido.
    """

    if type(value) is Decimal or type(value) in {date, datetime, time, UUID}:
        return str(value)
    raise TypeError("unsupported_canonical_type")


def canonical_artifact_bytes(value: Any) -> bytes:
    """Bytes canônicos do artefato. Não transforma objeto arbitrário em texto."""

    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=_typed_canonical_default,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise QueryCatalogError("invalid_artifact") from exc
    return text.encode("utf-8")


def artifact_checksum(value: Any) -> str:
    return hashlib.sha256(canonical_artifact_bytes(value)).hexdigest()


def canonical_money_text(value: Any) -> str:
    """Quantia não negativa com duas casas. Zero permanece zero."""

    if value is None or type(value) is bool or type(value) is float:
        raise QueryCatalogError("invalid_filter", field="minimum_gap")
    if type(value) is int:
        if value < 0 or value.bit_length() > 60:
            raise QueryCatalogError("invalid_filter", field="minimum_gap")
    elif type(value) is str:
        if not value or len(value) > _MAX_MONEY_TEXT:
            raise QueryCatalogError("invalid_filter", field="minimum_gap")
    elif type(value) is not Decimal:
        raise QueryCatalogError("invalid_filter", field="minimum_gap")
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError, OverflowError):
        raise QueryCatalogError("invalid_filter", field="minimum_gap") from None
    if not amount.is_finite() or amount < 0:
        raise QueryCatalogError("invalid_filter", field="minimum_gap")
    exponent = amount.as_tuple().exponent
    if (
        type(exponent) is not int
        or exponent > _MAX_MONEY_EXPONENT
        or exponent < _MIN_MONEY_EXPONENT
        or len(amount.as_tuple().digits) > _MAX_MONEY_DIGITS
    ):
        raise QueryCatalogError("invalid_filter", field="minimum_gap")
    try:
        quantized = amount.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, OverflowError):
        raise QueryCatalogError("invalid_filter", field="minimum_gap") from None
    if not quantized.is_finite():
        raise QueryCatalogError("invalid_filter", field="minimum_gap")
    return f"{quantized:.2f}"


def validate_query_plan(
    plan: Any,
    *,
    limits: QueryExecutionLimits | None = None,
) -> ValidatedQueryPlan:
    active_limits = limits or QueryExecutionLimits()
    if not isinstance(plan, Mapping):
        raise QueryCatalogError("invalid_query")
    _reject_forbidden(plan, depth=0)
    try:
        raw_size = len(canonical_artifact_bytes(plan))
    except QueryCatalogError as exc:
        raise QueryCatalogError("invalid_query") from exc
    if raw_size > active_limits.max_plan_bytes:
        raise QueryCatalogError("plan_too_large")
    unknown = _mapping_keys(plan) - PLAN_FIELDS
    if unknown:
        raise QueryCatalogError("unknown_field", field="queries")
    if plan.get("catalog_version") != CATALOG_VERSION:
        raise QueryCatalogError("invalid_catalog_version", field="catalog_version")
    queries = plan.get("queries")
    if not isinstance(queries, list) or not queries:
        raise QueryCatalogError("invalid_query", field="queries")
    if len(queries) > active_limits.max_queries:
        raise QueryCatalogError("too_many_queries", field="queries")
    validated: list[ValidatedQuery] = []
    seen_ids: set[str] = set()
    for item in queries:
        validated_query = _validate_query(item, limits=active_limits)
        if validated_query.query_id in seen_ids:
            raise QueryCatalogError("invalid_query", field="query_id")
        seen_ids.add(validated_query.query_id)
        validated.append(validated_query)
    return ValidatedQueryPlan(catalog_version=CATALOG_VERSION, queries=tuple(validated))


def _validate_query(item: Any, *, limits: QueryExecutionLimits) -> ValidatedQuery:
    if not isinstance(item, Mapping):
        raise QueryCatalogError("invalid_query")
    _reject_forbidden(item, depth=0)
    unknown = _mapping_keys(item) - QUERY_FIELDS
    if unknown:
        raise QueryCatalogError("unknown_field")
    query_type = item.get("type")
    if not isinstance(query_type, str) or query_type not in QUERY_TYPES:
        raise QueryCatalogError("invalid_query_type", field="type")
    query_id = _bounded_token(item.get("query_id"), field="query_id", max_len=_QUERY_ID_MAX)
    filters = item.get("filters", {})
    if filters is None:
        filters = {}
    if not isinstance(filters, Mapping):
        raise QueryCatalogError("invalid_filter", field="filters")
    _reject_forbidden(filters, depth=0)
    filter_unknown = _mapping_keys(filters) - FILTER_FIELDS[query_type]
    if filter_unknown:
        raise QueryCatalogError("unknown_field", field="filters")
    parsed_filters = _parse_filters(query_type, filters)
    fields = _parse_fields(query_type, item.get("fields", None))
    page_offset, page_limit = _parse_page(query_type, item.get("page", None), limits=limits)
    sort = _parse_sort(query_type, item.get("sort", None))
    return ValidatedQuery(
        query_id=query_id,
        query_type=query_type,
        filters=parsed_filters,
        fields=fields,
        page_offset=page_offset,
        page_limit=page_limit,
        sort=sort,
    )


def _parse_filters(query_type: str, filters: Mapping[str, Any]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    if "table_ids" in filters:
        parsed["table_ids"] = _parse_table_ids(filters.get("table_ids"))
    if "row_indexes" in filters:
        parsed["row_indexes"] = _parse_row_indexes(filters.get("row_indexes"))
    if "component_codes" in filters:
        parsed["component_codes"] = _parse_closed_list(
            filters.get("component_codes"),
            allowed=COMPONENT_CODES,
            field="component_codes",
        )
    if "codes" in filters:
        parsed["codes"] = _parse_diagnostic_codes(filters.get("codes"))
    if "ufs" in filters:
        parsed["ufs"] = _parse_ufs(filters.get("ufs"))
    if "include_absent" in filters:
        parsed["include_absent"] = _parse_bool(filters.get("include_absent"), field="include_absent")
    if "include_unusable" in filters:
        parsed["include_unusable"] = _parse_bool(
            filters.get("include_unusable"),
            field="include_unusable",
        )
    if "minimum_gap" in filters:
        parsed["minimum_gap"] = canonical_money_text(filters.get("minimum_gap"))
    if "bands_version" in filters:
        if filters.get("bands_version") != WEIGHT_BANDS_VERSION:
            raise QueryCatalogError("invalid_weight_band", field="bands_version")
        parsed["bands_version"] = WEIGHT_BANDS_VERSION
    elif query_type == QUERY_BY_WEIGHT:
        parsed["bands_version"] = WEIGHT_BANDS_VERSION
    if query_type == QUERY_CARRIER_COMPARISON and "table_ids" in parsed:
        if len(parsed["table_ids"]) < 2:
            raise QueryCatalogError("invalid_filter", field="table_ids")
    if query_type == QUERY_TOP_DIFFERENCES and "table_ids" in parsed:
        if len(parsed["table_ids"]) < 2:
            raise QueryCatalogError("invalid_filter", field="table_ids")
    return parsed


def _parse_fields(query_type: str, fields: Any) -> tuple[str, ...] | None:
    if fields is None:
        return None
    if not isinstance(fields, list) or not fields:
        raise QueryCatalogError("invalid_field_selection", field="fields")
    allowed = BODY_FIELDS[query_type]
    selected: list[str] = []
    for item in fields:
        if not isinstance(item, str) or item not in allowed:
            raise QueryCatalogError("invalid_field_selection", field="fields")
        if item in selected:
            raise QueryCatalogError("invalid_field_selection", field="fields")
        selected.append(item)
    return tuple(selected)


def _parse_page(
    query_type: str,
    page: Any,
    *,
    limits: QueryExecutionLimits,
) -> tuple[int | None, int | None]:
    if query_type not in PAGINATED_QUERY_TYPES:
        if page is not None:
            raise QueryCatalogError("invalid_page", field="page")
        return None, None
    if page is None:
        return 0, limits.max_page_size
    if not isinstance(page, Mapping):
        raise QueryCatalogError("invalid_page", field="page")
    _reject_forbidden(page, depth=0)
    if _mapping_keys(page) - PAGE_FIELDS:
        raise QueryCatalogError("unknown_field", field="page")
    offset = page.get("offset", 0)
    limit = page.get("limit", limits.max_page_size)
    if type(offset) is not int or offset < 0:
        raise QueryCatalogError("invalid_page", field="offset")
    if type(limit) is not int or not 1 <= limit <= limits.max_page_size:
        raise QueryCatalogError("invalid_page", field="limit")
    return offset, limit


def _parse_sort(query_type: str, sort: Any) -> str | None:
    allowed = SORTS_BY_TYPE[query_type]
    if sort is None:
        return DEFAULT_SORT_BY_TYPE.get(query_type)
    if not isinstance(sort, str) or sort not in allowed:
        raise QueryCatalogError("invalid_sort", field="sort")
    return sort


def _parse_table_ids(value: Any) -> tuple[str, ...]:
    items = _parse_string_list(value, field="table_ids")
    parsed: list[str] = []
    for item in items:
        token = _bounded_token(item, field="table_ids", max_len=_TABLE_ID_MAX)
        if token in parsed:
            raise QueryCatalogError("invalid_table_reference", field="table_ids")
        parsed.append(token)
    if not parsed:
        raise QueryCatalogError("invalid_table_reference", field="table_ids")
    return tuple(parsed)


def _parse_row_indexes(value: Any) -> tuple[int, ...]:
    if not isinstance(value, list) or not value or len(value) > _MAX_FILTER_ITEMS:
        raise QueryCatalogError("invalid_row_index", field="row_indexes")
    parsed: list[int] = []
    for item in value:
        if type(item) is not int or item < 1:
            raise QueryCatalogError("invalid_row_index", field="row_indexes")
        if item in parsed:
            raise QueryCatalogError("invalid_row_index", field="row_indexes")
        parsed.append(item)
    return tuple(parsed)


def _parse_ufs(value: Any) -> tuple[str, ...]:
    items = _parse_string_list(value, field="ufs")
    parsed: list[str] = []
    for item in items:
        code = item.strip().upper()
        if code not in OFFICIAL_UF_CODES:
            raise QueryCatalogError("invalid_uf", field="ufs")
        if code in parsed:
            raise QueryCatalogError("invalid_uf", field="ufs")
        parsed.append(code)
    return tuple(parsed)


def _parse_diagnostic_codes(value: Any) -> tuple[str, ...]:
    items = _parse_string_list(value, field="codes")
    parsed: list[str] = []
    for item in items:
        if not _is_token(item) or item in parsed:
            raise QueryCatalogError("invalid_filter", field="codes")
        parsed.append(item)
    return tuple(parsed)


def _parse_closed_list(value: Any, *, allowed: frozenset[str], field: str) -> tuple[str, ...]:
    items = _parse_string_list(value, field=field)
    parsed: list[str] = []
    for item in items:
        if item not in allowed or item in parsed:
            raise QueryCatalogError("invalid_filter", field=field)
        parsed.append(item)
    return tuple(parsed)


def _parse_string_list(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > _MAX_FILTER_ITEMS:
        raise QueryCatalogError("invalid_filter", field=field)
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise QueryCatalogError("invalid_filter", field=field)
        _reject_forbidden_text(item)
        items.append(item)
    return items


def _parse_bool(value: Any, *, field: str) -> bool:
    if type(value) is not bool:
        raise QueryCatalogError("invalid_filter", field=field)
    return value


def _bounded_token(value: Any, *, field: str, max_len: int) -> str:
    if not isinstance(value, str):
        raise QueryCatalogError("invalid_query", field=field)
    _reject_forbidden_text(value)
    if not value or len(value) > max_len or any(char not in _ID_CHARS for char in value):
        raise QueryCatalogError("invalid_query", field=field)
    return value


def _is_token(value: str) -> bool:
    if not value or len(value) > 64 or value[0].isdigit():
        return False
    return all(char in _TOKEN_CHARS for char in value)


def _mapping_keys(value: Mapping[str, Any]) -> set[str]:
    """Chaves do plano. Valor não hashable vira erro do catálogo, não TypeError."""

    try:
        keys = set(value)
    except TypeError as exc:
        raise QueryCatalogError("invalid_query") from exc
    if not all(isinstance(key, str) for key in keys):
        raise QueryCatalogError("invalid_query")
    return keys


def _bounded_int(value: Any, *, field: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise QueryCatalogError("invalid_limit", field=field)
    return value


def _reject_forbidden(value: Any, *, depth: int) -> None:
    """Aceita só o plano JSON fechado. Set, bytes e objetos não viram texto."""

    if depth > _MAX_DEPTH:
        raise QueryCatalogError("invalid_query")
    if isinstance(value, (bytes, bytearray, memoryview, set, frozenset)):
        raise QueryCatalogError("invalid_query")
    if type(value) is str:
        _reject_forbidden_text(value)
        return
    if value is None or type(value) in {bool, int, float, Decimal}:
        return
    if type(value) is dict:
        for key, item in value.items():
            if not isinstance(key, str):
                raise QueryCatalogError("invalid_query")
            if key in FORBIDDEN_PLAN_KEYS:
                raise QueryCatalogError("forbidden_expression", field=key)
            _reject_forbidden(item, depth=depth + 1)
        return
    if type(value) is list:
        for item in value:
            _reject_forbidden(item, depth=depth + 1)
        return
    raise QueryCatalogError("invalid_query")


def _reject_forbidden_text(value: str) -> None:
    folded = value.casefold()
    for fragment in _FORBIDDEN_VALUE_FRAGMENTS:
        if fragment in folded:
            raise QueryCatalogError("forbidden_expression")
    if ":\\" in value or value.startswith("/") or "cleiton_doc_tmp" in folded:
        raise QueryCatalogError("forbidden_expression")
