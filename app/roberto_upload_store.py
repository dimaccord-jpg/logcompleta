"""
Persistência temporária dedicada do upload do Roberto.

Armazena payload em arquivo JSON em diretório dedicado, com limpeza por TTL.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

UPLOAD_OK = "ok"
UPLOAD_NOT_FOUND = "not_found"
UPLOAD_EXPIRED = "expired"
UPLOAD_INVALID = "invalid"
UPLOAD_SCOPE_MISMATCH = "scope_mismatch"


@dataclass(frozen=True)
class UploadReadOutcome:
    status: str
    rows: list[dict] | None = None


def _utcnow_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


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


def _base_dir() -> str:
    root = None
    try:
        from app.settings import settings

        root = settings.data_dir
    except Exception:
        root = None
    root = root or os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(root, "roberto_upload_tmp")
    os.makedirs(path, exist_ok=True)
    return path


def _file_path(upload_id: str) -> str:
    safe_id = "".join(ch for ch in (upload_id or "") if ch.isalnum() or ch in ("-", "_"))
    return os.path.join(_base_dir(), f"{safe_id}.json")


def _cleanup_meta_path() -> str:
    return os.path.join(_base_dir(), ".cleanup_meta.json")


def _safe_remove_file(path: str, *, retries: int = 2, retry_delay_s: float = 0.02) -> bool:
    """
    Remove arquivo com tolerância mínima a lock transitório (Windows).
    """
    for attempt in range(max(0, int(retries)) + 1):
        try:
            if os.path.exists(path):
                os.remove(path)
            return True
        except PermissionError:
            if attempt >= retries:
                return False
            time.sleep(max(0.0, float(retry_delay_s)))
        except Exception:
            return False
    return False


def _write_json_atomic(
    path: str,
    payload: dict,
    *,
    retries: int = 3,
    retry_delay_s: float = 0.03,
) -> None:
    """
    Escrita atômica resiliente a locks transitórios (Windows).
    """
    last_error: Exception | None = None
    for attempt in range(max(0, int(retries)) + 1):
        temp_path = f"{path}.{uuid4().hex}.tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=True)
            os.replace(temp_path, path)
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


def _is_expired(created_at: datetime, ttl_minutes: int) -> bool:
    return datetime.now(UTC).replace(tzinfo=None) - created_at > timedelta(
        minutes=max(1, int(ttl_minutes))
    )


def save_upload_data(rows: list[dict]) -> str:
    upload_id = uuid4().hex
    payload = {
        "upload_id": upload_id,
        "created_at": _utcnow_iso(),
        "rows": rows,
    }
    try:
        from app.cleiton_doc_escopo import stamp_operational_cache_payload

        payload = stamp_operational_cache_payload(payload)
    except Exception:
        pass
    path = _file_path(upload_id)
    _write_json_atomic(path, payload)
    return upload_id


def _payload_owned_by_current_scope(payload: dict) -> bool:
    from app.cleiton_doc_escopo import current_operational_scope, operational_cache_payload_is_current

    if current_operational_scope() is None:
        return True
    return operational_cache_payload_is_current(payload)


def inspect_upload_data(upload_id: str, ttl_minutes: int) -> UploadReadOutcome:
    """
    Distingue MISS, expirado, inválido e SCOPE_MISMATCH.
    SCOPE_MISMATCH nunca autoriza exclusão física.
    """
    if not upload_id:
        return UploadReadOutcome(UPLOAD_NOT_FOUND)
    path = _file_path(upload_id)
    if not os.path.exists(path):
        return UploadReadOutcome(UPLOAD_NOT_FOUND)
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict):
            return UploadReadOutcome(UPLOAD_INVALID)
        from app.cleiton_doc_escopo import current_operational_scope, operational_cache_payload_is_current

        if current_operational_scope() is not None and not operational_cache_payload_is_current(
            payload
        ):
            return UploadReadOutcome(UPLOAD_SCOPE_MISMATCH)
        created_at = _parse_iso(payload.get("created_at"))
        if created_at is None or _is_expired(created_at, ttl_minutes):
            return UploadReadOutcome(UPLOAD_EXPIRED)
        rows = payload.get("rows")
        if not isinstance(rows, list):
            return UploadReadOutcome(UPLOAD_INVALID)
        return UploadReadOutcome(UPLOAD_OK, rows=rows)
    except Exception:
        return UploadReadOutcome(UPLOAD_INVALID)


def read_upload_data(upload_id: str, ttl_minutes: int) -> list[dict] | None:
    outcome = inspect_upload_data(upload_id, ttl_minutes)
    if outcome.status == UPLOAD_OK:
        return outcome.rows
    if outcome.status == UPLOAD_EXPIRED:
        clear_upload_data(upload_id)
    return None


def clear_upload_data(upload_id: str) -> bool:
    """
    Exclui somente com prova de ownership do escopo operacional atual.
    Sem prova (scope mismatch, legado autenticado, JSON ilegível autenticado): não apaga.
    """
    if not upload_id:
        return False
    path = _file_path(upload_id)
    if not os.path.exists(path):
        return True
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict) or not _payload_owned_by_current_scope(payload):
            return False
    except Exception:
        from app.cleiton_doc_escopo import current_operational_scope

        if current_operational_scope() is not None:
            return False
    try:
        return bool(_safe_remove_file(path))
    except Exception:
        return False


def cleanup_expired_uploads(ttl_minutes: int) -> int:
    removed = 0
    ttl = max(1, int(ttl_minutes))
    meta_name = os.path.basename(_cleanup_meta_path())
    for name in os.listdir(_base_dir()):
        if not name.endswith(".json"):
            continue
        if name == meta_name:
            continue
        path = os.path.join(_base_dir(), name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            created_at = _parse_iso(payload.get("created_at"))
            if created_at is None or _is_expired(created_at, ttl):
                if _safe_remove_file(path):
                    removed += 1
        except Exception:
            # Arquivo inválido também deve ser limpo para manter o diretório saudável.
            try:
                if _safe_remove_file(path):
                    removed += 1
            except Exception:
                pass
    return removed


def maybe_cleanup_expired_uploads(ttl_minutes: int, min_interval_seconds: int = 300) -> int:
    """
    Executa varredura de expirados apenas quando o último sweep está vencido.
    Evita custo O(n arquivos) em caminhos de leitura frequentes.
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    meta_path = _cleanup_meta_path()
    try:
        if os.path.exists(meta_path):
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            last_run = _parse_iso(meta.get("last_run_at"))
            if last_run is not None:
                elapsed = (now - last_run).total_seconds()
                if elapsed < max(30, int(min_interval_seconds)):
                    return 0
    except Exception:
        pass

    removed = cleanup_expired_uploads(ttl_minutes)
    try:
        _write_json_atomic(meta_path, {"last_run_at": _utcnow_iso()})
    except Exception:
        pass
    return removed
