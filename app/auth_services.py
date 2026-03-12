"""
Serviços de autenticação: login, recuperação de senha, OAuth Google, registro e perfil.
Toda a lógica de negócio de auth fica aqui; web.py apenas chama e redireciona.
"""
import os
import secrets
import logging
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from sqlalchemy import func

from app.extensions import db
from app.models import User

logger = logging.getLogger(__name__)


def send_email(
    to_email: str,
    subject: str,
    html: str,
    text: str | None = None,
) -> None:
    """
    Envia e-mail usando a API do Resend.
    Centraliza o envio para facilitar manutenção e troca de provider.
    """
    api_key = os.getenv("RESEND_API_KEY")
    if not api_key:
        raise RuntimeError("RESEND_API_KEY não configurada")

    mail_from = (
        os.getenv("MAIL_FROM")
        or os.getenv("MAIL_DEFAULT_SENDER")
        or "noreply@agentefrete.com.br"
    )

    payload: dict = {
        "from": f"Agentefrete <{mail_from}>",
        "to": [to_email],
        "subject": subject,
        "html": html,
    }
    if text:
        payload["text"] = text

    try:
        response = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
    except requests.RequestException as e:
        logger.exception("Erro de rede ao enviar e-mail via Resend: %s", e)
        raise RuntimeError("Falha de rede ao enviar e-mail de recuperação de senha.") from e

    if response.status_code >= 400:
        logger.error(
            "Erro ao enviar e-mail via Resend: status=%s, body=%s",
            response.status_code,
            response.text,
        )
        raise RuntimeError("Erro ao enviar e-mail de recuperação de senha.")

    logger.info(
        "E-mail de recuperação enviado via Resend para %s com assunto '%s'.",
        to_email,
        subject,
    )


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

# --- Constantes (e-mail de recuperação de senha) ---
PASSWORD_RESET_EMAIL_SUBJECT = "Redefinição de senha - Agentefrete"
PASSWORD_RESET_EMAIL_SALT = "password-reset-salt"
PASSWORD_RESET_TOKEN_MAX_AGE = 3600  # 1 hora


def _get_serializer(secret_key: str):
    return URLSafeTimedSerializer(secret_key, salt=PASSWORD_RESET_EMAIL_SALT)


def _normalize_email(email: str) -> str:
    """Normaliza e-mail para comparacoes consistentes no banco."""
    return (email or "").strip().lower()


def _is_profile_complete(user: User) -> bool:
    """Perfil completo exige os dois campos obrigatorios preenchidos."""
    return bool((user.job_role or "").strip()) and bool((user.usage_purpose or "").strip())


def _select_canonical_user(candidates: list[User], google_id: str | None) -> User:
    """
    Escolhe um usuario canonico entre candidatos com mesmo e-mail normalizado.
    Prioriza vinculo Google estavel e perfil ja completo para evitar regressao de onboarding.
    """
    if len(candidates) == 1:
        return candidates[0]

    # Se houver match exato por sub do Google, ele sempre vence.
    if google_id:
        for candidate in candidates:
            if candidate.oauth_provider == "google" and (candidate.oauth_sub or "").strip() == google_id:
                return candidate

    def _score(user: User):
        return (
            1 if _is_profile_complete(user) else 0,
            1 if (user.oauth_provider == "google" and (user.oauth_sub or "").strip()) else 0,
            1 if user.last_login_at else 0,
            -(user.id or 0),
        )

    return max(candidates, key=_score)


def _get_admin_emails():
    """Retorna conjunto de e-mails com privilégio admin (env)."""
    raw = os.getenv("ADMIN_EMAILS", "")
    candidates = [e.strip().lower() for e in raw.split(",") if e.strip()]
    for key in ("BOOTSTRAP_ADMIN_EMAIL", "MAIL_USERNAME"):
        val = (os.getenv(key) or "").strip().lower()
        if val:
            candidates.append(val)
    return set(candidates)


def _password_reset_email_body(full_name: str, reset_url: str) -> str:
    return f"""Olá {full_name},

Recebemos uma solicitação para redefinir sua senha no Agentefrete.

Para criar uma nova senha, acesse o link abaixo (válido por 1 hora):
{reset_url}

Se você não solicitou esta redefinição, ignore este e-mail.

Atenciosamente,
Equipe Agentefrete
"""


def _password_reset_email_html_body(full_name: str, reset_url: str) -> str:
    """
    Versão HTML do e-mail de recuperação com link clicável.
    Mantém o conteúdo equivalente ao texto plano.
    """
    return f"""
<p>Olá {full_name},</p>
<p>Recebemos uma solicitação para redefinir sua senha no Agentefrete.</p>
<p>Para criar uma nova senha, clique no link abaixo (válido por 1 hora):<br>
<a href="{reset_url}">{reset_url}</a></p>
<p>Se você não solicitou esta redefinição, ignore este e-mail.</p>
<p>Atenciosamente,<br>
Equipe Agentefrete</p>
""".strip()


