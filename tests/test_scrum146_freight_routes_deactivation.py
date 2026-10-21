"""SCRUM-146: freight_routes deixa de ser fonte tarifária executável no AgenteCompara."""
from __future__ import annotations

import copy

from app.agente_compara_doc_service import (
    _build_freight_pricing_index_legacy,
    _compile_pricing_contract,
    build_freight_pricing_index,
    build_pricing_contract,
    calculate_weight_freight,
    normalize_partial_first_extraction_to_temp_table,
    pricing_source_fingerprint,
)
from app.agente_compara_temp_table_validation_service import (
    CODE_CONFLICTING_LOOKUP_KEY,
    CODE_NO_EXECUTABLE_PRICING_RULE,
    validate_pricing_contract_for_confirmation,
)
from app.services.agente_compara_extraction_billing import entrega_faturavel


def _matrix(title: str, destination: str, amount: str) -> dict:
    return {
        "table_title": title,
        "columns": ["Destino", "0-10 kg"],
        "rows": [{"Destino": destination, "0-10 kg": amount}],
    }


def _snapshot(*tables: dict, routes: list | None = None) -> dict:
    return {
        "freight_tables": list(tables),
        "freight_routes": list(routes or []),
    }


def _route(destination: str, amount: str) -> dict:
    return {"destination": destination, "weight_10": amount}


def _refs(rule: dict) -> list[str]:
    return [ref for ref in (rule.get("source_refs") or []) if isinstance(ref, str)]


def test_divergent_routes_do_not_block_a_legitimate_matrix():
    routes = [_route("Curitiba/PR", "99,00"), _route("Londrina/PR", "80,00")]
    snapshot = _snapshot(_matrix("CargaAurora", "Curitiba/PR", "32"), routes=routes)
    preserved_routes = copy.deepcopy(snapshot["freight_routes"])

    contract = build_pricing_contract(snapshot)
    findings = validate_pricing_contract_for_confirmation(snapshot, contract)

    assert snapshot["freight_routes"] == preserved_routes
    assert [rule["region"] for rule in contract["rules"]] == ["Curitiba/PR"]
    assert contract["rules"][0]["brackets"][0]["value"] == 32.0
    assert _refs(contract["rules"][0]) == ["freight_tables[0]", "freight_tables[0].rows[0]"]
    assert findings["can_confirm"] is True
    assert CODE_CONFLICTING_LOOKUP_KEY not in {issue["code"] for issue in findings["blocking_issues"]}

    snapshot["pricing_contract"] = contract
    priced = build_freight_pricing_index(snapshot)["Curitiba/PR"]
    assert calculate_weight_freight(5, priced)["expected_freight"] == 32.0
    assert "Londrina/PR" not in build_freight_pricing_index(snapshot)


def test_two_legitimate_matrices_remain_conflicting():
    snapshot = _snapshot(
        _matrix("Tabela A", "Curitiba/PR", "32"),
        _matrix("Tabela B", "Curitiba/PR", "99"),
        routes=[_route("Curitiba/PR", "32")],
    )
    contract = build_pricing_contract(snapshot)
    findings = validate_pricing_contract_for_confirmation(snapshot, contract)

    assert [rule["brackets"][0]["value"] for rule in contract["rules"]] == [32.0, 99.0]
    assert [rule["source_table_title"] for rule in contract["rules"]] == ["Tabela A", "Tabela B"]
    assert all(not ref.startswith("freight_routes[") for rule in contract["rules"] for ref in _refs(rule))
    assert findings["can_confirm"] is False
    assert CODE_CONFLICTING_LOOKUP_KEY in {issue["code"] for issue in findings["blocking_issues"]}


def test_freight_table_source_index_is_not_shifted_by_ignoring_routes():
    snapshot = _snapshot(
        {"table_title": "Observações", "columns": ["Observacao"], "rows": [{"Observacao": "nota"}]},
        _matrix("CargaAurora", "Curitiba/PR", "32"),
        routes=[_route("Curitiba/PR", "99,00")],
    )
    contract = build_pricing_contract(snapshot)
    assert len(contract["rules"]) == 1
    assert "freight_tables[1]" in _refs(contract["rules"][0])
    assert "freight_tables[0]" not in _refs(contract["rules"][0])


