"""Contrato da navegação principal do shell (desktop e mobile)."""
from __future__ import annotations

import importlib
import os
import re
from types import SimpleNamespace

from flask_login import UserMixin

from app.capability_taxonomy import DESTINATIONS
from app.shell_navigation import SHELL_NAV_CATALOG, build_home_skills, build_shell_navigation

VISIBLE_LABELS = (
    "Início",
    "Habilidades",
    "Consultar o AgenteFrete",
    "Analisar fretes",
    "Auditar cobranças de frete",
    "Comparar tabelas",
    "Feed",
)

BANNED_NAV_LABELS = (
    "Roberto",
    "Cleide",
    "Júlia",
    "Julia",
    "Cleiton",
    "AgenteAudita",
    "AgenteCompara",
    "Agente Compara",
)


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


def _region(html: str, marker: str) -> str:
    start = html.index(marker)
    if "desktop" in marker:
        end = html.index('class="sidebar-footer"', start)
    else:
        end = html.index("<hr", start)
    return html[start:end]


def _labels(region: str) -> list[str]:
    return re.findall(r'class="menu-text">([^<]+)', region) + re.findall(
        r'class="nav-section-label menu-text"[^>]*>([^<]+)',
        region,
    )


def test_catalog_uses_real_destinations_and_keeps_internal_ids():
    by_id = {entry.id: entry for entry in SHELL_NAV_CATALOG}
    assert by_id["consultar_agentefrete"].destination_id == "julia_operational"
    assert by_id["analisar_fretes"].destination_id == "roberto_bi"
    assert by_id["auditar_cobrancas"].destination_id == "cleide_freight_audit"
    assert by_id["comparar_tabelas"].destination_id == "agente_compara"
    assert by_id["feed"].destination_id == "feed"
    assert DESTINATIONS["julia_operational"].url == "/chat_julia?mode=operational"
    assert DESTINATIONS["roberto_bi"].url == "/fretes"
    assert DESTINATIONS["cleide_freight_audit"].url == "/auditoria-frete"
    assert DESTINATIONS["agente_compara"].url == "/agente-compara"
    assert DESTINATIONS["feed"].url == "/feed"
    assert DESTINATIONS["cleide_audit"].url == "/cleide-bi-frete"
    assert "cleide_audit" not in {entry.destination_id for entry in SHELL_NAV_CATALOG}
    assert by_id["analisar_fretes"].requires_login is None
    assert DESTINATIONS["roberto_bi"].requires_login is True


def test_guest_and_authenticated_hrefs_follow_existing_login_rules():
    web = _load_web_module()

    def _nav(*, authenticated: bool, path: str = "/"):
        with web.app.test_request_context(path):
            return build_shell_navigation(
                authenticated=authenticated,
                request_path=path,
                url_for=web.url_for,
                has_endpoint=lambda name: name in web.app.view_functions,
            )

    guest = _nav(authenticated=False)
    auth = _nav(authenticated=True, path="/feed")
    guest_hrefs = {
        item["id"]: item["href"]
        for section in guest["sections"]
        for item in section["links"]
    }
    auth_hrefs = {
        item["id"]: item["href"]
        for section in auth["sections"]
        for item in section["links"]
    }

    assert guest_hrefs["inicio"] == "/"
    assert guest_hrefs["analisar_fretes"] == "/login?next=/fretes"
    assert guest_hrefs["feed"] == "/feed"
    assert guest_hrefs["auditar_cobrancas"] == "/login?next=/auditoria-frete"
    assert guest_hrefs["comparar_tabelas"] == "/login?next=/agente-compara"
    assert guest_hrefs["consultar_agentefrete"] == "/login?next=%2Fchat_julia%3Fmode%3Doperational"

    assert auth_hrefs["consultar_agentefrete"] == "/chat_julia?mode=operational"
    assert auth_hrefs["analisar_fretes"] == "/fretes"
    assert auth_hrefs["auditar_cobrancas"] == "/auditoria-frete"
    assert auth_hrefs["comparar_tabelas"] == "/agente-compara"
    assert auth_hrefs["feed"] == "/feed"
    assert "/acesso-desktop" not in auth_hrefs.values()
    assert "/cleide-bi-frete" not in auth_hrefs.values()

    feed_active = [
        item["id"]
        for section in auth["sections"]
        for item in section["links"]
        if item["active"]
    ]
    assert feed_active == ["feed"]


