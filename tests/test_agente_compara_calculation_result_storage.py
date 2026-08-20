"""Storage dedicado do resultado comparativo (correção Etapa 5)."""
from __future__ import annotations

import json
import math
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.agente_compara_calculation_result_storage import (
    AGENTE_COMPARA_CALC_STORAGE_TTL_HOURS,
    ERROR_MEMORY_MISSING,
    ERROR_RESULT_CORRUPT,
    ERROR_RESULT_FORBIDDEN_FIELD,
    ERROR_RESULT_IDENTITY_MISMATCH,
    ERROR_RESULT_MISSING,
    ERROR_RESULT_STORAGE_KEY_INVALID,
    ERROR_RESULT_TOO_LARGE,
    ERROR_MEMORY_TOO_LARGE,
    ERROR_MEMORY_SERIALIZATION_FAILED,
    AgenteComparaCalculationResultStorageError,
    build_calc_storage_retention_window,
    build_result_storage_key,
    delete_comparison_calculation_result,
    load_comparison_calculation_result,
    maybe_cleanup_expired_calculation_storage,
    resolve_result_storage_path,
    save_comparison_calculation_result,
)
from tests.cleiton_doc_fixtures import patch_cleiton_doc_store


def _unique_identity(prefix: str) -> tuple[str, str]:
    """comparison_id + fingerprint exclusivos por invocação (evita colisão em dir compartilhado)."""
    token = uuid.uuid4().hex
    cmp_id = f"cmp-{prefix}-{token[:16]}"
    fingerprint = (token * 3)[:64]
    return cmp_id, fingerprint


