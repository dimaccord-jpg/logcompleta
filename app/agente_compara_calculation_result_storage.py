"""
Storage dedicado do resultado comparativo do AgenteCompara (correção Etapa 5).

Persiste o payload completo fora da sessão Flask (filesystem isolado em
cleiton_doc_tmp/agente_compara_calc), com escrita atômica, checksum SHA-256
e retenção limitada (TTL) de result/memory.

Não importa Cleide. Não contém matemática de frete.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import re
from datetime import datetime, timedelta, timezone
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
AGENTE_COMPARA_CALC_STORAGE_TTL_HOURS = 48

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
_CALC_STORAGE_SWEEP_MIN_INTERVAL_SECONDS = 300
_CALC_STORAGE_SWEEP_MAX_FILES = 40
_calc_storage_sweep_monotonic = 0.0


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


def _calc_storage_ttl_hours() -> int:
    return max(1, int(AGENTE_COMPARA_CALC_STORAGE_TTL_HOURS))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_calc_storage_timestamp(dt: datetime) -> str:
    return _as_utc(dt).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_calc_storage_timestamp(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return _as_utc(raw)
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except Exception:
        return None
    return _as_utc(parsed)


def build_calc_storage_retention_window(
    *,
    created_at: str | datetime | None = None,
    expires_at: str | datetime | None = None,
) -> dict[str, str]:
    """Janela única de retenção do par result/memory (UTC, TTL centralizado)."""
    created_dt = _parse_calc_storage_timestamp(created_at)
    if created_dt is None:
        created_dt = _as_utc(_utcnow())
    expires_dt = _parse_calc_storage_timestamp(expires_at)
    if expires_dt is None:
        expires_dt = created_dt + timedelta(hours=_calc_storage_ttl_hours())
    return {
        "created_at": _format_calc_storage_timestamp(created_dt),
        "expires_at": _format_calc_storage_timestamp(expires_dt),
    }


def _retention_window_from_inputs(
    *,
    created_at: str | datetime | None = None,
    expires_at: str | datetime | None = None,
    meta: dict | None = None,
) -> dict[str, str]:
    payload = meta if isinstance(meta, dict) else {}
    return build_calc_storage_retention_window(
        created_at=created_at if created_at is not None else payload.get("created_at"),
        expires_at=expires_at if expires_at is not None else payload.get("expires_at"),
    )


def paired_calc_storage_key(storage_key: str | None) -> str | None:
    key = (storage_key or "").strip()
    if not key:
        return None
    if key.startswith(RESULT_FILENAME_PREFIX):
        return key.replace(RESULT_FILENAME_PREFIX, MEMORY_FILENAME_PREFIX, 1)
    if key.startswith(MEMORY_FILENAME_PREFIX):
        return key.replace(MEMORY_FILENAME_PREFIX, RESULT_FILENAME_PREFIX, 1)
    return None


def _mtime_utc(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _legacy_expires_at_from_mtime(path: Path) -> datetime | None:
    mtime = _mtime_utc(path)
    if mtime is None:
        return None
    return mtime + timedelta(hours=_calc_storage_ttl_hours())


def _envelope_expires_at(envelope: dict, path: Path) -> datetime | None:
    expires_at = _parse_calc_storage_timestamp(envelope.get("expires_at"))
    if expires_at is not None:
        return expires_at
    created_at = _parse_calc_storage_timestamp(envelope.get("created_at"))
    if created_at is not None:
        return created_at + timedelta(hours=_calc_storage_ttl_hours())
    # Legado: ausência de timestamps NÃO significa expirado. Fallback transitório = mtime + TTL.
    return _legacy_expires_at_from_mtime(path)


def _is_calc_storage_expired(envelope: dict, path: Path) -> bool:
    expires_at = _envelope_expires_at(envelope, path)
    if expires_at is None:
        return False
    return _as_utc(_utcnow()) >= expires_at


def _purge_expired_calc_storage_pair(
    *,
    result_storage_key: str | None = None,
    memory_storage_key: str | None = None,
) -> None:
    result_keys: set[str] = set()
    memory_keys: set[str] = set()
    result_key = (result_storage_key or "").strip()
    memory_key = (memory_storage_key or "").strip()
    if result_key:
        result_keys.add(result_key)
        paired = paired_calc_storage_key(result_key)
        if paired:
            memory_keys.add(paired)
    if memory_key:
        memory_keys.add(memory_key)
        paired = paired_calc_storage_key(memory_key)
        if paired:
            result_keys.add(paired)
    for key in result_keys:
        delete_comparison_calculation_result(key)
    for key in memory_keys:
        delete_comparison_calculation_memories(key)


def _raise_expired_calc_storage(
    *,
    artifact_type: str,
    storage_key: str,
    envelope: dict,
    comparison_id: str,
    fingerprint: str,
    missing_error: str,
    missing_message: str,
) -> None:
    logger.info(
        "agente_compara_%s_expired comparison_id=%s fingerprint=%s",
        artifact_type,
        (comparison_id or "")[:32],
        (fingerprint or "")[:12],
    )
    result_key = storage_key if artifact_type == "result" else (envelope.get("result_storage_key") or paired_calc_storage_key(storage_key))
    memory_key = storage_key if artifact_type == "memory" else (envelope.get("memory_storage_key") or paired_calc_storage_key(storage_key))
    _purge_expired_calc_storage_pair(
        result_storage_key=result_key if isinstance(result_key, str) else None,
        memory_storage_key=memory_key if isinstance(memory_key, str) else None,
    )
    raise AgenteComparaCalculationResultStorageError(
        missing_error,
        missing_message,
    )


def _peek_calc_storage_envelope(path: Path) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            envelope = json.load(handle)
    except Exception:
        return None
    return envelope if isinstance(envelope, dict) else None


def _sweep_expires_at(envelope: dict, path: Path) -> datetime | None:
    """
    Instantâneo de expiração para sweep.

    expires_at válido → autoridade.
    Ausência de expires_at → legado (mtime + TTL).
    expires_at presente porém inválido → None (não apagar).
    """
    if "expires_at" not in envelope or envelope.get("expires_at") is None:
        return _legacy_expires_at_from_mtime(path)
    parsed = _parse_calc_storage_timestamp(envelope.get("expires_at"))
    if parsed is None:
        logger.info("agente_compara_calc_sweep_skipped_invalid_expires_at")
        return None
    return parsed


def _purge_expired_recognized_calc_file(path: Path, envelope: dict) -> bool:
    key = path.stem
    try:
        resolved_path = path.resolve()
        if key.startswith(RESULT_FILENAME_PREFIX):
            if resolve_result_storage_path(key) != resolved_path:
                return False
            memory_key = envelope.get("memory_storage_key")
            _purge_expired_calc_storage_pair(
                result_storage_key=key,
                memory_storage_key=memory_key if isinstance(memory_key, str) else None,
            )
            return True
        if key.startswith(MEMORY_FILENAME_PREFIX):
            if resolve_memory_storage_path(key) != resolved_path:
                return False
            _purge_expired_calc_storage_pair(memory_storage_key=key)
            return True
    except (AgenteComparaCalculationResultStorageError, OSError):
        return False
    return False


def maybe_cleanup_expired_calculation_storage(
    directory: Path | None = None,
    *,
    min_interval_seconds: int = _CALC_STORAGE_SWEEP_MIN_INTERVAL_SECONDS,
    max_files: int = _CALC_STORAGE_SWEEP_MAX_FILES,
) -> int:
    """
    Sweep oportunístico limitado de cc_result_/cc_memory_ claramente expirados.

    Novos: expires_at explícito. Legado sem expires_at: mtime + TTL.
    Não remove JSON corrompido nem expires_at inválido. Sem scheduler.
    """
    global _calc_storage_sweep_monotonic
    now_mono = time.monotonic()
    if (now_mono - _calc_storage_sweep_monotonic) < max(0, int(min_interval_seconds)):
        return 0
    _calc_storage_sweep_monotonic = now_mono
    target = directory
    if target is None:
        try:
            target = get_calculation_result_storage_dir()
        except Exception:
            return 0
    removed = 0
    try:
        entries = list(target.iterdir())
    except OSError:
        return 0
    inspected = 0
    for path in entries:
        if inspected >= max(1, int(max_files)):
            break
        if not path.is_file() or path.suffix.lower() != ".json":
            continue
        name = path.name
        if not (name.startswith(RESULT_FILENAME_PREFIX) or name.startswith(MEMORY_FILENAME_PREFIX)):
            continue
        inspected += 1
        envelope = _peek_calc_storage_envelope(path)
        if envelope is None:
            continue
        expires_at = _sweep_expires_at(envelope, path)
        if expires_at is None:
            continue
        if _as_utc(_utcnow()) < expires_at:
            continue
        if _purge_expired_recognized_calc_file(path, envelope):
            removed += 1
    return removed


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
    created_at: str | datetime | None = None,
    expires_at: str | datetime | None = None,
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
    retention = _retention_window_from_inputs(
        created_at=created_at,
        expires_at=expires_at,
        meta=memory_storage_meta,
    )

    envelope = {
        "schema_version": int(schema_version),
        "comparison_id": (comparison_id or "").strip(),
        "request_fingerprint": (fingerprint or "").strip(),
        "created_at": retention["created_at"],
        "expires_at": retention["expires_at"],
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
    maybe_cleanup_expired_calculation_storage(path.parent)
    return {
        "result_storage_key": storage_key,
        "result_checksum": checksum,
        "result_size_bytes": size,
        "result_envelope_size_bytes": metrics.get("result_envelope_size_bytes"),
        "result_limit_bytes": RESULT_MAX_BYTES,
        "result_schema_version": int(result.get("schema_version") or 1),
        "schema_version": int(schema_version),
        "created_at": retention["created_at"],
        "expires_at": retention["expires_at"],
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

    if _is_calc_storage_expired(envelope, path):
        _raise_expired_calc_storage(
            artifact_type="result",
            storage_key=storage_key,
            envelope=envelope,
            comparison_id=comparison_id,
            fingerprint=fingerprint,
            missing_error=ERROR_RESULT_MISSING,
            missing_message="Resultado comparativo não encontrado.",
        )

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
    created_at: str | datetime | None = None,
    expires_at: str | datetime | None = None,
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
    retention = build_calc_storage_retention_window(created_at=created_at, expires_at=expires_at)
    envelope = {
        "schema_version": int(schema_version),
        "comparison_id": (comparison_id or "").strip(),
        "request_fingerprint": (fingerprint or "").strip(),
        "created_at": retention["created_at"],
        "expires_at": retention["expires_at"],
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
    maybe_cleanup_expired_calculation_storage(path.parent)
    return {
        "memory_storage_key": storage_key,
        "memory_checksum": checksum,
        "memory_size_bytes": size,
        "memory_envelope_size_bytes": metrics.get("memory_envelope_size_bytes"),
        "memory_limit_bytes": MEMORY_MAX_BYTES,
        "memory_schema_version": int(memory_payload.get("schema_version") or schema_version),
        "schema_version": int(schema_version),
        "created_at": retention["created_at"],
        "expires_at": retention["expires_at"],
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
    if not isinstance(envelope, dict):
        raise AgenteComparaCalculationResultStorageError(ERROR_MEMORY_CORRUPT, "Memórias corrompidas.")
    payload = envelope.get("memory_payload")
    if not isinstance(payload, dict):
        raise AgenteComparaCalculationResultStorageError(ERROR_MEMORY_CORRUPT, "Memórias corrompidas.")
    if (envelope.get("comparison_id") or "").strip() != (comparison_id or "").strip():
        raise AgenteComparaCalculationResultStorageError(ERROR_RESULT_IDENTITY_MISMATCH, "Memórias não pertencem à comparação informada.")
    if (envelope.get("request_fingerprint") or "").strip() != (fingerprint or "").strip():
        raise AgenteComparaCalculationResultStorageError(ERROR_RESULT_IDENTITY_MISMATCH, "Memórias não correspondem à configuração atual.")
    raw = _canonical_json_bytes(payload)
    checksum = sha256_hex_of_bytes(raw)
    if checksum != (envelope.get("memory_checksum") or "").strip():
        raise AgenteComparaCalculationResultStorageError(ERROR_MEMORY_CORRUPT, "Memórias corrompidas.")
    if expected_checksum and checksum != (expected_checksum or "").strip():
        raise AgenteComparaCalculationResultStorageError(ERROR_MEMORY_CORRUPT, "Memórias corrompidas.")
    if _is_calc_storage_expired(envelope, path):
        _raise_expired_calc_storage(
            artifact_type="memory",
            storage_key=storage_key,
            envelope=envelope,
            comparison_id=comparison_id,
            fingerprint=fingerprint,
            missing_error=ERROR_MEMORY_MISSING,
            missing_message="Memórias não encontradas.",
        )
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
