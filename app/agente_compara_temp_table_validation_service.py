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
CODE_NO_EXECUTABLE_PRICING_RULE = "NO_EXECUTABLE_PRICING_RULE"
CODE_UNRESOLVED_PRICING_TYPE = "UNRESOLVED_PRICING_TYPE"
CODE_MISSING_PRICING_UNIT = "MISSING_PRICING_UNIT"
CODE_INVALID_TARIFF = "INVALID_TARIFF"
CODE_INVALID_RANGE = "INVALID_RANGE"
CODE_INVALID_EXCESS = "INVALID_EXCESS"
CODE_MISSING_LOOKUP_KEY = "MISSING_LOOKUP_KEY"
CODE_CONFLICTING_LOOKUP_KEY = "CONFLICTING_LOOKUP_KEY"
CODE_LOST_TARIFF_ROW = "LOST_TARIFF_ROW"
CODE_FREIGHT_VALUE_NOT_EXECUTABLE = "FREIGHT_VALUE_NOT_EXECUTABLE"
CODE_ROUTE_TOLL_NOT_EXECUTABLE = "ROUTE_TOLL_NOT_EXECUTABLE"
CODE_CONTRACT_SNAPSHOT_MISMATCH = "CONTRACT_SNAPSHOT_MISMATCH"

_PRICING_FINDING_CODES = {
    "no_executable_rule": CODE_NO_EXECUTABLE_PRICING_RULE,
    "unresolved_pricing_type": CODE_UNRESOLVED_PRICING_TYPE,
    "missing_unit": CODE_MISSING_PRICING_UNIT,
    "invalid_tariff": CODE_INVALID_TARIFF,
    "invalid_range": CODE_INVALID_RANGE,
    "invalid_excess": CODE_INVALID_EXCESS,
    "missing_lookup_key": CODE_MISSING_LOOKUP_KEY,
    "conflicting_lookup_key": CODE_CONFLICTING_LOOKUP_KEY,
    "lost_tariff_row": CODE_LOST_TARIFF_ROW,
    "freight_value_not_executable": CODE_FREIGHT_VALUE_NOT_EXECUTABLE,
    "route_toll_not_executable": CODE_ROUTE_TOLL_NOT_EXECUTABLE,
    "contract_snapshot_mismatch": CODE_CONTRACT_SNAPSHOT_MISMATCH,
}
_PRICING_FINDING_MESSAGES = {
    "no_executable_rule": "A tabela não tem nenhuma regra de frete executável.",
    "unresolved_pricing_type": "Não foi possível resolver o modelo de preço de uma linha tarifária.",
    "missing_unit": "A unidade da tarifa é obrigatória.",
    "invalid_tariff": "Há uma tarifa inválida, negativa ou não numérica.",
    "invalid_range": "Há uma faixa de peso inválida, vazia ou conflitante.",
    "invalid_excess": "O excedente por kg está sem valor ou sem faixa final válida.",
    "missing_lookup_key": "Há tarifa ativa sem chave, destino ou região suficiente para execução.",
    "conflicting_lookup_key": "Há duas regras conflitantes para o mesmo destino.",
    "lost_tariff_row": "Uma linha tarifária ativa não gerou regra executável.",
    "freight_value_not_executable": "Frete valor está marcado, mas sem parâmetros executáveis.",
    "route_toll_not_executable": "Pedágio está marcado, mas sem parâmetros executáveis.",
    "contract_snapshot_mismatch": "O contrato tarifário não corresponde à tabela confirmada.",
}
_SUPPORTED_CONTRACT_TYPES = frozenset({"fixed_range", "direct_weight_rate", "range_plus_excess_per_kg"})
_RANGE_CONTRACT_TYPES = frozenset({"fixed_range", "range_plus_excess_per_kg"})

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


def _pricing_issue(finding: dict) -> dict:
    kind = str(finding.get("kind") or "invalid_tariff")
    source_ref = str(finding.get("source_ref") or kind)
    try:
        index = int(finding.get("index") or 0)
    except (TypeError, ValueError):
        index = 0
    return _json_safe_scalar(
        {
            "code": _PRICING_FINDING_CODES.get(kind, CODE_INVALID_TARIFF),
            "section": "pricing_contract",
            "item_id": f"pricing:{source_ref}",
            "index": index,
            "field": str(finding.get("field") or "pricing_contract"),
            "label": str(finding.get("label") or "Tabela tarifária"),
            "reason_code": kind,
            "severity": "blocking",
            "message": _PRICING_FINDING_MESSAGES.get(kind, _PRICING_FINDING_MESSAGES["invalid_tariff"]),
        }
    )


