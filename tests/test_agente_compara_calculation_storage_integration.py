"""Integração storage/lock/billing visibility (correção Etapa 5)."""
from __future__ import annotations

import copy
import json
import threading

import pytest

from app.agente_compara_calculation_execution_service import (
    BILLING_STATUS_APPLIED,
    BILLING_STATUS_FAILED,
    BILLING_STATUS_NOT_STARTED,
    ERROR_RESULT_CORRUPT_PUBLIC,
    ERROR_RESULT_MISSING,
    AgenteComparaCalculationExecutionError,
    execute_comparison_calculation,
    get_comparison_calculation_status,
)
from app.agente_compara_calculation_lock import acquire_comparison_calculation_lock
from app.agente_compara_calculation_result_storage import (
    delete_comparison_calculation_result,
    resolve_result_storage_path,
)
from app.agente_compara_comparison_state import (
    STEP_CALCULATION_READY,
    get_comparison_state,
    persist_comparison_state,
    public_comparison_calculation_summary,
)
from app.agente_compara_doc_service import reset_comparison_for_session
from tests.test_agente_compara_calculation_execution import (
    _assert_no_forbidden,
    _exec,
    _persisted,
    _ready_comparison_state,
    _session_dict_from_state,
    _setup_env,
)


def _big_result(n_rows: int, n_tables: int, comparison_id: str = "x") -> dict:
    tables = [
        {"table_id": f"t{i}", "slot_number": i + 1, "carrier_name": f"C{i}"}
        for i in range(n_tables)
    ]
    rows = []
    per_table = {}
    status_counts = {t["table_id"]: {"calculated": 0, "calculated_with_warnings": 0, "incomplete": 0, "error": 0} for t in tables}
    for r in range(n_rows):
        tr = {}
        for idx, t in enumerate(tables):
            mode = (r + idx) % 4
            base = {
                "table_id": t["table_id"],
                "carrier_name": t["carrier_name"],
                "slot_number": t["slot_number"],
                "calculated_freight": round(10.5 + idx + (r % 9) * 0.17, 2),
                "raw_status": "processed",
                "final_status": "calculated",
                "completeness_status": "complete",
                "is_partial_value": False,
                "calculation_memory": {
                    "status": "calculated",
                    "selected_rule": f"rule-{idx % 3}",
                    "normalized_region": f"REG-{r % 7}",
                    "weight_band": "ATE_30",
                    "trace": [f"step-{idx}", f"row-{r % 5}"]
                },
                "components": {"base": 10.0 + idx, "gris": 0.3, "tas": 0.2, "pedagio": 0.1},
                "evidence": {"region": f"SP-{r % 5}", "rule": f"weight-{idx}", "source": "fixture"},
                "warnings": [],
                "blocking_issues": [],
                "error": None,
            }
            if mode == 1:
                base["status"] = "calculated_with_warnings"
                base["final_status"] = "calculated_with_warnings"
                base["warnings"] = [f"warning-{idx}", f"duplicate-{r % 3}"]
                base["calculation_memory"]["status"] = "calculated_with_warnings"
                status_counts[t["table_id"]]["calculated_with_warnings"] += 1
            elif mode == 2:
                base["status"] = "incomplete"
                base["final_status"] = "incomplete"
                base["completeness_status"] = "partial"
                base["is_partial_value"] = True
                base["blocking_issues"] = [f"missing-coverage-{r % 4}"]
                base["calculation_memory"]["status"] = "incomplete"
                status_counts[t["table_id"]]["incomplete"] += 1
            elif mode == 3:
                base["status"] = "error"
                base["final_status"] = "error"
                base["calculated_freight"] = None
                base["error"] = {"code": "fixture_error", "message": "Falha controlada"}
                base["calculation_memory"]["status"] = "error"
                base["reason_code"] = "fixture_error"
                status_counts[t["table_id"]]["error"] += 1
            else:
                base["status"] = "calculated"
                status_counts[t["table_id"]]["calculated"] += 1
            tr[t["table_id"]] = base
        rows.append(
            {
                "row_index": r + 1,
                "document_number": f"D{r}",
                "destination_city": "Campinas",
                "destination_uf": "SP",
                "weight": 1.0 + (r % 12),
                "audited_weight": 1.0 + (r % 12),
                "invoice_value": 100.0 + (r % 40),
                "table_results": tr,
            }
        )
    for t in tables:
        counts = status_counts[t["table_id"]]
        per_table[t["table_id"]] = {
            "comparison_id": comparison_id,
            "table_id": t["table_id"],
            "slot_number": t["slot_number"],
            "carrier_name": t["carrier_name"],
            "row_count": n_rows,
            "calculated_count": counts["calculated"],
            "calculated_with_warnings_count": counts["calculated_with_warnings"],
            "incomplete_count": counts["incomplete"],
            "error_count": counts["error"],
            "summary": {"carrier_name": t["carrier_name"], "row_count": n_rows},
            "duration_ms": 100 + t["slot_number"],
            "schema_version": 1,
        }
    error_count = sum(c["error"] for c in status_counts.values())
    return {
        "schema_version": 1,
        "comparison_id": comparison_id,
        "table_count": n_tables,
        "tables": tables,
        "comparative_rows": rows,
        "results_by_table": per_table,
        "summary": {
            "calculated_cell_count": n_rows * n_tables - error_count,
            "error_cell_count": error_count,
            "total_calculation_cells": n_rows * n_tables,
        },
        "row_count": n_rows,
    }


