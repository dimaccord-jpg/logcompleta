import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

import app.cleiton_doc_service as svc
import app.cleiton_doc_store as store
from app.cleiton_doc_contracts import (
    ERROR_INVALID_SIZE,
    ERROR_MAX_FILES,
    ERROR_SESSION_BYTES,
    FIELD_DOC_ID,
    FIELD_SIZE_BYTES,
    SESSION_KEY_CLEITON_DOC_IDS,
    get_cleiton_doc_ids,
)
from app.extensions import db
from app.models import ConfigRegras
from app.services import cleiton_doc_config_service as doc_cfg_svc


def _patch_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "get_cleiton_doc_tmp_dir", lambda: str(tmp_path))


def _patch_cfg(monkeypatch, **overrides):
    base = doc_cfg_svc.get_cleiton_doc_config()
    cfg = SimpleNamespace(
        upload_enabled=overrides.get("upload_enabled", base.upload_enabled),
        max_files_per_session=overrides.get("max_files_per_session", base.max_files_per_session),
        session_max_bytes=overrides.get("session_max_bytes", base.session_max_bytes),
        upload_ttl_hours=overrides.get("upload_ttl_hours", base.upload_ttl_hours),
        cleanup_enabled=overrides.get("cleanup_enabled", base.cleanup_enabled),
        prompt_context_max_chars=base.prompt_context_max_chars,
        prompt_max_files_considered=base.prompt_max_files_considered,
        pdf_enabled=base.pdf_enabled,
        pdf_max_bytes=base.pdf_max_bytes,
        pdf_max_pages=base.pdf_max_pages,
        pdf_max_chars=base.pdf_max_chars,
        excel_enabled=base.excel_enabled,
        excel_max_bytes=base.excel_max_bytes,
        excel_max_rows=base.excel_max_rows,
        excel_max_columns=base.excel_max_columns,
        excel_max_chars=base.excel_max_chars,
        docx_enabled=base.docx_enabled,
        docx_max_bytes=base.docx_max_bytes,
        docx_max_paragraphs=base.docx_max_paragraphs,
        docx_max_chars=base.docx_max_chars,
        txt_enabled=base.txt_enabled,
        txt_max_bytes=base.txt_max_bytes,
        txt_max_chars=base.txt_max_chars,
        xml_enabled=base.xml_enabled,
        xml_max_bytes=base.xml_max_bytes,
        xml_max_nodes=base.xml_max_nodes,
        xml_max_depth=base.xml_max_depth,
        xml_max_chars=base.xml_max_chars,
        csv_enabled=base.csv_enabled,
        csv_max_bytes=base.csv_max_bytes,
        csv_max_rows=base.csv_max_rows,
        csv_max_columns=base.csv_max_columns,
        csv_max_chars=base.csv_max_chars,
    )
    monkeypatch.setattr("app.cleiton_doc_service.get_cleiton_doc_config", lambda: cfg)
    return cfg


@pytest.fixture
def session_app(app, tmp_path, monkeypatch, ctx):
    _patch_store(tmp_path, monkeypatch)
    _patch_cfg(monkeypatch)
    app.config["SECRET_KEY"] = "test-secret"
    return app


def test_empty_session_returns_empty_list(session_app):
    with session_app.test_request_context("/"):
        assert svc.get_active_documents_for_session() == []
        totals = svc.get_document_session_totals()
        assert totals["active_count"] == 0
        assert totals["total_bytes"] == 0


def test_register_one_placeholder(session_app):
    with session_app.test_request_context("/"):
        doc = svc.register_document_placeholder(
            display_name="contrato.pdf",
            extension=".pdf",
            mime_type="application/pdf",
            size_bytes=2048,
        )
        assert doc[FIELD_DOC_ID]
        assert doc[FIELD_SIZE_BYTES] == 2048
        active = svc.get_active_documents_for_session()
        assert len(active) == 1
        assert active[0][FIELD_DOC_ID] == doc[FIELD_DOC_ID]


