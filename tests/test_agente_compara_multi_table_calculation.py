"""Testes do orquestrador multitabela em memória do AgenteCompara (Etapa 4)."""
from __future__ import annotations

import copy
import importlib
import inspect
import json
import pathlib
import time
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.agente_compara_calculation_service import (
    STATUS_CALCULATED,
    STATUS_MISSING_FREIGHT_RULE,
    UnexpectedSingleTableCalculationError,
)
from app.agente_compara_comparison_calculation_service import (
    ERROR_DUPLICATE_SLOT,
    ERROR_DUPLICATE_TABLE_ID,
    ERROR_DUPLICATE_TEMP_TABLE_ID,
    ERROR_OPERATIONAL_FILE,
    ERROR_SERIALIZATION,
    ERROR_TABLE_NOT_CONFIRMED,
    ERROR_TABLE_REQUIRED,
    MULTI_TABLE_CALCULATION_SCHEMA_VERSION,
    AgenteComparaMultiTableCalculationError,
    InvalidMultiTableCalculationContextError,
    MultiTableCalculationInvariantError,
    UnexpectedMultiTableCalculationError,
    build_multi_table_calculation_context,
    calculate_comparison_in_memory,
    consolidate_results_by_row_index,
    validate_comparison_result_serializable,
    validate_single_table_result,
)
from app.agente_compara_comparison_state import (
    TABLE_STATUS_CONFIRMED,
    TABLE_STATUS_NEEDS_REVIEW,
    create_comparison,
    get_table_by_slot,
    persist_comparison_state,
)
from app.services.agente_compara_config_service import DEFAULT_CALCULATION_BASES

FORBIDDEN_COMPARATIVE_FIELDS = {
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
    "savings",
    "economy",
    "ranking",
    "recommendation",
}


