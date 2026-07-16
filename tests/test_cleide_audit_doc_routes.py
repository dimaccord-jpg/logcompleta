"""Testes de API documental da Cleide Auditoria (Fase 2)."""
from __future__ import annotations

import importlib
import io
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.cleide_audit_doc_service import (
    AUDIT_BI_DATASET_VERSION,
    CLEIDE_AUDIT_DOC_IDS_SESSION_KEY,
    _public_audit_batch,
    _public_audit_bi,
)
from app.cleiton_doc_contracts import (
    ERROR_INVALID_EXTENSION,
    FIELD_PREPARED_CONTEXT,
    SESSION_KEY_CLEITON_DOC_IDS,
)
from app.services.cleide_audit_config_service import (
    CleideAuditConfig,
    DEFAULT_FALLBACK_MESSAGE,
)
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
        "audited_file_max_bytes": None,
        "audited_file_max_rows": 2000,
    }
    defaults.update(overrides)
    return CleideAuditConfig(**defaults)


def _patch_audit_cfg(monkeypatch, **overrides):
    cfg = _default_audit_cfg(**overrides)
    targets = [
        "app.cleide_audit_routes.get_cleide_audit_config",
        "app.cleide_audit_doc_context.get_cleide_audit_config",
        "app.cleide_audit_doc_service.get_cleide_audit_config",
        "app.run_cleide_audit_chat.get_cleide_audit_config",
    ]
    for target in targets:
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
    fake_user = SimpleNamespace(is_authenticated=True, conta_id=1, franquia_id=1)
    monkeypatch.setattr(web, "current_user", fake_user)
    monkeypatch.setattr("app.cleide_audit_routes.current_user", fake_user)
    monkeypatch.setattr("app.julia_documents_routes.current_user", fake_user)
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
    monkeypatch.setattr(
        "app.julia_documents_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _u: authz_payload,
    )


def _upload(client, filename: str, content: bytes, mime: str = "text/plain"):
    return client.post(
        "/api/cleide-auditoria/documents/upload",
        data={"file": (io.BytesIO(content), filename, mime)},
        content_type="multipart/form-data",
    )


def _post_temp_table_save(client, payload: dict):
    return client.post(
        "/api/cleide-auditoria/temp-table/save",
        json=payload,
        content_type="application/json",
    )


@pytest.fixture
def web_client(app, tmp_path, monkeypatch, ctx):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    _authorized(monkeypatch, web)
    web.app.config["TESTING"] = True
    return web.app.test_client()


