"""Endpoints first-party internos do Growth (SCRUM-148 lote 2 / 4A / 157A).

Nao cria visitor ID. Fail-open na persistencia.
Nao inicia Stripe/monetizacao/billing; le MonetizacaoFato so para comprovar
checkout real em checkout_started.
Envelope externo browser somente quando FunnelEvent novo foi persistido.
"""
from __future__ import annotations

import logging
import re

from flask import Blueprint, jsonify, request
from flask_login import current_user
from app.capability_taxonomy import DESTINATIONS
from app.editorial_metadata import listar_habilidades

from app.funnel_event_service import (
    ALLOWED_CONTENT_TYPES,
    ALLOWED_GROWTH_CHECKOUT_PLANS,
    ALLOWED_GROWTH_PAGE_VIEW_PAGES,
    FUNNEL_EVENT_CHECKOUT_STARTED,
    FUNNEL_EVENT_CTA_CLICKED,
    FUNNEL_EVENT_PAGE_VIEW,
    FUNNEL_EVENT_PLAN_SELECTED,
    FUNNEL_SOURCE_GROWTH,
    resolve_funnel_identity_from_user,
    try_record_funnel_event,
    validate_growth_checkout_for_account,
)
from app.services.growth_external_event_service import build_external_event_envelope_if_new

logger = logging.getLogger(__name__)


def _maybe_send_meta_capi(record_result) -> None:
    """CAPI best-effort apos FunnelEvent novo commitado. Fail-open; nao altera JSON."""
    try:
        from app.services.growth_external_server_service import (
            try_send_growth_meta_capi_if_new,
        )

        try_send_growth_meta_capi_if_new(record_result)
    except Exception:
        logger.debug("growth meta capi: falha fail-open", exc_info=True)

growth_bp = Blueprint("growth", __name__)

ALLOWED_STATIC_CTA_IDS = frozenset({
    "home_skill_analisar_fretes", "home_skill_auditar_cobrancas",
    "home_skill_comparar_tabelas", "nav_login_cadastro",
    "discovery_continue_free", "limit_upgrade_view_plans", "profile_view_plans",
    "shell_regularize_payment", "fretes_login", "cleide_bi_login",
})


def allowed_cta_ids():
    return ALLOWED_STATIC_CTA_IDS | {
        f"discovery_handoff_{destination}" for destination in DESTINATIONS
    } | {f"article_skill_{skill['id']}" for skill in listar_habilidades()}


@growth_bp.app_context_processor
def growth_cta_context():
    return {"growth_allowed_cta_ids": sorted(allowed_cta_ids())}


@growth_bp.route("/api/growth/cta-clicked", methods=["POST"])
def growth_cta_clicked():
    payload, err = _require_json_payload()
    if err is not None:
        return err
    event_id = _normalize_event_id(payload.get("event_id"))
    if event_id is None:
        return jsonify({"ok": False, "error": "event_id_invalid"}), 400
    cta_id = payload.get("cta_id")
    if not isinstance(cta_id, str) or cta_id not in allowed_cta_ids():
        return jsonify({"ok": False, "error": "cta_id_invalid"}), 400
    identity = _resolve_auth_identity() or {}
    try_record_funnel_event(
        event_name=FUNNEL_EVENT_CTA_CLICKED,
        source=FUNNEL_SOURCE_GROWTH,
        user_id=identity.get("user_id"),
        conta_id=identity.get("conta_id"),
        franquia_id=identity.get("franquia_id"),
        idempotency_key=event_id,
        metadata_json={"cta_id": cta_id},
    )
    return jsonify({"ok": True})

_CHECKOUT_SESSION_ID_RE = re.compile(r"^[\w-]{1,200}$")


def _normalize_page(raw) -> str | None:
    """Somente endpoint Flask na allowlist Growth; nunca URL/texto livre."""
    if not isinstance(raw, str):
        return None
    page = raw.strip()
    if not page or page not in ALLOWED_GROWTH_PAGE_VIEW_PAGES:
        return None
    return page


