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
