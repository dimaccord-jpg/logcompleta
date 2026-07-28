"""Testes de API documental da Júlia (Fase 4)."""
from __future__ import annotations

import io
import json
import importlib
import os
from datetime import timedelta
from types import SimpleNamespace

import pytest

import app.cleiton_doc_service as svc
import app.cleiton_doc_store as store
from app.cleiton_doc_contracts import (
    ERROR_DISABLED_TYPE,
    ERROR_FILE_TOO_LARGE,
    ERROR_INVALID_EXTENSION,
    ERROR_INVALID_SIZE,
    ERROR_MAX_FILES,
    ERROR_MISSING_FILE,
    ERROR_SESSION_BYTES,
    ERROR_UPLOAD_DISABLED,
    FIELD_DOC_ID,
    FIELD_EXPIRES_AT,
    FIELD_PREPARED_CONTEXT,
    SESSION_KEY_CLEITON_DOC_IDS,
)
from app.cleide_audit_doc_service import CLEIDE_AUDIT_DOC_IDS_SESSION_KEY
from app.agente_compara_doc_service import AGENTE_COMPARA_DOC_IDS_SESSION_KEY
from app.services.agente_compara_config_service import (
    AgenteComparaConfig,
    DEFAULT_FALLBACK_MESSAGE as AC_DEFAULT_FALLBACK,
)
from app.services.cleide_audit_config_service import (
    CleideAuditConfig,
    DEFAULT_FALLBACK_MESSAGE as CLEIDE_DEFAULT_FALLBACK,
)
from tests.cleiton_doc_fixtures import (
    make_csv,
    make_txt,
    patch_cleiton_doc_cfg,
    patch_cleiton_doc_store,
)


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


def _authorized(monkeypatch, web):
    fake_user = SimpleNamespace(is_authenticated=True, conta_id=1, franquia_id=1, id=1)
    monkeypatch.setattr(web, "current_user", fake_user)
    monkeypatch.setattr("app.julia_documents_routes.current_user", fake_user)
    monkeypatch.setattr("app.cleide_audit_routes.current_user", fake_user)
    monkeypatch.setattr("app.agente_compara_api_routes.current_user", fake_user)
    authz = {"permitido": True, "modo_operacao": "normal"}
    monkeypatch.setattr(
        web,
        "avaliar_autorizacao_operacao_por_franquia",
        lambda _u: authz,
    )
    monkeypatch.setattr(
        "app.julia_documents_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _u: authz,
    )
    monkeypatch.setattr(
        "app.cleide_audit_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _u: authz,
    )
    monkeypatch.setattr(
        "app.agente_compara_api_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _u: authz,
    )


def _setup_doc_env(monkeypatch, tmp_path, **cfg_overrides):
    patch_cleiton_doc_store(tmp_path, monkeypatch)
    cfg = patch_cleiton_doc_cfg(monkeypatch, **cfg_overrides)
    monkeypatch.setattr("app.cleide_audit_doc_service.get_cleiton_doc_config", lambda: cfg)
    monkeypatch.setattr("app.cleide_audit_routes.get_cleiton_doc_config", lambda: cfg)
    monkeypatch.setattr("app.agente_compara_doc_service.get_cleiton_doc_config", lambda: cfg)
    monkeypatch.setattr("app.agente_compara_api_routes.get_cleiton_doc_config", lambda: cfg)
    ac_cfg = AgenteComparaConfig(
        chat_enabled=True,
        upload_enabled=True,
        chat_max_history=10,
        document_context_max_chars=24000,
        max_documents_considered=3,
        question_max_chars=4000,
        fallback_message=AC_DEFAULT_FALLBACK,
        no_documents_behavior="allow_guided",
        show_documents_used=True,
        no_hallucination_instruction_enabled=True,
        audited_file_max_bytes=None,
        audited_file_max_rows=2000,
    )
    cleide_cfg = CleideAuditConfig(
        chat_enabled=True,
        upload_enabled=True,
        chat_max_history=10,
        document_context_max_chars=24000,
        max_documents_considered=3,
        question_max_chars=4000,
        fallback_message=CLEIDE_DEFAULT_FALLBACK,
        no_documents_behavior="allow_guided",
        show_documents_used=True,
        no_hallucination_instruction_enabled=True,
        audited_file_max_bytes=None,
        audited_file_max_rows=2000,
    )
    for target, cfg_obj in (
        ("app.agente_compara_api_routes.get_agente_compara_config", ac_cfg),
        ("app.agente_compara_doc_service.get_agente_compara_config", ac_cfg),
        ("app.cleide_audit_routes.get_cleide_audit_config", cleide_cfg),
        ("app.cleide_audit_doc_service.get_cleide_audit_config", cleide_cfg),
    ):
        monkeypatch.setattr(target, lambda _cfg=cfg_obj: _cfg)
    return cfg


