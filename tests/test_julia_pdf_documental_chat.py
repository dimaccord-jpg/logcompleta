"""Testes de chat documental com PDF real via Gemini File API."""
from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.julia_doc_context as doc_ctx
import app.run_julia_chat as julia_chat
from app.cleiton_doc_contracts import FLOW_TYPE_JULIA_CHAT_DOCUMENTAL
from app.julia_doc_context import build_julia_document_context_for_chat
from app.models import IaConsumoEvento
from app.run_julia_chat import (
    DOCUMENTAL_DEADLINE_REPLY,
    GENERIC_REPLY_FALLBACK,
    _get_chat_model_candidates,
    _get_chat_model_fallback,
    chat_julia_reply,
)
from tests.cleiton_doc_fixtures import make_minimal_pdf, make_txt, patch_cleiton_doc_cfg, patch_cleiton_doc_store


class _FakeUploadedFile:
    def __init__(self):
        self.name = "files/chat-pdf-1"
        self.uri = "https://generativelanguage.googleapis.com/v1beta/files/chat-pdf-1"
        self.mime_type = "application/pdf"
        self.state = "ACTIVE"


def _patch_gemini(monkeypatch):
    import app.cleiton_doc_gemini_files as gemini_files

    client = MagicMock()
    uploaded = _FakeUploadedFile()
    client.files.upload.return_value = uploaded
    client.files.get.return_value = uploaded
    client.files.delete.return_value = None
    monkeypatch.setattr(gemini_files, "get_cleiton_gemini_client", lambda: client)
    part = SimpleNamespace(uri=uploaded.uri, mime_type=uploaded.mime_type)
    monkeypatch.setattr(gemini_files, "build_gemini_file_part_for_generate", lambda _r, **k: part)
    return part


@pytest.fixture
def session_app(app, tmp_path, monkeypatch, ctx):
    patch_cleiton_doc_store(tmp_path, monkeypatch)
    patch_cleiton_doc_cfg(monkeypatch)
    _patch_gemini(monkeypatch)
    app.config["SECRET_KEY"] = "test-secret"
    return app


def test_chat_with_pdf_includes_file_parts_in_governed_call(session_app, monkeypatch):
    import app.cleiton_doc_service as svc

    _patch_gemini(monkeypatch)
    capture = {}

    class _Resp:
        text = "resposta pdf"

    def _fake(client, model, contents, agent, flow_type, api_key_label):
        capture["contents"] = contents
        capture["flow_type"] = flow_type
        return _Resp()

    monkeypatch.setattr(julia_chat, "cleiton_governed_generate_content", _fake)
    monkeypatch.setattr(julia_chat, "_get_client", lambda: object())

    with session_app.test_request_context("/"):
        svc.prepare_and_register_document(
            display_name="manual.pdf",
            file_bytes=make_minimal_pdf(),
            mime_type="application/pdf",
        )
        doc_ctx_result = build_julia_document_context_for_chat()
        chat_julia_reply(
            "Resuma o PDF",
            [],
            document_context_block=doc_ctx_result["context_block"],
            document_file_parts=doc_ctx_result["gemini_file_parts"],
            flow_type=doc_ctx_result["flow_type"],
        )

    assert capture["flow_type"] == FLOW_TYPE_JULIA_CHAT_DOCUMENTAL
    assert isinstance(capture["contents"], list)
    assert len(capture["contents"]) >= 2
    assert "PDF" in capture["contents"][-1]
    assert "File API pendente" not in capture["contents"][-1]


def test_run_julia_chat_does_not_read_pdf_files():
    source = inspect.getsource(julia_chat)
    assert ".pdf" not in source.lower() or "document_file_parts" in source
    assert "open(" not in source
    assert "read_bytes" not in source


