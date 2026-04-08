"""
Backend do Chat Júlia: comunicação com o LLM (Google Gemini).
Chaves de API lidas de variáveis de ambiente (nunca hardcoded).
Histórico limitado por JULIA_CHAT_MAX_HISTORY (settings ou env).
"""
import logging
import os

from app.prompts import JULIA_CHAT_SYSTEM_PROMPT
from app.run_cleiton_gemini_governance import cleiton_governed_generate_content
from app.services.julia_web_search_service import (
    search_web_links,
    should_search_web_for_question,
)

logger = logging.getLogger(__name__)
SUGGESTION_META_PREFIX = "[[JULIA_SUGGESTION::"


def _api_key_label_chat() -> str:
    if os.getenv("GEMINI_API_KEY_1"):
        return "GEMINI_API_KEY_1"
    if os.getenv("GEMINI_API_KEY"):
        return "GEMINI_API_KEY"
    return "unknown"

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


def _sanitize_link_label(label: str) -> str:
    out = (label or "").replace("[", "(").replace("]", ")")
    return out.replace("\n", " ").strip()


def _format_web_links_markdown(web_links: list[dict]) -> str:
    if not web_links:
        return ""
    lines = ["Links uteis:"]
    for item in web_links:
        title = _sanitize_link_label((item or {}).get("title") or "Fonte")
        url = ((item or {}).get("url") or "").strip()
        if not url:
            continue
        lines.append(f"- [{title}]({url})")
    return "\n".join(lines) if len(lines) > 1 else ""


def _build_follow_up_suggestions(user_message: str) -> list[str]:
    text = (user_message or "").lower()
    suggestions = []
    if any(k in text for k in ("frete", "rodovi", "rota")):
        suggestions.extend(
            [
                "Quer um checklist para reduzir custo por rota?",
                "Posso sugerir KPIs para acompanhar esse cenário?",
            ]
        )
    if any(k in text for k in ("armazen", "estoque", "cd")):
        suggestions.extend(
            [
                "Quer priorizar ações de armazenagem para 30 dias?",
                "Posso montar um plano rapido de melhoria operacional?",
            ]
        )
    if any(k in text for k in ("fornecedor", "fabricante", "document", "link", "site")):
        suggestions.extend(
            [
                "Quer que eu compare opcoes por custo total e prazo?",
                "Posso organizar criterios tecnicos para sua avaliacao?",
            ]
        )
    suggestions.extend(
        [
            "Quer transformar isso em um plano de acao semanal?",
            "Deseja uma versao executiva para apresentar ao time?",
        ]
    )
    unique = []
    for s in suggestions:
        if s not in unique:
            unique.append(s)
        if len(unique) >= 3:
            break
    return unique


def _build_contents_with_history(
    history_slice: list,
    new_message: str,
    web_links: list[dict] | None = None,
    suggestion_meta: dict | None = None,
) -> str:
    """Monta o prompt com system + histórico + nova mensagem (para envio único ao modelo)."""
    parts = [JULIA_CHAT_SYSTEM_PROMPT.strip(), "\n\n---\n\nConversa recente:\n"]
    for msg in history_slice:
        role = (msg.get("role") or "user").lower()
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        label = "Usuário" if role == "user" else "Júlia"
        parts.append(f"{label}: {content}\n\n")
    parts.append(f"Usuário: {new_message.strip()}\n\n")
    meta = suggestion_meta or {}
    if meta.get("source") == "suggestion_chip":
        parts.append("Instrucao de interacao para a ultima entrada:\n")
        parts.append("- A entrada veio de uma sugestao clicavel do proprio chat.\n")
        parts.append("- Execute a acao solicitada diretamente na resposta.\n")
        parts.append("- Evite pedir reconfirmacao desnecessaria.\n")
        parts.append("- Mantenha foco estrito em logistica e supply chain.\n\n")
    links = list(web_links or [])
    if links:
        parts.append("Contexto web opcional (usar apenas se agregar precisao tecnica em logistica):\n")
        for item in links:
            title = _sanitize_link_label((item or {}).get("title") or "Fonte")
            url = ((item or {}).get("url") or "").strip()
            snippet = ((item or {}).get("snippet") or "").strip()
            if not url:
                continue
            parts.append(f"- {title} | {url}")
            if snippet:
                parts.append(f" | {snippet}")
            parts.append("\n")
        parts.append("\n")
    parts.append("Júlia:")
    return "".join(parts)


def _extract_suggestion_metadata(user_message: str) -> tuple[str, dict]:
    text = (user_message or "").strip()
    if not text.startswith(SUGGESTION_META_PREFIX):
        return text, {}
    end = text.find("]]")
    if end < 0:
        return text, {}
    raw_meta = text[len(SUGGESTION_META_PREFIX):end].strip()
    clean_message = text[end + 2 :].strip()
    meta: dict = {}
    for fragment in raw_meta.split(";"):
        if "=" not in fragment:
            continue
        key, val = fragment.split("=", 1)
        k = key.strip().lower()
        v = val.strip().lower()
        if k:
            meta[k] = v
    return clean_message, meta


def chat_julia_reply(user_message: str, history: list, max_history: int = 10) -> dict:
    """
    Envia a mensagem do usuário ao LLM com histórico limitado.
    history: lista de dicts com "role" (user/model) e "content".
    max_history: número máximo de mensagens anteriores a incluir (janela de memória).
    Retorna {"reply": str} em sucesso ou {"reply": str, "error": str} em fallback.
    """
    reply_fallback = "Desculpe, não consegui processar sua mensagem no momento. Tente de novo em instantes."
    clean_user_message, suggestion_meta = _extract_suggestion_metadata(user_message)
    if not (clean_user_message or "").strip():
        return {
            "reply": "Envie uma mensagem sobre logistica, fretes ou supply chain que eu respondo com prazer.",
            "suggestions": _build_follow_up_suggestions(""),
        }

    # Respeita o limite de histórico ao montar o contexto (padrão seguro)
    history_list = list(history) if isinstance(history, list) else []
    history_slice = history_list[-max_history:] if max_history > 0 else []

    client = _get_client()
    if not client:
        logger.warning("Chat Júlia: nenhuma chave Gemini configurada (GEMINI_API_KEY ou GEMINI_API_KEY_1).")
        return {"reply": "Assistente temporariamente indisponível. Verifique a configuração do serviço."}

    web_links: list[dict] = []
    if should_search_web_for_question(clean_user_message):
        web_links = search_web_links(clean_user_message)
    contents = _build_contents_with_history(
        history_slice,
        clean_user_message,
        web_links=web_links,
        suggestion_meta=suggestion_meta,
    )
    last_error = None
    for model in _get_chat_model_candidates():
        try:
            response = cleiton_governed_generate_content(
                client,
                model=model,
                contents=contents,
                agent="julia",
                flow_type="julia_chat",
                api_key_label=_api_key_label_chat(),
            )
            text = (response.text or "").strip()
            if text:
                links_md = _format_web_links_markdown(web_links)
                if links_md:
                    text = f"{text}\n\n{links_md}"
                out = {"reply": text, "suggestions": _build_follow_up_suggestions(clean_user_message)}
                if web_links:
                    out["web_links"] = web_links
                return out
            last_error = ValueError("Resposta vazia do modelo")
        except Exception as e:
            last_error = e
            logger.warning("Chat Júlia modelo %s: %s", model, e)

    if last_error:
        logger.exception("Chat Júlia falhou após fallbacks: %s", last_error)
    out = {"reply": reply_fallback, "suggestions": _build_follow_up_suggestions(clean_user_message)}
    if web_links:
        out["web_links"] = web_links
    return out
