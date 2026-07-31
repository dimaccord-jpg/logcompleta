"""
Estado global temporário da comparação multitabela (AgenteCompara Fase 1).

Persistido na sessão Flask; slots 1 e 2 obrigatórios, slot 3 opcional.
Sem banco, Redis ou rotas por slot.
"""
from __future__ import annotations

import copy
from uuid import uuid4

from flask import has_request_context, session

AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY = "agente_compara_comparison_state"

COMPARISON_STATUS_PREPARING = "preparing_tables"
COMPARISON_STATUS_TABLES_READY = "tables_ready"
COMPARISON_STATUS_CONFIGURATION_READY = "configuration_ready"
COMPARISON_STATUS_CALCULATION_RUNNING = "calculation_running"
COMPARISON_STATUS_CALCULATION_READY = "calculation_ready"
COMPARISON_STATUS_CALCULATION_FAILED = "calculation_failed"

STEP_PREPARE_TABLE_1 = "PREPARE_TABLE_1"
STEP_PREPARE_TABLE_2 = "PREPARE_TABLE_2"
STEP_ASK_TABLE_3 = "ASK_TABLE_3"
STEP_PREPARE_TABLE_3 = "PREPARE_TABLE_3"
STEP_TABLES_READY = "TABLES_READY"
STEP_TAXES = "TAXES"
STEP_COVERAGE = "COVERAGE"
STEP_CALCULATION_FILE = "CALCULATION_FILE"
STEP_CONFIGURATION_READY = "CONFIGURATION_READY"
STEP_CALCULATION_RUNNING = "CALCULATION_RUNNING"
STEP_CALCULATION_READY = "CALCULATION_READY"
STEP_CALCULATION_FAILED = "CALCULATION_FAILED"
STEP_ANALYSIS = "ANALYSIS"

COMPARISON_COMMON_PARAM_STEPS = frozenset(
    {
        STEP_TAXES,
        STEP_COVERAGE,
        STEP_CALCULATION_FILE,
        STEP_CONFIGURATION_READY,
    }
)

COMPARISON_CALCULATION_STEPS = frozenset(
    {
        STEP_CALCULATION_RUNNING,
        STEP_CALCULATION_READY,
        STEP_CALCULATION_FAILED,
    }
)

TABLE_STATUS_EMPTY = "empty"
TABLE_STATUS_LOCKED = "locked"
TABLE_STATUS_PROCESSING = "processing"
TABLE_STATUS_NEEDS_REVIEW = "needs_review"
TABLE_STATUS_CONFIRMED = "confirmed"
TABLE_STATUS_FAILED = "failed"
TABLE_STATUS_DISCARDED = "discarded"

MAX_TABLE_SLOTS = 3

ERROR_COMPARISON_NOT_FOUND = "agente_compara_comparison_not_found"
ERROR_COMPARISON_SCOPE_MISMATCH = "agente_compara_comparison_scope_mismatch"
ERROR_TABLE_NOT_FOUND = "agente_compara_table_not_found"
ERROR_TABLE_SCOPE_MISMATCH = "agente_compara_table_scope_mismatch"
ERROR_TABLE_LOCKED = "agente_compara_table_locked"
ERROR_TABLE_SLOT_MISMATCH = "agente_compara_table_slot_mismatch"
ERROR_TABLE_MAX_SLOTS = "agente_compara_table_max_slots"
ERROR_COMPARISON_STEP_INVALID = "agente_compara_comparison_step_invalid"
ERROR_CARRIER_NAME_REQUIRED = "agente_compara_carrier_name_required"
ERROR_CARRIER_NAME_INVALID = "agente_compara_carrier_name_invalid"
ERROR_TAX_SELECTED_TABLES_REQUIRED = "agente_compara_tax_selected_tables_required"
ERROR_TAX_SELECTED_TABLE_INVALID = "agente_compara_tax_selected_table_invalid"

CARRIER_NAME_MAX_LENGTH = 120


class AgenteComparaComparisonError(Exception):
    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


def _require_session() -> None:
    if not has_request_context():
        raise RuntimeError("Estado de comparação AgenteCompara requer request context Flask.")


def _mark_session_modified(session_obj=None) -> None:
    if session_obj is not None:
        if hasattr(session_obj, "modified"):
            try:
                session_obj.modified = True
            except (AttributeError, TypeError):
                pass
        return
    session.modified = True


def _new_table_entry(*, slot_number: int, status: str) -> dict:
    table_id = uuid4().hex
    return {
        "table_id": table_id,
        "slot_number": int(slot_number),
        "status": status,
        "doc_ids": [],
        "temp_table_id": None,
        "carrier_name": None,
        "confirmed": False,
        "error": None,
    }


