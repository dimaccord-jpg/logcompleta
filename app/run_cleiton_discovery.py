"""
Cleiton Discovery AI — descoberta de intenção e handoff para onboarding inteligente.

Fluxo: intenção → capability candidates → ranking → refinamento → destination ranking → handoff.
Toda chamada LLM passa por run_cleiton_gemini_governance (agent=cleiton, flow_type=onboarding_discovery).
Cleiton NÃO resolve o problema operacional; apenas descobre, refina e encaminha.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from app.capability_taxonomy import (
    CAPABILITY_DOMAINS_MVP,
    CAPABILITY_MODES,
    CTA_BY_ID,
    DESTINATIONS,
    DOMAIN_LABELS,
    MODE_LABELS,
    compute_confidence_level,
    decide_next_action,
    get_capability_availability,
    get_cta_by_id,
    is_ambiguous_logistics_intent,
    is_clear_editorial_market_intent,
    is_clear_forecast_planning_intent,
    is_clear_freight_bi_intent,
    is_clear_operational_audit_intent,
    is_future_capability_intent,
    rank_destinations_from_capabilities,
)
from app.consumo_identidade import get_consumo_identidade
from app.run_cleiton_gemini_governance import cleiton_governed_generate_content
from app.run_cleiton_agente_auditoria import registrar as auditoria_registrar
from app.utils.onboarding_text_normalization import (
    extract_user_terms_normalized,
    sanitize_user_message,
)

logger = logging.getLogger(__name__)

CLEITON_DISCOVERY_SYSTEM_PROMPT = """
Você é o Cleiton, camada de descoberta inteligente do Agentefrete.

Sua função NÃO é resolver o problema operacional do usuário.
Você deve interpretar a intenção, reconhecer o caminho mais provável e encaminhar com assertividade quando houver clareza.

REGRAS DE RESPOSTA (obrigatórias):
- NUNCA use a mesma resposta genérica para intenções diferentes.
- NUNCA liste todos os caminhos quando a intenção for clara.
- NUNCA prometa funcionalidade futura como se já existisse.
- Respostas curtas, contextuais e consultivas em português BR.
- NUNCA encaminhe diretamente para agentes (Roberto, Cleide, Júlia) na resposta; fale em capabilities e caminhos de trabalho.

MATRIZ DE CAPABILITIES (MVP):
- freight_bi (disponível): BI, custo, indicadores, dados de frete, dashboard → caminho Fretes/BI operacional.
- forecast_planning (disponível): previsão, previsibilidade, comportamento de frete → caminho Fretes/previsibilidade.
- operational_audit (disponível): auditoria, investigação, anomalia, transportadora, desvio, concentração → caminho Auditoria operacional.
- strategic_logistics (disponível): estratégia, planejamento amplo, logística, supply chain, negociação consultiva → caminho Consultoria estratégica.
- editorial_market (disponível): notícias, tendências, mercado, artigos → caminho Feed.

CAPABILITIES FUTURAS (ainda NÃO disponíveis — seja honesto):
- future_quotation: cotação de frete → informar indisponibilidade e oferecer alternativas reais (freight_bi, forecast_planning, strategic_logistics).
- future_bid: BID de frete → informar indisponibilidade e oferecer alternativas reais.

QUANDO A INTENÇÃO FOR CLARA E EXISTIR NO PRODUTO:
- Reconheça a intenção explicitamente.
- Sugira o caminho mais provável (um principal).
- Ofereça CTA direto na reply.
- NÃO liste todas as opções.
- NÃO peça contexto só para descobrir destino.
- Exemplos claros: "quero previsão de frete", "preciso de BI de frete", "analisar custo de frete", "quero auditar minha operação", "quero notícias".

QUANDO A INTENÇÃO FOR AMBÍGUA:
- Ofereça 2 a 4 hipóteses (BI, auditoria, previsibilidade, estratégia).
- Peça refinamento; não finja certeza.
- Exemplos ambíguos: "quero reduzir custo", "quero melhorar logística", "minha operação está ruim", "quero otimizar frete".

QUANDO A FUNCIONALIDADE NÃO EXISTIR (ex.: cotação de frete):
- Diga claramente que ainda não está disponível no AgenteFrete.
- Ofereça alternativas existentes: BI de frete, análise de custo, previsibilidade ou estratégia logística.
- NÃO prometa cotação automatizada.

Modos de capability: discover, explain, analyze, audit, forecast, generate_exec_output

