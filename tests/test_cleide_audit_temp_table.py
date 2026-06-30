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
from app.cleide_audit_prompt import (
    build_cleide_audit_temp_table_fallback_prompt,
    build_cleide_audit_temp_table_technical_prompt,
)
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
    assert "rotas/tabelas de frete" in prompt.lower()
    assert "nao reconstrua freight_tables a partir de freight_routes" in prompt.lower()
    assert (
        "generalidades em freight_routes" in prompt.lower()
        or "nao coloque generalidades em freight_routes" in prompt.lower()
    )
    assert (
        "servicos adicionais em freight_routes" in prompt.lower()
        or "nao coloque servicos adicionais em freight_routes" in prompt.lower()
    )


def test_temp_table_prompt_includes_active_calculation_bases_contract():
    prompt = build_cleide_audit_temp_table_technical_prompt(
        calculation_bases=[
            {
                "id": "pct_nota_fiscal",
                "label": "% por nota fiscal",
                "unit": "%",
                "aliases": ["valor da nota fiscal"],
                "calculation_type": "invoice_percentage",
                "operation": "percentage_of_variable",
                "audit_variable": "valor_nf",
            }
        ]
    )
    assert '"id":"pct_nota_fiscal"' in prompt
    assert '"label":"% por nota fiscal"' in prompt
    assert '"aliases":["valor da nota fiscal"]' in prompt
    assert "calculation_base_id" in prompt
    assert "calculation_base_label" in prompt
    assert "raw_calculation_basis" in prompt
    assert "calculation_base_id null" in prompt.lower()


def test_temp_table_prompt_is_light_partial_first():
    prompt = build_cleide_audit_temp_table_technical_prompt()
    lowered = prompt.lower()
    assert "nao calcula frete esperado" in lowered
    assert "nao monta auditoria final" in lowered
    assert "normalizacao do vinculo sera feita pelo backend" in lowered
    assert "sao opcionais" in lowered
    assert "memoria de calculo" in lowered
    assert "nao calcula frete esperado, divergencia" in lowered


def test_temp_table_prompt_requests_minimum_as_simple_extraction():
    prompt = build_cleide_audit_temp_table_technical_prompt()
    lowered = prompt.lower()
    assert "taxa minima" in lowered
    assert "extraia o item minimo separadamente" in lowered
    assert "nao calcule nem aplique o minimo" in lowered
    assert "modifier_type \"minimum_amount\"" not in prompt
    assert "compartilhe component_group" not in lowered


def test_temp_table_fallback_prompt_is_shorter_and_focused():
    main_prompt = build_cleide_audit_temp_table_technical_prompt()
    fallback_prompt = build_cleide_audit_temp_table_fallback_prompt()
    assert len(fallback_prompt) < len(main_prompt) * 0.5
    lowered = fallback_prompt.lower()
    assert "freight_routes" in fallback_prompt
    assert "weight_ranges" in fallback_prompt
    assert "accessorial_fees" in fallback_prompt
    assert "nao calcule frete" in lowered
    assert "calculation_bases" not in lowered


def test_extraction_fallback_uses_lite_prompt_after_primary_timeout(web_client, monkeypatch):
    calls: list[dict] = []

    def _generate(_client, *, model, contents, **_kwargs):
        prompt_text = contents[-1] if isinstance(contents, list) else contents
        calls.append({"model": model, "prompt": prompt_text})
        if model == "gemini-2.5-flash":
            raise Exception("504 DEADLINE_EXCEEDED")
        return SimpleNamespace(text=json.dumps(_alfa_like_partial_extraction_payload()))

    monkeypatch.setattr(audit_temp_table, "cleiton_governed_generate_content", _generate)
    monkeypatch.setattr(audit_temp_table, "_get_client", lambda: object())
    monkeypatch.setattr(
        audit_temp_table,
        "_get_model_candidates",
        lambda: ["gemini-2.5-flash", "gemini-2.5-flash-lite"],
    )
    _upload(
        web_client,
        "Tabela Alfa teste.xlsx",
        make_xlsx([["origem", "destino"], ["PR", "Capital"]]),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    assert len(calls) == 2
    assert calls[0]["model"] == "gemini-2.5-flash"
    assert calls[1]["model"] == "gemini-2.5-flash-lite"
    assert len(calls[1]["prompt"]) < len(calls[0]["prompt"])
    assert "calculation_bases" not in calls[1]["prompt"].lower()
    temp_table = web_client.get("/api/cleide-auditoria/documents/status").get_json()["temp_table"]
    assert temp_table["status"] == TEMP_TABLE_STATUS_NEEDS_REVIEW
    assert len(temp_table["freight_routes"]) == 1


def _alfa_like_partial_extraction_payload(**overrides) -> dict:
    payload = {
        "status": "needs_review",
        "freight_routes": [
            {
                "origin": "PR",
                "destination": "PR - Capital",
                "weight_30": "41,03",
                "weight_50": "45,46",
                "weight_70": "54,33",
                "weight_100": "57,66",
                "freight_weight_kg": "0,45",
            }
        ],
        "weight_ranges": [
            {"label": "ate 30 Kg", "min_weight": None, "max_weight": 30, "unit": "kg", "notes": ""},
        ],
        "accessorial_fees": [
            {
                "name": "GRIS",
                "value": "0,15%",
                "unit": "%",
                "calculation_basis": "sobre NF",
                "raw_calculation_basis": "sobre o valor da Nota Fiscal",
                "notes": "",
            },
            {
                "name": "GRIS minimo",
                "value": "R$ 4,99",
                "unit": "R$",
                "raw_calculation_basis": "minimo por CTe",
                "notes": "",
            },
        ],
        "freight_tables": [],
        "freight_values": [],
        "reading_alerts": [],
        "evidence_refs": [],
    }
    payload.update(overrides)
    return payload


def test_alfa_like_xlsx_partial_extraction_becomes_needs_review(web_client, monkeypatch):
    _fake_extraction_generate(monkeypatch, text=json.dumps(_alfa_like_partial_extraction_payload()))
    _upload(
        web_client,
        "Tabela Alfa teste.xlsx",
        make_xlsx([["origem", "destino"], ["PR", "Capital"]]),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    temp_table = web_client.get("/api/cleide-auditoria/documents/status").get_json()["temp_table"]
    assert temp_table["status"] == TEMP_TABLE_STATUS_NEEDS_REVIEW
    assert len(temp_table["freight_routes"]) == 1
    assert len(temp_table["weight_ranges"]) == 1
    assert len(temp_table["accessorial_fees"]) == 2


def test_partial_extraction_without_technical_fields_becomes_needs_review(web_client, monkeypatch):
    payload = _alfa_like_partial_extraction_payload()
    _fake_extraction_generate(monkeypatch, text=json.dumps(payload))
    _upload(web_client, "t.csv", make_csv([["a"], ["1"]]), "text/csv")
    temp_table = web_client.get("/api/cleide-auditoria/documents/status").get_json()["temp_table"]
    assert temp_table["status"] == TEMP_TABLE_STATUS_NEEDS_REVIEW
    gris = next(item for item in temp_table["accessorial_fees"] if item["name"] == "GRIS")
    assert gris.get("modifier_type") in (None, "base_fee")
    minimum = next(item for item in temp_table["accessorial_fees"] if "minimo" in item["name"].lower())
    assert minimum.get("minimum_amount") in (None, 4.99)


def test_double_model_timeout_without_response_stays_failed(web_client, monkeypatch):
    call_count = {"n": 0}

    def _fail(*_args, **_kwargs):
        call_count["n"] += 1
        raise Exception("504 DEADLINE_EXCEEDED")

    monkeypatch.setattr(audit_temp_table, "cleiton_governed_generate_content", _fail)
    monkeypatch.setattr(audit_temp_table, "_get_client", lambda: object())
    monkeypatch.setattr(
        audit_temp_table,
        "_get_model_candidates",
        lambda: ["gemini-2.5-flash", "gemini-2.5-flash-lite"],
    )
    _upload(web_client, "t.csv", make_csv([["a"], ["1"]]), "text/csv")
    assert call_count["n"] == 2
    temp_table = web_client.get("/api/cleide-auditoria/documents/status").get_json()["temp_table"]
    assert temp_table["status"] == TEMP_TABLE_STATUS_FAILED
    assert audit_temp_table.READING_ALERT_PROVIDER_TIMEOUT in temp_table["reading_alerts"]
    assert temp_table["freight_routes"] == []


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


def test_temp_table_save_and_advance_blocks_unmapped_calculation_base(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[
                {
                    "name": "Taxa XPTO",
                    "value": "0,20",
                    "unit": "%",
                    "calculation_basis": "não mapeado / revisar",
                    "calculation_base_id": None,
                    "raw_calculation_basis": "sobre NF",
                    "notes": "",
                }
            ]
        ),
    )
    resp = _post_temp_table_save(web_client, _save_payload_for_record(saved))
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["error_code"] == "invalid_accessorial_fees"
    assert data["message"] == "Revise as generalidades antes de avançar."
    assert data["errors"] == [
        {
            "section": "accessorial_fees",
            "index": 0,
            "name": "Taxa XPTO",
            "field": "calculation_base_id",
            "reason_code": "missing_calculation_base",
            "message": "Selecione uma base de cálculo ou exclua a linha.",
        }
    ]


def test_temp_table_save_and_advance_accepts_manual_configured_base(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[
                {
                    "name": "Taxa XPTO",
                    "value": "0,20",
                    "unit": "%",
                    "calculation_basis": "não mapeado / revisar",
                    "calculation_base_id": None,
                    "raw_calculation_basis": "sobre NF",
                    "notes": "",
                }
            ]
        ),
    )
    edited = _save_payload_for_record(saved)
    edited["edit_target"]["accessorial_fees"][0].update(
        {
            "calculation_base_id": "pct_nota_fiscal",
            "calculation_basis": "% por nota fiscal",
            "classification_source": "manual_configured_calculation_base",
        }
    )
    resp = _post_temp_table_save(web_client, edited)
    assert resp.status_code == 200
    fee = resp.get_json()["temp_table"]["accessorial_fees"][0]
    assert fee["calculation_base_id"] == "pct_nota_fiscal"
    assert fee["calculation_base_label"] == "% por nota fiscal"
    assert fee["operation"] == "percentage_of_variable"
    assert fee["audit_variable"] == "valor_nf"
    assert fee["raw_calculation_basis"] == "sobre NF"


def _manual_accessorial_fee(**overrides) -> dict:
    fee = {
        "name": "Taxa XPTO",
        "value": "0,20",
        "unit": "%",
        "calculation_basis": "não mapeado / revisar",
        "calculation_base_id": None,
        "raw_calculation_basis": "sobre NF",
        "notes": "",
    }
    fee.update(overrides)
    return fee


def _assert_accessorial_advance_error(resp, *, reason_code: str, field: str, index: int = 0):
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["error_code"] == "invalid_accessorial_fees"
    assert data["message"] == "Revise as generalidades antes de avançar."
    assert isinstance(data.get("errors"), list)
    assert data["errors"][0]["section"] == "accessorial_fees"
    assert data["errors"][0]["index"] == index
    assert data["errors"][0]["field"] == field
    assert data["errors"][0]["reason_code"] == reason_code


def test_temp_table_save_and_advance_accepts_por_cte_fixed_amount_without_audit_variable(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[_manual_accessorial_fee(value="10,24", unit="R$")]
        ),
    )
    edited = _save_payload_for_record(saved)
    edited["edit_target"]["accessorial_fees"][0].update(
        {
            "calculation_base_id": "por_cte",
            "calculation_basis": "por CTe",
            "classification_source": "manual_configured_calculation_base",
            "operation": "fixed_amount",
            "calculation_type": "fixed_amount",
            "audit_variable": None,
        }
    )
    resp = _post_temp_table_save(web_client, edited)
    assert resp.status_code == 200
    fee = resp.get_json()["temp_table"]["accessorial_fees"][0]
    assert fee["calculation_base_id"] == "por_cte"
    assert fee["operation"] == "fixed_amount"
    assert fee.get("audit_variable") is None


def test_temp_table_save_and_advance_por_cte_missing_value_is_invalid_accessorial_value(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[_manual_accessorial_fee(value="", unit="R$")]
        ),
    )
    edited = _save_payload_for_record(saved)
    edited["edit_target"]["accessorial_fees"][0].update(
        {
            "calculation_base_id": "por_cte",
            "calculation_basis": "por CTe",
            "classification_source": "manual_configured_calculation_base",
            "operation": "fixed_amount",
            "calculation_type": "fixed_amount",
        }
    )
    resp = _post_temp_table_save(web_client, edited)
    _assert_accessorial_advance_error(
        resp,
        reason_code="invalid_accessorial_value",
        field="value",
    )


def test_temp_table_save_and_advance_pct_nota_fiscal_valid(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(accessorial_fees=[_manual_accessorial_fee()]),
    )
    edited = _save_payload_for_record(saved)
    edited["edit_target"]["accessorial_fees"][0].update(
        {
            "calculation_base_id": "pct_nota_fiscal",
            "calculation_basis": "% por nota fiscal",
            "classification_source": "manual_configured_calculation_base",
            "operation": "percentage_of_variable",
            "audit_variable": "valor_nf",
        }
    )
    resp = _post_temp_table_save(web_client, edited)
    assert resp.status_code == 200


def test_temp_table_save_and_advance_pct_nota_fiscal_missing_value(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[_manual_accessorial_fee(value="", unit="%")]
        ),
    )
    edited = _save_payload_for_record(saved)
    edited["edit_target"]["accessorial_fees"][0].update(
        {
            "calculation_base_id": "pct_nota_fiscal",
            "calculation_basis": "% por nota fiscal",
            "classification_source": "manual_configured_calculation_base",
            "operation": "percentage_of_variable",
            "audit_variable": "valor_nf",
        }
    )
    resp = _post_temp_table_save(web_client, edited)
    _assert_accessorial_advance_error(
        resp,
        reason_code="invalid_accessorial_value",
        field="value",
    )


def test_temp_table_save_and_advance_por_kg_missing_value(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[_manual_accessorial_fee(value="", unit="R$")]
        ),
    )
    edited = _save_payload_for_record(saved)
    edited["edit_target"]["accessorial_fees"][0].update(
        {
            "calculation_base_id": "por_kg",
            "calculation_basis": "por kg",
            "classification_source": "manual_configured_calculation_base",
            "operation": "multiply_by_variable",
            "audit_variable": "peso",
        }
    )
    resp = _post_temp_table_save(web_client, edited)
    _assert_accessorial_advance_error(
        resp,
        reason_code="invalid_accessorial_value",
        field="value",
    )


def test_temp_table_save_and_advance_por_kg_valid(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[_manual_accessorial_fee(value="4,00", unit="R$")]
        ),
    )
    edited = _save_payload_for_record(saved)
    edited["edit_target"]["accessorial_fees"][0].update(
        {
            "calculation_base_id": "por_kg",
            "calculation_basis": "por kg",
            "classification_source": "manual_configured_calculation_base",
            "operation": "multiply_by_variable",
            "audit_variable": "peso",
        }
    )
    resp = _post_temp_table_save(web_client, edited)
    assert resp.status_code == 200


def test_temp_table_save_and_advance_ceil_fraction_requires_fraction_size(web_client, monkeypatch):
    import app.services.cleide_audit_config_service as cfg_service

    incomplete_base = dict(cfg_service.DEFAULT_CALCULATION_BASES[5])
    incomplete_base["id"] = "fracao_incompleta"
    incomplete_base["label"] = "fração incompleta"
    incomplete_base["parameters"] = {}
    _patch_audit_cfg(monkeypatch, calculation_bases=[incomplete_base])
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[_manual_accessorial_fee(value="4,00", unit="R$")]
        ),
    )
    edited = _save_payload_for_record(saved)
    edited["edit_target"]["accessorial_fees"][0].update(
        {
            "calculation_base_id": "fracao_incompleta",
            "calculation_basis": "fração incompleta",
            "classification_source": "manual_configured_calculation_base",
            "operation": "ceil_fraction",
            "audit_variable": "peso",
            "operation_parameters": {},
        }
    )
    resp = _post_temp_table_save(web_client, edited)
    _assert_accessorial_advance_error(
        resp,
        reason_code="unsupported_or_incomplete_operation",
        field="calculation_base_id",
    )


def test_temp_table_save_and_advance_incompatible_unit_for_por_cte(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[_manual_accessorial_fee(value="10,24", unit="%")]
        ),
    )
    edited = _save_payload_for_record(saved)
    edited["edit_target"]["accessorial_fees"][0].update(
        {
            "calculation_base_id": "por_cte",
            "calculation_basis": "por CTe",
            "classification_source": "manual_configured_calculation_base",
            "operation": "fixed_amount",
            "calculation_type": "fixed_amount",
        }
    )
    resp = _post_temp_table_save(web_client, edited)
    _assert_accessorial_advance_error(
        resp,
        reason_code="incompatible_accessorial_unit",
        field="unit",
    )


