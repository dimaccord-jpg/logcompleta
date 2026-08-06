"""Etapa 5 — integração controlada do cálculo comparativo (estados, fingerprint, API, billing, FE)."""
from __future__ import annotations

import copy
import importlib
import os
import pathlib
import time
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import event

from app.agente_compara_calculation_execution_service import (
    AGENTE_COMPARA_CALCULATION_ALGORITHM_VERSION,
    BILLING_STATUS_APPLIED,
    BILLING_STATUS_FAILED,
    BILLING_STATUS_NOT_STARTED,
    ERROR_CALCULATION_INPUT_CHANGED,
    ERROR_EXECUTION_CONFLICT,
    ERROR_EXECUTION_IN_PROGRESS,
    FLOW_TYPE_COMPARISON_CALCULATION,
    compute_calculation_fingerprint,
    ERROR_CALCULATION_FAILED,
    execute_comparison_calculation,
    get_comparison_calculation_status,
    build_calculation_fingerprint_payload,
)
from app.agente_compara_comparison_state import (
    AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY,
    COMPARISON_STATUS_CALCULATION_READY,
    COMPARISON_STATUS_CALCULATION_RUNNING,
    COMPARISON_STATUS_CONFIGURATION_READY,
    STEP_CALCULATION_FAILED,
    STEP_CALCULATION_READY,
    STEP_CALCULATION_RUNNING,
    STEP_CONFIGURATION_READY,
    STEP_TAXES,
    TABLE_STATUS_CONFIRMED,
    create_comparison,
    get_comparison_state,
    get_table_by_slot,
    persist_comparison_state,
    set_comparison_tax_config,
)
from app.agente_compara_doc_service import (
    AGENTE_COMPARA_TEMP_TABLE_ID_SESSION_KEY,
    _write_temp_table_atomic,
    _temp_table_path,
)
from app.services.agente_compara_config_service import (
    AgenteComparaConfig,
    DEFAULT_CALCULATION_BASES,
    DEFAULT_FALLBACK_MESSAGE,
)
from app.extensions import db
from app.models import FunnelEvent, User
from tests.cleiton_doc_fixtures import patch_cleiton_doc_cfg, patch_cleiton_doc_store
from tests.conftest import seed_conta_franquia_cliente, seed_usuario

