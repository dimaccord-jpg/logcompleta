"""Fundação visual do AgenteFrete: tokens compartilhados e tema global no shell."""
from __future__ import annotations

import importlib
import os
import pathlib
import shutil
import subprocess
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]
THEME_CSS = ROOT / "app" / "static" / "css" / "agentefrete-theme.css"
BASE_HTML = ROOT / "app" / "templates" / "base.html"
HEADER_HTML = ROOT / "app" / "templates" / "partials" / "app_global_header.html"
CHAT_HTML = ROOT / "app" / "templates" / "chat_julia.html"
DESKTOP_HTML = ROOT / "app" / "templates" / "acesso_desktop.html"

SEMANTIC_TOKENS = (
    "--af-bg",
    "--af-surface",
    "--af-surface-elevated",
    "--af-text",
    "--af-text-muted",
    "--af-border",
    "--af-primary",
    "--af-primary-hover",
    "--af-primary-text",
    "--af-success",
    "--af-warning",
    "--af-danger",
    "--af-info",
    "--af-radius-sm",
    "--af-radius-md",
    "--af-radius-lg",
    "--af-shadow-sm",
    "--af-shadow-md",
    "--af-transition",
)

SHELL_TEMPLATES = (
    "index.html",
    "login.html",
    "julia_chat_operational.html",
    "user_area.html",
    "contrate_plano.html",
    "fretes.html",
    "cleide_auditoria.html",
    "agente_compara.html",
    "feed.html",
    "noticia_interna.html",
    "request_reset.html",
    "reset_password.html",
    "complete_profile.html",
    "regularizar_pagamento.html",
    "feature_under_construction.html",
    "gestao_multiuser.html",
)


def _css() -> str:
    return THEME_CSS.read_text(encoding="utf-8")


def _slice(css: str, start_marker: str, end_marker: str) -> str:
    start = css.index(start_marker)
    end = css.index(end_marker, start + len(start_marker))
    return css[start:end]


def test_design_system_has_one_token_source_for_dark_and_light():
    css = _css()
    dark = _slice(css, ":root,", "html[data-theme=\"light\"]")
    light = _slice(css, "html[data-theme=\"light\"]", "/* 2. TIPOGRAFIA */")

    for token in SEMANTIC_TOKENS:
        assert f"{token}:" in dark, token
    for token in (
        "--af-bg:",
        "--af-surface:",
        "--af-surface-elevated:",
        "--af-text:",
        "--af-text-muted:",
        "--af-border:",
        "--af-primary:",
        "--af-primary-hover:",
        "--af-success:",
        "--af-warning:",
        "--af-danger:",
        "--af-info:",
    ):
        assert token in light

    assert "--af-bg-deep: var(--af-bg);" in dark
    assert "--af-cyan: var(--af-primary);" in dark
    assert "--sidebar-bg: var(--af-chrome-bg);" in dark
    assert "--ri-primary" not in css
    assert "--laranja-log" not in css


