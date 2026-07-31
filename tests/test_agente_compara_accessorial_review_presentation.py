"""View model público review_presentation das taxas (AgenteCompara)."""
from __future__ import annotations

import copy
import json
import os
import pathlib

import pytest

from app.services.agente_compara_config_service import (
    AgenteComparaConfig,
    DEFAULT_CALCULATION_BASES,
    DEFAULT_FALLBACK_MESSAGE,
)


def _js() -> str:
    return pathlib.Path("app/static/js/agente_compara.js").read_text(encoding="utf-8")


def _html() -> str:
    return pathlib.Path("app/templates/agente_compara.html").read_text(encoding="utf-8")


def _fn(js: str, name: str, next_name: str | None = None) -> str:
    start = js.index(f"function {name}")
    if next_name:
        end = js.index(f"function {next_name}", start + 1)
        return js[start:end]
    return js[start : start + 12000]


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
        calculation_bases=copy.deepcopy(DEFAULT_CALCULATION_BASES),
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


def _build(fee, *, index=0, fees=None, blocking_issue=None):
    from app.agente_compara_doc_service import (
        _accessorial_runtime_active_bases_by_id,
        _build_accessorial_fee_review_presentation,
    )

    fees = fees if fees is not None else [fee]
    return _build_accessorial_fee_review_presentation(
        fee,
        index=index,
        accessorial_fees=fees,
        blocking_issue=blocking_issue,
        active_bases_by_id=_accessorial_runtime_active_bases_by_id(),
    )


def test_mapped_configured_base(app_ctx):
    fee = {
        "name": "GRIS",
        "value": "0,35%",
        "unit": "%",
        "calculation_base_id": "pct_nota_fiscal",
        "calculation_base_label": "% por nota fiscal",
        "calculation_type": "invoice_percentage",
        "operation": "percentage_of_variable",
        "classification_source": "configured_calculation_base",
        "status": "calculable",
        "rate": 0.0035,
    }
    presentation = _build(fee)
    assert presentation["state"] == "resolved"
    assert presentation["basis_label"] == "% por nota fiscal"
    assert presentation["secondary_text"] is None
    assert presentation["requires_action"] is False
    assert presentation["is_blocking"] is False


def test_gris_invoice_percentage_recognized_without_public_base(app_ctx):
    fee = {
        "name": "GRIS",
        "value": "0,35%",
        "unit": "%",
        "rate": 0.0035,
        "calculation_base_id": None,
        "calculation_basis": "",
        "calculation_type": "invoice_percentage",
        "classification_source": "legacy_classifier",
        "classification_confidence": "high",
        "status": "calculable",
        "canonical_component": "risk_management",
        "modifier_type": "base_fee",
    }
    presentation = _build(fee)
    assert presentation["state"] == "resolved"
    assert presentation["basis_label"] == "Percentual sobre o valor da NF"
    assert "reconhecida automaticamente" in presentation["secondary_text"].lower()
    assert presentation["requires_action"] is False
    assert presentation["is_blocking"] is False
    assert presentation["severity"] == "info"


def test_tas_fixed_amount_recognized(app_ctx):
    fee = {
        "name": "TAS",
        "value": "10,00",
        "unit": "R$",
        "amount": 10.0,
        "calculation_base_id": None,
        "calculation_type": "fixed_amount",
        "classification_source": "legacy_classifier",
        "status": "calculable",
        "classification_confidence": "high",
        "canonical_component": "administrative_fee",
    }
    presentation = _build(fee)
    assert presentation["state"] == "resolved"
    assert presentation["basis_label"] == "Valor fixo"
    assert presentation["is_blocking"] is False


def test_pedagio_ceil_fraction_specific_label(app_ctx):
    fee = {
        "name": "Pedágio",
        "value": "5,00",
        "unit": "R$",
        "amount": 5.0,
        "calculation_base_id": "fracao_100kg",
        "calculation_base_label": "por fração de 100kg",
        "calculation_type": "weight_fraction",
        "operation": "ceil_fraction",
        "operation_parameters": {"fraction_size": 100},
        "classification_source": "configured_calculation_base",
        "status": "calculable",
        "canonical_component": "toll",
    }
    presentation = _build(fee)
    assert presentation["state"] == "resolved"
    assert "fração" in presentation["basis_label"].lower()
    assert presentation["is_blocking"] is False


