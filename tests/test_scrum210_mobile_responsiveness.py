"""SCRUM-210 — responsividade mobile da Home e do Login (estrutural)."""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
THEME_CSS = ROOT / "app" / "static" / "css" / "agentefrete-theme.css"
LOGIN_HTML = ROOT / "app" / "templates" / "login.html"
INDEX_HTML = ROOT / "app" / "templates" / "index.html"
BASE_HTML = ROOT / "app" / "templates" / "base.html"


def _css() -> str:
    return THEME_CSS.read_text(encoding="utf-8")


def _mobile_shell_block(css: str) -> str:
    marker = "@media (max-width: 767.98px) {\n  #sidebar {"
    start = css.index(marker)
    # bloco do shell mobile termina no fechamento deste media query
    depth = 0
    for i, ch in enumerate(css[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return css[start : i + 1]
    raise AssertionError("bloco mobile do shell não encontrado")


def test_shell_mobile_clears_sidebar_width_reservation_on_content_active():
    css = _css()
    mobile = _mobile_shell_block(css)

    assert "width: 100%" in mobile
    assert "margin-left: 0" in mobile
    assert "#content.active" in mobile
    # desktop preservado fora do MQ
    assert "width: calc(100% - var(--sidebar-collapsed))" in css
    assert "margin-left: var(--sidebar-collapsed)" in css
    assert "--sidebar-width: 260px" in css
    assert "--sidebar-collapsed: 80px" in css


def test_shell_mobile_breakpoint_aligns_with_bootstrap_md():
    css = _css()
    assert "@media (max-width: 767.98px) {\n  #sidebar {" in css
    # não deve restar o MQ antigo que deixava #content.active com width desktop
    assert re.search(
        r"@media\s*\(max-width:\s*768px\)\s*\{[^}]*#content\.active",
        css,
        re.DOTALL,
    ) is None


def test_home_keeps_mobile_skill_stack_and_full_width_shell_contract():
    css = _css()
    index = INDEX_HTML.read_text(encoding="utf-8")
    base = BASE_HTML.read_text(encoding="utf-8")

    assert 'id="content" class="active"' in base
    assert "home-skills-grid" in index
    assert "grid-template-columns: 1fr" in css
    assert "@media (max-width: 767.98px)" in css
    # home não introduz container estreito próprio
    assert 'style="max-width:' not in index


def test_login_mobile_layout_uses_full_column_and_stackable_actions():
    login = LOGIN_HTML.read_text(encoding="utf-8")
    css = _css()
    mobile = _mobile_shell_block(css)

    assert "col-12 col-md-7" in login
    assert "p-3 p-md-5" in login
    assert "af-login-actions" in login
    assert "af-login-card" in login
    assert "d-none d-md-flex" in login  # sidebar ilustrativa some no mobile
    assert ".af-login-actions" in mobile
    assert "flex-direction: column" in mobile
    assert "width: 100%" in mobile
    assert "Entrar no Sistema" in login
