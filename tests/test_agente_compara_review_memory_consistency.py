"""Consistência semântica entre revisão pública e memória de cálculo."""
from __future__ import annotations

import copy
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.agente_compara_calculation_service import (
    SingleTableCalculationContext,
    calculate_single_table,
)
from app.agente_compara_doc_service import _public_temp_table
from app.services.agente_compara_config_service import DEFAULT_CALCULATION_BASES


def _patch_bases(monkeypatch):
    cfg = SimpleNamespace(
        calculation_bases=copy.deepcopy(DEFAULT_CALCULATION_BASES),
        upload_ttl_hours=24,
    )
    monkeypatch.setattr(
        "app.agente_compara_doc_service.get_agente_compara_config",
        lambda: cfg,
    )
    return cfg


@pytest.fixture
def app_ctx(app, monkeypatch):
    os.environ.setdefault("APP_ENV", "dev")
    _patch_bases(monkeypatch)
    with app.app_context():
        yield


def _row(**overrides):
    base = {
        "row_index": 0,
        "document_number": "DOC-1",
        "destination_city": "Campinas",
        "destination_uf": "SP",
        "audited_weight": 48.0,
        "invoice_value": 1000.0,
    }
    base.update(overrides)
    return base


def _pricing_record(accessorial_fees=None):
    return {
        "temp_table_id": uuid4().hex,
        "status": "needs_review",
        "freight_tables": [
            {
                "table_title": "Tabela por região",
                "table_type": "weight_range_table",
                "columns": ["Região de frete", "Até 30 kg", "31 a 50 kg", "Excedente kg"],
                "rows": [
                    {
                        "Região de frete": "SP-Interior 1",
                        "Até 30 kg": "80,00",
                        "31 a 50 kg": "100,50",
                        "Excedente kg": "2,00",
                    }
                ],
            }
        ],
        "freight_routes": [],
        "accessorial_fees": list(accessorial_fees or []),
        "reading_alerts": [],
        "uncertain_fields": [],
        "coverage_table": None,
        "audit_batch": None,
    }


def _coverage():
    return {
        "rows": [
            {
                "destination_uf": "SP",
                "destination_city": "Campinas",
                "freight_region": "SP-Interior 1",
            }
        ]
    }


def _ctx(record):
    return SingleTableCalculationContext(
        comparison_id=uuid4().hex,
        table_id=uuid4().hex,
        temp_table_id=record["temp_table_id"],
        slot_number=1,
        carrier_name="Transportadora Alfa",
        table_record=record,
        normalized_rows=[_row()],
        tax_config=None,
        coverage_table=_coverage(),
        primary_temp_table_id=record["temp_table_id"],
    )


def _semantic_keys(fee: dict, presentation: dict) -> set[str]:
    keys = {
        str(fee.get("calculation_type") or "").strip(),
        str(fee.get("operation") or "").strip(),
        str(fee.get("audit_variable") or "").strip(),
        str(presentation.get("state") or "").strip(),
    }
    return {item for item in keys if item}


