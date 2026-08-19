"""
Ativação pós-cadastro — sequência de e-mails desktop_access.

Registration → 24h sem upload → E-mail 1 → 48h sem upload → E-mail 2
Máximo 2 e-mails. Um file_uploaded (Cleide ou AgenteCompara), opt-out
ou encerramento operacional da jornada (vínculo do User) encerra.

Não mistura com builders/follow-up pré-cadastro.
"""
from __future__ import annotations

import base64
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.auth_services import send_email
from app.extensions import db
from app.funnel_event_service import (
    FUNNEL_EVENT_FILE_UPLOADED,
    FUNNEL_SOURCE_AGENTE_COMPARA,
    FUNNEL_SOURCE_CLEIDE_AUDIT,
)
from app.models import FunnelEvent, Lead, User, utcnow_naive
from app.services.lead_acquisition_service import CAMPANHA_ACESSO_DESKTOP
from app.services.communication_suppression_service import (
    PURPOSE_ACTIVATION,
    SOURCE_ACTIVATION_UNSUBSCRIBE,
    check_email_suppression,
    normalize_email_hmac,
    suppress_email,
    suppress_email_hmac,
)
from app.services.lead_email_state import (
    LeadEmailIdentityError,
    is_lead_email_minimized,
)
from app.services.user_lifecycle_service import is_user_operationally_closed

logger = logging.getLogger(__name__)

PURPOSE_ACTIVATION_UNSUBSCRIBE = "desktop_access_activation_unsubscribe"
_ACTIVATION_UNSUBSCRIBE_SALT = "desktop-access-activation-unsubscribe-salt"

ACTIVATION_EMAIL_1_DELAY_HOURS = 24
ACTIVATION_EMAIL_2_DELAY_HOURS = 48
MAX_ACTIVATION_EMAILS = 2

ACTIVATION_EMAIL_1_SUBJECT = "Você está pagando o que foi acordado no seu frete?"
ACTIVATION_EMAIL_1_PREHEADER = (
    "Uma conferência simples pode mostrar se o valor cobrado segue as condições negociadas."
)
ACTIVATION_EMAIL_1_HEADLINE = "Você está pagando o que foi acordado?"
ACTIVATION_EMAIL_1_SUBHEADLINE = (
    "Compare o valor negociado com o valor cobrado antes da próxima análise."
)
ACTIVATION_EMAIL_1_MODULE = "Cleide Auditoria"
ACTIVATION_EMAIL_1_CTA = "Auditar meu primeiro frete"
ACTIVATION_EMAIL_1_HERO_ALT = (
    "Negociado, cobrado e conferido — fluxo visual da Cleide Auditoria"
)
ACTIVATION_EMAIL_1_CID = "activation-email1-hero"

ACTIVATION_EMAIL_2_SUBJECT = "Sua transportadora ainda oferece o melhor custo-benefício?"
ACTIVATION_EMAIL_2_PREHEADER = (
    "Compare tabelas e veja as diferenças antes da próxima negociação."
)
ACTIVATION_EMAIL_2_HEADLINE = "Sua transportadora ainda oferece o melhor custo-benefício?"
ACTIVATION_EMAIL_2_SUBHEADLINE = (
    "Coloque tabelas lado a lado e veja as diferenças antes da próxima negociação."
)
ACTIVATION_EMAIL_2_MODULE = "AgenteCompara"
ACTIVATION_EMAIL_2_CTA = "Comparar minhas tabelas"
ACTIVATION_EMAIL_2_HERO_ALT = (
    "Comparação lado a lado de tabelas de transportadoras — AgenteCompara"
)
ACTIVATION_EMAIL_2_CID = "activation-email2-hero"

_STATUS_SENT = "sent"
_STATUS_SKIPPED = "skipped"
_STATUS_FAILED = "failed"
_STATUS_SKIPPED_UPLOAD = "skipped_upload"
_STATUS_SKIPPED_OPT_OUT = "skipped_opt_out"
_STATUS_SKIPPED_JOURNEY_ENDED = "skipped_journey_ended"
_STATUS_SKIPPED_SUPPRESSION_UNAVAILABLE = "suppression_check_unavailable"
_STATUS_SKIPPED_LEAD_EMAIL_MINIMIZED = "skipped_lead_email_minimized"

