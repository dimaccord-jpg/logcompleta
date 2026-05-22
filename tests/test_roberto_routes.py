import importlib
import os
from types import SimpleNamespace
from flask_login import UserMixin


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


class _AuthUser(UserMixin):
    def __init__(self, user_id: str = "123", is_admin: bool = False):
        self.id = user_id
        self.is_admin = is_admin
        self.conta_id = 1
        self.franquia_id = 1
        self.email = "tester@example.com"
        self.full_name = "Tester User"


def _force_login(client, web, *, is_admin=False):
    user = _AuthUser(is_admin=is_admin)
    monkey_get = lambda _user_id: user
    setattr(web, "get_user_by_id", monkey_get)
    with client.session_transaction() as sess:
        sess["_user_id"] = user.get_id()
        sess["_fresh"] = True
    return user


def test_fretes_publica_quando_nao_autenticado(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=False))
    monkeypatch.setattr(web, "_load_indices_payload", lambda: {"origens": [], "destinos": []})

    calls = {"authz": 0}

    def _authz(_u):
        calls["authz"] += 1
        return {"permitido": True, "modo_operacao": "normal"}

    monkeypatch.setattr(web, "avaliar_autorizacao_operacao_por_franquia", _authz)

    client = web.app.test_client()
    resp = client.get("/fretes")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Análise de Fretes com Inteligência Artificial" in html
    assert "Faca login para enviar planilhas" in html
    assert "window.ROBERTO_BI_AUTHENTICATED = false;" in html
    assert "const redirectUnauthenticatedPrivateAction = true;" in html
    assert "window.location.href = loginUrl;" in html
    assert calls["authz"] == 0


def test_fretes_retorna_200_quando_autenticado(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(web, "_load_indices_payload", lambda: {"origens": [], "destinos": []})
    monkeypatch.setattr(
        web,
        "avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True, "modo_operacao": "normal"},
    )

    client = web.app.test_client()
    _force_login(client, web, is_admin=False)
    resp = client.get("/fretes")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Análise de Fretes com Inteligência Artificial" in html
    assert "Painel Operacional de Fretes" in html
    assert "window.ROBERTO_BI_AUTHENTICATED = true" in html
    assert "const redirectUnauthenticatedPrivateAction = false;" in html
    assert "Faca login para enviar planilhas" not in html


def test_login_redireciona_quando_usuario_ja_autenticado_admin(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=True, is_admin=True))
    client = web.app.test_client()
    resp = client.get("/login")
    assert resp.status_code == 302
    assert "/admin" in (resp.headers.get("Location") or "")


def test_login_redireciona_quando_usuario_ja_autenticado_padrao(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=True, is_admin=False))
    client = web.app.test_client()
    resp = client.get("/login")
    assert resp.status_code == 302
    assert resp.headers.get("Location") == "/"


def test_roberto_upload_sem_login_permanece_bloqueado(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=False))
    client = web.app.test_client()
    resp = client.post("/api/roberto/upload", data={}, content_type="multipart/form-data")
    assert resp.status_code == 401
    payload = resp.get_json() or {}
    assert payload.get("require_login") is True


def test_roberto_upload_logado_continua_fluxo_oficial(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(
        web,
        "avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True, "modo_operacao": "normal"},
    )

    called = {"upload": 0}

    def _upload_stub():
        called["upload"] += 1
        return web.jsonify({"success": True, "registros": 1}), 200

    monkeypatch.setattr(web, "processar_upload_frete_excel", _upload_stub)
    client = web.app.test_client()
    _force_login(client, web, is_admin=False)
    resp = client.post("/api/roberto/upload", data={}, content_type="multipart/form-data")
    assert resp.status_code == 200
    payload = resp.get_json() or {}
    assert payload.get("success") is True
    assert called["upload"] == 1


def test_fretes_autenticado_nao_tem_redirect_privado_para_login(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(web, "_load_indices_payload", lambda: {"origens": [], "destinos": []})
    monkeypatch.setattr(
        web,
        "avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True, "modo_operacao": "normal"},
    )
    client = web.app.test_client()
    _force_login(client, web, is_admin=False)
    resp = client.get("/fretes")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "const redirectUnauthenticatedPrivateAction = false;" in html


def test_roberto_bi_privado_sem_login_permanece_bloqueado(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=False))
    client = web.app.test_client()
    resp = client.get("/api/roberto_bi/meta")
    assert resp.status_code == 401
    payload = resp.get_json() or {}
    assert payload.get("require_login") is True
