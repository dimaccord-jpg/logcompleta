"""
Controle de privilégios admin por convite/revogação com confirmação por link seguro.
"""
from __future__ import annotations

from itsdangerous import BadSignature, SignatureExpired
from sqlalchemy import func

from app.auth_services import (
    _get_serializer,
    send_email,
)
from app.extensions import db
from app.models import User

ADMIN_CONTROL_TOKEN_MAX_AGE = 3600  # 1 hora
ADMIN_ACTION_PROMOTE = "promote_admin"
ADMIN_ACTION_REVOKE = "revoke_admin"
REVOGACAO_DESTINO_EMAIL = "diogo@agentefrete.com.br"


def normalizar_email(email: str | None) -> str:
    return (email or "").strip().lower()


def buscar_usuario_por_email(email: str | None) -> User | None:
    email_n = normalizar_email(email)
    if not email_n:
        return None
    return (
        User.query.filter(func.lower(User.email) == email_n)
        .order_by(User.id.asc())
        .first()
    )


def total_admins_ativos() -> int:
    return int(User.query.filter(User.is_admin.is_(True)).count())


def gerar_token_admin_action(
    *,
    secret_key: str,
    action: str,
    target_user_id: int,
    requested_by_user_id: int | None = None,
) -> str:
    serializer = _get_serializer(secret_key)
    payload = {
        "action": action,
        "target_user_id": int(target_user_id),
        "requested_by_user_id": (
            int(requested_by_user_id) if requested_by_user_id else None
        ),
    }
    return serializer.dumps(payload)


def validar_token_admin_action(
    *,
    secret_key: str,
    token: str,
    expected_action: str,
) -> tuple[dict | None, str | None]:
    serializer = _get_serializer(secret_key)
    try:
        data = serializer.loads(token, max_age=ADMIN_CONTROL_TOKEN_MAX_AGE)
    except SignatureExpired:
        return None, "O link de confirmação expirou."
    except BadSignature:
        return None, "Link de confirmação inválido."
    if data.get("action") != expected_action:
        return None, "Ação do link de confirmação é inválida."
    if not data.get("target_user_id"):
        return None, "Link de confirmação incompleto."
    return data, None


def enviar_email_convite_admin(
    *,
    target_user: User,
    confirm_url: str,
) -> None:
    subject = "Convite para administrador - Agente Frete"
    text = f"""Olá {target_user.full_name or target_user.email},

Você foi convidado para se tornar administrador do site Agente Frete.

Para confirmar, acesse o link (válido por 1 hora):
{confirm_url}

Se você não reconhece esta ação, ignore este e-mail.
"""
    html = f"""
<p>Olá {target_user.full_name or target_user.email},</p>
<p>Você foi convidado para se tornar administrador do site Agente Frete.</p>
<p>Para confirmar, acesse o link (válido por 1 hora):<br>
<a href="{confirm_url}">{confirm_url}</a></p>
<p>Se você não reconhece esta ação, ignore este e-mail.</p>
""".strip()
    send_email(
        to_email=target_user.email,
        subject=subject,
        html=html,
        text=text,
    )


def enviar_email_revogacao_admin(
    *,
    target_user: User,
    confirm_url: str,
) -> None:
    subject = "Confirmação de revogação de administrador - Agente Frete"
    text = f"""Foi solicitada a revogação do privilégio de administrador do usuário:
{target_user.email}

Para confirmar esta revogação, acesse o link (válido por 1 hora):
{confirm_url}

Se você não reconhece esta ação, ignore este e-mail.
"""
    html = f"""
<p>Foi solicitada a revogação do privilégio de administrador do usuário:</p>
<p><strong>{target_user.email}</strong></p>
<p>Para confirmar esta revogação, acesse o link (válido por 1 hora):<br>
<a href="{confirm_url}">{confirm_url}</a></p>
<p>Se você não reconhece esta ação, ignore este e-mail.</p>
""".strip()
    send_email(
        to_email=REVOGACAO_DESTINO_EMAIL,
        subject=subject,
        html=html,
        text=text,
    )


def aplicar_promocao_admin(target_user_id: int) -> tuple[bool, str]:
    user = db.session.get(User, int(target_user_id))
    if user is None:
        return False, "Usuário não encontrado."
    if user.is_admin:
        return False, "Este usuário já é administrador."
    user.is_admin = True
    db.session.add(user)
    db.session.commit()
    return True, f"Privilégio de administrador concedido para {user.email}."


def aplicar_revogacao_admin(target_user_id: int) -> tuple[bool, str]:
    user = db.session.get(User, int(target_user_id))
    if user is None:
        return False, "Usuário não encontrado."
    if not user.is_admin:
        return False, "Este usuário já não é administrador."
    if total_admins_ativos() <= 1:
        return False, "Não é permitido revogar o último administrador ativo."
    user.is_admin = False
    db.session.add(user)
    db.session.commit()
    return True, f"Privilégio de administrador revogado de {user.email}."
