"""
Reconciliação F1→F2: backfill legado reexecutável antes do enforcement.

Fecha a janela H-03: registros Multiuser criados após a migration da Fase 1
são capturados por nova execução determinística antes dos writes governados.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.extensions import db
from app.models import Conta, ContaOrganizacionalBackfillInconsistencia
from app.services.conta_multiuser_ciclo_service import alinhar_franquias_sem_ciclo_ao_canonico
from app.services.conta_organizacional_backfill_service import (
    InspecaoSchemaInconclusivaError,
    RelatorioBackfillMultiuser,
    aplicar_backfill_multiuser_legado,
)
from app.services.conta_organizacional_rules import SLUG_CONTA_SISTEMA

logger = logging.getLogger(__name__)


@dataclass
class RelatorioReconciliacaoFase2:
    contas_analisadas: int = 0
    contas_reconciliadas: int = 0
    contas_ambiguas: int = 0
    contas_divergentes: int = 0
    divergencias_detectadas: int = 0
    franquias_ciclo_alinhadas: int = 0
    backfill: RelatorioBackfillMultiuser = field(default_factory=RelatorioBackfillMultiuser)


def _contas_multiuser_alvo() -> list[Conta]:
    return (
        Conta.query.filter(Conta.multiuser_ativa.is_(True))
        .filter(Conta.slug != SLUG_CONTA_SISTEMA)
        .order_by(Conta.id.asc())
        .all()
    )


def executar_reconciliacao_multiuser_antes_enforcement(
    *,
    commit: bool = True,
) -> RelatorioReconciliacaoFase2:
    """
    1. Reexecuta o backfill determinístico da Fase 1.
    2. Alinha ciclo nulo somente quando o período canônico é determinável.
    3. Detecta divergências sem corrigi-las silenciosamente.
    Idempotente: não duplica vínculo nem inconsistência; não sobrescreve quantity governada.
    """
    from app.services.conta_multiuser_capacidade_service import listar_divergencias_conta

    connection = db.session.connection()
    try:
        backfill = aplicar_backfill_multiuser_legado(connection)
        db.session.flush()
        db.session.expire_all()
    except InspecaoSchemaInconclusivaError:
        db.session.rollback()
        raise
    except Exception:
        db.session.rollback()
        raise

    rel = RelatorioReconciliacaoFase2(backfill=backfill)
    contas = _contas_multiuser_alvo()
    rel.contas_analisadas = len(contas)

    inconsistencias = (
        db.session.query(ContaOrganizacionalBackfillInconsistencia.conta_id)
        .filter(ContaOrganizacionalBackfillInconsistencia.conta_id.isnot(None))
        .distinct()
        .all()
    )
    contas_ambiguas = {int(r[0]) for r in inconsistencias if r[0] is not None}

    for conta in contas:
        cid = int(conta.id)
        alinhadas = alinhar_franquias_sem_ciclo_ao_canonico(cid)
        rel.franquias_ciclo_alinhadas += alinhadas
        divs = listar_divergencias_conta(cid)
        ambigua = cid in contas_ambiguas
        divergente = bool(divs)
        if ambigua:
            rel.contas_ambiguas += 1
        if divergente:
            rel.contas_divergentes += 1
            rel.divergencias_detectadas += len(divs)
            logger.info(
                "Reconciliacao Multiuser divergente conta_id=%s tipos=%s",
                cid,
                ",".join(sorted({d.codigo for d in divs})),
            )
        if not ambigua and not divergente:
            rel.contas_reconciliadas += 1

    logger.info(
        "Reconciliacao Multiuser F1-F2: analisadas=%s reconciliadas=%s ambiguas=%s divergentes=%s "
        "vinculos_backfill=%s inconsistencias_backfill=%s ciclo_alinhado=%s",
        rel.contas_analisadas,
        rel.contas_reconciliadas,
        rel.contas_ambiguas,
        rel.contas_divergentes,
        backfill.vinculos_criados,
        backfill.inconsistencias,
        rel.franquias_ciclo_alinhadas,
    )
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return rel
