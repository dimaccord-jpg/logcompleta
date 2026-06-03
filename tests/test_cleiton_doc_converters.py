import json

import pytest

import app.cleiton_doc_service as svc
import app.cleiton_doc_store as store
from app.cleiton_doc_contracts import (
    CONTEXT_KIND_GEMINI_FILE,
    CONTEXT_KIND_TEXT,
    FIELD_CHAR_COUNT,
    FIELD_CONTEXT_KIND,
    FIELD_DOC_ID,
    FIELD_DOC_TYPE,
    FIELD_PREPARED_CONTEXT,
    FIELD_TRUNCATED,
)
from app.cleiton_doc_prepare import prepare_document
from app.cleiton_doc_security import CleitonDocSecurityError
from tests.cleiton_doc_fixtures import (
    make_csv,
    make_docx,
    make_minimal_pdf,
    make_txt,
    make_xlsx,
    make_xml,
    patch_cleiton_doc_cfg,
    patch_cleiton_doc_store,
)


@pytest.fixture
def session_app(app, tmp_path, monkeypatch, ctx):
    patch_cleiton_doc_store(tmp_path, monkeypatch)
    patch_cleiton_doc_cfg(monkeypatch)
    app.config["SECRET_KEY"] = "test-secret"
    return app


def _load_full_record(tmp_path, doc_id: str) -> dict:
    import json as json_mod

    path = tmp_path / f"{doc_id}.json"
    return json_mod.loads(path.read_text(encoding="utf-8"))


def test_prepare_document_returns_standard_shape(doc_cfg):
    result = prepare_document(
        display_name="a.txt",
        file_bytes=make_txt("hello"),
    )
    expected_keys = {
        "doc_type",
        "extension",
        "mime_type",
        "size_bytes",
        "prepared_context",
        "context_kind",
        "truncated",
        "char_count",
        "row_count",
        "column_count",
        "page_count",
        "node_count",
        "max_depth",
        "error_code",
        "warnings",
    }
    assert expected_keys.issubset(set(result.keys()))
    assert result["error_code"] is None


def test_prepare_and_register_stores_prepared_context_not_in_public_record(session_app, tmp_path):
    with session_app.test_request_context("/"):
        doc = svc.prepare_and_register_document(
            display_name="dados.txt",
            file_bytes=make_txt("conteudo temporario"),
            mime_type="text/plain",
        )
        assert doc[FIELD_DOC_TYPE] == "txt"
        assert doc[FIELD_CONTEXT_KIND] == CONTEXT_KIND_TEXT
        assert doc[FIELD_CHAR_COUNT] == len("conteudo temporario")
        assert FIELD_PREPARED_CONTEXT not in doc

        full = _load_full_record(tmp_path, doc[FIELD_DOC_ID])
        assert full[FIELD_PREPARED_CONTEXT] == "conteudo temporario"


def test_prepare_and_register_pdf_gemini_kind(session_app, tmp_path, monkeypatch):
    from tests.cleiton_doc_fixtures import patch_gemini_pdf_upload

    patch_gemini_pdf_upload(monkeypatch)
    with session_app.test_request_context("/"):
        doc = svc.prepare_and_register_document(
            display_name="arquivo.pdf",
            file_bytes=make_minimal_pdf(pages=1),
            mime_type="application/pdf",
        )
        assert doc[FIELD_CONTEXT_KIND] == CONTEXT_KIND_GEMINI_FILE
        full = _load_full_record(tmp_path, doc[FIELD_DOC_ID])
        payload = json.loads(full[FIELD_PREPARED_CONTEXT])
        assert payload["strategy"] == "gemini_file_api"


def test_prepare_and_register_does_not_register_on_failure(session_app, tmp_path):
    with session_app.test_request_context("/"):
        from flask import session

        from app.cleiton_doc_contracts import get_cleiton_doc_ids

        with pytest.raises(CleitonDocSecurityError):
            svc.prepare_and_register_document(
                display_name="bad.exe",
                file_bytes=b"x",
                extension=".exe",
            )
        assert get_cleiton_doc_ids(session) == []
        assert list(tmp_path.glob("*.json")) == []


def test_csv_xlsx_xml_docx_anti_rigidity_no_aliases(doc_cfg):
    csv_result = prepare_document(
        display_name="t.csv",
        file_bytes=make_csv([["x", "y"], ["1", "2"]]),
    )
    xlsx_result = prepare_document(
        display_name="t.xlsx",
        file_bytes=make_xlsx([["p", "q"]]),
    )
    xml_result = prepare_document(
        display_name="t.xml",
        file_bytes=make_xml('<?xml version="1.0"?><custom><v>1</v></custom>'),
    )
    docx_result = prepare_document(
        display_name="t.docx",
        file_bytes=make_docx(["sem estrutura contratual"]),
    )
    for result in (csv_result, xlsx_result, xml_result, docx_result):
        assert result["error_code"] is None
        assert "prepared_context" in result


def test_truncation_flag_propagates_to_register(session_app, monkeypatch):
    patch_cleiton_doc_cfg(monkeypatch, txt_max_chars=3)
    with session_app.test_request_context("/"):
        doc = svc.prepare_and_register_document(
            display_name="t.txt",
            file_bytes=make_txt("abcdef"),
        )
        assert doc[FIELD_TRUNCATED] is True
        assert doc[FIELD_CHAR_COUNT] == 3


def test_public_record_excludes_raw_prepared_context(session_app):
    with session_app.test_request_context("/"):
        doc = svc.prepare_and_register_document(
            display_name="secret.txt",
            file_bytes=make_txt("nao expor"),
        )
        active = svc.get_active_documents_for_session()
        assert active[0][FIELD_DOC_ID] == doc[FIELD_DOC_ID]
        assert FIELD_PREPARED_CONTEXT not in active[0]
