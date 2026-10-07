import json
from datetime import timedelta
from pathlib import Path

import pytest

import app.cleiton_doc_store as store
from app.cleiton_doc_contracts import (
    ERROR_DOC_ID_INVALID,
    ERROR_DOC_NOT_FOUND,
    ERROR_DOC_REMOVE_FAILED,
    ERROR_STORE_READ,
    FIELD_CREATED_AT,
    FIELD_DOC_ID,
    FIELD_EXPIRES_AT,
    FIELD_STATUS,
    STATUS_ACTIVE,
)


def _sample_record(doc_id: str = "abc123", *, expires_at: str | None = None) -> dict:
    return {
        FIELD_DOC_ID: doc_id,
        "display_name": "contrato.pdf",
        "safe_name": "contrato.pdf",
        "extension": ".pdf",
        "mime_type": "application/pdf",
        "size_bytes": 1024,
        FIELD_CREATED_AT: store._utcnow_iso(),
        FIELD_EXPIRES_AT: expires_at,
        FIELD_STATUS: STATUS_ACTIVE,
        "truncated": False,
        "context_kind": "placeholder",
        "context_ref": "placeholder:abc123",
        "source_agent": "cleiton",
        "session_key": None,
        "error_code": None,
    }


@pytest.fixture
def doc_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "get_cleiton_doc_tmp_dir", lambda: str(tmp_path))
    return tmp_path


def test_store_save_load_remove(doc_tmp):
    record = _sample_record("doc001")
    store.save_document_record(record)

    loaded = store.load_document_record("doc001", ttl_hours=48)
    assert loaded is not None
    assert loaded[FIELD_DOC_ID] == "doc001"

    result = store.remove_document_record("doc001")
    assert result["ok"] is True
    assert result["removed"] is True
    assert store.load_document_record("doc001", ttl_hours=48) is None


def test_store_expired_document_removed_on_load(doc_tmp):
    past = (store._utcnow() - timedelta(hours=72)).isoformat()
    record = _sample_record("expired001", expires_at=past)
    store.save_document_record(record)

    assert store.load_document_record("expired001", ttl_hours=48) is None
    assert not (doc_tmp / "expired001.json").exists()


def test_store_non_expired_document_remains(doc_tmp):
    future = (store._utcnow() + timedelta(hours=24)).isoformat()
    record = _sample_record("alive001", expires_at=future)
    store.save_document_record(record)

    loaded = store.load_document_record("alive001", ttl_hours=48)
    assert loaded is not None
    assert loaded[FIELD_DOC_ID] == "alive001"


def test_store_cleanup_expired(doc_tmp):
    past = (store._utcnow() - timedelta(hours=72)).isoformat()
    future = (store._utcnow() + timedelta(hours=24)).isoformat()
    store.save_document_record(_sample_record("gone001", expires_at=past))
    store.save_document_record(_sample_record("stay001", expires_at=future))

    removed = store.cleanup_expired_document_records(48)
    assert removed == 1
    assert not (doc_tmp / "gone001.json").exists()
    assert (doc_tmp / "stay001.json").exists()


def test_store_corrupted_json_does_not_break_cleanup(doc_tmp):
    bad_path = doc_tmp / "broken.json"
    bad_path.write_text("{not-json", encoding="utf-8")

    removed = store.cleanup_expired_document_records(48)
    assert removed == 1
    assert not bad_path.exists()

    active = store.list_document_records(ttl_hours=48)
    assert active == []


def test_store_corrupted_json_does_not_break_listing(doc_tmp):
    bad_path = doc_tmp / "broken2.json"
    bad_path.write_text("{not-json", encoding="utf-8")

    active = store.list_document_records(ttl_hours=48)
    assert active == []
    assert not bad_path.exists()


def test_store_path_traversal_blocked(doc_tmp):
    assert store.load_document_record("../secret", ttl_hours=48) is None

    with pytest.raises(ValueError):
        store._build_safe_path(str(doc_tmp), "../outside.json")

    result = store.remove_document_record("../../etc/passwd")
    assert result["ok"] is False
    assert result["error_code"] == ERROR_DOC_ID_INVALID


def test_store_maybe_cleanup_respects_disabled(doc_tmp):
    past = (store._utcnow() - timedelta(hours=72)).isoformat()
    store.save_document_record(_sample_record("old001", expires_at=past))

    removed = store.maybe_cleanup_expired_document_records(
        48,
        cleanup_enabled=False,
        min_interval_seconds=0,
    )
    assert removed == 0
    assert (doc_tmp / "old001.json").exists()


