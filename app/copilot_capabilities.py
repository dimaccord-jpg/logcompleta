"""Carrega o documento de capacidades do Copilot e expõe mapeamentos para guardrails."""
from __future__ import annotations

import pathlib
import re
from typing import Any

_CAPABILITIES_PATH = pathlib.Path(__file__).resolve().parent / "copilot_capabilities.md"

VALID_RECOMMENDED_AGENTS = frozenset({"roberto", "cleide", "julia", "feed"})

AGENT_TO_DESTINATION: dict[str, str] = {
    "roberto": "roberto_bi",
    "cleide": "cleide_audit",
    "julia": "julia_operational",
    "feed": "feed",
}

AGENT_BLURBS: dict[str, str] = {
    "roberto": (
        "Roberto olha para frente — usa histórico para prever custos, tendências "
        "e cenários dos próximos meses."
    ),
    "cleide": (
        "Cleide olha para trás — investiga custos realizados, desvios, anomalias "
        "e se você pagou certo."
    ),
    "julia": "Júlia apoia estratégia logística, supply chain, negociação e decisão gerencial.",
    "feed": "O Feed reúne notícias e tendências do mercado logístico.",
}

ACTIVITY_AMBIGUOUS_REPLY = (
    "Consigo te ajudar, mas isso pode seguir caminhos diferentes. "
    "Se você quer prever os próximos meses e projetar tendência de custo, Roberto é o melhor caminho. "
    "Se quer entender desvios, cobranças ou o que aconteceu nos últimos meses, Cleide é mais indicada. "
    "Se a ideia é decidir uma estratégia de redução ou negociação, Júlia pode ajudar. "
    "Qual é o seu objetivo principal?"
)

SPREADSHEET_AMBIGUOUS_REPLY = ACTIVITY_AMBIGUOUS_REPLY
DASHBOARD_AMBIGUOUS_REPLY = ACTIVITY_AMBIGUOUS_REPLY
COST_AMBIGUOUS_REPLY = ACTIVITY_AMBIGUOUS_REPLY

UNAVAILABLE_FEATURE_KEYWORDS: tuple[tuple[str, ...], ...] = (
    ("cotação automatizada", "cotacao automatizada", "cotar frete automaticamente"),
    ("bid de frete", "licitação de frete", "licitacao de frete"),
)

_ROBERTO_PREDICTIVE_RE = re.compile(
    r"\b(?:previs(?:[ãa]o|[ãa]es|ir)?|prever|projet(?:ar|ar)?|forecast|estim(?:ar|ativa)?|"
    r"pr[oó]xim(?:os?)?(?:\s+meses)?|futur[oa]?|tend[eê]ncia|previsibil|"
    r"cen[aá]rio(?:s)?(?:\s+futur)?|evolu[cç][aã]o\s+esperada|"
    r"quanto\s+(?:vou|posso)\s+gastar|para\s+onde\s+(?:meu\s+)?(?:custo|frete))\b",
    re.IGNORECASE,
)

_CLEIDE_RETROSPECTIVE_RE = re.compile(
    r"\b(?:audit(?:ar|oria)?|paguei|pagando|pag(?:ar|uei)\s+cert|pag(?:ar|uei)\s+err|"
    r"desvio(?:s)?|anomalias?|erros?|diverg(?:[eê]ncia|ir)?|cobran[cç]a(?:s)?|suspeit|indevid|"
    r"investig(?:ar)?|concentra(?:[cç][aã]o|r)?|realizad(?:o|a|os|as)?|passad(?:o|a|os|as)?|"
    r"[uú]ltimos\s+meses|ocorrid(?:o|a|os|as)?|onde\s+errei|desviou|confer(?:ir|encia)?)\b",
    re.IGNORECASE,
)

_JULIA_STRATEGIC_RE = re.compile(
    r"(?:\bdecid(?:ir|o|a)|\bestrat[eé]g(?:ia|ic)|\bnegoci(?:ar|a[cç][aã]o)|"
    r"\bplano\s+de\s+a[cç][aã]o|\bsupply\s+chain|\bestoque|\binfla[cç][aã]o|"
    r"\bc[aâ]mbio|\bcambial|\bimporta(?:[cç][aã]o|r)?|\bexporta(?:[cç][aã]o|r)?|\bmacroecon|"
    r"\binterpretar(?:\s+\w+){0,8}\s+(?:para\s+)?tomar\s+decis[aã]o|"
    r"\btomada\s+de\s+decis[aã]o|\bleitura\s+execut)",
    re.IGNORECASE,
)

_ARTIFACT_OR_THEME_RE = re.compile(
    r"\b(?:planilha(?:s)?|dashboard(?:s)?|analytics|\bbi\b|dados|transportadoras?|"
    r"custos?(?:\s+(?:de\s+)?frete|\s+log[ií]stico)?|gr[aá]ficos?|indicadores?|frete)\b",
    re.IGNORECASE,
)

