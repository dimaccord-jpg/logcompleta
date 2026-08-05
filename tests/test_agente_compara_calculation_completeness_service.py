"""Testes do gate de completeza do AgenteCompara."""
from __future__ import annotations

import copy
import json
import math

import pytest

from app.agente_compara_calculation_completeness_service import (
    STATUS_CALCULATED,
    STATUS_CALCULATED_WITH_WARNINGS,
    STATUS_INCOMPLETE,
    STATUS_NOT_CALCULATED,
    classify_accessorial_execution_support,
    classify_ignored_component,
    evaluate_calculation_completeness,
)
from app.agente_compara_calculation_memory_service import build_calculation_memory
from app.agente_compara_calculation_service import (
    STATUS_CALCULATED as ROW_STATUS_CALCULATED,
    STATUS_INCOMPLETE as ROW_STATUS_INCOMPLETE,
    STATUS_INVALID_WEIGHT,
    _normalize_row_result,
    calculate_single_table,
)
from app.agente_compara_temp_table_validation_service import (
    CODE_UNSUPPORTED_CONDITION,
    CODE_UNSUPPORTED_OPERATION,
    validate_temp_table_for_confirmation,
)
from tests.test_agente_compara_single_table_calculation import (
    _coverage_rows,
    _make_context,
    _pricing_record,
    _row,
)


def _raw_with_ignored(*ignored, expected_freight=94.79):
    return {
        "expected_freight": expected_freight,
        "status": "",
        "calculation_components": {
            "weight_freight": {"amount": 80.0, "details": "Até 20 kg"},
            "accessorial_fees": [
                {
                    "label": "ADV",
                    "amount": 10.0,
                    "calculation_type": "invoice_percentage",
                    "reason_code": "accessorial_percentage_calculated",
                },
                {
                    "label": "GRIS",
                    "amount": 4.79,
                    "calculation_type": "invoice_percentage",
                    "reason_code": "accessorial_percentage_calculated",
                },
            ],
            "ignored_accessorial_fees": list(ignored),
            "subtotal_before_taxes": expected_freight,
        },
    }


def test_completeness_full_calculation():
    raw = _raw_with_ignored()
    before = copy.deepcopy(raw)
    result = evaluate_calculation_completeness(raw, calculated_freight=94.79)
    assert result["status"] == STATUS_CALCULATED
    assert result["is_complete"] is True
    assert result["has_partial_value"] is False
    assert result["blocking_issues"] == []
    assert raw == before


def test_completeness_not_applicable_remains_complete():
    raw = _raw_with_ignored(
        {
            "label": "Taxa condicional",
            "canonical_component": "tas",
            "reason_code": "not_applicable",
        }
    )
    result = evaluate_calculation_completeness(raw, calculated_freight=94.79)
    assert result["status"] == STATUS_CALCULATED
    assert result["is_complete"] is True
    assert result["critical_ignored_component_count"] == 0
    assert result["benign_ignored_count"] == 1


def test_completeness_optional_benign_warning():
    raw = _raw_with_ignored(
        {
            "label": "Taxa opcional",
            "reason_code": "classification_warning_present",
        }
    )
    result = evaluate_calculation_completeness(raw, calculated_freight=94.79)
    assert result["status"] == STATUS_CALCULATED_WITH_WARNINGS
    assert result["is_complete"] is True
    assert result["has_partial_value"] is False
    assert len(result["warnings"]) == 1


def test_completeness_unsupported_condition_becomes_warning_without_proven_obligation():
    raw = _raw_with_ignored(
        {
            "label": "TAS",
            "canonical_component": "tas",
            "reason_code": "unsupported_accessorial_condition",
        },
        {
            "label": "Pedágio",
            "canonical_component": "toll",
            "reason_code": "conditions_present",
        },
    )
    result = evaluate_calculation_completeness(raw, calculated_freight=94.79)
    assert result["status"] == STATUS_CALCULATED_WITH_WARNINGS
    assert result["is_complete"] is True
    assert result["has_partial_value"] is False
    assert result["partial_value"] is None
    assert result["critical_ignored_component_count"] == 0
    codes = {item["reason_code"] for item in result["warnings"]}
    assert "unsupported_accessorial_condition" in codes
    assert "conditions_present" in codes


def test_completeness_unsupported_operation_incomplete():
    raw = _raw_with_ignored(
        {"label": "Taxa X", "reason_code": "unsupported_operation"}
    )
    result = evaluate_calculation_completeness(raw, calculated_freight=50.0)
    assert result["status"] == STATUS_INCOMPLETE


