"""Ponte analítica do chat do Agente Compara (SCRUM-238, alternativa A)."""
from __future__ import annotations

import inspect
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.agente_compara_calculation_execution_service import _rehydrate_compact_result
from app.agente_compara_calculation_result_storage import (
    load_comparison_calculation_result,
    save_comparison_calculation_memory_payload,
    save_comparison_calculation_result,
)
from app.agente_compara_chat_context_service import (
    SNAPSHOT_MISMATCH,
    SNAPSHOT_NOT_AUTHORIZED,
    SNAPSHOT_OBSOLETE,
    SNAPSHOT_UNAVAILABLE,
    authorize_comparison_snapshot_identity,
)
from app.agente_compara_chat_evidence_adapter import publication_document_closed
from app.agente_compara_chat_query_catalog import CATALOG_VERSION, artifact_checksum
from app.agente_compara_comparison_state import (
    AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY,
    COMPARISON_STATUS_CALCULATION_READY,
    STEP_CALCULATION_READY,
)
from app.run_agente_compara_comparison_chat import (
    chat_agente_compara_comparison_reply,
    execute_comparison_chat_analytical_plan,
)
from tests.cleiton_doc_fixtures import patch_cleiton_doc_store

_OPERATION = "22222222-2222-4222-8222-222222222222"
_CONTA = 11
_USUARIO = 22
_FRANQUIA = 33
_OTHER_CONTA = 44
_OTHER_USUARIO = 55
_OTHER_FRANQUIA = 66
_SECRET_DOCUMENT = "NF-SIGILO-238"
_SECRET_CARRIER = "TransportadoraSigilosa"


