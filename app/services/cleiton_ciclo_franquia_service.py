"""
Domínio Cleiton — ciclo operacional persistido na Franquia (início/fim de vigência).

Fontes de data:
- Sem tabela de pagamento/renovação no projeto: usamos `Franquia.created_at` como proxy de
  ativação até existir dado confiável de cobrança.
- Planos recorrentes (starter/pro): fim do ciclo = mesmo dia do mês seguinte (aniversário mensal
  aproximado), não calendário de fatura real — pendência explícita no retorno.
- Avulso: 30 dias corridos a partir da ativação.
- Free: início = ativação; fim opcional (null) — sem vigência comercial fixa nesta etapa.
- Interna: sem ciclo comercial (datas null).
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.extensions import db
from app.models import Franquia, utcnow_naive
from app.services.cleiton_plano_resolver import (
    CODIGO_AVULSO,
    CODIGO_FREE,
    CODIGO_INTERNA,
    CODIGO_PRO,
    CODIGO_STARTER,
    resolver_plano_operacional_para_franquia,
)

DIAS_AVULSO = 30


@dataclass(frozen=True)
class ResultadoCicloCleiton:
    inicio_ciclo: datetime | None
    fim_ciclo: datetime | None
    alterado: bool
    pendencias: tuple[str, ...]


def _ativacao_base(fr: Franquia) -> datetime:
    t = fr.created_at
    if t is None:
        return utcnow_naive()
    return t


def _add_um_mes_mesmo_dia(dt: datetime) -> datetime:
    y, m, d = dt.year, dt.month, dt.day
    if m == 12:
        y2, m2 = y + 1, 1
    else:
        y2, m2 = y, m + 1
    ult = calendar.monthrange(y2, m2)[1]
    d2 = min(d, ult)
    return dt.replace(year=y2, month=m2, day=d2)


def garantir_ciclo_operacional_franquia(franquia_id: int) -> ResultadoCicloCleiton:
    """
    Se a franquia ainda não tiver ciclo definido, inicializa `inicio_ciclo` / `fim_ciclo`
    conforme o plano resolvido. Persiste alterações.
    """
    fr = db.session.get(Franquia, int(franquia_id))
    if fr is None:
        return ResultadoCicloCleiton(
            inicio_ciclo=None,
            fim_ciclo=None,
            alterado=False,
            pendencias=("franquia_inexistente",),
        )

    plano = resolver_plano_operacional_para_franquia(fr.id)
    pendencias = list(plano.pendencias)

    if plano.codigo == CODIGO_INTERNA:
        return ResultadoCicloCleiton(
            inicio_ciclo=fr.inicio_ciclo,
            fim_ciclo=fr.fim_ciclo,
            alterado=False,
            pendencias=tuple(pendencias),
        )

    if fr.inicio_ciclo is not None and fr.fim_ciclo is not None:
        return ResultadoCicloCleiton(
            inicio_ciclo=fr.inicio_ciclo,
            fim_ciclo=fr.fim_ciclo,
            alterado=False,
            pendencias=tuple(pendencias),
        )

    base = _ativacao_base(fr)
    inicio = base
    fim: datetime | None

    if plano.codigo == CODIGO_AVULSO:
        fim = inicio + timedelta(days=DIAS_AVULSO)
        pendencias.append("vigencia_avulso_30_dias_corridos")
    elif plano.codigo in (CODIGO_STARTER, CODIGO_PRO):
        fim = _add_um_mes_mesmo_dia(inicio)
        pendencias.append("renovacao_recorrente_aproximada_sem_data_pagamento")
    elif plano.codigo == CODIGO_FREE:
        fim = None
        pendencias.append("ciclo_free_sem_fim_obrigatorio")
    else:
        fim = None
        pendencias.append("plano_indefinido_ciclo_nao_inicializado")

    if fr.inicio_ciclo is None and fr.fim_ciclo is None and fim is not None:
        fr.inicio_ciclo = inicio
        fr.fim_ciclo = fim
        db.session.add(fr)
        db.session.commit()
        return ResultadoCicloCleiton(
            inicio_ciclo=fr.inicio_ciclo,
            fim_ciclo=fr.fim_ciclo,
            alterado=True,
            pendencias=tuple(pendencias),
        )

    if fr.inicio_ciclo is None and fim is None and plano.codigo == CODIGO_FREE:
        fr.inicio_ciclo = inicio
        fr.fim_ciclo = None
        db.session.add(fr)
        db.session.commit()
        return ResultadoCicloCleiton(
            inicio_ciclo=fr.inicio_ciclo,
            fim_ciclo=None,
            alterado=True,
            pendencias=tuple(pendencias),
        )

    return ResultadoCicloCleiton(
        inicio_ciclo=fr.inicio_ciclo,
        fim_ciclo=fr.fim_ciclo,
        alterado=False,
        pendencias=tuple(pendencias),
    )


def ler_ciclo_vigente(franquia_id: int) -> ResultadoCicloCleiton:
    """Leitura sem persistência: estado atual da franquia + pendências do plano."""
    fr = db.session.get(Franquia, int(franquia_id))
    if fr is None:
        return ResultadoCicloCleiton(
            None, None, False, ("franquia_inexistente",)
        )
    plano = resolver_plano_operacional_para_franquia(fr.id)
    return ResultadoCicloCleiton(
        inicio_ciclo=fr.inicio_ciclo,
        fim_ciclo=fr.fim_ciclo,
        alterado=False,
        pendencias=tuple(plano.pendencias),
    )