def test_shell_templates_keep_single_global_theme_contract():
    base = BASE_HTML.read_text(encoding="utf-8")
    header = HEADER_HTML.read_text(encoding="utf-8")
    chat = CHAT_HTML.read_text(encoding="utf-8")
    desktop = DESKTOP_HTML.read_text(encoding="utf-8")

    assert 'data-theme="dark"' in base
    assert 'data-theme-preference="dark"' in base
    assert "af-theme-init" in base
    assert "partials/af_theme_control.html" in base
    assert "if (!window.AFTheme) return;" in base
    assert "--laranja-log" not in base
    assert "--ri-primary" not in header
    assert "--af-primary" in header
    assert "app_header_title|default('AgenteFrete')" in header
    assert "Copilot do AgenteFrete" not in chat
    assert 'data-copilot-surface="true"' in chat
    assert "Assistente virtual especializado em logística" in chat
    assert "agentefrete-theme.css" not in desktop

    # Contrato novo: tema global — sem opt-in / sem gate data-af-theme-enabled.
    assert "data-af-theme-enabled" not in base
    assert "af_theme_enabled" not in base
    assert "themeEnabled" not in base
    assert 'STORAGE_KEY = "af-theme"' in base

    for name in SHELL_TEMPLATES:
        html = (ROOT / "app" / "templates" / name).read_text(encoding="utf-8")
        assert "af_theme_enabled" not in html, name
        assert '{% extends "base.html" %}' in html, name

    sidebar = base[base.index('id="sidebar"'):base.index('id="content"')]
    mobile = base[base.index('id="mobileMenu"'):base.index('id="sidebar"')]
    footer = base[base.index("<footer"):base.index("</footer>")]
    assert "af_theme_variant = 'sidebar'" in sidebar
    assert "af_theme_variant = 'mobile'" in mobile
    assert "af_theme_control.html" not in footer
    assert "data-af-theme-select" not in footer
    # Controle sempre presente no shell (não depende de opt-in).
    assert "af_theme_control.html" in sidebar
    assert "af_theme_control.html" in mobile

    control = (ROOT / "app" / "templates" / "partials" / "af_theme_control.html").read_text(encoding="utf-8")
    assert 'data-af-theme-select' in control
    assert 'value="system"' in control
    assert 'value="dark"' in control
    assert 'value="light"' in control
    assert 'title="Usar preferência do sistema"' in control
    assert 'html[data-theme="light"] .julia-chat-actions-menu' in chat
    assert 'html[data-theme="light"] .julia-chat-attach-icon' in chat

    login_css = _css()
    assert "#001428" not in login_css
    assert "rgba(255, 255, 255, 0.8)" not in login_css
    assert ".af-login-sidebar" in login_css
    assert "var(--af-chrome-bg)" in login_css
    perfil_css = (ROOT / "app" / "static" / "css" / "user_area_perfil.css").read_text(encoding="utf-8")
    assert 'html[data-theme="light"] .af-profile-page #modalEncerrarContrato .btn-close' in perfil_css
    assert "filter: none;" in perfil_css


def test_light_shell_reuses_semantic_tokens_and_dark_chrome_stays():
    css = _css()
    dark = _slice(css, ":root,", 'html[data-theme="light"]')
    light = _slice(css, 'html[data-theme="light"]', "/* 2. TIPOGRAFIA */")

    assert "--af-chrome-bg: #050c16;" in dark
    assert "--af-chrome-hover: #112240;" in dark
    assert "--af-chrome-text: #ffffff;" in dark
    assert "--af-chrome-muted: #a8b2d1;" in dark
    assert "background: rgba(0, 0, 0, 0.4) !important;" in css
    assert "background: rgba(0, 0, 0, 0.3) !important;" in css
    assert "#content" in css and "background: var(--af-bg-deep)" in css
    assert ".af-shell-footer" in css and "background: var(--af-chrome-bg);" in css
    assert "#sidebar" in css and "background: var(--af-chrome-bg) !important;" in css

    assert "--af-chrome-bg: var(--af-surface);" in light
    assert "--af-chrome-hover: var(--af-surface-muted);" in light
    assert "--af-chrome-text: var(--af-text);" in light
    assert "--af-chrome-muted: var(--af-text-secondary);" in light
    assert "--af-chrome-accent: var(--af-primary);" in light
    assert "--af-chrome-border: var(--af-border);" in light
    assert "#050c16" not in light
    assert "#112240" not in light

    assert 'html[data-theme="light"] .sidebar-header' in css
    assert 'html[data-theme="light"] .sidebar-footer' in css
    assert 'html[data-theme="light"] #sidebarCollapse' in css
    assert 'html[data-theme="light"] #mobileMenu .btn-close-white' in css
    assert 'html[data-theme="light"] #mobileMenu .nav-item:hover' in css
    assert 'html[data-theme="light"] #sidebar .nav-item.active' in css