def _session(state=None):
    class _S(dict):
        modified = False

    sess = _S()
    if state is not None:
        sess[AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = state
    return sess


def _usuario(conta_id=_CONTA, usuario_id=_USUARIO, franquia_id=_FRANQUIA, *, authenticated=True):
    return SimpleNamespace(
        id=usuario_id,
        conta_id=conta_id,
        franquia_id=franquia_id,
        is_authenticated=authenticated,
    )


def _tables():
    return [
        {"table_id": "t1", "slot_number": 1, "carrier_name": f"{_SECRET_CARRIER}A"},
        {"table_id": "t2", "slot_number": 2, "carrier_name": f"{_SECRET_CARRIER}B"},
        {"table_id": "t3", "slot_number": 3, "carrier_name": f"{_SECRET_CARRIER}C"},
    ]


def _cell(table, freight, status, *, partial=False, error=None, ref=None):
    return {
        "table_id": table["table_id"],
        "carrier_name": table["carrier_name"],
        "slot_number": table["slot_number"],
        "calculated_freight": freight,
        "status": status,
        "final_status": status,
        "completeness_status": status,
        "is_partial_value": partial,
        "error": error,
        "memory_ref": ref,
    }


def _official_pair(comparison_id):
    tables = _tables()
    rows = [
        {
            "row_index": 1,
            "document_number": _SECRET_DOCUMENT,
            "destination_uf": "SP",
            "weight": 12,
            "table_results": {
                "t1": _cell(tables[0], 10.0, "calculated", ref="t1:1"),
                "t2": _cell(tables[1], 14.0, "calculated", ref="t2:1"),
                "t3": _cell(tables[2], 18.0, "calculated", ref="t3:1"),
            },
        },
        {
            "row_index": 2,
            "document_number": "NF-SIGILO-INCOMPLETO",
            "destination_uf": "RJ",
            "weight": 40,
            "table_results": {
                "t1": _cell(tables[0], 5.0, "incomplete", partial=True, ref="t1:2"),
                "t2": _cell(tables[1], 8.0, "calculated", ref="t2:2"),
                "t3": _cell(tables[2], 9.0, "calculated", ref="t3:2"),
            },
        },
        {
            "row_index": 3,
            "document_number": "NF-SIGILO-DIAG",
            "destination_uf": "MG",
            "weight": None,
            "table_results": {
                "t1": _cell(
                    tables[0],
                    None,
                    "missing_coverage_mapping",
                    error={"code": "missing_coverage_mapping"},
                    ref="t1:3",
                ),
                "t2": _cell(tables[1], None, "not_calculated", ref="t2:3"),
                "t3": _cell(tables[2], None, "not_calculated", ref="t3:3"),
            },
        },
    ]
    result = {
        "schema_version": 2,
        "comparison_id": comparison_id,
        "table_count": 3,
        "tables": tables,
        "row_count": 3,
        "comparative_rows": rows,
    }
    memory = {
        "schema_version": 3,
        "comparison_id": comparison_id,
        "components": {
            "cmp-t1": {"weight_freight": 10, "taxes": 0, "total": 10},
            "cmp-t3": {"weight_freight": 18, "total": 18},
        },
        "items": {
            "t1:1": {
                "comparison_id": comparison_id,
                "component_ref": "cmp-t1",
                "calculation_memory": {"status": "calculated", "total": 10, "calculated_freight": 10},
            },
            "t2:1": {
                "comparison_id": comparison_id,
                "component_ref": "cmp-missing",
                "calculation_memory": {"status": "calculated", "total": 14, "calculated_freight": 14},
            },
            "t3:1": {
                "comparison_id": comparison_id,
                "component_ref": "cmp-t3",
                "calculation_memory": {"status": "calculated", "total": 18, "calculated_freight": 18},
            },
        },
    }
    return result, memory


def _state(comparison_id, *, result_meta, memory_meta, billing="applied", stale=False, status=STEP_CALCULATION_READY):
    tables = {}
    for table in _tables():
        tables[table["table_id"]] = {
            "table_id": table["table_id"],
            "slot_number": table["slot_number"],
            "status": "confirmed",
            "doc_ids": [],
            "temp_table_id": f"tmp-{table['table_id']}",
            "carrier_name": table["carrier_name"],
            "confirmed": True,
            "error": None,
        }
    return {
        "comparison_id": comparison_id,
        "status": COMPARISON_STATUS_CALCULATION_READY,
        "current_step": STEP_CALCULATION_READY,
        "active_table_id": "t1",
        "desired_table_count": 3,
        "primary_temp_table_id": "tmp-t1",
        "tax_config": None,
        "tables": tables,
        "conta_id": _CONTA,
        "usuario_id": _USUARIO,
        "franquia_id": _FRANQUIA,
        "comparison_calculation": {
            "status": status,
            "stale": stale,
            "billing_status": billing,
            "request_fingerprint": result_meta["fingerprint"],
            "result_storage_key": result_meta["result_storage_key"],
            "result_checksum": result_meta["result_checksum"],
            "result_schema_version": result_meta["result_schema_version"],
            "memory_storage_key": memory_meta["memory_storage_key"],
            "memory_checksum": memory_meta["memory_checksum"],
            "memory_schema_version": memory_meta["memory_schema_version"],
        },
    }


def _query(query_id, query_type):
    return {"query_id": query_id, "type": query_type}


def _plan(queries):
    return {"catalog_version": CATALOG_VERSION, "queries": queries}


@pytest.fixture
def storage_env(tmp_path, monkeypatch):
    patch_cleiton_doc_store(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "app.agente_compara_calculation_result_storage.get_cleiton_doc_tmp_dir",
        lambda: str(tmp_path),
    )
    import app.agente_compara_calculation_result_storage as storage_mod

    monkeypatch.setattr(storage_mod, "_calc_storage_sweep_monotonic", time.monotonic())
    return tmp_path


@pytest.fixture
def load_calls(monkeypatch):
    import app.agente_compara_calculation_execution_service as execution
    import app.agente_compara_calculation_result_storage as storage

    calls = []
    real = storage.load_comparison_calculation_result

    def wrapped(**kwargs):
        calls.append(kwargs["comparison_id"])
        return real(**kwargs)

    monkeypatch.setattr(storage, "load_comparison_calculation_result", wrapped)
    monkeypatch.setattr(execution, "load_comparison_calculation_result", wrapped)
    return calls


@pytest.fixture
def side_effects(monkeypatch):
    effects = []
    monkeypatch.setattr(
        "app.run_agente_compara_comparison_chat._run_one_gemini_call",
        lambda *args, **kwargs: effects.append("gemini"),
    )
    monkeypatch.setattr(
        "app.services.cleiton_billable_ai_call.cleiton_governed_generate_content",
        lambda *args, **kwargs: effects.append("governed"),
    )
    import app.agente_compara_calculation_execution_service as execution

    monkeypatch.setattr(
        execution,
        "_apply_billing",
        lambda **kwargs: effects.append("billing"),
    )
    monkeypatch.setattr(
        execution,
        "calculate_comparison_in_memory",
        lambda *args, **kwargs: effects.append("calculation"),
    )
    return effects


def _persist(comparison_id, fingerprint, result, memory, **window):
    memory_meta = save_comparison_calculation_memory_payload(
        comparison_id=comparison_id,
        fingerprint=fingerprint,
        memory_payload=memory,
        **window,
    )
    result_meta = save_comparison_calculation_result(
        comparison_id=comparison_id,
        fingerprint=fingerprint,
        result=result,
        memory_storage_meta=memory_meta,
        **window,
    )
    result_meta["fingerprint"] = fingerprint
    memory_meta["fingerprint"] = fingerprint
    return result_meta, memory_meta


def _ready_context(monkeypatch, *, billing="applied", stale=False, fingerprint=None, comparison_id=None):
    comparison_id = comparison_id or uuid.uuid4().hex
    fingerprint = fingerprint or (uuid.uuid4().hex + uuid.uuid4().hex)
    result, memory = _official_pair(comparison_id)
    result_meta, memory_meta = _persist(comparison_id, fingerprint, result, memory)
    import app.agente_compara_calculation_execution_service as execution

    monkeypatch.setattr(
        execution,
        "_compute_current_fingerprint_for_state",
        lambda *args, **kwargs: fingerprint,
    )
    state = _state(
        comparison_id,
        result_meta=result_meta,
        memory_meta=memory_meta,
        billing=billing,
        stale=stale,
    )
    return {
        "comparison_id": comparison_id,
        "fingerprint": fingerprint,
        "result": result,
        "memory": memory,
        "result_meta": result_meta,
        "session": _session(state),
    }


def _component(publication, row_index, slot, code):
    query = next(item for item in publication["queries"] if item["query_type"] == "freight_components")
    for record in query["records"]:
        facts = record["facts"]
        if record["record_role"] == "detail" and facts["row_index"] == row_index and facts["carrier_slot"] == slot:
            return next(item for item in facts["components"] if item["code"] == code)
    raise AssertionError(f"componente ausente {row_index} {slot} {code}")


def _run(ready, plan):
    return execute_comparison_chat_analytical_plan(
        session_obj=ready["session"],
        usuario=_usuario(),
        plan=plan,
        comparison_id=ready["comparison_id"],
        operation_id=_OPERATION,
    )


def test_ready_comparison_executes_the_eight_queries_from_the_official_snapshot(
    storage_env, load_calls, side_effects, monkeypatch
):
    ready = _ready_context(monkeypatch)
    other_id = uuid.uuid4().hex
    other_fp = uuid.uuid4().hex + uuid.uuid4().hex
    other_result, other_memory = _official_pair(other_id)
    _persist(other_id, other_fp, other_result, other_memory)

    stored = load_comparison_calculation_result(
        storage_key=ready["result_meta"]["result_storage_key"],
        comparison_id=ready["comparison_id"],
        fingerprint=ready["fingerprint"],
        expected_checksum=ready["result_meta"]["result_checksum"],
    )
    rehydrated = _rehydrate_compact_result(stored, ready["memory"])
    assert artifact_checksum(stored) == ready["result_meta"]["result_checksum"]
    assert artifact_checksum(rehydrated) != ready["result_meta"]["result_checksum"]
    assert rehydrated["schema_version"] == 1
    assert stored["schema_version"] == 2

    first = _run(
        ready,
        _plan(
            [
                _query("summary", "official_summary"),
                _query("components", "freight_components"),
                _query("carriers", "carrier_comparison"),
                _query("gaps", "top_differences"),
            ]
        ),
    )
    second = _run(
        ready,
        _plan(
            [
                _query("ufs", "by_uf"),
                _query("weights", "by_weight"),
                _query("incomplete", "incomplete_freights"),
                _query("diagnostics", "non_calculation_diagnostics"),
            ]
        ),
    )
    assert first["status"] == "ok"
    assert second["status"] == "ok"
    assert set(first) == {"status", "error_code", "publication"}
    publications = [first["publication"], second["publication"]]
    executed = {query["query_type"]: query for publication in publications for query in publication["queries"]}
    assert set(executed) == {
        "official_summary",
        "freight_components",
        "carrier_comparison",
        "top_differences",
        "by_uf",
        "by_weight",
        "incomplete_freights",
        "non_calculation_diagnostics",
    }
    assert all(query["status"] == "ok" for query in executed.values())
    for publication in publications:
        publication_document_closed(publication)
        assert publication["comparison_id"] == ready["comparison_id"]
        assert publication["operation_id"] == _OPERATION
        assert publication["publication_version"] == "comparison_chat_publication_v1"
    taxes = _component(first["publication"], 1, 1, "taxes")
    assert taxes["amount_text"] == "0.00"
    assert taxes["amount_state"] == "zero"
    missing = _component(first["publication"], 1, 2, "weight_freight")
    assert missing["availability"] == "unavailable"
    assert missing["amount_state"] != "zero"
    assert missing["amount_text"] is None
    absent_taxes = _component(first["publication"], 1, 3, "taxes")
    assert absent_taxes["availability"] == "absent"
    assert absent_taxes["amount_state"] != "zero"
    carrier_summary = next(
        record["facts"]
        for record in executed["carrier_comparison"]["records"]
        if record["record_role"] == "summary"
    )
    assert {item["carrier_slot"] for item in carrier_summary["carriers"]} == {1, 2, 3}
    rendered = json.dumps(publications, ensure_ascii=False)
    for secret in (
        _SECRET_DOCUMENT,
        _SECRET_CARRIER,
        "cc_result_",
        "cc_memory_",
        "result_storage_key",
        "memory_storage_key",
        "document_number",
        "calculation_memory",
        other_id,
    ):
        assert secret not in rendered
    assert set(load_calls) == {ready["comparison_id"]}
    assert side_effects == []


def test_foreign_comparison_and_other_account_do_not_read_the_snapshot(
    storage_env, load_calls, side_effects, monkeypatch
):
    ready = _ready_context(monkeypatch)
    foreign = execute_comparison_chat_analytical_plan(
        session_obj=ready["session"],
        usuario=_usuario(),
        plan=_plan([_query("summary", "official_summary")]),
        comparison_id=uuid.uuid4().hex,
        operation_id=_OPERATION,
    )
    other_account = execute_comparison_chat_analytical_plan(
        session_obj=ready["session"],
        usuario=_usuario(_OTHER_CONTA, _OTHER_USUARIO, _OTHER_FRANQUIA),
        plan=_plan([_query("summary", "official_summary")]),
        comparison_id=ready["comparison_id"],
        operation_id=_OPERATION,
    )
    same_account_other_user = execute_comparison_chat_analytical_plan(
        session_obj=ready["session"],
        usuario=_usuario(_CONTA, _OTHER_USUARIO, _FRANQUIA),
        plan=_plan([_query("summary", "official_summary")]),
        comparison_id=ready["comparison_id"],
        operation_id=_OPERATION,
    )
    invalid_operation = execute_comparison_chat_analytical_plan(
        session_obj=ready["session"],
        usuario=_usuario(),
        plan=_plan([_query("summary", "official_summary")]),
        comparison_id=ready["comparison_id"],
        operation_id="operacao-invalida",
    )
    current = execute_comparison_chat_analytical_plan(
        session_obj=ready["session"],
        usuario=_usuario(),
        plan=_plan([_query("summary", "official_summary")]),
        operation_id=_OPERATION,
    )
    assert foreign["error_code"] == SNAPSHOT_NOT_AUTHORIZED
    assert other_account["error_code"] == SNAPSHOT_NOT_AUTHORIZED
    assert same_account_other_user["error_code"] == SNAPSHOT_NOT_AUTHORIZED
    assert invalid_operation["error_code"] == "invalid_operation_reference"
    assert current["status"] == "ok"
    assert current["publication"]["comparison_id"] == ready["comparison_id"]
    assert foreign["publication"] is None
    assert load_calls == [ready["comparison_id"], ready["comparison_id"]]
    assert side_effects == []


def test_stale_missing_and_expired_snapshots_are_refused(storage_env, load_calls, monkeypatch):
    stale = _ready_context(monkeypatch, stale=True)
    refused = _run(stale, _plan([_query("summary", "official_summary")]))
    assert refused["error_code"] == SNAPSHOT_OBSOLETE
    assert refused["publication"] is None

    drifted = _ready_context(monkeypatch)
    import app.agente_compara_calculation_execution_service as execution

    monkeypatch.setattr(
        execution,
        "_compute_current_fingerprint_for_state",
        lambda *args, **kwargs: "cd" * 32,
    )
    drifted_reply = _run(drifted, _plan([_query("summary", "official_summary")]))
    assert drifted_reply["error_code"] == SNAPSHOT_OBSOLETE

    pending = _ready_context(monkeypatch, billing="pending")
    pending_reply = _run(pending, _plan([_query("summary", "official_summary")]))
    assert pending_reply["error_code"] == SNAPSHOT_UNAVAILABLE

    missing = _ready_context(monkeypatch)
    missing["session"][AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY]["comparison_calculation"]["result_storage_key"] = ""
    missing_reply = _run(missing, _plan([_query("summary", "official_summary")]))
    assert missing_reply["error_code"] == SNAPSHOT_UNAVAILABLE

    expired_id = uuid.uuid4().hex
    expired_fp = uuid.uuid4().hex + uuid.uuid4().hex
    expired_result, expired_memory = _official_pair(expired_id)
    past = datetime.now(timezone.utc) - timedelta(hours=3)
    result_meta, memory_meta = _persist(
        expired_id,
        expired_fp,
        expired_result,
        expired_memory,
        created_at=past,
        expires_at=past + timedelta(minutes=5),
    )
    monkeypatch.setattr(
        execution,
        "_compute_current_fingerprint_for_state",
        lambda *args, **kwargs: expired_fp,
    )
    expired_state = _state(expired_id, result_meta=result_meta, memory_meta=memory_meta)
    expired_reply = execute_comparison_chat_analytical_plan(
        session_obj=_session(expired_state),
        usuario=_usuario(),
        plan=_plan([_query("summary", "official_summary")]),
        comparison_id=expired_id,
        operation_id=_OPERATION,
    )
    assert expired_reply["error_code"] == SNAPSHOT_UNAVAILABLE
    assert expired_reply["publication"] is None
    assert stale["comparison_id"] not in load_calls
    assert pending["comparison_id"] not in load_calls
    assert drifted["comparison_id"] not in load_calls


def test_checksum_divergence_does_not_rebuild_the_scope(storage_env, load_calls, monkeypatch):
    ready = _ready_context(monkeypatch)
    calc = ready["session"][AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY]["comparison_calculation"]
    calc["result_checksum"] = "ab" * 32
    divergent = _run(ready, _plan([_query("summary", "official_summary")]))
    assert divergent["error_code"] == SNAPSHOT_MISMATCH
    assert divergent["publication"] is None

    intact = _ready_context(monkeypatch)
    import app.agente_compara_chat_analytical_bridge as bridge

    executed = []
    monkeypatch.setattr(bridge, "artifact_checksum", lambda value: "ff" * 32)
    monkeypatch.setattr(bridge, "execute_analytical_plan", lambda *args, **kwargs: executed.append(True))
    rebuilt = _run(intact, _plan([_query("summary", "official_summary")]))
    assert rebuilt["error_code"] == SNAPSHOT_MISMATCH
    assert rebuilt["publication"] is None
    assert executed == []


def test_memory_from_another_comparison_is_rejected(storage_env, monkeypatch):
    comparison_id = uuid.uuid4().hex
    fingerprint = uuid.uuid4().hex + uuid.uuid4().hex
    result, memory = _official_pair(comparison_id)
    memory["items"]["t1:1"]["comparison_id"] = uuid.uuid4().hex
    result_meta, memory_meta = _persist(comparison_id, fingerprint, result, memory)
    import app.agente_compara_calculation_execution_service as execution

    monkeypatch.setattr(
        execution,
        "_compute_current_fingerprint_for_state",
        lambda *args, **kwargs: fingerprint,
    )
    reply = execute_comparison_chat_analytical_plan(
        session_obj=_session(_state(comparison_id, result_meta=result_meta, memory_meta=memory_meta)),
        usuario=_usuario(),
        plan=_plan([_query("summary", "official_summary")]),
        comparison_id=comparison_id,
        operation_id=_OPERATION,
    )
    assert reply["error_code"] == SNAPSHOT_MISMATCH
    assert reply["publication"] is None
    rendered = json.dumps(reply, ensure_ascii=False)
    assert memory["items"]["t1:1"]["comparison_id"] not in rendered


def test_invalid_plan_is_refused_without_publication(storage_env, monkeypatch):
    ready = _ready_context(monkeypatch)
    invalid = _run(ready, {"catalog_version": "outra", "queries": [_query("summary", "official_summary")]})
    unknown = _run(ready, _plan([_query("livre", "semantic_choice")]))
    tenant = _run(
        ready,
        {
            "catalog_version": CATALOG_VERSION,
            "conta_id": _OTHER_CONTA,
            "queries": [_query("summary", "official_summary")],
        },
    )
    assert invalid["error_code"] == "invalid_catalog_version"
    assert unknown["error_code"] == "invalid_query_type"
    assert tenant["error_code"] == "forbidden_expression"
    assert invalid["publication"] is None
    assert unknown["publication"] is None
    assert tenant["publication"] is None


def test_identity_authorization_does_not_accept_a_missing_stamp(storage_env):
    comparison_id = uuid.uuid4().hex
    state = {
        "comparison_id": comparison_id,
        "status": COMPARISON_STATUS_CALCULATION_READY,
        "current_step": STEP_CALCULATION_READY,
        "desired_table_count": 2,
        "tables": {
            "t1": {
                "table_id": "t1",
                "slot_number": 1,
                "status": "confirmed",
                "doc_ids": [],
                "temp_table_id": "tmp-t1",
                "carrier_name": "A",
                "confirmed": True,
                "error": None,
            },
            "t2": {
                "table_id": "t2",
                "slot_number": 2,
                "status": "confirmed",
                "doc_ids": [],
                "temp_table_id": "tmp-t2",
                "carrier_name": "B",
                "confirmed": True,
                "error": None,
            },
        },
    }
    refused = authorize_comparison_snapshot_identity(
        session_obj=_session(state),
        conta_id=_CONTA,
        usuario_id=_USUARIO,
        franquia_id=_FRANQUIA,
        comparison_id=comparison_id,
    )
    assert refused["error_code"] == SNAPSHOT_NOT_AUTHORIZED


def test_reply_path_does_not_activate_the_bridge(ctx, monkeypatch):
    del ctx
    activated = []
    import app.agente_compara_chat_analytical_bridge as bridge
    from app.run_agente_compara_comparison_chat import _reply_from_semantic_selection

    monkeypatch.setattr(
        bridge,
        "execute_authorized_analytical_plan",
        lambda **kwargs: activated.append(True),
    )
    monkeypatch.setattr(
        "app.run_agente_compara_comparison_chat.get_agente_compara_config",
        lambda: SimpleNamespace(
            question_max_chars=4000,
            chat_max_history=10,
            comparison_chat_question_max_chars=None,
            comparison_chat_history_max_items=None,
        ),
    )
    reply = chat_agente_compara_comparison_reply("", [], session_obj=_session())
    helper = inspect.getsource(_reply_from_semantic_selection)
    assert reply["error"] == "invalid_message"
    assert activated == []
    assert "execute_comparison_chat_analytical_plan" in helper
    assert "analytical_bridge" not in helper
    assert "cleiton_governed_generate_content" not in helper


def test_authentication_is_required_before_any_private_read(storage_env, load_calls, side_effects, monkeypatch):
    ready = _ready_context(monkeypatch)
    import app.agente_compara_chat_context_service as context_service

    state_reads = []
    real_state = context_service.get_comparison_state

    def track_state(session_obj):
        state_reads.append(session_obj)
        return real_state(session_obj)

    monkeypatch.setattr(context_service, "get_comparison_state", track_state)

    class _IndeterminateUser:
        id = _USUARIO
        conta_id = _CONTA
        franquia_id = _FRANQUIA

        @property
        def is_authenticated(self):
            raise RuntimeError("auth_backend_unavailable")

    plan = _plan([_query("summary", "official_summary")])
    refused_users = (
        _usuario(authenticated=False),
        SimpleNamespace(id=_USUARIO, conta_id=_CONTA, franquia_id=_FRANQUIA),
        _usuario(authenticated=None),
        _IndeterminateUser(),
        SimpleNamespace(id=_USUARIO, conta_id=_CONTA, franquia_id=_FRANQUIA, is_authenticated=1),
    )
    for usuario in refused_users:
        reply = execute_comparison_chat_analytical_plan(
            session_obj=ready["session"],
            usuario=usuario,
            plan=plan,
            comparison_id=ready["comparison_id"],
            operation_id=_OPERATION,
        )
        assert reply["status"] == "error"
        assert reply["error_code"] == SNAPSHOT_NOT_AUTHORIZED
        assert reply["publication"] is None
    assert load_calls == []
    assert state_reads == []
    assert side_effects == []

    published = _run(ready, plan)
    assert published["status"] == "ok"
    assert published["error_code"] is None
    assert published["publication"]["comparison_id"] == ready["comparison_id"]
    assert published["publication"]["publication_version"] == "comparison_chat_publication_v1"
    assert set(load_calls) == {ready["comparison_id"]}
    assert state_reads


def test_currency_requires_positive_fingerprint_proof(storage_env, load_calls, side_effects, monkeypatch):
    import app.agente_compara_calculation_execution_service as execution

    proven = _ready_context(monkeypatch)
    published = _run(proven, _plan([_query("summary", "official_summary")]))
    assert published["status"] == "ok"
    assert published["publication"]["queries"][0]["query_type"] == "official_summary"
    assert set(load_calls) == {proven["comparison_id"]}

    status_calls = []
    real_status = execution.get_comparison_calculation_status

    def track_status(**kwargs):
        status_calls.append(kwargs.get("comparison_id"))
        return real_status(**kwargs)

    monkeypatch.setattr(execution, "get_comparison_calculation_status", track_status)

    def _refuse(ready, fingerprint_fn):
        monkeypatch.setattr(execution, "_compute_current_fingerprint_for_state", fingerprint_fn)
        before_loads = len(load_calls)
        before_status = len(status_calls)
        reply = _run(ready, _plan([_query("summary", "official_summary")]))
        assert reply["status"] == "error"
        assert reply["error_code"] == SNAPSHOT_OBSOLETE
        assert reply["publication"] is None
        assert len(load_calls) == before_loads
        assert ready["comparison_id"] not in load_calls[before_loads:]
        assert len(status_calls) == before_status
        assert side_effects == []
        return reply

    exceptional = _ready_context(monkeypatch)
    assert artifact_checksum(exceptional["result"]) == exceptional["result_meta"]["result_checksum"]

    def _explode(*args, **kwargs):
        raise RuntimeError("fingerprint_backend_down")

    _refuse(exceptional, _explode)

    unproven = _ready_context(monkeypatch)
    assert artifact_checksum(unproven["result"]) == unproven["result_meta"]["result_checksum"]
    _refuse(unproven, lambda *args, **kwargs: None)

    obsolete = _ready_context(monkeypatch)
    assert artifact_checksum(obsolete["result"]) == obsolete["result_meta"]["result_checksum"]
    _refuse(obsolete, lambda *args, **kwargs: "ab" * 32)


def test_bridge_does_not_depend_on_the_frozen_durable_batch():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    source = (root / "app" / "agente_compara_chat_analytical_bridge.py").read_text(encoding="utf-8")
    runner = inspect.getsource(execute_comparison_chat_analytical_plan)
    for forbidden in (
        "agente_compara_chat_sqlalchemy_store",
        "agente_compara_chat_financial_read",
        "agente_compara_chat_operation_store",
        "generativeai",
        "google.genai",
        "calculate_comparison_in_memory",
        "cleiton_governed_generate_content",
    ):
        assert forbidden not in source
        assert forbidden not in runner
