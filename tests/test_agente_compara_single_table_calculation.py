"""Testes do motor determinístico unitário do AgenteCompara (Etapa 3)."""
from __future__ import annotations

import copy
import importlib
import inspect
import json
import pathlib
import time
from uuid import uuid4

import pytest

from app.agente_compara_calculation_service import (
    ERROR_IDENTITY_MISMATCH,
    ERROR_OPERATIONAL_FILE_EMPTY,
    ERROR_SLOT_MISMATCH,
    ERROR_TABLE_DATA_MISSING,
    ERROR_TABLE_NOT_CONFIRMED,
    ERROR_TABLE_NOT_FOUND,
    ERROR_TEMP_TABLE_MISMATCH,
    ERROR_TEMP_TABLE_MISSING,
    ERROR_COMPARISON_NOT_FOUND,
    ERROR_UNEXPECTED_CALCULATION,
    STATUS_AMBIGUOUS_COVERAGE_MAPPING,
    STATUS_CALCULATED,
    STATUS_INVALID_INVOICE_VALUE,
    STATUS_INVALID_WEIGHT,
    STATUS_MISSING_COVERAGE_MAPPING,
    STATUS_MISSING_FREIGHT_RULE,
    SingleTableCalculationContext,
    TableOwnershipError,
    InvalidCalculationContextError,
    UnexpectedSingleTableCalculationError,
    build_single_table_calculation_context,
    calculate_single_table,
)
from app.agente_compara_comparison_state import (
    TABLE_STATUS_CONFIRMED,
    TABLE_STATUS_NEEDS_REVIEW,
    create_comparison,
    get_table_by_slot,
    persist_comparison_state,
)
from app.agente_compara_doc_service import (
    AUDIT_STATUS_DIVERGENT,
    AUDIT_STATUS_INVALID_CHARGED_FREIGHT,
    AUDIT_STATUS_INVALID_WEIGHT,
    AUDIT_STATUS_MISSING_COVERAGE,
    AUDIT_STATUS_OK,
    _audit_single_row,
    _write_temp_table_atomic,
    _temp_table_path,
    build_coverage_index,
    build_freight_pricing_index,
)

FORBIDDEN_RESULT_KEYS = {
    "charged_freight",
    "freight_charged",
    "expected_freight",
    "difference",
    "divergence",
    "divergence_value",
    "overcharged",
    "undercharged",
    "divergent",
    "match",
}


def _pricing_record(
    *,
    region: str = "SP-Interior 1",
    weight_30: str = "87,13",
    weight_50: str = "100,50",
    excess: str = "2,00",
    freight_value_header: str | None = None,
    freight_value: str | None = None,
    accessorial_fees: list[dict] | None = None,
    temp_table_id: str | None = None,
) -> dict:
    columns = ["Região de frete", "Até 30 kg", "31 a 50 kg", "Excedente kg"]
    row = {
        "Região de frete": region,
        "Até 30 kg": weight_30,
        "31 a 50 kg": weight_50,
        "Excedente kg": excess,
    }
    if freight_value_header and freight_value is not None:
        columns.append(freight_value_header)
        row[freight_value_header] = freight_value
    return {
        "temp_table_id": temp_table_id or uuid4().hex,
        "status": "needs_review",
        "freight_tables": [
            {
                "table_title": "Tabela por região",
                "table_type": "weight_range_table",
                "columns": columns,
                "rows": [row],
            }
        ],
        "freight_routes": [],
        "accessorial_fees": list(accessorial_fees or []),
        "coverage_table": None,
        "audit_batch": None,
    }


def _coverage_rows(*pairs: tuple[str, str, str]) -> dict:
    rows = [
        {
            "destination_uf": uf,
            "destination_city": city,
            "freight_region": region,
        }
        for uf, city, region in pairs
    ]
    return {
        "status": "needs_review",
        "columns": ["UF destino", "Cidade destino", "Região de frete"],
        "rows": rows,
    }


def _row(
    *,
    row_index: int = 1,
    document_number: str = "7400455",
    destination_city: str = "Campinas",
    destination_uf: str = "SP",
    weight: float = 48.0,
    invoice_value: float | None = 1000.0,
    **extra,
) -> dict:
    payload = {
        "row_index": row_index,
        "document_number": document_number,
        "destination_city": destination_city,
        "destination_uf": destination_uf,
        "audited_weight": weight,
    }
    if invoice_value is not None:
        payload["invoice_value"] = invoice_value
    payload.update(extra)
    return payload


def _tax_config(
    *,
    include_taxes: bool = True,
    origin_uf: str = "SP",
    origin_city: str = "São Paulo",
    destination_uf: str = "RJ",
    rate: float = 12.0,
    selected_table_ids: list[str] | None = None,
    iss_rate: float | None = None,
) -> dict:
    return {
        "include_taxes": include_taxes,
        "origin_uf": origin_uf,
        "origin_city": origin_city,
        "iss_rate": iss_rate,
        "selected_table_ids": list(selected_table_ids or []),
        "destination_ufs": [{"uf": destination_uf, "source": "manual", "evidence": []}],
        "icms_rates": [
            {
                "destination_uf": destination_uf,
                "applied_rate": rate,
                "suggested_rate": rate,
                "is_active": True,
                "user_edited": False,
                "operation_type": "interstate",
            }
        ],
        "confirmed": True,
    }


