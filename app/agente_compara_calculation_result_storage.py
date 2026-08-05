"""
Storage dedicado do resultado comparativo do AgenteCompara (correção Etapa 5).

Persiste o payload completo fora da sessão Flask (filesystem isolado em
cleiton_doc_tmp/agente_compara_calc), com escrita atômica e checksum SHA-256.

Não importa Cleide. Não contém matemática de frete.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.cleiton_doc_store import (
    _build_safe_path,
    _safe_remove_file,
    _sanitize_doc_id,
    _write_json_atomic,
    get_cleiton_doc_tmp_dir,
)

logger = logging.getLogger(__name__)

RESULT_STORAGE_SCHEMA_VERSION = 2
RESULT_SUBDIR_NAME = "agente_compara_calc"
RESULT_FILENAME_PREFIX = "cc_result_"
MEMORY_FILENAME_PREFIX = "cc_memory_"

# Cenário medido ~5.14 MB (2000×3). Limite técnico folgado acima do caso aprovado.
RESULT_MAX_BYTES = 16 * 1024 * 1024

ERROR_RESULT_STORAGE_KEY_INVALID = "agente_compara_calculation_result_storage_key_invalid"
ERROR_RESULT_MISSING = "calculation_result_missing"
ERROR_RESULT_CORRUPT = "calculation_result_corrupt"
ERROR_RESULT_TOO_LARGE = "calculation_result_too_large"
ERROR_RESULT_IDENTITY_MISMATCH = "calculation_result_identity_mismatch"
ERROR_RESULT_FORBIDDEN_FIELD = "calculation_result_forbidden_field"
ERROR_MEMORY_MISSING = "calculation_memory_missing"
ERROR_MEMORY_CORRUPT = "calculation_memory_corrupted"
ERROR_MEMORY_STORAGE_FAILED = "calculation_memory_storage_failed"
ERROR_MEMORY_TOO_LARGE = "calculation_memory_too_large"
ERROR_RESULT_SERIALIZATION_FAILED = "calculation_result_serialization_failed"
ERROR_MEMORY_SERIALIZATION_FAILED = "calculation_memory_serialization_failed"
ERROR_RESULT_WRITE_FAILED = "calculation_result_write_failed"
ERROR_MEMORY_WRITE_FAILED = "calculation_memory_write_failed"
ERROR_RESULT_CHECKSUM_FAILED = "calculation_result_checksum_failed"
ERROR_MEMORY_CHECKSUM_FAILED = "calculation_memory_checksum_failed"
ERROR_RESULT_VALIDATION_FAILED = "calculation_result_validation_failed"
ERROR_MEMORY_VALIDATION_FAILED = "calculation_memory_validation_failed"

MEMORY_MAX_BYTES = 24 * 1024 * 1024

_FORBIDDEN_PUBLIC_RESULT_FIELDS = frozenset(
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

_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,200}$")


class AgenteComparaCalculationResultStorageError(Exception):
    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        safe_message: str | None = None,
        error_stage: str | None = None,
        artifact_type: str | None = None,
        retryable: bool = False,
        metrics: dict | None = None,
        operation: str | None = None,
        exc_class: str | None = None,
        errno: int | None = None,
        invalid_type: str | None = None,
        invalid_path: str | None = None,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.safe_message = safe_message or message
        self.error_stage = error_stage
        self.artifact_type = artifact_type
        self.retryable = bool(retryable)
        self.metrics = dict(metrics or {})
        self.operation = operation
        self.exc_class = exc_class
        self.errno = errno
        self.invalid_type = invalid_type
        self.invalid_path = invalid_path


def _raise_storage_error(
    error_code: str,
    message: str,
    *,
    safe_message: str | None = None,
    error_stage: str | None = None,
    artifact_type: str | None = None,
    retryable: bool = False,
    metrics: dict | None = None,
    operation: str | None = None,
    exc: Exception | None = None,
    invalid_type: str | None = None,
    invalid_path: str | None = None,
) -> None:
    raise AgenteComparaCalculationResultStorageError(
        error_code,
        message,
        safe_message=safe_message or message,
        error_stage=error_stage,
        artifact_type=artifact_type,
        retryable=retryable,
        metrics=metrics,
        operation=operation,
        exc_class=(type(exc).__name__ if exc is not None else None),
        errno=(getattr(exc, "errno", None) if exc is not None else None),
        invalid_type=invalid_type,
        invalid_path=invalid_path,
    ) from exc


def _canonical_json_bytes(value: Any) -> bytes:
    text = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )
    return text.encode("utf-8")


def sha256_hex_of_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def compute_result_checksum(result: dict) -> str:
    return sha256_hex_of_bytes(_canonical_json_bytes(result))


def get_calculation_result_storage_dir() -> Path:
    root = Path(get_cleiton_doc_tmp_dir()).resolve()
    target = (root / RESULT_SUBDIR_NAME).resolve()
    if root not in target.parents and target != root:
        raise AgenteComparaCalculationResultStorageError(
            ERROR_RESULT_STORAGE_KEY_INVALID,
            "Diretório de resultado inválido.",
        )
    target.mkdir(parents=True, exist_ok=True)
    return target


def build_result_storage_key(*, comparison_id: str, fingerprint: str) -> str:
    cmp_safe = _sanitize_doc_id((comparison_id or "").strip())
    fp_raw = (fingerprint or "").strip().lower()
    fp_safe = "".join(ch for ch in fp_raw if ch.isalnum())
    if len(fp_safe) < 16:
        raise AgenteComparaCalculationResultStorageError(
            ERROR_RESULT_STORAGE_KEY_INVALID,
            "Fingerprint inválido para storage key.",
        )
    # Identidade estável por comparison + fingerprint (sem execution_id / timestamp).
    key = f"{RESULT_FILENAME_PREFIX}{cmp_safe}_{fp_safe[:32]}"
    if not _SAFE_KEY_RE.match(key):
        raise AgenteComparaCalculationResultStorageError(
            ERROR_RESULT_STORAGE_KEY_INVALID,
            "Storage key inválida.",
        )
    return key


def resolve_result_storage_path(storage_key: str) -> Path:
    key = (storage_key or "").strip()
    if not _SAFE_KEY_RE.match(key) or not key.startswith(RESULT_FILENAME_PREFIX):
        raise AgenteComparaCalculationResultStorageError(
            ERROR_RESULT_STORAGE_KEY_INVALID,
            "Storage key inválida.",
        )
    directory = get_calculation_result_storage_dir()
    filename = f"{key}.json"
    path = _build_safe_path(str(directory), filename)
    # Extra guard: must remain under calc subdir.
    calc_dir = directory.resolve()
    resolved = path.resolve()
    if calc_dir not in resolved.parents and resolved != calc_dir:
        raise AgenteComparaCalculationResultStorageError(
            ERROR_RESULT_STORAGE_KEY_INVALID,
            "Path de resultado fora do diretório permitido.",
        )
    return resolved


def build_memory_storage_key(*, comparison_id: str, fingerprint: str) -> str:
    result_key = build_result_storage_key(comparison_id=comparison_id, fingerprint=fingerprint)
    return result_key.replace(RESULT_FILENAME_PREFIX, MEMORY_FILENAME_PREFIX, 1)


def resolve_memory_storage_path(storage_key: str) -> Path:
    key = (storage_key or "").strip()
    if not _SAFE_KEY_RE.match(key) or not key.startswith(MEMORY_FILENAME_PREFIX):
        raise AgenteComparaCalculationResultStorageError(
            ERROR_RESULT_STORAGE_KEY_INVALID,
            "Storage key de memória inválida.",
        )
    directory = get_calculation_result_storage_dir()
    path = _build_safe_path(str(directory), f"{key}.json")
    return path.resolve()


def _assert_no_forbidden_fields(node: Any) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _FORBIDDEN_PUBLIC_RESULT_FIELDS:
                raise AgenteComparaCalculationResultStorageError(
                    ERROR_RESULT_FORBIDDEN_FIELD,
                    "Resultado contém campo proibido.",
                )
            _assert_no_forbidden_fields(value)
    elif isinstance(node, list):
        for item in node:
            _assert_no_forbidden_fields(item)


def save_comparison_calculation_result(
    *,
    comparison_id: str,
    fingerprint: str,
    result: dict,
    schema_version: int = RESULT_STORAGE_SCHEMA_VERSION,
    memory_storage_meta: dict | None = None,
) -> dict:
    """
    Serializa, valida e grava atomicamente o resultado.

    Retorna metadados leves para a sessão (sem path absoluto).
    """
    if not isinstance(result, dict):
        raise AgenteComparaCalculationResultStorageError(
            ERROR_RESULT_CORRUPT,
            "Resultado inválido para persistência.",
            safe_message="Os cálculos foram processados, mas o resultado comparativo não pôde ser salvo.",
            error_stage="result_validation_failed",
            artifact_type="result",
            metrics={"last_completed_stage": "memory_checksum_validated"},
            operation="result.validate_input",
        )
    serialize_started = time.perf_counter()
    try:
        raw = _canonical_json_bytes(result)
    except (TypeError, ValueError) as exc:
        _raise_storage_error(
            ERROR_RESULT_SERIALIZATION_FAILED,
            "Resultado não serializável.",
            safe_message="Os cálculos foram processados, mas o resultado comparativo não pôde ser serializado.",
            error_stage="result_serialized",
            artifact_type="result",
            metrics={"last_completed_stage": "memory_checksum_validated"},
            operation="result.serialize",
            exc=exc,
        )

    size = len(raw)
    metrics = {
        "result_size_bytes": size,
        "result_serialization_duration_ms": int((time.perf_counter() - serialize_started) * 1000),
        "last_completed_stage": "result_serialized",
    }
    if size > RESULT_MAX_BYTES:
        raise AgenteComparaCalculationResultStorageError(
            ERROR_RESULT_TOO_LARGE,
            "Resultado excede o limite técnico de armazenamento.",
            safe_message="Os cálculos foram processados, mas o resultado comparativo excede o limite técnico de armazenamento.",
            error_stage="result_size_validated",
            artifact_type="result",
            metrics={**metrics, "failed_stage": "result_size_validated", "result_limit_bytes": RESULT_MAX_BYTES},
            operation="result.validate_size",
        )

    _assert_no_forbidden_fields(result)
    metrics["last_completed_stage"] = "result_size_validated"
    checksum = sha256_hex_of_bytes(raw)
    metrics["last_completed_stage"] = "result_checksum_validated"
    storage_key = build_result_storage_key(comparison_id=comparison_id, fingerprint=fingerprint)
    path = resolve_result_storage_path(storage_key)

    envelope = {
        "schema_version": int(schema_version),
        "comparison_id": (comparison_id or "").strip(),
        "request_fingerprint": (fingerprint or "").strip(),
        "result_schema_version": int(result.get("schema_version") or 1),
        "result_checksum": checksum,
        "result_size_bytes": size,
        "memory_storage_key": (memory_storage_meta or {}).get("memory_storage_key"),
        "memory_checksum": (memory_storage_meta or {}).get("memory_checksum"),
        "memory_size_bytes": (memory_storage_meta or {}).get("memory_size_bytes"),
        "memory_schema_version": (memory_storage_meta or {}).get("memory_schema_version"),
        "result": result,
    }

    try:
        envelope_raw = _canonical_json_bytes(envelope)
    except (TypeError, ValueError) as exc:
        _raise_storage_error(
            ERROR_RESULT_VALIDATION_FAILED,
            "Envelope do resultado não serializável.",
            safe_message="Os cálculos foram processados, mas o envelope do resultado não pôde ser validado.",
            error_stage="result_validation_failed",
            artifact_type="result",
            metrics=metrics,
            operation="result.validate_envelope",
            exc=exc,
        )
    metrics["result_envelope_size_bytes"] = len(envelope_raw)

    try:
        _write_json_atomic(path, envelope)
    except Exception as exc:
        logger.exception(
            "agente_compara_result_save_failed comparison_id=%s fingerprint=%s size=%s",
            (comparison_id or "")[:32],
            (fingerprint or "")[:12],
            size,
        )
        _raise_storage_error(
            ERROR_RESULT_WRITE_FAILED,
            "Não foi possível gravar o resultado comparativo.",
            safe_message="Os cálculos foram processados, mas o resultado comparativo não pôde ser salvo.",
            error_stage="result_replaced",
            artifact_type="result",
            retryable=True,
            metrics=metrics,
            operation="result.write_atomic",
            exc=exc,
        )

    metrics["last_completed_stage"] = "result_replaced"
    if not path.is_file():
        raise AgenteComparaCalculationResultStorageError(
            ERROR_RESULT_VALIDATION_FAILED,
            "Arquivo de resultado ausente após gravação.",
            safe_message="Os cálculos foram processados, mas o arquivo final do resultado não foi encontrado.",
            error_stage="result_reloaded",
            artifact_type="result",
            retryable=True,
            metrics=metrics,
            operation="result.reload_post_write",
        )
    metrics["last_completed_stage"] = "result_reloaded"

    logger.info(
        "agente_compara_result_saved comparison_id=%s fingerprint=%s result_size_bytes=%s",
        (comparison_id or "")[:32],
        (fingerprint or "")[:12],
        size,
    )
    return {
        "result_storage_key": storage_key,
        "result_checksum": checksum,
        "result_size_bytes": size,
        "result_envelope_size_bytes": metrics.get("result_envelope_size_bytes"),
        "result_limit_bytes": RESULT_MAX_BYTES,
        "result_schema_version": int(result.get("schema_version") or 1),
        "schema_version": int(schema_version),
        "memory_storage_key": (memory_storage_meta or {}).get("memory_storage_key"),
        "memory_checksum": (memory_storage_meta or {}).get("memory_checksum"),
        "memory_size_bytes": (memory_storage_meta or {}).get("memory_size_bytes"),
        "memory_schema_version": (memory_storage_meta or {}).get("memory_schema_version"),
        "memory_envelope_size_bytes": (memory_storage_meta or {}).get("memory_envelope_size_bytes"),
        "memory_limit_bytes": (memory_storage_meta or {}).get("memory_limit_bytes"),
    }


def load_comparison_calculation_result(
    *,
    storage_key: str,
    comparison_id: str,
    fingerprint: str,
    expected_checksum: str | None = None,
) -> dict:
    """Lê e valida integridade/identidade do resultado. Nunca retorna path."""
    path = resolve_result_storage_path(storage_key)
    if not path.is_file():
        logger.info(
            "agente_compara_result_missing comparison_id=%s fingerprint=%s",
            (comparison_id or "")[:32],
            (fingerprint or "")[:12],
        )
        raise AgenteComparaCalculationResultStorageError(
            ERROR_RESULT_MISSING,
            "Resultado comparativo não encontrado.",
        )

    try:
        size_on_disk = path.stat().st_size
    except OSError as exc:
        raise AgenteComparaCalculationResultStorageError(
            ERROR_RESULT_CORRUPT,
            "Não foi possível ler metadados do resultado.",
        ) from exc

    if size_on_disk > RESULT_MAX_BYTES + 64 * 1024:
        raise AgenteComparaCalculationResultStorageError(
            ERROR_RESULT_TOO_LARGE,
            "Arquivo de resultado excede o limite técnico.",
        )

    try:
        with open(path, "r", encoding="utf-8") as handle:
            envelope = json.load(handle)
    except Exception as exc:
        logger.info(
            "agente_compara_result_corrupt comparison_id=%s fingerprint=%s failure_code=%s",
            (comparison_id or "")[:32],
            (fingerprint or "")[:12],
            "json_invalid",
        )
        raise AgenteComparaCalculationResultStorageError(
            ERROR_RESULT_CORRUPT,
            "Resultado comparativo corrompido.",
        ) from exc

    if not isinstance(envelope, dict):
        raise AgenteComparaCalculationResultStorageError(
            ERROR_RESULT_CORRUPT,
            "Resultado comparativo corrompido.",
        )

    if int(envelope.get("schema_version") or 0) != RESULT_STORAGE_SCHEMA_VERSION:
        raise AgenteComparaCalculationResultStorageError(
            ERROR_RESULT_CORRUPT,
            "Schema do resultado não suportado.",
        )

    if (envelope.get("comparison_id") or "").strip() != (comparison_id or "").strip():
        raise AgenteComparaCalculationResultStorageError(
            ERROR_RESULT_IDENTITY_MISMATCH,
            "Resultado não pertence à comparação informada.",
        )
    if (envelope.get("request_fingerprint") or "").strip() != (fingerprint or "").strip():
        raise AgenteComparaCalculationResultStorageError(
            ERROR_RESULT_IDENTITY_MISMATCH,
            "Resultado não corresponde à configuração atual.",
        )

    result = envelope.get("result")
    if not isinstance(result, dict):
        raise AgenteComparaCalculationResultStorageError(
            ERROR_RESULT_CORRUPT,
            "Resultado comparativo corrompido.",
        )

    try:
        raw = _canonical_json_bytes(result)
    except (TypeError, ValueError) as exc:
        raise AgenteComparaCalculationResultStorageError(
            ERROR_RESULT_CORRUPT,
            "Resultado comparativo corrompido.",
        ) from exc

    checksum = sha256_hex_of_bytes(raw)
    stored_checksum = (envelope.get("result_checksum") or "").strip()
    if not stored_checksum or stored_checksum != checksum:
        logger.info(
            "agente_compara_result_corrupt comparison_id=%s fingerprint=%s failure_code=%s",
            (comparison_id or "")[:32],
            (fingerprint or "")[:12],
            "checksum_mismatch",
        )
        raise AgenteComparaCalculationResultStorageError(
            ERROR_RESULT_CORRUPT,
            "Integridade do resultado inválida.",
        )
    if expected_checksum and (expected_checksum or "").strip() != checksum:
        raise AgenteComparaCalculationResultStorageError(
            ERROR_RESULT_CORRUPT,
            "Integridade do resultado inválida.",
        )

    _assert_no_forbidden_fields(result)

    logger.info(
        "agente_compara_result_loaded comparison_id=%s fingerprint=%s result_size_bytes=%s",
        (comparison_id or "")[:32],
        (fingerprint or "")[:12],
        len(raw),
    )
    return result


def delete_comparison_calculation_result(storage_key: str | None) -> bool:
    key = (storage_key or "").strip()
    if not key:
        return False
    try:
        path = resolve_result_storage_path(key)
    except AgenteComparaCalculationResultStorageError:
        return False
    removed = _safe_remove_file(path)
    if removed:
        logger.info(
            "agente_compara_result_cleanup storage_key=%s",
            key[:48],
        )
    return removed


def delete_comparison_calculation_result_by_identity(
    *,
    comparison_id: str,
    fingerprint: str,
) -> bool:
    try:
        key = build_result_storage_key(comparison_id=comparison_id, fingerprint=fingerprint)
    except AgenteComparaCalculationResultStorageError:
        return False
    return delete_comparison_calculation_result(key)


def save_comparison_calculation_memory_payload(
    *,
    comparison_id: str,
    fingerprint: str,
    memory_payload: dict,
    schema_version: int = RESULT_STORAGE_SCHEMA_VERSION,
) -> dict:
    if not isinstance(memory_payload, dict):
        raise AgenteComparaCalculationResultStorageError(
            ERROR_MEMORY_CORRUPT,
            "Payload de memória inválido.",
            safe_message="Os cálculos foram processados, mas os detalhes não puderam ser preparados para armazenamento.",
            error_stage="memory_payload_created",
            artifact_type="memory",
            metrics={"last_completed_stage": "compact_result_created"},
            operation="memory.validate_input",
        )
    serialize_started = time.perf_counter()
    try:
        raw = _canonical_json_bytes(memory_payload)
    except (TypeError, ValueError) as exc:
        _raise_storage_error(
            ERROR_MEMORY_SERIALIZATION_FAILED,
            "Payload de memória não serializável.",
            safe_message="Os cálculos foram processados, mas os detalhes não puderam ser serializados.",
            error_stage="memory_payload_serialized",
            artifact_type="memory",
            metrics={"last_completed_stage": "compact_result_serialized"},
            operation="memory.serialize",
            exc=exc,
        )
    size = len(raw)
    metrics = {
        "memory_size_bytes": size,
        "memory_serialization_duration_ms": int((time.perf_counter() - serialize_started) * 1000),
        "last_completed_stage": "memory_payload_serialized",
    }
    if size > MEMORY_MAX_BYTES:
        raise AgenteComparaCalculationResultStorageError(
            ERROR_MEMORY_TOO_LARGE,
            "Memórias excedem o limite técnico.",
            safe_message="Os cálculos foram processados, mas os detalhes excedem o limite técnico de armazenamento.",
            error_stage="memory_size_validated",
            artifact_type="memory",
            metrics={**metrics, "failed_stage": "memory_size_validated", "memory_limit_bytes": MEMORY_MAX_BYTES},
            operation="memory.validate_size",
        )
    metrics["last_completed_stage"] = "memory_size_validated"
    checksum = sha256_hex_of_bytes(raw)
    metrics["last_completed_stage"] = "memory_checksum_validated"
    storage_key = build_memory_storage_key(comparison_id=comparison_id, fingerprint=fingerprint)
    path = resolve_memory_storage_path(storage_key)
    envelope = {
        "schema_version": int(schema_version),
        "comparison_id": (comparison_id or "").strip(),
        "request_fingerprint": (fingerprint or "").strip(),
        "memory_checksum": checksum,
        "memory_size_bytes": size,
        "memory_payload": memory_payload,
    }
    metrics["memory_envelope_size_bytes"] = len(_canonical_json_bytes(envelope))
    try:
        _write_json_atomic(path, envelope)
    except Exception as exc:
        _raise_storage_error(
            ERROR_MEMORY_WRITE_FAILED,
            "Não foi possível gravar as memórias.",
            safe_message="Os cálculos foram processados, mas os detalhes não puderam ser salvos.",
            error_stage="memory_replaced",
            artifact_type="memory",
            retryable=True,
            metrics=metrics,
            operation="memory.write_atomic",
            exc=exc,
        )
    metrics["last_completed_stage"] = "memory_replaced"
    if not path.is_file():
        raise AgenteComparaCalculationResultStorageError(
            ERROR_MEMORY_VALIDATION_FAILED,
            "Arquivo de memórias ausente após gravação.",
            safe_message="Os cálculos foram processados, mas o arquivo final de detalhes não foi encontrado.",
            error_stage="memory_reloaded",
            artifact_type="memory",
            retryable=True,
            metrics=metrics,
            operation="memory.reload_post_write",
        )
    metrics["last_completed_stage"] = "memory_reloaded"
    return {
        "memory_storage_key": storage_key,
        "memory_checksum": checksum,
        "memory_size_bytes": size,
        "memory_envelope_size_bytes": metrics.get("memory_envelope_size_bytes"),
        "memory_limit_bytes": MEMORY_MAX_BYTES,
        "memory_schema_version": int(memory_payload.get("schema_version") or schema_version),
        "schema_version": int(schema_version),
    }


def load_comparison_calculation_memory_payload(
    *,
    storage_key: str,
    comparison_id: str,
    fingerprint: str,
    expected_checksum: str | None = None,
) -> dict:
    path = resolve_memory_storage_path(storage_key)
    if not path.is_file():
        raise AgenteComparaCalculationResultStorageError(ERROR_MEMORY_MISSING, "Memórias não encontradas.")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            envelope = json.load(handle)
    except Exception as exc:
        raise AgenteComparaCalculationResultStorageError(ERROR_MEMORY_CORRUPT, "Memórias corrompidas.") from exc
    payload = envelope.get("memory_payload")
    if not isinstance(payload, dict):
        raise AgenteComparaCalculationResultStorageError(ERROR_MEMORY_CORRUPT, "Memórias corrompidas.")
    raw = _canonical_json_bytes(payload)
    checksum = sha256_hex_of_bytes(raw)
    if checksum != (envelope.get("memory_checksum") or "").strip():
        raise AgenteComparaCalculationResultStorageError(ERROR_MEMORY_CORRUPT, "Memórias corrompidas.")
    if expected_checksum and checksum != (expected_checksum or "").strip():
        raise AgenteComparaCalculationResultStorageError(ERROR_MEMORY_CORRUPT, "Memórias corrompidas.")
    if (envelope.get("comparison_id") or "").strip() != (comparison_id or "").strip():
        raise AgenteComparaCalculationResultStorageError(ERROR_RESULT_IDENTITY_MISMATCH, "Memórias não pertencem à comparação informada.")
    if (envelope.get("request_fingerprint") or "").strip() != (fingerprint or "").strip():
        raise AgenteComparaCalculationResultStorageError(ERROR_RESULT_IDENTITY_MISMATCH, "Memórias não correspondem à configuração atual.")
    return payload


def delete_comparison_calculation_memories(storage_key: str | None) -> bool:
    key = (storage_key or "").strip()
    if not key:
        return False
    try:
        path = resolve_memory_storage_path(key)
    except AgenteComparaCalculationResultStorageError:
        return False
    return _safe_remove_file(path)
