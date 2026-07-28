"""
Motor determinístico unitário do AgenteCompara (Etapa 3).

Calcula o frete de todas as linhas do arquivo operacional para UMA única
tabela de transportadora explicitamente informada.

Sem rota Flask, sem sessão, sem Gemini, sem billing, sem persistência,
sem comparação multitabela e sem dependência de frete cobrado.
"""
from __future__ import annotations

import copy
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.agente_compara_comparison_state import (
    TABLE_STATUS_CONFIRMED,
    get_comparison_state,
    get_comparison_tax_config,
    get_table_by_id,
)
from app.agente_compara_doc_service import (
    AUDIT_STATUS_AMBIGUOUS_COVERAGE,
    AUDIT_STATUS_INVALID_INVOICE_VALUE,
    AUDIT_STATUS_INVALID_WEIGHT,
    AUDIT_STATUS_MISSING_COVERAGE,
    AUDIT_STATUS_MISSING_FREIGHT_RULE,
    AUDIT_STATUS_UNSUPPORTED_PRICING,
    _calculate_expected_freight_row,
    build_coverage_index,
    build_freight_pricing_index,
    get_agente_compara_config,
    load_temp_table_record,
)

logger = logging.getLogger(__name__)

SINGLE_TABLE_CALCULATION_SCHEMA_VERSION = 1

STATUS_CALCULATED = "calculated"
STATUS_MISSING_COVERAGE_MAPPING = AUDIT_STATUS_MISSING_COVERAGE
STATUS_AMBIGUOUS_COVERAGE_MAPPING = AUDIT_STATUS_AMBIGUOUS_COVERAGE
STATUS_MISSING_FREIGHT_RULE = AUDIT_STATUS_MISSING_FREIGHT_RULE
STATUS_INVALID_WEIGHT = AUDIT_STATUS_INVALID_WEIGHT
STATUS_INVALID_INVOICE_VALUE = AUDIT_STATUS_INVALID_INVOICE_VALUE
STATUS_UNSUPPORTED_PRICING_MODEL = AUDIT_STATUS_UNSUPPORTED_PRICING

# Status de domínio produzidos pelo núcleo via payload (não via Exception).
_DOMAIN_ROW_STATUSES = frozenset(
    {
        STATUS_MISSING_COVERAGE_MAPPING,
        STATUS_AMBIGUOUS_COVERAGE_MAPPING,
        STATUS_MISSING_FREIGHT_RULE,
        STATUS_INVALID_WEIGHT,
        STATUS_INVALID_INVOICE_VALUE,
        STATUS_UNSUPPORTED_PRICING_MODEL,
    }
)

ERROR_UNEXPECTED_CALCULATION = "agente_compara_calculation_unexpected_error"

_FORBIDDEN_PUBLIC_FIELDS = frozenset(
    {
        "charged_freight",
        "freight_charged",
        "expected_freight",
        "difference",
        "divergence",
        "divergence_value",
        "overcharged",
        "undercharged",
        "divergent",
        "match",
    }
)

ERROR_COMPARISON_NOT_FOUND = "agente_compara_calculation_comparison_not_found"
ERROR_TABLE_NOT_FOUND = "agente_compara_calculation_table_not_found"
ERROR_TABLE_OWNERSHIP = "agente_compara_calculation_table_ownership"
ERROR_TEMP_TABLE_MISMATCH = "agente_compara_calculation_temp_table_mismatch"
ERROR_SLOT_MISMATCH = "agente_compara_calculation_slot_mismatch"
ERROR_TABLE_NOT_CONFIRMED = "agente_compara_calculation_table_not_confirmed"
ERROR_TEMP_TABLE_MISSING = "agente_compara_calculation_temp_table_missing"
ERROR_TABLE_DATA_MISSING = "agente_compara_calculation_table_data_missing"
ERROR_OPERATIONAL_FILE_EMPTY = "agente_compara_calculation_operational_file_empty"
ERROR_INVALID_CONTEXT = "agente_compara_calculation_invalid_context"
ERROR_IDENTITY_MISMATCH = "agente_compara_calculation_identity_mismatch"
ERROR_COVERAGE_MISMATCH = "agente_compara_calculation_coverage_mismatch"
ERROR_SCHEMA_UNSUPPORTED = "agente_compara_calculation_schema_unsupported"


