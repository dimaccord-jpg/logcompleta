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
    fake_user = SimpleNamespace(is_authenticated=True, conta_id=1, franquia_id=1)
    monkeypatch.setattr(web, "current_user", fake_user)
    monkeypatch.setattr("app.julia_documents_routes.current_user", fake_user)
    monkeypatch.setattr(
        web,
        "avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True, "modo_operacao": "normal"},
    )
    monkeypatch.setattr(
        "app.julia_documents_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True, "modo_operacao": "normal"},
    )


def _setup_doc_env(monkeypatch, tmp_path, **cfg_overrides):
    patch_cleiton_doc_store(tmp_path, monkeypatch)
    return patch_cleiton_doc_cfg(monkeypatch, **cfg_overrides)


def _upload(client, filename: str, content: bytes, mime: str = "text/plain"):
    return client.post(
        "/api/julia/documents/upload",
        data={"file": (io.BytesIO(content), filename)},
        content_type="multipart/form-data",
    )


@pytest.fixture
def web_client(app, tmp_path, monkeypatch, ctx):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    _authorized(monkeypatch, web)
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