def test_minimum_linked_modifier(app_ctx):
    gris = {
        "name": "GRIS",
        "item_id": "fee-gris",
        "value": "0,35%",
        "unit": "%",
        "rate": 0.0035,
        "calculation_type": "invoice_percentage",
        "classification_source": "legacy_classifier",
        "status": "calculable",
        "component_group": "risk_management",
        "canonical_component": "risk_management",
        "modifier_type": "base_fee",
    }
    minimum = {
        "name": "GRIS Mínimo",
        "item_id": "fee-gris-min",
        "value": "5,00",
        "unit": "R$",
        "minimum_amount": 5.0,
        "calculation_type": "minimum_amount",
        "modifier_type": "minimum_amount",
        "related_to": "risk_management",
        "component_group": "risk_management",
        "classification_source": "legacy_classifier",
        "status": "calculable",
    }
    fees = [gris, minimum]
    presentation = _build(minimum, index=1, fees=fees)
    assert presentation["state"] == "resolved"
    assert presentation["basis_label"] == "Mínimo aplicável a GRIS"
    assert presentation["related_to_label"] == "GRIS"
    assert presentation["requires_action"] is False
    assert presentation["is_blocking"] is False


def test_minimum_without_link_unresolved(app_ctx):
    minimum = {
        "name": "Mínimo solto",
        "value": "5,00",
        "unit": "R$",
        "minimum_amount": 5.0,
        "calculation_type": "minimum_amount",
        "modifier_type": "minimum_amount",
        "related_to": None,
        "classification_source": "legacy_classifier",
        "status": "needs_review",
    }
    presentation = _build(
        minimum,
        blocking_issue={
            "reason_code": "missing_minimum_base_link",
            "message": "Esta regra mínima não possui uma taxa principal válida vinculada.",
        },
    )
    assert presentation["state"] == "blocking"
    assert presentation["requires_action"] is True
    assert presentation["is_blocking"] is True
    assert presentation["reason_code"] == "missing_minimum_base_link"


def test_extraction_hypothesis_blocking_when_issue_present(app_ctx):
    fee = {
        "name": "Ruído opcional",
        "value": "",
        "unit": "",
        "calculation_base_id": None,
        "calculation_basis": "",
        "classification_source": "legacy_classifier",
        "status": "needs_review",
    }
    presentation = _build(
        fee,
        blocking_issue={
            "reason_code": "unconfirmed_extracted_rule",
            "message": "Selecione a base de cálculo de Ruído opcional.",
        },
    )
    assert presentation["state"] == "blocking"
    assert presentation["requires_action"] is True
    assert presentation["is_blocking"] is True
    assert presentation["severity"] == "error"
    assert presentation["reason_code"] == "unconfirmed_extracted_rule"
    assert "confirme se necessário" not in (presentation.get("secondary_text") or "").lower()
    assert "atenção" not in (presentation.get("secondary_text") or "").lower()


def test_extraction_hypothesis_without_issue_is_informational_not_warning(app_ctx):
    fee = {
        "name": "Ruído opcional",
        "value": "",
        "unit": "",
        "calculation_base_id": None,
        "calculation_basis": "",
        "classification_source": "legacy_classifier",
        "status": "needs_review",
    }
    presentation = _build(fee)  # no blocking_issue
    assert presentation["state"] == "informational"
    assert presentation["requires_action"] is False
    assert presentation["is_blocking"] is False
    assert presentation["severity"] == "info"
    assert presentation.get("secondary_text") in (None, "")


def test_classification_source_unmapped_unresolved(app_ctx):
    fee = {
        "name": "Pedagio geral",
        "value": "",
        "unit": "",
        "calculation_base_id": None,
        "calculation_basis": "não mapeado / revisar",
        "classification_source": "unmapped_calculation_base",
        "status": "needs_review",
    }
    presentation = _build(
        fee,
        blocking_issue={
            "reason_code": "missing_calculation_base",
            "message": "Defina a base de cálculo antes de continuar.",
        },
    )
    assert presentation["state"] == "blocking"
    assert presentation["basis_label"] == "Base de cálculo não identificada"
    assert presentation["is_blocking"] is True
    assert presentation["requires_action"] is True


