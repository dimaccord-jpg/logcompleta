"""
Backend do chat documental da Agente Compara (Fase 2 — endpoint backend).

Comunicação com LLM exclusivamente via governança Cleiton.
Sem regex, parser rígido ou acoplamento com Júlia/BI Cleide.
"""
from __future__ import annotations

import logging
import os
from typing import Any
from uuid import uuid4

from flask import has_app_context

from app.agente_compara_doc_service import AGENTE_COMPARA_CHAT_FLOW_TYPE, agente_compara_chat_idempotency_key
from app.agente_compara_prompt import build_agente_compara_system_prompt
from app.cleiton_doc_escopo import (
    operational_cache_payload_is_current,
    stamp_operational_cache_payload,
)
from app.run_cleiton_gemini_governance import (
    PERSIST_OK,
    STATUS_FAILURE,
    STATUS_SUCCESS,
    STATUS_SUCCESS_NO_METRICS,
    cleiton_governed_generate_content_observed,
)
from app.services.cleiton_ai_data_governance import CleitonAiGovernanceBlockedError
from app.services.cleiton_billable_ai_call import (
    SDK_RETRY_ATTEMPTS,
    gemini_http_options,
    tentativas_retry_sdk,
    usuario_operacional_da_chamada,
)
from app.services.agente_compara_config_service import get_agente_compara_config

logger = logging.getLogger(__name__)

_MOTIVOS_CONCLUSAO_FINANCEIRA = frozenset(
    {
        "ja_apropriado",
        "creditos_zero",
        "origem_sistema",
        "sem_usuario_id",
        "sem_franquia_id",
        "http_anonimo",
        "franquia_reservada_sistema",
        "competencia_ciclo_anterior",
        "custo_assumido_plataforma",
    }
)


class _GeracaoFinanceiraInterrompida(Exception):
    """A geração não tem conclusão financeira confirmada. O fallback para."""

    def __init__(self, mensagem_usuario: str | None = None) -> None:
        super().__init__(mensagem_usuario or "geracao_financeira_interrompida")
        self.mensagem_usuario = mensagem_usuario

CHAT_IDEMPOTENCY_CACHE_SESSION_KEY = "agente_compara_chat_idempotency_cache"
DEFAULT_AGENTE_COMPARA_CHAT_MAX_HISTORY = 10
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


def _try_record_agente_compara_chat_growth_task(
    *,
    event_name: str,
    execution_id: str,
    task_stage: str | None = None,
    error_code: str | None = None,
) -> None:
    """Observabilidade Growth fail-open; nao altera o chat documental do Agente Compara."""
    exec_id = (execution_id or "").strip()
    if not exec_id:
        return
    try:
        from app.funnel_event_service import (
            FUNNEL_SOURCE_AGENTE_COMPARA_CHAT,
            TASK_TYPE_AGENTE_COMPARA_CHAT,
            try_record_growth_task_event,
        )

        try_record_growth_task_event(
            event_name=event_name,
            source=FUNNEL_SOURCE_AGENTE_COMPARA_CHAT,
            task_type=TASK_TYPE_AGENTE_COMPARA_CHAT,
            idempotency_key=f"growth:agente_compara_chat:{exec_id}:{event_name}",
            task_stage=task_stage,
            error_code=error_code,
            execution_id=exec_id,
        )
    except Exception:
        logger.exception(
            "agente_compara_chat_growth_task_failed event=%s execution_id=%s",
            event_name,
            exec_id,
        )


def _get_max_history(*, max_history: int | None = None) -> int:
    if max_history is not None:
        return max(0, int(max_history))
    if not has_app_context():
        return DEFAULT_AGENTE_COMPARA_CHAT_MAX_HISTORY
    return max(0, int(get_agente_compara_config().chat_max_history))


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

        timeout_ms = 30_000
        raw = (os.getenv("GEMINI_HTTP_TIMEOUT_MS") or "").strip()
        if raw:
            try:
                timeout_ms = max(1_000, int(raw))
            except ValueError:
                pass
        return genai.Client(api_key=key, http_options=gemini_http_options(timeout_ms=timeout_ms))
    except Exception as exc:
        logger.error("Falha ao inicializar cliente Gemini para Agente Compara: %s", exc)
        return None


