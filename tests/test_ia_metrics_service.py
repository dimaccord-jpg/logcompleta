import io
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.extensions import db
from app.models import IaConsumoEvento, ProcessingEvent, utcnow_naive
from app.run_cleiton_processing_governance import cleiton_register_processing_event
from app.services.ia_metrics_service import (
    FLOW_TYPE_ONBOARDING_DISCOVERY,
    aggregate_cleide_processing_metrics_month,
    aggregate_month_metrics,
    aggregate_onboarding_discovery_metrics,
    get_ia_dashboard_payload,
)
from tests.conftest import seed_conta_franquia_cliente, seed_sistema_interno, seed_usuario


def _fake_discovery_response(payload: dict, *, usage_metadata=None):
    response = SimpleNamespace(text=json.dumps(payload))
    if usage_metadata is not None:
        response.usage_metadata = usage_metadata
    return response


def _fake_gemini_client(response, captured: dict | None = None):
    class _Models:
        def generate_content(self, *, model, contents, config=None):
            if captured is not None:
                captured["model"] = model
                captured["contents"] = contents
                captured["config"] = config
            return response

    class _Client:
        models = _Models()

    return _Client()


def _seed_ia_event(*, flow_type: str, total_tokens: int, agent: str = "julia") -> None:
    db.session.add(
        IaConsumoEvento(
            occurred_at=utcnow_naive(),
            provider="gemini",
            operation="generate_content",
            model="gemini-2.5-flash",
            agent=agent,
            flow_type=flow_type,
            api_key_label="test-key",
            status="success",
            input_tokens=total_tokens // 2,
            output_tokens=total_tokens // 2,
            total_tokens=total_tokens,
        )
    )
    db.session.commit()


def test_aggregate_month_metrics_excludes_onboarding_discovery(app):
    with app.app_context():
        _seed_ia_event(flow_type=FLOW_TYPE_ONBOARDING_DISCOVERY, total_tokens=5000, agent="cleiton")
        _seed_ia_event(flow_type="julia_chat", total_tokens=1200, agent="julia")
        _seed_ia_event(flow_type="roberto_chat_fretes", total_tokens=800, agent="roberto")

        today = utcnow_naive()
        payload = aggregate_month_metrics(today.year, today.month)

        assert payload["total_tokens_month"] == 2000
        assert payload["event_count_month"] == 2

        onboarding_count = IaConsumoEvento.query.filter_by(
            flow_type=FLOW_TYPE_ONBOARDING_DISCOVERY,
        ).count()
        assert onboarding_count == 1


def test_onboarding_discovery_tokens_visible_in_admin_payload(app):
    with app.app_context():
        _seed_ia_event(flow_type=FLOW_TYPE_ONBOARDING_DISCOVERY, total_tokens=5000, agent="cleiton")
        _seed_ia_event(flow_type="julia_chat", total_tokens=1200, agent="julia")

        today = utcnow_naive()
        operational = aggregate_month_metrics(today.year, today.month)
        onboarding = aggregate_onboarding_discovery_metrics(today.year, today.month)
        payload = get_ia_dashboard_payload(today.year, today.month)

        assert operational["total_tokens_month"] == 1200
        assert onboarding["total_tokens_month"] == 5000
        assert onboarding["event_count_month"] == 1
        assert onboarding["event_count_without_metrics_month"] == 0
        assert payload["onboarding_discovery_ia"]["total_tokens_month"] == 5000


def test_onboarding_discovery_admin_payload_shows_events_without_token_metrics(app):
    with app.app_context():
        db.session.add(
            IaConsumoEvento(
                occurred_at=utcnow_naive(),
                provider="gemini",
                operation="generate_content",
                model="gemini-2.5-flash",
                agent="cleiton",
                flow_type=FLOW_TYPE_ONBOARDING_DISCOVERY,
                api_key_label="test-key",
                status="success_no_metrics",
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
            )
        )
        db.session.commit()

        today = utcnow_naive()
        onboarding = aggregate_onboarding_discovery_metrics(today.year, today.month)

        assert onboarding["total_tokens_month"] == 0
        assert onboarding["event_count_month"] == 1
        assert onboarding["event_count_with_metrics_month"] == 0
        assert onboarding["event_count_without_metrics_month"] == 1
        assert onboarding["failure_event_count_month"] == 0