def test_validate_accessorial_fee_for_advance_returns_structured_errors():
    active_bases = {
        "por_cte": {"id": "por_cte", "unit": "R$", "operation": "fixed_amount"},
        "por_kg": {"id": "por_kg", "unit": "R$", "operation": "multiply_by_variable"},
    }
    missing_base = audit_doc_service._validate_accessorial_fee_for_advance(
        {"name": "TAS", "calculation_basis": "não mapeado / revisar"},
        0,
        active_bases,
    )
    assert missing_base["reason_code"] == "missing_calculation_base"
    assert missing_base["field"] == "calculation_base_id"

    missing_value = audit_doc_service._validate_accessorial_fee_for_advance(
        {
            "name": "TAS",
            "calculation_base_id": "por_cte",
            "calculation_basis": "por CTe",
            "operation": "fixed_amount",
            "value": "",
            "unit": "R$",
        },
        1,
        active_bases,
    )
    assert missing_value["reason_code"] == "invalid_accessorial_value"
    assert missing_value["field"] == "value"

    incompatible_unit = audit_doc_service._validate_accessorial_fee_for_advance(
        {
            "name": "TAS",
            "calculation_base_id": "por_cte",
            "calculation_basis": "por CTe",
            "operation": "fixed_amount",
            "value": "10,24",
            "unit": "%",
        },
        2,
        active_bases,
    )
    assert incompatible_unit["reason_code"] == "incompatible_accessorial_unit"
    assert incompatible_unit["field"] == "unit"

    incomplete_operation = audit_doc_service._validate_accessorial_fee_for_advance(
        {
            "name": "Taxa peso",
            "calculation_base_id": "fracao_100kg",
            "calculation_basis": "por fração de 100kg",
            "operation": "ceil_fraction",
            "audit_variable": "peso",
            "operation_parameters": {},
            "value": "4,00",
            "unit": "R$",
        },
        3,
        {"fracao_100kg": {"id": "fracao_100kg", "unit": "R$", "operation": "ceil_fraction"}},
    )
    assert incomplete_operation["reason_code"] == "unsupported_or_incomplete_operation"
    assert incomplete_operation["field"] == "calculation_base_id"


def _linked_gris_and_minimum_accessorial_fees() -> list[dict]:
    return [
        {
            "name": "GRIS",
            "value": "0,15%",
            "unit": "%",
            "calculation_basis": "% por nota fiscal",
            "calculation_base_id": "pct_nota_fiscal",
            "classification_source": "manual_configured_calculation_base",
            "operation": "percentage_of_variable",
            "audit_variable": "valor_nf",
            "modifier_type": "base_fee",
            "component_group": "gris",
            "related_to": None,
        },
        {
            "name": "GRIS mínimo",
            "value": "R$ 4,99",
            "unit": "R$",
            "calculation_basis": "não mapeado / revisar",
            "modifier_type": "minimum_amount",
            "calculation_type": "minimum_amount",
            "minimum_amount": 4.99,
            "component_group": "gris",
            "related_to": "gris",
            "canonical_component": "risk_management",
        },
    ]


def _configured_base_with_linked_minimum_fees(
    *,
    base_name: str,
    minimum_name: str,
    group: str,
    rate_value: str,
    minimum_amount: float,
) -> list[dict]:
    return [
        {
            "name": base_name,
            "value": rate_value,
            "unit": "%",
            "calculation_base_id": "pct_nota_fiscal",
            "classification_source": "configured_calculation_base",
            "operation": "percentage_of_variable",
            "audit_variable": "valor_nf",
            "modifier_type": "base_fee",
            "component_group": group,
            "canonical_component": group,
            "calculation_type": "invoice_percentage",
            "status": "calculable",
            "classification_confidence": "high",
        },
        {
            "name": minimum_name,
            "value": f"R$ {minimum_amount:.2f}".replace(".", ","),
            "unit": "R$",
            "modifier_type": "minimum_amount",
            "calculation_type": "minimum_amount",
            "minimum_amount": minimum_amount,
            "component_group": group,
            "related_to": group,
            "canonical_component": group,
            "status": "calculable",
            "classification_confidence": "high",
        },
    ]


def test_build_configured_accessorial_applies_linked_minimum_when_calculated_is_lower(web_client):
    from decimal import Decimal

    saved = _apply_payload(
        web_client,
        {
            "status": "needs_review",
            "freight_tables": [],
            "freight_routes": [],
            "freight_values": [],
            "accessorial_fees": _configured_base_with_linked_minimum_fees(
                base_name="GRIS",
                minimum_name="GRIS mínimo",
                group="risk_management",
                rate_value="0.15",
                minimum_amount=4.99,
            ),
            "weight_ranges": [],
            "reading_alerts": [],
            "evidence_refs": [],
        },
    )
    calculated, ignored, total = audit_doc_service._build_accessorial_percent_fee_components(
        saved["accessorial_fees"],
        invoice_value=Decimal("3016.55"),
        audit_variables={"valor_nf": Decimal("3016.55")},
        has_tariff_freight_value=False,
    )
    assert len(calculated) == 1
    component = calculated[0]
    assert component["label"] == "GRIS"
    assert component["calculation_base_id"] == "pct_nota_fiscal"
    assert component["operation"] == "percentage_of_variable"
    assert component["calculated_amount"] == 4.52
    assert component["minimum_amount"] == 4.99
    assert component["minimum_applied"] is True
    assert component["amount"] == 4.99
    assert "mínimo aplicado" in component["details"]
    assert total == Decimal("4.99")
    assert ignored == []


def test_build_configured_accessorial_keeps_calculated_when_above_linked_minimum(web_client):
    from decimal import Decimal

    saved = _apply_payload(
        web_client,
        {
            "status": "needs_review",
            "freight_tables": [],
            "freight_routes": [],
            "freight_values": [],
            "accessorial_fees": _configured_base_with_linked_minimum_fees(
                base_name="GRIS",
                minimum_name="GRIS mínimo",
                group="risk_management",
                rate_value="0.15",
                minimum_amount=4.99,
            ),
            "weight_ranges": [],
            "reading_alerts": [],
            "evidence_refs": [],
        },
    )
    calculated, ignored, total = audit_doc_service._build_accessorial_percent_fee_components(
        saved["accessorial_fees"],
        invoice_value=Decimal("10000"),
        audit_variables={"valor_nf": Decimal("10000")},
        has_tariff_freight_value=False,
    )
    assert len(calculated) == 1
    component = calculated[0]
    assert component["amount"] == 15.00
    assert component["minimum_applied"] is False
    assert component["minimum_amount"] == 4.99
    assert "mínimo não aplicado" in component["details"]
    assert total == Decimal("15")
    assert ignored == []


def test_build_configured_accessorial_orphan_minimum_stays_ignored(web_client):
    from decimal import Decimal

    saved = _apply_payload(
        web_client,
        {
            "status": "needs_review",
            "freight_tables": [],
            "freight_routes": [],
            "freight_values": [],
            "accessorial_fees": [
                {
                    "name": "GRIS mínimo",
                    "value": "R$ 4,99",
                    "unit": "R$",
                    "modifier_type": "minimum_amount",
                    "calculation_type": "minimum_amount",
                    "minimum_amount": 4.99,
                    "component_group": "risk_management",
                    "related_to": "risk_management",
                    "canonical_component": "risk_management",
                    "status": "calculable",
                    "classification_confidence": "high",
                }
            ],
            "weight_ranges": [],
            "reading_alerts": [],
            "evidence_refs": [],
        },
    )
    calculated, ignored, total = audit_doc_service._build_accessorial_percent_fee_components(
        saved["accessorial_fees"],
        invoice_value=Decimal("3016.55"),
        audit_variables={"valor_nf": Decimal("3016.55")},
        has_tariff_freight_value=False,
    )
    assert calculated == []
    assert total == Decimal("0")
    assert len(ignored) == 1
    assert ignored[0]["label"] == "GRIS mínimo"
    assert ignored[0]["reason_code"] == "accessorial_minimum_without_base_ignored"


def test_build_configured_accessorial_applies_linked_minimum_for_non_gris_component(web_client):
    from decimal import Decimal

    saved = _apply_payload(
        web_client,
        {
            "status": "needs_review",
            "freight_tables": [],
            "freight_routes": [],
            "freight_values": [],
            "accessorial_fees": _configured_base_with_linked_minimum_fees(
                base_name="Ad Valorem",
                minimum_name="Ad Valorem mínimo",
                group="ad_valorem",
                rate_value="0,10",
                minimum_amount=5.00,
            ),
            "weight_ranges": [],
            "reading_alerts": [],
            "evidence_refs": [],
        },
    )
    calculated, ignored, total = audit_doc_service._build_accessorial_percent_fee_components(
        saved["accessorial_fees"],
        invoice_value=Decimal("1000"),
        audit_variables={"valor_nf": Decimal("1000")},
        has_tariff_freight_value=False,
    )
    assert len(calculated) == 1
    component = calculated[0]
    assert component["label"] == "Ad Valorem"
    assert component["component_group"] == "ad_valorem"
    assert component["calculated_amount"] == 1.00
    assert component["minimum_applied"] is True
    assert component["amount"] == 5.00
    assert total == Decimal("5")
    assert ignored == []


def test_validate_linked_minimum_amount_allows_advance_without_calculation_base():
    fees = _linked_gris_and_minimum_accessorial_fees()
    assert (
        audit_doc_service._validate_linked_minimum_amount_for_advance(fees[1], 1, fees)
        is None
    )


def test_normalize_minimum_modifier_reconciles_conditional_gemini_payload():
    fees = audit_doc_service._normalize_accessorial_fees(
        [
            {
                "name": "GRIS",
                "value": "0.15",
                "unit": "%",
                "calculation_basis": "NF",
                "calculation_base_id": "pct_nota_fiscal",
                "modifier_type": "base_fee",
                "component_group": "risk_management",
                "canonical_component": "risk_management",
            },
            {
                "name": "GRIS mínimo",
                "value": "4.99",
                "unit": "R$",
                "calculation_basis": "não mapeado / revisar",
                "raw_calculation_basis": "GRIS-PERCENTUAL-NF-MINIMO",
                "modifier_type": "minimum_amount",
                "component_group": "risk_management",
                "related_to": "risk_management",
                "canonical_component": "risk_management",
            },
        ]
    )
    minimum = _fee_by_name(fees, "GRIS mínimo")
    assert minimum["calculation_type"] == "minimum_amount"
    assert minimum["modifier_type"] == "minimum_amount"
    assert minimum["minimum_amount"] == 4.99
    assert minimum["related_to"] == "risk_management"
    assert minimum["status"] == "calculable"
    assert "unsupported_reason" not in minimum
    assert not minimum.get("calculation_basis")


def test_normalize_minimum_with_value_only_populates_minimum_amount():
    fees = audit_doc_service._normalize_accessorial_fees(
        [
            {
                "name": "GRIS",
                "value": "0,15%",
                "unit": "%",
                "calculation_basis": "sobre NF",
                "calculation_base_id": "pct_nota_fiscal",
            },
            {
                "name": "GRIS mínimo",
                "value": "4.99",
                "unit": "R$",
                "modifier_type": "minimum_amount",
                "related_to": "risk_management",
                "component_group": "risk_management",
                "canonical_component": "risk_management",
            },
        ]
    )
    minimum = _fee_by_name(fees, "GRIS mínimo")
    assert minimum["minimum_amount"] == 4.99


def test_validate_linked_minimum_with_value_only_passes_advance():
    fees = audit_doc_service._normalize_accessorial_fees(
        [
            {
                "name": "GRIS",
                "value": "0.15",
                "unit": "%",
                "calculation_basis": "NF",
                "calculation_base_id": "pct_nota_fiscal",
                "modifier_type": "base_fee",
                "component_group": "risk_management",
                "canonical_component": "risk_management",
            },
            {
                "name": "GRIS mínimo",
                "value": "4.99",
                "unit": "R$",
                "modifier_type": "minimum_amount",
                "component_group": "risk_management",
                "related_to": "risk_management",
                "canonical_component": "risk_management",
            },
        ]
    )
    assert audit_doc_service._validate_linked_minimum_amount_for_advance(fees[1], 1, fees) is None


def test_validate_orphan_minimum_amount_blocks_advance_missing_link():
    error = audit_doc_service._validate_linked_minimum_amount_for_advance(
        {
            "name": "Taxa mínima",
            "value": "R$ 4,99",
            "unit": "R$",
            "calculation_basis": "não mapeado / revisar",
            "modifier_type": "minimum_amount",
            "calculation_type": "minimum_amount",
            "minimum_amount": 4.99,
        },
        0,
        [],
    )
    assert error is not None
    assert error["reason_code"] == "missing_minimum_base_link"
    assert error["field"] == "related_to"


def test_validate_minimum_with_invalid_base_link_blocks_advance():
    fees = _linked_gris_and_minimum_accessorial_fees()
    fees[1]["related_to"] = "inexistente"
    error = audit_doc_service._validate_linked_minimum_amount_for_advance(fees[1], 1, fees)
    assert error is not None
    assert error["reason_code"] == "invalid_minimum_base_link"
    assert error["field"] == "related_to"


def test_validate_minimum_with_invalid_value_blocks_advance():
    fees = _linked_gris_and_minimum_accessorial_fees()
    fees[1]["minimum_amount"] = None
    fees[1]["value"] = ""
    error = audit_doc_service._validate_linked_minimum_amount_for_advance(fees[1], 1, fees)
    assert error is not None
    assert error["reason_code"] == "invalid_accessorial_value"
    assert error["field"] == "value"


def test_validate_common_fee_without_base_still_blocks_advance():
    error = audit_doc_service._validate_accessorial_fee_for_advance(
        {
            "name": "Taxa XPTO",
            "value": "0,20",
            "unit": "%",
            "calculation_basis": "não mapeado / revisar",
        },
        0,
        {"por_cte": {"id": "por_cte", "unit": "R$", "operation": "fixed_amount"}},
    )
    assert error is not None
    assert error["reason_code"] == "missing_calculation_base"
    assert error["field"] == "calculation_base_id"


def test_temp_table_save_and_advance_accepts_linked_minimum_without_calculation_base(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[
                {
                    "name": "GRIS",
                    "value": "0.15",
                    "unit": "%",
                    "calculation_basis": "NF",
                    "calculation_base_id": "pct_nota_fiscal",
                    "modifier_type": "base_fee",
                    "component_group": "risk_management",
                    "canonical_component": "risk_management",
                },
                {
                    "name": "GRIS mínimo",
                    "value": "4.99",
                    "unit": "R$",
                    "calculation_basis": "não mapeado / revisar",
                    "raw_calculation_basis": "GRIS-PERCENTUAL-NF-MINIMO",
                    "modifier_type": "minimum_amount",
                    "component_group": "risk_management",
                    "related_to": "risk_management",
                    "canonical_component": "risk_management",
                },
            ]
        ),
    )
    minimum = _fee_by_name(saved["accessorial_fees"], "GRIS mínimo")
    assert minimum["minimum_amount"] == 4.99
    assert minimum["calculation_type"] == "minimum_amount"
    assert minimum["status"] == "calculable"

    edited = _save_payload_for_record(saved)
    edited["edit_target"]["accessorial_fees"][0].update(
        {
            "classification_source": "manual_configured_calculation_base",
            "operation": "percentage_of_variable",
            "audit_variable": "valor_nf",
        }
    )
    minimum = edited["edit_target"]["accessorial_fees"][1]
    assert minimum["modifier_type"] == "minimum_amount"
    assert minimum["related_to"] == "risk_management"
    assert not minimum.get("calculation_base_id")
    resp = _post_temp_table_save(web_client, edited)
    assert resp.status_code == 200
    saved_minimum = resp.get_json()["temp_table"]["accessorial_fees"][1]
    assert saved_minimum["modifier_type"] == "minimum_amount"
    assert saved_minimum["related_to"] == "risk_management"
    assert saved_minimum["minimum_amount"] == 4.99
    assert saved_minimum.get("calculation_base_id") is None


def test_temp_table_save_and_advance_blocks_orphan_minimum_without_base_link(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[
                {
                    "name": "Taxa mínima",
                    "value": "R$ 4,99",
                    "unit": "R$",
                    "calculation_basis": "não mapeado / revisar",
                    "notes": "",
                }
            ]
        ),
    )
    resp = _post_temp_table_save(web_client, _save_payload_for_record(saved))
    _assert_accessorial_advance_error(
        resp,
        reason_code="missing_minimum_base_link",
        field="related_to",
    )


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


def test_accessorial_fees_normalization_preserves_derived_fields(web_client):
    payload = _sample_hengst_freight_tables_payload(
        accessorial_fees=[
            {
                "name": "GRIS",
                "value": "0,30%",
                "unit": "%",
                "calculation_basis": "valor_nf",
                "notes": "% por nota",
                "scope": "global",
                "calculation_type": "invoice_percentage",
                "canonical_component": "risk_management",
                "classification_confidence": "high",
                "status": "calculable",
                "component_group": "gris",
                "modifier_type": "base_fee",
                "related_to": None,
            }
        ]
    )
    saved = _apply_payload(web_client, payload)
    fee = saved["accessorial_fees"][0]
    assert fee["name"] == "GRIS"
    assert fee["value"] == "0,30%"
    assert fee["unit"] == "%"
    assert fee["calculation_basis"] == "% por nota fiscal"
    assert fee["notes"] == "% por nota"
    assert fee["scope"] == "global"
    assert fee["calculation_type"] == "invoice_percentage"
    assert fee["canonical_component"] == "risk_management"
    assert fee["classification_confidence"] == "high"
    assert fee["status"] == "calculable"
    assert fee["component_group"] == "gris"
    assert fee["modifier_type"] == "base_fee"
    assert fee["related_to"] is None


def _fee_by_name(fees: list[dict], name: str) -> dict:
    return next(fee for fee in fees if fee["name"] == name)


