"""Recepção do webhook WhatsApp Cloud API: autenticidade, classe e idempotência.

Contrato oficial de assinatura (Meta, webhook de entrada):

- header ``X-Hub-Signature-256``
- valor ``sha256=`` seguido do HMAC-SHA256 hexadecimal do corpo bruto
- chave: App Secret do aplicativo

O JSON só é interpretado depois dessa validação. Não há envio, onboarding
nem resolução de identidade. Mensagem textual persiste só o corpo já
normalizado, na mesma unidade do evento. Mídia, status e desconhecido
não gravam conteúdo textual.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import EventoCanalRecebido, utcnow_naive
from app.services.canal_entrada_processamento_service import (
    RecusaConteudo,
    registrar_conteudo_textual_canal,
)
from app.services.whatsapp_meta_config import app_secret, verify_token

logger = logging.getLogger(__name__)

CODIGO_ASSINATURA_AUSENTE = "assinatura_ausente"
CODIGO_ASSINATURA_INVALIDA = "assinatura_invalida"
CODIGO_JSON_INVALIDO = "json_invalido"
CODIGO_ESTRUTURA_INESPERADA = "estrutura_inesperada"
CODIGO_EVENTO_SEM_ID = "evento_sem_id_externo"
CODIGO_CONFIGURACAO_AUSENTE = "configuracao_ausente"
CODIGO_PARAMETROS_AUSENTES = "parametros_ausentes"
CODIGO_VERIFY_TOKEN_INVALIDO = "verify_token_invalido"
CODIGO_FALHA_INTERNA = "falha_interna"

HEADER_ASSINATURA = "X-Hub-Signature-256"
PREFIXO_ASSINATURA = "sha256="
OBJETO_WHATSAPP = "whatsapp_business_account"
CAMPO_MENSAGENS = "messages"

_TIPOS_MIDIA = frozenset({"audio", "document", "image", "sticker", "video"})
_STATUS_ENTREGA = frozenset({"delivered", "failed", "played", "read", "sent"})
_STATUS_TOKEN = re.compile(r"^[a-z_]{1,32}$")
_DIGESTO = re.compile(r"^[0-9a-f]{64}$")
_CHALLENGE = re.compile(r"^[A-Za-z0-9]{1,128}$")
_ID_EXTERNO = re.compile(r"^[A-Za-z0-9._+=/:-]{1,200}$")
_OPACO = re.compile(r"^[0-9]{1,32}$")
_TIMESTAMP = re.compile(r"^[0-9]{1,20}$")


class WebhookMetaErro(Exception):
    """Falha normalizada. O texto da exceção é só o código, nunca o corpo."""

    def __init__(self, codigo: str):
        self.codigo = codigo
        super().__init__(codigo)


@dataclass(frozen=True)
class EventoNormalizado:
    provider: str
    evento_externo_id: str
    tipo_evento: str
    sujeito_externo: str | None
    contexto_destino: str | None
    diagnostico_seguro: str
    texto: str | None = None


@dataclass(frozen=True)
class ResultadoRecepcao:
    recebidos: int
    replays: int
    correlation_id: str
    eventos_replay: tuple[int, ...] = ()


def _iguais(esquerda: str, direita: str) -> bool:
    a = esquerda.encode("utf-8")
    b = direita.encode("utf-8")
    if len(a) != len(b):
        return False
    return hmac.compare_digest(a, b)


def _opaco(valor: object) -> str | None:
    if not isinstance(valor, str):
        return None
    texto = valor.strip()
    if _OPACO.fullmatch(texto):
        return texto
    return None


def _timestamp(valor: object) -> str:
    """Segundos Unix do payload oficial: string decimal, ou inteiro equivalente.

    Ausência, vazio, nulo e valor inválido não viram string vazia.
    """
    if isinstance(valor, str) and _TIMESTAMP.fullmatch(valor):
        return valor
    if isinstance(valor, int) and not isinstance(valor, bool) and 0 <= valor <= 10**18:
        return str(valor)
    raise WebhookMetaErro(CODIGO_EVENTO_SEM_ID)


def validar_challenge(mode: str | None, token: str | None, challenge: str | None) -> str:
    """Devolve o challenge quando o verify token confere. Não persiste nada."""
    if not mode or not token or not challenge:
        raise WebhookMetaErro(CODIGO_PARAMETROS_AUSENTES)
    if not _CHALLENGE.fullmatch(challenge):
        raise WebhookMetaErro(CODIGO_PARAMETROS_AUSENTES)
    esperado = verify_token()
    if not esperado:
        raise WebhookMetaErro(CODIGO_CONFIGURACAO_AUSENTE)
    token_ok = _iguais(token, esperado)
    if mode != "subscribe" or not token_ok:
        raise WebhookMetaErro(CODIGO_VERIFY_TOKEN_INVALIDO)
    return challenge


def validar_assinatura(corpo: bytes, cabecalho: str | None) -> None:
    """HMAC-SHA256 do corpo bruto. Não interpreta JSON."""
    segredo = app_secret()
    if not segredo:
        raise WebhookMetaErro(CODIGO_CONFIGURACAO_AUSENTE)
    if cabecalho is None or not str(cabecalho).strip():
        raise WebhookMetaErro(CODIGO_ASSINATURA_AUSENTE)
    header = str(cabecalho).strip()
    if not header.startswith(PREFIXO_ASSINATURA):
        raise WebhookMetaErro(CODIGO_ASSINATURA_INVALIDA)
    recebido = header[len(PREFIXO_ASSINATURA) :].strip().lower()
    if not _DIGESTO.fullmatch(recebido):
        raise WebhookMetaErro(CODIGO_ASSINATURA_INVALIDA)
    esperado = hmac.new(segredo.encode("utf-8"), corpo, hashlib.sha256).hexdigest()
    if not _iguais(esperado, recebido):
        raise WebhookMetaErro(CODIGO_ASSINATURA_INVALIDA)


def _interpretar_json(corpo: bytes) -> dict:
    try:
        payload = json.loads(corpo.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise WebhookMetaErro(CODIGO_JSON_INVALIDO) from None
    if not isinstance(payload, dict):
        raise WebhookMetaErro(CODIGO_ESTRUTURA_INESPERADA)
    return payload


def classificar_mensagem(tipo: object) -> tuple[str, str]:
    """Classe do lote. Não lê corpo, legenda nem arquivo."""
    if tipo == "text":
        return EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL, "mensagem_textual"
    if isinstance(tipo, str) and tipo in _TIPOS_MIDIA:
        return EventoCanalRecebido.TIPO_MIDIA, f"midia:{tipo}"
    return EventoCanalRecebido.TIPO_DESCONHECIDO, "desconhecido"


def _id_obrigatorio(valor: object) -> str:
    if not isinstance(valor, str) or not _ID_EXTERNO.fullmatch(valor):
        raise WebhookMetaErro(CODIGO_EVENTO_SEM_ID)
    return valor


def _contexto(value: dict) -> str | None:
    metadata = value.get("metadata")
    if not isinstance(metadata, dict):
        return None
    return _opaco(metadata.get("phone_number_id"))


def _corpo_textual(mensagem: dict) -> str:
    """Só o body da mensagem textual. Não lê legenda, perfil, mídia nem URL."""
    bloco = mensagem.get("text")
    if not isinstance(bloco, dict):
        raise WebhookMetaErro(CODIGO_ESTRUTURA_INESPERADA)
    corpo = bloco.get("body")
    if not isinstance(corpo, str):
        raise WebhookMetaErro(CODIGO_ESTRUTURA_INESPERADA)
    return corpo


def _de_mensagem(mensagem: object, contexto: str | None) -> EventoNormalizado:
    if not isinstance(mensagem, dict):
        raise WebhookMetaErro(CODIGO_ESTRUTURA_INESPERADA)
    tipo, diagnostico = classificar_mensagem(mensagem.get("type"))
    texto = None
    if tipo == EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL:
        texto = _corpo_textual(mensagem)
    return EventoNormalizado(
        provider=EventoCanalRecebido.PROVIDER_META_WHATSAPP,
        evento_externo_id=_id_obrigatorio(mensagem.get("id")),
        tipo_evento=tipo,
        sujeito_externo=_opaco(mensagem.get("from")),
        contexto_destino=contexto,
        diagnostico_seguro=diagnostico,
        texto=texto,
    )


def _de_status(item: object, contexto: str | None) -> EventoNormalizado:
    if not isinstance(item, dict):
        raise WebhookMetaErro(CODIGO_ESTRUTURA_INESPERADA)
    mensagem_id = _id_obrigatorio(item.get("id"))
    status = item.get("status")
    if not isinstance(status, str):
        raise WebhookMetaErro(CODIGO_EVENTO_SEM_ID)
    status_norm = status.strip().lower()
    if not _STATUS_TOKEN.fullmatch(status_norm):
        raise WebhookMetaErro(CODIGO_EVENTO_SEM_ID)
    marca = _timestamp(item.get("timestamp"))
    externo = f"{mensagem_id}:{status_norm}:{marca}"
    if not _ID_EXTERNO.fullmatch(externo):
        raise WebhookMetaErro(CODIGO_EVENTO_SEM_ID)
    if status_norm in _STATUS_ENTREGA:
        diagnostico = f"status_entrega:{status_norm}"
    else:
        diagnostico = "status_entrega"
    return EventoNormalizado(
        provider=EventoCanalRecebido.PROVIDER_META_WHATSAPP,
        evento_externo_id=externo,
        tipo_evento=EventoCanalRecebido.TIPO_STATUS_ENTREGA,
        sujeito_externo=_opaco(item.get("recipient_id")),
        contexto_destino=contexto,
        diagnostico_seguro=diagnostico,
    )


def extrair_eventos(payload: dict) -> list[EventoNormalizado]:
    """Eventos reconhecíveis do field messages. Outros fields ficam de fora."""
    if payload.get("object") != OBJETO_WHATSAPP:
        raise WebhookMetaErro(CODIGO_ESTRUTURA_INESPERADA)
    entradas = payload.get("entry")
    if not isinstance(entradas, list):
        raise WebhookMetaErro(CODIGO_ESTRUTURA_INESPERADA)
    eventos: list[EventoNormalizado] = []
    for entrada in entradas:
        if not isinstance(entrada, dict):
            raise WebhookMetaErro(CODIGO_ESTRUTURA_INESPERADA)
        changes = entrada.get("changes")
        if not isinstance(changes, list):
            raise WebhookMetaErro(CODIGO_ESTRUTURA_INESPERADA)
        for change in changes:
            if not isinstance(change, dict):
                raise WebhookMetaErro(CODIGO_ESTRUTURA_INESPERADA)
            if change.get("field") != CAMPO_MENSAGENS:
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                raise WebhookMetaErro(CODIGO_ESTRUTURA_INESPERADA)
            contexto = _contexto(value)
            mensagens = value.get("messages")
            statuses = value.get("statuses")
            if mensagens is not None:
                if not isinstance(mensagens, list):
                    raise WebhookMetaErro(CODIGO_ESTRUTURA_INESPERADA)
                eventos.extend(_de_mensagem(item, contexto) for item in mensagens)
            if statuses is not None:
                if not isinstance(statuses, list):
                    raise WebhookMetaErro(CODIGO_ESTRUTURA_INESPERADA)
                eventos.extend(_de_status(item, contexto) for item in statuses)
    return eventos


def _gravar_unidade(evento: EventoNormalizado, *, correlation_id: str, agora) -> None:
    """Evento e, se for textual, o conteúdo mínimo no mesmo SAVEPOINT."""
    row = EventoCanalRecebido(
        provider=evento.provider,
        evento_externo_id=evento.evento_externo_id,
        tipo_evento=evento.tipo_evento,
        sujeito_externo=evento.sujeito_externo,
        contexto_destino=evento.contexto_destino,
        recebido_em=agora,
        status_processamento=EventoCanalRecebido.STATUS_RECEBIDO,
        correlation_id=correlation_id,
        diagnostico_seguro=evento.diagnostico_seguro,
    )
    db.session.add(row)
    db.session.flush()
    if row.tipo_evento != EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL:
        return
    if not isinstance(evento.texto, str):
        raise WebhookMetaErro(CODIGO_ESTRUTURA_INESPERADA)
    registrar_conteudo_textual_canal(int(row.id), evento.texto, commit=False)


def _persistir(eventos: list[EventoNormalizado]) -> ResultadoRecepcao:
    correlation_id = uuid4().hex
    agora = utcnow_naive()
    recebidos = 0
    replays = 0
    eventos_replay: list[int] = []
    try:
        for evento in eventos:
            existente = EventoCanalRecebido.query.filter_by(
                provider=evento.provider,
                evento_externo_id=evento.evento_externo_id,
            ).one_or_none()
            if existente is not None:
                replays += 1
                eventos_replay.append(int(existente.id))
                continue
            try:
                with db.session.begin_nested():
                    _gravar_unidade(
                        evento,
                        correlation_id=correlation_id,
                        agora=agora,
                    )
            except IntegrityError:
                repetido = EventoCanalRecebido.query.filter_by(
                    provider=evento.provider,
                    evento_externo_id=evento.evento_externo_id,
                ).one_or_none()
                if repetido is None:
                    raise
                replays += 1
                eventos_replay.append(int(repetido.id))
                continue
            except RecusaConteudo as exc:
                logger.info("webhook_meta codigo=%s", exc.codigo)
                raise WebhookMetaErro(CODIGO_FALHA_INTERNA) from None
            recebidos += 1
        db.session.commit()
    except WebhookMetaErro:
        db.session.rollback()
        raise
    except Exception:
        db.session.rollback()
        logger.exception("webhook_meta falha_persistencia")
        raise WebhookMetaErro(CODIGO_FALHA_INTERNA) from None
    logger.info(
        "webhook_meta codigo=ok recebidos=%s replays=%s",
        recebidos,
        replays,
    )
    return ResultadoRecepcao(
        recebidos=recebidos,
        replays=replays,
        correlation_id=correlation_id,
        eventos_replay=tuple(eventos_replay),
    )


def receber_eventos(corpo: bytes, cabecalho_assinatura: str | None) -> ResultadoRecepcao:
    """Valida a assinatura, normaliza e persiste. Replay devolve sucesso."""
    if not isinstance(corpo, (bytes, bytearray)):
        raise WebhookMetaErro(CODIGO_JSON_INVALIDO)
    bruto = bytes(corpo)
    validar_assinatura(bruto, cabecalho_assinatura)
    payload = _interpretar_json(bruto)
    eventos = extrair_eventos(payload)
    return _persistir(eventos)
