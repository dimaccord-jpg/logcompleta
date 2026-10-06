"""Tentativa 1 de uma saída de canal já persistida.

EventoCanalSaida continua sendo a saída lógica. Esta tabela guarda a
execução técnica. Este módulo cria e sincroniza só a tentativa 1.
A recuperação manual de texto comum não passa por aqui.

Não envia de novo e não emite outro segredo.
"""
from __future__ import annotations

import logging

from sqlalchemy.exc import IntegrityError, OperationalError

from app.extensions import db
from app.models import (
    EstadoEntregaCanalSaida,
    EventoCanalSaida,
    InterpretacaoConversacionalCanal,
    OnboardingCanal,
    TentativaEnvioCanal,
    utcnow_naive,
)
from app.services.canal_interpretacao_conversacional_service import (
    ACAO_EMITIR_SENHA,
    ACAO_ORIENTAR_CONTA,
    CODIGO_CONTA_EXISTENTE,
    CODIGO_LINK_EMITIDO,
    CODIGO_LINK_NAO_EMITIDO,
)

logger = logging.getLogger(__name__)

_ACOES_LINK = frozenset({ACAO_EMITIR_SENHA, ACAO_ORIENTAR_CONTA})
_CODIGOS_LINK = frozenset(
    {CODIGO_LINK_EMITIDO, CODIGO_LINK_NAO_EMITIDO, CODIGO_CONTA_EXISTENTE}
)
_STATUS_DA_SAIDA = {
    EventoCanalSaida.STATUS_RESERVADO: TentativaEnvioCanal.STATUS_RESERVADA,
    EventoCanalSaida.STATUS_AGUARDANDO_LINK: TentativaEnvioCanal.STATUS_AGUARDANDO_LINK,
    EventoCanalSaida.STATUS_PREPARANDO_LINK: TentativaEnvioCanal.STATUS_PREPARANDO,
    EventoCanalSaida.STATUS_ACEITO: TentativaEnvioCanal.STATUS_ACEITA,
    EventoCanalSaida.STATUS_ERRO: TentativaEnvioCanal.STATUS_ERRO,
}


def interpretacao_de_link(interpretacao: InterpretacaoConversacionalCanal) -> bool:
    """Mesma decisão estruturada do envio: conclusão, ação, código ou etapa."""
    if interpretacao.conclusao_id is not None:
        return True
    if interpretacao.acao in _ACOES_LINK:
        return True
    if interpretacao.codigo in _CODIGOS_LINK:
        return True
    return interpretacao.etapa_final == OnboardingCanal.ETAPA_SENHA


def tipo_para_saida(
    saida: EventoCanalSaida,
    interpretacao: InterpretacaoConversacionalCanal | None,
) -> str:
    if saida.status_envio in (
        EventoCanalSaida.STATUS_AGUARDANDO_LINK,
        EventoCanalSaida.STATUS_PREPARANDO_LINK,
    ):
        return TentativaEnvioCanal.TIPO_LINK_SEGURO
    if saida.conclusao_id is not None:
        return TentativaEnvioCanal.TIPO_LINK_SEGURO
    if interpretacao is not None and interpretacao_de_link(interpretacao):
        return TentativaEnvioCanal.TIPO_LINK_SEGURO
    return TentativaEnvioCanal.TIPO_TEXTO_COMUM


def status_da_saida(status_envio: str) -> str:
    return _STATUS_DA_SAIDA[status_envio]


def _log(tentativa: TentativaEnvioCanal) -> None:
    logger.info(
        "tentativa_envio_canal saida_id=%s tentativa_id=%s correlation_id=%s "
        "status=%s codigo=%s numero=%s",
        tentativa.saida_id,
        tentativa.id if tentativa.id is not None else "-",
        tentativa.correlation_id,
        tentativa.status_tentativa,
        tentativa.codigo_erro or tentativa.classificacao_resultado or "-",
        tentativa.numero_tentativa,
    )


def _buscar_inicial(saida_id: int) -> TentativaEnvioCanal | None:
    return TentativaEnvioCanal.query.filter_by(
        saida_id=int(saida_id),
        numero_tentativa=TentativaEnvioCanal.NUMERO_INICIAL,
    ).one_or_none()


