"""
Backend do Chat Júlia: comunicação com o LLM (Google Gemini).
Chaves de API lidas de variáveis de ambiente (nunca hardcoded).
Histórico limitado por JULIA_CHAT_MAX_HISTORY (settings ou env).
"""
import logging
import os

from app.prompts import JULIA_CHAT_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# Modelos em ordem de fallback (compatível com run_julia_agente_redacao)
def _get_chat_model_candidates():
    candidates = [
        os.getenv("GEMINI_MODEL_TEXT", "").strip(),
        "gemini-2.5-flash",
        "gemini-1.5-flash",
    ]
    seen = set()
    out = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _get_client():
    """Cliente Gemini; chave lida de variáveis de ambiente."""
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
    except Exception as e:
        logger.error("Falha ao inicializar cliente Gemini para chat: %s", e)
        return None


def _build_contents_with_history(history_slice: list, new_message: str) -> str:
    """Monta o prompt com system + histórico + nova mensagem (para envio único ao modelo)."""
    parts = [JULIA_CHAT_SYSTEM_PROMPT.strip(), "\n\n---\n\nConversa recente:\n"]
    for msg in history_slice:
        role = (msg.get("role") or "user").lower()
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        label = "Usuário" if role == "user" else "Júlia"
        parts.append(f"{label}: {content}\n\n")
    parts.append(f"Usuário: {new_message.strip()}\n\nJúlia:")
    return "".join(parts)


def chat_julia_reply(user_message: str, history: list, max_history: int = 10) -> dict:
    """
    Envia a mensagem do usuário ao LLM com histórico limitado.
    history: lista de dicts com "role" (user/model) e "content".
    max_history: número máximo de mensagens anteriores a incluir (janela de memória).
    Retorna {"reply": str} em sucesso ou {"reply": str, "error": str} em fallback.
    """
    reply_fallback = "Desculpe, não consegui processar sua mensagem no momento. Tente de novo em instantes."
    if not (user_message or "").strip():
        return {"reply": "Envie uma mensagem sobre logística, fretes ou supply chain que eu respondo com prazer."}

    # Respeita o limite de histórico ao montar o contexto (padrão seguro)
    history_list = list(history) if isinstance(history, list) else []
    history_slice = history_list[-max_history:] if max_history > 0 else []

    client = _get_client()
    if not client:
        logger.warning("Chat Júlia: nenhuma chave Gemini configurada (GEMINI_API_KEY ou GEMINI_API_KEY_1).")
        return {"reply": "Assistente temporariamente indisponível. Verifique a configuração do serviço."}

    contents = _build_contents_with_history(history_slice, user_message)
    last_error = None
    for model in _get_chat_model_candidates():
        try:
            response = client.models.generate_content(model=model, contents=contents)
            text = (response.text or "").strip()
            if text:
                return {"reply": text}
            last_error = ValueError("Resposta vazia do modelo")
        except Exception as e:
            last_error = e
            logger.warning("Chat Júlia modelo %s: %s", model, e)

    if last_error:
        logger.exception("Chat Júlia falhou após fallbacks: %s", last_error)
    return {"reply": reply_fallback}
