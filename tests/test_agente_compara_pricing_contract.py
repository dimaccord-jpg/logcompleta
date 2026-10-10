"""Contrato tarifário canônico v1 do AgenteCompara (SCRUM-146 lotes 2 e 3.1)."""
from __future__ import annotations

import copy

import pytest

from app.agente_compara_calculation_service import (
    STATUS_MISSING_FREIGHT_RULE,
    calculate_single_table,
)
from app.agente_compara_doc_service import (
    AUDIT_STATUS_UNSUPPORTED_PRICING,
    _build_freight_pricing_index_legacy,
    _compile_pricing_contract,
    _find_pricing_rule_match,
    _is_excess_column,
    _is_region_column,
    _parse_range_from_label,
    _serialize_pricing_rule_for_fingerprint,
    build_freight_pricing_index,
    build_pricing_contract,
    calculate_weight_freight,
    pricing_source_fingerprint,
)
from app.agente_compara_temp_table_validation_service import (
    CODE_CONFLICTING_LOOKUP_KEY,
    CODE_INVALID_RANGE,
    CODE_INVALID_TARIFF,
    CODE_MISSING_LOOKUP_KEY,
    CODE_NO_EXECUTABLE_PRICING_RULE,
    CODE_READING_ALERT,
    CODE_UNRESOLVED_PRICING_TYPE,
    contract_preserves_origin_distinction,
    pricing_contract_structure_is_usable,
    validate_pricing_contract_for_confirmation,
    validate_temp_table_for_confirmation,
)
from tests.test_agente_compara_single_table_calculation import (
    _coverage_rows,
    _make_context,
    _pricing_record,
    _row,
)


def _fixed_range_table() -> dict:
    return {
        "freight_tables": [
            {
                "table_title": "Tabela fixa",
                "columns": ["Região", "Até 30 kg", "31 a 50 kg"],
                "rows": [
                    {
                        "Região": "Sul",
                        "Até 30 kg": "87,13",
                        "31 a 50 kg": "100,50",
                    }
                ],
                "confidence": "low",
                "notes": "",
                "context": {"delivery_deadline": None, "route_label": None},
            }
        ],
        "freight_routes": [],
        "reading_alerts": ["Título incompleto na página 2"],
        "uncertain_fields": ["prazo"],
    }


def _direct_table(*, unit_header: str, value: str) -> dict:
    return {
        "freight_tables": [
            {
                "table_title": "Tarifa direta",
                "columns": ["Região", unit_header],
                "rows": [{"Região": "Sul", unit_header: value}],
            }
        ],
        "freight_routes": [],
    }


def _excess_table() -> dict:
    return {
        "freight_tables": [
            {
                "table_title": "Tabela com excedente",
                "columns": ["Região", "Até 50 kg", "Excedente kg", "Frete Valor %", "Pedágio"],
                "rows": [
                    {
                        "Região": "Sul",
                        "Até 50 kg": "100,00",
                        "Excedente kg": "2,00",
                        "Frete Valor %": "0,35",
                        "Pedágio": "5,25",
                    }
                ],
            }
        ],
        "freight_routes": [],
    }


def _with_contract(table: dict) -> dict:
    record = copy.deepcopy(table)
    record["pricing_contract"] = build_pricing_contract(record)
    return record


def _assert_contract_calculates(table: dict, weight: float, expected: float, basis: str, monkeypatch):
    record = _with_contract(table)
    validation = validate_pricing_contract_for_confirmation(table, record["pricing_contract"])
    assert validation["can_confirm"] is True, validation["blocking_issues"]

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("compilador tarifário legado não deve rodar")

    monkeypatch.setattr(
        "app.agente_compara_doc_service._build_freight_pricing_index_legacy",
        _forbidden,
    )
    monkeypatch.setattr(
        "app.agente_compara_doc_service._build_rule_from_row_range_table",
        _forbidden,
    )
    monkeypatch.setattr(
        "app.agente_compara_doc_service._build_rules_from_matrix_table",
        _forbidden,
    )
    monkeypatch.setattr(
        "app.agente_compara_doc_service._build_rule_from_freight_route",
        _forbidden,
    )
    index = build_freight_pricing_index(record)
    assert "Sul" in index
    assert index["Sul"]["pricing_type"] == basis
    calculated = calculate_weight_freight(weight, index["Sul"])
    assert calculated is not None
    assert calculated["calculation_basis"] == basis
    assert calculated["expected_freight"] == expected
    assert index.get("Norte") is None
    return record, index


def test_fixed_range_contract_calculates_without_legacy_compiler(monkeypatch):
    record, index = _assert_contract_calculates(_fixed_range_table(), 20, 87.13, "fixed_range", monkeypatch)
    contract = record["pricing_contract"]
    assert contract["schema_version"] == 1
    assert contract["source_fingerprint"] == pricing_source_fingerprint(record)
    assert contract["rules"][0]["pricing_type"] == "fixed_range"
    assert contract["rules"][0]["lookup_keys"] == ["Sul"]
    assert contract["rules"][0]["excess"] is None
    assert "coverage_table" not in contract
    assert "tax_config" not in contract
    assert "accessorial_fees" not in contract
    assert index["Sul"]["pricing_type"] == "fixed_range"


def test_fixed_range_matches_legacy_math():
    table = _fixed_range_table()
    legacy = _build_freight_pricing_index_legacy(table)
    index = build_freight_pricing_index(_with_contract(table))
    for weight in (0, 10, 30, 31, 50, 51):
        assert calculate_weight_freight(weight, index["Sul"]) == calculate_weight_freight(weight, legacy["Sul"])


def test_direct_weight_rate_kg_contract(monkeypatch):
    table = _direct_table(unit_header="Frete kg", value="1,50")
    record, _index = _assert_contract_calculates(table, 10, 15.0, "direct_weight_rate", monkeypatch)
    rule = record["pricing_contract"]["rules"][0]
    assert rule["unit"] == "kg"
    assert rule["value_per_kg"] == 1.5