def _conclusao_da_tentativa(saida: EventoCanalSaida, tipo: str) -> int | None:
    if tipo != TentativaEnvioCanal.TIPO_LINK_SEGURO or saida.conclusao_id is None:
        return None
    return int(saida.conclusao_id)


def _classificacao_conhecida(saida: EventoCanalSaida, status: str) -> str | None:
    if status not in (
        TentativaEnvioCanal.STATUS_RESERVADA,
        TentativaEnvioCanal.STATUS_PREPARANDO,
    ):
        return None
    if saida.provider_message_id is not None:
        return None
    estado = EstadoEntregaCanalSaida.query.filter_by(saida_id=int(saida.id)).one_or_none()
    if (
        estado is not None
        and estado.classificacao_resultado
        == TentativaEnvioCanal.CLASSIFICACAO_RESULTADO_INCERTO
        and estado.status_entrega is None
    ):
        return TentativaEnvioCanal.CLASSIFICACAO_RESULTADO_INCERTO
    return None


def nova_tentativa(saida: EventoCanalSaida, *, tipo: str) -> TentativaEnvioCanal:
    status = status_da_saida(saida.status_envio)
    enviado_em = saida.enviado_em if status == TentativaEnvioCanal.STATUS_ACEITA else None
    return TentativaEnvioCanal(
        saida_id=int(saida.id),
        numero_tentativa=TentativaEnvioCanal.NUMERO_INICIAL,
        tipo_tentativa=tipo,
        origem_tentativa=TentativaEnvioCanal.ORIGEM_ENVIO_INICIAL,
        status_tentativa=status,
        codigo_erro=saida.codigo_erro if status == TentativaEnvioCanal.STATUS_ERRO else None,
        provider=saida.provider,
        provider_message_id=(
            saida.provider_message_id if status == TentativaEnvioCanal.STATUS_ACEITA else None
        ),
        conclusao_id=_conclusao_da_tentativa(saida, tipo),
        criado_em=saida.criado_em,
        preparado_em=None,
        enviado_em=enviado_em,
        finalizado_em=enviado_em,
        correlation_id=saida.correlation_id,
        classificacao_resultado=_classificacao_conhecida(saida, status),
    )


def anexar_tentativa_inicial(saida: EventoCanalSaida) -> None:
    """Inclui a tentativa 1 na transação corrente. Não faz commit."""
    interpretacao = None
    if saida.interpretacao_id is not None:
        interpretacao = db.session.get(
            InterpretacaoConversacionalCanal,
            int(saida.interpretacao_id),
        )
    tentativa = nova_tentativa(saida, tipo=tipo_para_saida(saida, interpretacao))
    db.session.add(tentativa)
    _log(tentativa)


