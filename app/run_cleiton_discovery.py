"""
Copilot Discovery — motor conversational-first para onboarding na Home.

Fluxo: mensagem + histórico + documento de capacidades → Gemini → resposta natural
com handoff opcional. Guardrails no backend; sem taxonomia/regex como motor principal.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from app.capability_taxonomy import CTA_BY_ID, DESTINATIONS
from app.consumo_identidade import get_consumo_identidade
from app.copilot_capabilities import (
    VALID_RECOMMENDED_AGENTS,
    agent_to_destination,
    build_local_conversational_reply,
    is_freight_audit_intent,
    is_roberto_bi_managerial_intent,
    load_capabilities_document,
    preferred_destination_for_message,
    should_suppress_handoff_for_unclear_activity,
)
from app.run_cleiton_gemini_governance import STATUS_SUCCESS_NO_METRICS
from app.run_cleiton_gemini_governance import cleiton_governed_generate_content
from app.run_cleiton_gemini_governance import register_internal_ia_event
from app.run_cleiton_agente_auditoria import registrar as auditoria_registrar
from app.utils.onboarding_text_normalization import (
    extract_user_terms_normalized,
    sanitize_user_message,
)

logger = logging.getLogger(__name__)

FLOW_TYPE_ONBOARDING_DISCOVERY = "onboarding_discovery"
AGENT_CLEITON = "cleiton"

VALID_CONFIDENCE = frozenset({"high", "medium", "low"})
VALID_DESTINATIONS = frozenset(DESTINATIONS.keys())

BANNED_REPLY_PATTERNS = (
    re.compile(r"Existem algumas formas de trabalhar esse tema", re.IGNORECASE),
    re.compile(
        r"^(?:📊|🔎|📈|🧠|📰)\s*(?:BI operacional|auditoria operacional|previsibilidade|estratégia|notícias)",
        re.IGNORECASE | re.MULTILINE,
    ),
)

COPILOT_JSON_CONTRACT = """
Responda SEMPRE com um único JSON válido (sem markdown, sem texto fora do JSON):
{
  "reply": "resposta natural em português BR",
  "recommended_agent": "roberto" | "cleide" | "julia" | "feed" | "agente_compara" | null,
  "handoff": {
    "destination": "roberto_bi" | "cleide_freight_audit" | "cleide_audit" | "julia_operational" | "feed" | "agente_compara",
    "label": "rótulo curto do botão (opcional)"
  } | null,
  "handoffs": [
    {"destination": "...", "label": "..."}
  ] | null,
  "needs_login": false,
  "confidence": "high" | "medium" | "low",
  "reason": "breve justificativa interna"
}

Regras do JSON:
- reply: conversacional; nunca menu fixo; nunca começar com "Existem algumas formas de trabalhar esse tema".
- recommended_agent: só quando fizer sentido; null para cumprimentos, curiosidade genérica ou falta de contexto.
- handoff / handoffs: opcionais; omita ou null se não houver recomendação real de navegação.
- Destinos técnicos de auditoria (agent `cleide`):
  - cleide_freight_audit → /auditoria-frete (Auditoria de Fretes com o AgenteAudita: cobrança, cobrado vs esperado, tabela negociada, divergências, memória de cálculo). Preferir este para auditoria.
  - cleide_audit → /cleide-bi-frete (BI de Auditoria legado). NÃO usar para auditoria de cobrança.
- Roberto: roberto_bi → /fretes (indicadores, gráficos, BI gerencial, previsões).
- AgenteCompara: agente_compara → /agente-compara (comparação de tabelas / BID comparativo interno). Label sugerido do CTA: "Iniciar comparação de tabelas".
- Artefato não define destino: planilha, PDF, tabela, custo ou transportadora isolados → converse e peça objetivo; sem handoff.
- Distinções:
  - Comparar tabelas/propostas sobre o mesmo volume → agente_compara.
  - Auditar cobrança / cobrado vs esperado → cleide_freight_audit.
  - Previsão / evolução histórica → roberto_bi.
  - Estratégia / negociação / sourcing sem cálculo multitabela → julia_operational.
