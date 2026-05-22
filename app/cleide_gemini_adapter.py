from __future__ import annotations

import logging
import os
import time
from typing import Any

from app.cleide_ai_flags import resolve_cleide_ai_flags, resolve_cleide_gemini_api_key
from app.cleide_ai_prompt import build_cleide_ai_contents
from app.run_cleiton_gemini_governance import cleiton_governed_generate_content

logger = logging.getLogger(__name__)

FLOW_TYPE_CLEIDE_AI = "cleide_chat_auditoria_frete_ai"
AGENT_CLEIDE = "cleide"


def _get_client(api_key: str):
    from google import genai
    from google.genai import types as genai_types

    timeout_ms = 30_000
    raw = (os.getenv("GEMINI_HTTP_TIMEOUT_MS") or "").strip()
    if raw:
        try:
            timeout_ms = max(1_000, int(raw))
        except ValueError:
            pass
    return genai.Client(
        api_key=api_key,
        http_options=genai_types.HttpOptions(timeout=timeout_ms),
    )


def _model_candidates() -> list[str]:
    candidates = [
        (os.getenv("CLEIDE_GEMINI_MODEL") or "").strip(),
        (os.getenv("GEMINI_MODEL_TEXT") or "").strip(),
        "gemini-2.5-flash",
        "gemini-1.5-flash",
    ]
    out: list[str] = []
    seen: set[str] = set()
    for model in candidates:
        if model and model not in seen:
            seen.add(model)
            out.append(model)
    return out


def _extract_usage(response: Any) -> dict[str, int | None]:
    um = getattr(response, "usage_metadata", None)
    if um is None:
        return {"input_tokens": None, "output_tokens": None, "total_tokens": None}
    try:
        inp = int(getattr(um, "prompt_token_count", None))
    except (TypeError, ValueError):
        inp = None
    try:
        out = int(getattr(um, "candidates_token_count", None))
    except (TypeError, ValueError):
        out = None
    try:
        total = int(getattr(um, "total_token_count", None))
    except (TypeError, ValueError):
        total = None
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "total_tokens": total,
    }


def _context_is_safe_for_ai(safe_operational_context: dict[str, Any]) -> tuple[bool, str]:
    if not isinstance(safe_operational_context, dict) or not safe_operational_context:
        return False, "missing_safe_operational_context"
    guards = safe_operational_context.get("security_guards")
    if not isinstance(guards, dict):
        return False, "missing_security_guards"
    if bool(guards.get("contains_raw_dataset")):
        return False, "unsafe_context_raw_dataset"
    if bool(guards.get("contains_full_rows")):
        return False, "unsafe_context_full_rows"
    if bool(guards.get("contains_roberto_payload")):
        return False, "unsafe_context_roberto_payload"
    return True, ""


def generate_cleide_ai_reply(
    *,
    question: str,
    safe_operational_context: dict[str, Any],
    history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """
    Adapter Gemini da Cleide.
    Envia somente safe_operational_context, com governança Cleiton e sem qualquer payload Roberto.
    """
    t0 = time.perf_counter()
    flags = resolve_cleide_ai_flags()
    if not flags.ai_enabled:
        return {
            "ok": False,
            "error_code": "ai_disabled",
            "reason": flags.reason,
            "provider": "gemini",
            "model": "",
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "usage": {"input_tokens": None, "output_tokens": None, "total_tokens": None},
        }

    safe_ctx_ok, safe_reason = _context_is_safe_for_ai(safe_operational_context)
    if not safe_ctx_ok:
        return {
            "ok": False,
            "error_code": "unsafe_context",
            "reason": safe_reason,
            "provider": "gemini",
            "model": "",
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "usage": {"input_tokens": None, "output_tokens": None, "total_tokens": None},
        }

    key, key_label = resolve_cleide_gemini_api_key()
    if not key:
        return {
            "ok": False,
            "error_code": "missing_key",
            "reason": "missing_api_key",
            "provider": "gemini",
            "model": "",
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "usage": {"input_tokens": None, "output_tokens": None, "total_tokens": None},
        }

    try:
        client = _get_client(key)
    except Exception as exc:
        logger.warning("Cleide Gemini adapter: falha ao criar client: %s", exc)
        return {
            "ok": False,
            "error_code": "provider_init_error",
            "reason": "client_init_failed",
            "provider": "gemini",
            "model": "",
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "usage": {"input_tokens": None, "output_tokens": None, "total_tokens": None},
        }

    contents = build_cleide_ai_contents(
        question=question,
        safe_operational_context=safe_operational_context,
        history=history or [],
    )
    last_error: Exception | None = None
    for model in _model_candidates():
        try:
            response = cleiton_governed_generate_content(
                client,
                model=model,
                contents=contents,
                agent=AGENT_CLEIDE,
                flow_type=FLOW_TYPE_CLEIDE_AI,
                api_key_label=key_label,
            )
            text = str(getattr(response, "text", "") or "").strip()
            usage = _extract_usage(response)
            if not text:
                return {
                    "ok": False,
                    "error_code": "empty_response",
                    "reason": "empty_response",
                    "provider": "gemini",
                    "model": model,
                    "latency_ms": int((time.perf_counter() - t0) * 1000),
                    "usage": usage,
                }
            return {
                "ok": True,
                "reply": text,
                "provider": "gemini",
                "model": model,
                "latency_ms": int((time.perf_counter() - t0) * 1000),
                "usage": usage,
                "error_code": "",
                "reason": "ok",
            }
        except Exception as exc:
            last_error = exc
            logger.warning("Cleide Gemini adapter: falha no modelo %s: %s", model, exc)

    return {
        "ok": False,
        "error_code": "provider_error",
        "reason": str(last_error or "provider_error"),
        "provider": "gemini",
        "model": "",
        "latency_ms": int((time.perf_counter() - t0) * 1000),
        "usage": {"input_tokens": None, "output_tokens": None, "total_tokens": None},
    }