def _finite_non_negative(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    number = float(value)
    return math.isfinite(number) and number >= 0


def _contract_brackets_conflict(brackets) -> bool:
    if not isinstance(brackets, list) or not brackets:
        return True
    seen_max: set[float] = set()
    previous_max = None
    for bracket in brackets:
        if not isinstance(bracket, dict):
            return True
        if not _finite_non_negative(bracket.get("min_kg")) or not _finite_non_negative(bracket.get("max_kg")):
            return True
        if not _finite_non_negative(bracket.get("value")):
            return True
        min_kg = float(bracket["min_kg"])
        max_kg = float(bracket["max_kg"])
        if max_kg < min_kg or max_kg in seen_max:
            return True
        if previous_max is not None and min_kg < previous_max:
            return True
        seen_max.add(max_kg)
        previous_max = max_kg
    return False


def pricing_contract_structure_findings(contract) -> list[dict]:
    """Defeitos estruturais do contrato, sem recompilar a tabela."""
    findings: list[dict] = []
    rules = contract.get("rules") if isinstance(contract, dict) else None
    if not isinstance(rules, list) or not rules:
        findings.append(
            {
                "kind": "no_executable_rule",
                "source_ref": "pricing_contract",
                "index": 0,
                "field": "rules",
                "label": "Tabela tarifária",
            }
        )
        return findings
    owned: dict[str, int] = {}
    for index, rule in enumerate(rules):
        source_ref = f"rules[{index}]"
        label = f"Regra {index + 1}"
        if isinstance(rule, dict):
            refs = rule.get("source_refs")
            if isinstance(refs, list) and refs:
                source_ref = str(refs[0])
            if rule.get("region"):
                label = str(rule.get("region"))
        if not isinstance(rule, dict):
            findings.append(
                {
                    "kind": "unresolved_pricing_type",
                    "source_ref": source_ref,
                    "index": index,
                    "field": "pricing_type",
                    "label": label,
                }
            )
            continue
        pricing_type = rule.get("pricing_type")
        if pricing_type not in _SUPPORTED_CONTRACT_TYPES:
            findings.append(
                {
                    "kind": "unresolved_pricing_type",
                    "source_ref": source_ref,
                    "index": index,
                    "field": "pricing_type",
                    "label": label,
                }
            )
            continue
        raw_lookup_keys = rule.get("lookup_keys")
        if raw_lookup_keys is None:
            raw_lookup_keys = []
        elif not isinstance(raw_lookup_keys, list):
            findings.append(
                {
                    "kind": "missing_lookup_key",
                    "source_ref": source_ref,
                    "index": index,
                    "field": "lookup_keys",
                    "label": label,
                }
            )
            continue
        keys = [key for key in raw_lookup_keys if isinstance(key, str) and key.strip()]
        if not keys:
            findings.append(
                {
                    "kind": "missing_lookup_key",
                    "source_ref": source_ref,
                    "index": index,
                    "field": "lookup_keys",
                    "label": label,
                }
            )
        for key in keys:
            if key in owned and owned[key] != index:
                findings.append(
                    {
                        "kind": "conflicting_lookup_key",
                        "source_ref": key,
                        "index": index,
                        "field": "lookup_keys",
                        "label": key,
                    }
                )
            else:
                owned[key] = index
        unit = str(rule.get("unit") or "").strip().lower()
        if pricing_type == "direct_weight_rate":
            if unit not in {"kg", "ton"}:
                findings.append(
                    {
                        "kind": "missing_unit",
                        "source_ref": source_ref,
                        "index": index,
                        "field": "unit",
                        "label": label,
                    }
                )
            amount_field = "value_per_ton" if unit == "ton" else "value_per_kg"
            if not _finite_non_negative(rule.get(amount_field)):
                findings.append(
                    {
                        "kind": "invalid_tariff",
                        "source_ref": source_ref,
                        "index": index,
                        "field": amount_field,
                        "label": label,
                    }
                )
        elif pricing_type in _RANGE_CONTRACT_TYPES:
            if unit != "kg":
                findings.append(
                    {
                        "kind": "missing_unit",
                        "source_ref": source_ref,
                        "index": index,
                        "field": "unit",
                        "label": label,
                    }
                )
            brackets = rule.get("brackets") if isinstance(rule.get("brackets"), list) else []
            if _contract_brackets_conflict(brackets):
                findings.append(
                    {
                        "kind": "invalid_range",
                        "source_ref": source_ref,
                        "index": index,
                        "field": "brackets",
                        "label": label,
                    }
                )
            if pricing_type == "range_plus_excess_per_kg":
                excess = rule.get("excess") if isinstance(rule.get("excess"), dict) else {}
                rate = excess.get("rate_per_kg")
                last = None
                if brackets:
                    last = max(brackets, key=lambda item: float(item.get("max_kg") or 0) if _finite_non_negative(item.get("max_kg")) else -1)
                if (
                    not _finite_non_negative(rate)
                    or not isinstance(last, dict)
                    or not _finite_non_negative(last.get("max_kg"))
                    or not _finite_non_negative(last.get("value"))
                ):
                    findings.append(
                        {
                            "kind": "invalid_excess",
                            "source_ref": source_ref,
                            "index": index,
                            "field": "excess",
                            "label": label,
                        }
                    )
        freight_value = rule.get("freight_value")
        if freight_value is not None and (
            not isinstance(freight_value, dict) or not _finite_non_negative(freight_value.get("rate"))
        ):
            findings.append(
                {
                    "kind": "freight_value_not_executable",
                    "source_ref": source_ref,
                    "index": index,
                    "field": "freight_value",
                    "label": label,
                }
            )
        route_toll = rule.get("route_toll")
        if route_toll is not None and (
            not isinstance(route_toll, dict)
            or not _finite_non_negative(route_toll.get("rate_per_fraction"))
            or not _finite_non_negative(route_toll.get("fraction_size_kg"))
            or float(route_toll.get("fraction_size_kg") or 0) <= 0
        ):
            findings.append(
                {
                    "kind": "route_toll_not_executable",
                    "source_ref": source_ref,
                    "index": index,
                    "field": "route_toll",
                    "label": label,
                }
            )
    return findings


def contract_preserves_origin_distinction(contract) -> bool:
    """True quando alguma lookup_key executável traz seletor de origem."""
    if not isinstance(contract, dict):
        return False
    for rule in contract.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        keys = rule.get("lookup_keys")
        if not isinstance(keys, list):
            continue
        for key in keys:
            if not isinstance(key, str) or not key.startswith("ORIGIN:"):
                continue
            if "=>" in key[len("ORIGIN:") :]:
                return True
    return False


def pricing_contract_structure_is_usable(contract) -> bool:
    try:
        if not isinstance(contract, dict) or contract.get("schema_version") != 1:
            return False
        return not pricing_contract_structure_findings(contract)
    except Exception:
        return False


def validate_pricing_contract_for_confirmation(temp_table, contract=None) -> dict:
    """Gate de executabilidade do contrato gerado na confirmação.

    Warnings documentais não entram aqui. Não altera o snapshot.
    """
    from app.agente_compara_doc_service import (
        _compile_pricing_contract,
        _pricing_contract_rules_signature,
        pricing_source_fingerprint,
    )

    compiled, findings = _compile_pricing_contract(temp_table if isinstance(temp_table, dict) else {})
    target = compiled if not isinstance(contract, dict) else contract
    raw_findings = list(findings)
    if int(target.get("schema_version") or 0) != 1 or target.get("source_fingerprint") != pricing_source_fingerprint(
        temp_table if isinstance(temp_table, dict) else {}
    ):
        raw_findings.append(
            {
                "kind": "contract_snapshot_mismatch",
                "source_ref": "pricing_contract",
                "index": 0,
                "field": "source_fingerprint",
                "label": "Tabela tarifária",
            }
        )
    if _pricing_contract_rules_signature(target.get("rules")) != _pricing_contract_rules_signature(compiled.get("rules")):
        raw_findings.append(
            {
                "kind": "contract_snapshot_mismatch",
                "source_ref": "pricing_contract.rules",
                "index": 0,
                "field": "rules",
                "label": "Tabela tarifária",
            }
        )
    try:
        raw_findings.extend(pricing_contract_structure_findings(target))
    except Exception:
        raw_findings.append(
            {
                "kind": "no_executable_rule",
                "source_ref": "pricing_contract",
                "index": 0,
                "field": "rules",
                "label": "Tabela tarifária",
            }
        )
    blocking_issues = _dedupe_pricing_issues([_pricing_issue(finding) for finding in raw_findings])
    blocking_issues.sort(
        key=lambda item: (
            str(item.get("section") or ""),
            int(item.get("index") or 0),
            str(item.get("code") or ""),
            str(item.get("item_id") or ""),
        )
    )
    return _json_safe_scalar(
        {
            "schema_version": VALIDATION_SCHEMA_VERSION,
            "can_confirm": len(blocking_issues) == 0,
            "blocking_count": len(blocking_issues),
            "warning_count": 0,
            "blocking_issues": blocking_issues,
            "warnings": [],
        }
    )


def _dedupe_pricing_issues(issues: list[dict]) -> list[dict]:
    deduped: list[dict] = []
    seen: set[tuple] = set()
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        marker = (issue.get("code"), issue.get("item_id"), issue.get("field"))
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append(issue)
    return deduped


def validate_temp_table_for_confirmation(temp_table, *, enforce_pricing_contract: bool = False) -> dict:
    """Valida snapshot de temp table para confirmação/avançar.

    Retorno JSON-safe, determinístico, sem mutação do input.
    O contrato tarifário só entra no gate quando enforce_pricing_contract=True,
    isto é, na confirmação da tabela revisada.
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
    if enforce_pricing_contract:
        pricing_validation = validate_pricing_contract_for_confirmation(temp_table)
        blocking_issues.extend(pricing_validation.get("blocking_issues") or [])

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
