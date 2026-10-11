"""SCRUM-146: rota equivalente à freight_table não publica segunda regra."""
from __future__ import annotations

import copy

from app.agente_compara_doc_service import (
    _compile_pricing_contract,
    _executable_tariff_signature,
    build_pricing_contract,
    calculate_weight_freight,
)
from app.agente_compara_temp_table_validation_service import (
    CODE_CONFLICTING_LOOKUP_KEY,
    validate_pricing_contract_for_confirmation,
)
from tests.test_agente_compara_pricing_contract import (
    _COMP01B_ZONES,
    _COMP03_WEIGHT_COLUMNS,
    _assert_comp01b_zone_amounts,
    _calculate,
    _comp01b_sulcarga_table,
    _comp_03a_hyphen_weight_table,
    _coverage_rows,
    _row,
)

_EIXOLOG_BANDS = ("0-10 kg", "11-30 kg", "31-50 kg", "51-100 kg", "101-200 kg", "201-500 kg", "501-1000 kg")
_EIXOLOG_ROUTE_FIELDS = (
    "weight_10",
    "weight_30",
    "weight_50",
    "weight_100",
    "weight_200",
    "weight_500",
    "weight_1000",
)
_EIXOLOG_ALFA = (
    ("Curitiba/PR", ("32", "44", "58", "82", "125", "245", "410")),
    ("Londrina/PR", ("36", "49", "64", "90", "136", "265", "440")),
    ("Joinville/SC", ("38", "52", "68", "96", "144", "280", "465")),
    ("Florianopolis/SC", ("42", "58", "76", "106", "158", "308", "505")),
)


def _city_row(region: str, amounts: tuple[str, ...]) -> dict:
    row = {"Destino": region, "Prazo": "5"}
    row.update(dict(zip(_EIXOLOG_BANDS, amounts, strict=True)))
    return row


def _city_route(region: str, amounts: tuple[str, ...]) -> dict:
    route = {"destination": region}
    route.update(dict(zip(_EIXOLOG_ROUTE_FIELDS, amounts, strict=True)))
    return route


def _eixolog_alfa_table() -> dict:
    return {
        "freight_tables": [
            {
                "table_title": "EixoLog Alfa",
                "columns": list(_COMP03_WEIGHT_COLUMNS),
                "rows": [_city_row(region, amounts) for region, amounts in _EIXOLOG_ALFA],
            }
        ],
        "freight_routes": [_city_route(region, amounts) for region, amounts in _EIXOLOG_ALFA],
    }


def _single_city_table(*, table_amount: str, route_amount: str, route_destination: str = "Curitiba/PR") -> dict:
    return {
        "freight_tables": [
            {
                "table_title": "EixoLog Alfa",
                "columns": ["Destino", "0-10 kg"],
                "rows": [{"Destino": "Curitiba/PR", "0-10 kg": table_amount}],
            }
        ],
        "freight_routes": [{"destination": route_destination, "weight_10": route_amount}],
    }


def _pricing_blocking_count(table: dict) -> int:
    validation = validate_pricing_contract_for_confirmation(table)
    return int(validation["blocking_count"])


def _route_backed_rules(rules: list[dict]) -> list[dict]:
    selected = []
    for rule in rules:
        refs = rule.get("source_refs") or []
        if any(isinstance(ref, str) and ref.startswith("freight_routes[") for ref in refs):
            if not any(isinstance(ref, str) and ref.startswith("freight_tables[") for ref in refs):
                selected.append(rule)
    return selected


def test_identical_table_and_route_collapse_to_one_rule():
    table = _single_city_table(table_amount="32", route_amount="32,00")
    contract, findings = _compile_pricing_contract(table)
    assert [rule["region"] for rule in contract["rules"]] == ["Curitiba/PR"]
    rule = contract["rules"][0]
    assert rule["source_table_title"] == "EixoLog Alfa"
    assert rule["brackets"][0]["label"] == "0-10 kg"
    assert rule["brackets"][0]["value"] == 32.0
    assert "freight_tables[0]" in rule["source_refs"]
    assert "freight_routes[0]" in rule["source_refs"]
    assert not any(item.get("kind") == "conflicting_lookup_key" for item in findings)
    assert _pricing_blocking_count(table) == 0
    assert calculate_weight_freight(5, rule)["expected_freight"] == 32.0