def test_direct_weight_rate_ton_contract(monkeypatch):
    table = _direct_table(unit_header="Valor tonelada", value="250,00")
    record, _index = _assert_contract_calculates(table, 2000, 500.0, "direct_weight_rate", monkeypatch)
    rule = record["pricing_contract"]["rules"][0]
    assert rule["unit"] == "ton"
    assert rule["value_per_ton"] == 250.0


def test_range_plus_excess_contract(monkeypatch):
    table = _excess_table()
    legacy = _build_freight_pricing_index_legacy(table)["Sul"]
    record, _index = _assert_contract_calculates(table, 60, 120.0, "range_plus_excess_per_kg", monkeypatch)
    rule = record["pricing_contract"]["rules"][0]
    assert rule["excess"]["rate_per_kg"] == 2.0
    assert rule["freight_value"]["rate"] == pytest.approx(0.0035)
    assert rule["route_toll"]["rate_per_fraction"] == pytest.approx(5.25)
    assert rule["route_toll"]["fraction_size_kg"] == 100.0
    assert _serialize_pricing_rule_for_fingerprint(rule) == _serialize_pricing_rule_for_fingerprint(legacy)


def test_record_without_contract_keeps_legacy_index(monkeypatch):
    table = _fixed_range_table()
    sentinel = {"legacy-only": {"pricing_type": "fixed_range", "brackets": [], "unit": "kg"}}
    monkeypatch.setattr(
        "app.agente_compara_doc_service._build_freight_pricing_index_legacy",
        lambda _table: sentinel,
    )
    assert build_freight_pricing_index(table) is sentinel
    unpatched = _build_freight_pricing_index_legacy(table)
    assert calculate_weight_freight(20, unpatched["Sul"])["expected_freight"] == 87.13


def test_stale_contract_falls_back_to_legacy(monkeypatch):
    table = _with_contract(_fixed_range_table())
    table["freight_tables"][0]["rows"][0]["Até 30 kg"] = "10,00"
    assert table["pricing_contract"]["source_fingerprint"] != pricing_source_fingerprint(table)
    sentinel = {"stale-fallback": True}
    monkeypatch.setattr(
        "app.agente_compara_doc_service._build_freight_pricing_index_legacy",
        lambda _table: sentinel,
    )
    assert build_freight_pricing_index(table) is sentinel
    fresh_legacy = _build_freight_pricing_index_legacy(
        {
            "freight_tables": table["freight_tables"],
            "freight_routes": [],
        }
    )
    assert calculate_weight_freight(20, fresh_legacy["Sul"])["expected_freight"] == 10.0


def test_tariff_edit_invalidates_fingerprint_but_edit_version_does_not():
    table = _fixed_range_table()
    original = pricing_source_fingerprint(table)
    contract = build_pricing_contract(table)
    assert contract["source_fingerprint"] == original
    edited = copy.deepcopy(table)
    edited["edit_version"] = 99
    assert pricing_source_fingerprint(edited) == original
    edited["freight_tables"][0]["rows"][0]["Até 30 kg"] = "1,00"
    assert pricing_source_fingerprint(edited) != original
    edited["freight_tables"][0]["_selected_pricing_dimension_column"] = "Região"
    assert pricing_source_fingerprint(edited) != pricing_source_fingerprint(
        {**edited, "freight_tables": [{k: v for k, v in edited["freight_tables"][0].items() if k != "_selected_pricing_dimension_column"}]}
    )


def test_gate_blocks_invalid_tariff_and_conflicting_lookup():
    invalid = _fixed_range_table()
    invalid["freight_tables"][0]["rows"][0]["Até 30 kg"] = "-5,00"
    blocked = validate_pricing_contract_for_confirmation(invalid)
    assert blocked["can_confirm"] is False
    assert CODE_INVALID_TARIFF in {issue["code"] for issue in blocked["blocking_issues"]}

    conflict = {
        "freight_tables": [
            {
                "columns": ["Região", "Até 10 kg"],
                "rows": [
                    {"Região": "Sul", "Até 10 kg": "10,00"},
                    {"Região": "Sul", "Até 10 kg": "20,00"},
                ],
            }
        ],
        "freight_routes": [],
    }
    conflicted = validate_pricing_contract_for_confirmation(conflict)
    assert conflicted["can_confirm"] is False
    assert CODE_CONFLICTING_LOOKUP_KEY in {issue["code"] for issue in conflicted["blocking_issues"]}


def test_documentary_warning_gap_and_missing_optional_toll_do_not_block(app, monkeypatch):
    from tests.test_agente_compara_temp_table_validation_service import _patch_config

    _patch_config(monkeypatch)
    table = _fixed_range_table()
    table["freight_tables"].append(
        {
            "table_title": "Condições gerais",
            "columns": ["Prazo", "Observação"],
            "rows": [{"Prazo": "", "Observação": "Documento auxiliar"}],
            "notes": "",
            "confidence": "low",
        }
    )
    table["freight_tables"][0]["columns"] = ["Região", "0 a 10 kg", "30 a 40 kg"]
    table["freight_tables"][0]["rows"] = [{"Região": "Sul", "0 a 10 kg": "10,00", "30 a 40 kg": "20,00"}]
    with app.app_context():
        result = validate_temp_table_for_confirmation(table, enforce_pricing_contract=True)
    assert result["can_confirm"] is True
    assert result["blocking_count"] == 0
    assert any(item["code"] == CODE_READING_ALERT for item in result["warnings"])
    contract = build_pricing_contract(table)
    assert contract["rules"][0]["pricing_type"] == "fixed_range"
    assert contract["rules"][0].get("freight_value") is None
    assert contract["rules"][0].get("route_toll") is None


