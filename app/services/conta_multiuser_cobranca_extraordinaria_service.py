"""
Cobrança extraordinária Multiuser (Fase 6).

Checkout Session mode=payment no Customer existente. Webhook
identificado por flow_type ANTES do processamento recorrente.
Não cria Subscription. Não usa invoice.paid como gate.
"""
from __future__ import annotations

import logging
from datetime import timedelta, timezone
from typing import Any

from app.extensions import db
from app.models import (
    ContaMonetizacaoVinculo,
    ContaMultiuserAumentoExcepcional,
    MonetizacaoFato,
    utcnow_naive,
)
from app.services.conta_multiuser_aumento_excepcional_service import (
    FLOW_TYPE_EXTRAORDINARY,
    STRIPE_CHECKOUT_MAX_TTL,
    SnapshotProporcional,
    liberar_assentos_excepcionais,
    marcar_pagamento_confirmado,
    marcar_reconciliacao,
    site_origin_padrao,
)
from app.services.conta_multiuser_aumento_service import _registrar_fato
from app.services.conta_multiuser_capacidade_service import bloquear_conta_para_capacidade
from app.services.conta_multiuser_errors import (
    AumentoExcepcionalCorrelacaoError,
    AumentoExcepcionalInvalidoError,
)
from app.services.cleiton_monetizacao_service import (
    PROVIDER_STRIPE,
    STATUS_TEC_APLICADO,
    STATUS_TEC_PENDENTE_CORRELACAO,
    STATUS_TEC_SEM_EFEITO,
    _metadata_from_event_object,
    _norm_text,
    _resolver_url_absoluta,
    _stripe_post,
    registrar_fato_monetizacao,
)

logger = logging.getLogger(__name__)

STRIPE_CHECKOUT_MIN_TTL = timedelta(seconds=1800)

EVENTOS_EXTRAORDINARIOS = {
    "checkout.session.completed",
    "checkout.session.async_payment_succeeded",
    "checkout.session.async_payment_failed",
    "checkout.session.expired",
    "payment_intent.succeeded",
    "payment_intent.payment_failed",
}

EVENTOS_CONFIRMACAO = {
    "checkout.session.completed",
    "checkout.session.async_payment_succeeded",
    "payment_intent.succeeded",
}

EVENTOS_FALHA = {
    "checkout.session.async_payment_failed",
    "payment_intent.payment_failed",
    "checkout.session.expired",
}


def _metadata_evento(evento: dict[str, Any], object_data: dict[str, Any]) -> dict[str, Any]:
    meta = dict(_metadata_from_event_object(object_data) or {})
    pi = object_data.get("payment_intent")
    if isinstance(pi, dict):
        pi_meta = pi.get("metadata") if isinstance(pi.get("metadata"), dict) else {}
        for k, v in pi_meta.items():
            if v not in (None, "") and k not in meta:
                meta[k] = v
    return meta


def _flow_type_meta(meta: dict[str, Any]) -> str:
    return str(meta.get("flow_type") or "").strip().lower()


def _lookup_excepcional_por_ids(
    *,
    object_data: dict[str, Any],
    meta: dict[str, Any],
) -> ContaMultiuserAumentoExcepcional | None:
    correlation = str(meta.get("correlation_id") or "").strip()
    if correlation:
        row = ContaMultiuserAumentoExcepcional.query.filter_by(
            correlation_id=correlation
        ).one_or_none()
        if row is not None:
            return row
    request_id = str(meta.get("request_id") or "").strip()
    if request_id:
        row = ContaMultiuserAumentoExcepcional.query.filter_by(
            request_id=request_id
        ).one_or_none()
        if row is not None:
            return row
    session_id = _norm_text(object_data.get("id") if str(object_data.get("object") or "") == "checkout.session" else object_data.get("checkout_session"))
    if not session_id:
        session_id = _norm_text(object_data.get("id")) if _norm_text(object_data.get("id") or "").startswith("cs_") else None
    if session_id:
        row = ContaMultiuserAumentoExcepcional.query.filter_by(
            stripe_checkout_session_id=session_id
        ).one_or_none()
        if row is not None:
            return row
    pi = object_data.get("payment_intent")
    pi_id = _norm_text(pi.get("id") if isinstance(pi, dict) else pi)
    if not pi_id and _norm_text(object_data.get("id") or "").startswith("pi_"):
        pi_id = _norm_text(object_data.get("id"))
    if pi_id:
        row = ContaMultiuserAumentoExcepcional.query.filter_by(
            stripe_payment_intent_id=pi_id
        ).one_or_none()
        if row is not None:
            return row
    invoice_pi = _norm_text(object_data.get("payment_intent")) if not isinstance(object_data.get("payment_intent"), dict) else None
    if invoice_pi:
        row = ContaMultiuserAumentoExcepcional.query.filter_by(
            stripe_payment_intent_id=invoice_pi
        ).one_or_none()
        if row is not None:
            return row
    return None


