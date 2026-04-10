import importlib
import os
from types import SimpleNamespace


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


def test_api_chat_roberto_401_when_not_authenticated(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=False))

    client = web.app.test_client()
    resp = client.post("/api/chat_roberto", json={"message": "oi", "history": []})
    assert resp.status_code == 401
    body = resp.get_json()
    assert body["require_login"] is True


def test_api_chat_roberto_bloqueado_por_franquia(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=True))
    monkeypatch.setattr(
        web,
        "avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": False, "mensagem_usuario": "Bloqueado por franquia."},
    )

    client = web.app.test_client()
    resp = client.post("/api/chat_roberto", json={"message": "analise", "history": []})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["limit_reached"] is True
    assert body["reply"] == "Bloqueado por franquia."


def test_api_chat_roberto_sucesso_fluxo_permitido(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=True))
    monkeypatch.setattr(
        web,
        "avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True, "modo_operacao": "normal"},
    )

    fake_cfg = SimpleNamespace(chat_max_history=7)

    import app.services.roberto_config_service as roberto_cfg
    import app.run_roberto_chat as roberto_chat

    monkeypatch.setattr(roberto_cfg, "get_roberto_config", lambda: fake_cfg)

    captured = {}

    def _fake_chat(message, history, max_history, **kwargs):
        captured["message"] = message
        captured["history"] = history
        captured["max_history"] = max_history
        return {"reply": "ok-roberto", "suggestions": []}

    monkeypatch.setattr(roberto_chat, "chat_roberto_reply", _fake_chat)

    client = web.app.test_client()
    payload = {"message": "me de um resumo", "history": [{"role": "user", "content": "x"}]}
    resp = client.post("/api/chat_roberto", json=payload)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["reply"] == "ok-roberto"
    assert body["limit_reached"] is False
    assert body["max_history"] == 7
    assert captured["message"] == "me de um resumo"
    assert captured["max_history"] == 7
