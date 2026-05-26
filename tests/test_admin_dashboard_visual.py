import importlib
import os
from types import SimpleNamespace


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


def test_dashboard_renderiza_sem_bloco_de_insight(monkeypatch):
    web = _load_web_module()
    from app.painel_admin import admin_routes

    monkeypatch.setattr(admin_routes, "verificar_acesso_admin", lambda: True)
    monkeypatch.setattr(
        "app.services.admin_dashboard_service.get_dashboard_metrics",
        lambda **_kwargs: {"total_usuarios": 1, "total_usuarios_pagantes": 1, "total_leads": 1},
    )
    monkeypatch.setattr("app.services.admin_dashboard_service.list_categorias_distintas", lambda: [])
    monkeypatch.setattr("app.services.admin_dashboard_service.list_franquia_status_distintos", lambda: [])
    monkeypatch.setattr("app.services.agent_service.obter_kpis_insight", lambda: {"recomendacoes_pendentes": 3})
    monkeypatch.setattr("app.services.agent_service.obter_recomendacoes_recentes", lambda limite=15: [])
    monkeypatch.setattr(
        "app.services.ia_metrics_service.get_ia_dashboard_payload",
        lambda _ano, _mes: {
            "month_competence": "2026-05",
            "total_tokens_month": 0,
            "tokens_by_api_key": {},
            "cleide_processing": {},
        },
    )

    with web.app.test_request_context("/admin/dashboard"):
        html = admin_routes.admin_dashboard.__wrapped__()

    assert isinstance(html, str)
    assert "Insight Estratégico (Customer Insight)" not in html
    assert "Recomendações recentes" not in html
    assert "Consumo de IA (Cleiton)" in html


def test_admin_pautas_permanece_acessivel(monkeypatch):
    web = _load_web_module()
    from app.painel_admin import admin_routes

    monkeypatch.setattr(admin_routes, "verificar_acesso_admin", lambda: True)
    monkeypatch.setattr(
        admin_routes,
        "current_user",
        SimpleNamespace(is_authenticated=True, is_admin=True, email="admin@example.com"),
    )
    monkeypatch.setattr(
        "app.services.pauta_service.listar_pautas",
        lambda **_kwargs: ([], ["aprovado"]),
    )

    with web.app.test_request_context("/admin/pautas"):
        html = admin_routes.pautas_admin.__wrapped__()

    assert isinstance(html, str)
    assert "Pautas editoriais" in html
