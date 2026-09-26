"""Testes direcionados do Lote 3A SCRUM-148: task_* Cleide Auditoria + Agente Compara."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.agente_compara_calculation_execution_service import (
    BILLING_STATUS_APPLIED,
    ERROR_CALCULATION_FAILED,
    _build_ready_response,
    _persist_failure,
    _try_record_agente_compara_growth_task,
)
from app.agente_compara_comparison_state import (
    STEP_CALCULATION_FAILED,
    STEP_CALCULATION_READY,
    STEP_CALCULATION_RUNNING,
    STEP_CONFIGURATION_READY,
    STEP_PREPARE_TABLE_1,
    advance_to_configuration_ready,
    start_comparison_for_session,
)
from app.cleide_audit_doc_service import (
    AUDIT_BATCH_STATUS_PROCESSED,
    TEMP_TABLE_STATUS_AWAITING_VALIDATION,
    TEMP_TABLE_STATUS_FAILED,
    TEMP_TABLE_STATUS_PROCESSING,
    apply_temp_table_extraction_from_model_payload,
    mark_temp_table_processing,
    run_audit_batch_for_session,
)
from app.funnel_event_service import (
    FUNNEL_EVENT_TASK_COMPLETED,
    FUNNEL_EVENT_TASK_FAILED,
    FUNNEL_EVENT_TASK_PREPARATION_COMPLETED,
    FUNNEL_EVENT_TASK_PREPARATION_STARTED,
    FUNNEL_EVENT_TASK_STARTED,
    FUNNEL_SOURCE_AGENTE_COMPARA,
    FUNNEL_SOURCE_CLEIDE_AUDIT,
    META_PIXEL_ALLOWED_EVENTS,
    TASK_TYPE_AGENTE_COMPARA,
    TASK_TYPE_CLEIDE_AUDIT,
    is_meta_pixel_allowed,
    try_record_growth_task_event,
)
from app.models import FunnelEvent
from tests.conftest import seed_conta_franquia_cliente, seed_usuario


def _auth_user(monkeypatch, *, email: str):
    conta, franquia = seed_conta_franquia_cliente(slug=f"lote3a-{email.split('@')[0]}")
    user = seed_usuario(franquia.id, conta.id, email=email)
    fake = SimpleNamespace(
        is_authenticated=True,
        id=user.id,
        conta_id=conta.id,
        franquia_id=franquia.id,
    )
    monkeypatch.setattr("flask_login.utils._get_user", lambda: fake)
    monkeypatch.setattr(
        "app.funnel_event_service._is_desktop_access_admin_test_mode",
        lambda: False,
    )
    return user


def _enable_session(app):
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True


def _events_for(user_id: int, event_name: str):
    return (
        FunnelEvent.query.filter_by(user_id=user_id, event_name=event_name)
        .order_by(FunnelEvent.id.asc())
        .all()
    )


def test_task_events_not_meta_authorized():
    for name in (
        FUNNEL_EVENT_TASK_PREPARATION_STARTED,
        FUNNEL_EVENT_TASK_PREPARATION_COMPLETED,
        FUNNEL_EVENT_TASK_STARTED,
        FUNNEL_EVENT_TASK_COMPLETED,
        FUNNEL_EVENT_TASK_FAILED,
    ):
        assert name not in META_PIXEL_ALLOWED_EVENTS
        assert is_meta_pixel_allowed(name) is False


def test_cleide_preparation_completed_and_failed(app, ctx, monkeypatch, tmp_path):
    from tests.cleiton_doc_fixtures import patch_cleiton_doc_cfg, patch_cleiton_doc_store

    _enable_session(app)
    patch_cleiton_doc_store(tmp_path, monkeypatch)
    cfg = patch_cleiton_doc_cfg(monkeypatch)
    monkeypatch.setattr("app.cleide_audit_doc_service.get_cleiton_doc_config", lambda: cfg)

    with app.app_context():
        user = _auth_user(monkeypatch, email="lote3a-cleide-prep@test.com")
        with app.test_request_context():
            from flask import session

            session["cleide_audit_doc_ids"] = ["doc-prep-1"]
            started = mark_temp_table_processing(["doc-prep-1"], user_scope=user.id, franquia_scope=user.franquia_id)
            assert started["status"] == TEMP_TABLE_STATUS_PROCESSING

            ready = apply_temp_table_extraction_from_model_payload(
                {"status": "awaiting_validation", "extracted_items": [{"x": 1}]},
                source_doc_ids=["doc-prep-1"],
            )
            assert ready["status"] == TEMP_TABLE_STATUS_AWAITING_VALIDATION

            # Retry do mesmo marco nao duplica.
            apply_temp_table_extraction_from_model_payload(
                {"status": "awaiting_validation", "extracted_items": [{"x": 1}]},
                source_doc_ids=["doc-prep-1"],
                force_overwrite=True,
            )

        started_events = _events_for(user.id, FUNNEL_EVENT_TASK_PREPARATION_STARTED)
        completed_events = _events_for(user.id, FUNNEL_EVENT_TASK_PREPARATION_COMPLETED)
        assert len(started_events) == 1
        assert len(completed_events) == 1
        assert started_events[0].source == FUNNEL_SOURCE_CLEIDE_AUDIT
        assert started_events[0].metadata_json["task_type"] == TASK_TYPE_CLEIDE_AUDIT
        assert started_events[0].metadata_json["task_stage"] == TEMP_TABLE_STATUS_PROCESSING
        assert completed_events[0].metadata_json["task_stage"] == TEMP_TABLE_STATUS_AWAITING_VALIDATION
        assert is_meta_pixel_allowed(started_events[0].event_name) is False


def test_cleide_preparation_failed_emits_task_failed(app, ctx, monkeypatch, tmp_path):
    from tests.cleiton_doc_fixtures import patch_cleiton_doc_cfg, patch_cleiton_doc_store

    _enable_session(app)
    patch_cleiton_doc_store(tmp_path, monkeypatch)
    cfg = patch_cleiton_doc_cfg(monkeypatch)
    monkeypatch.setattr("app.cleide_audit_doc_service.get_cleiton_doc_config", lambda: cfg)

    with app.app_context():
        user = _auth_user(monkeypatch, email="lote3a-cleide-fail-prep@test.com")
        with app.test_request_context():
            from flask import session

            session["cleide_audit_doc_ids"] = ["doc-fail-1"]
            mark_temp_table_processing(["doc-fail-1"], user_scope=user.id, franquia_scope=user.franquia_id)
            failed = apply_temp_table_extraction_from_model_payload(
                {"status": "failed", "extracted_items": []},
                source_doc_ids=["doc-fail-1"],
            )
            assert failed["status"] == TEMP_TABLE_STATUS_FAILED

        failed_events = _events_for(user.id, FUNNEL_EVENT_TASK_FAILED)
        assert len(failed_events) == 1
        assert failed_events[0].metadata_json["task_type"] == TASK_TYPE_CLEIDE_AUDIT
        assert failed_events[0].metadata_json["error_code"] == "cleide_audit_temp_table_failed"
        assert is_meta_pixel_allowed(FUNNEL_EVENT_TASK_FAILED) is False


def test_cleide_run_started_completed_and_idempotent(app, ctx, monkeypatch, tmp_path):
    from tests.cleiton_doc_fixtures import patch_cleiton_doc_cfg, patch_cleiton_doc_store

    _enable_session(app)
    patch_cleiton_doc_store(tmp_path, monkeypatch)
    cfg = patch_cleiton_doc_cfg(monkeypatch)
    monkeypatch.setattr("app.cleide_audit_doc_service.get_cleiton_doc_config", lambda: cfg)

    with app.app_context():
        user = _auth_user(monkeypatch, email="lote3a-cleide-run@test.com")
        with app.test_request_context():
            from flask import session

            session["cleide_audit_doc_ids"] = ["doc-run-1"]
            record = mark_temp_table_processing(
                ["doc-run-1"], user_scope=user.id, franquia_scope=user.franquia_id
            )
            temp_id = record["temp_table_id"]
            ready = apply_temp_table_extraction_from_model_payload(
                {
                    "status": "awaiting_validation",
                    "freight_tables": [{"carrier": "X", "rows": []}],
                    "extracted_items": [{"ok": True}],
                },
                source_doc_ids=["doc-run-1"],
            )
            # Monta lote minimo via patch do loader/compute.
            audit_batch = {
                "status": "uploaded",
                "audit_batch_id": "batch-lote3a-1",
                "temp_table_id": temp_id,
                "normalized_rows": [{"row_index": 1}],
                "expires_at": ready.get("expires_at"),
            }

            monkeypatch.setattr(
                "app.cleide_audit_doc_service.get_temp_table_id",
                lambda _s=None: temp_id,
            )
            monkeypatch.setattr(
                "app.cleide_audit_doc_service.sync_temp_table_with_session_documents",
                lambda: None,
            )
            monkeypatch.setattr(
                "app.cleide_audit_doc_service.load_temp_table_record",
                lambda *_a, **_k: {
                    **ready,
                    "status": TEMP_TABLE_STATUS_AWAITING_VALIDATION,
                    "audit_batch": audit_batch,
                    "user_scope": user.id,
                    "franquia_scope": user.franquia_id,
                },
            )
            monkeypatch.setattr(
                "app.cleide_audit_doc_service._assert_temp_table_scope",
                lambda *_a, **_k: None,
            )
            monkeypatch.setattr(
                "app.cleide_audit_doc_service._audit_batch_should_bill_operational_run",
                lambda *_a, **_k: False,
            )
            monkeypatch.setattr(
                "app.cleide_audit_doc_service._resolve_cleide_audit_execution_id",
                lambda: "exec-lote3a-1",
            )
            monkeypatch.setattr(
                "app.cleide_audit_doc_service.compute_audit_outputs",
                lambda *_a, **_k: {
                    "results": [{"row_index": 1, "status": "ok"}],
                    "generated_at": "2026-09-24T12:00:00",
                    "summary": {"processed_rows": 1},
                    "audit_diagnostics": {"has_errors": False, "total_errors": 0, "groups": []},
                    "fiscal_snapshot": {},
                },
            )
            monkeypatch.setattr(
                "app.cleide_audit_doc_service._pricing_rule_fingerprint",
                lambda *_a, **_k: "fp",
            )
            monkeypatch.setattr(
                "app.cleide_audit_doc_service._apply_tax_fiscal_snapshot_to_audit_batch",
                lambda batch, *_a, **_k: batch,
            )
            monkeypatch.setattr(
                "app.cleide_audit_doc_service.save_temp_table_record",
                lambda updated: updated,
            )
            monkeypatch.setattr(
                "app.cleide_audit_doc_service._public_temp_table",
                lambda saved: {
                    "temp_table_id": temp_id,
                    "audit_batch": {
                        **saved["audit_batch"],
                        "status": AUDIT_BATCH_STATUS_PROCESSED,
                        "processed_at": saved["audit_batch"]["processed_at"],
                    },
                },
            )

            first = run_audit_batch_for_session(user_scope=user.id, franquia_scope=user.franquia_id)
            second = run_audit_batch_for_session(user_scope=user.id, franquia_scope=user.franquia_id)
            assert first["audit_batch"]["status"] == AUDIT_BATCH_STATUS_PROCESSED
            assert second["audit_batch"]["status"] == AUDIT_BATCH_STATUS_PROCESSED

        started = _events_for(user.id, FUNNEL_EVENT_TASK_STARTED)
        completed = _events_for(user.id, FUNNEL_EVENT_TASK_COMPLETED)
        assert len(started) == 1
        assert len(completed) == 1
        assert started[0].metadata_json["task_type"] == TASK_TYPE_CLEIDE_AUDIT
        assert completed[0].metadata_json["task_type"] == TASK_TYPE_CLEIDE_AUDIT
        assert started[0].execution_id == "exec-lote3a-1"
        assert completed[0].audit_batch_id == "batch-lote3a-1"


def test_cleide_run_failure_emits_task_failed(app, ctx, monkeypatch, tmp_path):
    from tests.cleiton_doc_fixtures import patch_cleiton_doc_cfg, patch_cleiton_doc_store

    _enable_session(app)
    patch_cleiton_doc_store(tmp_path, monkeypatch)
    cfg = patch_cleiton_doc_cfg(monkeypatch)
    monkeypatch.setattr("app.cleide_audit_doc_service.get_cleiton_doc_config", lambda: cfg)

    with app.app_context():
        user = _auth_user(monkeypatch, email="lote3a-cleide-run-fail@test.com")
        with app.test_request_context():
            audit_batch = {
                "status": "uploaded",
                "audit_batch_id": "batch-fail-1",
                "normalized_rows": [{"row_index": 1}],
            }
            monkeypatch.setattr(
                "app.cleide_audit_doc_service.get_temp_table_id",
                lambda _s=None: "tt-fail",
            )
            monkeypatch.setattr(
                "app.cleide_audit_doc_service.sync_temp_table_with_session_documents",
                lambda: None,
            )
            monkeypatch.setattr(
                "app.cleide_audit_doc_service.load_temp_table_record",
                lambda *_a, **_k: {
                    "temp_table_id": "tt-fail",
                    "status": TEMP_TABLE_STATUS_AWAITING_VALIDATION,
                    "audit_batch": audit_batch,
                    "user_scope": user.id,
                    "franquia_scope": user.franquia_id,
                },
            )
            monkeypatch.setattr(
                "app.cleide_audit_doc_service._assert_temp_table_scope",
                lambda *_a, **_k: None,
            )
            monkeypatch.setattr(
                "app.cleide_audit_doc_service._audit_batch_should_bill_operational_run",
                lambda *_a, **_k: False,
            )
            monkeypatch.setattr(
                "app.cleide_audit_doc_service._resolve_cleide_audit_execution_id",
                lambda: "exec-fail-1",
            )
            monkeypatch.setattr(
                "app.cleide_audit_doc_service.compute_audit_outputs",
                lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
            )

            with pytest.raises(RuntimeError):
                run_audit_batch_for_session(user_scope=user.id, franquia_scope=user.franquia_id)

        failed = _events_for(user.id, FUNNEL_EVENT_TASK_FAILED)
        started = _events_for(user.id, FUNNEL_EVENT_TASK_STARTED)
        assert len(started) == 1
        assert len(failed) == 1
        assert failed[0].metadata_json["error_code"] == "cleide_audit_run_failed"


def test_cleide_growth_persist_failure_is_fail_open(app, ctx, monkeypatch):
    with app.app_context():
        user = _auth_user(monkeypatch, email="lote3a-cleide-failopen@test.com")
        monkeypatch.setattr(
            "app.funnel_event_service.record_funnel_event",
            lambda **_k: (_ for _ in ()).throw(RuntimeError("analytics down")),
        )
        result = try_record_growth_task_event(
            event_name=FUNNEL_EVENT_TASK_STARTED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            task_type=TASK_TYPE_CLEIDE_AUDIT,
            idempotency_key="growth:cleide_audit:fail-open:task_started",
            task_stage="uploaded",
            user=user,
        )
        assert result is None
        assert FunnelEvent.query.filter_by(idempotency_key="growth:cleide_audit:fail-open:task_started").count() == 0


def test_agente_compara_preparation_and_calc_lifecycle(app, ctx, monkeypatch):
    _enable_session(app)
    with app.app_context():
        user = _auth_user(monkeypatch, email="lote3a-ac-lifecycle@test.com")
        with app.test_request_context():
            started = start_comparison_for_session()
            assert started["comparison_started"] is True
            cmp_id = started["state"]["comparison_id"]

            # Replay de start nao recria evento.
            replay = start_comparison_for_session()
            assert replay["idempotent_replay"] is True

            state = dict(started["state"])
            state["current_step"] = "CALCULATION_FILE"
            ready = advance_to_configuration_ready(state)
            assert ready["current_step"] == STEP_CONFIGURATION_READY
            advance_to_configuration_ready(dict(ready) | {"current_step": "CALCULATION_FILE"})

            _try_record_agente_compara_growth_task(
                event_name=FUNNEL_EVENT_TASK_STARTED,
                comparison_id=cmp_id,
                execution_id="exec-ac-1",
                task_stage=STEP_CALCULATION_RUNNING,
            )
            _try_record_agente_compara_growth_task(
                event_name=FUNNEL_EVENT_TASK_STARTED,
                comparison_id=cmp_id,
                execution_id="exec-ac-1",
                task_stage=STEP_CALCULATION_RUNNING,
            )

            calc = {
                "status": STEP_CALCULATION_READY,
                "execution_id": "exec-ac-1",
                "billing_status": BILLING_STATUS_APPLIED,
                "stale": False,
                "fingerprint_short": "abc",
            }
            monkeypatch.setattr(
                "app.agente_compara_calculation_execution_service._record_calculation_funnel_event",
                lambda **_k: ({"is_first_audit": False}, False),
            )
            monkeypatch.setattr(
                "app.agente_compara_calculation_execution_service._billing_allows_result_release",
                lambda _c: True,
            )
            monkeypatch.setattr(
                "app.agente_compara_calculation_execution_service.public_comparison_calculation_summary",
                lambda *_a, **_k: {},
            )
            monkeypatch.setattr(
                "app.agente_compara_calculation_execution_service._build_analytics_for_released_result",
                lambda *_a, **_k: None,
            )
            _build_ready_response(
                state={"comparison_id": cmp_id, "current_step": STEP_CALCULATION_READY, "status": "calculation_ready"},
                calc=calc,
                idempotent_replay=False,
                result={"summary": {}},
            )
            _build_ready_response(
                state={"comparison_id": cmp_id, "current_step": STEP_CALCULATION_READY, "status": "calculation_ready"},
                calc=calc,
                idempotent_replay=True,
                result={"summary": {}},
            )

        prep_started = _events_for(user.id, FUNNEL_EVENT_TASK_PREPARATION_STARTED)
        prep_completed = _events_for(user.id, FUNNEL_EVENT_TASK_PREPARATION_COMPLETED)
        task_started = _events_for(user.id, FUNNEL_EVENT_TASK_STARTED)
        task_completed = _events_for(user.id, FUNNEL_EVENT_TASK_COMPLETED)

        assert len(prep_started) == 1
        assert prep_started[0].metadata_json == {
            "task_type": TASK_TYPE_AGENTE_COMPARA,
            "task_stage": STEP_PREPARE_TABLE_1,
        }
        assert len(prep_completed) == 1
        assert prep_completed[0].metadata_json["task_stage"] == STEP_CONFIGURATION_READY
        assert len(task_started) == 1
        assert len(task_completed) == 1
        assert task_completed[0].source == FUNNEL_SOURCE_AGENTE_COMPARA
        assert is_meta_pixel_allowed(task_completed[0].event_name) is False


def test_agente_compara_failed_emits_task_failed(app, ctx, monkeypatch):
    _enable_session(app)
    with app.app_context():
        user = _auth_user(monkeypatch, email="lote3a-ac-failed@test.com")
        with app.test_request_context():
            monkeypatch.setattr(
                "app.agente_compara_calculation_execution_service.get_comparison_state",
                lambda *_a, **_k: {"comparison_id": "cmp-fail-1", "current_step": STEP_CALCULATION_RUNNING},
            )
            monkeypatch.setattr(
                "app.agente_compara_calculation_execution_service.persist_comparison_state",
                lambda state, session_obj=None: state,
            )
            monkeypatch.setattr(
                "app.agente_compara_calculation_execution_service._lightweight_calc_for_session",
                lambda calc: calc,
            )
            _persist_failure(
                session_obj=MagicMock(),
                state={"comparison_id": "cmp-fail-1"},
                running_calc={"execution_id": "exec-fail-ac", "comparison_id": "cmp-fail-1"},
                error_code=ERROR_CALCULATION_FAILED,
                message="falhou",
                started_perf=0.0,
                fingerprint_short="ffff",
            )
            _persist_failure(
                session_obj=MagicMock(),
                state={"comparison_id": "cmp-fail-1"},
                running_calc={"execution_id": "exec-fail-ac", "comparison_id": "cmp-fail-1"},
                error_code=ERROR_CALCULATION_FAILED,
                message="falhou",
                started_perf=0.0,
                fingerprint_short="ffff",
            )

        failed = _events_for(user.id, FUNNEL_EVENT_TASK_FAILED)
        assert len(failed) == 1
        assert failed[0].metadata_json == {
            "task_type": TASK_TYPE_AGENTE_COMPARA,
            "task_stage": STEP_CALCULATION_FAILED,
            "error_code": ERROR_CALCULATION_FAILED,
        }


def test_agente_compara_growth_persist_failure_is_fail_open(app, ctx, monkeypatch):
    with app.app_context():
        user = _auth_user(monkeypatch, email="lote3a-ac-failopen@test.com")
        monkeypatch.setattr(
            "app.funnel_event_service.record_funnel_event",
            lambda **_k: (_ for _ in ()).throw(RuntimeError("analytics down")),
        )
        # Nao deve propagar.
        _try_record_agente_compara_growth_task(
            event_name=FUNNEL_EVENT_TASK_STARTED,
            comparison_id="cmp-fo",
            execution_id="exec-fo",
            task_stage=STEP_CALCULATION_RUNNING,
        )
        assert user.id is not None