def test_single_table_math_matches_with_and_without_contract():
    record = _pricing_record()
    without = calculate_single_table(_make_context(record=record, rows=[_row(weight=48), _row(weight=20, row_index=2)]))
    with_contract = copy.deepcopy(record)
    with_contract["pricing_contract"] = build_pricing_contract(with_contract)
    with_result = calculate_single_table(
        _make_context(record=with_contract, rows=[_row(weight=48), _row(weight=20, row_index=2)])
    )
    assert [item["calculated_freight"] for item in with_result["results"]] == [
        item["calculated_freight"] for item in without["results"]
    ]
    assert with_result["results"][0]["calculated_freight"] == 100.50


def test_documentary_prazo_valor_block_does_not_block_executable_tariff():
    table = _fixed_range_table()
    table["freight_tables"].append(
        {
            "table_title": "Prazos",
            "columns": ["Prazo", "Valor"],
            "rows": [{"Prazo": "5 dias", "Valor": "Sob consulta"}],
        }
    )
    validation = validate_pricing_contract_for_confirmation(table)
    assert validation["can_confirm"] is True, validation["blocking_issues"]
    assert CODE_UNRESOLVED_PRICING_TYPE not in {issue["code"] for issue in validation["blocking_issues"]}
    contract = build_pricing_contract(table)
    assert len(contract["rules"]) == 1
    assert contract["rules"][0]["lookup_keys"] == ["Sul"]
    assert contract["rules"][0]["pricing_type"] == "fixed_range"


def test_interior_sp_and_rj_keep_uf_qualified_keys_without_conflict():
    table = {
        "freight_tables": [
            {
                "table_title": "Interior por UF",
                "columns": ["UF", "Região de frete", "Até 30 kg"],
                "rows": [
                    {"UF": "SP", "Região de frete": "Interior", "Até 30 kg": "10,00"},
                    {"UF": "RJ", "Região de frete": "Interior", "Até 30 kg": "20,00"},
                ],
            }
        ],
        "freight_routes": [],
    }
    validation = validate_pricing_contract_for_confirmation(table)
    assert validation["can_confirm"] is True, validation["blocking_issues"]
    assert CODE_CONFLICTING_LOOKUP_KEY not in {issue["code"] for issue in validation["blocking_issues"]}
    contract = build_pricing_contract(table)
    assert len(contract["rules"]) == 2
    lookup_keys = [key for rule in contract["rules"] for key in rule["lookup_keys"]]
    assert "SP|INTERIOR" in lookup_keys
    assert "RJ|INTERIOR" in lookup_keys
    index = build_freight_pricing_index(_with_contract(table))
    sp_match = _find_pricing_rule_match(index, "Interior", "SP")
    rj_match = _find_pricing_rule_match(index, "Interior", "RJ")
    assert sp_match is not None and rj_match is not None
    sp_rule, _sp_kind, sp_key = sp_match
    rj_rule, _rj_kind, rj_key = rj_match
    assert sp_key == "SP|INTERIOR"
    assert rj_key == "RJ|INTERIOR"
    assert calculate_weight_freight(10, sp_rule)["expected_freight"] == 10.0
    assert calculate_weight_freight(10, rj_rule)["expected_freight"] == 20.0
    assert index["SP|INTERIOR"] is not index["RJ|INTERIOR"]


def test_malformed_lookup_keys_rejects_contract_and_uses_legacy(monkeypatch):
    table = _with_contract(_fixed_range_table())
    table["pricing_contract"]["rules"][0]["lookup_keys"] = 42
    assert table["pricing_contract"]["source_fingerprint"] == pricing_source_fingerprint(table)
    assert pricing_contract_structure_is_usable(table["pricing_contract"]) is False
    sentinel = {"legacy-path": True}
    monkeypatch.setattr(
        "app.agente_compara_doc_service._build_freight_pricing_index_legacy",
        lambda _table: sentinel,
    )
    assert build_freight_pricing_index(table) is sentinel


def _origin_distinguished_table() -> dict:
    return {
        "freight_tables": [
            {
                "table_title": "Tarifa por origem",
                "columns": ["Cidade origem", "UF origem", "Região de frete", "Até 30 kg"],
                "rows": [
                    {
                        "Cidade origem": "Campinas",
                        "UF origem": "SP",
                        "Região de frete": "MG-CAP",
                        "Até 30 kg": "10,00",
                    },
                    {
                        "Cidade origem": "Guarulhos",
                        "UF origem": "SP",
                        "Região de frete": "MG-CAP",
                        "Até 30 kg": "25,00",
                    },
                ],
            }
        ],
        "freight_routes": [],
    }


def _mg_cap_coverage() -> dict:
    return _coverage_rows(("MG", "Belo Horizonte", "MG-CAP"))


def _calculate(table: dict, rows: list[dict], coverage: dict):
    return calculate_single_table(_make_context(record=_with_contract(table), rows=rows, coverage=coverage))


def test_origin_distinguished_tariffs_keep_separate_rules_and_prices():
    table = _origin_distinguished_table()
    validation = validate_pricing_contract_for_confirmation(table)
    assert validation["can_confirm"] is True, validation["blocking_issues"]
    contract = build_pricing_contract(table)
    assert len(contract["rules"]) == 2
    assert contract_preserves_origin_distinction(contract) is True
    lookup_keys = [key for rule in contract["rules"] for key in rule["lookup_keys"]]
    assert lookup_keys == [
        "ORIGIN:SP|CAMPINAS=>MG-CAP",
        "ORIGIN:SP|GUARULHOS=>MG-CAP",
    ]
    assert "MG-CAP" not in lookup_keys
    index = build_freight_pricing_index(_with_contract(table))
    campinas = _find_pricing_rule_match(
        index,
        "MG-CAP",
        "MG",
        "Belo Horizonte",
        origin_uf="SP",
        origin_city="Campinas",
    )
    guarulhos = _find_pricing_rule_match(
        index,
        "MG-CAP",
        "MG",
        "Belo Horizonte",
        origin_uf="SP",
        origin_city="Guarulhos",
    )
    assert campinas is not None and guarulhos is not None
    assert campinas[2] == "ORIGIN:SP|CAMPINAS=>MG-CAP"
    assert guarulhos[2] == "ORIGIN:SP|GUARULHOS=>MG-CAP"
    assert calculate_weight_freight(10, campinas[0])["expected_freight"] == 10.0
    assert calculate_weight_freight(10, guarulhos[0])["expected_freight"] == 25.0

    calculated = _calculate(
        table,
        [
            _row(
                row_index=1,
                destination_city="Belo Horizonte",
                destination_uf="MG",
                weight=10,
                origin_city="Campinas",
                origin_uf="SP",
            ),
            _row(
                row_index=2,
                document_number="7400456",
                destination_city="Belo Horizonte",
                destination_uf="MG",
                weight=10,
                origin_city="Guarulhos",
                origin_uf="SP",
            ),
        ],
        _mg_cap_coverage(),
    )
    assert [item["calculated_freight"] for item in calculated["results"]] == [10.0, 25.0]
    assert calculated["results"][0]["evidence"]["pricing_lookup_key"] == "ORIGIN:SP|CAMPINAS=>MG-CAP"
    assert calculated["results"][1]["evidence"]["pricing_lookup_key"] == "ORIGIN:SP|GUARULHOS=>MG-CAP"