_IMG_DIR = Path(__file__).resolve().parents[1] / "static" / "img" / "email"
_HERO_EMAIL_1_PATH = _IMG_DIR / "activation_email1_negociado_cobrado_conferido.png"
_HERO_EMAIL_2_PATH = _IMG_DIR / "activation_email2_comparacao_tabelas.png"
_LOGO_PATH = _IMG_DIR / "logo_agentefrete.png"
_LOGO_CID = "agentefrete-logo"
_LOGO_FILENAME = "logo_agentefrete.png"


def _serializer(secret_key: str, salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key, salt=salt)


def generate_activation_unsubscribe_token(lead_id: int, *, secret_key: str) -> str:
    payload = {"lead_id": int(lead_id), "purpose": PURPOSE_ACTIVATION_UNSUBSCRIBE}
    return _serializer(secret_key, _ACTIVATION_UNSUBSCRIBE_SALT).dumps(payload)


def loads_activation_unsubscribe_payload(
    token: str, *, secret_key: str
) -> dict[str, Any] | None:
    try:
        data = _serializer(secret_key, _ACTIVATION_UNSUBSCRIBE_SALT).loads(token)
    except BadSignature:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("purpose") != PURPOSE_ACTIVATION_UNSUBSCRIBE:
        return None
    if "email" in data:
        return None
    lead_id = data.get("lead_id")
    if not isinstance(lead_id, int):
        return None
    return {"lead_id": lead_id, "purpose": PURPOSE_ACTIVATION_UNSUBSCRIBE}


def resolve_lead_for_activation_unsubscribe_token(
    token: str, *, secret_key: str
) -> Lead | None:
    payload = loads_activation_unsubscribe_payload(token, secret_key=secret_key)
    if payload is None:
        return None
    lead = db.session.get(Lead, payload["lead_id"])
    if lead is None:
        return None
    if lead.acquisition_campaign != CAMPANHA_ACESSO_DESKTOP:
        return None
    return lead


def apply_activation_opt_out(lead: Lead, *, now=None) -> None:
    """First-write-wins; um commit final: activation_opt_out_at + ACTIVATION.

    Lead minimizado: usa Lead.email_hmac (nunca o placeholder).
    HMAC ausente/inválido: fail-closed, rollback total.
    """
    try:
        if lead.activation_opt_out_at is None:
            lead.activation_opt_out_at = now if now is not None else utcnow_naive()
        if is_lead_email_minimized(lead):
            digest = normalize_email_hmac(getattr(lead, "email_hmac", None))
            if digest is None:
                raise LeadEmailIdentityError(
                    "Lead minimizado sem email_hmac valido; suppression recusada"
                )
            ok = suppress_email_hmac(
                digest,
                PURPOSE_ACTIVATION,
                SOURCE_ACTIVATION_UNSUBSCRIBE,
                suppressed_at=lead.activation_opt_out_at,
                commit=False,
            )
            if not ok:
                raise LeadEmailIdentityError(
                    "Falha ao persistir suppression por HMAC no activation opt-out"
                )
        else:
            suppress_email(
                lead.email,
                PURPOSE_ACTIVATION,
                SOURCE_ACTIVATION_UNSUBSCRIBE,
                suppressed_at=lead.activation_opt_out_at,
                commit=False,
            )
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception(
            "Falha ao aplicar activation opt-out: lead_id=%s",
            getattr(lead, "id", None),
        )
        raise


def has_first_upload(user_id: int) -> bool:
    """True se existir qualquer file_uploaded canônico (Cleide ou AgenteCompara)."""
    if user_id is None:
        return False
    exists = (
        FunnelEvent.query.filter(
            FunnelEvent.user_id == int(user_id),
            FunnelEvent.event_name == FUNNEL_EVENT_FILE_UPLOADED,
            FunnelEvent.source.in_(
                (FUNNEL_SOURCE_CLEIDE_AUDIT, FUNNEL_SOURCE_AGENTE_COMPARA)
            ),
        )
        .limit(1)
        .first()
    )
    return exists is not None


