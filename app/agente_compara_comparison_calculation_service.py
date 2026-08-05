"""
Orquestrador multitabela em memória do AgenteCompara (Etapa 4).

Coordena o cálculo isolado de 2 tabelas obrigatórias e, quando confirmada,
da 3ª tabela opcional — reutilizando exclusivamente o motor unitário da Etapa 3.

Sem rota Flask, sem frontend, sem persistência, sem Gemini, sem billing,
sem ranking e sem máquina de estados de cálculo.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.agente_compara_calculation_service import (
    STATUS_AMBIGUOUS_COVERAGE_MAPPING,
    STATUS_CALCULATED,
    STATUS_CALCULATED_WITH_WARNINGS,
    STATUS_INCOMPLETE,
    STATUS_INVALID_INVOICE_VALUE,
    STATUS_INVALID_WEIGHT,
    STATUS_MISSING_COVERAGE_MAPPING,
    STATUS_MISSING_FREIGHT_RULE,
    STATUS_UNSUPPORTED_PRICING_MODEL,
    AgenteComparaCalculationError,
    InvalidCalculationContextError,
    SingleTableCalculationContext,
    TableOwnershipError,
    UnexpectedSingleTableCalculationError,
    build_single_table_calculation_context,
    calculate_single_table,
)
from app.agente_compara_comparison_state import (
    TABLE_STATUS_CONFIRMED,
    get_comparison_state,
    get_comparison_tax_config,
    get_table_by_slot,
)

logger = logging.getLogger(__name__)

MULTI_TABLE_CALCULATION_SCHEMA_VERSION = 1
COMPACT_COMPARISON_RESULT_SCHEMA_VERSION = 2

ERROR_COMPARISON_NOT_FOUND = "agente_compara_multi_table_comparison_not_found"
ERROR_IDENTITY_MISMATCH = "agente_compara_multi_table_identity_mismatch"
ERROR_INVALID_CONTEXT = "agente_compara_multi_table_invalid_context"
ERROR_SCHEMA_UNSUPPORTED = "agente_compara_multi_table_schema_unsupported"
ERROR_TABLE_REQUIRED = "agente_compara_multi_table_table_required"
ERROR_TABLE_NOT_CONFIRMED = "agente_compara_multi_table_table_not_confirmed"
ERROR_DUPLICATE_SLOT = "agente_compara_multi_table_duplicate_slot"
ERROR_DUPLICATE_TABLE_ID = "agente_compara_multi_table_duplicate_table_id"
ERROR_DUPLICATE_TEMP_TABLE_ID = "agente_compara_multi_table_duplicate_temp_table_id"
ERROR_OPERATIONAL_FILE = "agente_compara_multi_table_operational_file_invalid"
ERROR_INVARIANT = "agente_compara_multi_table_invariant"
ERROR_UNEXPECTED = "agente_compara_multi_table_unexpected"
ERROR_SERIALIZATION = "agente_compara_multi_table_serialization"

_ALLOWED_DOMAIN_STATUSES = frozenset(
    {
        STATUS_CALCULATED,
        STATUS_CALCULATED_WITH_WARNINGS,
        STATUS_INCOMPLETE,
        STATUS_MISSING_COVERAGE_MAPPING,
        STATUS_AMBIGUOUS_COVERAGE_MAPPING,
        STATUS_MISSING_FREIGHT_RULE,
        STATUS_INVALID_WEIGHT,
        STATUS_INVALID_INVOICE_VALUE,
        STATUS_UNSUPPORTED_PRICING_MODEL,
    }
)

_COMPLETE_COMPARABLE_STATUSES = frozenset(
    {
        STATUS_CALCULATED,
        STATUS_CALCULATED_WITH_WARNINGS,
    }
)

_STATUSES_WITH_PUBLIC_FREIGHT = frozenset(
    {
        STATUS_CALCULATED,
        STATUS_CALCULATED_WITH_WARNINGS,
        STATUS_INCOMPLETE,
    }
)

_FORBIDDEN_COMPARATIVE_FIELDS = frozenset(
    {
        "valor_frete",
        "charged_freight",
        "expected_freight",
        "freight_charged",
        "difference",
        "divergence",
        "overcharged",
        "undercharged",
        "winner",
        "winning_carrier",
        "cheapest_carrier",
        "savings",
        "economy",
        "ranking",
        "recommendation",
    }
)


class AgenteComparaMultiTableCalculationError(Exception):
    """Erro sistêmico do orquestrador multitabela."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        comparison_id: str | None = None,
        table_id: str | None = None,
        slot_number: int | None = None,
        execution_id: str | None = None,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.comparison_id = comparison_id
        self.table_id = table_id
        self.slot_number = slot_number
        self.execution_id = execution_id


class InvalidMultiTableCalculationContextError(AgenteComparaMultiTableCalculationError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str = ERROR_INVALID_CONTEXT,
        comparison_id: str | None = None,
        table_id: str | None = None,
        slot_number: int | None = None,
        execution_id: str | None = None,
    ):
        super().__init__(
            error_code,
            message,
            comparison_id=comparison_id,
            table_id=table_id,
            slot_number=slot_number,
            execution_id=execution_id,
        )


class MultiTableCalculationInvariantError(AgenteComparaMultiTableCalculationError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str = ERROR_INVARIANT,
        comparison_id: str | None = None,
        table_id: str | None = None,
        slot_number: int | None = None,
        execution_id: str | None = None,
    ):
        super().__init__(
            error_code,
            message,
            comparison_id=comparison_id,
            table_id=table_id,
            slot_number=slot_number,
            execution_id=execution_id,
        )


class UnexpectedMultiTableCalculationError(AgenteComparaMultiTableCalculationError):
    def __init__(
        self,
        message: str,
        *,
        comparison_id: str | None = None,
        table_id: str | None = None,
        slot_number: int | None = None,
        execution_id: str | None = None,
        exception_type: str | None = None,
        error_code: str = ERROR_UNEXPECTED,
    ):
        super().__init__(
            error_code,
            message,
            comparison_id=comparison_id,
            table_id=table_id,
            slot_number=slot_number,
            execution_id=execution_id,
        )
        self.exception_type = exception_type


@dataclass(frozen=True)
class MultiTableCalculationContext:
    """Contrato de entrada imutável após a fase de preparação."""

    schema_version: int
    comparison_id: str
    execution_id: str | None
    table_contexts: tuple[SingleTableCalculationContext, ...]
    normalized_rows: tuple[dict, ...]
    row_count: int
    row_index_order: tuple[int, ...]


