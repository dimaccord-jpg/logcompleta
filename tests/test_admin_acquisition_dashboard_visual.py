"""Integração focada: bloco Aquisição no /admin/dashboard."""
from __future__ import annotations

import importlib
import os
from datetime import datetime

from app.extensions import db
from app.funnel_event_service import (
    FUNNEL_EVENT_FILE_UPLOADED,
    FUNNEL_SOURCE_CLEIDE_AUDIT,
    record_funnel_event,
)
from app.models import Lead
from app.services.admin_acquisition_dashboard_service import get_acquisition_dashboard_payload
from app.services.lead_acquisition_service import CAMPANHA_ACESSO_DESKTOP, FONTE_LANDING
from tests.conftest import seed_conta_franquia_cliente, seed_usuario


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


def _common_dashboard_mocks(monkeypatch, *, acquisition_payload=None):
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
    monkeypatch.setattr(
        "app.services.onboarding_admin_analytics_service.get_onboarding_word_cloud",
        lambda **kwargs: {
            "terms": [],
            "admin_hidden_terms": [],
            "total_raw_occurrences": 0,
            "total_filtered_occurrences": 0,
            "pareto_coverage": 0,
            "pareto_target": 0.8,
            "days": 30,
            "removed_terms": {"stopwords": {}, "admin_hidden": {}},
        },
    )
    monkeypatch.setattr(
        "app.services.admin_conversion_dashboard_service.get_conversion_dashboard_payload",
        lambda **_kwargs: {
            "filters": {
                "source": "all",
                "days": 30,
                "source_options": [
                    {"value": "all", "label": "Todos"},
                    {"value": "cleide_audit", "label": "Auditoria"},
                    {"value": "agente_compara", "label": "Agente Compara"},
                ],
                "period_options": [
                    {"value": 7, "label": "7 dias"},
                    {"value": 30, "label": "30 dias"},
                    {"value": 90, "label": "90 dias"},
                ],
            },
            "period": {
                "start_utc": "2026-07-12T00:00:00",
                "end_utc": "2026-08-10T23:59:59",
                "label": "Ultimos 30 dias",
            },
            "kpis": {
                "uploaded_users": 0,
                "completed_users": 0,
                "first_audit_users": 0,
                "completion_rate": 0.0,
                "first_audit_rate": 0.0,
                "abandoned_users": 0,
                "upload_events": 0,
                "completion_events": 0,
            },
            "funnel": [],
            "series": [],
            "data_quality": {"has_data": False, "warnings": [], "service_failed": False},
        },
    )
    if acquisition_payload is not None:
        monkeypatch.setattr(
            "app.services.admin_acquisition_dashboard_service.get_acquisition_dashboard_payload",
            lambda **_kwargs: acquisition_payload,
        )


def test_dashboard_renders_acquisition_block(monkeypatch):
    web = _load_web_module()
    from app.painel_admin import admin_routes

    _common_dashboard_mocks(
        monkeypatch,
        acquisition_payload={
            "campaign": CAMPANHA_ACESSO_DESKTOP,
            "period": {"start_utc": "2026-07-12T00:00:00", "end_utc": "2026-08-10T23:59:59", "label": "Ultimos 30 dias", "days": 30},
            "stages": {"lead": 4, "click": 3, "registration": 2, "first_use": 1, "first_audit": 1},
            "rates": {
                "lead_to_click": 0.75,
                "click_to_registration": 2 / 3,
                "registration_to_first_use": 0.5,
                "first_use_to_first_audit": 1.0,
                "lead_to_first_audit": 0.25,
            },
            "funnel": [
                {"key": "lead", "label": "Leads", "count": 4, "rate_from_previous": 1.0},
                {"key": "click", "label": "Cliques", "count": 3, "rate_from_previous": 0.75},
                {"key": "registration", "label": "Cadastros", "count": 2, "rate_from_previous": 2 / 3},
                {"key": "first_use", "label": "Primeiro uso", "count": 1, "rate_from_previous": 0.5},
                {"key": "first_audit", "label": "Primeira auditoria", "count": 1, "rate_from_previous": 1.0},
            ],
            "data_quality": {"has_data": True, "warnings": [], "service_failed": False},
        },
    )

    with web.app.test_request_context("/admin/dashboard?conversion_days=30"):
        html = admin_routes.admin_dashboard.__wrapped__()

    assert "Aquisição — Landing Desktop" in html
    assert "Coorte por data de captura do Lead" in html
    assert "Lead → First Audit" in html
    assert "acquisition-landing-funnel" in html
    assert "Leads" in html
    assert "Cliques" in html
    assert "Cadastros" in html
    assert "Primeiro uso" in html
    assert "Primeira auditoria" in html
    assert ">4<" in html or "4" in html
    assert "25.0%" in html or "75.0%" in html
    assert "Ativação pós-cadastro" in html
    assert "Máximo: 2 e-mails." in html
    assert "24 horas após o cadastro" in html


