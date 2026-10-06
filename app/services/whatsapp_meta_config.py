"""Configuração Meta do canal WhatsApp.

Segredos ficam só no ambiente do processo. Este módulo não grava banco
e não escreve log. O token de envio não é o token da Conversions API.

Entrada: verify token e App Secret do webhook assinado.
Saída textual: access token, versão da Graph API, URL base e timeout.
Sem esses dados de envio, a saída falha fechada e não chama a Graph API.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

ENV_VERIFY_TOKEN = "WHATSAPP_META_WEBHOOK_VERIFY_TOKEN"
ENV_APP_SECRET = "WHATSAPP_META_APP_SECRET"
ENV_ACCESS_TOKEN = "WHATSAPP_META_ACCESS_TOKEN"
ENV_GRAPH_API_VERSION = "WHATSAPP_CLOUD_GRAPH_VERSION"
ENV_GRAPH_BASE_URL = "WHATSAPP_META_GRAPH_BASE_URL"
ENV_SEND_TIMEOUT_SECONDS = "WHATSAPP_META_SEND_TIMEOUT_SECONDS"

# Host documentado da Messages API. A versão não tem default: vem do ambiente.
BASE_GRAPH_OFICIAL = "https://graph.facebook.com"
_VERSAO_RE = re.compile(r"^v\d+\.\d+$")
# Rótulo DNS: não vazio, sem hífen nas pontas, no máximo 63 caracteres.
_ROTULO_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_TIMEOUT_MAXIMO = 30.0
# O POST é {base}/{versão}/{phone_number_id}/messages. A base não leva path.
_PATH_BASE_SUPORTADO = ""


@dataclass(frozen=True)
class ConfigEnvioWhatsApp:
    access_token: str
    graph_api_version: str
    base_url: str
    timeout_seconds: float


def _ler(nome: str) -> str:
    return (os.getenv(nome) or "").strip()


def verify_token() -> str:
    """Token do challenge. Vazio significa configuração ausente."""
    return _ler(ENV_VERIFY_TOKEN)


def app_secret() -> str:
    """App Secret usado como chave HMAC. Vazio significa configuração ausente."""
    return _ler(ENV_APP_SECRET)


def access_token() -> str:
    """Token da Cloud API. Vazio significa configuração ausente. Não persiste."""
    valor = _ler(ENV_ACCESS_TOKEN)
    if not valor or len(valor) > 512:
        return ""
    if any(ord(caractere) < 33 or ord(caractere) > 126 for caractere in valor):
        return ""
    return valor


def graph_api_version() -> str:
    """Versão no formato v<inteiro>.<inteiro>. Sem default no código."""
    valor = _ler(ENV_GRAPH_API_VERSION)
    if _VERSAO_RE.fullmatch(valor):
        return valor
    return ""


def _hostname_valido(hostname: str) -> bool:
    if len(hostname) > 253:
        return False
    rotulos = hostname.split(".")
    return bool(rotulos) and all(_ROTULO_HOST.fullmatch(rotulo) for rotulo in rotulos)


def _base_url_valida(valor: str) -> bool:
    """HTTPS com host válido. Sem userinfo, query, fragment ou path de base."""
    if any(marca in valor for marca in ("@", "?", "#", "\\", " ", "\t", "\r", "\n")):
        return False
    try:
        partes = urlsplit(valor)
        porta = partes.port
    except ValueError:
        return False
    if partes.scheme != "https":
        return False
    if partes.username is not None or partes.password is not None:
        return False
    if partes.query or partes.fragment:
        return False
    if partes.path != _PATH_BASE_SUPORTADO:
        return False
    hostname = partes.hostname
    if not isinstance(hostname, str) or not _hostname_valido(hostname):
        return False
    if porta is not None and not 1 <= porta <= 65535:
        return False
    return True


def graph_base_url() -> str:
    """URL base HTTPS. Vazia no ambiente usa o host oficial da Graph API.

    Porta, quando presente, fica entre 1 e 65535. Userinfo, query, fragment
    e path invalidam a configuração: o cliente só acrescenta versão e phone id.
    """
    valor = _ler(ENV_GRAPH_BASE_URL)
    if not valor:
        return BASE_GRAPH_OFICIAL
    if valor.endswith("/") and not valor.endswith("//"):
        valor = valor[:-1]
    if _base_url_valida(valor):
        return valor
    return ""


def send_timeout_seconds() -> float | None:
    """Timeout HTTP positivo, no máximo 30 segundos. Ausente ou inválido falha fechado."""
    bruto = _ler(ENV_SEND_TIMEOUT_SECONDS)
    if not bruto:
        return None
    try:
        valor = float(bruto)
    except ValueError:
        return None
    if valor != valor or valor <= 0 or valor > _TIMEOUT_MAXIMO:
        return None
    return valor


def configuracao_envio() -> ConfigEnvioWhatsApp | None:
    """Configuração completa de envio, ou None se qualquer peça obrigatória faltar."""
    token = access_token()
    versao = graph_api_version()
    base = graph_base_url()
    timeout = send_timeout_seconds()
    if not token or not versao or not base or timeout is None:
        return None
    return ConfigEnvioWhatsApp(
        access_token=token,
        graph_api_version=versao,
        base_url=base,
        timeout_seconds=timeout,
    )
