"""Avaliação determinística de completeza do cálculo unitário (AgenteCompara).

Recebe o resultado bruto já calculado e classifica se o valor parcial/total
pode ser tratado como frete definitivo. Não recalcula, não persiste, não
acessa Flask/sessão/billing/Gemini e não importa Cleide.
"""
from __future__ import annotations

import copy
import math
from typing import Any

COMPLETENESS_SCHEMA_VERSION = 1

STATUS_CALCULATED = "calculated"
STATUS_CALCULATED_WITH_WARNINGS = "calculated_with_warnings"
STATUS_INCOMPLETE = "incomplete"
STATUS_NOT_CALCULATED = "not_calculated"

APPLICABILITY_APPLIED = "applied"
APPLICABILITY_NOT_APPLICABLE = "not_applicable"
APPLICABILITY_OPTIONAL_NOT_APPLIED = "optional_not_applied"
APPLICABILITY_UNSUPPORTED_CONDITION = "unsupported_condition"
APPLICABILITY_UNSUPPORTED_OPERATION = "unsupported_operation"
APPLICABILITY_INVALID_RULE = "invalid_rule"
APPLICABILITY_MISSING_REQUIRED_DATA = "missing_required_data"
APPLICABILITY_INDETERMINATE = "indeterminate"

# Reason codes produzidos pelo motor que NÃO comprometem completeza.
_BENIGN_REASON_CODES = frozenset(
    {
        "not_applicable",
        "condition_not_met",
        "optional_not_applied",
        "accessorial_fee_not_applied",
        "route_toll_duplicate_ignored",
        "duplicate_invoice_percentage_fee_ignored",
        "disabled_by_user",
    }
)

# Warnings informativos: total permanece completo e comparável.
_WARNING_REASON_CODES = frozenset(
    {
        "classification_warning_present",
        "unsupported_accessorial_condition",
        "conditions_present",
        "unsupported_reason_present",
    }
)

# Reason codes que tornam o cálculo incompleto (potencialmente aplicável, não resolvido).
_CRITICAL_REASON_CODES = frozenset(
    {
        "unsupported_operation",
        "unsupported_calculation_type",
        "missing_audit_variable",
        "invalid_amount",
        "not_configured_calculation_base",
        "legacy_classifier_not_calculated",
        "ambiguous_accessorial_percentage",
        "accessorial_minimum_without_base_ignored",
        "invalid_invoice_value",
        "missing_required_data",
        "invalid_rule",
        "unsupported_condition",
    }
)

_REASON_TO_APPLICABILITY = {
    "not_applicable": APPLICABILITY_NOT_APPLICABLE,
    "condition_not_met": APPLICABILITY_NOT_APPLICABLE,
    "optional_not_applied": APPLICABILITY_OPTIONAL_NOT_APPLIED,
    "accessorial_fee_not_applied": APPLICABILITY_OPTIONAL_NOT_APPLIED,
    "route_toll_duplicate_ignored": APPLICABILITY_NOT_APPLICABLE,
    "duplicate_invoice_percentage_fee_ignored": APPLICABILITY_NOT_APPLICABLE,
    "disabled_by_user": APPLICABILITY_OPTIONAL_NOT_APPLIED,
    "unsupported_accessorial_condition": APPLICABILITY_UNSUPPORTED_CONDITION,
    "conditions_present": APPLICABILITY_UNSUPPORTED_CONDITION,
    "unsupported_condition": APPLICABILITY_UNSUPPORTED_CONDITION,
    "unsupported_reason_present": APPLICABILITY_UNSUPPORTED_CONDITION,
    "unsupported_operation": APPLICABILITY_UNSUPPORTED_OPERATION,
    "unsupported_calculation_type": APPLICABILITY_UNSUPPORTED_OPERATION,
    "missing_audit_variable": APPLICABILITY_MISSING_REQUIRED_DATA,
    "invalid_amount": APPLICABILITY_INVALID_RULE,
    "invalid_rule": APPLICABILITY_INVALID_RULE,
    "not_configured_calculation_base": APPLICABILITY_INVALID_RULE,
    "legacy_classifier_not_calculated": APPLICABILITY_INDETERMINATE,
    "ambiguous_accessorial_percentage": APPLICABILITY_INDETERMINATE,
    "accessorial_minimum_without_base_ignored": APPLICABILITY_INVALID_RULE,
    "invalid_invoice_value": APPLICABILITY_MISSING_REQUIRED_DATA,
    "missing_required_data": APPLICABILITY_MISSING_REQUIRED_DATA,
    "classification_warning_present": APPLICABILITY_OPTIONAL_NOT_APPLIED,
}

