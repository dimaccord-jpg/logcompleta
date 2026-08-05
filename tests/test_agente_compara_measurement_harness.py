from __future__ import annotations

import copy
import gc
import json
import time
import tracemalloc
from datetime import datetime, timezone

import app.agente_compara_calculation_execution_service as exec_service
from app.agente_compara_calculation_execution_service import _rehydrate_compact_result
from app.agente_compara_calculation_result_storage import (
    _canonical_json_bytes,
    delete_comparison_calculation_memories,
    delete_comparison_calculation_result,
    load_comparison_calculation_memory_payload,
    load_comparison_calculation_result,
    save_comparison_calculation_memory_payload,
    save_comparison_calculation_result,
)
from app.agente_compara_comparison_calculation_service import compact_comparison_result_for_storage, hydrate_memory_item


def _build_measurement_fixture_100x3(comparison_id: str = "cmp-measure-100x3") -> dict:
    tables = [
        {"table_id": f"t{i}", "slot_number": i + 1, "carrier_name": f"Carrier {i + 1}"}
        for i in range(3)
    ]
    statuses = ["calculated", "calculated_with_warnings", "incomplete", "error"]
    shared_components = [
        {"code": "WEIGHT_FREIGHT", "label": "Frete-peso", "amount": 90.0, "basis": "fixed", "formula": "base"},
        {"code": "TOLL", "label": "Pedagio", "amount": 10.0, "basis": "fixed", "formula": "pedagio"},
    ]
    coverage_rows = [
        {"destination_uf": f"UF{i % 27}", "destination_city": f"Cidade {i}", "freight_region": f"REG-{i % 5}"}
        for i in range(2500)
    ]
    evidence_pool = [
        {
            "source": "fixture",
            "table": table["table_id"],
            "rule": f"rule-{region_idx}",
            "freight_region": f"REG-{region_idx}",
            "coverage_table": {"rows": coverage_rows},
        }
        for table in tables
        for region_idx in range(5)
    ]
    rows = []
    per_table = {}
    status_counts = {
        table["table_id"]: {"calculated": 0, "calculated_with_warnings": 0, "incomplete": 0, "error": 0}
        for table in tables
    }
    for row_index in range(1, 101):
        table_results = {}
        for table_idx, table in enumerate(tables):
            status = statuses[(row_index + table_idx) % len(statuses)]
            evidence = evidence_pool[(table_idx * 5) + ((row_index - 1) % 5)]
            warning = {"code": f"warn-{table_idx}", "message": f"Warning {table_idx}"}
            blocking = {"code": f"block-{table_idx}", "message": f"Blocking {table_idx}"}
            total = None if status == "error" else round(100 + row_index + table_idx / 10, 2)
            memory = {
                "schema_version": 1,
                "status": status,
                "total": total,
                "warnings": [warning] if status in {"calculated_with_warnings", "incomplete"} else [],
                "blocking_issues": [blocking] if status in {"incomplete", "error"} else [],
                "evidence": evidence,
                "components": copy.deepcopy(shared_components),
                "pricing": {"rule": f"rule-{(row_index - 1) % 5}", "type": "fixed"},
                "selected_rule": f"rule-{(row_index - 1) % 5}",
            }
            table_results[table["table_id"]] = {
                "table_id": table["table_id"],
                "carrier_name": table["carrier_name"],
                "slot_number": table["slot_number"],
                "calculated_freight": total,
                "status": status,
                "raw_status": status,
                "final_status": status,
                "completeness_status": "partial" if status == "incomplete" else "complete",
                "is_partial_value": status == "incomplete",
                "error": {"code": "fixture_error", "message": "Falha controlada"} if status == "error" else None,
                "warnings": list(memory["warnings"]),
                "blocking_issues": list(memory["blocking_issues"]),
                "components": copy.deepcopy(shared_components),
                "evidence": evidence,
                "calculation_memory": memory,
            }
            status_counts[table["table_id"]][status] += 1
        rows.append(
            {
                "row_index": row_index,
                "document_number": f"DOC-{row_index}",
                "destination_city": "Campinas",
                "destination_uf": "SP",
                "weight": 10 + row_index,
                "invoice_value": 1000 + row_index,
                "table_results": table_results,
            }
        )
    for table in tables:
        counts = status_counts[table["table_id"]]
        per_table[table["table_id"]] = {
            "comparison_id": comparison_id,
            "table_id": table["table_id"],
            "slot_number": table["slot_number"],
            "carrier_name": table["carrier_name"],
            "row_count": 100,
            "calculated_count": counts["calculated"],
            "calculated_with_warnings_count": counts["calculated_with_warnings"],
            "incomplete_count": counts["incomplete"],
            "error_count": counts["error"],
            "summary": {"carrier_name": table["carrier_name"], "coverage_classification": "shared"},
            "duration_ms": 10 + table["slot_number"],
            "schema_version": 1,
        }
    error_count = sum(v["error"] for v in status_counts.values())
    return {
        "schema_version": 1,
        "comparison_id": comparison_id,
        "table_count": 3,
        "tables": tables,
        "comparative_rows": rows,
        "results_by_table": per_table,
        "summary": {
            "calculated_cell_count": 300 - error_count,
            "error_cell_count": error_count,
            "total_calculation_cells": 300,
        },
        "row_count": 100,
    }


