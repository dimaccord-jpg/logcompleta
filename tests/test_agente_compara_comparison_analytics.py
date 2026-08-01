"""Testes do serviço de analytics comparativo do AgenteCompara."""
from __future__ import annotations

import copy
import json
import math
import pathlib
from typing import Any

import pytest

from app.agente_compara_comparison_analytics_service import (
    ANALYTICS_SCHEMA_VERSION,
    AgenteComparaComparisonAnalyticsError,
    build_comparison_analytics,
)

FORBIDDEN_KEYS = {
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


def _table_meta(
    *,
    table_id: str,
    slot_number: int,
    carrier_name: str,
    temp_table_id: str | None = None,
) -> dict:
    return {
        "table_id": table_id,
        "temp_table_id": temp_table_id or f"temp-{table_id}",
        "slot_number": slot_number,
        "carrier_name": carrier_name,
        "calculated_count": 0,
        "error_count": 0,
        "summary": {"total_calculated_freight": None, "calculated_count": 0, "error_count": 0},
    }


def _cell(
    *,
    table_id: str,
    carrier_name: str,
    slot_number: int,
    calculated_freight: float | None,
    status: str = "calculated",
    error: dict | None = None,
) -> dict:
    return {
        "table_id": table_id,
        "carrier_name": carrier_name,
        "slot_number": slot_number,
        "calculated_freight": calculated_freight,
        "status": status,
        "error": error,
    }


def _row(
    *,
    row_index: int,
    document_number: str,
    destination_city: str,
    destination_uf: str,
    weight: float,
    table_cells: dict[str, dict],
    invoice_value: float | None = None,
    issue_date: str | None = None,
    data_emissao: str | None = None,
) -> dict:
    payload: dict[str, Any] = {
        "row_index": row_index,
        "document_number": document_number,
        "destination_city": destination_city,
        "destination_uf": destination_uf,
        "weight": weight,
        "table_results": table_cells,
    }
    if invoice_value is not None:
        payload["invoice_value"] = invoice_value
    if issue_date is not None:
        payload["issue_date"] = issue_date
    if data_emissao is not None:
        payload["data_emissao"] = data_emissao
    return payload


def _build_result(
    *,
    tables: list[dict],
    comparative_rows: list[dict],
    comparison_id: str = "cmp-analytics-001",
) -> dict:
    return {
        "schema_version": 1,
        "comparison_id": comparison_id,
        "execution_id": "exec-001",
        "table_count": len(tables),
        "row_count": len(comparative_rows),
        "tables": tables,
        "results_by_table": {},
        "comparative_rows": comparative_rows,
        "summary": {},
    }


def _assert_no_forbidden_keys_recursive(node: Any, *, path: str = "") -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            assert key not in FORBIDDEN_KEYS, f"Campo proibido em {path}.{key}"
            child = f"{path}.{key}" if path else key
            _assert_no_forbidden_keys_recursive(value, path=child)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _assert_no_forbidden_keys_recursive(item, path=f"{path}[{index}]")


def _assert_no_nan_inf_recursive(node: Any) -> None:
    if isinstance(node, float):
        assert math.isfinite(node), f"Valor não finito: {node}"
    elif isinstance(node, dict):
        for value in node.values():
            _assert_no_nan_inf_recursive(value)
    elif isinstance(node, list):
        for item in node:
            _assert_no_nan_inf_recursive(item)


def test_two_tables_mixed_errors_independent_totals():
    t1 = _table_meta(table_id="t1", slot_number=1, carrier_name="Carrier A")
    t2 = _table_meta(table_id="t2", slot_number=2, carrier_name="Carrier B")
    rows = [
        _row(
            row_index=1,
            document_number="DOC-1",
            destination_city="Campinas",
            destination_uf="SP",
            weight=10,
            invoice_value=100,
            issue_date="2024-01-15",
            table_cells={
                "t1": _cell(table_id="t1", carrier_name="Carrier A", slot_number=1, calculated_freight=50.0),
                "t2": _cell(table_id="t2", carrier_name="Carrier B", slot_number=2, calculated_freight=60.0),
            },
        ),
        _row(
            row_index=2,
            document_number="DOC-2",
            destination_city="Rio de Janeiro",
            destination_uf="RJ",
            weight=20,
            invoice_value=200,
            issue_date="2024-02-20",
            table_cells={
                "t1": _cell(table_id="t1", carrier_name="Carrier A", slot_number=1, calculated_freight=80.0),
                "t2": _cell(
                    table_id="t2",
                    carrier_name="Carrier B",
                    slot_number=2,
                    calculated_freight=None,
                    status="missing_rule",
                    error={"code": "missing_rule"},
                ),
            },
        ),
    ]
    result = build_comparison_analytics(_build_result(tables=[t1, t2], comparative_rows=rows))

    assert result["schema_version"] == ANALYTICS_SCHEMA_VERSION
    assert result["comparison_id"] == "cmp-analytics-001"
    assert result["table_count"] == 2
    assert result["row_count"] == 2

    gs = result["global_summary"]
    assert gs["document_count"] == 2
    assert gs["total_weight"] == 30.0
    assert gs["total_invoice_value"] == 300.0
    assert gs["total_cells"] == 4
    assert gs["calculated_cells"] == 3
    assert gs["error_cells"] == 1
    assert gs["calculation_coverage_percentage"] == 75.0
    assert gs["period_start"] == "2024-01-15"
    assert gs["period_end"] == "2024-02-20"

    by_slot = {item["slot_number"]: item for item in result["tables"]}
    assert [item["slot_number"] for item in result["tables"]] == [1, 2]

    t1_stats = by_slot[1]
    assert t1_stats["calculated_freight_total"] == 130.0
    assert t1_stats["calculated_freight_average"] == 65.0
    assert t1_stats["total_weight_processed"] == 30.0
    assert t1_stats["calculated_freight_per_kg"] == 4.33
    assert t1_stats["calculated_rows"] == 2
    assert t1_stats["error_rows"] == 0
    assert t1_stats["uncalculated_rows"] == 0
    assert t1_stats["coverage_percentage"] == 100.0
    assert t1_stats["error_percentage"] == 0.0
    assert t1_stats["route_count"] == 2
    assert t1_stats["destination_uf_count"] == 2
    assert t1_stats["destination_city_count"] == 2
    assert t1_stats["display_name"] == "Carrier A"

    t2_stats = by_slot[2]
    assert t2_stats["calculated_freight_total"] == 60.0
    assert t2_stats["calculated_freight_average"] == 60.0
    assert t2_stats["total_weight_processed"] == 10.0
    assert t2_stats["calculated_freight_per_kg"] == 6.0
    assert t2_stats["calculated_rows"] == 1
    assert t2_stats["error_rows"] == 1
    assert t2_stats["coverage_percentage"] == 50.0
    assert t2_stats["error_percentage"] == 50.0
    assert t2_stats["route_count"] == 1


def test_three_tables_all_calculated():
    tables = [
        _table_meta(table_id="t1", slot_number=1, carrier_name="Alpha"),
        _table_meta(table_id="t2", slot_number=2, carrier_name="Beta"),
        _table_meta(table_id="t3", slot_number=3, carrier_name="Gamma"),
    ]
    rows = [
        _row(
            row_index=1,
            document_number="X1",
            destination_city="Curitiba",
            destination_uf="PR",
            weight=5,
            table_cells={
                "t1": _cell(table_id="t1", carrier_name="Alpha", slot_number=1, calculated_freight=10.0),
                "t2": _cell(table_id="t2", carrier_name="Beta", slot_number=2, calculated_freight=12.0),
                "t3": _cell(table_id="t3", carrier_name="Gamma", slot_number=3, calculated_freight=11.0),
            },
        ),
        _row(
            row_index=2,
            document_number="X2",
            destination_city="Curitiba",
            destination_uf="PR",
            weight=15,
            table_cells={
                "t1": _cell(table_id="t1", carrier_name="Alpha", slot_number=1, calculated_freight=20.0),
                "t2": _cell(table_id="t2", carrier_name="Beta", slot_number=2, calculated_freight=24.0),
                "t3": _cell(table_id="t3", carrier_name="Gamma", slot_number=3, calculated_freight=22.0),
            },
        ),
    ]
    analytics = build_comparison_analytics(_build_result(tables=tables, comparative_rows=rows))

    assert analytics["table_count"] == 3
    assert analytics["global_summary"]["total_cells"] == 6
    assert analytics["global_summary"]["calculated_cells"] == 6
    assert analytics["global_summary"]["error_cells"] == 0
    assert analytics["global_summary"]["calculation_coverage_percentage"] == 100.0

    alpha = next(item for item in analytics["tables"] if item["table_id"] == "t1")
    beta = next(item for item in analytics["tables"] if item["table_id"] == "t2")
    gamma = next(item for item in analytics["tables"] if item["table_id"] == "t3")

    assert alpha["calculated_freight_total"] == 30.0
    assert alpha["calculated_freight_average"] == 15.0
    assert alpha["total_weight_processed"] == 20.0
    assert alpha["calculated_freight_per_kg"] == 1.5
    assert beta["calculated_freight_total"] == 36.0
    assert gamma["calculated_freight_total"] == 33.0
    assert alpha["route_count"] == 1
    assert alpha["destination_city_count"] == 1
    assert alpha["destination_uf_count"] == 1


def test_weight_zero_freight_per_kg_null():
    tables = [
        _table_meta(table_id="t1", slot_number=1, carrier_name="A"),
        _table_meta(table_id="t2", slot_number=2, carrier_name="B"),
    ]
    rows = [
        _row(
            row_index=1,
            document_number="Z",
            destination_city="São Paulo",
            destination_uf="SP",
            weight=0,
            table_cells={
                "t1": _cell(table_id="t1", carrier_name="A", slot_number=1, calculated_freight=25.0),
                "t2": _cell(table_id="t2", carrier_name="B", slot_number=2, calculated_freight=30.0),
            },
        ),
    ]
    analytics = build_comparison_analytics(_build_result(tables=tables, comparative_rows=rows))
    t1 = analytics["tables"][0]
    assert t1["calculated_freight_total"] == 25.0
    assert t1["total_weight_processed"] == 0.0
    assert t1["calculated_freight_per_kg"] is None


def test_no_calculated_rows():
    tables = [
        _table_meta(table_id="t1", slot_number=1, carrier_name="A"),
        _table_meta(table_id="t2", slot_number=2, carrier_name="B"),
    ]
    rows = [
        _row(
            row_index=1,
            document_number="E1",
            destination_city="Salvador",
            destination_uf="BA",
            weight=8,
            table_cells={
                "t1": _cell(
                    table_id="t1",
                    carrier_name="A",
                    slot_number=1,
                    calculated_freight=None,
                    status="error",
                    error={"code": "x"},
                ),
                "t2": _cell(
                    table_id="t2",
                    carrier_name="B",
                    slot_number=2,
                    calculated_freight=None,
                    status="error",
                    error={"code": "x"},
                ),
            },
        ),
    ]
    analytics = build_comparison_analytics(_build_result(tables=tables, comparative_rows=rows))

    assert analytics["global_summary"]["calculated_cells"] == 0
    assert analytics["global_summary"]["calculation_coverage_percentage"] == 0.0
    for table_stats in analytics["tables"]:
        assert table_stats["calculated_freight_total"] is None
        assert table_stats["calculated_freight_average"] is None
        assert table_stats["total_weight_processed"] is None
        assert table_stats["calculated_freight_per_kg"] is None
        assert table_stats["calculated_rows"] == 0
        assert table_stats["error_rows"] == 1
        assert table_stats["coverage_percentage"] == 0.0
        assert table_stats["route_count"] == 0


def test_duplicate_documents_use_unique_document_count():
    tables = [
        _table_meta(table_id="t1", slot_number=1, carrier_name="A"),
        _table_meta(table_id="t2", slot_number=2, carrier_name="B"),
    ]
    rows = [
        _row(
            row_index=1,
            document_number="DUP",
            destination_city="A",
            destination_uf="SP",
            weight=1,
            table_cells={
                "t1": _cell(table_id="t1", carrier_name="A", slot_number=1, calculated_freight=1.0),
                "t2": _cell(table_id="t2", carrier_name="B", slot_number=2, calculated_freight=2.0),
            },
        ),
        _row(
            row_index=2,
            document_number="DUP",
            destination_city="B",
            destination_uf="RJ",
            weight=2,
            table_cells={
                "t1": _cell(table_id="t1", carrier_name="A", slot_number=1, calculated_freight=3.0),
                "t2": _cell(table_id="t2", carrier_name="B", slot_number=2, calculated_freight=4.0),
            },
        ),
    ]
    analytics = build_comparison_analytics(_build_result(tables=tables, comparative_rows=rows))
    assert analytics["row_count"] == 2
    assert analytics["global_summary"]["document_count"] == 1


def test_duplicate_cities_unique_routes():
    tables = [
        _table_meta(table_id="t1", slot_number=1, carrier_name="A"),
        _table_meta(table_id="t2", slot_number=2, carrier_name="B"),
    ]
    rows = [
        _row(
            row_index=1,
            document_number="R1",
            destination_city="Campinas",
            destination_uf="SP",
            weight=10,
            table_cells={
                "t1": _cell(table_id="t1", carrier_name="A", slot_number=1, calculated_freight=10.0),
                "t2": _cell(table_id="t2", carrier_name="B", slot_number=2, calculated_freight=11.0),
            },
        ),
        _row(
            row_index=2,
            document_number="R2",
            destination_city="Campinas",
            destination_uf="SP",
            weight=12,
            table_cells={
                "t1": _cell(table_id="t1", carrier_name="A", slot_number=1, calculated_freight=12.0),
                "t2": _cell(table_id="t2", carrier_name="B", slot_number=2, calculated_freight=13.0),
            },
        ),
        _row(
            row_index=3,
            document_number="R3",
            destination_city="Santos",
            destination_uf="SP",
            weight=8,
            table_cells={
                "t1": _cell(table_id="t1", carrier_name="A", slot_number=1, calculated_freight=8.0),
                "t2": _cell(table_id="t2", carrier_name="B", slot_number=2, calculated_freight=9.0),
            },
        ),
    ]
    analytics = build_comparison_analytics(_build_result(tables=tables, comparative_rows=rows))
    t1 = analytics["tables"][0]
    assert t1["route_count"] == 2
    assert t1["destination_city_count"] == 2
    assert t1["destination_uf_count"] == 1


def test_same_carrier_name_disambiguation():
    tables = [
        _table_meta(table_id="t1", slot_number=1, carrier_name="Transportes XYZ"),
        _table_meta(table_id="t2", slot_number=2, carrier_name="Transportes XYZ"),
    ]
    rows = [
        _row(
            row_index=1,
            document_number="D",
            destination_city="X",
            destination_uf="SP",
            weight=1,
            table_cells={
                "t1": _cell(table_id="t1", carrier_name="Transportes XYZ", slot_number=1, calculated_freight=1.0),
                "t2": _cell(table_id="t2", carrier_name="Transportes XYZ", slot_number=2, calculated_freight=2.0),
            },
        ),
    ]
    analytics = build_comparison_analytics(_build_result(tables=tables, comparative_rows=rows))
    names = {item["slot_number"]: item["display_name"] for item in analytics["tables"]}
    assert names[1] == "Transportes XYZ — Tabela 1"
    assert names[2] == "Transportes XYZ — Tabela 2"


def test_invoice_value_absent_total_invoice_null():
    tables = [
        _table_meta(table_id="t1", slot_number=1, carrier_name="A"),
        _table_meta(table_id="t2", slot_number=2, carrier_name="B"),
    ]
    rows = [
        _row(
            row_index=1,
            document_number="N1",
            destination_city="X",
            destination_uf="SP",
            weight=3,
            table_cells={
                "t1": _cell(table_id="t1", carrier_name="A", slot_number=1, calculated_freight=5.0),
                "t2": _cell(table_id="t2", carrier_name="B", slot_number=2, calculated_freight=6.0),
            },
        ),
    ]
    analytics = build_comparison_analytics(_build_result(tables=tables, comparative_rows=rows))
    assert analytics["global_summary"]["total_invoice_value"] is None


def test_issue_date_absent_period_null():
    tables = [
        _table_meta(table_id="t1", slot_number=1, carrier_name="A"),
        _table_meta(table_id="t2", slot_number=2, carrier_name="B"),
    ]
    rows = [
        _row(
            row_index=1,
            document_number="P1",
            destination_city="X",
            destination_uf="SP",
            weight=3,
            table_cells={
                "t1": _cell(table_id="t1", carrier_name="A", slot_number=1, calculated_freight=5.0),
                "t2": _cell(table_id="t2", carrier_name="B", slot_number=2, calculated_freight=6.0),
            },
        ),
    ]
    analytics = build_comparison_analytics(_build_result(tables=tables, comparative_rows=rows))
    assert analytics["global_summary"]["period_start"] is None
    assert analytics["global_summary"]["period_end"] is None


def test_issue_date_from_data_emissao():
    tables = [
        _table_meta(table_id="t1", slot_number=1, carrier_name="A"),
        _table_meta(table_id="t2", slot_number=2, carrier_name="B"),
    ]
    rows = [
        _row(
            row_index=1,
            document_number="P1",
            destination_city="X",
            destination_uf="SP",
            weight=3,
            data_emissao="2023-05-10",
            table_cells={
                "t1": _cell(table_id="t1", carrier_name="A", slot_number=1, calculated_freight=5.0),
                "t2": _cell(table_id="t2", carrier_name="B", slot_number=2, calculated_freight=6.0),
            },
        ),
        _row(
            row_index=2,
            document_number="P2",
            destination_city="Y",
            destination_uf="RJ",
            weight=4,
            data_emissao="2023-08-01",
            table_cells={
                "t1": _cell(table_id="t1", carrier_name="A", slot_number=1, calculated_freight=7.0),
                "t2": _cell(table_id="t2", carrier_name="B", slot_number=2, calculated_freight=8.0),
            },
        ),
    ]
    analytics = build_comparison_analytics(_build_result(tables=tables, comparative_rows=rows))
    assert analytics["global_summary"]["period_start"] == "2023-05-10"
    assert analytics["global_summary"]["period_end"] == "2023-08-01"


def test_no_nan_or_infinity_recursively():
    analytics = build_comparison_analytics(
        _build_result(
            tables=[
                _table_meta(table_id="t1", slot_number=1, carrier_name="A"),
                _table_meta(table_id="t2", slot_number=2, carrier_name="B"),
            ],
            comparative_rows=[
                _row(
                    row_index=1,
                    document_number="F",
                    destination_city="X",
                    destination_uf="SP",
                    weight=1,
                    table_cells={
                        "t1": _cell(table_id="t1", carrier_name="A", slot_number=1, calculated_freight=1.0),
                        "t2": _cell(table_id="t2", carrier_name="B", slot_number=2, calculated_freight=2.0),
                    },
                ),
            ],
        )
    )
    _assert_no_nan_inf_recursive(analytics)


def test_input_not_mutated():
    source = _build_result(
        tables=[
            _table_meta(table_id="t1", slot_number=1, carrier_name="A"),
            _table_meta(table_id="t2", slot_number=2, carrier_name="B"),
        ],
        comparative_rows=[
            _row(
                row_index=1,
                document_number="M1",
                destination_city="X",
                destination_uf="SP",
                weight=2,
                table_cells={
                    "t1": _cell(table_id="t1", carrier_name="A", slot_number=1, calculated_freight=4.0),
                    "t2": _cell(table_id="t2", carrier_name="B", slot_number=2, calculated_freight=5.0),
                },
            ),
        ],
    )
    before = copy.deepcopy(source)
    build_comparison_analytics(source)
    assert source == before


def test_deterministic_output():
    payload = _build_result(
        tables=[
            _table_meta(table_id="t1", slot_number=1, carrier_name="A"),
            _table_meta(table_id="t2", slot_number=2, carrier_name="B"),
        ],
        comparative_rows=[
            _row(
                row_index=1,
                document_number="D1",
                destination_city="X",
                destination_uf="SP",
                weight=2,
                table_cells={
                    "t1": _cell(table_id="t1", carrier_name="A", slot_number=1, calculated_freight=4.0),
                    "t2": _cell(table_id="t2", carrier_name="B", slot_number=2, calculated_freight=5.0),
                },
            ),
        ],
    )
    first = build_comparison_analytics(copy.deepcopy(payload))
    second = build_comparison_analytics(copy.deepcopy(payload))
    assert first == second


def test_no_forbidden_keys_recursively():
    analytics = build_comparison_analytics(
        _build_result(
            tables=[
                _table_meta(table_id="t1", slot_number=1, carrier_name="A"),
                _table_meta(table_id="t2", slot_number=2, carrier_name="B"),
            ],
            comparative_rows=[
                _row(
                    row_index=1,
                    document_number="K1",
                    destination_city="X",
                    destination_uf="SP",
                    weight=2,
                    table_cells={
                        "t1": _cell(table_id="t1", carrier_name="A", slot_number=1, calculated_freight=4.0),
                        "t2": _cell(table_id="t2", carrier_name="B", slot_number=2, calculated_freight=5.0),
                    },
                ),
            ],
        )
    )
    _assert_no_forbidden_keys_recursive(analytics)


def test_module_has_no_billing_or_gemini_imports():
    source = pathlib.Path("app/agente_compara_comparison_analytics_service.py").read_text(encoding="utf-8")
    lowered = source.lower()
    assert "gemini" not in lowered
    assert "billing" not in lowered
    assert "cleiton_governed_generate_content" not in source


def test_strict_json_serialization():
    analytics = build_comparison_analytics(
        _build_result(
            tables=[
                _table_meta(table_id="t1", slot_number=1, carrier_name="A"),
                _table_meta(table_id="t2", slot_number=2, carrier_name="B"),
            ],
            comparative_rows=[
                _row(
                    row_index=1,
                    document_number="J1",
                    destination_city="X",
                    destination_uf="SP",
                    weight=2,
                    invoice_value=50,
                    issue_date="2024-03-01",
                    table_cells={
                        "t1": _cell(table_id="t1", carrier_name="A", slot_number=1, calculated_freight=4.0),
                        "t2": _cell(table_id="t2", carrier_name="B", slot_number=2, calculated_freight=5.0),
                    },
                ),
            ],
        )
    )
    encoded = json.dumps(analytics, allow_nan=False)
    decoded = json.loads(encoded)
    assert decoded["global_summary"]["total_invoice_value"] == 50.0


def test_mutation_proof_independent_totals():
    tables = [
        _table_meta(table_id="t1", slot_number=1, carrier_name="A"),
        _table_meta(table_id="t2", slot_number=2, carrier_name="B"),
    ]
    base_rows = [
        _row(
            row_index=1,
            document_number="M1",
            destination_city="X",
            destination_uf="SP",
            weight=10,
            table_cells={
                "t1": _cell(table_id="t1", carrier_name="A", slot_number=1, calculated_freight=100.0),
                "t2": _cell(table_id="t2", carrier_name="B", slot_number=2, calculated_freight=200.0),
            },
        ),
    ]
    baseline = build_comparison_analytics(_build_result(tables=tables, comparative_rows=base_rows))
    assert baseline["tables"][0]["calculated_freight_total"] == 100.0

    mutated_rows = copy.deepcopy(base_rows)
    mutated_rows[0]["table_results"]["t1"]["calculated_freight"] = 150.0
    mutated = build_comparison_analytics(_build_result(tables=tables, comparative_rows=mutated_rows))
    assert mutated["tables"][0]["calculated_freight_total"] == 150.0
    assert mutated["tables"][0]["calculated_freight_average"] == 150.0
    assert mutated["tables"][0]["calculated_freight_per_kg"] == 15.0
    assert baseline["tables"][0]["calculated_freight_total"] == 100.0


def test_invalid_input_raises():
    with pytest.raises(AgenteComparaComparisonAnalyticsError):
        build_comparison_analytics({"schema_version": 2})
    with pytest.raises(AgenteComparaComparisonAnalyticsError):
        build_comparison_analytics(
            {
                "schema_version": 1,
                "comparison_id": "x",
                "table_count": 1,
                "row_count": 0,
                "tables": [],
                "comparative_rows": [],
            }
        )


def test_analytics_performance_2000x2_and_2000x3():
    import time

    def _big(table_count: int):
        tables = [
            {
                "table_id": f"t{i}",
                "temp_table_id": f"tt{i}",
                "slot_number": i,
                "carrier_name": f"Carrier {i}",
                "calculated_count": 2000,
                "error_count": 0,
                "summary": {"total_calculated_freight": 2000.0 * i, "calculated_count": 2000, "error_count": 0},
            }
            for i in range(1, table_count + 1)
        ]
        rows = []
        for idx in range(2000):
            table_results = {
                t["table_id"]: {
                    "table_id": t["table_id"],
                    "carrier_name": t["carrier_name"],
                    "slot_number": t["slot_number"],
                    "calculated_freight": float(10 + idx % 7),
                    "status": "calculated",
                    "error": None,
                }
                for t in tables
            }
            rows.append(
                {
                    "row_index": idx,
                    "document_number": f"D{idx}",
                    "destination_city": f"Cidade {idx % 50}",
                    "destination_uf": ["SP", "RJ", "MG", "PR"][idx % 4],
                    "weight": 1.0 + (idx % 9),
                    "invoice_value": 100.0 + idx,
                    "table_results": table_results,
                }
            )
        return _build_result(tables=tables, comparative_rows=rows)

    started = time.perf_counter()
    out2 = build_comparison_analytics(_big(2))
    elapsed2 = time.perf_counter() - started
    assert out2["row_count"] == 2000
    assert out2["table_count"] == 2
    assert elapsed2 < 2.5

    started = time.perf_counter()
    out3 = build_comparison_analytics(_big(3))
    elapsed3 = time.perf_counter() - started
    assert out3["row_count"] == 2000
    assert out3["table_count"] == 3
    assert elapsed3 < 3.5

def _incomplete_cell(*, table_id: str, carrier_name: str, slot_number: int, partial: float | None = 10.0) -> dict:
    return {
        "table_id": table_id,
        "carrier_name": carrier_name,
        "slot_number": slot_number,
        "calculated_freight": partial,
        "status": "incomplete",
        "final_status": "incomplete",
        "is_partial_value": True,
        "error": {"code": "incomplete"},
    }


def _not_calculated_cell(*, table_id: str, carrier_name: str, slot_number: int) -> dict:
    return {
        "table_id": table_id,
        "carrier_name": carrier_name,
        "slot_number": slot_number,
        "calculated_freight": None,
        "status": "not_calculated",
        "final_status": "not_calculated",
        "error": {"code": "not_calculated"},
    }


def _warnings_cell(*, table_id: str, carrier_name: str, slot_number: int, calculated_freight: float) -> dict:
    return {
        "table_id": table_id,
        "carrier_name": carrier_name,
        "slot_number": slot_number,
        "calculated_freight": calculated_freight,
        "status": "calculated_with_warnings",
        "final_status": "calculated_with_warnings",
        "error": None,
        "warnings": [{"code": "warn"}],
    }


def test_executive_schema_version_and_legacy_fields_preserved():
    tables = [
        _table_meta(table_id="t1", slot_number=1, carrier_name="A"),
        _table_meta(table_id="t2", slot_number=2, carrier_name="B"),
    ]
    rows = [
        _row(
            row_index=1,
            document_number="D1",
            destination_city="X",
            destination_uf="sp",
            weight=10,
            table_cells={
                "t1": _cell(table_id="t1", carrier_name="A", slot_number=1, calculated_freight=50.0),
                "t2": _cell(table_id="t2", carrier_name="B", slot_number=2, calculated_freight=60.0),
            },
        ),
    ]
    analytics = build_comparison_analytics(_build_result(tables=tables, comparative_rows=rows))
    assert analytics["schema_version"] == 2
    assert "global_summary" in analytics
    assert "tables" in analytics
    assert "comparability" in analytics
    assert "competitive_summary" in analytics
    assert "carrier_competitiveness" in analytics
    assert "geography" in analytics
    assert "executive_summary" in analytics


def test_comparability_universe_fixture_four_documents():
    """Fixture obrigatória: 4 docs / 2 transportadoras com universos mistos."""
    t1 = _table_meta(table_id="t1", slot_number=1, carrier_name="Carrier A")
    t2 = _table_meta(table_id="t2", slot_number=2, carrier_name="Carrier B")
    rows = [
        # Doc 1: ambas calculam — A vence
        _row(
            row_index=1,
            document_number="DOC-1",
            destination_city="Campinas",
            destination_uf="SP",
            weight=10,
            table_cells={
                "t1": _cell(table_id="t1", carrier_name="Carrier A", slot_number=1, calculated_freight=100.0),
                "t2": _cell(table_id="t2", carrier_name="Carrier B", slot_number=2, calculated_freight=120.0),
            },
        ),
        # Doc 2: apenas A calcula — parcial
        _row(
            row_index=2,
            document_number="DOC-2",
            destination_city="Santos",
            destination_uf="SP",
            weight=8,
            table_cells={
                "t1": _cell(table_id="t1", carrier_name="Carrier A", slot_number=1, calculated_freight=40.0),
                "t2": _not_calculated_cell(table_id="t2", carrier_name="Carrier B", slot_number=2),
            },
        ),
        # Doc 3: ambas calculam — B vence
        _row(
            row_index=3,
            document_number="DOC-3",
            destination_city="Niterói",
            destination_uf="RJ",
            weight=5,
            table_cells={
                "t1": _cell(table_id="t1", carrier_name="Carrier A", slot_number=1, calculated_freight=90.0),
                "t2": _cell(table_id="t2", carrier_name="Carrier B", slot_number=2, calculated_freight=70.0),
            },
        ),
        # Doc 4: nenhuma calcula — inconclusivo
        _row(
            row_index=4,
            document_number="DOC-4",
            destination_city="Salvador",
            destination_uf="BA",
            weight=12,
            table_cells={
                "t1": _not_calculated_cell(table_id="t1", carrier_name="Carrier A", slot_number=1),
                "t2": _incomplete_cell(table_id="t2", carrier_name="Carrier B", slot_number=2, partial=15.0),
            },
        ),
    ]
    analytics = build_comparison_analytics(_build_result(tables=[t1, t2], comparative_rows=rows))
    cmp_ = analytics["comparability"]
    assert cmp_["total_rows"] == 4
    assert cmp_["fully_comparable_rows"] == 2
    assert cmp_["partially_comparable_rows"] == 1
    assert cmp_["inconclusive_rows"] == 1

    by_id = {item["table_id"]: item for item in analytics["carrier_competitiveness"]}
    assert by_id["t1"]["wins"] == 1
    assert by_id["t2"]["wins"] == 1
    assert by_id["t1"]["comparable_row_count"] == 2
    assert by_id["t2"]["comparable_row_count"] == 2
    # Cobertura individual: A completa em 3 linhas (docs 1-3), B em 2 (docs 1 e 3)
    assert by_id["t1"]["calculated_rows"] == 3
    assert by_id["t2"]["calculated_rows"] == 2
    # Custo médio comparável usa somente docs 1 e 3
    assert by_id["t1"]["comparable_freight_average"] == 95.0  # (100+90)/2
    assert by_id["t2"]["comparable_freight_average"] == 95.0  # (120+70)/2
    # Economia: doc1=20, doc3=20
    assert analytics["competitive_summary"]["total_potential_savings"] == 40.0
    assert analytics["competitive_summary"]["decisive_row_count"] == 2


def test_three_carriers_second_lowest_is_savings_base():
    tables = [
        _table_meta(table_id="t1", slot_number=1, carrier_name="A"),
        _table_meta(table_id="t2", slot_number=2, carrier_name="B"),
        _table_meta(table_id="t3", slot_number=3, carrier_name="C"),
    ]
    rows = [
        _row(
            row_index=1,
            document_number="T1",
            destination_city="Curitiba",
            destination_uf="PR",
            weight=10,
            table_cells={
                "t1": _cell(table_id="t1", carrier_name="A", slot_number=1, calculated_freight=80.0),
                "t2": _cell(table_id="t2", carrier_name="B", slot_number=2, calculated_freight=120.0),
                "t3": _cell(table_id="t3", carrier_name="C", slot_number=3, calculated_freight=50.0),
            },
        ),
    ]
    analytics = build_comparison_analytics(_build_result(tables=tables, comparative_rows=rows))
    by_id = {item["table_id"]: item for item in analytics["carrier_competitiveness"]}
    assert by_id["t3"]["wins"] == 1
    assert by_id["t1"]["wins"] == 0
    assert by_id["t2"]["wins"] == 0
    # Economia = A (segunda menor) - C, NÃO B - C
    assert analytics["competitive_summary"]["total_potential_savings"] == 30.0
    assert by_id["t3"]["potential_savings_when_winner"] == 30.0
    assert analytics["executive_summary"]["lead_table_id"] == "t3"


def test_calculated_with_warnings_counts_as_complete():
    tables = [
        _table_meta(table_id="t1", slot_number=1, carrier_name="A"),
        _table_meta(table_id="t2", slot_number=2, carrier_name="B"),
    ]
    rows = [
        _row(
            row_index=1,
            document_number="W1",
            destination_city="X",
            destination_uf="SP",
            weight=5,
            table_cells={
                "t1": _warnings_cell(table_id="t1", carrier_name="A", slot_number=1, calculated_freight=40.0),
                "t2": _cell(table_id="t2", carrier_name="B", slot_number=2, calculated_freight=55.0),
            },
        ),
    ]
    analytics = build_comparison_analytics(_build_result(tables=tables, comparative_rows=rows))
    assert analytics["comparability"]["fully_comparable_rows"] == 1
    assert analytics["tables"][0]["calculated_rows"] == 1
    assert analytics["tables"][0]["calculated_with_warnings_rows"] == 1
    assert analytics["carrier_competitiveness"][0]["wins"] == 1


def test_incomplete_and_not_calculated_not_complete():
    tables = [
        _table_meta(table_id="t1", slot_number=1, carrier_name="A"),
        _table_meta(table_id="t2", slot_number=2, carrier_name="B"),
    ]
    rows = [
        _row(
            row_index=1,
            document_number="I1",
            destination_city="X",
            destination_uf="SP",
            weight=5,
            table_cells={
                "t1": _incomplete_cell(table_id="t1", carrier_name="A", slot_number=1, partial=10.0),
                "t2": _not_calculated_cell(table_id="t2", carrier_name="B", slot_number=2),
            },
        ),
    ]
    analytics = build_comparison_analytics(_build_result(tables=tables, comparative_rows=rows))
    assert analytics["comparability"]["inconclusive_rows"] == 1
    assert analytics["comparability"]["fully_comparable_rows"] == 0
    by_id = {item["table_id"]: item for item in analytics["carrier_competitiveness"]}
    assert by_id["t1"]["incomplete_rows"] == 1
    assert by_id["t1"]["calculated_rows"] == 0
    assert by_id["t2"]["not_calculated_rows"] == 1
    assert analytics["competitive_summary"]["total_potential_savings"] == 0.0
    assert analytics["executive_summary"]["lead_table_id"] is None


def test_tie_two_and_three_carriers_no_exclusive_winner():
    two = build_comparison_analytics(
        _build_result(
            tables=[
                _table_meta(table_id="t1", slot_number=1, carrier_name="A"),
                _table_meta(table_id="t2", slot_number=2, carrier_name="B"),
            ],
            comparative_rows=[
                _row(
                    row_index=1,
                    document_number="E2",
                    destination_city="X",
                    destination_uf="SP",
                    weight=1,
                    table_cells={
                        "t1": _cell(table_id="t1", carrier_name="A", slot_number=1, calculated_freight=50.004),
                        "t2": _cell(table_id="t2", carrier_name="B", slot_number=2, calculated_freight=50.001),
                    },
                ),
            ],
        )
    )
    # Ambos arredondam para 50.00 → empate monetário
    assert two["competitive_summary"]["tie_count"] == 1
    assert two["competitive_summary"]["decisive_row_count"] == 0
    assert two["competitive_summary"]["total_potential_savings"] == 0.0
    assert all(item["wins"] == 0 for item in two["carrier_competitiveness"])
    assert all(item["ties"] == 1 for item in two["carrier_competitiveness"])

    three = build_comparison_analytics(
        _build_result(
            tables=[
                _table_meta(table_id="t1", slot_number=1, carrier_name="A"),
                _table_meta(table_id="t2", slot_number=2, carrier_name="B"),
                _table_meta(table_id="t3", slot_number=3, carrier_name="C"),
            ],
            comparative_rows=[
                _row(
                    row_index=1,
                    document_number="E3",
                    destination_city="X",
                    destination_uf="RJ",
                    weight=1,
                    table_cells={
                        "t1": _cell(table_id="t1", carrier_name="A", slot_number=1, calculated_freight=10.0),
                        "t2": _cell(table_id="t2", carrier_name="B", slot_number=2, calculated_freight=10.0),
                        "t3": _cell(table_id="t3", carrier_name="C", slot_number=3, calculated_freight=10.0),
                    },
                ),
            ],
        )
    )
    assert three["competitive_summary"]["tie_count"] == 1
    assert three["executive_summary"]["lead_table_id"] is None


def test_win_percentage_uses_decisive_denominator():
    tables = [
        _table_meta(table_id="t1", slot_number=1, carrier_name="A"),
        _table_meta(table_id="t2", slot_number=2, carrier_name="B"),
    ]
    rows = [
        _row(
            row_index=1,
            document_number="W1",
            destination_city="X",
            destination_uf="SP",
            weight=1,
            table_cells={
                "t1": _cell(table_id="t1", carrier_name="A", slot_number=1, calculated_freight=10.0),
                "t2": _cell(table_id="t2", carrier_name="B", slot_number=2, calculated_freight=20.0),
            },
        ),
        _row(
            row_index=2,
            document_number="W2",
            destination_city="Y",
            destination_uf="SP",
            weight=1,
            table_cells={
                "t1": _cell(table_id="t1", carrier_name="A", slot_number=1, calculated_freight=15.0),
                "t2": _cell(table_id="t2", carrier_name="B", slot_number=2, calculated_freight=15.0),
            },
        ),
        _row(
            row_index=3,
            document_number="W3",
            destination_city="Z",
            destination_uf="RJ",
            weight=1,
            table_cells={
                "t1": _cell(table_id="t1", carrier_name="A", slot_number=1, calculated_freight=30.0),
                "t2": _cell(table_id="t2", carrier_name="B", slot_number=2, calculated_freight=25.0),
            },
        ),
    ]
    analytics = build_comparison_analytics(_build_result(tables=tables, comparative_rows=rows))
    assert analytics["competitive_summary"]["comparable_row_count"] == 3
    assert analytics["competitive_summary"]["tie_count"] == 1
    assert analytics["competitive_summary"]["decisive_row_count"] == 2
    by_id = {item["table_id"]: item for item in analytics["carrier_competitiveness"]}
    assert by_id["t1"]["wins"] == 1
    assert by_id["t2"]["wins"] == 1
    assert by_id["t1"]["win_percentage"] == 50.0
    assert by_id["t2"]["win_percentage"] == 50.0


def test_freight_per_kg_comparable_is_total_over_weight():
    tables = [
        _table_meta(table_id="t1", slot_number=1, carrier_name="A"),
        _table_meta(table_id="t2", slot_number=2, carrier_name="B"),
    ]
    rows = [
        _row(
            row_index=1,
            document_number="K1",
            destination_city="X",
            destination_uf="SP",
            weight=10,
            table_cells={
                "t1": _cell(table_id="t1", carrier_name="A", slot_number=1, calculated_freight=100.0),
                "t2": _cell(table_id="t2", carrier_name="B", slot_number=2, calculated_freight=120.0),
            },
        ),
        _row(
            row_index=2,
            document_number="K2",
            destination_city="Y",
            destination_uf="SP",
            weight=0,
            table_cells={
                "t1": _cell(table_id="t1", carrier_name="A", slot_number=1, calculated_freight=50.0),
                "t2": _cell(table_id="t2", carrier_name="B", slot_number=2, calculated_freight=40.0),
            },
        ),
    ]
    analytics = build_comparison_analytics(_build_result(tables=tables, comparative_rows=rows))
    by_id = {item["table_id"]: item for item in analytics["carrier_competitiveness"]}
    # frete/kg = soma fretes comparáveis / soma pesos > 0 (peso 0 não entra no denominador)
    assert by_id["t1"]["comparable_freight_per_kg_average"] == 15.0  # 150/10
    assert by_id["t2"]["comparable_freight_per_kg_average"] == 16.0  # 160/10


def test_geography_winner_by_wins_low_sample_and_no_base():
    tables = [
        _table_meta(table_id="t1", slot_number=1, carrier_name="A"),
        _table_meta(table_id="t2", slot_number=2, carrier_name="B"),
    ]
    rows = [
        # SP: 2 comparáveis, A vence ambos → baixa amostra
        _row(
            row_index=1,
            document_number="G1",
            destination_city="Campinas",
            destination_uf="sp",
            weight=1,
            table_cells={
                "t1": _cell(table_id="t1", carrier_name="A", slot_number=1, calculated_freight=10.0),
                "t2": _cell(table_id="t2", carrier_name="B", slot_number=2, calculated_freight=20.0),
            },
        ),
        _row(
            row_index=2,
            document_number="G2",
            destination_city="Santos",
            destination_uf="SP",
            weight=1,
            table_cells={
                "t1": _cell(table_id="t1", carrier_name="A", slot_number=1, calculated_freight=12.0),
                "t2": _cell(table_id="t2", carrier_name="B", slot_number=2, calculated_freight=18.0),
            },
        ),
        # RJ: linhas sem comparação
        _row(
            row_index=3,
            document_number="G3",
            destination_city="Niterói",
            destination_uf="RJ",
            weight=1,
            table_cells={
                "t1": _cell(table_id="t1", carrier_name="A", slot_number=1, calculated_freight=10.0),
                "t2": _not_calculated_cell(table_id="t2", carrier_name="B", slot_number=2),
            },
        ),
        # UF ausente
        _row(
            row_index=4,
            document_number="G4",
            destination_city="?",
            destination_uf="",
            weight=1,
            table_cells={
                "t1": _not_calculated_cell(table_id="t1", carrier_name="A", slot_number=1),
                "t2": _not_calculated_cell(table_id="t2", carrier_name="B", slot_number=2),
            },
        ),
    ]
    analytics = build_comparison_analytics(_build_result(tables=tables, comparative_rows=rows))
    by_uf = {item["uf_label"]: item for item in analytics["geography"]["destination_ufs"]}
    assert "SP" in by_uf
    assert by_uf["SP"]["uf"] == "SP"
    assert by_uf["SP"]["comparable_row_count"] == 2
    assert by_uf["SP"]["winner_table_id"] == "t1"
    assert by_uf["SP"]["low_sample"] is True
    assert by_uf["SP"]["map_status"] == "winner"
    assert by_uf["SP"]["total_potential_savings"] == 16.0  # 10+6

    assert by_uf["RJ"]["comparable_row_count"] == 0
    assert by_uf["RJ"]["winner_table_id"] is None
    assert by_uf["RJ"]["has_comparable_base"] is False
    assert by_uf["RJ"]["map_status"] == "no_comparable_base"

    assert by_uf["N/D"]["uf"] is None
    assert by_uf["N/D"]["comparable_row_count"] == 0
    assert by_uf["N/D"]["map_status"] == "no_comparable_base"

    ranking = analytics["geography"]["uf_potential_ranking"]
    assert ranking[0]["uf_label"] == "SP"
    assert analytics["geography"]["low_sample_threshold"] == 5


def test_geography_uf_tie_after_wins_and_average():
    tables = [
        _table_meta(table_id="t1", slot_number=1, carrier_name="A"),
        _table_meta(table_id="t2", slot_number=2, carrier_name="B"),
    ]
    rows = [
        _row(
            row_index=1,
            document_number="T1",
            destination_city="X",
            destination_uf="MG",
            weight=1,
            table_cells={
                "t1": _cell(table_id="t1", carrier_name="A", slot_number=1, calculated_freight=10.0),
                "t2": _cell(table_id="t2", carrier_name="B", slot_number=2, calculated_freight=20.0),
            },
        ),
        _row(
            row_index=2,
            document_number="T2",
            destination_city="Y",
            destination_uf="MG",
            weight=1,
            table_cells={
                "t1": _cell(table_id="t1", carrier_name="A", slot_number=1, calculated_freight=20.0),
                "t2": _cell(table_id="t2", carrier_name="B", slot_number=2, calculated_freight=10.0),
            },
        ),
    ]
    analytics = build_comparison_analytics(_build_result(tables=tables, comparative_rows=rows))
    mg = next(item for item in analytics["geography"]["destination_ufs"] if item["uf"] == "MG")
    # 1 vitória cada; custo médio igual (15) → empate de UF
    assert mg["winner_table_id"] is None
    assert mg["is_tie"] is True
    assert mg["map_status"] == "tie"


def test_no_global_winner_from_raw_totals_with_uneven_coverage():
    """A calcula 10 com total menor; B calcula 100; só 5 comparáveis — não induzir por total bruto."""
    tables = [
        _table_meta(table_id="t1", slot_number=1, carrier_name="A"),
        _table_meta(table_id="t2", slot_number=2, carrier_name="B"),
    ]
    rows = []
    # 5 comparáveis: B vence todas (mais barata no universo comparável)
    for i in range(5):
        rows.append(
            _row(
                row_index=i + 1,
                document_number=f"C{i}",
                destination_city="X",
                destination_uf="SP",
                weight=1,
                table_cells={
                    "t1": _cell(table_id="t1", carrier_name="A", slot_number=1, calculated_freight=100.0),
                    "t2": _cell(table_id="t2", carrier_name="B", slot_number=2, calculated_freight=80.0),
                },
            )
        )
    # +5 só A (total bruto de A ainda pode parecer baixo em média coberta, mas não no comparável)
    for i in range(5):
        rows.append(
            _row(
                row_index=10 + i,
                document_number=f"A{i}",
                destination_city="Y",
                destination_uf="RJ",
                weight=1,
                table_cells={
                    "t1": _cell(table_id="t1", carrier_name="A", slot_number=1, calculated_freight=1.0),
                    "t2": _not_calculated_cell(table_id="t2", carrier_name="B", slot_number=2),
                },
            )
        )
    # +95 só B
    for i in range(95):
        rows.append(
            _row(
                row_index=20 + i,
                document_number=f"B{i}",
                destination_city="Z",
                destination_uf="MG",
                weight=1,
                table_cells={
                    "t1": _not_calculated_cell(table_id="t1", carrier_name="A", slot_number=1),
                    "t2": _cell(table_id="t2", carrier_name="B", slot_number=2, calculated_freight=200.0),
                },
            )
        )
    analytics = build_comparison_analytics(_build_result(tables=tables, comparative_rows=rows))
    by_id = {item["table_id"]: item for item in analytics["carrier_competitiveness"]}
    # Total bruto operacional de A (5*100 + 5*1 = 505) << B (5*80 + 95*200)
    assert analytics["tables"][0]["calculated_freight_total"] == 505.0
    assert analytics["tables"][1]["calculated_freight_total"] == 19400.0
    # Competitividade: B vence as 5 comparáveis
    assert analytics["comparability"]["fully_comparable_rows"] == 5
    assert by_id["t2"]["wins"] == 5
    assert by_id["t1"]["wins"] == 0
    assert analytics["executive_summary"]["lead_table_id"] == "t2"
    assert by_id["t1"]["comparable_freight_average"] == 100.0
    assert by_id["t2"]["comparable_freight_average"] == 80.0


def test_same_carrier_name_distinct_table_ids_in_competitiveness():
    tables = [
        _table_meta(table_id="t1", slot_number=1, carrier_name="Mesma"),
        _table_meta(table_id="t2", slot_number=2, carrier_name="Mesma"),
    ]
    rows = [
        _row(
            row_index=1,
            document_number="S1",
            destination_city="X",
            destination_uf="SP",
            weight=1,
            table_cells={
                "t1": _cell(table_id="t1", carrier_name="Mesma", slot_number=1, calculated_freight=10.0),
                "t2": _cell(table_id="t2", carrier_name="Mesma", slot_number=2, calculated_freight=20.0),
            },
        ),
    ]
    analytics = build_comparison_analytics(_build_result(tables=tables, comparative_rows=rows))
    names = [item["display_name"] for item in analytics["carrier_competitiveness"]]
    assert names == ["Mesma — Tabela 1", "Mesma — Tabela 2"]
    assert analytics["carrier_competitiveness"][0]["table_id"] == "t1"
    assert analytics["carrier_competitiveness"][1]["table_id"] == "t2"


def test_duplicate_documents_distinct_row_index_compete_separately():
    tables = [
        _table_meta(table_id="t1", slot_number=1, carrier_name="A"),
        _table_meta(table_id="t2", slot_number=2, carrier_name="B"),
    ]
    rows = [
        _row(
            row_index=1,
            document_number="DUP",
            destination_city="X",
            destination_uf="SP",
            weight=1,
            table_cells={
                "t1": _cell(table_id="t1", carrier_name="A", slot_number=1, calculated_freight=10.0),
                "t2": _cell(table_id="t2", carrier_name="B", slot_number=2, calculated_freight=20.0),
            },
        ),
        _row(
            row_index=2,
            document_number="DUP",
            destination_city="Y",
            destination_uf="RJ",
            weight=1,
            table_cells={
                "t1": _cell(table_id="t1", carrier_name="A", slot_number=1, calculated_freight=30.0),
                "t2": _cell(table_id="t2", carrier_name="B", slot_number=2, calculated_freight=15.0),
            },
        ),
    ]
    analytics = build_comparison_analytics(_build_result(tables=tables, comparative_rows=rows))
    assert analytics["global_summary"]["document_count"] == 1
    assert analytics["comparability"]["fully_comparable_rows"] == 2
    by_id = {item["table_id"]: item for item in analytics["carrier_competitiveness"]}
    assert by_id["t1"]["wins"] == 1
    assert by_id["t2"]["wins"] == 1
