"""Carrega o documento de capacidades do Copilot e expõe mapeamentos para guardrails."""
from __future__ import annotations

import pathlib
import re
from typing import Any

_CAPABILITIES_PATH = pathlib.Path(__file__).resolve().parent / "copilot_capabilities.md"

VALID_RECOMMENDED_AGENTS = frozenset({
    "roberto",
    "cleide",
    "julia",
    "feed",
    "agente_compara",
})

# Cleide padrão = Auditoria de Fretes; BI Cleide anterior permanece como destino legado `cleide_audit`.
AGENT_TO_DESTINATION: dict[str, str] = {
    "roberto": "roberto_bi",
    "cleide": "cleide_freight_audit",
    "julia": "julia_operational",
    "feed": "feed",
    "agente_compara": "agente_compara",
}

AGENT_BLURBS: dict[str, str] = {
    "roberto": (
        "Roberto olha para frente — usa histórico para prever custos, tendências "
        "e cenários dos próximos meses, com indicadores e BI gerencial de fretes."
    ),
    "cleide": (
        "Cleide audita cobrança — compara valor cobrado com o esperado pela tabela "
        "negociada, explica memória de cálculo e aponta divergências."
    ),
    "julia": (
        "Júlia é a consultoria operacional logada — estratégia, supply chain, "
        "planejamento e apoio com documentos no chat quando logado."
    ),
    "agente_compara": (
        "O AgenteCompara compara duas ou três tabelas de transportadoras sobre o "
        "mesmo volume de embarques — custos, cobertura, diferenças, economia "
        "potencial e resultados por UF. A decisão final permanece com você."
    ),
    "feed": "O Feed reúne notícias e tendências do mercado logístico.",
}

ACTIVITY_AMBIGUOUS_REPLY = (
    "Consigo te ajudar, mas isso pode seguir caminhos diferentes. "
    "Se você quer prever os próximos meses, indicadores ou BI gerencial de fretes, "
    "Roberto é o melhor caminho. "
    "Se quer auditar cobrança, comparar cobrado com esperado ou validar tabela negociada, "
    "a Auditoria de Fretes da Cleide é mais indicada. "
    "Se quer comparar propostas de transportadoras sobre o mesmo volume, "
    "o AgenteCompara é o caminho. "
    "Se a ideia é decidir uma estratégia de redução ou negociação, Júlia pode ajudar. "
    "Qual é o seu objetivo principal?"
)

BI_VS_AUDIT_TRIAGE_REPLY = (
    "Depende do objetivo: "
    "para indicadores, gráficos, análise gerencial e previsões de frete, use o BI do Roberto (/fretes); "
    "para conferir cobrança, comparar cobrado com esperado, validar tabela negociada ou ver "
    "divergências e memória de cálculo, use a Auditoria de Fretes da Cleide (/auditoria-frete); "
    "para comparar duas ou três tabelas/propostas sobre o mesmo volume, use o AgenteCompara (/agente-compara)."
)

SPREADSHEET_AMBIGUOUS_REPLY = ACTIVITY_AMBIGUOUS_REPLY
DASHBOARD_AMBIGUOUS_REPLY = ACTIVITY_AMBIGUOUS_REPLY
COST_AMBIGUOUS_REPLY = ACTIVITY_AMBIGUOUS_REPLY

# Cotação/licitação automatizada no mercado — ainda indisponível.
# BID comparativo interno NÃO entra aqui (vai para AgenteCompara).
UNAVAILABLE_FEATURE_KEYWORDS: tuple[tuple[str, ...], ...] = (
    ("cotação automatizada", "cotacao automatizada", "cotar frete automaticamente"),
    (
        "enviar o bid para transportadoras",
        "enviar bid para transportadoras",
        "coletar propostas no mercado",
        "licitação aberta de frete",
        "licitacao aberta de frete",
    ),
)

_ROBERTO_PREDICTIVE_RE = re.compile(
    r"\b(?:previs(?:[ãa]o|[ãa]es|ir)?|prever|projet(?:ar|ar)?|forecast|estim(?:ar|ativa)?|"
    r"pr[oó]xim(?:os?)?(?:\s+meses)?|futur[oa]?|tend[eê]ncia|previsibil|"
    r"cen[aá]rio(?:s)?(?:\s+futur)?|evolu[cç][aã]o\s+esperada|"
    r"quanto\s+(?:vou|posso)\s+gastar|para\s+onde\s+(?:meu\s+)?(?:custo|frete))\b",
    re.IGNORECASE,
)

