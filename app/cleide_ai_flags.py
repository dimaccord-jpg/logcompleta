from __future__ import annotations

import os
from dataclasses import dataclass

CLEIDE_GEMINI_API_KEY_FALLBACK_ORDER: tuple[str, ...] = (
    "GEMINI_API_KEY_ROBERTO",
    "GEMINI_API_KEY",
    "GEMINI_API_KEY_1",
    "GEMINI_API_KEY_2",
    "CLEIDE_GEMINI_API_KEY",
    "GEMINI_API_KEY_CLEIDE",
)


@dataclass(frozen=True)
class CleideAiFlagResolution:
    ai_enabled: bool
    environment: str
    selected_flag: str
    reason: str
    has_api_key: bool
    api_key_label: str


def _detect_environment() -> str:
    raw = (os.getenv("APP_ENV") or "dev").strip().lower()
    if raw == "prod":
        return "prod"
    if raw == "homolog":
        return "homolog"
    return "local"


def _parse_bool_strict(raw: str | None) -> bool | None:
    if raw is None:
        return None
    val = str(raw).strip().lower()
    if val in {"1", "true", "yes", "on"}:
        return True
    if val in {"0", "false", "no", "off"}:
        return False
    return None


def resolve_cleide_gemini_api_key() -> tuple[str, str]:
    for label in CLEIDE_GEMINI_API_KEY_FALLBACK_ORDER:
        value = (os.getenv(label) or "").strip()
        if value:
            return value, label
    return "", ""


def resolve_cleide_ai_flags() -> CleideAiFlagResolution:
    try:
        env = _detect_environment()
        specific_map = {
            "local": "CLEIDE_AI_ENABLED_LOCAL",
            "homolog": "CLEIDE_AI_ENABLED_HOMOLOG",
            "prod": "CLEIDE_AI_ENABLED_PROD",
        }
        specific_flag = specific_map[env]
        raw_specific = os.getenv(specific_flag)
        raw_global = os.getenv("CLEIDE_AI_ENABLED")
        key, key_label = resolve_cleide_gemini_api_key()

        parsed_specific = _parse_bool_strict(raw_specific)
        parsed_global = _parse_bool_strict(raw_global)

        if raw_specific is not None and parsed_specific is None:
            return CleideAiFlagResolution(
                ai_enabled=False,
                environment=env,
                selected_flag=specific_flag,
                reason="invalid_specific_flag",
                has_api_key=bool(key),
                api_key_label=key_label,
            )
        if raw_specific is not None:
            enabled = bool(parsed_specific)
            reason = "specific_flag"
        elif raw_global is not None and parsed_global is None:
            return CleideAiFlagResolution(
                ai_enabled=False,
                environment=env,
                selected_flag="CLEIDE_AI_ENABLED",
                reason="invalid_global_flag",
                has_api_key=bool(key),
                api_key_label=key_label,
            )
        elif raw_global is not None:
            enabled = bool(parsed_global)
            reason = "global_flag_fallback"
        else:
            enabled = False
            reason = "flags_absent_default_false"

        if not enabled:
            return CleideAiFlagResolution(
                ai_enabled=False,
                environment=env,
                selected_flag=specific_flag if raw_specific is not None else "CLEIDE_AI_ENABLED",
                reason=reason,
                has_api_key=bool(key),
                api_key_label=key_label,
            )

        if not key:
            return CleideAiFlagResolution(
                ai_enabled=False,
                environment=env,
                selected_flag=specific_flag if raw_specific is not None else "CLEIDE_AI_ENABLED",
                reason="missing_api_key",
                has_api_key=False,
                api_key_label="",
            )

        return CleideAiFlagResolution(
            ai_enabled=True,
            environment=env,
            selected_flag=specific_flag if raw_specific is not None else "CLEIDE_AI_ENABLED",
            reason=reason,
            has_api_key=True,
            api_key_label=key_label,
        )
    except Exception:
        return CleideAiFlagResolution(
            ai_enabled=False,
            environment="local",
            selected_flag="CLEIDE_AI_ENABLED",
            reason="resolution_error",
            has_api_key=False,
            api_key_label="",
        )
