"""
Lock de arquivo por comparison_id para cálculo comparativo (AgenteCompara).

Lock exclusivo multiplataforma (msvcrt no Windows, fcntl no Unix), com timeout
controlado. Inclui mutex in-process complementar (threading) porque locks OS
podem ser reentrantes no mesmo processo em alguns ambientes Windows.

Não usa Redis/Celery. Não importa Cleide.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.agente_compara_calculation_result_storage import get_calculation_result_storage_dir
from app.cleiton_doc_store import _build_safe_path, _sanitize_doc_id

logger = logging.getLogger(__name__)

DEFAULT_LOCK_TIMEOUT_SECONDS = 8.0
DEFAULT_LOCK_POLL_SECONDS = 0.05

ERROR_LOCK_TIMEOUT = "agente_compara_calculation_lock_timeout"
ERROR_LOCK_FAILED = "agente_compara_calculation_lock_failed"

_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.Lock] = {}


def _process_local_lock(comparison_id: str) -> threading.Lock:
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(comparison_id)
        if lock is None:
            lock = threading.Lock()
            _PROCESS_LOCKS[comparison_id] = lock
        return lock


class AgenteComparaCalculationLockError(Exception):
    def __init__(self, error_code: str, message: str, *, http_status: int = 409):
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.http_status = http_status


def _lock_filename(comparison_id: str) -> str:
    safe = _sanitize_doc_id((comparison_id or "").strip())
    return f"cc_lock_{safe}.lock"


def resolve_calculation_lock_path(comparison_id: str) -> Path:
    directory = get_calculation_result_storage_dir()
    return _build_safe_path(str(directory), _lock_filename(comparison_id))


def _acquire_os_lock(handle) -> None:
    if os.name == "nt":
        import msvcrt

        # Lock 1 byte from start.
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_os_lock(handle) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        logger.debug("Falha ao liberar OS lock do cálculo comparativo.", exc_info=True)


def _raise_lock_timeout(cmp_id: str) -> None:
    logger.info(
        "agente_compara_lock_timeout comparison_id=%s",
        cmp_id[:32],
    )
    raise AgenteComparaCalculationLockError(
        ERROR_LOCK_TIMEOUT,
        "Já existe um cálculo comparativo em andamento. Tente novamente em instantes.",
        http_status=409,
    )


@contextmanager
def acquire_comparison_calculation_lock(
    comparison_id: str,
    *,
    timeout_seconds: float | None = None,
    poll_seconds: float | None = None,
) -> Iterator[Path]:
    """
    Adquire lock exclusivo por comparison_id.

    Yields o path lógico do lock (não deve ser exposto em HTTP).
    """
    cmp_id = (comparison_id or "").strip()
    if not cmp_id:
        raise AgenteComparaCalculationLockError(
            ERROR_LOCK_FAILED,
            "comparison_id inválido para lock.",
            http_status=400,
        )

    effective_timeout = (
        DEFAULT_LOCK_TIMEOUT_SECONDS if timeout_seconds is None else float(timeout_seconds)
    )
    effective_poll = DEFAULT_LOCK_POLL_SECONDS if poll_seconds is None else float(poll_seconds)

    local_lock = _process_local_lock(cmp_id)
    path = resolve_calculation_lock_path(cmp_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0.1, effective_timeout)
    handle = None
    acquired_os = False
    acquired_local = False

    try:
        while True:
            if local_lock.acquire(blocking=False):
                acquired_local = True
                break
            if time.monotonic() >= deadline:
                _raise_lock_timeout(cmp_id)
            time.sleep(max(0.01, effective_poll))

        # 'a+' cria o arquivo se necessário e permite locking.
        handle = open(path, "a+b")
        # Garantir pelo menos 1 byte antes do lock OS: em Windows, msvcrt.locking
        # sobre região inexistente/esvaziada por truncate(0) libera o lock.
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()

        while True:
            try:
                _acquire_os_lock(handle)
                acquired_os = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    _raise_lock_timeout(cmp_id)
                time.sleep(max(0.01, effective_poll))

        # Nunca truncate(0) após o lock OS — no Windows isso libera msvcrt.locking.
        meta = f"pid={os.getpid()};ts={time.time():.3f}\n".encode("utf-8")
        handle.seek(0)
        handle.write(meta)
        handle.flush()
        logger.info(
            "agente_compara_lock_acquired comparison_id=%s",
            cmp_id[:32],
        )
        yield path
    finally:
        if handle is not None:
            if acquired_os:
                _release_os_lock(handle)
                logger.info(
                    "agente_compara_lock_released comparison_id=%s",
                    cmp_id[:32],
                )
            try:
                handle.close()
            except Exception:
                pass
        if acquired_local:
            try:
                local_lock.release()
            except Exception:
                pass
