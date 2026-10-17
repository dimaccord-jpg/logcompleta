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
    _build_rules_from_matrix_table,
    _compile_pricing_contract,
    _find_pricing_rule_match,
    _freight_route_is_weight_100_only,
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
    CODE_AMBIGUOUS_COMPLETE_TABLE_MATCH,
    CODE_CONFLICTING_LOOKUP_KEY,
    CODE_INVALID_EXCESS,
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


_I01A_RANGE_HEADERS = ("0-10", "11-30", "31-50", "51-100", "101-200", "201-500")
_I01A_EXCESS_HEADER = "Excedente >500"
_I01A_NORMALIZED_BOUNDS = (
    (0.0, 10.0),
    (10.0, 30.0),
    (30.0, 50.0),
    (50.0, 100.0),
    (100.0, 200.0),
    (200.0, 500.0),
)
_I01A_CITIES = (
    ("Curitiba/PR", ("R$ 34,80", "R$ 49,35", "R$ 65,40", "R$ 91,75", "R$ 137,90", "R$ 269,40"), "R$ 0,71/kg", 0.71, (34.80, 49.35, 65.40, 91.75, 137.90, 269.40)),
    ("Londrina/PR", ("R$ 37,20", "R$ 52,80", "R$ 69,10", "R$ 97,30", "R$ 145,20", "R$ 283,70"), "R$ 0,74/kg", 0.74, (37.20, 52.80, 69.10, 97.30, 145.20, 283.70)),
    ("Joinville/SC", ("R$ 39,10", "R$ 55,60", "R$ 72,90", "R$ 101,80", "R$ 151,90", "R$ 296,20"), "R$ 0,77/kg", 0.77, (39.10, 55.60, 72.90, 101.80, 151.90, 296.20)),
    ("Florianópolis/SC", ("R$ 43,50", "R$ 61,80", "R$ 80,70", "R$ 112,90", "R$ 168,40", "R$ 327,60"), "R$ 0,83/kg", 0.83, (43.50, 61.80, 80.70, 112.90, 168.40, 327.60)),
    ("Belo Horizonte/MG", ("R$ 41,20", "R$ 58,90", "R$ 77,10", "R$ 108,20", "R$ 161,50", "R$ 315,40"), "R$ 0,80/kg", 0.80, (41.20, 58.90, 77.10, 108.20, 161.50, 315.40)),
    ("Uberlândia/MG", ("R$ 47,80", "R$ 67,60", "R$ 88,30", "R$ 123,90", "R$ 184,70", "R$ 359,50"), "R$ 0,91/kg", 0.91, (47.80, 67.60, 88.30, 123.90, 184.70, 359.50)),
)


def _i01a_row(city: str, amounts: tuple[str, ...], excess: str) -> dict:
    row = {"Destino": city, _I01A_EXCESS_HEADER: excess}
    for header, amount in zip(_I01A_RANGE_HEADERS, amounts):
        row[header] = amount
    return row


def _i01a_weight_matrix(*, notes: str, excess_by_city: dict[str, str] | None = None) -> dict:
    rows = []
    for city, amounts, excess, *_rest in _I01A_CITIES:
        rows.append(_i01a_row(city, amounts, (excess_by_city or {}).get(city, excess)))
    return {
        "freight_tables": [
            {
                "table_title": "Tabela comercial - origem Campinas/SP",
                "columns": ["Destino", *_I01A_RANGE_HEADERS, _I01A_EXCESS_HEADER],
                "rows": rows,
                "notes": notes,
                "context": {"origin": "Campinas/SP"},
            }
        ],
        "freight_routes": [],
    }


def _executable_rules_by_region(table: dict) -> dict[str, dict]:
    contract, _findings = _compile_pricing_contract(table)
    return {
        rule.get("region"): rule
        for rule in contract["rules"]
        if rule.get("pricing_type") == "range_plus_excess_per_kg"
    }