def evento_eh_cobranca_extraordinaria_multiuser(
    evento: dict[str, Any],
    object_data: dict[str, Any] | None = None,
) -> bool:
    """Identifica o domínio extraordinário ANTES de qualquer handler recorrente."""
    if not isinstance(evento, dict):
        return False
    obj = object_data
    if obj is None:
        data = evento.get("data")
        obj = (data.get("object") if isinstance(data, dict) else {}) or {}
    if not isinstance(obj, dict):
        obj = {}
    meta = _metadata_evento(evento, obj)
    if _flow_type_meta(meta) == FLOW_TYPE_EXTRAORDINARY:
        return True
    return _lookup_excepcional_por_ids(object_data=obj, meta=meta) is not None


def criar_checkout_extraordinario(
    *,
    row: ContaMultiuserAumentoExcepcional,
    snapshot: SnapshotProporcional,
    customer_id: str,
    site_origin: str | None = None,
) -> dict[str, Any]:
    origin = (site_origin or site_origin_padrao()).rstrip("/")
    success_url = _resolver_url_absoluta(origin, "/gestao-multiuser?extraordinario=ok")
    cancel_url = _resolver_url_absoluta(origin, "/gestao-multiuser?extraordinario=cancelado")
    agora = utcnow_naive()
    stripe_min = agora + STRIPE_CHECKOUT_MIN_TTL
    stripe_max = agora + STRIPE_CHECKOUT_MAX_TTL
    if snapshot.expires_at is not None and snapshot.expires_at >= stripe_min:
        stripe_expira = snapshot.expires_at
        if stripe_expira > stripe_max:
            stripe_expira = stripe_max
    else:
        stripe_expira = stripe_min
    expires_unix = int(stripe_expira.replace(tzinfo=timezone.utc).timestamp())
    metadata = {
        "flow_type": FLOW_TYPE_EXTRAORDINARY,
        "request_id": row.request_id,
        "correlation_id": row.correlation_id,
        "conta_id": str(int(row.conta_id)),
        "quantidade_aprovada": str(int(snapshot.quantidade_aprovada)),
        "excepcional_id": str(int(row.id)),
    }
    payload = {
        "mode": "payment",
        "customer": customer_id,
        "success_url": success_url,
        "cancel_url": cancel_url,
        "expires_at": str(expires_unix),
        "client_reference_id": f"mu_extra:{row.correlation_id}",
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": "brl",
        "line_items[0][price_data][unit_amount]": str(int(snapshot.valor_calculado_centavos)),
        "line_items[0][price_data][product_data][name]": (
            "Aumento excepcional de assentos Multiuser"
        ),
        "metadata[flow_type]": metadata["flow_type"],
        "metadata[request_id]": metadata["request_id"],
        "metadata[correlation_id]": metadata["correlation_id"],
        "metadata[conta_id]": metadata["conta_id"],
        "metadata[quantidade_aprovada]": metadata["quantidade_aprovada"],
        "metadata[excepcional_id]": metadata["excepcional_id"],
        "payment_intent_data[metadata][flow_type]": metadata["flow_type"],
        "payment_intent_data[metadata][request_id]": metadata["request_id"],
        "payment_intent_data[metadata][correlation_id]": metadata["correlation_id"],
        "payment_intent_data[metadata][conta_id]": metadata["conta_id"],
        "payment_intent_data[metadata][quantidade_aprovada]": metadata["quantidade_aprovada"],
        "payment_intent_data[metadata][excepcional_id]": metadata["excepcional_id"],
    }
    idempotency_key = f"stripe_mu_extra:{row.correlation_id}"[:200]
    response = _stripe_post(
        "/checkout/sessions",
        payload,
        idempotency_key=idempotency_key,
    )
    session_id = _norm_text(response.get("id"))
    if not session_id:
        raise AumentoExcepcionalInvalidoError("Stripe não devolveu Checkout Session.")
    pi = response.get("payment_intent")
    pi_id = _norm_text(pi.get("id") if isinstance(pi, dict) else pi)
    return {
        "id": session_id,
        "url": _norm_text(response.get("url")),
        "payment_intent": pi_id,
        "payload_enviado": payload,
    }


