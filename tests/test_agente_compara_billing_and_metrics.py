"""Billing operacional e métricas do AgenteCompara (isolados da Cleide)."""
from __future__ import annotations

from app.extensions import db
from app.models import CleitonBillingApropriacao, ProcessingEvent, utcnow_naive
from app.services.cleiton_upload_billing_service import (
    apropriar_billing_agente_compara_operational_flow,
    apropriar_billing_cleide_operational_flow,
)
from app.services.ia_metrics_service import (
    aggregate_agente_compara_processing_metrics_month,
    aggregate_cleide_processing_metrics_month,
    get_ia_dashboard_payload,
)
from tests.conftest import (
    seed_cleiton_cost_config,
    seed_conta_franquia_cliente,
    seed_sistema_interno,
    seed_usuario,
)


def test_apropriar_billing_agente_compara_creates_marker_with_agent(app):
    with app.app_context():
        seed_sistema_interno()
        seed_cleiton_cost_config()
        conta, franquia = seed_conta_franquia_cliente(slug="conta-ac-billing")
        user = seed_usuario(franquia.id, conta.id, email="ac-billing@test.com", categoria="pro")

        with app.test_request_context("/api/agente-compara/audit/upload", method="POST"):
            from flask import g

            g.identidade = {
                "conta_id": conta.id,
                "franquia_id": franquia.id,
                "usuario_id": user.id,
                "tipo_origem": "http_usuario",
                "origem_sistema": False,
            }
            out = apropriar_billing_agente_compara_operational_flow(
                flow_type="agente_compara_batch_upload",
                idempotency_key="agente-compara-batch-upload:sess:batch-1",
                rows_processed=10,
                processing_time_ms=200,
                status="success",
            )

        assert out["duplicado"] is False
        assert out["apropriado"] is True
        row = CleitonBillingApropriacao.query.filter_by(
            idempotency_key="agente-compara-batch-upload:sess:batch-1"
        ).one()
        assert row.agent == "agente_compara"
        assert row.flow_type == "agente_compara_batch_upload"


def test_billing_retry_same_idempotency_key_is_duplicate(app):
    with app.app_context():
        seed_sistema_interno()
        seed_cleiton_cost_config()
        conta, franquia = seed_conta_franquia_cliente(slug="conta-ac-billing-dup")
        user = seed_usuario(franquia.id, conta.id, email="ac-billing-dup@test.com", categoria="pro")

        with app.test_request_context("/api/agente-compara/audit/upload", method="POST"):
            from flask import g

            g.identidade = {
                "conta_id": conta.id,
                "franquia_id": franquia.id,
                "usuario_id": user.id,
                "tipo_origem": "http_usuario",
                "origem_sistema": False,
            }
            first = apropriar_billing_agente_compara_operational_flow(
                flow_type="agente_compara_coverage_upload",
                idempotency_key="agente-compara-coverage-upload:sess:v1",
                rows_processed=5,
                processing_time_ms=100,
                status="success",
            )
            second = apropriar_billing_agente_compara_operational_flow(
                flow_type="agente_compara_coverage_upload",
                idempotency_key="agente-compara-coverage-upload:sess:v1",
                rows_processed=5,
                processing_time_ms=100,
                status="success",
            )

        assert first["duplicado"] is False
        assert second["duplicado"] is True
        assert CleitonBillingApropriacao.query.count() == 1
        assert ProcessingEvent.query.filter_by(agent="agente_compara").count() == 1


