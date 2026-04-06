"""
Domínio Cleiton — reconciliação entre consumo persistido na Franquia e soma histórica abatível
nos eventos técnicos (IaConsumoEvento, ProcessingEvent).

Usa a mesma sequência do motor: `deve_abater_franquia_do_cliente` + conversão com a régua atual
(CleitonCostConfig). Eventos que falhariam conversão no motor não entram no total recalculado.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.extensions import db
from app.models import Franquia, IaConsumoEvento, ProcessingEvent
from app.services.cleiton_cost_service import get_or_create_config
from app.services.cleiton_franquia_operacional_service import (
    Q6,
    creditos_totais_de_evento_ia,
    creditos_totais_de_evento_processing,
    deve_abater_franquia_do_cliente,
    recalcular_status_operacional,
)
TOL = Q6


def _to_decimal(x) -> Decimal:
    if x is None:
        return Decimal("0")
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


@dataclass(frozen=True)
class ResultadoReconciliacaoFranquiaCleiton:
    franquia_id: int
    total_persistido: Decimal
    total_recalculado: Decimal
    diferenca: Decimal
    status: str
    contagem_eventos_ia_abativel: int
    contagem_eventos_processing_abativel: int
    contagem_eventos_ia_excluidos: int
    contagem_eventos_processing_excluidos: int
    correcao_aplicada: bool


def _somar_creditos_abativeis_franquia(franquia_id: int) -> tuple[Decimal, int, int, int, int]:
    fid = int(franquia_id)
    cfg = get_or_create_config()
    total = Decimal("0")
    n_ia_ok = 0
    n_pe_ok = 0
    n_ia_skip = 0
    n_pe_skip = 0

    for ev in (
        IaConsumoEvento.query.filter(IaConsumoEvento.franquia_id == fid)
        .order_by(IaConsumoEvento.id.asc())
        .all()
    ):
        pode, _ = deve_abater_franquia_do_cliente(
            franquia_id=ev.franquia_id,
            usuario_id=ev.usuario_id,
            origem_sistema=ev.origem_sistema,
            tipo_origem=ev.tipo_origem,
        )
        if not pode:
            n_ia_skip += 1
            continue
        c, err = creditos_totais_de_evento_ia(ev, cfg)
        if err:
            n_ia_skip += 1
            continue
        if c is None or c <= 0:
            n_ia_skip += 1
            continue
        total += c
        n_ia_ok += 1

    for ev in (
        ProcessingEvent.query.filter(ProcessingEvent.franquia_id == fid)
        .order_by(ProcessingEvent.id.asc())
        .all()
    ):
        pode, _ = deve_abater_franquia_do_cliente(
            franquia_id=ev.franquia_id,
            usuario_id=ev.usuario_id,
            origem_sistema=ev.origem_sistema,
            tipo_origem=ev.tipo_origem,
        )
        if not pode:
            n_pe_skip += 1
            continue
        c, err = creditos_totais_de_evento_processing(ev, cfg)
        if err:
            n_pe_skip += 1
            continue
        if c is None or c <= 0:
            n_pe_skip += 1
            continue
        total += c
        n_pe_ok += 1

    total = total.quantize(TOL)
    return total, n_ia_ok, n_pe_ok, n_ia_skip, n_pe_skip


def reconciliar_franquia_cleiton(
    franquia_id: int,
    *,
    aplicar_correcao: bool = False,
) -> ResultadoReconciliacaoFranquiaCleiton:
    """
    Compara `Franquia.consumo_acumulado` com a soma recalculada dos eventos abatíveis.

    `aplicar_correcao=False` (padrão): apenas leitura e comparação.
    `aplicar_correcao=True`: grava `consumo_acumulado = total_recalculado` e realinha status operacional.
    """
    fid = int(franquia_id)
    fr = db.session.get(Franquia, fid)
    if fr is None:
        raise ValueError(f"franquia_id={fid} inexistente")

    recalc, n_ia, n_pe, skip_ia, skip_pe = _somar_creditos_abativeis_franquia(fid)
    persistido = _to_decimal(fr.consumo_acumulado)
    diff = (persistido - recalc).quantize(TOL)
    ok = abs(diff) <= TOL
    status = "ok" if ok else "divergente"

    correcao = False
    if aplicar_correcao and not ok:
        fr.consumo_acumulado = recalc
        fr.status = recalcular_status_operacional(fr)
        db.session.add(fr)
        db.session.commit()
        correcao = True
        persistido = _to_decimal(fr.consumo_acumulado)
        diff = (persistido - recalc).quantize(TOL)
        status = "ok"

    return ResultadoReconciliacaoFranquiaCleiton(
        franquia_id=fid,
        total_persistido=persistido,
        total_recalculado=recalc,
        diferenca=diff,
        status=status,
        contagem_eventos_ia_abativel=n_ia,
        contagem_eventos_processing_abativel=n_pe,
        contagem_eventos_ia_excluidos=skip_ia,
        contagem_eventos_processing_excluidos=skip_pe,
        correcao_aplicada=correcao,
    )
