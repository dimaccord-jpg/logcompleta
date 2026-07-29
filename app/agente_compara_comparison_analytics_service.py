"""
Serviço isolado de analytics para comparação multitabela do AgenteCompara.

Recebe o resultado comparativo validado e produz payload serializável de indicadores.
Não persiste estado, não integra serviços externos de cobrança ou IA e não muta o resultado de entrada.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any

logger = logging.getLogger(__name__)

ANALYTICS_SCHEMA_VERSION = 1

_FORBIDDEN_KEYS = frozenset(
    {
        "valor_frete",
        "charged_freight",
        "expected_freight",
        "freight_charged",
        "difference",
        "divergence",
        "overcharged",
        "undercharged",
        "winner",
        "winning_carrier",
        "cheapest_carrier",
        "best_carrier",
        "worst_carrier",
        "savings",
        "economy",
        "ranking",
        "recommendation",
        "recommended_carrier",
    }
)


class AgenteComparaComparisonAnalyticsError(Exception):
    """Erro de validação ou processamento do analytics comparativo."""


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return math.isfinite(float(value))
    return False


def _round_money(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 2)


def _round_pct(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 1)


def _safe_div(numerator: float, denominator: float) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator


def _pct(part: int, total: int) -> float | None:
    ratio = _safe_div(float(part), float(total))
    if ratio is None:
        return None
    return _round_pct(ratio * 100.0)


def _is_calculated_cell(cell: Any) -> bool:
    if not isinstance(cell, dict):
        return False
    return cell.get("status") == "calculated" and _is_finite_number(cell.get("calculated_freight"))


def _cell_freight(cell: dict) -> float:
    return float(cell["calculated_freight"])


def _row_issue_date(row: dict) -> str | None:
    for key in ("issue_date", "data_emissao"):
        raw = row.get(key)
        if raw is None:
            continue
        text = str(raw).strip()
        if text:
            return text
    return None


def _assert_no_forbidden_keys(node: Any, *, path: str = "") -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _FORBIDDEN_KEYS:
                raise AgenteComparaComparisonAnalyticsError(
                    f"Campo proibido detectado: {path}.{key}" if path else f"Campo proibido detectado: {key}"
                )
            child_path = f"{path}.{key}" if path else key
            _assert_no_forbidden_keys(value, path=child_path)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _assert_no_forbidden_keys(item, path=f"{path}[{index}]")


def _validate_input(result: Any) -> dict:
    if not isinstance(result, dict):
        raise AgenteComparaComparisonAnalyticsError("Resultado deve ser um dict.")

    schema_version = result.get("schema_version")
    if schema_version != ANALYTICS_SCHEMA_VERSION:
        raise AgenteComparaComparisonAnalyticsError("schema_version inválido.")

    comparison_id = result.get("comparison_id")
    if not isinstance(comparison_id, str) or not comparison_id.strip():
        raise AgenteComparaComparisonAnalyticsError("comparison_id inválido.")

    table_count = result.get("table_count")
    if table_count not in (2, 3):
        raise AgenteComparaComparisonAnalyticsError("table_count deve ser 2 ou 3.")

    tables = result.get("tables")
    if not isinstance(tables, list) or len(tables) != table_count:
        raise AgenteComparaComparisonAnalyticsError("tables inválido.")

    comparative_rows = result.get("comparative_rows")
    if not isinstance(comparative_rows, list):
        raise AgenteComparaComparisonAnalyticsError("comparative_rows inválido.")

    row_count = result.get("row_count")
    if not isinstance(row_count, int) or row_count != len(comparative_rows):
        raise AgenteComparaComparisonAnalyticsError("row_count inconsistente.")

    seen_slots: set[int] = set()
    seen_table_ids: set[str] = set()
    for table in tables:
        if not isinstance(table, dict):
            raise AgenteComparaComparisonAnalyticsError("Entrada de tables inválida.")
        table_id = table.get("table_id")
        slot_number = table.get("slot_number")
        carrier_name = table.get("carrier_name")
        if not isinstance(table_id, str) or not table_id.strip():
            raise AgenteComparaComparisonAnalyticsError("table_id inválido.")
        if not isinstance(slot_number, int) or slot_number < 1:
            raise AgenteComparaComparisonAnalyticsError("slot_number inválido.")
        if not isinstance(carrier_name, str):
            raise AgenteComparaComparisonAnalyticsError("carrier_name inválido.")
        if slot_number in seen_slots or table_id in seen_table_ids:
            raise AgenteComparaComparisonAnalyticsError("tables duplicadas.")
        seen_slots.add(slot_number)
        seen_table_ids.add(table_id)

    for row in comparative_rows:
        if not isinstance(row, dict):
            raise AgenteComparaComparisonAnalyticsError("comparative_rows contém item inválido.")
        table_results = row.get("table_results")
        if not isinstance(table_results, dict):
            raise AgenteComparaComparisonAnalyticsError("table_results inválido.")

    return result


def _build_display_names(tables: list[dict]) -> dict[str, str]:
    carrier_counts: dict[str, int] = {}
    for table in tables:
        name = str(table.get("carrier_name") or "").strip()
        carrier_counts[name] = carrier_counts.get(name, 0) + 1

    display_names: dict[str, str] = {}
    for table in tables:
        table_id = table["table_id"]
        name = str(table.get("carrier_name") or "").strip()
        slot_number = int(table["slot_number"])
        if carrier_counts.get(name, 0) > 1:
            display_names[table_id] = f"{name} — Tabela {slot_number}"
        else:
            display_names[table_id] = name
    return display_names


def _build_global_summary(
    *,
    comparative_rows: list[dict],
    row_count: int,
    total_cells: int,
    calculated_cells: int,
    error_cells: int,
) -> dict[str, Any]:
    doc_numbers = [
        str(row.get("document_number")).strip()
        for row in comparative_rows
        if row.get("document_number") is not None and str(row.get("document_number")).strip()
    ]
    if doc_numbers:
        document_count = len(set(doc_numbers))
    else:
        document_count = row_count

    weights = [float(row["weight"]) for row in comparative_rows if _is_finite_number(row.get("weight"))]
    total_weight = _round_money(sum(weights)) if weights else None

    invoice_values = [
        float(row["invoice_value"])
        for row in comparative_rows
        if _is_finite_number(row.get("invoice_value"))
    ]
    total_invoice_value = _round_money(sum(invoice_values)) if invoice_values else None

    issue_dates = [_row_issue_date(row) for row in comparative_rows]
    issue_dates = [value for value in issue_dates if value]
    period_start = min(issue_dates) if issue_dates else None
    period_end = max(issue_dates) if issue_dates else None

    return {
        "document_count": document_count,
        "total_weight": total_weight,
        "total_invoice_value": total_invoice_value,
        "total_cells": total_cells,
        "calculated_cells": calculated_cells,
        "error_cells": error_cells,
        "calculation_coverage_percentage": _pct(calculated_cells, total_cells),
        "period_start": period_start,
        "period_end": period_end,
    }


def _build_table_analytics(
    *,
    table: dict,
    display_name: str,
    comparative_rows: list[dict],
) -> dict[str, Any]:
    table_id = table["table_id"]
    row_count = len(comparative_rows)
    calculated_rows = 0
    error_rows = 0
    freight_total = 0.0
    weight_processed = 0.0
    routes: set[tuple[str, str]] = set()
    destination_ufs: set[str] = set()
    destination_cities: set[str] = set()

    for row in comparative_rows:
        cell = row.get("table_results", {}).get(table_id)
        if _is_calculated_cell(cell):
            calculated_rows += 1
            freight = _cell_freight(cell)  # type: ignore[arg-type]
            freight_total += freight
            if _is_finite_number(row.get("weight")):
                weight_processed += float(row["weight"])
            dest_city = str(row.get("destination_city") or "").strip()
            dest_uf = str(row.get("destination_uf") or "").strip()
            if dest_city or dest_uf:
                routes.add((dest_city, dest_uf))
                if dest_uf:
                    destination_ufs.add(dest_uf)
                if dest_city:
                    destination_cities.add(dest_city)
        else:
            error_rows += 1

    calculated_freight_total = _round_money(freight_total) if calculated_rows > 0 else None
    calculated_freight_average = (
        _round_money(_safe_div(freight_total, float(calculated_rows))) if calculated_rows > 0 else None
    )
    total_weight_processed = _round_money(weight_processed) if calculated_rows > 0 else None

    calculated_freight_per_kg = None
    if calculated_rows > 0 and weight_processed > 0:
        calculated_freight_per_kg = _round_money(_safe_div(freight_total, weight_processed))

    return {
        "table_id": table_id,
        "slot_number": int(table["slot_number"]),
        "carrier_name": str(table.get("carrier_name") or "").strip(),
        "display_name": display_name,
        "calculated_freight_total": calculated_freight_total,
        "calculated_freight_average": calculated_freight_average,
        "total_weight_processed": total_weight_processed,
        "calculated_freight_per_kg": calculated_freight_per_kg,
        "row_count": row_count,
        "calculated_rows": calculated_rows,
        "error_rows": error_rows,
        "uncalculated_rows": error_rows,
        "coverage_percentage": _pct(calculated_rows, row_count),
        "error_percentage": _pct(error_rows, row_count),
        "route_count": len(routes),
        "destination_uf_count": len(destination_ufs),
        "destination_city_count": len(destination_cities),
    }


def build_comparison_analytics(result: dict) -> dict:
    """
    Constrói payload serializável de analytics a partir de um resultado comparativo validado.

    Não muta ``result``; não persiste estado; não define vencedor ou recomendação.
    """
    started = time.perf_counter()
    validated = _validate_input(result)

    comparison_id = validated["comparison_id"].strip()
    table_count = int(validated["table_count"])
    row_count = int(validated["row_count"])
    tables = sorted(validated["tables"], key=lambda item: int(item["slot_number"]))
    comparative_rows = validated["comparative_rows"]

    display_names = _build_display_names(tables)

    total_cells = table_count * row_count
    calculated_cells = 0
    for row in comparative_rows:
        table_results = row.get("table_results", {})
        for table in tables:
            cell = table_results.get(table["table_id"])
            if _is_calculated_cell(cell):
                calculated_cells += 1
    error_cells = total_cells - calculated_cells

    payload: dict[str, Any] = {
        "schema_version": ANALYTICS_SCHEMA_VERSION,
        "comparison_id": comparison_id,
        "table_count": table_count,
        "row_count": row_count,
        "global_summary": _build_global_summary(
            comparative_rows=comparative_rows,
            row_count=row_count,
            total_cells=total_cells,
            calculated_cells=calculated_cells,
            error_cells=error_cells,
        ),
        "tables": [
            _build_table_analytics(
                table=table,
                display_name=display_names[table["table_id"]],
                comparative_rows=comparative_rows,
            )
            for table in tables
        ],
    }

    _assert_no_forbidden_keys(payload)

    duration_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "analytics_generated comparison_id=%s table_count=%s row_count=%s duration_ms=%s",
        comparison_id,
        table_count,
        row_count,
        duration_ms,
    )

    return payload
