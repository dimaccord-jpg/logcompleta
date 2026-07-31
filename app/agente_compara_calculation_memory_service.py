"""
View model isolado da memória de cálculo do AgenteCompara.

Recebe o resultado unitário já calculado e monta um contrato JSON-safe
para apresentação no modal local. Não recalcula, não persiste e não
acessa sessão HTTP nem provedores externos de IA ou cobrança.
"""
from __future__ import annotations

import copy
import math
from typing import Any

CALCULATION_MEMORY_SCHEMA_VERSION = 1

MEMORY_STATUS_CALCULATED = "calculated"
MEMORY_STATUS_CALCULATED_WITH_WARNINGS = "calculated_with_warnings"
MEMORY_STATUS_INCOMPLETE = "incomplete"
MEMORY_STATUS_NOT_CALCULATED = "not_calculated"

_MEMORY_COMPLETE_STATUSES = frozenset(
    {
        MEMORY_STATUS_CALCULATED,
        MEMORY_STATUS_CALCULATED_WITH_WARNINGS,
    }
)

# Status de domínio que nunca montam memória de total.
_DOMAIN_LIKE_STATUSES = frozenset(
    {
        "missing_coverage_mapping",
        "ambiguous_coverage_mapping",
        "missing_freight_rule",
        "invalid_weight",
        "invalid_invoice_value",
        "unsupported_pricing_model",
    }
)

COMPONENT_ORDER = (
    "WEIGHT_FREIGHT",
    "FREIGHT_VALUE",
    "TOLL",
    "ACCESSORIAL",
    "IGNORED_ACCESSORIAL",
    "SUBTOTAL",
    "TAX",
    "TOTAL",
)

_COMPONENT_RANK = {code: index for index, code in enumerate(COMPONENT_ORDER)}

_MONEY_TOLERANCE = 0.005

_FORBIDDEN_EVIDENCE_KEYS = frozenset(
    {
        "path",
        "storage_path",
        "storage_key",
        "result_storage_key",
        "checksum",
        "result_checksum",
        "fingerprint",
        "request_fingerprint",
        "prompt",
        "stack",
        "stack_trace",
        "traceback",
        "exception",
    }
)


class CalculationMemoryTotalMismatchError(ValueError):
    """Total da memória diverge do frete calculado da célula."""


def _is_finite_number(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number)


def _safe_money(value: Any) -> float | None:
    if value is None:
        return None
    if not _is_finite_number(value):
        return None
    return round(float(value), 2)


def _safe_number(value: Any) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if not _is_finite_number(value):
        return None
    number = float(value)
    if number.is_integer():
        return int(number)
    return number


