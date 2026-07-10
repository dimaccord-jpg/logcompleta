from __future__ import annotations

import copy
from unittest.mock import MagicMock

import pytest
from flask import session

import app.cleide_audit_correction_service as correction_svc
import app.cleide_audit_doc_service as audit_svc
from tests.cleiton_doc_fixtures import patch_cleiton_doc_cfg, patch_cleiton_doc_store


@pytest.fixture
def session_app(app, tmp_path, monkeypatch, ctx):
    patch_cleiton_doc_store(tmp_path, monkeypatch)
    cfg = patch_cleiton_doc_cfg(monkeypatch)
    monkeypatch.setattr("app.cleide_audit_doc_service.get_cleiton_doc_config", lambda: cfg)
    monkeypatch.setattr("app.services.cleiton_doc_config_service.get_cleiton_doc_config", lambda: cfg)
    app.config["SECRET_KEY"] = "test-secret"
    return app


def _audit_row(row_index: int, city: str, region: str, *, charged: str = "10,00") -> dict:
    return {
        "row_index": row_index,
        "document_number": f"NF-{row_index}",
        "destination_uf": "SP",
        "destination_city": city,
        "audited_weight": "10",
        "charged_freight": charged,
        "carrier": "Transportadora",
        "origin_uf": "PR",
        "issue_date": "2026-07-07",
        "_coverage_region": region,
    }


def _record(*, extra_candidate: bool = False) -> dict:
    columns = ["Região de frete", "RAIO", "Até 50 kg"]
    rows = [
        {"Região de frete": "SUL", "RAIO": "CAPITAL", "Até 50 kg": "10,00"},
        {"Região de frete": "SUL", "RAIO": "INTERIOR", "Até 50 kg": "20,00"},
    ]
    if extra_candidate:
        columns = ["Região de frete", "RAIO", "ZONA", "Até 50 kg"]
        rows = [
            {"Região de frete": "SUL", "RAIO": "CAPITAL", "ZONA": "CAPITAL", "Até 50 kg": "10,00"},
            {"Região de frete": "SUL", "RAIO": "INTERIOR", "ZONA": "INTERIOR", "Até 50 kg": "20,00"},
        ]
    normalized_rows = [
        _audit_row(1, "Sao Paulo", "CAPITAL", charged="10,00"),
        _audit_row(2, "Campinas", "INTERIOR", charged="20,00"),
    ]
    return {
        "temp_table_id": "tt-test",
        "status": "needs_review",
        "version_marker": audit_svc.TEMP_TABLE_VERSION_MARKER,
        "edit_version": 0,
        "created_at": "2026-07-07T19:00:00+00:00",
        "updated_at": "2026-07-07T19:00:00+00:00",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "source_documents": ["doc-a"],
        "freight_tables": [
            {
                "table_title": "Tabela por dimensao",
                "columns": columns,
                "rows": rows,
            }
        ],
        "freight_routes": [],
        "accessorial_fees": [],
        "coverage_table": {
            "rows": [
                {"destination_uf": "SP", "destination_city": "Sao Paulo", "freight_region": "CAPITAL"},
                {"destination_uf": "SP", "destination_city": "Campinas", "freight_region": "INTERIOR"},
            ]
        },
        "audit_batch": {
            "audit_batch_id": "batch-a",
            "normalized_rows": normalized_rows,
        },
    }


def _suggestion(record: dict) -> dict:
    outputs = audit_svc.compute_audit_outputs(record, record["audit_batch"]["normalized_rows"])
    suggestions = outputs["audit_diagnostics"]["suggestions"]
    assert suggestions
    return suggestions[0]


def _processed_record(record: dict) -> dict:
    outputs = audit_svc.compute_audit_outputs(record, record["audit_batch"]["normalized_rows"])
    processed = copy.deepcopy(record)
    processed["audit_batch"]["status"] = "processed"
    processed["audit_batch"]["results"] = outputs["results"]
    processed["audit_batch"]["summary"] = outputs["summary"]
    processed["audit_batch"]["audit_diagnostics"] = outputs["audit_diagnostics"]
    return processed


def _save_current_record(record: dict) -> dict:
    current = copy.deepcopy(record)
    current["source_documents"] = []
    session[audit_svc.CLEIDE_AUDIT_DOC_IDS_SESSION_KEY] = []
    return audit_svc.save_temp_table_record(current)


