"""Publicação analítica do chat do Agente Compara.

SCRUM-238 / SCRUM-245. A fronteira pública é este módulo. O executor calcula
em memória privada e só devolve o que o publicador sela.

O contrato é ``comparison_chat_publication_v1``. Ele não altera
``CONTRACT_VERSION`` nem os estados do Lote 1. Cada evidência continua
vinculada à operação e ao snapshot por ``EvidenceReference``.

Identidade pública de linha e transportadora: ``row_index`` e ``carrier_slot``,
ordens internas do snapshot autorizado. ``table_id``, número de documento,
nome livre e caminho não entram no schema. Não há hash de identificador
privado. Código fora da allowlist oficial fica só como contagem.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid5

from app.agente_compara_chat_contracts import (
    CONTRACT_VERSION,
    AuthorizedComparisonScope,
    ChatContractError,
    EvidenceReference,
    assert_artifact_binding,
)
from app.agente_compara_chat_query_catalog import (
    CATALOG_VERSION,
    COMPONENT_CODES,
    DIAGNOSTIC_CONSTRAINTS,
    OFFICIAL_DIAGNOSTIC_CODES,
    OFFICIAL_UF_CODES,
    PUBLIC_ALIGNMENT_REASONS,
    PUBLIC_AMOUNT_STATES,
    PUBLIC_AVAILABILITY,
    PUBLIC_BUCKET_CODES,
    PUBLIC_CLAIM_CODES,
    PUBLIC_COMPLETENESS,
    PUBLIC_COMPONENT_LABELS,
    PUBLIC_COMPONENT_SOURCES,
    PUBLIC_ERROR_CODES,
    PUBLIC_GROUP_IDS,
    PUBLIC_INDICATOR_SOURCES,
    PUBLIC_LOSS_CODES,
    PUBLIC_MAP_STATUSES,
    PUBLIC_RECORD_ROLES,
    PUBLIC_SUBJECT_KINDS,
    PUBLIC_UNIVERSES,
    PUBLICATION_VERSION,
    WEIGHT_BANDS_V1,
    WEIGHT_BANDS_VERSION,
    QueryCatalogError,
    QueryExecutionLimits,
    artifact_checksum,
)

EVIDENCE_NAMESPACE = UUID("6f0c2a1e-7b14-4d3a-9e5f-1c8a2b7d4e60")

_TIE_RULE = "unique_lowest_amount"
_GAP_DEFINITION = "second_lowest_minus_lowest_on_fully_comparable_rows"
_LAST_BAND_RULE = "lower_inclusive_without_upper_bound"
_SAVINGS_UNIVERSE = "fully_comparable_rows"
_POPULATION_UNIVERSE = "all_operational_rows"
_CARRIER_TOTAL_UNIVERSE = "complete_cells_of_each_table"
_REFERENCE_TOKENS = frozenset(
    {"examined_rows", "numeric_weight_rows", "examined_rows_without_numeric_weight"}
)
_DETAIL_LEVELS = frozenset({"item", "aggregate", "none"})
_AMOUNT_SUPPORTS = frozenset({"official_numeric", "unsupported_decimal", "absent"})
_NUMERIC_SUPPORTS = frozenset({"official_numeric", "unsupported_decimal"})
_ACCESSORIAL_COUNT_CODES = frozenset({"gris", "ad_valorem", "accessorial"})
_OFFICIAL_WEIGHT_BANDS = {
    f"{lower}:{upper or 'inf'}": (lower, upper) for lower, upper in WEIGHT_BANDS_V1
}
_BAND_IDS = frozenset(_OFFICIAL_WEIGHT_BANDS)
_SAFE_GROUP_IDS = OFFICIAL_UF_CODES | PUBLIC_GROUP_IDS | _BAND_IDS
_SORTS = frozenset(
    {
        "row_index_asc",
        "absolute_gap_desc_row_index_asc",
        "uf_asc",
        "band_asc",
    }
)
_PROVENANCE_SOURCES = PUBLIC_COMPONENT_SOURCES - {"none"}
_VALUE_AVAILABILITY = frozenset({"preserved", "partial"})
_ALIGNED_GEOGRAPHY = "comparable_savings_match_official_geography"
_RANK_SORT = "absolute_gap_desc_row_index_asc"

_LOSS_BLOCKS = {
    "evidence_omitted": tuple(sorted(PUBLIC_CLAIM_CODES)),
    "context_omitted": tuple(sorted(PUBLIC_CLAIM_CODES)),
    "page_incomplete": ("page_ranking", "bounded_best_freight"),
    "monetary_withheld": (
        "potential_savings",
        "aggregate_total",
        "component_value",
        "component_provenance",
        "bounded_best_freight",
        "partial_freight",
    ),
    "carrier_identity_withheld": ("bounded_best_freight", "page_ranking", "component_value"),
    "universe_withheld": ("bounded_best_freight", "potential_savings", "population_count", "coverage"),
    "alignment_withheld": ("geography_group",),
    "component_conflict": ("component_value", "component_provenance"),
    "schema_rejected": tuple(sorted(PUBLIC_CLAIM_CODES)),
}

_ENVELOPE_KEYS = frozenset(
    {
        "publication_version",
        "contract_version",
        "status",
        "analysis_complete",
        "publication_complete",
        "evidence_sufficient",
        "operation_id",
        "comparison_id",
        "expected_result_version",
        "catalog_version",
        "population",
        "queries",
        "evidence_produced_count",
        "evidence_delivered_count",
        "evidence_omitted_count",
        "losses",
        "context_text",
        "context_chars",
        "error",
    }
)
_QUERY_KEYS = frozenset(
    {
        "query_id",
        "query_type",
        "status",
        "error",
        "selection",
        "population",
        "page",
        "publication_complete",
        "evidence_sufficient",
        "allowed_claims",
        "losses",
        "records",
    }
)
_RECORD_KEYS = frozenset(
    {
        "evidence_id",
        "evidence_digest",
        "publication_version",
        "schema_id",
        "contract_version",
        "operation_id",
        "comparison_id",
        "expected_result_version",
        "memory_checksum",
        "memory_schema_version",
        "catalog_version",
        "query_id",
        "query_type",
        "record_role",
        "ordinal",
        "subject",
        "population",
        "page",
        "facts",
    }
)
_POPULATION_KEYS = frozenset(
    {
        "examined_rows",
        "eligible_rows",
        "excluded_rows",
        "universe",
        "total_cells",
        "comparable_rows",
        "numeric_weight_rows",
        "complete_freight_rows",
        "complete_freight_count",
        "excluded_invalid_or_absent_weight",
        "excluded_unsupported_decimal_weight",
        "excluded_not_comparable_rows",
        "excluded_weight_reference",
        "excluded_comparability_reference",
    }
)
_PAGE_KEYS = frozenset({"offset", "limit", "total", "returned", "sort"})
_SELECTION_KEYS = frozenset(
    {
        "carrier_slots",
        "row_indexes",
        "component_codes",
        "ufs",
        "diagnostic_codes",
        "minimum_gap_text",
        "minimum_gap_state",
        "include_absent",
        "include_unusable",
        "bands_version",
    }
)
_SUBJECT_KEYS = frozenset({"kind", "row_index", "carrier_slot", "group_id"})
_LOSS_KEYS = frozenset({"code", "count", "blocks"})
_ERROR_KEYS = frozenset({"code"})
_PLAN_POPULATION_KEYS = frozenset({"examined_rows", "query_count"})
_CARRIER_KEYS = frozenset(
    {
        "carrier_slot",
        "population_rows",
        "complete_rows",
        "complete_with_warnings_rows",
        "partial_rows",
        "not_calculated_rows",
        "coverage_percentage",
        "official_freight_text",
        "official_freight_state",
        "wins",
        "ties",
        "comparable_row_count",
        "comparable_freight_text",
        "comparable_freight_state",
        "win_percentage",
    }
)
_COMPONENT_KEYS = frozenset(
    {
        "code",
        "label",
        "amount_text",
        "amount_state",
        "availability",
        "detail_level",
        "source",
        "numeric_support",
    }
)
_BUCKET_KEYS = frozenset({"code", "amount_text", "amount_state", "availability"})
_COUNT_KEYS = frozenset({"code", "availability", "count"})
_CANONICAL_COUNT_KEYS = frozenset({"code", "applied", "count"})
_FREIGHT_KEYS = frozenset({"carrier_slot", "amount_text", "amount_state"})
_GEO_CARRIER_KEYS = frozenset(
    {
        "carrier_slot",
        "complete_rows",
        "coverage_percentage",
        "complete_freight_text",
        "complete_freight_state",
        "comparable_freight_text",
        "comparable_freight_state",
        "wins",
    }
)
_SUMMARY_FACTS = {
    "official_summary": frozenset(
        {
            "total_rows",
            "total_cells",
            "document_count",
            "fully_comparable_rows",
            "partially_comparable_rows",
            "inconclusive_rows",
            "calculated_cells",
            "error_cells",
            "calculation_coverage_percentage",
            "fully_comparable_percentage",
            "total_weight_text",
            "total_weight_state",
            "total_invoice_text",
            "total_invoice_state",
            "carriers",
            "potential_savings_text",
            "potential_savings_state",
            "decisive_row_count",
            "tie_count",
            "comparable_row_count",
            "savings_universe",
            "tie_rule",
            "lead_carrier_slot",
            "lead_wins",
            "differences_universe",
            "population_universe",
            "carrier_total_universe",
        }
    ),
    "freight_components": frozenset(
        {"matched_rows", "returned_rows", "missing_row_indexes", "availability_counts"}
    ),
    "carrier_comparison": frozenset(
        {
            "universe",
            "indicator_source",
            "tie_rule",
            "global_superiority",
            "comparable_row_count",
            "decisive_row_count",
            "tie_count",
            "potential_savings_text",
            "potential_savings_state",
            "partially_comparable_rows",
            "inconclusive_rows",
            "carriers",
        }
    ),
    "top_differences": frozenset(
        {"gap_total_text", "gap_total_state", "ranked_rows", "difference_definition"}
    ),
    "by_uf": frozenset(
        {
            "examined_rows",
            "eligible_rows",
            "excluded_rows",
            "comparable_rows",
            "filtered_uf_rows",
            "absent_rows",
            "unusable_rows",
            "include_absent",
            "include_unusable",
            "selected_ufs",
            "aligned",
            "alignment_reason",
            "group_counts",
        }
    ),
    "by_weight": frozenset(
        {
            "examined_rows",
            "numeric_weight_rows",
            "complete_freight_rows",
            "complete_freight_count",
            "comparable_rows",
            "absent_rows",
            "invalid_rows",
            "unsupported_decimal_rows",
            "excluded_invalid_or_absent_weight",
            "excluded_weight_reference",
            "excluded_not_comparable_rows",
            "excluded_comparability_reference",
            "bands_version",
            "lower_inclusive",
            "upper_exclusive",
            "last_band_rule",
            "band_counts",
        }
    ),
    "incomplete_freights": frozenset({"partial_rows", "excluded_complete_with_warnings"}),
    "non_calculation_diagnostics": frozenset({"by_code", "unmapped_code_occurrences"}),
}
_DETAIL_FACTS = {
    "freight_components": frozenset(
        {
            "row_index",
            "carrier_slot",
            "completeness",
            "final_amount_text",
            "final_amount_state",
            "partial_amount_text",
            "partial_amount_state",
            "amount_support",
            "origin",
            "detail_level",
            "components",
            "buckets",
            "unresolved_component_refs",
            "unmapped_accessorial_count",
            "canonical_accessorial_counts",
        }
    ),
    "top_differences": frozenset(
        {"row_index", "absolute_gap_text", "absolute_gap_state", "tie", "freights"}
    ),
    "incomplete_freights": frozenset({"row_index", "cells"}),
    "non_calculation_diagnostics": frozenset({"row_index", "cells"}),
}
_GROUP_FACTS = frozenset(
    {
        "group_id",
        "group_state",
        "row_count",
        "comparable_row_count",
        "excluded_row_count",
        "scanned_rows",
        "potential_savings_text",
        "potential_savings_state",
        "monetary_values_published",
        "included",
        "lower_text",
        "lower_state",
        "upper_text",
        "upper_state",
        "lower_inclusive",
        "upper_exclusive",
        "last_band",
        "carriers",
    }
)
_OFFICIAL_UF_KEYS = frozenset(
    {
        "group_state",
        "uf",
        "row_count",
        "comparable_row_count",
        "potential_savings_text",
        "potential_savings_state",
        "tie_count",
        "is_tie",
        "has_comparable_base",
        "low_sample",
        "map_status",
        "winner_slot",
        "carriers",
    }
)
_OFFICIAL_GEO_FACTS = frozenset({"low_sample_threshold", "groups", "withheld_label_count"})
_GROUP_COUNT_KEYS = frozenset({"group_id", "row_count"})
_DIAGNOSTIC_CODE_KEYS = frozenset(
    {"code", "cell_count", "row_count", "preserved_meaning", "must_not_assert"}
)
_DIAGNOSTIC_CELL_KEYS = frozenset(
    {
        "carrier_slot",
        "code_state",
        "official_code",
        "preserved_meaning",
        "must_not_assert",
        "extra_official_codes",
        "unmapped_occurrence_count",
        "unresolved_diagnostic_refs",
    }
)
_PARTIAL_CELL_KEYS = frozenset(
    {
        "carrier_slot",
        "completeness",
        "partial_amount_text",
        "partial_amount_state",
        "final_amount_state",
        "official_reason_codes",
        "unmapped_reason_count",
    }
)
_FORBIDDEN_PUBLIC_KEYS = frozenset(
    {
        "body",
        "evidence",
        "evidence_package",
        "table_id",
        "document_number",
        "carrier_name",
        "display_name",
        "message",
        "path",
        "filename",
        "secret",
        "password",
        "credential",
        "prompt",
        "traceback",
        "raw",
        "calculation_memory",
        "signed_cursor",
        "cursor_contract",
    }
)


class _SchemaGap(Exception):
    pass


class _LossBook:
    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def add(self, code: str, count: int = 1) -> None:
        if code not in PUBLIC_LOSS_CODES or count <= 0:
            return
        self._counts[code] = self._counts.get(code, 0) + count

    def blocked_claims(self) -> set[str]:
        blocked: set[str] = set()
        for code in self._counts:
            blocked.update(_LOSS_BLOCKS.get(code, ()))
        return blocked

    def export(self) -> list[dict[str, Any]]:
        exported = []
        for code in sorted(self._counts):
            blocks = [item for item in _LOSS_BLOCKS.get(code, ()) if item in PUBLIC_CLAIM_CODES]
            exported.append({"code": code, "count": self._counts[code], "blocks": blocks})
        return exported

    def __bool__(self) -> bool:
        return bool(self._counts)


def evidence_authorizes_operation(
    reference: EvidenceReference,
    *,
    operation_id: str,
    comparison_id: str,
    expected_result_version: str,
    scope: AuthorizedComparisonScope,
) -> bool:
    """Reutiliza o vínculo do Lote 1. Memória antiga ou referência estrangeira falha fechado.

    ``assert_artifact_binding`` confere operação, comparação, contrato, versão do
    resultado, checksum de memória e versão do schema. A versão do resultado não
    inclui a memória; por isso o helper não a substitui por uma comparação parcial.
    """

    if type(reference) is not EvidenceReference or type(scope) is not AuthorizedComparisonScope:
        return False
    if scope.comparison_id != comparison_id or scope.expected_result_version != expected_result_version:
        return False
    try:
        assert_artifact_binding(
            reference,
            operation_id=operation_id,
            scope=scope,
            contract_version=CONTRACT_VERSION,
        )
    except ChatContractError:
        return False
    return True


def evidence_digest_matches(record: Mapping[str, Any]) -> bool:
    """O digest cobre o registro público sem o próprio identificador."""

    payload = {key: value for key, value in record.items() if key not in {"evidence_id", "evidence_digest"}}
    try:
        return artifact_checksum(payload) == record.get("evidence_digest")
    except QueryCatalogError:
        return False


def publication_document_closed(document: Mapping[str, Any]) -> None:
    """Recusa chave fora do schema. Falha de programa, não dado do usuário."""

    if set(document) != _ENVELOPE_KEYS:
        raise _SchemaGap(tuple(sorted(set(document) ^ _ENVELOPE_KEYS)))
    _reject_forbidden_keys(document)
    _exact_population_plan(document["population"])
    if document["error"] is not None:
        _exact(document["error"], _ERROR_KEYS)
    for loss in document["losses"]:
        _exact(loss, _LOSS_KEYS)
    for query in document["queries"]:
        _exact(query, _QUERY_KEYS)
        _exact(query["selection"], _SELECTION_KEYS)
        if query["population"] is not None:
            _exact(query["population"], _POPULATION_KEYS)
        if query["page"] is not None:
            _exact(query["page"], _PAGE_KEYS)
        if query["error"] is not None:
            _exact(query["error"], _ERROR_KEYS)
        for loss in query["losses"]:
            _exact(loss, _LOSS_KEYS)
        for record in query["records"]:
            _check_record(record)


def publish_analytical_execution(
    *,
    operation_id: str,
    scope: AuthorizedComparisonScope,
    examined_rows: int,
    table_slots: Mapping[str, int],
    private_queries: Sequence[Mapping[str, Any]],
    queries_complete: bool,
    plan_error: str | None,
    limits: QueryExecutionLimits | None = None,
) -> dict[str, Any]:
    """Sela a execução privada. O retorno não contém o corpo analítico.

    Falha inesperada vira envelope de erro. A mensagem da exceção não entra
    no documento e o corpo analítico parcial não é publicado.
    """

    failure = None
    try:
        return _publish_prepared(
            operation_id=operation_id,
            scope=scope,
            examined_rows=examined_rows,
            table_slots=table_slots,
            private_queries=private_queries,
            queries_complete=queries_complete,
            plan_error=plan_error,
            limits=limits,
        )
    except QueryCatalogError as exc:
        failure = (exc.code, exc.field)
    except Exception:
        return publication_failure(
            operation_id=operation_id,
            scope=scope,
            examined_rows=examined_rows,
        )
    raise QueryCatalogError(failure[0], field=failure[1])


def _publish_prepared(
    *,
    operation_id: str,
    scope: AuthorizedComparisonScope,
    examined_rows: int,
    table_slots: Mapping[str, int],
    private_queries: Sequence[Mapping[str, Any]],
    queries_complete: bool,
    plan_error: str | None,
    limits: QueryExecutionLimits | None = None,
) -> dict[str, Any]:
    """Sela a execução privada. O retorno não contém o corpo analítico."""

    active_limits = limits or QueryExecutionLimits()
    safe_error = _error_code(plan_error)
    if safe_error is not None:
        queries_complete = False
    unsigned = [] if safe_error else _project_queries(private_queries, table_slots)
    produced = len(unsigned)
    planned_by_query: dict[str, int] = {}
    for record in unsigned:
        planned_by_query[record["query_id"]] = planned_by_query.get(record["query_id"], 0) + 1
    selected = list(unsigned)
    ref_omitted = 0
    while len(selected) > active_limits.max_evidence_refs:
        selected.pop()
        ref_omitted += 1
    context_omitted = 0
    while True:
        envelope = _assemble(
            operation_id=operation_id,
            scope=scope,
            examined_rows=examined_rows,
            private_queries=private_queries,
            selected=selected,
            produced=produced,
            ref_omitted=ref_omitted,
            context_omitted=context_omitted,
            queries_complete=queries_complete,
            plan_error=safe_error,
            table_slots=table_slots,
            planned_by_query=planned_by_query,
        )
        snapshot = copy.deepcopy(envelope)
        try:
            rendered = _canonical(snapshot)
        except QueryCatalogError:
            return _failure_envelope(
                operation_id=operation_id,
                scope=scope,
                examined_rows=examined_rows,
                code="execution_failed",
            )
        if len(rendered) <= active_limits.max_context_chars:
            delivered = copy.deepcopy(snapshot)
            delivered["context_text"] = rendered
            delivered["context_chars"] = len(rendered)
            try:
                publication_document_closed(delivered)
            except _SchemaGap as exc:
                raise QueryCatalogError("schema_rejected") from exc
            return delivered
        if not selected:
            return _failure_envelope(
                operation_id=operation_id,
                scope=scope,
                examined_rows=examined_rows,
                code="context_limit_exceeded",
                analysis_complete=queries_complete and safe_error is None,
                max_context_chars=active_limits.max_context_chars,
            )
        selected.pop()
        context_omitted += 1


def publication_failure(
    *,
    operation_id: str,
    scope: AuthorizedComparisonScope,
    examined_rows: int = 0,
    code: str = "execution_failed",
) -> dict[str, Any]:
    """Erro público mínimo. Não recebe exceção, caminho nem payload privado."""

    try:
        safe_rows = examined_rows if type(examined_rows) is int and examined_rows >= 0 else 0
        return _failure_envelope(
            operation_id=operation_id,
            scope=scope,
            examined_rows=safe_rows,
            code=code if code in PUBLIC_ERROR_CODES else "execution_failed",
        )
    except Exception:
        return _failure_envelope(
            operation_id=operation_id,
            scope=scope,
            examined_rows=0,
            code="execution_failed",
        )


def _failure_envelope(
    *,
    operation_id: str,
    scope: AuthorizedComparisonScope,
    examined_rows: int,
    code: str,
    analysis_complete: bool = False,
    max_context_chars: int | None = None,
) -> dict[str, Any]:
    error = {"code": _error_code(code) or "execution_failed"}
    body = {
        "publication_version": PUBLICATION_VERSION,
        "contract_version": CONTRACT_VERSION,
        "status": "error",
        "analysis_complete": analysis_complete,
        "publication_complete": False,
        "evidence_sufficient": False,
        "operation_id": operation_id,
        "comparison_id": scope.comparison_id,
        "expected_result_version": scope.expected_result_version,
        "catalog_version": CATALOG_VERSION,
        "population": {"examined_rows": examined_rows, "query_count": 0},
        "queries": [],
        "evidence_produced_count": 0,
        "evidence_delivered_count": 0,
        "evidence_omitted_count": 0,
        "losses": [{"code": "context_omitted", "count": 1, "blocks": list(_LOSS_BLOCKS["context_omitted"])}]
        if code == "context_limit_exceeded"
        else [],
        "error": error,
    }
    try:
        rendered = _canonical(body)
    except Exception:
        rendered = ""
    if max_context_chars is not None and len(rendered) > max_context_chars:
        rendered = ""
    delivered = copy.deepcopy(body)
    delivered["context_text"] = rendered
    delivered["context_chars"] = len(rendered)
    return delivered


def _project_queries(private_queries: Sequence[Mapping[str, Any]], table_slots: Mapping[str, int]):
    projected = []
    for query in private_queries:
        if query.get("status") != "ok" or not isinstance(query.get("body"), Mapping):
            continue
        projected.extend(_project_query(query, table_slots))
    summaries = [item for item in projected if item.get("record_role") == "summary"]
    rest = [item for item in projected if item.get("record_role") != "summary"]
    return summaries + rest


def _project_query(query: Mapping[str, Any], table_slots: Mapping[str, int]) -> list[dict[str, Any]]:
    query_type = query.get("query_type")
    builders = {
        "official_summary": _project_official_summary,
        "freight_components": _project_freight_components,
        "carrier_comparison": _project_carrier_comparison,
        "top_differences": _project_top_differences,
        "by_uf": _project_by_uf,
        "by_weight": _project_by_weight,
        "incomplete_freights": _project_incomplete,
        "non_calculation_diagnostics": _project_diagnostics,
    }
    builder = builders.get(query_type)
    if builder is None:
        return []
    population = _population(query.get("population"), query.get("body") if isinstance(query.get("body"), Mapping) else {})
    page = _page(query.get("page"))
    records = builder(query, table_slots, population, page)
    for record in records:
        record["query_id"] = query.get("query_id")
        record["query_type"] = query_type
        record["schema_id"] = query_type
        record["population"] = population
        record["page"] = page
    return records


def _project_official_summary(query, table_slots, population, page) -> list[dict[str, Any]]:
    del table_slots, population, page
    body = query["body"]
    losses = _LossBook()
    wanted = _wanted(query)
    values = body.get("official_values") if wanted("official_values") else None
    completeness = body.get("completeness") if wanted("completeness") else None
    coverage = body.get("coverage") if wanted("coverage") else None
    differences = body.get("official_differences") if wanted("official_differences") else None
    summary_population = body.get("population") if wanted("population") else None
    universes = body.get("universes") if wanted("universes") else None
    carriers, lead_slot, lead_wins = _summary_carriers(values, differences, losses)
    competitive = differences.get("competitive_summary") if isinstance(differences, Mapping) else None
    if not isinstance(competitive, Mapping):
        competitive = {}
    savings_text, savings_state = _money(competitive.get("total_potential_savings"))
    if savings_state == "unavailable":
        losses.add("monetary_withheld")
    weight_text, weight_state = _money(None if not isinstance(values, Mapping) else values.get("total_weight"))
    invoice_text, invoice_state = _money(
        None if not isinstance(values, Mapping) else values.get("total_invoice_value")
    )
    if weight_state == "unavailable" or invoice_state == "unavailable":
        losses.add("monetary_withheld")
    facts = _exact(
        _SUMMARY_FACTS["official_summary"],
        {
            "total_rows": _plain_int(None if not isinstance(summary_population, Mapping) else summary_population.get("total_rows")),
            "total_cells": _plain_int(None if not isinstance(summary_population, Mapping) else summary_population.get("total_cells")),
            "document_count": _plain_int(
                None if not isinstance(summary_population, Mapping) else summary_population.get("document_count")
            ),
            "fully_comparable_rows": _plain_int(None if not isinstance(completeness, Mapping) else completeness.get("fully_comparable_rows")),
            "partially_comparable_rows": _plain_int(
                None if not isinstance(completeness, Mapping) else completeness.get("partially_comparable_rows")
            ),
            "inconclusive_rows": _plain_int(None if not isinstance(completeness, Mapping) else completeness.get("inconclusive_rows")),
            "calculated_cells": _plain_int(None if not isinstance(completeness, Mapping) else completeness.get("calculated_cells")),
            "error_cells": _plain_int(None if not isinstance(completeness, Mapping) else completeness.get("error_cells")),
            "calculation_coverage_percentage": _percentage(
                None if not isinstance(coverage, Mapping) else coverage.get("calculation_coverage_percentage")
            ),
            "fully_comparable_percentage": _percentage(
                None if not isinstance(coverage, Mapping) else coverage.get("fully_comparable_percentage")
            ),
            "total_weight_text": weight_text,
            "total_weight_state": weight_state,
            "total_invoice_text": invoice_text,
            "total_invoice_state": invoice_state,
            "carriers": carriers,
            "potential_savings_text": savings_text,
            "potential_savings_state": savings_state,
            "decisive_row_count": _plain_int(competitive.get("decisive_row_count")),
            "tie_count": _plain_int(competitive.get("tie_count")),
            "comparable_row_count": _plain_int(competitive.get("comparable_row_count")),
            "savings_universe": _SAVINGS_UNIVERSE if wanted("official_differences") else None,
            "tie_rule": _TIE_RULE if wanted("official_differences") else None,
            "lead_carrier_slot": lead_slot,
            "lead_wins": lead_wins,
            "differences_universe": _token(None if not isinstance(universes, Mapping) else universes.get("differences"), PUBLIC_UNIVERSES),
            "population_universe": _token(
                None if not isinstance(universes, Mapping) else universes.get("population"),
                PUBLIC_UNIVERSES,
            )
            or (_POPULATION_UNIVERSE if wanted("universes") else None),
            "carrier_total_universe": _CARRIER_TOTAL_UNIVERSE if wanted("universes") else None,
        },
    )
    return [_unsigned("summary", _subject("query"), facts, losses)]


def _summary_carriers(values, differences, losses: _LossBook):
    per_table = values.get("per_table") if isinstance(values, Mapping) else []
    competitive = differences.get("carrier_competitiveness") if isinstance(differences, Mapping) else []
    if not isinstance(per_table, list):
        per_table = []
    if not isinstance(competitive, list):
        competitive = []
    base: dict[int, Mapping[str, Any]] = {}
    wins: dict[int, Mapping[str, Any]] = {}
    for item in per_table:
        slot = _slot_of(item)
        if slot is None:
            losses.add("carrier_identity_withheld")
            continue
        base[slot] = item
    for item in competitive:
        slot = _slot_of(item)
        if slot is None:
            losses.add("carrier_identity_withheld")
            continue
        wins[slot] = item
    carriers = []
    for slot in sorted(set(base) | set(wins)):
        item = base.get(slot, {})
        win = wins.get(slot, {})
        freight_text, freight_state = _money(item.get("official_freight_total"))
        comparable_text, comparable_state = _money(win.get("comparable_freight_total"))
        if freight_state == "unavailable" or comparable_state == "unavailable":
            losses.add("monetary_withheld")
        carriers.append(
            _exact(
                _CARRIER_KEYS,
                {
                    "carrier_slot": slot,
                    "population_rows": _plain_int(item.get("population_rows")),
                    "complete_rows": _plain_int(item.get("complete_rows") if "complete_rows" in item else win.get("calculated_rows")),
                    "complete_with_warnings_rows": _plain_int(
                        item.get("complete_with_warnings_rows")
                        if "complete_with_warnings_rows" in item
                        else win.get("calculated_with_warnings_rows")
                    ),
                    "partial_rows": _plain_int(item.get("partial_rows") if "partial_rows" in item else win.get("incomplete_rows")),
                    "not_calculated_rows": _plain_int(item.get("not_calculated_rows")),
                    "coverage_percentage": _percentage(
                        item.get("coverage_percentage") if "coverage_percentage" in item else win.get("coverage_percentage")
                    ),
                    "official_freight_text": freight_text,
                    "official_freight_state": freight_state if item else "absent",
                    "wins": _plain_int(win.get("wins")) if win else None,
                    "ties": _plain_int(win.get("ties")) if win else None,
                    "comparable_row_count": _plain_int(win.get("comparable_row_count")) if win else None,
                    "comparable_freight_text": comparable_text if win else None,
                    "comparable_freight_state": comparable_state if win else "absent",
                    "win_percentage": _percentage(win.get("win_percentage")) if win else None,
                },
            )
        )
    lead_slot = None
    lead_wins = None
    decisive = _plain_int(differences.get("competitive_summary", {}).get("decisive_row_count")) if isinstance(differences, Mapping) and isinstance(differences.get("competitive_summary"), Mapping) else None
    if decisive and wins:
        ranking = sorted(
            ((slot, _plain_int(item.get("wins")) or 0) for slot, item in wins.items()),
            key=lambda item: (-item[1], item[0]),
        )
        top = [item for item in ranking if item[1] == ranking[0][1]]
        if len(top) == 1 and top[0][1] > 0:
            lead_slot = top[0][0]
            lead_wins = top[0][1]
    return carriers, lead_slot, lead_wins


def _project_freight_components(query, table_slots, population, page) -> list[dict[str, Any]]:
    del table_slots, population, page
    body = query["body"]
    losses = _LossBook()
    wanted = _wanted(query)
    rows = body.get("rows") if wanted("rows") else []
    if not isinstance(rows, list):
        rows = []
    counts = body.get("availability_counts") if wanted("availability_counts") else None
    missing = body.get("missing_row_indexes") if wanted("rows") and isinstance(body.get("missing_row_indexes"), list) else []
    summary = _exact(
        _SUMMARY_FACTS["freight_components"],
        {
            "matched_rows": _plain_int(query.get("population", {}).get("eligible_rows"))
            if isinstance(query.get("population"), Mapping)
            else None,
            "returned_rows": len(rows) if wanted("rows") else None,
            "missing_row_indexes": [item for item in missing if type(item) is int],
            "availability_counts": _availability_counts(counts),
        },
    )
    records = [_unsigned("summary", _subject("query"), summary, _LossBook())]
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        slot = _slot_of(item)
        row_index = _plain_int(item.get("row_index"))
        if slot is None or row_index is None:
            losses.add("carrier_identity_withheld")
            continue
        final_text, final_state = _money(item.get("final_amount"))
        partial_text, partial_state = _money(item.get("partial_amount"))
        if final_state == "unavailable" or partial_state == "unavailable":
            losses.add("monetary_withheld")
        components, component_losses = _components(item.get("components"))
        buckets, bucket_losses = _buckets(item.get("aggregated_buckets"))
        counts_public, unmapped = _accessorial_counts(item.get("itemized_accessorials"), item.get("ignored_accessorials"))
        item_losses = _LossBook()
        item_losses.add("monetary_withheld", int(final_state == "unavailable") + int(partial_state == "unavailable"))
        for code, count in component_losses.items():
            item_losses.add(code, count)
        for code, count in bucket_losses.items():
            item_losses.add(code, count)
        facts = _exact(
            _DETAIL_FACTS["freight_components"],
            {
                "row_index": row_index,
                "carrier_slot": slot,
                "completeness": _token(item.get("completeness"), PUBLIC_COMPLETENESS),
                "final_amount_text": final_text,
                "final_amount_state": final_state,
                "partial_amount_text": partial_text,
                "partial_amount_state": partial_state,
                "amount_support": _token(item.get("amount_support"), _AMOUNT_SUPPORTS),
                "origin": _token(item.get("origin"), PUBLIC_COMPONENT_SOURCES),
                "detail_level": _token(item.get("detail_level"), _DETAIL_LEVELS),
                "components": components,
                "buckets": buckets,
                "unresolved_component_refs": _plain_int(item.get("unresolved_component_refs")) or 0,
                "unmapped_accessorial_count": unmapped,
                "canonical_accessorial_counts": counts_public,
            },
        )
        records.append(_unsigned("detail", _subject("row", row_index=row_index, carrier_slot=slot), facts, item_losses))
    if losses:
        records[0]["losses"].add("carrier_identity_withheld", 1)
    return records


def _project_carrier_comparison(query, table_slots, population, page) -> list[dict[str, Any]]:
    del population, page
    body = query["body"]
    losses = _LossBook()
    wanted = _wanted(query)
    source = _token(body.get("indicator_source"), PUBLIC_INDICATOR_SOURCES)
    universe = _token(body.get("universe"), PUBLIC_UNIVERSES) if wanted("universe") else None
    if wanted("universe") and universe is None:
        losses.add("universe_withheld")
    exclusions = body.get("exclusions") if wanted("exclusions") and isinstance(body.get("exclusions"), Mapping) else {}
    differences = body.get("differences") if wanted("differences") and isinstance(body.get("differences"), Mapping) else {}
    publish_carriers = wanted("carriers")
    publish_coverage = wanted("coverage")
    if source == "official_analytics":
        competitive = differences.get("competitive_summary") if isinstance(differences.get("competitive_summary"), Mapping) else {}
        savings_text, savings_state = _money(competitive.get("total_potential_savings")) if wanted("differences") else (None, "absent")
        decisive = _plain_int(competitive.get("decisive_row_count")) if wanted("differences") else None
        ties = _plain_int(competitive.get("tie_count")) if wanted("differences") else None
        comparable = _plain_int(competitive.get("comparable_row_count")) if wanted("differences") else None
        carriers = (
            _official_comparison_carriers(
                body.get("carriers") if publish_carriers else [],
                body.get("coverage") if publish_coverage else [],
                table_slots,
                losses,
            )
            if publish_carriers or publish_coverage
            else []
        )
    else:
        savings_text, savings_state = _money(differences.get("potential_savings")) if wanted("differences") else (None, "absent")
        decisive = _plain_int(differences.get("decisive_row_count")) if wanted("differences") else None
        ties = _plain_int(differences.get("tie_count")) if wanted("differences") else None
        comparable = (
            _plain_int(query.get("population", {}).get("eligible_rows"))
            if wanted("differences") and isinstance(query.get("population"), Mapping)
            else None
        )
        carriers = (
            _subset_comparison_carriers(
                body.get("carriers") if publish_carriers else [],
                body.get("coverage") if publish_coverage else [],
                table_slots,
                losses,
            )
            if publish_carriers or publish_coverage
            else []
        )
    if wanted("differences") and savings_state == "unavailable":
        losses.add("monetary_withheld")
    facts = _exact(
        _SUMMARY_FACTS["carrier_comparison"],
        {
            "universe": universe,
            "indicator_source": source if wanted("indicator_source") else None,
            "tie_rule": _TIE_RULE if wanted("differences") or wanted("superiority_limit") else None,
            "global_superiority": False,
            "comparable_row_count": comparable,
            "decisive_row_count": decisive,
            "tie_count": ties,
            "potential_savings_text": savings_text,
            "potential_savings_state": savings_state,
            "partially_comparable_rows": _plain_int(exclusions.get("partially_comparable_rows")) if wanted("exclusions") else None,
            "inconclusive_rows": _plain_int(exclusions.get("inconclusive_rows")) if wanted("exclusions") else None,
            "carriers": carriers,
        },
    )
    return [_unsigned("summary", _subject("query"), facts, losses)]


def _official_comparison_carriers(carriers, coverage, table_slots, losses: _LossBook) -> list[dict[str, Any]]:
    by_slot: dict[int, dict[str, Any]] = {}
    for item in carriers or []:
        slot = _slot_of(item) or _slot_from_map(item.get("table_id"), table_slots)
        if slot is None:
            losses.add("carrier_identity_withheld")
            continue
        freight_text, freight_state = _money(item.get("comparable_freight_total"))
        if freight_state == "unavailable":
            losses.add("monetary_withheld")
        by_slot[slot] = {
            "comparable_freight_text": freight_text,
            "comparable_freight_state": freight_state,
            "wins": _plain_int(item.get("wins")),
            "ties": _plain_int(item.get("ties")),
            "comparable_row_count": _plain_int(item.get("comparable_row_count")),
            "win_percentage": _percentage(item.get("win_percentage")),
            "complete_with_warnings_rows": _plain_int(item.get("calculated_with_warnings_rows")),
            "not_calculated_rows": _plain_int(item.get("not_calculated_rows")),
        }
    for item in coverage or []:
        slot = _slot_of(item) or _slot_from_map(item.get("table_id"), table_slots)
        if slot is None:
            losses.add("carrier_identity_withheld")
            continue
        freight_text, freight_state = _money(item.get("official_freight_total"))
        if freight_state == "unavailable":
            losses.add("monetary_withheld")
        current = by_slot.setdefault(slot, {})
        current.update(
            {
                "complete_rows": _plain_int(item.get("complete_rows")),
                "partial_rows": _plain_int(item.get("partial_rows")),
                "not_calculated_rows": current.get("not_calculated_rows", _plain_int(item.get("not_calculated_rows"))),
                "coverage_percentage": _percentage(item.get("coverage_percentage")),
                "official_freight_text": freight_text,
                "official_freight_state": freight_state,
            }
        )
    published = []
    for slot in sorted(by_slot):
        item = by_slot[slot]
        published.append(
            _exact(
                _CARRIER_KEYS,
                {
                    "carrier_slot": slot,
                    "population_rows": None,
                    "complete_rows": item.get("complete_rows"),
                    "complete_with_warnings_rows": item.get("complete_with_warnings_rows"),
                    "partial_rows": item.get("partial_rows"),
                    "not_calculated_rows": item.get("not_calculated_rows"),
                    "coverage_percentage": item.get("coverage_percentage"),
                    "official_freight_text": item.get("official_freight_text"),
                    "official_freight_state": item.get("official_freight_state", "absent"),
                    "wins": item.get("wins"),
                    "ties": item.get("ties"),
                    "comparable_row_count": item.get("comparable_row_count"),
                    "comparable_freight_text": item.get("comparable_freight_text"),
                    "comparable_freight_state": item.get("comparable_freight_state", "absent"),
                    "win_percentage": item.get("win_percentage"),
                },
            )
        )
    return published


def _subset_comparison_carriers(carriers, coverage, table_slots, losses: _LossBook) -> list[dict[str, Any]]:
    by_slot: dict[int, dict[str, Any]] = {}
    for item in carriers or []:
        slot = _slot_of(item)
        if slot is None:
            losses.add("carrier_identity_withheld")
            continue
        freight_text, freight_state = _money(item.get("comparable_freight_total"))
        if freight_state == "unavailable":
            losses.add("monetary_withheld")
        by_slot[slot] = {
            "comparable_freight_text": freight_text,
            "comparable_freight_state": freight_state,
            "comparable_row_count": _plain_int(item.get("comparable_row_count")),
        }
    for item in coverage or []:
        slot = _slot_of(item) or _slot_from_map(item.get("table_id"), table_slots)
        if slot is None:
            losses.add("carrier_identity_withheld")
            continue
        current = by_slot.setdefault(slot, {})
        current.update(
            {
                "complete_rows": _plain_int(item.get("complete_rows")),
                "partial_rows": _plain_int(item.get("partial_rows")),
                "not_calculated_rows": _plain_int(item.get("not_calculated_rows")),
                "coverage_percentage": _percentage(item.get("coverage_percentage")),
            }
        )
    published = []
    for slot in sorted(by_slot):
        item = by_slot[slot]
        published.append(
            _exact(
                _CARRIER_KEYS,
                {
                    "carrier_slot": slot,
                    "population_rows": None,
                    "complete_rows": item.get("complete_rows"),
                    "complete_with_warnings_rows": None,
                    "partial_rows": item.get("partial_rows"),
                    "not_calculated_rows": item.get("not_calculated_rows"),
                    "coverage_percentage": item.get("coverage_percentage"),
                    "official_freight_text": None,
                    "official_freight_state": "absent",
                    "wins": None,
                    "ties": None,
                    "comparable_row_count": item.get("comparable_row_count"),
                    "comparable_freight_text": item.get("comparable_freight_text"),
                    "comparable_freight_state": item.get("comparable_freight_state", "absent"),
                    "win_percentage": None,
                },
            )
        )
    return published


def _project_top_differences(query, table_slots, population, page) -> list[dict[str, Any]]:
    del population, page
    body = query["body"]
    losses = _LossBook()
    wanted = _wanted(query)
    gap_text, gap_state = _money(body.get("gap_total")) if wanted("gap_total") else (None, "absent")
    if wanted("gap_total") and gap_state == "unavailable":
        losses.add("monetary_withheld")
    ranked = (
        _plain_int(query.get("population", {}).get("eligible_rows"))
        if (wanted("gap_total") or wanted("ordered_differences")) and isinstance(query.get("population"), Mapping)
        else None
    )
    summary = _exact(
        _SUMMARY_FACTS["top_differences"],
        {
            "gap_total_text": gap_text,
            "gap_total_state": gap_state,
            "ranked_rows": ranked,
            "difference_definition": _GAP_DEFINITION if wanted("difference_definition") else None,
        },
    )
    records = [_unsigned("summary", _subject("query"), summary, losses)]
    ordered = body.get("ordered_differences") if wanted("ordered_differences") else []
    for item in ordered or []:
        if not isinstance(item, Mapping):
            continue
        row_index = _plain_int(item.get("row_index"))
        amount_text, amount_state = _money(item.get("absolute_gap"))
        item_losses = _LossBook()
        if amount_state == "unavailable" or row_index is None:
            item_losses.add("monetary_withheld")
            continue
        freights = []
        for freight in item.get("freights") or []:
            if not isinstance(freight, Mapping):
                continue
            slot = _slot_from_map(freight.get("table_id"), table_slots)
            freight_text, freight_state = _money(freight.get("amount"))
            if slot is None or freight_state == "unavailable":
                item_losses.add("carrier_identity_withheld" if slot is None else "monetary_withheld")
                freights = []
                break
            freights.append(
                _exact(
                    _FREIGHT_KEYS,
                    {"carrier_slot": slot, "amount_text": freight_text, "amount_state": freight_state},
                )
            )
        if not freights:
            records[0]["losses"].add("carrier_identity_withheld")
            continue
        facts = _exact(
            _DETAIL_FACTS["top_differences"],
            {
                "row_index": row_index,
                "absolute_gap_text": amount_text,
                "absolute_gap_state": amount_state,
                "tie": item.get("tie") is True,
                "freights": freights,
            },
        )
        records.append(_unsigned("detail", _subject("row", row_index=row_index), facts, item_losses))
    return records


def _project_by_uf(query, table_slots, population, page) -> list[dict[str, Any]]:
    del population, page
    body = query["body"]
    losses = _LossBook()
    wanted = _wanted(query)
    alignment = body.get("alignment") if wanted("alignment") and isinstance(body.get("alignment"), Mapping) else {}
    reason = alignment.get("reason") if wanted("alignment") else None
    safe_reason = reason if reason in PUBLIC_ALIGNMENT_REASONS else None
    if wanted("alignment") and reason is not None and safe_reason is None:
        losses.add("alignment_withheld")
    filters = query.get("filters") if isinstance(query.get("filters"), Mapping) else {}
    selected = [item for item in filters.get("ufs") or [] if item in OFFICIAL_UF_CODES]
    exclusions = body.get("exclusions") if wanted("exclusions") and isinstance(body.get("exclusions"), Mapping) else {}
    summary = _exact(
        _SUMMARY_FACTS["by_uf"],
        {
            "examined_rows": _plain_int(body.get("examined_rows")),
            "eligible_rows": _plain_int(body.get("eligible_rows")),
            "excluded_rows": _plain_int(body.get("excluded_rows")),
            "comparable_rows": _plain_int(body.get("comparable_rows")),
            "filtered_uf_rows": _plain_int(exclusions.get("filtered_uf_rows")) if wanted("exclusions") else None,
            "absent_rows": _plain_int(exclusions.get("absent_rows")) if wanted("exclusions") else None,
            "unusable_rows": _plain_int(exclusions.get("unusable_rows")) if wanted("exclusions") else None,
            "include_absent": filters.get("include_absent") if type(filters.get("include_absent")) is bool else True,
            "include_unusable": filters.get("include_unusable") if type(filters.get("include_unusable")) is bool else True,
            "selected_ufs": selected,
            "aligned": alignment.get("aligned_with_official_geography") is True if wanted("alignment") else None,
            "alignment_reason": safe_reason,
            "group_counts": _group_counts(body.get("groups")) if wanted("groups") else [],
        },
    )
    records = [_unsigned("summary", _subject("query"), summary, losses)]
    if wanted("groups"):
        for item in body.get("groups") or []:
            records.append(_group_record(item, table_slots, role="detail"))
    for key, role_id, field in (
        ("absent", "uf_absent", "absent"),
        ("unusable", "uf_unusable", "unusable"),
    ):
        if not wanted(field):
            continue
        item = body.get(key)
        if isinstance(item, Mapping):
            records.append(_group_record(item, table_slots, role="special_group", fallback_id=role_id))
    geography = (
        _official_geography(body.get("official_analytics_geography"), table_slots, filters)
        if wanted("official_analytics_geography")
        else None
    )
    if geography is not None:
        records.append(_unsigned("official_geography", _subject("query"), geography, _LossBook()))
    return [item for item in records if item is not None]


def _project_by_weight(query, table_slots, population, page) -> list[dict[str, Any]]:
    del population, page
    body = query["body"]
    wanted = _wanted(query)
    exclusions = body.get("exclusions") if wanted("exclusions") and isinstance(body.get("exclusions"), Mapping) else {}
    rule = body.get("band_rule") if wanted("band_rule") and isinstance(body.get("band_rule"), Mapping) else {}
    unsupported = (
        body.get("unsupported_decimal_weight")
        if wanted("exclusions") and isinstance(body.get("unsupported_decimal_weight"), Mapping)
        else {}
    )
    summary = _exact(
        _SUMMARY_FACTS["by_weight"],
        {
            "examined_rows": _plain_int(body.get("examined_rows")),
            "numeric_weight_rows": _plain_int(body.get("numeric_weight_rows")),
            "complete_freight_rows": _plain_int(body.get("complete_freight_rows")),
            "complete_freight_count": _plain_int(body.get("complete_freight_count")),
            "comparable_rows": _plain_int(body.get("comparable_rows")),
            "absent_rows": _plain_int(exclusions.get("absent_rows")) if wanted("exclusions") else None,
            "invalid_rows": _plain_int(exclusions.get("invalid_rows")) if wanted("exclusions") else None,
            "unsupported_decimal_rows": _plain_int(unsupported.get("row_count")) if wanted("exclusions") else None,
            "excluded_invalid_or_absent_weight": _plain_int(exclusions.get("excluded_invalid_or_absent_weight"))
            if wanted("exclusions")
            else None,
            "excluded_weight_reference": _token(exclusions.get("excluded_invalid_or_absent_weight_reference"), _REFERENCE_TOKENS)
            if wanted("exclusions")
            else None,
            "excluded_not_comparable_rows": _plain_int(exclusions.get("excluded_not_comparable_rows"))
            if wanted("exclusions")
            else None,
            "excluded_comparability_reference": _token(exclusions.get("excluded_not_comparable_reference"), _REFERENCE_TOKENS)
            if wanted("exclusions")
            else None,
            "bands_version": WEIGHT_BANDS_VERSION if wanted("band_rule") and rule.get("version") in {None, WEIGHT_BANDS_VERSION} else None,
            "lower_inclusive": True if wanted("band_rule") else None,
            "upper_exclusive": True if wanted("band_rule") else None,
            "last_band_rule": _LAST_BAND_RULE if wanted("band_rule") else None,
            "band_counts": _group_counts(body.get("bands")) if wanted("bands") else [],
        },
    )
    records = [_unsigned("summary", _subject("query"), summary, _LossBook())]
    if wanted("bands"):
        for item in body.get("bands") or []:
            records.append(_group_record(item, table_slots, role="detail"))
    for key, fallback, field in (("absent", "weight_absent", "absent"), ("invalid", "weight_invalid", "invalid")):
        if not wanted(field):
            continue
        item = body.get(key)
        if isinstance(item, Mapping):
            records.append(_group_record(item, table_slots, role="special_group", fallback_id=fallback))
    if unsupported:
        withheld = _exact(
            _GROUP_FACTS,
            _empty_group("weight_unsupported_decimal", "unusable", _plain_int(unsupported.get("row_count")) or 0, included=False),
        )
        records.append(
            _unsigned("special_group", _subject("group", group_id="weight_unsupported_decimal"), withheld, _LossBook())
        )
    return [item for item in records if item is not None]


def _project_incomplete(query, table_slots, population, page) -> list[dict[str, Any]]:
    del population, page
    body = query["body"]
    wanted = _wanted(query)
    summary = _exact(
        _SUMMARY_FACTS["incomplete_freights"],
        {
            "partial_rows": _plain_int(query.get("population", {}).get("eligible_rows"))
            if wanted("rows") and isinstance(query.get("population"), Mapping)
            else None,
            "excluded_complete_with_warnings": _plain_int(body.get("excluded_complete_with_warnings"))
            if wanted("excluded_complete_with_warnings")
            else None,
        },
    )
    records = [_unsigned("summary", _subject("query"), summary, _LossBook())]
    for item in (body.get("rows") if wanted("rows") else []) or []:
        if not isinstance(item, Mapping):
            continue
        row_index = _plain_int(item.get("row_index"))
        if row_index is None:
            continue
        cells = []
        for cell in item.get("cells") or []:
            if not isinstance(cell, Mapping):
                continue
            slot = _slot_from_map(cell.get("table_id"), table_slots) or _slot_of(cell)
            if slot is None:
                continue
            partial_text, partial_state = _money(cell.get("partial_amount"))
            official, unmapped = _reason_codes(cell.get("reasons"))
            cells.append(
                _exact(
                    _PARTIAL_CELL_KEYS,
                    {
                        "carrier_slot": slot,
                        "completeness": "partial",
                        "partial_amount_text": partial_text,
                        "partial_amount_state": partial_state,
                        "final_amount_state": "absent",
                        "official_reason_codes": official,
                        "unmapped_reason_count": unmapped,
                    },
                )
            )
        facts = _exact(_DETAIL_FACTS["incomplete_freights"], {"row_index": row_index, "cells": cells})
        records.append(_unsigned("detail", _subject("row", row_index=row_index), facts, _LossBook()))
    return records


def _project_diagnostics(query, table_slots, population, page) -> list[dict[str, Any]]:
    del population, page
    body = query["body"]
    wanted = _wanted(query)
    include_constraints = wanted("constraints")
    by_code = []
    if wanted("by_code"):
        for item in body.get("by_code") or []:
            if not isinstance(item, Mapping) or item.get("code") not in OFFICIAL_DIAGNOSTIC_CODES:
                continue
            code = item["code"]
            constraint = DIAGNOSTIC_CONSTRAINTS.get(code, {}) if include_constraints else {}
            by_code.append(
                _exact(
                    _DIAGNOSTIC_CODE_KEYS,
                    {
                        "code": code,
                        "cell_count": _plain_int(item.get("cell_count")) or 0,
                        "row_count": _plain_int(item.get("row_count")) or 0,
                        "preserved_meaning": constraint.get("preserved_meaning") if include_constraints else None,
                        "must_not_assert": [entry for entry in constraint.get("must_not_assert") or [] if isinstance(entry, str)]
                        if include_constraints
                        else [],
                    },
                )
            )
    unmapped = 0
    if wanted("preserved_accessorial_codes"):
        for item in body.get("preserved_accessorial_codes") or []:
            if isinstance(item, Mapping) and item.get("code") not in OFFICIAL_DIAGNOSTIC_CODES:
                unmapped += _plain_int(item.get("cell_count")) or 0
    summary = _exact(
        _SUMMARY_FACTS["non_calculation_diagnostics"],
        {
            "by_code": by_code,
            "unmapped_code_occurrences": unmapped if wanted("preserved_accessorial_codes") else None,
        },
    )
    records = [_unsigned("summary", _subject("query"), summary, _LossBook())]
    if not wanted("rows"):
        return records
    for item in body.get("rows") or []:
        if not isinstance(item, Mapping):
            continue
        row_index = _plain_int(item.get("row_index"))
        if row_index is None:
            continue
        cells = []
        for cell in item.get("cells") or []:
            if not isinstance(cell, Mapping):
                continue
            slot = _slot_from_map(cell.get("table_id"), table_slots) or _slot_of(cell)
            if slot is None:
                continue
            primary = cell.get("code") if cell.get("code") in OFFICIAL_DIAGNOSTIC_CODES else None
            extras = [
                code
                for code in cell.get("preserved_codes") or []
                if code in OFFICIAL_DIAGNOSTIC_CODES and code != primary
            ]
            unknown = sum(
                1
                for code in cell.get("preserved_codes") or []
                if isinstance(code, str) and code not in OFFICIAL_DIAGNOSTIC_CODES
            )
            if primary is None:
                unknown += 1
            constraint = DIAGNOSTIC_CONSTRAINTS.get(primary, {}) if primary and include_constraints else {}
            cells.append(
                _exact(
                    _DIAGNOSTIC_CELL_KEYS,
                    {
                        "carrier_slot": slot,
                        "code_state": "official" if primary else "unmapped",
                        "official_code": primary,
                        "preserved_meaning": constraint.get("preserved_meaning") if primary and include_constraints else None,
                        "must_not_assert": [entry for entry in constraint.get("must_not_assert") or [] if isinstance(entry, str)]
                        if primary and include_constraints
                        else [],
                        "extra_official_codes": extras,
                        "unmapped_occurrence_count": unknown,
                        "unresolved_diagnostic_refs": _plain_int(cell.get("unresolved_diagnostic_refs")) or 0,
                    },
                )
            )
        facts = _exact(_DETAIL_FACTS["non_calculation_diagnostics"], {"row_index": row_index, "cells": cells})
        records.append(_unsigned("detail", _subject("row", row_index=row_index), facts, _LossBook()))
    return records


def _group_counts(items) -> list[dict[str, Any]]:
    counts = []
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        group_id = item.get("id") if isinstance(item.get("id"), str) else None
        if group_id not in _SAFE_GROUP_IDS:
            continue
        counts.append(_exact(_GROUP_COUNT_KEYS, {"group_id": group_id, "row_count": _plain_int(item.get("row_count")) or 0}))
    return counts


def _group_record(item, table_slots, *, role: str, fallback_id: str | None = None):
    if not isinstance(item, Mapping):
        return None
    raw_id = item.get("id") if isinstance(item.get("id"), str) else fallback_id
    if raw_id not in _SAFE_GROUP_IDS:
        raw_id = fallback_id
    if raw_id not in _SAFE_GROUP_IDS:
        return None
    included = item.get("included", True) is not False and item.get("monetary_values_published", True) is not False
    group_state = "uf" if raw_id in OFFICIAL_UF_CODES else ("absent" if "absent" in raw_id else "unusable")
    if raw_id in _BAND_IDS:
        group_state = "band"
    if not included:
        facts = _exact(_GROUP_FACTS, _empty_group(raw_id, group_state, _plain_int(item.get("row_count")) or 0, included=False))
        return _unsigned(role, _subject("group", group_id=raw_id), facts, _LossBook())
    savings_text, savings_state = _money(item.get("potential_savings"))
    lower_text, lower_state = _money(item.get("lower")) if "lower" in item else (None, "absent")
    upper_text, upper_state = _money(item.get("upper")) if item.get("upper") is not None else (None, "absent")
    if "upper" in item and item.get("upper") is None:
        upper_state = "absent"
    carriers = _geo_carriers(item.get("coverage"), table_slots, publish_money=True)
    facts = _exact(
        _GROUP_FACTS,
        {
            "group_id": raw_id,
            "group_state": group_state,
            "row_count": _plain_int(item.get("row_count")) or 0,
            "comparable_row_count": _plain_int(item.get("comparable_row_count")),
            "excluded_row_count": _plain_int(item.get("excluded_row_count")),
            "scanned_rows": _plain_int(item.get("scanned_rows")),
            "potential_savings_text": savings_text,
            "potential_savings_state": savings_state,
            "monetary_values_published": True,
            "included": True,
            "lower_text": lower_text,
            "lower_state": lower_state,
            "upper_text": upper_text,
            "upper_state": upper_state,
            "lower_inclusive": item.get("lower_inclusive") if type(item.get("lower_inclusive")) is bool else None,
            "upper_exclusive": item.get("upper_exclusive") if type(item.get("upper_exclusive")) is bool else None,
            "last_band": item.get("last_band") if type(item.get("last_band")) is bool else None,
            "carriers": carriers,
        },
    )
    return _unsigned(role, _subject("group", group_id=raw_id), facts, _LossBook())


def _empty_group(group_id: str, group_state: str, row_count: int, *, included: bool) -> dict[str, Any]:
    return {
        "group_id": group_id,
        "group_state": group_state,
        "row_count": row_count,
        "comparable_row_count": None,
        "excluded_row_count": None,
        "scanned_rows": None,
        "potential_savings_text": None,
        "potential_savings_state": "withheld",
        "monetary_values_published": False,
        "included": included,
        "lower_text": None,
        "lower_state": "absent",
        "upper_text": None,
        "upper_state": "absent",
        "lower_inclusive": None,
        "upper_exclusive": None,
        "last_band": None,
        "carriers": [],
    }


def _geo_carriers(coverage, table_slots, *, publish_money: bool) -> list[dict[str, Any]]:
    published = []
    for item in coverage or []:
        if not isinstance(item, Mapping):
            continue
        slot = _slot_of(item) or _slot_from_map(item.get("table_id"), table_slots)
        if slot is None:
            continue
        if publish_money:
            complete_text, complete_state = _money(item.get("complete_freight_total"))
            comparable_text, comparable_state = _money(item.get("comparable_freight_total"))
        else:
            complete_text, complete_state = None, "withheld"
            comparable_text, comparable_state = None, "withheld"
        published.append(
            _exact(
                _GEO_CARRIER_KEYS,
                {
                    "carrier_slot": slot,
                    "complete_rows": _plain_int(item.get("complete_rows")),
                    "coverage_percentage": _percentage(item.get("coverage_percentage")),
                    "complete_freight_text": complete_text,
                    "complete_freight_state": complete_state,
                    "comparable_freight_text": comparable_text,
                    "comparable_freight_state": comparable_state,
                    "wins": _plain_int(item.get("wins")),
                },
            )
        )
    published.sort(key=lambda item: item["carrier_slot"])
    return published


def _official_geography(geography, table_slots, filters) -> dict[str, Any] | None:
    if not isinstance(geography, Mapping):
        return None
    include_absent = filters.get("include_absent") if type(filters.get("include_absent")) is bool else True
    include_unusable = filters.get("include_unusable") if type(filters.get("include_unusable")) is bool else True
    selected = {item for item in filters.get("ufs") or [] if item in OFFICIAL_UF_CODES}
    groups = []
    withheld = 0
    for item in geography.get("destination_ufs") or []:
        if not isinstance(item, Mapping):
            continue
        label = item.get("uf_label")
        if label in OFFICIAL_UF_CODES:
            if selected and label not in selected:
                continue
            group_state = "uf"
            uf = label
        elif label == "N/D":
            if not include_absent:
                continue
            group_state = "absent"
            uf = None
        else:
            withheld += _plain_int(item.get("row_count")) or 0
            if not include_unusable:
                continue
            continue
        savings_text, savings_state = _money(item.get("total_potential_savings"))
        winner = _slot_from_map(item.get("winner_table_id"), table_slots)
        map_status = item.get("map_status") if item.get("map_status") in PUBLIC_MAP_STATUSES else "withheld"
        if item.get("winner_table_id") and winner is None:
            map_status = "withheld"
        groups.append(
            _exact(
                _OFFICIAL_UF_KEYS,
                {
                    "group_state": group_state,
                    "uf": uf,
                    "row_count": _plain_int(item.get("row_count")) or 0,
                    "comparable_row_count": _plain_int(item.get("comparable_row_count")) or 0,
                    "potential_savings_text": savings_text,
                    "potential_savings_state": savings_state,
                    "tie_count": _plain_int(item.get("tie_count")) or 0,
                    "is_tie": item.get("is_tie") is True,
                    "has_comparable_base": item.get("has_comparable_base") is True,
                    "low_sample": item.get("low_sample") is True,
                    "map_status": map_status,
                    "winner_slot": winner,
                    "carriers": _official_uf_carriers(item.get("tables"), table_slots),
                },
            )
        )
    threshold = item_threshold if type(item_threshold := geography.get("low_sample_threshold")) is int else None
    return _exact(
        _OFFICIAL_GEO_FACTS,
        {"low_sample_threshold": threshold, "groups": groups, "withheld_label_count": withheld},
    )


def _official_uf_carriers(tables, table_slots) -> list[dict[str, Any]]:
    published = []
    for item in tables or []:
        if not isinstance(item, Mapping):
            continue
        slot = _slot_of(item) or _slot_from_map(item.get("table_id"), table_slots)
        if slot is None:
            continue
        freight_text, freight_state = _money(item.get("comparable_freight_total"))
        published.append(
            _exact(
                _GEO_CARRIER_KEYS,
                {
                    "carrier_slot": slot,
                    "complete_rows": _plain_int(item.get("complete_rows")),
                    "coverage_percentage": _percentage(item.get("coverage_percentage")),
                    "complete_freight_text": None,
                    "complete_freight_state": "absent",
                    "comparable_freight_text": freight_text,
                    "comparable_freight_state": freight_state,
                    "wins": _plain_int(item.get("wins")),
                },
            )
        )
    published.sort(key=lambda item: item["carrier_slot"])
    return published


def _components(slots) -> tuple[list[dict[str, Any]], dict[str, int]]:
    losses: dict[str, int] = {}
    if not isinstance(slots, Mapping):
        return [], losses
    published = []
    for code in sorted(slots):
        if code not in COMPONENT_CODES:
            continue
        slot = slots.get(code)
        if not isinstance(slot, Mapping):
            continue
        text, state = _money(slot.get("amount"))
        availability = _token(slot.get("availability"), PUBLIC_AVAILABILITY)
        if state == "unavailable":
            losses["monetary_withheld"] = losses.get("monetary_withheld", 0) + 1
        if slot.get("conflict") is True:
            losses["component_conflict"] = losses.get("component_conflict", 0) + 1
        published.append(
            _exact(
                _COMPONENT_KEYS,
                {
                    "code": code,
                    "label": PUBLIC_COMPONENT_LABELS[code],
                    "amount_text": text,
                    "amount_state": state if state in PUBLIC_AMOUNT_STATES else "unavailable",
                    "availability": availability,
                    "detail_level": _token(slot.get("detail_level"), _DETAIL_LEVELS),
                    "source": _token(slot.get("source"), PUBLIC_COMPONENT_SOURCES),
                    "numeric_support": _token(slot.get("numeric_support"), _NUMERIC_SUPPORTS),
                },
            )
        )
    return published, losses


def _buckets(items) -> tuple[list[dict[str, Any]], dict[str, int]]:
    losses: dict[str, int] = {}
    published = []
    unknown = 0
    for item in items or []:
        if not isinstance(item, Mapping):
            unknown += 1
            continue
        code = item.get("code")
        if code not in PUBLIC_BUCKET_CODES:
            unknown += 1
            continue
        text, state = _money(item.get("amount"))
        if state == "unavailable":
            losses["monetary_withheld"] = losses.get("monetary_withheld", 0) + 1
        published.append(
            _exact(
                _BUCKET_KEYS,
                {
                    "code": code,
                    "amount_text": text,
                    "amount_state": state,
                    "availability": _token(item.get("availability"), PUBLIC_AVAILABILITY),
                },
            )
        )
    if unknown:
        losses["schema_rejected"] = unknown
    return published, losses


def _accessorial_counts(applied, ignored) -> tuple[list[dict[str, Any]], int]:
    counts: dict[tuple[str, bool], int] = {}
    unmapped = 0
    for applied_flag, items in ((True, applied), (False, ignored)):
        for item in items or []:
            if not isinstance(item, Mapping):
                unmapped += 1
                continue
            code = item.get("canonical_component")
            if code not in _ACCESSORIAL_COUNT_CODES:
                unmapped += 1
                code = "unmapped"
            key = (code, applied_flag)
            counts[key] = counts.get(key, 0) + 1
    published = [
        _exact(_CANONICAL_COUNT_KEYS, {"code": code, "applied": flag, "count": count})
        for (code, flag), count in sorted(counts.items())
        if code in _ACCESSORIAL_COUNT_CODES
    ]
    return published, unmapped


def _availability_counts(counts) -> list[dict[str, Any]]:
    if not isinstance(counts, Mapping):
        return []
    published = []
    for code in sorted(counts):
        if code not in COMPONENT_CODES or not isinstance(counts.get(code), Mapping):
            continue
        for availability in sorted(counts[code]):
            if availability not in PUBLIC_AVAILABILITY:
                continue
            count = _plain_int(counts[code].get(availability))
            if count is None:
                continue
            published.append(_exact(_COUNT_KEYS, {"code": code, "availability": availability, "count": count}))
    return published


def _reason_codes(reasons) -> tuple[list[str], int]:
    official = []
    unmapped = 0
    for item in reasons or []:
        if not isinstance(item, Mapping):
            unmapped += 1
            continue
        code = item.get("code")
        if code in OFFICIAL_DIAGNOSTIC_CODES and code not in official:
            official.append(code)
        elif code:
            unmapped += 1
    return official, unmapped


def _assemble(
    *,
    operation_id: str,
    scope: AuthorizedComparisonScope,
    examined_rows: int,
    private_queries: Sequence[Mapping[str, Any]],
    selected: list[dict[str, Any]],
    produced: int,
    ref_omitted: int,
    context_omitted: int,
    queries_complete: bool,
    plan_error: str | None,
    table_slots: Mapping[str, int],
    planned_by_query: Mapping[str, int],
) -> dict[str, Any]:
    by_query: dict[str, list[dict[str, Any]]] = {}
    for record in selected:
        by_query.setdefault(record["query_id"], []).append(record)
    queries = []
    plan_losses = _LossBook()
    if ref_omitted:
        plan_losses.add("evidence_omitted", ref_omitted)
    if context_omitted:
        plan_losses.add("context_omitted", context_omitted)
    analysis_complete = queries_complete and plan_error is None
    all_published = True
    all_sufficient = analysis_complete and not plan_losses
    for private in private_queries:
        query_id = private.get("query_id")
        sealed_source = by_query.get(query_id, [])
        planned_count = int(planned_by_query.get(query_id, 0))
        query_losses = _LossBook()
        for record in sealed_source:
            for loss in record["losses"].export():
                query_losses.add(loss["code"], loss["count"])
        dropped = plan_error is None and private.get("status") == "ok" and len(sealed_source) < planned_count
        if dropped:
            query_losses.add("page_incomplete", planned_count - len(sealed_source))
            all_published = False
        if private.get("status") != "ok":
            all_published = False
        page = _page(private.get("page"))
        if page is not None and private.get("status") == "ok":
            detail_count = sum(1 for item in sealed_source if item.get("record_role") == "detail")
            page = dict(page)
            if detail_count != page["returned"]:
                page["returned"] = detail_count
            for item in sealed_source:
                item["page"] = copy.deepcopy(page)
        delivered = _seal_query_records(
            sealed_source,
            operation_id=operation_id,
            scope=scope,
        )
        supported = _claims(private.get("query_type"), delivered)
        claims = _authorize_claims(supported, query_losses, plan_losses)
        query_published = private.get("status") == "ok" and not dropped and plan_error is None
        necessary_blocked = bool(supported & (query_losses.blocked_claims() | plan_losses.blocked_claims()))
        query_sufficient = (
            query_published
            and analysis_complete
            and not query_losses
            and not plan_losses
            and not necessary_blocked
        )
        if not query_sufficient:
            all_sufficient = False
        if not query_published:
            all_published = False
        queries.append(
            _exact(
                _QUERY_KEYS,
                {
                    "query_id": query_id,
                    "query_type": private.get("query_type"),
                    "status": "ok" if private.get("status") == "ok" and plan_error is None else "error",
                    "error": None
                    if private.get("status") == "ok" and plan_error is None
                    else {"code": _error_code(private.get("error_code")) or plan_error or "execution_failed"},
                    "selection": _selection(private, table_slots),
                    "population": _population(private.get("population"), private.get("body") or {})
                    if private.get("status") == "ok"
                    else None,
                    "page": page,
                    "publication_complete": query_published,
                    "evidence_sufficient": query_sufficient,
                    "allowed_claims": claims,
                    "losses": query_losses.export(),
                    "records": delivered,
                },
            )
        )
    omitted = produced - len(selected)
    if omitted and "evidence_omitted" not in {item["code"] for item in plan_losses.export()} and not context_omitted:
        plan_losses.add("evidence_omitted", omitted)
    publication_complete = analysis_complete and all_published and omitted == 0 and plan_error is None
    evidence_sufficient = publication_complete and all_sufficient and omitted == 0
    any_ok = plan_error is None and any(item.get("status") == "ok" for item in private_queries)
    if plan_error:
        status = "error"
        envelope_error = plan_error
    elif not selected and (context_omitted or ref_omitted):
        status = "error"
        envelope_error = "context_limit_exceeded"
    elif not any_ok:
        status = "error"
        first_error = next((item.get("error_code") for item in private_queries), None)
        envelope_error = _error_code(first_error) or "execution_failed"
    elif evidence_sufficient:
        status = "ok"
        envelope_error = None
    else:
        status = "partial"
        envelope_error = None
    return {
        "publication_version": PUBLICATION_VERSION,
        "contract_version": CONTRACT_VERSION,
        "status": status,
        "analysis_complete": analysis_complete,
        "publication_complete": publication_complete,
        "evidence_sufficient": evidence_sufficient,
        "operation_id": operation_id,
        "comparison_id": scope.comparison_id,
        "expected_result_version": scope.expected_result_version,
        "catalog_version": CATALOG_VERSION,
        "population": {"examined_rows": examined_rows, "query_count": len(private_queries)},
        "queries": queries,
        "evidence_produced_count": produced,
        "evidence_delivered_count": len(selected),
        "evidence_omitted_count": omitted,
        "losses": plan_losses.export(),
        "error": None if envelope_error is None else {"code": envelope_error},
    }


def _seal_query_records(records, *, operation_id: str, scope: AuthorizedComparisonScope) -> list[dict[str, Any]]:
    sealed = []
    for ordinal, record in enumerate(records):
        body = {
            "publication_version": PUBLICATION_VERSION,
            "schema_id": record["schema_id"],
            "contract_version": CONTRACT_VERSION,
            "operation_id": operation_id,
            "comparison_id": scope.comparison_id,
            "expected_result_version": scope.expected_result_version,
            "memory_checksum": scope.memory_checksum,
            "memory_schema_version": scope.memory_schema_version,
            "catalog_version": CATALOG_VERSION,
            "query_id": record["query_id"],
            "query_type": record["query_type"],
            "record_role": record["record_role"],
            "ordinal": ordinal,
            "subject": copy.deepcopy(record["subject"]),
            "population": copy.deepcopy(record["population"]),
            "page": copy.deepcopy(record["page"]),
            "facts": copy.deepcopy(record["facts"]),
        }
        digest = artifact_checksum(body)
        evidence_id = str(
            uuid5(
                EVIDENCE_NAMESPACE,
                "|".join(
                    (
                        PUBLICATION_VERSION,
                        operation_id,
                        scope.comparison_id,
                        scope.expected_result_version,
                        str(record["schema_id"]),
                        str(record["query_id"]),
                        str(ordinal),
                        digest,
                    )
                ),
            )
        )
        try:
            reference = EvidenceReference(
                evidence_id=evidence_id,
                evidence_digest=digest,
                operation_id=operation_id,
                comparison_id=scope.comparison_id,
                expected_result_version=scope.expected_result_version,
                contract_version=CONTRACT_VERSION,
                memory_checksum=scope.memory_checksum,
                memory_schema_version=scope.memory_schema_version,
            )
        except ChatContractError as exc:
            raise QueryCatalogError("invalid_evidence_binding") from exc
        body["evidence_id"] = reference.evidence_id
        body["evidence_digest"] = reference.evidence_digest
        sealed.append(body)
    return sealed


def _cents(value: Any) -> int | None:
    if not isinstance(value, str) or not _money_text(value):
        return None
    whole, frac = value.split(".", 1)
    return int(whole) * 100 + int(frac)


def _money_ready(text: Any, state: Any) -> bool:
    cents = _cents(text)
    if state not in {"present", "zero"} or cents is None:
        return False
    return (state == "zero") == (cents == 0)


def _component_fields_conflict(state: Any, availability: Any, source: Any) -> bool:
    if source == "none" and (state in {"present", "zero"} or availability in _VALUE_AVAILABILITY):
        return True
    if state in {"present", "zero"} and availability not in _VALUE_AVAILABILITY:
        return True
    if availability in _VALUE_AVAILABILITY and state not in {"present", "zero"}:
        return True
    return False


def _component_claim(component: Mapping[str, Any]) -> tuple[bool, bool]:
    state = component.get("amount_state")
    availability = component.get("availability")
    source = component.get("source")
    if _component_fields_conflict(state, availability, source):
        return False, False
    ready = (
        _money_ready(component.get("amount_text"), state)
        and availability in _VALUE_AVAILABILITY
        and source in _PROVENANCE_SOURCES
        and component.get("numeric_support") != "unsupported_decimal"
    )
    return ready, ready


def _unique_lowest_slot(carriers: Sequence[Any], text_key: str, state_key: str) -> int | None:
    priced: list[tuple[int, int]] = []
    if len(carriers) < 2:
        return None
    for item in carriers:
        if not isinstance(item, Mapping):
            return None
        if not _money_ready(item.get(text_key), item.get(state_key)):
            return None
        slot = item.get("carrier_slot")
        cents = _cents(item.get(text_key))
        if type(slot) is not int or cents is None:
            return None
        priced.append((cents, slot))
    priced.sort()
    if priced[0][0] == priced[1][0]:
        return None
    return priced[0][1]


def _wins_agree(carriers: Sequence[Mapping[str, Any]], winner: int) -> bool:
    values = [item.get("wins") for item in carriers]
    if all(value is None for value in values):
        return True
    if any(type(value) is not int for value in values):
        return False
    ranked = sorted(((int(item.get("wins")), item.get("carrier_slot")) for item in carriers), key=lambda item: (-item[0], item[1]))
    if ranked[0][0] <= 0 or ranked[0][0] == ranked[1][0]:
        return True
    return ranked[0][1] == winner


def _decision_counts(summary: Mapping[str, Any]) -> bool:
    comparable = summary.get("comparable_row_count")
    decisive = summary.get("decisive_row_count")
    ties = summary.get("tie_count")
    return (
        type(comparable) is int
        and type(decisive) is int
        and type(ties) is int
        and comparable > 0
        and decisive > 0
        and decisive + ties == comparable
    )


def _bounded_official_summary(summary: Mapping[str, Any]) -> bool:
    if summary.get("tie_rule") != _TIE_RULE or not _decision_counts(summary):
        return False
    if summary.get("savings_universe") not in PUBLIC_UNIVERSES:
        return False
    if summary.get("differences_universe") != summary.get("savings_universe"):
        return False
    carriers = summary.get("carriers") or []
    winner = _unique_lowest_slot(carriers, "comparable_freight_text", "comparable_freight_state")
    if winner is None or not _wins_agree(carriers, winner):
        return False
    return summary.get("lead_carrier_slot") == winner and type(summary.get("lead_wins")) is int and summary.get("lead_wins") > 0


def _bounded_carrier(summary: Mapping[str, Any]) -> bool:
    if summary.get("global_superiority") is not False or summary.get("tie_rule") != _TIE_RULE:
        return False
    if summary.get("universe") not in PUBLIC_UNIVERSES or not _decision_counts(summary):
        return False
    carriers = summary.get("carriers") or []
    winner = _unique_lowest_slot(carriers, "comparable_freight_text", "comparable_freight_state")
    return winner is not None and _wins_agree(carriers, winner)


def _savings_ready(summary: Mapping[str, Any], universe_key: str) -> bool:
    comparable = summary.get("comparable_row_count")
    return (
        summary.get(universe_key) in PUBLIC_UNIVERSES
        and type(comparable) is int
        and comparable > 0
        and _money_ready(summary.get("potential_savings_text"), summary.get("potential_savings_state"))
    )


def _aggregate_total_ready(summary: Mapping[str, Any]) -> bool:
    if summary.get("carrier_total_universe") != _CARRIER_TOTAL_UNIVERSE or type(summary.get("total_rows")) is not int:
        return False
    ready = False
    for carrier in summary.get("carriers") or []:
        if not isinstance(carrier, Mapping):
            continue
        if not _money_ready(carrier.get("official_freight_text"), carrier.get("official_freight_state")):
            continue
        population = carrier.get("population_rows")
        complete = carrier.get("complete_rows")
        if type(population) is not int or type(complete) is not int:
            continue
        if population <= 0 or complete < 0 or complete > population:
            continue
        if carrier.get("official_freight_state") == "present" and complete == 0:
            continue
        ready = True
    return ready


def _official_coverage_percentage(calculated: int, total: int) -> str | None:
    """Mesma definição de ``_pct``: parte / total × 100, uma casa.

    Total zero não tem percentual oficial. O retorno vazio só recusa o claim.
    """

    if total <= 0:
        return None
    rounded = round((float(calculated) / float(total)) * 100.0, 1)
    return _percentage(rounded)


def _coverage_ready(summary: Mapping[str, Any]) -> bool:
    total = summary.get("total_cells")
    calculated = summary.get("calculated_cells")
    errors = summary.get("error_cells")
    published = summary.get("calculation_coverage_percentage")
    if type(total) is not int or type(calculated) is not int or type(errors) is not int:
        return False
    if total <= 0 or calculated < 0 or errors < 0 or calculated > total:
        return False
    if calculated + errors != total:
        return False
    expected = _official_coverage_percentage(calculated, total)
    return isinstance(published, str) and expected is not None and published == expected


def _line_total_ready(record: Mapping[str, Any], summary: Mapping[str, Any] | None) -> bool:
    facts = record["facts"]
    if not isinstance(summary, Mapping) or type(summary.get("matched_rows")) is not int or summary.get("matched_rows") <= 0:
        return False
    if facts.get("completeness") != "complete" or facts.get("amount_support") != "official_numeric":
        return False
    if not _money_ready(facts.get("final_amount_text"), facts.get("final_amount_state")):
        return False
    total = next((item for item in facts.get("components") or [] if item.get("code") == "total"), None)
    if not isinstance(total, Mapping):
        return False
    return _money_ready(total.get("amount_text"), total.get("amount_state")) and total.get("amount_text") == facts.get("final_amount_text")


def _partial_component_ready(facts: Mapping[str, Any]) -> bool:
    return (
        facts.get("completeness") == "partial"
        and facts.get("amount_support") == "official_numeric"
        and facts.get("final_amount_state") == "absent"
        and _money_ready(facts.get("partial_amount_text"), facts.get("partial_amount_state"))
    )


def _partial_cell_ready(cell: Mapping[str, Any]) -> bool:
    return (
        cell.get("completeness") == "partial"
        and cell.get("final_amount_state") == "absent"
        and type(cell.get("carrier_slot")) is int
        and _money_ready(cell.get("partial_amount_text"), cell.get("partial_amount_state"))
    )


def _bucket_ready(bucket: Mapping[str, Any]) -> bool:
    if bucket.get("code") not in PUBLIC_BUCKET_CODES or bucket.get("availability") not in PUBLIC_AVAILABILITY:
        return False
    return _money_ready(bucket.get("amount_text"), bucket.get("amount_state"))


def _gap_matches(facts: Mapping[str, Any]) -> bool:
    freights = facts.get("freights") or []
    if len(freights) < 2 or not _money_ready(facts.get("absolute_gap_text"), facts.get("absolute_gap_state")):
        return False
    amounts: list[int] = []
    slots: list[int] = []
    for freight in freights:
        if not isinstance(freight, Mapping) or not _money_ready(freight.get("amount_text"), freight.get("amount_state")):
            return False
        slot = freight.get("carrier_slot")
        cents = _cents(freight.get("amount_text"))
        if type(slot) is not int or cents is None or slot in slots:
            return False
        slots.append(slot)
        amounts.append(cents)
    amounts.sort()
    gap = _cents(facts.get("absolute_gap_text"))
    if gap is None or gap != amounts[1] - amounts[0]:
        return False
    tied = gap == 0
    return facts.get("tie") is tied


def _page_ranking_ready(records: Sequence[Mapping[str, Any]], summary: Mapping[str, Any]) -> bool:
    if summary.get("difference_definition") != _GAP_DEFINITION:
        return False
    details = [item for item in records if item.get("record_role") == "detail"]
    if not details:
        return False
    page = details[0].get("page")
    if not isinstance(page, Mapping) or page.get("sort") != _RANK_SORT or page.get("returned") != len(details):
        return False
    if type(page.get("offset")) is not int or type(page.get("limit")) is not int or type(page.get("total")) is not int:
        return False
    previous: tuple[int, int] | None = None
    for item in details:
        if item.get("page") != page:
            return False
        facts = item.get("facts")
        if not isinstance(facts, Mapping) or type(facts.get("row_index")) is not int or not _gap_matches(facts):
            return False
        gap = _cents(facts.get("absolute_gap_text"))
        if gap is None:
            return False
        key = (-gap, int(facts["row_index"]))
        if previous is not None and key < previous:
            return False
        previous = key
    return True


def _geography_ready(facts: Mapping[str, Any], summary: Mapping[str, Any] | None) -> bool:
    if not isinstance(summary, Mapping):
        return False
    return (
        summary.get("aligned") is True
        and summary.get("alignment_reason") == _ALIGNED_GEOGRAPHY
        and facts.get("group_id") in OFFICIAL_UF_CODES
        and type(facts.get("row_count")) is int
    )


def _static_band_money(bound: str) -> tuple[str, str]:
    whole = int(bound)
    return f"{whole}.00", "zero" if whole == 0 else "present"


def _published_band_matches_official(facts: Mapping[str, Any]) -> bool:
    """Confere piso, teto e inclusividade com ``weight_bands_v1``. Não reclassifica peso."""

    official = _OFFICIAL_WEIGHT_BANDS.get(facts.get("group_id"))
    if official is None or facts.get("lower_inclusive") is not True:
        return False
    lower, upper = official
    lower_text, lower_state = _static_band_money(lower)
    if facts.get("lower_text") != lower_text or facts.get("lower_state") != lower_state:
        return False
    last = upper is None
    if facts.get("last_band") is not last:
        return False
    if last:
        return (
            facts.get("upper_text") is None
            and facts.get("upper_state") == "absent"
            and facts.get("upper_exclusive") is False
        )
    upper_text, upper_state = _static_band_money(upper)
    return (
        facts.get("upper_text") == upper_text
        and facts.get("upper_state") == upper_state
        and facts.get("upper_exclusive") is True
    )


def _weight_band_ready(facts: Mapping[str, Any], summary: Mapping[str, Any] | None) -> bool:
    if not isinstance(summary, Mapping):
        return False
    return (
        summary.get("bands_version") == WEIGHT_BANDS_VERSION
        and summary.get("last_band_rule") == _LAST_BAND_RULE
        and type(summary.get("numeric_weight_rows")) is int
        and facts.get("group_id") in _BAND_IDS
        and type(facts.get("row_count")) is int
        and _published_band_matches_official(facts)
    )


def _group_savings_ready(facts: Mapping[str, Any]) -> bool:
    comparable = facts.get("comparable_row_count")
    return (
        facts.get("monetary_values_published") is True
        and type(facts.get("row_count")) is int
        and type(comparable) is int
        and comparable > 0
        and _money_ready(facts.get("potential_savings_text"), facts.get("potential_savings_state"))
    )


def _diagnostic_code_ready(item: Mapping[str, Any]) -> bool:
    meaning = item.get("preserved_meaning")
    return (
        item.get("code") in OFFICIAL_DIAGNOSTIC_CODES
        and isinstance(meaning, str)
        and bool(meaning)
        and isinstance(item.get("must_not_assert"), list)
        and type(item.get("cell_count")) is int
        and type(item.get("row_count")) is int
    )


def _diagnostic_cell_ready(cell: Mapping[str, Any]) -> bool:
    meaning = cell.get("preserved_meaning")
    return (
        cell.get("code_state") == "official"
        and cell.get("official_code") in OFFICIAL_DIAGNOSTIC_CODES
        and isinstance(meaning, str)
        and bool(meaning)
        and isinstance(cell.get("must_not_assert"), list)
        and type(cell.get("carrier_slot")) is int
    )


def _diagnostic_population_ready(summary: Mapping[str, Any]) -> bool:
    codes = summary.get("by_code")
    if not isinstance(codes, list) or type(summary.get("unmapped_code_occurrences")) is not int:
        return False
    return all(
        isinstance(item, Mapping) and type(item.get("cell_count")) is int and type(item.get("row_count")) is int
        for item in codes
    )


def _authorize_claims(supported: set[str], *books: _LossBook) -> list[str]:
    """Une os bloqueios e só então retira claims. A ordem dos livros não muda o resultado."""

    blocked: set[str] = set()
    for book in books:
        blocked |= book.blocked_claims()
    return sorted(claim for claim in supported if claim not in blocked)


def _claims(query_type: str | None, records: list[dict[str, Any]]) -> set[str]:
    """Premissas do que os registros publicados sustentam, antes das perdas."""

    granted: set[str] = set()
    summary = next((item["facts"] for item in records if item["record_role"] == "summary"), None)
    if query_type == "official_summary" and isinstance(summary, Mapping):
        if type(summary.get("total_rows")) is int and summary.get("population_universe") in PUBLIC_UNIVERSES:
            granted.add("population_count")
        if _bounded_official_summary(summary):
            granted.add("bounded_best_freight")
        if _savings_ready(summary, "savings_universe") and summary.get("differences_universe") == summary.get("savings_universe"):
            granted.add("potential_savings")
        if _aggregate_total_ready(summary):
            granted.add("aggregate_total")
        if _coverage_ready(summary):
            granted.add("coverage")
    if query_type == "freight_components":
        if isinstance(summary, Mapping) and type(summary.get("matched_rows")) is int:
            granted.add("population_count")
        for record in records:
            if record["record_role"] != "detail":
                continue
            for component in record["facts"].get("components") or []:
                value_ok, provenance_ok = _component_claim(component)
                if value_ok:
                    granted.add("component_value")
                if provenance_ok:
                    granted.add("component_provenance")
            if _line_total_ready(record, summary):
                granted.add("aggregate_total")
            if _partial_component_ready(record["facts"]):
                granted.add("partial_freight")
            if any(_bucket_ready(bucket) for bucket in record["facts"].get("buckets") or []):
                granted.add("undifferentiated_bucket")
    if query_type == "carrier_comparison" and isinstance(summary, Mapping):
        if _bounded_carrier(summary):
            granted.add("bounded_best_freight")
        if _savings_ready(summary, "universe"):
            granted.add("potential_savings")
        if type(summary.get("comparable_row_count")) is int and summary.get("universe") in PUBLIC_UNIVERSES:
            granted.add("population_count")
    if query_type == "top_differences" and isinstance(summary, Mapping):
        if (
            _money_ready(summary.get("gap_total_text"), summary.get("gap_total_state"))
            and type(summary.get("ranked_rows")) is int
            and summary.get("ranked_rows") > 0
            and summary.get("difference_definition") == _GAP_DEFINITION
        ):
            granted.add("potential_savings")
        if _page_ranking_ready(records, summary):
            granted.add("page_ranking")
        if type(summary.get("ranked_rows")) is int:
            granted.add("population_count")
    if query_type in {"by_uf", "by_weight"}:
        for record in records:
            if record["record_role"] not in {"detail", "special_group"}:
                continue
            facts = record["facts"]
            if query_type == "by_uf" and _geography_ready(facts, summary):
                granted.add("geography_group")
            if query_type == "by_weight" and _weight_band_ready(facts, summary):
                granted.add("weight_band_population")
            if _group_savings_ready(facts):
                granted.add("potential_savings")
        if isinstance(summary, Mapping) and type(summary.get("examined_rows")) is int:
            granted.add("population_count")
    if query_type == "incomplete_freights":
        for record in records:
            if record["record_role"] != "detail":
                continue
            if any(_partial_cell_ready(cell) for cell in record["facts"].get("cells") or []):
                granted.add("partial_freight")
        if isinstance(summary, Mapping) and type(summary.get("partial_rows")) is int:
            granted.add("population_count")
    if query_type == "non_calculation_diagnostics":
        for record in records:
            facts = record["facts"]
            if record["record_role"] == "summary" and any(_diagnostic_code_ready(item) for item in facts.get("by_code") or []):
                granted.add("official_diagnostic")
            if any(_diagnostic_cell_ready(cell) for cell in facts.get("cells") or []):
                granted.add("official_diagnostic")
        if isinstance(summary, Mapping) and _diagnostic_population_ready(summary):
            granted.add("population_count")
    return granted


def _selection(private: Mapping[str, Any], table_slots: Mapping[str, int]) -> dict[str, Any]:
    filters = private.get("filters") if isinstance(private.get("filters"), Mapping) else {}
    slots = []
    for table_id in filters.get("table_ids") or []:
        slot = _slot_from_map(table_id, table_slots)
        if slot is not None and slot not in slots:
            slots.append(slot)
    gap_text, gap_state = _money(filters.get("minimum_gap")) if "minimum_gap" in filters else (None, "absent")
    return _exact(
        _SELECTION_KEYS,
        {
            "carrier_slots": slots or None,
            "row_indexes": [item for item in filters.get("row_indexes") or [] if type(item) is int] or None,
            "component_codes": [item for item in filters.get("component_codes") or [] if item in COMPONENT_CODES] or None,
            "ufs": [item for item in filters.get("ufs") or [] if item in OFFICIAL_UF_CODES] or None,
            "diagnostic_codes": [item for item in filters.get("codes") or [] if item in OFFICIAL_DIAGNOSTIC_CODES] or None,
            "minimum_gap_text": gap_text,
            "minimum_gap_state": gap_state,
            "include_absent": filters.get("include_absent") if type(filters.get("include_absent")) is bool else None,
            "include_unusable": filters.get("include_unusable") if type(filters.get("include_unusable")) is bool else None,
            "bands_version": filters.get("bands_version") if filters.get("bands_version") == WEIGHT_BANDS_VERSION else None,
        },
    )


def _population(private_population: Any, body: Mapping[str, Any]) -> dict[str, Any]:
    source = private_population if isinstance(private_population, Mapping) else {}
    universe = _token(source.get("universe"), PUBLIC_UNIVERSES)
    return _exact(
        _POPULATION_KEYS,
        {
            "examined_rows": _plain_int(source.get("total_rows")),
            "eligible_rows": _plain_int(source.get("eligible_rows")),
            "excluded_rows": _plain_int(source.get("excluded_rows")),
            "universe": universe,
            "total_cells": _plain_int(body.get("population", {}).get("total_cells"))
            if isinstance(body.get("population"), Mapping)
            else None,
            "comparable_rows": _plain_int(source.get("comparable_rows")),
            "numeric_weight_rows": _plain_int(source.get("numeric_weight_rows")),
            "complete_freight_rows": _plain_int(source.get("complete_freight_rows")),
            "complete_freight_count": _plain_int(source.get("complete_freight_count")),
            "excluded_invalid_or_absent_weight": _plain_int(source.get("excluded_invalid_or_absent_weight")),
            "excluded_unsupported_decimal_weight": _plain_int(source.get("excluded_unsupported_decimal_weight")),
            "excluded_not_comparable_rows": _plain_int(source.get("excluded_not_comparable_rows")),
            "excluded_weight_reference": _token(source.get("excluded_rows_reference"), _REFERENCE_TOKENS),
            "excluded_comparability_reference": None,
        },
    )


def _page(page: Any) -> dict[str, Any] | None:
    if not isinstance(page, Mapping):
        return None
    sort = page.get("sort") if page.get("sort") in _SORTS else None
    offset = _plain_int(page.get("offset"))
    limit = _plain_int(page.get("limit"))
    total = _plain_int(page.get("total"))
    returned = _plain_int(page.get("returned"))
    if None in {offset, limit, total, returned}:
        return None
    return _exact(
        _PAGE_KEYS,
        {"offset": offset, "limit": limit, "total": total, "returned": returned, "sort": sort},
    )


def _unsigned(role: str, subject: dict[str, Any], facts: dict[str, Any], losses: _LossBook) -> dict[str, Any]:
    if role not in PUBLIC_RECORD_ROLES:
        raise _SchemaGap((role,))
    return {"record_role": role, "subject": subject, "facts": facts, "losses": losses}


def _subject(kind: str, *, row_index: int | None = None, carrier_slot: int | None = None, group_id: str | None = None):
    if kind not in PUBLIC_SUBJECT_KINDS:
        raise _SchemaGap((kind,))
    return _exact(
        _SUBJECT_KEYS,
        {"kind": kind, "row_index": row_index, "carrier_slot": carrier_slot, "group_id": group_id},
    )


def _wanted(query: Mapping[str, Any]):
    fields = query.get("requested_fields")

    def allow(*names: str) -> bool:
        if fields is None:
            return True
        return any(name in fields for name in names)

    return allow


def _money(value: Any) -> tuple[str | None, str]:
    """Copia quantia já fechada. Não arredonda de novo."""

    if value is None:
        return None, "absent"
    if isinstance(value, str):
        if not _money_text(value):
            return None, "unavailable"
        return value, "zero" if value == "0.00" else "present"
    if type(value) is bool:
        return None, "unavailable"
    if type(value) is int:
        if value < 0:
            return None, "unavailable"
        text = f"{value}.00"
        return text, "zero" if value == 0 else "present"
    if type(value) is float:
        if value < 0 or value != value or value in {float("inf"), float("-inf")}:
            return None, "unavailable"
        cents = value * 100.0
        nearest = round(cents)
        if abs(cents - nearest) > 1e-6:
            return None, "unavailable"
        cents_int = int(nearest)
        text = f"{cents_int // 100}.{abs(cents_int) % 100:02d}"
        return text, "zero" if cents_int == 0 else "present"
    return None, "unavailable"


def _money_text(value: str) -> bool:
    if not 4 <= len(value) <= 24 or value.count(".") != 1:
        return False
    whole, frac = value.split(".", 1)
    return bool(whole) and len(frac) == 2 and whole.isdigit() and frac.isdigit()


def _percentage(value: Any) -> str | None:
    if value is None:
        return None
    if type(value) is int and not isinstance(value, bool):
        if value < 0:
            return None
        return f"{value}.0"
    if type(value) is float:
        if value < 0 or value != value or value in {float("inf"), float("-inf")}:
            return None
        tenths = value * 10.0
        nearest = round(tenths)
        if abs(tenths - nearest) > 1e-4:
            return None
        tenths_int = int(nearest)
        return f"{tenths_int // 10}.{tenths_int % 10}"
    return None


def _plain_int(value: Any) -> int | None:
    if type(value) is int:
        return value
    return None


def _token(value: Any, allowed: frozenset[str]) -> str | None:
    if isinstance(value, str) and value in allowed:
        return value
    return None


def _slot_of(item: Any) -> int | None:
    if not isinstance(item, Mapping):
        return None
    slot = item.get("slot_number")
    if type(slot) is int and slot >= 1:
        return slot
    return None


def _slot_from_map(table_id: Any, table_slots: Mapping[str, int]) -> int | None:
    if not isinstance(table_id, str):
        return None
    slot = table_slots.get(table_id)
    if type(slot) is int and slot >= 1:
        return slot
    return None


def _error_code(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value in PUBLIC_ERROR_CODES:
        return value
    return "execution_failed"


def _exact(first: Any, second: Any) -> dict[str, Any]:
    if isinstance(first, frozenset):
        keys, payload = first, second
    else:
        payload, keys = first, second
    if set(payload) != set(keys):
        raise _SchemaGap((tuple(sorted(set(keys) - set(payload))), tuple(sorted(set(payload) - set(keys)))))
    return dict(payload)


def _exact_population_plan(payload: Mapping[str, Any]) -> None:
    _exact(payload, _PLAN_POPULATION_KEYS)


def _check_record(record: Mapping[str, Any]) -> None:
    _exact(record, _RECORD_KEYS)
    if record["schema_id"] not in _SUMMARY_FACTS:
        raise _SchemaGap((record["schema_id"],))
    if record["record_role"] not in PUBLIC_RECORD_ROLES:
        raise _SchemaGap((record["record_role"],))
    _exact(record["subject"], _SUBJECT_KEYS)
    _exact(record["population"], _POPULATION_KEYS)
    if record["page"] is not None:
        _exact(record["page"], _PAGE_KEYS)
    facts = record["facts"]
    role = record["record_role"]
    schema_id = record["schema_id"]
    if role == "summary":
        _exact(facts, _SUMMARY_FACTS[schema_id])
    elif role == "official_geography":
        _exact(facts, _OFFICIAL_GEO_FACTS)
    elif schema_id in {"by_uf", "by_weight"}:
        _exact(facts, _GROUP_FACTS)
    else:
        _exact(facts, _DETAIL_FACTS[schema_id])
    _check_nested(schema_id, role, facts)


def _check_nested(schema_id: str, role: str, facts: Mapping[str, Any]) -> None:
    if schema_id in {"official_summary", "carrier_comparison"} and role == "summary":
        for item in facts.get("carriers") or []:
            _exact(item, _CARRIER_KEYS)
    if schema_id == "freight_components" and role == "summary":
        for item in facts.get("availability_counts") or []:
            _exact(item, _COUNT_KEYS)
    if schema_id == "freight_components" and role == "detail":
        for item in facts.get("components") or []:
            _exact(item, _COMPONENT_KEYS)
        for item in facts.get("buckets") or []:
            _exact(item, _BUCKET_KEYS)
        for item in facts.get("canonical_accessorial_counts") or []:
            _exact(item, _CANONICAL_COUNT_KEYS)
    if schema_id == "top_differences" and role == "detail":
        for item in facts.get("freights") or []:
            _exact(item, _FREIGHT_KEYS)
    if schema_id == "by_uf" and role == "summary":
        for item in facts.get("group_counts") or []:
            _exact(item, _GROUP_COUNT_KEYS)
    if schema_id == "by_weight" and role == "summary":
        for item in facts.get("band_counts") or []:
            _exact(item, _GROUP_COUNT_KEYS)
    if schema_id in {"by_uf", "by_weight"} and role in {"detail", "special_group"}:
        for item in facts.get("carriers") or []:
            _exact(item, _GEO_CARRIER_KEYS)
    if schema_id == "by_uf" and role == "official_geography":
        for item in facts.get("groups") or []:
            _exact(item, _OFFICIAL_UF_KEYS)
            for carrier in item.get("carriers") or []:
                _exact(carrier, _GEO_CARRIER_KEYS)
    if schema_id == "non_calculation_diagnostics" and role == "summary":
        for item in facts.get("by_code") or []:
            _exact(item, _DIAGNOSTIC_CODE_KEYS)
    if schema_id == "non_calculation_diagnostics" and role == "detail":
        for item in facts.get("cells") or []:
            _exact(item, _DIAGNOSTIC_CELL_KEYS)
    if schema_id == "incomplete_freights" and role == "detail":
        for item in facts.get("cells") or []:
            _exact(item, _PARTIAL_CELL_KEYS)


def _reject_forbidden_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in _FORBIDDEN_PUBLIC_KEYS:
                raise _SchemaGap((key,))
            _reject_forbidden_keys(item)
        return
    if isinstance(value, list):
        for item in value:
            _reject_forbidden_keys(item)


def _canonical(value: Any) -> str:
    try:
        return __import__("json").dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise QueryCatalogError("invalid_artifact") from exc