def _normalize_table_entry(raw: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None
    table_id = (raw.get("table_id") or "").strip()
    if not table_id:
        return None
    slot_raw = raw.get("slot_number")
    try:
        slot_number = int(slot_raw)
    except (TypeError, ValueError):
        return None
    if slot_number < 1 or slot_number > MAX_TABLE_SLOTS:
        return None
    doc_ids: list[str] = []
    for item in raw.get("doc_ids") or []:
        if isinstance(item, str):
            ref = item.strip()
            if ref and ref not in doc_ids:
                doc_ids.append(ref)
    temp_table_id = raw.get("temp_table_id")
    if isinstance(temp_table_id, str):
        temp_table_id = temp_table_id.strip() or None
    else:
        temp_table_id = None
    carrier_name = raw.get("carrier_name")
    if isinstance(carrier_name, str):
        carrier_name = carrier_name.strip() or None
    else:
        carrier_name = None
    error = raw.get("error")
    if isinstance(error, str):
        error = error.strip() or None
    else:
        error = None
    status = (raw.get("status") or TABLE_STATUS_EMPTY).strip() or TABLE_STATUS_EMPTY
    return {
        "table_id": table_id,
        "slot_number": slot_number,
        "status": status,
        "doc_ids": doc_ids,
        "temp_table_id": temp_table_id,
        "carrier_name": carrier_name,
        "confirmed": bool(raw.get("confirmed")),
        "error": error,
    }


def _normalize_comparison_state(raw: dict | None) -> dict | None:
    if not isinstance(raw, dict):
        return None
    comparison_id = (raw.get("comparison_id") or "").strip()
    if not comparison_id:
        return None
    current_step = (raw.get("current_step") or STEP_PREPARE_TABLE_1).strip() or STEP_PREPARE_TABLE_1
    active_table_id = (raw.get("active_table_id") or "").strip() or None
    try:
        desired_table_count = int(raw.get("desired_table_count") or 2)
    except (TypeError, ValueError):
        desired_table_count = 2
    desired_table_count = max(2, min(MAX_TABLE_SLOTS, desired_table_count))
    tables_raw = raw.get("tables")
    if not isinstance(tables_raw, dict):
        return None
    tables: dict[str, dict] = {}
    for key, value in tables_raw.items():
        entry = _normalize_table_entry(value if isinstance(value, dict) else {})
        if entry is None:
            continue
        tables[entry["table_id"]] = entry
    if len(tables) < 2:
        return None
    primary_temp_table_id = raw.get("primary_temp_table_id")
    if isinstance(primary_temp_table_id, str):
        primary_temp_table_id = primary_temp_table_id.strip() or None
    else:
        primary_temp_table_id = None
    tax_config = raw.get("tax_config")
    if not isinstance(tax_config, dict):
        tax_config = None
    comparison_calculation = raw.get("comparison_calculation")
    if not isinstance(comparison_calculation, dict):
        comparison_calculation = None
    payload = {
        "comparison_id": comparison_id,
        "status": (raw.get("status") or COMPARISON_STATUS_PREPARING).strip() or COMPARISON_STATUS_PREPARING,
        "current_step": current_step,
        "active_table_id": active_table_id,
        "desired_table_count": desired_table_count,
        "primary_temp_table_id": primary_temp_table_id,
        "tax_config": tax_config,
        "tables": tables,
    }
    if comparison_calculation is not None:
        payload["comparison_calculation"] = copy.deepcopy(comparison_calculation)
    return payload


def get_comparison_state(session_obj=None) -> dict | None:
    if session_obj is None:
        _require_session()
        sess = session
    else:
        sess = session_obj
    raw = sess.get(AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY)
    return _normalize_comparison_state(raw if isinstance(raw, dict) else None)


def set_comparison_state(state: dict, *, session_obj=None) -> dict:
    if session_obj is None:
        _require_session()
        sess = session
    else:
        sess = session_obj
    normalized = _normalize_comparison_state(state)
    if normalized is None:
        raise ValueError("Estado de comparação inválido.")
    sess[AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = normalized
    _mark_session_modified(sess if session_obj is None else session_obj)
    return normalized


def clear_comparison_state(*, session_obj=None) -> None:
    if session_obj is None:
        _require_session()
        sess = session
    else:
        sess = session_obj
    sess.pop(AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY, None)
    if session_obj is None:
        _mark_session_modified()


def create_comparison(*, session_obj=None) -> dict:
    """Cria comparação com slots 1 (ativo) e 2 (bloqueado)."""
    if session_obj is None:
        _require_session()
    table_1 = _new_table_entry(slot_number=1, status=TABLE_STATUS_EMPTY)
    table_2 = _new_table_entry(slot_number=2, status=TABLE_STATUS_LOCKED)
    state = {
        "comparison_id": uuid4().hex,
        "status": COMPARISON_STATUS_PREPARING,
        "current_step": STEP_PREPARE_TABLE_1,
        "active_table_id": table_1["table_id"],
        "desired_table_count": 2,
        "primary_temp_table_id": None,
        "tables": {
            table_1["table_id"]: table_1,
            table_2["table_id"]: table_2,
        },
    }
    return set_comparison_state(state, session_obj=session_obj)


def ensure_comparison(*, session_obj=None) -> dict:
    """Retorna comparação existente ou cria uma nova."""
    existing = get_comparison_state(session_obj)
    if existing is not None:
        return existing
    return create_comparison(session_obj=session_obj)


def get_comparison_if_exists(*, session_obj=None) -> dict | None:
    """Leitura passiva: nunca cria comparação."""
    return get_comparison_state(session_obj)


def start_comparison_for_session(*, session_obj=None) -> dict:
    """
    Único criador oficial da transição SEM_COMPARACAO → PREPARE_TABLE_1.

    Idempotente por sessão: se já existe comparação, devolve a atual sem mutar.
    Não aceita comparison_id/table_id/slot externos.
    """
    existing = get_comparison_if_exists(session_obj=session_obj)
    if existing is not None:
        return {
            "state": existing,
            "comparison_started": False,
            "idempotent_replay": True,
        }
    state = create_comparison(session_obj=session_obj)
    return {
        "state": state,
        "comparison_started": True,
        "idempotent_replay": False,
    }


def get_table_by_id(state: dict, table_id: str) -> dict | None:
    ref = (table_id or "").strip()
    if not ref or not isinstance(state, dict):
        return None
    tables = state.get("tables") if isinstance(state.get("tables"), dict) else {}
    entry = tables.get(ref)
    return entry if isinstance(entry, dict) else None


def get_table_by_slot(state: dict, slot_number: int) -> dict | None:
    if not isinstance(state, dict):
        return None
    tables = state.get("tables") if isinstance(state.get("tables"), dict) else {}
    for entry in tables.values():
        if isinstance(entry, dict) and int(entry.get("slot_number") or 0) == int(slot_number):
            return entry
    return None


def get_active_table(state: dict) -> dict | None:
    if not isinstance(state, dict):
        return None
    active_id = (state.get("active_table_id") or "").strip()
    if active_id:
        entry = get_table_by_id(state, active_id)
        if entry is not None:
            return entry
    return get_table_by_slot(state, 1)


def resolve_table_identity(
    *,
    comparison_id: str | None = None,
    table_id: str | None = None,
    slot: int | None = None,
    session_obj=None,
    auto_create: bool = True,
) -> tuple[dict, dict]:
    """
    Valida identidade composta comparison_id + table_id + slot.
    Retorna (state, table_entry).

    Se comparison_id/table_id forem enviados e não houver comparação (ou forem
    inválidos), NÃO cria comparação silenciosamente — falha com erro de escopo.
    Auto-create permanece apenas para clientes legados sem identidade.
    """
    if session_obj is None:
        _require_session()
    cmp_id = (comparison_id or "").strip()
    tbl_id = (table_id or "").strip()
    claimed_identity = bool(cmp_id or tbl_id)
    state = get_comparison_state(session_obj)
    if state is None:
        if claimed_identity:
            raise AgenteComparaComparisonError(
                ERROR_COMPARISON_NOT_FOUND if not cmp_id else ERROR_COMPARISON_SCOPE_MISMATCH,
                "Nenhuma comparação ativa nesta sessão para a identidade informada.",
            )
        if not auto_create:
            raise AgenteComparaComparisonError(
                ERROR_COMPARISON_NOT_FOUND,
                "Nenhuma comparação ativa nesta sessão.",
            )
        state = create_comparison(session_obj=session_obj)

    if cmp_id and cmp_id != state.get("comparison_id"):
        raise AgenteComparaComparisonError(
            ERROR_COMPARISON_SCOPE_MISMATCH,
            "comparison_id não pertence à sessão atual.",
        )

    entry = None
    if tbl_id:
        entry = get_table_by_id(state, tbl_id)
        if entry is None:
            raise AgenteComparaComparisonError(
                ERROR_TABLE_NOT_FOUND,
                "table_id não encontrado na comparação atual.",
            )
    elif slot is not None:
        try:
            slot_num = int(slot)
        except (TypeError, ValueError):
            slot_num = 0
        entry = get_table_by_slot(state, slot_num)
        if entry is None:
            raise AgenteComparaComparisonError(
                ERROR_TABLE_NOT_FOUND,
                f"Slot {slot} não encontrado na comparação atual.",
            )
    else:
        entry = get_active_table(state)
        if entry is None:
            raise AgenteComparaComparisonError(
                ERROR_TABLE_NOT_FOUND,
                "Nenhuma tabela ativa na comparação.",
            )

    if slot is not None:
        try:
            slot_num = int(slot)
        except (TypeError, ValueError):
            raise AgenteComparaComparisonError(
                ERROR_TABLE_SLOT_MISMATCH,
                "slot inválido.",
            ) from None
        if int(entry.get("slot_number") or 0) != slot_num:
            raise AgenteComparaComparisonError(
                ERROR_TABLE_SLOT_MISMATCH,
                "slot não corresponde ao table_id informado.",
            )

    if entry.get("status") == TABLE_STATUS_LOCKED:
        raise AgenteComparaComparisonError(
            ERROR_TABLE_LOCKED,
            "Esta tabela ainda não está liberada para preparação.",
        )

    return state, entry


def persist_comparison_state(state: dict, *, session_obj=None) -> dict:
    return set_comparison_state(state, session_obj=session_obj)


def _slot_table_ids(state: dict) -> list[str]:
    tables = state.get("tables") if isinstance(state.get("tables"), dict) else {}
    ordered = sorted(
        (entry for entry in tables.values() if isinstance(entry, dict)),
        key=lambda item: int(item.get("slot_number") or 0),
    )
    return [entry["table_id"] for entry in ordered if entry.get("table_id")]


def _first_confirmed_temp_table_id(state: dict) -> str | None:
    tables = state.get("tables") if isinstance(state.get("tables"), dict) else {}
    ordered = sorted(
        (entry for entry in tables.values() if isinstance(entry, dict)),
        key=lambda item: int(item.get("slot_number") or 0),
    )
    for entry in ordered:
        if entry.get("confirmed") and entry.get("temp_table_id"):
            return str(entry["temp_table_id"])
    return None


def validate_confirm_table_and_advance(state: dict, table_id: str) -> None:
    """Valida se a confirmação/avanco é permitida sem mutar o estado."""
    entry = get_table_by_id(state, table_id)
    if entry is None:
        raise AgenteComparaComparisonError(ERROR_TABLE_NOT_FOUND, "Tabela não encontrada.")
    if not entry.get("temp_table_id"):
        raise AgenteComparaComparisonError(
            ERROR_COMPARISON_STEP_INVALID,
            "Confirmação exige tabela temporária válida.",
        )
    slot = int(entry.get("slot_number") or 0)
    step = state.get("current_step")
    if slot == 1 and step == STEP_PREPARE_TABLE_1:
        return
    if slot == 2 and step == STEP_PREPARE_TABLE_2:
        return
    if slot == 3 and step == STEP_PREPARE_TABLE_3:
        return
    raise AgenteComparaComparisonError(
        ERROR_COMPARISON_STEP_INVALID,
        "Confirmação não permitida nesta etapa.",
    )


def confirm_table_and_advance(state: dict, table_id: str, *, session_obj=None) -> dict:
    """Confirma tabela revisada e avança current_step conforme fluxo."""
    validate_confirm_table_and_advance(state, table_id)
    entry = get_table_by_id(state, table_id)
    assert entry is not None
    entry["confirmed"] = True
    entry["status"] = TABLE_STATUS_CONFIRMED
    slot = int(entry.get("slot_number") or 0)
    step = state.get("current_step")

    if slot == 1 and step == STEP_PREPARE_TABLE_1:
        table_2 = get_table_by_slot(state, 2)
        if table_2 is not None:
            table_2["status"] = TABLE_STATUS_EMPTY
        state["current_step"] = STEP_PREPARE_TABLE_2
        state["active_table_id"] = table_2["table_id"] if table_2 else entry["table_id"]
    elif slot == 2 and step == STEP_PREPARE_TABLE_2:
        state["current_step"] = STEP_ASK_TABLE_3
        state["active_table_id"] = entry["table_id"]
    elif slot == 3 and step == STEP_PREPARE_TABLE_3:
        state["desired_table_count"] = 3
        state["current_step"] = STEP_TABLES_READY
        state["status"] = COMPARISON_STATUS_TABLES_READY
        state["primary_temp_table_id"] = _first_confirmed_temp_table_id(state)
        state["active_table_id"] = entry["table_id"]
        return advance_to_taxes(state, session_obj=session_obj)
    return persist_comparison_state(state, session_obj=session_obj)


def proceed_with_two_tables(state: dict, *, session_obj=None) -> dict:
    if state.get("current_step") != STEP_ASK_TABLE_3:
        raise AgenteComparaComparisonError(
            ERROR_COMPARISON_STEP_INVALID,
            "Escolha de duas tabelas só é permitida após confirmar a tabela 2.",
        )
    table_1 = get_table_by_slot(state, 1)
    table_2 = get_table_by_slot(state, 2)
    if not table_1 or not table_2 or not table_1.get("confirmed") or not table_2.get("confirmed"):
        raise AgenteComparaComparisonError(
            ERROR_COMPARISON_STEP_INVALID,
            "As duas tabelas obrigatórias precisam estar confirmadas.",
        )
    state["current_step"] = STEP_TABLES_READY
    state["status"] = COMPARISON_STATUS_TABLES_READY
    state["desired_table_count"] = 2
    state["primary_temp_table_id"] = _first_confirmed_temp_table_id(state)
    # primary_temp_table_id aponta só para compatibilidade legada; não representa o conjunto comparativo.
    return advance_to_taxes(state, session_obj=session_obj)


def add_third_table(state: dict, *, session_obj=None) -> dict:
    if state.get("current_step") != STEP_ASK_TABLE_3:
        raise AgenteComparaComparisonError(
            ERROR_COMPARISON_STEP_INVALID,
            "Terceira tabela só pode ser adicionada após confirmar a tabela 2.",
        )
    if get_table_by_slot(state, 3) is not None:
        raise AgenteComparaComparisonError(
            ERROR_TABLE_MAX_SLOTS,
            "A terceira tabela já existe nesta comparação.",
        )
    table_3 = _new_table_entry(slot_number=3, status=TABLE_STATUS_EMPTY)
    tables = dict(state.get("tables") or {})
    tables[table_3["table_id"]] = table_3
    state["tables"] = tables
    state["desired_table_count"] = 3
    state["current_step"] = STEP_PREPARE_TABLE_3
    state["active_table_id"] = table_3["table_id"]
    return persist_comparison_state(state, session_obj=session_obj)


def advance_to_taxes(state: dict, *, session_obj=None) -> dict:
    if state.get("current_step") != STEP_TABLES_READY:
        raise AgenteComparaComparisonError(
            ERROR_COMPARISON_STEP_INVALID,
            "Impostos só ficam disponíveis após TABLES_READY.",
        )
    if not all_required_tables_confirmed(state):
        raise AgenteComparaComparisonError(
            ERROR_COMPARISON_STEP_INVALID,
            "Todas as tabelas obrigatórias precisam estar confirmadas antes dos impostos.",
        )
    state["current_step"] = STEP_TAXES
    primary = state.get("primary_temp_table_id") or _first_confirmed_temp_table_id(state)
    state["primary_temp_table_id"] = primary
    return persist_comparison_state(state, session_obj=session_obj)


def advance_to_coverage(state: dict, *, session_obj=None) -> dict:
    if state.get("current_step") != STEP_TAXES:
        raise AgenteComparaComparisonError(
            ERROR_COMPARISON_STEP_INVALID,
            "Cidades atendidas só ficam disponíveis após informar os impostos.",
        )
    state["current_step"] = STEP_COVERAGE
    return persist_comparison_state(state, session_obj=session_obj)


def advance_to_calculation_file(state: dict, *, session_obj=None) -> dict:
    if state.get("current_step") != STEP_COVERAGE:
        raise AgenteComparaComparisonError(
            ERROR_COMPARISON_STEP_INVALID,
            "Arquivo operacional só fica disponível após a etapa de cidades atendidas.",
        )
    state["current_step"] = STEP_CALCULATION_FILE
    return persist_comparison_state(state, session_obj=session_obj)


def advance_to_configuration_ready(state: dict, *, session_obj=None) -> dict:
    if state.get("current_step") != STEP_CALCULATION_FILE:
        raise AgenteComparaComparisonError(
            ERROR_COMPARISON_STEP_INVALID,
            "Configuração só pode ser concluída após enviar o arquivo operacional.",
        )
    state["current_step"] = STEP_CONFIGURATION_READY
    state["status"] = COMPARISON_STATUS_CONFIGURATION_READY
    return persist_comparison_state(state, session_obj=session_obj)


def is_comparison_common_params_step(state: dict | None) -> bool:
    if not isinstance(state, dict):
        return False
    return state.get("current_step") in COMPARISON_COMMON_PARAM_STEPS


def comparison_blocks_audit_run(state: dict | None) -> bool:
    """Bloqueia audit/run singular enquanto a comparação multitabela não chegou a ANALYSIS."""
    if not isinstance(state, dict):
        return False
    step = state.get("current_step")
    if step == STEP_ANALYSIS:
        return False
    return step in {
        STEP_TABLES_READY,
        STEP_TAXES,
        STEP_COVERAGE,
        STEP_CALCULATION_FILE,
        STEP_CONFIGURATION_READY,
        STEP_CALCULATION_RUNNING,
        STEP_CALCULATION_READY,
        STEP_CALCULATION_FAILED,
    }


def invalidate_table_preparation(state: dict, table_id: str, *, session_obj=None) -> dict:
    """Substitui/limpa preparação de um slot sem afetar os demais."""
    entry = get_table_by_id(state, table_id)
    if entry is None:
        raise AgenteComparaComparisonError(ERROR_TABLE_NOT_FOUND, "Tabela não encontrada.")
    was_confirmed = bool(entry.get("confirmed"))
    entry["doc_ids"] = []
    entry["temp_table_id"] = None
    entry["carrier_name"] = None
    entry["confirmed"] = False
    entry["error"] = None
    slot = int(entry.get("slot_number") or 0)
    if slot == 3 and state.get("desired_table_count") == 3 and not entry.get("confirmed"):
        # mantém slot 3 vazio se ainda em preparação
        entry["status"] = TABLE_STATUS_EMPTY
    elif slot in {1, 2}:
        entry["status"] = TABLE_STATUS_EMPTY if slot == 1 or get_table_by_slot(state, 1) else TABLE_STATUS_EMPTY
    else:
        entry["status"] = TABLE_STATUS_EMPTY

    demote_steps = {
        STEP_ASK_TABLE_3,
        STEP_TABLES_READY,
        STEP_TAXES,
        STEP_COVERAGE,
        STEP_CALCULATION_FILE,
        STEP_CONFIGURATION_READY,
        STEP_CALCULATION_RUNNING,
        STEP_CALCULATION_READY,
        STEP_CALCULATION_FAILED,
        STEP_ANALYSIS,
    }
    current_step = state.get("current_step")
    if current_step in demote_steps or was_confirmed:
        if slot == 1:
            state["current_step"] = STEP_PREPARE_TABLE_1
        elif slot == 2:
            state["current_step"] = STEP_PREPARE_TABLE_2
        elif slot == 3:
            if int(state.get("desired_table_count") or 2) >= 3:
                state["current_step"] = STEP_PREPARE_TABLE_3
            else:
                state["current_step"] = STEP_ASK_TABLE_3
        else:
            state["current_step"] = STEP_PREPARE_TABLE_1
        state["status"] = COMPARISON_STATUS_PREPARING
        state["primary_temp_table_id"] = None
        # Impostos/cidades/arquivo dependem das tabelas confirmadas.
        state.pop("tax_config", None)
        calc = state.get("comparison_calculation")
        if isinstance(calc, dict):
            calc = dict(calc)
            calc["stale"] = True
            if calc.get("status") == STEP_CALCULATION_READY:
                calc["status"] = STEP_CALCULATION_READY
            state["comparison_calculation"] = calc

    state["active_table_id"] = entry["table_id"]
    return persist_comparison_state(state, session_obj=session_obj)


def remove_third_table_slot(state: dict, *, session_obj=None) -> dict:
    """Remove slot 3 quando usuário desiste da terceira tabela."""
    entry = get_table_by_slot(state, 3)
    if entry is None:
        return state
    tables = dict(state.get("tables") or {})
    tables.pop(entry["table_id"], None)
    state["tables"] = tables
    state["desired_table_count"] = 2
    if state.get("current_step") in {STEP_PREPARE_TABLE_3, STEP_ASK_TABLE_3}:
        table_1 = get_table_by_slot(state, 1)
        table_2 = get_table_by_slot(state, 2)
        if table_1 and table_2 and table_1.get("confirmed") and table_2.get("confirmed"):
            state["current_step"] = STEP_TABLES_READY
            state["status"] = COMPARISON_STATUS_TABLES_READY
            state["primary_temp_table_id"] = _first_confirmed_temp_table_id(state)
            return advance_to_taxes(state, session_obj=session_obj)
    return persist_comparison_state(state, session_obj=session_obj)


def all_required_tables_confirmed(state: dict) -> bool:
    table_1 = get_table_by_slot(state, 1)
    table_2 = get_table_by_slot(state, 2)
    if not table_1 or not table_2 or not table_1.get("confirmed") or not table_2.get("confirmed"):
        return False
    desired = int(state.get("desired_table_count") or 2)
    if desired >= 3:
        table_3 = get_table_by_slot(state, 3)
        return bool(table_3 and table_3.get("confirmed"))
    return True


TAX_FISCAL_STATUS_PENDING = "pending"
TAX_FISCAL_STATUS_CONFIGURED = "configured"
TAX_FISCAL_STATUS_NO_TAXES = "no_taxes"
TAX_FISCAL_STATUS_ERROR = "error"


def iter_required_confirmed_tables(state: dict) -> list[dict]:
    """Retorna tabelas obrigatórias confirmadas ordenadas por slot."""
    if not isinstance(state, dict):
        return []
    desired = int(state.get("desired_table_count") or 2)
    tables = state.get("tables") if isinstance(state.get("tables"), dict) else {}
    ordered = sorted(
        (entry for entry in tables.values() if isinstance(entry, dict)),
        key=lambda item: int(item.get("slot_number") or 0),
    )
    result: list[dict] = []
    for entry in ordered:
        slot = int(entry.get("slot_number") or 0)
        if slot > desired:
            continue
        if entry.get("confirmed"):
            result.append(entry)
    return result


def derive_tax_fiscal_status(tax_config) -> str:
    if not isinstance(tax_config, dict):
        return TAX_FISCAL_STATUS_PENDING
    include_taxes = tax_config.get("include_taxes")
    if include_taxes is False:
        return TAX_FISCAL_STATUS_NO_TAXES
    if include_taxes is not True:
        return TAX_FISCAL_STATUS_PENDING
    origin_uf = (tax_config.get("origin_uf") or "").strip().upper()
    if not origin_uf or len(origin_uf) != 2:
        return TAX_FISCAL_STATUS_ERROR
    iss_rate = tax_config.get("iss_rate")
    origin_city = (tax_config.get("origin_city") or "").strip()
    if iss_rate is not None and not origin_city:
        return TAX_FISCAL_STATUS_ERROR
    raw_rates = tax_config.get("icms_rates")
    if raw_rates is not None and not isinstance(raw_rates, list):
        return TAX_FISCAL_STATUS_ERROR
    raw_destinations = tax_config.get("destination_ufs")
    if raw_destinations is not None and not isinstance(raw_destinations, list):
        return TAX_FISCAL_STATUS_ERROR
    return TAX_FISCAL_STATUS_CONFIGURED


def is_saved_tax_config_complete(tax_config) -> bool:
    status = derive_tax_fiscal_status(tax_config)
    return status in {TAX_FISCAL_STATUS_CONFIGURED, TAX_FISCAL_STATUS_NO_TAXES}


def build_tax_fiscal_table_summary(entry: dict, *, tax_config=None) -> dict:
    if not isinstance(entry, dict):
        return {}
    status = derive_tax_fiscal_status(tax_config)
    return {
        "table_id": entry.get("table_id"),
        "slot_number": entry.get("slot_number"),
        "carrier_name": entry.get("carrier_name"),
        "temp_table_id": entry.get("temp_table_id"),
        "fiscal_status": status,
        "include_taxes": tax_config.get("include_taxes") if isinstance(tax_config, dict) else None,
    }


def get_comparison_tax_config(state: dict | None) -> dict | None:
    if not isinstance(state, dict):
        return None
    tax_config = state.get("tax_config")
    return tax_config if isinstance(tax_config, dict) else None


def set_comparison_tax_config(state: dict, tax_config: dict | None) -> dict:
    if not isinstance(state, dict):
        raise ValueError("Estado de comparação inválido.")
    if tax_config is None:
        state.pop("tax_config", None)
    elif isinstance(tax_config, dict):
        state["tax_config"] = tax_config
    else:
        raise ValueError("tax_config deve ser um objeto ou None.")
    return state


def normalize_selected_table_ids(raw_ids) -> list[str]:
    if raw_ids is None:
        return []
    if not isinstance(raw_ids, list):
        raise AgenteComparaComparisonError(
            ERROR_TAX_SELECTED_TABLE_INVALID,
            "selected_table_ids deve ser uma lista.",
        )
    seen: set[str] = set()
    normalized: list[str] = []
    for item in raw_ids:
        ref = (item or "").strip() if isinstance(item, str) else ""
        if not ref or ref in seen:
            continue
        seen.add(ref)
        normalized.append(ref)
    return normalized


def validate_selected_table_ids_for_tax(state: dict, raw_ids) -> list[str]:
    """Valida ownership e confirmação de cada table_id selecionado para impostos."""
    selected = normalize_selected_table_ids(raw_ids)
    if not selected:
        return []
    tables = state.get("tables") if isinstance(state.get("tables"), dict) else {}
    for table_id in selected:
        entry = tables.get(table_id)
        if not isinstance(entry, dict):
            raise AgenteComparaComparisonError(
                ERROR_TAX_SELECTED_TABLE_INVALID,
                "table_id selecionado não pertence à comparação atual.",
            )
        if not entry.get("confirmed"):
            raise AgenteComparaComparisonError(
                ERROR_TAX_SELECTED_TABLE_INVALID,
                "Impostos só podem incidir sobre tabelas confirmadas.",
            )
        if not (entry.get("temp_table_id") or "").strip():
            raise AgenteComparaComparisonError(
                ERROR_TAX_SELECTED_TABLE_INVALID,
                "Tabela temporária indisponível para a transportadora selecionada.",
            )
    return selected


def evaluate_can_advance_to_coverage(state: dict | None) -> bool:
    """Avanço para cidades exige configuração fiscal global salva e válida."""
    if not isinstance(state, dict) or state.get("current_step") != STEP_TAXES:
        return False
    tax_config = get_comparison_tax_config(state)
    if not isinstance(tax_config, dict) or not tax_config.get("confirmed"):
        return False
    if not is_saved_tax_config_complete(tax_config):
        return False
    if tax_config.get("include_taxes") is True:
        selected = tax_config.get("selected_table_ids") or []
        if not isinstance(selected, list) or len(selected) < 1:
            return False
    return True


def public_tax_config_summary(tax_config: dict | None) -> dict | None:
    if not isinstance(tax_config, dict):
        return None
    return copy.deepcopy(tax_config)


def public_table_summary(entry: dict) -> dict:
    if not isinstance(entry, dict):
        return {}
    return {
        "table_id": entry.get("table_id"),
        "slot_number": entry.get("slot_number"),
        "status": entry.get("status"),
        "doc_count": len(entry.get("doc_ids") or []),
        "temp_table_id": entry.get("temp_table_id"),
        "carrier_name": entry.get("carrier_name"),
        "confirmed": bool(entry.get("confirmed")),
        "error": entry.get("error"),
    }


def public_comparison_summary(
    state: dict,
    *,
    include_active_detail: bool = True,
    tax_table_ufs_preview: list[dict] | None = None,
) -> dict:
    if not isinstance(state, dict):
        return {}
    tables = state.get("tables") if isinstance(state.get("tables"), dict) else {}
    slot_summaries = []
    for entry in sorted(
        (item for item in tables.values() if isinstance(item, dict)),
        key=lambda item: int(item.get("slot_number") or 0),
    ):
        slot_summaries.append(public_table_summary(entry))
    payload = {
        "comparison_id": state.get("comparison_id"),
        "status": state.get("status"),
        "current_step": state.get("current_step"),
        "active_table_id": state.get("active_table_id"),
        "desired_table_count": state.get("desired_table_count"),
        "primary_temp_table_id": state.get("primary_temp_table_id"),
        "tables": slot_summaries,
    }
    if include_active_detail:
        active = get_active_table(state)
        if active is not None:
            payload["active_table"] = public_table_summary(active)
    if state.get("current_step") == STEP_TAXES:
        tax_config_public = public_tax_config_summary(get_comparison_tax_config(state))
        if tax_config_public is not None:
            payload["tax_config"] = tax_config_public
        payload["can_advance_to_coverage"] = evaluate_can_advance_to_coverage(state)
        if isinstance(tax_table_ufs_preview, list):
            payload["tax_table_ufs_preview"] = list(tax_table_ufs_preview)
    elif state.get("current_step") in {
        STEP_CONFIGURATION_READY,
        STEP_CALCULATION_RUNNING,
        STEP_CALCULATION_READY,
        STEP_CALCULATION_FAILED,
    }:
        # Revisão final / cálculo: expor tax_config confirmado para navegação read-only.
        tax_config_public = public_tax_config_summary(get_comparison_tax_config(state))
        if tax_config_public is not None:
            payload["tax_config"] = tax_config_public
    calc = state.get("comparison_calculation")
    if isinstance(calc, dict):
        payload["comparison_calculation"] = public_comparison_calculation_summary(
            calc,
            include_result=False,
        )
    return payload


def public_comparison_calculation_summary(
    calc: dict | None,
    *,
    include_result: bool = False,
) -> dict | None:
    """Resumo público seguro da execução de cálculo (sem path/storage físico)."""
    if not isinstance(calc, dict):
        return None
    payload = {
        "schema_version": calc.get("schema_version"),
        "calculation_algorithm_version": calc.get("calculation_algorithm_version"),
        "execution_id": calc.get("execution_id"),
        "fingerprint_short": calc.get("fingerprint_short"),
        "status": calc.get("status"),
        "stale": bool(calc.get("stale")),
        "started_at": calc.get("started_at"),
        "finished_at": calc.get("finished_at"),
        "failed_at": calc.get("failed_at"),
        "table_ids": list(calc.get("table_ids") or []),
        "slot_numbers": list(calc.get("slot_numbers") or []),
        "source_row_count": calc.get("source_row_count"),
        "calculated_table_count": calc.get("calculated_table_count"),
        "calculated_cell_count": calc.get("calculated_cell_count"),
        "error_cell_count": calc.get("error_cell_count"),
        "result_size_bytes": calc.get("result_size_bytes"),
        "billing_status": calc.get("billing_status"),
        "attempt_count": calc.get("attempt_count"),
        "error": calc.get("error") if calc.get("status") == STEP_CALCULATION_FAILED else None,
    }
    # Resultado completo vive em storage dedicado; sessão/resumo nunca o embute.
    # include_result permanece por compatibilidade de assinatura, mas é ignorado.
    _ = include_result
    return payload


def document_belongs_to_table(doc_record: dict | None, *, comparison_id: str, table_id: str) -> bool:
    if not isinstance(doc_record, dict):
        return False
    doc_cmp = (doc_record.get("comparison_id") or "").strip()
    doc_tbl = (doc_record.get("table_id") or "").strip()
    if doc_cmp and doc_tbl:
        return doc_cmp == comparison_id and doc_tbl == table_id
    return False