def test_i01a_unitless_headers_compile_when_matrix_proves_weight():
    assert _parse_range_from_label("0-10") is None
    assert _parse_range_from_label("0-10 kg") == (0.0, 10.0)
    assert _is_excess_column(_I01A_EXCESS_HEADER) is True
    assert _parse_range_from_label(_I01A_EXCESS_HEADER) is None

    table = _i01a_weight_matrix(notes="Faixa máxima 500 kg + excedente por kg")
    curitiba_row = table["freight_tables"][0]["rows"][0]
    built = _build_rules_from_matrix_table(
        {
            "table_title": table["freight_tables"][0]["table_title"],
            "columns": table["freight_tables"][0]["columns"],
            "rows": [curitiba_row],
            "notes": table["freight_tables"][0]["notes"],
            "context": table["freight_tables"][0]["context"],
        }
    )
    assert built
    assert {rule["pricing_type"] for _key, rule in built} == {"range_plus_excess_per_kg"}
    assert all(rule["brackets"] for _key, rule in built)
    assert all(rule["excess"]["rate_per_kg"] == 0.71 for _key, rule in built)

    rules = _executable_rules_by_region(table)
    assert set(rules) == {city for city, *_rest in _I01A_CITIES}
    for city, _amounts, _excess, rate, values in _I01A_CITIES:
        rule = rules[city]
        assert [
            (bracket["min_kg"], bracket["max_kg"], bracket["value"]) for bracket in rule["brackets"]
        ] == [(*bounds, amount) for bounds, amount in zip(_I01A_NORMALIZED_BOUNDS, values)]
        assert rule["brackets"][-1]["max_kg"] == 500.0
        assert rule["excess"]["rate_per_kg"] == rate
        assert all(bracket.get("label") != _I01A_EXCESS_HEADER for bracket in rule["brackets"])

    validation = validate_pricing_contract_for_confirmation(table)
    blocking = {issue["code"] for issue in validation["blocking_issues"]}
    assert CODE_INVALID_EXCESS not in blocking
    assert CODE_NO_EXECUTABLE_PRICING_RULE not in blocking
    assert validation["can_confirm"] is True, validation["blocking_issues"]

    curitiba = rules["Curitiba/PR"]
    within_band = calculate_weight_freight(10, curitiba)
    next_band = calculate_weight_freight(10.5, curitiba)
    at_limit = calculate_weight_freight(500, curitiba)
    above_limit = calculate_weight_freight(600, curitiba)
    assert within_band["expected_freight"] == 34.80
    assert next_band["expected_freight"] == 49.35
    assert at_limit["expected_freight"] == 269.40
    assert above_limit["calculation_basis"] == "range_plus_excess_per_kg"
    assert above_limit["expected_freight"] == 340.40


def test_excess_threshold_alone_does_not_prove_unitless_ranges():
    excess_by_city = {city: excess.replace("R$ ", "").replace("/kg", "") for city, _amounts, excess, *_rest in _I01A_CITIES}
    table = _i01a_weight_matrix(notes="", excess_by_city=excess_by_city)
    assert _is_excess_column(_I01A_EXCESS_HEADER) is True
    rules = _executable_rules_by_region(table)
    assert rules == {}
    validation = validate_pricing_contract_for_confirmation(table)
    assert validation["can_confirm"] is False
    assert CODE_NO_EXECUTABLE_PRICING_RULE in {issue["code"] for issue in validation["blocking_issues"]}

    proved_by_cell = _i01a_weight_matrix(notes="")
    proved_rules = _executable_rules_by_region(proved_by_cell)
    assert len(proved_rules) == 6
    assert proved_rules["Curitiba/PR"]["brackets"][-1]["max_kg"] == 500.0
    assert proved_rules["Curitiba/PR"]["excess"]["rate_per_kg"] == 0.71
    assert validate_pricing_contract_for_confirmation(proved_by_cell)["can_confirm"] is True


