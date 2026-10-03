"""Orquestra o pipeline já existente de um EventoCanalRecebido.

Não interpreta, não monta pedido da Cloud API, não emite token e não
reconcilia status por conta própria. Cada etapa permanece no serviço
que já a possui. Usuário vinculado segue o chat operacional já existente.
Não há fila, worker nem repetição automática de envio.

Latência do webhook
-------------------
A aplicação não tem fila. Depois que o lote 3A confirma a persistência,
a rota chama este módulo no mesmo processo e só então devolve o HTTP.
A resposta espera a entrada, a interpretação e, quando cabível, o POST
já implementado.

Se o processo cair depois do commit, o estado gravado permanece.
Um replay do mesmo evento externo não cria outra linha. A rota só
chama de novo este módulo quando os artefatos já persistidos ainda
têm uma etapa segura seguinte. Evento concluído não entra de novo.

Saída ``reservado``, ``preparando_link`` e resultado externo já
encerrado permanecem como estão: nenhuma chamada nova à Meta.
Uma segunda chamada direta a ``processar_evento_whatsapp`` reusa a
interpretação e a saída já gravadas e não faz outro POST.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.extensions import db
from app.models import (
    AplicacaoStatusCanal,
    EventoCanalRecebido,
    EventoCanalSaida,
    ExecucaoOperacionalCanal,
    InterpretacaoConversacionalCanal,
)
from app.services.canal_entrada_processamento_service import (
    CODIGO_JA_PROCESSADO,
    CODIGO_ROTEADO,
    ROTA_GUEST,
    ROTA_ONBOARDING,
    ROTA_USUARIO_VINCULADO,
    processar_evento_canal_recebido,
    reconstruir_roteamento_canal,
)
from app.services.canal_interpretacao_conversacional_service import (
    ACAO_RESERVADA,
    CODIGO_EM_TRATAMENTO,
    CODIGO_EVENTO_AUSENTE as CODIGO_INTERPRETACAO_AUSENTE,
    CODIGO_EVENTO_NAO_ROTEADO,
    CODIGO_ROTEAMENTO_INDISPONIVEL,
    interpretar_mensagem_canal,
)
from app.services.canal_operacao_whatsapp_service import (
    executar_operacao_canal,
    operacao_canal_pendente,
)
from app.services.canal_reconciliacao_status_service import reconciliar_status_saida
from app.services.canal_saida_link_seguro_service import entregar_link_seguro
from app.services.canal_saida_whatsapp_service import enviar_texto_da_interpretacao

logger = logging.getLogger(__name__)

CODIGO_EVENTO_INVALIDO = "evento_invalido"
CODIGO_EVENTO_AUSENTE = "evento_ausente"
CODIGO_ERRO_TECNICO = "erro_tecnico"

ETAPA_ENTRADA = "entrada"
ETAPA_INTERPRETACAO = "interpretacao"
ETAPA_OPERACIONAL = "operacional"
ETAPA_SAIDA = "saida"
ETAPA_LINK = "link_seguro"
ETAPA_RECONCILIACAO = "reconciliacao"

_SEM_RETORNO = frozenset(
    {
        CODIGO_ROTEAMENTO_INDISPONIVEL,
        CODIGO_EVENTO_NAO_ROTEADO,
        CODIGO_INTERPRETACAO_AUSENTE,
    }
)
_ROTAS_INTERPRETAVEIS = frozenset({ROTA_GUEST, ROTA_ONBOARDING})


@dataclass(frozen=True)
class ResultadoOrquestracaoCanal:
    codigo: str
    evento_id: int | None = None
    identidade_id: int | None = None
    rota: str | None = None
    etapa: str | None = None
    saida_id: int | None = None
    correlation_id: str | None = None
    status_envio: str | None = None
    status_entrega: str | None = None


def _id_valido(valor: object) -> bool:
    return not isinstance(valor, bool) and isinstance(valor, int) and valor > 0


def _log(resultado: ResultadoOrquestracaoCanal) -> None:
    logger.info(
        "canal_orquestracao evento_id=%s identidade_id=%s rota=%s etapa=%s "
        "saida_id=%s correlation_id=%s codigo=%s",
        resultado.evento_id if resultado.evento_id is not None else "-",
        resultado.identidade_id if resultado.identidade_id is not None else "-",
        resultado.rota or "-",
        resultado.etapa or "-",
        resultado.saida_id if resultado.saida_id is not None else "-",
        resultado.correlation_id or "-",
        resultado.codigo,
    )


def _resultado(
    codigo: str,
    *,
    evento_id: int | None = None,
    identidade_id: int | None = None,
    rota: str | None = None,
    etapa: str | None = None,
    saida_id: int | None = None,
    correlation_id: str | None = None,
    status_envio: str | None = None,
    status_entrega: str | None = None,
) -> ResultadoOrquestracaoCanal:
    return ResultadoOrquestracaoCanal(
        codigo=codigo,
        evento_id=evento_id,
        identidade_id=identidade_id,
        rota=rota,
        etapa=etapa,
        saida_id=saida_id,
        correlation_id=correlation_id,
        status_envio=status_envio,
        status_entrega=status_entrega,
    )


def _correlation(evento_id: int) -> str | None:
    evento = db.session.get(EventoCanalRecebido, evento_id)
    if evento is None:
        return None
    return evento.correlation_id


def _de_roteamento(roteamento: object, *, etapa: str = ETAPA_ENTRADA) -> ResultadoOrquestracaoCanal:
    evento_id = getattr(roteamento, "evento_id", None)
    identidade_id = getattr(roteamento, "identidade_id", None)
    return _resultado(
        str(getattr(roteamento, "codigo", CODIGO_ERRO_TECNICO)),
        evento_id=int(evento_id) if _id_valido(evento_id) else None,
        identidade_id=int(identidade_id) if _id_valido(identidade_id) else None,
        rota=getattr(roteamento, "rota", None),
        etapa=etapa,
        correlation_id=getattr(roteamento, "correlation_id", None),
    )


def _de_saida(
    envio: object,
    *,
    evento_id: int,
    identidade_id: int | None,
    rota: str | None,
    correlation_id: str | None,
    etapa: str,
) -> ResultadoOrquestracaoCanal:
    saida_id = getattr(envio, "saida_id", None)
    return _resultado(
        str(getattr(envio, "codigo", CODIGO_ERRO_TECNICO)),
        evento_id=evento_id,
        identidade_id=identidade_id,
        rota=rota,
        etapa=etapa,
        saida_id=int(saida_id) if _id_valido(saida_id) else None,
        correlation_id=getattr(envio, "correlation_id", None) or correlation_id,
        status_envio=getattr(envio, "status_envio", None),
    )


def _de_reconciliacao(reconciliacao: object) -> ResultadoOrquestracaoCanal:
    evento_id = getattr(reconciliacao, "evento_id", None)
    saida_id = getattr(reconciliacao, "saida_id", None)
    return _resultado(
        str(getattr(reconciliacao, "codigo", CODIGO_ERRO_TECNICO)),
        evento_id=int(evento_id) if _id_valido(evento_id) else None,
        etapa=ETAPA_RECONCILIACAO,
        saida_id=int(saida_id) if _id_valido(saida_id) else None,
        correlation_id=getattr(reconciliacao, "correlation_id", None),
        status_entrega=getattr(reconciliacao, "status_entrega", None),
    )


def _apos_interpretacao(
    evento_id: int,
    interpretacao: object,
    *,
    rota: str | None,
    correlation_id: str | None,
    marco: dict[str, str],
) -> ResultadoOrquestracaoCanal:
    identidade_id = getattr(interpretacao, "identidade_id", None)
    identidade = int(identidade_id) if _id_valido(identidade_id) else None
    correlation = getattr(interpretacao, "correlation_id", None) or correlation_id
    row = InterpretacaoConversacionalCanal.query.filter_by(evento_id=evento_id).one_or_none()
    if (
        row is None
        or row.codigo == CODIGO_EM_TRATAMENTO
        or row.acao == ACAO_RESERVADA
    ):
        marco["etapa"] = ETAPA_INTERPRETACAO
        return _resultado(
            str(getattr(interpretacao, "codigo", CODIGO_ERRO_TECNICO)),
            evento_id=evento_id,
            identidade_id=identidade,
            rota=rota,
            etapa=ETAPA_INTERPRETACAO,
            correlation_id=correlation,
        )
    marco["etapa"] = ETAPA_SAIDA
    envio = enviar_texto_da_interpretacao(int(row.id))
    if envio.status_envio == EventoCanalSaida.STATUS_AGUARDANDO_LINK and _id_valido(envio.saida_id):
        marco["etapa"] = ETAPA_LINK
        entrega = entregar_link_seguro(int(envio.saida_id))
        return _de_saida(
            entrega,
            evento_id=evento_id,
            identidade_id=identidade,
            rota=rota,
            correlation_id=correlation,
            etapa=ETAPA_LINK,
        )
    return _de_saida(
        envio,
        evento_id=evento_id,
        identidade_id=identidade,
        rota=rota,
        correlation_id=correlation,
        etapa=ETAPA_SAIDA,
    )


def _interpretacao_incompleta(row: InterpretacaoConversacionalCanal) -> bool:
    return row.codigo == CODIGO_EM_TRATAMENTO or row.acao == ACAO_RESERVADA


def _rota_interpretavel(roteamento: object) -> bool:
    return (
        getattr(roteamento, "codigo", None) == CODIGO_ROTEADO
        and getattr(roteamento, "rota", None) in _ROTAS_INTERPRETAVEIS
    )


def _rota_operacional(roteamento: object) -> bool:
    return (
        getattr(roteamento, "codigo", None) == CODIGO_ROTEADO
        and getattr(roteamento, "rota", None) == ROTA_USUARIO_VINCULADO
    )


def _tem_execucao(evento_id: int) -> bool:
    return (
        ExecucaoOperacionalCanal.query.filter_by(evento_id=evento_id).one_or_none()
        is not None
    )


def _de_operacional(
    operacao: object,
    *,
    rota: str | None,
    correlation_id: str | None,
) -> ResultadoOrquestracaoCanal:
    evento_id = getattr(operacao, "evento_id", None)
    identidade_id = getattr(operacao, "identidade_id", None)
    saida_id = getattr(operacao, "saida_id", None)
    etapa = ETAPA_SAIDA if _id_valido(saida_id) else ETAPA_OPERACIONAL
    return _resultado(
        str(getattr(operacao, "codigo", CODIGO_ERRO_TECNICO)),
        evento_id=int(evento_id) if _id_valido(evento_id) else None,
        identidade_id=int(identidade_id) if _id_valido(identidade_id) else None,
        rota=rota or ROTA_USUARIO_VINCULADO,
        etapa=etapa,
        saida_id=int(saida_id) if _id_valido(saida_id) else None,
        correlation_id=getattr(operacao, "correlation_id", None) or correlation_id,
        status_envio=getattr(operacao, "status_envio", None),
    )


def _seguir_operacional(
    evento_id: int,
    marco: dict[str, str],
    correlation_id: str | None,
    identidade_id: int | None,
    rota: str | None,
) -> ResultadoOrquestracaoCanal:
    marco["etapa"] = ETAPA_OPERACIONAL
    operacao = executar_operacao_canal(evento_id, identidade_id)
    resultado = _de_operacional(
        operacao,
        rota=rota,
        correlation_id=correlation_id,
    )
    marco["etapa"] = resultado.etapa or ETAPA_OPERACIONAL
    return resultado


def _continuar_textual(
    evento_id: int,
    marco: dict[str, str],
    correlation_id: str | None,
) -> ResultadoOrquestracaoCanal:
    """Segue do ponto já gravado. Não reivindica o 3B de novo."""
    roteamento = reconstruir_roteamento_canal(evento_id)
    if _tem_execucao(evento_id) or _rota_operacional(roteamento):
        identidade_id = getattr(roteamento, "identidade_id", None)
        return _seguir_operacional(
            evento_id,
            marco,
            getattr(roteamento, "correlation_id", None) or correlation_id,
            int(identidade_id) if _id_valido(identidade_id) else None,
            getattr(roteamento, "rota", None),
        )
    row = InterpretacaoConversacionalCanal.query.filter_by(evento_id=evento_id).one_or_none()
    if (row is None or _interpretacao_incompleta(row)) and not _rota_interpretavel(roteamento):
        return _de_roteamento(roteamento)
    marco["etapa"] = ETAPA_INTERPRETACAO
    interpretacao = interpretar_mensagem_canal(roteamento)
    if interpretacao.codigo in _SEM_RETORNO:
        return _de_roteamento(roteamento)
    return _apos_interpretacao(
        evento_id,
        interpretacao,
        rota=getattr(roteamento, "rota", None),
        correlation_id=getattr(roteamento, "correlation_id", None) or correlation_id,
        marco=marco,
    )


def _processar(evento_id: int, marco: dict[str, str]) -> ResultadoOrquestracaoCanal:
    evento = db.session.get(EventoCanalRecebido, evento_id)
    if evento is None:
        return _resultado(CODIGO_EVENTO_AUSENTE, etapa=ETAPA_ENTRADA)
    tipo = evento.tipo_evento
    correlation_id = evento.correlation_id
    if tipo == EventoCanalRecebido.TIPO_STATUS_ENTREGA:
        marco["etapa"] = ETAPA_RECONCILIACAO
        return _de_reconciliacao(reconciliar_status_saida(evento_id))
    if (
        tipo == EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL
        and evento.status_processamento == EventoCanalRecebido.STATUS_ROTEADO
    ):
        return _continuar_textual(evento_id, marco, correlation_id)
    marco["etapa"] = ETAPA_ENTRADA
    roteamento = processar_evento_canal_recebido(evento_id)
    if tipo != EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL:
        return _de_roteamento(roteamento)
    if roteamento.codigo == CODIGO_ROTEADO and roteamento.rota == ROTA_USUARIO_VINCULADO:
        return _seguir_operacional(
            evento_id,
            marco,
            roteamento.correlation_id or correlation_id,
            roteamento.identidade_id,
            roteamento.rota,
        )
    if roteamento.codigo == CODIGO_ROTEADO:
        marco["etapa"] = ETAPA_INTERPRETACAO
        interpretacao = interpretar_mensagem_canal(roteamento)
        return _apos_interpretacao(
            evento_id,
            interpretacao,
            rota=roteamento.rota,
            correlation_id=roteamento.correlation_id or correlation_id,
            marco=marco,
        )
    if roteamento.codigo == CODIGO_JA_PROCESSADO:
        return _continuar_textual(evento_id, marco, correlation_id)
    return _de_roteamento(roteamento)


def processar_evento_whatsapp(evento_recebido_id: int) -> ResultadoOrquestracaoCanal:
    """Coordena um evento já persistido. Não recebe payload da Meta."""
    if not _id_valido(evento_recebido_id):
        resultado = _resultado(CODIGO_EVENTO_INVALIDO, etapa=ETAPA_ENTRADA)
        _log(resultado)
        return resultado
    evento_id = int(evento_recebido_id)
    marco = {"etapa": ETAPA_ENTRADA}
    try:
        resultado = _processar(evento_id, marco)
    except Exception:
        db.session.rollback()
        resultado = _resultado(
            CODIGO_ERRO_TECNICO,
            evento_id=evento_id,
            etapa=marco["etapa"],
            correlation_id=_correlation(evento_id),
        )
    _log(resultado)
    return resultado


def _necessita_continuacao(evento_id: int) -> bool:
    """Há etapa segura seguinte nos registros já gravados.

    Não trata saída reservada, preparando_link nem resultado de envio
    já encerrado. Status com aplicação gravada também fica de fora.
    """
    evento = db.session.get(EventoCanalRecebido, evento_id)
    if evento is None:
        return False
    if evento.tipo_evento == EventoCanalRecebido.TIPO_STATUS_ENTREGA:
        return (
            AplicacaoStatusCanal.query.filter_by(evento_recebido_id=evento_id).first()
            is None
        )
    if evento.status_processamento == EventoCanalRecebido.STATUS_RECEBIDO:
        return True
    if evento.tipo_evento != EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL:
        return False
    if evento.status_processamento != EventoCanalRecebido.STATUS_ROTEADO:
        return False
    if _tem_execucao(evento_id):
        return operacao_canal_pendente(evento_id)
    if _rota_operacional(reconstruir_roteamento_canal(evento_id)):
        chave = EventoCanalSaida.chave_orientacao_franquia(evento_id)
        if EventoCanalSaida.query.filter_by(chave_idempotencia=chave).one_or_none() is not None:
            return False
        return True
    row = InterpretacaoConversacionalCanal.query.filter_by(evento_id=evento_id).one_or_none()
    if row is None or _interpretacao_incompleta(row):
        return _rota_interpretavel(reconstruir_roteamento_canal(evento_id))
    saida = (
        EventoCanalSaida.query.filter_by(evento_entrada_id=evento_id)
        .order_by(EventoCanalSaida.id.asc())
        .first()
    )
    if saida is None:
        return True
    return saida.status_envio == EventoCanalSaida.STATUS_AGUARDANDO_LINK


def continuar_replays_pendentes(identificadores: object) -> None:
    """Retoma só o replay cujo evento ainda tem etapa segura seguinte."""
    if not isinstance(identificadores, (list, tuple)):
        return
    for bruto in identificadores:
        if not _id_valido(bruto):
            continue
        evento_id = int(bruto)
        if not _necessita_continuacao(evento_id):
            continue
        processar_evento_whatsapp(evento_id)


def orquestrar_eventos_persistidos(correlation_id: str) -> None:
    """Dispara os eventos inseridos nesta recepção, na ordem do id interno.

    O correlation_id é gravado só na inserção. O replay do mesmo evento
    externo entra por ``continuar_replays_pendentes``.
    """
    if not isinstance(correlation_id, str) or not correlation_id:
        return
    identificadores = [
        int(identificador)
        for (identificador,) in (
            db.session.query(EventoCanalRecebido.id)
            .filter(EventoCanalRecebido.correlation_id == correlation_id)
            .order_by(EventoCanalRecebido.id.asc())
            .all()
        )
    ]
    for evento_id in identificadores:
        processar_evento_whatsapp(evento_id)