def test_session_has_storage_pointer_not_result(app, tmp_path, monkeypatch):
    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state()
        sess = _session_dict_from_state(state)
        out = _exec(
            app,
            sess,
            comparison_id=state["comparison_id"],
            execution_id="stor-1",
            emit_billing=lambda **kwargs: None,
        )
        assert out["ok"] is True
        assert out["result"]
        calc = _persisted(sess)["comparison_calculation"]
        assert calc.get("result") is None or "result" not in calc
        assert calc.get("result_storage_key")
        assert calc.get("result_checksum")
        assert calc.get("result_size_bytes")
        assert "comparative_rows" not in calc
        assert "results_by_table" not in calc
        blob = json.dumps(calc, ensure_ascii=False, default=str)
        assert "comparative_rows" not in blob
        assert "results_by_table" not in blob
        path = resolve_result_storage_path(calc["result_storage_key"])
        assert path.is_file()
        summary = public_comparison_calculation_summary(calc, include_result=True)
        assert summary is not None
        assert "result" not in summary
        assert "result_storage_key" not in summary


@pytest.mark.parametrize("n_tables", [2, 3])
def test_large_payload_keeps_session_light(app, tmp_path, monkeypatch, n_tables):
    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state(three_tables=(n_tables == 3))
        sess = _session_dict_from_state(state)
        big = _big_result(2000, n_tables, comparison_id=state["comparison_id"])

        def motor(_context):
            return copy.deepcopy(big)

        out = _exec(
            app,
            sess,
            comparison_id=state["comparison_id"],
            execution_id=f"stor-large-{n_tables}",
            calculate_fn=motor,
            emit_billing=lambda **kwargs: None,
        )
        assert out["ok"] is True
        assert out["result"]["row_count"] == 2000
        calc = _persisted(sess)["comparison_calculation"]
        session_blob = json.dumps(calc, ensure_ascii=False, default=str)
        assert len(session_blob.encode("utf-8")) < 50_000
        assert "comparative_rows" not in session_blob
        assert "results_by_table" not in session_blob
        path = resolve_result_storage_path(calc["result_storage_key"])
        assert path.stat().st_size > 100_000