Responda SEMPRE com um único JSON válido (sem markdown, sem texto fora do JSON) neste formato:
{
  "reply": "texto contextual curto para o usuário",
  "capability_candidates": [
    {"domain": "freight_bi", "mode": "analyze", "score": 88, "rationale": "breve"}
  ],
  "refinement_options": ["opção curta 1", "opção curta 2"],
  "user_confirmed": false,
  "suggested_capability_mode": "analyze"
}

Regras do JSON:
- capability_candidates: 2 a 5 itens, scores decrescentes; domain deve ser domínio MVP ou future_* quando aplicável.
- Para intenção clara disponível: top score alto (>= 85), gap claro vs segundo candidato, refinement_options vazio.
- Para intenção ambígua: scores próximos, refinement_options com 2 a 4 hipóteses.
- Para future_quotation/future_bid: incluir domínio future_* no topo; refinement_options com alternativas reais.
- user_confirmed: true apenas se o usuário escolheu explicitamente um caminho na última mensagem.
""".strip()

GENERIC_DISCOVERY_REPLY_PATTERN = re.compile(
    r"Existem algumas formas de trabalhar esse tema",
    re.IGNORECASE,
)

CONFIRMATION_PATTERNS = re.compile(
    r"\b(sim|confirmo|esse caminho|quero (?:o|a|esse|essa)|pode ser|vamos (?:com|para|de)|"
    r"prefiro|escolho|bi operacional|auditoria|previsib|estrat[eé]gia|not[ií]cias|feed)\b",
    re.IGNORECASE,
)


def _api_key_label() -> str:
    if os.getenv("GEMINI_API_KEY_1"):
        return "GEMINI_API_KEY_1"
    if os.getenv("GEMINI_API_KEY"):
        return "GEMINI_API_KEY"
    return "unknown"


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
        return genai.Client(api_key=key, http_options=genai_types.HttpOptions(timeout_ms=timeout_ms))
    except Exception as e:
        logger.error("Cleiton Discovery: falha ao inicializar cliente Gemini: %s", e)
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


def _normalize_candidates(raw: list | None, seed_domains: list[str] | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen_domains: set[str] = set()
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        domain = str(item.get("domain") or "").strip()
        if domain not in CAPABILITY_DOMAINS_MVP:
            continue
        mode = str(item.get("mode") or "discover").strip()
        if mode not in CAPABILITY_MODES:
            mode = "discover"
        try:
            score = float(item.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        score = max(0.0, min(100.0, score))
        seen_domains.add(domain)
        out.append({
            "domain": domain,
            "mode": mode,
            "score": round(score, 1),
            "label": DOMAIN_LABELS.get(domain, domain),
            "mode_label": MODE_LABELS.get(mode, mode),
            "rationale": (item.get("rationale") or "").strip()[:200],
        })
    out.sort(key=lambda x: x["score"], reverse=True)

    if seed_domains and len(out) < 2:
        for i, domain in enumerate(seed_domains):
            if domain in seen_domains or domain not in CAPABILITY_DOMAINS_MVP:
                continue
            out.append({
                "domain": domain,
                "mode": "discover",
                "score": round(max(40.0, 65.0 - i * 8), 1),
                "label": DOMAIN_LABELS.get(domain, domain),
                "mode_label": MODE_LABELS.get("discover", "discover"),
                "rationale": "Sugerido pelo contexto inicial",
            })
        out.sort(key=lambda x: x["score"], reverse=True)
    return out[:5]


def _build_discovery_prompt(
    user_message: str,
    history: list,
    *,
    cta_id: str | None = None,
    seed_domains: list[str] | None = None,
) -> str:
    parts = [CLEITON_DISCOVERY_SYSTEM_PROMPT, "\n\n---\n\nContexto da conversa:\n"]
    if cta_id and cta_id in CTA_BY_ID:
        parts.append(f"Entrada via CTA da Home (id={cta_id}) — tratar como hipótese inicial, não como destino final.\n")
    if seed_domains:
        parts.append(f"Domínios seed (candidatos iniciais): {', '.join(seed_domains)}\n\n")
    for msg in history[-8:]:
        role = (msg.get("role") or "user").lower()
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        label = "Usuário" if role == "user" else "Cleiton"
        parts.append(f"{label}: {content}\n\n")
    parts.append(f"Usuário: {user_message.strip()}\n\n")
    parts.append("Responda apenas com o JSON especificado.")
    return "".join(parts)


def _detect_user_confirmed(user_message: str, llm_confirmed: bool) -> bool:
    if llm_confirmed:
        return True
    return bool(CONFIRMATION_PATTERNS.search(user_message or ""))


def _build_handoff_payload(
    top_dest: dict[str, Any],
    top_cap: dict[str, Any],
    *,
    suggested_mode: str | None = None,
) -> dict[str, Any]:
    dest_id = top_dest["destination"]
    spec = DESTINATIONS.get(dest_id)
    action = top_dest.get("handoff_action") or (spec.handoff_action if spec else None)
    label = top_dest.get("label") or (spec.label if spec else dest_id)
    payload: dict[str, Any] = {
        "destination": dest_id,
        "label": label,
        "requires_login": False,
        "requires_dataset": top_dest.get("requires_dataset", False),
        "capability_domain": top_cap["domain"],
        "capability_mode": top_cap.get("mode") or suggested_mode or "discover",
    }
    if action == "start_julia":
        payload["action"] = "start_julia"
        payload["url"] = None
    else:
        payload["url"] = top_dest.get("url")
    return payload


def _infer_seed_domains_for_message(
    user_message: str,
    seed_domains: list[str] | None,
    *,
    cta_id: str | None = None,
) -> list[str]:
    future_domain = is_future_capability_intent(user_message, cta_id=cta_id)
    if future_domain:
        alts = get_capability_availability(future_domain).get("alternatives") or []
        return list(alts[:3]) or ["freight_bi", "forecast_planning", "strategic_logistics"]
    if is_clear_editorial_market_intent(user_message, cta_id=cta_id):
        return ["editorial_market"]
    if is_clear_forecast_planning_intent(user_message, cta_id=cta_id):
        return ["forecast_planning", "freight_bi"]
    if is_clear_freight_bi_intent(user_message, cta_id=cta_id):
        return ["freight_bi", "forecast_planning"]
    if is_clear_operational_audit_intent(user_message, cta_id=cta_id):
        return ["operational_audit", "freight_bi"]
    return seed_domains or list(CAPABILITY_DOMAINS_MVP)[:4]


def _build_contextual_discovery_reply(
    user_message: str,
    *,
    cta_id: str | None = None,
    next_action: str | None = None,
    top_domain: str | None = None,
) -> tuple[str, list[str]]:
    future_domain = is_future_capability_intent(user_message, cta_id=cta_id)
    if future_domain == "future_quotation":
        return (
            "Ainda não temos cotação automatizada de frete disponível no AgenteFrete. "
            "Posso, porém, te ajudar a analisar custos atuais, entender comportamento de frete "
            "ou preparar uma visão estratégica para apoiar negociações.",
            [
                "Quero analisar custos e indicadores de frete",
                "Quero previsibilidade e comportamento de frete",
                "Busco apoio estratégico para negociação",
            ],
        )
    if future_domain == "future_bid":
        return (
            "Ainda não temos módulo de BID de frete disponível no AgenteFrete. "
            "Posso te orientar com BI de frete, previsibilidade ou estratégia logística "
            "para preparar sua operação.",
            [
                "Quero analisar custos e indicadores de frete",
                "Quero previsibilidade e comportamento de frete",
                "Busco apoio estratégico logística",
            ],
        )
    if is_clear_editorial_market_intent(user_message, cta_id=cta_id, top_domain=top_domain):
        return (
            "Posso te levar direto ao feed de notícias e tendências do mercado logístico.",
            [],
        )
    if is_clear_forecast_planning_intent(user_message, cta_id=cta_id, top_domain=top_domain):
        return (
            "Entendi que você quer trabalhar previsibilidade e comportamento de frete. "
            "O caminho mais direto é o módulo de Fretes, com indicadores e visão de previsão "
            "para apoiar seu planejamento.",
            [],
        )
    if is_clear_freight_bi_intent(user_message, cta_id=cta_id, top_domain=top_domain):
        return (
            "Pelo que você descreveu, faz sentido começar pelo BI de fretes: custos, indicadores "
            "e dados operacionais para analisar sua base de frete.",
            [],
        )
    if is_clear_operational_audit_intent(user_message, cta_id=cta_id, top_domain=top_domain):
        return (
            "Parece que você precisa investigar a operação — auditoria, anomalias ou desvios "
            "na base de fretes. Posso te encaminhar para o caminho de auditoria operacional.",
            [],
        )
    if is_ambiguous_logistics_intent(user_message):
        return (
            "Esse tema pode ser trabalhado de formas diferentes no Agentefrete.\n\n"
            "Algumas hipóteses:\n\n"
            "📊 BI operacional de fretes\n"
            "🔎 auditoria operacional\n"
            "📈 previsibilidade e comportamento de frete\n"
            "🧠 estratégia logística\n\n"
            "Qual delas se aproxima mais do que você precisa agora?",
            [
                "Quero analisar custos e indicadores com dados",
                "Preciso investigar anomalias na operação",
                "Busco previsibilidade de frete",
                "Quero visão estratégica logística",
            ],
        )
    return (
        "Existem algumas formas de trabalhar esse tema no Agentefrete.\n\n"
        "Posso ajudar com:\n\n"
        "📊 BI operacional de fretes\n"
        "🔎 auditoria operacional\n"
        "📈 previsibilidade e planejamento\n"
        "🧠 estratégia logística\n"
        "📰 notícias e tendências\n\n"
        "Me conte mais sobre o que você quer analisar.",
        [
            "Quero analisar custos e indicadores com dados",
            "Preciso investigar anomalias na operação",
            "Busco visão estratégica e planejamento",
            "Só quero acompanhar o mercado",
        ],
    )


def _maybe_contextualize_reply(
    reply: str,
    user_message: str,
    *,
    cta_id: str | None = None,
    next_action: str | None = None,
    top_domain: str | None = None,
) -> str:
    if reply and not GENERIC_DISCOVERY_REPLY_PATTERN.search(reply):
        return reply
    contextual, _ = _build_contextual_discovery_reply(
        user_message,
        cta_id=cta_id,
        next_action=next_action,
        top_domain=top_domain,
    )
    return contextual


def _build_onboarding_audit_context(
    user_message: str,
    *,
    cta_id: str | None,
    candidates: list[dict[str, Any]],
    handoff: dict[str, Any] | None,
    next_action: str,
    history_turns: int,
) -> dict[str, Any]:
    clean_message = (user_message or "").strip()
    contexto: dict[str, Any] = {
        "cta_id": cta_id,
        "capability_top": candidates[0]["domain"] if candidates else None,
        "capability_scores": {c["domain"]: c["score"] for c in candidates[:3]},
        "destination_top": handoff["destination"] if handoff else None,
        "handoff_status": next_action,
        "handoff_action": handoff.get("action") if handoff else None,
        "history_turns": history_turns,
        "user_message_sanitized": sanitize_user_message(clean_message),
        "user_terms_normalized": extract_user_terms_normalized(clean_message),
        "message_length": len(clean_message),
    }
    ident: dict[str, Any] | None = None
    try:
        ident = get_consumo_identidade()
    except RuntimeError:
        ident = None
    if ident and ident.get("tipo_origem"):
        contexto["tipo_origem"] = str(ident["tipo_origem"])[:80]
    return contexto


def _fallback_discovery_reply(
    user_message: str,
    seed_domains: list[str] | None,
    *,
    cta_id: str | None = None,
) -> dict[str, Any]:
    domains = _infer_seed_domains_for_message(user_message, seed_domains, cta_id=cta_id)
    future_domain = is_future_capability_intent(user_message, cta_id=cta_id)
    if future_domain:
        candidates = _normalize_candidates(
            [
                {"domain": d, "mode": "discover", "score": 82 - i * 6}
                for i, d in enumerate(domains[:3])
            ],
            seed_domains=domains,
        )
    else:
        candidates = _normalize_candidates(
            [{"domain": d, "mode": "discover", "score": 90 - i * 14} for i, d in enumerate(domains[:2])],
            seed_domains=domains,
        )
    confidence = compute_confidence_level(candidates)
    top_domain = candidates[0]["domain"] if candidates else None
    history_turns = 1
    next_action = decide_next_action(
        confidence,
        history_turns=history_turns,
        top_capability_domain=top_domain,
        user_message=user_message,
        cta_id=cta_id,
    )
    destination_candidates = rank_destinations_from_capabilities(candidates)
    handoff = None
    if next_action == "handoff" and destination_candidates and not future_domain:
        handoff = _build_handoff_payload(destination_candidates[0], candidates[0])

    reply, refinement_options = _build_contextual_discovery_reply(
        user_message,
        cta_id=cta_id,
        next_action=next_action,
        top_domain=top_domain,
    )

    discovery_extra: dict[str, Any] = {}
    if future_domain:
        discovery_extra["future_capability"] = future_domain
        discovery_extra["availability"] = get_capability_availability(future_domain)

    return {
        "reply": reply,
        "discovery": {
            "capability_candidates": candidates,
            "confidence": confidence,
            "next_action": next_action,
            **discovery_extra,
        },
        "destination_candidates": destination_candidates,
        "handoff": handoff,
        "refinement_options": refinement_options,
    }


def cleiton_discovery_reply(
    user_message: str,
    history: list | None = None,
    *,
    cta_id: str | None = None,
) -> dict[str, Any]:
    """
    Processa uma mensagem de descoberta via Cleiton + Gemini governado.
    Retorna reply natural + metadados de discovery/handoff para o frontend.
    """
    history_list = list(history) if isinstance(history, list) else []
    clean_message = (user_message or "").strip()
    if not clean_message:
        return _fallback_discovery_reply("", None, cta_id=cta_id)

    cta = get_cta_by_id(cta_id)
    seed_domains = list(cta.get("seed_domains") or []) if cta else None

    client = _get_client()
    if not client:
        logger.warning("Cleiton Discovery: Gemini não configurado.")
        return _fallback_discovery_reply(clean_message, seed_domains, cta_id=cta_id)

    contents = _build_discovery_prompt(
        clean_message,
        history_list,
        cta_id=cta_id,
        seed_domains=seed_domains,
    )

    parsed: dict[str, Any] | None = None
    last_error: Exception | None = None
    for model in _get_chat_model_candidates():
        try:
            response = cleiton_governed_generate_content(
                client,
                model=model,
                contents=contents,
                agent="cleiton",
                flow_type="onboarding_discovery",
                api_key_label=_api_key_label(),
            )
            parsed = _extract_json_object((response.text or "").strip())
            if parsed:
                break
            last_error = ValueError("Resposta JSON vazia ou inválida")
        except Exception as e:
            last_error = e
            logger.warning("Cleiton Discovery modelo %s: %s", model, e)

    if not parsed:
        if last_error:
            logger.exception("Cleiton Discovery falhou: %s", last_error)
        return _fallback_discovery_reply(clean_message, seed_domains, cta_id=cta_id)

    candidates = _normalize_candidates(parsed.get("capability_candidates"), seed_domains=seed_domains)
    if not candidates:
        return _fallback_discovery_reply(clean_message, seed_domains, cta_id=cta_id)

    confidence = compute_confidence_level(candidates)
    user_confirmed = _detect_user_confirmed(
        clean_message,
        bool(parsed.get("user_confirmed")),
    )
    history_turns = len([m for m in history_list if (m.get("role") or "").lower() == "user"]) + 1
    top_domain = candidates[0]["domain"] if candidates else None
    next_action = decide_next_action(
        confidence,
        user_confirmed=user_confirmed,
        history_turns=history_turns,
        top_capability_domain=top_domain,
        user_message=clean_message,
        cta_id=cta_id,
    )

    destination_candidates = rank_destinations_from_capabilities(candidates)
    handoff = None
    future_domain = is_future_capability_intent(clean_message, cta_id=cta_id)
    if next_action == "handoff" and destination_candidates and not future_domain:
        handoff = _build_handoff_payload(
            destination_candidates[0],
            candidates[0],
            suggested_mode=parsed.get("suggested_capability_mode"),
        )

    refinement_options = parsed.get("refinement_options") or []
    if not isinstance(refinement_options, list):
        refinement_options = []
    refinement_options = [str(o).strip() for o in refinement_options if str(o).strip()][:4]

    reply = (parsed.get("reply") or "").strip()
    if not reply:
        fallback = _fallback_discovery_reply(clean_message, seed_domains, cta_id=cta_id)
        reply = fallback["reply"]
        if not refinement_options:
            refinement_options = fallback.get("refinement_options") or []
    else:
        reply = _maybe_contextualize_reply(
            reply,
            clean_message,
            cta_id=cta_id,
            next_action=next_action,
            top_domain=top_domain,
        )

    if future_domain and not refinement_options:
        _, refinement_options = _build_contextual_discovery_reply(
            clean_message,
            cta_id=cta_id,
            next_action=next_action,
            top_domain=top_domain,
        )

    discovery_payload: dict[str, Any] = {
        "capability_candidates": candidates,
        "confidence": confidence,
        "next_action": next_action,
    }
    if future_domain:
        discovery_payload["future_capability"] = future_domain
        discovery_payload["availability"] = get_capability_availability(future_domain)

    try:
        auditoria_registrar(
            tipo_decisao="onboarding_discovery",
            decisao=f"confidence={confidence}; next_action={next_action}",
            contexto=_build_onboarding_audit_context(
                clean_message,
                cta_id=cta_id,
                candidates=candidates,
                handoff=handoff,
                next_action=next_action,
                history_turns=history_turns,
            ),
            resultado="sucesso",
        )
    except Exception:
        logger.debug("Auditoria onboarding_discovery ignorada (sem persistência).", exc_info=True)

    return {
        "reply": reply,
        "discovery": discovery_payload,
        "destination_candidates": destination_candidates,
        "handoff": handoff,
        "refinement_options": refinement_options,
    }