def _gris_fee(rate_percent: str = "1,00%") -> dict:
    """Generalidade simples no formato aceito pelo normalizador runtime."""
    return {
        "name": "GRIS",
        "value": rate_percent,
        "unit": "%",
        "calculation_basis": "sobre nota fiscal",
        "notes": "",
    }


def _make_context(
    *,
    record: dict | None = None,
    rows: list[dict] | None = None,
    coverage: dict | None = None,
    tax_config: dict | None = None,
    comparison_id: str | None = None,
    table_id: str | None = None,
    temp_table_id: str | None = None,
    slot_number: int = 1,
    carrier_name: str = "Transportadora Alfa",
    primary_temp_table_id: str | None = None,
) -> SingleTableCalculationContext:
    record = copy.deepcopy(record or _pricing_record())
    temp_table_id = temp_table_id or record.get("temp_table_id") or uuid4().hex
    record["temp_table_id"] = temp_table_id
    coverage = coverage if coverage is not None else _coverage_rows(("SP", "Campinas", "SP-Interior 1"))
    rows = rows if rows is not None else [_row()]
    return SingleTableCalculationContext(
        comparison_id=comparison_id or uuid4().hex,
        table_id=table_id or uuid4().hex,
        temp_table_id=temp_table_id,
        slot_number=slot_number,
        carrier_name=carrier_name,
        table_record=record,
        normalized_rows=rows,
        tax_config=tax_config,
        coverage_table=coverage,
        primary_temp_table_id=primary_temp_table_id,
    )


def _assert_no_forbidden_fields(payload: dict) -> None:
    blob = json.dumps(payload, ensure_ascii=False)
    for key in FORBIDDEN_RESULT_KEYS:
        assert f'"{key}"' not in blob
    for result in payload.get("results") or []:
        for key in FORBIDDEN_RESULT_KEYS:
            assert key not in result


def test_simple_single_table_calculation():
    result = calculate_single_table(_make_context())
    assert result["schema_version"] == 1
    assert result["row_count"] == 1
    assert result["calculated_count"] == 1
    assert result["error_count"] == 0
    assert result["results"][0]["status"] == STATUS_CALCULATED
    assert result["results"][0]["calculated_freight"] == 100.50
    assert result["summary"]["total_calculated_freight"] == 100.50
    _assert_no_forbidden_fields(result)


def test_identity_fields_preserved():
    ctx = _make_context(
        comparison_id="cmp_abc",
        table_id="table_abc",
        temp_table_id="tt_abc",
        slot_number=2,
        carrier_name="Transportadora Beta",
    )
    result = calculate_single_table(ctx)
    assert result["comparison_id"] == "cmp_abc"
    assert result["table_id"] == "table_abc"
    assert result["temp_table_id"] == "tt_abc"
    assert result["slot_number"] == 2
    assert result["carrier_name"] == "Transportadora Beta"


def test_slot_2_ignores_primary_temp_table_id():
    record_a = _pricing_record(weight_30="10,00", weight_50="20,00", excess="1,00", temp_table_id="tt_a")
    record_b = _pricing_record(weight_30="87,13", weight_50="100,50", excess="2,00", temp_table_id="tt_b")
    ctx = _make_context(
        record=record_b,
        temp_table_id="tt_b",
        slot_number=2,
        carrier_name="Beta",
        primary_temp_table_id="tt_a",
        table_id="table_b",
    )
    # primary aponta para A, mas o contexto é B.
    assert ctx.primary_temp_table_id == "tt_a"
    result = calculate_single_table(ctx)
    assert result["temp_table_id"] == "tt_b"
    assert result["slot_number"] == 2
    assert result["results"][0]["calculated_freight"] == 100.50
    assert result["results"][0]["calculated_freight"] != 20.00


def _comparison_state_with_tables(
    *,
    slot1_temp: str = "tt_slot1",
    slot2_temp: str = "tt_slot2",
    slot1_confirmed: bool = True,
    slot2_confirmed: bool = True,
    primary: str | None = "tt_slot1",
) -> dict:
    sess: dict = {}
    state = create_comparison(session_obj=sess)
    t1 = get_table_by_slot(state, 1)
    t2 = get_table_by_slot(state, 2)
    assert t1 and t2
    t1["temp_table_id"] = slot1_temp
    t1["confirmed"] = slot1_confirmed
    t1["status"] = TABLE_STATUS_CONFIRMED if slot1_confirmed else TABLE_STATUS_NEEDS_REVIEW
    t1["carrier_name"] = "Alfa"
    t2["temp_table_id"] = slot2_temp
    t2["confirmed"] = slot2_confirmed
    t2["status"] = TABLE_STATUS_CONFIRMED if slot2_confirmed else TABLE_STATUS_NEEDS_REVIEW
    t2["carrier_name"] = "Beta"
    state["primary_temp_table_id"] = primary
    persist_comparison_state(state, session_obj=sess)
    return state


