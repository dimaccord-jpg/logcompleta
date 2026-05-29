"""
Persistência e operações admin para termos ocultos na nuvem do onboarding.
"""
from __future__ import annotations

from app.extensions import db
from app.models import OnboardingWordCloudHiddenTerm, utcnow_naive
from app.utils.onboarding_text_normalization import normalize_word_cloud_term


class InvalidHiddenTermError(ValueError):
    """Termo inválido para ocultação (vazio, curto, etc.)."""


def get_active_hidden_term_set() -> frozenset[str]:
    rows = (
        OnboardingWordCloudHiddenTerm.query.filter_by(is_active=True)
        .with_entities(OnboardingWordCloudHiddenTerm.term_normalized)
        .all()
    )
    return frozenset(row[0] for row in rows if row[0])


def list_active_hidden_terms() -> list[dict]:
    rows = (
        OnboardingWordCloudHiddenTerm.query.filter_by(is_active=True)
        .order_by(OnboardingWordCloudHiddenTerm.term_normalized.asc())
        .all()
    )
    return [
        {
            "id": row.id,
            "term": row.term_normalized,
            "hidden_by_user_id": row.hidden_by_user_id,
            "notes": row.notes,
            "created_at": row.created_at,
        }
        for row in rows
    ]


def hide_term(
    raw_term: str,
    *,
    hidden_by_user_id: int | None = None,
    notes: str | None = None,
) -> OnboardingWordCloudHiddenTerm:
    term = normalize_word_cloud_term(raw_term)
    if not term:
        raise InvalidHiddenTermError("Informe um termo valido para ocultar.")

    existing = (
        OnboardingWordCloudHiddenTerm.query.filter_by(term_normalized=term)
        .order_by(OnboardingWordCloudHiddenTerm.id.desc())
        .first()
    )
    if existing:
        if existing.is_active:
            return existing
        existing.is_active = True
        existing.hidden_by_user_id = hidden_by_user_id
        if notes is not None:
            existing.notes = (notes or "")[:255] or None
        existing.updated_at = utcnow_naive()
        db.session.commit()
        return existing

    row = OnboardingWordCloudHiddenTerm(
        term_normalized=term,
        is_active=True,
        hidden_by_user_id=hidden_by_user_id,
        notes=(notes or "")[:255] or None if notes else None,
    )
    db.session.add(row)
    db.session.commit()
    return row


def restore_hidden_term(term_id: int) -> OnboardingWordCloudHiddenTerm | None:
    row = OnboardingWordCloudHiddenTerm.query.get(int(term_id))
    if row is None or not row.is_active:
        return None
    row.is_active = False
    row.updated_at = utcnow_naive()
    db.session.commit()
    return row
