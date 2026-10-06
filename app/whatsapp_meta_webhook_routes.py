"""Webhook de entrada da Meta. GET só faz o challenge; POST só recebe evento.

O POST devolve 200 depois da orquestração. Não há fila neste processo:
a latência inclui o pipeline quando o evento é novo. Replay do mesmo
evento só volta ao orquestrador se ainda houver etapa segura seguinte.
Falha depois da persistência mantém o 200.
"""
from __future__ import annotations

import logging

from flask import Response, jsonify, request

from app.extensions import db
from app.services.canal_orquestracao_whatsapp_service import (
    continuar_replays_pendentes,
    orquestrar_eventos_persistidos,
)
from app.services.whatsapp_meta_webhook_service import (
    CODIGO_ASSINATURA_AUSENTE,
    CODIGO_ASSINATURA_INVALIDA,
    CODIGO_CONFIGURACAO_AUSENTE,
    CODIGO_ESTRUTURA_INESPERADA,
    CODIGO_EVENTO_SEM_ID,
    CODIGO_FALHA_INTERNA,
    CODIGO_JSON_INVALIDO,
    CODIGO_PARAMETROS_AUSENTES,
    CODIGO_VERIFY_TOKEN_INVALIDO,
    HEADER_ASSINATURA,
    WebhookMetaErro,
    receber_eventos,
    validar_challenge,
)

logger = logging.getLogger(__name__)

CAMINHO = "/webhooks/whatsapp/meta"

_HTTP = {
    CODIGO_PARAMETROS_AUSENTES: 400,
    CODIGO_VERIFY_TOKEN_INVALIDO: 403,
    CODIGO_CONFIGURACAO_AUSENTE: 403,
    CODIGO_ASSINATURA_AUSENTE: 403,
    CODIGO_ASSINATURA_INVALIDA: 403,
    CODIGO_JSON_INVALIDO: 400,
    CODIGO_ESTRUTURA_INESPERADA: 400,
    CODIGO_EVENTO_SEM_ID: 400,
    CODIGO_FALHA_INTERNA: 500,
}


def _resposta_erro(exc: WebhookMetaErro):
    logger.info("webhook_meta codigo=%s", exc.codigo)
    return jsonify({"ok": False, "codigo": exc.codigo}), _HTTP.get(exc.codigo, 400)


def whatsapp_meta_webhook_verificacao():
    try:
        challenge = validar_challenge(
            request.args.get("hub.mode"),
            request.args.get("hub.verify_token"),
            request.args.get("hub.challenge"),
        )
    except WebhookMetaErro as exc:
        return _resposta_erro(exc)
    return Response(challenge, status=200, mimetype="text/plain")


def whatsapp_meta_webhook_recebimento():
    corpo = request.get_data(cache=True, as_text=False) or b""
    assinatura = request.headers.get(HEADER_ASSINATURA)
    try:
        resultado = receber_eventos(corpo, assinatura)
    except WebhookMetaErro as exc:
        db.session.rollback()
        return _resposta_erro(exc)
    except Exception:
        db.session.rollback()
        logger.exception("webhook_meta falha_interna")
        return jsonify({"ok": False, "codigo": CODIGO_FALHA_INTERNA}), 500
    if resultado.recebidos:
        try:
            orquestrar_eventos_persistidos(resultado.correlation_id)
        except Exception:
            db.session.rollback()
            logger.error(
                "webhook_meta orquestracao correlation_id=%s codigo=erro_tecnico",
                resultado.correlation_id,
            )
    if resultado.eventos_replay:
        try:
            continuar_replays_pendentes(resultado.eventos_replay)
        except Exception:
            db.session.rollback()
            logger.error(
                "webhook_meta orquestracao_replay codigo=erro_tecnico",
            )
    return jsonify(
        {
            "ok": True,
            "recebidos": resultado.recebidos,
            "replays": resultado.replays,
        }
    ), 200


def register_whatsapp_meta_webhook_routes(app) -> None:
    app.add_url_rule(
        CAMINHO,
        endpoint="whatsapp_meta_webhook_verificacao",
        view_func=whatsapp_meta_webhook_verificacao,
        methods=["GET"],
    )
    app.add_url_rule(
        CAMINHO,
        endpoint="whatsapp_meta_webhook_recebimento",
        view_func=whatsapp_meta_webhook_recebimento,
        methods=["POST"],
    )