def test_validation_issue_associated_and_no_false_blocker(app_ctx):
    from app.agente_compara_doc_service import _public_temp_table

    recognized = {
        "name": "GRIS",
        "item_id": "fee-gris",
        "value": "0,35%",
        "unit": "%",
        "rate": 0.0035,
        "calculation_base_id": None,
        "calculation_type": "invoice_percentage",
        "classification_source": "legacy_classifier",
        "status": "calculable",
        "classification_confidence": "high",
    }
    unresolved = {
        "name": "Pedagio geral",
        "item_id": "fee-ped",
        "value": "",
        "unit": "",
        "calculation_base_id": None,
        "calculation_basis": "não mapeado / revisar",
        "classification_source": "unmapped_calculation_base",
        "status": "needs_review",
    }
    record = {
        "temp_table_id": "tt-review-1",
        "status": "needs_review",
        "accessorial_fees": [recognized, unresolved],
        "reading_alerts": [],
        "uncertain_fields": [],
    }
    before = copy.deepcopy(record)
    public = _public_temp_table(record)
    assert record == before
    assert public["validation"]["can_confirm"] is False
    assert public["accessorial_fees"][0]["review_presentation"]["state"] == "resolved"
    assert public["accessorial_fees"][0]["review_presentation"]["is_blocking"] is False
    assert public["accessorial_fees"][1]["review_presentation"]["state"] == "blocking"
    assert public["accessorial_fees"][1]["review_presentation"]["is_blocking"] is True
    assert public["accessorial_fees"][1]["review_presentation"]["reason_code"] == "missing_calculation_base"


def test_json_safe_deterministic_and_no_mutation(app_ctx):
    from app.agente_compara_doc_service import _public_temp_table

    fee = {
        "name": "Retorno",
        "item_id": "fee-ret",
        "value": "50%",
        "unit": "%",
        "rate": 0.5,
        "calculation_base_id": None,
        "calculation_type": "freight_percentage",
        "classification_source": "legacy_classifier",
        "status": "calculable",
        "classification_confidence": "high",
    }
    record = {
        "temp_table_id": "tt-review-2",
        "status": "needs_review",
        "accessorial_fees": [fee],
        "reading_alerts": [],
        "uncertain_fields": [],
    }
    before = copy.deepcopy(record)
    first = _public_temp_table(record)
    second = _public_temp_table(record)
    assert record == before
    assert "review_presentation" not in record["accessorial_fees"][0]
    encoded = json.dumps(first["accessorial_fees"][0]["review_presentation"])
    assert json.loads(encoded) == first["accessorial_fees"][0]["review_presentation"]
    assert (
        first["accessorial_fees"][0]["review_presentation"]
        == second["accessorial_fees"][0]["review_presentation"]
    )
    assert first["accessorial_fees"][0]["review_presentation"]["state"] == "resolved"
    assert (
        first["accessorial_fees"][0]["review_presentation"]["basis_label"]
        == "Percentual sobre o frete de envio"
    )


@pytest.mark.parametrize("slot_number", [1, 2, 3])
def test_review_presentation_same_contract_slots(app_ctx, slot_number):
    from app.agente_compara_doc_service import _public_temp_table

    record = {
        "temp_table_id": f"tt-slot-{slot_number}",
        "status": "needs_review",
        "accessorial_fees": [
            {
                "name": "Reentrega",
                "value": "100%",
                "unit": "%",
                "rate": 1.0,
                "calculation_type": "freight_percentage",
                "classification_source": "legacy_classifier",
                "status": "calculable",
            }
        ],
        "reading_alerts": [],
        "uncertain_fields": [],
        "slot_number": slot_number,
    }
    public = _public_temp_table(record)
    presentation = public["accessorial_fees"][0]["review_presentation"]
    assert presentation["state"] == "resolved"
    assert presentation["basis_label"] == "Percentual sobre o frete de envio"
    assert set(presentation) >= {
        "state",
        "basis_label",
        "secondary_text",
        "requires_action",
        "is_blocking",
        "severity",
        "source",
    }


