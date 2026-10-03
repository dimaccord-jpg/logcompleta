"""Recuperação explícita de uma saída de texto comum.

Cria a tentativa seguinte, no máximo a 3, e só então fala com a Cloud API.
A tentativa anterior permanece. Não há segunda chamada de modelo, não há
emissão de segredo e não há cobrança.

Cada ação explícita traz uma chave opaca. A mesma chave devolve a
tentativa já gravada, reservada, aceita ou em erro, e não faz outro POST.
Outra tentativa só nasce de outra chave. A unicidade (saida, chave) é do
banco. Duas chaves diferentes não abrem duas tentativas ao mesmo tempo:
quem perde devolve conflito. envio_inicial reservada continua elegível.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError, OperationalError

from app.extensions import db
from app.models import (
    EstadoEntregaCanalSaida,
    EstadoEntregaTentativaCanal,
    EventoCanalRecebido,
    EventoCanalSaida,
    IdentidadeCanalExterna,
    InterpretacaoConversacionalCanal,
    TentativaEnvioCanal,
    utcnow_naive,
)
from app.services.canal_aquisicao_service import PROVEDOR_WHATSAPP_META
from app.services.canal_tentativa_envio_service import interpretacao_de_link
from app.services.whatsapp_meta_cloud_api_adapter import WhatsAppMetaCloudApiAdapter
from app.services.whatsapp_meta_config import configuracao_envio

logger = logging.getLogger(__name__)

CODIGO_TIPO_NAO_SUPORTADO = "tipo_nao_suportado"
CODIGO_EVIDENCIA_POSITIVA = "evidencia_positiva"
CODIGO_LIMITE_TENTATIVAS = "limite_tentativas"
CODIGO_CONFLITO = "conflito"
CODIGO_ESTADO_NAO_ELEGIVEL = "estado_nao_elegivel"
CODIGO_MOTIVO_INVALIDO = "motivo_invalido"
CODIGO_CHAVE_INVALIDA = "chave_invalida"
CODIGO_SAIDA_AUSENTE = "saida_ausente"
CODIGO_SAIDA_INVALIDA = "saida_invalida"
CODIGO_CONFIGURACAO_AUSENTE = EventoCanalSaida.CODIGO_CONFIGURACAO_AUSENTE
CODIGO_INTERPRETACAO_AUSENTE = "interpretacao_ausente"
CODIGO_INTERPRETACAO_INVALIDA = "interpretacao_invalida"
CODIGO_TEXTO_AUSENTE = "texto_ausente"
CODIGO_EVENTO_AUSENTE = "evento_ausente"
CODIGO_EVENTO_INVALIDO = "evento_invalido"
CODIGO_CONTEXTO_INVALIDO = "contexto_invalido"
CODIGO_IDENTIDADE_INDISPONIVEL = "identidade_indisponivel"

_DIGITOS = re.compile(r"^[0-9]{1,32}$")
_CHAVE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_ERROS_ELEGIVEIS = frozenset(EventoCanalSaida.CODIGOS_ERRO)
_ESTADOS_IDENTIDADE = frozenset(
    {
        IdentidadeCanalExterna.ESTADO_GUEST,
        IdentidadeCanalExterna.ESTADO_CADASTRO_EM_ANDAMENTO,
        IdentidadeCanalExterna.ESTADO_VINCULADA,
    }
)

_travas: dict[int, threading.Lock] = {}
_travas_guard = threading.Lock()


@dataclass(frozen=True)
class ResultadoRecuperacaoTexto:
    codigo: str
    saida_id: int | None = None
    tentativa_id: int | None = None
    numero_tentativa: int | None = None
    status_tentativa: str | None = None
    provider_message_id: str | None = None
    codigo_erro: str | None = None
    correlation_id: str | None = None
    motivo: str | None = None


def _trava(saida_id: int) -> threading.Lock:
    with _travas_guard:
        lock = _travas.get(saida_id)
        if lock is None:
            lock = threading.Lock()
            _travas[saida_id] = lock
        return lock


def _id_valido(valor: object) -> bool:
    return not isinstance(valor, bool) and isinstance(valor, int) and valor > 0


def _motivo_valido(motivo: object) -> bool:
    return isinstance(motivo, str) and motivo in TentativaEnvioCanal.MOTIVOS


def _chave_normalizada(chave: object) -> str | None:
    if not isinstance(chave, str):
        return None
    candidato = chave.lower()
    if _CHAVE.fullmatch(candidato) is None:
        return None
    return candidato


def _log(resultado: ResultadoRecuperacaoTexto) -> ResultadoRecuperacaoTexto:
    logger.info(
        "recuperacao_texto saida_id=%s tentativa_id=%s correlation_id=%s "
        "numero=%s origem=%s motivo=%s status=%s codigo=%s",
        resultado.saida_id if resultado.saida_id is not None else "-",
        resultado.tentativa_id if resultado.tentativa_id is not None else "-",
        resultado.correlation_id or "-",
        resultado.numero_tentativa if resultado.numero_tentativa is not None else "-",
        TentativaEnvioCanal.ORIGEM_RECUPERACAO_MANUAL,
        resultado.motivo or "-",
        resultado.status_tentativa or "-",
        resultado.codigo_erro or resultado.codigo,
    )
    return resultado


def _resultado(
    codigo: str,
    *,
    saida_id: int | None = None,
    tentativa_id: int | None = None,
    numero_tentativa: int | None = None,
    status_tentativa: str | None = None,
    provider_message_id: str | None = None,
    codigo_erro: str | None = None,
    correlation_id: str | None = None,
    motivo: str | None = None,
) -> ResultadoRecuperacaoTexto:
    return ResultadoRecuperacaoTexto(
        codigo=codigo,
        saida_id=saida_id,
        tentativa_id=tentativa_id,
        numero_tentativa=numero_tentativa,
        status_tentativa=status_tentativa,
        provider_message_id=provider_message_id,
        codigo_erro=codigo_erro,
        correlation_id=correlation_id,
        motivo=motivo,
    )


def _por_chave(saida_id: int, chave: str) -> TentativaEnvioCanal | None:
    return TentativaEnvioCanal.query.filter_by(
        saida_id=int(saida_id),
        chave_recuperacao=chave,
    ).one_or_none()


def _codigo_existente(tentativa: TentativaEnvioCanal) -> str:
    if tentativa.status_tentativa == TentativaEnvioCanal.STATUS_ACEITA:
        return EventoCanalSaida.STATUS_ACEITO
    if tentativa.status_tentativa == TentativaEnvioCanal.STATUS_ERRO:
        return tentativa.codigo_erro or EventoCanalSaida.STATUS_ERRO
    return tentativa.status_tentativa


def _ja_gravada(saida_id: int, chave: str) -> ResultadoRecuperacaoTexto | None:
    existente = _por_chave(saida_id, chave)
    if existente is None:
        return None
    return _de_tentativa(existente, _codigo_existente(existente))


def _ultima(saida_id: int) -> TentativaEnvioCanal | None:
    return (
        TentativaEnvioCanal.query.filter_by(saida_id=int(saida_id))
        .order_by(
            TentativaEnvioCanal.numero_tentativa.desc(),
            TentativaEnvioCanal.id.desc(),
        )
        .first()
    )


def _identidade_valida(sujeito: str) -> bool:
    row = (
        IdentidadeCanalExterna.query.filter_by(
            provedor=PROVEDOR_WHATSAPP_META,
            sujeito_externo=sujeito,
        )
        .filter(IdentidadeCanalExterna.estado != IdentidadeCanalExterna.ESTADO_REVOGADA)
        .one_or_none()
    )
    return row is not None and row.estado in _ESTADOS_IDENTIDADE


def _eh_link(
    saida: EventoCanalSaida,
    interpretacao: InterpretacaoConversacionalCanal | None,
    ultima: TentativaEnvioCanal | None,
) -> bool:
    if ultima is not None and ultima.tipo_tentativa == TentativaEnvioCanal.TIPO_LINK_SEGURO:
        return True
    if saida.status_envio in (
        EventoCanalSaida.STATUS_AGUARDANDO_LINK,
        EventoCanalSaida.STATUS_PREPARANDO_LINK,
    ):
        return True
    if saida.conclusao_id is not None:
        return True
    if interpretacao is not None and interpretacao_de_link(interpretacao):
        return True
    return False


def _contexto(
    saida: EventoCanalSaida,
    interpretacao: InterpretacaoConversacionalCanal | None,
    evento: EventoCanalRecebido | None,
) -> str | None:
    if interpretacao is None:
        return CODIGO_INTERPRETACAO_AUSENTE
    if int(interpretacao.id) != int(saida.interpretacao_id):
        return CODIGO_INTERPRETACAO_INVALIDA
    if int(interpretacao.evento_id) != int(saida.evento_entrada_id):
        return CODIGO_INTERPRETACAO_INVALIDA
    if not isinstance(interpretacao.texto_resposta, str) or not interpretacao.texto_resposta:
        return CODIGO_TEXTO_AUSENTE
    if evento is None or int(evento.id) != int(saida.evento_entrada_id):
        return CODIGO_EVENTO_AUSENTE
    if (
        evento.tipo_evento != EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL
        or evento.provider != EventoCanalRecebido.PROVIDER_META_WHATSAPP
    ):
        return CODIGO_EVENTO_INVALIDO
    contexto = evento.contexto_destino
    sujeito = evento.sujeito_externo
    if not isinstance(contexto, str) or not _DIGITOS.fullmatch(contexto):
        return CODIGO_CONTEXTO_INVALIDO
    if not isinstance(sujeito, str) or not _DIGITOS.fullmatch(sujeito):
        return CODIGO_CONTEXTO_INVALIDO
    if not _identidade_valida(sujeito):
        return CODIGO_IDENTIDADE_INDISPONIVEL
    return None


def _evidencia_positiva(saida_id: int) -> bool:
    for linha in EstadoEntregaTentativaCanal.query.filter_by(saida_id=int(saida_id)).all():
        if linha.sent_em is not None or linha.delivered_em is not None or linha.read_em is not None:
            return True
    estado = EstadoEntregaCanalSaida.query.filter_by(saida_id=int(saida_id)).one_or_none()
    if estado is None:
        return False
    return (
        estado.sent_em is not None
        or estado.delivered_em is not None
        or estado.read_em is not None
    )


def _entrega_da(ultima: TentativaEnvioCanal):
    especifica = EstadoEntregaTentativaCanal.query.filter_by(
        tentativa_envio_id=int(ultima.id)
    ).one_or_none()
    if especifica is not None:
        return especifica
    maior = (
        db.session.query(db.func.max(TentativaEnvioCanal.numero_tentativa))
        .filter(TentativaEnvioCanal.saida_id == int(ultima.saida_id))
        .scalar()
    )
    if maior is None or int(maior) != int(ultima.numero_tentativa):
        return None
    return EstadoEntregaCanalSaida.query.filter_by(saida_id=int(ultima.saida_id)).one_or_none()


def _em_curso(ultima: TentativaEnvioCanal) -> bool:
    return (
        ultima.origem_tentativa == TentativaEnvioCanal.ORIGEM_RECUPERACAO_MANUAL
        and ultima.status_tentativa == TentativaEnvioCanal.STATUS_RESERVADA
        and ultima.finalizado_em is None
    )


def _elegivel(ultima: TentativaEnvioCanal) -> bool:
    if ultima.status_tentativa == TentativaEnvioCanal.STATUS_ERRO and ultima.codigo_erro in _ERROS_ELEGIVEIS:
        return True
    if ultima.status_tentativa == TentativaEnvioCanal.STATUS_RESERVADA:
        return True
    if (
        ultima.classificacao_resultado == TentativaEnvioCanal.CLASSIFICACAO_RESULTADO_INCERTO
        and ultima.status_tentativa
        in (
            TentativaEnvioCanal.STATUS_RESERVADA,
            TentativaEnvioCanal.STATUS_PREPARANDO,
        )
    ):
        return True
    if ultima.status_tentativa != TentativaEnvioCanal.STATUS_ACEITA:
        return False
    if ultima.provider_message_id is None:
        return False
    entrega = _entrega_da(ultima)
    if entrega is None or entrega.status_entrega != EstadoEntregaCanalSaida.STATUS_FAILED:
        return False
    if entrega.sent_em is not None or entrega.delivered_em is not None or entrega.read_em is not None:
        return False
    return True


def _de_tentativa(tentativa: TentativaEnvioCanal, codigo: str) -> ResultadoRecuperacaoTexto:
    return _resultado(
        codigo,
        saida_id=int(tentativa.saida_id),
        tentativa_id=int(tentativa.id),
        numero_tentativa=int(tentativa.numero_tentativa),
        status_tentativa=tentativa.status_tentativa,
        provider_message_id=tentativa.provider_message_id,
        codigo_erro=tentativa.codigo_erro,
        correlation_id=tentativa.correlation_id,
        motivo=tentativa.motivo_recuperacao,
    )


def _diagnostico(
    saida: EventoCanalSaida,
    *,
    motivo: str,
    foto: int | None = None,
) -> ResultadoRecuperacaoTexto | None:
    if saida.provider != EventoCanalSaida.PROVIDER_META_WHATSAPP:
        return _resultado(CODIGO_ESTADO_NAO_ELEGIVEL, saida_id=int(saida.id))
    if saida.interpretacao_id is None:
        return _resultado(
            CODIGO_INTERPRETACAO_AUSENTE,
            saida_id=int(saida.id),
            correlation_id=saida.correlation_id,
            motivo=motivo,
        )
    interpretacao = db.session.get(InterpretacaoConversacionalCanal, int(saida.interpretacao_id))
    evento = db.session.get(EventoCanalRecebido, int(saida.evento_entrada_id))
    ultima = _ultima(int(saida.id))
    correlation = saida.correlation_id
    if _eh_link(saida, interpretacao, ultima):
        return _resultado(
            CODIGO_TIPO_NAO_SUPORTADO,
            saida_id=int(saida.id),
            correlation_id=correlation,
            motivo=motivo,
        )
    contexto = _contexto(saida, interpretacao, evento)
    if contexto is not None:
        return _resultado(
            contexto,
            saida_id=int(saida.id),
            correlation_id=correlation,
            motivo=motivo,
        )
    if ultima is None:
        return _resultado(
            CODIGO_ESTADO_NAO_ELEGIVEL,
            saida_id=int(saida.id),
            correlation_id=correlation,
            motivo=motivo,
        )
    if foto is not None and int(ultima.numero_tentativa) != int(foto):
        return _resultado(
            CODIGO_CONFLITO,
            saida_id=int(saida.id),
            tentativa_id=int(ultima.id),
            numero_tentativa=int(ultima.numero_tentativa),
            status_tentativa=ultima.status_tentativa,
            correlation_id=correlation,
            motivo=motivo,
        )
    if _em_curso(ultima):
        return _resultado(
            CODIGO_CONFLITO,
            saida_id=int(saida.id),
            tentativa_id=int(ultima.id),
            numero_tentativa=int(ultima.numero_tentativa),
            status_tentativa=ultima.status_tentativa,
            correlation_id=correlation,
            motivo=motivo,
        )
    if _evidencia_positiva(int(saida.id)):
        return _resultado(
            CODIGO_EVIDENCIA_POSITIVA,
            saida_id=int(saida.id),
            tentativa_id=int(ultima.id),
            numero_tentativa=int(ultima.numero_tentativa),
            status_tentativa=ultima.status_tentativa,
            correlation_id=correlation,
            motivo=motivo,
        )
    if int(ultima.numero_tentativa) >= TentativaEnvioCanal.NUMERO_MAXIMO:
        return _resultado(
            CODIGO_LIMITE_TENTATIVAS,
            saida_id=int(saida.id),
            tentativa_id=int(ultima.id),
            numero_tentativa=int(ultima.numero_tentativa),
            status_tentativa=ultima.status_tentativa,
            correlation_id=correlation,
            motivo=motivo,
        )
    if not _elegivel(ultima):
        return _resultado(
            CODIGO_ESTADO_NAO_ELEGIVEL,
            saida_id=int(saida.id),
            tentativa_id=int(ultima.id),
            numero_tentativa=int(ultima.numero_tentativa),
            status_tentativa=ultima.status_tentativa,
            correlation_id=correlation,
            motivo=motivo,
        )
    if configuracao_envio() is None:
        return _resultado(
            CODIGO_CONFIGURACAO_AUSENTE,
            saida_id=int(saida.id),
            tentativa_id=int(ultima.id),
            numero_tentativa=int(ultima.numero_tentativa),
            status_tentativa=ultima.status_tentativa,
            codigo_erro=CODIGO_CONFIGURACAO_AUSENTE,
            correlation_id=correlation,
            motivo=motivo,
        )
    return None


def _depois_da_colisao(saida_id: int, motivo: str, chave: str) -> ResultadoRecuperacaoTexto:
    db.session.rollback()
    for _tentativa in range(8):
        gravada = _ja_gravada(saida_id, chave)
        if gravada is not None:
            return gravada
        time.sleep(0.05)
    return _resultado(CODIGO_CONFLITO, saida_id=saida_id, motivo=motivo)


def _reservar(
    saida_id: int,
    motivo: str,
    chave: str,
    foto: int,
) -> ResultadoRecuperacaoTexto | tuple:
    gravada = _ja_gravada(saida_id, chave)
    if gravada is not None:
        return gravada
    try:
        with db.session.begin_nested():
            saida = db.session.get(EventoCanalSaida, saida_id)
            if saida is None:
                return _resultado(CODIGO_SAIDA_AUSENTE)
            repetida = _ja_gravada(saida_id, chave)
            if repetida is not None:
                return repetida
            bloqueio = _diagnostico(saida, motivo=motivo, foto=foto)
            if bloqueio is not None:
                return bloqueio
            ultima = _ultima(saida_id)
            interpretacao = db.session.get(
                InterpretacaoConversacionalCanal,
                int(saida.interpretacao_id),
            )
            evento = db.session.get(EventoCanalRecebido, int(saida.evento_entrada_id))
            if ultima is None or interpretacao is None or evento is None:
                return _resultado(
                    CODIGO_ESTADO_NAO_ELEGIVEL,
                    saida_id=saida_id,
                    correlation_id=saida.correlation_id,
                    motivo=motivo,
                )
            numero = int(ultima.numero_tentativa) + 1
            tentativa = TentativaEnvioCanal(
                saida_id=saida_id,
                numero_tentativa=numero,
                tipo_tentativa=TentativaEnvioCanal.TIPO_TEXTO_COMUM,
                origem_tentativa=TentativaEnvioCanal.ORIGEM_RECUPERACAO_MANUAL,
                status_tentativa=TentativaEnvioCanal.STATUS_RESERVADA,
                codigo_erro=None,
                provider=EventoCanalSaida.PROVIDER_META_WHATSAPP,
                provider_message_id=None,
                conclusao_id=None,
                criado_em=utcnow_naive(),
                preparado_em=None,
                enviado_em=None,
                finalizado_em=None,
                correlation_id=saida.correlation_id,
                classificacao_resultado=None,
                motivo_recuperacao=motivo,
                chave_recuperacao=chave,
            )
            db.session.add(tentativa)
            db.session.flush()
            tentativa_id = int(tentativa.id)
            texto = str(interpretacao.texto_resposta)
            phone_number_id = str(evento.contexto_destino)
            destinatario = str(evento.sujeito_externo)
        db.session.commit()
    except IntegrityError:
        return _depois_da_colisao(saida_id, motivo, chave)
    except OperationalError as exc:
        if "locked" not in str(exc).lower():
            db.session.rollback()
            raise
        return _depois_da_colisao(saida_id, motivo, chave)
    return (tentativa_id, texto, phone_number_id, destinatario)


def _fechar(tentativa_id: int, saida_id: int, http) -> TentativaEnvioCanal | None:
    tentativa = db.session.get(TentativaEnvioCanal, tentativa_id)
    saida = db.session.get(EventoCanalSaida, saida_id)
    if tentativa is None or saida is None:
        return None
    if tentativa.status_tentativa != TentativaEnvioCanal.STATUS_RESERVADA:
        return tentativa
    agora = utcnow_naive()
    if http.aceito and http.provider_message_id:
        tentativa.status_tentativa = TentativaEnvioCanal.STATUS_ACEITA
        tentativa.provider_message_id = http.provider_message_id
        tentativa.codigo_erro = None
        tentativa.enviado_em = agora
        tentativa.finalizado_em = agora
        tentativa.classificacao_resultado = None
        saida.status_envio = EventoCanalSaida.STATUS_ACEITO
        saida.provider_message_id = http.provider_message_id
        saida.codigo_erro = None
        saida.enviado_em = agora
    else:
        codigo = http.codigo_erro or EventoCanalSaida.CODIGO_RESPOSTA_INVALIDA
        if codigo not in _ERROS_ELEGIVEIS:
            codigo = EventoCanalSaida.CODIGO_RESPOSTA_INVALIDA
        tentativa.status_tentativa = TentativaEnvioCanal.STATUS_ERRO
        tentativa.provider_message_id = None
        tentativa.codigo_erro = codigo
        tentativa.enviado_em = None
        tentativa.finalizado_em = agora
        tentativa.classificacao_resultado = None
        saida.status_envio = EventoCanalSaida.STATUS_ERRO
        saida.provider_message_id = None
        saida.codigo_erro = codigo
        saida.enviado_em = None
    db.session.commit()
    return db.session.get(TentativaEnvioCanal, tentativa_id)


def _recuperar(saida_id: int, motivo: str, chave: str) -> ResultadoRecuperacaoTexto:
    gravada = _ja_gravada(saida_id, chave)
    if gravada is not None:
        return gravada
    saida = db.session.get(EventoCanalSaida, saida_id)
    if saida is None:
        return _resultado(CODIGO_SAIDA_AUSENTE)
    bloqueio = _diagnostico(saida, motivo=motivo)
    if bloqueio is not None:
        return bloqueio
    ultima = _ultima(saida_id)
    if ultima is None:
        return _resultado(CODIGO_ESTADO_NAO_ELEGIVEL, saida_id=saida_id, motivo=motivo)
    foto = int(ultima.numero_tentativa)
    reservado = _reservar(saida_id, motivo, chave, foto)
    if isinstance(reservado, ResultadoRecuperacaoTexto):
        return reservado
    tentativa_id, texto, phone_number_id, destinatario = reservado
    http = WhatsAppMetaCloudApiAdapter().enviar_texto(
        phone_number_id=phone_number_id,
        destinatario=destinatario,
        texto=texto,
    )
    try:
        fechada = _fechar(tentativa_id, saida_id, http)
    except Exception:
        db.session.rollback()
        fechada = db.session.get(TentativaEnvioCanal, tentativa_id)
    if fechada is None:
        return _resultado(
            TentativaEnvioCanal.STATUS_RESERVADA,
            saida_id=saida_id,
            tentativa_id=tentativa_id,
            motivo=motivo,
        )
    return _de_tentativa(fechada, _codigo_existente(fechada))


def recuperar_saida_textual(
    saida_id: int,
    motivo: str,
    chave_recuperacao: str,
    operador_contexto=None,
) -> ResultadoRecuperacaoTexto:
    """Nova tentativa explícita de texto comum. Não dispara sozinha.

    A mesma chave_recuperacao repete o resultado já gravado e não envia
    de novo. Outra decisão humana precisa de outra chave.
    """
    del operador_contexto
    if not _id_valido(saida_id):
        return _log(_resultado(CODIGO_SAIDA_INVALIDA))
    if not _motivo_valido(motivo):
        return _log(_resultado(CODIGO_MOTIVO_INVALIDO, saida_id=saida_id))
    chave = _chave_normalizada(chave_recuperacao)
    if chave is None:
        return _log(_resultado(CODIGO_CHAVE_INVALIDA, saida_id=saida_id, motivo=motivo))
    with _trava(int(saida_id)):
        return _log(_recuperar(int(saida_id), motivo, chave))