def _get_model_candidates() -> list[str]:
    candidates = [
        (os.getenv("GEMINI_MODEL_TEXT") or "").strip(),
        "gemini-2.5-flash",
        (os.getenv("AGENTE_COMPARA_CHAT_MODEL_FALLBACK") or "").strip(),
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
    system_prompt = build_agente_compara_system_prompt().strip()
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
        label = "Usuário" if role == "user" else "Agente Compara"
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        parts.append(f"{label}: {content}\n\n")
    parts.append(f"Usuário: {user_message.strip()}\n\nAgente Compara:")
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
    payload = cache.get(agente_compara_chat_idempotency_key(ref))
    if not isinstance(payload, dict):
        return None
    if not operational_cache_payload_is_current(payload):
        return None
    return payload


def cache_chat_response(session_obj, request_id: str, payload: dict) -> None:
    ref = (request_id or "").strip()
    if not ref or not isinstance(payload, dict):
        return
    cache = session_obj.get(CHAT_IDEMPOTENCY_CACHE_SESSION_KEY)
    if not isinstance(cache, dict):
        cache = {}
    cache[agente_compara_chat_idempotency_key(ref)] = stamp_operational_cache_payload(
        {
            "answer": payload.get("answer"),
            "documents_used": list(payload.get("documents_used") or []),
            "flow_type": payload.get("flow_type") or AGENTE_COMPARA_CHAT_FLOW_TYPE,
        }
    )
    session_obj[CHAT_IDEMPOTENCY_CACHE_SESSION_KEY] = cache
    session_obj.modified = True


def _identidade_do_operador(usuario: Any) -> dict | None:
    """Conta, franquia e usuário reais. Não promove a chamada a origem de sistema."""
    if usuario is None or getattr(usuario, "is_authenticated", False) is not True:
        return None
    from app.consumo_identidade import TIPO_ORIGEM_HTTP_USUARIO, identidade_de_usuario
    from app.models import User

    if isinstance(usuario, User):
        try:
            ident = identidade_de_usuario(usuario, TIPO_ORIGEM_HTTP_USUARIO)
        except Exception:
            logger.info("Agente Compara chat identidade do operador indisponivel")
            return None
    else:
        from app.agente_compara_chat_semantic_selection import operational_identity

        tripla = operational_identity(usuario)
        if tripla is None or not has_app_context():
            return None
        conta_id, usuario_id, franquia_id = tripla
        ident = {
            "conta_id": conta_id,
            "franquia_id": franquia_id,
            "usuario_id": usuario_id,
            "tipo_origem": TIPO_ORIGEM_HTTP_USUARIO,
            "origem_sistema": False,
        }
    if ident.get("origem_sistema") is True:
        return None
    if ident.get("conta_id") is None or ident.get("franquia_id") is None or ident.get("usuario_id") is None:
        return None
    return ident


def _recusar_operacao(usuario: Any) -> str | None:
    """None quando a autorização operacional da Home permite a geração."""
    from app.services.cleiton_operacao_autorizacao_service import (
        avaliar_autorizacao_operacao_por_franquia,
    )

    if _identidade_do_operador(usuario) is None:
        return ""
    try:
        authz = avaliar_autorizacao_operacao_por_franquia(usuario)
    except Exception:
        logger.info("Agente Compara chat autorizacao operacional indisponivel")
        return ""
    if not isinstance(authz, dict) or authz.get("permitido") is not True:
        mensagem = authz.get("mensagem_usuario") if isinstance(authz, dict) else None
        return mensagem if isinstance(mensagem, str) else ""
    return None


def _config_tecnica_atual():
    """Mesmos tetos de saída, candidatos e thinking já aplicados neste chat."""
    from app.services.cleiton_billable_ai_call import (
        _preparar_config_limitada,
        resolver_limites_config,
    )

    saida, candidatos, thinking, erro = resolver_limites_config(None)
    if erro:
        return None
    config, ok, _motivo = _preparar_config_limitada(
        None,
        saida=saida,
        candidatos=candidatos,
        thinking=thinking,
    )
    if not ok:
        return None
    return config


def _metricas_observadas(observation: Any) -> bool:
    return not (
        observation.input_tokens is None
        and observation.output_tokens is None
        and observation.total_tokens is None
    )


def _conclusao_financeira_confirmada(resultado: Any) -> bool:
    if getattr(resultado, "abateu_franquia", False) is True:
        return True
    motivo = getattr(resultado, "motivo_nao_abateu", None)
    return motivo in _MOTIVOS_CONCLUSAO_FINANCEIRA


def _aplicar_motor_compartilhado(evento_id: int) -> None:
    from app.extensions import db
    from app.services.cleiton_franquia_operacional_service import (
        aplicar_motor_apos_ia_consumo_evento,
    )

    try:
        resultado = aplicar_motor_apos_ia_consumo_evento(int(evento_id))
    except Exception:
        logger.info("Agente Compara chat abatimento nao confirmado evento_id=%s", evento_id)
        try:
            db.session.rollback()
        except Exception:
            pass
        raise _GeracaoFinanceiraInterrompida() from None
    if not _conclusao_financeira_confirmada(resultado):
        logger.info(
            "Agente Compara chat conclusao financeira ausente evento_id=%s motivo=%s",
            evento_id,
            getattr(resultado, "motivo_nao_abateu", None),
        )
        raise _GeracaoFinanceiraInterrompida()


def cleiton_governed_generate_content(
    client: Any,
    *,
    model: str,
    contents: Any,
    agent: str,
    flow_type: str,
    api_key_label: str,
    **_ignorado: Any,
) -> Any:
    """Autoriza como a Home, registra o evento e abate o consumo observado.

    Não reserva crédito e não grava tentativa de chamada. Consumo sem métrica
    não entra no motor e não libera outra geração.
    """
    from app.consumo_identidade import (
        clear_consumo_identidade,
        get_consumo_identidade,
        set_consumo_identidade,
    )

    usuario = usuario_operacional_da_chamada()
    recusa = _recusar_operacao(usuario)
    if recusa is not None:
        raise _GeracaoFinanceiraInterrompida(recusa or None)
    if tentativas_retry_sdk(client) != SDK_RETRY_ATTEMPTS:
        raise _GeracaoFinanceiraInterrompida()
    config = _config_tecnica_atual()
    if config is None:
        raise _GeracaoFinanceiraInterrompida()
    ident = _identidade_do_operador(usuario)
    if ident is None:
        raise _GeracaoFinanceiraInterrompida()

    anterior = get_consumo_identidade()
    set_consumo_identidade(ident)
    try:
        observation = cleiton_governed_generate_content_observed(
            client,
            model=model,
            contents=contents,
            config=config,
            agent=agent,
            flow_type=flow_type,
            api_key_label=api_key_label,
            identidade=ident,
            apply_motor=False,
        )
    finally:
        if anterior is None:
            clear_consumo_identidade()
        else:
            set_consumo_identidade(anterior)

    if observation.persist_status != PERSIST_OK or observation.evento_id is None:
        raise _GeracaoFinanceiraInterrompida()
    if observation.status == STATUS_SUCCESS_NO_METRICS or not _metricas_observadas(observation):
        raise _GeracaoFinanceiraInterrompida()
    if observation.status not in (STATUS_SUCCESS, STATUS_FAILURE):
        raise _GeracaoFinanceiraInterrompida()
    _aplicar_motor_compartilhado(int(observation.evento_id))
    if observation.provider_exception is not None:
        raise observation.provider_exception
    if observation.response is None:
        raise _GeracaoFinanceiraInterrompida()
    return observation.response


def chat_agente_compara_reply(
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
    execution_id: str | None = None,
) -> dict:
    """
    Envia mensagem ao LLM com histórico e contexto documental da Agente Compara.

    Retorna dict com answer, flow_type, documents_used e eventual error.
    """
    from app.funnel_event_service import (
        FUNNEL_EVENT_TASK_COMPLETED,
        FUNNEL_EVENT_TASK_FAILED,
        FUNNEL_EVENT_TASK_STARTED,
    )

    audit_cfg = get_agente_compara_config()
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
            "flow_type": AGENTE_COMPARA_CHAT_FLOW_TYPE,
            "documents_used": [],
            "error": "invalid_message",
            "message": "Mensagem vazia.",
        }
    if len(clean_message) > message_limit:
        return {
            "answer": "",
            "flow_type": AGENTE_COMPARA_CHAT_FLOW_TYPE,
            "documents_used": [],
            "error": "invalid_message",
            "message": f"Mensagem excede o limite de {message_limit} caracteres.",
        }
    clean_message = clean_message[: min(message_limit, MAX_MESSAGE_CHARS)]

    history_limit = _get_max_history(max_history=max_history)
    history_slice = sanitize_chat_history(history, max_history=history_limit)
    documents_used = _documents_used_from_meta(documents_meta) if has_documents else []

    growth_exec_id = (execution_id or "").strip() or str(uuid4())
    growth_started = False

    def _growth_start(*, task_stage: str = "llm_generation") -> None:
        nonlocal growth_started
        if growth_started:
            return
        _try_record_agente_compara_chat_growth_task(
            event_name=FUNNEL_EVENT_TASK_STARTED,
            execution_id=growth_exec_id,
            task_stage=task_stage,
        )
        growth_started = True

    def _growth_complete(*, task_stage: str = "reply_ready") -> None:
        if not growth_started:
            return
        _try_record_agente_compara_chat_growth_task(
            event_name=FUNNEL_EVENT_TASK_COMPLETED,
            execution_id=growth_exec_id,
            task_stage=task_stage,
        )

    def _growth_fail(error_code: str, *, task_stage: str | None = None) -> None:
        if not growth_started:
            return
        _try_record_agente_compara_chat_growth_task(
            event_name=FUNNEL_EVENT_TASK_FAILED,
            execution_id=growth_exec_id,
            task_stage=task_stage,
            error_code=error_code or "agente_compara_chat_failed",
        )

    client = _get_client()
    if not client:
        logger.warning(
            "Agente Compara chat: nenhuma chave Gemini configurada (GEMINI_API_KEY ou GEMINI_API_KEY_1)."
        )
        _try_record_agente_compara_chat_growth_task(
            event_name=FUNNEL_EVENT_TASK_FAILED,
            execution_id=growth_exec_id,
            task_stage="provider_check",
            error_code="agente_compara_chat_provider_unavailable",
        )
        return {
            "answer": "",
            "flow_type": AGENTE_COMPARA_CHAT_FLOW_TYPE,
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

    _growth_start()

    last_error: Exception | None = None
    for model in _get_model_candidates():
        try:
            response = cleiton_governed_generate_content(
                client,
                model=model,
                contents=contents,
                agent="agente_compara",
                flow_type=AGENTE_COMPARA_CHAT_FLOW_TYPE,
                api_key_label=_api_key_label(),
            )
            text = (getattr(response, "text", None) or "").strip()
            if text:
                _growth_complete()
                return {
                    "answer": text,
                    "flow_type": AGENTE_COMPARA_CHAT_FLOW_TYPE,
                    "documents_used": documents_used,
                }
            last_error = ValueError("Resposta vazia do modelo")
        except (_GeracaoFinanceiraInterrompida, CleitonAiGovernanceBlockedError) as exc:
            last_error = exc
            break
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Agente Compara chat provider failure: model=%s exc_type=%s message=%s",
                model,
                exc.__class__.__name__,
                exc,
            )

    if last_error:
        logger.exception("Agente Compara chat falhou após fallbacks: %s", last_error)
    _growth_fail("agente_compara_chat_processing_failed", task_stage="llm_generation")
    mensagem = processing_message
    mensagem_operacao = getattr(last_error, "mensagem_usuario", None) or getattr(
        last_error, "user_message", None
    )
    if isinstance(mensagem_operacao, str) and mensagem_operacao.strip():
        mensagem = mensagem_operacao
    return {
        "answer": "",
        "flow_type": AGENTE_COMPARA_CHAT_FLOW_TYPE,
        "documents_used": documents_used,
        "error": "processing_failed",
        "message": mensagem,
    }
