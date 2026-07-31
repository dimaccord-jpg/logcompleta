"""Validação determinística de temp table do AgenteCompara antes da confirmação.

Isolado do fluxo Cleide/Auditoria. Sem chamadas externas, sem persistência e
sem mutação do snapshot recebido.
"""
from __future__ import annotations

import copy
import math
from typing import Any

VALIDATION_SCHEMA_VERSION = 1

CODE_UNMAPPED_CALCULATION_BASE = "UNMAPPED_CALCULATION_BASE"
CODE_UNCONFIRMED_EXTRACTED_RULE = "UNCONFIRMED_EXTRACTED_RULE"
CODE_MISSING_VALUE = "MISSING_VALUE"
CODE_INVALID_VALUE = "INVALID_VALUE"
CODE_INCOMPATIBLE_UNIT = "INCOMPATIBLE_UNIT"
CODE_UNSUPPORTED_COMPOUND_RULE = "UNSUPPORTED_COMPOUND_RULE"
CODE_MINIMUM_WITHOUT_BASE = "MINIMUM_WITHOUT_BASE"
CODE_MISSING_LINKED_FEE = CODE_MINIMUM_WITHOUT_BASE  # alias estável do vínculo mínimo
CODE_ACCESSORIAL_RATE_CONFLICT = "ACCESSORIAL_RATE_CONFLICT"
CODE_UNSUPPORTED_CONDITION = "UNSUPPORTED_CONDITION"
CODE_UNSUPPORTED_OPERATION = "UNSUPPORTED_OPERATION"
CODE_INVALID_RULE = "INVALID_RULE"
CODE_READING_ALERT = "READING_ALERT"
CODE_UNCERTAIN_FIELD = "UNCERTAIN_FIELD"

_REASON_TO_CODE = {
    "missing_calculation_base": CODE_UNMAPPED_CALCULATION_BASE,
    "unconfirmed_extracted_rule": CODE_UNCONFIRMED_EXTRACTED_RULE,
    "invalid_accessorial_value": CODE_INVALID_VALUE,
    "incompatible_accessorial_unit": CODE_INCOMPATIBLE_UNIT,
    "unsupported_or_incomplete_operation": CODE_UNSUPPORTED_COMPOUND_RULE,
    "percentage_without_audit_variable": CODE_UNSUPPORTED_COMPOUND_RULE,
    "missing_minimum_base_link": CODE_MINIMUM_WITHOUT_BASE,
    "invalid_minimum_base_link": CODE_MINIMUM_WITHOUT_BASE,
    "accessorial_rate_conflict": CODE_ACCESSORIAL_RATE_CONFLICT,
    "unsupported_accessorial_condition": CODE_UNSUPPORTED_CONDITION,
    "conditions_present": CODE_UNSUPPORTED_CONDITION,
    "unsupported_reason_present": CODE_UNSUPPORTED_CONDITION,
    "unsupported_operation": CODE_UNSUPPORTED_OPERATION,
    "invalid_rule": CODE_INVALID_RULE,
}

# Reexport do contrato único validador × motor (sem Cleide).
from app.agente_compara_calculation_completeness_service import (  # noqa: E402
    classify_accessorial_execution_support,
)

_BLOCKING_MESSAGE_UNMAPPED = "Selecione a base de cálculo antes de continuar."
_BLOCKING_MESSAGE_UNCONFIRMED = "Selecione a base de cálculo antes de continuar."
_BLOCKING_MESSAGE_MINIMUM_LINK = (
    "Vincule a uma taxa principal válida ou exclua a regra."
)


def _json_safe_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe_scalar(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe_scalar(val) for key, val in value.items()}
    try:
        if hasattr(value, "as_tuple"):  # Decimal
            as_float = float(value)
            if math.isnan(as_float) or math.isinf(as_float):
                return None
            return as_float
    except Exception:
        pass
    return str(value)


def accessorial_fee_item_identity(fee: dict | None, index: int) -> str:
    """Identidade técnica estável do item (validador, presentation, editor e testes)."""
    if isinstance(fee, dict):
        for key in ("item_id", "fee_id", "id"):
            raw = fee.get(key)
            if raw is None or isinstance(raw, bool):
                continue
            text = str(raw).strip()
            if text:
                return text
    return f"accessorial_fees:{index}"


def _fee_item_id(fee: dict, index: int) -> str:
    return accessorial_fee_item_identity(fee, index)


def _fee_display_label(fee: dict, index: int) -> str:
    label = str(fee.get("name") or "").strip()
    return label or f"Item {index + 1}"


def _action_message_for_unmapped(fee: dict, index: int) -> str:
    label = _fee_display_label(fee, index)
    if label.startswith("Item "):
        return _BLOCKING_MESSAGE_UNMAPPED
    return f"Selecione a base de cálculo de {label}."


