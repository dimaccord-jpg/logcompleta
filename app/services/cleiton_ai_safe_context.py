"""
Minimização e aliases temporários para saída de IA externa (SCRUM-75).

O mapping vive só na memória da operação. Não é persistido, não é logado
e não cruza usuários. A representação segura é uma cópia; o original local
permanece intacto.
"""
from __future__ import annotations

from typing import Any

from app.services.cleiton_ai_privacy_classifier import (
    CATEGORY_API_KEY,
    CATEGORY_BANK,
    CATEGORY_CARD,
    CATEGORY_CNPJ,
    CATEGORY_CPF,
    CATEGORY_EMAIL,
    CATEGORY_FILENAME,
    CATEGORY_FISCAL_KEY,
    CATEGORY_PERSON_NAME,
    CATEGORY_PHONE,
    CATEGORY_SECRET,
    CATEGORY_STRIPE_ID,
    CATEGORY_TECHNICAL_ID,
    CATEGORY_TOKEN,
    DetectedSpan,
)
from app.services.external_ai_masking import (
    MASKABLE_FIELD_KEYS,
    ExternalAiMaskingSession,
)

_CATEGORY_PREFIX = {
    CATEGORY_CPF: "CPF",
    CATEGORY_CNPJ: "EMPRESA",
    CATEGORY_EMAIL: "EMAIL",
    CATEGORY_PHONE: "TEL",
    CATEGORY_FISCAL_KEY: "CHAVE",
    CATEGORY_SECRET: "CREDENCIAL",
    CATEGORY_API_KEY: "CREDENCIAL",
    CATEGORY_TOKEN: "CREDENCIAL",
    CATEGORY_CARD: "CARTAO",
    CATEGORY_BANK: "DADO_BANCARIO",
    CATEGORY_STRIPE_ID: "ID_PAGAMENTO",
    CATEGORY_TECHNICAL_ID: "ID_TECNICO",
    CATEGORY_PERSON_NAME: "Pessoa",
    CATEGORY_FILENAME: "ARQUIVO",
}

_LABEL_STYLE = {
    CATEGORY_CNPJ: "plain",
    CATEGORY_PERSON_NAME: "plain",
}

_FIELD_KEY_CATEGORY = {
    "display_name": CATEGORY_FILENAME,
    "source_file_name": CATEGORY_FILENAME,
    "filename": CATEGORY_FILENAME,
    "email": CATEGORY_EMAIL,
    "customer_email": CATEGORY_EMAIL,
    "phone": CATEGORY_PHONE,
    "telefone": CATEGORY_PHONE,
    "cpf": CATEGORY_CPF,
}


class CleitonAiAliasSession:
    """Aliases estáveis apenas nesta operação. Não persistir."""

    def __init__(self, *, usuario_id: int | str | None = None) -> None:
        self.usuario_id = usuario_id
        self.field_session = ExternalAiMaskingSession()
        self._tokens: dict[tuple[str, str], str] = {}
        self._counts: dict[str, int] = {}
        self.aliases_issued: dict[str, int] = {}

    def alias_for(self, category: str, original: str, *, suffix: str = "") -> str:
        key = (category, original)
        existing = self._tokens.get(key)
        if existing is not None:
            return existing
        prefix = _CATEGORY_PREFIX.get(category, "DADO")
        n = self._counts.get(prefix, 0) + 1
        self._counts[prefix] = n
        if _LABEL_STYLE.get(category) == "plain":
            token = f"{prefix} {n}{suffix}"
        else:
            token = f"[{prefix}_{n}]{suffix}"
        self._tokens[key] = token
        self.aliases_issued[category] = self.aliases_issued.get(category, 0) + 1
        return token

    def alias_for_field(self, key: str, value: str) -> str:
        category = _FIELD_KEY_CATEGORY.get(key)
        if category is None:
            return value
        suffix = _file_suffix(value) if category == CATEGORY_FILENAME else ""
        return self.alias_for(category, value, suffix=suffix)

    def mapping_size(self) -> int:
        return len(self._tokens)


def apply_spans_to_text(text: str, spans: list[DetectedSpan], session: CleitonAiAliasSession) -> str:
    if not spans or not text:
        return text
    out = text
    for span in sorted(spans, key=lambda item: item.start, reverse=True):
        original = text[span.start : span.end]
        if not original:
            continue
        suffix = ""
        if span.category == CATEGORY_FILENAME:
            suffix = _file_suffix(original)
        token = session.alias_for(span.category, original, suffix=suffix)
        out = out[: span.start] + token + out[span.end :]
    return out


def _file_suffix(name: str) -> str:
    if "." not in name:
        return ""
    head, tail = name.rsplit(".", 1)
    if not head or not tail:
        return ""
    if "/" in tail or "\\" in tail:
        return ""
    if len(tail) > 8:
        return ""
    return "." + tail


def copy_and_minimize_structured(
    payload: Any,
    *,
    session: CleitonAiAliasSession,
    text_minimizer,
) -> Any:
    """
    Cópia profunda: chaves estruturadas conhecidas + varredura de strings,
    inclusive em notes/carrier/diagnostic/observações e chaves desconhecidas.

    Caminho estruturado e textual compartilham a mesma sessão de aliases.
    """
    if isinstance(payload, dict):
        out: dict[Any, Any] = {}
        for key, value in payload.items():
            if (
                isinstance(key, str)
                and key in MASKABLE_FIELD_KEYS
                and isinstance(value, str)
                and value.strip()
            ):
                out[key] = session.alias_for_field(key, value)
            else:
                out[key] = copy_and_minimize_structured(
                    value, session=session, text_minimizer=text_minimizer
                )
        return out
    if isinstance(payload, list):
        return [
            copy_and_minimize_structured(item, session=session, text_minimizer=text_minimizer)
            for item in payload
        ]
    if isinstance(payload, tuple):
        return tuple(
            copy_and_minimize_structured(item, session=session, text_minimizer=text_minimizer)
            for item in payload
        )
    if isinstance(payload, str):
        return text_minimizer(payload, session)
    return payload