def test_completeness_missing_required_incomplete():
    raw = _raw_with_ignored(
        {"label": "Taxa Y", "reason_code": "missing_audit_variable"}
    )
    result = evaluate_calculation_completeness(raw, calculated_freight=50.0)
    assert result["status"] == STATUS_INCOMPLETE


def test_completeness_invalid_rule_incomplete():
    raw = _raw_with_ignored({"label": "Taxa Z", "reason_code": "invalid_amount"})
    result = evaluate_calculation_completeness(raw, calculated_freight=50.0)
    assert result["status"] == STATUS_INCOMPLETE


def test_completeness_not_calculated_without_principal():
    result = evaluate_calculation_completeness({"status": "invalid_weight"}, calculated_freight=None)
    assert result["status"] == STATUS_NOT_CALCULATED
    assert result["is_complete"] is False
    assert result["has_partial_value"] is False


def test_completeness_deterministic_order_and_json_safe():
    raw = _raw_with_ignored(
        {"label": "B", "reason_code": "unsupported_operation", "canonical_component": "b"},
        {"label": "A", "reason_code": "conditions_present", "canonical_component": "a"},
    )
    first = evaluate_calculation_completeness(raw, calculated_freight=10.0)
    second = evaluate_calculation_completeness(raw, calculated_freight=10.0)
    assert first == second
    encoded = json.dumps(first)
    decoded = json.loads(encoded)
    assert decoded["status"] == STATUS_INCOMPLETE
    assert all(
        not (isinstance(v, float) and (math.isnan(v) or math.isinf(v)))
        for v in [decoded["partial_value"], decoded["critical_ignored_component_count"]]
    )


def test_completeness_no_mutation():
    raw = _raw_with_ignored({"label": "TAS", "reason_code": "unsupported_accessorial_condition"})
    before = copy.deepcopy(raw)
    evaluate_calculation_completeness(raw, calculated_freight=94.79)
    assert raw == before


def test_completeness_no_gemini_billing_cleide_imports():
    import app.agente_compara_calculation_completeness_service as mod

    source = open(mod.__file__, encoding="utf-8").read()
    body = source.split('"""', 2)[-1]
    assert "from app.cleide" not in source
    assert "import app.cleide" not in source
    assert "cleide_audit" not in source
    assert "gemini" not in body.lower()
    assert "billing" not in body.lower()
    assert "flask" not in body.lower()


def test_classify_support_accepts_simple_fixed_amount_condition_for_supported_base():
    support = classify_accessorial_execution_support(
        {
            "name": "TAS",
            "operation": "fixed_amount",
            "conditions": "somente para carga fracionada especial",
            "classification_source": "manual_configured_calculation_base",
            "calculation_base_id": "por_cte",
        }
    )
    assert support["supported"] is True
    assert support["blocking"] is False
    assert support["reason_code"] is None


def test_classify_support_blocks_multiply_by_variable():
    support = classify_accessorial_execution_support(
        {
            "name": "Taxa",
            "operation": "multiply_by_variable",
            "classification_source": "manual_configured_calculation_base",
        }
    )
    assert support["supported"] is False
    assert support["blocking"] is True
    assert support["reason_code"] == "unsupported_operation"


def test_classify_support_not_applicable_not_blocking():
    support = classify_accessorial_execution_support(
        {
            "name": "Taxa",
            "applicable": False,
            "conditions": "qualquer texto",
            "operation": "fixed_amount",
        }
    )
    assert support["blocking"] is False
    assert support["reason_code"] == "not_applicable"


def test_validator_allows_simple_fixed_amount_condition_for_supported_base(app, monkeypatch):
    from app.services.agente_compara_config_service import (
        AgenteComparaConfig,
        DEFAULT_FALLBACK_MESSAGE,
    )

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
    with app.app_context():
        table = {
            "accessorial_fees": [
                {
                    "name": "TAS",
                    "value": "10,00",
                    "unit": "R$",
                    "calculation_base_id": "por_cte",
                    "calculation_basis": "por CTe",
                    "classification_source": "manual_configured_calculation_base",
                    "operation": "fixed_amount",
                    "calculation_type": "fixed_amount",
                    "status": "calculable",
                    "conditions": "quando a remessa for especial",
                }
            ]
        }
        result = validate_temp_table_for_confirmation(table)
        assert result["can_confirm"] is True
        assert result["blocking_issues"] == []


