"""SCRUM-146: rota isolada só entra na freight_table com limiar estruturado.

O compilador compara excess_threshold_kg com o max_kg da última faixa.
column_labels permanece evidência e não decide a absorção.
"""
from __future__ import annotations

import inspect

import pytest

from app.agente_compara_doc_service import (
    _absorb_excess_only_route_into_complete_table,
    _compile_pricing_contract,
    _executable_tariff_signature,
    _route_structured_excess_threshold_kg,
    build_pricing_contract,
    calculate_weight_freight,
)
from app.agente_compara_temp_table_validation_service import (
    CODE_CONFLICTING_LOOKUP_KEY,
    validate_pricing_contract_for_confirmation,
)
from tests.test_agente_compara_pricing_contract import (
    _comp01b_sulcarga_table,
    _comp_03a_hyphen_weight_table,
)

_EIXOLOG_COLUMNS = [
    "Destino",
    "Prazo",
    "0-10 kg",
    "11-30 kg",
    "31-50 kg",
    "51-100 kg",
    "101-200 kg",
    "201-500 kg",
    ">500 kg",
]
_CURITIBA_BRACKETS = ("31", "45", "60", "87", "132", "276")
_EIXOLOG_RUNTIME_DESTINATIONS = (
    ("Curitiba/PR", "0.54", 500),
    ("Londrina/PR", "0.59", 500),
    ("Joinville/SC", "0.63", 500),
    ("Florianopolis/SC", "0.68", 500),
)
_ABSENT = object()


def _eixolog_runtime_route(
    destination: str,
    excess: str,
    *,
    threshold=_ABSENT,
    label: str | None = ">500 kg",
) -> dict:
    route = {
        "origin": "Sao Paulo/SP - capital",
        "destination": destination,
        "freight_type": None,
        "freight_weight_kg": excess,
        "notes": "",
        "confidence": "needs_review",
    }
    if threshold is _ABSENT:
        route["excess_threshold_kg"] = 500
    else:
        route["excess_threshold_kg"] = threshold
    if label is not None:
        route["column_labels"] = {"freight_weight_kg": label}
    return route


def _eixolog_runtime_row(destination: str, excess: str, brackets: tuple[str, ...] = _CURITIBA_BRACKETS) -> dict:
    row = {"Destino": destination, "Prazo": "5", ">500 kg": excess}
    row.update(
        {
            "0-10 kg": brackets[0],
            "11-30 kg": brackets[1],
            "31-50 kg": brackets[2],
            "51-100 kg": brackets[3],
            "101-200 kg": brackets[4],
            "201-500 kg": brackets[5],
        }
    )
    return row


def _eixolog_runtime_snapshot(destinations=_EIXOLOG_RUNTIME_DESTINATIONS) -> dict:
    """Tabela com faixas e rota só com taxa e limiar já estruturados."""
    return {
        "freight_tables": [
            {
                "table_title": "EixoLog",
                "columns": list(_EIXOLOG_COLUMNS),
                "rows": [
                    _eixolog_runtime_row(destination, excess)
                    for destination, excess, _threshold in destinations
                ],
            }
        ],
        "freight_routes": [
            _eixolog_runtime_route(destination, excess, threshold=threshold)
            for destination, excess, threshold in destinations
        ],
    }


def _pricing_blocking_count(table: dict) -> int:
    validation = validate_pricing_contract_for_confirmation(table)
    return int(validation["blocking_count"])


def _assert_curitiba_route_absorbed(table: dict) -> None:
    contract, findings = _compile_pricing_contract(table)
    assert len(contract["rules"]) == 1
    rule = contract["rules"][0]
    assert rule["pricing_type"] == "range_plus_excess_per_kg"
    assert rule["excess"]["rate_per_kg"] == 0.54
    assert "freight_tables[0]" in rule["source_refs"]
    assert "freight_routes[0]" in rule["source_refs"]
    assert not any(item["pricing_type"] == "direct_weight_rate" for item in contract["rules"])
    assert not any(item.get("kind") == "conflicting_lookup_key" for item in findings)
    assert _pricing_blocking_count(table) == 0


