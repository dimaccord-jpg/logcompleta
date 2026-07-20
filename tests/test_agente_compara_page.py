"""Contratos de página/UI do AgenteCompara (Compare Tabelas)."""
from __future__ import annotations

import importlib
import os
import pathlib
from types import SimpleNamespace


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


def _operational_client(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=True))
    monkeypatch.setattr(web, "get_julia_chat_max_history", lambda: 10)
    monkeypatch.setattr(
        web,
        "avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True},
    )
    return web.app.test_client()


def test_agente_compara_page_returns_200(monkeypatch):
    web = _load_web_module()
    resp = web.app.test_client().get("/agente-compara")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "agente_compara.js" in html
    assert "cleide_auditoria.js" not in html
    assert 'id="agenteComparaShell"' in html
    assert 'id="agenteComparaActionsMenu"' in html
    assert "/api/cleide-auditoria" not in html


def test_agente_compara_template_uses_own_js_and_ids():
    source = pathlib.Path("app/templates/agente_compara.html").read_text(encoding="utf-8")
    assert "agente_compara.js" in source
    assert "cleide_auditoria.js" not in source
    assert 'id="agenteComparaShell"' in source
    assert 'id="agenteComparaActionsMenu"' in source
    assert 'id="agenteComparaInput"' in source
    assert 'id="agenteComparaSend"' in source
    assert "/api/cleide-auditoria" not in source


def test_agente_compara_js_has_no_cleide_auditoria_api():
    js = pathlib.Path("app/static/js/agente_compara.js").read_text(encoding="utf-8")
    assert "/api/agente-compara/" in js
    assert "/api/cleide-auditoria" not in js
    assert "agenteComparaShell" in js or "agenteComparaActionsMenu" in js


def test_compare_tabelas_in_julia_plus_menu_on_operational_home(monkeypatch):
    client = _operational_client(monkeypatch)
    html = client.get("/").get_data(as_text=True)
    menu_start = html.index('id="juliaChatActionsMenu"')
    menu_chunk = html[menu_start : menu_start + 2800]
    assert "Compare Tabelas" in menu_chunk
    assert "/agente-compara" in menu_chunk


def test_compare_tabelas_in_cleide_auditoria_actions_menu(monkeypatch):
    web = _load_web_module()
    html = web.app.test_client().get("/auditoria-frete").get_data(as_text=True)
    menu_start = html.index('id="cleideAuditoriaActionsMenu"')
    menu_chunk = html[menu_start : menu_start + 1800]
    assert "Compare Tabelas" in menu_chunk
    assert "/agente-compara" in menu_chunk


def test_agente_compara_not_in_base_html_main_nav():
    base = pathlib.Path("app/templates/base.html").read_text(encoding="utf-8")
    assert "Compare Tabelas" not in base
    assert "agente-compara" not in base
    assert "agente_compara" not in base


def test_agente_compara_not_advertised_in_copilot_capabilities():
    caps = pathlib.Path("app/copilot_capabilities.md").read_text(encoding="utf-8")
    taxonomy = pathlib.Path("app/capability_taxonomy.py").read_text(encoding="utf-8")
    for token in ("agente-compara", "Compare Tabelas", "agente_compara"):
        assert token not in caps
        assert token not in taxonomy