def test_replay_reads_storage_no_motor_no_rebill(app, tmp_path, monkeypatch):
    calls = {"motor": 0, "billing": 0}

    def motor(context):
        calls["motor"] += 1
        from app.agente_compara_comparison_calculation_service import calculate_comparison_in_memory

        return calculate_comparison_in_memory(context)

    def billing(**kwargs):
        calls["billing"] += 1

    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state()
        sess = _session_dict_from_state(state)
        first = _exec(
            app,
            sess,
            comparison_id=state["comparison_id"],
            execution_id="stor-replay-1",
            calculate_fn=motor,
            emit_billing=billing,
        )
        key = _persisted(sess)["comparison_calculation"]["result_storage_key"]
        second = _exec(
            app,
            sess,
            comparison_id=state["comparison_id"],
            execution_id="stor-replay-2",
            calculate_fn=motor,
            emit_billing=billing,
        )
        assert first["idempotent_replay"] is False
        assert second["idempotent_replay"] is True
        assert second["result"]
        assert calls["motor"] == 1
        assert calls["billing"] == 1
        assert _persisted(sess)["comparison_calculation"]["result_storage_key"] == key


def test_billing_failed_keeps_file_and_retry_releases(app, tmp_path, monkeypatch):
    calls = {"motor": 0, "billing": 0}

    def motor(context):
        calls["motor"] += 1
        from app.agente_compara_comparison_calculation_service import calculate_comparison_in_memory

        return calculate_comparison_in_memory(context)

    def billing(**kwargs):
        calls["billing"] += 1
        if calls["billing"] == 1:
            raise RuntimeError("down")

    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state()
        sess = _session_dict_from_state(state)
        first = _exec(
            app,
            sess,
            comparison_id=state["comparison_id"],
            execution_id="stor-bill",
            calculate_fn=motor,
            emit_billing=billing,
        )
        assert first["ok"] is False
        assert first["billing_status"] == BILLING_STATUS_FAILED
        assert first.get("result") is None
        calc = _persisted(sess)["comparison_calculation"]
        path = resolve_result_storage_path(calc["result_storage_key"])
        assert path.is_file()
        status = get_comparison_calculation_status(
            comparison_id=state["comparison_id"],
            session_obj=sess,
        )
        assert status["result"] is None
        assert status["billing_status"] == BILLING_STATUS_FAILED
        second = _exec(
            app,
            sess,
            comparison_id=state["comparison_id"],
            execution_id="stor-bill",
            calculate_fn=motor,
            emit_billing=billing,
        )
        assert second["ok"] is True
        assert second["billing_status"] == BILLING_STATUS_APPLIED
        assert second.get("result")
        assert calls["motor"] == 1
        assert calls["billing"] == 2


def test_storage_missing_blocks_replay_and_get(app, tmp_path, monkeypatch):
    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state()
        sess = _session_dict_from_state(state)
        _exec(
            app,
            sess,
            comparison_id=state["comparison_id"],
            execution_id="stor-miss",
            emit_billing=lambda **kwargs: None,
        )
        calc = _persisted(sess)["comparison_calculation"]
        delete_comparison_calculation_result(calc["result_storage_key"])
        billing = []
        with pytest.raises(AgenteComparaCalculationExecutionError) as exc:
            _exec(
                app,
                sess,
                comparison_id=state["comparison_id"],
                execution_id="stor-miss-2",
                emit_billing=lambda **kwargs: billing.append(1),
            )
        assert exc.value.error_code == ERROR_RESULT_MISSING
        assert billing == []
        status = get_comparison_calculation_status(
            comparison_id=state["comparison_id"],
            session_obj=sess,
        )
        assert status["ok"] is False
        assert status["error_code"] == ERROR_RESULT_MISSING
        assert status["result"] is None


