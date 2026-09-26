"""Meta Conversions API server-side (SCRUM-158 Lote 158A) — homolog-first, fail-open.

Growth first-party permanece a fonte de verdade. Este adapter e best-effort:
ausencia/rejeicao da Meta jamais altera produto, FunnelEvent ou resposta funcional.

Escopo deste lote (somente Meta CAPI):
  page_view -> PageView
  signup_completed -> CompleteRegistration
  first_relevant_task_completed -> FirstRelevantTaskCompleted
  checkout_started -> InitiateCheckout

Explicitamente NAO implementado neste lote:
  - Google Measurement Protocol / GA4 server-side
  - Google Ads server-side
  - Meta Purchase / Growth paid externo
  - OpenAI Ads server-side
  - fila/worker/retries
  - Advanced Matching (email/telefone/nome hash, IP, User-Agent)

META_CAPI_ENABLED default false: codigo pode existir sem ativar producao
antes da homologacao real com test_event_code.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests

from app.funnel_event_service import (
    FUNNEL_EVENT_CHECKOUT_STARTED,
    FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
    FUNNEL_EVENT_PAGE_VIEW,
    FUNNEL_EVENT_SIGNUP_COMPLETED,
    TASK_TYPE_AGENTE_COMPARA,
    TASK_TYPE_AGENTE_COMPARA_CHAT,
    TASK_TYPE_CLEIDE_AUDIT,
    TASK_TYPE_CLEIDE_AUDIT_CHAT,
    TASK_TYPE_CLEIDE_BI,
    TASK_TYPE_CLEIDE_BI_CHAT,
    TASK_TYPE_FREIGHT_QUERY,
    TASK_TYPE_JULIA_CHAT,
    TASK_TYPE_ROBERTO_BI,
    TASK_TYPE_ROBERTO_CHAT,
)
from app.privacy_marketing import (
    PRIVACY_MARKETING_COOKIE_NAME,
    PRIVACY_MARKETING_STATE_ACCEPTED,
    parse_privacy_marketing_cookie,
)
from app.services.growth_external_event_service import build_external_event_token

logger = logging.getLogger(__name__)

# --- Status tecnico controlado (nunca corpo bruto Meta) ---------------------
STATUS_DISABLED = "disabled"
STATUS_CONSENT_NOT_ACCEPTED = "consent_not_accepted"
STATUS_CONFIG_MISSING = "config_missing"
STATUS_TEST_CODE_IN_PROD = "test_code_in_prod"
STATUS_USER_DATA_MISSING = "user_data_missing"
STATUS_URL_UNAVAILABLE = "url_unavailable"
STATUS_EVENT_TIME_INVALID = "event_time_invalid"
STATUS_UNSUPPORTED_EVENT = "unsupported_event"
STATUS_NOT_NEW = "not_new"
STATUS_TIMEOUT = "timeout"
STATUS_HTTP_ERROR = "http_error"
STATUS_SUCCESS = "success"
STATUS_INTERNAL_ERROR = "internal_error"

META_CAPI_ALLOWED_GROWTH_EVENTS = frozenset(
    {
        FUNNEL_EVENT_PAGE_VIEW,
        FUNNEL_EVENT_SIGNUP_COMPLETED,
        FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
        FUNNEL_EVENT_CHECKOUT_STARTED,
    }
)

# Nomes Meta identicos ao browser (af_external_tracking.js). Token HMAC usa
# o nome Growth, nunca o nome Meta.
META_CAPI_EVENT_NAME_MAP = {
    FUNNEL_EVENT_PAGE_VIEW: "PageView",
    FUNNEL_EVENT_SIGNUP_COMPLETED: "CompleteRegistration",
    FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED: "FirstRelevantTaskCompleted",
    FUNNEL_EVENT_CHECKOUT_STARTED: "InitiateCheckout",
}

# Allowlist CAPI propria (paths literais). Separada da Growth page allowlist.
# Nao inclui detalhe_noticia, reset com token, newsletter cancel, OAuth,
# convite, admin com identificador, /acesso-desktop.
META_CAPI_PAGE_VIEW_PATHS: dict[str, str] = {
    "index": "/",
    "feed": "/feed",
    "login": "/login",
    "request_password_reset": "/request-password-reset",
    "complete_profile": "/complete-profile",
    "chat_julia": "/chat_julia",
    "fretes": "/fretes",
    "controle_estoque": "/controle-estoque",
    "insights_frete": "/insights-frete",
    "user.perfil": "/perfil",
    "user.contrate_plano": "/contrate-um-plano",
    "user.regularizar_pagamento": "/perfil/regularizar-pagamento",
    "agente_compara.agente_compara_page": "/agente-compara",
    "cleide.auditoria_frete": "/cleide-bi-frete",
    "cleide.cleide_auditoria": "/auditoria-frete",
    "multiuser_painel.gestao_multiuser": "/gestao-multiuser",
}

# task_type so resolve URL internamente; nunca entra no payload Meta.
META_CAPI_FIRST_RELEVANT_PATHS: dict[str, str] = {
    TASK_TYPE_CLEIDE_AUDIT: "/auditoria-frete",
    TASK_TYPE_CLEIDE_AUDIT_CHAT: "/auditoria-frete",
    TASK_TYPE_AGENTE_COMPARA: "/agente-compara",
    TASK_TYPE_AGENTE_COMPARA_CHAT: "/agente-compara",
    TASK_TYPE_CLEIDE_BI: "/cleide-bi-frete",
    TASK_TYPE_CLEIDE_BI_CHAT: "/cleide-bi-frete",
    TASK_TYPE_ROBERTO_BI: "/fretes",
    TASK_TYPE_ROBERTO_CHAT: "/fretes",
    TASK_TYPE_FREIGHT_QUERY: "/fretes",
    # julia_chat: SKIP deliberado neste lote (sem URL arbitraria).
}

META_COOKIE_FBP = "_fbp"
META_COOKIE_FBC = "_fbc"
META_COOKIE_MAX_BYTES = 1024
_GRAPH_VERSION_RE = re.compile(r"^v\d+\.\d+$")
_TEST_EVENT_CODE_MAX_LEN = 128


def _result(
    *,
    attempted: bool,
    success: bool,
    status: str,
    http_status: int | None = None,
) -> dict[str, Any]:
    return {
        "attempted": bool(attempted),
        "success": bool(success),
        "status": str(status),
        "http_status": http_status,
    }


def _env_str(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _cfg_from_app(name: str) -> str:
    try:
        from flask import current_app, has_app_context

        if has_app_context():
            return (current_app.config.get(name) or "").strip()
    except Exception:
        pass
    return ""


def _settings_attr(name: str) -> str:
    try:
        from app.settings import settings

        return (getattr(settings, name, None) or "").strip()
    except Exception:
        return ""


def _resolve_str(env_name: str, *, settings_attr: str | None = None, config_key: str | None = None) -> str:
    value = _env_str(env_name)
    if value:
        return value
    if config_key:
        value = _cfg_from_app(config_key)
        if value:
            return value
    if settings_attr:
        return _settings_attr(settings_attr)
    return ""


def _resolve_meta_capi_enabled() -> bool:
    raw = _env_str("META_CAPI_ENABLED")
    if raw:
        return raw.lower() == "true"
    try:
        from flask import current_app, has_app_context

        if has_app_context() and "META_CAPI_ENABLED" in current_app.config:
            val = current_app.config.get("META_CAPI_ENABLED")
            if isinstance(val, bool):
                return val
            return str(val or "").strip().lower() == "true"
    except Exception:
        pass
    try:
        from app.settings import settings

        return bool(getattr(settings, "meta_capi_enabled", False))
    except Exception:
        return False


def _resolve_timeouts() -> tuple[float, float]:
    connect_default = 0.5
    read_default = 1.0
    try:
        from app.settings import settings

        connect_default = float(getattr(settings, "meta_capi_connect_timeout_seconds", 0.5) or 0.5)
        read_default = float(getattr(settings, "meta_capi_read_timeout_seconds", 1.0) or 1.0)
    except Exception:
        pass

    def _one(env_name: str, config_key: str, default: float) -> float:
        raw = _env_str(env_name) or _cfg_from_app(config_key)
        if not raw:
            return default
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return default
        if value <= 0:
            return default
        return value

    return (
        _one("META_CAPI_CONNECT_TIMEOUT_SECONDS", "META_CAPI_CONNECT_TIMEOUT_SECONDS", connect_default),
        _one("META_CAPI_READ_TIMEOUT_SECONDS", "META_CAPI_READ_TIMEOUT_SECONDS", read_default),
    )


def _resolve_app_env() -> str:
    raw = _env_str("APP_ENV").lower()
    if raw in ("dev", "homolog", "prod"):
        return raw
    try:
        from app.settings import settings

        env = str(getattr(settings, "app_env", "") or "").strip().lower()
        if env in ("dev", "homolog", "prod"):
            return env
    except Exception:
        pass
    return ""


def is_valid_meta_graph_api_version(value: Any) -> bool:
    """Formato obrigatorio v<inteiro>.<inteiro>. Sem default no codigo."""
    if not isinstance(value, str):
        return False
    return bool(_GRAPH_VERSION_RE.fullmatch(value.strip()))


def is_valid_meta_click_cookie(value: Any) -> bool:
    """
    Validacao comum _fbp/_fbc (contrato conservador do projeto).

    Valida EXATAMENTE a string original: sem strip/lstrip/rstrip/replace,
    sem normalizacao Unicode, sem truncamento. Somente str; vazio/whitespace
    invalido; max 1024 bytes UTF-8 do valor original; rejeita NUL, CR/LF,
    controles C0, DEL e C1. Invalido = ausente. Nao loga o valor.
    """
    if not isinstance(value, str):
        return False
    if value == "" or value.isspace():
        return False
    try:
        raw_bytes = value.encode("utf-8")
    except Exception:
        return False
    if len(raw_bytes) > META_COOKIE_MAX_BYTES:
        return False
    for ch in value:
        code = ord(ch)
        if code == 0 or code == 0x7F:
            return False
        if code < 0x20:
            return False
        if 0x80 <= code <= 0x9F:
            return False
    return True


def read_meta_click_cookies_from_request(cookies: Any) -> dict[str, str]:
    """Leitura efemera de _fbp/_fbc da request atual. Invalidos = ausentes."""
    out: dict[str, str] = {}
    if cookies is None:
        return out
    try:
        raw_fbp = cookies.get(META_COOKIE_FBP)
        raw_fbc = cookies.get(META_COOKIE_FBC)
    except Exception:
        return out
    if is_valid_meta_click_cookie(raw_fbp):
        out["fbp"] = raw_fbp
    if is_valid_meta_click_cookie(raw_fbc):
        out["fbc"] = raw_fbc
    return out


def validate_public_base_url_for_capi(raw: Any) -> str | None:
    """
    Origem HTTPS segura para event_source_url.

    Exige HTTPS, host, sem userinfo/query/fragment. Retorna origem sem path
    trailing slash (exceto root), ou None.
    """
    if not isinstance(raw, str):
        return None
    candidate = raw.strip().rstrip("/")
    if not candidate:
        return None
    try:
        parsed = urlparse(candidate)
    except Exception:
        return None
    if parsed.scheme != "https":
        return None
    if not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if parsed.query or parsed.fragment:
        return None
    if parsed.path not in ("", "/"):
        # Origem deve ser so scheme+host[+port]; path extra nao e origem.
        return None
    netloc = parsed.netloc
    if "@" in netloc:
        return None
    return f"https://{netloc}"


def build_canonical_event_source_url(path: str) -> str | None:
    """public_base_url validada + PATH LITERAL allowlisted. Sem request.host."""
    if not isinstance(path, str) or not path.startswith("/"):
        return None
    if any(ch in path for ch in ("?", "#", "\x00", "\r", "\n")):
        return None
    base = validate_public_base_url_for_capi(
        _resolve_str(
            "PUBLIC_BASE_URL",
            settings_attr="public_base_url",
            config_key="PUBLIC_BASE_URL",
        )
        or _settings_attr("public_base_url")
    )
    if base is None:
        return None
    if path == "/":
        return f"{base}/"
    return f"{base}{path}"


def funnel_event_time_unix(occurred_at: Any) -> int | None:
    """
    Unix seconds UTC a partir de FunnelEvent.occurred_at.

    Naive => UTC explicito (contrato do modelo). Aware => converter para UTC.
    None/invalido/nao representavel/futuro => None (SKIP; sem correcao).
    """
    if occurred_at is None or not isinstance(occurred_at, datetime):
        return None
    try:
        if occurred_at.tzinfo is None:
            dt_utc = occurred_at.replace(tzinfo=timezone.utc)
        else:
            dt_utc = occurred_at.astimezone(timezone.utc)
        unix_ts = int(dt_utc.timestamp())
    except (OSError, OverflowError, ValueError, TypeError):
        return None
    now_ts = int(datetime.now(timezone.utc).timestamp())
    if unix_ts > now_ts:
        return None
    if unix_ts < 0:
        return None
    return unix_ts


def _resolve_page_view_path(metadata: Any) -> str | None:
    if not isinstance(metadata, dict):
        return None
    page = metadata.get("page")
    if not isinstance(page, str):
        return None
    return META_CAPI_PAGE_VIEW_PATHS.get(page.strip())


def _resolve_first_relevant_path(metadata: Any) -> str | None:
    if not isinstance(metadata, dict):
        return None
    task_type = metadata.get("task_type")
    if not isinstance(task_type, str):
        return None
    task_n = task_type.strip()
    if task_n == TASK_TYPE_JULIA_CHAT:
        return None
    return META_CAPI_FIRST_RELEVANT_PATHS.get(task_n)


def resolve_meta_capi_event_source_path(event_name: str, metadata: Any) -> str | None:
    """PATH literal allowlisted para event_source_url (sem query/token)."""
    name = str(event_name or "").strip().lower()
    if name == FUNNEL_EVENT_PAGE_VIEW:
        return _resolve_page_view_path(metadata)
    if name == FUNNEL_EVENT_SIGNUP_COMPLETED:
        return "/login"
    if name == FUNNEL_EVENT_CHECKOUT_STARTED:
        return "/contrate-um-plano"
    if name == FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED:
        return _resolve_first_relevant_path(metadata)
    return None


def _is_valid_test_event_code(value: str) -> bool:
    if not value or len(value) > _TEST_EVENT_CODE_MAX_LEN:
        return False
    for ch in value:
        code = ord(ch)
        if code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F:
            return False
    return True


def _raw_meta_capi_test_event_code() -> str | None:
    """
    Valor bruto de META_CAPI_TEST_EVENT_CODE antes de strip/validacao/sanitizacao.

    None = chave ausente; '' = vazia (semantica nao configurada); qualquer outro
    conteudo (incl. whitespace/controles/tamanho excessivo) = configurado/presente.
    """
    if "META_CAPI_TEST_EVENT_CODE" in os.environ:
        return os.environ["META_CAPI_TEST_EVENT_CODE"]
    try:
        from flask import current_app, has_app_context

        if has_app_context() and "META_CAPI_TEST_EVENT_CODE" in current_app.config:
            val = current_app.config.get("META_CAPI_TEST_EVENT_CODE")
            if val is None:
                return None
            if isinstance(val, str):
                return val
            return None
    except Exception:
        pass
    try:
        from app.settings import settings

        val = getattr(settings, "meta_capi_test_event_code", None)
        if val is None:
            return None
        if isinstance(val, str):
            return val
    except Exception:
        pass
    return None


def _is_meta_capi_test_event_code_configured(raw: str | None) -> bool:
    """Presente/configurado: nao ausente e nao string vazia. Whitespace conta."""
    return raw is not None and raw != ""


def _log_capi(
    *,
    growth_event: str,
    meta_event: str | None,
    status: str,
    http_status: int | None,
    timed_out: bool,
    duration_ms: int | None,
    graph_version: str | None,
) -> None:
    # Allowlist estrita: sem token, cookies, payload, URL, IP, UA, body, exc.
    logger.info(
        "growth_meta_capi provider=meta growth_event=%s meta_event=%s status=%s "
        "http_status=%s timeout=%s duration_ms=%s graph_version=%s",
        growth_event or "-",
        meta_event or "-",
        status,
        http_status if http_status is not None else "-",
        bool(timed_out),
        duration_ms if duration_ms is not None else "-",
        graph_version or "-",
    )


def _read_request_consent_and_cookies() -> tuple[str, dict[str, str]]:
    """Consentimento e cookies somente da request original atual."""
    try:
        from flask import has_request_context, request

        if not has_request_context():
            return "unknown", {}
        privacy_state = parse_privacy_marketing_cookie(
            request.cookies.get(PRIVACY_MARKETING_COOKIE_NAME)
        )
        user_data = read_meta_click_cookies_from_request(request.cookies)
        return privacy_state, user_data
    except Exception:
        return "unknown", {}


def build_meta_capi_event_payload(
    *,
    funnel_event: Any,
    user_data: dict[str, str],
    event_source_url: str,
    event_time: int,
    event_id: str,
) -> dict[str, Any] | None:
    """Projecao manual do evento Meta (sem custom_data / SDK)."""
    growth_name = str(getattr(funnel_event, "event_name", "") or "").strip().lower()
    meta_name = META_CAPI_EVENT_NAME_MAP.get(growth_name)
    if not meta_name:
        return None
    if not user_data:
        return None
    payload: dict[str, Any] = {
        "event_name": meta_name,
        "event_time": int(event_time),
        "event_id": event_id,
        "action_source": "website",
        "event_source_url": event_source_url,
        "user_data": dict(user_data),
    }
    return payload


def _post_meta_capi(
    *,
    pixel_id: str,
    graph_version: str,
    access_token: str,
    event_payload: dict[str, Any],
    test_event_code: str | None,
) -> tuple[str, int | None, bool, int]:
    """
    Transporte HTTP unico. Retorna (status, http_status, timed_out, duration_ms).
    Nunca propaga excecao. Sem retry. Sem redirects.
    """
    url = f"https://graph.facebook.com/{graph_version}/{pixel_id}/events"
    form: dict[str, str] = {
        "access_token": access_token,
        "data": json.dumps([event_payload], separators=(",", ":"), ensure_ascii=False),
    }
    if test_event_code:
        form["test_event_code"] = test_event_code

    connect_t, read_t = _resolve_timeouts()
    started = time.monotonic()
    timed_out = False
    http_status: int | None = None
    try:
        resp = requests.post(
            url,
            data=form,
            timeout=(connect_t, read_t),
            allow_redirects=False,
        )
        http_status = int(getattr(resp, "status_code", 0) or 0)
        # Nao ler/logar body.
        duration_ms = int((time.monotonic() - started) * 1000)
        if 200 <= http_status < 300:
            return STATUS_SUCCESS, http_status, False, duration_ms
        return STATUS_HTTP_ERROR, http_status, False, duration_ms
    except requests.Timeout:
        timed_out = True
        duration_ms = int((time.monotonic() - started) * 1000)
        return STATUS_TIMEOUT, None, True, duration_ms
    except requests.RequestException:
        duration_ms = int((time.monotonic() - started) * 1000)
        return STATUS_HTTP_ERROR, http_status, False, duration_ms
    except Exception:
        duration_ms = int((time.monotonic() - started) * 1000)
        return STATUS_INTERNAL_ERROR, http_status, timed_out, duration_ms


def try_send_growth_meta_capi(funnel_event: Any) -> dict[str, Any]:
    """
    Tenta CAPI para um FunnelEvent ja commitado.

    Fail-open absoluto: nunca commit/rollback/flush; nunca edita entidades;
    nunca propaga excecao; nunca altera resposta funcional do caller.
    """
    growth_event = ""
    meta_event: str | None = None
    graph_version: str | None = None
    try:
        if funnel_event is None:
            return _result(attempted=False, success=False, status=STATUS_UNSUPPORTED_EVENT)

        growth_event = str(getattr(funnel_event, "event_name", "") or "").strip().lower()
        if growth_event not in META_CAPI_ALLOWED_GROWTH_EVENTS:
            _log_capi(
                growth_event=growth_event or "unknown",
                meta_event=None,
                status=STATUS_UNSUPPORTED_EVENT,
                http_status=None,
                timed_out=False,
                duration_ms=None,
                graph_version=None,
            )
            return _result(attempted=False, success=False, status=STATUS_UNSUPPORTED_EVENT)

        meta_event = META_CAPI_EVENT_NAME_MAP.get(growth_event)

        if not _resolve_meta_capi_enabled():
            _log_capi(
                growth_event=growth_event,
                meta_event=meta_event,
                status=STATUS_DISABLED,
                http_status=None,
                timed_out=False,
                duration_ms=None,
                graph_version=None,
            )
            return _result(attempted=False, success=False, status=STATUS_DISABLED)

        pixel_id = _resolve_str(
            "FACEBOOK_PIXEL_ID",
            settings_attr="facebook_pixel_id",
            config_key="FACEBOOK_PIXEL_ID",
        )
        access_token = _resolve_str(
            "META_CAPI_ACCESS_TOKEN",
            settings_attr="meta_capi_access_token",
            config_key="META_CAPI_ACCESS_TOKEN",
        )
        graph_version = _resolve_str(
            "META_GRAPH_API_VERSION",
            settings_attr="meta_graph_api_version",
            config_key="META_GRAPH_API_VERSION",
        )
        if (
            not pixel_id
            or not access_token
            or not is_valid_meta_graph_api_version(graph_version)
        ):
            _log_capi(
                growth_event=growth_event,
                meta_event=meta_event,
                status=STATUS_CONFIG_MISSING,
                http_status=None,
                timed_out=False,
                duration_ms=None,
                graph_version=graph_version or None,
            )
            return _result(attempted=False, success=False, status=STATUS_CONFIG_MISSING)

        # Presenca bruta ANTES de strip/validacao/sanitizacao.
        # Em prod, qualquer valor configurado/presente → SKIP (zero HTTP).
        test_code_raw = _raw_meta_capi_test_event_code()
        app_env = _resolve_app_env()
        test_event_code: str | None = None
        if _is_meta_capi_test_event_code_configured(test_code_raw):
            if app_env == "prod":
                # Invalido presente NAO vira "nao configurado": bloqueia envio real.
                _log_capi(
                    growth_event=growth_event,
                    meta_event=meta_event,
                    status=STATUS_TEST_CODE_IN_PROD,
                    http_status=None,
                    timed_out=False,
                    duration_ms=None,
                    graph_version=graph_version,
                )
                return _result(
                    attempted=False, success=False, status=STATUS_TEST_CODE_IN_PROD
                )
            if app_env in ("dev", "homolog"):
                # Somente codigo que passa a validacao existente; nunca enviar invalido.
                if test_code_raw is not None and _is_valid_test_event_code(
                    test_code_raw
                ):
                    test_event_code = test_code_raw

        privacy_state, user_data = _read_request_consent_and_cookies()
        if privacy_state != PRIVACY_MARKETING_STATE_ACCEPTED:
            _log_capi(
                growth_event=growth_event,
                meta_event=meta_event,
                status=STATUS_CONSENT_NOT_ACCEPTED,
                http_status=None,
                timed_out=False,
                duration_ms=None,
                graph_version=graph_version,
            )
            return _result(
                attempted=False, success=False, status=STATUS_CONSENT_NOT_ACCEPTED
            )

        if not user_data:
            _log_capi(
                growth_event=growth_event,
                meta_event=meta_event,
                status=STATUS_USER_DATA_MISSING,
                http_status=None,
                timed_out=False,
                duration_ms=None,
                graph_version=graph_version,
            )
            return _result(
                attempted=False, success=False, status=STATUS_USER_DATA_MISSING
            )

        path = resolve_meta_capi_event_source_path(
            growth_event, getattr(funnel_event, "metadata_json", None)
        )
        event_source_url = build_canonical_event_source_url(path) if path else None
        if not event_source_url:
            _log_capi(
                growth_event=growth_event,
                meta_event=meta_event,
                status=STATUS_URL_UNAVAILABLE,
                http_status=None,
                timed_out=False,
                duration_ms=None,
                graph_version=graph_version,
            )
            return _result(
                attempted=False, success=False, status=STATUS_URL_UNAVAILABLE
            )

        event_time = funnel_event_time_unix(getattr(funnel_event, "occurred_at", None))
        if event_time is None:
            _log_capi(
                growth_event=growth_event,
                meta_event=meta_event,
                status=STATUS_EVENT_TIME_INVALID,
                http_status=None,
                timed_out=False,
                duration_ms=None,
                graph_version=graph_version,
            )
            return _result(
                attempted=False, success=False, status=STATUS_EVENT_TIME_INVALID
            )

        event_pk = getattr(funnel_event, "id", None)
        if event_pk is None:
            return _result(attempted=False, success=False, status=STATUS_UNSUPPORTED_EVENT)
        try:
            event_id = build_external_event_token(
                event_name=growth_event,
                funnel_event_id=int(event_pk),
            )
        except Exception:
            _log_capi(
                growth_event=growth_event,
                meta_event=meta_event,
                status=STATUS_INTERNAL_ERROR,
                http_status=None,
                timed_out=False,
                duration_ms=None,
                graph_version=graph_version,
            )
            return _result(attempted=False, success=False, status=STATUS_INTERNAL_ERROR)

        event_payload = build_meta_capi_event_payload(
            funnel_event=funnel_event,
            user_data=user_data,
            event_source_url=event_source_url,
            event_time=event_time,
            event_id=event_id,
        )
        if event_payload is None:
            return _result(
                attempted=False, success=False, status=STATUS_USER_DATA_MISSING
            )

        status, http_status, timed_out, duration_ms = _post_meta_capi(
            pixel_id=pixel_id,
            graph_version=graph_version,
            access_token=access_token,
            event_payload=event_payload,
            test_event_code=test_event_code,
        )
        _log_capi(
            growth_event=growth_event,
            meta_event=meta_event,
            status=status,
            http_status=http_status,
            timed_out=timed_out,
            duration_ms=duration_ms,
            graph_version=graph_version,
        )
        return _result(
            attempted=True,
            success=(status == STATUS_SUCCESS),
            status=status,
            http_status=http_status,
        )
    except Exception:
        _log_capi(
            growth_event=growth_event or "unknown",
            meta_event=meta_event,
            status=STATUS_INTERNAL_ERROR,
            http_status=None,
            timed_out=False,
            duration_ms=None,
            graph_version=graph_version,
        )
        return _result(attempted=False, success=False, status=STATUS_INTERNAL_ERROR)


def try_send_growth_meta_capi_if_new(record_result: Any) -> dict[str, Any]:
    """
    CAPI somente apos FunnelEvent novo (created=True) ja commitado.

    Replay/falha Growth => nenhum HTTP. Fail-open.
    """
    try:
        if not isinstance(record_result, dict):
            return _result(attempted=False, success=False, status=STATUS_NOT_NEW)
        if record_result.get("created") is not True:
            return _result(attempted=False, success=False, status=STATUS_NOT_NEW)
        return try_send_growth_meta_capi(record_result.get("event"))
    except Exception:
        return _result(attempted=False, success=False, status=STATUS_INTERNAL_ERROR)
