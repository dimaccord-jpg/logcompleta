import json
from datetime import timedelta

import pytest

import app.cleiton_doc_store as store
from app.cleiton_doc_contracts import (
    ERROR_DOC_ID_INVALID,
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
