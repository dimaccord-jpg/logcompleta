import importlib
import os
from types import SimpleNamespace


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


def test_admin_agentes_cleide_forbidden_para_nao_admin(monkeypatch):
    web = _load_web_module()
    from app.painel_admin import admin_routes

    monkeypatch.setattr(
        admin_routes,
        "current_user",
        SimpleNamespace(is_authenticated=True, is_admin=False, email="user@example.com"),
    )
    monkeypatch.setattr(admin_routes, "verificar_acesso_admin", lambda: False)
    with web.app.test_request_context("/admin/agentes/cleide"):
        resp = admin_routes.agentes_cleide.__wrapped__()
        assert resp[1] == 403


def test_admin_agentes_cleide_get_ok_para_admin(monkeypatch):
    web = _load_web_module()
    from app.painel_admin import admin_routes

    monkeypatch.setattr(
        admin_routes,
        "current_user",
        SimpleNamespace(is_authenticated=True, is_admin=True, email="admin@example.com"),
    )
    monkeypatch.setattr(admin_routes, "verificar_acesso_admin", lambda: True)
    monkeypatch.setattr(
        "app.services.cleide_config_service.get_cleide_config",
        lambda: SimpleNamespace(
            upload_total_max=10000,
            chat_context_max_items_per_table=10,
            chat_context_rankings_limit=12,
            chat_context_max_text_len=80,
            chat_context_max_chars=6000,
            chat_response_max_chars=3000,
            chat_context_mode="executivo",
            chat_context_include_transportadora=1,
            chat_context_include_uf_origem=1,
            chat_context_include_uf_destino=1,
            chat_context_include_temporal=1,
            chat_context_include_paretos=1,
        ),
    )
    with web.app.test_request_context("/admin/agentes/cleide"):
        html = admin_routes.agentes_cleide.__wrapped__()
    assert isinstance(html, str)
    assert "Cleide — Controles de contexto" in html
    assert "chat_context_mode" in html
    assert "upload_total_max" in html


def test_admin_agentes_cleide_post_salva(monkeypatch):
    web = _load_web_module()
    from app.painel_admin import admin_routes

    monkeypatch.setattr(
        admin_routes,
        "current_user",
        SimpleNamespace(is_authenticated=True, is_admin=True, email="admin@example.com"),
    )
    monkeypatch.setattr(admin_routes, "verificar_acesso_admin", lambda: True)
    calls = {"saved": False}

    def _save(payload):
        calls["saved"] = True
        assert payload["chat_context_mode"] == "conservador"
        assert payload["chat_context_max_items_per_table"] == "7"
        assert payload["upload_total_max"] == "12345"
        assert payload["chat_response_max_chars"] == "3000"

    monkeypatch.setattr("app.services.cleide_config_service.salvar_cleide_config", _save)

    with web.app.test_request_context(
        "/admin/agentes/cleide",
        method="POST",
        data={
            "upload_total_max": "12345",
            "chat_context_max_items_per_table": "7",
            "chat_context_max_text_len": "90",
            "chat_context_rankings_limit": "5",
            "chat_response_max_chars": "3000",
            "chat_context_mode": "conservador",
            "chat_context_max_chars": "7000",
            "chat_context_include_transportadora": "on",
            "chat_context_include_uf_destino": "on",
        },
    ):
        resp = admin_routes.agentes_cleide.__wrapped__()
    assert resp.status_code == 302
    assert "/admin/agentes/cleide" in (resp.location or "")
    assert calls["saved"] is True