# --- Autenticação local (email/senha) ---


def authenticate_user(email: str, password: str):
    """
    Autentica usuário por e-mail e senha.
    Retorna (user, None) em sucesso ou (None, mensagem_erro) em falha.
    """
    email = _normalize_email(email)
    if not email or not (password or "").strip():
        return None, "Email ou senha incorretos."
    user = User.query.filter(func.lower(User.email) == email).order_by(User.id.asc()).first()
    if not user or not user.verify_password(password):
        return None, "Email ou senha incorretos."
    user.last_login_at = _utcnow_naive()
    db.session.commit()
    return user, None


# --- Recuperação de senha ---


def request_password_reset(
    email: str,
    *,
    secret_key: str,
    build_reset_url,
):
    """
    Processa solicitação de recuperação de senha: gera token, envia e-mail.
    build_reset_url(token: str) -> str deve retornar a URL completa (ex.: url_for(..., _external=True)).
    Retorna (success: bool, message: str, dev_reset_url: str | None).
    """
    email = _normalize_email(email)
    if not email:
        return False, "Informe o e-mail cadastrado.", None

    user = User.query.filter(func.lower(User.email) == email).order_by(User.id.asc()).first()
    if not user:
        return True, "Se o e-mail estiver cadastrado, enviaremos um link de recuperação.", None

    serializer = _get_serializer(secret_key)
    token = serializer.dumps({"user_id": user.id})
    reset_url = build_reset_url(token)

    subject = PASSWORD_RESET_EMAIL_SUBJECT
    body_text = _password_reset_email_body(user.full_name, reset_url)
    html_body = _password_reset_email_html_body(user.full_name, reset_url)

    try:
        send_email(
            to_email=user.email,
            subject=subject,
            html=html_body,
            text=body_text,
        )
        dev_link = reset_url  # caller pode usar em debug
        return True, "Se o e-mail estiver cadastrado, enviaremos um link de recuperação. Confira também a pasta de spam ou lixo eletrônico.", dev_link
    except Exception as e:
        logger.error("Erro ao enviar e-mail de recuperação de senha via Resend: %s", e)
        return False, "Não foi possível enviar o e-mail de recuperação. Tente novamente mais tarde.", None


def get_user_for_reset_token(token: str, *, secret_key: str):
    """
    Valida token de reset e retorna o usuário para exibir o formulário (GET).
    Retorna (user, None, None) em sucesso ou (None, message, redirect_view) em falha.
    """
    serializer = _get_serializer(secret_key)
    try:
        data = serializer.loads(token, max_age=PASSWORD_RESET_TOKEN_MAX_AGE)
        user_id = data.get("user_id")
    except SignatureExpired:
        return None, "O link de redefinição expirou. Solicite novamente.", "request_password_reset"
    except BadSignature:
        return None, "Link de redefinição inválido.", "login"

    user = db.session.get(User, user_id)
    if not user:
        return None, "Usuário não encontrado.", "login"
    return user, None, None


def reset_password_with_token(
    token: str,
    password: str,
    confirm_password: str,
    *,
    secret_key: str,
):
    """
    Valida token e redefine a senha do usuário.
    Retorna (success: bool, message: str, redirect_view: str | None).
    redirect_view é o nome da rota para redirect (ex. 'login' ou 'request_password_reset').
    """
    user, err_msg, redirect_view = get_user_for_reset_token(token, secret_key=secret_key)
    if user is None:
        return False, err_msg, redirect_view

    if not password or not confirm_password:
        return False, "Preencha todos os campos.", None
    if password != confirm_password:
        return False, "As senhas não conferem.", None

    user.set_password(password)
    db.session.commit()
    return True, "Senha redefinida com sucesso. Faça login com a nova senha.", "login"


# --- OAuth Google ---


def get_google_oauth_login_url(*, client_id: str, redirect_uri: str, auth_url: str):
    """
    Gera state e URL de autorização Google.
    Retorna (auth_url: str, state: str). A rota deve guardar state na session.
    """
    state = secrets.token_urlsafe(32)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
    }
    url = f"{auth_url}?{urlencode(params)}"
    return url, state