def test_storage_corrupt_blocks_without_billing(app, tmp_path, monkeypatch):
    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state()
        sess = _session_dict_from_state(state)
        _exec(
            app,
            sess,
            comparison_id=state["comparison_id"],
            execution_id="stor-corr",
            emit_billing=lambda **kwargs: None,
        )
        calc = _persisted(sess)["comparison_calculation"]
        path = resolve_result_storage_path(calc["result_storage_key"])
        path.write_text("{broken", encoding="utf-8")
        billing = []
        with pytest.raises(AgenteComparaCalculationExecutionError) as exc:
            _exec(
                app,
                sess,
                comparison_id=state["comparison_id"],
                execution_id="stor-corr-2",
                emit_billing=lambda **kwargs: billing.append(1),
            )
        assert exc.value.error_code in {ERROR_RESULT_CORRUPT_PUBLIC, ERROR_RESULT_MISSING}
        assert billing == []


def test_new_ready_replaces_previous_storage(app, tmp_path, monkeypatch):
    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state()
        sess = _session_dict_from_state(state)
        _exec(
            app,
            sess,
            comparison_id=state["comparison_id"],
            execution_id="stor-old",
            emit_billing=lambda **kwargs: None,
        )
        old_key = _persisted(sess)["comparison_calculation"]["result_storage_key"]
        old_path = resolve_result_storage_path(old_key)
        assert old_path.is_file()

        # Force fingerprint change by altering operational rows.
        from app.agente_compara_doc_service import load_temp_table_record
        from app.services.cleiton_doc_config_service import get_cleiton_doc_config
        from tests.test_agente_compara_calculation_execution import _write_record

        ttl = get_cleiton_doc_config().upload_ttl_hours
        primary = load_temp_table_record(state["primary_temp_table_id"], ttl_hours=ttl)
        primary["audit_batch"]["normalized_rows"][0]["audited_weight"] = 99.0
        _write_record(primary)
        live = _persisted(sess)
        live["comparison_calculation"]["stale"] = True
        live["current_step"] = "CONFIGURATION_READY"
        persist_comparison_state(live, session_obj=sess)

        _exec(
            app,
            sess,
            comparison_id=state["comparison_id"],
            execution_id="stor-new",
            emit_billing=lambda **kwargs: None,
        )
        new_key = _persisted(sess)["comparison_calculation"]["result_storage_key"]
        assert new_key != old_key
        assert resolve_result_storage_path(new_key).is_file()
        assert not old_path.is_file()


def test_reset_removes_result_storage(app, tmp_path, monkeypatch):
    app.config["SECRET_KEY"] = "test-secret-reset-storage"
    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state()
        sess = _session_dict_from_state(state)
        _exec(
            app,
            sess,
            comparison_id=state["comparison_id"],
            execution_id="stor-reset",
            emit_billing=lambda **kwargs: None,
        )
        calc = _persisted(sess)["comparison_calculation"]
        key = calc["result_storage_key"]
        path = resolve_result_storage_path(key)
        assert path.is_file()

        from flask import session as flask_session

        # reset uses Flask session; inject state into request context session.
        with app.test_request_context():
            flask_session.clear()
            for k, v in sess.items():
                flask_session[k] = copy.deepcopy(v)
            reset_comparison_for_session(comparison_id=state["comparison_id"])
        assert not path.is_file()


def test_concurrent_request_blocked_by_lock(app, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.agente_compara_calculation_lock.DEFAULT_LOCK_TIMEOUT_SECONDS",
        0.4,
    )
    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state()
        sess = _session_dict_from_state(state)
        cmp_id = state["comparison_id"]
        errors = []

        def blocked():
            try:
                _exec(
                    app,
                    sess,
                    comparison_id=cmp_id,
                    execution_id="stor-blocked",
                    emit_billing=lambda **kwargs: None,
                )
            except AgenteComparaCalculationExecutionError as exc:
                errors.append(exc.error_code)
            except Exception as exc:  # pragma: no cover
                errors.append(type(exc).__name__)

        with acquire_comparison_calculation_lock(cmp_id, timeout_seconds=2.0):
            t = threading.Thread(target=blocked)
            t.start()
            t.join(timeout=5.0)
            assert not t.is_alive()
        assert errors
        assert errors[0] == "agente_compara_calculation_lock_timeout"