def test_missing_or_different_origin_does_not_select_tariff():
    table = _origin_distinguished_table()
    missing = _calculate(
        table,
        [
            _row(
                destination_city="Belo Horizonte",
                destination_uf="MG",
                weight=10,
            )
        ],
        _mg_cap_coverage(),
    )
    assert missing["results"][0]["calculated_freight"] is None
    assert missing["results"][0]["status"] == STATUS_MISSING_FREIGHT_RULE

    other = _calculate(
        table,
        [
            _row(
                destination_city="Belo Horizonte",
                destination_uf="MG",
                weight=10,
                origin_city="Santos",
                origin_uf="SP",
            )
        ],
        _mg_cap_coverage(),
    )
    assert other["results"][0]["calculated_freight"] is None
    assert other["results"][0]["status"] == STATUS_MISSING_FREIGHT_RULE
    index = build_freight_pricing_index(_with_contract(table))
    assert _find_pricing_rule_match(index, "MG-CAP", origin_uf="SP", origin_city="Santos") is None
    assert "MG-CAP" not in index


def test_rule_without_origin_dependency_keeps_previous_lookup():
    table = _fixed_range_table()
    contract = build_pricing_contract(table)
    assert contract["rules"][0]["lookup_keys"] == ["Sul"]
    assert contract_preserves_origin_distinction(contract) is False
    calculated = _calculate(
        table,
        [
            _row(
                destination_city="Curitiba",
                destination_uf="PR",
                weight=20,
                origin_city="Campinas",
                origin_uf="SP",
            )
        ],
        _coverage_rows(("PR", "Curitiba", "Sul")),
    )
    assert calculated["results"][0]["calculated_freight"] == 87.13
    assert calculated["results"][0]["evidence"]["pricing_lookup_key"] == "Sul"


def test_direct_destination_curitiba_resolves_qualified_key_without_coverage():
    table = {
        "freight_tables": [
            {
                "table_title": "Destino direto",
                "columns": ["Cidade", "UF", "Até 30 kg"],
                "rows": [{"Cidade": "Curitiba", "UF": "PR", "Até 30 kg": "40,00"}],
            }
        ],
        "freight_routes": [],
    }
    contract = build_pricing_contract(table)
    lookup_keys = contract["rules"][0]["lookup_keys"]
    assert "PR|CURITIBA" in lookup_keys
    validation = validate_pricing_contract_for_confirmation(table)
    assert validation["can_confirm"] is True, validation["blocking_issues"]
    calculated = _calculate(
        table,
        [_row(destination_city="Curitiba", destination_uf="PR", weight=10, invoice_value=None)],
        {"rows": []},
    )
    assert calculated["results"][0]["calculated_freight"] == 40.0
    assert calculated["results"][0]["evidence"]["pricing_lookup_key"] == "PR|CURITIBA"


def test_zone_and_group_keep_existing_freight_region_keys():
    zone = {
        "freight_tables": [
            {
                "columns": ["Zona", "Até 10 kg"],
                "_selected_pricing_dimension_column": "Zona",
                "rows": [{"Zona": "P1", "Até 10 kg": "12,00"}],
            }
        ],
        "freight_routes": [],
    }
    group = {
        "freight_tables": [
            {
                "columns": ["Grupo", "Até 10 kg"],
                "_selected_pricing_dimension_column": "Grupo",
                "rows": [{"Grupo": "MG-CAP", "Até 10 kg": "18,00"}],
            }
        ],
        "freight_routes": [],
    }
    zone_contract = build_pricing_contract(zone)
    group_contract = build_pricing_contract(group)
    assert zone_contract["rules"][0]["lookup_keys"] == ["P1"]
    assert group_contract["rules"][0]["lookup_keys"] == ["MG-CAP"]
    assert "zone" not in zone_contract["rules"][0]
    assert "group" not in group_contract["rules"][0]
    zone_result = _calculate(
        zone,
        [_row(destination_city="Campinas", destination_uf="SP", weight=5, invoice_value=None)],
        _coverage_rows(("SP", "Campinas", "P1")),
    )
    group_result = _calculate(
        group,
        [_row(destination_city="Belo Horizonte", destination_uf="MG", weight=5, invoice_value=None)],
        _mg_cap_coverage(),
    )
    assert zone_result["results"][0]["calculated_freight"] == 12.0
    assert zone_result["results"][0]["evidence"]["pricing_lookup_key"] == "P1"
    assert group_result["results"][0]["calculated_freight"] == 18.0
    assert group_result["results"][0]["evidence"]["pricing_lookup_key"] == "MG-CAP"


def test_zone_column_is_recognized_without_explicit_dimension_selection():
    assert _is_region_column("Zona") is True
    assert _is_region_column("Grupo") is False