def sincronizar_tentativa_com_saida(saida: EventoCanalSaida) -> None:
    """Copia o resultado técnico já gravado na saída para a tentativa 1.

    O objeto da saída precisa estar com os valores novos. Quem alterou
    a saída por UPDATE direto recarrega a linha antes de chamar.
    Se já existe tentativa posterior, a tentativa 1 permanece como foi.
    """
    posteriores = TentativaEnvioCanal.query.filter(
        TentativaEnvioCanal.saida_id == int(saida.id),
        TentativaEnvioCanal.numero_tentativa > TentativaEnvioCanal.NUMERO_INICIAL,
    ).count()
    if posteriores:
        return
    tentativa = _buscar_inicial(int(saida.id))
    if tentativa is None:
        return
    status = status_da_saida(saida.status_envio)
    tentativa.status_tentativa = status
    tentativa.provider = saida.provider
    tentativa.correlation_id = saida.correlation_id
    if tentativa.tipo_tentativa == TentativaEnvioCanal.TIPO_LINK_SEGURO:
        tentativa.conclusao_id = _conclusao_da_tentativa(
            saida,
            TentativaEnvioCanal.TIPO_LINK_SEGURO,
        )
    else:
        tentativa.conclusao_id = None
    if status == TentativaEnvioCanal.STATUS_ACEITA:
        tentativa.provider_message_id = saida.provider_message_id
        tentativa.codigo_erro = None
        tentativa.enviado_em = saida.enviado_em
        tentativa.finalizado_em = saida.enviado_em
    elif status == TentativaEnvioCanal.STATUS_ERRO:
        tentativa.provider_message_id = None
        tentativa.codigo_erro = saida.codigo_erro
        tentativa.enviado_em = None
        if tentativa.finalizado_em is None:
            tentativa.finalizado_em = utcnow_naive()
    elif status == TentativaEnvioCanal.STATUS_PREPARANDO:
        tentativa.provider_message_id = None
        tentativa.codigo_erro = None
        tentativa.enviado_em = None
        tentativa.finalizado_em = None
        if tentativa.preparado_em is None:
            tentativa.preparado_em = utcnow_naive()
    else:
        tentativa.provider_message_id = None
        tentativa.codigo_erro = None
        tentativa.enviado_em = None
        tentativa.finalizado_em = None
        tentativa.preparado_em = None
    if status not in (
        TentativaEnvioCanal.STATUS_RESERVADA,
        TentativaEnvioCanal.STATUS_PREPARANDO,
    ):
        tentativa.classificacao_resultado = None
    _log(tentativa)


def espelhar_tentativa_persistida(saida_id: int) -> None:
    """Relê a saída depois de um UPDATE e espelha a tentativa 1."""
    saida = db.session.get(EventoCanalSaida, saida_id)
    if saida is None:
        return
    db.session.refresh(saida)
    sincronizar_tentativa_com_saida(saida)


def refletir_resultado_incerto(saida_id: int) -> None:
    """Marca a evidência na tentativa 1, se ela já existir.

    Não cria tentativa, não muda o status da execução e não dispara envio.
    """
    alteradas = (
        TentativaEnvioCanal.query.filter_by(
            saida_id=int(saida_id),
            numero_tentativa=TentativaEnvioCanal.NUMERO_INICIAL,
        )
        .filter(
            TentativaEnvioCanal.status_tentativa.in_(
                (
                    TentativaEnvioCanal.STATUS_RESERVADA,
                    TentativaEnvioCanal.STATUS_PREPARANDO,
                )
            ),
            TentativaEnvioCanal.provider_message_id.is_(None),
            TentativaEnvioCanal.codigo_erro.is_(None),
            TentativaEnvioCanal.enviado_em.is_(None),
            TentativaEnvioCanal.classificacao_resultado.is_(None),
        )
        .update(
            {
                "classificacao_resultado": (
                    TentativaEnvioCanal.CLASSIFICACAO_RESULTADO_INCERTO
                )
            },
            synchronize_session="fetch",
        )
    )
    if alteradas:
        logger.info(
            "tentativa_envio_canal saida_id=%s status=%s codigo=%s numero=%s",
            saida_id,
            "-",
            TentativaEnvioCanal.CLASSIFICACAO_RESULTADO_INCERTO,
            TentativaEnvioCanal.NUMERO_INICIAL,
        )


def garantir_tentativa_inicial(saida_id: int) -> TentativaEnvioCanal | None:
    """Garante uma única tentativa 1 para uma saída já confirmada.

    Duas chamadas concorrentes deixam uma linha. A unicidade do banco
    absorve a corrida. IntegrityError não sai daqui.
    """
    if isinstance(saida_id, bool) or not isinstance(saida_id, int) or saida_id <= 0:
        return None
    for _ in range(3):
        existente = _buscar_inicial(saida_id)
        if existente is not None:
            return existente
        saida = db.session.get(EventoCanalSaida, saida_id)
        if saida is None:
            return None
        try:
            anexar_tentativa_inicial(saida)
            db.session.flush()
            tentativa_id = int(_buscar_inicial(saida_id).id)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            continue
        except OperationalError as exc:
            db.session.rollback()
            if "locked" not in str(exc).lower():
                raise
            continue
        return db.session.get(TentativaEnvioCanal, tentativa_id)
    return _buscar_inicial(saida_id)
