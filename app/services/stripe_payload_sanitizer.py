"""Projeção allowlist de objetos Stripe para persistência. Sem PII."""
from __future__ import annotations

from typing import Any

METADATA_TECNICA_ALLOWLIST = frozenset(
    {
        "conta_id",
        "franquia_id",
        "usuario_id",
        "plano_interno",
        "correlation_id",
        "checkout_intent_id",
        "quantity_solicitada",
        "fluxo_origem",
    }
)

_OBJETO_CHAVES = (
    "id",
    "object",
    "customer",
    "subscription",
    "invoice",
    "status",
    "payment_status",
    "mode",
    "currency",
    "quantity",
    "amount_due",
    "amount_paid",
    "amount_remaining",
    "current_period_start",
    "current_period_end",
    "period_start",
    "period_end",
    "expires_at",
    "created",
    "livemode",
    "billing_reason",
    "collection_method",
    "attempt_count",
)

_PRICE_CHAVES = (
    "id",
    "object",
    "active",
    "currency",
    "product",
    "type",
    "unit_amount",
    "unit_amount_decimal",
    "recurring",
    "billing_scheme",
)

_PII_NEVER = frozenset(
    {
        "email",
        "customer_email",
        "customer_details",
        "billing_details",
        "shipping",
        "shipping_details",
        "address",
        "phone",
        "name",
        "card",
        "payment_method",
        "payment_method_details",
        "payment_intent",
        "charges",
        "receipt_email",
        "receipt_number",
        "receipt_url",
        "bank_account",
        "sepa_debit",
        "au_becs_debit",
    }
)


def _norm_key(value: Any) -> str:
    return str(value or "").strip().lower()


def _metadata_tecnica(metadata: Any) -> dict[str, Any] | None:
    if not isinstance(metadata, dict):
        return None
    out = {
        str(k): v
        for k, v in metadata.items()
        if str(k).strip() in METADATA_TECNICA_ALLOWLIST and v not in (None, "")
    }
    return out or None


def _sanitizar_price(price: Any) -> Any:
    if isinstance(price, str):
        return price
    if not isinstance(price, dict):
        return None
    out: dict[str, Any] = {}
    for key in _PRICE_CHAVES:
        if key in price and price[key] not in (None, ""):
            val = price[key]
            if key == "product" and isinstance(val, dict):
                out[key] = val.get("id")
            elif key == "recurring" and isinstance(val, dict):
                out[key] = {
                    rk: val[rk]
                    for rk in ("interval", "interval_count", "usage_type")
                    if rk in val
                }
            else:
                out[key] = val
    return out or None


def _sanitizar_period(period: Any) -> dict[str, Any] | None:
    if not isinstance(period, dict):
        return None
    out = {}
    for key in ("start", "end"):
        if period.get(key) not in (None, ""):
            out[key] = period[key]
    return out or None


def _sanitizar_pricing(pricing: Any) -> dict[str, Any] | None:
    if not isinstance(pricing, dict):
        return None
    out: dict[str, Any] = {}
    if pricing.get("type") not in (None, ""):
        out["type"] = pricing.get("type")
    if pricing.get("unit_amount_decimal") not in (None, ""):
        out["unit_amount_decimal"] = pricing.get("unit_amount_decimal")
    details = pricing.get("price_details")
    if isinstance(details, dict):
        pd: dict[str, Any] = {}
        price = details.get("price")
        if isinstance(price, dict):
            if price.get("id") not in (None, ""):
                pd["price"] = price.get("id")
        elif price not in (None, ""):
            pd["price"] = price
        product = details.get("product")
        if isinstance(product, dict):
            if product.get("id") not in (None, ""):
                pd["product"] = product.get("id")
        elif product not in (None, ""):
            pd["product"] = product
        if pd:
            out["price_details"] = pd
    return out or None


def _sanitizar_parent(parent: Any) -> dict[str, Any] | None:
    if not isinstance(parent, dict):
        return None
    out: dict[str, Any] = {}
    if parent.get("type"):
        out["type"] = parent.get("type")
    details = parent.get("subscription_item_details")
    if isinstance(details, dict):
        det = {}
        for key in ("subscription", "subscription_item", "invoice_item", "proration"):
            if details.get(key) not in (None, ""):
                det[key] = details[key]
        if det:
            out["subscription_item_details"] = det
    sub_det = parent.get("subscription_details")
    if isinstance(sub_det, dict):
        sd: dict[str, Any] = {}
        if sub_det.get("subscription") not in (None, ""):
            sid = sub_det.get("subscription")
            sd["subscription"] = sid.get("id") if isinstance(sid, dict) else sid
        meta = _metadata_tecnica(sub_det.get("metadata"))
        if meta:
            sd["metadata"] = meta
        if sd:
            out["subscription_details"] = sd
    inv_det = parent.get("invoice_item_details")
    if isinstance(inv_det, dict) and inv_det.get("proration") is True:
        out["invoice_item_details"] = {"proration": True}
    return out or None


