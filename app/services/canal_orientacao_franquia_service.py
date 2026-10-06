"""Gate comercial do WhatsApp vinculado, antes da inteligência operacional.

A decisão sai só de avaliar_autorizacao_operacao_por_franquia. Este
módulo não calcula saldo, não lê consumo do canal e não chama a Meta.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from app.extensions import db
from app.models import EventoCanalRecebido, EventoCanalSaida
from app.services.canal_saida_whatsapp_service import enviar_orientacao_franquia
from app.services.cleiton_mensageria_operacao_service import UPGRADE_PATH_DEFAULT
from app.services.cleiton_operacao_autorizacao_service import (
    avaliar_autorizacao_operacao_por_franquia,
)

logger = logging.getLogger(__name__)

CODIGO_FRANQUIA_INDISPONIVEL = "franquia_indisponivel"


@dataclass(frozen=True)
class InterrupcaoFranquiaCanal:
    codigo: str
    evento_id: int
    saida_id: int | None = None
    status_envio: str | None = None
    correlation_id: str | None = None


def _base_publica() -> str:
    bruto = (os.getenv("PUBLIC_BASE_URL") or "").strip()
    if not bruto:
        from app.settings import settings

        bruto = (getattr(settings, "public_base_url", "") or "").strip()
    return bruto.rstrip("/")


def _caminho_cta(decisao: dict) -> str:
    for chave in ("upgrade_cta", "regularizacao_cta"):
        bloco = decisao.get(chave)
        if not isinstance(bloco, dict):
            continue
        url = bloco.get("upgrade_url")
        if isinstance(url, str) and url.strip():
            return url.strip()
    return UPGRADE_PATH_DEFAULT


def url_publica_contratacao(decisao: dict) -> str:
    """Junta a rota já existente do CTA à base pública configurada."""
    caminho = _caminho_cta(decisao)
    if caminho.startswith("https://") or caminho.startswith("http://"):
        return caminho
    if not caminho.startswith("/"):
        caminho = f"/{caminho}"
    base = _base_publica()
    if not base:
        return caminho
    return f"{base}{caminho}"


def montar_texto_orientacao_whatsapp(decisao: dict) -> str:
    """Reusa a mensagem do Gerenciador e aponta a contratação no canal."""
    url = url_publica_contratacao(decisao)
    aviso = decisao.get("mensagem_usuario")
    if not isinstance(aviso, str) or not aviso.strip():
        aviso = "O limite disponível foi atingido."
    else:
        aviso = aviso.strip()
    return (
        f"{aviso} Para continuar usando o AgenteFrete, "
        f"contrate ou regularize um plano: {url}"
    )


def orientacao_existente(evento_id: int) -> InterrupcaoFranquiaCanal | None:
    chave = EventoCanalSaida.chave_orientacao_franquia(int(evento_id))
    row = EventoCanalSaida.query.filter_by(chave_idempotencia=chave).one_or_none()
    if row is None:
        return None
    return InterrupcaoFranquiaCanal(
        codigo=CODIGO_FRANQUIA_INDISPONIVEL,
        evento_id=int(evento_id),
        saida_id=int(row.id),
        status_envio=row.status_envio,
        correlation_id=row.correlation_id,
    )


def interromper_se_franquia_impede(
    evento: EventoCanalRecebido,
    user: object,
) -> InterrupcaoFranquiaCanal | None:
    """None quando o estado vigente permite operar. Não chama a Júlia."""
    evento_id = int(evento.id)
    existente = orientacao_existente(evento_id)
    if existente is not None:
        return existente
    try:
        decisao = avaliar_autorizacao_operacao_por_franquia(user)
    except Exception:
        logger.info(
            "canal_franquia evento_id=%s codigo=consulta_indisponivel",
            evento_id,
        )
        raise
    if not isinstance(decisao, dict) or decisao.get("permitido") is not True:
        if not isinstance(decisao, dict):
            decisao = {}
        logger.info(
            "canal_franquia evento_id=%s codigo=%s status=%s motivo=%s",
            evento_id,
            CODIGO_FRANQUIA_INDISPONIVEL,
            decisao.get("status_franquia") or "-",
            decisao.get("motivo") or "-",
        )
        # A decisão já está em memória. A leitura da franquia pode ter
        # abortado a transação (Postgres) sem impedir o retorno. Sem este
        # rollback, a saída não grava nem chama a Meta e o WhatsApp fica
        # em silêncio. Não desfaz o roteamento: ele já foi confirmado.
        texto = montar_texto_orientacao_whatsapp(decisao)
        db.session.rollback()
        envio = enviar_orientacao_franquia(evento_id, texto)
        return InterrupcaoFranquiaCanal(
            codigo=CODIGO_FRANQUIA_INDISPONIVEL,
            evento_id=evento_id,
            saida_id=int(envio.saida_id) if isinstance(envio.saida_id, int) else None,
            status_envio=envio.status_envio,
            correlation_id=envio.correlation_id,
        )
    return None