def _require_non_empty_str(value: Any, field_name: str, *, comparison_id: str | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidMultiTableCalculationContextError(
            f"Campo obrigatório ausente ou inválido: {field_name}.",
            comparison_id=comparison_id,
        )
    return value.strip()


def _is_table_confirmed(entry: dict) -> bool:
    return bool(entry.get("confirmed")) or entry.get("status") == TABLE_STATUS_CONFIRMED


def _resolve_confirmed_tables(state: dict, *, comparison_id: str) -> list[dict]:
    """Resolve tabelas confirmadas: slots 1 e 2 obrigatórios; slot 3 só se confirmado."""
    tables_raw = state.get("tables") if isinstance(state.get("tables"), dict) else {}
    by_slot: dict[int, dict] = {}
    seen_table_ids: set[str] = set()
    seen_temp_ids: set[str] = set()

    for entry in tables_raw.values():
        if not isinstance(entry, dict):
            continue
        try:
            slot = int(entry.get("slot_number"))
        except (TypeError, ValueError) as exc:
            raise InvalidMultiTableCalculationContextError(
                "Slot da tabela inválido no estado da comparação.",
                error_code=ERROR_INVALID_CONTEXT,
                comparison_id=comparison_id,
            ) from exc
        if slot < 1 or slot > 3:
            raise InvalidMultiTableCalculationContextError(
                "slot_number fora do intervalo permitido.",
                comparison_id=comparison_id,
                slot_number=slot,
            )
        if slot in by_slot:
            raise InvalidMultiTableCalculationContextError(
                "Slots duplicados no estado da comparação.",
                error_code=ERROR_DUPLICATE_SLOT,
                comparison_id=comparison_id,
                slot_number=slot,
            )
        table_id = (entry.get("table_id") or "").strip()
        if not table_id:
            raise InvalidMultiTableCalculationContextError(
                "table_id ausente em entrada de tabela.",
                comparison_id=comparison_id,
                slot_number=slot,
            )
        if table_id in seen_table_ids:
            raise InvalidMultiTableCalculationContextError(
                "table_id duplicado no estado da comparação.",
                error_code=ERROR_DUPLICATE_TABLE_ID,
                comparison_id=comparison_id,
                table_id=table_id,
                slot_number=slot,
            )
        seen_table_ids.add(table_id)

        temp_table_id = (entry.get("temp_table_id") or "").strip()
        if _is_table_confirmed(entry):
            if not temp_table_id:
                raise InvalidMultiTableCalculationContextError(
                    "Tabela confirmada sem temp_table_id.",
                    comparison_id=comparison_id,
                    table_id=table_id,
                    slot_number=slot,
                )
            if temp_table_id in seen_temp_ids:
                raise InvalidMultiTableCalculationContextError(
                    "temp_table_id duplicado no estado da comparação.",
                    error_code=ERROR_DUPLICATE_TEMP_TABLE_ID,
                    comparison_id=comparison_id,
                    table_id=table_id,
                    slot_number=slot,
                )
            seen_temp_ids.add(temp_table_id)
            carrier = (entry.get("carrier_name") or "").strip()
            if not carrier:
                raise InvalidMultiTableCalculationContextError(
                    "carrier_name é obrigatório para tabela confirmada.",
                    comparison_id=comparison_id,
                    table_id=table_id,
                    slot_number=slot,
                )
        by_slot[slot] = entry

    for required_slot in (1, 2):
        entry = by_slot.get(required_slot)
        if entry is None:
            # fallback via helper oficial
            entry = get_table_by_slot(state, required_slot)
        if entry is None:
            raise InvalidMultiTableCalculationContextError(
                f"Tabela {required_slot} é obrigatória e está ausente.",
                error_code=ERROR_TABLE_REQUIRED,
                comparison_id=comparison_id,
                slot_number=required_slot,
            )
        if not _is_table_confirmed(entry):
            raise InvalidMultiTableCalculationContextError(
                f"Tabela {required_slot} precisa estar confirmada.",
                error_code=ERROR_TABLE_NOT_CONFIRMED,
                comparison_id=comparison_id,
                table_id=(entry.get("table_id") or None),
                slot_number=required_slot,
            )
        by_slot[required_slot] = entry

    selected: list[dict] = [by_slot[1], by_slot[2]]

    slot3 = by_slot.get(3)
    if slot3 is None:
        slot3 = get_table_by_slot(state, 3)
    if slot3 is not None and _is_table_confirmed(slot3):
        # Revalidar unicidade de temp_table_id já coberta no loop; garantir carrier.
        temp3 = (slot3.get("temp_table_id") or "").strip()
        if not temp3:
            raise InvalidMultiTableCalculationContextError(
                "Tabela 3 confirmada sem temp_table_id.",
                comparison_id=comparison_id,
                table_id=(slot3.get("table_id") or None),
                slot_number=3,
            )
        carrier3 = (slot3.get("carrier_name") or "").strip()
        if not carrier3:
            raise InvalidMultiTableCalculationContextError(
                "carrier_name é obrigatório para tabela confirmada.",
                comparison_id=comparison_id,
                table_id=(slot3.get("table_id") or None),
                slot_number=3,
            )
        selected.append(slot3)
    # Slot 3 presente mas não confirmado: ignorado (table_count=2).

    if len(selected) < 2 or len(selected) > 3:
        raise InvalidMultiTableCalculationContextError(
            "Quantidade de tabelas confirmadas inválida para comparação.",
            comparison_id=comparison_id,
        )

    selected.sort(key=lambda item: int(item.get("slot_number") or 0))
    return selected


def _build_row_index_map(normalized_rows: list[dict], *, comparison_id: str) -> tuple[dict[int, dict], tuple[int, ...]]:
    if not isinstance(normalized_rows, list) or not normalized_rows:
        raise InvalidMultiTableCalculationContextError(
            "Arquivo operacional sem linhas normalizadas.",
            error_code=ERROR_OPERATIONAL_FILE,
            comparison_id=comparison_id,
        )
    index: dict[int, dict] = {}
    for item in normalized_rows:
        if not isinstance(item, dict):
            raise InvalidMultiTableCalculationContextError(
                "Linhas normalizadas inválidas no arquivo operacional.",
                error_code=ERROR_OPERATIONAL_FILE,
                comparison_id=comparison_id,
            )
        raw_idx = item.get("row_index")
        if not isinstance(raw_idx, int) or isinstance(raw_idx, bool) or raw_idx < 1:
            raise InvalidMultiTableCalculationContextError(
                "row_index inválido no arquivo operacional.",
                error_code=ERROR_OPERATIONAL_FILE,
                comparison_id=comparison_id,
            )
        if raw_idx in index:
            raise InvalidMultiTableCalculationContextError(
                "row_index duplicado no arquivo operacional.",
                error_code=ERROR_OPERATIONAL_FILE,
                comparison_id=comparison_id,
            )
        index[raw_idx] = item
    order = tuple(sorted(index.keys()))
    return index, order


def _lookup_table_record(
    table_records: dict[str, dict] | None,
    *,
    table_id: str,
    temp_table_id: str,
) -> dict | None:
    if not isinstance(table_records, dict):
        return None
    if table_id in table_records and isinstance(table_records[table_id], dict):
        return table_records[table_id]
    if temp_table_id in table_records and isinstance(table_records[temp_table_id], dict):
        return table_records[temp_table_id]
    return None


def _wrap_unit_context_error(
    exc: BaseException,
    *,
    comparison_id: str,
    table_id: str | None = None,
    slot_number: int | None = None,
    execution_id: str | None = None,
) -> AgenteComparaMultiTableCalculationError:
    if isinstance(exc, (InvalidCalculationContextError, TableOwnershipError)):
        code = getattr(exc, "error_code", ERROR_INVALID_CONTEXT) or ERROR_INVALID_CONTEXT
        wrapped = InvalidMultiTableCalculationContextError(
            getattr(exc, "message", None) or str(exc) or "Contexto unitário inválido.",
            error_code=code if code.startswith("agente_compara_") else ERROR_INVALID_CONTEXT,
            comparison_id=comparison_id,
            table_id=table_id,
            slot_number=slot_number,
            execution_id=execution_id,
        )
        return wrapped
    if isinstance(exc, AgenteComparaCalculationError):
        wrapped = InvalidMultiTableCalculationContextError(
            getattr(exc, "message", None) or str(exc) or "Falha na preparação do contexto unitário.",
            error_code=getattr(exc, "error_code", ERROR_INVALID_CONTEXT) or ERROR_INVALID_CONTEXT,
            comparison_id=comparison_id,
            table_id=table_id,
            slot_number=slot_number,
            execution_id=execution_id,
        )
        return wrapped
    wrapped = UnexpectedMultiTableCalculationError(
        "Falha inesperada na preparação do contexto multitabela.",
        comparison_id=comparison_id,
        table_id=table_id,
        slot_number=slot_number,
        execution_id=execution_id,
        exception_type=type(exc).__name__,
    )
    return wrapped


def build_multi_table_calculation_context(
    *,
    comparison_id: str,
    comparison_state: dict | None = None,
    session_obj=None,
    normalized_rows: list[dict] | None = None,
    table_records: dict[str, dict] | None = None,
    tax_config: dict | None = None,
    coverage_table: dict | list | None = None,
    schema_version: int = MULTI_TABLE_CALCULATION_SCHEMA_VERSION,
    execution_id: str | None = None,
    ttl_hours: int | None = None,
) -> MultiTableCalculationContext:
    """FASE A — resolve tabelas, valida cenário e monta todos os contextos unitários.

    Não executa ``calculate_single_table``.
    """
    if schema_version != MULTI_TABLE_CALCULATION_SCHEMA_VERSION:
        raise InvalidMultiTableCalculationContextError(
            "Versão de schema multitabela não suportada.",
            error_code=ERROR_SCHEMA_UNSUPPORTED,
        )

    comparison_id = _require_non_empty_str(comparison_id, "comparison_id")
    resolved_execution_id = (
        execution_id.strip() if isinstance(execution_id, str) and execution_id.strip() else None
    )

    state = comparison_state
    if state is None and session_obj is not None:
        state = get_comparison_state(session_obj)
    if not isinstance(state, dict):
        raise InvalidMultiTableCalculationContextError(
            "Comparação não encontrada.",
            error_code=ERROR_COMPARISON_NOT_FOUND,
            comparison_id=comparison_id,
            execution_id=resolved_execution_id,
        )

    state_comparison_id = (state.get("comparison_id") or "").strip()
    if state_comparison_id != comparison_id:
        raise InvalidMultiTableCalculationContextError(
            "comparison_id não corresponde à comparação ativa.",
            error_code=ERROR_IDENTITY_MISMATCH,
            comparison_id=comparison_id,
            execution_id=resolved_execution_id,
        )

    confirmed_tables = _resolve_confirmed_tables(state, comparison_id=comparison_id)

    rows = normalized_rows
    if rows is None and isinstance(table_records, dict):
        for record in table_records.values():
            if not isinstance(record, dict):
                continue
            batch = record.get("audit_batch") if isinstance(record.get("audit_batch"), dict) else {}
            candidate = batch.get("normalized_rows")
            if isinstance(candidate, list) and candidate:
                rows = candidate
                break

    row_index_map, row_index_order = _build_row_index_map(
        list(rows) if isinstance(rows, list) else [],
        comparison_id=comparison_id,
    )
    # Snapshot imutável das linhas operacionais (metadados autoritativos).
    rows_snapshot = tuple(copy.deepcopy(row_index_map[idx]) for idx in row_index_order)

    resolved_tax = tax_config
    if resolved_tax is None:
        resolved_tax = get_comparison_tax_config(state)

    table_contexts: list[SingleTableCalculationContext] = []
    for entry in confirmed_tables:
        table_id = (entry.get("table_id") or "").strip()
        temp_table_id = (entry.get("temp_table_id") or "").strip()
        slot_number = int(entry.get("slot_number"))
        carrier_name = (entry.get("carrier_name") or "").strip()
        record = _lookup_table_record(
            table_records,
            table_id=table_id,
            temp_table_id=temp_table_id,
        )
        try:
            unit_ctx = build_single_table_calculation_context(
                comparison_id=comparison_id,
                table_id=table_id,
                temp_table_id=temp_table_id,
                slot_number=slot_number,
                carrier_name=carrier_name,
                comparison_state=state,
                table_record=record,
                normalized_rows=list(copy.deepcopy(row) for row in rows_snapshot),
                tax_config=resolved_tax,
                coverage_table=coverage_table,
                schema_version=1,
                execution_id=resolved_execution_id,
                ttl_hours=ttl_hours,
            )
        except AgenteComparaMultiTableCalculationError:
            raise
        except Exception as exc:  # noqa: BLE001 - convertido em erro de preparação
            wrapped = _wrap_unit_context_error(
                exc,
                comparison_id=comparison_id,
                table_id=table_id,
                slot_number=slot_number,
                execution_id=resolved_execution_id,
            )
            raise wrapped from exc
        table_contexts.append(unit_ctx)

    if len(table_contexts) < 2 or len(table_contexts) > 3:
        raise InvalidMultiTableCalculationContextError(
            "Quantidade de contextos unitários inválida.",
            comparison_id=comparison_id,
            execution_id=resolved_execution_id,
        )

    # Ordenação determinística por slot (já vem ordenada; reforço explícito).
    table_contexts.sort(key=lambda ctx: ctx.slot_number)

    return MultiTableCalculationContext(
        schema_version=MULTI_TABLE_CALCULATION_SCHEMA_VERSION,
        comparison_id=comparison_id,
        execution_id=resolved_execution_id,
        table_contexts=tuple(table_contexts),
        normalized_rows=rows_snapshot,
        row_count=len(rows_snapshot),
        row_index_order=row_index_order,
    )


def _assert_finite_number(value: Any, *, path: str, comparison_id: str | None, execution_id: str | None) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise MultiTableCalculationInvariantError(
                f"Valor numérico não finito em {path}.",
                error_code=ERROR_SERIALIZATION,
                comparison_id=comparison_id,
                execution_id=execution_id,
            )
        return
    if isinstance(value, Decimal):
        raise MultiTableCalculationInvariantError(
            f"Decimal não convertido em {path}.",
            error_code=ERROR_SERIALIZATION,
            comparison_id=comparison_id,
            execution_id=execution_id,
        )


def _assert_json_safe(value: Any, *, path: str = "$", comparison_id: str | None = None, execution_id: str | None = None) -> None:
    if value is None or isinstance(value, (str, int, bool)):
        return
    if isinstance(value, float):
        _assert_finite_number(value, path=path, comparison_id=comparison_id, execution_id=execution_id)
        return
    if isinstance(value, Decimal):
        _assert_finite_number(value, path=path, comparison_id=comparison_id, execution_id=execution_id)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise MultiTableCalculationInvariantError(
                    f"Chave não string em {path}.",
                    error_code=ERROR_SERIALIZATION,
                    comparison_id=comparison_id,
                    execution_id=execution_id,
                )
            _assert_json_safe(
                item,
                path=f"{path}.{key}",
                comparison_id=comparison_id,
                execution_id=execution_id,
            )
        return
    if isinstance(value, list):
        for idx, item in enumerate(value):
            _assert_json_safe(
                item,
                path=f"{path}[{idx}]",
                comparison_id=comparison_id,
                execution_id=execution_id,
            )
        return
    if isinstance(value, (set, bytes, bytearray, memoryview)):
        raise MultiTableCalculationInvariantError(
            f"Tipo não serializável em {path}.",
            error_code=ERROR_SERIALIZATION,
            comparison_id=comparison_id,
            execution_id=execution_id,
        )
    if isinstance(value, BaseException):
        raise MultiTableCalculationInvariantError(
            f"Exceção embutida em {path}.",
            error_code=ERROR_SERIALIZATION,
            comparison_id=comparison_id,
            execution_id=execution_id,
        )
    raise MultiTableCalculationInvariantError(
        f"Objeto não serializável em {path}.",
        error_code=ERROR_SERIALIZATION,
        comparison_id=comparison_id,
        execution_id=execution_id,
    )


def _assert_no_forbidden_fields(payload: Any, *, comparison_id: str | None = None, execution_id: str | None = None) -> None:
    stack: list[Any] = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                if key in _FORBIDDEN_COMPARATIVE_FIELDS:
                    raise MultiTableCalculationInvariantError(
                        f"Campo proibido presente no resultado: {key}.",
                        comparison_id=comparison_id,
                        execution_id=execution_id,
                    )
                stack.append(value)
        elif isinstance(current, list):
            stack.extend(current)


def validate_comparison_result_serializable(
    payload: dict,
    *,
    comparison_id: str | None = None,
    execution_id: str | None = None,
) -> None:
    """Garante serialização JSON estrita (sem NaN/Infinity) e ausência de campos proibidos."""
    _assert_json_safe(payload, comparison_id=comparison_id, execution_id=execution_id)
    _assert_no_forbidden_fields(payload, comparison_id=comparison_id, execution_id=execution_id)
    try:
        json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise MultiTableCalculationInvariantError(
            "Resultado comparativo não serializável em JSON.",
            error_code=ERROR_SERIALIZATION,
            comparison_id=comparison_id,
            execution_id=execution_id,
        ) from exc


def validate_single_table_result(
    unit_result: dict,
    *,
    context: SingleTableCalculationContext,
    expected_row_indexes: set[int],
    expected_row_count: int,
) -> dict[int, dict]:
    """Valida invariantes do resultado unitário antes da consolidação."""
    if not isinstance(unit_result, dict):
        raise MultiTableCalculationInvariantError(
            "Resultado unitário estruturalmente inválido.",
            comparison_id=context.comparison_id,
            table_id=context.table_id,
            slot_number=context.slot_number,
            execution_id=context.execution_id,
        )

    if unit_result.get("schema_version") != 1:
        raise MultiTableCalculationInvariantError(
            "schema_version unitário não suportado.",
            comparison_id=context.comparison_id,
            table_id=context.table_id,
            slot_number=context.slot_number,
            execution_id=context.execution_id,
        )
    if unit_result.get("comparison_id") != context.comparison_id:
        raise MultiTableCalculationInvariantError(
            "comparison_id divergente no resultado unitário.",
            comparison_id=context.comparison_id,
            table_id=context.table_id,
            slot_number=context.slot_number,
            execution_id=context.execution_id,
        )
    if unit_result.get("table_id") != context.table_id:
        raise MultiTableCalculationInvariantError(
            "table_id divergente no resultado unitário.",
            comparison_id=context.comparison_id,
            table_id=context.table_id,
            slot_number=context.slot_number,
            execution_id=context.execution_id,
        )
    if unit_result.get("temp_table_id") != context.temp_table_id:
        raise MultiTableCalculationInvariantError(
            "temp_table_id divergente no resultado unitário.",
            comparison_id=context.comparison_id,
            table_id=context.table_id,
            slot_number=context.slot_number,
            execution_id=context.execution_id,
        )
    if int(unit_result.get("slot_number")) != int(context.slot_number):
        raise MultiTableCalculationInvariantError(
            "slot_number divergente no resultado unitário.",
            comparison_id=context.comparison_id,
            table_id=context.table_id,
            slot_number=context.slot_number,
            execution_id=context.execution_id,
        )
    if (unit_result.get("carrier_name") or "").strip() != context.carrier_name:
        raise MultiTableCalculationInvariantError(
            "carrier_name divergente no resultado unitário.",
            comparison_id=context.comparison_id,
            table_id=context.table_id,
            slot_number=context.slot_number,
            execution_id=context.execution_id,
        )

    results = unit_result.get("results")
    if not isinstance(results, list):
        raise MultiTableCalculationInvariantError(
            "results unitário inválido.",
            comparison_id=context.comparison_id,
            table_id=context.table_id,
            slot_number=context.slot_number,
            execution_id=context.execution_id,
        )
    if len(results) != expected_row_count or unit_result.get("row_count") != expected_row_count:
        raise MultiTableCalculationInvariantError(
            "row_count divergente no resultado unitário.",
            comparison_id=context.comparison_id,
            table_id=context.table_id,
            slot_number=context.slot_number,
            execution_id=context.execution_id,
        )

    calculated_count = unit_result.get("calculated_count")
    calculated_with_warnings_count = unit_result.get("calculated_with_warnings_count", 0)
    incomplete_count = unit_result.get("incomplete_count", 0)
    error_count = unit_result.get("error_count")
    if not isinstance(calculated_count, int) or not isinstance(error_count, int):
        raise MultiTableCalculationInvariantError(
            "Contagens unitárias inválidas.",
            comparison_id=context.comparison_id,
            table_id=context.table_id,
            slot_number=context.slot_number,
            execution_id=context.execution_id,
        )
    if not isinstance(calculated_with_warnings_count, int):
        calculated_with_warnings_count = 0
    if not isinstance(incomplete_count, int):
        incomplete_count = 0
    if (
        calculated_count
        + calculated_with_warnings_count
        + incomplete_count
        + error_count
        != expected_row_count
    ):
        raise MultiTableCalculationInvariantError(
            "Contagens unitárias incoerentes.",
            comparison_id=context.comparison_id,
            table_id=context.table_id,
            slot_number=context.slot_number,
            execution_id=context.execution_id,
        )

    by_row: dict[int, dict] = {}
    for item in results:
        if not isinstance(item, dict):
            raise MultiTableCalculationInvariantError(
                "Linha de resultado unitário inválida.",
                comparison_id=context.comparison_id,
                table_id=context.table_id,
                slot_number=context.slot_number,
                execution_id=context.execution_id,
            )
        row_index = item.get("row_index")
        if not isinstance(row_index, int) or isinstance(row_index, bool) or row_index < 1:
            raise MultiTableCalculationInvariantError(
                "row_index inválido no resultado unitário.",
                comparison_id=context.comparison_id,
                table_id=context.table_id,
                slot_number=context.slot_number,
                execution_id=context.execution_id,
            )
        if row_index in by_row:
            raise MultiTableCalculationInvariantError(
                "row_index duplicado no resultado unitário.",
                comparison_id=context.comparison_id,
                table_id=context.table_id,
                slot_number=context.slot_number,
                execution_id=context.execution_id,
            )
        status = item.get("status")
        if status not in _ALLOWED_DOMAIN_STATUSES:
            raise MultiTableCalculationInvariantError(
                "Status de domínio não permitido no resultado unitário.",
                comparison_id=context.comparison_id,
                table_id=context.table_id,
                slot_number=context.slot_number,
                execution_id=context.execution_id,
            )
        freight = item.get("calculated_freight")
        if status in _STATUSES_WITH_PUBLIC_FREIGHT:
            if status in _COMPLETE_COMPARABLE_STATUSES and freight is None:
                raise MultiTableCalculationInvariantError(
                    "calculated_freight ausente em linha calculada.",
                    comparison_id=context.comparison_id,
                    table_id=context.table_id,
                    slot_number=context.slot_number,
                    execution_id=context.execution_id,
                )
            if freight is not None and isinstance(freight, float) and (
                math.isnan(freight) or math.isinf(freight)
            ):
                raise MultiTableCalculationInvariantError(
                    "calculated_freight não finito no resultado unitário.",
                    error_code=ERROR_SERIALIZATION,
                    comparison_id=context.comparison_id,
                    table_id=context.table_id,
                    slot_number=context.slot_number,
                    execution_id=context.execution_id,
                )
        elif freight is not None:
            raise MultiTableCalculationInvariantError(
                "calculated_freight deve ser nulo em erro de domínio.",
                comparison_id=context.comparison_id,
                table_id=context.table_id,
                slot_number=context.slot_number,
                execution_id=context.execution_id,
            )
        by_row[row_index] = item

    actual_indexes = set(by_row.keys())
    if actual_indexes != expected_row_indexes:
        missing = expected_row_indexes - actual_indexes
        extra = actual_indexes - expected_row_indexes
        if missing:
            raise MultiTableCalculationInvariantError(
                "row_index ausente no resultado unitário.",
                comparison_id=context.comparison_id,
                table_id=context.table_id,
                slot_number=context.slot_number,
                execution_id=context.execution_id,
            )
        if extra:
            raise MultiTableCalculationInvariantError(
                "row_index extra no resultado unitário.",
                comparison_id=context.comparison_id,
                table_id=context.table_id,
                slot_number=context.slot_number,
                execution_id=context.execution_id,
            )
        raise MultiTableCalculationInvariantError(
            "Conjunto de row_index divergente no resultado unitário.",
            comparison_id=context.comparison_id,
            table_id=context.table_id,
            slot_number=context.slot_number,
            execution_id=context.execution_id,
        )

    _assert_json_safe(
        unit_result,
        comparison_id=context.comparison_id,
        execution_id=context.execution_id,
    )
    return by_row


def consolidate_results_by_row_index(
    *,
    context: MultiTableCalculationContext,
    unit_results: list[dict],
    results_by_row_per_table: list[dict[int, dict]],
) -> list[dict]:
    """Consolida células por row_index usando metadados do arquivo operacional."""
    source_by_index = {
        int(row["row_index"]): row for row in context.normalized_rows if isinstance(row, dict)
    }
    comparative_rows: list[dict] = []
    for row_index in context.row_index_order:
        source = source_by_index[row_index]
        table_results: dict[str, dict] = {}
        for unit_result, by_row in zip(unit_results, results_by_row_per_table):
            table_id = unit_result["table_id"]
            cell = by_row[row_index]
            memory = copy.deepcopy(cell.get("calculation_memory")) if isinstance(cell.get("calculation_memory"), dict) else None
            if isinstance(memory, dict):
                memory["table_id"] = table_id
                memory["slot_number"] = unit_result["slot_number"]
                memory["carrier_name"] = unit_result["carrier_name"]
                memory["row_index"] = row_index
            table_results[table_id] = {
                "table_id": table_id,
                "carrier_name": unit_result["carrier_name"],
                "slot_number": unit_result["slot_number"],
                "calculated_freight": cell.get("calculated_freight"),
                "status": cell.get("status"),
                "raw_status": cell.get("raw_status"),
                "final_status": cell.get("final_status") or cell.get("status"),
                "completeness_status": cell.get("completeness_status"),
                "completeness": copy.deepcopy(cell.get("completeness")),
                "is_partial_value": bool(cell.get("is_partial_value")),
                "blocking_issues": copy.deepcopy(cell.get("blocking_issues") or []),
                "warnings": copy.deepcopy(cell.get("warnings") or []),
                "error": copy.deepcopy(cell.get("error")),
                "components": copy.deepcopy(cell.get("components") or {}),
                "evidence": copy.deepcopy(cell.get("evidence") or {}),
                "calculation_memory": memory,
            }
        row_payload = {
            "row_index": row_index,
            "document_number": source.get("document_number"),
            "destination_city": source.get("destination_city"),
            "destination_uf": source.get("destination_uf"),
            "weight": source.get("audited_weight", source.get("weight")),
            "table_results": table_results,
        }
        invoice_value = source.get("invoice_value")
        if invoice_value is not None:
            row_payload["invoice_value"] = invoice_value
        comparative_rows.append(row_payload)
    return comparative_rows


def _build_memory_ref(*, table_id: str, row_index: int) -> str:
    safe_table = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(table_id or "").strip())
    if not safe_table:
        safe_table = "table"
    return f"{safe_table}:{int(row_index)}"


