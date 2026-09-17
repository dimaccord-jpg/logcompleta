"""
Ciclo canônico Multiuser (Fase 2).

Fonte preferencial: vínculo comercial persistido da Conta
(`ContaMonetizacaoVinculo.vigencia_externa_inicio/fim`).
Não consulta Stripe live. Não escolhe Franquia arbitrária quando há divergência.
Fan-out de renovação/ativação confirmada pertence à Fase 3 (`aplicar_fanout_ciclo_confirmado`).
Não executa fan-out automaticamente na resolução canônica.

Período só é cronologicamente válido quando inicio e fim existem e fim > inicio.
Não inverte, não recalcula e não preenche data faltante.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.extensions import db
from app.models import Conta, ContaMonetizacaoVinculo, Franquia
from app.services.conta_organizacional_rules import (
    FONTE_CICLO_DIVERGENTE,
    FONTE_CICLO_FRANQUIAS_UNANIMES,
    FONTE_CICLO_INCONCLUSIVO,
    FONTE_CICLO_VINCULO_MONETIZACAO,
    SLUG_CONTA_SISTEMA,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CicloCanonicoConta:
    conta_id: int
    inicio: datetime | None
    fim: datetime | None
    fonte: str
    determinavel: bool
    divergente: bool
    inconclusivo: bool


def periodo_completo_valido(inicio, fim) -> bool:
    """
    Completo e cronologicamente válido: ambos presentes e fim > inicio.
    Tipos incompatíveis não são convertidos: a fonte é insegura.
    """
    if inicio is None or fim is None:
        return False
    try:
        return fim > inicio
    except TypeError:
        logger.info(
            "Ciclo Multiuser: inicio/fim incomparáveis; fonte tratada como insegura."
        )
        return False


def periodo_completo(inicio, fim) -> bool:
    return inicio is not None and fim is not None


def periodo_completo_invalido(inicio, fim) -> bool:
    """Ambos presentes, mas não cronologicamente válidos (zero, negativo ou incomparável)."""
    return periodo_completo(inicio, fim) and not periodo_completo_valido(inicio, fim)


def _periodos_iguais(a: tuple[datetime | None, datetime | None], b: tuple[datetime | None, datetime | None]) -> bool:
    return a[0] == b[0] and a[1] == b[1]


def _ciclo_divergente(conta_id: int) -> CicloCanonicoConta:
    logger.info(
        "Ciclo Multiuser divergente conta_id=%s fonte=%s",
        conta_id,
        FONTE_CICLO_DIVERGENTE,
    )
    return CicloCanonicoConta(
        conta_id=conta_id,
        inicio=None,
        fim=None,
        fonte=FONTE_CICLO_DIVERGENTE,
        determinavel=False,
        divergente=True,
        inconclusivo=False,
    )


def _ciclo_inconclusivo(conta_id: int) -> CicloCanonicoConta:
    return CicloCanonicoConta(
        conta_id=conta_id,
        inicio=None,
        fim=None,
        fonte=FONTE_CICLO_INCONCLUSIVO,
        determinavel=False,
        divergente=False,
        inconclusivo=True,
    )


def _franquias_operacionais_conta(conta_id: int) -> list[Franquia]:
    return (
        Franquia.query.filter_by(conta_id=int(conta_id))
        .order_by(Franquia.id.asc())
        .all()
    )


def _vinculo_monetario_ativo(conta_id: int) -> ContaMonetizacaoVinculo | None:
    return (
        ContaMonetizacaoVinculo.query.filter_by(conta_id=int(conta_id), ativo=True)
        .order_by(ContaMonetizacaoVinculo.id.asc())
        .first()
    )


def resolver_ciclo_canonico_conta(conta_id: int) -> CicloCanonicoConta:
    """
    Resolve o ciclo comercial da Conta sem inventar datas.

    Ordem:
    1. ContaMonetizacaoVinculo ativo com período completo e fim > inicio
    2. Unanimidade dos períodos cronologicamente válidos das Franquias
    3. Divergência explícita se houver períodos discordantes ou cronologicamente inválidos
    4. Inconclusivo se não houver período seguro

    Vínculo monetário completo com fim <= inicio é inseguro: divergente,
    sem fallback silencioso para Franquias.
    """
    cid = int(conta_id)
    conta = db.session.get(Conta, cid)
    if conta is None:
        return _ciclo_inconclusivo(cid)
    if (conta.slug or "") == SLUG_CONTA_SISTEMA:
        return _ciclo_inconclusivo(cid)

    vinculo = _vinculo_monetario_ativo(cid)
    if vinculo is not None:
        ini_v = vinculo.vigencia_externa_inicio
        fim_v = vinculo.vigencia_externa_fim
        if periodo_completo_valido(ini_v, fim_v):
            return CicloCanonicoConta(
                conta_id=cid,
                inicio=ini_v,
                fim=fim_v,
                fonte=FONTE_CICLO_VINCULO_MONETIZACAO,
                determinavel=True,
                divergente=False,
                inconclusivo=False,
            )
        if periodo_completo(ini_v, fim_v):
            return _ciclo_divergente(cid)

    periodos_presentes: list[tuple[datetime | None, datetime | None]] = []
    for fr in _franquias_operacionais_conta(cid):
        if fr.inicio_ciclo is None and fr.fim_ciclo is None:
            continue
        periodos_presentes.append((fr.inicio_ciclo, fr.fim_ciclo))

    if not periodos_presentes:
        return _ciclo_inconclusivo(cid)

    if any(periodo_completo_invalido(inicio, fim) for inicio, fim in periodos_presentes):
        return _ciclo_divergente(cid)

    primeiro = periodos_presentes[0]
    if any(not _periodos_iguais(p, primeiro) for p in periodos_presentes[1:]):
        return _ciclo_divergente(cid)

    inicio, fim = primeiro
    if not periodo_completo_valido(inicio, fim):
        return _ciclo_inconclusivo(cid)
    return CicloCanonicoConta(
        conta_id=cid,
        inicio=inicio,
        fim=fim,
        fonte=FONTE_CICLO_FRANQUIAS_UNANIMES,
        determinavel=True,
        divergente=False,
        inconclusivo=False,
    )


def alinhar_franquia_ao_ciclo_conta(franquia: Franquia, ciclo: CicloCanonicoConta) -> bool:
    """
    Aplica o ciclo canônico somente quando determinável, cronologicamente válido
    e a Franquia ainda não tem período. Não altera consumo_acumulado nem limite_total.
    Não sobrescreve período já persistido (válido ou inválido).
    """
    if not ciclo.determinavel:
        return False
    if not periodo_completo_valido(ciclo.inicio, ciclo.fim):
        return False
    if franquia.inicio_ciclo is not None or franquia.fim_ciclo is not None:
        return False
    franquia.inicio_ciclo = ciclo.inicio
    franquia.fim_ciclo = ciclo.fim
    db.session.add(franquia)
    logger.info(
        "Franquia alinhada ao ciclo canônico conta_id=%s franquia_id=%s fonte=%s",
        ciclo.conta_id,
        franquia.id,
        ciclo.fonte,
    )
    return True


def alinhar_franquias_sem_ciclo_ao_canonico(conta_id: int) -> int:
    """Backfill determinístico: preenche somente Franquias com ciclo totalmente nulo."""
    ciclo = resolver_ciclo_canonico_conta(conta_id)
    if not ciclo.determinavel or not periodo_completo_valido(ciclo.inicio, ciclo.fim):
        return 0
    alinhadas = 0
    for fr in _franquias_operacionais_conta(conta_id):
        if alinhar_franquia_ao_ciclo_conta(fr, ciclo):
            alinhadas += 1
    return alinhadas


@dataclass
class ResultadoFanoutCiclo:
    conta_id: int
    inicio: datetime
    fim: datetime
    franquias_alinhadas: int = 0
    consumos_resetados: int = 0
    status_recalculados: int = 0
    ignorado: bool = False
    motivo_ignorado: str | None = None
    franquia_ids: list[int] = field(default_factory=list)


def evento_atrasado_relativo_ciclo_canonico(
    conta_id: int,
    inicio_evento,
    fim_evento,
) -> bool:
    """
    Compara o período da invoice com o ciclo comercial canônico da Conta
    (vínculo monetário), não com uma Franquia-âncora.
    Sem ciclo comercial persistido: não é atrasado (ativação inicial).
    """
    ciclo = resolver_ciclo_canonico_conta(int(conta_id))
    if not ciclo.determinavel or ciclo.fim is None:
        return False
    if fim_evento is None:
        return False
    try:
        return fim_evento <= ciclo.fim
    except TypeError:
        return False


def aplicar_fanout_ciclo_confirmado(
    conta_id: int,
    inicio: datetime,
    fim: datetime,
    *,
    resetar_consumo: bool,
) -> ResultadoFanoutCiclo:
    """
    Propaga período financeiro confirmado a todas as Franquias da Conta.

    Invariantes:
    - mesmo início e mesmo fim
    - recusa período cronologicamente inválido (fim <= inicio)
    - consumo permanece individual; reset só quando solicitado e no novo período
    - não cria saldo compartilhado
    """
    cid = int(conta_id)
    if not periodo_completo_valido(inicio, fim):
        logger.info(
            "Fan-out Multiuser recusado: período cronologicamente inválido conta_id=%s",
            cid,
        )
        return ResultadoFanoutCiclo(
            conta_id=cid,
            inicio=inicio,
            fim=fim,
            ignorado=True,
            motivo_ignorado="periodo_cronologicamente_invalido",
        )

    from app.services.cleiton_franquia_operacional_service import (
        classificar_estado_operacional_franquia,
    )
    from app.services.cleiton_plano_resolver import resolver_plano_operacional_para_franquia

    resultado = ResultadoFanoutCiclo(conta_id=cid, inicio=inicio, fim=fim)
    for fr in _franquias_operacionais_conta(cid):
        alterou = False
        if fr.inicio_ciclo != inicio:
            fr.inicio_ciclo = inicio
            alterou = True
        if fr.fim_ciclo != fim:
            fr.fim_ciclo = fim
            alterou = True
        if resetar_consumo:
            consumo_atual = Decimal(str(fr.consumo_acumulado or "0"))
            if consumo_atual != Decimal("0"):
                fr.consumo_acumulado = Decimal("0")
                resultado.consumos_resetados += 1
                alterou = True
        if alterou:
            resultado.franquias_alinhadas += 1
        plano = resolver_plano_operacional_para_franquia(int(fr.id))
        st, _ = classificar_estado_operacional_franquia(fr, plano)
        if fr.status != st:
            fr.status = st
            resultado.status_recalculados += 1
        db.session.add(fr)
        resultado.franquia_ids.append(int(fr.id))
        logger.info(
            "Fan-out ciclo Multiuser conta_id=%s franquia_id=%s reset_consumo=%s",
            cid,
            fr.id,
            resetar_consumo,
        )
    db.session.flush()
    try:
        from app.services.conta_multiuser_reducao_service import (
            tentar_efetivar_reducao_no_corte,
        )

        tentar_efetivar_reducao_no_corte(cid, referencia=inicio, commit=False)
    except Exception:
        logger.exception(
            "Reducao Multiuser no corte nao efetivada conta_id=%s (fan-out preservado)",
            cid,
        )
    return resultado