def test_reentrega_minimum_linked_label(app_ctx):
    reentrega = {
        "name": "Reentrega",
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
    presentation = _build(minimum, index=1, fees=[reentrega, minimum])
    assert presentation["state"] == "resolved"
    assert presentation["basis_label"] == "Mínimo aplicável a Reentrega"


def test_frontend_renderer_prefers_review_presentation():
    js = _js()
    html = _html()
    fn = _fn(js, "appendReadonlyCalculationBasisCell", "calculationBaseOptionLabel")
    assert "item.review_presentation" in fn
    assert "basis_label" in fn
    assert "secondary_text" in fn
    assert "requires_action" in fn
    assert "accessorial-basis-review--" in fn
    assert "aria-label" in fn
    assert "Base de cálculo não identificada" in fn
    assert "Base não classificada" in fn
    assert "confirme se necessário" not in fn
    assert "Atenção" not in fn
    assert "Bloqueante" in fn
    assert "Selecione a base de cálculo antes de continuar." in fn
    # Não usar o literal genérico como fallback único para ausência de base ID.
    assert 'td.textContent = \'não mapeado / revisar\'' not in fn
    assert "não mapeado / revisar" not in fn.split("Compatibilidade com payload legado")[0]
    assert "accessorial-basis-review__badge" in html
    assert "accessorial-basis-review--action-required" in html
    assert "function markAccessorialFeeAsUnmapped" in js
    assert "agenteComparaTempTableModalEdit" in js
    assert "tempTableConfirmationCanProceed" in js
    assert "validation.can_confirm" in js or "can_confirm" in js
    assert "Resolva " in js
    assert "antes de salvar e avançar" in js


def test_frontend_legacy_fallback_does_not_generic_unmapped_revisar():
    js = _js()
    fn = _fn(js, "appendReadonlyCalculationBasisCell", "calculationBaseOptionLabel")
    legacy = fn.split("Compatibilidade com payload legado")[-1]
    assert "Base de cálculo não identificada" in legacy
    assert "Mínimo aplicável a " in legacy
    assert "Base não classificada" in legacy
    assert "não mapeado / revisar" in legacy  # apenas para filtrar texto extraído legado
    assert "td.textContent = 'não mapeado / revisar'" not in legacy


def test_public_payload_presentation_derived_from_validation_no_orphan(app_ctx):
    from app.agente_compara_doc_service import _public_temp_table

    record = {
        "temp_table_id": "tt-orphan",
        "status": "needs_review",
        "accessorial_fees": [
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
        ],
        "reading_alerts": [],
        "uncertain_fields": [],
    }
    public = _public_temp_table(record)
    validation = public["validation"]
    assert validation["can_confirm"] is False
    assert validation["blocking_count"] == 2
    for fee in public["accessorial_fees"]:
        pres = fee["review_presentation"]
        if pres["is_blocking"]:
            assert pres["requires_action"] is True
            assert pres["severity"] == "error"
            assert pres["state"] == "blocking"
            item_id = fee["item_id"]
            assert any(i["item_id"] == item_id for i in validation["blocking_issues"])
        assert pres["severity"] != "warning"
        text = (pres.get("secondary_text") or "") + (pres.get("basis_label") or "")
        assert "atenção" not in text.lower()
        assert "confirme se necessário" not in text.lower()


def test_requires_action_implies_blocking_fields(app_ctx):
    from app.agente_compara_doc_service import _public_temp_table

    record = {
        "temp_table_id": "tt-invariant",
        "status": "needs_review",
        "accessorial_fees": [
            {
                "name": "Ruído opcional",
                "item_id": "fee-noise",
                "value": "",
                "unit": "",
                "calculation_base_id": None,
                "calculation_basis": "",
                "classification_source": "legacy_classifier",
                "status": "needs_review",
            },
            {
                "name": "GRIS",
                "item_id": "fee-gris",
                "value": "0,35%",
                "unit": "%",
                "rate": 0.0035,
                "calculation_type": "invoice_percentage",
                "classification_source": "legacy_classifier",
                "status": "calculable",
                "classification_confidence": "high",
            },
        ],
        "reading_alerts": [],
        "uncertain_fields": [],
    }
    public = _public_temp_table(record)
    for fee in public["accessorial_fees"]:
        pres = fee["review_presentation"]
        if pres.get("requires_action") is True:
            assert pres["is_blocking"] is True
            assert pres["severity"] == "error"
            assert pres["state"] == "blocking"
            assert public["validation"]["can_confirm"] is False
            assert public["validation"]["blocking_count"] >= 1
