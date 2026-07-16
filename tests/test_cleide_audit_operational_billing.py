"""Governança operacional (não-LLM) dos fluxos da Auditoria Cleide."""
from __future__ import annotations

import pytest

import app.cleide_audit_doc_service as audit_doc_service
from app.extensions import db
from app.models import CleitonBillingApropriacao, Franquia, IaConsumoEvento, ProcessingEvent, utcnow_naive
from app.services.ia_metrics_service import (
    CLEIDE_PROCESSING_FLOW_TYPES,
    aggregate_cleide_processing_metrics_month,
    get_ia_dashboard_payload,
)
from tests.conftest import (
    seed_cleiton_cost_config,
    seed_conta_franquia_cliente,
    seed_sistema_interno,
    seed_usuario,
)
from tests.test_cleide_audit_temp_table import (
    _sample_audit_row,
    _sample_audit_xlsx,
    _sample_coverage_csv,
    _sample_pricing_payload,
    _setup_doc_env,
)


@pytest.fixture
def audit_billing_ctx(app, tmp_path, monkeypatch):
    app.config["SECRET_KEY"] = "test-secret-audit-billing"
    with app.app_context():
        seed_sistema_interno()
        seed_cleiton_cost_config()
        conta, franquia = seed_conta_franquia_cliente(slug="conta-audit-billing")
        user = seed_usuario(franquia.id, conta.id, email="audit-billing@test.com", categoria="pro")
        _setup_doc_env(monkeypatch, tmp_path)
        yield {
            "app": app,
            "conta": conta,
            "franquia": franquia,
            "user": user,
        }


def _identidade(ctx):
    return {
        "conta_id": ctx["conta"].id,
        "franquia_id": ctx["franquia"].id,
        "usuario_id": ctx["user"].id,
        "tipo_origem": "http_usuario",
        "origem_sistema": False,
    }


def _bind_session_to_temp_table(saved: dict) -> None:
    from flask import session

    session[audit_doc_service.CLEIDE_AUDIT_TEMP_TABLE_ID_SESSION_KEY] = saved["temp_table_id"]
    session[audit_doc_service.CLEIDE_AUDIT_TEMP_TABLE_SOURCE_DOCS_SESSION_KEY] = list(
        saved.get("source_documents") or ["doc-1"]
    )
    session[audit_doc_service.CLEIDE_AUDIT_DOC_IDS_SESSION_KEY] = list(
        saved.get("source_documents") or ["doc-1"]
    )


def _seed_temp_table(ctx):
    from flask import g, session

    g.identidade = _identidade(ctx)
    session[audit_doc_service.CLEIDE_AUDIT_DOC_IDS_SESSION_KEY] = ["doc-1"]
    audit_doc_service.mark_temp_table_processing(["doc-1"])
    saved = audit_doc_service.apply_temp_table_extraction_from_model_payload(
        _sample_pricing_payload(),
        source_doc_ids=["doc-1"],
    )
    _bind_session_to_temp_table(saved)
    return saved


def _run_coverage_upload(app, ctx, *, execution_id: str | None = None):
    headers = {"X-Execution-ID": execution_id} if execution_id else {}
    with app.test_request_context("/", method="POST", headers=headers):
        from flask import g

        g.identidade = _identidade(ctx)
        _seed_temp_table(ctx)
        return audit_doc_service.upload_coverage_table_from_file(
            display_name="coverage.csv",
            file_bytes=_sample_coverage_csv(),
            extension=".csv",
            user_scope=ctx["user"].id,
            franquia_scope=ctx["franquia"].id,
        )


def _run_audit_upload(app, ctx, *, rows: int = 1):
    from tests.test_cleide_audit_temp_table import _sample_audit_xlsx

    content = _sample_audit_xlsx(_sample_audit_row(valor_frete="100.50", peso="48"))
    if rows > 1:
        content = _sample_audit_xlsx(
            _sample_audit_row(valor_frete="100.50", peso="48"),
            _sample_audit_row(numero_documento="124", valor_frete="99.00", peso="48"),
        )
    with app.test_request_context("/", method="POST"):
        from flask import g

        g.identidade = _identidade(ctx)
        _seed_temp_table(ctx)
        return audit_doc_service.upload_audit_batch_from_file(
            display_name="auditado.xlsx",
            file_bytes=content,
            extension=".xlsx",
            user_scope=ctx["user"].id,
            franquia_scope=ctx["franquia"].id,
        )