class AgenteComparaCalculationError(Exception):
    """Erro sistêmico do motor unitário (interrompe a execução)."""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


class InvalidCalculationContextError(AgenteComparaCalculationError):
    def __init__(self, message: str, *, error_code: str = ERROR_INVALID_CONTEXT):
        super().__init__(error_code, message)


class TableOwnershipError(AgenteComparaCalculationError):
    def __init__(self, message: str, *, error_code: str = ERROR_TABLE_OWNERSHIP):
        super().__init__(error_code, message)


class UnexpectedSingleTableCalculationError(AgenteComparaCalculationError):
    """Bug ou invariante quebrada no cálculo unitário — interrompe o lote."""

    def __init__(
        self,
        message: str,
        *,
        comparison_id: str | None = None,
        table_id: str | None = None,
        slot_number: int | None = None,
        row_index=None,
        exception_type: str | None = None,
        error_code: str = ERROR_UNEXPECTED_CALCULATION,
    ):
        super().__init__(error_code, message)
        self.comparison_id = comparison_id
        self.table_id = table_id
        self.slot_number = slot_number
        self.row_index = row_index
        self.exception_type = exception_type


@dataclass(frozen=True)
class SingleTableCalculationContext:
    """Contrato de entrada imutável para o cálculo de uma única tabela."""

    comparison_id: str
    table_id: str
    temp_table_id: str
    slot_number: int
    carrier_name: str
    table_record: dict
    normalized_rows: list[dict]
    tax_config: dict | None = None
    coverage_table: dict | list | None = None
    schema_version: int = SINGLE_TABLE_CALCULATION_SCHEMA_VERSION
    execution_id: str | None = None
    # Metadados de ownership já validados (não usados no loop de cálculo).
    primary_temp_table_id: str | None = field(default=None, repr=False, compare=False)


def _require_non_empty_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidCalculationContextError(
            f"Campo obrigatório ausente ou inválido: {field_name}."
        )
    return value.strip()


def _require_positive_int_slot(value: Any) -> int:
    try:
        slot = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidCalculationContextError("slot_number inválido.") from exc
    if slot < 1 or slot > 3:
        raise InvalidCalculationContextError("slot_number fora do intervalo permitido.")
    return slot


def _public_error_message(message: str) -> str:
    text = (message or "").strip() or "Não foi possível calcular esta linha."
    # Evita vazar caminhos internos (tt_*.json, filesystem, etc.).
    lowered = text.lower()
    if any(token in lowered for token in ("\\", "/", ".json", "tt_", "c:\\", "/tmp")):
        return "Não foi possível calcular com os dados informados."
    return text


def _row_error_payload(code: str, message: str) -> dict:
    return {
        "code": code,
        "message": _public_error_message(message),
    }


def _map_row_status(raw_status: str | None, *, has_expected: bool) -> str:
    if has_expected and (raw_status is None or raw_status == ""):
        return STATUS_CALCULATED
    if raw_status in _DOMAIN_ROW_STATUSES:
        return raw_status
    if raw_status == STATUS_CALCULATED:
        return STATUS_CALCULATED
    raise UnexpectedSingleTableCalculationError(
        "Status de linha inesperado retornado pelo núcleo de cálculo.",
        exception_type="unexpected_row_status",
    )