def test_build_context_rejects_divergent_temp_table_id():
    state = _comparison_state_with_tables()
    table = get_table_by_slot(state, 1)
    with pytest.raises(TableOwnershipError) as exc:
        build_single_table_calculation_context(
            comparison_id=state["comparison_id"],
            table_id=table["table_id"],
            temp_table_id="tt_other",
            slot_number=1,
            carrier_name="Alfa",
            comparison_state=state,
            table_record=_pricing_record(temp_table_id="tt_other"),
            normalized_rows=[_row()],
            coverage_table=_coverage_rows(("SP", "Campinas", "SP-Interior 1")),
        )
    assert exc.value.error_code == ERROR_TEMP_TABLE_MISMATCH


def test_build_context_rejects_table_from_other_comparison():
    state = _comparison_state_with_tables()
    foreign_table_id = uuid4().hex
    with pytest.raises(TableOwnershipError) as exc:
        build_single_table_calculation_context(
            comparison_id=state["comparison_id"],
            table_id=foreign_table_id,
            temp_table_id="tt_x",
            slot_number=1,
            carrier_name="Alfa",
            comparison_state=state,
            table_record=_pricing_record(temp_table_id="tt_x"),
            normalized_rows=[_row()],
        )
    assert exc.value.error_code == ERROR_TABLE_NOT_FOUND


def test_build_context_rejects_divergent_slot():
    state = _comparison_state_with_tables()
    table = get_table_by_slot(state, 1)
    with pytest.raises(TableOwnershipError) as exc:
        build_single_table_calculation_context(
            comparison_id=state["comparison_id"],
            table_id=table["table_id"],
            temp_table_id="tt_slot1",
            slot_number=2,
            carrier_name="Alfa",
            comparison_state=state,
            table_record=_pricing_record(temp_table_id="tt_slot1"),
            normalized_rows=[_row()],
        )
    assert exc.value.error_code == ERROR_SLOT_MISMATCH


def test_build_context_rejects_unconfirmed_table():
    state = _comparison_state_with_tables(slot1_confirmed=False)
    table = get_table_by_slot(state, 1)
    with pytest.raises(InvalidCalculationContextError) as exc:
        build_single_table_calculation_context(
            comparison_id=state["comparison_id"],
            table_id=table["table_id"],
            temp_table_id="tt_slot1",
            slot_number=1,
            carrier_name="Alfa",
            comparison_state=state,
            table_record=_pricing_record(temp_table_id="tt_slot1"),
            normalized_rows=[_row()],
        )
    assert exc.value.error_code == ERROR_TABLE_NOT_CONFIRMED


def test_build_context_rejects_missing_temp_record(monkeypatch):
    state = _comparison_state_with_tables()
    table = get_table_by_slot(state, 1)
    monkeypatch.setattr(
        "app.agente_compara_calculation_service.load_temp_table_record",
        lambda *_args, **_kwargs: None,
    )
    with pytest.raises(InvalidCalculationContextError) as exc:
        build_single_table_calculation_context(
            comparison_id=state["comparison_id"],
            table_id=table["table_id"],
            temp_table_id="tt_slot1",
            slot_number=1,
            carrier_name="Alfa",
            comparison_state=state,
            normalized_rows=[_row()],
            ttl_hours=24,
        )
    assert exc.value.error_code == ERROR_TEMP_TABLE_MISSING
    assert "\\" not in exc.value.message
    assert "/" not in exc.value.message
    assert ".json" not in exc.value.message.lower()

def test_empty_operational_file_raises():
    with pytest.raises(InvalidCalculationContextError) as exc:
        calculate_single_table(_make_context(rows=[]))
    assert exc.value.error_code == ERROR_OPERATIONAL_FILE_EMPTY


def test_valid_row_and_missing_route():
    ctx = _make_context(
        rows=[
            _row(row_index=1, document_number="1"),
            _row(row_index=2, document_number="2", destination_city="Santos", destination_uf="SP"),
        ],
        coverage=_coverage_rows(
            ("SP", "Campinas", "SP-Interior 1"),
            ("SP", "Santos", "Regiao-Inexistente"),
        ),
    )
    result = calculate_single_table(ctx)
    assert result["results"][0]["status"] == STATUS_CALCULATED
    assert result["results"][1]["status"] == STATUS_MISSING_FREIGHT_RULE
    assert result["calculated_count"] == 1
    assert result["error_count"] == 1


def test_missing_coverage_and_ambiguous_coverage():
    coverage = {
        "rows": [
            {"destination_uf": "SP", "destination_city": "Campinas", "freight_region": "SP-Interior 1"},
            {"destination_uf": "SP", "destination_city": "Campinas", "freight_region": "SP-Interior 2"},
        ]
    }
    result = calculate_single_table(
        _make_context(
            rows=[
                _row(row_index=1, destination_city="Campinas"),
                _row(row_index=2, destination_city="Ribeirão Preto"),
            ],
            coverage=coverage,
        )
    )
    assert result["results"][0]["status"] == STATUS_AMBIGUOUS_COVERAGE_MAPPING
    assert result["results"][1]["status"] == STATUS_MISSING_COVERAGE_MAPPING


def test_invalid_weight_and_zero_weight():
    result = calculate_single_table(
        _make_context(
            rows=[
                _row(row_index=1, weight=-5),
                _row(row_index=2, weight=0),
                _row(row_index=3, weight="abc"),
            ]
        )
    )
    assert result["results"][0]["status"] == STATUS_INVALID_WEIGHT
    assert result["results"][1]["status"] == STATUS_CALCULATED
    assert result["results"][1]["calculated_freight"] == 87.13
    assert result["results"][2]["status"] == STATUS_INVALID_WEIGHT


