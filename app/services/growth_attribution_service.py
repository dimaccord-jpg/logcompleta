"""Atribuicao first-party por UTM + click IDs (SCRUM-159 Lotes 159A/159C).

Preserva first_touch e current_session_origin na sessao Flask.
Click IDs (gclid/gbraid/wbraid/fbclid) somente com consentimento accepted.
Sem visitor ID, sem fingerprint, sem inventar direct/source.
Fail-open: falha de atribuicao nao afeta a resposta funcional.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from flask import has_request_context, request, session
from flask_login import current_user

from app.privacy_marketing import (
    PRIVACY_MARKETING_COOKIE_NAME,
    PRIVACY_MARKETING_STATE_ACCEPTED,
    PRIVACY_MARKETING_STATE_UNKNOWN,
    parse_privacy_marketing_cookie,
)

logger = logging.getLogger(__name__)

SESSION_KEY = "growth_attribution"
ATTRIBUTION_VERSION = 1

# Query allowlist → campo interno. utm_term fica de fora.
UTM_QUERY_TO_FIELD: dict[str, str] = {
    "utm_source": "source",
    "utm_medium": "medium",
    "utm_campaign": "campaign",
    "utm_id": "campaign_id",
    "utm_content": "content",
}

FIELD_LIMITS: dict[str, int] = {
    "source": 50,
    "medium": 50,
    "campaign": 80,
    "campaign_id": 120,
    "content": 120,
}

LANDING_PAGE_LIMIT = 120
CLICK_ID_LIMIT = 512

CLICK_ID_PARAMS = frozenset({"gclid", "gbraid", "wbraid", "fbclid"})
# Alias historico 159A (testes / imports).
DEFERRED_CLICK_ID_PARAMS = CLICK_ID_PARAMS

UTM_ACQUISITION_KEYS = frozenset(
    {"source", "medium", "campaign", "campaign_id", "content"}
)
ACQUISITION_KEYS = UTM_ACQUISITION_KEYS | CLICK_ID_PARAMS
ORIGIN_ALLOWED_KEYS = frozenset(
    {"source", "medium", "campaign", "campaign_id", "content", "landing_page"}
    | CLICK_ID_PARAMS
)

_ENDPOINT_RE = re.compile(r"^[\w.-]{1,120}$")

_EXCLUDED_PATH_PREFIXES = (
    "/api/",
    "/health/",
    "/cron/",
    "/ops/",
    "/static/",
    "/media/",
    "/webhook",
    "/stripe",
    "/admin/",
)

_EXCLUDED_ENDPOINTS = frozenset(
    {
        "static",
        "logout",
        "login_google",
        "google_callback",
        "request_password_reset",
        "reset_password",
        "admin_promocao_confirmar",
        "admin_revogacao_confirmar",
        "robots_txt",
        "sitemap_xml",
        "acesso_desktop_continuar",
        "acesso_desktop_descadastrar",
        "newsletter_cancelar",
    }
)


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def _looks_like_url(value: str) -> bool:
    lowered = value.lower()
    if lowered.startswith(("http://", "https://", "ftp://", "//")):
        return True
    if "://" in value:
        return True
    return False


# Esquemas URI explicitamente bloqueados em click IDs (alem do padrao generico).
_CLICK_ID_BLOCKED_SCHEME_PREFIXES = (
    "http:",
    "https:",
    "javascript:",
    "data:",
    "vbscript:",
    "file:",
    "blob:",
)

# RFC 3986 scheme: ALPHA *( ALPHA / DIGIT / "+" / "-" / "." ) ":"
_URI_SCHEME_PREFIX_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def _looks_like_uri_scheme(value: str) -> bool:
    """Detecta aparencia de URI/esquema em click IDs (nao depende de '://')."""
    lowered = value.lower()
    if lowered.startswith(_CLICK_ID_BLOCKED_SCHEME_PREFIXES):
        return True
    if _URI_SCHEME_PREFIX_RE.match(value) is not None:
        return True
    # Protocol-relative e URLs com '://' (cobertura complementar).
    if _looks_like_url(value):
        return True
    return False


def _looks_like_html_or_markup(value: str) -> bool:
    return "<" in value or ">" in value


def _looks_like_structural_payload(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False
    if stripped[0] in "{[(" or stripped[-1] in "})]":
        return True
    if '":' in stripped or "':" in stripped:
        return True
    if "&" in stripped and "=" in stripped:
        return True
    return False


def get_request_privacy_marketing_state() -> str:
    """Estado atual do cookie af_privacy_marketing (unica fonte de verdade)."""
    if not has_request_context():
        return PRIVACY_MARKETING_STATE_UNKNOWN
    cookie_name = PRIVACY_MARKETING_COOKIE_NAME
    try:
        from flask import current_app

        cookie_name = current_app.config.get(
            "PRIVACY_MARKETING_COOKIE_NAME", PRIVACY_MARKETING_COOKIE_NAME
        )
    except RuntimeError:
        pass
    return parse_privacy_marketing_cookie(request.cookies.get(cookie_name))


def is_click_id_capture_allowed(state: str | None = None) -> bool:
    current = state if state is not None else get_request_privacy_marketing_state()
    return current == PRIVACY_MARKETING_STATE_ACCEPTED


def validate_attribution_field(field: str, raw: Any) -> str | None:
    """Valida um campo de aquisicao UTM. Descarta o campo se invalido (nao trunca)."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if _has_control_chars(text):
        return None
    if _looks_like_url(text):
        return None
    # Conservador: qualquer '@' indica email/PII evidente — descarta o campo inteiro.
    if "@" in text:
        return None
    limit = FIELD_LIMITS.get(field)
    if limit is not None and len(text) > limit:
        return None
    return text


