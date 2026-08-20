"""
Elegibilidade de destinatários para notificações jurídicas
(Termo de Uso e Política de Privacidade).

Comunicação operacional/legal: não consulta newsletter nem
CommunicationSuppression de marketing.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.models import User
from app.services.user_operational_state import (
    EMAIL_ANONIMO_ENCERRAMENTO_DOMINIO,
    EMAIL_ANONIMO_ENCERRAMENTO_PREFIXO,
    is_user_operationally_closed,
    normalize_email,
)

REASON_ELIGIBLE = "eligible"
REASON_CLOSED = "closed"
REASON_INVALID_EMAIL = "invalid_email"


@dataclass(frozen=True)
class LegalNotificationEligibility:
    eligible: bool
    reason: str
    email: str


def is_technical_closure_placeholder_email(email: str | None) -> bool:
    """
    True se o endereço é o placeholder técnico encerrado_<id>@anon.local.

    Independente do User.id: impede envio mesmo quando o helper operacional
    não classificar o usuário como encerrado (ex.: id divergente).
    """
    normalized = normalize_email(email)
    domain = f"@{EMAIL_ANONIMO_ENCERRAMENTO_DOMINIO}"
    prefix = EMAIL_ANONIMO_ENCERRAMENTO_PREFIXO
    if not normalized.startswith(prefix) or not normalized.endswith(domain):
        return False
    local_suffix = normalized[len(prefix) : -len(domain)]
    return bool(local_suffix) and local_suffix.isdigit()


def _operational_email(raw_email) -> str | None:
    if raw_email is None:
        return None
    if not isinstance(raw_email, str):
        return None
    stripped = raw_email.strip()
    if not stripped:
        return None
    return stripped


def classify_legal_notification_recipient(
    user: User | None,
) -> LegalNotificationEligibility:
    """
    Decide se um User pode receber notificação jurídica.

    Rejeita encerrado/desidentificado, placeholder técnico
    encerrado_<id>@anon.local, e e-mail ausente/vazio.
    Não consulta newsletter nem CommunicationSuppression.
    """
    if not isinstance(user, User):
        return LegalNotificationEligibility(
            eligible=False,
            reason=REASON_INVALID_EMAIL,
            email="",
        )

    raw_email = getattr(user, "email", None)
    if is_user_operationally_closed(user) or is_technical_closure_placeholder_email(
        raw_email if isinstance(raw_email, str) else None
    ):
        return LegalNotificationEligibility(
            eligible=False,
            reason=REASON_CLOSED,
            email="",
        )

    email = _operational_email(raw_email)
    if email is None:
        return LegalNotificationEligibility(
            eligible=False,
            reason=REASON_INVALID_EMAIL,
            email="",
        )

    return LegalNotificationEligibility(
        eligible=True,
        reason=REASON_ELIGIBLE,
        email=email,
    )


def can_receive_legal_notification(user: User | None) -> bool:
    return classify_legal_notification_recipient(user).eligible