def _safe_text(value: Any, *, max_chars: int = 500) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    lowered = text.lower()
    # Bloqueia vazamento de path/arquivo interno sem descartar textos operacionais comuns.
    if any(
        token in lowered
        for token in (
            "c:\\",
            "/tmp",
            "tt_",
            ".json",
            "stack trace",
            "traceback",
            "result_storage_key",
            "request_fingerprint",
        )
    ):
        return None
    if "\\" in text and (":\\" in text or text.startswith("\\\\")):
        return None
    if len(text) > max_chars:
        return f"{text[: max_chars - 1].rstrip()}…"
    return text


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return value
    if isinstance(value, dict):
        return {
            str(key): _json_safe(item)
            for key, item in value.items()
            if key not in _FORBIDDEN_EVIDENCE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if _is_finite_number(value):
        return float(value)
    text = _safe_text(value)
    return text


def _omit_empty(payload: dict[str, Any], *, keep_none_keys: frozenset[str] | None = None) -> dict[str, Any]:
    keep = keep_none_keys or frozenset()
    cleaned: dict[str, Any] = {}
    for key, value in payload.items():
        if value is None and key not in keep:
            continue
        if value == [] and key not in keep:
            continue
        if value == {} and key not in keep:
            continue
        cleaned[key] = value
    return cleaned


def _component_payload(
    *,
    code: str,
    label: str,
    amount: Any = None,
    basis: Any = None,
    rate: Any = None,
    quantity: Any = None,
    operation: Any = None,
    minimum_amount: Any = None,
    minimum_applied: bool | None = None,
    applied: bool = True,
    ignored: bool = False,
    reason: Any = None,
    source: Any = None,
) -> dict[str, Any]:
    payload = {
        "code": code,
        "label": label,
        "basis": _safe_text(basis),
        "rate": _safe_number(rate),
        "quantity": _safe_number(quantity),
        "operation": _safe_text(operation),
        "amount": _safe_money(amount),
        "minimum_amount": _safe_money(minimum_amount),
        "minimum_applied": bool(minimum_applied) if minimum_applied is not None else False,
        "applied": bool(applied),
        "ignored": bool(ignored),
        "reason": _safe_text(reason),
        "source": _safe_text(source),
    }
    # Mantém flags booleanas relevantes mesmo quando False.
    keep_false = {"minimum_applied", "applied", "ignored"}
    cleaned: dict[str, Any] = {}
    for key, value in payload.items():
        if value is None and key not in keep_false:
            if not (ignored and key == "amount"):
                continue
        cleaned[key] = value
    return cleaned



def _sort_components(components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    decorated: list[tuple[int, int, dict[str, Any]]] = []
    for index, item in enumerate(components):
        code = str(item.get("code") or "")
        rank = _COMPONENT_RANK.get(code, len(COMPONENT_ORDER) + 10)
        decorated.append((rank, index, item))
    decorated.sort(key=lambda entry: (entry[0], entry[1]))
    return [item for _, _, item in decorated]


def _extract_weight_freight(source: dict, raw: dict) -> dict[str, Any] | None:
    weight = source.get("weight_freight")
    if isinstance(weight, dict) and weight.get("amount") is not None:
        return _component_payload(
            code="WEIGHT_FREIGHT",
            label="Frete-peso",
            amount=weight.get("amount"),
            basis=weight.get("basis") or raw.get("calculation_basis"),
            operation=weight.get("basis") or raw.get("calculation_basis"),
            source=_safe_text(weight.get("details") or raw.get("calculation_details")),
            applied=True,
        )
    if raw.get("weight_freight") is not None:
        return _component_payload(
            code="WEIGHT_FREIGHT",
            label="Frete-peso",
            amount=raw.get("weight_freight"),
            basis=raw.get("calculation_basis"),
            operation=raw.get("calculation_basis"),
            source=_safe_text(raw.get("calculation_details")),
            applied=True,
        )
    return None


def _extract_freight_value(source: dict, raw: dict) -> dict[str, Any] | None:
    freight_value = source.get("freight_value") or source.get("tariff_freight_value")
    if isinstance(freight_value, dict) and freight_value.get("amount") is not None:
        label = _safe_text(freight_value.get("source_column")) or "Frete-valor"
        return _component_payload(
            code="FREIGHT_VALUE",
            label=label,
            amount=freight_value.get("amount"),
            basis=freight_value.get("details"),
            rate=freight_value.get("rate"),
            quantity=freight_value.get("invoice_value"),
            operation="invoice_percentage",
            source=_safe_text(freight_value.get("source_column") or freight_value.get("source_value")),
            applied=True,
        )
    if raw.get("freight_value_amount") is not None:
        return _component_payload(
            code="FREIGHT_VALUE",
            label="Frete-valor",
            amount=raw.get("freight_value_amount"),
            operation="invoice_percentage",
            applied=True,
        )
    return None


def _extract_toll(source: dict, raw: dict) -> dict[str, Any] | None:
    route_toll = source.get("route_toll") or source.get("tariff_route_toll")
    if isinstance(route_toll, dict) and route_toll.get("amount") is not None:
        label = _safe_text(route_toll.get("source_column")) or "Pedágio"
        return _component_payload(
            code="TOLL",
            label=label,
            amount=route_toll.get("amount"),
            basis=route_toll.get("details"),
            rate=route_toll.get("rate_per_fraction"),
            quantity=route_toll.get("fractions"),
            operation="route_toll",
            source=_safe_text(route_toll.get("source_column") or route_toll.get("source_value")),
            applied=True,
        )
    if raw.get("route_toll_amount") is not None:
        return _component_payload(
            code="TOLL",
            label="Pedágio",
            amount=raw.get("route_toll_amount"),
            operation="route_toll",
            applied=True,
        )
    return None


def _extract_accessorials(source: dict) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    accessorials = source.get("accessorial_fees")
    if isinstance(accessorials, list):
        for item in accessorials:
            if not isinstance(item, dict):
                continue
            amount = item.get("amount")
            if amount is None:
                amount = item.get("calculated_amount")
            label = (
                _safe_text(item.get("label"))
                or _safe_text(item.get("name"))
                or _safe_text(item.get("canonical_component"))
                or "Adicional"
            )
            items.append(
                _component_payload(
                    code="ACCESSORIAL",
                    label=label,
                    amount=amount,
                    basis=item.get("details"),
                    rate=item.get("rate"),
                    quantity=item.get("invoice_value"),
                    operation=item.get("calculation_type") or item.get("operation"),
                    minimum_amount=item.get("minimum_amount"),
                    minimum_applied=item.get("minimum_applied"),
                    applied=True,
                    ignored=False,
                    reason=item.get("reason_code"),
                    source=_safe_text(item.get("source_block") or item.get("source_value")),
                )
            )

    ignored = source.get("ignored_accessorial_fees")
    if isinstance(ignored, list):
        for item in ignored:
            if not isinstance(item, dict):
                continue
            label = (
                _safe_text(item.get("label"))
                or _safe_text(item.get("name"))
                or _safe_text(item.get("canonical_component"))
                or "Adicional"
            )
            reason = (
                item.get("ignored_reason")
                or item.get("reason")
                or item.get("reason_code")
                or item.get("message")
            )
            items.append(
                _component_payload(
                    code="IGNORED_ACCESSORIAL",
                    label=label,
                    amount=None,
                    basis=reason,
                    operation=item.get("calculation_type") or item.get("operation"),
                    applied=False,
                    ignored=True,
                    reason=reason,
                    source=_safe_text(item.get("source_block")),
                )
            )
    return items


def _extract_taxes(source: dict) -> list[dict[str, Any]]:
    taxes: list[dict[str, Any]] = []
    tax_components = source.get("tax_components")
    if not isinstance(tax_components, list):
        return taxes
    for item in tax_components:
        if not isinstance(item, dict):
            continue
        tax_type = _safe_text(item.get("tax_type") or item.get("type")) or "Imposto"
        applied = item.get("applied")
        if applied is None:
            applied = item.get("amount") is not None
        taxes.append(
            {
                "code": "TAX",
                "label": tax_type,
                "tax_type": tax_type,
                "basis": _safe_money(item.get("base_amount")),
                "rate": _safe_number(item.get("rate") or item.get("applied_rate")),
                "amount": _safe_money(item.get("amount") or item.get("tax_amount")),
                "operation": _safe_text(item.get("calculation_mode")),
                "applied": bool(applied),
                "ignored": not bool(applied),
                "reason": _safe_text(item.get("ignored_reason")),
                "source": _safe_text(item.get("source_name") or item.get("source_type")),
            }
        )
    return taxes


def _extract_pricing(raw: dict, source: dict) -> dict[str, Any]:
    pricing: dict[str, Any] = {}
    for key in (
        "pricing_type",
        "pricing_lookup_key",
        "pricing_lookup_kind",
        "freight_region",
        "calculation_basis",
    ):
        text = _safe_text(raw.get(key))
        if text:
            pricing[key if key != "calculation_basis" else "weight_basis_type"] = text
            if key == "calculation_basis":
                pricing["pricing_type"] = pricing.get("pricing_type") or text

    weight = source.get("weight_freight") if isinstance(source.get("weight_freight"), dict) else {}
    weight_band = weight.get("details") or raw.get("calculation_details")
    weight_basis = weight.get("basis") or raw.get("calculation_basis")
    if _safe_text(weight_band):
        pricing["weight_band"] = _safe_text(weight_band)
    if _safe_text(weight_basis):
        pricing["weight_basis"] = _safe_text(weight_basis)
    if _safe_text(raw.get("freight_region")):
        pricing["freight_region"] = _safe_text(raw.get("freight_region"))
    return pricing


def _extract_evidence(raw: dict, source: dict) -> dict[str, Any]:
    """Evidências técnicas enxutas — evita duplicar destino já presente no row pai."""
    evidence: dict[str, Any] = {}
    for key in (
        "freight_region",
        "calculation_basis",
        "calculation_details",
        "pricing_type",
        "pricing_lookup_key",
        "pricing_lookup_kind",
    ):
        text = _safe_text(raw.get(key), max_chars=1000)
        if text:
            evidence[key] = text

    weight = source.get("weight_freight") if isinstance(source.get("weight_freight"), dict) else {}
    if _safe_text(weight.get("details")):
        evidence["weight_band"] = _safe_text(weight.get("details"))
    if _safe_text(weight.get("basis")):
        evidence["weight_basis"] = _safe_text(weight.get("basis"))

    taxes_applied = []
    for tax in _extract_taxes(source):
        if not tax.get("applied"):
            continue
        taxes_applied.append(
            {
                "tax_type": tax.get("tax_type"),
                "applied_rate": tax.get("rate"),
                "amount": tax.get("amount"),
                "calculation_mode": tax.get("operation"),
            }
        )
    if taxes_applied:
        evidence["taxes_applied"] = taxes_applied

    return _json_safe(evidence)


def _build_diagnostic(
    *,
    status: str,
    error: dict | None,
    raw: dict,
) -> dict[str, Any]:
    diagnostic_raw = raw.get("diagnostic") if isinstance(raw.get("diagnostic"), dict) else {}
    error_payload = error if isinstance(error, dict) else {}
    code = (
        _safe_text(error_payload.get("code"))
        or _safe_text(status)
        or _safe_text(diagnostic_raw.get("diagnostic_group_code"))
        or _safe_text(diagnostic_raw.get("failure_stage"))
        or "not_calculated"
    )
    message = (
        _safe_text(error_payload.get("message"), max_chars=500)
        or _safe_text(diagnostic_raw.get("message"), max_chars=500)
        or "Não foi possível calcular esta linha."
    )
    component = _safe_text(
        diagnostic_raw.get("component")
        or diagnostic_raw.get("failure_stage")
        or diagnostic_raw.get("diagnostic_group_code")
    )
    reason = _safe_text(diagnostic_raw.get("failure_stage") or diagnostic_raw.get("diagnostic_group_code"))
    evidence: dict[str, Any] = {}
    search_context = diagnostic_raw.get("search_context")
    if isinstance(search_context, dict):
        for key, value in search_context.items():
            text = _safe_text(value)
            if text:
                evidence[str(key)] = text
    attempted_keys = diagnostic_raw.get("attempted_keys")
    if isinstance(attempted_keys, list) and attempted_keys:
        cleaned_keys = [_safe_text(item) for item in attempted_keys]
        evidence["attempted_keys"] = [item for item in cleaned_keys if item]
    if raw.get("freight_region"):
        evidence["freight_region"] = _safe_text(raw.get("freight_region"))
    return {
        "code": code,
        "message": message,
        "component": component,
        "reason": reason,
        "evidence": _json_safe(evidence),
    }


def validate_calculation_memory_total(
    memory: dict[str, Any],
    *,
    calculated_freight: float | None,
    tolerance: float = _MONEY_TOLERANCE,
) -> None:
    """Garante total == calculated_freight para células calculadas completas."""
    if not isinstance(memory, dict):
        raise CalculationMemoryTotalMismatchError("Memória de cálculo inválida.")
    status = memory.get("status")
    if status == MEMORY_STATUS_INCOMPLETE:
        # Valor parcial é diagnóstico; total rotulado como parcial, não frete definitivo.
        return
    if status not in _MEMORY_COMPLETE_STATUSES:
        if memory.get("total") is not None and status == MEMORY_STATUS_NOT_CALCULATED:
            raise CalculationMemoryTotalMismatchError(
                "Memória não calculada não pode expor total monetário."
            )
        return
    total = memory.get("total")
    freight = calculated_freight if calculated_freight is not None else memory.get("calculated_freight")
    if total is None or freight is None:
        raise CalculationMemoryTotalMismatchError("Total ou frete calculado ausente.")
    if abs(float(total) - float(freight)) > tolerance:
        raise CalculationMemoryTotalMismatchError(
            f"Total da memória ({total}) diverge do frete calculado ({freight})."
        )


def build_not_calculated_diagnostic(
    *,
    raw: dict | None,
    status: str,
    error: dict | None,
    row_index: Any,
    table_id: str | None = None,
    slot_number: int | None = None,
    carrier_name: str | None = None,
) -> dict[str, Any]:
    """Monta memória de diagnóstico sem inventar total zero."""
    source_raw = raw if isinstance(raw, dict) else {}
    diagnostic = _build_diagnostic(status=status, error=error, raw=source_raw)
    memory = {
        "schema_version": CALCULATION_MEMORY_SCHEMA_VERSION,
        "status": MEMORY_STATUS_NOT_CALCULATED,
        "row_index": row_index,
        "table_id": table_id,
        "slot_number": slot_number,
        "carrier_name": _safe_text(carrier_name),
        "calculated_freight": None,
        "pricing": _extract_pricing(source_raw, {}),
        "components": [],
        "taxes": [],
        "total": None,
        "evidence": _extract_evidence(source_raw, {}),
        "diagnostic": diagnostic,
    }
    return _json_safe(
        _omit_empty(
            memory,
            keep_none_keys=frozenset({"total", "calculated_freight", "components", "taxes"}),
        )
    )


def build_calculation_memory(
    raw: dict,
    *,
    calculated_freight: float | None,
    status: str,
    error: dict | None = None,
    row_index: Any = None,
    table_id: str | None = None,
    slot_number: int | None = None,
    carrier_name: str | None = None,
    completeness: dict | None = None,
    is_partial_value: bool = False,
) -> dict[str, Any]:
    """Monta memória a partir do resultado unitário já calculado (sem recalcular)."""
    source_raw = raw if isinstance(raw, dict) else {}
    # Não mutar entrada.
    source_raw_view = source_raw
    components_source = (
        source_raw_view.get("calculation_components")
        if isinstance(source_raw_view.get("calculation_components"), dict)
        else {}
    )
    completeness_payload = completeness if isinstance(completeness, dict) else {}

    normalized_status = str(status or "").strip()
    if normalized_status in _DOMAIN_LIKE_STATUSES or normalized_status == MEMORY_STATUS_NOT_CALCULATED:
        return build_not_calculated_diagnostic(
            raw=source_raw_view,
            status=normalized_status or MEMORY_STATUS_NOT_CALCULATED,
            error=error,
            row_index=row_index if row_index is not None else source_raw_view.get("row_index"),
            table_id=table_id,
            slot_number=slot_number,
            carrier_name=carrier_name,
        )

    if normalized_status not in {
        MEMORY_STATUS_CALCULATED,
        MEMORY_STATUS_CALCULATED_WITH_WARNINGS,
        MEMORY_STATUS_INCOMPLETE,
    }:
        # Fallback legado: status desconhecido com valor → incompleto conservador se houver ignored crítico.
        if calculated_freight is None:
            return build_not_calculated_diagnostic(
                raw=source_raw_view,
                status=normalized_status or MEMORY_STATUS_NOT_CALCULATED,
                error=error,
                row_index=row_index if row_index is not None else source_raw_view.get("row_index"),
                table_id=table_id,
                slot_number=slot_number,
                carrier_name=carrier_name,
            )
        normalized_status = MEMORY_STATUS_INCOMPLETE if is_partial_value else MEMORY_STATUS_CALCULATED

    components: list[dict[str, Any]] = []
    weight = _extract_weight_freight(components_source, source_raw_view)
    if weight:
        components.append(weight)
    freight_value = _extract_freight_value(components_source, source_raw_view)
    if freight_value:
        components.append(freight_value)
    toll = _extract_toll(components_source, source_raw_view)
    if toll:
        components.append(toll)
    components.extend(_extract_accessorials(components_source))
    components = _sort_components(components)

    taxes = _extract_taxes(components_source)
    subtotal = _safe_money(components_source.get("subtotal_before_taxes"))
    if subtotal is None and components_source.get("tax_total") is None:
        subtotal = _safe_money(calculated_freight)

    total = _safe_money(calculated_freight)
    memory_status = normalized_status
    if memory_status == MEMORY_STATUS_INCOMPLETE:
        status_label = "Cálculo incompleto"
        total_label = "Valor parcial calculado"
    elif memory_status == MEMORY_STATUS_CALCULATED_WITH_WARNINGS:
        status_label = "Calculado com ressalvas"
        total_label = "Total calculado"
    else:
        status_label = "Calculado"
        total_label = "Total calculado"

    blocking_issues = list(completeness_payload.get("blocking_issues") or [])
    warnings = list(completeness_payload.get("warnings") or [])

    memory = {
        "schema_version": CALCULATION_MEMORY_SCHEMA_VERSION,
        "status": memory_status,
        "status_label": status_label,
        "total_label": total_label,
        "row_index": row_index if row_index is not None else source_raw_view.get("row_index"),
        "table_id": table_id,
        "slot_number": slot_number,
        "carrier_name": _safe_text(carrier_name),
        "calculated_freight": total,
        "is_partial_value": bool(is_partial_value or memory_status == MEMORY_STATUS_INCOMPLETE),
        "pricing": _extract_pricing(source_raw_view, components_source),
        "components": components,
        "taxes": taxes,
        "subtotal_before_taxes": subtotal,
        "total": total,
        "blocking_issues": _json_safe(blocking_issues),
        "warnings": _json_safe(warnings),
        "completeness": _json_safe(completeness_payload) if completeness_payload else None,
        "evidence": _extract_evidence(source_raw_view, components_source),
    }
    if memory_status == MEMORY_STATUS_INCOMPLETE and error:
        memory["diagnostic"] = _build_diagnostic(
            status=memory_status,
            error=error,
            raw=source_raw_view,
        )
    memory = _json_safe(_omit_empty(memory))
    # Garante campos obrigatórios do contrato.
    memory["status"] = memory_status
    memory["status_label"] = status_label
    memory["total_label"] = total_label
    memory["total"] = total
    memory["calculated_freight"] = total
    memory["components"] = components
    memory["is_partial_value"] = bool(is_partial_value or memory_status == MEMORY_STATUS_INCOMPLETE)
    if taxes:
        memory["taxes"] = taxes
    if blocking_issues:
        memory["blocking_issues"] = _json_safe(blocking_issues)
    if warnings:
        memory["warnings"] = _json_safe(warnings)
    validate_calculation_memory_total(memory, calculated_freight=total)
    return memory


def attach_memory_identity(
    memory: dict[str, Any] | None,
    *,
    table_id: str | None = None,
    slot_number: int | None = None,
    carrier_name: str | None = None,
    row_index: Any = None,
) -> dict[str, Any] | None:
    """Copia memória e preenche identidade de célula sem mutar a original."""
    if not isinstance(memory, dict):
        return None
    payload = copy.deepcopy(memory)
    if table_id is not None:
        payload["table_id"] = table_id
    if slot_number is not None:
        payload["slot_number"] = slot_number
    if carrier_name is not None:
        payload["carrier_name"] = _safe_text(carrier_name)
    if row_index is not None:
        payload["row_index"] = row_index
    return _json_safe(payload)