def _validar_correlacao(
    *,
    row: ContaMultiuserAumentoExcepcional,
    meta: dict[str, Any],
    object_data: dict[str, Any],
) -> None:
    request_id = str(meta.get("request_id") or "").strip()
    correlation = str(meta.get("correlation_id") or "").strip()
    conta_meta = str(meta.get("conta_id") or "").strip()
    customer = _norm_text(object_data.get("customer"))
    if request_id and request_id != row.request_id:
        raise AumentoExcepcionalCorrelacaoError("request_id divergente.")
    if correlation and correlation != row.correlation_id:
        raise AumentoExcepcionalCorrelacaoError("correlation_id divergente.")
    if not request_id and not correlation:
        if not row.stripe_checkout_session_id and not row.stripe_payment_intent_id:
            raise AumentoExcepcionalCorrelacaoError("correlação ausente.")
    if conta_meta and str(int(row.conta_id)) != conta_meta:
        raise AumentoExcepcionalCorrelacaoError("conta_id divergente.")
    if customer and row.stripe_customer_id and customer != row.stripe_customer_id:
        raise AumentoExcepcionalCorrelacaoError("customer divergente.")
    if customer:
        vinculo = (
            ContaMonetizacaoVinculo.query.filter_by(
                conta_id=int(row.conta_id),
                ativo=True,
                customer_id=customer,
            ).first()
        )
        if vinculo is None:
            raise AumentoExcepcionalCorrelacaoError("customer não pertence à Conta.")


def _pagamento_pago(event_type: str, object_data: dict[str, Any]) -> bool:
    if event_type == "checkout.session.completed":
        status = str(object_data.get("payment_status") or "").strip().lower()
        return status == "paid"
    if event_type in {"checkout.session.async_payment_succeeded", "payment_intent.succeeded"}:
        if event_type == "payment_intent.succeeded":
            return str(object_data.get("status") or "").strip().lower() == "succeeded"
        return True
    return False


