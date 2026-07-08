import importlib
import json
import os
import pathlib
from types import SimpleNamespace

from werkzeug.datastructures import MultiDict

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
        "audited_file_max_bytes": None,
        "audited_file_max_rows": 2000,
        "calculation_bases": cleide_audit_config_service.DEFAULT_CALCULATION_BASES,
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


def _calculation_bases_form_rows(rows):
    data = [("form_name", "cleide_audit_calculation_bases")]
    for index, row in enumerate(rows):
        parameters = row.get("parameters") or {}
        data.extend(
            [
                ("calculation_base_row_index", str(index)),
                (f"calculation_base_id_{index}", row.get("id", "")),
                (f"calculation_base_label_{index}", row.get("label", "")),
                (f"calculation_base_unit_{index}", row.get("unit", "")),
                (
                    f"calculation_base_calculation_type_{index}",
                    row.get("calculation_type", "fixed_amount"),
                ),
                (f"calculation_base_operation_{index}", row.get("operation", "fixed_amount")),
                (f"calculation_base_audit_variable_{index}", row.get("audit_variable") or ""),
                (f"calculation_base_fraction_size_{index}", str(parameters.get("fraction_size", ""))),
                (f"calculation_base_display_order_{index}", str(row.get("display_order", (index + 1) * 10))),
            ]
        )
        aliases = row.get("aliases", "")
        if isinstance(aliases, list):
            aliases = "; ".join(aliases)
        data.append((f"calculation_base_aliases_{index}", aliases))
        if row.get("is_active", True):
            data.append((f"calculation_base_is_active_{index}", "1"))
        if row.get("allows_minimum", False):
            data.append((f"calculation_base_allows_minimum_{index}", "1"))
        if row.get("allows_maximum", False):
            data.append((f"calculation_base_allows_maximum_{index}", "1"))
        if row.get("requires_structured_condition", False):
            data.append((f"calculation_base_requires_structured_condition_{index}", "1"))
    return MultiDict(data)


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
    assert "Bases de cálculo da auditoria" in html
    assert "cleide_audit_cfg_calculation_bases" in html
    assert 'name="form_name" value="cleide_audit_calculation_bases"' in html
    assert 'id="cleideAuditCalculationBasesTable"' in html
    assert 'name="calculation_base_label_0"' in html
    assert 'name="calculation_bases_json"' not in html
    assert "% por nota fiscal" in html


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
        "audited_file_max_bytes",
        "audited_file_max_rows",
        "no_documents_behavior",
        "show_documents_used",
        "no_hallucination_instruction_enabled",
        "fallback_message",
    ]
    for field in audit_fields:
        assert f'name="{field}"' in html
    assert "Máximo de linhas por lote auditado" in html
    assert "Limite específico do arquivo auditado" in html
    assert "cleide_audit_cfg_audited_file_max_bytes" in html
    assert "cleide_audit_cfg_audited_file_max_rows" in html
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
        assert payload["audited_file_max_bytes"] == "1048576"
        assert payload["audited_file_max_rows"] == "10"
        assert payload["no_documents_behavior"] == "require_documents"
        assert payload["fallback_message"] == "Falha temporária da Cleide Auditoria."
        assert payload["document_context_max_chars"] == "18000"
        assert payload["max_documents_considered"] == "2"
        assert payload["show_documents_used"] == "on"
        assert payload["no_hallucination_instruction_enabled"] == "on"
        assert payload["chat_enabled"] == "on"
        assert payload["upload_enabled"] == "on"

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
            "audited_file_max_bytes": "1048576",
            "audited_file_max_rows": "10",
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


def test_admin_agentes_cleide_post_audit_persiste_audited_file_max_rows(app, ctx, monkeypatch):
    from app.painel_admin import admin_routes

    app.config["SECRET_KEY"] = "test-secret"
    _register_admin_blueprint(app)
    monkeypatch.setattr(admin_routes, "current_user", _admin_user())
    monkeypatch.setattr(admin_routes, "verificar_acesso_admin", lambda: True)

    with app.test_request_context(
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
            "audited_file_max_bytes": "1048576",
            "audited_file_max_rows": "10",
            "no_documents_behavior": "allow_guided",
            "show_documents_used": "on",
            "no_hallucination_instruction_enabled": "on",
            "fallback_message": "Falha temporária da Cleide Auditoria.",
        },
    ):
        admin_routes.agentes_cleide.__wrapped__()

    row_bytes = ConfigRegras.query.filter_by(
        chave="cleide_audit_cfg_audited_file_max_bytes"
    ).first()
    row_rows = ConfigRegras.query.filter_by(chave="cleide_audit_cfg_audited_file_max_rows").first()
    assert row_bytes is not None
    assert row_bytes.valor_inteiro == 1048576
    assert row_rows is not None
    assert row_rows.valor_inteiro == 10
    loaded = cleide_audit_config_service.get_cleide_audit_config()
    assert loaded.audited_file_max_bytes == 1048576
    assert loaded.audited_file_max_rows == 10


