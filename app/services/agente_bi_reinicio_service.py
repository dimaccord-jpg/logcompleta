"""Reinício físico do AgenteBI no escopo autenticado.

Apaga, numa única transação, o conteúdo operacional do usuário, da conta
e da franquia já resolvidos no servidor. Não chama serviços que gravam
por conta própria e não mexe em saldo, tentativa ou permissão.

Uma requisição que tenha começado antes — em especial um upload que só
grava ao final — pode inserir conteúdo novo depois que esta transação
termina. Este serviço não cria trava adicional para esse intervalo.
"""
from __future__ import annotations

import logging

from sqlalchemy import delete, insert, select

from app.extensions import db
from app.models import (
    AgenteBiAnalise,
    AgenteBiAplicacao,
    AgenteBiDashboard,
    AgenteBiDashboardDataset,
    AgenteBiDataset,
    AgenteBiDatasetBloco,
    AgenteBiExecucao,
    AgenteBiExecucaoEvento,
    AgenteBiReinicioAutorizacao,
    utcnow_naive,
)
from app.services.agente_bi_persistencia_service import AgenteBiErro, EscopoAgenteBi

logger = logging.getLogger(__name__)


def reiniciar(escopo: EscopoAgenteBi, *, ponto_de_controle=None) -> None:
    """Esvazia o BI do escopo. A segunda chamada, já vazio, também sucede."""
    if not isinstance(escopo, EscopoAgenteBi):
        raise AgenteBiErro("identidade_incompleta", 403)
    try:
        _autorizar(escopo)
        _reservar_linhas(escopo)
        if _lease_vigente(escopo):
            raise AgenteBiErro("execucao_em_andamento", 409)
        _excluir(escopo, ponto_de_controle)
        _revogar(escopo)
        db.session.expunge_all()
        db.session.commit()
    except AgenteBiErro:
        db.session.rollback()
        raise
    except Exception:
        db.session.rollback()
        logger.exception("evento=agente_bi_reinicio_falhou usuario_id=%s", escopo.usuario_id)
        raise AgenteBiErro("reinicio_indisponivel", 500) from None
    logger.info("evento=agente_bi_reinicio usuario_id=%s", escopo.usuario_id)


def _autorizar(escopo: EscopoAgenteBi) -> None:
    db.session.execute(
        insert(AgenteBiReinicioAutorizacao.__table__).values(
            usuario_id=escopo.usuario_id,
            conta_id=escopo.conta_id,
            franquia_id=escopo.franquia_id,
        )
    )


def _revogar(escopo: EscopoAgenteBi) -> None:
    tabela = AgenteBiReinicioAutorizacao.__table__
    db.session.execute(delete(tabela).where(*_filtro(tabela, escopo)))


def _reservar_linhas(escopo: EscopoAgenteBi) -> None:
    """Segura as linhas do escopo até o fim da transação no PostgreSQL."""
    if db.session.get_bind().dialect.name != "postgresql":
        return
    for tabela in _TABELAS_ESCOPO:
        db.session.execute(select(tabela.c.id).where(*_filtro(tabela, escopo)).with_for_update())


def _lease_vigente(escopo: EscopoAgenteBi) -> bool:
    tabela = AgenteBiExecucao.__table__
    agora = utcnow_naive()
    linha = db.session.execute(
        select(tabela.c.id)
        .where(
            *_filtro(tabela, escopo),
            tabela.c.estado == "adquirida",
            tabela.c.lease_ate.is_not(None),
            tabela.c.lease_ate > agora,
        )
        .limit(1)
    ).first()
    return linha is not None


def _excluir(escopo: EscopoAgenteBi, ponto_de_controle) -> None:
    etapas = (
        ("eventos", _excluir_eventos),
        ("execucoes", _excluir_tabela(AgenteBiExecucao.__table__)),
        ("aplicacoes", _excluir_tabela(AgenteBiAplicacao.__table__)),
        ("vinculos", _excluir_tabela(AgenteBiDashboardDataset.__table__)),
        ("dashboards", _excluir_tabela(AgenteBiDashboard.__table__)),
        ("analises", _excluir_tabela(AgenteBiAnalise.__table__)),
        ("blocos", _excluir_tabela(AgenteBiDatasetBloco.__table__)),
        ("datasets", _excluir_datasets),
    )
    for nome, acao in etapas:
        acao(escopo)
        if ponto_de_controle is not None:
            ponto_de_controle(nome)


def _excluir_eventos(escopo: EscopoAgenteBi) -> None:
    execucoes = AgenteBiExecucao.__table__
    eventos = AgenteBiExecucaoEvento.__table__
    ids = select(execucoes.c.id).where(*_filtro(execucoes, escopo))
    db.session.execute(delete(eventos).where(eventos.c.execucao_id.in_(ids)))


def _excluir_tabela(tabela):
    def _acao(escopo: EscopoAgenteBi) -> None:
        db.session.execute(delete(tabela).where(*_filtro(tabela, escopo)))

    return _acao


def _excluir_datasets(escopo: EscopoAgenteBi) -> None:
    tabela = AgenteBiDataset.__table__
    ids = db.session.execute(
        select(tabela.c.id)
        .where(*_filtro(tabela, escopo))
        .order_by(tabela.c.versao.desc(), tabela.c.id.desc())
    ).scalars().all()
    for dataset_id in ids:
        db.session.execute(
            delete(tabela).where(tabela.c.id == int(dataset_id), *_filtro(tabela, escopo))
        )


def _filtro(tabela, escopo: EscopoAgenteBi):
    return (
        tabela.c.usuario_id == escopo.usuario_id,
        tabela.c.conta_id == escopo.conta_id,
        tabela.c.franquia_id == escopo.franquia_id,
    )


_TABELAS_ESCOPO = (
    AgenteBiExecucao.__table__,
    AgenteBiAplicacao.__table__,
    AgenteBiDashboardDataset.__table__,
    AgenteBiDashboard.__table__,
    AgenteBiAnalise.__table__,
    AgenteBiDatasetBloco.__table__,
    AgenteBiDataset.__table__,
)