def test_light_chrome_hover_and_privacy_button_use_semantic_tokens():
    css = _css()
    privacy = (ROOT / "app" / "static" / "css" / "privacy_consent.css").read_text(encoding="utf-8")
    hover = _slice(
        css,
        'html[data-theme="light"] #sidebarCollapse:hover',
        'html[data-theme="light"] .mobile-navbar.navbar-dark',
    )
    assert "color: var(--af-chrome-text)" in hover
    assert "background: var(--af-chrome-hover)" in hover
    assert "border-color: var(--af-chrome-accent)" in hover
    assert "--af-primary-text" not in hover
    assert "#fff" not in hover
    assert "#sidebarCollapse:hover { background: var(--af-chrome-accent) !important; border-color: var(--af-chrome-accent) !important; }" in css

    footer_btn = _slice(privacy, ".af-privacy-footer-btn {", ".af-privacy-banner,")
    assert "color: var(--af-chrome-muted);" in footer_btn
    assert "color: var(--af-chrome-text);" in footer_btn
    # Dark: base permanece transparente (sem regressão).
    assert "background: transparent;" in footer_btn
    assert "border: 0;" in footer_btn
    # Light: superfície e borda semânticas distinguíveis do footer.
    assert 'html[data-theme="light"] .af-privacy-footer-btn' in footer_btn
    assert "background: var(--af-surface-elevated);" in footer_btn
    assert "border: 1px solid var(--af-chrome-border);" in footer_btn
    assert "background: var(--af-chrome-hover);" in footer_btn
    assert "border-color: var(--af-chrome-border);" in footer_btn
    assert "outline: 2px solid var(--af-chrome-accent);" in footer_btn
    assert "#a8b2d1" not in footer_btn
    assert "#f0f0f5" not in footer_btn
    assert "#0a0a0f" in privacy


def _shell_regions(html: str) -> tuple[str, str, str]:
    sidebar = html[html.index('id="sidebar"'):html.index('id="content"')]
    mobile = html[html.index('id="mobileMenu"'):html.index('id="sidebar"')]
    footer = html[html.index("af-shell-footer"):html.index("</footer>")]
    return sidebar, mobile, footer


def _client(monkeypatch):
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    web = importlib.import_module("app.web")
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=False))
    monkeypatch.setattr(web, "get_julia_chat_max_history", lambda: 10)
    monkeypatch.setattr(web, "avaliar_autorizacao_operacao_por_franquia", lambda _u: {"permitido": True})
    return web.app.test_client()


def test_shell_pages_expose_theme_control_globally(monkeypatch):
    client = _client(monkeypatch)

    # Rotas públicas/acessíveis sem auth no test client.
    for path in ("/", "/login", "/chat_julia?mode=operational", "/feed"):
        response = client.get(path)
        assert response.status_code == 200, path
        html = response.get_data(as_text=True)
        assert "data-af-theme-enabled" not in html, path
        assert 'STORAGE_KEY = "af-theme"' in html, path
        sidebar, mobile, footer = _shell_regions(html)
        assert 'id="af-theme-select-sidebar"' in sidebar, path
        assert 'id="af-theme-select-mobile"' in mobile, path
        assert 'data-af-theme-select' not in footer, path
        assert html.count('id="af-theme-select-') == 2, path

    # Fretes/Perfil/Planos herdam base.html (contrato estático + seletor no shell).
    for name in ("fretes.html", "user_area.html", "contrate_plano.html", "cleide_auditoria.html", "agente_compara.html"):
        html = (ROOT / "app" / "templates" / name).read_text(encoding="utf-8")
        assert '{% extends "base.html" %}' in html, name
        assert "af_theme_enabled" not in html, name


