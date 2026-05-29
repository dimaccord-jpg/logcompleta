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
            "total_tokens_month": 1800,
            "operational_tokens_month": 1800,
            "onboarding_tokens_month": 4200,
            "total_internal_tokens_month": 6000,
            "tokens_by_api_key": {},
            "cleide_processing": {},
            "onboarding_discovery_ia": {
                "total_tokens_month": 4200,
                "event_count_month": 3,
                "event_count_with_metrics_month": 1,
                "event_count_without_metrics_month": 2,
                "failure_event_count_month": 1,
                "tokens_by_api_key": {"GEMINI_API_KEY": 4200},
            },
        },
    )
    monkeypatch.setattr(
        "app.services.onboarding_admin_analytics_service.get_onboarding_word_cloud",
        lambda **kwargs: {
            "terms": [{"term": "frete", "count": 5}, {"term": "custo", "count": 2}],
            "admin_hidden_terms": [{"id": 1, "term": "ola"}],
            "total_raw_occurrences": 10,
            "total_filtered_occurrences": 7,
            "pareto_coverage": 1.0,
            "pareto_target": 0.80,
            "days": 30,
            "removed_terms": {"stopwords": {}, "admin_hidden": {}},
        },
    )

    with web.app.test_request_context("/admin/dashboard"):
        html = admin_routes.admin_dashboard.__wrapped__()

    assert isinstance(html, str)
    assert "Insight Estratégico (Customer Insight)" not in html
    assert "Recomendações recentes" not in html
    assert "Consumo de IA (Cleiton)" in html
    assert "Visao consolidada do consumo interno de IA" in html
    assert "Tokens operacionais no mes" in html
    assert "Tokens onboarding no mes" in html
    assert "Total interno de IA no mes" in html
    assert "Consumo IA - Onboarding" in html
    assert "Conta como consumo interno de IA, mas nao abate franquia do cliente." in html
    assert "Analise de termos do onboarding" in html
    assert "Exibindo termos relevantes apos filtros e ocultacoes do admin." in html
    assert "frete" in html
    assert "Ocultar" in html
    assert "Termos ocultos" in html
    assert "Reexibir" in html
    assert "ola" in html
    assert "4200" in html.replace(",", "")
    assert "6000" in html.replace(",", "")
    assert "2 sem metrica" in html
    assert "1 falha(s)" in html


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