class _CanonicalSerializer:
    def __init__(self, stats: dict[str, int] | None = None, *, use_identity_cache: bool = True) -> None:
        self.stats = stats
        self.use_identity_cache = use_identity_cache
        self.identity_cache: dict[int, str] = {}

    def _bump(self, metric: str, prefix: str | None = None) -> None:
        if self.stats is None:
            return
        self.stats[metric] = self.stats.get(metric, 0) + 1
        if prefix:
            scoped = f"{prefix}_{metric}"
            self.stats[scoped] = self.stats.get(scoped, 0) + 1

    def render(self, payload: Any, *, prefix: str | None = None) -> str:
        if isinstance(payload, dict):
            payload_id = id(payload)
            if self.use_identity_cache:
                cached = self.identity_cache.get(payload_id)
                if cached is not None:
                    self._bump("identity_hits", prefix)
                    return cached
            self._bump("normalize_calls", prefix)
            rendered = "{" + ",".join(
                f"{json.dumps(str(key), ensure_ascii=False, allow_nan=False)}:{self.render(value, prefix=prefix)}"
                for key, value in sorted(payload.items(), key=lambda item: str(item[0]))
            ) + "}"
            if self.use_identity_cache:
                self.identity_cache[payload_id] = rendered
            return rendered
        if isinstance(payload, (list, tuple)):
            payload_id = id(payload)
            if self.use_identity_cache:
                cached = self.identity_cache.get(payload_id)
                if cached is not None:
                    self._bump("identity_hits", prefix)
                    return cached
            self._bump("normalize_calls", prefix)
            rendered = "[" + ",".join(self.render(value, prefix=prefix) for value in payload) + "]"
            if self.use_identity_cache:
                self.identity_cache[payload_id] = rendered
            return rendered
        self._bump("json_dumps", prefix)
        return json.dumps(payload, ensure_ascii=False, allow_nan=False, default=str)


