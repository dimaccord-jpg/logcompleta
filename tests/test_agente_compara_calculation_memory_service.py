"""Testes unitários da memória de cálculo do AgenteCompara."""
from __future__ import annotations

import copy
import importlib
import inspect
import json
import math
from pathlib import Path

import pytest

from app.agente_compara_calculation_memory_service import (
    CALCULATION_MEMORY_SCHEMA_VERSION,
    MEMORY_STATUS_CALCULATED,
    MEMORY_STATUS_NOT_CALCULATED,
    CalculationMemoryTotalMismatchError,
    build_calculation_memory,
    build_not_calculated_diagnostic,
    validate_calculation_memory_total,
)


def _raw_success(**overrides):
    raw = {
        "row_index": 0,
        "numero_documento": "NF-1",
        "destination_city": "Campinas",
        "destination_uf": "SP",
        "freight_region": "SP-Interior 1",
        "audited_weight": 50.0,
        "expected_freight": 265.22,
        "weight_freight": 100.5,
        "freight_value_amount": 50.0,
        "route_toll_amount": 10.0,
        "calculation_basis": "range_plus_excess_per_kg",
        "calculation_details": "Faixa até 50 kg",
        "pricing_type": "range_plus_excess_per_kg",
        "pricing_lookup_key": "SP-Interior 1",
        "pricing_lookup_kind": "region",
        "status": None,
        "calculation_components": {
            "weight_freight": {
                "amount": 100.5,
                "basis": "range_plus_excess_per_kg",
                "details": "Faixa até 50 kg",
            },
            "freight_value": {
                "amount": 50.0,
                "rate": 0.01,
                "invoice_value": 5000.0,
                "source_column": "Frete Valor",
                "details": "Valor NF 5000 x 1%",
            },
            "route_toll": {
                "amount": 10.0,
                "rate_per_fraction": 5.0,
                "fractions": 2,
                "details": "ceil(50/100) x 5",
                "source_column": "Pedágio",
            },
            "accessorial_fees": [
                {
                    "label": "GRIS",
                    "amount": 20.0,
                    "rate": 0.004,
                    "invoice_value": 5000.0,
                    "calculation_type": "invoice_percentage",
                    "minimum_amount": 15.0,
                    "minimum_applied": False,
                    "details": "GRIS 0,4%",
                    "source_block": "accessorial_fees",
                },
                {
                    "label": "Despacho",
                    "amount": 25.0,
                    "calculation_type": "fixed",
                    "minimum_amount": 25.0,
                    "minimum_applied": True,
                    "calculated_amount": 20.0,
                    "details": "mínimo aplicado",
                    "component_group": "dispatch",
                },
            ],
            "ignored_accessorial_fees": [
                {
                    "label": "Taxa extra",
                    "ignored_reason": "Duplicata de frete valor",
                    "reason_code": "duplicate_ignored",
                }
            ],
            "subtotal_before_taxes": 205.5,
            "tax_total": 59.72,
            "tax_components": [
                {
                    "tax_type": "ICMS",
                    "base_amount": 205.5,
                    "rate": 0.12,
                    "amount": 59.72,
                    "calculation_mode": "inside",
                    "applied": True,
                    "source_name": "Tabela ICMS",
                }
            ],
        },
    }
    raw.update(overrides)
    return raw


def test_calculated_complete_memory():
    raw = _raw_success()
    original = copy.deepcopy(raw)
    memory = build_calculation_memory(
        raw,
        calculated_freight=265.22,
        status="calculated",
        row_index=0,
        table_id="tbl-a",
        slot_number=1,
        carrier_name="Bertolini",
    )
    assert memory["schema_version"] == CALCULATION_MEMORY_SCHEMA_VERSION
    assert memory["status"] == MEMORY_STATUS_CALCULATED
    assert memory["row_index"] == 0
    assert memory["table_id"] == "tbl-a"
    assert memory["slot_number"] == 1
    assert memory["carrier_name"] == "Bertolini"
    assert memory["total"] == 265.22
    assert memory["calculated_freight"] == 265.22
    assert memory["subtotal_before_taxes"] == 205.5
    assert memory.get("diagnostic") in (None, {})
    codes = [item["code"] for item in memory["components"]]
    assert codes[0] == "WEIGHT_FREIGHT"
    assert "FREIGHT_VALUE" in codes
    assert "TOLL" in codes
    assert codes.count("ACCESSORIAL") == 2
    assert "IGNORED_ACCESSORIAL" in codes
    assert memory["taxes"][0]["tax_type"] == "ICMS"
    assert memory["evidence"]["freight_region"] == "SP-Interior 1"
    assert raw == original


