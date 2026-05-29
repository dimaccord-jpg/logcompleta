"""Testes do serviço de analytics administrativo do onboarding discovery."""
from __future__ import annotations

import json
from datetime import timedelta

from app.models import AuditoriaGerencial, OnboardingWordCloudHiddenTerm, utcnow_naive
from app.services.onboarding_admin_analytics_service import get_onboarding_word_cloud
from app.services.onboarding_word_cloud_hidden_terms_service import hide_term, restore_hidden_term


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


def _terms(result: dict) -> list[dict]:
    return result["terms"]


def test_get_onboarding_word_cloud_aggregates_terms(app):
    with app.app_context():
        _seed_discovery_event({"user_terms_normalized": ["frete", "custo"]})
        _seed_discovery_event({"user_terms_normalized": ["frete", "bi"]})
        _seed_discovery_event({"user_terms_normalized": ["frete"]})

        result = get_onboarding_word_cloud(limit=10, days=30, pareto_ratio=1.0)

    assert _terms(result)[0] == {"term": "frete", "count": 3}
    assert {"term": "custo", "count": 1} in _terms(result)
    assert {"term": "bi", "count": 1} in _terms(result)
    assert result["total_raw_occurrences"] == 5


def test_get_onboarding_word_cloud_respects_window_and_limit(app):
    with app.app_context():
        _seed_discovery_event({"user_terms_normalized": ["antigo"]}, days_ago=40)
        _seed_discovery_event({"user_terms_normalized": ["recente"]}, days_ago=2)

        result = get_onboarding_word_cloud(limit=1, days=30)

    assert _terms(result) == [{"term": "recente", "count": 1}]


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

    assert _terms(result) == []
    assert result["total_raw_occurrences"] == 0


def test_stopwords_remove_common_noise_terms(app):
    with app.app_context():
        _seed_discovery_event(
            {"user_terms_normalized": ["ola", "tem", "quais", "sao", "voces", "outros", "frete"]}
        )

        result = get_onboarding_word_cloud(limit=20, days=30)
        displayed = {item["term"] for item in _terms(result)}

    assert displayed == {"frete"}
    assert result["removed_terms"]["stopwords"]
    assert "ola" in result["removed_terms"]["stopwords"]


def test_admin_hidden_term_not_in_cloud_or_table(app):
    with app.app_context():
        _seed_discovery_event({"user_terms_normalized": ["frete", "ruido"]})
        hide_term("ruido")

        result = get_onboarding_word_cloud(limit=10, days=30)
        displayed = {item["term"] for item in _terms(result)}

    assert displayed == {"frete"}
    assert result["removed_terms"]["admin_hidden"].get("ruido") == 1


def test_hidden_term_does_not_delete_raw_auditoria(app):
    with app.app_context():
        _seed_discovery_event({"user_terms_normalized": ["frete", "ruido"]})
        hide_term("ruido")

        row = AuditoriaGerencial.query.filter_by(tipo_decisao="onboarding_discovery").first()
        contexto = json.loads(row.contexto_json)

    assert "ruido" in contexto["user_terms_normalized"]
    assert "frete" in contexto["user_terms_normalized"]


def test_restored_term_reappears(app):
    with app.app_context():
        _seed_discovery_event({"user_terms_normalized": ["frete", "ruido"]})
        hidden = hide_term("ruido")
        before = get_onboarding_word_cloud(limit=10, days=30)
        restore_hidden_term(hidden.id)
        after = get_onboarding_word_cloud(limit=10, days=30)

    assert {item["term"] for item in _terms(before)} == {"frete"}
    assert {item["term"] for item in _terms(after)} == {"frete", "ruido"}


def test_pareto_80_20_after_filters(app):
    with app.app_context():
        # frete=80%, custo=15%, bi=5% após filtros
        terms = ["frete"] * 80 + ["custo"] * 15 + ["bi"] * 5
        _seed_discovery_event({"user_terms_normalized": terms})

        result = get_onboarding_word_cloud(limit=40, days=30, pareto_ratio=0.80)
        displayed = _terms(result)

    assert len(displayed) == 1
    assert displayed[0]["term"] == "frete"
    assert result["pareto_coverage"] >= 0.80


def test_pareto_applied_after_stopwords_and_admin_hidden(app):
    with app.app_context():
        terms = ["frete"] * 50 + ["ola"] * 30 + ["ruido"] * 20
        _seed_discovery_event({"user_terms_normalized": terms})
        hide_term("ruido")

        result = get_onboarding_word_cloud(limit=40, days=30, pareto_ratio=0.80)
        displayed = {item["term"] for item in _terms(result)}

    assert displayed == {"frete"}
    assert result["total_raw_occurrences"] == 100
    assert result["total_filtered_occurrences"] == 50


def test_historical_data_filtered_retroactively(app):
    with app.app_context():
        _seed_discovery_event({"user_terms_normalized": ["ola", "frete"]}, days_ago=25)
        hide_term("ola")

        result = get_onboarding_word_cloud(limit=10, days=30)

    assert {item["term"] for item in _terms(result)} == {"frete"}


def test_normalize_hiding_ola_also_filters_ola_variants(app):
    with app.app_context():
        _seed_discovery_event({"user_terms_normalized": ["ola", "frete"]})
        hide_term("Olá")

        result = get_onboarding_word_cloud(limit=10, days=30)

        assert {item["term"] for item in _terms(result)} == {"frete"}
        row = OnboardingWordCloudHiddenTerm.query.filter_by(is_active=True).first()
        assert row.term_normalized == "ola"