def test_unitless_numeric_columns_without_weight_context_stay_blocked():
    table = {
        "freight_tables": [
            {
                "table_title": "Tabela comercial",
                "columns": ["Destino", "0-10", "11-30"],
                "rows": [{"Destino": "Curitiba/PR", "0-10": "34,80", "11-30": "49,35"}],
                "notes": "",
            }
        ],
        "freight_routes": [],
    }
    built = _build_rules_from_matrix_table(table["freight_tables"][0])
    assert built
    assert {rule["pricing_type"] for _key, rule in built} == {AUDIT_STATUS_UNSUPPORTED_PRICING}
    assert all(not rule["brackets"] for _key, rule in built)
    assert all(rule["excess"] is None for _key, rule in built)
    validation = validate_pricing_contract_for_confirmation(table)
    assert validation["can_confirm"] is False
    assert CODE_NO_EXECUTABLE_PRICING_RULE in {issue["code"] for issue in validation["blocking_issues"]}

    prose_table = {
        "freight_tables": [
            {
                "table_title": "Tabela comercial",
                "columns": ["Destino", "0 a 10", "11-30"],
                "rows": [{"Destino": "Curitiba/PR", "0 a 10": "34,80", "11-30": "49,35"}],
                "notes": "",
            }
        ],
        "freight_routes": [],
    }
    prose_rules = [
        rule
        for rule in build_pricing_contract(prose_table)["rules"]
        if rule.get("pricing_type") == "fixed_range"
    ]
    assert len(prose_rules) == 1
    assert [bracket["label"] for bracket in prose_rules[0]["brackets"]] == ["0 a 10"]


def test_weight_context_does_not_absorb_unrelated_numeric_columns():
    table = {
        "freight_tables": [
            {
                "table_title": "Faixas de peso (kg)",
                "columns": [
                    "Destino",
                    "Prazo",
                    "0-10",
                    "11-30",
                    "80000-82999",
                    "Rota 1-2",
                    "11-30 dias",
                    "Excedente >30",
                ],
                "rows": [
                    {
                        "Destino": "Curitiba/PR",
                        "Prazo": "5",
                        "0-10": "34,80",
                        "11-30": "49,35",
                        "80000-82999": "12,00",
                        "Rota 1-2": "9,00",
                        "11-30 dias": "8,00",
                        "Excedente >30": "0,71",
                    }
                ],
                "notes": "",
            }
        ],
        "freight_routes": [],
    }
    rules = _executable_rules_by_region(table)
    rule = rules["Curitiba/PR"]
    assert [(bracket["min_kg"], bracket["max_kg"], bracket["value"]) for bracket in rule["brackets"]] == [
        (0.0, 10.0, 34.80),
        (10.0, 30.0, 49.35),
    ]
    assert rule["excess"]["rate_per_kg"] == 0.71
    assert [bracket["label"] for bracket in rule["brackets"]] == ["0-10", "11-30"]


def test_incomplete_unitless_weight_row_stays_blocked():
    table = _i01a_weight_matrix(notes="Faixa máxima 500 kg + excedente por kg")
    for row in table["freight_tables"][0]["rows"]:
        for header in _I01A_RANGE_HEADERS:
            row[header] = None
    validation = validate_pricing_contract_for_confirmation(table)
    assert validation["can_confirm"] is False
    blocking = {issue["code"] for issue in validation["blocking_issues"]}
    assert CODE_INVALID_EXCESS in blocking
    assert CODE_NO_EXECUTABLE_PRICING_RULE in blocking


def _unitless_destination_table(
    *,
    columns: list[str],
    row: dict,
    notes: str,
    title: str = "Tabela comercial",
) -> dict:
    return {
        "freight_tables": [
            {
                "table_title": title,
                "columns": columns,
                "rows": [row],
                "notes": notes,
            }
        ],
        "freight_routes": [],
    }


def _assert_weight_not_priced(table: dict, weight: float, forbidden_amount: float) -> dict:
    contract, _findings = _compile_pricing_contract(table)
    priced = []
    for rule in contract["rules"]:
        if rule.get("pricing_type") not in {"fixed_range", "range_plus_excess_per_kg"}:
            continue
        calculated = calculate_weight_freight(weight, rule)
        if calculated is not None:
            priced.append(calculated["expected_freight"])
    assert forbidden_amount not in priced
    assert priced == []
    validation = validate_pricing_contract_for_confirmation(table)
    assert validation["can_confirm"] is False
    return validation