def _normalize_optional_content(payload: dict) -> tuple[str | None, int | None] | None:
    raw_type = payload.get("content_type")
    raw_id = payload.get("content_id")
    if raw_type is None and (raw_id is None or raw_id == ""):
        return (None, None)
    content_type = str(raw_type or "").strip().lower()
    if content_type not in ALLOWED_CONTENT_TYPES:
        return None
    try:
        content_id = int(raw_id)
    except (TypeError, ValueError):
        return None
    if content_id <= 0:
        return None
    return (content_type, content_id)


def _require_json_payload():
    if not request.is_json:
        return None, (jsonify({"ok": False, "error": "json_required"}), 400)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return None, (jsonify({"ok": False, "error": "json_required"}), 400)
    return payload, None


def _normalize_event_id(raw) -> str | None:
    event_id = str(raw or "").strip()
    # Reserva espaco para prefixo growth:checkout_started:conta:{id}:occurrence:
    if not event_id or len(event_id) > 80:
        return None
    return event_id


def _normalize_growth_plan(raw) -> str | None:
    if not isinstance(raw, str):
        return None
    plan = raw.strip().lower()
    if plan not in ALLOWED_GROWTH_CHECKOUT_PLANS:
        return None
    return plan


def _normalize_checkout_session_id(raw) -> str | None:
    if not isinstance(raw, str):
        return None
    session_id = raw.strip()
    if not session_id or not _CHECKOUT_SESSION_ID_RE.fullmatch(session_id):
        return None
    return session_id


def _checkout_started_idempotency_key(*, conta_id: int, event_id: str) -> str:
    return f"growth:checkout_started:conta:{int(conta_id)}:occurrence:{event_id}"


def _resolve_auth_identity():
    """Identidade so do backend. Cliente nunca define user/conta/franquia."""
    if not getattr(current_user, "is_authenticated", False):
        return None
    try:
        return resolve_funnel_identity_from_user(current_user)
    except Exception:
        logger.debug("growth: identidade indisponivel", exc_info=True)
        return None


def _optional_current_session_attribution() -> dict | None:
    """
    Snapshot backend current_session_origin para plan_selected / checkout_started.

    Nunca aceita attribution do payload do browser. Fail-open.
    """
    try:
        from app.services.growth_attribution_service import (
            snapshot_current_session_origin_for_persist,
        )

        return snapshot_current_session_origin_for_persist()
    except Exception:
        logger.debug("growth: snapshot attribution indisponivel", exc_info=True)
        return None


def _plan_metadata_with_optional_attribution(plan: str) -> dict:
    metadata: dict = {"plan": plan}
    snap = _optional_current_session_attribution()
    if snap:
        metadata["growth_attribution"] = snap
    return metadata


def _ok_with_optional_external(record_result) -> dict:
    """Contrato callers: ok sempre; external_event so em ocorrencia nova."""
    body: dict = {"ok": True}
    envelope = build_external_event_envelope_if_new(record_result)
    if envelope is not None:
        body["external_event"] = envelope
    return body


@growth_bp.route("/api/growth/page-view", methods=["POST"])
def growth_page_view():
    """Registra page_view interno apos apresentacao no navegador."""
    payload, err = _require_json_payload()
    if err is not None:
        return err

    # Identidade do cliente e ignorada (mesmo se enviada).
    # Aceita apenas chaves do contrato; chaves extras nao entram no evento.
    event_id = str(payload.get("event_id") or "").strip()
    if not event_id or len(event_id) > 160:
        return jsonify({"ok": False, "error": "event_id_invalid"}), 400

    page = _normalize_page(payload.get("page"))
    if page is None:
        return jsonify({"ok": False, "error": "page_invalid"}), 400

    content = _normalize_optional_content(payload)
    if content is None:
        return jsonify({"ok": False, "error": "content_invalid"}), 400
    content_type, content_id = content

    metadata: dict = {"page": page}
    if content_id is not None and content_type is not None:
        metadata["content_type"] = content_type
        metadata["content_id"] = content_id

    identity = {"user_id": None, "conta_id": None, "franquia_id": None}
    try:
        identity = resolve_funnel_identity_from_user(current_user)
    except Exception:
        logger.debug("growth page_view: identidade indisponivel; seguindo anonimo", exc_info=True)

    result = try_record_funnel_event(
        event_name=FUNNEL_EVENT_PAGE_VIEW,
        source=FUNNEL_SOURCE_GROWTH,
        user_id=identity["user_id"],
        conta_id=identity["conta_id"],
        franquia_id=identity["franquia_id"],
        idempotency_key=event_id,
        metadata_json=metadata,
    )
    # CAPI somente apos Growth novo commitado; falha Meta nao altera JSON.
    _maybe_send_meta_capi(result)
    # Fail-open: mesmo se a persistencia falhar, nao quebra a pagina.
    return jsonify(_ok_with_optional_external(result))