@pytest.mark.parametrize(
    ("base_fee", "minimum_fee", "group"),
    [
        (
            {
                "name": "GRIS: % sobre o valor da Nota Fiscal",
                "value": "0,30%",
                "unit": "%",
                "calculation_basis": "sobre o valor da Nota Fiscal",
                "notes": "",
            },
            {
                "name": "GRIS Mínimo R$ por Cte",
                "value": "R$ 4,13",
                "unit": "R$",
                "calculation_basis": "por Cte",
                "notes": "",
            },
            "gris",
        ),
        (
            {
                "name": "TRT: % sobre o valor do frete",
                "value": "5%",
                "unit": "%",
                "calculation_basis": "sobre o valor do frete",
                "notes": "",
            },
            {
                "name": "TRT: (Taxa Mínima) R$ por Cte",
                "value": "R$ 15,00",
                "unit": "R$",
                "calculation_basis": "por Cte",
                "notes": "",
            },
            "trt",
        ),
        (
            {
                "name": "TDE: % sobre o valor do frete",
                "value": "10%",
                "unit": "%",
                "calculation_basis": "sobre o valor do frete",
                "notes": "",
            },
            {
                "name": "TDE mínimo: R$ por Cte",
                "value": "R$ 20,00",
                "unit": "R$",
                "calculation_basis": "por Cte",
                "notes": "",
            },
            "tde",
        ),
        (
            {
                "name": "Taxa de agendamento: % do frete original",
                "value": "3%",
                "unit": "%",
                "calculation_basis": "do frete original",
                "notes": "",
            },
            {
                "name": "Agendamento mínimo: R$ por Cte",
                "value": "R$ 12,00",
                "unit": "R$",
                "calculation_basis": "por Cte",
                "notes": "",
            },
            "agendamento",
        ),
    ],
)
def test_accessorial_fees_link_base_fee_and_minimum_modifier(web_client, base_fee, minimum_fee, group):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(accessorial_fees=[base_fee, minimum_fee]),
    )
    base = _fee_by_name(saved["accessorial_fees"], base_fee["name"])
    minimum = _fee_by_name(saved["accessorial_fees"], minimum_fee["name"])

    assert base["component_group"] == group
    assert base["modifier_type"] == "base_fee"
    assert base["related_to"] is None
    assert minimum["component_group"] == group
    assert minimum["calculation_type"] == "minimum_amount"
    assert minimum["modifier_type"] == "minimum_amount"
    assert minimum["related_to"] == group


def test_accessorial_minimum_without_matching_base_fee_stays_unlinked(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[
                {
                    "name": "GRIS Mínimo R$ por Cte",
                    "value": "R$ 4,13",
                    "unit": "R$",
                    "calculation_basis": "por Cte",
                    "notes": "",
                }
            ]
        ),
    )
    fee = saved["accessorial_fees"][0]
    assert fee["calculation_type"] == "minimum_amount"
    assert fee["modifier_type"] == "minimum_amount"
    assert fee["component_group"] == "gris"
    assert fee["related_to"] is None
    assert fee["status"] == "needs_review"


def test_accessorial_gris_percentual_nf_links_hyphenated_minimum(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[
                {
                    "name": "GRIS-PERCENTUAL-NF",
                    "value": "0,15",
                    "unit": "%",
                    "calculation_basis": "% por nota fiscal",
                    "calculation_base_id": "pct_nota_fiscal",
                    "notes": "",
                },
                {
                    "name": "GRIS-PERCENTUAL-NF-MINIMO",
                    "value": "4,99",
                    "unit": "R$",
                    "calculation_basis": "não mapeado / revisar",
                    "notes": "",
                },
            ]
        ),
    )
    base = _fee_by_name(saved["accessorial_fees"], "GRIS-PERCENTUAL-NF")
    minimum = _fee_by_name(saved["accessorial_fees"], "GRIS-PERCENTUAL-NF-MINIMO")

    assert base["calculation_type"] == "invoice_percentage"
    assert base["modifier_type"] == "base_fee"
    assert base["status"] == "calculable"
    assert base["component_group"] == "gris"
    assert base["related_to"] is None

    assert minimum["calculation_type"] == "minimum_amount"
    assert minimum["modifier_type"] == "minimum_amount"
    assert minimum["minimum_amount"] == 4.99
    assert minimum["component_group"] == "gris"
    assert minimum["related_to"] == "gris"
    assert minimum["status"] == "calculable"
    assert not minimum.get("calculation_basis")


def test_accessorial_ad_valorem_percent_links_minimum(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[
                {
                    "name": "Ad Valorem",
                    "value": "0,10%",
                    "unit": "%",
                    "calculation_basis": "sobre NF",
                    "notes": "",
                },
                {
                    "name": "Ad Valorem mínimo",
                    "value": "R$ 5,00",
                    "unit": "R$",
                    "calculation_basis": "não mapeado / revisar",
                    "notes": "",
                },
            ]
        ),
    )
    base = _fee_by_name(saved["accessorial_fees"], "Ad Valorem")
    minimum = _fee_by_name(saved["accessorial_fees"], "Ad Valorem mínimo")

    assert base["calculation_type"] == "invoice_percentage"
    assert base["modifier_type"] == "base_fee"
    assert base["canonical_component"] == "ad_valorem"
    assert base["component_group"] == "ad_valorem"
    assert base["related_to"] is None

    assert minimum["calculation_type"] == "minimum_amount"
    assert minimum["modifier_type"] == "minimum_amount"
    assert minimum["minimum_amount"] == 5.00
    assert minimum["component_group"] == "ad_valorem"
    assert minimum["related_to"] == "ad_valorem"
    assert minimum["status"] == "calculable"


def test_accessorial_orphan_minimum_with_unmapped_basis_stays_unlinked(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[
                {
                    "name": "Taxa mínima",
                    "value": "R$ 4,99",
                    "unit": "R$",
                    "calculation_basis": "não mapeado / revisar",
                    "notes": "",
                }
            ]
        ),
    )
    fee = saved["accessorial_fees"][0]
    assert fee["calculation_type"] == "minimum_amount"
    assert fee["modifier_type"] == "minimum_amount"
    assert fee["minimum_amount"] == 4.99
    assert fee["related_to"] is None
    assert fee["status"] == "needs_review"
    assert not fee.get("calculation_basis")


def test_audit_run_applies_gris_hyphenated_minimum_via_linked_modifier(web_client):
    result = _run_single_audit(
        web_client,
        _freight_value_pricing_payload(
            header="Frete Valor",
            freight_value="0,54",
            weight_value="100,00",
            accessorial_fees=[
                {
                    "name": "GRIS-PERCENTUAL-NF",
                    "value": "0,30%",
                    "unit": "%",
                    "calculation_basis": "sobre NF",
                    "notes": "",
                },
                {
                    "name": "GRIS-PERCENTUAL-NF-MINIMO",
                    "value": "R$ 50,00",
                    "unit": "R$",
                    "calculation_basis": "não mapeado / revisar",
                    "notes": "",
                },
            ],
        ),
        audit_row=_sample_audit_row(valor_frete="150,00", peso="48", valor_nf="1000"),
    )
    assert result["status"] == "ok"
    assert result["accessorial_fees_amount"] == 50.00
    component = result["calculation_components"]["accessorial_fees"][0]
    assert component["label"] == "GRIS-PERCENTUAL-NF"
    assert component["calculated_amount"] == 3.00
    assert component["minimum_amount"] == 50.00
    assert component["minimum_applied"] is True
    assert component["amount"] == 50.00
    assert result["calculation_components"]["ignored_accessorial_fees"] == []


def test_accessorial_maximum_modifier_is_structured(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[
                {
                    "name": "TRT limite máximo",
                    "value": "R$ 100,00",
                    "unit": "R$",
                    "calculation_basis": "",
                    "notes": "teto por CTe",
                }
            ]
        ),
    )
    fee = saved["accessorial_fees"][0]
    assert fee["calculation_type"] == "maximum_amount"
    assert fee["maximum_amount"] == 100.00
    assert fee["component_group"] == "trt"
    assert fee["modifier_type"] == "maximum_amount"
    assert fee["related_to"] is None


def test_accessorial_base_fee_without_minimum_does_not_create_modifier(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[
                {
                    "name": "TDE: % sobre o valor do frete",
                    "value": "10%",
                    "unit": "%",
                    "calculation_basis": "sobre o valor do frete",
                    "notes": "",
                }
            ]
        ),
    )
    assert len(saved["accessorial_fees"]) == 1
    fee = saved["accessorial_fees"][0]
    assert fee["calculation_type"] == "freight_percentage"
    assert fee["component_group"] == "tde"
    assert fee["modifier_type"] == "base_fee"
    assert fee["related_to"] is None


def test_temp_table_save_reload_preserves_accessorial_relationship_fields(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[
                {
                    "name": "GRIS: % sobre o valor da Nota Fiscal",
                    "value": "0,30%",
                    "unit": "%",
                    "calculation_basis": "sobre o valor da Nota Fiscal",
                    "notes": "",
                },
                {
                    "name": "GRIS Mínimo R$ por Cte",
                    "value": "R$ 4,13",
                    "unit": "R$",
                    "calculation_basis": "por Cte",
                    "notes": "",
                },
            ]
        ),
    )
    resp = _post_temp_table_save(web_client, _save_payload_for_record(saved))
    assert resp.status_code == 200
    body = resp.get_json()
    fees = body["temp_table"]["accessorial_fees"]
    assert _fee_by_name(fees, "GRIS: % sobre o valor da Nota Fiscal")["component_group"] == "gris"
    assert _fee_by_name(fees, "GRIS Mínimo R$ por Cte")["related_to"] == "gris"

    path = audit_doc_service._temp_table_path(saved["temp_table_id"])
    with open(path, "r", encoding="utf-8") as handle:
        stored = json.load(handle)
    assert _fee_by_name(stored["accessorial_fees"], "GRIS Mínimo R$ por Cte")["modifier_type"] == "minimum_amount"


@pytest.mark.parametrize(
    ("raw_fee", "expected"),
    [
        (
            {
                "name": "GRIS",
                "value": "0,30%",
                "unit": "%",
                "calculation_basis": "",
                "notes": "% por nota",
                "original_text": "GRIS 0,30% % por nota",
            },
            {
                "calculation_type": "invoice_percentage",
                "canonical_component": "risk_management",
                "classification_confidence": "high",
                "status": "calculable",
                "rate": 0.003,
            },
        ),
        (
            {"name": "Seguro", "value": "0,20%", "unit": "%", "calculation_basis": "sobre NF", "notes": ""},
            {"calculation_type": "invoice_percentage", "canonical_component": "insurance"},
        ),
        (
            {
                "name": "Taxa XPTO",
                "value": "0,25%",
                "unit": "%",
                "calculation_basis": "sobre nota fiscal",
                "notes": "",
            },
            {"calculation_type": "invoice_percentage", "canonical_component": "generic_accessorial"},
        ),
        (
            {
                "name": "TAS",
                "value": "R$ 15,00",
                "unit": "R$",
                "calculation_basis": "por conhecimento",
                "notes": "",
            },
            {
                "calculation_type": "fixed_amount",
                "canonical_component": "administrative_fee",
                "amount": 15.00,
            },
        ),
        (
            {
                "name": "Pedágio",
                "value": "R$ 10,00",
                "unit": "R$",
                "calculation_basis": "por entrega",
                "notes": "",
            },
            {"calculation_type": "fixed_amount", "canonical_component": "toll", "amount": 10.00},
        ),
        (
            {"name": "TSO", "value": "5%", "unit": "%", "calculation_basis": "sobre frete", "notes": ""},
            {"calculation_type": "freight_percentage", "canonical_component": "operational_fee", "rate": 0.05},
        ),
        (
            {"name": "Mínimo", "value": "R$ 20,00", "unit": "R$", "calculation_basis": "", "notes": ""},
            {"calculation_type": "minimum_amount", "minimum_amount": 20.00},
        ),
        (
            {"name": "Teto", "value": "R$ 100,00", "unit": "R$", "calculation_basis": "", "notes": ""},
            {"calculation_type": "maximum_amount", "maximum_amount": 100.00},
        ),
    ],
)
def test_accessorial_fees_auto_classifies_generalities(web_client, raw_fee, expected):
    payload = _sample_hengst_freight_tables_payload(accessorial_fees=[raw_fee])
    saved = _apply_payload(web_client, payload)
    fee = saved["accessorial_fees"][0]
    for field, value in expected.items():
        assert fee[field] == value
    assert fee["source_block"] == "accessorial_fees"
    assert fee["name"] == raw_fee["name"]
    assert fee["value"] == raw_fee["value"]
    assert fee["unit"] == raw_fee["unit"]
    if fee.get("calculation_base_id"):
        assert fee["calculation_basis"] == fee["calculation_base_label"]
    else:
        assert fee["calculation_basis"] == (raw_fee["calculation_basis"] or None)
    assert fee["notes"] == raw_fee["notes"]


@pytest.mark.parametrize(
    ("raw_fee", "expected"),
    [
        (
            {
                "name": "GRIS",
                "value": "0,20",
                "unit": "%",
                "calculation_basis": "valor da nota fiscal",
                "notes": "",
            },
            {
                "calculation_base_id": "pct_nota_fiscal",
                "calculation_base_label": "% por nota fiscal",
                "calculation_type": "invoice_percentage",
                "audit_variable": "valor_nf",
                "operation": "percentage_of_variable",
                "operation_parameters": {},
            },
        ),
        (
            {
                "name": "GRIS: % sobre o valor da Nota Fiscal",
                "value": "0,20",
                "unit": "%",
                "calculation_basis": "sobre o valor da Nota Fiscal",
                "notes": "",
            },
            {
                "calculation_base_id": "pct_nota_fiscal",
                "calculation_type": "invoice_percentage",
                "audit_variable": "valor_nf",
                "operation": "percentage_of_variable",
            },
        ),
        (
            {
                "name": "T.S.O: % sobre o valor da Nota Fiscal",
                "value": "0,20",
                "unit": "%",
                "calculation_basis": "sobre o valor da NF",
                "notes": "",
            },
            {
                "calculation_base_id": "pct_nota_fiscal",
                "calculation_type": "invoice_percentage",
                "audit_variable": "valor_nf",
            },
        ),
        (
            {
                "name": "Cobrança de Armazenagem Seguro",
                "value": "0,20",
                "unit": "%",
                "calculation_basis": "sobre o valor de N.Fiscal",
                "notes": "",
            },
            {
                "calculation_base_id": "pct_nota_fiscal",
                "calculation_type": "invoice_percentage",
                "audit_variable": "valor_nf",
            },
        ),
        (
            {
                "name": "Redespacho Fluvial",
                "value": "0,20",
                "unit": "%",
                "calculation_basis": "S/ Valor da Nota Fiscal",
                "notes": "",
            },
            {
                "calculation_base_id": "pct_nota_fiscal",
                "calculation_type": "invoice_percentage",
                "audit_variable": "valor_nf",
            },
        ),
        (
            {
                "name": "TAS",
                "value": "R$ 15,00",
                "unit": "R$",
                "calculation_basis": "por Cte",
                "notes": "",
            },
            {
                "calculation_base_id": "por_cte",
                "calculation_type": "fixed_amount",
                "audit_variable": None,
                "operation": "fixed_amount",
            },
        ),
        (
            {
                "name": "Redespacho",
                "value": "R$ 18,00",
                "unit": "R$",
                "calculation_basis": "por conhecimento",
                "notes": "",
            },
            {
                "calculation_base_id": "por_conhecimento",
                "calculation_type": "fixed_amount",
                "operation": "fixed_amount",
            },
        ),
        (
            {
                "name": "Taxa peso",
                "value": "R$ 4,00",
                "unit": "R$",
                "calculation_basis": "para cada 100Kg ou fração",
                "notes": "",
            },
            {
                "calculation_base_id": "fracao_100kg",
                "calculation_type": "weight_fraction",
                "audit_variable": "peso",
                "operation": "ceil_fraction",
                "operation_parameters": {"fraction_size": 100},
            },
        ),
        (
            {
                "name": "Taxa XPTO",
                "value": "0,20",
                "unit": "%",
                "calculation_basis": "valor da nota fiscal",
                "notes": "",
            },
            {
                "calculation_base_id": "pct_nota_fiscal",
                "calculation_type": "invoice_percentage",
                "audit_variable": "valor_nf",
                "operation": "percentage_of_variable",
            },
        ),
        (
            {
                "name": "Taxa XPTO",
                "value": "0,25",
                "unit": "%",
                "calculation_basis": "sobre nota fiscal",
                "notes": "",
            },
            {
                "calculation_base_id": "pct_nota_fiscal",
                "calculation_type": "invoice_percentage",
                "audit_variable": "valor_nf",
                "operation": "percentage_of_variable",
            },
        ),
        (
            {
                "name": "Taxa XPTO",
                "value": "0,25%",
                "unit": None,
                "calculation_basis": "sobre nota fiscal",
                "notes": "",
            },
            {
                "calculation_base_id": "pct_nota_fiscal",
                "calculation_type": "invoice_percentage",
                "audit_variable": "valor_nf",
                "operation": "percentage_of_variable",
            },
        ),
        (
            {
                "name": "Seguro",
                "value": "0,20%",
                "unit": "%",
                "calculation_basis": "sobre NF",
                "notes": "",
            },
            {
                "calculation_base_id": "pct_nota_fiscal",
                "calculation_type": "invoice_percentage",
                "audit_variable": "valor_nf",
            },
        ),
    ],
)
def test_accessorial_fees_resolve_configured_calculation_bases(web_client, raw_fee, expected):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload(accessorial_fees=[raw_fee]))
    fee = saved["accessorial_fees"][0]
    for field, value in expected.items():
        assert fee.get(field) == value
    assert fee["classification_source"] == "configured_calculation_base"
    assert fee["source_block"] == "accessorial_fees"
    assert fee["name"] == raw_fee["name"]


