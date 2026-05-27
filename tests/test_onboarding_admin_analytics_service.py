"""Testes do serviço de analytics administrativo do onboarding discovery."""
from __future__ import annotations

import json
from datetime import timedelta

from app.models import AuditoriaGerencial, utcnow_naive
from app.services.onboarding_admin_analytics_service import get_onboarding_word_cloud


def _seed_discovery_event(contexto: dict, *, days_ago: int = 1) -> None:
    row = AuditoriaGerencial(
        tipo_decisao="onboarding_discovery",
        decisao="confidence=high; next_action=handoff",
        contexto_json=json.dumps(contexto, ensure_ascii=False),
        resultado="sucesso",
        created_at=utcnow_naive() - timedelta(days=days_ago),
    )
    from app.extensions import db

    db.session.add(row)
    db.session.commit()


def test_get_onboarding_word_cloud_aggregates_terms(app):
    with app.app_context():
        _seed_discovery_event({"user_terms_normalized": ["frete", "custo"]})
        _seed_discovery_event({"user_terms_normalized": ["frete", "bi"]})
        _seed_discovery_event({"user_terms_normalized": ["frete"]})

        result = get_onboarding_word_cloud(limit=10, days=30)

    assert result[0] == {"term": "frete", "count": 3}
    assert {"term": "custo", "count": 1} in result
    assert {"term": "bi", "count": 1} in result


def test_get_onboarding_word_cloud_respects_window_and_limit(app):
    with app.app_context():
        _seed_discovery_event({"user_terms_normalized": ["antigo"]}, days_ago=40)
        _seed_discovery_event({"user_terms_normalized": ["recente"]}, days_ago=2)

        result = get_onboarding_word_cloud(limit=1, days=30)

    assert result == [{"term": "recente", "count": 1}]


def test_get_onboarding_word_cloud_skips_invalid_context(app):
    with app.app_context():
        row = AuditoriaGerencial(
            tipo_decisao="onboarding_discovery",
            decisao="x",
            contexto_json="{invalid",
            resultado="sucesso",
        )
        from app.extensions import db

        db.session.add(row)
        db.session.commit()

        result = get_onboarding_word_cloud(limit=10, days=30)

    assert result == []