def _assert_curitiba_route_not_absorbed(table: dict) -> dict:
    contract, findings = _compile_pricing_contract(table)
    assert [rule["pricing_type"] for rule in contract["rules"]] == [
        "range_plus_excess_per_kg",
        "direct_weight_rate",
    ]
    table_rule, route_rule = contract["rules"]
    assert table_rule["excess"]["rate_per_kg"] == 0.54
    assert "freight_routes[0]" not in (table_rule.get("source_refs") or [])
    assert "freight_routes[0]" in route_rule["source_refs"]
    assert any(item.get("kind") == "conflicting_lookup_key" for item in findings)
    assert _pricing_blocking_count(table) == 0
    return route_rule


def test_curitiba_excess_only_route_stays_inside_the_table_rule():
    table = _eixolog_runtime_snapshot((("Curitiba/PR", "0.54", 500),))
    assert table["freight_routes"][0]["freight_weight_kg"] == "0.54"
    assert table["freight_routes"][0]["excess_threshold_kg"] == 500
    assert table["freight_routes"][0]["column_labels"] == {"freight_weight_kg": ">500 kg"}
    contract, findings = _compile_pricing_contract(table)
    assert len(contract["rules"]) == 1
    rule = contract["rules"][0]
    assert rule["region"] == "Curitiba/PR"
    assert rule["pricing_type"] == "range_plus_excess_per_kg"
    assert rule["excess"]["rate_per_kg"] == 0.54
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
    assert "freight_tables[0]" in rule["source_refs"]
    assert "freight_routes[0]" in rule["source_refs"]
    assert not any(item["pricing_type"] == "direct_weight_rate" for item in contract["rules"])
    assert not any(item.get("kind") == "conflicting_lookup_key" for item in findings)
    assert _pricing_blocking_count(table) == 0
    assert calculate_weight_freight(600, rule)["expected_freight"] == 330.0
    assert calculate_weight_freight(600, rule)["calculation_basis"] == "range_plus_excess_per_kg"


@pytest.mark.parametrize(
    ("destination", "excess", "threshold"),
    _EIXOLOG_RUNTIME_DESTINATIONS,
    ids=["Curitiba", "Londrina", "Joinville", "Florianopolis"],
)
def test_real_destination_excess_only_route_is_absorbed(destination, excess, threshold):
    table = _eixolog_runtime_snapshot(((destination, excess, threshold),))
    contract, findings = _compile_pricing_contract(table)
    assert len(contract["rules"]) == 1
    rule = contract["rules"][0]
    assert rule["region"] == destination
    assert rule["pricing_type"] == "range_plus_excess_per_kg"
    assert rule["excess"]["rate_per_kg"] == float(excess)
    assert rule["brackets"][-1]["max_kg"] == threshold
    assert "freight_tables[0]" in rule["source_refs"]
    assert "freight_routes[0]" in rule["source_refs"]
    assert not any(item["pricing_type"] == "direct_weight_rate" for item in contract["rules"])
    assert not any(item.get("kind") == "conflicting_lookup_key" for item in findings)
    assert _pricing_blocking_count(table) == 0


def test_comp01a_eixolog_runtime_snapshot_publishes_four_rules_without_conflict():
    table = _eixolog_runtime_snapshot()
    contract, findings = _compile_pricing_contract(table)
    assert [rule["region"] for rule in contract["rules"]] == [
        "Curitiba/PR",
        "Londrina/PR",
        "Joinville/SC",
        "Florianopolis/SC",
    ]
    assert len(contract["rules"]) == 4
    assert {rule["pricing_type"] for rule in contract["rules"]} == {"range_plus_excess_per_kg"}
    excess_by_region = {
        rule["region"]: rule["excess"]["rate_per_kg"] for rule in contract["rules"]
    }
    assert excess_by_region == {
        "Curitiba/PR": 0.54,
        "Londrina/PR": 0.59,
        "Joinville/SC": 0.63,
        "Florianopolis/SC": 0.68,
    }
    assert {rule["brackets"][-1]["max_kg"] for rule in contract["rules"]} == {500.0}
    for index, rule in enumerate(contract["rules"]):
        assert "freight_tables[0]" in rule["source_refs"]
        assert f"freight_routes[{index}]" in rule["source_refs"]
    assert not any(item.get("kind") == "conflicting_lookup_key" for item in findings)
    executable = build_pricing_contract(table)
    validation = validate_pricing_contract_for_confirmation(table, executable)
    assert validation["blocking_count"] == 0
    assert validation["can_confirm"] is True