def test_onboarding_discovery_admin_payload_counts_failure_and_error_status(app):
    with app.app_context():
        db.session.add_all(
            [
                IaConsumoEvento(
                    occurred_at=utcnow_naive(),
                    provider="gemini",
                    operation="generate_content",
                    model="gemini-2.5-flash",
                    agent="cleiton",
                    flow_type=FLOW_TYPE_ONBOARDING_DISCOVERY,
                    api_key_label="test-key",
                    status="failure",
                    input_tokens=None,
                    output_tokens=None,
                    total_tokens=None,
                ),
                IaConsumoEvento(
                    occurred_at=utcnow_naive(),
                    provider="gemini",
                    operation="generate_content",
                    model="gemini-2.5-flash",
                    agent="cleiton",
                    flow_type=FLOW_TYPE_ONBOARDING_DISCOVERY,
                    api_key_label="test-key",
                    status="error",
                    input_tokens=None,
                    output_tokens=None,
                    total_tokens=None,
                ),
            ]
        )
        db.session.commit()

        today = utcnow_naive()
        onboarding = aggregate_onboarding_discovery_metrics(today.year, today.month)

        assert onboarding["event_count_month"] == 2
        assert onboarding["event_count_without_metrics_month"] == 2
        assert onboarding["failure_event_count_month"] == 2


def test_onboarding_discovery_fallback_without_gemini_still_generates_admin_event(app, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)

    with app.app_context():
        from app.run_cleiton_discovery import cleiton_discovery_reply

        result = cleiton_discovery_reply("quero reduzir custo", [])

        assert result["reply"]
        event = IaConsumoEvento.query.filter_by(
            flow_type=FLOW_TYPE_ONBOARDING_DISCOVERY,
            operation="onboarding_discovery_fallback",
        ).one()
        assert event.total_tokens is None
        assert event.status == "success_no_metrics"

        today = utcnow_naive()
        onboarding = aggregate_onboarding_discovery_metrics(today.year, today.month)
        assert onboarding["event_count_month"] == 1
        assert onboarding["event_count_without_metrics_month"] == 1


def test_onboarding_discovery_tokens_do_not_abate_franquia(app, monkeypatch):
    with app.app_context():
        from app.consumo_identidade import identidade_http_anonimo
        from app.models import Franquia
        from app.run_cleiton_gemini_governance import _persist_event

        seed_sistema_interno()
        conta, franquia = seed_conta_franquia_cliente(slug="conta-onboarding-tokens")
        franquia.consumo_acumulado = 0
        db.session.commit()
        consumo_antes = float(Franquia.query.get(franquia.id).consumo_acumulado or 0)

        monkeypatch.setattr(
            "app.run_cleiton_gemini_governance.resolve_identidade_para_persistencia",
            identidade_http_anonimo,
        )
        monkeypatch.setattr(
            "app.services.cleiton_franquia_operacional_service.aplicar_motor_apos_ia_consumo_evento",
            lambda _event_id: None,
        )

        _persist_event(
            operation="generate_content",
            model="gemini-2.5-flash",
            agent="cleiton",
            flow_type=FLOW_TYPE_ONBOARDING_DISCOVERY,
            api_key_label="test-key",
            status="success",
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
        )

        event = IaConsumoEvento.query.filter_by(flow_type=FLOW_TYPE_ONBOARDING_DISCOVERY).one()
        assert event.flow_type == FLOW_TYPE_ONBOARDING_DISCOVERY
        assert event.total_tokens == 150
        consumo_depois = float(Franquia.query.get(franquia.id).consumo_acumulado or 0)
        assert consumo_depois == consumo_antes


