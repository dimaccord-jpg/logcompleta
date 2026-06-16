"""
Backend do chat documental da Cleide Auditoria (Fase 2 — endpoint backend).

Comunicação com LLM exclusivamente via governança Cleiton.
Sem regex, parser rígido ou acoplamento com Júlia/BI Cleide.
"""
from __future__ import annotations

import logging
import os
from typing import Any
from uuid import uuid4

from flask import has_app_context

from app.cleide_audit_doc_service import CLEIDE_AUDIT_CHAT_FLOW_TYPE, cleide_audit_chat_idempotency_key
from app.cleide_audit_prompt import build_cleide_audit_system_prompt
from app.run_cleiton_gemini_governance import cleiton_governed_generate_content
from app.services.cleide_audit_config_service import get_cleide_audit_config

logger = logging.getLogger(__name__)

CHAT_IDEMPOTENCY_CACHE_SESSION_KEY = "cleide_audit_chat_idempotency_cache"
DEFAULT_CLEIDE_AUDIT_CHAT_MAX_HISTORY = 10
DEFAULT_MODEL_FALLBACK = "gemini-2.5-flash-lite"
MAX_MESSAGE_CHARS = 12_000
MAX_HISTORY_ITEM_CHARS = 8_000
SERVICE_UNAVAILABLE_MESSAGE = (
    "Assistente temporariamente indisponível. Verifique a configuração do serviço."
)
PROCESSING_ERROR_MESSAGE = (
    "Não foi possível processar sua mensagem no momento. Tente novamente em instantes."
)
EXTRA_ANTI_HALLUCINATION_INSTRUCTION = (
    "\n\nReforço adicional: cite apenas evidências presentes nos documentos e no histórico; "
    "não invente valores, documentos ou conclusões."
)


def _get_max_history(*, max_history: int | None = None) -> int:
    if max_history is not None:
        return max(0, int(max_history))
    if not has_app_context():
        return DEFAULT_CLEIDE_AUDIT_CHAT_MAX_HISTORY
    return max(0, int(get_cleide_audit_config().chat_max_history))


def _api_key_label() -> str:
    if os.getenv("GEMINI_API_KEY_1"):
        return "GEMINI_API_KEY_1"
    if os.getenv("GEMINI_API_KEY"):
        return "GEMINI_API_KEY"
    return "unknown"


def _get_client():
    key = os.getenv("GEMINI_API_KEY_1") or os.getenv("GEMINI_API_KEY")
    if not key:
        return None
    try:
        from google import genai
        from google.genai import types as genai_types

        timeout_ms = 30_000
        raw = (os.getenv("GEMINI_HTTP_TIMEOUT_MS") or "").strip()
        if raw:
            try:
                timeout_ms = max(1_000, int(raw))
            except ValueError:
                pass
        return genai.Client(api_key=key, http_options=genai_types.HttpOptions(timeout=timeout_ms))
    except Exception as exc:
        logger.error("Falha ao inicializar cliente Gemini para Cleide Auditoria: %s", exc)
        return None


def _get_model_candidates() -> list[str]:
    candidates = [
        (os.getenv("GEMINI_MODEL_TEXT") or "").strip(),
        "gemini-2.5-flash",
        (os.getenv("CLEIDE_AUDIT_CHAT_MODEL_FALLBACK") or "").strip(),
        DEFAULT_MODEL_FALLBACK,
    ]
    seen: set[str] = set()
    out: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


def normalize_chat_request_id(raw: Any) -> str:
    """Normaliza request_id opaco; tipos inválidos recebem novo identificador seguro."""
    if not isinstance(raw, str):
        return uuid4().hex
    ref = raw.strip()
    return ref or uuid4().hex


def sanitize_chat_history(history: Any, *, max_history: int | None = None) -> list[dict]:
    """Normaliza histórico recebido do cliente sem regex; ignora itens malformados."""
    if not isinstance(history, list):
        return []
    limit = _get_max_history(max_history=max_history)
    if limit <= 0:
        return []

    cleaned: list[dict] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        raw_role = item.get("role")
        if not isinstance(raw_role, str):
            continue
        role = raw_role.strip().lower()
        if role == "assistant":
            role = "model"
        elif role == "model":
            role = "model"
        elif role != "user":
            continue
        raw_content = item.get("content")
        if not isinstance(raw_content, str):
            continue
        content = raw_content.strip()
        if not content:
            continue
        cleaned.append(
            {
                "role": role,
                "content": content[:MAX_HISTORY_ITEM_CHARS],
            }
        )
    return cleaned[-limit:]


def _documents_used_from_meta(documents_meta: list | None) -> list[dict]:
    used: list[dict] = []
    for item in documents_meta or []:
        if not isinstance(item, dict):
            continue
        used.append(
            {
                "display_name": (item.get("display_name") or "documento")[:120],
                "doc_type": (item.get("doc_type") or "")[:20],
            }
        )
    return used


def _build_contents(
    history_slice: list[dict],
    user_message: str,
    *,
    document_context_block: str | None = None,
    document_file_parts: list | None = None,
    no_hallucination_instruction_enabled: bool = True,
) -> str | list:
    system_prompt = build_cleide_audit_system_prompt().strip()
    if no_hallucination_instruction_enabled:
        system_prompt += EXTRA_ANTI_HALLUCINATION_INSTRUCTION
    parts = [system_prompt, "\n\n---\n\n"]
    doc_block = (document_context_block or "").strip()
    if doc_block:
        parts.append(doc_block)
        parts.append("\n\n---\n\n")
    parts.append("Conversa recente:\n")
    for msg in history_slice:
        role = (msg.get("role") or "user").lower()
        label = "Usuário" if role == "user" else "Cleide"
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        parts.append(f"{label}: {content}\n\n")
    parts.append(f"Usuário: {user_message.strip()}\n\nCleide:")
    prompt_text = "".join(parts)
    file_parts = [part for part in (document_file_parts or []) if part is not None]
    if file_parts:
        return file_parts + [prompt_text]
    return prompt_text