def test_theme_script_applies_preference_globally_without_opt_in():
    cscript = shutil.which("cscript")
    assert cscript, "cscript é necessário para executar o script de tema isolado."
    harness = r"""
var fso = new ActiveXObject("Scripting.FileSystemObject");
var htmlFile = fso.OpenTextFile("app\\templates\\base.html", 1);
var html = htmlFile.ReadAll();
htmlFile.Close();
var start = html.indexOf("/* af-theme-init");
var end = html.indexOf("})(window);", start);
if (start < 0 || end < 0) {
  WScript.Echo("marcadores do script de tema ausentes");
  WScript.Quit(2);
}
var source = html.substring(start, end + "})(window);".length);
if (source.indexOf("themeEnabled") >= 0 || source.indexOf("data-af-theme-enabled") >= 0) {
  WScript.Echo("contrato antigo de opt-in ainda presente no script");
  WScript.Quit(3);
}

function boot(storedValue, throwOnRead, matchLight, matchMediaMissing) {
  var attrs = {};
  var state = { writes: 0, kept: storedValue };
  var meta = { content: "#0A0A0F" };
  var document = {
    documentElement: {
      setAttribute: function (name, value) { attrs[name] = value; },
      getAttribute: function (name) { return attrs[name]; }
    },
    querySelector: function (sel) {
      if (sel === 'meta[name="theme-color"]') {
        return {
          setAttribute: function (name, value) {
            if (name === "content") meta.content = value;
          }
        };
      }
      return null;
    },
    querySelectorAll: function () { return []; }
  };
  var window = {
    localStorage: {
      getItem: function () {
        if (throwOnRead) throw new Error("denied");
        return state.kept;
      },
      setItem: function (key, value) { state.writes += 1; state.kept = String(value); }
    },
    matchMedia: matchMediaMissing ? undefined : function (query) {
      return { matches: matchLight && String(query).indexOf("light") !== -1 };
    }
  };
  var api = new Function("window", "document", source + "\nreturn window.AFTheme;")(window, document);
  return { attrs: attrs, meta: meta, api: api, state: state };
}

function assert(cond, msg) {
  if (!cond) {
    WScript.Echo(msg);
    WScript.Quit(1);
  }
}

var result = boot(null, false, false, false);
assert(result.attrs["data-theme"] === "dark", "localStorage vazio deve cair em dark");
assert(result.attrs["data-theme-preference"] === "dark", "preferencia vazia deve ser dark");

result = boot("nope", false, false, false);
assert(result.attrs["data-theme"] === "dark", "preferencia invalida deve cair em dark");
assert(result.attrs["data-theme-preference"] === "dark", "preferencia invalida nao pode ser aplicada");

result = boot("light", false, false, false);
assert(result.attrs["data-theme"] === "light", "light explicito");
assert(result.attrs["data-theme-preference"] === "light", "preferencia light preservada");
assert(result.meta.content === "#f4f7f9", "theme-color do tema claro");

result = boot("dark", false, true, false);
assert(result.attrs["data-theme"] === "dark", "dark explicito");
assert(result.attrs["data-theme-preference"] === "dark", "preferencia dark preservada");

result = boot("system", false, true, false);
assert(result.attrs["data-theme"] === "light", "sistema com prefers-color-scheme light");
assert(result.attrs["data-theme-preference"] === "system", "preferencia sistema preservada");

result = boot("system", false, false, false);
assert(result.attrs["data-theme"] === "dark", "sistema com esquema escuro");
assert(result.attrs["data-theme-preference"] === "system", "preferencia sistema no esquema escuro");

result = boot("system", false, true, true);
assert(result.attrs["data-theme"] === "dark", "sistema sem matchMedia deve cair em dark");

result = boot(null, true, false, false);
assert(result.attrs["data-theme"] === "dark", "falha de localStorage deve cair em dark");

result = boot(null, false, false, false);
var applied = result.api.applyTheme("garbage", false);
assert(applied.theme === "dark" && applied.preference === "dark", "applyTheme invalido");

result = boot("light", false, true, false);
assert(result.attrs["data-theme"] === "light", "light aplica sem opt-in");
assert(result.state.writes === 0, "boot sem persist nao grava");
assert(result.state.kept === "light", "light salvo permanece no storage");

result = boot("system", false, true, false);
assert(result.attrs["data-theme"] === "light", "sistema claro aplica light sem opt-in");
assert(result.state.writes === 0, "preferencia sistema permanece salva");
assert(result.state.kept === "system", "valor sistema permanece no storage");

result = boot("light", false, false, false);
var persisted = result.api.applyTheme("system", true);
assert(persisted.preference === "system", "persist guarda system");
assert(result.state.kept === "system", "localStorage atualizado para system");
assert(result.state.writes === 1, "persist grava uma vez");
WScript.Echo("ok");
"""
    harness_path = ROOT / "tests" / "_af_theme_harness.js"
    harness_path.write_text(harness, encoding="utf-8")
    try:
        completed = subprocess.run(
            [cscript, "//Nologo", str(harness_path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        harness_path.unlink(missing_ok=True)
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_home_shell_still_renders_with_theme_control(monkeypatch):
    response = _client(monkeypatch).get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "data-af-theme-enabled" not in html
    assert 'data-theme="dark"' in html
    assert 'data-af-theme-select' in html
    assert "Copilot do AgenteFrete" not in html
    assert "Assistente virtual especializado em logística" in html
    assert "Pergunte sobre sua operação ou escolha uma habilidade para começar." in html
    assert '<span class="af-text-gradient">AgenteFrete</span>' in html
    assert "agentefrete-theme.css" in html