def test_home_skills_follow_catalog_order_and_shell_login_rules():
    web = _load_web_module()

    def _skills(*, authenticated: bool):
        with web.app.test_request_context("/"):
            return build_home_skills(
                authenticated=authenticated,
                url_for=web.url_for,
                has_endpoint=lambda name: name in web.app.view_functions,
            )

    guest = _skills(authenticated=False)
    auth = _skills(authenticated=True)
    assert [item["id"] for item in guest] == [
        "analisar_fretes",
        "auditar_cobrancas",
        "comparar_tabelas",
    ]
    assert [item["label"] for item in guest] == [
        "Analisar fretes",
        "Auditar cobranças de frete",
        "Comparar tabelas",
    ]
    assert guest[0]["href"] == "/login?next=/fretes"
    assert guest[0]["requires_login"] is True
    assert guest[1]["href"] == "/login?next=/auditoria-frete"
    assert guest[1]["requires_login"] is True
    assert guest[2]["href"] == "/login?next=/agente-compara"
    assert guest[2]["requires_login"] is True
    assert auth[0]["href"] == "/fretes"
    assert auth[1]["href"] == "/auditoria-frete"
    assert auth[2]["href"] == "/agente-compara"
    assert "consultar_agentefrete" not in {item["id"] for item in guest}
    assert "feed" not in {item["id"] for item in guest}


def test_home_shell_repeats_the_same_destinations_on_desktop_and_mobile(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=False))
    monkeypatch.setattr(web, "get_julia_chat_max_history", lambda: 10)
    monkeypatch.setattr(web, "avaliar_autorizacao_operacao_por_franquia", lambda _u: {"permitido": True})
    html = web.app.test_client().get("/").get_data(as_text=True)
    desktop = _region(html, 'data-shell-nav="desktop"')
    mobile = _region(html, 'data-shell-nav="mobile"')

    for label in VISIBLE_LABELS:
        assert label in desktop
        assert label in mobile

    for banned in BANNED_NAV_LABELS:
        assert banned not in _labels(desktop)
        assert banned not in _labels(mobile)

    assert 'href="/login?next=/auditoria-frete"' in desktop
    assert 'href="/login?next=/auditoria-frete"' in mobile
    assert 'href="/login?next=/agente-compara"' in desktop
    assert 'href="/login?next=/agente-compara"' in mobile
    assert 'href="/login?next=/fretes"' in desktop
    assert 'href="/login?next=/fretes"' in mobile
    assert 'href="/login?next=%2Fchat_julia%3Fmode%3Doperational"' in desktop
    assert 'href="/login?next=%2Fchat_julia%3Fmode%3Doperational"' in mobile
    assert 'href="/fretes"' not in desktop
    assert 'href="/fretes"' not in mobile
    assert 'href="/feed"' in desktop
    assert 'href="/feed"' in mobile
    assert "/acesso-desktop" not in desktop
    assert "/acesso-desktop" not in mobile
    assert "/cleide-bi-frete" not in desktop
    assert 'aria-label="Expandir menu lateral"' in html
    assert 'aria-label="Abrir menu de navegação"' in html
    assert 'aria-label="Fechar menu"' in html
    from pathlib import Path

    css = Path("app/static/css/agentefrete-theme.css").read_text(encoding="utf-8")
    start = css.index("/* Mobile: mesmos destinos")
    end = css.index("/* 4. AREA DE CONTEUDO */", start)
    assert "display: none" not in css[start:end]


class _AuthUser(UserMixin):
    def __init__(self):
        self.id = "shell-user"
        self.is_admin = False
        self.conta_id = 1
        self.franquia_id = 1
        self.email = "shell@example.com"
        self.full_name = "Shell User"
        self.categoria = "free"
        self.franquia = None


def test_authenticated_shell_keeps_direct_skill_links_and_profile(monkeypatch):
    web = _load_web_module()
    user = _AuthUser()
    monkeypatch.setattr(web, "load_user_for_flask_login", lambda _user_id: user)
    monkeypatch.setattr(web, "get_julia_chat_max_history", lambda: 10)
    monkeypatch.setattr(web, "avaliar_autorizacao_operacao_por_franquia", lambda _u: {"permitido": True})
    client = web.app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = user.get_id()
        sess["_fresh"] = True
    html = client.get("/").get_data(as_text=True)
    desktop = _region(html, 'data-shell-nav="desktop"')
    mobile = _region(html, 'data-shell-nav="mobile"')
    assert 'href="/chat_julia?mode=operational"' in desktop
    assert 'href="/chat_julia?mode=operational"' in mobile
    assert 'href="/fretes"' in desktop
    assert 'href="/fretes"' in mobile
    assert 'href="/auditoria-frete"' in desktop
    assert 'href="/agente-compara"' in mobile
    assert 'href="/login?next=/fretes"' not in desktop
    assert 'href="/login?next=/auditoria-frete"' not in desktop
    assert 'href="/login?next=/agente-compara"' not in mobile
    assert 'href="/perfil"' in html
    assert "Área do Usuário" in html
    assert 'aria-label="Área do usuário"' in html
