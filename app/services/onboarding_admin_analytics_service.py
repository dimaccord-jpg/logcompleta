"""
Agregações administrativas sobre eventos onboarding_discovery (AuditoriaGerencial).
"""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from app.models import AuditoriaGerencial, utcnow_naive
from app.services.onboarding_word_cloud_hidden_terms_service import get_active_hidden_term_set
from app.utils.onboarding_text_normalization import (
    is_onboarding_stopword,
    normalize_word_cloud_term,
)

TIPO_DECISAO_ONBOARDING_DISCOVERY = "onboarding_discovery"

DEFAULT_WORD_CLOUD_DAYS = 30
DEFAULT_WORD_CLOUD_MAX_TERMS = 40
DEFAULT_PARETO_RATIO = 0.80


def _aggregate_raw_term_counts(*, days: int) -> tuple[dict[str, int], int]:
    days = max(1, min(int(days), 365))
    since = utcnow_naive() - timedelta(days=days)

    rows = (
        AuditoriaGerencial.query.filter(
            AuditoriaGerencial.tipo_decisao == TIPO_DECISAO_ONBOARDING_DISCOVERY,
            AuditoriaGerencial.created_at >= since,
        )
        .all()
    )

    counts: dict[str, int] = {}
    total_raw = 0
    for row in rows:
        if not row.contexto_json:
            continue
        try:
            contexto = json.loads(row.contexto_json)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(contexto, dict):
            continue
        raw_terms = contexto.get("user_terms_normalized")
        if not isinstance(raw_terms, list):
            continue
        for raw_term in raw_terms:
            term = normalize_word_cloud_term(str(raw_term or ""))
            if not term:
                continue
            counts[term] = counts.get(term, 0) + 1
            total_raw += 1
    return counts, total_raw


def _apply_filters(
    raw_counts: dict[str, int],
    *,
    admin_hidden: frozenset[str],
) -> tuple[dict[str, int], dict[str, int]]:
    """Retorna contagens filtradas e mapa de termos removidos (motivo -> termo -> count)."""
    filtered: dict[str, int] = {}
    removed: dict[str, dict[str, int]] = {
        "stopwords": {},
        "admin_hidden": {},
    }

    for term, count in raw_counts.items():
        if is_onboarding_stopword(term):
            removed["stopwords"][term] = count
            continue
        if term in admin_hidden:
            removed["admin_hidden"][term] = count
            continue
        filtered[term] = count
    return filtered, removed


def _apply_pareto(
    ranked: list[tuple[str, int]],
    *,
    total_filtered: int,
    pareto_ratio: float,
    max_terms: int,
) -> tuple[list[dict[str, Any]], float]:
    if not ranked or total_filtered <= 0:
        return [], 0.0

    display: list[dict[str, Any]] = []
    cumulative = 0
    target = max(0.0, min(float(pareto_ratio), 1.0))
    cap = max(1, int(max_terms))

    for term, count in ranked:
        display.append({"term": term, "count": count})
        cumulative += count
        if len(display) >= cap:
            break
        if cumulative / total_filtered >= target:
            break

    coverage = cumulative / total_filtered if total_filtered else 0.0
    return display, coverage


def get_onboarding_word_cloud(
    *,
    limit: int = DEFAULT_WORD_CLOUD_MAX_TERMS,
    days: int = DEFAULT_WORD_CLOUD_DAYS,
    pareto_ratio: float = DEFAULT_PARETO_RATIO,
) -> dict[str, Any]:
    """
    Agrega termos normalizados dos últimos `days` dias com filtros e Pareto 80/20.

    Fluxo: user_terms_normalized → stopwords → ocultos admin → frequência → Pareto.
    Dados brutos em AuditoriaGerencial permanecem intactos.
    """
    limit = max(1, min(int(limit), 200))
    days = max(1, min(int(days), 365))

    raw_counts, total_raw = _aggregate_raw_term_counts(days=days)
    admin_hidden = get_active_hidden_term_set()
    filtered_counts, removed = _apply_filters(raw_counts, admin_hidden=admin_hidden)
    total_filtered = sum(filtered_counts.values())

    ranked = sorted(filtered_counts.items(), key=lambda item: (-item[1], item[0]))
    terms, pareto_coverage = _apply_pareto(
        ranked,
        total_filtered=total_filtered,
        pareto_ratio=pareto_ratio,
        max_terms=limit,
    )

    return {
        "terms": terms,
        "admin_hidden_terms": [
            {"id": item["id"], "term": item["term"]}
            for item in _list_admin_hidden_for_payload()
        ],
        "total_raw_occurrences": total_raw,
        "total_filtered_occurrences": total_filtered,
        "pareto_coverage": round(pareto_coverage, 4),
        "pareto_target": pareto_ratio,
        "days": days,
        "removed_terms": removed,
    }


def _list_admin_hidden_for_payload() -> list[dict[str, Any]]:
    from app.services.onboarding_word_cloud_hidden_terms_service import list_active_hidden_terms

    return list_active_hidden_terms()