def test_compute_audit_outputs_does_not_persist(monkeypatch):
    record = _record()
    monkeypatch.setattr(audit_svc, "save_temp_table_record", pytest.fail)

    outputs = audit_svc.compute_audit_outputs(record, record["audit_batch"]["normalized_rows"])

    assert outputs["summary"]["missing_freight_rule"] == 2
    assert outputs["audit_diagnostics"]["suggestions"][0]["transformation"]["type"] == "select_pricing_dimension"
    assert outputs["audit_diagnostics"]["suggestions"][0]["transformation"]["parameters"]["current_column"] == "Região de frete"


def test_preview_select_pricing_dimension_is_dry_run_and_safe():
    record = _record()
    original = copy.deepcopy(record)
    suggestion = _suggestion(record)

    preview = correction_svc.preview_audit_correction(record, suggestion)

    assert record == original
    assert preview["before"]["summary"]["missing_freight_rule"] == 2
    assert preview["after"]["summary"]["missing_freight_rule"] == 0
    assert preview["delta"]["resolved_errors"] == 2
    assert preview["delta"]["new_ok"] == 2
    assert preview["regressions"] == []
    assert preview["safe_to_apply"] is True
    assert preview["sample_changes"]


def test_ambiguous_candidate_does_not_generate_high_confidence_suggestion():
    record = _record(extra_candidate=True)
    outputs = audit_svc.compute_audit_outputs(record, record["audit_batch"]["normalized_rows"])
    assert outputs["audit_diagnostics"]["groups"] == []
    assert outputs["audit_diagnostics"]["suggestions"] == []


def test_preview_rejects_other_artifact_suggestion():
    record = _record()
    suggestion = _suggestion(record)
    changed = copy.deepcopy(record)
    changed["audit_batch"]["audit_batch_id"] = "batch-b"

    with pytest.raises(correction_svc.CleideAuditCorrectionError) as exc:
        correction_svc.preview_audit_correction(changed, suggestion)

    assert exc.value.error_code == correction_svc.ERROR_CORRECTION_CONSTRAINT_MISMATCH


def test_preview_rejects_fingerprint_and_edit_version_changes():
    record = _record()
    suggestion = _suggestion(record)
    changed = copy.deepcopy(record)
    changed["edit_version"] = 1

    with pytest.raises(correction_svc.CleideAuditCorrectionError) as exc:
        correction_svc.preview_audit_correction(changed, suggestion)

    assert exc.value.error_code == correction_svc.ERROR_CORRECTION_CONSTRAINT_MISMATCH


def test_preview_rejects_source_document_changes():
    record = _record()
    suggestion = _suggestion(record)
    changed = copy.deepcopy(record)
    changed["source_documents"] = ["doc-b"]

    with pytest.raises(correction_svc.CleideAuditCorrectionError) as exc:
        correction_svc.preview_audit_correction(changed, suggestion)

    assert exc.value.error_code == correction_svc.ERROR_CORRECTION_CONSTRAINT_MISMATCH


def test_apply_after_valid_preview_persists_once_and_creates_undo_snapshot(session_app, monkeypatch):
    save_spy = MagicMock(wraps=audit_svc.save_temp_table_record)
    monkeypatch.setattr(audit_svc, "save_temp_table_record", save_spy)
    with session_app.test_request_context("/"):
        record = _save_current_record(_processed_record(_record()))
        suggestion = _suggestion(record)
        preview = correction_svc.preview_audit_correction_for_session(suggestion["suggestion_id"])

        applied = correction_svc.apply_audit_correction_for_session(
            preview_id=preview["preview_id"],
            suggestion_id=preview["suggestion_id"],
            user_scope=1,
            franquia_scope=None,
        )

        assert applied["temp_table"]["audit_batch"]["summary"]["missing_freight_rule"] == 0
        assert applied["temp_table"]["audit_correction"]["can_undo"] is True
        assert applied["temp_table"]["audit_correction"]["last_application_id"] == applied["application_id"]
        raw = audit_svc.load_temp_table_record("tt-test", ttl_hours=24)
        assert raw["edit_version"] == 1
        assert len(raw["correction_history"]) == 1
        assert raw["correction_history"][0]["snapshot"]["freight_tables"][0]["rows"][0]["Região de frete"] == "SUL"
        # One save before the operation setup and one final save for apply.
        assert save_spy.call_count == 2


def test_apply_requires_stored_unexpired_preview(session_app):
    with session_app.test_request_context("/"):
        _save_current_record(_processed_record(_record()))

        with pytest.raises(correction_svc.CleideAuditCorrectionError) as exc:
            correction_svc.apply_audit_correction_for_session(
                preview_id="prev_missing",
                suggestion_id="sug_missing",
            )

        assert exc.value.error_code == correction_svc.ERROR_CORRECTION_PREVIEW_NOT_FOUND


