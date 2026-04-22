import os

import pytest

# Permite importar app.web em ambiente de teste sem depender da shell externa.
os.environ.setdefault("APP_ENV", "dev")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg2://test_user:test_pass@localhost:5432/test_db",
)

from app import web as web_app  # noqa: E402


class _FakeAdminUser:
    id = 77
    is_authenticated = True


@pytest.fixture
def client():
    app = web_app.app
    app.config["TESTING"] = True
    original_login_disabled = app.config.get("LOGIN_DISABLED", False)
    app.config["LOGIN_DISABLED"] = True
    try:
        yield app.test_client()
    finally:
        app.config["LOGIN_DISABLED"] = original_login_disabled


def test_admin_validacao_post_reprocessamento_preserva_contrato(client, monkeypatch):
    monkeypatch.setattr("app.painel_admin.admin_routes.verificar_acesso_admin", lambda: True)
    monkeypatch.setattr("app.painel_admin.admin_routes.current_user", _FakeAdminUser())

    def _fake_reprocessar(*, franquia_id, admin_user_id, limite):
        assert franquia_id == 12
        assert admin_user_id == 77
        assert limite == 9
        return {
            "ok": True,
            "franquia_id": franquia_id,
            "reprocessamento": {"total_analisado": 1, "total_resolvido": 1},
        }

    monkeypatch.setattr(
        "app.services.cleiton_franquia_validacao_admin_service.reprocessar_pendencias_monetizacao_franquia_admin",
        _fake_reprocessar,
    )

    resp = client.post(
        "/admin/api/cleiton-franquia/12/validacao",
        json={"acao": "reprocessar_pendencias_correlacao", "limite": "9"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["franquia_id"] == 12
    assert body["reprocessamento"]["total_analisado"] == 1


def test_admin_validacao_post_aplicar_correcao_compativel(client, monkeypatch):
    monkeypatch.setattr("app.painel_admin.admin_routes.verificar_acesso_admin", lambda: True)
    monkeypatch.setattr("app.painel_admin.admin_routes.current_user", _FakeAdminUser())

    calls = {}

    def _fake_obter_pacote(franquia_id, *, sincronizar_ciclo_leitura, aplicar_correcao):
        calls["franquia_id"] = franquia_id
        calls["sincronizar"] = sincronizar_ciclo_leitura
        calls["aplicar"] = aplicar_correcao
        return {"ok": True, "franquia_id": franquia_id, "modo": "leitura"}

    monkeypatch.setattr(
        "app.services.cleiton_franquia_validacao_admin_service.obter_pacote_validacao_franquia_cleiton",
        _fake_obter_pacote,
    )

    resp = client.post(
        "/admin/api/cleiton-franquia/33/validacao",
        json={"aplicar_correcao": True},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["franquia_id"] == 33
    assert calls == {"franquia_id": 33, "sincronizar": False, "aplicar": True}


def test_admin_validacao_trata_erro_valor_com_contrato_minimo(client, monkeypatch):
    monkeypatch.setattr("app.painel_admin.admin_routes.verificar_acesso_admin", lambda: True)
    monkeypatch.setattr("app.painel_admin.admin_routes.current_user", _FakeAdminUser())

    monkeypatch.setattr(
        "app.services.cleiton_franquia_validacao_admin_service.obter_pacote_validacao_franquia_cleiton",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("falha_controlada")),
    )

    resp = client.get("/admin/api/cleiton-franquia/10/validacao")
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["ok"] is False
    assert body["erro"] == "falha_controlada"
    assert body["error"] == "falha_controlada"
    assert body["codigo_erro"] == "admin_validacao_requisicao_invalida"


def test_admin_validacao_forbidden_retrocompativel(client, monkeypatch):
    monkeypatch.setattr("app.painel_admin.admin_routes.verificar_acesso_admin", lambda: False)
    resp = client.get("/admin/api/cleiton-franquia/10/validacao")
    assert resp.status_code == 403
    body = resp.get_json()
    assert body["ok"] is False
    assert body["erro"] == "forbidden"
    assert body["error"] == "forbidden"
    assert body["codigo_erro"] == "admin_validacao_forbidden"