def test_threshold_100_does_not_match_table_max_500():
    table = _eixolog_runtime_snapshot((("Curitiba/PR", "0.54", 100),))
    route_rule = _assert_curitiba_route_not_absorbed(table)
    assert route_rule["value_per_kg"] == 0.54


def test_null_threshold_is_not_absorbed():
    table = _eixolog_runtime_snapshot((("Curitiba/PR", "0.54", None),))
    route_rule = _assert_curitiba_route_not_absorbed(table)
    assert route_rule["value_per_kg"] == 0.54


def test_absent_threshold_is_not_absorbed():
    table = _eixolog_runtime_snapshot((("Curitiba/PR", "0.54", 500),))
    table["freight_routes"][0].pop("excess_threshold_kg")
    route_rule = _assert_curitiba_route_not_absorbed(table)
    assert route_rule["value_per_kg"] == 0.54


def test_column_label_does_not_decide_absorption():
    absorbed = _eixolog_runtime_snapshot((("Curitiba/PR", "0.54", 500),))
    absorbed["freight_routes"][0]["column_labels"] = {"freight_weight_kg": "Frete por kg"}
    _assert_curitiba_route_absorbed(absorbed)

    blocked = _eixolog_runtime_snapshot((("Curitiba/PR", "0.54", 500),))
    blocked["freight_routes"][0]["column_labels"] = {"freight_weight_kg": ">500 kg"}
    blocked["freight_routes"][0].pop("excess_threshold_kg")
    _assert_curitiba_route_not_absorbed(blocked)


def test_phrase_stored_in_threshold_field_is_not_executable():
    table = _eixolog_runtime_snapshot((("Curitiba/PR", "0.54", ">500 kg"),))
    assert _route_structured_excess_threshold_kg(table["freight_routes"][0]) is None
    _assert_curitiba_route_not_absorbed(table)


@pytest.mark.parametrize("rate", ["0.544", "0.5400001", "0.5399999"])
def test_near_but_different_rate_is_not_absorbed(rate):
    table = _eixolog_runtime_snapshot((("Curitiba/PR", "0.54", 500),))
    table["freight_routes"][0]["freight_weight_kg"] = rate
    route_rule = _assert_curitiba_route_not_absorbed(table)
    assert route_rule["value_per_kg"] == float(rate)


def test_rate_0540_is_absorbed():
    table = _eixolog_runtime_snapshot((("Curitiba/PR", "0.54", 500),))
    table["freight_routes"][0]["freight_weight_kg"] = "0.540"
    _assert_curitiba_route_absorbed(table)


def test_divergent_weight_aliases_are_not_absorbed():
    table = _eixolog_runtime_snapshot((("Curitiba/PR", "0.54", 500),))
    route = table["freight_routes"][0]
    route["freight_weight_kg"] = "0.54"
    route["frete_peso_kg"] = "0.99"
    _assert_curitiba_route_not_absorbed(table)


def test_equivalent_weight_aliases_are_absorbed():
    table = _eixolog_runtime_snapshot((("Curitiba/PR", "0.54", 500),))
    route = table["freight_routes"][0]
    route["freight_weight_kg"] = "0.54"
    route["frete_peso_kg"] = "0.540"
    _assert_curitiba_route_absorbed(table)


def test_invalid_weight_alias_is_not_absorbed():
    table = _eixolog_runtime_snapshot((("Curitiba/PR", "0.54", 500),))
    table["freight_routes"][0]["frete_peso_kg"] = "invalid"
    _assert_curitiba_route_not_absorbed(table)


def test_frete_peso_kg_alias_with_structured_threshold_is_absorbed():
    table = _eixolog_runtime_snapshot((("Curitiba/PR", "0.54", 500),))
    route = table["freight_routes"][0]
    route.pop("freight_weight_kg")
    route["frete_peso_kg"] = "0.54"
    contract, findings = _compile_pricing_contract(table)
    assert len(contract["rules"]) == 1
    assert contract["rules"][0]["pricing_type"] == "range_plus_excess_per_kg"
    assert contract["rules"][0]["excess"]["rate_per_kg"] == 0.54
    assert "freight_routes[0]" in contract["rules"][0]["source_refs"]
    assert not any(item.get("kind") == "conflicting_lookup_key" for item in findings)


