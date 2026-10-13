"""SCRUM-146 COMP-02: dimensão tarifária ``grupo`` sem alargar o reconhecimento de região."""
from __future__ import annotations

from app.agente_compara_doc_service import (
    _is_region_column,
    _selected_region_column,
    build_pricing_contract,
)
from app.agente_compara_temp_table_validation_service import (
    CODE_CONFLICTING_LOOKUP_KEY,
    validate_pricing_contract_for_confirmation,
)

_GROUPS = ("Norte", "Sul", "Leste", "Oeste")
_ORIGINS = (("Campinas", "SP"), ("Guarulhos", "SP"))


def _matrix_table(columns: list[str], rows: list[dict], **extra) -> dict:
    table = {
        "table_title": extra.pop("table_title", "Tabela por grupo"),
        "columns": columns,
        "rows": rows,
    }
    table.update(extra)
    return {"freight_tables": [table], "freight_routes": []}


def _priced_row(group: str, amount: str, *, origin_city: str | None = None, origin_uf: str | None = None, origem: str | None = None, extra: dict | None = None) -> dict:
    row = {
        "Grupo": group,
        "Até 10 kg": amount,
        "11 a 30 kg": "30,00",
        "Excedente kg": "1,50",
    }
    if origin_city is not None:
        row["Cidade origem"] = origin_city
    if origin_uf is not None:
        row["UF origem"] = origin_uf
    if origem is not None:
        row["Origem"] = origem
    if extra:
        row.update(extra)
    return row


def test_grupo_is_not_a_global_region_column():
    assert _is_region_column("Grupo") is False
    assert _is_region_column("Grupo comercial") is False
    assert _is_region_column("Região") is True
    assert _is_region_column("Zona") is True


def test_four_distinct_groups_share_one_supported_origin():
    rows = [
        _priced_row(group, f"{index + 1}0,00", origin_city="Campinas", origin_uf="SP")
        for index, group in enumerate(_GROUPS)
    ]
    table = _matrix_table(
        ["Cidade origem", "UF origem", "Grupo", "Até 10 kg", "11 a 30 kg", "Excedente kg"],
        rows,
    )
    contract = build_pricing_contract(table)
    validation = validate_pricing_contract_for_confirmation(table)

    assert len(contract["rules"]) == 4
    assert [rule["region"] for rule in contract["rules"]] == list(_GROUPS)
    assert [rule["lookup_keys"] for rule in contract["rules"]] == [[group] for group in _GROUPS]
    assert validation["can_confirm"] is True, validation["blocking_issues"]
    assert CODE_CONFLICTING_LOOKUP_KEY not in {issue["code"] for issue in validation["blocking_issues"]}
    assert _selected_region_column(table["freight_tables"][0]) == "Grupo"


def test_eight_rules_keep_explicitly_supported_origins_ranges_and_excess():
    rows = []
    for origin_index, (city, uf) in enumerate(_ORIGINS):
        for group_index, group in enumerate(_GROUPS):
            amount = f"{origin_index + 1}{group_index + 1},00"
            rows.append(_priced_row(group, amount, origin_city=city, origin_uf=uf))
    table = _matrix_table(
        ["Cidade origem", "UF origem", "Grupo", "Até 10 kg", "11 a 30 kg", "Excedente kg"],
        rows,
    )
    contract = build_pricing_contract(table)
    validation = validate_pricing_contract_for_confirmation(table)

    assert len(contract["rules"]) == 8
    assert validation["can_confirm"] is True, validation["blocking_issues"]
    lookup_keys = [key for rule in contract["rules"] for key in rule["lookup_keys"]]
    assert lookup_keys == [
        "ORIGIN:SP|CAMPINAS=>Norte",
        "ORIGIN:SP|CAMPINAS=>Sul",
        "ORIGIN:SP|CAMPINAS=>Leste",
        "ORIGIN:SP|CAMPINAS=>Oeste",
        "ORIGIN:SP|GUARULHOS=>Norte",
        "ORIGIN:SP|GUARULHOS=>Sul",
        "ORIGIN:SP|GUARULHOS=>Leste",
        "ORIGIN:SP|GUARULHOS=>Oeste",
    ]
    for rule in contract["rules"]:
        assert rule["pricing_type"] == "range_plus_excess_per_kg"
        assert [bracket["max_kg"] for bracket in rule["brackets"]] == [10.0, 30.0]
        assert rule["excess"] == {"rate_per_kg": 1.5}
        assert rule["unit"] == "kg"