def test_onboarding_discovery_end_to_end_persists_usage_metadata_and_dashboard_metrics(app, monkeypatch):
    with app.app_context():
        from app.consumo_identidade import identidade_http_anonimo, set_consumo_identidade
        from app.run_cleiton_discovery import cleiton_discovery_reply

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        usage = type(
            "UsageMetadata",
            (),
            {
                "prompt_token_count": 100,
                "candidates_token_count": 50,
                "total_token_count": 150,
            },
        )()
        response = _fake_discovery_response(
            {
                "reply": "Posso ajudar a mapear seus custos logísticos.",
                "recommended_agent": None,
                "handoff": None,
                "confidence": "high",
                "reason": "teste ponta a ponta",
            },
            usage_metadata=usage,
        )
        monkeypatch.setattr("app.run_cleiton_discovery._get_client", lambda: _fake_gemini_client(response))
        monkeypatch.setattr("app.run_cleiton_discovery.auditoria_registrar", lambda *a, **k: None)
        monkeypatch.setattr(
            "app.services.cleiton_franquia_operacional_service.aplicar_motor_apos_ia_consumo_evento",
            lambda _event_id: None,
        )
        set_consumo_identidade(identidade_http_anonimo())
        result = cleiton_discovery_reply("Quero reduzir custos de frete", [])

        assert result["reply"]
        event = IaConsumoEvento.query.filter_by(flow_type=FLOW_TYPE_ONBOARDING_DISCOVERY).one()
        assert event.flow_type == FLOW_TYPE_ONBOARDING_DISCOVERY
        assert event.status == "success"
        assert event.input_tokens == 100
        assert event.output_tokens == 50
        assert event.total_tokens == 150

        today = utcnow_naive()
        onboarding = aggregate_onboarding_discovery_metrics(today.year, today.month)
        payload = get_ia_dashboard_payload(today.year, today.month)
        assert onboarding["total_tokens_month"] == 150
        assert onboarding["event_count_month"] == 1
        assert onboarding["event_count_with_metrics_month"] == 1
        assert payload["onboarding_tokens_month"] == 150
        assert payload["onboarding_discovery_ia"]["total_tokens_month"] == 150
        assert payload["total_internal_tokens_month"] == 150


def test_onboarding_discovery_end_to_end_without_usage_metadata_keeps_event_visible(app, monkeypatch):
    with app.app_context():
        from app.consumo_identidade import identidade_http_anonimo, set_consumo_identidade
        from app.run_cleiton_discovery import cleiton_discovery_reply

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        response = _fake_discovery_response(
            {
                "reply": "Consigo orientar seu diagnóstico inicial.",
                "recommended_agent": None,
                "handoff": None,
                "confidence": "medium",
                "reason": "sem metrica",
            }
        )
        monkeypatch.setattr("app.run_cleiton_discovery._get_client", lambda: _fake_gemini_client(response))
        monkeypatch.setattr("app.run_cleiton_discovery.auditoria_registrar", lambda *a, **k: None)
        monkeypatch.setattr(
            "app.services.cleiton_franquia_operacional_service.aplicar_motor_apos_ia_consumo_evento",
            lambda _event_id: None,
        )

        set_consumo_identidade(identidade_http_anonimo())
        result = cleiton_discovery_reply("Quero entender minha operação", [])

        assert result["reply"]
        event = IaConsumoEvento.query.filter_by(flow_type=FLOW_TYPE_ONBOARDING_DISCOVERY).one()
        assert event.status == "success_no_metrics"
        assert event.input_tokens is None
        assert event.output_tokens is None
        assert event.total_tokens is None

        today = utcnow_naive()
        onboarding = aggregate_onboarding_discovery_metrics(today.year, today.month)
        payload = get_ia_dashboard_payload(today.year, today.month)
        assert onboarding["event_count_month"] == 1
        assert onboarding["event_count_without_metrics_month"] == 1
        assert onboarding["total_tokens_month"] == 0
        assert payload["onboarding_discovery_ia"]["event_count_month"] == 1
        assert payload["onboarding_discovery_ia"]["event_count_without_metrics_month"] == 1