FORBIDDEN_PUBLIC_FIELDS = {
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


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


def _patch_ac_cfg(monkeypatch):
    cfg = AgenteComparaConfig(
        chat_enabled=True,
        upload_enabled=True,
        chat_max_history=10,
        document_context_max_chars=24000,
        max_documents_considered=3,
        question_max_chars=4000,
        fallback_message=DEFAULT_FALLBACK_MESSAGE,
        no_documents_behavior="allow_guided",
        show_documents_used=True,
        no_hallucination_instruction_enabled=True,
        audited_file_max_bytes=None,
        audited_file_max_rows=2000,
        calculation_bases=copy.deepcopy(DEFAULT_CALCULATION_BASES),
    )
    for target in (
        "app.agente_compara_api_routes.get_agente_compara_config",
        "app.agente_compara_doc_service.get_agente_compara_config",
        "app.agente_compara_doc_context.get_agente_compara_config",
    ):
        monkeypatch.setattr(target, lambda _cfg=cfg: _cfg)
    return cfg


def _setup_env(monkeypatch, tmp_path):
    patch_cleiton_doc_store(tmp_path, monkeypatch)
    cfg = patch_cleiton_doc_cfg(monkeypatch)
    monkeypatch.setattr("app.agente_compara_doc_service.get_cleiton_doc_config", lambda: cfg)
    monkeypatch.setattr("app.agente_compara_api_routes.get_cleiton_doc_config", lambda: cfg)
    monkeypatch.setattr(
        "app.agente_compara_calculation_execution_service.get_cleiton_doc_config",
        lambda: cfg,
        raising=False,
    )
    monkeypatch.setattr("app.services.cleiton_doc_config_service.get_cleiton_doc_config", lambda: cfg)
    _patch_ac_cfg(monkeypatch)
    monkeypatch.setattr(
        "app.agente_compara_doc_service.get_agente_compara_config",
        lambda: SimpleNamespace(calculation_bases=copy.deepcopy(DEFAULT_CALCULATION_BASES), upload_ttl_hours=24),
    )
    return cfg


def _authorized_funnel_user(monkeypatch, web, *, user_id: int = 101, conta_id: int = 201, franquia_id: int = 301):
    fake_user = SimpleNamespace(
        is_authenticated=True,
        conta_id=conta_id,
        franquia_id=franquia_id,
        id=user_id,
    )
    monkeypatch.setattr(web, "current_user", fake_user)
    monkeypatch.setattr("app.agente_compara_api_routes.current_user", fake_user)
    monkeypatch.setattr(
        "app.agente_compara_calculation_execution_service.current_user",
        fake_user,
    )
    monkeypatch.setattr(
        web,
        "avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True, "modo_operacao": "normal"},
    )
    monkeypatch.setattr(
        "app.agente_compara_api_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True, "modo_operacao": "normal"},
    )
    return fake_user


def _authorized(monkeypatch, web):
    fake_user = SimpleNamespace(is_authenticated=True, conta_id=1, franquia_id=1, id=1)
    monkeypatch.setattr(web, "current_user", fake_user)
    monkeypatch.setattr("app.agente_compara_api_routes.current_user", fake_user)
    monkeypatch.setattr(
        web,
        "avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True, "modo_operacao": "normal"},
    )
    monkeypatch.setattr(
        "app.agente_compara_api_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True, "modo_operacao": "normal"},
    )


@pytest.fixture
def web_client(app, tmp_path, monkeypatch, ctx):
    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
    web = _load_web_module()
    _authorized(monkeypatch, web)
    web.app.config["TESTING"] = True
    web.app.config["SECRET_KEY"] = "test-secret"
    return web.app.test_client()


def _pricing_record(*, temp_table_id: str, region: str = "SP-Interior 1") -> dict:
    return {
        "temp_table_id": temp_table_id,
        "status": "needs_review",
        "edit_version": 1,
        "updated_at": "2026-01-01T00:00:00+00:00",
        "created_at": "2026-01-01T00:00:00+00:00",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "freight_tables": [
            {
                "table_title": "Tabela por região",
                "table_type": "weight_range_table",
                "columns": ["Região de frete", "Até 30 kg", "31 a 50 kg", "Excedente kg"],
                "rows": [
                    {
                        "Região de frete": region,
                        "Até 30 kg": "87,13",
                        "31 a 50 kg": "100,50",
                        "Excedente kg": "2,00",
                    }
                ],
            }
        ],
        "freight_routes": [],
        "accessorial_fees": [],
        "coverage_table": {
            "status": "needs_review",
            "columns": ["UF destino", "Cidade destino", "Região de frete"],
            "rows": [
                {
                    "destination_uf": "SP",
                    "destination_city": "Campinas",
                    "freight_region": region,
                },
                {
                    "destination_uf": "RJ",
                    "destination_city": "Niterói",
                    "freight_region": region,
                },
            ],
        },
        "audit_batch": None,
        "source_documents": [f"doc-{temp_table_id}"],
        "active_document_id": f"doc-{temp_table_id}",
    }


def _row(row_index: int, document_number: str, city: str = "Campinas", uf: str = "SP", weight: float = 48.0):
    return {
        "row_index": row_index,
        "document_number": document_number,
        "destination_city": city,
        "destination_uf": uf,
        "audited_weight": weight,
        "invoice_value": 1000.0,
    }


def _write_record(record: dict) -> None:
    path = _temp_table_path(record["temp_table_id"])
    _write_temp_table_atomic(path, record)


def _ready_comparison_state(*, same_carrier: bool = False, three_tables: bool = False) -> dict:
    sess: dict = {}
    state = create_comparison(session_obj=sess)
    t1 = get_table_by_slot(state, 1)
    t2 = get_table_by_slot(state, 2)
    assert t1 and t2
    tt1 = "tt_calc_1_" + uuid4().hex[:8]
    tt2 = "tt_calc_2_" + uuid4().hex[:8]
    t1.update(
        {
            "temp_table_id": tt1,
            "confirmed": True,
            "status": TABLE_STATUS_CONFIRMED,
            "carrier_name": "Transportadora Alfa",
        }
    )
    t2.update(
        {
            "temp_table_id": tt2,
            "confirmed": True,
            "status": TABLE_STATUS_CONFIRMED,
            "carrier_name": "Transportadora Alfa" if same_carrier else "Transportadora Beta",
        }
    )
    records = {
        tt1: _pricing_record(temp_table_id=tt1),
        tt2: _pricing_record(temp_table_id=tt2),
    }
    if three_tables:
        tt3 = "tt_calc_3_" + uuid4().hex[:8]
        entry = {
            "table_id": uuid4().hex,
            "slot_number": 3,
            "status": TABLE_STATUS_CONFIRMED,
            "doc_ids": [],
            "temp_table_id": tt3,
            "carrier_name": "Transportadora Gama",
            "confirmed": True,
            "error": None,
        }
        state["tables"][entry["table_id"]] = entry
        state["desired_table_count"] = 3
        records[tt3] = _pricing_record(temp_table_id=tt3)

    rows = [
        _row(1, "DOC-1", "Campinas", "SP", 48.0),
        _row(2, "DOC-2", "Campinas", "SP", 20.0),
        _row(3, "DOC-DUP", "Niterói", "RJ", 48.0),
        _row(4, "DOC-DUP", "Campinas", "SP", 48.0),
    ]
    # Force domain error on slot2 for row 3 by removing RJ coverage from tt2 only.
    records[tt2]["coverage_table"]["rows"] = [
        {
            "destination_uf": "SP",
            "destination_city": "Campinas",
            "freight_region": "SP-Interior 1",
        }
    ]

    primary = tt1
    records[primary]["audit_batch"] = {
        "status": "uploaded",
        "audit_batch_id": "batch-" + uuid4().hex[:8],
        "temp_table_id": primary,
        "source_file_name": "operacional.xlsx",
        "sheet_name": "Planilha1",
        "row_count": len(rows),
        "input_schema_version": 1,
        "normalized_rows": rows,
    }
    for record in records.values():
        _write_record(record)

    state["primary_temp_table_id"] = primary
    state["current_step"] = STEP_CONFIGURATION_READY
    state["status"] = COMPARISON_STATUS_CONFIGURATION_READY
    set_comparison_tax_config(
        state,
        {
            "include_taxes": False,
            "confirmed": True,
            "selected_table_ids": [],
        },
    )
    persist_comparison_state(state, session_obj=sess)
    return state


def _session_dict_from_state(state: dict) -> dict:
    return {
        AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY: copy.deepcopy(state),
        AGENTE_COMPARA_TEMP_TABLE_ID_SESSION_KEY: state.get("primary_temp_table_id"),
    }


def _assert_no_forbidden(payload):
    text = str(payload)
    for field in FORBIDDEN_PUBLIC_FIELDS:
        assert f"'{field}'" not in text or (isinstance(payload, dict) and field not in str(payload.keys()))
    if isinstance(payload, dict):

        def walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    assert k not in FORBIDDEN_PUBLIC_FIELDS
                    walk(v)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(payload)


def _exec(app, sess, **kwargs):
    return execute_comparison_calculation(session_obj=sess, **kwargs)


def _persisted(sess):
    return get_comparison_state(sess)


# ---------------------------------------------------------------------------
# Checkpoint 1 — fingerprint / persistence / estados
# ---------------------------------------------------------------------------


def test_fingerprint_deterministic_and_ignores_carrier_name(app, tmp_path, monkeypatch):
    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state(same_carrier=True)
        from app.agente_compara_doc_service import load_temp_table_record
        from app.services.cleiton_doc_config_service import get_cleiton_doc_config

        ttl = get_cleiton_doc_config().upload_ttl_hours
        primary = load_temp_table_record(state["primary_temp_table_id"], ttl_hours=ttl)
        rows = primary["audit_batch"]["normalized_rows"]
        coverage = primary["coverage_table"]
        records = {}
        for entry in state["tables"].values():
            if entry.get("confirmed"):
                records[entry["temp_table_id"]] = load_temp_table_record(entry["temp_table_id"], ttl_hours=ttl)
        payload = build_calculation_fingerprint_payload(
            comparison_id=state["comparison_id"],
            state=state,
            normalized_rows=rows,
            table_records=records,
            tax_config=state.get("tax_config"),
            coverage_table=coverage,
            source_file_identity={
                "audit_batch_id": primary["audit_batch"]["audit_batch_id"],
                "source_file_name": "operacional.xlsx",
                "sheet_name": "Planilha1",
                "row_count": len(rows),
                "input_schema_version": 1,
                "temp_table_id": state["primary_temp_table_id"],
            },
        )
        fp1 = compute_calculation_fingerprint(payload)
        fp2 = compute_calculation_fingerprint(copy.deepcopy(payload))
        assert fp1 == fp2
        assert len(fp1) == 64
        # Mutating carrier_name must not change fingerprint payload (not included).
        state2 = copy.deepcopy(state)
        for entry in state2["tables"].values():
            entry["carrier_name"] = "Outro Nome"
        payload2 = build_calculation_fingerprint_payload(
            comparison_id=state2["comparison_id"],
            state=state2,
            normalized_rows=rows,
            table_records=records,
            tax_config=state2.get("tax_config"),
            coverage_table=coverage,
            source_file_identity=payload["source_file_identity"],
        )
        assert compute_calculation_fingerprint(payload2) == fp1


def test_configuration_ready_runs_and_persists_ready(app, tmp_path, monkeypatch):
    billing_calls = []

    def _emit(**kwargs):
        billing_calls.append(kwargs)

    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state()
        sess = _session_dict_from_state(state)
        result = _exec(
            app,
            sess,
            comparison_id=state["comparison_id"],
            execution_id="exec-ready-1",
            emit_billing=_emit,
        )
        assert result["ok"] is True
        assert result["status"] == STEP_CALCULATION_READY
        assert result["idempotent_replay"] is False
        assert result["result"]["table_count"] == 2
        _assert_no_forbidden(result["result"])
        persisted = _persisted(sess)
        assert persisted["current_step"] == STEP_CALCULATION_READY
        calc = persisted["comparison_calculation"]
        assert calc["status"] == STEP_CALCULATION_READY
        assert "result" not in calc or calc.get("result") is None
        assert calc.get("result_storage_key")
        assert calc.get("result_checksum")
        assert calc.get("result_size_bytes")
        assert "results_by_table" not in calc
        assert "comparative_rows" not in calc
        assert calc["billing_status"] == BILLING_STATUS_APPLIED
        assert len(billing_calls) == 1
        assert billing_calls[0]["flow_type"] == FLOW_TYPE_COMPARISON_CALCULATION


def test_earlier_step_rejects_calculation(app, tmp_path, monkeypatch):
    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state()
        state["current_step"] = STEP_TAXES
        sess = _session_dict_from_state(state)
        from app.agente_compara_calculation_execution_service import AgenteComparaCalculationExecutionError

        with pytest.raises(AgenteComparaCalculationExecutionError) as exc:
            _exec(
                app,
                sess,
                comparison_id=state["comparison_id"],
                execution_id="exec-early",
                emit_billing=lambda **kwargs: None,
            )
        assert exc.value.error_code.endswith("step_invalid") or "not_ready" in exc.value.error_code


def test_running_persisted_before_motor(app, tmp_path, monkeypatch):
    seen = {}

    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state()
        sess = _session_dict_from_state(state)

        def motor(context):
            persisted = _persisted(sess)
            seen["step"] = persisted["current_step"]
            seen["status"] = persisted["comparison_calculation"]["status"]
            from app.agente_compara_comparison_calculation_service import calculate_comparison_in_memory

            return calculate_comparison_in_memory(context)

        _exec(
            app,
            sess,
            comparison_id=state["comparison_id"],
            execution_id="exec-running-order",
            calculate_fn=motor,
            emit_billing=lambda **kwargs: None,
        )
        assert seen["step"] == STEP_CALCULATION_RUNNING
        assert seen["status"] == STEP_CALCULATION_RUNNING


def test_failed_has_no_partial_result(app, tmp_path, monkeypatch):
    def boom(_context):
        raise RuntimeError("boom")

    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state()
        sess = _session_dict_from_state(state)
        from app.agente_compara_calculation_execution_service import AgenteComparaCalculationExecutionError

        with pytest.raises(AgenteComparaCalculationExecutionError):
            _exec(
                app,
                sess,
                comparison_id=state["comparison_id"],
                execution_id="exec-fail-1",
                calculate_fn=boom,
                emit_billing=lambda **kwargs: (_ for _ in ()).throw(AssertionError("billing")),
            )
        persisted = _persisted(sess)
        assert persisted["current_step"] == STEP_CALCULATION_FAILED
        calc = persisted["comparison_calculation"]
        assert calc.get("result") is None
        assert not calc.get("result_storage_key")
        assert calc["billing_status"] == BILLING_STATUS_NOT_STARTED


# ---------------------------------------------------------------------------
# Checkpoint 2 — idempotência / concorrência
# ---------------------------------------------------------------------------


def test_idempotent_replay_same_execution_and_fingerprint(app, tmp_path, monkeypatch):
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
            execution_id="exec-idem-1",
            calculate_fn=motor,
            emit_billing=billing,
        )
        second = _exec(
            app,
            sess,
            comparison_id=state["comparison_id"],
            execution_id="exec-idem-1",
            calculate_fn=motor,
            emit_billing=billing,
        )
        assert first["idempotent_replay"] is False
        assert second["idempotent_replay"] is True
        assert calls["motor"] == 1
        assert calls["billing"] == 1


def test_different_execution_same_fingerprint_replays(app, tmp_path, monkeypatch):
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
        _exec(
            app,
            sess,
            comparison_id=state["comparison_id"],
            execution_id="exec-a",
            calculate_fn=motor,
            emit_billing=billing,
        )
        replay = _exec(
            app,
            sess,
            comparison_id=state["comparison_id"],
            execution_id="exec-b",
            calculate_fn=motor,
            emit_billing=billing,
        )
        assert replay["idempotent_replay"] is True
        assert calls["motor"] == 1
        assert calls["billing"] == 1


