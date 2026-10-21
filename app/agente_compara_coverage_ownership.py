"""Ownership de coverage por tabela no AgenteCompara multitabela.

Cada tabela confirmada resolve a própria abrangência. O arquivo operacional
só é liberado quando todas as tabelas obrigatórias estão configuradas ou
explicitamente dispensadas. Zona abstrata (P1, S2) sem coverage compatível
e sem mapeamento cidade→zona no contrato bloqueia o avanço.
"""
from __future__ import annotations

import logging
import re
import unicodedata

from app.agente_compara_comparison_state import (
    ERROR_COVERAGE_CONFIGURATION_PENDING,
    AgenteComparaComparisonError,
    get_table_by_id,
    iter_required_confirmed_tables,
    persist_comparison_state,
)

logger = logging.getLogger(__name__)

COVERAGE_STATUS_CONFIGURED = "configured"
COVERAGE_STATUS_WAIVED = "waived"
COVERAGE_STATUS_PENDING = "pending"
COVERAGE_DISPOSITION_WAIVED = "waived"

_EXECUTABLE_PRICING_TYPES = frozenset(
    {"fixed_range", "direct_weight_rate", "range_plus_excess_per_kg"}
)
_ABSTRACT_ZONE_RE = re.compile(r"^[A-Z]{1,3}\d{1,3}$")


def canonical_zone_label(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = unicodedata.normalize("NFKD", value)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^A-Za-z0-9]", "", text).upper()
    if not text or not _ABSTRACT_ZONE_RE.fullmatch(text):
        return None
    return text


def coverage_pending_message(carrier_name: str | None) -> str:
    name = (carrier_name or "").strip() or "selecionada"
    return f"A tabela {name} ainda precisa da configuração de cidades atendidas."


def _lookup_key_maps_city(key: str) -> bool:
    if not isinstance(key, str) or not key.strip():
        return False
    candidate = key.split("=>", 1)[-1]
    if "|" not in candidate:
        return False
    left, right = candidate.split("|", 1)
    if len(left.strip()) != 2 or not right.strip():
        return False
    if canonical_zone_label(right):
        return False
    return True


def _coverage_rows(record: dict | None) -> list[dict]:
    if not isinstance(record, dict):
        return []
    coverage = record.get("coverage_table")
    if not isinstance(coverage, dict):
        return []
    rows = coverage.get("rows")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _coverage_zone_labels(rows: list[dict]) -> set[str]:
    labels: set[str] = set()
    for row in rows:
        label = canonical_zone_label(row.get("freight_region"))
        if label:
            labels.add(label)
    return labels


def _unresolved_abstract_zones(record: dict | None) -> set[str]:
    """Zonas executáveis sem chave cidade→zona no contrato.

    freight_routes só contam quando já viraram chave de cidade no contrato.
    Não substituem um arquivo de abrangência ausente.
    """
    if not isinstance(record, dict):
        return set()
    contract = _pricing_contract_for_gate(record)
    unresolved: set[str] = set()
    for rule in contract.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        if rule.get("pricing_type") not in _EXECUTABLE_PRICING_TYPES:
            continue
        zone = canonical_zone_label(rule.get("region"))
        if not zone:
            continue
        keys = rule.get("lookup_keys") or []
        if any(_lookup_key_maps_city(key) for key in keys if isinstance(key, str)):
            continue
        unresolved.add(zone)
    return unresolved


def _pricing_contract_for_gate(record: dict) -> dict:
    """Lê o contrato persistido válido antes de recompilar.

    A recompilação atual desativa freight_routes e apaga chaves de cidade já
    reconhecidas num contrato histórico. Um contrato novo confirmado já foi
    persistido por essa mesma compilação, então a abrangência não muda.
    """
    stored = record.get("pricing_contract") if isinstance(record, dict) else None
    try:
        from app.agente_compara_doc_service import _pricing_contract_is_usable

        if _pricing_contract_is_usable(record, stored):
            return stored
    except Exception:
        logger.exception("Falha ao ler contrato persistido para a situação de coverage.")
    try:
        from app.agente_compara_doc_service import build_pricing_contract

        compiled = build_pricing_contract(record)
    except Exception:
        logger.exception("Falha ao compilar contrato para a situação de coverage.")
        return {}
    return compiled if isinstance(compiled, dict) else {}


def _load_table_record(entry: dict) -> dict | None:
    temp_table_id = (entry.get("temp_table_id") or "").strip()
    if not temp_table_id:
        return None
    from app.agente_compara_doc_service import get_cleiton_doc_config, load_temp_table_record

    cfg = get_cleiton_doc_config()
    record = load_temp_table_record(temp_table_id, ttl_hours=cfg.upload_ttl_hours)
    return record if isinstance(record, dict) else None


def evaluate_table_coverage(entry: dict, record: dict | None = None) -> dict:
    if record is None:
        record = _load_table_record(entry)
    rows = _coverage_rows(record)
    unresolved = _unresolved_abstract_zones(record)
    covered = _coverage_zone_labels(rows)
    still_required = bool(unresolved - covered)
    configured = bool(rows) and not still_required
    waived = (
        entry.get("coverage_disposition") == COVERAGE_DISPOSITION_WAIVED
        and not still_required
    )
    if configured:
        status = COVERAGE_STATUS_CONFIGURED
    elif waived:
        status = COVERAGE_STATUS_WAIVED
    else:
        status = COVERAGE_STATUS_PENDING
    return {
        "table_id": entry.get("table_id"),
        "slot_number": entry.get("slot_number"),
        "carrier_name": entry.get("carrier_name"),
        "coverage_status": status,
        "coverage_rows_count": len(rows),
        "coverage_ready": bool(configured or waived),
        "coverage_required": still_required,
    }


def coverage_situations_for_state(state: dict | None) -> list[dict]:
    if not isinstance(state, dict):
        return []
    situations: list[dict] = []
    for entry in iter_required_confirmed_tables(state):
        situations.append(evaluate_table_coverage(entry))
    return situations


def blocking_coverage_message(situations: list[dict]) -> str | None:
    blockers = [item for item in situations if item.get("coverage_required") and not item.get("coverage_ready")]
    if not blockers:
        pending = [item for item in situations if not item.get("coverage_ready")]
        if not pending:
            return None
        blockers = pending
    return " ".join(coverage_pending_message(item.get("carrier_name")) for item in blockers)


def assert_required_coverage_resolved(state: dict) -> None:
    situations = coverage_situations_for_state(state)
    message = blocking_coverage_message(situations)
    if message:
        raise AgenteComparaComparisonError(ERROR_COVERAGE_CONFIGURATION_PENDING, message)


def waive_tables_that_do_not_require_coverage(state: dict, *, session_obj=None) -> dict:
    """Dispensa só tabelas cuja coverage não é exigida pelo contrato."""
    changed = False
    for entry in iter_required_confirmed_tables(state):
        situation = evaluate_table_coverage(entry)
        if situation["coverage_ready"] or situation["coverage_required"]:
            continue
        current = get_table_by_id(state, entry.get("table_id"))
        if current is None:
            continue
        if current.get("coverage_disposition") != COVERAGE_DISPOSITION_WAIVED:
            current["coverage_disposition"] = COVERAGE_DISPOSITION_WAIVED
            changed = True
    if changed:
        return persist_comparison_state(state, session_obj=session_obj)
    return state
