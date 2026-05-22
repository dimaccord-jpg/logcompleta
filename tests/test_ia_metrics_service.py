import io

from app.models import ProcessingEvent, utcnow_naive
from app.run_cleiton_processing_governance import cleiton_register_processing_event
from app.services.ia_metrics_service import get_ia_dashboard_payload
from tests.conftest import seed_conta_franquia_cliente, seed_sistema_interno, seed_usuario


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