# BI gerencial / indicadores → Roberto (/fretes), não Auditoria nem BI Cleide legado.
_ROBERTO_BI_MANAGERIAL_RE = re.compile(
    r"(?:indicadores?\s+(?:de\s+)?frete|gr[aá]ficos?(?:\s*/\s*|\s+e\s+|\s+)?bi|"
    r"bi\s+gerencial|an[aá]lise\s+de\s+dados\s+de\s+frete|"
    r"dashboard\s+de\s+fretes?|painel\s+(?:gerencial|de\s+fretes?)|"
    r"visualiza[cç][oõ]es?\s+de\s+frete)",
    re.IGNORECASE,
)

_CLEIDE_RETROSPECTIVE_RE = re.compile(
    r"\b(?:audit(?:ar|oria)?|paguei|pagando|pag(?:ar|uei)\s+cert|pag(?:ar|uei)\s+err|"
    r"desvio(?:s)?|anomalias?|erros?|diverg(?:[eê]ncia|ir)?|cobran[cç]a(?:s)?|suspeit|indevid|"
    r"investig(?:ar)?|concentra(?:[cç][aã]o|r)?|realizad(?:o|a|os|as)?|passad(?:o|a|os|as)?|"
    r"[uú]ltimos\s+meses|ocorrid(?:o|a|os|as)?|onde\s+errei|desviou|confer(?:ir|encia)?)\b",
    re.IGNORECASE,
)

# Intenções explícitas da nova Auditoria de Fretes (/auditoria-frete).
_CLEIDE_FREIGHT_AUDIT_RE = re.compile(
    r"(?:auditar?\s+cobran[cç]|auditar?\s+(?:os\s+)?fretes?|auditoria\s+de\s+(?:frete|cobran[cç])|"
    r"valor\s+cobrado\s+est[aá]\s+correto|cobrado\s+(?:com|versus|vs\.?|x)\s+esperado|"
    r"comparar\s+cobrado|validar\s+tabela\s+negociada|tabela\s+negociada|"
    r"diverg[eê]ncia(?:s)?\s+(?:de\s+)?frete|mem[oó]ria\s+de\s+c[aá]lculo|"
    r"documentos?\s+sem\s+c[aá]lculo|cidades?\s+sem\s+(?:frete\s+)?calculad|"
    r"sem\s+frete\s+calculad|cobran[cç]as?\s+de\s+frete|"
    r"conferir\s+se\s+(?:fui\s+)?cobrad|"
    r"ct-?e\s+com\s+(?:a\s+)?tabela|"
    r"paguei\s+o\s+valor\s+correto)",
    re.IGNORECASE,
)

# Comparação multitabela / BID comparativo interno → AgenteCompara.
# Exige intenção composta; não dispara por palavra isolada (bid, tabela, proposta).
_FREIGHT_TABLE_COMPARISON_RE = re.compile(
    r"(?:"
    r"comparar\s+(?:(?:duas|tr[e?]s|2|3)\s+)?tabelas(?:\s+de\s+frete)?|"
    r"compar(?:ar|e)\s+(?:as\s+)?(?:(?:duas|tr[e?]s|2|3)\s+)?tabelas(?:\s+de\s+frete)?|"
    r"comparar\s+propostas?\s+(?:de\s+|comerciais?\s+de\s+)?transportadoras|"
    r"comparar\s+(?:as\s+)?propostas?(?!\s+para\s+decidir)(?=.{0,60}(?:transportadoras|tabelas|custos?|cobertura|frete|comerciais))|"
    r"comparar\s+(?:custo|cobertura|custos|valores).{0,40}(?:cobertura|custo|transportadoras|tabelas)|"
    r"comparar\s+cobertura\s+e\s+custo|"
    r"fazer\s+(?:um\s+)?bid|"
    r"\bbid\b\s+entre\s+(?:tr[eê]s\s+)?transportadoras|"
    r"bid\s+comparativ|"
    r"concorr[eê]ncia\s+comparativa|"
    r"equalizar\s+(?:os\s+)?(?:valores|propostas|custos)|"
    r"(?:(?:duas|tr[e?]s|2|3)\s+)?tabelas?.{0,30}lado\s+a\s+lado|"
    r"propostas?.{0,40}lado\s+a\s+lado|"
    r"lado\s+a\s+lado.{0,40}(?:tabelas?|propostas?)|"
    r"aplicar\s+(?:os\s+|o\s+)?mesm|"
    r"mesm[oa]\s+(?:base|volume|arquivo|embarques?).{0,50}(?:tabelas?|propostas?|diferentes)|"
    r"simular\s+(?:meu\s+)?volume|"
    r"economia\s+potencial|"
    r"vencedoras?\s+por\s+(?:uf|estado|embarque)|"
    r"mais\s+competitiva\s+por\s+uf|"
    r"menor\s+custo\s+por\s+uf|"
    r"qual\s+tabela\s+(?:[eé]\s+)?mais\s+competitiva|"
    r"propostas?\s+comerciais?.{0,40}(?:compar|lado\s+a\s+lado|equaliz)|"
    r"agente\s*compara|agentecompara"
    r")",
    re.IGNORECASE | re.DOTALL,
)

