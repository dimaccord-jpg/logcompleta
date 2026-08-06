"""Smoke de upload documental do AgenteCompara (sessão isolada da Cleide)."""
from __future__ import annotations

import importlib
import io
import os
from types import SimpleNamespace

import pytest

from app.extensions import db
from sqlalchemy.exc import SQLAlchemyError
from app.agente_compara_doc_service import AGENTE_COMPARA_DOC_IDS_SESSION_KEY
from app.cleide_audit_doc_service import CLEIDE_AUDIT_DOC_IDS_SESSION_KEY
from app.models import CleitonBillingApropriacao, FunnelEvent, ProcessingEvent
from tests.conftest import seed_conta_franquia_cliente, seed_usuario
from app.services.agente_compara_config_service import (
    AgenteComparaConfig,
    DEFAULT_FALLBACK_MESSAGE,
)
from tests.cleiton_doc_fixtures import make_csv, patch_cleiton_doc_cfg, patch_cleiton_doc_store


def _default_ac_cfg(**overrides):
    defaults = {
        "chat_enabled": True,
        "upload_enabled": True,
        "chat_max_history": 10,
        "document_context_max_chars": 24000,
        "max_documents_considered": 3,
        "question_max_chars": 4000,
        "fallback_message": DEFAULT_FALLBACK_MESSAGE,
        "no_documents_behavior": "allow_guided",
        "show_documents_used": True,
        "no_hallucination_instruction_enabled": True,
        "audited_file_max_bytes": None,
        "audited_file_max_rows": 2000,
    }
    defaults.update(overrides)
    return AgenteComparaConfig(**defaults)


def _patch_ac_cfg(monkeypatch, **overrides):
    cfg = _default_ac_cfg(**overrides)
    for target in (
        "app.agente_compara_api_routes.get_agente_compara_config",
        "app.agente_compara_doc_service.get_agente_compara_config",
    ):
        monkeypatch.setattr(target, lambda _cfg=cfg: _cfg)
    return cfg


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


def _setup_doc_env(monkeypatch, tmp_path, **cfg_overrides):
    patch_cleiton_doc_store(tmp_path, monkeypatch)
    cfg = patch_cleiton_doc_cfg(monkeypatch, **cfg_overrides)
    monkeypatch.setattr("app.agente_compara_doc_service.get_cleiton_doc_config", lambda: cfg)
    monkeypatch.setattr("app.agente_compara_api_routes.get_cleiton_doc_config", lambda: cfg)
    _patch_ac_cfg(monkeypatch)
    return cfg


def _authorized(monkeypatch, web, *, authz=None):
    fake_user = SimpleNamespace(is_authenticated=True, conta_id=1, franquia_id=1)
    monkeypatch.setattr(web, "current_user", fake_user)
    monkeypatch.setattr("app.agente_compara_api_routes.current_user", fake_user)
    authz_payload = authz or {"permitido": True, "modo_operacao": "normal"}
    monkeypatch.setattr(
        web,
        "avaliar_autorizacao_operacao_por_franquia",
        lambda _u: authz_payload,
    )
    monkeypatch.setattr(
        "app.agente_compara_api_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _u: authz_payload,
    )


DEFAULT_CARRIER_NAME = "Transportadora Teste"


def _authorized_db_user(monkeypatch, web, *, email: str = "ac-upload-funnel@test.com"):
    conta, franquia = seed_conta_franquia_cliente(slug=f"conta-{email.split('@')[0]}")
    user = seed_usuario(franquia.id, conta.id, email=email)
    fake_user = SimpleNamespace(
        is_authenticated=True,
        id=user.id,
        conta_id=user.conta_id,
        franquia_id=user.franquia_id,
    )
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
    return user


def _upload(client, filename: str, content: bytes, mime: str = "text/csv", *, carrier_name: str = DEFAULT_CARRIER_NAME, **form_fields):
    data = {
        "file": (io.BytesIO(content), filename, mime),
        "carrier_name": carrier_name,
    }
    data.update(form_fields)
    return client.post(
        "/api/agente-compara/documents/upload",
        data=data,
        content_type="multipart/form-data",
    )


@pytest.fixture
def web_client(app, tmp_path, monkeypatch, ctx):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
        from app.agente_compara_api_routes import agente_compara_api_bp

        app.config["TESTING"] = True
        app.config["SECRET_KEY"] = "test-secret"
        if "agente_compara_api" not in app.blueprints:
            app.register_blueprint(agente_compara_api_bp)
    _authorized(
        monkeypatch,
        SimpleNamespace(
            app=app,
            current_user=None,
            avaliar_autorizacao_operacao_por_franquia=None,
        ),
    )
    return app.test_client()


