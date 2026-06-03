"""Testes de integração documental do chat da Júlia (Fase 4)."""
from __future__ import annotations

import importlib
import inspect
import os
from types import SimpleNamespace

import pytest

import app.run_julia_chat as julia_chat
from app.cleiton_doc_contracts import (
    FIELD_PREPARED_CONTEXT,
    FLOW_TYPE_JULIA_CHAT,
    FLOW_TYPE_JULIA_CHAT_DOCUMENTAL,
)
from app.julia_doc_context import build_julia_document_context_for_chat
from app.run_julia_chat import chat_julia_reply
from tests.cleiton_doc_fixtures import make_txt, patch_cleiton_doc_cfg, patch_cleiton_doc_store


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


@pytest.fixture
def session_app(app, tmp_path, monkeypatch, ctx):
    patch_cleiton_doc_store(tmp_path, monkeypatch)
    patch_cleiton_doc_cfg(monkeypatch)
    app.config["SECRET_KEY"] = "test-secret"
    return app


def _fake_gemini_response(monkeypatch, capture: dict):
    class _Resp:
        text = "resposta simulada"

    def _fake(client, model, contents, agent, flow_type, api_key_label):
        capture["contents"] = contents
        capture["flow_type"] = flow_type
        capture["agent"] = agent
        return _Resp()

    monkeypatch.setattr(julia_chat, "cleiton_governed_generate_content", _fake)
    monkeypatch.setattr(julia_chat, "_get_client", lambda: object())


def test_chat_without_document_keeps_julia_chat_flow(monkeypatch):
    capture = {}
    _fake_gemini_response(monkeypatch, capture)
    result = chat_julia_reply("Como reduzir custo de frete?", [])
    assert result["reply"]
    assert capture["flow_type"] == FLOW_TYPE_JULIA_CHAT
    assert "Contexto documental temporário" not in capture["contents"]


def test_chat_with_document_context_uses_documental_flow(session_app, monkeypatch):
    import app.cleiton_doc_service as svc

    capture = {}
    _fake_gemini_response(monkeypatch, capture)
    with session_app.test_request_context("/"):
        svc.prepare_and_register_document(
            display_name="relatorio.txt",
            file_bytes=make_txt("evidencia de frete SP-RJ"),
            mime_type="text/plain",
        )
        doc_ctx = build_julia_document_context_for_chat()
        chat_julia_reply(
            "O que diz o documento?",
            [],
            document_context_block=doc_ctx["context_block"],
            flow_type=doc_ctx["flow_type"],
        )
    assert capture["flow_type"] == FLOW_TYPE_JULIA_CHAT_DOCUMENTAL
    assert "evidencia de frete SP-RJ" in capture["contents"]
    assert "Contexto documental temporário autorizado por Cleiton" in capture["contents"]


def test_prompt_respects_max_chars(session_app, monkeypatch):
    import app.cleiton_doc_service as svc

    patch_cleiton_doc_cfg(monkeypatch, prompt_context_max_chars=120)
    with session_app.test_request_context("/"):
        svc.prepare_and_register_document(
            display_name="longo.txt",
            file_bytes=make_txt("A" * 500),
            mime_type="text/plain",
        )
        doc_ctx = build_julia_document_context_for_chat()
    assert doc_ctx["meta"]["context_truncated"] is True
    assert len(doc_ctx["context_block"]) <= 120


def test_prompt_respects_max_files_considered(session_app, monkeypatch):
    import app.cleiton_doc_service as svc

    patch_cleiton_doc_cfg(monkeypatch, prompt_max_files_considered=1, max_files_per_session=5)
    with session_app.test_request_context("/"):
        svc.prepare_and_register_document(
            display_name="primeiro.txt",
            file_bytes=make_txt("DOC-UM"),
            mime_type="text/plain",
        )
        svc.prepare_and_register_document(
            display_name="segundo.txt",
            file_bytes=make_txt("DOC-DOIS"),
            mime_type="text/plain",
        )
        doc_ctx = build_julia_document_context_for_chat()
    assert doc_ctx["meta"]["files_considered"] == 1
    assert "DOC-UM" not in doc_ctx["context_block"]
    assert "DOC-DOIS" in doc_ctx["context_block"]