def _sanitizar_linha(linha: Any) -> dict[str, Any] | None:
    if not isinstance(linha, dict):
        return None
    out: dict[str, Any] = {}
    for key in ("id", "object", "type", "quantity", "proration", "subscription", "subscription_item"):
        if linha.get(key) not in (None, ""):
            out[key] = linha[key]
    price = _sanitizar_price(linha.get("price"))
    if price is not None:
        out["price"] = price
    pricing = _sanitizar_pricing(linha.get("pricing"))
    if pricing is not None:
        out["pricing"] = pricing
    period = _sanitizar_period(linha.get("period"))
    if period:
        out["period"] = period
    parent = _sanitizar_parent(linha.get("parent"))
    if parent:
        out["parent"] = parent
    meta = _metadata_tecnica(linha.get("metadata"))
    if meta:
        out["metadata"] = meta
    return out or None


def _sanitizar_item(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    out: dict[str, Any] = {}
    for key in ("id", "object", "quantity", "subscription"):
        if item.get(key) not in (None, ""):
            out[key] = item[key]
    price = _sanitizar_price(item.get("price"))
    if price is not None:
        out["price"] = price
    return out or None


def _sanitizar_lista(container: Any, item_fn) -> dict[str, Any] | list | None:
    if isinstance(container, list):
        itens = [item_fn(x) for x in container]
        return [x for x in itens if x]
    if not isinstance(container, dict):
        return None
    data = container.get("data")
    if not isinstance(data, list):
        return None
    itens = [item_fn(x) for x in data]
    return {"object": container.get("object") or "list", "data": [x for x in itens if x]}


def sanitizar_objeto_stripe(objeto: Any) -> dict[str, Any]:
    """Constrói dict novo por allowlist. Não copia o payload para depois apagar campos."""
    if not isinstance(objeto, dict):
        return {}
    out: dict[str, Any] = {}
    for key in _OBJETO_CHAVES:
        if key in _PII_NEVER:
            continue
        if objeto.get(key) not in (None, ""):
            val = objeto[key]
            if key in {"customer", "subscription", "invoice"} and isinstance(val, dict):
                out[key] = val.get("id") or sanitizar_objeto_stripe(val)
            else:
                out[key] = val
    meta = _metadata_tecnica(objeto.get("metadata"))
    if meta:
        out["metadata"] = meta
    price = _sanitizar_price(objeto.get("price"))
    if price is not None:
        out["price"] = price
    period = _sanitizar_period(objeto.get("period"))
    if period:
        out["period"] = period
    lines = _sanitizar_lista(objeto.get("lines"), _sanitizar_linha)
    if lines:
        out["lines"] = lines
    items = _sanitizar_lista(objeto.get("items"), _sanitizar_item)
    if items:
        out["items"] = items
    line_items = _sanitizar_lista(objeto.get("line_items"), _sanitizar_linha)
    if line_items:
        out["line_items"] = line_items
    parent = _sanitizar_parent(objeto.get("parent"))
    if parent:
        out["parent"] = parent
    if objeto.get("client_reference_id"):
        out["client_reference_id"] = objeto.get("client_reference_id")
    return out


def sanitizar_payload_stripe(payload: Any) -> dict[str, Any]:
    """
    Projeta evento ou objeto Stripe para persistência.
    Remove e-mail, endereço, billing details, cartão e metadata livre.
    """
    if not isinstance(payload, dict):
        return {}
    if payload.get("object") == "event" or "type" in payload and "data" in payload:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        objeto = data.get("object") if isinstance(data, dict) else {}
        return {
            "id": payload.get("id"),
            "object": payload.get("object") or "event",
            "type": payload.get("type"),
            "created": payload.get("created"),
            "livemode": payload.get("livemode"),
            "data": {"object": sanitizar_objeto_stripe(objeto)},
        }
    origem = payload.get("origem")
    if origem and len(payload) <= 3 and not any(_norm_key(k) in _PII_NEVER for k in payload):
        return dict(payload)
    return sanitizar_objeto_stripe(payload)


def payload_contem_pii_proibida(payload: Any) -> bool:
    texto = str(payload).lower() if payload is not None else ""
    marcadores = (
        "customer_email",
        "billing_details",
        "\"last4\"",
        "card last4",
        "shipping_details",
    )
    return any(m in texto for m in marcadores)