def test_register_up_to_five_documents(session_app):
    with session_app.test_request_context("/"):
        ids = []
        for idx in range(5):
            doc = svc.register_document_placeholder(
                display_name=f"doc{idx}.pdf",
                extension=".pdf",
                mime_type="application/pdf",
                size_bytes=1000,
            )
            ids.append(doc[FIELD_DOC_ID])
        active = svc.get_active_documents_for_session()
        assert len(active) == 5
        assert {item[FIELD_DOC_ID] for item in active} == set(ids)


def test_block_sixth_document_by_max_files(session_app, monkeypatch):
    _patch_cfg(monkeypatch, max_files_per_session=5)
    with session_app.test_request_context("/"):
        for _ in range(5):
            svc.register_document_placeholder(
                display_name="doc.pdf",
                extension=".pdf",
                mime_type="application/pdf",
                size_bytes=100,
            )
        with pytest.raises(svc.CleitonDocSessionError) as exc:
            svc.register_document_placeholder(
                display_name="sexto.pdf",
                extension=".pdf",
                mime_type="application/pdf",
                size_bytes=100,
            )
        assert exc.value.error_code == ERROR_MAX_FILES


def test_block_document_when_session_bytes_exceeded(session_app, monkeypatch):
    _patch_cfg(monkeypatch, session_max_bytes=5000, max_files_per_session=10)
    with session_app.test_request_context("/"):
        svc.register_document_placeholder(
            display_name="a.pdf",
            extension=".pdf",
            mime_type="application/pdf",
            size_bytes=3000,
        )
        with pytest.raises(svc.CleitonDocSessionError) as exc:
            svc.register_document_placeholder(
                display_name="b.pdf",
                extension=".pdf",
                mime_type="application/pdf",
                size_bytes=2500,
            )
        assert exc.value.error_code == ERROR_SESSION_BYTES


def test_list_active_documents(session_app):
    with session_app.test_request_context("/"):
        doc1 = svc.register_document_placeholder(
            display_name="a.pdf",
            extension=".pdf",
            mime_type="application/pdf",
            size_bytes=100,
        )
        doc2 = svc.register_document_placeholder(
            display_name="b.pdf",
            extension=".pdf",
            mime_type="application/pdf",
            size_bytes=200,
        )
        active = svc.get_active_documents_for_session()
        assert {item[FIELD_DOC_ID] for item in active} == {doc1[FIELD_DOC_ID], doc2[FIELD_DOC_ID]}


def test_remove_individual_document(session_app):
    with session_app.test_request_context("/"):
        doc = svc.register_document_placeholder(
            display_name="doc.pdf",
            extension=".pdf",
            mime_type="application/pdf",
            size_bytes=1024,
        )
        result = svc.remove_document_from_session(doc[FIELD_DOC_ID])
        assert result["ok"] is True
        assert result["removed_from_session"] is True
        assert svc.get_active_documents_for_session() == []


def test_clear_all_documents(session_app):
    with session_app.test_request_context("/"):
        svc.register_document_placeholder(
            display_name="a.pdf",
            extension=".pdf",
            mime_type="application/pdf",
            size_bytes=100,
        )
        svc.register_document_placeholder(
            display_name="b.pdf",
            extension=".pdf",
            mime_type="application/pdf",
            size_bytes=100,
        )
        result = svc.clear_documents_for_session()
        assert result["ok"] is True
        assert result["requested"] == 2
        assert svc.get_active_documents_for_session() == []
        from flask import session

        assert get_cleiton_doc_ids(session) == []


def test_expired_document_removed_by_cleanup(session_app, tmp_path):
    with session_app.test_request_context("/"):
        doc = svc.register_document_placeholder(
            display_name="doc.pdf",
            extension=".pdf",
            mime_type="application/pdf",
            size_bytes=100,
        )
        path = tmp_path / f"{doc[FIELD_DOC_ID]}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["expires_at"] = (store._utcnow() - timedelta(hours=1)).isoformat()
        path.write_text(json.dumps(payload), encoding="utf-8")

        removed = svc.cleanup_expired_documents_for_session()
        assert removed == 1
        assert svc.get_active_documents_for_session() == []