def test_truncated_document_signaled(session_app, monkeypatch):
    import app.cleiton_doc_service as svc

    with session_app.test_request_context("/"):
        doc = svc.prepare_and_register_document(
            display_name="t.txt",
            file_bytes=make_txt("x"),
            mime_type="text/plain",
        )
        from app.cleiton_doc_store import load_document_record

        record = load_document_record(doc["doc_id"], ttl_hours=24)
        record["truncated"] = True
        from app.cleiton_doc_store import save_document_record

        save_document_record(record)
        doc_ctx = build_julia_document_context_for_chat()
    assert "truncado" in doc_ctx["context_block"]


def test_governed_generate_content_still_used(session_app, monkeypatch):
    import app.cleiton_doc_service as svc

    calls = {"count": 0}

    class _Resp:
        text = "ok"

    def _fake(*args, **kwargs):
        calls["count"] += 1
        return _Resp()

    monkeypatch.setattr(julia_chat, "cleiton_governed_generate_content", _fake)
    monkeypatch.setattr(julia_chat, "_get_client", lambda: object())
    with session_app.test_request_context("/"):
        svc.prepare_and_register_document(
            display_name="g.txt",
            file_bytes=make_txt("ctx"),
            mime_type="text/plain",
        )
        doc_ctx = build_julia_document_context_for_chat()
        chat_julia_reply("oi", [], document_context_block=doc_ctx["context_block"])
    assert calls["count"] >= 1


def test_run_julia_chat_does_not_process_documents():
    source = inspect.getsource(julia_chat)
    assert "prepare_and_register_document" not in source
    assert "prepare_document" not in source
    assert "convert_document" not in source
    assert "load_document_record" not in source


def test_no_semantic_regex_in_document_modules():
    from app import julia_doc_context as mod

    source = inspect.getsource(mod)
    assert "re.compile" not in source
    chat_source = inspect.getsource(julia_chat.chat_julia_reply)
    assert "re.search" not in chat_source
    assert "re.match" not in chat_source


def test_no_question_parser_in_julia_doc_context():
    from app import julia_doc_context as mod

    source = inspect.getsource(mod)
    forbidden = (
        "if pergunta",
        "if intent",
        "classificar",
        "parser",
        "regex",
        "se contém",
    )
    lowered = source.lower()
    for token in forbidden:
        assert token not in lowered


def test_open_questions_remain_possible(monkeypatch):
    capture = {}
    _fake_gemini_response(monkeypatch, capture)
    chat_julia_reply("Me conte o que achar relevante sobre logística.", [])
    assert capture["flow_type"] == FLOW_TYPE_JULIA_CHAT


def test_api_chat_julia_without_documents_unchanged(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        patch_cleiton_doc_store(tmp_path, monkeypatch)
        patch_cleiton_doc_cfg(monkeypatch)
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=True))
    monkeypatch.setattr(
        web,
        "avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True},
    )

    captured = {}

    def _fake_chat(message, history, max_history, **kwargs):
        captured.update(kwargs)
        return {"reply": "ok-julia", "suggestions": []}

    monkeypatch.setattr("app.run_julia_chat.chat_julia_reply", _fake_chat)

    client = web.app.test_client()
    resp = client.post("/api/chat_julia", json={"message": "oi", "history": []})
    assert resp.status_code == 200
    assert resp.get_json()["reply"] == "ok-julia"
    assert not (captured.get("document_context_block") or "").strip()
    assert captured.get("flow_type") == FLOW_TYPE_JULIA_CHAT


def test_api_chat_julia_with_session_documents(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        patch_cleiton_doc_store(tmp_path, monkeypatch)
        patch_cleiton_doc_cfg(monkeypatch)
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=True))
    monkeypatch.setattr(
        web,
        "avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True},
    )
    monkeypatch.setattr("app.julia_documents_routes.current_user", SimpleNamespace(is_authenticated=True))
    monkeypatch.setattr(
        "app.julia_documents_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True},
    )

    client = web.app.test_client()
    import io

    up = client.post(
        "/api/julia/documents/upload",
        data={"file": (io.BytesIO(make_txt("evidencia")), "e.txt")},
        content_type="multipart/form-data",
    )
    assert up.status_code == 200

    captured = {}

    def _fake_chat(message, history, max_history, **kwargs):
        captured.update(kwargs)
        return {"reply": "ok-doc", "suggestions": []}

    monkeypatch.setattr("app.run_julia_chat.chat_julia_reply", _fake_chat)
    resp = client.post("/api/chat_julia", json={"message": "analise", "history": []})
    assert resp.status_code == 200
    assert "evidencia" in (captured.get("document_context_block") or "")
    assert captured.get("flow_type") == FLOW_TYPE_JULIA_CHAT_DOCUMENTAL