def test_validator_blocks_unsupported_operation(app, monkeypatch):
    from app.services.agente_compara_config_service import (
        AgenteComparaConfig,
        DEFAULT_FALLBACK_MESSAGE,
    )

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
    with app.app_context():
        table = {
            "accessorial_fees": [
                {
                    "name": "Taxa",
                    "value": "10,00",
                    "unit": "R$",
                    "calculation_base_id": "por_cte",
                    "calculation_basis": "por CTe",
                    "classification_source": "manual_configured_calculation_base",
                    "operation": "multiply_by_variable",
                    "audit_variable": "peso",
                    "calculation_type": "fixed_amount",
                    "status": "calculable",
                }
            ]
        }
        result = validate_temp_table_for_confirmation(table)
        assert result["can_confirm"] is False
        assert result["blocking_issues"][0]["code"] == CODE_UNSUPPORTED_OPERATION


def test_normalize_row_ignored_condition_preserves_calculated_with_warnings():
    raw = _raw_with_ignored(
        {
            "label": "TAS",
            "canonical_component": "tas",
            "reason_code": "unsupported_accessorial_condition",
        },
        {
            "label": "Pedágio",
            "canonical_component": "toll",
            "reason_code": "conditions_present",
        },
    )
    source = _row(
        destination_city="Caruaru",
        destination_uf="PE",
        weight=13.6,
        invoice_value=1500.0,
    )
    result = _normalize_row_result(raw, source, table_id="t1", slot_number=1, carrier_name="Gbex")
    assert result["status"] == STATUS_CALCULATED_WITH_WARNINGS
    assert result["final_status"] == STATUS_CALCULATED_WITH_WARNINGS
    assert result["is_partial_value"] is False
    assert result["calculated_freight"] == 94.79
    assert result["completeness"]["critical_ignored_component_count"] == 0
    memory = result["calculation_memory"]
    assert memory["status"] == "calculated_with_warnings"
    assert memory["status_label"] == "Calculado com ressalvas"
    assert memory["total_label"] == "Total calculado"
    assert memory["is_partial_value"] is False
    assert memory["total"] == 94.79


def test_normalize_row_not_applicable_stays_calculated():
    raw = _raw_with_ignored(
        {
            "label": "Taxa condicional",
            "reason_code": "not_applicable",
        }
    )
    result = _normalize_row_result(raw, _row(), table_id="t1", slot_number=1, carrier_name="Ctrl")
    assert result["status"] == ROW_STATUS_CALCULATED
    assert result["final_status"] == ROW_STATUS_CALCULATED
    assert result["is_partial_value"] is False
    assert result["calculated_freight"] == 94.79
    assert result["calculation_memory"]["status"] == "calculated"
    assert result["calculation_memory"]["total_label"] == "Total calculado"


def test_normalize_row_domain_error_clears_freight():
    raw = {
        "status": STATUS_INVALID_WEIGHT,
        "expected_freight": None,
        "diagnostic": {"message": "Peso inválido"},
    }
    result = _normalize_row_result(raw, _row(weight=None), table_id="t1", slot_number=1)
    assert result["status"] == STATUS_INVALID_WEIGHT
    assert result["calculated_freight"] is None
    assert result["is_partial_value"] is False


def _configured_fee(*, name: str, conditions: str | None = None, **extra) -> dict:
    fee = {
        "name": name,
        "value": "10,00",
        "unit": "R$",
        "calculation_base_id": "por_cte",
        "calculation_basis": "por CTe",
        "classification_source": "configured_calculation_base",
        "operation": "fixed_amount",
        "calculation_type": "fixed_amount",
        "status": "calculable",
        "classification_confidence": "high",
        "notes": "",
    }
    if conditions is not None:
        fee["conditions"] = conditions
    fee.update(extra)
    return fee


def _conditional_ignored_fee(*, name: str, value: str = "10,00", unit: str = "R$") -> dict:
    """Taxa com condição textual que o motor ignora como unsupported (sem avaliar aplicabilidade)."""
    return {
        "name": name,
        "value": value,
        "unit": unit,
        "calculation_basis": "sobre nota fiscal" if unit == "%" else "por CTe",
        "notes": "somente para remessas especiais",
    }