def test_day_narrative_does_not_confirm_unitless_ranges_as_weight():
    columns = ["Destino", "0-10", "11-30", "Excedente >30"]
    row = {"Destino": "Curitiba/PR", "0-10": "34,80", "11-30": "49,35", "Excedente >30": "0,71"}
    notes = "Faixas de prazo em dias; excedente por dia"
    table = _unitless_destination_table(columns=columns, row=row, notes=notes)
    validation = _assert_weight_not_priced(table, 10, 34.80)
    assert CODE_NO_EXECUTABLE_PRICING_RULE in {issue["code"] for issue in validation["blocking_issues"]}

    contradicted = _unitless_destination_table(
        columns=columns,
        row={**row, "Excedente >30": "0,71/kg"},
        notes=notes,
        title="Faixas de peso (kg)",
    )
    contradicted_validation = _assert_weight_not_priced(contradicted, 10, 34.80)
    assert contradicted_validation["can_confirm"] is False


def test_disconnected_unitless_weight_ranges_stay_blocked():
    table = _unitless_destination_table(
        columns=["Destino", "0-10", "31-50", "Excedente >50"],
        row={"Destino": "Curitiba/PR", "0-10": "34,80", "31-50": "65,40", "Excedente >50": "0,71"},
        notes="Faixa de peso em kg",
        title="Faixas de peso (kg)",
    )
    validation = _assert_weight_not_priced(table, 20, 65.40)
    assert CODE_INVALID_RANGE in {issue["code"] for issue in validation["blocking_issues"]}


def test_blank_intermediate_unitless_band_stays_blocked():
    table = _unitless_destination_table(
        columns=["Destino", "0-10", "11-30", "31-50", "Excedente >50"],
        row={
            "Destino": "Curitiba/PR",
            "0-10": "34,80",
            "11-30": None,
            "31-50": "65,40",
            "Excedente >50": "0,71",
        },
        notes="Faixa de peso em kg",
        title="Faixas de peso (kg)",
    )
    validation = _assert_weight_not_priced(table, 20, 65.40)
    blocking = {issue["code"] for issue in validation["blocking_issues"]}
    assert CODE_INVALID_RANGE in blocking


def test_labeled_weight_gap_keeps_existing_normalization():
    table = _unitless_destination_table(
        columns=["Destino", "0-10 kg", "31-50 kg"],
        row={"Destino": "Sul", "0-10 kg": "10,00", "31-50 kg": "20,00"},
        notes="",
        title="Faixas com unidade",
    )
    rules = [
        rule
        for rule in build_pricing_contract(table)["rules"]
        if rule.get("pricing_type") == "fixed_range"
    ]
    assert len(rules) == 1
    assert calculate_weight_freight(20, rules[0])["expected_freight"] == 20.0
    validation = validate_pricing_contract_for_confirmation(table)
    assert validation["can_confirm"] is True, validation["blocking_issues"]


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


_COMP01B_COLUMNS = [
    "Zona",
    "Até 10 kg",
    "Até 30 kg",
    "Até 50 kg",
    "Até 100 kg",
    "Até 200 kg",
    "Até 500 kg",
    ">500 kg",
]