def _canonical_catalog_json(payload: Any, *, serializer: _CanonicalSerializer, prefix: str | None = None) -> str:
    return serializer.render(payload, prefix=prefix)


def _catalog_hash_from_json(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _stable_catalog_key(prefix: str, payload: Any) -> str:
    return f"{prefix}:{_catalog_hash_from_json(_canonical_catalog_json(payload))}"


def _sanitize_calculation_memory(memory: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(memory, dict):
        return None
    sanitized = {
        key: value
        for key, value in memory.items()
        if key not in {"components", "warnings", "blocking_issues", "evidence"}
    }
    return sanitized


def _drop_empty_values(payload: Any) -> Any:
    if isinstance(payload, dict):
        cleaned: dict[str, Any] = {}
        for key, value in payload.items():
            normalized = _drop_empty_values(value)
            if normalized in (None, "", [], {}):
                continue
            cleaned[key] = normalized
        return cleaned
    if isinstance(payload, list):
        cleaned_list = [_drop_empty_values(value) for value in payload]
        return [value for value in cleaned_list if value not in (None, "", [], {})]
    return payload


def _sorted_unique_list(values: list[Any]) -> list[Any]:
    unique: list[Any] = []
    seen: set[str] = set()
    for value in values:
        key = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, default=str)
        if key in seen:
            continue
        seen.add(key)
        unique.append(value)
    return sorted(
        unique,
        key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, allow_nan=False, default=str),
    )


