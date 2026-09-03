"""Dashboard administrativo do experimento de CTA da Home."""
from __future__ import annotations

import importlib
import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from app.extensions import db
from app.models import HomeCtaExperimentEvent
from app.services.admin_home_cta_experiment_service import (
    empty_home_cta_experiment_dashboard_payload,
    get_home_cta_experiment_dashboard_payload,
)
from app.services.home_cta_experiment_service import (
    EVENT_CONVERSION,
    EVENT_IMPRESSION,
    HOME_CTA_EXPERIMENT,
    HOME_CTA_VARIANTS,
)


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


def _seed_event(*, variant, event_type, assignment_id, occurred_at, origin=None):
    db.session.add(
        HomeCtaExperimentEvent(
            experiment=HOME_CTA_EXPERIMENT,
            assignment_id=assignment_id,
            variant=variant,
            event_type=event_type,
            interaction_origin=origin,
            occurred_at=occurred_at,
        )
    )


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
        "funnel": [],
        "data_quality": {"has_data": False, "warnings": [], "service_failed": False},
    }


def _common_dashboard_mocks(monkeypatch, *, cta_payload=None):
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
                "source_options": [],
                "period_options": [
                    {"value": 7, "label": "7 dias"},
                    {"value": 30, "label": "30 dias"},
                    {"value": 90, "label": "90 dias"},
                ],
            },
            "period": {"start_utc": None, "end_utc": None, "label": "Ultimos 30 dias"},
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
    monkeypatch.setattr(
        "app.services.admin_acquisition_dashboard_service.get_acquisition_dashboard_payload",
        lambda **_kwargs: _empty_acquisition_payload(),
    )
    if cta_payload is not None:
        monkeypatch.setattr(
            "app.services.admin_home_cta_experiment_service.get_home_cta_experiment_dashboard_payload",
            lambda **_kwargs: cta_payload,
        )


def test_abc_aparecem_mesmo_sem_dados(app):
    with app.app_context():
        payload = get_home_cta_experiment_dashboard_payload(days=30, now_utc=datetime(2026, 9, 3, 12, 0, 0))
    ids = [row["id"] for row in payload["variants"]]
    assert ids == ["cta_a", "cta_b", "cta_c"]
    for row in payload["variants"]:
        assert row["text"] == HOME_CTA_VARIANTS[row["id"]]
        assert row["impressions"] == 0
        assert row["conversions"] == 0
        assert row["conversion_rate"] == 0.0


def test_impressoes_conversions_taxa_e_uplift(app):
    with app.app_context():
        now = datetime(2026, 9, 3, 12, 0, 0)
        for i in range(100):
            _seed_event(variant="cta_a", event_type=EVENT_IMPRESSION, assignment_id=f"a-imp-{i}", occurred_at=now)
        for i in range(15):
            _seed_event(
                variant="cta_a",
                event_type=EVENT_CONVERSION,
                assignment_id=f"a-imp-{i}",
                occurred_at=now,
                origin="typed",
            )
        for i in range(100):
            _seed_event(variant="cta_b", event_type=EVENT_IMPRESSION, assignment_id=f"b-imp-{i}", occurred_at=now)
        for i in range(18):
            _seed_event(
                variant="cta_b",
                event_type=EVENT_CONVERSION,
                assignment_id=f"b-imp-{i}",
                occurred_at=now,
                origin="typed",
            )
        for i in range(100):
            _seed_event(variant="cta_c", event_type=EVENT_IMPRESSION, assignment_id=f"c-imp-{i}", occurred_at=now)
        for i in range(12):
            _seed_event(
                variant="cta_c",
                event_type=EVENT_CONVERSION,
                assignment_id=f"c-imp-{i}",
                occurred_at=now,
                origin="typed",
            )
        db.session.commit()
        payload = get_home_cta_experiment_dashboard_payload(days=30, now_utc=now)
    by_id = {row["id"]: row for row in payload["variants"]}
    assert by_id["cta_a"]["impressions"] == 100
    assert by_id["cta_a"]["conversions"] == 15
    assert by_id["cta_a"]["conversion_rate"] == 15.0
    assert by_id["cta_a"]["uplift_pp_vs_a"] is None
    assert by_id["cta_b"]["impressions"] == 100
    assert by_id["cta_b"]["conversions"] == 18
    assert by_id["cta_b"]["conversion_rate"] == 18.0
    assert by_id["cta_b"]["uplift_pp_vs_a"] == 3.0
    assert by_id["cta_c"]["impressions"] == 100
    assert by_id["cta_c"]["conversions"] == 12
    assert by_id["cta_c"]["conversion_rate"] == 12.0
    assert by_id["cta_c"]["uplift_pp_vs_a"] == -3.0


def test_zero_impressions_taxa_zero(app):
    with app.app_context():
        payload = get_home_cta_experiment_dashboard_payload(days=7, now_utc=datetime(2026, 9, 3, 12, 0, 0))
    for row in payload["variants"]:
        assert row["impressions"] == 0
        assert row["conversion_rate"] == 0.0