def test_gbex_equivalent_calculated_with_warnings_with_tas_and_pedagio():
    """Cenário sintético equivalente ao Gbex: TAS/Pedágio com condição não suportada."""
    record = _pricing_record(
        region="PE-Caruaru",
        weight_30="80,00",
        weight_50="95,00",
        accessorial_fees=[
            {
                "name": "ADV",
                "value": "0,30%",
                "unit": "%",
                "calculation_basis": "sobre nota fiscal",
                "notes": "",
            },
            {
                "name": "GRIS",
                "value": "0,10%",
                "unit": "%",
                "calculation_basis": "sobre nota fiscal",
                "notes": "",
            },
            _conditional_ignored_fee(name="TAS", value="10,00", unit="R$"),
            _conditional_ignored_fee(name="Pedágio", value="1,50", unit="R$"),
        ],
    )
    ctx = _make_context(
        record=record,
        coverage=_coverage_rows(("PE", "Caruaru", "PE-Caruaru")),
        rows=[
            _row(
                destination_city="Caruaru",
                destination_uf="PE",
                weight=13.6,
                invoice_value=2000.0,
            )
        ],
        carrier_name="Transportadora Sintetica A",
    )
    result = calculate_single_table(ctx)
    row = result["results"][0]
    assert row["status"] == STATUS_CALCULATED_WITH_WARNINGS
    assert row["final_status"] == STATUS_CALCULATED_WITH_WARNINGS
    assert row["is_partial_value"] is False
    assert row["calculated_freight"] is not None
    ignored = (row.get("components") or {}).get("ignored_accessorial_fees") or []
    labels = {str(item.get("label") or "") for item in ignored}
    reasons = {str(item.get("reason_code") or "") for item in ignored}
    assert "TAS" in labels
    assert "Pedágio" in labels
    assert "conditions_present" in reasons or "unsupported_accessorial_condition" in reasons
    memory = row["calculation_memory"]
    assert memory["status"] == "calculated_with_warnings"
    assert memory["status_label"] == "Calculado com ressalvas"
    assert memory["total_label"] == "Total calculado"
    assert result["incomplete_count"] == 0
    assert result["calculated_with_warnings_count"] == 1


def test_supported_pedagio_configured_remains_calculated(monkeypatch):
    """Controle: pedágio configurado sem condição permanece completo."""
    from types import SimpleNamespace

    from app.services.agente_compara_config_service import DEFAULT_CALCULATION_BASES

    cfg = SimpleNamespace(
        calculation_bases=copy.deepcopy(DEFAULT_CALCULATION_BASES),
        upload_ttl_hours=24,
    )
    monkeypatch.setattr("app.agente_compara_doc_service.get_agente_compara_config", lambda: cfg)

    record = _pricing_record(
        region="SP-Interior 1",
        accessorial_fees=[
            _configured_fee(name="Pedágio", canonical_component="toll", value="3,50"),
            {
                "name": "GRIS",
                "value": "0,10%",
                "unit": "%",
                "calculation_basis": "sobre nota fiscal",
                "notes": "",
            },
        ],
    )
    result = calculate_single_table(
        _make_context(record=record, carrier_name="Transportadora Sintetica B")
    )
    row = result["results"][0]
    assert row["status"] == ROW_STATUS_CALCULATED
    assert row["final_status"] == ROW_STATUS_CALCULATED
    assert row["is_partial_value"] is False
    accessorials = (row.get("components") or {}).get("accessorials") or []
    labels = {str(item.get("label") or item.get("name") or "") for item in accessorials}
    assert "Pedágio" in labels
    assert result["calculated_count"] == 1
    assert result["incomplete_count"] == 0


def test_memory_incomplete_does_not_call_partial_total_completo():
    raw = _raw_with_ignored(
        {"label": "TAS", "reason_code": "unsupported_accessorial_condition"}
    )
    completeness = evaluate_calculation_completeness(raw, calculated_freight=94.79)
    memory = build_calculation_memory(
        raw,
        calculated_freight=94.79,
        status="incomplete",
        completeness=completeness,
        is_partial_value=True,
        row_index=1,
        carrier_name="Gbex",
    )
    assert memory["status"] == "incomplete"
    assert memory["total_label"] == "Valor parcial calculado"
    assert memory["status_label"] == "Cálculo incompleto"
    assert memory["total_label"] != "Total calculado"
    assert any(
        item.get("ignored") and "TAS" in str(item.get("label") or "")
        for item in memory["components"]
    )


def test_classify_ignored_unknown_reason_is_warning():
    classified = classify_ignored_component({"label": "X", "reason_code": "algo_novo_desconhecido"})
    assert classified["severity"] == "warning"


def test_completeness_required_applicable_unsupported_condition_stays_incomplete():
    raw = _raw_with_ignored(
        {
            "label": "TAS obrigat?ria",
            "reason_code": "unsupported_accessorial_condition",
            "required": True,
            "applicable": True,
        }
    )
    result = evaluate_calculation_completeness(raw, calculated_freight=94.79)
    assert result["status"] == STATUS_INCOMPLETE
    assert result["critical_ignored_component_count"] == 1
    assert result["blocking_issues"][0]["reason_code"] == "unsupported_accessorial_condition"
