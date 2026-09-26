"""Envelope e token opaco para tracking browser externo (SCRUM-157 Lote 157A).

Growth first-party permanece a fonte de verdade. Este modulo so materializa
um envelope minimo quando um FunnelEvent novo foi persistido.

Reutilizavel pela SCRUM-158 (CAPI / server-side): o token HMAC deve permanecer
estavel entre browser e server para o mesmo FunnelEvent.id.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Any

from app.funnel_event_service import (
    ALLOWED_GROWTH_CHECKOUT_PLANS,
    ALLOWED_GROWTH_PAGE_VIEW_PAGES,
    ALLOWED_SIGNUP_METHODS,
    FUNNEL_EVENT_CHECKOUT_STARTED,
    FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
    FUNNEL_EVENT_PAGE_VIEW,
    FUNNEL_EVENT_SIGNUP_COMPLETED,
)

logger = logging.getLogger(__name__)

EXTERNAL_EVENT_TOKEN_VERSION = "v1"
EXTERNAL_EVENT_TOKEN_DIGEST_HEX_LEN = 32  # 128 bits; prefixo v1_ fora do digest
GROWTH_EXTERNAL_EVENT_TOKEN_SECRET_ENV = "GROWTH_EXTERNAL_EVENT_TOKEN_SECRET"

EXTERNAL_BROWSER_ALLOWED_EVENTS = frozenset(
    {
        FUNNEL_EVENT_PAGE_VIEW,
        FUNNEL_EVENT_SIGNUP_COMPLETED,
        FUNNEL_EVENT_CHECKOUT_STARTED,
        FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
    }
)

def _resolve_token_secret() -> str:
    """Segredo estavel: env dedicado, config Flask, senao SECRET_KEY."""
    dedicated = (os.getenv(GROWTH_EXTERNAL_EVENT_TOKEN_SECRET_ENV) or "").strip()
    if dedicated:
        return dedicated
    try:
        from flask import current_app

        cfg_dedicated = (
            current_app.config.get("GROWTH_EXTERNAL_EVENT_TOKEN_SECRET") or ""
        ).strip()
        if cfg_dedicated:
            return cfg_dedicated
        app_secret = (current_app.config.get("SECRET_KEY") or "").strip()
        if app_secret:
            return app_secret
    except Exception:
        logger.debug("growth_external: secret via current_app indisponivel", exc_info=True)
    try:
        from app.settings import settings

        if (settings.growth_external_event_token_secret or "").strip():
            return settings.growth_external_event_token_secret.strip()
        fallback = (settings.secret_key or "").strip()
        if fallback:
            return fallback
    except Exception:
        logger.debug("growth_external: settings secret indisponivel", exc_info=True)
    return ""


def build_external_event_token(*, event_name: str, funnel_event_id: int) -> str:
    """
    Token opaco versionado: v1_<hmac_sha256 truncado>.

    Material: growth-external:v1:<event_name>:<funnel_event.id>
    Nao embute id puro, idempotency_key nem identidade.
    """
    name = str(event_name or "").strip().lower()
    event_id = int(funnel_event_id)
    secret = _resolve_token_secret()
    if not secret:
        raise ValueError("segredo de token externo indisponivel")
    material = f"growth-external:{EXTERNAL_EVENT_TOKEN_VERSION}:{name}:{event_id}"
    digest = hmac.new(
        secret.encode("utf-8"),
        material.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:EXTERNAL_EVENT_TOKEN_DIGEST_HEX_LEN]
    return f"{EXTERNAL_EVENT_TOKEN_VERSION}_{digest}"


def _controlled_params(event_name: str, metadata: Any) -> dict[str, Any]:
    """Allowlist minima de params publicos; nenhuma chave/valor arbitrario."""
    meta = metadata if isinstance(metadata, dict) else {}
    if event_name == FUNNEL_EVENT_PAGE_VIEW:
        page = meta.get("page")
        if isinstance(page, str):
            page_n = page.strip()
            if page_n in ALLOWED_GROWTH_PAGE_VIEW_PAGES:
                return {"page": page_n}
        return {}
    if event_name == FUNNEL_EVENT_SIGNUP_COMPLETED:
        method = meta.get("signup_method")
        if isinstance(method, str):
            method_n = method.strip().lower()
            if method_n in ALLOWED_SIGNUP_METHODS:
                return {"signup_method": method_n}
        return {}
    if event_name == FUNNEL_EVENT_CHECKOUT_STARTED:
        plan = meta.get("plan")
        if isinstance(plan, str):
            plan_n = plan.strip().lower()
            if plan_n in ALLOWED_GROWTH_CHECKOUT_PLANS:
                return {"plan": plan_n}
        return {}
    if event_name == FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED:
        # 157B: envelope publico sem task_type nem metadata.
        return {}
    return {}


def build_external_event_envelope(funnel_event: Any) -> dict[str, Any] | None:
    """Monta envelope minimo a partir de um FunnelEvent persistido."""
    if funnel_event is None:
        return None
    event_name = str(getattr(funnel_event, "event_name", "") or "").strip().lower()
    if event_name not in EXTERNAL_BROWSER_ALLOWED_EVENTS:
        return None
    event_id = getattr(funnel_event, "id", None)
    if event_id is None:
        return None
    try:
        token = build_external_event_token(
            event_name=event_name,
            funnel_event_id=int(event_id),
        )
    except Exception:
        logger.exception(
            "growth_external: falha ao gerar token event_name=%s id=%s",
            event_name,
            event_id,
        )
        return None
    return {
        "event": event_name,
        "token": token,
        "params": _controlled_params(event_name, getattr(funnel_event, "metadata_json", None)),
    }


def build_external_event_envelope_if_new(record_result: dict[str, Any] | None) -> dict[str, Any] | None:
    """
    Envelope somente quando a ocorrencia Growth e nova (created=True).

    Replay (created=False) ou falha (None) => None.
    """
    if not isinstance(record_result, dict):
        return None
    if record_result.get("created") is not True:
        return None
    return build_external_event_envelope(record_result.get("event"))


def is_minimal_external_envelope(value: Any) -> bool:
    """Valida envelope de sessao/browser: so event+token+params controlados."""
    if not isinstance(value, dict):
        return False
    event = str(value.get("event") or "").strip().lower()
    if event not in EXTERNAL_BROWSER_ALLOWED_EVENTS:
        return False
    token = str(value.get("token") or "").strip()
    if not token.startswith(f"{EXTERNAL_EVENT_TOKEN_VERSION}_"):
        return False
    if len(token) > 80:
        return False
    params = value.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return False
    allowed_keys = {
        FUNNEL_EVENT_PAGE_VIEW: frozenset({"page"}),
        FUNNEL_EVENT_SIGNUP_COMPLETED: frozenset({"signup_method"}),
        FUNNEL_EVENT_CHECKOUT_STARTED: frozenset({"plan"}),
        FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED: frozenset(),
    }[event]
    if set(params.keys()) - allowed_keys:
        return False
    # Revalida valores controlados
    rebuilt = _controlled_params(event, params)
    return rebuilt == {k: params[k] for k in rebuilt}
