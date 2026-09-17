"""Seleção inequívoca da linha contratual Multiuser em invoice Stripe."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


class LinhaContratualMultiuserAusenteError(ValueError):
    """Nenhuma linha contratual Multiuser inequívoca."""


class LinhaContratualMultiuserAmbiguoError(ValueError):
    """Duas ou mais linhas candidatas sem desambiguação."""


@dataclass(frozen=True)
class LinhaContratualMultiuser:
    price_id: str
    quantity: int
    inicio: datetime | None
    fim: datetime | None
    subscription_id: str | None
    subscription_item_id: str | None
    linha: dict[str, Any]


def _norm(value: Any) -> str | None:
    txt = str(value).strip() if value is not None else ""
    return txt or None


def _to_int(value: Any) -> int | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _price_id_da_linha(linha: dict[str, Any]) -> str | None:
    price = linha.get("price")
    if isinstance(price, dict):
        return _norm(price.get("id"))
    return _norm(price)


def subscription_id_da_linha(linha: dict[str, Any]) -> str | None:
    sid = _norm(linha.get("subscription"))
    if sid:
        return sid
    parent = linha.get("parent")
    if isinstance(parent, dict):
        details = parent.get("subscription_item_details")
        if isinstance(details, dict):
            sid = _norm(details.get("subscription"))
            if sid:
                return sid
    return None


def subscription_item_id_da_linha(linha: dict[str, Any]) -> str | None:
    item = _norm(linha.get("subscription_item"))
    if item:
        return item
    parent = linha.get("parent")
    if isinstance(parent, dict):
        details = parent.get("subscription_item_details")
        if isinstance(details, dict):
            item = _norm(details.get("subscription_item"))
            if item:
                return item
    return None


def _eh_linha_ajuste_ou_proration(linha: dict[str, Any]) -> bool:
    if linha.get("proration") is True:
        return True
    tipo = (_norm(linha.get("type")) or "").lower()
    if tipo in {"invoiceitem", "invoice_item"}:
        return True
    return False


def _linhas_invoice(invoice: dict[str, Any]) -> list[dict[str, Any]]:
    lines = invoice.get("lines")
    if isinstance(lines, dict):
        data = lines.get("data")
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
    if isinstance(lines, list):
        return [x for x in lines if isinstance(x, dict)]
    return []


def _periodo_da_linha(linha: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
    from app.services.cleiton_monetizacao_service import _to_datetime_utc_naive

    period = linha.get("period") if isinstance(linha.get("period"), dict) else {}
    inicio = _to_datetime_utc_naive((period or {}).get("start"))
    fim = _to_datetime_utc_naive((period or {}).get("end"))
    return inicio, fim


def selecionar_linha_contratual_multiuser(
    invoice: dict[str, Any],
    *,
    price_id_esperado: str,
    subscription_id_esperado: str | None = None,
    subscription_item_id_esperado: str | None = None,
) -> LinhaContratualMultiuser:
    """
    Exige exatamente uma linha contratual inequívoca.
    Price, quantity e período saem dessa mesma linha.
    """
    price_esperado = _norm(price_id_esperado)
    if not price_esperado:
        raise LinhaContratualMultiuserAusenteError(
            "Price Multiuser esperado ausente para selecionar a linha da invoice."
        )
    candidatas: list[dict[str, Any]] = []
    for linha in _linhas_invoice(invoice):
        if _eh_linha_ajuste_ou_proration(linha):
            continue
        if _price_id_da_linha(linha) != price_esperado:
            continue
        sid = subscription_id_da_linha(linha)
        if subscription_id_esperado and sid and sid != _norm(subscription_id_esperado):
            continue
        item_id = subscription_item_id_da_linha(linha)
        if (
            subscription_item_id_esperado
            and item_id
            and item_id != _norm(subscription_item_id_esperado)
        ):
            continue
        candidatas.append(linha)

    if subscription_item_id_esperado and len(candidatas) > 1:
        filtradas = [
            c
            for c in candidatas
            if subscription_item_id_da_linha(c) == _norm(subscription_item_id_esperado)
        ]
        candidatas = filtradas

    if not candidatas:
        raise LinhaContratualMultiuserAusenteError(
            "Nenhuma linha contratual Multiuser inequívoca na invoice."
        )
    if len(candidatas) > 1:
        raise LinhaContratualMultiuserAmbiguoError(
            "Invoice Multiuser com linhas contratuais ambíguas."
        )

    linha = candidatas[0]
    price_id = _price_id_da_linha(linha)
    quantity = _to_int(linha.get("quantity"))
    if not price_id or quantity is None or quantity < 1:
        raise LinhaContratualMultiuserAusenteError(
            "Linha contratual Multiuser sem Price/quantity utilizáveis."
        )
    inicio, fim = _periodo_da_linha(linha)
    return LinhaContratualMultiuser(
        price_id=price_id,
        quantity=int(quantity),
        inicio=inicio,
        fim=fim,
        subscription_id=subscription_id_da_linha(linha) or _norm(subscription_id_esperado),
        subscription_item_id=subscription_item_id_da_linha(linha)
        or _norm(subscription_item_id_esperado),
        linha=linha,
    )


def subscription_item_multiuser_da_assinatura(
    subscription: dict[str, Any] | None,
    *,
    price_id_esperado: str,
) -> dict[str, Any] | None:
    if not isinstance(subscription, dict):
        return None
    items = subscription.get("items")
    data = items.get("data") if isinstance(items, dict) else items
    if not isinstance(data, list):
        return None
    price_esperado = _norm(price_id_esperado)
    achados = []
    for item in data:
        if not isinstance(item, dict):
            continue
        price = item.get("price")
        pid = _norm(price.get("id") if isinstance(price, dict) else price)
        if pid == price_esperado:
            achados.append(item)
    if len(achados) != 1:
        return None
    return achados[0]
