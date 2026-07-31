"""Testes unitários do validador determinístico de temp table (AgenteCompara)."""
from __future__ import annotations

import copy
import json
import math
import os

import pytest

from app.agente_compara_temp_table_validation_service import (
    CODE_INCOMPATIBLE_UNIT,
    CODE_INVALID_VALUE,
    CODE_MINIMUM_WITHOUT_BASE,
    CODE_MISSING_VALUE,
    CODE_READING_ALERT,
    CODE_UNCERTAIN_FIELD,
    CODE_UNCONFIRMED_EXTRACTED_RULE,
    CODE_UNMAPPED_CALCULATION_BASE,
    validate_temp_table_for_confirmation,
)
from app.services.agente_compara_config_service import (
    AgenteComparaConfig,
    DEFAULT_FALLBACK_MESSAGE,
)


def _patch_config(monkeypatch):
    cfg = AgenteComparaConfig(
        chat_enabled=True,
        upload_enabled=True,
        chat_max_history=10,
        document_context_max_chars=24000,
        max_documents_considered=3,
        question_max_chars=4000,
        fallback_message=DEFAULT_FALLBACK_MESSAGE,
        no_documents_behavior="allow_guided",
        show_documents_used=True,
        no_hallucination_instruction_enabled=True,
        audited_file_max_bytes=None,
        audited_file_max_rows=2000,
    )
    monkeypatch.setattr(
        "app.services.agente_compara_config_service.get_agente_compara_config",
        lambda: cfg,
    )
    monkeypatch.setattr(
        "app.agente_compara_doc_service.get_agente_compara_config",
        lambda: cfg,
    )
    return cfg

@pytest.fixture
def app_ctx(app, monkeypatch):
    os.environ.setdefault("APP_ENV", "dev")
    _patch_config(monkeypatch)
    with app.app_context():
        yield


def _fee(**overrides):
    base = {
        "name": "Taxa A",
        "value": "10,00",
        "unit": "R$",
        "calculation_base_id": "por_cte",
        "calculation_basis": "por CTe",
        "classification_source": "manual_configured_calculation_base",
        "operation": "fixed_amount",
        "calculation_type": "fixed_amount",
        "status": "calculable",
        "notes": "",
    }
    base.update(overrides)
    return base


def _valid_table(**overrides):
    payload = {
        "accessorial_fees": [_fee()],
        "reading_alerts": [],
        "uncertain_fields": [],
    }
    payload.update(overrides)
    return payload


def test_valid_snapshot_can_confirm(app_ctx):
    result = validate_temp_table_for_confirmation(_valid_table())
    assert result["can_confirm"] is True
    assert result["blocking_count"] == 0
    assert result["blocking_issues"] == []


def test_unmapped_calculation_base_blocks(app_ctx):
    table = _valid_table(
        accessorial_fees=[
            _fee(
                calculation_base_id=None,
                calculation_basis="não mapeado / revisar",
                classification_source="unmapped_calculation_base",
                operation=None,
                value="",
                unit="",
                status="needs_review",
            )
        ]
    )
    result = validate_temp_table_for_confirmation(table)
    assert result["can_confirm"] is False
    assert result["blocking_count"] == 1
    assert result["blocking_issues"][0]["code"] == CODE_UNMAPPED_CALCULATION_BASE
    assert result["blocking_issues"][0]["field"] == "calculation_base_id"


def test_missing_value_blocks(app_ctx):
    table = _valid_table(accessorial_fees=[_fee(value="", unit="R$")])
    result = validate_temp_table_for_confirmation(table)
    assert result["can_confirm"] is False
    assert result["blocking_issues"][0]["code"] == CODE_MISSING_VALUE
    assert result["blocking_issues"][0]["field"] == "value"


def test_invalid_value_blocks(app_ctx):
    table = _valid_table(accessorial_fees=[_fee(value="abc", unit="R$")])
    result = validate_temp_table_for_confirmation(table)
    assert result["can_confirm"] is False
    assert result["blocking_issues"][0]["code"] == CODE_INVALID_VALUE
    assert result["blocking_issues"][0]["field"] == "value"


def test_incompatible_unit_blocks(app_ctx):
    table = _valid_table(accessorial_fees=[_fee(value="10,24", unit="%")])
    result = validate_temp_table_for_confirmation(table)
    assert result["can_confirm"] is False
    assert result["blocking_issues"][0]["code"] == CODE_INCOMPATIBLE_UNIT
    assert result["blocking_issues"][0]["field"] == "unit"


def test_generic_warning_does_not_block(app_ctx):
    table = _valid_table(reading_alerts=["Leitura parcial do rodapé"])
    result = validate_temp_table_for_confirmation(table)
    assert result["can_confirm"] is True
    assert result["warning_count"] == 1
    assert result["warnings"][0]["code"] == CODE_READING_ALERT