def _extract_components(raw: dict, calculated_freight: float | None) -> dict:
    source = raw.get("calculation_components") if isinstance(raw.get("calculation_components"), dict) else {}
    components: dict[str, Any] = {}

    weight_freight = source.get("weight_freight")
    if isinstance(weight_freight, dict) and weight_freight.get("amount") is not None:
        components["weight_freight"] = weight_freight.get("amount")
    elif raw.get("weight_freight") is not None:
        components["weight_freight"] = raw.get("weight_freight")

    freight_value = source.get("freight_value") or source.get("tariff_freight_value")
    if isinstance(freight_value, dict) and freight_value.get("amount") is not None:
        components["freight_value_component"] = freight_value.get("amount")
    elif raw.get("freight_value_amount") is not None:
        components["freight_value_component"] = raw.get("freight_value_amount")

    route_toll = source.get("route_toll") or source.get("tariff_route_toll")
    if isinstance(route_toll, dict) and route_toll.get("amount") is not None:
        components["toll"] = route_toll.get("amount")
    elif raw.get("route_toll_amount") is not None:
        components["toll"] = raw.get("route_toll_amount")

    accessorials = source.get("accessorial_fees")
    if isinstance(accessorials, list) and accessorials:
        components["accessorials"] = accessorials
        gris_total = 0.0
        dispatch_total = 0.0
        found_gris = False
        found_dispatch = False
        for item in accessorials:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("label") or "").strip().lower()
            amount = item.get("amount")
            if amount is None:
                continue
            try:
                amount_f = float(amount)
            except (TypeError, ValueError):
                continue
            if "gris" in name or "ad valorem" in name or "advalorem" in name or "risco" in name:
                gris_total += amount_f
                found_gris = True
            group = str(item.get("component_group") or item.get("canonical_component") or "").strip().lower()
            if "despacho" in name or group in {"dispatch", "despacho"}:
                dispatch_total += amount_f
                found_dispatch = True
        if found_gris:
            components["gris"] = round(gris_total, 2)
        if found_dispatch:
            components["dispatch"] = round(dispatch_total, 2)

    if source.get("subtotal_before_taxes") is not None:
        components["subtotal"] = source.get("subtotal_before_taxes")
    elif calculated_freight is not None and source.get("tax_total") is None:
        components["subtotal"] = calculated_freight

    tax_total = source.get("tax_total")
    tax_components = source.get("tax_components")
    if tax_total is not None:
        components["taxes"] = tax_total
    if isinstance(tax_components, list) and tax_components:
        for tax_item in tax_components:
            if not isinstance(tax_item, dict):
                continue
            tax_type = str(tax_item.get("tax_type") or tax_item.get("type") or "").strip().upper()
            amount = tax_item.get("amount") or tax_item.get("tax_amount")
            if amount is None:
                continue
            if tax_type == "ICMS":
                components["icms"] = amount
            elif tax_type == "ISS":
                components["iss"] = amount

    if calculated_freight is not None:
        components["total"] = calculated_freight

    return components


def _extract_evidence(raw: dict) -> dict:
    evidence: dict[str, Any] = {}
    freight_region = raw.get("freight_region")
    if freight_region:
        evidence["freight_region"] = freight_region
    if raw.get("destination_city"):
        evidence["destination_city"] = raw.get("destination_city")
    if raw.get("destination_uf"):
        evidence["destination_uf"] = raw.get("destination_uf")
    if raw.get("calculation_basis"):
        evidence["calculation_basis"] = raw.get("calculation_basis")
    if raw.get("calculation_details"):
        evidence["calculation_details"] = raw.get("calculation_details")
    if raw.get("pricing_type"):
        evidence["pricing_type"] = raw.get("pricing_type")
    if raw.get("pricing_lookup_key"):
        evidence["pricing_lookup_key"] = raw.get("pricing_lookup_key")
    if raw.get("pricing_lookup_kind"):
        evidence["pricing_lookup_kind"] = raw.get("pricing_lookup_kind")

    components = raw.get("calculation_components") if isinstance(raw.get("calculation_components"), dict) else {}
    weight_freight = components.get("weight_freight")
    if isinstance(weight_freight, dict):
        if weight_freight.get("details"):
            evidence["weight_band"] = weight_freight.get("details")
        if weight_freight.get("basis"):
            evidence["weight_basis"] = weight_freight.get("basis")

    tax_components = components.get("tax_components")
    if isinstance(tax_components, list) and tax_components:
        evidence["taxes_applied"] = [
            {
                "tax_type": item.get("tax_type") or item.get("type"),
                "applied_rate": item.get("applied_rate") or item.get("rate"),
                "amount": item.get("amount") or item.get("tax_amount"),
            }
            for item in tax_components
            if isinstance(item, dict)
        ]

    accessorials = components.get("accessorial_fees")
    if isinstance(accessorials, list) and accessorials:
        evidence["accessorials_applied"] = [
            {
                "name": item.get("name") or item.get("label"),
                "amount": item.get("amount"),
                "calculation_type": item.get("calculation_type") or item.get("operation"),
            }
            for item in accessorials
            if isinstance(item, dict)
        ]

    return evidence


