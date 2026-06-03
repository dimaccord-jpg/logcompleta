"""Testes de PDF real via Gemini Files API (Cleiton governado)."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import app.cleiton_doc_gemini_files as gemini_files
import app.cleiton_doc_service as svc
from app.cleiton_doc_contracts import (
    CONTEXT_KIND_GEMINI_FILE,
    FIELD_CONTEXT_KIND,
    FIELD_GEMINI_FILE_NAME,
    FIELD_GEMINI_FILE_STATE,
    FIELD_GEMINI_FILE_URI,
    FIELD_GEMINI_MIME_TYPE,
    FIELD_PDF_CONTEXT_READY,
    FIELD_PREPARED_CONTEXT,
    FIELD_STATUS,
    GEMINI_FILE_STATE_ACTIVE,
    STATUS_ACTIVE,
    STATUS_ERROR,
)
from app.cleiton_doc_store import peek_document_record, remove_document_record
from tests.cleiton_doc_fixtures import make_minimal_pdf, patch_cleiton_doc_cfg, patch_cleiton_doc_store


class _FakeUploadedFile:
    def __init__(self, *, name: str, uri: str, state: Any = "ACTIVE"):
        self.name = name
        self.uri = uri
        self.mime_type = "application/pdf"
        self.state = state


class _SdkFileStateEnum:
    def __init__(self, name: str):
        self.name = name

    def __str__(self) -> str:
        return f"FileState.{self.name}"


def _fake_gemini_client(*, upload_state: Any = "ACTIVE"):
    client = MagicMock()
    uploaded = _FakeUploadedFile(
        name="files/test-pdf-abc",
        uri="https://generativelanguage.googleapis.com/v1beta/files/test-pdf-abc",
        state=upload_state,
    )
    client.files.upload.return_value = uploaded
    client.files.get.return_value = uploaded
    client.files.delete.return_value = None
    return client


@pytest.fixture
def session_app(app, tmp_path, monkeypatch, ctx):
    patch_cleiton_doc_store(tmp_path, monkeypatch)
    patch_cleiton_doc_cfg(monkeypatch)
    app.config["SECRET_KEY"] = "test-secret"
    return app


@pytest.mark.parametrize(
    ("raw", "expected", "active"),
    [
        ("ACTIVE", "ACTIVE", True),
        ("FileState.ACTIVE", "ACTIVE", True),
        ("filestate.active", "ACTIVE", True),
        (_SdkFileStateEnum("ACTIVE"), "ACTIVE", True),
        ("PROCESSING", "PROCESSING", False),
        ("FileState.PROCESSING", "PROCESSING", False),
        (_SdkFileStateEnum("PROCESSING"), "PROCESSING", False),
        ("FAILED", "FAILED", False),
        ("FileState.FAILED", "FAILED", False),
        (None, "", False),
        ("INACTIVE", "INACTIVE", False),
        ("file_state_active", "FILE_STATE_ACTIVE", False),
        ("STATE_UNSPECIFIED", "STATE_UNSPECIFIED", False),
    ],
)
def test_normalize_gemini_file_state(raw, expected, active):
    assert gemini_files.normalize_gemini_file_state(raw) == expected
    assert gemini_files.is_gemini_file_active_state(raw) is active


def test_upload_pdf_with_sdk_file_state_enum_is_ready(session_app, monkeypatch):
    monkeypatch.setattr(
        gemini_files,
        "get_cleiton_gemini_client",
        lambda: _fake_gemini_client(upload_state=_SdkFileStateEnum("ACTIVE")),
    )
    with session_app.test_request_context("/"):
        public = svc.prepare_and_register_document(
            display_name="enum.pdf",
            file_bytes=make_minimal_pdf(),
            mime_type="application/pdf",
        )
    assert public[FIELD_STATUS] == STATUS_ACTIVE
    assert public[FIELD_PDF_CONTEXT_READY] is True
    record = peek_document_record(public["doc_id"])
    assert record[FIELD_GEMINI_FILE_STATE] == GEMINI_FILE_STATE_ACTIVE


def test_pdf_context_ready_from_record_accepts_file_state_enum():
    record = {
        FIELD_CONTEXT_KIND: CONTEXT_KIND_GEMINI_FILE,
        FIELD_GEMINI_FILE_NAME: "files/x",
        FIELD_GEMINI_FILE_STATE: _SdkFileStateEnum("ACTIVE"),
    }
    assert gemini_files.pdf_context_ready_from_record(record) is True


def test_build_gemini_file_part_accepts_sdk_enum_state(session_app, monkeypatch):
    client = _fake_gemini_client(upload_state=_SdkFileStateEnum("ACTIVE"))
    monkeypatch.setattr(gemini_files, "get_cleiton_gemini_client", lambda: client)
    record = {
        FIELD_CONTEXT_KIND: CONTEXT_KIND_GEMINI_FILE,
        FIELD_GEMINI_FILE_NAME: "files/test-pdf-abc",
        FIELD_GEMINI_FILE_STATE: GEMINI_FILE_STATE_ACTIVE,
        FIELD_GEMINI_FILE_URI: "https://generativelanguage.googleapis.com/v1beta/files/test-pdf-abc",
        FIELD_GEMINI_MIME_TYPE: "application/pdf",
    }
    part = gemini_files.build_gemini_file_part_for_generate(record)
    assert part is not None


def test_upload_pdf_with_file_state_active_string_is_ready(session_app, monkeypatch):
    monkeypatch.setattr(
        gemini_files,
        "get_cleiton_gemini_client",
        lambda: _fake_gemini_client(upload_state="FileState.ACTIVE"),
    )
    with session_app.test_request_context("/"):
        public = svc.prepare_and_register_document(
            display_name="str.pdf",
            file_bytes=make_minimal_pdf(),
            mime_type="application/pdf",
        )
    assert public[FIELD_STATUS] == STATUS_ACTIVE
    assert public[FIELD_PDF_CONTEXT_READY] is True


def test_upload_pdf_processing_stays_error(session_app, monkeypatch):
    monkeypatch.setattr(
        gemini_files,
        "get_cleiton_gemini_client",
        lambda: _fake_gemini_client(upload_state=_SdkFileStateEnum("PROCESSING")),
    )
    with session_app.test_request_context("/"):
        public = svc.prepare_and_register_document(
            display_name="wait.pdf",
            file_bytes=make_minimal_pdf(),
            mime_type="application/pdf",
        )
    assert public[FIELD_STATUS] == STATUS_ERROR
    assert public[FIELD_PDF_CONTEXT_READY] is False


def test_upload_pdf_saves_gemini_reference_not_in_public(session_app, monkeypatch):
    monkeypatch.setattr(gemini_files, "get_cleiton_gemini_client", lambda: _fake_gemini_client())
    with session_app.test_request_context("/"):
        public = svc.prepare_and_register_document(
            display_name="relatorio.pdf",
            file_bytes=make_minimal_pdf(pages=1),
            mime_type="application/pdf",
        )
    assert public[FIELD_STATUS] == STATUS_ACTIVE
    assert public[FIELD_PDF_CONTEXT_READY] is True
    assert public.get(FIELD_GEMINI_FILE_NAME) is None
    assert public.get(FIELD_GEMINI_FILE_URI) is None
    record = peek_document_record(public["doc_id"])
    assert record[FIELD_GEMINI_FILE_NAME] == "files/test-pdf-abc"
    assert record[FIELD_GEMINI_FILE_STATE] == GEMINI_FILE_STATE_ACTIVE


def test_delete_removes_gemini_reference(session_app, monkeypatch):
    client = _fake_gemini_client()
    monkeypatch.setattr(gemini_files, "get_cleiton_gemini_client", lambda: client)
    with session_app.test_request_context("/"):
        doc = svc.prepare_and_register_document(
            display_name="a.pdf",
            file_bytes=make_minimal_pdf(),
            mime_type="application/pdf",
        )
        svc.remove_document_from_session(doc["doc_id"])
    client.files.delete.assert_called()


def test_clear_removes_gemini_references(session_app, monkeypatch):
    client = _fake_gemini_client()
    monkeypatch.setattr(gemini_files, "get_cleiton_gemini_client", lambda: client)
    with session_app.test_request_context("/"):
        svc.prepare_and_register_document(
            display_name="a.pdf",
            file_bytes=make_minimal_pdf(),
            mime_type="application/pdf",
        )
        svc.clear_documents_for_session()
    assert client.files.delete.call_count >= 1


def test_gemini_delete_failure_does_not_break_user(session_app, monkeypatch):
    client = _fake_gemini_client()
    client.files.delete.side_effect = RuntimeError("remote fail")
    monkeypatch.setattr(gemini_files, "get_cleiton_gemini_client", lambda: client)
    with session_app.test_request_context("/"):
        doc = svc.prepare_and_register_document(
            display_name="a.pdf",
            file_bytes=make_minimal_pdf(),
            mime_type="application/pdf",
        )
        result = svc.remove_document_from_session(doc["doc_id"])
    assert result["ok"] is True


def test_gemini_upload_failure_registers_error_status(session_app, monkeypatch):
    client = _fake_gemini_client(upload_state="FAILED")
    monkeypatch.setattr(gemini_files, "get_cleiton_gemini_client", lambda: client)
    with session_app.test_request_context("/"):
        public = svc.prepare_and_register_document(
            display_name="bad.pdf",
            file_bytes=make_minimal_pdf(),
            mime_type="application/pdf",
        )
    assert public[FIELD_STATUS] == STATUS_ERROR
    assert public[FIELD_PDF_CONTEXT_READY] is False


def test_no_client_files_upload_outside_adapter(monkeypatch):
    import inspect

    source = inspect.getsource(gemini_files)
    assert "client.files.upload" in source
    import app.run_julia_chat as julia_chat

    chat_src = inspect.getsource(julia_chat)
    assert "files.upload" not in chat_src


def test_upload_pdf_prepared_context_marks_ready(session_app, monkeypatch):
    monkeypatch.setattr(gemini_files, "get_cleiton_gemini_client", lambda: _fake_gemini_client())
    with session_app.test_request_context("/"):
        public = svc.prepare_and_register_document(
            display_name="ok.pdf",
            file_bytes=make_minimal_pdf(),
            mime_type="application/pdf",
        )
    record = peek_document_record(public["doc_id"])
    assert record[FIELD_CONTEXT_KIND] == CONTEXT_KIND_GEMINI_FILE
    assert '"gemini_file_ready": true' in (record.get(FIELD_PREPARED_CONTEXT) or "").lower()


def test_remove_document_record_cleans_gemini(monkeypatch, tmp_path):
    patch_cleiton_doc_store(tmp_path, monkeypatch)
    client = _fake_gemini_client()
    monkeypatch.setattr(gemini_files, "get_cleiton_gemini_client", lambda: client)
    record = {
        "doc_id": "abc123def4567890abcdef1234567890",
        "gemini_file_name": "files/remote-1",
        "context_kind": CONTEXT_KIND_GEMINI_FILE,
        "created_at": "2099-01-01T00:00:00",
        "expires_at": "2099-02-01T00:00:00",
    }
    from app.cleiton_doc_store import save_document_record

    save_document_record(record)
    remove_document_record(record["doc_id"])
    client.files.delete.assert_called_with(name="files/remote-1")


def test_maybe_cleanup_expired_cleiton_docs_deletes_gemini_remote(session_app, monkeypatch):
    from datetime import timedelta

    import app.cleiton_doc_store as doc_store

    client = _fake_gemini_client()
    monkeypatch.setattr(gemini_files, "get_cleiton_gemini_client", lambda: client)

    past = (doc_store._utcnow() - timedelta(hours=72)).isoformat()
    record = {
        "doc_id": "maybecleanup001234567890abcdef12",
        "gemini_file_name": "files/maybe-remote",
        "context_kind": CONTEXT_KIND_GEMINI_FILE,
        "created_at": past,
        "expires_at": past,
    }
    with session_app.test_request_context("/"):
        doc_store.save_document_record(record)
        removed = svc.maybe_cleanup_expired_cleiton_docs(min_interval_seconds=0)
    assert removed == 1
    client.files.delete.assert_called_with(name="files/maybe-remote")


def test_cleanup_expired_public_record_still_hides_gemini_refs(session_app, monkeypatch):
    monkeypatch.setattr(gemini_files, "get_cleiton_gemini_client", lambda: _fake_gemini_client())
    with session_app.test_request_context("/"):
        public = svc.prepare_and_register_document(
            display_name="hidden.pdf",
            file_bytes=make_minimal_pdf(),
            mime_type="application/pdf",
        )
    assert public.get(FIELD_GEMINI_FILE_NAME) is None
    assert public.get(FIELD_GEMINI_FILE_URI) is None
