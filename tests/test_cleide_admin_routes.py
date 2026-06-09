import importlib
import os
import pathlib
from types import SimpleNamespace

from app.extensions import db
from app.models import ConfigRegras
from app.services import cleide_audit_config_service


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


def _admin_user():
    return SimpleNamespace(is_authenticated=True, is_admin=True, email="admin@example.com")


def _bi_cfg(**overrides):
    base = {
        "upload_total_max": 10000,
        "chat_context_max_items_per_table": 10,
        "chat_context_rankings_limit": 12,
        "chat_context_max_text_len": 80,
        "chat_context_max_chars": 6000,
        "chat_response_max_chars": 3000,
        "chat_context_mode": "executivo",
        "chat_context_include_transportadora": 1,
        "chat_context_include_uf_origem": 1,
        "chat_context_include_uf_destino": 1,
        "chat_context_include_temporal": 1,
        "chat_context_include_paretos": 1,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _audit_cfg(**overrides):
    base = {
        "chat_enabled": True,
        "upload_enabled": True,
        "chat_max_history": 10,
        "document_context_max_chars": 24000,
        "max_documents_considered": 3,
        "question_max_chars": 4000,
        "fallback_message": cleide_audit_config_service.DEFAULT_FALLBACK_MESSAGE,
        "no_documents_behavior": "allow_guided",
        "show_documents_used": True,
        "no_hallucination_instruction_enabled": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _patch_admin_access(monkeypatch):
    from app.painel_admin import admin_routes

    monkeypatch.setattr(admin_routes, "current_user", _admin_user())
    monkeypatch.setattr(admin_routes, "verificar_acesso_admin", lambda: True)
    return admin_routes


def _register_admin_blueprint(app):
    from app.painel_admin.admin_routes import admin_bp

    if "admin" not in app.blueprints:
        app.register_blueprint(admin_bp)


def _render_cleide_admin(monkeypatch):
    web = _load_web_module()
    admin_routes = _patch_admin_access(monkeypatch)
    monkeypatch.setattr(
        "app.services.cleide_config_service.get_cleide_config",
        lambda: _bi_cfg(),
    )
    monkeypatch.setattr(
        "app.services.cleide_audit_config_service.get_cleide_audit_config",
        lambda: _audit_cfg(),
    )
    with web.app.test_request_context("/admin/agentes/cleide"):
        return admin_routes.agentes_cleide.__wrapped__()


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


def test_admin_agentes_cleide_get_retorna_200(monkeypatch):
    web = _load_web_module()
    admin_routes = _patch_admin_access(monkeypatch)
    monkeypatch.setattr(
        "app.services.cleide_config_service.get_cleide_config",
        lambda: _bi_cfg(),
    )
    monkeypatch.setattr(
        "app.services.cleide_audit_config_service.get_cleide_audit_config",
        lambda: _audit_cfg(),
    )
    with web.app.test_request_context("/admin/agentes/cleide"):
        html = admin_routes.agentes_cleide.__wrapped__()
    assert isinstance(html, str)
    assert "Cleide BI / Auditoria de Frete atual" in html
    assert "Cleide Auditoria documental" in html


def test_admin_agentes_cleide_get_renderiza_valores_audit_cfg(monkeypatch):
    fallback = "Mensagem fallback customizada para teste admin."
    web = _load_web_module()
    admin_routes = _patch_admin_access(monkeypatch)
    monkeypatch.setattr(
        "app.services.cleide_config_service.get_cleide_config",
        lambda: _bi_cfg(),
    )
    monkeypatch.setattr(
        "app.services.cleide_audit_config_service.get_cleide_audit_config",
        lambda: _audit_cfg(
            chat_enabled=True,
            upload_enabled=False,
            fallback_message=fallback,
        ),
    )
    with web.app.test_request_context("/admin/agentes/cleide"):
        html = admin_routes.agentes_cleide.__wrapped__()
    assert fallback in html
    assert 'name="chat_enabled"' in html
    assert 'name="upload_enabled"' in html
    assert 'name="fallback_message"' in html
    audit_form_start = html.index('name="form_name" value="cleide_audit"')
    audit_section = html[audit_form_start:audit_form_start + 4000]
    assert "checked" in audit_section.split('name="upload_enabled"')[0]


def test_admin_agentes_cleide_get_mostra_bloco_bi(monkeypatch):
    html = _render_cleide_admin(monkeypatch)
    assert "Cleide BI / Auditoria de Frete atual" in html
    assert "upload_total_max" in html
    assert "chat_context_mode" in html
    assert 'name="form_name" value="cleide_bi"' in html


def test_admin_agentes_cleide_get_mostra_bloco_auditoria(monkeypatch):
    html = _render_cleide_admin(monkeypatch)
    assert "Cleide Auditoria documental" in html
    assert "cleide_audit_cfg_chat_enabled" in html
    assert "cleide_audit_cfg_fallback_message" in html
    assert 'name="form_name" value="cleide_audit"' in html


def test_admin_agentes_cleide_get_mostra_aviso_limites_cleiton(monkeypatch):
    html = _render_cleide_admin(monkeypatch)
    assert (
        "Formatos permitidos, TTL, tamanho máximo e limites técnicos por tipo continuam sendo controlados globalmente em"
        in html
    )
    assert "/admin/agentes/cleiton" in html


def test_admin_agentes_cleide_get_campos_audit_no_form_correto(monkeypatch):
    html = _render_cleide_admin(monkeypatch)
    audit_fields = [
        "chat_enabled",
        "upload_enabled",
        "chat_max_history",
        "document_context_max_chars",
        "max_documents_considered",
        "question_max_chars",
        "no_documents_behavior",
        "show_documents_used",
        "no_hallucination_instruction_enabled",
        "fallback_message",
    ]
    for field in audit_fields:
        assert f'name="{field}"' in html
    assert "pdf_max_pages" not in html
    assert "upload_ttl_hours" not in html


def test_admin_agentes_cleide_get_campos_bi_no_bloco_bi(monkeypatch):
    html = _render_cleide_admin(monkeypatch)
    bi_fields = [
        "upload_total_max",
        "chat_context_max_items_per_table",
        "chat_context_rankings_limit",
        "chat_context_max_text_len",
        "chat_context_max_chars",
        "chat_response_max_chars",
        "chat_context_mode",
        "chat_context_include_transportadora",
    ]
    for field in bi_fields:
        assert f'name="{field}"' in html


def test_admin_agentes_cleide_post_bi_salva(monkeypatch):
    web = _load_web_module()
    admin_routes = _patch_admin_access(monkeypatch)
    calls = {"saved": False}

    def _save(payload):
        calls["saved"] = True
        assert payload["chat_context_mode"] == "conservador"
        assert payload["chat_context_max_items_per_table"] == "7"
        assert payload["upload_total_max"] == "12345"
        assert payload["chat_response_max_chars"] == "3000"

    monkeypatch.setattr("app.services.cleide_config_service.salvar_cleide_config", _save)
    monkeypatch.setattr(
        "app.services.cleide_audit_config_service.salvar_cleide_audit_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("audit save should not run")),
    )

    with web.app.test_request_context(
        "/admin/agentes/cleide",
        method="POST",
        data={
            "form_name": "cleide_bi",
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
        from flask import get_flashed_messages

        msgs = get_flashed_messages(with_categories=True)

    assert resp.status_code == 302
    assert "/admin/agentes/cleide" in (resp.location or "")
    assert calls["saved"] is True
    assert any("Cleide BI" in msg for _, msg in msgs)


def test_admin_agentes_cleide_post_audit_salva(monkeypatch):
    web = _load_web_module()
    admin_routes = _patch_admin_access(monkeypatch)
    calls = {"saved": False}

    def _save(payload):
        calls["saved"] = True
        assert payload["chat_max_history"] == "8"
        assert payload["question_max_chars"] == "3500"
        assert payload["no_documents_behavior"] == "require_documents"
        assert payload["fallback_message"] == "Falha temporária da Cleide Auditoria."

    monkeypatch.setattr("app.services.cleide_config_service.salvar_cleide_config", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("bi save should not run")))
    monkeypatch.setattr(
        "app.services.cleide_audit_config_service.salvar_cleide_audit_config",
        _save,
    )

    with web.app.test_request_context(
        "/admin/agentes/cleide",
        method="POST",
        data={
            "form_name": "cleide_audit",
            "chat_enabled": "on",
            "upload_enabled": "on",
            "chat_max_history": "8",
            "document_context_max_chars": "18000",
            "max_documents_considered": "2",
            "question_max_chars": "3500",
            "no_documents_behavior": "require_documents",
            "show_documents_used": "on",
            "no_hallucination_instruction_enabled": "on",
            "fallback_message": "Falha temporária da Cleide Auditoria.",
        },
    ):
        resp = admin_routes.agentes_cleide.__wrapped__()
        from flask import get_flashed_messages

        msgs = get_flashed_messages(with_categories=True)

    assert resp.status_code == 302
    assert calls["saved"] is True
    assert any("Cleide Auditoria documental" in msg for _, msg in msgs)


def test_admin_agentes_cleide_post_audit_invalido_nao_quebra(monkeypatch):
    web = _load_web_module()
    admin_routes = _patch_admin_access(monkeypatch)
    monkeypatch.setattr(
        "app.services.cleide_config_service.get_cleide_config",
        lambda: _bi_cfg(),
    )
    monkeypatch.setattr(
        "app.services.cleide_audit_config_service.get_cleide_audit_config",
        lambda: _audit_cfg(),
    )
    monkeypatch.setattr(
        "app.services.cleide_audit_config_service.salvar_cleide_audit_config",
        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("chat_max_history fora da faixa permitida.")),
    )

    with web.app.test_request_context(
        "/admin/agentes/cleide",
        method="POST",
        data={
            "form_name": "cleide_audit",
            "chat_max_history": "999",
            "document_context_max_chars": "18000",
            "max_documents_considered": "2",
            "question_max_chars": "3500",
            "no_documents_behavior": "allow_guided",
            "fallback_message": "ok",
        },
    ):
        resp = admin_routes.agentes_cleide.__wrapped__()
        from flask import get_flashed_messages

        msgs = get_flashed_messages(with_categories=True)

    assert resp.status_code == 302
    assert any("chat_max_history fora da faixa permitida." in msg for _, msg in msgs)

    with web.app.test_request_context("/admin/agentes/cleide"):
        html = admin_routes.agentes_cleide.__wrapped__()
    assert "Cleide Auditoria documental" in html
    assert 'name="fallback_message"' in html


def test_admin_agentes_cleide_post_form_desconhecido(monkeypatch):
    web = _load_web_module()
    admin_routes = _patch_admin_access(monkeypatch)
    monkeypatch.setattr(
        "app.services.cleide_config_service.get_cleide_config",
        lambda: _bi_cfg(),
    )
    monkeypatch.setattr(
        "app.services.cleide_audit_config_service.get_cleide_audit_config",
        lambda: _audit_cfg(),
    )

    with web.app.test_request_context(
        "/admin/agentes/cleide",
        method="POST",
        data={"form_name": "outro"},
    ):
        resp = admin_routes.agentes_cleide.__wrapped__()
        from flask import get_flashed_messages

        msgs = get_flashed_messages(with_categories=True)

    assert resp.status_code == 302
    assert any("Formulário não reconhecido" in msg for _, msg in msgs)

    with web.app.test_request_context("/admin/agentes/cleide"):
        html = admin_routes.agentes_cleide.__wrapped__()
    assert "Cleide BI / Auditoria de Frete atual" in html
    assert "Cleide Auditoria documental" in html


def test_admin_agentes_cleide_post_audit_nao_altera_cleide_cfg(app, ctx, monkeypatch):
    from app.painel_admin import admin_routes

    app.config["SECRET_KEY"] = "test-secret"
    _register_admin_blueprint(app)
    monkeypatch.setattr(admin_routes, "current_user", _admin_user())
    monkeypatch.setattr(admin_routes, "verificar_acesso_admin", lambda: True)

    db.session.add(ConfigRegras(chave="cleide_cfg_upload_total_max", valor_inteiro=7777))
    db.session.add(ConfigRegras(chave="cleide_cfg_chat_context_mode", valor_texto="executivo"))
    db.session.commit()

    with app.test_request_context(
        "/admin/agentes/cleide",
        method="POST",
        data={
            "form_name": "cleide_audit",
            "chat_enabled": "0",
            "upload_enabled": "1",
            "chat_max_history": "6",
            "document_context_max_chars": "15000",
            "max_documents_considered": "2",
            "question_max_chars": "3000",
            "no_documents_behavior": "allow_guided",
            "fallback_message": "fallback audit",
        },
    ):
        admin_routes.agentes_cleide.__wrapped__()

    bi_upload = ConfigRegras.query.filter_by(chave="cleide_cfg_upload_total_max").first()
    bi_mode = ConfigRegras.query.filter_by(chave="cleide_cfg_chat_context_mode").first()
    audit_history = ConfigRegras.query.filter_by(chave="cleide_audit_cfg_chat_max_history").first()

    assert bi_upload.valor_inteiro == 7777
    assert bi_mode.valor_texto == "executivo"
    assert audit_history.valor_inteiro == 6


def test_admin_agentes_cleide_post_bi_nao_altera_cleide_audit_cfg(app, ctx, monkeypatch):
    from app.painel_admin import admin_routes

    app.config["SECRET_KEY"] = "test-secret"
    _register_admin_blueprint(app)
    monkeypatch.setattr(admin_routes, "current_user", _admin_user())
    monkeypatch.setattr(admin_routes, "verificar_acesso_admin", lambda: True)

    cleide_audit_config_service.salvar_cleide_audit_config(
        {
            "chat_max_history": "9",
            "question_max_chars": "4100",
            "fallback_message": "mensagem audit original",
        }
    )

    with app.test_request_context(
        "/admin/agentes/cleide",
        method="POST",
        data={
            "form_name": "cleide_bi",
            "upload_total_max": "15000",
            "chat_context_max_items_per_table": "8",
            "chat_context_max_text_len": "64",
            "chat_context_rankings_limit": "10",
            "chat_response_max_chars": "2800",
            "chat_context_mode": "conservador",
            "chat_context_max_chars": "5500",
        },
    ):
        admin_routes.agentes_cleide.__wrapped__()

    audit_history = ConfigRegras.query.filter_by(chave="cleide_audit_cfg_chat_max_history").first()
    audit_question = ConfigRegras.query.filter_by(chave="cleide_audit_cfg_question_max_chars").first()
    audit_fallback = ConfigRegras.query.filter_by(chave="cleide_audit_cfg_fallback_message").first()
    bi_upload = ConfigRegras.query.filter_by(chave="cleide_cfg_upload_total_max").first()

    assert audit_history.valor_inteiro == 9
    assert audit_question.valor_inteiro == 4100
    assert audit_fallback.valor_texto == "mensagem audit original"
    assert bi_upload.valor_inteiro == 15000


def test_admin_agentes_cleide_post_audit_invalido_nao_altera_bi(app, ctx, monkeypatch):
    from app.painel_admin import admin_routes

    app.config["SECRET_KEY"] = "test-secret"
    _register_admin_blueprint(app)
    monkeypatch.setattr(admin_routes, "current_user", _admin_user())
    monkeypatch.setattr(admin_routes, "verificar_acesso_admin", lambda: True)

    db.session.add(ConfigRegras(chave="cleide_cfg_upload_total_max", valor_inteiro=4321))
    db.session.commit()

    with app.test_request_context(
        "/admin/agentes/cleide",
        method="POST",
        data={
            "form_name": "cleide_audit",
            "chat_max_history": "0",
            "document_context_max_chars": "18000",
            "max_documents_considered": "2",
            "question_max_chars": "3500",
            "no_documents_behavior": "allow_guided",
            "fallback_message": "ok",
        },
    ):
        admin_routes.agentes_cleide.__wrapped__()

    bi_upload = ConfigRegras.query.filter_by(chave="cleide_cfg_upload_total_max").first()
    audit_history = ConfigRegras.query.filter_by(chave="cleide_audit_cfg_chat_max_history").first()

    assert bi_upload.valor_inteiro == 4321
    assert audit_history is None or audit_history.valor_inteiro != 0


def test_regressao_admin_cleiton_nao_alterado():
    html = pathlib.Path("app/painel_admin/template_admin/agentes_cleiton.html").read_text(encoding="utf-8")
    assert "Cleiton - Parâmetros operacionais" in html
    assert "cleiton_doc_form" in html


def test_regressao_admin_julia_nao_alterado():
    html = pathlib.Path("app/painel_admin/template_admin/agentes_julia.html").read_text(encoding="utf-8")
    assert "julia_chat_max_history" in html


def test_regressao_runtime_auditoria_nao_alterado():
    routes = pathlib.Path("app/cleide_audit_routes.py").read_text(encoding="utf-8")
    assert "get_cleide_audit_config" in routes
    assert "salvar_cleide_audit_config" not in routes


def test_regressao_frontend_auditoria_nao_alterado():
    html = pathlib.Path("app/templates/cleide_auditoria.html").read_text(encoding="utf-8")
    js = pathlib.Path("app/static/js/cleide_auditoria.js").read_text(encoding="utf-8")
    assert "cleide-auditoria" in html or "cleide_auditoria" in html
    assert "/api/cleide-auditoria/" in js


def test_regressao_cleide_bi_campos_preservados(monkeypatch):
    html = _render_cleide_admin(monkeypatch)
    for field in (
        "upload_total_max",
        "chat_context_max_items_per_table",
        "chat_context_rankings_limit",
        "chat_context_max_text_len",
        "chat_context_max_chars",
        "chat_response_max_chars",
        "chat_context_mode",
    ):
        assert f'name="{field}"' in html
