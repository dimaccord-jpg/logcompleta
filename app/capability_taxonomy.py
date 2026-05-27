"""
Taxonomia extensível de capabilities para onboarding inteligente (Cleiton Discovery).

Domínios MVP ativos; domínios futuros preparados mas não expostos ao roteamento.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import re

# --- Domínios MVP ---
CAPABILITY_DOMAINS_MVP = frozenset({
    "editorial_market",
    "strategic_logistics",
    "freight_bi",
    "operational_audit",
    "forecast_planning",
})

# Preparados, não implementados
CAPABILITY_DOMAINS_FUTURE = frozenset({
    "future_bid",
    "future_quotation",
    "future_supply_planning",
})

# --- Modos extensíveis ---
CAPABILITY_MODES = frozenset({
    "discover",
    "explain",
    "analyze",
    "audit",
    "forecast",
    "generate_exec_output",
})

DOMAIN_LABELS: dict[str, str] = {
    "editorial_market": "Notícias e tendências de mercado",
    "strategic_logistics": "Estratégia e consultoria logística",
    "freight_bi": "BI operacional de fretes e indicadores",
    "operational_audit": "Auditoria operacional de fretes",
    "forecast_planning": "Previsibilidade e planejamento de frete",
    "future_quotation": "Cotação automatizada de frete",
    "future_bid": "BID de frete",
    "future_supply_planning": "Planejamento de supply chain",
}

# Disponibilidade real no produto (MVP + futuras)
CAPABILITY_AVAILABILITY: dict[str, dict[str, Any]] = {
    "editorial_market": {"available": True, "status": "available", "alternatives": []},
    "strategic_logistics": {"available": True, "status": "available", "alternatives": []},
    "freight_bi": {"available": True, "status": "available", "alternatives": []},
    "operational_audit": {"available": True, "status": "available", "alternatives": []},
    "forecast_planning": {"available": True, "status": "available", "alternatives": []},
    "future_quotation": {
        "available": False,
        "status": "future",
        "alternatives": ["freight_bi", "forecast_planning", "strategic_logistics"],
    },
    "future_bid": {
        "available": False,
        "status": "future",
        "alternatives": ["freight_bi", "forecast_planning", "strategic_logistics"],
    },
    "future_supply_planning": {
        "available": False,
        "status": "future",
        "alternatives": ["forecast_planning", "strategic_logistics"],
    },
}

MODE_LABELS: dict[str, str] = {
    "discover": "Descobrir oportunidades",
    "explain": "Explicar conceitos e contexto",
    "analyze": "Analisar dados e indicadores",
    "audit": "Auditar operação",
    "forecast": "Prever comportamento",
    "generate_exec_output": "Gerar saída executiva",
}


@dataclass(frozen=True)
class DestinationSpec:
    id: str
    label: str
    url: str | None
    requires_login: bool  # login no handoff do onboarding (sempre False na navegação)
    requires_dataset: bool
    agent: str | None  # None = superfície editorial; agente só no handoff operacional
    handoff_action: str | None = None  # ex.: start_julia = permanece no shell da Home


DESTINATIONS: dict[str, DestinationSpec] = {
    "feed": DestinationSpec(
        id="feed",
        label="Feed de notícias e tendências",
        url="/feed",
        requires_login=False,
        requires_dataset=False,
        agent=None,
    ),
    "julia_operational": DestinationSpec(
        id="julia_operational",
        label="Conversar com a Júlia",
        url="/chat_julia?mode=operational",
        requires_login=False,
        requires_dataset=False,
        agent="julia",
        handoff_action="start_julia",
    ),
    "roberto_bi": DestinationSpec(
        id="roberto_bi",
        label="BI e indicadores de frete (Roberto)",
        url="/fretes",
        requires_login=False,
        requires_dataset=True,
        agent="roberto",
    ),
    "cleide_audit": DestinationSpec(
        id="cleide_audit",
        label="Auditoria operacional de frete (Cleide)",
        url="/auditoria-frete",
        requires_login=False,
        requires_dataset=True,
        agent="cleide",
    ),
}

# Domínio → destinos candidatos (ranking inicial; Cleiton refina antes do handoff)
DOMAIN_DESTINATION_CANDIDATES: dict[str, list[str]] = {
    "editorial_market": ["feed"],
    "strategic_logistics": ["julia_operational"],
    "freight_bi": ["roberto_bi"],
    "operational_audit": ["cleide_audit"],
    "forecast_planning": ["roberto_bi"],
}

# CTAs da Home: geram candidates, NÃO definem destino final
ONBOARDING_CTAS: list[dict[str, Any]] = [
    {
        "id": "reduce_cost",
        "label": "📉 Reduzir custos e encontrar oportunidades",
        "seed_domains": ["freight_bi", "operational_audit", "strategic_logistics", "forecast_planning"],
        "seed_message": "Quero reduzir custos e encontrar oportunidades na operação logística.",
    },
    {
        "id": "understand_freight",
        "label": "📊 Entender fretes e indicadores",
        "seed_domains": ["freight_bi", "forecast_planning"],
        "seed_message": "Quero entender meus fretes e indicadores.",
    },
    {
        "id": "forecast_planning",
        "label": "📈 Previsibilidade e planejamento",
        "seed_domains": ["forecast_planning", "freight_bi", "strategic_logistics"],
        "seed_message": "Quero trabalhar previsibilidade e planejamento de frete.",
    },
    {
        "id": "operational_audit",
        "label": "🔎 Auditoria operacional",
        "seed_domains": ["operational_audit", "freight_bi"],
        "seed_message": "Preciso de auditoria operacional na minha base de fretes.",
    },
    {
        "id": "strategic_support",
        "label": "🧠 Apoio estratégico logística",
        "seed_domains": ["strategic_logistics", "forecast_planning"],
        "seed_message": "Busco apoio estratégico em logística e supply chain.",
    },
    {
        "id": "market_news",
        "label": "📰 Notícias e tendências",
        "seed_domains": ["editorial_market"],
        "seed_message": "Quero acompanhar notícias e tendências do mercado logístico.",
    },
    {
        "id": "not_sure",
        "label": "✨ Não sei por onde começar",
        "seed_domains": list(CAPABILITY_DOMAINS_MVP),
        "seed_message": "Não sei por onde começar — preciso de ajuda para descobrir o melhor caminho.",
    },
]

CTA_BY_ID: dict[str, dict[str, Any]] = {c["id"]: c for c in ONBOARDING_CTAS}

CONFIDENCE_HANDOFF_THRESHOLD = 78
CONFIDENCE_REFINE_THRESHOLD = 55
TOP_GAP_FOR_HANDOFF = 12

EDITORIAL_CLEAR_INTENT = re.compile(
    r"\b(not[ií]cias?|tend[eê]ncias?|mercado|feed|artigos?|editorial|"
    r"acompanhar\s+o\s+mercado|mercado\s+log[ií]stico)\b",
    re.IGNORECASE,
)

FREIGHT_BI_CLEAR_INTENT = re.compile(
    r"\b("
    r"bi\b|business\s+intelligence|dashboard|indicadores?|"
    r"custos?\s+(?:de\s+)?frete|dados?\s+(?:de\s+)?frete|"
    r"analisar\s+(?:meu\s+|o\s+)?custo|custo\s+de\s+frete|"
    r"bi\s+(?:de\s+|para\s+)?frete"
    r")\b",
    re.IGNORECASE,
)

FORECAST_CLEAR_INTENT = re.compile(
    r"\b("
    r"previs[aã]o|previsibilidade|prever|comportamento\s+(?:do\s+)?frete|"
    r"proje[cç][aã]o\s+(?:de\s+)?frete|prever\s+(?:o\s+)?frete"
    r")\b",
    re.IGNORECASE,
)

OPERATIONAL_AUDIT_CLEAR_INTENT = re.compile(
    r"\b("
    r"auditar|auditoria|investigar|anomali|desvio|concentra[cç][aã]o|"
    r"transportadora\s+(?:est[aá]|cara|ruim|cara)"
    r")\b",
    re.IGNORECASE,
)

STRATEGIC_CLEAR_INTENT = re.compile(
    r"\b("
    r"estrat[eé]gia\s+log[ií]stica|supply\s+chain|consultoria\s+log[ií]stica|"
    r"planejamento\s+estrat[eé]gico|negocia[cç][aã]o\s+consultiva"
    r")\b",
    re.IGNORECASE,
)

FUTURE_QUOTATION_INTENT = re.compile(
    r"\b(cota[cç][aã]o|cota[cç]ionar|cotar\s+frete|pre[cç]o\s+de\s+frete)\b",
    re.IGNORECASE,
)

FUTURE_BID_INTENT = re.compile(
    r"\b(\bbid\b|licita[cç][aã]o\s+de\s+frete)\b",
    re.IGNORECASE,
)

AMBIGUOUS_LOGISTICS_INTENT = re.compile(
    r"\b("
    r"reduzir\s+custo|melhorar\s+(?:minha\s+)?log[ií]stica|otimizar\s+(?:frete|opera[cç][aã]o)|"
    r"opera[cç][aã]o\s+est[aá]\s+ruim|minha\s+opera[cç][aã]o|"
    r"transportadora\s+est[aá]\s+cara"
    r")\b",
    re.IGNORECASE,
)


def get_capability_availability(domain: str) -> dict[str, Any]:
    spec = CAPABILITY_AVAILABILITY.get(domain)
    if spec:
        return dict(spec)
    return {"available": True, "status": "available", "alternatives": []}


def is_future_capability_intent(
    message: str,
    *,
    cta_id: str | None = None,
) -> str | None:
    """Retorna domínio futuro detectado (ex.: future_quotation) ou None."""
    text = (message or "").strip()
    if not text:
        return None
    if FUTURE_QUOTATION_INTENT.search(text):
        return "future_quotation"
    if FUTURE_BID_INTENT.search(text):
        return "future_bid"
    return None


def is_clear_freight_bi_intent(
    message: str,
    *,
    cta_id: str | None = None,
    top_domain: str | None = None,
) -> bool:
    if cta_id == "understand_freight":
        return True
    text = (message or "").strip()
    if not text or is_future_capability_intent(text):
        return False
    if FREIGHT_BI_CLEAR_INTENT.search(text):
        return True
    return top_domain == "freight_bi" and FREIGHT_BI_CLEAR_INTENT.search(text)


def is_clear_forecast_planning_intent(
    message: str,
    *,
    cta_id: str | None = None,
    top_domain: str | None = None,
) -> bool:
    if cta_id == "forecast_planning":
        return True
    text = (message or "").strip()
    if not text or is_future_capability_intent(text):
        return False
    if FORECAST_CLEAR_INTENT.search(text):
        return True
    return top_domain == "forecast_planning" and FORECAST_CLEAR_INTENT.search(text)


def is_clear_operational_audit_intent(
    message: str,
    *,
    cta_id: str | None = None,
    top_domain: str | None = None,
) -> bool:
    if cta_id == "operational_audit":
        return True
    text = (message or "").strip()
    if not text:
        return False
    if OPERATIONAL_AUDIT_CLEAR_INTENT.search(text):
        return True
    return top_domain == "operational_audit" and OPERATIONAL_AUDIT_CLEAR_INTENT.search(text)


def is_clear_strategic_logistics_intent(
    message: str,
    *,
    cta_id: str | None = None,
    top_domain: str | None = None,
) -> bool:
    if cta_id == "strategic_support":
        return True
    text = (message or "").strip()
    if not text or is_future_capability_intent(text):
        return False
    if STRATEGIC_CLEAR_INTENT.search(text):
        return True
    return top_domain == "strategic_logistics" and STRATEGIC_CLEAR_INTENT.search(text)


def is_ambiguous_logistics_intent(message: str) -> bool:
    text = (message or "").strip()
    if not text:
        return False
    if is_future_capability_intent(text):
        return False
    if not AMBIGUOUS_LOGISTICS_INTENT.search(text):
        return False
    if FREIGHT_BI_CLEAR_INTENT.search(text):
        return False
    if FORECAST_CLEAR_INTENT.search(text):
        return False
    if OPERATIONAL_AUDIT_CLEAR_INTENT.search(text):
        return False
    if STRATEGIC_CLEAR_INTENT.search(text):
        return False
    if EDITORIAL_CLEAR_INTENT.search(text):
        return False
    return True


def is_clear_intent_for_domain(
    message: str,
    domain: str | None,
    *,
    cta_id: str | None = None,
) -> bool:
    if not domain:
        return False
    checks = {
        "editorial_market": is_clear_editorial_market_intent,
        "freight_bi": is_clear_freight_bi_intent,
        "forecast_planning": is_clear_forecast_planning_intent,
        "operational_audit": is_clear_operational_audit_intent,
        "strategic_logistics": is_clear_strategic_logistics_intent,
    }
    checker = checks.get(domain)
    if not checker:
        return False
    return checker(message, cta_id=cta_id, top_domain=domain)


def is_clear_editorial_market_intent(
    message: str,
    *,
    cta_id: str | None = None,
    top_domain: str | None = None,
) -> bool:
    """Intenção inequívoca de notícias/tendências — handoff imediato para Feed."""
    if cta_id == "market_news":
        return True
    text = (message or "").strip().lower()
    if not text:
        return False
    if text in ("notícias", "noticias", "tendências", "tendencias", "mercado", "feed"):
        return True
    if re.match(r"^quero\s+(not[ií]cias|tend[eê]ncias|acompanhar)", text):
        return True
    if top_domain == "editorial_market" and EDITORIAL_CLEAR_INTENT.search(text):
        return True
    if EDITORIAL_CLEAR_INTENT.search(text):
        return True
    return False


def get_cta_by_id(cta_id: str | None) -> dict[str, Any] | None:
    if not cta_id:
        return None
    return CTA_BY_ID.get(str(cta_id).strip())


def rank_destinations_from_capabilities(
    capability_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Ranqueia destinos a partir dos domínios candidatos (sem roteamento direto a agente)."""
    dest_scores: dict[str, float] = {}
    for cap in capability_candidates:
        domain = (cap.get("domain") or "").strip()
        score = float(cap.get("score") or 0)
        if domain not in CAPABILITY_DOMAINS_MVP:
            continue
        for dest_id in DOMAIN_DESTINATION_CANDIDATES.get(domain, []):
            dest_scores[dest_id] = max(dest_scores.get(dest_id, 0), score)

    ranked = sorted(dest_scores.items(), key=lambda x: x[1], reverse=True)
    out: list[dict[str, Any]] = []
    for dest_id, score in ranked:
        spec = DESTINATIONS.get(dest_id)
        if not spec:
            continue
        out.append({
            "destination": dest_id,
            "score": round(score, 1),
            "label": spec.label,
            "url": spec.url,
            "requires_login": spec.requires_login,
            "requires_dataset": spec.requires_dataset,
            "agent": spec.agent,
            "handoff_action": spec.handoff_action,
        })
    return out


