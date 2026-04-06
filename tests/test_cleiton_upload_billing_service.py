from decimal import Decimal

from app.extensions import db
from app.models import CleitonBillingApropriacao, Franquia, ProcessingEvent
from app.services.cleiton_upload_billing_service import apropriar_billing_upload_roberto
from tests.conftest import (
    seed_cleiton_cost_config,
    seed_conta_franquia_cliente,
    seed_sistema_interno,
    seed_usuario,
)


def test_apropria_upload_roberto_converte_rows_ms_em_creditos(app):
    with app.app_context():
        seed_sistema_interno()
        seed_cleiton_cost_config()
        conta, franquia = seed_conta_franquia_cliente()
        user = seed_usuario(franquia.id, conta.id)

        with app.test_request_context("/api/roberto/upload", method="POST"):
            from flask import g

            g.identidade = {
                "conta_id": conta.id,
                "franquia_id": franquia.id,
                "usuario_id": user.id,
                "tipo_origem": "http_usuario",
                "origem_sistema": False,
            }
            out = apropriar_billing_upload_roberto(
                idempotency_key="upload-k1:success",
                rows_processed=100,
                processing_time_ms=1000,
                status="success",
            )

        assert out["duplicado"] is False
        assert out["apropriado"] is True
        assert out["creditos_apropriados"] == "2.000000"
        fr = db.session.get(Franquia, franquia.id)
        assert fr.consumo_acumulado == Decimal("2")
        assert fr.status == Franquia.STATUS_ACTIVE


def test_idempotencia_upload_roberto_nao_duplica_cobranca(app):
    with app.app_context():
        seed_sistema_interno()
        seed_cleiton_cost_config()
        conta, franquia = seed_conta_franquia_cliente()
        user = seed_usuario(franquia.id, conta.id)

        with app.test_request_context("/api/roberto/upload", method="POST"):
            from flask import g

            g.identidade = {
                "conta_id": conta.id,
                "franquia_id": franquia.id,
                "usuario_id": user.id,
                "tipo_origem": "http_usuario",
                "origem_sistema": False,
            }
            first = apropriar_billing_upload_roberto(
                idempotency_key="upload-k2:success",
                rows_processed=100,
                processing_time_ms=1000,
                status="success",
            )
            second = apropriar_billing_upload_roberto(
                idempotency_key="upload-k2:success",
                rows_processed=100,
                processing_time_ms=1000,
                status="success",
            )

        assert first["duplicado"] is False
        assert first["apropriado"] is True
        assert second["duplicado"] is True
        assert second["apropriado"] is True

        fr = db.session.get(Franquia, franquia.id)
        assert fr.consumo_acumulado == Decimal("2")
        assert ProcessingEvent.query.count() == 1
        assert CleitonBillingApropriacao.query.count() == 1


def test_upload_failure_registra_evento_sem_abater(app):
    with app.app_context():
        seed_sistema_interno()
        seed_cleiton_cost_config()
        conta, franquia = seed_conta_franquia_cliente()
        user = seed_usuario(franquia.id, conta.id)

        with app.test_request_context("/api/roberto/upload", method="POST"):
            from flask import g

            g.identidade = {
                "conta_id": conta.id,
                "franquia_id": franquia.id,
                "usuario_id": user.id,
                "tipo_origem": "http_usuario",
                "origem_sistema": False,
            }
            out = apropriar_billing_upload_roberto(
                idempotency_key="upload-k3:failure",
                rows_processed=0,
                processing_time_ms=120,
                status="failure",
                error_summary="falha teste",
            )

        assert out["duplicado"] is False
        assert out["apropriado"] is False
        fr = db.session.get(Franquia, franquia.id)
        assert fr.consumo_acumulado == Decimal("0")
