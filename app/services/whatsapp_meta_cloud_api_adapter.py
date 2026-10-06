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
Em HTTP 4xx registra só status, códigos seguros, versão, hostname,
tamanho do texto e flags numéricas. O retorno continua http_4xx.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

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

logger = logging.getLogger(__name__)

_DIGITOS = re.compile(r"^[0-9]{1,32}$")
_ID_MENSAGEM = re.compile(r"^[A-Za-z0-9._+=/:-]{1,200}$")
_VERSAO_GRAFO = re.compile(r"^v\d+\.\d+$")
_TIPO_ERRO_META = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_FBTRACE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_ROTULO_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_TEXTO_MAXIMO = 4096
_CODIGO_META_MAXIMO = 2_147_483_647
_CHAVES_ERRO_META = ("error_code", "error_subcode", "error_type", "fbtrace_id")


@dataclass(frozen=True)
class ResultadoAdapterWhatsApp:
    codigo_erro: str | None = None
    provider_message_id: str | None = None

    @property
    def aceito(self) -> bool:
        return self.codigo_erro is None and bool(self.provider_message_id)


def _erro(codigo: str) -> ResultadoAdapterWhatsApp:
    return ResultadoAdapterWhatsApp(codigo_erro=codigo)


def _inteiro_diagnostico(valor: object) -> int | None:
    if isinstance(valor, bool) or not isinstance(valor, int):
        return None
    if valor < 0 or valor > _CODIGO_META_MAXIMO:
        return None
    return valor


def _fbtrace_seguro(valor: object) -> str | None:
    if not isinstance(valor, str) or not _FBTRACE.fullmatch(valor):
        return None
    if not any(caractere.isalpha() for caractere in valor):
        return None
    return valor


def _hostname_seguro(base_url: object) -> str | None:
    if not isinstance(base_url, str) or not base_url:
        return None
    if any(marca in base_url for marca in ("\r", "\n", "\t", " ")):
        return None
    try:
        hostname = urlsplit(base_url).hostname
    except ValueError:
        return None
    if not isinstance(hostname, str) or len(hostname) > 253:
        return None
    rotulos = hostname.split(".")
    if not rotulos or not all(_ROTULO_HOST.fullmatch(rotulo) for rotulo in rotulos):
        return None
    return hostname


def _campos_erro_meta(payload: object) -> dict[str, object] | None:
    """Campos seguros do erro. None quando o JSON não tem a forma esperada."""
    if not isinstance(payload, dict):
        return None
    erro = payload.get("error")
    if not isinstance(erro, dict):
        return None
    campos: dict[str, object] = {}
    codigo = _inteiro_diagnostico(erro.get("code"))
    if codigo is not None:
        campos["error_code"] = codigo
    subcodigo = _inteiro_diagnostico(erro.get("error_subcode"))
    if subcodigo is not None:
        campos["error_subcode"] = subcodigo
    tipo = erro.get("type")
    if isinstance(tipo, str) and _TIPO_ERRO_META.fullmatch(tipo):
        campos["error_type"] = tipo
    bruto_fbtrace = erro.get("fbtrace_id", payload.get("fbtrace_id"))
    fbtrace = _fbtrace_seguro(bruto_fbtrace)
    if fbtrace is not None:
        campos["fbtrace_id"] = fbtrace
    return campos


def _linha_diagnostico_4xx(
    status: int,
    campos_erro: dict[str, object],
    *,
    graph_api_version: object,
    base_url: object,
    text_length: int,
    phone_number_id_numeric: bool,
    recipient_numeric: bool,
) -> str:
    partes = [f"status={status}"]
    for chave in _CHAVES_ERRO_META:
        if chave in campos_erro:
            partes.append(f"{chave}={campos_erro[chave]}")
    if isinstance(graph_api_version, str) and _VERSAO_GRAFO.fullmatch(graph_api_version):
        partes.append(f"graph_version={graph_api_version}")
    hostname = _hostname_seguro(base_url)
    if hostname is not None:
        partes.append(f"graph_host={hostname}")
    partes.append(f"text_length={text_length}")
    partes.append(
        "phone_number_id_numeric=" + ("true" if phone_number_id_numeric else "false")
    )
    partes.append("recipient_numeric=" + ("true" if recipient_numeric else "false"))
    return " ".join(partes)


def _registrar_diagnostico_4xx(
    resposta: object,
    *,
    status: int,
    graph_api_version: object,
    base_url: object,
    text_length: int,
    phone_number_id_numeric: bool,
    recipient_numeric: bool,
) -> None:
    """Observa o 4xx sem alterar o código devolvido ao chamador."""
    try:
        payload = resposta.json()
    except Exception:
        # Leitura inválida não pode mudar o retorno http_4xx nem derrubar o envio.
        campos = None
    else:
        try:
            campos = _campos_erro_meta(payload)
        except Exception:
            campos = None
    try:
        if campos is None:
            logger.info(
                "whatsapp_meta_cloud_api erro_http status=%s diagnostico_meta_indisponivel=true",
                status,
            )
            return
        logger.info(
            "whatsapp_meta_cloud_api erro_http %s",
            _linha_diagnostico_4xx(
                status,
                campos,
                graph_api_version=graph_api_version,
                base_url=base_url,
                text_length=text_length,
                phone_number_id_numeric=phone_number_id_numeric,
                recipient_numeric=recipient_numeric,
            ),
        )
    except Exception:
        return


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
                _registrar_diagnostico_4xx(
                    resposta,
                    status=status,
                    graph_api_version=config.graph_api_version,
                    base_url=config.base_url,
                    text_length=len(texto),
                    phone_number_id_numeric=bool(_DIGITOS.fullmatch(phone_number_id)),
                    recipient_numeric=bool(_DIGITOS.fullmatch(destinatario)),
                )
                return _erro(CODIGO_HTTP_4XX)
            if 500 <= status <= 599:
                return _erro(CODIGO_HTTP_5XX)
            return _erro(CODIGO_RESPOSTA_INVALIDA)
        finally:
            resposta.close()