def test_weight_band_limit_and_excess():
    result = calculate_single_table(
        _make_context(
            rows=[
                _row(row_index=1, weight=30),
                _row(row_index=2, weight=50),
                _row(row_index=3, weight=53),
            ]
        )
    )
    assert result["results"][0]["calculated_freight"] == 87.13
    assert result["results"][1]["calculated_freight"] == 100.50
    assert result["results"][2]["calculated_freight"] == 106.50  # 100.50 + 3*2


def test_invoice_value_optional_without_percent_rule():
    result = calculate_single_table(
        _make_context(rows=[_row(invoice_value=None)])
    )
    assert result["results"][0]["status"] == STATUS_CALCULATED
    assert result["results"][0]["calculated_freight"] == 100.50


def test_invoice_value_required_with_percent_rule():
    record = _pricing_record(
        freight_value_header="Frete Valor %",
        freight_value="0,1%",
        weight_50="100,00",
    )
    result = calculate_single_table(
        _make_context(
            record=record,
            rows=[
                _row(row_index=1, invoice_value=None),
                _row(row_index=2, invoice_value=1000.0),
            ],
        )
    )
    assert result["results"][0]["status"] == STATUS_INVALID_INVOICE_VALUE
    assert result["results"][1]["status"] == STATUS_CALCULATED
    assert result["results"][1]["calculated_freight"] == 101.00


def test_accessorial_applied_and_isolated_by_table():
    record_a = _pricing_record(accessorial_fees=[_gris_fee("1,00%")], temp_table_id="tt_a")
    record_b = _pricing_record(accessorial_fees=[], temp_table_id="tt_b")
    result_b = calculate_single_table(
        _make_context(
            record=record_b,
            temp_table_id="tt_b",
            slot_number=2,
            carrier_name="Beta",
            primary_temp_table_id="tt_a",
            rows=[_row(invoice_value=1000.0)],
        )
    )
    assert result_b["results"][0]["status"] == STATUS_CALCULATED
    assert result_b["results"][0]["calculated_freight"] == 100.50
    assert "gris" not in result_b["results"][0]["components"]

    result_a = calculate_single_table(
        _make_context(
            record=record_a,
            temp_table_id="tt_a",
            slot_number=1,
            carrier_name="Alfa",
            rows=[_row(invoice_value=1000.0)],
        )
    )
    assert result_a["results"][0]["calculated_freight"] == 110.50
    assert result_a["results"][0]["components"].get("gris") == 10.0


def test_tax_applied_and_isolated_by_table():
    tax_a = _tax_config(destination_uf="RJ", rate=12.0)
    tax_b = _tax_config(destination_uf="RJ", rate=7.0)
    rows = [_row(destination_city="Niterói", destination_uf="RJ", weight=48)]
    coverage = _coverage_rows(("RJ", "Niterói", "SP-Interior 1"))

    result_b = calculate_single_table(
        _make_context(
            rows=rows,
            coverage=coverage,
            tax_config=tax_b,
            slot_number=2,
            carrier_name="Beta",
            primary_temp_table_id="tt_a",
        )
    )
    freight_b = result_b["results"][0]["calculated_freight"]
    assert result_b["results"][0]["status"] == STATUS_CALCULATED
    assert freight_b == round(100.50 / (1 - 0.07), 2)

    result_a = calculate_single_table(
        _make_context(
            rows=rows,
            coverage=coverage,
            tax_config=tax_a,
            slot_number=1,
            carrier_name="Alfa",
        )
    )
    freight_a = result_a["results"][0]["calculated_freight"]
    assert freight_a == round(100.50 / (1 - 0.12), 2)
    assert freight_a != freight_b


def test_duplicate_documents_preserved_independently():
    result = calculate_single_table(
        _make_context(
            rows=[
                _row(row_index=1, document_number="DUP", weight=20),
                _row(row_index=2, document_number="DUP", weight=48),
            ]
        )
    )
    assert len(result["results"]) == 2
    assert result["results"][0]["document_number"] == "DUP"
    assert result["results"][1]["document_number"] == "DUP"
    assert result["results"][0]["calculated_freight"] == 87.13
    assert result["results"][1]["calculated_freight"] == 100.50


def test_result_order_preserved_by_row_index():
    result = calculate_single_table(
        _make_context(
            rows=[
                _row(row_index=3, document_number="c", weight=20),
                _row(row_index=1, document_number="a", weight=48),
                _row(row_index=2, document_number="b", weight=53),
            ]
        )
    )
    assert [item["row_index"] for item in result["results"]] == [1, 2, 3]
    assert [item["document_number"] for item in result["results"]] == ["a", "b", "c"]


def test_input_not_mutated():
    record = _pricing_record(accessorial_fees=[_gris_fee()])
    rows = [_row(), _row(row_index=2, document_number="2")]
    coverage = _coverage_rows(("SP", "Campinas", "SP-Interior 1"))
    tax = _tax_config(include_taxes=False)
    before_record = copy.deepcopy(record)
    before_rows = copy.deepcopy(rows)
    before_coverage = copy.deepcopy(coverage)
    before_tax = copy.deepcopy(tax)
    ctx = _make_context(record=record, rows=rows, coverage=coverage, tax_config=tax)
    calculate_single_table(ctx)
    assert record == before_record
    assert rows == before_rows
    assert coverage == before_coverage
    assert tax == before_tax
    assert ctx.table_record == before_record
    assert ctx.normalized_rows == before_rows
    assert ctx.coverage_table == before_coverage
    assert ctx.tax_config == before_tax


