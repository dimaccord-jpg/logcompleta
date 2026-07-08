"""Testes da base interna de prompt e contexto documental da Cleide Auditoria (Fase 1)."""
from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.cleide_audit_doc_context as audit_ctx
import app.cleide_audit_doc_service as audit_svc
import app.cleide_audit_prompt as audit_prompt
import app.cleiton_doc_service as cleiton_svc
from app.cleide_audit_doc_service import CLEIDE_AUDIT_CHAT_FLOW_TYPE
from app.cleiton_doc_contracts import get_cleiton_doc_ids
from app.cleide_audit_prompt import (
    build_cleide_audit_document_guidance,
    build_cleide_audit_system_prompt,
)
from app.services.cleide_audit_config_service import (
    CleideAuditConfig,
    DEFAULT_FALLBACK_MESSAGE,
)
from tests.cleiton_doc_fixtures import make_txt, patch_cleiton_doc_cfg, patch_cleiton_doc_store


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


def _patch_cleide_audit_cfg(monkeypatch, **overrides):
    cfg = _default_audit_cfg(**overrides)
    monkeypatch.setattr("app.cleide_audit_doc_context.get_cleide_audit_config", lambda: cfg)
    return cfg


def _patch_audit_cfg(monkeypatch, **overrides):
    cfg = patch_cleiton_doc_cfg(monkeypatch, **overrides)
    monkeypatch.setattr("app.cleide_audit_doc_context.get_cleiton_doc_config", lambda: cfg)
    monkeypatch.setattr("app.cleide_audit_doc_service.get_cleiton_doc_config", lambda: cfg)
    _patch_cleide_audit_cfg(monkeypatch)
    return cfg


@pytest.fixture
def session_app(app, tmp_path, monkeypatch, ctx):
    patch_cleiton_doc_store(tmp_path, monkeypatch)
    _patch_audit_cfg(monkeypatch)
    app.config["SECRET_KEY"] = "test-secret"
    return app


def test_empty_session_has_no_documents(session_app, monkeypatch):
    chat_mock = MagicMock()
    gemini_mock = MagicMock()
    monkeypatch.setattr(
        "app.run_cleiton_gemini_governance.cleiton_governed_generate_content",
        chat_mock,
    )
    monkeypatch.setattr(
        "app.cleiton_doc_gemini_files.get_cleiton_gemini_client",
        gemini_mock,
    )

    with session_app.test_request_context("/"):
        from flask import session

        result = audit_ctx.build_cleide_audit_document_context_for_chat(session)

    assert result["has_documents"] is False
    assert result["context_block"] == ""
    assert result["flow_type"] == CLEIDE_AUDIT_CHAT_FLOW_TYPE
    assert result["meta"]["files_considered"] == 0
    assert result["gemini_file_parts"] == []
    chat_mock.assert_not_called()
    gemini_mock.assert_not_called()


def test_with_cleide_documents_builds_context(session_app):
    with session_app.test_request_context("/"):
        from flask import session

        audit_svc.prepare_and_register_document(
            display_name="cte-auditoria.txt",
            file_bytes=make_txt("valor frete SP-RJ: 1250.00"),
            mime_type="text/plain",
            extension=".txt",
        )
        result = audit_ctx.build_cleide_audit_document_context_for_chat(session)

    assert result["has_documents"] is True
    assert result["meta"]["files_considered"] == 1
    assert result["meta"]["files_total_active"] == 1
    assert "valor frete SP-RJ: 1250.00" in result["context_block"]
    assert "Cleide Auditoria" in result["context_block"]
    assert result["meta"]["documents"][0]["display_name"] == "cte-auditoria.txt"


def test_pdf_ready_enters_gemini_file_parts(session_app, monkeypatch):
    monkeypatch.setattr(
        "app.cleide_audit_doc_service.upload_pdf_to_gemini_files_api",
        lambda **kwargs: SimpleNamespace(
            ok=True,
            gemini_file_name="files/abc123",
            gemini_file_uri="gs://bucket/abc123",
            gemini_mime_type="application/pdf",
            gemini_file_state="ACTIVE",
            gemini_uploaded_at="2026-06-09T10:00:00",
            prepared_context='{"strategy":"gemini_file_api","gemini_file_ready":true}',
            warnings=[],
            error_summary=None,
        ),
    )
    monkeypatch.setattr(
        "app.cleide_audit_doc_context.build_gemini_file_part_for_generate",
        lambda record: {"file_name": record.get("gemini_file_name")},
    )

    with session_app.test_request_context("/"):
        from flask import session

        audit_svc.prepare_and_register_document(
            display_name="tabela.pdf",
            file_bytes=b"%PDF-1.4\n/Type /Page\nconteudo",
            mime_type="application/pdf",
            extension=".pdf",
        )
        result = audit_ctx.build_cleide_audit_document_context_for_chat(session)

    assert result["has_documents"] is True
    assert result["meta"]["pdf_files_ready"] == 1
    assert result["gemini_file_parts"] == [{"file_name": "files/abc123"}]
    assert "fase futura" not in result["context_block"].lower()


