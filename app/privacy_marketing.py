"""Preferência first-party de marketing (Meta Pixel / OpenAI Ads Measurement)."""

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

SESSION_PIXEL_EVENT_COMPLETE_REGISTRATION = "pixel_event_complete_registration_once"
SESSION_PIXEL_EVENT_LEAD = "pixel_event_lead_once"
PENDING_MARKETING_PIXEL_SESSION_KEYS = (
    SESSION_PIXEL_EVENT_COMPLETE_REGISTRATION,
    SESSION_PIXEL_EVENT_LEAD,
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