def handle_google_oauth_callback(
    code: str,
    state: str,
    session_state: str,
    *,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    token_url: str,
    userinfo_url: str,
):
    """
    Troca code por token, obtém userinfo, busca/cria usuário.
    Retorna (user, error_message, needs_profile_completion).
    error_message é None em sucesso; needs_profile_completion indica redirect para complete_profile.
    """
    if not state or state != session_state:
        logger.error("State inválido ou não encontrado - proteção CSRF falhada")
        return None, "Falha na validação de segurança. Tente novamente.", False

    if not code:
        return None, "Authorization code não fornecido.", False

    try:
        token_data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
        token_response = requests.post(token_url, data=token_data, timeout=30)
        if token_response.status_code != 200:
            logger.error("Erro ao obter token: %s", token_response.text)
            return None, "Não foi possível obter o token de acesso.", False

        tokens = token_response.json()
        access_token = tokens.get("access_token")
        if not access_token:
            return None, "Não foi possível obter o token de acesso.", False

        headers = {"Authorization": f"Bearer {access_token}"}
        userinfo_response = requests.get(userinfo_url, headers=headers, timeout=30)
        if userinfo_response.status_code != 200:
            logger.error("Erro ao obter userinfo: %s", userinfo_response.text)
            return None, "Não foi possível obter os dados do Google.", False

        user_data = userinfo_response.json()
        email = _normalize_email(user_data.get("email") or "")
        name = user_data.get("name") or user_data.get("given_name") or "Usuário Google"
        # Em OpenID Connect o identificador estavel e "sub". Mantemos fallback para "id" por compatibilidade.
        google_id = (user_data.get("sub") or user_data.get("id") or "").strip() or None

        if not email:
            return None, "Sua conta Google não retornou um e-mail válido.", False

        admin_emails = _get_admin_emails()
        user = None
        if google_id:
            user = User.query.filter_by(oauth_provider="google", oauth_sub=google_id).first()

        email_candidates = (
            User.query.filter(func.lower(User.email) == email)
            .order_by(User.id.asc())
            .all()
        )

        if not user and email_candidates:
            user = _select_canonical_user(email_candidates, google_id)
            if len(email_candidates) > 1:
                logger.warning(
                    "Múltiplos usuários para e-mail normalizado '%s'. Selecionado id=%s",
                    email,
                    user.id,
                )

        if not user:
            user = User(
                email=email,
                full_name=name,
                is_admin=email in admin_emails,
                categoria="free",
                creditos=10,
                subscribes_to_newsletter=False,
                usage_purpose=None,
                job_role=None,
                oauth_provider="google",
                oauth_sub=google_id,
            )
            db.session.add(user)
        else:
            if not user.oauth_provider:
                user.oauth_provider = "google"
                user.oauth_sub = google_id
            elif user.oauth_provider == "google" and not user.oauth_sub and google_id:
                user.oauth_sub = google_id
            elif user.oauth_provider == "google" and google_id and user.oauth_sub != google_id:
                # Mantem vinculo Google consistente para evitar fallback por e-mail em logins futuros.
                user.oauth_sub = google_id
            if email in admin_emails and not user.is_admin:
                user.is_admin = True

        user.last_login_at = _utcnow_naive()
        db.session.commit()

        # Emergencial: nunca bloquear login OAuth com redirecionamento obrigatório
        # para complete-profile. O perfil pode ser completado manualmente depois.
        needs_profile = False
        return user, None, needs_profile

    except requests.RequestException as e:
        logger.exception("Erro de rede no callback OAuth: %s", e)
        return None, f"Erro no login com Google: {e}", False
    except Exception as e:
        logger.exception("Erro no google_callback: %s", e)
        return None, f"Erro no login com Google: {str(e)}", False


# --- Perfil e registro ---


def complete_user_profile(
    user,
    job_role: str,
    usage_purpose: str,
    subscribes_to_newsletter: bool,
):
    """Atualiza perfil do usuário. Retorna (success: bool, message: str)."""
    if not (job_role or "").strip() or not (usage_purpose or "").strip():
        return False, "Por favor, preencha todos os campos obrigatórios."
    user.job_role = (job_role or "").strip()
    user.usage_purpose = (usage_purpose or "").strip()
    user.subscribes_to_newsletter = bool(subscribes_to_newsletter)
    db.session.commit()
    return True, "Perfil completado com sucesso! Bem-vindo!"


def register_user(
    full_name: str,
    email: str,
    password: str,
    job_role: str = "",
    usage_purpose: str = "",
    subscribes_to_newsletter: bool = False,
):
    """
    Cria novo usuário (cadastro local).
    Retorna (user, None) em sucesso ou (None, mensagem_erro) em falha.
    """
    full_name = (full_name or "").strip()
    email = _normalize_email(email)
    if not full_name or not email or not (password or "").strip():
        return None, "Por favor, preencha nome, e-mail e senha."

    if User.query.filter(func.lower(User.email) == email).first():
        return None, "Este e-mail já está cadastrado."

    new_user = User(
        email=email,
        full_name=full_name or email,
        is_admin=False,
        categoria="free",
        creditos=10,
        subscribes_to_newsletter=subscribes_to_newsletter,
        usage_purpose=usage_purpose or None,
        job_role=job_role or None,
    )
    new_user.set_password(password)
    db.session.add(new_user)
    db.session.commit()
    return new_user, None