# P1 é a tarifa SulCarga Beta. P2/S1/S2 repetem a mesma estrutura com valores distintos.
_COMP01B_ZONES = (
    {
        "zone": "P1",
        "city": "Curitiba",
        "uf": "PR",
        "destination": "Curitiba/PR",
        "amounts": ("35", "49", "65", "94", "139", "288", "0,56"),
        "expected": {6: 35.0, 18: 49.0, 42: 65.0, 85: 94.0, 160: 139.0, 340: 288.0, 620: 355.20},
    },
    {
        "zone": "P2",
        "city": "Londrina",
        "uf": "PR",
        "destination": "Londrina/PR",
        "amounts": ("40", "55", "72", "108", "155", "310", "0,70"),
        "expected": {6: 40.0, 18: 55.0, 42: 72.0, 85: 108.0, 160: 155.0, 340: 310.0, 620: 394.0},
    },
    {
        "zone": "S1",
        "city": "Joinville",
        "uf": "SC",
        "destination": "Joinville/SC",
        "amounts": ("38", "52", "68", "99", "145", "295", "0,61"),
        "expected": {6: 38.0, 18: 52.0, 42: 68.0, 85: 99.0, 160: 145.0, 340: 295.0, 620: 368.20},
    },
    {
        "zone": "S2",
        "city": "Florianópolis",
        "uf": "SC",
        "destination": "Florianópolis/SC",
        "amounts": ("48", "66", "84", "120", "175", "330", "0,75"),
        "expected": {6: 48.0, 18: 66.0, 42: 84.0, 85: 120.0, 160: 175.0, 340: 330.0, 620: 420.0},
    },
)


def _comp01b_sulcarga_table() -> dict:
    rows = []
    routes = []
    for zone in _COMP01B_ZONES:
        ate_10, ate_30, ate_50, ate_100, ate_200, ate_500, excess = zone["amounts"]
        rows.append(
            {
                "Zona": zone["zone"],
                "Até 10 kg": ate_10,
                "Até 30 kg": ate_30,
                "Até 50 kg": ate_50,
                "Até 100 kg": ate_100,
                "Até 200 kg": ate_200,
                "Até 500 kg": ate_500,
                ">500 kg": excess,
            }
        )
        routes.append({"destination": zone["destination"], "weight_100": ate_100})
    return {
        "freight_tables": [
            {
                "table_title": "SulCarga Beta",
                "columns": list(_COMP01B_COLUMNS),
                "rows": rows,
            }
        ],
        "freight_routes": routes,
    }


def _assert_comp01b_zone_amounts(table: dict, zone: dict) -> None:
    for weight, amount in zone["expected"].items():
        calculated = _calculate(
            table,
            [
                _row(
                    destination_city=zone["city"],
                    destination_uf=zone["uf"],
                    weight=weight,
                    invoice_value=None,
                )
            ],
            {"rows": []},
        )
        result = calculated["results"][0]
        assert result["calculated_freight"] == amount, (zone["zone"], weight, result)
        assert result["status"] == "calculated"


def test_comp01b_partial_route_does_not_publish_degraded_fixed_range():
    table = _comp01b_sulcarga_table()
    contract, findings = _compile_pricing_contract(table)
    assert not any(item.get("kind") == "conflicting_lookup_key" for item in findings)
    assert [rule["region"] for rule in contract["rules"]] == ["P1", "P2", "S1", "S2"]
    assert {rule["pricing_type"] for rule in contract["rules"]} == {"range_plus_excess_per_kg"}
    for rule in contract["rules"]:
        assert rule["region"] != "Curitiba/PR"
        assert not any(
            bracket.get("min_kg") == 0.0 and bracket.get("max_kg") == 100.0 and bracket.get("value") == 94.0
            for bracket in rule["brackets"]
        )
    p1 = next(rule for rule in contract["rules"] if rule["region"] == "P1")
    assert "P1" in p1["lookup_keys"]
    assert "PR|CURITIBA" in p1["lookup_keys"]
    assert p1["excess"]["rate_per_kg"] == 0.56
    assert p1["brackets"][-1]["max_kg"] == 500.0
    assert p1["brackets"][-1]["value"] == 288.0
    validation = validate_pricing_contract_for_confirmation(table, contract)
    assert validation["can_confirm"] is True, validation["blocking_issues"]


@pytest.mark.parametrize("zone", _COMP01B_ZONES, ids=[item["zone"] for item in _COMP01B_ZONES])
def test_comp01b_city_lookup_uses_complete_zone_tariff(zone):
    table = _comp01b_sulcarga_table()
    _assert_comp01b_zone_amounts(table, zone)
    coverage = _coverage_rows((zone["uf"], zone["city"], zone["zone"]))
    covered = _calculate(
        table,
        [
            _row(
                destination_city=zone["city"],
                destination_uf=zone["uf"],
                weight=6,
                invoice_value=None,
            )
        ],
        coverage,
    )
    assert covered["results"][0]["calculated_freight"] == zone["expected"][6]
    assert covered["results"][0]["evidence"]["pricing_lookup_key"] == zone["zone"]


