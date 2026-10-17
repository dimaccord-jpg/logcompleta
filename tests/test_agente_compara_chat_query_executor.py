"""Executor analítico do chat do Agente Compara (SCRUM-238 / SCRUM-245).

Gabaritos saem de contas próprias sobre os fixtures. O retorno público é só
``comparison_chat_publication_v1``. Não se exige ``queries[].body`` nem fatos
privados.
"""
from __future__ import annotations

import copy
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid5

import pytest

from app.agente_compara_chat_contracts import AuthorizedComparisonScope, EvidenceReference
from app.agente_compara_chat_evidence_adapter import (
    EVIDENCE_NAMESPACE,
    _LossBook,
    _authorize_claims,
    _claims,
    evidence_authorizes_operation,
    evidence_digest_matches,
    publication_document_closed,
)
from app.agente_compara_chat_query_catalog import (
    CATALOG_VERSION,
    PUBLICATION_VERSION,
    WEIGHT_BANDS_V1,
    QueryCatalogError,
    QueryExecutionLimits,
    artifact_checksum,
    canonical_artifact_bytes,
    validate_query_plan,
)
from app.agente_compara_chat_query_executor import (
    authorize_analytical_projection,
    execute_analytical_plan,
)
import app.agente_compara_chat_query_executor as executor_module
from app.agente_compara_comparison_analytics_service import build_comparison_analytics

_ROOT = Path(__file__).resolve().parents[1]
_NEW_FILES = (
    _ROOT / "app" / "agente_compara_chat_query_catalog.py",
    _ROOT / "app" / "agente_compara_chat_query_executor.py",
    _ROOT / "app" / "agente_compara_chat_evidence_adapter.py",
)
_FORBIDDEN_IMPORTS = (
    "agente_compara_calculation_service",
    "agente_compara_calculation_execution_service",
    "agente_compara_calculation_result_storage",
    "agente_compara_comparison_calculation_service",
    "agente_compara_doc_service",
    "generativeai",
    "google.genai",
    "flask",
    "sqlalchemy",
    "subprocess",
)
_OPERATION = "22222222-2222-4222-8222-222222222222"
_OTHER_OPERATION = "33333333-3333-4333-8333-333333333333"
_COMPARISON = "11111111-1111-4111-8111-111111111111"
_UFS = ("SP", "RJ", "MG", "BA", "PR")
_PRE_BOUNDARY = frozenset(
    {
        "body",
        "evidence",
        "evidence_package",
        "table_id",
        "document_number",
        "carrier_name",
        "display_name",
        "message",
        "path",
        "filename",
        "signed_cursor",
        "cursor_contract",
    }
)


def _scope(result, memory=None, fingerprint="ab" * 32):
    payload = {
        "comparison_id": result["comparison_id"],
        "result_fingerprint": fingerprint,
        "result_checksum": artifact_checksum(result),
        "result_schema_version": result["schema_version"],
    }
    if memory is not None:
        payload["memory_checksum"] = artifact_checksum(memory)
        payload["memory_schema_version"] = memory["schema_version"]
    return AuthorizedComparisonScope(**payload)


def _project(result, memory=None, analytics=None, fingerprint="ab" * 32):
    return authorize_analytical_projection(
        operation_id=_OPERATION,
        scope=_scope(result, memory, fingerprint),
        result=result,
        memory_payload=memory,
        official_analytics=analytics,
    )


def _plan(queries):
    return {"catalog_version": CATALOG_VERSION, "queries": queries}


def _query(query_id, query_type, **extra):
    payload = {"query_id": query_id, "type": query_type}
    payload.update(extra)
    return payload


def _tables(count=2):
    names = ("Alpha", "Beta", "Gama")
    return [
        {"table_id": f"table-{index}", "slot_number": index, "carrier_name": names[index - 1]}
        for index in range(1, count + 1)
    ]


def _base_result(rows, tables=None):
    chosen = tables or _tables(2)
    return {
        "schema_version": 1,
        "comparison_id": _COMPARISON,
        "table_count": len(chosen),
        "tables": chosen,
        "row_count": len(rows),
        "comparative_rows": rows,
    }


def _cell(status, freight, *, include_components=True, components=None, **extra):
    payload = {
        "status": status,
        "final_status": status,
        "completeness_status": status,
        "calculated_freight": None if freight is None else float(freight),
        "is_partial_value": status == "incomplete",
        "warnings": extra.pop("warnings", []),
        "blocking_issues": extra.pop("blocking_issues", []),
        "error": extra.pop("error", None),
    }
    if include_components:
        payload["components"] = {} if components is None else components
    payload.update(extra)
    return payload


def _row(index, uf, weight, cells, document=None):
    return {
        "row_index": index,
        "document_number": document or f"DOC-{index}",
        "destination_uf": uf,
        "weight": weight,
        "table_results": cells,
    }


def _scale_spec(index):
    if index % 19 == 0:
        uf = "São Paulo"
    elif index % 17 == 0:
        uf = None
    else:
        uf = _UFS[(index - 1) % 5]
    if index % 23 == 0:
        weight = None
    elif index % 29 == 0:
        weight = 0
    elif index % 31 == 0:
        weight = -1
    elif index % 11 == 0:
        weight = 10
    elif index % 37 == 0:
        weight = 1000
    else:
        weight = index % 1000
    if index % 15 == 0:
        return uf, weight, ("incomplete", Decimal("1.00")), ("incomplete", Decimal("1.00"))
    if index % 13 == 0:
        return uf, weight, ("calculated", Decimal(index % 50)), ("missing_coverage_mapping", None)
    if index % 7 == 0:
        freight = Decimal(index % 50)
        return uf, weight, ("calculated_with_warnings", freight), ("calculated", freight)
    freight_a = Decimal(index % 50)
    extra = Decimal("0") if index % 3 == 0 else Decimal((index % 5) + 1)
    return uf, weight, ("calculated", freight_a), ("calculated", freight_a + extra)


def _scale_result(size):
    rows = []
    for index in range(1, size + 1):
        uf, weight, left, right = _scale_spec(index)
        cells = {}
        for table, spec in zip(_tables(2), (left, right)):
            kind, amount = spec
            if kind == "incomplete":
                built = _cell("incomplete", amount, error={"code": "incomplete", "message": "valor parcial"})
            elif kind == "missing_coverage_mapping":
                built = _cell(
                    "missing_coverage_mapping",
                    None,
                    error={"code": "missing_coverage_mapping", "message": "sem classificacao de cobertura"},
                )
            elif kind == "calculated_with_warnings":
                built = _cell(
                    "calculated_with_warnings",
                    amount,
                    components={"weight_freight": float(amount), "toll": 0, "total": float(amount)},
                    warnings=[{"code": "non_blocking_notice"}],
                )
            else:
                built = _cell(
                    "calculated",
                    amount,
                    components={"weight_freight": float(amount), "toll": 0, "total": float(amount)},
                )
            cells[table["table_id"]] = built
        rows.append(_row(index, uf, weight, cells))
    return _base_result(rows)


def _oracle_gap(left, right):
    if left[0] not in {"calculated", "calculated_with_warnings"}:
        return None
    if right[0] not in {"calculated", "calculated_with_warnings"}:
        return None
    amounts = sorted((left[1], right[1]))
    if amounts[0] == amounts[1]:
        return Decimal("0.00")
    return (amounts[1] - amounts[0]).quantize(Decimal("0.01"))


def _oracle_rank(size):
    ranked = []
    for index in range(1, size + 1):
        _, _, left, right = _scale_spec(index)
        gap = _oracle_gap(left, right)
        if gap is None:
            continue
        ranked.append((gap, index))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return ranked


def _oracle_weight_populations(size):
    numeric = 0
    comparable = 0
    for index in range(1, size + 1):
        _, weight, left, right = _scale_spec(index)
        if _oracle_band(weight) in {"absent", "invalid"}:
            continue
        numeric += 1
        if _oracle_gap(left, right) is not None:
            comparable += 1
    return numeric, comparable


def _oracle_band(weight):
    if weight is None:
        return "absent"
    if isinstance(weight, str) or weight < 0:
        return "invalid"
    amount = Decimal(weight)
    for lower, upper in WEIGHT_BANDS_V1:
        low = Decimal(lower)
        if upper is None and amount >= low:
            return f"{lower}:inf"
        if upper is not None and low <= amount < Decimal(upper):
            return f"{lower}:{upper}"
    return "invalid"


def _official_money(value):
    if value is None:
        return None, "absent"
    cents = int(round(float(value) * 100))
    text = f"{cents // 100}.{cents % 100:02d}"
    return text, "zero" if cents == 0 else "present"


def _query_map(result):
    return {item["query_id"]: item for item in result["queries"]}


def _run(result, queries, **kwargs):
    return execute_analytical_plan(_plan(queries), _project(result), **kwargs)


def _records(query, role):
    return [item for item in query["records"] if item["record_role"] == role]


def _component(query, row_index, slot, code):
    for record in _records(query, "detail"):
        facts = record["facts"]
        if facts["row_index"] == row_index and facts["carrier_slot"] == slot:
            return next(item for item in facts["components"] if item["code"] == code)
    raise AssertionError(f"missing component {row_index} {slot} {code}")


def _keys(value, found):
    if isinstance(value, dict):
        found.update(value)
        for item in value.values():
            _keys(item, found)
    elif isinstance(value, list):
        for item in value:
            _keys(item, found)


def _assert_boundary(executed):
    assert executed["publication_version"] == PUBLICATION_VERSION
    assert "body" not in executed
    assert "evidence_package" not in executed
    found = set()
    _keys(executed, found)
    assert found.isdisjoint(_PRE_BOUNDARY)
    if executed["context_text"]:
        public = {key: value for key, value in executed.items() if key not in {"context_text", "context_chars"}}
        assert json.loads(executed["context_text"]) == public
        assert executed["context_chars"] == len(executed["context_text"])
        publication_document_closed(executed)
    assert executed["context_chars"] <= 48_000
    _assert_claim_loss_invariant(executed)


def _assert_claim_loss_invariant(executed):
    """Nenhuma afirmação fica autorizada e bloqueada ao mesmo tempo."""

    global_blocked: set[str] = set()
    for loss in executed["losses"]:
        global_blocked.update(loss["blocks"])
    for query in executed["queries"]:
        blocked = set(global_blocked)
        for loss in query["losses"]:
            blocked.update(loss["blocks"])
        assert set(query["allowed_claims"]).isdisjoint(blocked)
    if executed["losses"] or any(query["losses"] for query in executed["queries"]):
        assert executed["evidence_sufficient"] is False


