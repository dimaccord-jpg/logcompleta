"""Política de senha do cadastro local.

O formulário público só dizia "mínimo 8 caracteres". O cadastro web e a
conclusão de onboarding por canal usam esta mesma regra. Não persiste senha
e não registra o valor em log.
"""
from __future__ import annotations

SENHA_CADASTRO_MINIMO = 8


def validar_senha_cadastro(password: str) -> str | None:
    """Retorna a mensagem de recusa, ou None quando a senha pode ser gravada."""
    if not isinstance(password, str) or not password.strip():
        return "Informe uma senha."
    if len(password) < SENHA_CADASTRO_MINIMO:
        return f"A senha precisa ter no mínimo {SENHA_CADASTRO_MINIMO} caracteres."
    return None