def test_same_execution_different_fingerprint_conflicts(app, tmp_path, monkeypatch):
    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state()
        sess = _session_dict_from_state(state)
        _exec(
            app,
            sess,
            comparison_id=state["comparison_id"],
            execution_id="exec-conflict",
            emit_billing=lambda **kwargs: None,
        )
        # Force fingerprint change
        persisted = _persisted(sess)
        persisted["tax_config"] = {
            "include_taxes": True,
            "confirmed": True,
            "origin_uf": "SP",
            "origin_city": "São Paulo",
            "selected_table_ids": [get_table_by_slot(persisted, 1)["table_id"]],
            "destination_ufs": [],
            "icms_rates": [],
        }
        persisted["current_step"] = STEP_CONFIGURATION_READY
        persisted["comparison_calculation"]["stale"] = True
        persist_comparison_state(persisted, session_obj=sess)
        from app.agente_compara_calculation_execution_service import AgenteComparaCalculationExecutionError

        with pytest.raises(AgenteComparaCalculationExecutionError) as exc:
            _exec(
                app,
                sess,
                comparison_id=state["comparison_id"],
                execution_id="exec-conflict",
                emit_billing=lambda **kwargs: None,
            )
        assert exc.value.error_code == ERROR_EXECUTION_CONFLICT


def test_concurrent_second_request_does_not_run_motor(app, tmp_path, monkeypatch):
    calls = {"motor": 0}

    def motor(context):
        calls["motor"] += 1
        from app.agente_compara_comparison_calculation_service import calculate_comparison_in_memory

        return calculate_comparison_in_memory(context)

    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state()
        sess = _session_dict_from_state(state)

        def after_running(_state):
            from app.agente_compara_calculation_execution_service import AgenteComparaCalculationExecutionError

            with pytest.raises(AgenteComparaCalculationExecutionError) as exc:
                _exec(
                    app,
                    sess,
                    comparison_id=state["comparison_id"],
                    execution_id="exec-concurrent-2",
                    calculate_fn=motor,
                    emit_billing=lambda **kwargs: None,
                )
            # Nested call while file/process lock is held → lock timeout (preferred)
            # or RUNNING conflict if lock were reentrant in the same process.
            assert exc.value.error_code in {
                ERROR_EXECUTION_IN_PROGRESS,
                "agente_compara_calculation_lock_timeout",
            }

        _exec(
            app,
            sess,
            comparison_id=state["comparison_id"],
            execution_id="exec-concurrent-1",
            calculate_fn=motor,
            emit_billing=lambda **kwargs: None,
            after_running_hook=after_running,
        )
        assert calls["motor"] == 1


def test_input_changed_during_running_fails(app, tmp_path, monkeypatch):
    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state()
        sess = _session_dict_from_state(state)

        def after_running(_state):
            live = _persisted(sess)
            live["tax_config"] = {
                "include_taxes": True,
                "confirmed": True,
                "origin_uf": "RJ",
                "origin_city": "Rio",
                "selected_table_ids": [get_table_by_slot(live, 1)["table_id"]],
                "destination_ufs": [],
                "icms_rates": [],
            }
            persist_comparison_state(live, session_obj=sess)

        result = _exec(
            app,
            sess,
            comparison_id=state["comparison_id"],
            execution_id="exec-input-changed",
            emit_billing=lambda **kwargs: (_ for _ in ()).throw(AssertionError("no billing")),
            after_running_hook=after_running,
        )
        assert result["ok"] is False
        assert result["status"] == STEP_CALCULATION_FAILED
        assert result["error_code"] == ERROR_CALCULATION_INPUT_CHANGED
        persisted = _persisted(sess)
        assert persisted["comparison_calculation"].get("result") is None
        assert not persisted["comparison_calculation"].get("result_storage_key")
        assert persisted["comparison_calculation"]["billing_status"] == BILLING_STATUS_NOT_STARTED


# ---------------------------------------------------------------------------
# Checkpoint 3 — billing
# ---------------------------------------------------------------------------


def test_billing_once_for_two_and_three_tables(app, tmp_path, monkeypatch):
    for three in (False, True):
        calls = []
        with app.app_context():
            _setup_env(monkeypatch, tmp_path)
            state = _ready_comparison_state(three_tables=three)
            sess = _session_dict_from_state(state)
            out = _exec(
                app,
                sess,
                comparison_id=state["comparison_id"],
                execution_id=f"exec-bill-{'3' if three else '2'}",
                emit_billing=lambda **kwargs: calls.append(kwargs),
            )
            assert out["ok"] is True
            assert len(calls) == 1
            assert calls[0]["flow_type"] == FLOW_TYPE_COMPARISON_CALCULATION


def test_billing_failure_keeps_ready_and_retry_does_not_recalculate(app, tmp_path, monkeypatch):
    calls = {"motor": 0, "billing": 0}

    def motor(context):
        calls["motor"] += 1
        from app.agente_compara_comparison_calculation_service import calculate_comparison_in_memory

        return calculate_comparison_in_memory(context)

    def billing(**kwargs):
        calls["billing"] += 1
        if calls["billing"] == 1:
            raise RuntimeError("billing down")

    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state()
        sess = _session_dict_from_state(state)
        first = _exec(
            app,
            sess,
            comparison_id=state["comparison_id"],
            execution_id="exec-bill-fail",
            calculate_fn=motor,
            emit_billing=billing,
        )
        assert first["ok"] is False
        assert first["billing_status"] == BILLING_STATUS_FAILED
        assert first.get("result") is None
        persisted = _persisted(sess)
        assert persisted["current_step"] == STEP_CALCULATION_READY
        assert "result" not in persisted["comparison_calculation"] or persisted["comparison_calculation"].get("result") is None
        assert persisted["comparison_calculation"].get("result_storage_key")
        second = _exec(
            app,
            sess,
            comparison_id=state["comparison_id"],
            execution_id="exec-bill-fail",
            calculate_fn=motor,
            emit_billing=billing,
        )
        assert second["idempotent_replay"] is True
        assert second["ok"] is True
        assert second.get("result")
        assert calls["motor"] == 1
        assert calls["billing"] == 2
        assert _persisted(sess)["comparison_calculation"]["billing_status"] == BILLING_STATUS_APPLIED


def test_get_does_not_bill(app, tmp_path, monkeypatch):
    billing = MagicMock()
    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state()
        sess = _session_dict_from_state(state)
        _exec(
            app,
            sess,
            comparison_id=state["comparison_id"],
            execution_id="exec-get-no-bill",
            emit_billing=lambda **kwargs: None,
        )
        monkeypatch.setattr(
            "app.agente_compara_calculation_execution_service._apply_billing",
            billing,
        )
        status = get_comparison_calculation_status(
            comparison_id=state["comparison_id"],
            session_obj=sess,
        )
        assert status["status"] == STEP_CALCULATION_READY
        billing.assert_not_called()


# ---------------------------------------------------------------------------
# Checkpoint 4 — API
# ---------------------------------------------------------------------------


def test_api_calculate_auth_required(web_client, monkeypatch):
    monkeypatch.setattr(
        "app.agente_compara_api_routes.current_user",
        SimpleNamespace(is_authenticated=False),
    )
    resp = web_client.post("/api/agente-compara/comparison/calculate", json={})
    assert resp.status_code == 401