def test_accessorial_fees_accepts_valid_calculation_base_id_and_preserves_raw_basis(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[
                {
                    "name": "GRIS",
                    "value": "0,20",
                    "unit": "%",
                    "calculation_basis": "texto do modelo",
                    "calculation_base_id": "pct_nota_fiscal",
                    "calculation_base_label": "rótulo não confiável",
                    "raw_calculation_basis": "sobre o valor da Nota Fiscal",
                    "notes": "",
                }
            ]
        ),
    )
    fee = saved["accessorial_fees"][0]
    assert fee["calculation_base_id"] == "pct_nota_fiscal"
    assert fee["calculation_base_label"] == "% por nota fiscal"
    assert fee["calculation_basis"] == "% por nota fiscal"
    assert fee["calculation_type"] == "invoice_percentage"
    assert fee["audit_variable"] == "valor_nf"
    assert fee["operation"] == "percentage_of_variable"
    assert fee["operation_parameters"] == {}
    assert fee["classification_source"] == "configured_calculation_base"
    assert fee["raw_calculation_basis"] == "sobre o valor da Nota Fiscal"


def test_accessorial_fees_rejects_invalid_calculation_base_id_without_trusting_payload(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[
                {
                    "name": "Taxa XPTO",
                    "value": "0,20",
                    "unit": "%",
                    "calculation_basis": "% por nota fiscal",
                    "calculation_base_id": "base_inexistente",
                    "calculation_base_label": "% por nota fiscal",
                    "raw_calculation_basis": "sobre NF",
                    "notes": "",
                }
            ]
        ),
    )
    fee = saved["accessorial_fees"][0]
    assert fee["calculation_base_id"] is None
    assert fee.get("calculation_base_label") is None
    assert fee["calculation_basis"] == "não mapeado / revisar"
    assert fee["classification_source"] == "unmapped_calculation_base"
    assert fee["raw_calculation_basis"] == "sobre NF"


def test_accessorial_fees_known_name_with_unconfigured_basis_uses_legacy_classifier(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[
                {
                    "name": "GRIS",
                    "value": "0,20%",
                    "unit": "%",
                    "calculation_basis": "base inexistente",
                    "notes": "",
                }
            ]
        ),
    )
    fee = saved["accessorial_fees"][0]
    assert fee["calculation_base_id"] is None
    assert fee["classification_source"] == "legacy_classifier"
    assert fee["calculation_type"] == "invoice_percentage"
    assert fee["canonical_component"] == "risk_management"


def test_accessorial_fees_unit_incompativel_nao_resolve_base_configurada(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[
                {
                    "name": "GRIS",
                    "value": "R$ 10,00",
                    "unit": "R$",
                    "calculation_basis": "valor da nota fiscal",
                    "notes": "",
                }
            ]
        ),
    )
    fee = saved["accessorial_fees"][0]
    assert fee["calculation_base_id"] is None
    assert fee["classification_source"] == "legacy_classifier"


def test_accessorial_fees_ambiguous_configured_base_falls_back_to_legacy(
    web_client,
    monkeypatch,
):
    ambiguous_bases = [
        {
            "id": "pct_nota_fiscal",
            "label": "% por nota fiscal",
            "aliases": ["valor da nota fiscal"],
            "unit": "%",
            "calculation_type": "invoice_percentage",
            "audit_variable": "valor_nf",
            "operation": "percentage_of_variable",
            "parameters": {},
            "allows_minimum": True,
            "allows_maximum": True,
            "requires_structured_condition": False,
            "is_active": True,
            "display_order": 10,
        },
        {
            "id": "pct_nota_fiscal_dup",
            "label": "% por nota fiscal duplicada",
            "aliases": ["valor da nota fiscal"],
            "unit": "%",
            "calculation_type": "invoice_percentage",
            "audit_variable": "valor_nf",
            "operation": "percentage_of_variable",
            "parameters": {},
            "allows_minimum": True,
            "allows_maximum": True,
            "requires_structured_condition": False,
            "is_active": True,
            "display_order": 20,
        },
    ]
    _patch_audit_cfg(monkeypatch, calculation_bases=ambiguous_bases)

    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[
                {
                    "name": "Taxa XPTO",
                    "value": "0,20%",
                    "unit": "%",
                    "calculation_basis": "valor da nota fiscal",
                    "notes": "",
                }
            ]
        ),
    )
    fee = saved["accessorial_fees"][0]
    assert fee["calculation_base_id"] is None
    assert fee["classification_source"] == "legacy_classifier"
    assert fee["classification_warning"] == "ambiguous_calculation_base"
    assert fee["calculation_type"] == "invoice_percentage"


def test_accessorial_fees_auto_classifies_textual_condition(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[
                {
                    "name": "Taxa especial",
                    "value": "sob consulta",
                    "unit": "",
                    "calculation_basis": "",
                    "notes": "apenas entrega agendada",
                }
            ]
        ),
    )
    fee = saved["accessorial_fees"][0]
    assert fee["calculation_type"] == "conditional"
    assert fee["status"] == "unsupported"
    assert fee["classification_confidence"] == "medium"
    assert fee["canonical_component"] == "generic_accessorial"
    assert fee["conditions"] == "apenas entrega agendada sob consulta"
    assert fee["unsupported_reason"] == "textual_condition"


def test_accessorial_fees_auto_classifies_ambiguous_case_as_unknown(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[
                {
                    "name": "Taxa XPTO",
                    "value": "conforme tabela",
                    "unit": "",
                    "calculation_basis": "",
                    "notes": "",
                }
            ]
        ),
    )
    fee = saved["accessorial_fees"][0]
    assert fee["calculation_type"] == "unknown"
    assert fee["status"] == "unknown"
    assert fee["classification_confidence"] == "low"
    assert fee["canonical_component"] == "generic_accessorial"


@pytest.mark.parametrize(
    ("name", "expected_component"),
    [
        ("Sob. NF", "generic_accessorial"),
        ("S/NF", "generic_accessorial"),
        ("F.V. %", "freight_value"),
        ("FV %", "freight_value"),
    ],
)
def test_accessorial_fees_refines_invoice_percentage_aliases(web_client, name, expected_component):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[
                {
                    "name": name,
                    "value": "0,10",
                    "unit": "",
                    "calculation_basis": "",
                    "notes": "",
                }
            ]
        ),
    )
    fee = saved["accessorial_fees"][0]
    assert fee["calculation_type"] == "invoice_percentage"
    assert fee["canonical_component"] == expected_component
    assert fee["classification_confidence"] == "high"
    assert fee["status"] == "calculable"
    assert fee["rate"] == 0.001


def test_accessorial_fees_rebaixam_regra_composta_gris(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[
                {
                    "name": "GRIS",
                    "value": "0,15%; 0,30% RJ mínimo R$4,13",
                    "unit": "%/R$",
                    "calculation_basis": "",
                    "notes": "",
                }
            ]
        ),
    )
    fee = saved["accessorial_fees"][0]
    assert fee["canonical_component"] == "risk_management"
    assert fee["status"] in {"needs_review", "unsupported"}
    assert fee["status"] != "calculable"
    assert fee["classification_confidence"] == "medium"
    assert fee["unsupported_reason"] == "compound_accessorial_rule"
    assert fee["minimum_amount"] == 4.13
    assert "RJ" in fee["conditions"]


def test_accessorial_fees_do_not_capture_note_number_as_amount(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[
                {
                    "name": "Dedicado Toco/Truck/Cavalo + Carreta",
                    "value": "",
                    "unit": "",
                    "calculation_basis": "",
                    "notes": "após o 3º dia",
                }
            ]
        ),
    )
    fee = saved["accessorial_fees"][0]
    assert "amount" not in fee
    assert fee["status"] == "unsupported"
    assert fee["unsupported_reason"] == "missing_monetary_amount"
    assert fee["conditions"] == "após o 3º dia"


def test_accessorial_fees_classifies_taxa_minima_with_or_without_amount(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[
                {
                    "name": "Coleta: (Taxa Mínima)",
                    "value": "R$ 25,00",
                    "unit": "R$",
                    "calculation_basis": "",
                    "notes": "",
                },
                {
                    "name": "Coleta: (Taxa Mínima)",
                    "value": "",
                    "unit": "",
                    "calculation_basis": "",
                    "notes": "",
                },
            ]
        ),
    )
    with_amount, without_amount = saved["accessorial_fees"]
    assert with_amount["calculation_type"] == "minimum_amount"
    assert with_amount["status"] == "needs_review"
    assert with_amount["minimum_amount"] == 25.00
    assert without_amount["calculation_type"] == "minimum_amount"
    assert without_amount["status"] == "needs_review"
    assert "minimum_amount" not in without_amount


def test_accessorial_fees_classifies_tde_embedded_amount_as_review(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[
                {
                    "name": "TDE 15.00",
                    "value": "",
                    "unit": "",
                    "calculation_basis": "",
                    "notes": "",
                }
            ]
        ),
    )
    fee = saved["accessorial_fees"][0]
    assert fee["calculation_type"] == "fixed_amount"
    assert fee["canonical_component"] == "generic_accessorial"
    assert fee["amount"] == 15.00
    assert fee["status"] == "needs_review"
    assert fee["unsupported_reason"] == "missing_application_basis"


@pytest.mark.parametrize(
    "raw_fee",
    [
        {
            "name": "Estadia",
            "value": "R$ 100,00",
            "unit": "R$",
            "calculation_basis": "por dia",
            "notes": "",
        },
        {
            "name": "Paletização",
            "value": "R$ 10,00",
            "unit": "R$",
            "calculation_basis": "por pallet",
            "notes": "",
        },
    ],
)
def test_accessorial_fees_classifies_operational_rates_as_review_only(web_client, raw_fee):
    saved = _apply_payload(web_client, _sample_hengst_freight_tables_payload(accessorial_fees=[raw_fee]))
    fee = saved["accessorial_fees"][0]
    assert fee["calculation_type"] == "fixed_amount"
    assert fee["classification_confidence"] == "medium"
    assert fee["status"] == "needs_review"
    assert fee["amount"] == (100.00 if raw_fee["name"] == "Estadia" else 10.00)
    assert fee["unsupported_reason"] == "operational_unit_rate"


def test_accessorial_fees_classifies_embedded_seguro_sobre_nf(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[
                {
                    "name": "Cobrança de Armazenagem Seguro 0,20% sobre NF",
                    "value": "",
                    "unit": "",
                    "calculation_basis": "",
                    "notes": "",
                }
            ]
        ),
    )
    fee = saved["accessorial_fees"][0]
    assert fee["calculation_type"] == "invoice_percentage"
    assert fee["canonical_component"] == "insurance"
    assert fee["classification_confidence"] == "high"
    assert fee["status"] == "calculable"
    assert fee["rate"] == 0.002


def test_accessorial_fees_sanitize_legacy_pedagio_geral_calculable(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[
                {
                    "name": "Pedágio geral",
                    "value": "conforme tabela",
                    "unit": "",
                    "calculation_basis": "",
                    "notes": "",
                    "calculation_type": "invoice_percentage",
                    "canonical_component": "toll",
                    "classification_confidence": "high",
                    "status": "calculable",
                    "rate": 0.1,
                }
            ]
        ),
    )
    fee = saved["accessorial_fees"][0]
    assert fee["calculation_type"] == "unknown"
    assert fee["canonical_component"] == "toll"
    assert fee["classification_confidence"] == "low"
    assert fee["status"] == "unknown"
    assert "rate" not in fee


def test_accessorial_fees_sanitize_legacy_compound_gris_calculable(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[
                {
                    "name": "GRIS",
                    "value": "0,15%; 0,30% RJ mínimo R$4,13",
                    "unit": "%/R$",
                    "calculation_basis": "valor da NF",
                    "notes": "",
                    "calculation_type": "invoice_percentage",
                    "canonical_component": "risk_management",
                    "classification_confidence": "high",
                    "status": "calculable",
                    "rate": 0.0015,
                }
            ]
        ),
    )
    fee = saved["accessorial_fees"][0]
    assert fee["calculation_type"] == "invoice_percentage"
    assert fee["canonical_component"] == "risk_management"
    assert fee["status"] == "needs_review"
    assert fee["classification_confidence"] == "medium"
    assert fee["unsupported_reason"] == "compound_accessorial_rule"
    assert fee["minimum_amount"] == 4.13


def test_accessorial_fees_sanitize_legacy_operational_calculable(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[
                {
                    "name": "Estadia Kg/dia",
                    "value": "R$ 3,00",
                    "unit": "R$",
                    "calculation_basis": "kg/dia",
                    "notes": "",
                    "calculation_type": "fixed_amount",
                    "canonical_component": "generic_accessorial",
                    "classification_confidence": "high",
                    "status": "calculable",
                    "amount": 3.00,
                }
            ]
        ),
    )
    fee = saved["accessorial_fees"][0]
    assert fee["calculation_type"] == "fixed_amount"
    assert fee["status"] == "needs_review"
    assert fee["classification_confidence"] == "medium"
    assert fee["unsupported_reason"] == "operational_unit_rate"
    assert fee["amount"] == 3.00


def test_accessorial_fees_taxa_xpto_clear_formula_requires_review(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[
                {
                    "name": "Taxa XPTO",
                    "value": "0,25%",
                    "unit": "%",
                    "calculation_basis": "sobre nota fiscal",
                    "notes": "",
                }
            ]
        ),
    )
    fee = saved["accessorial_fees"][0]
    assert fee["calculation_type"] == "invoice_percentage"
    assert fee["canonical_component"] == "generic_accessorial"
    assert fee["classification_confidence"] == "high"
    assert fee["status"] == "calculable"
    assert fee["rate"] == 0.0025


def test_accessorial_fees_conservative_reclassification_persists_on_save_reload(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[
                {
                    "name": "Pedágio geral",
                    "value": "conforme lei",
                    "unit": "",
                    "calculation_basis": "",
                    "notes": "",
                    "calculation_type": "invoice_percentage",
                    "canonical_component": "toll",
                    "classification_confidence": "high",
                    "status": "calculable",
                    "rate": 0.01,
                }
            ]
        ),
    )
    fee = saved["accessorial_fees"][0]
    assert fee["calculation_type"] == "unknown"
    assert fee["status"] == "unknown"
    assert "rate" not in fee

    resp = _post_temp_table_save(web_client, _save_payload_for_record(saved))
    assert resp.status_code == 200
    public_fee = resp.get_json()["temp_table"]["accessorial_fees"][0]
    assert public_fee == fee

    reloaded = audit_doc_service.load_temp_table_record(saved["temp_table_id"], ttl_hours=24)
    assert reloaded is not None
    assert reloaded["accessorial_fees"][0] == fee


def test_accessorial_fees_auto_classification_persists_on_save_reload(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[
                {
                    "name": "TAS",
                    "value": "R$ 15,00",
                    "unit": "R$",
                    "calculation_basis": "por CTe",
                    "notes": "",
                }
            ]
        ),
    )
    fee = saved["accessorial_fees"][0]
    assert fee["calculation_type"] == "fixed_amount"
    assert fee["canonical_component"] == "administrative_fee"
    assert fee["amount"] == 15.00

    resp = _post_temp_table_save(web_client, _save_payload_for_record(saved))
    assert resp.status_code == 200
    public_fee = resp.get_json()["temp_table"]["accessorial_fees"][0]
    assert public_fee == fee

    reloaded = audit_doc_service.load_temp_table_record(saved["temp_table_id"], ttl_hours=24)
    assert reloaded is not None
    assert reloaded["accessorial_fees"][0] == fee


def test_temp_table_save_reload_preserves_accessorial_derived_fields(web_client):
    saved = _apply_payload(
        web_client,
        _sample_hengst_freight_tables_payload(
            accessorial_fees=[
                {
                    "name": "GRIS",
                    "value": "0,30%",
                    "unit": "%",
                    "calculation_basis": "valor_nf",
                    "notes": "% por nota",
                    "scope": "global",
                    "calculation_type": "invoice_percentage",
                    "canonical_component": "risk_management",
                    "classification_confidence": "high",
                    "status": "calculable",
                }
            ]
        ),
    )
    edited_fees = list(saved["accessorial_fees"])
    edited_fees[0] = dict(edited_fees[0], notes="validado manualmente")
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
    public_fee = resp.get_json()["temp_table"]["accessorial_fees"][0]
    assert public_fee["calculation_type"] == "invoice_percentage"
    assert public_fee["canonical_component"] == "risk_management"
    assert public_fee["classification_confidence"] == "high"
    assert public_fee["status"] == "calculable"

    reloaded = audit_doc_service.load_temp_table_record(saved["temp_table_id"], ttl_hours=24)
    assert reloaded is not None
    reloaded_fee = reloaded["accessorial_fees"][0]
    assert reloaded_fee == public_fee
    path = audit_doc_service._temp_table_path(saved["temp_table_id"])
    with open(path, "r", encoding="utf-8") as handle:
        stored = json.load(handle)
    assert stored["accessorial_fees"][0] == public_fee