def test_stale_get_does_not_load_heavy_result(app, tmp_path, monkeypatch):
    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state()
        sess = _session_dict_from_state(state)
        big = _big_result(500, 2, comparison_id=state["comparison_id"])
        _exec(
            app,
            sess,
            comparison_id=state["comparison_id"],
            execution_id="stor-stale",
            calculate_fn=lambda _c: copy.deepcopy(big),
            emit_billing=lambda **kwargs: None,
        )
        from app.agente_compara_doc_service import load_temp_table_record
        from app.services.cleiton_doc_config_service import get_cleiton_doc_config
        from tests.test_agente_compara_calculation_execution import _write_record

        ttl = get_cleiton_doc_config().upload_ttl_hours
        primary = load_temp_table_record(state["primary_temp_table_id"], ttl_hours=ttl)
        primary["audit_batch"]["normalized_rows"][0]["invoice_value"] = 9999.0
        _write_record(primary)

        status = get_comparison_calculation_status(
            comparison_id=state["comparison_id"],
            session_obj=sess,
        )
        assert status["stale"] is True
        assert status["result"] is None
        assert status.get("previous_result_available") is True


def test_frontend_billing_visibility_contract():
    from pathlib import Path

    js = Path("app/static/js/agente_compara.js").read_text(encoding="utf-8")
    assert "Finalizando processamento" in js
    assert "Tentar novamente" in js
    assert "billing_status === 'applied'" in js or 'billing_status === "applied"' in js
    process_fn = js[
        js.index("function processComparisonCalculations") : js.index(
            "function clearCalculationFileSummary"
        )
    ]
    assert "result_storage_key" not in process_fn
    assert "cleiton_doc_tmp" not in process_fn
    assert "cc_result_" not in process_fn


def test_partial_cleanup_when_result_write_fails(app, tmp_path, monkeypatch):
    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state(three_tables=True)
        sess = _session_dict_from_state(state)
        import app.agente_compara_calculation_execution_service as exec_mod
        import app.agente_compara_calculation_result_storage as storage_mod

        real_save_memory = storage_mod.save_comparison_calculation_memory_payload

        def fail_result(**kwargs):
            raise storage_mod.AgenteComparaCalculationResultStorageError(
                storage_mod.ERROR_RESULT_WRITE_FAILED,
                'write failed',
                safe_message='Os c?lculos foram processados, mas o resultado comparativo n?o p?de ser salvo.',
                error_stage='result_replaced',
                artifact_type='result',
                retryable=True,
                metrics={'last_completed_stage': 'memory_reloaded', 'memory_size_bytes': 1234},
                operation='result.write_atomic',
            )

        monkeypatch.setattr(storage_mod, 'save_comparison_calculation_result', fail_result)
        monkeypatch.setattr(exec_mod, 'save_comparison_calculation_result', fail_result)
        out = _exec(
            app,
            sess,
            comparison_id=state['comparison_id'],
            execution_id='stor-partial-cleanup',
            calculate_fn=lambda _context: copy.deepcopy(_big_result(20, 3, comparison_id=state['comparison_id'])),
            emit_billing=lambda **kwargs: (_ for _ in ()).throw(AssertionError('billing')),
        )
        assert out['ok'] is False
        assert out['error_code'] == storage_mod.ERROR_RESULT_WRITE_FAILED
        assert out['artifact_type'] == 'result'
        calc = _persisted(sess)['comparison_calculation']
        assert calc['billing_status'] == BILLING_STATUS_NOT_STARTED
        assert calc['memory_storage_key'] is None
        assert calc['result_storage_key'] is None
        calc_dir = tmp_path / 'agente_compara_calc'
        assert not any(p.name.startswith('cc_memory_') for p in calc_dir.glob('*.json'))