def test_listing_never_exposes_prepared_context(session_app):
    import app.cleiton_doc_service as svc

    with session_app.test_request_context("/"):
        public = svc.prepare_and_register_document(
            display_name="p.txt",
            file_bytes=make_txt("segredo"),
            mime_type="text/plain",
        )
    assert FIELD_PREPARED_CONTEXT not in public


def test_pdf_placeholder_does_not_pretend_content(session_app, monkeypatch):
    import app.cleiton_doc_gemini_files as gemini_files
    import app.cleiton_doc_service as svc
    from tests.cleiton_doc_fixtures import make_minimal_pdf, patch_gemini_pdf_upload

    patch_gemini_pdf_upload(monkeypatch)
    monkeypatch.setattr(
        gemini_files,
        "build_gemini_file_part_for_generate",
        lambda _r, **k: SimpleNamespace(uri="https://x", mime_type="application/pdf"),
    )

    with session_app.test_request_context("/"):
        svc.prepare_and_register_document(
            display_name="doc.pdf",
            file_bytes=make_minimal_pdf(pages=1),
            mime_type="application/pdf",
        )
        doc_ctx = build_julia_document_context_for_chat()
    assert "File API pendente" not in doc_ctx["context_block"]
    assert "indisponível nesta fase" not in doc_ctx["context_block"]
    assert "multimodal" in doc_ctx["context_block"] or "Gemini File API" in doc_ctx["context_block"]


def test_api_chat_julia_degrades_when_document_context_fails(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        patch_cleiton_doc_store(tmp_path, monkeypatch)
        patch_cleiton_doc_cfg(monkeypatch)
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=True))
    monkeypatch.setattr(
        web,
        "avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True},
    )

    def _boom():
        raise RuntimeError("falha simulada no contexto documental")

    monkeypatch.setattr("app.julia_doc_context.build_julia_document_context_for_chat", _boom)

    captured = {}

    def _fake_chat(message, history, max_history, **kwargs):
        captured.update(kwargs)
        return {"reply": "ok-degraded", "suggestions": []}

    monkeypatch.setattr("app.run_julia_chat.chat_julia_reply", _fake_chat)

    client = web.app.test_client()
    resp = client.post("/api/chat_julia", json={"message": "oi", "history": []})
    assert resp.status_code == 200
    assert resp.get_json()["reply"] == "ok-degraded"
    assert captured.get("flow_type") == FLOW_TYPE_JULIA_CHAT
    assert not (captured.get("document_context_block") or "").strip()


def test_api_chat_julia_documental_flow_type_via_governed_generate(app, ctx, monkeypatch, tmp_path):
    """Teste mais forte: /api/chat_julia com documento ativo passa flow_type documental ao Cleiton."""
    import io

    import app.run_julia_chat as julia_chat

    with app.app_context():
        patch_cleiton_doc_store(tmp_path, monkeypatch)
        patch_cleiton_doc_cfg(monkeypatch)
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=True))
    monkeypatch.setattr("app.julia_documents_routes.current_user", SimpleNamespace(is_authenticated=True))
    monkeypatch.setattr(
        web,
        "avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True},
    )
    monkeypatch.setattr(
        "app.julia_documents_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True},
    )

    client = web.app.test_client()
    up = client.post(
        "/api/julia/documents/upload",
        data={"file": (io.BytesIO(make_txt("evidencia forte")), "e.txt")},
        content_type="multipart/form-data",
    )
    assert up.status_code == 200

    capture = {}

    class _Resp:
        text = "resposta documental"

    def _fake_governed(client_obj, model, contents, agent, flow_type, api_key_label):
        capture["flow_type"] = flow_type
        capture["contents"] = contents
        return _Resp()

    monkeypatch.setattr(julia_chat, "cleiton_governed_generate_content", _fake_governed)
    monkeypatch.setattr(julia_chat, "_get_client", lambda: object())

    resp = client.post("/api/chat_julia", json={"message": "analise o documento", "history": []})
    assert resp.status_code == 200
    assert capture.get("flow_type") == FLOW_TYPE_JULIA_CHAT_DOCUMENTAL
    assert "evidencia forte" in (capture.get("contents") or "")
