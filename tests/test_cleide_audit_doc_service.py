"""Testes do wrapper documental fino da Cleide Auditoria (Fase 1)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.cleide_audit_doc_service as audit_svc
import app.cleiton_doc_service as cleiton_svc
import app.cleiton_doc_store as store
from app.cleiton_doc_contracts import (
    FIELD_DOC_ID,
    FIELD_SIZE_BYTES,
    SESSION_KEY_CLEITON_DOC_IDS,
    get_cleiton_doc_ids,
)
from app.services import cleiton_doc_config_service as doc_cfg_svc
from tests.cleiton_doc_fixtures import make_txt, patch_cleiton_doc_cfg, patch_cleiton_doc_store


def _patch_audit_cfg(monkeypatch, **overrides):
    cfg = patch_cleiton_doc_cfg(monkeypatch, **overrides)
    monkeypatch.setattr("app.cleide_audit_doc_service.get_cleiton_doc_config", lambda: cfg)
    return cfg


@pytest.fixture
def session_app(app, tmp_path, monkeypatch, ctx):
    patch_cleiton_doc_store(tmp_path, monkeypatch)
    _patch_audit_cfg(monkeypatch)
    app.config["SECRET_KEY"] = "test-secret"
    return app


def test_empty_session_returns_empty_list(session_app):
    with session_app.test_request_context("/"):
        assert audit_svc.get_active_documents_for_session() == []
        totals = audit_svc.get_document_session_totals()
        assert totals["active_count"] == 0
        assert totals["total_bytes"] == 0


def test_prepare_document_uses_cleide_audit_doc_ids(session_app):
    with session_app.test_request_context("/"):
        from flask import session

        doc = audit_svc.prepare_and_register_document(
            display_name="nota.txt",
            file_bytes=make_txt("conteudo de teste"),
            mime_type="text/plain",
            extension=".txt",
        )
        assert doc[FIELD_DOC_ID]
        assert audit_svc.get_cleide_audit_doc_ids(session) == [doc[FIELD_DOC_ID]]
        assert get_cleiton_doc_ids(session) == []


def test_prepare_document_does_not_alter_julia_session(session_app):
    with session_app.test_request_context("/"):
        from flask import session

        julia_doc = cleiton_svc.register_document_placeholder(
            display_name="julia.pdf",
            extension=".pdf",
            mime_type="application/pdf",
            size_bytes=512,
        )
        audit_doc = audit_svc.prepare_and_register_document(
            display_name="audit.txt",
            file_bytes=make_txt("auditoria"),
            mime_type="text/plain",
            extension=".txt",
        )
        assert get_cleiton_doc_ids(session) == [julia_doc[FIELD_DOC_ID]]
        assert audit_svc.get_cleide_audit_doc_ids(session) == [audit_doc[FIELD_DOC_ID]]
        assert len(cleiton_svc.get_active_documents_for_session()) == 1
        assert len(audit_svc.get_active_documents_for_session()) == 1


def test_remove_document_only_from_cleide_audit(session_app):
    with session_app.test_request_context("/"):
        from flask import session

        julia_doc = cleiton_svc.register_document_placeholder(
            display_name="julia.pdf",
            extension=".pdf",
            mime_type="application/pdf",
            size_bytes=512,
        )
        audit_doc = audit_svc.prepare_and_register_document(
            display_name="audit.txt",
            file_bytes=make_txt("auditoria"),
            mime_type="text/plain",
            extension=".txt",
        )
        result = audit_svc.remove_document_from_session(audit_doc[FIELD_DOC_ID])
        assert result["ok"] is True
        assert result["removed_from_session"] is True
        assert audit_svc.get_cleide_audit_doc_ids(session) == []
        assert get_cleiton_doc_ids(session) == [julia_doc[FIELD_DOC_ID]]


def test_clear_session_only_cleide_audit_documents(session_app):
    with session_app.test_request_context("/"):
        from flask import session

        cleiton_svc.register_document_placeholder(
            display_name="julia.pdf",
            extension=".pdf",
            mime_type="application/pdf",
            size_bytes=512,
        )
        audit_svc.prepare_and_register_document(
            display_name="a.txt",
            file_bytes=make_txt("a"),
            mime_type="text/plain",
            extension=".txt",
        )
        audit_svc.prepare_and_register_document(
            display_name="b.txt",
            file_bytes=make_txt("b"),
            mime_type="text/plain",
            extension=".txt",
        )
        result = audit_svc.clear_documents_for_session()
        assert result["ok"] is True
        assert result["requested"] == 2
        assert audit_svc.get_active_documents_for_session() == []
        assert len(get_cleiton_doc_ids(session)) == 1
        assert len(cleiton_svc.get_active_documents_for_session()) == 1


def test_allowed_formats_from_central_config(session_app, monkeypatch):
    _patch_audit_cfg(monkeypatch, pdf_enabled=False, txt_enabled=True, xml_enabled=False)
    with session_app.test_request_context("/"):
        formats = audit_svc.get_allowed_document_formats()
        extensions = {item["extension"] for item in formats}
        assert ".txt" in extensions
        assert ".pdf" not in extensions
        assert all(item["enabled"] for item in formats)


def test_limits_not_hardcoded_in_wrapper(session_app, monkeypatch, ctx):
    doc_cfg_svc.salvar_cleiton_doc_config(
        {
            "upload_enabled": "1",
            "max_files_per_session": "2",
            "session_max_bytes": str(512 * 1024),
            "upload_ttl_hours": "24",
            "cleanup_enabled": "1",
            "prompt_context_max_chars": "24000",
            "prompt_max_files_considered": "2",
            "pdf_enabled": "1",
            "pdf_max_bytes": str(5 * 1024 * 1024),
            "pdf_max_pages": "50",
            "pdf_max_chars": "120000",
            "excel_enabled": "1",
            "excel_max_bytes": str(5 * 1024 * 1024),
            "excel_max_rows": "5000",
            "excel_max_columns": "80",
            "excel_max_chars": "120000",
            "docx_enabled": "1",
            "docx_max_bytes": str(5 * 1024 * 1024),
            "docx_max_paragraphs": "5000",
            "docx_max_chars": "120000",
            "txt_enabled": "1",
            "txt_max_bytes": str(1024 * 1024),
            "txt_max_chars": "120000",
            "xml_enabled": "1",
            "xml_max_bytes": str(2 * 1024 * 1024),
            "xml_max_nodes": "20000",
            "xml_max_depth": "20",
            "xml_max_chars": "120000",
            "csv_enabled": "1",
            "csv_max_bytes": str(2 * 1024 * 1024),
            "csv_max_rows": "10000",
            "csv_max_columns": "80",
            "csv_max_chars": "120000",
        }
    )
    monkeypatch.setattr(
        "app.cleide_audit_doc_service.get_cleiton_doc_config",
        doc_cfg_svc.get_cleiton_doc_config,
    )
    with session_app.test_request_context("/"):
        totals = audit_svc.get_document_session_totals()
        assert totals["max_files_per_session"] == 2
        assert totals["session_max_bytes"] == 512 * 1024


def test_wrapper_does_not_call_chat_or_billing(monkeypatch, session_app):
    chat_mock = MagicMock()
    billing_mock = MagicMock()
    monkeypatch.setattr("app.run_cleiton_gemini_governance.cleiton_governed_generate_content", chat_mock)
    monkeypatch.setattr(
        "app.services.cleiton_operacao_autorizacao_service.avaliar_autorizacao_operacao_por_franquia",
        billing_mock,
    )
    gemini_upload_mock = MagicMock()
    monkeypatch.setattr("app.cleide_audit_doc_service.upload_pdf_to_gemini_files_api", gemini_upload_mock)

    with session_app.test_request_context("/"):
        audit_svc.prepare_and_register_document(
            display_name="nota.txt",
            file_bytes=make_txt("sem ia"),
            mime_type="text/plain",
            extension=".txt",
        )

    chat_mock.assert_not_called()
    billing_mock.assert_not_called()
    gemini_upload_mock.assert_not_called()


def test_build_status_metadata_structure(session_app):
    with session_app.test_request_context("/"):
        audit_svc.prepare_and_register_document(
            display_name="nota.txt",
            file_bytes=make_txt("status"),
            mime_type="text/plain",
            extension=".txt",
        )
        payload = audit_svc.build_document_status_metadata()
        assert payload["domain"] == audit_svc.CLEIDE_AUDIT_DOMAIN
        assert payload["session"]["count"] == 1
        assert len(payload["documents"]) == 1
        assert payload["flow_types"]["upload"] == audit_svc.CLEIDE_AUDIT_DOCUMENT_UPLOAD_FLOW_TYPE
        assert isinstance(payload["allowed_formats"], list)


def test_idempotency_keys_official_format():
    assert audit_svc.cleide_audit_upload_idempotency_key("req-1") == "cleide-audit-upload:req-1"
    assert audit_svc.cleide_audit_upload_doc_idempotency_key("doc-1") == "cleide-audit-upload-doc:doc-1"
    assert audit_svc.cleide_audit_chat_idempotency_key("req-2") == "cleide-audit-chat:req-2"


def test_session_keys_are_isolated_constants():
    assert audit_svc.CLEIDE_AUDIT_DOC_IDS_SESSION_KEY == "cleide_audit_doc_ids"
    assert audit_svc.CLEIDE_AUDIT_DOC_IDS_SESSION_KEY != SESSION_KEY_CLEITON_DOC_IDS
