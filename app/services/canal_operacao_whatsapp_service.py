"""Liga mensagem de usuário vinculado ao chat operacional já existente.

Não cria outro chat, não interpreta onboarding e não abate token de IA.
A pergunta sai só de ConteudoTextualCanal. O User sai da identidade
já validada. O estado vigente da franquia é lido antes da execução:
se impedir uso, a orientação comercial sai e a Júlia não é chamada.
Depois da conclusão útil, a interação comercial do canal é registrada.
A saída para a Meta continua independente desse registro.
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from uuid import uuid4

from flask import g, has_request_context
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.cleiton_doc_contracts import FLOW_TYPE_JULIA_CHAT
from app.consumo_identidade import identidade_http_anonimo
from app.extensions import db
from app.infra import get_julia_chat_max_history
from app.julia_doc_context import build_julia_document_context_for_chat
from app.models import (
    ConteudoTextualCanal,
    Conta,
    EventoCanalRecebido,
    EventoCanalSaida,
    ExecucaoOperacionalCanal,
    Franquia,
    IdentidadeCanalExterna,
    User,
    utcnow_naive,
)
from app.run_julia_chat import (
    GENERIC_REPLY_FALLBACK,
    PROVIDER_UNAVAILABLE_REPLY,
    chat_julia_reply,
)
from app.services.canal_aquisicao_service import PROVEDOR_WHATSAPP_META
from app.services.canal_entrada_processamento_service import (
    RecusaConteudo,
    validar_texto_operacional,
)
from app.services.canal_consumo_whatsapp_service import (
    registrar_consumo_operacional_canal,
)
from app.services.canal_orientacao_franquia_service import (
    interromper_se_franquia_impede,
    orientacao_existente,
)
from app.services.canal_saida_whatsapp_service import enviar_texto_da_execucao_operacional
from app.services.cleiton_ai_data_governance import USER_SAFE_PREPARATION_FAILED

logger = logging.getLogger(__name__)

CODIGO_EVENTO_AUSENTE = "evento_ausente"
CODIGO_EVENTO_INVALIDO = "evento_invalido"

_CORRELATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_RESPOSTA_MAXIMA = ExecucaoOperacionalCanal.TEXTO_MAXIMO

_travas: dict[int, threading.Lock] = {}
_travas_guard = threading.Lock()


@dataclass(frozen=True)
class ResultadoOperacionalCanal:
    codigo: str
    evento_id: int | None = None
    identidade_id: int | None = None
    user_id: int | None = None
    execucao_id: int | None = None
    execution_id: str | None = None
    saida_id: int | None = None
    correlation_id: str | None = None
    status_envio: str | None = None


def _trava(evento_id: int) -> threading.Lock:
    with _travas_guard:
        lock = _travas.get(evento_id)
        if lock is None:
            lock = threading.Lock()
            _travas[evento_id] = lock
        return lock


def _id_valido(valor: object) -> bool:
    return not isinstance(valor, bool) and isinstance(valor, int) and valor > 0


def _log(resultado: ResultadoOperacionalCanal) -> None:
    logger.info(
        "canal_operacional evento_id=%s user_id=%s execucao_id=%s execution_id=%s "
        "codigo=%s correlation_id=%s status=%s",
        resultado.evento_id if resultado.evento_id is not None else "-",
        resultado.user_id if resultado.user_id is not None else "-",
        resultado.execucao_id if resultado.execucao_id is not None else "-",
        resultado.execution_id or "-",
        resultado.codigo,
        resultado.correlation_id or "-",
        resultado.status_envio or "-",
    )


def _resultado(
    codigo: str,
    *,
    evento_id: int | None = None,
    identidade_id: int | None = None,
    user_id: int | None = None,
    execucao_id: int | None = None,
    execution_id: str | None = None,
    saida_id: int | None = None,
    correlation_id: str | None = None,
    status_envio: str | None = None,
) -> ResultadoOperacionalCanal:
    return ResultadoOperacionalCanal(
        codigo=codigo,
        evento_id=evento_id,
        identidade_id=identidade_id,
        user_id=user_id,
        execucao_id=execucao_id,
        execution_id=execution_id,
        saida_id=saida_id,
        correlation_id=correlation_id,
        status_envio=status_envio,
    )


def _correlation(evento: EventoCanalRecebido) -> str | None:
    valor = evento.correlation_id
    if isinstance(valor, str) and _CORRELATION_RE.fullmatch(valor):
        return valor
    return None


def _buscar(evento_id: int) -> ExecucaoOperacionalCanal | None:
    return ExecucaoOperacionalCanal.query.filter_by(evento_id=evento_id).one_or_none()


def _da_linha(
    row: ExecucaoOperacionalCanal,
    *,
    codigo: str | None = None,
    saida_id: int | None = None,
    status_envio: str | None = None,
) -> ResultadoOperacionalCanal:
    user_id = int(row.user_id) if _id_valido(row.user_id) else None
    return _resultado(
        codigo or row.codigo,
        evento_id=int(row.evento_id),
        identidade_id=int(row.identidade_id),
        user_id=user_id,
        execucao_id=int(row.id),
        execution_id=row.execution_id,
        saida_id=saida_id,
        correlation_id=row.correlation_id,
        status_envio=status_envio,
    )


def _inserir(row: ExecucaoOperacionalCanal) -> ExecucaoOperacionalCanal | None:
    try:
        with db.session.begin_nested():
            db.session.add(row)
            db.session.flush()
            execucao_id = int(row.id)
    except IntegrityError:
        return None
    db.session.commit()
    return db.session.get(ExecucaoOperacionalCanal, execucao_id)


def _falha(
    evento: EventoCanalRecebido,
    identidade_id: int,
    codigo: str,
    user_id: int | None = None,
) -> ResultadoOperacionalCanal:
    agora = utcnow_naive()
    gravada = _inserir(
        ExecucaoOperacionalCanal(
            evento_id=int(evento.id),
            identidade_id=int(identidade_id),
            user_id=int(user_id) if _id_valido(user_id) else None,
            codigo=codigo,
            estado=ExecucaoOperacionalCanal.ESTADO_FALHA,
            texto_resposta=None,
            execution_id=None,
            conclusao_util=0,
            correlation_id=_correlation(evento),
            criada_em=agora,
            atualizada_em=agora,
        )
    )
    if gravada is None:
        existente = _buscar(int(evento.id))
        if existente is not None:
            return _da_linha(existente)
        return _resultado(
            codigo,
            evento_id=int(evento.id),
            identidade_id=identidade_id,
            correlation_id=_correlation(evento),
        )
    return _da_linha(gravada)


def _texto_do_evento(evento_id: int) -> str | None:
    row = ConteudoTextualCanal.query.filter_by(evento_id=evento_id).one_or_none()
    if row is None:
        return None
    try:
        return validar_texto_operacional(row.texto)
    except RecusaConteudo:
        return None


def _user_id_existente(identidade: IdentidadeCanalExterna) -> int | None:
    if not _id_valido(identidade.user_id):
        return None
    if db.session.get(User, int(identidade.user_id)) is None:
        return None
    return int(identidade.user_id)


def _codigo_vinculo(identidade: IdentidadeCanalExterna | None) -> str | None:
    """None quando o User da identidade vinculada está utilizável."""
    if identidade is None:
        return ExecucaoOperacionalCanal.CODIGO_IDENTIDADE_INVALIDA
    if (
        identidade.estado != IdentidadeCanalExterna.ESTADO_VINCULADA
        or identidade.revogada_em is not None
        or identidade.vinculada_em is None
        or identidade.provedor != PROVEDOR_WHATSAPP_META
        or not _id_valido(identidade.user_id)
    ):
        return ExecucaoOperacionalCanal.CODIGO_IDENTIDADE_INVALIDA
    user = db.session.get(User, int(identidade.user_id))
    if user is None:
        return ExecucaoOperacionalCanal.CODIGO_CONTEXTO_INDISPONIVEL
    if db.session.get(Conta, user.conta_id) is None:
        return ExecucaoOperacionalCanal.CODIGO_CONTEXTO_INDISPONIVEL
    if db.session.get(Franquia, user.franquia_id) is None:
        return ExecucaoOperacionalCanal.CODIGO_CONTEXTO_INDISPONIVEL
    return None


def _fechar_reserva(
    row: ExecucaoOperacionalCanal,
    codigo: str,
) -> ResultadoOperacionalCanal:
    atual = db.session.get(ExecucaoOperacionalCanal, int(row.id))
    if atual is None:
        return _resultado(CODIGO_EVENTO_AUSENTE, evento_id=int(row.evento_id))
    if atual.estado != ExecucaoOperacionalCanal.ESTADO_RESERVADA:
        return _retomar(atual)
    atual.estado = ExecucaoOperacionalCanal.ESTADO_FALHA
    atual.codigo = codigo
    atual.texto_resposta = None
    atual.conclusao_util = 0
    atual.atualizada_em = utcnow_naive()
    db.session.commit()
    fechada = db.session.get(ExecucaoOperacionalCanal, int(row.id))
    if fechada is None:
        return _resultado(codigo, evento_id=int(row.evento_id))
    return _da_linha(fechada)


def _contexto_documental(evento_id: int) -> dict:
    try:
        return build_julia_document_context_for_chat()
    except Exception:
        logger.info(
            "canal_operacional evento_id=%s codigo=contexto_documental_degradado",
            evento_id,
        )
        return {
            "context_block": "",
            "gemini_file_parts": None,
            "flow_type": FLOW_TYPE_JULIA_CHAT,
        }


def _classificar_resposta(bruto: object) -> tuple[str, str | None]:
    if not isinstance(bruto, dict):
        return ExecucaoOperacionalCanal.CODIGO_RESPOSTA_INUTILIZAVEL, None
    reply = bruto.get("reply")
    if not isinstance(reply, str):
        return ExecucaoOperacionalCanal.CODIGO_RESPOSTA_INUTILIZAVEL, None
    texto = reply.strip()
    if texto == USER_SAFE_PREPARATION_FAILED:
        return ExecucaoOperacionalCanal.CODIGO_GOVERNANCA_NEGADA, None
    if texto == PROVIDER_UNAVAILABLE_REPLY:
        return ExecucaoOperacionalCanal.CODIGO_PROVEDOR_INDISPONIVEL, None
    if texto == GENERIC_REPLY_FALLBACK:
        return ExecucaoOperacionalCanal.CODIGO_FALHA_PROVEDOR, None
    if not texto or len(texto) > _RESPOSTA_MAXIMA:
        return ExecucaoOperacionalCanal.CODIGO_RESPOSTA_INUTILIZAVEL, None
    return ExecucaoOperacionalCanal.CODIGO_RESPOSTA, texto


def _limite_historico() -> int:
    try:
        return max(1, int(get_julia_chat_max_history()))
    except Exception:
        return 10


def _chamar_julia(
    texto: str,
    execution_id: str,
    *,
    documental: dict,
    max_history: int,
) -> dict:
    """Reusa o chat web. A identidade de consumo fica anônima para não abater crédito."""
    tinha = has_request_context() and hasattr(g, "identidade")
    anterior = getattr(g, "identidade", None) if has_request_context() else None
    if has_request_context():
        g.identidade = identidade_http_anonimo()
    try:
        return chat_julia_reply(
            texto,
            [],
            max_history=max_history,
            document_context_block=documental.get("context_block") or None,
            document_file_parts=documental.get("gemini_file_parts") or None,
            flow_type=documental.get("flow_type") or FLOW_TYPE_JULIA_CHAT,
            execution_id=execution_id,
            allow_provider_fallback=False,
        )
    finally:
        if has_request_context():
            if tinha:
                g.identidade = anterior
            elif hasattr(g, "identidade"):
                del g.identidade


def _registrar_consumo(row: ExecucaoOperacionalCanal) -> None:
    """Registra a interação já concluída. Falha financeira não apaga a resposta."""
    if (
        row.estado != ExecucaoOperacionalCanal.ESTADO_CONCLUIDA
        or row.conclusao_util != 1
    ):
        return
    execucao_id = int(row.id)
    evento_id = int(row.evento_id)
    user_id = int(row.user_id) if _id_valido(row.user_id) else None
    try:
        registrar_consumo_operacional_canal(execucao_id)
    except Exception as exc:
        db.session.rollback()
        logger.info(
            "canal_consumo evento_id=%s execucao_id=%s user_id=%s codigo=falha_tecnica erro=%s",
            evento_id,
            execucao_id,
            user_id if user_id is not None else "-",
            type(exc).__name__,
        )


def _da_interrupcao(
    item: object,
    *,
    identidade_id: int | None,
    user_id: int | None,
) -> ResultadoOperacionalCanal:
    saida_id = getattr(item, "saida_id", None)
    return _resultado(
        str(getattr(item, "codigo", CODIGO_EVENTO_AUSENTE)),
        evento_id=int(getattr(item, "evento_id")),
        identidade_id=identidade_id,
        user_id=user_id,
        saida_id=int(saida_id) if _id_valido(saida_id) else None,
        correlation_id=getattr(item, "correlation_id", None),
        status_envio=getattr(item, "status_envio", None),
    )


def _enviar(row: ExecucaoOperacionalCanal) -> ResultadoOperacionalCanal:
    envio = enviar_texto_da_execucao_operacional(int(row.id))
    saida_id = int(envio.saida_id) if _id_valido(envio.saida_id) else None
    return _da_linha(
        row,
        codigo=envio.codigo,
        saida_id=saida_id,
        status_envio=envio.status_envio,
    )


def _marcar_chamada(row: ExecucaoOperacionalCanal) -> ExecucaoOperacionalCanal | None:
    """Só uma transação sai de reservada. O commit acontece antes do provider."""
    execucao_id = int(row.id)
    db.session.commit()
    resultado = db.session.execute(
        update(ExecucaoOperacionalCanal)
        .where(ExecucaoOperacionalCanal.id == execucao_id)
        .where(ExecucaoOperacionalCanal.estado == ExecucaoOperacionalCanal.ESTADO_RESERVADA)
        .values(
            estado=ExecucaoOperacionalCanal.ESTADO_CHAMADA_INICIADA,
            atualizada_em=utcnow_naive(),
        )
        .execution_options(synchronize_session=False)
    )
    if int(resultado.rowcount or 0) != 1:
        db.session.rollback()
        return None
    db.session.commit()
    return db.session.get(ExecucaoOperacionalCanal, execucao_id)


def _concluir_chamada(
    execucao_id: int,
    codigo: str,
    texto: str | None,
) -> ExecucaoOperacionalCanal | None:
    db.session.rollback()
    row = db.session.get(ExecucaoOperacionalCanal, execucao_id)
    if row is None or row.estado != ExecucaoOperacionalCanal.ESTADO_CHAMADA_INICIADA:
        return row
    row.atualizada_em = utcnow_naive()
    if codigo == ExecucaoOperacionalCanal.CODIGO_RESPOSTA and texto:
        row.estado = ExecucaoOperacionalCanal.ESTADO_CONCLUIDA
        row.codigo = ExecucaoOperacionalCanal.CODIGO_RESPOSTA
        row.texto_resposta = texto
        row.conclusao_util = 1
    else:
        row.estado = ExecucaoOperacionalCanal.ESTADO_FALHA
        row.codigo = codigo
        row.texto_resposta = None
        row.conclusao_util = 0
    db.session.commit()
    return db.session.get(ExecucaoOperacionalCanal, execucao_id)


def _chamar(row: ExecucaoOperacionalCanal, texto: str) -> ResultadoOperacionalCanal:
    documental = _contexto_documental(int(row.evento_id))
    max_history = _limite_historico()
    iniciada = _marcar_chamada(row)
    if iniciada is None:
        atual = db.session.get(ExecucaoOperacionalCanal, int(row.id))
        if atual is None:
            return _resultado(CODIGO_EVENTO_AUSENTE, evento_id=int(row.evento_id))
        return _retomar(atual)
    execucao_id = int(iniciada.id)
    execution_id = str(iniciada.execution_id)
    try:
        bruto = _chamar_julia(
            texto,
            execution_id,
            documental=documental,
            max_history=max_history,
        )
    except Exception as exc:
        logger.info(
            "canal_operacional evento_id=%s codigo=%s erro=%s",
            int(iniciada.evento_id),
            ExecucaoOperacionalCanal.CODIGO_RESULTADO_INCERTO,
            type(exc).__name__,
        )
        db.session.rollback()
        conservada = db.session.get(ExecucaoOperacionalCanal, execucao_id)
        if conservada is None:
            return _resultado(ExecucaoOperacionalCanal.CODIGO_RESULTADO_INCERTO)
        return _da_linha(conservada, codigo=ExecucaoOperacionalCanal.CODIGO_RESULTADO_INCERTO)
    codigo, resposta = _classificar_resposta(bruto)
    fechada = _concluir_chamada(execucao_id, codigo, resposta)
    if fechada is None:
        return _resultado(
            ExecucaoOperacionalCanal.CODIGO_RESULTADO_INCERTO,
            evento_id=int(row.evento_id),
            execution_id=execution_id,
        )
    if fechada.estado != ExecucaoOperacionalCanal.ESTADO_CONCLUIDA:
        return _da_linha(fechada)
    _registrar_consumo(fechada)
    return _enviar(fechada)


def _retomar(row: ExecucaoOperacionalCanal) -> ResultadoOperacionalCanal:
    if row.estado == ExecucaoOperacionalCanal.ESTADO_CHAMADA_INICIADA:
        return _da_linha(row, codigo=ExecucaoOperacionalCanal.CODIGO_RESULTADO_INCERTO)
    if row.estado == ExecucaoOperacionalCanal.ESTADO_FALHA:
        return _da_linha(row)
    if row.estado == ExecucaoOperacionalCanal.ESTADO_CONCLUIDA:
        _registrar_consumo(row)
        return _enviar(row)
    if row.estado != ExecucaoOperacionalCanal.ESTADO_RESERVADA:
        return _da_linha(row, codigo=ExecucaoOperacionalCanal.CODIGO_ERRO_TECNICO)
    evento = db.session.get(EventoCanalRecebido, int(row.evento_id))
    identidade = db.session.get(IdentidadeCanalExterna, int(row.identidade_id))
    if (
        evento is None
        or identidade is None
        or identidade.sujeito_externo != evento.sujeito_externo
    ):
        return _fechar_reserva(row, ExecucaoOperacionalCanal.CODIGO_IDENTIDADE_INVALIDA)
    codigo_vinculo = _codigo_vinculo(identidade)
    if codigo_vinculo is not None:
        return _fechar_reserva(row, codigo_vinculo)
    texto = _texto_do_evento(int(row.evento_id))
    if texto is None:
        return _fechar_reserva(row, ExecucaoOperacionalCanal.CODIGO_MENSAGEM_INVALIDA)
    # A reserva ainda não chamou a Júlia. O mesmo gate da execução nova
    # relê a franquia vigente antes do claim reservada → chamada_iniciada.
    user = db.session.get(User, int(identidade.user_id))
    if user is None:
        return _fechar_reserva(row, ExecucaoOperacionalCanal.CODIGO_CONTEXTO_INDISPONIVEL)
    parada = interromper_se_franquia_impede(evento, user)
    if parada is not None:
        return _da_interrupcao(
            parada,
            identidade_id=int(row.identidade_id),
            user_id=int(user.id),
        )
    return _chamar(row, texto)


def _preparar(
    evento: EventoCanalRecebido,
    identidade_id: int | None,
) -> ExecucaoOperacionalCanal | ResultadoOperacionalCanal:
    if not _id_valido(identidade_id):
        return _resultado(
            ExecucaoOperacionalCanal.CODIGO_IDENTIDADE_INVALIDA,
            evento_id=int(evento.id),
            correlation_id=_correlation(evento),
        )
    identidade_id = int(identidade_id)
    identidade = db.session.get(IdentidadeCanalExterna, identidade_id)
    if identidade is None or identidade.sujeito_externo != evento.sujeito_externo:
        if identidade is None:
            return _resultado(
                ExecucaoOperacionalCanal.CODIGO_IDENTIDADE_INVALIDA,
                evento_id=int(evento.id),
                identidade_id=identidade_id,
                correlation_id=_correlation(evento),
            )
        return _falha(
            evento,
            identidade_id,
            ExecucaoOperacionalCanal.CODIGO_IDENTIDADE_INVALIDA,
            _user_id_existente(identidade),
        )
    codigo_vinculo = _codigo_vinculo(identidade)
    if codigo_vinculo is not None:
        return _falha(
            evento,
            identidade_id,
            codigo_vinculo,
            _user_id_existente(identidade),
        )
    user_id = int(identidade.user_id)
    user = db.session.get(User, user_id)
    if user is None:
        return _falha(
            evento,
            identidade_id,
            ExecucaoOperacionalCanal.CODIGO_CONTEXTO_INDISPONIVEL,
            user_id,
        )
    parada = interromper_se_franquia_impede(evento, user)
    if parada is not None:
        return _da_interrupcao(parada, identidade_id=identidade_id, user_id=user_id)
    texto = _texto_do_evento(int(evento.id))
    if texto is None:
        return _falha(
            evento,
            identidade_id,
            ExecucaoOperacionalCanal.CODIGO_MENSAGEM_INVALIDA,
            user_id,
        )
    agora = utcnow_naive()
    gravada = _inserir(
        ExecucaoOperacionalCanal(
            evento_id=int(evento.id),
            identidade_id=identidade_id,
            user_id=user_id,
            codigo=ExecucaoOperacionalCanal.CODIGO_EM_TRATAMENTO,
            estado=ExecucaoOperacionalCanal.ESTADO_RESERVADA,
            texto_resposta=None,
            execution_id=str(uuid4()),
            conclusao_util=0,
            correlation_id=_correlation(evento),
            criada_em=agora,
            atualizada_em=agora,
        )
    )
    if gravada is None:
        existente = _buscar(int(evento.id))
        if existente is None:
            return _resultado(
                ExecucaoOperacionalCanal.CODIGO_ERRO_TECNICO,
                evento_id=int(evento.id),
                identidade_id=identidade_id,
                user_id=user_id,
                correlation_id=_correlation(evento),
            )
        return _retomar(existente)
    return _chamar(gravada, texto)


def _executar(evento_id: int, identidade_id: int | None) -> ResultadoOperacionalCanal:
    existente = _buscar(evento_id)
    if existente is not None:
        return _retomar(existente)
    orientada = orientacao_existente(evento_id)
    if orientada is not None:
        return _da_interrupcao(
            orientada,
            identidade_id=int(identidade_id) if _id_valido(identidade_id) else None,
            user_id=None,
        )
    evento = db.session.get(EventoCanalRecebido, evento_id)
    if evento is None:
        return _resultado(CODIGO_EVENTO_AUSENTE)
    if (
        evento.tipo_evento != EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL
        or evento.status_processamento != EventoCanalRecebido.STATUS_ROTEADO
    ):
        return _resultado(
            CODIGO_EVENTO_INVALIDO,
            evento_id=int(evento.id),
            correlation_id=_correlation(evento),
        )
    return _preparar(evento, identidade_id)


def executar_operacao_canal(
    evento_id: int,
    identidade_id: int | None = None,
) -> ResultadoOperacionalCanal:
    """Executa ou retoma a operação do evento. Não recebe payload da Meta."""
    if not _id_valido(evento_id):
        resultado = _resultado(CODIGO_EVENTO_INVALIDO)
        _log(resultado)
        return resultado
    with _trava(int(evento_id)):
        resultado = _executar(int(evento_id), identidade_id)
        _log(resultado)
        return resultado


def operacao_canal_pendente(evento_id: int) -> bool:
    """Há etapa segura seguinte: chamar a operação ou só enviar a resposta já gravada."""
    if not _id_valido(evento_id):
        return False
    row = _buscar(int(evento_id))
    if row is None:
        return False
    if row.estado == ExecucaoOperacionalCanal.ESTADO_RESERVADA:
        return True
    if (
        row.estado == ExecucaoOperacionalCanal.ESTADO_CONCLUIDA
        and row.conclusao_util == 1
        and isinstance(row.texto_resposta, str)
        and row.texto_resposta
    ):
        saida = (
            EventoCanalSaida.query.filter_by(evento_entrada_id=int(evento_id))
            .order_by(EventoCanalSaida.id.asc())
            .first()
        )
        return saida is None
    return False