def has_valid_campaign_registration(lead: Lead) -> bool:
    """
    Registration canônica da campanha:
    converted_user_id + converted_at >= campaign_captured_at.
    Exclui User pré-existente reconciliado com converted_at anterior à captura.
    """
    if lead.acquisition_campaign != CAMPANHA_ACESSO_DESKTOP:
        return False
    if lead.converted_user_id is None or lead.converted_at is None:
        return False
    if lead.campaign_captured_at is None:
        return False
    return lead.converted_at >= lead.campaign_captured_at


def is_activation_opted_out(lead: Lead) -> bool:
    """Opt-out real da sequência OU opt-out anterior da campanha (privacidade)."""
    return _activation_communication_block(lead) == _STATUS_SKIPPED_OPT_OUT


def _activation_communication_block(lead: Lead) -> str | None:
    """Skip de envio de ativação: opt-out real vs falha operacional da suppression."""
    if is_lead_email_minimized(lead):
        return _STATUS_SKIPPED_LEAD_EMAIL_MINIMIZED
    if lead.activation_opt_out_at is not None:
        return _STATUS_SKIPPED_OPT_OUT
    if lead.opt_out_at is not None:
        return _STATUS_SKIPPED_OPT_OUT
    check = check_email_suppression(lead.email, PURPOSE_ACTIVATION)
    if check.is_unavailable:
        return _STATUS_SKIPPED_SUPPRESSION_UNAVAILABLE
    if check.suppressed:
        return _STATUS_SKIPPED_OPT_OUT
    return None


def is_activation_journey_ended(lead: Lead) -> bool:
    """
    Jornada de ativação explicitamente encerrada para o User convertido atual.

    Não usa activation_opt_out_at. Encerramento antigo de outro user_id
    não bloqueia automaticamente uma jornada futura associada a outro User.
    """
    if getattr(lead, "activation_ended_at", None) is None:
        return False
    ended_for = getattr(lead, "activation_ended_for_user_id", None)
    converted = getattr(lead, "converted_user_id", None)
    if ended_for is None or converted is None:
        return False
    return int(ended_for) == int(converted)


def _converted_user_is_operationally_closed(lead: Lead) -> bool:
    """
    Compatibilidade histórica: User já encerrado antes da coluna existir.

    Conservador: converted_user_id ausente ou User inexistente não é
    inferido como encerrado. Leitura pura — não grava activation_ended_at.
    """
    if lead.converted_user_id is None:
        return False
    user = db.session.get(User, int(lead.converted_user_id))
    if user is None:
        return False
    return is_user_operationally_closed(user)


def is_activation_journey_unavailable(lead: Lead) -> bool:
    """
    Gate completo da jornada: não deve receber e-mail de ativação.

    Combina:
    A) opt-out já existente (activation_opt_out_at / opt_out_at);
    B) CommunicationSuppression ACTIVATION já existente;
    B2) consulta de suppression operacionalmente indisponível (fail-closed);
    C) encerramento explícito da jornada (ended_at + ended_for == converted);
    D) User convertido operacionalmente encerrado (ponte histórica).

    Não trata C/D como opt-out. Não persiste nada.
    """
    if _activation_communication_block(lead) is not None:
        return True
    if is_activation_journey_ended(lead):
        return True
    return _converted_user_is_operationally_closed(lead)


def _skip_reason_when_unavailable(lead: Lead) -> str:
    block = _activation_communication_block(lead)
    if block is not None:
        return block
    return _STATUS_SKIPPED_JOURNEY_ENDED


def activation_email_1_eligible_since(*, now=None):
    ref = now if now is not None else utcnow_naive()
    return ref - timedelta(hours=ACTIVATION_EMAIL_1_DELAY_HOURS)


def activation_email_2_eligible_since(*, now=None):
    ref = now if now is not None else utcnow_naive()
    return ref - timedelta(hours=ACTIVATION_EMAIL_2_DELAY_HOURS)


def email_1_eligible_at(lead: Lead):
    if lead.converted_at is None:
        return None
    return lead.converted_at + timedelta(hours=ACTIVATION_EMAIL_1_DELAY_HOURS)


