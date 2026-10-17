"""Executor analítico somente leitura do chat do Agente Compara.

SCRUM-245. Opera sobre uma cópia privada da projeção já autorizada. Não escolhe
conta, usuário, conexão nem arquivo. Não recalcula frete, não grava resultado,
não limpa armazenamento e não chama modelo.

A leitura da execução não volta aos dicionários do chamador. Analytics só entram
como oficiais quando o serviço puro de analytics os reproduz a partir do
resultado autorizado. Checksum de estabilidade não substitui essa proveniência.

O campo numérico ``gris`` do extrator oficial mistura GRIS, Ad Valorem e risco.
Ele entra só como agregado sem detalhe. GRIS identificável exige código
canônico ``gris`` ou ``risk_management``. Ad Valorem exige ``ad_valorem``.
Rótulo livre não classifica componente.
"""
from __future__ import annotations

import copy
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.agente_compara_chat_contracts import AuthorizedComparisonScope, canonical_comparison_id
from app.agente_compara_chat_evidence_adapter import publication_failure, publish_analytical_execution
from app.agente_compara_chat_query_catalog import (
    CANONICAL_COMPONENT_CODES,
    CATALOG_VERSION,
    COMPONENT_CODES,
    CURSOR_CONTRACT,
    DIAGNOSTIC_CONSTRAINTS,
    OFFICIAL_DIAGNOSTIC_CODES,
    OFFICIAL_UF_CODES,
    SORT_ROW_INDEX_ASC,
    SORT_UF_ASC,
    WEIGHT_BANDS_V1,
    WEIGHT_BANDS_VERSION,
    QueryCatalogError,
    QueryExecutionLimits,
    ValidatedQuery,
    artifact_checksum,
    validate_query_plan,
)

_COMPLETE_STATUSES = frozenset({"calculated", "calculated_with_warnings"})
_TOKEN_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")
_MONEY = Decimal("0.01")


@dataclass(frozen=True)
class AuthorizedAnalyticalProjection:
    """Cópia imutável, para o executor, do resultado oficial já autorizado."""

    operation_id: str
    scope: AuthorizedComparisonScope
    result: dict[str, Any]
    memory_payload: dict[str, Any] | None = None
    official_analytics: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        failure = None
        unexpected = False
        try:
            _authorize_projection(self)
        except QueryCatalogError as exc:
            failure = (exc.code, exc.field)
        except Exception:
            unexpected = True
        if unexpected:
            raise QueryCatalogError("execution_failed")
        if failure is not None:
            raise QueryCatalogError(failure[0], field=failure[1])


def _authorize_projection(projection: AuthorizedAnalyticalProjection) -> None:
        operation_id = _canonical_operation_id(projection.operation_id)
        if not isinstance(projection.scope, AuthorizedComparisonScope):
            raise QueryCatalogError("snapshot_mismatch")
        if not isinstance(projection.result, Mapping):
            raise QueryCatalogError("invalid_projection")
        result = copy.deepcopy(dict(projection.result))
        _validate_result_shape(result, scope=projection.scope)
        memory = None
        if projection.memory_payload is not None:
            if not isinstance(projection.memory_payload, Mapping):
                raise QueryCatalogError("invalid_projection")
            memory = copy.deepcopy(dict(projection.memory_payload))
            _validate_memory_payload(memory, scope=projection.scope)
        elif projection.scope.memory_checksum is not None:
            raise QueryCatalogError("snapshot_mismatch")
        analytics = None
        analytics_checksum = None
        if projection.official_analytics is not None:
            if not isinstance(projection.official_analytics, Mapping):
                raise QueryCatalogError("invalid_projection")
            supplied = copy.deepcopy(dict(projection.official_analytics))
            _validate_official_analytics(supplied, result=result, scope=projection.scope)
            trusted = _trusted_analytics(result)
            try:
                if artifact_checksum(supplied) != artifact_checksum(trusted):
                    raise QueryCatalogError(
                        "analytics_provenance_mismatch",
                        field="official_analytics",
                    )
                analytics_checksum = artifact_checksum(trusted)
            except QueryCatalogError as exc:
                if exc.code == "invalid_artifact":
                    raise QueryCatalogError("invalid_projection") from None
                raise
            analytics = trusted
        object.__setattr__(projection, "operation_id", operation_id)
        object.__setattr__(projection, "result", result)
        object.__setattr__(projection, "memory_payload", memory)
        object.__setattr__(projection, "official_analytics", analytics)
        object.__setattr__(projection, "_authorized_analytics_checksum", analytics_checksum)


