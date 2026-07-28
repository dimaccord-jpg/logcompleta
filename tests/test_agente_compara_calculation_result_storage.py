"""Storage dedicado do resultado comparativo (correção Etapa 5)."""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from app.agente_compara_calculation_result_storage import (
    ERROR_RESULT_CORRUPT,
    ERROR_RESULT_FORBIDDEN_FIELD,
    ERROR_RESULT_IDENTITY_MISMATCH,
    ERROR_RESULT_MISSING,
    ERROR_RESULT_STORAGE_KEY_INVALID,
    ERROR_RESULT_TOO_LARGE,
    RESULT_MAX_BYTES,
    AgenteComparaCalculationResultStorageError,
    build_result_storage_key,
    delete_comparison_calculation_result,
    load_comparison_calculation_result,
    resolve_result_storage_path,
    save_comparison_calculation_result,
)
from tests.cleiton_doc_fixtures import patch_cleiton_doc_store


FP = "a" * 64
CMP = "cmp-storage-test-001"


def _minimal_result(**overrides):
    base = {
        "schema_version": 1,
        "comparison_id": CMP,
        "table_count": 1,
        "row_count": 0,
        "tables": [{"table_id": "t1", "slot_number": 1, "carrier_name": "A"}],
        "comparative_rows": [],
        "results_by_table": {"t1": {"rows": []}},
        "summary": {
            "calculated_cell_count": 0,
            "error_cell_count": 0,
            "total_calculation_cells": 0,
        },
    }
    base.update(overrides)
    return base


@pytest.fixture
def storage_env(tmp_path, monkeypatch):
    patch_cleiton_doc_store(tmp_path, monkeypatch)
    return tmp_path


def test_save_atomic_checksum_size_and_read(storage_env):
    result = _minimal_result()
    meta = save_comparison_calculation_result(
        comparison_id=CMP,
        fingerprint=FP,
        result=result,
    )
    assert meta["result_storage_key"]
    assert meta["result_checksum"]
    assert meta["result_size_bytes"] > 0
    assert ":" not in meta["result_storage_key"]
    assert "\\" not in meta["result_storage_key"]
    assert "/" not in meta["result_storage_key"]
    path = resolve_result_storage_path(meta["result_storage_key"])
    assert path.is_file()
    loaded = load_comparison_calculation_result(
        storage_key=meta["result_storage_key"],
        comparison_id=CMP,
        fingerprint=FP,
        expected_checksum=meta["result_checksum"],
    )
    assert loaded["table_count"] == 1
    assert loaded["comparative_rows"] == []


def test_missing_file(storage_env):
    unique_cmp = "cmp-missing-only"
    unique_fp = "c" * 64
    key = build_result_storage_key(comparison_id=unique_cmp, fingerprint=unique_fp)
    with pytest.raises(AgenteComparaCalculationResultStorageError) as exc:
        load_comparison_calculation_result(
            storage_key=key,
            comparison_id=unique_cmp,
            fingerprint=unique_fp,
        )
    assert exc.value.error_code == ERROR_RESULT_MISSING


def test_corrupt_json(storage_env):
    meta = save_comparison_calculation_result(
        comparison_id=CMP, fingerprint=FP, result=_minimal_result()
    )
    path = resolve_result_storage_path(meta["result_storage_key"])
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(AgenteComparaCalculationResultStorageError) as exc:
        load_comparison_calculation_result(
            storage_key=meta["result_storage_key"],
            comparison_id=CMP,
            fingerprint=FP,
        )
    assert exc.value.error_code == ERROR_RESULT_CORRUPT


def test_checksum_mismatch(storage_env):
    meta = save_comparison_calculation_result(
        comparison_id=CMP, fingerprint=FP, result=_minimal_result()
    )
    path = resolve_result_storage_path(meta["result_storage_key"])
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["result"]["row_count"] = 99
    path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(AgenteComparaCalculationResultStorageError) as exc:
        load_comparison_calculation_result(
            storage_key=meta["result_storage_key"],
            comparison_id=CMP,
            fingerprint=FP,
            expected_checksum=meta["result_checksum"],
        )
    assert exc.value.error_code == ERROR_RESULT_CORRUPT


