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

RESULT_STORAGE_SCHEMA_VERSION = 1
RESULT_SUBDIR_NAME = "agente_compara_calc"
RESULT_FILENAME_PREFIX = "cc_result_"

# Cenário medido ~5.14 MB (2000×3). Limite técnico folgado acima do caso aprovado.
RESULT_MAX_BYTES = 16 * 1024 * 1024

ERROR_RESULT_STORAGE_KEY_INVALID = "agente_compara_calculation_result_storage_key_invalid"
ERROR_RESULT_MISSING = "calculation_result_missing"
ERROR_RESULT_CORRUPT = "calculation_result_corrupt"
ERROR_RESULT_TOO_LARGE = "calculation_result_too_large"
ERROR_RESULT_IDENTITY_MISMATCH = "calculation_result_identity_mismatch"
ERROR_RESULT_FORBIDDEN_FIELD = "calculation_result_forbidden_field"

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
    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


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
) -> dict:
    """
    Serializa, valida e grava atomicamente o resultado.

    Retorna metadados leves para a sessão (sem path absoluto).
    """
    if not isinstance(result, dict):
        raise AgenteComparaCalculationResultStorageError(
            ERROR_RESULT_CORRUPT,
            "Resultado inválido para persistência.",
        )
    try:
        raw = _canonical_json_bytes(result)
    except (TypeError, ValueError) as exc:
        raise AgenteComparaCalculationResultStorageError(
            ERROR_RESULT_CORRUPT,
            "Resultado não serializável.",
        ) from exc

    size = len(raw)
    if size > RESULT_MAX_BYTES:
        raise AgenteComparaCalculationResultStorageError(
            ERROR_RESULT_TOO_LARGE,
            "Resultado excede o limite técnico de armazenamento.",
        )

    _assert_no_forbidden_fields(result)
    checksum = sha256_hex_of_bytes(raw)
    storage_key = build_result_storage_key(comparison_id=comparison_id, fingerprint=fingerprint)
    path = resolve_result_storage_path(storage_key)

    envelope = {
        "schema_version": int(schema_version),
        "comparison_id": (comparison_id or "").strip(),
        "request_fingerprint": (fingerprint or "").strip(),
        "result_schema_version": int(result.get("schema_version") or 1),
        "result_checksum": checksum,
        "result_size_bytes": size,
        "result": result,
    }

    # Validar serialização do envelope com allow_nan=False antes de gravar.
    try:
        _canonical_json_bytes(envelope)
    except (TypeError, ValueError) as exc:
        raise AgenteComparaCalculationResultStorageError(
            ERROR_RESULT_CORRUPT,
            "Envelope do resultado não serializável.",
        ) from exc

    try:
        _write_json_atomic(path, envelope)
    except Exception as exc:
        logger.exception(
            "agente_compara_result_save_failed comparison_id=%s fingerprint=%s size=%s",
            (comparison_id or "")[:32],
            (fingerprint or "")[:12],
            size,
        )
        raise AgenteComparaCalculationResultStorageError(
            ERROR_RESULT_CORRUPT,
            "Não foi possível gravar o resultado comparativo.",
        ) from exc

    # Confirma existência pós-replace.
    if not path.is_file():
        raise AgenteComparaCalculationResultStorageError(
            ERROR_RESULT_MISSING,
            "Arquivo de resultado ausente após gravação.",
        )

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
        "result_schema_version": int(result.get("schema_version") or 1),
        "schema_version": int(schema_version),
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
