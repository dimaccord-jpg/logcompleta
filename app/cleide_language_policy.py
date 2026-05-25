from __future__ import annotations

import unicodedata

# Fonte oficial de verdade de linguagem da Cleide (Fase 9.1).
CLEIDE_ALLOWED_LANGUAGE: tuple[str, ...] = (
    "concentração operacional",
    "comportamento atípico",
    "variação relevante",
    "oportunidade de investigação",
    "dados insuficientes",
    "tendência operacional",
    "participação relevante",
)

CLEIDE_FORBIDDEN_LANGUAGE: tuple[str, ...] = (
    "erro de cobrança",
    "cobrança incorreta",
    "transportadora errada",
    "valor incorreto",
    "divergência contratual",
    "fraude",
    "superfaturamento",
    "responsabilidade financeira",
    "conclusão financeira acusatória",
)

# Bloqueios adicionais de escopo juridico/acusatorio.
CLEIDE_OUT_OF_SCOPE_LANGUAGE: tuple[str, ...] = (
    "acusação",
    "culpa",
    "culpado",
    "jurídico",
    "processo judicial",
)


def normalize_language_token(value: str) -> str:
    raw = str(value or "").strip().lower()
    normalized = unicodedata.normalize("NFD", raw)
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def normalized_allowed_language() -> set[str]:
    return {normalize_language_token(token) for token in CLEIDE_ALLOWED_LANGUAGE}


def normalized_forbidden_language() -> set[str]:
    return {normalize_language_token(token) for token in CLEIDE_FORBIDDEN_LANGUAGE}


def normalized_out_of_scope_language() -> set[str]:
    return {normalize_language_token(token) for token in CLEIDE_OUT_OF_SCOPE_LANGUAGE}
