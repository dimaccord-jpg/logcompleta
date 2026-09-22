"""Fundação visual do AgenteFrete: tokens compartilhados e alternância de tema."""
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


def test_shell_templates_keep_single_theme_contract():
    base = BASE_HTML.read_text(encoding="utf-8")
    header = HEADER_HTML.read_text(encoding="utf-8")
    chat = CHAT_HTML.read_text(encoding="utf-8")
    desktop = DESKTOP_HTML.read_text(encoding="utf-8")

    assert 'data-theme="dark"' in base
    assert 'data-theme-preference="dark"' in base
    assert "af-theme-init" in base
    assert 'data-af-theme-select' in base
    assert "if (!window.AFTheme) return;" in base
    assert "--laranja-log" not in base
    assert "--ri-primary" not in header
    assert "--af-primary" in header
    assert "app_header_title|default('AgenteFrete')" in header
    assert "Copilot do AgenteFrete" not in chat
    assert 'data-copilot-surface="true"' in chat
    assert "Assistente virtual especializado em logística" in chat
    assert "agentefrete-theme.css" not in desktop
    assert 'data-af-theme-enabled="{%- block af_theme_enabled -%}false{%- endblock -%}"' in base
    index = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")
    auditoria = (ROOT / "app" / "templates" / "cleide_auditoria.html").read_text(encoding="utf-8")
    compara = (ROOT / "app" / "templates" / "agente_compara.html").read_text(encoding="utf-8")
    assert "{% block af_theme_enabled %}true{% endblock %}" in index
    assert "af_theme_enabled" not in auditoria
    assert "af_theme_enabled" not in compara


def test_theme_script_fallbacks_and_system_preference():
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

function boot(storedValue, throwOnRead, matchLight, matchMediaMissing, themeEnabled) {
  var attrs = {};
  var writes = 0;
  var kept = storedValue;
  attrs["data-af-theme-enabled"] = themeEnabled === false ? "false" : "true";
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
        return kept;
      },
      setItem: function (key, value) { writes += 1; kept = String(value); }
    },
    matchMedia: matchMediaMissing ? undefined : function (query) {
      return { matches: matchLight && String(query).indexOf("light") !== -1 };
    }
  };
  var api = new Function("window", "document", source + "\nreturn window.AFTheme;")(window, document);
  return { attrs: attrs, meta: meta, api: api, writes: writes, kept: kept };
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
assert(result.meta.content === "#f4f7f9", "theme-color do tema claro");

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

result = boot("light", false, true, false, false);
assert(result.attrs["data-theme"] === "dark", "superficie sem opt-in permanece dark");
assert(result.writes === 0, "preferencia salva nao pode ser apagada");
assert(result.kept === "light", "light salvo permanece no storage");

result = boot("system", false, true, false, false);
assert(result.attrs["data-theme"] === "dark", "sistema claro nao aplica light sem opt-in");
assert(result.writes === 0, "preferencia sistema permanece salva");
assert(result.kept === "system", "valor sistema permanece no storage");
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
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    web = importlib.import_module("app.web")
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=False))
    monkeypatch.setattr(web, "get_julia_chat_max_history", lambda: 10)
    monkeypatch.setattr(web, "avaliar_autorizacao_operacao_por_franquia", lambda _u: {"permitido": True})
    response = web.app.test_client().get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'data-af-theme-enabled="true"' in html
    assert 'data-theme="dark"' in html
    assert 'data-af-theme-select' in html
    assert "Copilot do AgenteFrete" not in html
    assert "Assistente virtual especializado em logística" in html
    assert "Pergunte sobre sua operação ou escolha uma habilidade para começar." in html
    assert '<span class="af-text-gradient">AgenteFrete</span>' in html
    assert "agentefrete-theme.css" in html