def get_cached_chat_response(session_obj, request_id: str) -> dict | None:
    """
    Retorna resposta cacheada por request_id na sessão Flask.

    Idempotência limitada à sessão atual (sem persistência em banco nesta fase).
    """
    ref = (request_id or "").strip()
    if not ref:
        return None
    cache = session_obj.get(CHAT_IDEMPOTENCY_CACHE_SESSION_KEY)
    if not isinstance(cache, dict):
        return None
    payload = cache.get(cleide_audit_chat_idempotency_key(ref))
    return payload if isinstance(payload, dict) else None


def cache_chat_response(session_obj, request_id: str, payload: dict) -> None:
    ref = (request_id or "").strip()
    if not ref or not isinstance(payload, dict):
        return
    cache = session_obj.get(CHAT_IDEMPOTENCY_CACHE_SESSION_KEY)
    if not isinstance(cache, dict):
        cache = {}
    cache[cleide_audit_chat_idempotency_key(ref)] = {
        "answer": payload.get("answer"),
        "documents_used": list(payload.get("documents_used") or []),
        "flow_type": payload.get("flow_type") or CLEIDE_AUDIT_CHAT_FLOW_TYPE,
    }
    session_obj[CHAT_IDEMPOTENCY_CACHE_SESSION_KEY] = cache
    session_obj.modified = True


def chat_cleide_audit_reply(
    user_message: str,
    history: list,
    *,
    document_context_block: str | None = None,
    document_file_parts: list | None = None,
    has_documents: bool = False,
    documents_meta: list | None = None,
    source_doc_ids: list[str] | None = None,
    session_obj=None,
    max_history: int | None = None,
    question_max_chars: int | None = None,
    fallback_message: str | None = None,
    no_hallucination_instruction_enabled: bool | None = None,
) -> dict:
    """
    Envia mensagem ao LLM com histórico e contexto documental da Cleide Auditoria.

    Retorna dict com answer, flow_type, documents_used e eventual error.
    """
    audit_cfg = get_cleide_audit_config()
    message_limit = (
        int(question_max_chars)
        if question_max_chars is not None
        else int(audit_cfg.question_max_chars)
    )
    processing_message = (
        (fallback_message or audit_cfg.fallback_message or PROCESSING_ERROR_MESSAGE).strip()
        or PROCESSING_ERROR_MESSAGE
    )
    anti_hallucination_enabled = (
        audit_cfg.no_hallucination_instruction_enabled
        if no_hallucination_instruction_enabled is None
        else bool(no_hallucination_instruction_enabled)
    )

    clean_message = (user_message or "").strip()
    if not clean_message:
        return {
            "answer": "",
            "flow_type": CLEIDE_AUDIT_CHAT_FLOW_TYPE,
            "documents_used": [],
            "error": "invalid_message",
            "message": "Mensagem vazia.",
        }
    if len(clean_message) > message_limit:
        return {
            "answer": "",
            "flow_type": CLEIDE_AUDIT_CHAT_FLOW_TYPE,
            "documents_used": [],
            "error": "invalid_message",
            "message": f"Mensagem excede o limite de {message_limit} caracteres.",
        }
    clean_message = clean_message[: min(message_limit, MAX_MESSAGE_CHARS)]

    history_limit = _get_max_history(max_history=max_history)
    history_slice = sanitize_chat_history(history, max_history=history_limit)
    documents_used = _documents_used_from_meta(documents_meta) if has_documents else []

    client = _get_client()
    if not client:
        logger.warning(
            "Cleide Auditoria chat: nenhuma chave Gemini configurada (GEMINI_API_KEY ou GEMINI_API_KEY_1)."
        )
        return {
            "answer": "",
            "flow_type": CLEIDE_AUDIT_CHAT_FLOW_TYPE,
            "documents_used": documents_used,
            "error": "service_unavailable",
            "message": SERVICE_UNAVAILABLE_MESSAGE,
        }

    contents = _build_contents(
        history_slice,
        clean_message,
        document_context_block=document_context_block,
        document_file_parts=document_file_parts,
        no_hallucination_instruction_enabled=anti_hallucination_enabled,
    )

    last_error: Exception | None = None
    for model in _get_model_candidates():
        try:
            response = cleiton_governed_generate_content(
                client,
                model=model,
                contents=contents,
                agent="cleide",
                flow_type=CLEIDE_AUDIT_CHAT_FLOW_TYPE,
                api_key_label=_api_key_label(),
            )
            text = (getattr(response, "text", None) or "").strip()
            if text:
                return {
                    "answer": text,
                    "flow_type": CLEIDE_AUDIT_CHAT_FLOW_TYPE,
                    "documents_used": documents_used,
                }
            last_error = ValueError("Resposta vazia do modelo")
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Cleide Auditoria chat provider failure: model=%s exc_type=%s message=%s",
                model,
                exc.__class__.__name__,
                exc,
            )

    if last_error:
        logger.exception("Cleide Auditoria chat falhou após fallbacks: %s", last_error)
    return {
        "answer": "",
        "flow_type": CLEIDE_AUDIT_CHAT_FLOW_TYPE,
        "documents_used": documents_used,
        "error": "processing_failed",
        "message": processing_message,
    }
