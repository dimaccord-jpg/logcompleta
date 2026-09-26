"""Preferência first-party de marketing (Meta Pixel / GA4 / OpenAI Ads Measurement)."""

from __future__ import annotations

from typing import Any


PRIVACY_MARKETING_STATE_UNKNOWN = "unknown"
PRIVACY_MARKETING_STATE_ACCEPTED = "accepted"
PRIVACY_MARKETING_STATE_REJECTED = "rejected"
PRIVACY_MARKETING_DECISIONS = (
    PRIVACY_MARKETING_STATE_ACCEPTED,
    PRIVACY_MARKETING_STATE_REJECTED,
)

PRIVACY_MARKETING_COOKIE_VERSION = "v1"
PRIVACY_MARKETING_COOKIE_NAME = "af_privacy_marketing"
PRIVACY_MARKETING_COOKIE_MAX_AGE_SECONDS = 180 * 24 * 3600
PRIVACY_MARKETING_COOKIE_PATH = "/"
PRIVACY_MARKETING_COOKIE_SAMESITE = "Lax"

# Envelope minimo externo (SCRUM-157A). Substitui flags booleanas Meta de signup/lead.
SESSION_EXTERNAL_EVENT_PENDING = "af_external_event_pending"
# Slot dedicado first_relevant (SCRUM-157B). Nao reutiliza o slot de signup.
SESSION_EXTERNAL_EVENT_PENDING_FIRST_RELEVANT = (
    "af_external_event_pending_first_relevant"
)
PENDING_MARKETING_PIXEL_SESSION_KEYS = (
    SESSION_EXTERNAL_EVENT_PENDING,
    SESSION_EXTERNAL_EVENT_PENDING_FIRST_RELEVANT,
)


def parse_privacy_marketing_cookie(raw: str | None) -> str:
    """Converte o cookie versionado em unknown|accepted|rejected.

    Ausência, formato inválido ou versão desconhecida nunca são aceite.
    """
    value = (raw or "").strip()
    if not value:
        return PRIVACY_MARKETING_STATE_UNKNOWN

    prefix = f"{PRIVACY_MARKETING_COOKIE_VERSION}:"
    if not value.startswith(prefix):
        return PRIVACY_MARKETING_STATE_UNKNOWN

    decision = value[len(prefix) :]
    if decision in PRIVACY_MARKETING_DECISIONS:
        return decision
    return PRIVACY_MARKETING_STATE_UNKNOWN


def is_privacy_marketing_allowed(state: str) -> bool:
    return state == PRIVACY_MARKETING_STATE_ACCEPTED


def privacy_marketing_cookie_value(decision: str) -> str:
    if decision not in PRIVACY_MARKETING_DECISIONS:
        raise ValueError("decision de marketing inválida")
    return f"{PRIVACY_MARKETING_COOKIE_VERSION}:{decision}"


def apply_privacy_marketing_cookie(
    response: Any,
    *,
    decision: str,
    cookie_name: str,
    max_age_seconds: int,
    secure: bool,
) -> Any:
    response.set_cookie(
        cookie_name,
        privacy_marketing_cookie_value(decision),
        max_age=max_age_seconds,
        path=PRIVACY_MARKETING_COOKIE_PATH,
        httponly=True,
        samesite=PRIVACY_MARKETING_COOKIE_SAMESITE,
        secure=secure,
    )
    return response


def discard_pending_marketing_pixel_flags(session_obj: Any) -> None:
    for key in PENDING_MARKETING_PIXEL_SESSION_KEYS:
        session_obj.pop(key, None)


def store_pending_external_event(session_obj: Any, envelope: dict | None) -> None:
    """Guarda somente envelope minimo de signup/checkout; None limpa."""
    if envelope is None:
        session_obj.pop(SESSION_EXTERNAL_EVENT_PENDING, None)
        return
    session_obj[SESSION_EXTERNAL_EVENT_PENDING] = envelope


def store_pending_first_relevant_external_event(
    session_obj: Any, envelope: dict | None
) -> None:
    """Slot dedicado first_relevant_task_completed; nao sobrescreve signup."""
    if envelope is None:
        session_obj.pop(SESSION_EXTERNAL_EVENT_PENDING_FIRST_RELEVANT, None)
        return
    session_obj[SESSION_EXTERNAL_EVENT_PENDING_FIRST_RELEVANT] = envelope


def _pop_validated_pending_envelope(session_obj: Any, session_key: str) -> dict | None:
    raw = session_obj.pop(session_key, None)
    if not isinstance(raw, dict):
        return None
    try:
        from app.services.growth_external_event_service import is_minimal_external_envelope

        if not is_minimal_external_envelope(raw):
            return None
    except Exception:
        return None
    return raw


def pop_pending_external_event_if_allowed(
    session_obj: Any,
    *,
    marketing_allowed: bool,
) -> dict | None:
    """
    accepted: consome envelope de signup uma vez.
    unknown: deixa pendente.
    rejected: caller deve discard_pending antes.
    """
    if not marketing_allowed:
        return None
    return _pop_validated_pending_envelope(session_obj, SESSION_EXTERNAL_EVENT_PENDING)


def pop_pending_first_relevant_external_event_if_allowed(
    session_obj: Any,
    *,
    marketing_allowed: bool,
) -> dict | None:
    """accepted: consome envelope first_relevant uma vez (slot separado)."""
    if not marketing_allowed:
        return None
    return _pop_validated_pending_envelope(
        session_obj, SESSION_EXTERNAL_EVENT_PENDING_FIRST_RELEVANT
    )


def pop_all_pending_external_events_if_allowed(
    session_obj: Any,
    *,
    marketing_allowed: bool,
) -> list[dict]:
    """
    Consome independentemente signup e first_relevant (ordem estavel).
    Nao sobrescreve um com o outro.
    """
    out: list[dict] = []
    signup = pop_pending_external_event_if_allowed(
        session_obj, marketing_allowed=marketing_allowed
    )
    if signup is not None:
        out.append(signup)
    first_relevant = pop_pending_first_relevant_external_event_if_allowed(
        session_obj, marketing_allowed=marketing_allowed
    )
    if first_relevant is not None:
        out.append(first_relevant)
    return out