def test_two_freight_tables_for_the_same_destination_are_not_chosen():
    route = _eixolog_runtime_route("Curitiba/PR", "0.54", threshold=500)
    table = {
        "freight_tables": [
            {
                "table_title": "EixoLog A",
                "columns": list(_EIXOLOG_COLUMNS),
                "rows": [_eixolog_runtime_row("Curitiba/PR", "0.54")],
            },
            {
                "table_title": "EixoLog B",
                "columns": list(_EIXOLOG_COLUMNS),
                "rows": [_eixolog_runtime_row("Curitiba/PR", "0.54", ("40", "45", "60", "87", "132", "276"))],
            },
        ],
        "freight_routes": [route],
    }
    contract, findings = _compile_pricing_contract(table)
    route_rules = [
        rule
        for rule in contract["rules"]
        if "freight_routes[0]" in (rule.get("source_refs") or [])
        and not any(str(ref).startswith("freight_tables[") for ref in (rule.get("source_refs") or []))
    ]
    assert len(route_rules) == 1
    assert route_rules[0]["pricing_type"] == "direct_weight_rate"
    assert route_rules[0]["value_per_kg"] == 0.54
    assert any(item.get("kind") == "conflicting_lookup_key" for item in findings)
    validation = validate_pricing_contract_for_confirmation(table, contract)
    assert CODE_CONFLICTING_LOOKUP_KEY in {issue["code"] for issue in validation["blocking_issues"]}


def test_route_with_own_brackets_does_not_enter_excess_only_helper():
    route = {
        "destination": "Curitiba/PR",
        "weight_10": "31",
        "weight_30": "45",
        "freight_weight_kg": "0.54",
        "excess_threshold_kg": 500,
        "column_labels": {"freight_weight_kg": ">500 kg"},
    }
    rules = [
        {
            "pricing_type": "range_plus_excess_per_kg",
            "lookup_keys": ["PR|CURITIBA"],
            "brackets": [{"min_kg": 0.0, "max_kg": 500.0, "value": 276.0}],
            "excess": {"rate_per_kg": 0.54},
            "source_refs": ["freight_tables[0]"],
        }
    ]
    owned = {"PR|CURITIBA": 0}
    assert (
        _absorb_excess_only_route_into_complete_table(
            route,
            rules,
            owned,
            ["PR|CURITIBA", "Curitiba/PR"],
            ["freight_routes[0]"],
        )
        is False
    )
    assert rules[0]["source_refs"] == ["freight_tables[0]"]


def test_route_with_own_brackets_still_follows_economic_equivalence():
    columns = ["Destino", "0-10 kg", "11-30 kg", ">500 kg"]
    matching = {
        "freight_tables": [
            {
                "table_title": "EixoLog",
                "columns": columns,
                "rows": [
                    {
                        "Destino": "Curitiba/PR",
                        "0-10 kg": "31",
                        "11-30 kg": "45",
                        ">500 kg": "0.54",
                    }
                ],
            }
        ],
        "freight_routes": [
            {
                "destination": "Curitiba/PR",
                "weight_10": "31",
                "weight_30": "45",
                "freight_weight_kg": "0.54",
                "excess_threshold_kg": 500,
                "column_labels": {"freight_weight_kg": ">500 kg"},
            }
        ],
    }
    contract, findings = _compile_pricing_contract(matching)
    assert len(contract["rules"]) == 1
    assert contract["rules"][0]["pricing_type"] == "range_plus_excess_per_kg"
    assert contract["rules"][0]["excess"]["rate_per_kg"] == 0.54
    assert "freight_tables[0]" in contract["rules"][0]["source_refs"]
    assert "freight_routes[0]" in contract["rules"][0]["source_refs"]
    assert not any(item.get("kind") == "conflicting_lookup_key" for item in findings)


def test_incompatible_executable_key_is_not_absorbed_by_the_helper():
    table = _eixolog_runtime_snapshot((("Curitiba/PR", "0.54", 500),))
    table["freight_routes"][0]["gris"] = "0.30"
    _assert_curitiba_route_not_absorbed(table)