def _normalize_row_result(raw: dict, source_row: dict) -> dict:
    if not isinstance(raw, dict):
        raise UnexpectedSingleTableCalculationError(
            "Núcleo de cálculo retornou estrutura inválida.",
            exception_type=type(raw).__name__,
            row_index=source_row.get("row_index") if isinstance(source_row, dict) else None,
        )

    calculated_freight = raw.get("expected_freight")
    if calculated_freight is not None:
        try:
            calculated_freight = round(float(calculated_freight), 2)
        except (TypeError, ValueError) as exc:
            raise UnexpectedSingleTableCalculationError(
                "Frete calculado em formato inválido.",
                exception_type=type(exc).__name__,
                row_index=source_row.get("row_index"),
            ) from exc

    try:
        status = _map_row_status(raw.get("status"), has_expected=calculated_freight is not None)
    except UnexpectedSingleTableCalculationError as exc:
        exc.row_index = source_row.get("row_index")
        raise

    if status == STATUS_CALCULATED and calculated_freight is None:
        raise UnexpectedSingleTableCalculationError(
            "Linha marcada como calculada sem frete válido.",
            exception_type="missing_calculated_freight",
            row_index=source_row.get("row_index"),
        )

    error = None
    if status != STATUS_CALCULATED:
        diagnostic = raw.get("diagnostic") if isinstance(raw.get("diagnostic"), dict) else {}
        message = diagnostic.get("message") or f"Falha no cálculo da linha ({status})."
        error = _row_error_payload(status, str(message))
        calculated_freight = None

    invoice_value = source_row.get("invoice_value")
    if invoice_value is None:
        invoice_value = raw.get("invoice_value")

    row_index = source_row.get("row_index", raw.get("row_index"))
    if row_index is None:
        raise UnexpectedSingleTableCalculationError(
            "Linha operacional sem row_index.",
            exception_type="missing_row_index",
        )

    result = {
        "row_index": row_index,
        "document_number": source_row.get("document_number") or raw.get("numero_documento"),
        "destination_city": source_row.get("destination_city") or raw.get("destination_city"),
        "destination_uf": source_row.get("destination_uf") or raw.get("destination_uf"),
        "weight": source_row.get("audited_weight", raw.get("audited_weight")),
        "invoice_value": invoice_value,
        "calculated_freight": calculated_freight,
        "status": status,
        "error": error,
        "components": _extract_components(raw, calculated_freight) if status == STATUS_CALCULATED else {},
        "evidence": _extract_evidence(raw) if (
            status == STATUS_CALCULATED or raw.get("freight_region") or raw.get("diagnostic")
        ) else {},
    }
    if result["invoice_value"] is None:
        result.pop("invoice_value", None)

    for forbidden in _FORBIDDEN_PUBLIC_FIELDS:
        result.pop(forbidden, None)
    return result


def _raise_unexpected_row_failure(
    *,
    context: SingleTableCalculationContext,
    source_row: dict,
    exc: BaseException,
) -> None:
    row_index = source_row.get("row_index") if isinstance(source_row, dict) else None
    logger.exception(
        "agente_compara_single_table_unexpected comparison_id=%s table_id=%s slot=%s "
        "row_index=%s exception_type=%s",
        context.comparison_id,
        context.table_id,
        context.slot_number,
        row_index,
        type(exc).__name__,
    )
    raise UnexpectedSingleTableCalculationError(
        "Falha inesperada no cálculo unitário da tabela.",
        comparison_id=context.comparison_id,
        table_id=context.table_id,
        slot_number=context.slot_number,
        row_index=row_index,
        exception_type=type(exc).__name__,
    ) from exc


def _resolve_effective_tax_config(tax_config: dict | None, table_id: str) -> dict | None:
    if not isinstance(tax_config, dict):
        return None
    if tax_config.get("include_taxes") is not True:
        return copy.deepcopy(tax_config)
    selected = tax_config.get("selected_table_ids")
    if isinstance(selected, list) and selected:
        normalized = {str(item).strip() for item in selected if isinstance(item, str) and item.strip()}
        if table_id not in normalized:
            effective = copy.deepcopy(tax_config)
            effective["include_taxes"] = False
            return effective
    return copy.deepcopy(tax_config)


def _table_has_prepared_data(record: dict) -> bool:
    freight_tables = record.get("freight_tables")
    freight_routes = record.get("freight_routes")
    has_tables = isinstance(freight_tables, list) and any(
        isinstance(item, dict) and (item.get("rows") or item.get("columns"))
        for item in freight_tables
    )
    has_routes = isinstance(freight_routes, list) and any(isinstance(item, dict) for item in freight_routes)
    return bool(has_tables or has_routes)