def _prepare_audit_upload_and_run(app, ctx, *, execution_id: str | None = None):
    headers = {"X-Execution-ID": execution_id} if execution_id else {}
    with app.test_request_context("/", method="POST", headers=headers):
        from flask import g

        g.identidade = _identidade(ctx)
        saved = _seed_temp_table(ctx)
        audit_doc_service.upload_coverage_table_from_file(
            display_name="coverage.csv",
            file_bytes=_sample_coverage_csv(),
            extension=".csv",
            user_scope=ctx["user"].id,
            franquia_scope=ctx["franquia"].id,
        )
        audit_doc_service.upload_audit_batch_from_file(
            display_name="auditado.xlsx",
            file_bytes=_sample_audit_xlsx(_sample_audit_row(valor_frete="100.50", peso="48")),
            extension=".xlsx",
            user_scope=ctx["user"].id,
            franquia_scope=ctx["franquia"].id,
        )
        result = audit_doc_service.run_audit_batch_for_session(
            user_scope=ctx["user"].id,
            franquia_scope=ctx["franquia"].id,
        )
        return saved, result


def _mark_audit_batch_needs_reprocess(saved: dict) -> None:
    cfg = audit_doc_service.get_cleiton_doc_config()
    record = audit_doc_service.load_temp_table_record(saved["temp_table_id"], ttl_hours=cfg.upload_ttl_hours)
    assert record is not None
    audit_batch = dict(record.get("audit_batch") or {})
    audit_batch["needs_reprocess"] = True
    audit_batch["stale_reason"] = "test_reprocess"
    record["audit_batch"] = audit_batch
    audit_doc_service.save_temp_table_record(record)


def test_idempotency_keys_official_format():
    assert (
        audit_doc_service.cleide_audit_coverage_upload_idempotency_key("sess-1", "exec-1")
        == "cleide-audit-coverage-upload:sess-1:exec-1"
    )
    assert (
        audit_doc_service.cleide_audit_batch_upload_idempotency_key("sess-1", "batch-1")
        == "cleide-audit-batch-upload:sess-1:batch-1"
    )
    assert (
        audit_doc_service.cleide_audit_batch_run_idempotency_key("sess-1", "batch-1", "run-1")
        == "cleide-audit-batch-run:sess-1:batch-1:run-1"
    )


def test_coverage_upload_registra_processing_event_e_debita_franquia(audit_billing_ctx):
    app = audit_billing_ctx["app"]
    franquia_id = audit_billing_ctx["franquia"].id
    with app.app_context():
        before = db.session.get(Franquia, franquia_id).consumo_acumulado
        _run_coverage_upload(app, audit_billing_ctx, execution_id="cov-exec-1")

        events = ProcessingEvent.query.filter_by(
            agent="cleide",
            flow_type=audit_doc_service.CLEIDE_AUDIT_COVERAGE_UPLOAD_FLOW_TYPE,
        ).all()
        assert len(events) == 1
        assert events[0].rows_processed == 2
        assert events[0].status == "success"
        assert events[0].franquia_id == franquia_id
        assert db.session.get(Franquia, franquia_id).consumo_acumulado > before


def test_audit_upload_registra_evento_e_debita_franquia(audit_billing_ctx):
    app = audit_billing_ctx["app"]
    franquia_id = audit_billing_ctx["franquia"].id
    with app.app_context():
        before = db.session.get(Franquia, franquia_id).consumo_acumulado
        _run_audit_upload(app, audit_billing_ctx, rows=2)
        events = ProcessingEvent.query.filter_by(
            agent="cleide",
            flow_type=audit_doc_service.CLEIDE_AUDIT_BATCH_UPLOAD_FLOW_TYPE,
        ).all()
        assert len(events) == 1
        assert events[0].rows_processed == 2
        assert db.session.get(Franquia, franquia_id).consumo_acumulado > before


def test_primeiro_processar_auditoria_nao_debita_novamente(audit_billing_ctx):
    app = audit_billing_ctx["app"]
    franquia_id = audit_billing_ctx["franquia"].id
    with app.app_context():
        with app.test_request_context("/", method="POST", headers={"X-Execution-ID": "prep-run"}):
            from flask import g

            g.identidade = _identidade(audit_billing_ctx)
            saved = _seed_temp_table(audit_billing_ctx)
            audit_doc_service.upload_audit_batch_from_file(
                display_name="auditado.xlsx",
                file_bytes=_sample_audit_xlsx(_sample_audit_row(valor_frete="100.50", peso="48")),
                extension=".xlsx",
                user_scope=audit_billing_ctx["user"].id,
                franquia_scope=audit_billing_ctx["franquia"].id,
            )
            consumo_after_upload = db.session.get(Franquia, franquia_id).consumo_acumulado
            audit_doc_service.run_audit_batch_for_session(
                user_scope=audit_billing_ctx["user"].id,
                franquia_scope=audit_billing_ctx["franquia"].id,
            )

        assert ProcessingEvent.query.filter_by(
            flow_type=audit_doc_service.CLEIDE_AUDIT_BATCH_PROCESSED_FLOW_TYPE,
        ).count() == 0
        assert db.session.get(Franquia, franquia_id).consumo_acumulado == consumo_after_upload
        cfg = audit_doc_service.get_cleiton_doc_config()
        record = audit_doc_service.load_temp_table_record(saved["temp_table_id"], ttl_hours=cfg.upload_ttl_hours)
        assert record["audit_batch"]["status"] == audit_doc_service.AUDIT_BATCH_STATUS_PROCESSED