def execute_analytical_plan(
    plan: Mapping[str, Any],
    projection: AuthorizedAnalyticalProjection,
    *,
    limits: QueryExecutionLimits | None = None,
    clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Executa o plano fechado. A projeção de entrada permanece intacta.

    Erro de validação continua tipado. Falha inesperada em qualquer etapa,
    inclusive antes das consultas, volta como envelope público sem a exceção.
    """

    if not isinstance(projection, AuthorizedAnalyticalProjection):
        raise QueryCatalogError("invalid_projection")
    active_limits = limits or QueryExecutionLimits()
    failure = None
    try:
        return _execute_analytical_plan(plan, projection, limits=active_limits, clock=clock)
    except QueryCatalogError as exc:
        failure = (exc.code, exc.field)
    except Exception:
        return _sealed_execution_failure(projection)
    raise QueryCatalogError(failure[0], field=failure[1])


def _sealed_execution_failure(projection: AuthorizedAnalyticalProjection) -> dict[str, Any]:
    """Envelope mínimo. Não lê a mensagem da falha nem devolve corpo analítico."""

    try:
        operation_id = projection.operation_id
        scope = projection.scope
    except Exception:
        raise QueryCatalogError("execution_failed") from None
    return publication_failure(operation_id=operation_id, scope=scope, examined_rows=0)


def _execute_analytical_plan(
    plan: Mapping[str, Any],
    projection: AuthorizedAnalyticalProjection,
    *,
    limits: QueryExecutionLimits | None = None,
    clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    active_limits = limits or QueryExecutionLimits()
    validated = validate_query_plan(plan, limits=active_limits)
    private = _capture_private_projection(projection)
    budget = _Budget(clock or time.monotonic, active_limits.max_processing_seconds)
    population = len(private.result["comparative_rows"])
    if population > active_limits.max_population:
        _assert_private_intact(private)
        return _publish_private_execution(
            private,
            examined_rows=population,
            table_slots={},
            private_queries=[],
            queries_complete=False,
            plan_error="population_limit_exceeded",
            limits=active_limits,
        )

    tables = _ordered_tables(private.result["tables"])
    table_slots = {table["table_id"]: int(table["slot_number"]) for table in tables}
    rows = [
        _resolve_row(row, tables, private)
        for row in private.result["comparative_rows"]
    ]
    analytics = _load_analytics(private, budget)
    private_queries: list[dict[str, Any]] = []
    for query in validated.queries:
        if budget.exceeded():
            private_queries.append(_query_error(query, "processing_limit_exceeded"))
            continue
        try:
            private_queries.append(
                _execute_query(
                    query,
                    rows=rows,
                    tables=tables,
                    analytics=analytics,
                    projection=private,
                    budget=budget,
                )
            )
        except QueryCatalogError:
            raise
        except Exception:
            private_queries.append(_query_error(query, "execution_failed"))
    _assert_private_intact(private)
    return _publish_private_execution(
        private,
        examined_rows=population,
        table_slots=table_slots,
        private_queries=private_queries,
        queries_complete=all(item["status"] == "ok" for item in private_queries),
        plan_error=None,
        limits=active_limits,
    )


def _execute_query(
    query: ValidatedQuery,
    *,
    rows: list[dict[str, Any]],
    tables: list[dict[str, Any]],
    analytics: Mapping[str, Any] | None,
    projection: AuthorizedAnalyticalProjection,
    budget: _Budget,
) -> dict[str, Any]:
    selected, error = _select_tables(tables, query.filters.get("table_ids"))
    if error:
        return _query_error(query, error)
    if query.query_type == "official_summary":
        body, population, facts = _official_summary(query, analytics, projection, len(rows))
    elif query.query_type == "freight_components":
        body, population, facts = _freight_components(query, rows, selected, projection, budget)
    elif query.query_type == "carrier_comparison":
        body, population, facts = _carrier_comparison(
            query, rows, selected, tables, analytics, projection, budget
        )
    elif query.query_type == "top_differences":
        body, population, facts = _top_differences(query, rows, selected, projection, budget)
    elif query.query_type == "by_uf":
        body, population, facts = _by_uf(query, rows, tables, analytics, projection, budget)
    elif query.query_type == "by_weight":
        body, population, facts = _by_weight(query, rows, tables, projection, budget)
    elif query.query_type == "incomplete_freights":
        body, population, facts = _incomplete_freights(query, rows, selected, projection, budget)
    else:
        body, population, facts = _diagnostics(query, rows, selected, projection, budget)
    del facts
    if body is None:
        return _query_error(query, population if isinstance(population, str) else "processing_limit_exceeded")
    page = body.pop("_page", None)
    return {
        "query_id": query.query_id,
        "query_type": query.query_type,
        "status": "ok",
        "error_code": None,
        "population": population,
        "page": page,
        "body": body,
        "requested_fields": query.fields,
        "filters": _public_filters(query.filters),
        "sort": query.sort,
    }


def _official_summary(query, analytics, projection, total_rows):
    if not isinstance(analytics, Mapping):
        return None, "official_analytics_unavailable", []
    per_table = []
    for item in analytics["tables"]:
        per_table.append(
            {
                "table_id": item["table_id"],
                "slot_number": item["slot_number"],
                "carrier_name": item["carrier_name"],
                "population_rows": item["row_count"],
                "complete_rows": item["calculated_rows"],
                "complete_with_warnings_rows": item["calculated_with_warnings_rows"],
                "partial_rows": item["incomplete_rows"],
                "not_calculated_rows": item["not_calculated_rows"],
                "coverage_percentage": item["coverage_percentage"],
                "official_freight_total": item["calculated_freight_total"],
                "universe": "complete_cells_of_this_table",
            }
        )
    global_summary = analytics["global_summary"]
    comparability = analytics["comparability"]
    body = {
        "population": {
            "total_rows": analytics["row_count"],
            "total_cells": global_summary["total_cells"],
            "document_count": global_summary["document_count"],
            "universe": "all_operational_rows",
        },
        "completeness": {
            "fully_comparable_rows": comparability["fully_comparable_rows"],
            "partially_comparable_rows": comparability["partially_comparable_rows"],
            "inconclusive_rows": comparability["inconclusive_rows"],
            "calculated_cells": global_summary["calculated_cells"],
            "error_cells": global_summary["error_cells"],
        },
        "coverage": {
            "calculation_coverage_percentage": global_summary["calculation_coverage_percentage"],
            "fully_comparable_percentage": comparability["fully_comparable_percentage"],
        },
        "official_values": {
            "per_table": per_table,
            "total_weight": global_summary["total_weight"],
            "total_invoice_value": global_summary["total_invoice_value"],
        },
        "official_differences": {
            "universe": "fully_comparable_rows",
            "competitive_summary": analytics["competitive_summary"],
            "carrier_competitiveness": analytics["carrier_competitiveness"],
        },
        "universes": {
            "population": "all_operational_rows",
            "per_table_totals": "complete_cells_of_each_table",
            "differences": "fully_comparable_rows",
        },
    }
    population = _population(total_rows, total_rows, "all_operational_rows")
    facts = [
        _fact(
            projection,
            query,
            population_total=total_rows,
            eligible_count=total_rows,
            comparable_count=comparability["fully_comparable_rows"],
            excluded_count=total_rows - comparability["fully_comparable_rows"],
            completeness="mixed",
            data={
                "fact_role": "summary",
                "universe": "all_operational_rows",
                "population": body["population"],
                "completeness": body["completeness"],
                "coverage": body["coverage"],
                "per_table": body["official_values"]["per_table"],
                "total_weight": body["official_values"]["total_weight"],
                "total_invoice_value": body["official_values"]["total_invoice_value"],
                "exclusions": {
                    "partially_comparable_rows": body["completeness"]["partially_comparable_rows"],
                    "inconclusive_rows": body["completeness"]["inconclusive_rows"],
                },
                "official_differences": body["official_differences"],
                "universes": body["universes"],
            },
        )
    ]
    return body, population, facts


def _freight_components(query, rows, tables, projection, budget):
    indexes = set(query.filters.get("row_indexes") or [])
    codes = query.filters.get("component_codes")
    matched_rows = []
    missing = []
    if indexes:
        present = {row["row_index"] for row in rows}
        missing = [index for index in query.filters["row_indexes"] if index not in present]
    flat = []
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        if budget.exceeded():
            return None, "processing_limit_exceeded", []
        if indexes and row["row_index"] not in indexes:
            continue
        matched_rows.append(row)
        for table in tables:
            cell = row["cells"].get(table["table_id"])
            flat.append(_component_record(row, table, cell, codes))
    for record in flat:
        for code, slot in record["components"].items():
            bucket = counts.setdefault(code, {})
            availability = slot["availability"]
            bucket[availability] = bucket.get(availability, 0) + 1
    page, page_items = _page(flat, query)
    body = {
        "rows": page_items,
        "availability_counts": counts,
        "missing_row_indexes": missing,
        "_page": page,
    }
    population = _population(len(rows), len(matched_rows), "filtered_rows")
    facts = _detail_facts(
        projection,
        query,
        page_items,
        population_total=len(rows),
        eligible_count=len(matched_rows),
        comparable_count=len(matched_rows),
        excluded_count=len(rows) - len(matched_rows),
        data={
            "fact_role": "summary",
            "matched_rows": len(matched_rows),
            "returned_rows": len(page_items),
            "missing_row_indexes": missing,
            "availability_counts": counts,
        },
        item_payload=_component_fact,
    )
    for record in page_items:
        record.pop("publication_notices", None)
    return body, population, facts


def _carrier_comparison(query, rows, selected, all_tables, analytics, projection, budget):
    full_selection = {table["table_id"] for table in selected} == {table["table_id"] for table in all_tables}
    if full_selection and isinstance(analytics, Mapping):
        comparability = analytics["comparability"]
        body = {
            "universe": "fully_comparable_rows",
            "indicator_source": "official_analytics",
            "carriers": analytics["carrier_competitiveness"],
            "differences": {
                "universe": "fully_comparable_rows",
                "competitive_summary": analytics["competitive_summary"],
            },
            "coverage": [
                {
                    "table_id": item["table_id"],
                    "complete_rows": item["calculated_rows"],
                    "partial_rows": item["incomplete_rows"],
                    "not_calculated_rows": item["not_calculated_rows"],
                    "coverage_percentage": item["coverage_percentage"],
                    "official_freight_total": item["calculated_freight_total"],
                    "universe": "complete_cells_of_this_table",
                }
                for item in analytics["tables"]
            ],
            "exclusions": {
                "partially_comparable_rows": comparability["partially_comparable_rows"],
                "inconclusive_rows": comparability["inconclusive_rows"],
            },
            "superiority_limit": _superiority_limit("fully_comparable_rows"),
        }
        comparable = comparability["fully_comparable_rows"]
        population = _population(len(rows), comparable, "fully_comparable_rows")
        facts = [
            _fact(
                projection,
                query,
                population_total=len(rows),
                eligible_count=comparable,
                comparable_count=comparable,
                excluded_count=len(rows) - comparable,
                completeness="mixed",
                data=_carrier_fact_data(body),
            )
        ]
        return body, population, facts

    stats = _comparable_stats(rows, selected, budget)
    if stats is None:
        return None, "processing_limit_exceeded", []
    body = {
        "universe": "selected_tables_fully_comparable",
        "indicator_source": "selected_tables_fully_comparable",
        "carriers": stats["carriers"],
        "differences": {
            "universe": "selected_tables_fully_comparable",
            "potential_savings": stats["potential_savings"],
            "decisive_row_count": stats["decisive_row_count"],
            "tie_count": stats["tie_count"],
            "definition": "second_lowest_minus_lowest_on_rows_where_every_selected_table_is_complete",
        },
        "coverage": stats["coverage"],
        "exclusions": stats["exclusions"],
        "superiority_limit": _superiority_limit("selected_tables_fully_comparable"),
    }
    population = _population(len(rows), stats["comparable_rows"], "selected_tables_fully_comparable")
    facts = [
        _fact(
            projection,
            query,
            population_total=len(rows),
            eligible_count=stats["comparable_rows"],
            comparable_count=stats["comparable_rows"],
            excluded_count=len(rows) - stats["comparable_rows"],
            completeness="mixed",
            data=_carrier_fact_data(body),
        )
    ]
    return body, population, facts


def _top_differences(query, rows, tables, projection, budget):
    table_ids = [table["table_id"] for table in tables]
    minimum = Decimal(query.filters["minimum_gap"]) if "minimum_gap" in query.filters else None
    ranked = []
    excluded = 0
    for row in rows:
        if budget.exceeded():
            return None, "processing_limit_exceeded", []
        evaluation = _row_gap(row, table_ids)
        if evaluation is None:
            excluded += 1
            continue
        if minimum is not None and evaluation["gap"] < minimum:
            excluded += 1
            continue
        ranked.append((evaluation["gap"], row["row_index"], row, evaluation))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    gap_total = sum((item[0] for item in ranked), Decimal("0.00")).quantize(_MONEY)
    ordered = [
        {
            "row_index": row["row_index"],
            "document_number": row["document_number"],
            "absolute_gap": _format_money(evaluation["gap"]),
            "tie": evaluation["tie"],
            "freights": evaluation["freights"],
        }
        for _, _, row, evaluation in ranked
    ]
    page, page_items = _page(ordered, query)
    body = {
        "ordered_differences": page_items,
        "gap_total": _format_money(gap_total),
        "sort": query.sort,
        "difference_definition": "second_lowest_minus_lowest_on_fully_comparable_rows",
        "_page": page,
    }
    population = _population(len(rows), len(ranked), "fully_comparable_rows")
    population["excluded_rows"] = excluded
    facts = _detail_facts(
        projection,
        query,
        page_items,
        population_total=len(rows),
        eligible_count=len(ranked),
        comparable_count=len(ranked),
        excluded_count=excluded,
        data={
            "fact_role": "summary",
            "gap_total": body["gap_total"],
            "ranked_rows": len(ranked),
            "difference_definition": body["difference_definition"],
        },
        item_payload=_gap_fact,
    )
    return body, population, facts


def _by_uf(query, rows, tables, analytics, projection, budget):
    selected_ufs = set(query.filters.get("ufs") or [])
    include_absent = query.filters.get("include_absent", True)
    include_unusable = query.filters.get("include_unusable", True)
    groups: dict[str, dict[str, Any]] = {}
    absent_rows: list[dict[str, Any]] = []
    unusable_rows: list[dict[str, Any]] = []
    for row in rows:
        if budget.exceeded():
            return None, "processing_limit_exceeded", []
        kind, code = _classify_uf(row.get("destination_uf"))
        if kind == "uf":
            groups.setdefault(code, _new_geo_bucket(tables))
            _accumulate_geo(groups[code], row, tables)
        elif kind == "absent":
            absent_rows.append(row)
        else:
            unusable_rows.append(row)
    filtered_out = 0
    ordered_codes: list[str] = []
    for code in sorted(groups):
        if selected_ufs and code not in selected_ufs:
            filtered_out += groups[code]["row_count"]
            continue
        ordered_codes.append(code)
    ordered = [
        _finish_geo(code, groups[code], tables, groups[code]["row_count"])
        for code in ordered_codes
    ]
    absent = _finish_special(absent_rows, tables, "uf_absent")
    unusable = _finish_special(unusable_rows, tables, "uf_unusable")
    page, page_items = _page(ordered, query)
    alignment = _geography_alignment(groups, absent_rows, unusable_rows, analytics)
    examined = len(rows)
    eligible = sum(groups[code]["row_count"] for code in ordered_codes)
    comparable = sum(groups[code]["comparable_row_count"] for code in ordered_codes)
    if include_absent:
        eligible += len(absent_rows)
        comparable += int(absent["comparable_row_count"])
    if include_unusable:
        eligible += len(unusable_rows)
        comparable += int(unusable["comparable_row_count"])
    excluded = examined - eligible
    if not selected_ufs and include_absent and include_unusable:
        universe = "all_operational_rows_by_preserved_uf"
    else:
        universe = "rows_matching_preserved_uf_filters"
    breakdown = {
        "fact_role": "summary",
        "examined_rows": examined,
        "eligible_rows": eligible,
        "excluded_rows": excluded,
        "comparable_rows": comparable,
        "filtered_uf_rows": filtered_out,
        "absent_rows": len(absent_rows),
        "unusable_rows": len(unusable_rows),
        "include_absent": include_absent,
        "include_unusable": include_unusable,
        "ufs": sorted(selected_ufs),
        "alignment": {
            "aligned_with_official_geography": alignment.get("aligned_with_official_geography"),
            "reason": alignment.get("reason"),
        },
        "page": {
            "offset": page["offset"],
            "limit": page["limit"],
            "total": page["total"],
            "returned": page["returned"],
        },
    }
    body = {
        "groups": page_items,
        "absent": _public_exclusion(absent, included=include_absent),
        "unusable": _public_exclusion(unusable, included=include_unusable),
        "exclusions": {
            "absent_rows": len(absent_rows),
            "unusable_rows": len(unusable_rows),
            "filtered_uf_rows": filtered_out,
            "absent_included": include_absent,
            "unusable_included": include_unusable,
            "monetary_values_of_excluded_groups": False,
        },
        "examined_rows": examined,
        "eligible_rows": eligible,
        "excluded_rows": excluded,
        "comparable_rows": comparable,
        "official_analytics_geography": _public_geography(
            analytics,
            selected_ufs=selected_ufs,
            include_absent=include_absent,
            include_unusable=include_unusable,
        ),
        "alignment": alignment,
        "_page": page,
    }
    population = _population(examined, eligible, universe)
    facts = _detail_facts(
        projection,
        query,
        page_items,
        population_total=examined,
        eligible_count=eligible,
        comparable_count=comparable,
        excluded_count=excluded,
        completeness="mixed",
        data=breakdown,
        item_payload=_uf_group_fact,
    )
    group_counts = {
        "population_total": examined,
        "eligible_count": eligible,
        "comparable_count": comparable,
        "excluded_count": excluded,
        "completeness": "mixed",
    }
    if include_absent:
        _append_group_fact(
            facts,
            projection,
            query,
            body["absent"],
            builder=_uf_group_fact,
            role="special_group",
            **group_counts,
        )
    if include_unusable:
        _append_group_fact(
            facts,
            projection,
            query,
            body["unusable"],
            builder=_uf_group_fact,
            role="special_group",
            **group_counts,
        )
    geography = body.get("official_analytics_geography")
    if isinstance(geography, Mapping):
        facts.append(
            _fact(
                projection,
                query,
                population_total=examined,
                eligible_count=eligible,
                comparable_count=comparable,
                excluded_count=excluded,
                completeness="mixed",
                data={
                    "fact_role": "official_geography",
                    "official_analytics_geography": geography,
                },
            )
        )
    return body, population, facts


def _by_weight(query, rows, tables, projection, budget):
    bands = {
        _band_id(lower, upper): _new_geo_bucket(tables)
        for lower, upper in WEIGHT_BANDS_V1
    }
    absent_rows: list[dict[str, Any]] = []
    invalid_rows: list[dict[str, Any]] = []
    unsupported_rows: list[dict[str, Any]] = []
    for row in rows:
        if budget.exceeded():
            return None, "processing_limit_exceeded", []
        kind, band = _classify_weight(row.get("weight"))
        if kind == "band" and band is not None:
            _accumulate_geo(bands[band], row, tables)
        elif kind == "absent":
            absent_rows.append(row)
        elif kind == "unsupported_decimal":
            unsupported_rows.append(row)
        else:
            invalid_rows.append(row)
    ordered = []
    for lower, upper in WEIGHT_BANDS_V1:
        band_id = _band_id(lower, upper)
        finished = _finish_geo(band_id, bands[band_id], tables, len(rows))
        finished["lower"] = _format_money(Decimal(lower))
        finished["upper"] = None if upper is None else _format_money(Decimal(upper))
        finished["lower_inclusive"] = True
        finished["upper_exclusive"] = upper is not None
        finished["last_band"] = upper is None
        ordered.append(finished)
    page, page_items = _page(ordered, query)
    body = {
        "bands": page_items,
        "absent": _finish_special(absent_rows, tables, "weight_absent"),
        "invalid": _finish_special(invalid_rows, tables, "weight_invalid"),
        "band_rule": {
            "version": WEIGHT_BANDS_VERSION,
            "lower_inclusive": True,
            "upper_exclusive": True,
            "last_band": "lower_inclusive_without_upper_bound",
        },
        "exclusions": {
            "absent_rows": len(absent_rows),
            "invalid_rows": len(invalid_rows),
        },
        "_page": page,
    }
    numeric = len(rows) - len(absent_rows) - len(invalid_rows) - len(unsupported_rows)
    comparable = sum(bucket["comparable_row_count"] for bucket in bands.values())
    complete_rows, complete_cells = _weight_completion(rows, tables)
    excluded_weight = len(absent_rows) + len(invalid_rows)
    excluded_comparability = numeric - comparable
    body["examined_rows"] = len(rows)
    body["numeric_weight_rows"] = numeric
    body["complete_freight_rows"] = complete_rows
    body["complete_freight_count"] = complete_cells
    body["comparable_rows"] = comparable
    body["unsupported_decimal_weight"] = {
        "id": "weight_unsupported_decimal",
        "row_count": len(unsupported_rows),
        "monetary_values_published": False,
        "reason": "decimal_not_in_official_numeric_contract",
    }
    body["exclusions"] = {
        "absent_rows": len(absent_rows),
        "invalid_rows": len(invalid_rows),
        "unsupported_decimal_weight_rows": len(unsupported_rows),
        "unsupported_decimal_monetary_values_published": False,
        "numeric_weight_rows": numeric,
        "complete_freight_rows": complete_rows,
        "complete_freight_count": complete_cells,
        "comparable_rows": comparable,
        "excluded_invalid_or_absent_weight": excluded_weight,
        "excluded_invalid_or_absent_weight_reference": "examined_rows",
        "excluded_not_comparable_rows": excluded_comparability,
        "excluded_not_comparable_reference": "numeric_weight_rows",
    }
    population = _population(len(rows), numeric, "rows_with_numeric_weight")
    population["numeric_weight_rows"] = numeric
    population["complete_freight_rows"] = complete_rows
    population["complete_freight_count"] = complete_cells
    population["comparable_rows"] = comparable
    population["excluded_invalid_or_absent_weight"] = excluded_weight
    population["excluded_unsupported_decimal_weight"] = len(unsupported_rows)
    population["excluded_not_comparable_rows"] = excluded_comparability
    population["excluded_rows_reference"] = "examined_rows_without_numeric_weight"
    envelope_excluded = len(rows) - numeric
    facts = _detail_facts(
        projection,
        query,
        page_items,
        population_total=len(rows),
        eligible_count=numeric,
        comparable_count=comparable,
        excluded_count=envelope_excluded,
        completeness="mixed",
        data={
            "fact_role": "summary",
            "bands_version": query.filters.get("bands_version", WEIGHT_BANDS_VERSION),
            "examined_rows": len(rows),
            "numeric_weight_rows": numeric,
            "complete_freight_rows": complete_rows,
            "complete_freight_count": complete_cells,
            "comparable_rows": comparable,
            "excluded_invalid_or_absent_weight": excluded_weight,
            "excluded_invalid_or_absent_weight_reference": "examined_rows",
            "excluded_not_comparable_rows": excluded_comparability,
            "excluded_not_comparable_reference": "numeric_weight_rows",
            "weight_absent_rows": len(absent_rows),
            "weight_invalid_rows": len(invalid_rows),
            "unsupported_decimal_weight_rows": len(unsupported_rows),
            "unsupported_decimal_monetary_values_published": False,
            "page": {
                "offset": page["offset"],
                "limit": page["limit"],
                "total": page["total"],
                "returned": page["returned"],
            },
        },
        item_payload=_weight_band_fact,
    )
    weight_counts = {
        "population_total": len(rows),
        "eligible_count": numeric,
        "comparable_count": comparable,
        "excluded_count": envelope_excluded,
        "completeness": "mixed",
    }
    for special in (body["absent"], body["invalid"]):
        _append_group_fact(
            facts,
            projection,
            query,
            special,
            builder=_weight_band_fact,
            role="special_group",
            **weight_counts,
        )
    if unsupported_rows:
        facts.append(
            _fact(
                projection,
                query,
                **weight_counts,
                data={
                    "fact_role": "special_group",
                    "id": "weight_unsupported_decimal",
                    "row_count": len(unsupported_rows),
                    "monetary_values_published": False,
                    "reason": "decimal_not_in_official_numeric_contract",
                },
            )
        )
    return body, population, facts


def _incomplete_freights(query, rows, tables, projection, budget):
    indexes = set(query.filters.get("row_indexes") or [])
    included = []
    warnings_only = 0
    for row in rows:
        if budget.exceeded():
            return None, "processing_limit_exceeded", []
        if indexes and row["row_index"] not in indexes:
            continue
        cells = [row["cells"].get(table["table_id"]) for table in tables]
        partial_cells = [cell for cell in cells if cell and cell["completeness"] == "partial"]
        if partial_cells:
            included.append(_incomplete_record(row, partial_cells))
            continue
        if cells and all(cell and cell["completeness"] == "complete" for cell in cells):
            if any(cell["warning_complete"] for cell in cells if cell):
                warnings_only += 1
    page, page_items = _page(included, query)
    body = {
        "rows": page_items,
        "excluded_complete_with_warnings": warnings_only,
        "_page": page,
    }
    population = _population(len(rows), len(included), "rows_with_partial_cells")
    facts = _detail_facts(
        projection,
        query,
        page_items,
        population_total=len(rows),
        eligible_count=len(included),
        comparable_count=len(included),
        excluded_count=len(rows) - len(included),
        data={
            "fact_role": "summary",
            "partial_rows": len(included),
            "excluded_complete_with_warnings": warnings_only,
        },
        item_payload=_partial_fact,
    )
    return body, population, facts


def _diagnostics(query, rows, tables, projection, budget):
    codes = set(query.filters.get("codes") or [])
    matched = []
    by_code: dict[str, dict[str, Any]] = {}
    accessorial_counts: dict[str, int] = {}
    for row in rows:
        if budget.exceeded():
            return None, "processing_limit_exceeded", []
        cell_records = []
        for table in tables:
            cell = row["cells"].get(table["table_id"])
            if not cell or cell["completeness"] != "not_calculated":
                continue
            record = _diagnostic_record(table, cell)
            for extra in record["preserved_codes"]:
                if extra not in OFFICIAL_DIAGNOSTIC_CODES:
                    accessorial_counts[extra] = accessorial_counts.get(extra, 0) + 1
            if codes and record["code"] not in codes and not any(item in codes for item in record["preserved_codes"]):
                continue
            cell_records.append(record)
            bucket = by_code.setdefault(
                record["code"],
                {"code": record["code"], "cell_count": 0, "row_indexes": set(), "constraint": record["constraint"]},
            )
            bucket["cell_count"] += 1
            bucket["row_indexes"].add(row["row_index"])
        if cell_records:
            cell_records.sort(key=lambda record: str(record.get("table_id") or ""))
            matched.append(
                {
                    "row_index": row["row_index"],
                    "document_number": row["document_number"],
                    "cells": cell_records,
                }
            )
    summary = [
        {
            "code": code,
            "cell_count": bucket["cell_count"],
            "row_count": len(bucket["row_indexes"]),
            "constraint": bucket["constraint"],
        }
        for code, bucket in sorted(by_code.items())
    ]
    page, page_items = _page(matched, query)
    body = {
        "by_code": summary,
        "rows": page_items,
        "constraints": {code: DIAGNOSTIC_CONSTRAINTS[code] for code in sorted(OFFICIAL_DIAGNOSTIC_CODES)},
        "preserved_accessorial_codes": [
            {"code": code, "cell_count": accessorial_counts[code]}
            for code in sorted(accessorial_counts)
        ],
        "_page": page,
    }
    population = _population(len(rows), len(matched), "rows_with_not_calculated_cells")
    facts = _detail_facts(
        projection,
        query,
        page_items,
        population_total=len(rows),
        eligible_count=len(matched),
        comparable_count=len(matched),
        excluded_count=len(rows) - len(matched),
        data={
            "fact_role": "summary",
            "by_code": summary,
            "constraints": body["constraints"],
            "preserved_accessorial_codes": body["preserved_accessorial_codes"],
        },
        item_payload=_diagnostic_fact,
    )
    return body, population, facts


def _resolve_row(
    row: Mapping[str, Any],
    tables: list[dict[str, Any]],
    projection: AuthorizedAnalyticalProjection,
) -> dict[str, Any]:
    cells = {}
    raw_cells = row.get("table_results") if isinstance(row.get("table_results"), Mapping) else {}
    for table in tables:
        raw = raw_cells.get(table["table_id"])
        cell = raw if isinstance(raw, Mapping) else None
        memory_item = _memory_item(cell, projection) if projection.result.get("schema_version") == 2 else None
        cells[table["table_id"]] = _resolve_cell(cell, table, memory_item, projection.memory_payload)
    return {
        "row_index": int(row["row_index"]),
        "document_number": _public_text(row.get("document_number"), limit=120),
        "destination_uf": row.get("destination_uf"),
        "weight": row.get("weight"),
        "cells": cells,
    }


def _resolve_cell(cell, table, memory_item, memory_payload) -> dict[str, Any]:
    completeness = _completeness(cell)
    raw_freight = None if cell is None else cell.get("calculated_freight")
    freight = None if cell is None else _money_decimal(raw_freight)
    if type(raw_freight) is Decimal:
        amount_support = "unsupported_decimal"
    elif freight is None:
        amount_support = "absent"
    else:
        amount_support = "official_numeric"
    status = None if cell is None else _cell_status(cell)
    components = _classify_components(
        cell if isinstance(cell, Mapping) else None,
        memory_item,
        memory_payload,
        completeness,
    )
    diagnostic, blocking_issues, unresolved_diagnostics = _resolve_diagnostics(
        cell,
        memory_item,
        memory_payload,
    )
    return {
        "table_id": table["table_id"],
        "slot_number": table["slot_number"],
        "carrier_name": table["carrier_name"],
        "status": status,
        "completeness": completeness,
        "warning_complete": completeness == "complete" and status == "calculated_with_warnings",
        "final_amount": _format_money(freight) if completeness == "complete" and freight is not None else None,
        "partial_amount": _format_money(freight) if completeness == "partial" and freight is not None else None,
        "amount_support": amount_support,
        "error": cell.get("error") if isinstance(cell, Mapping) and isinstance(cell.get("error"), Mapping) else None,
        "blocking_issues": blocking_issues,
        "diagnostic": diagnostic,
        "unresolved_diagnostic_refs": unresolved_diagnostics,
        "components": components,
        "raw": cell if isinstance(cell, Mapping) else None,
    }


def _classify_components(cell, memory_item, memory_payload, completeness) -> dict[str, Any]:
    source = _component_source(cell, memory_item, memory_payload)
    origin = source["origin"]
    if source["kind"] in {"none", "missing", "stripped"} and completeness == "not_calculated":
        slots = _blank_slots("not_applicable", origin)
    elif source["kind"] in {"none", "missing"}:
        slots = _blank_slots("unavailable", origin)
    elif source["kind"] == "stripped":
        slots = _blank_slots("unavailable", origin)
    elif source["kind"] == "list":
        slots = _blank_slots("absent", origin)
        _apply_component_list(slots, source["items"], origin)
    else:
        slots = _blank_slots("absent", origin)
        _apply_component_dict(slots, source["payload"], origin, source["buckets"])
    if source.get("inline_dict") is not None:
        _note_contaminated_gris(slots, source["inline_dict"], source["buckets"])
    _overlay_memory(slots, source.get("memory"), origin)
    source["accessorials"] = list(slots.pop("_collected_accessorials", []))
    source["ignored"] = list(slots.pop("_collected_ignored", []))
    publication_notices = list(slots.pop("_publication_notices", []))
    slots.pop("_accessorials", None)
    slots.pop("_ignored", None)
    slots = {key: value for key, value in slots.items() if key in COMPONENT_CODES}
    if completeness == "partial":
        for slot in slots.values():
            if slot["availability"] == "preserved":
                slot["availability"] = "partial"
    _strip_private(slots)
    detail = "itemized" if source["kind"] == "list" else ("aggregate" if source["kind"] == "dict" else "none")
    return {
        "origin": origin,
        "detail_level": detail,
        "slots": slots,
        "itemized_accessorials": source["accessorials"],
        "ignored_accessorials": source["ignored"],
        "aggregated_buckets": source["buckets"],
        "unresolved_component_refs": source["missing"],
        "publication_notices": publication_notices,
    }


def _component_source(cell, memory_item, memory_payload) -> dict[str, Any]:
    base = {
        "accessorials": [],
        "ignored": [],
        "buckets": [],
        "missing": 0,
        "inline_dict": None,
        "memory": None,
    }
    if memory_item is not None:
        memory = memory_item.get("calculation_memory")
        base["memory"] = memory if isinstance(memory, Mapping) else None
        catalog = {}
        if isinstance(memory_payload, Mapping) and isinstance(memory_payload.get("components"), Mapping):
            catalog = memory_payload["components"]
        refs = memory_item.get("component_refs") if "component_refs" in memory_item else None
        if isinstance(refs, list) and refs:
            return _source_from_ref_list(base, refs, catalog)
        if "component_ref" in memory_item and isinstance(memory_item.get("component_ref"), str):
            ref = memory_item.get("component_ref")
            if ref:
                found = catalog.get(ref)
                if isinstance(found, Mapping):
                    base.update(kind="dict", payload=dict(found), origin="memory_catalog_dict")
                    return base
                base.update(kind="missing", missing=1, origin="memory_catalog_missing")
                return base
        inline = _preserved_components(memory_item, memory)
        if inline is not None:
            return _source_from_components(
                base,
                inline,
                origin_list="rehydrated_component_list",
                origin_dict="rehydrated_component_dict",
            )
        base.update(kind="stripped", origin="stripped_memory")
        return base

    memory = cell.get("calculation_memory") if isinstance(cell, Mapping) else None
    memory = memory if isinstance(memory, Mapping) else None
    base["memory"] = memory
    inline_cell = cell.get("components") if isinstance(cell, Mapping) and "components" in cell else None
    if isinstance(memory, Mapping) and "components" in memory and _component_shape(memory.get("components")):
        base["inline_dict"] = dict(inline_cell) if isinstance(inline_cell, Mapping) else None
        return _source_from_components(
            base,
            memory.get("components"),
            origin_list="memory_component_list",
            origin_dict="memory_component_dict",
        )
    if _component_shape(inline_cell) == "list":
        base.update(kind="list", items=list(inline_cell), origin="cell_component_list")
        return base
    if _component_shape(inline_cell) == "dict":
        base.update(kind="dict", payload=dict(inline_cell), origin="cell_component_dict")
        return base
    base.update(kind="none", origin="none")
    return base


def _component_shape(value: Any) -> str | None:
    if isinstance(value, list):
        return "list"
    if isinstance(value, Mapping):
        return "dict"
    return None


def _source_from_ref_list(base, refs, catalog) -> dict[str, Any]:
    items = []
    missing = 0
    for ref in refs:
        found = catalog.get(ref) if isinstance(ref, str) else None
        if isinstance(found, Mapping):
            items.append(dict(found))
        else:
            missing += 1
    base.update(
        kind="list",
        items=items,
        missing=missing,
        origin="memory_catalog_list",
        accessorials=[],
        ignored=[],
    )
    return base


def _preserved_components(memory_item, memory):
    """Componentes já materializados. Referência vazia não autoriza reconstrução."""

    if "components" in memory_item:
        components = memory_item.get("components")
        if _component_shape(components):
            return components
        return None
    compact_refs = "component_ref" in memory_item or "component_refs" in memory_item
    if compact_refs:
        return None
    if isinstance(memory, Mapping) and "components" in memory and _component_shape(memory.get("components")):
        return memory.get("components")
    return None


def _source_from_components(base, components, *, origin_list: str, origin_dict: str) -> dict[str, Any]:
    if isinstance(components, list):
        base.update(kind="list", items=list(components), origin=origin_list)
        return base
    if isinstance(components, Mapping):
        base.update(kind="dict", payload=dict(components), origin=origin_dict)
        return base
    base.update(kind="none", origin="none")
    return base


def _apply_component_list(slots, items, origin) -> None:
    for item in items:
        if not isinstance(item, Mapping):
            continue
        code = item.get("code")
        amount_value = item.get("amount")
        if code == "WEIGHT_FREIGHT":
            _take_amount(slots, "weight_freight", amount_value, origin, "item")
        elif code == "FREIGHT_VALUE":
            _take_amount(slots, "freight_value", amount_value, origin, "item")
        elif code == "TOLL":
            _take_amount(slots, "toll", amount_value, origin, "item")
        elif code == "SUBTOTAL":
            _take_amount(slots, "subtotal", amount_value, origin, "item")
        elif code == "TOTAL":
            _take_amount(slots, "total", amount_value, origin, "item")
        elif code == "TAX":
            _apply_tax(slots, item, origin)
        elif code == "ACCESSORIAL":
            _note_accessorial(slots, item, origin, applied=True)
        elif code == "IGNORED_ACCESSORIAL":
            _note_accessorial(slots, item, origin, applied=False)


def _apply_component_dict(slots, payload, origin, buckets) -> None:
    mapping = {
        "weight_freight": "weight_freight",
        "freight_value_component": "freight_value",
        "toll": "toll",
        "icms": "icms",
        "iss": "iss",
        "subtotal": "subtotal",
        "total": "total",
    }
    for raw_key, slot_name in mapping.items():
        if raw_key in payload:
            _take_amount(slots, slot_name, payload.get(raw_key), origin, "item")
    accessorials = payload.get("accessorials")
    if isinstance(accessorials, list):
        for item in accessorials:
            if isinstance(item, Mapping):
                _note_accessorial(slots, item, origin, applied=True)
    ignored = payload.get("ignored_accessorial_fees")
    if isinstance(ignored, list):
        for item in ignored:
            if isinstance(item, Mapping):
                _note_accessorial(slots, item, origin, applied=False)
    _preserve_tax_aggregate(slots, payload, origin)
    _note_contaminated_gris(slots, payload, buckets)
    if "dispatch" in payload:
        raw_dispatch = payload.get("dispatch")
        if type(raw_dispatch) is Decimal:
            buckets.append(
                {
                    "code": "dispatch",
                    "amount": None,
                    "availability": "unsupported_decimal",
                    "numeric_support": "unsupported_decimal",
                    "identifiable_as": None,
                }
            )
        else:
            amount = _money_decimal(raw_dispatch)
            buckets.append(
                {
                    "code": "dispatch",
                    "amount": None if amount is None else _format_money(amount),
                    "availability": "aggregated_without_detail",
                    "identifiable_as": None,
                }
            )


def _preserve_tax_aggregate(slots, payload, origin) -> None:
    """Mantém o total oficial de impostos, inclusive zero, sem somar ICMS e ISS."""

    if "taxes" not in payload:
        return
    if _decimal_blocked(slots, "taxes", payload.get("taxes")):
        return
    amount = _money_decimal(payload.get("taxes"))
    if amount is None:
        return
    _assign(slots, "taxes", amount, origin, "aggregate")
    slots["taxes"]["detail_level"] = "aggregate"


def _note_contaminated_gris(slots, payload, buckets) -> None:
    if not isinstance(payload, Mapping) or "gris" not in payload:
        return
    raw_gris = payload.get("gris")
    if type(raw_gris) is Decimal:
        buckets.append(
            {
                "code": "gris_ad_valorem_bucket",
                "amount": None,
                "availability": "unsupported_decimal",
                "numeric_support": "unsupported_decimal",
                "identifiable_as": None,
            }
        )
        return
    amount = _money_decimal(raw_gris)
    identifiable = slots["gris"]["availability"] == "preserved" or slots["ad_valorem"]["availability"] == "preserved"
    buckets.append(
        {
            "code": "gris_ad_valorem_bucket",
            "amount": None if amount is None else _format_money(amount),
            "availability": "aggregated_without_detail",
            "identifiable_as": None,
            "suppressed": identifiable,
        }
    )
    if identifiable:
        return
    for name in ("gris", "ad_valorem"):
        if slots[name]["availability"] == "absent":
            slots[name] = {
                "availability": "aggregated_without_detail",
                "amount": None,
                "amount_state": "absent",
                "detail_level": "aggregate",
                "source": slots[name]["source"],
                "bucket": "gris_ad_valorem_bucket",
            }


def _note_accessorial(slots, item, origin, *, applied: bool) -> None:
    canonical = _canonical_name(item)
    notices: list[dict[str, Any]] = []
    label = _public_text(
        item.get("label") or item.get("name"),
        limit=80,
        notices=notices,
        category="label",
    )
    if notices:
        slots.setdefault("_publication_notices", []).extend(notices)
    raw_amount = item.get("amount")
    if type(raw_amount) is Decimal:
        record = {
            "canonical_component": canonical,
            "label": label,
            "amount": None,
            "amount_state": "absent",
            "numeric_support": "unsupported_decimal",
            "applied": applied,
            "reason_code": _token_or_none(item.get("reason_code") or item.get("reason")),
        }
        if applied:
            slots.setdefault("_accessorials", []).append(record)
        else:
            slots.setdefault("_ignored", []).append(record)
        return
    amount = _money_decimal(raw_amount)
    record = {
        "canonical_component": canonical,
        "label": label,
        "amount": None if amount is None else _format_money(amount),
        "amount_state": _amount_state(amount),
        "applied": applied,
        "reason_code": _token_or_none(item.get("reason_code") or item.get("reason")),
    }
    if applied:
        slots.setdefault("_accessorials", []).append(record)
        if canonical in {"gris", "ad_valorem"}:
            _accumulate(slots, canonical, amount, origin)
        else:
            _accumulate(slots, "accessorial", amount, origin)
    else:
        slots.setdefault("_ignored", []).append(record)


def _overlay_memory(slots, memory, origin) -> None:
    if not isinstance(memory, Mapping):
        _flush_collections(slots)
        return
    if "subtotal_before_taxes" in memory:
        _merge_money(slots, "subtotal", memory.get("subtotal_before_taxes"), origin)
    if "total" in memory:
        _merge_money(slots, "total", memory.get("total"), origin)
    taxes = memory.get("taxes")
    if isinstance(taxes, list):
        for item in taxes:
            if isinstance(item, Mapping):
                _apply_tax(slots, item, origin)
    _flush_collections(slots)


def _apply_tax(slots, item, origin) -> None:
    tax_type = item.get("tax_type") or item.get("type")
    if not isinstance(tax_type, str):
        return
    normalized = tax_type.strip().upper()
    if normalized not in {"ICMS", "ISS"}:
        return
    amount = _money_decimal(item.get("amount"))
    if type(item.get("amount")) is Decimal:
        name = normalized.casefold()
        _decimal_blocked(slots, name, item.get("amount"))
        return
    applied = item.get("applied")
    name = normalized.casefold()
    if applied is False and amount is None:
        if slots[name]["availability"] == "absent":
            slots[name] = {
                "availability": "not_applicable",
                "amount": None,
                "amount_state": "absent",
                "detail_level": "item",
                "source": origin,
            }
        return
    _merge_money(slots, name, item.get("amount"), origin)


def _decimal_blocked(slots, name, value) -> bool:
    if type(value) is not Decimal:
        return False
    slot = slots[name]
    slot["numeric_support"] = "unsupported_decimal"
    slot["availability"] = "unsupported_decimal"
    slot["amount"] = None
    slot["amount_state"] = "absent"
    return True


def _take_amount(slots, name, value, origin, detail) -> None:
    if _decimal_blocked(slots, name, value):
        return
    _assign(slots, name, _money_decimal(value), origin, detail)


def _merge_money(slots, name, value, origin) -> None:
    if _decimal_blocked(slots, name, value):
        return
    amount = _money_decimal(value)
    current = slots[name]
    if amount is None:
        if current["availability"] in {"absent", "unavailable"}:
            _assign(slots, name, None, origin, current["detail_level"])
        return
    if current["availability"] in {"absent", "unavailable", "not_applicable", "aggregated_without_detail"}:
        _assign(slots, name, amount, origin, "item")
        return
    current_amount = _money_decimal(current.get("amount"))
    if current_amount is not None and current_amount != amount:
        slots[name] = {
            "availability": "unavailable",
            "amount": None,
            "amount_state": "absent",
            "detail_level": "none",
            "source": origin,
            "conflict": True,
        }


def _accumulate(slots, name, amount, origin) -> None:
    current = slots[name]
    if current["availability"] == "unavailable":
        return
    if amount is None:
        _assign(slots, name, None, origin, "item")
        return
    values = current.setdefault("_values", [])
    values.append(amount)
    total = sum(values, Decimal("0.00"))
    current["availability"] = "preserved"
    current["amount"] = _format_money(total)
    current["amount_state"] = _amount_state(total)
    current["detail_level"] = "item"
    current["source"] = origin


def _assign(slots, name, amount, origin, detail) -> None:
    if amount is None:
        slots[name] = {
            "availability": "unavailable",
            "amount": None,
            "amount_state": "absent",
            "detail_level": detail,
            "source": origin,
        }
        return
    slots[name] = {
        "availability": "preserved",
        "amount": _format_money(amount),
        "amount_state": _amount_state(amount),
        "detail_level": detail,
        "source": origin,
    }


def _blank_slots(availability: str, source: str) -> dict[str, dict[str, Any]]:
    return {
        code: {
            "availability": availability,
            "amount": None,
            "amount_state": "absent",
            "detail_level": "none",
            "source": source,
        }
        for code in sorted(COMPONENT_CODES)
    }


def _flush_collections(slots) -> None:
    accessorials = slots.pop("_accessorials", [])
    ignored = slots.pop("_ignored", [])
    slots["_collected_accessorials"] = accessorials
    slots["_collected_ignored"] = ignored


def _strip_private(slots) -> None:
    for slot in slots.values():
        slot.pop("_values", None)


def _component_record(row, table, cell, codes) -> dict[str, Any]:
    classified = cell["components"] if cell else _classify_components(None, None, None, "not_calculated")
    # Collections were stored on the source dict during classify via side channels.
    slots = classified["slots"]
    if codes:
        slots = {code: slots[code] for code in codes if code in slots}
    record = {
        "row_index": row["row_index"],
        "document_number": row["document_number"],
        "table_id": table["table_id"],
        "slot_number": table["slot_number"],
        "carrier_name": table["carrier_name"],
        "completeness": None if cell is None else cell["completeness"],
        "final_amount": None if cell is None else cell["final_amount"],
        "partial_amount": None if cell is None else cell["partial_amount"],
        "amount_support": None if cell is None else cell.get("amount_support"),
        "origin": classified["origin"],
        "detail_level": classified["detail_level"],
        "components": slots,
        "itemized_accessorials": classified["itemized_accessorials"],
        "ignored_accessorials": classified["ignored_accessorials"],
        "aggregated_buckets": classified["aggregated_buckets"],
        "unresolved_component_refs": classified["unresolved_component_refs"],
    }
    if classified.get("publication_notices"):
        record["publication_notices"] = list(classified["publication_notices"])
    return record


def _comparable_stats(rows, tables, budget):
    table_ids = [table["table_id"] for table in tables]
    comparable = 0
    partial = 0
    inconclusive = 0
    decisive = 0
    ties = 0
    savings = Decimal("0.00")
    coverage = {
        table["table_id"]: {
            "table_id": table["table_id"],
            "complete_rows": 0,
            "partial_rows": 0,
            "not_calculated_rows": 0,
            "comparable_freight": Decimal("0.00"),
        }
        for table in tables
    }
    for row in rows:
        if budget.exceeded():
            return None
        class_name, amounts = _comparability(row, table_ids)
        for table_id in table_ids:
            cell = row["cells"].get(table_id)
            kind = "not_calculated" if cell is None else cell["completeness"]
            if kind == "complete":
                coverage[table_id]["complete_rows"] += 1
            elif kind == "partial":
                coverage[table_id]["partial_rows"] += 1
            else:
                coverage[table_id]["not_calculated_rows"] += 1
        if class_name == "fully_comparable":
            comparable += 1
            gap = _gap_from_amounts(amounts)
            for table_id, amount in amounts:
                coverage[table_id]["comparable_freight"] += amount
            if gap == 0:
                ties += 1
            else:
                decisive += 1
                savings += gap
        elif class_name == "partially_comparable":
            partial += 1
        else:
            inconclusive += 1
    total = len(rows)
    carriers = []
    coverage_rows = []
    for table in tables:
        item = coverage[table["table_id"]]
        carriers.append(
            {
                "table_id": table["table_id"],
                "slot_number": table["slot_number"],
                "carrier_name": table["carrier_name"],
                "comparable_row_count": comparable,
                "comparable_freight_total": _format_money(item["comparable_freight"]) if comparable else None,
                "universe": "selected_tables_fully_comparable",
            }
        )
        coverage_rows.append(
            {
                "table_id": table["table_id"],
                "complete_rows": item["complete_rows"],
                "partial_rows": item["partial_rows"],
                "not_calculated_rows": item["not_calculated_rows"],
                "coverage_percentage": _percent(item["complete_rows"], total),
                "universe": "all_rows_of_this_table",
            }
        )
    return {
        "comparable_rows": comparable,
        "carriers": carriers,
        "coverage": coverage_rows,
        "potential_savings": _format_money(savings) if decisive else _format_money(Decimal("0.00")),
        "decisive_row_count": decisive,
        "tie_count": ties,
        "exclusions": {
            "partially_comparable_rows": partial,
            "inconclusive_rows": inconclusive,
        },
    }


def _row_gap(row, table_ids):
    class_name, amounts = _comparability(row, table_ids)
    if class_name != "fully_comparable":
        return None
    ordered = sorted(amounts, key=lambda item: (item[1], item[0]))
    gap = _gap_from_amounts(amounts)
    return {
        "gap": gap,
        "tie": gap == 0,
        "freights": [
            {"table_id": table_id, "amount": _format_money(amount)}
            for table_id, amount in ordered
        ],
    }


def _comparability(row, table_ids):
    amounts = []
    complete = 0
    for table_id in table_ids:
        cell = row["cells"].get(table_id)
        if cell and cell["completeness"] == "complete" and cell["final_amount"] is not None:
            complete += 1
            amounts.append((table_id, Decimal(cell["final_amount"])))
    if complete == len(table_ids):
        return "fully_comparable", amounts
    if complete == 0:
        return "inconclusive", []
    return "partially_comparable", []


def _gap_from_amounts(amounts) -> Decimal:
    ordered = sorted(amount for _, amount in amounts)
    minimum = ordered[0]
    if sum(1 for amount in ordered if amount == minimum) > 1:
        return Decimal("0.00")
    return (ordered[1] - minimum).quantize(_MONEY)


def _new_geo_bucket(tables) -> dict[str, Any]:
    return {
        "row_count": 0,
        "comparable_row_count": 0,
        "excluded_row_count": 0,
        "savings": Decimal("0.00"),
        "complete": {table["table_id"]: 0 for table in tables},
        "complete_freight": {table["table_id"]: Decimal("0.00") for table in tables},
        "comparable_freight": {table["table_id"]: Decimal("0.00") for table in tables},
    }


def _accumulate_geo(bucket, row, tables) -> None:
    table_ids = [table["table_id"] for table in tables]
    bucket["row_count"] += 1
    class_name, amounts = _comparability(row, table_ids)
    for table in tables:
        cell = row["cells"].get(table["table_id"])
        if cell and cell["completeness"] == "complete" and cell["final_amount"] is not None:
            bucket["complete"][table["table_id"]] += 1
            bucket["complete_freight"][table["table_id"]] += Decimal(cell["final_amount"])
    if class_name == "fully_comparable":
        bucket["comparable_row_count"] += 1
        gap = _gap_from_amounts(amounts)
        bucket["savings"] += gap
        for table_id, amount in amounts:
            bucket["comparable_freight"][table_id] += amount
    else:
        bucket["excluded_row_count"] += 1


def _finish_geo(identity, bucket, tables, total_rows) -> dict[str, Any]:
    return {
        "id": identity,
        "row_count": bucket["row_count"],
        "comparable_row_count": bucket["comparable_row_count"],
        "excluded_row_count": bucket["excluded_row_count"],
        "coverage": [
            {
                "table_id": table["table_id"],
                "complete_rows": bucket["complete"][table["table_id"]],
                "coverage_percentage": _percent(bucket["complete"][table["table_id"]], bucket["row_count"]),
                "complete_freight_total": _format_money(bucket["complete_freight"][table["table_id"]]),
                "comparable_freight_total": _format_money(bucket["comparable_freight"][table["table_id"]]),
            }
            for table in tables
        ],
        "potential_savings": _format_money(bucket["savings"]),
        "universe": "preserved_group",
        "scanned_rows": total_rows,
    }


def _finish_special(rows, tables, identity) -> dict[str, Any]:
    bucket = _new_geo_bucket(tables)
    for row in rows:
        _accumulate_geo(bucket, row, tables)
    finished = _finish_geo(identity, bucket, tables, len(rows))
    finished["row_count"] = len(rows)
    return finished


def _geography_alignment(groups, absent_rows, unusable_rows, analytics) -> dict[str, Any]:
    if not isinstance(analytics, Mapping):
        return {"aligned_with_official_geography": False, "reason": "official_analytics_unavailable"}
    if unusable_rows:
        return {
            "aligned_with_official_geography": False,
            "reason": "unusable_uf_kept_separate",
        }
    official = {
        item.get("uf_label"): item
        for item in analytics.get("geography", {}).get("destination_ufs", [])
        if isinstance(item, Mapping)
    }
    for code, bucket in groups.items():
        official_item = official.get(code)
        if official_item is None:
            return {"aligned_with_official_geography": False, "reason": "official_uf_missing"}
        official_savings = _money_decimal(official_item.get("total_potential_savings"))
        if official_savings is None or official_savings != bucket["savings"].quantize(_MONEY):
            return {"aligned_with_official_geography": False, "reason": "savings_differ"}
    absent_official = official.get("N/D")
    absent_count = 0 if absent_official is None else int(absent_official.get("row_count") or 0)
    if absent_count != len(absent_rows):
        return {"aligned_with_official_geography": False, "reason": "absent_count_differs"}
    return {"aligned_with_official_geography": True, "reason": "comparable_savings_match_official_geography"}


def _public_exclusion(aggregate: Mapping[str, Any], *, included: bool) -> dict[str, Any]:
    """Contagem separada quando o grupo não faz parte do universo publicado."""

    if included:
        published = dict(aggregate)
        published["included"] = True
        published["monetary_values_published"] = True
        return published
    return {
        "id": aggregate.get("id"),
        "row_count": int(aggregate.get("row_count") or 0),
        "included": False,
        "monetary_values_published": False,
    }


def _public_geography(analytics, *, selected_ufs, include_absent, include_unusable):
    if not isinstance(analytics, Mapping):
        return None
    geography = analytics.get("geography")
    if not isinstance(geography, Mapping):
        return None
    if not selected_ufs and include_absent and include_unusable:
        return geography

    def keep(label: Any) -> bool:
        kind = _official_label_kind(label)
        if kind == "absent":
            return include_absent
        if kind == "unusable":
            return include_unusable
        if selected_ufs and label not in selected_ufs:
            return False
        return True

    destination = [
        item
        for item in geography.get("destination_ufs") or []
        if isinstance(item, Mapping) and keep(item.get("uf_label"))
    ]
    ranking = [
        item
        for item in geography.get("uf_potential_ranking") or []
        if isinstance(item, Mapping) and keep(item.get("uf_label"))
    ]
    return {
        "destination_ufs": destination,
        "uf_potential_ranking": ranking,
        "low_sample_threshold": geography.get("low_sample_threshold"),
        "ufs_with_comparable_base": sum(1 for item in destination if item.get("has_comparable_base")),
        "uf_count": len(destination),
        "publication": "selected_universe_only",
    }


def _official_label_kind(label: Any) -> str:
    if not isinstance(label, str) or not label.strip() or label == "N/D":
        return "absent"
    if label in OFFICIAL_UF_CODES:
        return "uf"
    return "unusable"


def _weight_completion(rows, tables) -> tuple[int, int]:
    """Fretes completos dentro das linhas com peso numérico válido."""

    complete_rows = 0
    complete_cells = 0
    expected = len(tables)
    for row in rows:
        kind, _band = _classify_weight(row.get("weight"))
        if kind != "band":
            continue
        cells = [row["cells"].get(table["table_id"]) for table in tables]
        complete_in_row = sum(1 for cell in cells if cell and cell["completeness"] == "complete")
        complete_cells += complete_in_row
        if expected and complete_in_row == expected:
            complete_rows += 1
    return complete_rows, complete_cells


def _classify_uf(raw) -> tuple[str, str | None]:
    if raw is None:
        return "absent", None
    if not isinstance(raw, str):
        return "unusable", None
    stripped = raw.strip()
    if not stripped:
        return "absent", None
    if len(stripped) == 2 and stripped.upper() in OFFICIAL_UF_CODES:
        return "uf", stripped.upper()
    return "unusable", None


def _append_group_fact(facts, projection, query, item, *, builder, role, **counts) -> None:
    if not isinstance(item, Mapping):
        return
    payload = builder(item)
    payload["fact_role"] = role
    payload["monetary_values_published"] = item.get("monetary_values_published", True)
    facts.append(_fact(projection, query, data=payload, **counts))


def _classify_weight(raw) -> tuple[str, str | None]:
    if type(raw) is Decimal:
        return "unsupported_decimal", None
    if raw is None:
        return "absent", None
    amount = _money_decimal(raw) if not isinstance(raw, str) else None
    if amount is None or amount < 0:
        return "invalid", None
    for lower, upper in WEIGHT_BANDS_V1:
        low = Decimal(lower)
        if upper is None:
            if amount >= low:
                return "band", _band_id(lower, upper)
        elif low <= amount < Decimal(upper):
            return "band", _band_id(lower, upper)
    return "invalid", None


def _band_id(lower: str, upper: str | None) -> str:
    return f"{lower}:{upper or 'inf'}"


def _incomplete_record(row, cells) -> dict[str, Any]:
    return {
        "row_index": row["row_index"],
        "document_number": row["document_number"],
        "cells": [
            {
                "table_id": cell["table_id"],
                "completeness": "partial",
                "partial_amount": cell["partial_amount"],
                "final_amount": None,
                "reasons": _preserved_reasons(cell),
            }
            for cell in cells
        ],
    }


def _diagnostic_record(table, cell) -> dict[str, Any]:
    primary = _primary_code(cell)
    preserved = _extra_codes(cell, primary)
    constraint = DIAGNOSTIC_CONSTRAINTS.get(primary) or {
        "preserved_meaning": "preserved_without_added_meaning",
        "must_not_assert": [],
    }
    return {
        "table_id": table["table_id"],
        "code": primary,
        "status": cell["status"],
        "constraint": constraint,
        "preserved_codes": preserved,
        "preserved_messages": _preserved_reasons(cell),
        "unresolved_diagnostic_refs": int(cell.get("unresolved_diagnostic_refs") or 0),
        "asserted_facts": [],
        "evidence": _safe_evidence(cell),
    }


def _primary_code(cell) -> str:
    error = cell.get("error")
    if isinstance(error, Mapping):
        code = _token_or_none(error.get("code"))
        if code:
            return code
    status = _token_or_none(cell.get("status"))
    if status:
        return status
    diagnostic = cell.get("diagnostic")
    if isinstance(diagnostic, Mapping):
        code = _token_or_none(diagnostic.get("code"))
        if code:
            return code
    return "not_calculated"


def _extra_codes(cell, primary: str) -> list[str]:
    found: list[str] = []

    def add(value: Any) -> None:
        code = _token_or_none(value)
        if code and code != primary and code not in found:
            found.append(code)

    for item in cell.get("blocking_issues") or []:
        if isinstance(item, Mapping):
            add(item.get("reason_code"))
            add(item.get("code"))
    diagnostic = cell.get("diagnostic")
    if isinstance(diagnostic, Mapping):
        add(diagnostic.get("code"))
        add(diagnostic.get("reason"))
        add(diagnostic.get("component"))
    ignored = cell["components"]["ignored_accessorials"]
    for item in ignored:
        add(item.get("reason_code"))
    return found


def _preserved_reasons(cell) -> list[dict[str, Any]]:
    reasons = []
    error = cell.get("error")
    if isinstance(error, Mapping):
        reasons.append(
            {
                "code": _token_or_none(error.get("code")),
                "message": _public_text(error.get("message"), limit=200),
            }
        )
    for item in cell.get("blocking_issues") or []:
        if isinstance(item, Mapping):
            reasons.append(
                {
                    "code": _token_or_none(item.get("reason_code") or item.get("code")),
                    "message": _public_text(item.get("message"), limit=200),
                }
            )
    diagnostic = cell.get("diagnostic")
    if isinstance(diagnostic, Mapping):
        reasons.append(
            {
                "code": _token_or_none(diagnostic.get("code")),
                "message": _public_text(diagnostic.get("message"), limit=200),
            }
        )
    return reasons


def _safe_evidence(cell) -> dict[str, Any]:
    diagnostic = cell.get("diagnostic")
    if not isinstance(diagnostic, Mapping):
        return {}
    evidence = diagnostic.get("evidence") if isinstance(diagnostic.get("evidence"), Mapping) else {}
    cleaned = {}
    for key, value in evidence.items():
        if not isinstance(key, str) or not _token_or_none(key):
            continue
        if isinstance(value, str):
            cleaned[key] = _public_text(value, limit=200)
        elif type(value) in {int, bool}:
            cleaned[key] = value
    return cleaned


def _resolve_diagnostics(cell, memory_item, memory_payload):
    blocking, unresolved = _resolve_blocking(cell, memory_item, memory_payload)
    diagnostic = _diagnostic_source(cell, memory_item, memory_payload)
    if diagnostic is None and isinstance(memory_item, Mapping):
        reason = memory_item.get("reason_code")
        if isinstance(reason, str) and reason.strip():
            diagnostic = {"code": reason.strip()}
    return diagnostic, blocking, unresolved


def _resolve_blocking(cell, memory_item, memory_payload):
    if isinstance(cell, Mapping) and isinstance(cell.get("blocking_issues"), list) and cell.get("blocking_issues"):
        return [dict(item) for item in cell["blocking_issues"] if isinstance(item, Mapping)], 0
    if not isinstance(memory_item, Mapping):
        return [], 0
    inline = memory_item.get("blocking_issues")
    if isinstance(inline, list) and inline:
        return [dict(item) for item in inline if isinstance(item, Mapping)], 0
    memory = memory_item.get("calculation_memory")
    if isinstance(memory, Mapping) and isinstance(memory.get("blocking_issues"), list) and memory.get("blocking_issues"):
        return [dict(item) for item in memory["blocking_issues"] if isinstance(item, Mapping)], 0
    codes = memory_item.get("blocking_codes")
    if isinstance(codes, list):
        return _messages_from_codes(codes, memory_payload)
    return [], 0


def _messages_from_codes(codes, memory_payload):
    catalog = {}
    if isinstance(memory_payload, Mapping) and isinstance(memory_payload.get("messages"), Mapping):
        catalog = memory_payload["messages"]
    resolved = []
    missing = 0
    for code in codes:
        found = catalog.get(code) if isinstance(code, str) else None
        if not isinstance(found, Mapping):
            missing += 1
            continue
        preserved = found.get("code")
        if not isinstance(preserved, str) or not preserved.strip():
            missing += 1
            continue
        message = found.get("text") if isinstance(found.get("text"), str) else found.get("message")
        resolved.append(
            {
                "code": preserved.strip(),
                "message": message if isinstance(message, str) else None,
                "severity": found.get("severity") if isinstance(found.get("severity"), str) else None,
            }
        )
    return resolved, missing


def _diagnostic_source(cell, memory_item, memory_payload) -> dict[str, Any] | None:
    if isinstance(cell, Mapping) and isinstance(cell.get("diagnostic"), Mapping):
        return dict(cell["diagnostic"])
    if isinstance(memory_item, Mapping):
        memory = memory_item.get("calculation_memory")
        if isinstance(memory, Mapping) and isinstance(memory.get("diagnostic"), Mapping):
            return dict(memory["diagnostic"])
        if isinstance(memory_item.get("diagnostic"), Mapping):
            return dict(memory_item["diagnostic"])
        for evidence in _evidence_payloads(memory_item, memory, memory_payload):
            diagnostic = evidence.get("diagnostic")
            if isinstance(diagnostic, Mapping):
                return dict(diagnostic)
    return None


def _evidence_payloads(memory_item, memory, memory_payload):
    found = []
    for container in (memory_item, memory if isinstance(memory, Mapping) else None):
        if not isinstance(container, Mapping):
            continue
        for key in ("evidence", "item_evidence"):
            payload = container.get(key)
            if isinstance(payload, Mapping):
                found.append(payload)
    catalog = {}
    if isinstance(memory_payload, Mapping) and isinstance(memory_payload.get("evidence_catalog"), Mapping):
        catalog = memory_payload["evidence_catalog"]
    refs = memory_item.get("evidence_refs") if isinstance(memory_item, Mapping) else None
    if isinstance(refs, list):
        for ref in refs:
            payload = catalog.get(ref) if isinstance(ref, str) else None
            if isinstance(payload, Mapping):
                found.append(payload)
    return found


def _memory_item(cell, projection):
    if not isinstance(cell, Mapping) or not isinstance(projection.memory_payload, Mapping):
        return None
    ref = cell.get("memory_ref")
    items = projection.memory_payload.get("items")
    if isinstance(ref, str) and isinstance(items, Mapping):
        item = items.get(ref)
        return item if isinstance(item, Mapping) else None
    return None


def _load_analytics(projection, budget):
    if budget.exceeded():
        return None
    if projection.official_analytics is not None:
        return projection.official_analytics
    return _trusted_analytics(projection.result)


def _trusted_analytics(result: Mapping[str, Any]) -> dict[str, Any]:
    """Reproduz os analytics oficiais a partir do resultado já validado.

    O serviço é puro: não grava, não fatura e não chama o motor de frete.
    O retorno dele é a fonte analítica. Um checksum do payload recebido não
    prova que esse payload saiu do mesmo resultado.
    """

    view = copy.deepcopy(dict(result))
    if int(view.get("schema_version") or 0) == 2:
        view["schema_version"] = 1
    from app.agente_compara_comparison_analytics_service import (
        AgenteComparaComparisonAnalyticsError,
        build_comparison_analytics,
    )

    failed = False
    try:
        built = build_comparison_analytics(view)
    except AgenteComparaComparisonAnalyticsError:
        failed = True
        built = None
    if failed or not isinstance(built, dict):
        raise QueryCatalogError("official_analytics_unavailable")
    return built


def _select_tables(tables, requested):
    if not requested:
        return tables, None
    known = {table["table_id"]: table for table in tables}
    if any(table_id not in known for table_id in requested):
        return [], "invalid_table_reference"
    chosen = [known[table_id] for table_id in requested]
    chosen.sort(key=lambda item: (item["slot_number"], item["table_id"]))
    return chosen, None


def _ordered_tables(tables) -> list[dict[str, Any]]:
    return sorted(tables, key=lambda item: (int(item["slot_number"]), str(item["table_id"])))


def _row_table_sort_key(item: Mapping[str, Any]):
    row_index = item.get("row_index")
    slot = item.get("slot_number")
    table_id = item.get("table_id") or ""
    return (
        row_index if isinstance(row_index, int) else 10**12,
        slot if isinstance(slot, int) else 10**12,
        str(table_id),
    )


def _page(items, query):
    ordered = list(items)
    if query.sort == SORT_ROW_INDEX_ASC:
        ordered.sort(key=_row_table_sort_key)
    elif query.sort == SORT_UF_ASC:
        ordered.sort(key=lambda item: str(item.get("id") or ""))
    offset = 0 if query.page_offset is None else query.page_offset
    limit = len(ordered) if query.page_limit is None else query.page_limit
    page_items = ordered[offset : offset + limit]
    return (
        {
            "offset": offset,
            "limit": limit,
            "total": len(ordered),
            "returned": len(page_items),
            "sort": query.sort,
            "cursor_contract": CURSOR_CONTRACT,
            "signed_cursor": None,
        },
        page_items,
    )


def _population(total: int, eligible: int, universe: str) -> dict[str, Any]:
    return {
        "total_rows": total,
        "eligible_rows": eligible,
        "excluded_rows": total - eligible,
        "universe": universe,
    }


def _fact(projection, query, *, publication_notices=None, **kwargs) -> dict[str, Any]:
    fact = {
        "operation_id": projection.operation_id,
        "comparison_id": projection.scope.comparison_id,
        "expected_result_version": projection.scope.expected_result_version,
        "result_schema_version": projection.scope.result_schema_version,
        "catalog_version": CATALOG_VERSION,
        "query_id": query.query_id,
        "query_type": query.query_type,
        "filters": _public_filters(query.filters),
        "sort": query.sort,
        "page_offset": query.page_offset,
        "page_limit": query.page_limit,
        "source_type": "official_comparison_result",
        "table_id": kwargs.get("table_id"),
        "row_index": kwargs.get("row_index"),
        "population_total": kwargs.get("population_total"),
        "eligible_count": kwargs.get("eligible_count"),
        "comparable_count": kwargs.get("comparable_count"),
        "excluded_count": kwargs.get("excluded_count"),
        "completeness": kwargs.get("completeness"),
        "truncated": kwargs.get("truncated", False) is True,
        "data": kwargs.get("data"),
    }
    if publication_notices:
        fact["_publication_notices"] = list(publication_notices)
    return fact


def _public_filters(filters: Mapping[str, Any]) -> dict[str, Any]:
    published: dict[str, Any] = {}
    for key, value in filters.items():
        if isinstance(value, tuple):
            published[key] = list(value)
        else:
            published[key] = value
    return published


def _detail_facts(projection, query, page_items, *, item_payload, **kwargs):
    facts = [_fact(projection, query, **kwargs)]
    for item in page_items:
        if not isinstance(item, Mapping):
            continue
        payload = item_payload(item)
        notices = []
        if isinstance(payload, dict):
            notices = list(payload.pop("_publication_notices", []) or [])
        facts.append(
            _fact(
                projection,
                query,
                publication_notices=notices,
                table_id=item.get("table_id"),
                row_index=item.get("row_index"),
                population_total=kwargs.get("population_total"),
                eligible_count=kwargs.get("eligible_count"),
                comparable_count=kwargs.get("comparable_count"),
                excluded_count=kwargs.get("excluded_count"),
                completeness=item.get("completeness") or kwargs.get("completeness"),
                data=payload,
            )
        )
    return facts


def _carrier_fact_data(body: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "fact_role": "summary",
        "universe": body.get("universe"),
        "indicator_source": body.get("indicator_source"),
        "carriers": body.get("carriers"),
        "differences": body.get("differences"),
        "coverage": body.get("coverage"),
        "exclusions": body.get("exclusions"),
        "superiority_limit": body.get("superiority_limit"),
    }


def _component_fact(item: Mapping[str, Any]) -> dict[str, Any]:
    components = {}
    for code, slot in (item.get("components") or {}).items():
        if not isinstance(slot, Mapping):
            continue
        published = {
            "amount": slot.get("amount"),
            "amount_state": slot.get("amount_state"),
            "availability": slot.get("availability"),
            "source": slot.get("source"),
            "detail_level": slot.get("detail_level"),
        }
        if slot.get("numeric_support"):
            published["numeric_support"] = slot.get("numeric_support")
        components[str(code)] = published
    payload = {
        "fact_role": "detail",
        "row_index": item.get("row_index"),
        "table_id": item.get("table_id"),
        "origin": item.get("origin"),
        "completeness": item.get("completeness"),
        "final_amount": item.get("final_amount"),
        "partial_amount": item.get("partial_amount"),
        "amount_support": item.get("amount_support"),
        "components": components,
        "itemized_accessorials": item.get("itemized_accessorials"),
        "ignored_accessorials": item.get("ignored_accessorials"),
        "aggregated_buckets": item.get("aggregated_buckets"),
        "unresolved_component_refs": item.get("unresolved_component_refs"),
    }
    notices = item.get("publication_notices")
    if notices:
        payload["_publication_notices"] = list(notices)
    return payload


def _gap_fact(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "fact_role": "detail",
        "row_index": item.get("row_index"),
        "absolute_gap": item.get("absolute_gap"),
        "tie": item.get("tie"),
        "freights": item.get("freights"),
    }


def _uf_group_fact(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "fact_role": "detail",
        "id": item.get("id"),
        "row_count": item.get("row_count"),
        "comparable_row_count": item.get("comparable_row_count"),
        "excluded_row_count": item.get("excluded_row_count"),
        "potential_savings": item.get("potential_savings"),
        "coverage": item.get("coverage"),
        "scanned_rows": item.get("scanned_rows"),
        "universe": item.get("universe"),
    }


def _weight_band_fact(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "fact_role": "detail",
        "id": item.get("id"),
        "lower": item.get("lower"),
        "upper": item.get("upper"),
        "row_count": item.get("row_count"),
        "comparable_row_count": item.get("comparable_row_count"),
        "excluded_row_count": item.get("excluded_row_count"),
        "potential_savings": item.get("potential_savings"),
        "coverage": item.get("coverage"),
        "population_meaning": "rows_in_this_weight_group",
        "comparable_meaning": "fully_comparable_freights_inside_the_group",
    }


def _partial_fact(item: Mapping[str, Any]) -> dict[str, Any]:
    cells = []
    for cell in item.get("cells") or []:
        if not isinstance(cell, Mapping):
            continue
        cells.append(
            {
                "table_id": cell.get("table_id"),
                "completeness": cell.get("completeness"),
                "partial_amount": cell.get("partial_amount"),
                "final_amount": cell.get("final_amount"),
                "reasons": [
                    {"code": reason.get("code")}
                    for reason in cell.get("reasons") or []
                    if isinstance(reason, Mapping)
                ],
            }
        )
    return {"fact_role": "detail", "row_index": item.get("row_index"), "cells": cells}


def _diagnostic_fact(item: Mapping[str, Any]) -> dict[str, Any]:
    cells = []
    for cell in item.get("cells") or []:
        if not isinstance(cell, Mapping):
            continue
        constraint = cell.get("constraint") if isinstance(cell.get("constraint"), Mapping) else {}
        cells.append(
            {
                "table_id": cell.get("table_id"),
                "code": cell.get("code"),
                "preserved_codes": cell.get("preserved_codes"),
                "preserved_meaning": constraint.get("preserved_meaning"),
                "must_not_assert": list(constraint.get("must_not_assert") or []),
                "unresolved_diagnostic_refs": cell.get("unresolved_diagnostic_refs"),
            }
        )
    return {"fact_role": "detail", "row_index": item.get("row_index"), "cells": cells}


def _superiority_limit(universe: str) -> dict[str, Any]:
    return {
        "global_superiority": False,
        "universe": universe,
        "statement": "comparison_is_limited_to_the_stated_eligible_universe",
    }


def _query_error(query: ValidatedQuery, code: str) -> dict[str, Any]:
    return {
        "query_id": query.query_id,
        "query_type": query.query_type,
        "status": "error",
        "error_code": code,
        "population": None,
        "page": None,
        "body": None,
        "requested_fields": query.fields,
        "filters": _public_filters(query.filters),
        "sort": query.sort,
    }


def _publish_private_execution(
    projection,
    *,
    examined_rows: int,
    table_slots: dict[str, int],
    private_queries: list[dict[str, Any]],
    queries_complete: bool,
    plan_error: str | None,
    limits: QueryExecutionLimits,
) -> dict[str, Any]:
    """A única saída do executor é o documento selado pelo publicador."""

    try:
        published = publish_analytical_execution(
            operation_id=projection.operation_id,
            scope=projection.scope,
            examined_rows=examined_rows,
            table_slots=table_slots,
            private_queries=private_queries,
            queries_complete=queries_complete,
            plan_error=plan_error,
            limits=limits,
        )
    except QueryCatalogError:
        raise
    except Exception:
        published = publication_failure(
            operation_id=projection.operation_id,
            scope=projection.scope,
            examined_rows=examined_rows,
        )
    return copy.deepcopy(published)


@dataclass(frozen=True)
class _PrivateProjection:
    """Cópia de leitura da execução. Não é o dicionário entregue pelo chamador."""

    operation_id: str
    scope: AuthorizedComparisonScope
    result: dict[str, Any]
    memory_payload: dict[str, Any] | None
    official_analytics: dict[str, Any] | None
    result_checksum: str
    memory_checksum: str | None
    analytics_checksum: str | None


def _capture_private_projection(projection: AuthorizedAnalyticalProjection) -> _PrivateProjection:
    """Uma projeção privada por execução, conferida antes e depois da cópia.

    Se o original mudar enquanto a cópia é feita, a cópia e o snapshot deixam
    de ter o mesmo checksum e a execução é rejeitada.
    """

    _assert_snapshot_bound(projection)
    private_result = copy.deepcopy(projection.result)
    private_memory = (
        copy.deepcopy(projection.memory_payload) if projection.memory_payload is not None else None
    )
    private_analytics = (
        copy.deepcopy(projection.official_analytics)
        if projection.official_analytics is not None
        else None
    )
    try:
        _assert_snapshot_bound(projection)
    except QueryCatalogError as exc:
        raise QueryCatalogError("projection_mutated") from exc
    _assert_copy_bound(projection, private_result, private_memory, private_analytics)
    return _PrivateProjection(
        operation_id=projection.operation_id,
        scope=projection.scope,
        result=private_result,
        memory_payload=private_memory,
        official_analytics=private_analytics,
        result_checksum=projection.scope.result_checksum,
        memory_checksum=projection.scope.memory_checksum,
        analytics_checksum=getattr(projection, "_authorized_analytics_checksum", None),
    )


def _assert_copy_bound(projection, result, memory, analytics) -> None:
    if _checksum(result) != projection.scope.result_checksum:
        raise QueryCatalogError("projection_mutated")
    if projection.scope.memory_checksum is None:
        if memory is not None:
            raise QueryCatalogError("projection_mutated")
    elif memory is None or _checksum(memory) != projection.scope.memory_checksum:
        raise QueryCatalogError("projection_mutated")
    pinned = getattr(projection, "_authorized_analytics_checksum", None)
    if pinned is None:
        if analytics is not None:
            raise QueryCatalogError("projection_mutated")
        return
    if analytics is None or _checksum(analytics) != pinned:
        raise QueryCatalogError("projection_mutated")


def _assert_private_intact(private: _PrivateProjection) -> None:
    if _checksum(private.result) != private.result_checksum:
        raise QueryCatalogError("projection_mutated")
    if private.memory_checksum is None:
        if private.memory_payload is not None:
            raise QueryCatalogError("projection_mutated")
    elif private.memory_payload is None or _checksum(private.memory_payload) != private.memory_checksum:
        raise QueryCatalogError("projection_mutated")
    if private.analytics_checksum is None:
        if private.official_analytics is not None:
            raise QueryCatalogError("projection_mutated")
        return
    if private.official_analytics is None or _checksum(private.official_analytics) != private.analytics_checksum:
        raise QueryCatalogError("projection_mutated")


def _checksum(value: Any) -> str:
    try:
        return artifact_checksum(value)
    except QueryCatalogError as exc:
        raise QueryCatalogError("invalid_projection") from exc


def _assert_snapshot_bound(projection: AuthorizedAnalyticalProjection) -> None:
    """A execução só segue se o payload ainda for o snapshot autorizado."""

    if _checksum(projection.result) != projection.scope.result_checksum:
        raise QueryCatalogError("snapshot_mismatch")
    if projection.scope.memory_checksum is None:
        if projection.memory_payload is not None:
            raise QueryCatalogError("snapshot_mismatch")
    elif (
        projection.memory_payload is None
        or _checksum(projection.memory_payload) != projection.scope.memory_checksum
    ):
        raise QueryCatalogError("snapshot_mismatch")
    pinned = getattr(projection, "_authorized_analytics_checksum", None)
    if projection.official_analytics is None:
        if pinned is not None:
            raise QueryCatalogError("snapshot_mismatch")
        return
    if _checksum(projection.official_analytics) != pinned:
        raise QueryCatalogError("snapshot_mismatch")


def _assert_projection_intact(projection: AuthorizedAnalyticalProjection) -> None:
    try:
        _assert_snapshot_bound(projection)
    except QueryCatalogError as exc:
        raise QueryCatalogError("projection_mutated") from exc


class _Budget:
    def __init__(self, clock: Callable[[], float], limit: float):
        self.clock = clock
        self.limit = float(limit)
        self.start = self.clock()

    def exceeded(self) -> bool:
        return (self.clock() - self.start) > self.limit


def _validate_result_shape(result: Mapping[str, Any], *, scope: AuthorizedComparisonScope) -> None:
    schema = result.get("schema_version")
    if schema not in {1, 2} or schema != scope.result_schema_version:
        raise QueryCatalogError("snapshot_mismatch")
    if _checksum(result) != scope.result_checksum:
        raise QueryCatalogError("snapshot_mismatch")
    try:
        comparison_id = canonical_comparison_id(result.get("comparison_id"))
    except Exception as exc:
        raise QueryCatalogError("snapshot_mismatch") from exc
    if comparison_id != scope.comparison_id:
        raise QueryCatalogError("snapshot_mismatch")
    tables = result.get("tables")
    rows = result.get("comparative_rows")
    if not isinstance(tables, list) or not isinstance(rows, list):
        raise QueryCatalogError("invalid_projection")
    if result.get("table_count") != len(tables) or result.get("table_count") not in {2, 3}:
        raise QueryCatalogError("invalid_projection")
    if result.get("row_count") != len(rows):
        raise QueryCatalogError("invalid_projection")
    seen_tables = set()
    seen_rows: set[int] = set()
    for table in tables:
        if not isinstance(table, Mapping):
            raise QueryCatalogError("invalid_projection")
        table_id = table.get("table_id")
        slot = table.get("slot_number")
        carrier = table.get("carrier_name")
        if not isinstance(table_id, str) or not table_id or table_id in seen_tables:
            raise QueryCatalogError("invalid_projection")
        if type(slot) is not int or slot < 1 or not isinstance(carrier, str):
            raise QueryCatalogError("invalid_projection")
        seen_tables.add(table_id)
    for row in rows:
        row_index = row.get("row_index") if isinstance(row, Mapping) else None
        if type(row_index) is not int or row_index < 1 or row_index in seen_rows:
            raise QueryCatalogError("invalid_projection")
        seen_rows.add(row_index)
        if not isinstance(row.get("table_results"), Mapping):
            raise QueryCatalogError("invalid_projection")


def _validate_memory_payload(memory: Mapping[str, Any], *, scope: AuthorizedComparisonScope) -> None:
    """Exige identidade, versão e checksum já autorizados. Não cria checksum novo."""

    if scope.memory_checksum is None or scope.memory_schema_version is None:
        raise QueryCatalogError("snapshot_mismatch")
    try:
        comparison_id = canonical_comparison_id(memory.get("comparison_id"))
    except Exception as exc:
        raise QueryCatalogError("snapshot_mismatch") from exc
    if comparison_id != scope.comparison_id:
        raise QueryCatalogError("snapshot_mismatch")
    schema_version = memory.get("schema_version")
    if type(schema_version) is not int or schema_version != scope.memory_schema_version:
        raise QueryCatalogError("snapshot_mismatch")
    if _checksum(memory) != scope.memory_checksum:
        raise QueryCatalogError("snapshot_mismatch")
    items = memory.get("items")
    if not isinstance(items, Mapping):
        return
    for item in items.values():
        if not isinstance(item, Mapping) or "comparison_id" not in item:
            continue
        try:
            item_comparison = canonical_comparison_id(item.get("comparison_id"))
        except Exception as exc:
            raise QueryCatalogError("snapshot_mismatch") from exc
        if item_comparison != scope.comparison_id:
            raise QueryCatalogError("snapshot_mismatch")


def _validate_official_analytics(analytics, *, result, scope) -> None:
    if analytics.get("schema_version") != 2:
        raise QueryCatalogError("snapshot_mismatch")
    if analytics.get("row_count") != result.get("row_count"):
        raise QueryCatalogError("snapshot_mismatch")
    try:
        comparison_id = canonical_comparison_id(analytics.get("comparison_id"))
    except Exception as exc:
        raise QueryCatalogError("snapshot_mismatch") from exc
    if comparison_id != scope.comparison_id:
        raise QueryCatalogError("snapshot_mismatch")
    for key in (
        "global_summary",
        "tables",
        "comparability",
        "competitive_summary",
        "carrier_competitiveness",
        "geography",
    ):
        if key not in analytics:
            raise QueryCatalogError("official_analytics_unavailable")


authorize_analytical_projection = AuthorizedAnalyticalProjection


def _canonical_operation_id(value: Any) -> str:
    if not isinstance(value, str):
        raise QueryCatalogError("invalid_projection")
    try:
        parsed = UUID(value.strip())
    except (ValueError, AttributeError, TypeError) as exc:
        raise QueryCatalogError("invalid_projection") from exc
    return str(parsed)


def _cell_status(cell: Mapping[str, Any] | None) -> str | None:
    if not isinstance(cell, Mapping):
        return None
    for key in ("final_status", "completeness_status", "status"):
        raw = cell.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def _completeness(cell) -> str:
    if _is_complete(cell):
        return "complete"
    if _is_incomplete(cell):
        return "partial"
    return "not_calculated"


def _is_complete(cell) -> bool:
    if _cell_status(cell) not in _COMPLETE_STATUSES:
        return False
    return _money_decimal(None if cell is None else cell.get("calculated_freight")) is not None


def _is_incomplete(cell) -> bool:
    if not isinstance(cell, Mapping):
        return False
    if _cell_status(cell) == "incomplete":
        return True
    return bool(cell.get("is_partial_value")) and not _is_complete(cell)


def _canonical_name(item: Mapping[str, Any]) -> str | None:
    for key in ("canonical_component", "component_group", "component_code"):
        raw = item.get(key)
        if isinstance(raw, str) and raw in CANONICAL_COMPONENT_CODES:
            return CANONICAL_COMPONENT_CODES[raw]
    return None


def _money_decimal(value: Any) -> Decimal | None:
    """Aceita o mesmo contrato numérico do analytics oficial: int ou float finito.

    ``Decimal`` não é convertido. O serviço oficial não o trata como número
    finito, então uma conversão aqui fabricaria competitividade.
    """

    if value is None or isinstance(value, bool) or isinstance(value, str) or type(value) is Decimal:
        return None
    if isinstance(value, int):
        amount = Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            return None
        amount = Decimal(str(round(value, 2)))
    else:
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return amount.quantize(_MONEY)


def _format_money(amount: Decimal) -> str:
    return f"{amount.quantize(_MONEY):.2f}"


def _amount_state(amount: Decimal | None) -> str:
    if amount is None:
        return "absent"
    if amount == 0:
        return "zero"
    return "present"


def _percent(part: int, total: int) -> float | None:
    if total <= 0:
        return None
    return round((part / total) * 100.0, 1)


def _token_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().casefold()
    if not text or len(text) > 64 or text[0].isdigit():
        return None
    if any(char not in _TOKEN_CHARS for char in text):
        return None
    return text


def _public_text(
    value: Any,
    *,
    limit: int,
    notices: list[dict[str, Any]] | None = None,
    category: str = "evidence_text",
) -> str | None:
    """Texto livre permanece fora da projeção derivada.

    Rótulo, mensagem e número de documento não entram no schema público.
    A assinatura permanece para os chamadores já existentes.
    """

    del value, limit, notices, category
    return None