def test_idempotency_key_prefix_separation_cleide_vs_agente_compara(app):
    with app.app_context():
        seed_sistema_interno()
        seed_cleiton_cost_config()
        conta, franquia = seed_conta_franquia_cliente(slug="conta-ac-key-sep")
        user = seed_usuario(franquia.id, conta.id, email="ac-key-sep@test.com", categoria="pro")

        with app.test_request_context("/", method="POST"):
            from flask import g

            g.identidade = {
                "conta_id": conta.id,
                "franquia_id": franquia.id,
                "usuario_id": user.id,
                "tipo_origem": "http_usuario",
                "origem_sistema": False,
            }
            cleide_out = apropriar_billing_cleide_operational_flow(
                flow_type="cleide_audit_batch_upload",
                idempotency_key="cleide-audit-batch-upload:sess:batch-1",
                rows_processed=3,
                processing_time_ms=50,
                status="success",
            )
            ac_out = apropriar_billing_agente_compara_operational_flow(
                flow_type="agente_compara_batch_upload",
                idempotency_key="agente-compara-batch-upload:sess:batch-1",
                rows_processed=3,
                processing_time_ms=50,
                status="success",
            )

        assert cleide_out["duplicado"] is False
        assert ac_out["duplicado"] is False
        assert CleitonBillingApropriacao.query.count() == 2
        agents = {r.agent for r in CleitonBillingApropriacao.query.all()}
        assert agents == {"cleide", "agente_compara"}


def test_aggregate_agente_compara_processing_only_counts_own_flows(app):
    with app.app_context():
        seed_sistema_interno()
        today = utcnow_naive()
        for agent, flow_type, rows in (
            ("agente_compara", "agente_compara_coverage_upload", 2),
            ("agente_compara", "agente_compara_batch_upload", 4),
            ("agente_compara", "agente_compara_batch_processed", 7),
            ("cleide", "cleide_audit_batch_upload", 99),
            ("agente_compara", "agente_compara_chat", 1),  # fora do consolidado
        ):
            db.session.add(
                ProcessingEvent(
                    occurred_at=today,
                    agent=agent,
                    flow_type=flow_type,
                    processing_type="non_llm",
                    rows_processed=rows,
                    processing_time_ms=100,
                    status="success",
                )
            )
        db.session.commit()

        ac = aggregate_agente_compara_processing_metrics_month(today.year, today.month)
        cleide = aggregate_cleide_processing_metrics_month(today.year, today.month)

        assert ac["total_processing_events_month"] == 3
        assert ac["total_rows_processed_month"] == 6  # coverage + batch_upload (reprocess não soma linhas)
        assert cleide["total_rows_processed_month"] == 99
        assert "agente_compara" not in str(cleide.get("agent") or "")


def test_cleide_aggregator_excludes_agente_compara_events(app):
    with app.app_context():
        seed_sistema_interno()
        today = utcnow_naive()
        db.session.add(
            ProcessingEvent(
                occurred_at=today,
                agent="agente_compara",
                flow_type="agente_compara_batch_upload",
                processing_type="non_llm",
                rows_processed=50,
                processing_time_ms=100,
                status="success",
            )
        )
        db.session.commit()
        cleide = aggregate_cleide_processing_metrics_month(today.year, today.month)
        assert cleide["total_processing_events_month"] == 0
        assert cleide["total_rows_processed_month"] == 0


def test_ia_dashboard_payload_has_separate_agente_compara_processing_block(app):
    with app.app_context():
        seed_sistema_interno()
        today = utcnow_naive()
        db.session.add_all(
            [
                ProcessingEvent(
                    occurred_at=today,
                    agent="cleide",
                    flow_type="cleide_audit_batch_upload",
                    processing_type="non_llm",
                    rows_processed=10,
                    processing_time_ms=100,
                    status="success",
                ),
                ProcessingEvent(
                    occurred_at=today,
                    agent="agente_compara",
                    flow_type="agente_compara_batch_upload",
                    processing_type="non_llm",
                    rows_processed=8,
                    processing_time_ms=120,
                    status="success",
                ),
            ]
        )
        db.session.commit()

        payload = get_ia_dashboard_payload(today.year, today.month)
        cleide_block = payload["cleide_processing"]
        ac_block = payload["agente_compara_processing"]
        assert cleide_block["total_rows_processed_month"] == 10
        assert ac_block["total_rows_processed_month"] == 8
        assert cleide_block is not ac_block
