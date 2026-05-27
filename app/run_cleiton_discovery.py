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
    get_cta_by_id,
    is_clear_editorial_market_intent,
    rank_destinations_from_capabilities,
)
from app.run_cleiton_gemini_governance import cleiton_governed_generate_content
from app.run_cleiton_agente_auditoria import registrar as auditoria_registrar

logger = logging.getLogger(__name__)

CLEITON_DISCOVERY_SYSTEM_PROMPT = """
Você é o Cleiton, camada de descoberta inteligente do Agentefrete.

Sua função NÃO é resolver o problema operacional do usuário.
Você deve:
1. Interpretar a intenção do usuário em linguagem natural.
2. Levantar hipóteses de capability (domínio + modo).
3. Refinar quando a intenção for ambígua, com opções claras em português.
4. Ranquear capabilities com score interno (0-100).
5. Preparar handoff apenas quando houver clareza suficiente.

REGRA CRÍTICA: NUNCA encaminhe diretamente para agentes (Roberto, Cleide, Júlia) na resposta.
Fale em termos de capabilities e caminhos de trabalho, não em nomes de agentes como destino imediato.

Domínios de capability disponíveis (MVP):
- editorial_market: notícias, tendências, artigos de mercado logístico
- strategic_logistics: estratégia, supply chain ampla, consultoria logística
- freight_bi: BI de fretes, indicadores, dashboards, custo baseado em dados
- operational_audit: auditoria operacional, anomalias, concentração, investigação
- forecast_planning: previsibilidade, comportamento de frete, planejamento

Modos de capability:
- discover, explain, analyze, audit, forecast, generate_exec_output

Quando a intenção for ambígua (ex.: "quero reduzir custo"), apresente opções como:
📊 BI operacional
🔎 auditoria operacional
📈 previsibilidade e comportamento de frete
🧠 estratégia logística

Peça mais contexto antes de sugerir handoff definitivo.

Responda SEMPRE com um único JSON válido (sem markdown, sem texto fora do JSON) neste formato:
{
  "reply": "texto em linguagem natural para o usuário (tom consultivo, português BR)",
  "capability_candidates": [
    {"domain": "freight_bi", "mode": "analyze", "score": 74, "rationale": "breve"}
  ],
  "refinement_options": ["opção curta 1", "opção curta 2"],
  "user_confirmed": false,
  "suggested_capability_mode": "analyze"
}

Regras do JSON:
- capability_candidates: 2 a 5 itens, scores decrescentes, domain deve ser um dos domínios MVP.
- refinement_options: 2 a 4 opções clicáveis quando next_action seria refine; vazio se handoff claro.
- user_confirmed: true apenas se o usuário escolheu explicitamente um caminho na última mensagem.
- reply: natural, sem mencionar JSON, scores ou agentes como destino direto.
""".strip()

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


def _fallback_discovery_reply(
    user_message: str,
    seed_domains: list[str] | None,
    *,
    cta_id: str | None = None,
) -> dict[str, Any]:
    domains = seed_domains or list(CAPABILITY_DOMAINS_MVP)[:4]
    if is_clear_editorial_market_intent(user_message, cta_id=cta_id):
        domains = ["editorial_market"]
    candidates = _normalize_candidates(
        [{"domain": d, "mode": "discover", "score": 90 - i * 5} for i, d in enumerate(domains[:4])],
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
    if next_action == "handoff" and destination_candidates:
        handoff = _build_handoff_payload(destination_candidates[0], candidates[0])

    if is_clear_editorial_market_intent(user_message, cta_id=cta_id, top_domain=top_domain):
        reply = "Posso te levar direto ao feed de notícias e tendências do mercado logístico."
        refinement_options: list[str] = []
    else:
        reply = (
            "Existem algumas formas de trabalhar esse tema no Agentefrete.\n\n"
            "Posso ajudar com:\n\n"
            "📊 BI operacional de fretes\n"
            "🔎 auditoria operacional\n"
            "📈 previsibilidade e planejamento\n"
            "🧠 estratégia logística\n"
            "📰 notícias e tendências\n\n"
            "Me conte mais sobre o que você quer analisar."
        )
        refinement_options = [
            "Quero analisar custos e indicadores com dados",
            "Preciso investigar anomalias na operação",
            "Busco visão estratégica e planejamento",
            "Só quero acompanhar o mercado",
        ]

    return {
        "reply": reply,
        "discovery": {
            "capability_candidates": candidates,
            "confidence": confidence,
            "next_action": next_action,
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
    if next_action == "handoff" and destination_candidates:
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
        reply = _fallback_discovery_reply(clean_message, seed_domains, cta_id=cta_id)["reply"]

    discovery_payload = {
        "capability_candidates": candidates,
        "confidence": confidence,
        "next_action": next_action,
    }

    try:
        auditoria_registrar(
            tipo_decisao="onboarding_discovery",
            decisao=f"confidence={confidence}; next_action={next_action}",
            contexto={
                "cta_id": cta_id,
                "capability_top": candidates[0]["domain"] if candidates else None,
                "capability_scores": {c["domain"]: c["score"] for c in candidates[:3]},
                "destination_top": handoff["destination"] if handoff else None,
                "handoff_status": next_action,
                "handoff_action": handoff.get("action") if handoff else None,
                "history_turns": history_turns,
            },
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
