import importlib
import os
from types import SimpleNamespace

import flask_login.utils


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


def _set_current_user(monkeypatch, *, user_id="1"):
    fake_user = SimpleNamespace(
        is_authenticated=True,
        is_active=True,
        is_anonymous=False,
        get_id=lambda: user_id,
        conta_id=1,
        franquia_id=1,
        categoria="pro",
        full_name="Teste Cleide",
        email="cleide@example.com",
        franquia=None,
    )
    monkeypatch.setattr(flask_login.utils, "_get_user", lambda: fake_user)
    return fake_user


def test_html_cleide_nao_referencia_contratos_roberto(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", _set_current_user(monkeypatch))
    monkeypatch.setattr(
        "app.cleide_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True, "modo_operacao": "normal"},
    )
    monkeypatch.setattr("app.cleide_routes.get_cleide_config", lambda: SimpleNamespace(layout_version=1))

    client = web.app.test_client()
    resp = client.get("/auditoria-frete")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "roberto_upload_ref" not in html
    assert "roberto_upload_tmp" not in html
    assert "chat_roberto" not in html
    assert 'agent="roberto"' not in html
    assert "roberto_cfg_" not in html
    assert "GEMINI_API_KEY_ROBERTO" not in html


def test_html_cleide_sem_ativacao_funcional_real(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", _set_current_user(monkeypatch))
    monkeypatch.setattr(
        "app.cleide_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True, "modo_operacao": "normal"},
    )
    monkeypatch.setattr("app.cleide_routes.get_cleide_config", lambda: SimpleNamespace(layout_version=1))

    client = web.app.test_client()
    resp = client.get("/auditoria-frete")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "/api/roberto/" not in html
    assert "/api/chat_roberto" not in html
    assert "chat_roberto_fretes.js" not in html
    assert "Chat operacional da Cleide" in html
    assert "id=\"cleideChatFloating\"" in html
    assert "id=\"cleideChatToggle\"" in html
    assert "id=\"cleideChatPanel\"" in html
    assert "id=\"cleideChatClose\"" in html


def test_roberto_endpoints_seguem_importaveis():
    import app.run_roberto_chat as roberto_chat

    assert hasattr(roberto_chat, "chat_roberto_reply")


def test_js_cleide_sem_ia_recomendacoes_billing_ou_chat_funcional():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "app" / "static" / "js" / "cleide_auditoria_frete.js"
    ).read_text(encoding="utf-8")

    forbidden = [
        "processingevent",
        "iaconsumoevento",
        "flow_type_roberto",
        "/api/roberto/",
        "/api/chat_roberto",
        "openai",
        "gemini",
        "billing",
        "consumo",
    ]
    for token in forbidden:
        assert token not in source.lower()
    assert "/api/chat_cleide" in source
    assert "json.stringify({ question, history: buildchathistory() })" in source.lower()
    assert ".roberto-" not in source
    assert "cleidechattoggle" in source.lower()
    assert "cleidechatpanel" in source.lower()
    assert "setcleidechatopen" in source.lower()


def test_js_cleide_sem_endpoint_roberto_flow_parser_payload():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "app" / "static" / "js" / "cleide_auditoria_frete.js"
    ).read_text(encoding="utf-8").lower()
    assert "flow_type_roberto" not in source
    assert "parser_roberto" not in source
    assert "payload_roberto" not in source
    assert "/api/roberto/" not in source


def test_operational_context_sem_referencias_roboto_ou_ia_runtime():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "cleide_operational_context.py").read_text(
        encoding="utf-8"
    ).lower()
    assert "/api/roberto/" not in source
    assert "flow_type_roberto" not in source
    assert "payload_roberto" not in source
    assert "openai" not in source
    assert "gemini" not in source
    assert "processingevent" not in source
    assert "iaconsumoevento" not in source


def test_chat_context_layer_sem_roberto_sem_ia_runtime():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "cleide_chat_context.py").read_text(
        encoding="utf-8"
    ).lower()
    assert "/api/roberto/" not in source
    assert "flow_type_roberto" not in source
    assert "payload_roberto" not in source
    assert "openai" not in source
    assert "gemini" not in source
    assert "processingevent" not in source
    assert "iaconsumoevento" not in source


def test_cleide_adapter_sem_import_run_roberto_chat():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "cleide_gemini_adapter.py").read_text(
        encoding="utf-8"
    ).lower()
    assert "run_roberto_chat" not in source
    assert "flow_type_roberto" not in source
