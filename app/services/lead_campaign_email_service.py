"""
Etapa 3/4 - e-mail CTA assinado, follow-up, clique e unsubscribe da jornada desktop_access.

Nao concede autenticacao, creditos nem beneficio especial.
Nao altera newsletter. Reconciliacao Lead->User fica no conversion service.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Callable

from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.auth_services import send_email
from app.extensions import db
from app.models import Lead, utcnow_naive
from app.services.lead_acquisition_service import CAMPANHA_ACESSO_DESKTOP
from app.services.communication_suppression_service import (
    PURPOSE_ACTIVATION,
    PURPOSE_PRE_REGISTRATION,
    SOURCE_CAMPAIGN_UNSUBSCRIBE,
    check_email_suppression,
    normalize_email_hmac,
    suppress_email,
    suppress_email_hmac,
)
from app.services.lead_email_state import (
    LeadEmailIdentityError,
    is_lead_email_minimized,
)

logger = logging.getLogger(__name__)

PURPOSE_CTA = "desktop_access_cta"
PURPOSE_UNSUBSCRIBE = "desktop_access_unsubscribe"

_CTA_SALT = "desktop-access-cta-salt"
_UNSUBSCRIBE_SALT = "desktop-access-unsubscribe-salt"

CTA_EMAIL_SUBJECT = "Continue no computador com o AgenteFrete"
FOLLOWUP_EMAIL_SUBJECT = "Lembrete: continue no computador com o AgenteFrete"

# Constantes operacionais do follow-up MVP (unico ponto canonico).
FOLLOWUP_DELAY_HOURS = 24
MAX_FOLLOWUPS = 1

_STATUS_SENT = "sent"
_STATUS_SKIPPED = "skipped"
_STATUS_FAILED = "failed"
_STATUS_SKIPPED_CONVERTED = "skipped_converted"
_STATUS_SKIPPED_OPT_OUT = "skipped_opt_out"
_STATUS_SKIPPED_SUPPRESSION_UNAVAILABLE = "suppression_check_unavailable"
_STATUS_SKIPPED_LEAD_EMAIL_MINIMIZED = "skipped_lead_email_minimized"


def _build_pre_registration_layout(
    *,
    subject: str,
    headline: str,
    paragraphs: list[str],
    cta_label: str,
    cta_url: str,
    unsubscribe_url: str,
    unsubscribe_label: str = "Cancelar mensagens",
) -> dict[str, str]:
    """Layout visual compartilhado do fluxo PRE-CADASTRO (conteudo injetado pelo caller)."""
    text_parts = ["[AgenteFrete]", "", headline, ""]
    text_parts.extend(paragraphs)
    text_parts.extend(
        [
            "",
            f"[ {cta_label.upper()} ]",
            cta_url,
            "",
            f"{unsubscribe_label}:",
            unsubscribe_url,
            "",
        ]
    )
    text = "\n".join(text_parts)

    paragraphs_html = "".join(
        f'<p style="margin:0 0 14px 0;font-size:16px;line-height:1.7;color:#334155;">{p}</p>'
        for p in paragraphs
    )
    html = f"""
<div style="background:#f5f7fb;padding:32px 16px;font-family:Arial,sans-serif;color:#14213d;">
  <div style="max-width:640px;margin:0 auto;background:#ffffff;border:1px solid #d9e2f2;border-radius:24px;overflow:hidden;">
    <div style="padding:18px 28px;background:#14213d;color:#ffffff;font-size:14px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;">
      AgenteFrete
    </div>
    <div style="padding:32px 28px 16px 28px;">
      <h1 style="margin:0 0 16px 0;font-size:28px;line-height:1.2;color:#0f172a;">
        {headline}
      </h1>
      {paragraphs_html}
      <div style="margin:28px 0 22px 0;">
        <a href="{cta_url}" style="display:inline-block;background:#ffb703;color:#14213d;text-decoration:none;font-weight:800;font-size:14px;letter-spacing:0.04em;text-transform:uppercase;padding:16px 24px;border-radius:14px;">
          {cta_label}
        </a>
      </div>
    </div>
    <div style="padding:18px 28px 24px 28px;border-top:1px solid #e2e8f0;background:#f8fafc;">
      <a href="{unsubscribe_url}" style="font-size:14px;line-height:1.5;color:#64748b;text-decoration:underline;">
        {unsubscribe_label}
      </a>
    </div>
  </div>
