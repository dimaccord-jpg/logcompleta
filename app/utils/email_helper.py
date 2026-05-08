"""
Helper de e-mail para notificações administrativas.
Delega o envio real para auth_services para não duplicar configuração (Resend, etc.).
Use este módulo quando precisar enviar e-mails a partir de serviços do admin.
"""
from app.auth_services import (
    send_email,
    send_terms_updated_notification,
    send_privacy_policy_updated_notification,
)

__all__ = [
    "send_email",
    "send_terms_updated_notification",
    "send_privacy_policy_updated_notification",
]
