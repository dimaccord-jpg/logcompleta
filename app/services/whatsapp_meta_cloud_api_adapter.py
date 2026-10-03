"""Cliente HTTP da Messages API para texto.

Contrato oficial consultado em 2026-07-02, página Text messages da
WhatsApp Cloud API:

POST https://graph.facebook.com/<API_VERSION>/<WHATSAPP_BUSINESS_PHONE_NUMBER_ID>/messages
Authorization: Bearer <ACCESS_TOKEN>
Content-Type: application/json

Corpo mínimo de texto:

{
  "messaging_product": "whatsapp",
  "recipient_type": "individual",
  "to": "<WHATSAPP_USER_PHONE_NUMBER>",
  "type": "text",
  "text": {"body": "<BODY_TEXT>"}
}

O id aceito vem de messages[0].id, no exemplo wamid.<...>.
Este módulo só envia o texto recebido. Não decide jornada e não consulta cobrança.
Não registra o request, o header Authorization nem o corpo da resposta.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import requests

from app.services.whatsapp_meta_config import (
    ConfigEnvioWhatsApp,
    configuracao_envio,
)

CODIGO_TIMEOUT = "timeout"
CODIGO_HTTP_4XX = "http_4xx"
CODIGO_HTTP_5XX = "http_5xx"
CODIGO_RESPOSTA_INVALIDA = "resposta_invalida"
CODIGO_CONFIGURACAO_AUSENTE = "configuracao_ausente"
CODIGO_FALHA_TRANSPORTE = "falha_transporte"

_DIGITOS = re.compile(r"^[0-9]{1,32}$")
_ID_MENSAGEM = re.compile(r"^[A-Za-z0-9._+=/:-]{1,200}$")
_TEXTO_MAXIMO = 4096


@dataclass(frozen=True)
class ResultadoAdapterWhatsApp:
    codigo_erro: str | None = None
    provider_message_id: str | None = None

    @property
    def aceito(self) -> bool:
        return self.codigo_erro is None and bool(self.provider_message_id)


def _erro(codigo: str) -> ResultadoAdapterWhatsApp:
    return ResultadoAdapterWhatsApp(codigo_erro=codigo)


def _id_aceito(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    mensagens = payload.get("messages")
    if not isinstance(mensagens, list) or len(mensagens) != 1:
        return None
    primeira = mensagens[0]
    if not isinstance(primeira, dict):
        return None
    identificador = primeira.get("id")
    if isinstance(identificador, str) and _ID_MENSAGEM.fullmatch(identificador):
        return identificador
    return None


class WhatsAppMetaCloudApiAdapter:
    """Monta o POST oficial, aplica timeout e devolve só id ou código normalizado."""

    def __init__(self, config: ConfigEnvioWhatsApp | None = None):
        self._config = config

    def enviar_texto(self, *, phone_number_id: str, destinatario: str, texto: str) -> ResultadoAdapterWhatsApp:
        config = self._config if self._config is not None else configuracao_envio()
        if config is None:
            return _erro(CODIGO_CONFIGURACAO_AUSENTE)
        if not _DIGITOS.fullmatch(phone_number_id) or not _DIGITOS.fullmatch(destinatario):
            return _erro(CODIGO_RESPOSTA_INVALIDA)
        if not isinstance(texto, str) or not texto or len(texto) > _TEXTO_MAXIMO:
            return _erro(CODIGO_RESPOSTA_INVALIDA)
        url = f"{config.base_url}/{config.graph_api_version}/{phone_number_id}/messages"
        corpo = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": destinatario,
            "type": "text",
            "text": {"body": texto},
        }
        cabecalhos = {
            "Authorization": f"Bearer {config.access_token}",
            "Content-Type": "application/json",
        }
        try:
            resposta = requests.post(
                url,
                json=corpo,
                headers=cabecalhos,
                timeout=config.timeout_seconds,
                allow_redirects=False,
            )
        except requests.Timeout:
            return _erro(CODIGO_TIMEOUT)
        except requests.RequestException:
            return _erro(CODIGO_FALHA_TRANSPORTE)
        try:
            status = int(resposta.status_code)
            if 200 <= status <= 299:
                try:
                    payload = resposta.json()
                except ValueError:
                    return _erro(CODIGO_RESPOSTA_INVALIDA)
                identificador = _id_aceito(payload)
                if identificador is None:
                    return _erro(CODIGO_RESPOSTA_INVALIDA)
                return ResultadoAdapterWhatsApp(provider_message_id=identificador)
            if 400 <= status <= 499:
                return _erro(CODIGO_HTTP_4XX)
            if 500 <= status <= 599:
                return _erro(CODIGO_HTTP_5XX)
            return _erro(CODIGO_RESPOSTA_INVALIDA)
        finally:
            resposta.close()