def test_onboarding_discovery_capabilities_document_regression_preserves_metrics(app, monkeypatch):
    with app.app_context():
        from app.consumo_identidade import identidade_http_anonimo, set_consumo_identidade
        from app.run_cleiton_discovery import cleiton_discovery_reply

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setattr("app.run_cleiton_discovery.auditoria_registrar", lambda *a, **k: None)
        monkeypatch.setattr(
            "app.services.cleiton_franquia_operacional_service.aplicar_motor_apos_ia_consumo_evento",
            lambda _event_id: None,
        )
        captured = {}
        usage = type(
            "UsageMetadata",
            (),
            {
                "prompt_token_count": 100,
                "candidates_token_count": 50,
                "total_token_count": 150,
            },
        )()

        response = _fake_discovery_response(
            {
                "reply": "Tenho caminhos para explorar sua operação.",
                "recommended_agent": None,
                "handoff": None,
                "confidence": "high",
                "reason": "regressao documento",
            },
            usage_metadata=usage,
        )
        monkeypatch.setattr("app.run_cleiton_discovery._get_client", lambda: _fake_gemini_client(response, captured))

        set_consumo_identidade(identidade_http_anonimo())
        result = cleiton_discovery_reply("Quero mapear as capacidades do produto", [])

        assert result["reply"]
        assert "DOCUMENTO DE CAPACIDADES" in captured["contents"]
        event = IaConsumoEvento.query.filter_by(flow_type=FLOW_TYPE_ONBOARDING_DISCOVERY).one()
        assert event.flow_type == FLOW_TYPE_ONBOARDING_DISCOVERY
        assert event.total_tokens == 150


def test_dashboard_end_to_end_separates_onboarding_operational_and_missing_metrics(app):
    with app.app_context():
        db.session.add_all(
            [
                IaConsumoEvento(
                    occurred_at=utcnow_naive(),
                    provider="gemini",
                    operation="generate_content",
                    model="gemini-2.5-flash",
                    agent="cleiton",
                    flow_type=FLOW_TYPE_ONBOARDING_DISCOVERY,
                    api_key_label="test-key",
                    status="success",
                    input_tokens=100,
                    output_tokens=50,
                    total_tokens=150,
                ),
                IaConsumoEvento(
                    occurred_at=utcnow_naive(),
                    provider="gemini",
                    operation="generate_content",
                    model="gemini-2.5-flash",
                    agent="cleiton",
                    flow_type=FLOW_TYPE_ONBOARDING_DISCOVERY,
                    api_key_label="test-key",
                    status="success_no_metrics",
                    input_tokens=None,
                    output_tokens=None,
                    total_tokens=None,
                ),
                IaConsumoEvento(
                    occurred_at=utcnow_naive(),
                    provider="gemini",
                    operation="generate_content",
                    model="gemini-2.5-flash",
                    agent="julia",
                    flow_type="julia_chat",
                    api_key_label="test-key",
                    status="success",
                    input_tokens=120,
                    output_tokens=80,
                    total_tokens=200,
                ),
            ]
        )
        db.session.commit()

        today = utcnow_naive()
        payload = get_ia_dashboard_payload(today.year, today.month)
        onboarding = payload["onboarding_discovery_ia"]

        assert payload["onboarding_tokens_month"] == 150
        assert payload["operational_tokens_month"] == 200
        assert payload["total_internal_tokens_month"] == 350
        assert onboarding["event_count_without_metrics_month"] == 1


def test_get_ia_dashboard_payload_separa_roberto_e_cleide(app):
    with app.app_context():
        seed_sistema_interno()
        conta, franquia = seed_conta_franquia_cliente(slug="conta-metrics-separadas")
        user = seed_usuario(
            franquia.id,
            conta.id,
            email="metrics@test.com",
            categoria="pro",
        )

        with app.test_request_context("/api/cleide/upload", method="POST"):
            from flask import g

            g.identidade = {
                "conta_id": conta.id,
                "franquia_id": franquia.id,
                "usuario_id": user.id,
                "tipo_origem": "http_usuario",
                "origem_sistema": False,
            }
            cleiton_register_processing_event(
                agent="roberto",
                flow_type="upload_bi",
                processing_type="non_llm",
                rows_processed=25,
                processing_time_ms=500,
                status="success",
                apply_operational_motor=False,
            )
            cleiton_register_processing_event(
                agent="cleide",
                flow_type="upload_fretes",
                processing_type="non_llm",
                rows_processed=10,
                processing_time_ms=250,
                status="success",
                apply_operational_motor=False,
            )

            # Evento da Cleide em outro flow_type nao entra no bloco principal.
            cleiton_register_processing_event(
                agent="cleide",
                flow_type="cleide_upload_fretes",
                processing_type="non_llm",
                rows_processed=999,
                processing_time_ms=1000,
                status="success",
                apply_operational_motor=False,
            )

        today = utcnow_naive()
        payload = get_ia_dashboard_payload(today.year, today.month)

        assert payload["total_processing_events_month"] == 1
        assert payload["total_rows_processed_month"] == 25
        assert payload["avg_processing_time_ms"] == 500.0

        cleide_block = payload.get("cleide_processing") or {}
        assert cleide_block["total_processing_events_month"] == 1
        assert cleide_block["total_rows_processed_month"] == 10
        assert cleide_block["avg_processing_time_ms"] == 250.0


