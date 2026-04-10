"""
Persistência temporária dedicada do upload do Roberto.

Armazena payload em arquivo JSON em diretório dedicado, com limpeza por TTL.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4


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
    path = _file_path(upload_id)
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=True)
    os.replace(temp_path, path)
    return upload_id


def read_upload_data(upload_id: str, ttl_minutes: int) -> list[dict] | None:
    if not upload_id:
        return None
    path = _file_path(upload_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        created_at = _parse_iso(payload.get("created_at"))
        if created_at is None or _is_expired(created_at, ttl_minutes):
            clear_upload_data(upload_id)
            return None
        rows = payload.get("rows")
        return rows if isinstance(rows, list) else None
    except Exception:
        return None


def clear_upload_data(upload_id: str) -> None:
    if not upload_id:
        return
    path = _file_path(upload_id)
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def cleanup_expired_uploads(ttl_minutes: int) -> int:
    removed = 0
    ttl = max(1, int(ttl_minutes))
    for name in os.listdir(_base_dir()):
        if not name.endswith(".json"):
            continue
        path = os.path.join(_base_dir(), name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            created_at = _parse_iso(payload.get("created_at"))
            if created_at is None or _is_expired(created_at, ttl):
                os.remove(path)
                removed += 1
        except Exception:
            # Arquivo inválido também deve ser limpo para manter o diretório saudável.
            try:
                os.remove(path)
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
    tmp = f"{meta_path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"last_run_at": _utcnow_iso()}, f, ensure_ascii=True)
        os.replace(tmp, meta_path)
    except Exception:
        pass
    return removed