def test_zone_rows_without_selected_dimension_keep_distinct_lookup_keys():
    table = {
        "freight_tables": [
            {
                "table_title": "Tabela de Precos por Zona - Sul",
                "columns": ["Zona", "Até 10 kg"],
                "rows": [
                    {"Zona": "S1", "Até 10 kg": "10,00"},
                    {"Zona": "S2", "Até 10 kg": "20,00"},
                ],
            }
        ],
        "freight_routes": [],
    }
    contract = build_pricing_contract(table)
    assert len(contract["rules"]) == 2
    assert [rule["lookup_keys"] for rule in contract["rules"]] == [["S1"], ["S2"]]
    assert "Tabela de Precos por Zona - Sul" not in {
        key for rule in contract["rules"] for key in rule["lookup_keys"]
    }
    validation = validate_pricing_contract_for_confirmation(table)
    assert validation["can_confirm"] is True, validation["blocking_issues"]
    assert CODE_CONFLICTING_LOOKUP_KEY not in {issue["code"] for issue in validation["blocking_issues"]}

    s1 = _calculate(
        table,
        [_row(destination_city="Porto Alegre", destination_uf="RS", weight=5, invoice_value=None)],
        _coverage_rows(("RS", "Porto Alegre", "S1")),
    )
    s2 = _calculate(
        table,
        [_row(destination_city="Florianopolis", destination_uf="SC", weight=5, invoice_value=None)],
        _coverage_rows(("SC", "Florianopolis", "S2")),
    )
    assert s1["results"][0]["calculated_freight"] == 10.0
    assert s1["results"][0]["evidence"]["pricing_lookup_key"] == "S1"
    assert s2["results"][0]["calculated_freight"] == 20.0
    assert s2["results"][0]["evidence"]["pricing_lookup_key"] == "S2"


def test_same_executable_origin_key_stays_blocked():
    table = _origin_distinguished_table()
    table["freight_tables"][0]["rows"].append(
        {
            "Cidade origem": "Campinas",
            "UF origem": "SP",
            "Região de frete": "MG-CAP",
            "Até 30 kg": "99,00",
        }
    )
    validation = validate_pricing_contract_for_confirmation(table)
    assert validation["can_confirm"] is False
    assert CODE_CONFLICTING_LOOKUP_KEY in {issue["code"] for issue in validation["blocking_issues"]}
    labels = {issue.get("label") for issue in validation["blocking_issues"]}
    assert "ORIGIN:SP|CAMPINAS=>MG-CAP" in labels


def test_origin_columns_do_not_qualify_when_destinations_differ():
    table = {
        "freight_tables": [
            {
                "columns": ["Cidade origem", "UF origem", "Região de frete", "Até 10 kg"],
                "rows": [
                    {
                        "Cidade origem": "Campinas",
                        "UF origem": "SP",
                        "Região de frete": "MG-CAP",
                        "Até 10 kg": "10,00",
                    },
                    {
                        "Cidade origem": "Guarulhos",
                        "UF origem": "SP",
                        "Região de frete": "SP-CAP",
                        "Até 10 kg": "20,00",
                    },
                ],
            }
        ],
        "freight_routes": [],
    }
    validation = validate_pricing_contract_for_confirmation(table)
    assert validation["can_confirm"] is True, validation["blocking_issues"]
    lookup_keys = [key for rule in build_pricing_contract(table)["rules"] for key in rule["lookup_keys"]]
    assert lookup_keys == ["MG-CAP", "SP-CAP"]


def test_stale_contract_without_origin_selector_does_not_pick_tariff(monkeypatch):
    table = _origin_distinguished_table()
    compiled = build_pricing_contract(table)
    stale_rule = copy.deepcopy(compiled["rules"][0])
    stale_rule["lookup_keys"] = ["MG-CAP"]
    stale_rule["region"] = "MG-CAP"
    table["pricing_contract"] = {
        "schema_version": 1,
        "source_fingerprint": pricing_source_fingerprint(table),
        "rules": [stale_rule],
    }
    assert contract_preserves_origin_distinction(table["pricing_contract"]) is False
    validation = validate_pricing_contract_for_confirmation(table, table["pricing_contract"])
    assert validation["can_confirm"] is False

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("contrato sem seletor de origem não pode cair no legado")

    monkeypatch.setattr(
        "app.agente_compara_doc_service._build_freight_pricing_index_legacy",
        _forbidden,
    )
    assert build_freight_pricing_index(table) == {}
    calculated = calculate_single_table(
        _make_context(
            record=table,
            rows=[
                _row(
                    destination_city="Belo Horizonte",
                    destination_uf="MG",
                    weight=10,
                    origin_city="Campinas",
                    origin_uf="SP",
                )
            ],
            coverage=_mg_cap_coverage(),
        )
    )
    assert calculated["results"][0]["calculated_freight"] is None
    assert calculated["results"][0]["status"] == STATUS_MISSING_FREIGHT_RULE


def test_legacy_route_origin_is_not_treated_as_city_selector():
    table = {
        "freight_tables": [],
        "freight_routes": [
            {"origin": "Interior", "destination": "SP", "weight_30": "10,00"},
            {"origin": "DF", "destination": "JOINVILLE", "weight_10": "15,00"},
            {"origin": "Campinas", "destination": "MG-CAP", "weight_10": "10,00"},
            {"origin": "Guarulhos", "destination": "MG-CAP", "weight_10": "20,00"},
        ],
    }
    contract = build_pricing_contract(table)
    lookup_keys = [key for rule in contract["rules"] for key in rule["lookup_keys"]]
    assert not any(key.startswith("ORIGIN:") for key in lookup_keys)
    assert "SP|INTERIOR" in lookup_keys
    assert "JOINVILLE" in lookup_keys
    assert "MG-CAP" in lookup_keys
    route_validation = validate_pricing_contract_for_confirmation(table)
    assert route_validation["can_confirm"] is False
    assert CODE_CONFLICTING_LOOKUP_KEY in {issue["code"] for issue in route_validation["blocking_issues"]}
    without_contract = {
        "freight_tables": [],
        "freight_routes": table["freight_routes"][:2],
    }
    legacy = _build_freight_pricing_index_legacy(without_contract)
    assert "SP|INTERIOR" in legacy
    assert "JOINVILLE" in legacy
    assert set(build_freight_pricing_index(without_contract)) == set(legacy)
    origin_matrix_without_contract = _origin_distinguished_table()
    legacy_matrix = build_freight_pricing_index(origin_matrix_without_contract)
    assert not any(str(key).startswith("ORIGIN:") for key in legacy_matrix)
    assert legacy_matrix["MG-CAP"]["pricing_type"] == AUDIT_STATUS_UNSUPPORTED_PRICING