def compute_confidence_level(candidates: list[dict[str, Any]]) -> str:
    if not candidates:
        return "low"
    scores = sorted([float(c.get("score") or 0) for c in candidates], reverse=True)
    top = scores[0]
    second = scores[1] if len(scores) > 1 else 0
    gap = top - second
    if top >= CONFIDENCE_HANDOFF_THRESHOLD and gap >= TOP_GAP_FOR_HANDOFF:
        return "high"
    if top >= CONFIDENCE_REFINE_THRESHOLD:
        return "medium"
    return "low"


def decide_next_action(
    confidence: str,
    *,
    user_confirmed: bool = False,
    history_turns: int = 0,
    top_capability_domain: str | None = None,
    user_message: str | None = None,
    cta_id: str | None = None,
) -> str:
    """
    refine | handoff
    Handoff imediato quando intenção clara e capability disponível.
    Capabilities futuras: refine (sem handoff para módulo inexistente).
    """
    message = user_message or ""

    if is_future_capability_intent(message, cta_id=cta_id):
        return "refine"

    if is_clear_editorial_market_intent(
        message,
        cta_id=cta_id,
        top_domain=top_capability_domain,
    ):
        return "handoff"

    if (
        confidence == "high"
        and top_capability_domain
        and get_capability_availability(top_capability_domain).get("available", True)
        and is_clear_intent_for_domain(message, top_capability_domain, cta_id=cta_id)
    ):
        return "handoff"

    if is_ambiguous_logistics_intent(message) and not user_confirmed:
        return "refine"

    if confidence == "high" and (user_confirmed or history_turns >= 2):
        return "handoff"

    return "refine"
