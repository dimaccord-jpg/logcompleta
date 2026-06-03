"""
Backend do Chat Júlia: comunicação com o LLM (Google Gemini).
Chaves de API lidas de variáveis de ambiente (nunca hardcoded).
Histórico limitado por JULIA_CHAT_MAX_HISTORY (settings ou env).
"""
import logging
import os

from app.cleiton_doc_contracts import FLOW_TYPE_JULIA_CHAT, FLOW_TYPE_JULIA_CHAT_DOCUMENTAL
from app.prompts import JULIA_CHAT_SYSTEM_PROMPT
from app.run_cleiton_gemini_governance import cleiton_governed_generate_content
from app.services.julia_web_search_service import (
    search_web_links,
    should_search_web_for_question,
)

logger = logging.getLogger(__name__)
SUGGESTION_META_PREFIX = "[[JULIA_SUGGESTION::"
DEFAULT_JULIA_CHAT_MODEL_FALLBACK = "gemini-2.5-flash-lite"
DOCUMENTAL_DEADLINE_REPLY = (
    "Consegui acessar os PDFs, mas a comparação completa entre eles excedeu o tempo de processamento. "
    "Você pode pedir uma comparação mais específica, como preços, prazos, cláusulas ou diferenças por rota."
)
GENERIC_REPLY_FALLBACK = (
    "Desculpe, não consegui processar sua mensagem no momento. Tente de novo em instantes."
)


def _api_key_label_chat() -> str:
    if os.getenv("GEMINI_API_KEY_1"):
        return "GEMINI_API_KEY_1"
    if os.getenv("GEMINI_API_KEY"):
        return "GEMINI_API_KEY"
    return "unknown"

def _get_chat_model_fallback() -> str:
    for env_key in ("JULIA_CHAT_MODEL_FALLBACK", "GEMINI_MODEL_TEXT_FALLBACK"):
        val = (os.getenv(env_key) or "").strip()
        if val:
            return val
    return DEFAULT_JULIA_CHAT_MODEL_FALLBACK


def _get_chat_model_candidates() -> list[str]:
    """Modelos em ordem de fallback; nunca inclui gemini-1.5-flash (descontinuado no runtime)."""
    candidates = [
        (os.getenv("GEMINI_MODEL_TEXT") or "").strip(),
        "gemini-2.5-flash",
        _get_chat_model_fallback(),
    ]
    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _is_documental_pdf_context(flow_type: str, document_file_parts: list | None) -> bool:
    if flow_type != FLOW_TYPE_JULIA_CHAT_DOCUMENTAL:
        return False
    return bool([p for p in (document_file_parts or []) if p is not None])


def _is_provider_deadline_error(exc: Exception | None) -> bool:
    if exc is None:
        return False
    if isinstance(exc, TimeoutError):
        return True
    msg = str(exc).lower()
    return any(
        token in msg
        for token in (
            "504",
            "deadline_exceeded",
            "deadline exceeded",
            "timed out",
            "timeout",
            "time out",
        )
    )


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


def _summarize_contents_for_log(contents: str | list) -> dict:
    if isinstance(contents, str):
        return {
            "contents_type": "str",
            "contents_items": 1,
            "item_types": ["text"],
            "file_parts": 0,
            "text_items": 1,
        }
    summary = {
        "contents_type": type(contents).__name__,
        "contents_items": len(contents),
        "item_types": [],
        "file_parts": 0,
        "text_items": 0,
    }
    for item in contents:
        if isinstance(item, str):
            summary["item_types"].append("text")
            summary["text_items"] += 1
        elif getattr(item, "file_data", None) is not None:
            summary["item_types"].append("file_part")
            summary["file_parts"] += 1
        else:
            summary["item_types"].append(type(item).__name__)
    return summary


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
    document_context_block: str | None = None,
    document_file_parts: list | None = None,
) -> str | list:
    """Monta prompt com system + histórico + nova mensagem (string ou lista com partes de arquivo)."""
    parts = [JULIA_CHAT_SYSTEM_PROMPT.strip(), "\n\n---\n\n"]
    doc_block = (document_context_block or "").strip()
    if doc_block:
        parts.append(doc_block)
        parts.append("\n\n---\n\n")
    parts.append("Conversa recente:\n")
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
    prompt_text = "".join(parts)
    file_parts = [p for p in (document_file_parts or []) if p is not None]
    if file_parts:
        return file_parts + [prompt_text]
    return prompt_text


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


