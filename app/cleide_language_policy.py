from __future__ import annotations

import unicodedata

# Fonte oficial de verdade de linguagem da Cleide (Fase 9.1).
CLEIDE_ALLOWED_LANGUAGE: tuple[str, ...] = (
    "concentracao operacional",
    "comportamento atipico",
    "variacao relevante",
    "oportunidade de investigacao",
    "dados insuficientes",
    "tendencia operacional",
    "participacao relevante",
)

CLEIDE_FORBIDDEN_LANGUAGE: tuple[str, ...] = (
    "erro de cobranca",
    "cobranca incorreta",
    "transportadora errada",
    "valor incorreto",
    "divergencia contratual",
    "fraude",
    "superfaturamento",
    "responsabilidade financeira",
    "conclusao financeira acusatoria",
)

# Bloqueios adicionais de escopo juridico/acusatorio.
CLEIDE_OUT_OF_SCOPE_LANGUAGE: tuple[str, ...] = (
    "acusacao",
    "culpa",
    "culpado",
    "juridico",
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
