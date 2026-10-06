"""Registra a interação comercial de uma execução WhatsApp útil.

O Gerenciador de Franquia continua dono do saldo. Este módulo identifica
o User originador e a franquia vigente dele e grava um consumo idempotente.
Se a taxa do Bloco D existe, a apropriação ocorre em seguida. Sem taxa
positiva, o consumo permanece pendente e a franquia não muda.

Não cobra guest, onboarding, status, mídia, falha nem token de IA.
Não guarda texto, telefone, e-mail nem payload.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.exc import IntegrityError

from app.consumo_identidade import identidade_de_usuario
from app.extensions import db
from app.models import (
    CleitonCostConfig,
    ConsumoInteracaoCanal,
    EventoCanalRecebido,
    ExecucaoOperacionalCanal,
    Franquia,
    IdentidadeCanalExterna,
    User,
    utcnow_naive,
)
from app.services.canal_aquisicao_service import PROVEDOR_WHATSAPP_META
from app.services.cleiton_franquia_leitura_service import ler_franquia_operacional_cleiton

logger = logging.getLogger(__name__)

TIPO_ORIGEM_CANAL = "canal_whatsapp"

CODIGO_REGISTRADO = "consumo_registrado"
CODIGO_IDEMPOTENTE = "consumo_idempotente"
CODIGO_INELEGIVEL = "consumo_inelegivel"
CODIGO_FALHA_TECNICA = "falha_tecnica"


@dataclass(frozen=True)
class ResultadoConsumoCanal:
    codigo: str
    consumo_id: int | None = None
    execucao_id: int | None = None
    evento_id: int | None = None
    user_id: int | None = None
    conta_id: int | None = None
    franquia_id: int | None = None
    estado: str | None = None
    motivo: str | None = None
    canal: str | None = None
    unidade: str | None = None
    quantidade: int | None = None
    correlation_id: str | None = None
    duplicado: bool = False


def chave_consumo_operacional(execucao_id: int) -> str:
    return ConsumoInteracaoCanal.chave_da_execucao(execucao_id)


def obter_regra_conversao_whatsapp() -> Decimal | None:
    """Interações úteis que 1 crédito compra, se o Bloco D já definiu a taxa.

    A fonte é CleitonCostConfig. Não há valor padrão. Régua de token,
    linha ou milissegundo não serve como taxa do canal.
    """
    config = db.session.get(CleitonCostConfig, 1)
    if config is None:
        return None
    return _taxa_positiva(config.interacoes_whatsapp_por_credito)


def registrar_consumo_operacional_canal(execucao_id: int) -> ResultadoConsumoCanal:
    """Uma execução útil produz no máximo um consumo. Replay devolve a mesma linha."""
    if not _id_valido(execucao_id):
        resultado = _resultado(CODIGO_INELEGIVEL)
        _log(resultado)
        return resultado
    execucao_id = int(execucao_id)
    existente = _por_execucao(execucao_id)
    if existente is not None:
        atual = _sincronizar_linha(existente, imediata=False)
        resultado = _de_linha(atual, duplicado=True)
        _log(resultado)
        return resultado
    try:
        return _registrar_novo(execucao_id)
    except Exception as exc:
        db.session.rollback()
        logger.info(
            "canal_consumo execucao_id=%s codigo=%s erro=%s",
            execucao_id,
            CODIGO_FALHA_TECNICA,
            type(exc).__name__,
        )
        return _persistir_falha_por_id(
            execucao_id,
            ConsumoInteracaoCanal.MOTIVO_FALHA_REGISTRO,
        )


def _registrar_novo(execucao_id: int) -> ResultadoConsumoCanal:
    execucao = db.session.get(ExecucaoOperacionalCanal, execucao_id)
    if not _elegivel(execucao):
        resultado = _resultado(CODIGO_INELEGIVEL, execucao_id=execucao_id)
        _log(resultado)
        return resultado
    contexto = _contexto_do_originador(execucao)
    if contexto is None:
        return _persistir_falha(
            execucao,
            ConsumoInteracaoCanal.MOTIVO_CONTEXTO_INDISPONIVEL,
        )
    conta_id, franquia_id = contexto
    # Sem taxa positiva o registro fica pronto e o saldo não muda.
    # Com taxa, a apropriação usa a régua central depois da linha gravada.
    if obter_regra_conversao_whatsapp() is None:
        logger.info(
            "canal_consumo execucao_id=%s user_id=%s franquia_id=%s codigo=%s",
            execucao_id,
            int(execucao.user_id),
            franquia_id,
            ConsumoInteracaoCanal.MOTIVO_TAXA_PENDENTE,
        )
    return _persistir_pronta(execucao, conta_id, franquia_id)


def _persistir_pronta(
    execucao: ExecucaoOperacionalCanal,
    conta_id: int,
    franquia_id: int,
) -> ResultadoConsumoCanal:
    agora = utcnow_naive()
    gravada = _inserir(
        _linha(
            execucao,
            conta_id=conta_id,
            franquia_id=franquia_id,
            estado=ConsumoInteracaoCanal.ESTADO_PRONTA,
            motivo=ConsumoInteracaoCanal.MOTIVO_TAXA_PENDENTE,
            agora=agora,
        )
    )
    if gravada is None:
        existente = _por_execucao(int(execucao.id))
        if existente is None:
            return _persistir_falha(
                execucao,
                ConsumoInteracaoCanal.MOTIVO_FALHA_REGISTRO,
            )
        atual = _sincronizar_linha(existente, imediata=False)
        resultado = _de_linha(atual, duplicado=True)
        _log(resultado)
        return resultado
    atual = _sincronizar_linha(gravada, imediata=True)
    resultado = _de_linha(atual, duplicado=False)
    _log(resultado)
    return resultado


def _sincronizar_linha(row: ConsumoInteracaoCanal, *, imediata: bool) -> ConsumoInteracaoCanal:
    consumo_id = int(row.id)
    try:
        from app.services.canal_apropriacao_whatsapp_service import sincronizar_interacao

        sincronizar_interacao(consumo_id, imediata=imediata)
    except Exception as exc:
        db.session.rollback()
        logger.info(
            "canal_consumo consumo_id=%s codigo=%s erro=%s",
            consumo_id,
            "erro_apropriacao",
            type(exc).__name__,
        )
    atual = db.session.get(ConsumoInteracaoCanal, consumo_id)
    return atual or row


def _contexto_do_originador(
    execucao: ExecucaoOperacionalCanal,
) -> tuple[int, int] | None:
    """Franquia vigente do User que originou a execução, não de outro membro."""
    user = db.session.get(User, int(execucao.user_id))
    if user is None or user.conta_id is None or user.franquia_id is None:
        return None
    if db.session.get(Franquia, int(user.franquia_id)) is None:
        return None
    ident = identidade_de_usuario(user, TIPO_ORIGEM_CANAL)
    if ident.get("usuario_id") != int(execucao.user_id):
        return None
    conta_id = ident.get("conta_id")
    franquia_id = ident.get("franquia_id")
    if conta_id is None or franquia_id is None:
        return None
    if int(conta_id) != int(user.conta_id) or int(franquia_id) != int(user.franquia_id):
        return None
    try:
        leitura = ler_franquia_operacional_cleiton(
            int(franquia_id),
            sincronizar_ciclo=False,
        )
    except Exception as exc:
        execucao_id = int(execucao.id)
        user_id = int(execucao.user_id)
        db.session.rollback()
        logger.info(
            "canal_consumo execucao_id=%s user_id=%s codigo=%s erro=%s",
            execucao_id,
            user_id,
            ConsumoInteracaoCanal.MOTIVO_CONTEXTO_INDISPONIVEL,
            type(exc).__name__,
        )
        return None
    if leitura is None or int(leitura.franquia_id) != int(franquia_id):
        return None
    return int(conta_id), int(franquia_id)


def _elegivel(execucao: ExecucaoOperacionalCanal | None) -> bool:
    if execucao is None:
        return False
    if execucao.estado != ExecucaoOperacionalCanal.ESTADO_CONCLUIDA:
        return False
    if execucao.conclusao_util != 1:
        return False
    if execucao.codigo != ExecucaoOperacionalCanal.CODIGO_RESPOSTA:
        return False
    if not isinstance(execucao.texto_resposta, str) or not execucao.texto_resposta:
        return False
    if not _id_valido(execucao.user_id):
        return False
    identidade = db.session.get(IdentidadeCanalExterna, int(execucao.identidade_id))
    if identidade is None:
        return False
    if identidade.provedor != PROVEDOR_WHATSAPP_META:
        return False
    if identidade.estado != IdentidadeCanalExterna.ESTADO_VINCULADA:
        return False
    if identidade.user_id is None or int(identidade.user_id) != int(execucao.user_id):
        return False
    evento = db.session.get(EventoCanalRecebido, int(execucao.evento_id))
    if evento is None:
        return False
    if evento.tipo_evento != EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL:
        return False
    if evento.provider != EventoCanalRecebido.PROVIDER_META_WHATSAPP:
        return False
    return True


def _linha(
    execucao: ExecucaoOperacionalCanal,
    *,
    conta_id: int | None,
    franquia_id: int | None,
    estado: str,
    motivo: str,
    agora,
) -> ConsumoInteracaoCanal:
    return ConsumoInteracaoCanal(
        execucao_operacional_canal_id=int(execucao.id),
        evento_canal_recebido_id=int(execucao.evento_id),
        user_id=int(execucao.user_id),
        conta_id=conta_id,
        franquia_id=franquia_id,
        tipo_consumo=ConsumoInteracaoCanal.TIPO_WHATSAPP_OPERACIONAL,
        quantidade=ConsumoInteracaoCanal.QUANTIDADE_INTERACAO,
        unidade=ConsumoInteracaoCanal.UNIDADE_INTERACAO,
        canal=ConsumoInteracaoCanal.CANAL_WHATSAPP,
        chave_idempotente=chave_consumo_operacional(int(execucao.id)),
        estado=estado,
        motivo=motivo,
        correlation_id=execucao.correlation_id,
        creditos_apropriados=None,
        criada_em=agora,
        atualizada_em=agora,
    )


def _persistir_falha(
    execucao: ExecucaoOperacionalCanal,
    motivo: str,
) -> ResultadoConsumoCanal:
    existente = _por_execucao(int(execucao.id))
    if existente is not None:
        resultado = _de_linha(existente, duplicado=True)
        _log(resultado)
        return resultado
    gravada = _inserir(
        _linha(
            execucao,
            conta_id=None,
            franquia_id=None,
            estado=ConsumoInteracaoCanal.ESTADO_FALHA,
            motivo=motivo,
            agora=utcnow_naive(),
        )
    )
    if gravada is None:
        existente = _por_execucao(int(execucao.id))
        if existente is not None:
            resultado = _de_linha(existente, duplicado=True)
            _log(resultado)
            return resultado
        resultado = _resultado(
            CODIGO_FALHA_TECNICA,
            execucao_id=int(execucao.id),
            evento_id=int(execucao.evento_id),
            user_id=int(execucao.user_id),
            estado=ConsumoInteracaoCanal.ESTADO_FALHA,
            motivo=motivo,
        )
        _log(resultado)
        return resultado
    resultado = _de_linha(gravada, duplicado=False)
    _log(resultado)
    return resultado


def _persistir_falha_por_id(execucao_id: int, motivo: str) -> ResultadoConsumoCanal:
    try:
        existente = _por_execucao(execucao_id)
        if existente is not None:
            resultado = _de_linha(existente, duplicado=True)
            _log(resultado)
            return resultado
        execucao = db.session.get(ExecucaoOperacionalCanal, execucao_id)
        if not _elegivel(execucao):
            resultado = _resultado(CODIGO_FALHA_TECNICA, execucao_id=execucao_id)
            _log(resultado)
            return resultado
        return _persistir_falha(execucao, motivo)
    except Exception as exc:
        db.session.rollback()
        logger.info(
            "canal_consumo execucao_id=%s codigo=%s erro=%s",
            execucao_id,
            CODIGO_FALHA_TECNICA,
            type(exc).__name__,
        )
        resultado = _resultado(CODIGO_FALHA_TECNICA, execucao_id=execucao_id)
        _log(resultado)
        return resultado


def _inserir(row: ConsumoInteracaoCanal) -> ConsumoInteracaoCanal | None:
    try:
        with db.session.begin_nested():
            db.session.add(row)
            db.session.flush()
            consumo_id = int(row.id)
    except IntegrityError:
        return None
    db.session.commit()
    return db.session.get(ConsumoInteracaoCanal, consumo_id)


def _por_execucao(execucao_id: int) -> ConsumoInteracaoCanal | None:
    return ConsumoInteracaoCanal.query.filter_by(
        execucao_operacional_canal_id=int(execucao_id)
    ).one_or_none()


def _de_linha(row: ConsumoInteracaoCanal, *, duplicado: bool) -> ResultadoConsumoCanal:
    codigo = CODIGO_IDEMPOTENTE if duplicado else CODIGO_REGISTRADO
    if row.estado == ConsumoInteracaoCanal.ESTADO_FALHA:
        codigo = CODIGO_FALHA_TECNICA
    return _resultado(
        codigo,
        consumo_id=int(row.id),
        execucao_id=int(row.execucao_operacional_canal_id),
        evento_id=int(row.evento_canal_recebido_id),
        user_id=int(row.user_id),
        conta_id=int(row.conta_id) if row.conta_id is not None else None,
        franquia_id=int(row.franquia_id) if row.franquia_id is not None else None,
        estado=row.estado,
        motivo=row.motivo,
        canal=row.canal,
        unidade=row.unidade,
        quantidade=int(row.quantidade),
        correlation_id=row.correlation_id,
        duplicado=duplicado,
    )


def _resultado(
    codigo: str,
    *,
    consumo_id: int | None = None,
    execucao_id: int | None = None,
    evento_id: int | None = None,
    user_id: int | None = None,
    conta_id: int | None = None,
    franquia_id: int | None = None,
    estado: str | None = None,
    motivo: str | None = None,
    canal: str | None = None,
    unidade: str | None = None,
    quantidade: int | None = None,
    correlation_id: str | None = None,
    duplicado: bool = False,
) -> ResultadoConsumoCanal:
    return ResultadoConsumoCanal(
        codigo=codigo,
        consumo_id=consumo_id,
        execucao_id=execucao_id,
        evento_id=evento_id,
        user_id=user_id,
        conta_id=conta_id,
        franquia_id=franquia_id,
        estado=estado,
        motivo=motivo,
        canal=canal,
        unidade=unidade,
        quantidade=quantidade,
        correlation_id=correlation_id,
        duplicado=duplicado,
    )


def _log(resultado: ResultadoConsumoCanal) -> None:
    logger.info(
        "canal_consumo consumo_id=%s execucao_id=%s evento_id=%s user_id=%s "
        "franquia_id=%s canal=%s unidade=%s quantidade=%s estado=%s motivo=%s "
        "correlation_id=%s codigo=%s",
        resultado.consumo_id if resultado.consumo_id is not None else "-",
        resultado.execucao_id if resultado.execucao_id is not None else "-",
        resultado.evento_id if resultado.evento_id is not None else "-",
        resultado.user_id if resultado.user_id is not None else "-",
        resultado.franquia_id if resultado.franquia_id is not None else "-",
        resultado.canal or ConsumoInteracaoCanal.CANAL_WHATSAPP,
        resultado.unidade or "-",
        resultado.quantidade if resultado.quantidade is not None else "-",
        resultado.estado or "-",
        resultado.motivo or "-",
        resultado.correlation_id or "-",
        resultado.codigo,
    )


def _taxa_positiva(bruto: object) -> Decimal | None:
    if isinstance(bruto, bool) or bruto is None:
        return None
    try:
        taxa = Decimal(str(bruto))
    except Exception:
        return None
    if taxa <= 0:
        return None
    return taxa


def _id_valido(valor: object) -> bool:
    return not isinstance(valor, bool) and isinstance(valor, int) and valor > 0