def chat_julia_reply(
    user_message: str,
    history: list,
    max_history: int = 10,
    *,
    document_context_block: str | None = None,
    document_file_parts: list | None = None,
    flow_type: str | None = None,
) -> dict:
    """
    Envia a mensagem do usuário ao LLM com histórico limitado.
    history: lista de dicts com "role" (user/model) e "content".
    max_history: número máximo de mensagens anteriores a incluir (janela de memória).
    document_context_block: bloco interno montado pelo Cleiton (Fase 4); não processa arquivos aqui.
    document_file_parts: partes de arquivo Gemini autorizadas pelo Cleiton (PDF real).
    flow_type: trilho de governança; padrão julia_chat ou julia_chat_documental quando há contexto.
    Retorna {"reply": str} em sucesso ou {"reply": str, "error": str} em fallback.
    """
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
        document_context_block=document_context_block,
        document_file_parts=document_file_parts,
    )
    resolved_flow_type = (flow_type or "").strip()
    if not resolved_flow_type:
        has_doc_context = bool((document_context_block or "").strip()) or bool(document_file_parts)
        resolved_flow_type = (
            FLOW_TYPE_JULIA_CHAT_DOCUMENTAL if has_doc_context else FLOW_TYPE_JULIA_CHAT
        )
    contents_summary = _summarize_contents_for_log(contents)
    logger.info(
        "Chat Julia request: flow_type=%s history_slice=%s web_links=%s doc_block_present=%s doc_file_parts=%s contents=%s",
        resolved_flow_type,
        len(history_slice),
        len(web_links),
        bool((document_context_block or "").strip()),
        len([p for p in (document_file_parts or []) if p is not None]),
        contents_summary,
    )
    last_error = None
    failed_models: list[str] = []
    model_candidates = _get_chat_model_candidates()
    documental_pdf = _is_documental_pdf_context(resolved_flow_type, document_file_parts)

    for idx, model in enumerate(model_candidates):
        if idx > 0:
            logger.warning(
                "Chat Julia fallback attempt: failed_model=%s fallback_model=%s flow_type=%s",
                failed_models[-1],
                model,
                resolved_flow_type,
            )
        try:
            logger.info(
                "Chat Julia provider attempt: model=%s flow_type=%s contents_type=%s contents_items=%s file_parts=%s",
                model,
                resolved_flow_type,
                contents_summary["contents_type"],
                contents_summary["contents_items"],
                contents_summary["file_parts"],
            )
            response = cleiton_governed_generate_content(
                client,
                model=model,
                contents=contents,
                agent="julia",
                flow_type=resolved_flow_type,
                api_key_label=_api_key_label_chat(),
            )
            text = (response.text or "").strip()
            logger.info(
                "Chat Julia provider success: model=%s response_text_present=%s usage_metadata_present=%s",
                model,
                bool(text),
                getattr(response, "usage_metadata", None) is not None,
            )
            if text:
                links_md = _format_web_links_markdown(web_links)
                if links_md:
                    text = f"{text}\n\n{links_md}"
                out = {"reply": text, "suggestions": _build_follow_up_suggestions(clean_user_message)}
                if web_links:
                    out["web_links"] = web_links
                return out
            last_error = ValueError("Resposta vazia do modelo")
            failed_models.append(model)
        except Exception as e:
            last_error = e
            failed_models.append(model)
            logger.warning(
                "Chat Julia provider failure: model=%s exc_type=%s message=%s",
                model,
                e.__class__.__name__,
                e,
            )
            if documental_pdf and _is_provider_deadline_error(e):
                logger.warning(
                    "Chat Julia documental deadline: model=%s flow_type=%s file_parts=%s; "
                    "retornando mensagem específica sem fallback adicional",
                    model,
                    resolved_flow_type,
                    contents_summary["file_parts"],
                )
                break

    if last_error:
        logger.exception("Chat Júlia falhou após fallbacks: %s", last_error)
    if documental_pdf and last_error and _is_provider_deadline_error(last_error):
        reply_text = DOCUMENTAL_DEADLINE_REPLY
    else:
        reply_text = GENERIC_REPLY_FALLBACK
    out = {"reply": reply_text, "suggestions": _build_follow_up_suggestions(clean_user_message)}
    if web_links:
        out["web_links"] = web_links
    return out
