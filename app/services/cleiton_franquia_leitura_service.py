"""
Domínio Cleiton — leitura operacional consolidada da Franquia (persistida).

Não substitui `consumo_leitura_service` (agregação por eventos técnicos); complementa a visão
operacional para consumo pelo sistema (limites, ciclo, status, plano).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.extensions import db
from app.models import Franquia
from app.services.cleiton_ciclo_franquia_service import (
    garantir_ciclo_operacional_franquia,
    ler_ciclo_vigente,
)
from app.services.cleiton_franquia_operacional_service import (
    classificar_estado_operacional_franquia,
)
from app.services.cleiton_plano_resolver import (
    PlanoResolvidoCleiton,
    resolver_plano_operacional_para_franquia,
)


@dataclass(frozen=True)
class LeituraOperacionalFranquiaCleiton:
    franquia_id: int
    limite_total: Decimal | None
    consumo_acumulado: Decimal
    saldo_disponivel: Decimal | None
    inicio_ciclo: Any
    fim_ciclo: Any
    status: str
    plano_resolvido: str
    motivo_status: str
    pendencias: tuple[str, ...]


def _to_decimal(x: Any) -> Decimal:
    if x is None:
        return Decimal("0")
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


def _saldo(limite: Any, consumo: Decimal) -> Decimal | None:
    if limite is None:
        return None
    d = _to_decimal(limite)
    if d <= 0:
        return None
    return d - consumo


def ler_franquia_operacional_cleiton(
    franquia_id: int,
    *,
    sincronizar_ciclo: bool = False,
) -> LeituraOperacionalFranquiaCleiton | None:
    """
    Retorna snapshot operacional. Se `sincronizar_ciclo=True`, aplica `garantir_ciclo` antes
    (persistência de início/fim quando ainda ausentes).
    """
    fid = int(franquia_id)
    if sincronizar_ciclo:
        ciclo = garantir_ciclo_operacional_franquia(fid)
        pend_ciclo = list(ciclo.pendencias)
    else:
        ciclo = ler_ciclo_vigente(fid)
        pend_ciclo = list(ciclo.pendencias)

    fr = db.session.get(Franquia, fid)
    if fr is None:
        return None

    plano: PlanoResolvidoCleiton = resolver_plano_operacional_para_franquia(fid)
    pend = tuple(dict.fromkeys((*plano.pendencias, *pend_ciclo)))

    consumo = _to_decimal(fr.consumo_acumulado)
    lim = fr.limite_total
    saldo = _saldo(lim, consumo)

    st, motivo = classificar_estado_operacional_franquia(fr, plano)

    return LeituraOperacionalFranquiaCleiton(
        franquia_id=fid,
        limite_total=(None if lim is None else _to_decimal(lim)),
        consumo_acumulado=consumo,
        saldo_disponivel=saldo,
        inicio_ciclo=fr.inicio_ciclo,
        fim_ciclo=fr.fim_ciclo,
        status=st,
        plano_resolvido=plano.codigo,
        motivo_status=motivo,
        pendencias=pend,
    )


def definir_bloqueio_manual_franquia(franquia_id: int, ativo: bool) -> bool:
    """
    Persiste bloqueio manual administrativo. UI/admin pode chamar quando existir fluxo.
    Recalcula status após gravar.
    """
    from app.services.cleiton_franquia_operacional_service import (
        aplicar_status_apos_mudanca_estrutural,
    )

    fr = db.session.get(Franquia, int(franquia_id))
    if fr is None:
        return False
    fr.bloqueio_manual = bool(ativo)
    db.session.add(fr)
    db.session.commit()
    aplicar_status_apos_mudanca_estrutural(fr.id)
    return True