def test_admin_agentes_cleide_post_audit_limite_bytes_acima_global_flash_warning(
    app,
    ctx,
    monkeypatch,
):
    from app.painel_admin import admin_routes
    from app.services.cleiton_doc_config_service import salvar_cleiton_doc_config

    app.config["SECRET_KEY"] = "test-secret"
    _register_admin_blueprint(app)
    monkeypatch.setattr(admin_routes, "current_user", _admin_user())
    monkeypatch.setattr(admin_routes, "verificar_acesso_admin", lambda: True)
    salvar_cleiton_doc_config({"excel_max_bytes": "1048576"})

    with app.test_request_context(
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
            "audited_file_max_bytes": "2097152",
            "audited_file_max_rows": "10",
            "no_documents_behavior": "allow_guided",
            "show_documents_used": "on",
            "no_hallucination_instruction_enabled": "on",
            "fallback_message": "Falha temporária da Cleide Auditoria.",
        },
    ):
        resp = admin_routes.agentes_cleide.__wrapped__()
        from flask import get_flashed_messages

        msgs = get_flashed_messages(with_categories=True)

    assert resp.status_code == 302
    assert any(
        category == "warning"
        and "não pode ultrapassar o limite global de Excel definido em Cleiton" in msg
        for category, msg in msgs
    )
    assert (
        ConfigRegras.query.filter_by(chave="cleide_audit_cfg_audited_file_max_bytes").first()
        is None
    )


def test_admin_agentes_cleide_post_audit_excecao_inesperada_rollback_e_log(
    app,
    ctx,
    monkeypatch,
    caplog,
):
    from app.painel_admin import admin_routes

    app.config["SECRET_KEY"] = "test-secret"
    _register_admin_blueprint(app)
    monkeypatch.setattr(admin_routes, "current_user", _admin_user())
    monkeypatch.setattr(admin_routes, "verificar_acesso_admin", lambda: True)

    def _raise_after_pending_write(_payload):
        db.session.add(ConfigRegras(chave="cleide_audit_cfg_partial_unexpected", valor_inteiro=1))
        raise RuntimeError("boom interno")

    monkeypatch.setattr(
        "app.services.cleide_audit_config_service.salvar_cleide_audit_config",
        _raise_after_pending_write,
    )

    with app.test_request_context(
        "/admin/agentes/cleide",
        method="POST",
        data={
            "form_name": "cleide_audit",
            "chat_enabled": "on",
            "chat_max_history": "8",
            "document_context_max_chars": "18000",
            "max_documents_considered": "2",
            "question_max_chars": "3500",
            "audited_file_max_bytes": "",
            "audited_file_max_rows": "10",
            "no_documents_behavior": "allow_guided",
            "fallback_message": "Falha temporária da Cleide Auditoria.",
        },
    ):
        with caplog.at_level("ERROR", logger="app.painel_admin.admin_routes"):
            resp = admin_routes.agentes_cleide.__wrapped__()
        from flask import get_flashed_messages

        msgs = get_flashed_messages(with_categories=True)

    assert resp.status_code == 302
    assert any(
        category == "danger"
        and "Não foi possível salvar os parâmetros da Cleide Auditoria documental." in msg
        for category, msg in msgs
    )
    assert (
        ConfigRegras.query.filter_by(chave="cleide_audit_cfg_partial_unexpected").first()
        is None
    )
    assert "form_name=cleide_audit" in caplog.text
    assert "audited_file_max_bytes" in caplog.text
    assert "Falha temporária da Cleide Auditoria" not in caplog.text