def _assert_absent(executed, *markers):
    rendered = json.dumps(executed, ensure_ascii=False)
    for marker in markers:
        assert marker not in rendered


class _SensitiveValue:
    def __str__(self) -> str:
        return "segredo-local"


def _official_json(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")


def _assert_controlled(call) -> QueryCatalogError:
    with pytest.raises(QueryCatalogError) as caught:
        call()
    assert "segredo-local" not in str(caught.value)
    assert "999999" not in str(caught.value)
    return caught.value


def _handcrafted_result():
    memory_components = [
        {"code": "WEIGHT_FREIGHT", "amount": 0, "applied": True},
        {"code": "TOLL", "amount": 0, "applied": True},
        {"code": "ACCESSORIAL", "canonical_component": "gris", "label": "Ad Valorem", "amount": 0, "applied": True},
        {"code": "ACCESSORIAL", "canonical_component": "ad_valorem", "label": "GRIS", "amount": 2, "applied": True},
    ]
    taxes = [
        {"code": "TAX", "tax_type": "ICMS", "amount": 0, "applied": True},
        {"code": "TAX", "tax_type": "ISS", "amount": 1.5, "applied": True},
    ]
    rows = [
        _row(1, "SP", 5, {
            "table-1": _cell("calculated", Decimal("10"), components={"total": 10, "weight_freight": 10}),
            "table-2": _cell("calculated", Decimal("20"), components={"total": 20, "weight_freight": 20}),
        }),
        _row(2, "SP", 0, {
            "table-1": _cell(
                "calculated",
                Decimal("0"),
                components={"gris": 2, "toll": 0, "total": 0},
                calculation_memory={
                    "status": "calculated",
                    "components": memory_components,
                    "taxes": taxes,
                    "subtotal_before_taxes": 0,
                    "total": 0,
                    "calculated_freight": 0,
                },
            ),
            "table-2": _cell("calculated", Decimal("0"), components={"total": 0, "weight_freight": 0, "toll": 0}),
        }),
        _row(3, None, None, {
            "table-1": _cell("calculated", Decimal("8"), components={"total": 8}),
            "table-2": _cell("calculated", Decimal("8"), components={"total": 8}),
        }),
        _row(4, "RJ", 12, {
            "table-1": _cell("calculated", Decimal("9"), components={"total": 9, "weight_freight": 9}),
            "table-2": _cell(
                "missing_coverage_mapping",
                None,
                error={"code": "missing_coverage_mapping", "message": "destino sem classificacao"},
            ),
        }),
        _row(5, "São Paulo", -1, {
            "table-1": _cell(
                "incomplete",
                Decimal("4"),
                components={"weight_freight": 4, "total": 4},
                error={"code": "incomplete", "message": "valor parcial"},
            ),
            "table-2": _cell(
                "calculated_with_warnings",
                Decimal("7"),
                components={"total": 7},
                warnings=[{"code": "notice"}],
            ),
        }),
        _row(6, "MG", 1000, {
            "table-1": _cell("calculated", Decimal("10"), components={"total": 10}),
            "table-2": _cell("calculated", Decimal("30"), components={"total": 30}),
        }),
        _row(7, "BA", 10, {
            "table-1": _cell(
                "calculated_with_warnings",
                Decimal("8"),
                components={"total": 8, "toll": 0},
                warnings=[{"code": "notice"}],
            ),
            "table-2": _cell("calculated", Decimal("8"), components={"total": 8, "toll": 0}),
        }),
        _row(8, "PR", "abc", {
            "table-1": _cell("invalid_weight", None, error={"code": "invalid_weight", "message": "peso invalido"}),
            "table-2": _cell(
                "ambiguous_coverage_mapping",
                None,
                error={"code": "ambiguous_coverage_mapping", "message": "classificacao ambigua"},
            ),
        }),
        _row(9, "BA", 50, {
            "table-1": _cell(
                "calculated",
                Decimal("15"),
                include_components=False,
                calculation_memory={"status": "calculated", "total": 15, "calculated_freight": 15},
            ),
            "table-2": _cell(
                "not_calculated",
                None,
                error={"code": "not_calculated", "message": "calculo nao realizado"},
                blocking_issues=[{"reason_code": "unsupported_accessorial_condition"}],
            ),
        }),
        _row(10, "SP", 30, {
            "table-1": _cell("calculated", Decimal("20"), components={"gris": 5, "weight_freight": 20, "total": 20}),
            "table-2": _cell(
                "invalid_invoice_value",
                None,
                error={"code": "invalid_invoice_value", "message": "nota invalida"},
            ),
        }),
        _row(11, "MG", 40, {
            "table-1": _cell(
                "calculated",
                Decimal("11"),
                calculation_memory={
                    "status": "calculated",
                    "components": [{"code": "ACCESSORIAL", "label": "GRIS", "amount": 4, "applied": True}],
                    "total": 11,
                    "calculated_freight": 11,
                },
            ),
            "table-2": _cell(
                "unsupported_pricing_model",
                None,
                error={"code": "unsupported_pricing_model", "message": "modelo incompativel"},
            ),
        }),
        _row(12, "RJ", 15, {
            "table-1": _cell("missing_freight_rule", None, error={"code": "missing_freight_rule", "message": "regra ausente"}),
            "table-2": _cell("calculated", Decimal("6"), components={"total": 6, "icms": 1, "iss": 0}),
        }),
    ]
    return _base_result(rows)


def test_catalog_rejects_malformed_sql_and_unknown_fields():
    with pytest.raises(QueryCatalogError) as empty:
        validate_query_plan({"catalog_version": "outro", "queries": [_query("q1", "official_summary")]})
    assert empty.value.code == "invalid_catalog_version"
    with pytest.raises(QueryCatalogError) as too_many:
        validate_query_plan(_plan([_query(f"q{index}", "official_summary") for index in range(5)]))
    assert too_many.value.code == "too_many_queries"
    with pytest.raises(QueryCatalogError) as unknown:
        validate_query_plan(_plan([_query("q1", "official_summary", surprise=True)]))
    assert unknown.value.code == "unknown_field"
    with pytest.raises(QueryCatalogError) as sql_key:
        validate_query_plan(_plan([_query("q1", "by_uf", filters={"sql": "select * from fretes"})]))
    assert sql_key.value.code == "forbidden_expression"
    with pytest.raises(QueryCatalogError) as page:
        validate_query_plan(_plan([_query("q1", "top_differences", page={"offset": -1, "limit": 10})]))
    assert page.value.code == "invalid_page"


def test_eight_queries_publish_closed_schemas_and_keep_the_gabarito():
    result = _handcrafted_result()
    original = copy.deepcopy(result)
    first = _run(result, [
        _query("summary", "official_summary"),
        _query("carriers", "carrier_comparison"),
        _query("gaps", "top_differences", page={"offset": 0, "limit": 10}),
    ])
    components_page = _run(result, [
        _query("components", "freight_components", filters={"row_indexes": [2, 9, 10, 11]}),
    ])
    second = _run(result, [
        _query("ufs", "by_uf"),
        _query("weights", "by_weight"),
    ])
    third = _run(result, [
        _query("partials", "incomplete_freights"),
        _query("diagnostics", "non_calculation_diagnostics"),
    ])
    assert result == original
    for executed in (first, components_page, second, third):
        _assert_boundary(executed)
        assert executed["analysis_complete"] is True
        _assert_absent(executed, "table-1", "table-2", "DOC-1", "DOC-6", "São Paulo", "Alpha", "destino sem classificacao")
    official = build_comparison_analytics(copy.deepcopy(result))
    summary = _records(_query_map(first)["summary"], "summary")[0]["facts"]
    savings, _state = _official_money(official["competitive_summary"]["total_potential_savings"])
    assert summary["total_rows"] == official["row_count"]
    assert summary["potential_savings_text"] == savings
    assert summary["differences_universe"] == "fully_comparable_rows"
    assert summary["tie_rule"] == "unique_lowest_amount"
    assert "table_id" not in json.dumps(summary)

    components = _query_map(components_page)["components"]
    gris = _component(components, 2, 1, "gris")
    assert gris["amount_text"] == "0.00"
    assert gris["amount_state"] == "zero"
    assert gris["availability"] == "preserved"
    assert gris["label"] == "GRIS"
    ad_valorem = _component(components, 2, 1, "ad_valorem")
    assert ad_valorem["amount_text"] == "2.00"
    assert ad_valorem["label"] == "Ad Valorem"
    assert _component(components, 2, 1, "toll")["amount_state"] == "zero"
    assert _component(components, 2, 1, "icms")["amount_text"] == "0.00"
    assert _component(components, 2, 1, "iss")["amount_text"] == "1.50"
    aggregate = _component(components, 10, 1, "gris")
    assert aggregate["availability"] == "aggregated_without_detail"
    assert aggregate["amount_text"] is None
    bucket = next(
        item["facts"]["buckets"]
        for item in _records(components, "detail")
        if item["facts"]["row_index"] == 10 and item["facts"]["carrier_slot"] == 1
    )
    assert bucket[0]["amount_text"] == "5.00"
    assert _component(components, 11, 1, "gris")["availability"] == "absent"
    assert _component(components, 11, 1, "accessorial")["amount_text"] == "4.00"
    assert _component(components, 9, 1, "weight_freight")["availability"] == "unavailable"
    assert _component(components, 9, 1, "total")["amount_text"] == "15.00"
    assert "component_value" in components["allowed_claims"]

    gaps = _query_map(first)["gaps"]
    assert gaps["page"]["total"] == 5
    details = [item["facts"] for item in _records(gaps, "detail")]
    assert [item["row_index"] for item in details] == [6, 1, 2, 3, 7]
    assert details[0]["absolute_gap_text"] == "20.00"
    assert details[2]["tie"] is True
    assert _records(gaps, "summary")[0]["facts"]["gap_total_text"] == "30.00"
    assert "page_ranking" in gaps["allowed_claims"]
    carrier_facts = _records(_query_map(first)["carriers"], "summary")[0]["facts"]
    winners = [item for item in carrier_facts["carriers"] if item["wins"]]
    if (
        carrier_facts["comparable_row_count"]
        and carrier_facts["tie_rule"]
        and carrier_facts["universe"]
        and len(winners) == 1
        and carrier_facts["tie_count"] is not None
    ):
        assert "bounded_best_freight" in _query_map(first)["carriers"]["allowed_claims"]
    assert carrier_facts["global_superiority"] is False
    assert carrier_facts["indicator_source"] == "official_analytics"
    assert {item["carrier_slot"] for item in carrier_facts["carriers"]} == {1, 2}

    partials = _query_map(third)["partials"]
    partial_rows = [item["facts"] for item in _records(partials, "detail")]
    assert [item["row_index"] for item in partial_rows] == [5]
    assert partial_rows[0]["cells"][0]["final_amount_state"] == "absent"
    assert partial_rows[0]["cells"][0]["partial_amount_text"] == "4.00"
    assert _records(partials, "summary")[0]["facts"]["excluded_complete_with_warnings"] == 1
    assert "partial_freight" in partials["allowed_claims"]

    diagnostics = _query_map(third)["diagnostics"]
    codes = {item["code"] for item in _records(diagnostics, "summary")[0]["facts"]["by_code"]}
    assert codes == {
        "missing_coverage_mapping",
        "ambiguous_coverage_mapping",
        "missing_freight_rule",
        "invalid_weight",
        "invalid_invoice_value",
        "unsupported_pricing_model",
        "not_calculated",
    }
    assert _records(diagnostics, "summary")[0]["facts"]["unmapped_code_occurrences"] >= 1
    _assert_absent(third, "unsupported_accessorial_condition", "regra ausente", "nota invalida")
    assert "official_diagnostic" in diagnostics["allowed_claims"]
    coverage = next(
        cell
        for record in _records(diagnostics, "detail")
        if record["facts"]["row_index"] == 4
        for cell in record["facts"]["cells"]
        if cell["carrier_slot"] == 2
    )
    assert coverage["official_code"] == "missing_coverage_mapping"
    assert "route_not_served" in coverage["must_not_assert"]

    ufs = _query_map(second)["ufs"]
    groups = {item["facts"]["group_id"]: item["facts"] for item in _records(ufs, "detail")}
    assert "São Paulo" not in groups
    absent = next(item["facts"] for item in _records(ufs, "special_group") if item["facts"]["group_id"] == "uf_absent")
    unusable = next(item["facts"] for item in _records(ufs, "special_group") if item["facts"]["group_id"] == "uf_unusable")
    assert absent["row_count"] == 1
    assert unusable["row_count"] == 1
    assert _records(ufs, "summary")[0]["facts"]["aligned"] is False

    weights = _query_map(second)["weights"]
    bands = {item["facts"]["group_id"]: item["facts"] for item in _records(weights, "detail")}
    assert bands["10:30"]["row_count"] == 3
    assert bands["1000:inf"]["row_count"] == 1
    assert bands["1000:inf"]["last_band"] is True
    absent_weight = next(item["facts"] for item in _records(weights, "special_group") if item["facts"]["group_id"] == "weight_absent")
    invalid_weight = next(item["facts"] for item in _records(weights, "special_group") if item["facts"]["group_id"] == "weight_invalid")
    assert absent_weight["row_count"] == 1
    assert invalid_weight["row_count"] == 2
    assert _records(weights, "summary")[0]["facts"]["last_band_rule"] == "lower_inclusive_without_upper_bound"


def _assert_population(size):
    result = _scale_result(size)
    official = build_comparison_analytics(copy.deepcopy(result))
    executed = _run(result, [
        _query("summary", "official_summary"),
        _query("gaps", "top_differences", page={"offset": 50, "limit": 50}),
        _query("ufs", "by_uf"),
        _query("weights", "by_weight"),
    ])
    _assert_boundary(executed)
    assert executed["population"]["examined_rows"] == size
    summary = _records(_query_map(executed)["summary"], "summary")[0]["facts"]
    savings, _state = _official_money(official["competitive_summary"]["total_potential_savings"])
    assert summary["total_rows"] == size
    assert summary["potential_savings_text"] == savings
    weight_text, weight_state = _official_money(official["global_summary"]["total_weight"])
    invoice_text, invoice_state = _official_money(official["global_summary"]["total_invoice_value"])
    assert summary["total_weight_text"] == weight_text
    assert summary["total_weight_state"] == weight_state
    assert summary["total_invoice_text"] == invoice_text
    assert summary["total_invoice_state"] == invoice_state
    ranked = _oracle_rank(size)
    gap_total = sum((gap for gap, _index in ranked), Decimal("0.00")).quantize(Decimal("0.01"))
    gaps = _query_map(executed)["gaps"]
    assert gaps["page"]["total"] == len(ranked)
    assert _records(gaps, "summary")[0]["facts"]["gap_total_text"] == f"{gap_total:.2f}"
    if len(ranked) > 50 and gaps["evidence_sufficient"]:
        details = [item["facts"] for item in _records(gaps, "detail")]
        assert details[0]["row_index"] == ranked[50][1]
        assert details[0]["absolute_gap_text"] == f"{ranked[50][0]:.2f}"
    expected_ufs = {"SP": 0, "RJ": 0, "MG": 0, "BA": 0, "PR": 0}
    absent = unusable = weight_absent = weight_invalid = 0
    bands = {f"{lower}:{upper or 'inf'}": 0 for lower, upper in WEIGHT_BANDS_V1}
    for index in range(1, size + 1):
        uf, weight, _left, _right = _scale_spec(index)
        if uf is None:
            absent += 1
        elif uf in expected_ufs:
            expected_ufs[uf] += 1
        else:
            unusable += 1
        band = _oracle_band(weight)
        if band == "absent":
            weight_absent += 1
        elif band == "invalid":
            weight_invalid += 1
        else:
            bands[band] += 1
    uf_query = _query_map(executed)["ufs"]
    uf_summary = _records(uf_query, "summary")[0]["facts"]
    uf_counts = {item["group_id"]: item["row_count"] for item in uf_summary["group_counts"]}
    assert uf_counts == expected_ufs
    assert uf_summary["absent_rows"] == absent
    assert uf_summary["unusable_rows"] == unusable
    for record in _records(uf_query, "detail"):
        assert record["facts"]["row_count"] == expected_ufs[record["facts"]["group_id"]]
    for record in _records(uf_query, "special_group"):
        expected_special = absent if record["facts"]["group_id"] == "uf_absent" else unusable
        assert record["facts"]["row_count"] == expected_special
    weight_query = _query_map(executed)["weights"]
    weight_summary = _records(weight_query, "summary")[0]["facts"]
    weight_counts = {item["group_id"]: item["row_count"] for item in weight_summary["band_counts"]}
    assert weight_counts == bands
    assert weight_summary["absent_rows"] == weight_absent
    assert weight_summary["invalid_rows"] == weight_invalid
    for record in _records(weight_query, "detail"):
        assert record["facts"]["row_count"] == bands[record["facts"]["group_id"]]
        if record["facts"]["group_id"] == "1000:inf":
            assert record["facts"]["last_band"] is True
    numeric_weight, comparable_weight = _oracle_weight_populations(size)
    weight_summary = _records(weight_query, "summary")[0]["facts"]
    assert weight_summary["numeric_weight_rows"] == numeric_weight
    assert weight_summary["comparable_rows"] == comparable_weight
    assert weight_query["population"]["eligible_rows"] == numeric_weight
    assert weight_query["population"]["comparable_rows"] == comparable_weight
    if executed["evidence_omitted_count"]:
        assert executed["evidence_sufficient"] is False
        assert executed["publication_complete"] is False


@pytest.mark.parametrize("size", [27, 240, 2000])
def test_scale_aggregates_match_independent_oracle(size):
    _assert_population(size)


def test_zero_partial_and_unavailable_stay_distinct():
    result = _scale_result(60)
    executed = _run(result, [
        _query("components", "freight_components", filters={"row_indexes": [50], "component_codes": ["toll", "weight_freight"]}),
        _query("partials", "incomplete_freights"),
    ])
    _assert_boundary(executed)
    component = _component(_query_map(executed)["components"], 50, 1, "weight_freight")
    assert component["amount_text"] == "0.00"
    assert component["amount_state"] == "zero"
    toll = _component(_query_map(executed)["components"], 50, 1, "toll")
    assert toll["amount_state"] == "zero"
    assert toll["amount_state"] != "absent"
    partials = _records(_query_map(executed)["partials"], "detail")
    assert partials
    assert all(cell["completeness"] == "partial" for item in partials for cell in item["facts"]["cells"])
    assert "aggregate_total" not in _query_map(executed)["partials"]["allowed_claims"]
    assert "partial_freight" in _query_map(executed)["partials"]["allowed_claims"]


def test_subset_comparison_does_not_reuse_the_broader_universe():
    tables = _tables(3)
    rows = [
        _row(1, "SP", 10, {
            "table-1": _cell("calculated", Decimal("10")),
            "table-2": _cell("calculated", Decimal("30")),
            "table-3": _cell("calculated", Decimal("40")),
        }),
        _row(2, "RJ", 10, {
            "table-1": _cell("calculated", Decimal("10")),
            "table-2": _cell("calculated", Decimal("10")),
            "table-3": _cell("missing_coverage_mapping", None, error={"code": "missing_coverage_mapping"}),
        }),
    ]
    result = _base_result(rows, tables)
    full = _run(result, [_query("full", "carrier_comparison"), _query("gaps", "top_differences")])
    subset = _run(result, [_query("subset", "carrier_comparison", filters={"table_ids": ["table-1", "table-2"]})])
    gap = _records(_query_map(full)["gaps"], "detail")[0]["facts"]
    assert gap["absolute_gap_text"] == "20.00"
    full_facts = _records(_query_map(full)["full"], "summary")[0]["facts"]
    subset_facts = _records(_query_map(subset)["subset"], "summary")[0]["facts"]
    assert full_facts["indicator_source"] == "official_analytics"
    assert full_facts["global_superiority"] is False
    assert subset_facts["indicator_source"] == "selected_tables_fully_comparable"
    assert subset_facts["universe"] == "selected_tables_fully_comparable"
    assert subset_facts["potential_savings_text"] == "20.00"
    assert subset_facts["partially_comparable_rows"] == 0
    assert subset["queries"][0]["selection"]["carrier_slots"] == [1, 2]
    _assert_absent(subset, "table-1", "table-2", "table-3")


def test_sensitive_markers_do_not_cross_the_boundary():
    secret = "super-secret-token"
    password = "password=hunter2"
    windows = r"C:\Users\secret\tabela.xlsx"
    unix = "/etc/passwd"
    unc = r"\\fileserver\share\fretes.xlsx"
    phrase = r"veja C:\temp\oculto.txt agora"
    document = "DOC-998877"
    carrier = "carrier-secret-name"
    unknown = "unmapped_code_zxq"
    nested = "sk-live-abc"
    long_text = "A" * 5000
    control = "frete\u0000oculto\u202e"
    tables = [
        {"table_id": windows, "slot_number": 1, "carrier_name": carrier},
        {"table_id": unc, "slot_number": 2, "carrier_name": phrase},
    ]
    rows = [
        _row(1, "SP", 10, {
            windows: _cell(
                "calculated",
                Decimal("10"),
                components={
                    "total": 10,
                    "Total": 99,
                    "total ": 77,
                    "weight_freight": 10,
                    "api_key": {nested: secret},
                    "note": long_text,
                },
                error={"code": "calculated", "message": password},
            ),
            unc: _cell(
                "not_calculated",
                None,
                error={"code": "not_calculated", "message": f"{unix} {phrase}"},
                blocking_issues=[{"reason_code": unknown, "message": control}],
            ),
        }, document=document),
    ]
    executed = _run(_base_result(rows, tables), [
        _query("components", "freight_components"),
        _query("diagnostics", "non_calculation_diagnostics"),
        _query("summary", "official_summary"),
    ])
    _assert_boundary(executed)
    _assert_absent(
        executed,
        secret,
        password,
        windows,
        unix,
        unc,
        phrase,
        document,
        carrier,
        unknown,
        nested,
        long_text,
        control,
        "oculto",
    )
    total = _component(_query_map(executed)["components"], 1, 1, "total")
    assert total["amount_text"] == "10.00"
    assert _records(_query_map(executed)["diagnostics"], "summary")[0]["facts"]["unmapped_code_occurrences"] >= 1


def test_private_key_does_not_change_public_facts_and_amount_does():
    left = _base_result([
        _row(1, "SP", 10, {
            "table-1": _cell("calculated", Decimal("10"), components={"total": 10}),
            "table-2": _cell("calculated", Decimal("12"), components={"total": 12}),
        }, document="DOC-PRIVADO"),
    ])
    right = copy.deepcopy(left)
    right["comparative_rows"][0]["document_number"] = "OUTRO-DOC"
    right["comparative_rows"][0]["table_results"]["table-1"]["components"]["surprise"] = "segredo-aninhado"
    first = _run(left, [_query("gaps", "top_differences")])
    second = _run(right, [_query("gaps", "top_differences")])
    def facts(executed):
        return [record["facts"] for record in executed["queries"][0]["records"]]
    assert facts(first) == facts(second)
    _assert_absent(first, "DOC-PRIVADO")
    _assert_absent(second, "OUTRO-DOC", "segredo-aninhado")
    changed = copy.deepcopy(left)
    changed["comparative_rows"][0]["table_results"]["table-2"]["calculated_freight"] = 30.0
    changed["comparative_rows"][0]["table_results"]["table-2"]["components"]["total"] = 30
    third = _run(changed, [_query("gaps", "top_differences")])
    assert facts(first) != facts(third)
    assert _records(_query_map(third)["gaps"], "detail")[0]["facts"]["absolute_gap_text"] == "20.00"
    assert third["expected_result_version"] != first["expected_result_version"]


def test_pagination_is_deterministic_and_over_limit_is_not_sufficient():
    result = _handcrafted_result()
    first = _run(result, [_query("gaps", "top_differences", page={"offset": 1, "limit": 2})])
    second = _run(result, [_query("gaps", "top_differences", page={"offset": 1, "limit": 2})])
    ids_first = [item["evidence_id"] for item in first["queries"][0]["records"]]
    ids_second = [item["evidence_id"] for item in second["queries"][0]["records"]]
    assert ids_first == ids_second
    details = [item["facts"]["row_index"] for item in _records(first["queries"][0], "detail")]
    assert details == [1, 2]
    assert first["queries"][0]["page"]["total"] == 5
    assert first["queries"][0]["page"]["returned"] == 2
    wide = _scale_result(60)
    crowded = _run(wide, [_query("components", "freight_components", page={"offset": 0, "limit": 50})])
    assert crowded["evidence_produced_count"] > crowded["evidence_delivered_count"]
    assert crowded["evidence_omitted_count"] > 0
    assert crowded["evidence_sufficient"] is False
    assert crowded["publication_complete"] is False
    assert crowded["status"] == "partial"


def test_evidence_identity_changes_with_content_and_rejects_foreign_binding():
    result = _scale_result(2)
    scope = _scope(result)
    executed = execute_analytical_plan(_plan([_query("summary", "official_summary")]), _project(result))
    record = executed["queries"][0]["records"][0]
    assert evidence_digest_matches(record)
    reference = EvidenceReference(
        evidence_id=record["evidence_id"],
        evidence_digest=record["evidence_digest"],
        operation_id=record["operation_id"],
        comparison_id=record["comparison_id"],
        expected_result_version=record["expected_result_version"],
        contract_version=record["contract_version"],
        memory_checksum=record["memory_checksum"],
        memory_schema_version=record["memory_schema_version"],
    )
    assert evidence_authorizes_operation(
        reference,
        operation_id=_OPERATION,
        comparison_id=record["comparison_id"],
        expected_result_version=record["expected_result_version"],
        scope=scope,
    )
    assert evidence_authorizes_operation(
        reference,
        operation_id=_OTHER_OPERATION,
        comparison_id=record["comparison_id"],
        expected_result_version=record["expected_result_version"],
        scope=scope,
    ) is False
    mutated = copy.deepcopy(record)
    mutated["facts"] = copy.deepcopy(record["facts"])
    mutated["facts"]["total_rows"] = 999
    mutated.pop("evidence_digest")
    mutated.pop("evidence_id")
    assert artifact_checksum(mutated) != record["evidence_digest"]
    digest_name = "|".join((
        PUBLICATION_VERSION,
        record["operation_id"],
        record["comparison_id"],
        record["expected_result_version"],
        record["schema_id"],
        record["query_id"],
        str(record["ordinal"]),
        record["evidence_digest"],
    ))
    assert record["evidence_id"] == str(uuid5(EVIDENCE_NAMESPACE, digest_name))
    assert uuid5(EVIDENCE_NAMESPACE, "table-1") != UUID(record["evidence_id"])


def test_snapshot_and_memory_swaps_are_rejected():
    result = _scale_result(2)
    projection = _project(result)
    projection.result["comparative_rows"][0]["destination_uf"] = "AM"
    with pytest.raises(QueryCatalogError) as mutated:
        execute_analytical_plan(_plan([_query("ufs", "by_uf")]), projection)
    assert mutated.value.code == "snapshot_mismatch"
    caller = copy.deepcopy(result)
    official = build_comparison_analytics(copy.deepcopy(caller))
    analytics_projection = _project(caller, analytics=official)
    analytics_projection.official_analytics["row_count"] = 0
    with pytest.raises(QueryCatalogError) as analytics_mutated:
        execute_analytical_plan(_plan([_query("summary", "official_summary")]), analytics_projection)
    assert analytics_mutated.value.code == "snapshot_mismatch"
    tampered = copy.deepcopy(official)
    tampered["tables"][0]["calculated_freight_total"] = 999999
    error = _assert_controlled(lambda: _project(result, analytics=tampered))
    assert error.code == "analytics_provenance_mismatch"


def test_mutation_before_during_and_after_execution():
    caller = _base_result([
        _row(1, "SP", 10, {
            "table-1": _cell("calculated", Decimal("1.00"), components={"total": 1}),
            "table-2": _cell("calculated", Decimal("3.00"), components={"total": 3}),
        }),
    ])
    untouched = copy.deepcopy(caller)
    projection = _project(caller)
    cell = projection.result["comparative_rows"][0]["table_results"]["table-1"]
    original = cell["calculated_freight"]
    calls = {"n": 0}

    def clock():
        calls["n"] += 1
        cell["calculated_freight"] = 999 if calls["n"] == 1 else original
        return 0

    executed = execute_analytical_plan(_plan([_query("gaps", "top_differences")]), projection, clock=clock)
    assert caller == untouched
    assert cell["calculated_freight"] == original
    rendered = json.dumps(executed)
    assert "999" not in rendered
    amounts = {
        item["amount_text"]
        for item in _records(executed["queries"][0], "detail")[0]["facts"]["freights"]
    }
    assert amounts == {"1.00", "3.00"}
    before = json.loads(executed["context_text"])
    executed["queries"][0]["records"][0]["facts"]["gap_total_text"] = "0.00"
    assert json.loads(executed["context_text"]) == before


def test_mutation_while_the_private_copy_is_created_is_rejected(monkeypatch):
    caller = _scale_result(2)
    untouched = copy.deepcopy(caller)
    projection = _project(caller)
    cell = projection.result["comparative_rows"][0]["table_results"]["table-1"]
    original = cell["calculated_freight"]
    real_deepcopy = executor_module.copy.deepcopy

    def flipping(obj, memo=None):
        if obj is projection.result:
            cell["calculated_freight"] = 999
            try:
                return real_deepcopy(obj, memo)
            finally:
                cell["calculated_freight"] = original
        return real_deepcopy(obj, memo)

    monkeypatch.setattr(executor_module.copy, "deepcopy", flipping)
    error = _assert_controlled(
        lambda: execute_analytical_plan(_plan([_query("summary", "official_summary")]), projection)
    )
    assert error.code == "projection_mutated"
    assert caller == untouched


def test_context_truncation_is_explicit(monkeypatch):
    result = _scale_result(4)
    executed = execute_analytical_plan(
        _plan([_query("summary", "official_summary")]),
        _project(result),
        limits=QueryExecutionLimits(max_context_chars=80),
    )
    assert executed["analysis_complete"] is True
    assert executed["evidence_sufficient"] is False
    assert executed["publication_complete"] is False
    assert executed["error"]["code"] == "context_limit_exceeded"
    assert executed["status"] == "error"
    assert executed["context_chars"] <= 80
    assert executed["queries"] == []


def test_exceptions_do_not_expose_internal_data(monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError(r"C:\secret\boom password=hunter2")

    monkeypatch.setattr(executor_module, "_freight_components", boom)
    executed = _run(_scale_result(2), [_query("components", "freight_components")])
    assert executed["queries"][0]["status"] == "error"
    assert executed["queries"][0]["error"]["code"] == "execution_failed"
    assert executed["queries"][0]["records"] == []
    _assert_absent(executed, r"C:\secret\boom", "password=hunter2", "RuntimeError")
    assert executed["evidence_sufficient"] is False


def test_population_limit_fails_closed():
    executed = execute_analytical_plan(
        _plan([_query("summary", "official_summary")]),
        _project(_scale_result(11)),
        limits=QueryExecutionLimits(max_population=10),
    )
    assert executed["status"] == "error"
    assert executed["error"]["code"] == "population_limit_exceeded"
    assert executed["analysis_complete"] is False
    assert executed["evidence_sufficient"] is False
    assert executed["queries"] == []


def test_weight_denominators_and_excluded_uf_money_stay_separate():
    rows = [
        _row(1, "SP", 5, {
            "table-1": _cell("calculated", Decimal("10"), components={"total": 10}),
            "table-2": _cell("calculated", Decimal("12"), components={"total": 12}),
        }),
        _row(2, "SP", 12, {
            "table-1": _cell("calculated", Decimal("8"), components={"total": 8}),
            "table-2": _cell("incomplete", Decimal("4"), components={"total": 4}),
        }),
        _row(3, "RJ", None, {
            "table-1": _cell("calculated", Decimal("9"), components={"total": 9}),
            "table-2": _cell("calculated", Decimal("9"), components={"total": 9}),
        }),
        _row(4, "MG", -1, {
            "table-1": _cell("calculated", Decimal("7"), components={"total": 7}),
            "table-2": _cell("not_calculated", None),
        }),
        _row(5, "BA", "abc", {
            "table-1": _cell("invalid_weight", None),
            "table-2": _cell("invalid_weight", None),
        }),
    ]
    executed = _run(_base_result(rows), [_query("weights", "by_weight")])
    summary = _records(_query_map(executed)["weights"], "summary")[0]["facts"]
    assert summary["examined_rows"] == 5
    assert summary["numeric_weight_rows"] == 2
    assert summary["complete_freight_rows"] == 1
    assert summary["complete_freight_count"] == 3
    assert summary["comparable_rows"] == 1
    assert summary["excluded_invalid_or_absent_weight"] == 3
    assert summary["excluded_weight_reference"] == "examined_rows"
    assert summary["excluded_not_comparable_rows"] == 1
    assert summary["excluded_comparability_reference"] == "numeric_weight_rows"
    band = next(item["facts"] for item in _records(_query_map(executed)["weights"], "detail") if item["facts"]["group_id"] == "0:10")
    assert band["row_count"] == 1
    assert band["comparable_row_count"] == 1

    absent_rows = [
        _row(1, None, 10, {
            "table-1": _cell("calculated", Decimal("10"), components={"total": 10}),
            "table-2": _cell("calculated", Decimal("14"), components={"total": 14}),
        }),
        _row(2, "SP", 10, {
            "table-1": _cell("calculated", Decimal("3"), components={"total": 3}),
            "table-2": _cell("calculated", Decimal("3"), components={"total": 3}),
        }),
    ]
    hidden = _run(_base_result(absent_rows), [_query("ufs", "by_uf", filters={"ufs": ["SP"], "include_absent": False, "include_unusable": False})])
    rendered = json.dumps(_query_map(hidden)["ufs"]["records"])
    assert "14.00" not in rendered
    assert "4.00" not in rendered
    assert all(item["facts"].get("group_id") != "uf_absent" or item["facts"]["monetary_values_published"] is False for item in hidden["queries"][0]["records"])
    shown = _run(_base_result(absent_rows), [_query("ufs", "by_uf", filters={"include_absent": True, "include_unusable": False})])
    special = next(item["facts"] for item in _records(_query_map(shown)["ufs"], "special_group") if item["facts"]["group_id"] == "uf_absent")
    assert special["monetary_values_published"] is True
    assert special["potential_savings_text"] == "4.00"
    assert special["row_count"] == 1


def test_decimal_freight_is_unsupported_and_not_zero():
    rows = [
        _row(1, "SP", 10, {
            "table-1": _cell("calculated", None, components={"total": Decimal("1.50")}),
            "table-2": _cell("calculated", Decimal("2"), components={"total": 2}),
        }),
    ]
    rows[0]["table_results"]["table-1"]["calculated_freight"] = Decimal("1.50")
    executed = _run(_base_result(rows), [_query("components", "freight_components")])
    detail = next(item["facts"] for item in _records(_query_map(executed)["components"], "detail") if item["facts"]["carrier_slot"] == 1)
    assert detail["final_amount_state"] == "absent"
    assert detail["amount_support"] == "unsupported_decimal"
    assert detail["final_amount_text"] is None
    total = next(item for item in detail["components"] if item["code"] == "total")
    assert total["amount_text"] is None
    assert total["amount_state"] != "zero"
    supported = next(item["facts"] for item in _records(_query_map(executed)["components"], "detail") if item["facts"]["carrier_slot"] == 2)
    assert supported["final_amount_text"] == "2.00"
    assert "aggregate_total" in _query_map(executed)["components"]["allowed_claims"]


def test_compact_catalog_does_not_rebuild_discarded_components():
    result = _base_result([
        _row(1, "SP", 10, {
            "table-1": {
                "table_id": "table-1",
                "carrier_name": "Alpha",
                "slot_number": 1,
                "calculated_freight": 10.0,
                "status": "calculated",
                "final_status": "calculated",
                "completeness_status": "calculated",
                "is_partial_value": False,
                "memory_ref": "table-1:1",
            },
            "table-2": {
                "table_id": "table-2",
                "carrier_name": "Beta",
                "slot_number": 2,
                "calculated_freight": 12.0,
                "status": "calculated",
                "final_status": "calculated",
                "completeness_status": "calculated",
                "is_partial_value": False,
                "memory_ref": "table-2:1",
            },
        }),
    ])
    result["schema_version"] = 2
    memory = {
        "schema_version": 3,
        "comparison_id": _COMPARISON,
        "components": {"cmp:1": {"weight_freight": 10, "gris": 5, "total": 10}},
        "items": {
            "table-1:1": {
                "component_ref": "cmp:1",
                "calculation_memory": {"status": "calculated", "total": 10, "calculated_freight": 10},
            },
            "table-2:1": {
                "component_ref": "cmp:missing",
                "calculation_memory": {"status": "calculated", "total": 12, "calculated_freight": 12},
            },
        },
    }
    original_memory = copy.deepcopy(memory)
    executed = execute_analytical_plan(
        _plan([_query("components", "freight_components")]),
        _project(result, memory),
    )
    assert memory == original_memory
    kept = next(item["facts"] for item in _records(_query_map(executed)["components"], "detail") if item["facts"]["carrier_slot"] == 1)
    missing = next(item["facts"] for item in _records(_query_map(executed)["components"], "detail") if item["facts"]["carrier_slot"] == 2)
    assert kept["origin"] == "memory_catalog_dict"
    assert next(item for item in kept["components"] if item["code"] == "weight_freight")["amount_text"] == "10.00"
    gris = next(item for item in kept["components"] if item["code"] == "gris")
    assert gris["amount_text"] is None
    assert gris["availability"] == "aggregated_without_detail"
    assert missing["origin"] == "memory_catalog_missing"
    assert next(item for item in missing["components"] if item["code"] == "weight_freight")["availability"] == "unavailable"
    assert next(item for item in missing["components"] if item["code"] == "total")["amount_text"] == "12.00"
    assert missing["unresolved_component_refs"] == 1


def test_tax_aggregate_stays_distinct_from_icms_and_iss():
    rows = [
        _row(1, "SP", 10, {
            "table-1": _cell("calculated", Decimal("11.50"), components={"taxes": 0, "icms": 1.5, "iss": 0, "weight_freight": 10, "total": 11.5}),
            "table-2": _cell("calculated", Decimal("22.50"), components={"taxes": 12.5, "weight_freight": 10, "total": 22.5}),
        }),
    ]
    executed = _run(_base_result(rows), [_query("components", "freight_components")])
    detailed_taxes = _component(_query_map(executed)["components"], 1, 1, "taxes")
    assert detailed_taxes["amount_text"] == "0.00"
    assert detailed_taxes["amount_state"] == "zero"
    assert detailed_taxes["detail_level"] == "aggregate"
    assert _component(_query_map(executed)["components"], 1, 1, "icms")["amount_text"] == "1.50"
    assert _component(_query_map(executed)["components"], 1, 1, "iss")["amount_state"] == "zero"
    aggregate = _component(_query_map(executed)["components"], 1, 2, "taxes")
    assert aggregate["amount_text"] == "12.50"
    assert _component(_query_map(executed)["components"], 1, 2, "icms")["availability"] == "absent"
    assert _component(_query_map(executed)["components"], 1, 2, "iss")["availability"] == "absent"


def test_new_modules_do_not_import_protected_services():
    for path in _NEW_FILES:
        source = path.read_text(encoding="utf-8")
        for forbidden in _FORBIDDEN_IMPORTS:
            assert forbidden not in source


def test_decimal_and_dates_follow_the_official_checksum_rule():
    payload = {
        "amount": Decimal("1.50"),
        "day": date(2026, 10, 14),
        "moment": datetime(2026, 10, 14, 15, 23, 0),
        "operation": UUID("22222222-2222-4222-8222-222222222222"),
    }
    assert canonical_artifact_bytes(payload) == _official_json(payload)
    error = _assert_controlled(lambda: canonical_artifact_bytes({"leak": _SensitiveValue()}))
    assert error.code == "invalid_artifact"


def test_claims_are_withheld_without_their_premises():
    rows = [
        _row(1, "SP", 10, {
            "table-1": _cell("not_calculated", None, error={"code": "not_calculated"}),
            "table-2": _cell("not_calculated", None, error={"code": "not_calculated"}),
        }),
    ]
    executed = _run(_base_result(rows), [_query("carriers", "carrier_comparison"), _query("diagnostics", "non_calculation_diagnostics")])
    assert "bounded_best_freight" not in _query_map(executed)["carriers"]["allowed_claims"]
    assert "potential_savings" not in _query_map(executed)["carriers"]["allowed_claims"]
    assert "official_diagnostic" in _query_map(executed)["diagnostics"]["allowed_claims"]
    assert executed["analysis_complete"] is True
    assert executed["queries"][0]["evidence_sufficient"] is True
    assert "population_count" in _query_map(executed)["carriers"]["allowed_claims"]


_PRIVATE_MARKER = "PRIV_DIAG_88217 C:\\private\\secret.txt password=SYNTHETIC_123"


def _reference_from(record) -> EvidenceReference:
    return EvidenceReference(
        evidence_id=record["evidence_id"],
        evidence_digest=record["evidence_digest"],
        operation_id=record["operation_id"],
        comparison_id=record["comparison_id"],
        expected_result_version=record["expected_result_version"],
        contract_version=record["contract_version"],
        memory_checksum=record["memory_checksum"],
        memory_schema_version=record["memory_schema_version"],
    )


def _without(records, role, key, value=None):
    cloned = copy.deepcopy(records)
    target = next(item for item in cloned if item["record_role"] == role)
    target["facts"][key] = value
    return cloned


def test_best_freight_requires_participants_values_tie_rule_and_winner():
    rows = [
        _row(1, "SP", 10, {
            "table-1": _cell("calculated", Decimal("10"), components={"total": 10}),
            "table-2": _cell("not_calculated", None, error={"code": "not_calculated"}),
        }),
    ]
    executed = _run(_base_result(rows), [_query("carriers", "carrier_comparison")])
    assert "bounded_best_freight" not in _query_map(executed)["carriers"]["allowed_claims"]
    _assert_claim_loss_invariant(executed)
    full = _run(_handcrafted_result(), [_query("carriers", "carrier_comparison")])
    records = _query_map(full)["carriers"]["records"]
    assert "bounded_best_freight" in _claims("carrier_comparison", records)
    carriers = copy.deepcopy(records)
    facts = carriers[0]["facts"]
    facts["carriers"] = facts["carriers"][:1]
    assert "bounded_best_freight" not in _claims("carrier_comparison", carriers)
    for key, value in (
        ("tie_rule", None),
        ("universe", None),
        ("comparable_row_count", 0),
        ("decisive_row_count", 0),
        ("global_superiority", True),
    ):
        assert "bounded_best_freight" not in _claims("carrier_comparison", _without(records, "summary", key, value))
    tied = copy.deepcopy(records)
    for carrier in tied[0]["facts"]["carriers"]:
        carrier["comparable_freight_text"] = "10.00"
        carrier["comparable_freight_state"] = "present"
    assert "bounded_best_freight" not in _claims("carrier_comparison", tied)


def test_ranking_requires_both_origin_values_gap_identity_order_and_page():
    executed = _run(_handcrafted_result(), [_query("gaps", "top_differences", page={"offset": 0, "limit": 10})])
    records = _query_map(executed)["gaps"]["records"]
    assert "page_ranking" in _claims("top_differences", records)
    one_origin = copy.deepcopy(records)
    detail = next(item for item in one_origin if item["record_role"] == "detail")
    detail["facts"]["freights"] = detail["facts"]["freights"][:1]
    assert "page_ranking" not in _claims("top_differences", one_origin)
    for key, value in (
        ("difference_definition", None),
        ("gap_total_text", None),
    ):
        changed = _without(records, "summary", key, value)
        if key == "difference_definition":
            assert "page_ranking" not in _claims("top_differences", changed)
    unordered = copy.deepcopy(records)
    details = [item for item in unordered if item["record_role"] == "detail"]
    details[0]["facts"], details[1]["facts"] = details[1]["facts"], details[0]["facts"]
    assert "page_ranking" not in _claims("top_differences", unordered)
    missing_page = copy.deepcopy(records)
    for item in missing_page:
        item["page"] = None
    assert "page_ranking" not in _claims("top_differences", missing_page)


def test_component_provenance_none_does_not_authorize_value_or_origin():
    rows = [
        _row(1, "SP", 10, {
            "table-1": _cell("calculated", Decimal("15"), include_components=False, calculation_memory={"status": "calculated", "total": 15, "calculated_freight": 15}),
            "table-2": _cell("not_calculated", None, error={"code": "not_calculated"}),
        }),
    ]
    executed = _run(_base_result(rows), [_query("components", "freight_components")])
    query = _query_map(executed)["components"]
    total = _component(query, 1, 1, "total")
    assert total["source"] == "none"
    assert total["amount_text"] == "15.00"
    assert "component_value" not in query["allowed_claims"]
    assert "component_provenance" not in query["allowed_claims"]
    _assert_claim_loss_invariant(executed)


def test_aggregate_total_requires_official_value_population_and_denominator():
    executed = _run(_handcrafted_result(), [_query("summary", "official_summary")])
    records = _query_map(executed)["summary"]["records"]
    assert "aggregate_total" in _claims("official_summary", records)
    missing_denominator = copy.deepcopy(records)
    for carrier in missing_denominator[0]["facts"]["carriers"]:
        carrier["population_rows"] = None
        carrier["complete_rows"] = None
    assert "aggregate_total" not in _claims("official_summary", missing_denominator)
    assert "aggregate_total" not in _claims("official_summary", _without(records, "summary", "carrier_total_universe", None))
    assert "coverage" in _claims("official_summary", records)
    assert "coverage" not in _claims("official_summary", _without(records, "summary", "total_cells", None))
    assert "coverage" not in _claims("official_summary", _without(records, "summary", "calculation_coverage_percentage", None))


def test_unaligned_uf_does_not_authorize_geography_equivalence():
    executed = _run(_handcrafted_result(), [_query("ufs", "by_uf")])
    query = _query_map(executed)["ufs"]
    summary = _records(query, "summary")[0]["facts"]
    assert summary["aligned"] is False
    assert "geography_group" not in query["allowed_claims"]
    records = query["records"]
    forced = copy.deepcopy(records)
    forced_summary = next(item for item in forced if item["record_role"] == "summary")
    forced_summary["facts"]["aligned"] = True
    forced_summary["facts"]["alignment_reason"] = "comparable_savings_match_official_geography"
    assert "geography_group" in _claims("by_uf", forced)
    forced_summary["facts"]["alignment_reason"] = "savings_differ"
    assert "geography_group" not in _claims("by_uf", forced)
    _assert_claim_loss_invariant(executed)


def test_savings_without_comparable_base_is_not_authorized():
    rows = [
        _row(1, "SP", 10, {
            "table-1": _cell("not_calculated", None, error={"code": "not_calculated"}),
            "table-2": _cell("not_calculated", None, error={"code": "not_calculated"}),
        }),
    ]
    executed = _run(_base_result(rows), [_query("carriers", "carrier_comparison"), _query("gaps", "top_differences")])
    assert "potential_savings" not in _query_map(executed)["carriers"]["allowed_claims"]
    assert "potential_savings" not in _query_map(executed)["gaps"]["allowed_claims"]
    comparable = _run(_handcrafted_result(), [_query("carriers", "carrier_comparison")])
    records = _query_map(comparable)["carriers"]["records"]
    assert "potential_savings" in _claims("carrier_comparison", records)
    assert "potential_savings" not in _claims("carrier_comparison", _without(records, "summary", "comparable_row_count", 0))
    assert "potential_savings" not in _claims("carrier_comparison", _without(records, "summary", "universe", None))
    assert "potential_savings" not in _claims("carrier_comparison", _without(records, "summary", "potential_savings_text", None))


def test_each_claim_loses_authorization_when_one_premise_disappears():
    result = _handcrafted_result()
    executed = _run(result, [
        _query("summary", "official_summary"),
        _query("carriers", "carrier_comparison"),
        _query("gaps", "top_differences"),
        _query("weights", "by_weight"),
    ])
    diagnostics = _run(result, [_query("diagnostics", "non_calculation_diagnostics")])
    components = _run(result, [_query("components", "freight_components", filters={"row_indexes": [2]})])
    cases = {
        "official_summary": _query_map(executed)["summary"]["records"],
        "carrier_comparison": _query_map(executed)["carriers"]["records"],
        "top_differences": _query_map(executed)["gaps"]["records"],
        "by_weight": _query_map(executed)["weights"]["records"],
        "non_calculation_diagnostics": _query_map(diagnostics)["diagnostics"]["records"],
        "freight_components": _query_map(components)["components"]["records"],
    }
    removals = {
        "official_summary": {
            "bounded_best_freight": ("tie_rule", None),
            "potential_savings": ("savings_universe", None),
            "aggregate_total": ("carrier_total_universe", None),
            "coverage": ("total_cells", 0),
            "population_count": ("total_rows", None),
        },
        "carrier_comparison": {
            "bounded_best_freight": ("tie_rule", None),
            "potential_savings": ("comparable_row_count", 0),
            "population_count": ("universe", None),
        },
        "top_differences": {
            "potential_savings": ("difference_definition", None),
            "page_ranking": ("difference_definition", None),
            "population_count": ("ranked_rows", None),
        },
        "by_weight": {
            "weight_band_population": ("last_band_rule", None),
            "population_count": ("examined_rows", None),
        },
        "non_calculation_diagnostics": {
            "population_count": ("unmapped_code_occurrences", None),
        },
        "freight_components": {
            "population_count": ("matched_rows", None),
        },
    }
    for query_type, records in cases.items():
        granted = _claims(query_type, records)
        for claim, (key, value) in removals[query_type].items():
            assert claim in granted
            assert claim not in _claims(query_type, _without(records, "summary", key, value))
    diagnostic_records = cases["non_calculation_diagnostics"]
    assert "official_diagnostic" in _claims("non_calculation_diagnostics", diagnostic_records)
    without_meaning = copy.deepcopy(diagnostic_records)
    for record in without_meaning:
        for item in record["facts"].get("by_code") or []:
            item["preserved_meaning"] = None
        for cell in record["facts"].get("cells") or []:
            cell["preserved_meaning"] = None
    assert "official_diagnostic" not in _claims("non_calculation_diagnostics", without_meaning)
    component_records = cases["freight_components"]
    assert "component_value" in _claims("freight_components", component_records)
    stripped = copy.deepcopy(component_records)
    for record in stripped:
        if record["record_role"] != "detail":
            continue
        for component in record["facts"]["components"]:
            component["source"] = "none"
    assert "component_value" not in _claims("freight_components", stripped)
    assert "component_provenance" not in _claims("freight_components", stripped)


def test_local_and_global_losses_block_claims_together():
    executed = _run(
        _handcrafted_result(),
        [_query("gaps", "top_differences")],
        limits=QueryExecutionLimits(max_evidence_refs=1),
    )
    query = executed["queries"][0]
    local = {claim for loss in query["losses"] for claim in loss["blocks"]}
    remote = {claim for loss in executed["losses"] for claim in loss["blocks"]}
    assert "page_incomplete" in {loss["code"] for loss in query["losses"]}
    assert "evidence_omitted" in {loss["code"] for loss in executed["losses"]}
    assert "potential_savings" in remote
    assert "population_count" in remote
    assert set(query["allowed_claims"]).isdisjoint(local | remote)
    assert "potential_savings" not in query["allowed_claims"]
    assert "population_count" not in query["allowed_claims"]
    assert executed["analysis_complete"] is True
    assert executed["publication_complete"] is False
    assert executed["evidence_sufficient"] is False
    assert query["evidence_sufficient"] is False
    assert query["records"]
    _assert_claim_loss_invariant(executed)


def test_loss_application_order_does_not_change_the_decision():
    local = _LossBook()
    local.add("page_incomplete", 4)
    local.add("monetary_withheld", 2)
    remote = _LossBook()
    remote.add("evidence_omitted", 5)
    supported = {
        "potential_savings",
        "population_count",
        "page_ranking",
        "aggregate_total",
        "bounded_best_freight",
    }
    forward = _authorize_claims(supported, local, remote)
    backward = _authorize_claims(supported, remote, local)
    assert forward == backward
    assert set(forward).isdisjoint(local.blocked_claims() | remote.blocked_claims())
    assert "potential_savings" not in forward
    assert "population_count" not in forward


def test_unexpected_failures_publish_no_private_marker(monkeypatch):
    result = _scale_result(2)

    def boom(*_args, **_kwargs):
        raise RuntimeError(_PRIVATE_MARKER)

    monkeypatch.setattr(executor_module, "_load_analytics", boom)
    analytics_failed = _run(result, [_query("summary", "official_summary")])
    assert analytics_failed["status"] == "error"
    assert analytics_failed["error"]["code"] == "execution_failed"
    assert analytics_failed["queries"] == []
    assert analytics_failed["analysis_complete"] is False
    assert analytics_failed["evidence_sufficient"] is False
    _assert_absent(analytics_failed, "PRIV_DIAG_88217", "secret.txt", "password=SYNTHETIC_123", "RuntimeError")

    monkeypatch.setattr(executor_module, "_resolve_row", boom)
    rows_failed = _run(result, [_query("gaps", "top_differences")])
    assert rows_failed["queries"] == []
    assert rows_failed["error"]["code"] == "execution_failed"
    _assert_absent(rows_failed, "PRIV_DIAG_88217", "secret.txt", "password=SYNTHETIC_123")

    monkeypatch.setattr(executor_module, "publish_analytical_execution", boom)
    publication_failed = _run(result, [_query("summary", "official_summary")])
    assert publication_failed["status"] == "error"
    assert publication_failed["queries"] == []
    assert publication_failed["evidence_sufficient"] is False
    _assert_absent(publication_failed, "PRIV_DIAG_88217", "secret.txt", "password=SYNTHETIC_123")


def test_controlled_validation_stays_distinct_from_unexpected_failure(monkeypatch):
    from app.agente_compara_comparison_analytics_service import AgenteComparaComparisonAnalyticsError

    def boom(_view):
        raise AgenteComparaComparisonAnalyticsError(_PRIVATE_MARKER)

    monkeypatch.setattr(
        "app.agente_compara_comparison_analytics_service.build_comparison_analytics",
        boom,
    )
    with pytest.raises(QueryCatalogError) as caught:
        _run(_scale_result(2), [_query("summary", "official_summary")])
    assert caught.value.code == "official_analytics_unavailable"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "PRIV_DIAG_88217" not in str(caught.value)
    assert "password=SYNTHETIC_123" not in repr(caught.value)


def test_previous_memory_and_foreign_operation_fail_closed():
    result = _scale_result(2)
    memory = {"schema_version": 3, "comparison_id": _COMPARISON, "items": {}}
    scope = _scope(result, memory)
    executed = execute_analytical_plan(
        _plan([_query("summary", "official_summary")]),
        _project(result, memory),
    )
    record = executed["queries"][0]["records"][0]
    current = _reference_from(record)
    assert evidence_authorizes_operation(
        current,
        operation_id=_OPERATION,
        comparison_id=scope.comparison_id,
        expected_result_version=scope.expected_result_version,
        scope=scope,
    )
    stale_memory = {"schema_version": 3, "comparison_id": _COMPARISON, "items": {"old": {"kept": True}}}
    stale = EvidenceReference(
        evidence_id=record["evidence_id"],
        evidence_digest=record["evidence_digest"],
        operation_id=record["operation_id"],
        comparison_id=record["comparison_id"],
        expected_result_version=record["expected_result_version"],
        contract_version=record["contract_version"],
        memory_checksum=artifact_checksum(stale_memory),
        memory_schema_version=3,
    )
    assert evidence_authorizes_operation(
        stale,
        operation_id=_OPERATION,
        comparison_id=scope.comparison_id,
        expected_result_version=scope.expected_result_version,
        scope=scope,
    ) is False
    other_version = EvidenceReference(
        evidence_id=record["evidence_id"],
        evidence_digest=record["evidence_digest"],
        operation_id=record["operation_id"],
        comparison_id=record["comparison_id"],
        expected_result_version=record["expected_result_version"],
        contract_version=record["contract_version"],
        memory_checksum=record["memory_checksum"],
        memory_schema_version=4,
    )
    assert evidence_authorizes_operation(
        other_version,
        operation_id=_OPERATION,
        comparison_id=scope.comparison_id,
        expected_result_version=scope.expected_result_version,
        scope=scope,
    ) is False
    assert evidence_authorizes_operation(
        current,
        operation_id=_OTHER_OPERATION,
        comparison_id=scope.comparison_id,
        expected_result_version=scope.expected_result_version,
        scope=scope,
    ) is False


def test_single_field_selection_omits_unrequested_analytical_detail():
    result = _handcrafted_result()
    original = copy.deepcopy(result)
    first = _run(result, [
        _query("summary", "official_summary", fields=["population"]),
        _query("components", "freight_components", fields=["availability_counts"], filters={"row_indexes": [2]}),
        _query("carriers", "carrier_comparison", fields=["universe"]),
        _query("gaps", "top_differences", fields=["gap_total"]),
    ])
    second = _run(result, [
        _query("ufs", "by_uf", fields=["alignment"]),
        _query("weights", "by_weight", fields=["band_rule"]),
        _query("partials", "incomplete_freights", fields=["excluded_complete_with_warnings"]),
        _query("diagnostics", "non_calculation_diagnostics", fields=["by_code"]),
    ])
    assert result == original
    for executed in (first, second):
        _assert_boundary(executed)
    summary = _records(_query_map(first)["summary"], "summary")[0]["facts"]
    assert summary["total_rows"] == result["row_count"]
    assert summary["carriers"] == []
    assert summary["potential_savings_text"] is None
    assert "bounded_best_freight" not in _query_map(first)["summary"]["allowed_claims"]
    assert _records(_query_map(first)["components"], "detail") == []
    assert "component_value" not in _query_map(first)["components"]["allowed_claims"]
    carrier_facts = _records(_query_map(first)["carriers"], "summary")[0]["facts"]
    assert carrier_facts["carriers"] == []
    assert carrier_facts["universe"] == "fully_comparable_rows"
    assert "bounded_best_freight" not in _query_map(first)["carriers"]["allowed_claims"]
    gap_query = _query_map(first)["gaps"]
    assert _records(gap_query, "detail") == []
    assert _records(gap_query, "summary")[0]["facts"]["gap_total_text"] == "30.00"
    assert "absolute_gap_text" not in json.dumps(gap_query["records"])
    assert "page_ranking" not in gap_query["allowed_claims"]
    uf_query = _query_map(second)["ufs"]
    assert _records(uf_query, "detail") == []
    assert _records(uf_query, "special_group") == []
    assert _records(uf_query, "summary")[0]["facts"]["alignment_reason"]
    assert "geography_group" not in uf_query["allowed_claims"]
    weight_query = _query_map(second)["weights"]
    assert _records(weight_query, "detail") == []
    assert _records(weight_query, "summary")[0]["facts"]["last_band_rule"] == "lower_inclusive_without_upper_bound"
    assert _records(weight_query, "summary")[0]["facts"]["band_counts"] == []
    assert "weight_band_population" not in weight_query["allowed_claims"]
    partial_query = _query_map(second)["partials"]
    assert _records(partial_query, "detail") == []
    assert _records(partial_query, "summary")[0]["facts"]["excluded_complete_with_warnings"] == 1
    assert "partial_freight" not in partial_query["allowed_claims"]
    diagnostic_query = _query_map(second)["diagnostics"]
    assert _records(diagnostic_query, "detail") == []
    codes = _records(diagnostic_query, "summary")[0]["facts"]["by_code"]
    assert codes
    assert all(item["preserved_meaning"] is None for item in codes)
    assert "official_diagnostic" not in diagnostic_query["allowed_claims"]


def test_pagination_and_context_survive_field_selection():
    result = _handcrafted_result()
    executed = _run(result, [
        _query(
            "gaps",
            "top_differences",
            fields=["ordered_differences", "difference_definition"],
            page={"offset": 1, "limit": 2},
        ),
    ])
    _assert_boundary(executed)
    query = executed["queries"][0]
    details = [item["facts"]["row_index"] for item in _records(query, "detail")]
    assert details == [1, 2]
    assert query["page"]["offset"] == 1
    assert query["page"]["limit"] == 2
    assert query["page"]["returned"] == 2
    assert _records(query, "summary")[0]["facts"]["gap_total_text"] is None
    assert "page_ranking" in query["allowed_claims"]
    assert "potential_savings" not in query["allowed_claims"]
    public = {key: value for key, value in executed.items() if key not in {"context_text", "context_chars"}}
    assert json.loads(executed["context_text"]) == public


def test_field_selection_does_not_mutate_input_or_sealed_context():
    caller = _handcrafted_result()
    untouched = copy.deepcopy(caller)
    executed = _run(caller, [_query("weights", "by_weight", fields=["band_rule"])])
    assert caller == untouched
    before = json.loads(executed["context_text"])
    executed["queries"][0]["records"][0]["facts"]["last_band_rule"] = "alterado"
    assert json.loads(executed["context_text"]) == before
    assert executed["queries"][0]["records"][0]["facts"]["last_band_rule"] == "alterado"


def _gris_components(gris):
    rows = [
        _row(1, "SP", 10, {
            "table-1": _cell(
                "calculated",
                Decimal("20"),
                components={"gris": gris, "weight_freight": 20, "total": 20},
            ),
            "table-2": _cell(
                "calculated",
                Decimal("22"),
                components={"weight_freight": 22, "total": 22},
            ),
        }),
    ]
    executed = _run(_base_result(rows), [_query("components", "freight_components")])
    return _query_map(executed)["components"]


def _gris_bucket(query):
    facts = next(
        item["facts"]
        for item in _records(query, "detail")
        if item["facts"]["row_index"] == 1 and item["facts"]["carrier_slot"] == 1
    )
    buckets = [item for item in facts["buckets"] if item["code"] == "gris_ad_valorem_bucket"]
    assert len(buckets) == 1
    return buckets[0]


def test_undifferentiated_bucket_requires_numeric_support():
    missing = _gris_components(None)
    missing_bucket = _gris_bucket(missing)
    assert missing_bucket["availability"] == "aggregated_without_detail"
    assert missing_bucket["amount_text"] is None
    assert missing_bucket["amount_state"] == "absent"
    assert "undifferentiated_bucket" not in missing["allowed_claims"]
    assert _component(missing, 1, 1, "gris")["amount_text"] is None
    assert _component(missing, 1, 1, "ad_valorem")["amount_text"] is None

    invalid = _gris_components("invalid")
    invalid_bucket = _gris_bucket(invalid)
    assert invalid_bucket["availability"] == "aggregated_without_detail"
    assert invalid_bucket["amount_text"] is None
    assert "undifferentiated_bucket" not in invalid["allowed_claims"]
    assert _component(invalid, 1, 1, "gris")["amount_text"] is None
    assert _component(invalid, 1, 1, "ad_valorem")["amount_text"] is None

    ready = _gris_components(5)
    ready_bucket = _gris_bucket(ready)
    assert ready_bucket["code"] == "gris_ad_valorem_bucket"
    assert ready_bucket["availability"] == "aggregated_without_detail"
    assert ready_bucket["amount_text"] == "5.00"
    assert ready_bucket["amount_state"] == "present"
    assert "undifferentiated_bucket" in ready["allowed_claims"]
    assert _component(ready, 1, 1, "gris")["amount_text"] is None
    assert _component(ready, 1, 1, "gris")["availability"] == "aggregated_without_detail"
    assert _component(ready, 1, 1, "ad_valorem")["amount_text"] is None
    assert _component(ready, 1, 1, "ad_valorem")["availability"] == "aggregated_without_detail"
    declared_only = copy.deepcopy(ready["records"])
    for record in declared_only:
        for bucket in record["facts"].get("buckets") or []:
            if bucket["code"] == "gris_ad_valorem_bucket":
                bucket["amount_text"] = None
                bucket["amount_state"] = "absent"
    assert "undifferentiated_bucket" not in _claims("freight_components", declared_only)

    zero = _gris_components(0)
    zero_bucket = _gris_bucket(zero)
    assert zero_bucket["amount_text"] == "0.00"
    assert zero_bucket["amount_state"] == "zero"
    assert "undifferentiated_bucket" in zero["allowed_claims"]
    assert _component(zero, 1, 1, "gris")["amount_text"] is None
    assert _component(zero, 1, 1, "ad_valorem")["amount_text"] is None


def test_coverage_claim_requires_coherent_official_counts():
    result = _handcrafted_result()
    official = build_comparison_analytics(copy.deepcopy(result))
    executed = _run(result, [_query("summary", "official_summary")])
    records = _query_map(executed)["summary"]["records"]
    summary = records[0]["facts"]
    global_summary = official["global_summary"]
    assert summary["total_cells"] == global_summary["total_cells"]
    assert summary["calculated_cells"] == global_summary["calculated_cells"]
    assert summary["error_cells"] == global_summary["error_cells"]
    assert summary["calculated_cells"] + summary["error_cells"] == summary["total_cells"]
    assert summary["calculation_coverage_percentage"] == f"{global_summary['calculation_coverage_percentage']:.1f}"
    assert "coverage" in _claims("official_summary", records)

    zero_total = copy.deepcopy(records)
    zero_facts = zero_total[0]["facts"]
    zero_facts["total_cells"] = 0
    zero_facts["calculated_cells"] = 0
    zero_facts["error_cells"] = 0
    zero_facts["calculation_coverage_percentage"] = "0.0"
    assert "coverage" not in _claims("official_summary", zero_total)

    above = copy.deepcopy(records)
    above[0]["facts"]["calculated_cells"] = above[0]["facts"]["total_cells"] + 1
    assert "coverage" not in _claims("official_summary", above)

    contradictory = copy.deepcopy(records)
    contradictory[0]["facts"]["error_cells"] = contradictory[0]["facts"]["error_cells"] + 1
    assert "coverage" not in _claims("official_summary", contradictory)
    assert "coverage" not in _claims("official_summary", _without(records, "summary", "error_cells", None))

    incompatible = copy.deepcopy(records)
    published = incompatible[0]["facts"]["calculation_coverage_percentage"]
    incompatible[0]["facts"]["calculation_coverage_percentage"] = "0.0" if published != "0.0" else "1.0"
    assert "coverage" not in _claims("official_summary", incompatible)

    zero_money = _base_result([
        _row(1, "SP", 10, {
            "table-1": _cell("calculated", Decimal("0"), components={"total": 0, "weight_freight": 0}),
            "table-2": _cell("calculated", Decimal("0"), components={"total": 0, "weight_freight": 0}),
        }),
    ])
    zero_official = build_comparison_analytics(copy.deepcopy(zero_money))
    zero_executed = _run(zero_money, [_query("summary", "official_summary")])
    zero_query = _query_map(zero_executed)["summary"]
    zero_summary = _records(zero_query, "summary")[0]["facts"]
    assert zero_official["global_summary"]["calculation_coverage_percentage"] == 100.0
    assert "coverage" in zero_query["allowed_claims"]
    assert {item["official_freight_text"] for item in zero_summary["carriers"]} == {"0.00"}
    assert {item["official_freight_state"] for item in zero_summary["carriers"]} == {"zero"}
    assert {item["comparable_freight_text"] for item in zero_summary["carriers"]} == {"0.00"}
    assert {item["comparable_freight_state"] for item in zero_summary["carriers"]} == {"zero"}


def _band_only(records, group_id):
    cloned = copy.deepcopy(records)
    summary = next(item for item in cloned if item["record_role"] == "summary")
    detail = next(
        item for item in cloned if item["record_role"] == "detail" and item["facts"]["group_id"] == group_id
    )
    return [summary, detail]


def _official_limit(bound):
    whole = int(bound)
    return f"{whole}.00", "zero" if whole == 0 else "present"


def test_weight_band_claim_matches_official_limits():
    executed = _run(_handcrafted_result(), [_query("weights", "by_weight")])
    records = _query_map(executed)["weights"]["records"]
    lower, upper = next(item for item in WEIGHT_BANDS_V1 if item == ("10", "30"))
    bounded = _band_only(records, f"{lower}:{upper}")
    facts = bounded[1]["facts"]
    lower_text, lower_state = _official_limit(lower)
    upper_text, upper_state = _official_limit(upper)
    assert facts["lower_text"] == lower_text
    assert facts["lower_state"] == lower_state
    assert facts["upper_text"] == upper_text
    assert facts["upper_state"] == upper_state
    assert facts["lower_inclusive"] is True
    assert facts["upper_exclusive"] is True
    assert facts["last_band"] is False
    assert "weight_band_population" in _claims("by_weight", bounded)

    inverted = _band_only(records, f"{lower}:{upper}")
    inverted[1]["facts"]["lower_text"] = upper_text
    inverted[1]["facts"]["upper_text"] = lower_text
    assert "weight_band_population" not in _claims("by_weight", inverted)

    wrong_lower = _band_only(records, f"{lower}:{upper}")
    wrong_lower[1]["facts"]["lower_inclusive"] = False
    assert "weight_band_population" not in _claims("by_weight", wrong_lower)
    wrong_upper = _band_only(records, f"{lower}:{upper}")
    wrong_upper[1]["facts"]["upper_exclusive"] = False
    assert "weight_band_population" not in _claims("by_weight", wrong_upper)

    last_lower, last_upper = WEIGHT_BANDS_V1[-1]
    assert last_upper is None
    last = _band_only(records, f"{last_lower}:inf")
    last_facts = last[1]["facts"]
    last_text, last_state = _official_limit(last_lower)
    assert last_facts["lower_text"] == last_text
    assert last_facts["lower_state"] == last_state
    assert last_facts["upper_text"] is None
    assert last_facts["upper_state"] == "absent"
    assert last_facts["lower_inclusive"] is True
    assert last_facts["upper_exclusive"] is False
    assert last_facts["last_band"] is True
    assert last[0]["facts"]["last_band_rule"] == "lower_inclusive_without_upper_bound"
    assert "weight_band_population" in _claims("by_weight", last)

    closed_last = _band_only(records, f"{last_lower}:inf")
    closed_last[1]["facts"]["upper_exclusive"] = True
    assert "weight_band_population" not in _claims("by_weight", closed_last)
    forged_ceiling = _band_only(records, f"{last_lower}:inf")
    forged_ceiling[1]["facts"]["upper_text"] = "2000.00"
    forged_ceiling[1]["facts"]["upper_state"] = "present"
    forged_ceiling[1]["facts"]["upper_exclusive"] = True
    forged_ceiling[1]["facts"]["last_band"] = False
    assert "weight_band_population" not in _claims("by_weight", forged_ceiling)
