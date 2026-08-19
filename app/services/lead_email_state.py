"""
Estado de minimização do plaintext Lead.email.

Marcador determinístico (exato, sem regex/prefixo genérico):

    lead_minimized_<lead.id>@anon.invalid

Não implica opt-out, CommunicationSuppression, consentimento nem recusa.
Não usa email_hmac, converted_user_id nem timestamps para decidir o estado.
"""
from __future__ import annotations

LEAD_MINIMIZED_EMAIL_PREFIX = "lead_minimized_"
LEAD_MINIMIZED_EMAIL_DOMAIN = "anon.invalid"


class LeadEmailIdentityError(RuntimeError):
    """Lead minimizado sem identidade HMAC utilizável (fail-closed)."""


def normalize_lead_email(email: str | None) -> str:
    return (email or "").strip().lower()


def lead_minimized_email(lead_id: int) -> str:
    """Placeholder não roteável, único por Lead.id, independente do e-mail original."""
    return (
        f"{LEAD_MINIMIZED_EMAIL_PREFIX}{int(lead_id)}"
        f"@{LEAD_MINIMIZED_EMAIL_DOMAIN}"
    )


def is_lead_email_minimized(lead) -> bool:
    """True somente quando o e-mail é exatamente o placeholder deste Lead.id."""
    if lead is None or getattr(lead, "id", None) is None:
        return False
    expected = lead_minimized_email(int(lead.id))
    return normalize_lead_email(getattr(lead, "email", None)) == normalize_lead_email(
        expected
    )