def _action_message_for_minimum(fee: dict, index: int) -> str:
    label = _fee_display_label(fee, index)
    if label.startswith("Item "):
        return _BLOCKING_MESSAGE_MINIMUM_LINK
    return f"Vincule {label} a uma taxa principal válida ou exclua a regra."


def _value_absent(fee: dict) -> bool:
    for key in ("value", "rate", "amount", "minimum_amount"):
        raw = fee.get(key)
        if raw is None:
            continue
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return False
        if str(raw).strip():
            return False
    return True


def _map_blocking_issue(error: dict, fee: dict) -> dict:
    reason = str(error.get("reason_code") or "").strip()
    code = _REASON_TO_CODE.get(reason, reason or "BLOCKING_ISSUE")
    if reason == "invalid_accessorial_value" and _value_absent(fee):
        code = CODE_MISSING_VALUE
    index = error.get("index")
    try:
        index_int = int(index)
    except (TypeError, ValueError):
        index_int = 0
    if reason == "missing_calculation_base":
        message = _action_message_for_unmapped(fee, index_int)
    elif reason == "unconfirmed_extracted_rule":
        message = _action_message_for_unmapped(fee, index_int)
    elif reason in {"missing_minimum_base_link", "invalid_minimum_base_link"}:
        message = _action_message_for_minimum(fee, index_int)
    else:
        message = str(error.get("message") or "").strip() or _BLOCKING_MESSAGE_UNMAPPED
    issue = {
        "code": code,
        "section": str(error.get("section") or "accessorial_fees"),
        "item_id": _fee_item_id(fee, index_int),
        "index": index_int,
        "field": str(error.get("field") or ""),
        "label": str(error.get("name") or "").strip() or f"Item {index_int + 1}",
        "reason_code": reason,
        "severity": "blocking",
        "message": message,
    }
    related = error.get("related_fields")
    if isinstance(related, list) and related:
        issue["related_fields"] = [str(item) for item in related if item is not None]
    return _json_safe_scalar(issue)


def _warning_entries(temp_table: dict) -> list[dict]:
    warnings: list[dict] = []
    reading_alerts = temp_table.get("reading_alerts")
    if isinstance(reading_alerts, list):
        for idx, alert in enumerate(reading_alerts):
            text = str(alert or "").strip()
            if not text:
                continue
            warnings.append(
                _json_safe_scalar(
                    {
                        "code": CODE_READING_ALERT,
                        "section": "reading_alerts",
                        "item_id": f"reading_alerts:{idx}",
                        "index": idx,
                        "field": "reading_alerts",
                        "label": "Alerta de leitura",
                        "severity": "warning",
                        "message": text[:240],
                    }
                )
            )
    uncertain_fields = temp_table.get("uncertain_fields")
    if isinstance(uncertain_fields, list):
        for idx, field in enumerate(uncertain_fields):
            text = str(field or "").strip()
            if not text:
                continue
            warnings.append(
                _json_safe_scalar(
                    {
                        "code": CODE_UNCERTAIN_FIELD,
                        "section": "uncertain_fields",
                        "item_id": f"uncertain_fields:{idx}",
                        "index": idx,
                        "field": "uncertain_fields",
                        "label": "Campo incerto",
                        "severity": "warning",
                        "message": text[:240],
                    }
                )
            )
    return warnings


def _support_blocking_issue(fee: dict, index: int, support: dict) -> dict:
    reason = str(support.get("reason_code") or "unsupported_accessorial_condition").strip()
    code = _REASON_TO_CODE.get(reason, CODE_UNSUPPORTED_CONDITION)
    return _json_safe_scalar(
        {
            "code": code,
            "section": "accessorial_fees",
            "item_id": _fee_item_id(fee, index),
            "index": index,
            "field": "conditions" if "condition" in reason else "operation",
            "label": str(fee.get("name") or "").strip() or f"Item {index + 1}",
            "reason_code": reason,
            "severity": "blocking",
            "message": str(support.get("message") or "").strip()
            or "A regra desta taxa não é executável pelo motor de cálculo.",
            "applicability": support.get("applicability"),
        }
    )


def _unconfirmed_extracted_issue(fee: dict, index: int) -> dict:
    return _json_safe_scalar(
        {
            "code": CODE_UNCONFIRMED_EXTRACTED_RULE,
            "section": "accessorial_fees",
            "item_id": _fee_item_id(fee, index),
            "index": index,
            "field": "calculation_base_id",
            "label": _fee_display_label(fee, index),
            "reason_code": "unconfirmed_extracted_rule",
            "severity": "blocking",
            "message": _action_message_for_unmapped(fee, index),
        }
    )