_PUBLIC_MESSAGES = {
    APPLICABILITY_UNSUPPORTED_CONDITION: "A condição desta taxa não pôde ser avaliada.",
    APPLICABILITY_UNSUPPORTED_OPERATION: "A operação desta taxa não é suportada pelo motor.",
    APPLICABILITY_INVALID_RULE: "A regra desta taxa está incompleta ou inconsistente.",
    APPLICABILITY_MISSING_REQUIRED_DATA: "Faltam dados obrigatórios para calcular esta taxa.",
    APPLICABILITY_INDETERMINATE: "A aplicabilidade desta taxa ficou indeterminada.",
}


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(val) for key, val in value.items()}
    try:
        if hasattr(value, "as_tuple"):  # Decimal
            as_float = float(value)
            if math.isnan(as_float) or math.isinf(as_float):
                return None
            return as_float
    except Exception:
        pass
    return str(value)


def _reason_code_of(item: dict) -> str:
    for key in ("reason_code", "ignored_reason", "reason", "code"):
        raw = item.get(key)
        if raw is None or isinstance(raw, bool):
            continue
        text = str(raw).strip()
        if text:
            return text
    return ""


def _component_id(item: dict, index: int) -> str:
    for key in ("component_id", "item_id", "fee_id", "id"):
        raw = item.get(key)
        if raw is None or isinstance(raw, bool):
            continue
        text = str(raw).strip()
        if text:
            return text
    return f"ignored_accessorial:{index}"


def _component_code(item: dict) -> str | None:
    for key in ("canonical_component", "canonical_name", "component_group", "component_code"):
        raw = item.get(key)
        if raw is None or isinstance(raw, bool):
            continue
        text = str(raw).strip()
        if text:
            return text
    return None


def _component_label(item: dict) -> str:
    for key in ("label", "name"):
        raw = item.get(key)
        if raw is None or isinstance(raw, bool):
            continue
        text = str(raw).strip()
        if text:
            return text
    code = _component_code(item)
    return code or "Componente acessório"


def _explicit_applicability(item: dict) -> str | None:
    raw = item.get("applicability")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if item.get("applicable") is False:
        return APPLICABILITY_NOT_APPLICABLE
    if item.get("optional") is True or item.get("required") is False:
        return APPLICABILITY_OPTIONAL_NOT_APPLIED
    return None


def _explicit_required(item: dict) -> bool:
    if item.get("required") is True:
        return True
    if item.get("optional") is True or item.get("required") is False:
        return False
    return False


def _explicitly_applicable(item: dict) -> bool:
    applicability = _explicit_applicability(item)
    if applicability in {APPLICABILITY_NOT_APPLICABLE, APPLICABILITY_OPTIONAL_NOT_APPLIED}:
        return False
    if item.get("applicable") is True:
        return True
    return False


def _reason_requires_proven_obligation(reason: str) -> bool:
    return reason in {
        "unsupported_accessorial_condition",
        "conditions_present",
        "unsupported_reason_present",
    }


def _is_blocking_ignored_component(item: dict, reason: str, applicability: str) -> bool:
    if reason in _CRITICAL_REASON_CODES:
        return True
    if not _reason_requires_proven_obligation(reason):
        return False
    if applicability in {APPLICABILITY_NOT_APPLICABLE, APPLICABILITY_OPTIONAL_NOT_APPLIED}:
        return False
    if not _explicit_required(item):
        return False
    return _explicitly_applicable(item)