def test_pdf_not_ready_no_file_parts(session_app, monkeypatch):
    import app.cleiton_doc_gemini_files as gemini_files
    import app.cleiton_doc_service as svc

    client = MagicMock()
    uploaded = _FakeUploadedFile()
    uploaded.state = "FAILED"
    client.files.upload.return_value = uploaded
    client.files.get.return_value = uploaded
    monkeypatch.setattr(gemini_files, "get_cleiton_gemini_client", lambda: client)

    with session_app.test_request_context("/"):
        svc.prepare_and_register_document(
            display_name="fail.pdf",
            file_bytes=make_minimal_pdf(),
            mime_type="application/pdf",
        )
        ctx_result = build_julia_document_context_for_chat()
    assert ctx_result["gemini_file_parts"] == []
    assert "não pôde ser preparado" in ctx_result["context_block"] or "indisponível" in ctx_result["context_block"]


def test_txt_chat_still_works(session_app, monkeypatch):
    import app.cleiton_doc_service as svc

    capture = {}

    class _Resp:
        text = "ok txt"

    def _fake(client, model, contents, agent, flow_type, api_key_label):
        capture["contents"] = contents
        capture["flow_type"] = flow_type
        return _Resp()

    monkeypatch.setattr(julia_chat, "cleiton_governed_generate_content", _fake)
    monkeypatch.setattr(julia_chat, "_get_client", lambda: object())

    with session_app.test_request_context("/"):
        svc.prepare_and_register_document(
            display_name="nota.txt",
            file_bytes=make_txt("conteudo textual"),
            mime_type="text/plain",
        )
        doc_ctx_result = build_julia_document_context_for_chat()
        chat_julia_reply(
            "analise",
            [],
            document_context_block=doc_ctx_result["context_block"],
            document_file_parts=doc_ctx_result["gemini_file_parts"],
            flow_type=doc_ctx_result["flow_type"],
        )

    assert capture["flow_type"] == FLOW_TYPE_JULIA_CHAT_DOCUMENTAL
    assert isinstance(capture["contents"], str)
    assert "conteudo textual" in capture["contents"]


class _DeadlineExceededError(Exception):
    pass


def _deadline_exc() -> Exception:
    return _DeadlineExceededError("504 DEADLINE_EXCEEDED")


