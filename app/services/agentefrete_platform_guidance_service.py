"""Orientação determinística para ferramentas internas do AgenteFrete operacional.

Camada aditiva: não participa da geração da resposta do LLM.
Zero chamadas extras de modelo. Fail-open no endpoint.
"""
from __future__ import annotations

import re
from typing import Any

from app.capability_taxonomy import DESTINATIONS
from app.copilot_capabilities import (
    is_automated_market_bid_intent,
    is_freight_audit_intent,
    is_freight_table_comparison_intent,
    is_roberto_bi_managerial_intent,
    is_screen_triage_question,
    preferred_destination_for_message,
    should_suppress_handoff_for_cost_context,
    should_suppress_handoff_for_dashboard_context,
    should_suppress_handoff_for_spreadsheet_context,
    should_suppress_handoff_for_unclear_activity,
)

ALLOWED_PLATFORM_DESTINATIONS = frozenset({
    "cleide_freight_audit",
    "roberto_bi",
    "agente_compara",
})

# Copy operacional de UI. Rotas canônicas vêm exclusivamente de DESTINATIONS.
OPERATIONAL_COPY: dict[str, dict[str, str]] = {
    "cleide_freight_audit": {
        "title": "Faça isso diretamente no AgenteAudita",
        "description": (
            "Você pode realizar essa auditoria na ferramenta dedicada do AgenteFrete. "
            "O AgenteAudita conduz o fluxo operacional da auditoria. Você pode manter "
            "este chat aberto para tirar dúvidas durante o processo."
        ),
        "action_label": "Abrir AgenteAudita",
    },
    "roberto_bi": {
        "title": "Analise seus dados no BI de Fretes",
        "description": (
            "Você pode usar o Roberto para trabalhar com indicadores, análises e "
            "previsões de frete enquanto mantém este chat aberto para apoio."
        ),
        "action_label": "Abrir BI de Fretes",
    },
    "agente_compara": {
        "title": "Compare suas tabelas no AgenteCompara",
        "description": (
            "Você pode usar o AgenteCompara para preparar e comparar tabelas de "
            "transportadoras em uma ferramenta dedicada."
        ),
        "action_label": "Abrir AgenteCompara",
    },
}

_SUGGESTION_META_RE = re.compile(
    r"^\[\[JULIA_SUGGESTION::.*?\]\]\s*",
    re.IGNORECASE | re.DOTALL,
)

_COMPARE_WORD_RE = re.compile(
    r"compar(?:ar|[aá]-l[oa]s)",
    re.IGNORECASE,
)


def resolve_agentefrete_platform_guidance(user_message: str) -> dict[str, Any] | None:
    """Resolve orientação de ferramenta interna a partir só da mensagem textual.

    Não lê documentos, histórico, request nem saída do LLM.
    """
    text = _plain_user_message(user_message)
    if not text:
        return None

    preferred = preferred_destination_for_message(text)
    destination = preferred if preferred in ALLOWED_PLATFORM_DESTINATIONS else None
    complementary = _complementary_allowed_destination(text)

    if destination is None:
        destination = complementary

    if destination not in ALLOWED_PLATFORM_DESTINATIONS:
        return None

    if _is_ambiguous_activity(text) and complementary != destination:
        return None

    return _build_handoff(destination)


def _plain_user_message(message: str | None) -> str:
    text = (message or "").strip()
    if not text:
        return ""
    return _SUGGESTION_META_RE.sub("", text, count=1).strip()


def _is_ambiguous_activity(message: str) -> bool:
    return (
        should_suppress_handoff_for_unclear_activity(message)
        or should_suppress_handoff_for_spreadsheet_context(message)
        or should_suppress_handoff_for_dashboard_context(message)
        or should_suppress_handoff_for_cost_context(message)
    )


def _complementary_allowed_destination(message: str) -> str | None:
    """Reforço local para casos claros que o resolver compartilhado ainda classifica como ambíguos."""
    if is_automated_market_bid_intent(message) or is_screen_triage_question(message):
        return None
    if is_freight_audit_intent(message) or _is_clear_freight_audit(message):
        return "cleide_freight_audit"
    if is_freight_table_comparison_intent(message) or _is_clear_table_comparison(message):
        return "agente_compara"
    if is_roberto_bi_managerial_intent(message) or _is_clear_roberto_bi(message):
        return "roberto_bi"
    return None


def _is_clear_freight_audit(message: str) -> bool:
    text = (message or "").strip().lower()
    has_audit = bool(re.search(r"\b(?:auditar|auditoria)\b", text))
    has_freight = bool(re.search(r"\bfretes?\b", text))
    has_check_charge = bool(re.search(r"\bconfer(?:ir|encia)\b", text)) and bool(
        re.search(r"(?:cobr(?:ando|ado|an[cç]a)|tabela)", text)
    )
    return (has_audit and has_freight) or has_check_charge


def _is_clear_table_comparison(message: str) -> bool:
    text = (message or "").strip().lower()
    has_tables = bool(re.search(r"\btabelas?\b", text))
    has_carriers = bool(re.search(r"\btransportadoras?\b", text))
    has_compare = bool(_COMPARE_WORD_RE.search(text))
    has_count = bool(re.search(r"\b(?:duas|tr[eê]s|2|3)\b", text))
    return has_compare and has_tables and (has_carriers or has_count)


def _is_clear_roberto_bi(message: str) -> bool:
    text = (message or "").strip().lower()
    has_indicator = bool(re.search(r"\bindicadores?\b", text))
    has_forecast = bool(
        re.search(r"\b(?:previs(?:[aã]o|[oõ]es)|prever|projetar|forecast)\b", text)
    )
    has_freight = bool(re.search(r"\bfretes?\b", text))
    return has_indicator and has_forecast and has_freight


def _build_handoff(destination: str) -> dict[str, Any] | None:
    spec = DESTINATIONS.get(destination)
    if spec is None:
        return None
    url = spec.url
    if not url:
        return None
    copy = OPERATIONAL_COPY.get(destination)
    if not copy:
        return None
    return {
        "destination": destination,
        "label": copy["action_label"],
        "url": url,
        "open_in_new_tab": True,
        "guidance_title": copy["title"],
        "guidance_text": copy["description"],
    }