def test_explicit_route_origin_fields_qualify_lookup_keys():
    table = {
        "freight_tables": [],
        "freight_routes": [
            {
                "origin": "rótulo",
                "origin_city": "Campinas",
                "origin_uf": "SP",
                "destination": "MG-CAP",
                "weight_10": "10,00",
            },
            {
                "origin": "rótulo",
                "origin_city": "Guarulhos",
                "origin_uf": "SP",
                "destination": "MG-CAP",
                "weight_10": "25,00",
            },
        ],
    }
    validation = validate_pricing_contract_for_confirmation(table)
    assert validation["can_confirm"] is True, validation["blocking_issues"]
    lookup_keys = [key for rule in build_pricing_contract(table)["rules"] for key in rule["lookup_keys"]]
    assert lookup_keys == [
        "ORIGIN:SP|CAMPINAS=>MG-CAP",
        "ORIGIN:SP|GUARULHOS=>MG-CAP",
    ]
    calculated = _calculate(
        table,
        [
            _row(
                destination_city="Belo Horizonte",
                destination_uf="MG",
                weight=8,
                origin_city="Campinas",
                origin_uf="SP",
                invoice_value=None,
            )
        ],
        _mg_cap_coverage(),
    )
    assert calculated["results"][0]["calculated_freight"] == 10.0


def test_parse_range_from_label_accepts_hyphenated_weight_headers():
    assert _parse_range_from_label("0-10 kg") == (0.0, 10.0)
    assert _parse_range_from_label("11-30 kg") == (11.0, 30.0)
    assert _parse_range_from_label("31-50 kg") == (31.0, 50.0)
    assert _parse_range_from_label("501-1000 kg") == (501.0, 1000.0)
    assert _parse_range_from_label("51–100 kg") == (51.0, 100.0)
    assert _parse_range_from_label("101—200 kg") == (101.0, 200.0)
    assert _parse_range_from_label("0 a 10 kg") == (0.0, 10.0)
    assert _parse_range_from_label("Até 10 kg") == (0.0, 10.0)


def test_parse_range_from_label_ignores_hyphen_without_weight_context():
    assert _parse_range_from_label("0-10") is None
    assert _parse_range_from_label("11-30") is None
    assert _parse_range_from_label("11-30 dias") is None
    assert _parse_range_from_label("0–10") is None
    assert _parse_range_from_label("80000-82999") is None
    assert _parse_range_from_label("Rota 1-2") is None
    assert _parse_range_from_label("30-10 kg") is None


def test_parse_range_from_label_does_not_treat_greater_than_weight_as_closed_range():
    assert _parse_range_from_label(">500 kg") is None
    assert _parse_range_from_label("> 500 kg") is None
    assert _parse_range_from_label("acima de 500 kg") is None
    assert _parse_range_from_label(">500") is None
    assert _parse_range_from_label("> 10 dias") is None


def test_greater_than_weight_header_is_excess_column():
    assert _is_excess_column(">500 kg") is True
    assert _is_excess_column("> 500 kg") is True
    assert _is_excess_column("acima de 500 kg") is True
    assert _is_excess_column(">500") is False
    assert _is_excess_column("> 500") is False
    assert _is_excess_column(">500 dias") is False
    assert _is_excess_column("Rota > 2") is False


def test_greater_than_weight_matrix_compiles_range_plus_excess():
    excess_header = ">500 kg"
    table = {
        "freight_tables": [
            {
                "table_title": "Homolog excedente maior-que",
                "columns": [
                    "Destino",
                    "Prazo",
                    "0-10 kg",
                    "11-30 kg",
                    "31-50 kg",
                    "51-100 kg",
                    "101-200 kg",
                    "201-500 kg",
                    excess_header,
                ],
                "rows": [
                    {
                        "Destino": "Curitiba/PR",
                        "Prazo": "5",
                        "0-10 kg": "31",
                        "11-30 kg": "45",
                        "31-50 kg": "60",
                        "51-100 kg": "87",
                        "101-200 kg": "132",
                        "201-500 kg": "276",
                        excess_header: "0,54",
                    }
                ],
            }
        ],
        "freight_routes": [],
    }
    contract, findings = _compile_pricing_contract(table)
    assert not any(item.get("kind") == "invalid_range" for item in findings)
    rules = [rule for rule in contract["rules"] if rule.get("pricing_type") == "range_plus_excess_per_kg"]
    assert len(rules) == 1
    rule = rules[0]
    assert rule["region"] == "Curitiba/PR"
    assert [
        (bracket["min_kg"], bracket["max_kg"], bracket["value"]) for bracket in rule["brackets"]
    ] == [
        (0.0, 10.0, 31.0),
        (10.0, 30.0, 45.0),
        (30.0, 50.0, 60.0),
        (50.0, 100.0, 87.0),
        (100.0, 200.0, 132.0),
        (200.0, 500.0, 276.0),
    ]
    assert rule["brackets"][-1]["max_kg"] == 500.0
    assert all(bracket.get("label") != excess_header for bracket in rule["brackets"])
    assert rule["excess"]["rate_per_kg"] == 0.54
    validation = validate_pricing_contract_for_confirmation(table, contract)
    assert CODE_INVALID_RANGE not in {issue["code"] for issue in validation["blocking_issues"]}
    assert validation["can_confirm"] is True, validation["blocking_issues"]
    calculated = calculate_weight_freight(600, rule)
    assert calculated is not None
    assert calculated["calculation_basis"] == "range_plus_excess_per_kg"
    assert calculated["expected_freight"] == 330.00