def test_accessorial_fees_legacy_payload_stays_compatible(web_client):
    payload = _sample_hengst_freight_tables_payload(
        accessorial_fees=[
            {
                "name": "Pedágio",
                "value": "10,00",
                "unit": "R$",
                "calculation_basis": "por entrega",
                "notes": "",
                "scope": "global",
            }
        ]
    )
    saved = _apply_payload(web_client, payload)
    fee = saved["accessorial_fees"][0]
    assert fee["name"] == "Pedágio"
    assert fee["value"] == "10,00"
    assert fee["unit"] == "R$"
    assert fee["calculation_basis"] == "por entrega"
    assert fee["notes"] == ""
    assert fee["scope"] == "global"
    assert fee["calculation_type"] == "fixed_amount"
    assert fee["canonical_component"] == "toll"
    assert fee["status"] == "calculable"
    assert fee["amount"] == 10.00


def test_accessorial_fees_sanitizes_legacy_optional_derived_fields(web_client):
    payload = _sample_hengst_freight_tables_payload(
        accessorial_fees=[
            {
                "name": "Taxa condicional",
                "value": "sob consulta",
                "unit": "",
                "calculation_basis": "condição comercial",
                "notes": "",
                "scope": "global",
                "rate": "0,30%",
                "amount": "15,00",
                "minimum_amount": "10,00",
                "maximum_amount": "50,00",
                "conditions": {"when": "entrega especial"},
                "unsupported_reason": "depende de evento operacional",
                "source_block": "accessorial_fees",
                "original_text": "Taxa condicional conforme ocorrência",
                "evidence_ref": "p. 2",
            }
        ]
    )
    saved = _apply_payload(web_client, payload)
    fee = saved["accessorial_fees"][0]
    assert "rate" not in fee
    assert "amount" not in fee
    assert "minimum_amount" not in fee
    assert "maximum_amount" not in fee
    assert fee["calculation_type"] == "conditional"
    assert fee["status"] == "unsupported"
    assert fee["conditions"] == "global Taxa condicional conforme ocorrência sob consulta"
    assert fee["unsupported_reason"] == "textual_condition"
    assert fee["source_block"] == "accessorial_fees"
    assert fee["original_text"] == "Taxa condicional conforme ocorrência"
    assert fee["evidence_ref"] == "p. 2"


def test_accessorial_fees_invalid_derived_values_fall_back_safely(web_client):
    payload = _sample_hengst_freight_tables_payload(
        accessorial_fees=[
            {
                "name": "Taxa desconhecida",
                "value": "abc",
                "unit": "",
                "calculation_basis": "",
                "notes": "",
                "scope": "global",
                "calculation_type": "percentual_novo",
                "canonical_component": "componente_novo",
                "classification_confidence": "certeza",
                "status": "pronto",
            }
        ]
    )
    saved = _apply_payload(web_client, payload)
    fee = saved["accessorial_fees"][0]
    assert fee["calculation_type"] == "unknown"
    assert fee["canonical_component"] == "generic_accessorial"
    assert fee["classification_confidence"] == "low"
    assert fee["status"] == "unknown"


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


def _freight_value_pricing_payload(
    *,
    header: str = "Frete Valor %",
    freight_value: str = "0,1%",
    weight_value: str = "100,00",
    accessorial_fees: list[dict] | None = None,
) -> dict:
    return {
        "status": "needs_review",
        "freight_tables": [
            {
                "table_title": "Tabela por região",
                "table_type": "weight_range_table",
                "columns": ["Região de frete", "Até 50 kg", header],
                "rows": [
                    {
                        "Região de frete": "SP-Interior 1",
                        "Até 50 kg": weight_value,
                        header: freight_value,
                    }
                ],
            }
        ],
        "freight_routes": [],
        "freight_values": [{"label": "Frete Valor fora da regra", "value": "999", "unit": "%", "notes": ""}],
        "accessorial_fees": accessorial_fees or [],
        "weight_ranges": [],
        "reading_alerts": [],
        "evidence_refs": [],
    }


def _run_single_audit(web_client, pricing_payload: dict, *, audit_row=None) -> dict:
    _apply_payload(web_client, pricing_payload)
    coverage = make_csv([["UF destino", "Cidade destino", "Região de frete"], ["SP", "Campinas", "SP-Interior 1"]])
    assert _post_coverage_upload(web_client, "coverage.csv", coverage, "text/csv").status_code == 200
    row = audit_row or _sample_audit_row(valor_frete="101,00", peso="48", valor_nf="1000,00")
    assert _post_audit_upload(
        web_client,
        "auditado.xlsx",
        _sample_audit_xlsx(row),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ).status_code == 200
    resp = _post_audit_run(web_client)
    assert resp.status_code == 200
    return resp.get_json()["temp_table"]["audit_batch"]["results"][0]


def _accessorial_fee(name: str, value: str, *, unit: str | None = None, calculation_basis: str | None = None) -> dict:
    return {"name": name, "value": value, "unit": unit, "calculation_basis": calculation_basis, "notes": ""}


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


def test_build_freight_pricing_index_detects_freight_value_percent_column():
    index = audit_doc_service.build_freight_pricing_index(
        _freight_value_pricing_payload(header="Frete Valor %", freight_value="0,54")
    )
    freight_value = index["SP-Interior 1"]["freight_value"]
    assert freight_value["rate"] == 0.0054
    assert freight_value["source_column"] == "Frete Valor %"
    assert freight_value["source_value"] == "0,54"
    assert freight_value["calculation_base"] == "invoice_value"


@pytest.mark.parametrize(
    "header",
    [
        "Frete Valor (%)",
        "% NF",
        "% Nota",
        "% Nota Fiscal",
        "% Valor NF",
        "% Sobre NF",
        "Percentual NF",
        "Perc. NF",
        "Sobre NF",
        "Sob. NF",
        "S/NF",
        "FV %",
        "F.V. %",
        "Ad Valorem %",
        "freight_value_pct",
    ],
)
def test_build_freight_pricing_index_accepts_safe_freight_value_aliases(header):
    index = audit_doc_service.build_freight_pricing_index(
        _freight_value_pricing_payload(header=header, freight_value="1,65")
    )
    freight_value = index["SP-Interior 1"]["freight_value"]
    assert freight_value["rate"] == 0.0165
    assert freight_value["source_column"] == header


@pytest.mark.parametrize(
    "header",
    ["Valor Frete", "Seguro/Ad Valorem", "Ad Valorem", "Frete Valor", "FV", "Taxa NF", "Seguro"],
)
def test_build_freight_pricing_index_ignores_ambiguous_freight_value_aliases(header):
    index = audit_doc_service.build_freight_pricing_index(
        _freight_value_pricing_payload(header=header, freight_value="0,54")
    )
    assert index["SP-Interior 1"].get("freight_value") is None


def test_region_column_recognizes_uf_cidades():
    assert audit_doc_service._is_region_column("UF Cidades") is True


def test_build_freight_pricing_index_builds_city_destination_keys():
    index = audit_doc_service.build_freight_pricing_index(_sample_city_pricing_payload())
    assert index["Palmas"]["pricing_type"] == "range_plus_excess_per_kg"
    assert index["PALMAS"]["region"] == "Palmas"
    assert index["TO|PALMAS"]["region"] == "Palmas"


def _multi_uf_region_pricing_payload(*, region_label: str, rows: list[dict]) -> dict:
    return {
        "status": "needs_review",
        "freight_tables": [
            {
                "table_title": "Tabela multi-UF",
                "table_type": "weight_range_table",
                "columns": ["UF", "Região de frete", "Até 30 kg"],
                "rows": rows,
            }
        ],
        "freight_routes": [],
        "freight_values": [],
        "accessorial_fees": [],
        "weight_ranges": [],
        "reading_alerts": [],
        "evidence_refs": [],
    }


def _mono_uf_region_pricing_payload(*, region_label: str, value: str = "50,00") -> dict:
    return {
        "status": "needs_review",
        "freight_tables": [
            {
                "table_title": "Tabela mono-UF",
                "table_type": "weight_range_table",
                "columns": ["Região de frete", "Até 30 kg"],
                "rows": [{"Região de frete": region_label, "Até 30 kg": value}],
            }
        ],
        "freight_routes": [],
        "freight_values": [],
        "accessorial_fees": [],
        "weight_ranges": [],
        "reading_alerts": [],
        "evidence_refs": [],
    }


def test_find_pricing_rule_falls_back_from_coverage_region_to_city():
    pricing_index = audit_doc_service.build_freight_pricing_index(_sample_city_pricing_payload())
    rule = audit_doc_service._find_pricing_rule(
        pricing_index,
        "TO - Capital",
        "TO",
        "Palmas",
    )
    assert rule is pricing_index["TO|PALMAS"]


def test_find_pricing_rule_uses_uf_region_when_generic_capital_is_unsupported():
    payload = _multi_uf_region_pricing_payload(
        region_label="Capital",
        rows=[
            {"UF": "PR", "Região de frete": "Capital", "Até 30 kg": "50,00"},
            {"UF": "RS", "Região de frete": "Capital", "Até 30 kg": "60,00"},
        ],
    )
    pricing_index = audit_doc_service.build_freight_pricing_index(payload)
    assert pricing_index["Capital"]["pricing_type"] == "unsupported_pricing_model"
    match = audit_doc_service._find_pricing_rule_match(pricing_index, "Capital", "PR", "Curitiba")
    assert match is not None
    rule, lookup_kind, lookup_key = match
    assert lookup_key == "PR|CAPITAL"
    assert rule["pricing_type"] == "fixed_range"
    assert rule["brackets"][0]["value"] == 50.0
    assert lookup_kind == "freight_region"


def test_find_pricing_rule_uses_uf_region_when_generic_interior_is_unsupported():
    payload = _multi_uf_region_pricing_payload(
        region_label="Interior",
        rows=[
            {"UF": "ES", "Região de frete": "Interior", "Até 30 kg": "70,00"},
            {"UF": "RS", "Região de frete": "Interior", "Até 30 kg": "80,00"},
        ],
    )
    pricing_index = audit_doc_service.build_freight_pricing_index(payload)
    assert pricing_index["Interior"]["pricing_type"] == "unsupported_pricing_model"
    match = audit_doc_service._find_pricing_rule_match(pricing_index, "Interior", "ES", "Castelo")
    assert match is not None
    rule, lookup_kind, lookup_key = match
    assert lookup_key == "ES|INTERIOR"
    assert rule["pricing_type"] == "fixed_range"
    assert rule["brackets"][0]["value"] == 70.0
    assert lookup_kind == "freight_region"


def test_find_pricing_rule_keeps_explicit_region_without_uf_composition():
    pricing_index = audit_doc_service.build_freight_pricing_index(_sample_pricing_payload())
    match = audit_doc_service._find_pricing_rule_match(pricing_index, "SP-Interior 1", "SP", "Campinas")
    assert match is not None
    rule, lookup_kind, lookup_key = match
    assert lookup_key == "SP-Interior 1"
    assert rule["pricing_type"] == "range_plus_excess_per_kg"
    assert lookup_kind == "freight_region"


def test_find_pricing_rule_mono_uf_region_still_works():
    payload = _mono_uf_region_pricing_payload(region_label="Capital", value="55,00")
    pricing_index = audit_doc_service.build_freight_pricing_index(payload)
    assert pricing_index["Capital"]["pricing_type"] == "fixed_range"
    match = audit_doc_service._find_pricing_rule_match(pricing_index, "Capital", "PR", "Curitiba")
    assert match is not None
    rule, lookup_kind, lookup_key = match
    assert lookup_key == "Capital"
    assert rule["brackets"][0]["value"] == 55.0
    assert lookup_kind == "freight_region"


def test_find_pricing_rule_returns_none_when_no_rule_exists():
    pricing_index = audit_doc_service.build_freight_pricing_index(_sample_pricing_payload())
    assert audit_doc_service._find_pricing_rule_match(pricing_index, "SP-Interior 9", "SP", "Campinas") is None


def test_find_pricing_rule_returns_unsupported_when_only_unsupported_exists():
    pricing_index = {
        "Capital": audit_doc_service._make_unsupported_rule("Capital", "Tabela", "Colisão"),
    }
    match = audit_doc_service._find_pricing_rule_match(pricing_index, "Capital", "PR", "Curitiba")
    assert match is not None
    rule, lookup_kind, lookup_key = match
    assert lookup_key == "Capital"
    assert rule["pricing_type"] == "unsupported_pricing_model"
    assert lookup_kind == "freight_region"


def _freight_route_region_pricing_payload(
    *,
    destination: str,
    weight_30: str = "50,00",
    extra_routes: list[dict] | None = None,
) -> dict:
    routes = [
        {
            "origin": "SP",
            "destination": destination,
            "weight_30": weight_30,
        }
    ]
    if extra_routes:
        routes.extend(extra_routes)
    return {
        "status": "needs_review",
        "freight_tables": [],
        "freight_routes": routes,
        "freight_values": [],
        "accessorial_fees": [],
        "weight_ranges": [],
        "reading_alerts": [],
        "evidence_refs": [],
    }


def test_freight_route_capital_pr_registers_contextual_key_and_matches():
    payload = _freight_route_region_pricing_payload(destination="Capital - PR", weight_30="50,00")
    pricing_index = audit_doc_service.build_freight_pricing_index(payload)
    assert "Capital - PR" in pricing_index
    assert "PR|CAPITAL" in pricing_index
    assert pricing_index["PR|CAPITAL"]["region"] == "Capital - PR"
    match = audit_doc_service._find_pricing_rule_match(pricing_index, "Capital", "PR", "Curitiba")
    assert match is not None
    rule, lookup_kind, lookup_key = match
    assert lookup_key == "PR|CAPITAL"
    assert rule["pricing_type"] == "fixed_range"
    assert rule["brackets"][0]["value"] == 50.0
    assert lookup_kind == "freight_region"


def test_freight_route_interior_es_registers_contextual_key_and_matches():
    payload = _freight_route_region_pricing_payload(destination="Interior - ES", weight_30="70,00")
    pricing_index = audit_doc_service.build_freight_pricing_index(payload)
    assert "Interior - ES" in pricing_index
    assert "ES|INTERIOR" in pricing_index
    match = audit_doc_service._find_pricing_rule_match(pricing_index, "Interior", "ES", "Castelo")
    assert match is not None
    rule, lookup_kind, lookup_key = match
    assert lookup_key == "ES|INTERIOR"
    assert rule["pricing_type"] == "fixed_range"
    assert rule["brackets"][0]["value"] == 70.0
    assert lookup_kind == "freight_region"


def _homolog_freight_route_payload(
    *,
    origin: str | None,
    destination: str,
    notes: str = "",
    weight_30: str = "50,00",
    extra_routes: list[dict] | None = None,
    **route_overrides,
) -> dict:
    route = {
        "origin": origin,
        "destination": destination,
        "notes": notes,
        "weight_30": weight_30,
    }
    route.update(route_overrides)
    routes = [route]
    if extra_routes:
        routes.extend(extra_routes)
    return {
        "status": "needs_review",
        "freight_tables": [],
        "freight_routes": routes,
        "freight_values": [],
        "accessorial_fees": [],
        "weight_ranges": [],
        "reading_alerts": [],
        "evidence_refs": [],
    }


def _homolog_capital_pr_freight_route_payload(**overrides) -> dict:
    route_overrides = {
        "weight_30": "41,03",
        "weight_50": "45,46",
        "weight_70": "54,33",
        "weight_100": "57,66",
        "freight_weight_kg": "0,45",
        **overrides,
    }
    return _homolog_freight_route_payload(
        origin="Capital",
        destination="PR",
        notes="Região: Capital",
        **route_overrides,
    )


def test_freight_route_homolog_capital_pr_registers_contextual_key_and_matches():
    payload = _homolog_capital_pr_freight_route_payload()
    pricing_index = audit_doc_service.build_freight_pricing_index(payload)
    assert "Capital" in pricing_index
    assert "PR|CAPITAL" in pricing_index
    assert pricing_index["PR|CAPITAL"]["region"] == "Capital"
    match = audit_doc_service._find_pricing_rule_match(pricing_index, "Capital", "PR", "Curitiba")
    assert match is not None
    rule, lookup_kind, lookup_key = match
    assert lookup_key in {"Capital", "PR|CAPITAL"}
    assert rule["pricing_type"] == "range_plus_excess_per_kg"
    assert rule["brackets"][0]["value"] == 41.03
    assert rule["excess"]["rate_per_kg"] == 0.45
    assert lookup_kind == "freight_region"
    calculated = audit_doc_service.calculate_weight_freight(20, rule)
    assert calculated["expected_freight"] == 41.03


def _homolog_real_capital_pr_freight_route_payload(**overrides) -> dict:
    route_overrides = {
        "weight_30": "41.03",
        "weight_50": "45.46",
        "weight_70": "54.33",
        "weight_100": "57.66",
        "freight_weight_kg": "0.45",
        **overrides,
    }
    return _homolog_freight_route_payload(
        origin=None,
        destination="PR",
        notes="Rota classificada como 'Capital'.",
        **route_overrides,
    )