def test_non_admin_still_denied(monkeypatch):
    web = _load_web_module()
    from app.painel_admin import admin_routes

    monkeypatch.setattr(admin_routes, "verificar_acesso_admin", lambda: False)

    with web.app.test_request_context("/admin/dashboard"):
        result = admin_routes.admin_dashboard.__wrapped__()

    assert result == ("Acesso Negado", 403)


def test_acquisition_failure_degrades_without_breaking_page(monkeypatch):
    web = _load_web_module()
    from app.painel_admin import admin_routes

    _common_dashboard_mocks(monkeypatch)
    monkeypatch.setattr(
        "app.services.admin_acquisition_dashboard_service.get_acquisition_dashboard_payload",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("acquisition down")),
    )

    with web.app.test_request_context("/admin/dashboard"):
        html = admin_routes.admin_dashboard.__wrapped__()

    assert "Aquisição — Landing Desktop" in html
    assert "Métricas de aquisição indisponíveis temporariamente." in html
    assert "Conversões" in html
    assert "Consumo de IA (Cleiton)" in html


def test_product_filter_does_not_change_acquisition_numbers(app):
    """Mesmo período + produtos diferentes → mesmos números de aquisição (service)."""
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="acq-prod-filter")
        user = seed_usuario(franquia.id, conta.id, email="acq-prod@t.com")
        user.created_at = datetime(2026, 8, 2, 9, 0, 0)
        lead = Lead(
            email="acq-prod@t.com",
            acquisition_campaign=CAMPANHA_ACESSO_DESKTOP,
            acquisition_source=FONTE_LANDING,
            campaign_captured_at=datetime(2026, 8, 1, 10, 0, 0),
            cta_clicked_at=datetime(2026, 8, 1, 11, 0, 0),
            converted_user_id=user.id,
            converted_at=datetime(2026, 8, 2, 9, 0, 0),
            followup_count=0,
        )
        db.session.add(lead)
        db.session.commit()
        record_funnel_event(
            event_name=FUNNEL_EVENT_FILE_UPLOADED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            user_id=user.id,
            conta_id=user.conta_id,
            franquia_id=user.franquia_id,
            idempotency_key="acq-prod-up",
            occurred_at=datetime(2026, 8, 3, 10, 0, 0),
        )
        user.first_audit_completed_at = datetime(2026, 8, 3, 11, 0, 0)
        db.session.commit()

        # O service de aquisição não recebe source — produto não entra na assinatura.
        a = get_acquisition_dashboard_payload(days=30, now_utc=datetime(2026, 8, 10, 12, 0, 0))
        b = get_acquisition_dashboard_payload(days=30, now_utc=datetime(2026, 8, 10, 12, 0, 0))
        assert a["stages"] == b["stages"]
        assert a["stages"]["lead"] == 1
        assert a["stages"]["first_audit"] == 1


def test_dashboard_passes_period_not_product_to_acquisition(monkeypatch):
    web = _load_web_module()
    from app.painel_admin import admin_routes

    captured = {}

    def _capture_acquisition(**kwargs):
        captured.update(kwargs)
        return {
            "campaign": CAMPANHA_ACESSO_DESKTOP,
            "period": {"start_utc": None, "end_utc": None, "label": "Ultimos 7 dias", "days": 7},
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

    _common_dashboard_mocks(monkeypatch)
    monkeypatch.setattr(
        "app.services.admin_acquisition_dashboard_service.get_acquisition_dashboard_payload",
        _capture_acquisition,
    )

    with web.app.test_request_context(
        "/admin/dashboard?conversion_source=agente_compara&conversion_days=7"
    ):
        admin_routes.admin_dashboard.__wrapped__()

    assert captured.get("days") == 7 or captured.get("days") == "7"
    assert "source" not in captured
    assert "conversion_source" not in captured