def test_novo_clique_processar_sem_needs_reprocess_nao_debita(audit_billing_ctx):
    app = audit_billing_ctx["app"]
    franquia_id = audit_billing_ctx["franquia"].id
    with app.app_context():
        saved, _ = _prepare_audit_upload_and_run(app, audit_billing_ctx, execution_id="first-run")
        consumo_after_first = db.session.get(Franquia, franquia_id).consumo_acumulado

        with app.test_request_context("/", method="POST", headers={"X-Execution-ID": "second-run"}):
            from flask import g

            g.identidade = _identidade(audit_billing_ctx)
            _bind_session_to_temp_table(saved)
            audit_doc_service.run_audit_batch_for_session(
                user_scope=audit_billing_ctx["user"].id,
                franquia_scope=audit_billing_ctx["franquia"].id,
            )

        assert ProcessingEvent.query.filter_by(
            flow_type=audit_doc_service.CLEIDE_AUDIT_BATCH_PROCESSED_FLOW_TYPE,
        ).count() == 0
        assert db.session.get(Franquia, franquia_id).consumo_acumulado == consumo_after_first


def test_reprocessamento_com_needs_reprocess_debita_novamente(audit_billing_ctx):
    app = audit_billing_ctx["app"]
    franquia_id = audit_billing_ctx["franquia"].id
    with app.app_context():
        saved, _ = _prepare_audit_upload_and_run(app, audit_billing_ctx, execution_id="first-run")
        consumo_after_first = db.session.get(Franquia, franquia_id).consumo_acumulado

        with app.test_request_context("/", method="POST", headers={"X-Execution-ID": "mark-reprocess"}):
            from flask import g

            g.identidade = _identidade(audit_billing_ctx)
            _mark_audit_batch_needs_reprocess(saved)

        with app.test_request_context("/", method="POST", headers={"X-Execution-ID": "reprocess-run"}):
            from flask import g

            g.identidade = _identidade(audit_billing_ctx)
            _bind_session_to_temp_table(saved)
            audit_doc_service.run_audit_batch_for_session(
                user_scope=audit_billing_ctx["user"].id,
                franquia_scope=audit_billing_ctx["franquia"].id,
            )

        events = ProcessingEvent.query.filter_by(
            flow_type=audit_doc_service.CLEIDE_AUDIT_BATCH_PROCESSED_FLOW_TYPE,
        ).all()
        assert len(events) == 1
        assert events[0].rows_processed == 1
        assert db.session.get(Franquia, franquia_id).consumo_acumulado > consumo_after_first


def test_dashboard_nao_soma_linhas_duplicadas(audit_billing_ctx):
    app = audit_billing_ctx["app"]
    with app.app_context():
        _prepare_audit_upload_and_run(app, audit_billing_ctx, execution_id="dash-run-1")
        today = utcnow_naive()
        payload = get_ia_dashboard_payload(today.year, today.month)
        cleide_block = payload["cleide_processing"]
        assert cleide_block["total_processing_events_month"] == 2
        assert cleide_block["total_rows_processed_month"] == 3
        assert cleide_block["avg_processing_time_ms"] is not None


def test_idempotencia_nao_duplica_cobranca(audit_billing_ctx):
    app = audit_billing_ctx["app"]
    with app.app_context():
        with app.test_request_context("/", method="POST", headers={"X-Execution-ID": "cov-idem-1"}):
            from flask import g

            g.identidade = _identidade(audit_billing_ctx)
            _seed_temp_table(audit_billing_ctx)
            audit_doc_service.upload_coverage_table_from_file(
                display_name="coverage.csv",
                file_bytes=_sample_coverage_csv(),
                extension=".csv",
                user_scope=audit_billing_ctx["user"].id,
                franquia_scope=audit_billing_ctx["franquia"].id,
            )
            audit_doc_service.upload_coverage_table_from_file(
                display_name="coverage.csv",
                file_bytes=_sample_coverage_csv(),
                extension=".csv",
                user_scope=audit_billing_ctx["user"].id,
                franquia_scope=audit_billing_ctx["franquia"].id,
            )
        events = ProcessingEvent.query.filter_by(
            flow_type=audit_doc_service.CLEIDE_AUDIT_COVERAGE_UPLOAD_FLOW_TYPE,
        ).all()
        assert len(events) == 1
        assert CleitonBillingApropriacao.query.filter(
            CleitonBillingApropriacao.idempotency_key.like("cleide-audit-coverage-upload:%")
        ).count() == 1