def test_freight_route_homolog_real_payload_null_origin_pr_notes_capital():
    payload = _homolog_real_capital_pr_freight_route_payload()
    pricing_index = audit_doc_service.build_freight_pricing_index(payload)
    assert "Capital" in pricing_index
    assert "PR|CAPITAL" in pricing_index
    assert pricing_index["PR|CAPITAL"]["region"] == "Capital"
    match = audit_doc_service._find_pricing_rule_match(pricing_index, "Capital", "PR", "Curitiba")
    assert match is not None
    rule, lookup_kind, lookup_key = match
    assert lookup_key in {"Capital", "PR|CAPITAL"}
    assert rule["pricing_type"] == "range_plus_excess_per_kg"
    assert rule["brackets"][0]["value"] == 41.03
    assert rule["excess"]["rate_per_kg"] == 0.45
    assert lookup_kind == "freight_region"
    calculated = audit_doc_service.calculate_weight_freight(20, rule)
    assert calculated["expected_freight"] == 41.03


def test_freight_route_homolog_null_origin_es_interior_via_notes():
    payload = _homolog_freight_route_payload(
        origin=None,
        destination="ES",
        notes="Rota classificada como 'Interior'.",
        weight_30="70,00",
    )
    pricing_index = audit_doc_service.build_freight_pricing_index(payload)
    assert "Interior" in pricing_index
    assert "ES|INTERIOR" in pricing_index
    match = audit_doc_service._find_pricing_rule_match(pricing_index, "Interior", "ES", "Castelo")
    assert match is not None
    rule, lookup_kind, lookup_key = match
    assert lookup_key in {"Interior", "ES|INTERIOR"}
    assert rule["pricing_type"] == "fixed_range"
    assert rule["brackets"][0]["value"] == 70.0
    assert lookup_kind == "freight_region"


def test_freight_route_homolog_null_origin_without_notes_does_not_invent_region():
    payload = _homolog_freight_route_payload(
        origin=None,
        destination="PR",
        notes="",
        weight_30="41,03",
        weight_50="45,46",
        weight_70="54,33",
        weight_100="57,66",
        freight_weight_kg="0,45",
    )
    pricing_index = audit_doc_service.build_freight_pricing_index(payload)
    assert "PR|CAPITAL" not in pricing_index
    assert audit_doc_service._find_pricing_rule_match(pricing_index, "Capital", "PR", "Curitiba") is None


def test_freight_route_homolog_null_origin_notes_without_quotes_preserves_current_long_text_behavior():
    notes = "Rota classificada como Capital atendimento especial"
    assert audit_doc_service._region_label_from_freight_route_notes(notes) == "Capital atendimento especial"