_COMP03_WEIGHT_COLUMNS = [
    "Destino",
    "Prazo",
    "0-10 kg",
    "11-30 kg",
    "31-50 kg",
    "51-100 kg",
    "101-200 kg",
    "201-500 kg",
    "501-1000 kg",
]


def _rota_simples_city_uf_table(
    *,
    title: str,
    first_bracket: str,
    region: str = "Curitiba/PR",
) -> dict:
    row = {
        "Destino": region,
        "Prazo": "5",
        "0-10 kg": first_bracket,
        "11-30 kg": "44",
        "31-50 kg": "58",
        "51-100 kg": "82",
        "101-200 kg": "125",
        "201-500 kg": "245",
        "501-1000 kg": "410",
    }
    return {
        "freight_tables": [
            {
                "table_title": title,
                "columns": list(_COMP03_WEIGHT_COLUMNS),
                "rows": [row],
            }
        ],
        "freight_routes": [],
    }


def _comp_03a_hyphen_weight_table() -> dict:
    return _rota_simples_city_uf_table(title="COMP-03A", first_bracket="32")


def _assert_comp03_weight_brackets(rule: dict, *, first_amount: float) -> None:
    expected = {
        5: first_amount,
        10: first_amount,
        10.5: 44.0,
        11: 44.0,
        30: 44.0,
        30.5: 58.0,
        750: 410.0,
        1000: 410.0,
    }
    for weight, amount in expected.items():
        calculated = calculate_weight_freight(weight, rule)
        assert calculated is not None, weight
        assert calculated["calculation_basis"] == "fixed_range"
        assert calculated["expected_freight"] == amount


def _curitiba_pricing_match(table: dict):
    index = build_freight_pricing_index(_with_contract(table))
    assert "PR|CURITIBA" in index
    match = _find_pricing_rule_match(
        index,
        freight_region="CURITIBA",
        destination_uf="PR",
        destination_city="Curitiba",
    )
    assert match is not None
    rule, _kind, key = match
    assert key == "PR|CURITIBA"
    return rule, index


def test_comp_03a_hyphen_matrix_compiles_executable_fixed_range():
    table = _comp_03a_hyphen_weight_table()
    contract, _findings = _compile_pricing_contract(table)
    rules = [rule for rule in contract["rules"] if rule.get("pricing_type") == "fixed_range"]
    assert len(rules) == 1
    rule = rules[0]
    assert rule["region"] == "Curitiba/PR"
    assert rule["lookup_keys"] == ["Curitiba/PR", "CURITIBA PR", "PR|CURITIBA"]
    assert [
        (bracket["min_kg"], bracket["max_kg"], bracket["value"]) for bracket in rule["brackets"]
    ] == [
        (0.0, 10.0, 32.0),
        (10.0, 30.0, 44.0),
        (30.0, 50.0, 58.0),
        (50.0, 100.0, 82.0),
        (100.0, 200.0, 125.0),
        (200.0, 500.0, 245.0),
        (500.0, 1000.0, 410.0),
    ]
    validation = validate_pricing_contract_for_confirmation(table, contract)
    assert CODE_NO_EXECUTABLE_PRICING_RULE not in {
        issue["code"] for issue in validation["blocking_issues"]
    }
    assert validation["can_confirm"] is True, validation["blocking_issues"]
    expected = {
        10: 32.0,
        10.5: 44.0,
        11: 44.0,
        30: 44.0,
        30.5: 58.0,
        750: 410.0,
    }
    for weight, amount in expected.items():
        calculated = calculate_weight_freight(weight, rule)
        assert calculated is not None
        assert calculated["calculation_basis"] == "fixed_range"
        assert calculated["expected_freight"] == amount


def test_active_route_without_destination_blocks_confirmation():
    table = {
        "freight_tables": [],
        "freight_routes": [
            {"destination": "Sul", "weight_10": "10,00"},
            {"weight_10": "25,00"},
        ],
    }
    contract = build_pricing_contract(table)
    assert any("Sul" in (rule.get("lookup_keys") or []) for rule in contract["rules"])
    validation = validate_pricing_contract_for_confirmation(table)
    assert validation["can_confirm"] is False
    assert CODE_MISSING_LOOKUP_KEY in {issue["code"] for issue in validation["blocking_issues"]}
    assert any(issue.get("field") == "destination" for issue in validation["blocking_issues"])


@pytest.mark.parametrize(
    "region",
    [
        "Curitiba/PR",
        "Curitiba / PR",
        "Curitiba - PR",
        "Curitiba \u2013 PR",
        "Curitiba \u2014 PR",
        "PR/Curitiba",
    ],
)
def test_comp03_explicit_city_uf_formats_keep_alias_and_add_canonical_key(region):
    table = _rota_simples_city_uf_table(title="COMP-03", first_bracket="32", region=region)
    keys = build_pricing_contract(table)["rules"][0]["lookup_keys"]
    assert keys[0] == region
    assert "PR|CURITIBA" in keys
    assert "CURITIBA" not in keys


def test_comp03_rota_simples_a_and_b_resolve_curitiba_by_canonical_key():
    rota_a = _rota_simples_city_uf_table(title="Rota Simples A", first_bracket="32")
    rota_b = _rota_simples_city_uf_table(title="Rota Simples B", first_bracket="34")

    for table, first_amount in ((rota_a, 32.0), (rota_b, 34.0)):
        contract = build_pricing_contract(table)
        assert contract["rules"][0]["lookup_keys"] == [
            "Curitiba/PR",
            "CURITIBA PR",
            "PR|CURITIBA",
        ]
        validation = validate_pricing_contract_for_confirmation(table, contract)
        assert validation["can_confirm"] is True, validation["blocking_issues"]
        rule, index = _curitiba_pricing_match(table)
        assert index["PR|CURITIBA"]["region"] == "Curitiba/PR"
        assert calculate_weight_freight(5, rule)["expected_freight"] == first_amount
        _assert_comp03_weight_brackets(rule, first_amount=first_amount)


