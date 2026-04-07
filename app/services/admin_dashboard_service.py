"""
Métricas reais do dashboard administrativo (usuários, leads, filtros).
Alinhado a auth_services.encerrar_contrato para usuários cancelados (email anonimizado).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import not_

from app.extensions import db
from app.models import Franquia, Lead, User

# Padrão de email após encerramento (auth_services.encerrar_contrato).
CANCELADO_EMAIL_LIKE = "encerrado%@anon.local"


def _base_users_join_franquia():
    return User.query.join(Franquia, User.franquia_id == Franquia.id)


def _apply_dashboard_filters(q, *, categoria: str | None, franquia_status: str | None, cancelado: str | None):
    """
    Filtros combináveis.
    :param categoria: valor exato de User.categoria ou None (todos).
    :param franquia_status: valor exato de Franquia.status ou None (todos).
    :param cancelado: 'todos' | 'ativos' | 'somente_cancelados'.
        - ativos: exclui emails anonimizados (padrão operacional).
        - somente_cancelados: apenas emails que batem com o padrão de cancelamento.
    """
    if categoria:
        q = q.filter(User.categoria == categoria)
    if franquia_status:
        q = q.filter(Franquia.status == franquia_status)
    modo = (cancelado or "ativos").strip().lower()
    if modo == "ativos":
        q = q.filter(not_(User.email.like(CANCELADO_EMAIL_LIKE)))
    elif modo == "somente_cancelados":
        q = q.filter(User.email.like(CANCELADO_EMAIL_LIKE))
    return q


def get_dashboard_metrics(
    *,
    categoria: str | None = None,
    franquia_status: str | None = None,
    cancelado: str | None = None,
) -> dict[str, Any]:
    """Totais de usuários e pagantes conforme filtros; leads sempre total global."""
    q = _base_users_join_franquia()
    q = _apply_dashboard_filters(q, categoria=categoria, franquia_status=franquia_status, cancelado=cancelado)
    total_usuarios = q.count()

    q2 = _base_users_join_franquia()
    q2 = _apply_dashboard_filters(q2, categoria=categoria, franquia_status=franquia_status, cancelado=cancelado)
    q2 = q2.filter(User.categoria != "free")
    total_pagantes = q2.count()

    total_leads = db.session.query(Lead).count()

    return {
        "total_usuarios": total_usuarios,
        "total_usuarios_pagantes": total_pagantes,
        "total_leads": total_leads,
    }


def list_categorias_distintas() -> list[str]:
    rows = (
        db.session.query(User.categoria)
        .filter(User.categoria.isnot(None))
        .distinct()
        .order_by(User.categoria.asc())
        .all()
    )
    return [r[0] for r in rows if r[0]]


def list_franquia_status_distintos() -> list[str]:
    rows = (
        db.session.query(Franquia.status)
        .distinct()
        .order_by(Franquia.status.asc())
        .all()
    )
    return [r[0] for r in rows if r[0]]