def test_pdf_sem_texto_gera_orientacao_honesta_no_contexto(session_app, monkeypatch):
    monkeypatch.setattr(
        "app.cleide_audit_doc_service.upload_pdf_to_gemini_files_api",
        lambda **kwargs: SimpleNamespace(
            ok=False,
            gemini_file_name=None,
            gemini_file_uri=None,
            gemini_mime_type=None,
            gemini_file_state=None,
            gemini_uploaded_at=None,
            prepared_context='{"strategy":"gemini_file_api","gemini_file_error":true}',
            warnings=[],
            error_summary="gemini_client_unavailable",
        ),
    )

    with session_app.test_request_context("/"):
        from flask import session

        audit_svc.prepare_and_register_document(
            display_name="scan.pdf",
            file_bytes=b"%PDF-1.4\n/Type /Page\nimagem",
            mime_type="application/pdf",
            extension=".pdf",
        )
        result = audit_ctx.build_cleide_audit_document_context_for_chat(session)

    lowered = result["context_block"].lower()
    assert "nao ficou legivel" in lowered or "nao consegui extrair" in lowered
    assert "excel, csv ou pdf com texto selecionavel" in lowered
    assert "fase futura multimodal" not in lowered


def test_respects_max_chars(session_app, monkeypatch):
    _patch_cleide_audit_cfg(monkeypatch, document_context_max_chars=120)
    with session_app.test_request_context("/"):
        from flask import session

        audit_svc.prepare_and_register_document(
            display_name="longo.txt",
            file_bytes=make_txt("A" * 500),
            mime_type="text/plain",
            extension=".txt",
        )
        result = audit_ctx.build_cleide_audit_document_context_for_chat(session)

    assert result["has_documents"] is True
    assert result["meta"]["context_truncated"] is True
    assert len(result["context_block"]) <= 120


def test_respects_max_files_considered(session_app, monkeypatch):
    _patch_audit_cfg(monkeypatch, max_files_per_session=5)
    _patch_cleide_audit_cfg(monkeypatch, max_documents_considered=1)
    with session_app.test_request_context("/"):
        from flask import session

        audit_svc.prepare_and_register_document(
            display_name="primeiro.txt",
            file_bytes=make_txt("DOC-UM"),
            mime_type="text/plain",
            extension=".txt",
        )
        audit_svc.prepare_and_register_document(
            display_name="segundo.txt",
            file_bytes=make_txt("DOC-DOIS"),
            mime_type="text/plain",
            extension=".txt",
        )
        result = audit_ctx.build_cleide_audit_document_context_for_chat(session)

    assert result["meta"]["files_considered"] == 1
    assert "DOC-UM" not in result["context_block"]
    assert "DOC-DOIS" in result["context_block"]


def test_julia_documents_are_not_used(session_app):
    with session_app.test_request_context("/"):
        from flask import session

        cleiton_svc.prepare_and_register_document(
            display_name="julia-only.txt",
            file_bytes=make_txt("conteudo julia"),
            mime_type="text/plain",
            extension=".txt",
        )
        audit_svc.prepare_and_register_document(
            display_name="cleide-only.txt",
            file_bytes=make_txt("conteudo cleide"),
            mime_type="text/plain",
            extension=".txt",
        )
        assert get_cleiton_doc_ids(session)
        assert audit_svc.get_cleide_audit_doc_ids(session)
        result = audit_ctx.build_cleide_audit_document_context_for_chat(session)

    assert result["has_documents"] is True
    assert "conteudo cleide" in result["context_block"]
    assert "conteudo julia" not in result["context_block"]
    assert result["meta"]["files_considered"] == 1
    assert result["meta"]["documents"][0]["display_name"] == "cleide-only.txt"


def test_system_prompt_identity_and_rules():
    prompt = build_cleide_audit_system_prompt()
    assert "Cleide" in prompt
    assert "Auditora Virtual de AgenteFrete" in prompt
    assert "Não invente" in prompt or "não invente" in prompt.lower()
    assert "template fixo" in prompt.lower() or "padrão rígido" in prompt.lower()
    assert "Júlia" in prompt
    assert "Roberto" in prompt
    assert "BI Cleide" in prompt
    assert "insuficiente" in prompt.lower() or "faltam" in prompt.lower() or "falta" in prompt.lower()


def test_document_guidance_covers_insufficiency():
    guidance = build_cleide_audit_document_guidance()
    assert "Cleide Auditoria" in guidance or "auditoria" in guidance.lower()
    assert "não invente" in guidance.lower()
    assert "template fixo" in guidance.lower() or "formato rígido" in guidance.lower()
    assert "Júlia" in guidance
    assert "Roberto" in guidance
    assert "insuficiente" in guidance.lower() or "adicionais" in guidance.lower()