def test_admin_agentes_cleide_post_calculation_bases_tabular_salva_duas_bases(
    app,
    ctx,
    monkeypatch,
):
    from app.painel_admin import admin_routes

    app.config["SECRET_KEY"] = "test-secret"
    _register_admin_blueprint(app)
    monkeypatch.setattr(admin_routes, "current_user", _admin_user())
    monkeypatch.setattr(admin_routes, "verificar_acesso_admin", lambda: True)

    with app.test_request_context(
        "/admin/agentes/cleide",
        method="POST",
        data=_calculation_bases_form_rows(
            [
                cleide_audit_config_service.DEFAULT_CALCULATION_BASES[0],
                cleide_audit_config_service.DEFAULT_CALCULATION_BASES[1],
            ]
        ),
    ):
        resp = admin_routes.agentes_cleide.__wrapped__()
        from flask import get_flashed_messages

        msgs = get_flashed_messages(with_categories=True)

    assert resp.status_code == 302
    assert any("Bases de cálculo da Cleide Auditoria" in msg for _, msg in msgs)
    row = ConfigRegras.query.filter_by(chave="cleide_audit_cfg_calculation_bases").first()
    assert row is not None
    persisted = json.loads(row.valor_texto)
    assert [base["label"] for base in persisted] == [
        "% por nota fiscal",
        "por CTe",
    ]
    assert [base["id"] for base in persisted] == ["pct_nota_fiscal", "por_cte"]


def test_admin_agentes_cleide_post_calculation_bases_tabular_aliases_e_fracao(
    app,
    ctx,
    monkeypatch,
):
    from app.painel_admin import admin_routes

    app.config["SECRET_KEY"] = "test-secret"
    _register_admin_blueprint(app)
    monkeypatch.setattr(admin_routes, "current_user", _admin_user())
    monkeypatch.setattr(admin_routes, "verificar_acesso_admin", lambda: True)

    with app.test_request_context(
        "/admin/agentes/cleide",
        method="POST",
        data=_calculation_bases_form_rows(
            [
                {
                    "id": "pct_nota_fiscal",
                    "label": "% por nota fiscal",
                    "aliases": "valor da nf; sobre nf; nota fiscal",
                    "unit": "%",
                    "calculation_type": "invoice_percentage",
                    "audit_variable": "valor_nf",
                    "operation": "percentage_of_variable",
                    "display_order": 10,
                    "is_active": True,
                },
                {
                    "id": "fracao_100kg",
                    "label": "por fração de 100kg",
                    "aliases": "100kg ou fração, cada 100kg",
                    "unit": "R$",
                    "calculation_type": "weight_fraction",
                    "audit_variable": "peso",
                    "operation": "ceil_fraction",
                    "parameters": {"fraction_size": 100},
                    "display_order": 20,
                    "is_active": True,
                },
            ]
        ),
    ):
        admin_routes.agentes_cleide.__wrapped__()

    row = ConfigRegras.query.filter_by(chave="cleide_audit_cfg_calculation_bases").first()
    persisted = json.loads(row.valor_texto)
    assert persisted[0]["aliases"] == ["valor da nf", "sobre nf", "nota fiscal"]
    assert persisted[1]["parameters"] == {"fraction_size": 100}


def test_admin_agentes_cleide_post_calculation_bases_excluir_remove_linha(
    app,
    ctx,
    monkeypatch,
):
    from app.painel_admin import admin_routes

    app.config["SECRET_KEY"] = "test-secret"
    _register_admin_blueprint(app)
    monkeypatch.setattr(admin_routes, "current_user", _admin_user())
    monkeypatch.setattr(admin_routes, "verificar_acesso_admin", lambda: True)
    cleide_audit_config_service.salvar_cleide_audit_calculation_bases(
        [
            cleide_audit_config_service.DEFAULT_CALCULATION_BASES[0],
            cleide_audit_config_service.DEFAULT_CALCULATION_BASES[1],
        ]
    )

    with app.test_request_context(
        "/admin/agentes/cleide",
        method="POST",
        data=_calculation_bases_form_rows([cleide_audit_config_service.DEFAULT_CALCULATION_BASES[0]]),
    ):
        admin_routes.agentes_cleide.__wrapped__()

    persisted = cleide_audit_config_service.carregar_cleide_audit_calculation_bases()
    assert [base["id"] for base in persisted] == ["pct_nota_fiscal"]


def test_admin_agentes_cleide_post_calculation_bases_adicionar_gera_id(
    app,
    ctx,
    monkeypatch,
):
    from app.painel_admin import admin_routes

    app.config["SECRET_KEY"] = "test-secret"
    _register_admin_blueprint(app)
    monkeypatch.setattr(admin_routes, "current_user", _admin_user())
    monkeypatch.setattr(admin_routes, "verificar_acesso_admin", lambda: True)

    with app.test_request_context(
        "/admin/agentes/cleide",
        method="POST",
        data=_calculation_bases_form_rows(
            [
                {
                    "label": "Taxa administrativa",
                    "aliases": "taxa adm",
                    "unit": "R$",
                    "calculation_type": "fixed_amount",
                    "operation": "fixed_amount",
                    "display_order": 10,
                    "is_active": True,
                }
            ]
        ),
    ):
        admin_routes.agentes_cleide.__wrapped__()

    persisted = cleide_audit_config_service.carregar_cleide_audit_calculation_bases()
    assert persisted[0]["id"] == "taxa_administrativa"
    assert persisted[0]["label"] == "Taxa administrativa"