def test_routes_alone_are_not_executable_and_synthesized_tables_stay_on_their_path():
    routes_only = _snapshot(routes=[_route("Curitiba/PR", "32,00")])
    discarded = build_pricing_contract(routes_only)
    discarded_validation = validate_pricing_contract_for_confirmation(routes_only, discarded)
    assert discarded["rules"] == []
    assert discarded_validation["can_confirm"] is False
    assert CODE_NO_EXECUTABLE_PRICING_RULE in {
        issue["code"] for issue in discarded_validation["blocking_issues"]
    }

    normalized = normalize_partial_first_extraction_to_temp_table(
        {
            "status": "needs_review",
            "freight_tables": [],
            "freight_routes": [
                {
                    "origin": "Joinville",
                    "destination": "Sao Paulo",
                    "weight_50": "120,00",
                    "weight_100": "180,00",
                }
            ],
            "freight_values": [],
            "accessorial_fees": [],
            "weight_ranges": [],
            "reading_alerts": [],
            "evidence_refs": [],
        }
    )
    assert normalized["freight_tables"][0]["table_type"] == "route_matrix_synthesized"
    compiled = build_pricing_contract(normalized)
    assert len(compiled["rules"]) == 1
    assert all(ref.startswith("freight_tables[") for ref in _refs(compiled["rules"][0]))
    assert validate_pricing_contract_for_confirmation(normalized, compiled)["can_confirm"] is True


def test_stored_contract_keeps_route_rules_already_confirmed():
    snapshot = _snapshot(routes=[_route("Curitiba/PR", "32,00")])
    historical, _findings = _compile_pricing_contract(snapshot, include_freight_routes=True)
    assert historical["rules"][0]["source_refs"] == ["freight_routes[0]"]
    assert historical["source_fingerprint"] == pricing_source_fingerprint(snapshot)
    snapshot["pricing_contract"] = historical

    indexed = build_freight_pricing_index(snapshot)
    assert calculate_weight_freight(5, indexed["Curitiba/PR"])["expected_freight"] == 32.0
    assert build_pricing_contract(snapshot)["rules"] == []
    assert _build_freight_pricing_index_legacy(snapshot) == {}


def test_billing_eligibility_stays_independent_of_the_executable_deactivation():
    matrix = _snapshot(_matrix("CargaAurora", "Curitiba/PR", "32"), routes=[_route("Curitiba/PR", "99,00")])
    matrix["temp_table_id"] = "matriz"
    matrix["status"] = "needs_review"
    routes_only = _snapshot(routes=[_route("Curitiba/PR", "32,00")])
    routes_only["temp_table_id"] = "rotas"
    routes_only["status"] = "needs_review"
    synthesized = normalize_partial_first_extraction_to_temp_table(
        {
            "status": "needs_review",
            "freight_tables": [],
            "freight_routes": [
                {
                    "destination": "Sao Paulo",
                    "weight_50": "120,00",
                    "weight_100": "180,00",
                }
            ],
            "reading_alerts": [],
            "evidence_refs": [],
        }
    )
    synthesized["freight_routes"] = []
    synthesized["temp_table_id"] = "sintetizada"
    synthesized["status"] = "needs_review"
    technical_route = normalize_partial_first_extraction_to_temp_table(
        {
            "status": "needs_review",
            "freight_tables": [],
            "freight_routes": [_route("Sao Paulo", "120,00")],
            "reading_alerts": [],
            "evidence_refs": [],
        }
    )
    technical_route["temp_table_id"] = "rota-tecnica"
    technical_route["status"] = "needs_review"

    assert entrega_faturavel(matrix) is True
    assert entrega_faturavel(routes_only) is True
    assert entrega_faturavel(synthesized) is True
    assert entrega_faturavel(technical_route) is True
    assert synthesized["freight_tables"][0]["table_type"] == "route_matrix_synthesized"
    assert build_pricing_contract(technical_route)["rules"] == []