# Cotação/licitação automatizada ou contratação — honestidade, sem handoff operacional.
_AUTOMATED_MARKET_BID_RE = re.compile(
    r"(?:"
    r"cota[cç][aã]o\s+automat|"
    r"cotar\s+frete\s+automatic|"
    r"envi(?:ar|e)\s+(?:o\s+)?bid\s+(?:para|ao|às|as)\s+transportadoras|"
    r"sistema\s+envie\s+(?:o\s+)?bid|"
    r"colet(?:ar|em)\s+propostas?\s+(?:no\s+)?mercado|"
    r"dispara(?:r)?\s+(?:uma\s+)?concorr[eê]ncia|"
    r"publicar\s+(?:uma\s+)?concorr[eê]ncia|"
    r"contratar?\s+(?:a\s+)?(?:melhor\s+)?(?:empresa|transportadora)|"
    r"escolh(?:a|er)\s+e\s+contrat|"
    r"fech(?:e|ar)\s+automaticamente|"
    r"decid(?:a|ir)\s+automaticamente|"
    r"licita[cç][aã]o\s+(?:aberta|automat)|"
    r"cota[cç][aã]o\s+(?:aberta|de\s+frete)"
    r")",
    re.IGNORECASE,
)

_JULIA_STRATEGIC_RE = re.compile(
    r"(?:\bdecid(?:ir|o|a)|\bestrat[eé]g(?:ia|ic)|\bnegoci(?:ar|a[cç][aã]o)|"
    r"\bplano\s+de\s+a[cç][aã]o|\bsupply\s+chain|\bestoque|\binfla[cç][aã]o|"
    r"\bc[aâ]mbio|\bcambial|\bimporta(?:[cç][aã]o|r)?|\bexporta(?:[cç][aã]o|r)?|\bmacroecon|"
    r"\binterpretar(?:\s+\w+){0,8}\s+(?:para\s+)?tomar\s+decis[aã]o|"
    r"\btomada\s+de\s+decis[aã]o|\bleitura\s+execut|"
    r"\bcomparar\s+propostas\s+para\s+decidir|"
    r"\bcomparar\s+alternativas|"
    r"\bresumir(?:\s+(?:o\s+)?(?:documento|contrato|arquivo))?|"
    r"\bmontar\s+plano|planejamento\s+log[ií]stico|"
    r"\bsourcing|"
    r"\bentender\s+(?:o\s+)?cen[aá]rio)",
    re.IGNORECASE,
)

_ARTIFACT_OR_THEME_RE = re.compile(
    r"\b(?:planilha(?:s)?|dashboard(?:s)?|analytics|\bbi\b|dados|transportadoras?|"
    r"custos?(?:\s+(?:de\s+)?frete|\s+log[ií]stico)?|gr[aá]ficos?|indicadores?|frete|"
    r"documento(?:s)?|\bpdf(?:s)?\b|\bxml\b|anexo(?:s)?|tabela(?:s)?|"
    r"proposta(?:s)?|\bbid\b)\b",
    re.IGNORECASE,
)

_FORMAT_QUESTION_RE = re.compile(
    r"\b(?:aceita\s+planilha|aceitam\s+planilha|aceita\s+pdf|aceitam\s+pdf|"
    r"aceita\s+documento|subir|upload|enviar\s+(?:meus\s+)?dados|"
    r"enviar\s+arquivos?|posso\s+subir|anexar\s+(?:arquivo|documento|pdf))\b",
    re.IGNORECASE,
)