def test_temp_json_not_rewritten(tmp_path, monkeypatch):
    from tests.cleiton_doc_fixtures import patch_cleiton_doc_store

    patch_cleiton_doc_store(tmp_path, monkeypatch)
    record = _pricing_record(temp_table_id="ttpersist01")
    path = _temp_table_path("ttpersist01")
    _write_temp_table_atomic(path, record)
    mtime_before = path.stat().st_mtime_ns
    content_before = path.read_bytes()
    time.sleep(0.02)
    calculate_single_table(_make_context(record=record, temp_table_id="ttpersist01"))
    assert path.read_bytes() == content_before
    assert path.stat().st_mtime_ns == mtime_before

def test_deterministic_output():
    ctx = _make_context(
        rows=[_row(row_index=1, weight=48), _row(row_index=2, weight=53, document_number="2")]
    )
    first = calculate_single_table(ctx)
    second = calculate_single_table(ctx)
    first.pop("duration_ms", None)
    second.pop("duration_ms", None)
    assert first == second


def test_components_and_evidence_preserved():
    record = _pricing_record(
        freight_value_header="Frete Valor %",
        freight_value="0,1%",
        weight_50="100,00",
        accessorial_fees=[_gris_fee("0,10%")],
    )
    result = calculate_single_table(
        _make_context(
            record=record,
            rows=[_row(destination_city="Niterói", destination_uf="RJ", invoice_value=1000.0)],
            tax_config=_tax_config(destination_uf="RJ", rate=12.0),
            coverage=_coverage_rows(("RJ", "Niterói", "SP-Interior 1")),
        )
    )
    row = result["results"][0]
    assert row["status"] == STATUS_CALCULATED
    assert "weight_freight" in row["components"]
    assert "freight_value_component" in row["components"]
    assert "gris" in row["components"]
    assert "taxes" in row["components"] or "icms" in row["components"]
    assert "total" in row["components"]
    assert row["evidence"].get("freight_region") == "SP-Interior 1"
    assert row["evidence"].get("calculation_basis")
    assert row["evidence"].get("calculation_details")
    memory = row["calculation_memory"]
    assert memory["status"] == "calculated"
    assert memory["total"] == row["calculated_freight"]
    assert memory["table_id"] == result["table_id"]
    assert any(item["code"] == "WEIGHT_FREIGHT" for item in memory["components"])


def test_row_error_does_not_stop_others():
    result = calculate_single_table(
        _make_context(
            rows=[
                _row(row_index=1, weight=-1),
                _row(row_index=2, weight=48),
                _row(row_index=3, destination_city="Desconhecida"),
            ]
        )
    )
    assert result["results"][0]["status"] == STATUS_INVALID_WEIGHT
    assert result["results"][1]["status"] == STATUS_CALCULATED
    assert result["results"][2]["status"] == STATUS_MISSING_COVERAGE_MAPPING
    assert result["calculated_count"] == 1


def test_zero_charged_freight_fields():
    result = calculate_single_table(
        _make_context(
            rows=[
                _row(row_index=1),
                {
                    "row_index": 2,
                    "document_number": "2",
                    "destination_city": "Campinas",
                    "destination_uf": "SP",
                    "audited_weight": 20,
                    # deliberadamente sem valor_frete / charged_freight
                },
            ]
        )
    )
    assert result["calculated_count"] == 2
    _assert_no_forbidden_fields(result)


def test_zero_gemini_and_billing_and_persistence(monkeypatch):
    calls = {"save": 0}

    def boom_save(*_a, **_k):
        calls["save"] += 1
        raise AssertionError("Persistência não deve ocorrer")

    monkeypatch.setattr(
        "app.agente_compara_doc_service.save_temp_table_record",
        boom_save,
        raising=False,
    )
    monkeypatch.setattr(
        "app.agente_compara_calculation_service.load_temp_table_record",
        boom_save,
        raising=False,
    )

    result = calculate_single_table(_make_context())
    assert result["calculated_count"] == 1
    assert calls["save"] == 0


def test_no_status_mutation_on_context_record():
    record = _pricing_record()
    record["audit_batch"] = {"status": "uploaded", "normalized_rows": [_row()]}
    before = copy.deepcopy(record["audit_batch"]["status"])
    calculate_single_table(_make_context(record=record, rows=[_row()]))
    assert record["audit_batch"]["status"] == before
    assert before == "uploaded"


