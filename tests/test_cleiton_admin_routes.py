import importlib
import os
from html.parser import HTMLParser
from types import SimpleNamespace


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


def _doc_cfg(**overrides):
    base = {
        "upload_enabled": True,
        "max_files_per_session": 5,
        "session_max_bytes": 15 * 1024 * 1024,
        "upload_ttl_hours": 48,
        "cleanup_enabled": True,
        "prompt_context_max_chars": 24000,
        "prompt_max_files_considered": 3,
        "pdf_enabled": True,
        "pdf_max_bytes": 5 * 1024 * 1024,
        "pdf_max_pages": 50,
        "pdf_max_chars": 120000,
        "excel_enabled": True,
        "excel_max_bytes": 5 * 1024 * 1024,
        "excel_max_rows": 5000,
        "excel_max_columns": 80,
        "excel_max_chars": 120000,
        "docx_enabled": True,
        "docx_max_bytes": 5 * 1024 * 1024,
        "docx_max_paragraphs": 5000,
        "docx_max_chars": 120000,
        "txt_enabled": True,
        "txt_max_bytes": 1 * 1024 * 1024,
        "txt_max_chars": 120000,
        "xml_enabled": True,
        "xml_max_bytes": 2 * 1024 * 1024,
        "xml_max_nodes": 20000,
        "xml_max_depth": 20,
        "xml_max_chars": 120000,
        "csv_enabled": True,
        "csv_max_bytes": 2 * 1024 * 1024,
        "csv_max_rows": 10000,
        "csv_max_columns": 80,
        "csv_max_chars": 120000,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _RenderedFormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self._in_target_form = False
        self.inputs = []
        self.buttons = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form" and attrs.get("action") == "/admin/agentes/cleiton":
            self._in_target_form = True
            return
        if not self._in_target_form:
            return
        if tag == "input":
            self.inputs.append(attrs)
        elif tag == "button":
            self.buttons.append(attrs)

    def handle_endtag(self, tag):
        if tag == "form" and self._in_target_form:
            self._in_target_form = False


def _payload_from_rendered_form(html: str) -> dict[str, str]:
    parser = _RenderedFormParser()
    parser.feed(html)
    payload = {}
    for attrs in parser.inputs:
        name = attrs.get("name")
        if not name:
            continue
        input_type = (attrs.get("type") or "text").lower()
        if input_type == "checkbox":
            if "checked" in attrs:
                payload[name] = attrs.get("value", "on")
            continue
        payload[name] = attrs.get("value", "")
    return payload


def test_admin_agentes_cleiton_get_carrega_bloco_documental(monkeypatch):
    web = _load_web_module()
    from app.painel_admin import admin_routes

    monkeypatch.setattr(
        admin_routes,
        "current_user",
        SimpleNamespace(is_authenticated=True, is_admin=True, email="admin@example.com"),
    )
    monkeypatch.setattr(admin_routes, "verificar_acesso_admin", lambda: True)
    monkeypatch.setattr(
        "app.services.cleiton_cost_service.get_or_create_config",
        lambda: SimpleNamespace(
            runtime_monthly_cost=450.0,
            month_seconds=2592000,
            allocation_percent=1.0,
            overhead_factor=1.0,
            cost_per_million_tokens=None,
            credit_tokens_per_credit=1000.0,
            credit_lines_per_credit=500.0,
            credit_ms_per_credit=60000.0,
            updated_at=None,
        ),
    )
    monkeypatch.setattr("app.services.cleiton_cost_service.compute_cost_per_second", lambda cfg: 0.001)
    monkeypatch.setattr("app.services.cleiton_doc_config_service.get_cleiton_doc_config", lambda: _doc_cfg())

    with web.app.test_request_context("/admin/agentes/cleiton"):
        html = admin_routes.agentes_cleiton.__wrapped__()

    assert isinstance(html, str)
    assert "Cleiton - Parâmetros operacionais" in html
    assert "referência de <strong>tokens</strong>" in html
    assert "régua de conversão de créditos" in html
    assert "upload documental da Júlia" in html
    assert "Bloco E - Upload documental governado" in html
    assert "Configurações que controlam o upload documental usado pela Júlia." in html
    assert "Atenção: estes limites controlam segurança, custo e experiência." in html
    assert "Ativar upload documental" in html
    assert "Ativar limpeza automática" in html
    assert "Máximo de documentos por sessão" in html
    assert "Tempo de validade dos documentos (horas)" in html
    assert "Tamanho máximo total da sessão (bytes)" in html
    assert "Limite de contexto enviado à IA (caracteres)" in html
    assert "Máximo de documentos considerados por resposta" in html
    assert "Permitir PDF" in html
    assert "Permitir Excel/XLSX" in html
    assert "Permitir DOCX/Word" in html
    assert "Permitir TXT" in html
    assert "Permitir XML" in html
    assert "Permitir CSV" in html
    assert 'name="upload_enabled"' in html
    assert 'name="cleanup_enabled"' in html
    assert 'name="max_files_per_session"' in html
    assert 'name="prompt_context_max_chars"' in html
    assert 'id="cleiton_doc_upload_enabled"' in html
    assert "Chave: <code>cleiton_doc_upload_enabled</code>" in html
    assert "será ativada em etapa posterior" not in html
    assert "Gemini Files API" in html
    assert "sem parser local pesado" in html
    for broken in ("Ã", "â€”", "Ãº", "Ãª", "Ã©", "Ã§", "Ã£"):
        assert broken not in html


def test_admin_agentes_cleiton_post_erro_documental_mensagem_clara(monkeypatch):
    web = _load_web_module()
    from app.painel_admin import admin_routes

    monkeypatch.setattr(
        admin_routes,
        "current_user",
        SimpleNamespace(is_authenticated=True, is_admin=True, email="admin@example.com"),
    )
    monkeypatch.setattr(admin_routes, "verificar_acesso_admin", lambda: True)

    def _raise_doc(*, cost_kwargs, doc_campos):
        raise ValueError(
            "Configuração inválida: prompt_max_files_considered não pode ser maior que max_files_per_session."
        )

    monkeypatch.setattr(
        "app.services.cleiton_doc_config_service.salvar_agentes_cleiton_config",
        _raise_doc,
    )

    with web.app.test_request_context(
        "/admin/agentes/cleiton",
        method="POST",
        data={
            "cleiton_doc_form": "1",
            "runtime_monthly_cost": "999",
            "month_seconds": "2592000",
            "allocation_percent": "1.0",
            "overhead_factor": "1.0",
            "max_files_per_session": "2",
            "prompt_max_files_considered": "3",
            "session_max_bytes": str(15 * 1024 * 1024),
            "upload_ttl_hours": "48",
            "prompt_context_max_chars": "24000",
            "pdf_max_bytes": str(5 * 1024 * 1024),
            "pdf_max_pages": "50",
            "pdf_max_chars": "120000",
            "excel_max_bytes": str(5 * 1024 * 1024),
            "excel_max_rows": "5000",
            "excel_max_columns": "80",
            "excel_max_chars": "120000",
            "docx_max_bytes": str(5 * 1024 * 1024),
            "docx_max_paragraphs": "5000",
            "docx_max_chars": "120000",
            "txt_max_bytes": str(1 * 1024 * 1024),
            "txt_max_chars": "120000",
            "xml_max_bytes": str(2 * 1024 * 1024),
            "xml_max_nodes": "20000",
            "xml_max_depth": "20",
            "xml_max_chars": "120000",
            "csv_max_bytes": str(2 * 1024 * 1024),
            "csv_max_rows": "10000",
            "csv_max_columns": "80",
            "csv_max_chars": "120000",
        },
    ):
        admin_routes.agentes_cleiton.__wrapped__()
        from flask import get_flashed_messages

        msgs = get_flashed_messages(with_categories=True)

    assert any(cat == "danger" and "Configuração documental não salva" in msg for cat, msg in msgs)
    assert any("prompt_max_files_considered" in msg for _, msg in msgs)


def test_admin_agentes_cleiton_post_salva_campos_documentais_sem_quebrar_cost(monkeypatch):
    web = _load_web_module()
    from app.painel_admin import admin_routes

    monkeypatch.setattr(
        admin_routes,
        "current_user",
        SimpleNamespace(is_authenticated=True, is_admin=True, email="admin@example.com"),
    )
    monkeypatch.setattr(admin_routes, "verificar_acesso_admin", lambda: True)
    calls = {"unified": False}

    def _save_unified(*, cost_kwargs, doc_campos):
        calls["unified"] = True
        assert cost_kwargs["month_seconds"] == 2592000
        assert cost_kwargs["credit_tokens_per_credit"] == 1000.0
        assert doc_campos is not None
        assert doc_campos["max_files_per_session"] == "5"
        assert doc_campos["upload_ttl_hours"] == "48"
        assert doc_campos["pdf_max_pages"] == "50"
        assert doc_campos["csv_max_columns"] == "80"

    monkeypatch.setattr(
        "app.services.cleiton_doc_config_service.salvar_agentes_cleiton_config",
        _save_unified,
    )

    with web.app.test_request_context(
        "/admin/agentes/cleiton",
        method="POST",
        data={
            "cleiton_doc_form": "1",
            "runtime_monthly_cost": "450",
            "month_seconds": "2592000",
            "allocation_percent": "1.0",
            "overhead_factor": "1.0",
            "cost_per_million_tokens": "",
            "credit_tokens_per_credit": "1000",
            "credit_lines_per_credit": "500",
            "credit_ms_per_credit": "60000",
            "upload_enabled": "on",
            "max_files_per_session": "5",
            "session_max_bytes": str(15 * 1024 * 1024),
            "upload_ttl_hours": "48",
            "cleanup_enabled": "on",
            "prompt_context_max_chars": "24000",
            "prompt_max_files_considered": "3",
            "pdf_enabled": "on",
            "pdf_max_bytes": str(5 * 1024 * 1024),
            "pdf_max_pages": "50",
            "pdf_max_chars": "120000",
            "excel_enabled": "on",
            "excel_max_bytes": str(5 * 1024 * 1024),
            "excel_max_rows": "5000",
            "excel_max_columns": "80",
            "excel_max_chars": "120000",
            "docx_enabled": "on",
            "docx_max_bytes": str(5 * 1024 * 1024),
            "docx_max_paragraphs": "5000",
            "docx_max_chars": "120000",
            "txt_enabled": "on",
            "txt_max_bytes": str(1 * 1024 * 1024),
            "txt_max_chars": "120000",
            "xml_enabled": "on",
            "xml_max_bytes": str(2 * 1024 * 1024),
            "xml_max_nodes": "20000",
            "xml_max_depth": "20",
            "xml_max_chars": "120000",
            "csv_enabled": "on",
            "csv_max_bytes": str(2 * 1024 * 1024),
            "csv_max_rows": "10000",
            "csv_max_columns": "80",
            "csv_max_chars": "120000",
        },
    ):
        resp = admin_routes.agentes_cleiton.__wrapped__()

    assert resp.status_code == 302
    assert "/admin/agentes/cleiton" in (resp.location or "")
    assert calls["unified"] is True


def test_admin_agentes_cleiton_post_antigo_continua_funcionando(monkeypatch):
    web = _load_web_module()
    from app.painel_admin import admin_routes

    monkeypatch.setattr(
        admin_routes,
        "current_user",
        SimpleNamespace(is_authenticated=True, is_admin=True, email="admin@example.com"),
    )
    monkeypatch.setattr(admin_routes, "verificar_acesso_admin", lambda: True)
    calls = {"unified": False}

    def _save_unified(*, cost_kwargs, doc_campos):
        calls["unified"] = True
        assert doc_campos is None

    monkeypatch.setattr(
        "app.services.cleiton_doc_config_service.salvar_agentes_cleiton_config",
        _save_unified,
    )
    monkeypatch.setattr("app.services.cleiton_doc_config_service.get_cleiton_doc_config", lambda: _doc_cfg())

    with web.app.test_request_context(
        "/admin/agentes/cleiton",
        method="POST",
        data={
            "runtime_monthly_cost": "450",
            "month_seconds": "2592000",
            "allocation_percent": "1.0",
            "overhead_factor": "1.0",
            "credit_tokens_per_credit": "1000",
            "credit_lines_per_credit": "500",
            "credit_ms_per_credit": "60000",
        },
    ):
        resp = admin_routes.agentes_cleiton.__wrapped__()

    assert resp.status_code == 302
    assert "/admin/agentes/cleiton" in (resp.location or "")
    assert calls["unified"] is True


def test_admin_agentes_cleiton_post_payload_completo_extraido_do_get(monkeypatch):
    web = _load_web_module()
    from app.painel_admin import admin_routes

    monkeypatch.setattr(
        admin_routes,
        "current_user",
        SimpleNamespace(is_authenticated=True, is_admin=True, email="admin@example.com"),
    )
    monkeypatch.setattr(admin_routes, "verificar_acesso_admin", lambda: True)
    monkeypatch.setattr(
        "app.services.cleiton_cost_service.get_or_create_config",
        lambda: SimpleNamespace(
            runtime_monthly_cost=450.0,
            month_seconds=2592000,
            allocation_percent=1.0,
            overhead_factor=1.0,
            cost_per_million_tokens=None,
            credit_tokens_per_credit=1000.0,
            credit_lines_per_credit=500.0,
            credit_ms_per_credit=60000.0,
            updated_at=None,
        ),
    )
    monkeypatch.setattr("app.services.cleiton_cost_service.compute_cost_per_second", lambda cfg: 0.001)
    monkeypatch.setattr("app.services.cleiton_doc_config_service.get_cleiton_doc_config", lambda: _doc_cfg())

    with web.app.test_request_context("/admin/agentes/cleiton"):
        html = admin_routes.agentes_cleiton.__wrapped__()

    payload = _payload_from_rendered_form(html)
    assert payload["cleiton_doc_form"] == "1"
    assert payload["upload_enabled"] == "on"
    assert payload["max_files_per_session"] == "5"

    payload["max_files_per_session"] = "4"
    payload.pop("upload_enabled", None)

    calls = {"doc_campos": None}

    def _save_unified(*, cost_kwargs, doc_campos):
        calls["doc_campos"] = doc_campos
        assert cost_kwargs["runtime_monthly_cost"] == 450.0
        assert doc_campos is not None
        assert doc_campos["max_files_per_session"] == "4"
        assert doc_campos["upload_enabled"] is None

    monkeypatch.setattr(
        "app.services.cleiton_doc_config_service.salvar_agentes_cleiton_config",
        _save_unified,
    )

    with web.app.test_request_context(
        "/admin/agentes/cleiton",
        method="POST",
        data=payload,
    ):
        resp = admin_routes.agentes_cleiton.__wrapped__()

    assert resp.status_code == 302
    assert "/admin/agentes/cleiton" in (resp.location or "")
    assert calls["doc_campos"] is not None


def test_admin_agentes_cleiton_get_upload_desmarcado_quando_config_false(monkeypatch):
    web = _load_web_module()
    from app.painel_admin import admin_routes

    monkeypatch.setattr(
        admin_routes,
        "current_user",
        SimpleNamespace(is_authenticated=True, is_admin=True, email="admin@example.com"),
    )
    monkeypatch.setattr(admin_routes, "verificar_acesso_admin", lambda: True)
    monkeypatch.setattr(
        "app.services.cleiton_cost_service.get_or_create_config",
        lambda: SimpleNamespace(
            runtime_monthly_cost=450.0,
            month_seconds=2592000,
            allocation_percent=1.0,
            overhead_factor=1.0,
            cost_per_million_tokens=None,
            credit_tokens_per_credit=1000.0,
            credit_lines_per_credit=500.0,
            credit_ms_per_credit=60000.0,
            updated_at=None,
        ),
    )
    monkeypatch.setattr("app.services.cleiton_cost_service.compute_cost_per_second", lambda cfg: 0.001)
    monkeypatch.setattr(
        "app.services.cleiton_doc_config_service.get_cleiton_doc_config",
        lambda: _doc_cfg(upload_enabled=False, max_files_per_session=4),
    )

    with web.app.test_request_context("/admin/agentes/cleiton"):
        html = admin_routes.agentes_cleiton.__wrapped__()

    parser = _RenderedFormParser()
    parser.feed(html)
    upload_attrs = [attrs for attrs in parser.inputs if attrs.get("name") == "upload_enabled"]
    assert upload_attrs
    assert "checked" not in upload_attrs[0]
    assert 'name="max_files_per_session"' in html
    assert 'value="4"' in html
