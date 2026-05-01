"""
Dominio Cleiton - monetizacao Stripe Fase 2.

Principios:
- Stripe fornece fatos externos; nao e fonte operacional final.
- Franquia segue como fonte unica de verdade operacional.
- Efeito operacional em Franquia ocorre apenas por servico central do Cleiton.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import uuid
import calendar
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import ContaMonetizacaoVinculo, Franquia, MonetizacaoFato, User, utcnow_naive
from app.services import plano_service
from app.services.cleiton_ciclo_franquia_service import garantir_ciclo_operacional_franquia
from app.services.cleiton_franquia_operacional_service import (
    aplicar_status_apos_mudanca_estrutural,
)

logger = logging.getLogger(__name__)

PROVIDER_STRIPE = "stripe"
STATUS_TEC_PENDENTE_CORRELACAO = "pendente_correlacao"
STATUS_TEC_SEM_EFEITO = "registrado_sem_efeito_operacional"
STATUS_TEC_APLICADO = "efeito_operacional_aplicado"
STATUS_TEC_IGNORADO = "evento_nao_relevante"

STRIPE_EVENTOS_RELEVANTES = {
    "checkout.session.completed",
    "invoice.paid",
    "invoice.payment_failed",
    "customer.subscription.updated",
    "customer.subscription.deleted",
}

HIERARQUIA_PLANOS = {"free": 0, "starter": 1, "pro": 2}
MUDANCA_PENDENTE_ORIGEM_USUARIO = "solicitacao_usuario"


def _json_dumps(payload: dict[str, Any] | None) -> str:
    return json.dumps(payload or {}, ensure_ascii=True, sort_keys=True, default=str)


def _json_loads(payload: str | None) -> dict[str, Any]:
    if not payload:
        return {}
    try:
        parsed = json.loads(payload)
    except Exception:
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {}


def _json_loads_nullable(payload: str | None) -> dict[str, Any] | None:
    txt = (payload or "").strip()
    if not txt:
        return None
    parsed = _json_loads(txt)
    if parsed:
        return parsed
    return {"_parse_error": "json_invalido_ou_nao_objeto", "_raw": txt}


def _to_datetime_utc_naive(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    if isinstance(value, (int, float)):
        if value <= 0:
            return None
        return datetime.fromtimestamp(float(value), tz=timezone.utc).replace(tzinfo=None)
    if isinstance(value, str):
        txt = value.strip()
        if not txt:
            return None
        try:
            # parse ISO na forma mais comum
            if txt.endswith("Z"):
                txt = txt[:-1] + "+00:00"
            dt = datetime.fromisoformat(txt)
            if dt.tzinfo is None:
                return dt
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        except Exception:
            return None
    return None


def _to_int_or_none(value: Any) -> int | None:
    try:
        if value is None:
            return None
        txt = str(value).strip()
        if not txt:
            return None
        return int(txt)
    except Exception:
        return None


def _norm_text(value: Any) -> str | None:
    txt = (str(value).strip() if value is not None else "")
    return txt or None


def _normalizar_plano_codigo(plano_codigo: str | None) -> str | None:
    plano_n = (plano_codigo or "").strip().lower()
    if plano_n in HIERARQUIA_PLANOS:
        return plano_n
    return None


def _rank_plano(plano_codigo: str | None) -> int:
    plano_n = _normalizar_plano_codigo(plano_codigo)
    if plano_n is None:
        return -1
    return HIERARQUIA_PLANOS[plano_n]


def _resolver_plano_operacional_atual(franquia_id: int) -> str:
    usuarios = (
        User.query.filter(User.franquia_id == int(franquia_id))
        .order_by(User.id.asc())
        .all()
    )
    for user in usuarios:
        categoria = _normalizar_plano_codigo(user.categoria)
        if categoria is not None:
            return categoria
    return "free"


def _obter_vinculo_ativo_por_conta(conta_id: int) -> ContaMonetizacaoVinculo | None:
    return (
        ContaMonetizacaoVinculo.query.filter_by(
            conta_id=int(conta_id),
            provider=PROVIDER_STRIPE,
            ativo=True,
        )
        .order_by(ContaMonetizacaoVinculo.updated_at.desc(), ContaMonetizacaoVinculo.id.desc())
        .first()
    )


def _obter_customer_id_fallback_conta(conta_id: int) -> str | None:
    rows = (
        ContaMonetizacaoVinculo.query.filter_by(conta_id=int(conta_id), provider=PROVIDER_STRIPE)
        .order_by(ContaMonetizacaoVinculo.updated_at.desc(), ContaMonetizacaoVinculo.id.desc())
        .limit(25)
        .all()
    )
    for row in rows:
        cid = _norm_text(row.customer_id)
        if cid:
            return cid
    return None


def _primeiro_subscription_item_id(subscription: dict[str, Any]) -> str | None:
    items = subscription.get("items")
    data = items.get("data") if isinstance(items, dict) else None
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    if isinstance(first, dict):
        return _norm_text(first.get("id"))
    return None


def _primeiro_price_id_de_subscription(subscription: dict[str, Any]) -> str | None:
    items = subscription.get("items")
    data = items.get("data") if isinstance(items, dict) else None
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    if not isinstance(first, dict):
        return None
    price = first.get("price")
    if isinstance(price, dict):
        return _norm_text(price.get("id"))
    return _norm_text(first.get("price"))


def _plano_e_price_de_subscription(subscription: dict[str, Any]) -> tuple[str | None, str | None]:
    meta = subscription.get("metadata") if isinstance(subscription.get("metadata"), dict) else {}
    plano = _normalizar_plano_codigo(meta.get("plano_interno"))
    price_id = _primeiro_price_id_de_subscription(subscription)
    if plano is None and price_id:
        resolved = plano_service.resolver_plano_por_gateway_price_id_admin(
            provider=PROVIDER_STRIPE,
            price_id=price_id,
        )
        if resolved:
            plano = _normalizar_plano_codigo(resolved.get("plano_codigo"))
    return plano, price_id


def _listar_subscriptions_cliente_stripe(customer_id: str) -> list[dict[str, Any]]:
    body = _stripe_get(
        "/subscriptions",
        params=[
            ("customer", customer_id),
            ("limit", "30"),
            ("expand[]", "data.items.data.price"),
        ],
    )
    raw = body.get("data") if isinstance(body.get("data"), list) else []
    out: list[dict[str, Any]] = []
    for s in raw:
        if not isinstance(s, dict):
            continue
        st = (_norm_text(s.get("status")) or "").lower()
        if st in ("active", "trialing", "past_due"):
            out.append(s)
    logger.info(
        "[DOWNGRADE_DEBUG] ASSINATURA_LISTA_STRIPE_RESPONSE customer_id=%s total_bruto=%s total_apos_filtro=%s",
        customer_id,
        len(raw),
        len(out),
    )
    return out


def _escolher_subscription_canonica(conta_id: int, candidatos: list[dict[str, Any]]) -> dict[str, Any]:
    if len(candidatos) == 1:
        return candidatos[0]
    cid_str = str(int(conta_id))
    for s in candidatos:
        md = s.get("metadata") if isinstance(s.get("metadata"), dict) else {}
        if _norm_text(md.get("conta_id")) == cid_str:
            logger.warning(
                "[Stripe] multiplas_assinaturas_ativas usando_match_metadata conta_id=%s subscription_id=%s total=%s",
                conta_id,
                s.get("id"),
                len(candidatos),
            )
            return s
    candidatos_ordenados = sorted(
        candidatos,
        key=lambda s: int(s.get("created") or 0),
        reverse=True,
    )
    chosen = candidatos_ordenados[0]
    logger.warning(
        "[Stripe] multiplas_assinaturas_ativas usando_mais_recente conta_id=%s subscription_id=%s total=%s",
        conta_id,
        chosen.get("id"),
        len(candidatos),
    )
    return chosen


def _sincronizar_vinculo_com_subscription_stripe(
    *,
    conta_id: int,
    franquia_id: int,
    subscription: dict[str, Any],
    payload_bruto: dict[str, Any] | None = None,
) -> ContaMonetizacaoVinculo:
    prev = _obter_vinculo_ativo_por_conta(int(conta_id))
    plano_i, price_i = _plano_e_price_de_subscription(subscription)
    if price_i is None and prev is not None:
        price_i = _norm_text(prev.price_id)
    if plano_i is None and prev is not None:
        plano_i = _normalizar_plano_codigo(prev.plano_interno)
    status_n = (_norm_text(subscription.get("status")) or "").lower() or None
    cust = _norm_text(subscription.get("customer"))
    if cust is None and prev is not None:
        cust = _norm_text(prev.customer_id)
    sub_id = _norm_text(subscription.get("id"))
    if sub_id is None and prev is not None:
        sub_id = _norm_text(prev.subscription_id)
    return atualizar_vinculo_comercial_stripe(
        conta_id=int(conta_id),
        plano_interno=plano_i,
        status_contratual_externo=status_n,
        customer_id=cust,
        subscription_id=sub_id,
        price_id=price_i,
        vigencia_externa_inicio=_to_datetime_utc_naive(subscription.get("current_period_start"))
        or (prev.vigencia_externa_inicio if prev is not None else None),
        vigencia_externa_fim=_to_datetime_utc_naive(subscription.get("current_period_end"))
        or (prev.vigencia_externa_fim if prev is not None else None),
        snapshot_normalizado={"conta_id": int(conta_id), "franquia_id": int(franquia_id)},
        payload_bruto_sanitizado=payload_bruto or subscription,
    )


def _stripe_get_subscription_body_para_obter(
    conta_id_i: int, sid: str, *, contexto_log: str
) -> dict[str, Any] | None:
    """GET /subscriptions/{id} com expand; None em erro HTTP/rede."""
    path = f"/subscriptions/{sid}"
    logger.info(
        "[DOWNGRADE_DEBUG] ASSINATURA_RESOLVE_GET_SUBSCRIPTION conta_id=%s path=%s contexto=%s",
        conta_id_i,
        path,
        contexto_log,
    )
    try:
        return _stripe_get(
            path,
            params=[("expand[]", "items.data.price")],
        )
    except Exception as exc:
        logger.info(
            "[DOWNGRADE_DEBUG] ASSINATURA_RESOLVE_GET_FALHOU conta_id=%s subscription_id=%s contexto=%s exc_type=%s exc=%s",
            conta_id_i,
            sid,
            contexto_log,
            type(exc).__name__,
            exc,
        )
        logger.info(
            "[Stripe] assinatura_recuperada stripe_get falhou conta_id=%s subscription_id=%s",
            conta_id_i,
            sid,
        )
        return None


def _assinatura_payload_de_subscription_stripe(
    full: dict[str, Any],
    *,
    origem: str,
) -> dict[str, Any]:
    return {
        "subscription_id": _norm_text(full.get("id")),
        "customer_id": _norm_text(full.get("customer")),
        "subscription_item_id": _primeiro_subscription_item_id(full),
        "stripe_subscription": full,
        "origem": origem,
    }


def _debug_repr_assinatura_resolvida(assinatura: dict[str, Any] | None) -> str:
    """Resumo JSON-safe para logs (sem payload Stripe completo)."""
    if assinatura is None:
        return "null"
    sub = assinatura.get("stripe_subscription")
    resumo: dict[str, Any] = {
        "tipo_retorno": type(assinatura).__name__,
        "chaves": sorted(assinatura.keys()) if isinstance(assinatura, dict) else None,
        "subscription_id": assinatura.get("subscription_id"),
        "customer_id": assinatura.get("customer_id"),
        "origem": assinatura.get("origem"),
        "subscription_item_id": assinatura.get("subscription_item_id"),
        "stripe_subscription_eh_dict": isinstance(sub, dict),
        "stripe_subscription_id": (sub.get("id") if isinstance(sub, dict) else None),
        "stripe_subscription_status": (sub.get("status") if isinstance(sub, dict) else None),
    }
    return _json_dumps(resumo)


def _subscription_ids_ordenados_vinculos_stripe_conta(conta_id_i: int) -> list[str]:
    """
    Lista ordenada (mais recente primeiro) de subscription_id distintos em vinculos Stripe da conta.
    Inclui vinculo inativo (historico). Em SQLite de teste pode haver apenas um registro por conta.
    """
    rows_hist = (
        ContaMonetizacaoVinculo.query.filter_by(conta_id=conta_id_i, provider=PROVIDER_STRIPE)
        .filter(ContaMonetizacaoVinculo.subscription_id.isnot(None))
        .filter(ContaMonetizacaoVinculo.subscription_id != "")
        .order_by(ContaMonetizacaoVinculo.updated_at.desc(), ContaMonetizacaoVinculo.id.desc())
        .limit(60)
        .all()
    )
    ordered: list[str] = []
    seen: set[str] = set()
    for row in rows_hist:
        hid = _norm_text(row.subscription_id)
        if not hid or hid in seen:
            continue
        seen.add(hid)
        ordered.append(hid)
    return ordered


def _obter_assinatura_stripe_ativa(conta_id: int) -> dict[str, Any] | None:
    """
    Resolve subscription_id confiavel: vinculo ativo + GET; se cancelado/erro, tenta subscription_ids
    em vinculos historicos da conta antes do fallback por customer na Stripe.
    """
    conta_id_i = int(conta_id)
    vinculo = _obter_vinculo_ativo_por_conta(conta_id_i)
    logger.info(
        "[DOWNGRADE_DEBUG] ASSINATURA_RESOLVE_ENTRADA conta_id=%s vinculo_ativo_encontrado=%s vinculo_id=%s",
        conta_id_i,
        vinculo is not None,
        getattr(vinculo, "id", None),
    )
    sid = _norm_text(vinculo.subscription_id) if vinculo is not None else None
    logger.info(
        "[DOWNGRADE_DEBUG] ASSINATURA_RESOLVE_VINCULO_SUBSCRIPTION_ID conta_id=%s subscription_id_presente=%s subscription_id=%s",
        conta_id_i,
        bool(sid),
        sid,
    )
    subscription_ids_tentados: set[str] = set()
    if sid:
        subscription_ids_tentados.add(sid)
        full = _stripe_get_subscription_body_para_obter(
            conta_id_i, sid, contexto_log="vinculo_ativo"
        )
        if full is not None:
            st = (_norm_text(full.get("status")) or "").lower()
            logger.info(
                "[DOWNGRADE_DEBUG] ASSINATURA_RESOLVE_GET_OK conta_id=%s subscription_id=%s status_stripe=%s",
                conta_id_i,
                sid,
                st,
            )
            if st in ("active", "trialing", "past_due"):
                logger.info(
                    "[Stripe] assinatura_recuperada origem=vinculo_local_stripe_get conta_id=%s subscription_id=%s",
                    conta_id_i,
                    sid,
                )
                return _assinatura_payload_de_subscription_stripe(
                    full,
                    origem="vinculo_local_stripe_get",
                )
            logger.info(
                "[DOWNGRADE_DEBUG] ASSINATURA_RESOLVE_GET_STATUS_INACEITAVEL conta_id=%s subscription_id=%s status=%s",
                conta_id_i,
                sid,
                st,
            )

    candidatos_hist_validos: list[dict[str, Any]] = []
    for hid in _subscription_ids_ordenados_vinculos_stripe_conta(conta_id_i):
        if hid in subscription_ids_tentados:
            continue
        subscription_ids_tentados.add(hid)
        logger.info(
            "[DOWNGRADE_DEBUG] ASSINATURA_RESOLVE_HISTORICO_TRY conta_id=%s subscription_id=%s",
            conta_id_i,
            hid,
        )
        body = _stripe_get_subscription_body_para_obter(
            conta_id_i, hid, contexto_log="vinculo_historico"
        )
        if body is None:
            continue
        st = (_norm_text(body.get("status")) or "").lower()
        logger.info(
            "[DOWNGRADE_DEBUG] ASSINATURA_RESOLVE_HISTORICO_GET_OK conta_id=%s subscription_id=%s status=%s",
            conta_id_i,
            hid,
            st,
        )
        if st in ("active", "trialing", "past_due"):
            candidatos_hist_validos.append(body)
    if candidatos_hist_validos:
        cid_str = str(conta_id_i)
        preferidos = [
            b
            for b in candidatos_hist_validos
            if isinstance(b.get("metadata"), dict)
            and _norm_text(b["metadata"].get("conta_id")) == cid_str
        ]
        chosen_hist = preferidos[0] if preferidos else candidatos_hist_validos[0]
        logger.info(
            "[Stripe] assinatura_recuperada origem=vinculo_historico_stripe_get conta_id=%s subscription_id=%s "
            "preferencia_metadata_conta=%s",
            conta_id_i,
            _norm_text(chosen_hist.get("id")),
            bool(preferidos),
        )
        return _assinatura_payload_de_subscription_stripe(
            chosen_hist,
            origem="vinculo_historico_stripe_get",
        )

    cid = _norm_text(vinculo.customer_id) if vinculo is not None else None
    customer_veio_do_vinculo_ativo = bool(cid)
    if not cid:
        cid = _obter_customer_id_fallback_conta(conta_id_i)
    customer_veio_do_historico_conta = bool(cid) and not customer_veio_do_vinculo_ativo
    if not cid:
        logger.info("[Stripe] assinatura_recuperada impossivel sem customer_id conta_id=%s", conta_id_i)
        logger.info(
            "[DOWNGRADE_DEBUG] ASSINATURA_RESOLVE_SEM_CUSTOMER_ID conta_id=%s",
            conta_id_i,
        )
        return None

    logger.info(
        "[DOWNGRADE_DEBUG] ASSINATURA_FALLBACK_CUSTOMER conta_id=%s customer_id=%s "
        "customer_veio_vinculo_ativo=%s customer_veio_historico_conta=%s",
        conta_id_i,
        cid,
        customer_veio_do_vinculo_ativo,
        customer_veio_do_historico_conta,
    )
    candidatos = _listar_subscriptions_cliente_stripe(cid)
    logger.info(
        "[DOWNGRADE_DEBUG] ASSINATURA_LISTA_SUBSCRIPTIONS conta_id=%s customer_id=%s total_apos_filtro=%s ids=%s",
        conta_id_i,
        cid,
        len(candidatos),
        [c.get("id") for c in candidatos if isinstance(c, dict)],
    )
    if not candidatos:
        logger.info(
            "[Stripe] assinatura_recuperada lista_stripe_vazia conta_id=%s customer_id=%s",
            conta_id_i,
            cid,
        )
        return None
    chosen = _escolher_subscription_canonica(conta_id_i, candidatos)
    logger.info(
        "[DOWNGRADE_DEBUG] ASSINATURA_ESCOLHIDA_LISTA conta_id=%s subscription_id_escolhida=%s status=%s",
        conta_id_i,
        chosen.get("id"),
        (_norm_text(chosen.get("status")) or "").lower() if isinstance(chosen, dict) else None,
    )
    logger.info(
        "[Stripe] assinatura_recuperada origem=stripe_list_subscriptions conta_id=%s subscription_id=%s",
        conta_id_i,
        chosen.get("id"),
    )
    return {
        "subscription_id": _norm_text(chosen.get("id")),
        "customer_id": _norm_text(chosen.get("customer")) or cid,
        "subscription_item_id": _primeiro_subscription_item_id(chosen),
        "stripe_subscription": chosen,
        "origem": "stripe_list_subscriptions",
    }


def _guardrail_ids_evento_vs_vinculo_ativo(
    *,
    conta_id: int | None,
    customer_id_evento: str | None,
    subscription_id_evento: str | None,
) -> tuple[bool, dict[str, Any]]:
    """
    Impede promover novo vinculo quando o evento Stripe referencia outra assinatura/cliente
    que nao a assinatura canonica ja registrada no vinculo ativo.
    """
    if conta_id is None:
        return False, {}
    v = _obter_vinculo_ativo_por_conta(int(conta_id))
    if v is None:
        return False, {}
    sub_v = _norm_text(v.subscription_id)
    cus_v = _norm_text(v.customer_id)
    sub_e = _norm_text(subscription_id_evento)
    cus_e = _norm_text(customer_id_evento)
    detalhes: dict[str, Any] = {
        "vinculo_id": v.id,
        "vinculo_subscription_id": sub_v,
        "vinculo_customer_id": cus_v,
        "evento_subscription_id": sub_e,
        "evento_customer_id": cus_e,
    }
    if sub_v and sub_e and sub_e != sub_v:
        detalhes["motivo"] = "subscription_id divergente_do_vinculo_ativo"
        pend = _extrair_pendencia_downgrade_snapshot(_json_loads(v.snapshot_normalizado_json))
        if pend is not None:
            detalhes["mudanca_pendente_ativa"] = True
            detalhes["motivo"] = "subscription_id divergente_do_vinculo_ativo_durante_mudanca_pendente"
        return True, detalhes
    if cus_v and cus_e and cus_e != cus_v:
        detalhes["motivo"] = "customer_id divergente_do_vinculo_ativo"
        pend = _extrair_pendencia_downgrade_snapshot(_json_loads(v.snapshot_normalizado_json))
        if pend is not None:
            detalhes["mudanca_pendente_ativa"] = True
            detalhes["motivo"] = "customer_id divergente_do_vinculo_ativo_durante_mudanca_pendente"
        return True, detalhes
    return False, detalhes


def _registrar_mudanca_pendente_vinculo(
    *,
    conta_id: int,
    plano_futuro: str,
    efetivar_em: datetime,
    origem: str,
) -> dict[str, Any]:
    vinculo = _obter_vinculo_ativo_por_conta(conta_id)
    if vinculo is None:
        raise ValueError("Vinculo comercial ativo nao encontrado para registrar downgrade pendente.")
    snapshot = _json_loads(vinculo.snapshot_normalizado_json)
    snapshot["mudanca_pendente"] = True
    snapshot["tipo_mudanca"] = "downgrade"
    snapshot["plano_futuro"] = plano_futuro
    snapshot["efetivar_em"] = efetivar_em.isoformat()
    snapshot["origem"] = origem
    snapshot["atualizado_em"] = utcnow_naive().isoformat()
    vinculo.snapshot_normalizado_json = _json_dumps(snapshot)
    db.session.add(vinculo)
    return snapshot


def _limpar_mudanca_pendente_vinculo(vinculo: ContaMonetizacaoVinculo) -> None:
    snapshot = _json_loads(vinculo.snapshot_normalizado_json)
    if not snapshot:
        return
    if not snapshot.get("mudanca_pendente"):
        return
    snapshot["mudanca_pendente"] = False
    snapshot["mudanca_efetivada_em"] = utcnow_naive().isoformat()
    snapshot.pop("tipo_mudanca", None)
    snapshot.pop("plano_futuro", None)
    snapshot.pop("efetivar_em", None)
    snapshot.pop("origem", None)
    vinculo.snapshot_normalizado_json = _json_dumps(snapshot)
    db.session.add(vinculo)


def _extrair_pendencia_downgrade_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    snap = dict(snapshot or {})
    if not bool(snap.get("mudanca_pendente")):
        return None
    if (str(snap.get("tipo_mudanca") or "").strip().lower()) != "downgrade":
        return None
    plano_futuro = _normalizar_plano_codigo(snap.get("plano_futuro"))
    efetivar_em = _to_datetime_utc_naive(snap.get("efetivar_em"))
    if plano_futuro is None or efetivar_em is None:
        return None
    return {
        "mudanca_pendente": True,
        "tipo_mudanca": "downgrade",
        "plano_futuro": plano_futuro,
        "efetivar_em": efetivar_em.isoformat(),
        "origem": _norm_text(snap.get("origem")) or MUDANCA_PENDENTE_ORIGEM_USUARIO,
    }


def obter_pendencia_downgrade_conta_ativa(conta_id: int | None) -> dict[str, Any] | None:
    """
    Le o estado real de downgrade pendente no vinculo Stripe ativo (sem inferir pelo checkout).
    Retorno alinhado ao contrato de conciliacao: mudanca_pendente, plano_pendente, efetivar_em,
    mais atualizado_em (ISO) quando presente no snapshot, para UX condicionar retorno recente.
    """
    if conta_id is None:
        return None
    try:
        vinculo = _obter_vinculo_ativo_por_conta(int(conta_id))
    except Exception:
        logger.exception(
            "[StripeDebug][Pendencia] Falha de infraestrutura ao consultar vinculo ativo conta_id=%s",
            conta_id,
        )
        return None
    if vinculo is None:
        return None
    raw_snap = vinculo.snapshot_normalizado_json
    try:
        if (raw_snap or "").strip():
            parsed = json.loads(raw_snap)
            if not isinstance(parsed, dict):
                raise TypeError("snapshot_normalizado_json nao e objeto JSON")
            snap_full = parsed
        else:
            snap_full = {}
    except Exception:
        logger.exception(
            "[StripeDebug][Pendencia] Falha de parse do snapshot_normalizado_json conta_id=%s vinculo_id=%s",
            conta_id,
            getattr(vinculo, "id", None),
        )
        return None
    pend = _extrair_pendencia_downgrade_snapshot(snap_full)
    if pend is None:
        return None
    atualizado = snap_full.get("atualizado_em")
    if atualizado is not None and not isinstance(atualizado, str):
        atualizado = str(atualizado)
    return {
        "mudanca_pendente": True,
        "plano_pendente": pend["plano_futuro"],
        "efetivar_em": pend["efetivar_em"],
        "atualizado_em": atualizado,
    }


def _mesclar_pendencia_no_snapshot(
    *,
    snapshot_novo: dict[str, Any] | None,
    snapshot_anterior: dict[str, Any] | None,
) -> dict[str, Any]:
    merged = dict(snapshot_novo or {})
    pendencia_nova = _extrair_pendencia_downgrade_snapshot(merged)
    if pendencia_nova is not None:
        return merged
    pendencia_anterior = _extrair_pendencia_downgrade_snapshot(snapshot_anterior)
    if pendencia_anterior is None:
        return merged
    merged.update(pendencia_anterior)
    merged["pendencia_preservada_de_vinculo_anterior"] = True
    return merged


def _resolver_data_efetivacao_downgrade(
    *,
    ciclo_fim_evento: datetime | None,
    vigencia_externa_fim: datetime | None,
    fim_ciclo_local: datetime | None,
) -> datetime | None:
    return ciclo_fim_evento or vigencia_externa_fim or fim_ciclo_local


def _add_um_mes_mesmo_dia(dt: datetime) -> datetime:
    y, m, d = dt.year, dt.month, dt.day
    if m == 12:
        y2, m2 = y + 1, 1
    else:
        y2, m2 = y, m + 1
    ult = calendar.monthrange(y2, m2)[1]
    d2 = min(d, ult)
    return dt.replace(year=y2, month=m2, day=d2)


def _obter_api_key_stripe() -> str:
    return (os.getenv("STRIPE_API_KEY") or "").strip()


def _obter_publishable_key_stripe() -> str:
    return (os.getenv("STRIPE_PUBLISHABLE_KEY") or "").strip()


def _obter_webhook_secret_stripe() -> str:
    return (os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()


def _obter_success_url_stripe() -> str:
    return (os.getenv("STRIPE_SUCCESS_URL") or "").strip() or "/contrate-um-plano?checkout=success"


def _obter_cancel_url_stripe() -> str:
    return (os.getenv("STRIPE_CANCEL_URL") or "").strip() or "/contrate-um-plano?checkout=cancelled"


def _obter_base_checkout_url() -> str:
    return (os.getenv("STRIPE_CHECKOUT_API_BASE_URL") or "").strip() or "https://api.stripe.com/v1"


def _resolver_url_absoluta(site_origin: str | None, target: str) -> str:
    target_n = (target or "").strip()
    if not target_n:
        return site_origin or "/"
    if target_n.startswith("http://") or target_n.startswith("https://"):
        return target_n
    origin = (site_origin or "").rstrip("/")
    if not origin:
        return target_n
    if not target_n.startswith("/"):
        target_n = "/" + target_n
    return origin + target_n


def _adicionar_query_param(url: str, key: str, value: str) -> str:
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query[key] = value
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def _stripe_post(path: str, payload: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
    api_key = _obter_api_key_stripe()
    if not api_key:
        raise ValueError("STRIPE_API_KEY ausente no ambiente.")
    base_url = _obter_base_checkout_url().rstrip("/")
    url = f"{base_url}{path}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Idempotency-Key": idempotency_key,
    }
    resp = requests.post(
        url,
        data=payload,
        headers=headers,
        timeout=20,
    )
    body: dict[str, Any] = {}
    try:
        body = resp.json()
    except Exception:
        body = {"raw_text": resp.text}
    if resp.status_code >= 400:
        raise ValueError(f"Stripe retornou erro HTTP {resp.status_code}: {body}")
    return body


def _stripe_get(path: str, *, params: list[tuple[str, str]] | None = None) -> dict[str, Any]:
    api_key = _obter_api_key_stripe()
    if not api_key:
        raise ValueError("STRIPE_API_KEY ausente no ambiente.")
    base_url = _obter_base_checkout_url().rstrip("/")
    url = f"{base_url}{path}"
    headers = {
        "Authorization": f"Bearer {api_key}",
    }
    resp = requests.get(
        url,
        params=params,
        headers=headers,
        timeout=20,
    )
    body: dict[str, Any] = {}
    try:
        body = resp.json()
    except Exception:
        body = {"raw_text": resp.text}
    if resp.status_code >= 400:
        raise ValueError(f"Stripe retornou erro HTTP {resp.status_code}: {body}")
    return body


def _subscription_id_from_invoice_line(line: dict[str, Any]) -> str | None:
    """
    Extrai subscription id de uma linha de invoice.

    Em payloads recentes do Stripe o id pode vir apenas em
    parent.subscription_item_details.subscription (sem subscription no objeto raiz da linha).
    """
    if not isinstance(line, dict):
        return None
    sid = _norm_text(line.get("subscription"))
    if sid:
        return sid
    parent = line.get("parent")
    if isinstance(parent, dict):
        sid_details = parent.get("subscription_item_details")
        if isinstance(sid_details, dict):
            sid = _norm_text(sid_details.get("subscription"))
            if sid:
                return sid
    return None


def _agendar_cancelamento_assinatura_stripe(
    *,
    subscription_id: str,
    idempotency_key: str,
    conta_id: int | None = None,
    franquia_id: int | None = None,
) -> dict[str, Any]:
    logging.info(
        "[Stripe][cancel_at_period_end] solicitado subscription_id=%s conta_id=%s franquia_id=%s idempotency_key=%s",
        subscription_id,
        conta_id,
        franquia_id,
        idempotency_key,
    )
    payload = {
        "cancel_at_period_end": "true",
    }
    path = f"/subscriptions/{subscription_id}"
    try:
        body = _stripe_post(path, payload, idempotency_key=idempotency_key)
    except Exception:
        logging.exception(
            "[Stripe][cancel_at_period_end] falha subscription_id=%s conta_id=%s franquia_id=%s idempotency_key=%s",
            subscription_id,
            conta_id,
            franquia_id,
            idempotency_key,
        )
        raise
    logging.info(
        "[Stripe][cancel_at_period_end] ok subscription_id=%s conta_id=%s franquia_id=%s cancel_at_period_end=%s",
        subscription_id,
        conta_id,
        franquia_id,
        body.get("cancel_at_period_end"),
    )
    return body


def listar_planos_contratacao_publica() -> list[dict[str, Any]]:
    """
    Lista planos de contratacao habilitados para a jornada oficial no site.
    """
    saida: list[dict[str, Any]] = []
    for plano in plano_service.listar_planos_saas_admin():
        codigo = (plano.get("codigo") or "").strip().lower()
        if codigo not in ("free", "starter", "pro"):
            continue
        if codigo == "free":
            saida.append(
                {
                    "codigo": "free",
                    "nome": plano.get("nome") or "Free",
                    "valor_admin": "0.00",
                    "limite_franquia_referencia": (
                        str(plano.get("franquia_referencia"))
                        if plano.get("franquia_referencia") is not None
                        else None
                    ),
                    "habilitado_checkout": True,
                    "pendencias_gateway": [],
                }
            )
            continue
        gateway = plano.get("gateway_config") or {}
        pronto = bool(gateway.get("configuracao_valida"))
        saida.append(
            {
                "codigo": codigo,
                "nome": plano.get("nome") or codigo.capitalize(),
                "valor_admin": (
                    str(plano.get("valor_admin"))
                    if plano.get("valor_admin") is not None
                    else None
                ),
                "limite_franquia_referencia": (
                    str(plano.get("franquia_referencia"))
                    if plano.get("franquia_referencia") is not None
                    else None
                ),
                "habilitado_checkout": pronto,
                "pendencias_gateway": list(gateway.get("pendencias") or []),
            }
        )
    return saida


def iniciar_jornada_assinatura_stripe(
    *,
    user,
    plano_codigo: str,
    site_origin: str | None = None,
) -> dict[str, Any]:
    """
    Inicia checkout embedded Stripe para a tela oficial /contrate-um-plano.
    Nao aplica efeito operacional direto na Franquia.
    """
    if user is None:
        raise ValueError("Usuario obrigatorio para iniciar jornada de contratacao.")
    conta_id = _to_int_or_none(getattr(user, "conta_id", None))
    franquia_id = _to_int_or_none(getattr(user, "franquia_id", None))
    usuario_id = _to_int_or_none(getattr(user, "id", None))
    if conta_id is None or franquia_id is None:
        raise ValueError("Usuario sem correlacao operacional conta/franquia.")
    plano_n = _normalizar_plano_codigo(plano_codigo)
    if plano_n is None:
        raise ValueError("Plano fora do escopo de contratacao Stripe.")

    vinculo_ativo = _obter_vinculo_ativo_por_conta(conta_id)
    pendencia_vinculo_ativo = None
    if vinculo_ativo is not None:
        pendencia_vinculo_ativo = _extrair_pendencia_downgrade_snapshot(
            _json_loads(vinculo_ativo.snapshot_normalizado_json)
        )
    msg_bloqueio_pendencia = (
        "Existe uma alteracao de plano pendente para o fim do ciclo. "
        "Aguarde a efetivacao antes de iniciar nova contratacao."
    )
    if plano_n == "free":
        if pendencia_vinculo_ativo is not None:
            registrar_fato_monetizacao(
                tipo_fato="stripe_checkout_guardrail_mudanca_pendente",
                status_tecnico=STATUS_TEC_SEM_EFEITO,
                provider=PROVIDER_STRIPE,
                conta_id=conta_id,
                franquia_id=franquia_id,
                usuario_id=usuario_id,
                idempotency_key=(
                    f"stripe_checkout_guardrail_mudanca_pendente:{conta_id}:{franquia_id}:"
                    f"free:{pendencia_vinculo_ativo.get('plano_futuro')}:{pendencia_vinculo_ativo.get('efetivar_em')}"
                )[:190],
                snapshot_normalizado={
                    "motivo": "mudanca_pendente_no_vinculo_ativo",
                    "plano_solicitado": "free",
                    "plano_pendente": pendencia_vinculo_ativo.get("plano_futuro"),
                    "efetivar_em": pendencia_vinculo_ativo.get("efetivar_em"),
                },
                payload_bruto_sanitizado={"origem": "iniciar_jornada_assinatura_stripe"},
            )
            db.session.commit()
            raise ValueError(msg_bloqueio_pendencia)
        logger.info("[DOWNGRADE_DEBUG] DOWNGRADE_FREE_START conta_id=%s", conta_id)
        assinatura_free = _obter_assinatura_stripe_ativa(conta_id)
        logger.info(
            "[DOWNGRADE_DEBUG] ASSINATURA_RESOLVIDA conta_id=%s resumo=%s",
            conta_id,
            _debug_repr_assinatura_resolvida(assinatura_free),
        )
        if assinatura_free is None or not _norm_text(assinatura_free.get("subscription_id")):
            logger.error(
                "[DOWNGRADE_DEBUG] SEM_ASSINATURA_APOS_RESOLUCAO conta_id=%s assinatura_eh_none=%s "
                "subscription_id_norm=%s tipo=%s",
                conta_id,
                assinatura_free is None,
                _norm_text(assinatura_free.get("subscription_id")) if isinstance(assinatura_free, dict) else None,
                type(assinatura_free).__name__,
            )
            raise ValueError("Conta sem assinatura Stripe ativa para downgrade para Free.")
        logger.info(
            "[DOWNGRADE_DEBUG] ASSINATURA_ACEITA_SEGUE_FLUXO conta_id=%s subscription_id=%s origem=%s",
            conta_id,
            _norm_text(assinatura_free.get("subscription_id")),
            assinatura_free.get("origem"),
        )
        fr = db.session.get(Franquia, int(franquia_id))
        if fr is None:
            raise ValueError("Franquia operacional nao encontrada.")
        sub_base = assinatura_free.get("stripe_subscription")
        if isinstance(sub_base, dict):
            _sincronizar_vinculo_com_subscription_stripe(
                conta_id=conta_id,
                franquia_id=int(franquia_id),
                subscription=sub_base,
                payload_bruto=sub_base,
            )
        v_pos = _obter_vinculo_ativo_por_conta(conta_id)
        fim_vigencia = (
            (v_pos.vigencia_externa_fim if v_pos is not None else None) or fr.fim_ciclo
        )
        if fim_vigencia is None:
            raise ValueError("Nao foi possivel determinar a data de vencimento atual para downgrade.")
        sub_id_cancel = str(assinatura_free["subscription_id"])
        idempotency_key = (
            f"stripe_subscription_cancel_period_end:{conta_id}:{franquia_id}:{sub_id_cancel}"
        )
        response = _agendar_cancelamento_assinatura_stripe(
            subscription_id=sub_id_cancel,
            idempotency_key=idempotency_key,
            conta_id=conta_id,
            franquia_id=franquia_id,
        )
        if isinstance(response, dict) and response.get("id"):
            _sincronizar_vinculo_com_subscription_stripe(
                conta_id=conta_id,
                franquia_id=int(franquia_id),
                subscription=response,
                payload_bruto=response,
            )
        v_after_cancel = _obter_vinculo_ativo_por_conta(conta_id)
        efetivar_em = _resolver_data_efetivacao_downgrade(
            ciclo_fim_evento=(
                _to_datetime_utc_naive(response.get("cancel_at"))
                or _to_datetime_utc_naive(response.get("current_period_end"))
            ),
            vigencia_externa_fim=(v_after_cancel.vigencia_externa_fim if v_after_cancel else None),
            fim_ciclo_local=fim_vigencia,
        )
        if efetivar_em is None:
            raise ValueError("Nao foi possivel determinar a data de efetivacao do downgrade.")
        _registrar_mudanca_pendente_vinculo(
            conta_id=conta_id,
            plano_futuro="free",
            efetivar_em=efetivar_em,
            origem=MUDANCA_PENDENTE_ORIGEM_USUARIO,
        )
        registrar_fato_monetizacao(
            tipo_fato="stripe_subscription_cancel_at_period_end_requested",
            status_tecnico=STATUS_TEC_APLICADO,
            provider=PROVIDER_STRIPE,
            conta_id=conta_id,
            franquia_id=franquia_id,
            usuario_id=usuario_id,
            idempotency_key=idempotency_key,
            correlation_key=_norm_text(sub_id_cancel),
            external_event_id=_norm_text(sub_id_cancel),
            customer_id=_norm_text(response.get("customer")) or _norm_text(assinatura_free.get("customer_id")),
            subscription_id=_norm_text(sub_id_cancel),
            price_id=_norm_text(v_after_cancel.price_id if v_after_cancel else None),
            snapshot_normalizado={
                "mudanca_pendente": True,
                "tipo_mudanca": "downgrade",
                "plano_futuro": "free",
                "efetivar_em": efetivar_em.isoformat(),
                "origem": MUDANCA_PENDENTE_ORIGEM_USUARIO,
                "fluxo": "cancelamento_fim_periodo",
            },
            payload_bruto_sanitizado=response,
        )
        db.session.commit()
        return {
            "checkout_session_id": None,
            "checkout_client_secret": None,
            "publishable_key": None,
            "plano_codigo": "free",
            "downgrade_agendado": True,
            "efetivar_em": efetivar_em.isoformat(),
        }

    cfg = plano_service.obter_configuracao_gateway_plano_admin(plano_n)
    if not cfg:
        raise ValueError("Plano fora do escopo de contratacao Stripe.")
    if not cfg.get("configuracao_valida"):
        raise ValueError("Plano com configuracao Stripe pendente no admin.")
    price_id = _norm_text(cfg.get("price_id"))
    if not price_id:
        raise ValueError("Plano sem price_id Stripe configurado.")

    publishable_key = _obter_publishable_key_stripe()
    if not publishable_key:
        raise ValueError("STRIPE_PUBLISHABLE_KEY ausente no ambiente.")

    assinatura_existente = _obter_assinatura_stripe_ativa(conta_id)
    if pendencia_vinculo_ativo is not None and (
        assinatura_existente is None or not _norm_text(assinatura_existente.get("subscription_id"))
    ):
        registrar_fato_monetizacao(
            tipo_fato="stripe_checkout_guardrail_mudanca_pendente",
            status_tecnico=STATUS_TEC_SEM_EFEITO,
            provider=PROVIDER_STRIPE,
            conta_id=conta_id,
            franquia_id=franquia_id,
            usuario_id=usuario_id,
            idempotency_key=(
                f"stripe_checkout_guardrail_mudanca_pendente:{conta_id}:{franquia_id}:{plano_n}:"
                f"{pendencia_vinculo_ativo.get('plano_futuro')}:{pendencia_vinculo_ativo.get('efetivar_em')}"
            )[:190],
            snapshot_normalizado={
                "motivo": "mudanca_pendente_no_vinculo_ativo",
                "plano_solicitado": plano_n,
                "plano_pendente": pendencia_vinculo_ativo.get("plano_futuro"),
                "efetivar_em": pendencia_vinculo_ativo.get("efetivar_em"),
            },
            payload_bruto_sanitizado={"origem": "iniciar_jornada_assinatura_stripe"},
        )
        db.session.commit()
        raise ValueError(msg_bloqueio_pendencia)
    if assinatura_existente is not None and _norm_text(assinatura_existente.get("subscription_id")):
        plano_atual_u = _normalizar_plano_codigo(getattr(user, "categoria", None)) or "free"
        destino = plano_n
        if plano_atual_u == destino:
            raise ValueError("Plano solicitado coincide com o plano atual da assinatura.")
        item_sid = _norm_text(assinatura_existente.get("subscription_item_id"))
        if not item_sid:
            raise ValueError("Assinatura Stripe sem item recorrente para atualizacao de plano.")
        logger.info(
            "[Stripe] bloqueio_checkout_assinatura_ativa conta_id=%s subscription_id=%s fluxo=subscription_modify",
            conta_id,
            assinatura_existente.get("subscription_id"),
        )
        idempotency_key_mod = (
            f"stripe_subscription_modify:{conta_id}:{assinatura_existente['subscription_id']}:"
            f"{destino}:{uuid.uuid4().hex}"
        )
        payload_mod: dict[str, Any] = {
            "items[0][id]": item_sid,
            "items[0][price]": price_id,
            "proration_behavior": "none",
            f"metadata[plano_interno]": destino,
            f"metadata[conta_id]": str(conta_id),
            f"metadata[franquia_id]": str(franquia_id),
            f"metadata[usuario_id]": str(usuario_id or ""),
        }
        response_mod = _stripe_post(
            f"/subscriptions/{assinatura_existente['subscription_id']}",
            payload_mod,
            idempotency_key=idempotency_key_mod,
        )
        logger.info(
            "[Stripe] assinatura_plano_atualizada conta_id=%s subscription_id=%s plano_destino=%s",
            conta_id,
            assinatura_existente.get("subscription_id"),
            destino,
        )
        _sincronizar_vinculo_com_subscription_stripe(
            conta_id=conta_id,
            franquia_id=int(franquia_id),
            subscription=response_mod,
            payload_bruto=response_mod,
        )
        eh_downgrade = _rank_plano(destino) < _rank_plano(plano_atual_u)
        out_mod: dict[str, Any] = {
            "checkout_session_id": None,
            "checkout_client_secret": None,
            "publishable_key": None,
            "plano_codigo": destino,
            "assinatura_atualizada_sem_checkout": True,
        }
        if eh_downgrade and destino != "free":
            fr_mod = db.session.get(Franquia, int(franquia_id))
            if fr_mod is None:
                raise ValueError("Franquia operacional nao encontrada.")
            v_mod = _obter_vinculo_ativo_por_conta(conta_id)
            efetivar_em_mod = _resolver_data_efetivacao_downgrade(
                ciclo_fim_evento=_to_datetime_utc_naive(response_mod.get("current_period_end")),
                vigencia_externa_fim=(v_mod.vigencia_externa_fim if v_mod is not None else None),
                fim_ciclo_local=fr_mod.fim_ciclo,
            )
            if efetivar_em_mod is None:
                raise ValueError("Nao foi possivel determinar a data de efetivacao do downgrade.")
            _registrar_mudanca_pendente_vinculo(
                conta_id=conta_id,
                plano_futuro=destino,
                efetivar_em=efetivar_em_mod,
                origem=MUDANCA_PENDENTE_ORIGEM_USUARIO,
            )
            out_mod["downgrade_agendado"] = True
            out_mod["efetivar_em"] = efetivar_em_mod.isoformat()
        registrar_fato_monetizacao(
            tipo_fato="stripe_subscription_plan_modify_requested",
            status_tecnico=STATUS_TEC_APLICADO,
            provider=PROVIDER_STRIPE,
            conta_id=conta_id,
            franquia_id=franquia_id,
            usuario_id=usuario_id,
            idempotency_key=idempotency_key_mod,
            correlation_key=_norm_text(assinatura_existente.get("subscription_id")),
            external_event_id=_norm_text(assinatura_existente.get("subscription_id")),
            customer_id=_norm_text(response_mod.get("customer")),
            subscription_id=_norm_text(response_mod.get("id")),
            price_id=price_id,
            snapshot_normalizado={
                "plano_destino": destino,
                "plano_operacional_usuario": plano_atual_u,
                "proration_behavior": "none",
                "conta_id": conta_id,
                "franquia_id": franquia_id,
            },
            payload_bruto_sanitizado=response_mod,
        )
        db.session.commit()
        return out_mod

    customer_id_existente = (
        _norm_text(vinculo_ativo.customer_id) if vinculo_ativo is not None else None
    )

    success_url = _resolver_url_absoluta(site_origin, _obter_success_url_stripe())
    return_url = success_url

    metadata = {
        "conta_id": str(conta_id),
        "franquia_id": str(franquia_id),
        "usuario_id": str(usuario_id or ""),
        "plano_interno": plano_n,
        "fluxo_origem": "contrate_um_plano",
    }
    payload = {
        "mode": "subscription",
        "ui_mode": "embedded_page",
        "line_items[0][price]": price_id,
        "line_items[0][quantity]": "1",
        "return_url": return_url,
        "client_reference_id": f"conta:{conta_id}:franquia:{franquia_id}",
        "metadata[conta_id]": metadata["conta_id"],
        "metadata[franquia_id]": metadata["franquia_id"],
        "metadata[usuario_id]": metadata["usuario_id"],
        "metadata[plano_interno]": metadata["plano_interno"],
        "metadata[fluxo_origem]": metadata["fluxo_origem"],
        "subscription_data[metadata][conta_id]": metadata["conta_id"],
        "subscription_data[metadata][franquia_id]": metadata["franquia_id"],
        "subscription_data[metadata][usuario_id]": metadata["usuario_id"],
        "subscription_data[metadata][plano_interno]": metadata["plano_interno"],
        "subscription_data[metadata][fluxo_origem]": metadata["fluxo_origem"],
    }
    if customer_id_existente:
        payload["customer"] = customer_id_existente

    logger.info(
        "[Stripe] checkout_nova_assinatura conta_id=%s plano=%s (sem assinatura ativa na Stripe)",
        conta_id,
        plano_n,
    )
    assinatura_revalidacao = _obter_assinatura_stripe_ativa(conta_id)
    if assinatura_revalidacao is not None and _norm_text(assinatura_revalidacao.get("subscription_id")):
        logger.error(
            "[Stripe][CheckoutGuardrail] checkout_pago_bloqueado_assinatura_ativa_revalidacao "
            "conta_id=%s subscription_id=%s plano_destino=%s",
            conta_id,
            assinatura_revalidacao.get("subscription_id"),
            plano_n,
        )
        raise ValueError(
            "Conta ja possui assinatura Stripe ativa; atualize o plano pela assinatura existente "
            "em vez de novo checkout."
        )
    idempotency_key = f"stripe_checkout_start:{conta_id}:{franquia_id}:{plano_n}:{uuid.uuid4().hex}"
    response = _stripe_post("/checkout/sessions", payload, idempotency_key=idempotency_key)
    session_id = _norm_text(response.get("id"))
    if not session_id:
        raise ValueError("Resposta Stripe sem id de checkout session.")

    registrar_fato_monetizacao(
        tipo_fato="stripe_checkout_session_created",
        status_tecnico="success",
        provider=PROVIDER_STRIPE,
        conta_id=conta_id,
        franquia_id=franquia_id,
        usuario_id=usuario_id,
        idempotency_key=idempotency_key,
        correlation_key=session_id,
        external_event_id=session_id,
        customer_id=_norm_text(response.get("customer")),
        subscription_id=_norm_text(response.get("subscription")),
        price_id=price_id,
        identificadores_externos={
            "checkout_session_id": session_id,
            "client_reference_id": response.get("client_reference_id"),
        },
        snapshot_normalizado={
            "checkout_session_id": session_id,
            "plano_interno": plano_n,
            "conta_id": conta_id,
            "franquia_id": franquia_id,
            "status": response.get("status"),
            "payment_status": response.get("payment_status"),
        },
        payload_bruto_sanitizado=response,
    )

    return {
        "checkout_session_id": session_id,
        "checkout_client_secret": _norm_text(response.get("client_secret")),
        "publishable_key": publishable_key,
        "plano_codigo": plano_n,
    }


def sincronizar_retorno_checkout_stripe(
    *,
    checkout_session_id: str,
) -> dict[str, Any]:
    session_id = (checkout_session_id or "").strip()
    if not session_id:
        raise ValueError("checkout_session_id_obrigatorio")

    session = _stripe_get(
        f"/checkout/sessions/{session_id}",
        params=[
            ("expand[]", "line_items.data.price"),
            ("expand[]", "invoice.lines.data.price"),
            ("expand[]", "subscription.items.data.price"),
            ("expand[]", "subscription"),
            ("expand[]", "invoice"),
        ],
    )
    status = (_norm_text(session.get("status")) or "").lower()
    payment_status = (_norm_text(session.get("payment_status")) or "").lower()

    event_type, object_data = _montar_evento_retorno_checkout(session)
    synthetic_event = {
        "id": f"checkout_return:{session_id}:{event_type}",
        "type": event_type,
        "created": int(time.time()),
        "data": {"object": object_data},
    }
    resultado = processar_evento_stripe(synthetic_event)
    resultado["checkout_session_id"] = session_id
    resultado["checkout_status"] = status
    resultado["checkout_payment_status"] = payment_status
    return resultado


def _montar_evento_retorno_checkout(session: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    session_obj = dict(session or {})
    subscription = session_obj.get("subscription")
    invoice = session_obj.get("invoice")
    subscription_obj = subscription if isinstance(subscription, dict) else {}
    invoice_obj = invoice if isinstance(invoice, dict) else {}

    metadata_merged = {}
    for candidate in (
        session_obj.get("metadata"),
        subscription_obj.get("metadata"),
        invoice_obj.get("metadata"),
    ):
        if isinstance(candidate, dict):
            for k, v in candidate.items():
                if v not in (None, ""):
                    metadata_merged[k] = v

    status = (_norm_text(session_obj.get("status")) or "").lower()
    payment_status = (_norm_text(session_obj.get("payment_status")) or "").lower()

    if payment_status == "paid" and invoice_obj:
        invoice_payload = dict(invoice_obj)
        invoice_payload["metadata"] = metadata_merged
        if not invoice_payload.get("customer") and session_obj.get("customer"):
            invoice_payload["customer"] = session_obj.get("customer")
        if not invoice_payload.get("subscription") and session_obj.get("subscription"):
            invoice_payload["subscription"] = (
                subscription_obj.get("id")
                if isinstance(subscription_obj, dict) and subscription_obj.get("id")
                else session_obj.get("subscription")
            )
        return "invoice.paid", invoice_payload

    if subscription_obj:
        subscription_payload = dict(subscription_obj)
        subscription_payload["metadata"] = metadata_merged
        if not subscription_payload.get("customer") and session_obj.get("customer"):
            subscription_payload["customer"] = session_obj.get("customer")
        sub_status = (_norm_text(subscription_payload.get("status")) or "").lower()
        if payment_status == "paid" or sub_status in {"active", "trialing"}:
            return "customer.subscription.updated", subscription_payload
        return "checkout.session.completed", _build_checkout_completed_payload(
            session_obj, metadata_merged
        )

    return "checkout.session.completed", _build_checkout_completed_payload(
        session_obj, metadata_merged
    )


def _build_checkout_completed_payload(
    session_obj: dict[str, Any],
    metadata_merged: dict[str, Any],
) -> dict[str, Any]:
    payload = dict(session_obj)
    payload["metadata"] = metadata_merged
    subscription = session_obj.get("subscription")
    invoice = session_obj.get("invoice")
    if isinstance(subscription, dict):
        payload["subscription"] = subscription.get("id")
    if isinstance(invoice, dict):
        payload["invoice"] = invoice.get("id")
    return payload


def registrar_vinculo_comercial_externo(
    *,
    conta_id: int,
    provider: str,
    customer_id: str | None = None,
    subscription_id: str | None = None,
    price_id: str | None = None,
    plano_interno: str | None = None,
    status_contratual_externo: str | None = None,
    vigencia_externa_inicio: datetime | None = None,
    vigencia_externa_fim: datetime | None = None,
    snapshot_normalizado: dict[str, Any] | None = None,
    payload_bruto_sanitizado: dict[str, Any] | None = None,
    substituir_vinculo_ativo: bool = True,
) -> ContaMonetizacaoVinculo:
    """
    Cria um novo vinculo comercial externo.
    Quando solicitado, desativa vinculos ativos anteriores da mesma conta (mantendo historico).
    """
    conta_id_i = int(conta_id)
    provider_n = (provider or "").strip().lower()
    if not provider_n:
        raise ValueError("provider e obrigatorio para registrar vinculo comercial externo.")

    vinculo_ativo_stripe = (
        _obter_vinculo_ativo_por_conta(conta_id_i)
        if provider_n == PROVIDER_STRIPE and substituir_vinculo_ativo
        else None
    )
    if vinculo_ativo_stripe is not None:
        sub_ativo = _norm_text(vinculo_ativo_stripe.subscription_id)
        cus_ativo = _norm_text(vinculo_ativo_stripe.customer_id)
        sub_novo = _norm_text(subscription_id)
        cus_novo = _norm_text(customer_id)
        pendencia_ativa = _extrair_pendencia_downgrade_snapshot(
            _json_loads(vinculo_ativo_stripe.snapshot_normalizado_json)
        )
        sub_divergente = bool(sub_ativo and sub_novo and sub_ativo != sub_novo)
        cus_divergente = bool(cus_ativo and cus_novo and cus_ativo != cus_novo)
        if sub_divergente or cus_divergente:
            motivo = "ids_divergentes_do_vinculo_ativo"
            if sub_divergente:
                motivo = "subscription_id_divergente_do_vinculo_ativo"
            if cus_divergente:
                motivo = "customer_id_divergente_do_vinculo_ativo"
            if pendencia_ativa is not None:
                motivo = f"{motivo}_durante_mudanca_pendente"
            franquia_row = (
                Franquia.query.filter(Franquia.conta_id == conta_id_i)
                .order_by(Franquia.id.asc())
                .first()
            )
            registrar_fato_monetizacao(
                tipo_fato="stripe_vinculo_persistencia_bloqueada",
                status_tecnico=STATUS_TEC_SEM_EFEITO,
                provider=provider_n,
                conta_id=conta_id_i,
                franquia_id=(int(franquia_row.id) if franquia_row is not None else None),
                idempotency_key=(
                    f"stripe_vinculo_persistencia_bloqueada:{conta_id_i}:{motivo}:{sub_novo or '-'}:{cus_novo or '-'}"
                )[:190],
                correlation_key=sub_novo or sub_ativo,
                external_event_id=sub_novo,
                customer_id=cus_novo,
                subscription_id=sub_novo,
                price_id=_norm_text(price_id),
                snapshot_normalizado={
                    "motivo": motivo,
                    "vinculo_id_ativo": int(vinculo_ativo_stripe.id),
                    "vinculo_subscription_id": sub_ativo,
                    "vinculo_customer_id": cus_ativo,
                    "novo_subscription_id": sub_novo,
                    "novo_customer_id": cus_novo,
                    "mudanca_pendente": bool(pendencia_ativa is not None),
                    "plano_pendente": (
                        pendencia_ativa.get("plano_futuro") if pendencia_ativa is not None else None
                    ),
                    "efetivar_em": (
                        pendencia_ativa.get("efetivar_em") if pendencia_ativa is not None else None
                    ),
                },
                payload_bruto_sanitizado=payload_bruto_sanitizado,
            )
            return vinculo_ativo_stripe

    novo: ContaMonetizacaoVinculo | None = None
    snapshot_base = dict(snapshot_normalizado or {})
    try:
        # Savepoint local: colisao de unicidade nao deve invalidar a transacao externa.
        with db.session.begin_nested():
            snapshot_vinculo_anterior: dict[str, Any] | None = None
            ativos: list[Any] = []
            if substituir_vinculo_ativo:
                ativos = (
                    ContaMonetizacaoVinculo.query.filter_by(conta_id=conta_id_i, ativo=True)
                    .with_for_update()
                    .order_by(ContaMonetizacaoVinculo.id.desc())
                    .all()
                )
                if ativos:
                    snapshot_vinculo_anterior = _json_loads(ativos[0].snapshot_normalizado_json)
                agora = utcnow_naive()
                for row in ativos:
                    row.ativo = False
                    row.desativado_em = agora
                    db.session.add(row)
                if ativos:
                    db.session.flush()
            snapshot_merged = _mesclar_pendencia_no_snapshot(
                snapshot_novo=snapshot_base,
                snapshot_anterior=snapshot_vinculo_anterior,
            )
            prev_subscription_id = None
            if substituir_vinculo_ativo and ativos:
                prev_subscription_id = _norm_text(ativos[0].subscription_id) or None
            effective_subscription_id = _norm_text(subscription_id) or prev_subscription_id

            novo = ContaMonetizacaoVinculo(
                conta_id=conta_id_i,
                provider=provider_n,
                customer_id=(customer_id or "").strip() or None,
                subscription_id=(effective_subscription_id or "").strip() or None,
                price_id=(price_id or "").strip() or None,
                plano_interno=(plano_interno or "").strip().lower() or None,
                status_contratual_externo=(status_contratual_externo or "").strip().lower() or None,
                vigencia_externa_inicio=vigencia_externa_inicio,
                vigencia_externa_fim=vigencia_externa_fim,
                ativo=True,
                snapshot_normalizado_json=_json_dumps(snapshot_merged),
                payload_bruto_sanitizado_json=_json_dumps(payload_bruto_sanitizado),
            )
            db.session.add(novo)
            db.session.flush()
    except IntegrityError as exc:
        ativo_atual = (
            ContaMonetizacaoVinculo.query.filter_by(conta_id=conta_id_i, ativo=True)
            .order_by(ContaMonetizacaoVinculo.updated_at.desc(), ContaMonetizacaoVinculo.id.desc())
            .first()
        )
        if ativo_atual is not None:
            snapshot_merge_concorrencia = _mesclar_pendencia_no_snapshot(
                snapshot_novo=dict(snapshot_normalizado or {}),
                snapshot_anterior=_json_loads(ativo_atual.snapshot_normalizado_json),
            )
            prev_sub_conc = _norm_text(ativo_atual.subscription_id) or None
            effective_sub_conc = _norm_text(subscription_id) or prev_sub_conc
            ativo_atual.provider = provider_n
            ativo_atual.customer_id = (customer_id or "").strip() or None
            ativo_atual.subscription_id = (effective_sub_conc or "").strip() or None
            ativo_atual.price_id = (price_id or "").strip() or None
            ativo_atual.plano_interno = (plano_interno or "").strip().lower() or None
            ativo_atual.status_contratual_externo = (
                (status_contratual_externo or "").strip().lower() or None
            )
            ativo_atual.vigencia_externa_inicio = vigencia_externa_inicio
            ativo_atual.vigencia_externa_fim = vigencia_externa_fim
            ativo_atual.snapshot_normalizado_json = _json_dumps(snapshot_merge_concorrencia)
            ativo_atual.payload_bruto_sanitizado_json = _json_dumps(payload_bruto_sanitizado)
            db.session.add(ativo_atual)
            db.session.flush()
            return ativo_atual
        raise ValueError(
            "Nao foi possivel registrar o vinculo por colisao de concorrencia."
        ) from exc
    if novo is None:
        raise ValueError("Nao foi possivel registrar o vinculo comercial externo.")
    return novo


def registrar_fato_monetizacao(
    *,
    tipo_fato: str,
    status_tecnico: str,
    conta_id: int | None = None,
    franquia_id: int | None = None,
    usuario_id: int | None = None,
    provider: str | None = None,
    idempotency_key: str | None = None,
    correlation_key: str | None = None,
    timestamp_externo: datetime | None = None,
    external_event_id: str | None = None,
    customer_id: str | None = None,
    subscription_id: str | None = None,
    price_id: str | None = None,
    invoice_id: str | None = None,
    identificadores_externos: dict[str, Any] | None = None,
    snapshot_normalizado: dict[str, Any] | None = None,
    payload_bruto_sanitizado: dict[str, Any] | None = None,
) -> MonetizacaoFato:
    """
    Persiste fato append-only de monetizacao para futura correlacao de eventos externos.
    """
    tipo_n = (tipo_fato or "").strip().lower()
    status_n = (status_tecnico or "").strip().lower()
    idempotency_key_n = (idempotency_key or "").strip() or None
    if not tipo_n:
        raise ValueError("tipo_fato e obrigatorio.")
    if not status_n:
        raise ValueError("status_tecnico e obrigatorio.")
    if idempotency_key_n:
        existente = MonetizacaoFato.query.filter_by(
            idempotency_key=idempotency_key_n
        ).first()
        if existente is not None:
            return existente

    row: MonetizacaoFato | None = None
    try:
        # Savepoint local: evita rollback global da sessao em colisao de idempotencia.
        with db.session.begin_nested():
            row = MonetizacaoFato(
                tipo_fato=tipo_n,
                status_tecnico=status_n,
                idempotency_key=idempotency_key_n,
                correlation_key=(correlation_key or "").strip() or None,
                timestamp_externo=timestamp_externo,
                timestamp_interno=utcnow_naive(),
                provider=(provider or "").strip().lower() or None,
                conta_id=int(conta_id) if conta_id is not None else None,
                franquia_id=int(franquia_id) if franquia_id is not None else None,
                usuario_id=int(usuario_id) if usuario_id is not None else None,
                external_event_id=(external_event_id or "").strip() or None,
                customer_id=(customer_id or "").strip() or None,
                subscription_id=(subscription_id or "").strip() or None,
                price_id=(price_id or "").strip() or None,
                invoice_id=(invoice_id or "").strip() or None,
                identificadores_externos_json=_json_dumps(identificadores_externos),
                snapshot_normalizado_json=_json_dumps(snapshot_normalizado),
                payload_bruto_sanitizado_json=_json_dumps(payload_bruto_sanitizado),
            )
            db.session.add(row)
            db.session.flush()
    except IntegrityError as exc:
        if idempotency_key_n:
            existente = MonetizacaoFato.query.filter_by(
                idempotency_key=idempotency_key_n
            ).first()
            if existente is not None:
                return existente
            raise ValueError(
                "Nao foi possivel registrar fato monetario: colisao de idempotencia sem estado reaproveitavel."
            ) from exc
        raise
    if row is None:
        raise ValueError("Nao foi possivel registrar fato monetario.")
    return row


def validar_assinatura_webhook_stripe(
    raw_payload: bytes,
    stripe_signature_header: str | None,
    *,
    tolerance_seconds: int = 300,
) -> None:
    """
    Verificacao HMAC SHA256 da assinatura Stripe (formato Stripe-Signature).
    """
    secret = _obter_webhook_secret_stripe()
    if not secret:
        raise ValueError("STRIPE_WEBHOOK_SECRET ausente no ambiente.")
    sig_header = (stripe_signature_header or "").strip()
    if not sig_header:
        raise ValueError("Header Stripe-Signature ausente.")
    parts = {}
    for chunk in sig_header.split(","):
        if "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        parts.setdefault(k.strip(), []).append(v.strip())
    ts_values = parts.get("t") or []
    v1_values = parts.get("v1") or []
    if not ts_values or not v1_values:
        raise ValueError("Header Stripe-Signature invalido.")
    ts_txt = ts_values[0]
    try:
        ts_int = int(ts_txt)
    except Exception as exc:
        raise ValueError("Timestamp invalido na assinatura Stripe.") from exc
    now_int = int(time.time())
    if abs(now_int - ts_int) > max(1, int(tolerance_seconds)):
        raise ValueError("Assinatura Stripe fora da janela de tolerancia.")
    signed_payload = f"{ts_txt}.{raw_payload.decode('utf-8')}".encode("utf-8")
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, candidate) for candidate in v1_values):
        raise ValueError("Assinatura Stripe invalida.")


def processar_webhook_stripe(
    *,
    raw_payload: bytes,
    stripe_signature_header: str | None,
) -> dict[str, Any]:
    """
    Fluxo oficial de ingestao webhook Stripe.
    """
    validar_assinatura_webhook_stripe(raw_payload, stripe_signature_header)
    try:
        evento = json.loads(raw_payload.decode("utf-8"))
    except Exception as exc:
        raise ValueError("Payload JSON invalido no webhook Stripe.") from exc
    if not isinstance(evento, dict):
        raise ValueError("Payload Stripe invalido: objeto esperado.")
    return processar_evento_stripe(evento)


def _buscar_fato_existente_para_conciliacao(
    *,
    event_type: str,
    subscription_id: str | None,
    invoice_id: str | None,
    idempotency_key: str | None,
) -> MonetizacaoFato | None:
    tipo_fato = f"stripe_{(event_type or '').replace('.', '_')}"
    if invoice_id:
        row = (
            MonetizacaoFato.query.filter_by(
                provider=PROVIDER_STRIPE,
                tipo_fato=tipo_fato,
                invoice_id=invoice_id,
            )
            .order_by(MonetizacaoFato.timestamp_interno.desc(), MonetizacaoFato.id.desc())
            .first()
        )
        if row is not None:
            return row
    if subscription_id:
        row = (
            MonetizacaoFato.query.filter_by(
                provider=PROVIDER_STRIPE,
                tipo_fato=tipo_fato,
                subscription_id=subscription_id,
            )
            .order_by(MonetizacaoFato.timestamp_interno.desc(), MonetizacaoFato.id.desc())
            .first()
        )
        if row is not None:
            return row
    if idempotency_key:
        return MonetizacaoFato.query.filter_by(idempotency_key=idempotency_key).first()
    return None


def processar_fato_stripe_conciliado(
    *,
    event_type: str,
    object_data: dict[str, Any],
    session_id: str | None = None,
    event_id: str | None = None,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    object_payload = dict(object_data or {})
    logging.info(
        "[StripeDebug][FatoConciliado] Entrada processar_fato_stripe_conciliado event_type=%s event_id=%s session_id=%s object_data_resumo=%s",
        event_type,
        event_id,
        session_id,
        {
            "id": object_payload.get("id"),
            "status": object_payload.get("status"),
            "customer": object_payload.get("customer"),
            "subscription": object_payload.get("subscription"),
            "invoice": object_payload.get("invoice"),
            "metadata": object_payload.get("metadata"),
        },
    )
    evento_interno = {
        "id": event_id,
        "type": (event_type or "").strip().lower(),
        "created": int(created_at.timestamp()) if isinstance(created_at, datetime) else int(time.time()),
        "data": {"object": object_payload},
    }
    ids = _extrair_ids_externos_stripe(evento_interno, object_payload)
    logging.info(
        "[StripeDebug][FatoConciliado] IDs externos extraidos customer_id=%s subscription_id=%s invoice_id=%s price_id=%s",
        ids.get("customer_id"),
        ids.get("subscription_id"),
        ids.get("invoice_id"),
        ids.get("price_id"),
    )
    event_type_n = (_norm_text(event_type) or "").lower()
    stable_resource_id = ids.get("invoice_id") or ids.get("subscription_id") or _norm_text(session_id)
    idempotency_key = None
    if event_type_n and stable_resource_id:
        idempotency_key = f"stripe_conciliacao:{event_type_n}:{stable_resource_id}"
    existente = _buscar_fato_existente_para_conciliacao(
        event_type=event_type_n,
        subscription_id=ids.get("subscription_id"),
        invoice_id=ids.get("invoice_id"),
        idempotency_key=idempotency_key,
    )
    logging.info(
        "[StripeDebug][FatoConciliado] Fato existente para conciliacao encontrado=%s status_tecnico=%s tipo_fato=%s",
        existente is not None,
        getattr(existente, "status_tecnico", None),
        getattr(existente, "tipo_fato", None),
    )
    if existente is not None:
        logging.info(
            "[StripeDebug][FatoConciliado] Retorno antecipado replay/idempotencia event_type=%s event_id=%s",
            event_type_n,
            event_id,
        )
        return {
            "ok": True,
            "replay": True,
            "event_id": event_id,
            "event_type": event_type_n,
            "status_tecnico": existente.status_tecnico,
            "efeito_operacional_aplicado": existente.status_tecnico == STATUS_TEC_APLICADO,
            "modo": "conciliacao_checkout",
        }

    correlacao = _resolver_correlacao_evento(
        evento=evento_interno,
        object_data=object_payload,
        ids=ids,
    )
    logging.info(
        "[StripeDebug][FatoConciliado] Correlacao resolvida conta_id=%s franquia_id=%s usuario_id=%s correlacao_inequivoca=%s pendencias=%s",
        correlacao.get("conta_id"),
        correlacao.get("franquia_id"),
        correlacao.get("usuario_id"),
        correlacao.get("correlacao_inequivoca"),
        correlacao.get("pendencias"),
    )
    snapshot_normalizado = {
        "event_id": event_id,
        "event_type": event_type_n,
        "event_created": created_at.isoformat() if created_at else None,
        "correlacao": correlacao,
        "ids_externos": ids,
        "origem_ingestao": "conciliacao_checkout_session",
        "checkout_session_id": _norm_text(session_id),
    }

    if event_type_n not in STRIPE_EVENTOS_RELEVANTES:
        fato = _persistir_fato_evento_stripe(
            fato_reprocessado=None,
            tipo_fato="stripe_evento_ignorado",
            status_tecnico=STATUS_TEC_IGNORADO,
            correlacao=correlacao,
            idempotency_key=idempotency_key,
            event_id=event_id,
            created_at=created_at,
            ids=ids,
            snapshot_normalizado=snapshot_normalizado,
            evento=evento_interno,
        )
        db.session.commit()
        logging.info(
            "[StripeDebug][FatoConciliado] Retorno antecipado evento ignorado event_type=%s status_tecnico=%s",
            event_type_n,
            fato.status_tecnico,
        )
        return {
            "ok": True,
            "replay": False,
            "event_id": event_id,
            "event_type": event_type_n,
            "status_tecnico": fato.status_tecnico,
            "efeito_operacional_aplicado": False,
            "modo": "conciliacao_checkout",
        }

    if not correlacao.get("correlacao_inequivoca"):
        fato = _persistir_fato_evento_stripe(
            fato_reprocessado=None,
            tipo_fato=f"stripe_{event_type_n.replace('.', '_')}",
            status_tecnico=STATUS_TEC_PENDENTE_CORRELACAO,
            correlacao=correlacao,
            idempotency_key=idempotency_key,
            event_id=event_id,
            created_at=created_at,
            ids=ids,
            snapshot_normalizado=snapshot_normalizado,
            evento=evento_interno,
        )
        db.session.commit()
        logging.warning(
            "[StripeDebug][FatoConciliado] Retorno antecipado pendente correlacao event_type=%s pendencias=%s",
            event_type_n,
            correlacao.get("pendencias"),
        )
        return {
            "ok": True,
            "replay": False,
            "event_id": event_id,
            "event_type": event_type_n,
            "status_tecnico": fato.status_tecnico,
            "efeito_operacional_aplicado": False,
            "pendente_correlacao": True,
            "modo": "conciliacao_checkout",
        }

    plano_codigo = _resolver_plano_interno_evento(
        object_data=object_payload,
        ids=ids,
        correlacao=correlacao,
    )
    if event_type_n == "customer.subscription.deleted":
        plano_codigo = "free"
    logging.info(
        "[StripeDebug][FatoConciliado] Plano interno resolvido plano_codigo=%s",
        plano_codigo,
    )
    status_contratual = _resolver_status_contratual_evento(
        event_type=event_type_n,
        object_data=object_payload,
    )
    logging.info(
        "[StripeDebug][FatoConciliado] Status contratual resolvido status_contratual=%s",
        status_contratual,
    )
    ciclo = _resolver_ciclo_evento_stripe(
        event_type=event_type_n,
        object_data=object_payload,
    )
    logging.info(
        "[StripeDebug][FatoConciliado] Ciclo resolvido fonte_ciclo=%s inicio_ciclo=%s fim_ciclo=%s pendencias=%s",
        ciclo.get("fonte_ciclo"),
        ciclo.get("inicio_ciclo"),
        ciclo.get("fim_ciclo"),
        ciclo.get("pendencias"),
    )
    efeito_operacional = {
        "aplicado": False,
        "consumo_reiniciado_renovacao": False,
        "status_operacional_resultante": None,
        "politica_payment_failed": None,
        "ciclo_ignorado_evento_antigo": False,
    }
    ignorar_atualizacao_vinculo_evento_antigo = False
    if event_type_n == "invoice.paid":
        fr = db.session.get(Franquia, int(correlacao["franquia_id"]))
        if fr is not None:
            inicio_ciclo_evt = ciclo.get("inicio_ciclo")
            fim_ciclo_evt = ciclo.get("fim_ciclo")
            if (
                inicio_ciclo_evt is not None
                and fim_ciclo_evt is not None
                and fr.fim_ciclo is not None
                and fim_ciclo_evt <= fr.fim_ciclo
            ):
                ignorar_atualizacao_vinculo_evento_antigo = True
    bloquear_promocao_vinculo_ids, detalhes_guard_vinculo = _guardrail_ids_evento_vs_vinculo_ativo(
        conta_id=correlacao.get("conta_id"),
        customer_id_evento=ids.get("customer_id"),
        subscription_id_evento=ids.get("subscription_id"),
    )
    if bloquear_promocao_vinculo_ids:
        logger.warning(
            "[Stripe][VinculoGuardrail] promocao_vinculo_bloqueada modo=conciliacao conta_id=%s motivo=%s detalhes=%s",
            correlacao.get("conta_id"),
            detalhes_guard_vinculo.get("motivo"),
            _json_dumps(detalhes_guard_vinculo),
        )
        registrar_fato_monetizacao(
            tipo_fato="stripe_vinculo_guardrail_ids_inconsistentes",
            status_tecnico=STATUS_TEC_SEM_EFEITO,
            provider=PROVIDER_STRIPE,
            conta_id=int(correlacao["conta_id"]),
            franquia_id=_to_int_or_none(correlacao.get("franquia_id")),
            usuario_id=_to_int_or_none(correlacao.get("usuario_id")),
            idempotency_key=(
                f"stripe_vinculo_guardrail:{event_id or stable_resource_id}:"
                f"{(detalhes_guard_vinculo.get('motivo') or 'desconhecido')}"
            )[:190],
            correlation_key=_norm_text(ids.get("subscription_id")),
            external_event_id=_norm_text(event_id),
            customer_id=ids.get("customer_id"),
            subscription_id=ids.get("subscription_id"),
            snapshot_normalizado={
                "guardrail": True,
                "modo": "conciliacao_checkout_session",
                "detalhes": detalhes_guard_vinculo,
                "event_type": event_type_n,
            },
            payload_bruto_sanitizado=object_payload,
        )
    vinculo_atualizado = None
    if not ignorar_atualizacao_vinculo_evento_antigo and not bloquear_promocao_vinculo_ids:
        logging.info(
            "[StripeDebug][FatoConciliado] Antes atualizar_vinculo_comercial_stripe entrada=%s",
            {
                "conta_id": int(correlacao["conta_id"]),
                "plano_interno": plano_codigo,
                "status_contratual_externo": status_contratual,
                "customer_id": ids.get("customer_id"),
                "subscription_id": ids.get("subscription_id"),
                "price_id": ids.get("price_id"),
                "vigencia_externa_inicio": ciclo.get("inicio_ciclo"),
                "vigencia_externa_fim": ciclo.get("fim_ciclo"),
            },
        )
        vinculo_atualizado = atualizar_vinculo_comercial_stripe(
            conta_id=int(correlacao["conta_id"]),
            plano_interno=plano_codigo,
            status_contratual_externo=status_contratual,
            customer_id=ids.get("customer_id"),
            subscription_id=ids.get("subscription_id"),
            price_id=ids.get("price_id"),
            vigencia_externa_inicio=ciclo.get("inicio_ciclo"),
            vigencia_externa_fim=ciclo.get("fim_ciclo"),
            snapshot_normalizado={
                "fonte_evento": event_type_n,
                "confianca_ciclo": ciclo.get("fonte_ciclo"),
                "pendencias": ciclo.get("pendencias"),
                "status_contratual_externo": status_contratual,
                "origem_ingestao": "conciliacao_checkout_session",
            },
            payload_bruto_sanitizado=object_payload,
        )
        logging.info(
            "[StripeDebug][FatoConciliado] Depois atualizar_vinculo_comercial_stripe retorno=%s",
            {
                "id": getattr(vinculo_atualizado, "id", None),
                "conta_id": getattr(vinculo_atualizado, "conta_id", None),
                "provider": getattr(vinculo_atualizado, "provider", None),
                "customer_id": getattr(vinculo_atualizado, "customer_id", None),
                "subscription_id": getattr(vinculo_atualizado, "subscription_id", None),
                "price_id": getattr(vinculo_atualizado, "price_id", None),
                "plano_interno": getattr(vinculo_atualizado, "plano_interno", None),
                "status_contratual_externo": getattr(
                    vinculo_atualizado, "status_contratual_externo", None
                ),
                "ativo": getattr(vinculo_atualizado, "ativo", None),
            },
        )
    elif bloquear_promocao_vinculo_ids:
        logging.info(
            "[StripeDebug][FatoConciliado] atualizar_vinculo_comercial_stripe omitido guardrail_ids_inconsistentes",
        )

    if event_type_n in {
        "invoice.paid",
        "invoice.payment_failed",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    } and not bloquear_promocao_vinculo_ids:
        logging.info(
            "[StripeDebug][FatoConciliado] Antes aplicar_fato_contratual_em_franquia franquia_id=%s plano_codigo=%s event_type=%s status_contratual=%s",
            correlacao.get("franquia_id"),
            plano_codigo,
            event_type_n,
            status_contratual,
        )
        efeito_operacional = aplicar_fato_contratual_em_franquia(
            franquia_id=int(correlacao["franquia_id"]),
            plano_codigo=plano_codigo,
            event_type=event_type_n,
            status_contratual_externo=status_contratual,
            ciclo=ciclo,
        )
        logging.info(
            "[StripeDebug][FatoConciliado] Depois aplicar_fato_contratual_em_franquia retorno=%s",
            efeito_operacional,
        )
    efeito_aplicado = bool(efeito_operacional.get("aplicado"))

    status_tecnico = STATUS_TEC_APLICADO if efeito_aplicado else STATUS_TEC_SEM_EFEITO
    snapshot_normalizado["plano_resolvido"] = plano_codigo
    snapshot_normalizado["status_contratual_externo"] = status_contratual
    snapshot_normalizado["ciclo_contratual"] = {
        "inicio_ciclo": (
            ciclo.get("inicio_ciclo").isoformat() if ciclo.get("inicio_ciclo") else None
        ),
        "fim_ciclo": (
            ciclo.get("fim_ciclo").isoformat() if ciclo.get("fim_ciclo") else None
        ),
        "fonte_ciclo": ciclo.get("fonte_ciclo"),
        "pendencias": ciclo.get("pendencias"),
    }
    snapshot_normalizado["efeito_operacional_aplicado"] = efeito_aplicado
    snapshot_normalizado["efeito_operacional"] = {
        "consumo_reiniciado_renovacao": bool(
            efeito_operacional.get("consumo_reiniciado_renovacao")
        ),
        "status_operacional_resultante": efeito_operacional.get(
            "status_operacional_resultante"
        ),
        "politica_payment_failed": efeito_operacional.get("politica_payment_failed"),
        "ciclo_ignorado_evento_antigo": bool(
            efeito_operacional.get("ciclo_ignorado_evento_antigo")
        ),
        "mudanca_pendente": bool(efeito_operacional.get("mudanca_pendente")),
        "plano_pendente": efeito_operacional.get("plano_pendente"),
        "efetivar_em": efeito_operacional.get("efetivar_em"),
    }
    snapshot_normalizado["vinculo_externo_ignorado_evento_antigo"] = bool(
        ignorar_atualizacao_vinculo_evento_antigo
    )
    snapshot_normalizado["vinculo_guardrail_ids_inconsistentes"] = bool(bloquear_promocao_vinculo_ids)
    if bloquear_promocao_vinculo_ids:
        snapshot_normalizado["vinculo_guardrail_detalhes"] = detalhes_guard_vinculo

    fato = _persistir_fato_evento_stripe(
        fato_reprocessado=None,
        tipo_fato=f"stripe_{event_type_n.replace('.', '_')}",
        status_tecnico=status_tecnico,
        correlacao=correlacao,
        idempotency_key=idempotency_key,
        event_id=event_id,
        created_at=created_at,
        ids=ids,
        snapshot_normalizado=snapshot_normalizado,
        evento=evento_interno,
    )
    db.session.commit()
    if efeito_aplicado:
        logging.info(
            "[StripeDebug][FatoConciliado] Retorno final aplicado com efeito operacional event_type=%s status_tecnico=%s",
            event_type_n,
            fato.status_tecnico,
        )
    else:
        logging.info(
            "[StripeDebug][FatoConciliado] Retorno final sem efeito operacional event_type=%s status_tecnico=%s",
            event_type_n,
            fato.status_tecnico,
        )
    return {
        "ok": True,
        "replay": False,
        "event_id": event_id,
        "event_type": event_type_n,
        "status_tecnico": fato.status_tecnico,
        "efeito_operacional_aplicado": efeito_aplicado,
        "mudanca_pendente": bool(efeito_operacional.get("mudanca_pendente")),
        "plano_pendente": efeito_operacional.get("plano_pendente"),
        "efetivar_em": efeito_operacional.get("efetivar_em"),
        "modo": "conciliacao_checkout",
        "vinculo_guardrail_bloqueado": bool(bloquear_promocao_vinculo_ids),
    }


def processar_evento_stripe(
    evento: dict[str, Any],
    *,
    reprocessamento_admin: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event_id = _norm_text(evento.get("id"))
    event_type = (_norm_text(evento.get("type")) or "").lower()
    created_at = _to_datetime_utc_naive(evento.get("created"))
    object_data = (
        ((evento.get("data") or {}).get("object") or {})
        if isinstance(evento.get("data"), dict)
        else {}
    )
    if not isinstance(object_data, dict):
        object_data = {}

    idempotency_key = None
    if event_id and event_type:
        idempotency_key = f"stripe_event:{event_id}:{event_type}"
    fato_reprocessado: MonetizacaoFato | None = None
    is_reprocessamento_admin = bool(reprocessamento_admin)
    if idempotency_key:
        existente = MonetizacaoFato.query.filter_by(idempotency_key=idempotency_key).first()
        if existente is not None:
            if (
                is_reprocessamento_admin
                and (existente.status_tecnico or "").strip().lower()
                == STATUS_TEC_PENDENTE_CORRELACAO
            ):
                fato_reprocessado = existente
            else:
                return {
                    "ok": True,
                    "replay": True,
                    "event_id": event_id,
                    "event_type": event_type,
                    "status_tecnico": existente.status_tecnico,
                    "efeito_operacional_aplicado": existente.status_tecnico == STATUS_TEC_APLICADO,
                }

    ids = _extrair_ids_externos_stripe(evento, object_data)
    correlacao = _resolver_correlacao_evento(evento=evento, object_data=object_data, ids=ids)
    snapshot_normalizado = {
        "event_id": event_id,
        "event_type": event_type,
        "event_created": created_at.isoformat() if created_at else None,
        "correlacao": correlacao,
        "ids_externos": ids,
    }
    if is_reprocessamento_admin:
        snapshot_normalizado["reprocessamento_admin"] = _montar_contexto_reprocessamento_admin(
            fato_reprocessado=fato_reprocessado,
            contexto=reprocessamento_admin,
        )

    if event_type not in STRIPE_EVENTOS_RELEVANTES:
        fato = _persistir_fato_evento_stripe(
            fato_reprocessado=fato_reprocessado,
            tipo_fato="stripe_evento_ignorado",
            status_tecnico=STATUS_TEC_IGNORADO,
            correlacao=correlacao,
            idempotency_key=idempotency_key,
            event_id=event_id,
            created_at=created_at,
            ids=ids,
            snapshot_normalizado=snapshot_normalizado,
            evento=evento,
        )
        db.session.commit()
        return {
            "ok": True,
            "replay": False,
            "event_id": event_id,
            "event_type": event_type,
            "status_tecnico": fato.status_tecnico,
            "efeito_operacional_aplicado": False,
        }

    if not correlacao.get("correlacao_inequivoca"):
        fato = _persistir_fato_evento_stripe(
            fato_reprocessado=fato_reprocessado,
            tipo_fato=f"stripe_{event_type.replace('.', '_')}",
            status_tecnico=STATUS_TEC_PENDENTE_CORRELACAO,
            correlacao=correlacao,
            idempotency_key=idempotency_key,
            event_id=event_id,
            created_at=created_at,
            ids=ids,
            snapshot_normalizado=snapshot_normalizado,
            evento=evento,
        )
        db.session.commit()
        return {
            "ok": True,
            "replay": False,
            "event_id": event_id,
            "event_type": event_type,
            "status_tecnico": fato.status_tecnico,
            "efeito_operacional_aplicado": False,
            "pendente_correlacao": True,
        }

    plano_codigo = _resolver_plano_interno_evento(
        object_data=object_data,
        ids=ids,
        correlacao=correlacao,
    )
    if event_type == "customer.subscription.deleted":
        plano_codigo = "free"
    status_contratual = _resolver_status_contratual_evento(event_type=event_type, object_data=object_data)
    ciclo = _resolver_ciclo_evento_stripe(event_type=event_type, object_data=object_data)
    efeito_operacional = {
        "aplicado": False,
        "consumo_reiniciado_renovacao": False,
        "status_operacional_resultante": None,
        "politica_payment_failed": None,
        "ciclo_ignorado_evento_antigo": False,
    }
    ignorar_atualizacao_vinculo_evento_antigo = False
    if event_type == "invoice.paid":
        fr = db.session.get(Franquia, int(correlacao["franquia_id"]))
        if fr is not None:
            inicio_ciclo_evt = ciclo.get("inicio_ciclo")
            fim_ciclo_evt = ciclo.get("fim_ciclo")
            if (
                inicio_ciclo_evt is not None
                and fim_ciclo_evt is not None
                and fr.fim_ciclo is not None
                and fim_ciclo_evt <= fr.fim_ciclo
            ):
                ignorar_atualizacao_vinculo_evento_antigo = True
    bloquear_promocao_vinculo_ids, detalhes_guard_vinculo = _guardrail_ids_evento_vs_vinculo_ativo(
        conta_id=correlacao.get("conta_id"),
        customer_id_evento=ids.get("customer_id"),
        subscription_id_evento=ids.get("subscription_id"),
    )
    if bloquear_promocao_vinculo_ids:
        logger.warning(
            "[Stripe][VinculoGuardrail] promocao_vinculo_bloqueada modo=webhook conta_id=%s motivo=%s detalhes=%s",
            correlacao.get("conta_id"),
            detalhes_guard_vinculo.get("motivo"),
            _json_dumps(detalhes_guard_vinculo),
        )
        registrar_fato_monetizacao(
            tipo_fato="stripe_vinculo_guardrail_ids_inconsistentes",
            status_tecnico=STATUS_TEC_SEM_EFEITO,
            provider=PROVIDER_STRIPE,
            conta_id=int(correlacao["conta_id"]),
            franquia_id=_to_int_or_none(correlacao.get("franquia_id")),
            usuario_id=_to_int_or_none(correlacao.get("usuario_id")),
            idempotency_key=(
                f"stripe_vinculo_guardrail:{event_id or 'sem_id'}:"
                f"{(detalhes_guard_vinculo.get('motivo') or 'desconhecido')}"
            )[:190],
            correlation_key=_norm_text(ids.get("subscription_id")),
            external_event_id=_norm_text(event_id),
            customer_id=ids.get("customer_id"),
            subscription_id=ids.get("subscription_id"),
            snapshot_normalizado={
                "guardrail": True,
                "modo": "webhook",
                "detalhes": detalhes_guard_vinculo,
                "event_type": event_type,
            },
            payload_bruto_sanitizado=object_data,
        )
    vinculo_stripe: ContaMonetizacaoVinculo | None = None
    if not ignorar_atualizacao_vinculo_evento_antigo and not bloquear_promocao_vinculo_ids:
        vinculo_stripe = atualizar_vinculo_comercial_stripe(
            conta_id=int(correlacao["conta_id"]),
            plano_interno=plano_codigo,
            status_contratual_externo=status_contratual,
            customer_id=ids.get("customer_id"),
            subscription_id=ids.get("subscription_id"),
            price_id=ids.get("price_id"),
            vigencia_externa_inicio=ciclo.get("inicio_ciclo"),
            vigencia_externa_fim=ciclo.get("fim_ciclo"),
            snapshot_normalizado={
                "fonte_evento": event_type,
                "confianca_ciclo": ciclo.get("fonte_ciclo"),
                "pendencias": ciclo.get("pendencias"),
                "status_contratual_externo": status_contratual,
            },
            payload_bruto_sanitizado=object_data,
        )

    if event_type in {
        "invoice.paid",
        "invoice.payment_failed",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    } and not bloquear_promocao_vinculo_ids:
        efeito_operacional = aplicar_fato_contratual_em_franquia(
            franquia_id=int(correlacao["franquia_id"]),
            plano_codigo=plano_codigo,
            event_type=event_type,
            status_contratual_externo=status_contratual,
            ciclo=ciclo,
        )
    efeito_aplicado = bool(efeito_operacional.get("aplicado"))

    status_tecnico = STATUS_TEC_APLICADO if efeito_aplicado else STATUS_TEC_SEM_EFEITO
    snapshot_normalizado["plano_resolvido"] = plano_codigo
    snapshot_normalizado["status_contratual_externo"] = status_contratual
    snapshot_normalizado["ciclo_contratual"] = {
        "inicio_ciclo": (
            ciclo.get("inicio_ciclo").isoformat() if ciclo.get("inicio_ciclo") else None
        ),
        "fim_ciclo": (
            ciclo.get("fim_ciclo").isoformat() if ciclo.get("fim_ciclo") else None
        ),
        "fonte_ciclo": ciclo.get("fonte_ciclo"),
        "pendencias": ciclo.get("pendencias"),
    }
    snapshot_normalizado["efeito_operacional_aplicado"] = efeito_aplicado
    snapshot_normalizado["efeito_operacional"] = {
        "consumo_reiniciado_renovacao": bool(
            efeito_operacional.get("consumo_reiniciado_renovacao")
        ),
        "status_operacional_resultante": efeito_operacional.get(
            "status_operacional_resultante"
        ),
        "politica_payment_failed": efeito_operacional.get("politica_payment_failed"),
        "ciclo_ignorado_evento_antigo": bool(
            efeito_operacional.get("ciclo_ignorado_evento_antigo")
        ),
        "mudanca_pendente": bool(efeito_operacional.get("mudanca_pendente")),
        "plano_pendente": efeito_operacional.get("plano_pendente"),
        "efetivar_em": efeito_operacional.get("efetivar_em"),
    }
    snapshot_normalizado["vinculo_externo_ignorado_evento_antigo"] = bool(
        ignorar_atualizacao_vinculo_evento_antigo
    )
    snapshot_normalizado["vinculo_guardrail_ids_inconsistentes"] = bool(bloquear_promocao_vinculo_ids)
    if bloquear_promocao_vinculo_ids:
        snapshot_normalizado["vinculo_guardrail_detalhes"] = detalhes_guard_vinculo

    fato = _persistir_fato_evento_stripe(
        fato_reprocessado=fato_reprocessado,
        tipo_fato=f"stripe_{event_type.replace('.', '_')}",
        status_tecnico=status_tecnico,
        correlacao=correlacao,
        idempotency_key=idempotency_key,
        event_id=event_id,
        created_at=created_at,
        ids=ids,
        snapshot_normalizado=snapshot_normalizado,
        evento=evento,
    )
    db.session.commit()
    return {
        "ok": True,
        "replay": False,
        "event_id": event_id,
        "event_type": event_type,
        "status_tecnico": fato.status_tecnico,
        "efeito_operacional_aplicado": efeito_aplicado,
        "mudanca_pendente": bool(efeito_operacional.get("mudanca_pendente")),
        "plano_pendente": efeito_operacional.get("plano_pendente"),
        "efetivar_em": efeito_operacional.get("efetivar_em"),
        "vinculo_guardrail_bloqueado": bool(bloquear_promocao_vinculo_ids),
    }


def conciliar_checkout_session_stripe(session_id: str) -> dict[str, Any]:
    session_id_n = (session_id or "").strip()
    if not session_id_n:
        raise ValueError("checkout_session_id_obrigatorio")
    logging.info(
        "[StripeDebug][Conciliacao] Entrada conciliar_checkout_session_stripe session_id=%s",
        session_id_n,
    )

    session = _stripe_get(f"/checkout/sessions/{session_id_n}")
    status = (_norm_text(session.get("status")) or "").lower()
    payment_status = (_norm_text(session.get("payment_status")) or "").lower()
    logging.info(
        "[StripeDebug][Conciliacao] Checkout session carregada session_id=%s status=%s payment_status=%s subscription=%s customer=%s client_reference_id=%s metadata=%s",
        session.get("id"),
        status,
        payment_status,
        session.get("subscription"),
        session.get("customer"),
        session.get("client_reference_id"),
        session.get("metadata"),
    )

    if payment_status != "paid" and status != "complete":
        logging.warning(
            "[StripeDebug][Conciliacao] Retorno antecipado checkout nao confirmado status=%s payment_status=%s motivo=%s",
            status,
            payment_status,
            "checkout_ainda_nao_confirmado",
        )
        return {
            "ok": True,
            "checkout_session_id": session_id_n,
            "checkout_status": status,
            "checkout_payment_status": payment_status,
            "conciliado": False,
            "motivo": "checkout_ainda_nao_confirmado",
        }

    subscription_id = _norm_text(session.get("subscription"))
    customer_id = _norm_text(session.get("customer"))
    metadata_session = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}

    if subscription_id:
        logging.info(
            "[StripeDebug][Conciliacao] Antes buscar subscription subscription_id=%s",
            subscription_id,
        )
        subscription = _stripe_get(
            f"/subscriptions/{subscription_id}",
            params=[("expand[]", "items.data.price")],
        )
        items_data = (
            (subscription.get("items") or {}).get("data")
            if isinstance(subscription.get("items"), dict)
            else None
        )
        first_item = items_data[0] if isinstance(items_data, list) and items_data else {}
        first_price = first_item.get("price") if isinstance(first_item, dict) else {}
        logging.info(
            "[StripeDebug][Conciliacao] Subscription carregada subscription_id=%s status=%s current_period_start=%s current_period_end=%s items_data_0_price_id=%s metadata=%s",
            subscription.get("id"),
            subscription.get("status"),
            subscription.get("current_period_start"),
            subscription.get("current_period_end"),
            (first_price.get("id") if isinstance(first_price, dict) else first_price),
            subscription.get("metadata"),
        )
        metadata = {}
        if isinstance(metadata_session, dict):
            metadata.update(metadata_session)
        metadata_subscription = (
            subscription.get("metadata") if isinstance(subscription.get("metadata"), dict) else {}
        )
        if isinstance(metadata_subscription, dict):
            for key, value in metadata_subscription.items():
                if value not in (None, ""):
                    metadata[key] = value
        subscription_payload = dict(subscription)
        subscription_payload["metadata"] = metadata
        if not subscription_payload.get("customer") and customer_id:
            subscription_payload["customer"] = customer_id
        client_reference_id = _norm_text(session.get("client_reference_id"))
        if client_reference_id and not subscription_payload.get("client_reference_id"):
            subscription_payload["client_reference_id"] = client_reference_id
        logging.info(
            "[StripeDebug][Conciliacao] Antes processar_fato_stripe_conciliado event_type=%s session_id=%s subscription_id=%s object_data_resumo=%s",
            "customer.subscription.updated",
            session_id_n,
            subscription_id,
            {
                "id": subscription_payload.get("id"),
                "status": subscription_payload.get("status"),
                "customer": subscription_payload.get("customer"),
                "metadata": subscription_payload.get("metadata"),
                "client_reference_id": subscription_payload.get("client_reference_id"),
            },
        )
        out = processar_fato_stripe_conciliado(
            event_type="customer.subscription.updated",
            object_data=subscription_payload,
            session_id=session_id_n,
            event_id=f"stripe_conciliacao:checkout_session:{session_id_n}:subscription:{subscription_id}",
            created_at=utcnow_naive(),
        )
        logging.info(
            "[StripeDebug][Conciliacao] Depois processar_fato_stripe_conciliado resultado=%s",
            out,
        )
        out["checkout_session_id"] = session_id_n
        out["checkout_status"] = status
        out["checkout_payment_status"] = payment_status
        out["subscription_id"] = subscription_id
        return out

    checkout_payload = dict(session)
    checkout_payload["metadata"] = metadata_session if isinstance(metadata_session, dict) else {}
    logging.info(
        "[StripeDebug][Conciliacao] Antes processar_fato_stripe_conciliado event_type=%s session_id=%s subscription_id=%s object_data_resumo=%s",
        "checkout.session.completed",
        session_id_n,
        subscription_id,
        {
            "id": checkout_payload.get("id"),
            "status": checkout_payload.get("status"),
            "payment_status": checkout_payload.get("payment_status"),
            "customer": checkout_payload.get("customer"),
            "metadata": checkout_payload.get("metadata"),
        },
    )
    out = processar_fato_stripe_conciliado(
        event_type="checkout.session.completed",
        object_data=checkout_payload,
        session_id=session_id_n,
        event_id=f"stripe_conciliacao:checkout_session:{session_id_n}",
        created_at=utcnow_naive(),
    )
    logging.info(
        "[StripeDebug][Conciliacao] Depois processar_fato_stripe_conciliado resultado=%s",
        out,
    )
    out["checkout_session_id"] = session_id_n
    out["checkout_status"] = status
    out["checkout_payment_status"] = payment_status
    return out


def _montar_contexto_reprocessamento_admin(
    *,
    fato_reprocessado: MonetizacaoFato | None,
    contexto: dict[str, Any] | None,
) -> dict[str, Any]:
    base = dict(contexto or {})
    tentativas_previas = 0
    if fato_reprocessado is not None:
        snapshot_anterior = _json_loads(fato_reprocessado.snapshot_normalizado_json)
        tentativas_previas = _extrair_contador_reprocessamento(snapshot_anterior)
    base["tentativas"] = tentativas_previas + 1
    base["reprocessado_em"] = utcnow_naive().isoformat()
    return base


def _persistir_fato_evento_stripe(
    *,
    fato_reprocessado: MonetizacaoFato | None,
    tipo_fato: str,
    status_tecnico: str,
    correlacao: dict[str, Any],
    idempotency_key: str | None,
    event_id: str | None,
    created_at: datetime | None,
    ids: dict[str, Any],
    snapshot_normalizado: dict[str, Any],
    evento: dict[str, Any],
) -> MonetizacaoFato:
    if fato_reprocessado is None:
        return registrar_fato_monetizacao(
            tipo_fato=tipo_fato,
            status_tecnico=status_tecnico,
            provider=PROVIDER_STRIPE,
            conta_id=correlacao.get("conta_id"),
            franquia_id=correlacao.get("franquia_id"),
            usuario_id=correlacao.get("usuario_id"),
            idempotency_key=idempotency_key,
            correlation_key=event_id,
            timestamp_externo=created_at,
            external_event_id=event_id,
            customer_id=ids.get("customer_id"),
            subscription_id=ids.get("subscription_id"),
            price_id=ids.get("price_id"),
            invoice_id=ids.get("invoice_id"),
            identificadores_externos=ids,
            snapshot_normalizado=snapshot_normalizado,
            payload_bruto_sanitizado=evento,
        )
    fato_reprocessado.tipo_fato = tipo_fato
    fato_reprocessado.status_tecnico = status_tecnico
    fato_reprocessado.provider = PROVIDER_STRIPE
    fato_reprocessado.conta_id = correlacao.get("conta_id")
    fato_reprocessado.franquia_id = correlacao.get("franquia_id")
    fato_reprocessado.usuario_id = correlacao.get("usuario_id")
    fato_reprocessado.correlation_key = event_id
    fato_reprocessado.timestamp_externo = created_at
    fato_reprocessado.external_event_id = event_id
    fato_reprocessado.customer_id = ids.get("customer_id")
    fato_reprocessado.subscription_id = ids.get("subscription_id")
    fato_reprocessado.price_id = ids.get("price_id")
    fato_reprocessado.invoice_id = ids.get("invoice_id")
    fato_reprocessado.identificadores_externos_json = _json_dumps(ids)
    fato_reprocessado.snapshot_normalizado_json = _json_dumps(snapshot_normalizado)
    fato_reprocessado.payload_bruto_sanitizado_json = _json_dumps(evento)
    db.session.add(fato_reprocessado)
    db.session.flush()
    return fato_reprocessado


def atualizar_vinculo_comercial_stripe(
    *,
    conta_id: int,
    plano_interno: str | None,
    status_contratual_externo: str | None,
    customer_id: str | None,
    subscription_id: str | None,
    price_id: str | None,
    vigencia_externa_inicio: datetime | None,
    vigencia_externa_fim: datetime | None,
    snapshot_normalizado: dict[str, Any] | None,
    payload_bruto_sanitizado: dict[str, Any] | None,
) -> ContaMonetizacaoVinculo:
    return registrar_vinculo_comercial_externo(
        conta_id=int(conta_id),
        provider=PROVIDER_STRIPE,
        customer_id=customer_id,
        subscription_id=subscription_id,
        price_id=price_id,
        plano_interno=plano_interno,
        status_contratual_externo=status_contratual_externo,
        vigencia_externa_inicio=vigencia_externa_inicio,
        vigencia_externa_fim=vigencia_externa_fim,
        snapshot_normalizado=snapshot_normalizado,
        payload_bruto_sanitizado=payload_bruto_sanitizado,
        substituir_vinculo_ativo=True,
    )


def _aplicar_plano_operacional_franquia(fr: Franquia, plano_n: str) -> bool:
    alterou = False
    limite_ref = plano_service.obter_limite_referencia_plano_admin(
        plano_n,
        exigir_configurado=False,
    )
    if limite_ref is not None and fr.limite_total != limite_ref:
        fr.limite_total = limite_ref
        alterou = True
    if _sincronizar_categoria_usuarios_franquia(int(fr.id), plano_n):
        alterou = True
    return alterou


def _downgrade_pago_ja_vigente_ciclo_stripe(fr: Franquia, ciclo: dict[str, Any]) -> bool:
    """
    O periodo de cobranca do evento (inicio) ja e posterior ao fim de ciclo operacional
    conhecido, ou, sem fim local, o inicio Stripe e estritamente posterior ao inicio local.
    Isto alinha fim/virada reais: downgrade agendado continua com fim de ciclo ainda distante
    (ex.: 2099) e nao e confundido com virada ja ocorrida.
    """
    ci = ciclo.get("inicio_ciclo")
    if not isinstance(ci, datetime):
        return False
    if fr.fim_ciclo is not None:
        return ci >= fr.fim_ciclo
    if fr.inicio_ciclo is not None:
        return ci > fr.inicio_ciclo
    return False


def aplicar_fato_contratual_em_franquia(
    *,
    franquia_id: int,
    plano_codigo: str | None,
    event_type: str,
    status_contratual_externo: str | None,
    ciclo: dict[str, Any],
) -> dict[str, Any]:
    """
    Aplica efeito contratual centralizado sobre Franquia.
    """
    fr = db.session.get(Franquia, int(franquia_id))
    if fr is None:
        return {
            "aplicado": False,
            "consumo_reiniciado_renovacao": False,
            "status_operacional_resultante": None,
            "politica_payment_failed": None,
            "mudanca_pendente": False,
            "plano_pendente": None,
            "efetivar_em": None,
        }

    alterou = False
    consumo_reiniciado = False
    politica_payment_failed = None
    ciclo_ignorado_evento_antigo = False
    plano_n = _normalizar_plano_codigo(plano_codigo)
    plano_atual = _resolver_plano_operacional_atual(int(franquia_id))
    mudanca_pendente = False
    plano_pendente = None
    efetivar_em = None
    agora = utcnow_naive()
    vinculo_ativo_conta = _obter_vinculo_ativo_por_conta(int(fr.conta_id))
    vigencia_externa_fim = (
        vinculo_ativo_conta.vigencia_externa_fim if vinculo_ativo_conta is not None else None
    )
    ciclo_fim_evento = ciclo.get("fim_ciclo")
    eh_downgrade = (
        plano_n in {"free", "starter", "pro"}
        and plano_n is not None
        and _rank_plano(plano_n) < _rank_plano(plano_atual)
    )
    downgrade_para_plano_pago = bool(eh_downgrade and plano_n not in (None, "free"))
    downgrade_pago_ja_vigente = bool(
        downgrade_para_plano_pago and _downgrade_pago_ja_vigente_ciclo_stripe(fr, ciclo)
    )
    free_efetivado_no_deleted = False

    if (event_type or "").strip().lower() == "customer.subscription.deleted":
        pendencia_existente = None
        if vinculo_ativo_conta is not None:
            pendencia_existente = _extrair_pendencia_downgrade_snapshot(
                _json_loads(vinculo_ativo_conta.snapshot_normalizado_json)
            )
        if (
            pendencia_existente is not None
            and pendencia_existente.get("plano_futuro") == "free"
            and _to_datetime_utc_naive(pendencia_existente.get("efetivar_em")) is not None
            and _to_datetime_utc_naive(pendencia_existente.get("efetivar_em")) > agora
        ):
            mudanca_pendente = True
            plano_pendente = "free"
            efetivar_em = pendencia_existente.get("efetivar_em")
        else:
            if vinculo_ativo_conta is not None:
                _limpar_mudanca_pendente_vinculo(vinculo_ativo_conta)
                registrar_fato_monetizacao(
                    tipo_fato="stripe_subscription_deleted_pendencia_limpa",
                    status_tecnico=STATUS_TEC_APLICADO,
                    provider=PROVIDER_STRIPE,
                    conta_id=int(fr.conta_id),
                    franquia_id=int(fr.id),
                    customer_id=_norm_text(vinculo_ativo_conta.customer_id),
                    subscription_id=_norm_text(vinculo_ativo_conta.subscription_id),
                    price_id=_norm_text(vinculo_ativo_conta.price_id),
                    idempotency_key=(
                        f"stripe_subscription_deleted_pendencia_limpa:{fr.conta_id}:{fr.id}:"
                        f"{_norm_text(vinculo_ativo_conta.subscription_id) or '-'}"
                    )[:190],
                    snapshot_normalizado={
                        "event_type": "customer.subscription.deleted",
                        "mudanca_pendente_removida": True,
                    },
                    payload_bruto_sanitizado={"origem": "aplicar_fato_contratual_em_franquia"},
                )
            if _aplicar_plano_operacional_franquia(fr, "free"):
                alterou = True
            novo_inicio_free = ciclo.get("fim_ciclo") or ciclo.get("inicio_ciclo") or agora
            if fr.inicio_ciclo != novo_inicio_free:
                fr.inicio_ciclo = novo_inicio_free
                alterou = True
            if fr.fim_ciclo is not None:
                fr.fim_ciclo = None
                alterou = True
            consumo_atual = Decimal(str(fr.consumo_acumulado or "0"))
            if consumo_atual != Decimal("0"):
                fr.consumo_acumulado = Decimal("0")
                consumo_reiniciado = True
                alterou = True
            free_efetivado_no_deleted = True
            registrar_fato_monetizacao(
                tipo_fato="stripe_subscription_deleted_free_efetivado",
                status_tecnico=STATUS_TEC_APLICADO,
                provider=PROVIDER_STRIPE,
                conta_id=int(fr.conta_id),
                franquia_id=int(fr.id),
                customer_id=_norm_text(vinculo_ativo_conta.customer_id if vinculo_ativo_conta else None),
                subscription_id=_norm_text(
                    vinculo_ativo_conta.subscription_id if vinculo_ativo_conta else None
                ),
                price_id=_norm_text(vinculo_ativo_conta.price_id if vinculo_ativo_conta else None),
                idempotency_key=(
                    f"stripe_subscription_deleted_free_efetivado:{fr.conta_id}:{fr.id}:"
                    f"{_norm_text(vinculo_ativo_conta.subscription_id if vinculo_ativo_conta else None) or '-'}"
                )[:190],
                snapshot_normalizado={
                    "event_type": "customer.subscription.deleted",
                    "free_efetivado": True,
                    "inicio_ciclo": novo_inicio_free.isoformat() if isinstance(novo_inicio_free, datetime) else None,
                    "fim_ciclo": None,
                    "consumo_zerado": True,
                },
                payload_bruto_sanitizado={"origem": "aplicar_fato_contratual_em_franquia"},
            )
    elif downgrade_pago_ja_vigente:
        if vinculo_ativo_conta is not None:
            _limpar_mudanca_pendente_vinculo(vinculo_ativo_conta)
        if _aplicar_plano_operacional_franquia(fr, str(plano_n)):
            alterou = True
    elif downgrade_para_plano_pago:
        # Downgrade para plano pago inferior (ex.: Pro -> Starter): nunca aplicar operacional
        # imediato quando faltar data no evento; mantem a mesma ordem Stripe do resolver quando ha datas.
        efetivar_dt = _resolver_data_efetivacao_downgrade(
            ciclo_fim_evento=ciclo_fim_evento,
            vigencia_externa_fim=vigencia_externa_fim,
            fim_ciclo_local=fr.fim_ciclo,
        )
        if efetivar_dt is None:
            efetivar_dt = fr.fim_ciclo or vigencia_externa_fim or ciclo_fim_evento
        if efetivar_dt is None and vinculo_ativo_conta is not None:
            pend_prev = _extrair_pendencia_downgrade_snapshot(
                _json_loads(vinculo_ativo_conta.snapshot_normalizado_json)
            )
            if pend_prev is not None and pend_prev.get("plano_futuro") == plano_n:
                efetivar_dt = _to_datetime_utc_naive(pend_prev.get("efetivar_em"))
        if efetivar_dt is None:
            res_ciclo = garantir_ciclo_operacional_franquia(fr.id)
            efetivar_dt = res_ciclo.fim_ciclo
        if efetivar_dt is not None and efetivar_dt <= agora:
            refut = None
            if fr.fim_ciclo is not None and fr.fim_ciclo > agora:
                refut = fr.fim_ciclo
            elif vigencia_externa_fim is not None and vigencia_externa_fim > agora:
                refut = vigencia_externa_fim
            efetivar_dt = refut
        if efetivar_dt is not None and efetivar_dt > agora:
            snapshot_pendente = _registrar_mudanca_pendente_vinculo(
                conta_id=int(fr.conta_id),
                plano_futuro=plano_n,
                efetivar_em=efetivar_dt,
                origem=MUDANCA_PENDENTE_ORIGEM_USUARIO,
            )
            mudanca_pendente = True
            plano_pendente = plano_n
            efetivar_em = snapshot_pendente.get("efetivar_em")
    elif (
        plano_n in {"free", "starter", "pro"}
        and _rank_plano(plano_n) < _rank_plano(plano_atual)
        and _resolver_data_efetivacao_downgrade(
            ciclo_fim_evento=ciclo_fim_evento,
            vigencia_externa_fim=vigencia_externa_fim,
            fim_ciclo_local=fr.fim_ciclo,
        )
        is not None
    ):
        efetivar_dt = _resolver_data_efetivacao_downgrade(
            ciclo_fim_evento=ciclo_fim_evento,
            vigencia_externa_fim=vigencia_externa_fim,
            fim_ciclo_local=fr.fim_ciclo,
        )
        if efetivar_dt is None or efetivar_dt <= agora:
            efetivar_dt = None
        if efetivar_dt is not None:
            snapshot_pendente = _registrar_mudanca_pendente_vinculo(
                conta_id=int(fr.conta_id),
                plano_futuro=plano_n,
                efetivar_em=efetivar_dt,
                origem=MUDANCA_PENDENTE_ORIGEM_USUARIO,
            )
            mudanca_pendente = True
            plano_pendente = plano_n
            efetivar_em = snapshot_pendente.get("efetivar_em")
    elif plano_n in {"free", "starter", "pro"}:
        if not (eh_downgrade and downgrade_para_plano_pago) and _aplicar_plano_operacional_franquia(
            fr, plano_n
        ):
            alterou = True

    preservar_ciclo_franquia_consumo_invoice_downgrade_pago_pendente = (
        mudanca_pendente
        and (event_type or "").strip().lower() == "invoice.paid"
        and _normalizar_plano_codigo(plano_pendente) not in (None, "free")
    )

    inicio = ciclo.get("inicio_ciclo")
    fim = ciclo.get("fim_ciclo")
    if (
        (event_type or "").strip().lower() in ("invoice.paid", "customer.subscription.updated")
        and inicio is not None
        and fim is not None
        and fr.fim_ciclo is not None
        and fim <= fr.fim_ciclo
    ):
        # Evento fora de ordem/replay tardio: evita retroceder ciclo operacional.
        inicio = None
        fim = None
        ciclo_ignorado_evento_antigo = True
    event_type_l = (event_type or "").strip().lower()
    ciclo_renovado = bool(
        event_type_l in ("invoice.paid", "customer.subscription.updated")
        and inicio is not None
        and fim is not None
        and (fr.inicio_ciclo != inicio or fr.fim_ciclo != fim)
        and not preservar_ciclo_franquia_consumo_invoice_downgrade_pago_pendente
    )
    if (
        inicio is not None
        and fim is not None
        and not preservar_ciclo_franquia_consumo_invoice_downgrade_pago_pendente
        and not free_efetivado_no_deleted
    ):
        if fr.inicio_ciclo != inicio:
            fr.inicio_ciclo = inicio
            alterou = True
        if fr.fim_ciclo != fim:
            fr.fim_ciclo = fim
            alterou = True
        if ciclo_renovado:
            consumo_atual = Decimal(str(fr.consumo_acumulado or "0"))
            if consumo_atual != Decimal("0"):
                fr.consumo_acumulado = Decimal("0")
                consumo_reiniciado = True
                alterou = True
    elif event_type == "customer.subscription.deleted" and not free_efetivado_no_deleted:
        if fim is not None and (fr.fim_ciclo is None or fim > fr.fim_ciclo):
            fr.fim_ciclo = fim
            alterou = True
    elif ciclo.get("fonte_ciclo") == "fallback_interno_excecao_controlada":
        # Sem ciclo Stripe confiavel: mantem fallback legado explicitamente como excecao.
        ciclo_fallback = garantir_ciclo_operacional_franquia(fr.id)
        if fr.inicio_ciclo != ciclo_fallback.inicio_ciclo:
            fr.inicio_ciclo = ciclo_fallback.inicio_ciclo
            alterou = True
        if fr.fim_ciclo != ciclo_fallback.fim_ciclo:
            fr.fim_ciclo = ciclo_fallback.fim_ciclo
            alterou = True

    # invoice.payment_failed: reflexo operacional explicito sem bloqueio imediato.
    # Politica: manter ciclo/limite atuais e reavaliar status no trilho oficial Cleiton.
    forcar_reavaliacao = event_type == "invoice.payment_failed"
    if forcar_reavaliacao:
        politica_payment_failed = "nao_bloqueio_imediato_reavaliacao_operacional"

    # Status contratual externo nao escreve diretamente status da Franquia;
    # a reclassificacao operacional fica no trilho central do Cleiton.
    if forcar_reavaliacao or (
        status_contratual_externo and status_contratual_externo.strip().lower() in {
        "active",
        "trialing",
        "past_due",
        "unpaid",
        "canceled",
    }):
        db.session.add(fr)
        db.session.flush()
        alterar_status = True
    else:
        alterar_status = alterou
        db.session.add(fr)
        db.session.flush()

    if alterou:
        db.session.commit()

    efeito_operacional_aplicado = bool(alterou or consumo_reiniciado or forcar_reavaliacao)
    if mudanca_pendente:
        efeito_operacional_aplicado = False

    if alterar_status:
        aplicar_status_apos_mudanca_estrutural(fr.id)
        fr_refresh = db.session.get(Franquia, int(franquia_id))
        if free_efetivado_no_deleted and fr_refresh is not None and fr_refresh.fim_ciclo is not None:
            registrar_fato_monetizacao(
                tipo_fato="stripe_subscription_deleted_free_inconsistente_pos_status",
                status_tecnico=STATUS_TEC_SEM_EFEITO,
                provider=PROVIDER_STRIPE,
                conta_id=int(fr.conta_id),
                franquia_id=int(fr.id),
                customer_id=_norm_text(vinculo_ativo_conta.customer_id if vinculo_ativo_conta else None),
                subscription_id=_norm_text(
                    vinculo_ativo_conta.subscription_id if vinculo_ativo_conta else None
                ),
                price_id=_norm_text(vinculo_ativo_conta.price_id if vinculo_ativo_conta else None),
                idempotency_key=(
                    f"stripe_subscription_deleted_free_inconsistente:{fr.conta_id}:{fr.id}:"
                    f"{_norm_text(vinculo_ativo_conta.subscription_id if vinculo_ativo_conta else None) or '-'}"
                )[:190],
                snapshot_normalizado={
                    "event_type": "customer.subscription.deleted",
                    "motivo": "fim_ciclo_persistiu_preenchido_apos_reclassificacao",
                    "fim_ciclo": fr_refresh.fim_ciclo.isoformat(),
                },
                payload_bruto_sanitizado={"origem": "aplicar_fato_contratual_em_franquia"},
            )
        return {
            "aplicado": efeito_operacional_aplicado,
            "consumo_reiniciado_renovacao": consumo_reiniciado,
            "status_operacional_resultante": (
                fr_refresh.status if fr_refresh is not None else None
            ),
            "politica_payment_failed": politica_payment_failed,
            "ciclo_ignorado_evento_antigo": ciclo_ignorado_evento_antigo,
            "mudanca_pendente": mudanca_pendente,
            "plano_pendente": plano_pendente,
            "efetivar_em": efetivar_em,
        }
    return {
        "aplicado": False,
        "consumo_reiniciado_renovacao": consumo_reiniciado,
        "status_operacional_resultante": fr.status,
        "politica_payment_failed": politica_payment_failed,
        "ciclo_ignorado_evento_antigo": ciclo_ignorado_evento_antigo,
        "mudanca_pendente": mudanca_pendente,
        "plano_pendente": plano_pendente,
        "efetivar_em": efetivar_em,
    }


def _sincronizar_categoria_usuarios_franquia(
    franquia_id: int,
    plano_codigo: str | None,
) -> bool:
    plano_n = (plano_codigo or "").strip().lower()
    logging.info(
        "[StripeDebug][Categoria] Entrada sincronizar_categoria franquia_id=%s plano_n=%s",
        franquia_id,
        plano_n,
    )
    if plano_n not in {"free", "starter", "pro"}:
        return False
    alterou = False
    usuarios = User.query.filter(User.franquia_id == int(franquia_id)).all()
    logging.info(
        "[StripeDebug][Categoria] Usuarios encontrados franquia_id=%s user_ids=%s",
        franquia_id,
        [getattr(user, "id", None) for user in usuarios],
    )
    for user in usuarios:
        categoria_antes = (user.categoria or "").strip().lower()
        if categoria_antes == plano_n:
            continue
        user.categoria = plano_n
        db.session.add(user)
        logging.info(
            "[StripeDebug][Categoria] Usuario categoria alterada user_id=%s categoria_antes=%s categoria_depois=%s",
            getattr(user, "id", None),
            categoria_antes,
            plano_n,
        )
        alterou = True
    return alterou


def obter_projecao_auditoria_monetizacao(conta_id: int) -> dict[str, Any]:
    fatos_recentes = (
        MonetizacaoFato.query.filter(MonetizacaoFato.conta_id == int(conta_id))
        .order_by(MonetizacaoFato.timestamp_interno.desc(), MonetizacaoFato.id.desc())
        .limit(50)
        .all()
    )
    pendentes = [
        f
        for f in fatos_recentes
        if (f.status_tecnico or "").strip().lower() == STATUS_TEC_PENDENTE_CORRELACAO
    ]
    ultimo_com_efeito = next(
        (
            f
            for f in fatos_recentes
            if (f.status_tecnico or "").strip().lower() == STATUS_TEC_APLICADO
        ),
        None,
    )
    ciclo_ultimo = {}
    if ultimo_com_efeito is not None:
        snap = _json_loads(ultimo_com_efeito.snapshot_normalizado_json)
        ciclo_ultimo = dict((snap.get("ciclo_contratual") or {}))
    return {
        "pendencias_correlacao_qtd": len(pendentes),
        "pendencias_correlacao_eventos": [
            {
                "fato_id": f.id,
                "external_event_id": f.external_event_id,
                "tipo_fato": f.tipo_fato,
                "timestamp_interno": f.timestamp_interno,
                "pendencias_correlacao": list(
                    (_json_loads(f.snapshot_normalizado_json).get("correlacao") or {}).get(
                        "pendencias"
                    )
                    or []
                ),
                "reprocessavel_admin": True,
            }
            for f in pendentes[:10]
        ],
        "ultimo_evento_com_efeito": (
            {
                "external_event_id": ultimo_com_efeito.external_event_id,
                "tipo_fato": ultimo_com_efeito.tipo_fato,
                "timestamp_interno": ultimo_com_efeito.timestamp_interno,
            }
            if ultimo_com_efeito is not None
            else None
        ),
        "ultimo_ciclo_contratual": ciclo_ultimo or None,
    }


def reprocessar_pendencias_correlacao_por_conta_admin(
    *,
    conta_id: int,
    franquia_id_contexto: int | None = None,
    admin_user_id: int | None = None,
    limite: int = 20,
) -> dict[str, Any]:
    conta_id_i = int(conta_id)
    limite_i = max(1, min(100, int(limite)))
    candidatos = (
        MonetizacaoFato.query.filter(
            MonetizacaoFato.provider == PROVIDER_STRIPE,
            MonetizacaoFato.status_tecnico == STATUS_TEC_PENDENTE_CORRELACAO,
        )
        .order_by(MonetizacaoFato.timestamp_interno.asc(), MonetizacaoFato.id.asc())
        .limit(max(limite_i * 10, 200))
        .all()
    )
    pendentes: list[MonetizacaoFato] = []
    franquia_id_ctx = (
        int(franquia_id_contexto) if franquia_id_contexto is not None else None
    )
    for fato in candidatos:
        if fato.conta_id is not None and int(fato.conta_id) == conta_id_i:
            pendentes.append(fato)
            if len(pendentes) >= limite_i:
                break
            continue
        if fato.franquia_id is not None and franquia_id_ctx is not None:
            if int(fato.franquia_id) == franquia_id_ctx:
                pendentes.append(fato)
                if len(pendentes) >= limite_i:
                    break
                continue
        evento = _json_loads(fato.payload_bruto_sanitizado_json)
        obj = ((evento.get("data") or {}).get("object") or {}) if isinstance(evento, dict) else {}
        metadata = _metadata_from_event_object(obj) if isinstance(obj, dict) else {}
        conta_metadata = _to_int_or_none(metadata.get("conta_id"))
        franquia_metadata = _to_int_or_none(metadata.get("franquia_id"))
        if conta_metadata is not None and int(conta_metadata) == conta_id_i:
            pendentes.append(fato)
        elif (
            franquia_id_ctx is not None
            and franquia_metadata is not None
            and int(franquia_metadata) == franquia_id_ctx
        ):
            pendentes.append(fato)
        if len(pendentes) >= limite_i:
            break
    resultados: list[dict[str, Any]] = []
    for fato in pendentes:
        resultados.append(
            reprocessar_fato_pendente_correlacao_admin(
                fato_id=int(fato.id),
                admin_user_id=admin_user_id,
                franquia_id_contexto=franquia_id_contexto,
            )
        )
    resolvidos = [r for r in resultados if not r.get("permanece_pendente", False)]
    return {
        "conta_id": conta_id_i,
        "franquia_id_contexto": franquia_id_contexto,
        "total_analisado": len(resultados),
        "total_resolvido": len(resolvidos),
        "total_permanece_pendente": len(resultados) - len(resolvidos),
        "resultados": resultados,
    }


def reprocessar_fato_pendente_correlacao_admin(
    *,
    fato_id: int,
    admin_user_id: int | None = None,
    franquia_id_contexto: int | None = None,
) -> dict[str, Any]:
    fato = db.session.get(MonetizacaoFato, int(fato_id))
    if fato is None:
        return {
            "ok": False,
            "fato_id": int(fato_id),
            "erro": "fato_nao_encontrado",
        }
    if (fato.status_tecnico or "").strip().lower() != STATUS_TEC_PENDENTE_CORRELACAO:
        return {
            "ok": True,
            "fato_id": fato.id,
            "status": "nao_reprocessado_status_incompativel",
            "status_tecnico_atual": fato.status_tecnico,
            "permanece_pendente": False,
        }
    if (fato.provider or "").strip().lower() != PROVIDER_STRIPE:
        return {
            "ok": False,
            "fato_id": fato.id,
            "erro": "provider_nao_suportado_para_reprocessamento",
        }
    evento = _json_loads(fato.payload_bruto_sanitizado_json)
    if not isinstance(evento, dict) or not evento:
        return {
            "ok": False,
            "fato_id": fato.id,
            "erro": "payload_evento_indisponivel_para_reprocessamento",
        }
    resultado = processar_evento_stripe(
        evento,
        reprocessamento_admin={
            "executado_por_admin_user_id": admin_user_id,
            "fato_id_origem": fato.id,
            "franquia_id_contexto": franquia_id_contexto,
        },
    )
    status_final = (resultado.get("status_tecnico") or "").strip().lower()
    return {
        "ok": True,
        "fato_id": fato.id,
        "event_id": resultado.get("event_id"),
        "event_type": resultado.get("event_type"),
        "status_tecnico_resultante": status_final,
        "efeito_operacional_aplicado": bool(resultado.get("efeito_operacional_aplicado")),
        "permanece_pendente": status_final == STATUS_TEC_PENDENTE_CORRELACAO,
    }


def _extrair_ids_externos_stripe(evento: dict[str, Any], objeto: dict[str, Any]) -> dict[str, Any]:
    obj_id = _norm_text(objeto.get("id"))
    event_type = (_norm_text(evento.get("type")) or "").lower()
    customer_id = _norm_text(objeto.get("customer"))
    subscription_id = _norm_text(objeto.get("subscription"))
    invoice_id = _norm_text(objeto.get("invoice"))
    price_id = None

    if event_type.startswith("invoice."):
        invoice_id = obj_id or invoice_id
        lines = (objeto.get("lines") or {}).get("data") if isinstance(objeto.get("lines"), dict) else None
        if isinstance(lines, list) and lines:
            first = lines[0] if isinstance(lines[0], dict) else {}
            price_id = _norm_text(((first.get("price") or {}).get("id")) if isinstance(first.get("price"), dict) else first.get("price"))
            if not subscription_id:
                for raw in lines:
                    sid_line = _subscription_id_from_invoice_line(
                        raw if isinstance(raw, dict) else {}
                    )
                    if sid_line:
                        subscription_id = sid_line
                        break
    elif event_type.startswith("customer.subscription."):
        subscription_id = obj_id or subscription_id
        items = (objeto.get("items") or {}).get("data") if isinstance(objeto.get("items"), dict) else None
        if isinstance(items, list) and items:
            first = items[0] if isinstance(items[0], dict) else {}
            price_id = _norm_text(((first.get("price") or {}).get("id")) if isinstance(first.get("price"), dict) else first.get("price"))
    elif event_type == "checkout.session.completed":
        if not invoice_id:
            invoice_id = _norm_text(objeto.get("invoice"))
        line_items = objeto.get("line_items")
        if isinstance(line_items, dict):
            data = line_items.get("data")
            if isinstance(data, list) and data:
                first = data[0] if isinstance(data[0], dict) else {}
                price = first.get("price")
                if isinstance(price, dict):
                    price_id = _norm_text(price.get("id"))

    return {
        "object_id": obj_id,
        "customer_id": customer_id,
        "subscription_id": subscription_id,
        "invoice_id": invoice_id,
        "price_id": price_id,
    }


_METADATA_CHAVES_CRITICAS_LINHA = ("usuario_id", "conta_id", "franquia_id", "plano_interno")


def _metadata_stripe_dict_limpo(candidate: Any) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        return {}
    return {str(k): v for k, v in candidate.items() if v not in (None, "")}


def _valor_normalizado_chave_critica_metadata(chave: str, valor: Any) -> Any:
    ck = (chave or "").strip().lower()
    if ck in {"usuario_id", "conta_id", "franquia_id"}:
        return _to_int_or_none(valor)
    if ck == "plano_interno":
        t = _norm_text(valor)
        return t.lower() if t else None
    return None


def _linha_invoice_tem_metadata_com_chave_critica(metadata: dict[str, Any]) -> bool:
    for ck in _METADATA_CHAVES_CRITICAS_LINHA:
        if _valor_normalizado_chave_critica_metadata(ck, metadata.get(ck)) is not None:
            return True
    return False


def _metadata_linhas_invoice_seguro(lines: list[Any]) -> dict[str, Any]:
    """
    Nao concatena metadata de todas as linhas. Uma linha util isolada exporta o dict completo
    daquela linha; varias linhas exigem consenso nos campos criticos, senao a camada e descartada.
    """
    metadatas_linhas: list[dict[str, Any]] = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        bruto = line.get("metadata")
        if not isinstance(bruto, dict) or not bruto:
            continue
        limpo = _metadata_stripe_dict_limpo(bruto)
        if not limpo or not _linha_invoice_tem_metadata_com_chave_critica(limpo):
            continue
        metadatas_linhas.append(limpo)
    if not metadatas_linhas:
        return {}
    if len(metadatas_linhas) == 1:
        return dict(metadatas_linhas[0])
    for ck in _METADATA_CHAVES_CRITICAS_LINHA:
        valores: set[Any] = set()
        for md in metadatas_linhas:
            nv = _valor_normalizado_chave_critica_metadata(ck, md.get(ck))
            if nv is not None:
                valores.add(nv)
        if len(valores) > 1:
            logger.info(
                "[Stripe][MetadataInvoice] Metadata critico divergente entre linhas; "
                "ignorando camada lines.data[*].metadata."
            )
            return {}
    out: dict[str, Any] = {}
    for ck in _METADATA_CHAVES_CRITICAS_LINHA:
        for md in metadatas_linhas:
            if md.get(ck) in (None, ""):
                continue
            if _valor_normalizado_chave_critica_metadata(ck, md.get(ck)) is None:
                continue
            out[ck] = md[ck]
            break
    return out


def _metadata_from_event_object(objeto: dict[str, Any]) -> dict[str, Any]:
    """
    Unifica metadata de objetos Stripe (ex.: Invoice) com precedencia explicita.

    Precedencia (da mais fraca para a mais forte; cada camada sobrescreve chaves da anterior):
      1) lines.data[*].metadata — apenas se uma unica linha util ou consenso nos campos
         criticos (usuario_id, conta_id, franquia_id, plano_interno); conflito descarta a camada.
      2) parent.subscription_details.metadata
      3) subscription_details.metadata (no objeto raiz, ex. invoice)
      4) objeto.metadata — vence em empate (fonte preferida do comercio no objeto)
    """
    lines = (objeto.get("lines") or {}).get("data") if isinstance(objeto.get("lines"), dict) else None
    linhas_meta: dict[str, Any] = {}
    if isinstance(lines, list):
        linhas_meta = _metadata_linhas_invoice_seguro(lines)

    parent_meta: dict[str, Any] = {}
    parent_obj = objeto.get("parent")
    if isinstance(parent_obj, dict):
        sd_parent = parent_obj.get("subscription_details")
        if isinstance(sd_parent, dict):
            parent_meta = _metadata_stripe_dict_limpo(sd_parent.get("metadata"))

    subdet_meta: dict[str, Any] = {}
    subdet = objeto.get("subscription_details")
    if isinstance(subdet, dict):
        subdet_meta = _metadata_stripe_dict_limpo(subdet.get("metadata"))

    objeto_meta = _metadata_stripe_dict_limpo(objeto.get("metadata"))

    mesclado: dict[str, Any] = {}
    for camada in (linhas_meta, parent_meta, subdet_meta, objeto_meta):
        for k, v in camada.items():
            if v not in (None, ""):
                mesclado[k] = v
    return mesclado


def _resolver_correlacao_evento(
    *,
    evento: dict[str, Any],
    object_data: dict[str, Any],
    ids: dict[str, Any],
) -> dict[str, Any]:
    metadata = _metadata_from_event_object(object_data)
    conta_id_metadata = _to_int_or_none(metadata.get("conta_id"))
    franquia_id_metadata = _to_int_or_none(metadata.get("franquia_id"))
    conta_id = conta_id_metadata
    franquia_id = franquia_id_metadata
    usuario_id = _to_int_or_none(metadata.get("usuario_id"))
    client_reference_id = _norm_text(object_data.get("client_reference_id"))
    vinculou_por_ids = False
    franquia_multiplas_na_conta = False

    if conta_id is None:
        if client_reference_id:
            chunks = client_reference_id.split(":")
            if len(chunks) >= 4 and chunks[0] == "conta" and chunks[2] == "franquia":
                conta_id = _to_int_or_none(chunks[1])
                franquia_id = franquia_id or _to_int_or_none(chunks[3])

    if conta_id is None:
        vinculo = _buscar_vinculo_por_ids_externos(
            customer_id=ids.get("customer_id"),
            subscription_id=ids.get("subscription_id"),
        )
        if vinculo is not None:
            vinculou_por_ids = True
            conta_id = int(vinculo.conta_id)
            snap = _json_loads(vinculo.snapshot_normalizado_json)
            franquia_id = franquia_id or _to_int_or_none(snap.get("franquia_id"))

    if conta_id is not None and franquia_id is None:
        franquia_id = _resolver_franquia_unica_da_conta(conta_id)
        if franquia_id is None:
            rows_franquia = (
                Franquia.query.filter(Franquia.conta_id == int(conta_id))
                .order_by(Franquia.id.asc())
                .limit(2)
                .all()
            )
            franquia_multiplas_na_conta = len(rows_franquia) > 1

    correlacao_inequivoca = conta_id is not None and franquia_id is not None
    pendencias: list[str] = []
    if conta_id is None:
        if conta_id_metadata is None and not client_reference_id and not ids.get("subscription_id") and not ids.get("customer_id"):
            pendencias.append("conta_id_ausente_em_metadata_client_reference_e_ids_externos")
        elif conta_id_metadata is None and not client_reference_id:
            pendencias.append("conta_id_ausente_em_metadata_e_client_reference")
        elif not vinculou_por_ids:
            pendencias.append("conta_id_nao_correlacionado_por_vinculo_externo")
        pendencias.append("conta_id_nao_correlacionado")
    if franquia_id is None:
        if conta_id is not None and franquia_multiplas_na_conta:
            pendencias.append("conta_com_multiplas_franquias_sem_correlacao_inequivoca")
        elif franquia_id_metadata is None:
            pendencias.append("franquia_id_ausente_em_metadata")
        pendencias.append("franquia_id_nao_correlacionado")
    return {
        "conta_id": conta_id,
        "franquia_id": franquia_id,
        "usuario_id": usuario_id,
        "correlacao_inequivoca": correlacao_inequivoca,
        "pendencias": pendencias,
    }


def _resolver_franquia_unica_da_conta(conta_id: int) -> int | None:
    rows = (
        Franquia.query.filter(Franquia.conta_id == int(conta_id))
        .order_by(Franquia.id.asc())
        .limit(2)
        .all()
    )
    if len(rows) == 1:
        return int(rows[0].id)
    return None


def _buscar_vinculo_por_ids_externos(
    *,
    customer_id: str | None,
    subscription_id: str | None,
) -> ContaMonetizacaoVinculo | None:
    if subscription_id:
        row = (
            ContaMonetizacaoVinculo.query.filter_by(
                provider=PROVIDER_STRIPE,
                subscription_id=subscription_id,
                ativo=True,
            )
            .order_by(ContaMonetizacaoVinculo.updated_at.desc(), ContaMonetizacaoVinculo.id.desc())
            .first()
        )
        if row is not None:
            return row
    if customer_id:
        row = (
            ContaMonetizacaoVinculo.query.filter_by(
                provider=PROVIDER_STRIPE,
                customer_id=customer_id,
                ativo=True,
            )
            .order_by(ContaMonetizacaoVinculo.updated_at.desc(), ContaMonetizacaoVinculo.id.desc())
            .first()
        )
        if row is not None:
            return row
    return None


def _resolver_plano_interno_evento(
    *,
    object_data: dict[str, Any],
    ids: dict[str, Any],
    correlacao: dict[str, Any],
) -> str | None:
    metadata = _metadata_from_event_object(object_data)
    plano_metadata = _norm_text(metadata.get("plano_interno"))
    if plano_metadata:
        return plano_metadata.lower()

    from_price = plano_service.resolver_plano_por_gateway_price_id_admin(
        provider=PROVIDER_STRIPE,
        price_id=ids.get("price_id"),
    )
    if from_price:
        return (from_price.get("plano_codigo") or "").strip().lower() or None

    conta_id = correlacao.get("conta_id")
    if conta_id is not None:
        vinculo_ativo = (
            ContaMonetizacaoVinculo.query.filter_by(
                conta_id=int(conta_id),
                provider=PROVIDER_STRIPE,
                ativo=True,
            )
            .order_by(ContaMonetizacaoVinculo.updated_at.desc(), ContaMonetizacaoVinculo.id.desc())
            .first()
        )
        if vinculo_ativo is not None and _norm_text(vinculo_ativo.plano_interno):
            return _norm_text(vinculo_ativo.plano_interno).lower()
    return None


def _resolver_status_contratual_evento(*, event_type: str, object_data: dict[str, Any]) -> str | None:
    status_obj = _norm_text(object_data.get("status"))
    if event_type == "invoice.paid":
        return "paid"
    if event_type == "invoice.payment_failed":
        return "payment_failed"
    if event_type == "checkout.session.completed":
        return status_obj or "checkout_completed"
    if event_type == "customer.subscription.deleted":
        return status_obj or "canceled"
    if event_type == "customer.subscription.updated":
        return status_obj or "updated"
    return status_obj


def _resolver_ciclo_evento_stripe(
    *,
    event_type: str,
    object_data: dict[str, Any],
) -> dict[str, Any]:
    """
    Prioridade de ciclo:
    1) periodo contratual confirmado Stripe
    2) proxima cobranca confiavel (derivacao controlada)
    3) fallback interno (excecao controlada)
    """
    pendencias: list[str] = []

    # invoice.paid: principal fonte de ativacao/renovacao
    if event_type == "invoice.paid":
        lines = (object_data.get("lines") or {}).get("data") if isinstance(object_data.get("lines"), dict) else None
        if isinstance(lines, list) and lines:
            first = lines[0] if isinstance(lines[0], dict) else {}
            period = first.get("period") if isinstance(first, dict) else {}
            start = _to_datetime_utc_naive((period or {}).get("start"))
            end = _to_datetime_utc_naive((period or {}).get("end"))
            if start is not None and end is not None:
                return {
                    "inicio_ciclo": start,
                    "fim_ciclo": end,
                    "fonte_ciclo": "stripe_periodo_contratual_confirmado",
                    "pendencias": pendencias,
                }
        start = _to_datetime_utc_naive(object_data.get("period_start"))
        end = _to_datetime_utc_naive(object_data.get("period_end"))
        if start is not None and end is not None:
            return {
                "inicio_ciclo": start,
                "fim_ciclo": end,
                "fonte_ciclo": "stripe_periodo_fatura_confirmado",
                "pendencias": pendencias,
            }
        next_attempt = _to_datetime_utc_naive(object_data.get("next_payment_attempt"))
        if next_attempt is not None:
            return {
                "inicio_ciclo": next_attempt - timedelta(days=30),
                "fim_ciclo": next_attempt,
                "fonte_ciclo": "stripe_proxima_cobranca_derivada_controlada",
                "pendencias": ["ciclo_derivado_por_proxima_cobranca_confiavel"],
            }
        pendencias.append("ciclo_stripe_insuficiente_invoice_paid")
        return {
            "inicio_ciclo": None,
            "fim_ciclo": None,
            "fonte_ciclo": "fallback_interno_excecao_controlada",
            "pendencias": pendencias,
        }

    if event_type in {"customer.subscription.updated", "customer.subscription.deleted"}:
        start = _to_datetime_utc_naive(object_data.get("current_period_start"))
        end = _to_datetime_utc_naive(object_data.get("current_period_end"))
        if start is not None and end is not None:
            return {
                "inicio_ciclo": start,
                "fim_ciclo": end,
                "fonte_ciclo": "stripe_periodo_assinatura_confirmado",
                "pendencias": pendencias,
            }
        cancel_at = _to_datetime_utc_naive(object_data.get("cancel_at"))
        if cancel_at is not None:
            return {
                "inicio_ciclo": None,
                "fim_ciclo": cancel_at,
                "fonte_ciclo": "stripe_encerramento_assinatura_confirmado",
                "pendencias": ["inicio_ciclo_nao_informado_pelo_evento"],
            }
        pendencias.append("ciclo_stripe_insuficiente_subscription_event")
        return {
            "inicio_ciclo": None,
            "fim_ciclo": None,
            "fonte_ciclo": "fallback_interno_excecao_controlada",
            "pendencias": pendencias,
        }

    # checkout.session.completed e invoice.payment_failed nao alteram ciclo operacional diretamente
    return {
        "inicio_ciclo": None,
        "fim_ciclo": None,
        "fonte_ciclo": "sem_alteracao_ciclo_por_tipo_evento",
        "pendencias": pendencias,
    }


def efetivar_mudancas_pendentes_ciclo(*, agora: datetime | None = None, limite: int = 200) -> dict[str, Any]:
    referencia = agora or utcnow_naive()
    avaliados = 0
    efetivados = 0
    erros = 0
    vinculos = (
        ContaMonetizacaoVinculo.query.filter_by(provider=PROVIDER_STRIPE, ativo=True)
        .order_by(ContaMonetizacaoVinculo.updated_at.asc(), ContaMonetizacaoVinculo.id.asc())
        .limit(max(1, int(limite)))
        .all()
    )
    for vinculo in vinculos:
        snapshot = _json_loads(vinculo.snapshot_normalizado_json)
        if not snapshot.get("mudanca_pendente"):
            fatos_recentes = (
                MonetizacaoFato.query.filter(
                    MonetizacaoFato.conta_id == int(vinculo.conta_id),
                    MonetizacaoFato.provider == PROVIDER_STRIPE,
                )
                .order_by(MonetizacaoFato.timestamp_interno.desc(), MonetizacaoFato.id.desc())
                .limit(30)
                .all()
            )
            pendencia_historica = None
            for fato in fatos_recentes:
                snap_fato = _json_loads(fato.snapshot_normalizado_json)
                pend_fato = _extrair_pendencia_downgrade_snapshot(snap_fato)
                if pend_fato is not None:
                    pendencia_historica = {"fato_id": int(fato.id), "pendencia": pend_fato}
                    break
            if pendencia_historica is not None:
                registrar_fato_monetizacao(
                    tipo_fato="cleiton_cron_pendencia_ausente_no_vinculo_ativo",
                    status_tecnico=STATUS_TEC_SEM_EFEITO,
                    provider=PROVIDER_STRIPE,
                    conta_id=int(vinculo.conta_id),
                    franquia_id=None,
                    customer_id=_norm_text(vinculo.customer_id),
                    subscription_id=_norm_text(vinculo.subscription_id),
                    price_id=_norm_text(vinculo.price_id),
                    idempotency_key=(
                        f"cleiton_cron_pendencia_ausente:{vinculo.id}:{pendencia_historica['fato_id']}"
                    )[:190],
                    snapshot_normalizado={
                        "vinculo_id": int(vinculo.id),
                        "pendencia_historica_fato_id": pendencia_historica["fato_id"],
                        "pendencia_historica": pendencia_historica["pendencia"],
                    },
                    payload_bruto_sanitizado={"origem": "efetivar_mudancas_pendentes_ciclo"},
                )
            continue
        avaliados += 1
        if (snapshot.get("tipo_mudanca") or "").strip().lower() != "downgrade":
            continue
        plano_futuro = _normalizar_plano_codigo(snapshot.get("plano_futuro"))
        efetivar_em = _to_datetime_utc_naive(snapshot.get("efetivar_em"))
        if plano_futuro not in {"free", "starter"} or efetivar_em is None:
            erros += 1
            continue
        if efetivar_em > referencia:
            continue
        franquia = (
            Franquia.query.filter(Franquia.conta_id == int(vinculo.conta_id))
            .order_by(Franquia.id.asc())
            .first()
        )
        if franquia is None:
            erros += 1
            continue
        alterou = False
        plano_atual = _resolver_plano_operacional_atual(int(franquia.id))
        if plano_atual != plano_futuro and _aplicar_plano_operacional_franquia(franquia, plano_futuro):
            alterou = True
        if plano_futuro == "free":
            if franquia.inicio_ciclo != efetivar_em:
                franquia.inicio_ciclo = efetivar_em
                alterou = True
            if franquia.fim_ciclo is not None:
                franquia.fim_ciclo = None
                alterou = True
        elif plano_futuro == "starter":
            novo_inicio = efetivar_em
            novo_fim = _add_um_mes_mesmo_dia(efetivar_em)
            if franquia.inicio_ciclo != novo_inicio:
                franquia.inicio_ciclo = novo_inicio
                alterou = True
            if franquia.fim_ciclo != novo_fim:
                franquia.fim_ciclo = novo_fim
                alterou = True
        consumo_atual = Decimal(str(franquia.consumo_acumulado or "0"))
        if consumo_atual != Decimal("0"):
            franquia.consumo_acumulado = Decimal("0")
            alterou = True
        _limpar_mudanca_pendente_vinculo(vinculo)
        db.session.add(franquia)
        db.session.flush()
        aplicar_status_apos_mudanca_estrutural(franquia.id)
        registrar_fato_monetizacao(
            tipo_fato="cleiton_downgrade_efetivado_virada_ciclo",
            status_tecnico=STATUS_TEC_APLICADO if alterou else STATUS_TEC_SEM_EFEITO,
            provider=PROVIDER_STRIPE,
            conta_id=int(vinculo.conta_id),
            franquia_id=int(franquia.id),
            customer_id=_norm_text(vinculo.customer_id),
            subscription_id=_norm_text(vinculo.subscription_id),
            price_id=_norm_text(vinculo.price_id),
            idempotency_key=(
                f"cleiton_downgrade_virada:{vinculo.id}:{plano_futuro}:{efetivar_em.isoformat()}"
            ),
            snapshot_normalizado={
                "plano_futuro": plano_futuro,
                "efetivar_em": efetivar_em.isoformat(),
                "efetivado_em": referencia.isoformat(),
                "consumo_zerado": True,
            },
            payload_bruto_sanitizado={"origem": "rotina_periodica_cleiton"},
        )
        efetivados += 1
    if avaliados:
        db.session.commit()
    return {
        "ok": True,
        "avaliados": avaliados,
        "efetivados": efetivados,
        "erros": erros,
        "referencia": referencia.isoformat(),
    }


def obter_contexto_monetizacao_conta(
    conta_id: int,
    *,
    limite_fatos: int = 15,
) -> dict[str, Any]:
    conta_id_i = int(conta_id)
    vinculos = (
        ContaMonetizacaoVinculo.query.filter_by(conta_id=conta_id_i)
        .order_by(
            ContaMonetizacaoVinculo.ativo.desc(),
            ContaMonetizacaoVinculo.updated_at.desc(),
            ContaMonetizacaoVinculo.id.desc(),
        )
        .all()
    )
    vinculo_ativo = next((row for row in vinculos if row.ativo), None)
    fatos = (
        MonetizacaoFato.query.filter(MonetizacaoFato.conta_id == conta_id_i)
        .order_by(MonetizacaoFato.timestamp_interno.desc(), MonetizacaoFato.id.desc())
        .limit(max(1, int(limite_fatos)))
        .all()
    )

    return {
        "vinculo_comercial_externo_ativo": _vinculo_to_dict(vinculo_ativo),
        "vinculos_comerciais_historico": [_vinculo_to_dict(v) for v in vinculos[:10]],
        "fatos_monetizacao_recentes": [_fato_to_dict(f) for f in fatos],
        "auditoria_monetizacao": obter_projecao_auditoria_monetizacao(conta_id_i),
    }


def _vinculo_to_dict(vinculo: ContaMonetizacaoVinculo | None) -> dict[str, Any] | None:
    if vinculo is None:
        return None
    return {
        "id": vinculo.id,
        "conta_id": vinculo.conta_id,
        "provider": vinculo.provider,
        "customer_id": vinculo.customer_id,
        "subscription_id": vinculo.subscription_id,
        "price_id": vinculo.price_id,
        "plano_interno": vinculo.plano_interno,
        "status_contratual_externo": vinculo.status_contratual_externo,
        "vigencia_externa_inicio": vinculo.vigencia_externa_inicio,
        "vigencia_externa_fim": vinculo.vigencia_externa_fim,
        "ativo": vinculo.ativo,
        "snapshot_normalizado_json": vinculo.snapshot_normalizado_json,
        "snapshot_normalizado": _json_loads_nullable(vinculo.snapshot_normalizado_json),
        "payload_bruto_sanitizado_json": vinculo.payload_bruto_sanitizado_json,
        "payload_bruto_sanitizado": _json_loads_nullable(vinculo.payload_bruto_sanitizado_json),
        "created_at": vinculo.created_at,
        "updated_at": vinculo.updated_at,
        "desativado_em": vinculo.desativado_em,
    }


def _fato_to_dict(fato: MonetizacaoFato) -> dict[str, Any]:
    return {
        "id": fato.id,
        "tipo_fato": fato.tipo_fato,
        "status_tecnico": fato.status_tecnico,
        "idempotency_key": fato.idempotency_key,
        "correlation_key": fato.correlation_key,
        "timestamp_externo": fato.timestamp_externo,
        "timestamp_interno": fato.timestamp_interno,
        "provider": fato.provider,
        "conta_id": fato.conta_id,
        "franquia_id": fato.franquia_id,
        "usuario_id": fato.usuario_id,
        "external_event_id": fato.external_event_id,
        "customer_id": fato.customer_id,
        "subscription_id": fato.subscription_id,
        "price_id": fato.price_id,
        "invoice_id": fato.invoice_id,
        "identificadores_externos_json": fato.identificadores_externos_json,
        "identificadores_externos": _json_loads_nullable(fato.identificadores_externos_json),
        "snapshot_normalizado_json": fato.snapshot_normalizado_json,
        "snapshot_normalizado": _json_loads_nullable(fato.snapshot_normalizado_json),
        "payload_bruto_sanitizado_json": fato.payload_bruto_sanitizado_json,
        "payload_bruto_sanitizado": _json_loads_nullable(fato.payload_bruto_sanitizado_json),
    }


def _extrair_contador_reprocessamento(snapshot: dict[str, Any]) -> int:
    ctx = snapshot.get("reprocessamento_admin")
    if isinstance(ctx, dict):
        val = _to_int_or_none(ctx.get("tentativas"))
        if val is not None and val >= 0:
            return val
    return 0
