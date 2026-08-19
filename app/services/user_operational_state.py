"""
Estado operacional persistente do User.

Módulo neutro, sem side effects e sem persistência.
Não trata billing, e-mail de ativação, LGPD apply nem Flask-Login.

Marcador determinístico atual (dívida técnica: um lifecycle_status/closed_at
explícito seria mais limpo; não implementar neste pacote):

    User.email == encerrado_<id>@anon.local  (normalizado)
"""
from __future__ import annotations

from app.models import User

EMAIL_ANONIMO_ENCERRAMENTO_PREFIXO = "encerrado_"
EMAIL_ANONIMO_ENCERRAMENTO_DOMINIO = "anon.local"


def normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


def email_operacional_apos_encerramento(user_id: int) -> str:
    """E-mail substituto do perfil operacional após encerramento/desidentificação."""
    return (
        f"{EMAIL_ANONIMO_ENCERRAMENTO_PREFIXO}{int(user_id)}"
        f"@{EMAIL_ANONIMO_ENCERRAMENTO_DOMINIO}"
    )


def is_user_operationally_closed(user: User | None) -> bool:
    """
    True somente quando o e-mail operacional é exatamente
    encerrado_<id>@anon.local (normalizado).

    Não usa regex, prefixo genérico, full_name, password_hash,
    OAuth nem newsletter.
    """
    if not isinstance(user, User) or getattr(user, "id", None) is None:
        return False
    expected = email_operacional_apos_encerramento(int(user.id))
    return normalize_email(user.email) == normalize_email(expected)