def _utc_checkpoint() -> str:
    return datetime.now(timezone.utc).isoformat()


def _walk_has_coverage_rows(node) -> bool:
    if isinstance(node, dict):
        rows = node.get("rows")
        if isinstance(rows, list):
            return True
        return any(_walk_has_coverage_rows(value) for value in node.values())
    if isinstance(node, list):
        return any(_walk_has_coverage_rows(value) for value in node)
    return False


def _count_evidence_refs(memory_payload: dict) -> dict:
    items = memory_payload.get("items") if isinstance(memory_payload.get("items"), dict) else {}
    catalog = memory_payload.get("evidence_catalog") if isinstance(memory_payload.get("evidence_catalog"), dict) else {}
    counts = {key: 0 for key in catalog}
    unknown_refs = []
    for item in items.values():
        if not isinstance(item, dict):
            continue
        for ref in item.get("evidence_refs") or []:
            if not isinstance(ref, str):
                continue
            if ref in counts:
                counts[ref] += 1
            else:
                unknown_refs.append(ref)
    ref_values = list(counts.values())
    total_refs = sum(ref_values)
    evidence_count = len(counts)
    return {
        "total_refs": total_refs,
        "evidence_count": evidence_count,
        "avg_refs_per_evidence": (total_refs / evidence_count) if evidence_count else 0.0,
        "min_refs": min(ref_values) if ref_values else 0,
        "max_refs": max(ref_values) if ref_values else 0,
        "orphan_evidences": sorted(ref for ref, count in counts.items() if count == 0),
        "unknown_refs": sorted(set(unknown_refs)),
    }


def _index_cells(result: dict) -> dict:
    indexed = {}
    for row in result.get("comparative_rows") or []:
        if not isinstance(row, dict):
            continue
        row_index = int(row.get("row_index") or 0)
        table_results = row.get("table_results") if isinstance(row.get("table_results"), dict) else {}
        for table_id, cell in table_results.items():
            if isinstance(cell, dict):
                indexed[f"{table_id}:{row_index}"] = cell
    return indexed


def _essential_components(cell: dict) -> list[tuple]:
    values = cell.get("components") or []
    if isinstance(values, dict):
        return sorted((str(key), value) for key, value in values.items())
    if isinstance(values, list):
        return [
            (
                component.get("code"),
                component.get("amount"),
                component.get("basis"),
                component.get("formula"),
            )
            for component in values
            if isinstance(component, dict)
        ]
    return []


