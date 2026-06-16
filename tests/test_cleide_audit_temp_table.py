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
    should_attempt_temp_table_extraction,
    split_temp_table_block_from_answer,
    temp_table_status_message,
)
from app.cleide_audit_prompt import build_cleide_audit_temp_table_technical_prompt
from app.services.cleide_audit_config_service import CleideAuditConfig, DEFAULT_FALLBACK_MESSAGE
from tests.cleiton_doc_fixtures import (
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
    }
    defaults.update(overrides)
    return CleideAuditConfig(**defaults)


def _patch_audit_cfg(monkeypatch, **overrides):
    cfg = _default_audit_cfg(**overrides)
    for target in (
        "app.cleide_audit_routes.get_cleide_audit_config",
        "app.cleide_audit_doc_context.get_cleide_audit_config",
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


def test_no_new_routes(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    rules = {rule.rule for rule in web.app.url_map.iter_rules()}
    assert "/api/cleide-auditoria/documents/status" in rules
    assert not any("temp-table" in rule for rule in rules)
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
            return apply_temp_table_extraction_from_model_payload(payload, source_doc_ids=["doc-1"])


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
