"""
Lifecycle operacional do User.

Este módulo trata o encerramento do vínculo operacional de um User.
Não trata cancelamento de assinatura, downgrade, billing Stripe,
nem exercício de direitos/eliminação LGPD.

Distinções explícitas:
- encerrar vínculo operacional != cancelar assinatura
- encerrar vínculo operacional != exclusão LGPD
- anonimizar perfil operacional != apagar histórico empresarial/financeiro
- encerrar jornada de ativação do User X != opt-out / CommunicationSuppression
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.extensions import db
from app.models import Lead, User, utcnow_naive
from app.services.user_operational_state import (
    email_operacional_apos_encerramento,
    is_user_operationally_closed,
    normalize_email as _normalize_email,
)

logger = logging.getLogger(__name__)

NOME_OPERACIONAL_APOS_ENCERRAMENTO = "Conta encerrada"


@dataclass(frozen=True)
class ResultadoEncerramentoContratual:
    """Resultado do encerramento do vínculo operacional. Não representa exclusão LGPD."""

    sucesso: bool
    mensagem: str | None = None


def _jornada_ativacao_ja_encerrada_para_usuario(lead: Lead, user_id: int) -> bool:
    """First-write-wins: jornada já marcada para este user_id."""
    return (
        lead.activation_ended_at is not None
        and lead.activation_ended_for_user_id is not None
        and int(lead.activation_ended_for_user_id) == int(user_id)
    )


def _listar_leads_jornada_ativacao(user: User) -> list[Lead]:
    """Leads cuja jornada de ativação está ligada a este User. Sem mutação."""
    return Lead.query.filter(Lead.converted_user_id == int(user.id)).all()


def _encerrar_jornadas_ativacao_associadas(user: User) -> int:
    """
    Marca o fim operacional da jornada de ativação dos Leads deste User.

    First-write-wins para a mesma jornada (mesmo user_id).
    Não preenche opt_out_at / activation_opt_out_at.
    Não cria CommunicationSuppression.
    Não persiste: o caller transacional controla o commit.
    Ausência de Lead não é erro.
    Retorna quantas jornadas foram marcadas nesta chamada.
    """
    uid = int(user.id)
    leads = _listar_leads_jornada_ativacao(user)
    if not leads:
        return 0
    now = utcnow_naive()
    ended = 0
    for lead in leads:
        if _jornada_ativacao_ja_encerrada_para_usuario(lead, uid):
            continue
        lead.activation_ended_at = now
        lead.activation_ended_for_user_id = uid
        ended += 1
    return ended


def _desidentificar_usuario(user: User) -> User:
    """
    Remove atributos de perfil/identidade do User. Sem persistência.

    Reutilizado pelo encerramento operacional e pelo exercício de privacidade.
    Não apaga o row nem altera Conta/Franquia/billing/eventos/leads.
    """
    user.email = email_operacional_apos_encerramento(user.id)
    user.full_name = NOME_OPERACIONAL_APOS_ENCERRAMENTO
    user.password_hash = None
    user.oauth_provider = None
    user.oauth_sub = None
    user.subscribes_to_newsletter = False
    user.job_role = None
    user.usage_purpose = None
    return user


def anonimizar_perfil_operacional_para_encerramento(user: User) -> User:
    """
    Remove identidade operacional do User para encerramento contratual.

    Não apaga o row, não altera Conta/Franquia, não toca billing, eventos,
    leads nem evidência de aceite (`accepted_terms_at`).
    Não persiste: o caller transacional é encerrar_vinculo_operacional_usuario.
    """
    return _desidentificar_usuario(user)


def encerrar_vinculo_operacional_usuario(user: User | None) -> ResultadoEncerramentoContratual:
    """
    Encerra o vínculo operacional daquele User.

    Preserva o registro para integridade referencial (Conta, Franquia, FKs,
    histórico). Não faz logout nem limpa sessão: isso permanece na rota.

    Na mesma transação, encerra a jornada de ativação associada a este
    user_id. Isso não é opt-out nem suppression.
    """
    if not isinstance(user, User) or getattr(user, "id", None) is None:
        return ResultadoEncerramentoContratual(
            sucesso=False,
            mensagem="Usuário inválido para encerramento contratual.",
        )

    uid = user.id
    try:
        from app.services.newsletter_subscription_service import (
            unsubscribe_for_user_before_deidentify,
        )

        unsubscribe_for_user_before_deidentify(user, commit=False)
        _encerrar_jornadas_ativacao_associadas(user)
        anonimizar_perfil_operacional_para_encerramento(user)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    logger.info(
        "Vínculo operacional encerrado e perfil operacional anonimizado para user id=%s",
        uid,
    )
    return ResultadoEncerramentoContratual(sucesso=True)
