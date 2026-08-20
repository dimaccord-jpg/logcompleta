"""Pacote 4C-B — backfill controlado de CommunicationSuppression a partir de Lead."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import text

from app.extensions import db
from app.models import CommunicationSuppression, Lead, User
from app.services.lead_acquisition_service import (
    CAMPANHA_ACESSO_DESKTOP,
    FONTE_LANDING,
)
from app.services import communication_suppression_service as suppression
from app.services import communication_suppression_backfill_service as backfill
from tests.conftest import seed_sistema_interno, seed_usuario


HMAC_SECRET = "communication-suppression-test-secret-a-32b"

_LEAD_FROZEN_FIELDS = (
    "email",
    "opt_out_at",
    "activation_opt_out_at",
    "converted_user_id",
    "converted_at",
    "acquisition_campaign",
    "acquisition_source",
    "campaign_captured_at",
    "cta_email_sent_at",
    "cta_clicked_at",
    "followup_count",
    "last_followup_sent_at",
    "activation_email_1_sent_at",
    "activation_email_2_sent_at",
    "activation_ended_at",
    "activation_ended_for_user_id",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _enable_suppression(app, secret: str = HMAC_SECRET) -> None:
    app.config["COMMUNICATION_SUPPRESSION_HMAC_SECRET"] = secret


def _disable_suppression(app) -> None:
    app.config["COMMUNICATION_SUPPRESSION_HMAC_SECRET"] = ""


def _make_lead(**kwargs) -> Lead:
    defaults = {
        "email": "lead@empresa.com",
        "acquisition_campaign": CAMPANHA_ACESSO_DESKTOP,
        "acquisition_source": FONTE_LANDING,
        "campaign_captured_at": _utcnow(),
        "followup_count": 0,
    }
    defaults.update(kwargs)
    lead = Lead(**defaults)
    db.session.add(lead)
    db.session.commit()
    return lead


def _snapshot_lead(lead: Lead) -> dict:
    return {name: getattr(lead, name) for name in _LEAD_FROZEN_FIELDS}


def _rows_by_purpose():
    return {row.purpose: row for row in CommunicationSuppression.query.all()}


def _assert_no_pii(text: str, *emails: str) -> None:
    lowered = text.lower()
    assert HMAC_SECRET not in text
    for email in emails:
        assert email not in text
        assert email.lower() not in lowered
        local = email.split("@", 1)[0]
        if local:
            assert local.lower() not in lowered
        digest = suppression._email_hmac(email.strip().lower(), HMAC_SECRET)
        assert digest not in text


def test_opt_out_at_creates_pre_and_activation(app):
    _enable_suppression(app)
    with app.app_context():
        stamp = datetime(2026, 7, 1, 10, 0, 0)
        lead = _make_lead(email="hist.opt@empresa.com", opt_out_at=stamp)
        frozen = _snapshot_lead(lead)

        report = backfill.run_backfill(apply=True)

        assert report.mode == "APPLY"
        assert report.created_pre_registration == 1
        assert report.created_activation == 1
        assert report.expected_pre_registration == 1
        assert report.expected_activation == 1
        rows = _rows_by_purpose()
        assert set(rows) == {
            suppression.PURPOSE_PRE_REGISTRATION,
            suppression.PURPOSE_ACTIVATION,
        }
        for row in rows.values():
            assert row.suppressed_at == stamp
            assert row.source == suppression.SOURCE_HISTORICAL_CAMPAIGN_OPT_OUT
        lead = db.session.get(Lead, lead.id)
        assert _snapshot_lead(lead) == frozen


def test_activation_opt_out_at_creates_only_activation(app):
    _enable_suppression(app)
    with app.app_context():
        stamp = datetime(2026, 6, 15, 8, 30, 0)
        lead = _make_lead(
            email="hist.act.only@empresa.com",
            activation_opt_out_at=stamp,
        )
        frozen = _snapshot_lead(lead)

        report = backfill.run_backfill(apply=True)

        assert report.created_pre_registration == 0
        assert report.created_activation == 1
        assert report.expected_pre_registration == 0
        assert report.expected_activation == 1
        row = CommunicationSuppression.query.one()
        assert row.purpose == suppression.PURPOSE_ACTIVATION
        assert row.suppressed_at == stamp
        assert row.source == suppression.SOURCE_HISTORICAL_ACTIVATION_OPT_OUT
        lead = db.session.get(Lead, lead.id)
        assert _snapshot_lead(lead) == frozen


def test_both_timestamps_activation_uses_oldest(app):
    _enable_suppression(app)
    with app.app_context():
        older = datetime(2026, 5, 1, 9, 0, 0)
        newer = datetime(2026, 8, 1, 9, 0, 0)
        _make_lead(
            email="hist.both.min@empresa.com",
            opt_out_at=newer,
            activation_opt_out_at=older,
        )

        backfill.run_backfill(apply=True)

        rows = _rows_by_purpose()
        pre = rows[suppression.PURPOSE_PRE_REGISTRATION]
        act = rows[suppression.PURPOSE_ACTIVATION]
        assert pre.suppressed_at == newer
        assert pre.source == suppression.SOURCE_HISTORICAL_CAMPAIGN_OPT_OUT
        assert act.suppressed_at == older
        assert act.source == suppression.SOURCE_HISTORICAL_ACTIVATION_OPT_OUT


def test_both_timestamps_campaign_older_uses_campaign_source(app):
    _enable_suppression(app)
    with app.app_context():
        campaign_at = datetime(2026, 4, 1, 9, 0, 0)
        activation_at = datetime(2026, 4, 20, 9, 0, 0)
        _make_lead(
            email="hist.both.campaign.older@empresa.com",
            opt_out_at=campaign_at,
            activation_opt_out_at=activation_at,
        )

        backfill.run_backfill(apply=True)

        act = _rows_by_purpose()[suppression.PURPOSE_ACTIVATION]
        assert act.suppressed_at == campaign_at
        assert act.source == suppression.SOURCE_HISTORICAL_CAMPAIGN_OPT_OUT


def test_equal_timestamps_use_campaign_historical_source(app):
    _enable_suppression(app)
    with app.app_context():
        stamp = datetime(2026, 4, 10, 12, 0, 0)
        _make_lead(
            email="hist.equal@empresa.com",
            opt_out_at=stamp,
            activation_opt_out_at=stamp,
        )

        backfill.run_backfill(apply=True)

        act = _rows_by_purpose()[suppression.PURPOSE_ACTIVATION]
        assert act.suppressed_at == stamp
        assert act.source == suppression.SOURCE_HISTORICAL_CAMPAIGN_OPT_OUT
        pre = _rows_by_purpose()[suppression.PURPOSE_PRE_REGISTRATION]
        assert pre.source == suppression.SOURCE_HISTORICAL_CAMPAIGN_OPT_OUT
        assert pre.suppressed_at == stamp


def test_dry_run_does_not_write(app):
    _enable_suppression(app)
    with app.app_context():
        stamp = datetime(2026, 3, 1, 11, 0, 0)
        lead = _make_lead(email="hist.dry@empresa.com", opt_out_at=stamp)
        frozen = _snapshot_lead(lead)
        before = CommunicationSuppression.query.count()
        before_ids = [row.id for row in CommunicationSuppression.query.all()]

        with patch.object(
            backfill,
            "suppress_email",
            side_effect=AssertionError("dry-run must not persist"),
        ):
            report = backfill.run_backfill(apply=False)

        assert report.mode == "DRY_RUN"
        assert report.would_create_pre_registration == 1
        assert report.would_create_activation == 1
        assert report.created_pre_registration == 0
        assert report.created_activation == 0
        assert CommunicationSuppression.query.count() == before
        assert [row.id for row in CommunicationSuppression.query.all()] == before_ids
        assert not db.session.new
        lead = db.session.get(Lead, lead.id)
        assert _snapshot_lead(lead) == frozen


def test_apply_persists_and_second_run_is_idempotent(app):
    _enable_suppression(app)
    with app.app_context():
        stamp = datetime(2026, 2, 2, 7, 0, 0)
        lead = _make_lead(email="hist.idemp@empresa.com", opt_out_at=stamp)
        frozen = _snapshot_lead(lead)

        first = backfill.run_backfill(apply=True)
        assert first.created_pre_registration == 1
        assert first.created_activation == 1
        first_rows = {
            row.purpose: (row.id, row.suppressed_at, row.source, row.email_hmac)
            for row in CommunicationSuppression.query.all()
        }

        second = backfill.run_backfill(apply=True)
        assert second.created_pre_registration == 0
        assert second.created_activation == 0
        assert second.existing_pre_registration == 1
        assert second.existing_activation == 1
        assert CommunicationSuppression.query.count() == 2
        for row in CommunicationSuppression.query.all():
            assert (row.id, row.suppressed_at, row.source, row.email_hmac) == first_rows[
                row.purpose
            ]
        lead = db.session.get(Lead, lead.id)
        assert _snapshot_lead(lead) == frozen


def test_existing_identical_row_preserved(app):
    _enable_suppression(app)
    with app.app_context():
        stamp = datetime(2026, 1, 20, 15, 0, 0)
        lead = _make_lead(email="hist.existing@empresa.com", opt_out_at=stamp)
        suppression.suppress_email(
            lead.email,
            suppression.PURPOSE_PRE_REGISTRATION,
            suppression.SOURCE_HISTORICAL_CAMPAIGN_OPT_OUT,
            suppressed_at=stamp,
        )
        suppression.suppress_email(
            lead.email,
            suppression.PURPOSE_ACTIVATION,
            suppression.SOURCE_HISTORICAL_CAMPAIGN_OPT_OUT,
            suppressed_at=stamp,
        )
        original = {
            row.purpose: (row.id, row.suppressed_at, row.source, row.created_at)
            for row in CommunicationSuppression.query.all()
        }

        report = backfill.run_backfill(apply=True)

        assert report.created_pre_registration == 0
        assert report.created_activation == 0
        assert report.existing_pre_registration == 1
        assert report.existing_activation == 1
        assert report.conflicts_pre_registration == 0
        assert report.conflicts_activation == 0
        for row in CommunicationSuppression.query.all():
            assert (
                row.id,
                row.suppressed_at,
                row.source,
                row.created_at,
            ) == original[row.purpose]


def test_conflict_different_timestamp_preserved(app):
    _enable_suppression(app)
    with app.app_context():
        historical = datetime(2026, 7, 1, 10, 0, 0)
        existing_at = datetime(2026, 8, 10, 10, 0, 0)
        lead = _make_lead(email="hist.conflict@empresa.com", opt_out_at=historical)
        suppression.suppress_email(
            lead.email,
            suppression.PURPOSE_PRE_REGISTRATION,
            suppression.SOURCE_HISTORICAL_CAMPAIGN_OPT_OUT,
            suppressed_at=existing_at,
        )
        suppression.suppress_email(
            lead.email,
            suppression.PURPOSE_ACTIVATION,
            suppression.SOURCE_HISTORICAL_CAMPAIGN_OPT_OUT,
            suppressed_at=existing_at,
        )
        original = {
            row.purpose: (row.id, row.suppressed_at, row.source)
            for row in CommunicationSuppression.query.all()
        }

        report = backfill.run_backfill(apply=True)

        assert report.conflicts_pre_registration == 1
        assert report.conflicts_activation == 1
        assert report.created_pre_registration == 0
        assert report.created_activation == 0
        assert CommunicationSuppression.query.count() == 2
        for row in CommunicationSuppression.query.all():
            assert (row.id, row.suppressed_at, row.source) == original[row.purpose]
            assert row.suppressed_at == existing_at


def test_secret_absent_aborts_dry_run_and_apply(app):
    _disable_suppression(app)
    with app.app_context():
        lead = _make_lead(
            email="hist.nosecret@empresa.com",
            opt_out_at=datetime(2026, 1, 1, 0, 0, 0),
        )
        frozen = _snapshot_lead(lead)
        for apply_mode in (False, True):
            with pytest.raises(
                backfill.SuppressionBackfillAborted,
                match="COMMUNICATION_SUPPRESSION_HMAC_SECRET",
            ):
                backfill.run_backfill(apply=apply_mode)
            assert CommunicationSuppression.query.count() == 0
        lead = db.session.get(Lead, lead.id)
        assert _snapshot_lead(lead) == frozen
        assert suppression.is_suppression_enabled() is False


def test_apply_batch_error_rolls_back_and_aborts(app):
    _enable_suppression(app)
    with app.app_context():
        stamp = datetime(2026, 9, 1, 6, 0, 0)
        first = _make_lead(
            email="hist.batch1@empresa.com",
            activation_opt_out_at=stamp,
        )
        second = _make_lead(
            email="hist.batch2@empresa.com",
            activation_opt_out_at=stamp + timedelta(hours=1),
        )
        first_id = first.id
        second_id = second.id
        frozen_first = _snapshot_lead(first)
        frozen_second = _snapshot_lead(second)
        real = suppression.suppress_email
        calls = {"n": 0}

        def fail_second(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RuntimeError("simulated batch failure")
            return real(*args, **kwargs)

        with patch.object(backfill, "suppress_email", side_effect=fail_second):
            with pytest.raises(
                backfill.SuppressionBackfillAborted,
                match="Erro inesperado no batch",
            ):
                backfill.run_backfill(apply=True, batch_size=50)

        db.session.expire_all()
        assert CommunicationSuppression.query.count() == 0
        assert (
            db.session.execute(text("SELECT COUNT(*) FROM communication_suppression")).scalar()
            == 0
        )
        assert db.session.get(Lead, first_id) is not None
        assert _snapshot_lead(db.session.get(Lead, first_id)) == frozen_first
        assert _snapshot_lead(db.session.get(Lead, second_id)) == frozen_second


def test_backfill_does_not_touch_newsletter(app):
    _enable_suppression(app)
    with app.app_context():
        conta, franquia = seed_sistema_interno()
        user = seed_usuario(
            franquia.id,
            conta.id,
            email="hist.news@empresa.com",
        )
        user.subscribes_to_newsletter = True
        db.session.commit()
        user_id = user.id
        _make_lead(
            email=user.email,
            opt_out_at=datetime(2026, 1, 5, 9, 0, 0),
        )

        backfill.run_backfill(apply=True)

        user = db.session.get(User, user_id)
        assert user.subscribes_to_newsletter is True
        purposes = {row.purpose for row in CommunicationSuppression.query.all()}
        assert purposes == {
            suppression.PURPOSE_PRE_REGISTRATION,
            suppression.PURPOSE_ACTIVATION,
        }
        assert "newsletter" not in purposes


def test_lead_without_opt_out_is_not_scanned(app):
    _enable_suppression(app)
    with app.app_context():
        _make_lead(email="hist.clean@empresa.com")
        _make_lead(
            email="hist.opt.only@empresa.com",
            opt_out_at=datetime(2026, 1, 8, 8, 0, 0),
        )
        report = backfill.run_backfill(apply=True)
        assert report.leads_scanned == 1
        assert report.leads_with_opt_out == 1
        assert CommunicationSuppression.query.count() == 2


def test_missing_table_aborts_without_writes(app):
    _enable_suppression(app)
    with app.app_context():
        _make_lead(
            email="hist.notable@empresa.com",
            opt_out_at=datetime(2026, 1, 9, 8, 0, 0),
        )
        with patch.object(backfill, "_table_exists", return_value=False):
            with pytest.raises(
                backfill.SuppressionBackfillAborted,
                match="communication_suppression indisponivel",
            ):
                backfill.run_backfill(apply=True)
        assert CommunicationSuppression.query.count() == 0


def test_output_and_logs_have_no_pii(app, caplog):
    _enable_suppression(app)
    email = "pii.unique.token@empresa.com"
    with app.app_context():
        _make_lead(email=email, opt_out_at=datetime(2026, 1, 11, 8, 0, 0))
        with caplog.at_level("INFO"):
            report = backfill.run_backfill(apply=True)
        rendered = backfill.format_report(report)
        logs = " ".join(record.getMessage() for record in caplog.records)
        _assert_no_pii(rendered, email)
        _assert_no_pii(logs, email)
        captured = []
        exit_code = backfill.emit_backfill_cli(
            apply=False,
            batch_size=100,
            echo=captured.append,
        )
        assert exit_code == 0
        _assert_no_pii("\n".join(captured), email)
        assert captured[0] == "MODE=DRY_RUN"


def test_cli_defaults_to_dry_run(app):
    _enable_suppression(app)
    with app.app_context():
        _make_lead(
            email="hist.cli@empresa.com",
            opt_out_at=datetime(2026, 1, 12, 8, 0, 0),
        )
        backfill.register_communication_suppression_backfill_command(app)
        runner = app.test_cli_runner()
        result = runner.invoke(args=["communication-suppression-backfill"])
        assert result.exit_code == 0
        assert "MODE=DRY_RUN" in result.output
        assert "STATUS=OK" in result.output
        assert CommunicationSuppression.query.count() == 0
        _assert_no_pii(result.output, "hist.cli@empresa.com")


def test_cli_apply_persists_on_isolated_test_db(app):
    _enable_suppression(app)
    with app.app_context():
        _make_lead(
            email="hist.cli.apply@empresa.com",
            opt_out_at=datetime(2026, 1, 13, 8, 0, 0),
        )
        backfill.register_communication_suppression_backfill_command(app)
        runner = app.test_cli_runner()
        result = runner.invoke(args=["communication-suppression-backfill", "--apply"])
        assert result.exit_code == 0
        assert result.output.splitlines()[0] == "MODE=APPLY"
        assert "STATUS=OK" in result.output
        assert CommunicationSuppression.query.count() == 2
        _assert_no_pii(result.output, "hist.cli.apply@empresa.com")


def test_cli_secret_absent_aborts(app):
    _disable_suppression(app)
    with app.app_context():
        _make_lead(
            email="hist.cli.nosecret@empresa.com",
            opt_out_at=datetime(2026, 1, 14, 8, 0, 0),
        )
        backfill.register_communication_suppression_backfill_command(app)
        runner = app.test_cli_runner()
        result = runner.invoke(args=["communication-suppression-backfill", "--apply"])
        assert result.exit_code == 1
        assert "MODE=APPLY" in result.output
        assert "STATUS=ABORTED" in result.output
        assert "COMMUNICATION_SUPPRESSION_HMAC_SECRET" in result.output
        assert CommunicationSuppression.query.count() == 0
        _assert_no_pii(result.output, "hist.cli.nosecret@empresa.com")


def test_live_4a_source_with_matching_timestamp_is_existing(app):
    _enable_suppression(app)
    with app.app_context():
        stamp = datetime(2026, 1, 16, 10, 0, 0)
        lead = _make_lead(email="hist.live4a@empresa.com", opt_out_at=stamp)
        suppression.suppress_email(
            lead.email,
            suppression.PURPOSE_PRE_REGISTRATION,
            suppression.SOURCE_CAMPAIGN_UNSUBSCRIBE,
            suppressed_at=stamp,
        )
        suppression.suppress_email(
            lead.email,
            suppression.PURPOSE_ACTIVATION,
            suppression.SOURCE_CAMPAIGN_UNSUBSCRIBE,
            suppressed_at=stamp,
        )
        original = {
            row.purpose: (row.id, row.suppressed_at, row.source)
            for row in CommunicationSuppression.query.all()
        }

        report = backfill.run_backfill(apply=True)

        assert report.existing_pre_registration == 1
        assert report.existing_activation == 1
        assert report.conflicts_pre_registration == 0
        assert report.conflicts_activation == 0
        assert report.created_pre_registration == 0
        assert report.created_activation == 0
        for row in CommunicationSuppression.query.all():
            assert (row.id, row.suppressed_at, row.source) == original[row.purpose]
            assert row.source == suppression.SOURCE_CAMPAIGN_UNSUBSCRIBE


def test_incompatible_source_same_timestamp_is_conflict(app):
    _enable_suppression(app)
    with app.app_context():
        stamp = datetime(2026, 1, 17, 10, 0, 0)
        lead = _make_lead(email="hist.source.conflict@empresa.com", opt_out_at=stamp)
        suppression.suppress_email(
            lead.email,
            suppression.PURPOSE_ACTIVATION,
            suppression.SOURCE_HISTORICAL_ACTIVATION_OPT_OUT,
            suppressed_at=stamp,
        )
        original = CommunicationSuppression.query.one()
        original_id = original.id
        original_source = original.source

        report = backfill.run_backfill(apply=True)

        assert report.created_pre_registration == 1
        assert report.conflicts_activation == 1
        assert report.created_activation == 0
        row = CommunicationSuppression.query.filter_by(
            purpose=suppression.PURPOSE_ACTIVATION
        ).one()
        assert row.id == original_id
        assert row.source == original_source
        assert row.suppressed_at == stamp


def test_dry_run_reports_would_conflict_without_writes(app):
    _enable_suppression(app)
    with app.app_context():
        historical = datetime(2026, 1, 18, 10, 0, 0)
        existing_at = datetime(2026, 2, 1, 10, 0, 0)
        lead = _make_lead(email="hist.dry.conflict@empresa.com", opt_out_at=historical)
        suppression.suppress_email(
            lead.email,
            suppression.PURPOSE_PRE_REGISTRATION,
            suppression.SOURCE_HISTORICAL_CAMPAIGN_OPT_OUT,
            suppressed_at=existing_at,
        )
        suppression.suppress_email(
            lead.email,
            suppression.PURPOSE_ACTIVATION,
            suppression.SOURCE_HISTORICAL_CAMPAIGN_OPT_OUT,
            suppressed_at=existing_at,
        )
        before = CommunicationSuppression.query.count()
        original = {
            row.purpose: (row.id, row.suppressed_at, row.source)
            for row in CommunicationSuppression.query.all()
        }

        report = backfill.run_backfill()

        assert report.mode == "DRY_RUN"
        assert report.would_conflict_pre_registration == 1
        assert report.would_conflict_activation == 1
        assert report.conflicts_pre_registration == 0
        assert report.created_pre_registration == 0
        assert CommunicationSuppression.query.count() == before
        for row in CommunicationSuppression.query.all():
            assert (row.id, row.suppressed_at, row.source) == original[row.purpose]


def test_cursor_batches_by_id(app):
    _enable_suppression(app)
    with app.app_context():
        stamp = datetime(2026, 1, 19, 10, 0, 0)
        _make_lead(email="hist.batch.a@empresa.com", opt_out_at=stamp)
        _make_lead(
            email="hist.batch.b@empresa.com",
            activation_opt_out_at=stamp,
        )
        report = backfill.run_backfill(apply=True, batch_size=1)
        assert report.batches == 2
        assert report.leads_scanned == 2
        assert report.created_pre_registration == 1
        assert report.created_activation == 2
        assert CommunicationSuppression.query.count() == 3


def test_dry_run_default_does_not_commit(app):
    _enable_suppression(app)
    with app.app_context():
        _make_lead(
            email="hist.default.dry@empresa.com",
            opt_out_at=datetime(2026, 1, 20, 10, 0, 0),
        )
        with patch.object(
            db.session,
            "commit",
            side_effect=AssertionError("dry-run must not commit"),
        ):
            report = backfill.run_backfill()
        assert report.mode == "DRY_RUN"
        assert report.would_create_pre_registration == 1
        assert report.would_create_activation == 1
        assert CommunicationSuppression.query.count() == 0
        assert not db.session.new
