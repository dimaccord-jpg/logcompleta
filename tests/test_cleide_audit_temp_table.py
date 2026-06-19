"""Testes da tabela temporária da Cleide Auditoria (fase 1 — pipeline pós-upload)."""
from __future__ import annotations

import importlib
import io
import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.cleide_audit_doc_service as audit_doc_service
import app.run_cleide_audit_chat as audit_chat
import app.run_cleide_audit_temp_table as audit_temp_table
from app.cleide_audit_doc_service import (
    CLEIDE_AUDIT_TEMP_TABLE_EXTRACTION_FLOW_TYPE,
    TEMP_TABLE_JSON_BEGIN,
    TEMP_TABLE_JSON_END,
    TEMP_TABLE_STATUS_AWAITING_VALIDATION,
    TEMP_TABLE_STATUS_DISCARDED,
    TEMP_TABLE_STATUS_FAILED,
    TEMP_TABLE_STATUS_NEEDS_REVIEW,
    TEMP_TABLE_STATUS_PROCESSING,
    apply_temp_table_extraction_from_model_payload,
    build_document_status_metadata,
    get_cleide_audit_doc_ids,
    invalidate_temp_table_for_session,
    mark_temp_table_processing,
    normalize_partial_first_extraction_to_temp_table,
    remove_document_from_session,
    save_temp_table_edit,
    should_attempt_temp_table_extraction,
    split_temp_table_block_from_answer,
    temp_table_status_message,
    CleideAuditCoverageError,
    CleideAuditTempTableError,
    ERROR_COVERAGE_PARSE_FAILED,
    ERROR_TEMP_TABLE_INVALID_PAYLOAD,
    ERROR_TEMP_TABLE_NOT_FOUND,
    HUMAN_REVIEW_STATUS_EDITED,
    HUMAN_REVIEW_STATUS_REVIEWED,
    TEMP_TABLE_SAVE_MAX_PAYLOAD_BYTES,
    _parse_coverage_tabular_rows,
    _resolve_coverage_field,
)
from app.cleide_audit_prompt import build_cleide_audit_temp_table_technical_prompt
from app.services.cleide_audit_config_service import CleideAuditConfig, DEFAULT_FALLBACK_MESSAGE
from tests.cleiton_doc_fixtures import (
    make_audit_xlsx,
    make_csv,
    make_docx,
    make_minimal_pdf,
    make_txt,
    make_xlsx,
    make_xml,
    patch_cleiton_doc_cfg,
    patch_cleiton_doc_store,
    patch_gemini_pdf_upload,
)


def _default_audit_cfg(**overrides):
    defaults = {
        "chat_enabled": True,
        "upload_enabled": True,
        "chat_max_history": 10,
        "document_context_max_chars": 24000,
        "max_documents_considered": 3,
        "question_max_chars": 4000,
        "fallback_message": DEFAULT_FALLBACK_MESSAGE,
        "no_documents_behavior": "allow_guided",
        "show_documents_used": True,
        "no_hallucination_instruction_enabled": True,
        "audited_file_max_rows": 2000,
    }
    defaults.update(overrides)
    return CleideAuditConfig(**defaults)


def _patch_audit_cfg(monkeypatch, **overrides):
    cfg = _default_audit_cfg(**overrides)
    for target in (
        "app.cleide_audit_routes.get_cleide_audit_config",
        "app.cleide_audit_doc_context.get_cleide_audit_config",
        "app.cleide_audit_doc_service.get_cleide_audit_config",
        "app.run_cleide_audit_chat.get_cleide_audit_config",
    ):
        monkeypatch.setattr(target, lambda _cfg=cfg: _cfg)
    return cfg


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


def _setup_doc_env(monkeypatch, tmp_path, **cfg_overrides):
    patch_cleiton_doc_store(tmp_path, monkeypatch)
    cfg = patch_cleiton_doc_cfg(monkeypatch, **cfg_overrides)
    monkeypatch.setattr("app.cleide_audit_doc_service.get_cleiton_doc_config", lambda: cfg)
    monkeypatch.setattr("app.cleide_audit_routes.get_cleiton_doc_config", lambda: cfg)
    monkeypatch.setattr("app.cleide_audit_doc_context.get_cleiton_doc_config", lambda: cfg)
    _patch_audit_cfg(monkeypatch)
    return cfg


def _authorized(monkeypatch, web, *, authz=None):
    fake_user = SimpleNamespace(is_authenticated=True, conta_id=1, franquia_id=1, id=42)
    monkeypatch.setattr(web, "current_user", fake_user)
    monkeypatch.setattr("app.cleide_audit_routes.current_user", fake_user)
    authz_payload = authz or {"permitido": True, "modo_operacao": "normal"}
    monkeypatch.setattr(
        web,
        "avaliar_autorizacao_operacao_por_franquia",
        lambda _u: authz_payload,
    )
    monkeypatch.setattr(
        "app.cleide_audit_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _u: authz_payload,
    )


def _upload(client, filename: str, content: bytes, mime: str = "text/plain"):
    return client.post(
        "/api/cleide-auditoria/documents/upload",
        data={"file": (io.BytesIO(content), filename, mime)},
        content_type="multipart/form-data",
    )


def _chat(client, message: str = "analisar tabela", *, request_id: str = "temp-table-test"):
    return client.post(
        "/api/cleide-auditoria/chat",
        json={"message": message, "history": [], "request_id": request_id},
        content_type="application/json",
    )


def _fake_extraction_generate(monkeypatch, *, text: str):
    captured: dict = {}

    class _Resp:
        pass

    def _fake(client, model, contents, agent, flow_type, api_key_label):
        captured["contents"] = contents
        captured["flow_type"] = flow_type
        captured["agent"] = agent
        resp = _Resp()
        resp.text = text
        return resp

    monkeypatch.setattr(audit_temp_table, "cleiton_governed_generate_content", _fake)
    monkeypatch.setattr(audit_temp_table, "_get_client", lambda: object())
    return captured


def _fake_chat_generate(monkeypatch, *, text: str = "Resposta conversacional."):
    captured: dict = {}

    class _Resp:
        pass

    def _fake(client, model, contents, agent, flow_type, api_key_label):
        captured["contents"] = contents
        captured["flow_type"] = flow_type
        resp = _Resp()
        resp.text = text
        return resp

    monkeypatch.setattr(audit_chat, "cleiton_governed_generate_content", _fake)
    monkeypatch.setattr(audit_chat, "_get_client", lambda: object())
    return captured


@pytest.fixture
def web_client(app, tmp_path, monkeypatch, ctx):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    _authorized(monkeypatch, web)
    web.app.config["TESTING"] = True
    return web.app.test_client()


def test_split_temp_table_block_from_answer():
    payload = {"status": "awaiting_validation", "extracted_items": [{"route": "SP-RJ"}]}
    raw = f"Resposta visível.\n{TEMP_TABLE_JSON_BEGIN}\n{json.dumps(payload)}\n{TEMP_TABLE_JSON_END}"
    visible, parsed = split_temp_table_block_from_answer(raw)
    assert visible == "Resposta visível."
    assert parsed["status"] == "awaiting_validation"


def test_status_exposes_temp_table_null_when_absent(web_client):
    body = web_client.get("/api/cleide-auditoria/documents/status").get_json()
    assert body["ok"] is True
    assert body.get("temp_table") is None


def test_upload_csv_creates_temp_table(web_client, monkeypatch):
    payload = {
        "status": "awaiting_validation",
        "detected_carrier": "Transportadora X",
        "origins": ["SP"],
        "destinations": ["RJ"],
        "routes": ["SP-RJ"],
        "weight_ranges": ["0-30kg"],
        "freight_values": ["120.00"],
        "accessorial_fees": ["TDE 15.00"],
        "charge_type_detected": "por faixa de peso",
        "extracted_items": [{"origin": "SP", "destination": "RJ", "value": "120.00"}],
        "uncertain_fields": [],
        "reading_alerts": [],
        "evidence_refs": [],
    }
    captured = _fake_extraction_generate(monkeypatch, text=json.dumps(payload))
    up = _upload(
        web_client,
        "tabela.csv",
        make_csv([["origem", "destino", "valor"], ["SP", "RJ", "120.00"]]),
        "text/csv",
    ).get_json()
    doc_id = up["document"]["doc_id"]
    assert captured["flow_type"] == CLEIDE_AUDIT_TEMP_TABLE_EXTRACTION_FLOW_TYPE
    assert captured["agent"] == "cleide"
    assert "Tarefa adicional nesta resposta" not in str(captured["contents"])

    status = web_client.get("/api/cleide-auditoria/documents/status").get_json()
    temp_table = status["temp_table"]
    assert temp_table is not None
    assert temp_table["status"] == TEMP_TABLE_STATUS_NEEDS_REVIEW
    assert doc_id in temp_table["source_documents"]
    assert temp_table["ui_visibility"]["readonly"] is True
    assert temp_table["operational_owner"] == "cleiton"


def test_upload_triggers_extraction_not_chat(web_client, monkeypatch):
    payload = {"status": "awaiting_validation", "extracted_items": []}
    extraction_mock = MagicMock()
    chat_mock = MagicMock()

    class _Resp:
        text = json.dumps(payload)

    extraction_mock.return_value = _Resp()
    monkeypatch.setattr(audit_temp_table, "cleiton_governed_generate_content", extraction_mock)
    monkeypatch.setattr(audit_temp_table, "_get_client", lambda: object())
    monkeypatch.setattr(audit_chat, "cleiton_governed_generate_content", chat_mock)
    monkeypatch.setattr(audit_chat, "_get_client", lambda: object())

    resp = _upload(web_client, "tabela.csv", make_csv([["a", "b"], ["1", "2"]]), "text/csv")
    assert resp.status_code == 200
    extraction_mock.assert_called()
    chat_mock.assert_not_called()


def test_needs_review_when_uncertain_fields(web_client, monkeypatch):
    payload = {
        "status": "awaiting_validation",
        "uncertain_fields": ["transportadora"],
        "reading_alerts": ["layout ambíguo"],
        "extracted_items": [],
    }
    _fake_extraction_generate(monkeypatch, text=json.dumps(payload))
    _upload(web_client, "tabela.csv", make_csv([["a", "b"], ["1", "2"]]), "text/csv")
    temp_table = web_client.get("/api/cleide-auditoria/documents/status").get_json()["temp_table"]
    assert temp_table["status"] == TEMP_TABLE_STATUS_NEEDS_REVIEW


def test_failed_when_model_payload_missing(web_client, monkeypatch):
    _fake_extraction_generate(monkeypatch, text="Não encontrei estrutura tabular.")
    _upload(web_client, "tabela.txt", make_txt("conteudo ilegivel"))
    temp_table = web_client.get("/api/cleide-auditoria/documents/status").get_json()["temp_table"]
    assert temp_table["status"] == TEMP_TABLE_STATUS_FAILED
    assert audit_temp_table.READING_ALERT_PARSER_NO_JSON in temp_table["reading_alerts"]


def test_second_upload_same_set_does_not_duplicate_extraction(web_client, monkeypatch):
    payload = {"status": "awaiting_validation", "extracted_items": []}
    calls = {"count": 0}

    class _Resp:
        text = json.dumps(payload)

    def _fake(*args, **kwargs):
        calls["count"] += 1
        return _Resp()

    monkeypatch.setattr(audit_temp_table, "cleiton_governed_generate_content", _fake)
    monkeypatch.setattr(audit_temp_table, "_get_client", lambda: object())
    first = _upload(web_client, "tabela.csv", make_csv([["a", "b"], ["1", "2"]]), "text/csv")
    assert first.status_code == 200
    assert calls["count"] == 1
    status = web_client.get("/api/cleide-auditoria/documents/status").get_json()
    assert status["temp_table"]["status"] == TEMP_TABLE_STATUS_AWAITING_VALIDATION


def test_remove_document_invalidates_temp_table(web_client, monkeypatch):
    payload = {"status": "awaiting_validation", "extracted_items": []}
    _fake_extraction_generate(monkeypatch, text=json.dumps(payload))
    up = _upload(web_client, "tabela.csv", make_csv([["a", "b"], ["1", "2"]]), "text/csv").get_json()
    doc_id = up["document"]["doc_id"]
    assert web_client.get("/api/cleide-auditoria/documents/status").get_json()["temp_table"]

    delete_resp = web_client.delete(f"/api/cleide-auditoria/documents/{doc_id}")
    assert delete_resp.status_code == 200
    status = web_client.get("/api/cleide-auditoria/documents/status").get_json()
    assert status["temp_table"] is None


def test_clear_session_invalidates_temp_table(web_client, monkeypatch):
    payload = {"status": "awaiting_validation", "extracted_items": []}
    _fake_extraction_generate(monkeypatch, text=json.dumps(payload))
    _upload(web_client, "tabela.csv", make_csv([["a", "b"], ["1", "2"]]), "text/csv")
    assert web_client.get("/api/cleide-auditoria/documents/status").get_json()["temp_table"]

    clear_resp = web_client.post("/api/cleide-auditoria/documents/clear")
    assert clear_resp.status_code == 200
    assert web_client.get("/api/cleide-auditoria/documents/status").get_json()["temp_table"] is None


def test_expired_temp_table_status(web_client, monkeypatch, tmp_path):
    payload = {"status": "awaiting_validation", "extracted_items": []}
    _fake_extraction_generate(monkeypatch, text=json.dumps(payload))
    _upload(web_client, "tabela.csv", make_csv([["a", "b"], ["1", "2"]]), "text/csv")
    status = web_client.get("/api/cleide-auditoria/documents/status").get_json()
    temp_table_id = status["temp_table"]["temp_table_id"]

    expired_at = (datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)).isoformat()
    path = audit_doc_service._temp_table_path(temp_table_id)
    with open(path, "r", encoding="utf-8") as handle:
        record = json.load(handle)
    record["expires_at"] = expired_at
    record["created_at"] = expired_at
    record["updated_at"] = expired_at
    audit_doc_service._write_temp_table_atomic(path, record)

    body = web_client.get("/api/cleide-auditoria/documents/status").get_json()
    assert body["temp_table"]["status"] == audit_doc_service.TEMP_TABLE_STATUS_EXPIRED


