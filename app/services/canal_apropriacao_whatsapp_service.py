"""Apropria interação útil de WhatsApp na régua do Gerenciador de Franquia.

A taxa é só CleitonCostConfig.interacoes_whatsapp_por_credito.
O saldo muda por lancar_creditos_convertidos, com a mesma quantização das
outras unidades. A chave whatsapp:operacao:<execucao_id> impede
segundo débito. Não guarda texto, telefone, e-mail nem payload.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    CleitonBillingApropriacao,
    CleitonCostConfig,
    ConsumoInteracaoCanal,
    utcnow_naive,
)
from app.services.cleiton_ciclo_franquia_service import garantir_ciclo_operacional_franquia
from app.services.cleiton_franquia_operacional_service import (
    converter_interacoes_whatsapp_para_creditos,
    deve_abater_franquia_do_cliente,
    lancar_creditos_convertidos,
)

logger = logging.getLogger(__name__)

CODIGO_APROPRIADA = "apropriada"
CODIGO_IDEMPOTENTE = "apropriacao_idempotente"
CODIGO_TAXA_PENDENTE = "taxa_comercial_whatsapp_pendente"
CODIGO_ERRO = "erro_apropriacao"
CODIGO_INELEGIVEL = "inelegivel"
CODIGO_EM_ANDAMENTO = "apropriacao_em_andamento"

_AGENT = "canal_whatsapp"
_FLOW = "interacao_whatsapp_util"
_MOTIVO_PENDING = "pending"
_MOTIVO_RESERVADO = "reservado"
_MOTIVO_APROPRIADO = "apropriado"
_TIPO_ORIGEM = "canal_whatsapp"
_ESPERA_PASSOS = 40
_ESPERA_SEGUNDOS = 0.05


@dataclass(frozen=True)
class ResultadoApropriacaoCanal:
    codigo: str
    consumo_id: int | None = None
    execucao_id: int | None = None
    franquia_id: int | None = None
    creditos: Decimal | None = None
    duplicado: bool = False


def sincronizar_interacao(
    consumo_id: int, *, imediata: bool
) -> ResultadoApropriacaoCanal:
    """Conclui lançamento já feito ou, se imediata, aplica a taxa vigente.

    Replay sem taxa e sem lançamento anterior não debita. Consumo gravado
    enquanto a taxa estava vazia continua pendente até chamada explícita.
    """
    if isinstance(consumo_id, bool) or not isinstance(consumo_id, int) or consumo_id <= 0:
        resultado = _resultado(CODIGO_INELEGIVEL)
        _log(resultado)
        return resultado
    row = db.session.get(ConsumoInteracaoCanal, int(consumo_id))
    if row is None or row.estado == ConsumoInteracaoCanal.ESTADO_FALHA:
        resultado = _resultado(CODIGO_INELEGIVEL, consumo_id=consumo_id)
        _log(resultado)
        return resultado
    if row.estado == ConsumoInteracaoCanal.ESTADO_APROPRIADA:
        resultado = _resultado(
            CODIGO_IDEMPOTENTE,
            row,
            creditos=_decimal(row.creditos_apropriados),
            duplicado=True,
        )
        _log(resultado)
        return resultado
    ja = _creditos_gravados(_marcador(row.chave_idempotente))
    if ja is not None:
        _concluir_local(int(row.id), ja)
        atual = db.session.get(ConsumoInteracaoCanal, int(row.id))
        resultado = _resultado(CODIGO_IDEMPOTENTE, atual or row, creditos=ja, duplicado=True)
        _log(resultado)
        return resultado
    if not imediata:
        resultado = _resultado(CODIGO_TAXA_PENDENTE, row)
        _log(resultado)
        return resultado
    cfg = db.session.get(CleitonCostConfig, 1)
    creditos = None
    erro = "interacoes_whatsapp_por_credito ausente ou inválido na CleitonCostConfig"
    if cfg is not None:
        creditos, erro = converter_interacoes_whatsapp_para_creditos(int(row.quantidade), cfg)
    if erro or creditos is None or creditos <= 0:
        _manter_pendente(int(row.id))
        atual = db.session.get(ConsumoInteracaoCanal, int(row.id))
        resultado = _resultado(CODIGO_TAXA_PENDENTE, atual or row)
        _log(resultado)
        return resultado
    pode, _motivo = deve_abater_franquia_do_cliente(
        franquia_id=row.franquia_id,
        usuario_id=row.user_id,
        origem_sistema=False,
        tipo_origem=_TIPO_ORIGEM,
    )
    if not pode or row.franquia_id is None:
        _marcar_erro(int(row.id))
        atual = db.session.get(ConsumoInteracaoCanal, int(row.id))
        resultado = _resultado(CODIGO_ERRO, atual or row)
        _log(resultado)
        return resultado
    return _debitar(row, creditos)


def apropriar_consumos_pendentes() -> list[ResultadoApropriacaoCanal]:
    """Aplica a taxa atual aos consumos ainda não lançados. Idempotente."""
    ids = [
        int(item.id)
        for item in ConsumoInteracaoCanal.query.filter(
            ConsumoInteracaoCanal.estado.in_(
                (
                    ConsumoInteracaoCanal.ESTADO_PRONTA,
                    ConsumoInteracaoCanal.ESTADO_ERRO,
                )
            )
        )
        .order_by(ConsumoInteracaoCanal.id.asc())
        .all()
    ]
    return [sincronizar_interacao(consumo_id, imediata=True) for consumo_id in ids]


def _debitar(
    row: ConsumoInteracaoCanal, creditos: Decimal, *, retomada: bool = False
) -> ResultadoApropriacaoCanal:
    consumo_id = int(row.id)
    franquia_id = int(row.franquia_id)
    marker = _obter_ou_criar_marcador(row)
    if marker is None:
        _marcar_erro(consumo_id)
        atual = db.session.get(ConsumoInteracaoCanal, consumo_id)
        resultado = _resultado(CODIGO_ERRO, atual)
        _log(resultado)
        return resultado
    marker_id = int(marker.id)
    ja = _creditos_gravados(marker)
    if ja is not None:
        _concluir_local(consumo_id, ja)
        atual = db.session.get(ConsumoInteracaoCanal, consumo_id)
        resultado = _resultado(CODIGO_IDEMPOTENTE, atual, creditos=ja, duplicado=True)
        _log(resultado)
        return resultado
    if not _reservar(marker_id):
        return _aguardar_conclusao(
            consumo_id, marker_id, creditos, retomada=retomada
        )
    try:
        garantir_ciclo_operacional_franquia(franquia_id)
        marker = db.session.get(CleitonBillingApropriacao, marker_id)
        if marker is None:
            raise RuntimeError("marcador_ausente")
        marker.creditos_apropriados = creditos
        marker.motivo = _MOTIVO_APROPRIADO
        marker.status = "success"
        db.session.add(marker)
        governanca = lancar_creditos_convertidos(franquia_id, creditos)
    except Exception as exc:
        db.session.rollback()
        logger.info(
            "canal_apropriacao consumo_id=%s codigo=%s erro=%s",
            consumo_id,
            CODIGO_ERRO,
            type(exc).__name__,
        )
        _liberar_marcador(marker_id)
        _marcar_erro(consumo_id)
        atual = db.session.get(ConsumoInteracaoCanal, consumo_id)
        resultado = _resultado(CODIGO_ERRO, atual)
        _log(resultado)
        return resultado
    if not governanca.abateu_franquia:
        db.session.rollback()
        _liberar_marcador(marker_id)
        _marcar_erro(consumo_id)
        atual = db.session.get(ConsumoInteracaoCanal, consumo_id)
        resultado = _resultado(CODIGO_ERRO, atual)
        _log(resultado)
        return resultado
    marker = db.session.get(CleitonBillingApropriacao, marker_id)
    gravado = _creditos_gravados(marker) or creditos
    _concluir_local(consumo_id, gravado)
    atual = db.session.get(ConsumoInteracaoCanal, consumo_id)
    resultado = _resultado(CODIGO_APROPRIADA, atual, creditos=gravado, duplicado=False)
    _log(resultado)
    return resultado


def _aguardar_conclusao(
    consumo_id: int,
    marker_id: int,
    creditos: Decimal,
    *,
    retomada: bool,
) -> ResultadoApropriacaoCanal:
    marker = _esperar_creditos(marker_id)
    ja = _creditos_gravados(marker)
    if ja is not None:
        _concluir_local(consumo_id, ja)
        atual = db.session.get(ConsumoInteracaoCanal, consumo_id)
        resultado = _resultado(CODIGO_IDEMPOTENTE, atual, creditos=ja, duplicado=True)
        _log(resultado)
        return resultado
    if not retomada and _rebaixar_reservado(marker_id):
        row = db.session.get(ConsumoInteracaoCanal, consumo_id)
        if row is not None and row.estado != ConsumoInteracaoCanal.ESTADO_APROPRIADA:
            return _debitar(row, creditos, retomada=True)
    atual = db.session.get(ConsumoInteracaoCanal, consumo_id)
    resultado = _resultado(CODIGO_EM_ANDAMENTO, atual)
    _log(resultado)
    return resultado


def _rebaixar_reservado(marker_id: int) -> bool:
    """Devolve um marcador reservado sem crédito para uma nova tentativa única."""
    rebaixado = db.session.execute(
        update(CleitonBillingApropriacao)
        .where(
            CleitonBillingApropriacao.id == int(marker_id),
            CleitonBillingApropriacao.motivo == _MOTIVO_RESERVADO,
            CleitonBillingApropriacao.creditos_apropriados.is_(None),
        )
        .values(motivo=_MOTIVO_PENDING, status="pending")
        .returning(CleitonBillingApropriacao.id)
    ).scalar_one_or_none()
    db.session.commit()
    return rebaixado is not None


def _obter_ou_criar_marcador(
    row: ConsumoInteracaoCanal,
) -> CleitonBillingApropriacao | None:
    chave = row.chave_idempotente
    existente = _marcador(chave)
    if existente is not None:
        return existente
    marker = CleitonBillingApropriacao(
        idempotency_key=chave,
        agent=_AGENT,
        flow_type=_FLOW,
        status="pending",
        error_summary=None,
        rows_processed=0,
        processing_time_ms=0,
        processing_event_id=None,
        creditos_apropriados=None,
        motivo=_MOTIVO_PENDING,
        conta_id=int(row.conta_id) if row.conta_id is not None else None,
        franquia_id=int(row.franquia_id) if row.franquia_id is not None else None,
        usuario_id=int(row.user_id),
    )
    try:
        with db.session.begin_nested():
            db.session.add(marker)
            db.session.flush()
            marker_id = int(marker.id)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return _marcador(chave)
    return db.session.get(CleitonBillingApropriacao, marker_id)


def _reservar(marker_id: int) -> bool:
    reservado = db.session.execute(
        update(CleitonBillingApropriacao)
        .where(
            CleitonBillingApropriacao.id == int(marker_id),
            CleitonBillingApropriacao.motivo == _MOTIVO_PENDING,
            CleitonBillingApropriacao.creditos_apropriados.is_(None),
        )
        .values(motivo=_MOTIVO_RESERVADO, status="reservado")
        .returning(CleitonBillingApropriacao.id)
    ).scalar_one_or_none()
    db.session.commit()
    return reservado is not None


def _esperar_creditos(marker_id: int) -> CleitonBillingApropriacao | None:
    marker = None
    for _ in range(_ESPERA_PASSOS):
        db.session.expire_all()
        marker = db.session.get(CleitonBillingApropriacao, int(marker_id))
        if _creditos_gravados(marker) is not None:
            return marker
        time.sleep(_ESPERA_SEGUNDOS)
    db.session.expire_all()
    return db.session.get(CleitonBillingApropriacao, int(marker_id))


def _liberar_marcador(marker_id: int | None) -> None:
    if marker_id is None:
        return
    marker = db.session.get(CleitonBillingApropriacao, int(marker_id))
    if marker is None or marker.creditos_apropriados is not None:
        return
    if marker.motivo == _MOTIVO_APROPRIADO:
        return
    marker.motivo = _MOTIVO_PENDING
    marker.status = "pending"
    db.session.add(marker)
    db.session.commit()


def _concluir_local(consumo_id: int, creditos: Decimal) -> None:
    row = db.session.get(ConsumoInteracaoCanal, int(consumo_id))
    if row is None or row.estado == ConsumoInteracaoCanal.ESTADO_APROPRIADA:
        return
    if creditos <= 0:
        return
    row.estado = ConsumoInteracaoCanal.ESTADO_APROPRIADA
    row.motivo = ConsumoInteracaoCanal.MOTIVO_APROPRIADA
    row.creditos_apropriados = creditos
    row.atualizada_em = utcnow_naive()
    db.session.add(row)
    db.session.commit()


def _manter_pendente(consumo_id: int) -> None:
    row = db.session.get(ConsumoInteracaoCanal, int(consumo_id))
    if row is None or row.estado != ConsumoInteracaoCanal.ESTADO_ERRO:
        return
    if row.creditos_apropriados is not None:
        return
    row.estado = ConsumoInteracaoCanal.ESTADO_PRONTA
    row.motivo = ConsumoInteracaoCanal.MOTIVO_TAXA_PENDENTE
    row.atualizada_em = utcnow_naive()
    db.session.add(row)
    db.session.commit()


def _marcar_erro(consumo_id: int) -> None:
    row = db.session.get(ConsumoInteracaoCanal, int(consumo_id))
    if row is None or row.estado == ConsumoInteracaoCanal.ESTADO_APROPRIADA:
        return
    row.estado = ConsumoInteracaoCanal.ESTADO_ERRO
    row.motivo = ConsumoInteracaoCanal.MOTIVO_ERRO
    row.creditos_apropriados = None
    row.atualizada_em = utcnow_naive()
    db.session.add(row)
    db.session.commit()


def _marcador(chave: str) -> CleitonBillingApropriacao | None:
    return CleitonBillingApropriacao.query.filter_by(idempotency_key=chave).one_or_none()


def _creditos_gravados(marker: CleitonBillingApropriacao | None) -> Decimal | None:
    if marker is None or marker.motivo != _MOTIVO_APROPRIADO:
        return None
    if marker.creditos_apropriados is None:
        return None
    valor = _decimal(marker.creditos_apropriados)
    if valor is None or valor <= 0:
        return None
    return valor


def _decimal(bruto: object) -> Decimal | None:
    if bruto is None or isinstance(bruto, bool):
        return None
    try:
        return Decimal(str(bruto))
    except Exception:
        return None


def _resultado(
    codigo: str,
    row: ConsumoInteracaoCanal | None = None,
    *,
    consumo_id: int | None = None,
    creditos: Decimal | None = None,
    duplicado: bool = False,
) -> ResultadoApropriacaoCanal:
    if row is None:
        return ResultadoApropriacaoCanal(
            codigo=codigo,
            consumo_id=consumo_id,
            creditos=creditos,
            duplicado=duplicado,
        )
    return ResultadoApropriacaoCanal(
        codigo=codigo,
        consumo_id=int(row.id),
        execucao_id=int(row.execucao_operacional_canal_id),
        franquia_id=int(row.franquia_id) if row.franquia_id is not None else None,
        creditos=creditos if creditos is not None else _decimal(row.creditos_apropriados),
        duplicado=duplicado,
    )


def _log(resultado: ResultadoApropriacaoCanal) -> None:
    logger.info(
        "canal_apropriacao consumo_id=%s execucao_id=%s franquia_id=%s creditos=%s codigo=%s",
        resultado.consumo_id if resultado.consumo_id is not None else "-",
        resultado.execucao_id if resultado.execucao_id is not None else "-",
        resultado.franquia_id if resultado.franquia_id is not None else "-",
        resultado.creditos if resultado.creditos is not None else "-",
        resultado.codigo,
    )