def test_not_calculated_diagnostic():
    raw = {
        "row_index": 2,
        "status": "missing_freight_rule",
        "diagnostic": {
            "failure_stage": "pricing_lookup",
            "diagnostic_group_code": "missing_freight_rule",
            "message": "Nenhuma faixa aplicável.",
            "search_context": {"destination_city": "Campinas", "destination_uf": "SP"},
            "attempted_keys": ["SP-Interior 1"],
        },
        "freight_region": "SP-Interior 1",
        "calculation_components": {},
    }
    memory = build_not_calculated_diagnostic(
        raw=raw,
        status="missing_freight_rule",
        error={"code": "missing_freight_rule", "message": "Nenhuma faixa aplicável."},
        row_index=2,
        table_id="tbl-b",
        slot_number=2,
        carrier_name="X",
    )
    assert memory["status"] == MEMORY_STATUS_NOT_CALCULATED
    assert memory["total"] is None
    assert memory["calculated_freight"] is None
    assert memory["components"] == []
    assert memory["diagnostic"]["code"] == "missing_freight_rule"
    assert "faixa" in memory["diagnostic"]["message"].lower()


def test_total_matches_calculated_freight():
    memory = build_calculation_memory(
        _raw_success(),
        calculated_freight=265.22,
        status="calculated",
        row_index=0,
        table_id="t1",
    )
    validate_calculation_memory_total(memory, calculated_freight=265.22)
    with pytest.raises(CalculationMemoryTotalMismatchError):
        validate_calculation_memory_total(
            {**memory, "total": 10.0},
            calculated_freight=265.22,
        )


def test_component_order_deterministic():
    memory = build_calculation_memory(
        _raw_success(),
        calculated_freight=265.22,
        status="calculated",
        row_index=0,
    )
    codes = [item["code"] for item in memory["components"]]
    assert codes.index("WEIGHT_FREIGHT") < codes.index("FREIGHT_VALUE")
    assert codes.index("FREIGHT_VALUE") < codes.index("TOLL")
    assert codes.index("TOLL") < codes.index("ACCESSORIAL")
    assert codes.index("ACCESSORIAL") < codes.index("IGNORED_ACCESSORIAL")


def test_minimum_applied_and_not_applied():
    memory = build_calculation_memory(
        _raw_success(),
        calculated_freight=265.22,
        status="calculated",
        row_index=0,
    )
    by_label = {item["label"]: item for item in memory["components"] if item["code"] == "ACCESSORIAL"}
    assert by_label["GRIS"]["minimum_applied"] is False
    assert by_label["GRIS"]["minimum_amount"] == 15.0
    assert by_label["Despacho"]["minimum_applied"] is True
    assert by_label["Despacho"]["minimum_amount"] == 25.0


def test_toll_accessorial_taxes_ignored_evidence():
    memory = build_calculation_memory(
        _raw_success(),
        calculated_freight=265.22,
        status="calculated",
        row_index=0,
    )
    toll = next(item for item in memory["components"] if item["code"] == "TOLL")
    assert toll["amount"] == 10.0
    ignored = next(item for item in memory["components"] if item["code"] == "IGNORED_ACCESSORIAL")
    assert ignored["ignored"] is True
    assert ignored["amount"] is None
    assert memory["taxes"][0]["amount"] == 59.72
    assert memory["evidence"]["pricing_lookup_key"] == "SP-Interior 1"


def test_missing_fields_are_omitted_not_invented():
    raw = {
        "row_index": 1,
        "expected_freight": 10.0,
        "calculation_components": {
            "weight_freight": {"amount": 10.0, "basis": "fixed_range"},
            "subtotal_before_taxes": 10.0,
        },
    }
    memory = build_calculation_memory(
        raw,
        calculated_freight=10.0,
        status="calculated",
        row_index=1,
    )
    assert "rounding" not in memory or memory.get("rounding") is None
    assert len(memory.get("taxes") or []) == 0
    assert all(item["code"] != "FREIGHT_VALUE" for item in memory["components"])


def test_json_safe_no_nan_infinity_and_serializable():
    raw = _raw_success()
    raw["calculation_components"]["weight_freight"]["amount"] = float("nan")
    memory = build_calculation_memory(
        _raw_success(expected_freight=12.34),
        calculated_freight=12.34,
        status="calculated",
        row_index=0,
    )
    payload = json.dumps(memory, allow_nan=False)
    assert "NaN" not in payload
    assert "Infinity" not in payload
    # Entrada com NaN não deve vazar como número inválido no amount do componente.
    bad = build_calculation_memory(
        raw,
        calculated_freight=265.22,
        status="calculated",
        row_index=0,
    )
    weight = next(item for item in bad["components"] if item["code"] == "WEIGHT_FREIGHT")
    assert weight.get("amount") is None or math.isfinite(float(weight["amount"]))


def test_no_auditoria_gemini_billing_imports():
    module = importlib.import_module("app.agente_compara_calculation_memory_service")
    source = Path(inspect.getsourcefile(module)).read_text(encoding="utf-8")
    import_lines = [
        line.strip().lower()
        for line in source.splitlines()
        if line.strip().startswith("import ") or line.strip().startswith("from ")
    ]
    joined = "\n".join(import_lines)
    assert "cleide" not in joined
    assert "gemini" not in joined
    assert "billing" not in joined
    assert "stripe" not in joined
    assert "flask" not in joined
