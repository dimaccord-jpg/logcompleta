"""
Mascaramento field-aware de estruturas no outbound Gemini.

Opera somente sobre dict/list/scalars já estruturados.
Não busca identificadores em texto livre.
O mapping vive só na memória da operação; não é persistido nem logado.
"""
from __future__ import annotations

from typing import Any

MASKABLE_FIELD_KEYS = frozenset(
    {
        "display_name",
        "source_file_name",
        "filename",
        "email",
        "customer_email",
        "phone",
        "telefone",
        "cpf",
    }
)

_FILE_NAME_KEYS = frozenset({"display_name", "source_file_name", "filename"})
_EMAIL_KEYS = frozenset({"email", "customer_email"})
_PHONE_KEYS = frozenset({"phone", "telefone"})
_CPF_KEYS = frozenset({"cpf"})


class ExternalAiMaskingSession:
    """Mapping in-memory de uma sanitização outbound. Não persistir."""

    def __init__(self) -> None:
        self._file_tokens: dict[str, str] = {}
        self._email_tokens: dict[str, str] = {}
        self._phone_tokens: dict[str, str] = {}
        self._cpf_tokens: dict[str, str] = {}

    def mask_field(self, key: str, value: str) -> str:
        if key in _FILE_NAME_KEYS:
            return self._stable_token(
                self._file_tokens,
                value,
                "ARQUIVO",
                suffix=_known_file_suffix(value),
            )
        if key in _EMAIL_KEYS:
            return self._stable_token(self._email_tokens, value, "EMAIL")
        if key in _PHONE_KEYS:
            return self._stable_token(self._phone_tokens, value, "TEL")
        if key in _CPF_KEYS:
            return self._stable_token(self._cpf_tokens, value, "CPF")
        return value

    @staticmethod
    def _stable_token(
        store: dict[str, str],
        original: str,
        prefix: str,
        *,
        suffix: str = "",
    ) -> str:
        existing = store.get(original)
        if existing is not None:
            return existing
        token = f"[{prefix}_{len(store) + 1}]{suffix}"
        store[original] = token
        return token


def _known_file_suffix(name: str) -> str:
    if "." not in name:
        return ""
    head, tail = name.rsplit(".", 1)
    if not head or not tail:
        return ""
    if "/" in tail or "\\" in tail:
        return ""
    return "." + tail


def mask_structured_for_external_ai(
    payload: Any,
    *,
    session: ExternalAiMaskingSession | None = None,
) -> Any:
    """
    Devolve cópia outbound com valores das chaves allowlist substituídos.

    O payload original permanece intacto. Strings sem chave autorizada
    não são inspecionadas nem alteradas.
    """
    ctx = session or ExternalAiMaskingSession()
    return _copy_and_mask(payload, ctx)


def _copy_and_mask(node: Any, ctx: ExternalAiMaskingSession) -> Any:
    if isinstance(node, dict):
        out: dict[Any, Any] = {}
        for key, value in node.items():
            if (
                isinstance(key, str)
                and key in MASKABLE_FIELD_KEYS
                and isinstance(value, str)
                and value.strip()
            ):
                out[key] = ctx.mask_field(key, value)
            else:
                out[key] = _copy_and_mask(value, ctx)
        return out
    if isinstance(node, list):
        return [_copy_and_mask(item, ctx) for item in node]
    if isinstance(node, tuple):
        return tuple(_copy_and_mask(item, ctx) for item in node)
    return node