def test_schema_mismatch(storage_env):
    meta = save_comparison_calculation_result(
        comparison_id=CMP, fingerprint=FP, result=_minimal_result()
    )
    path = resolve_result_storage_path(meta["result_storage_key"])
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["schema_version"] = 999
    path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(AgenteComparaCalculationResultStorageError) as exc:
        load_comparison_calculation_result(
            storage_key=meta["result_storage_key"],
            comparison_id=CMP,
            fingerprint=FP,
        )
    assert exc.value.error_code == ERROR_RESULT_CORRUPT


def test_comparison_id_mismatch(storage_env):
    meta = save_comparison_calculation_result(
        comparison_id=CMP, fingerprint=FP, result=_minimal_result()
    )
    with pytest.raises(AgenteComparaCalculationResultStorageError) as exc:
        load_comparison_calculation_result(
            storage_key=meta["result_storage_key"],
            comparison_id="other-cmp",
            fingerprint=FP,
        )
    assert exc.value.error_code == ERROR_RESULT_IDENTITY_MISMATCH


def test_fingerprint_mismatch(storage_env):
    meta = save_comparison_calculation_result(
        comparison_id=CMP, fingerprint=FP, result=_minimal_result()
    )
    with pytest.raises(AgenteComparaCalculationResultStorageError) as exc:
        load_comparison_calculation_result(
            storage_key=meta["result_storage_key"],
            comparison_id=CMP,
            fingerprint="b" * 64,
        )
    assert exc.value.error_code == ERROR_RESULT_IDENTITY_MISMATCH


def test_forbidden_field_rejected(storage_env):
    bad = _minimal_result()
    bad["winner"] = "x"
    with pytest.raises(AgenteComparaCalculationResultStorageError) as exc:
        save_comparison_calculation_result(
            comparison_id=CMP, fingerprint=FP, result=bad
        )
    assert exc.value.error_code == ERROR_RESULT_FORBIDDEN_FIELD


def test_nan_infinity_rejected(storage_env):
    bad = _minimal_result()
    bad["summary"]["bad"] = float("nan")
    with pytest.raises(AgenteComparaCalculationResultStorageError):
        save_comparison_calculation_result(
            comparison_id=CMP, fingerprint=FP, result=bad
        )
    bad2 = _minimal_result()
    bad2["summary"]["bad"] = float("inf")
    with pytest.raises(AgenteComparaCalculationResultStorageError):
        save_comparison_calculation_result(
            comparison_id=CMP, fingerprint=FP, result=bad2
        )
    assert math.isnan(float("nan"))


def test_oversize_rejected(storage_env, monkeypatch):
    monkeypatch.setattr(
        "app.agente_compara_calculation_result_storage.RESULT_MAX_BYTES",
        200,
    )
    big = _minimal_result(comparative_rows=[{"pad": "x" * 500}])
    with pytest.raises(AgenteComparaCalculationResultStorageError) as exc:
        save_comparison_calculation_result(
            comparison_id=CMP, fingerprint=FP, result=big
        )
    assert exc.value.error_code == ERROR_RESULT_TOO_LARGE


def test_invalid_key_and_traversal(storage_env):
    with pytest.raises(AgenteComparaCalculationResultStorageError) as exc:
        resolve_result_storage_path("../etc/passwd")
    assert exc.value.error_code == ERROR_RESULT_STORAGE_KEY_INVALID
    with pytest.raises(AgenteComparaCalculationResultStorageError):
        resolve_result_storage_path("cc_result_../../x")


def test_delete_removes_file(storage_env):
    meta = save_comparison_calculation_result(
        comparison_id=CMP, fingerprint=FP, result=_minimal_result()
    )
    path = resolve_result_storage_path(meta["result_storage_key"])
    assert path.is_file()
    assert delete_comparison_calculation_result(meta["result_storage_key"]) is True
    assert not path.is_file()


def test_key_stable_for_same_identity(storage_env):
    k1 = build_result_storage_key(comparison_id=CMP, fingerprint=FP)
    k2 = build_result_storage_key(comparison_id=CMP, fingerprint=FP)
    assert k1 == k2
    assert k1.startswith("cc_result_")