def _upload_cleide(client, filename: str, content: bytes, mime: str = "text/plain"):
    return client.post(
        "/api/cleide-auditoria/documents/upload",
        data={"file": (io.BytesIO(content), filename, mime)},
        content_type="multipart/form-data",
    )


def _upload_ac(client, filename: str, content: bytes, mime: str = "text/csv", *, carrier_name: str = "Transportadora Teste"):
    return client.post(
        "/api/agente-compara/documents/upload",
        data={
            "file": (io.BytesIO(content), filename, mime),
            "carrier_name": carrier_name,
        },
        content_type="multipart/form-data",
    )


def _upload(client, filename: str, content: bytes, mime: str = "text/plain"):
    return client.post(
        "/api/julia/documents/upload",
        data={"file": (io.BytesIO(content), filename)},
        content_type="multipart/form-data",
    )


@pytest.fixture
def session_app(app, tmp_path, monkeypatch, ctx):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    app.config["SECRET_KEY"] = "test-secret-julia-legacy"
    return app


@pytest.fixture
def web_client(app, tmp_path, monkeypatch, ctx):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    _authorized(monkeypatch, web)
    monkeypatch.setattr(
        "app.run_cleide_audit_temp_table.trigger_temp_table_extraction_for_session",
        lambda **_k: None,
    )
    monkeypatch.setattr(
        "app.run_agente_compara_temp_table.trigger_temp_table_extraction_for_session",
        lambda **_k: None,
    )
    web.app.config["TESTING"] = True
    return web.app.test_client()


def test_upload_valid_txt(web_client):
    resp = _upload(web_client, "nota.txt", make_txt("conteudo seguro"))
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["document"]["doc_id"]
    assert body["document"]["display_name"]
    assert body["session"]["count"] == 1
    assert FIELD_PREPARED_CONTEXT not in body["document"]