def test_non_expired_document_remains_after_cleanup(session_app):
    with session_app.test_request_context("/"):
        doc = svc.register_document_placeholder(
            display_name="doc.pdf",
            extension=".pdf",
            mime_type="application/pdf",
            size_bytes=100,
        )
        removed = svc.cleanup_expired_documents_for_session()
        assert removed == 0
        active = svc.get_active_documents_for_session()
        assert len(active) == 1
        assert active[0][FIELD_DOC_ID] == doc[FIELD_DOC_ID]


def test_clear_tolerates_already_removed_document(session_app, tmp_path):
    with session_app.test_request_context("/"):
        doc = svc.register_document_placeholder(
            display_name="doc.pdf",
            extension=".pdf",
            mime_type="application/pdf",
            size_bytes=100,
        )
        path = tmp_path / f"{doc[FIELD_DOC_ID]}.json"
        path.unlink(missing_ok=True)
        result = svc.clear_documents_for_session()
        assert result["ok"] is True
        assert result["requested"] == 1


def test_ttl_uses_config_upload_ttl_hours(session_app, monkeypatch):
    _patch_cfg(monkeypatch, upload_ttl_hours=12)
    with session_app.test_request_context("/"):
        doc = svc.register_document_placeholder(
            display_name="ttl.pdf",
            extension=".pdf",
            mime_type="application/pdf",
            size_bytes=100,
        )
        expires_at = store._parse_iso(doc["expires_at"])
        created_at = store._parse_iso(doc["created_at"])
        assert expires_at is not None and created_at is not None
        delta = expires_at - created_at
        assert delta == timedelta(hours=12)


def test_disable_cleanup_blocks_automatic_cleanup(session_app, tmp_path, monkeypatch):
    _patch_cfg(monkeypatch, cleanup_enabled=False)
    with session_app.test_request_context("/"):
        doc = svc.register_document_placeholder(
            display_name="doc.pdf",
            extension=".pdf",
            mime_type="application/pdf",
            size_bytes=100,
        )
        path = tmp_path / f"{doc[FIELD_DOC_ID]}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["expires_at"] = (store._utcnow() - timedelta(hours=1)).isoformat()
        path.write_text(json.dumps(payload), encoding="utf-8")

    removed = svc.maybe_cleanup_expired_cleiton_docs(min_interval_seconds=0)
    assert removed == 0
    assert path.exists()


def test_session_behavior_is_preserved(session_app):
    with session_app.test_request_context("/"):
        doc = svc.register_document_placeholder(
            display_name="doc.pdf",
            extension=".pdf",
            mime_type="application/pdf",
            size_bytes=100,
        )
        doc_id = doc[FIELD_DOC_ID]

    with session_app.test_request_context("/other"):
        assert svc.get_active_documents_for_session() == []

    with session_app.test_request_context("/"):
        from flask import session

        session[SESSION_KEY_CLEITON_DOC_IDS] = [doc_id]
        active = svc.get_active_documents_for_session()
        assert len(active) == 1


def test_no_sql_table_for_document_content(ctx):
    import app.models as models

    table_names = set(db.metadata.tables.keys())
    forbidden = [
        name
        for name in table_names
        if "cleiton_doc" in name and name not in {"config_regras"}
    ]
    assert forbidden == []
    assert not hasattr(models, "CleitonDocContent")
    assert not hasattr(models, "CleitonDocument")