def classify_ignored_component(item: dict, *, index: int = 0) -> dict:
    """Classifica um componente ignorado sem mutar a entrada."""
    if not isinstance(item, dict):
        return {
            "component_id": f"ignored_accessorial:{index}",
            "component_code": None,
            "label": "Componente inválido",
            "reason_code": "invalid_rule",
            "applicability": APPLICABILITY_INVALID_RULE,
            "severity": "blocking",
            "message": _PUBLIC_MESSAGES[APPLICABILITY_INVALID_RULE],
        }

    reason = _reason_code_of(item)
    applicability = _explicit_applicability(item) or _REASON_TO_APPLICABILITY.get(
        reason, APPLICABILITY_INDETERMINATE
    )

    if applicability == APPLICABILITY_NOT_APPLICABLE or reason in {
        "not_applicable",
        "condition_not_met",
        "route_toll_duplicate_ignored",
        "duplicate_invoice_percentage_fee_ignored",
    }:
        severity = "benign"
    elif _is_blocking_ignored_component(item, reason, applicability):
        severity = "blocking"
    elif applicability == APPLICABILITY_OPTIONAL_NOT_APPLIED or reason in _WARNING_REASON_CODES or reason in {
        "optional_not_applied",
        "accessorial_fee_not_applied",
        "disabled_by_user",
        "classification_warning_present",
    }:
        severity = "warning"
    elif reason in _BENIGN_REASON_CODES:
        severity = "benign"
    elif not reason:
        severity = "warning"
        applicability = APPLICABILITY_INDETERMINATE
    else:
        severity = "warning"
        applicability = APPLICABILITY_INDETERMINATE

    message = _PUBLIC_MESSAGES.get(applicability) or (
        "Componente acessório não pôde ser resolvido no cálculo."
        if severity == "blocking"
        else "Componente acessório omitido sem impacto na completeza."
    )

    payload = {
        "component_id": _component_id(item, index),
        "component_code": _component_code(item),
        "label": _component_label(item),
        "reason_code": reason or "indeterminate_ignored_component",
        "applicability": applicability,
        "severity": severity,
        "message": message,
    }
    return _json_safe(payload)


def _extract_ignored_components(raw: dict) -> list[dict]:
    source = raw.get("calculation_components") if isinstance(raw.get("calculation_components"), dict) else {}
    ignored = source.get("ignored_accessorial_fees")
    if ignored is None:
        ignored = raw.get("ignored_accessorial_fees")
    if not isinstance(ignored, list):
        return []
    return [item for item in ignored if isinstance(item, dict)]


def evaluate_calculation_completeness(
    raw: dict | None,
    *,
    calculated_freight: float | None = None,
    raw_status: str | None = None,
) -> dict:
    """Avalia completeza a partir do resultado bruto do núcleo.

    Não muta ``raw``. Retorno JSON-safe e determinístico.
    """
    source = raw if isinstance(raw, dict) else {}
    freight = calculated_freight
    if freight is None:
        freight = source.get("expected_freight")
        if freight is None:
            freight = source.get("calculated_freight")
    try:
        freight_value = float(freight) if freight is not None else None
        if freight_value is not None and (math.isnan(freight_value) or math.isinf(freight_value)):
            freight_value = None
        elif freight_value is not None:
            freight_value = round(freight_value, 2)
    except (TypeError, ValueError):
        freight_value = None

    domain_status = str(raw_status or source.get("status") or "").strip()
    ignored_raw = _extract_ignored_components(source)
    classified = [classify_ignored_component(item, index=idx) for idx, item in enumerate(ignored_raw)]
    classified.sort(
        key=lambda item: (
            str(item.get("severity") or ""),
            str(item.get("reason_code") or ""),
            str(item.get("component_id") or ""),
            str(item.get("label") or ""),
        )
    )

    blocking = [item for item in classified if item.get("severity") == "blocking"]
    warnings = [item for item in classified if item.get("severity") == "warning"]
    benign = [item for item in classified if item.get("severity") == "benign"]

    # Domínio do núcleo sem valor utilizável.
    if freight_value is None:
        status = STATUS_NOT_CALCULATED
        is_complete = False
        has_partial = False
    elif blocking:
        status = STATUS_INCOMPLETE
        is_complete = False
        has_partial = True
    elif warnings:
        status = STATUS_CALCULATED_WITH_WARNINGS
        is_complete = True
        has_partial = False
    else:
        status = STATUS_CALCULATED
        is_complete = True
        has_partial = False

    # Preserva rastreio do status bruto quando informado e distinto.
    result = {
        "schema_version": COMPLETENESS_SCHEMA_VERSION,
        "status": status,
        "completeness_status": status,
        "raw_status": domain_status or None,
        "is_complete": is_complete,
        "has_partial_value": has_partial,
        "partial_value": freight_value if has_partial else None,
        "blocking_issues": [
            {
                "code": str(item.get("reason_code") or "CRITICAL_IGNORED_COMPONENT").upper(),
                "component_id": item.get("component_id"),
                "component_code": item.get("component_code"),
                "reason_code": item.get("reason_code"),
                "applicability": item.get("applicability"),
                "message": item.get("message"),
                "label": item.get("label"),
            }
            for item in blocking
        ],
        "warnings": [
            {
                "code": str(item.get("reason_code") or "WARNING_IGNORED_COMPONENT").upper(),
                "component_id": item.get("component_id"),
                "component_code": item.get("component_code"),
                "reason_code": item.get("reason_code"),
                "applicability": item.get("applicability"),
                "message": item.get("message"),
                "label": item.get("label"),
            }
            for item in warnings
        ],
        "ignored_components": classified,
        "benign_ignored_count": len(benign),
        "critical_ignored_component_count": len(blocking),
        "warning_ignored_component_count": len(warnings),
    }
    return _json_safe(copy.deepcopy(result))