def test_comp01b_curitiba_index_is_the_complete_p1_rule():
    table = _comp01b_sulcarga_table()
    index = build_freight_pricing_index(_with_contract(table))
    assert index["PR|CURITIBA"] is index["P1"]
    assert index["P1"]["pricing_type"] == "range_plus_excess_per_kg"
    assert "Curitiba/PR" not in {
        rule.get("region") for rule in build_pricing_contract(table)["rules"]
    }
    calculated = calculate_weight_freight(620, index["PR|CURITIBA"])
    assert calculated["calculation_basis"] == "range_plus_excess_per_kg"
    assert calculated["expected_freight"] == 355.20


def test_weight_100_only_route_without_table_stays_executable():
    table = {
        "freight_tables": [],
        "freight_routes": [{"destination": "Curitiba/PR", "weight_100": "94,00"}],
    }
    rule = build_pricing_contract(table)["rules"][0]
    assert rule["pricing_type"] == "fixed_range"
    assert rule["region"] == "Curitiba/PR"
    assert [(bracket["min_kg"], bracket["max_kg"], bracket["value"]) for bracket in rule["brackets"]] == [
        (0.0, 100.0, 94.0)
    ]
    assert calculate_weight_freight(6, rule)["expected_freight"] == 94.0


def test_autonomous_complete_freight_route_stays_independent():
    route = {
        "destination": "Santos/SP",
        "weight_10": "22,00",
        "weight_30": "33,00",
        "weight_50": "44,00",
        "weight_100": "55,00",
    }
    alone = {"freight_tables": [], "freight_routes": [route]}
    alone_rule = build_pricing_contract(alone)["rules"][0]
    assert alone_rule["region"] == "Santos/SP"
    assert alone_rule["pricing_type"] == "fixed_range"
    assert calculate_weight_freight(6, alone_rule)["expected_freight"] == 22.0
    assert calculate_weight_freight(85, alone_rule)["expected_freight"] == 55.0

    combined = _comp01b_sulcarga_table()
    combined["freight_routes"].append(route)
    contract = build_pricing_contract(combined)
    regions = [rule["region"] for rule in contract["rules"]]
    assert regions == ["P1", "P2", "S1", "S2", "Santos/SP"]
    santos = contract["rules"][-1]
    assert santos["pricing_type"] == "fixed_range"
    assert "SP|SANTOS" in santos["lookup_keys"]
    assert calculate_weight_freight(6, santos)["expected_freight"] == 22.0
    assert "SP|SANTOS" not in {
        key for rule in contract["rules"] if rule["region"] != "Santos/SP" for key in rule["lookup_keys"]
    }


def test_weight_100_route_with_unrelated_table_stays_independent():
    table = {
        "freight_tables": [
            {
                "columns": ["Zona", "Até 10 kg", "Até 100 kg", ">500 kg"],
                "rows": [{"Zona": "P1", "Até 10 kg": "35", "Até 100 kg": "94", ">500 kg": "0,56"}],
            }
        ],
        "freight_routes": [{"destination": "Santos/SP", "weight_100": "70,00"}],
    }
    contract, findings = _compile_pricing_contract(table)
    assert [rule["region"] for rule in contract["rules"]] == ["P1", "Santos/SP"]
    assert not any(item.get("kind") == "ambiguous_complete_table_match" for item in findings)
    santos = contract["rules"][1]
    assert santos["pricing_type"] == "fixed_range"
    assert santos["brackets"] == [
        {"min_kg": 0.0, "max_kg": 100.0, "value": 70.0, "label": "Até 100 kg"}
    ]