def test_upload_valid_csv(web_client):
    content = make_csv([["col_a", "col_b"], ["1", "2"]])
    resp = web_client.post(
        "/api/julia/documents/upload",
        data={"file": (io.BytesIO(content), "dados.csv", "text/csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    assert resp.get_json()["document"]["doc_type"] == "csv"


def test_upload_invalid_extension(web_client):
    resp = _upload(web_client, "malware.exe", b"payload")
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["ok"] is False
    assert body["error_code"] == ERROR_INVALID_EXTENSION
    assert "payload" not in body.get("message", "")


def test_upload_invalid_size(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path, txt_max_bytes=10)
    web = _load_web_module()
    _authorized(monkeypatch, web)
    client = web.app.test_client()
    resp = _upload(client, "grande.txt", make_txt("x" * 100))
    assert resp.status_code == 413
    assert resp.get_json()["error_code"] == ERROR_FILE_TOO_LARGE


def test_upload_disabled_type(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path, csv_enabled=False)
    web = _load_web_module()
    _authorized(monkeypatch, web)
    client = web.app.test_client()
    resp = _upload(client, "d.csv", make_csv([["a"], ["b"]]))
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == ERROR_DISABLED_TYPE


def test_upload_disabled_globally(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path, upload_enabled=False)
    web = _load_web_module()
    _authorized(monkeypatch, web)
    client = web.app.test_client()
    resp = _upload(client, "a.txt", make_txt("x"))
    assert resp.status_code == 403
    assert resp.get_json()["error_code"] == ERROR_UPLOAD_DISABLED


def test_max_five_files(web_client):
    for idx in range(5):
        resp = _upload(web_client, f"f{idx}.txt", make_txt("a"))
        assert resp.status_code == 200
    resp = _upload(web_client, "sexto.txt", make_txt("b"))
    assert resp.status_code == 409
    assert resp.get_json()["error_code"] == ERROR_MAX_FILES


def test_session_bytes_limit(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path, session_max_bytes=20, max_files_per_session=10, txt_max_bytes=100)
    web = _load_web_module()
    _authorized(monkeypatch, web)
    client = web.app.test_client()
    resp = _upload(client, "a.txt", make_txt("12345678901234567890123456789012345"))
    assert resp.status_code == 413
    assert resp.get_json()["error_code"] in {ERROR_SESSION_BYTES, ERROR_INVALID_SIZE, ERROR_FILE_TOO_LARGE}


def test_list_does_not_return_prepared_context(web_client):
    _upload(web_client, "x.txt", make_txt("segredo interno"))
    resp = web_client.get("/api/julia/documents")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert len(body["documents"]) == 1
    assert FIELD_PREPARED_CONTEXT not in body["documents"][0]
    raw = json.dumps(body)
    assert "segredo interno" not in raw


def test_delete_single_document(web_client):
    up = _upload(web_client, "del.txt", make_txt("x")).get_json()
    doc_id = up["document"]["doc_id"]
    resp = web_client.delete(f"/api/julia/documents/{doc_id}")
    assert resp.status_code == 200
    assert resp.get_json()["session"]["count"] == 0


def test_delete_tolerates_missing_document(web_client):
    resp = web_client.delete("/api/julia/documents/inexistente")
    assert resp.status_code == 200
    assert resp.get_json()["session"]["count"] == 0


def test_clear_all_documents(web_client, tmp_path):
    up = _upload(web_client, "c.txt", make_txt("temp")).get_json()
    doc_id = up["document"]["doc_id"]
    resp = web_client.post("/api/julia/documents/clear")
    assert resp.status_code == 200
    assert resp.get_json()["session"]["count"] == 0
    assert not (tmp_path / f"{doc_id}.json").exists()


def test_expired_document_not_listed(web_client, tmp_path):
    up = _upload(web_client, "exp.txt", make_txt("x")).get_json()
    doc_id = up["document"]["doc_id"]
    path = tmp_path / f"{doc_id}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record[FIELD_EXPIRES_AT] = (svc._utcnow() - timedelta(hours=1)).isoformat()
    path.write_text(json.dumps(record), encoding="utf-8")
    resp = web_client.get("/api/julia/documents")
    assert resp.get_json()["session"]["count"] == 0


def test_error_does_not_leak_file_content(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path, txt_max_bytes=5)
    web = _load_web_module()
    _authorized(monkeypatch, web)
    client = web.app.test_client()
    secret = "conteudo-super-secreto-12345"
    resp = _upload(client, "s.txt", make_txt(secret))
    body = resp.get_json()
    assert body["ok"] is False
    assert secret not in json.dumps(body)


def test_upload_missing_file_uses_contract_code(web_client):
    resp = web_client.post(
        "/api/julia/documents/upload",
        data={},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == ERROR_MISSING_FILE


def test_list_requires_login(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=False))
    monkeypatch.setattr("app.julia_documents_routes.current_user", SimpleNamespace(is_authenticated=False))
    client = web.app.test_client()
    resp = client.get("/api/julia/documents")
    assert resp.status_code == 401
    assert resp.get_json()["error_code"] == "auth_required"


def test_delete_requires_login(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=False))
    monkeypatch.setattr("app.julia_documents_routes.current_user", SimpleNamespace(is_authenticated=False))
    client = web.app.test_client()
    resp = client.delete("/api/julia/documents/qualquer-id")
    assert resp.status_code == 401
    assert resp.get_json()["error_code"] == "auth_required"


def test_clear_requires_login(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=False))
    monkeypatch.setattr("app.julia_documents_routes.current_user", SimpleNamespace(is_authenticated=False))
    client = web.app.test_client()
    resp = client.post("/api/julia/documents/clear")
    assert resp.status_code == 401
    assert resp.get_json()["error_code"] == "auth_required"


def test_list_blocked_by_franquia(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=True))
    monkeypatch.setattr("app.julia_documents_routes.current_user", SimpleNamespace(is_authenticated=True))
    monkeypatch.setattr(
        "app.julia_documents_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": False, "mensagem_usuario": "Bloqueado."},
    )
    client = web.app.test_client()
    resp = client.get("/api/julia/documents")
    assert resp.status_code == 403
    assert resp.get_json()["error_code"] == "franquia_blocked"


def test_delete_blocked_by_franquia(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=True))
    monkeypatch.setattr("app.julia_documents_routes.current_user", SimpleNamespace(is_authenticated=True))
    monkeypatch.setattr(
        "app.julia_documents_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": False, "mensagem_usuario": "Bloqueado."},
    )
    client = web.app.test_client()
    resp = client.delete("/api/julia/documents/doc-id")
    assert resp.status_code == 403
    assert resp.get_json()["error_code"] == "franquia_blocked"