def test_comp03_confirmed_contract_without_canonical_key_still_indexes_it():
    table = _rota_simples_city_uf_table(title="Rota Simples A", first_bracket="32")
    contract = build_pricing_contract(table)
    contract["rules"][0]["lookup_keys"] = ["Curitiba/PR", "CURITIBA PR"]
    table["pricing_contract"] = contract
    index = build_freight_pricing_index(table)
    assert "PR|CURITIBA" in index
    assert index["Curitiba/PR"] is index["PR|CURITIBA"]
    match = _find_pricing_rule_match(
        index,
        freight_region="CURITIBA",
        destination_uf="PR",
        destination_city="Curitiba",
    )
    assert match is not None
    rule, _kind, key = match
    assert key == "PR|CURITIBA"
    assert calculate_weight_freight(5, rule)["expected_freight"] == 32.0


def test_comp03_homonymous_cities_do_not_share_city_only_alias():
    table = _rota_simples_city_uf_table(title="Homônimos", first_bracket="32")
    table["freight_tables"][0]["rows"].append(
        {
            "Destino": "Curitiba/SC",
            "Prazo": "5",
            "0-10 kg": "99",
            "11-30 kg": "44",
            "31-50 kg": "58",
            "51-100 kg": "82",
            "101-200 kg": "125",
            "201-500 kg": "245",
            "501-1000 kg": "410",
        }
    )
    validation = validate_pricing_contract_for_confirmation(table)
    assert validation["can_confirm"] is True, validation["blocking_issues"]
    contract = build_pricing_contract(table)
    pr_rule = next(rule for rule in contract["rules"] if "PR|CURITIBA" in rule["lookup_keys"])
    sc_rule = next(rule for rule in contract["rules"] if "SC|CURITIBA" in rule["lookup_keys"])
    assert "CURITIBA" not in pr_rule["lookup_keys"]
    assert "CURITIBA" not in sc_rule["lookup_keys"]
    assert set(pr_rule["lookup_keys"]).isdisjoint(sc_rule["lookup_keys"])
    index = build_freight_pricing_index(_with_contract(table))
    pr_match = _find_pricing_rule_match(index, "CURITIBA", "PR", "Curitiba")
    sc_match = _find_pricing_rule_match(index, "CURITIBA", "SC", "Curitiba")
    assert pr_match is not None and sc_match is not None
    assert pr_match[2] == "PR|CURITIBA"
    assert sc_match[2] == "SC|CURITIBA"
    assert calculate_weight_freight(5, pr_match[0])["expected_freight"] == 32.0
    assert calculate_weight_freight(5, sc_match[0])["expected_freight"] == 99.0
    assert pr_match[0] is not sc_match[0]


def test_city_without_explicit_uf_does_not_invent_canonical_key():
    plain = {
        "freight_tables": [
            {
                "columns": ["Destino", "0-10 kg"],
                "rows": [{"Destino": "Curitiba", "0-10 kg": "32"}],
            }
        ],
        "freight_routes": [],
    }
    spaced = {
        "freight_tables": [
            {
                "columns": ["Destino", "0-10 kg"],
                "rows": [{"Destino": "Curitiba PR", "0-10 kg": "32"}],
            }
        ],
        "freight_routes": [],
    }
    invalid_uf = {
        "freight_tables": [
            {
                "columns": ["Destino", "0-10 kg"],
                "rows": [{"Destino": "Curitiba/XX", "0-10 kg": "32"}],
            }
        ],
        "freight_routes": [],
    }
    assert build_pricing_contract(plain)["rules"][0]["lookup_keys"] == ["Curitiba", "CURITIBA"]
    assert build_pricing_contract(spaced)["rules"][0]["lookup_keys"] == ["Curitiba PR", "CURITIBA PR"]
    invalid_keys = build_pricing_contract(invalid_uf)["rules"][0]["lookup_keys"]
    assert "Curitiba/XX" in invalid_keys
    assert not any("|" in key for key in invalid_keys)


def test_comp03_origin_qualified_city_uf_does_not_publish_bare_canonical_key():
    table = {
        "freight_tables": [
            {
                "columns": ["Cidade origem", "UF origem", "Destino", "0-10 kg"],
                "rows": [
                    {
                        "Cidade origem": "Campinas",
                        "UF origem": "SP",
                        "Destino": "Curitiba/PR",
                        "0-10 kg": "32",
                    },
                    {
                        "Cidade origem": "Guarulhos",
                        "UF origem": "SP",
                        "Destino": "Curitiba/PR",
                        "0-10 kg": "34",
                    },
                ],
            }
        ],
        "freight_routes": [],
    }
    keys = [key for rule in build_pricing_contract(table)["rules"] for key in rule["lookup_keys"]]
    assert keys
    assert all(key.startswith("ORIGIN:") for key in keys)
    assert "PR|CURITIBA" not in keys
    assert any(key.endswith("=>PR|CURITIBA") for key in keys)
    index = build_freight_pricing_index(_with_contract(table))
    assert "PR|CURITIBA" not in index
    assert _find_pricing_rule_match(index, "CURITIBA", "PR", "Curitiba") is None
    match = _find_pricing_rule_match(
        index,
        "CURITIBA",
        "PR",
        "Curitiba",
        origin_uf="SP",
        origin_city="Campinas",
    )
    assert match is not None
    assert match[2].startswith("ORIGIN:SP|CAMPINAS=>")
    assert calculate_weight_freight(5, match[0])["expected_freight"] == 32.0


def test_freight_route_city_slash_uf_adds_canonical_key_without_city_only_alias():
    table = {
        "freight_tables": [],
        "freight_routes": [{"destination": "Curitiba/PR", "weight_10": "32,00"}],
    }
    keys = build_pricing_contract(table)["rules"][0]["lookup_keys"]
    assert "Curitiba/PR" in keys
    assert "PR|CURITIBA" in keys
    assert "CURITIBA" not in keys
