"""Persistência isolada no JSON do job ou da temp table.

A resolução não entra em semantic_freight_contract. Sem migration.
O formulário não grava este campo.
"""
from __future__ import annotations

import copy
from typing import Any

from app.services.semantic_resolution.models import QUEUE_JSON_KEY, TECHNICAL_JSON_KEY
from app.services.semantic_resolution.queue import queue_dependency_fingerprint, validate_queue


class SemanticResolutionPersistenceError(ValueError):
    """Falha local. Não altera o contrato operacional nem o semantic freight contract."""


def _isolate(preview: dict[str, Any]) -> dict[str, Any]:
    updated = copy.deepcopy(preview)
    updated["preview_only"] = True
    updated["activated"] = False
    updated["operational"] = False
    return updated


def attach_semantic_resolution_preview(record: dict[str, Any], preview: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(record, dict) or not isinstance(preview, dict):
        raise SemanticResolutionPersistenceError("record_or_preview_invalid")
    status = str(record.get("status") or "").strip().lower()
    if status == "expired":
        raise SemanticResolutionPersistenceError("temp_table_expired")
    updated = copy.deepcopy(record)
    updated[TECHNICAL_JSON_KEY] = _isolate(preview)
    return updated


def preserve_semantic_resolution_preview(
    *,
    stored: dict[str, Any],
    updated: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    """O save do formulário não substitui o preview técnico."""
    existing = stored.get(TECHNICAL_JSON_KEY) if isinstance(stored, dict) else None
    payload_mentions = isinstance(payload, dict) and TECHNICAL_JSON_KEY in payload
    if isinstance(existing, dict):
        updated[TECHNICAL_JSON_KEY] = copy.deepcopy(existing)
        return
    if payload_mentions:
        updated.pop(TECHNICAL_JSON_KEY, None)


def _queue_dependencies_match(queue: dict[str, Any], current: dict[str, Any]) -> bool:
    """Compara o fingerprint canônico inteiro. Subconjunto igual não mantém a fila current."""
    snapshot = queue.get("dependency_snapshot") if isinstance(queue.get("dependency_snapshot"), dict) else {}
    return queue_dependency_fingerprint(snapshot) == queue_dependency_fingerprint(current)


def attach_semantic_resolution_queue(
    record: dict[str, Any],
    queue: dict[str, Any],
    *,
    current_dependencies: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Grava a fila na chave isolada. Não reativa fila stale nem fila expirada."""
    if not isinstance(record, dict) or not isinstance(queue, dict):
        raise SemanticResolutionPersistenceError("record_or_queue_invalid")
    status = str(record.get("status") or "").strip().lower()
    if status == "expired":
        raise SemanticResolutionPersistenceError("temp_table_expired")
    stored_queue = copy.deepcopy(queue)
    stored_queue["preview_only"] = True
    stored_queue["activated"] = False
    stored_queue["operational"] = False
    if current_dependencies is not None and not _queue_dependencies_match(stored_queue, current_dependencies):
        stored_queue["dependency_status"] = "stale"
    elif stored_queue.get("dependency_status") != "stale":
        stored_queue["dependency_status"] = "current"
    validation = validate_queue(stored_queue)
    if not validation["ok"]:
        raise SemanticResolutionPersistenceError("queue_schema_invalid")
    updated = copy.deepcopy(record)
    updated[QUEUE_JSON_KEY] = stored_queue
    return updated


def preserve_semantic_resolution_queue(
    *,
    stored: dict[str, Any],
    updated: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    """O formulário não grava e não apaga a fila técnica."""
    existing = stored.get(QUEUE_JSON_KEY) if isinstance(stored, dict) else None
    payload_mentions = isinstance(payload, dict) and QUEUE_JSON_KEY in payload
    if isinstance(existing, dict):
        updated[QUEUE_JSON_KEY] = copy.deepcopy(existing)
        return
    if payload_mentions:
        updated.pop(QUEUE_JSON_KEY, None)
