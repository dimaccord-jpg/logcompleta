"""Experimento isolado do CTA principal da Home (home_chat_cta_v1).

Não usa FunnelEvent. Não persiste PII. Fail-open na telemetria.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from typing import Any

from flask import session as flask_session
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import HomeCtaExperimentEvent, utcnow_naive

logger = logging.getLogger(__name__)

HOME_CTA_EXPERIMENT = "home_chat_cta_v1"

HOME_CTA_VARIANTS = {
    "cta_a": "Como posso ajudar sua operação logística hoje?",
    "cta_b": "Descreva seu desafio logístico e veja como o AgenteFrete pode ajudar.",
    "cta_c": "Tem uma dúvida de logística? Conte o cenário e receba uma orientação.",
}

HOME_CTA_VARIANT_IDS = tuple(HOME_CTA_VARIANTS.keys())

EVENT_IMPRESSION = "impression"
EVENT_CONVERSION = "conversion"
ALLOWED_EVENT_TYPES = frozenset({EVENT_IMPRESSION, EVENT_CONVERSION})

ORIGIN_TYPED = "typed"
ORIGIN_SUGGESTION = "suggestion"

SESSION_KEY = "home_chat_cta_experiment"

_ASSIGNMENT_ID_HEX_LEN = 64


def get_variant_text(variant: str) -> str:
    return HOME_CTA_VARIANTS[variant]


def is_valid_variant(variant: str | None) -> bool:
    return variant in HOME_CTA_VARIANTS


def build_home_cta_template_context(assignment: dict[str, str] | None) -> dict[str, str] | None:
    if not assignment:
        return None
    variant = assignment.get("variant") or ""
    if not is_valid_variant(variant):
        return None
    return {
        "experiment": HOME_CTA_EXPERIMENT,
        "variant": variant,
        "text": get_variant_text(variant),
    }


def load_home_cta_assignment_from_session(store=None) -> dict[str, str] | None:
    raw = (store if store is not None else flask_session).get(SESSION_KEY)
    return _parse_session_assignment(raw)


def resolve_home_cta_assignment(*, user=None, store=None) -> dict[str, str]:
    """Reutiliza assignment da session; só sorteia ou deriva se ainda não houver."""
    sess = store if store is not None else flask_session
    existing = _parse_session_assignment(sess.get(SESSION_KEY))
    if existing:
        return existing

    user_id_material = _authenticated_user_id_material(user)
    if user_id_material is not None:
        assignment = build_authenticated_assignment(user_id_material)
    else:
        assignment = build_anonymous_assignment()
    _save_assignment_to_session(sess, assignment)
    return assignment


def build_anonymous_assignment() -> dict[str, str]:
    variant = secrets.choice(HOME_CTA_VARIANT_IDS)
    return {
        "experiment": HOME_CTA_EXPERIMENT,
        "variant": variant,
        "assignment_id": secrets.token_hex(16),
    }


def build_authenticated_assignment(user_id_material: str) -> dict[str, str]:
    """Variante e assignment_id determinísticos. Não grava user.id em claro."""
    material = f"{HOME_CTA_EXPERIMENT}:{user_id_material}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    variant = HOME_CTA_VARIANT_IDS[int(digest, 16) % len(HOME_CTA_VARIANT_IDS)]
    assignment_id = hashlib.sha256(
        f"{HOME_CTA_EXPERIMENT}:assignment:{user_id_material}".encode("utf-8")
    ).hexdigest()
    return {
        "experiment": HOME_CTA_EXPERIMENT,
        "variant": variant,
        "assignment_id": assignment_id,
    }


def record_home_cta_event(
    *,
    experiment: str,
    assignment_id: str,
    variant: str,
    event_type: str,
    interaction_origin: str | None = None,
) -> dict[str, Any]:
    """Persiste um evento com savepoint. Colisão unique = replay válido."""
    experiment_n = (experiment or "").strip()
    assignment_id_n = (assignment_id or "").strip()
    variant_n = (variant or "").strip()
    event_type_n = (event_type or "").strip()
    if experiment_n != HOME_CTA_EXPERIMENT:
        raise ValueError("experiment invalido")
    if not assignment_id_n or len(assignment_id_n) > _ASSIGNMENT_ID_HEX_LEN:
        raise ValueError("assignment_id invalido")
    if not is_valid_variant(variant_n):
        raise ValueError("variant invalida")
    if event_type_n not in ALLOWED_EVENT_TYPES:
        raise ValueError("event_type invalido")

    origin_n = (interaction_origin or "").strip() or None
    if event_type_n == EVENT_CONVERSION:
        if origin_n not in {ORIGIN_TYPED, ORIGIN_SUGGESTION}:
            raise ValueError("interaction_origin invalido")
    else:
        origin_n = None

    orm = db.session
    row: HomeCtaExperimentEvent | None = None
    try:
        with orm.begin_nested():
            row = HomeCtaExperimentEvent(
                experiment=experiment_n,
                assignment_id=assignment_id_n,
                variant=variant_n,
                event_type=event_type_n,
                interaction_origin=origin_n,
                occurred_at=utcnow_naive(),
            )
            orm.add(row)
            orm.flush()
    except IntegrityError:
        existing = (
            HomeCtaExperimentEvent.query.filter_by(
                experiment=experiment_n,
                assignment_id=assignment_id_n,
                event_type=event_type_n,
            ).first()
        )
        if existing is None:
            raise
        return {"created": False, "event": existing}

    if row is None:
        raise RuntimeError("Nao foi possivel registrar evento do CTA da Home.")
    return {"created": True, "event": row}


def try_record_home_cta_impression(assignment: dict[str, str] | None) -> None:
    """Fail-open: falha de telemetria não quebra a Home."""
    if not assignment:
        return
    try:
        record_home_cta_event(
            experiment=assignment.get("experiment") or "",
            assignment_id=assignment.get("assignment_id") or "",
            variant=assignment.get("variant") or "",
            event_type=EVENT_IMPRESSION,
        )
        db.session.commit()
    except Exception:
        _safe_rollback()
        logger.exception("home_cta_experiment impression failed")


def try_record_home_cta_conversion_from_session(store=None, *, cta_id: str | None = None) -> None:
    """Fail-open: falha de telemetria não quebra o chat. Sem assignment = no-op."""
    assignment = load_home_cta_assignment_from_session(store)
    if not assignment:
        return
    origin = ORIGIN_SUGGESTION if (cta_id or "").strip() else ORIGIN_TYPED
    try:
        record_home_cta_event(
            experiment=assignment.get("experiment") or "",
            assignment_id=assignment.get("assignment_id") or "",
            variant=assignment.get("variant") or "",
            event_type=EVENT_CONVERSION,
            interaction_origin=origin,
        )
        db.session.commit()
    except Exception:
        _safe_rollback()
        logger.exception("home_cta_experiment conversion failed")


def _parse_session_assignment(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    experiment = str(raw.get("experiment") or "").strip()
    variant = str(raw.get("variant") or "").strip()
    assignment_id = str(raw.get("assignment_id") or "").strip()
    if experiment != HOME_CTA_EXPERIMENT:
        return None
    if not is_valid_variant(variant):
        return None
    if not assignment_id or len(assignment_id) > _ASSIGNMENT_ID_HEX_LEN:
        return None
    return {
        "experiment": HOME_CTA_EXPERIMENT,
        "variant": variant,
        "assignment_id": assignment_id,
    }


def _save_assignment_to_session(store, assignment: dict[str, str]) -> None:
    store[SESSION_KEY] = {
        "experiment": assignment["experiment"],
        "variant": assignment["variant"],
        "assignment_id": assignment["assignment_id"],
    }
    try:
        store.modified = True
    except Exception:
        pass


def _authenticated_user_id_material(user) -> str | None:
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    raw = getattr(user, "id", None)
    if raw is None or raw == "":
        return None
    return str(raw)


def _safe_rollback() -> None:
    try:
        db.session.rollback()
    except Exception:
        logger.exception("home_cta_experiment rollback failed")
