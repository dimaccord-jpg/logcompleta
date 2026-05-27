"""
Agregações administrativas sobre eventos onboarding_discovery (AuditoriaGerencial).
"""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from app.models import AuditoriaGerencial, utcnow_naive

TIPO_DECISAO_ONBOARDING_DISCOVERY = "onboarding_discovery"


def get_onboarding_word_cloud(*, limit: int = 30, days: int = 30) -> list[dict[str, Any]]:
    """
    Agrega termos normalizados de mensagens de usuário nos últimos `days` dias.
    Retorna lista ordenada por contagem decrescente: [{"term": "frete", "count": 18}, ...].
    """
    limit = max(1, min(int(limit), 200))
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
            term = str(raw_term or "").strip().lower()
            if not term:
                continue
            counts[term] = counts.get(term, 0) + 1

    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [{"term": term, "count": count} for term, count in ranked[:limit]]
