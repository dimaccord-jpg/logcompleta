"""
Dados estruturais de capabilities e destinos para onboarding (CTAs, handoff targets).

O roteamento conversacional vive em run_cleiton_discovery.py + copilot_capabilities.md.
Este módulo não faz classificação de intenção por regex.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CAPABILITY_DOMAINS_MVP = frozenset({
    "editorial_market",
    "strategic_logistics",
    "freight_bi",
    "operational_audit",
    "forecast_planning",
})

CAPABILITY_DOMAINS_FUTURE = frozenset({
    "future_bid",
    "future_quotation",
    "future_supply_planning",
})

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
    "freight_bi": "BI de Fretes e indicadores (Roberto)",
    "operational_audit": "Auditoria de Fretes (Cleide)",
    "forecast_planning": "Previsibilidade e planejamento de frete",
    "future_quotation": "Cotação automatizada de frete",
    "future_bid": "BID de frete",
    "future_supply_planning": "Planejamento de supply chain",
}

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
    requires_login: bool
    requires_dataset: bool
    agent: str | None
    handoff_action: str | None = None


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
        label="Continuar com Júlia gratuitamente",
        url="/chat_julia?mode=operational",
        requires_login=True,
        requires_dataset=False,
        agent="julia",
        handoff_action="start_julia",
    ),
    "roberto_bi": DestinationSpec(
        id="roberto_bi",
        label="BI de Fretes (Roberto)",
        url="/fretes",
        requires_login=True,
        requires_dataset=True,
        agent="roberto",
    ),
    "cleide_freight_audit": DestinationSpec(
        id="cleide_freight_audit",
        label="Auditoria de Fretes (Cleide)",
        url="/auditoria-frete",
        requires_login=True,
        requires_dataset=True,
        agent="cleide",
    ),
    # Legado: BI Cleide anterior — não é o destino padrão de auditoria de cobrança.
    "cleide_audit": DestinationSpec(
        id="cleide_audit",
        label="BI Cleide (legado)",
        url="/cleide-bi-frete",
        requires_login=True,
        requires_dataset=True,
        agent="cleide",
    ),
}

DOMAIN_DESTINATION_CANDIDATES: dict[str, list[str]] = {
    "editorial_market": ["feed"],
    "strategic_logistics": ["julia_operational"],
    "freight_bi": ["roberto_bi"],
    "operational_audit": ["cleide_freight_audit"],
    "forecast_planning": ["roberto_bi"],
}

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
        "label": "📋 Auditoria de Fretes — cobrança e divergências",
        "seed_domains": ["operational_audit", "freight_bi"],
        "seed_message": "Quero auditar cobranças de frete e comparar valor cobrado com o esperado.",
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


def get_capability_availability(domain: str) -> dict[str, Any]:
    spec = CAPABILITY_AVAILABILITY.get(domain)
    if spec:
        return dict(spec)
    return {"available": True, "status": "available", "alternatives": []}


def get_cta_by_id(cta_id: str | None) -> dict[str, Any] | None:
    if not cta_id:
        return None
    return CTA_BY_ID.get(str(cta_id).strip())


def rank_destinations_from_capabilities(
    capability_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Ranqueia destinos a partir de candidatos de domínio (uso legado/audit)."""
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