@growth_bp.route("/api/growth/plan-selected", methods=["POST"])
def growth_plan_selected():
    """Registra plan_selected apos selecao explicita starter|pro|multiuser."""
    payload, err = _require_json_payload()
    if err is not None:
        return err

    identity = _resolve_auth_identity()
    if identity is None or identity.get("user_id") is None:
        return jsonify({"ok": False, "error": "auth_required"}), 401

    event_id = _normalize_event_id(payload.get("event_id"))
    if event_id is None:
        return jsonify({"ok": False, "error": "event_id_invalid"}), 400

    plan = _normalize_growth_plan(payload.get("plan"))
    if plan is None:
        return jsonify({"ok": False, "error": "plan_invalid"}), 400

    # Identidade e attribution do cliente ignoradas; so plan + event_id do contrato.
    # growth_attribution / first_touch / current_session_origin do browser: descartados.
    try_record_funnel_event(
        event_name=FUNNEL_EVENT_PLAN_SELECTED,
        source=FUNNEL_SOURCE_GROWTH,
        user_id=identity["user_id"],
        conta_id=identity.get("conta_id"),
        franquia_id=identity.get("franquia_id"),
        idempotency_key=event_id,
        metadata_json=_plan_metadata_with_optional_attribution(plan),
    )
    return jsonify({"ok": True})


@growth_bp.route("/api/growth/checkout-started", methods=["POST"])
def growth_checkout_started():
    """Registra checkout_started apos checkout Stripe real comprovado no backend."""
    payload, err = _require_json_payload()
    if err is not None:
        return err

    identity = _resolve_auth_identity()
    if identity is None or identity.get("user_id") is None:
        return jsonify({"ok": False, "error": "auth_required"}), 401

    conta_id = identity.get("conta_id")
    if conta_id is None:
        return jsonify({"ok": False, "error": "conta_required"}), 400

    event_id = _normalize_event_id(payload.get("event_id"))
    if event_id is None:
        return jsonify({"ok": False, "error": "event_id_invalid"}), 400

    plan = _normalize_growth_plan(payload.get("plan"))
    if plan is None:
        return jsonify({"ok": False, "error": "plan_invalid"}), 400

    checkout_session_id = _normalize_checkout_session_id(payload.get("checkout_session_id"))
    if checkout_session_id is None:
        return jsonify({"ok": False, "error": "checkout_session_id_invalid"}), 400

    # Browser pode enviar o id para correlacionar; nao e prova suficiente.
    if not validate_growth_checkout_for_account(
        conta_id, checkout_session_id, plan
    ):
        # Fail-open comercial: sem FunnelEvent / external_event.
        return jsonify({"ok": True})

    # Sem client_secret / publishable_key / amount / PII no evento.
    # Attribution so da sessao backend desta request (nao herda de plan_selected).
    # correlation_id interno nao entra no envelope externo.
    result = try_record_funnel_event(
        event_name=FUNNEL_EVENT_CHECKOUT_STARTED,
        source=FUNNEL_SOURCE_GROWTH,
        user_id=identity.get("user_id"),
        conta_id=conta_id,
        franquia_id=identity.get("franquia_id"),
        idempotency_key=_checkout_started_idempotency_key(
            conta_id=int(conta_id),
            event_id=event_id,
        ),
        correlation_id=checkout_session_id,
        metadata_json=_plan_metadata_with_optional_attribution(plan),
    )
    # CAPI apos checkout_started Growth novo commitado; fail-open.
    _maybe_send_meta_capi(result)
    return jsonify(_ok_with_optional_external(result))