def test_table_isolation_full_scenario():
    record_a = _pricing_record(
        weight_50="20,00",
        excess="1,00",
        accessorial_fees=[_gris_fee("1,00%")],
        temp_table_id="tt_alfa",
    )
    record_b = _pricing_record(
        weight_50="100,50",
        excess="2,00",
        accessorial_fees=[],
        temp_table_id="tt_beta",
    )
    tax_b = _tax_config(rate=7.0, destination_uf="RJ")
    rows = [_row(destination_city="Niterói", destination_uf="RJ", weight=48, invoice_value=1000.0)]
    coverage = _coverage_rows(("RJ", "Niterói", "SP-Interior 1"))

    result = calculate_single_table(
        _make_context(
            record=record_b,
            rows=rows,
            coverage=coverage,
            tax_config=tax_b,
            temp_table_id="tt_beta",
            table_id="table_beta",
            slot_number=2,
            carrier_name="Transportadora Beta",
            primary_temp_table_id="tt_alfa",
        )
    )
    assert result["table_id"] == "table_beta"
    assert result["temp_table_id"] == "tt_beta"
    assert result["carrier_name"] == "Transportadora Beta"
    assert result["slot_number"] == 2
    assert result["results"][0]["calculated_freight"] == round(100.50 / (1 - 0.07), 2)
    assert "gris" not in result["results"][0]["components"]
    blob = json.dumps(result, ensure_ascii=False).lower()
    assert "alfa" not in blob
    assert "tt_alfa" not in blob