def test_store_maybe_cleanup_runs_when_enabled(doc_tmp):
    past = (store._utcnow() - timedelta(hours=72)).isoformat()
    store.save_document_record(_sample_record("old002", expires_at=past))

    removed = store.maybe_cleanup_expired_document_records(
        48,
        cleanup_enabled=True,
        min_interval_seconds=0,
    )
    assert removed == 1
    assert not (doc_tmp / "old002.json").exists()


def test_store_maybe_cleanup_throttles(doc_tmp, monkeypatch):
    store.maybe_cleanup_expired_document_records(
        48,
        cleanup_enabled=True,
        min_interval_seconds=3600,
    )

    def _fail_cleanup(_ttl):
        raise AssertionError("cleanup não deveria rodar antes do intervalo")

    monkeypatch.setattr(store, "cleanup_expired_document_records", _fail_cleanup)
    assert (
        store.maybe_cleanup_expired_document_records(
            48,
            cleanup_enabled=True,
            min_interval_seconds=3600,
        )
        == 0
    )


def test_store_list_active_documents(doc_tmp):
    future = (store._utcnow() + timedelta(hours=12)).isoformat()
    store.save_document_record(_sample_record("a001", expires_at=future))
    store.save_document_record(_sample_record("a002", expires_at=future))

    active = store.list_document_records(ttl_hours=48)
    ids = {item[FIELD_DOC_ID] for item in active}
    assert ids == {"a001", "a002"}


def test_store_ttl_from_expires_at_field(doc_tmp):
    created = store._utcnow() - timedelta(hours=1)
    expires = created + timedelta(hours=2)
    record = _sample_record("ttl001")
    record[FIELD_CREATED_AT] = created.isoformat()
    record[FIELD_EXPIRES_AT] = expires.isoformat()
    store.save_document_record(record)

    path = doc_tmp / "ttl001.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload[FIELD_EXPIRES_AT] == expires.isoformat()

    loaded = store.load_document_record("ttl001", ttl_hours=48)
    assert loaded is not None


def test_cleanup_expired_document_records_deletes_gemini_remote(doc_tmp, monkeypatch):
    from unittest.mock import MagicMock

    import app.cleiton_doc_gemini_files as gemini_files

    client = MagicMock()
    client.files.delete.return_value = None
    monkeypatch.setattr(gemini_files, "get_cleiton_gemini_client", lambda: client)

    past = (store._utcnow() - timedelta(hours=72)).isoformat()
    doc_id = "expiredgemini001234567890abcdef12"
    record = {
        FIELD_DOC_ID: doc_id,
        "gemini_file_name": "files/expired-remote",
        "context_kind": "gemini_file",
        FIELD_CREATED_AT: past,
        FIELD_EXPIRES_AT: past,
    }
    store.save_document_record(record)

    removed = store.cleanup_expired_document_records(48)
    assert removed == 1
    assert not (doc_tmp / f"{doc_id}.json").exists()
    client.files.delete.assert_called_with(name="files/expired-remote")


def test_maybe_cleanup_expired_document_records_deletes_gemini_remote(doc_tmp, monkeypatch):
    from unittest.mock import MagicMock

    import app.cleiton_doc_gemini_files as gemini_files

    client = MagicMock()
    client.files.delete.return_value = None
    monkeypatch.setattr(gemini_files, "get_cleiton_gemini_client", lambda: client)

    past = (store._utcnow() - timedelta(hours=72)).isoformat()
    doc_id = "maybecleanup001234567890abcdef12"
    record = {
        FIELD_DOC_ID: doc_id,
        "gemini_file_name": "files/maybe-remote",
        "context_kind": "gemini_file",
        FIELD_CREATED_AT: past,
        FIELD_EXPIRES_AT: past,
    }
    store.save_document_record(record)

    removed = store.maybe_cleanup_expired_document_records(48, min_interval_seconds=0)
    assert removed == 1
    client.files.delete.assert_called_with(name="files/maybe-remote")


def test_cleanup_expired_gemini_delete_failure_does_not_break_local(doc_tmp, monkeypatch):
    from unittest.mock import MagicMock

    import app.cleiton_doc_gemini_files as gemini_files

    client = MagicMock()
    client.files.delete.side_effect = RuntimeError("already gone")
    monkeypatch.setattr(gemini_files, "get_cleiton_gemini_client", lambda: client)

    past = (store._utcnow() - timedelta(hours=72)).isoformat()
    doc_id = "failcleanup001234567890abcdef12"
    record = {
        FIELD_DOC_ID: doc_id,
        "gemini_file_name": "files/missing-remote",
        "context_kind": "gemini_file",
        FIELD_CREATED_AT: past,
        FIELD_EXPIRES_AT: past,
    }
    store.save_document_record(record)

    removed = store.cleanup_expired_document_records(48)
    assert removed == 1
    assert not (doc_tmp / f"{doc_id}.json").exists()