def test_clear_blocked_by_franquia(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=True))
    monkeypatch.setattr("app.julia_documents_routes.current_user", SimpleNamespace(is_authenticated=True))
    monkeypatch.setattr(
        "app.julia_documents_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": False, "mensagem_usuario": "Bloqueado."},
    )
    client = web.app.test_client()
    resp = client.post("/api/julia/documents/clear")
    assert resp.status_code == 403
    assert resp.get_json()["error_code"] == "franquia_blocked"


def test_upload_requires_login(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=False))
    monkeypatch.setattr("app.julia_documents_routes.current_user", SimpleNamespace(is_authenticated=False))
    client = web.app.test_client()
    resp = _upload(client, "a.txt", make_txt("x"))
    assert resp.status_code == 401


def test_upload_blocked_by_franquia(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=True))
    monkeypatch.setattr("app.julia_documents_routes.current_user", SimpleNamespace(is_authenticated=True))
    monkeypatch.setattr(
        "app.julia_documents_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": False, "mensagem_usuario": "Bloqueado."},
    )
    client = web.app.test_client()
    resp = _upload(client, "a.txt", make_txt("x"))
    assert resp.status_code == 403


def test_content_not_in_database(web_client, app, ctx):
    from app.extensions import db
    from sqlalchemy import inspect, text

    secret = "nao-vai-pro-sql-xyz"
    _upload(web_client, "db.txt", make_txt(secret))
    with app.app_context():
        inspector = inspect(db.engine)
        for table in inspector.get_table_names():
            result = db.session.execute(text(f'SELECT * FROM "{table}" LIMIT 50'))
            for row in result.fetchall():
                assert secret not in str(row)


def test_session_key_present_after_upload(web_client):
    _upload(web_client, "s.txt", make_txt("a"))
    with web_client.session_transaction() as sess:
        ids = sess.get(SESSION_KEY_CLEITON_DOC_IDS) or []
        assert len(ids) == 1


def test_julia_cannot_delete_cleide_document(web_client, tmp_path):
    cleide_up = _upload_cleide(web_client, "cleide.txt", make_txt("cleide")).get_json()
    cleide_doc_id = cleide_up["document"]["doc_id"]
    assert (tmp_path / f"{cleide_doc_id}.json").is_file()

    resp = web_client.delete(f"/api/julia/documents/{cleide_doc_id}")
    assert resp.status_code == 200
    assert (tmp_path / f"{cleide_doc_id}.json").is_file()

    with web_client.session_transaction() as sess:
        assert cleide_doc_id in (sess.get(CLEIDE_AUDIT_DOC_IDS_SESSION_KEY) or [])
        assert cleide_doc_id not in (sess.get(SESSION_KEY_CLEITON_DOC_IDS) or [])


def test_julia_cannot_delete_agente_compara_document(web_client, tmp_path):
    ac_up = _upload_ac(web_client, "ac.csv", make_csv([["a"], ["1"]]), "text/csv").get_json()
    ac_doc_id = ac_up["document"]["doc_id"]
    assert (tmp_path / f"{ac_doc_id}.json").is_file()

    resp = web_client.delete(f"/api/julia/documents/{ac_doc_id}")
    assert resp.status_code == 200
    assert (tmp_path / f"{ac_doc_id}.json").is_file()

    with web_client.session_transaction() as sess:
        assert ac_doc_id in (sess.get(AGENTE_COMPARA_DOC_IDS_SESSION_KEY) or [])
        assert ac_doc_id not in (sess.get(SESSION_KEY_CLEITON_DOC_IDS) or [])


def test_julia_cannot_delete_document_from_other_session(web_client, tmp_path):
    first = _upload(web_client, "a.txt", make_txt("a")).get_json()
    foreign_doc_id = first["document"]["doc_id"]

    other_client = web_client.application.test_client()
    _upload(other_client, "b.txt", make_txt("b"))
    resp = other_client.delete(f"/api/julia/documents/{foreign_doc_id}")
    assert resp.status_code == 200
    assert (tmp_path / f"{foreign_doc_id}.json").is_file()


