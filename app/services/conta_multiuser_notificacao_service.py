"""Notificação interna mínima V1 (Fase 7). Persistência privada e idempotente."""
from __future__ import annotations

import html
import logging
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import NotificacaoInterna, User, utcnow_naive
from app.services.conta_multiuser_errors import NotificacaoInternaNaoAutorizadaError

logger = logging.getLogger(__name__)

CTA_INTERNOS_VALIDOS = frozenset(
    {
        "user.perfil",
        "user.contrate_plano",
        "multiuser_painel.gestao_multiuser",
        "admin.controle_usuarios",
    }
)

TIPOS_V1 = frozenset(NotificacaoInterna.TIPOS_V1)


@dataclass(frozen=True)
class NotificacaoLeitura:
    id: int
    tipo: str
    mensagem: str
    cta_interno: str | None
    cta_url: str | None
    referencia_dominio: str | None
    created_at: str
    lida: bool


def _validar_cta(cta_interno: str | None) -> str | None:
    if cta_interno is None or not str(cta_interno).strip():
        return None
    chave = str(cta_interno).strip()
    if chave not in CTA_INTERNOS_VALIDOS:
        raise ValueError("CTA interno inválido.")
    return chave


def _resolver_cta_url(cta_interno: str | None) -> str | None:
    if not cta_interno:
        return None
    from flask import has_app_context, url_for

    if not has_app_context():
        return None
    try:
        return url_for(cta_interno)
    except Exception:
        logger.info("evento=notificacao_cta_nao_resolvido cta=%s", cta_interno)
        return None


def criar_notificacao(
    *,
    user_id: int,
    conta_id: int | None,
    tipo: str,
    mensagem: str,
    dedup_key: str,
    cta_interno: str | None = None,
    referencia_dominio: str | None = None,
    commit: bool = False,
) -> NotificacaoInterna:
    tipo_n = (tipo or "").strip()
    if tipo_n not in TIPOS_V1:
        raise ValueError("Tipo de notificação interna não suportado.")
    mensagem_n = (mensagem or "").strip()[:500]
    if not mensagem_n:
        raise ValueError("Mensagem de notificação obrigatória.")
    dedup = (dedup_key or "").strip()[:200]
    if not dedup:
        raise ValueError("Chave de deduplicação obrigatória.")
    cta = _validar_cta(cta_interno)
    existente = (
        NotificacaoInterna.query.filter_by(user_id=int(user_id), dedup_key=dedup)
        .order_by(NotificacaoInterna.id.asc())
        .first()
    )
    if existente is not None:
        return existente
    row = NotificacaoInterna(
        user_id=int(user_id),
        conta_id=int(conta_id) if conta_id is not None else None,
        tipo=tipo_n,
        mensagem=mensagem_n,
        cta_interno=cta,
        referencia_dominio=(referencia_dominio or "").strip()[:120] or None,
        dedup_key=dedup,
        created_at=utcnow_naive(),
    )
    try:
        with db.session.begin_nested():
            db.session.add(row)
            db.session.flush()
    except IntegrityError:
        existente = (
            NotificacaoInterna.query.filter_by(user_id=int(user_id), dedup_key=dedup)
            .order_by(NotificacaoInterna.id.asc())
            .first()
        )
        if existente is not None:
            return existente
        raise
    if commit:
        db.session.commit()
    return row


def tentar_enviar_email_notificacao(
    user: User | None,
    assunto: str,
    texto: str,
    cta_label: str | None = None,
    cta_url: str | None = None,
) -> None:
    """Projeção. Falha de e-mail nunca desfaz o evento de domínio."""
    if user is None or not (user.email or "").strip():
        return
    try:
        from app.auth_services import send_email

        label = (cta_label or "").strip()
        url = (cta_url or "").strip()
        if label and url:
            html_body = (
                f"<p>{html.escape(texto)}</p>"
                f'<p><a href="{html.escape(url, quote=True)}">{html.escape(label)}</a></p>'
            )
            text_body = f"{texto}\n\n{label}: {url}"
        else:
            html_body = f"<p>{texto}</p>"
            text_body = texto

        send_email(
            to_email=user.email,
            subject=assunto,
            html=html_body,
            text=text_body,
        )
    except Exception:
        logger.exception(
            "evento=notificacao_email_falhou user_id=%s (evento de dominio preservado)",
            getattr(user, "id", None),
        )


def listar_notificacoes_do_user(user: User, *, limite: int = 30) -> list[NotificacaoLeitura]:
    rows = (
        NotificacaoInterna.query.filter_by(user_id=int(user.id))
        .order_by(NotificacaoInterna.created_at.desc(), NotificacaoInterna.id.desc())
        .limit(int(limite))
        .all()
    )
    out: list[NotificacaoLeitura] = []
    for row in rows:
        out.append(
            NotificacaoLeitura(
                id=int(row.id),
                tipo=row.tipo,
                mensagem=row.mensagem,
                cta_interno=row.cta_interno,
                cta_url=_resolver_cta_url(row.cta_interno),
                referencia_dominio=row.referencia_dominio,
                created_at=row.created_at.isoformat() if row.created_at else "",
                lida=row.read_at is not None,
            )
        )
    return out


def contar_nao_lidas(user: User | None) -> int:
    if user is None or not getattr(user, "id", None):
        return 0
    return (
        NotificacaoInterna.query.filter_by(user_id=int(user.id))
        .filter(NotificacaoInterna.read_at.is_(None))
        .count()
    )


def marcar_como_lida(user: User, notificacao_id: int, *, commit: bool = True) -> NotificacaoInterna:
    row = db.session.get(NotificacaoInterna, int(notificacao_id))
    if row is None or int(row.user_id) != int(user.id):
        raise NotificacaoInternaNaoAutorizadaError(
            "Notificação não encontrada."
        )
    if row.read_at is None:
        row.read_at = utcnow_naive()
        db.session.add(row)
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return row


def notificacao_para_api(item: NotificacaoLeitura) -> dict:
    return {
        "id": item.id,
        "tipo": item.tipo,
        "mensagem": item.mensagem,
        "cta_interno": item.cta_interno,
        "cta_url": item.cta_url,
        "referencia_dominio": item.referencia_dominio,
        "created_at": item.created_at,
        "lida": item.lida,
    }