def test_review_memory_consistency_for_applied_recognized_fees(app_ctx):
    fees = [
        {
            "name": "GRIS",
            "item_id": "fee-gris",
            "value": "1,00%",
            "unit": "%",
            "rate": 0.01,
            "calculation_base_id": None,
            "calculation_type": "invoice_percentage",
            "classification_source": "legacy_classifier",
            "classification_confidence": "high",
            "status": "calculable",
            "canonical_component": "risk_management",
            "modifier_type": "base_fee",
            "component_group": "risk_management",
        },
        {
            "name": "TAS",
            "item_id": "fee-tas",
            "value": "10,00",
            "unit": "R$",
            "amount": 10.0,
            "calculation_base_id": "por_cte",
            "calculation_base_label": "por CTe",
            "calculation_type": "fixed_amount",
            "operation": "fixed_amount",
            "classification_source": "configured_calculation_base",
            "classification_confidence": "high",
            "status": "calculable",
            "canonical_component": "administrative_fee",
        },
        {
            "name": "Pedágio",
            "item_id": "fee-ped",
            "value": "5,00",
            "unit": "R$",
            "amount": 5.0,
            "calculation_base_id": "fracao_100kg",
            "calculation_base_label": "por fração de 100kg",
            "calculation_type": "weight_fraction",
            "operation": "ceil_fraction",
            "operation_parameters": {"fraction_size": 100},
            "audit_variable": "peso",
            "classification_source": "configured_calculation_base",
            "classification_confidence": "high",
            "status": "calculable",
            "canonical_component": "toll",
        },
        {
            "name": "GRIS Mínimo",
            "item_id": "fee-gris-min",
            "value": "2,00",
            "unit": "R$",
            "minimum_amount": 2.0,
            "calculation_type": "minimum_amount",
            "modifier_type": "minimum_amount",
            "related_to": "risk_management",
            "component_group": "risk_management",
            "classification_source": "legacy_classifier",
            "status": "calculable",
        },
        {
            "name": "Pendente",
            "item_id": "fee-unresolved",
            "value": "",
            "unit": "",
            "calculation_base_id": None,
            "calculation_basis": "não mapeado / revisar",
            "classification_source": "unmapped_calculation_base",
            "status": "needs_review",
        },
    ]
    record = _pricing_record(accessorial_fees=fees)
    public = _public_temp_table(record)
    by_id = {fee["item_id"]: fee for fee in public["accessorial_fees"]}

    assert by_id["fee-gris"]["review_presentation"]["state"] == "resolved"
    assert by_id["fee-gris"]["review_presentation"]["is_blocking"] is False
    assert "NF" in by_id["fee-gris"]["review_presentation"]["basis_label"]

    assert by_id["fee-tas"]["review_presentation"]["state"] == "resolved"
    assert by_id["fee-ped"]["review_presentation"]["state"] == "resolved"
    assert "fração" in by_id["fee-ped"]["review_presentation"]["basis_label"].lower()

    assert by_id["fee-gris-min"]["review_presentation"]["state"] == "resolved"
    assert by_id["fee-unresolved"]["review_presentation"]["state"] == "blocking"
    assert by_id["fee-unresolved"]["review_presentation"]["is_blocking"] is True

    # Memória usa apenas taxas aplicáveis; unresolved formal não deve contradizer estado visual.
    # Cálculo exclui a taxa unresolved (bloqueante na confirmação); se presente no snapshot,
    # o gate de completeza marca incomplete — aqui validamos só as reconhecidas aplicadas.
    calc_fees = [fee for fee in fees if fee["item_id"] != "fee-unresolved"]
    calc = calculate_single_table(_ctx(_pricing_record(accessorial_fees=calc_fees)))
    row = calc["results"][0]
    memory = row.get("calculation_memory") or {}
    assert memory.get("status") == "calculated"
    applied = [
        item
        for item in (memory.get("components") or [])
        if isinstance(item, dict) and item.get("applied") and item.get("code") == "ACCESSORIAL"
    ]
    assert applied, "esperado ao menos um componente ACCESSORIAL aplicado"

    for component in applied:
        label = str(component.get("label") or "").strip().lower()
        operation = str(component.get("operation") or "").strip()
        matching_fee = None
        for fee in public["accessorial_fees"]:
            fee_name = str(fee.get("name") or "").strip().lower()
            if fee_name and fee_name == label:
                matching_fee = fee
                break
            if operation and operation == str(fee.get("calculation_type") or "").strip():
                if fee.get("canonical_component") and fee_name.startswith(label[:3]):
                    matching_fee = fee
                    break
        if matching_fee is None:
            continue
        presentation = matching_fee.get("review_presentation") or {}
        assert presentation.get("state") != "blocking"
        assert presentation.get("is_blocking") is not True
        keys = _semantic_keys(matching_fee, presentation)
        assert operation in keys or presentation.get("state") in {
            "resolved",
            "informational",
        }
