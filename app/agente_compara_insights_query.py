"""
Camada determinística de intenções e respostas do chat analítico pós-BI da Agente Compara.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

INTENT_EXPLAIN_CALCULATION = "explicar_calculo_documento"
INTENT_LOCATE_DOCUMENT = "localizar_documento"
INTENT_TOP_DIVERGENCES = "maiores_divergencias"
INTENT_OVERCHARGED = "cobrancas_a_mais"
INTENT_UNDERCHARGED = "cobrancas_a_menor"
INTENT_SMALLEST_DIVERGENCES = "menores_divergencias"
INTENT_EXPLAIN_CHART = "explicar_grafico"
INTENT_EXPLAIN_CARRIER_UF = "explicar_impacto_transportadora_uf"
INTENT_EXPLAIN_CAUSES = "explicar_causas_divergencia"
INTENT_BATCH_SUMMARY = "resumo_lote"
INTENT_CHARGE_VALIDITY = "validar_cobranca"
INTENT_EXECUTIVE_SUMMARY = "executive_summary"
INTENT_MANAGEMENT_EMAIL_DRAFT = "management_email_draft"
INTENT_ACTION_PLAN = "action_plan"
INTENT_ROOT_CAUSE_HYPOTHESES = "root_cause_hypotheses"
INTENT_PRIORITIZATION = "prioritization"
INTENT_CARRIER_NEGOTIATION_BRIEF = "carrier_negotiation_brief"
INTENT_AUDIT_FINDINGS_REPORT = "audit_findings_report"
INTENT_RISK_ALERTS = "risk_alerts"
INTENT_NEXT_STEPS = "next_steps"
INTENT_EXPLAIN_BUSINESS_IMPACT = "explain_business_impact"
INTENT_DOCUMENT_FOLLOWUP = "document_followup"
INTENT_UNCALCULATED_CITIES = "uncalculated_cities"
INTENT_UNCALCULATED_DOCUMENTS = "uncalculated_documents"
INTENT_EXPLAIN_UNCALCULATED_REASONS = "explain_uncalculated_reasons"
INTENT_SEND_EMAIL_BLOCKED = "send_email_blocked"
INTENT_OUT_OF_SCOPE = "fora_de_escopo"
INTENT_AMBIGUOUS = "ambiguidade"

RANKING_MAX_ITEMS = 50
DEFAULT_TOP_N = 5
ANALYTICAL_EVIDENCE_TOP_N = 5
UNCALCULATED_EVIDENCE_TOP_N = 8

UNCALCULATED_REASON_CODES = frozenset(
    {
        "missing_coverage_mapping",
        "ambiguous_coverage_mapping",
        "missing_freight_rule",
        "unsupported_pricing_model",
        "invalid_weight",
        "invalid_charged_freight",
        "invalid_invoice_value",
    }
)

REASON_CODE_LABELS = {
    "missing_coverage_mapping": "cobertura ausente",
    "ambiguous_coverage_mapping": "cobertura ambígua",
    "missing_freight_rule": "regra de frete ausente",
    "unsupported_pricing_model": "modelo de precificação não suportado",
    "invalid_weight": "peso inválido",
    "invalid_charged_freight": "valor cobrado inválido",
    "invalid_invoice_value": "valor de NF inválido",
}

CHART_KEYS = {
    "transportadora": "transportadora",
    "carrier": "transportadora",
    "uf_destino": "uf_destino",
    "uf": "uf_destino",
    "destino": "uf_destino",
    "temporal": "temporal",
    "periodo": "temporal",
    "evolucao": "temporal",
    "pareto": "pareto_transportadora",
    "pareto_transportadora": "pareto_transportadora",
}

# Ações externas / assinatura — "plano" genérico NÃO bloqueia (ex.: plano de ação).
OUT_OF_SCOPE_HINTS = (
    "contrato",
    "juridico",
    "advogado",
    "processo judicial",
    "reclame aqui",
    "procon",
    "cancelar assinatura",
    "alterar assinatura",
    "altere assinatura",
    "contratar plano",
    "contrate plano",
    "alterar plano",
    "altere plano",
    "cancelar plano",
    "cancele plano",
    "meu plano",
    "assinatura stripe",
    "stripe",
    "julia",
    "roberto",
    "faça upload",
    "fazer upload",
    "upload",
    "anexar documento",
    "enviar pdf",
    "ocr",
    "reprocessar agora",
    "reprocesse",
    "reprocessar",
    "executar auditoria",
    "calcular novamente",
)

# Ações externas que a plataforma executaria (exceto envio — tratado à parte).
_NON_SEND_EXECUTION_PATTERNS = (
    r"\bpublique\b",
    r"\bpublicar\b",
    r"\bcontrate\b",
    r"\bcontratar\b",
    r"\breprocesse\b",
    r"\breprocessar\b",
    r"\bfa[cç]a upload\b",
    r"\bfazer upload\b",
    r"\baltere (?:a |minha )?assinatura\b",
    r"\balterar (?:a |minha )?assinatura\b",
    r"\bcancele (?:o |meu )?plano\b",
    r"\bcancelar (?:o |meu )?plano\b",
    r"\bcontrate (?:o |meu )?plano\b",
    r"\bcontratar (?:o |meu )?plano\b",
)

# Imperativo: a Agente Compara/plataforma deve enviar.
_PLATFORM_SEND_IMPERATIVE_PATTERNS = (
    r"\benvie\b",
    r"\bmande\b",
    r"\bdispare\b",
    r"\bencaminhe\b",
)

# Infinitivo de envio como ação pedida à plataforma (não "para eu enviar").
_PLATFORM_SEND_INFINITIVE_PATTERNS = (
    r"\benviar\b",
    r"\bmandar\b",
    r"\bdisparar\b",
    r"\bencaminhar\b",
)

_USER_WILL_SEND_HINTS = (
    "para eu enviar",
    "para eu mandar",
    "pra eu enviar",
    "pra eu mandar",
    "para eu encaminhar",
    "pra eu encaminhar",
    "para eu disparar",
    "que eu vou enviar",
    "que eu vou mandar",
    "que eu envie",
    "eu mesmo enviar",
    "eu mesma enviar",
)

_CONTENT_PREPARE_HINTS = (
    "prepare",
    "preparar",
    "redija",
    "redigir",
    "escreva",
    "escrever",
    "monte",
    "montar",
    "elabore",
    "elaborar",
    "rascunhe",
    "rascunhar",
    "faça",
    "faca",
    "faz",
    "crie",
    "criar",
    "gere",
    "gerar",
    "produza",
    "produzir",
    "minuta",
    "draft",
)

_CONTENT_ARTIFACT_HINTS = (
    "e-mail",
    "email",
    "e mail",
    "minuta",
    "resumo",
    "relatório",
    "relatorio",
    "briefing",
    "apresentação",
    "apresentacao",
    "plano de ação",
    "plano de acao",
    "análise executiva",
    "analise executiva",
    "executivo",
    "comunicado",
)

# Correções explícitas de digitação do domínio (antes do fuzzy).
_DOMAIN_TYPO_MAP = {
    "e-ma": "e-mail",
    "ema": "e-mail",
    "emaill": "e-mail",
    "emial": "e-mail",
    "mail": "e-mail",
    "execituvo": "executivo",
    "execultivo": "executivo",
    "executvo": "executivo",
    "executiv": "executivo",
    "analise": "análise",
    "anlise": "análise",
    "divergencia": "divergência",
    "divergencias": "divergências",
    "calculo": "cálculo",
    "calculos": "cálculos",
    "estao": "estão",
    "voce": "você",
    "diretoria": "diretoria",
    "auditoria": "auditoria",
}

_DOMAIN_FUZZY_VOCAB = (
    "e-mail",
    "email",
    "executivo",
    "executiva",
    "análise",
    "analise",
    "resumo",
    "divergência",
    "divergencias",
    "cálculo",
    "calculo",
    "documento",
    "cidades",
    "cidade",
    "frete",
    "calculado",
    "auditoria",
    "minuta",
    "equipe",
    "diretoria",
    "transportadora",
    "prepare",
    "preparar",
)

_ANAPHORA_HINTS = (
    "daquele documento",
    "desse documento",
    "deste documento",
    "aquele documento",
    "esse documento",
    "este documento",
    "documento específico",
    "documento especifico",
    "estou falando",
    "falo daquele",
    "do mesmo documento",
    "dessa linha",
    "nessa linha",
    "nessa cobranca",
    "nessa cobrança",
)

_FOLLOWUP_TOPIC_HINTS = (
    "diverg",
    "imposto",
    "icms",
    "iss",
    "peso",
    "taxa",
    "taxas",
    "transportadora",
    "cidade",
    "destino",
    "por que",
    "porque",
    "motivo",
    "esperado",
    "cobrad",
    "component",
    "faixa",
    "pedágio",
    "pedagio",
    "gris",
)

GEMINI_ANALYTICAL_INTENTS = frozenset(
    {
        INTENT_EXECUTIVE_SUMMARY,
        INTENT_MANAGEMENT_EMAIL_DRAFT,
        INTENT_ACTION_PLAN,
        INTENT_ROOT_CAUSE_HYPOTHESES,
        INTENT_PRIORITIZATION,
        INTENT_CARRIER_NEGOTIATION_BRIEF,
        INTENT_AUDIT_FINDINGS_REPORT,
        INTENT_RISK_ALERTS,
        INTENT_NEXT_STEPS,
        INTENT_EXPLAIN_BUSINESS_IMPACT,
        INTENT_EXPLAIN_CAUSES,
    }
)

CHARGE_VALIDITY_HINTS = (
    "cobrança está errada",
    "cobranca esta errada",
    "cobrança errada",
    "cobranca errada",
    "foi cobrado errado",
    "cobrado errado",
    "cobrança indevida",
    "cobranca indevida",
    "essa cobrança",
    "essa cobranca",
    "está cobrando errado",
    "esta cobrando errado",
    "está errada a cobrança",
    "esta errada a cobranca",
)


def _normalize_text(value: str) -> str:
    return " ".join((value or "").lower().split())


def _strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFD", value or "")
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def _fuzzy_domain_token(token: str) -> str | None:
    """Corrige token do domínio com similaridade alta; None se inseguro."""
    if len(token) < 4:
        return None
    folded = _strip_accents(token)
    best_word = None
    best_ratio = 0.0
    for vocab in _DOMAIN_FUZZY_VOCAB:
        ratio = SequenceMatcher(None, folded, _strip_accents(vocab)).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_word = vocab
    if best_word and best_ratio >= 0.82:
        # Canonicaliza alguns alvos.
        if best_word in {"email", "e-mail"}:
            return "e-mail"
        if best_word in {"analise", "análise"}:
            return "análise"
        if best_word in {"calculo", "cálculo"}:
            return "cálculo"
        return best_word
    return None


def normalize_insights_message(message: str) -> tuple[str, dict[str, Any]]:
    """
    Normalização de domínio para classificação: typos comuns + fuzzy seguro.
    Retorna (texto_normalizado, meta).
    """
    text = _normalize_text(message)
    corrections: list[str] = []
    if not text:
        return "", {"corrections": corrections, "had_typos": False}

    # Frases/fragmentos antes da tokenização fina.
    phrase_fixes = (
        (r"\be\s*-\s*ma\b", "e-mail"),
        (r"\be\s+ma\b", "e-mail"),
        (r"\be\s*mail\b", "e-mail"),
        (r"\bsem\s+frete\s+calculado\??\b", "sem frete calculado"),
    )
    for pattern, replacement in phrase_fixes:
        updated = re.sub(pattern, replacement, text)
        if updated != text:
            corrections.append(f"{pattern}->{replacement}")
            text = updated

    tokens = text.split()
    fixed_tokens: list[str] = []
    for token in tokens:
        bare = token.strip(".,;:!?()[]\"'")
        suffix = token[len(bare) :] if bare else ""
        prefix_len = len(token) - len(token.lstrip(".,;:!?()[]\"'"))
        # token may have leading punctuation rarely; keep simple.
        mapped = _DOMAIN_TYPO_MAP.get(bare) or _DOMAIN_TYPO_MAP.get(_strip_accents(bare))
        if mapped and mapped != bare:
            corrections.append(f"{bare}->{mapped}")
            fixed_tokens.append(mapped + suffix)
            continue
        fuzzy = _fuzzy_domain_token(bare)
        if fuzzy and fuzzy != bare and _strip_accents(fuzzy) != _strip_accents(bare):
            # Evita "corrigir" palavras já corretas só por acento.
            corrections.append(f"{bare}->{fuzzy}")
            fixed_tokens.append(fuzzy + suffix)
            continue
        fixed_tokens.append(token)

    normalized = " ".join(fixed_tokens)
    return normalized, {"corrections": corrections, "had_typos": bool(corrections)}


def _safe_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not (parsed == parsed):  # NaN
        return None
    return parsed


def format_brl(value) -> str:
    num = _safe_float(value)
    if num is None:
        return "não informado"
    formatted = f"{abs(num):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    prefix = "-" if num < 0 else ""
    return f"R$ {prefix}{formatted}"


def stale_warning(bundle: dict) -> str:
    if not bundle.get("needs_reprocess") and not bundle.get("stale_reason"):
        return ""
    reason = (bundle.get("stale_reason") or "dados possivelmente desatualizados").strip()
    return (
        f"⚠️ **Atenção:** os dados deste lote podem estar desatualizados ({reason}). "
        "Recomendo reprocessar a auditoria antes de conclusões definitivas.\n\n"
    )


def judgment_prudence_note() -> str:
    """Ressalva curta e contextual — só para perguntas que exigem julgamento."""
    return (
        "\n\nEssa análise indica uma divergência que merece validação, "
        "mas a confirmação final depende da sua revisão dos documentos e regras aplicáveis."
    )


_NUMBER_WORDS_PT = {
    "um": 1,
    "uma": 1,
    "dois": 2,
    "duas": 2,
    "três": 3,
    "tres": 3,
    "quatro": 4,
    "cinco": 5,
    "seis": 6,
    "dez": 10,
    "doze": 12,
    "vinte": 20,
}


def _parse_quantity_token(token: str) -> int | None:
    raw = str(token or "").strip().lower()
    if not raw:
        return None
    if raw in _NUMBER_WORDS_PT:
        return _NUMBER_WORDS_PT[raw]
    try:
        return int(raw)
    except ValueError:
        return None


def extract_top_n_explicit(message: str) -> int | None:
    """Extrai quantidade pedida pelo usuário, ou None se não houver quantidade explícita."""
    text = _normalize_text(message)
    words_alt = "|".join(sorted(_NUMBER_WORDS_PT.keys(), key=len, reverse=True))
    patterns = (
        r"\btop\s+(\d{1,2})\b",
        rf"\btop\s+({words_alt})\b",
        r"\b(?:as|os|às)\s+(\d{1,2})\s+(?:maiores|menores|principais|primeir[oa]s)\b",
        rf"\b(?:as|os|às)\s+({words_alt})\s+(?:maiores|menores|principais|primeir[oa]s)\b",
        r"\b(\d{1,2})\s+(?:maiores|menores|principais|primeir[oa]s)\b",
        rf"\b({words_alt})\s+(?:maiores|menores|principais|primeir[oa]s)\b",
        r"\b(?:quero|liste|listar|mostre|mostrar|traga|exiba|exibir)\s+(?:as\s+|os\s+|às\s+)?(\d{1,2})\b",
        rf"\b(?:quero|liste|listar|mostre|mostrar|traga|exiba|exibir)\s+(?:as\s+|os\s+|às\s+)?({words_alt})\b",
        r"\bme mostre\s+(\d{1,2})\b",
        rf"\bme mostre\s+({words_alt})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        parsed = _parse_quantity_token(match.group(1))
        if parsed is not None:
            return min(RANKING_MAX_ITEMS, max(1, parsed))
    return None


def extract_top_n(message: str, *, default: int = DEFAULT_TOP_N) -> int:
    explicit = extract_top_n_explicit(message)
    if explicit is not None:
        return explicit
    return min(RANKING_MAX_ITEMS, max(1, default))


def resolve_ranking_limit(
    message: str,
    *,
    conversation_focus: dict | None = None,
    default: int = DEFAULT_TOP_N,
) -> tuple[int, bool]:
    """
    Retorna (limite, quantidade_explicita).
    Em follow-up de direção ("e a menor?"), reutiliza last_ranking_limit se existir.
    """
    explicit = extract_top_n_explicit(message)
    if explicit is not None:
        return explicit, True
    text = _normalize_text(message)
    if _is_ranking_direction_followup(text) and isinstance(conversation_focus, dict):
        reused = conversation_focus.get("last_ranking_limit")
        try:
            if reused is not None:
                return min(RANKING_MAX_ITEMS, max(1, int(reused))), False
        except (TypeError, ValueError):
            pass
    return min(RANKING_MAX_ITEMS, max(1, default)), False


def _is_ranking_direction_followup(text: str) -> bool:
    compact = _normalize_text(text).strip(" ?!.")
    return compact in {
        "e a menor",
        "e as menores",
        "a menor",
        "agora a menor",
        "e cobrado a menor",
        "e a maior",
        "e as maiores",
        "a maior",
        "agora a maior",
        "e a mais",
        "e cobrado a maior",
        "e cobrado a mais",
        "e cobradas a menor",
        "e cobradas a maior",
    }


def _ranking_direction_from_text(text: str) -> str | None:
    """Retorna overcharged/undercharged/absolute ou None se não for ranking."""
    under_hints = (
        "cobrado a menor",
        "cobrada a menor",
        "cobrados a menor",
        "cobradas a menor",
        "cobrança a menor",
        "cobranca a menor",
        "cobranças a menor",
        "cobrancas a menor",
        "a menor que o",
        "menor que o cálculo",
        "menor que o calculo",
        "menor que o nosso",
        "abaixo do esperado",
        "abaixo do nosso cálculo",
        "abaixo do nosso calculo",
        "divergência a menor",
        "divergencia a menor",
        "divergências a menor",
        "divergencias a menor",
    )
    over_hints = (
        "cobrado a maior",
        "cobrada a maior",
        "cobrados a maior",
        "cobradas a maior",
        "cobrado a mais",
        "cobrada a mais",
        "cobrados a mais",
        "cobradas a mais",
        "cobrança a maior",
        "cobranca a maior",
        "cobrança a mais",
        "cobranca a mais",
        "cobranças a maior",
        "cobrancas a maior",
        "cobranças a mais",
        "cobrancas a mais",
        "a maior que o",
        "maior que o cálculo",
        "maior que o calculo",
        "maior que o nosso",
        "acima do esperado",
        "acima do nosso cálculo",
        "acima do nosso calculo",
        "divergência a maior",
        "divergencia a maior",
        "divergências a maior",
        "divergencias a maior",
    )
    if any(hint in text for hint in under_hints) or (
        "a menor" in text and ("diverg" in text or "cobr" in text or "calcul" in text)
    ):
        return INTENT_UNDERCHARGED
    if any(hint in text for hint in over_hints) or (
        ("a maior" in text or "a mais" in text)
        and ("diverg" in text or "cobr" in text or "calcul" in text)
    ):
        return INTENT_OVERCHARGED
    return None


def _extract_document_number(message: str) -> str | None:
    text = _normalize_text(message)
    patterns = (
        r"(?:ct-?e|cte|documento|nota|nf(?:-?e)?)\s*[#:]?\s*(\d+)",
        r"\bn[ºo°\.]?\s*(\d{3,})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


def _extract_row_index(message: str) -> int | None:
    text = _normalize_text(message)
    match = re.search(r"\blinha\s*(\d+)\b", text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def find_rows_by_document(bundle: dict, document_number: str) -> list[dict]:
    target = str(document_number).strip()
    if not target:
        return []
    matches: list[dict] = []
    for row in bundle.get("merged_rows") or []:
        doc = str(row.get("document_number") or "").strip()
        if doc == target:
            matches.append(row)
    return matches


def find_row_by_index(bundle: dict, row_index: int) -> dict | None:
    for row in bundle.get("merged_rows") or []:
        try:
            if int(row.get("row_index")) == int(row_index):
                return row
        except (TypeError, ValueError):
            continue
    return None


def resolve_document_target(bundle: dict, message: str) -> dict[str, Any]:
    row_index = _extract_row_index(message)
    if row_index is not None:
        row = find_row_by_index(bundle, row_index)
        if row:
            return {"kind": "single", "rows": [row], "reference": f"linha {row_index}"}
        return {"kind": "not_found", "rows": [], "reference": f"linha {row_index}"}

    document_number = _extract_document_number(message)
    if document_number:
        matches = find_rows_by_document(bundle, document_number)
        if not matches:
            return {"kind": "not_found", "rows": [], "reference": document_number}
        if len(matches) == 1:
            return {"kind": "single", "rows": matches, "reference": document_number}
        return {"kind": "duplicate", "rows": matches, "reference": document_number}

    return {"kind": "none", "rows": [], "reference": None}


def _user_will_send(text: str) -> bool:
    return any(hint in text for hint in _USER_WILL_SEND_HINTS)


def _mentions_communication_target(text: str) -> bool:
    return any(
        token in text
        for token in (
            "e-mail",
            "email",
            "e mail",
            "minuta",
            "mensagem",
            "comunicado",
            "equipe",
            "diretoria",
            "chefes",
            "whatsapp",
        )
    )


def _has_platform_send_request(text: str) -> bool:
    """
    True quando o usuário pede que a plataforma envie.
    'para eu enviar' com prepare/conteúdo NÃO é envio pela plataforma.
    """
    if any(re.search(pattern, text) for pattern in _PLATFORM_SEND_IMPERATIVE_PATTERNS):
        return True
    if any(re.search(pattern, text) for pattern in _PLATFORM_SEND_INFINITIVE_PATTERNS):
        # Prepare/redija ... para eu enviar → permitido.
        if _user_will_send(text) and (
            _has_content_prepare(text) or _mentions_email_or_message(text) or "minuta" in text
        ):
            return False
        if _user_will_send(text) and not any(
            re.search(pattern, text) for pattern in _PLATFORM_SEND_IMPERATIVE_PATTERNS
        ):
            return False
        return True
    return False


def _has_non_send_execution_action(text: str) -> bool:
    return any(re.search(pattern, text) for pattern in _NON_SEND_EXECUTION_PATTERNS)


def _has_execution_action(text: str) -> bool:
    """Compat: qualquer execução externa (envio proibido ou outras ações)."""
    return _has_platform_send_request(text) or _has_non_send_execution_action(text)


def _has_content_prepare(text: str) -> bool:
    return any(hint in text for hint in _CONTENT_PREPARE_HINTS)


def _mentions_email_or_message(text: str) -> bool:
    return any(
        token in text
        for token in (
            "e-mail",
            "email",
            "e mail",
            "mensagem para",
            "comunicado",
            "whatsapp",
            "minuta",
        )
    )


def _mentions_content_artifact(text: str) -> bool:
    return any(token in text for token in _CONTENT_ARTIFACT_HINTS)


def _is_action_plan_request(text: str) -> bool:
    return any(
        token in text
        for token in (
            "plano de ação",
            "plano de acao",
            "plano de acção",
            "action plan",
        )
    ) or (
        _has_content_prepare(text)
        and "plano" in text
        and "assinatura" not in text
        and "contratar" not in text
        and "cancelar" not in text
    )


def _is_subscription_or_platform_out_of_scope(text: str) -> bool:
    if _is_action_plan_request(text):
        hints = [
            hint
            for hint in OUT_OF_SCOPE_HINTS
            if "plano" not in hint and hint not in {"meu plano"}
        ]
    else:
        hints = list(OUT_OF_SCOPE_HINTS)
    return any(hint in text for hint in hints)


def _has_recent_document_focus(conversation_focus: dict | None) -> bool:
    if not isinstance(conversation_focus, dict):
        return False
    return bool(conversation_focus.get("document_number") or conversation_focus.get("row_index") is not None)


def _is_anaphoric_or_followup(text: str, conversation_focus: dict | None) -> bool:
    if not _has_recent_document_focus(conversation_focus):
        return False
    if any(hint in text for hint in _ANAPHORA_HINTS):
        return True
    if any(hint in text for hint in _FOLLOWUP_TOPIC_HINTS):
        # Evita capturar rankings gerais ("maiores divergências") quando não há âncora anafórica.
        if any(token in text for token in ("maiores", "menores", "ranking", "top ", "lista", "todas", "todos")):
            return False
        if "cidades" in text or "sem frete" in text:
            return False
        return True
    # Perguntas curtas com foco recente.
    return len(text.split()) <= 8 and any(
        token in text for token in ("e o ", "e a ", "qual ", "quanto", "por que", "porque")
    )


def _classify_textual_content(text: str) -> str | None:
    if (
        _mentions_email_or_message(text)
        or ("executivo" in text and any(token in text for token in ("e-mail", "email", "minuta", "mensagem", "comunicado")))
    ) and (
        _has_content_prepare(text)
        or any(
            token in text
            for token in (
                "minuta",
                "rascunho",
                "assunto",
                "para meus chefes",
                "para a diretoria",
                "para diretoria",
                "para minha equipe",
                "para a equipe",
                "para eu enviar",
                "para eu mandar",
                "executivo",
            )
        )
    ):
        return INTENT_MANAGEMENT_EMAIL_DRAFT

    if _has_content_prepare(text) and _mentions_content_artifact(text):
        if _mentions_email_or_message(text) or "minuta" in text or "comunicado" in text:
            return INTENT_MANAGEMENT_EMAIL_DRAFT
        if "briefing" in text or "negoci" in text:
            return INTENT_CARRIER_NEGOTIATION_BRIEF
        if "plano" in text:
            return INTENT_ACTION_PLAN
        if "relatório" in text or "relatorio" in text:
            return INTENT_AUDIT_FINDINGS_REPORT
        if (
            "resumo" in text
            or "análise executiva" in text
            or "analise executiva" in text
            or "apresent" in text
            or ("executivo" in text and "resumo" in text)
            or ("executivo" in text and "auditoria" in text and _has_content_prepare(text))
        ):
            return INTENT_EXECUTIVE_SUMMARY

    if any(
        token in text
        for token in (
            "resumo executivo",
            "síntese executiva",
            "sintese executiva",
            "visão executiva",
            "visao executiva",
            "análise executiva",
            "analise executiva",
            "para diretoria",
            "para a diretoria",
            "resuma para",
            "explique para diretoria",
        )
    ):
        return INTENT_EXECUTIVE_SUMMARY

    if _is_action_plan_request(text):
        return INTENT_ACTION_PLAN

    if any(
        token in text
        for token in (
            "negociar com a transportadora",
            "negociar com transportadora",
            "negociação com a transportadora",
            "negociacao com a transportadora",
            "briefing de negociação",
            "briefing de negociacao",
            "o que devo negociar",
            "o que negociar",
        )
    ):
        return INTENT_CARRIER_NEGOTIATION_BRIEF

    if any(
        token in text
        for token in (
            "atacar primeiro",
            "prioriz",
            "prioridade",
            "revisão urgente",
            "revisao urgente",
            "merecem revisão",
            "merecem revisao",
            "pontos devo atacar",
            "o que atacar",
            "documentos urgentes",
        )
    ):
        return INTENT_PRIORITIZATION

    if any(
        token in text
        for token in (
            "hipótese",
            "hipotese",
            "hipóteses",
            "hipoteses",
            "mais prováv",
            "mais provav",
            "causas mais",
            "possíveis causas",
            "possiveis causas",
            "root cause",
        )
    ):
        return INTENT_ROOT_CAUSE_HYPOTHESES

    if any(
        token in text
        for token in (
            "alerta de risco",
            "alertas de risco",
            "riscos do lote",
            "riscos financeiros",
            "quais riscos",
        )
    ):
        return INTENT_RISK_ALERTS

    if any(
        token in text
        for token in (
            "próximos passos",
            "proximos passos",
            "proximo passo",
            "próximo passo",
            "next steps",
            "o que fazer agora",
            "o que faço agora",
        )
    ):
        return INTENT_NEXT_STEPS

    if any(
        token in text
        for token in (
            "impacto financeiro",
            "impacto de negócio",
            "impacto de negocio",
            "impacto no negócio",
            "impacto no negocio",
            "linguagem simples",
            "explique o impacto",
            "impacto em linguagem",
        )
    ):
        return INTENT_EXPLAIN_BUSINESS_IMPACT

    if any(
        token in text
        for token in (
            "relatório de achados",
            "relatorio de achados",
            "relatório da auditoria",
            "relatorio da auditoria",
            "findings",
            "achados da auditoria",
        )
    ):
        return INTENT_AUDIT_FINDINGS_REPORT

    if "resumo" in text and _has_content_prepare(text):
        return INTENT_EXECUTIVE_SUMMARY

    if "resumo" in text and "lote" not in text:
        return INTENT_EXECUTIVE_SUMMARY

    return None


def classify_intent(
    message: str,
    *,
    visual_focus: dict | None = None,
    conversation_focus: dict | None = None,
) -> str:
    """
    Precedência:
    1) ação externa proibida
    2) documento/linha explícitos
    3) continuação anafórica com foco recente
    4) cidades/documentos sem frete calculado
    5) intenções gerenciais/textuais
    6) rankings e agregados
    7) ambiguidade
    8) fora de escopo real
    """
    text, _norm_meta = normalize_insights_message(message)
    if not text:
        return INTENT_AMBIGUOUS

    # 1) Ações externas — "para eu enviar" com prepare NÃO bloqueia.
    if _has_platform_send_request(text):
        if _mentions_communication_target(text) or "esse e-mail" in text or "este e-mail" in text:
            return INTENT_SEND_EMAIL_BLOCKED
        return INTENT_OUT_OF_SCOPE

    if _has_non_send_execution_action(text):
        return INTENT_OUT_OF_SCOPE

    if _is_subscription_or_platform_out_of_scope(text):
        return INTENT_OUT_OF_SCOPE

    if any(hint in text for hint in CHARGE_VALIDITY_HINTS):
        return INTENT_CHARGE_VALIDITY

    has_doc_ref = bool(_extract_document_number(message) or _extract_row_index(message))

    # 2) Documento/linha explícitos
    if has_doc_ref and any(
        token in text
        for token in (
            "explique",
            "explicar",
            "explica",
            "cálculo",
            "calculo",
            "como foi calculado",
            "memória",
            "memoria",
            "me explique",
            "me explica",
        )
    ):
        return INTENT_EXPLAIN_CALCULATION

    if has_doc_ref and any(token in text for token in ("onde", "localiz", "achar", "encont", "qual linha")):
        return INTENT_LOCATE_DOCUMENT

    if has_doc_ref and any(
        token in text for token in ("cálculo", "calculo", "diverg", "cobrad", "esperad", "imposto", "peso", "taxa")
    ):
        return INTENT_EXPLAIN_CALCULATION

    if has_doc_ref:
        return INTENT_LOCATE_DOCUMENT

    # 2b) Follow-up de direção de ranking ("e a menor?") — antes do foco documental
    if _is_ranking_direction_followup(text):
        if isinstance(conversation_focus, dict) and conversation_focus.get("last_ranking_limit") is not None:
            if "menor" in text:
                return INTENT_UNDERCHARGED
            return INTENT_OVERCHARGED
        return INTENT_AMBIGUOUS

    # 3) Continuação anafórica / pergunta curta com foco recente
    if _is_anaphoric_or_followup(text, conversation_focus):
        return INTENT_DOCUMENT_FOLLOWUP

    # 4) Cidades/documentos sem frete calculado
    if any(
        token in text
        for token in (
            "por que essas cidades",
            "porque essas cidades",
            "por que não calcularam",
            "porque nao calcularam",
            "por que nao calcularam",
            "por que não calcularam",
            "sem frete calculado",
            "ficaram sem frete",
            "estão sem frete",
            "estao sem frete",
            "não calcularam",
            "nao calcularam",
            "não calculou",
            "nao calculou",
        )
    ):
        if "documento" in text or "linha" in text:
            return INTENT_UNCALCULATED_DOCUMENTS
        if any(token in text for token in ("por que", "porque", "motivo", "razão", "razao")):
            return INTENT_EXPLAIN_UNCALCULATED_REASONS
        if "cidade" in text:
            return INTENT_UNCALCULATED_CITIES
        if "documento" in text:
            return INTENT_UNCALCULATED_DOCUMENTS
        return INTENT_UNCALCULATED_CITIES

    if "cidade" in text and any(token in text for token in ("sem cálculo", "sem calculo", "não calcul", "nao calcul", "sem frete")):
        return INTENT_UNCALCULATED_CITIES

    if "documento" in text and any(token in text for token in ("sem cálculo", "sem calculo", "não calcul", "nao calcul", "sem frete")):
        return INTENT_UNCALCULATED_DOCUMENTS

    # 5) Intenções gerenciais e textuais
    textual = _classify_textual_content(text)
    if textual:
        return textual

    # 6) Rankings e agregados — direção (a maior/a menor) tem precedência sobre absoluto
    direction = _ranking_direction_from_text(text)
    if direction == INTENT_UNDERCHARGED:
        return INTENT_UNDERCHARGED
    if direction == INTENT_OVERCHARGED:
        return INTENT_OVERCHARGED

    if ("menores diverg" in text or "menor diverg" in text) and "a menor" not in text and "cobrad" not in text:
        return INTENT_SMALLEST_DIVERGENCES

    if "maiores diverg" in text or "maior diverg" in text or (
        any(token in text for token in ("liste", "listar", "quero as", "quero os", "top "))
        and "diverg" in text
    ):
        return INTENT_TOP_DIVERGENCES

    if any(token in text for token in ("pareto", "confiança", "confianca", "kpi", "gráfico", "grafico", "dashboard", "bi")):
        return INTENT_EXPLAIN_CHART

    if any(
        token in text
        for token in (
            "impacto por transportadora",
            "impacto por uf",
            "por transportadora",
            "por uf",
            "uf destino",
        )
    ):
        return INTENT_EXPLAIN_CARRIER_UF

    if "transportadora" in text and "negoci" not in text:
        return INTENT_EXPLAIN_CARRIER_UF

    if any(token in text for token in ("causa", "motivo")) and "cidade" not in text:
        return INTENT_ROOT_CAUSE_HYPOTHESES

    if any(
        token in text
        for token in (
            "resumo do lote",
            "panorama",
            "visão geral",
            "visao geral",
            "overview",
            "situação do lote",
            "situacao do lote",
        )
    ):
        return INTENT_BATCH_SUMMARY

    if visual_focus and isinstance(visual_focus, dict):
        if any(visual_focus.get(key) for key in ("chart_key", "carrier", "destination_uf")):
            return INTENT_EXPLAIN_CHART

    if any(token in text for token in ("ranking", "top ", "listar", "liste", "maiores diverg")):
        return INTENT_TOP_DIVERGENCES

    if "documentos" in text and any(token in text for token in ("revis", "urgent", "prior")):
        return INTENT_PRIORITIZATION

    if "documentos" in text and "sem frete" not in text:
        return INTENT_TOP_DIVERGENCES

    # 7) Ambiguidade natural no domínio
    if len(text.split()) <= 3 or any(token in text for token in ("isso", "aquele", "dessa", "me explica")):
        return INTENT_AMBIGUOUS

    if "diverg" in text and not any(token in text for token in ("maiores", "menores", "ranking", "top ")):
        return INTENT_AMBIGUOUS

    # 8) Fora de escopo real
    return INTENT_OUT_OF_SCOPE


def _row_divergence(row: dict) -> float | None:
    divergence = _safe_float(row.get("divergence_value"))
    if divergence is not None:
        return divergence
    charged = _safe_float(row.get("charged_freight"))
    expected = _safe_float(row.get("expected_freight"))
    if charged is None or expected is None:
        return None
    return charged - expected


def _apply_visual_filters(rows: list[dict], visual_focus: dict | None) -> list[dict]:
    if not visual_focus or not isinstance(visual_focus, dict):
        return rows
    filtered = rows
    carrier = visual_focus.get("carrier")
    if isinstance(carrier, str) and carrier.strip():
        carrier_val = carrier.strip()
        filtered = [row for row in filtered if str(row.get("carrier") or "").strip() == carrier_val]
    destination_uf = visual_focus.get("destination_uf")
    if isinstance(destination_uf, str) and destination_uf.strip():
        uf_val = destination_uf.strip().upper()
        filtered = [
            row for row in filtered if str(row.get("destination_uf") or "").strip().upper() == uf_val
        ]
    origin_uf = visual_focus.get("origin_uf")
    if isinstance(origin_uf, str) and origin_uf.strip():
        origin_val = origin_uf.strip().upper()
        filtered = [
            row for row in filtered if str(row.get("origin_uf") or "").strip().upper() == origin_val
        ]
    issue_date = visual_focus.get("issue_date")
    if isinstance(issue_date, str) and issue_date.strip():
        date_val = issue_date.strip()
        filtered = [row for row in filtered if str(row.get("issue_date") or "").strip() == date_val]
    return filtered


def is_uncalculated_row(row: dict) -> bool:
    """Linha sem frete calculado (não confundir com divergente calculado)."""
    if not isinstance(row, dict):
        return False
    status = str(row.get("status") or "").strip()
    reason = str(row.get("reason_code") or "").strip()
    if status in UNCALCULATED_REASON_CODES or reason in UNCALCULATED_REASON_CODES:
        return True
    expected = _safe_float(row.get("expected_freight"))
    if expected is not None and status in {"ok", "divergent"}:
        return False
    if expected is None:
        return True
    divergence = _safe_float(row.get("divergence_value"))
    charged = _safe_float(row.get("charged_freight"))
    if divergence is None and (charged is None or expected is None):
        return True
    return False


def build_ranking(bundle: dict, intent: str, *, limit: int, visual_focus: dict | None = None) -> list[dict]:
    rows = _apply_visual_filters(list(bundle.get("merged_rows") or []), visual_focus)
    scored: list[tuple[float, dict]] = []
    for row in rows:
        if is_uncalculated_row(row):
            continue
        if _safe_float(row.get("expected_freight")) is None:
            continue
        divergence = _row_divergence(row)
        if divergence is None:
            continue
        if intent == INTENT_OVERCHARGED and divergence <= 0:
            continue
        if intent == INTENT_UNDERCHARGED and divergence >= 0:
            continue
        if intent == INTENT_SMALLEST_DIVERGENCES and abs(divergence) <= 0.004:
            continue
        if intent == INTENT_TOP_DIVERGENCES:
            score = abs(divergence)
            scored.append((score, row))
        elif intent == INTENT_SMALLEST_DIVERGENCES:
            score = abs(divergence)
            scored.append((score, row))
        elif intent == INTENT_OVERCHARGED:
            scored.append((divergence, row))
        elif intent == INTENT_UNDERCHARGED:
            scored.append((abs(divergence), row))
        else:
            scored.append((abs(divergence), row))

    reverse = intent != INTENT_SMALLEST_DIVERGENCES
    scored.sort(key=lambda item: item[0], reverse=reverse)
    return [row for _, row in scored[:limit]]


def _row_label(row: dict) -> str:
    doc = str(row.get("document_number") or "").strip() or "sem número"
    line = row.get("row_index")
    carrier = str(row.get("carrier") or "").strip() or "transportadora não informada"
    destination = str(row.get("destination_uf") or "").strip() or "UF não informada"
    return f"Linha {line} · Doc. {doc} · {carrier} · Destino {destination}"


def format_ranking_response(bundle: dict, intent: str, rows: list[dict], *, limit: int) -> str:
    base_titles = {
        INTENT_TOP_DIVERGENCES: "Maiores divergências absolutas",
        INTENT_OVERCHARGED: "Cobranças a maior",
        INTENT_UNDERCHARGED: "Cobranças a menor",
        INTENT_SMALLEST_DIVERGENCES: "Menores divergências",
    }
    base = base_titles.get(intent, "Ranking")
    found = len(rows)
    if found == 0:
        title = f"{base} (top {limit})"
        return (
            stale_warning(bundle)
            + f"**{title}**\n\n"
            "Não encontrei linhas com divergência financeira calculável para este filtro."
        )

    if found < limit:
        title = f"{base} — encontrei {found} de {limit} solicitadas"
        shortage_note = f"Encontrei apenas {found} ocorrência(s) nesse critério.\n"
    else:
        title = f"{base} (top {found})"
        shortage_note = ""

    lines = [stale_warning(bundle) + f"**{title}**\n", shortage_note]
    for index, row in enumerate(rows, start=1):
        divergence = _row_divergence(row)
        charged = format_brl(row.get("charged_freight"))
        expected = format_brl(row.get("expected_freight"))
        if intent == INTENT_UNDERCHARGED:
            div_text = f"{format_brl(abs(divergence or 0))} (cobrado a menor)"
        elif intent == INTENT_OVERCHARGED:
            div_text = f"{format_brl(abs(divergence or 0))} (cobrado a maior)"
        else:
            div_text = format_brl(divergence)
        lines.append(
            f"{index}. {_row_label(row)}\n"
            f"   - Cobrado: {charged} · Esperado: {expected} · Divergência: {div_text}"
        )
    if limit >= RANKING_MAX_ITEMS:
        lines.append(
            f"\n*Exibindo até {RANKING_MAX_ITEMS} itens. Use filtros ou exportação para analisar o lote completo.*"
        )
    return "\n".join(line for line in lines if line)


def format_duplicate_document_options(bundle: dict, rows: list[dict], reference: str) -> str:
    lines = [
        stale_warning(bundle),
        f"Encontrei **{len(rows)} linhas** com o identificador **{reference}**. "
        "Preciso que você esclareça qual delas deseja analisar:\n",
    ]
    for row in rows:
        divergence = _row_divergence(row)
        lines.append(
            f"- Linha {row.get('row_index')} · {str(row.get('carrier') or 'transportadora não informada')} · "
            f"Destino {str(row.get('destination_uf') or 'UF não informada')} · "
            f"Cobrado {format_brl(row.get('charged_freight'))} · "
            f"Esperado {format_brl(row.get('expected_freight'))} · "
            f"Divergência {format_brl(divergence)}"
        )
    lines.append(
        "\nInforme a **linha** desejada (ex.: \"linha 2\") ou detalhe transportadora/destino para eu refinar."
    )
    return "\n".join(lines)


def format_document_not_found(bundle: dict, reference: str) -> str:
    return (
        stale_warning(bundle)
        + f"Não encontrei documento ou linha correspondente a **{reference}** neste lote processado.\n\n"
        "Verifique o número informado ou peça o ranking de maiores divergências."
    )


def format_document_location(bundle: dict, row: dict, reference: str) -> str:
    divergence = _row_divergence(row)
    return (
        stale_warning(bundle)
        + f"**Localização — {reference}**\n\n"
        f"- Linha: {row.get('row_index')}\n"
        f"- Identificador informado: {str(row.get('document_number') or 'não informado')}\n"
        f"- Transportadora: {str(row.get('carrier') or 'não informada')}\n"
        f"- Destino: {str(row.get('destination_city') or 'cidade não informada')} / "
        f"{str(row.get('destination_uf') or 'UF não informada')}\n"
        f"- Cobrado: {format_brl(row.get('charged_freight'))}\n"
        f"- Esperado: {format_brl(row.get('expected_freight'))}\n"
        f"- Divergência: {format_brl(divergence)}\n"
        f"- Status: {str(row.get('status') or 'não informado')}"
    )


def _component_amount(components: dict, key: str) -> str | None:
    block = components.get(key)
    if isinstance(block, dict):
        amount = block.get("amount")
        if amount is not None:
            return format_brl(amount)
    if components.get(key) is not None and not isinstance(components.get(key), (dict, list)):
        return format_brl(components.get(key))
    return None


def _append_component_line(lines: list[str], label: str, value: str | None) -> None:
    if value is not None:
        lines.append(f"- {label}: {value}")


def _format_accessorial_item(item: dict) -> str:
    name = str(item.get("name") or item.get("label") or item.get("fee_name") or "taxa").strip()
    amount = format_brl(item.get("amount"))
    details = str(item.get("details") or "").strip()
    minimum_applied = item.get("minimum_applied")
    minimum_amount = item.get("minimum_amount")
    parts = [f"{name}: {amount}"]
    if details:
        parts.append(details)
    if minimum_amount is not None:
        suffix = "mínimo aplicado" if minimum_applied else "mínimo não aplicado"
        parts.append(f"{suffix} ({format_brl(minimum_amount)})")
    return " · ".join(parts)


def _humanize_calculation_basis(basis) -> str:
    code = str(basis or "").strip()
    labels = {
        "range_plus_excess_per_kg": "faixa fixa + excedente por kg",
        "fixed_range": "faixa fixa de peso",
        "direct_weight_rate": "tarifa direta por peso",
    }
    return labels.get(code, code)


def _append_excess_from_weight_details(lines: list[str], weight_block: dict, components: dict) -> None:
    """Extrai excedente dos detalhes persistidos em weight_freight quando não há bloco separado."""
    if isinstance(components.get("excess"), dict) or isinstance(components.get("weight_excess"), dict):
        return
    if not isinstance(weight_block, dict):
        return

    details = str(weight_block.get("details") or "").strip()
    if not details:
        return

    lower = details.lower()
    if "excedente" not in lower and "excesso" not in lower:
        return

    lines.append("- Excedente de peso (detalhes persistidos em frete por peso/faixa):")
    lines.append(f"  - {details}")

    faixa_match = re.search(r"faixa\s+at[eé]\s+([\d.,]+)\s*kg", details, flags=re.IGNORECASE)
    if faixa_match:
        lines.append(f"  - Limite/faixa de peso: até {faixa_match.group(1)} kg")

    kg_match = re.search(
        r"([\d.,]+)\s*kg\s*(?:excedente|excedentes|exced|acima)",
        details,
        flags=re.IGNORECASE,
    )
    if kg_match:
        lines.append(f"  - Peso excedente informado: {kg_match.group(1)} kg")

    rate_match = re.search(
        r"(?:r\$\s*)?([\d.,]+)\s*(?:/|\s*por\s*)kg",
        details,
        flags=re.IGNORECASE,
    )
    if rate_match:
        lines.append(f"  - Tarifa por kg (persistida): R$ {rate_match.group(1)}/kg")

    excess_amount = _component_amount(components, "excess") or _component_amount(components, "weight_excess")
    if excess_amount is not None:
        lines.append(f"  - Valor do excedente (persistido): {excess_amount}")

    basis = str(weight_block.get("basis") or "").strip()
    if basis == "range_plus_excess_per_kg":
        lines.append("  - Modelo aplicado: faixa fixa + excedente por kg")


def format_calculation_explanation(bundle: dict, row: dict) -> str:
    components = row.get("calculation_components") if isinstance(row.get("calculation_components"), dict) else {}
    lines = [
        stale_warning(bundle),
        f"**Explicação do cálculo — linha {row.get('row_index')} "
        f"(identificador {str(row.get('document_number') or 'sem número')})**\n",
        "**Fatos calculados (persistidos):**",
    ]

    weight_block = components.get("weight_freight")
    if isinstance(weight_block, dict):
        _append_component_line(lines, "Frete por peso/faixa", _component_amount(components, "weight_freight"))
        basis = weight_block.get("basis")
        if basis:
            lines.append(f"  - Base utilizada: {_humanize_calculation_basis(basis)}")
        details = str(weight_block.get("details") or "").strip()
        if details:
            lines.append(f"  - Detalhes da faixa/peso: {details}")
        _append_excess_from_weight_details(lines, weight_block, components)
    elif row.get("weight_freight") is not None:
        _append_component_line(lines, "Frete por peso/faixa", format_brl(row.get("weight_freight")))

    excess_block = components.get("excess") or components.get("weight_excess")
    if isinstance(excess_block, dict):
        _append_component_line(lines, "Excedente de peso", _component_amount(components, "excess") or _component_amount(components, "weight_excess"))
        excess_details = str(excess_block.get("details") or "").strip()
        if excess_details:
            lines.append(f"  - Detalhes do excedente: {excess_details}")

    freight_value_block = components.get("freight_value") or components.get("tariff_freight_value")
    if isinstance(freight_value_block, dict):
        _append_component_line(lines, "Frete valor / ad valorem (%)", _component_amount(components, "freight_value") or _component_amount(components, "tariff_freight_value"))
        fv_details = str(freight_value_block.get("details") or "").strip()
        if fv_details:
            lines.append(f"  - {fv_details}")
    elif row.get("freight_value_amount") is not None:
        _append_component_line(lines, "Frete valor / ad valorem (%)", format_brl(row.get("freight_value_amount")))

    toll_block = components.get("route_toll") or components.get("tariff_route_toll")
    if isinstance(toll_block, dict):
        _append_component_line(lines, "Pedágio/rota", _component_amount(components, "route_toll") or _component_amount(components, "tariff_route_toll"))
        toll_details = str(toll_block.get("details") or "").strip()
        if toll_details:
            lines.append(f"  - {toll_details}")
    elif row.get("route_toll_amount") is not None:
        _append_component_line(lines, "Pedágio/rota", format_brl(row.get("route_toll_amount")))

    accessorial_fees = components.get("accessorial_fees")
    if isinstance(accessorial_fees, list) and accessorial_fees:
        lines.append("- Taxas acessórias fixas:")
        for item in accessorial_fees:
            if isinstance(item, dict):
                name_lower = str(item.get("name") or item.get("label") or "").lower()
                prefix = "  - GRIS/seguro: " if "gris" in name_lower or "seguro" in name_lower else "  - "
                lines.append(prefix + _format_accessorial_item(item))
    elif row.get("accessorial_fees_amount") is not None:
        _append_component_line(lines, "Taxas acessórias", format_brl(row.get("accessorial_fees_amount")))

    percent_fees = components.get("accessorial_percent_fees")
    if isinstance(percent_fees, list) and percent_fees:
        lines.append("- Taxas percentuais acessórias:")
        for item in percent_fees:
            if isinstance(item, dict):
                lines.append("  - " + _format_accessorial_item(item))
    elif row.get("accessorial_percent_fees_amount") is not None:
        _append_component_line(lines, "Taxas percentuais acessórias", format_brl(row.get("accessorial_percent_fees_amount")))

    ignored_fees = components.get("ignored_accessorial_fees")
    if isinstance(ignored_fees, list) and ignored_fees:
        lines.append("- Taxas ignoradas (com motivo):")
        for item in ignored_fees:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("label") or "taxa").strip()
            reason = str(item.get("reason") or item.get("reason_code") or item.get("ignored_reason") or "motivo não informado").strip()
            lines.append(f"  - {name}: {reason}")

    subtotal = components.get("subtotal_before_taxes")
    if subtotal is not None:
        _append_component_line(lines, "Subtotal antes dos impostos", format_brl(subtotal))

    tax_components = components.get("tax_components")
    if isinstance(tax_components, list) and tax_components:
        lines.append("- Impostos (ICMS/ISS):")
        for item in tax_components:
            if isinstance(item, dict):
                label = str(item.get("tax_type") or item.get("label") or "imposto").strip()
                lines.append(f"  - {label}: {format_brl(item.get('amount'))}")
    elif components.get("tax_total") is not None:
        _append_component_line(lines, "Total tributário", format_brl(components.get("tax_total")))

    _append_component_line(lines, "Frete esperado (calculado)", format_brl(row.get("expected_freight")))
    _append_component_line(lines, "Frete cobrado (registrado)", format_brl(row.get("charged_freight")))
    _append_component_line(lines, "Divergência (calculada)", format_brl(row.get("divergence_value")))
    lines.append(f"- Status: {str(row.get('status') or 'não informado')}")

    details = row.get("calculation_details")
    if isinstance(details, str) and details.strip():
        lines.append(f"\n**Detalhes registrados:** {details.strip()}")

    if len(lines) <= 4:
        return (
            stale_warning(bundle)
            + "**Explicação de cálculo**\n\n"
            "Não há memória de cálculo suficiente persistida para esta linha."
        )

    lines.append(
        "\n*Esta explicação descreve apenas componentes persistidos pelo motor de auditoria; "
        "não recalculei valores no chat.*"
    )
    return "\n".join(lines)


def _aggregate_by_field(rows: list[dict], field: str) -> list[dict]:
    grouped: dict[str, dict] = {}
    for row in rows:
        key = str(row.get(field) or "").strip()
        if not key:
            continue
        divergence = _row_divergence(row)
        if divergence is None:
            continue
        bucket = grouped.setdefault(
            key,
            {"key": key, "impacto_total": 0.0, "cobrado_a_mais": 0.0, "cobrado_a_menor": 0.0, "linhas": 0},
        )
        bucket["linhas"] += 1
        if divergence > 0:
            bucket["cobrado_a_mais"] += divergence
            bucket["impacto_total"] += divergence
        elif divergence < 0:
            bucket["cobrado_a_menor"] += abs(divergence)
            bucket["impacto_total"] += abs(divergence)
    return sorted(grouped.values(), key=lambda item: item["impacto_total"], reverse=True)


def _build_financial_metrics(rows: list[dict]) -> dict[str, Any]:
    metrics = {
        "total_rows": len(rows),
        "financial_rows": 0,
        "divergent_rows": 0,
        "overcharged": 0.0,
        "undercharged": 0.0,
        "absolute_impact": 0.0,
        "confidence_ratio": 0.0,
    }
    for row in rows:
        charged = _safe_float(row.get("charged_freight"))
        expected = _safe_float(row.get("expected_freight"))
        divergence = _row_divergence(row)
        if charged is not None and expected is not None:
            metrics["financial_rows"] += 1
        if divergence is None:
            continue
        if abs(divergence) > 0.004:
            metrics["divergent_rows"] += 1
        if divergence > 0:
            metrics["overcharged"] += divergence
            metrics["absolute_impact"] += divergence
        elif divergence < 0:
            metrics["undercharged"] += abs(divergence)
            metrics["absolute_impact"] += abs(divergence)
    if metrics["total_rows"]:
        metrics["confidence_ratio"] = (metrics["financial_rows"] / metrics["total_rows"]) * 100
    metrics["average_absolute_divergence"] = (
        metrics["absolute_impact"] / metrics["total_rows"] if metrics["total_rows"] else 0.0
    )
    return metrics


def _resolve_chart_key(message: str, visual_focus: dict | None) -> str | None:
    if visual_focus and isinstance(visual_focus, dict):
        raw = visual_focus.get("chart_key")
        if isinstance(raw, str) and raw.strip():
            return CHART_KEYS.get(raw.strip().lower(), raw.strip().lower())
    text = _normalize_text(message)
    for token, chart_key in CHART_KEYS.items():
        if token in text:
            return chart_key
    if "transportadora" in text:
        return "transportadora"
    if "uf" in text:
        return "uf_destino"
    return None


def _bi_source_note(from_audit_bi: bool) -> str:
    if from_audit_bi:
        return ""
    return "\n*Nota: BI indisponível neste lote; métricas derivadas do backend a partir dos resultados processados.*\n"


def format_batch_summary(bundle: dict, *, visual_focus: dict | None = None) -> str:
    from app.agente_compara_insights_bi import bi_rows_from_bundle, build_financial_metrics

    rows, from_bi = bi_rows_from_bundle(bundle, visual_focus)
    metrics = build_financial_metrics(rows)
    summary = bundle.get("summary") if isinstance(bundle.get("summary"), dict) else {}
    lines = [
        stale_warning(bundle),
        "**Resumo do lote auditado**\n",
        _bi_source_note(from_bi).strip(),
        f"- Arquivo: {str(bundle.get('source_file_name') or 'não informado')}",
        f"- Linhas analisadas: {metrics['total_rows']}",
        f"- Linhas com base financeira (cobrado e esperado): {metrics['financial_rows']}",
        f"- Linhas divergentes: {metrics['divergent_rows']}",
        f"- Confiança da auditoria: {metrics['confidence_ratio']:.1f}% ({metrics['confidence_label']})",
        f"- Cobrado a mais: {format_brl(metrics['overcharged'])}",
        f"- Cobrado a menor: {format_brl(metrics['undercharged'])}",
        f"- Impacto total: {format_brl(metrics['absolute_impact'])}",
        f"- Divergência média por documento: {format_brl(metrics['average_absolute_divergence'])}",
    ]
    if summary:
        processed = summary.get("processed_rows")
        divergent = summary.get("divergent_rows")
        if processed is not None:
            lines.append(f"- Linhas processadas (motor): {processed}")
        if divergent is not None:
            lines.append(f"- Divergentes (motor): {divergent}")
    return "\n".join(line for line in lines if line)


def format_chart_explanation(bundle: dict, *, message: str, visual_focus: dict | None) -> str:
    from app.agente_compara_insights_bi import (
        AUDIT_BI_TOP_N,
        aggregate_by_date_chronological,
        bi_rows_from_bundle,
        build_financial_metrics,
        build_overcharge_pareto,
        carrier_impact_top,
        uf_impact_top,
    )

    rows, from_bi = bi_rows_from_bundle(bundle, visual_focus)
    metrics = build_financial_metrics(rows)
    chart_key = _resolve_chart_key(message, visual_focus)
    filters_text = ""
    if visual_focus and isinstance(visual_focus, dict):
        active = {
            key: value
            for key, value in visual_focus.items()
            if key != "chart_key" and value not in (None, "", False)
        }
        if active:
            filters_text = f"\n**Filtros visuais informados:** {active}\n"

    lines = [
        stale_warning(bundle),
        "**Explicação do BI executivo**\n",
        _bi_source_note(from_bi).strip(),
        filters_text,
        "**KPIs (mesma base do gráfico exibido):**",
        f"- Confiança da auditoria: {metrics['confidence_label']} ({metrics['confidence_ratio']:.1f}%) — "
        f"{metrics['financial_rows']} de {metrics['total_rows']} linhas calculáveis",
        f"- Cobrado a mais: {format_brl(metrics['overcharged'])}",
        f"- Cobrado a menor: {format_brl(metrics['undercharged'])}",
        f"- Impacto total: {format_brl(metrics['absolute_impact'])}",
        f"- Divergência média por documento: {format_brl(metrics['average_absolute_divergence'])}",
        f"- Linhas divergentes: {metrics['divergent_rows']}",
        f"- Linhas analisadas: {metrics['total_rows']}",
    ]

    if chart_key == "transportadora":
        aggregates = carrier_impact_top(rows, limit=5)
        lines.append("\n**Impacto financeiro por transportadora (top 5 do BI):**")
        for item in aggregates:
            lines.append(
                f"- {item['chave']}: impacto {format_brl(item['impacto_total'])} "
                f"(a mais {format_brl(item['cobrado_a_mais'])} · a menor {format_brl(item['cobrado_a_menor'])})"
            )
    elif chart_key == "uf_destino":
        aggregates = uf_impact_top(rows, limit=5)
        lines.append("\n**Impacto financeiro por UF destino (top 5 do BI):**")
        for item in aggregates:
            lines.append(
                f"- {item['chave']}: impacto {format_brl(item['impacto_total'])} "
                f"({item['quantidade']} linhas)"
            )
    elif chart_key == "temporal":
        aggregates = aggregate_by_date_chronological(rows)[:AUDIT_BI_TOP_N]
        lines.append("\n**Evolução diária do impacto (ordem cronológica, como no gráfico):**")
        for item in aggregates:
            lines.append(
                f"- {item['data']}: impacto {format_brl(item['impacto_total'])} "
                f"(a mais {format_brl(item['cobrado_a_mais'])} · a menor {format_brl(item['cobrado_a_menor'])})"
            )
    elif chart_key == "pareto_transportadora":
        pareto = build_overcharge_pareto(rows, "carrier")
        lines.append("\n**Pareto do valor cobrado a mais por transportadora (somente positivos, como no BI):**")
        for item in pareto[:5]:
            lines.append(
                f"- {item['chave']}: {format_brl(item['valor'])} "
                f"({item['percentual']:.1f}% do total · acumulado {item['percentual_acumulado']:.1f}%)"
            )
    else:
        lines.append(
            "\n*Informe qual gráfico deseja detalhar: transportadora, UF destino, evolução temporal ou Pareto.*"
        )

    lines.append(
        "\n*Interpretação:* os valores refletem o BI executivo desta sessão; "
        "não estabelecem responsabilidade definitiva."
    )
    return "\n".join(line for line in lines if line is not None)


def format_carrier_uf_explanation(bundle: dict, *, message: str, visual_focus: dict | None) -> str:
    from app.agente_compara_insights_bi import bi_rows_from_bundle, carrier_impact_top, uf_impact_top

    rows, from_bi = bi_rows_from_bundle(bundle, visual_focus)
    text = _normalize_text(message)
    lines = [stale_warning(bundle), "**Impacto por transportadora / UF destino**\n", _bi_source_note(from_bi).strip()]
    if "transportadora" in text or (visual_focus or {}).get("carrier"):
        aggregates = carrier_impact_top(rows, limit=5)
        lines.append("**Por transportadora (top 5 do BI):**")
        for item in aggregates:
            lines.append(
                f"- {item['chave']}: impacto {format_brl(item['impacto_total'])} · {item['quantidade']} linhas"
            )
    if any(token in text for token in ("uf", "destino")) or (visual_focus or {}).get("destination_uf"):
        aggregates = uf_impact_top(rows, limit=5)
        lines.append("\n**Por UF destino (top 5 do BI):**")
        for item in aggregates:
            lines.append(
                f"- {item['chave']}: impacto {format_brl(item['impacto_total'])} · {item['quantidade']} linhas"
            )
    if len(lines) <= 3:
        carrier_agg = carrier_impact_top(rows, limit=3)
        uf_agg = uf_impact_top(rows, limit=3)
        lines.append("**Por transportadora (top 3 do BI):**")
        for item in carrier_agg:
            lines.append(f"- {item['chave']}: impacto {format_brl(item['impacto_total'])}")
        lines.append("\n**Por UF destino (top 3 do BI):**")
        for item in uf_agg:
            lines.append(f"- {item['chave']}: impacto {format_brl(item['impacto_total'])}")
    lines.append(
        "\n*Hipótese:* concentrações elevadas podem indicar tabela, rota ou regra de cobrança específica — "
        "valide com a transportadora e documentos de suporte antes de conclusões."
    )
    return "\n".join(line for line in lines if line)


def format_charge_validity_response(bundle: dict, message: str, *, visual_focus: dict | None = None) -> str:
    target = resolve_document_target(bundle, message)
    row = None
    if target["kind"] == "single":
        row = target["rows"][0]
    elif target["kind"] == "duplicate":
        return format_duplicate_document_options(bundle, target["rows"], str(target["reference"]))

    lines = [
        stale_warning(bundle),
        "**Análise prudente da cobrança**\n",
        "Uma divergência calculada **não equivale automaticamente** a cobrança indevida. "
        "Apresento abaixo os fatos persistidos pelo motor de auditoria.\n",
    ]

    if row:
        divergence = _row_divergence(row)
        has_divergence = divergence is not None and abs(divergence) > 0.004
        lines.extend(
            [
                f"- Documento/linha: {str(row.get('document_number') or 'não informado')} (linha {row.get('row_index')})",
                f"- Cobrado: {format_brl(row.get('charged_freight'))}",
                f"- Esperado (calculado): {format_brl(row.get('expected_freight'))}",
                f"- Divergência calculada: {format_brl(divergence)}",
                f"- Status: {str(row.get('status') or 'não informado')}",
            ]
        )
        if has_divergence:
            direction = "acima do esperado" if (divergence or 0) > 0 else "abaixo do esperado"
            lines.append(
                f"\n**Fato calculado:** há divergência ({direction}). "
                "Isso indica diferença entre o valor cobrado e o esperado com base na memória de cálculo persistida."
            )
            lines.append(
                "**Hipóteses (requerem validação):** diferença de tabela/faixa, taxa acessória, pedágio, "
                "imposto ou mapeamento de cobertura. Recomendo confrontar CT-e/NF, tabela contratada e evidências operacionais."
            )
        else:
            lines.append(
                "\n**Fato calculado:** não há divergência financeira relevante registrada para esta linha."
            )
    else:
        from app.agente_compara_insights_bi import bi_rows_from_bundle, build_financial_metrics

        rows, _from_bi = bi_rows_from_bundle(bundle, visual_focus)
        metrics = build_financial_metrics(rows)
        lines.extend(
            [
                "Não identifiquei documento específico na pergunta. Resumo do lote:",
                f"- Linhas divergentes: {metrics['divergent_rows']} de {metrics['total_rows']}",
                f"- Cobrado a mais (agregado): {format_brl(metrics['overcharged'])}",
                f"- Cobrado a menor (agregado): {format_brl(metrics['undercharged'])}",
            ]
        )
        lines.append(
            "\nPara avaliar uma cobrança específica, informe o número do documento ou a linha."
        )

    lines.append(judgment_prudence_note())
    return "\n".join(lines)


def format_out_of_scope(bundle: dict) -> str:
    return (
        stale_warning(bundle)
        + "Não consigo executar essa ação pela plataforma, mas posso preparar um texto, "
        "análise ou plano com base na auditoria."
    )


def format_send_email_blocked(bundle: dict) -> str:
    return (
        stale_warning(bundle)
        + "Não consigo enviar o e-mail pela plataforma, mas posso preparar ou ajustar "
        "a minuta para você enviar."
    )


def format_ambiguity(bundle: dict) -> str:
    return (
        stale_warning(bundle)
        + "Pode me dar um pouco mais de detalhe? Por exemplo, cite o documento/linha, "
        "peça as maiores divergências, cidades sem frete calculado, um resumo executivo "
        "ou a minuta de e-mail com a análise do lote."
    )


def _tax_summary_from_row(row: dict) -> dict[str, Any]:
    components = row.get("calculation_components") if isinstance(row.get("calculation_components"), dict) else {}
    taxes: list[dict[str, Any]] = []
    tax_components = components.get("tax_components")
    if isinstance(tax_components, list):
        for item in tax_components[:6]:
            if isinstance(item, dict):
                taxes.append(
                    {
                        "label": item.get("tax_type") or item.get("label") or "imposto",
                        "amount": item.get("amount"),
                    }
                )
    return {
        "tax_total": components.get("tax_total"),
        "items": taxes,
    }


def _compact_row_evidence(row: dict) -> dict[str, Any]:
    components = row.get("calculation_components") if isinstance(row.get("calculation_components"), dict) else {}
    component_keys = sorted(str(key) for key in components.keys())[:12]
    diagnostic = row.get("diagnostic") if isinstance(row.get("diagnostic"), dict) else {}
    return {
        "row_index": row.get("row_index"),
        "document_number": row.get("document_number"),
        "carrier": row.get("carrier"),
        "destination_city": row.get("destination_city"),
        "destination_uf": row.get("destination_uf"),
        "freight_region": row.get("freight_region"),
        "audited_weight": row.get("audited_weight"),
        "issue_date": row.get("issue_date"),
        "charged_freight": row.get("charged_freight"),
        "expected_freight": row.get("expected_freight"),
        "divergence_value": _row_divergence(row),
        "status": row.get("status"),
        "reason_code": row.get("reason_code"),
        "failure_stage": diagnostic.get("failure_stage"),
        "diagnostic_message": diagnostic.get("message"),
        "attempted_keys": list(diagnostic.get("attempted_keys") or [])[:8],
        "component_keys": component_keys,
        "taxes": _tax_summary_from_row(row),
        "uncalculated": is_uncalculated_row(row),
    }


def _diagnostic_group_label(group: dict) -> str:
    return str(
        group.get("title")
        or group.get("label")
        or group.get("code")
        or group.get("group_code")
        or "diagnóstico"
    ).strip()


def _diagnostic_group_count(group: dict) -> int | None:
    for key in ("affected_rows", "count", "total"):
        value = group.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def collect_uncalculated_rows(bundle: dict, *, visual_focus: dict | None = None) -> list[dict]:
    rows = _apply_visual_filters(list(bundle.get("merged_rows") or []), visual_focus)
    return [row for row in rows if is_uncalculated_row(row)]


def build_uncalculated_views(bundle: dict, *, visual_focus: dict | None = None) -> dict[str, Any]:
    rows = collect_uncalculated_rows(bundle, visual_focus=visual_focus)
    by_city: dict[str, dict] = {}
    by_reason: dict[str, dict] = {}
    evidence: list[dict] = []
    for row in rows:
        city = str(row.get("destination_city") or "").strip() or "cidade não informada"
        uf = str(row.get("destination_uf") or "").strip() or "UF n/d"
        city_key = f"{city}/{uf}"
        bucket = by_city.setdefault(
            city_key,
            {
                "city": city,
                "uf": uf,
                "key": city_key,
                "count": 0,
                "sample_documents": [],
                "reason_codes": {},
                "regions": set(),
            },
        )
        bucket["count"] += 1
        doc = str(row.get("document_number") or "").strip()
        if doc and doc not in bucket["sample_documents"] and len(bucket["sample_documents"]) < 3:
            bucket["sample_documents"].append(doc)
        reason = str(row.get("reason_code") or row.get("status") or "motivo_nao_informado").strip()
        bucket["reason_codes"][reason] = bucket["reason_codes"].get(reason, 0) + 1
        region = str(row.get("freight_region") or "").strip()
        if region:
            bucket["regions"].add(region)

        reason_bucket = by_reason.setdefault(
            reason,
            {"reason_code": reason, "label": REASON_CODE_LABELS.get(reason, reason), "count": 0},
        )
        reason_bucket["count"] += 1
        if len(evidence) < UNCALCULATED_EVIDENCE_TOP_N:
            evidence.append(_compact_row_evidence(row))

    city_list = []
    for item in by_city.values():
        top_reason = None
        if item["reason_codes"]:
            top_reason = max(item["reason_codes"].items(), key=lambda pair: pair[1])[0]
        city_list.append(
            {
                "city": item["city"],
                "uf": item["uf"],
                "key": item["key"],
                "count": item["count"],
                "sample_documents": item["sample_documents"],
                "top_reason_code": top_reason,
                "top_reason_label": REASON_CODE_LABELS.get(top_reason or "", top_reason or "motivo não informado"),
                "regions": sorted(item["regions"]),
            }
        )
    city_list.sort(key=lambda item: item["count"], reverse=True)
    reason_list = sorted(by_reason.values(), key=lambda item: item["count"], reverse=True)
    return {
        "total_rows": len(rows),
        "cities": city_list,
        "reasons": reason_list,
        "documents": evidence,
    }


def resolve_focus_row(bundle: dict, conversation_focus: dict | None) -> dict | None:
    if not isinstance(conversation_focus, dict):
        return None
    row_index = conversation_focus.get("row_index")
    if row_index is not None:
        try:
            row = find_row_by_index(bundle, int(row_index))
        except (TypeError, ValueError):
            row = None
        if row:
            return row
    document_number = conversation_focus.get("document_number")
    if document_number:
        matches = find_rows_by_document(bundle, str(document_number))
        if len(matches) == 1:
            return matches[0]
    return None


def format_document_followup(
    bundle: dict,
    row: dict,
    message: str,
    *,
    conversation_focus: dict | None = None,
) -> str:
    text = _normalize_text(message)
    doc = str(row.get("document_number") or "").strip() or "sem número"
    charged = row.get("charged_freight")
    expected = row.get("expected_freight")
    divergence = _row_divergence(row)
    acknowledge = any(hint in text for hint in _ANAPHORA_HINTS) or "estou falando" in text

    if is_uncalculated_row(row):
        reason = str(row.get("reason_code") or row.get("status") or "").strip()
        label = REASON_CODE_LABELS.get(reason, reason or "motivo não informado")
        prefix = f"Entendi, você está falando do documento {doc}. " if acknowledge else f"Sobre o documento {doc}: "
        return (
            stale_warning(bundle)
            + prefix
            + f"ele ficou sem frete calculado ({label}). "
            "Posso detalhar o motivo ou listar outras cidades/documentos na mesma situação."
        )

    if acknowledge:
        prefix = f"Entendi, você está falando do documento {doc}. "
    else:
        prefix = f"No documento {doc}, "

    if any(token in text for token in ("imposto", "icms", "iss")):
        taxes = _tax_summary_from_row(row)
        if taxes.get("items"):
            parts = [f"{item.get('label')}: {format_brl(item.get('amount'))}" for item in taxes["items"]]
            return stale_warning(bundle) + prefix + "os impostos persistidos foram: " + "; ".join(parts) + "."
        if taxes.get("tax_total") is not None:
            return stale_warning(bundle) + prefix + f"o total tributário persistido foi {format_brl(taxes.get('tax_total'))}."
        return stale_warning(bundle) + prefix + "não há detalhe de imposto persistido nessa linha."

    if "peso" in text:
        weight = row.get("audited_weight")
        if weight is None or weight == "":
            return stale_warning(bundle) + prefix + "o peso auditado não está informado nessa linha."
        return stale_warning(bundle) + prefix + f"o peso auditado foi {weight}."

    if "transportadora" in text:
        carrier = str(row.get("carrier") or "").strip() or "não informada"
        return stale_warning(bundle) + prefix + f"a transportadora é {carrier}."

    if "cidade" in text or "destino" in text:
        city = str(row.get("destination_city") or "").strip() or "cidade não informada"
        uf = str(row.get("destination_uf") or "").strip() or "UF n/d"
        return stale_warning(bundle) + prefix + f"o destino é {city}/{uf}."

    if any(token in text for token in ("taxa", "taxas", "component", "pedágio", "pedagio", "gris", "faixa")):
        return format_calculation_explanation(bundle, row)

    if any(token in text for token in ("por que", "porque", "motivo", "diverg")):
        if divergence is None:
            return stale_warning(bundle) + prefix + "não há divergência financeira calculável persistida."
        direction = "cobrado a mais" if divergence > 0 else "cobrado a menor" if divergence < 0 else "sem diferença"
        body = (
            f"a divergência foi de {format_brl(abs(divergence))} {direction}: "
            f"{format_brl(charged)} cobrado contra {format_brl(expected)} esperado."
        )
        if any(token in text for token in ("por que", "porque", "motivo")):
            body += (
                " Isso indica diferença entre cobrado e esperado; "
                "a causa específica merece validação na memória de cálculo e nas regras aplicadas."
            )
        return stale_warning(bundle) + prefix + body

    if divergence is not None:
        direction = "cobrado a mais" if divergence > 0 else "cobrado a menor" if divergence < 0 else "sem diferença"
        return (
            stale_warning(bundle)
            + prefix
            + f"a divergência foi de {format_brl(abs(divergence))} {direction}: "
            + f"{format_brl(charged)} cobrado contra {format_brl(expected)} esperado."
        )
    return format_calculation_explanation(bundle, row)


def _recommend_for_reason(reason_code: str | None, region: str | None = None) -> str:
    code = str(reason_code or "").strip()
    region_txt = f" da região {region}" if region else ""
    mapping = {
        "missing_coverage_mapping": "validar a classificação de cobertura da cidade/UF",
        "ambiguous_coverage_mapping": "desambiguar o mapeamento de cobertura",
        "missing_freight_rule": f"revisar a tarifa{region_txt} (regra ausente ou incompatível)",
        "unsupported_pricing_model": "verificar se o modelo de precificação está suportado na tabela",
        "invalid_weight": "corrigir o peso auditado na planilha",
        "invalid_charged_freight": "corrigir o valor de frete cobrado informado",
        "invalid_invoice_value": "corrigir o valor da NF informado",
    }
    return mapping.get(code, "revisar os dados de entrada e as tabelas cadastradas")


def format_uncalculated_cities(bundle: dict, *, visual_focus: dict | None = None) -> str:
    views = build_uncalculated_views(bundle, visual_focus=visual_focus)
    cities = views.get("cities") or []
    total_rows = int(views.get("total_rows") or 0)
    if not cities:
        return (
            stale_warning(bundle)
            + "Não encontrei cidades com linhas sem frete calculado neste lote. "
            "As linhas disponíveis têm frete esperado calculado (ainda que algumas possam estar divergentes)."
        )
    lines = [
        stale_warning(bundle),
        f"Encontrei **{len(cities)} cidade(s)** com linhas sem frete calculado, "
        f"totalizando **{total_rows} documento(s)/linha(s)**.",
    ]
    for item in cities[:6]:
        region = (item.get("regions") or [None])[0]
        reason_label = item.get("top_reason_label") or "motivo não informado"
        sample = ", ".join(item.get("sample_documents") or []) or "sem exemplo"
        region_bit = f" (região {region})" if region else ""
        lines.append(
            f"- **{item.get('city')}/{item.get('uf')}**: {item.get('count')} linha(s){region_bit}; "
            f"motivo provável: {reason_label}; docs ex.: {sample}."
        )
        lines.append(f"  Recomendação: {_recommend_for_reason(item.get('top_reason_code'), region)}.")
    return "\n".join(line for line in lines if line)


def format_uncalculated_documents(bundle: dict, *, visual_focus: dict | None = None) -> str:
    views = build_uncalculated_views(bundle, visual_focus=visual_focus)
    docs = views.get("documents") or []
    if not docs:
        return (
            stale_warning(bundle)
            + "Não encontrei documentos/linhas sem frete calculado neste lote."
        )
    lines = [
        stale_warning(bundle),
        f"Encontrei **{views.get('total_rows')} documento(s)/linha(s)** sem frete calculado. Principais:",
    ]
    for item in docs:
        reason = str(item.get("reason_code") or item.get("status") or "").strip()
        label = REASON_CODE_LABELS.get(reason, reason or "motivo não informado")
        city = item.get("destination_city") or "cidade n/d"
        uf = item.get("destination_uf") or "UF n/d"
        lines.append(
            f"- Linha {item.get('row_index')} · Doc. {item.get('document_number') or 's/n'} · "
            f"{city}/{uf} · {label}"
            + (f" · região {item.get('freight_region')}" if item.get("freight_region") else "")
        )
    return "\n".join(lines)


def format_uncalculated_reasons(bundle: dict, *, visual_focus: dict | None = None) -> str:
    views = build_uncalculated_views(bundle, visual_focus=visual_focus)
    cities = views.get("cities") or []
    reasons = views.get("reasons") or []
    if not cities and not reasons:
        return (
            stale_warning(bundle)
            + "Não há linhas sem frete calculado para explicar neste lote."
        )
    lines = [
        stale_warning(bundle),
        "Essas cidades/documentos não calcularam por motivos persistidos na auditoria:",
    ]
    for item in cities[:6]:
        region = (item.get("regions") or [None])[0]
        reason = item.get("top_reason_code")
        label = item.get("top_reason_label")
        region_bit = f", região {region}" if region else ""
        lines.append(
            f"- {item.get('city')}/{item.get('uf')}{region_bit}: {item.get('count')} linha(s) "
            f"com {label} (`{reason}`)."
        )
    if reasons:
        lines.append("\nDistribuição por motivo:")
        for item in reasons[:6]:
            lines.append(f"- {item.get('label')} (`{item.get('reason_code')}`): {item.get('count')} linha(s)")
    sample = (views.get("documents") or [None])[0]
    if isinstance(sample, dict) and sample.get("diagnostic_message"):
        lines.append(f"\nExemplo de diagnóstico: {sample.get('diagnostic_message')}")
    lines.append(
        "\nRecomendo revisar cobertura, regras tarifárias e campos de entrada "
        "(peso, frete cobrado e NF) conforme o motivo de cada cidade."
    )
    return "\n".join(line for line in lines if line)


def _collect_component_signals(rows: list[dict]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    taxes = 0
    toll = 0
    gris = 0
    excess = 0
    minimums = 0
    ignored = 0
    for row in rows:
        components = row.get("calculation_components") if isinstance(row.get("calculation_components"), dict) else {}
        for key in components.keys():
            key_str = str(key)
            counts[key_str] = counts.get(key_str, 0) + 1
        if components.get("tax_components") or components.get("tax_total") is not None:
            taxes += 1
        if components.get("route_toll") or components.get("tariff_route_toll") or row.get("route_toll_amount") is not None:
            toll += 1
        accessorial = components.get("accessorial_fees")
        if isinstance(accessorial, list):
            for item in accessorial:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or item.get("label") or "").lower()
                if "gris" in name or "seguro" in name:
                    gris += 1
                if item.get("minimum_applied"):
                    minimums += 1
        if isinstance(components.get("excess"), dict) or isinstance(components.get("weight_excess"), dict):
            excess += 1
        weight_block = components.get("weight_freight")
        if isinstance(weight_block, dict):
            details = str(weight_block.get("details") or "").lower()
            if "excedente" in details or "excesso" in details:
                excess += 1
        ignored_fees = components.get("ignored_accessorial_fees")
        if isinstance(ignored_fees, list) and ignored_fees:
            ignored += len(ignored_fees)
    predominant = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:8]
    return {
        "predominant_components": [{"component": key, "rows": count} for key, count in predominant],
        "rows_with_taxes": taxes,
        "rows_with_toll": toll,
        "rows_with_gris_signal": gris,
        "rows_with_excess_signal": excess,
        "rows_with_minimum_signal": minimums,
        "ignored_fee_mentions": ignored,
    }


def _share_pct(part: float, total: float) -> float:
    if total <= 0:
        return 0.0
    return round((part / total) * 100, 1)


def build_analytical_package(bundle: dict, *, visual_focus: dict | None = None) -> dict[str, Any]:
    """
    Pacote analítico fechado: apenas agregados e evidências compactas.
    Nunca inclui a planilha inteira nem todas as linhas.
    """
    from app.agente_compara_insights_bi import (
        aggregate_by_date_chronological,
        bi_rows_from_bundle,
        build_financial_metrics,
        build_overcharge_pareto,
        carrier_impact_top,
        uf_impact_top,
    )

    rows, from_bi = bi_rows_from_bundle(bundle, visual_focus)
    metrics = build_financial_metrics(rows)
    absolute = float(metrics.get("absolute_impact") or 0.0)
    overcharged = float(metrics.get("overcharged") or 0.0)
    undercharged = float(metrics.get("undercharged") or 0.0)
    net_impact = overcharged - undercharged
    total_rows = int(metrics.get("total_rows") or 0)
    financial_rows = int(metrics.get("financial_rows") or 0)

    top_abs = build_ranking(bundle, INTENT_TOP_DIVERGENCES, limit=ANALYTICAL_EVIDENCE_TOP_N, visual_focus=visual_focus)
    top_over = build_ranking(bundle, INTENT_OVERCHARGED, limit=ANALYTICAL_EVIDENCE_TOP_N, visual_focus=visual_focus)
    top_under = build_ranking(bundle, INTENT_UNDERCHARGED, limit=ANALYTICAL_EVIDENCE_TOP_N, visual_focus=visual_focus)

    carriers = carrier_impact_top(rows, limit=ANALYTICAL_EVIDENCE_TOP_N)
    ufs = uf_impact_top(rows, limit=ANALYTICAL_EVIDENCE_TOP_N)
    pareto = build_overcharge_pareto(rows, "carrier")[:ANALYTICAL_EVIDENCE_TOP_N]
    temporal = aggregate_by_date_chronological(rows)[:ANALYTICAL_EVIDENCE_TOP_N]
    # Componentes vêm das linhas mescladas (BI público não carrega calculation_components).
    merged_for_components = _apply_visual_filters(list(bundle.get("merged_rows") or []), visual_focus)
    component_signals = _collect_component_signals(merged_for_components)

    diagnostic_groups: list[dict] = []
    diagnostics = bundle.get("audit_diagnostics")
    if isinstance(diagnostics, dict) and isinstance(diagnostics.get("groups"), list):
        for group in diagnostics.get("groups")[:ANALYTICAL_EVIDENCE_TOP_N]:
            if isinstance(group, dict):
                diagnostic_groups.append(
                    {
                        "label": _diagnostic_group_label(group),
                        "title": group.get("title"),
                        "code": group.get("code") or group.get("group_code"),
                        "count": _diagnostic_group_count(group),
                        "affected_rows": group.get("affected_rows"),
                        "failure_stage": group.get("failure_stage"),
                        "message": group.get("message"),
                    }
                )

    tax_summary = bundle.get("tax_summary") if isinstance(bundle.get("tax_summary"), dict) else None
    uncalculated = build_uncalculated_views(bundle, visual_focus=visual_focus)
    coverage_summary = bundle.get("coverage_summary") if isinstance(bundle.get("coverage_summary"), dict) else None
    methodology = {
        "source": "audit_bi" if from_bi else "derived_backend",
        "note": (
            "Valores refletem o lote processado e o BI executivo da sessão; "
            "o chat não recalcula frete nem confirma cobrança indevida definitiva."
        ),
        "divergence_basis": "cobrado - esperado (quando ambos disponíveis)",
    }

    return {
        "source_file_name": bundle.get("source_file_name"),
        "lines_analyzed": total_rows,
        "lines_with_financial_basis": financial_rows,
        "lines_without_financial_basis": max(0, total_rows - financial_rows),
        "audit_confidence": {
            "ratio_pct": round(float(metrics.get("confidence_ratio") or 0.0), 1),
            "label": metrics.get("confidence_label") or "Indisponível",
        },
        "overcharged_total": overcharged,
        "undercharged_total": undercharged,
        "absolute_impact": absolute,
        "net_impact": net_impact,
        "direction_share_pct": {
            "overcharged": _share_pct(overcharged, absolute),
            "undercharged": _share_pct(undercharged, absolute),
        },
        "top_documents_by_absolute_impact": [_compact_row_evidence(row) for row in top_abs],
        "top_overcharges": [_compact_row_evidence(row) for row in top_over],
        "top_undercharges": [_compact_row_evidence(row) for row in top_under],
        "carrier_concentration": carriers,
        "uf_concentration": ufs,
        "pareto_overcharge_by_carrier": pareto,
        "temporal_evolution": temporal,
        "component_signals": component_signals,
        "taxes": tax_summary,
        "persisted_diagnostics": diagnostic_groups,
        "uncalculated": {
            "total_rows": uncalculated.get("total_rows"),
            "cities": (uncalculated.get("cities") or [])[:ANALYTICAL_EVIDENCE_TOP_N],
            "reasons": (uncalculated.get("reasons") or [])[:ANALYTICAL_EVIDENCE_TOP_N],
            "documents": (uncalculated.get("documents") or [])[:ANALYTICAL_EVIDENCE_TOP_N],
        },
        "coverage_summary": coverage_summary,
        "methodology": methodology,
        "stale_alert": {
            "needs_reprocess": bool(bundle.get("needs_reprocess")),
            "stale_reason": bundle.get("stale_reason"),
        },
        "visual_focus": visual_focus or {},
        "evidence_row_count": len(top_abs) + len(top_over) + len(top_under),
        "full_row_count_excluded": total_rows,
    }


def _confidence_limitation_note(package: dict) -> str:
    label = str((package.get("audit_confidence") or {}).get("label") or "").strip().lower()
    ratio = (package.get("audit_confidence") or {}).get("ratio_pct")
    if label in {"média", "media", "baixa"}:
        return (
            f"**Limitação de confiança:** a base desta auditoria está com confiança **{label}** "
            f"({ratio}%). Priorize validação documental antes de decisões definitivas.\n"
        )
    return ""


def _format_top_docs_lines(items: list[dict], *, limit: int = 3) -> list[str]:
    lines: list[str] = []
    for index, item in enumerate(items[:limit], start=1):
        lines.append(
            f"{index}. Linha {item.get('row_index')} · Doc. {item.get('document_number') or 's/n'} · "
            f"{item.get('carrier') or 'transportadora n/d'} · Destino {item.get('destination_uf') or 'n/d'} · "
            f"Divergência {format_brl(item.get('divergence_value'))}"
        )
    return lines


def format_executive_summary(bundle: dict, package: dict | None = None, *, visual_focus: dict | None = None) -> str:
    pkg = package or build_analytical_package(bundle, visual_focus=visual_focus)
    conf = pkg.get("audit_confidence") or {}
    carriers = pkg.get("carrier_concentration") or []
    ufs = pkg.get("uf_concentration") or []
    top_docs = pkg.get("top_documents_by_absolute_impact") or []
    components = (pkg.get("component_signals") or {}).get("predominant_components") or []
    diagnostics = pkg.get("persisted_diagnostics") or []

    lines = [
        stale_warning(bundle),
        "**Resumo executivo do lote auditado**\n",
        _confidence_limitation_note(pkg),
        "**Fatos calculados**",
        f"- Arquivo: {pkg.get('source_file_name') or 'não informado'}",
        f"- Linhas analisadas: {pkg.get('lines_analyzed')}",
        f"- Com base financeira: {pkg.get('lines_with_financial_basis')} · "
        f"Sem base financeira: {pkg.get('lines_without_financial_basis')}",
        f"- Confiança da auditoria: {conf.get('label')} ({conf.get('ratio_pct')}%)",
        f"- Cobrado a mais: {format_brl(pkg.get('overcharged_total'))} "
        f"({(pkg.get('direction_share_pct') or {}).get('overcharged')}% do impacto absoluto)",
        f"- Cobrado a menor: {format_brl(pkg.get('undercharged_total'))} "
        f"({(pkg.get('direction_share_pct') or {}).get('undercharged')}% do impacto absoluto)",
        f"- Impacto absoluto: {format_brl(pkg.get('absolute_impact'))}",
        f"- Impacto líquido (a mais − a menor): {format_brl(pkg.get('net_impact'))}",
        "\n**Leitura gerencial**",
    ]

    if carriers:
        top = carriers[0]
        share = _share_pct(float(top.get("impacto_total") or 0), float(pkg.get("absolute_impact") or 0))
        lines.append(
            f"- Concentração por transportadora: **{top.get('chave')}** concentra cerca de "
            f"{share}% do impacto absoluto ({format_brl(top.get('impacto_total'))})."
        )
    else:
        lines.append("- Concentração por transportadora: sem base agregável neste recorte.")

    if ufs:
        top_uf = ufs[0]
        lines.append(
            f"- Concentração por UF destino: **{top_uf.get('chave')}** com impacto "
            f"{format_brl(top_uf.get('impacto_total'))} em {top_uf.get('quantidade')} linha(s)."
        )

    direction = "predominância de cobranças acima do esperado"
    if float(pkg.get("undercharged_total") or 0) > float(pkg.get("overcharged_total") or 0):
        direction = "predominância de cobranças abaixo do esperado"
    lines.append(f"- Direção do impacto: {direction}.")

    lines.append("\n**Documentos prioritários para revisão**")
    lines.extend(_format_top_docs_lines(top_docs) or ["- Nenhum documento com divergência calculável no recorte."])

    lines.append("\n**Padrões / hipóteses (não conclusivas)**")
    if components:
        names = ", ".join(str(item.get("component")) for item in components[:4])
        lines.append(f"- Componentes mais presentes na memória persistida: {names}.")
    if diagnostics:
        labels = ", ".join(str(item.get("label")) for item in diagnostics[:3] if item.get("label"))
        if labels:
            lines.append(f"- Diagnósticos persistidos: {labels}.")
    if not components and not diagnostics:
        lines.append("- Há indícios de divergência financeira; a causa específica merece validação documental.")
    else:
        lines.append(
            "- Esses padrões podem estar relacionados às divergências observadas; recomendo validar tabela, "
            "faixa, pedágio, GRIS, impostos e evidências operacionais."
        )

    lines.extend(
        [
            "\n**Riscos**",
            "- Decisões baseadas apenas no agregado, sem revisar documentos prioritários.",
            "- Base com confiança média/baixa pode distorcer priorização (quando aplicável).",
            "\n**Próximos passos sugeridos**",
            "1. Revisar os documentos de maior impacto absoluto.",
            "2. Solicitar memória de cálculo às transportadoras concentradoras.",
            "3. Confrontar componentes persistidos com CT-e/NF e tabela contratada.",
        ]
    )
    return "\n".join(line for line in lines if line is not None)


def format_management_email_draft(
    bundle: dict,
    package: dict | None = None,
    *,
    visual_focus: dict | None = None,
    user_message: str | None = None,
) -> str:
    pkg = package or build_analytical_package(bundle, visual_focus=visual_focus)
    conf = pkg.get("audit_confidence") or {}
    carriers = pkg.get("carrier_concentration") or []
    top_docs = pkg.get("top_documents_by_absolute_impact") or []
    top_carrier = carriers[0]["chave"] if carriers else "transportadoras do lote"
    normalized, norm_meta = normalize_insights_message(user_message or "")

    subject = (
        f"Auditoria de frete — impacto {format_brl(pkg.get('absolute_impact'))} "
        f"em {pkg.get('source_file_name') or 'lote processado'}"
    )
    body_lines = [
        "Prezados,",
        "",
        "Segue sugestão de comunicação com base na auditoria de frete já processada nesta sessão.",
        "",
        "Resumo dos achados:",
        f"- Confiança da base: {conf.get('label')} ({conf.get('ratio_pct')}%).",
        f"- Impacto absoluto: {format_brl(pkg.get('absolute_impact'))}.",
        f"- Cobrado a mais: {format_brl(pkg.get('overcharged_total'))}.",
        f"- Cobrado a menor: {format_brl(pkg.get('undercharged_total'))}.",
        f"- Impacto líquido: {format_brl(pkg.get('net_impact'))}.",
        f"- Concentração relevante em {top_carrier}.",
        "",
        "Documentos prioritários para revisão:",
    ]
    body_lines.extend(_format_top_docs_lines(top_docs) or ["- Sem documentos divergentes calculáveis no recorte."])
    body_lines.extend(
        [
            "",
            "Riscos / cuidados:",
            "- Os valores indicam divergências calculadas e merecem validação; não constituem conclusão de cobrança indevida.",
            "- Recomendo revisar evidências e regras aplicáveis antes de qualquer providência.",
            "",
            "Próximos passos sugeridos:",
            "1. Validar os documentos de maior impacto absoluto.",
            "2. Pedir memória de cálculo às transportadoras com maior concentração.",
            "3. Retornar com status após a revisão interna.",
            "",
            "Atenciosamente,",
            "[Seu nome]",
        ]
    )
    if norm_meta.get("had_typos"):
        intro = (
            "Claro. Interpretei seu pedido como uma minuta de e-mail executivo baseada na auditoria. "
            "Segue uma sugestão para você revisar e enviar:\n\n"
        )
    elif _user_will_send(normalized) or "equipe" in normalized:
        intro = (
            "Claro. Segue uma sugestão de minuta executiva para você revisar e enviar à sua equipe:\n\n"
        )
    else:
        intro = "Claro. Segue uma sugestão de minuta executiva baseada no lote auditado:\n\n"

    return (
        stale_warning(bundle)
        + intro
        + f"**Assunto:** {subject}\n\n"
        + "\n".join(body_lines)
    )


def format_action_plan(bundle: dict, package: dict | None = None, *, visual_focus: dict | None = None) -> str:
    pkg = package or build_analytical_package(bundle, visual_focus=visual_focus)
    top_docs = pkg.get("top_documents_by_absolute_impact") or []
    carriers = pkg.get("carrier_concentration") or []
    lines = [
        stale_warning(bundle),
        "**Plano de ação sugerido (priorizado por impacto absoluto)**\n",
        _confidence_limitation_note(pkg),
        "**Fatos calculados**",
        f"- Impacto absoluto: {format_brl(pkg.get('absolute_impact'))}",
        f"- Cobrado a mais / a menor: {format_brl(pkg.get('overcharged_total'))} / {format_brl(pkg.get('undercharged_total'))}",
        "\n**Prioridade 1 — documentos de maior impacto**",
    ]
    lines.extend(_format_top_docs_lines(top_docs) or ["- Sem documentos divergentes no recorte."])
    lines.append("\n**Prioridade 2 — concentração por transportadora**")
    if carriers:
        for item in carriers[:3]:
            lines.append(
                f"- {item.get('chave')}: impacto {format_brl(item.get('impacto_total'))} · "
                f"{item.get('quantidade')} linha(s)"
            )
    else:
        lines.append("- Sem concentração agregável.")
    lines.extend(
        [
            "\n**Prioridade 3 — validações recomendadas**",
            "1. Confrontar cobrado vs esperado nos documentos prioritários.",
            "2. Solicitar memória de cálculo e evidências de faixa/taxa/pedágio/imposto.",
            "3. Reavaliar após validação — sem tomar decisão final só com o agregado.",
            "\n**Leitura gerencial:** atacar primeiro o que concentra maior impacto financeiro absoluto, "
            "depois padrões recorrentes por transportadora/UF, considerando a confiança da base.",
        ]
    )
    return "\n".join(line for line in lines if line)


def format_root_cause_hypotheses(bundle: dict, package: dict | None = None, *, visual_focus: dict | None = None) -> str:
    pkg = package or build_analytical_package(bundle, visual_focus=visual_focus)
    signals = pkg.get("component_signals") or {}
    diagnostics = pkg.get("persisted_diagnostics") or []
    components = signals.get("predominant_components") or []
    lines = [
        stale_warning(bundle),
        "**Hipóteses mais prováveis (com base apenas em evidências persistidas)**\n",
        "As hipóteses abaixo são apenas pistas para validação — não confirmam causa definitiva.\n",
        "**Evidências presentes no lote:**",
    ]
    if components:
        for item in components[:5]:
            lines.append(f"- Componente persistido `{item.get('component')}` em {item.get('rows')} linha(s).")
    if signals.get("rows_with_toll"):
        lines.append(f"- Sinal de pedágio/rota em {signals.get('rows_with_toll')} linha(s).")
    if signals.get("rows_with_gris_signal"):
        lines.append(f"- Sinal de GRIS/seguro em {signals.get('rows_with_gris_signal')} linha(s).")
    if signals.get("rows_with_excess_signal"):
        lines.append(f"- Sinal de excedente de peso em {signals.get('rows_with_excess_signal')} linha(s).")
    if signals.get("rows_with_taxes"):
        lines.append(f"- Sinal tributário em {signals.get('rows_with_taxes')} linha(s).")
    if signals.get("ignored_fee_mentions"):
        lines.append(f"- Taxas ignoradas registradas: {signals.get('ignored_fee_mentions')} menção(ões).")
    if diagnostics:
        lines.append("\n**Diagnósticos persistidos:**")
        for item in diagnostics[:5]:
            label = item.get("label") or "diagnóstico"
            count = item.get("count")
            lines.append(f"- {label}" + (f" ({count} ocorrência(s))" if count is not None else ""))
    if not components and not diagnostics and not any(
        signals.get(key)
        for key in (
            "rows_with_toll",
            "rows_with_gris_signal",
            "rows_with_excess_signal",
            "rows_with_taxes",
            "ignored_fee_mentions",
        )
    ):
        lines.append("- Há divergência financeira agregada, porém sem componentes/diagnósticos detalhados suficientes.")
        lines.append(
            "\n**Hipótese prudente:** diferença de tabela, faixa, taxa ou mapeamento — merece validação documental."
        )
    else:
        lines.append("\n**Hipóteses (para validação):**")
        if any(str(item.get("component") or "").startswith("weight") for item in components) or signals.get(
            "rows_with_excess_signal"
        ):
            lines.append("- Pode estar relacionado a faixa de peso/excedente aplicado de forma distinta na cobrança.")
        if signals.get("rows_with_toll"):
            lines.append("- Pode estar relacionado a pedágio/rota não alinhado entre cobrança e esperado.")
        if signals.get("rows_with_gris_signal"):
            lines.append("- Pode estar relacionado a GRIS/seguro ou mínimo de taxa acessória.")
        if signals.get("rows_with_taxes"):
            lines.append("- Pode estar relacionado a base tributária (ICMS/ISS) diferente da aplicada.")
        if diagnostics:
            lines.append("- Os diagnósticos persistidos merecem revisão pontual nos documentos associados.")
        if signals.get("ignored_fee_mentions"):
            lines.append("- Taxas ignoradas no cálculo podem explicar parte da diferença — valide o motivo registrado.")
    lines.append(
        "\nRecomendo confrontar CT-e/NF, tabela contratada e memória de cálculo antes de qualquer conclusão."
    )
    return "\n".join(lines)


def format_prioritization(bundle: dict, package: dict | None = None, *, visual_focus: dict | None = None) -> str:
    pkg = package or build_analytical_package(bundle, visual_focus=visual_focus)
    top_docs = pkg.get("top_documents_by_absolute_impact") or []
    carriers = pkg.get("carrier_concentration") or []
    lines = [
        stale_warning(bundle),
        "**Priorização — o que atacar primeiro**\n",
        _confidence_limitation_note(pkg),
        "Critérios: impacto financeiro absoluto, concentração, recorrência e confiança da base.\n",
        "**Documentos com revisão mais urgente (impacto absoluto):**",
    ]
    lines.extend(_format_top_docs_lines(top_docs, limit=5) or ["- Nenhum documento divergente calculável."])
    if carriers:
        lines.append("\n**Concentração que amplifica prioridade:**")
        for item in carriers[:3]:
            lines.append(
                f"- {item.get('chave')}: {format_brl(item.get('impacto_total'))} · "
                f"{item.get('linhas_divergentes', item.get('quantidade'))} linha(s)"
            )
    lines.append(
        "\n**Leitura gerencial:** comece pelos documentos acima; padrões recorrentes na mesma "
        "transportadora/UF tendem a render mais na validação conjunta."
    )
    return "\n".join(line for line in lines if line)


def format_carrier_negotiation_brief(
    bundle: dict, package: dict | None = None, *, visual_focus: dict | None = None
) -> str:
    pkg = package or build_analytical_package(bundle, visual_focus=visual_focus)
    top_docs = pkg.get("top_overcharges") or pkg.get("top_documents_by_absolute_impact") or []
    carriers = pkg.get("carrier_concentration") or []
    top_carrier = carriers[0] if carriers else None
    lines = [
        stale_warning(bundle),
        "**Briefing de negociação com transportadora (sugestão)**\n",
        _confidence_limitation_note(pkg),
        "**Fatos calculados do lote**",
        f"- Impacto absoluto: {format_brl(pkg.get('absolute_impact'))}",
        f"- Cobrado a mais: {format_brl(pkg.get('overcharged_total'))}",
        f"- Cobrado a menor: {format_brl(pkg.get('undercharged_total'))}",
    ]
    if top_carrier:
        lines.append(
            f"- Transportadora com maior concentração de impacto: **{top_carrier.get('chave')}** "
            f"({format_brl(top_carrier.get('impacto_total'))})."
        )
    lines.append("\n**Documentos de maior impacto para levar à conversa**")
    lines.extend(_format_top_docs_lines(top_docs, limit=5) or ["- Sem documentos prioritários no recorte."])
    lines.extend(
        [
            "\n**Pontos a validar na negociação**",
            "- Comparar cobrado versus esperado linha a linha nos documentos prioritários.",
            "- Pedir memória de cálculo (faixa/peso, excedente, pedágio, GRIS, impostos, mínimos e taxas).",
            "- Confirmar regras/faixas/componentes aplicados versus os persistidos na auditoria.",
            "- Solicitar evidências operacionais (CT-e/NF, tabela vigente, aditivos).",
            "\n**Tom recomendado:** há indícios de divergência que merecem esclarecimento; "
            "trate os números como base para validação, não como conclusão final.",
        ]
    )
    return "\n".join(line for line in lines if line)


def format_audit_findings_report(
    bundle: dict, package: dict | None = None, *, visual_focus: dict | None = None
) -> str:
    pkg = package or build_analytical_package(bundle, visual_focus=visual_focus)
    conf = pkg.get("audit_confidence") or {}
    lines = [
        stale_warning(bundle),
        "**Relatório de achados da auditoria**\n",
        _confidence_limitation_note(pkg),
        f"- Escopo: {pkg.get('lines_analyzed')} linhas · confiança {conf.get('label')}",
        f"- Impacto absoluto: {format_brl(pkg.get('absolute_impact'))}",
        f"- A mais / a menor: {format_brl(pkg.get('overcharged_total'))} / {format_brl(pkg.get('undercharged_total'))}",
        "\n**Principais achados**",
    ]
    for item in (pkg.get("top_documents_by_absolute_impact") or [])[:5]:
        lines.append(
            f"- Doc. {item.get('document_number') or 's/n'} (linha {item.get('row_index')}): "
            f"divergência {format_brl(item.get('divergence_value'))} · "
            f"{item.get('carrier') or 'n/d'} / {item.get('destination_uf') or 'n/d'}"
        )
    diagnostics = pkg.get("persisted_diagnostics") or []
    if diagnostics:
        lines.append("\n**Diagnósticos persistidos**")
        for item in diagnostics[:5]:
            lines.append(f"- {item.get('label')}: {item.get('count')} ocorrência(s)")
    lines.append(
        "\nOs achados indicam divergências calculadas que merecem validação; "
        "não constituem conclusão definitiva de irregularidade."
    )
    return "\n".join(line for line in lines if line)


def format_risk_alerts(bundle: dict, package: dict | None = None, *, visual_focus: dict | None = None) -> str:
    pkg = package or build_analytical_package(bundle, visual_focus=visual_focus)
    conf = pkg.get("audit_confidence") or {}
    lines = [
        stale_warning(bundle),
        "**Alertas de risco (leitura prudente)**\n",
        _confidence_limitation_note(pkg),
    ]
    absolute = float(pkg.get("absolute_impact") or 0)
    if absolute > 0:
        lines.append(f"- Há impacto financeiro absoluto de {format_brl(absolute)} concentrado em poucos documentos/transportadoras.")
    if str(conf.get("label") or "").lower() in {"média", "media", "baixa"}:
        lines.append("- Confiança da base limitada: risco de priorizar documentos com cobertura financeira incompleta.")
    stale = pkg.get("stale_alert") or {}
    if stale.get("needs_reprocess") or stale.get("stale_reason"):
        lines.append("- Dados possivelmente desatualizados: risco de decisão com base em lote stale.")
    carriers = pkg.get("carrier_concentration") or []
    if carriers:
        share = _share_pct(float(carriers[0].get("impacto_total") or 0), absolute)
        if share >= 40:
            lines.append(
                f"- Concentração elevada em {carriers[0].get('chave')} (~{share}% do impacto): "
                "vale validação conjunta dos documentos dessa transportadora."
            )
    if not any(line.startswith("-") for line in lines):
        lines.append("- Não há alertas materiais além da prudência usual de validação documental.")
    lines.append(
        "\nEstes alertas são indícios para revisão e não substituem a validação documental."
    )
    return "\n".join(line for line in lines if line)


def format_next_steps(bundle: dict, package: dict | None = None, *, visual_focus: dict | None = None) -> str:
    pkg = package or build_analytical_package(bundle, visual_focus=visual_focus)
    top_docs = pkg.get("top_documents_by_absolute_impact") or []
    lines = [
        stale_warning(bundle),
        "**Próximos passos sugeridos**\n",
        _confidence_limitation_note(pkg),
        "1. Revisar os documentos de maior impacto absoluto:",
    ]
    lines.extend(_format_top_docs_lines(top_docs, limit=3) or ["   - Sem documentos prioritários no recorte."])
    lines.extend(
        [
            "2. Solicitar memória de cálculo às transportadoras com maior concentração de impacto.",
            "3. Validar componentes persistidos (faixa, excedente, pedágio, GRIS, impostos, taxas ignoradas).",
            "4. Registrar conclusões internas após a revisão — a decisão final permanece com você.",
        ]
    )
    return "\n".join(line for line in lines if line)


def format_business_impact_explanation(
    bundle: dict, package: dict | None = None, *, visual_focus: dict | None = None
) -> str:
    pkg = package or build_analytical_package(bundle, visual_focus=visual_focus)
    conf = pkg.get("audit_confidence") or {}
    carriers = pkg.get("carrier_concentration") or []
    lines = [
        stale_warning(bundle),
        "**Impacto financeiro em linguagem simples**\n",
        _confidence_limitation_note(pkg),
        "**Fatos calculados**",
        f"- Neste lote, o impacto absoluto das divergências é {format_brl(pkg.get('absolute_impact'))}.",
        f"- Isso se divide em {format_brl(pkg.get('overcharged_total'))} cobrados a mais e "
        f"{format_brl(pkg.get('undercharged_total'))} cobrados a menor.",
        f"- Em termos líquidos (a mais − a menor): {format_brl(pkg.get('net_impact'))}.",
        f"- A confiança da base está {str(conf.get('label') or 'indisponível').lower()} "
        f"({conf.get('ratio_pct')}% das linhas com cobrado e esperado).",
        "\n**Leitura gerencial**",
        "- Em linguagem simples: quanto maior o impacto absoluto e quanto mais concentrado em poucas "
        "transportadoras/documentos, maior a urgência de revisar esses pontos primeiro.",
    ]
    if carriers:
        lines.append(
            f"- Hoje a maior concentração aparece em **{carriers[0].get('chave')}** "
            f"({format_brl(carriers[0].get('impacto_total'))})."
        )
    lines.append(
        "\nIsso indica diferença entre cobrado e esperado calculado; "
        "não prova, por si só, cobrança indevida ou responsabilidade final."
    )
    return "\n".join(line for line in lines if line)


def format_managerial_fallback(
    bundle: dict,
    intent: str,
    *,
    package: dict | None = None,
    visual_focus: dict | None = None,
    user_message: str | None = None,
) -> str:
    pkg = package or build_analytical_package(bundle, visual_focus=visual_focus)
    if intent == INTENT_MANAGEMENT_EMAIL_DRAFT:
        return format_management_email_draft(
            bundle,
            pkg,
            visual_focus=visual_focus,
            user_message=user_message,
        )
    formatters = {
        INTENT_EXECUTIVE_SUMMARY: format_executive_summary,
        INTENT_ACTION_PLAN: format_action_plan,
        INTENT_ROOT_CAUSE_HYPOTHESES: format_root_cause_hypotheses,
        INTENT_EXPLAIN_CAUSES: format_root_cause_hypotheses,
        INTENT_PRIORITIZATION: format_prioritization,
        INTENT_CARRIER_NEGOTIATION_BRIEF: format_carrier_negotiation_brief,
        INTENT_AUDIT_FINDINGS_REPORT: format_audit_findings_report,
        INTENT_RISK_ALERTS: format_risk_alerts,
        INTENT_NEXT_STEPS: format_next_steps,
        INTENT_EXPLAIN_BUSINESS_IMPACT: format_business_impact_explanation,
    }
    formatter = formatters.get(intent)
    if formatter:
        return formatter(bundle, pkg, visual_focus=visual_focus)
    return format_ambiguity(bundle)


def build_compact_context_for_gemini(
    bundle: dict,
    intent: str,
    *,
    rows: list[dict] | None = None,
    visual_focus: dict | None = None,
) -> dict[str, Any]:
    package = build_analytical_package(bundle, visual_focus=visual_focus)
    context: dict[str, Any] = {
        "intent": intent,
        "analytical_package": package,
        "source_file_name": package.get("source_file_name"),
        "needs_reprocess": (package.get("stale_alert") or {}).get("needs_reprocess"),
        "stale_reason": (package.get("stale_alert") or {}).get("stale_reason"),
        "visual_focus": visual_focus or {},
    }
    if rows:
        # Evidência pontual (documento/linha) — nunca o lote completo.
        context["focused_rows"] = [_compact_row_evidence(row) for row in rows[:ANALYTICAL_EVIDENCE_TOP_N]]
    # Garantia explícita: não embutir merged_rows/results completos.
    context["safety"] = {
        "includes_full_spreadsheet": False,
        "includes_all_rows": False,
        "evidence_only_top_n": ANALYTICAL_EVIDENCE_TOP_N,
    }
    return context


def try_deterministic_response(
    bundle: dict,
    intent: str,
    message: str,
    *,
    visual_focus: dict | None = None,
    conversation_focus: dict | None = None,
) -> tuple[str | None, list[dict] | None, bool]:
    """
    Retorna (resposta, linhas_contexto, totalmente_deterministico).
    Intenções gerenciais/Gemini retornam (None, None, False) para o runner tentar o modelo.
    """
    if intent == INTENT_OUT_OF_SCOPE:
        return format_out_of_scope(bundle), None, True

    if intent == INTENT_SEND_EMAIL_BLOCKED:
        return format_send_email_blocked(bundle), None, True

    if intent == INTENT_AMBIGUOUS:
        if _is_ranking_direction_followup(_normalize_text(message)):
            return (
                stale_warning(bundle)
                + "Pode esclarecer a quantidade e o critério? Por exemplo: "
                "\"liste as 3 maiores divergências a menor\".",
                None,
                True,
            )
        return format_ambiguity(bundle), None, True

    if intent == INTENT_BATCH_SUMMARY:
        return format_batch_summary(bundle, visual_focus=visual_focus), None, True

    if intent == INTENT_CHARGE_VALIDITY:
        return format_charge_validity_response(bundle, message, visual_focus=visual_focus), None, True

    if intent == INTENT_UNCALCULATED_CITIES:
        return format_uncalculated_cities(bundle, visual_focus=visual_focus), None, True

    if intent == INTENT_UNCALCULATED_DOCUMENTS:
        return format_uncalculated_documents(bundle, visual_focus=visual_focus), None, True

    if intent == INTENT_EXPLAIN_UNCALCULATED_REASONS:
        return format_uncalculated_reasons(bundle, visual_focus=visual_focus), None, True

    if intent == INTENT_DOCUMENT_FOLLOWUP:
        row = resolve_focus_row(bundle, conversation_focus)
        if not row:
            return (
                stale_warning(bundle)
                + "Não consegui confirmar o documento em foco neste lote. "
                "Cite o número do documento ou a linha para eu continuar.",
                None,
                True,
            )
        return (
            format_document_followup(bundle, row, message, conversation_focus=conversation_focus),
            [row],
            True,
        )

    if intent in {
        INTENT_TOP_DIVERGENCES,
        INTENT_OVERCHARGED,
        INTENT_UNDERCHARGED,
        INTENT_SMALLEST_DIVERGENCES,
    }:
        if _is_ranking_direction_followup(_normalize_text(message)) and not (
            isinstance(conversation_focus, dict) and conversation_focus.get("last_ranking_limit") is not None
        ):
            return (
                stale_warning(bundle)
                + "Pode esclarecer a quantidade e o critério? Por exemplo: "
                "\"liste as 3 maiores divergências a menor\".",
                None,
                True,
            )
        limit, _explicit = resolve_ranking_limit(message, conversation_focus=conversation_focus)
        ranked = build_ranking(bundle, intent, limit=limit, visual_focus=visual_focus)
        return format_ranking_response(bundle, intent, ranked, limit=limit), ranked, True

    if intent in {INTENT_EXPLAIN_CHART}:
        return format_chart_explanation(bundle, message=message, visual_focus=visual_focus), None, True

    if intent == INTENT_EXPLAIN_CARRIER_UF:
        return format_carrier_uf_explanation(bundle, message=message, visual_focus=visual_focus), None, True

    if intent in {INTENT_EXPLAIN_CALCULATION, INTENT_LOCATE_DOCUMENT}:
        target = resolve_document_target(bundle, message)
        if target["kind"] == "not_found":
            return format_document_not_found(bundle, str(target["reference"])), None, True
        if target["kind"] == "duplicate":
            return format_duplicate_document_options(bundle, target["rows"], str(target["reference"])), target["rows"], True
        if target["kind"] == "single":
            row = target["rows"][0]
            if intent == INTENT_LOCATE_DOCUMENT:
                return format_document_location(bundle, row, str(target["reference"])), [row], True
            return format_calculation_explanation(bundle, row), [row], True

    if intent in GEMINI_ANALYTICAL_INTENTS:
        return None, None, False

    return None, None, False