def validate_click_id(raw: Any) -> str | None:
    """Validacao dedicada aos quatro click IDs. Nao interpreta semanticamente."""
    if not isinstance(raw, str):
        return None
    # Controles no valor ORIGINAL, antes do strip.
    if _has_control_chars(raw):
        return None
    text = raw.strip()
    if not text:
        return None
    if len(text) > CLICK_ID_LIMIT:
        return None
    if _looks_like_uri_scheme(text):
        return None
    if "@" in text:
        return None
    if _looks_like_html_or_markup(text):
        return None
    if _looks_like_structural_payload(text):
        return None
    return text


def validate_landing_page(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if _has_control_chars(text):
        return None
    if _looks_like_url(text):
        return None
    if len(text) > LANDING_PAGE_LIMIT:
        return None
    if not _ENDPOINT_RE.fullmatch(text):
        return None
    return text


def validate_origin_object(raw: Any) -> dict[str, str] | None:
    """Valida um objeto de origem (first_touch / current_session_origin)."""
    if not isinstance(raw, dict):
        return None
    unknown = set(raw.keys()) - ORIGIN_ALLOWED_KEYS
    if unknown:
        return None
    out: dict[str, str] = {}
    for key, value in raw.items():
        if key == "landing_page":
            validated = validate_landing_page(value)
        elif key in CLICK_ID_PARAMS:
            validated = validate_click_id(value)
        else:
            validated = validate_attribution_field(key, value)
        if validated is None:
            continue
        out[key] = validated
    if not (set(out.keys()) & ACQUISITION_KEYS):
        return None
    return out


def _strip_click_ids_from_origin(origin: Any) -> dict[str, str] | None:
    """Remove click IDs; landing isolada nao representa aquisicao."""
    if not isinstance(origin, dict):
        return None
    cleaned = {k: v for k, v in origin.items() if k not in CLICK_ID_PARAMS}
    if not (set(cleaned.keys()) & UTM_ACQUISITION_KEYS):
        return None
    # Revalida estrutura apos limpeza (preserva apenas campos validos).
    return validate_origin_object(cleaned)


def purge_click_ids_from_session_attribution() -> None:
    """Remove click IDs residuais de first_touch/current na sessao atual."""
    if not has_request_context():
        return
    existing = get_session_growth_attribution()
    if existing is None:
        return

    changed = False
    for key in ("first_touch", "current_session_origin"):
        origin = existing.get(key)
        if not isinstance(origin, dict):
            continue
        if not (set(origin.keys()) & CLICK_ID_PARAMS):
            continue
        cleaned = _strip_click_ids_from_origin(origin)
        if cleaned is None:
            existing.pop(key, None)
        else:
            existing[key] = cleaned
        changed = True

    if not changed:
        return

    has_first = isinstance(existing.get("first_touch"), dict)
    has_current = isinstance(existing.get("current_session_origin"), dict)
    if not has_first and not has_current:
        session.pop(SESSION_KEY, None)
        return
    session[SESSION_KEY] = existing


def strip_click_ids_from_relative_url(url: str) -> str:
    """Remove gclid/gbraid/wbraid/fbclid da query de um caminho relativo."""
    raw = (url or "").strip()
    if not raw:
        return raw
    parsed = urlsplit(raw)
    if not parsed.query:
        return raw
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    filtered = [(k, v) for k, v in pairs if k not in CLICK_ID_PARAMS]
    if len(filtered) == len(pairs):
        return raw
    new_query = urlencode(filtered)
    path = parsed.path or ""
    if new_query:
        return f"{path}?{new_query}"
    return path


def sanitize_next_url_for_consent(url: str, *, state: str | None = None) -> str:
    """Se consentimento != accepted, remove click IDs da URL relativa."""
    if is_click_id_capture_allowed(state):
        return url
    return strip_click_ids_from_relative_url(url)


def sanitize_post_login_next_in_session(*, state: str | None = None) -> None:
    """Limpa click IDs de post_login_next ja existente (ex.: decision rejected)."""
    if not has_request_context():
        return
    raw = session.get("post_login_next")
    if not isinstance(raw, str) or not raw.strip():
        return
    if is_click_id_capture_allowed(state):
        return
    cleaned = strip_click_ids_from_relative_url(raw)
    if cleaned != raw:
        session["post_login_next"] = cleaned


def sanitize_growth_attribution_snapshot(
    raw: Any,
    *,
    require_current_only: bool = False,
) -> dict[str, dict[str, str]] | None:
    """Sanitiza snapshot para FunnelEvent metadata. Sem chaves extras.

    Validacao estrutural: NAO depende do cookie atual (fatos historicos com
    click IDs legitimamente capturados sob accepted devem continuar validos).
    """
    if not isinstance(raw, dict):
        return None
    allowed_top = {"first_touch", "current_session_origin"}
    if set(raw.keys()) - allowed_top:
        return None
    if require_current_only:
        if "first_touch" in raw:
            return None
        if "current_session_origin" not in raw:
            return None

    out: dict[str, dict[str, str]] = {}
    for key in ("first_touch", "current_session_origin"):
        if key not in raw:
            continue
        origin = validate_origin_object(raw[key])
        if origin is None:
            return None
        out[key] = origin

    if not out:
        return None
    if require_current_only and set(out.keys()) != {"current_session_origin"}:
        return None
    return out


def _omit_click_ids_from_snapshot_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Defesa em profundidade: omite click IDs de um payload de snapshot."""
    out: dict[str, Any] = {}
    for key, origin in payload.items():
        if not isinstance(origin, dict):
            continue
        cleaned = {k: v for k, v in origin.items() if k not in CLICK_ID_PARAMS}
        # Landing isolada nao e aquisicao — omite a origem inteira.
        if not (set(cleaned.keys()) & UTM_ACQUISITION_KEYS):
            continue
        out[key] = cleaned
    return out


def _endpoint_base(endpoint: str) -> str:
    return endpoint.rsplit(".", 1)[-1]


def is_eligible_attribution_request() -> bool:
    if not has_request_context():
        return False
    if request.method != "GET":
        return False
    endpoint = request.endpoint
    if not isinstance(endpoint, str) or not endpoint.strip():
        return False
    if endpoint == "static" or _endpoint_base(endpoint) == "static":
        return False
    path = request.path or ""
    for prefix in _EXCLUDED_PATH_PREFIXES:
        if path.startswith(prefix):
            return False
    if endpoint in _EXCLUDED_ENDPOINTS or _endpoint_base(endpoint) in _EXCLUDED_ENDPOINTS:
        return False
    # Rotas de token / reset / oauth callback por padrao de path.
    lowered = path.lower()
    if "/reset-password/" in lowered or lowered.startswith("/reset-password/"):
        return False
    if "/login/google" in lowered:
        return False
    if "/callback" in lowered:
        return False
    return True


def extract_acquisition_from_request() -> dict[str, str] | None:
    """Extrai origem UTM (+ click IDs se accepted) do request. Sem aquisicao → None."""
    if not has_request_context():
        return None
    fields: dict[str, str] = {}
    args = request.args
    for query_key, field in UTM_QUERY_TO_FIELD.items():
        if query_key not in args:
            continue
        raw = args.get(query_key)
        validated = validate_attribution_field(field, raw)
        if validated is not None:
            fields[field] = validated

    # UTMs independentes do consentimento; click IDs somente accepted.
    if is_click_id_capture_allowed():
        for param in sorted(CLICK_ID_PARAMS):
            if param not in args:
                continue
            values = args.getlist(param)
            # Parametro repetido → rejeitar o click ID inteiro (nao escolher um).
            if len(values) != 1:
                continue
            validated = validate_click_id(values[0])
            if validated is not None:
                fields[param] = validated

    if not fields:
        return None

    landing = validate_landing_page(request.endpoint)
    if landing is not None:
        fields["landing_page"] = landing
    return fields


def get_session_growth_attribution() -> dict[str, Any] | None:
    raw = session.get(SESSION_KEY)
    if not isinstance(raw, dict):
        return None
    if raw.get("version") != ATTRIBUTION_VERSION:
        return None
    return raw


def snapshot_growth_attribution_for_persist(
    *,
    include_first_touch: bool = True,
    include_current: bool = True,
) -> dict[str, dict[str, str]] | None:
    """Snapshot estrito a partir da sessao (nunca do browser).

    Click IDs so entram em NOVOS snapshots quando o consentimento atual e accepted.
    """
    if not has_request_context():
        return None
    ctx = get_session_growth_attribution()
    if ctx is None:
        return None
    payload: dict[str, Any] = {}
    if include_first_touch and "first_touch" in ctx:
        payload["first_touch"] = ctx["first_touch"]
    if include_current and "current_session_origin" in ctx:
        payload["current_session_origin"] = ctx["current_session_origin"]
    if not payload:
        return None
    if not is_click_id_capture_allowed():
        payload = _omit_click_ids_from_snapshot_payload(payload)
    return sanitize_growth_attribution_snapshot(
        payload,
        require_current_only=include_current and not include_first_touch,
    )


def snapshot_current_session_origin_for_persist() -> dict[str, dict[str, str]] | None:
    """
    Snapshot opcional so com current_session_origin (Lote 159B).

    Fail-open: sem request, sem origem valida ou erro de sanitizacao => None.
    Nunca inclui first_touch. Nunca le payload do browser.
    """
    try:
        snap = snapshot_growth_attribution_for_persist(
            include_first_touch=False,
            include_current=True,
        )
        if not snap or "current_session_origin" not in snap:
            return None
        if "first_touch" in snap:
            return None
        return snap
    except Exception:
        logger.debug("snapshot current_session_origin indisponivel", exc_info=True)
        return None


def apply_acquisition_to_session(origin: dict[str, str]) -> bool:
    """
    Atualiza sessao com aquisicao valida.

    Retorna True se current_session_origin foi criado/substituido.
    first_touch so e criado na primeira observacao; nunca sobrescrito.
    """
    normalized = validate_origin_object(origin)
    if normalized is None:
        return False

    existing = get_session_growth_attribution()
    if existing is None:
        session[SESSION_KEY] = {
            "version": ATTRIBUTION_VERSION,
            "first_touch": dict(normalized),
            "current_session_origin": dict(normalized),
        }
        return True

    current = existing.get("current_session_origin")
    if isinstance(current, dict) and current == normalized:
        return False

    # Substitui o conjunto inteiro; nao mescla campos de campanhas distintas.
    existing["current_session_origin"] = dict(normalized)
    # first_touch permanece intacto (mesmo se ausente no existente, nao inventa).
    if "first_touch" not in existing or not isinstance(existing.get("first_touch"), dict):
        existing["first_touch"] = dict(normalized)
    session[SESSION_KEY] = existing
    return True


def apply_growth_attribution_before_request() -> None:
    """Hook before_request: captura UTM/click IDs em paginas elegiveis (GET). Fail-open."""
    try:
        if not is_eligible_attribution_request():
            return
        # Expiraçao/unknown/rejected: limpar IDs residuais antes de nova aquisicao.
        if not is_click_id_capture_allowed():
            purge_click_ids_from_session_attribution()
        origin = extract_acquisition_from_request()
        if origin is None:
            return
        changed = apply_acquisition_to_session(origin)
        if not changed:
            return
        if not getattr(current_user, "is_authenticated", False):
            return
        from app.funnel_event_service import try_record_session_origin_observed

        try_record_session_origin_observed(current_user)
    except Exception:
        # Log tecnico sem valores UTM/click ID.
        logger.exception("growth attribution before_request failed")