def test_ambiguous_weight_100_echo_stays_executable_and_unlinked():
    table = {
        "freight_tables": [
            {
                "columns": list(_COMP01B_COLUMNS),
                "rows": [
                    {
                        "Zona": "P1",
                        "Até 10 kg": "35",
                        "Até 30 kg": "49",
                        "Até 50 kg": "65",
                        "Até 100 kg": "94",
                        "Até 200 kg": "139",
                        "Até 500 kg": "288",
                        ">500 kg": "0,56",
                    },
                    {
                        "Zona": "P2",
                        "Até 10 kg": "36",
                        "Até 30 kg": "50",
                        "Até 50 kg": "66",
                        "Até 100 kg": "94",
                        "Até 200 kg": "140",
                        "Até 500 kg": "289",
                        ">500 kg": "0,57",
                    },
                ],
            }
        ],
        "freight_routes": [{"destination": "Curitiba/PR", "weight_100": "94"}],
    }
    contract, findings = _compile_pricing_contract(table)
    assert [rule["region"] for rule in contract["rules"]] == ["P1", "P2", "Curitiba/PR"]
    p1 = next(rule for rule in contract["rules"] if rule["region"] == "P1")
    p2 = next(rule for rule in contract["rules"] if rule["region"] == "P2")
    curitiba = next(rule for rule in contract["rules"] if rule["region"] == "Curitiba/PR")
    assert "PR|CURITIBA" not in p1["lookup_keys"]
    assert "PR|CURITIBA" not in p2["lookup_keys"]
    assert "PR|CURITIBA" in curitiba["lookup_keys"]
    assert curitiba["pricing_type"] == "fixed_range"
    assert curitiba["brackets"] == [
        {"min_kg": 0.0, "max_kg": 100.0, "value": 94.0, "label": "Até 100 kg"}
    ]
    assert any(item.get("kind") == "ambiguous_complete_table_match" for item in findings)
    validation = validate_pricing_contract_for_confirmation(table, contract)
    assert CODE_AMBIGUOUS_COMPLETE_TABLE_MATCH in {
        issue["code"] for issue in validation["blocking_issues"]
    }
    assert validation["can_confirm"] is False


def test_route_with_higher_weight_bands_is_not_weight_100_only():
    route = {
        "destination": "Curitiba/PR",
        "weight_100": "94",
        "weight_200": "139",
        "weight_500": "288",
    }
    assert _freight_route_is_weight_100_only(route) is False
    table = _comp01b_sulcarga_table()
    table["freight_routes"] = [route]
    contract = build_pricing_contract(table)
    curitiba = next(rule for rule in contract["rules"] if rule["region"] == "Curitiba/PR")
    p1 = next(rule for rule in contract["rules"] if rule["region"] == "P1")
    assert [bracket["max_kg"] for bracket in curitiba["brackets"]] == [100.0, 200.0, 500.0]
    assert [bracket["value"] for bracket in curitiba["brackets"]] == [94.0, 139.0, 288.0]
    assert "PR|CURITIBA" in curitiba["lookup_keys"]
    assert "PR|CURITIBA" not in p1["lookup_keys"]

    alias_route = {
        "destination": "Curitiba/PR",
        "weight_100kg": "94",
        "weight_200kg": "139",
        "weight_500kg": "288",
    }
    assert _freight_route_is_weight_100_only(alias_route) is False
    alias_table = _comp01b_sulcarga_table()
    alias_table["freight_routes"] = [alias_route]
    alias_contract = build_pricing_contract(alias_table)
    alias_city = next(rule for rule in alias_contract["rules"] if rule["region"] == "Curitiba/PR")
    alias_p1 = next(rule for rule in alias_contract["rules"] if rule["region"] == "P1")
    assert [bracket["max_kg"] for bracket in alias_city["brackets"]] == [100.0, 200.0, 500.0]
    assert "PR|CURITIBA" not in alias_p1["lookup_keys"]