@pytest.fixture(autouse=True)
def _patch_calculation_bases(monkeypatch):
    cfg = SimpleNamespace(
        calculation_bases=copy.deepcopy(DEFAULT_CALCULATION_BASES),
        upload_ttl_hours=24,
    )
    monkeypatch.setattr("app.agente_compara_doc_service.get_agente_compara_config", lambda: cfg)


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
    return {
        "status": "needs_review",
        "columns": ["UF destino", "Cidade destino", "Região de frete"],
        "rows": [
            {
                "destination_uf": uf,
                "destination_city": city,
                "freight_region": region,
            }
            for uf, city, region in pairs
        ],
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
    return {
        "name": "GRIS",
        "value": rate_percent,
        "unit": "%",
        "calculation_basis": "sobre nota fiscal",
        "notes": "",
    }


def _comparison_state(
    *,
    slot1_temp: str = "tt_slot1",
    slot2_temp: str = "tt_slot2",
    slot3_temp: str | None = None,
    slot1_confirmed: bool = True,
    slot2_confirmed: bool = True,
    slot3_confirmed: bool | None = None,
    slot1_carrier: str = "Transportadora Alfa",
    slot2_carrier: str = "Transportadora Beta",
    slot3_carrier: str = "Transportadora Gama",
    primary: str | None = "tt_slot1",
    include_slot3_unconfirmed: bool = False,
) -> dict:
    sess: dict = {}
    state = create_comparison(session_obj=sess)
    t1 = get_table_by_slot(state, 1)
    t2 = get_table_by_slot(state, 2)
    assert t1 and t2
    t1["temp_table_id"] = slot1_temp
    t1["confirmed"] = slot1_confirmed
    t1["status"] = TABLE_STATUS_CONFIRMED if slot1_confirmed else TABLE_STATUS_NEEDS_REVIEW
    t1["carrier_name"] = slot1_carrier
    t2["temp_table_id"] = slot2_temp
    t2["confirmed"] = slot2_confirmed
    t2["status"] = TABLE_STATUS_CONFIRMED if slot2_confirmed else TABLE_STATUS_NEEDS_REVIEW
    t2["carrier_name"] = slot2_carrier
    state["primary_temp_table_id"] = primary

    if slot3_temp is not None or include_slot3_unconfirmed:
        confirmed = bool(slot3_confirmed) if slot3_confirmed is not None else False
        if slot3_confirmed is True:
            confirmed = True
        entry = {
            "table_id": uuid4().hex,
            "slot_number": 3,
            "status": TABLE_STATUS_CONFIRMED if confirmed else TABLE_STATUS_NEEDS_REVIEW,
            "doc_ids": [],
            "temp_table_id": slot3_temp or "tt_slot3",
            "carrier_name": slot3_carrier,
            "confirmed": confirmed,
            "error": None,
        }
        state["tables"][entry["table_id"]] = entry
        state["desired_table_count"] = 3

    persist_comparison_state(state, session_obj=sess)
    return state


def _default_records(
    *,
    tt1: str = "tt_slot1",
    tt2: str = "tt_slot2",
    tt3: str | None = None,
    weight_50_a: str = "100,50",
    weight_50_b: str = "120,00",
    weight_50_c: str = "130,00",
    fees_a=None,
    fees_b=None,
    fees_c=None,
) -> dict[str, dict]:
    records = {
        tt1: _pricing_record(temp_table_id=tt1, weight_50=weight_50_a, accessorial_fees=fees_a),
        tt2: _pricing_record(temp_table_id=tt2, weight_50=weight_50_b, accessorial_fees=fees_b),
    }
    if tt3 is not None:
        records[tt3] = _pricing_record(
            temp_table_id=tt3, weight_50=weight_50_c, accessorial_fees=fees_c
        )
    return records


def _build_and_run(
    *,
    state: dict | None = None,
    rows: list[dict] | None = None,
    records: dict[str, dict] | None = None,
    coverage: dict | None = None,
    tax_config: dict | None = None,
    execution_id: str | None = None,
    **state_kwargs,
):
    state = state or _comparison_state(**state_kwargs)
    rows = rows if rows is not None else [_row()]
    records = records or _default_records()
    coverage = coverage if coverage is not None else _coverage_rows(("SP", "Campinas", "SP-Interior 1"))
    ctx = build_multi_table_calculation_context(
        comparison_id=state["comparison_id"],
        comparison_state=state,
        normalized_rows=rows,
        table_records=records,
        coverage_table=coverage,
        tax_config=tax_config,
        execution_id=execution_id,
    )
    result = calculate_comparison_in_memory(ctx)
    return state, ctx, result


def _assert_no_forbidden(payload: dict) -> None:
    blob = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    for key in FORBIDDEN_COMPARATIVE_FIELDS:
        assert f'"{key}"' not in blob


# ---------------------------------------------------------------------------
# Cenários felizes
# ---------------------------------------------------------------------------


def test_two_valid_tables():
    state, _ctx, result = _build_and_run()
    assert result["schema_version"] == MULTI_TABLE_CALCULATION_SCHEMA_VERSION
    assert result["table_count"] == 2
    assert result["row_count"] == 1
    assert len(result["tables"]) == 2
    assert len(result["results_by_table"]) == 2
    assert len(result["comparative_rows"]) == 1
    assert result["summary"]["total_calculation_cells"] == 2
    assert result["summary"]["calculated_cell_count"] == 2
    assert result["summary"]["error_cell_count"] == 0
    _assert_no_forbidden(result)
    json.dumps(result, ensure_ascii=False, allow_nan=False)


def test_three_valid_tables():
    state = _comparison_state(slot3_temp="tt_slot3", slot3_confirmed=True)
    records = _default_records(tt3="tt_slot3")
    _state, _ctx, result = _build_and_run(state=state, records=records)
    assert result["table_count"] == 3
    assert result["summary"]["total_calculation_cells"] == 3
    row = result["comparative_rows"][0]
    assert len(row["table_results"]) == 3


def test_table_3_absent_runs_two():
    _state, ctx, result = _build_and_run()
    assert len(ctx.table_contexts) == 2
    assert result["table_count"] == 2


def test_table_3_present_but_unconfirmed_runs_two(monkeypatch):
    state = _comparison_state(
        slot3_temp="tt_slot3",
        slot3_confirmed=False,
        include_slot3_unconfirmed=True,
    )
    records = _default_records(tt3="tt_slot3")
    calls = {"n": 0}
    import app.agente_compara_comparison_calculation_service as orch

    original = orch.calculate_single_table

    def spy(ctx):
        calls["n"] += 1
        assert ctx.slot_number in (1, 2)
        return original(ctx)

    monkeypatch.setattr(orch, "calculate_single_table", spy)
    ctx = build_multi_table_calculation_context(
        comparison_id=state["comparison_id"],
        comparison_state=state,
        normalized_rows=[_row()],
        table_records=records,
        coverage_table=_coverage_rows(("SP", "Campinas", "SP-Interior 1")),
    )
    result = calculate_comparison_in_memory(ctx)
    assert calls["n"] == 2
    assert result["table_count"] == 2


def test_only_one_confirmed_table_raises():
    state = _comparison_state(slot2_confirmed=False)
    with pytest.raises(InvalidMultiTableCalculationContextError) as exc:
        build_multi_table_calculation_context(
            comparison_id=state["comparison_id"],
            comparison_state=state,
            normalized_rows=[_row()],
            table_records=_default_records(),
            coverage_table=_coverage_rows(("SP", "Campinas", "SP-Interior 1")),
        )
    assert exc.value.error_code == ERROR_TABLE_NOT_CONFIRMED
    assert exc.value.slot_number == 2


def test_table_1_absent_raises():
    state = _comparison_state()
    t1 = get_table_by_slot(state, 1)
    del state["tables"][t1["table_id"]]
    with pytest.raises(InvalidMultiTableCalculationContextError) as exc:
        build_multi_table_calculation_context(
            comparison_id=state["comparison_id"],
            comparison_state=state,
            normalized_rows=[_row()],
            table_records=_default_records(),
            coverage_table=_coverage_rows(("SP", "Campinas", "SP-Interior 1")),
        )
    assert exc.value.error_code == ERROR_TABLE_REQUIRED
    assert exc.value.slot_number == 1


def test_table_2_absent_raises():
    state = _comparison_state()
    t2 = get_table_by_slot(state, 2)
    del state["tables"][t2["table_id"]]
    with pytest.raises(InvalidMultiTableCalculationContextError) as exc:
        build_multi_table_calculation_context(
            comparison_id=state["comparison_id"],
            comparison_state=state,
            normalized_rows=[_row()],
            table_records=_default_records(),
            coverage_table=_coverage_rows(("SP", "Campinas", "SP-Interior 1")),
        )
    assert exc.value.error_code == ERROR_TABLE_REQUIRED
    assert exc.value.slot_number == 2


def test_duplicate_slots_raises():
    state = _comparison_state()
    t1 = get_table_by_slot(state, 1)
    t2 = get_table_by_slot(state, 2)
    t2["slot_number"] = 1
    with pytest.raises(InvalidMultiTableCalculationContextError) as exc:
        build_multi_table_calculation_context(
            comparison_id=state["comparison_id"],
            comparison_state=state,
            normalized_rows=[_row()],
            table_records=_default_records(),
            coverage_table=_coverage_rows(("SP", "Campinas", "SP-Interior 1")),
        )
    assert exc.value.error_code == ERROR_DUPLICATE_SLOT


def test_duplicate_table_ids_raises():
    state = _comparison_state()
    t1 = get_table_by_slot(state, 1)
    t2 = get_table_by_slot(state, 2)
    t2["table_id"] = t1["table_id"]
    # chave do dict também precisa colidir — simula entries com mesmo table_id lógico
    state["tables"] = {
        "a": t1,
        "b": {**t2, "table_id": t1["table_id"]},
    }
    with pytest.raises(InvalidMultiTableCalculationContextError) as exc:
        build_multi_table_calculation_context(
            comparison_id=state["comparison_id"],
            comparison_state=state,
            normalized_rows=[_row()],
            table_records=_default_records(),
            coverage_table=_coverage_rows(("SP", "Campinas", "SP-Interior 1")),
        )
    assert exc.value.error_code == ERROR_DUPLICATE_TABLE_ID


def test_duplicate_temp_table_ids_raises():
    state = _comparison_state(slot1_temp="tt_same", slot2_temp="tt_same")
    records = {
        "tt_same": _pricing_record(temp_table_id="tt_same"),
    }
    with pytest.raises(InvalidMultiTableCalculationContextError) as exc:
        build_multi_table_calculation_context(
            comparison_id=state["comparison_id"],
            comparison_state=state,
            normalized_rows=[_row()],
            table_records=records,
            coverage_table=_coverage_rows(("SP", "Campinas", "SP-Interior 1")),
        )
    assert exc.value.error_code == ERROR_DUPLICATE_TEMP_TABLE_ID


def test_table_id_temp_table_id_divergent_prep_fails(monkeypatch):
    state = _comparison_state()
    t2 = get_table_by_slot(state, 2)
    # Estado diz tt_slot2, mas record/lookup força divergência no builder unitário
    # via temp_table_id informado diferente do entry — alteramos entry.temp vs o que
    # passamos no record lookup key inconsistente: entry aponta tt_slot2, mas
    # adulteramos entry para apontar id A enquanto table_records só tem B com
    # identidade interna divergente.
    t2["temp_table_id"] = "tt_declared"
    records = {
        "tt_slot1": _pricing_record(temp_table_id="tt_slot1"),
        "tt_declared": _pricing_record(temp_table_id="tt_other_identity"),
    }
    calls = {"n": 0}
    import app.agente_compara_comparison_calculation_service as orch

    monkeypatch.setattr(
        orch,
        "calculate_single_table",
        lambda *_a, **_k: calls.__setitem__("n", calls["n"] + 1) or {},
    )
    with pytest.raises(InvalidMultiTableCalculationContextError):
        build_multi_table_calculation_context(
            comparison_id=state["comparison_id"],
            comparison_state=state,
            normalized_rows=[_row()],
            table_records=records,
            coverage_table=_coverage_rows(("SP", "Campinas", "SP-Interior 1")),
        )
    assert calls["n"] == 0


# ---------------------------------------------------------------------------
# Ordem / identidade
# ---------------------------------------------------------------------------


def test_tables_out_of_order_in_state_execute_by_slot(monkeypatch):
    state = _comparison_state()
    t1 = get_table_by_slot(state, 1)
    t2 = get_table_by_slot(state, 2)
    # Reinsere em ordem invertida no dict
    state["tables"] = {t2["table_id"]: t2, t1["table_id"]: t1}
    order = []
    import app.agente_compara_comparison_calculation_service as orch

    original = orch.calculate_single_table

    def spy(ctx):
        order.append((ctx.slot_number, ctx.table_id, ctx.temp_table_id))
        return original(ctx)

    monkeypatch.setattr(orch, "calculate_single_table", spy)
    ctx = build_multi_table_calculation_context(
        comparison_id=state["comparison_id"],
        comparison_state=state,
        normalized_rows=[_row()],
        table_records=_default_records(),
        coverage_table=_coverage_rows(("SP", "Campinas", "SP-Interior 1")),
    )
    calculate_comparison_in_memory(ctx)
    assert [item[0] for item in order] == [1, 2]
    assert order[0][1] == t1["table_id"]
    assert order[1][1] == t2["table_id"]


def test_same_carrier_name_keeps_separate_results():
    state = _comparison_state(slot1_carrier="Mesma", slot2_carrier="Mesma")
    _state, _ctx, result = _build_and_run(state=state)
    t1 = get_table_by_slot(state, 1)
    t2 = get_table_by_slot(state, 2)
    assert t1["table_id"] in result["results_by_table"]
    assert t2["table_id"] in result["results_by_table"]
    assert len(result["results_by_table"]) == 2
    row = result["comparative_rows"][0]
    assert set(row["table_results"].keys()) == {t1["table_id"], t2["table_id"]}
    assert row["table_results"][t1["table_id"]]["carrier_name"] == "Mesma"
    assert row["table_results"][t2["table_id"]]["carrier_name"] == "Mesma"
    # valores distintos por tabela
    assert (
        row["table_results"][t1["table_id"]]["calculated_freight"]
        != row["table_results"][t2["table_id"]]["calculated_freight"]
    )


def test_primary_temp_table_id_irrelevant():
    state = _comparison_state(primary="tt_slot1")
    records = _default_records(weight_50_a="20,00", weight_50_b="100,50")
    _state, _ctx, result = _build_and_run(state=state, records=records)
    t1 = get_table_by_slot(state, 1)
    t2 = get_table_by_slot(state, 2)
    assert result["results_by_table"][t1["table_id"]]["results"][0]["calculated_freight"] == 20.0
    assert result["results_by_table"][t2["table_id"]]["results"][0]["calculated_freight"] == 100.50


def test_builder_prepares_all_contexts_before_calculation(monkeypatch):
    state = _comparison_state()
    build_calls = []
    calc_calls = []
    import app.agente_compara_comparison_calculation_service as orch

    original_build = orch.build_single_table_calculation_context
    original_calc = orch.calculate_single_table

    def build_spy(**kwargs):
        assert calc_calls == [], "cálculo não deve ocorrer durante preparação"
        ctx = original_build(**kwargs)
        build_calls.append(ctx.slot_number)
        return ctx

    def calc_spy(ctx):
        assert len(build_calls) == 2
        calc_calls.append(ctx.slot_number)
        return original_calc(ctx)

    monkeypatch.setattr(orch, "build_single_table_calculation_context", build_spy)
    monkeypatch.setattr(orch, "calculate_single_table", calc_spy)
    ctx = build_multi_table_calculation_context(
        comparison_id=state["comparison_id"],
        comparison_state=state,
        normalized_rows=[_row()],
        table_records=_default_records(),
        coverage_table=_coverage_rows(("SP", "Campinas", "SP-Interior 1")),
    )
    assert build_calls == [1, 2]
    assert calc_calls == []
    calculate_comparison_in_memory(ctx)
    assert calc_calls == [1, 2]


def test_prep_failure_table2_calls_zero_engines(monkeypatch):
    state = _comparison_state()
    t2 = get_table_by_slot(state, 2)
    t2["temp_table_id"] = "tt_declared"
    records = {
        "tt_slot1": _pricing_record(temp_table_id="tt_slot1"),
        "tt_declared": _pricing_record(temp_table_id="tt_mismatch_inside"),
    }
    calc_calls = {"n": 0}
    import app.agente_compara_comparison_calculation_service as orch

    monkeypatch.setattr(
        orch,
        "calculate_single_table",
        lambda *_a, **_k: calc_calls.__setitem__("n", calc_calls["n"] + 1),
    )
    with pytest.raises(InvalidMultiTableCalculationContextError):
        build_multi_table_calculation_context(
            comparison_id=state["comparison_id"],
            comparison_state=state,
            normalized_rows=[_row()],
            table_records=records,
            coverage_table=_coverage_rows(("SP", "Campinas", "SP-Interior 1")),
        )
    assert calc_calls["n"] == 0


def test_prep_failure_table3_invalid_calls_zero_engines(monkeypatch):
    state = _comparison_state(slot3_temp="tt_slot3", slot3_confirmed=True)
    t3 = get_table_by_slot(state, 3)
    t3["temp_table_id"] = "tt_declared3"
    records = _default_records(tt3="tt_declared3")
    records["tt_declared3"] = _pricing_record(temp_table_id="tt_wrong")
    calc_calls = {"n": 0}
    import app.agente_compara_comparison_calculation_service as orch

    monkeypatch.setattr(
        orch,
        "calculate_single_table",
        lambda *_a, **_k: calc_calls.__setitem__("n", calc_calls["n"] + 1),
    )
    with pytest.raises(InvalidMultiTableCalculationContextError):
        build_multi_table_calculation_context(
            comparison_id=state["comparison_id"],
            comparison_state=state,
            normalized_rows=[_row()],
            table_records=records,
            coverage_table=_coverage_rows(("SP", "Campinas", "SP-Interior 1")),
        )
    assert calc_calls["n"] == 0


def test_motor_called_once_per_table_two_and_three(monkeypatch):
    import app.agente_compara_comparison_calculation_service as orch

    original = orch.calculate_single_table

    def run(state, records, expected_n):
        calls = []

        def spy(ctx):
            calls.append((ctx.table_id, ctx.temp_table_id, ctx.slot_number))
            return original(ctx)

        monkeypatch.setattr(orch, "calculate_single_table", spy)
        ctx = build_multi_table_calculation_context(
            comparison_id=state["comparison_id"],
            comparison_state=state,
            normalized_rows=[_row()],
            table_records=records,
            coverage_table=_coverage_rows(("SP", "Campinas", "SP-Interior 1")),
        )
        calculate_comparison_in_memory(ctx)
        assert len(calls) == expected_n
        assert len({c[0] for c in calls}) == expected_n
        assert [c[2] for c in calls] == list(range(1, expected_n + 1))

    run(_comparison_state(), _default_records(), 2)
    run(
        _comparison_state(slot3_temp="tt_slot3", slot3_confirmed=True),
        _default_records(tt3="tt_slot3"),
        3,
    )


# ---------------------------------------------------------------------------
# Consolidação / row_index
# ---------------------------------------------------------------------------


def test_results_separated_by_table_id_and_row_index_consolidation():
    rows = [
        _row(row_index=2, document_number="B"),
        _row(row_index=1, document_number="A"),
    ]
    _state, _ctx, result = _build_and_run(rows=rows)
    assert [r["row_index"] for r in result["comparative_rows"]] == [1, 2]
    assert result["comparative_rows"][0]["document_number"] == "A"
    assert result["comparative_rows"][1]["document_number"] == "B"


def test_duplicate_documents_preserved():
    rows = [
        _row(row_index=1, document_number="DUP"),
        _row(row_index=2, document_number="DUP"),
    ]
    _state, _ctx, result = _build_and_run(rows=rows)
    assert result["row_count"] == 2
    assert [r["document_number"] for r in result["comparative_rows"]] == ["DUP", "DUP"]
    assert [r["row_index"] for r in result["comparative_rows"]] == [1, 2]


def test_normalized_rows_out_of_order_sorted_in_output():
    rows = [
        _row(row_index=10, document_number="10"),
        _row(row_index=3, document_number="3"),
        _row(row_index=7, document_number="7"),
    ]
    _state, _ctx, result = _build_and_run(rows=rows)
    assert [r["row_index"] for r in result["comparative_rows"]] == [3, 7, 10]


def test_duplicate_row_index_in_file_raises():
    state = _comparison_state()
    with pytest.raises(InvalidMultiTableCalculationContextError) as exc:
        build_multi_table_calculation_context(
            comparison_id=state["comparison_id"],
            comparison_state=state,
            normalized_rows=[_row(row_index=1), _row(row_index=1, document_number="x")],
            table_records=_default_records(),
            coverage_table=_coverage_rows(("SP", "Campinas", "SP-Interior 1")),
        )
    assert exc.value.error_code == ERROR_OPERATIONAL_FILE


def test_missing_row_index_in_file_raises():
    state = _comparison_state()
    bad = _row()
    del bad["row_index"]
    with pytest.raises(InvalidMultiTableCalculationContextError) as exc:
        build_multi_table_calculation_context(
            comparison_id=state["comparison_id"],
            comparison_state=state,
            normalized_rows=[bad],
            table_records=_default_records(),
            coverage_table=_coverage_rows(("SP", "Campinas", "SP-Interior 1")),
        )
    assert exc.value.error_code == ERROR_OPERATIONAL_FILE


def _unit_result_shell(ctx, results):
    calculated = sum(1 for r in results if r.get("status") == STATUS_CALCULATED)
    return {
        "schema_version": 1,
        "comparison_id": ctx.comparison_id,
        "table_id": ctx.table_id,
        "temp_table_id": ctx.temp_table_id,
        "slot_number": ctx.slot_number,
        "carrier_name": ctx.carrier_name,
        "row_count": len(results),
        "calculated_count": calculated,
        "error_count": len(results) - calculated,
        "results": results,
        "summary": {
            "total_calculated_freight": 0.0,
            "calculated_count": calculated,
            "error_count": len(results) - calculated,
        },
        "duration_ms": 0,
    }


def test_extra_row_index_in_unit_result(monkeypatch):
    state = _comparison_state()
    import app.agente_compara_comparison_calculation_service as orch

    original = orch.calculate_single_table

    def adulterate(ctx):
        payload = original(ctx)
        if ctx.slot_number == 1:
            extra = copy.deepcopy(payload["results"][0])
            extra["row_index"] = 999
            payload["results"].append(extra)
            payload["row_count"] = len(payload["results"])
            payload["calculated_count"] = len(payload["results"])
            payload["error_count"] = 0
        return payload

    monkeypatch.setattr(orch, "calculate_single_table", adulterate)
    ctx = build_multi_table_calculation_context(
        comparison_id=state["comparison_id"],
        comparison_state=state,
        normalized_rows=[_row()],
        table_records=_default_records(),
        coverage_table=_coverage_rows(("SP", "Campinas", "SP-Interior 1")),
    )
    with pytest.raises(MultiTableCalculationInvariantError):
        calculate_comparison_in_memory(ctx)


def test_missing_row_index_in_unit_result(monkeypatch):
    state = _comparison_state()
    import app.agente_compara_comparison_calculation_service as orch

    original = orch.calculate_single_table

    def adulterate(ctx):
        payload = original(ctx)
        if ctx.slot_number == 2:
            payload["results"] = []
            payload["row_count"] = 0
            payload["calculated_count"] = 0
            payload["error_count"] = 0
        return payload

    monkeypatch.setattr(orch, "calculate_single_table", adulterate)
    ctx = build_multi_table_calculation_context(
        comparison_id=state["comparison_id"],
        comparison_state=state,
        normalized_rows=[_row()],
        table_records=_default_records(),
        coverage_table=_coverage_rows(("SP", "Campinas", "SP-Interior 1")),
    )
    with pytest.raises(MultiTableCalculationInvariantError):
        calculate_comparison_in_memory(ctx)


def test_duplicate_row_index_in_unit_result(monkeypatch):
    state = _comparison_state()
    import app.agente_compara_comparison_calculation_service as orch

    original = orch.calculate_single_table

    def adulterate(ctx):
        payload = original(ctx)
        if ctx.slot_number == 1:
            dup = copy.deepcopy(payload["results"][0])
            payload["results"].append(dup)
            payload["row_count"] = 2
            payload["calculated_count"] = 2
            payload["error_count"] = 0
        return payload

    monkeypatch.setattr(orch, "calculate_single_table", adulterate)
    ctx = build_multi_table_calculation_context(
        comparison_id=state["comparison_id"],
        comparison_state=state,
        normalized_rows=[_row()],
        table_records=_default_records(),
        coverage_table=_coverage_rows(("SP", "Campinas", "SP-Interior 1")),
    )
    with pytest.raises(MultiTableCalculationInvariantError):
        calculate_comparison_in_memory(ctx)


def test_row_count_and_counts_divergent(monkeypatch):
    state = _comparison_state()
    import app.agente_compara_comparison_calculation_service as orch

    original = orch.calculate_single_table

    def adulterate(ctx):
        payload = original(ctx)
        if ctx.slot_number == 1:
            payload["calculated_count"] = 99
            payload["error_count"] = 0
        return payload

    monkeypatch.setattr(orch, "calculate_single_table", adulterate)
    ctx = build_multi_table_calculation_context(
        comparison_id=state["comparison_id"],
        comparison_state=state,
        normalized_rows=[_row()],
        table_records=_default_records(),
        coverage_table=_coverage_rows(("SP", "Campinas", "SP-Interior 1")),
    )
    with pytest.raises(MultiTableCalculationInvariantError):
        calculate_comparison_in_memory(ctx)


def test_identity_and_schema_divergent_in_unit_result(monkeypatch):
    state = _comparison_state()
    import app.agente_compara_comparison_calculation_service as orch

    original = orch.calculate_single_table

    def bad_identity(ctx):
        payload = original(ctx)
        if ctx.slot_number == 1:
            payload["table_id"] = "outra"
        return payload

    monkeypatch.setattr(orch, "calculate_single_table", bad_identity)
    ctx = build_multi_table_calculation_context(
        comparison_id=state["comparison_id"],
        comparison_state=state,
        normalized_rows=[_row()],
        table_records=_default_records(),
        coverage_table=_coverage_rows(("SP", "Campinas", "SP-Interior 1")),
    )
    with pytest.raises(MultiTableCalculationInvariantError):
        calculate_comparison_in_memory(ctx)

    def bad_schema(ctx):
        payload = original(ctx)
        payload["schema_version"] = 99
        return payload

    monkeypatch.setattr(orch, "calculate_single_table", bad_schema)
    ctx2 = build_multi_table_calculation_context(
        comparison_id=state["comparison_id"],
        comparison_state=state,
        normalized_rows=[_row()],
        table_records=_default_records(),
        coverage_table=_coverage_rows(("SP", "Campinas", "SP-Interior 1")),
    )
    with pytest.raises(MultiTableCalculationInvariantError):
        calculate_comparison_in_memory(ctx2)


# ---------------------------------------------------------------------------
# Domínio vs sistêmico
# ---------------------------------------------------------------------------


def test_domain_error_in_one_table_preserves_others(monkeypatch):
    state = _comparison_state()
    import app.agente_compara_comparison_calculation_service as orch

    original = orch.calculate_single_table

    def spy(ctx):
        payload = original(ctx)
        if ctx.slot_number == 2:
            for item in payload["results"]:
                if item["row_index"] == 1:
                    item["status"] = STATUS_MISSING_FREIGHT_RULE
                    item["calculated_freight"] = None
                    item["error"] = {
                        "code": STATUS_MISSING_FREIGHT_RULE,
                        "message": "Regra ausente",
                    }
                    item["components"] = {}
            payload["calculated_count"] = sum(
                1 for r in payload["results"] if r["status"] == STATUS_CALCULATED
            )
            payload["error_count"] = payload["row_count"] - payload["calculated_count"]
            payload["summary"]["calculated_count"] = payload["calculated_count"]
            payload["summary"]["error_count"] = payload["error_count"]
        return payload

    monkeypatch.setattr(orch, "calculate_single_table", spy)
    ctx = build_multi_table_calculation_context(
        comparison_id=state["comparison_id"],
        comparison_state=state,
        normalized_rows=[
            _row(row_index=1),
            _row(row_index=2, document_number="2"),
        ],
        table_records=_default_records(),
        coverage_table=_coverage_rows(("SP", "Campinas", "SP-Interior 1")),
    )
    result = calculate_comparison_in_memory(ctx)

    assert result["summary"]["total_calculation_cells"] == 4
    assert result["summary"]["error_cell_count"] == 1
    assert result["summary"]["calculated_cell_count"] == 3
    t1 = get_table_by_slot(state, 1)
    t2 = get_table_by_slot(state, 2)
    row1 = result["comparative_rows"][0]
    assert row1["table_results"][t1["table_id"]]["status"] == STATUS_CALCULATED
    assert row1["table_results"][t2["table_id"]]["status"] == STATUS_MISSING_FREIGHT_RULE
    assert row1["table_results"][t1["table_id"]]["calculated_freight"] is not None


def test_multiple_domain_errors():
    rows = [
        _row(row_index=1, weight=-1),
        _row(row_index=2, destination_city="Santos"),
    ]
    coverage = _coverage_rows(
        ("SP", "Campinas", "SP-Interior 1"),
        ("SP", "Santos", "Regiao-X"),
    )
    _state, _ctx, result = _build_and_run(rows=rows, coverage=coverage)
    assert result["summary"]["error_cell_count"] == 4  # 2 rows × 2 tables
    assert result["summary"]["calculated_cell_count"] == 0
    assert result["table_count"] == 2


def test_systemic_failure_table1(monkeypatch):
    state = _comparison_state()
    import app.agente_compara_comparison_calculation_service as orch

    def boom(ctx):
        raise UnexpectedSingleTableCalculationError(
            "falha",
            comparison_id=ctx.comparison_id,
            table_id=ctx.table_id,
            slot_number=ctx.slot_number,
            exception_type="Boom",
        )

    monkeypatch.setattr(orch, "calculate_single_table", boom)
    ctx = build_multi_table_calculation_context(
        comparison_id=state["comparison_id"],
        comparison_state=state,
        normalized_rows=[_row()],
        table_records=_default_records(),
        coverage_table=_coverage_rows(("SP", "Campinas", "SP-Interior 1")),
    )
    with pytest.raises(UnexpectedMultiTableCalculationError) as exc:
        calculate_comparison_in_memory(ctx)
    assert exc.value.slot_number == 1
    assert isinstance(exc.value.__cause__, UnexpectedSingleTableCalculationError)


def test_systemic_failure_table2_skips_table3_no_partial(monkeypatch):
    state = _comparison_state(slot3_temp="tt_slot3", slot3_confirmed=True)
    records = _default_records(tt3="tt_slot3")
    import app.agente_compara_comparison_calculation_service as orch

    original = orch.calculate_single_table
    calls = []

    def spy(ctx):
        calls.append(ctx.slot_number)
        if ctx.slot_number == 2:
            raise UnexpectedSingleTableCalculationError(
                "falha t2",
                comparison_id=ctx.comparison_id,
                table_id=ctx.table_id,
                slot_number=2,
                exception_type="Boom",
            )
        return original(ctx)

    monkeypatch.setattr(orch, "calculate_single_table", spy)
    ctx = build_multi_table_calculation_context(
        comparison_id=state["comparison_id"],
        comparison_state=state,
        normalized_rows=[_row()],
        table_records=records,
        coverage_table=_coverage_rows(("SP", "Campinas", "SP-Interior 1")),
    )
    with pytest.raises(UnexpectedMultiTableCalculationError) as exc:
        calculate_comparison_in_memory(ctx)
    assert calls == [1, 2]
    assert 3 not in calls
    assert exc.value.slot_number == 2
    assert isinstance(exc.value.__cause__, UnexpectedSingleTableCalculationError)
    assert exc.value.__cause__ is not None


# ---------------------------------------------------------------------------
# Contrato rico / summary / componentes
# ---------------------------------------------------------------------------


def test_full_contract_rich_scenario():
    state = _comparison_state()
    t1 = get_table_by_slot(state, 1)
    t2 = get_table_by_slot(state, 2)
    records = {
        "tt_slot1": _pricing_record(
            temp_table_id="tt_slot1",
            weight_50="100,50",
            accessorial_fees=[_gris_fee("0,15%")],
            freight_value_header="Frete Valor %",
            freight_value="1,00",
        ),
        "tt_slot2": _pricing_record(
            temp_table_id="tt_slot2",
            weight_50="135,20",
            accessorial_fees=[_gris_fee("0,20%")],
        ),
    }
    rows = [
        _row(row_index=1, document_number="7400455", invoice_value=1000.0),
        _row(row_index=2, document_number="7400455", destination_city="Santos", invoice_value=500.0),
        _row(row_index=3, document_number="7400999", weight=20.0, invoice_value=800.0),
    ]
    coverage = _coverage_rows(
        ("SP", "Campinas", "SP-Interior 1"),
        ("SP", "Santos", "Regiao-Inexistente"),
    )
    tax = _tax_config(
        include_taxes=True,
        destination_uf="SP",
        rate=12.0,
        selected_table_ids=[t1["table_id"]],
    )
    # Ajuste destino das rows para RJ? Coverage é SP. Use tax destination SP.
    _state, _ctx, result = _build_and_run(
        state=state,
        rows=rows,
        records=records,
        coverage=coverage,
        tax_config=tax,
        execution_id="exec_test",
    )
    assert result["execution_id"] == "exec_test"
    assert result["table_count"] == 2
    assert result["row_count"] == 3
    assert set(result["results_by_table"].keys()) == {t1["table_id"], t2["table_id"]}
    assert result["summary"]["total_calculation_cells"] == 6
    # row2 missing rule in both tables (Regiao-Inexistente)
    assert result["summary"]["error_cell_count"] >= 2
    for crow in result["comparative_rows"]:
        assert set(crow["table_results"].keys()) == {t1["table_id"], t2["table_id"]}
    # componentes/evidências na linha calculada
    row1 = result["comparative_rows"][0]
    cell1 = row1["table_results"][t1["table_id"]]
    assert cell1["status"] == STATUS_CALCULATED
    assert isinstance(cell1["components"], dict)
    assert cell1["components"]
    assert isinstance(cell1["evidence"], dict)
    _assert_no_forbidden(result)
    json.dumps(result, ensure_ascii=False, allow_nan=False)


def test_summary_math_closes():
    _state, _ctx, result = _build_and_run(rows=[_row(row_index=1), _row(row_index=2)])
    s = result["summary"]
    assert s["total_calculation_cells"] == s["row_count"] * s["table_count"]
    assert s["calculated_cell_count"] + s["error_cell_count"] == s["total_calculation_cells"]


def test_components_and_evidence_preserved():
    records = {
        "tt_slot1": _pricing_record(
            temp_table_id="tt_slot1",
            accessorial_fees=[_gris_fee("1,00%")],
        ),
        "tt_slot2": _pricing_record(temp_table_id="tt_slot2", weight_50="120,00"),
    }
    state, _ctx, result = _build_and_run(records=records)
    t1 = get_table_by_slot(state, 1)
    unit = result["results_by_table"][t1["table_id"]]["results"][0]
    cell = result["comparative_rows"][0]["table_results"][t1["table_id"]]
    assert cell["components"] == unit["components"]
    assert cell["evidence"] == unit["evidence"]


# ---------------------------------------------------------------------------
# Determinismo / não mutação / serialização
# ---------------------------------------------------------------------------


def test_deterministic_two_runs():
    state = _comparison_state()
    rows = [_row(row_index=1), _row(row_index=2, document_number="2")]
    records = _default_records()
    coverage = _coverage_rows(("SP", "Campinas", "SP-Interior 1"))

    def run_once():
        ctx = build_multi_table_calculation_context(
            comparison_id=state["comparison_id"],
            comparison_state=state,
            normalized_rows=copy.deepcopy(rows),
            table_records=copy.deepcopy(records),
            coverage_table=copy.deepcopy(coverage),
            execution_id="exec_fixed",
        )
        result = calculate_comparison_in_memory(ctx)
        result = copy.deepcopy(result)
        result.pop("duration_ms", None)
        for entry in result["results_by_table"].values():
            entry.pop("duration_ms", None)
        return result

    a = run_once()
    b = run_once()
    assert a == b


def test_no_mutation_of_inputs():
    state = _comparison_state()
    rows = [_row()]
    records = _default_records()
    coverage = _coverage_rows(("SP", "Campinas", "SP-Interior 1"))
    tax = _tax_config(include_taxes=False)
    before_state = copy.deepcopy(state)
    before_rows = copy.deepcopy(rows)
    before_records = copy.deepcopy(records)
    before_coverage = copy.deepcopy(coverage)
    before_tax = copy.deepcopy(tax)
    ctx = build_multi_table_calculation_context(
        comparison_id=state["comparison_id"],
        comparison_state=state,
        normalized_rows=rows,
        table_records=records,
        coverage_table=coverage,
        tax_config=tax,
    )
    before_ctx_rows = copy.deepcopy(list(ctx.normalized_rows))
    result = calculate_comparison_in_memory(ctx)
    assert state == before_state
    assert rows == before_rows
    assert records == before_records
    assert coverage == before_coverage
    assert tax == before_tax
    assert list(ctx.normalized_rows) == before_ctx_rows
    assert "comparative_rows" not in (records["tt_slot1"].get("audit_batch") or {})
    assert result["row_count"] == 1


def test_nan_inf_decimal_set_rejected(monkeypatch):
    state = _comparison_state()
    import app.agente_compara_comparison_calculation_service as orch

    original = orch.calculate_single_table

    def with_bad(bad_value):
        def adulterate(ctx):
            payload = original(ctx)
            if ctx.slot_number == 1:
                payload["results"][0]["calculated_freight"] = bad_value
            return payload

        return adulterate

    for bad in (float("nan"), float("inf"), float("-inf")):
        monkeypatch.setattr(orch, "calculate_single_table", with_bad(bad))
        ctx = build_multi_table_calculation_context(
            comparison_id=state["comparison_id"],
            comparison_state=state,
            normalized_rows=[_row()],
            table_records=_default_records(),
            coverage_table=_coverage_rows(("SP", "Campinas", "SP-Interior 1")),
        )
        with pytest.raises(MultiTableCalculationInvariantError) as exc:
            calculate_comparison_in_memory(ctx)
        assert exc.value.error_code == ERROR_SERIALIZATION

    monkeypatch.setattr(orch, "calculate_single_table", with_bad(Decimal("10.5")))
    ctx = build_multi_table_calculation_context(
        comparison_id=state["comparison_id"],
        comparison_state=state,
        normalized_rows=[_row()],
        table_records=_default_records(),
        coverage_table=_coverage_rows(("SP", "Campinas", "SP-Interior 1")),
    )
    with pytest.raises(MultiTableCalculationInvariantError):
        calculate_comparison_in_memory(ctx)

    def with_set(ctx):
        payload = original(ctx)
        if ctx.slot_number == 1:
            payload["results"][0]["components"] = {"x": {1, 2}}
        return payload

    monkeypatch.setattr(orch, "calculate_single_table", with_set)
    ctx = build_multi_table_calculation_context(
        comparison_id=state["comparison_id"],
        comparison_state=state,
        normalized_rows=[_row()],
        table_records=_default_records(),
        coverage_table=_coverage_rows(("SP", "Campinas", "SP-Interior 1")),
    )
    with pytest.raises(MultiTableCalculationInvariantError):
        calculate_comparison_in_memory(ctx)


def test_validate_comparison_result_rejects_forbidden_field():
    payload = {
        "schema_version": 1,
        "comparison_id": "c",
        "winner": "x",
    }
    with pytest.raises(MultiTableCalculationInvariantError):
        validate_comparison_result_serializable(payload, comparison_id="c")


# ---------------------------------------------------------------------------
# Wiring / persistência / gemini / billing
# ---------------------------------------------------------------------------


def test_zero_wiring_zero_gemini_zero_billing():
    """Etapa 4: orquestrador permanece sem Gemini/billing; wiring HTTP ficou na Etapa 5."""
    routes_src = pathlib.Path("app/agente_compara_api_routes.py").read_text(encoding="utf-8")
    js_src = pathlib.Path("app/static/js/agente_compara.js").read_text(encoding="utf-8")
    state_src = pathlib.Path("app/agente_compara_comparison_state.py").read_text(encoding="utf-8")

    # Orquestrador não é importado no comparison_state (persistência só guarda payload).
    assert "calculate_comparison_in_memory" not in state_src
    assert "build_multi_table_calculation_context" not in state_src

    # Etapa 5 conecta via execution service — rotas não importam o motor unitário.
    assert "agente_compara_calculation_service" not in routes_src
    assert "calculate_single_table" not in routes_src
    assert "agente_compara_calculation_execution_service" in routes_src
    assert "comparison/calculate" in routes_src
    assert "comparison/calculate" in js_src

    assert "agenteComparaProcessCalculationsButton" in js_src
    assert "function processComparisonCalculations" in js_src

    orch = importlib.import_module("app.agente_compara_comparison_calculation_service")
    source = inspect.getsource(orch)
    assert "import app.cleide" not in source
    assert "from app.cleide" not in source
    assert "generate_content" not in source
    assert "run_gemini" not in source
    assert "apropriar" not in source
    assert "stripe" not in source.lower()
    assert "apropriar_franquia" not in source
    assert "flow_type" not in source
    assert "billing_service" not in source
    assert "from app.services.billing" not in source


def test_zero_persistence(tmp_path, monkeypatch):
    from app.agente_compara_doc_service import _write_temp_table_atomic, _temp_table_path

    # Isola store se fixture de projeto existir; senão usa records em memória.
    state = _comparison_state()
    records = _default_records()
    records["tt_slot1"]["audit_batch"] = {"normalized_rows": [_row()], "status": "ready"}
    before_records = copy.deepcopy(records)
    before_state = copy.deepcopy(state)
    ctx = build_multi_table_calculation_context(
        comparison_id=state["comparison_id"],
        comparison_state=state,
        normalized_rows=[_row()],
        table_records=records,
        coverage_table=_coverage_rows(("SP", "Campinas", "SP-Interior 1")),
    )
    result = calculate_comparison_in_memory(ctx)
    assert state == before_state
    assert records == before_records
    assert "results_by_table" not in state
    assert "comparative_rows" not in state
    assert result["comparative_rows"]


def test_button_process_calculations_is_wired_in_frontend():
    """Etapa 5: botão possui listener dedicado; orquestrador Etapa 4 permanece intacto."""
    js_src = pathlib.Path("app/static/js/agente_compara.js").read_text(encoding="utf-8")
    assert "agenteComparaProcessCalculationsButton" in js_src
    assert "setProcessCalculationsButtonState" in js_src
    assert "function processComparisonCalculations" in js_src
    assert "bindProcessCalculationsButton" in js_src
    assert "API_AUDIT_RUN" in js_src  # legado permanece
    process_fn = js_src[
        js_src.index("function processComparisonCalculations") : js_src.index(
            "function clearCalculationFileSummary"
        )
    ]
    assert "API_AUDIT_RUN" not in process_fn
    assert "runAuditProcessing" not in process_fn


# ---------------------------------------------------------------------------
# Benchmark
# ---------------------------------------------------------------------------


def test_benchmark_2_tables_2000_rows():
    rows = [
        _row(row_index=i + 1, document_number=str(i + 1), weight=48 if i % 2 == 0 else 20)
        for i in range(2000)
    ]
    started = time.perf_counter()
    _state, _ctx, result = _build_and_run(rows=rows)
    elapsed = time.perf_counter() - started
    assert result["row_count"] == 2000
    assert result["table_count"] == 2
    assert result["summary"]["total_calculation_cells"] == 4000
    assert elapsed < 90.0
    assert result["duration_ms"] >= 0


def test_benchmark_3_tables_2000_rows():
    state = _comparison_state(slot3_temp="tt_slot3", slot3_confirmed=True)
    records = _default_records(tt3="tt_slot3")
    rows = [
        _row(row_index=i + 1, document_number=str(i + 1), weight=48 if i % 2 == 0 else 20)
        for i in range(2000)
    ]
    started = time.perf_counter()
    _state, _ctx, result = _build_and_run(state=state, records=records, rows=rows)
    elapsed = time.perf_counter() - started
    assert result["table_count"] == 3
    assert result["summary"]["total_calculation_cells"] == 6000
    assert elapsed < 120.0


def test_metadata_comes_from_operational_file_not_first_carrier(monkeypatch):
    state = _comparison_state()
    rows = [_row(row_index=1, document_number="DOC-COMUM", destination_city="Campinas")]
    import app.agente_compara_comparison_calculation_service as orch

    original = orch.calculate_single_table

    def spy(ctx):
        payload = original(ctx)
        if ctx.slot_number == 1:
            for item in payload["results"]:
                item["document_number"] = "DOC-TABELA-1"
                item["destination_city"] = "Cidade-Errada"
        return payload

    monkeypatch.setattr(orch, "calculate_single_table", spy)
    ctx = build_multi_table_calculation_context(
        comparison_id=state["comparison_id"],
        comparison_state=state,
        normalized_rows=rows,
        table_records=_default_records(),
        coverage_table=_coverage_rows(("SP", "Campinas", "SP-Interior 1")),
    )
    result = calculate_comparison_in_memory(ctx)
    assert result["comparative_rows"][0]["document_number"] == "DOC-COMUM"
    assert result["comparative_rows"][0]["destination_city"] == "Campinas"


def test_unsupported_schema_version():
    state = _comparison_state()
    with pytest.raises(InvalidMultiTableCalculationContextError) as exc:
        build_multi_table_calculation_context(
            comparison_id=state["comparison_id"],
            comparison_state=state,
            normalized_rows=[_row()],
            table_records=_default_records(),
            coverage_table=_coverage_rows(("SP", "Campinas", "SP-Interior 1")),
            schema_version=99,
        )
    assert "schema" in exc.value.message.lower() or exc.value.error_code.endswith("schema_unsupported")