def classify_accessorial_execution_support(fee: dict | None) -> dict:
    """Contrato único de suporte validador × motor para uma taxa acessória.

    Determinístico, sem IA e sem mutação da entrada.
    """
    if not isinstance(fee, dict):
        return _json_safe(
            {
                "supported": False,
                "applicability": APPLICABILITY_INVALID_RULE,
                "reason_code": "invalid_rule",
                "blocking": True,
                "message": _PUBLIC_MESSAGES[APPLICABILITY_INVALID_RULE],
            }
        )

    if fee.get("applicable") is False:
        return _json_safe(
            {
                "supported": True,
                "applicability": APPLICABILITY_NOT_APPLICABLE,
                "reason_code": "not_applicable",
                "blocking": False,
                "message": "Taxa formalmente não aplicável.",
            }
        )

    optional = fee.get("optional") is True or fee.get("required") is False

    base_id = str(fee.get("calculation_base_id") or "").strip().lower()
    operation = str(fee.get("operation") or "").strip()
    simple_fixed_bases = {"por_cte", "por_conhecimento", "por_documento"}
    ignore_textual_conditions = (
        operation == "fixed_amount"
        and base_id in simple_fixed_bases
        and fee.get("unsupported_reason") in (None, "", False)
    )

    conditions = fee.get("conditions")
    has_conditions = False
    if isinstance(conditions, str):
        has_conditions = bool(conditions.strip())
    elif isinstance(conditions, (list, tuple, dict)):
        has_conditions = bool(conditions)
    elif conditions:
        has_conditions = True

    if has_conditions and not ignore_textual_conditions:
        return _json_safe(
            {
                "supported": False,
                "applicability": APPLICABILITY_UNSUPPORTED_CONDITION,
                "reason_code": "unsupported_accessorial_condition",
                "blocking": not optional,
                "message": _PUBLIC_MESSAGES[APPLICABILITY_UNSUPPORTED_CONDITION],
            }
        )

    unsupported_reason = fee.get("unsupported_reason")
    if unsupported_reason not in (None, "", False):
        return _json_safe(
            {
                "supported": False,
                "applicability": APPLICABILITY_UNSUPPORTED_CONDITION,
                "reason_code": "unsupported_reason_present",
                "blocking": not optional,
                "message": _PUBLIC_MESSAGES[APPLICABILITY_UNSUPPORTED_CONDITION],
            }
        )

    operation = str(fee.get("operation") or "").strip()
    motor_ops = {"percentage_of_variable", "fixed_amount", "ceil_fraction"}
    # multiply_by_variable é aceito em contratos legados de UI, mas o motor
    # configurado não a executa — alinhar validador × motor.
    if operation == "multiply_by_variable":
        return _json_safe(
            {
                "supported": False,
                "applicability": APPLICABILITY_UNSUPPORTED_OPERATION,
                "reason_code": "unsupported_operation",
                "blocking": not optional,
                "message": _PUBLIC_MESSAGES[APPLICABILITY_UNSUPPORTED_OPERATION],
            }
        )
    if operation and operation not in motor_ops and str(fee.get("classification_source") or "").startswith(
        ("configured_", "manual_")
    ):
        # Operação explícita fora do conjunto executável do motor.
        if operation not in {"", "percentage_of_variable", "fixed_amount", "ceil_fraction"}:
            return _json_safe(
                {
                    "supported": False,
                    "applicability": APPLICABILITY_UNSUPPORTED_OPERATION,
                    "reason_code": "unsupported_operation",
                    "blocking": not optional,
                    "message": _PUBLIC_MESSAGES[APPLICABILITY_UNSUPPORTED_OPERATION],
                }
            )

    return _json_safe(
        {
            "supported": True,
            "applicability": APPLICABILITY_APPLIED,
            "reason_code": None,
            "blocking": False,
            "message": None,
        }
    )