def test_julia_legitimate_delete_removes_store_and_session(web_client, tmp_path):
    up = _upload(web_client, "ok.txt", make_txt("ok")).get_json()
    doc_id = up["document"]["doc_id"]
    resp = web_client.delete(f"/api/julia/documents/{doc_id}")
    assert resp.status_code == 200
    assert resp.get_json()["session"]["count"] == 0
    assert not (tmp_path / f"{doc_id}.json").exists()


def test_julia_delete_nonexistent_is_safe(web_client, tmp_path):
    resp = web_client.delete("/api/julia/documents/00000000000000000000000000000000")
    assert resp.status_code == 200
    assert resp.get_json()["session"]["count"] == 0


def test_julia_clear_preserves_cleide_document(web_client, tmp_path):
    julia_up = _upload(web_client, "j.txt", make_txt("julia")).get_json()
    julia_doc_id = julia_up["document"]["doc_id"]
    cleide_up = _upload_cleide(web_client, "cleide.txt", make_txt("cleide")).get_json()
    cleide_doc_id = cleide_up["document"]["doc_id"]

    with web_client.session_transaction() as sess:
        ids = list(sess.get(SESSION_KEY_CLEITON_DOC_IDS) or [])
        ids.append(cleide_doc_id)
        sess[SESSION_KEY_CLEITON_DOC_IDS] = ids

    resp = web_client.post("/api/julia/documents/clear")
    assert resp.status_code == 200
    assert not (tmp_path / f"{julia_doc_id}.json").exists()
    assert (tmp_path / f"{cleide_doc_id}.json").is_file()

    with web_client.session_transaction() as sess:
        assert cleide_doc_id in (sess.get(CLEIDE_AUDIT_DOC_IDS_SESSION_KEY) or [])
        assert cleide_doc_id not in (sess.get(SESSION_KEY_CLEITON_DOC_IDS) or [])


def test_julia_clear_preserves_agente_compara_document(web_client, tmp_path):
    julia_up = _upload(web_client, "j.txt", make_txt("julia")).get_json()
    julia_doc_id = julia_up["document"]["doc_id"]
    ac_up = _upload_ac(web_client, "ac.csv", make_csv([["a"], ["1"]]), "text/csv").get_json()
    ac_doc_id = ac_up["document"]["doc_id"]

    with web_client.session_transaction() as sess:
        ids = list(sess.get(SESSION_KEY_CLEITON_DOC_IDS) or [])
        ids.append(ac_doc_id)
        sess[SESSION_KEY_CLEITON_DOC_IDS] = ids

    resp = web_client.post("/api/julia/documents/clear")
    assert resp.status_code == 200
    assert not (tmp_path / f"{julia_doc_id}.json").exists()
    assert (tmp_path / f"{ac_doc_id}.json").is_file()

    with web_client.session_transaction() as sess:
        assert ac_doc_id in (sess.get(AGENTE_COMPARA_DOC_IDS_SESSION_KEY) or [])
        assert ac_doc_id not in (sess.get(SESSION_KEY_CLEITON_DOC_IDS) or [])


def test_julia_legacy_document_with_session_key_none_deletes_safely(session_app, tmp_path):
    from app.cleiton_doc_contracts import FIELD_SESSION_KEY, FIELD_SOURCE_AGENT, SOURCE_AGENT_CLEITON

    with session_app.test_request_context("/"):
        doc = svc.register_document_placeholder(
            display_name="legacy.txt",
            extension=".txt",
            mime_type="application/pdf",
            size_bytes=128,
        )
        path = tmp_path / f"{doc[FIELD_DOC_ID]}.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record.get(FIELD_SOURCE_AGENT) == SOURCE_AGENT_CLEITON
        assert record.get(FIELD_SESSION_KEY) is None

        result = svc.remove_document_from_session(doc[FIELD_DOC_ID])
        assert result["removed_from_session"] is True
        assert result["removed_from_store"] is True
        assert not path.exists()


def test_julia_legacy_document_not_removable_from_other_session(session_app, tmp_path):
    with session_app.test_request_context("/"):
        doc = svc.register_document_placeholder(
            display_name="legacy.txt",
            extension=".txt",
            mime_type="text/plain",
            size_bytes=128,
        )
        foreign_id = doc[FIELD_DOC_ID]

    with session_app.test_request_context("/"):
        result = svc.remove_document_from_session(foreign_id)
        assert result["removed_from_session"] is False
        assert result["removed_from_store"] is False
        assert (tmp_path / f"{foreign_id}.json").is_file()