def test_same_lookup_keys_with_different_tariff_stay_blocking():
    table = _single_city_table(table_amount="32", route_amount="99,00")
    contract, findings = _compile_pricing_contract(table)
    assert [rule["region"] for rule in contract["rules"]] == ["Curitiba/PR", "Curitiba/PR"]
    table_rule, route_rule = contract["rules"]
    assert table_rule["brackets"][0]["value"] == 32.0
    assert route_rule["brackets"][0]["value"] == 99.0
    assert "freight_routes[0]" not in (table_rule.get("source_refs") or [])
    assert any(item.get("kind") == "conflicting_lookup_key" for item in findings)
    validation = validate_pricing_contract_for_confirmation(table, contract)
    assert validation["can_confirm"] is False
    assert CODE_CONFLICTING_LOOKUP_KEY in {issue["code"] for issue in validation["blocking_issues"]}
    assert _pricing_blocking_count(table) > 0


def test_equivalent_route_alias_is_merged_into_existing_rule():
    table = _single_city_table(
        table_amount="32",
        route_amount="32",
        route_destination="Curitiba / PR",
    )
    contract, findings = _compile_pricing_contract(table)
    assert len(contract["rules"]) == 1
    rule = contract["rules"][0]
    assert rule["region"] == "Curitiba/PR"
    assert rule["label"] == "Curitiba/PR"
    assert "Curitiba / PR" in rule["lookup_keys"]
    assert "PR|CURITIBA" in rule["lookup_keys"]
    assert rule["lookup_keys"].count("PR|CURITIBA") == 1
    assert "freight_routes[0]" in rule["source_refs"]
    assert not any(item.get("kind") == "conflicting_lookup_key" for item in findings)
    assert _pricing_blocking_count(table) == 0


def test_route_disputed_by_two_rules_is_not_deduped():
    table = {
        "freight_tables": [
            {
                "table_title": "EixoLog Alfa",
                "columns": ["Destino", "0-10 kg"],
                "rows": [
                    {"Destino": "Curitiba/PR", "0-10 kg": "32"},
                    {"Destino": "Curitiba/PR", "0-10 kg": "99"},
                ],
            }
        ],
        "freight_routes": [{"destination": "Curitiba/PR", "weight_10": "32"}],
    }
    contract, findings = _compile_pricing_contract(table)
    assert len(contract["rules"]) == 3
    assert [rule["brackets"][0]["value"] for rule in contract["rules"]] == [32.0, 99.0, 32.0]
    assert len(_route_backed_rules(contract["rules"])) == 1
    assert not any(
        "freight_routes[0]" in (rule.get("source_refs") or [])
        and any(str(ref).startswith("freight_tables[") for ref in (rule.get("source_refs") or []))
        for rule in contract["rules"]
    )
    assert any(item.get("kind") == "conflicting_lookup_key" for item in findings)
    validation = validate_pricing_contract_for_confirmation(table, contract)
    assert CODE_CONFLICTING_LOOKUP_KEY in {issue["code"] for issue in validation["blocking_issues"]}


def test_source_refs_do_not_split_equivalent_tariffs():
    table = _single_city_table(table_amount="32", route_amount="32")
    table["freight_routes"][0]["evidence_ref"] = "rota-curitiba"
    table["freight_routes"][0]["notes"] = "mesma tarifa, outra evidência"
    contract, findings = _compile_pricing_contract(table)
    assert len(contract["rules"]) == 1
    rule = contract["rules"][0]
    assert rule["source_refs"] == [
        "freight_tables[0]",
        "freight_tables[0].rows[0]",
        "freight_routes[0]",
    ]
    assert not any(item.get("kind") == "conflicting_lookup_key" for item in findings)

    base = {
        "pricing_type": "fixed_range",
        "unit": "kg",
        "region": "Curitiba/PR",
        "label": "Curitiba/PR",
        "source_table_title": "EixoLog Alfa",
        "source_refs": ["freight_tables[0]"],
        "normalization_notes": ["faixa lida da tabela"],
        "evidence_ref": "tabela",
        "lookup_keys": ["Curitiba/PR", "PR|CURITIBA"],
        "brackets": [{"min_kg": 0.0, "max_kg": 10.0, "value": 32.0, "label": "0-10 kg"}],
        "excess": None,
        "freight_value": {
            "rate": 1.5,
            "calculation_base": "invoice_value",
            "source_column": "Frete valor %",
            "source_value": "1,50",
        },
        "route_toll": {
            "rate_per_fraction": 5.0,
            "fraction_size_kg": 100.0,
            "source_column": "Pedágio",
            "source_value": "5,00",
        },
    }
    other = copy.deepcopy(base)
    other["label"] = "Curitiba / PR"
    other["source_table_title"] = "freight_routes"
    other["source_refs"] = ["freight_routes[0]"]
    other["normalization_notes"] = ["eco da rota"]
    other["evidence_ref"] = "rota-curitiba"
    other["lookup_keys"] = ["PR|CURITIBA", "Curitiba / PR"]
    other["brackets"] = [{"min_kg": 0, "max_kg": 10, "value": 32, "label": "Até 10 kg"}]
    other["freight_value"] = {
        "rate": 1.50,
        "calculation_base": "invoice_value",
        "source_column": "ADV",
        "source_value": "1.5%",
    }
    other["route_toll"] = {
        "rate_per_fraction": 5,
        "fraction_size_kg": 100,
        "source_column": "pedagio",
        "source_value": "5",
    }
    assert _executable_tariff_signature(base) == _executable_tariff_signature(other)

    different_toll = copy.deepcopy(other)
    different_toll["route_toll"]["rate_per_fraction"] = 9.0
    assert _executable_tariff_signature(base) != _executable_tariff_signature(different_toll)
    different_value = copy.deepcopy(other)
    different_value["brackets"][0]["value"] = 99.0
    assert _executable_tariff_signature(base) != _executable_tariff_signature(different_value)