def test_csv_upload_lands_in_agente_compara_session_key(web_client, app):
    content = make_csv([["col_a", "col_b"], ["1", "2"]])
    with web_client.session_transaction() as sess:
        sess[CLEIDE_AUDIT_DOC_IDS_SESSION_KEY] = ["cleide-keep"]

    resp = _upload(web_client, "dados.csv", content, "text/csv")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    doc_id = body["document"]["doc_id"]
    assert body["document"]["doc_type"] == "csv"

    with web_client.session_transaction() as sess:
        assert doc_id in (sess.get(AGENTE_COMPARA_DOC_IDS_SESSION_KEY) or [])
        assert CLEIDE_AUDIT_DOC_IDS_SESSION_KEY in sess
        assert sess[CLEIDE_AUDIT_DOC_IDS_SESSION_KEY] == ["cleide-keep"]
        assert doc_id not in (sess.get(CLEIDE_AUDIT_DOC_IDS_SESSION_KEY) or [])


def test_upload_without_carrier_name_returns_400(web_client, monkeypatch):
    monkeypatch.setattr(
        "app.run_agente_compara_temp_table.trigger_temp_table_extraction_for_session",
        lambda **_k: (_ for _ in ()).throw(AssertionError("Gemini não deve ser chamado")),
    )
    content = make_csv([["col_a", "col_b"], ["1", "2"]])
    resp = web_client.post(
        "/api/agente-compara/documents/upload",
        data={"file": (io.BytesIO(content), "dados.csv", "text/csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "agente_compara_carrier_name_required"


def test_upload_with_empty_carrier_name_returns_400(web_client, monkeypatch):
    monkeypatch.setattr(
        "app.run_agente_compara_temp_table.trigger_temp_table_extraction_for_session",
        lambda **_k: None,
    )
    content = make_csv([["col_a", "col_b"], ["1", "2"]])
    resp = _upload(web_client, "dados.csv", content, carrier_name="   ")
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "agente_compara_carrier_name_required"


def test_upload_stores_carrier_name_on_slot(web_client, monkeypatch):
    monkeypatch.setattr(
        "app.agente_compara_api_routes.trigger_temp_table_extraction_for_session",
        lambda **_k: None,
    )
    monkeypatch.setattr(
        "app.run_agente_compara_temp_table.trigger_temp_table_extraction_for_session",
        lambda **_k: None,
    )
    content = make_csv([["col_a", "col_b"], ["1", "2"]])
    resp = _upload(web_client, "dados.csv", content, carrier_name="Intercargo Transportes", slot="1")
    assert resp.status_code == 200
    with web_client.session_transaction() as sess:
        from app.agente_compara_comparison_state import get_comparison_state

        state = get_comparison_state(sess)
        table_1 = next(t for t in state["tables"].values() if t["slot_number"] == 1)
        assert table_1["carrier_name"] == "Intercargo Transportes"


def test_upload_same_carrier_name_on_two_slots_allowed(web_client, monkeypatch):
    monkeypatch.setattr(
        "app.agente_compara_api_routes.trigger_temp_table_extraction_for_session",
        lambda **_k: None,
    )
    monkeypatch.setattr(
        "app.run_agente_compara_temp_table.trigger_temp_table_extraction_for_session",
        lambda **_k: None,
    )
    content = make_csv([["a"], ["1"]])
    r1 = _upload(web_client, "t1.csv", content, carrier_name="Mesma Transportadora", slot="1")
    assert r1.status_code == 200
    table2_id = r1.get_json()["comparison"]["tables"][1]["table_id"]
    with web_client.session_transaction() as sess:
        from app.agente_compara_comparison_state import AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY, get_comparison_state
        from app.agente_compara_doc_service import get_temp_table_id

        state = get_comparison_state(sess)
        t1 = next(t for t in state["tables"].values() if t["slot_number"] == 1)
        t1["confirmed"] = True
        t1["status"] = "confirmed"
        t2 = next(t for t in state["tables"].values() if t["slot_number"] == 2)
        t2["status"] = "empty"
        state["current_step"] = "PREPARE_TABLE_2"
        state["active_table_id"] = table2_id
        sess[AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = state

    r2 = _upload(
        web_client,
        "t2.csv",
        content,
        carrier_name="Mesma Transportadora",
        slot="2",
        table_id=table2_id,
    )
    assert r2.status_code == 200
    with web_client.session_transaction() as sess:
        from app.agente_compara_comparison_state import get_comparison_state

        state = get_comparison_state(sess)
        names = {
            t["slot_number"]: t.get("carrier_name")
            for t in state["tables"].values()
            if t["slot_number"] in (1, 2)
        }
    assert names[1] == "Mesma Transportadora"
    assert names[2] == "Mesma Transportadora"


def test_detected_carrier_does_not_overwrite_manual_name(app, tmp_path, monkeypatch):
    from flask import session

    from app.agente_compara_comparison_state import (
        create_comparison,
        get_comparison_state,
        get_table_by_slot,
        persist_comparison_state,
    )
    from app.agente_compara_doc_service import get_temp_table_id, save_temp_table_record

    app.config["SECRET_KEY"] = "test-secret"
    with app.test_request_context():
        patch_cleiton_doc_store(tmp_path, monkeypatch)
        cfg = patch_cleiton_doc_cfg(monkeypatch)
        monkeypatch.setattr("app.agente_compara_doc_service.get_cleiton_doc_config", lambda: cfg)
        state = create_comparison()
        table_1 = get_table_by_slot(state, 1)
        table_1["carrier_name"] = "Intercargo"
        persist_comparison_state(state)
        record = {
            "temp_table_id": "tt-manual-carrier",
            "detected_carrier": "Intercargo Transportes Ltda.",
            "status": "needs_review",
            "source_documents": [],
            "freight_tables": [],
            "freight_routes": [],
            "accessorial_fees": [],
        }
        save_temp_table_record(record, table_id=table_1["table_id"])
        refreshed = get_table_by_slot(get_comparison_state(session), 1)
        assert refreshed["carrier_name"] == "Intercargo"


def test_get_temp_table_id_scoped_ignores_legacy_session_key(app):
    from flask import session

    from app.agente_compara_comparison_state import (
        AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY,
        create_comparison,
        get_table_by_slot,
    )
    from app.agente_compara_doc_service import (
        AGENTE_COMPARA_TEMP_TABLE_ID_SESSION_KEY,
        get_temp_table_id,
    )

    app.config["SECRET_KEY"] = "test-secret"
    with app.test_request_context():
        state = create_comparison()
        t1 = get_table_by_slot(state, 1)
        t2 = get_table_by_slot(state, 2)
        t1["temp_table_id"] = "tt-slot-1"
        session[AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = state
        session[AGENTE_COMPARA_TEMP_TABLE_ID_SESSION_KEY] = "tt-slot-1"
        assert get_temp_table_id(session, table_id=t2["table_id"]) is None
        assert get_temp_table_id(session, table_id=t1["table_id"]) == "tt-slot-1"


def test_carrier_integration_two_tables_independent_names(web_client, monkeypatch):
    import json

    import app.run_agente_compara_temp_table as temp_mod
    from app.agente_compara_doc_service import apply_temp_table_extraction_from_model_payload

    calls: dict = {}

    def _fake_run(source_doc_ids, **kwargs):
        calls["count"] = calls.get("count", 0) + 1
        return apply_temp_table_extraction_from_model_payload(
            json.loads(
                '{"freight_tables":[{"name":"T1","columns":["c"],"rows":[["1"]]}],'
                '"freight_routes":[],"accessorial_fees":[],"detected_carrier":"Extraída Gemini",'
                '"reading_alerts":[],"evidence_refs":[]}'
            ),
            source_doc_ids=source_doc_ids,
            table_id=kwargs.get("table_id"),
            comparison_id=kwargs.get("comparison_id"),
            slot_number=kwargs.get("slot_number"),
        )

    monkeypatch.setattr(temp_mod, "run_agente_compara_temp_table_extraction", _fake_run)
    monkeypatch.setattr(
        temp_mod,
        "build_agente_compara_document_context_for_chat",
        lambda *_a, **_k: {"has_documents": True, "context_block": "ctx", "gemini_file_parts": None},
    )

    content = make_csv([["a"], ["1"]])
    r1 = _upload(web_client, "t1.csv", content, carrier_name="Transportadora A", slot="1")
    assert r1.status_code == 200

    from app.agente_compara_comparison_state import (
        AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY,
        STEP_PREPARE_TABLE_2,
        get_comparison_state,
    )

    with web_client.session_transaction() as sess:
        state = get_comparison_state(sess)
        t1 = next(t for t in state["tables"].values() if t["slot_number"] == 1)
        t1["confirmed"] = True
        t1["status"] = "confirmed"
        t2 = next(t for t in state["tables"].values() if t["slot_number"] == 2)
        t2["status"] = "empty"
        state["current_step"] = STEP_PREPARE_TABLE_2
        table2_id = t2["table_id"]
        state["active_table_id"] = table2_id
        sess[AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = state

    r2 = _upload(
        web_client,
        "t2.csv",
        content,
        carrier_name="Transportadora B",
        slot="2",
        table_id=table2_id,
    )
    assert r2.status_code == 200
    with web_client.session_transaction() as sess:
        state = get_comparison_state(sess)
        names = {t["slot_number"]: t.get("carrier_name") for t in state["tables"].values()}
    assert names[1] == "Transportadora A"
    assert names[2] == "Transportadora B"
    assert calls.get("count") == 2


def test_new_upload_replaces_previous_slot_document_and_marks_retry_metadata(web_client, monkeypatch):
    monkeypatch.setattr(
        "app.agente_compara_api_routes.trigger_temp_table_extraction_for_session",
        lambda **_k: None,
    )
    monkeypatch.setattr(
        "app.run_agente_compara_temp_table.trigger_temp_table_extraction_for_session",
        lambda **_k: None,
    )
    first = _upload(web_client, "t1.csv", make_csv([["a"], ["1"]]), carrier_name="Carrier A", slot="1")
    assert first.status_code == 200
    first_body = first.get_json()
    first_doc_id = first_body["document"]["doc_id"]

    from app.agente_compara_doc_service import get_temp_table_id, save_temp_table_record
    from app.cleiton_doc_store import load_document_record

    with web_client.session_transaction() as sess:
        from app.agente_compara_comparison_state import AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY, get_comparison_state

        state = get_comparison_state(sess)
        table_1 = next(t for t in state["tables"].values() if t["slot_number"] == 1)
        active_temp_table_id_before_save = table_1["temp_table_id"]
        state_payload = dict(sess)
        with web_client.application.test_request_context():
            from flask import session

            session.update(state_payload)
            failed_temp_table = save_temp_table_record(
                {
                    "temp_table_id": active_temp_table_id_before_save,
                    "status": "failed",
                    "source_documents": [first_doc_id],
                    "reading_alerts": ["Gemini retornou timeout durante a extração técnica."],
                    "comparison_id": state["comparison_id"],
                    "table_id": table_1["table_id"],
                    "slot_number": 1,
                },
                table_id=table_1["table_id"],
            )
            failed_temp_table_id = failed_temp_table["temp_table_id"]
            for key, value in session.items():
                sess[key] = value
            for key in list(sess.keys()):
                if key not in session:
                    sess.pop(key, None)

    with web_client.session_transaction() as sess:
        from app.agente_compara_doc_service import load_temp_table_record
        from app.agente_compara_comparison_state import get_comparison_state

        state = get_comparison_state(sess)
        table_1 = next(t for t in state["tables"].values() if t["slot_number"] == 1)
        assert table_1["temp_table_id"] == failed_temp_table_id
        persisted = load_temp_table_record(failed_temp_table_id, ttl_hours=24)
        assert persisted is not None
        assert persisted["status"] == "failed"
        assert persisted["failure_origin"] == "platform"
        assert persisted["failure_code"] == "platform_temporary_failure"
        assert persisted["retryable"] is True

    second = _upload(web_client, "t2.csv", make_csv([["a"], ["2"]]), carrier_name="Carrier A", slot="1")
    assert second.status_code == 200
    second_body = second.get_json()
    second_doc_id = second_body["document"]["doc_id"]

    with web_client.session_transaction() as sess:
        from app.agente_compara_comparison_state import get_comparison_state

        state = get_comparison_state(sess)
        table_1 = next(t for t in state["tables"].values() if t["slot_number"] == 1)
        assert table_1["doc_ids"] == [second_doc_id]
        replaced = load_document_record(first_doc_id, ttl_hours=24)
        current = load_document_record(second_doc_id, ttl_hours=24)
        assert replaced["status"] == "replaced"
        assert replaced["error_code"] is None
        assert current["retry_of"] == first_doc_id
        assert current["original_document_id"] == first_doc_id
        assert current["retry_failure_code"] == "platform_temporary_failure"
        assert current["retry_failure_origin"] == "platform"
        assert current["is_technical_retry"] is True
        assert current["credit_disposition"] == "preserved"
        assert current["retryable"] is True
        assert current.get("error_code") is None
        assert current.get("prepared_context") is not replaced.get("prepared_context")

def test_technical_retry_eligible_does_not_consume_again(web_client, monkeypatch):
    from app.run_agente_compara_temp_table import trigger_temp_table_extraction_for_session

    motor_calls = []
    provider_calls = {"count": 0}

    monkeypatch.setattr(
        "app.services.cleiton_franquia_operacional_service.aplicar_motor_apos_processing_event",
        lambda evento_id: motor_calls.append(evento_id),
    )
    monkeypatch.setattr("app.run_agente_compara_temp_table._get_client", lambda: object())
    monkeypatch.setattr("app.run_agente_compara_temp_table._get_model_candidates", lambda: ["model-test"])

    def _fake_generate(*_args, **_kwargs):
        provider_calls["count"] += 1
        if provider_calls["count"] == 1:
            raise TimeoutError("provider timeout")
        return SimpleNamespace(
            text='{"status":"needs_review","freight_tables":[{"table_title":"Tabela A","columns":["origem"],"rows":[{"origem":"SP"}]}]}'
        )

    monkeypatch.setattr(
        "app.run_agente_compara_temp_table.cleiton_governed_generate_content",
        _fake_generate,
    )
    monkeypatch.setattr(
        "app.run_agente_compara_temp_table.build_agente_compara_document_context_for_chat",
        lambda *_a, **_k: {
            "has_documents": True,
            "context_block": "ctx",
            "gemini_file_parts": None,
        },
    )

    def _run_trigger_with_request_context(*, comparison_id: str, table_id: str, slot: int) -> None:
        with web_client.session_transaction() as sess:
            state_payload = dict(sess)
            with web_client.application.test_request_context("/api/agente-compara/audit/upload", method="POST"):
                from flask import session

                session.update(state_payload)
                trigger_temp_table_extraction_for_session(
                    session_obj=session,
                    comparison_id=comparison_id,
                    table_id=table_id,
                    slot=slot,
                )
                for key, value in session.items():
                    sess[key] = value
                for key in list(sess.keys()):
                    if key not in session:
                        sess.pop(key, None)

    first = _upload(web_client, "t1.csv", make_csv([["a"], ["1"]]), carrier_name="Carrier A", slot="1")
    assert first.status_code == 200
    first_doc_id = first.get_json()["document"]["doc_id"]
    comparison_id = first.get_json()["comparison"]["comparison_id"]
    table_id = first.get_json()["comparison"]["tables"][0]["table_id"]

    with web_client.application.app_context():
        first_phase_events = ProcessingEvent.query.filter_by(
            agent="agente_compara",
            flow_type="agente_compara_temp_table_extraction",
        ).order_by(ProcessingEvent.id.asc()).all()
        assert motor_calls == []
        assert CleitonBillingApropriacao.query.count() == 0
        assert len(first_phase_events) == 1

    with web_client.session_transaction() as sess:
        from app.agente_compara_doc_service import load_document_record, load_temp_table_record
        from app.agente_compara_comparison_state import get_comparison_state

        state = get_comparison_state(sess)
        table_1 = next(t for t in state["tables"].values() if t["slot_number"] == 1)
        failed_doc = load_document_record(first_doc_id, ttl_hours=24)
        failed_temp_table = load_temp_table_record(table_1["temp_table_id"], ttl_hours=24)

        assert table_1["doc_ids"] == [first_doc_id]
        assert failed_doc["credit_disposition"] == "preserved"
        assert failed_temp_table is not None
        assert failed_temp_table["status"] == "failed"
        assert failed_temp_table["credit_disposition"] == "preserved"

    second = _upload(
        web_client,
        "t2.csv",
        make_csv([["a"], ["2"]]),
        carrier_name="Carrier A",
        slot="1",
        comparison_id=comparison_id,
        table_id=table_id,
    )
    assert second.status_code == 200
    second_doc_id = second.get_json()["document"]["doc_id"]

    with web_client.application.app_context():
        retry_phase_events = ProcessingEvent.query.filter_by(
            agent="agente_compara",
            flow_type="agente_compara_temp_table_extraction",
        ).order_by(ProcessingEvent.id.asc()).all()
        assert motor_calls == []
        assert provider_calls["count"] == 2
        assert CleitonBillingApropriacao.query.count() == 0
        assert len(retry_phase_events) == 2

    with web_client.session_transaction() as sess:
        from app.agente_compara_doc_service import get_temp_table_id, load_document_record, load_temp_table_record
        from app.agente_compara_comparison_state import get_comparison_state

        state = get_comparison_state(sess)
        table_1 = next(t for t in state["tables"].values() if t["slot_number"] == 1)
        current = load_document_record(second_doc_id, ttl_hours=24)
        replaced = load_document_record(first_doc_id, ttl_hours=24)
        temp_table = load_temp_table_record(get_temp_table_id(sess, table_id=table_id), ttl_hours=24)

        assert table_1["doc_ids"] == [second_doc_id]
        assert replaced["status"] == "replaced"
        assert current["retry_of"] == first_doc_id
        assert current["is_technical_retry"] is True
        assert current["is_free_retry"] is True
        # Original technical failure preserved the credit; the free retry starts no new consumption.
        assert current["credit_disposition"] == "not_consumed"
        assert temp_table["status"] == "needs_review"
        assert temp_table["retry_of"] == first_doc_id
        assert temp_table["credit_disposition"] == "not_consumed"
        assert temp_table["comparison_id"] == comparison_id
        assert temp_table["slot_number"] == 1

    _run_trigger_with_request_context(
        comparison_id=comparison_id,
        table_id=table_id,
        slot=1,
    )

    with web_client.application.app_context():
        replay_phase_events = ProcessingEvent.query.filter_by(
            agent="agente_compara",
            flow_type="agente_compara_temp_table_extraction",
        ).order_by(ProcessingEvent.id.asc()).all()
        assert len(replay_phase_events) == 2
        assert motor_calls == []
        assert provider_calls["count"] == 2
        assert CleitonBillingApropriacao.query.count() == 0

    with web_client.session_transaction() as sess:
        from app.agente_compara_doc_service import get_temp_table_id, load_document_record, load_temp_table_record
        from app.agente_compara_comparison_state import get_comparison_state

        state = get_comparison_state(sess)
        table_1 = next(t for t in state["tables"].values() if t["slot_number"] == 1)
        current = load_document_record(second_doc_id, ttl_hours=24)
        temp_table = load_temp_table_record(get_temp_table_id(sess, table_id=table_id), ttl_hours=24)

        assert table_1["doc_ids"] == [second_doc_id]
        assert current["retry_of"] == first_doc_id
        assert current["is_free_retry"] is True
        assert current["credit_disposition"] == "not_consumed"
        assert temp_table is not None
        assert temp_table["retry_of"] == first_doc_id
        assert temp_table["credit_disposition"] == "not_consumed"

def _store_failed_temp_table_for_retry(web_client, *, doc_id: str, slot: int, comparison_id: str, table_id: str, reading_alerts: list[str] | None = None):
    from app.agente_compara_doc_service import get_temp_table_id, save_temp_table_record
    from app.agente_compara_comparison_state import AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY

    with web_client.session_transaction() as sess:
        state_payload = dict(sess)
        with web_client.application.test_request_context():
            from flask import session

            session.update(state_payload)
            save_temp_table_record(
                {
                    "temp_table_id": f"tt-failed-retry-{slot}",
                    "status": "failed",
                    "source_documents": [doc_id],
                    "reading_alerts": reading_alerts or ["Gemini retornou timeout durante a extração técnica."],
                    "comparison_id": comparison_id,
                    "table_id": table_id,
                    "slot_number": slot,
                },
                table_id=table_id,
            )
            for key, value in session.items():
                sess[key] = value
            for key in list(sess.keys()):
                if key not in session:
                    sess.pop(key, None)


def test_new_upload_does_not_mark_retry_when_previous_document_succeeded(web_client, monkeypatch):
    monkeypatch.setattr("app.agente_compara_api_routes.trigger_temp_table_extraction_for_session", lambda **_k: None)
    monkeypatch.setattr("app.run_agente_compara_temp_table.trigger_temp_table_extraction_for_session", lambda **_k: None)
    first = _upload(web_client, "ok.csv", make_csv([["a"], ["1"]]), carrier_name="Carrier A", slot="1")
    assert first.status_code == 200
    first_doc_id = first.get_json()["document"]["doc_id"]
    second = _upload(web_client, "novo.csv", make_csv([["a"], ["2"]]), carrier_name="Carrier A", slot="1")
    assert second.status_code == 200
    second_doc_id = second.get_json()["document"]["doc_id"]

    from app.cleiton_doc_store import load_document_record

    current = load_document_record(second_doc_id, ttl_hours=24)
    replaced = load_document_record(first_doc_id, ttl_hours=24)
    assert replaced["status"] == "replaced"
    assert current.get("retry_of") is None
    assert current.get("is_technical_retry") in (None, False)


def test_new_upload_does_not_mark_retry_for_non_retryable_document_failure(web_client, monkeypatch):
    monkeypatch.setattr("app.agente_compara_api_routes.trigger_temp_table_extraction_for_session", lambda **_k: None)
    monkeypatch.setattr("app.run_agente_compara_temp_table.trigger_temp_table_extraction_for_session", lambda **_k: None)
    first = _upload(web_client, "doc.csv", make_csv([["a"], ["1"]]), carrier_name="Carrier A", slot="1")
    assert first.status_code == 200
    body = first.get_json()
    first_doc_id = body["document"]["doc_id"]
    table_1 = body["comparison"]["tables"][0]
    _store_failed_temp_table_for_retry(
        web_client,
        doc_id=first_doc_id,
        slot=1,
        comparison_id=body["comparison"]["comparison_id"],
        table_id=table_1["table_id"],
        reading_alerts=["Arquivo vazio e formato incompatível para extração."],
    )
    second = _upload(web_client, "novo.csv", make_csv([["a"], ["2"]]), carrier_name="Carrier A", slot="1")
    assert second.status_code == 200

    from app.cleiton_doc_store import load_document_record

    current = load_document_record(second.get_json()["document"]["doc_id"], ttl_hours=24)
    assert current.get("retry_of") is None
    assert current.get("retry_failure_code") is None


def test_new_upload_does_not_mark_retry_for_different_slot(web_client, monkeypatch):
    monkeypatch.setattr("app.agente_compara_api_routes.trigger_temp_table_extraction_for_session", lambda **_k: None)
    monkeypatch.setattr("app.run_agente_compara_temp_table.trigger_temp_table_extraction_for_session", lambda **_k: None)
    first = _upload(web_client, "slot1.csv", make_csv([["a"], ["1"]]), carrier_name="Carrier A", slot="1")
    assert first.status_code == 200
    body = first.get_json()
    table_1 = body["comparison"]["tables"][0]
    first_doc_id = body["document"]["doc_id"]
    _store_failed_temp_table_for_retry(
        web_client,
        doc_id=first_doc_id,
        slot=1,
        comparison_id=body["comparison"]["comparison_id"],
        table_id=table_1["table_id"],
    )
    table2_id = body["comparison"]["tables"][1]["table_id"]
    with web_client.session_transaction() as sess:
        from app.agente_compara_comparison_state import AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY, get_comparison_state

        state = get_comparison_state(sess)
        t1 = next(t for t in state["tables"].values() if t["slot_number"] == 1)
        t1["confirmed"] = True
        t1["status"] = "confirmed"
        t2 = next(t for t in state["tables"].values() if t["slot_number"] == 2)
        t2["status"] = "empty"
        state["current_step"] = "PREPARE_TABLE_2"
        state["active_table_id"] = table2_id
        sess[AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = state

    second = _upload(web_client, "slot2.csv", make_csv([["a"], ["2"]]), carrier_name="Carrier B", slot="2", table_id=table2_id)
    assert second.status_code == 200

    from app.cleiton_doc_store import load_document_record

    current = load_document_record(second.get_json()["document"]["doc_id"], ttl_hours=24)
    assert current.get("retry_of") is None
    assert current.get("original_document_id") is None


def test_new_upload_does_not_mark_retry_for_different_comparison(web_client, monkeypatch):
    monkeypatch.setattr("app.agente_compara_api_routes.trigger_temp_table_extraction_for_session", lambda **_k: None)
    monkeypatch.setattr("app.run_agente_compara_temp_table.trigger_temp_table_extraction_for_session", lambda **_k: None)
    first = _upload(web_client, "base.csv", make_csv([["a"], ["1"]]), carrier_name="Carrier A", slot="1")
    assert first.status_code == 200
    body = first.get_json()
    table_1 = body["comparison"]["tables"][0]
    first_doc_id = body["document"]["doc_id"]
    _store_failed_temp_table_for_retry(
        web_client,
        doc_id=first_doc_id,
        slot=1,
        comparison_id=body["comparison"]["comparison_id"],
        table_id=table_1["table_id"],
    )

    from app.agente_compara_comparison_state import clear_comparison_state, create_comparison

    with web_client.session_transaction() as sess:
        sess["agente_compara_doc_ids"] = []
        sess["agente_compara_temp_table_id"] = None
        sess["agente_compara_temp_table_ids_by_table"] = {}
        sess["agente_compara_temp_table_source_docs_by_table"] = {}
        clear_comparison_state(session_obj=sess)
        create_comparison(session_obj=sess)

    second = _upload(web_client, "nova.csv", make_csv([["a"], ["2"]]), carrier_name="Carrier A", slot="1")
    assert second.status_code == 200

    from app.cleiton_doc_store import load_document_record

    current = load_document_record(second.get_json()["document"]["doc_id"], ttl_hours=24)
    assert current.get("retry_of") is None
    assert current.get("retry_failure_code") is None


def test_upload_success_creates_funnel_event_and_response_flag(web_client, app, monkeypatch):
    web = _load_web_module()
    with app.app_context():
        user = _authorized_db_user(monkeypatch, web, email="ac-upload-created@test.com")
        before = FunnelEvent.query.count()
    content = make_csv([["col_a", "col_b"], ["1", "2"]])
    resp = _upload(web_client, "dados.csv", content, slot="1")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["funnel_event"]["allow_meta_pixel"] is True
    assert body["funnel_event"]["event_name"] == "file_uploaded"

    with app.app_context():
        db.session.remove()
        assert FunnelEvent.query.count() == before + 1
        event = FunnelEvent.query.order_by(FunnelEvent.id.desc()).first()
        assert event.source == "agente_compara"
        assert event.user_id == user.id
        assert event.conta_id == user.conta_id
        assert event.franquia_id == user.franquia_id
        assert event.document_id == body["document"]["doc_id"]
        assert event.comparison_id == body["comparison"]["comparison_id"]


def test_upload_replay_omits_funnel_event_and_does_not_duplicate_row(web_client, app, monkeypatch):
    web = _load_web_module()
    with app.app_context():
        _authorized_db_user(monkeypatch, web, email="ac-upload-replay@test.com")
        before = FunnelEvent.query.count()

    monkeypatch.setattr("app.agente_compara_api_routes.trigger_temp_table_extraction_for_session", lambda **_k: None)
    content = make_csv([["col_a", "col_b"], ["1", "2"]])

    first = _upload(web_client, "dados.csv", content, slot="1")
    assert first.status_code == 200
    first_body = first.get_json()
    assert first_body["funnel_event"]["allow_meta_pixel"] is True

    with app.app_context():
        db.session.remove()
        persisted = FunnelEvent.query.order_by(FunnelEvent.id.desc()).first()
        assert persisted is not None
        assert persisted.idempotency_key

        from app.agente_compara_api_routes import record_funnel_event

        replay_result = record_funnel_event(
            event_name=persisted.event_name,
            source=persisted.source,
            user_id=persisted.user_id,
            conta_id=persisted.conta_id,
            franquia_id=persisted.franquia_id,
            idempotency_key=persisted.idempotency_key,
            document_id=persisted.document_id,
            comparison_id=persisted.comparison_id,
            execution_id=persisted.execution_id,
            correlation_id=persisted.correlation_id,
            metadata_json=persisted.metadata_json,
        )

        replay_payload = {
            "ok": True,
            "document": first_body["document"],
            "comparison": first_body["comparison"],
        }
        if replay_result.get("created") is True:
            replay_payload["funnel_event"] = {
                "event_name": "file_uploaded",
                "source": "agente_compara",
                "allow_meta_pixel": True,
                "is_first_audit": False,
            }

        assert replay_result["created"] is False
        db.session.remove()
        same_key_rows = FunnelEvent.query.filter_by(idempotency_key=persisted.idempotency_key).count()
        assert FunnelEvent.query.count() == before + 1
        assert same_key_rows == 1
        assert "funnel_event" not in replay_payload


def test_upload_funnel_failure_does_not_break_success_response(web_client, app, monkeypatch):
    web = _load_web_module()
    with app.app_context():
        _authorized_db_user(monkeypatch, web, email="ac-upload-analytics-fail@test.com")
        before = FunnelEvent.query.count()
    monkeypatch.setattr("app.agente_compara_api_routes.trigger_temp_table_extraction_for_session", lambda **_k: None)
    monkeypatch.setattr(
        "app.agente_compara_api_routes.record_funnel_event",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("boom")),
    )
    content = make_csv([["col_a", "col_b"], ["1", "2"]])
    resp = _upload(web_client, "dados.csv", content, slot="1")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert "funnel_event" not in body

    with app.app_context():
        assert FunnelEvent.query.count() == before


def test_upload_unexpected_funnel_failure_rolls_back_and_preserves_success_response(web_client, app, monkeypatch):
    web = _load_web_module()
    with app.app_context():
        _authorized_db_user(monkeypatch, web, email="ac-upload-runtime-fail@test.com")
        before = FunnelEvent.query.count()

    calls = {"rollback": 0}
    real_rollback = db.session.rollback

    def tracked_rollback():
        calls["rollback"] += 1
        return real_rollback()

    monkeypatch.setattr("app.agente_compara_api_routes.trigger_temp_table_extraction_for_session", lambda **_k: None)
    monkeypatch.setattr(
        "app.agente_compara_api_routes.record_funnel_event",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("unexpected analytics failure")),
    )
    monkeypatch.setattr(db.session, "rollback", tracked_rollback)

    resp = _upload(web_client, "dados.csv", make_csv([["col_a", "col_b"], ["1", "2"]]), slot="1")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert "funnel_event" not in body
    assert "allow_meta_pixel" not in body
    assert calls["rollback"] == 1

    with app.app_context():
        db.session.remove()
        assert FunnelEvent.query.count() == before


def test_upload_commit_failure_rolls_back_and_omits_pixel_authorization(web_client, app, monkeypatch):
    web = _load_web_module()
    with app.app_context():
        _authorized_db_user(monkeypatch, web, email="ac-upload-commit-fail@test.com")
        before = FunnelEvent.query.count()

    calls = {"commit": 0, "rollback": 0}
    real_rollback = db.session.rollback

    def fail_commit():
        calls["commit"] += 1
        real_rollback()
        raise SQLAlchemyError("commit failed")

    def tracked_rollback():
        calls["rollback"] += 1
        return real_rollback()

    monkeypatch.setattr("app.agente_compara_api_routes.trigger_temp_table_extraction_for_session", lambda **_k: None)
    monkeypatch.setattr("app.agente_compara_api_routes.record_funnel_event", lambda **_kwargs: {"created": True})
    monkeypatch.setattr(db.session, "commit", fail_commit)
    monkeypatch.setattr(db.session, "rollback", tracked_rollback)

    resp = _upload(web_client, "dados.csv", make_csv([["col_a", "col_b"], ["1", "2"]]), slot="1")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert "funnel_event" not in body
    assert calls["commit"] == 1
    assert calls["rollback"] == 1

    with app.app_context():
        db.session.remove()
        assert FunnelEvent.query.count() == before