def test_uncertain_fields_do_not_block(app_ctx):
    table = _valid_table(uncertain_fields=["origem"])
    result = validate_temp_table_for_confirmation(table)
    assert result["can_confirm"] is True
    assert result["warnings"][0]["code"] == CODE_UNCERTAIN_FIELD


def test_reading_alerts_do_not_block(app_ctx):
    table = _valid_table(reading_alerts=["alerta genérico"])
    result = validate_temp_table_for_confirmation(table)
    assert result["can_confirm"] is True
    assert result["blocking_count"] == 0


def test_optional_legacy_incomplete_blocks(app_ctx):
    table = _valid_table(
        accessorial_fees=[
            {
                "name": "Ruído opcional",
                "value": "",
                "unit": "",
                "calculation_base_id": None,
                "calculation_basis": "",
                "classification_source": "legacy_classifier",
                "status": "needs_review",
            }
        ]
    )
    result = validate_temp_table_for_confirmation(table)
    assert result["can_confirm"] is False
    assert result["blocking_count"] == 1
    assert result["blocking_issues"][0]["code"] == CODE_UNCONFIRMED_EXTRACTED_RULE
    assert result["blocking_issues"][0]["reason_code"] == "unconfirmed_extracted_rule"


def test_extraction_hypothesis_blocks(app_ctx):
    table = _valid_table(
        accessorial_fees=[
            {
                "name": "Ruído opcional",
                "value": "",
                "unit": "",
                "calculation_base_id": None,
                "calculation_basis": "",
                "classification_source": "legacy_classifier",
                "status": "needs_review",
            }
        ]
    )
    result = validate_temp_table_for_confirmation(table)
    assert result["can_confirm"] is False
    assert result["blocking_count"] == 1
    assert result["blocking_issues"][0]["code"] == CODE_UNCONFIRMED_EXTRACTED_RULE


def test_minimum_without_link_blocks(app_ctx):
    table = _valid_table(
        accessorial_fees=[
            {
                "name": "Reentrega Mínimo",
                "item_id": "fee-min",
                "value": "25,00",
                "unit": "R$",
                "minimum_amount": 25.0,
                "calculation_type": "minimum_amount",
                "modifier_type": "minimum_amount",
                "related_to": None,
                "classification_source": "legacy_classifier",
                "status": "needs_review",
            }
        ]
    )
    result = validate_temp_table_for_confirmation(table)
    assert result["can_confirm"] is False
    assert result["blocking_count"] == 1
    assert result["blocking_issues"][0]["code"] == CODE_MINIMUM_WITHOUT_BASE
    assert "Vincule Reentrega Mínimo" in result["blocking_issues"][0]["message"]


def test_minimum_linked_does_not_block(app_ctx):
    reentrega = {
        "name": "Reentrega",
        "item_id": "fee-re",
        "value": "100%",
        "unit": "%",
        "rate": 1.0,
        "calculation_type": "freight_percentage",
        "classification_source": "legacy_classifier",
        "status": "calculable",
        "component_group": "operational_fee",
        "canonical_component": "operational_fee",
        "modifier_type": "base_fee",
    }
    minimum = {
        "name": "Reentrega Mínimo",
        "item_id": "fee-min",
        "value": "25,00",
        "unit": "R$",
        "minimum_amount": 25.0,
        "calculation_type": "minimum_amount",
        "modifier_type": "minimum_amount",
        "related_to": "operational_fee",
        "component_group": "operational_fee",
        "classification_source": "legacy_classifier",
        "status": "calculable",
    }
    result = validate_temp_table_for_confirmation(
        _valid_table(accessorial_fees=[reentrega, minimum])
    )
    assert result["can_confirm"] is True
    assert result["blocking_count"] == 0


def test_three_print_blockers(app_ctx):
    """Retorno sem base + Reentrega sem base + Reentrega Mínimo sem vínculo."""
    fees = [
        {
            "name": "Retorno/Devolução",
            "item_id": "fee-ret",
            "value": "",
            "unit": "",
            "calculation_base_id": None,
            "calculation_basis": "não mapeado / revisar",
            "classification_source": "unmapped_calculation_base",
            "status": "needs_review",
        },
        {
            "name": "Reentrega",
            "item_id": "fee-ree",
            "value": "",
            "unit": "",
            "calculation_base_id": None,
            "calculation_basis": "",
            "classification_source": "legacy_classifier",
            "status": "needs_review",
        },
        {
            "name": "Reentrega Mínimo",
            "item_id": "fee-min",
            "value": "25,00",
            "unit": "R$",
            "minimum_amount": 25.0,
            "calculation_type": "minimum_amount",
            "modifier_type": "minimum_amount",
            "related_to": None,
            "classification_source": "legacy_classifier",
            "status": "needs_review",
        },
    ]
    result = validate_temp_table_for_confirmation(_valid_table(accessorial_fees=fees))
    assert result["can_confirm"] is False
    assert result["blocking_count"] == 3
    codes = {i["code"] for i in result["blocking_issues"]}
    assert CODE_UNMAPPED_CALCULATION_BASE in codes
    assert CODE_UNCONFIRMED_EXTRACTED_RULE in codes
    assert CODE_MINIMUM_WITHOUT_BASE in codes