def test_limits_use_config_values(session_app, monkeypatch, ctx):
    doc_cfg_svc.salvar_cleiton_doc_config(
        {
            "upload_enabled": "1",
            "max_files_per_session": "3",
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
        "app.cleiton_doc_service.get_cleiton_doc_config",
        doc_cfg_svc.get_cleiton_doc_config,
    )

    with session_app.test_request_context("/"):
        totals = svc.get_document_session_totals()
        assert totals["max_files_per_session"] == 3
        assert totals["session_max_bytes"] == 512 * 1024

        for _ in range(3):
            svc.register_document_placeholder(
                display_name="x.pdf",
                extension=".pdf",
                mime_type="application/pdf",
                size_bytes=1000,
            )
        with pytest.raises(svc.CleitonDocSessionError) as exc:
            svc.register_document_placeholder(
                display_name="extra.pdf",
                extension=".pdf",
                mime_type="application/pdf",
                size_bytes=100,
            )
        assert exc.value.error_code == ERROR_MAX_FILES

    rows = ConfigRegras.query.filter(ConfigRegras.chave == "cleiton_doc_max_files_per_session").all()
    assert len(rows) == 1
    assert rows[0].valor_inteiro == 3


def _placeholder_kwargs(**overrides):
    payload = {
        "display_name": "doc.pdf",
        "extension": ".pdf",
        "mime_type": "application/pdf",
        "size_bytes": 1024,
    }
    payload.update(overrides)
    return payload


def _store_json_files(tmp_path):
    return [p for p in tmp_path.glob("*.json") if p.name != ".cleanup_meta.json"]


def test_reject_negative_size_bytes(session_app, tmp_path):
    with session_app.test_request_context("/"):
        from flask import session

        with pytest.raises(svc.CleitonDocSessionError) as exc:
            svc.register_document_placeholder(**_placeholder_kwargs(size_bytes=-10))
        assert exc.value.error_code == ERROR_INVALID_SIZE
        assert get_cleiton_doc_ids(session) == []
        assert _store_json_files(tmp_path) == []


def test_reject_non_numeric_size_bytes(session_app, tmp_path):
    with session_app.test_request_context("/"):
        from flask import session

        with pytest.raises(svc.CleitonDocSessionError) as exc:
            svc.register_document_placeholder(**_placeholder_kwargs(size_bytes="abc"))
        assert exc.value.error_code == ERROR_INVALID_SIZE
        assert get_cleiton_doc_ids(session) == []
        assert _store_json_files(tmp_path) == []


def test_reject_missing_size_bytes(session_app, tmp_path):
    with session_app.test_request_context("/"):
        from flask import session

        with pytest.raises(svc.CleitonDocSessionError) as exc:
            svc.assert_session_can_accept_document(None)
        assert exc.value.error_code == ERROR_INVALID_SIZE
        assert get_cleiton_doc_ids(session) == []
        assert _store_json_files(tmp_path) == []


def test_reject_zero_size_bytes(session_app, tmp_path):
    with session_app.test_request_context("/"):
        from flask import session

        with pytest.raises(svc.CleitonDocSessionError) as exc:
            svc.register_document_placeholder(**_placeholder_kwargs(size_bytes=0))
        assert exc.value.error_code == ERROR_INVALID_SIZE
        assert get_cleiton_doc_ids(session) == []
        assert _store_json_files(tmp_path) == []


def test_reject_size_bytes_above_session_limit(session_app, monkeypatch, tmp_path):
    _patch_cfg(monkeypatch, session_max_bytes=5000)
    with session_app.test_request_context("/"):
        from flask import session

        with pytest.raises(svc.CleitonDocSessionError) as exc:
            svc.register_document_placeholder(**_placeholder_kwargs(size_bytes=5001))
        assert exc.value.error_code == ERROR_INVALID_SIZE
        assert get_cleiton_doc_ids(session) == []
        assert _store_json_files(tmp_path) == []


def test_valid_positive_size_bytes_still_works(session_app, tmp_path):
    with session_app.test_request_context("/"):
        from flask import session

        doc = svc.register_document_placeholder(**_placeholder_kwargs(size_bytes=2048))
        assert doc[FIELD_SIZE_BYTES] == 2048
        assert len(get_cleiton_doc_ids(session)) == 1
        assert len(_store_json_files(tmp_path)) == 1


def test_assert_session_can_accept_document_rejects_invalid_before_limits(session_app):
    with session_app.test_request_context("/"):
        with pytest.raises(svc.CleitonDocSessionError) as exc:
            svc.assert_session_can_accept_document(-1)
        assert exc.value.error_code == ERROR_INVALID_SIZE

        with pytest.raises(svc.CleitonDocSessionError) as exc2:
            svc.assert_session_can_accept_document("x")
        assert exc2.value.error_code == ERROR_INVALID_SIZE