def _collect_accessorial_blocking_issues(accessorial_fees: list) -> list[dict]:
    # Import lazy para evitar ciclo com doc_service e manter o serviço testável.
    # Usa o namespace de doc_service para reaproveitar o mesmo ponto de patch dos testes.
    from app.agente_compara_doc_service import (
        _accessorial_fee_is_extraction_hypothesis,
        _accessorial_fee_is_minimum_modifier,
        _accessorial_fee_should_block_advance,
        _validate_accessorial_fee_for_advance,
        _validate_linked_minimum_amount_for_advance,
        get_active_calculation_bases_for_runtime,
        get_agente_compara_config,
    )

    active_bases = get_active_calculation_bases_for_runtime(
        get_agente_compara_config().calculation_bases
    )
    active_bases_by_id = {
        str(base.get("id") or "").strip(): base
        for base in active_bases
        if str(base.get("id") or "").strip()
    }
    issues: list[dict] = []
    for index, fee in enumerate(accessorial_fees):
        if not isinstance(fee, dict):
            continue
        if not _accessorial_fee_should_block_advance(fee):
            continue
        # Gate estrutural alinhado ao motor: condição/operação inexequível bloqueia.
        support = classify_accessorial_execution_support(fee)
        if support.get("blocking") is True:
            issues.append(_support_blocking_issue(fee, index, support))
            continue
        if _accessorial_fee_is_minimum_modifier(fee):
            error = _validate_linked_minimum_amount_for_advance(
                fee,
                index,
                accessorial_fees,
                active_bases_by_id,
            )
        elif _accessorial_fee_is_extraction_hypothesis(fee):
            issues.append(_unconfirmed_extracted_issue(fee, index))
            continue
        else:
            error = _validate_accessorial_fee_for_advance(fee, index, active_bases_by_id)
        if error is not None:
            issues.append(_map_blocking_issue(error, fee))
    return issues


def _empty_validation_result() -> dict:
    return {
        "schema_version": VALIDATION_SCHEMA_VERSION,
        "can_confirm": True,
        "blocking_count": 0,
        "warning_count": 0,
        "blocking_issues": [],
        "warnings": [],
    }


def validate_temp_table_for_confirmation(temp_table) -> dict:
    """Valida snapshot de temp table para confirmação/avançar.

    Retorno JSON-safe, determinístico, sem mutação do input.
    """
    if not isinstance(temp_table, dict):
        return _empty_validation_result()

    # Trabalha sobre cópia rasa das listas referenciadas para não mutar o snapshot.
    snapshot = {
        "accessorial_fees": list(temp_table.get("accessorial_fees") or []),
        "reading_alerts": list(temp_table.get("reading_alerts") or []),
        "uncertain_fields": list(temp_table.get("uncertain_fields") or []),
    }

    fees = snapshot["accessorial_fees"]
    fees_for_validation = [copy.deepcopy(fee) if isinstance(fee, dict) else fee for fee in fees]
    blocking_issues = _collect_accessorial_blocking_issues(fees_for_validation)
    warnings = _warning_entries(snapshot)

    # Ordem determinística: seção, índice, código, item_id.
    blocking_issues.sort(
        key=lambda item: (
            str(item.get("section") or ""),
            int(item.get("index") or 0),
            str(item.get("code") or ""),
            str(item.get("item_id") or ""),
        )
    )
    warnings.sort(
        key=lambda item: (
            str(item.get("section") or ""),
            int(item.get("index") or 0),
            str(item.get("code") or ""),
            str(item.get("item_id") or ""),
        )
    )

    result = {
        "schema_version": VALIDATION_SCHEMA_VERSION,
        "can_confirm": len(blocking_issues) == 0,
        "blocking_count": len(blocking_issues),
        "warning_count": len(warnings),
        "blocking_issues": blocking_issues,
        "warnings": warnings,
    }
    return _json_safe_scalar(result)


def validation_errors_for_api(validation: dict) -> list[dict]:
    """Converte issues do validador para o formato `errors` já consumido pelo frontend."""
    errors: list[dict] = []
    for issue in validation.get("blocking_issues") or []:
        if not isinstance(issue, dict):
            continue
        errors.append(
            _json_safe_scalar(
                {
                    "code": issue.get("code"),
                    "section": issue.get("section") or "accessorial_fees",
                    "index": issue.get("index"),
                    "name": issue.get("label"),
                    "field": issue.get("field"),
                    "reason_code": issue.get("reason_code") or issue.get("code"),
                    "severity": "blocking",
                    "message": issue.get("message"),
                    "item_id": issue.get("item_id"),
                    **(
                        {"related_fields": issue.get("related_fields")}
                        if isinstance(issue.get("related_fields"), list)
                        else {}
                    ),
                }
            )
        )
    return errors