def test_excess_only_route_without_table_stays_direct_weight_rate():
    table = {
        "freight_tables": [],
        "freight_routes": [_eixolog_runtime_route("Curitiba/PR", "0.54", threshold=_ABSENT)],
    }
    table["freight_routes"][0].pop("excess_threshold_kg")
    contract, findings = _compile_pricing_contract(table)
    assert len(contract["rules"]) == 1
    rule = contract["rules"][0]
    assert rule["pricing_type"] == "direct_weight_rate"
    assert rule["value_per_kg"] == 0.54
    assert rule["source_refs"] == ["freight_routes[0]"]
    assert not any(item.get("kind") == "conflicting_lookup_key" for item in findings)
    assert calculate_weight_freight(10, rule)["expected_freight"] == 5.4
    assert calculate_weight_freight(10, rule)["calculation_basis"] == "direct_weight_rate"


def test_threshold_without_table_does_not_invent_a_range_rule():
    table = {
        "freight_tables": [],
        "freight_routes": [_eixolog_runtime_route("Curitiba/PR", "0.54", threshold=500)],
    }
    contract, findings = _compile_pricing_contract(table)
    assert len(contract["rules"]) == 1
    assert contract["rules"][0]["pricing_type"] == "direct_weight_rate"
    assert contract["rules"][0]["value_per_kg"] == 0.54
    assert not any(item.get("kind") == "conflicting_lookup_key" for item in findings)


def test_spaced_destination_alias_is_added_to_the_table_rule():
    table = _eixolog_runtime_snapshot((("Curitiba/PR", "0.54", 500),))
    table["freight_routes"][0]["destination"] = "Curitiba / PR"
    contract, findings = _compile_pricing_contract(table)
    assert len(contract["rules"]) == 1
    rule = contract["rules"][0]
    assert rule["pricing_type"] == "range_plus_excess_per_kg"
    assert "Curitiba / PR" in rule["lookup_keys"]
    assert rule["lookup_keys"].count("PR|CURITIBA") == 1
    assert "freight_routes[0]" in rule["source_refs"]
    assert not any(item.get("kind") == "conflicting_lookup_key" for item in findings)


def test_direct_weight_rate_is_not_treated_as_the_same_tariff_as_excess():
    table_rule = {
        "pricing_type": "range_plus_excess_per_kg",
        "unit": "kg",
        "brackets": [{"min_kg": 0.0, "max_kg": 500.0, "value": 276.0}],
        "excess": {"rate_per_kg": 0.54},
    }
    route_rule = {
        "pricing_type": "direct_weight_rate",
        "unit": "kg",
        "brackets": [],
        "excess": None,
        "value_per_kg": 0.54,
    }
    assert _executable_tariff_signature(table_rule) != _executable_tariff_signature(route_rule)


def test_compiler_does_not_interpret_threshold_text():
    import app.agente_compara_doc_service as service

    assert not hasattr(service, "_STRICT_EXCESS_THRESHOLD_RE")
    assert not hasattr(service, "_strict_excess_threshold_kg")
    assert not hasattr(service, "_isolated_excess_labels_match")
    assert not hasattr(service, "_column_label_for_route_field")
    source = "\n".join(
        inspect.getsource(fn)
        for fn in (
            service._absorb_excess_only_route_into_complete_table,
            service._route_structured_excess_threshold_kg,
        )
    )
    for token in ("column_labels", "re.compile", "re.sub", ".split(", ".startswith(", ".endswith("):
        assert token not in source


def test_sulcarga_zones_have_no_regression():
    table = _comp01b_sulcarga_table()
    contract, findings = _compile_pricing_contract(table)
    assert [rule["region"] for rule in contract["rules"]] == ["P1", "P2", "S1", "S2"]
    assert len(contract["rules"]) == 4
    assert not any(item.get("kind") == "conflicting_lookup_key" for item in findings)
    assert _pricing_blocking_count(table) == 0


def test_comp03_city_uf_contract_has_no_regression():
    table = _comp_03a_hyphen_weight_table()
    contract = build_pricing_contract(table)
    assert len(contract["rules"]) == 1
    assert contract["rules"][0]["region"] == "Curitiba/PR"
    assert contract["rules"][0]["pricing_type"] == "fixed_range"
    validation = validate_pricing_contract_for_confirmation(table, contract)
    assert validation["can_confirm"] is True, validation["blocking_issues"]
    assert validation["blocking_count"] == 0
    assert calculate_weight_freight(5, contract["rules"][0])["expected_freight"] == 32.0
    assert calculate_weight_freight(750, contract["rules"][0])["expected_freight"] == 410.0
