"""
Processa um EventoCanalRecebido já autenticado.

Monta uma mensagem interna sem JSON do provider, resolve a identidade no
serviço existente e classifica a rota. O texto usado é só o que já está em
ConteudoTextualCanal. Não envia mensagem, não chama modelo e não consulta
billing.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    ConteudoTextualCanal,
    EventoCanalRecebido,
    IdentidadeCanalExterna,
    User,
)
from app.services.canal_aquisicao_service import (
    PROVEDOR_WHATSAPP_META,
    CanalAquisicaoError,
    _buscar_identidade_ativa,
    obter_ou_criar_identidade_externa,
)

logger = logging.getLogger(__name__)

PROVEDOR_IDENTIDADE_WHATSAPP = PROVEDOR_WHATSAPP_META

CODIGO_ROTEADO = "roteado"
CODIGO_JA_PROCESSADO = "evento_ja_processado"
CODIGO_EM_PROCESSAMENTO = "evento_em_processamento"
CODIGO_AGUARDANDO_MIDIA = "aguardando_suporte_midia"
CODIGO_AGUARDANDO_SAIDA = "aguardando_lote_saida"
CODIGO_IGNORADO = "evento_ignorado"
CODIGO_SUJEITO_AUSENTE = "sujeito_ausente"
CODIGO_TEXTO_AUSENTE = "texto_ausente"
CODIGO_TEXTO_VAZIO = "texto_vazio"
CODIGO_TEXTO_LIMITE = "texto_acima_do_limite"
CODIGO_TEXTO_INVALIDO = "texto_invalido"
CODIGO_TEXTO_NAO_APLICAVEL = "texto_nao_aplicavel"
CODIGO_CONTEUDO_DIVERGENTE = "conteudo_divergente"
CODIGO_IDENTIDADE_INCOERENTE = "identidade_incoerente"
CODIGO_IDENTIDADE_REVOGADA = "identidade_revogada"
CODIGO_PROVIDER_NAO_SUPORTADO = "provider_nao_suportado"
CODIGO_EVENTO_AUSENTE = "evento_ausente"
CODIGO_EVENTO_INVALIDO = "evento_invalido"
CODIGO_ERRO_SEGURO = "erro_seguro"

ROTA_GUEST = "guest"
ROTA_ONBOARDING = "onboarding"
ROTA_USUARIO_VINCULADO = "usuario_vinculado"
ROTA_BLOQUEADA = "bloqueada"
ROTA_REVOGADA = "revogada"

_STATUS_TERMINAL = {
    CODIGO_ROTEADO: EventoCanalRecebido.STATUS_ROTEADO,
    CODIGO_AGUARDANDO_SAIDA: EventoCanalRecebido.STATUS_ROTEADO,
    CODIGO_IDENTIDADE_REVOGADA: EventoCanalRecebido.STATUS_ROTEADO,
    CODIGO_AGUARDANDO_MIDIA: EventoCanalRecebido.STATUS_AGUARDANDO_SUPORTE_MIDIA,
    CODIGO_IGNORADO: EventoCanalRecebido.STATUS_IGNORADO,
}

_travas: dict[int, threading.Lock] = {}
_travas_guard = threading.Lock()


class RecusaConteudo(Exception):
    """Recusa do texto operacional. A mensagem é só o código."""

    def __init__(self, codigo: str):
        self.codigo = codigo
        super().__init__(codigo)


class _RecusaRoteamento(Exception):
    def __init__(self, codigo: str):
        self.codigo = codigo
        super().__init__(codigo)


@dataclass(frozen=True)
class MensagemCanalEntrada:
    evento_id: int
    provider: str
    evento_externo_id: str
    sujeito_externo: str | None
    contexto_destino: str | None
    tipo: str
    texto: str | None
    recebido_em: datetime
    correlation_id: str | None


@dataclass(frozen=True)
class ResultadoRoteamentoCanal:
    codigo: str
    evento_id: int | None = None
    identidade_id: int | None = None
    rota: str | None = None
    estado_identidade: str | None = None
    correlation_id: str | None = None


def _trava(evento_id: int) -> threading.Lock:
    with _travas_guard:
        lock = _travas.get(evento_id)
        if lock is None:
            lock = threading.Lock()
            _travas[evento_id] = lock
        return lock


def _log(resultado: ResultadoRoteamentoCanal) -> None:
    logger.info(
        "canal_entrada evento_id=%s identidade_id=%s correlation_id=%s rota=%s codigo=%s",
        resultado.evento_id if resultado.evento_id is not None else "-",
        resultado.identidade_id if resultado.identidade_id is not None else "-",
        resultado.correlation_id or "-",
        resultado.rota or "-",
        resultado.codigo,
    )


def _id_valido(valor: object) -> bool:
    return not isinstance(valor, bool) and isinstance(valor, int) and valor > 0


def validar_texto_operacional(texto: object) -> str:
    """String não vazia após trim, dentro do limite. Sem campo extra."""
    if not isinstance(texto, str):
        raise RecusaConteudo(CODIGO_TEXTO_INVALIDO)
    limpo = texto.strip()
    if not limpo:
        raise RecusaConteudo(CODIGO_TEXTO_VAZIO)
    if len(limpo) > ConteudoTextualCanal.TEXTO_MAXIMO:
        raise RecusaConteudo(CODIGO_TEXTO_LIMITE)
    return limpo


def registrar_conteudo_textual_canal(
    evento_id: int,
    texto: str,
    *,
    commit: bool = False,
) -> ConteudoTextualCanal:
    """Persiste só o texto allowlisted. A primeira gravação permanece."""
    if not _id_valido(evento_id):
        raise RecusaConteudo(CODIGO_EVENTO_INVALIDO)
    limpo = validar_texto_operacional(texto)
    evento = db.session.get(EventoCanalRecebido, evento_id)
    if evento is None:
        raise RecusaConteudo(CODIGO_EVENTO_AUSENTE)
    if evento.tipo_evento != EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL:
        raise RecusaConteudo(CODIGO_TEXTO_NAO_APLICAVEL)
    existente = ConteudoTextualCanal.query.filter_by(evento_id=evento_id).one_or_none()
    if existente is not None:
        if existente.texto == limpo:
            return existente
        raise RecusaConteudo(CODIGO_CONTEUDO_DIVERGENTE)
    row = ConteudoTextualCanal(evento_id=evento_id, texto=limpo)
    try:
        with db.session.begin_nested():
            db.session.add(row)
            db.session.flush()
    except IntegrityError:
        repetido = ConteudoTextualCanal.query.filter_by(evento_id=evento_id).one_or_none()
        if repetido is not None and repetido.texto == limpo:
            return repetido
        raise RecusaConteudo(CODIGO_CONTEUDO_DIVERGENTE) from None
    if commit:
        db.session.commit()
    return row


def montar_mensagem_canal_entrada(evento: EventoCanalRecebido) -> MensagemCanalEntrada:
    """DTO interno. Texto só em mensagem textual, lido da tabela allowlisted."""
    texto = None
    if evento.tipo_evento == EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL:
        texto = _carregar_texto(int(evento.id))
    return MensagemCanalEntrada(
        evento_id=int(evento.id),
        provider=evento.provider,
        evento_externo_id=evento.evento_externo_id,
        sujeito_externo=evento.sujeito_externo,
        contexto_destino=evento.contexto_destino,
        tipo=evento.tipo_evento,
        texto=texto,
        recebido_em=evento.recebido_em,
        correlation_id=evento.correlation_id,
    )


def _carregar_texto(evento_id: int) -> str:
    row = ConteudoTextualCanal.query.filter_by(evento_id=evento_id).one_or_none()
    if row is None:
        raise _RecusaRoteamento(CODIGO_TEXTO_AUSENTE)
    try:
        return validar_texto_operacional(row.texto)
    except RecusaConteudo as exc:
        raise _RecusaRoteamento(exc.codigo) from None


def _vinculo_coerente(identidade: IdentidadeCanalExterna) -> bool:
    user_id = identidade.user_id
    if not _id_valido(user_id) or identidade.vinculada_em is None:
        return False
    return db.session.get(User, int(user_id)) is not None


def _classificar_identidade(identidade: IdentidadeCanalExterna) -> tuple[str, str]:
    estado = identidade.estado
    if estado == IdentidadeCanalExterna.ESTADO_REVOGADA:
        return ROTA_REVOGADA, estado
    if estado == IdentidadeCanalExterna.ESTADO_BLOQUEADA:
        return ROTA_BLOQUEADA, estado
    if estado == IdentidadeCanalExterna.ESTADO_VINCULADA:
        if not _vinculo_coerente(identidade):
            raise _RecusaRoteamento(CODIGO_IDENTIDADE_INCOERENTE)
        return ROTA_USUARIO_VINCULADO, estado
    if estado == IdentidadeCanalExterna.ESTADO_CADASTRO_EM_ANDAMENTO:
        return ROTA_ONBOARDING, estado
    if estado == IdentidadeCanalExterna.ESTADO_GUEST:
        return ROTA_GUEST, estado
    raise _RecusaRoteamento(CODIGO_ERRO_SEGURO)


def _rotear_textual(mensagem: MensagemCanalEntrada) -> ResultadoRoteamentoCanal:
    if not isinstance(mensagem.sujeito_externo, str) or not mensagem.sujeito_externo.strip():
        raise _RecusaRoteamento(CODIGO_SUJEITO_AUSENTE)
    try:
        identidade = obter_ou_criar_identidade_externa(
            provedor=PROVEDOR_IDENTIDADE_WHATSAPP,
            sujeito_externo=mensagem.sujeito_externo,
            contexto_destino=mensagem.contexto_destino,
            commit=False,
        )
    except CanalAquisicaoError as exc:
        raise _RecusaRoteamento(exc.codigo) from None
    rota, estado = _classificar_identidade(identidade)
    codigo = CODIGO_IDENTIDADE_REVOGADA if rota == ROTA_REVOGADA else CODIGO_ROTEADO
    return ResultadoRoteamentoCanal(
        codigo=codigo,
        evento_id=mensagem.evento_id,
        identidade_id=int(identidade.id),
        rota=rota,
        estado_identidade=estado,
        correlation_id=mensagem.correlation_id,
    )


def _rotear(evento: EventoCanalRecebido) -> ResultadoRoteamentoCanal:
    if evento.provider != EventoCanalRecebido.PROVIDER_META_WHATSAPP:
        raise _RecusaRoteamento(CODIGO_PROVIDER_NAO_SUPORTADO)
    mensagem = montar_mensagem_canal_entrada(evento)
    if mensagem.tipo == EventoCanalRecebido.TIPO_MIDIA:
        return ResultadoRoteamentoCanal(
            codigo=CODIGO_AGUARDANDO_MIDIA,
            evento_id=mensagem.evento_id,
            correlation_id=mensagem.correlation_id,
        )
    if mensagem.tipo == EventoCanalRecebido.TIPO_STATUS_ENTREGA:
        return ResultadoRoteamentoCanal(
            codigo=CODIGO_AGUARDANDO_SAIDA,
            evento_id=mensagem.evento_id,
            correlation_id=mensagem.correlation_id,
        )
    if mensagem.tipo == EventoCanalRecebido.TIPO_DESCONHECIDO:
        return ResultadoRoteamentoCanal(
            codigo=CODIGO_IGNORADO,
            evento_id=mensagem.evento_id,
            correlation_id=mensagem.correlation_id,
        )
    if mensagem.tipo != EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL:
        raise _RecusaRoteamento(CODIGO_ERRO_SEGURO)
    return _rotear_textual(mensagem)


def _reclamar(evento_id: int) -> bool:
    resultado = db.session.execute(
        update(EventoCanalRecebido)
        .where(EventoCanalRecebido.id == evento_id)
        .where(
            EventoCanalRecebido.status_processamento == EventoCanalRecebido.STATUS_RECEBIDO
        )
        .values(status_processamento=EventoCanalRecebido.STATUS_PROCESSANDO)
        .execution_options(synchronize_session="fetch")
    )
    if int(resultado.rowcount or 0) != 1:
        return False
    db.session.commit()
    return True


def _aplicar_status(evento_id: int, status: str) -> None:
    evento = db.session.get(EventoCanalRecebido, evento_id)
    if evento is None or evento.status_processamento != EventoCanalRecebido.STATUS_PROCESSANDO:
        return
    evento.status_processamento = status
    db.session.commit()


def _status_terminal(codigo: str) -> str:
    return _STATUS_TERMINAL.get(codigo, EventoCanalRecebido.STATUS_ERRO_SEGURO)


def _executar(evento_id: int) -> ResultadoRoteamentoCanal:
    evento = db.session.get(EventoCanalRecebido, evento_id)
    if evento is None or evento.status_processamento != EventoCanalRecebido.STATUS_PROCESSANDO:
        raise _RecusaRoteamento(CODIGO_ERRO_SEGURO)
    resultado = _rotear(evento)
    evento.status_processamento = _status_terminal(resultado.codigo)
    db.session.commit()
    return resultado


def _correlation(evento_id: int) -> str | None:
    evento = db.session.get(EventoCanalRecebido, evento_id)
    if evento is None:
        return None
    return evento.correlation_id


def _resultado_nao_reclamado(evento_id: int) -> ResultadoRoteamentoCanal:
    evento = db.session.get(EventoCanalRecebido, evento_id)
    if evento is None:
        return ResultadoRoteamentoCanal(codigo=CODIGO_EVENTO_AUSENTE)
    if evento.status_processamento == EventoCanalRecebido.STATUS_PROCESSANDO:
        codigo = CODIGO_EM_PROCESSAMENTO
    elif evento.status_processamento == EventoCanalRecebido.STATUS_RECEBIDO:
        codigo = CODIGO_EM_PROCESSAMENTO
    else:
        codigo = CODIGO_JA_PROCESSADO
    return ResultadoRoteamentoCanal(
        codigo=codigo,
        evento_id=int(evento.id),
        correlation_id=evento.correlation_id,
    )


def reconstruir_roteamento_canal(evento_id: int) -> ResultadoRoteamentoCanal:
    """Relê a rota de um evento textual já roteado.

    Usa a identidade ativa e a mesma classificação do processamento.
    Não reivindica o evento, não cria identidade e não grava status.
    """
    if not _id_valido(evento_id):
        return ResultadoRoteamentoCanal(codigo=CODIGO_EVENTO_INVALIDO)
    evento = db.session.get(EventoCanalRecebido, int(evento_id))
    if evento is None:
        return ResultadoRoteamentoCanal(codigo=CODIGO_EVENTO_AUSENTE)
    base = ResultadoRoteamentoCanal(
        codigo=CODIGO_JA_PROCESSADO,
        evento_id=int(evento.id),
        correlation_id=evento.correlation_id,
    )
    if (
        evento.tipo_evento != EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL
        or evento.status_processamento != EventoCanalRecebido.STATUS_ROTEADO
    ):
        return base
    sujeito = evento.sujeito_externo
    if not isinstance(sujeito, str) or not sujeito.strip():
        return base
    identidade = _buscar_identidade_ativa(PROVEDOR_IDENTIDADE_WHATSAPP, sujeito)
    if identidade is None:
        return base
    try:
        rota, estado = _classificar_identidade(identidade)
    except _RecusaRoteamento:
        return base
    codigo = CODIGO_IDENTIDADE_REVOGADA if rota == ROTA_REVOGADA else CODIGO_ROTEADO
    return ResultadoRoteamentoCanal(
        codigo=codigo,
        evento_id=int(evento.id),
        identidade_id=int(identidade.id),
        rota=rota,
        estado_identidade=estado,
        correlation_id=evento.correlation_id,
    )


def processar_evento_canal_recebido(evento_id: int) -> ResultadoRoteamentoCanal:
    """Reivindica o evento uma vez e devolve a rota. Não relança erro de domínio."""
    if not _id_valido(evento_id):
        resultado = ResultadoRoteamentoCanal(codigo=CODIGO_EVENTO_INVALIDO)
        _log(resultado)
        return resultado
    with _trava(evento_id):
        if not _reclamar(evento_id):
            resultado = _resultado_nao_reclamado(evento_id)
            _log(resultado)
            return resultado
        try:
            resultado = _executar(evento_id)
        except _RecusaRoteamento as recusa:
            db.session.rollback()
            _aplicar_status(evento_id, EventoCanalRecebido.STATUS_ERRO_SEGURO)
            resultado = ResultadoRoteamentoCanal(
                codigo=recusa.codigo,
                evento_id=evento_id,
                correlation_id=_correlation(evento_id),
            )
        except Exception:
            db.session.rollback()
            _aplicar_status(evento_id, EventoCanalRecebido.STATUS_ERRO_SEGURO)
            resultado = ResultadoRoteamentoCanal(
                codigo=CODIGO_ERRO_SEGURO,
                evento_id=evento_id,
                correlation_id=_correlation(evento_id),
            )
        _log(resultado)
        return resultado