_FORMAT_QUESTION_RE = re.compile(
    r"\b(?:aceita\s+planilha|aceitam\s+planilha|subir|upload|enviar\s+(?:meus\s+)?dados|"
    r"posso\s+subir)\b",
    re.IGNORECASE,
)

_GENERIC_ANALYZE_RE = re.compile(
    r"\b(?:analisar|analise|analisa|ver\s+(?:meu|minhas?|os))\b",
    re.IGNORECASE,
)


def load_capabilities_document() -> str:
    return _CAPABILITIES_PATH.read_text(encoding="utf-8")


def agent_to_destination(agent: str | None) -> str | None:
    if not agent:
        return None
    normalized = str(agent).strip().lower()
    return AGENT_TO_DESTINATION.get(normalized)


def resolve_activity_intent(message: str) -> str | None:
    """
    Classifica intenção pela atividade fim e horizonte temporal.
    Artefatos (planilha, dashboard, BI, custo) só geram handoff com atividade clara.
    """
    text = (message or "").strip().lower()
    if not text:
        return None

    roberto = bool(_ROBERTO_PREDICTIVE_RE.search(text))
    cleide = bool(_CLEIDE_RETROSPECTIVE_RE.search(text))
    julia = bool(_JULIA_STRATEGIC_RE.search(text))

    if sum((roberto, cleide, julia)) >= 2:
        return "multi_mixed"

    if cleide:
        return "cleide_retrospective"
    if roberto:
        return "roberto_predictive"
    if julia:
        return "julia_strategic"

    if _FORMAT_QUESTION_RE.search(text):
        return "artifact_format_question"

    if _ARTIFACT_OR_THEME_RE.search(text) or _GENERIC_ANALYZE_RE.search(text):
        return "ambiguous"

    if re.search(r"\b(?:auditar|audit|auditoria|investigar|anomalias?|desvio)\b", text):
        return "cleide_retrospective"

    return None


def should_suppress_handoff_for_unclear_activity(message: str) -> bool:
    """Guardrail: suprime handoff quando atividade fim não está clara."""
    intent = resolve_activity_intent(message)
    return intent in ("ambiguous", "artifact_format_question")


def resolve_spreadsheet_context(message: str) -> str | None:
    text = (message or "").strip().lower()
    if not re.search(
        r"\b(?:planilha|planilhas|arquivo|base(?:\s+de)?\s+dados|"
        r"subir\s+(?:meus\s+)?dados|upload|aceita\s+planilha|aceitam\s+planilha|"
        r"posso\s+subir|hist[oó]rico\s+de\s+fretes)\b",
        text,
    ):
        return None
    intent = resolve_activity_intent(message)
    if intent == "artifact_format_question":
        return "spreadsheet_acceptance" if "aceita" in text else "spreadsheet_upload"
    if intent == "cleide_retrospective":
        return "spreadsheet_audit"
    if intent == "roberto_predictive":
        return "spreadsheet_bi"
    if intent == "julia_strategic":
        return "spreadsheet_strategic"
    return "spreadsheet_ambiguous"


def resolve_dashboard_context(message: str) -> str | None:
    text = (message or "").strip().lower()
    if not re.search(r"\bdashboards?\b", text):
        return None
    intent = resolve_activity_intent(message)
    if intent == "julia_strategic":
        return "dashboard_strategic"
    if intent == "cleide_retrospective":
        return "dashboard_audit"
    if intent == "roberto_predictive":
        return "dashboard_bi"
    return "dashboard_ambiguous"


def resolve_cost_context(message: str) -> str | None:
    text = (message or "").strip().lower()
    if not re.search(
        r"\b(?:custos?\s+(?:de\s+)?frete|custo\s+log[ií]stico|pagando\s+certo|"
        r"cobran[cç]a(?:s)?)\b",
        text,
    ):
        return None
    intent = resolve_activity_intent(message)
    if intent == "cleide_retrospective":
        return "cost_audit"
    if intent == "roberto_predictive":
        return "cost_bi"
    if intent == "julia_strategic":
        return "cost_strategic"
    return "cost_ambiguous"


def should_suppress_handoff_for_spreadsheet_context(message: str) -> bool:
    ctx = resolve_spreadsheet_context(message)
    return ctx in ("spreadsheet_ambiguous", "spreadsheet_acceptance", "spreadsheet_upload")


def should_suppress_handoff_for_dashboard_context(message: str) -> bool:
    return resolve_dashboard_context(message) == "dashboard_ambiguous"


def should_suppress_handoff_for_cost_context(message: str) -> bool:
    return resolve_cost_context(message) == "cost_ambiguous"


