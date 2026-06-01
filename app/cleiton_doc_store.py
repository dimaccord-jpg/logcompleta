"""
Store temporário em disco para metadados documentais do Cleiton.

Persiste apenas JSON técnico fora do banco. Sem conteúdo bruto de documento
e sem taxonomia de negócio.
"""
from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from app.cleiton_doc_contracts import (
    CLEANUP_META_FILENAME,
    ERROR_DOC_ID_INVALID,
    ERROR_STORE_PATH,
    FIELD_CREATED_AT,
    FIELD_DOC_ID,
    FIELD_EXPIRES_AT,
    TMP_DIR_NAME,
)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo:
            off = dt.utcoffset()
            dt = (dt.replace(tzinfo=None) - off) if off else dt.replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _resolve_data_root() -> str:
    try:
        from app.settings import settings

        root = settings.data_dir
    except (ImportError, AttributeError, RuntimeError):
        root = None
    return root or os.path.dirname(os.path.abspath(__file__))


def get_cleiton_doc_tmp_dir() -> str:
    path = os.path.join(_resolve_data_root(), TMP_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def _cleanup_meta_path() -> str:
    return os.path.join(get_cleiton_doc_tmp_dir(), CLEANUP_META_FILENAME)


def _sanitize_doc_id(doc_id: str | None) -> str:
    raw = (doc_id or "").strip()
    if not raw:
        raise ValueError(ERROR_DOC_ID_INVALID)
    if raw != Path(raw).name:
        raise ValueError(ERROR_DOC_ID_INVALID)
    if any(sep in raw for sep in ("/", "\\")):
        raise ValueError(ERROR_DOC_ID_INVALID)
    safe = "".join(ch for ch in raw if ch.isalnum() or ch in ("-", "_"))
    if not safe or safe != raw:
        raise ValueError(ERROR_DOC_ID_INVALID)
    return safe


def _validate_basename(filename: str | None) -> str:
    raw = (filename or "").strip()
    if not raw:
        raise ValueError(ERROR_STORE_PATH)
    base = Path(raw).name
    if base != raw or base in {".", ".."}:
        raise ValueError(ERROR_STORE_PATH)
    if any(sep in raw for sep in ("/", "\\")):
        raise ValueError(ERROR_STORE_PATH)
    return base


def _build_safe_path(directory: str, filename: str) -> Path:
    safe_name = _validate_basename(filename)
    absolute_dir = Path(directory).resolve()
    candidate = (absolute_dir / safe_name).resolve()
    if absolute_dir != candidate.parent:
        raise ValueError(ERROR_STORE_PATH)
    return candidate


def _doc_json_path(doc_id: str) -> Path:
    safe_id = _sanitize_doc_id(doc_id)
    return _build_safe_path(get_cleiton_doc_tmp_dir(), f"{safe_id}.json")


def _safe_remove_file(path: Path, *, retries: int = 2, retry_delay_s: float = 0.02) -> bool:
    for attempt in range(max(0, int(retries)) + 1):
        try:
            if path.exists():
                path.unlink()
            return True
        except PermissionError:
            if attempt >= retries:
                return False
            time.sleep(max(0.0, float(retry_delay_s)))
        except Exception:
            return False
    return False


def _write_json_atomic(
    path: Path,
    payload: dict,
    *,
    retries: int = 3,
    retry_delay_s: float = 0.03,
) -> None:
    last_error: Exception | None = None
    for attempt in range(max(0, int(retries)) + 1):
        temp_path = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=True)
            os.replace(str(temp_path), str(path))
            return
        except PermissionError as exc:
            last_error = exc
            _safe_remove_file(temp_path)
            if attempt >= retries:
                break
            time.sleep(max(0.0, float(retry_delay_s)))
        except Exception as exc:
            last_error = exc
            _safe_remove_file(temp_path)
            break
    if last_error is not None:
        raise last_error


def _expires_at_from_record(record: dict, ttl_hours: int) -> datetime | None:
    expires_at = _parse_iso(record.get(FIELD_EXPIRES_AT))
    if expires_at is not None:
        return expires_at
    created_at = _parse_iso(record.get(FIELD_CREATED_AT))
    if created_at is None:
        return None
    return created_at + timedelta(hours=max(1, int(ttl_hours)))


def _is_expired(record: dict, ttl_hours: int) -> bool:
    expires_at = _expires_at_from_record(record, ttl_hours)
    if expires_at is None:
        return True
    return _utcnow() >= expires_at


def save_document_record(record: dict) -> None:
    doc_id = _sanitize_doc_id(record.get(FIELD_DOC_ID))
    record = dict(record)
    record[FIELD_DOC_ID] = doc_id
    path = _doc_json_path(doc_id)
    _write_json_atomic(path, record)


def load_document_record(doc_id: str, *, ttl_hours: int) -> dict | None:
    try:
        path = _doc_json_path(doc_id)
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict):
            remove_document_record(doc_id)
            return None
        if _is_expired(payload, ttl_hours):
            remove_document_record(doc_id)
            return None
        return payload
    except Exception:
        remove_document_record(doc_id)
        return None


def remove_document_record(doc_id: str) -> dict:
    try:
        path = _doc_json_path(doc_id)
    except ValueError as exc:
        return {
            "ok": False,
            "doc_id": doc_id,
            "removed": False,
            "error_code": str(exc.args[0]) if exc.args else ERROR_DOC_ID_INVALID,
        }
    removed = _safe_remove_file(path)
    return {
        "ok": True,
        "doc_id": doc_id,
        "removed": removed or not path.exists(),
        "error_code": None,
    }


def list_document_records(*, ttl_hours: int) -> list[dict]:
    base_dir = Path(get_cleiton_doc_tmp_dir())
    active: list[dict] = []
    meta_name = Path(CLEANUP_META_FILENAME).name
    for path in base_dir.iterdir():
        if not path.is_file() or path.suffix.lower() != ".json":
            continue
        if path.name == meta_name:
            continue
        doc_id = path.stem
        record = load_document_record(doc_id, ttl_hours=ttl_hours)
        if record is not None:
            active.append(record)
    return active


def cleanup_expired_document_records(ttl_hours: int) -> int:
    removed = 0
    base_dir = Path(get_cleiton_doc_tmp_dir())
    meta_name = Path(CLEANUP_META_FILENAME).name
    for path in base_dir.iterdir():
        if not path.is_file() or path.suffix.lower() != ".json":
            continue
        if path.name == meta_name:
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if not isinstance(payload, dict) or _is_expired(payload, ttl_hours):
                if _safe_remove_file(path):
                    removed += 1
                continue
        except Exception:
            if _safe_remove_file(path):
                removed += 1
    return removed


def maybe_cleanup_expired_document_records(
    ttl_hours: int,
    *,
    cleanup_enabled: bool = True,
    min_interval_seconds: int = 300,
) -> int:
    if not cleanup_enabled:
        return 0

    now = _utcnow()
    meta_path = Path(_cleanup_meta_path())
    try:
        if meta_path.is_file():
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            last_run = _parse_iso(meta.get("last_run_at"))
            if last_run is not None:
                elapsed = (now - last_run).total_seconds()
                if elapsed < max(30, int(min_interval_seconds)):
                    return 0
    except Exception:
        pass

    removed = cleanup_expired_document_records(ttl_hours)
    try:
        _write_json_atomic(meta_path, {"last_run_at": _utcnow_iso()})
    except Exception:
        pass
    return removed