def _coverage_from_sources(
    *,
    coverage_table: dict | list | None,
    table_record: dict,
) -> dict | None:
    if isinstance(coverage_table, dict):
        return coverage_table
    if isinstance(coverage_table, list):
        return {"rows": coverage_table}
    record_coverage = table_record.get("coverage_table")
    if isinstance(record_coverage, dict):
        return record_coverage
    return None


def build_single_table_calculation_context(
    *,
    comparison_id: str,
    table_id: str,
    temp_table_id: str,
    slot_number: int,
    carrier_name: str | None = None,
    comparison_state: dict | None = None,
    session_obj=None,
    table_record: dict | None = None,
    normalized_rows: list[dict] | None = None,
    tax_config: dict | None = None,
    coverage_table: dict | list | None = None,
    schema_version: int = SINGLE_TABLE_CALCULATION_SCHEMA_VERSION,
    execution_id: str | None = None,
    ttl_hours: int | None = None,
) -> SingleTableCalculationContext:
    """Resolve ownership e monta o contexto explícito de uma tabela.

    Pode carregar o record do disco quando ``table_record`` não é informado.
    Não executa cálculo.
    """
    if schema_version != SINGLE_TABLE_CALCULATION_SCHEMA_VERSION:
        raise InvalidCalculationContextError(
            "Versão de schema de cálculo não suportada.",
            error_code=ERROR_SCHEMA_UNSUPPORTED,
        )

    comparison_id = _require_non_empty_str(comparison_id, "comparison_id")
    table_id = _require_non_empty_str(table_id, "table_id")
    temp_table_id = _require_non_empty_str(temp_table_id, "temp_table_id")
    slot_number = _require_positive_int_slot(slot_number)

    state = comparison_state
    if state is None and session_obj is not None:
        state = get_comparison_state(session_obj)
    if state is None:
        raise InvalidCalculationContextError(
            "Comparação não encontrada.",
            error_code=ERROR_COMPARISON_NOT_FOUND,
        )
    state_comparison_id = (state.get("comparison_id") or "").strip()
    if state_comparison_id != comparison_id:
        raise TableOwnershipError(
            "comparison_id não corresponde à comparação ativa.",
            error_code=ERROR_IDENTITY_MISMATCH,
        )

    entry = get_table_by_id(state, table_id)
    if entry is None:
        raise TableOwnershipError(
            "Tabela não pertence à comparação informada.",
            error_code=ERROR_TABLE_NOT_FOUND,
        )

    entry_temp = (entry.get("temp_table_id") or "").strip()
    if entry_temp != temp_table_id:
        raise TableOwnershipError(
            "temp_table_id não corresponde à tabela informada.",
            error_code=ERROR_TEMP_TABLE_MISMATCH,
        )

    try:
        entry_slot = int(entry.get("slot_number"))
    except (TypeError, ValueError) as exc:
        raise TableOwnershipError(
            "Slot da tabela inválido no estado da comparação.",
            error_code=ERROR_SLOT_MISMATCH,
        ) from exc
    if entry_slot != slot_number:
        raise TableOwnershipError(
            "slot_number divergente do estado da comparação.",
            error_code=ERROR_SLOT_MISMATCH,
        )

    if not entry.get("confirmed") and entry.get("status") != TABLE_STATUS_CONFIRMED:
        raise InvalidCalculationContextError(
            "Tabela ainda não está confirmada para cálculo.",
            error_code=ERROR_TABLE_NOT_CONFIRMED,
        )

    resolved_carrier = carrier_name if carrier_name is not None else entry.get("carrier_name")
    resolved_carrier = (resolved_carrier or "").strip()
    if not resolved_carrier:
        raise InvalidCalculationContextError("carrier_name é obrigatório para o cálculo unitário.")

    entry_carrier = (entry.get("carrier_name") or "").strip()
    if entry_carrier and carrier_name is not None and entry_carrier != resolved_carrier:
        raise TableOwnershipError(
            "carrier_name divergente da tabela confirmada.",
            error_code=ERROR_IDENTITY_MISMATCH,
        )

    record = table_record
    if record is None:
        hours = ttl_hours
        if hours is None:
            hours = int(get_agente_compara_config().upload_ttl_hours)
        record = load_temp_table_record(temp_table_id, ttl_hours=hours)
        if record is None:
            raise InvalidCalculationContextError(
                "Record da tabela temporária não encontrado.",
                error_code=ERROR_TEMP_TABLE_MISSING,
            )

    if not isinstance(record, dict):
        raise InvalidCalculationContextError(
            "Estrutura da tabela temporária inválida.",
            error_code=ERROR_TABLE_DATA_MISSING,
        )

    record_temp_id = (record.get("temp_table_id") or "").strip()
    if record_temp_id and record_temp_id != temp_table_id:
        raise TableOwnershipError(
            "Identidade do record diverge do temp_table_id informado.",
            error_code=ERROR_TEMP_TABLE_MISMATCH,
        )

    if not _table_has_prepared_data(record):
        raise InvalidCalculationContextError(
            "Tabela sem dados de frete preparados para cálculo.",
            error_code=ERROR_TABLE_DATA_MISSING,
        )

    rows = normalized_rows
    if rows is None:
        audit_batch = record.get("audit_batch") if isinstance(record.get("audit_batch"), dict) else {}
        rows = audit_batch.get("normalized_rows")
    if not isinstance(rows, list) or not rows:
        raise InvalidCalculationContextError(
            "Arquivo operacional sem linhas normalizadas.",
            error_code=ERROR_OPERATIONAL_FILE_EMPTY,
        )
    if not all(isinstance(item, dict) for item in rows):
        raise InvalidCalculationContextError(
            "Linhas normalizadas inválidas no arquivo operacional.",
            error_code=ERROR_OPERATIONAL_FILE_EMPTY,
        )

    resolved_tax = tax_config
    if resolved_tax is None:
        resolved_tax = get_comparison_tax_config(state)
    effective_tax = _resolve_effective_tax_config(resolved_tax, table_id)

    resolved_coverage = _coverage_from_sources(
        coverage_table=coverage_table,
        table_record=record,
    )

    primary = state.get("primary_temp_table_id")
    if isinstance(primary, str):
        primary = primary.strip() or None
    else:
        primary = None

    # Snapshot defensivo: o contexto congela cópias; o cálculo não deve mutar as originais.
    return SingleTableCalculationContext(
        comparison_id=comparison_id,
        table_id=table_id,
        temp_table_id=temp_table_id,
        slot_number=slot_number,
        carrier_name=resolved_carrier,
        table_record=copy.deepcopy(record),
        normalized_rows=copy.deepcopy(rows),
        tax_config=copy.deepcopy(effective_tax) if effective_tax is not None else None,
        coverage_table=copy.deepcopy(resolved_coverage) if resolved_coverage is not None else None,
        schema_version=schema_version,
        execution_id=(execution_id.strip() if isinstance(execution_id, str) and execution_id.strip() else None),
        primary_temp_table_id=primary,
    )