def _minimal_result(comparison_id: str, **overrides):
    base = {
        "schema_version": 1,
        "comparison_id": comparison_id,
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
    # Binding local no módulo de storage: patch só em cleiton_doc_store NÃO basta.
    patch_cleiton_doc_store(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "app.agente_compara_calculation_result_storage.get_cleiton_doc_tmp_dir",
        lambda: str(tmp_path),
    )
    import app.agente_compara_calculation_result_storage as storage_mod

    monkeypatch.setattr(storage_mod, "_calc_storage_sweep_monotonic", time.monotonic())
    calc_dir = tmp_path / "agente_compara_calc"
    return {"tmp_path": tmp_path, "calc_dir": calc_dir}


def test_save_atomic_checksum_size_and_read(storage_env):
    cmp_id, fp = _unique_identity("save")
    result = _minimal_result(cmp_id)
    meta = save_comparison_calculation_result(
        comparison_id=cmp_id,
        fingerprint=fp,
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
    assert storage_env["tmp_path"] in path.parents
    loaded = load_comparison_calculation_result(
        storage_key=meta["result_storage_key"],
        comparison_id=cmp_id,
        fingerprint=fp,
        expected_checksum=meta["result_checksum"],
    )
    assert loaded["table_count"] == 1
    assert loaded["comparative_rows"] == []


def test_missing_file(storage_env):
    cmp_id, fp = _unique_identity("missing")
    key = build_result_storage_key(comparison_id=cmp_id, fingerprint=fp)
    path = resolve_result_storage_path(key)
    assert not path.is_file()
    assert storage_env["tmp_path"] in path.parents
    with pytest.raises(AgenteComparaCalculationResultStorageError) as exc:
        load_comparison_calculation_result(
            storage_key=key,
            comparison_id=cmp_id,
            fingerprint=fp,
        )
    assert exc.value.error_code == ERROR_RESULT_MISSING


def test_corrupt_json(storage_env):
    cmp_id, fp = _unique_identity("corrupt")
    meta = save_comparison_calculation_result(
        comparison_id=cmp_id, fingerprint=fp, result=_minimal_result(cmp_id)
    )
    path = resolve_result_storage_path(meta["result_storage_key"])
    assert path.is_file()
    assert storage_env["tmp_path"] in path.parents
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(AgenteComparaCalculationResultStorageError) as exc:
        load_comparison_calculation_result(
            storage_key=meta["result_storage_key"],
            comparison_id=cmp_id,
            fingerprint=fp,
        )
    assert exc.value.error_code == ERROR_RESULT_CORRUPT


def test_checksum_mismatch(storage_env):
    cmp_id, fp = _unique_identity("checksum")
    meta = save_comparison_calculation_result(
        comparison_id=cmp_id, fingerprint=fp, result=_minimal_result(cmp_id)
    )
    path = resolve_result_storage_path(meta["result_storage_key"])
    assert path.is_file(), f"arquivo ausente após save: {path}"
    assert storage_env["tmp_path"] in path.parents
    assert path == resolve_result_storage_path(meta["result_storage_key"])

    envelope = json.loads(path.read_text(encoding="utf-8"))
    original_checksum = envelope["result_checksum"]
    assert original_checksum == meta["result_checksum"]
    envelope["result"]["row_count"] = 99
    # NÃO recalcula checksum — adulteração proposital do resultado.
    path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    assert path.is_file(), f"arquivo ausente após adulteração: {path}"

    with pytest.raises(AgenteComparaCalculationResultStorageError) as exc:
        load_comparison_calculation_result(
            storage_key=meta["result_storage_key"],
            comparison_id=cmp_id,
            fingerprint=fp,
            expected_checksum=meta["result_checksum"],
        )
    assert exc.value.error_code == ERROR_RESULT_CORRUPT
    assert path.is_file()


def test_schema_mismatch(storage_env):
    cmp_id, fp = _unique_identity("schema")
    meta = save_comparison_calculation_result(
        comparison_id=cmp_id, fingerprint=fp, result=_minimal_result(cmp_id)
    )
    path = resolve_result_storage_path(meta["result_storage_key"])
    assert path.is_file(), f"arquivo ausente após save: {path}"
    assert storage_env["tmp_path"] in path.parents

    envelope = json.loads(path.read_text(encoding="utf-8"))
    assert envelope.get("schema_version") == 2
    envelope["schema_version"] = 999
    path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")

    # Confirma que o arquivo adulterado é o mesmo path que o load usará.
    assert resolve_result_storage_path(meta["result_storage_key"]) == path
    reloaded_envelope = json.loads(path.read_text(encoding="utf-8"))
    assert reloaded_envelope.get("schema_version") == 999

    with pytest.raises(AgenteComparaCalculationResultStorageError) as exc:
        load_comparison_calculation_result(
            storage_key=meta["result_storage_key"],
            comparison_id=cmp_id,
            fingerprint=fp,
        )
    assert exc.value.error_code == ERROR_RESULT_CORRUPT


def test_comparison_id_mismatch(storage_env):
    cmp_id, fp = _unique_identity("idmis")
    meta = save_comparison_calculation_result(
        comparison_id=cmp_id, fingerprint=fp, result=_minimal_result(cmp_id)
    )
    with pytest.raises(AgenteComparaCalculationResultStorageError) as exc:
        load_comparison_calculation_result(
            storage_key=meta["result_storage_key"],
            comparison_id="other-cmp",
            fingerprint=fp,
        )
    assert exc.value.error_code == ERROR_RESULT_IDENTITY_MISMATCH


def test_fingerprint_mismatch(storage_env):
    cmp_id, fp = _unique_identity("fpmis")
    meta = save_comparison_calculation_result(
        comparison_id=cmp_id, fingerprint=fp, result=_minimal_result(cmp_id)
    )
    other_fp = "b" * 64
    with pytest.raises(AgenteComparaCalculationResultStorageError) as exc:
        load_comparison_calculation_result(
            storage_key=meta["result_storage_key"],
            comparison_id=cmp_id,
            fingerprint=other_fp,
        )
    assert exc.value.error_code == ERROR_RESULT_IDENTITY_MISMATCH


def test_forbidden_field_rejected(storage_env):
    cmp_id, fp = _unique_identity("forbid")
    bad = _minimal_result(cmp_id)
    bad["winner"] = "x"
    with pytest.raises(AgenteComparaCalculationResultStorageError) as exc:
        save_comparison_calculation_result(
            comparison_id=cmp_id, fingerprint=fp, result=bad
        )
    assert exc.value.error_code == ERROR_RESULT_FORBIDDEN_FIELD


def test_nan_infinity_rejected(storage_env):
    cmp_id, fp = _unique_identity("nan")
    bad = _minimal_result(cmp_id)
    bad["summary"]["bad"] = float("nan")
    with pytest.raises(AgenteComparaCalculationResultStorageError):
        save_comparison_calculation_result(
            comparison_id=cmp_id, fingerprint=fp, result=bad
        )
    bad2 = _minimal_result(cmp_id)
    bad2["summary"]["bad"] = float("inf")
    with pytest.raises(AgenteComparaCalculationResultStorageError):
        save_comparison_calculation_result(
            comparison_id=cmp_id, fingerprint=fp, result=bad2
        )
    assert math.isnan(float("nan"))


def test_oversize_rejected(storage_env, monkeypatch):
    cmp_id, fp = _unique_identity("oversize")
    monkeypatch.setattr(
        "app.agente_compara_calculation_result_storage.RESULT_MAX_BYTES",
        200,
    )
    big = _minimal_result(cmp_id, comparative_rows=[{"pad": "x" * 500}])
    with pytest.raises(AgenteComparaCalculationResultStorageError) as exc:
        save_comparison_calculation_result(
            comparison_id=cmp_id, fingerprint=fp, result=big
        )
    assert exc.value.error_code == ERROR_RESULT_TOO_LARGE


def test_invalid_key_and_traversal(storage_env):
    with pytest.raises(AgenteComparaCalculationResultStorageError) as exc:
        resolve_result_storage_path("../etc/passwd")
    assert exc.value.error_code == ERROR_RESULT_STORAGE_KEY_INVALID
    with pytest.raises(AgenteComparaCalculationResultStorageError):
        resolve_result_storage_path("cc_result_../../x")


def test_delete_removes_file(storage_env):
    cmp_id, fp = _unique_identity("delete")
    meta = save_comparison_calculation_result(
        comparison_id=cmp_id, fingerprint=fp, result=_minimal_result(cmp_id)
    )
    path = resolve_result_storage_path(meta["result_storage_key"])
    assert path.is_file()
    assert storage_env["tmp_path"] in path.parents
    assert delete_comparison_calculation_result(meta["result_storage_key"]) is True
    assert not path.is_file()


def test_key_stable_for_same_identity(storage_env):
    cmp_id, fp = _unique_identity("stable")
    k1 = build_result_storage_key(comparison_id=cmp_id, fingerprint=fp)
    k2 = build_result_storage_key(comparison_id=cmp_id, fingerprint=fp)
    assert k1 == k2
    assert k1.startswith("cc_result_")


def test_memory_payload_roundtrip(storage_env):
    cmp_id, fp = _unique_identity("memory")
    payload = {
        "schema_version": 2,
        "comparison_id": cmp_id,
        "items": {"t1:1": {"memory_ref": "t1:1", "table_id": "t1", "row_index": 1, "calculation_memory": {"status": "calculated"}}},
    }
    from app.agente_compara_calculation_result_storage import save_comparison_calculation_memory_payload, load_comparison_calculation_memory_payload, resolve_memory_storage_path
    meta = save_comparison_calculation_memory_payload(comparison_id=cmp_id, fingerprint=fp, memory_payload=payload)
    assert resolve_memory_storage_path(meta["memory_storage_key"]).is_file()
    loaded = load_comparison_calculation_memory_payload(
        storage_key=meta["memory_storage_key"],
        comparison_id=cmp_id,
        fingerprint=fp,
        expected_checksum=meta["memory_checksum"],
    )
    assert loaded["items"]["t1:1"]["table_id"] == "t1"


def test_memory_payload_oversize_rejected(storage_env, monkeypatch):
    from app.agente_compara_calculation_result_storage import save_comparison_calculation_memory_payload
    cmp_id, fp = _unique_identity("mem-oversize")
    monkeypatch.setattr(
        "app.agente_compara_calculation_result_storage.MEMORY_MAX_BYTES",
        200,
    )
    payload = {
        "schema_version": 2,
        "comparison_id": cmp_id,
        "items": {"t1:1": {"memory_ref": "t1:1", "table_id": "t1", "row_index": 1, "calculation_memory": {"payload": "x" * 500}}},
    }
    with pytest.raises(AgenteComparaCalculationResultStorageError) as exc:
        save_comparison_calculation_memory_payload(comparison_id=cmp_id, fingerprint=fp, memory_payload=payload)
    assert exc.value.error_code == ERROR_MEMORY_TOO_LARGE
    assert exc.value.artifact_type == "memory"


def test_memory_payload_non_serializable_reports_specific_error(storage_env):
    from app.agente_compara_calculation_result_storage import save_comparison_calculation_memory_payload
    cmp_id, fp = _unique_identity("mem-serialize")
    payload = {
        "schema_version": 2,
        "comparison_id": cmp_id,
        "items": {"t1:1": {"memory_ref": "t1:1", "table_id": "t1", "row_index": 1, "calculation_memory": {"bad": float("nan")}}},
    }
    with pytest.raises(AgenteComparaCalculationResultStorageError) as exc:
        save_comparison_calculation_memory_payload(comparison_id=cmp_id, fingerprint=fp, memory_payload=payload)
    assert exc.value.error_code == ERROR_MEMORY_SERIALIZATION_FAILED
    assert exc.value.artifact_type == "memory"


def _minimal_memory(comparison_id: str) -> dict:
    return {
        "schema_version": 2,
        "comparison_id": comparison_id,
        "items": {
            "t1:1": {
                "memory_ref": "t1:1",
                "table_id": "t1",
                "row_index": 1,
                "calculation_memory": {"status": "calculated"},
            }
        },
    }


def _freeze_storage_clock(monkeypatch, when: datetime):
    import app.agente_compara_calculation_result_storage as storage_mod

    monkeypatch.setattr(storage_mod, "_utcnow", lambda: when)


def test_ttl_constant_is_48_hours_and_centralized():
    assert AGENTE_COMPARA_CALC_STORAGE_TTL_HOURS == 48
    source = Path("app/agente_compara_calculation_result_storage.py").read_text(encoding="utf-8")
    assert source.count("AGENTE_COMPARA_CALC_STORAGE_TTL_HOURS") >= 2
    assert "timedelta(hours=48)" not in source


def test_save_result_envelope_has_retention_timestamps(storage_env, monkeypatch):
    fixed = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)
    _freeze_storage_clock(monkeypatch, fixed)
    cmp_id, fp = _unique_identity("ttl-result")
    meta = save_comparison_calculation_result(
        comparison_id=cmp_id, fingerprint=fp, result=_minimal_result(cmp_id)
    )
    expected = build_calc_storage_retention_window(created_at=fixed)
    assert meta["created_at"] == expected["created_at"]
    assert meta["expires_at"] == expected["expires_at"]
    path = resolve_result_storage_path(meta["result_storage_key"])
    envelope = json.loads(path.read_text(encoding="utf-8"))
    assert envelope["created_at"] == expected["created_at"]
    assert envelope["expires_at"] == expected["expires_at"]
    created = datetime.fromisoformat(envelope["created_at"].replace("Z", "+00:00"))
    expires = datetime.fromisoformat(envelope["expires_at"].replace("Z", "+00:00"))
    assert expires - created == timedelta(hours=AGENTE_COMPARA_CALC_STORAGE_TTL_HOURS)


def test_save_memory_envelope_has_retention_timestamps(storage_env, monkeypatch):
    from app.agente_compara_calculation_result_storage import (
        resolve_memory_storage_path,
        save_comparison_calculation_memory_payload,
    )

    fixed = datetime(2026, 8, 18, 15, 30, 0, tzinfo=timezone.utc)
    _freeze_storage_clock(monkeypatch, fixed)
    cmp_id, fp = _unique_identity("ttl-memory")
    meta = save_comparison_calculation_memory_payload(
        comparison_id=cmp_id, fingerprint=fp, memory_payload=_minimal_memory(cmp_id)
    )
    expected = build_calc_storage_retention_window(created_at=fixed)
    assert meta["created_at"] == expected["created_at"]
    assert meta["expires_at"] == expected["expires_at"]
    envelope = json.loads(resolve_memory_storage_path(meta["memory_storage_key"]).read_text(encoding="utf-8"))
    assert envelope["created_at"] == expected["created_at"]
    assert envelope["expires_at"] == expected["expires_at"]


def test_result_and_memory_share_the_same_retention_window(storage_env, monkeypatch):
    from app.agente_compara_calculation_result_storage import (
        resolve_memory_storage_path,
        save_comparison_calculation_memory_payload,
    )

    fixed = datetime(2026, 8, 18, 9, 0, 0, tzinfo=timezone.utc)
    _freeze_storage_clock(monkeypatch, fixed)
    cmp_id, fp = _unique_identity("ttl-pair")
    retention = build_calc_storage_retention_window()
    memory_meta = save_comparison_calculation_memory_payload(
        comparison_id=cmp_id,
        fingerprint=fp,
        memory_payload=_minimal_memory(cmp_id),
        created_at=retention["created_at"],
        expires_at=retention["expires_at"],
    )
    result_meta = save_comparison_calculation_result(
        comparison_id=cmp_id,
        fingerprint=fp,
        result=_minimal_result(cmp_id),
        memory_storage_meta=memory_meta,
        created_at=retention["created_at"],
        expires_at=retention["expires_at"],
    )
    result_env = json.loads(resolve_result_storage_path(result_meta["result_storage_key"]).read_text(encoding="utf-8"))
    memory_env = json.loads(resolve_memory_storage_path(memory_meta["memory_storage_key"]).read_text(encoding="utf-8"))
    assert result_env["created_at"] == memory_env["created_at"] == retention["created_at"]
    assert result_env["expires_at"] == memory_env["expires_at"] == retention["expires_at"]


def test_load_result_and_memory_before_ttl(storage_env, monkeypatch):
    from app.agente_compara_calculation_result_storage import (
        load_comparison_calculation_memory_payload,
        save_comparison_calculation_memory_payload,
    )

    created = datetime(2026, 8, 18, 10, 0, 0, tzinfo=timezone.utc)
    _freeze_storage_clock(monkeypatch, created)
    cmp_id, fp = _unique_identity("ttl-before")
    retention = build_calc_storage_retention_window()
    memory_meta = save_comparison_calculation_memory_payload(
        comparison_id=cmp_id,
        fingerprint=fp,
        memory_payload=_minimal_memory(cmp_id),
        created_at=retention["created_at"],
        expires_at=retention["expires_at"],
    )
    result_meta = save_comparison_calculation_result(
        comparison_id=cmp_id,
        fingerprint=fp,
        result=_minimal_result(cmp_id),
        memory_storage_meta=memory_meta,
        created_at=retention["created_at"],
        expires_at=retention["expires_at"],
    )
    _freeze_storage_clock(monkeypatch, created + timedelta(hours=47))
    loaded_result = load_comparison_calculation_result(
        storage_key=result_meta["result_storage_key"],
        comparison_id=cmp_id,
        fingerprint=fp,
        expected_checksum=result_meta["result_checksum"],
    )
    loaded_memory = load_comparison_calculation_memory_payload(
        storage_key=memory_meta["memory_storage_key"],
        comparison_id=cmp_id,
        fingerprint=fp,
        expected_checksum=memory_meta["memory_checksum"],
    )
    assert loaded_result["comparison_id"] == cmp_id
    assert loaded_memory["items"]["t1:1"]["table_id"] == "t1"


def test_load_result_after_ttl_does_not_return_content(storage_env, monkeypatch):
    created = datetime(2026, 8, 18, 10, 0, 0, tzinfo=timezone.utc)
    _freeze_storage_clock(monkeypatch, created)
    cmp_id, fp = _unique_identity("ttl-after")
    meta = save_comparison_calculation_result(
        comparison_id=cmp_id, fingerprint=fp, result=_minimal_result(cmp_id)
    )
    path = resolve_result_storage_path(meta["result_storage_key"])
    assert path.is_file()
    _freeze_storage_clock(monkeypatch, created + timedelta(hours=49))
    with pytest.raises(AgenteComparaCalculationResultStorageError) as exc:
        load_comparison_calculation_result(
            storage_key=meta["result_storage_key"],
            comparison_id=cmp_id,
            fingerprint=fp,
        )
    assert exc.value.error_code == ERROR_RESULT_MISSING
    assert not path.is_file()


def test_load_memory_after_ttl_does_not_return_content(storage_env, monkeypatch):
    from app.agente_compara_calculation_result_storage import (
        load_comparison_calculation_memory_payload,
        resolve_memory_storage_path,
        save_comparison_calculation_memory_payload,
    )

    created = datetime(2026, 8, 18, 10, 0, 0, tzinfo=timezone.utc)
    _freeze_storage_clock(monkeypatch, created)
    cmp_id, fp = _unique_identity("ttl-mem-after")
    meta = save_comparison_calculation_memory_payload(
        comparison_id=cmp_id, fingerprint=fp, memory_payload=_minimal_memory(cmp_id)
    )
    path = resolve_memory_storage_path(meta["memory_storage_key"])
    assert path.is_file()
    _freeze_storage_clock(monkeypatch, created + timedelta(hours=49))
    with pytest.raises(AgenteComparaCalculationResultStorageError) as exc:
        load_comparison_calculation_memory_payload(
            storage_key=meta["memory_storage_key"],
            comparison_id=cmp_id,
            fingerprint=fp,
        )
    assert exc.value.error_code == ERROR_MEMORY_MISSING
    assert not path.is_file()


def test_result_and_memory_expire_together(storage_env, monkeypatch):
    from app.agente_compara_calculation_result_storage import (
        resolve_memory_storage_path,
        save_comparison_calculation_memory_payload,
    )

    created = datetime(2026, 8, 18, 10, 0, 0, tzinfo=timezone.utc)
    _freeze_storage_clock(monkeypatch, created)
    cmp_id, fp = _unique_identity("ttl-together")
    retention = build_calc_storage_retention_window()
    memory_meta = save_comparison_calculation_memory_payload(
        comparison_id=cmp_id,
        fingerprint=fp,
        memory_payload=_minimal_memory(cmp_id),
        created_at=retention["created_at"],
        expires_at=retention["expires_at"],
    )
    result_meta = save_comparison_calculation_result(
        comparison_id=cmp_id,
        fingerprint=fp,
        result=_minimal_result(cmp_id),
        memory_storage_meta=memory_meta,
        created_at=retention["created_at"],
        expires_at=retention["expires_at"],
    )
    result_path = resolve_result_storage_path(result_meta["result_storage_key"])
    memory_path = resolve_memory_storage_path(memory_meta["memory_storage_key"])
    assert result_path.is_file()
    assert memory_path.is_file()
    _freeze_storage_clock(monkeypatch, created + timedelta(hours=49))
    with pytest.raises(AgenteComparaCalculationResultStorageError) as exc:
        load_comparison_calculation_result(
            storage_key=result_meta["result_storage_key"],
            comparison_id=cmp_id,
            fingerprint=fp,
        )
    assert exc.value.error_code == ERROR_RESULT_MISSING
    assert not result_path.is_file()
    assert not memory_path.is_file()


def test_expired_storage_still_requires_identity(storage_env, monkeypatch):
    created = datetime(2026, 8, 18, 10, 0, 0, tzinfo=timezone.utc)
    _freeze_storage_clock(monkeypatch, created)
    cmp_id, fp = _unique_identity("ttl-identity")
    meta = save_comparison_calculation_result(
        comparison_id=cmp_id, fingerprint=fp, result=_minimal_result(cmp_id)
    )
    path = resolve_result_storage_path(meta["result_storage_key"])
    _freeze_storage_clock(monkeypatch, created + timedelta(hours=49))
    with pytest.raises(AgenteComparaCalculationResultStorageError) as exc:
        load_comparison_calculation_result(
            storage_key=meta["result_storage_key"],
            comparison_id="other-cmp",
            fingerprint=fp,
        )
    assert exc.value.error_code == ERROR_RESULT_IDENTITY_MISMATCH
    assert path.is_file()
    with pytest.raises(AgenteComparaCalculationResultStorageError) as exc:
        load_comparison_calculation_result(
            storage_key=meta["result_storage_key"],
            comparison_id=cmp_id,
            fingerprint="b" * 64,
        )
    assert exc.value.error_code == ERROR_RESULT_IDENTITY_MISMATCH
    assert path.is_file()


def test_legacy_envelope_without_timestamps_loads(storage_env):
    cmp_id, fp = _unique_identity("ttl-legacy")
    meta = save_comparison_calculation_result(
        comparison_id=cmp_id, fingerprint=fp, result=_minimal_result(cmp_id)
    )
    path = resolve_result_storage_path(meta["result_storage_key"])
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope.pop("created_at", None)
    envelope.pop("expires_at", None)
    path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    loaded = load_comparison_calculation_result(
        storage_key=meta["result_storage_key"],
        comparison_id=cmp_id,
        fingerprint=fp,
        expected_checksum=meta["result_checksum"],
    )
    assert loaded["comparison_id"] == cmp_id
    assert path.is_file()


def test_legacy_memory_envelope_without_timestamps_loads(storage_env):
    from app.agente_compara_calculation_result_storage import (
        load_comparison_calculation_memory_payload,
        resolve_memory_storage_path,
        save_comparison_calculation_memory_payload,
    )

    cmp_id, fp = _unique_identity("ttl-legacy-mem")
    meta = save_comparison_calculation_memory_payload(
        comparison_id=cmp_id, fingerprint=fp, memory_payload=_minimal_memory(cmp_id)
    )
    path = resolve_memory_storage_path(meta["memory_storage_key"])
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope.pop("created_at", None)
    envelope.pop("expires_at", None)
    path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    loaded = load_comparison_calculation_memory_payload(
        storage_key=meta["memory_storage_key"],
        comparison_id=cmp_id,
        fingerprint=fp,
        expected_checksum=meta["memory_checksum"],
    )
    assert loaded["items"]["t1:1"]["table_id"] == "t1"


def test_legacy_mtime_fallback_expires_after_identity(storage_env):
    cmp_id, fp = _unique_identity("ttl-legacy-mtime")
    meta = save_comparison_calculation_result(
        comparison_id=cmp_id, fingerprint=fp, result=_minimal_result(cmp_id)
    )
    path = resolve_result_storage_path(meta["result_storage_key"])
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope.pop("created_at", None)
    envelope.pop("expires_at", None)
    path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    stale = time.time() - ((AGENTE_COMPARA_CALC_STORAGE_TTL_HOURS + 1) * 3600)
    os.utime(path, (stale, stale))
    with pytest.raises(AgenteComparaCalculationResultStorageError) as exc:
        load_comparison_calculation_result(
            storage_key=meta["result_storage_key"],
            comparison_id=cmp_id,
            fingerprint=fp,
        )
    assert exc.value.error_code == ERROR_RESULT_MISSING
    assert not path.is_file()


def test_checksum_mismatch_is_not_treated_as_expired(storage_env, monkeypatch):
    created = datetime(2026, 8, 18, 10, 0, 0, tzinfo=timezone.utc)
    _freeze_storage_clock(monkeypatch, created)
    cmp_id, fp = _unique_identity("ttl-checksum")
    meta = save_comparison_calculation_result(
        comparison_id=cmp_id, fingerprint=fp, result=_minimal_result(cmp_id)
    )
    path = resolve_result_storage_path(meta["result_storage_key"])
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["result"]["row_count"] = 99
    path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    _freeze_storage_clock(monkeypatch, created + timedelta(hours=49))
    with pytest.raises(AgenteComparaCalculationResultStorageError) as exc:
        load_comparison_calculation_result(
            storage_key=meta["result_storage_key"],
            comparison_id=cmp_id,
            fingerprint=fp,
            expected_checksum=meta["result_checksum"],
        )
    assert exc.value.error_code == ERROR_RESULT_CORRUPT
    assert path.is_file()


def test_opportunistic_sweep_removes_clearly_expired_new_envelopes(storage_env, monkeypatch):
    created = datetime(2026, 8, 18, 10, 0, 0, tzinfo=timezone.utc)
    _freeze_storage_clock(monkeypatch, created)
    cmp_id, fp = _unique_identity("ttl-sweep")
    meta = save_comparison_calculation_result(
        comparison_id=cmp_id, fingerprint=fp, result=_minimal_result(cmp_id)
    )
    path = resolve_result_storage_path(meta["result_storage_key"])
    assert path.is_file()
    _freeze_storage_clock(monkeypatch, created + timedelta(hours=49))
    removed = maybe_cleanup_expired_calculation_storage(path.parent, min_interval_seconds=0)
    assert removed >= 1
    assert not path.is_file()


def _strip_retention_timestamps(path):
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope.pop("created_at", None)
    envelope.pop("expires_at", None)
    path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    return envelope


def _freeze_storage_mtime(monkeypatch, when: datetime):
    import app.agente_compara_calculation_result_storage as storage_mod

    monkeypatch.setattr(storage_mod, "_mtime_utc", lambda _path: when)


def test_sweep_removes_stale_legacy_result_and_memory(storage_env, monkeypatch):
    from app.agente_compara_calculation_result_storage import (
        resolve_memory_storage_path,
        save_comparison_calculation_memory_payload,
    )

    created = datetime(2026, 8, 18, 10, 0, 0, tzinfo=timezone.utc)
    _freeze_storage_clock(monkeypatch, created)
    _freeze_storage_mtime(monkeypatch, created - timedelta(hours=AGENTE_COMPARA_CALC_STORAGE_TTL_HOURS + 1))
    cmp_id, fp = _unique_identity("ttl-sweep-legacy-stale")
    memory_meta = save_comparison_calculation_memory_payload(
        comparison_id=cmp_id, fingerprint=fp, memory_payload=_minimal_memory(cmp_id)
    )
    result_meta = save_comparison_calculation_result(
        comparison_id=cmp_id,
        fingerprint=fp,
        result=_minimal_result(cmp_id),
        memory_storage_meta=memory_meta,
    )
    result_path = resolve_result_storage_path(result_meta["result_storage_key"])
    memory_path = resolve_memory_storage_path(memory_meta["memory_storage_key"])
    _strip_retention_timestamps(result_path)
    _strip_retention_timestamps(memory_path)
    removed = maybe_cleanup_expired_calculation_storage(result_path.parent, min_interval_seconds=0)
    assert removed >= 1
    assert not result_path.is_file()
    assert not memory_path.is_file()


def test_sweep_preserves_fresh_legacy_without_timestamps(storage_env, monkeypatch):
    created = datetime(2026, 8, 18, 10, 0, 0, tzinfo=timezone.utc)
    _freeze_storage_clock(monkeypatch, created)
    _freeze_storage_mtime(monkeypatch, created - timedelta(hours=1))
    cmp_id, fp = _unique_identity("ttl-sweep-legacy-fresh")
    meta = save_comparison_calculation_result(
        comparison_id=cmp_id, fingerprint=fp, result=_minimal_result(cmp_id)
    )
    path = resolve_result_storage_path(meta["result_storage_key"])
    _strip_retention_timestamps(path)
    maybe_cleanup_expired_calculation_storage(path.parent, min_interval_seconds=0)
    assert path.is_file()


def test_sweep_explicit_expires_at_wins_over_old_mtime(storage_env, monkeypatch):
    created = datetime(2026, 8, 18, 10, 0, 0, tzinfo=timezone.utc)
    _freeze_storage_clock(monkeypatch, created)
    _freeze_storage_mtime(monkeypatch, created - timedelta(hours=AGENTE_COMPARA_CALC_STORAGE_TTL_HOURS + 1))
    cmp_id, fp = _unique_identity("ttl-sweep-explicit")
    meta = save_comparison_calculation_result(
        comparison_id=cmp_id, fingerprint=fp, result=_minimal_result(cmp_id)
    )
    path = resolve_result_storage_path(meta["result_storage_key"])
    envelope = json.loads(path.read_text(encoding="utf-8"))
    assert envelope.get("expires_at")
    maybe_cleanup_expired_calculation_storage(path.parent, min_interval_seconds=0)
    assert path.is_file()
    loaded = load_comparison_calculation_result(
        storage_key=meta["result_storage_key"],
        comparison_id=cmp_id,
        fingerprint=fp,
        expected_checksum=meta["result_checksum"],
    )
    assert loaded["comparison_id"] == cmp_id


def test_sweep_skips_invalid_expires_at_and_corrupt(storage_env, monkeypatch):
    created = datetime(2026, 8, 18, 10, 0, 0, tzinfo=timezone.utc)
    _freeze_storage_clock(monkeypatch, created)
    _freeze_storage_mtime(monkeypatch, created - timedelta(hours=AGENTE_COMPARA_CALC_STORAGE_TTL_HOURS + 1))
    cmp_invalid, fp_invalid = _unique_identity("ttl-sweep-invalid-exp")
    invalid_meta = save_comparison_calculation_result(
        comparison_id=cmp_invalid, fingerprint=fp_invalid, result=_minimal_result(cmp_invalid)
    )
    invalid_path = resolve_result_storage_path(invalid_meta["result_storage_key"])
    envelope = json.loads(invalid_path.read_text(encoding="utf-8"))
    envelope["expires_at"] = "not-a-timestamp"
    invalid_path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")

    cmp_corrupt, fp_corrupt = _unique_identity("ttl-sweep-corrupt")
    corrupt_meta = save_comparison_calculation_result(
        comparison_id=cmp_corrupt, fingerprint=fp_corrupt, result=_minimal_result(cmp_corrupt)
    )
    corrupt_path = resolve_result_storage_path(corrupt_meta["result_storage_key"])
    corrupt_path.write_text("{not-json", encoding="utf-8")

    _freeze_storage_clock(monkeypatch, created + timedelta(hours=49))
    maybe_cleanup_expired_calculation_storage(invalid_path.parent, min_interval_seconds=0)
    assert invalid_path.is_file()
    assert corrupt_path.is_file()


def test_memory_identity_mismatch_does_not_cleanup(storage_env, monkeypatch):
    from app.agente_compara_calculation_result_storage import (
        load_comparison_calculation_memory_payload,
        resolve_memory_storage_path,
        save_comparison_calculation_memory_payload,
    )

    created = datetime(2026, 8, 18, 10, 0, 0, tzinfo=timezone.utc)
    _freeze_storage_clock(monkeypatch, created)
    cmp_id, fp = _unique_identity("ttl-mem-identity")
    meta = save_comparison_calculation_memory_payload(
        comparison_id=cmp_id, fingerprint=fp, memory_payload=_minimal_memory(cmp_id)
    )
    path = resolve_memory_storage_path(meta["memory_storage_key"])
    _freeze_storage_clock(monkeypatch, created + timedelta(hours=49))
    with pytest.raises(AgenteComparaCalculationResultStorageError) as exc:
        load_comparison_calculation_memory_payload(
            storage_key=meta["memory_storage_key"],
            comparison_id="other-cmp",
            fingerprint=fp,
        )
    assert exc.value.error_code == ERROR_RESULT_IDENTITY_MISMATCH
    assert path.is_file()
    with pytest.raises(AgenteComparaCalculationResultStorageError) as exc:
        load_comparison_calculation_memory_payload(
            storage_key=meta["memory_storage_key"],
            comparison_id=cmp_id,
            fingerprint="b" * 64,
        )
    assert exc.value.error_code == ERROR_RESULT_IDENTITY_MISMATCH
    assert path.is_file()