_GENERIC_ANALYZE_RE = re.compile(
    r"\b(?:analisar|analise|analisa|ver\s+(?:meu|minhas?|os))\b",
    re.IGNORECASE,
)

_SCREEN_TRIAGE_RE = re.compile(
    r"(?:qual\s+tela\s+(?:devo|eu\s+devo|usar|escolher)|"
    r"qual\s+(?:caminho|destino|agente)\s+(?:devo|usar|escolher)|"
    r"bi\s+ou\s+auditoria|auditoria\s+ou\s+bi|"
    r"roberto\s+ou\s+cleide|cleide\s+ou\s+roberto)",
    re.IGNORECASE,
)


def load_capabilities_document() -> str:
    return _CAPABILITIES_PATH.read_text(encoding="utf-8")


def agent_to_destination(agent: str | None) -> str | None:
    if not agent:
        return None
    normalized = str(agent).strip().lower()
    return AGENT_TO_DESTINATION.get(normalized)


def is_freight_audit_intent(message: str) -> bool:
    text = (message or "").strip().lower()
    if not text:
        return False
    return bool(_CLEIDE_FREIGHT_AUDIT_RE.search(text))


def is_freight_table_comparison_intent(message: str) -> bool:
    text = (message or "").strip().lower()
    if not text:
        return False
    return bool(_FREIGHT_TABLE_COMPARISON_RE.search(text))


def is_automated_market_bid_intent(message: str) -> bool:
    text = (message or "").strip().lower()
    if not text:
        return False
    return bool(_AUTOMATED_MARKET_BID_RE.search(text))


def is_roberto_bi_managerial_intent(message: str) -> bool:
    text = (message or "").strip().lower()
    if not text:
        return False
    return bool(_ROBERTO_BI_MANAGERIAL_RE.search(text))


def is_screen_triage_question(message: str) -> bool:
    text = (message or "").strip().lower()
    if not text:
        return False
    return bool(_SCREEN_TRIAGE_RE.search(text))


def resolve_activity_intent(message: str) -> str | None:
    """
    Classifica intenção pela atividade fim e horizonte temporal.
    Artefatos (planilha, dashboard, BI, custo) só geram handoff com atividade clara.
    """
    text = (message or "").strip().lower()
    if not text:
        return None

    if is_screen_triage_question(text):
        return "screen_triage"

    # Cotação/licitação automatizada ou contratação — honestidade antes de handoffs.
    if is_automated_market_bid_intent(text):
        return "unavailable_feature"

    # Auditoria de cobrança explícita tem prioridade sobre comparação multitabela genérica.
    if is_freight_audit_intent(text):
        return "cleide_freight_audit"

    # Comparação multitabela / BID comparativo interno → AgenteCompara.
    if is_freight_table_comparison_intent(text):
        return "freight_table_comparison"

    if is_roberto_bi_managerial_intent(text):
        return "roberto_predictive"

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
    return intent in (
        "ambiguous",
        "artifact_format_question",
        "screen_triage",
        "unavailable_feature",
    )


def resolve_spreadsheet_context(message: str) -> str | None:
    text = (message or "").strip().lower()
    if not re.search(
        r"\b(?:planilha|planilhas|arquivo|documento(?:s)?|\bpdf(?:s)?\b|"
        r"base(?:\s+de)?\s+dados|subir\s+(?:meus\s+)?dados|upload|"
        r"aceita\s+planilha|aceitam\s+planilha|posso\s+subir|"
        r"hist[oó]rico\s+de\s+fretes)\b",
        text,
    ):
        return None
    intent = resolve_activity_intent(message)
    if intent == "artifact_format_question":
        return "spreadsheet_acceptance" if "aceita" in text else "spreadsheet_upload"
    if intent in ("cleide_retrospective", "cleide_freight_audit"):
        return "spreadsheet_audit"
    if intent == "roberto_predictive":
        return "spreadsheet_bi"
    if intent == "freight_table_comparison":
        return "spreadsheet_comparison"
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
    if intent in ("cleide_retrospective", "cleide_freight_audit"):
        return "dashboard_audit"
    if intent == "roberto_predictive":
        return "dashboard_bi"
    if intent == "freight_table_comparison":
        return "dashboard_comparison"
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
    if intent in ("cleide_retrospective", "cleide_freight_audit"):
        return "cost_audit"
    if intent == "roberto_predictive":
        return "cost_bi"
    if intent == "freight_table_comparison":
        return "cost_comparison"
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