def test_falha_antes_de_persistir_nao_debita(audit_billing_ctx):
    app = audit_billing_ctx["app"]
    franquia_id = audit_billing_ctx["franquia"].id
    with app.app_context():
        before = db.session.get(Franquia, franquia_id).consumo_acumulado
        with app.test_request_context("/", method="POST"):
            from flask import g

            g.identidade = _identidade(audit_billing_ctx)
            _seed_temp_table(audit_billing_ctx)
            with pytest.raises(audit_doc_service.CleideAuditCoverageError):
                audit_doc_service.upload_coverage_table_from_file(
                    display_name="coverage.csv",
                    file_bytes=b"",
                    extension=".csv",
                    user_scope=audit_billing_ctx["user"].id,
                    franquia_scope=audit_billing_ctx["franquia"].id,
                )
        assert ProcessingEvent.query.filter_by(agent="cleide").count() == 0
        assert db.session.get(Franquia, franquia_id).consumo_acumulado == before


def test_roberto_continua_inalterado(audit_billing_ctx):
    app = audit_billing_ctx["app"]
    with app.app_context():
        from app.run_cleiton_processing_governance import cleiton_register_processing_event

        with app.test_request_context("/"):
            from flask import g

            g.identidade = _identidade(audit_billing_ctx)
            cleiton_register_processing_event(
                agent="roberto",
                flow_type="upload_bi",
                processing_type="non_llm",
                rows_processed=50,
                processing_time_ms=400,
                status="success",
                apply_operational_motor=False,
            )
        _prepare_audit_upload_and_run(app, audit_billing_ctx, execution_id="iso-run-1")
        payload = get_ia_dashboard_payload(utcnow_naive().year, utcnow_naive().month)
        assert payload["total_processing_events_month"] == 1
        assert payload["total_rows_processed_month"] == 50
        assert payload["cleide_processing"]["total_processing_events_month"] == 2


def test_tokens_gemini_ficam_fora_da_trilha_operacional(audit_billing_ctx):
    app = audit_billing_ctx["app"]
    with app.app_context():
        db.session.add(
            IaConsumoEvento(
                occurred_at=utcnow_naive(),
                provider="gemini",
                operation="generate_content",
                model="gemini-2.5-flash",
                agent="cleide_audit",
                flow_type=audit_doc_service.CLEIDE_AUDIT_TEMP_TABLE_EXTRACTION_FLOW_TYPE,
                api_key_label="test-key",
                status="success",
                input_tokens=100,
                output_tokens=50,
                total_tokens=150,
            )
        )
        db.session.commit()
        ia_before = IaConsumoEvento.query.count()
        _prepare_audit_upload_and_run(app, audit_billing_ctx, execution_id="gem-run-1")
        assert IaConsumoEvento.query.count() == ia_before
        assert ProcessingEvent.query.filter_by(agent="cleide").count() == 2


def test_temp_table_save_sem_recalculo_nao_debita(audit_billing_ctx):
    app = audit_billing_ctx["app"]
    franquia_id = audit_billing_ctx["franquia"].id
    with app.app_context():
        with app.test_request_context("/"):
            from flask import g

            g.identidade = _identidade(audit_billing_ctx)
            saved = _seed_temp_table(audit_billing_ctx)
            before = db.session.get(Franquia, franquia_id).consumo_acumulado
            audit_doc_service.save_temp_table_edit(
                {
                    "temp_table_id": saved["temp_table_id"],
                    "edit_target": {
                        "freight_tables": saved["freight_tables"],
                        "freight_routes": saved.get("freight_routes") or [],
                        "accessorial_fees": saved.get("accessorial_fees") or [],
                    },
                    "review_action": "save_and_advance",
                },
                user_scope=audit_billing_ctx["user"].id,
                franquia_scope=audit_billing_ctx["franquia"].id,
            )
        assert ProcessingEvent.query.filter_by(agent="cleide").count() == 0
        assert db.session.get(Franquia, franquia_id).consumo_acumulado == before


def test_cleide_processing_flow_types_whitelist():
    assert "upload_fretes" in CLEIDE_PROCESSING_FLOW_TYPES
    assert audit_doc_service.CLEIDE_AUDIT_COVERAGE_UPLOAD_FLOW_TYPE in CLEIDE_PROCESSING_FLOW_TYPES
    assert audit_doc_service.CLEIDE_AUDIT_BATCH_UPLOAD_FLOW_TYPE in CLEIDE_PROCESSING_FLOW_TYPES
    assert audit_doc_service.CLEIDE_AUDIT_BATCH_PROCESSED_FLOW_TYPE not in CLEIDE_PROCESSING_FLOW_TYPES
