"""
Golden tests do motor unitário AgenteCompara.

Dois tipos de garantia convivem neste arquivo:

1. GOLDEN INDEPENDENTE (principal)
   Expected congelado manualmente, com memória de cálculo documentada.
   NÃO chama `_calculate_expected_freight_row`, `_audit_single_row`,
   `compute_audit_outputs` nem qualquer adapter do núcleo produtivo
   para obter o valor esperado.

2. PARIDADE COMPLEMENTAR COM AUDITORIA (secundário)
   Compara AgenteCompara.calculated_freight com Cleide.expected_freight.
   Não é oráculo matemático independente: ambos compartilham lógica
   equivalente. Serve apenas como sinal de paridade entre fluxos.
   Cleide é importado SOMENTE neste ambiente de teste.
"""
from __future__ import annotations

import copy
from decimal import Decimal, ROUND_HALF_UP
from types import SimpleNamespace

import pytest

from app.agente_compara_calculation_service import (
    STATUS_CALCULATED,
    SingleTableCalculationContext,
    calculate_single_table,
)
from app.cleide_audit_doc_service import compute_audit_outputs as cleide_compute_audit_outputs
from app.services.agente_compara_config_service import DEFAULT_CALCULATION_BASES

MONEY_TOLERANCE = 0.0


@pytest.fixture(autouse=True)
def _patch_calculation_bases(monkeypatch):
    """Evita I/O de ConfigRegras ao normalizar generalidades nos testes unitários."""
    cfg = SimpleNamespace(
        calculation_bases=copy.deepcopy(DEFAULT_CALCULATION_BASES),
        upload_ttl_hours=24,
    )
    monkeypatch.setattr("app.agente_compara_doc_service.get_agente_compara_config", lambda: cfg)


