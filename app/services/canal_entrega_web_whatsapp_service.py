"""Entrega no WhatsApp do próprio User um texto já produzido no chat web.

A origem é SolicitacaoEntregaCanal. O destino sai só da identidade
vinculada. O HTTP passa pelo adapter oficial, com EventoCanalSaida e
TentativaEnvioCanal. Não cria evento de entrada, execução operacional
nem consumo de franquia.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    EventoCanalSaida,
    IdentidadeCanalExterna,
    SolicitacaoEntregaCanal,
    User,
    utcnow_naive,
)
from app.services.canal_aquisicao_service import PROVEDOR_WHATSAPP_META
from app.services.canal_intencao_entrega_whatsapp_service import (
    DecisaoEntregaWhatsApp,
    decidir_enviar_para_meu_whatsapp,
)
from app.services.canal_saida_whatsapp_service import (
    concluir_envio_reservado,
    inserir_saida_canal,
)
from app.services.cleiton_ai_data_governance import CleitonAiGovernanceBlockedError
from app.services.whatsapp_meta_cloud_api_adapter import _TEXTO_MAXIMO
from app.services.whatsapp_meta_config import configuracao_envio

logger = logging.getLogger(__name__)

CODIGO_ENVIADO = "enviado"
CODIGO_PARCIAL = "parcial"
CODIGO_FALHA_ENVIO = "falha_envio"
CODIGO_SEM_VINCULO = "sem_vinculo"
CODIGO_AMBIGUO = "ambiguo"
CODIGO_SEM_TEXTO = "sem_texto"
CODIGO_USUARIO_INVALIDO = "usuario_invalido"
CODIGO_SUPERFICIE_INVALIDA = "superficie_invalida"
CODIGO_CONFIGURACAO_AUSENTE = "configuracao_ausente"
CODIGO_CHAVE_INVALIDA = "chave_invalida"
CODIGO_CONFLITO = "conflito"
CODIGO_FALHA_CLASSIFICACAO = "falha_classificacao"

MENSAGEM_ENVIADO = "Enviei para o seu WhatsApp."
MENSAGEM_SEM_VINCULO = (
    "Seu WhatsApp ainda não está conectado ao AgenteFrete. Configure-o em Plugins."
)
MENSAGEM_SEM_ANTERIOR = "Não encontrei uma resposta anterior para enviar."
MENSAGEM_FALHA = "Não consegui enviar para o seu WhatsApp agora. Tente novamente em instantes."
MENSAGEM_FALHA_CLASSIFICACAO = (
    "Não consegui processar o pedido de envio para o WhatsApp agora. Tente novamente."
)

_DIGITOS = re.compile(r"^[0-9]{1,32}$")
_CHAVE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{31,79}$")
_PAPEIS_ASSISTENTE = frozenset({"model", "assistant"})

_travas: dict[str, threading.Lock] = {}
_travas_guard = threading.Lock()


@dataclass(frozen=True)
class DestinoWhatsAppVinculado:
    identidade_id: int
    destinatario: str
    phone_number_id: str


@dataclass(frozen=True)
class ResultadoEntregaWeb:
    codigo: str
    solicitacao_id: int | None = None
    correlation_id: str | None = None
    partes: int = 0
    partes_aceitas: int = 0


def _trava(chave: str) -> threading.Lock:
    with _travas_guard:
        lock = _travas.get(chave)
        if lock is None:
            lock = threading.Lock()
            _travas[chave] = lock
        return lock


def _log(resultado: ResultadoEntregaWeb, *, user_id: int | None, superficie: str) -> None:
    logger.info(
        "entrega_web_whatsapp user_id=%s solicitacao_id=%s superficie=%s "
        "correlation_id=%s partes=%s aceitas=%s codigo=%s",
        user_id if user_id is not None else "-",
        resultado.solicitacao_id if resultado.solicitacao_id is not None else "-",
        superficie or "-",
        resultado.correlation_id or "-",
        resultado.partes,
        resultado.partes_aceitas,
        resultado.codigo,
    )


def dividir_texto_whatsapp(texto: str, limite: int = _TEXTO_MAXIMO) -> list[str]:
    """Corta em partes na ordem original, de preferência num espaço ou quebra."""
    if not isinstance(texto, str) or not texto:
        return []
    if limite < 1:
        return []
    if len(texto) <= limite:
        return [texto]
    partes: list[str] = []
    restante = texto
    while restante:
        if len(restante) <= limite:
            partes.append(restante)
            break
        janela = restante[:limite]
        corte = max(janela.rfind(" "), janela.rfind("\n"))
        if corte <= 0:
            corte = limite
        partes.append(restante[:corte])
        restante = restante[corte:]
    return partes


def ultima_resposta_assistente(historico: object) -> str | None:
    """Último texto de assistente já mostrado. Ignora o pedido atual do usuário."""
    if not isinstance(historico, list):
        return None
    for item in reversed(historico):
        if not isinstance(item, dict):
            continue
        papel = str(item.get("role") or "").strip().lower()
        if papel not in _PAPEIS_ASSISTENTE:
            continue
        bruto = item.get("content")
        if not isinstance(bruto, str):
            bruto = item.get("answer")
        if not isinstance(bruto, str):
            continue
        texto = bruto.strip()
        if texto:
            return texto
    return None


def _usuario_valido(usuario: object) -> User | None:
    if usuario is None or not getattr(usuario, "is_authenticated", False):
        return None
    identificador = getattr(usuario, "id", None)
    if isinstance(identificador, bool) or not isinstance(identificador, int) or identificador <= 0:
        return None
    row = db.session.get(User, identificador)
    if row is None:
        return None
    return row


def resolver_whatsapp_do_usuario(usuario: object) -> tuple[str, DestinoWhatsAppVinculado | None]:
    """Localiza o único WhatsApp vinculado. Não escolhe entre vários."""
    user = _usuario_valido(usuario)
    if user is None:
        return CODIGO_USUARIO_INVALIDO, None
    # IdentidadeCanalExterna persiste o provedor como whatsapp_meta.
    # meta_whatsapp é o provider da saída, não desta tabela.
    linhas = (
        IdentidadeCanalExterna.query.filter_by(
            provedor=PROVEDOR_WHATSAPP_META,
            estado=IdentidadeCanalExterna.ESTADO_VINCULADA,
            user_id=int(user.id),
        )
        .filter(
            IdentidadeCanalExterna.revogada_em.is_(None),
            IdentidadeCanalExterna.vinculada_em.isnot(None),
        )
        .all()
    )
    validas: list[IdentidadeCanalExterna] = []
    for linha in linhas:
        sujeito = linha.sujeito_externo
        contexto = linha.contexto_destino
        if not isinstance(sujeito, str) or not _DIGITOS.fullmatch(sujeito):
            continue
        if not isinstance(contexto, str) or not _DIGITOS.fullmatch(contexto):
            continue
        validas.append(linha)
    if not validas:
        return CODIGO_SEM_VINCULO, None
    if len(validas) > 1:
        return CODIGO_AMBIGUO, None
    escolhida = validas[0]
    return CODIGO_ENVIADO, DestinoWhatsAppVinculado(
        identidade_id=int(escolhida.id),
        destinatario=str(escolhida.sujeito_externo),
        phone_number_id=str(escolhida.contexto_destino),
    )


def _hash_conteudo(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def chave_da_requisicao_web(user_id: int, superficie: str, identidade_requisicao: str) -> str:
    """Chave estável: usuário, superfície e a identidade da requisição. Não usa o texto."""
    material = f"{int(user_id)}|{superficie}|{identidade_requisicao.strip()}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _chave_da_requisicao(usuario: object, superficie: str, identidade: str | None) -> str | None:
    if not isinstance(identidade, str) or not identidade.strip():
        return None
    if superficie not in SolicitacaoEntregaCanal.SUPERFICIES:
        return None
    user = _usuario_valido(usuario)
    if user is None:
        return None
    return chave_da_requisicao_web(int(user.id), superficie, identidade)


def _chave_valida(chave: str | None) -> str | None:
    if chave is None:
        return uuid4().hex
    if isinstance(chave, str) and _CHAVE.fullmatch(chave):
        return chave
    return None


def _buscar_solicitacao(chave: str) -> SolicitacaoEntregaCanal | None:
    return SolicitacaoEntregaCanal.query.filter_by(chave_idempotencia=chave).one_or_none()


def _saidas_da_solicitacao(solicitacao_id: int) -> list[EventoCanalSaida]:
    return (
        EventoCanalSaida.query.filter_by(solicitacao_entrega_id=int(solicitacao_id))
        .order_by(EventoCanalSaida.id.asc())
        .all()
    )


def _resultado_das_saidas(
    solicitacao: SolicitacaoEntregaCanal,
    esperadas: int,
) -> ResultadoEntregaWeb:
    saidas = _saidas_da_solicitacao(int(solicitacao.id))
    aceitas = sum(1 for row in saidas if row.status_envio == EventoCanalSaida.STATUS_ACEITO)
    if esperadas > 0 and len(saidas) == esperadas and aceitas == esperadas:
        codigo = CODIGO_ENVIADO
    elif aceitas and aceitas < max(len(saidas), esperadas):
        codigo = CODIGO_PARCIAL
    elif saidas and aceitas == 0:
        codigo = CODIGO_FALHA_ENVIO
    else:
        codigo = CODIGO_FALHA_ENVIO
    return ResultadoEntregaWeb(
        codigo=codigo,
        solicitacao_id=int(solicitacao.id),
        correlation_id=solicitacao.correlation_id,
        partes=len(saidas) or esperadas,
        partes_aceitas=aceitas,
    )


def _criar_solicitacao(
    *,
    user_id: int,
    superficie: str,
    chave: str,
    content_hash: str,
    correlation_id: str,
) -> SolicitacaoEntregaCanal | None:
    row = SolicitacaoEntregaCanal(
        user_id=int(user_id),
        provider=SolicitacaoEntregaCanal.PROVIDER_META_WHATSAPP,
        superficie_origem=superficie,
        correlation_id=correlation_id,
        chave_idempotencia=chave,
        content_hash=content_hash,
        criada_em=utcnow_naive(),
    )
    try:
        with db.session.begin_nested():
            db.session.add(row)
            db.session.flush()
            solicitacao_id = int(row.id)
    except IntegrityError:
        return None
    db.session.commit()
    return db.session.get(SolicitacaoEntregaCanal, solicitacao_id)


def _reservar_parte(
    solicitacao: SolicitacaoEntregaCanal,
    parte: int,
) -> EventoCanalSaida | None:
    return inserir_saida_canal(
        EventoCanalSaida(
            evento_entrada_id=None,
            solicitacao_entrega_id=int(solicitacao.id),
            interpretacao_id=None,
            execucao_operacional_id=None,
            conclusao_id=None,
            provider=EventoCanalSaida.PROVIDER_META_WHATSAPP,
            chave_idempotencia=EventoCanalSaida.chave_resposta_web(int(solicitacao.id), parte),
            status_envio=EventoCanalSaida.STATUS_RESERVADO,
            provider_message_id=None,
            codigo_erro=None,
            criado_em=utcnow_naive(),
            enviado_em=None,
            correlation_id=solicitacao.correlation_id,
        )
    )


def _enviar_partes(
    solicitacao: SolicitacaoEntregaCanal,
    partes: list[str],
    destino: DestinoWhatsAppVinculado,
) -> ResultadoEntregaWeb:
    for indice, texto in enumerate(partes, start=1):
        chave = EventoCanalSaida.chave_resposta_web(int(solicitacao.id), indice)
        existente = EventoCanalSaida.query.filter_by(chave_idempotencia=chave).one_or_none()
        if existente is not None:
            continue
        reservada = _reservar_parte(solicitacao, indice)
        if reservada is None:
            continue
        if reservada.status_envio != EventoCanalSaida.STATUS_RESERVADO:
            continue
        concluir_envio_reservado(
            int(reservada.id),
            phone_number_id=destino.phone_number_id,
            destinatario=destino.destinatario,
            texto=texto,
        )
    return _resultado_das_saidas(solicitacao, len(partes))


def entregar_texto_no_whatsapp_do_usuario(
    *,
    usuario: object,
    superficie: str,
    texto: str,
    chave_idempotencia: str | None = None,
) -> ResultadoEntregaWeb:
    """Envia o texto ao WhatsApp vinculado. Replay da mesma chave não repete o HTTP."""
    user = _usuario_valido(usuario)
    user_id = int(user.id) if user is not None else None
    if user is None:
        resultado = ResultadoEntregaWeb(codigo=CODIGO_USUARIO_INVALIDO)
        _log(resultado, user_id=None, superficie=superficie)
        return resultado
    if superficie not in SolicitacaoEntregaCanal.SUPERFICIES:
        resultado = ResultadoEntregaWeb(codigo=CODIGO_SUPERFICIE_INVALIDA)
        _log(resultado, user_id=user_id, superficie=superficie)
        return resultado
    if not isinstance(texto, str) or not texto.strip():
        resultado = ResultadoEntregaWeb(codigo=CODIGO_SEM_TEXTO)
        _log(resultado, user_id=user_id, superficie=superficie)
        return resultado
    chave = _chave_valida(chave_idempotencia)
    if chave is None:
        resultado = ResultadoEntregaWeb(codigo=CODIGO_CHAVE_INVALIDA)
        _log(resultado, user_id=user_id, superficie=superficie)
        return resultado
    content_hash = _hash_conteudo(texto)
    partes = dividir_texto_whatsapp(texto)
    if not partes or any(len(parte) > _TEXTO_MAXIMO or not parte for parte in partes):
        resultado = ResultadoEntregaWeb(codigo=CODIGO_FALHA_ENVIO)
        _log(resultado, user_id=user_id, superficie=superficie)
        return resultado
    with _trava(chave):
        existente = _buscar_solicitacao(chave)
        if existente is not None:
            if (
                int(existente.user_id) != user_id
                or existente.superficie_origem != superficie
                or existente.content_hash != content_hash
            ):
                resultado = ResultadoEntregaWeb(
                    codigo=CODIGO_CONFLITO,
                    solicitacao_id=int(existente.id),
                    correlation_id=existente.correlation_id,
                )
                _log(resultado, user_id=user_id, superficie=superficie)
                return resultado
            saidas = _saidas_da_solicitacao(int(existente.id))
            if len(saidas) >= len(partes):
                resultado = _resultado_das_saidas(existente, len(partes))
                _log(resultado, user_id=user_id, superficie=superficie)
                return resultado
        codigo_destino, destino = resolver_whatsapp_do_usuario(user)
        if destino is None:
            resultado = ResultadoEntregaWeb(codigo=codigo_destino)
            _log(resultado, user_id=user_id, superficie=superficie)
            return resultado
        if existente is not None:
            if configuracao_envio() is None:
                resultado = ResultadoEntregaWeb(
                    codigo=CODIGO_CONFIGURACAO_AUSENTE,
                    solicitacao_id=int(existente.id),
                    correlation_id=existente.correlation_id,
                    partes=len(partes),
                )
                _log(resultado, user_id=user_id, superficie=superficie)
                return resultado
            resultado = _enviar_partes(existente, partes, destino)
            _log(resultado, user_id=user_id, superficie=superficie)
            return resultado
        if configuracao_envio() is None:
            resultado = ResultadoEntregaWeb(codigo=CODIGO_CONFIGURACAO_AUSENTE)
            _log(resultado, user_id=user_id, superficie=superficie)
            return resultado
        correlation_id = uuid4().hex
        criada = _criar_solicitacao(
            user_id=user_id,
            superficie=superficie,
            chave=chave,
            content_hash=content_hash,
            correlation_id=correlation_id,
        )
        if criada is None:
            repetida = _buscar_solicitacao(chave)
            if repetida is None:
                resultado = ResultadoEntregaWeb(codigo=CODIGO_FALHA_ENVIO)
                _log(resultado, user_id=user_id, superficie=superficie)
                return resultado
            resultado = _resultado_das_saidas(repetida, len(partes))
            _log(resultado, user_id=user_id, superficie=superficie)
            return resultado
        resultado = _enviar_partes(criada, partes, destino)
        _log(resultado, user_id=user_id, superficie=superficie)
        return resultado


def avaliar_pedido_de_entrega(
    *,
    usuario: object,
    superficie: str,
    mensagem: str,
    historico: object,
    agent: str,
    flow_type: str,
    client: object,
    model: str,
    api_key_label: str,
    identidade_requisicao: str | None = None,
) -> ResultadoEntregaWeb | None:
    """None quando a decisão é não enviar. Falha técnica não segue para o chat."""
    if client is None or usuario is None:
        return None
    try:
        pedido = decidir_enviar_para_meu_whatsapp(
            mensagem,
            agent=agent,
            flow_type=flow_type,
            client=client,
            model=model,
            api_key_label=api_key_label,
        )
    except CleitonAiGovernanceBlockedError:
        raise
    except Exception as exc:
        logger.info(
            "entrega_web_intencao user_id=%s superficie=%s codigo=falha_tecnica erro=%s",
            getattr(usuario, "id", "-"),
            superficie,
            type(exc).__name__,
        )
        return ResultadoEntregaWeb(codigo=CODIGO_FALHA_CLASSIFICACAO)
    if pedido is DecisaoEntregaWhatsApp.FALHA_TECNICA:
        logger.info(
            "entrega_web_intencao user_id=%s superficie=%s codigo=falha_tecnica",
            getattr(usuario, "id", "-"),
            superficie,
        )
        return ResultadoEntregaWeb(codigo=CODIGO_FALHA_CLASSIFICACAO)
    if pedido is not DecisaoEntregaWhatsApp.POSITIVO:
        return None
    texto = ultima_resposta_assistente(historico)
    if not texto:
        return ResultadoEntregaWeb(codigo=CODIGO_SEM_TEXTO)
    return entregar_texto_no_whatsapp_do_usuario(
        usuario=usuario,
        superficie=superficie,
        texto=texto,
        chave_idempotencia=_chave_da_requisicao(usuario, superficie, identidade_requisicao),
    )


def mensagem_da_entrega(resultado: ResultadoEntregaWeb, *, sem_anterior: bool = False) -> str:
    if sem_anterior:
        return MENSAGEM_SEM_ANTERIOR
    if resultado.codigo == CODIGO_ENVIADO:
        return MENSAGEM_ENVIADO
    if resultado.codigo == CODIGO_SEM_VINCULO:
        return MENSAGEM_SEM_VINCULO
    if resultado.codigo == CODIGO_SEM_TEXTO:
        return MENSAGEM_SEM_ANTERIOR
    if resultado.codigo == CODIGO_FALHA_CLASSIFICACAO:
        return MENSAGEM_FALHA_CLASSIFICACAO
    return MENSAGEM_FALHA
