"""
Normalização e sanitização leve de mensagens do onboarding discovery.
Sem dependências externas pesadas; apenas stdlib (re, unicodedata).
"""
from __future__ import annotations

import re
import unicodedata

SANITIZED_MESSAGE_MAX_LENGTH = 200

_STOPWORDS = frozenset({
    # stopwords originais
    "de", "da", "do", "das", "dos", "para", "com", "que", "quero", "preciso",
    "minha", "meu", "meus", "minhas", "uma", "um", "por", "sobre", "como",
    "posso", "voce", "no", "na", "em", "e", "o", "a", "os", "as",
    # conversacionais / funcionais comuns (sem valor gerencial na nuvem)
    "ola", "oi", "bom", "boa", "dia", "tarde", "noite",
    "tem", "tenho", "temos", "quais", "qual", "sao", "voces",
    "outros", "outro", "base", "ver", "eu", "gostaria", "queria",
    "pode", "poderia", "tambem", "muito", "mais", "menos",
    "seus", "suas", "esse", "essa", "isso", "aquilo",
})

WORD_CLOUD_TERM_MAX_LENGTH = 64

_USEFUL_TERMS = frozenset({
    "frete", "custo", "cotacao", "previsao", "auditoria", "bi", "transportadora",
    "estoque", "dolar", "euro", "cambio", "cambial", "inflacao", "juros", "selic",
    "combustivel", "petroleo", "importacao", "exportacao", "internacional",
    "prazo", "indicador", "planejamento", "logistica",
})

_RE_EMAIL = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
    re.IGNORECASE,
)
_RE_CPF = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
_RE_PHONE = re.compile(
    r"(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{2}\)?[\s.-]?)?\d{4,5}[\s.-]?\d{4}\b",
)


def _strip_sensitive_patterns(text: str) -> str:
    cleaned = _RE_EMAIL.sub("[email]", text)
    cleaned = _RE_CPF.sub("[cpf]", cleaned)
    cleaned = _RE_PHONE.sub("[telefone]", cleaned)
    return cleaned


def _normalize_text(value: str) -> str:
    base = (value or "").strip().lower()
    normalized = unicodedata.normalize("NFD", base)
    without_accents = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    without_punct = re.sub(r"[^\w\s]", " ", without_accents, flags=re.UNICODE)
    return re.sub(r"\s+", " ", without_punct).strip()


def sanitize_user_message(message: str, *, max_length: int = SANITIZED_MESSAGE_MAX_LENGTH) -> str:
    """Remove padrões sensíveis simples e trunca; nunca persiste mensagem bruta."""
    text = _strip_sensitive_patterns((message or "").strip())
    text = re.sub(r"\s+", " ", text).strip()
    if max_length > 0 and len(text) > max_length:
        return text[:max_length]
    return text


def is_onboarding_stopword(term: str) -> bool:
    """Indica se o termo normalizado deve ser ignorado na nuvem do onboarding."""
    token = (term or "").strip().lower()
    return bool(token) and token in _STOPWORDS


def get_onboarding_stopwords() -> frozenset[str]:
    """Conjunto imutável de stopwords do onboarding (somente leitura)."""
    return _STOPWORDS


def normalize_word_cloud_term(value: str) -> str:
    """
    Normaliza um termo único para uso na nuvem / ocultação admin.
    Retorna string vazia se inválido (vazio, só dígitos, curto demais ou longo demais).
    """
    normalized = _normalize_text(value or "")
    if not normalized:
        return ""
    token = normalized.split()[0]
    if not token or token.isdigit() or len(token) < 2:
        return ""
    if len(token) > WORD_CLOUD_TERM_MAX_LENGTH:
        return token[:WORD_CLOUD_TERM_MAX_LENGTH]
    return token


def extract_user_terms_normalized(message: str) -> list[str]:
    """Extrai termos normalizados da mensagem do usuário (sem respostas da IA)."""
    text = _normalize_text(_strip_sensitive_patterns((message or "").strip()))
    if not text:
        return []

    terms: list[str] = []
    seen: set[str] = set()
    for token in text.split():
        if token in _USEFUL_TERMS:
            if token not in seen:
                seen.add(token)
                terms.append(token)
            continue
        if is_onboarding_stopword(token) or token.isdigit() or len(token) < 2:
            continue
        if token not in seen:
            seen.add(token)
            terms.append(token)
    return terms