def test_single_origem_column_keeps_conflicting_lookup_block():
    table = _matrix_table(
        ["Origem", "Grupo", "Até 10 kg", "11 a 30 kg", "Excedente kg"],
        [
            _priced_row("Capital", "10,00", origem="Campinas"),
            _priced_row("Capital", "20,00", origem="Guarulhos"),
        ],
        table_title="Tabela unica",
    )
    validation = validate_pricing_contract_for_confirmation(table)

    assert validation["can_confirm"] is False
    conflicts = [issue for issue in validation["blocking_issues"] if issue["code"] == CODE_CONFLICTING_LOOKUP_KEY]
    assert conflicts
    assert conflicts[0]["label"] == "Capital"
    assert conflicts[0]["message"] == "Há duas regras conflitantes para o mesmo destino."
    assert "Tabela unica" not in {issue["label"] for issue in conflicts}


def test_regiao_and_zona_precede_grupo_without_explicit_selection():
    regiao = _matrix_table(
        ["Grupo", "Região", "Até 10 kg"],
        [{"Grupo": "Norte", "Região": "Sul", "Até 10 kg": "10,00"}],
    )
    zona = _matrix_table(
        ["Grupo", "Zona", "Até 10 kg"],
        [{"Grupo": "Norte", "Zona": "P1", "Até 10 kg": "12,00"}],
    )
    both = _matrix_table(
        ["Região", "Zona", "Até 10 kg"],
        [{"Região": "Sul", "Zona": "P1", "Até 10 kg": "14,00"}],
    )

    assert _selected_region_column(regiao["freight_tables"][0]) == "Região"
    assert build_pricing_contract(regiao)["rules"][0]["lookup_keys"] == ["Sul"]
    assert _selected_region_column(zona["freight_tables"][0]) == "Zona"
    assert build_pricing_contract(zona)["rules"][0]["lookup_keys"] == ["P1"]
    assert _selected_region_column(both["freight_tables"][0]) == "Região"
    assert build_pricing_contract(both)["rules"][0]["lookup_keys"] == ["Sul"]


def test_explicit_dimension_selection_overrides_region_and_invalid_selection_falls_through():
    selected_group = _matrix_table(
        ["Região", "Grupo", "Até 10 kg", "11 a 30 kg", "Excedente kg"],
        [
            _priced_row("Norte", "10,00", extra={"Região": "Sul"}),
            _priced_row("Leste", "20,00", extra={"Região": "Sul"}),
        ],
        _selected_pricing_dimension_column="Grupo",
    )
    selected_zone = _matrix_table(
        ["Região", "Zona", "Até 10 kg"],
        [{"Região": "Sul", "Zona": "P1", "Até 10 kg": "14,00"}],
        _selected_pricing_dimension_column="Zona",
    )
    invalid_selection = _matrix_table(
        ["Grupo", "Até 10 kg"],
        [{"Grupo": "Norte", "Até 10 kg": "10,00"}],
        _selected_pricing_dimension_column="Coluna inexistente",
    )

    group_contract = build_pricing_contract(selected_group)
    assert _selected_region_column(selected_group["freight_tables"][0]) == "Grupo"
    assert [rule["lookup_keys"] for rule in group_contract["rules"]] == [["Norte"], ["Leste"]]
    assert validate_pricing_contract_for_confirmation(selected_group)["can_confirm"] is True
    assert group_contract["rules"][0]["pricing_type"] == "range_plus_excess_per_kg"
    assert group_contract["rules"][0]["excess"] == {"rate_per_kg": 1.5}

    assert _selected_region_column(selected_zone["freight_tables"][0]) == "Zona"
    assert build_pricing_contract(selected_zone)["rules"][0]["lookup_keys"] == ["P1"]

    assert _selected_region_column(invalid_selection["freight_tables"][0]) == "Grupo"
    assert build_pricing_contract(invalid_selection)["rules"][0]["lookup_keys"] == ["Norte"]


def test_commercial_group_header_is_not_treated_as_grupo():
    table = _matrix_table(
        ["Grupo comercial", "Até 10 kg"],
        [
            {"Grupo comercial": "Atacado", "Até 10 kg": "10,00"},
            {"Grupo comercial": "Varejo", "Até 10 kg": "20,00"},
        ],
        table_title="Tabela unica",
    )
    validation = validate_pricing_contract_for_confirmation(table)
    labels = {issue["label"] for issue in validation["blocking_issues"]}

    assert _selected_region_column(table["freight_tables"][0]) is None
    assert validation["can_confirm"] is False
    assert "Tabela unica" in labels
    assert "Atacado" not in labels
    assert "Varejo" not in labels