def test_apply_blocks_when_edit_version_changes_after_preview(session_app):
    with session_app.test_request_context("/"):
        record = _save_current_record(_processed_record(_record()))
        suggestion = _suggestion(record)
        preview = correction_svc.preview_audit_correction_for_session(suggestion["suggestion_id"])
        changed = audit_svc.load_temp_table_record("tt-test", ttl_hours=24)
        changed["edit_version"] = 1
        _save_current_record(changed)

        with pytest.raises(correction_svc.CleideAuditCorrectionError) as exc:
            correction_svc.apply_audit_correction_for_session(
                preview_id=preview["preview_id"],
                suggestion_id=preview["suggestion_id"],
            )

        assert exc.value.error_code == correction_svc.ERROR_CORRECTION_CONSTRAINT_MISMATCH


def test_undo_restores_previous_table_and_reprocesses(session_app):
    with session_app.test_request_context("/"):
        record = _save_current_record(_processed_record(_record()))
        suggestion = _suggestion(record)
        preview = correction_svc.preview_audit_correction_for_session(suggestion["suggestion_id"])
        applied = correction_svc.apply_audit_correction_for_session(
            preview_id=preview["preview_id"],
            suggestion_id=preview["suggestion_id"],
        )

        undone = correction_svc.undo_last_audit_correction_for_session(
            application_id=applied["application_id"],
        )

        assert undone["undone_application_id"] == applied["application_id"]
        assert undone["temp_table"]["audit_batch"]["summary"]["missing_freight_rule"] == 2
        assert undone["temp_table"]["audit_correction"]["can_undo"] is False
        raw = audit_svc.load_temp_table_record("tt-test", ttl_hours=24)
        assert raw["edit_version"] == 2
        assert raw["correction_history"] == []


def test_correction_apply_undo_preserves_tax_fiscal_snapshot(session_app):
    record = _record()
    record["tax_config"] = {
        "include_taxes": True,
        "origin_uf": "SP",
        "origin_city": "Sao Paulo",
        "iss_rate": None,
        "destination_ufs": [{"uf": "SP", "source": "manual", "evidence": [], "user_confirmed": True}],
        "icms_rates": [
            {
                "destination_uf": "SP",
                "operation_type": "intermunicipal",
                "suggested_rate": 12.0,
                "applied_rate": 12.0,
                "source_name": audit_svc.ICMS_INTERMUNICIPAL_SOURCE_NAME,
                "source_type": "manual",
                "user_edited": False,
                "is_active": True,
            }
        ],
    }
    processed = _processed_record(record)
    outputs = audit_svc.compute_audit_outputs(processed, processed["audit_batch"]["normalized_rows"])
    processed["audit_batch"] = audit_svc._apply_tax_fiscal_snapshot_to_audit_batch(
        processed["audit_batch"],
        outputs["fiscal_snapshot"],
    )
    processed["audit_batch"]["results"] = outputs["results"]
    processed["audit_batch"]["summary"] = outputs["summary"]
    processed["audit_batch"]["audit_diagnostics"] = outputs["audit_diagnostics"]

    with session_app.test_request_context("/"):
        saved = _save_current_record(processed)
        suggestion = _suggestion(saved)
        preview = correction_svc.preview_audit_correction_for_session(suggestion["suggestion_id"])
        applied = correction_svc.apply_audit_correction_for_session(
            preview_id=preview["preview_id"],
            suggestion_id=preview["suggestion_id"],
        )
        batch_after_apply = applied["temp_table"]["audit_batch"]
        assert batch_after_apply.get("tax_calculation_mode") == "inside"
        assert batch_after_apply.get("tax_calculation_version") == "cleide_audit_tax_v2"
        assert "tax_config_snapshot" in batch_after_apply

        raw = audit_svc.load_temp_table_record("tt-test", ttl_hours=24)
        assert raw["correction_history"][0]["snapshot"]["tax_config"] == record["tax_config"]

        undone = correction_svc.undo_last_audit_correction_for_session(
            application_id=applied["application_id"],
        )
        batch_after_undo = undone["temp_table"]["audit_batch"]
        assert batch_after_undo.get("tax_calculation_mode") == "inside"
        assert batch_after_undo.get("tax_calculation_version") == "cleide_audit_tax_v2"
        assert batch_after_undo.get("tax_config_snapshot", {}).get("origin_uf") == "SP"