def processar_evento_extraordinario_multiuser(
    evento: dict[str, Any],
    *,
    reprocessamento_admin: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Dispatcher extraordinário. Não reseta ciclo, não renova, não mexe
    em invoice.paid recorrente.
    """
    _ = reprocessamento_admin
    event_id = _norm_text(evento.get("id"))
    event_type = (_norm_text(evento.get("type")) or "").lower()
    object_data = (
        ((evento.get("data") or {}).get("object") or {})
        if isinstance(evento.get("data"), dict)
        else {}
    )
    if not isinstance(object_data, dict):
        object_data = {}
    meta = _metadata_evento(evento, object_data)
    idempotency_key = None
    if event_id and event_type:
        idempotency_key = f"stripe_event:{event_id}:{event_type}"
    if idempotency_key:
        existente = MonetizacaoFato.query.filter_by(idempotency_key=idempotency_key).first()
        if existente is not None:
            return {
                "ok": True,
                "replay": True,
                "event_id": event_id,
                "event_type": event_type,
                "status_tecnico": existente.status_tecnico,
                "efeito_operacional_aplicado": existente.status_tecnico == STATUS_TEC_APLICADO,
                "dominio": FLOW_TYPE_EXTRAORDINARY,
            }

    row = _lookup_excepcional_por_ids(object_data=object_data, meta=meta)
    if row is None:
        registrar_fato_monetizacao(
            tipo_fato=f"stripe_{event_type.replace('.', '_')}" if event_type else "stripe_evento",
            status_tecnico=STATUS_TEC_PENDENTE_CORRELACAO,
            provider=PROVIDER_STRIPE,
            idempotency_key=idempotency_key,
            external_event_id=event_id,
            snapshot_normalizado={
                "dominio": FLOW_TYPE_EXTRAORDINARY,
                "motivo": "correlacao_inexistente",
                "flow_type": _flow_type_meta(meta),
            },
            payload_bruto_sanitizado=evento,
        )
        db.session.commit()
        logger.info(
            "evento=extraordinario_fail_closed motivo=correlacao_inexistente event_type=%s",
            event_type,
        )
        return {
            "ok": True,
            "replay": False,
            "event_id": event_id,
            "event_type": event_type,
            "status_tecnico": STATUS_TEC_PENDENTE_CORRELACAO,
            "efeito_operacional_aplicado": False,
            "dominio": FLOW_TYPE_EXTRAORDINARY,
            "fail_closed": True,
        }

    try:
        _validar_correlacao(row=row, meta=meta, object_data=object_data)
    except AumentoExcepcionalCorrelacaoError as exc:
        registrar_fato_monetizacao(
            tipo_fato=f"stripe_{event_type.replace('.', '_')}",
            status_tecnico=STATUS_TEC_PENDENTE_CORRELACAO,
            provider=PROVIDER_STRIPE,
            conta_id=int(row.conta_id),
            idempotency_key=idempotency_key,
            correlation_key=row.correlation_id,
            external_event_id=event_id,
            customer_id=_norm_text(object_data.get("customer")),
            snapshot_normalizado={
                "dominio": FLOW_TYPE_EXTRAORDINARY,
                "motivo": str(exc),
                "excepcional_id": row.id,
            },
            payload_bruto_sanitizado=evento,
        )
        db.session.commit()
        logger.info(
            "evento=extraordinario_fail_closed motivo=%s excepcional_id=%s",
            exc,
            row.id,
        )
        return {
            "ok": True,
            "replay": False,
            "event_id": event_id,
            "event_type": event_type,
            "status_tecnico": STATUS_TEC_PENDENTE_CORRELACAO,
            "efeito_operacional_aplicado": False,
            "dominio": FLOW_TYPE_EXTRAORDINARY,
            "fail_closed": True,
        }

    bloquear_conta_para_capacidade(int(row.conta_id))
    row = (
        ContaMultiuserAumentoExcepcional.query.filter_by(id=int(row.id))
        .with_for_update()
        .one()
    )

    if row.estado == ContaMultiuserAumentoExcepcional.ESTADO_REJEITADA:
        registrar_fato_monetizacao(
            tipo_fato="multiuser_excepcional_pagamento_ignorado",
            status_tecnico=STATUS_TEC_SEM_EFEITO,
            provider=PROVIDER_STRIPE,
            conta_id=int(row.conta_id),
            idempotency_key=idempotency_key,
            correlation_key=row.correlation_id,
            external_event_id=event_id,
            snapshot_normalizado={"motivo": "request_rejeitada", "excepcional_id": row.id},
            payload_bruto_sanitizado=evento,
        )
        db.session.commit()
        return {
            "ok": True,
            "replay": False,
            "event_id": event_id,
            "event_type": event_type,
            "status_tecnico": STATUS_TEC_SEM_EFEITO,
            "efeito_operacional_aplicado": False,
            "dominio": FLOW_TYPE_EXTRAORDINARY,
            "fail_closed": True,
        }

    if row.estado == ContaMultiuserAumentoExcepcional.ESTADO_LIBERADA:
        registrar_fato_monetizacao(
            tipo_fato="multiuser_excepcional_replay",
            status_tecnico=STATUS_TEC_SEM_EFEITO,
            provider=PROVIDER_STRIPE,
            conta_id=int(row.conta_id),
            idempotency_key=idempotency_key,
            correlation_key=row.correlation_id,
            external_event_id=event_id,
            snapshot_normalizado={"motivo": "ja_liberada", "excepcional_id": row.id},
            payload_bruto_sanitizado=evento,
        )
        db.session.commit()
        return {
            "ok": True,
            "replay": True,
            "event_id": event_id,
            "event_type": event_type,
            "status_tecnico": STATUS_TEC_SEM_EFEITO,
            "efeito_operacional_aplicado": False,
            "dominio": FLOW_TYPE_EXTRAORDINARY,
        }

    if event_type not in EVENTOS_EXTRAORDINARIOS:
        registrar_fato_monetizacao(
            tipo_fato=f"stripe_{event_type.replace('.', '_')}",
            status_tecnico=STATUS_TEC_SEM_EFEITO,
            provider=PROVIDER_STRIPE,
            conta_id=int(row.conta_id),
            idempotency_key=idempotency_key,
            correlation_key=row.correlation_id,
            external_event_id=event_id,
            snapshot_normalizado={
                "dominio": FLOW_TYPE_EXTRAORDINARY,
                "motivo": "evento_nao_usado_no_fluxo",
                "excepcional_id": row.id,
            },
            payload_bruto_sanitizado=evento,
        )
        db.session.commit()
        return {
            "ok": True,
            "replay": False,
            "event_id": event_id,
            "event_type": event_type,
            "status_tecnico": STATUS_TEC_SEM_EFEITO,
            "efeito_operacional_aplicado": False,
            "dominio": FLOW_TYPE_EXTRAORDINARY,
        }

    if event_type in EVENTOS_FALHA:
        _registrar_fato(
            tipo_fato="multiuser_excepcional_pagamento_falhou",
            status_tecnico="sem_efeito",
            conta_id=int(row.conta_id),
            usuario_id=row.administrador_id,
            correlation_id=row.correlation_id,
            idempotency_key=idempotency_key,
            snapshot={"event_type": event_type, "excepcional_id": row.id},
        )
        if event_type == "checkout.session.expired":
            if row.estado == ContaMultiuserAumentoExcepcional.ESTADO_AGUARDANDO_PAGAMENTO:
                row.estado = ContaMultiuserAumentoExcepcional.ESTADO_EXPIRADA
                row.updated_at = utcnow_naive()
                db.session.add(row)
        db.session.commit()
        return {
            "ok": True,
            "replay": False,
            "event_id": event_id,
            "event_type": event_type,
            "status_tecnico": STATUS_TEC_SEM_EFEITO,
            "efeito_operacional_aplicado": False,
            "dominio": FLOW_TYPE_EXTRAORDINARY,
        }

    if event_type in EVENTOS_CONFIRMACAO and not _pagamento_pago(event_type, object_data):
        registrar_fato_monetizacao(
            tipo_fato="multiuser_excepcional_pagamento_assincrono",
            status_tecnico=STATUS_TEC_SEM_EFEITO,
            provider=PROVIDER_STRIPE,
            conta_id=int(row.conta_id),
            idempotency_key=idempotency_key,
            correlation_key=row.correlation_id,
            external_event_id=event_id,
            snapshot_normalizado={
                "payment_status": object_data.get("payment_status"),
                "excepcional_id": row.id,
            },
            payload_bruto_sanitizado=evento,
        )
        db.session.commit()
        return {
            "ok": True,
            "replay": False,
            "event_id": event_id,
            "event_type": event_type,
            "status_tecnico": STATUS_TEC_SEM_EFEITO,
            "efeito_operacional_aplicado": False,
            "dominio": FLOW_TYPE_EXTRAORDINARY,
            "aguardando_async": True,
        }

    agora = utcnow_naive()
    tardio = bool(row.expires_at and agora > row.expires_at)
    if tardio:
        _registrar_fato(
            tipo_fato="multiuser_excepcional_pagamento_tardio",
            status_tecnico="recebido",
            conta_id=int(row.conta_id),
            usuario_id=row.administrador_id,
            correlation_id=row.correlation_id,
            customer_id=_norm_text(object_data.get("customer")),
            idempotency_key=f"mu_exc_tardio:{event_id or row.correlation_id}",
            snapshot={
                "expires_at": row.expires_at.isoformat() if row.expires_at else None,
                "event_type": event_type,
            },
        )
        marcar_reconciliacao(
            row,
            motivo="pagamento_tardio",
            snapshot={"event_id": event_id, "event_type": event_type},
        )
        if idempotency_key:
            registrar_fato_monetizacao(
                tipo_fato=f"stripe_{event_type.replace('.', '_')}",
                status_tecnico=STATUS_TEC_SEM_EFEITO,
                provider=PROVIDER_STRIPE,
                conta_id=int(row.conta_id),
                idempotency_key=idempotency_key,
                correlation_key=row.correlation_id,
                external_event_id=event_id,
                snapshot_normalizado={"motivo": "pagamento_tardio", "excepcional_id": row.id},
                payload_bruto_sanitizado=evento,
            )
        db.session.commit()
        return {
            "ok": True,
            "replay": False,
            "event_id": event_id,
            "event_type": event_type,
            "status_tecnico": STATUS_TEC_SEM_EFEITO,
            "efeito_operacional_aplicado": False,
            "dominio": FLOW_TYPE_EXTRAORDINARY,
            "reconciliacao_necessaria": True,
        }

    try:
        from app.services.conta_multiuser_aumento_service import _resolver_ciclo_conta

        ciclo = _resolver_ciclo_conta(int(row.conta_id))
        if ciclo.inicio != row.ciclo_inicio or ciclo.fim != row.ciclo_fim:
            _registrar_fato(
                tipo_fato="multiuser_excepcional_pagamento_confirmado",
                status_tecnico="recebido",
                conta_id=int(row.conta_id),
                usuario_id=row.administrador_id,
                correlation_id=row.correlation_id,
                idempotency_key=f"mu_exc_pago_ciclo:{row.correlation_id}",
                snapshot={"event_type": event_type},
            )
            marcar_reconciliacao(
                row,
                motivo="ciclo_incompativel",
                snapshot={
                    "ciclo_snapshot_inicio": row.ciclo_inicio.isoformat(),
                    "ciclo_snapshot_fim": row.ciclo_fim.isoformat(),
                },
            )
            if idempotency_key:
                registrar_fato_monetizacao(
                    tipo_fato=f"stripe_{event_type.replace('.', '_')}",
                    status_tecnico=STATUS_TEC_SEM_EFEITO,
                    provider=PROVIDER_STRIPE,
                    conta_id=int(row.conta_id),
                    idempotency_key=idempotency_key,
                    correlation_key=row.correlation_id,
                    external_event_id=event_id,
                    snapshot_normalizado={"motivo": "ciclo_incompativel"},
                    payload_bruto_sanitizado=evento,
                )
            db.session.commit()
            return {
                "ok": True,
                "replay": False,
                "event_id": event_id,
                "event_type": event_type,
                "status_tecnico": STATUS_TEC_SEM_EFEITO,
                "efeito_operacional_aplicado": False,
                "dominio": FLOW_TYPE_EXTRAORDINARY,
                "reconciliacao_necessaria": True,
            }
    except Exception:
        logger.exception("evento=extraordinario_ciclo_indisponivel excepcional_id=%s", row.id)
        marcar_reconciliacao(row, motivo="ciclo_indeterminado")
        db.session.commit()
        return {
            "ok": True,
            "replay": False,
            "event_id": event_id,
            "event_type": event_type,
            "status_tecnico": STATUS_TEC_SEM_EFEITO,
            "efeito_operacional_aplicado": False,
            "dominio": FLOW_TYPE_EXTRAORDINARY,
            "reconciliacao_necessaria": True,
        }

    pi = object_data.get("payment_intent")
    pi_id = _norm_text(pi.get("id") if isinstance(pi, dict) else pi)
    if not pi_id and _norm_text(object_data.get("id") or "").startswith("pi_"):
        pi_id = _norm_text(object_data.get("id"))
    session_id = _norm_text(object_data.get("id")) if _norm_text(object_data.get("id") or "").startswith("cs_") else None

    _registrar_fato(
        tipo_fato="multiuser_excepcional_pagamento_confirmado",
        status_tecnico="recebido",
        conta_id=int(row.conta_id),
        usuario_id=row.administrador_id,
        correlation_id=row.correlation_id,
        customer_id=_norm_text(object_data.get("customer")),
        idempotency_key=f"mu_exc_pago:{row.correlation_id}",
        snapshot={"event_type": event_type, "event_id": event_id},
    )
    try:
        marcar_pagamento_confirmado(
            row,
            payment_intent_id=pi_id,
            checkout_session_id=session_id,
        )
    except AumentoExcepcionalInvalidoError as exc:
        marcar_reconciliacao(row, motivo="liberacao_bloqueada", snapshot={"erro": str(exc)[:180]})
        if idempotency_key:
            registrar_fato_monetizacao(
                tipo_fato=f"stripe_{event_type.replace('.', '_')}",
                status_tecnico=STATUS_TEC_SEM_EFEITO,
                provider=PROVIDER_STRIPE,
                conta_id=int(row.conta_id),
                idempotency_key=idempotency_key,
                correlation_key=row.correlation_id,
                external_event_id=event_id,
                snapshot_normalizado={"motivo": str(exc)[:180]},
                payload_bruto_sanitizado=evento,
            )
        db.session.commit()
        return {
            "ok": True,
            "replay": False,
            "event_id": event_id,
            "event_type": event_type,
            "status_tecnico": STATUS_TEC_SEM_EFEITO,
            "efeito_operacional_aplicado": False,
            "dominio": FLOW_TYPE_EXTRAORDINARY,
            "reconciliacao_necessaria": True,
        }
    db.session.commit()
    from app.services.conta_multiuser_aumento_excepcional_service import (
        comunicar_estado_excepcional,
    )

    comunicar_estado_excepcional(row, "pagamento_confirmado")
    excepcional_id = int(row.id)

    try:
        bloquear_conta_para_capacidade(int(row.conta_id))
        row = db.session.get(ContaMultiuserAumentoExcepcional, excepcional_id)
        if row is None:
            raise AumentoExcepcionalInvalidoError("Solicitação excepcional não encontrada.")
        liberar_assentos_excepcionais(row, motivo="pagamento", commit=True)
    except Exception:
        logger.exception(
            "evento=extraordinario_liberacao_falhou excepcional_id=%s",
            excepcional_id,
        )
        try:
            db.session.rollback()
        except Exception:
            pass
        raise

    if idempotency_key:
        registrar_fato_monetizacao(
            tipo_fato=f"stripe_{event_type.replace('.', '_')}",
            status_tecnico=STATUS_TEC_APLICADO,
            provider=PROVIDER_STRIPE,
            conta_id=int(row.conta_id),
            idempotency_key=idempotency_key,
            correlation_key=row.correlation_id,
            external_event_id=event_id,
            customer_id=_norm_text(object_data.get("customer")),
            snapshot_normalizado={
                "dominio": FLOW_TYPE_EXTRAORDINARY,
                "excepcional_id": row.id,
                "liberada": True,
            },
            payload_bruto_sanitizado=evento,
        )
    db.session.commit()
    return {
        "ok": True,
        "replay": False,
        "event_id": event_id,
        "event_type": event_type,
        "status_tecnico": STATUS_TEC_APLICADO,
        "efeito_operacional_aplicado": True,
        "dominio": FLOW_TYPE_EXTRAORDINARY,
    }
