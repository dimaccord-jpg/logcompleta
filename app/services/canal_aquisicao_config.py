"""Configuração central provisória da aquisição por canal.

Os números deste módulo são o default do lote de identidade guest.
Não são decisão final de produto e não fazem parte de Growth, Cleiton
ou billing. O domínio lê daqui para não espalhar o limite nem a validade.
"""
from __future__ import annotations

import os
import re
from datetime import timedelta
from urllib.parse import urlsplit

# Provisório: o produto deste lote permite 5 respostas úteis antes do cadastro.
LIMITE_INTERACOES_GUEST_PADRAO = 5

# Provisório: 72 horas. Não é a validade final da jornada.
VALIDADE_ONBOARDING_CANAL_PADRAO = timedelta(hours=72)


def limite_interacoes_guest() -> int:
    """Inteiro >= 1. Configuração inválida falha; não há coerção de float nem bool."""
    valor = LIMITE_INTERACOES_GUEST_PADRAO
    if isinstance(valor, bool) or not isinstance(valor, int) or valor < 1:
        raise ValueError("limite_invalido")
    return valor


# Sem contrato de deep link. Lista vazia recusa qualquer URL externa,
# inclusive ONBOARDING_CANAL_RETORNO_URL.
_HOSTS_RETORNO_CANAL: frozenset[str] = frozenset()


def url_retorno_canal_permitida() -> str | None:
    """URL https de retorno ao canal, só se o host estiver na allowlist.

    O pedido do usuário não entra aqui. Sem host permitido, o retorno fica
    na página interna.
    """
    if not _HOSTS_RETORNO_CANAL:
        return None
    bruto = (os.getenv("ONBOARDING_CANAL_RETORNO_URL") or "").strip()
    if not bruto:
        return None
    parsed = urlsplit(bruto)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    if parsed.hostname.lower() not in _HOSTS_RETORNO_CANAL:
        return None
    return bruto


# Deep link da Central. Dígitos E.164 sem o sinal de mais: país + número.
_NUMERO_PUBLICO_WHATSAPP_RE = re.compile(r"^[1-9]\d{7,14}$")
ENV_WHATSAPP_PUBLIC_NUMBER = "WHATSAPP_PUBLIC_NUMBER"


def numero_publico_whatsapp() -> str | None:
    """Número público do deep link. Ausente ou inválido não vira conexão.

    O valor não é logado. O navegador não escolhe este destino.
    """
    bruto = (os.getenv(ENV_WHATSAPP_PUBLIC_NUMBER) or "").strip()
    if not _NUMERO_PUBLICO_WHATSAPP_RE.fullmatch(bruto):
        return None
    return bruto


def validade_onboarding_canal() -> timedelta:
    """Duração positiva. Zero, negativo ou tipo inválido não produzem jornada."""
    valor = VALIDADE_ONBOARDING_CANAL_PADRAO
    if not isinstance(valor, timedelta) or valor <= timedelta(0):
        raise ValueError("validade_invalida")
    return valor