def calculate_single_table(context: SingleTableCalculationContext) -> dict:
    """Calcula frete para uma única tabela de forma determinística e sem I/O.

    Não acessa Flask session, não persiste, não cobra e não chama Gemini.

    Contrato de ordem: a saída é sempre ordenada crescentemente por ``row_index``.
    Documentos duplicados são preservados como linhas independentes.
    """
    started = time.perf_counter()
    if not isinstance(context, SingleTableCalculationContext):
        raise InvalidCalculationContextError("Contexto de cálculo inválido.")
    if context.schema_version != SINGLE_TABLE_CALCULATION_SCHEMA_VERSION:
        raise InvalidCalculationContextError(
            "Versão de schema de cálculo não suportada.",
            error_code=ERROR_SCHEMA_UNSUPPORTED,
        )

    # Trabalha exclusivamente sobre snapshots locais.
    table_snapshot = copy.deepcopy(context.table_record)
    rows_snapshot = copy.deepcopy(context.normalized_rows)
    tax_snapshot = copy.deepcopy(context.tax_config) if context.tax_config is not None else None
    coverage_snapshot = _coverage_from_sources(
        coverage_table=copy.deepcopy(context.coverage_table) if context.coverage_table is not None else None,
        table_record=table_snapshot,
    )

    if not rows_snapshot:
        raise InvalidCalculationContextError(
            "Arquivo operacional sem linhas normalizadas.",
            error_code=ERROR_OPERATIONAL_FILE_EMPTY,
        )
    if not _table_has_prepared_data(table_snapshot):
        raise InvalidCalculationContextError(
            "Tabela sem dados de frete preparados para cálculo.",
            error_code=ERROR_TABLE_DATA_MISSING,
        )

    # Índices preparados uma vez por execução.
    has_coverage = bool(
        isinstance(coverage_snapshot, dict)
        and isinstance(coverage_snapshot.get("rows"), list)
        and coverage_snapshot.get("rows")
    )
    coverage_index = build_coverage_index(coverage_snapshot or {"rows": []})
    pricing_index = build_freight_pricing_index(table_snapshot)
    accessorial_fees = (
        table_snapshot.get("accessorial_fees")
        if isinstance(table_snapshot.get("accessorial_fees"), list)
        else []
    )

    results: list[dict] = []
    for row in rows_snapshot:
        source_row = row if isinstance(row, dict) else {}
        try:
            raw = _calculate_expected_freight_row(
                source_row,
                coverage_index=coverage_index,
                pricing_index=pricing_index,
                has_coverage=has_coverage,
                accessorial_fees=accessorial_fees,
                tax_config=tax_snapshot,
            )
            results.append(_normalize_row_result(raw, source_row))
        except UnexpectedSingleTableCalculationError as exc:
            if exc.comparison_id is None:
                exc.comparison_id = context.comparison_id
            if exc.table_id is None:
                exc.table_id = context.table_id
            if exc.slot_number is None:
                exc.slot_number = context.slot_number
            if exc.row_index is None and isinstance(source_row, dict):
                exc.row_index = source_row.get("row_index")
            logger.exception(
                "agente_compara_single_table_unexpected comparison_id=%s table_id=%s slot=%s "
                "row_index=%s exception_type=%s",
                exc.comparison_id,
                exc.table_id,
                exc.slot_number,
                exc.row_index,
                exc.exception_type or type(exc).__name__,
            )
            raise
        except AgenteComparaCalculationError:
            raise
        except Exception as exc:  # noqa: BLE001 - convertido em erro sistêmico explícito
            _raise_unexpected_row_failure(context=context, source_row=source_row, exc=exc)

    # Ordem oficial do contrato: crescente por row_index (não ordem física de entrada).
    results.sort(
        key=lambda item: (
            item.get("row_index") is None,
            item.get("row_index") if isinstance(item.get("row_index"), int) else 10**9,
        )
    )

    calculated_count = sum(1 for item in results if item.get("status") == STATUS_CALCULATED)
    error_count = len(results) - calculated_count
    if calculated_count + error_count != len(results):
        raise UnexpectedSingleTableCalculationError(
            "Contagens de resultado incoerentes após o cálculo unitário.",
            comparison_id=context.comparison_id,
            table_id=context.table_id,
            slot_number=context.slot_number,
            exception_type="incoherent_counts",
        )

    total_calculated = round(
        sum(
            float(item["calculated_freight"])
            for item in results
            if item.get("status") == STATUS_CALCULATED and item.get("calculated_freight") is not None
        ),
        2,
    )
    duration_ms = int((time.perf_counter() - started) * 1000)

    payload = {
        "schema_version": SINGLE_TABLE_CALCULATION_SCHEMA_VERSION,
        "comparison_id": context.comparison_id,
        "table_id": context.table_id,
        "temp_table_id": context.temp_table_id,
        "slot_number": context.slot_number,
        "carrier_name": context.carrier_name,
        "row_count": len(results),
        "calculated_count": calculated_count,
        "error_count": error_count,
        "results": results,
        "summary": {
            "total_calculated_freight": total_calculated,
            "calculated_count": calculated_count,
            "error_count": error_count,
        },
        "duration_ms": duration_ms,
    }
    if context.execution_id:
        payload["execution_id"] = context.execution_id

    logger.info(
        "agente_compara_single_table_calc comparison_id=%s table_id=%s slot=%s "
        "row_count=%s calculated_count=%s error_count=%s duration_ms=%s",
        context.comparison_id,
        context.table_id,
        context.slot_number,
        payload["row_count"],
        calculated_count,
        error_count,
        duration_ms,
    )
    return payload


# Alias interno estável para orquestração futura (Etapa 4).
calculate_single_table_freight = calculate_single_table
