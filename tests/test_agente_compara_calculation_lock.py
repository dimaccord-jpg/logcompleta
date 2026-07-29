"""Lock por comparison_id para cálculo comparativo (correção Etapa 5)."""
from __future__ import annotations

import json
import multiprocessing
import os
import threading
import time
from pathlib import Path

import pytest

from app.agente_compara_calculation_lock import (
    ERROR_LOCK_TIMEOUT,
    AgenteComparaCalculationLockError,
    acquire_comparison_calculation_lock,
    resolve_calculation_lock_path,
)
from tests.cleiton_doc_fixtures import patch_cleiton_doc_store


@pytest.fixture
def lock_env(tmp_path, monkeypatch):
    # patch_cleiton_doc_store altera só app.cleiton_doc_store; o storage importa
    # get_cleiton_doc_tmp_dir por binding local — precisa patchar ambos.
    patch_cleiton_doc_store(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "app.agente_compara_calculation_result_storage.get_cleiton_doc_tmp_dir",
        lambda: str(tmp_path),
    )
    return tmp_path


def test_same_comparison_blocks_second_acquire(lock_env):
    cmp_id = "cmp-lock-same"
    acquired = threading.Event()
    release = threading.Event()

    def holder():
        with acquire_comparison_calculation_lock(cmp_id, timeout_seconds=2.0):
            acquired.set()
            release.wait(timeout=5.0)

    t = threading.Thread(target=holder)
    t.start()
    assert acquired.wait(timeout=2.0)
    with pytest.raises(AgenteComparaCalculationLockError) as exc:
        with acquire_comparison_calculation_lock(cmp_id, timeout_seconds=0.3):
            pass
    assert exc.value.error_code == ERROR_LOCK_TIMEOUT
    assert exc.value.http_status == 409
    msg = exc.value.message
    assert ":\\" not in msg
    assert "/agente_compara_calc/" not in msg
    release.set()
    t.join(timeout=3.0)


def test_different_comparisons_do_not_block(lock_env):
    with acquire_comparison_calculation_lock("cmp-a", timeout_seconds=1.0):
        with acquire_comparison_calculation_lock("cmp-b", timeout_seconds=1.0):
            assert True


def test_lock_released_after_success(lock_env):
    cmp_id = "cmp-lock-release"
    with acquire_comparison_calculation_lock(cmp_id, timeout_seconds=1.0):
        pass
    with acquire_comparison_calculation_lock(cmp_id, timeout_seconds=1.0):
        pass


def test_lock_released_after_exception(lock_env):
    cmp_id = "cmp-lock-exc"
    with pytest.raises(RuntimeError):
        with acquire_comparison_calculation_lock(cmp_id, timeout_seconds=1.0):
            raise RuntimeError("boom")
    with acquire_comparison_calculation_lock(cmp_id, timeout_seconds=1.0):
        pass


def test_timeout_message_has_no_absolute_path(lock_env):
    cmp_id = "cmp-lock-path"
    path = resolve_calculation_lock_path(cmp_id)
    assert path.is_absolute()
    with acquire_comparison_calculation_lock(cmp_id, timeout_seconds=1.0):
        with pytest.raises(AgenteComparaCalculationLockError) as exc:
            with acquire_comparison_calculation_lock(cmp_id, timeout_seconds=0.2):
                pass
        assert str(path) not in exc.value.message
        assert str(path.parent) not in exc.value.message


def test_orphan_lock_file_does_not_block_acquire(lock_env):
    """Mere existência do arquivo .lock sem handle ativo não bloqueia aquisição."""
    cmp_id = "cmp-lock-orphan"
    path = resolve_calculation_lock_path(cmp_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"stale-orphan-lock\n")
    assert path.is_file()
    with acquire_comparison_calculation_lock(cmp_id, timeout_seconds=1.0):
        pass


# ---------------------------------------------------------------------------
# Multiprocess proof (spawn + file signals — no multiprocessing.Queue)
# ---------------------------------------------------------------------------


def _wait_for_file(path: Path, timeout_seconds: float = 10.0) -> None:
    deadline = time.monotonic() + float(timeout_seconds)
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.05)
    raise TimeoutError(f"Arquivo-sinal não apareceu a tempo: {path.name}")