def test_blocking_issues_imply_cannot_confirm(app_ctx):
    table = _valid_table(
        accessorial_fees=[
            {
                "name": "Ruído opcional",
                "value": "",
                "unit": "",
                "calculation_base_id": None,
                "calculation_basis": "",
                "classification_source": "legacy_classifier",
                "status": "needs_review",
            }
        ]
    )
    result = validate_temp_table_for_confirmation(table)
    assert result["blocking_count"] >= 1
    assert result["blocking_issues"]
    assert result["can_confirm"] is False


def test_accessorial_fee_item_identity_helper():
    from app.agente_compara_temp_table_validation_service import accessorial_fee_item_identity

    assert accessorial_fee_item_identity({"item_id": "a"}, 0) == "a"
    assert accessorial_fee_item_identity({"fee_id": "b"}, 1) == "b"
    assert accessorial_fee_item_identity({}, 2) == "accessorial_fees:2"


def test_removed_optional_item_does_not_block(app_ctx):
    table = _valid_table(accessorial_fees=[])
    result = validate_temp_table_for_confirmation(table)
    assert result["can_confirm"] is True


def test_multiple_blockers_and_deterministic_order(app_ctx):
    table = _valid_table(
        accessorial_fees=[
            _fee(
                name="B",
                item_id="fee-b",
                calculation_base_id=None,
                calculation_basis="não mapeado / revisar",
                classification_source="unmapped_calculation_base",
                operation=None,
                value="",
                status="needs_review",
            ),
            _fee(name="A", item_id="fee-a", value=""),
        ]
    )
    first = validate_temp_table_for_confirmation(table)
    second = validate_temp_table_for_confirmation(table)
    assert first["blocking_count"] == 2
    assert [issue["item_id"] for issue in first["blocking_issues"]] == [
        issue["item_id"] for issue in second["blocking_issues"]
    ]
    assert first["blocking_issues"][0]["index"] <= first["blocking_issues"][1]["index"]


def test_same_label_distinct_item_ids(app_ctx):
    table = _valid_table(
        accessorial_fees=[
            _fee(
                name="GRIS",
                item_id="fee-1",
                calculation_base_id=None,
                calculation_basis="não mapeado / revisar",
                classification_source="unmapped_calculation_base",
                operation=None,
                value="",
                status="needs_review",
            ),
            _fee(
                name="GRIS",
                item_id="fee-2",
                calculation_base_id=None,
                calculation_basis="não mapeado / revisar",
                classification_source="unmapped_calculation_base",
                operation=None,
                value="",
                status="needs_review",
            ),
        ]
    )
    result = validate_temp_table_for_confirmation(table)
    ids = [issue["item_id"] for issue in result["blocking_issues"]]
    assert ids == ["fee-1", "fee-2"]
    assert result["blocking_issues"][0]["label"] == result["blocking_issues"][1]["label"]


def test_does_not_mutate_input(app_ctx):
    table = _valid_table(
        accessorial_fees=[
            _fee(
                calculation_base_id=None,
                calculation_basis="não mapeado / revisar",
                classification_source="unmapped_calculation_base",
                operation=None,
                value="",
                status="needs_review",
            )
        ]
    )
    before = copy.deepcopy(table)
    validate_temp_table_for_confirmation(table)
    assert table == before


def test_json_safe_output(app_ctx):
    result = validate_temp_table_for_confirmation(_valid_table(reading_alerts=["ok"]))
    encoded = json.dumps(result)
    decoded = json.loads(encoded)
    assert decoded["can_confirm"] is True
    assert all(
        not (isinstance(v, float) and (math.isnan(v) or math.isinf(v)))
        for v in [decoded["blocking_count"]]
    )


def test_no_gemini_or_billing_or_cleide_imports():
    import app.agente_compara_temp_table_validation_service as mod

    source = open(mod.__file__, encoding="utf-8").read()
    body = source.split('"""', 2)[-1]
    assert "from app.cleide" not in source
    assert "import app.cleide" not in source
    assert "cleide_audit" not in source
    assert "billing" not in body.lower()
    assert "gemini" not in body.lower()
