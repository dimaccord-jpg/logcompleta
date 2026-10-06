"""Relaciona status de entrega já recebidos à saída pelo id do provider.

A entrada é um EventoCanalRecebido do tipo status_entrega. O id externo
composto pelo lote de recepção traz mensagem, status e timestamp, nessa
ordem, separados por dois-pontos a partir da direita. O diagnóstico tem
de concordar com o status. Não há segunda validação de assinatura.

O id do provider procura primeiro a tentativa de envio. A saída lógica
sai dessa tentativa. Se nenhuma tentativa tiver o id, o vínculo direto
com a saída permanece. Mais de uma tentativa com o mesmo id não escolhe
e não aplica status. Destinatário, usuário, texto e horário aproximado
não ligam um status a uma reserva. Um id desconhecido, inclusive o sent
tardio depois de timeout sem provider_message_id, permanece sem_saida:
não é atribuído à tentativa 1 nem a outra tentativa. status_envio da
chamada HTTP permanece.
sent, delivered e read avançam sem regressão, na tentativa encontrada.
O mesmo evento não é aplicado duas vezes. A exceção explícita é
reconciliar_status_orfao: só reavalia um evento cujo último resultado
foi sem_saida, e grava a tentativa seguinte sem apagar a primeira.

Não consulta a Cloud API, não emite token e não altera conclusão.
Não varre eventos órfãos quando a saída ganha provider_message_id.
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    AplicacaoStatusCanal,
    EstadoEntregaCanalSaida,
    EstadoEntregaTentativaCanal,
    EventoCanalRecebido,
    EventoCanalSaida,
    TentativaEnvioCanal,
    utcnow_naive,
)
from app.services.canal_tentativa_envio_service import refletir_resultado_incerto

logger = logging.getLogger(__name__)

CODIGO_APLICADO = AplicacaoStatusCanal.RESULTADO_APLICADO
CODIGO_SEM_EFEITO = AplicacaoStatusCanal.RESULTADO_SEM_EFEITO
CODIGO_REGISTRADO_SEM_AVANCO = AplicacaoStatusCanal.RESULTADO_SEM_AVANCO
CODIGO_SEM_SAIDA = AplicacaoStatusCanal.RESULTADO_SEM_SAIDA
CODIGO_STATUS_IGNORADO = AplicacaoStatusCanal.RESULTADO_STATUS_IGNORADO
CODIGO_SAIDA_AMBIGUA = AplicacaoStatusCanal.RESULTADO_SAIDA_AMBIGUA
CODIGO_ILEGIVEL = AplicacaoStatusCanal.RESULTADO_ILEGIVEL
CODIGO_JA_TRATADO = "ja_tratado"
CODIGO_AINDA_SEM_SAIDA = "ainda_sem_saida"
CODIGO_REAPLICACAO_NAO_PERMITIDA = "reaplicacao_nao_permitida"
CODIGO_EVENTO_AUSENTE = "evento_ausente"
CODIGO_EVENTO_INVALIDO = "evento_invalido"
CODIGO_EVENTO_NAO_STATUS = "evento_nao_status"
CODIGO_EM_PROCESSAMENTO = "em_processamento"
CODIGO_ERRO_SEGURO = "erro_seguro"
CODIGO_CLASSIFICADO = EstadoEntregaCanalSaida.CLASSIFICACAO_RESULTADO_INCERTO
CODIGO_JA_CLASSIFICADO = "resultado_incerto_ja_registrado"
CODIGO_CLASSIFICACAO_NAO_APLICAVEL = "classificacao_nao_aplicavel"
CODIGO_SAIDA_AUSENTE = "saida_ausente"
CODIGO_SAIDA_INVALIDA = "saida_invalida"

_RECUPERADOS = frozenset(
    {
        CODIGO_APLICADO,
        CODIGO_SEM_EFEITO,
        CODIGO_REGISTRADO_SEM_AVANCO,
    }
)
_SUPORTADOS = frozenset(
    {
        EstadoEntregaCanalSaida.STATUS_SENT,
        EstadoEntregaCanalSaida.STATUS_DELIVERED,
        EstadoEntregaCanalSaida.STATUS_READ,
        EstadoEntregaCanalSaida.STATUS_FAILED,
    }
)
_ORDEM = {
    EstadoEntregaCanalSaida.STATUS_SENT: 1,
    EstadoEntregaCanalSaida.STATUS_DELIVERED: 2,
    EstadoEntregaCanalSaida.STATUS_READ: 3,
}
_DESTINO_EVENTO = {
    CODIGO_APLICADO: EventoCanalRecebido.STATUS_ROTEADO,
    CODIGO_SEM_EFEITO: EventoCanalRecebido.STATUS_ROTEADO,
    CODIGO_REGISTRADO_SEM_AVANCO: EventoCanalRecebido.STATUS_ROTEADO,
    CODIGO_SEM_SAIDA: EventoCanalRecebido.STATUS_IGNORADO,
    CODIGO_STATUS_IGNORADO: EventoCanalRecebido.STATUS_IGNORADO,
    CODIGO_SAIDA_AMBIGUA: EventoCanalRecebido.STATUS_IGNORADO,
    CODIGO_ILEGIVEL: EventoCanalRecebido.STATUS_IGNORADO,
}
_ESTADOS_INCERTOS = frozenset(
    {
        EventoCanalSaida.STATUS_RESERVADO,
        EventoCanalSaida.STATUS_PREPARANDO_LINK,
    }
)

_COMPOSTO = re.compile(r"^(?P<mensagem>.+):(?P<status>[a-z_]{1,32}):(?P<marca>[0-9]{1,20})$")
_CORRELATION = re.compile(r"^[A-Za-z0-9]{1,32}$")

_travas_evento: dict[int, threading.Lock] = {}
_travas_saida: dict[int, threading.Lock] = {}
_travas_guard = threading.Lock()


class _Conflito(Exception):
    """Outra transação alterou a versão do estado de entrega."""


@dataclass(frozen=True)
class _Componentes:
    mensagem_id: str | None
    status: str | None
    quando: datetime | None
    suportado: bool
    ilegivel: bool


@dataclass(frozen=True)
class _Projecao:
    mudou: bool
    avancou: bool
    status_entrega: str
    provider_status_em: datetime | None
    sent_em: datetime | None
    delivered_em: datetime | None
    read_em: datetime | None
    failed_em: datetime | None
    codigo_falha_entrega: str | None
    classificacao_resultado: str | None


@dataclass(frozen=True)
class ResultadoReconciliacaoStatus:
    codigo: str
    evento_id: int | None = None
    saida_id: int | None = None
    status_entrega: str | None = None
    correlation_id: str | None = None
    provider_status_em: datetime | None = None
    classificacao_resultado: str | None = None


def _trava(mapa: dict[int, threading.Lock], chave: int) -> threading.Lock:
    with _travas_guard:
        lock = mapa.get(chave)
        if lock is None:
            lock = threading.Lock()
            mapa[chave] = lock
        return lock


def _id_valido(valor: object) -> bool:
    return not isinstance(valor, bool) and isinstance(valor, int) and valor > 0


def _log(resultado: ResultadoReconciliacaoStatus) -> None:
    logger.info(
        "reconciliacao_status evento_id=%s saida_id=%s correlation_id=%s status=%s codigo=%s",
        resultado.evento_id if resultado.evento_id is not None else "-",
        resultado.saida_id if resultado.saida_id is not None else "-",
        resultado.correlation_id or "-",
        resultado.status_entrega or resultado.classificacao_resultado or "-",
        resultado.codigo,
    )


def _resultado(
    codigo: str,
    *,
    evento_id: int | None = None,
    saida_id: int | None = None,
    status_entrega: str | None = None,
    correlation_id: str | None = None,
    provider_status_em: datetime | None = None,
    classificacao_resultado: str | None = None,
) -> ResultadoReconciliacaoStatus:
    return ResultadoReconciliacaoStatus(
        codigo=codigo,
        evento_id=evento_id,
        saida_id=saida_id,
        status_entrega=status_entrega,
        correlation_id=correlation_id,
        provider_status_em=provider_status_em,
        classificacao_resultado=classificacao_resultado,
    )


def _correlation(evento: EventoCanalRecebido) -> str | None:
    valor = evento.correlation_id
    if isinstance(valor, str) and _CORRELATION.fullmatch(valor):
        return valor
    return None


def _quando(marca: str) -> datetime | None:
    try:
        segundos = int(marca)
        if segundos < 0:
            return None
        return datetime.fromtimestamp(segundos, timezone.utc).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def _extrair(evento: EventoCanalRecebido) -> _Componentes:
    """Separa o id composto. O diagnóstico precisa nomear o mesmo status."""
    bruto = evento.evento_externo_id
    diagnostico = evento.diagnostico_seguro
    if not isinstance(bruto, str) or not isinstance(diagnostico, str):
        return _Componentes(None, None, None, False, True)
    encontrado = _COMPOSTO.fullmatch(bruto)
    if encontrado is None:
        return _Componentes(None, None, None, False, True)
    mensagem = encontrado.group("mensagem")
    status = encontrado.group("status")
    quando = _quando(encontrado.group("marca"))
    if quando is None or not mensagem or len(mensagem) > EventoCanalSaida.MENSAGEM_MAXIMA:
        return _Componentes(None, None, None, False, True)
    esperado = f"status_entrega:{status}"
    if diagnostico == esperado and status in _SUPORTADOS:
        return _Componentes(mensagem, status, quando, True, False)
    if diagnostico == esperado or diagnostico == "status_entrega":
        return _Componentes(mensagem, status, quando, False, False)
    return _Componentes(None, None, None, False, True)


def _horario_do_status(
    status: str,
    *,
    sent_em: datetime | None,
    delivered_em: datetime | None,
    read_em: datetime | None,
    failed_em: datetime | None,
) -> datetime | None:
    if status == EstadoEntregaCanalSaida.STATUS_SENT:
        return sent_em
    if status == EstadoEntregaCanalSaida.STATUS_DELIVERED:
        return delivered_em
    if status == EstadoEntregaCanalSaida.STATUS_READ:
        return read_em
    if status == EstadoEntregaCanalSaida.STATUS_FAILED:
        return failed_em
    return None


def _status_resultante(atual: str | None, recebido: str) -> str:
    if atual is None:
        return recebido
    if recebido == atual:
        return atual
    if recebido in _ORDEM:
        if atual == EstadoEntregaCanalSaida.STATUS_FAILED:
            if recebido == EstadoEntregaCanalSaida.STATUS_SENT:
                return atual
            return recebido
        if atual in _ORDEM and _ORDEM[recebido] > _ORDEM[atual]:
            return recebido
        return atual
    if atual in (EstadoEntregaCanalSaida.STATUS_DELIVERED, EstadoEntregaCanalSaida.STATUS_READ):
        return atual
    return EstadoEntregaCanalSaida.STATUS_FAILED


def _projetar(estado: EstadoEntregaCanalSaida, status: str, quando: datetime) -> _Projecao:
    sent_em = estado.sent_em
    delivered_em = estado.delivered_em
    read_em = estado.read_em
    failed_em = estado.failed_em
    codigo = estado.codigo_falha_entrega
    atual = estado.status_entrega
    if status == EstadoEntregaCanalSaida.STATUS_SENT and sent_em is None:
        sent_em = quando
    elif status == EstadoEntregaCanalSaida.STATUS_DELIVERED and delivered_em is None:
        delivered_em = quando
    elif status == EstadoEntregaCanalSaida.STATUS_READ and read_em is None:
        read_em = quando
    elif (
        status == EstadoEntregaCanalSaida.STATUS_FAILED
        and failed_em is None
        and atual not in (
            EstadoEntregaCanalSaida.STATUS_DELIVERED,
            EstadoEntregaCanalSaida.STATUS_READ,
        )
    ):
        failed_em = quando
        codigo = EstadoEntregaCanalSaida.CODIGO_FALHA_ENTREGA
    novo = _status_resultante(atual, status)
    provider_em = estado.provider_status_em
    classificacao = estado.classificacao_resultado
    if novo != atual:
        provider_em = _horario_do_status(
            novo,
            sent_em=sent_em,
            delivered_em=delivered_em,
            read_em=read_em,
            failed_em=failed_em,
        )
        classificacao = None
    mudou = (
        novo != atual
        or sent_em != estado.sent_em
        or delivered_em != estado.delivered_em
        or read_em != estado.read_em
        or failed_em != estado.failed_em
        or codigo != estado.codigo_falha_entrega
        or provider_em != estado.provider_status_em
        or classificacao != estado.classificacao_resultado
    )
    return _Projecao(
        mudou=mudou,
        avancou=novo != atual,
        status_entrega=novo,
        provider_status_em=provider_em,
        sent_em=sent_em,
        delivered_em=delivered_em,
        read_em=read_em,
        failed_em=failed_em,
        codigo_falha_entrega=codigo,
        classificacao_resultado=classificacao,
    )


def _valores_iniciais(status: str, quando: datetime) -> dict:
    sent_em = quando if status == EstadoEntregaCanalSaida.STATUS_SENT else None
    delivered_em = quando if status == EstadoEntregaCanalSaida.STATUS_DELIVERED else None
    read_em = quando if status == EstadoEntregaCanalSaida.STATUS_READ else None
    failed_em = quando if status == EstadoEntregaCanalSaida.STATUS_FAILED else None
    codigo = (
        EstadoEntregaCanalSaida.CODIGO_FALHA_ENTREGA
        if status == EstadoEntregaCanalSaida.STATUS_FAILED
        else None
    )
    return {
        "versao": 0,
        "status_entrega": status,
        "provider_status_em": quando,
        "sent_em": sent_em,
        "delivered_em": delivered_em,
        "read_em": read_em,
        "failed_em": failed_em,
        "codigo_falha_entrega": codigo,
        "classificacao_resultado": None,
    }


def _codigo_da_projecao(projecao: _Projecao) -> str:
    if not projecao.mudou:
        return CODIGO_SEM_EFEITO
    if projecao.avancou:
        return CODIGO_APLICADO
    return CODIGO_REGISTRADO_SEM_AVANCO


def _gravar_estado(modelo, estado, projecao: _Projecao) -> None:
    resultado = db.session.execute(
        update(modelo)
        .where(modelo.id == estado.id)
        .where(modelo.versao == estado.versao)
        .values(
            versao=int(estado.versao) + 1,
            status_entrega=projecao.status_entrega,
            provider_status_em=projecao.provider_status_em,
            sent_em=projecao.sent_em,
            delivered_em=projecao.delivered_em,
            read_em=projecao.read_em,
            failed_em=projecao.failed_em,
            codigo_falha_entrega=projecao.codigo_falha_entrega,
            classificacao_resultado=projecao.classificacao_resultado,
        )
        .execution_options(synchronize_session="fetch")
    )
    if int(resultado.rowcount or 0) != 1:
        raise _Conflito()


def _aplicar_estado_saida(saida_id: int, status: str, quando: datetime) -> tuple[str, str | None]:
    estado = EstadoEntregaCanalSaida.query.filter_by(saida_id=saida_id).one_or_none()
    if estado is None:
        db.session.add(
            EstadoEntregaCanalSaida(saida_id=saida_id, **_valores_iniciais(status, quando))
        )
        db.session.flush()
        return CODIGO_APLICADO, status
    projecao = _projetar(estado, status, quando)
    if not projecao.mudou:
        return CODIGO_SEM_EFEITO, estado.status_entrega
    _gravar_estado(EstadoEntregaCanalSaida, estado, projecao)
    return _codigo_da_projecao(projecao), projecao.status_entrega


def _aplicar_estado_tentativa(
    tentativa_id: int,
    saida_id: int,
    status: str,
    quando: datetime,
) -> tuple[str, str | None]:
    estado = EstadoEntregaTentativaCanal.query.filter_by(
        tentativa_envio_id=tentativa_id
    ).one_or_none()
    if estado is None:
        db.session.add(
            EstadoEntregaTentativaCanal(
                tentativa_envio_id=tentativa_id,
                saida_id=saida_id,
                **_valores_iniciais(status, quando),
            )
        )
        db.session.flush()
        return CODIGO_APLICADO, status
    projecao = _projetar(estado, status, quando)
    if not projecao.mudou:
        return CODIGO_SEM_EFEITO, estado.status_entrega
    _gravar_estado(EstadoEntregaTentativaCanal, estado, projecao)
    return _codigo_da_projecao(projecao), projecao.status_entrega


def _tentativa_e_recente(tentativa_id: int, saida_id: int) -> bool:
    tentativa = db.session.get(TentativaEnvioCanal, tentativa_id)
    if tentativa is None:
        return False
    maior = (
        db.session.query(db.func.max(TentativaEnvioCanal.numero_tentativa))
        .filter(TentativaEnvioCanal.saida_id == int(saida_id))
        .scalar()
    )
    if maior is None:
        return True
    return int(tentativa.numero_tentativa) == int(maior)


def _entrega_igual(estado: EstadoEntregaCanalSaida, origem: EstadoEntregaTentativaCanal) -> bool:
    return (
        estado.status_entrega == origem.status_entrega
        and estado.provider_status_em == origem.provider_status_em
        and estado.sent_em == origem.sent_em
        and estado.delivered_em == origem.delivered_em
        and estado.read_em == origem.read_em
        and estado.failed_em == origem.failed_em
        and estado.codigo_falha_entrega == origem.codigo_falha_entrega
        and estado.classificacao_resultado == origem.classificacao_resultado
    )


def _espelhar_entrega_na_saida(saida_id: int, tentativa_id: int) -> None:
    """A projeção da saída copia a tentativa mais recente, sem fundir outra."""
    origem = EstadoEntregaTentativaCanal.query.filter_by(
        tentativa_envio_id=tentativa_id
    ).one_or_none()
    if origem is None:
        return
    estado = EstadoEntregaCanalSaida.query.filter_by(saida_id=saida_id).one_or_none()
    if estado is None:
        db.session.add(
            EstadoEntregaCanalSaida(
                saida_id=saida_id,
                versao=0,
                status_entrega=origem.status_entrega,
                provider_status_em=origem.provider_status_em,
                sent_em=origem.sent_em,
                delivered_em=origem.delivered_em,
                read_em=origem.read_em,
                failed_em=origem.failed_em,
                codigo_falha_entrega=origem.codigo_falha_entrega,
                classificacao_resultado=origem.classificacao_resultado,
            )
        )
        db.session.flush()
        return
    if _entrega_igual(estado, origem):
        return
    projecao = _Projecao(
        mudou=True,
        avancou=True,
        status_entrega=origem.status_entrega,
        provider_status_em=origem.provider_status_em,
        sent_em=origem.sent_em,
        delivered_em=origem.delivered_em,
        read_em=origem.read_em,
        failed_em=origem.failed_em,
        codigo_falha_entrega=origem.codigo_falha_entrega,
        classificacao_resultado=origem.classificacao_resultado,
    )
    _gravar_estado(EstadoEntregaCanalSaida, estado, projecao)


def _aplicar(
    saida_id: int,
    status: str,
    quando: datetime,
    tentativa_id: int | None = None,
) -> tuple[str, str | None]:
    if tentativa_id is None:
        return _aplicar_estado_saida(saida_id, status, quando)
    codigo, status_entrega = _aplicar_estado_tentativa(
        tentativa_id,
        saida_id,
        status,
        quando,
    )
    if _tentativa_e_recente(tentativa_id, saida_id):
        _espelhar_entrega_na_saida(saida_id, tentativa_id)
    return codigo, status_entrega


def _marcar_evento(evento_id: int, codigo: str) -> None:
    destino = _DESTINO_EVENTO.get(codigo)
    if destino is None:
        return
    db.session.execute(
        update(EventoCanalRecebido)
        .where(EventoCanalRecebido.id == evento_id)
        .where(
            EventoCanalRecebido.status_processamento.in_(
                (
                    EventoCanalRecebido.STATUS_RECEBIDO,
                    EventoCanalRecebido.STATUS_ROTEADO,
                    EventoCanalRecebido.STATUS_IGNORADO,
                )
            )
        )
        .values(status_processamento=destino)
        .execution_options(synchronize_session="fetch")
    )


def _de_aplicacao(row: AplicacaoStatusCanal) -> ResultadoReconciliacaoStatus:
    status = None
    classificacao = None
    estado = None
    if row.tentativa_envio_id is not None:
        estado = EstadoEntregaTentativaCanal.query.filter_by(
            tentativa_envio_id=int(row.tentativa_envio_id)
        ).one_or_none()
    if estado is None and row.saida_id is not None:
        estado = EstadoEntregaCanalSaida.query.filter_by(saida_id=int(row.saida_id)).one_or_none()
    if estado is not None:
        status = estado.status_entrega
        classificacao = estado.classificacao_resultado
    return _resultado(
        CODIGO_JA_TRATADO,
        evento_id=int(row.evento_recebido_id),
        saida_id=int(row.saida_id) if row.saida_id is not None else None,
        status_entrega=status,
        correlation_id=row.correlation_id,
        provider_status_em=row.provider_status_em,
        classificacao_resultado=classificacao,
    )


def _ultima_aplicacao(evento_id: int) -> AplicacaoStatusCanal | None:
    return (
        AplicacaoStatusCanal.query.filter_by(evento_recebido_id=evento_id)
        .order_by(
            AplicacaoStatusCanal.numero_tentativa.desc(),
            AplicacaoStatusCanal.id.desc(),
        )
        .first()
    )


def _aplicacao_recuperada(evento_id: int) -> AplicacaoStatusCanal | None:
    return (
        AplicacaoStatusCanal.query.filter(
            AplicacaoStatusCanal.evento_recebido_id == evento_id,
            AplicacaoStatusCanal.resultado.in_(tuple(_RECUPERADOS)),
        )
        .order_by(
            AplicacaoStatusCanal.numero_tentativa.desc(),
            AplicacaoStatusCanal.id.desc(),
        )
        .first()
    )


def _ja_persistido(evento_id: int) -> ResultadoReconciliacaoStatus | None:
    row = _ultima_aplicacao(evento_id)
    if row is None:
        return None
    return _de_aplicacao(row)


def _eh_unicidade(exc: IntegrityError) -> bool:
    texto = str(getattr(exc, "orig", exc)).lower()
    return "unique" in texto


def _persistir(
    evento: EventoCanalRecebido,
    *,
    codigo: str,
    saida_id: int | None,
    quando: datetime | None,
    status_entrega: str | None,
    numero_tentativa: int = 1,
) -> ResultadoReconciliacaoStatus:
    correlation = _correlation(evento)
    with db.session.begin_nested():
        db.session.add(
            AplicacaoStatusCanal(
                evento_recebido_id=int(evento.id),
                numero_tentativa=numero_tentativa,
                saida_id=saida_id,
                resultado=codigo,
                provider_status_em=quando,
                correlation_id=correlation,
                criado_em=utcnow_naive(),
            )
        )
        _marcar_evento(int(evento.id), codigo)
        db.session.flush()
    db.session.commit()
    return _resultado(
        codigo,
        evento_id=int(evento.id),
        saida_id=saida_id,
        status_entrega=status_entrega,
        correlation_id=correlation,
        provider_status_em=quando,
    )


def _persistir_efeito(
    evento: EventoCanalRecebido,
    saida: EventoCanalSaida,
    componentes: _Componentes,
    *,
    tentativa: TentativaEnvioCanal | None = None,
    reaplicar: bool = False,
) -> ResultadoReconciliacaoStatus:
    if componentes.quando is None or not componentes.status:
        if reaplicar:
            evento_id = int(evento.id)
            correlation = _correlation(evento)
            db.session.rollback()
            return _resultado(
                CODIGO_REAPLICACAO_NAO_PERMITIDA,
                evento_id=evento_id,
                correlation_id=correlation,
            )
        return _persistir(
            evento,
            codigo=CODIGO_ILEGIVEL,
            saida_id=None,
            quando=None,
            status_entrega=None,
        )
    with _trava(_travas_saida, int(saida.id)):
        if reaplicar:
            recuperada = _aplicacao_recuperada(int(evento.id))
            if recuperada is not None:
                aplicacao_id = int(recuperada.id)
                db.session.rollback()
                row = db.session.get(AplicacaoStatusCanal, aplicacao_id)
                if row is not None:
                    return _de_aplicacao(row)
                return _resultado(
                    CODIGO_JA_TRATADO,
                    evento_id=int(evento.id),
                    saida_id=int(saida.id),
                )
            ultima = _ultima_aplicacao(int(evento.id))
            if ultima is None or ultima.resultado != CODIGO_SEM_SAIDA:
                return _terminal_ja_gravado(int(evento.id), ultima)
            numero = int(ultima.numero_tentativa) + 1
        else:
            tratado = _ja_persistido(int(evento.id))
            if tratado is not None:
                return tratado
            numero = 1
        correlation = _correlation(evento)
        tentativa_id = None if tentativa is None else int(tentativa.id)
        with db.session.begin_nested():
            codigo, status_entrega = _aplicar(
                int(saida.id),
                componentes.status,
                componentes.quando,
                tentativa_id=tentativa_id,
            )
            db.session.add(
                AplicacaoStatusCanal(
                    evento_recebido_id=int(evento.id),
                    numero_tentativa=numero,
                    saida_id=int(saida.id),
                    resultado=codigo,
                    provider_status_em=componentes.quando,
                    correlation_id=correlation,
                    criado_em=utcnow_naive(),
                    tentativa_envio_id=tentativa_id,
                )
            )
            _marcar_evento(int(evento.id), codigo)
            db.session.flush()
        db.session.commit()
        return _resultado(
            codigo,
            evento_id=int(evento.id),
            saida_id=int(saida.id),
            status_entrega=status_entrega,
            correlation_id=correlation,
            provider_status_em=componentes.quando,
        )


def _tentar(evento_id: int) -> ResultadoReconciliacaoStatus:
    tratado = _ja_persistido(evento_id)
    if tratado is not None:
        return tratado
    evento = db.session.get(EventoCanalRecebido, evento_id)
    if evento is None:
        return _resultado(CODIGO_EVENTO_AUSENTE)
    correlation = _correlation(evento)
    if evento.tipo_evento != EventoCanalRecebido.TIPO_STATUS_ENTREGA:
        return _resultado(
            CODIGO_EVENTO_NAO_STATUS,
            evento_id=evento_id,
            correlation_id=correlation,
        )
    if evento.provider != EventoCanalRecebido.PROVIDER_META_WHATSAPP:
        return _persistir(
            evento,
            codigo=CODIGO_ILEGIVEL,
            saida_id=None,
            quando=None,
            status_entrega=None,
        )
    componentes = _extrair(evento)
    if componentes.ilegivel:
        return _persistir(
            evento,
            codigo=CODIGO_ILEGIVEL,
            saida_id=None,
            quando=None,
            status_entrega=None,
        )
    if not componentes.suportado:
        return _persistir(
            evento,
            codigo=CODIGO_STATUS_IGNORADO,
            saida_id=None,
            quando=componentes.quando,
            status_entrega=None,
        )
    resolucao, saida, tentativa = _resolver_mensagem(componentes.mensagem_id)
    if resolucao == "sem":
        return _persistir(
            evento,
            codigo=CODIGO_SEM_SAIDA,
            saida_id=None,
            quando=componentes.quando,
            status_entrega=None,
        )
    if resolucao == "ambigua" or saida is None:
        return _persistir(
            evento,
            codigo=CODIGO_SAIDA_AMBIGUA,
            saida_id=None,
            quando=componentes.quando,
            status_entrega=None,
        )
    return _persistir_efeito(evento, saida, componentes, tentativa=tentativa)


def _executar(evento_id: int) -> ResultadoReconciliacaoStatus:
    correlation = None
    for _tentativa in range(3):
        try:
            return _tentar(evento_id)
        except _Conflito:
            db.session.rollback()
        except IntegrityError as exc:
            db.session.rollback()
            if not _eh_unicidade(exc):
                evento = db.session.get(EventoCanalRecebido, evento_id)
                if evento is not None:
                    correlation = _correlation(evento)
                return _resultado(
                    CODIGO_ERRO_SEGURO,
                    evento_id=evento_id,
                    correlation_id=correlation,
                )
            tratado = _ja_persistido(evento_id)
            if tratado is not None:
                return tratado
    tratado = _ja_persistido(evento_id)
    if tratado is not None:
        return tratado
    evento = db.session.get(EventoCanalRecebido, evento_id)
    if evento is not None:
        correlation = _correlation(evento)
    return _resultado(
        CODIGO_EM_PROCESSAMENTO,
        evento_id=evento_id,
        correlation_id=correlation,
    )


def _recusa_orfao(
    evento: EventoCanalRecebido | None,
    evento_id: int,
) -> ResultadoReconciliacaoStatus:
    correlation = None if evento is None else _correlation(evento)
    return _resultado(
        CODIGO_REAPLICACAO_NAO_PERMITIDA,
        evento_id=evento_id,
        correlation_id=correlation,
    )


def _query_evento_exclusivo(evento_id: int):
    """Lock pessimista da linha do evento. PostgreSQL espera; SQLite ignora."""
    return (
        db.session.query(EventoCanalRecebido)
        .filter(EventoCanalRecebido.id == int(evento_id))
        .with_for_update()
    )


def _bloquear_evento(evento_id: int) -> EventoCanalRecebido | None:
    return _query_evento_exclusivo(evento_id).one_or_none()


def _terminal_ja_gravado(
    evento_id: int,
    ultima: AplicacaoStatusCanal | None,
) -> ResultadoReconciliacaoStatus:
    """Solta o lock e devolve o desfecho já persistido, sem nova tentativa."""
    if ultima is None:
        db.session.rollback()
        evento = db.session.get(EventoCanalRecebido, evento_id)
        return _recusa_orfao(evento, evento_id)
    resultado = ultima.resultado
    aplicacao_id = int(ultima.id)
    provider_em = ultima.provider_status_em
    correlation = ultima.correlation_id
    db.session.rollback()
    if resultado in _RECUPERADOS:
        row = db.session.get(AplicacaoStatusCanal, aplicacao_id)
        if row is not None:
            return _de_aplicacao(row)
        return _resultado(CODIGO_JA_TRATADO, evento_id=evento_id)
    if resultado == CODIGO_SAIDA_AMBIGUA:
        return _resultado(
            CODIGO_SAIDA_AMBIGUA,
            evento_id=evento_id,
            correlation_id=correlation,
            provider_status_em=provider_em,
        )
    evento = db.session.get(EventoCanalRecebido, evento_id)
    return _recusa_orfao(evento, evento_id)


def _ja_terminal(evento_id: int) -> ResultadoReconciliacaoStatus | None:
    recuperada = _aplicacao_recuperada(evento_id)
    if recuperada is not None:
        return _de_aplicacao(recuperada)
    ultima = _ultima_aplicacao(evento_id)
    if ultima is None or ultima.resultado == CODIGO_SEM_SAIDA:
        return None
    if ultima.resultado == CODIGO_SAIDA_AMBIGUA:
        return _resultado(
            CODIGO_SAIDA_AMBIGUA,
            evento_id=evento_id,
            correlation_id=ultima.correlation_id,
            provider_status_em=ultima.provider_status_em,
        )
    evento = db.session.get(EventoCanalRecebido, evento_id)
    return _recusa_orfao(evento, evento_id)


def _saidas_da_mensagem(mensagem_id: str) -> list[EventoCanalSaida]:
    return EventoCanalSaida.query.filter_by(
        provider=EventoCanalSaida.PROVIDER_META_WHATSAPP,
        provider_message_id=mensagem_id,
    ).all()


def _resolver_mensagem(
    mensagem_id: str | None,
) -> tuple[str, EventoCanalSaida | None, TentativaEnvioCanal | None]:
    """Tentativa pelo id do provider; a saída sai da tentativa.

    Sem tentativa compatível, vale o id gravado na saída. Várias
    tentativas com o mesmo id não escolhem a mais recente.
    """
    if not mensagem_id:
        return "sem", None, None
    tentativas = TentativaEnvioCanal.query.filter(
        TentativaEnvioCanal.provider == EventoCanalSaida.PROVIDER_META_WHATSAPP,
        TentativaEnvioCanal.provider_message_id == mensagem_id,
    ).all()
    if len(tentativas) > 1:
        return "ambigua", None, None
    if len(tentativas) == 1:
        tentativa = tentativas[0]
        saida = db.session.get(EventoCanalSaida, int(tentativa.saida_id))
        if saida is None or saida.provider != EventoCanalSaida.PROVIDER_META_WHATSAPP:
            return "sem", None, None
        return "unica", saida, tentativa
    saidas = _saidas_da_mensagem(mensagem_id)
    if not saidas:
        return "sem", None, None
    if len(saidas) != 1:
        return "ambigua", None, None
    return "unica", saidas[0], None


def _reaplicar_sob_exclusao(evento_id: int) -> ResultadoReconciliacaoStatus:
    """Decide e grava a tentativa seguinte com o evento bloqueado.

    A releitura acontece depois do lock. Se a última tentativa deixou de
    ser sem_saida, não há insert nem mudança de entrega. O número seguinte
    sai só dessa linha revalidada.
    """
    evento = _bloquear_evento(evento_id)
    if evento is None:
        db.session.rollback()
        return _resultado(CODIGO_EVENTO_AUSENTE)
    if evento.tipo_evento != EventoCanalRecebido.TIPO_STATUS_ENTREGA:
        correlation = _correlation(evento)
        db.session.rollback()
        return _resultado(
            CODIGO_EVENTO_NAO_STATUS,
            evento_id=evento_id,
            correlation_id=correlation,
        )
    if evento.provider != EventoCanalRecebido.PROVIDER_META_WHATSAPP:
        correlation = _correlation(evento)
        db.session.rollback()
        return _resultado(
            CODIGO_REAPLICACAO_NAO_PERMITIDA,
            evento_id=evento_id,
            correlation_id=correlation,
        )
    ultima = _ultima_aplicacao(evento_id)
    if ultima is None or ultima.resultado != CODIGO_SEM_SAIDA:
        return _terminal_ja_gravado(evento_id, ultima)
    componentes = _extrair(evento)
    if (
        componentes.ilegivel
        or not componentes.suportado
        or componentes.quando is None
        or not componentes.mensagem_id
    ):
        correlation = _correlation(evento)
        db.session.rollback()
        return _resultado(
            CODIGO_REAPLICACAO_NAO_PERMITIDA,
            evento_id=evento_id,
            correlation_id=correlation,
        )
    numero = int(ultima.numero_tentativa) + 1
    resolucao, saida, tentativa = _resolver_mensagem(componentes.mensagem_id)
    if resolucao == "sem" or saida is None and resolucao != "ambigua":
        correlation = _correlation(evento)
        quando = ultima.provider_status_em
        db.session.rollback()
        return _resultado(
            CODIGO_AINDA_SEM_SAIDA,
            evento_id=evento_id,
            correlation_id=correlation,
            provider_status_em=quando,
        )
    if resolucao == "ambigua" or saida is None:
        return _persistir(
            evento,
            codigo=CODIGO_SAIDA_AMBIGUA,
            saida_id=None,
            quando=componentes.quando,
            status_entrega=None,
            numero_tentativa=numero,
        )
    return _persistir_efeito(
        evento,
        saida,
        componentes,
        tentativa=tentativa,
        reaplicar=True,
    )


def _tentar_orfao(evento_id: int) -> ResultadoReconciliacaoStatus:
    evento = db.session.get(EventoCanalRecebido, evento_id)
    if evento is None:
        return _resultado(CODIGO_EVENTO_AUSENTE)
    correlation = _correlation(evento)
    if evento.tipo_evento != EventoCanalRecebido.TIPO_STATUS_ENTREGA:
        return _resultado(
            CODIGO_EVENTO_NAO_STATUS,
            evento_id=evento_id,
            correlation_id=correlation,
        )
    if evento.provider != EventoCanalRecebido.PROVIDER_META_WHATSAPP:
        return _recusa_orfao(evento, evento_id)
    recuperada = _aplicacao_recuperada(evento_id)
    if recuperada is not None:
        return _de_aplicacao(recuperada)
    ultima = _ultima_aplicacao(evento_id)
    if ultima is not None and ultima.resultado == CODIGO_SAIDA_AMBIGUA:
        return _resultado(
            CODIGO_SAIDA_AMBIGUA,
            evento_id=evento_id,
            correlation_id=ultima.correlation_id,
            provider_status_em=ultima.provider_status_em,
        )
    if ultima is None or ultima.resultado != CODIGO_SEM_SAIDA:
        return _recusa_orfao(evento, evento_id)
    componentes = _extrair(evento)
    if (
        componentes.ilegivel
        or not componentes.suportado
        or componentes.quando is None
        or not componentes.mensagem_id
    ):
        return _recusa_orfao(evento, evento_id)
    resolucao, _saida, _tentativa = _resolver_mensagem(componentes.mensagem_id)
    if resolucao == "sem":
        return _resultado(
            CODIGO_AINDA_SEM_SAIDA,
            evento_id=evento_id,
            correlation_id=correlation,
            provider_status_em=ultima.provider_status_em,
        )
    return _reaplicar_sob_exclusao(evento_id)


def _executar_orfao(evento_id: int) -> ResultadoReconciliacaoStatus:
    correlation = None
    for _tentativa in range(3):
        try:
            return _tentar_orfao(evento_id)
        except _Conflito:
            db.session.rollback()
        except IntegrityError as exc:
            db.session.rollback()
            if not _eh_unicidade(exc):
                evento = db.session.get(EventoCanalRecebido, evento_id)
                if evento is not None:
                    correlation = _correlation(evento)
                return _resultado(
                    CODIGO_ERRO_SEGURO,
                    evento_id=evento_id,
                    correlation_id=correlation,
                )
            terminal = _ja_terminal(evento_id)
            if terminal is not None:
                return terminal
    terminal = _ja_terminal(evento_id)
    if terminal is not None:
        return terminal
    evento = db.session.get(EventoCanalRecebido, evento_id)
    if evento is not None:
        correlation = _correlation(evento)
    return _resultado(
        CODIGO_EM_PROCESSAMENTO,
        evento_id=evento_id,
        correlation_id=correlation,
    )


def reconciliar_status_saida(evento_recebido_id: int) -> ResultadoReconciliacaoStatus:
    """Aplica um status_entrega já gravado. Não relança erro de domínio."""
    if not _id_valido(evento_recebido_id):
        resultado = _resultado(CODIGO_EVENTO_INVALIDO)
        _log(resultado)
        return resultado
    with _trava(_travas_evento, evento_recebido_id):
        resultado = _executar(evento_recebido_id)
    _log(resultado)
    return resultado


def reconciliar_status_orfao(evento_recebido_id: int) -> ResultadoReconciliacaoStatus:
    """Reavalia um status que terminou em sem_saida.

    Só segue quando a última tentativa desse evento é sem_saida.
    A decisão e o insert da tentativa seguinte usam o mesmo lock da
    linha do evento. A primeira linha permanece.
    Não busca órfãos ao gravar provider_message_id.
    """
    if not _id_valido(evento_recebido_id):
        resultado = _resultado(CODIGO_EVENTO_INVALIDO)
        _log(resultado)
        return resultado
    with _trava(_travas_evento, evento_recebido_id):
        resultado = _executar_orfao(evento_recebido_id)
    _log(resultado)
    return resultado


def _estado_incerto(saida_id: int) -> ResultadoReconciliacaoStatus:
    row = EstadoEntregaCanalSaida.query.filter_by(saida_id=saida_id).one_or_none()
    classificacao = None if row is None else row.classificacao_resultado
    status = None if row is None else row.status_entrega
    return _resultado(
        CODIGO_JA_CLASSIFICADO,
        saida_id=saida_id,
        status_entrega=status,
        classificacao_resultado=classificacao,
    )


def classificar_resultado_incerto(saida_id: int) -> ResultadoReconciliacaoStatus:
    """Marca reserva ou preparo de link sem id do provider.

    Não observa relógio e não dispara sozinha. A saída permanece no
    status_envio em que estava.
    """
    if not _id_valido(saida_id):
        resultado = _resultado(CODIGO_SAIDA_INVALIDA)
        _log(resultado)
        return resultado
    with _trava(_travas_saida, saida_id):
        saida = db.session.get(EventoCanalSaida, saida_id)
        if saida is None:
            resultado = _resultado(CODIGO_SAIDA_AUSENTE)
            _log(resultado)
            return resultado
        if (
            saida.status_envio not in _ESTADOS_INCERTOS
            or saida.provider_message_id is not None
        ):
            resultado = _resultado(
                CODIGO_CLASSIFICACAO_NAO_APLICAVEL,
                saida_id=saida_id,
            )
            _log(resultado)
            return resultado
        estado = EstadoEntregaCanalSaida.query.filter_by(saida_id=saida_id).one_or_none()
        if (
            estado is not None
            and estado.classificacao_resultado == CODIGO_CLASSIFICADO
            and estado.status_entrega is None
        ):
            resultado = _estado_incerto(saida_id)
            _log(resultado)
            return resultado
        if estado is not None:
            resultado = _resultado(
                CODIGO_CLASSIFICACAO_NAO_APLICAVEL,
                saida_id=saida_id,
                status_entrega=estado.status_entrega,
            )
            _log(resultado)
            return resultado
        try:
            with db.session.begin_nested():
                db.session.add(
                    EstadoEntregaCanalSaida(
                        saida_id=saida_id,
                        versao=0,
                        status_entrega=None,
                        provider_status_em=None,
                        sent_em=None,
                        delivered_em=None,
                        read_em=None,
                        failed_em=None,
                        codigo_falha_entrega=None,
                        classificacao_resultado=CODIGO_CLASSIFICADO,
                    )
                )
                db.session.flush()
                refletir_resultado_incerto(saida_id)
        except IntegrityError:
            db.session.rollback()
            estado = EstadoEntregaCanalSaida.query.filter_by(saida_id=saida_id).one_or_none()
            if (
                estado is not None
                and estado.classificacao_resultado == CODIGO_CLASSIFICADO
                and estado.status_entrega is None
            ):
                resultado = _estado_incerto(saida_id)
            else:
                resultado = _resultado(
                    CODIGO_CLASSIFICACAO_NAO_APLICAVEL,
                    saida_id=saida_id,
                    status_entrega=None if estado is None else estado.status_entrega,
                )
            _log(resultado)
            return resultado
        db.session.commit()
        resultado = _resultado(
            CODIGO_CLASSIFICADO,
            saida_id=saida_id,
            classificacao_resultado=CODIGO_CLASSIFICADO,
        )
    _log(resultado)
    return resultado