</div>
""".strip()
    return {
        "subject": subject,
        "html": html,
        "text": text,
    }


def _serializer(secret_key: str, salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key, salt=salt)


def generate_cta_token(lead_id: int, *, secret_key: str) -> str:
    payload = {"lead_id": int(lead_id), "purpose": PURPOSE_CTA}
    return _serializer(secret_key, _CTA_SALT).dumps(payload)


def generate_unsubscribe_token(lead_id: int, *, secret_key: str) -> str:
    payload = {"lead_id": int(lead_id), "purpose": PURPOSE_UNSUBSCRIBE}
    return _serializer(secret_key, _UNSUBSCRIBE_SALT).dumps(payload)


def loads_cta_payload(token: str, *, secret_key: str) -> dict[str, Any] | None:
    """
    Valida assinatura CTA sem max_age.

    Decisao MVP: o link nao concede sessao/credito; permanece utilizavel
    sem expiracao temporal nesta versao.
    """
    try:
        data = _serializer(secret_key, _CTA_SALT).loads(token)
    except BadSignature:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("purpose") != PURPOSE_CTA:
        return None
    lead_id = data.get("lead_id")
    if not isinstance(lead_id, int):
        return None
    return {"lead_id": lead_id, "purpose": PURPOSE_CTA}


def loads_unsubscribe_payload(token: str, *, secret_key: str) -> dict[str, Any] | None:
    """Valida assinatura unsubscribe sem max_age (permanece utilizavel)."""
    try:
        data = _serializer(secret_key, _UNSUBSCRIBE_SALT).loads(token)
    except BadSignature:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("purpose") != PURPOSE_UNSUBSCRIBE:
        return None
    lead_id = data.get("lead_id")
    if not isinstance(lead_id, int):
        return None
    return {"lead_id": lead_id, "purpose": PURPOSE_UNSUBSCRIBE}


def _lead_compativel_campanha(lead: Lead | None) -> Lead | None:
    if lead is None:
        return None
    if lead.acquisition_campaign != CAMPANHA_ACESSO_DESKTOP:
        return None
    return lead


def resolve_lead_for_cta_token(token: str, *, secret_key: str) -> Lead | None:
    payload = loads_cta_payload(token, secret_key=secret_key)
    if payload is None:
        return None
    lead = db.session.get(Lead, payload["lead_id"])
    return _lead_compativel_campanha(lead)


def resolve_lead_for_unsubscribe_token(token: str, *, secret_key: str) -> Lead | None:
    payload = loads_unsubscribe_payload(token, secret_key=secret_key)
    if payload is None:
        return None
    lead = db.session.get(Lead, payload["lead_id"])
    return _lead_compativel_campanha(lead)


def build_initial_cta_email(*, cta_url: str, unsubscribe_url: str) -> dict[str, str]:
    """
    Builder puro do e-mail CTA inicial PRE-CADASTRO (subject + html + text).

    Usado pelo envio real e pelo E2E Replay, com a mesma copy e URLs injetadas.
    """
    return _build_pre_registration_layout(
        subject=CTA_EMAIL_SUBJECT,
        headline="Continue sua experiência no computador",
        paragraphs=[
            "Você pediu para continuar sua experiência no AgenteFrete pelo computador.",
            "Crie sua conta para acessar as ferramentas e começar suas análises.",
        ],
        cta_label="Continuar no computador",
        cta_url=cta_url,
        unsubscribe_url=unsubscribe_url,
    )


def _build_cta_email_text(*, cta_url: str, unsubscribe_url: str) -> str:
    return build_initial_cta_email(cta_url=cta_url, unsubscribe_url=unsubscribe_url)["text"]


def _build_cta_email_html(*, cta_url: str, unsubscribe_url: str) -> str:
    return build_initial_cta_email(cta_url=cta_url, unsubscribe_url=unsubscribe_url)["html"]


def _pre_registration_communication_block(lead: Lead) -> str | None:
    """Skip de envio pré-cadastro: opt-out real vs falha operacional da suppression."""
    if is_lead_email_minimized(lead):
        return _STATUS_SKIPPED_LEAD_EMAIL_MINIMIZED
    if lead.opt_out_at is not None:
        return _STATUS_SKIPPED_OPT_OUT
    check = check_email_suppression(lead.email, PURPOSE_PRE_REGISTRATION)
    if check.is_unavailable:
        return _STATUS_SKIPPED_SUPPRESSION_UNAVAILABLE
    if check.suppressed:
        return _STATUS_SKIPPED_OPT_OUT
    return None


def should_send_initial_cta(lead: Lead) -> bool:
    if lead.acquisition_campaign != CAMPANHA_ACESSO_DESKTOP:
        return False
    if _pre_registration_communication_block(lead) is not None:
        return False
    if lead.cta_email_sent_at is not None:
        return False
    return True


def maybe_send_initial_cta_email(
    lead: Lead,
    *,
    secret_key: str,
    build_cta_url: Callable[[str], str],
    build_unsubscribe_url: Callable[[str], str],
) -> str:
    """
    Envia no maximo um CTA inicial por Lead/campanha.

    Retorna: sent | skipped | failed | suppression_check_unavailable
    Grava cta_email_sent_at somente apos sucesso do sender.
    """
    block = _pre_registration_communication_block(lead)
    if block == _STATUS_SKIPPED_SUPPRESSION_UNAVAILABLE:
        return _STATUS_SKIPPED_SUPPRESSION_UNAVAILABLE
    if block == _STATUS_SKIPPED_LEAD_EMAIL_MINIMIZED:
        return _STATUS_SKIPPED_LEAD_EMAIL_MINIMIZED
    if not should_send_initial_cta(lead):
        return _STATUS_SKIPPED

    try:
        if is_lead_email_minimized(lead):
            return _STATUS_SKIPPED_LEAD_EMAIL_MINIMIZED
        cta_token = generate_cta_token(lead.id, secret_key=secret_key)
        unsub_token = generate_unsubscribe_token(lead.id, secret_key=secret_key)
        cta_url = build_cta_url(cta_token)
        unsubscribe_url = build_unsubscribe_url(unsub_token)
        built = build_initial_cta_email(cta_url=cta_url, unsubscribe_url=unsubscribe_url)
        send_email(
            to_email=lead.email,
            subject=built["subject"],
            html=built["html"],
            text=built["text"],
        )
    except Exception:
        logger.exception(
            "Falha ao enviar CTA inicial da jornada desktop_access: lead_id=%s",
            lead.id,
        )
        return _STATUS_FAILED

    lead.cta_email_sent_at = utcnow_naive()
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception(
            "CTA enviado mas falha ao gravar cta_email_sent_at: lead_id=%s",
            lead.id,
        )
        return _STATUS_FAILED

    logger.info(
        "CTA inicial enviado para jornada desktop_access: lead_id=%s",
        lead.id,
    )
    return _STATUS_SENT


def mark_first_cta_click(lead: Lead) -> None:
    """Primeiro clique vence; idempotente."""
    if lead.cta_clicked_at is not None:
        return
    lead.cta_clicked_at = utcnow_naive()
    db.session.commit()


def apply_campaign_opt_out(lead: Lead) -> None:
    """
    Primeiro opt-out vence; um commit final.

    Mapeamento alinhado ao futuro backfill histórico:
    opt_out_at → PRE_REGISTRATION + ACTIVATION.
    Não altera newsletter nem remove o Lead.

    Lead minimizado: usa Lead.email_hmac (nunca o placeholder).
    HMAC ausente/inválido: fail-closed, rollback total.
    """
    try:
        if lead.opt_out_at is None:
            lead.opt_out_at = utcnow_naive()
        stamp = lead.opt_out_at
        if is_lead_email_minimized(lead):
            digest = normalize_email_hmac(getattr(lead, "email_hmac", None))
            if digest is None:
                raise LeadEmailIdentityError(
                    "Lead minimizado sem email_hmac valido; suppression recusada"
                )
            ok_pre = suppress_email_hmac(
                digest,
                PURPOSE_PRE_REGISTRATION,
                SOURCE_CAMPAIGN_UNSUBSCRIBE,
                suppressed_at=stamp,
                commit=False,
            )
            ok_act = suppress_email_hmac(
                digest,
                PURPOSE_ACTIVATION,
                SOURCE_CAMPAIGN_UNSUBSCRIBE,
                suppressed_at=stamp,
                commit=False,
            )
            if not ok_pre or not ok_act:
                raise LeadEmailIdentityError(
                    "Falha ao persistir suppression por HMAC no opt-out de campanha"
                )
        else:
            suppress_email(
                lead.email,
                PURPOSE_PRE_REGISTRATION,
                SOURCE_CAMPAIGN_UNSUBSCRIBE,
                suppressed_at=stamp,
                commit=False,
            )
            suppress_email(
                lead.email,
                PURPOSE_ACTIVATION,
                SOURCE_CAMPAIGN_UNSUBSCRIBE,
                suppressed_at=stamp,
                commit=False,
            )
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception(
            "Falha ao aplicar opt-out de campanha: lead_id=%s",
            getattr(lead, "id", None),
        )
        raise


def build_followup_email(*, cta_url: str, unsubscribe_url: str) -> dict[str, str]:
    """
    Builder puro do e-mail de follow-up PRE-CADASTRO (subject + html + text).

    Usado pelo envio real e pelo E2E Replay, com a mesma copy e URLs injetadas.
    Conteudo proprio — nao reutiliza a copy do CTA inicial nem da ativacao.
    """
    return _build_pre_registration_layout(
        subject=FOLLOWUP_EMAIL_SUBJECT,
        headline="Seu acesso no computador ainda está disponível",
        paragraphs=[
            "Se você ainda quiser continuar, use o link abaixo para criar sua conta.",
            "Se você já realizou o cadastro recentemente, pode ignorar esta mensagem.",
        ],
        cta_label="Continuar meu cadastro",
        cta_url=cta_url,
        unsubscribe_url=unsubscribe_url,
    )


def _build_followup_email_text(*, cta_url: str, unsubscribe_url: str) -> str:
    return build_followup_email(cta_url=cta_url, unsubscribe_url=unsubscribe_url)["text"]


def _build_followup_email_html(*, cta_url: str, unsubscribe_url: str) -> str:
    return build_followup_email(cta_url=cta_url, unsubscribe_url=unsubscribe_url)["html"]


def followup_eligible_since(*, now=None):
    """Limite temporal: elegivel quando cta_email_sent_at <= agora - FOLLOWUP_DELAY_HOURS."""
    ref = now if now is not None else utcnow_naive()
    return ref - timedelta(hours=FOLLOWUP_DELAY_HOURS)


def should_send_followup(lead: Lead, *, now=None) -> bool:
    """Elegibilidade de consulta (antes do recheck final pre-envio)."""
    if lead.acquisition_campaign != CAMPANHA_ACESSO_DESKTOP:
        return False
    if lead.cta_email_sent_at is None:
        return False
    if lead.converted_user_id is not None:
        return False
    if _pre_registration_communication_block(lead) is not None:
        return False
    count = lead.followup_count if lead.followup_count is not None else 0
    if count >= MAX_FOLLOWUPS:
        return False
    if lead.cta_email_sent_at > followup_eligible_since(now=now):
        return False
    return True


def list_followup_candidates(*, now=None) -> list[Lead]:
    """Leads elegiveis ao unico follow-up (apos reconciliacao do batch)."""
    threshold = followup_eligible_since(now=now)
    return (
        Lead.query.filter(
            Lead.acquisition_campaign == CAMPANHA_ACESSO_DESKTOP,
            Lead.cta_email_sent_at.isnot(None),
            Lead.converted_user_id.is_(None),
            Lead.opt_out_at.is_(None),
            Lead.followup_count < MAX_FOLLOWUPS,
            Lead.cta_email_sent_at <= threshold,
        )
        .order_by(Lead.id.asc())
        .all()
    )


def maybe_send_followup_email(
    lead: Lead,
    *,
    secret_key: str,
    build_cta_url: Callable[[str], str],
    build_unsubscribe_url: Callable[[str], str],
    now=None,
) -> str:
    """
    Envia no maximo um follow-up por Lead/campanha.

    Retorna: sent | skipped | skipped_converted | skipped_opt_out |
    suppression_check_unavailable | failed

    Recheck imediatamente antes do envio:
    - User correspondente -> marca conversao e nao envia
    - opt_out_at definido -> nao envia

    Race residual: entre o recheck final e o send_email externo um cadastro
    pode ainda ocorrer; aceito no MVP sem lock distribuido.

    Grava followup_count / last_followup_sent_at somente apos sucesso do sender.
    Nao altera cta_email_sent_at.
    """
    from app.services.lead_campaign_conversion_service import (
        STATUS_AMBIGUOUS,
        STATUS_CONVERTED,
        reconcile_lead,
    )

    if not should_send_followup(lead, now=now):
        block = _pre_registration_communication_block(lead)
        if block == _STATUS_SKIPPED_LEAD_EMAIL_MINIMIZED:
            return _STATUS_SKIPPED_LEAD_EMAIL_MINIMIZED
        if lead.converted_user_id is not None:
            return _STATUS_SKIPPED_CONVERTED
        if block is not None:
            return block
        return _STATUS_SKIPPED

    # Recheck de conversao imediatamente antes do envio.
    recon_status = reconcile_lead(lead)
    if recon_status == STATUS_CONVERTED or lead.converted_user_id is not None:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception(
                "Follow-up: conversao detectada mas falha ao gravar: lead_id=%s",
                lead.id,
            )
            return _STATUS_FAILED
        logger.info(
            "Follow-up suprimido por conversao (recheck): lead_id=%s",
            lead.id,
        )
        return _STATUS_SKIPPED_CONVERTED

    if recon_status == STATUS_AMBIGUOUS:
        # Nao enviar lembrete quando ha Users ambiguos para o e-mail logico.
        return _STATUS_SKIPPED

    # Recheck de opt-out (pode ter mudado apos a selecao do batch).
    db.session.refresh(lead)
    block = _pre_registration_communication_block(lead)
    if block is not None:
        return block

    if not should_send_followup(lead, now=now):
        return _STATUS_SKIPPED

    try:
        if is_lead_email_minimized(lead):
            return _STATUS_SKIPPED_LEAD_EMAIL_MINIMIZED
        cta_token = generate_cta_token(lead.id, secret_key=secret_key)
        unsub_token = generate_unsubscribe_token(lead.id, secret_key=secret_key)
        cta_url = build_cta_url(cta_token)
        unsubscribe_url = build_unsubscribe_url(unsub_token)
        built = build_followup_email(cta_url=cta_url, unsubscribe_url=unsubscribe_url)
        send_email(
            to_email=lead.email,
            subject=built["subject"],
            html=built["html"],
            text=built["text"],
        )
    except Exception:
        logger.exception(
            "Falha ao enviar follow-up da jornada desktop_access: lead_id=%s",
            lead.id,
        )
        return _STATUS_FAILED

    sent_at = now if now is not None else utcnow_naive()
    lead.followup_count = (lead.followup_count or 0) + 1
    lead.last_followup_sent_at = sent_at
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception(
            "Follow-up enviado mas falha ao gravar contadores: lead_id=%s",
            lead.id,
        )
        return _STATUS_FAILED

    logger.info(
        "Follow-up enviado para jornada desktop_access: lead_id=%s followup_count=%s",
        lead.id,
        lead.followup_count,
    )
    return _STATUS_SENT


def process_eligible_followups(
    *,
    secret_key: str,
    build_cta_url: Callable[[str], str],
    build_unsubscribe_url: Callable[[str], str],
    now=None,
) -> dict[str, Any]:
    """
    Processa candidatos a follow-up um a um.

    Falha de um Lead nao aborta os demais (rollback local via commit por envio).
    Deve ser chamada apos a reconciliacao do batch.
    """
    stats = {
        "candidates": 0,
        "sent": 0,
        "skipped": 0,
        "skipped_converted": 0,
        "skipped_opt_out": 0,
        "skipped_suppression_unavailable": 0,
        "skipped_lead_email_minimized": 0,
        "failed": 0,
    }
    candidates = list_followup_candidates(now=now)
    stats["candidates"] = len(candidates)

    for lead in candidates:
        try:
            status = maybe_send_followup_email(
                lead,
                secret_key=secret_key,
                build_cta_url=build_cta_url,
                build_unsubscribe_url=build_unsubscribe_url,
                now=now,
            )
        except Exception:
            db.session.rollback()
            logger.exception(
                "Erro inesperado no follow-up desktop_access: lead_id=%s",
                lead.id,
            )
            stats["failed"] += 1
            continue

        if status == _STATUS_SENT:
            stats["sent"] += 1
        elif status == _STATUS_SKIPPED_CONVERTED:
            stats["skipped_converted"] += 1
        elif status == _STATUS_SKIPPED_OPT_OUT:
            stats["skipped_opt_out"] += 1
        elif status == _STATUS_SKIPPED_SUPPRESSION_UNAVAILABLE:
            stats["skipped_suppression_unavailable"] += 1
        elif status == _STATUS_SKIPPED_LEAD_EMAIL_MINIMIZED:
            stats["skipped_lead_email_minimized"] += 1
        elif status == _STATUS_FAILED:
            stats["failed"] += 1
        else:
            stats["skipped"] += 1

    logger.info(
        "Follow-up desktop_access: candidates=%s sent=%s skipped_converted=%s "
        "skipped_opt_out=%s skipped_suppression_unavailable=%s skipped=%s failed=%s",
        stats["candidates"],
        stats["sent"],
        stats["skipped_converted"],
        stats["skipped_opt_out"],
        stats["skipped_suppression_unavailable"],
        stats["skipped"],
        stats["failed"],
    )
    return stats