def _write_json_result(path_str: str, payload: dict) -> None:
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))


def _touch_flag(path_str: str, *, note: str = "1") -> None:
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    # monotonic apenas para diagnóstico; não é critério de sucesso.
    path.write_text(f"{note}|mono={time.monotonic():.6f}", encoding="utf-8")


def _patch_tmp_dir(shared_dir: str) -> None:
    import app.agente_compara_calculation_result_storage as result_storage
    import app.cleiton_doc_store as store

    store.get_cleiton_doc_tmp_dir = lambda: shared_dir  # type: ignore[method-assign]
    result_storage.get_cleiton_doc_tmp_dir = lambda: shared_dir  # type: ignore[method-assign]


def _read_json_result(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _join_process(proc: multiprocessing.Process, *, timeout: float, label: str) -> None:
    proc.join(timeout=timeout)
    if proc.is_alive():
        proc.terminate()
        proc.join(timeout=5.0)
        raise AssertionError(f"Processo {label} permaneceu vivo após timeout={timeout}s")


def _multiprocess_lock_holder(
    shared_dir: str,
    comparison_id: str,
    acquired_flag: str,
    release_allowed_flag: str,
    released_flag: str,
    result_path: str,
) -> None:
    """Processo A: adquire, sinaliza, aguarda release_allowed, libera."""
    try:
        _patch_tmp_dir(shared_dir)
        from app.agente_compara_calculation_lock import (
            AgenteComparaCalculationLockError,
            acquire_comparison_calculation_lock,
            resolve_calculation_lock_path,
        )

        lock_path = str(resolve_calculation_lock_path(comparison_id))
        with acquire_comparison_calculation_lock(comparison_id, timeout_seconds=2.0):
            _touch_flag(acquired_flag, note=f"acquired|path={lock_path}")
            _wait_for_file(Path(release_allowed_flag), timeout_seconds=30.0)
        _touch_flag(released_flag, note="released")
        _write_json_result(
            result_path,
            {
                "status": "acquired_and_released",
                "lock_path": lock_path,
                "pid": os.getpid(),
            },
        )
    except AgenteComparaCalculationLockError as exc:
        _write_json_result(
            result_path,
            {
                "status": "timeout",
                "error_type": type(exc).__name__,
                "error_code": getattr(exc, "error_code", None),
                "pid": os.getpid(),
            },
        )
    except Exception as exc:  # pragma: no cover - diagnostic path
        _write_json_result(
            result_path,
            {
                "status": "unexpected_error",
                "exception_type": type(exc).__name__,
                "message": str(exc)[:400],
                "pid": os.getpid(),
            },
        )


def _multiprocess_lock_contender(
    shared_dir: str,
    comparison_id: str,
    lock_timeout_seconds: float,
    acquired_flag: str,
    started_flag: str,
    inside_flag: str,
    result_path: str,
) -> None:
    """Processo B: após holder_acquired, tenta o mesmo comparison_id."""
    try:
        _patch_tmp_dir(shared_dir)
        from app.agente_compara_calculation_lock import (
            ERROR_LOCK_TIMEOUT,
            AgenteComparaCalculationLockError,
            acquire_comparison_calculation_lock,
            resolve_calculation_lock_path,
        )

        _wait_for_file(Path(acquired_flag), timeout_seconds=20.0)
        lock_path = str(resolve_calculation_lock_path(comparison_id))
        _touch_flag(started_flag, note=f"started|path={lock_path}")
        try:
            with acquire_comparison_calculation_lock(
                comparison_id,
                timeout_seconds=float(lock_timeout_seconds),
            ):
                _touch_flag(inside_flag, note="inside")
                _write_json_result(
                    result_path,
                    {
                        "status": "acquired",
                        "lock_path": lock_path,
                        "pid": os.getpid(),
                    },
                )
        except AgenteComparaCalculationLockError as exc:
            status = "timeout" if exc.error_code == ERROR_LOCK_TIMEOUT else "lock_error"
            _write_json_result(
                result_path,
                {
                    "status": status,
                    "error_type": type(exc).__name__,
                    "error_code": exc.error_code,
                    "lock_path": lock_path,
                    "pid": os.getpid(),
                },
            )
    except Exception as exc:  # pragma: no cover - diagnostic path
        _write_json_result(
            result_path,
            {
                "status": "unexpected_error",
                "exception_type": type(exc).__name__,
                "message": str(exc)[:400],
                "pid": os.getpid(),
            },
        )


def _multiprocess_other_comparison(
    shared_dir: str,
    comparison_id: str,
    acquired_flag: str,
    result_path: str,
) -> None:
    """Processo C: comparison_id diferente deve adquirir enquanto A segura o original."""
    try:
        _patch_tmp_dir(shared_dir)
        from app.agente_compara_calculation_lock import (
            AgenteComparaCalculationLockError,
            acquire_comparison_calculation_lock,
            resolve_calculation_lock_path,
        )

        _wait_for_file(Path(acquired_flag), timeout_seconds=20.0)
        lock_path = str(resolve_calculation_lock_path(comparison_id))
        try:
            with acquire_comparison_calculation_lock(comparison_id, timeout_seconds=2.0):
                _write_json_result(
                    result_path,
                    {
                        "status": "acquired",
                        "lock_path": lock_path,
                        "pid": os.getpid(),
                    },
                )
        except AgenteComparaCalculationLockError as exc:
            _write_json_result(
                result_path,
                {
                    "status": "timeout",
                    "error_type": type(exc).__name__,
                    "error_code": exc.error_code,
                    "lock_path": lock_path,
                    "pid": os.getpid(),
                },
            )
    except Exception as exc:  # pragma: no cover - diagnostic path
        _write_json_result(
            result_path,
            {
                "status": "unexpected_error",
                "exception_type": type(exc).__name__,
                "message": str(exc)[:400],
                "pid": os.getpid(),
            },
        )


def _multiprocess_post_release(
    shared_dir: str,
    comparison_id: str,
    released_flag: str,
    result_path: str,
) -> None:
    """Processo D: após liberação, mesmo comparison_id deve adquirir."""
    try:
        _patch_tmp_dir(shared_dir)
        from app.agente_compara_calculation_lock import (
            AgenteComparaCalculationLockError,
            acquire_comparison_calculation_lock,
        )

        _wait_for_file(Path(released_flag), timeout_seconds=20.0)
        try:
            with acquire_comparison_calculation_lock(comparison_id, timeout_seconds=2.0):
                _write_json_result(
                    result_path,
                    {"status": "acquired", "pid": os.getpid()},
                )
        except AgenteComparaCalculationLockError as exc:
            _write_json_result(
                result_path,
                {
                    "status": "timeout",
                    "error_type": type(exc).__name__,
                    "error_code": exc.error_code,
                    "pid": os.getpid(),
                },
            )
    except Exception as exc:  # pragma: no cover - diagnostic path
        _write_json_result(
            result_path,
            {
                "status": "unexpected_error",
                "exception_type": type(exc).__name__,
                "message": str(exc)[:400],
                "pid": os.getpid(),
            },
        )


def test_multiprocess_same_comparison_lock(lock_env):
    """
    Prova multiprocesso real via spawn + handshake por arquivos-sinal.

    Holder libera somente após `release_allowed` do pai — sem hold baseado
    apenas em sleep. Não usa multiprocessing.Queue / Manager.
    """
    ctx = multiprocessing.get_context("spawn")
    shared = os.fspath(lock_env)
    signal_dir = Path(lock_env) / "mp_signals"
    signal_dir.mkdir(parents=True, exist_ok=True)

    cmp_same = "cmp-lock-mp-same"
    cmp_other = "cmp-lock-mp-other"

    acquired_flag = os.fspath(signal_dir / "holder_acquired.flag")
    contender_started = os.fspath(signal_dir / "contender_started.flag")
    contender_inside = os.fspath(signal_dir / "contender_inside.flag")
    release_allowed = os.fspath(signal_dir / "release_allowed.flag")
    released_flag = os.fspath(signal_dir / "holder_released.flag")

    holder_result = os.fspath(signal_dir / "holder_result.json")
    contender_result = os.fspath(signal_dir / "contender_result.json")
    other_result = os.fspath(signal_dir / "other_comparison_result.json")
    post_result = os.fspath(signal_dir / "post_release_result.json")

    # Timeout curto e conhecido: holder permanece vivo até o pai liberar.
    contender_lock_timeout = 0.8

    processes: list[multiprocessing.Process] = []
    try:
        holder = ctx.Process(
            target=_multiprocess_lock_holder,
            args=(
                shared,
                cmp_same,
                acquired_flag,
                release_allowed,
                released_flag,
                holder_result,
            ),
            name="lock-holder",
        )
        processes.append(holder)
        holder.start()

        # Espera prova de aquisição; se o holder morrer, expõe o JSON de erro.
        try:
            _wait_for_file(Path(acquired_flag), timeout_seconds=25.0)
        except TimeoutError:
            if not holder.is_alive():
                holder_payload = (
                    _read_json_result(Path(holder_result))
                    if Path(holder_result).is_file()
                    else {"status": "missing_result"}
                )
                raise AssertionError(
                    "Holder morreu antes de holder_acquired.flag: "
                    f"exitcode={holder.exitcode} payload={holder_payload}"
                ) from None
            raise

        assert holder.is_alive(), (
            "Holder não está vivo após holder_acquired.flag; "
            f"exitcode={holder.exitcode}"
        )

        contender = ctx.Process(
            target=_multiprocess_lock_contender,
            args=(
                shared,
                cmp_same,
                contender_lock_timeout,
                acquired_flag,
                contender_started,
                contender_inside,
                contender_result,
            ),
            name="lock-contender",
        )
        other = ctx.Process(
            target=_multiprocess_other_comparison,
            args=(shared, cmp_other, acquired_flag, other_result),
            name="lock-other",
        )
        processes.extend([contender, other])
        contender.start()
        other.start()

        _wait_for_file(Path(contender_started), timeout_seconds=25.0)
        assert holder.is_alive(), "Holder morreu durante a tentativa do contender"

        _join_process(contender, timeout=25.0, label="contender")
        _join_process(other, timeout=25.0, label="other")
        assert contender.exitcode == 0, f"contender exitcode={contender.exitcode}"
        assert other.exitcode == 0, f"other exitcode={other.exitcode}"
        assert holder.is_alive(), "Holder liberou/morreu antes do fim do contender"

        contender_payload = _read_json_result(Path(contender_result))
        other_payload = _read_json_result(Path(other_result))
        assert contender_payload.get("status") == "timeout", contender_payload
        assert contender_payload.get("error_code") == ERROR_LOCK_TIMEOUT, contender_payload
        assert not Path(contender_inside).is_file()
        assert other_payload.get("status") == "acquired", other_payload

        # Mesmo path físico para same comparison_id (diagnóstico).
        holder_acquired_note = Path(acquired_flag).read_text(encoding="utf-8")
        contender_started_note = Path(contender_started).read_text(encoding="utf-8")
        assert "path=" in holder_acquired_note
        assert "path=" in contender_started_note
        holder_path = holder_acquired_note.split("path=", 1)[1].split("|", 1)[0]
        contender_path = contender_started_note.split("path=", 1)[1].split("|", 1)[0]
        assert holder_path == contender_path == contender_payload.get("lock_path")

        _touch_flag(release_allowed, note="release")
        _join_process(holder, timeout=25.0, label="holder")
        assert holder.exitcode == 0, (
            f"holder exitcode={holder.exitcode} "
            f"payload={_read_json_result(Path(holder_result)) if Path(holder_result).is_file() else None}"
        )
        holder_payload = _read_json_result(Path(holder_result))
        assert holder_payload.get("status") == "acquired_and_released", holder_payload
        assert Path(released_flag).is_file()

        post = ctx.Process(
            target=_multiprocess_post_release,
            args=(shared, cmp_same, released_flag, post_result),
            name="lock-post-release",
        )
        processes.append(post)
        post.start()
        _join_process(post, timeout=25.0, label="post-release")
        assert post.exitcode == 0, f"post exitcode={post.exitcode}"
        post_payload = _read_json_result(Path(post_result))
        assert post_payload.get("status") == "acquired", post_payload
    finally:
        for proc in processes:
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=5.0)
        assert all(not proc.is_alive() for proc in processes)