def _future_iso() -> str:
    return (store._utcnow() + timedelta(hours=24)).isoformat()


def _bind_original(doc_id: str, payload: bytes, *, expires_at: str | None = None) -> str:
    ref = store.save_document_original(doc_id, payload)
    record = _sample_record(doc_id, expires_at=expires_at or _future_iso())
    record[store.FIELD_ORIGINAL_REF] = ref
    store.save_document_record(record)
    return ref


def test_original_ref_is_relative_and_derived_only_from_doc_id(doc_tmp):
    doc_id = "orig001"
    payload = b"bytes-do-upload"
    user_name = r"..\segredo\planilha_secreta_usuario.csv"
    ref = _bind_original(doc_id, payload)

    assert ref == f"originals/{doc_id}.bin"
    assert user_name not in ref
    assert "planilha_secreta_usuario" not in ref
    assert "segredo" not in ref
    assert ":\\" not in ref
    assert not ref.startswith("/")
    assert ".." not in ref

    bin_path = doc_tmp / "originals" / f"{doc_id}.bin"
    assert bin_path.is_file()
    assert bin_path.name == f"{doc_id}.bin"
    stored = json.loads((doc_tmp / f"{doc_id}.json").read_text(encoding="utf-8"))
    assert stored[store.FIELD_ORIGINAL_REF] == ref
    assert str(doc_tmp) not in stored[store.FIELD_ORIGINAL_REF]
    assert store.load_document_original_bytes(doc_id, ttl_hours=48) == payload


def test_remove_document_record_removes_json_and_original(doc_tmp):
    doc_id = "origremove001"
    _bind_original(doc_id, b"apagar")

    result = store.remove_document_record(doc_id)

    assert result["ok"] is True
    assert result["removed"] is True
    assert not (doc_tmp / f"{doc_id}.json").exists()
    assert not (doc_tmp / "originals" / f"{doc_id}.bin").exists()


def test_remove_document_record_missing_original_still_removes_json(doc_tmp):
    doc_id = "origmissing001"
    record = _sample_record(doc_id, expires_at=_future_iso())
    record[store.FIELD_ORIGINAL_REF] = f"originals/{doc_id}.bin"
    store.save_document_record(record)

    result = store.remove_document_record(doc_id)

    assert result["ok"] is True
    assert result["removed"] is True
    assert not (doc_tmp / f"{doc_id}.json").exists()


def test_remove_document_record_keeps_json_when_original_unlink_fails(doc_tmp, monkeypatch):
    doc_id = "origlock001"
    ref = _bind_original(doc_id, b"preso")
    json_path = doc_tmp / f"{doc_id}.json"
    bin_path = doc_tmp / "originals" / f"{doc_id}.bin"
    before = json.loads(json_path.read_text(encoding="utf-8"))
    real_unlink = Path.unlink
    locked = {"on": True}

    def locked_unlink(self, *args, **kwargs):
        if locked["on"] and self.suffix.lower() == ".bin":
            raise PermissionError("arquivo em uso")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked_unlink)

    result = store.remove_document_record(doc_id)

    assert result["ok"] is False
    assert result["removed"] is False
    assert result["error_code"] == ERROR_DOC_REMOVE_FAILED
    assert "PermissionError" not in json.dumps(result)
    assert "arquivo em uso" not in json.dumps(result)
    assert json_path.is_file()
    stored = json.loads(json_path.read_text(encoding="utf-8"))
    assert stored == before
    assert stored[store.FIELD_ORIGINAL_REF] == ref
    assert bin_path.is_file()
    assert bin_path.read_bytes() == b"preso"

    locked["on"] = False
    retry = store.remove_document_record(doc_id)

    assert retry["ok"] is True
    assert retry["removed"] is True
    assert not json_path.exists()
    assert not bin_path.exists()


def test_expired_document_cannot_recover_original(doc_tmp):
    doc_id = "origexpired001"
    payload = b"nao-recuperar"
    past = (store._utcnow() - timedelta(hours=2)).isoformat()
    _bind_original(doc_id, payload, expires_at=past)

    with pytest.raises(store.DocumentOriginalUnavailable) as exc_info:
        store.load_document_original_bytes(doc_id, ttl_hours=48)

    assert exc_info.value.error_code == ERROR_DOC_NOT_FOUND
    assert "originals" not in str(exc_info.value)
    assert str(doc_tmp) not in str(exc_info.value)
    assert payload not in str(exc_info.value).encode("utf-8", "replace")
    assert not (doc_tmp / f"{doc_id}.json").exists()
    assert not (doc_tmp / "originals" / f"{doc_id}.bin").exists()