def _normalize_evidence_for_catalog(
    evidence: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not isinstance(evidence, dict):
        return None, None

    common_fields = {
        "source",
        "table",
        "rule",
        "page",
        "snippet",
        "excerpt",
        "trecho",
        "quote",
        "pricing_type",
        "calculation_basis",
        "weight_band",
        "weight_basis",
        "taxes_applied",
        "accessorials_applied",
        "freight_region",
        "coverage_table",
        "coverage_source_ref",
        "coverage_classification",
        "coverage_reference",
        "coverage_rule_ref",
        "region",
        "city_uf_match",
    }
    item_fields = {
        "row_index",
        "document_number",
        "destination_city",
        "destination_uf",
        "pricing_lookup_key",
        "pricing_lookup_kind",
        "matched_key",
        "matched_rule",
        "status",
        "total",
        "calculated_freight",
        "reason_code",
        "calculation_details",
        "diagnostic",
    }

    common_payload: dict[str, Any] = {}
    item_payload: dict[str, Any] = {}

    for key, value in evidence.items():
        if key in item_fields:
            item_payload[key] = copy.deepcopy(value)
            continue
        if key == "coverage_table":
            if isinstance(value, dict):
                coverage_meta = {k: copy.deepcopy(v) for k, v in value.items() if k != "rows"}
                coverage_meta = _drop_empty_values(coverage_meta)
                if coverage_meta:
                    common_payload[key] = coverage_meta
            continue
        target = common_payload if key in common_fields else item_payload
        target[key] = copy.deepcopy(value)

    common_payload = _drop_empty_values(common_payload) or {}
    item_payload = _drop_empty_values(item_payload) or {}

    for list_key in ("taxes_applied", "accessorials_applied"):
        if isinstance(common_payload.get(list_key), list):
            common_payload[list_key] = _sorted_unique_list(common_payload[list_key])

    return (common_payload or None), (item_payload or None)


class _CatalogBuilder:
    def __init__(self, *, prefix: str, serializer: _CanonicalSerializer, stats: dict[str, int] | None = None, use_identity_cache: bool = True) -> None:
        self.prefix = prefix
        self.serializer = serializer
        self.stats = stats
        self.use_identity_cache = use_identity_cache
        self.catalog: dict[str, dict[str, Any]] = {}
        self.content_index: dict[str, str] = {}
        self.identity_index: dict[int, str] = {}

    def _bump(self, metric: str) -> None:
        if self.stats is None:
            return
        self.stats[metric] = self.stats.get(metric, 0) + 1
        scoped = f"{self.prefix}_{metric}"
        self.stats[scoped] = self.stats.get(scoped, 0) + 1

    def ref(self, payload: dict[str, Any] | None) -> str | None:
        if not isinstance(payload, dict) or not payload:
            return None
        identity_key = id(payload)
        if self.use_identity_cache:
            existing = self.identity_index.get(identity_key)
            if existing:
                self._bump("identity_hits")
                return existing
        stable_json = _canonical_catalog_json(payload, serializer=self.serializer, prefix=self.prefix)
        existing = self.content_index.get(stable_json)
        if existing:
            if self.use_identity_cache:
                self.identity_index[identity_key] = existing
            self._bump("content_hits")
            return existing
        digest = _catalog_hash_from_json(stable_json)
        self._bump("hash_calls")
        base_ref = f"{self.prefix}:{digest}"
        ref = base_ref
        suffix = 2
        while ref in self.catalog and self.catalog[ref] != payload:
            ref = f"{base_ref}:{suffix}"
            suffix += 1
        self.catalog[ref] = payload
        self.content_index[stable_json] = ref
        if self.use_identity_cache:
            self.identity_index[identity_key] = ref
        return ref


def _catalog_ref(
    builder: _CatalogBuilder,
    *,
    payload: dict[str, Any] | None,
) -> str | None:
    return builder.ref(payload)


def _catalog_message_payload(message: Any, *, severity: str) -> dict[str, Any] | None:
    if isinstance(message, dict):
        code = message.get("code") or message.get("reason_code") or message.get("message")
        text = message.get("message") or message.get("text") or message.get("label") or code
        guidance = message.get("guidance") or message.get("orientation")
    else:
        code = message
        text = message
        guidance = None
    if code is None and text is None:
        return None
    return {
        "code": str(code or text),
        "text": str(text or code),
        "severity": severity,
        "guidance": guidance,
    }


def hydrate_memory_item(item: dict[str, Any], memory_payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    if not isinstance(memory_payload, dict) or int(memory_payload.get("schema_version") or 0) < 3:
        return copy.deepcopy(item)

    tables = memory_payload.get("tables") if isinstance(memory_payload.get("tables"), dict) else {}
    rules = memory_payload.get("rules") if isinstance(memory_payload.get("rules"), dict) else {}
    components = memory_payload.get("components") if isinstance(memory_payload.get("components"), dict) else {}
    evidence_catalog = memory_payload.get("evidence_catalog") if isinstance(memory_payload.get("evidence_catalog"), dict) else {}
    messages = memory_payload.get("messages") if isinstance(memory_payload.get("messages"), dict) else {}

    hydrated = copy.deepcopy(item)
    rule_ref = hydrated.pop("rule_ref", None)
    component_ref = hydrated.pop("component_ref", None)
    component_refs = hydrated.pop("component_refs", None)
    evidence_refs = hydrated.pop("evidence_refs", None)
    item_evidence = hydrated.pop("item_evidence", None)
    warning_codes = hydrated.pop("warning_codes", None)
    blocking_codes = hydrated.pop("blocking_codes", None)
    table_ref = hydrated.get("table_id")

    if isinstance(rule_ref, str) and isinstance(rules.get(rule_ref), dict):
        hydrated["rule"] = copy.deepcopy(rules[rule_ref])
    if isinstance(component_ref, str) and isinstance(components.get(component_ref), dict):
        hydrated["components"] = copy.deepcopy(components[component_ref])
    elif isinstance(component_refs, list):
        hydrated["components"] = [copy.deepcopy(components[ref]) for ref in component_refs if isinstance(components.get(ref), dict)]
    if isinstance(evidence_refs, list):
        merged_evidence: dict[str, Any] = {}
        for ref in evidence_refs:
            payload = evidence_catalog.get(ref)
            if isinstance(payload, dict):
                merged_evidence.update(copy.deepcopy(payload))
        if isinstance(item_evidence, dict):
            merged_evidence.update(copy.deepcopy(item_evidence))
        hydrated["evidence"] = merged_evidence
    elif isinstance(item_evidence, dict):
        hydrated["evidence"] = copy.deepcopy(item_evidence)
    if isinstance(warning_codes, list):
        hydrated["warnings"] = [copy.deepcopy(messages[code]) for code in warning_codes if isinstance(messages.get(code), dict)]
    if isinstance(blocking_codes, list):
        hydrated["blocking_issues"] = [copy.deepcopy(messages[code]) for code in blocking_codes if isinstance(messages.get(code), dict)]
    if isinstance(table_ref, str) and isinstance(tables.get(table_ref), dict):
        hydrated.setdefault("table", copy.deepcopy(tables[table_ref]))
    calculation_memory = hydrated.get("calculation_memory")
    if isinstance(calculation_memory, dict):
        if "components" not in calculation_memory and "components" in hydrated:
            calculation_memory["components"] = copy.deepcopy(hydrated.get("components") or [])
        if "warnings" not in calculation_memory and "warnings" in hydrated:
            calculation_memory["warnings"] = copy.deepcopy(hydrated.get("warnings") or [])
        if "blocking_issues" not in calculation_memory and "blocking_issues" in hydrated:
            calculation_memory["blocking_issues"] = copy.deepcopy(hydrated.get("blocking_issues") or [])
        if "evidence" not in calculation_memory and "evidence" in hydrated:
            calculation_memory["evidence"] = copy.deepcopy(hydrated.get("evidence") or {})
    return hydrated


def compact_comparison_result_for_storage(result: dict, *, _debug_stats: dict[str, int] | None = None) -> dict:
    """Separa mem?rias detalhadas do resultado comparativo para persist?ncia compacta."""
    if not isinstance(result, dict):
        raise MultiTableCalculationInvariantError("Resultado comparativo inv?lido para compacta??o.")

    comparison_id = str(result.get("comparison_id") or "").strip()
    comparative_rows_in = result.get("comparative_rows") if isinstance(result.get("comparative_rows"), list) else []
    results_by_table_in = result.get("results_by_table") if isinstance(result.get("results_by_table"), dict) else {}

    compact_rows = []
    memory_items = {}
    table_builder = _CatalogBuilder(prefix="tbl", serializer=_CanonicalSerializer(_debug_stats), stats=_debug_stats)
    rule_builder = _CatalogBuilder(prefix="rule", serializer=_CanonicalSerializer(_debug_stats), stats=_debug_stats)
    component_builder = _CatalogBuilder(prefix="cmp", serializer=_CanonicalSerializer(_debug_stats), stats=_debug_stats)
    evidence_builder = _CatalogBuilder(prefix="evd", serializer=_CanonicalSerializer(_debug_stats), stats=_debug_stats)
    message_builder = _CatalogBuilder(prefix="msg", serializer=_CanonicalSerializer(_debug_stats, use_identity_cache=False), stats=_debug_stats, use_identity_cache=False)
    per_table_counts = {}

    for row in comparative_rows_in:
        if not isinstance(row, dict):
            continue
        row_index = int(row.get("row_index") or 0)
        compact_table_results = {}
        raw_table_results = row.get("table_results") if isinstance(row.get("table_results"), dict) else {}
        for table_id, cell in raw_table_results.items():
            if not isinstance(cell, dict):
                continue
            memory_ref = _build_memory_ref(table_id=str(table_id), row_index=row_index)
            compact_table_results[str(table_id)] = {
                "table_id": cell.get("table_id") or table_id,
                "carrier_name": cell.get("carrier_name"),
                "slot_number": cell.get("slot_number"),
                "calculated_freight": cell.get("calculated_freight"),
                "status": cell.get("status"),
                "raw_status": cell.get("raw_status"),
                "final_status": cell.get("final_status") or cell.get("status"),
                "completeness_status": cell.get("completeness_status"),
                "is_partial_value": bool(cell.get("is_partial_value")),
                "error": copy.deepcopy(cell.get("error")),
                "memory_ref": memory_ref,
            }
            calculation_memory = _sanitize_calculation_memory(cell.get("calculation_memory"))
            table_ref = _catalog_ref(
                table_builder,
                payload={
                    "table_id": cell.get("table_id") or table_id,
                    "carrier_name": cell.get("carrier_name"),
                    "slot_number": cell.get("slot_number"),
                    "source_table_id": cell.get("table_id") or table_id,
                    "coverage_scope": row.get("destination_uf"),
                },
            )
            rule_ref = _catalog_ref(
                rule_builder,
                payload={
                    "selected_rule": (calculation_memory or {}).get("selected_rule"),
                    "pricing": (calculation_memory or {}).get("pricing"),
                    "rule_source": ((cell.get("evidence") or {}) if isinstance(cell.get("evidence"), dict) else {}).get("source"),
                },
            )
            component_ref = None
            component_refs = []
            components_payload = cell.get("components")
            if isinstance(components_payload, dict):
                component_ref = _catalog_ref(component_builder, payload=components_payload)
            elif isinstance(components_payload, list):
                for component in components_payload:
                    ref = _catalog_ref(component_builder, payload=component if isinstance(component, dict) else None)
                    if ref:
                        component_refs.append(ref)
            evidence_refs = []
            evidence_payload, item_evidence = _normalize_evidence_for_catalog(
                cell.get("evidence") if isinstance(cell.get("evidence"), dict) else None
            )
            if isinstance(evidence_payload, dict):
                ref = _catalog_ref(evidence_builder, payload=evidence_payload)
                if ref:
                    evidence_refs.append(ref)
            warning_codes = []
            for warning in cell.get("warnings") or []:
                ref = _catalog_ref(message_builder, payload=_catalog_message_payload(warning, severity="warning"))
                if ref:
                    warning_codes.append(ref)
            blocking_codes = []
            for blocking in cell.get("blocking_issues") or []:
                ref = _catalog_ref(message_builder, payload=_catalog_message_payload(blocking, severity="blocking"))
                if ref:
                    blocking_codes.append(ref)
            memory_items[memory_ref] = {
                "memory_ref": memory_ref,
                "comparison_id": comparison_id,
                "table_id": table_ref or (cell.get("table_id") or table_id),
                "row_index": row_index,
                "document_number": row.get("document_number"),
                "carrier_name": cell.get("carrier_name"),
                "slot_number": cell.get("slot_number"),
                "status": (calculation_memory or {}).get("status") or cell.get("status"),
                "total": (calculation_memory or {}).get("total", cell.get("calculated_freight")),
                "calculation_memory": calculation_memory,
                "item_evidence": item_evidence or None,
                "rule_ref": rule_ref,
                "component_ref": component_ref,
                "component_refs": component_refs,
                "evidence_refs": evidence_refs,
                "warning_codes": warning_codes,
                "blocking_codes": blocking_codes,
                "reason_code": (cell.get("error") or {}).get("code") if isinstance(cell.get("error"), dict) else None,
            }
        compact_row = {
            "row_index": row_index,
            "document_number": row.get("document_number"),
            "destination_city": row.get("destination_city"),
            "destination_uf": row.get("destination_uf"),
            "weight": row.get("weight"),
            "table_results": compact_table_results,
        }
        if "invoice_value" in row:
            compact_row["invoice_value"] = row.get("invoice_value")
        compact_rows.append(compact_row)

    compact_results_by_table = {}
    for table_id, table_result in results_by_table_in.items():
        if not isinstance(table_result, dict):
            continue
        counts = {
            "calculated_count": int(table_result.get("calculated_count") or 0),
            "calculated_with_warnings_count": int(table_result.get("calculated_with_warnings_count") or 0),
            "incomplete_count": int(table_result.get("incomplete_count") or 0),
            "error_count": int(table_result.get("error_count") or 0),
        }
        per_table_counts[str(table_id)] = counts
        compact_results_by_table[str(table_id)] = {
            "comparison_id": table_result.get("comparison_id"),
            "table_id": table_result.get("table_id") or table_id,
            "temp_table_id": table_result.get("temp_table_id"),
            "slot_number": table_result.get("slot_number"),
            "carrier_name": table_result.get("carrier_name"),
            "row_count": table_result.get("row_count"),
            "calculated_count": counts["calculated_count"],
            "calculated_with_warnings_count": counts["calculated_with_warnings_count"],
            "incomplete_count": counts["incomplete_count"],
            "error_count": counts["error_count"],
            "summary": copy.deepcopy(table_result.get("summary") or {}),
            "duration_ms": table_result.get("duration_ms", 0),
            "schema_version": table_result.get("schema_version"),
        }

    compact_result = dict(result)
    compact_result["schema_version"] = COMPACT_COMPARISON_RESULT_SCHEMA_VERSION
    compact_result["comparative_rows"] = compact_rows
    compact_result["results_by_table"] = compact_results_by_table
    memory_payload = {
        "schema_version": 3,
        "comparison_id": comparison_id,
        "execution_id": result.get("execution_id"),
        "tables": table_builder.catalog,
        "rules": rule_builder.catalog,
        "components": component_builder.catalog,
        "evidence_catalog": evidence_builder.catalog,
        "messages": message_builder.catalog,
        "items": memory_items,
    }
    return {
        "compact_result": compact_result,
        "memory_payload": memory_payload,
        "schema_version": COMPACT_COMPARISON_RESULT_SCHEMA_VERSION,
        "memory_ref_count": len(memory_items),
        "table_counts": per_table_counts,
    }


def calculate_comparison_in_memory(context: MultiTableCalculationContext) -> dict:
    """FASE B — executa o motor unitário por slot e consolida o resultado em memória."""
    started = time.perf_counter()
    if not isinstance(context, MultiTableCalculationContext):
        raise InvalidMultiTableCalculationContextError("Contexto multitabela inválido.")
    if context.schema_version != MULTI_TABLE_CALCULATION_SCHEMA_VERSION:
        raise InvalidMultiTableCalculationContextError(
            "Versão de schema multitabela não suportada.",
            error_code=ERROR_SCHEMA_UNSUPPORTED,
            comparison_id=getattr(context, "comparison_id", None),
            execution_id=getattr(context, "execution_id", None),
        )
    if len(context.table_contexts) < 2 or len(context.table_contexts) > 3:
        raise InvalidMultiTableCalculationContextError(
            "Quantidade de tabelas inválida no contexto.",
            comparison_id=context.comparison_id,
            execution_id=context.execution_id,
        )

    expected_row_indexes = set(context.row_index_order)
    expected_row_count = context.row_count
    unit_results: list[dict] = []
    results_by_row_per_table: list[dict[int, dict]] = []

    for unit_ctx in context.table_contexts:
        try:
            unit_result = calculate_single_table(unit_ctx)
        except UnexpectedSingleTableCalculationError as exc:
            logger.exception(
                "agente_compara_multi_table_unexpected comparison_id=%s execution_id=%s "
                "table_id=%s slot=%s exception_type=%s",
                context.comparison_id,
                context.execution_id,
                unit_ctx.table_id,
                unit_ctx.slot_number,
                type(exc).__name__,
            )
            raise UnexpectedMultiTableCalculationError(
                "Falha sistêmica no cálculo unitário durante a orquestração.",
                comparison_id=context.comparison_id,
                table_id=unit_ctx.table_id,
                slot_number=unit_ctx.slot_number,
                execution_id=context.execution_id,
                exception_type=type(exc).__name__,
            ) from exc
        except AgenteComparaCalculationError as exc:
            logger.exception(
                "agente_compara_multi_table_unexpected comparison_id=%s execution_id=%s "
                "table_id=%s slot=%s exception_type=%s",
                context.comparison_id,
                context.execution_id,
                unit_ctx.table_id,
                unit_ctx.slot_number,
                type(exc).__name__,
            )
            raise UnexpectedMultiTableCalculationError(
                "Falha sistêmica no cálculo unitário durante a orquestração.",
                comparison_id=context.comparison_id,
                table_id=unit_ctx.table_id,
                slot_number=unit_ctx.slot_number,
                execution_id=context.execution_id,
                exception_type=type(exc).__name__,
            ) from exc
        except AgenteComparaMultiTableCalculationError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "agente_compara_multi_table_unexpected comparison_id=%s execution_id=%s "
                "table_id=%s slot=%s exception_type=%s",
                context.comparison_id,
                context.execution_id,
                unit_ctx.table_id,
                unit_ctx.slot_number,
                type(exc).__name__,
            )
            raise UnexpectedMultiTableCalculationError(
                "Falha inesperada na orquestração multitabela.",
                comparison_id=context.comparison_id,
                table_id=unit_ctx.table_id,
                slot_number=unit_ctx.slot_number,
                execution_id=context.execution_id,
                exception_type=type(exc).__name__,
            ) from exc

        by_row = validate_single_table_result(
            unit_result,
            context=unit_ctx,
            expected_row_indexes=expected_row_indexes,
            expected_row_count=expected_row_count,
        )
        unit_results.append(unit_result)
        results_by_row_per_table.append(by_row)

    # Paridade estrutural entre todas as tabelas.
    reference_indexes = set(results_by_row_per_table[0].keys())
    for idx, by_row in enumerate(results_by_row_per_table[1:], start=1):
        if set(by_row.keys()) != reference_indexes:
            raise MultiTableCalculationInvariantError(
                "Paridade estrutural de row_index quebrada entre tabelas.",
                comparison_id=context.comparison_id,
                table_id=unit_results[idx]["table_id"],
                slot_number=unit_results[idx]["slot_number"],
                execution_id=context.execution_id,
            )

    comparative_rows = consolidate_results_by_row_index(
        context=context,
        unit_results=unit_results,
        results_by_row_per_table=results_by_row_per_table,
    )

    tables_meta: list[dict] = []
    results_by_table: dict[str, dict] = {}
    calculated_cell_count = 0
    calculated_with_warnings_cell_count = 0
    incomplete_cell_count = 0
    error_cell_count = 0
    tables_with_all_rows_calculated = 0
    tables_with_row_errors = 0

    for unit_result in unit_results:
        table_id = unit_result["table_id"]
        # Snapshot defensivo: não mutar resultado unitário original.
        unit_snapshot = copy.deepcopy(unit_result)
        calc_count = int(unit_snapshot["calculated_count"])
        warn_count = int(unit_snapshot.get("calculated_with_warnings_count") or 0)
        incomplete_count = int(unit_snapshot.get("incomplete_count") or 0)
        err_count = int(unit_snapshot["error_count"])
        calculated_cell_count += calc_count
        calculated_with_warnings_cell_count += warn_count
        incomplete_cell_count += incomplete_count
        error_cell_count += err_count
        if err_count == 0 and incomplete_count == 0:
            tables_with_all_rows_calculated += 1
        else:
            tables_with_row_errors += 1

        summary = unit_snapshot.get("summary") if isinstance(unit_snapshot.get("summary"), dict) else {}
        tables_meta.append(
            {
                "table_id": table_id,
                "temp_table_id": unit_snapshot["temp_table_id"],
                "slot_number": unit_snapshot["slot_number"],
                "carrier_name": unit_snapshot["carrier_name"],
                "calculated_count": calc_count,
                "calculated_with_warnings_count": warn_count,
                "incomplete_count": incomplete_count,
                "error_count": err_count,
                "summary": {
                    "total_calculated_freight": summary.get("total_calculated_freight"),
                    "calculated_count": calc_count,
                    "calculated_with_warnings_count": warn_count,
                    "incomplete_count": incomplete_count,
                    "error_count": err_count,
                },
            }
        )
        # Memória de cálculo canônica fica em comparative_rows[table_results]
        # para evitar duplicar o payload em results_by_table.
        public_results = []
        for row in unit_snapshot.get("results") or []:
            if not isinstance(row, dict):
                continue
            slim = dict(row)
            slim.pop("calculation_memory", None)
            public_results.append(slim)
        results_by_table[table_id] = {
            "comparison_id": unit_snapshot["comparison_id"],
            "table_id": table_id,
            "temp_table_id": unit_snapshot["temp_table_id"],
            "slot_number": unit_snapshot["slot_number"],
            "carrier_name": unit_snapshot["carrier_name"],
            "row_count": unit_snapshot["row_count"],
            "calculated_count": calc_count,
            "calculated_with_warnings_count": warn_count,
            "incomplete_count": incomplete_count,
            "error_count": err_count,
            "results": public_results,
            "summary": copy.deepcopy(summary),
            "duration_ms": unit_snapshot.get("duration_ms", 0),
            "schema_version": unit_snapshot.get("schema_version"),
        }

    table_count = len(unit_results)
    total_calculation_cells = expected_row_count * table_count
    if (
        calculated_cell_count
        + calculated_with_warnings_cell_count
        + incomplete_cell_count
        + error_cell_count
        != total_calculation_cells
    ):
        raise MultiTableCalculationInvariantError(
            "Contagens globais de células incoerentes.",
            comparison_id=context.comparison_id,
            execution_id=context.execution_id,
        )

    duration_ms = int((time.perf_counter() - started) * 1000)
    payload = {
        "schema_version": MULTI_TABLE_CALCULATION_SCHEMA_VERSION,
        "comparison_id": context.comparison_id,
        "execution_id": context.execution_id,
        "table_count": table_count,
        "row_count": expected_row_count,
        "tables": tables_meta,
        "results_by_table": results_by_table,
        "comparative_rows": comparative_rows,
        "summary": {
            "table_count": table_count,
            "row_count": expected_row_count,
            "completed_table_count": table_count,
            "total_calculation_cells": total_calculation_cells,
            "calculated_cell_count": calculated_cell_count,
            "calculated_with_warnings_cell_count": calculated_with_warnings_cell_count,
            "incomplete_cell_count": incomplete_cell_count,
            "error_cell_count": error_cell_count,
            "tables_with_all_rows_calculated": tables_with_all_rows_calculated,
            "tables_with_row_errors": tables_with_row_errors,
        },
        "duration_ms": duration_ms,
    }

    validate_comparison_result_serializable(
        payload,
        comparison_id=context.comparison_id,
        execution_id=context.execution_id,
    )

    logger.info(
        "agente_compara_multi_table_calc comparison_id=%s execution_id=%s table_count=%s "
        "row_count=%s calculated_cell_count=%s calculated_with_warnings_cell_count=%s "
        "incomplete_cell_count=%s error_cell_count=%s duration_ms=%s",
        context.comparison_id,
        context.execution_id,
        table_count,
        expected_row_count,
        calculated_cell_count,
        calculated_with_warnings_cell_count,
        incomplete_cell_count,
        error_cell_count,
        duration_ms,
    )
    return payload