def _q(value) -> float:
    """HALF_UP em 2 casas — mesma regra monetária do motor."""
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _tax_inside(subtotal: float, rate: float) -> tuple[float, float]:
    """Imposto por dentro: total = subtotal / (1 - rate/100), HALF_UP."""
    sub = Decimal(str(subtotal))
    rate_d = Decimal(str(rate)) / Decimal("100")
    total = (sub / (Decimal("1") - rate_d)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    tax = (total - sub).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(tax), float(total)


def _base_record(*, accessorial_fees=None, freight_value=None, temp_table_id="tt_golden"):
    columns = ["Região de frete", "Até 30 kg", "31 a 50 kg", "Excedente kg"]
    row = {
        "Região de frete": "SP-Interior 1",
        "Até 30 kg": "87,13",
        "31 a 50 kg": "100,50",
        "Excedente kg": "2,00",
    }
    if freight_value is not None:
        columns.append("Frete Valor %")
        row["Frete Valor %"] = freight_value
    return {
        "temp_table_id": temp_table_id,
        "status": "needs_review",
        "freight_tables": [
            {
                "table_title": "Tabela golden",
                "table_type": "weight_range_table",
                "columns": columns,
                "rows": [row],
            }
        ],
        "freight_routes": [],
        "accessorial_fees": list(accessorial_fees or []),
    }


def _coverage_sp():
    return {
        "rows": [
            {
                "destination_uf": "SP",
                "destination_city": "Campinas",
                "freight_region": "SP-Interior 1",
            }
        ]
    }


def _coverage_rj():
    return {
        "rows": [
            {
                "destination_uf": "RJ",
                "destination_city": "Niterói",
                "freight_region": "SP-Interior 1",
            }
        ]
    }


def _tax_rj_12():
    return {
        "include_taxes": True,
        "origin_uf": "SP",
        "origin_city": "São Paulo",
        "iss_rate": None,
        "selected_table_ids": [],
        "destination_ufs": [{"uf": "RJ", "source": "manual", "evidence": []}],
        "icms_rates": [
            {
                "destination_uf": "RJ",
                "applied_rate": 12.0,
                "suggested_rate": 12.0,
                "is_active": True,
                "user_edited": False,
                "operation_type": "interstate",
            }
        ],
        "confirmed": True,
    }


def _gris_percent(value: str = "0,15%") -> dict:
    return {
        "name": "GRIS",
        "value": value,
        "unit": "%",
        "calculation_basis": "sobre nota fiscal",
        "notes": "",
    }


def _dispatch_fixed(amount: str = "R$ 12,00") -> dict:
    """Generalidade fixa resolvida via base configurada `por_cte`."""
    return {
        "name": "Despacho",
        "value": amount,
        "unit": "R$",
        "calculation_basis": "por CTe",
        "notes": "",
    }


def _gris_with_minimum(*, rate: str = "0,30%", minimum: float = 50.0) -> list[dict]:
    """GRIS percentual + mínimo vinculado (suporte real do núcleo)."""
    return [
        {
            "name": "GRIS",
            "value": rate,
            "unit": "%",
            "calculation_basis": "sobre nota fiscal",
            "notes": "",
        },
        {
            "name": "GRIS mínimo",
            "value": f"R$ {minimum:.2f}".replace(".", ","),
            "unit": "R$",
            "calculation_basis": "não mapeado / revisar",
            "notes": "",
        },
    ]


def _row(*, weight: float, invoice_value: float = 1000.0, uf: str = "SP", city: str = "Campinas", row_index: int = 1):
    return {
        "row_index": row_index,
        "document_number": f"G{row_index}",
        "destination_city": city,
        "destination_uf": uf,
        "audited_weight": weight,
        "invoice_value": invoice_value,
    }


def _run(record, row, *, tax=None, coverage=None):
    ctx = SingleTableCalculationContext(
        comparison_id="cmp_golden_independent",
        table_id="table_golden",
        temp_table_id=record.get("temp_table_id") or "tt_golden",
        slot_number=1,
        carrier_name="Transportadora Golden",
        table_record=copy.deepcopy(record),
        normalized_rows=[copy.deepcopy(row)],
        tax_config=copy.deepcopy(tax) if tax is not None else None,
        coverage_table=copy.deepcopy(coverage or _coverage_sp()),
    )
    return calculate_single_table(ctx)


def _assert_expected(result: dict, expected: dict):
    row = result["results"][0]
    assert row["status"] == STATUS_CALCULATED
    assert row["calculated_freight"] == pytest.approx(expected["total"], abs=MONEY_TOLERANCE)
    components = row["components"]
    assert components.get("weight_freight") == pytest.approx(expected["weight_freight"], abs=MONEY_TOLERANCE)
    if "freight_value_component" in expected:
        assert components.get("freight_value_component") == pytest.approx(
            expected["freight_value_component"], abs=MONEY_TOLERANCE
        )
    if "gris" in expected:
        assert components.get("gris") == pytest.approx(expected["gris"], abs=MONEY_TOLERANCE)
    if "dispatch" in expected:
        assert components.get("dispatch") == pytest.approx(expected["dispatch"], abs=MONEY_TOLERANCE)
    if "subtotal" in expected:
        assert components.get("subtotal") == pytest.approx(expected["subtotal"], abs=MONEY_TOLERANCE)
    if "taxes" in expected:
        assert components.get("taxes") == pytest.approx(expected["taxes"], abs=MONEY_TOLERANCE)
    assert components.get("total") == pytest.approx(expected["total"], abs=MONEY_TOLERANCE)


# ---------------------------------------------------------------------------
# 1) GOLDEN INDEPENDENTE — expected congelado manualmente
# ---------------------------------------------------------------------------


def test_golden_independent_weight_band_simple():
    """
    Memória:
      peso 20 kg → faixa Até 30 kg = 87,13
      subtotal = 87,13
      total = 87,13
    """
    expected = {
        "weight_freight": 87.13,
        "subtotal": 87.13,
        "total": 87.13,
    }
    result = _run(_base_record(), _row(weight=20))
    _assert_expected(result, expected)


def test_golden_independent_weight_band_limit():
    """
    Memória:
      peso 30 kg exatamente no limite da 1ª faixa = 87,13
    """
    expected = {
        "weight_freight": 87.13,
        "subtotal": 87.13,
        "total": 87.13,
    }
    result = _run(_base_record(), _row(weight=30))
    _assert_expected(result, expected)


def test_golden_independent_excess_weight():
    """
    Memória:
      peso 53 kg > última faixa (50)
      frete peso = 100,50 + (53-50)×2,00 = 100,50 + 6,00 = 106,50
    """
    expected = {
        "weight_freight": 106.50,
        "subtotal": 106.50,
        "total": 106.50,
    }
    result = _run(_base_record(), _row(weight=53))
    _assert_expected(result, expected)
    assert "excedente" in (result["results"][0]["evidence"].get("calculation_details") or "").lower()


def test_golden_independent_freight_value_percent():
    """
    Memória:
      peso 48 kg → faixa 31–50 = 100,50
      frete valor 0,10% × NF 1000 = 1,00
      subtotal = 101,50
      total = 101,50
    """
    expected = {
        "weight_freight": 100.50,
        "freight_value_component": 1.00,
        "subtotal": 101.50,
        "total": 101.50,
    }
    result = _run(_base_record(freight_value="0,10%"), _row(weight=48, invoice_value=1000))
    _assert_expected(result, expected)


def test_golden_independent_fixed_accessorial():
    """
    Memória:
      peso 48 kg → 100,50
      despacho fixo = 12,00
      subtotal = 112,50
      total = 112,50
    """
    expected = {
        "weight_freight": 100.50,
        "dispatch": 12.00,
        "subtotal": 112.50,
        "total": 112.50,
    }
    result = _run(_base_record(accessorial_fees=[_dispatch_fixed()]), _row(weight=48))
    _assert_expected(result, expected)


def test_golden_independent_percent_accessorial():
    """
    Memória:
      peso 48 kg → 100,50
      GRIS 0,15% × NF 1000 = 1,50
      subtotal = 102,00
      total = 102,00
    """
    expected = {
        "weight_freight": 100.50,
        "gris": 1.50,
        "subtotal": 102.00,
        "total": 102.00,
    }
    result = _run(_base_record(accessorial_fees=[_gris_percent()]), _row(weight=48, invoice_value=1000))
    _assert_expected(result, expected)


def test_golden_independent_tax_inside():
    """
    Memória (ICMS por dentro 12%):
      peso 48 kg → 100,50
      subtotal = 100,50
      total = 100,50 / (1 - 0,12) = 114,2045… → 114,20 (HALF_UP)
      imposto = 114,20 - 100,50 = 13,70
    """
    tax_amount, total = _tax_inside(100.50, 12.0)
    assert tax_amount == 13.70
    assert total == 114.20
    expected = {
        "weight_freight": 100.50,
        "subtotal": 100.50,
        "taxes": 13.70,
        "total": 114.20,
    }
    result = _run(
        _base_record(),
        _row(weight=48, uf="RJ", city="Niterói"),
        tax=_tax_rj_12(),
        coverage=_coverage_rj(),
    )
    _assert_expected(result, expected)
    assert result["results"][0]["components"].get("icms") == pytest.approx(13.70, abs=MONEY_TOLERANCE)


def test_golden_independent_minimum_accessorial_applied():
    """
    Memória (mínimo vinculado ao GRIS — suporte real do núcleo):
      peso 48 kg → 100,50
      GRIS 0,30% × NF 1000 = 3,00
      mínimo GRIS = 50,00 → mínimo aplicado
      subtotal = 100,50 + 50,00 = 150,50
      total = 150,50
    """
    expected = {
        "weight_freight": 100.50,
        "gris": 50.00,
        "subtotal": 150.50,
        "total": 150.50,
    }
    result = _run(
        _base_record(accessorial_fees=_gris_with_minimum()),
        _row(weight=48, invoice_value=1000),
    )
    _assert_expected(result, expected)


def test_golden_independent_combined_rich():
    """
    Memória combinada:
      peso 53 kg → 100,50 + 3×2,00 = 106,50
      frete valor 0,10% × 1000 = 1,00
      GRIS 0,15% × 1000 = 1,50
      despacho = 12,00
      subtotal = 106,50 + 1,00 + 1,50 + 12,00 = 121,00
      ICMS 12% por dentro:
        total = 121,00 / 0,88 = 137,50
        imposto = 16,50
    """
    subtotal = _q(106.50 + 1.00 + 1.50 + 12.00)
    assert subtotal == 121.00
    tax_amount, total = _tax_inside(subtotal, 12.0)
    assert tax_amount == 16.50
    assert total == 137.50
    expected = {
        "weight_freight": 106.50,
        "freight_value_component": 1.00,
        "gris": 1.50,
        "dispatch": 12.00,
        "subtotal": 121.00,
        "taxes": 16.50,
        "total": 137.50,
    }
    result = _run(
        _base_record(
            freight_value="0,10%",
            accessorial_fees=[_gris_percent(), _dispatch_fixed()],
        ),
        _row(weight=53, invoice_value=1000, uf="RJ", city="Niterói"),
        tax=_tax_rj_12(),
        coverage=_coverage_rj(),
    )
    _assert_expected(result, expected)
    row = result["results"][0]
    assert row["evidence"].get("freight_region") == "SP-Interior 1"
    assert row["evidence"].get("calculation_basis")


def test_golden_independent_rounding_half_up_edge():
    """
    Memória de arredondamento HALF_UP:
      peso 20 kg → 87,13
      GRIS 0,15% × NF 3333 = 4,9995 → 5,00 (HALF_UP)
      subtotal = 87,13 + 5,00 = 92,13
      total = 92,13
    """
    gris = _q(Decimal("3333") * Decimal("0.0015"))
    assert gris == 5.00
    expected = {
        "weight_freight": 87.13,
        "gris": 5.00,
        "subtotal": 92.13,
        "total": 92.13,
    }
    result = _run(
        _base_record(accessorial_fees=[_gris_percent()]),
        _row(weight=20, invoice_value=3333),
    )
    _assert_expected(result, expected)


def test_golden_independent_detects_adulterated_nucleus(monkeypatch):
    """
    Prova de independência: adulterar o retorno produtivo produz divergência
    em relação ao expected congelado. O teste NÃO fica vermelho — afirma a diferença.
    """
    from app import agente_compara_calculation_service as calc_mod

    expected_total = 87.13
    original = calc_mod._calculate_expected_freight_row

    def adulterated(*args, **kwargs):
        raw = original(*args, **kwargs)
        if isinstance(raw, dict) and raw.get("expected_freight") is not None:
            raw = dict(raw)
            raw["expected_freight"] = float(raw["expected_freight"]) + 1.00
            components = dict(raw.get("calculation_components") or {})
            if isinstance(components.get("weight_freight"), dict):
                wf = dict(components["weight_freight"])
                wf["amount"] = float(wf.get("amount") or 0) + 1.00
                components["weight_freight"] = wf
            raw["calculation_components"] = components
            raw["weight_freight"] = float(raw.get("weight_freight") or 0) + 1.00
        return raw

    monkeypatch.setattr(calc_mod, "_calculate_expected_freight_row", adulterated)
    result = _run(_base_record(), _row(weight=20))
    adulterated_total = result["results"][0]["calculated_freight"]
    assert adulterated_total == pytest.approx(expected_total + 1.00, abs=MONEY_TOLERANCE)
    assert adulterated_total != pytest.approx(expected_total, abs=MONEY_TOLERANCE)


# ---------------------------------------------------------------------------
# 2) PARIDADE COMPLEMENTAR COM AUDITORIA (não é oráculo independente)
# ---------------------------------------------------------------------------


def test_parity_complementary_with_auditoria_expected_freight():
    """
    Teste de paridade entre fluxos — NÃO usar como única garantia matemática.

    charged_freight = 0.01 existe apenas na fixture da referência histórica.
    """
    record = _base_record(
        freight_value="0,10%",
        accessorial_fees=[_gris_percent()],
        temp_table_id="tt_parity",
    )
    record["tax_config"] = _tax_rj_12()
    record["coverage_table"] = {
        "rows": _coverage_sp()["rows"] + _coverage_rj()["rows"],
    }
    rows_compara = [
        _row(weight=20, row_index=1),
        _row(weight=48, row_index=2),
        _row(weight=53, row_index=3),
        _row(weight=48, uf="RJ", city="Niterói", row_index=4),
    ]
    rows_auditoria = []
    for item in rows_compara:
        cloned = copy.deepcopy(item)
        cloned["charged_freight"] = 0.01  # sentinela apenas para o legado
        rows_auditoria.append(cloned)

    auditoria = {
        item["row_index"]: item
        for item in cleide_compute_audit_outputs(copy.deepcopy(record), rows_auditoria)["results"]
    }
    compara = calculate_single_table(
        SingleTableCalculationContext(
            comparison_id="cmp_parity",
            table_id="table_parity",
            temp_table_id="tt_parity",
            slot_number=1,
            carrier_name="Parity",
            table_record=copy.deepcopy(record),
            normalized_rows=copy.deepcopy(rows_compara),
            tax_config=copy.deepcopy(record["tax_config"]),
            coverage_table=copy.deepcopy(record["coverage_table"]),
        )
    )
    assert compara["calculated_count"] == 4
    for row in compara["results"]:
        ref = auditoria[row["row_index"]]
        assert row["calculated_freight"] == pytest.approx(float(ref["expected_freight"]), abs=MONEY_TOLERANCE)
