from __future__ import annotations

import copy
import json
from pathlib import Path

from app.agente_compara_comparison_calculation_service import (
    compact_comparison_result_for_storage,
    hydrate_memory_item,
)


def _build_fixture_10x3() -> dict:
    coverage_rows = [
        {"uf": f"UF{i % 27}", "city": f"Cidade {i}", "region": f"REG-{i % 5}"}
        for i in range(2500)
    ]
    shared_coverage = {"rows": coverage_rows}
    shared_warning = {"code": "warn_shared", "message": "Alerta compartilhado"}
    shared_blocking = {"code": "block_shared", "message": "Bloqueio compartilhado"}
    tables = [
        {"table_id": f"t{i}", "slot_number": i + 1, "carrier_name": f"Carrier {i}"}
        for i in range(3)
    ]
    statuses = ["calculated", "calculated_with_warnings", "incomplete", "error"]
    rows = []
    for row_index in range(1, 11):
        table_results = {}
        for table_idx, table in enumerate(tables):
            status = statuses[(row_index + table_idx) % len(statuses)]
            warning = shared_warning
            blocking = shared_blocking
            evidence = {
                "source": "fixture",
                "table": table["table_id"],
                "rule": "rule-shared",
                "freight_region": "Sudeste",
                "coverage_table": shared_coverage,
            }
            memory = {
                "schema_version": 1,
                "status": status,
                "total": None if status == "error" else round(100 + row_index + table_idx / 10, 2),
                "warnings": [warning] if status in {"calculated_with_warnings", "incomplete"} else [],
                "blocking_issues": [blocking] if status in {"incomplete", "error"} else [],
                "evidence": evidence,
                "components": [
                    {"code": "WEIGHT_FREIGHT", "label": "Frete-peso", "amount": 90.0, "basis": "fixed", "formula": "base"},
                    {"code": "TOLL", "label": "Ped?gio", "amount": 10.0, "basis": "fixed", "formula": "pedagio"},
                ],
                "pricing": {"rule": "rule-shared", "type": "fixed"},
                "selected_rule": "rule-shared",
            }
            table_results[table["table_id"]] = {
                "table_id": table["table_id"],
                "carrier_name": table["carrier_name"],
                "slot_number": table["slot_number"],
                "calculated_freight": memory["total"],
                "status": status,
                "raw_status": status,
                "final_status": status,
                "completeness_status": "partial" if status == "incomplete" else "complete",
                "is_partial_value": status == "incomplete",
                "error": {"code": "fixture_error", "message": "Falha controlada"} if status == "error" else None,
                "warnings": list(memory["warnings"]),
                "blocking_issues": list(memory["blocking_issues"]),
                "components": [dict(component) for component in memory["components"]],
                "evidence": evidence,
                "calculation_memory": memory,
            }
        rows.append({
            "row_index": row_index,
            "document_number": f"DOC-{row_index}",
            "destination_city": "Campinas",
            "destination_uf": "SP",
            "weight": 10 + row_index,
            "invoice_value": 1000 + row_index,
            "table_results": table_results,
        })
    return {
        "schema_version": 1,
        "comparison_id": "cmp-dedup",
        "table_count": 3,
        "tables": tables,
        "comparative_rows": rows,
        "results_by_table": {
            table["table_id"]: {
                "comparison_id": "cmp-dedup",
                "table_id": table["table_id"],
                "slot_number": table["slot_number"],
                "carrier_name": table["carrier_name"],
                "row_count": 10,
                "calculated_count": 3,
                "calculated_with_warnings_count": 3,
                "incomplete_count": 2,
                "error_count": 2,
                "summary": {"carrier_name": table["carrier_name"], "coverage_classification": "shared"},
                "duration_ms": 10,
                "schema_version": 1,
            }
            for table in tables
        },
        "summary": {
            "calculated_cell_count": 22,
            "error_cell_count": 8,
            "total_calculation_cells": 30,
        },
        "row_count": 10,
    }