def test_anonymous_status_still_401(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    anon = SimpleNamespace(is_authenticated=False)
    monkeypatch.setattr(web, "current_user", anon)
    monkeypatch.setattr("app.cleide_audit_routes.current_user", anon)
    assert web.app.test_client().get("/api/cleide-auditoria/documents/status").status_code == 401


def test_public_auditoria_page_unchanged(monkeypatch):
    web = _load_web_module()
    html = web.app.test_client().get("/auditoria-frete").get_data(as_text=True)
    assert "Cleide, Auditora Virtual de AgenteFrete" in html
    assert "cleide_auditoria.js" in html


def test_no_new_routes_except_temp_table_save(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    rules = {rule.rule for rule in web.app.url_map.iter_rules()}
    assert "/api/cleide-auditoria/documents/status" in rules
    assert "/api/cleide-auditoria/temp-table/save" in rules
    assert not any("checklist" in rule for rule in rules)


def test_pdf_csv_xlsx_upload_paths(web_client, monkeypatch):
    patch_gemini_pdf_upload(monkeypatch)
    payload = {"status": "awaiting_validation", "extracted_items": []}
    _fake_extraction_generate(monkeypatch, text=json.dumps(payload))
    assert _upload(web_client, "t.pdf", make_minimal_pdf(), "application/pdf").status_code == 200
    assert web_client.get("/api/cleide-auditoria/documents/status").get_json()["temp_table"]
    assert _upload(web_client, "t.csv", make_csv([["a"], ["1"]]), "text/csv").status_code == 200
    assert _upload(
        web_client,
        "t.xlsx",
        make_xlsx([["a"], ["1"]]),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ).status_code == 200


def test_txt_xml_docx_upload_generates_temp_table(web_client, monkeypatch):
    payload = {"status": "awaiting_validation", "extracted_items": []}
    _fake_extraction_generate(monkeypatch, text=json.dumps(payload))
    assert _upload(web_client, "d.txt", make_txt("frete 100")).status_code == 200
    assert _upload(
        web_client,
        "d.xml",
        make_xml('<?xml version="1.0"?><root><item>ok</item></root>'),
        "application/xml",
    ).status_code == 200
    assert _upload(
        web_client,
        "d.docx",
        make_docx(["paragrafo"]),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ).status_code == 200
    assert web_client.get("/api/cleide-auditoria/documents/status").get_json()["temp_table"]


def test_should_attempt_false_without_documents(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
        with app.test_request_context():
            assert should_attempt_temp_table_extraction(audit_doc_service.session, []) is False


def test_mark_processing_and_apply_unit(web_client):
    with web_client.session_transaction() as sess:
        sess[audit_doc_service.CLEIDE_AUDIT_DOC_IDS_SESSION_KEY] = ["doc-1"]
    with web_client.application.app_context():
        with web_client.application.test_request_context():
            record = mark_temp_table_processing(["doc-1"])
            assert record["status"] == TEMP_TABLE_STATUS_PROCESSING
            saved = apply_temp_table_extraction_from_model_payload(
                {"status": "awaiting_validation", "extracted_items": [{"x": 1}]},
                source_doc_ids=["doc-1"],
            )
            assert saved["status"] == TEMP_TABLE_STATUS_AWAITING_VALIDATION


def test_invalidate_clears_session(web_client):
    with web_client.session_transaction() as sess:
        sess[audit_doc_service.CLEIDE_AUDIT_DOC_IDS_SESSION_KEY] = ["doc-1"]
    with web_client.application.app_context():
        with web_client.application.test_request_context():
            mark_temp_table_processing(["doc-1"])
            invalidate_temp_table_for_session(reason=TEMP_TABLE_STATUS_DISCARDED)
            assert audit_doc_service.get_temp_table_id(audit_doc_service.session) is None


def _apply_payload(web_client, payload: dict) -> dict:
    with web_client.session_transaction() as sess:
        sess[audit_doc_service.CLEIDE_AUDIT_DOC_IDS_SESSION_KEY] = ["doc-1"]
    with web_client.application.app_context():
        with web_client.application.test_request_context():
            mark_temp_table_processing(["doc-1"])
            saved = apply_temp_table_extraction_from_model_payload(payload, source_doc_ids=["doc-1"])
    with web_client.session_transaction() as sess:
        sess[audit_doc_service.CLEIDE_AUDIT_TEMP_TABLE_ID_SESSION_KEY] = saved["temp_table_id"]
        sess[audit_doc_service.CLEIDE_AUDIT_TEMP_TABLE_SOURCE_DOCS_SESSION_KEY] = list(
            saved.get("source_documents") or ["doc-1"]
        )
        sess[audit_doc_service.CLEIDE_AUDIT_DOC_IDS_SESSION_KEY] = list(
            saved.get("source_documents") or ["doc-1"]
        )
    return saved


def test_coerce_status_integer_falls_back_to_failed(web_client):
    saved = _apply_payload(web_client, {"status": 123, "extracted_items": []})
    assert saved["status"] == TEMP_TABLE_STATUS_FAILED


def test_coerce_status_missing_falls_back_to_failed(web_client):
    saved = _apply_payload(web_client, {"extracted_items": []})
    assert saved["status"] == TEMP_TABLE_STATUS_FAILED


def test_coerce_status_valid_string_preserved(web_client):
    saved = _apply_payload(
        web_client,
        {"status": "awaiting_validation", "extracted_items": [{"route": "SP-RJ"}]},
    )
    assert saved["status"] == TEMP_TABLE_STATUS_AWAITING_VALIDATION


def test_coerce_status_invalid_string_falls_back_to_failed(web_client):
    saved = _apply_payload(web_client, {"status": "not_a_real_status", "extracted_items": []})
    assert saved["status"] == TEMP_TABLE_STATUS_FAILED


def test_should_not_retry_when_failed_same_documents(web_client):
    with web_client.application.app_context():
        with web_client.application.test_request_context():
            from flask import session as flask_session

            flask_session[audit_doc_service.CLEIDE_AUDIT_DOC_IDS_SESSION_KEY] = ["doc-a"]
            apply_temp_table_extraction_from_model_payload(
                {"status": "failed", "extracted_items": []},
                source_doc_ids=["doc-a"],
            )
            assert should_attempt_temp_table_extraction(flask_session, ["doc-a"]) is False


def test_should_not_retry_when_awaiting_validation_or_needs_review(web_client):
    with web_client.application.app_context():
        with web_client.application.test_request_context():
            from flask import session as flask_session

            flask_session[audit_doc_service.CLEIDE_AUDIT_DOC_IDS_SESSION_KEY] = ["doc-a"]
            apply_temp_table_extraction_from_model_payload(
                {"status": "awaiting_validation", "extracted_items": []},
                source_doc_ids=["doc-a"],
            )
            assert should_attempt_temp_table_extraction(flask_session, ["doc-a"]) is False


def test_should_retry_when_source_documents_change(web_client):
    with web_client.application.app_context():
        with web_client.application.test_request_context():
            from flask import session as flask_session

            flask_session[audit_doc_service.CLEIDE_AUDIT_DOC_IDS_SESSION_KEY] = ["doc-a", "doc-b"]
            apply_temp_table_extraction_from_model_payload(
                {"status": "failed", "extracted_items": []},
                source_doc_ids=["doc-a"],
            )
            assert should_attempt_temp_table_extraction(
                flask_session, ["doc-a", "doc-b"]
            ) is True


def test_chat_does_not_create_temp_table(web_client, monkeypatch):
    payload = {"status": "awaiting_validation", "extracted_items": []}
    _fake_extraction_generate(monkeypatch, text=json.dumps(payload))
    _upload(web_client, "tabela.csv", make_csv([["a", "b"], ["1", "2"]]), "text/csv")
    before = web_client.get("/api/cleide-auditoria/documents/status").get_json()["temp_table"]
    captured = _fake_chat_generate(monkeypatch, text="Resposta conversacional sem bloco técnico.")
    resp = _chat(web_client, request_id="chat-no-temp-1")
    assert resp.status_code == 200
    assert TEMP_TABLE_JSON_BEGIN not in resp.get_json()["answer"]
    assert "extrator tecnico" not in str(captured.get("contents", "")).lower()
    after = web_client.get("/api/cleide-auditoria/documents/status").get_json()["temp_table"]
    assert after["temp_table_id"] == before["temp_table_id"]
    assert after["status"] == before["status"]


def test_chat_does_not_use_technical_extraction_prompt(web_client, monkeypatch):
    payload = {"status": "awaiting_validation", "extracted_items": []}
    _fake_extraction_generate(monkeypatch, text=json.dumps(payload))
    _upload(web_client, "t.csv", make_csv([["a"], ["1"]]), "text/csv")
    captured = _fake_chat_generate(
        monkeypatch,
        text="Resposta conversacional sobre o anexo.",
    )
    resp = _chat(web_client, request_id="chat-block-test")
    assert resp.status_code == 200
    contents = str(captured.get("contents", ""))
    assert "extrator tecnico" not in contents.lower()
    assert "Tarefa adicional nesta resposta" not in contents


def test_timeout_marks_failed(web_client, monkeypatch):
    def _fail(*_args, **_kwargs):
        raise TimeoutError("gemini timeout")

    monkeypatch.setattr(audit_temp_table, "cleiton_governed_generate_content", _fail)
    monkeypatch.setattr(audit_temp_table, "_get_client", lambda: object())
    _upload(web_client, "t.csv", make_csv([["a"], ["1"]]), "text/csv")
    temp_table = web_client.get("/api/cleide-auditoria/documents/status").get_json()["temp_table"]
    assert temp_table["status"] == TEMP_TABLE_STATUS_FAILED
    assert audit_temp_table.READING_ALERT_PROVIDER_TIMEOUT in temp_table["reading_alerts"]


def test_multiple_documents_consolidated(web_client, monkeypatch):
    payload = {"status": "awaiting_validation", "extracted_items": [{"route": "SP-RJ"}]}
    captured = _fake_extraction_generate(monkeypatch, text=json.dumps(payload))
    _upload(web_client, "a.csv", make_csv([["origem"], ["SP"]]), "text/csv")
    _upload(web_client, "b.csv", make_csv([["destino"], ["RJ"]]), "text/csv")
    status = web_client.get("/api/cleide-auditoria/documents/status").get_json()
    assert len(status["temp_table"]["source_documents"]) == 2
    assert "origem" in str(captured.get("contents", "")).lower() or captured.get("contents")


def test_new_document_set_after_failed_allows_retry(web_client, monkeypatch):
    _fake_extraction_generate(monkeypatch, text="Sem json.")
    first = _upload(
        web_client,
        "tabela_a.csv",
        make_csv([["a"], ["1"]]),
        "text/csv",
    ).get_json()
    doc_a = first["document"]["doc_id"]
    assert (
        web_client.get("/api/cleide-auditoria/documents/status").get_json()["temp_table"]["status"]
        == TEMP_TABLE_STATUS_FAILED
    )

    web_client.delete(f"/api/cleide-auditoria/documents/{doc_a}")
    payload = {"status": "awaiting_validation", "extracted_items": []}
    captured = _fake_extraction_generate(monkeypatch, text=json.dumps(payload))
    _upload(web_client, "tabela_b.csv", make_csv([["b"], ["2"]]), "text/csv")
    assert captured["flow_type"] == CLEIDE_AUDIT_TEMP_TABLE_EXTRACTION_FLOW_TYPE
    assert (
        web_client.get("/api/cleide-auditoria/documents/status").get_json()["temp_table"]["status"]
        == TEMP_TABLE_STATUS_AWAITING_VALIDATION
    )


def test_pdf_upload_generates_temp_table(web_client, monkeypatch):
    patch_gemini_pdf_upload(monkeypatch)
    monkeypatch.setattr(
        "app.cleide_audit_doc_context.build_gemini_file_part_for_generate",
        lambda record: {"pdf": record.get("gemini_file_name")},
    )
    payload = {"status": "awaiting_validation", "extracted_items": []}
    captured = _fake_extraction_generate(monkeypatch, text=json.dumps(payload))
    resp = _upload(web_client, "tabela.pdf", make_minimal_pdf(), "application/pdf")
    assert resp.status_code == 200
    assert captured["flow_type"] == CLEIDE_AUDIT_TEMP_TABLE_EXTRACTION_FLOW_TYPE
    assert web_client.get("/api/cleide-auditoria/documents/status").get_json()["temp_table"]


def test_xlsx_upload_generates_temp_table(web_client, monkeypatch):
    payload = {"status": "awaiting_validation", "extracted_items": []}
    _fake_extraction_generate(monkeypatch, text=json.dumps(payload))
    resp = _upload(
        web_client,
        "tabela.xlsx",
        make_xlsx([["origem", "valor"], ["SP", "100"]]),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    assert resp.status_code == 200
    assert (
        web_client.get("/api/cleide-auditoria/documents/status").get_json()["temp_table"]["status"]
        == TEMP_TABLE_STATUS_AWAITING_VALIDATION
    )


def test_operational_messages_defined():
    assert "estruturação" in temp_table_status_message(TEMP_TABLE_STATUS_PROCESSING).lower()
    assert "aguardando" in temp_table_status_message(TEMP_TABLE_STATUS_AWAITING_VALIDATION).lower()
    assert (
        temp_table_status_message(TEMP_TABLE_STATUS_NEEDS_REVIEW)
        == "A tabela temporária foi gerada. Revise os dados antes de continuar."
    )
    assert "leitura incerta" not in temp_table_status_message(TEMP_TABLE_STATUS_NEEDS_REVIEW)


def test_technical_prompt_is_not_conversational():
    prompt = build_cleide_audit_temp_table_technical_prompt()
    assert "extrator tecnico" in prompt.lower()
    assert "Tarefa adicional nesta resposta" not in prompt
    assert "nao use markdown" in prompt.lower()
    assert "nao use bloco de codigo" in prompt.lower()


def test_parse_extraction_response_accepts_plain_json():
    payload = {"status": "awaiting_validation", "extracted_items": []}
    parsed = audit_temp_table._parse_extraction_response(json.dumps(payload))
    assert parsed == payload


def test_parse_extraction_response_accepts_json_code_fence():
    payload = {"status": "awaiting_validation", "extracted_items": []}
    raw = f"```json\n{json.dumps(payload, indent=2)}\n```"
    parsed = audit_temp_table._parse_extraction_response(raw)
    assert parsed == payload


def test_parse_extraction_response_accepts_untyped_code_fence():
    payload = {"status": "needs_review", "reading_alerts": ["Leitura parcial"]}
    raw = f"```\n{json.dumps(payload, indent=2)}\n```"
    parsed = audit_temp_table._parse_extraction_response(raw)
    assert parsed == payload


def test_parse_extraction_response_extracts_json_from_surrounding_text():
    raw = (
        'Segue a estrutura solicitada: {"status":"needs_review",'
        '"reading_alerts":["Leitura parcial"]} Revise os dados.'
    )
    parsed = audit_temp_table._parse_extraction_response(raw)
    assert parsed["status"] == "needs_review"
    assert parsed["reading_alerts"] == ["Leitura parcial"]


def test_parse_extraction_response_returns_none_for_no_json():
    assert audit_temp_table._parse_extraction_response("Não consegui estruturar a tabela.") is None
    assert audit_temp_table._parse_extraction_response("") is None
    assert audit_temp_table._parse_extraction_response("[1, 2, 3]") is None


def test_csv_response_with_fenced_json_creates_temp_table(web_client, monkeypatch):
    payload = {"status": "awaiting_validation", "extracted_items": [{"origin": "SP"}]}
    fenced = f"```json\n{json.dumps(payload)}\n```"
    _fake_extraction_generate(monkeypatch, text=fenced)
    _upload(web_client, "tabela.csv", make_csv([["origem"], ["SP"]]), "text/csv")
    temp_table = web_client.get("/api/cleide-auditoria/documents/status").get_json()["temp_table"]
    assert temp_table["status"] == TEMP_TABLE_STATUS_AWAITING_VALIDATION
    assert temp_table["extracted_items"] == [{"origin": "SP"}]


def test_xlsx_response_with_fenced_json_creates_temp_table(web_client, monkeypatch):
    payload = {"status": "awaiting_validation", "extracted_items": [{"origin": "SP"}]}
    fenced = f"```\n{json.dumps(payload)}\n```"
    _fake_extraction_generate(monkeypatch, text=fenced)
    resp = _upload(
        web_client,
        "tabela.xlsx",
        make_xlsx([["origem"], ["SP"]]),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    assert resp.status_code == 200
    temp_table = web_client.get("/api/cleide-auditoria/documents/status").get_json()["temp_table"]
    assert temp_table["status"] == TEMP_TABLE_STATUS_AWAITING_VALIDATION


def test_partial_payload_with_alerts_becomes_needs_review(web_client, monkeypatch):
    payload = {
        "status": "needs_review",
        "extracted_items": [{"route": "SP-RJ"}],
        "reading_alerts": ["Leitura parcial"],
    }
    _fake_extraction_generate(monkeypatch, text=json.dumps(payload))
    _upload(web_client, "tabela.csv", make_csv([["a"], ["1"]]), "text/csv")
    temp_table = web_client.get("/api/cleide-auditoria/documents/status").get_json()["temp_table"]
    assert temp_table["status"] == TEMP_TABLE_STATUS_NEEDS_REVIEW
    assert "Leitura parcial" in temp_table["reading_alerts"]


def test_provider_timeout_persists_failed_with_reading_alert(web_client, monkeypatch):
    def _fail(*_args, **_kwargs):
        raise Exception("504 DEADLINE_EXCEEDED")

    monkeypatch.setattr(audit_temp_table, "cleiton_governed_generate_content", _fail)
    monkeypatch.setattr(audit_temp_table, "_get_client", lambda: object())
    _upload(web_client, "t.csv", make_csv([["a"], ["1"]]), "text/csv")
    temp_table = web_client.get("/api/cleide-auditoria/documents/status").get_json()["temp_table"]
    assert temp_table["status"] == TEMP_TABLE_STATUS_FAILED
    assert audit_temp_table.READING_ALERT_PROVIDER_TIMEOUT in temp_table["reading_alerts"]


def test_failed_artifact_preserves_error_reason_without_stacktrace(web_client, monkeypatch):
    _fake_extraction_generate(monkeypatch, text="Resposta sem json.")
    _upload(web_client, "t.csv", make_csv([["a"], ["1"]]), "text/csv")
    temp_table = web_client.get("/api/cleide-auditoria/documents/status").get_json()["temp_table"]
    assert temp_table["status"] == TEMP_TABLE_STATUS_FAILED
    assert temp_table["reading_alerts"]
    joined = " ".join(temp_table["reading_alerts"]).lower()
    assert "traceback" not in joined
    assert "exception" not in joined
    assert audit_temp_table.READING_ALERT_PARSER_NO_JSON in temp_table["reading_alerts"]


def _pdf_like_partial_first_payload() -> dict:
    """Referência de comportamento esperado (não hardcoded em produção)."""
    return {
        "status": "needs_review",
        "freight_values": [
            {"label": "Frete ate 30 Kg", "value": None, "unit": "R$", "notes": ""},
            {"label": "Frete ate 50 Kg", "value": None, "unit": "R$", "notes": ""},
            {"label": "Frete ate 70 Kg", "value": None, "unit": "R$", "notes": ""},
            {"label": "Frete ate 100 Kg", "value": None, "unit": "R$", "notes": ""},
        ],
        "accessorial_fees": [
            {
                "name": "Pedagio",
                "value": "R$8,61 por fracao de 100Kg",
                "unit": "R$",
                "calculation_basis": "fracao de 100Kg",
                "notes": "",
            },
            {
                "name": "GRIS",
                "value": "0,15%; 0,30% RJ minimo R$4,13",
                "unit": "%/R$",
                "calculation_basis": "valor da NF",
                "notes": "",
            },
        ],
        "weight_ranges": [
            {"label": "ate 30 Kg", "min_weight": None, "max_weight": 30, "unit": "kg", "notes": ""},
            {"label": "ate 50 Kg", "min_weight": None, "max_weight": 50, "unit": "kg", "notes": ""},
        ],
        "reading_alerts": [
            "Dados extraidos parcialmente a partir do PDF. Validacao humana obrigatoria."
        ],
        "evidence_refs": [],
    }


def test_partial_first_payload_with_accessorial_fees_becomes_needs_review(web_client, monkeypatch):
    payload = {
        "status": "needs_review",
        "freight_values": [],
        "accessorial_fees": [
            {"name": "Pedagio", "value": None, "unit": "R$", "calculation_basis": "", "notes": ""},
        ],
        "weight_ranges": [],
        "reading_alerts": [],
        "evidence_refs": [],
    }
    saved = _apply_payload(web_client, payload)
    assert saved["status"] == TEMP_TABLE_STATUS_NEEDS_REVIEW
    assert len(saved["accessorial_fees"]) == 1


def test_partial_first_payload_with_freight_values_becomes_needs_review(web_client, monkeypatch):
    payload = {
        "status": "needs_review",
        "freight_values": [
            {"label": "Frete ate 30 Kg", "value": None, "unit": "R$", "notes": ""},
        ],
        "accessorial_fees": [],
        "weight_ranges": [],
        "reading_alerts": [],
        "evidence_refs": [],
    }
    saved = _apply_payload(web_client, payload)
    assert saved["status"] == TEMP_TABLE_STATUS_NEEDS_REVIEW
    assert saved["freight_values"][0]["label"] == "Frete ate 30 Kg"


def test_partial_first_payload_with_weight_ranges_becomes_needs_review(web_client, monkeypatch):
    payload = {
        "status": "needs_review",
        "freight_values": [],
        "accessorial_fees": [],
        "weight_ranges": [
            {"label": "ate 30 Kg", "min_weight": None, "max_weight": 30, "unit": "kg", "notes": ""},
        ],
        "reading_alerts": [],
        "evidence_refs": [],
    }
    saved = _apply_payload(web_client, payload)
    assert saved["status"] == TEMP_TABLE_STATUS_NEEDS_REVIEW
    assert saved["weight_ranges"][0]["max_weight"] == 30


def test_partial_first_empty_payload_becomes_failed(web_client):
    payload = {
        "status": "failed",
        "freight_values": [],
        "accessorial_fees": [],
        "weight_ranges": [],
        "reading_alerts": [],
        "evidence_refs": [],
    }
    saved = _apply_payload(web_client, payload)
    assert saved["status"] == TEMP_TABLE_STATUS_FAILED


def test_pdf_like_detected_costs_do_not_become_failed(web_client, monkeypatch):
    payload = _pdf_like_partial_first_payload()
    _fake_extraction_generate(monkeypatch, text=json.dumps(payload))
    _upload(web_client, "tabela.csv", make_csv([["a"], ["1"]]), "text/csv")
    temp_table = web_client.get("/api/cleide-auditoria/documents/status").get_json()["temp_table"]
    assert temp_table["status"] == TEMP_TABLE_STATUS_NEEDS_REVIEW
    assert len(temp_table["freight_values"]) == 4
    assert len(temp_table["accessorial_fees"]) == 2
    assert len(temp_table["weight_ranges"]) == 2


def test_partial_first_normalizes_into_temp_table_contract(web_client):
    payload = {
        "status": "needs_review",
        "freight_values": [{"label": "Frete SP-RJ", "value": "120.00", "unit": "R$", "notes": ""}],
        "accessorial_fees": [],
        "weight_ranges": [],
        "reading_alerts": ["Leitura parcial"],
        "evidence_refs": ["pag. 2"],
    }
    normalized = normalize_partial_first_extraction_to_temp_table(payload)
    assert normalized["status"] == TEMP_TABLE_STATUS_NEEDS_REVIEW
    assert normalized["detected_carrier"] is None
    assert normalized["origins"] == []
    assert normalized["destinations"] == []
    assert normalized["routes"] == []
    assert normalized["extracted_items"] == []
    assert normalized["uncertain_fields"] == []
    assert normalized["charge_type_detected"] is None
    assert len(normalized["freight_values"]) == 1

    saved = _apply_payload(web_client, payload)
    assert saved["origins"] == []
    assert saved["routes"] == []
    assert saved["extracted_items"] == []


def test_failed_reserved_for_no_useful_data(web_client):
    payload_failed_status_with_data = {
        "status": "failed",
        "freight_values": [
            {"label": "Frete ate 30 Kg", "value": None, "unit": "R$", "notes": ""},
        ],
        "accessorial_fees": [],
        "weight_ranges": [],
        "reading_alerts": [],
        "evidence_refs": [],
    }
    saved = _apply_payload(web_client, payload_failed_status_with_data)
    assert saved["status"] == TEMP_TABLE_STATUS_NEEDS_REVIEW

    payload_needs_review_empty = {
        "status": "needs_review",
        "freight_values": [],
        "accessorial_fees": [],
        "weight_ranges": [],
        "reading_alerts": [],
        "evidence_refs": [],
    }
    saved_empty = _apply_payload(web_client, payload_needs_review_empty)
    assert saved_empty["status"] == TEMP_TABLE_STATUS_FAILED


def test_chat_still_does_not_create_temp_table(web_client, monkeypatch):
    payload = {
        "status": "needs_review",
        "freight_values": [{"label": "Frete", "value": None, "unit": "R$", "notes": ""}],
        "accessorial_fees": [],
        "weight_ranges": [],
        "reading_alerts": [],
        "evidence_refs": [],
    }
    _fake_extraction_generate(monkeypatch, text=json.dumps(payload))
    _upload(web_client, "tabela.csv", make_csv([["a", "b"], ["1", "2"]]), "text/csv")
    before = web_client.get("/api/cleide-auditoria/documents/status").get_json()["temp_table"]
    captured = _fake_chat_generate(monkeypatch, text="Resposta conversacional sem bloco técnico.")
    resp = _chat(web_client, request_id="chat-no-temp-partial-first")
    assert resp.status_code == 200
    assert TEMP_TABLE_JSON_BEGIN not in resp.get_json()["answer"]
    assert "custos de frete" not in str(captured.get("contents", "")).lower()
    after = web_client.get("/api/cleide-auditoria/documents/status").get_json()["temp_table"]
    assert after["temp_table_id"] == before["temp_table_id"]
    assert after["status"] == before["status"]


def test_temp_table_prompt_uses_partial_first_contract():
    prompt = build_cleide_audit_temp_table_technical_prompt()
    assert "custos de frete" in prompt.lower()
    assert "freight_tables" in prompt
    assert "freight_routes" in prompt
    assert "freight_values" in prompt
    assert "accessorial_fees" in prompt
    assert "weight_ranges" in prompt
    assert "detected_carrier" not in prompt
    assert "origins" not in prompt
    assert "extracted_items" not in prompt
    assert "awaiting_validation" not in prompt
    assert "needs_review" in prompt
    assert "failed" in prompt
    assert "nao use markdown" in prompt.lower()


def test_existing_parser_tolerance_is_preserved():
    payload = {
        "status": "needs_review",
        "freight_values": [{"label": "Frete", "value": None, "unit": "R$", "notes": ""}],
        "accessorial_fees": [],
        "weight_ranges": [],
        "reading_alerts": [],
        "evidence_refs": [],
    }
    parsed_plain = audit_temp_table._parse_extraction_response(json.dumps(payload))
    assert parsed_plain == payload

    fenced = f"```json\n{json.dumps(payload, indent=2)}\n```"
    parsed_fence = audit_temp_table._parse_extraction_response(fenced)
    assert parsed_fence == payload

    raw_surrounded = (
        f'Análise parcial: {json.dumps(payload)} Fim.'
    )
    parsed_surrounded = audit_temp_table._parse_extraction_response(raw_surrounded)
    assert parsed_surrounded["status"] == "needs_review"
    assert parsed_surrounded["freight_values"][0]["label"] == "Frete"

    assert audit_temp_table._parse_extraction_response("Sem json.") is None


def test_temp_table_extraction_timeout_defaults_to_60000(monkeypatch):
    monkeypatch.delenv("CLEIDE_AUDIT_TEMP_TABLE_TIMEOUT_MS", raising=False)
    monkeypatch.delenv("GEMINI_HTTP_TIMEOUT_MS", raising=False)
    assert audit_temp_table.get_extraction_timeout_ms() == 60_000


def test_temp_table_extraction_respects_explicit_timeout_env(monkeypatch):
    monkeypatch.setenv("CLEIDE_AUDIT_TEMP_TABLE_TIMEOUT_MS", "90000")
    monkeypatch.setenv("GEMINI_HTTP_TIMEOUT_MS", "20000")
    assert audit_temp_table.get_extraction_timeout_ms() == 90_000


def test_temp_table_extraction_uses_specific_timeout_not_global_gemini_timeout(monkeypatch):
    monkeypatch.delenv("CLEIDE_AUDIT_TEMP_TABLE_TIMEOUT_MS", raising=False)
    monkeypatch.setenv("GEMINI_HTTP_TIMEOUT_MS", "20000")
    assert audit_temp_table.get_extraction_timeout_ms() == 60_000

    captured: dict = {}

    import google.genai as genai_module

    def fake_http_options(timeout):
        captured["timeout_ms"] = timeout
        return SimpleNamespace(timeout=timeout)

    def fake_client(api_key, http_options):
        captured["api_key"] = api_key
        captured["http_options"] = http_options
        return object()

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
    monkeypatch.setattr(genai_module.types, "HttpOptions", fake_http_options)
    monkeypatch.setattr(genai_module, "Client", fake_client)

    client = audit_temp_table._get_client()
    assert client is not None
    assert captured["timeout_ms"] == 60_000


def test_temp_table_provider_timeout_persists_effective_timeout_alert(web_client, monkeypatch):
    monkeypatch.delenv("CLEIDE_AUDIT_TEMP_TABLE_TIMEOUT_MS", raising=False)
    monkeypatch.setenv("GEMINI_HTTP_TIMEOUT_MS", "20000")

    def _fail(*_args, **_kwargs):
        raise Exception("504 DEADLINE_EXCEEDED")

    monkeypatch.setattr(audit_temp_table, "cleiton_governed_generate_content", _fail)
    monkeypatch.setattr(audit_temp_table, "_get_client", lambda: object())
    monkeypatch.setattr(
        audit_temp_table,
        "_get_model_candidates",
        lambda: ["gemini-2.5-flash"],
    )
    _upload(web_client, "t.csv", make_csv([["a"], ["1"]]), "text/csv")
    temp_table = web_client.get("/api/cleide-auditoria/documents/status").get_json()["temp_table"]
    assert temp_table["status"] == TEMP_TABLE_STATUS_FAILED
    assert audit_temp_table.READING_ALERT_PROVIDER_TIMEOUT in temp_table["reading_alerts"]
    joined = " ".join(temp_table["reading_alerts"])
    assert "timeout efetivo: 60000 ms" in joined.lower()
    assert "gemini-2.5-flash" in joined.lower()


def test_temp_table_double_model_timeout_records_failed_with_reason(web_client, monkeypatch):
    monkeypatch.delenv("CLEIDE_AUDIT_TEMP_TABLE_TIMEOUT_MS", raising=False)
    monkeypatch.setenv("GEMINI_HTTP_TIMEOUT_MS", "20000")

    def _fail(*_args, **_kwargs):
        raise Exception("504 DEADLINE_EXCEEDED")

    monkeypatch.setattr(audit_temp_table, "cleiton_governed_generate_content", _fail)
    monkeypatch.setattr(audit_temp_table, "_get_client", lambda: object())
    monkeypatch.setattr(
        audit_temp_table,
        "_get_model_candidates",
        lambda: ["gemini-2.5-flash", "gemini-2.5-flash-lite"],
    )
    _upload(web_client, "t.csv", make_csv([["a"], ["1"]]), "text/csv")
    temp_table = web_client.get("/api/cleide-auditoria/documents/status").get_json()["temp_table"]
    assert temp_table["status"] == TEMP_TABLE_STATUS_FAILED
    joined = " ".join(temp_table["reading_alerts"]).lower()
    assert audit_temp_table.READING_ALERT_PROVIDER_TIMEOUT.lower() in joined
    assert "gemini-2.5-flash" in joined
    assert "gemini-2.5-flash-lite" in joined
    assert "timeout efetivo: 60000 ms" in joined
    assert "traceback" not in joined
    assert "exception" not in joined


def test_partial_first_payload_still_becomes_needs_review(web_client, monkeypatch):
    payload = {
        "status": "needs_review",
        "freight_values": [
            {"label": "Frete ate 30 Kg", "value": None, "unit": "R$", "notes": ""},
        ],
        "accessorial_fees": [],
        "weight_ranges": [],
        "reading_alerts": [],
        "evidence_refs": [],
    }
    monkeypatch.setenv("GEMINI_HTTP_TIMEOUT_MS", "20000")
    _fake_extraction_generate(monkeypatch, text=json.dumps(payload))
    _upload(web_client, "t.csv", make_csv([["a"], ["1"]]), "text/csv")
    temp_table = web_client.get("/api/cleide-auditoria/documents/status").get_json()["temp_table"]
    assert temp_table["status"] == TEMP_TABLE_STATUS_NEEDS_REVIEW
    assert temp_table["freight_values"][0]["label"] == "Frete ate 30 Kg"


def _sample_freight_routes_payload(**overrides) -> dict:
    payload = {
        "status": "needs_review",
        "freight_routes": [
            {
                "origin": "DF",
                "destination": "JOINVILLE",
                "freight_type": "FOB",
                "weight_30": "115,00",
                "weight_50": "135,00",
                "weight_70": "168,00",
                "weight_100": "190,00",
                "boarding_fee": "190,0000",
                "freight_value_pct": "0,3000",
                "freight_weight_kg": "1,5000",
                "notes": "",
                "evidence_ref": "TABELA ALFA ATUAL.pdf (page 1)",
                "confidence": "needs_review",
            }
        ],
        "freight_values": [],
        "accessorial_fees": [],
        "weight_ranges": [],
        "reading_alerts": [],
        "evidence_refs": [],
    }
    payload.update(overrides)
    return payload


def test_freight_routes_payload_becomes_needs_review(web_client):
    saved = _apply_payload(web_client, _sample_freight_routes_payload())
    assert saved["status"] == TEMP_TABLE_STATUS_NEEDS_REVIEW
    assert len(saved["freight_routes"]) == 1
    assert saved["freight_routes"][0]["origin"] == "DF"
    assert saved["freight_routes"][0]["destination"] == "JOINVILLE"


def test_normalize_partial_first_preserves_freight_routes():
    payload = _sample_freight_routes_payload()
    normalized = normalize_partial_first_extraction_to_temp_table(payload)
    assert normalized["status"] == TEMP_TABLE_STATUS_NEEDS_REVIEW
    assert len(normalized["freight_routes"]) == 1
    assert normalized["freight_routes"][0]["freight_type"] == "FOB"
    assert normalized["freight_routes"][0]["weight_30"] == "115,00"


def test_coerce_temp_table_payload_preserves_freight_routes(web_client):
    saved = _apply_payload(web_client, _sample_freight_routes_payload())
    assert len(saved["freight_routes"]) == 1
    assert saved["freight_routes"][0]["boarding_fee"] == "190,0000"


def test_public_temp_table_exposes_freight_routes(web_client):
    saved = _apply_payload(web_client, _sample_freight_routes_payload())
    public = audit_doc_service._public_temp_table(saved)
    assert public is not None
    assert "freight_routes" in public
    assert len(public["freight_routes"]) == 1
    assert public["freight_routes"][0]["origin"] == "DF"


def test_resolve_extraction_status_considers_freight_routes_useful(web_client):
    payload = {
        "status": "failed",
        "freight_routes": [{"origin": "SP", "destination": None, "freight_type": None}],
        "freight_values": [],
        "accessorial_fees": [],
        "weight_ranges": [],
        "reading_alerts": [],
        "evidence_refs": [],
    }
    saved = _apply_payload(web_client, payload)
    assert saved["status"] == TEMP_TABLE_STATUS_NEEDS_REVIEW


def test_freight_routes_only_useful_does_not_become_failed(web_client):
    payload = {
        "status": "needs_review",
        "freight_routes": [
            {
                "origin": None,
                "destination": "RJ",
                "freight_type": None,
                "weight_30": None,
                "weight_50": None,
                "weight_70": None,
                "weight_100": None,
                "boarding_fee": None,
                "freight_value_pct": None,
                "freight_weight_kg": None,
            }
        ],
        "freight_values": [],
        "accessorial_fees": [],
        "weight_ranges": [],
        "reading_alerts": [],
        "evidence_refs": [],
    }
    saved = _apply_payload(web_client, payload)
    assert saved["status"] == TEMP_TABLE_STATUS_NEEDS_REVIEW


def test_freight_routes_missing_fields_preserved_as_none(web_client):
    payload = {
        "status": "needs_review",
        "freight_routes": [{"origin": "DF", "destination": "JOINVILLE"}],
        "freight_values": [],
        "accessorial_fees": [],
        "weight_ranges": [],
        "reading_alerts": [],
        "evidence_refs": [],
    }
    saved = _apply_payload(web_client, payload)
    route = saved["freight_routes"][0]
    assert route["origin"] == "DF"
    assert route["destination"] == "JOINVILLE"
    assert route["freight_type"] is None
    assert route["weight_30"] is None
    assert route["boarding_fee"] is None
    assert route["notes"] == ""


def test_accessorial_fees_still_works_with_freight_routes(web_client):
    payload = _sample_freight_routes_payload(
        accessorial_fees=[
            {
                "name": "Pedagio",
                "value": "R$8,61",
                "unit": "R$",
                "calculation_basis": "fracao de 100Kg",
                "notes": "",
            }
        ],
    )
    saved = _apply_payload(web_client, payload)
    assert saved["status"] == TEMP_TABLE_STATUS_NEEDS_REVIEW
    assert len(saved["freight_routes"]) == 1
    assert len(saved["accessorial_fees"]) == 1
    assert saved["accessorial_fees"][0]["name"] == "Pedagio"


def test_freight_values_fallback_still_works_without_freight_routes(web_client):
    payload = {
        "status": "needs_review",
        "freight_routes": [],
        "freight_values": [
            {"label": "Frete ate 30 Kg", "value": "120.00", "unit": "R$", "notes": ""},
        ],
        "accessorial_fees": [],
        "weight_ranges": [],
        "reading_alerts": [],
        "evidence_refs": [],
    }
    saved = _apply_payload(web_client, payload)
    assert saved["status"] == TEMP_TABLE_STATUS_NEEDS_REVIEW
    assert saved["freight_routes"] == []
    assert len(saved["freight_values"]) == 1


def test_freight_routes_normalizes_aliases():
    payload = {
        "status": "needs_review",
        "freight_routes": [
            {
                "origin": "DF",
                "destination": "JOINVILLE",
                "type": "CIF",
                "weight_30kg": "100,00",
                "taxa_embarque_kg": "1,00",
            }
        ],
        "freight_values": [],
        "accessorial_fees": [],
        "weight_ranges": [],
        "reading_alerts": [],
        "evidence_refs": [],
    }
    normalized = normalize_partial_first_extraction_to_temp_table(payload)
    route = normalized["freight_routes"][0]
    assert route["freight_type"] == "CIF"
    assert route["weight_30"] == "100,00"
    assert route["boarding_fee"] == "1,00"


def test_temp_table_prompt_includes_freight_routes_contract():
    prompt = build_cleide_audit_temp_table_technical_prompt()
    assert "freight_tables" in prompt
    assert "freight_routes" in prompt
    assert "tabelas tarifarias" in prompt.lower()
    assert "nao force colunas fixas de alfa" in prompt.lower()
    assert "nao reconstrua freight_tables a partir de freight_routes" in prompt.lower()
    assert (
        "generalidades em freight_routes" in prompt.lower()
        or "nao coloque generalidades em freight_routes" in prompt.lower()
    )
    assert (
        "servicos adicionais em freight_routes" in prompt.lower()
        or "nao coloque servicos adicionais em freight_routes" in prompt.lower()
    )


def _sample_hengst_freight_tables_payload(**overrides) -> dict:
    payload = {
        "status": "needs_review",
        "freight_tables": [
            {
                "table_title": "IAM - SP CAPITAL",
                "table_type": "weight_range_table",
                "context": {
                    "route_label": "IAM - SP CAPITAL",
                    "origin": None,
                    "destination": None,
                    "customer": None,
                    "supplier": None,
                    "valid_from": "08/04/2025",
                    "valid_to": "31/03/2026",
                    "delivery_deadline": "72h após a coleta",
                },
                "columns": [
                    "Frete Peso",
                    "Frete",
                    "Pedágio (F/100kg)",
                    "TX",
                    "Seguro",
                    "Gris",
                    "Imposto",
                ],
                "rows": [
                    {
                        "Frete Peso": "De 0 kgs à 50 Kgs",
                        "Frete": "R$ 37,80",
                        "Pedágio (F/100kg)": "R$ 2,16",
                        "TX": "R$ 12,96",
                        "Seguro": "0,20%",
                        "Gris": "0,15%",
                        "Imposto": "(+) ICMS",
                    }
                ],
                "notes": "",
                "evidence_ref": "Proposta HENGST 20252026.pdf (page 1)",
                "confidence": "needs_review",
            },
            {
                "table_title": "IAM - SP INTERIOR",
                "table_type": "weight_range_table",
                "context": {"route_label": "IAM - SP INTERIOR"},
                "columns": ["Frete Peso", "Frete", "Pedágio (F/100kg)"],
                "rows": [{"Frete Peso": "De 0 kgs à 50 Kgs", "Frete": "R$ 42,00"}],
                "notes": "",
                "evidence_ref": "Proposta HENGST 20252026.pdf (page 2)",
                "confidence": "needs_review",
            },
        ],
        "freight_routes": [],
        "freight_values": [],
        "accessorial_fees": [
            {
                "name": "Pedágio geral",
                "value": "conforme tabela",
                "unit": "",
                "calculation_basis": "",
                "notes": "",
            }
        ],
        "weight_ranges": [],
        "reading_alerts": [],
        "evidence_refs": [],
    }
    payload.update(overrides)
    return payload


def test_freight_tables_payload_becomes_needs_review(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    assert saved["status"] == TEMP_TABLE_STATUS_NEEDS_REVIEW
    assert len(saved["freight_tables"]) == 2


def test_normalize_partial_first_preserves_freight_tables():
    payload = _sample_hengst_freight_tables_payload()
    normalized = normalize_partial_first_extraction_to_temp_table(payload)
    assert normalized["status"] == TEMP_TABLE_STATUS_NEEDS_REVIEW
    assert len(normalized["freight_tables"]) == 2
    table = normalized["freight_tables"][0]
    assert table["table_title"] == "IAM - SP CAPITAL"
    assert table["table_type"] == "weight_range_table"
    assert table["context"]["valid_from"] == "08/04/2025"
    assert table["columns"] == [
        "Frete Peso",
        "Frete",
        "Pedágio (F/100kg)",
        "TX",
        "Seguro",
        "Gris",
        "Imposto",
    ]
    assert table["rows"][0]["Frete"] == "R$ 37,80"
    assert table["evidence_ref"] == "Proposta HENGST 20252026.pdf (page 1)"
    assert table["confidence"] == "needs_review"


def test_coerce_temp_table_payload_preserves_freight_tables(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    assert len(saved["freight_tables"]) == 2
    assert saved["freight_tables"][0]["columns"][0] == "Frete Peso"
    assert saved["freight_tables"][0]["rows"][0]["Gris"] == "0,15%"


def test_public_temp_table_exposes_freight_tables(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    public = audit_doc_service._public_temp_table(saved)
    assert public is not None
    assert "freight_tables" in public
    assert len(public["freight_tables"]) == 2
    assert public["freight_tables"][0]["table_title"] == "IAM - SP CAPITAL"


def test_has_useful_partial_extraction_data_considers_freight_tables(web_client):
    payload = {
        "status": "failed",
        "freight_tables": [
            {
                "table_title": None,
                "columns": [],
                "rows": [{"Frete": "R$ 10,00"}],
            }
        ],
        "freight_routes": [],
        "freight_values": [],
        "accessorial_fees": [],
        "weight_ranges": [],
        "reading_alerts": [],
        "evidence_refs": [],
    }
    saved = _apply_payload(web_client, payload)
    assert saved["status"] == TEMP_TABLE_STATUS_NEEDS_REVIEW


def test_hengst_like_payload_not_forced_to_alfa_columns(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    assert saved["freight_routes"] == []
    table = saved["freight_tables"][0]
    assert "Frete Peso" in table["columns"]
    assert "weight_30" not in table["columns"]
    assert "origin" not in table["columns"]


def test_alfa_like_payload_still_works_with_freight_routes(web_client):
    saved = _apply_payload(web_client, _sample_freight_routes_payload())
    assert saved["status"] == TEMP_TABLE_STATUS_NEEDS_REVIEW
    assert len(saved["freight_routes"]) == 1
    assert saved["freight_routes"][0]["origin"] == "DF"


def test_mark_processing_initializes_freight_tables(web_client):
    with web_client.session_transaction() as sess:
        sess[audit_doc_service.CLEIDE_AUDIT_DOC_IDS_SESSION_KEY] = ["doc-1"]
    with web_client.application.app_context():
        with web_client.application.test_request_context():
            record = mark_temp_table_processing(["doc-1"])
            assert record["freight_tables"] == []


def test_freight_table_useful_with_title_only(web_client):
    payload = {
        "status": "needs_review",
        "freight_tables": [{"table_title": "SÃO PAULO - JOINVILLE", "columns": [], "rows": []}],
        "freight_routes": [],
        "freight_values": [],
        "accessorial_fees": [],
        "weight_ranges": [],
        "reading_alerts": [],
        "evidence_refs": [],
    }
    saved = _apply_payload(web_client, payload)
    assert saved["status"] == TEMP_TABLE_STATUS_NEEDS_REVIEW
    assert saved["freight_tables"][0]["table_title"] == "SÃO PAULO - JOINVILLE"


def test_freight_table_useful_with_column_only(web_client):
    payload = {
        "status": "needs_review",
        "freight_tables": [{"table_title": None, "columns": ["Frete Vol. (R$/Pallet)"], "rows": []}],
        "freight_routes": [],
        "freight_values": [],
        "accessorial_fees": [],
        "weight_ranges": [],
        "reading_alerts": [],
        "evidence_refs": [],
    }
    saved = _apply_payload(web_client, payload)
    assert saved["status"] == TEMP_TABLE_STATUS_NEEDS_REVIEW
    assert saved["freight_tables"][0]["columns"] == ["Frete Vol. (R$/Pallet)"]


def _save_payload_for_record(record: dict, **overrides) -> dict:
    tables = record.get("freight_tables") or []
    routes = record.get("freight_routes") or []
    fees = record.get("accessorial_fees") or []
    payload = {
        "temp_table_id": record["temp_table_id"],
        "edit_target": {
            "freight_tables": tables,
            "freight_routes": routes,
            "accessorial_fees": fees,
        },
        "review_action": "save_and_advance",
    }
    payload.update(overrides)
    return payload


def _post_temp_table_save(web_client, payload: dict):
    return web_client.post(
        "/api/cleide-auditoria/temp-table/save",
        json=payload,
        content_type="application/json",
    )


def test_temp_table_save_requires_login(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    anon = SimpleNamespace(is_authenticated=False)
    monkeypatch.setattr(web, "current_user", anon)
    monkeypatch.setattr("app.cleide_audit_routes.current_user", anon)
    client = web.app.test_client()
    resp = _post_temp_table_save(client, {"temp_table_id": "x", "edit_target": {}})
    assert resp.status_code == 401
    assert resp.get_json()["error_code"] == "auth_required"


def test_temp_table_save_without_active_temp_table(web_client):
    resp = _post_temp_table_save(
        web_client,
        {
            "temp_table_id": "missing",
            "edit_target": {"freight_tables": [], "freight_routes": []},
            "review_action": "save_and_advance",
        },
    )
    assert resp.status_code == 404
    assert resp.get_json()["error_code"] == ERROR_TEMP_TABLE_NOT_FOUND


def test_temp_table_save_id_mismatch(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    resp = _post_temp_table_save(
        web_client,
        {
            "temp_table_id": "wrong-id",
            "edit_target": {"freight_tables": [], "freight_routes": []},
            "review_action": "save_and_advance",
        },
    )
    assert resp.status_code == 409
    assert resp.get_json()["error_code"] == "cleide_audit_temp_table_id_mismatch"
    assert saved["temp_table_id"] != "wrong-id"


def test_temp_table_save_preserves_expires_at(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    original_expires = saved["expires_at"]
    edited_tables = list(saved["freight_tables"])
    edited_tables[0] = dict(edited_tables[0])
    edited_tables[0]["rows"] = list(edited_tables[0]["rows"])
    edited_tables[0]["rows"][0] = dict(edited_tables[0]["rows"][0])
    edited_tables[0]["rows"][0]["Frete"] = "R$ 40,00"
    resp = _post_temp_table_save(
        web_client,
        {
            "temp_table_id": saved["temp_table_id"],
            "edit_target": {"freight_tables": edited_tables, "freight_routes": []},
            "review_action": "save_and_advance",
        },
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["temp_table"]["expires_at"] == original_expires


def test_temp_table_save_preserves_source_documents_and_id(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    resp = _post_temp_table_save(
        web_client,
        _save_payload_for_record(saved),
    )
    assert resp.status_code == 200
    public = resp.get_json()["temp_table"]
    assert public["temp_table_id"] == saved["temp_table_id"]
    assert public["source_documents"] == saved["source_documents"]


def test_temp_table_save_updates_review_metadata(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    resp = _post_temp_table_save(web_client, _save_payload_for_record(saved))
    assert resp.status_code == 200
    public = resp.get_json()["temp_table"]
    assert public["human_review_status"] == HUMAN_REVIEW_STATUS_EDITED
    assert public["human_edited_at"]
    assert public["human_edited_by_user_id"] == 42
    assert public["updated_at"]
    assert public.get("edit_version") == 1


def test_temp_table_save_review_only_marks_reviewed(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    resp = _post_temp_table_save(
        web_client,
        {
            "temp_table_id": saved["temp_table_id"],
            "edit_target": {"freight_tables": [], "freight_routes": []},
            "review_action": "save_and_advance",
        },
    )
    assert resp.status_code == 200
    public = resp.get_json()["temp_table"]
    assert public["human_review_status"] == HUMAN_REVIEW_STATUS_REVIEWED


def test_temp_table_save_does_not_call_gemini(web_client, monkeypatch):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    gemini_mock = MagicMock()
    monkeypatch.setattr(audit_temp_table, "cleiton_governed_generate_content", gemini_mock)
    resp = _post_temp_table_save(web_client, _save_payload_for_record(saved))
    assert resp.status_code == 200
    gemini_mock.assert_not_called()


def test_temp_table_save_does_not_create_new_artifact(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    before_id = saved["temp_table_id"]
    resp = _post_temp_table_save(web_client, _save_payload_for_record(saved))
    assert resp.status_code == 200
    assert resp.get_json()["temp_table"]["temp_table_id"] == before_id


def test_temp_table_save_rejects_invalid_payload(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    resp = _post_temp_table_save(
        web_client,
        {
            "temp_table_id": saved["temp_table_id"],
            "edit_target": {
                "freight_tables": [{"columns": [""], "rows": [{"": "x"}]}],
                "freight_routes": [],
            },
            "review_action": "save_and_advance",
        },
    )
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == ERROR_TEMP_TABLE_INVALID_PAYLOAD


def test_temp_table_save_rejects_empty_main_table(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    resp = _post_temp_table_save(
        web_client,
        {
            "temp_table_id": saved["temp_table_id"],
            "edit_target": {
                "freight_tables": [{"columns": [], "rows": []}],
                "freight_routes": [],
            },
            "review_action": "save_and_advance",
        },
    )
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == ERROR_TEMP_TABLE_INVALID_PAYLOAD


def test_temp_table_save_rejects_oversized_payload(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    huge = "x" * (TEMP_TABLE_SAVE_MAX_PAYLOAD_BYTES + 1024)
    edited_tables = list(saved["freight_tables"])
    edited_tables[0] = dict(edited_tables[0])
    edited_tables[0]["rows"] = [{"Frete Peso": huge, "Frete": "1", "Pedágio (F/100kg)": "1", "TX": "1", "Seguro": "1", "Gris": "1", "Imposto": "1"}]
    resp = _post_temp_table_save(
        web_client,
        {
            "temp_table_id": saved["temp_table_id"],
            "edit_target": {"freight_tables": edited_tables, "freight_routes": []},
            "review_action": "save_and_advance",
        },
    )
    assert resp.status_code == 413


def test_temp_table_save_persists_only_in_temp_artifact(web_client, tmp_path):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    edited_tables = list(saved["freight_tables"])
    edited_tables[0] = dict(edited_tables[0])
    edited_tables[0]["rows"] = list(edited_tables[0]["rows"])
    edited_tables[0]["rows"][0] = dict(edited_tables[0]["rows"][0])
    edited_tables[0]["rows"][0]["Frete"] = "R$ 99,99"
    resp = _post_temp_table_save(
        web_client,
        {
            "temp_table_id": saved["temp_table_id"],
            "edit_target": {"freight_tables": edited_tables, "freight_routes": []},
            "review_action": "save_and_advance",
        },
    )
    assert resp.status_code == 200
    path = audit_doc_service._temp_table_path(saved["temp_table_id"])
    assert path.is_file()
    with open(path, "r", encoding="utf-8") as handle:
        stored = json.load(handle)
    assert stored["freight_tables"][0]["rows"][0]["Frete"] == "R$ 99,99"
    assert stored["accessorial_fees"] == saved["accessorial_fees"]


def test_temp_table_save_persists_accessorial_fees_edits(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    edited_fees = list(saved["accessorial_fees"])
    edited_fees[0] = dict(edited_fees[0])
    edited_fees[0]["notes"] = "ajuste manual"
    edited_fees[0]["scope"] = "general"
    resp = _post_temp_table_save(
        web_client,
        {
            "temp_table_id": saved["temp_table_id"],
            "edit_target": {
                "freight_tables": [],
                "freight_routes": [],
                "accessorial_fees": edited_fees,
            },
            "review_action": "save_and_advance",
        },
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["temp_table"]["accessorial_fees"][0]["notes"] == "ajuste manual"
    assert body["temp_table"]["accessorial_fees"][0]["scope"] == "general"


def test_clear_documents_removes_edited_temp_table(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    resp = _post_temp_table_save(web_client, _save_payload_for_record(saved))
    assert resp.status_code == 200
    with web_client.session_transaction() as sess:
        sess[audit_doc_service.CLEIDE_AUDIT_DOC_IDS_SESSION_KEY] = ["doc-1"]
    web_client.post("/api/cleide-auditoria/documents/clear")
    assert web_client.get("/api/cleide-auditoria/documents/status").get_json()["temp_table"] is None


def test_temp_table_save_expired_record(web_client, monkeypatch, tmp_path):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    path = audit_doc_service._temp_table_path(saved["temp_table_id"])
    with open(path, "r", encoding="utf-8") as handle:
        record = json.load(handle)
    record["status"] = audit_doc_service.TEMP_TABLE_STATUS_NEEDS_REVIEW
    record["expires_at"] = (datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)).isoformat()
    audit_doc_service._write_temp_table_atomic(path, record)
    resp = _post_temp_table_save(web_client, _save_payload_for_record(saved))
    assert resp.status_code == 404
    assert resp.get_json()["error_code"] == audit_doc_service.ERROR_TEMP_TABLE_EXPIRED


def _apply_extraction_with_client_session(web_client, payload, source_doc_ids=None):
    source_doc_ids = source_doc_ids or ["doc-1"]
    session_keys = (
        audit_doc_service.CLEIDE_AUDIT_TEMP_TABLE_ID_SESSION_KEY,
        audit_doc_service.CLEIDE_AUDIT_TEMP_TABLE_SOURCE_DOCS_SESSION_KEY,
        audit_doc_service.CLEIDE_AUDIT_DOC_IDS_SESSION_KEY,
    )
    with web_client.session_transaction() as sess:
        snapshot = {key: sess.get(key) for key in session_keys}
    with web_client.application.app_context():
        with web_client.application.test_request_context():
            from flask import session as flask_session

            for key, value in snapshot.items():
                if value is not None:
                    flask_session[key] = value
            return apply_temp_table_extraction_from_model_payload(
                payload,
                source_doc_ids=source_doc_ids,
            )


def test_temp_table_save_removes_accessorial_fees_persists_in_artifact(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    resp = _post_temp_table_save(
        web_client,
        {
            "temp_table_id": saved["temp_table_id"],
            "edit_target": {
                "freight_tables": saved["freight_tables"],
                "freight_routes": [],
                "accessorial_fees": [],
            },
            "review_action": "save_and_advance",
        },
    )
    assert resp.status_code == 200
    path = audit_doc_service._temp_table_path(saved["temp_table_id"])
    with open(path, "r", encoding="utf-8") as handle:
        stored = json.load(handle)
    assert stored["accessorial_fees"] == []


def test_temp_table_status_after_save_returns_reduced_accessorial_fees(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    save_resp = _post_temp_table_save(
        web_client,
        {
            "temp_table_id": saved["temp_table_id"],
            "edit_target": {
                "freight_tables": saved["freight_tables"],
                "freight_routes": [],
                "accessorial_fees": [],
            },
            "review_action": "save_and_advance",
        },
    )
    assert save_resp.status_code == 200
    public = save_resp.get_json()["temp_table"]
    assert public["accessorial_fees"] == []
    assert public["human_review_status"] == HUMAN_REVIEW_STATUS_EDITED
    assert public.get("edit_version") == 1


def test_extraction_does_not_overwrite_human_reviewed_record(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    save_resp = _post_temp_table_save(
        web_client,
        {
            "temp_table_id": saved["temp_table_id"],
            "edit_target": {
                "freight_tables": saved["freight_tables"],
                "freight_routes": [],
                "accessorial_fees": [],
            },
            "review_action": "save_and_advance",
        },
    )
    assert save_resp.status_code == 200
    saved_public = save_resp.get_json()["temp_table"]

    model_payload = _sample_hengst_freight_tables_payload()
    model_payload["accessorial_fees"].append(
        {
            "name": "Taxa reintroduzida",
            "value": "R$ 10,00",
            "unit": "R$",
            "calculation_basis": "",
            "notes": "",
        }
    )
    result = _apply_extraction_with_client_session(web_client, model_payload)
    assert result["human_review_status"] == saved_public["human_review_status"]
    assert result["human_edited_at"] == saved_public["human_edited_at"]
    assert result["human_edited_by_user_id"] == saved_public["human_edited_by_user_id"]
    assert result["edit_version"] == saved_public["edit_version"]
    assert result["accessorial_fees"] == []


def test_extraction_does_not_reintroduce_removed_accessorial_fees(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    _post_temp_table_save(
        web_client,
        {
            "temp_table_id": saved["temp_table_id"],
            "edit_target": {
                "freight_tables": saved["freight_tables"],
                "freight_routes": [],
                "accessorial_fees": [],
            },
            "review_action": "save_and_advance",
        },
    )
    model_payload = _sample_hengst_freight_tables_payload()
    assert len(model_payload["accessorial_fees"]) >= 1
    result = _apply_extraction_with_client_session(web_client, model_payload)
    assert result["accessorial_fees"] == []


def test_extraction_initial_without_human_review_still_works(web_client):
    payload = _sample_hengst_freight_tables_payload()
    saved = _apply_payload(web_client, payload)
    assert saved["status"] == TEMP_TABLE_STATUS_NEEDS_REVIEW
    assert len(saved["freight_tables"]) == 2
    assert len(saved["accessorial_fees"]) == 1
    assert saved.get("human_review_status") is None


def test_extraction_allowed_when_source_documents_change(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    _post_temp_table_save(
        web_client,
        {
            "temp_table_id": saved["temp_table_id"],
            "edit_target": {
                "freight_tables": saved["freight_tables"],
                "freight_routes": [],
                "accessorial_fees": [],
            },
            "review_action": "save_and_advance",
        },
    )
    model_payload = _sample_hengst_freight_tables_payload()
    result = _apply_extraction_with_client_session(
        web_client,
        model_payload,
        source_doc_ids=["doc-1", "doc-2"],
    )
    assert result["source_documents"] == ["doc-1", "doc-2"]
    assert len(result["accessorial_fees"]) == 1


def test_extraction_skip_preserves_expires_at(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    original_expires = saved["expires_at"]
    _post_temp_table_save(
        web_client,
        {
            "temp_table_id": saved["temp_table_id"],
            "edit_target": {
                "freight_tables": saved["freight_tables"],
                "freight_routes": [],
                "accessorial_fees": [],
            },
            "review_action": "save_and_advance",
        },
    )
    result = _apply_extraction_with_client_session(
        web_client,
        _sample_hengst_freight_tables_payload(),
    )
    assert result["expires_at"] == original_expires


def test_extraction_force_overwrite_bypasses_human_review_guard(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    _post_temp_table_save(
        web_client,
        {
            "temp_table_id": saved["temp_table_id"],
            "edit_target": {
                "freight_tables": saved["freight_tables"],
                "freight_routes": [],
                "accessorial_fees": [],
            },
            "review_action": "save_and_advance",
        },
    )
    with web_client.session_transaction() as sess:
        snapshot = {
            audit_doc_service.CLEIDE_AUDIT_TEMP_TABLE_ID_SESSION_KEY: sess.get(
                audit_doc_service.CLEIDE_AUDIT_TEMP_TABLE_ID_SESSION_KEY
            ),
        }
    with web_client.application.app_context():
        with web_client.application.test_request_context():
            from flask import session as flask_session

            flask_session[audit_doc_service.CLEIDE_AUDIT_TEMP_TABLE_ID_SESSION_KEY] = snapshot[
                audit_doc_service.CLEIDE_AUDIT_TEMP_TABLE_ID_SESSION_KEY
            ]
            result = apply_temp_table_extraction_from_model_payload(
                _sample_hengst_freight_tables_payload(),
                source_doc_ids=["doc-1"],
                force_overwrite=True,
            )
    assert len(result["accessorial_fees"]) == 1
    assert result.get("human_review_status") is None


def _sample_coverage_csv() -> bytes:
    return make_csv(
        [
            ["uf_destino", "cidade_destino", "regiao_frete"],
            ["SP", "Campinas", "SP-Interior 1"],
            ["AM", "Manaus", "AM-Capital"],
        ]
    )


def _sample_coverage_xlsx() -> bytes:
    return make_xlsx(
        [
            ["UF destino", "Cidade destino", "Região de frete"],
            ["SP", "Sorocaba", "SP-Interior 2"],
        ]
    )


def _post_coverage_upload(web_client, filename: str, content: bytes, mime: str):
    return web_client.post(
        "/api/cleide-auditoria/coverage/upload",
        data={"file": (io.BytesIO(content), filename, mime)},
        content_type="multipart/form-data",
    )


def test_coverage_upload_requires_login(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    anon = SimpleNamespace(is_authenticated=False)
    monkeypatch.setattr(web, "current_user", anon)
    monkeypatch.setattr("app.cleide_audit_routes.current_user", anon)
    client = web.app.test_client()
    resp = _post_coverage_upload(client, "cidades.csv", _sample_coverage_csv(), "text/csv")
    assert resp.status_code == 401
    assert resp.get_json()["error_code"] == "auth_required"


def test_coverage_upload_rejects_pdf(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    resp = _post_coverage_upload(
        web_client,
        "cidades.pdf",
        make_minimal_pdf(),
        "application/pdf",
    )
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "cleide_audit_coverage_invalid_format"
    assert saved["freight_tables"]


def test_coverage_upload_accepts_csv(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    resp = _post_coverage_upload(web_client, "cidades.csv", _sample_coverage_csv(), "text/csv")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    coverage = body["temp_table"]["coverage_table"]
    assert coverage["rows"][0]["destination_city"] == "Campinas"
    assert body["temp_table"]["freight_tables"] == saved["freight_tables"]


def test_coverage_upload_accepts_xlsx(web_client):
    _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    resp = _post_coverage_upload(
        web_client,
        "cidades.xlsx",
        _sample_coverage_xlsx(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    assert resp.status_code == 200
    rows = resp.get_json()["temp_table"]["coverage_table"]["rows"]
    assert rows[0]["destination_city"] == "Sorocaba"


def test_coverage_upload_creates_coverage_table(web_client):
    _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    resp = _post_coverage_upload(web_client, "cidades.csv", _sample_coverage_csv(), "text/csv")
    coverage = resp.get_json()["temp_table"]["coverage_table"]
    assert coverage["status"] == TEMP_TABLE_STATUS_NEEDS_REVIEW
    assert coverage["columns"] == ["UF destino", "Cidade destino", "Região de frete"]
    assert len(coverage["rows"]) == 2


def test_coverage_upload_does_not_alter_freight_tables(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    before = saved["freight_tables"]
    resp = _post_coverage_upload(web_client, "cidades.csv", _sample_coverage_csv(), "text/csv")
    assert resp.get_json()["temp_table"]["freight_tables"] == before


def test_coverage_upload_does_not_alter_freight_routes(web_client):
    payload = _sample_hengst_freight_tables_payload()
    payload["freight_routes"] = [
        {
            "origin": "SP",
            "destination": "RJ",
            "freight_type": "Rodo",
            "weight_30": "10",
        }
    ]
    saved = _apply_payload(web_client, payload)
    resp = _post_coverage_upload(web_client, "cidades.csv", _sample_coverage_csv(), "text/csv")
    assert resp.get_json()["temp_table"]["freight_routes"] == saved["freight_routes"]


def test_coverage_upload_does_not_alter_accessorial_fees(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    resp = _post_coverage_upload(web_client, "cidades.csv", _sample_coverage_csv(), "text/csv")
    assert resp.get_json()["temp_table"]["accessorial_fees"] == saved["accessorial_fees"]


def test_coverage_upload_preserves_expires_at(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    original_expires = saved["expires_at"]
    resp = _post_coverage_upload(web_client, "cidades.csv", _sample_coverage_csv(), "text/csv")
    assert resp.get_json()["temp_table"]["expires_at"] == original_expires


def test_coverage_upload_does_not_call_trigger_extraction(web_client, monkeypatch):
    _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    trigger_mock = MagicMock()
    monkeypatch.setattr("app.cleide_audit_routes.trigger_temp_table_extraction_for_session", trigger_mock)
    resp = _post_coverage_upload(web_client, "cidades.csv", _sample_coverage_csv(), "text/csv")
    assert resp.status_code == 200
    trigger_mock.assert_not_called()


def test_coverage_upload_does_not_call_gemini(web_client, monkeypatch):
    _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    gemini_mock = MagicMock()
    monkeypatch.setattr(audit_temp_table, "cleiton_governed_generate_content", gemini_mock)
    resp = _post_coverage_upload(web_client, "cidades.csv", _sample_coverage_csv(), "text/csv")
    assert resp.status_code == 200
    gemini_mock.assert_not_called()


def test_coverage_save_preserves_ttl(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    upload_resp = _post_coverage_upload(web_client, "cidades.csv", _sample_coverage_csv(), "text/csv")
    original_expires = upload_resp.get_json()["temp_table"]["expires_at"]
    rows = upload_resp.get_json()["temp_table"]["coverage_table"]["rows"]
    rows[0] = dict(rows[0])
    rows[0]["destination_city"] = "Campinas Alterada"
    resp = _post_temp_table_save(
        web_client,
        {
            "temp_table_id": saved["temp_table_id"],
            "edit_target": {"coverage_table": {"rows": rows}},
            "review_action": "save_and_advance",
        },
    )
    assert resp.status_code == 200
    assert resp.get_json()["temp_table"]["expires_at"] == original_expires


def test_coverage_save_updates_edit_version(web_client):
    _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    upload_resp = _post_coverage_upload(web_client, "cidades.csv", _sample_coverage_csv(), "text/csv")
    rows = upload_resp.get_json()["temp_table"]["coverage_table"]["rows"]
    resp = _post_temp_table_save(
        web_client,
        {
            "temp_table_id": upload_resp.get_json()["temp_table"]["temp_table_id"],
            "edit_target": {"coverage_table": {"rows": rows}},
            "review_action": "save_and_advance",
        },
    )
    coverage = resp.get_json()["temp_table"]["coverage_table"]
    assert coverage["edit_version"] == 1


def test_coverage_save_marks_human_review_status(web_client):
    _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    upload_resp = _post_coverage_upload(web_client, "cidades.csv", _sample_coverage_csv(), "text/csv")
    rows = upload_resp.get_json()["temp_table"]["coverage_table"]["rows"]
    resp = _post_temp_table_save(
        web_client,
        {
            "temp_table_id": upload_resp.get_json()["temp_table"]["temp_table_id"],
            "edit_target": {"coverage_table": {"rows": rows}},
            "review_action": "save_and_advance",
        },
    )
    coverage = resp.get_json()["temp_table"]["coverage_table"]
    assert coverage["human_review_status"] == HUMAN_REVIEW_STATUS_EDITED


def test_coverage_save_does_not_alter_freight(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    upload_resp = _post_coverage_upload(web_client, "cidades.csv", _sample_coverage_csv(), "text/csv")
    rows = upload_resp.get_json()["temp_table"]["coverage_table"]["rows"]
    resp = _post_temp_table_save(
        web_client,
        {
            "temp_table_id": saved["temp_table_id"],
            "edit_target": {"coverage_table": {"rows": rows}},
            "review_action": "save_and_advance",
        },
    )
    public = resp.get_json()["temp_table"]
    assert public["freight_tables"] == saved["freight_tables"]
    assert public["freight_routes"] == saved["freight_routes"]
    assert public["accessorial_fees"] == saved["accessorial_fees"]


def test_clear_documents_removes_coverage(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    _post_coverage_upload(web_client, "cidades.csv", _sample_coverage_csv(), "text/csv")
    clear_resp = web_client.post("/api/cleide-auditoria/documents/clear")
    assert clear_resp.status_code == 200
    status = web_client.get("/api/cleide-auditoria/documents/status").get_json()
    assert status.get("temp_table") is None


def test_absence_of_coverage_does_not_block_status(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    save_resp = _post_temp_table_save(web_client, _save_payload_for_record(saved))
    assert save_resp.status_code == 200
    body = save_resp.get_json()
    assert body["ok"] is True
    assert body["temp_table"] is not None
    assert "coverage_table" not in body["temp_table"]


def test_extraction_preserves_existing_coverage(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    upload_resp = _post_coverage_upload(web_client, "cidades.csv", _sample_coverage_csv(), "text/csv")
    coverage_before = upload_resp.get_json()["temp_table"]["coverage_table"]
    result = _apply_extraction_with_client_session(
        web_client,
        _sample_hengst_freight_tables_payload(),
    )
    assert result["coverage_table"]["rows"] == coverage_before["rows"]


def _coverage_tabular_rows(headers: list[str], data_rows: list[list[str]]) -> list[list[str]]:
    return [headers, *data_rows]


def _assert_coverage_row(row: dict, *, uf: str, city: str, region: str) -> None:
    assert row["destination_uf"] == uf
    assert row["destination_city"] == city
    assert row["freight_region"] == region


def test_coverage_parser_accepts_praca_uf_cidade_destino_real_case():
    rows, warnings = _parse_coverage_tabular_rows(
        _coverage_tabular_rows(
            ["PRAÇA", "UF ", "CIDADE DESTINO"],
            [["SP-Interior 1", "SP", "Campinas"], ["AM-Capital", "AM", "Manaus"]],
        ),
        source_file_name="teste cidade.xlsx",
    )
    assert not warnings
    assert len(rows) == 2
    _assert_coverage_row(rows[0], uf="SP", city="Campinas", region="SP-Interior 1")
    assert _resolve_coverage_field("PRAÇA") == "freight_region"
    assert _resolve_coverage_field("UF ") == "destination_uf"
    assert _resolve_coverage_field("CIDADE DESTINO") == "destination_city"


@pytest.mark.parametrize(
    ("headers", "data"),
    [
        (["Região", "Estado", "Município"], [["Sul", "RS", "Porto Alegre"]]),
        (["Rota", "UF destino", "Cidade destino"], [["SP-Metropolitana", "SP", "São Paulo"]]),
        (["Itinerário", "Estado destino", "Cidade"], [["TO - Interior", "TO", "Palmas"]]),
        (["Código região", "Unidade Federativa", "Localidade"], [["Norte", "PA", "Belém"]]),
        (["Praça", "UF de entrega", "Cidade de entrega"], [["AM-Fluvias", "AM", "Manaus"]]),
    ],
)
def test_coverage_parser_accepts_logistics_header_variations(headers, data):
    rows, _ = _parse_coverage_tabular_rows(
        _coverage_tabular_rows(headers, data),
        source_file_name="variacoes.csv",
    )
    assert len(rows) == 1
    assert rows[0]["destination_uf"] == data[0][1]
    assert rows[0]["destination_city"] == data[0][2]
    assert rows[0]["freight_region"] == data[0][0]


@pytest.mark.parametrize(
    ("headers", "data"),
    [
        (["Regiao Fret", "UF", "Cidade"], [["SP Interior 1", "SP", "Campinas"]]),
        (["Praca", "Estado", "Municipo"], [["SP-Interior 1", "SP", "Campinas"]]),
        (["Itinerarioo", "UF Destno", "Cidade Detino"], [["Norte", "SP", "Campinas"]]),
    ],
)
def test_coverage_parser_accepts_common_header_typos(headers, data):
    rows, _ = _parse_coverage_tabular_rows(
        _coverage_tabular_rows(headers, data),
        source_file_name="typos.csv",
    )
    assert len(rows) == 1
    assert rows[0]["destination_uf"] == "SP"
    assert rows[0]["destination_city"] == "Campinas"


@pytest.mark.parametrize(
    "headers",
    [
        ["Região", "Cidade"],
        ["Praça", "UF"],
        ["Rota", "Itinerário"],
    ],
)
def test_coverage_parser_rejects_missing_required_column(headers):
    with pytest.raises(CleideAuditCoverageError) as exc:
        _parse_coverage_tabular_rows(
            _coverage_tabular_rows(headers, [["A", "B", "C"][: len(headers)]]),
            source_file_name="missing.csv",
        )
    assert exc.value.error_code == ERROR_COVERAGE_PARSE_FAILED
    assert "Colunas obrigatórias ausentes" in exc.value.message


def test_coverage_parser_rejects_ambiguous_destino_headers():
    with pytest.raises(CleideAuditCoverageError) as exc:
        _parse_coverage_tabular_rows(
            _coverage_tabular_rows(
                ["Destino", "Destino 2", "Observação"],
                [["valor-a", "valor-b", "valor-c"]],
            ),
            source_file_name="ambiguo.csv",
        )
    assert exc.value.error_code == ERROR_COVERAGE_PARSE_FAILED
    assert "Colunas obrigatórias ausentes" in exc.value.message
    assert "UF destino" in exc.value.message


def test_coverage_missing_column_error_includes_hints():
    with pytest.raises(CleideAuditCoverageError) as exc:
        _parse_coverage_tabular_rows(
            _coverage_tabular_rows(["UF", "Cidade"], [["SP", "Campinas"]]),
            source_file_name="sem-regiao.csv",
        )
    message = exc.value.message
    assert "Região de frete" in message
    assert "Praça" in message or "Região" in message


def test_coverage_upload_accepts_teste_cidade_xlsx_headers(web_client):
    _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    content = make_xlsx(
        [
            ["PRAÇA", "UF ", "CIDADE DESTINO"],
            ["SP-Interior 1", "SP", "Campinas"],
            ["AM-Capital", "AM", "Manaus"],
        ]
    )
    resp = _post_coverage_upload(
        web_client,
        "teste cidade.xlsx",
        content,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    assert resp.status_code == 200
    rows = resp.get_json()["temp_table"]["coverage_table"]["rows"]
    assert len(rows) == 2
    _assert_coverage_row(rows[0], uf="SP", city="Campinas", region="SP-Interior 1")


def test_coverage_upload_accepts_praca_csv_variant(web_client):
    _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    content = make_csv(
        [
            ["PRAÇA", "UF", "CIDADE DESTINO"],
            ["SP-Interior 1", "SP", "Campinas"],
        ]
    )
    resp = _post_coverage_upload(web_client, "teste cidade.csv", content, "text/csv")
    assert resp.status_code == 200
    row = resp.get_json()["temp_table"]["coverage_table"]["rows"][0]
    _assert_coverage_row(row, uf="SP", city="Campinas", region="SP-Interior 1")


AUDIT_TEMPLATE_HEADERS = [
    "transportadora",
    "numero_documento",
    "cidade_origem",
    "uf_origem",
    "cidade_destino",
    "uf_destino",
    "valor_nf",
    "valor_frete",
    "peso",
    "modal",
    "data_emissao",
    "data_entrega",
]


def _sample_audit_row(**overrides):
    row = [
        "Transportadora X",
        "123",
        "São Paulo",
        "SP",
        "Campinas",
        "SP",
        "1000",
        "100.5",
        "48",
        "Rodo",
        "2024-01-01",
        "2024-01-05",
    ]
    if overrides:
        header_index = {name: idx for idx, name in enumerate(AUDIT_TEMPLATE_HEADERS)}
        for key, value in overrides.items():
            if key in header_index:
                row[header_index[key]] = value
    return row


def _sample_audit_xlsx(*rows, sheet_name: str = "Modelo Cleide") -> bytes:
    data_rows = rows or [_sample_audit_row()]
    return make_audit_xlsx([AUDIT_TEMPLATE_HEADERS, *data_rows], sheet_name=sheet_name)


def _sample_audit_csv(*rows) -> bytes:
    data_rows = rows or [_sample_audit_row()]
    return make_csv([AUDIT_TEMPLATE_HEADERS, *data_rows])


def _post_audit_upload(web_client, filename: str, content: bytes, mime: str):
    return web_client.post(
        "/api/cleide-auditoria/audit/upload",
        data={"file": (io.BytesIO(content), filename, mime)},
        content_type="multipart/form-data",
    )


def _post_audit_run(web_client):
    return web_client.post("/api/cleide-auditoria/audit/run", json={})


def _sample_pricing_payload() -> dict:
    return {
        "status": "needs_review",
        "freight_tables": [
            {
                "table_title": "Tabela por região",
                "table_type": "weight_range_table",
                "columns": ["Região de frete", "Até 30 kg", "31 a 50 kg", "Excedente kg"],
                "rows": [
                    {
                        "Região de frete": "SP-Interior 1",
                        "Até 30 kg": "87,13",
                        "31 a 50 kg": "100,50",
                        "Excedente kg": "2,00",
                    }
                ],
            }
        ],
        "freight_routes": [],
        "freight_values": [],
        "accessorial_fees": [],
        "weight_ranges": [],
        "reading_alerts": [],
        "evidence_refs": [],
    }


def _sample_city_pricing_payload() -> dict:
    return {
        "status": "needs_review",
        "freight_tables": [
            {
                "table_title": "Tabela por cidade",
                "table_type": "weight_range_table",
                "columns": ["UF", "UF Cidades", "151 a 200Kg", "Kg Excedente"],
                "rows": [
                    {
                        "UF": "TO",
                        "UF Cidades": "Palmas",
                        "151 a 200Kg": "360,00",
                        "Kg Excedente": "1,378340248",
                    },
                    {
                        "UF": "AC",
                        "UF Cidades": "Rio Branco",
                        "151 a 200Kg": "399,16",
                        "Kg Excedente": "1,38",
                    },
                ],
            }
        ],
        "freight_routes": [],
        "freight_values": [],
        "accessorial_fees": [],
        "weight_ranges": [],
        "reading_alerts": [],
        "evidence_refs": [],
    }


def test_build_coverage_index_resolves_uf_city():
    index = audit_doc_service.build_coverage_index(
        {"rows": [{"destination_uf": " sp ", "destination_city": " Campinas ", "freight_region": "SP-Interior 1"}]}
    )
    assert index["SP|CAMPINAS"] == "SP-Interior 1"


def test_build_coverage_index_detects_duplicate_region():
    index = audit_doc_service.build_coverage_index(
        {
            "rows": [
                {"destination_uf": "SP", "destination_city": "Campinas", "freight_region": "SP-Interior 1"},
                {"destination_uf": "SP", "destination_city": "Campinas", "freight_region": "SP-Interior 2"},
            ]
        }
    )
    assert index["SP|CAMPINAS"]["reason_code"] == "ambiguous_coverage_mapping"


def test_build_freight_pricing_index_builds_fixed_range():
    index = audit_doc_service.build_freight_pricing_index(_sample_pricing_payload())
    rule = index["SP-Interior 1"]
    assert rule["pricing_type"] == "range_plus_excess_per_kg"
    assert rule["brackets"][0]["max_kg"] == 30.0


def test_region_column_recognizes_uf_cidades():
    assert audit_doc_service._is_region_column("UF Cidades") is True


def test_build_freight_pricing_index_builds_city_destination_keys():
    index = audit_doc_service.build_freight_pricing_index(_sample_city_pricing_payload())
    assert index["Palmas"]["pricing_type"] == "range_plus_excess_per_kg"
    assert index["PALMAS"]["region"] == "Palmas"
    assert index["TO|PALMAS"]["region"] == "Palmas"


def test_find_pricing_rule_falls_back_from_coverage_region_to_city():
    pricing_index = audit_doc_service.build_freight_pricing_index(_sample_city_pricing_payload())
    rule = audit_doc_service._find_pricing_rule(
        pricing_index,
        "TO - Capital",
        "TO",
        "Palmas",
    )
    assert rule is pricing_index["TO|PALMAS"]


def test_calculate_weight_freight_fixed_range():
    rule = {
        "pricing_type": "fixed_range",
        "brackets": [
            {"min_kg": 0, "max_kg": 30, "value": 87.13, "label": "0 a 30 kg"},
            {"min_kg": 30, "max_kg": 50, "value": 100.50, "label": "31 a 50 kg"},
        ],
    }
    calculated = audit_doc_service.calculate_weight_freight(48, rule)
    assert calculated["expected_freight"] == 100.50
    assert calculated["calculation_basis"] == "fixed_range"


def test_calculate_weight_freight_range_plus_excess():
    rule = {
        "pricing_type": "range_plus_excess_per_kg",
        "brackets": [{"min_kg": 0, "max_kg": 100, "value": 200.00, "label": "Até 100 kg"}],
        "excess": {"rate_per_kg": 3.5},
    }
    assert audit_doc_service.calculate_weight_freight(103, rule)["expected_freight"] == 210.50


def test_calculate_weight_freight_direct_kg():
    rule = {"pricing_type": "direct_weight_rate", "unit": "kg", "value_per_kg": 2.5}
    assert audit_doc_service.calculate_weight_freight(10, rule)["expected_freight"] == 25.00


def test_calculate_weight_freight_direct_ton():
    rule = {"pricing_type": "direct_weight_rate", "unit": "ton", "value_per_ton": 800}
    assert audit_doc_service.calculate_weight_freight(1500, rule)["expected_freight"] == 1200.00


def test_compare_charged_vs_expected_ok():
    result = audit_doc_service.compare_charged_vs_expected(87.13, 87.13)
    assert result["status"] == "ok"
    assert result["divergence_value"] == 0


def test_compare_charged_vs_expected_divergent():
    result = audit_doc_service.compare_charged_vs_expected(87.14, 87.13)
    assert result["status"] == "divergent"
    assert result["divergence_value"] == 0.01


def test_audit_run_requires_login(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    anon = SimpleNamespace(is_authenticated=False)
    monkeypatch.setattr(web, "current_user", anon)
    monkeypatch.setattr("app.cleide_audit_routes.current_user", anon)
    client = web.app.test_client()
    resp = _post_audit_run(client)
    assert resp.status_code == 401
    assert resp.get_json()["error_code"] == "auth_required"


def test_audit_run_requires_active_temp_table(web_client):
    resp = _post_audit_run(web_client)
    assert resp.status_code == 404
    assert resp.get_json()["error_code"] == "cleide_audit_audit_no_temp_table"


def test_audit_run_requires_audit_batch(web_client):
    _apply_payload(web_client, _sample_pricing_payload())
    resp = _post_audit_run(web_client)
    assert resp.status_code == 404
    assert resp.get_json()["error_code"] == "cleide_audit_audit_batch_not_found"


def test_audit_run_records_results_summary_and_preserves_tables(web_client):
    saved = _apply_payload(web_client, _sample_pricing_payload())
    coverage = make_csv(
        [
            ["UF destino", "Cidade destino", "Região de frete"],
            ["SP", "Campinas", "SP-Interior 1"],
        ]
    )
    coverage_resp = _post_coverage_upload(web_client, "coverage.csv", coverage, "text/csv")
    assert coverage_resp.status_code == 200
    upload_resp = _post_audit_upload(
        web_client,
        "auditado.xlsx",
        _sample_audit_xlsx(
            _sample_audit_row(valor_frete="100.50", peso="48"),
            _sample_audit_row(numero_documento="124", valor_frete="99.00", peso="48"),
        ),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    assert upload_resp.status_code == 200

    resp = _post_audit_run(web_client)
    assert resp.status_code == 200
    temp_table = resp.get_json()["temp_table"]
    batch = temp_table["audit_batch"]
    assert batch["status"] == "processed"
    assert len(batch["results"]) == 2
    assert batch["results"][0]["status"] == "ok"
    assert batch["results"][1]["status"] == "divergent"
    assert batch["summary"]["total_rows"] == 2
    assert batch["summary"]["ok"] == 1
    assert batch["summary"]["divergent"] == 1
    assert temp_table["expires_at"] == saved["expires_at"]
    assert temp_table["freight_tables"] == saved["freight_tables"]
    assert temp_table["accessorial_fees"] == saved["accessorial_fees"]
    assert temp_table["coverage_table"]["rows"][0]["freight_region"] == "SP-Interior 1"


def test_audit_run_missing_coverage_mapping(web_client):
    _apply_payload(web_client, _sample_pricing_payload())
    coverage = make_csv([["UF destino", "Cidade destino", "Região de frete"], ["SP", "Sorocaba", "SP-Interior 1"]])
    assert _post_coverage_upload(web_client, "coverage.csv", coverage, "text/csv").status_code == 200
    assert _post_audit_upload(web_client, "auditado.xlsx", _sample_audit_xlsx(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet").status_code == 200
    resp = _post_audit_run(web_client)
    result = resp.get_json()["temp_table"]["audit_batch"]["results"][0]
    assert result["status"] == "missing_coverage_mapping"


def test_audit_run_invalid_weight_and_charged_freight(web_client):
    _apply_payload(web_client, _sample_pricing_payload())
    coverage = make_csv([["UF destino", "Cidade destino", "Região de frete"], ["SP", "Campinas", "SP-Interior 1"]])
    assert _post_coverage_upload(web_client, "coverage.csv", coverage, "text/csv").status_code == 200
    assert _post_audit_upload(web_client, "auditado.xlsx", _sample_audit_xlsx(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet").status_code == 200
    with web_client.session_transaction() as sess:
        active_id = sess[audit_doc_service.CLEIDE_AUDIT_TEMP_TABLE_ID_SESSION_KEY]
    record = audit_doc_service.load_temp_table_record(active_id, ttl_hours=24)
    record["audit_batch"]["normalized_rows"][0]["audited_weight"] = "x"
    record["audit_batch"]["normalized_rows"].append(
        {
            **record["audit_batch"]["normalized_rows"][0],
            "row_index": 2,
            "audited_weight": 10,
            "charged_freight": "x",
        }
    )
    audit_doc_service._write_temp_table_atomic(audit_doc_service._temp_table_path(active_id), record)
    resp = _post_audit_run(web_client)
    statuses = [row["status"] for row in resp.get_json()["temp_table"]["audit_batch"]["results"]]
    assert statuses == ["invalid_weight", "invalid_charged_freight"]


def test_audit_run_missing_freight_rule(web_client):
    _apply_payload(web_client, _sample_pricing_payload())
    coverage = make_csv([["UF destino", "Cidade destino", "Região de frete"], ["SP", "Campinas", "SP-Interior 9"]])
    assert _post_coverage_upload(web_client, "coverage.csv", coverage, "text/csv").status_code == 200
    assert _post_audit_upload(web_client, "auditado.xlsx", _sample_audit_xlsx(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet").status_code == 200
    resp = _post_audit_run(web_client)
    assert resp.get_json()["temp_table"]["audit_batch"]["results"][0]["status"] == "missing_freight_rule"


def test_audit_run_falls_back_to_city_destination_rule_when_coverage_region_has_no_rule(web_client):
    _apply_payload(web_client, _sample_city_pricing_payload())
    coverage = make_csv(
        [
            ["UF destino", "Cidade destino", "Região de frete"],
            ["TO", "Palmas", "TO - Capital"],
            ["AC", "Rio Branco", "AC - Capital"],
        ]
    )
    assert _post_coverage_upload(web_client, "coverage.csv", coverage, "text/csv").status_code == 200
    audit_file = _sample_audit_xlsx(
        _sample_audit_row(
            numero_documento="TO-1",
            uf_destino="TO",
            cidade_destino="Palmas",
            peso="320,5",
            valor_frete="760,40",
        ),
        _sample_audit_row(
            numero_documento="AC-1",
            uf_destino="AC",
            cidade_destino="Rio Branco",
            peso="201",
            valor_frete="400,54",
        ),
    )
    assert _post_audit_upload(
        web_client,
        "auditado.xlsx",
        audit_file,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ).status_code == 200

    resp = _post_audit_run(web_client)
    assert resp.status_code == 200
    results = resp.get_json()["temp_table"]["audit_batch"]["results"]
    palmas = results[0]
    rio_branco = results[1]
    assert palmas["freight_region"] == "TO - Capital"
    assert palmas["expected_freight"] == 526.09
    assert palmas["status"] == "divergent"
    assert palmas["divergence_value"] == 234.31
    assert "regra localizada por cidade/destino" in palmas["calculation_details"]
    assert rio_branco["freight_region"] == "AC - Capital"
    assert rio_branco["expected_freight"] == 400.54
    assert rio_branco["status"] == "ok"


def test_audit_run_keeps_missing_coverage_before_city_fallback(web_client):
    _apply_payload(web_client, _sample_city_pricing_payload())
    coverage = make_csv([["UF destino", "Cidade destino", "Região de frete"], ["TO", "Palmas", "TO - Capital"]])
    assert _post_coverage_upload(web_client, "coverage.csv", coverage, "text/csv").status_code == 200
    audit_file = _sample_audit_xlsx(
        _sample_audit_row(
            numero_documento="SP-1",
            uf_destino="SP",
            cidade_destino="Rio Branco",
            peso="201",
            valor_frete="400,54",
        )
    )
    assert _post_audit_upload(
        web_client,
        "auditado.xlsx",
        audit_file,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ).status_code == 200
    resp = _post_audit_run(web_client)
    result = resp.get_json()["temp_table"]["audit_batch"]["results"][0]
    assert result["status"] == "missing_coverage_mapping"


def test_audit_run_unsupported_pricing_model(web_client):
    payload = _sample_pricing_payload()
    payload["freight_tables"] = [
        {"table_title": "SP-Interior 1", "table_type": "texto", "columns": ["Observação"], "rows": [{"Observação": "Sem faixa"}]}
    ]
    _apply_payload(web_client, payload)
    coverage = make_csv([["UF destino", "Cidade destino", "Região de frete"], ["SP", "Campinas", "SP-Interior 1"]])
    assert _post_coverage_upload(web_client, "coverage.csv", coverage, "text/csv").status_code == 200
    assert _post_audit_upload(web_client, "auditado.xlsx", _sample_audit_xlsx(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet").status_code == 200
    resp = _post_audit_run(web_client)
    assert resp.get_json()["temp_table"]["audit_batch"]["results"][0]["status"] == "unsupported_pricing_model"


def test_audit_run_city_key_with_unsupported_model_is_not_missing_rule(web_client):
    payload = _sample_city_pricing_payload()
    payload["freight_tables"] = [
        {
            "table_title": "Tabela por cidade sem faixa",
            "table_type": "texto",
            "columns": ["UF", "UF Cidades", "Observação"],
            "rows": [{"UF": "TO", "UF Cidades": "Palmas", "Observação": "Sem faixa"}],
        }
    ]
    _apply_payload(web_client, payload)
    coverage = make_csv([["UF destino", "Cidade destino", "Região de frete"], ["TO", "Palmas", "TO - Capital"]])
    assert _post_coverage_upload(web_client, "coverage.csv", coverage, "text/csv").status_code == 200
    audit_file = _sample_audit_xlsx(
        _sample_audit_row(uf_destino="TO", cidade_destino="Palmas", peso="201", valor_frete="400,54")
    )
    assert _post_audit_upload(
        web_client,
        "auditado.xlsx",
        audit_file,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ).status_code == 200
    resp = _post_audit_run(web_client)
    result = resp.get_json()["temp_table"]["audit_batch"]["results"][0]
    assert result["status"] == "unsupported_pricing_model"


def test_audited_file_max_rows_default_is_configurable(ctx):
    from app.services import cleide_audit_config_service as svc

    cfg = svc.get_cleide_audit_config()
    assert cfg.audited_file_max_rows == svc.DEFAULT_AUDITED_FILE_MAX_ROWS


def test_audit_upload_requires_login(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    anon = SimpleNamespace(is_authenticated=False)
    monkeypatch.setattr(web, "current_user", anon)
    monkeypatch.setattr("app.cleide_audit_routes.current_user", anon)
    client = web.app.test_client()
    resp = _post_audit_upload(
        client,
        "auditado.xlsx",
        _sample_audit_xlsx(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    assert resp.status_code == 401
    assert resp.get_json()["error_code"] == "auth_required"


def test_audit_upload_requires_active_temp_table(web_client):
    resp = _post_audit_upload(
        web_client,
        "auditado.xlsx",
        _sample_audit_xlsx(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    assert resp.status_code == 404
    assert resp.get_json()["error_code"] == "cleide_audit_audit_no_temp_table"


def test_audit_upload_rejects_missing_required_columns(web_client):
    _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    content = make_audit_xlsx(
        [
            ["transportadora", "cidade_destino", "uf_destino"],
            ["Transp X", "Campinas", "SP"],
        ]
    )
    resp = _post_audit_upload(
        web_client,
        "auditado.xlsx",
        content,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "cleide_audit_audit_missing_columns"


def test_audit_upload_accepts_real_template(web_client):
    _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    resp = _post_audit_upload(
        web_client,
        "auditado.xlsx",
        _sample_audit_xlsx(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    assert resp.status_code == 200
    batch = resp.get_json()["temp_table"]["audit_batch"]
    assert batch["status"] == "uploaded"
    assert batch["row_count"] == 1
    assert batch["source_file_name"] == "auditado.xlsx"
    assert batch["sheet_name"] == "Modelo Cleide"


def test_audit_upload_accepts_valor_frete_cobrado_alias(web_client):
    _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    headers = list(AUDIT_TEMPLATE_HEADERS)
    headers[7] = "valor_frete_cobrado"
    content = make_audit_xlsx([headers, _sample_audit_row()])
    resp = _post_audit_upload(
        web_client,
        "auditado.xlsx",
        content,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    assert resp.status_code == 200
    assert resp.get_json()["temp_table"]["audit_batch"]["row_count"] == 1


def test_audit_upload_accepts_peso_auditado_alias(web_client):
    _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    headers = list(AUDIT_TEMPLATE_HEADERS)
    headers[8] = "peso_auditado"
    content = make_audit_xlsx([headers, _sample_audit_row()])
    resp = _post_audit_upload(
        web_client,
        "auditado.xlsx",
        content,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    assert resp.status_code == 200
    assert resp.get_json()["temp_table"]["audit_batch"]["row_count"] == 1


def test_audit_upload_rejects_batch_above_configured_limit(web_client, monkeypatch):
    _patch_audit_cfg(monkeypatch, audited_file_max_rows=1)
    _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    content = _sample_audit_xlsx(_sample_audit_row(), _sample_audit_row(numero_documento="456"))
    resp = _post_audit_upload(
        web_client,
        "auditado.xlsx",
        content,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    assert resp.status_code == 413
    assert resp.get_json()["error_code"] == "cleide_audit_audit_too_many_rows"


def test_audit_upload_preserves_expires_at(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    original_expires = saved["expires_at"]
    resp = _post_audit_upload(
        web_client,
        "auditado.xlsx",
        _sample_audit_xlsx(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    assert resp.status_code == 200
    assert resp.get_json()["temp_table"]["expires_at"] == original_expires
    assert resp.get_json()["temp_table"]["audit_batch"]["expires_at"] == original_expires


def test_audit_upload_creates_audit_batch(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    resp = _post_audit_upload(
        web_client,
        "auditado.xlsx",
        _sample_audit_xlsx(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    assert resp.status_code == 200
    batch = resp.get_json()["temp_table"]["audit_batch"]
    assert batch["audit_batch_id"]
    assert batch["temp_table_id"] == saved["temp_table_id"]
    assert batch["max_rows"] == 2000
    assert batch["input_schema_version"] == "cleide_audit_input_v1"
    assert batch["results"] == []
    assert batch["summary"] is None
    assert batch["row_count"] == 1


def test_audit_upload_does_not_alter_freight_tables(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    before = saved["freight_tables"]
    resp = _post_audit_upload(
        web_client,
        "auditado.xlsx",
        _sample_audit_xlsx(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    assert resp.status_code == 200
    assert resp.get_json()["temp_table"]["freight_tables"] == before


def test_audit_upload_does_not_alter_coverage_table(web_client):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    resp = _post_coverage_upload(web_client, "cidades.csv", _sample_coverage_csv(), "text/csv")
    coverage_before = resp.get_json()["temp_table"]["coverage_table"]
    audit_resp = _post_audit_upload(
        web_client,
        "auditado.xlsx",
        _sample_audit_xlsx(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    assert audit_resp.status_code == 200
    assert audit_resp.get_json()["temp_table"]["coverage_table"] == coverage_before


def test_audit_upload_does_not_call_gemini(web_client, monkeypatch):
    _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    chat_mock = MagicMock()
    extraction_mock = MagicMock()
    monkeypatch.setattr("app.run_cleide_audit_chat.cleiton_governed_generate_content", chat_mock)
    monkeypatch.setattr(
        "app.run_cleide_audit_temp_table.cleiton_governed_generate_content",
        extraction_mock,
    )
    resp = _post_audit_upload(
        web_client,
        "auditado.xlsx",
        _sample_audit_xlsx(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    assert resp.status_code == 200
    chat_mock.assert_not_called()
    extraction_mock.assert_not_called()


def test_clear_documents_removes_audit_batch(web_client):
    _apply_payload(web_client, _sample_hengst_freight_tables_payload())
    resp = _post_audit_upload(
        web_client,
        "auditado.xlsx",
        _sample_audit_xlsx(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    assert resp.status_code == 200
    assert resp.get_json()["temp_table"]["audit_batch"]["row_count"] == 1

    clear_resp = web_client.post("/api/cleide-auditoria/documents/clear")
    assert clear_resp.status_code == 200
    status_after = web_client.get("/api/cleide-auditoria/documents/status").get_json()
    assert status_after.get("temp_table") is None
