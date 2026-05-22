"""
Storage temporario dedicado da Cleide.

Fase 3:
- persistencia temporaria de upload bruto (csv/xlsx) por referencia;
- lifecycle upload-only (um upload ativo por sessao);
- limpeza de expirados por TTL.
"""
from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

_VALID_SUFFIXES = {".csv", ".xlsx"}
_CLEANUP_META_NAME = ".cleanup_meta"

def _resolve_data_root() -> str:
    try:
        from app.settings import settings

        root = settings.data_dir
    except (ImportError, AttributeError, RuntimeError):
        root = None
    return root or os.path.dirname(os.path.abspath(__file__))


def get_cleide_upload_tmp_dir() -> str:
    path = os.path.join(_resolve_data_root(), "cleide_upload_tmp")
    os.makedirs(path, exist_ok=True)
    return path


def _now_utc_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _build_upload_name(*, original_filename: str, safe_filename: str) -> str:
    ext = Path(safe_filename).suffix.lower()
    if ext not in _VALID_SUFFIXES:
        raise ValueError("Extensao de upload Cleide invalida.")
    stamp = _now_utc_naive().strftime("%Y%m%d%H%M%S")
    return f"cleide_{stamp}_{uuid4().hex}_{safe_filename}"


def _clean_upload_ref(upload_ref: str) -> str:
    raw = (upload_ref or "").strip()
    if not raw:
        raise ValueError("Upload ref da Cleide invalido.")
    if any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in raw):
        raise ValueError("Upload ref da Cleide invalido.")
    return raw


def _upload_path_for_ref(upload_ref: str, *, ext_hint: str = ".csv") -> Path:
    ref = _clean_upload_ref(upload_ref)
    ext = ext_hint.lower()
    if ext not in _VALID_SUFFIXES:
        ext = ".csv"
    return _build_safe_path(Path(get_cleide_upload_tmp_dir()), f"{ref}{ext}")


def save_cleide_upload_file(*, file_storage, safe_filename: str) -> dict:
    upload_ref = uuid4().hex
    final_name = _build_upload_name(
        original_filename=file_storage.filename or "",
        safe_filename=safe_filename,
    )
    absolute_path = _build_safe_path(Path(get_cleide_upload_tmp_dir()), final_name)
    file_storage.save(str(absolute_path))
    if not absolute_path.is_file():
        raise ValueError("Falha ao persistir upload da Cleide.")
    ref_path = _upload_path_for_ref(upload_ref, ext_hint=Path(final_name).suffix)
    try:
        absolute_path.replace(ref_path)
    except OSError:
        if absolute_path.exists():
            absolute_path.unlink(missing_ok=True)
        raise
    return {
        "upload_ref": upload_ref,
        "absolute_path": str(ref_path),
        "safe_filename": safe_filename,
        "stored_filename": ref_path.name,
        "file_size_bytes": int(ref_path.stat().st_size),
    }


def clear_cleide_upload_file(upload_ref: str | None) -> None:
    if not upload_ref:
        return
    ref = _clean_upload_ref(upload_ref)
    base_dir = Path(get_cleide_upload_tmp_dir())
    for ext in _VALID_SUFFIXES:
        try:
            p = _build_safe_path(base_dir, f"{ref}{ext}")
        except ValueError:
            continue
        p.unlink(missing_ok=True)


def resolve_cleide_upload_file(upload_ref: str | None) -> Path | None:
    if not upload_ref:
        return None
    ref = _clean_upload_ref(upload_ref)
    base_dir = Path(get_cleide_upload_tmp_dir())
    for ext in _VALID_SUFFIXES:
        p = _build_safe_path(base_dir, f"{ref}{ext}")
        if p.exists() and p.is_file():
            return p
    return None


def cleanup_expired_cleide_uploads(ttl_minutes: int) -> int:
    removed = 0
    ttl = max(1, int(ttl_minutes))
    base_dir = Path(get_cleide_upload_tmp_dir())
    deadline = _now_utc_naive() - timedelta(minutes=ttl)
    for path in base_dir.iterdir():
        if not path.is_file():
            continue
        if path.name == _CLEANUP_META_NAME:
            continue
        if path.suffix.lower() not in _VALID_SUFFIXES:
            continue
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        if mtime < deadline:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def maybe_cleanup_expired_cleide_uploads(ttl_minutes: int, *, min_interval_seconds: int = 300) -> int:
    meta_path = _build_safe_path(Path(get_cleide_upload_tmp_dir()), _CLEANUP_META_NAME)
    now = _now_utc_naive()
    if meta_path.exists():
        elapsed = now.timestamp() - meta_path.stat().st_mtime
        if elapsed < max(30, int(min_interval_seconds)):
            return 0
    removed = cleanup_expired_cleide_uploads(ttl_minutes)
    try:
        meta_path.touch()
    except OSError as exc:
        if getattr(exc, "errno", None) not in (13, 16):
            raise
        time.sleep(0)
    return removed


def get_upload_ref_extension(upload_ref: str | None) -> str | None:
    p = resolve_cleide_upload_file(upload_ref)
    if p is None:
        return None
    return p.suffix.lower()


def _validate_basename(filename: str | None) -> str:
    raw = (filename or "").strip()
    if not raw:
        raise ValueError("Nome de arquivo invalido para upload Cleide.")
    base = Path(raw).name
    if base != raw or base in {".", ".."}:
        raise ValueError("Nome de arquivo invalido para upload Cleide.")
    if any(sep in raw for sep in ("/", "\\")):
        raise ValueError("Nome de arquivo invalido para upload Cleide.")
    return base


def _build_safe_path(directory: Path, filename: str | None) -> Path:
    safe_name = _validate_basename(filename)
    absolute_dir = directory.resolve()
    candidate = (absolute_dir / safe_name).resolve()
    if absolute_dir != candidate.parent:
        raise ValueError("Path invalido para storage temporario da Cleide.")
    return candidate
