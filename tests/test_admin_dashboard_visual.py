import importlib
import os
from types import SimpleNamespace


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


def _empty_acquisition_payload():
    return {
        "campaign": "desktop_access",
        "period": {"start_utc": None, "end_utc": None, "label": "Ultimos 30 dias", "days": 30},
        "stages": {"lead": 0, "click": 0, "registration": 0, "first_use": 0, "first_audit": 0},
        "rates": {
            "lead_to_click": 0.0,
            "click_to_registration": 0.0,
            "registration_to_first_use": 0.0,
            "first_use_to_first_audit": 0.0,
            "lead_to_first_audit": 0.0,
        },
        "funnel": [
            {"key": "lead", "label": "Leads", "count": 0, "rate_from_previous": 0.0},
            {"key": "click", "label": "Cliques", "count": 0, "rate_from_previous": 0.0},
            {"key": "registration", "label": "Cadastros", "count": 0, "rate_from_previous": 0.0},
            {"key": "first_use", "label": "Primeiro uso", "count": 0, "rate_from_previous": 0.0},
            {"key": "first_audit", "label": "Primeira auditoria", "count": 0, "rate_from_previous": 0.0},
        ],
        "data_quality": {"has_data": False, "warnings": [], "service_failed": False},
    }


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
    monkeypatch.setattr(
        "app.services.admin_acquisition_dashboard_service.get_acquisition_dashboard_payload",
        lambda **_kwargs: _empty_acquisition_payload(),
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


def test_dashboard_renderiza_bloco_de_conversoes_com_filtros_preservados(monkeypatch):
    web = _load_web_module()
    from app.painel_admin import admin_routes

    monkeypatch.setattr(admin_routes, "verificar_acesso_admin", lambda: True)
    monkeypatch.setattr(
        "app.services.admin_dashboard_service.get_dashboard_metrics",
        lambda **_kwargs: {"total_usuarios": 1, "total_usuarios_pagantes": 1, "total_leads": 1},
    )
    monkeypatch.setattr("app.services.admin_dashboard_service.list_categorias_distintas", lambda: [])
    monkeypatch.setattr("app.services.admin_dashboard_service.list_franquia_status_distintos", lambda: [])
    monkeypatch.setattr("app.services.agent_service.obter_kpis_insight", lambda: {"recomendacoes_pendentes": 0})
    monkeypatch.setattr("app.services.agent_service.obter_recomendacoes_recentes", lambda limite=15: [])
    monkeypatch.setattr("app.services.ia_metrics_service.get_ia_dashboard_payload", lambda _ano, _mes: {})
    monkeypatch.setattr("app.services.onboarding_admin_analytics_service.get_onboarding_word_cloud", lambda **kwargs: {"terms": [], "admin_hidden_terms": [], "total_raw_occurrences": 0, "total_filtered_occurrences": 0, "pareto_coverage": 0, "pareto_target": 0.8, "days": 30, "removed_terms": {"stopwords": {}, "admin_hidden": {}}})
    monkeypatch.setattr(
        "app.services.admin_conversion_dashboard_service.get_conversion_dashboard_payload",
        lambda **_kwargs: {
            "filters": {
                "source": "cleide_audit",
                "days": 90,
                "source_options": [{"value": "all", "label": "Todos"}, {"value": "cleide_audit", "label": "Auditoria"}, {"value": "agente_compara", "label": "Agente Compara"}],
                "period_options": [{"value": 7, "label": "7 dias"}, {"value": 30, "label": "30 dias"}, {"value": 90, "label": "90 dias"}],
            },
            "period": {"start_utc": "2026-05-09T00:00:00", "end_utc": "2026-08-06T23:59:59", "label": "Ultimos 90 dias"},
            "kpis": {"uploaded_users": 10, "completed_users": 7, "first_audit_users": 6, "completion_rate": 0.7, "first_audit_rate": 0.6, "abandoned_users": 3, "upload_events": 15, "completion_events": 8},
            "funnel": [{"key": "uploaded", "label": "Upload", "users": 10, "rate": 1.0, "dropoff_users": 3}, {"key": "completed", "label": "Conclusão", "users": 7, "rate": 0.7, "dropoff_users": 1}, {"key": "first_audit", "label": "Primeira auditoria", "users": 6, "rate": 0.6, "dropoff_users": 0}],
            "series": [{"date": "2026-08-06", "label": "06/08", "uploaded_users": 1, "completed_users": 1, "first_audit_users": 1}],
            "data_quality": {"has_data": True, "warnings": [], "service_failed": False},
        },
    )
    monkeypatch.setattr(
        "app.services.admin_acquisition_dashboard_service.get_acquisition_dashboard_payload",
        lambda **_kwargs: _empty_acquisition_payload(),
    )

    with web.app.test_request_context("/admin/dashboard?categoria=vip&franquia_status=ativa&cancelado=ativos&conversion_source=cleide_audit&conversion_days=90"):
        html = admin_routes.admin_dashboard.__wrapped__()

    assert "Conversões" in html
    assert "Aquisição — Landing Desktop" in html
    assert "Usuários com upload" in html
    assert "Usuários com conclusão" in html
    assert "Primeiras auditorias" in html
    assert "Taxa de conclusão" in html
    assert 'name="conversion_source"' in html
    assert 'name="conversion_days"' in html
    assert 'value="categoria"' not in html
    assert 'name="categoria" value="vip"' in html
    assert 'name="franquia_status" value="ativa"' in html
    assert 'name="cancelado" value="ativos"' in html
    assert 'value="cleide_audit" selected' in html
    assert 'value="90" selected' in html
    assert "conversionSeriesChart" in html
    assert "tojson" not in html
    assert "Chart" in html


def test_dashboard_conversion_failure_degrades_without_breaking_page(monkeypatch):
    web = _load_web_module()
    from app.painel_admin import admin_routes

    monkeypatch.setattr(admin_routes, "verificar_acesso_admin", lambda: True)
    monkeypatch.setattr(
        "app.services.admin_dashboard_service.get_dashboard_metrics",
        lambda **_kwargs: {"total_usuarios": 1, "total_usuarios_pagantes": 1, "total_leads": 1},
    )
    monkeypatch.setattr("app.services.admin_dashboard_service.list_categorias_distintas", lambda: [])
    monkeypatch.setattr("app.services.admin_dashboard_service.list_franquia_status_distintos", lambda: [])
    monkeypatch.setattr("app.services.agent_service.obter_kpis_insight", lambda: {"recomendacoes_pendentes": 0})
    monkeypatch.setattr("app.services.agent_service.obter_recomendacoes_recentes", lambda limite=15: [])
    monkeypatch.setattr("app.services.ia_metrics_service.get_ia_dashboard_payload", lambda _ano, _mes: {})
    monkeypatch.setattr("app.services.onboarding_admin_analytics_service.get_onboarding_word_cloud", lambda **kwargs: {"terms": [], "admin_hidden_terms": [], "total_raw_occurrences": 0, "total_filtered_occurrences": 0, "pareto_coverage": 0, "pareto_target": 0.8, "days": 30, "removed_terms": {"stopwords": {}, "admin_hidden": {}}})
    monkeypatch.setattr(
        "app.services.admin_conversion_dashboard_service.get_conversion_dashboard_payload",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("conversion down")),
    )
    monkeypatch.setattr(
        "app.services.admin_acquisition_dashboard_service.get_acquisition_dashboard_payload",
        lambda **_kwargs: _empty_acquisition_payload(),
    )

    with web.app.test_request_context("/admin/dashboard?conversion_source=agente_compara&conversion_days=7"):
        html = admin_routes.admin_dashboard.__wrapped__()

    assert "Conversões" in html
    assert "Métricas de conversão indisponíveis temporariamente." in html
    assert "Nenhum dado de conversão encontrado" in html
    assert "Aquisição — Landing Desktop" in html
    assert "Consumo de IA (Cleiton)" in html