def test_aggregate_cleide_processing_inclui_fluxos_auditoria(app):
    with app.app_context():
        seed_sistema_interno()
        from app.models import utcnow_naive

        today = utcnow_naive()
        for flow_type, rows in (
            ("upload_fretes", 10),
            ("cleide_audit_coverage_upload", 2),
            ("cleide_audit_batch_upload", 3),
            ("cleide_audit_batch_processed", 5),
        ):
            db.session.add(
                ProcessingEvent(
                    occurred_at=today,
                    agent="cleide",
                    flow_type=flow_type,
                    processing_type="non_llm",
                    rows_processed=rows,
                    processing_time_ms=100,
                    status="success",
                )
            )
        db.session.commit()

        agg = aggregate_cleide_processing_metrics_month(today.year, today.month)
        assert agg["total_processing_events_month"] == 4
        assert agg["total_rows_processed_month"] == 15


def test_get_ia_dashboard_payload_exibe_total_interno_geral(app):
    with app.app_context():
        _seed_ia_event(flow_type=FLOW_TYPE_ONBOARDING_DISCOVERY, total_tokens=5000, agent="cleiton")
        _seed_ia_event(flow_type="julia_chat", total_tokens=1200, agent="julia")
        _seed_ia_event(flow_type="roberto_chat_fretes", total_tokens=800, agent="roberto")

        today = utcnow_naive()
        payload = get_ia_dashboard_payload(today.year, today.month)

        assert payload["operational_tokens_month"] == 2000
        assert payload["onboarding_tokens_month"] == 5000
        assert payload["total_internal_tokens_month"] == 7000
        assert payload["onboarding_discovery_ia"]["total_tokens_month"] == 5000


def test_cleide_upload_registra_processing_event_proprio(app, monkeypatch, tmp_path):
    with app.app_context():
        app.config["SECRET_KEY"] = "test-secret-cleide-upload-event"
        seed_sistema_interno()
        conta, franquia = seed_conta_franquia_cliente(slug="conta-cleide-upload-event")
        user = seed_usuario(
            franquia.id,
            conta.id,
            email="cleide-event@test.com",
            categoria="pro",
        )

        monkeypatch.setattr(
            "app.cleide_upload_pipeline.get_cleide_config",
            lambda: type(
                "Cfg",
                (),
                {
                    "upload_max_file_size_bytes": 2 * 1024 * 1024,
                    "upload_ttl_minutes": 30,
                    "csv_delimiter_default": ",",
                    "structural_max_rows": 10_000,
                    "structural_max_columns": 120,
                    "analytics_max_rows": 10_000,
                    "analytics_group_limit": 25,
                },
            )(),
        )
        monkeypatch.setattr(
            "app.cleide_upload_store.get_cleide_upload_tmp_dir",
            lambda: str(tmp_path),
        )

        from app.cleide_upload_pipeline import process_cleide_upload

        csv_payload = (
            "transportadora,uf_origem,uf_destino,valor_frete,peso,data_emissao\n"
            "XP,SP,RJ,100,1,2026-01-01\n"
            "XP,SP,MG,200,2,2026-01-02\n"
        ).encode("utf-8")

        with app.test_request_context(
            "/api/cleide/upload",
            method="POST",
            data={"file": (io.BytesIO(csv_payload), "base.csv")},
            content_type="multipart/form-data",
        ):
            from flask import g

            g.identidade = {
                "conta_id": conta.id,
                "franquia_id": franquia.id,
                "usuario_id": user.id,
                "tipo_origem": "http_usuario",
                "origem_sistema": False,
            }
            response, status = process_cleide_upload()
            assert status == 200
            body = response.get_json()
            assert body["success"] is True

        events = (
            ProcessingEvent.query.filter_by(agent="cleide", flow_type="upload_fretes")
            .order_by(ProcessingEvent.id.desc())
            .all()
        )
        assert len(events) == 1
        assert events[0].rows_processed == 2
        assert events[0].status == "success"