def _classify_local_context(message: str) -> str:
    """Classificação mínima só para fallback offline (sem Gemini)."""
    text = (message or "").strip().lower()
    if not text:
        return "empty"

    intent = resolve_activity_intent(message)
    if intent:
        return intent

    if re.fullmatch(
        r"(?:ol[aá]|oi|bom\s+dia|boa\s+tarde|boa\s+noite|e\s*a[ií]|hey|hello|hi)[!.?\s]*",
        text,
        flags=re.IGNORECASE,
    ):
        return "greeting"
    if re.search(
        r"\b(?:como\s+(?:voc[eê]|pode|posso)\s+(?:ajud|atend)|"
        r"o\s+que\s+(?:voc[eê]|vc)\s+faz|como\s+funciona)\b",
        text,
    ):
        return "help"
    if re.search(r"\b(?:not[ií]cias?|tend[eê]ncias?|feed|mercado\s+log[ií]stico)\b", text):
        return "feed"
    if re.search(r"\b(?:cota[cç][aã]o|cotar\s+frete|\bbid\b)\b", text):
        return "unavailable_feature"
    return "general"


def build_local_conversational_reply(user_message: str) -> dict[str, Any]:
    """
    Resposta mínima sem Gemini, derivada dos metadados de capacidades do produto.
    Usado apenas quando Gemini não está disponível ou falhou de forma irrecuperável.
    """
    ctx = _classify_local_context(user_message)

    if ctx in ("ambiguous", "artifact_format_question", "multi_mixed"):
        return {
            "reply": ACTIVITY_AMBIGUOUS_REPLY,
            "recommended_agent": None,
            "handoff": None,
            "handoffs": None,
            "confidence": "medium",
            "reason": f"local_{ctx}",
        }

    if ctx == "roberto_predictive":
        return {
            "reply": (
                "Para prever, projetar ou estimar custos futuros com base no histórico, "
                f"Roberto é o caminho. {AGENT_BLURBS['roberto']}"
            ),
            "recommended_agent": "roberto",
            "handoff": {"destination": "roberto_bi"},
            "handoffs": None,
            "confidence": "high",
            "reason": "local_roberto_predictive",
        }

    if ctx == "cleide_retrospective":
        return {
            "reply": (
                "Para investigar o que já ocorreu — desvios, anomalias, cobranças ou "
                f"se pagou certo — Cleide é o caminho. {AGENT_BLURBS['cleide']}"
            ),
            "recommended_agent": "cleide",
            "handoff": {"destination": "cleide_audit"},
            "handoffs": None,
            "confidence": "high",
            "reason": "local_cleide_retrospective",
        }

    if ctx == "julia_strategic":
        return {
            "reply": (
                "Para decidir, negociar, planejar estrategicamente ou entender impactos "
                f"macroeconômicos, Júlia é a melhor apoio. {AGENT_BLURBS['julia']}"
            ),
            "recommended_agent": "julia",
            "handoff": {"destination": "julia_operational"},
            "handoffs": None,
            "confidence": "medium",
            "reason": "local_julia_strategic",
        }

    if ctx == "greeting":
        return {
            "reply": (
                "Olá! Sou o Copilot do AgenteFrete. "
                "Conte o que você quer entender ou resolver na sua operação logística."
            ),
            "recommended_agent": None,
            "handoff": None,
            "handoffs": None,
            "confidence": "medium",
            "reason": "local_greeting",
        }

    if ctx == "help":
        return {
            "reply": (
                "Posso conversar sobre sua operação logística e, quando fizer sentido, "
                "indicar o melhor caminho — previsão com Roberto, investigação com Cleide, "
                "estratégia com Júlia ou notícias no Feed. O que você quer explorar?"
            ),
            "recommended_agent": None,
            "handoff": None,
            "handoffs": None,
            "confidence": "medium",
            "reason": "local_help",
        }

    if ctx == "feed":
        return {
            "reply": f"Posso te levar ao feed editorial. {AGENT_BLURBS['feed']}",
            "recommended_agent": "feed",
            "handoff": {"destination": "feed"},
            "handoffs": None,
            "confidence": "high",
            "reason": "local_feed_intent",
        }

    if ctx == "unavailable_feature":
        return {
            "reply": (
                "Ainda não temos cotação ou BID automatizado de frete no AgenteFrete. "
                "Posso ajudar com previsão de custos, auditoria ou consultoria estratégica."
            ),
            "recommended_agent": None,
            "handoff": None,
            "handoffs": None,
            "confidence": "high",
            "reason": "local_unavailable_feature",
        }

    return {
        "reply": (
            "Sou o Copilot do AgenteFrete. Posso ajudar com previsão de fretes, "
            "auditoria de custos realizados, estratégia logística e tendências de mercado. "
            "O que você precisa agora?"
        ),
        "recommended_agent": None,
        "handoff": None,
        "handoffs": None,
        "confidence": "low",
        "reason": "local_general",
    }