def test_anonymous_receives_401_on_all_endpoints(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    anon = SimpleNamespace(is_authenticated=False)
    monkeypatch.setattr(web, "current_user", anon)
    monkeypatch.setattr("app.cleide_audit_routes.current_user", anon)
    client = web.app.test_client()

    assert client.get("/api/cleide-auditoria/documents/status").status_code == 401
    assert client.post("/api/cleide-auditoria/documents/clear").status_code == 401
    assert client.delete("/api/cleide-auditoria/documents/doc-id").status_code == 401
    assert _upload(client, "a.txt", make_txt("x")).status_code == 401
    assert client.post(
        "/api/cleide-auditoria/chat",
        json={"message": "auditar"},
        content_type="application/json",
    ).status_code == 401
    assert _post_temp_table_save(client, {"temp_table_id": "x", "edit_target": {}}).status_code == 401
    assert client.post("/api/cleide-auditoria/audit/run", json={}).status_code == 401
    assert client.post(
        "/api/cleide-auditoria/audit/correction/preview",
        json={"suggestion_id": "sug_1"},
    ).status_code == 401
    assert client.post(
        "/api/cleide-auditoria/audit/correction/apply",
        json={"preview_id": "prev_1", "suggestion_id": "sug_1"},
    ).status_code == 401
    assert client.post(
        "/api/cleide-auditoria/audit/correction/undo",
        json={"application_id": "corr_1"},
    ).status_code == 401

    body = client.get("/api/cleide-auditoria/documents/status").get_json()
    assert body["error_code"] == "auth_required"


def test_authorized_user_accesses_status(web_client):
    resp = web_client.get("/api/cleide-auditoria/documents/status")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["domain"] == "cleide_audit"
    assert "flow_types" in body
    assert "upload_enabled" in body
    assert "cleiton_upload_enabled" in body
    assert "cleide_audit_upload_enabled" in body
    assert body["upload_enabled"] is (
        body["cleiton_upload_enabled"] and body["cleide_audit_upload_enabled"]
    )
    assert isinstance(body["allowed_formats"], list)
    assert isinstance(body["calculation_bases"], list)


def test_status_returns_only_active_calculation_bases(web_client, monkeypatch):
    _patch_audit_cfg(
        monkeypatch,
        calculation_bases=[
            {
                "id": "inactive_base",
                "label": "base inativa",
                "aliases": [],
                "unit": "R$",
                "calculation_type": "fixed_amount",
                "audit_variable": None,
                "operation": "fixed_amount",
                "parameters": {},
                "is_active": False,
                "display_order": 1,
            },
            {
                "id": "pct_nota_fiscal",
                "label": "% por nota fiscal",
                "aliases": ["valor nf"],
                "unit": "%",
                "calculation_type": "invoice_percentage",
                "audit_variable": "valor_nf",
                "operation": "percentage_of_variable",
                "parameters": {},
                "is_active": True,
                "display_order": 2,
            },
        ],
    )
    body = web_client.get("/api/cleide-auditoria/documents/status").get_json()
    ids = [base["id"] for base in body["calculation_bases"]]
    assert ids == ["pct_nota_fiscal"]
    assert body["calculation_bases"][0]["parameters"] == {}
    assert body["calculation_bases"][0]["audit_variable"] == "valor_nf"


def test_correction_preview_route_accepts_only_suggestion_id(web_client, monkeypatch):
    preview_payload = {
        "preview_id": "prev_1",
        "suggestion_id": "sug_1",
        "generated_at": "2026-07-07T19:00:00+00:00",
        "expires_at": "2026-07-07T19:10:00+00:00",
        "before": {"summary": {}, "diagnostic_totals": {}},
        "after": {"summary": {}, "diagnostic_totals": {}},
        "delta": {
            "resolved_errors": 1,
            "remaining_errors": 0,
            "new_errors": 0,
            "changed_rows": 1,
            "new_ok": 1,
            "new_divergent": 0,
        },
        "regressions": [],
        "sample_changes": [],
        "remaining_errors": [],
        "safe_to_apply": True,
        "safety_reasons": [],
    }
    preview_mock = MagicMock(return_value=preview_payload)
    monkeypatch.setattr("app.cleide_audit_routes.preview_audit_correction_for_session", preview_mock)

    resp = web_client.post(
        "/api/cleide-auditoria/audit/correction/preview",
        json={
            "suggestion_id": "sug_1",
            "transformation": {"type": "select_pricing_dimension", "parameters": {"candidate_column": "ignored"}},
        },
    )

    assert resp.status_code == 200
    assert resp.get_json()["preview"] == preview_payload
    preview_mock.assert_called_once()
    assert preview_mock.call_args.args == ("sug_1",)


def test_correction_apply_route_accepts_only_preview_identifiers(web_client, monkeypatch):
    applied_payload = {
        "application_id": "corr_1",
        "temp_table": {"temp_table_id": "tt-test", "audit_correction": {"can_undo": True}},
    }
    apply_mock = MagicMock(return_value=applied_payload)
    monkeypatch.setattr("app.cleide_audit_routes.apply_audit_correction_for_session", apply_mock)

    resp = web_client.post(
        "/api/cleide-auditoria/audit/correction/apply",
        json={
            "preview_id": "prev_1",
            "suggestion_id": "sug_1",
            "transformation": {"parameters": {"candidate_column": "ignored"}},
        },
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["application_id"] == "corr_1"
    assert body["temp_table"]["audit_correction"]["can_undo"] is True
    apply_mock.assert_called_once()
    assert apply_mock.call_args.kwargs["preview_id"] == "prev_1"
    assert apply_mock.call_args.kwargs["suggestion_id"] == "sug_1"


def test_correction_undo_route_accepts_application_id(web_client, monkeypatch):
    undo_payload = {
        "undone_application_id": "corr_1",
        "temp_table": {"temp_table_id": "tt-test", "audit_correction": {"can_undo": False}},
    }
    undo_mock = MagicMock(return_value=undo_payload)
    monkeypatch.setattr("app.cleide_audit_routes.undo_last_audit_correction_for_session", undo_mock)

    resp = web_client.post(
        "/api/cleide-auditoria/audit/correction/undo",
        json={"application_id": "corr_1"},
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["undone_application_id"] == "corr_1"
    assert body["temp_table"]["audit_correction"]["can_undo"] is False
    undo_mock.assert_called_once()
    assert undo_mock.call_args.kwargs["application_id"] == "corr_1"


def test_status_exposes_combined_upload_flags(web_client, monkeypatch):
    _patch_audit_cfg(monkeypatch, upload_enabled=False)
    body = web_client.get("/api/cleide-auditoria/documents/status").get_json()
    assert body["cleiton_upload_enabled"] is True
    assert body["cleide_audit_upload_enabled"] is False
    assert body["upload_enabled"] is False


def test_status_upload_effective_requires_both_flags_enabled(web_client, monkeypatch):
    _patch_audit_cfg(monkeypatch, upload_enabled=True)
    body = web_client.get("/api/cleide-auditoria/documents/status").get_json()
    assert body["cleiton_upload_enabled"] is True
    assert body["cleide_audit_upload_enabled"] is True
    assert body["upload_enabled"] is True


def test_status_reflects_cleiton_global_upload_disabled(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path, upload_enabled=False)
    _patch_audit_cfg(monkeypatch, upload_enabled=True)
    web = _load_web_module()
    _authorized(monkeypatch, web)
    body = web.app.test_client().get("/api/cleide-auditoria/documents/status").get_json()
    assert body["cleiton_upload_enabled"] is False
    assert body["cleide_audit_upload_enabled"] is True
    assert body["upload_enabled"] is False


def test_blocked_user_receives_403(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    upgrade_cta = {
        "error_code": "plan_limit_reached",
        "message": "Você atingiu o limite de uso do plano Free. Não pare agora! ",
        "message_suffix": " e continue criando sem interrupções.",
        "upgrade_url": "/contrate-um-plano",
        "upgrade_label": "Faça o upgrade",
    }
    _authorized(
        monkeypatch,
        web,
        authz={
            "permitido": False,
            "modo_operacao": "blocked",
            "mensagem_usuario": "Bloqueado.",
            "upgrade_cta": upgrade_cta,
        },
    )
    client = web.app.test_client()
    resp = client.get("/api/cleide-auditoria/documents/status")
    assert resp.status_code == 403
    body = resp.get_json()
    assert body["error_code"] == "franquia_blocked"
    assert body["message"] == "Bloqueado."
    assert "authorization" in body
    assert body["authorization"]["upgrade_cta"] == upgrade_cta


def test_blocked_user_403_preserves_upgrade_cta_on_upload(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    upgrade_cta = {
        "error_code": "plan_limit_reached",
        "message": "Você atingiu o limite de uso do plano Free. Não pare agora! ",
        "message_suffix": " e continue criando sem interrupções.",
        "upgrade_url": "/contrate-um-plano",
        "upgrade_label": "Faça o upgrade",
    }
    _authorized(
        monkeypatch,
        web,
        authz={
            "permitido": False,
            "modo_operacao": "blocked",
            "mensagem_usuario": "Limite atingido.",
            "upgrade_cta": upgrade_cta,
        },
    )
    client = web.app.test_client()
    resp = _upload(client, "a.txt", make_txt("x"))
    assert resp.status_code == 403
    body = resp.get_json()
    assert body["error_code"] == "franquia_blocked"
    assert body["authorization"]["upgrade_cta"]["upgrade_url"] == "/contrate-um-plano"
    assert body["authorization"]["upgrade_cta"]["upgrade_label"] == "Faça o upgrade"


def test_expired_user_receives_403(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    _authorized(
        monkeypatch,
        web,
        authz={
            "permitido": False,
            "modo_operacao": "blocked",
            "status_franquia": "expired",
            "mensagem_usuario": "Plano expirado.",
        },
    )
    client = web.app.test_client()
    resp = _upload(client, "a.txt", make_txt("x"))
    assert resp.status_code == 403
    assert resp.get_json()["error_code"] == "franquia_blocked"


def test_degraded_user_allowed_by_current_policy(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    _authorized(
        monkeypatch,
        web,
        authz={"permitido": True, "modo_operacao": "degraded", "status_franquia": "degraded"},
    )
    client = web.app.test_client()
    resp = _upload(client, "a.txt", make_txt("degraded ok"))
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_upload_valid_txt(web_client):
    resp = _upload(web_client, "nota.txt", make_txt("conteudo seguro"))
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["document"]["doc_id"]
    assert body["session"]["count"] == 1
    assert isinstance(body["allowed_formats"], list)
    assert FIELD_PREPARED_CONTEXT not in body["document"]


def test_upload_valid_xml(web_client):
    content = make_xml('<?xml version="1.0"?><root><item>ok</item></root>')
    resp = _upload(web_client, "dados.xml", content, "application/xml")
    assert resp.status_code == 200
    assert resp.get_json()["document"]["doc_type"] == "xml"


def test_upload_valid_csv(web_client):
    content = make_csv([["col_a", "col_b"], ["1", "2"]])
    resp = _upload(web_client, "dados.csv", content, "text/csv")
    assert resp.status_code == 200
    assert resp.get_json()["document"]["doc_type"] == "csv"


def test_upload_valid_xlsx(web_client):
    content = make_xlsx([["a", "b"], ["1", "2"]])
    resp = _upload(
        web_client,
        "planilha.xlsx",
        content,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    assert resp.status_code == 200
    assert resp.get_json()["document"]["doc_type"] == "xlsx"


def test_upload_valid_docx(web_client):
    content = make_docx(["paragrafo um", "paragrafo dois"])
    resp = _upload(
        web_client,
        "relatorio.docx",
        content,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert resp.status_code == 200
    assert resp.get_json()["document"]["doc_type"] == "docx"


def test_upload_valid_pdf_with_mock(web_client, monkeypatch):
    patch_gemini_pdf_upload(monkeypatch)
    resp = _upload(web_client, "doc.pdf", make_minimal_pdf(), "application/pdf")
    assert resp.status_code == 200
    assert resp.get_json()["document"]["doc_type"] == "pdf"


def test_upload_rejects_forbidden_extension(web_client):
    resp = _upload(web_client, "malware.exe", b"payload")
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["ok"] is False
    assert body["error_code"] == ERROR_INVALID_EXTENSION


def test_status_reads_only_cleide_audit_doc_ids(web_client):
    _upload(web_client, "audit.txt", make_txt("somente audit"))
    with web_client.session_transaction() as sess:
        assert len(sess.get(CLEIDE_AUDIT_DOC_IDS_SESSION_KEY) or []) == 1
        assert sess.get(SESSION_KEY_CLEITON_DOC_IDS) in (None, [])

    resp = web_client.get("/api/cleide-auditoria/documents/status")
    assert resp.status_code == 200
    assert len(resp.get_json()["documents"]) == 1


def test_status_does_not_read_julia_session(web_client):
    web_client.post(
        "/api/julia/documents/upload",
        data={"file": (io.BytesIO(make_txt("julia")), "julia.txt", "text/plain")},
        content_type="multipart/form-data",
    )
    with web_client.session_transaction() as sess:
        assert len(sess.get(SESSION_KEY_CLEITON_DOC_IDS) or []) == 1
        assert sess.get(CLEIDE_AUDIT_DOC_IDS_SESSION_KEY) in (None, [])

    resp = web_client.get("/api/cleide-auditoria/documents/status")
    assert resp.status_code == 200
    assert resp.get_json()["session"]["count"] == 0
    assert resp.get_json()["documents"] == []


def test_delete_removes_only_cleide_audit_document(web_client):
    web_client.post(
        "/api/julia/documents/upload",
        data={"file": (io.BytesIO(make_txt("julia")), "julia.txt", "text/plain")},
        content_type="multipart/form-data",
    )
    up = _upload(web_client, "audit.txt", make_txt("audit")).get_json()
    doc_id = up["document"]["doc_id"]

    resp = web_client.delete(f"/api/cleide-auditoria/documents/{doc_id}")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["removed_from_session"] is True
    assert body["session"]["count"] == 0

    julia_list = web_client.get("/api/julia/documents").get_json()
    assert julia_list["session"]["count"] == 1


def test_clear_only_cleide_audit_documents(web_client, tmp_path):
    web_client.post(
        "/api/julia/documents/upload",
        data={"file": (io.BytesIO(make_txt("julia")), "julia.txt", "text/plain")},
        content_type="multipart/form-data",
    )
    up = _upload(web_client, "audit.txt", make_txt("audit")).get_json()
    doc_id = up["document"]["doc_id"]

    resp = web_client.post("/api/cleide-auditoria/documents/clear")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["removed_from_session"] == 1
    assert body["session"]["count"] == 0
    assert not (tmp_path / f"{doc_id}.json").exists()

    julia_list = web_client.get("/api/julia/documents").get_json()
    assert julia_list["session"]["count"] == 1


def test_blueprint_exposes_cleide_auditoria_routes():
    import app.cleide_audit_routes as routes_mod

    assert routes_mod.cleide_audit_documents_upload.__name__ == "cleide_audit_documents_upload"
    assert routes_mod.cleide_audit_documents_status.__name__ == "cleide_audit_documents_status"
    assert routes_mod.cleide_audit_documents_delete.__name__ == "cleide_audit_documents_delete"
    assert routes_mod.cleide_audit_documents_clear.__name__ == "cleide_audit_documents_clear"
    assert routes_mod.cleide_audit_chat.__name__ == "cleide_audit_chat"
    assert routes_mod.cleide_audit_coverage_upload.__name__ == "cleide_audit_coverage_upload"
    assert routes_mod.cleide_audit_batch_upload.__name__ == "cleide_audit_batch_upload"
    assert routes_mod.cleide_audit_batch_run.__name__ == "cleide_audit_batch_run"
    assert routes_mod.cleide_audit_template_download.__name__ == "cleide_audit_template_download"


def test_registered_routes_use_cleide_auditoria_namespace(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    rules = {rule.rule for rule in web.app.url_map.iter_rules()}
    assert "/api/cleide-auditoria/documents/upload" in rules
    assert "/api/cleide-auditoria/documents/status" in rules
    assert "/api/cleide-auditoria/documents/clear" in rules
    assert "/api/cleide-auditoria/chat" in rules
    assert "/api/cleide-auditoria/coverage/upload" in rules
    assert "/api/cleide-auditoria/audit/upload" in rules
    assert "/api/cleide-auditoria/audit/run" in rules
    assert "/api/cleide-auditoria/audit-template" in rules
    assert not any(rule.startswith("/api/cleide/documents") for rule in rules)


def test_chat_endpoint_registered_in_cleide_auditoria_namespace(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    rules = {rule.rule for rule in web.app.url_map.iter_rules()}
    assert "/api/cleide-auditoria/chat" in rules


def test_no_ia_called_on_upload(web_client, monkeypatch):
    chat_mock = MagicMock()
    monkeypatch.setattr("app.run_cleide_audit_chat.cleiton_governed_generate_content", chat_mock)
    payload = {"status": "awaiting_validation", "extracted_items": []}

    class _Resp:
        text = __import__("json").dumps(payload)

    extraction_mock = MagicMock(return_value=_Resp())
    monkeypatch.setattr(
        "app.run_cleide_audit_temp_table.cleiton_governed_generate_content",
        extraction_mock,
    )
    monkeypatch.setattr("app.run_cleide_audit_temp_table._get_client", lambda: object())
    resp = _upload(web_client, "nota.txt", make_txt("sem ia no chat"))
    assert resp.status_code == 200
    chat_mock.assert_not_called()
    extraction_mock.assert_called()


def test_julia_documents_still_work(web_client):
    resp = web_client.post(
        "/api/julia/documents/upload",
        data={"file": (io.BytesIO(make_txt("julia ok")), "julia.txt", "text/plain")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    assert resp.get_json()["session"]["count"] == 1


def test_audit_upload_disabled_blocks_upload_only(web_client, monkeypatch):
    _patch_audit_cfg(monkeypatch, upload_enabled=False)
    resp = _upload(web_client, "nota.txt", make_txt("bloqueado"))
    assert resp.status_code == 403
    assert resp.get_json()["error_code"] == "cleiton_doc_upload_disabled"


def test_audit_upload_disabled_does_not_block_status(web_client, monkeypatch):
    _patch_audit_cfg(monkeypatch, upload_enabled=False)
    resp = web_client.get("/api/cleide-auditoria/documents/status")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_audit_upload_disabled_does_not_block_delete_and_clear(web_client, monkeypatch):
    _patch_audit_cfg(monkeypatch, upload_enabled=True)
    up = _upload(web_client, "audit.txt", make_txt("audit")).get_json()
    doc_id = up["document"]["doc_id"]
    _patch_audit_cfg(monkeypatch, upload_enabled=False)

    delete_resp = web_client.delete(f"/api/cleide-auditoria/documents/{doc_id}")
    assert delete_resp.status_code == 200

    up2 = _upload(web_client, "outro.txt", make_txt("outro"))
    assert up2.status_code == 403

    _patch_audit_cfg(monkeypatch, upload_enabled=True)
    up3 = _upload(web_client, "terceiro.txt", make_txt("terceiro"))
    assert up3.status_code == 200
    _patch_audit_cfg(monkeypatch, upload_enabled=False)

    clear_resp = web_client.post("/api/cleide-auditoria/documents/clear")
    assert clear_resp.status_code == 200


def test_cleiton_global_upload_disabled_still_blocks_even_if_audit_enabled(
    app, ctx, monkeypatch, tmp_path
):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path, upload_enabled=False)
    _patch_audit_cfg(monkeypatch, upload_enabled=True)
    web = _load_web_module()
    _authorized(monkeypatch, web)
    client = web.app.test_client()
    resp = _upload(client, "nota.txt", make_txt("bloqueado global"))
    assert resp.status_code == 403
    assert resp.get_json()["error_code"] == "cleiton_doc_upload_disabled"


def test_chat_disabled_does_not_block_document_status(web_client, monkeypatch):
    _patch_audit_cfg(monkeypatch, chat_enabled=False, upload_enabled=True)
    resp = web_client.get("/api/cleide-auditoria/documents/status")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_chat_disabled_does_not_block_upload(web_client, monkeypatch):
    _patch_audit_cfg(monkeypatch, chat_enabled=False, upload_enabled=True)
    resp = _upload(web_client, "nota.txt", make_txt("upload com chat desligado"))
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_chat_disabled_does_not_block_delete_and_clear(web_client, monkeypatch):
    _patch_audit_cfg(monkeypatch, chat_enabled=True, upload_enabled=True)
    doc_id = _upload(web_client, "audit.txt", make_txt("audit")).get_json()["document"]["doc_id"]
    _patch_audit_cfg(monkeypatch, chat_enabled=False, upload_enabled=True)

    delete_resp = web_client.delete(f"/api/cleide-auditoria/documents/{doc_id}")
    assert delete_resp.status_code == 200

    _upload(web_client, "outro.txt", make_txt("outro"))
    clear_resp = web_client.post("/api/cleide-auditoria/documents/clear")
    assert clear_resp.status_code == 200


def test_upload_route_reads_cleide_audit_config():
    import inspect

    import app.cleide_audit_routes as routes_mod

    source = inspect.getsource(routes_mod.cleide_audit_documents_upload)
    assert "get_cleide_audit_config" in source
    assert "upload_enabled" in source
    assert "get_cleide_config" not in source


def test_cleide_legacy_route_still_registered(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    rules = {rule.rule for rule in web.app.url_map.iter_rules()}
    assert "/cleide-bi-frete" in rules
    assert "/auditoria-frete" in rules


def test_audit_template_download_returns_xlsx(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    client = web.app.test_client()
    resp = client.get("/api/cleide-auditoria/audit-template")
    assert resp.status_code == 200
    assert (
        resp.headers.get("Content-Type")
        == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert resp.data.startswith(b"PK")


def test_cleide_bi_template_route_unchanged(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    client = web.app.test_client()
    resp = client.get("/api/cleide/template")
    assert resp.status_code == 200
    assert resp.data.startswith(b"PK")


def test_public_audit_batch_exposes_sanitized_audit_diagnostics():
    audit_batch = {
        "status": "processed",
        "row_count": 609,
        "results": [],
        "summary": {"total_rows": 609},
        "audit_diagnostics": {
            "has_errors": True,
            "total_errors": 609,
            "generated_at": "2026-07-07T19:30:00+00:00",
            "groups": [
                {
                    "code": "pricing_dimension_mismatch",
                    "title": "Dimensão tarifária incompatível",
                    "failure_stage": "pricing_rule_match",
                    "affected_rows": 609,
                    "sample_row_indexes": [1, 2, 3, 4, 5, 6],
                    "requested_values": ["CAPITAL", "INTERIOR 1", "", "CAPITAL"],
                    "available_values": ["SUL", "SUDESTE"],
                    "candidate_column": "RAIO",
                    "candidate_values": ["CAPITAL", "INTERIOR 1"],
                    "confidence": "high",
                    "message": "As regiões utilizadas na cobertura não coincidem.",
                    "evidence": [],
                    "actionability": {
                        "can_review_registered_table": True,
                        "can_apply_automatically": False,
                        "can_fix_source_files": True,
                        "internal_note": "não expor",
                    },
                    "internal_debug": "não expor",
                }
            ],
        },
    }

    payload = _public_audit_batch(audit_batch)

    diagnostics = payload["audit_diagnostics"]
    assert diagnostics == {
        "has_errors": True,
        "total_errors": 609,
        "generated_at": "2026-07-07T19:30:00+00:00",
        "groups": [
            {
                "code": "pricing_dimension_mismatch",
                "title": "Dimensão tarifária incompatível",
                "failure_stage": "pricing_rule_match",
                "affected_rows": 609,
                "sample_row_indexes": [1, 2, 3, 4, 5],
                "requested_values": ["CAPITAL", "INTERIOR 1"],
                "available_values": ["SUL", "SUDESTE"],
                "candidate_column": "RAIO",
                "candidate_values": ["CAPITAL", "INTERIOR 1"],
                "confidence": "high",
                "message": "As regiões utilizadas na cobertura não coincidem.",
                "evidence": [],
                "actionability": {
                    "can_review_registered_table": True,
                    "can_apply_automatically": False,
                    "can_fix_source_files": True,
                },
            }
        ],
    }
    assert "audit_diagnostics" in payload


def test_public_audit_bi_not_ready_without_normalized_rows():
    payload = _public_audit_bi(None)
    assert payload["dataset_version"] == AUDIT_BI_DATASET_VERSION
    assert payload["ready"] is False
    assert payload["row_count"] == 0
    assert payload["rows"] == []
    assert payload["message"]


def test_public_audit_bi_ready_with_normalized_rows_without_results():
    audit_batch = {
        "normalized_rows": [
            {
                "row_index": 1,
                "carrier": "Transportadora X",
                "origin_uf": "SP",
                "destination_uf": "PR",
                "issue_date": "2024-01-01",
                "audited_weight": 48.0,
                "charged_freight": 100.5,
                "document_number": "123",
            }
        ]
    }
    payload = _public_audit_bi(audit_batch)
    assert payload["ready"] is True
    assert payload["row_count"] == 1
    row = payload["rows"][0]
    assert row["carrier"] == "Transportadora X"
    assert row["expected_freight"] is None
    assert row["divergence_value"] is None
    assert row["status"] is None
    assert "document_number" not in row


def test_public_audit_bi_ready_with_sanitized_rows():
    audit_batch = {
        "normalized_rows": [
            {
                "row_index": 1,
                "carrier": "Transportadora X",
                "origin_uf": "SP",
                "destination_uf": "PR",
                "destination_city": "Curitiba",
                "issue_date": "2024-01-01",
                "audited_weight": 48.0,
                "charged_freight": 100.5,
                "document_number": "123",
            }
        ],
        "results": [
            {
                "row_index": 1,
                "expected_freight": 100.5,
                "divergence_value": 0.0,
                "status": "ok",
                "calculation_details": "secret",
            }
        ],
    }
    payload = _public_audit_bi(audit_batch)
    assert payload["ready"] is True
    assert payload["row_count"] == 1
    row = payload["rows"][0]
    assert row["expected_freight"] == 100.5
    assert row["status"] == "ok"
    assert "destination_city" not in row
    assert "document_number" not in row
    assert "calculation_details" not in payload