def preferred_destination_for_message(message: str) -> str | None:
    """Destino determinístico preferido para fallback e remapeamento de guardrail."""
    intent = resolve_activity_intent(message)
    if intent == "cleide_freight_audit":
        return "cleide_freight_audit"
    if intent == "cleide_retrospective":
        return "cleide_freight_audit"
    if intent == "freight_table_comparison":
        return "agente_compara"
    if intent == "roberto_predictive":
        return "roberto_bi"
    if intent == "julia_strategic":
        return "julia_operational"
    return None


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
    return "general"


def build_local_conversational_reply(user_message: str) -> dict[str, Any]:
    """
    Resposta mínima sem Gemini, derivada dos metadados de capacidades do produto.
    Usado apenas quando Gemini não está disponível ou falhou de forma irrecuperável.
    """
    ctx = _classify_local_context(user_message)

    if ctx == "screen_triage":
        return {
            "reply": BI_VS_AUDIT_TRIAGE_REPLY,
            "recommended_agent": None,
            "handoff": None,
            "handoffs": None,
            "confidence": "high",
            "reason": "local_screen_triage",
        }

    if ctx in ("ambiguous", "artifact_format_question", "multi_mixed"):
        return {
            "reply": ACTIVITY_AMBIGUOUS_REPLY,
            "recommended_agent": None,
            "handoff": None,
            "handoffs": None,
            "confidence": "medium",
            "reason": f"local_{ctx}",
        }

    if ctx == "unavailable_feature":
        return {
            "reply": (
                "O AgenteCompara pode comparar as tabelas e propostas que você já possui, "
                "mas não envia a concorrência ao mercado nem contrata transportadoras. "
                "A decisão final permanece com você. "
                "Se quiser comparar tabelas fornecidas, posso te levar ao AgenteCompara."
            ),
            "recommended_agent": None,
            "handoff": None,
            "handoffs": None,
            "confidence": "high",
            "reason": "local_unavailable_feature",
        }

    if ctx == "freight_table_comparison":
        return {
            "reply": (
                "Para comparar duas ou três tabelas de transportadoras sobre o mesmo "
                "volume de embarques, use o AgenteCompara. Ele calcula os custos de cada "
                "tabela e mostra cobertura, diferenças, economia potencial e resultados por UF."
            ),
            "recommended_agent": "agente_compara",
            "handoff": {
                "destination": "agente_compara",
                "label": "Iniciar comparação de tabelas",
            },
            "handoffs": None,
            "confidence": "high",
            "reason": "local_freight_table_comparison",
        }

    if ctx == "roberto_predictive":
        return {
            "reply": (
                "Para indicadores, gráficos, BI gerencial ou prever/projetar custos futuros, "
                f"Roberto é o caminho. {AGENT_BLURBS['roberto']}"
            ),
            "recommended_agent": "roberto",
            "handoff": {"destination": "roberto_bi"},
            "handoffs": None,
            "confidence": "high",
            "reason": "local_roberto_predictive",
        }

    if ctx in ("cleide_freight_audit", "cleide_retrospective"):
        return {
            "reply": (
                "Para auditar cobrança, comparar cobrado com esperado, validar tabela negociada "
                f"ou investigar divergências — a Auditoria de Fretes da Cleide é o caminho. "
                f"{AGENT_BLURBS['cleide']}"
            ),
            "recommended_agent": "cleide",
            "handoff": {"destination": "cleide_freight_audit"},
            "handoffs": None,
            "confidence": "high",
            "reason": "local_cleide_freight_audit",
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
                "indicar o melhor caminho — BI e previsão com Roberto, Auditoria de Fretes "
                "com Cleide, comparação de tabelas com AgenteCompara, estratégia com Júlia "
                "ou notícias no Feed. O que você quer explorar?"
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

    return {
        "reply": (
            "Sou o Copilot do AgenteFrete. Posso ajudar com BI e previsão de fretes, "
            "Auditoria de Fretes (cobrança e divergências), comparação de tabelas "
            "(AgenteCompara), estratégia logística e tendências de mercado. "
            "O que você precisa agora?"
        ),
        "recommended_agent": None,
        "handoff": None,
        "handoffs": None,
        "confidence": "low",
        "reason": "local_general",
    }