def test_api_calculate_success_and_get_restore(web_client, app, tmp_path, monkeypatch):
    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state()
    with web_client.session_transaction() as sess:
        sess[AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = state
        sess[AGENTE_COMPARA_TEMP_TABLE_ID_SESSION_KEY] = state["primary_temp_table_id"]

    billing_calls = []
    monkeypatch.setattr(
        "app.agente_compara_calculation_execution_service._emit_agente_compara_operational_billing",
        lambda **kwargs: billing_calls.append(kwargs),
        raising=False,
    )

    def _emit(emitted, started_at, flow_type, idempotency_key, rows_processed, status="success", error_summary=None, execution_id=None):
        billing_calls.append({"flow_type": flow_type, "idempotency_key": idempotency_key})
        emitted[0] = True

    monkeypatch.setattr(
        "app.agente_compara_doc_service._emit_agente_compara_operational_billing",
        _emit,
    )

    resp = web_client.post(
        "/api/agente-compara/comparison/calculate",
        json={
            "comparison_id": state["comparison_id"],
            "execution_id": "api-exec-1",
            "schema_version": 1,
        },
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["status"] == STEP_CALCULATION_READY
    assert body["result"]["comparative_rows"]
    _assert_no_forbidden(body["result"])

    get_resp = web_client.get(
        f"/api/agente-compara/comparison/calculation?comparison_id={state['comparison_id']}"
    )
    assert get_resp.status_code == 200
    get_body = get_resp.get_json()
    assert get_body["status"] == STEP_CALCULATION_READY
    assert get_body["result"]["row_count"] == body["result"]["row_count"]

    replay = web_client.post(
        "/api/agente-compara/comparison/calculate",
        json={
            "comparison_id": state["comparison_id"],
            "execution_id": "api-exec-1",
            "schema_version": 1,
        },
    )
    assert replay.get_json()["idempotent_replay"] is True
    assert len(billing_calls) == 1


def test_api_ownership_mismatch(web_client, app, tmp_path, monkeypatch):
    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state()
    with web_client.session_transaction() as sess:
        sess[AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = state
    resp = web_client.post(
        "/api/agente-compara/comparison/calculate",
        json={"comparison_id": "other-comparison", "execution_id": "x"},
    )
    assert resp.status_code == 409


def test_api_get_not_started(web_client, app, tmp_path, monkeypatch):
    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state()
    with web_client.session_transaction() as sess:
        sess[AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = state
    resp = web_client.get(
        f"/api/agente-compara/comparison/calculation?comparison_id={state['comparison_id']}"
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "not_started"
    assert body["result"] is None


def test_replay_reuses_stored_result_without_motor_or_additional_billing(app, tmp_path, monkeypatch):
    """Replay idempotente: reutiliza storage, sem motor nem billing adicionais.

    Não usa comparação temporal relativa (second < first), que é sensível a
    filesystem/cache/scheduler e não prova o contrato comportamental.
    """
    calls = {"motor": 0, "billing": 0, "save": 0, "load": 0}
    saved_keys: list[str] = []

    def motor(context):
        calls["motor"] += 1
        from app.agente_compara_comparison_calculation_service import calculate_comparison_in_memory

        return calculate_comparison_in_memory(context)

    def billing(**kwargs):
        calls["billing"] += 1

    import app.agente_compara_calculation_execution_service as exec_mod
    import app.agente_compara_calculation_result_storage as storage_mod

    real_save = storage_mod.save_comparison_calculation_result
    real_load = storage_mod.load_comparison_calculation_result

    def save_wrapper(**kwargs):
        calls["save"] += 1
        meta = real_save(**kwargs)
        saved_keys.append(str(meta.get("result_storage_key") or meta.get("storage_key") or ""))
        return meta

    def load_wrapper(**kwargs):
        calls["load"] += 1
        return real_load(**kwargs)

    monkeypatch.setattr(storage_mod, "save_comparison_calculation_result", save_wrapper)
    monkeypatch.setattr(storage_mod, "load_comparison_calculation_result", load_wrapper)
    monkeypatch.setattr(exec_mod, "save_comparison_calculation_result", save_wrapper)
    monkeypatch.setattr(exec_mod, "load_comparison_calculation_result", load_wrapper)

    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state()
        sess = _session_dict_from_state(state)
        comparison_id = state["comparison_id"]

        first = _exec(
            app,
            sess,
            comparison_id=comparison_id,
            execution_id="exec-replay-stable",
            calculate_fn=motor,
            emit_billing=billing,
        )
        assert first["ok"] is True
        assert first["idempotent_replay"] is False
        assert first["status"] == STEP_CALCULATION_READY
        assert first["billing_status"] == BILLING_STATUS_APPLIED
        assert first.get("result") is not None
        assert calls["motor"] == 1
        assert calls["billing"] == 1
        assert calls["save"] == 1
        # Primeira execução devolve public_result em memória; load pode ser zero.
        load_after_first = calls["load"]

        persisted_after_first = _persisted(sess)
        calc_after_first = persisted_after_first["comparison_calculation"]
        storage_key = calc_after_first.get("result_storage_key")
        checksum = calc_after_first.get("result_checksum")
        size_bytes = calc_after_first.get("result_size_bytes")
        fingerprint = calc_after_first.get("request_fingerprint")
        fingerprint_short = calc_after_first.get("fingerprint_short")
        execution_id = calc_after_first.get("execution_id")
        attempt_count = calc_after_first.get("attempt_count")
        billing_key = calc_after_first.get("billing_idempotency_key")
        table_ids = list(calc_after_first.get("table_ids") or [])
        calculated_table_count = calc_after_first.get("calculated_table_count")
        calculated_cell_count = calc_after_first.get("calculated_cell_count")
        error_cell_count = calc_after_first.get("error_cell_count")
        started_at = calc_after_first.get("started_at")
        finished_at = calc_after_first.get("finished_at")

        assert storage_key
        assert checksum
        assert fingerprint
        assert execution_id == "exec-replay-stable"
        assert storage_key in saved_keys
        assert calc_after_first.get("stale") is not True
        assert "result" not in calc_after_first or calc_after_first.get("result") is None
        assert persisted_after_first["current_step"] == STEP_CALCULATION_READY

        files_before_replay = sorted(p.name for p in tmp_path.rglob("*") if p.is_file())

        second = _exec(
            app,
            sess,
            comparison_id=comparison_id,
            execution_id="exec-replay-stable",
            calculate_fn=motor,
            emit_billing=billing,
        )

        assert second["ok"] is True
        assert second["idempotent_replay"] is True
        assert second["status"] == STEP_CALCULATION_READY
        assert second["billing_status"] == BILLING_STATUS_APPLIED
        assert second.get("fingerprint_short") == fingerprint_short
        assert second.get("result") is not None
        assert second.get("result") == first.get("result")
        if "analytics" in first or "analytics" in second:
            assert second.get("analytics") == first.get("analytics")

        # Motor e billing: sem incremento no replay applied.
        assert calls["motor"] == 1
        assert calls["billing"] == 1
        # Sem nova gravação do resultado.
        assert calls["save"] == 1
        # Replay lê o storage (contrato atual via _build_success_response).
        assert calls["load"] > load_after_first

        persisted_after_replay = _persisted(sess)
        calc_after_replay = persisted_after_replay["comparison_calculation"]
        assert persisted_after_replay["current_step"] == STEP_CALCULATION_READY
        assert calc_after_replay.get("status") == STEP_CALCULATION_READY
        assert calc_after_replay.get("result_storage_key") == storage_key
        assert calc_after_replay.get("result_checksum") == checksum
        assert calc_after_replay.get("result_size_bytes") == size_bytes
        assert calc_after_replay.get("request_fingerprint") == fingerprint
        assert calc_after_replay.get("fingerprint_short") == fingerprint_short
        assert calc_after_replay.get("execution_id") == execution_id
        assert calc_after_replay.get("attempt_count") == attempt_count
        assert calc_after_replay.get("billing_status") == BILLING_STATUS_APPLIED
        assert calc_after_replay.get("billing_idempotency_key") == billing_key
        assert list(calc_after_replay.get("table_ids") or []) == table_ids
        assert calc_after_replay.get("calculated_table_count") == calculated_table_count
        assert calc_after_replay.get("calculated_cell_count") == calculated_cell_count
        assert calc_after_replay.get("error_cell_count") == error_cell_count
        assert calc_after_replay.get("started_at") == started_at
        assert calc_after_replay.get("finished_at") == finished_at
        assert calc_after_replay.get("stale") is not True
        assert "result" not in calc_after_replay or calc_after_replay.get("result") is None

        files_after_replay = sorted(p.name for p in tmp_path.rglob("*") if p.is_file())
        assert files_after_replay == files_before_replay

        _assert_no_forbidden(second)
        import json

        json.dumps(second, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Checkpoint 5 — frontend contract
# ---------------------------------------------------------------------------


def test_frontend_process_calculations_contract():
    js = pathlib.Path("app/static/js/agente_compara.js").read_text(encoding="utf-8")
    html = pathlib.Path("app/templates/agente_compara.html").read_text(encoding="utf-8")

    assert "API_COMPARISON_CALCULATE" in js
    assert "/api/agente-compara/comparison/calculate" in js
    assert "API_COMPARISON_CALCULATION" in js
    assert "function processComparisonCalculations" in js
    assert "comparisonCalculationInFlight" in js
    assert "function bindProcessCalculationsButton" in js
    assert "function renderComparisonCalculationResults" in js
    assert "Frete calculado —" in js
    assert "Não calculado" in js
    assert "formatComparisonMoney" in js
    assert "aria-busy" in js
    assert "agente-compara-comparison-calculation-scroll" in html
    assert "runAuditProcessing" in js  # legado permanece, mas...
    process_fn = js[js.index("function processComparisonCalculations") : js.index("function clearCalculationFileSummary")]
    assert "API_AUDIT_RUN" not in process_fn
    assert "runAuditProcessing" not in process_fn
    assert "winner" not in process_fn.lower()
    assert "ranking" not in process_fn.lower()
    assert "savings" not in process_fn.lower()
    assert "charged_freight" not in process_fn
    assert js.count("function onProcessCalculationsButtonClick") == 1
    assert "dataset.processCalculationsBound" in js
    assert "generateRequestId()" in process_fn
    assert "Finalizando processamento" in js
    assert "Tentar novamente" in js
    assert "billing_status === 'applied'" in js
    assert "result_storage_key" not in process_fn
    assert "cleiton_doc_tmp" not in process_fn
    assert "cc_result_" not in process_fn


def test_visual_contract_columns_and_domain_errors(app, tmp_path, monkeypatch):
    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state(same_carrier=True)
        # Remove freight rules from table 2 so domain errors appear only nessa coluna.
        tt2 = get_table_by_slot(state, 2)["temp_table_id"]
        from app.agente_compara_doc_service import load_temp_table_record
        from app.services.cleiton_doc_config_service import get_cleiton_doc_config

        ttl = get_cleiton_doc_config().upload_ttl_hours
        record2 = load_temp_table_record(tt2, ttl_hours=ttl)
        assert record2 is not None
        record2["freight_tables"] = [
            {
                "table_title": "Tabela vazia",
                "table_type": "weight_range_table",
                "columns": ["Região de frete", "Até 30 kg", "31 a 50 kg", "Excedente kg"],
                "rows": [],
            }
        ]
        _write_record(record2)
        sess = _session_dict_from_state(state)
        out = _exec(
            app,
            sess,
            comparison_id=state["comparison_id"],
            execution_id="exec-visual",
            emit_billing=lambda **kwargs: None,
        )
        result = out["result"]
        assert result["table_count"] == 2
        assert len(result["tables"]) == 2
        assert result["tables"][0]["carrier_name"] == result["tables"][1]["carrier_name"]
        assert result["tables"][0]["table_id"] != result["tables"][1]["table_id"]
        dup_rows = [r for r in result["comparative_rows"] if r["document_number"] == "DOC-DUP"]
        assert len(dup_rows) == 2
        tid1 = result["tables"][0]["table_id"]
        tid2 = result["tables"][1]["table_id"]
        row1 = result["comparative_rows"][0]
        assert row1["table_results"][tid1]["status"] == "calculated"
        assert row1["table_results"][tid2]["status"] != "calculated"
        assert row1["table_results"][tid1].get("calculated_freight") is not None
        _assert_no_forbidden(result)


def test_execution_service_has_no_cleide_import():
    source = pathlib.Path("app/agente_compara_calculation_execution_service.py").read_text(encoding="utf-8")
    assert "from app.cleide" not in source
    assert "import app.cleide" not in source
    assert "generate_content" not in source
    assert "run_gemini" not in source


# ---------------------------------------------------------------------------
# Algorithm version — fingerprint / replay invalidation
# ---------------------------------------------------------------------------


def _fingerprint_payload_for_state(state: dict) -> dict:
    from app.agente_compara_doc_service import load_temp_table_record
    from app.services.cleiton_doc_config_service import get_cleiton_doc_config

    ttl = get_cleiton_doc_config().upload_ttl_hours
    primary = load_temp_table_record(state["primary_temp_table_id"], ttl_hours=ttl)
    rows = primary["audit_batch"]["normalized_rows"]
    coverage = primary["coverage_table"]
    records = {}
    for entry in state["tables"].values():
        if entry.get("confirmed"):
            records[entry["temp_table_id"]] = load_temp_table_record(entry["temp_table_id"], ttl_hours=ttl)
    return build_calculation_fingerprint_payload(
        comparison_id=state["comparison_id"],
        state=state,
        normalized_rows=rows,
        table_records=records,
        tax_config=state.get("tax_config"),
        coverage_table=coverage,
        source_file_identity={
            "audit_batch_id": primary["audit_batch"]["audit_batch_id"],
            "source_file_name": "operacional.xlsx",
            "sheet_name": "Planilha1",
            "row_count": len(rows),
            "input_schema_version": 1,
            "temp_table_id": state["primary_temp_table_id"],
        },
    )


def _conditional_fee(*, name: str, value: str = "10,00", unit: str = "R$") -> dict:
    return {
        "name": name,
        "value": value,
        "unit": unit,
        "calculation_basis": "sobre nota fiscal" if unit == "%" else "por CTe",
        "notes": "somente para remessas especiais",
    }


def _gbex_incomplete_comparison_state() -> dict:
    """Comparação sintética: tabela A incomplete (TAS/Pedágio), tabela B calculated."""
    sess: dict = {}
    state = create_comparison(session_obj=sess)
    t1 = get_table_by_slot(state, 1)
    t2 = get_table_by_slot(state, 2)
    assert t1 and t2
    tt1 = "tt_gbex_" + uuid4().hex[:8]
    tt2 = "tt_ctrl_" + uuid4().hex[:8]
    t1.update(
        {
            "temp_table_id": tt1,
            "confirmed": True,
            "status": TABLE_STATUS_CONFIRMED,
            "carrier_name": "Transportadora Sintetica A",
        }
    )
    t2.update(
        {
            "temp_table_id": tt2,
            "confirmed": True,
            "status": TABLE_STATUS_CONFIRMED,
            "carrier_name": "Transportadora Sintetica B",
        }
    )
    region = "PE-Caruaru"
    rows = [
        {
            "row_index": 1,
            "document_number": "DOC-GBEX",
            "destination_city": "Caruaru",
            "destination_uf": "PE",
            "audited_weight": 13.6,
            "invoice_value": 2000.0,
        }
    ]
    coverage = {
        "status": "needs_review",
        "columns": ["UF destino", "Cidade destino", "Região de frete"],
        "rows": [
            {
                "destination_uf": "PE",
                "destination_city": "Caruaru",
                "freight_region": region,
            }
        ],
    }
    fees_a = [
        {
            "name": "ADV",
            "value": "0,30%",
            "unit": "%",
            "calculation_basis": "sobre nota fiscal",
            "notes": "",
        },
        {
            "name": "GRIS",
            "value": "0,10%",
            "unit": "%",
            "calculation_basis": "sobre nota fiscal",
            "notes": "",
        },
        _conditional_fee(name="TAS", value="10,00", unit="R$"),
        _conditional_fee(name="Pedágio", value="1,50", unit="R$"),
    ]
    rec_a = _pricing_record(temp_table_id=tt1, region=region)
    rec_a["accessorial_fees"] = fees_a
    rec_a["coverage_table"] = copy.deepcopy(coverage)
    rec_a["freight_tables"][0]["rows"][0]["Até 30 kg"] = "80,00"
    rec_a["freight_tables"][0]["rows"][0]["31 a 50 kg"] = "95,00"
    rec_b = _pricing_record(temp_table_id=tt2, region=region)
    rec_b["coverage_table"] = copy.deepcopy(coverage)
    rec_b["freight_tables"][0]["rows"][0]["Até 30 kg"] = "100,50"
    rec_b["freight_tables"][0]["rows"][0]["31 a 50 kg"] = "110,00"
    for rec in (rec_a, rec_b):
        rec["audit_batch"] = {
            "status": "uploaded",
            "audit_batch_id": "batch-gbex-" + uuid4().hex[:8],
            "temp_table_id": tt1,
            "source_file_name": "operacional-gbex.xlsx",
            "sheet_name": "Planilha1",
            "row_count": len(rows),
            "input_schema_version": 1,
            "normalized_rows": rows,
        }
        _write_record(rec)
    # Primary hosts shared operational file.
    rec_a["audit_batch"]["temp_table_id"] = tt1
    _write_record(rec_a)
    state["primary_temp_table_id"] = tt1
    state["current_step"] = STEP_CONFIGURATION_READY
    state["status"] = COMPARISON_STATUS_CONFIGURATION_READY
    set_comparison_tax_config(
        state,
        {"include_taxes": False, "confirmed": True, "selected_table_ids": []},
    )
    persist_comparison_state(state, session_obj=sess)
    return state


def test_fingerprint_includes_algorithm_version(app, tmp_path, monkeypatch):
    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state()
        payload = _fingerprint_payload_for_state(state)
        assert payload["calculation_algorithm_version"] == AGENTE_COMPARA_CALCULATION_ALGORITHM_VERSION
        assert payload["schema_version"] == 1
        assert payload["calculation_algorithm_version"] != payload["schema_version"] or True
        fp1 = compute_calculation_fingerprint(payload)
        fp2 = compute_calculation_fingerprint(copy.deepcopy(payload))
        assert fp1 == fp2
        assert len(fp1) == 64


def test_fingerprint_same_input_same_version_equal(app, tmp_path, monkeypatch):
    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state()
        p1 = _fingerprint_payload_for_state(state)
        p2 = _fingerprint_payload_for_state(state)
        assert compute_calculation_fingerprint(p1) == compute_calculation_fingerprint(p2)


def test_fingerprint_version_change_changes_hash(app, tmp_path, monkeypatch):
    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state()
        payload = _fingerprint_payload_for_state(state)
        fp_current = compute_calculation_fingerprint(payload)
        legacy = copy.deepcopy(payload)
        legacy["calculation_algorithm_version"] = 1
        fp_legacy = compute_calculation_fingerprint(legacy)
        assert fp_current != fp_legacy
        without = copy.deepcopy(payload)
        without.pop("calculation_algorithm_version", None)
        assert compute_calculation_fingerprint(without) != fp_current


def test_fingerprint_version_does_not_alter_operational_fields(app, tmp_path, monkeypatch):
    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state()
        payload = _fingerprint_payload_for_state(state)
        operational_keys = {
            "comparison_id",
            "schema_version",
            "source_file_identity",
            "tables",
            "table_count",
            "tax_config",
            "coverage_digest",
        }
        for key in operational_keys:
            assert key in payload
        assert set(payload.keys()) == operational_keys | {"calculation_algorithm_version"}


def test_fingerprint_deterministic_order_and_json_safe(app, tmp_path, monkeypatch):
    import json as _json

    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state()
        payload = _fingerprint_payload_for_state(state)
        encoded = _json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        decoded = _json.loads(encoded)
        assert compute_calculation_fingerprint(decoded) == compute_calculation_fingerprint(payload)


def test_algorithm_version_constant_stable_and_single():
    import re

    source = pathlib.Path("app/agente_compara_calculation_execution_service.py").read_text(encoding="utf-8")
    matches = re.findall(r"^AGENTE_COMPARA_CALCULATION_ALGORITHM_VERSION\s*=\s*(\d+)", source, re.M)
    assert matches == [str(AGENTE_COMPARA_CALCULATION_ALGORITHM_VERSION)]
    assert AGENTE_COMPARA_CALCULATION_ALGORITHM_VERSION == 2
    assert "datetime.now" not in source.split("AGENTE_COMPARA_CALCULATION_ALGORITHM_VERSION")[1][:200]


def test_legacy_result_without_algorithm_version_reads_safely():
    from app.agente_compara_comparison_state import public_comparison_calculation_summary

    legacy = {
        "schema_version": 1,
        "execution_id": "exec-legacy",
        "fingerprint_short": "abc",
        "status": STEP_CALCULATION_READY,
        "stale": False,
        "billing_status": BILLING_STATUS_APPLIED,
        "attempt_count": 1,
    }
    summary = public_comparison_calculation_summary(legacy)
    assert summary is not None
    assert summary.get("calculation_algorithm_version") is None
    assert summary["status"] == STEP_CALCULATION_READY


def test_replay_same_algorithm_version_reuses_result(app, tmp_path, monkeypatch):
    calls = {"motor": 0, "billing": 0, "save": 0}

    def motor(context):
        calls["motor"] += 1
        from app.agente_compara_comparison_calculation_service import calculate_comparison_in_memory

        return calculate_comparison_in_memory(context)

    def billing(**kwargs):
        calls["billing"] += 1

    import app.agente_compara_calculation_execution_service as exec_mod

    real_save = exec_mod.save_comparison_calculation_result

    def save_spy(**kwargs):
        calls["save"] += 1
        return real_save(**kwargs)

    monkeypatch.setattr(exec_mod, "save_comparison_calculation_result", save_spy)

    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state()
        sess = _session_dict_from_state(state)
        first = _exec(
            app,
            sess,
            comparison_id=state["comparison_id"],
            execution_id="algo-replay-1",
            calculate_fn=motor,
            emit_billing=billing,
        )
        second = _exec(
            app,
            sess,
            comparison_id=state["comparison_id"],
            execution_id="algo-replay-2",
            calculate_fn=motor,
            emit_billing=billing,
        )
        assert first["idempotent_replay"] is False
        assert second["idempotent_replay"] is True
        assert calls["motor"] == 1
        assert calls["billing"] == 1
        assert calls["save"] == 1
        assert first["result"]["calculation_algorithm_version"] == AGENTE_COMPARA_CALCULATION_ALGORITHM_VERSION
        assert second["result"]["calculation_algorithm_version"] == AGENTE_COMPARA_CALCULATION_ALGORITHM_VERSION
        calc = _persisted(sess)["comparison_calculation"]
        assert calc["calculation_algorithm_version"] == AGENTE_COMPARA_CALCULATION_ALGORITHM_VERSION


def test_different_algorithm_version_forces_new_execution(app, tmp_path, monkeypatch):
    calls = {"motor": 0, "billing": 0}

    def motor(context):
        calls["motor"] += 1
        from app.agente_compara_comparison_calculation_service import calculate_comparison_in_memory

        return calculate_comparison_in_memory(context)

    def billing(**kwargs):
        calls["billing"] += 1

    import app.agente_compara_calculation_execution_service as exec_mod

    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state()
        sess = _session_dict_from_state(state)
        first = _exec(
            app,
            sess,
            comparison_id=state["comparison_id"],
            execution_id="algo-v-old",
            calculate_fn=motor,
            emit_billing=billing,
        )
        assert first["idempotent_replay"] is False
        assert calls["motor"] == 1
        assert calls["billing"] == 1
        calc = _persisted(sess)["comparison_calculation"]
        old_key = calc["result_storage_key"]
        old_fp = calc["request_fingerprint"]
        assert calc["calculation_algorithm_version"] == AGENTE_COMPARA_CALCULATION_ALGORITHM_VERSION

        # Bump algorithm version → fingerprint muda → READY antigo não é replay.
        monkeypatch.setattr(exec_mod, "AGENTE_COMPARA_CALCULATION_ALGORITHM_VERSION", 99)

        second = _exec(
            app,
            sess,
            comparison_id=state["comparison_id"],
            execution_id="algo-v-new",
            calculate_fn=motor,
            emit_billing=billing,
        )
        assert second["idempotent_replay"] is False
        assert calls["motor"] == 2
        assert calls["billing"] == 2
        calc2 = _persisted(sess)["comparison_calculation"]
        assert calc2["request_fingerprint"] != old_fp
        assert calc2["calculation_algorithm_version"] == 99
        assert calc2["result_storage_key"] != old_key
        assert second["result"]["calculation_algorithm_version"] == 99
        assert second["idempotent_replay"] is False


def test_legacy_calculated_ready_not_replayed_after_version_bump(app, tmp_path, monkeypatch):
    """Resultado legado calculated com fingerprint antigo não mascara motor novo."""
    from app.agente_compara_calculation_result_storage import (
        build_result_storage_key,
        save_comparison_calculation_result,
    )

    calls = {"motor": 0}

    def motor(context):
        calls["motor"] += 1
        from app.agente_compara_comparison_calculation_service import calculate_comparison_in_memory

        out = calculate_comparison_in_memory(context)
        # Force incomplete on first table cell to prove new motor path runs.
        for row in out.get("comparative_rows") or []:
            for cell in (row.get("table_results") or {}).values():
                if isinstance(cell, dict) and cell.get("status") == "calculated":
                    cell["status"] = "incomplete"
                    cell["final_status"] = "incomplete"
                    cell["is_partial_value"] = True
                    break
            break
        return out

    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state()
        sess = _session_dict_from_state(state)
        cmp_id = state["comparison_id"]
        legacy_fp = "a" * 64
        legacy_result = {
            "schema_version": 1,
            "comparison_id": cmp_id,
            "execution_id": "legacy-exec",
            "table_count": 2,
            "row_count": 1,
            "tables": [],
            "comparative_rows": [
                {
                    "row_index": 1,
                    "table_results": {
                        "t1": {"status": "calculated", "final_status": "calculated", "calculated_freight": 999.0}
                    },
                }
            ],
            "summary": {"calculated_cell_count": 2, "error_cell_count": 0, "incomplete_cell_count": 0},
        }
        meta = save_comparison_calculation_result(
            comparison_id=cmp_id,
            fingerprint=legacy_fp,
            result=legacy_result,
        )
        state["current_step"] = STEP_CALCULATION_READY
        state["status"] = COMPARISON_STATUS_CALCULATION_READY
        state["comparison_calculation"] = {
            "schema_version": 1,
            # sem calculation_algorithm_version → legado
            "execution_id": "legacy-exec",
            "request_fingerprint": legacy_fp,
            "fingerprint_short": legacy_fp[:12],
            "status": STEP_CALCULATION_READY,
            "stale": False,
            "result_storage_key": meta["result_storage_key"],
            "result_checksum": meta["result_checksum"],
            "result_size_bytes": meta["result_size_bytes"],
            "result_schema_version": meta.get("result_schema_version"),
            "billing_status": BILLING_STATUS_APPLIED,
            "source_row_count": 4,
            "calculated_table_count": 2,
            "calculated_cell_count": 2,
            "error_cell_count": 0,
            "attempt_count": 1,
        }
        persist_comparison_state(state, session_obj=sess)
        sess = _session_dict_from_state(get_comparison_state(sess) or state)

        out = _exec(
            app,
            sess,
            comparison_id=cmp_id,
            execution_id="post-legacy",
            calculate_fn=motor,
            emit_billing=lambda **kwargs: None,
        )
        assert out["idempotent_replay"] is False
        assert calls["motor"] == 1
        assert out["result"]["calculation_algorithm_version"] == AGENTE_COMPARA_CALCULATION_ALGORITHM_VERSION
        # Must not return the legacy calculated freight 999.
        freights = []
        for row in out["result"].get("comparative_rows") or []:
            for cell in (row.get("table_results") or {}).values():
                if isinstance(cell, dict):
                    freights.append(cell.get("calculated_freight"))
        assert 999.0 not in freights
        calc = _persisted(sess)["comparison_calculation"]
        assert calc["request_fingerprint"] != legacy_fp
        assert calc["result_storage_key"] != meta["result_storage_key"]
        assert build_result_storage_key(comparison_id=cmp_id, fingerprint=legacy_fp) == meta["result_storage_key"]


def test_api_post_get_gbex_preserves_calculated_with_warnings_status(web_client, app, tmp_path, monkeypatch):
    """POST/GET real: incomplete sem injeção de payload no frontend."""
    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _gbex_incomplete_comparison_state()
    with web_client.session_transaction() as sess:
        sess[AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = state
        sess[AGENTE_COMPARA_TEMP_TABLE_ID_SESSION_KEY] = state["primary_temp_table_id"]

    monkeypatch.setattr(
        "app.agente_compara_doc_service._emit_agente_compara_operational_billing",
        lambda *args, **kwargs: True,
    )

    resp = web_client.post(
        "/api/agente-compara/comparison/calculate",
        json={
            "comparison_id": state["comparison_id"],
            "execution_id": "api-gbex-1",
            "schema_version": 1,
        },
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["idempotent_replay"] is False
    result = body["result"]
    assert result["calculation_algorithm_version"] == AGENTE_COMPARA_CALCULATION_ALGORITHM_VERSION
    assert int(result["summary"].get("calculated_with_warnings_cell_count") or 0) >= 1

    rows = result["comparative_rows"]
    assert len(rows) == 1
    table_results = rows[0]["table_results"]
    warning_cells = [
        c for c in table_results.values() if (c.get("final_status") or c.get("status")) == "calculated_with_warnings"
    ]
    assert warning_cells, "esperado ao menos um calculated_with_warnings (TAS/Ped?gio)"
    cell = warning_cells[0]
    assert cell["status"] == "calculated_with_warnings"
    assert cell["final_status"] == "calculated_with_warnings"
    assert cell.get("is_partial_value") is False
    assert cell.get("calculated_freight") is not None
    memory = cell.get("calculation_memory") or {}
    assert memory.get("status") == "calculated_with_warnings"
    assert "ressalvas" in str(memory.get("status_label") or "").lower()
    labels = " ".join(
        str(i.get("label") or "") for i in (cell.get("blocking_issues") or [])
    ) + " " + str((cell.get("components") or {}).get("ignored_accessorial_fees") or [])
    assert "TAS" in labels
    assert "Pedágio" in labels or "Pedagio" in labels

    get_resp = web_client.get(
        f"/api/agente-compara/comparison/calculation?comparison_id={state['comparison_id']}"
    )
    assert get_resp.status_code == 200
    get_body = get_resp.get_json()
    assert get_body["status"] == STEP_CALCULATION_READY
    get_result = get_body["result"]
    assert get_result["calculation_algorithm_version"] == AGENTE_COMPARA_CALCULATION_ALGORITHM_VERSION
    get_cell = None
    for row in get_result["comparative_rows"]:
        for c in (row.get("table_results") or {}).values():
            if (c.get("final_status") or c.get("status")) == "calculated_with_warnings":
                get_cell = c
                break
    assert get_cell is not None
    assert get_cell["status"] == "calculated_with_warnings"
    assert get_cell["final_status"] == "calculated_with_warnings"
    assert (get_cell.get("calculation_memory") or {}).get("status") == "calculated_with_warnings"


def test_storage_failure_preserves_stage_and_metrics(app, tmp_path, monkeypatch):
    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state(three_tables=True)
        sess = _session_dict_from_state(state)
        import app.agente_compara_calculation_execution_service as exec_mod
        import app.agente_compara_calculation_result_storage as storage_mod

        def fail_memory(**kwargs):
            raise storage_mod.AgenteComparaCalculationResultStorageError(
                storage_mod.ERROR_MEMORY_WRITE_FAILED,
                'memory write failed',
                safe_message='Os c?lculos foram processados, mas os detalhes n?o puderam ser salvos.',
                error_stage='memory_replaced',
                artifact_type='memory',
                retryable=True,
                metrics={
                    'last_completed_stage': 'memory_checksum_validated',
                    'raw_result_size_bytes': 999,
                    'compact_result_size_bytes': 555,
                    'memory_payload_size_bytes': 444,
                    'memory_envelope_size_bytes': 500,
                    'table_count': 3,
                    'cell_count': 6000,
                },
                operation='memory.write_atomic',
            )

        monkeypatch.setattr(storage_mod, 'save_comparison_calculation_memory_payload', fail_memory)
        monkeypatch.setattr(exec_mod, 'save_comparison_calculation_memory_payload', fail_memory)
        result = _exec(
            app,
            sess,
            comparison_id=state['comparison_id'],
            execution_id='exec-storage-failure',
            calculate_fn=lambda _context: copy.deepcopy(__import__('tests.test_agente_compara_calculation_storage_integration', fromlist=['_big_result'])._big_result(50, 3, comparison_id=state['comparison_id'])),
            emit_billing=lambda **kwargs: (_ for _ in ()).throw(AssertionError('billing')),
        )
        assert result['ok'] is False
        assert result['error_code'] == storage_mod.ERROR_MEMORY_WRITE_FAILED
        assert result['error_stage'] == 'memory_replaced'
        assert result['artifact_type'] == 'memory'
        assert result['retryable'] is True
        calc = _persisted(sess)['comparison_calculation']
        assert calc['failed_stage'] == 'memory_replaced'
        assert calc['failed_artifact'] == 'memory'
        assert calc['raw_result_size_bytes'] == 999
        assert calc['compact_result_size_bytes'] == 555
        assert calc['memory_payload_size_bytes'] == 444
        assert calc['memory_envelope_size_bytes'] == 500
        assert calc['billing_status'] == BILLING_STATUS_NOT_STARTED
        assert calc['result_storage_key'] is None
        assert calc['memory_storage_key'] is None


def test_preflight_blocks_residual_failed_table_before_calculation(app, tmp_path, monkeypatch):
    billing_calls = []

    def _emit(**kwargs):
        billing_calls.append(kwargs)

    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state()
        from app.agente_compara_doc_service import load_temp_table_record
        from app.services.cleiton_doc_config_service import get_cleiton_doc_config

        ttl = get_cleiton_doc_config().upload_ttl_hours
        table_2 = get_table_by_slot(state, 2)
        record = load_temp_table_record(table_2["temp_table_id"], ttl_hours=ttl)
        record["status"] = "failed"
        record["reading_alerts"] = ["Gemini retornou timeout durante a extra??o t?cnica."]
        record["failure_origin"] = "platform"
        record["failure_code"] = "provider_timeout"
        record["retryable"] = True
        record["credit_disposition"] = "preserved"
        _write_record(record)
        sess = _session_dict_from_state(state)

        from app.agente_compara_calculation_execution_service import AgenteComparaCalculationExecutionError

        with pytest.raises(AgenteComparaCalculationExecutionError) as exc:
            _exec(
                app,
                sess,
                comparison_id=state["comparison_id"],
                execution_id="exec-preflight-block",
                emit_billing=_emit,
            )
        assert exc.value.error_code == "comparison_table_preparation_failed"
        assert exc.value.error_stage == "table_preflight_validated"
        assert exc.value.failed_table_name == table_2["carrier_name"]
        assert exc.value.failed_table_id == table_2["table_id"]
        assert exc.value.failed_slot == 2
        assert exc.value.retryable is True
        assert exc.value.credit_disposition == "preserved"
        assert exc.value.safe_message == (
            f"A tabela {table_2['carrier_name']} ainda n?o est? pronta. "
            "Processe novamente essa tabela antes de iniciar a compara??o."
        )
        assert billing_calls == []
        persisted = _persisted(sess)
        assert persisted.get("comparison_calculation") is None


def test_api_preflight_failure_returns_public_payload(web_client, app, tmp_path, monkeypatch):
    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
        state = _ready_comparison_state()
        from app.agente_compara_doc_service import load_temp_table_record
        from app.services.cleiton_doc_config_service import get_cleiton_doc_config

        ttl = get_cleiton_doc_config().upload_ttl_hours
        table_2 = get_table_by_slot(state, 2)
        record = load_temp_table_record(table_2["temp_table_id"], ttl_hours=ttl)
        record["status"] = "failed"
        record["reading_alerts"] = ["Gemini retornou timeout durante a extra??o t?cnica."]
        record["failure_origin"] = "platform"
        record["failure_code"] = "provider_timeout"
        record["retryable"] = True
        record["credit_disposition"] = "preserved"
        _write_record(record)
    with web_client.session_transaction() as sess:
        sess[AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = state
        sess[AGENTE_COMPARA_TEMP_TABLE_ID_SESSION_KEY] = state["primary_temp_table_id"]

    resp = web_client.post(
        "/api/agente-compara/comparison/calculate",
        json={
            "comparison_id": state["comparison_id"],
            "execution_id": "api-preflight-block",
            "schema_version": 1,
        },
    )
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["ok"] is False
    assert body["error_code"] == "comparison_table_preparation_failed"
    assert body["error_stage"] == "table_preflight_validated"
    assert body["failed_table_name"] == table_2["carrier_name"]
    assert body["failed_table_id"] == table_2["table_id"]
    assert body["failed_slot"] == 2
    assert body["retryable"] is True
    assert body["credit_disposition"] == "preserved"
    assert body["safe_message"] == body["message"]
    assert "ainda" in body["message"].lower()


def test_api_calculate_success_creates_funnel_event_and_first_audit(web_client, app, monkeypatch):
    web = _load_web_module()
    _authorized_funnel_user(monkeypatch, web)
    _patch_billing_success(monkeypatch)
    monkeypatch.setattr(
        "app.agente_compara_calculation_execution_service._record_calculation_funnel_event",
        lambda **_kwargs: ({"is_first_audit": True, "funnel_event": {"event_name": "freight_calculated", "source": "agente_compara", "allow_meta_pixel": True, "is_first_audit": True}}, True),
    )

    state = _ready_comparison_state()
    _persist_ready_fixture_state(web_client, state)

    resp = web_client.post(
        "/api/agente-compara/comparison/calculate",
        json={"comparison_id": state["comparison_id"], "execution_id": "exec-funnel-1"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["billing_status"] == BILLING_STATUS_APPLIED
    assert body["is_first_audit"] is True
    assert body["funnel_event"]["event_name"] == "freight_calculated"
    assert body["funnel_event"]["allow_meta_pixel"] is True
    assert body["funnel_event"]["is_first_audit"] is True



def test_api_calculate_replay_omits_funnel_event_and_first_audit_false(web_client, app, monkeypatch):
    web = _load_web_module()
    _authorized_funnel_user(monkeypatch, web)
    _patch_billing_success(monkeypatch)
    monkeypatch.setattr(
        "app.agente_compara_calculation_execution_service._record_calculation_funnel_event",
        lambda **_kwargs: ({"is_first_audit": False}, False),
    )
    state = _ready_comparison_state()
    _persist_ready_fixture_state(web_client, state)

    first = web_client.post(
        "/api/agente-compara/comparison/calculate",
        json={"comparison_id": state["comparison_id"], "execution_id": "exec-funnel-replay"},
    )
    replay = web_client.post(
        "/api/agente-compara/comparison/calculate",
        json={"comparison_id": state["comparison_id"], "execution_id": "exec-funnel-replay"},
    )
    assert first.status_code == 200
    body = replay.get_json()
    assert body["idempotent_replay"] is True
    assert body["is_first_audit"] is False
    assert "funnel_event" not in body


def test_api_calculate_analytics_failure_preserves_success_and_billing(web_client, app, monkeypatch):
    web = _load_web_module()
    _authorized_funnel_user(monkeypatch, web)
    _patch_billing_success(monkeypatch)
    monkeypatch.setattr(
        "app.agente_compara_calculation_execution_service._record_calculation_funnel_event",
        lambda **_kwargs: ({"is_first_audit": False}, False),
    )
    state = _ready_comparison_state()
    _persist_ready_fixture_state(web_client, state)
    monkeypatch.setattr(
        "app.agente_compara_calculation_execution_service.record_funnel_event",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("boom")),
    )

    resp = web_client.post(
        "/api/agente-compara/comparison/calculate",
        json={"comparison_id": state["comparison_id"], "execution_id": "exec-funnel-fail"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == STEP_CALCULATION_READY
    assert body["billing_status"] == BILLING_STATUS_APPLIED
    assert body["is_first_audit"] is False
    assert "funnel_event" not in body





def _patch_billing_success(monkeypatch):
    def _fake_apply_billing(*, calc, rows_processed, started_perf, execution_id, emit_billing):
        updated = dict(calc)
        updated["billing_status"] = BILLING_STATUS_APPLIED
        updated["billing_applied_at"] = "2026-08-06T00:00:00Z"
        return updated, True

    monkeypatch.setattr(
        "app.agente_compara_calculation_execution_service._apply_billing",
        _fake_apply_billing,
    )

def _persist_ready_fixture_state(web_client, state: dict) -> None:
    with web_client.session_transaction() as sess:
        sess[AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = state


def test_record_calculation_funnel_event_persists_and_marks_first_audit(app, monkeypatch):
    from app.agente_compara_calculation_execution_service import _record_calculation_funnel_event

    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-funnel-helper")
        user = seed_usuario(franquia.id, conta.id, email="calc-helper@test.com")
        user_id = user.id
        fake_user = SimpleNamespace(
            is_authenticated=True,
            id=user.id,
            conta_id=user.conta_id,
            franquia_id=user.franquia_id,
        )
        monkeypatch.setattr("app.agente_compara_calculation_execution_service.current_user", fake_user)

        payload, created = _record_calculation_funnel_event(
            comparison_id="cmp-helper-1",
            execution_id="exec-helper-1",
            idempotent_replay=False,
            calc={"status": STEP_CALCULATION_READY, "billing_status": BILLING_STATUS_APPLIED, "stale": False},
        )

        assert created is True
        assert payload["is_first_audit"] is True
        assert payload["funnel_event"]["allow_meta_pixel"] is True
        db.session.remove()
        refreshed = db.session.get(User, user_id)
        event = FunnelEvent.query.order_by(FunnelEvent.id.desc()).first()
        assert refreshed.first_audit_completed_at is not None
        assert refreshed.first_audit_completed_at.tzinfo is None
        assert event.comparison_id == "cmp-helper-1"
        assert event.execution_id == "exec-helper-1"



def test_record_calculation_funnel_event_replay_omits_pixel_and_first_audit_false(app, monkeypatch):
    from app.agente_compara_calculation_execution_service import _record_calculation_funnel_event

    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-funnel-helper-replay")
        user = seed_usuario(franquia.id, conta.id, email="calc-helper-replay@test.com")
        fake_user = SimpleNamespace(
            is_authenticated=True,
            id=user.id,
            conta_id=user.conta_id,
            franquia_id=user.franquia_id,
        )
        monkeypatch.setattr("app.agente_compara_calculation_execution_service.current_user", fake_user)

        first_payload, first_created = _record_calculation_funnel_event(
            comparison_id="cmp-helper-replay",
            execution_id="exec-helper-replay",
            idempotent_replay=False,
            calc={"status": STEP_CALCULATION_READY, "billing_status": BILLING_STATUS_APPLIED, "stale": False},
        )
        replay_payload, replay_created = _record_calculation_funnel_event(
            comparison_id="cmp-helper-replay",
            execution_id="exec-helper-replay",
            idempotent_replay=True,
            calc={"status": STEP_CALCULATION_READY, "billing_status": BILLING_STATUS_APPLIED, "stale": False},
        )

        assert first_created is True
        assert replay_created is False
        assert first_payload["is_first_audit"] is True
        assert replay_payload == {"is_first_audit": False}
        db.session.remove()
        assert FunnelEvent.query.count() == 2


def test_record_calculation_funnel_event_does_not_overwrite_first_audit_timestamp(app, monkeypatch):
    from app.agente_compara_calculation_execution_service import _record_calculation_funnel_event

    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-funnel-helper-repeat")
        user = seed_usuario(franquia.id, conta.id, email="calc-helper-repeat@test.com")
        user_id = user.id
        fake_user = SimpleNamespace(
            is_authenticated=True,
            id=user.id,
            conta_id=user.conta_id,
            franquia_id=user.franquia_id,
        )
        monkeypatch.setattr("app.agente_compara_calculation_execution_service.current_user", fake_user)

        first_payload, first_created = _record_calculation_funnel_event(
            comparison_id="cmp-helper-repeat",
            execution_id="exec-helper-repeat-1",
            idempotent_replay=False,
            calc={"status": STEP_CALCULATION_READY, "billing_status": BILLING_STATUS_APPLIED, "stale": False},
        )
        db.session.remove()
        first_timestamp = db.session.get(User, user_id).first_audit_completed_at

        second_payload, second_created = _record_calculation_funnel_event(
            comparison_id="cmp-helper-repeat",
            execution_id="exec-helper-repeat-2",
            idempotent_replay=False,
            calc={"status": STEP_CALCULATION_READY, "billing_status": BILLING_STATUS_APPLIED, "stale": False},
        )

        assert first_created is True
        assert second_created is True
        assert first_payload["is_first_audit"] is True
        assert second_payload["is_first_audit"] is False
        db.session.remove()
        refreshed = db.session.get(User, user_id)
        assert refreshed.first_audit_completed_at == first_timestamp
        assert FunnelEvent.query.count() == 3


def test_record_calculation_funnel_event_commit_failure_rolls_back_without_undoing_billing(app, monkeypatch):
    from app.agente_compara_calculation_execution_service import _record_calculation_funnel_event

    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-funnel-helper-commit-fail")
        user = seed_usuario(franquia.id, conta.id, email="calc-helper-commit-fail@test.com")
        user_id = user.id
        fake_user = SimpleNamespace(
            is_authenticated=True,
            id=user.id,
            conta_id=user.conta_id,
            franquia_id=user.franquia_id,
        )
        monkeypatch.setattr("app.agente_compara_calculation_execution_service.current_user", fake_user)

        calls = {"commit": 0, "rollback": 0}
        real_rollback = db.session.rollback
        orm_session = db.session()

        def fail_before_commit(_session):
            calls["commit"] += 1
            real_rollback()
            raise RuntimeError("commit failed")

        def tracked_rollback():
            calls["rollback"] += 1
            return real_rollback()

        monkeypatch.setattr("app.agente_compara_calculation_execution_service.record_funnel_event", lambda **_kwargs: {"created": True})
        event.listen(orm_session, "before_commit", fail_before_commit)
        monkeypatch.setattr(db.session, "rollback", tracked_rollback)

        payload, created = _record_calculation_funnel_event(
            comparison_id="cmp-helper-commit-fail",
            execution_id="exec-helper-commit-fail",
            idempotent_replay=False,
            calc={"status": STEP_CALCULATION_READY, "billing_status": BILLING_STATUS_APPLIED, "stale": False},
        )

        assert created is False
        assert payload == {"is_first_audit": False}
        assert calls["commit"] == 1
        assert calls["rollback"] == 1
        event.remove(orm_session, "before_commit", fail_before_commit)
        db.session.remove()
        refreshed = db.session.get(User, user_id)
        assert refreshed.first_audit_completed_at is None
        assert FunnelEvent.query.count() == 0