def _contains_coverage_rows(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "rows" and isinstance(value, list):
                return True
            if _contains_coverage_rows(value):
                return True
    elif isinstance(node, list):
        for value in node:
            if _contains_coverage_rows(value):
                return True
    return False


def test_memory_payload_deduplicates_small_fixture():
    result = _build_fixture_10x3()
    before_bytes = len(json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8"))

    compacted = compact_comparison_result_for_storage(result)
    payload = compacted["memory_payload"]
    after_bytes = len(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))

    assert payload["schema_version"] == 3
    assert len(payload["items"]) == 30
    assert len(payload["components"]) == 2
    assert len(payload["messages"]) == 2
    assert len(payload["evidence_catalog"]) == 3
    assert len(payload["tables"]) == 3
    assert len(payload["rules"]) == 1
    assert before_bytes > after_bytes

    for item in payload["items"].values():
        assert not _contains_coverage_rows(item)
        assert "warning_codes" in item
        assert "blocking_codes" in item

    for evidence in payload["evidence_catalog"].values():
        assert not _contains_coverage_rows(evidence)


def test_memory_payload_uses_identity_and_content_caches_for_shared_large_objects():
    result = _build_fixture_10x3()
    stats = {}

    compacted = compact_comparison_result_for_storage(result, _debug_stats=stats)
    payload = compacted["memory_payload"]

    assert len(payload["items"]) == 30
    assert len(payload["evidence_catalog"]) == 3
    assert stats["evd_hash_calls"] == 3
    assert stats["evd_content_hits"] >= 20
    assert stats.get("evd_identity_hits", 0) == 0
    assert stats["msg_content_hits"] >= 10


def test_memory_payload_still_deduplicates_distinct_equivalent_objects():
    result = _build_fixture_10x3()
    first = result["comparative_rows"][0]["table_results"]["t0"]
    duplicate = {
        "source": first["evidence"]["source"],
        "table": first["evidence"]["table"],
        "rule": first["evidence"]["rule"],
        "freight_region": first["evidence"]["freight_region"],
        "coverage_table": {"rows": first["evidence"]["coverage_table"]["rows"]},
    }
    result["comparative_rows"][1]["table_results"]["t0"]["evidence"] = duplicate
    result["comparative_rows"][1]["table_results"]["t0"]["calculation_memory"]["evidence"] = duplicate

    compacted = compact_comparison_result_for_storage(result)

    assert len(compacted["memory_payload"]["evidence_catalog"]) == 3


def test_memory_payload_rehydrates_public_contract_and_legacy_item():
    result = _build_fixture_10x3()
    compacted = compact_comparison_result_for_storage(result)
    payload = compacted["memory_payload"]

    sample_key = next(key for key, value in payload["items"].items() if value.get("blocking_codes"))
    hydrated = hydrate_memory_item(payload["items"][sample_key], payload)
    legacy = hydrate_memory_item({
        "memory_ref": "legacy:1",
        "table_id": "t1",
        "row_index": 1,
        "components": [{"code": "X"}],
        "evidence": {"foo": "bar"},
        "warnings": ["w"],
        "blocking_issues": ["b"],
        "calculation_memory": {"status": "calculated"},
    }, {"schema_version": 2})

    assert hydrated["components"][0]["code"] == "WEIGHT_FREIGHT"
    assert hydrated["warnings"][0]["text"] == "Alerta compartilhado"
    assert hydrated["blocking_issues"][0]["text"] == "Bloqueio compartilhado"
    assert hydrated["evidence"]["freight_region"] == "Sudeste"
    assert "coverage_table" not in hydrated["evidence"]
    assert hydrated["calculation_memory"]["status"] in {"calculated", "calculated_with_warnings", "incomplete", "error"}
    assert hydrated["calculation_memory"]["evidence"]["freight_region"] == "Sudeste"
    assert "coverage_table" not in hydrated["calculation_memory"]["evidence"]
    assert legacy["evidence"]["foo"] == "bar"


def test_utf8_strings_are_restored_in_productive_files():
    backend = Path("app/agente_compara_calculation_result_storage.py").read_text(encoding="utf-8")
    frontend = Path("app/static/js/agente_compara.js").read_text(encoding="utf-8")
    doc_service = Path("app/agente_compara_doc_service.py").read_text(encoding="utf-8")

    backend_expected = "Os c\u00e1lculos foram processados, mas os detalhes excedem o limite t\u00e9cnico de armazenamento."
    frontend_expected = "Os c\u00e1lculos foram processados, mas o armazenamento excedeu o limite t\u00e9cnico."
    doc_expected = "N\u00e3o foi poss\u00edvel preparar a tabela por uma falha tempor\u00e1ria. Nenhum cr\u00e9dito foi consumido por esta tentativa."

    assert backend_expected in backend
    assert frontend_expected in frontend
    assert doc_expected in doc_service
    assert "c?lculos" not in backend
    assert "t?cnico" not in backend
    assert "c?lculos" not in frontend
    assert "t?cnico" not in frontend
    assert "N?o foi poss?vel preparar a tabela" not in doc_service
    assert "tempor?ria" not in doc_service


def test_memory_payload_preserves_statuses_and_values_for_all_cells():
    result = _build_fixture_10x3()
    compacted = compact_comparison_result_for_storage(result)
    payload = compacted["memory_payload"]
    compact_rows = {
        (row["row_index"], table_id): cell
        for row in compacted["compact_result"]["comparative_rows"]
        for table_id, cell in row["table_results"].items()
    }

    for row in result["comparative_rows"]:
        for table_id, original_cell in row["table_results"].items():
            memory_ref = f"{table_id}:{row['row_index']}"
            compact_cell = compact_rows[(row["row_index"], table_id)]
            hydrated = hydrate_memory_item(payload["items"][memory_ref], payload)

            assert compact_cell["memory_ref"] == memory_ref
            assert compact_cell["status"] == original_cell["status"]
            assert compact_cell["final_status"] == original_cell["final_status"]
            assert hydrated["status"] == original_cell["status"]
            assert hydrated["total"] == (original_cell.get("calculation_memory") or {}).get("total")
            assert (hydrated.get("calculation_memory") or {}).get("status") == original_cell["status"]
            assert (hydrated.get("calculation_memory") or {}).get("total") == (original_cell.get("calculation_memory") or {}).get("total")