def test_comp01a_eixolog_alfa_equivalent_routes_are_not_conflicting():
    table = _eixolog_alfa_table()
    contract, findings = _compile_pricing_contract(table)
    assert [rule["region"] for rule in contract["rules"]] == [
        "Curitiba/PR",
        "Londrina/PR",
        "Joinville/SC",
        "Florianopolis/SC",
    ]
    assert len(contract["rules"]) == 4
    assert _route_backed_rules(contract["rules"]) == []
    for rule in contract["rules"]:
        assert any(str(ref).startswith("freight_routes[") for ref in rule["source_refs"])
        assert any(str(ref).startswith("freight_tables[") for ref in rule["source_refs"])
    assert not any(item.get("kind") == "conflicting_lookup_key" for item in findings)
    validation = validate_pricing_contract_for_confirmation(table, contract)
    pricing_blocking_count = validation["blocking_count"]
    assert pricing_blocking_count == 0
    assert validation["can_confirm"] is True
    curitiba = contract["rules"][0]
    assert calculate_weight_freight(5, curitiba)["expected_freight"] == 32.0
    assert calculate_weight_freight(750, curitiba)["expected_freight"] == 410.0
    assert build_pricing_contract(table)["rules"][3]["region"] == "Florianopolis/SC"


def test_comp01b_zone_tariffs_remain_authoritative():
    table = _comp01b_sulcarga_table()
    contract, findings = _compile_pricing_contract(table)
    assert [rule["region"] for rule in contract["rules"]] == ["P1", "P2", "S1", "S2"]
    assert not any(item.get("kind") == "conflicting_lookup_key" for item in findings)
    assert _pricing_blocking_count(table) == 0
    expected_zones = {
        "Curitiba": "P1",
        "Londrina": "P2",
        "Joinville": "S1",
        "Florianópolis": "S2",
    }
    for zone in _COMP01B_ZONES:
        assert expected_zones[zone["city"]] == zone["zone"]
        _assert_comp01b_zone_amounts(table, zone)
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
            _coverage_rows((zone["uf"], zone["city"], zone["zone"])),
        )
        assert covered["results"][0]["calculated_freight"] == zone["expected"][6]
        assert covered["results"][0]["evidence"]["pricing_lookup_key"] == zone["zone"]


def test_comp03_city_uf_contract_stays_confirmable():
    table = _comp_03a_hyphen_weight_table()
    contract = build_pricing_contract(table)
    assert contract["rules"][0]["region"] == "Curitiba/PR"
    assert contract["rules"][0]["lookup_keys"] == ["Curitiba/PR", "CURITIBA PR", "PR|CURITIBA"]
    validation = validate_pricing_contract_for_confirmation(table, contract)
    assert validation["can_confirm"] is True, validation["blocking_issues"]
    assert validation["blocking_count"] == 0
    assert calculate_weight_freight(5, contract["rules"][0])["expected_freight"] == 32.0
    assert calculate_weight_freight(750, contract["rules"][0])["expected_freight"] == 410.0