def test_context_does_not_expose_internal_paths(session_app, tmp_path):
    with session_app.test_request_context("/"):
        from flask import session

        audit_svc.prepare_and_register_document(
            display_name="nota.txt",
            file_bytes=make_txt("evidencia"),
            mime_type="text/plain",
            extension=".txt",
        )
        result = audit_ctx.build_cleide_audit_document_context_for_chat(session)

    block = result["context_block"]
    assert str(tmp_path) not in block
    assert "cleiton_doc_tmp" not in block
    assert ".json" not in block.lower() or "documento" in block.lower()


def test_context_module_does_not_call_ia_or_gemini(monkeypatch, session_app):
    chat_mock = MagicMock()
    gemini_client_mock = MagicMock()
    gemini_upload_mock = MagicMock()
    monkeypatch.setattr(
        "app.run_cleiton_gemini_governance.cleiton_governed_generate_content",
        chat_mock,
    )
    monkeypatch.setattr(
        "app.cleiton_doc_gemini_files.get_cleiton_gemini_client",
        lambda: gemini_client_mock,
    )
    monkeypatch.setattr(
        "app.cleide_audit_doc_service.upload_pdf_to_gemini_files_api",
        gemini_upload_mock,
    )

    with session_app.test_request_context("/"):
        from flask import session

        audit_svc.prepare_and_register_document(
            display_name="ctx.txt",
            file_bytes=make_txt("sem ia"),
            mime_type="text/plain",
            extension=".txt",
        )
        audit_ctx.build_cleide_audit_document_context_for_chat(session)

    chat_mock.assert_not_called()
    gemini_client_mock.assert_not_called()
    gemini_upload_mock.assert_not_called()


def test_chat_endpoint_lives_in_audit_routes_not_web():
    repo_root = Path(__file__).resolve().parents[1]
    routes_source = (repo_root / "app" / "cleide_audit_routes.py").read_text(encoding="utf-8")
    web_source = (repo_root / "app" / "web.py").read_text(encoding="utf-8")

    assert "/api/cleide-auditoria/chat" in routes_source
    assert "build_cleide_audit_document_context_for_chat" in routes_source
    assert "/api/cleide-auditoria/chat" not in web_source
    assert "build_cleide_audit_document_context_for_chat" not in web_source


def test_context_module_has_no_regex():
    source = inspect.getsource(audit_ctx)
    assert "import re" not in source
    assert "re.compile" not in source
    assert "re.search" not in source
    assert "re.match" not in source


def test_context_module_no_bi_cleide_coupling():
    source = inspect.getsource(audit_ctx)
    for token in (
        "/api/chat_cleide",
        "cleide_routes",
        "cleide_upload_pipeline",
        "chat_cleide",
    ):
        assert token not in source


def test_context_module_no_get_cleiton_doc_ids():
    source = inspect.getsource(audit_ctx)
    assert "get_cleiton_doc_ids" not in source


def test_context_module_isolated_from_julia():
    source = inspect.getsource(audit_ctx)
    assert "julia_doc_context" not in source
    assert "get_cleiton_doc_ids" not in source
    assert "JULIA_CHAT" not in source
    assert "build_julia_document_context_for_chat" not in source


def test_prompt_module_has_no_ia_calls():
    source = inspect.getsource(audit_prompt)
    assert "generate_content" not in source
    assert "gemini" not in source.lower()
    assert "cleiton_governed" not in source


def test_context_uses_cleide_audit_config_limits(session_app, monkeypatch):
    source = inspect.getsource(audit_ctx)
    assert "get_cleide_audit_config" in source
    assert "document_context_max_chars" in source
    assert "max_documents_considered" in source
    assert "get_cleide_config" not in source
    assert "cleide_cfg_" not in source


def test_audit_limits_respeitam_teto_cleiton_global(session_app, monkeypatch, ctx):
    from app.extensions import db
    from app.services.cleide_audit_config_service import get_cleide_audit_config, salvar_cleide_audit_config
    from app.services.cleiton_doc_config_service import salvar_cleiton_doc_config

    salvar_cleiton_doc_config(
        {
            "prompt_context_max_chars": "5000",
            "prompt_max_files_considered": "2",
        }
    )
    salvar_cleide_audit_config(
        {
            "document_context_max_chars": "20000",
            "max_documents_considered": "5",
        }
    )
    db.session.commit()

    cfg = get_cleide_audit_config()
    assert cfg.document_context_max_chars == 5000
    assert cfg.max_documents_considered == 2

    _patch_cleide_audit_cfg(
        monkeypatch,
        document_context_max_chars=cfg.document_context_max_chars,
        max_documents_considered=cfg.max_documents_considered,
    )
    with session_app.test_request_context("/"):
        from flask import session

        audit_svc.prepare_and_register_document(
            display_name="longo.txt",
            file_bytes=make_txt("A" * 8000),
            mime_type="text/plain",
            extension=".txt",
        )
        result = audit_ctx.build_cleide_audit_document_context_for_chat(session)

    assert result["meta"]["context_truncated"] is True
    assert len(result["context_block"]) <= 5000