def test_admin_agentes_cleide_post_calculation_bases_invalido_preserva_anterior(
    app,
    ctx,
    monkeypatch,
):
    from app.painel_admin import admin_routes

    app.config["SECRET_KEY"] = "test-secret"
    _register_admin_blueprint(app)
    monkeypatch.setattr(admin_routes, "current_user", _admin_user())
    monkeypatch.setattr(admin_routes, "verificar_acesso_admin", lambda: True)
    cleide_audit_config_service.salvar_cleide_audit_calculation_bases(
        [cleide_audit_config_service.DEFAULT_CALCULATION_BASES[1]]
    )
    before = ConfigRegras.query.filter_by(
        chave="cleide_audit_cfg_calculation_bases"
    ).first().valor_texto

    with app.test_request_context(
        "/admin/agentes/cleide",
        method="POST",
        data=_calculation_bases_form_rows(
            [
                {
                    "id": "base_invalida",
                    "label": "",
                    "unit": "R$",
                    "calculation_type": "fixed_amount",
                    "operation": "fixed_amount",
                    "display_order": 10,
                    "is_active": True,
                }
            ]
        ),
    ):
        resp = admin_routes.agentes_cleide.__wrapped__()
        from flask import get_flashed_messages

        msgs = get_flashed_messages(with_categories=True)

    after = ConfigRegras.query.filter_by(
        chave="cleide_audit_cfg_calculation_bases"
    ).first().valor_texto
    assert resp.status_code == 302
    assert any("nome da base é obrigatório" in msg for _, msg in msgs)
    assert after == before


def test_admin_agentes_cleide_post_calculation_bases_nao_altera_cleide_bi(
    app,
    ctx,
    monkeypatch,
):
    from app.painel_admin import admin_routes

    app.config["SECRET_KEY"] = "test-secret"
    _register_admin_blueprint(app)
    monkeypatch.setattr(admin_routes, "current_user", _admin_user())
    monkeypatch.setattr(admin_routes, "verificar_acesso_admin", lambda: True)
    db.session.add(ConfigRegras(chave="cleide_cfg_upload_total_max", valor_inteiro=9876))
    db.session.commit()

    with app.test_request_context(
        "/admin/agentes/cleide",
        method="POST",
        data=_calculation_bases_form_rows([cleide_audit_config_service.DEFAULT_CALCULATION_BASES[0]]),
    ):
        admin_routes.agentes_cleide.__wrapped__()

    bi_upload = ConfigRegras.query.filter_by(chave="cleide_cfg_upload_total_max").first()
    assert bi_upload.valor_inteiro == 9876


def test_admin_agentes_cleide_get_carrega_calculation_bases_salvas(monkeypatch):
    html = _render_cleide_admin(monkeypatch)
    table = html.split('id="cleideAuditCalculationBasesTable"')[1]
    assert 'name="calculation_base_id_0" value="pct_nota_fiscal"' in table
    assert "percentage_of_variable" in table
    assert 'value="fracao_100kg"' in table
    assert "Avançado/Debug" in html


def test_admin_agentes_cleide_get_renderiza_audited_file_max_rows_salvo(monkeypatch):
    web = _load_web_module()
    admin_routes = _patch_admin_access(monkeypatch)
    monkeypatch.setattr(
        "app.services.cleide_config_service.get_cleide_config",
        lambda: _bi_cfg(),
    )
    monkeypatch.setattr(
        "app.services.cleide_audit_config_service.get_cleide_audit_config",
        lambda: _audit_cfg(audited_file_max_rows=10),
    )

    with web.app.test_request_context("/admin/agentes/cleide"):
        html = admin_routes.agentes_cleide.__wrapped__()

    assert 'id="cleideAuditAuditedFileMaxRows"' in html
    assert 'name="audited_file_max_rows"' in html
    field_html = html.split('id="cleideAuditAuditedFileMaxRows"')[1].split(">", 1)[0]
    assert 'value="10"' in field_html


def test_admin_agentes_cleide_get_audited_file_max_rows_default_2000(monkeypatch):
    html = _render_cleide_admin(monkeypatch)
    field_html = html.split('id="cleideAuditAuditedFileMaxRows"')[1].split(">", 1)[0]
    assert 'value="2000"' in field_html


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
            "audited_file_max_rows": "2000",
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
            "audited_file_max_rows": "2000",
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
            "audited_file_max_rows": "2000",
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