def _run_measurement_harness(*, use_tracemalloc: bool) -> dict:
    checkpoints = []
    timings = {}
    memory_meta = None
    result_meta = None
    comparison_id = f"cmp-measure-100x3-{'trace' if use_tracemalloc else 'plain'}"
    fingerprint = f"fp-{comparison_id}-0123456789abcdef0123456789abcdef"

    def mark(label: str) -> None:
        checkpoints.append({"label": label, "timestamp": _utc_checkpoint()})

    def measure(label: str, fn):
        started = time.perf_counter()
        value = fn()
        timings[label] = (time.perf_counter() - started) * 1000
        return value

    exec_service.hydrate_memory_item = hydrate_memory_item
    started_total = time.perf_counter()
    traced_peak = None
    if use_tracemalloc:
        tracemalloc.start()
    try:
        imports = measure(
            "imports_and_initialization_ms",
            lambda: {
                "compact": compact_comparison_result_for_storage,
                "rehydrate": _rehydrate_compact_result,
                "canonical": _canonical_json_bytes,
            },
        )
        mark("inicializacao concluida")
        fixture = measure("fixture_creation_ms", lambda: _build_measurement_fixture_100x3(comparison_id=comparison_id))
        mark("fixture concluida")
        raw_result_size_bytes = measure("raw_size_serialization_ms", lambda: len(_canonical_json_bytes(fixture)))
        mark("medicao bruta concluida")
        compacted = measure("compaction_ms", lambda: compact_comparison_result_for_storage(fixture))
        compact_result = compacted["compact_result"]
        memory_payload = compacted["memory_payload"]
        mark("compactacao concluida")
        memory_payload_ref = measure("memory_payload_materialization_ms", lambda: memory_payload)
        compact_probe_bytes = measure("compact_serialization_ms", lambda: len(_canonical_json_bytes(compact_result)))
        memory_probe_bytes = measure("memory_serialization_probe_ms", lambda: len(_canonical_json_bytes(memory_payload_ref)))
        coverage_items_ok = measure(
            "coverage_items_scan_ms",
            lambda: all(not _walk_has_coverage_rows(item) for item in memory_payload_ref.get("items", {}).values()),
        )
        coverage_catalog_ok = measure(
            "coverage_catalog_scan_ms",
            lambda: all(not _walk_has_coverage_rows(ev) for ev in memory_payload_ref.get("evidence_catalog", {}).values()),
        )
        evidence_stats = measure("evidence_ref_count_ms", lambda: _count_evidence_refs(memory_payload_ref))
        mark("validacoes estruturais concluidas")
        memory_meta = measure(
            "memory_save_ms",
            lambda: save_comparison_calculation_memory_payload(
                comparison_id=comparison_id,
                fingerprint=fingerprint,
                memory_payload=memory_payload_ref,
            ),
        )
        result_meta = measure(
            "result_save_ms",
            lambda: save_comparison_calculation_result(
                comparison_id=comparison_id,
                fingerprint=fingerprint,
                result=compact_result,
                memory_storage_meta=memory_meta,
            ),
        )
        mark("storage concluido")
        loaded_memory = measure(
            "memory_load_ms",
            lambda: load_comparison_calculation_memory_payload(
                storage_key=memory_meta["memory_storage_key"],
                comparison_id=comparison_id,
                fingerprint=fingerprint,
                expected_checksum=memory_meta["memory_checksum"],
            ),
        )
        loaded_result = measure(
            "result_load_ms",
            lambda: load_comparison_calculation_result(
                storage_key=result_meta["result_storage_key"],
                comparison_id=comparison_id,
                fingerprint=fingerprint,
                expected_checksum=result_meta["result_checksum"],
            ),
        )
        mark("load concluido")
        rehydrated = measure("rehydration_ms", lambda: _rehydrate_compact_result(loaded_result, loaded_memory))
        mark("reidratacao concluida")
        indexes = measure(
            "comparison_index_build_ms",
            lambda: (_index_cells(fixture), _index_cells(compact_result), _index_cells(rehydrated)),
        )

        def _compare() -> None:
            original_index, compact_index, hydrated_index = indexes
            for memory_ref, original_cell in original_index.items():
                compact_cell = compact_index[memory_ref]
                hydrated_cell = hydrated_index[memory_ref]
                assert compact_cell["memory_ref"] == memory_ref
                assert compact_cell["status"] == original_cell["status"]
                assert compact_cell["final_status"] == original_cell["final_status"]
                assert hydrated_cell["status"] == original_cell["status"]
                assert (hydrated_cell.get("calculation_memory") or {}).get("status") == (
                    original_cell.get("calculation_memory") or {}
                ).get("status")
                assert (hydrated_cell.get("calculation_memory") or {}).get("total") == (
                    original_cell.get("calculation_memory") or {}
                ).get("total")
                assert _essential_components(hydrated_cell) == _essential_components(original_cell)

        measure("comparison_value_status_ms", _compare)
        mark("comparacao concluida")
    finally:
        if use_tracemalloc:
            _current, peak = tracemalloc.get_traced_memory()
            traced_peak = peak
            tracemalloc.stop()
    cleanup_started = time.perf_counter()
    if result_meta is not None:
        delete_comparison_calculation_result(result_meta["result_storage_key"])
    if memory_meta is not None:
        delete_comparison_calculation_memories(memory_meta["memory_storage_key"])
    timings["cleanup_ms"] = (time.perf_counter() - cleanup_started) * 1000
    gc_started = time.perf_counter()
    gc.collect()
    timings["garbage_collection_ms"] = (time.perf_counter() - gc_started) * 1000
    timings["tracemalloc_overhead_ms"] = 0.0
    mark("cleanup concluido")
    total_ms = (time.perf_counter() - started_total) * 1000
    instrumented_total_ms = sum(timings.values())
    productive_ms = sum(
        timings[key]
        for key in (
            "fixture_creation_ms",
            "compaction_ms",
            "memory_save_ms",
            "result_save_ms",
            "memory_load_ms",
            "result_load_ms",
            "rehydration_ms",
            "comparison_index_build_ms",
            "comparison_value_status_ms",
            "cleanup_ms",
            "garbage_collection_ms",
        )
    )
    diagnostic_ms = total_ms - productive_ms
    return {
        "mode": "with_tracemalloc" if use_tracemalloc else "without_tracemalloc",
        "checkpoints": checkpoints,
        "timings_ms": timings,
        "total_ms": total_ms,
        "instrumented_total_ms": instrumented_total_ms,
        "instrumented_coverage_ratio": instrumented_total_ms / total_ms if total_ms else 1.0,
        "productive_ms": productive_ms,
        "diagnostic_ms": diagnostic_ms,
        "raw_result_size_bytes": raw_result_size_bytes,
        "compact_result_size_bytes": result_meta["result_size_bytes"],
        "memory_payload_size_bytes": memory_meta["memory_size_bytes"],
        "compact_serialized_probe_bytes": compact_probe_bytes,
        "memory_serialized_probe_bytes": memory_probe_bytes,
        "coverage_items_ok": coverage_items_ok,
        "coverage_catalog_ok": coverage_catalog_ok,
        "evidence_stats": evidence_stats,
        "tracemalloc_peak_bytes": traced_peak,
        "imports_loaded": bool(imports),
    }


def test_measurement_harness_100x3():
    without_trace = _run_measurement_harness(use_tracemalloc=False)
    with_trace = _run_measurement_harness(use_tracemalloc=True)

    assert without_trace["instrumented_coverage_ratio"] >= 0.95
    assert without_trace["productive_ms"] < 10_000
    assert without_trace["memory_payload_size_bytes"] < 24 * 1024 * 1024
    assert without_trace["compact_result_size_bytes"] < 16 * 1024 * 1024
    assert without_trace["coverage_items_ok"] is True
    assert without_trace["coverage_catalog_ok"] is True
    assert without_trace["evidence_stats"]["total_refs"] > 0
    assert with_trace["tracemalloc_peak_bytes"] is not None
    print(json.dumps({"without_tracemalloc": without_trace, "with_tracemalloc": with_trace}, ensure_ascii=False, indent=2))
