"""SCRUM-211: login nas habilidades da Home e proteção de GET /fretes."""
from __future__ import annotations

import importlib
import os
from types import SimpleNamespace

from flask_login import UserMixin

from app.shell_navigation import build_home_skills


def _load_web():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


class _AuthUser(UserMixin):
    def __init__(self):
        self.id = "scrum211-user"
        self.is_admin = False
        self.conta_id = 1
        self.franquia_id = 1
        self.email = "scrum211@example.com"
        self.full_name = "SCRUM211 User"
        self.categoria = "free"
        self.franquia = None


def _force_login(client, web, monkeypatch):
    user = _AuthUser()
    monkeypatch.setattr(web, "load_user_for_flask_login", lambda _user_id: user)
    with client.session_transaction() as sess:
        sess["_user_id"] = user.get_id()
        sess["_fresh"] = True
    return user


def test_guest_get_fretes_redirects_to_login_with_safe_next(monkeypatch):
    web = _load_web()
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=False))
    client = web.app.test_client()
    resp = client.get("/fretes", follow_redirects=False)
    assert resp.status_code in (302, 303)
    location = resp.headers.get("Location") or ""
    assert "/login" in location
    assert "next=/fretes" in location or "next=%2Ffretes" in location
    assert resp.status_code != 200


def test_authenticated_get_fretes_remains_accessible(monkeypatch):
    web = _load_web()
    monkeypatch.setattr(web, "_load_indices_payload", lambda: {"origens": [], "destinos": []})
    monkeypatch.setattr(
        web,
        "avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True, "modo_operacao": "normal"},
    )
    client = web.app.test_client()
    _force_login(client, web, monkeypatch)
    resp = client.get("/fretes")
    assert resp.status_code == 200
    assert "Análise de Fretes com Inteligência Artificial" in resp.get_data(as_text=True)


def test_home_guest_skills_all_go_through_login():
    web = _load_web()
    with web.app.test_request_context("/"):
        skills = build_home_skills(
            authenticated=False,
            url_for=web.url_for,
            has_endpoint=lambda name: name in web.app.view_functions,
        )
    by_id = {item["id"]: item for item in skills}
    assert by_id["analisar_fretes"]["href"] == "/login?next=/fretes"
    assert by_id["auditar_cobrancas"]["href"] == "/login?next=/auditoria-frete"
    assert by_id["comparar_tabelas"]["href"] == "/login?next=/agente-compara"


def test_home_authenticated_skills_stay_direct():
    web = _load_web()
    with web.app.test_request_context("/"):
        skills = build_home_skills(
            authenticated=True,
            url_for=web.url_for,
            has_endpoint=lambda name: name in web.app.view_functions,
        )
    by_id = {item["id"]: item for item in skills}
    assert by_id["analisar_fretes"]["href"] == "/fretes"
    assert by_id["auditar_cobrancas"]["href"] == "/auditoria-frete"
    assert by_id["comparar_tabelas"]["href"] == "/agente-compara"


def test_home_discovery_remains_public_for_guest(monkeypatch):
    web = _load_web()
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=False))
    monkeypatch.setattr(web, "get_julia_chat_max_history", lambda: 10)
    monkeypatch.setattr(web, "avaliar_autorizacao_operacao_por_franquia", lambda _u: {"permitido": True})
    resp = web.app.test_client().get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'id="copilotWelcomeMessage"' in html
    assert "window.ONBOARDING_DISCOVERY_API = '/api/onboarding_discovery'" in html
    assert 'data-home-skill="analisar_fretes"' in html
    assert 'href="/login?next=/fretes"' in html


def test_auditoria_e_comparacao_guest_page_behavior_preserved(monkeypatch):
    """Auditoria/Comparação: Home exige login; páginas GET seguem o contrato atual."""
    web = _load_web()
    monkeypatch.setattr(web, "current_user", SimpleNamespace(is_authenticated=False))
    client = web.app.test_client()

    auditoria = client.get("/auditoria-frete")
    assert auditoria.status_code == 200
    assert "cleide_auditoria.js" in auditoria.get_data(as_text=True)

    compara = client.get("/agente-compara")
    assert compara.status_code == 200
    assert "agente_compara.js" in compara.get_data(as_text=True) or "AgenteCompara" in compara.get_data(
        as_text=True
    )


def test_sitemap_nao_anuncia_fretes_como_publico(monkeypatch):
    web = _load_web()

    class _FakeQuery:
        def filter(self, *_a, **_k):
            return self

        def order_by(self, *_a, **_k):
            return self

        def all(self):
            return []

    class _FakeField:
        def isnot(self, _value):
            return self

        def in_(self, _values):
            return self

        def desc(self):
            return self

    monkeypatch.setattr(
        web,
        "NoticiaPortal",
        SimpleNamespace(
            query=_FakeQuery(),
            publicado_em=_FakeField(),
            status_publicacao=_FakeField(),
        ),
    )
    resp = web.app.test_client().get("/sitemap.xml")
    assert resp.status_code == 200
    xml = resp.get_data(as_text=True)
    assert "/fretes" not in xml
    assert "/feed" in xml