def test_audit_run_freight_route_homolog_real_null_origin_pr_resolves_coverage_region(web_client):
    _apply_payload(web_client, _homolog_real_capital_pr_freight_route_payload())
    coverage = make_csv([["UF destino", "Cidade destino", "Região de frete"], ["PR", "Curitiba", "Capital"]])
    assert _post_coverage_upload(web_client, "coverage.csv", coverage, "text/csv").status_code == 200
    audit_file = _sample_audit_xlsx(
        _sample_audit_row(
            numero_documento="7414646",
            uf_destino="PR",
            cidade_destino="Curitiba",
            peso="20",
            valor_frete="41,03",
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
    assert result["status"] == "ok"
    assert result["reason_code"] != "missing_freight_rule"
    assert result["freight_region"] == "Capital"
    assert result["expected_freight"] == 41.03
    assert result["calculation_components"]["weight_freight"]["amount"] == 41.03


def test_audit_run_freight_route_homolog_real_doc_7414646_uses_70kg_bracket(web_client):
    _apply_payload(web_client, _homolog_real_capital_pr_freight_route_payload())
    coverage = make_csv([["UF destino", "Cidade destino", "Regi?o de frete"], ["PR", "Curitiba", "Capital"]])
    assert _post_coverage_upload(web_client, "coverage.csv", coverage, "text/csv").status_code == 200
    audit_file = _sample_audit_xlsx(
        _sample_audit_row(
            numero_documento="7414646",
            uf_destino="PR",
            cidade_destino="Curitiba",
            peso="68,64",
            valor_frete="54,33",
            valor_nf="1000,00",
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
    assert result["status"] not in {"missing_freight_rule", "unsupported_pricing_model"}
    assert result["reason_code"] != "missing_freight_rule"
    assert result["expected_freight"] is not None
    assert result["freight_region"] == "Capital"
    assert result["weight_freight"] == pytest.approx(54.33, abs=0.01)
    assert result["expected_freight"] == pytest.approx(54.33, abs=0.01)
    assert result["calculation_basis"] == "range_plus_excess_per_kg"
    assert result["calculation_components"]["weight_freight"]["basis"] == "range_plus_excess_per_kg"
    assert "direct_weight_rate" not in str(result["calculation_details"])
    assert "direct_weight_rate" not in json.dumps(result["calculation_components"], ensure_ascii=False)
    assert result["weight_freight"] != pytest.approx(68.64 * 0.45, abs=0.01)


def test_audit_run_freight_route_homolog_real_null_origin_pr_above_100kg_uses_excess(web_client):
    _apply_payload(web_client, _homolog_real_capital_pr_freight_route_payload())
    coverage = make_csv([["UF destino", "Cidade destino", "Regi?o de frete"], ["PR", "Curitiba", "Capital"]])
    assert _post_coverage_upload(web_client, "coverage.csv", coverage, "text/csv").status_code == 200
    audit_file = _sample_audit_xlsx(
        _sample_audit_row(
            uf_destino="PR",
            cidade_destino="Curitiba",
            peso="506,88",
            valor_frete="240,76",
            valor_nf="1000,00",
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
    assert result["status"] != "missing_freight_rule"
    assert result["reason_code"] != "missing_freight_rule"
    assert result["expected_freight"] is not None
    assert result["freight_region"] == "Capital"
    assert result["weight_freight"] == pytest.approx(240.76, abs=0.01)
    assert result["expected_freight"] == pytest.approx(240.76, abs=0.01)
    assert result["calculation_basis"] == "range_plus_excess_per_kg"
    assert result["calculation_components"]["weight_freight"]["basis"] == "range_plus_excess_per_kg"
    assert "direct_weight_rate" not in str(result["calculation_details"])
    assert "direct_weight_rate" not in json.dumps(result["calculation_components"], ensure_ascii=False)
    assert result["weight_freight"] != pytest.approx(506.88 * 0.45, abs=0.01)


def test_freight_route_homolog_interior_es_registers_contextual_key_and_matches():
    payload = _homolog_freight_route_payload(
        origin="Interior",
        destination="ES",
        notes="Região: Interior",
        weight_30="70,00",
    )
    pricing_index = audit_doc_service.build_freight_pricing_index(payload)
    assert "Interior" in pricing_index
    assert "ES|INTERIOR" in pricing_index
    match = audit_doc_service._find_pricing_rule_match(pricing_index, "Interior", "ES", "Castelo")
    assert match is not None
    rule, lookup_kind, lookup_key = match
    assert lookup_key in {"Interior", "ES|INTERIOR"}
    assert rule["pricing_type"] == "fixed_range"
    assert rule["brackets"][0]["value"] == 70.0
    assert lookup_kind == "freight_region"


def test_freight_route_homolog_parity_with_freight_tables():
    table_payload = {
        "status": "needs_review",
        "freight_tables": [
            {
                "table_title": "Tabela por região e UF",
                "table_type": "weight_range_table",
                "columns": ["Região", "UF", "ATÉ 30KG", "ATÉ 50KG", "ATÉ 70KG", "ATÉ 100KG", "Excedente"],
                "rows": [
                    {
                        "Região": "Capital",
                        "UF": "PR",
                        "ATÉ 30KG": "41,03",
                        "ATÉ 50KG": "45,46",
                        "ATÉ 70KG": "54,33",
                        "ATÉ 100KG": "57,66",
                        "Excedente": "0,45",
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
    route_payload = _homolog_capital_pr_freight_route_payload()
    table_index = audit_doc_service.build_freight_pricing_index(table_payload)
    route_index = audit_doc_service.build_freight_pricing_index(route_payload)
    table_match = audit_doc_service._find_pricing_rule_match(table_index, "Capital", "PR", "Curitiba")
    route_match = audit_doc_service._find_pricing_rule_match(route_index, "Capital", "PR", "Curitiba")
    assert table_match is not None
    assert route_match is not None
    assert "PR|CAPITAL" in table_index
    assert "PR|CAPITAL" in route_index
    table_rule, _, _ = table_match
    route_rule, _, _ = route_match
    assert table_rule["pricing_type"] == route_rule["pricing_type"]
    assert table_rule["brackets"][0]["value"] == route_rule["brackets"][0]["value"]
    assert table_rule["excess"]["rate_per_kg"] == route_rule["excess"]["rate_per_kg"]
    assert audit_doc_service.calculate_weight_freight(68.64, table_rule)["expected_freight"] == audit_doc_service.calculate_weight_freight(
        68.64, route_rule
    )["expected_freight"]


def test_freight_route_homolog_does_not_canonicalize_real_logistics_route():
    payload = _homolog_freight_route_payload(
        origin="São Paulo",
        destination="Curitiba",
        weight_30="90,00",
    )
    pricing_index = audit_doc_service.build_freight_pricing_index(payload)
    assert "Curitiba" in pricing_index
    assert "PR|CAPITAL" not in pricing_index
    assert "PR|SAO PAULO" not in pricing_index


def test_freight_route_homolog_does_not_canonicalize_both_uf_endpoints():
    payload = _homolog_freight_route_payload(origin="SP", destination="PR", weight_30="90,00")
    pricing_index = audit_doc_service.build_freight_pricing_index(payload)
    assert "PR" in pricing_index
    assert "PR|SP" not in pricing_index
    assert audit_doc_service._find_pricing_rule_match(pricing_index, "Capital", "PR", "Curitiba") is None


def test_freight_route_pr_capital_composite_registers_contextual_key():
    payload = _freight_route_region_pricing_payload(destination="PR - Capital", weight_30="50,00")
    pricing_index = audit_doc_service.build_freight_pricing_index(payload)
    assert "PR - Capital" in pricing_index
    assert "PR|CAPITAL" in pricing_index
    match = audit_doc_service._find_pricing_rule_match(pricing_index, "Capital", "PR", "Curitiba")
    assert match is not None
    _, _, lookup_key = match
    assert lookup_key == "PR|CAPITAL"


def test_audit_run_freight_route_homolog_capital_pr_resolves_coverage_region(web_client):
    _apply_payload(web_client, _homolog_capital_pr_freight_route_payload())
    coverage = make_csv([["UF destino", "Cidade destino", "Região de frete"], ["PR", "Curitiba", "Capital"]])
    assert _post_coverage_upload(web_client, "coverage.csv", coverage, "text/csv").status_code == 200
    audit_file = _sample_audit_xlsx(
        _sample_audit_row(uf_destino="PR", cidade_destino="Curitiba", peso="20", valor_frete="41,03")
    )
    assert _post_audit_upload(
        web_client,
        "auditado.xlsx",
        audit_file,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ).status_code == 200
    resp = _post_audit_run(web_client)
    result = resp.get_json()["temp_table"]["audit_batch"]["results"][0]
    assert result["status"] == "ok"
    assert result["freight_region"] == "Capital"
    assert result["expected_freight"] == 41.03


def test_audit_run_freight_route_homolog_interior_es_resolves_coverage_region(web_client):
    _apply_payload(
        web_client,
        _homolog_freight_route_payload(
            origin="Interior",
            destination="ES",
            notes="Região: Interior",
            weight_30="70,00",
        ),
    )
    coverage = make_csv([["UF destino", "Cidade destino", "Região de frete"], ["ES", "Castelo", "Interior"]])
    assert _post_coverage_upload(web_client, "coverage.csv", coverage, "text/csv").status_code == 200
    audit_file = _sample_audit_xlsx(
        _sample_audit_row(uf_destino="ES", cidade_destino="Castelo", peso="20", valor_frete="70,00")
    )
    assert _post_audit_upload(
        web_client,
        "auditado.xlsx",
        audit_file,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ).status_code == 200
    resp = _post_audit_run(web_client)
    result = resp.get_json()["temp_table"]["audit_batch"]["results"][0]
    assert result["status"] == "ok"
    assert result["freight_region"] == "Interior"
    assert result["expected_freight"] == 70.0


def test_freight_route_explicit_region_not_split_into_contextual_key():
    payload = _freight_route_region_pricing_payload(destination="AM-Interior 1", weight_30="80,00")
    pricing_index = audit_doc_service.build_freight_pricing_index(payload)
    assert "AM-Interior 1" in pricing_index
    assert "AM|INTERIOR 1" not in pricing_index
    match = audit_doc_service._find_pricing_rule_match(pricing_index, "AM-Interior 1", "AM", "Manaus")
    assert match is not None
    rule, lookup_kind, lookup_key = match
    assert lookup_key == "AM-Interior 1"
    assert rule["pricing_type"] == "fixed_range"
    assert lookup_kind == "freight_region"


def _pr_capital_freight_route_payload(*, include_excess: bool = True) -> dict:
    route = {
        "origin": "PR",
        "destination": "PR - Capital",
        "weight_30": "41,03",
        "weight_50": "45,46",
        "weight_70": "54,33",
        "weight_100": "57,66",
    }
    if include_excess:
        route["freight_weight_kg"] = "0,45"
    return {
        "status": "needs_review",
        "freight_tables": [],
        "freight_routes": [route],
        "freight_values": [],
        "accessorial_fees": [],
        "weight_ranges": [],
        "reading_alerts": [],
        "evidence_refs": [],
    }


def _pr_capital_freight_route_rule(*, include_excess: bool = True) -> dict:
    payload = _pr_capital_freight_route_payload(include_excess=include_excess)
    return audit_doc_service.build_freight_pricing_index(payload)["PR - Capital"]


def test_freight_route_range_plus_excess_within_bracket():
    rule = _pr_capital_freight_route_rule()
    assert rule["pricing_type"] == "range_plus_excess_per_kg"
    assert rule["excess"]["rate_per_kg"] == 0.45
    calculated = audit_doc_service.calculate_weight_freight(68.64, rule)
    assert calculated["expected_freight"] == 54.33
    assert calculated["calculation_basis"] == "range_plus_excess_per_kg"
    wrong = audit_doc_service.calculate_weight_freight(
        68.64,
        {"pricing_type": "direct_weight_rate", "unit": "kg", "value_per_kg": 0.45},
    )
    assert wrong["expected_freight"] == 30.89


def test_freight_route_range_plus_excess_above_last_bracket():
    rule = _pr_capital_freight_route_rule()
    calculated = audit_doc_service.calculate_weight_freight(506.88, rule)
    assert calculated["expected_freight"] == 240.76
    assert calculated["calculation_basis"] == "range_plus_excess_per_kg"
    wrong = audit_doc_service.calculate_weight_freight(
        506.88,
        {"pricing_type": "direct_weight_rate", "unit": "kg", "value_per_kg": 0.45},
    )
    assert wrong["expected_freight"] == 228.10


def test_freight_route_fixed_range_without_excess():
    rule = _pr_capital_freight_route_rule(include_excess=False)
    assert rule["pricing_type"] == "fixed_range"
    assert rule.get("excess") is None
    calculated = audit_doc_service.calculate_weight_freight(68.64, rule)
    assert calculated["expected_freight"] == 54.33
    assert calculated["calculation_basis"] == "fixed_range"


def test_freight_route_direct_weight_rate_without_brackets():
    payload = {
        "status": "needs_review",
        "freight_tables": [],
        "freight_routes": [
            {
                "origin": "SP",
                "destination": "SP - Interior",
                "freight_weight_kg": "2,50",
            }
        ],
        "freight_values": [],
        "accessorial_fees": [],
        "weight_ranges": [],
        "reading_alerts": [],
        "evidence_refs": [],
    }
    rule = audit_doc_service.build_freight_pricing_index(payload)["SP - Interior"]
    assert rule["pricing_type"] == "direct_weight_rate"
    assert rule["value_per_kg"] == 2.5
    calculated = audit_doc_service.calculate_weight_freight(10, rule)
    assert calculated["expected_freight"] == 25.00
    assert calculated["calculation_basis"] == "direct_weight_rate"


def test_audit_run_freight_route_capital_pr_resolves_coverage_region(web_client):
    _apply_payload(web_client, _freight_route_region_pricing_payload(destination="Capital - PR", weight_30="50,00"))
    coverage = make_csv([["UF destino", "Cidade destino", "Região de frete"], ["PR", "Curitiba", "Capital"]])
    assert _post_coverage_upload(web_client, "coverage.csv", coverage, "text/csv").status_code == 200
    audit_file = _sample_audit_xlsx(
        _sample_audit_row(uf_destino="PR", cidade_destino="Curitiba", peso="20", valor_frete="50,00")
    )
    assert _post_audit_upload(
        web_client,
        "auditado.xlsx",
        audit_file,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ).status_code == 200
    resp = _post_audit_run(web_client)
    result = resp.get_json()["temp_table"]["audit_batch"]["results"][0]
    assert result["status"] == "ok"
    assert result["freight_region"] == "Capital"
    assert result["expected_freight"] == 50.0


def test_audit_run_freight_route_interior_es_resolves_coverage_region(web_client):
    _apply_payload(web_client, _freight_route_region_pricing_payload(destination="Interior - ES", weight_30="70,00"))
    coverage = make_csv([["UF destino", "Cidade destino", "Região de frete"], ["ES", "Castelo", "Interior"]])
    assert _post_coverage_upload(web_client, "coverage.csv", coverage, "text/csv").status_code == 200
    audit_file = _sample_audit_xlsx(
        _sample_audit_row(uf_destino="ES", cidade_destino="Castelo", peso="20", valor_frete="70,00")
    )
    assert _post_audit_upload(
        web_client,
        "auditado.xlsx",
        audit_file,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ).status_code == 200
    resp = _post_audit_run(web_client)
    result = resp.get_json()["temp_table"]["audit_batch"]["results"][0]
    assert result["status"] == "ok"
    assert result["freight_region"] == "Interior"
    assert result["expected_freight"] == 70.0


def test_audit_run_multi_uf_capital_uses_uf_region_rule(web_client):
    payload = _multi_uf_region_pricing_payload(
        region_label="Capital",
        rows=[
            {"UF": "PR", "Região de frete": "Capital", "Até 30 kg": "50,00"},
            {"UF": "RS", "Região de frete": "Capital", "Até 30 kg": "60,00"},
        ],
    )
    _apply_payload(web_client, payload)
    coverage = make_csv([["UF destino", "Cidade destino", "Região de frete"], ["PR", "Curitiba", "Capital"]])
    assert _post_coverage_upload(web_client, "coverage.csv", coverage, "text/csv").status_code == 200
    audit_file = _sample_audit_xlsx(
        _sample_audit_row(uf_destino="PR", cidade_destino="Curitiba", peso="20", valor_frete="50,00")
    )
    assert _post_audit_upload(
        web_client,
        "auditado.xlsx",
        audit_file,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ).status_code == 200
    resp = _post_audit_run(web_client)
    result = resp.get_json()["temp_table"]["audit_batch"]["results"][0]
    assert result["status"] == "ok"
    assert result["freight_region"] == "Capital"
    assert result["expected_freight"] == 50.0


def test_audit_run_multi_uf_interior_uses_uf_region_rule(web_client):
    payload = _multi_uf_region_pricing_payload(
        region_label="Interior",
        rows=[
            {"UF": "ES", "Região de frete": "Interior", "Até 30 kg": "70,00"},
            {"UF": "RS", "Região de frete": "Interior", "Até 30 kg": "80,00"},
        ],
    )
    _apply_payload(web_client, payload)
    coverage = make_csv([["UF destino", "Cidade destino", "Região de frete"], ["ES", "Castelo", "Interior"]])
    assert _post_coverage_upload(web_client, "coverage.csv", coverage, "text/csv").status_code == 200
    audit_file = _sample_audit_xlsx(
        _sample_audit_row(uf_destino="ES", cidade_destino="Castelo", peso="20", valor_frete="70,00")
    )
    assert _post_audit_upload(
        web_client,
        "auditado.xlsx",
        audit_file,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ).status_code == 200
    resp = _post_audit_run(web_client)
    result = resp.get_json()["temp_table"]["audit_batch"]["results"][0]
    assert result["status"] == "ok"
    assert result["freight_region"] == "Interior"
    assert result["expected_freight"] == 70.0


def test_audit_run_mono_uf_capital_still_works(web_client):
    _apply_payload(web_client, _mono_uf_region_pricing_payload(region_label="Capital", value="55,00"))
    coverage = make_csv([["UF destino", "Cidade destino", "Região de frete"], ["PR", "Curitiba", "Capital"]])
    assert _post_coverage_upload(web_client, "coverage.csv", coverage, "text/csv").status_code == 200
    audit_file = _sample_audit_xlsx(
        _sample_audit_row(uf_destino="PR", cidade_destino="Curitiba", peso="20", valor_frete="55,00")
    )
    assert _post_audit_upload(
        web_client,
        "auditado.xlsx",
        audit_file,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ).status_code == 200
    resp = _post_audit_run(web_client)
    result = resp.get_json()["temp_table"]["audit_batch"]["results"][0]
    assert result["status"] == "ok"
    assert result["freight_region"] == "Capital"
    assert result["expected_freight"] == 55.0


def test_audit_run_unsupported_pricing_when_no_contextual_fallback(web_client):
    duplicate_capital_table = {
        "table_title": "Tabela Capital duplicada",
        "table_type": "weight_range_table",
        "columns": ["Região de frete", "Até 30 kg"],
        "rows": [{"Região de frete": "Capital", "Até 30 kg": "60,00"}],
    }
    payload = {
        "status": "needs_review",
        "freight_tables": [
            {
                "table_title": "Tabela Capital 1",
                "table_type": "weight_range_table",
                "columns": ["Região de frete", "Até 30 kg"],
                "rows": [{"Região de frete": "Capital", "Até 30 kg": "50,00"}],
            },
            duplicate_capital_table,
        ],
        "freight_routes": [],
        "freight_values": [],
        "accessorial_fees": [],
        "weight_ranges": [],
        "reading_alerts": [],
        "evidence_refs": [],
    }
    _apply_payload(web_client, payload)
    pricing_index = audit_doc_service.build_freight_pricing_index(payload)
    assert pricing_index["Capital"]["pricing_type"] == "unsupported_pricing_model"
    assert "PR|CAPITAL" not in pricing_index
    coverage = make_csv([["UF destino", "Cidade destino", "Região de frete"], ["PR", "Curitiba", "Capital"]])
    assert _post_coverage_upload(web_client, "coverage.csv", coverage, "text/csv").status_code == 200
    audit_file = _sample_audit_xlsx(
        _sample_audit_row(uf_destino="PR", cidade_destino="Curitiba", peso="20", valor_frete="50,00")
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
    assert result["freight_region"] == "Capital"


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
    ok_result = batch["results"][0]
    assert ok_result["expected_freight"] == 100.50
    assert isinstance(ok_result.get("calculation_components"), dict)
    assert ok_result["calculation_components"]["weight_freight"]["amount"] == 100.50


def test_audit_run_adds_simple_freight_value_to_weight_freight(web_client):
    result = _run_single_audit(
        web_client,
        _freight_value_pricing_payload(header="Frete Valor %", freight_value="0,1%", weight_value="100,00"),
    )
    assert result["status"] == "ok"
    assert result["weight_freight"] == 100.00
    assert result["freight_value_amount"] == 1.00
    assert result["expected_freight"] == 101.00
    assert result["divergence_value"] == 0
    assert result["calculation_components"]["freight_value"]["rate"] == 0.001
    assert result["calculation_components"]["freight_value"]["invoice_value"] == 1000.00


def test_audit_run_converts_freight_value_percent_column_without_percent_symbol(web_client):
    result = _run_single_audit(
        web_client,
        _freight_value_pricing_payload(header="Frete Valor %", freight_value="0,54", weight_value="100,00"),
        audit_row=_sample_audit_row(valor_frete="105,40", peso="48", valor_nf="1000"),
    )
    assert result["status"] == "ok"
    assert result["freight_value_amount"] == 5.40
    assert result["expected_freight"] == 105.40
    assert result["calculation_components"]["freight_value"]["source_column"] == "Frete Valor %"


def test_audit_run_sums_freight_value_with_range_plus_excess_weight_freight(web_client):
    payload = _sample_city_pricing_payload()
    payload["freight_tables"][0]["columns"].append("% Valor NF")
    for row in payload["freight_tables"][0]["rows"]:
        row["% Valor NF"] = "0,54"
    _apply_payload(web_client, payload)
    coverage = make_csv([["UF destino", "Cidade destino", "Região de frete"], ["TO", "Palmas", "TO - Capital"]])
    assert _post_coverage_upload(web_client, "coverage.csv", coverage, "text/csv").status_code == 200
    audit_file = _sample_audit_xlsx(
        _sample_audit_row(
            uf_destino="TO",
            cidade_destino="Palmas",
            peso="320,5",
            valor_nf="1000",
            valor_frete="531,49",
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
    assert result["weight_freight"] == 526.09
    assert result["freight_value_amount"] == 5.40
    assert result["expected_freight"] == 531.49
    assert result["status"] == "ok"


def test_audit_run_returns_invalid_invoice_value_when_rule_requires_freight_value(web_client):
    result = _run_single_audit(
        web_client,
        _freight_value_pricing_payload(header="Frete Valor %", freight_value="0,1%", weight_value="100,00"),
        audit_row=_sample_audit_row(valor_frete="101,00", peso="48", valor_nf=""),
    )
    assert result["status"] == "invalid_invoice_value"
    assert result["reason_code"] == "invalid_invoice_value"
    assert result["expected_freight"] is None


def test_audit_run_calculates_simple_accessorial_ad_valorem_percent(web_client):
    result = _run_single_audit(
        web_client,
        _freight_value_pricing_payload(
            header="Frete Valor",
            freight_value="0,54",
            weight_value="100,00",
            accessorial_fees=[_accessorial_fee("Ad Valorem", "0,10%")],
        ),
        audit_row=_sample_audit_row(valor_frete="101,00", peso="48", valor_nf="1000"),
    )
    assert result["status"] == "ok"
    assert result["expected_freight"] == 101.00
    assert result["freight_value_amount"] is None
    assert result["accessorial_fees_amount"] == 1.00
    assert result["accessorial_percent_fees_amount"] == 1.00
    component = result["calculation_components"]["accessorial_fees"][0]
    assert component["label"] == "Ad Valorem"
    assert component["canonical_name"] == "ad_valorem"
    assert component["canonical_component"] == "ad_valorem"
    assert component["calculation_type"] == "invoice_percentage"
    assert component["calculated_amount"] == 1.00
    assert component["minimum_amount"] is None
    assert component["minimum_applied"] is False
    assert component["amount"] == 1.00
    assert component["rate"] == 0.001
    assert component["source_value"] == "0,10%"
    assert component["invoice_value"] == 1000.00
    assert "Ad Valorem: 1000 x 0,1% = 1" == component["details"]
    assert component["source_block"] == "accessorial_fees"
    assert component["reason_code"] == "accessorial_percentage_calculated"


def test_audit_run_calculates_unknown_percent_by_configured_base(web_client):
    result = _run_single_audit(
        web_client,
        _freight_value_pricing_payload(
            header="Frete Valor",
            freight_value="0,54",
            weight_value="100,00",
            accessorial_fees=[
                _accessorial_fee(
                    "Taxa XPTO",
                    "0,25%",
                    calculation_basis="sobre nota fiscal",
                )
            ],
        ),
        audit_row=_sample_audit_row(valor_frete="102,50", peso="48", valor_nf="1000"),
    )
    assert result["status"] == "ok"
    assert result["expected_freight"] == 102.50
    assert result["accessorial_fees_amount"] == 2.50
    component = result["calculation_components"]["accessorial_fees"][0]
    assert component["label"] == "Taxa XPTO"
    assert component["classification_source"] == "configured_calculation_base"
    assert component["calculation_base_id"] == "pct_nota_fiscal"
    assert component["operation"] == "percentage_of_variable"
    assert component["audit_variable"] == "valor_nf"
    assert component["amount"] == 2.50


def test_audit_run_calculates_gris_percent_by_configured_base(web_client):
    result = _run_single_audit(
        web_client,
        _freight_value_pricing_payload(
            header="Frete Valor",
            freight_value="0,54",
            weight_value="100,00",
            accessorial_fees=[
                _accessorial_fee(
                    "GRIS",
                    "0,20%",
                    calculation_basis="sobre o valor da Nota Fiscal",
                )
            ],
        ),
        audit_row=_sample_audit_row(valor_frete="102,00", peso="48", valor_nf="1000"),
    )
    component = result["calculation_components"]["accessorial_fees"][0]
    assert result["expected_freight"] == 102.00
    assert component["label"] == "GRIS"
    assert component["calculation_base_id"] == "pct_nota_fiscal"
    assert component["operation"] == "percentage_of_variable"
    assert component["amount"] == 2.00


@pytest.mark.parametrize(
    ("name", "value", "expected_amount"),
    [
        ("TAS", "R$ 10,24", 10.24),
        ("SUFRAMA", "R$ 57,62", 57.62),
        ("Taxa ABC", "R$ 34,18", 34.18),
    ],
)
def test_audit_run_calculates_fixed_amount_by_configured_base(
    web_client,
    name,
    value,
    expected_amount,
):
    result = _run_single_audit(
        web_client,
        _freight_value_pricing_payload(
            header="Frete Valor",
            freight_value="0,54",
            weight_value="100,00",
            accessorial_fees=[
                _accessorial_fee(name, value, unit="R$", calculation_basis="por Cte")
            ],
        ),
        audit_row=_sample_audit_row(valor_frete=str(100 + expected_amount), peso="48", valor_nf="1000"),
    )
    component = result["calculation_components"]["accessorial_fees"][0]
    assert result["expected_freight"] == round(100 + expected_amount, 2)
    assert result["accessorial_fees_amount"] == expected_amount
    assert result["accessorial_percent_fees_amount"] is None
    assert component["label"] == name
    assert component["calculation_base_id"] == "por_cte"
    assert component["operation"] == "fixed_amount"
    assert component["amount"] == expected_amount
    assert component["details"] == f"valor fixo = {str(expected_amount).replace('.', ',')}"


def test_audit_run_sums_tas_and_suframa_fixed_amounts(web_client):
    result = _run_single_audit(
        web_client,
        _freight_value_pricing_payload(
            header="Frete Valor",
            freight_value="0,54",
            weight_value="100,00",
            accessorial_fees=[
                _accessorial_fee("TAS", "R$ 10,24", unit="R$", calculation_basis="por Cte"),
                _accessorial_fee("SUFRAMA", "R$ 57,62", unit="R$", calculation_basis="por Cte"),
            ],
        ),
        audit_row=_sample_audit_row(valor_frete="167,86", peso="48", valor_nf="1000"),
    )
    assert result["status"] == "ok"
    assert result["expected_freight"] == 167.86
    assert result["accessorial_fees_amount"] == 67.86
    assert [(item["label"], item["amount"]) for item in result["calculation_components"]["accessorial_fees"]] == [
        ("TAS", 10.24),
        ("SUFRAMA", 57.62),
    ]


def test_audit_run_calculates_weight_fraction_by_configured_base(web_client):
    result = _run_single_audit(
        web_client,
        {
            **_sample_pricing_payload(),
            "freight_tables": [
                {
                    "table_title": "Tabela por região",
                    "table_type": "weight_range_table",
                    "columns": ["Região de frete", "Até 300 kg"],
                    "rows": [{"Região de frete": "SP-Interior 1", "Até 300 kg": "100,00"}],
                }
            ],
            "accessorial_fees": [
                _accessorial_fee(
                    "Pedágio",
                    "R$ 10,24",
                    unit="R$",
                    calculation_basis="para cada 100Kg ou fração",
                )
            ],
        },
        audit_row=_sample_audit_row(valor_frete="130,72", peso="201", valor_nf="1000"),
    )
    component = result["calculation_components"]["accessorial_fees"][0]
    assert result["status"] == "ok"
    assert result["expected_freight"] == 130.72
    assert result["accessorial_fees_amount"] == 30.72
    assert component["label"] == "Pedágio"
    assert component["calculation_base_id"] == "fracao_100kg"
    assert component["operation"] == "ceil_fraction"
    assert component["audit_variable"] == "peso"
    assert component["fraction_size"] == 100.00
    assert component["weight"] == 201.00
    assert component["base_amount"] == 10.24
    assert component["fractions"] == 3
    assert component["amount"] == 30.72


def test_audit_run_does_not_calculate_new_accessorial_types(web_client):
    result = _run_single_audit(
        web_client,
        _freight_value_pricing_payload(
            header="Frete Valor",
            freight_value="0,54",
            weight_value="100,00",
            accessorial_fees=[
                {
                    "name": "Pedágio automático",
                    "value": "999,00",
                    "unit": "R$",
                    "calculation_basis": "por entrega",
                    "notes": "",
                },
                {
                    "name": "TAS",
                    "value": "R$ 15,00",
                    "unit": "R$",
                    "calculation_basis": "por dia",
                    "notes": "",
                },
                {
                    "name": "TSO",
                    "value": "5%",
                    "unit": "%",
                    "calculation_basis": "sobre frete",
                    "notes": "",
                },
            ],
        ),
        audit_row=_sample_audit_row(valor_frete="100,00", peso="48", valor_nf="1000"),
    )
    assert result["status"] == "ok"
    assert result["weight_freight"] == 100.00
    assert result["freight_value_amount"] is None
    assert result["accessorial_percent_fees_amount"] is None
    assert result["expected_freight"] == 100.00
    assert result["calculation_components"]["accessorial_percent_fees"] == []
    ignored = result["calculation_components"]["ignored_accessorial_fees"]
    assert [item["label"] for item in ignored] == ["Pedágio automático", "TAS", "TSO"]
    assert ignored[0]["source_value"] == "999,00"


def test_audit_run_applies_configured_linked_minimum_modifier(web_client):
    result = _run_single_audit(
        web_client,
        _freight_value_pricing_payload(
            header="Frete Valor",
            freight_value="0,3",
            weight_value="100,00",
            accessorial_fees=_configured_base_with_linked_minimum_fees(
                base_name="GRIS",
                minimum_name="GRIS mínimo",
                group="risk_management",
                rate_value="0.15",
                minimum_amount=4.99,
            ),
        ),
        audit_row=_sample_audit_row(valor_frete="104,99", peso="48", valor_nf="3016,55"),
    )
    assert result["status"] == "ok"
    assert result["accessorial_fees_amount"] == 4.99
    component = result["calculation_components"]["accessorial_fees"][0]
    assert component["label"] == "GRIS"
    assert component["calculation_base_id"] == "pct_nota_fiscal"
    assert component["operation"] == "percentage_of_variable"
    assert component["calculated_amount"] == 4.52
    assert component["minimum_amount"] == 4.99
    assert component["minimum_applied"] is True
    assert component["amount"] == 4.99
    assert result["calculation_components"]["ignored_accessorial_fees"] == []


def test_audit_run_applies_linked_accessorial_minimum_modifier(web_client):
    result = _run_single_audit(
        web_client,
        _freight_value_pricing_payload(
            header="Frete Valor",
            freight_value="0,54",
            weight_value="100,00",
            accessorial_fees=[
                _accessorial_fee("GRIS", "0,30%", calculation_basis="sobre NF"),
                {
                    "name": "GRIS Mínimo R$ por Cte",
                    "value": "R$ 50,00",
                    "unit": "R$",
                    "calculation_basis": "por Cte",
                    "notes": "",
                },
                {
                    "name": "TRT limite máximo",
                    "value": "R$ 10,00",
                    "unit": "R$",
                    "calculation_basis": "",
                    "notes": "teto por CTe",
                },
            ],
        ),
        audit_row=_sample_audit_row(valor_frete="150,00", peso="48", valor_nf="1000"),
    )
    assert result["status"] == "ok"
    assert result["expected_freight"] == 150.00
    assert result["accessorial_fees_amount"] == 50.00
    assert result["accessorial_percent_fees_amount"] == 50.00
    component = result["calculation_components"]["accessorial_fees"][0]
    assert component["label"] == "GRIS"
    assert component["calculation_base_id"] == "pct_nota_fiscal"
    assert component["operation"] == "percentage_of_variable"
    assert component["calculated_amount"] == 3.00
    assert component["minimum_amount"] == 50.00
    assert component["minimum_applied"] is True
    assert component["amount"] == 50.00
    ignored = result["calculation_components"]["ignored_accessorial_fees"]
    assert {item["label"] for item in ignored} == {"TRT limite máximo"}


def test_audit_run_keeps_accessorial_percent_when_minimum_not_applied(web_client):
    result = _run_single_audit(
        web_client,
        _freight_value_pricing_payload(
            header="Frete Valor",
            freight_value="0,54",
            weight_value="100,00",
            accessorial_fees=[
                _accessorial_fee("GRIS", "0,20%", calculation_basis="sobre NF"),
                {
                    "name": "GRIS Mínimo R$ por Cte",
                    "value": "R$ 6,84",
                    "unit": "R$",
                    "calculation_basis": "por Cte",
                    "notes": "",
                },
            ],
        ),
        audit_row=_sample_audit_row(valor_frete="120,00", peso="48", valor_nf="10000"),
    )
    assert result["status"] == "ok"
    assert result["expected_freight"] == 120.00
    assert result["accessorial_fees_amount"] == 20.00
    component = result["calculation_components"]["accessorial_fees"][0]
    assert component["calculation_base_id"] == "pct_nota_fiscal"
    assert component["amount"] == 20.00


def test_audit_run_calculates_accessorial_percent_when_unit_is_percent(web_client):
    result = _run_single_audit(
        web_client,
        _freight_value_pricing_payload(
            header="Frete Valor",
            freight_value="0,54",
            weight_value="100,00",
            accessorial_fees=[_accessorial_fee("Ad Valorem", "0,10", unit="%")],
        ),
        audit_row=_sample_audit_row(valor_frete="101,00", peso="48", valor_nf="1000"),
    )
    assert result["status"] == "ok"
    assert result["accessorial_percent_fees_amount"] == 1.00
    assert result["calculation_components"]["accessorial_percent_fees"][0]["rate"] == 0.001


@pytest.mark.parametrize(
    "alias",
    [
        "Frete Valor %",
        "FV %",
        "F.V. %",
        "% NF",
        "% Nota Fiscal",
        "% Valor NF",
        "% Sobre NF",
        "Percentual NF",
        "Perc. NF",
        "Sob. NF",
        "S/NF",
        "Ad Valorem %",
    ],
)
def test_audit_run_calculates_safe_accessorial_percent_aliases(alias, web_client):
    result = _run_single_audit(
        web_client,
        _freight_value_pricing_payload(
            header="Frete Valor",
            freight_value="0,54",
            weight_value="100,00",
            accessorial_fees=[_accessorial_fee(alias, "0,10")],
        ),
        audit_row=_sample_audit_row(valor_frete="101,00", peso="48", valor_nf="1000"),
    )
    assert result["status"] == "ok"
    assert result["accessorial_percent_fees_amount"] == 1.00
    assert result["calculation_components"]["accessorial_percent_fees"][0]["label"] == alias


def test_audit_run_calculates_accessorial_ad_valorem_when_tariff_freight_value_absent(web_client):
    result = _run_single_audit(
        web_client,
        _freight_value_pricing_payload(
            header="Frete Valor",
            freight_value="0,54",
            weight_value="100,00",
            accessorial_fees=[_accessorial_fee("Ad Valorem %", "0,10")],
        ),
        audit_row=_sample_audit_row(valor_frete="101,00", peso="48", valor_nf="1000"),
    )
    assert result["status"] == "ok"
    assert result["expected_freight"] == 101.00
    assert result["accessorial_percent_fees_amount"] == 1.00


def test_audit_run_counts_only_pricing_block_freight_value_when_accessorial_also_exists(web_client):
    result = _run_single_audit(
        web_client,
        _freight_value_pricing_payload(
            header="Frete Valor %",
            freight_value="0,1",
            weight_value="100,00",
            accessorial_fees=[_accessorial_fee("Frete Valor %", "0,10")],
        ),
    )
    assert result["status"] == "ok"
    assert result["freight_value_amount"] == 1.00
    assert result["accessorial_percent_fees_amount"] is None
    assert result["expected_freight"] == 101.00
    ignored = result["calculation_components"]["ignored_accessorial_fees"][0]
    assert ignored["label"] == "Frete Valor %"
    assert ignored["canonical_name"] == "freight_value"
    assert ignored["reason_code"] == "duplicate_invoice_percentage_fee_ignored"


def test_audit_run_calculates_gris_seguro_and_unknown_names_by_formula(web_client):
    result = _run_single_audit(
        web_client,
        _freight_value_pricing_payload(
            header="Frete Valor",
            freight_value="0,54",
            weight_value="100,00",
            accessorial_fees=[
                _accessorial_fee("GRIS", "0,20%", calculation_basis="sobre NF"),
                _accessorial_fee("Seguro", "0,15%", calculation_basis="sobre NF"),
                _accessorial_fee("Taxa XPTO", "0,25%", calculation_basis="sobre nota fiscal"),
            ],
        ),
        audit_row=_sample_audit_row(valor_frete="106,00", peso="48", valor_nf="1000"),
    )
    assert result["status"] == "ok"
    assert result["expected_freight"] == 106.00
    assert result["accessorial_fees_amount"] == 6.00
    components = result["calculation_components"]["accessorial_fees"]
    assert [(item["label"], item["amount"]) for item in components] == [
        ("GRIS", 2.00),
        ("Seguro", 1.50),
        ("Taxa XPTO", 2.50),
    ]
    assert [item["canonical_component"] for item in components] == [
        "risk_management",
        "insurance",
        "generic_accessorial",
    ]
    assert result["calculation_components"]["ignored_accessorial_fees"] == []


def test_audit_run_does_not_sum_minimum_without_principal_fee(web_client):
    result = _run_single_audit(
        web_client,
        _freight_value_pricing_payload(
            header="Frete Valor",
            freight_value="0,54",
            weight_value="100,00",
            accessorial_fees=[
                {
                    "name": "GRIS Mínimo R$ por Cte",
                    "value": "R$ 6,84",
                    "unit": "R$",
                    "calculation_basis": "por Cte",
                    "notes": "",
                },
            ],
        ),
        audit_row=_sample_audit_row(valor_frete="100,00", peso="48", valor_nf="1000"),
    )
    assert result["status"] == "ok"
    assert result["expected_freight"] == 100.00
    assert result["accessorial_fees_amount"] is None
    assert result["calculation_components"]["accessorial_fees"] == []
    ignored = result["calculation_components"]["ignored_accessorial_fees"]
    assert ignored[0]["label"] == "GRIS Mínimo R$ por Cte"
    assert ignored[0]["reason_code"] == "accessorial_minimum_without_base_ignored"


def test_audit_run_does_not_calculate_accessorial_with_textual_condition(web_client):
    result = _run_single_audit(
        web_client,
        _freight_value_pricing_payload(
            header="Frete Valor",
            freight_value="0,54",
            weight_value="100,00",
            accessorial_fees=[
                {
                    "name": "GRIS",
                    "value": "0,15%; 0,30% RJ mínimo R$4,13",
                    "unit": "%/R$",
                    "calculation_basis": "sobre NF",
                    "notes": "",
                },
            ],
        ),
        audit_row=_sample_audit_row(valor_frete="100,00", peso="48", valor_nf="1000"),
    )
    assert result["status"] == "ok"
    assert result["expected_freight"] == 100.00
    assert result["accessorial_fees_amount"] is None
    assert result["calculation_components"]["accessorial_fees"] == []
    ignored = result["calculation_components"]["ignored_accessorial_fees"]
    assert ignored[0]["label"] == "GRIS"
    assert ignored[0]["reason_code"] == "unsupported_accessorial_condition"


def test_audit_run_does_not_calculate_configured_accessorial_with_textual_condition(web_client):
    result = _run_single_audit(
        web_client,
        _freight_value_pricing_payload(
            header="Frete Valor",
            freight_value="0,54",
            weight_value="100,00",
            accessorial_fees=[
                {
                    "name": "Taxa XPTO",
                    "value": "0,20%",
                    "unit": "%",
                    "calculation_basis": "sobre nota fiscal",
                    "notes": "somente RJ",
                },
            ],
        ),
        audit_row=_sample_audit_row(valor_frete="100,00", peso="48", valor_nf="1000"),
    )
    assert result["status"] == "ok"
    assert result["expected_freight"] == 100.00
    assert result["calculation_components"]["accessorial_fees"] == []
    ignored = result["calculation_components"]["ignored_accessorial_fees"][0]
    assert ignored["label"] == "Taxa XPTO"
    assert ignored["reason_code"] == "conditions_present"
    assert ignored["calculation_base_id"] == "pct_nota_fiscal"


def test_audit_run_ignores_ambiguous_accessorial_percentages(web_client):
    result = _run_single_audit(
        web_client,
        _freight_value_pricing_payload(
            header="Frete Valor",
            freight_value="0,54",
            weight_value="100,00",
            accessorial_fees=[
                _accessorial_fee("Valor Frete", "10,00"),
                _accessorial_fee("Taxa", "0,10"),
                _accessorial_fee("Ad Valorem", "0,10"),
            ],
        ),
        audit_row=_sample_audit_row(valor_frete="100,00", peso="48", valor_nf="1000"),
    )
    assert result["status"] == "ok"
    assert result["expected_freight"] == 100.00
    assert result["accessorial_percent_fees_amount"] is None
    ignored = result["calculation_components"]["ignored_accessorial_fees"]
    assert [item["label"] for item in ignored] == ["Valor Frete", "Taxa", "Ad Valorem"]
    assert {item["reason_code"] for item in ignored} == {"ambiguous_accessorial_percentage"}


def test_audit_run_preserves_base_when_accessorial_invoice_value_is_invalid(web_client):
    result = _run_single_audit(
        web_client,
        {
            **_sample_pricing_payload(),
            "accessorial_fees": [_accessorial_fee("GRIS", "0,20%", calculation_basis="sobre NF")],
        },
        audit_row=_sample_audit_row(valor_frete="100,50", peso="48", valor_nf=""),
    )
    assert result["status"] == "ok"
    assert result["reason_code"] is None
    assert result["weight_freight"] == 100.50
    assert result["freight_value_amount"] is None
    assert result["expected_freight"] == 100.50
    assert result["accessorial_fees_amount"] is None
    assert result["calculation_components"]["accessorial_fees"] == []
    ignored = result["calculation_components"]["ignored_accessorial_fees"]
    assert ignored[0]["label"] == "GRIS"
    assert ignored[0]["reason_code"] == "missing_audit_variable"


def test_audit_run_calculates_accessorial_percent_after_base_when_invoice_value_is_valid(web_client):
    result = _run_single_audit(
        web_client,
        {
            **_sample_pricing_payload(),
            "accessorial_fees": [_accessorial_fee("GRIS", "0,20%", calculation_basis="sobre NF")],
        },
        audit_row=_sample_audit_row(valor_frete="102,50", peso="48", valor_nf="1000"),
    )
    assert result["status"] == "ok"
    assert result["weight_freight"] == 100.50
    assert result["expected_freight"] == 102.50
    assert result["accessorial_fees_amount"] == 2.00
    component = result["calculation_components"]["accessorial_fees"][0]
    assert component["label"] == "GRIS"
    assert component["amount"] == 2.00


def test_audit_run_applies_accessorial_minimum_after_base(web_client):
    result = _run_single_audit(
        web_client,
        {
            **_sample_pricing_payload(),
            "accessorial_fees": [
                _accessorial_fee("GRIS", "0,20%", calculation_basis="sobre NF"),
                {
                    "name": "GRIS Mínimo R$ por Cte",
                    "value": "R$ 6,84",
                    "unit": "R$",
                    "calculation_basis": "por Cte",
                    "notes": "",
                },
            ],
        },
        audit_row=_sample_audit_row(valor_frete="107,34", peso="48", valor_nf="1000"),
    )
    assert result["status"] == "ok"
    assert result["weight_freight"] == 100.50
    assert result["expected_freight"] == 107.34
    assert result["accessorial_fees_amount"] == 6.84
    component = result["calculation_components"]["accessorial_fees"][0]
    assert component["calculation_base_id"] == "pct_nota_fiscal"
    assert component["operation"] == "percentage_of_variable"
    assert component["calculated_amount"] == 2.00
    assert component["minimum_amount"] == 6.84
    assert component["minimum_applied"] is True
    assert component["amount"] == 6.84
    assert result["calculation_components"]["ignored_accessorial_fees"] == []


def test_audit_run_missing_coverage_mapping(web_client):
    _apply_payload(web_client, _sample_pricing_payload())
    coverage = make_csv([["UF destino", "Cidade destino", "Região de frete"], ["SP", "Sorocaba", "SP-Interior 1"]])
    assert _post_coverage_upload(web_client, "coverage.csv", coverage, "text/csv").status_code == 200
    assert _post_audit_upload(web_client, "auditado.xlsx", _sample_audit_xlsx(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet").status_code == 200
    resp = _post_audit_run(web_client)
    result = resp.get_json()["temp_table"]["audit_batch"]["results"][0]
    assert result["status"] == "missing_coverage_mapping"


def test_audit_run_missing_coverage_mapping_with_accessorial_keeps_no_mapping(web_client):
    _apply_payload(
        web_client,
        {
            **_sample_pricing_payload(),
            "accessorial_fees": [_accessorial_fee("GRIS", "0,20%", calculation_basis="sobre NF")],
        },
    )
    coverage = make_csv([["UF destino", "Cidade destino", "Região de frete"], ["SP", "Sorocaba", "SP-Interior 1"]])
    assert _post_coverage_upload(web_client, "coverage.csv", coverage, "text/csv").status_code == 200
    assert _post_audit_upload(
        web_client,
        "auditado.xlsx",
        _sample_audit_xlsx(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ).status_code == 200
    resp = _post_audit_run(web_client)
    result = resp.get_json()["temp_table"]["audit_batch"]["results"][0]
    assert result["status"] == "missing_coverage_mapping"
    assert result["expected_freight"] is None


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


def test_audit_run_missing_freight_rule_with_accessorial_keeps_no_rule(web_client):
    _apply_payload(
        web_client,
        {
            **_sample_pricing_payload(),
            "accessorial_fees": [_accessorial_fee("GRIS", "0,20%", calculation_basis="sobre NF")],
        },
    )
    coverage = make_csv([["UF destino", "Cidade destino", "Região de frete"], ["SP", "Campinas", "SP-Interior 9"]])
    assert _post_coverage_upload(web_client, "coverage.csv", coverage, "text/csv").status_code == 200
    assert _post_audit_upload(
        web_client,
        "auditado.xlsx",
        _sample_audit_xlsx(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ).status_code == 200
    resp = _post_audit_run(web_client)
    result = resp.get_json()["temp_table"]["audit_batch"]["results"][0]
    assert result["status"] == "missing_freight_rule"
    assert result["expected_freight"] is None


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


def test_audit_run_city_rules_keep_base_with_accessorial_and_missing_invoice_value(web_client):
    payload = {
        **_sample_city_pricing_payload(),
        "accessorial_fees": [_accessorial_fee("GRIS", "0,20%", calculation_basis="sobre NF")],
    }
    _apply_payload(web_client, payload)
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
            valor_nf="",
            valor_frete="526,09",
        ),
        _sample_audit_row(
            numero_documento="AC-1",
            uf_destino="AC",
            cidade_destino="Rio Branco",
            peso="201",
            valor_nf="",
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
    palmas, rio_branco = results
    assert palmas["freight_region"] == "TO - Capital"
    assert palmas["expected_freight"] == 526.09
    assert palmas["status"] == "ok"
    assert rio_branco["freight_region"] == "AC - Capital"
    assert rio_branco["expected_freight"] == 400.54
    assert rio_branco["status"] == "ok"
    for result in results:
        assert result["accessorial_fees_amount"] is None
        assert result["calculation_components"]["accessorial_fees"] == []
        ignored = result["calculation_components"]["ignored_accessorial_fees"]
        assert ignored[0]["label"] == "GRIS"
        assert ignored[0]["reason_code"] == "missing_audit_variable"


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
