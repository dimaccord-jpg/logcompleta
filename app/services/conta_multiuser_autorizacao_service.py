"""
Autorização Multiuser V1 (Fase 8).

Centraliza checagens já existentes de Contratante/Membro/admin/ownership.
Não cria RBAC novo. Não amplia acesso por conta_id.
"""
from __future__ import annotations

import logging

from app.extensions import db
from app.models import ContaVinculoOrganizacional, User
from app.services.conta_multiuser_errors import GestaoMultiuserNaoAutorizadaError
from app.services.conta_organizacional_rules import ESTADO_ATIVO, PAPEL_CONTRATANTE, PAPEL_MEMBRO

logger = logging.getLogger(__name__)


def vinculo_ativo_do_user(user_id: int | None) -> ContaVinculoOrganizacional | None:
    if user_id is None:
        return None
    return (
        ContaVinculoOrganizacional.query.filter_by(
            user_id=int(user_id),
            estado=ESTADO_ATIVO,
        )
        .order_by(ContaVinculoOrganizacional.id.asc())
        .first()
    )


def carregar_ator(actor_id: int) -> User | None:
    return db.session.get(User, int(actor_id))


def user_eh_admin_plataforma(user: User | None) -> bool:
    return user is not None and bool(getattr(user, "is_admin", False))


def user_eh_contratante_ativo(user: User | None) -> bool:
    if user is None or not getattr(user, "id", None):
        return False
    vinculo = vinculo_ativo_do_user(int(user.id))
    return vinculo is not None and (vinculo.papel or "") == PAPEL_CONTRATANTE


def user_eh_membro_ativo(user: User | None) -> bool:
    if user is None or not getattr(user, "id", None):
        return False
    vinculo = vinculo_ativo_do_user(int(user.id))
    return vinculo is not None and (vinculo.papel or "") == PAPEL_MEMBRO


def mesma_conta_operacional(user_a: User | None, user_b: User | None) -> bool:
    if user_a is None or user_b is None:
        return False
    conta_a = getattr(user_a, "conta_id", None)
    conta_b = getattr(user_b, "conta_id", None)
    if conta_a is None or conta_b is None:
        return False
    return int(conta_a) == int(conta_b)


def mesmo_owner_operacional(
    ator: User | None,
    *,
    usuario_id: int | None,
    conta_id: int | None = None,
    franquia_id: int | None = None,
) -> bool:
    """Ownership operacional exige o User dono. Mesma Conta não autoriza."""
    if ator is None or usuario_id is None:
        return False
    if int(ator.id) != int(usuario_id):
        return False
    if conta_id is not None and int(getattr(ator, "conta_id", 0) or 0) != int(conta_id):
        return False
    if franquia_id is not None and int(getattr(ator, "franquia_id", 0) or 0) != int(franquia_id):
        return False
    return True


def exigir_contratante_ativo(
    user: User | None,
    *,
    erro_cls=GestaoMultiuserNaoAutorizadaError,
    mensagem: str = "Somente o Contratante ativo da Conta pode gerenciar assentos.",
    evento_log: str = "gestao_multiuser_bloqueada",
) -> ContaVinculoOrganizacional:
    """Autoridade: vínculo ativo papel=contratante. Não usa categoria/is_admin."""
    user_id = getattr(user, "id", None) if user is not None else None
    vinculo = vinculo_ativo_do_user(user_id)
    if vinculo is None or (vinculo.papel or "") != PAPEL_CONTRATANTE:
        logger.info(
            "evento=%s motivo=nao_contratante user_id=%s",
            evento_log,
            user_id,
        )
        raise erro_cls(mensagem)
    return vinculo


def exigir_contratante_ativo_por_id(
    actor_id: int,
    *,
    erro_cls=GestaoMultiuserNaoAutorizadaError,
    mensagem: str = "Somente o Contratante ativo da Conta pode gerenciar assentos.",
    evento_log: str = "gestao_multiuser_bloqueada",
) -> ContaVinculoOrganizacional:
    ator = carregar_ator(int(actor_id))
    if ator is None:
        raise erro_cls(mensagem)
    return exigir_contratante_ativo(
        ator,
        erro_cls=erro_cls,
        mensagem=mensagem,
        evento_log=evento_log,
    )


def exigir_admin_plataforma(
    user: User | None,
    *,
    erro_cls=GestaoMultiuserNaoAutorizadaError,
    mensagem: str = "Somente o administrador da plataforma pode executar esta ação.",
) -> User:
    if not user_eh_admin_plataforma(user):
        raise erro_cls(mensagem)
    return user
