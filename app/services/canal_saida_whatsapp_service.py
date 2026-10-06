"""Envia uma vez o texto já produzido por uma interpretação concluída.

Não interpreta de novo. Não chama modelo, não consulta cobrança e não
monta link de conclusão. O texto comum sai exatamente como foi persistido.

Quando a interpretação representa emissão ou entrega de link, o texto
guardado não contém o token. A decisão usa os campos estruturados do
Lote 3C: conclusao_id, ação, código e etapa. O texto não é analisado
nem reescrito. O Lote 2 não oferece um jeito de produzir o link na
entrega sem recuperar o segredo. Este lote não inventa esse mecanismo:
não lê o hash, não reconstrói o token e não envia a frase. A saída fica
em aguardando_link_seguro, com ou sem conclusao_id.

A orientação de franquia sai pelo mesmo caminho, com a chave
meta_whatsapp:<evento_entrada_id>:orientacao_franquia:resposta_principal.
Não há execução útil nem interpretação. O texto chega pronto e não é
recalculado aqui.

Idempotência e janela de duplicação
------------------------------------
A chave meta_whatsapp:<evento_entrada_id>:resposta_principal é única no
banco. A reserva é confirmada antes do HTTP, junto com a tentativa 1.
Se a tentativa não puder ser gravada, a saída também não fica. Se a
linha já existe — aceita, com erro, pendente de link ou ainda reservada
— a chamada devolve o resultado gravado e não fala com a Meta de novo.

Isso reduz o caso em que a Meta aceita e o processo cai antes de gravar
o message id: o retry da aplicação encontra `reservado` e não reenvia.
Não é exactly-once externo. A chamada HTTP já iniciada pode ter sido
aceita sem o id ficar gravado. Este lote não consulta status nem faz
retry automático. enviado_em marca a aceitação registrada localmente,
não a entrega no aparelho.
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    EventoCanalRecebido,
    EventoCanalSaida,
    ExecucaoOperacionalCanal,
    IdentidadeCanalExterna,
    InterpretacaoConversacionalCanal,
    utcnow_naive,
)
from app.services.canal_aquisicao_service import PROVEDOR_WHATSAPP_META
from app.services.canal_interpretacao_conversacional_service import (
    ACAO_RESERVADA,
    CODIGO_EM_TRATAMENTO,
)
from app.services.canal_tentativa_envio_service import (
    anexar_tentativa_inicial,
    interpretacao_de_link,
    sincronizar_tentativa_com_saida,
)
from app.services.whatsapp_meta_cloud_api_adapter import WhatsAppMetaCloudApiAdapter
from app.services.whatsapp_meta_config import configuracao_envio

logger = logging.getLogger(__name__)

CODIGO_INTERPRETACAO_INVALIDA = "interpretacao_invalida"
CODIGO_INTERPRETACAO_AUSENTE = "interpretacao_ausente"
CODIGO_INTERPRETACAO_INCOMPLETA = "interpretacao_incompleta"
CODIGO_TEXTO_AUSENTE = "texto_ausente"
CODIGO_EVENTO_AUSENTE = "evento_ausente"
CODIGO_EVENTO_NAO_TEXTUAL = "evento_nao_textual"
CODIGO_IDENTIDADE_INDISPONIVEL = "identidade_indisponivel"
CODIGO_CONTEXTO_AUSENTE = "contexto_ausente"
CODIGO_DESTINATARIO_AUSENTE = "destinatario_ausente"
CODIGO_CONFIGURACAO_AUSENTE = EventoCanalSaida.CODIGO_CONFIGURACAO_AUSENTE

_DIGITOS = re.compile(r"^[0-9]{1,32}$")
_CORRELATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_ESTADOS_VALIDOS = frozenset(
    {
        IdentidadeCanalExterna.ESTADO_GUEST,
        IdentidadeCanalExterna.ESTADO_CADASTRO_EM_ANDAMENTO,
        IdentidadeCanalExterna.ESTADO_VINCULADA,
    }
)

_travas: dict[int, threading.Lock] = {}
_travas_execucao: dict[int, threading.Lock] = {}
_travas_orientacao: dict[int, threading.Lock] = {}
_travas_guard = threading.Lock()


@dataclass(frozen=True)
class ResultadoSaidaCanal:
    codigo: str
    status_envio: str | None = None
    provider_message_id: str | None = None
    codigo_erro: str | None = None
    correlation_id: str | None = None
    saida_id: int | None = None
    evento_entrada_id: int | None = None
    conclusao_id: int | None = None


def _trava(interpretacao_id: int) -> threading.Lock:
    with _travas_guard:
        lock = _travas.get(interpretacao_id)
        if lock is None:
            lock = threading.Lock()
            _travas[interpretacao_id] = lock
        return lock


def _trava_execucao(execucao_id: int) -> threading.Lock:
    with _travas_guard:
        lock = _travas_execucao.get(execucao_id)
        if lock is None:
            lock = threading.Lock()
            _travas_execucao[execucao_id] = lock
        return lock


def _trava_orientacao(evento_id: int) -> threading.Lock:
    with _travas_guard:
        lock = _travas_orientacao.get(evento_id)
        if lock is None:
            lock = threading.Lock()
            _travas_orientacao[evento_id] = lock
        return lock


def _id_valido(valor: object) -> bool:
    return not isinstance(valor, bool) and isinstance(valor, int) and valor > 0


def _log(resultado: ResultadoSaidaCanal) -> None:
    logger.info(
        "canal_saida evento_id=%s saida_id=%s correlation_id=%s status=%s codigo=%s",
        resultado.evento_entrada_id if resultado.evento_entrada_id is not None else "-",
        resultado.saida_id if resultado.saida_id is not None else "-",
        resultado.correlation_id or "-",
        resultado.status_envio or "-",
        resultado.codigo_erro or resultado.codigo,
    )


def _resultado(
    codigo: str,
    *,
    status_envio: str | None = None,
    provider_message_id: str | None = None,
    codigo_erro: str | None = None,
    correlation_id: str | None = None,
    saida_id: int | None = None,
    evento_entrada_id: int | None = None,
    conclusao_id: int | None = None,
) -> ResultadoSaidaCanal:
    return ResultadoSaidaCanal(
        codigo=codigo,
        status_envio=status_envio,
        provider_message_id=provider_message_id,
        codigo_erro=codigo_erro,
        correlation_id=correlation_id,
        saida_id=saida_id,
        evento_entrada_id=evento_entrada_id,
        conclusao_id=conclusao_id,
    )


def _de_linha(row: EventoCanalSaida) -> ResultadoSaidaCanal:
    codigo = row.codigo_erro if row.status_envio == EventoCanalSaida.STATUS_ERRO else row.status_envio
    conclusao_id = int(row.conclusao_id) if row.conclusao_id is not None else None
    return _resultado(
        codigo or row.status_envio,
        status_envio=row.status_envio,
        provider_message_id=row.provider_message_id,
        codigo_erro=row.codigo_erro,
        correlation_id=row.correlation_id,
        saida_id=int(row.id),
        evento_entrada_id=(
            int(row.evento_entrada_id) if row.evento_entrada_id is not None else None
        ),
        conclusao_id=conclusao_id,
    )


def _correlation(evento: EventoCanalRecebido) -> str:
    valor = evento.correlation_id
    if isinstance(valor, str) and _CORRELATION_RE.fullmatch(valor):
        return valor
    return uuid4().hex


def _identidade_valida(sujeito: str) -> bool:
    row = (
        IdentidadeCanalExterna.query.filter_by(
            provedor=PROVEDOR_WHATSAPP_META,
            sujeito_externo=sujeito,
        )
        .filter(IdentidadeCanalExterna.estado != IdentidadeCanalExterna.ESTADO_REVOGADA)
        .one_or_none()
    )
    return row is not None and row.estado in _ESTADOS_VALIDOS


def _buscar_saida(chave: str) -> EventoCanalSaida | None:
    return EventoCanalSaida.query.filter_by(chave_idempotencia=chave).one_or_none()


def _inserir(row: EventoCanalSaida) -> EventoCanalSaida | None:
    try:
        with db.session.begin_nested():
            db.session.add(row)
            db.session.flush()
            anexar_tentativa_inicial(row)
            db.session.flush()
            saida_id = int(row.id)
    except IntegrityError:
        return None
    db.session.commit()
    return db.session.get(EventoCanalSaida, saida_id)


def _nova_linha(
    *,
    evento: EventoCanalRecebido,
    chave: str,
    status_envio: str,
    interpretacao: InterpretacaoConversacionalCanal | None = None,
    execucao: ExecucaoOperacionalCanal | None = None,
    codigo_erro: str | None = None,
) -> EventoCanalSaida:
    return EventoCanalSaida(
        evento_entrada_id=int(evento.id),
        interpretacao_id=int(interpretacao.id) if interpretacao is not None else None,
        execucao_operacional_id=int(execucao.id) if execucao is not None else None,
        provider=EventoCanalSaida.PROVIDER_META_WHATSAPP,
        chave_idempotencia=chave,
        status_envio=status_envio,
        provider_message_id=None,
        codigo_erro=codigo_erro,
        criado_em=utcnow_naive(),
        enviado_em=None,
        correlation_id=_correlation(evento),
    )


def _marcar(
    saida_id: int,
    *,
    status_envio: str,
    provider_message_id: str | None,
    codigo_erro: str | None,
) -> EventoCanalSaida | None:
    row = db.session.get(EventoCanalSaida, saida_id)
    if row is None:
        return None
    if row.status_envio != EventoCanalSaida.STATUS_RESERVADO:
        return row
    row.status_envio = status_envio
    row.provider_message_id = provider_message_id
    row.codigo_erro = codigo_erro
    row.enviado_em = utcnow_naive() if status_envio == EventoCanalSaida.STATUS_ACEITO else None
    sincronizar_tentativa_com_saida(row)
    db.session.commit()
    return db.session.get(EventoCanalSaida, saida_id)


def inserir_saida_canal(row: EventoCanalSaida) -> EventoCanalSaida | None:
    """Reserva a saída e a tentativa 1. None significa chave já gravada."""
    return _inserir(row)


def concluir_envio_reservado(
    saida_id: int,
    *,
    phone_number_id: str,
    destinatario: str,
    texto: str,
) -> EventoCanalSaida | None:
    """Chama o adapter oficial e grava o resultado da reserva já confirmada.

    Falha de gravação depois do HTTP não dispara outro POST.
    """
    http = WhatsAppMetaCloudApiAdapter().enviar_texto(
        phone_number_id=phone_number_id,
        destinatario=destinatario,
        texto=texto,
    )
    if http.aceito:
        status = EventoCanalSaida.STATUS_ACEITO
        message_id = http.provider_message_id
        codigo_erro = None
    else:
        status = EventoCanalSaida.STATUS_ERRO
        message_id = None
        codigo_erro = http.codigo_erro or EventoCanalSaida.CODIGO_RESPOSTA_INVALIDA
    try:
        return _marcar(
            saida_id,
            status_envio=status,
            provider_message_id=message_id,
            codigo_erro=codigo_erro,
        )
    except Exception:
        db.session.rollback()
        return db.session.get(EventoCanalSaida, saida_id)


def _pendente_de_link(interpretacao: InterpretacaoConversacionalCanal) -> bool:
    """Emissão ou entrega de link ainda sem o segredo na mensagem.

    conclusao_id, ação, código e a etapa de senha bastam. A frase
    persistida permanece como está.
    """
    return interpretacao_de_link(interpretacao)


def _recusar_precondicao(
    evento: EventoCanalRecebido | None,
    interpretacao: InterpretacaoConversacionalCanal,
) -> ResultadoSaidaCanal | None:
    if not isinstance(interpretacao.texto_resposta, str) or not interpretacao.texto_resposta:
        return _resultado(CODIGO_TEXTO_AUSENTE, evento_entrada_id=int(interpretacao.evento_id))
    if evento is None:
        return _resultado(CODIGO_EVENTO_AUSENTE, evento_entrada_id=int(interpretacao.evento_id))
    if evento.tipo_evento != EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL:
        return _resultado(CODIGO_EVENTO_NAO_TEXTUAL, evento_entrada_id=int(evento.id))
    if evento.provider != EventoCanalRecebido.PROVIDER_META_WHATSAPP:
        return _resultado(CODIGO_EVENTO_NAO_TEXTUAL, evento_entrada_id=int(evento.id))
    contexto = evento.contexto_destino
    if not isinstance(contexto, str) or not _DIGITOS.fullmatch(contexto):
        return _resultado(CODIGO_CONTEXTO_AUSENTE, evento_entrada_id=int(evento.id))
    sujeito = evento.sujeito_externo
    if not isinstance(sujeito, str) or not _DIGITOS.fullmatch(sujeito):
        return _resultado(CODIGO_DESTINATARIO_AUSENTE, evento_entrada_id=int(evento.id))
    if not _identidade_valida(sujeito):
        return _resultado(
            CODIGO_IDENTIDADE_INDISPONIVEL,
            evento_entrada_id=int(evento.id),
            correlation_id=evento.correlation_id,
        )
    return None


def _enviar(interpretacao_id: int) -> ResultadoSaidaCanal:
    interpretacao = db.session.get(InterpretacaoConversacionalCanal, interpretacao_id)
    if interpretacao is None:
        return _resultado(CODIGO_INTERPRETACAO_AUSENTE)
    if (
        interpretacao.codigo == CODIGO_EM_TRATAMENTO
        or interpretacao.acao == ACAO_RESERVADA
    ):
        return _resultado(
            CODIGO_INTERPRETACAO_INCOMPLETA,
            evento_entrada_id=int(interpretacao.evento_id),
        )
    evento = db.session.get(EventoCanalRecebido, int(interpretacao.evento_id))
    if evento is None:
        return _resultado(CODIGO_EVENTO_AUSENTE, evento_entrada_id=int(interpretacao.evento_id))
    chave = EventoCanalSaida.chave_resposta_principal(int(evento.id))
    existente = _buscar_saida(chave)
    if existente is not None:
        return _de_linha(existente)
    recusa = _recusar_precondicao(evento, interpretacao)
    if recusa is not None:
        return recusa
    if _pendente_de_link(interpretacao):
        gravada = _inserir(
            _nova_linha(
                evento=evento,
                interpretacao=interpretacao,
                chave=chave,
                status_envio=EventoCanalSaida.STATUS_AGUARDANDO_LINK,
            )
        )
        if gravada is None:
            repetida = _buscar_saida(chave)
            if repetida is None:
                return _resultado(CODIGO_INTERPRETACAO_AUSENTE, evento_entrada_id=int(evento.id))
            return _de_linha(repetida)
        return _de_linha(gravada)
    if configuracao_envio() is None:
        return _resultado(
            CODIGO_CONFIGURACAO_AUSENTE,
            codigo_erro=CODIGO_CONFIGURACAO_AUSENTE,
            evento_entrada_id=int(evento.id),
            correlation_id=evento.correlation_id,
        )
    phone_number_id = str(evento.contexto_destino)
    destinatario = str(evento.sujeito_externo)
    texto = str(interpretacao.texto_resposta)
    evento_id = int(evento.id)
    reservada = _inserir(
        _nova_linha(
            evento=evento,
            interpretacao=interpretacao,
            chave=chave,
            status_envio=EventoCanalSaida.STATUS_RESERVADO,
        )
    )
    if reservada is None:
        repetida = _buscar_saida(chave)
        if repetida is None:
            return _resultado(CODIGO_INTERPRETACAO_AUSENTE, evento_entrada_id=int(evento.id))
        return _de_linha(repetida)
    saida_id = int(reservada.id)
    # A reserva já está confirmada. Daqui em diante, falha de gravação
    # não dispara outro POST. O texto é o que já estava persistido.
    marcada = concluir_envio_reservado(
        saida_id,
        phone_number_id=phone_number_id,
        destinatario=destinatario,
        texto=texto,
    )
    if marcada is None:
        return _resultado(
            EventoCanalSaida.STATUS_RESERVADO,
            status_envio=EventoCanalSaida.STATUS_RESERVADO,
            evento_entrada_id=evento_id,
        )
    return _de_linha(marcada)


def enviar_texto_da_interpretacao(interpretacao_id: int) -> ResultadoSaidaCanal:
    """Envia, ou devolve, a resposta principal de uma interpretação concluída."""
    if not _id_valido(interpretacao_id):
        resultado = _resultado(CODIGO_INTERPRETACAO_INVALIDA)
        _log(resultado)
        return resultado
    with _trava(int(interpretacao_id)):
        resultado = _enviar(int(interpretacao_id))
        _log(resultado)
        return resultado


CODIGO_EXECUCAO_AUSENTE = "execucao_ausente"
CODIGO_EXECUCAO_INCOMPLETA = "execucao_incompleta"
CODIGO_EXECUCAO_INVALIDA = "execucao_invalida"


def _recusar_texto(
    evento: EventoCanalRecebido | None,
    texto: str | None,
    evento_id: int,
) -> ResultadoSaidaCanal | None:
    if not isinstance(texto, str) or not texto:
        return _resultado(CODIGO_TEXTO_AUSENTE, evento_entrada_id=evento_id)
    if evento is None:
        return _resultado(CODIGO_EVENTO_AUSENTE, evento_entrada_id=evento_id)
    if evento.tipo_evento != EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL:
        return _resultado(CODIGO_EVENTO_NAO_TEXTUAL, evento_entrada_id=int(evento.id))
    if evento.provider != EventoCanalRecebido.PROVIDER_META_WHATSAPP:
        return _resultado(CODIGO_EVENTO_NAO_TEXTUAL, evento_entrada_id=int(evento.id))
    contexto = evento.contexto_destino
    if not isinstance(contexto, str) or not _DIGITOS.fullmatch(contexto):
        return _resultado(CODIGO_CONTEXTO_AUSENTE, evento_entrada_id=int(evento.id))
    sujeito = evento.sujeito_externo
    if not isinstance(sujeito, str) or not _DIGITOS.fullmatch(sujeito):
        return _resultado(CODIGO_DESTINATARIO_AUSENTE, evento_entrada_id=int(evento.id))
    if not _identidade_valida(sujeito):
        return _resultado(
            CODIGO_IDENTIDADE_INDISPONIVEL,
            evento_entrada_id=int(evento.id),
            correlation_id=evento.correlation_id,
        )
    return None


def _enviar_execucao(execucao_id: int) -> ResultadoSaidaCanal:
    execucao = db.session.get(ExecucaoOperacionalCanal, execucao_id)
    if execucao is None:
        return _resultado(CODIGO_EXECUCAO_AUSENTE)
    evento_id = int(execucao.evento_id)
    if (
        execucao.estado != ExecucaoOperacionalCanal.ESTADO_CONCLUIDA
        or execucao.conclusao_util != 1
        or not isinstance(execucao.texto_resposta, str)
        or not execucao.texto_resposta
    ):
        return _resultado(CODIGO_EXECUCAO_INCOMPLETA, evento_entrada_id=evento_id)
    evento = db.session.get(EventoCanalRecebido, evento_id)
    chave = EventoCanalSaida.chave_resposta_principal(evento_id)
    existente = _buscar_saida(chave)
    if existente is not None:
        return _de_linha(existente)
    recusa = _recusar_texto(evento, execucao.texto_resposta, evento_id)
    if recusa is not None:
        return recusa
    if configuracao_envio() is None:
        correlation = evento.correlation_id if evento is not None else None
        return _resultado(
            CODIGO_CONFIGURACAO_AUSENTE,
            codigo_erro=CODIGO_CONFIGURACAO_AUSENTE,
            evento_entrada_id=evento_id,
            correlation_id=correlation,
        )
    phone_number_id = str(evento.contexto_destino)
    destinatario = str(evento.sujeito_externo)
    texto = str(execucao.texto_resposta)
    reservada = _inserir(
        _nova_linha(
            evento=evento,
            execucao=execucao,
            chave=chave,
            status_envio=EventoCanalSaida.STATUS_RESERVADO,
        )
    )
    if reservada is None:
        repetida = _buscar_saida(chave)
        if repetida is None:
            return _resultado(CODIGO_EXECUCAO_AUSENTE, evento_entrada_id=evento_id)
        return _de_linha(repetida)
    saida_id = int(reservada.id)
    marcada = concluir_envio_reservado(
        saida_id,
        phone_number_id=phone_number_id,
        destinatario=destinatario,
        texto=texto,
    )
    if marcada is None:
        return _resultado(
            EventoCanalSaida.STATUS_RESERVADO,
            status_envio=EventoCanalSaida.STATUS_RESERVADO,
            evento_entrada_id=evento_id,
        )
    return _de_linha(marcada)


def enviar_texto_da_execucao_operacional(execucao_id: int) -> ResultadoSaidaCanal:
    """Envia, ou devolve, a resposta principal de uma execução operacional concluída."""
    if not _id_valido(execucao_id):
        resultado = _resultado(CODIGO_EXECUCAO_INVALIDA)
        _log(resultado)
        return resultado
    with _trava_execucao(int(execucao_id)):
        resultado = _enviar_execucao(int(execucao_id))
        _log(resultado)
        return resultado


def _enviar_orientacao(evento_id: int, texto: str) -> ResultadoSaidaCanal:
    evento = db.session.get(EventoCanalRecebido, evento_id)
    chave = EventoCanalSaida.chave_orientacao_franquia(evento_id)
    existente = _buscar_saida(chave)
    if existente is not None:
        return _de_linha(existente)
    recusa = _recusar_texto(evento, texto, evento_id)
    if recusa is not None:
        return recusa
    if configuracao_envio() is None:
        correlation = evento.correlation_id if evento is not None else None
        return _resultado(
            CODIGO_CONFIGURACAO_AUSENTE,
            codigo_erro=CODIGO_CONFIGURACAO_AUSENTE,
            evento_entrada_id=evento_id,
            correlation_id=correlation,
        )
    phone_number_id = str(evento.contexto_destino)
    destinatario = str(evento.sujeito_externo)
    reservada = _inserir(
        _nova_linha(
            evento=evento,
            chave=chave,
            status_envio=EventoCanalSaida.STATUS_RESERVADO,
        )
    )
    if reservada is None:
        repetida = _buscar_saida(chave)
        if repetida is None:
            return _resultado(CODIGO_EVENTO_AUSENTE, evento_entrada_id=evento_id)
        return _de_linha(repetida)
    saida_id = int(reservada.id)
    marcada = concluir_envio_reservado(
        saida_id,
        phone_number_id=phone_number_id,
        destinatario=destinatario,
        texto=texto,
    )
    if marcada is None:
        return _resultado(
            EventoCanalSaida.STATUS_RESERVADO,
            status_envio=EventoCanalSaida.STATUS_RESERVADO,
            evento_entrada_id=evento_id,
        )
    return _de_linha(marcada)


def enviar_orientacao_franquia(evento_id: int, texto: str) -> ResultadoSaidaCanal:
    """Envia, ou devolve, a orientação comercial já decidida para o evento."""
    if not _id_valido(evento_id):
        resultado = _resultado(CODIGO_EVENTO_AUSENTE)
        _log(resultado)
        return resultado
    with _trava_orientacao(int(evento_id)):
        resultado = _enviar_orientacao(int(evento_id), texto)
        _log(resultado)
        return resultado