- handoffs: use para oferecer dois caminhos (ex.: AgenteCompara + AgenteFrete; AgenteCompara + AgenteAudita; AgenteCompara + Roberto; AgenteFrete + Roberto), no máximo 2.
- needs_login: true apenas para continuidade operacional real com AgenteFrete; default false.
- O campo label do JSON é opcional; o backend usa o rótulo canônico da taxonomia.
- Pode encaminhar BID comparativo interno ao AgenteCompara.
- Não prometa cotação automatizada, BID aberto no mercado, coleta externa de propostas, contratação ou decisão automática.
""".strip()


FALLBACK_UNAVAILABLE_REPLY = (
    "Estou com dificuldade para responder agora. "
    "Tente reformular sua pergunta ou aguarde um instante."
)

EMPTY_MESSAGE_REPLY = (
    "Olá! Sou o Copilot do AgenteFrete. "
    "Conte o que você quer entender ou resolver na sua operação logística."
)


def _api_key_label() -> str:
    if os.getenv("GEMINI_API_KEY_1"):
        return "GEMINI_API_KEY_1"
    if os.getenv("GEMINI_API_KEY"):
        return "GEMINI_API_KEY"
    return "unknown"


def _gemini_api_key() -> str | None:
    key = (os.getenv("GEMINI_API_KEY_1") or os.getenv("GEMINI_API_KEY") or "").strip()
    return key or None


def _get_chat_model_candidates() -> list[str]:
    candidates = [
        os.getenv("GEMINI_MODEL_TEXT", "").strip(),
        "gemini-2.5-flash",
        "gemini-1.5-flash",
    ]
    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _get_client():
    key = _gemini_api_key()
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
        try:
            return genai.Client(
                api_key=key,
                http_options=genai_types.HttpOptions(timeout=timeout_ms),
            )
        except Exception:
            return genai.Client(api_key=key)
    except Exception as e:
        logger.error("Copilot Discovery: falha ao inicializar cliente Gemini: %s", e)
        return None


def _extract_json_object(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", raw)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return None


def _parse_gemini_response(raw_text: str) -> tuple[dict[str, Any] | None, str | None]:
    """Tenta JSON completo, campo reply parcial ou texto puro recuperável."""
    text = (raw_text or "").strip()
    if not text:
        return None, "empty_response"

    parsed = _extract_json_object(text)
    if parsed and (parsed.get("reply") or parsed.get("recommended_agent") is not None):
        return parsed, None

    reply_match = re.search(
        r'"reply"\s*:\s*"((?:\\.|[^"\\])*)"',
        text,
        flags=re.DOTALL,
    )
    if reply_match:
        try:
            reply = json.loads(f'"{reply_match.group(1)}"')
            if isinstance(reply, str) and reply.strip():
                recovered = dict(parsed or {})
                recovered["reply"] = reply.strip()
                recovered.setdefault("confidence", "medium")
                recovered.setdefault("reason", "recovered_reply_field")
                return recovered, "partial_json"
        except json.JSONDecodeError:
            pass

    cleaned = text
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    if cleaned and not cleaned.startswith("{") and not cleaned.startswith("["):
        if len(cleaned) >= 8 and "Existem algumas formas de trabalhar esse tema" not in cleaned:
            return {
                "reply": cleaned,
                "confidence": "medium",
                "reason": "plain_text_response",
            }, "plain_text"

    return None, "json_parse_failed"


def _build_system_prompt() -> str:
    capabilities = load_capabilities_document()
    return (
        "Você é o Copilot do AgenteFrete — assistente conversacional na Home do produto.\n\n"
        "Seu papel é conversar de forma natural, explicar o que o produto faz com honestidade "
        "e sugerir agentes ou navegação apenas quando fizer sentido.\n\n"
        "Identidade pública: use AgenteFrete (nunca Júlia) e AgenteAudita (nunca Cleide ou "
        "Cleide Auditoria). IDs técnicos internos (`julia`, `cleide`, `julia_operational`, "
        "`cleide_freight_audit`) não devem ser apresentados como marca.\n\n"
        "--- DOCUMENTO DE CAPACIDADES ---\n"
        f"{capabilities}\n"
        "--- FIM DO DOCUMENTO ---\n\n"
        f"{COPILOT_JSON_CONTRACT}"
    )


def _build_user_prompt(
    user_message: str,
    history: list,
    *,
    cta_id: str | None = None,
) -> str:
    parts = ["Histórico recente da conversa:\n"]
    if cta_id and cta_id in CTA_BY_ID:
        parts.append(
            f"(Usuário entrou via pill da Home: {cta_id} — use como contexto, não como destino fixo.)\n\n"
        )
    for msg in history[-8:]:
        role = (msg.get("role") or "user").lower()
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        label = "Usuário" if role == "user" else "Copilot"
        parts.append(f"{label}: {content}\n\n")
    parts.append(f"Usuário: {user_message.strip()}\n\n")
    parts.append("Responda apenas com o JSON especificado.")
    return "".join(parts)


def _is_banned_reply(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in BANNED_REPLY_PATTERNS)


def _normalize_confidence(raw: Any) -> str:
    value = str(raw or "low").strip().lower()
    return value if value in VALID_CONFIDENCE else "low"


def _normalize_recommended_agent(raw: Any) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip().lower()
    if value in ("null", "none", ""):
        return None
    return value if value in VALID_RECOMMENDED_AGENTS else None


def _build_handoff_payload(
    dest_id: str,
    *,
    capability_domain: str | None = None,
) -> dict[str, Any] | None:
    if dest_id not in VALID_DESTINATIONS:
        return None
    spec = DESTINATIONS[dest_id]
    if not capability_domain and dest_id == "agente_compara":
        capability_domain = "freight_table_comparison"
    # Destinations conhecidos: o label público vem da taxonomia, não de texto livre do modelo.
    payload: dict[str, Any] = {
        "destination": dest_id,
        "label": spec.label.strip(),
        "requires_login": bool(spec.requires_login),
        "requires_dataset": spec.requires_dataset,
        "url": spec.url,
    }
    if capability_domain:
        payload["capability_domain"] = capability_domain
    if spec.handoff_action == "start_julia":
        payload["action"] = "start_julia"
    if spec.agent:
        payload["agent"] = spec.agent
    return payload


def _remap_destination_for_message(dest_id: str, user_message: str) -> str:
    """
    Corrige handoffs legados sem converter o novo destino de volta ao antigo.

    - Intenção de auditoria de cobrança + cleide_audit legado → cleide_freight_audit.
    - Intenção de BI gerencial + destino Cleide → roberto_bi.
    - Nunca remapeia cleide_freight_audit para cleide_audit.
    """
    preferred = preferred_destination_for_message(user_message)
    if preferred == "cleide_freight_audit" and dest_id == "cleide_audit":
        return "cleide_freight_audit"
    if (
        preferred == "roberto_bi"
        and dest_id in ("cleide_audit", "cleide_freight_audit")
        and is_roberto_bi_managerial_intent(user_message)
        and not is_freight_audit_intent(user_message)
    ):
        return "roberto_bi"
    return dest_id


def _normalize_destination_id(raw: Any) -> str:
    """Normaliza id de destination (case, hífen) para bater com a taxonomy."""
    value = str(raw or "").strip().lower().replace("-", "_")
    aliases = {
        "agentecompara": "agente_compara",
        "compare_tabelas": "agente_compara",
        "comparar_tabelas": "agente_compara",
    }
    return aliases.get(value, value)


def _parse_handoff_entry(raw: Any, *, user_message: str = "") -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    dest_id = _normalize_destination_id(raw.get("destination"))
    if dest_id not in VALID_DESTINATIONS:
        mapped = agent_to_destination(str(raw.get("agent") or raw.get("recommended_agent") or ""))
        dest_id = _normalize_destination_id(mapped or dest_id)
    if dest_id not in VALID_DESTINATIONS:
        return None
    dest_id = _remap_destination_for_message(dest_id, user_message)
    if dest_id not in VALID_DESTINATIONS:
        return None
    return _build_handoff_payload(dest_id)


def _collect_handoffs(
    parsed: dict[str, Any],
    recommended_agent: str | None,
    *,
    user_message: str = "",
) -> list[dict[str, Any]]:
    handoffs: list[dict[str, Any]] = []
    seen: set[str] = set()

    raw_multi = parsed.get("handoffs")
    if isinstance(raw_multi, list):
        for item in raw_multi[:2]:
            payload = _parse_handoff_entry(item, user_message=user_message)
            if payload and payload["destination"] not in seen:
                seen.add(payload["destination"])
                handoffs.append(payload)

    if not handoffs:
        single = _parse_handoff_entry(parsed.get("handoff"), user_message=user_message)
        if single:
            handoffs.append(single)

    if not handoffs and recommended_agent:
        dest_id = agent_to_destination(recommended_agent)
        if dest_id:
            dest_id = _remap_destination_for_message(dest_id, user_message)
            payload = _build_handoff_payload(dest_id)
            if payload:
                handoffs.append(payload)

    return handoffs


def _should_suppress_handoff(user_message: str, confidence: str) -> bool:
    """Evita handoff automático em cumprimentos e perguntas exploratórias vagas."""
    text = (user_message or "").strip().lower()
    if not text:
        return True
    if confidence == "low":
        return True
    greeting_only = re.fullmatch(
        r"(?:ol[aá]|oi|bom\s+dia|boa\s+tarde|boa\s+noite|e\s*a[ií]|hey|hello|hi)[!.?\s]*",
        text,
        flags=re.IGNORECASE,
    )
    if greeting_only:
        return True
    product_curiosity = re.search(
        r"\b(?:voc[eê]s?\s+(?:tem|possui|oferece)|o\s+que\s+voc[eê]s?\s+faz|"
        r"algum\s+bi\b|possui\s+algum\s+bi|tem\s+bi\b)\b",
        text,
    )
    if product_curiosity and confidence != "high":
        return True
    if should_suppress_handoff_for_unclear_activity(user_message):
        return True
    return False


def _apply_guardrails(
    parsed: dict[str, Any],
    user_message: str,
    *,
    pipeline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pipeline = pipeline or {}
    reply = (parsed.get("reply") or "").strip()
    guardrail_notes: list[str] = []

    if _is_banned_reply(reply):
        guardrail_notes.append("banned_reply_stripped")
        reply = ""

    confidence = _normalize_confidence(parsed.get("confidence"))
    recommended_agent = _normalize_recommended_agent(parsed.get("recommended_agent"))
    reason = str(parsed.get("reason") or "").strip()[:300]

    handoffs = _collect_handoffs(parsed, recommended_agent, user_message=user_message)
    if _should_suppress_handoff(user_message, confidence):
        if handoffs:
            guardrail_notes.append("handoff_suppressed")
        handoffs = []
    elif not handoffs:
        # Gemini às vezes explica o AgenteCompara no texto e omite handoff no JSON.
        # Injeta só o destination de comparação multitabela (correção pontual do CTA).
        preferred = preferred_destination_for_message(user_message)
        if preferred == "agente_compara":
            injected = _build_handoff_payload(preferred)
            if injected:
                handoffs = [injected]
                guardrail_notes.append("preferred_destination_injected")
                if not recommended_agent:
                    recommended_agent = _normalize_recommended_agent(injected.get("agent"))
    # needs_login do modelo NÃO desprotege destino operacional; taxonomia é a fonte.
    needs_login = parsed.get("needs_login") is True
    for h in handoffs:
        dest_id = str(h.get("destination") or "")
        spec = DESTINATIONS.get(dest_id)
        if spec is not None:
            h["requires_login"] = bool(spec.requires_login)
            if spec.url:
                h["url"] = spec.url

    fallback_reason = None
    if not reply:
        local = build_local_conversational_reply(user_message)
        reply = local["reply"]
        reason = reason or local.get("reason") or "guardrail_empty_reply_local"
        fallback_reason = "guardrail_empty_reply_local"
        if not handoffs and local.get("handoff"):
            handoffs = _collect_handoffs(local, local.get("recommended_agent"), user_message=user_message)
            if _should_suppress_handoff(user_message, _normalize_confidence(local.get("confidence"))):
                handoffs = []
            else:
                guardrail_notes.append("local_handoff_from_capabilities")
            for h in handoffs:
                dest_id = str(h.get("destination") or "")
                spec = DESTINATIONS.get(dest_id)
                if spec is not None:
                    h["requires_login"] = bool(spec.requires_login)
                    if spec.url:
                        h["url"] = spec.url

    next_action = "converse"
    if len(handoffs) >= 2:
        next_action = "multi_handoff"
    elif len(handoffs) == 1:
        next_action = "handoff"

    handoff = handoffs[0] if len(handoffs) == 1 else None

    destination_candidates = [
        {
            "destination": h["destination"],
            "label": h["label"],
            "url": h.get("url"),
            "agent": h.get("agent"),
            "score": 90.0,
        }
        for h in handoffs
    ]

    discovery: dict[str, Any] = {
        "confidence": confidence,
        "next_action": next_action,
        "recommended_agent": recommended_agent,
        "reason": reason,
        "capability_candidates": [],
        "needs_login": needs_login,
        "pipeline": {
            **pipeline,
            "guardrail_notes": guardrail_notes,
            "fallback_reason": fallback_reason,
        },
    }

    return {
        "reply": reply,
        "recommended_agent": recommended_agent,
        "handoff": handoff,
        "handoffs": handoffs,
        "refinement_options": [],
        "destination_candidates": destination_candidates,
        "discovery": discovery,
    }


def _local_fallback_response(user_message: str, *, reason: str) -> dict[str, Any]:
    local = build_local_conversational_reply(user_message)
    local.setdefault("needs_login", False)
    pipeline = {
        "gemini_called": False,
        "fallback_reason": reason,
        "source": "local_capabilities",
    }
    result = _apply_guardrails(local, user_message, pipeline=pipeline)
    discovery = dict(result.get("discovery") or {})
    merged_pipeline = dict(discovery.get("pipeline") or {})
    merged_pipeline["fallback_reason"] = reason
    merged_pipeline.setdefault("source", "local_capabilities")
    discovery["pipeline"] = merged_pipeline
    result["discovery"] = discovery
    return result


def _unavailable_response(*, reason: str = "gemini_unavailable", user_message: str = "") -> dict[str, Any]:
    if user_message.strip():
        return _local_fallback_response(user_message, reason=reason)
    return {
        "reply": FALLBACK_UNAVAILABLE_REPLY,
        "recommended_agent": None,
        "handoff": None,
        "handoffs": [],
        "refinement_options": [],
        "destination_candidates": [],
        "discovery": {
            "confidence": "low",
            "next_action": "converse",
            "recommended_agent": None,
            "reason": reason,
            "capability_candidates": [],
            "needs_login": False,
            "pipeline": {"fallback_reason": reason, "source": "unavailable"},
        },
    }


def _build_onboarding_audit_context(
    user_message: str,
    *,
    cta_id: str | None,
    discovery: dict[str, Any],
    handoff: dict[str, Any] | None,
    history_turns: int,
) -> dict[str, Any]:
    clean_message = (user_message or "").strip()
    next_action = str(discovery.get("next_action") or "converse")
    pipeline = discovery.get("pipeline") or {}
    capability_top = discovery.get("recommended_agent")
    if isinstance(handoff, dict) and handoff.get("capability_domain"):
        capability_top = handoff.get("capability_domain")
    contexto: dict[str, Any] = {
        "cta_id": cta_id,
        "capability_top": capability_top,
        "capability_scores": {},
        "destination_top": handoff["destination"] if handoff else None,
        "handoff_status": next_action,
        "handoff_action": handoff.get("action") if handoff else None,
        "history_turns": history_turns,
        "user_message_sanitized": sanitize_user_message(clean_message),
        "user_terms_normalized": extract_user_terms_normalized(clean_message),
        "message_length": len(clean_message),
        "confidence": discovery.get("confidence"),
        "reason": discovery.get("reason"),
        "pipeline_fallback_reason": pipeline.get("fallback_reason"),
        "pipeline_gemini_called": pipeline.get("gemini_called"),
        "pipeline_parse_error": pipeline.get("parse_error"),
    }
    ident: dict[str, Any] | None = None
    try:
        ident = get_consumo_identidade()
    except RuntimeError:
        ident = None
    if ident and ident.get("tipo_origem"):
        contexto["tipo_origem"] = str(ident["tipo_origem"])[:80]
    return contexto


def _attach_onboarding_audit(
    result: dict[str, Any],
    *,
    user_message: str,
    cta_id: str | None,
    history_turns: int,
) -> dict[str, Any]:
    discovery = dict(result.get("discovery") or {})
    handoff = result.get("handoff")
    next_action = str(discovery.get("next_action") or "converse")
    audit_logged = False
    try:
        auditoria_registrar(
            tipo_decisao="onboarding_discovery",
            decisao=f"confidence={discovery.get('confidence') or 'low'}; next_action={next_action}",
            contexto=_build_onboarding_audit_context(
                user_message,
                cta_id=cta_id,
                discovery=discovery,
                handoff=handoff,
                history_turns=history_turns,
            ),
            resultado="sucesso",
        )
        audit_logged = True
    except Exception:
        logger.warning(
            "Auditoria onboarding_discovery falhou; observabilidade administrativa pode ficar incompleta.",
            exc_info=True,
        )
    discovery["audit_logged"] = audit_logged
    return {**result, "discovery": discovery}


def _log_pipeline_trace(message: str, trace: dict[str, Any]) -> None:
    logger.info(
        "Copilot pipeline | msg=%r | capabilities_loaded=%s | doc_len=%s | "
        "api_key=%s | client=%s | gemini_called=%s | parse_error=%s | "
        "fallback_reason=%s | reply_preview=%r",
        message[:80],
        trace.get("capabilities_doc_loaded"),
        trace.get("capabilities_doc_len"),
        trace.get("gemini_api_key_present"),
        trace.get("gemini_client_present"),
        trace.get("gemini_called"),
        trace.get("parse_error"),
        trace.get("fallback_reason"),
        (trace.get("reply_preview") or "")[:120],
    )


def cleiton_discovery_reply(
    user_message: str,
    history: list | None = None,
    *,
    cta_id: str | None = None,
) -> dict[str, Any]:
    """
    Processa mensagem do Copilot via Gemini governado + guardrails.
    Retorna reply natural + metadados opcionais de handoff para o frontend.
    """
    history_list = list(history) if isinstance(history, list) else []
    clean_message = (user_message or "").strip()
    history_turns = len([m for m in history_list if (m.get("role") or "").lower() == "user"]) + 1

    capabilities_doc = load_capabilities_document()
    pipeline_trace: dict[str, Any] = {
        "capabilities_doc_loaded": bool(capabilities_doc),
        "capabilities_doc_len": len(capabilities_doc),
        "gemini_api_key_present": bool(_gemini_api_key()),
        "gemini_client_present": False,
        "gemini_called": False,
        "prompt_len": 0,
        "raw_response_len": 0,
        "raw_response_preview": None,
        "parse_error": None,
        "parse_mode": None,
        "fallback_reason": None,
    }

    if not clean_message:
        empty = {
            "reply": EMPTY_MESSAGE_REPLY,
            "recommended_agent": None,
            "handoff": None,
            "handoffs": [],
            "refinement_options": [],
            "destination_candidates": [],
            "discovery": {
                "confidence": "low",
                "next_action": "converse",
                "recommended_agent": None,
                "reason": "empty_message",
                "capability_candidates": [],
                "needs_login": False,
                "pipeline": pipeline_trace,
            },
        }
        return _attach_onboarding_audit(
            empty,
            user_message="",
            cta_id=cta_id,
            history_turns=history_turns,
        )

    client = _get_client()
    pipeline_trace["gemini_client_present"] = client is not None

    if not client:
        fallback_reason = "no_gemini_key" if not _gemini_api_key() else "client_init_failed"
        pipeline_trace["fallback_reason"] = fallback_reason
        register_internal_ia_event(
            operation="onboarding_discovery_fallback",
            model="",
            agent=AGENT_CLEITON,
            flow_type=FLOW_TYPE_ONBOARDING_DISCOVERY,
            api_key_label="fallback_no_gemini" if fallback_reason == "no_gemini_key" else "client_init_failed",
            status=STATUS_SUCCESS_NO_METRICS,
        )
        logger.warning(
            "Copilot Discovery: Gemini indisponível (%s). Usando fallback local.",
            fallback_reason,
        )
        result = _local_fallback_response(clean_message, reason=fallback_reason)
        pipeline_trace["reply_preview"] = result.get("reply")
        _log_pipeline_trace(clean_message, {**pipeline_trace, **(result.get("discovery", {}).get("pipeline") or {})})
        return _attach_onboarding_audit(
            result,
            user_message=clean_message,
            cta_id=cta_id,
            history_turns=history_turns,
        )

    system_prompt = _build_system_prompt()
    user_prompt = _build_user_prompt(clean_message, history_list, cta_id=cta_id)
    contents = f"{system_prompt}\n\n---\n\n{user_prompt}"
    pipeline_trace["prompt_len"] = len(contents)

    parsed: dict[str, Any] | None = None
    last_error: Exception | None = None
    raw_response_text = ""
    for model in _get_chat_model_candidates():
        try:
            pipeline_trace["gemini_called"] = True
            response = cleiton_governed_generate_content(
                client,
                model=model,
                contents=contents,
                agent=AGENT_CLEITON,
                flow_type=FLOW_TYPE_ONBOARDING_DISCOVERY,
                api_key_label=_api_key_label(),
            )
            raw_response_text = (response.text or "").strip()
            pipeline_trace["raw_response_len"] = len(raw_response_text)
            pipeline_trace["raw_response_preview"] = raw_response_text[:240]
            parsed, parse_error = _parse_gemini_response(raw_response_text)
            pipeline_trace["parse_error"] = parse_error
            if parsed:
                pipeline_trace["parse_mode"] = "json" if parse_error is None else parse_error
                break
            last_error = ValueError(parse_error or "Resposta JSON vazia ou inválida")
        except Exception as e:
            last_error = e
            pipeline_trace["parse_error"] = str(e)[:200]
            logger.warning("Copilot Discovery modelo %s: %s", model, e)

    if not parsed:
        if last_error:
            logger.exception("Copilot Discovery falhou: %s", last_error)
        pipeline_trace["fallback_reason"] = "gemini_parse_failed"
        result = _local_fallback_response(clean_message, reason="gemini_parse_failed")
        pipeline_trace["reply_preview"] = result.get("reply")
        discovery = dict(result.get("discovery") or {})
        discovery["pipeline"] = {**(discovery.get("pipeline") or {}), **pipeline_trace}
        result["discovery"] = discovery
        _log_pipeline_trace(clean_message, pipeline_trace)
        return _attach_onboarding_audit(
            result,
            user_message=clean_message,
            cta_id=cta_id,
            history_turns=history_turns,
        )

    result = _apply_guardrails(parsed, clean_message, pipeline=pipeline_trace)
    pipeline_trace["reply_preview"] = result.get("reply")
    _log_pipeline_trace(clean_message, pipeline_trace)
    return _attach_onboarding_audit(
        result,
        user_message=clean_message,
        cta_id=cta_id,
        history_turns=history_turns,
    )
