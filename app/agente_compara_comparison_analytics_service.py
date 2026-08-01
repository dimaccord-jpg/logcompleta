"""
Serviço isolado de analytics para comparação multitabela do AgenteCompara.

Recebe o resultado comparativo validado e produz payload serializável de indicadores.
Não persiste estado, não integra serviços externos de cobrança ou IA e não muta o resultado de entrada.

Universos:
- total: todas as linhas operacionais;
- coberto por tabela: linhas com cálculo completo daquela transportadora;
- comparável: linhas em que todas as tabelas participantes possuem cálculo completo.

Competitividade de custo (vitórias, médias, economia, geografia de vencedora)
usa exclusivamente o universo comparável.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any

logger = logging.getLogger(__name__)

# Schema do resultado comparativo de entrada (não confundir com analytics).
RESULT_SCHEMA_VERSION = 1
ANALYTICS_SCHEMA_VERSION = 2

# UFs com menos documentos comparáveis que este limiar recebem low_sample=true.
LOW_SAMPLE_COMPARABLE_THRESHOLD = 5

STATUS_CALCULATED = "calculated"
STATUS_CALCULATED_WITH_WARNINGS = "calculated_with_warnings"
STATUS_INCOMPLETE = "incomplete"
STATUS_NOT_CALCULATED = "not_calculated"

# Alinhado a _COMPLETE_ROW_STATUSES do motor unitário / completeza.
_COMPLETE_STATUSES = frozenset(
    {
        STATUS_CALCULATED,
        STATUS_CALCULATED_WITH_WARNINGS,
    }
)

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


def _cell_status(cell: Any) -> str | None:
    if not isinstance(cell, dict):
        return None
    for key in ("final_status", "completeness_status", "status"):
        raw = cell.get(key)
        if raw is None:
            continue
        text = str(raw).strip()
        if text:
            return text
    return None


def _is_complete_cell(cell: Any) -> bool:
    """Cálculo completo utilizável (calculated ou calculated_with_warnings + frete finito)."""
    if not isinstance(cell, dict):
        return False
    status = _cell_status(cell)
    if status not in _COMPLETE_STATUSES:
        return False
    return _is_finite_number(cell.get("calculated_freight"))


def _is_incomplete_cell(cell: Any) -> bool:
    if not isinstance(cell, dict):
        return False
    status = _cell_status(cell)
    if status == STATUS_INCOMPLETE:
        return True
    return bool(cell.get("is_partial_value")) and not _is_complete_cell(cell)


def _is_calculated_cell(cell: Any) -> bool:
    """Compatível com consumidores legados: célula com cálculo completo."""
    return _is_complete_cell(cell)


def _cell_freight(cell: dict) -> float:
    return float(cell["calculated_freight"])


def _classify_cell(cell: Any) -> str:
    """Retorna complete | incomplete | not_calculated."""
    if _is_complete_cell(cell):
        return "complete"
    if _is_incomplete_cell(cell):
        return "incomplete"
    return "not_calculated"


def _normalize_destination_uf(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip().upper()
    return text or None


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
    if schema_version != RESULT_SCHEMA_VERSION:
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
    calculated_with_warnings_rows = 0
    incomplete_rows = 0
    not_calculated_rows = 0
    freight_total = 0.0
    weight_processed = 0.0
    routes: set[tuple[str, str]] = set()
    destination_ufs: set[str] = set()
    destination_cities: set[str] = set()

    for row in comparative_rows:
        cell = row.get("table_results", {}).get(table_id)
        kind = _classify_cell(cell)
        if kind == "complete":
            calculated_rows += 1
            status = _cell_status(cell)
            if status == STATUS_CALCULATED_WITH_WARNINGS:
                calculated_with_warnings_rows += 1
            freight = _cell_freight(cell)  # type: ignore[arg-type]
            freight_total += freight
            if _is_finite_number(row.get("weight")):
                weight_processed += float(row["weight"])
            dest_city = str(row.get("destination_city") or "").strip()
            dest_uf = str(row.get("destination_uf") or "").strip()
            if dest_city or dest_uf:
                routes.add((dest_city, dest_uf))
                if dest_uf:
                    destination_ufs.add(dest_uf.upper())
                if dest_city:
                    destination_cities.add(dest_city)
        elif kind == "incomplete":
            incomplete_rows += 1
        else:
            not_calculated_rows += 1

    error_rows = incomplete_rows + not_calculated_rows
    calculated_freight_total = _round_money(freight_total) if calculated_rows > 0 else None
    calculated_freight_average = (
        _round_money(_safe_div(freight_total, float(calculated_rows))) if calculated_rows > 0 else None
    )
    total_weight_processed = _round_money(weight_processed) if calculated_rows > 0 else None

    calculated_freight_per_kg = None
    if calculated_rows > 0 and weight_processed > 0:
        calculated_freight_per_kg = _round_money(_safe_div(freight_total, weight_processed))

    without_complete = incomplete_rows + not_calculated_rows

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
        "calculated_with_warnings_rows": calculated_with_warnings_rows,
        "incomplete_rows": incomplete_rows,
        "not_calculated_rows": not_calculated_rows,
        "error_rows": error_rows,
        "uncalculated_rows": error_rows,
        "rows_without_complete_calculation": without_complete,
        "coverage_percentage": _pct(calculated_rows, row_count),
        "error_percentage": _pct(error_rows, row_count),
        "route_count": len(routes),
        "destination_uf_count": len(destination_ufs),
        "destination_city_count": len(destination_cities),
    }


def _money_key(value: float) -> float:
    """Chave monetária oficial (2 casas) para empates e ordenação."""
    return round(float(value), 2)


def _evaluate_comparable_row(
    *,
    row: dict,
    table_ids: list[str],
) -> dict[str, Any] | None:
    """
    Avalia uma linha totalmente comparável.

    Retorna None se a linha não for totalmente comparável.
    """
    freights: list[tuple[str, float]] = []
    for table_id in table_ids:
        cell = row.get("table_results", {}).get(table_id)
        if not _is_complete_cell(cell):
            return None
        freights.append((table_id, _money_key(_cell_freight(cell))))  # type: ignore[arg-type]

    ordered = sorted(freights, key=lambda item: (item[1], item[0]))
    min_value = ordered[0][1]
    winners = [table_id for table_id, value in freights if value == min_value]
    is_tie = len(winners) > 1

    second_value = None
    gap_absolute = None
    gap_percentage = None
    potential_savings = None
    if not is_tie and len(ordered) >= 2:
        second_value = ordered[1][1]
        gap_absolute = _round_money(second_value - min_value)
        potential_savings = gap_absolute
        if min_value != 0:
            gap_percentage = _round_pct(((second_value - min_value) / abs(min_value)) * 100.0)
        else:
            gap_percentage = None if second_value == 0 else _round_pct(100.0)

    return {
        "freights": {table_id: value for table_id, value in freights},
        "min_value": min_value,
        "winner_table_ids": winners,
        "is_tie": is_tie,
        "second_value": second_value,
        "gap_absolute": gap_absolute,
        "gap_percentage": gap_percentage,
        "potential_savings": potential_savings,
    }


def _row_comparability_class(*, row: dict, table_ids: list[str]) -> str:
    complete_count = 0
    for table_id in table_ids:
        cell = row.get("table_results", {}).get(table_id)
        if _is_complete_cell(cell):
            complete_count += 1
    if complete_count == len(table_ids):
        return "fully_comparable"
    if complete_count == 0:
        return "inconclusive"
    return "partially_comparable"


def _build_executive_analytics(
    *,
    tables: list[dict],
    display_names: dict[str, str],
    comparative_rows: list[dict],
) -> dict[str, Any]:
    table_ids = [table["table_id"] for table in tables]
    total_rows = len(comparative_rows)

    # Acumuladores por transportadora (cobertura + competitividade).
    carrier_stats: dict[str, dict[str, Any]] = {}
    for table in tables:
        tid = table["table_id"]
        carrier_stats[tid] = {
            "table_id": tid,
            "slot_number": int(table["slot_number"]),
            "display_name": display_names[tid],
            "carrier_name": str(table.get("carrier_name") or "").strip(),
            "calculated_rows": 0,
            "calculated_with_warnings_rows": 0,
            "incomplete_rows": 0,
            "not_calculated_rows": 0,
            "wins": 0,
            "ties": 0,
            "comparable_freight_total": 0.0,
            "comparable_weight_total": 0.0,
            "comparable_row_count": 0,
            "potential_savings_when_winner": 0.0,
        }

    fully_comparable = 0
    partially_comparable = 0
    inconclusive = 0
    tie_count = 0
    decisive_row_count = 0
    total_potential_savings = 0.0
    gap_pct_sum = 0.0
    gap_pct_n = 0

    # Geografia: um passe linear.
    geo: dict[str, dict[str, Any]] = {}

    def _ensure_uf(uf_key: str) -> dict[str, Any]:
        if uf_key not in geo:
            geo[uf_key] = {
                "uf": uf_key,
                "row_count": 0,
                "comparable_row_count": 0,
                "tie_count": 0,
                "total_potential_savings": 0.0,
                "wins": {tid: 0 for tid in table_ids},
                "comparable_freight_total": {tid: 0.0 for tid in table_ids},
                "comparable_weight_total": {tid: 0.0 for tid in table_ids},
                "coverage_complete": {tid: 0 for tid in table_ids},
            }
        return geo[uf_key]

    for row in comparative_rows:
        uf_key = _normalize_destination_uf(row.get("destination_uf")) or "N/D"
        uf_bucket = _ensure_uf(uf_key)
        uf_bucket["row_count"] += 1

        table_results = row.get("table_results", {})
        for tid in table_ids:
            cell = table_results.get(tid)
            kind = _classify_cell(cell)
            stats = carrier_stats[tid]
            if kind == "complete":
                stats["calculated_rows"] += 1
                uf_bucket["coverage_complete"][tid] += 1
                if _cell_status(cell) == STATUS_CALCULATED_WITH_WARNINGS:
                    stats["calculated_with_warnings_rows"] += 1
            elif kind == "incomplete":
                stats["incomplete_rows"] += 1
            else:
                stats["not_calculated_rows"] += 1

        cmp_class = _row_comparability_class(row=row, table_ids=table_ids)
        if cmp_class == "fully_comparable":
            fully_comparable += 1
        elif cmp_class == "partially_comparable":
            partially_comparable += 1
            continue
        else:
            inconclusive += 1
            continue

        evaluation = _evaluate_comparable_row(row=row, table_ids=table_ids)
        if evaluation is None:
            # Defesa: não deveria ocorrer após fully_comparable.
            inconclusive += 1
            fully_comparable -= 1
            continue

        uf_bucket["comparable_row_count"] += 1
        weight = float(row["weight"]) if _is_finite_number(row.get("weight")) else 0.0

        for tid in table_ids:
            freight = evaluation["freights"][tid]
            carrier_stats[tid]["comparable_freight_total"] += freight
            carrier_stats[tid]["comparable_row_count"] += 1
            if weight > 0:
                carrier_stats[tid]["comparable_weight_total"] += weight
            uf_bucket["comparable_freight_total"][tid] += freight
            if weight > 0:
                uf_bucket["comparable_weight_total"][tid] += weight

        if evaluation["is_tie"]:
            tie_count += 1
            uf_bucket["tie_count"] += 1
            for tid in evaluation["winner_table_ids"]:
                carrier_stats[tid]["ties"] += 1
        else:
            decisive_row_count += 1
            winner_tid = evaluation["winner_table_ids"][0]
            carrier_stats[winner_tid]["wins"] += 1
            uf_bucket["wins"][winner_tid] += 1
            savings = evaluation["potential_savings"] or 0.0
            total_potential_savings += savings
            carrier_stats[winner_tid]["potential_savings_when_winner"] += savings
            uf_bucket["total_potential_savings"] += savings
            if evaluation["gap_percentage"] is not None:
                gap_pct_sum += float(evaluation["gap_percentage"])
                gap_pct_n += 1

    comparability = {
        "total_rows": total_rows,
        "fully_comparable_rows": fully_comparable,
        "partially_comparable_rows": partially_comparable,
        "inconclusive_rows": inconclusive,
        "fully_comparable_percentage": _pct(fully_comparable, total_rows),
        "partially_comparable_percentage": _pct(partially_comparable, total_rows),
        "inconclusive_percentage": _pct(inconclusive, total_rows),
    }

    competitive_summary = {
        "comparable_row_count": fully_comparable,
        "tie_count": tie_count,
        "decisive_row_count": decisive_row_count,
        "total_potential_savings": _round_money(total_potential_savings) if decisive_row_count > 0 else 0.0,
        "average_potential_savings": (
            _round_money(_safe_div(total_potential_savings, float(decisive_row_count)))
            if decisive_row_count > 0
            else None
        ),
        "average_gap_percentage": _round_pct(_safe_div(gap_pct_sum, float(gap_pct_n))) if gap_pct_n > 0 else None,
        "win_percentage_denominator": "decisive_row_count",
        "potential_savings_definition": (
            "Diferença entre a menor tarifa calculada e a segunda menor, "
            "apenas em documentos totalmente comparáveis com vencedora única."
        ),
    }

    carrier_competitiveness: list[dict[str, Any]] = []
    for table in tables:
        tid = table["table_id"]
        stats = carrier_stats[tid]
        complete = int(stats["calculated_rows"])
        freight_total = float(stats["comparable_freight_total"])
        weight_total = float(stats["comparable_weight_total"])
        comparable_n = int(stats["comparable_row_count"])
        wins = int(stats["wins"])
        carrier_competitiveness.append(
            {
                "table_id": tid,
                "slot_number": int(stats["slot_number"]),
                "display_name": stats["display_name"],
                "carrier_name": stats["carrier_name"],
                "calculated_rows": complete,
                "calculated_with_warnings_rows": int(stats["calculated_with_warnings_rows"]),
                "incomplete_rows": int(stats["incomplete_rows"]),
                "not_calculated_rows": int(stats["not_calculated_rows"]),
                "rows_without_complete_calculation": int(stats["incomplete_rows"])
                + int(stats["not_calculated_rows"]),
                "coverage_percentage": _pct(complete, total_rows),
                "wins": wins,
                "ties": int(stats["ties"]),
                "win_percentage": _pct(wins, decisive_row_count) if decisive_row_count > 0 else None,
                "tie_percentage": _pct(int(stats["ties"]), fully_comparable) if fully_comparable > 0 else None,
                "comparable_freight_total": _round_money(freight_total) if comparable_n > 0 else None,
                "comparable_freight_average": (
                    _round_money(_safe_div(freight_total, float(comparable_n))) if comparable_n > 0 else None
                ),
                "comparable_weight_total": _round_money(weight_total) if comparable_n > 0 else None,
                "comparable_freight_per_kg_average": (
                    _round_money(_safe_div(freight_total, weight_total))
                    if comparable_n > 0 and weight_total > 0
                    else None
                ),
                "comparable_row_count": comparable_n,
                "potential_savings_when_winner": _round_money(float(stats["potential_savings_when_winner"])),
            }
        )

    destination_ufs: list[dict[str, Any]] = []
    for uf_key in sorted(geo.keys()):
        bucket = geo[uf_key]
        comparable_n = int(bucket["comparable_row_count"])
        uf_tables: list[dict[str, Any]] = []
        for table in tables:
            tid = table["table_id"]
            freight_total = float(bucket["comparable_freight_total"][tid])
            weight_total = float(bucket["comparable_weight_total"][tid])
            wins = int(bucket["wins"][tid])
            uf_tables.append(
                {
                    "table_id": tid,
                    "slot_number": int(table["slot_number"]),
                    "display_name": display_names[tid],
                    "wins": wins,
                    "win_percentage": _pct(wins, comparable_n - int(bucket["tie_count"]))
                    if (comparable_n - int(bucket["tie_count"])) > 0
                    else (_pct(wins, comparable_n) if comparable_n > 0 else None),
                    "comparable_freight_total": _round_money(freight_total) if comparable_n > 0 else None,
                    "comparable_freight_average": (
                        _round_money(_safe_div(freight_total, float(comparable_n))) if comparable_n > 0 else None
                    ),
                    "comparable_freight_per_kg_average": (
                        _round_money(_safe_div(freight_total, weight_total))
                        if comparable_n > 0 and weight_total > 0
                        else None
                    ),
                    "complete_rows": int(bucket["coverage_complete"][tid]),
                    "coverage_percentage": _pct(int(bucket["coverage_complete"][tid]), int(bucket["row_count"])),
                }
            )

        winner_table_id = None
        winner_display_name = None
        winner_wins = None
        winner_share = None
        uf_is_tie = False

        if comparable_n > 0:
            max_wins = max(int(bucket["wins"][tid]) for tid in table_ids)
            top = [tid for tid in table_ids if int(bucket["wins"][tid]) == max_wins]
            if max_wins == 0:
                # Apenas empates na UF.
                uf_is_tie = True
            elif len(top) == 1:
                winner_table_id = top[0]
                winner_display_name = display_names[winner_table_id]
                winner_wins = max_wins
                decisive_uf = comparable_n - int(bucket["tie_count"])
                winner_share = _pct(max_wins, decisive_uf) if decisive_uf > 0 else _pct(max_wins, comparable_n)
            else:
                # Desempate: menor custo médio comparável.
                averages: list[tuple[float, str]] = []
                for tid in top:
                    avg = _safe_div(
                        float(bucket["comparable_freight_total"][tid]),
                        float(comparable_n),
                    )
                    if avg is None:
                        averages.append((float("inf"), tid))
                    else:
                        averages.append((_money_key(avg), tid))
                averages.sort(key=lambda item: (item[0], item[1]))
                best_avg = averages[0][0]
                avg_winners = [tid for avg, tid in averages if avg == best_avg]
                if len(avg_winners) == 1 and best_avg != float("inf"):
                    winner_table_id = avg_winners[0]
                    winner_display_name = display_names[winner_table_id]
                    winner_wins = int(bucket["wins"][winner_table_id])
                    decisive_uf = comparable_n - int(bucket["tie_count"])
                    winner_share = (
                        _pct(winner_wins, decisive_uf) if decisive_uf > 0 else _pct(winner_wins, comparable_n)
                    )
                else:
                    uf_is_tie = True

        has_comparable_base = comparable_n > 0
        if not has_comparable_base:
            map_status = "no_comparable_base"
        elif uf_is_tie or not winner_table_id:
            map_status = "tie"
        else:
            map_status = "winner"

        destination_ufs.append(
            {
                "uf": uf_key if uf_key != "N/D" else None,
                "uf_label": uf_key,
                "row_count": int(bucket["row_count"]),
                "comparable_row_count": comparable_n,
                "comparable_percentage": _pct(comparable_n, int(bucket["row_count"])),
                "winner_table_id": winner_table_id,
                "winner_display_name": winner_display_name,
                "winner_wins": winner_wins,
                "winner_share": winner_share,
                "is_tie": uf_is_tie,
                "tie_count": int(bucket["tie_count"]),
                "total_potential_savings": _round_money(float(bucket["total_potential_savings"]))
                if comparable_n > 0
                else 0.0,
                "average_potential_savings": (
                    _round_money(
                        _safe_div(
                            float(bucket["total_potential_savings"]),
                            float(comparable_n - int(bucket["tie_count"])),
                        )
                    )
                    if (comparable_n - int(bucket["tie_count"])) > 0
                    else None
                ),
                "low_sample": comparable_n > 0 and comparable_n < LOW_SAMPLE_COMPARABLE_THRESHOLD,
                "has_comparable_base": has_comparable_base,
                "map_status": map_status,
                "tables": uf_tables,
            }
        )

    # Ranking por economia potencial (todas as UFs no analytics; UI limita top 10).
    uf_potential_ranking = sorted(
        [
            {
                "uf": item["uf"],
                "uf_label": item["uf_label"],
                "winner_table_id": item["winner_table_id"],
                "winner_display_name": item["winner_display_name"],
                "comparable_row_count": item["comparable_row_count"],
                "total_potential_savings": item["total_potential_savings"],
                "average_potential_savings": item["average_potential_savings"],
                "low_sample": item["low_sample"],
                "has_comparable_base": item["has_comparable_base"],
                "is_tie": item["is_tie"],
            }
            for item in destination_ufs
            if item["has_comparable_base"]
        ],
        key=lambda item: (
            -(float(item["total_potential_savings"] or 0.0)),
            -int(item["comparable_row_count"] or 0),
            str(item["uf_label"] or ""),
        ),
    )

    lead_table_id = None
    lead_display_name = None
    lead_wins = None
    lead_win_percentage = None
    if decisive_row_count > 0:
        ordered_carriers = sorted(
            carrier_competitiveness,
            key=lambda item: (-int(item["wins"]), int(item["slot_number"])),
        )
        top_wins = int(ordered_carriers[0]["wins"])
        leaders = [item for item in ordered_carriers if int(item["wins"]) == top_wins]
        if len(leaders) == 1 and top_wins > 0:
            lead_table_id = leaders[0]["table_id"]
            lead_display_name = leaders[0]["display_name"]
            lead_wins = top_wins
            lead_win_percentage = leaders[0]["win_percentage"]

    ufs_with_comparable = sum(1 for item in destination_ufs if item["has_comparable_base"])
    rows_without_complete_total = sum(
        int(item["rows_without_complete_calculation"]) for item in carrier_competitiveness
    )

    executive_summary = {
        "table_count": len(tables),
        "row_count": total_rows,
        "fully_comparable_rows": fully_comparable,
        "fully_comparable_percentage": comparability["fully_comparable_percentage"],
        "lead_table_id": lead_table_id,
        "lead_display_name": lead_display_name,
        "lead_wins": lead_wins,
        "lead_win_percentage": lead_win_percentage,
        "total_potential_savings": competitive_summary["total_potential_savings"],
        "rows_without_complete_calculation": rows_without_complete_total,
        "ufs_with_comparable_base": ufs_with_comparable,
        "decisive_row_count": decisive_row_count,
        "tie_count": tie_count,
    }

    return {
        "comparability": comparability,
        "competitive_summary": competitive_summary,
        "carrier_competitiveness": carrier_competitiveness,
        "geography": {
            "destination_ufs": destination_ufs,
            "uf_potential_ranking": uf_potential_ranking,
            "low_sample_threshold": LOW_SAMPLE_COMPARABLE_THRESHOLD,
            "ufs_with_comparable_base": ufs_with_comparable,
            "uf_count": len(destination_ufs),
        },
        "executive_summary": executive_summary,
    }


def build_comparison_analytics(result: dict) -> dict:
    """
    Constrói payload serializável de analytics a partir de um resultado comparativo validado.

    Não muta ``result``; não persiste estado; não recomenda transportadora contratual.
    Indicadores competitivos usam apenas o universo comparável.
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
            if _is_complete_cell(cell):
                calculated_cells += 1
    error_cells = total_cells - calculated_cells

    table_analytics = [
        _build_table_analytics(
            table=table,
            display_name=display_names[table["table_id"]],
            comparative_rows=comparative_rows,
        )
        for table in tables
    ]

    executive = _build_executive_analytics(
        tables=tables,
        display_names=display_names,
        comparative_rows=comparative_rows,
    )

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
        "tables": table_analytics,
        "comparability": executive["comparability"],
        "competitive_summary": executive["competitive_summary"],
        "carrier_competitiveness": executive["carrier_competitiveness"],
        "geography": executive["geography"],
        "executive_summary": executive["executive_summary"],
    }

    _assert_no_forbidden_keys(payload)

    duration_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "analytics_generated comparison_id=%s table_count=%s row_count=%s "
        "fully_comparable=%s duration_ms=%s",
        comparison_id,
        table_count,
        row_count,
        executive["comparability"]["fully_comparable_rows"],
        duration_ms,
    )

    return payload
