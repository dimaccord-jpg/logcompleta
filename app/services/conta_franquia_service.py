"""
Vínculo de negócio: Conta (raiz contratual) e Franquia (unidade operacional de consumo).
Sem billing; apenas identidade e helpers para runtime e bootstrap.
"""
from __future__ import annotations

import logging
import re
import uuid
from typing import Optional, Tuple

from app.extensions import db
from app.models import Conta, Franquia, User

logger = logging.getLogger(__name__)

_SLUG_SAFE = re.compile(r"[^a-z0-9\-]+")

# Cache processo-local para IDs da Conta/Franquia sistema (evita query repetida).
_sistema_ids_cache: Optional[Tuple[Optional[int], Optional[int]]] = None


def _slugify_local(s: str, fallback: str) -> str:
    x = (s or "").strip().lower()
    x = _SLUG_SAFE.sub("-", x).strip("-")[:72]
    return x or fallback


def get_sistema_interno_ids() -> Tuple[Optional[int], Optional[int]]:
    """
    Conta/Franquia reservadas para consumo sistema/cron/CLI (sem operador humano).
    Cache em processo após primeira resolução bem-sucedida.
    """
    global _sistema_ids_cache
    if _sistema_ids_cache is not None:
        return _sistema_ids_cache
    c = Conta.query.filter_by(slug=Conta.SLUG_SISTEMA).first()
    if not c:
        logger.error(
            "Conta sistema interna (slug=%r) não encontrada; rode a migration fase2 etapa2.",
            Conta.SLUG_SISTEMA,
        )
        _sistema_ids_cache = (None, None)
        return _sistema_ids_cache
    f = (
        Franquia.query.filter_by(conta_id=c.id, slug=Franquia.SLUG_SISTEMA_OPERACIONAL)
        .first()
    )
    if not f:
        logger.error(
            "Franquia sistema interna não encontrada para conta id=%s; rode a migration.",
            c.id,
        )
        _sistema_ids_cache = (None, None)
        return _sistema_ids_cache
    _sistema_ids_cache = (c.id, f.id)
    return _sistema_ids_cache


def criar_conta_franquia_para_cadastro(email: str, nome_exibicao: str) -> Tuple[Conta, Franquia]:
    """
    Cria Conta + Franquia principal antes de inserir User (colunas NOT NULL no banco).
    Slug único por tentativa de cadastro.
    """
    slug_base = _slugify_local(email, "conta") + "-" + uuid.uuid4().hex[:10]
    slug_conta = f"conta-{slug_base}"[:80]
    if Conta.query.filter_by(slug=slug_conta).first():
        slug_conta = f"conta-{uuid.uuid4().hex}"[:80]
    nome = (nome_exibicao or email or "Conta")[:255]
    return criar_conta_e_franquia_operacional(
        nome_conta=nome,
        slug_conta=slug_conta,
        nome_franquia="Principal",
        slug_franquia="principal",
    )


def criar_conta_e_franquia_operacional(
    *,
    nome_conta: str,
    slug_conta: str,
    nome_franquia: str = "Principal",
    slug_franquia: str = "principal",
) -> Tuple[Conta, Franquia]:
    """Cria par conta + franquia default (uma unidade operacional)."""
    conta = Conta(
        nome=(nome_conta or "Conta")[:255],
        slug=slug_conta[:80],
        status=Conta.STATUS_ATIVA,
    )
    db.session.add(conta)
    db.session.flush()
    franquia = Franquia(
        conta_id=conta.id,
        nome=(nome_franquia or "Principal")[:255],
        slug=slug_franquia[:80],
        status=Franquia.STATUS_ACTIVE,
    )
    db.session.add(franquia)
    db.session.flush()
    return conta, franquia


def vincular_usuario_a_conta_nova(user: User, email_para_slug: str) -> None:
    """Associa usuário a uma Conta/Franquia recém-criada (usuário já persistido com id)."""
    slug_base = _slugify_local(email_para_slug, f"u{user.id}")
    slug_conta = f"conta-{slug_base}"[:80]
    # unicidade: se colidir, acrescenta id
    if Conta.query.filter_by(slug=slug_conta).first():
        slug_conta = f"conta-{user.id}-{slug_base}"[:80]
    nome = (user.full_name or user.email or f"Conta {user.id}")[:255]
    conta, franquia = criar_conta_e_franquia_operacional(
        nome_conta=nome,
        slug_conta=slug_conta,
        nome_franquia="Principal",
        slug_franquia="principal",
    )
    user.conta_id = conta.id
    user.franquia_id = franquia.id


def ensure_user_vinculo_conta_franquia(user: User) -> None:
    """
    Garante user.conta_id e user.franquia_id para identidade de consumo.
    Uso: request autenticado; evita commit explícito — flush + commit ao fim do request.
    """
    if user.conta_id and user.franquia_id:
        return
    try:
        db.session.refresh(user)
    except Exception:
        pass
    if user.conta_id and user.franquia_id:
        return
    logger.warning(
        "Usuário id=%s sem vínculo Conta/Franquia; criando vínculo operacional.",
        user.id,
    )
    vincular_usuario_a_conta_nova(user, user.email or "")
    try:
        db.session.flush()
    except Exception as e:
        logger.exception("Falha ao criar vínculo conta/franquia para user id=%s: %s", user.id, e)
        raise