def test_build_context_uses_explicit_table_despite_primary(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from tests.cleiton_doc_fixtures import patch_cleiton_doc_store

    patch_cleiton_doc_store(tmp_path, monkeypatch)
    state = _comparison_state_with_tables(
        slot1_temp="tt_primary",
        slot2_temp="tt_secondary",
        primary="tt_primary",
    )
    t2 = get_table_by_slot(state, 2)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    record_b = _pricing_record(temp_table_id="tt_secondary", weight_50="100,50")
    record_b["created_at"] = now.isoformat()
    record_b["expires_at"] = (now + timedelta(hours=24)).isoformat()
    _write_temp_table_atomic(_temp_table_path("tt_secondary"), record_b)

    ctx = build_single_table_calculation_context(
        comparison_id=state["comparison_id"],
        table_id=t2["table_id"],
        temp_table_id="tt_secondary",
        slot_number=2,
        carrier_name="Beta",
        comparison_state=state,
        normalized_rows=[_row()],
        coverage_table=_coverage_rows(("SP", "Campinas", "SP-Interior 1")),
        ttl_hours=24,
    )
    assert ctx.temp_table_id == "tt_secondary"
    assert ctx.slot_number == 2
    assert ctx.primary_temp_table_id == "tt_primary"
    result = calculate_single_table(ctx)
    assert result["temp_table_id"] == "tt_secondary"
    assert result["results"][0]["calculated_freight"] == 100.50


def test_performance_2000_rows_controlled():
    rows = [
        _row(row_index=i + 1, document_number=str(i + 1), weight=48 if i % 2 == 0 else 20)
        for i in range(2000)
    ]
    started = time.perf_counter()
    result = calculate_single_table(_make_context(rows=rows))
    elapsed = time.perf_counter() - started
    assert result["row_count"] == 2000
    assert result["calculated_count"] == 2000
    # Benchmark controlado: não bloqueante; apenas registra teto folgado.
    assert elapsed < 30.0
    assert result["duration_ms"] >= 0


def test_legacy_flow_not_wired_to_new_service():
    routes_src = pathlib.Path("app/agente_compara_api_routes.py").read_text(encoding="utf-8")
    js_src = pathlib.Path("app/static/js/agente_compara.js").read_text(encoding="utf-8")
    # Rotas da Etapa 5 não importam o motor unitário diretamente.
    assert "agente_compara_calculation_service" not in routes_src
    assert "calculate_single_table" not in routes_src
    assert "agente_compara_calculation_execution_service" in routes_src
    assert "comparison/calculate" in routes_src
    assert "calculate_single_table" not in js_src
    assert "agenteComparaProcessCalculationsButton" in js_src
    assert "setProcessCalculationsButtonState" in js_src
    assert "function processComparisonCalculations" in js_src
    process_fn = js_src[
        js_src.index("function processComparisonCalculations") : js_src.index(
            "function clearCalculationFileSummary"
        )
    ]
    assert "API_AUDIT_RUN" not in process_fn
    assert "runAuditProcessing" not in process_fn

    calc_mod = importlib.import_module("app.agente_compara_calculation_service")
    assert "cleide_audit" not in calc_mod.__name__
    source = inspect.getsource(calc_mod)
    assert "import app.cleide" not in source
    assert "from app.cleide" not in source
    assert "apropriar" not in source
    assert "generate_content" not in source
    assert "run_gemini" not in source


def test_record_without_prepared_data_raises():
    record = {
        "temp_table_id": "tt_empty",
        "freight_tables": [],
        "freight_routes": [],
        "accessorial_fees": [],
    }
    with pytest.raises(InvalidCalculationContextError) as exc:
        calculate_single_table(_make_context(record=record, temp_table_id="tt_empty"))
    assert exc.value.error_code == ERROR_TABLE_DATA_MISSING


def test_dead_statuses_removed_from_public_contract():
    import app.agente_compara_calculation_service as calc_mod

    assert not hasattr(calc_mod, "STATUS_INVALID_INPUT")
    assert not hasattr(calc_mod, "STATUS_CALCULATION_ERROR")
    source = inspect.getsource(calc_mod)
    assert 'STATUS_INVALID_INPUT = "invalid_input"' not in source
    assert 'STATUS_CALCULATION_ERROR = "calculation_error"' not in source


def test_unexpected_exception_propagates_as_systemic(monkeypatch):
    import app.agente_compara_calculation_service as calc_mod

    def boom(*_a, **_k):
        raise TypeError("invariante quebrada no núcleo")

    monkeypatch.setattr(calc_mod, "_calculate_expected_freight_row", boom)
    record = _pricing_record()
    before = copy.deepcopy(record)
    with pytest.raises(UnexpectedSingleTableCalculationError) as exc:
        calculate_single_table(_make_context(record=record, rows=[_row()]))
    assert exc.value.error_code == ERROR_UNEXPECTED_CALCULATION
    assert exc.value.exception_type == "TypeError"
    assert exc.value.row_index == 1
    assert isinstance(exc.value.__cause__, TypeError)
    assert record == before


def test_unexpected_exception_on_second_row_fails_whole_batch(monkeypatch):
    import app.agente_compara_calculation_service as calc_mod

    original = calc_mod._calculate_expected_freight_row
    calls = {"n": 0}

    def boom_second(row, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("falha na segunda linha")
        return original(row, **kwargs)

    monkeypatch.setattr(calc_mod, "_calculate_expected_freight_row", boom_second)
    with pytest.raises(UnexpectedSingleTableCalculationError) as exc:
        calculate_single_table(
            _make_context(
                rows=[
                    _row(row_index=1, document_number="1"),
                    _row(row_index=2, document_number="2"),
                ]
            )
        )
    assert exc.value.row_index == 2
    assert isinstance(exc.value.__cause__, RuntimeError)
    assert calls["n"] == 2


def test_domain_errors_do_not_interrupt_other_rows():
    result = calculate_single_table(
        _make_context(
            rows=[
                _row(row_index=1, destination_city="Desconhecida"),
                _row(row_index=2, weight=48),
                _row(
                    row_index=3,
                    destination_city="Santos",
                    destination_uf="SP",
                ),
            ],
            coverage=_coverage_rows(
                ("SP", "Campinas", "SP-Interior 1"),
                ("SP", "Santos", "Regiao-Sem-Regra"),
            ),
        )
    )
    assert result["row_count"] == 3
    assert result["results"][0]["status"] == STATUS_MISSING_COVERAGE_MAPPING
    assert result["results"][1]["status"] == STATUS_CALCULATED
    assert result["results"][2]["status"] == STATUS_MISSING_FREIGHT_RULE
    assert result["calculated_count"] == 1
    assert result["error_count"] == 2
    assert result["summary"]["total_calculated_freight"] == 100.50


def test_comparison_id_not_found():
    with pytest.raises(InvalidCalculationContextError) as exc:
        build_single_table_calculation_context(
            comparison_id="cmp_missing",
            table_id="table_x",
            temp_table_id="tt_x",
            slot_number=1,
            carrier_name="Alfa",
            comparison_state=None,
            table_record=_pricing_record(temp_table_id="tt_x"),
            normalized_rows=[_row()],
        )
    assert exc.value.error_code == ERROR_COMPARISON_NOT_FOUND


def test_comparison_id_divergent_from_state():
    state = _comparison_state_with_tables()
    table = get_table_by_slot(state, 1)
    with pytest.raises(TableOwnershipError) as exc:
        build_single_table_calculation_context(
            comparison_id="cmp_other",
            table_id=table["table_id"],
            temp_table_id="tt_slot1",
            slot_number=1,
            carrier_name="Alfa",
            comparison_state=state,
            table_record=_pricing_record(temp_table_id="tt_slot1"),
            normalized_rows=[_row()],
        )
    assert exc.value.error_code == ERROR_IDENTITY_MISMATCH


def test_component_reconciliation_rich_case(monkeypatch):
    """
    Componentes:
      - weight_freight: base
      - freight_value_component: adicional percentual NF
      - gris / dispatch: generalidades
      - subtotal: agregador pré-imposto
      - taxes/icms: imposto por dentro
      - total / calculated_freight: totalizador final
    """
    from types import SimpleNamespace
    from app.services.agente_compara_config_service import DEFAULT_CALCULATION_BASES

    cfg = SimpleNamespace(calculation_bases=copy.deepcopy(DEFAULT_CALCULATION_BASES), upload_ttl_hours=24)
    monkeypatch.setattr("app.agente_compara_doc_service.get_agente_compara_config", lambda: cfg)

    record = _pricing_record(
        freight_value_header="Frete Valor %",
        freight_value="0,10%",
        accessorial_fees=[
            {
                "name": "GRIS",
                "value": "0,15%",
                "unit": "%",
                "calculation_basis": "sobre nota fiscal",
                "notes": "",
            },
            {
                "name": "Despacho",
                "value": "R$ 12,00",
                "unit": "R$",
                "calculation_basis": "por CTe",
                "notes": "",
            },
        ],
    )
    result = calculate_single_table(
        _make_context(
            record=record,
            rows=[_row(destination_city="Niterói", destination_uf="RJ", weight=53, invoice_value=1000)],
            coverage=_coverage_rows(("RJ", "Niterói", "SP-Interior 1")),
            tax_config=_tax_config(destination_uf="RJ", rate=12.0),
        )
    )
    row = result["results"][0]
    c = row["components"]
    freight_parts = (
        float(c["weight_freight"])
        + float(c["freight_value_component"])
        + float(c["gris"])
        + float(c["dispatch"])
    )
    assert freight_parts == pytest.approx(float(c["subtotal"]), abs=0.0)
    assert float(c["subtotal"]) + float(c["taxes"]) == pytest.approx(float(c["total"]), abs=0.0)
    assert row["calculated_freight"] == pytest.approx(float(c["total"]), abs=0.0)


def test_json_dumps_rich_result_is_serializable():
    record = _pricing_record(
        freight_value_header="Frete Valor %",
        freight_value="0,10%",
        accessorial_fees=[
            {
                "name": "GRIS",
                "value": "0,15%",
                "unit": "%",
                "calculation_basis": "sobre nota fiscal",
                "notes": "",
            }
        ],
    )
    result = calculate_single_table(
        _make_context(
            record=record,
            rows=[
                _row(row_index=1, weight=53, invoice_value=1000, destination_city="Niterói", destination_uf="RJ"),
                _row(row_index=2, weight=-1, document_number="ERR"),
            ],
            coverage=_coverage_rows(("RJ", "Niterói", "SP-Interior 1")),
            tax_config=_tax_config(destination_uf="RJ", rate=12.0),
        )
    )
    payload = json.dumps(result, ensure_ascii=False)
    assert "calculated_freight" in payload
    assert "Decimal" not in payload
    assert "Infinity" not in payload
    assert "NaN" not in payload
    reloaded = json.loads(payload)
    assert reloaded["row_count"] == 2
    assert reloaded["results"][0]["status"] == STATUS_CALCULATED
    assert reloaded["results"][1]["status"] == STATUS_INVALID_WEIGHT


def test_result_order_is_by_row_index_not_input_order():
    """Contrato oficial: saída ordenada por row_index crescente."""
    result = calculate_single_table(
        _make_context(
            rows=[
                _row(row_index=5, document_number="e", weight=20),
                _row(row_index=2, document_number="b", weight=48),
                _row(row_index=4, document_number="d", weight=20),
                _row(row_index=1, document_number="a", weight=48),
            ]
        )
    )
    assert [item["row_index"] for item in result["results"]] == [1, 2, 4, 5]


def _audit_indexes(record=None, coverage=None):
    record = record or _pricing_record()
    coverage = coverage or _coverage_rows(("SP", "Campinas", "SP-Interior 1"))
    return (
        build_coverage_index(coverage),
        build_freight_pricing_index(record),
        True,
        record.get("accessorial_fees") or [],
    )


def test_audit_single_row_invalid_charged_before_calc():
    coverage_index, pricing_index, has_coverage, fees = _audit_indexes()
    row = _row(weight=48)
    result = _audit_single_row(
        row,
        coverage_index=coverage_index,
        pricing_index=pricing_index,
        has_coverage=has_coverage,
        accessorial_fees=fees,
    )
    assert result["status"] == AUDIT_STATUS_INVALID_CHARGED_FREIGHT
    assert result["expected_freight"] is None


def test_audit_single_row_match_over_under_and_tolerance():
    coverage_index, pricing_index, has_coverage, fees = _audit_indexes()

    def run(charged):
        row = _row(weight=48)
        row["charged_freight"] = charged
        return _audit_single_row(
            row,
            coverage_index=coverage_index,
            pricing_index=pricing_index,
            has_coverage=has_coverage,
            accessorial_fees=fees,
        )

    match = run(100.50)
    assert match["status"] == AUDIT_STATUS_OK
    assert match["expected_freight"] == 100.50
    assert match["divergence_value"] == 0

    over = run(101.50)
    assert over["status"] == AUDIT_STATUS_DIVERGENT
    assert over["divergence_value"] == 1.00

    under = run(99.50)
    assert under["status"] == AUDIT_STATUS_DIVERGENT
    assert under["divergence_value"] == -1.00

    almost = run(100.504)
    assert almost["expected_freight"] == 100.50
    assert almost["charged_freight"] == 100.50
    assert almost["status"] == AUDIT_STATUS_OK
    assert almost["divergence_value"] == 0


def test_audit_single_row_preserves_domain_statuses():
    coverage_index, pricing_index, has_coverage, fees = _audit_indexes()
    invalid_weight = _row(weight=-1)
    invalid_weight["charged_freight"] = 10
    result_w = _audit_single_row(
        invalid_weight,
        coverage_index=coverage_index,
        pricing_index=pricing_index,
        has_coverage=has_coverage,
        accessorial_fees=fees,
    )
    assert result_w["status"] == AUDIT_STATUS_INVALID_WEIGHT

    missing = _row(destination_city="Xablau")
    missing["charged_freight"] = 10
    result_c = _audit_single_row(
        missing,
        coverage_index=coverage_index,
        pricing_index=pricing_index,
        has_coverage=has_coverage,
        accessorial_fees=fees,
    )
    assert result_c["status"] == AUDIT_STATUS_MISSING_COVERAGE


def test_rounding_half_up_edge_in_unit_service():
    record = _pricing_record(
        accessorial_fees=[
            {
                "name": "GRIS",
                "value": "0,15%",
                "unit": "%",
                "calculation_basis": "sobre nota fiscal",
                "notes": "",
            }
        ]
    )
    result = calculate_single_table(
        _make_context(record=record, rows=[_row(weight=20, invoice_value=3333)])
    )
    assert result["results"][0]["components"]["gris"] == 5.00
    assert result["results"][0]["calculated_freight"] == 92.13