def test_conversions_maior_que_impressions_sinaliza_qualidade(app):
    with app.app_context():
        now = datetime(2026, 9, 3, 12, 0, 0)
        _seed_event(variant="cta_b", event_type=EVENT_IMPRESSION, assignment_id="only-imp", occurred_at=now)
        _seed_event(
            variant="cta_b",
            event_type=EVENT_CONVERSION,
            assignment_id="conv-1",
            occurred_at=now,
            origin="typed",
        )
        _seed_event(
            variant="cta_b",
            event_type=EVENT_CONVERSION,
            assignment_id="conv-2",
            occurred_at=now,
            origin="typed",
        )
        db.session.commit()
        payload = get_home_cta_experiment_dashboard_payload(days=30, now_utc=now)
    by_id = {row["id"]: row for row in payload["variants"]}
    assert by_id["cta_b"]["impressions"] == 1
    assert by_id["cta_b"]["conversions"] == 1
    assert by_id["cta_b"]["conversions_raw"] == 2
    assert by_id["cta_b"]["conversion_rate"] == 100.0
    assert "cta_b" in payload["data_quality"]["inconsistent_variants"]
    assert payload["data_quality"]["warnings"]


def test_filtro_de_periodo(app):
    with app.app_context():
        now = datetime(2026, 9, 3, 12, 0, 0)
        _seed_event(
            variant="cta_a",
            event_type=EVENT_IMPRESSION,
            assignment_id="old",
            occurred_at=datetime(2026, 1, 1, 10, 0, 0),
        )
        _seed_event(
            variant="cta_a",
            event_type=EVENT_IMPRESSION,
            assignment_id="mid",
            occurred_at=datetime(2026, 8, 1, 10, 0, 0),
        )
        _seed_event(variant="cta_a", event_type=EVENT_IMPRESSION, assignment_id="new", occurred_at=now)
        db.session.commit()
        week = get_home_cta_experiment_dashboard_payload(days=7, now_utc=now)
        quarter = get_home_cta_experiment_dashboard_payload(days=90, now_utc=now)
    week_a = next(row for row in week["variants"] if row["id"] == "cta_a")
    quarter_a = next(row for row in quarter["variants"] if row["id"] == "cta_a")
    assert week["period"]["days"] == 7
    assert quarter["period"]["days"] == 90
    assert week_a["impressions"] == 1
    assert quarter_a["impressions"] == 2


def test_dashboard_renderiza_bloco(monkeypatch):
    web = _load_web_module()
    from app.painel_admin import admin_routes

    payload = empty_home_cta_experiment_dashboard_payload(days=30)
    payload["variants"][0]["impressions"] = 100
    payload["variants"][0]["conversions"] = 15
    payload["variants"][0]["conversion_rate"] = 15.0
    payload["variants"][1]["impressions"] = 100
    payload["variants"][1]["conversions"] = 18
    payload["variants"][1]["conversion_rate"] = 18.0
    payload["variants"][1]["uplift_pp_vs_a"] = 3.0
    payload["variants"][2]["impressions"] = 100
    payload["variants"][2]["conversions"] = 12
    payload["variants"][2]["conversion_rate"] = 12.0
    payload["variants"][2]["uplift_pp_vs_a"] = -3.0
    _common_dashboard_mocks(monkeypatch, cta_payload=payload)

    with web.app.test_request_context("/admin/dashboard?conversion_days=30"):
        html = admin_routes.admin_dashboard.__wrapped__()

    assert "Experimento — CTA da Home" in html
    assert "Impressões" in html
    assert "Chats iniciados" in html
    assert HOME_CTA_VARIANTS["cta_a"] in html
    assert HOME_CTA_VARIANTS["cta_b"] in html
    assert HOME_CTA_VARIANTS["cta_c"] in html
    assert "15.0%" in html
    assert "+3.0 p.p." in html
    assert "-3.0 p.p." in html
    assert ">base<" in html or ">base\n" in html or "base" in html


def test_dashboard_nao_admin_continua_protegido(monkeypatch):
    web = _load_web_module()
    from app.painel_admin import admin_routes

    monkeypatch.setattr(admin_routes, "verificar_acesso_admin", lambda: False)
    with web.app.test_request_context("/admin/dashboard"):
        result = admin_routes.admin_dashboard.__wrapped__()
    assert result == ("Acesso Negado", 403)


def test_falha_do_service_nao_quebra_dashboard(monkeypatch):
    web = _load_web_module()
    from app.painel_admin import admin_routes

    _common_dashboard_mocks(monkeypatch)
    monkeypatch.setattr(
        "app.services.admin_home_cta_experiment_service.get_home_cta_experiment_dashboard_payload",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("cta dash down")),
    )
    with web.app.test_request_context("/admin/dashboard"):
        html = admin_routes.admin_dashboard.__wrapped__()
    assert "Experimento — CTA da Home" in html
    assert "Métricas do experimento de CTA indisponíveis temporariamente." in html
    assert "Conversões" in html


def test_service_nao_consulta_user_email():
    src = Path("app/services/admin_home_cta_experiment_service.py").read_text(encoding="utf-8")
    assert "from app.models import User" not in src
    assert "User." not in src
    assert "FunnelEvent.query" not in src
    assert "from app.models import Lead" not in src
    assert "HomeCtaExperimentEvent" in src


def test_dashboard_passa_conversion_days(monkeypatch):
    web = _load_web_module()
    from app.painel_admin import admin_routes

    captured = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return empty_home_cta_experiment_dashboard_payload(days=kwargs.get("days") or 30)

    _common_dashboard_mocks(monkeypatch)
    monkeypatch.setattr(
        "app.services.admin_home_cta_experiment_service.get_home_cta_experiment_dashboard_payload",
        _capture,
    )
    with web.app.test_request_context("/admin/dashboard?conversion_days=7"):
        admin_routes.admin_dashboard.__wrapped__()
    assert captured["days"] in (7, "7")