def test_other_executable_component_blocks_weight_100_only():
    toll_route = {"destination": "Curitiba/PR", "weight_100": "94", "pedagio": "5,00"}
    gris_route = {"destination": "Curitiba/PR", "weight_100": "94", "gris": "3,50"}
    excess_route = {"destination": "Curitiba/PR", "weight_100": "94", "freight_weight_kg": "0,56"}
    assert _freight_route_is_weight_100_only(toll_route) is False
    assert _freight_route_is_weight_100_only(gris_route) is False
    assert _freight_route_is_weight_100_only(excess_route) is False
    table = _comp01b_sulcarga_table()
    table["freight_routes"] = [toll_route]
    contract = build_pricing_contract(table)
    curitiba = next(rule for rule in contract["rules"] if rule["region"] == "Curitiba/PR")
    p1 = next(rule for rule in contract["rules"] if rule["region"] == "P1")
    assert curitiba["route_toll"]["rate_per_fraction"] == 5.0
    assert "PR|CURITIBA" not in p1["lookup_keys"]


def test_blank_extra_weight_key_still_yields_to_unique_table():
    table = _comp01b_sulcarga_table()
    for route in table["freight_routes"]:
        if route["destination"] == "Curitiba/PR":
            route["weight_200"] = ""
            route["weight_500"] = "   "
            assert _freight_route_is_weight_100_only(route) is True
    contract = build_pricing_contract(table)
    assert "Curitiba/PR" not in {rule["region"] for rule in contract["rules"]}
    p1 = next(rule for rule in contract["rules"] if rule["region"] == "P1")
    assert "PR|CURITIBA" in p1["lookup_keys"]


def test_explicit_zone_note_yields_when_weight_100_matches():
    table = _comp01b_sulcarga_table()
    table["freight_tables"][0]["rows"][1]["Até 100 kg"] = "94"
    for route in table["freight_routes"]:
        if route["destination"] == "Curitiba/PR":
            route["notes"] = "Rota baseada na Zona P1"
            route["weight_100"] = "94"
    contract, findings = _compile_pricing_contract(table)
    p1 = next(rule for rule in contract["rules"] if rule["region"] == "P1")
    p2 = next(rule for rule in contract["rules"] if rule["region"] == "P2")
    assert "Curitiba/PR" not in {rule["region"] for rule in contract["rules"]}
    assert "PR|CURITIBA" in p1["lookup_keys"]
    assert "PR|CURITIBA" not in p2["lookup_keys"]
    assert not any(item.get("kind") == "ambiguous_complete_table_match" for item in findings)
    calculated = _calculate(
        table,
        [_row(destination_city="Curitiba", destination_uf="PR", weight=6, invoice_value=None)],
        {"rows": []},
    )
    assert calculated["results"][0]["calculated_freight"] == 35.0
    assert calculated["results"][0]["status"] == "calculated"
    heavy = _calculate(
        table,
        [_row(destination_city="Curitiba", destination_uf="PR", weight=620, invoice_value=None)],
        {"rows": []},
    )
    assert heavy["results"][0]["calculated_freight"] == 355.20


def test_explicit_zone_note_does_not_yield_when_weight_100_disagrees():
    table = _comp01b_sulcarga_table()
    table["freight_tables"][0]["rows"][1]["Até 100 kg"] = "70"
    for route in table["freight_routes"]:
        if route["destination"] == "Curitiba/PR":
            route["notes"] = "Rota baseada na Zona P1"
            route["weight_100"] = "70"
    contract, findings = _compile_pricing_contract(table)
    p1 = next(rule for rule in contract["rules"] if rule["region"] == "P1")
    p2 = next(rule for rule in contract["rules"] if rule["region"] == "P2")
    curitiba = next(rule for rule in contract["rules"] if rule["region"] == "Curitiba/PR")
    assert "PR|CURITIBA" not in p1["lookup_keys"]
    assert "PR|CURITIBA" not in p2["lookup_keys"]
    assert "PR|CURITIBA" in curitiba["lookup_keys"]
    assert curitiba["brackets"] == [
        {"min_kg": 0.0, "max_kg": 100.0, "value": 70.0, "label": "Até 100 kg"}
    ]
    assert not any(item.get("kind") == "ambiguous_complete_table_match" for item in findings)
    validation = validate_pricing_contract_for_confirmation(table, contract)
    assert validation["can_confirm"] is True, validation["blocking_issues"]