def email_2_eligible_at(lead: Lead):
    if lead.activation_email_1_sent_at is None:
        return None
    return lead.activation_email_1_sent_at + timedelta(hours=ACTIVATION_EMAIL_2_DELAY_HOURS)


def _cid_attachment(path: Path, *, content_id: str, filename: str) -> dict[str, str]:
    content = base64.b64encode(path.read_bytes()).decode("ascii")
    return {
        "filename": filename,
        "content": content,
        "content_id": content_id,
        "content_type": "image/png",
    }


def _build_activation_attachments(*, hero_path: Path, hero_cid: str, hero_filename: str) -> list[dict[str, str]]:
    attachments: list[dict[str, str]] = []
    if _LOGO_PATH.is_file():
        attachments.append(
            _cid_attachment(_LOGO_PATH, content_id=_LOGO_CID, filename=_LOGO_FILENAME)
        )
    if hero_path.is_file():
        attachments.append(
            _cid_attachment(hero_path, content_id=hero_cid, filename=hero_filename)
        )
    return attachments


def _build_activation_layout(
    *,
    subject: str,
    preheader: str,
    module_label: str,
    headline: str,
    subheadline: str,
    paragraphs: list[str],
    visual_caption: str,
    hero_cid: str,
    hero_alt: str,
    cta_label: str,
    cta_url: str,
    footer_note: str,
    unsubscribe_url: str,
) -> dict[str, Any]:
    """Layout HTML email-safe compartilhado (conteúdo injetado pelos builders)."""
    unsubscribe_label = "Cancelar estes lembretes"
    text_parts = [
        "AgenteFrete",
        module_label,
        "",
        headline,
        subheadline,
        "",
    ]
    text_parts.extend(paragraphs)
    text_parts.extend(
        [
            "",
            visual_caption,
            "",
            f"[ {cta_label} ]",
            cta_url,
            "",
            footer_note,
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
<!DOCTYPE html>
<html lang="pt-BR">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{subject}</title>
</head>
<body style="margin:0;padding:0;background:#f5f7fb;">
  <div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent;">
    {preheader}
  </div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f5f7fb;padding:24px 12px;">
    <tr>
      <td align="center">
        <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;width:100%;background:#ffffff;border:1px solid #d9e2f2;">
          <tr>
            <td style="padding:22px 28px 8px 28px;background:#14213d;font-family:Arial,Helvetica,sans-serif;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td width="64" valign="middle" style="width:64px;padding:0 14px 0 0;">
                    <img src="cid:{_LOGO_CID}" alt="AgenteFrete" width="56" style="display:block;border:0;outline:none;text-decoration:none;width:56px;max-width:56px;height:auto;">
                  </td>
                  <td valign="middle" style="padding:0;">
                    <div style="font-size:18px;line-height:1.2;font-weight:800;letter-spacing:0.02em;color:#ffffff;">
                      AgenteFrete
                    </div>
                    <div style="margin-top:8px;">
                      <span style="display:inline-block;padding:5px 10px;border:1px solid #ffb703;border-radius:999px;font-size:11px;line-height:1.2;font-weight:700;letter-spacing:0.04em;text-transform:uppercase;color:#ffb703;">
                        {module_label}
                      </span>
                    </div>
                  </td>
                </tr>
              </table>
              <h1 style="margin:22px 0 10px 0;font-size:26px;line-height:1.28;font-weight:800;color:#ffffff;">
                {headline}
              </h1>
              <p style="margin:0 0 8px 0;font-size:15px;line-height:1.55;color:#cbd5e1;">
                {subheadline}
              </p>
            </td>
          </tr>
          <tr>
            <td style="padding:0;background:#14213d;line-height:0;font-size:0;">
              <img src="cid:{hero_cid}" alt="{hero_alt}" width="600" style="display:block;border:0;outline:none;text-decoration:none;width:100%;max-width:600px;height:auto;">
            </td>
          </tr>
          <tr>
            <td style="padding:26px 28px 8px 28px;font-family:Arial,Helvetica,sans-serif;">
              {paragraphs_html}
              <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:28px 0 18px 0;">
                <tr>
                  <td align="center" bgcolor="#ffb703" style="border-radius:14px;">
                    <a href="{cta_url}" style="display:inline-block;background:#ffb703;color:#14213d;text-decoration:none;font-weight:800;font-size:14px;letter-spacing:0.04em;text-transform:uppercase;padding:16px 24px;border-radius:14px;font-family:Arial,Helvetica,sans-serif;">
                      {cta_label}
                    </a>
                  </td>
                </tr>
              </table>
              <p style="margin:0 0 8px 0;font-size:14px;line-height:1.6;color:#64748b;">
                {footer_note}
              </p>
            </td>
          </tr>
          <tr>
            <td style="padding:18px 28px 24px 28px;border-top:1px solid #e2e8f0;background:#f8fafc;font-family:Arial,Helvetica,sans-serif;">
              <a href="{unsubscribe_url}" style="font-size:14px;line-height:1.5;color:#64748b;text-decoration:underline;">
                {unsubscribe_label}
              </a>
              <p style="margin:12px 0 0 0;font-size:12px;line-height:1.5;color:#94a3b8;">
                AgenteFrete · Este lembrete refere-se à sua conta.
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
""".strip()
    return {
        "subject": subject,
        "html": html,
        "text": text,
        "preheader": preheader,
    }


def build_activation_email_1(
    *,
    cta_url: str,
    unsubscribe_url: str,
) -> dict[str, Any]:
    """Builder puro do E-mail 1 (Cleide Auditoria)."""
    built = _build_activation_layout(
        subject=ACTIVATION_EMAIL_1_SUBJECT,
        preheader=ACTIVATION_EMAIL_1_PREHEADER,
        module_label=ACTIVATION_EMAIL_1_MODULE,
        headline=ACTIVATION_EMAIL_1_HEADLINE,
        subheadline=ACTIVATION_EMAIL_1_SUBHEADLINE,
        paragraphs=[
            "Você criou sua conta no AgenteFrete, mas ainda não enviou nenhum documento para análise.",
            "Valores, taxas e condições de frete podem passar despercebidos quando o que foi cobrado não é confrontado com o que foi negociado.",
            "A Cleide Auditoria faz essa conferência com você.",
        ],
        visual_caption="NEGOCIADO → COBRADO → CONFERIDO",
        hero_cid=ACTIVATION_EMAIL_1_CID,
        hero_alt=ACTIVATION_EMAIL_1_HERO_ALT,
        cta_label=ACTIVATION_EMAIL_1_CTA,
        cta_url=cta_url,
        footer_note=(
            "Você está recebendo este lembrete porque criou sua conta no AgenteFrete "
            "e ainda não iniciou uma análise."
        ),
        unsubscribe_url=unsubscribe_url,
    )
    built["attachments"] = _build_activation_attachments(
        hero_path=_HERO_EMAIL_1_PATH,
        hero_cid=ACTIVATION_EMAIL_1_CID,
        hero_filename="activation_email1_negociado_cobrado_conferido.png",
    )
    return built


def build_activation_email_2(
    *,
    cta_url: str,
    unsubscribe_url: str,
) -> dict[str, Any]:
    """Builder puro do E-mail 2 (AgenteCompara)."""
    built = _build_activation_layout(
        subject=ACTIVATION_EMAIL_2_SUBJECT,
        preheader=ACTIVATION_EMAIL_2_PREHEADER,
        module_label=ACTIVATION_EMAIL_2_MODULE,
        headline=ACTIVATION_EMAIL_2_HEADLINE,
        subheadline=ACTIVATION_EMAIL_2_SUBHEADLINE,
        paragraphs=[
            "Preço de frete não é apenas uma tarifa.",
            "Faixas de peso, taxas, destinos e condições negociadas podem mudar bastante o resultado final entre transportadoras.",
            "O AgenteCompara ajuda você a colocar tabelas lado a lado e identificar essas diferenças com mais clareza.",
        ],
        visual_caption="COMPARE TABELAS LADO A LADO",
        hero_cid=ACTIVATION_EMAIL_2_CID,
        hero_alt=ACTIVATION_EMAIL_2_HERO_ALT,
        cta_label=ACTIVATION_EMAIL_2_CTA,
        cta_url=cta_url,
        footer_note="Você ainda não iniciou nenhuma análise no AgenteFrete.",
        unsubscribe_url=unsubscribe_url,
    )
    built["attachments"] = _build_activation_attachments(
        hero_path=_HERO_EMAIL_2_PATH,
        hero_cid=ACTIVATION_EMAIL_2_CID,
        hero_filename="activation_email2_comparacao_tabelas.png",
    )
    return built


def should_send_activation_email_1(lead: Lead, *, now=None) -> bool:
    if not has_valid_campaign_registration(lead):
        return False
    if is_activation_journey_unavailable(lead):
        return False
    if lead.activation_email_1_sent_at is not None:
        return False
    if lead.activation_email_2_sent_at is not None:
        return False
    if has_first_upload(int(lead.converted_user_id)):
        return False
    eligible_at = email_1_eligible_at(lead)
    if eligible_at is None:
        return False
    ref = now if now is not None else utcnow_naive()
    return ref >= eligible_at


def should_send_activation_email_2(lead: Lead, *, now=None) -> bool:
    if not has_valid_campaign_registration(lead):
        return False
    if is_activation_journey_unavailable(lead):
        return False
    if lead.activation_email_1_sent_at is None:
        return False
    if lead.activation_email_2_sent_at is not None:
        return False
    if has_first_upload(int(lead.converted_user_id)):
        return False
    eligible_at = email_2_eligible_at(lead)
    if eligible_at is None:
        return False
    ref = now if now is not None else utcnow_naive()
    return ref >= eligible_at


def list_activation_email_1_candidates(*, now=None) -> list[Lead]:
    threshold = activation_email_1_eligible_since(now=now)
    return (
        Lead.query.filter(
            Lead.acquisition_campaign == CAMPANHA_ACESSO_DESKTOP,
            Lead.converted_user_id.isnot(None),
            Lead.converted_at.isnot(None),
            Lead.campaign_captured_at.isnot(None),
            Lead.converted_at >= Lead.campaign_captured_at,
            Lead.activation_email_1_sent_at.is_(None),
            Lead.activation_email_2_sent_at.is_(None),
            Lead.activation_opt_out_at.is_(None),
            Lead.opt_out_at.is_(None),
            Lead.converted_at <= threshold,
        )
        .order_by(Lead.id.asc())
        .all()
    )


def list_activation_email_2_candidates(*, now=None) -> list[Lead]:
    threshold = activation_email_2_eligible_since(now=now)
    return (
        Lead.query.filter(
            Lead.acquisition_campaign == CAMPANHA_ACESSO_DESKTOP,
            Lead.converted_user_id.isnot(None),
            Lead.converted_at.isnot(None),
            Lead.campaign_captured_at.isnot(None),
            Lead.converted_at >= Lead.campaign_captured_at,
            Lead.activation_email_1_sent_at.isnot(None),
            Lead.activation_email_2_sent_at.is_(None),
            Lead.activation_opt_out_at.is_(None),
            Lead.opt_out_at.is_(None),
            Lead.activation_email_1_sent_at <= threshold,
        )
        .order_by(Lead.id.asc())
        .all()
    )


def _list_unsubscribe_headers(unsubscribe_url: str) -> dict[str, str]:
    return {
        "List-Unsubscribe": f"<{unsubscribe_url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


def _recheck_before_send(lead: Lead, *, which: str, now=None) -> str | None:
    """
    Recheck imediato antes do send_email.
    Retorna status de skip ou None se ainda elegível.
    """
    db.session.refresh(lead)
    if is_activation_journey_unavailable(lead):
        return _skip_reason_when_unavailable(lead)
    if lead.converted_user_id is None:
        return _STATUS_SKIPPED
    if has_first_upload(int(lead.converted_user_id)):
        return _STATUS_SKIPPED_UPLOAD
    if which == "email1":
        if lead.activation_email_1_sent_at is not None:
            return _STATUS_SKIPPED
        if not should_send_activation_email_1(lead, now=now):
            return _STATUS_SKIPPED
    else:
        if lead.activation_email_2_sent_at is not None:
            return _STATUS_SKIPPED
        if not should_send_activation_email_2(lead, now=now):
            return _STATUS_SKIPPED
    return None


def maybe_send_activation_email_1(
    lead: Lead,
    *,
    secret_key: str,
    build_cta_url: Callable[[], str],
    build_unsubscribe_url: Callable[[str], str],
    now=None,
) -> str:
    """
    Envia E-mail 1 no máximo uma vez.
    Grava activation_email_1_sent_at somente após sucesso do sender.
    Residual: provider success + DB commit failure (retry pode reenviar).
    """
    if is_lead_email_minimized(lead):
        return _STATUS_SKIPPED_LEAD_EMAIL_MINIMIZED
    if not should_send_activation_email_1(lead, now=now):
        if is_activation_journey_unavailable(lead):
            return _skip_reason_when_unavailable(lead)
        if lead.converted_user_id and has_first_upload(int(lead.converted_user_id)):
            return _STATUS_SKIPPED_UPLOAD
        return _STATUS_SKIPPED

    skip = _recheck_before_send(lead, which="email1", now=now)
    if skip is not None:
        return skip

    try:
        if is_lead_email_minimized(lead):
            return _STATUS_SKIPPED_LEAD_EMAIL_MINIMIZED
        unsub_token = generate_activation_unsubscribe_token(lead.id, secret_key=secret_key)
        unsubscribe_url = build_unsubscribe_url(unsub_token)
        cta_url = build_cta_url()
        built = build_activation_email_1(cta_url=cta_url, unsubscribe_url=unsubscribe_url)
        send_email(
            to_email=lead.email,
            subject=built["subject"],
            html=built["html"],
            text=built["text"],
            attachments=built.get("attachments") or None,
            headers=_list_unsubscribe_headers(unsubscribe_url),
        )
    except Exception:
        logger.exception(
            "Falha ao enviar activation email1 desktop_access: lead_id=%s",
            lead.id,
        )
        return _STATUS_FAILED

    sent_at = now if now is not None else utcnow_naive()
    lead.activation_email_1_sent_at = sent_at
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception(
            "Activation email1 enviado mas falha ao gravar timestamp: lead_id=%s",
            lead.id,
        )
        return _STATUS_FAILED

    logger.info("Activation email1 enviado: lead_id=%s", lead.id)
    return _STATUS_SENT


def maybe_send_activation_email_2(
    lead: Lead,
    *,
    secret_key: str,
    build_cta_url: Callable[[], str],
    build_unsubscribe_url: Callable[[str], str],
    now=None,
) -> str:
    """
    Envia E-mail 2 no máximo uma vez (somente após E1).
    Grava activation_email_2_sent_at somente após sucesso do sender.
    """
    if is_lead_email_minimized(lead):
        return _STATUS_SKIPPED_LEAD_EMAIL_MINIMIZED
    if not should_send_activation_email_2(lead, now=now):
        if is_activation_journey_unavailable(lead):
            return _skip_reason_when_unavailable(lead)
        if lead.converted_user_id and has_first_upload(int(lead.converted_user_id)):
            return _STATUS_SKIPPED_UPLOAD
        return _STATUS_SKIPPED

    skip = _recheck_before_send(lead, which="email2", now=now)
    if skip is not None:
        return skip

    try:
        if is_lead_email_minimized(lead):
            return _STATUS_SKIPPED_LEAD_EMAIL_MINIMIZED
        unsub_token = generate_activation_unsubscribe_token(lead.id, secret_key=secret_key)
        unsubscribe_url = build_unsubscribe_url(unsub_token)
        cta_url = build_cta_url()
        built = build_activation_email_2(cta_url=cta_url, unsubscribe_url=unsubscribe_url)
        send_email(
            to_email=lead.email,
            subject=built["subject"],
            html=built["html"],
            text=built["text"],
            attachments=built.get("attachments") or None,
            headers=_list_unsubscribe_headers(unsubscribe_url),
        )
    except Exception:
        logger.exception(
            "Falha ao enviar activation email2 desktop_access: lead_id=%s",
            lead.id,
        )
        return _STATUS_FAILED

    sent_at = now if now is not None else utcnow_naive()
    lead.activation_email_2_sent_at = sent_at
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception(
            "Activation email2 enviado mas falha ao gravar timestamp: lead_id=%s",
            lead.id,
        )
        return _STATUS_FAILED

    logger.info("Activation email2 enviado: lead_id=%s", lead.id)
    return _STATUS_SENT


def process_eligible_activation_emails(
    *,
    secret_key: str,
    build_email_1_cta_url: Callable[[], str],
    build_email_2_cta_url: Callable[[], str],
    build_unsubscribe_url: Callable[[str], str],
    now=None,
) -> dict[str, Any]:
    """
    Processa E-mail 1 e E-mail 2 com recheck imediato antes de cada envio.
    Logs apenas agregados.
    """
    stats = {
        "examined": 0,
        "email1_candidates": 0,
        "email1_sent": 0,
        "email2_candidates": 0,
        "email2_sent": 0,
        "suppressed_upload": 0,
        "suppressed_opt_out": 0,
        "skipped_suppression_unavailable": 0,
        "skipped_lead_email_minimized": 0,
        "failures": 0,
    }

    email1_candidates = list_activation_email_1_candidates(now=now)
    stats["email1_candidates"] = len(email1_candidates)
    stats["examined"] += len(email1_candidates)

    for lead in email1_candidates:
        try:
            status = maybe_send_activation_email_1(
                lead,
                secret_key=secret_key,
                build_cta_url=build_email_1_cta_url,
                build_unsubscribe_url=build_unsubscribe_url,
                now=now,
            )
        except Exception:
            db.session.rollback()
            logger.exception(
                "Erro inesperado activation email1: lead_id=%s", lead.id
            )
            stats["failures"] += 1
            continue
        if status == _STATUS_SENT:
            stats["email1_sent"] += 1
        elif status == _STATUS_SKIPPED_UPLOAD:
            stats["suppressed_upload"] += 1
        elif status == _STATUS_SKIPPED_OPT_OUT:
            stats["suppressed_opt_out"] += 1
        elif status == _STATUS_SKIPPED_SUPPRESSION_UNAVAILABLE:
            stats["skipped_suppression_unavailable"] += 1
        elif status == _STATUS_SKIPPED_LEAD_EMAIL_MINIMIZED:
            stats["skipped_lead_email_minimized"] += 1
        elif status == _STATUS_FAILED:
            stats["failures"] += 1

    email2_candidates = list_activation_email_2_candidates(now=now)
    stats["email2_candidates"] = len(email2_candidates)
    stats["examined"] += len(email2_candidates)

    for lead in email2_candidates:
        try:
            status = maybe_send_activation_email_2(
                lead,
                secret_key=secret_key,
                build_cta_url=build_email_2_cta_url,
                build_unsubscribe_url=build_unsubscribe_url,
                now=now,
            )
        except Exception:
            db.session.rollback()
            logger.exception(
                "Erro inesperado activation email2: lead_id=%s", lead.id
            )
            stats["failures"] += 1
            continue
        if status == _STATUS_SENT:
            stats["email2_sent"] += 1
        elif status == _STATUS_SKIPPED_UPLOAD:
            stats["suppressed_upload"] += 1
        elif status == _STATUS_SKIPPED_OPT_OUT:
            stats["suppressed_opt_out"] += 1
        elif status == _STATUS_SKIPPED_SUPPRESSION_UNAVAILABLE:
            stats["skipped_suppression_unavailable"] += 1
        elif status == _STATUS_SKIPPED_LEAD_EMAIL_MINIMIZED:
            stats["skipped_lead_email_minimized"] += 1
        elif status == _STATUS_FAILED:
            stats["failures"] += 1

    logger.info(
        "Activation desktop_access: examined=%s email1_candidates=%s email1_sent=%s "
        "email2_candidates=%s email2_sent=%s suppressed_upload=%s "
        "suppressed_opt_out=%s skipped_suppression_unavailable=%s failures=%s",
        stats["examined"],
        stats["email1_candidates"],
        stats["email1_sent"],
        stats["email2_candidates"],
        stats["email2_sent"],
        stats["suppressed_upload"],
        stats["suppressed_opt_out"],
        stats["skipped_suppression_unavailable"],
        stats["failures"],
    )
    return stats