def test_cleanup_expired_removes_original_and_keeps_live_one(doc_tmp):
    past = (store._utcnow() - timedelta(hours=3)).isoformat()
    _bind_original("goneorig001", b"velho", expires_at=past)
    _bind_original("stayorig001", b"vivo", expires_at=_future_iso())

    removed = store.cleanup_expired_document_records(48)

    assert removed == 1
    assert not (doc_tmp / "goneorig001.json").exists()
    assert not (doc_tmp / "originals" / "goneorig001.bin").exists()
    assert (doc_tmp / "stayorig001.json").exists()
    assert (doc_tmp / "originals" / "stayorig001.bin").read_bytes() == b"vivo"


def test_resave_document_does_not_renew_original_or_duplicate_bytes(doc_tmp):
    doc_id = "origkeep001"
    payload = b"mesmo-arquivo"
    _bind_original(doc_id, payload)
    stored = store.load_document_record(doc_id, ttl_hours=48)
    expires_at = stored[FIELD_EXPIRES_AT]
    stored["status"] = "confirmed"
    store.save_document_record(stored)

    reloaded = store.load_document_record(doc_id, ttl_hours=48)
    assert reloaded[FIELD_EXPIRES_AT] == expires_at
    assert store.load_document_original_bytes(doc_id, ttl_hours=48) == payload
    bins = list((doc_tmp / "originals").glob("*.bin"))
    assert [path.name for path in bins] == [f"{doc_id}.bin"]


def test_load_original_rejects_tampered_absolute_ref(doc_tmp):
    doc_id = "origtamper001"
    payload = b"canonico"
    _bind_original(doc_id, payload)
    outside = doc_tmp.parent / "fora-do-store.bin"
    outside.write_bytes(b"nao-ler")
    stored_path = doc_tmp / f"{doc_id}.json"
    stored = json.loads(stored_path.read_text(encoding="utf-8"))
    stored[store.FIELD_ORIGINAL_REF] = str(outside)
    stored_path.write_text(json.dumps(stored), encoding="utf-8")

    with pytest.raises(store.DocumentOriginalUnavailable) as exc_info:
        store.load_document_original_bytes(doc_id, ttl_hours=48)

    assert exc_info.value.error_code == ERROR_STORE_READ
    assert outside.read_bytes() == b"nao-ler"
    assert str(outside) not in str(exc_info.value)


def test_load_original_reuses_operational_and_domain_scope(doc_tmp, monkeypatch):
    doc_id = "origscope001"
    payload = b"escopo"
    ref = store.save_document_original(doc_id, payload)
    record = _sample_record(doc_id, expires_at=_future_iso())
    record["source_agent"] = "agente_compara"
    record["session_key"] = "agente_compara_doc_ids"
    record[store.FIELD_ORIGINAL_REF] = ref
    store.save_document_record(record)

    assert (
        store.load_document_original_bytes(
            doc_id,
            ttl_hours=48,
            expected_source_agent="agente_compara",
            expected_session_key="agente_compara_doc_ids",
        )
        == payload
    )

    with pytest.raises(store.DocumentOriginalUnavailable) as denied:
        store.load_document_original_bytes(
            doc_id,
            ttl_hours=48,
            expected_source_agent="cleide",
            expected_session_key="agente_compara_doc_ids",
        )
    assert denied.value.error_code == ERROR_DOC_NOT_FOUND
    assert (doc_tmp / "originals" / f"{doc_id}.bin").read_bytes() == payload

    monkeypatch.setattr(store, "document_record_matches_operational_scope", lambda _record: False)
    with pytest.raises(store.DocumentOriginalUnavailable) as blocked:
        store.load_document_original_bytes(doc_id, ttl_hours=48)
    assert blocked.value.error_code == ERROR_DOC_NOT_FOUND
    assert (doc_tmp / "originals" / f"{doc_id}.bin").read_bytes() == payload


def test_load_original_rejects_invalid_doc_id(doc_tmp):
    with pytest.raises(store.DocumentOriginalUnavailable) as exc_info:
        store.load_document_original_bytes("../secret", ttl_hours=48)
    assert exc_info.value.error_code == ERROR_DOC_ID_INVALID
    assert not (doc_tmp / "originals").exists() or list((doc_tmp / "originals").glob("*")) == []