def test_chat_model_candidates_never_use_gemini_15_flash(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL_TEXT", "gemini-2.5-flash")
    monkeypatch.delenv("JULIA_CHAT_MODEL_FALLBACK", raising=False)
    monkeypatch.delenv("GEMINI_MODEL_TEXT_FALLBACK", raising=False)
    candidates = _get_chat_model_candidates()
    assert "gemini-1.5-flash" not in candidates
    assert _get_chat_model_fallback() == "gemini-2.5-flash-lite"
    assert candidates[-1] == "gemini-2.5-flash-lite"


def test_chat_model_fallback_configurable(monkeypatch):
    monkeypatch.setenv("JULIA_CHAT_MODEL_FALLBACK", "gemini-2.5-pro")
    assert _get_chat_model_fallback() == "gemini-2.5-pro"
    candidates = _get_chat_model_candidates()
    assert "gemini-2.5-pro" in candidates
    assert "gemini-1.5-flash" not in candidates


def test_documental_deadline_returns_useful_message_not_generic(monkeypatch):
    attempts = []

    def _fake(_client, model, contents, agent, flow_type, api_key_label):
        attempts.append(model)
        raise _deadline_exc()

    monkeypatch.setattr(julia_chat, "cleiton_governed_generate_content", _fake)
    monkeypatch.setattr(julia_chat, "_get_client", lambda: object())
    part = SimpleNamespace(file_data=SimpleNamespace(file_uri="https://x", mime_type="application/pdf"))

    result = chat_julia_reply(
        "Compare os dois PDFs",
        [],
        document_context_block="ctx pdf",
        document_file_parts=[part, part],
        flow_type=FLOW_TYPE_JULIA_CHAT_DOCUMENTAL,
    )

    assert result["reply"] == DOCUMENTAL_DEADLINE_REPLY
    assert result["reply"] != GENERIC_REPLY_FALLBACK
    assert len(attempts) == 1
    assert "gemini-1.5-flash" not in attempts


def test_documental_deadline_does_not_expose_stack_or_gemini_refs(monkeypatch):
    monkeypatch.setattr(
        julia_chat,
        "cleiton_governed_generate_content",
        lambda *_a, **_k: (_ for _ in ()).throw(_deadline_exc()),
    )
    monkeypatch.setattr(julia_chat, "_get_client", lambda: object())
    part = SimpleNamespace(file_data=SimpleNamespace(file_uri="https://x", mime_type="application/pdf"))

    result = chat_julia_reply(
        "Compare preços",
        [],
        document_context_block="ctx",
        document_file_parts=[part],
        flow_type=FLOW_TYPE_JULIA_CHAT_DOCUMENTAL,
    )
    reply = result["reply"]
    assert "Traceback" not in reply
    assert "generativelanguage" not in reply.lower()
    assert "files/" not in reply
    assert "gemini_file" not in reply.lower()


def test_single_pdf_summary_still_works(session_app, monkeypatch):
    import app.cleiton_doc_service as svc

    class _Resp:
        text = "resumo do pdf"

    monkeypatch.setattr(julia_chat, "cleiton_governed_generate_content", lambda *_a, **_k: _Resp())
    monkeypatch.setattr(julia_chat, "_get_client", lambda: object())

    with session_app.test_request_context("/"):
        svc.prepare_and_register_document(
            display_name="one.pdf",
            file_bytes=make_minimal_pdf(),
            mime_type="application/pdf",
        )
        doc_ctx_result = build_julia_document_context_for_chat()
        result = chat_julia_reply(
            "Resuma o PDF",
            [],
            document_context_block=doc_ctx_result["context_block"],
            document_file_parts=doc_ctx_result["gemini_file_parts"],
            flow_type=doc_ctx_result["flow_type"],
        )
    assert result["reply"] == "resumo do pdf"


def test_two_pdf_summary_still_works(session_app, monkeypatch):
    import app.cleiton_doc_service as svc

    class _Resp:
        text = "resumo dos dois pdfs"

    monkeypatch.setattr(julia_chat, "cleiton_governed_generate_content", lambda *_a, **_k: _Resp())
    monkeypatch.setattr(julia_chat, "_get_client", lambda: object())

    with session_app.test_request_context("/"):
        svc.prepare_and_register_document(
            display_name="a.pdf",
            file_bytes=make_minimal_pdf(),
            mime_type="application/pdf",
        )
        svc.prepare_and_register_document(
            display_name="b.pdf",
            file_bytes=make_minimal_pdf(),
            mime_type="application/pdf",
        )
        doc_ctx_result = build_julia_document_context_for_chat()
        result = chat_julia_reply(
            "Resuma cada PDF",
            [],
            document_context_block=doc_ctx_result["context_block"],
            document_file_parts=doc_ctx_result["gemini_file_parts"],
            flow_type=doc_ctx_result["flow_type"],
        )
    assert result["reply"] == "resumo dos dois pdfs"
    assert len(doc_ctx_result["gemini_file_parts"]) == 2


def test_two_pdf_comparison_deadline_returns_useful_message(session_app, monkeypatch):
    import app.cleiton_doc_service as svc

    def _fake(_client, model, contents, agent, flow_type, api_key_label):
        raise _deadline_exc()

    monkeypatch.setattr(julia_chat, "cleiton_governed_generate_content", _fake)
    monkeypatch.setattr(julia_chat, "_get_client", lambda: object())

    with session_app.test_request_context("/"):
        svc.prepare_and_register_document(
            display_name="a.pdf",
            file_bytes=make_minimal_pdf(),
            mime_type="application/pdf",
        )
        svc.prepare_and_register_document(
            display_name="b.pdf",
            file_bytes=make_minimal_pdf(),
            mime_type="application/pdf",
        )
        doc_ctx_result = build_julia_document_context_for_chat()
        result = chat_julia_reply(
            "Compare diferenças entre os PDFs",
            [],
            document_context_block=doc_ctx_result["context_block"],
            document_file_parts=doc_ctx_result["gemini_file_parts"],
            flow_type=doc_ctx_result["flow_type"],
        )
    assert "comparação completa" in result["reply"].lower()
    assert result["reply"] != GENERIC_REPLY_FALLBACK


def test_text_chat_without_document_unchanged(monkeypatch):
    class _Resp:
        text = "resposta textual"

    monkeypatch.setattr(julia_chat, "cleiton_governed_generate_content", lambda *_a, **_k: _Resp())
    monkeypatch.setattr(julia_chat, "_get_client", lambda: object())

    result = chat_julia_reply("Como reduzir frete?", [])
    assert result["reply"] == "resposta textual"


def test_non_deadline_documental_error_uses_supported_fallback(monkeypatch):
    attempts = []

    class _Resp:
        text = "ok fallback"

    def _fake(_client, model, contents, agent, flow_type, api_key_label):
        attempts.append(model)
        if len(attempts) == 1:
            raise RuntimeError("503 UNAVAILABLE")
        return _Resp()

    monkeypatch.setenv("GEMINI_MODEL_TEXT", "gemini-2.5-flash")
    monkeypatch.setenv("JULIA_CHAT_MODEL_FALLBACK", "gemini-2.5-flash-lite")
    monkeypatch.setattr(julia_chat, "cleiton_governed_generate_content", _fake)
    monkeypatch.setattr(julia_chat, "_get_client", lambda: object())
    part = SimpleNamespace(file_data=SimpleNamespace(file_uri="https://x", mime_type="application/pdf"))

    result = chat_julia_reply(
        "Resuma",
        [],
        document_context_block="ctx",
        document_file_parts=[part],
        flow_type=FLOW_TYPE_JULIA_CHAT_DOCUMENTAL,
    )
    assert result["reply"] == "ok fallback"
    assert attempts == ["gemini-2.5-flash", "gemini-2.5-flash-lite"]
    assert "gemini-1.5-flash" not in attempts


def test_documental_deadline_persists_ia_consumo_failure(app, monkeypatch):
    from app.consumo_identidade import identidade_http_anonimo

    monkeypatch.setenv("GEMINI_MODEL_TEXT", "gemini-2.5-flash")
    client = MagicMock()
    client.models.generate_content.side_effect = _deadline_exc()
    monkeypatch.setattr(julia_chat, "_get_client", lambda: client)
    monkeypatch.setattr(
        "app.run_cleiton_gemini_governance.resolve_identidade_para_persistencia",
        identidade_http_anonimo,
    )
    monkeypatch.setattr(
        "app.services.cleiton_franquia_operacional_service.aplicar_motor_apos_ia_consumo_evento",
        lambda _event_id: None,
    )
    part = SimpleNamespace(file_data=SimpleNamespace(file_uri="https://x", mime_type="application/pdf"))

    with app.app_context():
        from app.extensions import db

        db.session.query(IaConsumoEvento).delete()
        db.session.commit()
        result = chat_julia_reply(
            "Compare",
            [],
            document_context_block="ctx",
            document_file_parts=[part],
            flow_type=FLOW_TYPE_JULIA_CHAT_DOCUMENTAL,
        )
        assert result["reply"] == DOCUMENTAL_DEADLINE_REPLY
        event = IaConsumoEvento.query.filter_by(flow_type=FLOW_TYPE_JULIA_CHAT_DOCUMENTAL).one()
        assert event.status == "failure"
        assert "504" in (event.error_summary or "")
        assert event.agent == "julia"
