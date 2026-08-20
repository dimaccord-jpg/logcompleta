"""LEAD-R2 — minimização controlada de Lead.email (placeholder + HMAC)."""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.extensions import db
from app.models import CommunicationSuppression, Lead, NewsletterSubscription, User
from app.services.lead_acquisition_service import (
    CAMPANHA_ACESSO_DESKTOP,
    FONTE_LANDING,
    capturar_lead_para_campanha,
)
from app.services import communication_suppression_backfill_service as backfill
from app.services import communication_suppression_service as suppression
from app.services import desktop_access_activation_email_service as activation
from app.services import lead_campaign_email_service as campaign_email
from app.services import lead_email_minimization_service as minimization
from app.services.lead_campaign_conversion_service import reconcile_lead
from app.services.lead_email_state import (
    LeadEmailIdentityError,
    is_lead_email_minimized,
    lead_minimized_email,
)
from app.services.user_operational_state import email_operacional_apos_encerramento
from tests.conftest import seed_sistema_interno, seed_usuario

HMAC_SECRET = "communication-suppression-test-secret-a-32b"
TOKEN_SECRET = "test-secret-cta-tokens"
ROOT = Path(__file__).resolve().parents[1]

_ANALYTICS_FIELDS = (
    "converted_user_id",
    "converted_at",
    "acquisition_campaign",
    "campaign_captured_at",
    "cta_email_sent_at",
    "followup_count",
    "last_followup_sent_at",
    "activation_email_1_sent_at",
    "activation_email_2_sent_at",
    "activation_ended_at",
    "activation_ended_for_user_id",
    "opt_out_at",
    "activation_opt_out_at",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _enable_suppression(app, secret: str = HMAC_SECRET) -> None:
    app.config["COMMUNICATION_SUPPRESSION_HMAC_SECRET"] = secret


def _disable_suppression(app) -> None:
    app.config["COMMUNICATION_SUPPRESSION_HMAC_SECRET"] = ""


def _ensure_conta_franquia():
    from app.models import Conta

    existing = Conta.query.filter_by(slug=Conta.SLUG_SISTEMA).first()
    if existing is not None:
        return existing, existing.franquias.first()
    return seed_sistema_interno()


def _make_user(email: str, *, created_at: datetime | None = None) -> User:
    conta, franquia = _ensure_conta_franquia()
    user = seed_usuario(franquia.id, conta.id, email=email)
    if created_at is not None:
        user.created_at = created_at
        db.session.commit()
    return user


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


def _make_converted_lead(*, email: str = "conv@empresa.com", **kwargs) -> Lead:
    captured = kwargs.pop("captured_at", None) or _utcnow() - timedelta(days=2)
    converted = kwargs.pop("converted_at", None) or captured + timedelta(hours=1)
    user = kwargs.pop("user", None) or _make_user(email, created_at=converted)
    defaults = {
        "email": email,
        "acquisition_campaign": CAMPANHA_ACESSO_DESKTOP,
        "acquisition_source": FONTE_LANDING,
        "campaign_captured_at": captured,
        "converted_user_id": user.id,
        "converted_at": converted,
        "followup_count": 0,
        "cta_email_sent_at": captured,
    }
    defaults.update(kwargs)
    lead = Lead(**defaults)
    db.session.add(lead)
    db.session.commit()
    return lead


def _make_terminal_lead(*, email: str = "term@empresa.com", **kwargs) -> Lead:
    lead = _make_converted_lead(email=email, **kwargs)
    lead.activation_ended_at = _utcnow()
    lead.activation_ended_for_user_id = lead.converted_user_id
    db.session.commit()
    return lead


def _snapshot_analytics(lead: Lead) -> dict:
    return {name: getattr(lead, name) for name in _ANALYTICS_FIELDS}


def _cta_builders():
    return (
        lambda token: f"https://example.test/acesso-desktop/continuar/{token}",
        lambda token: f"https://example.test/acesso-desktop/descadastrar/{token}",
    )


def _assert_no_pii(text: str, *emails: str) -> None:
    lowered = text.lower()
    assert HMAC_SECRET not in text
    for email in emails:
        assert email not in text
        assert email.lower() not in lowered
        local = email.split("@", 1)[0]
        if local and local not in ("lead_minimized", "anon"):
            assert local.lower() not in lowered
        digest = suppression._email_hmac(email.strip().lower(), HMAC_SECRET)
        assert digest not in text


def test_migration_encadeada_apos_x8y9z0a1b2c3():
    cfg = Config()
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    script = ScriptDirectory.from_config(cfg)
    rev = script.get_revision("y9z0a1b2c3d4")
    assert rev is not None
    assert rev.down_revision == "x8y9z0a1b2c3"
    assert script.get_heads() == ["y9z0a1b2c3d4"]


def test_migration_upgrade_downgrade_sqlite(tmp_path):
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine, inspect, text

    db_path = tmp_path / "lead_email_hmac_mig.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE leads (
                    id INTEGER PRIMARY KEY,
                    email VARCHAR(150) NOT NULL
                )
                """
            )
        )

    mig_path = ROOT / "migrations" / "versions" / "y9z0a1b2c3d4_lead_email_hmac.py"
    spec = importlib.util.spec_from_file_location("lead_email_hmac_mig_mod", mig_path)
    mig = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mig)

    def _run(fn):
        with engine.connect() as conn:
            context = MigrationContext.configure(conn, opts={"render_as_batch": True})
            ops = Operations(context)
            original_op = mig.op
            try:
                mig.op = ops
                with conn.begin():
                    fn()
            finally:
                mig.op = original_op

    _run(mig.upgrade)
    lead_cols = {c["name"] for c in inspect(engine).get_columns("leads")}
    assert "email_hmac" in lead_cols
    email_col = next(c for c in inspect(engine).get_columns("leads") if c["name"] == "email")
    assert email_col["nullable"] is False

    _run(mig.downgrade)
    lead_cols = {c["name"] for c in inspect(engine).get_columns("leads")}
    assert "email_hmac" not in lead_cols
    email_col = next(c for c in inspect(engine).get_columns("leads") if c["name"] == "email")
    assert email_col["nullable"] is False


def test_1_nao_convertido_nao_elegivel(app):
    _enable_suppression(app)
    with app.app_context():
        lead = _make_lead(email="ativo@empresa.com")
        decision = minimization.evaluate_lead_email_minimization_eligibility(lead)
        assert decision.status == minimization.STATUS_SKIPPED_NOT_CONVERTED


def test_2_convertido_activation_ativa_nao_elegivel(app):
    _enable_suppression(app)
    with app.app_context():
        lead = _make_converted_lead(email="ativa@empresa.com")
        decision = minimization.evaluate_lead_email_minimization_eligibility(lead)
        assert decision.status == minimization.STATUS_SKIPPED_ACTIVATION_ACTIVE


def test_3_convertido_activation_ended_compativel_elegivel(app):
    _enable_suppression(app)
    with app.app_context():
        lead = _make_terminal_lead(email="ended@empresa.com")
        decision = minimization.evaluate_lead_email_minimization_eligibility(lead)
        assert decision.status == minimization.STATUS_ELIGIBLE


def test_4_activation_ended_outro_user_nao_elegivel(app):
    _enable_suppression(app)
    with app.app_context():
        lead = _make_converted_lead(email="outro.ended@empresa.com")
        lead.activation_ended_at = _utcnow()
        lead.activation_ended_for_user_id = int(lead.converted_user_id) + 99
        db.session.commit()
        decision = minimization.evaluate_lead_email_minimization_eligibility(lead)
        assert decision.status == minimization.STATUS_SKIPPED_ACTIVATION_ACTIVE


def test_5_opt_out_historico_sem_suppression_pre_nao_elegivel(app):
    _enable_suppression(app)
    with app.app_context():
        lead = _make_terminal_lead(email="hist.pre@empresa.com")
        lead.opt_out_at = _utcnow()
        db.session.commit()
        decision = minimization.evaluate_lead_email_minimization_eligibility(lead)
        assert decision.status == minimization.STATUS_SKIPPED_HISTORICAL_SUPPRESSION_PENDING


def test_6_activation_optout_sem_suppression_act_nao_elegivel(app):
    _enable_suppression(app)
    with app.app_context():
        lead = _make_terminal_lead(email="hist.act@empresa.com")
        lead.activation_opt_out_at = _utcnow()
        db.session.commit()
        decision = minimization.evaluate_lead_email_minimization_eligibility(lead)
        assert decision.status == minimization.STATUS_SKIPPED_HISTORICAL_SUPPRESSION_PENDING


def test_7_optout_com_suppression_correta_pode_ser_elegivel(app):
    _enable_suppression(app)
    with app.app_context():
        stamp = datetime(2026, 3, 1, 10, 0, 0)
        lead = _make_converted_lead(email="opt.ok@empresa.com")
        lead.opt_out_at = stamp
        db.session.commit()
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
        decision = minimization.evaluate_lead_email_minimization_eligibility(lead)
        assert decision.status == minimization.STATUS_ELIGIBLE


def test_8_dry_run_zero_writes(app):
    _enable_suppression(app)
    with app.app_context():
        lead = _make_terminal_lead(email="dry@empresa.com")
        original = lead.email
        before_supp = CommunicationSuppression.query.count()
        with patch.object(db.session, "commit", side_effect=AssertionError("dry-run must not commit")):
            report = minimization.run_minimization()
        lead = db.session.get(Lead, lead.id)
        assert report.mode == "DRY_RUN"
        assert report.would_minimize == 1
        assert report.minimized == 0
        assert lead.email == original
        assert lead.email_hmac is None
        assert CommunicationSuppression.query.count() == before_supp


def test_9_10_11_12_apply_hmac_placeholder_sem_suppression(app):
    _enable_suppression(app)
    with app.app_context():
        original = "apply.ok@empresa.com"
        lead = _make_terminal_lead(email=original)
        lead_id = lead.id
        expected_hmac = suppression.derive_email_hmac(original)
        before_supp = CommunicationSuppression.query.count()

        report = minimization.run_minimization(apply=True)

        lead = db.session.get(Lead, lead_id)
        assert report.minimized == 1
        assert lead.email_hmac == expected_hmac
        assert lead.email == lead_minimized_email(lead_id)
        assert is_lead_email_minimized(lead)
        assert CommunicationSuppression.query.count() == before_supp
        assert suppression.is_email_suppressed(original, suppression.PURPOSE_PRE_REGISTRATION) is False


def test_13_14_placeholder_unico_e_invalid(app):
    _enable_suppression(app)
    with app.app_context():
        a = _make_terminal_lead(email="ph.a@empresa.com")
        b = _make_terminal_lead(email="ph.b@empresa.com")
        minimization.run_minimization(apply=True)
        a = db.session.get(Lead, a.id)
        b = db.session.get(Lead, b.id)
        assert a.email != b.email
        assert a.email.endswith("@anon.invalid")
        assert b.email.endswith("@anon.invalid")
        assert a.email == f"lead_minimized_{a.id}@anon.invalid"
        assert b.email == f"lead_minimized_{b.id}@anon.invalid"


def test_15_37_already_minimized_idempotente(app):
    _enable_suppression(app)
    with app.app_context():
        lead = _make_terminal_lead(email="idemp@empresa.com")
        first = minimization.run_minimization(apply=True)
        lead = db.session.get(Lead, lead.id)
        frozen_email = lead.email
        frozen_hmac = lead.email_hmac
        frozen_analytics = _snapshot_analytics(lead)
        ended_at = lead.activation_ended_at

        second = minimization.run_minimization(apply=True)
        lead = db.session.get(Lead, lead.id)
        assert first.minimized == 1
        assert second.minimized == 0
        assert second.already_minimized == 1
        assert lead.email == frozen_email
        assert lead.email_hmac == frozen_hmac
        assert _snapshot_analytics(lead) == frozen_analytics
        assert lead.activation_ended_at == ended_at


def test_16_placeholder_sem_hmac_conflito(app):
    _enable_suppression(app)
    with app.app_context():
        lead = _make_converted_lead(email="conflict@empresa.com")
        lead.email = lead_minimized_email(lead.id)
        lead.email_hmac = None
        db.session.commit()
        lead_id = lead.id
        decision = minimization.evaluate_lead_email_minimization_eligibility(lead)
        assert decision.status == minimization.STATUS_CONFLICT
        with pytest.raises(minimization.LeadEmailMinimizationAborted):
            minimization.run_minimization(apply=True)
        lead = db.session.get(Lead, lead_id)
        assert lead.email_hmac is None


def test_17_secret_ausente_apply_aborta_sem_writes(app):
    _disable_suppression(app)
    with app.app_context():
        lead = _make_terminal_lead(email="nosecret@empresa.com")
        original = lead.email
        with pytest.raises(
            minimization.LeadEmailMinimizationAborted,
            match="COMMUNICATION_SUPPRESSION_HMAC_SECRET",
        ):
            minimization.run_minimization(apply=True)
        lead = db.session.get(Lead, lead.id)
        assert lead.email == original
        assert lead.email_hmac is None
        assert CommunicationSuppression.query.count() == 0


def test_18_19_rollback_nao_deixa_estado_parcial(app):
    _enable_suppression(app)
    with app.app_context():
        a = _make_terminal_lead(email="roll.a@empresa.com")
        b = _make_terminal_lead(email="roll.b@empresa.com")
        a_id = a.id
        b_id = b.id
        original_a = a.email
        original_b = b.email
        with patch.object(db.session, "commit", side_effect=RuntimeError("boom")):
            with pytest.raises(minimization.LeadEmailMinimizationAborted):
                minimization.run_minimization(apply=True)
        db.session.rollback()
        db.session.expire_all()
        a = db.session.get(Lead, a_id)
        b = db.session.get(Lead, b_id)
        assert a.email == original_a
        assert b.email == original_b
        assert a.email_hmac is None
        assert b.email_hmac is None
        assert not is_lead_email_minimized(a)
        assert not is_lead_email_minimized(b)


def test_20_22_23_late_unsubscribe_pre_apos_minimizacao(app):
    _enable_suppression(app)
    with app.app_context():
        original = "late.pre@empresa.com"
        lead = _make_terminal_lead(email=original)
        minimization.run_minimization(apply=True)
        lead = db.session.get(Lead, lead.id)
        placeholder = lead.email
        assert is_lead_email_minimized(lead)
        campaign_email.apply_campaign_opt_out(lead)
        lead = db.session.get(Lead, lead.id)
        assert lead.opt_out_at is not None
        assert suppression.is_email_suppressed(original, suppression.PURPOSE_PRE_REGISTRATION)
        assert suppression.is_email_suppressed(original, suppression.PURPOSE_ACTIVATION)
        assert suppression.is_email_suppressed(placeholder, suppression.PURPOSE_PRE_REGISTRATION) is False
        row = CommunicationSuppression.query.filter_by(
            purpose=suppression.PURPOSE_PRE_REGISTRATION
        ).one()
        assert row.email_hmac == suppression.derive_email_hmac(original)
        assert row.email_hmac != suppression.derive_email_hmac(placeholder)


def test_21_24_late_unsubscribe_act_apos_minimizacao(app):
    _enable_suppression(app)
    with app.app_context():
        original = "late.act@empresa.com"
        lead = _make_terminal_lead(email=original)
        minimization.run_minimization(apply=True)
        lead = db.session.get(Lead, lead.id)
        placeholder = lead.email
        activation.apply_activation_opt_out(lead)
        lead = db.session.get(Lead, lead.id)
        assert lead.activation_opt_out_at is not None
        assert suppression.is_email_suppressed(original, suppression.PURPOSE_ACTIVATION)
        assert suppression.is_email_suppressed(placeholder, suppression.PURPOSE_ACTIVATION) is False
        row = CommunicationSuppression.query.one()
        assert row.email_hmac == suppression.derive_email_hmac(original)


def test_late_unsubscribe_minimizado_sem_hmac_rollback(app):
    _enable_suppression(app)
    with app.app_context():
        lead = _make_converted_lead(email="fail.hmac@empresa.com")
        lead.email = lead_minimized_email(lead.id)
        lead.email_hmac = None
        db.session.commit()
        with pytest.raises(LeadEmailIdentityError):
            campaign_email.apply_campaign_opt_out(lead)
        lead = db.session.get(Lead, lead.id)
        assert lead.opt_out_at is None
        assert CommunicationSuppression.query.count() == 0
        with pytest.raises(LeadEmailIdentityError):
            activation.apply_activation_opt_out(lead)
        lead = db.session.get(Lead, lead.id)
        assert lead.activation_opt_out_at is None
        assert CommunicationSuppression.query.count() == 0


def test_25_cta_nao_envia_para_minimizado(app):
    _enable_suppression(app)
    with app.app_context():
        lead = _make_terminal_lead(email="cta.min@empresa.com")
        minimization.run_minimization(apply=True)
        lead = db.session.get(Lead, lead.id)
        build_cta, build_unsub = _cta_builders()
        with patch.object(campaign_email, "send_email") as send_mock:
            status = campaign_email.maybe_send_initial_cta_email(
                lead,
                secret_key=TOKEN_SECRET,
                build_cta_url=build_cta,
                build_unsubscribe_url=build_unsub,
            )
        send_mock.assert_not_called()
        assert status == "skipped_lead_email_minimized"
        assert lead.cta_email_sent_at is not None  # já existia do fixture
        # não grava novo envio: timestamp permanece o original da fixture
        frozen = lead.cta_email_sent_at
        lead2 = db.session.get(Lead, lead.id)
        assert lead2.cta_email_sent_at == frozen


def test_26_followup_nao_envia_para_minimizado(app):
    _enable_suppression(app)
    with app.app_context():
        captured = _utcnow() - timedelta(hours=30)
        lead = _make_lead(
            email="fu.min@empresa.com",
            cta_email_sent_at=captured,
            campaign_captured_at=captured,
        )
        lead.email = lead_minimized_email(lead.id)
        lead.email_hmac = suppression.derive_email_hmac("fu.min@empresa.com")
        db.session.commit()
        build_cta, build_unsub = _cta_builders()
        with patch.object(campaign_email, "send_email") as send_mock:
            status = campaign_email.maybe_send_followup_email(
                lead,
                secret_key=TOKEN_SECRET,
                build_cta_url=build_cta,
                build_unsubscribe_url=build_unsub,
            )
        send_mock.assert_not_called()
        assert status == "skipped_lead_email_minimized"
        assert lead.followup_count == 0
        assert lead.last_followup_sent_at is None


def test_27_28_activation_nao_envia_para_minimizado(app):
    _enable_suppression(app)
    with app.app_context():
        converted = _utcnow() - timedelta(days=3)
        lead = _make_converted_lead(
            email="act.min@empresa.com",
            converted_at=converted,
            captured_at=converted - timedelta(hours=1),
        )
        lead.email = lead_minimized_email(lead.id)
        lead.email_hmac = suppression.derive_email_hmac("act.min@empresa.com")
        db.session.commit()
        with patch.object(activation, "send_email") as send_mock:
            s1 = activation.maybe_send_activation_email_1(
                lead,
                secret_key=TOKEN_SECRET,
                build_cta_url=lambda: "https://example.test/a",
                build_unsubscribe_url=lambda token: f"https://example.test/u/{token}",
                now=converted + timedelta(hours=30),
            )
            s2 = activation.maybe_send_activation_email_2(
                lead,
                secret_key=TOKEN_SECRET,
                build_cta_url=lambda: "https://example.test/b",
                build_unsubscribe_url=lambda token: f"https://example.test/u/{token}",
                now=converted + timedelta(hours=80),
            )
        send_mock.assert_not_called()
        assert s1 == "skipped_lead_email_minimized"
        assert s2 == "skipped_lead_email_minimized"
        lead = db.session.get(Lead, lead.id)
        assert lead.activation_email_1_sent_at is None
        assert lead.activation_email_2_sent_at is None


def test_29_30_backfill_usa_hmac_e_nunca_placeholder(app):
    _enable_suppression(app)
    with app.app_context():
        original = "hist.min@empresa.com"
        stamp = datetime(2026, 4, 1, 9, 0, 0)
        lead = _make_terminal_lead(email=original)
        lead.opt_out_at = stamp
        db.session.commit()
        suppression.suppress_email(
            original,
            suppression.PURPOSE_PRE_REGISTRATION,
            suppression.SOURCE_HISTORICAL_CAMPAIGN_OPT_OUT,
            suppressed_at=stamp,
        )
        suppression.suppress_email(
            original,
            suppression.PURPOSE_ACTIVATION,
            suppression.SOURCE_HISTORICAL_CAMPAIGN_OPT_OUT,
            suppressed_at=stamp,
        )
        minimization.run_minimization(apply=True)
        lead = db.session.get(Lead, lead.id)
        placeholder = lead.email
        original_hmac = lead.email_hmac
        before = {
            row.purpose: (row.id, row.email_hmac, row.suppressed_at)
            for row in CommunicationSuppression.query.all()
        }

        report = backfill.run_backfill(apply=True)

        assert report.existing_pre_registration == 1
        assert report.existing_activation == 1
        assert report.created_pre_registration == 0
        for row in CommunicationSuppression.query.all():
            assert row.email_hmac == original_hmac
            assert row.email_hmac != suppression.derive_email_hmac(placeholder)
            assert (row.id, row.email_hmac, row.suppressed_at) == before[row.purpose]


def test_backfill_minimizado_sem_hmac_nao_deriva_placeholder(app):
    _enable_suppression(app)
    with app.app_context():
        stamp = datetime(2026, 4, 2, 9, 0, 0)
        lead = _make_converted_lead(email="hist.bad@empresa.com")
        lead.opt_out_at = stamp
        lead.email = lead_minimized_email(lead.id)
        lead.email_hmac = None
        db.session.commit()
        placeholder = lead.email
        report = backfill.run_backfill(apply=True)
        assert report.conflicts_pre_registration == 1
        assert report.conflicts_activation == 1
        assert report.created_pre_registration == 0
        assert CommunicationSuppression.query.count() == 0
        assert suppression.is_email_suppressed(placeholder, suppression.PURPOSE_PRE_REGISTRATION) is False


def test_31_nao_convertido_mantem_dedupe_plaintext(app):
    _enable_suppression(app)
    with app.app_context():
        first = capturar_lead_para_campanha("dedupe@empresa.com", CAMPANHA_ACESSO_DESKTOP, FONTE_LANDING)
        second = capturar_lead_para_campanha("dedupe@empresa.com", CAMPANHA_ACESSO_DESKTOP, FONTE_LANDING)
        assert first["lead"].id == second["lead"].id
        assert Lead.query.filter_by(email="dedupe@empresa.com").count() == 1


def test_32_33_analytics_preservados(app):
    _enable_suppression(app)
    with app.app_context():
        lead = _make_terminal_lead(email="analytics@empresa.com")
        frozen = _snapshot_analytics(lead)
        uid = lead.converted_user_id
        campaign = lead.acquisition_campaign
        minimization.run_minimization(apply=True)
        lead = db.session.get(Lead, lead.id)
        assert lead.converted_user_id == uid
        assert lead.acquisition_campaign == campaign
        assert _snapshot_analytics(lead) == frozen


def test_34_35_36_cli_dry_run_apply_sem_pii(app):
    _enable_suppression(app)
    with app.app_context():
        original = "cli.user@empresa.com"
        lead = _make_terminal_lead(email=original)
        minimization.register_lead_email_minimization_command(app)
        runner = app.test_cli_runner()

        dry = runner.invoke(args=["lead-email-minimization"])
        assert dry.exit_code == 0
        assert "MODE=DRY_RUN" in dry.output
        assert "STATUS=OK" in dry.output
        lead = db.session.get(Lead, lead.id)
        assert lead.email == original
        assert lead.email_hmac is None
        _assert_no_pii(dry.output, original)
        assert "email_hmac=" not in dry.output
        assert "lead_id=" not in dry.output

        applied = runner.invoke(args=["lead-email-minimization", "--apply"])
        assert applied.exit_code == 0
        assert applied.output.splitlines()[0] == "MODE=APPLY"
        assert "STATUS=OK" in applied.output
        lead = db.session.get(Lead, lead.id)
        assert is_lead_email_minimized(lead)
        _assert_no_pii(applied.output, original)
        assert lead.email_hmac not in applied.output
        assert "lead_id=" not in applied.output
        assert "user_id=" not in applied.output


def test_cli_secret_ausente_apply_aborta(app):
    _disable_suppression(app)
    with app.app_context():
        lead = _make_terminal_lead(email="cli.nosecret@empresa.com")
        minimization.register_lead_email_minimization_command(app)
        runner = app.test_cli_runner()
        result = runner.invoke(args=["lead-email-minimization", "--apply"])
        assert result.exit_code == 1
        assert "MODE=APPLY" in result.output
        assert "STATUS=ABORTED" in result.output
        lead = db.session.get(Lead, lead.id)
        assert lead.email == "cli.nosecret@empresa.com"
        assert lead.email_hmac is None
        _assert_no_pii(result.output, "cli.nosecret@empresa.com")


def test_dry_run_secret_ausente_declara_indisponibilidade(app):
    _disable_suppression(app)
    with app.app_context():
        _make_terminal_lead(email="dry.nosecret@empresa.com")
        report = minimization.run_minimization()
        assert report.suppression_hmac_unavailable is True
        assert report.would_minimize == 0
        assert report.minimized == 0


def test_converted_user_operationally_closed_elegivel(app):
    _enable_suppression(app)
    with app.app_context():
        lead = _make_converted_lead(email="closed@empresa.com")
        user = db.session.get(User, lead.converted_user_id)
        user.email = email_operacional_apos_encerramento(user.id)
        db.session.commit()
        decision = minimization.evaluate_lead_email_minimization_eligibility(lead)
        assert decision.status == minimization.STATUS_ELIGIBLE


def test_first_upload_isolado_nao_elegivel(app):
    _enable_suppression(app)
    with app.app_context():
        from app.funnel_event_service import (
            FUNNEL_EVENT_FILE_UPLOADED,
            FUNNEL_SOURCE_CLEIDE_AUDIT,
        )
        from app.models import FunnelEvent

        lead = _make_converted_lead(email="upload.only@empresa.com")
        user = db.session.get(User, lead.converted_user_id)
        db.session.add(
            FunnelEvent(
                user_id=user.id,
                conta_id=user.conta_id,
                franquia_id=user.franquia_id,
                event_name=FUNNEL_EVENT_FILE_UPLOADED,
                source=FUNNEL_SOURCE_CLEIDE_AUDIT,
                idempotency_key="lead-r2-upload-only",
            )
        )
        db.session.commit()
        decision = minimization.evaluate_lead_email_minimization_eligibility(lead)
        assert decision.status == minimization.STATUS_SKIPPED_ACTIVATION_ACTIVE


def test_activation_email_2_isolado_nao_elegivel(app):
    _enable_suppression(app)
    with app.app_context():
        lead = _make_converted_lead(email="e2.only@empresa.com")
        lead.activation_email_1_sent_at = _utcnow() - timedelta(days=3)
        lead.activation_email_2_sent_at = _utcnow() - timedelta(days=1)
        db.session.commit()
        decision = minimization.evaluate_lead_email_minimization_eligibility(lead)
        assert decision.status == minimization.STATUS_SKIPPED_ACTIVATION_ACTIVE


def test_newsletter_nao_alterada(app):
    _enable_suppression(app)
    with app.app_context():
        original = "news.keep@empresa.com"
        lead = _make_terminal_lead(email=original)
        sub = NewsletterSubscription(
            email=original,
            subscribed_at=_utcnow(),
            source=NewsletterSubscription.SOURCE_PUBLIC_NEWSLETTER,
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
        db.session.add(sub)
        db.session.commit()
        minimization.run_minimization(apply=True)
        assert NewsletterSubscription.query.count() == 1
        persisted = NewsletterSubscription.query.one()
        assert persisted.email == original
        assert persisted.unsubscribed_at is None


def test_reconcile_converted_nao_precisa_plaintext(app):
    _enable_suppression(app)
    with app.app_context():
        lead = _make_terminal_lead(email="recon@empresa.com")
        uid = lead.converted_user_id
        minimization.run_minimization(apply=True)
        lead = db.session.get(Lead, lead.id)
        status = reconcile_lead(lead)
        assert status == "already_converted"
        assert lead.converted_user_id == uid


def test_derive_email_hmac_unica_autoridade(app):
    _enable_suppression(app)
    with app.app_context():
        digest = suppression.derive_email_hmac("  User@Example.COM ")
        ok = suppression.suppress_email_hmac(
            digest,
            suppression.PURPOSE_PRE_REGISTRATION,
            suppression.SOURCE_CAMPAIGN_UNSUBSCRIBE,
        )
        assert ok is True
        row = CommunicationSuppression.query.one()
        assert row.email_hmac == digest
        assert row.email_hmac == suppression.derive_email_hmac("user@example.com")


def test_suppress_email_hmac_rejeita_digest_invalido(app):
    _enable_suppression(app)
    with app.app_context():
        assert suppression.suppress_email_hmac(
            "not-hex",
            suppression.PURPOSE_PRE_REGISTRATION,
            suppression.SOURCE_CAMPAIGN_UNSUBSCRIBE,
        ) is False
        assert suppression.suppress_email_hmac(
            "A" * 64,
            suppression.PURPOSE_PRE_REGISTRATION,
            suppression.SOURCE_CAMPAIGN_UNSUBSCRIBE,
        ) is True
        row = CommunicationSuppression.query.one()
        assert row.email_hmac == "a" * 64
        assert CommunicationSuppression.query.count() == 1


def test_r21_plaintext_hmac_none_minimiza_normalmente(app):
    _enable_suppression(app)
    with app.app_context():
        original = "r21.none@empresa.com"
        lead = _make_terminal_lead(email=original)
        assert lead.email_hmac is None
        lead_id = lead.id
        report = minimization.run_minimization(apply=True)
        lead = db.session.get(Lead, lead_id)
        assert report.minimized == 1
        assert is_lead_email_minimized(lead)
        assert lead.email_hmac == suppression.derive_email_hmac(original)


def test_r21_plaintext_hmac_correto_minimiza_sem_alterar_digest(app):
    _enable_suppression(app)
    with app.app_context():
        original = "r21.match@empresa.com"
        lead = _make_terminal_lead(email=original)
        stored = suppression.derive_email_hmac(original)
        lead.email_hmac = stored
        db.session.commit()
        lead_id = lead.id
        report = minimization.run_minimization(apply=True)
        lead = db.session.get(Lead, lead_id)
        assert report.minimized == 1
        assert is_lead_email_minimized(lead)
        assert lead.email_hmac == stored
        assert lead.email_hmac == suppression.derive_email_hmac(original)
        assert CommunicationSuppression.query.count() == 0


def test_r21_plaintext_hmac_divergente_conflito_sem_mutacao(app):
    _enable_suppression(app)
    with app.app_context():
        original = "r21.mismatch@empresa.com"
        lead = _make_terminal_lead(email=original)
        divergente = suppression.derive_email_hmac("outro.digest@empresa.com")
        lead.email_hmac = divergente
        db.session.commit()
        lead_id = lead.id
        before_supp = CommunicationSuppression.query.count()
        decision = minimization.evaluate_lead_email_minimization_eligibility(lead)
        assert decision.status == minimization.STATUS_CONFLICT
        assert decision.reason == minimization.REASON_CONFLICT_EMAIL_HMAC_MISMATCH

        dry = minimization.run_minimization()
        lead = db.session.get(Lead, lead_id)
        assert dry.mode == "DRY_RUN"
        assert dry.conflicts == 1
        assert dry.would_minimize == 0
        assert dry.minimized == 0
        assert lead.email == original
        assert lead.email_hmac == divergente

        with pytest.raises(
            minimization.LeadEmailMinimizationAborted,
            match="conflict_email_hmac_mismatch",
        ):
            minimization.run_minimization(apply=True)
        lead = db.session.get(Lead, lead_id)
        assert lead.email == original
        assert lead.email_hmac == divergente
        assert not is_lead_email_minimized(lead)
        assert CommunicationSuppression.query.count() == before_supp


def test_r21_plaintext_hmac_invalido_conflito(app):
    _enable_suppression(app)
    with app.app_context():
        original = "r21.invalid@empresa.com"
        lead = _make_terminal_lead(email=original)
        lead.email_hmac = "not-a-valid-hmac-digest"
        db.session.commit()
        lead_id = lead.id
        decision = minimization.evaluate_lead_email_minimization_eligibility(lead)
        assert decision.status == minimization.STATUS_CONFLICT
        assert decision.reason == minimization.REASON_CONFLICT_EMAIL_HMAC_INVALID
        with pytest.raises(
            minimization.LeadEmailMinimizationAborted,
            match="conflict_email_hmac_invalid",
        ):
            minimization.run_minimization(apply=True)
        lead = db.session.get(Lead, lead_id)
        assert lead.email == original
        assert lead.email_hmac == "not-a-valid-hmac-digest"
        assert CommunicationSuppression.query.count() == 0


def test_r21_batch_abort_nao_minimiza_irmao_valido(app):
    _enable_suppression(app)
    with app.app_context():
        ok = _make_terminal_lead(email="r21.ok.batch@empresa.com")
        bad = _make_terminal_lead(email="r21.bad.batch@empresa.com")
        bad.email_hmac = suppression.derive_email_hmac("r21.other@empresa.com")
        db.session.commit()
        ok_id = ok.id
        bad_id = bad.id
        original_ok = ok.email
        original_bad = bad.email
        stored_bad = bad.email_hmac
        with pytest.raises(minimization.LeadEmailMinimizationAborted):
            minimization.run_minimization(apply=True)
        ok = db.session.get(Lead, ok_id)
        bad = db.session.get(Lead, bad_id)
        assert ok.email == original_ok
        assert ok.email_hmac is None
        assert not is_lead_email_minimized(ok)
        assert bad.email == original_bad
        assert bad.email_hmac == stored_bad
        assert not is_lead_email_minimized(bad)


def test_r21_already_minimized_permanece_idempotente(app):
    _enable_suppression(app)
    with app.app_context():
        lead = _make_terminal_lead(email="r21.idemp@empresa.com")
        minimization.run_minimization(apply=True)
        lead = db.session.get(Lead, lead.id)
        frozen_email = lead.email
        frozen_hmac = lead.email_hmac
        second = minimization.run_minimization(apply=True)
        lead = db.session.get(Lead, lead.id)
        assert second.already_minimized == 1
        assert second.minimized == 0
        assert second.conflicts == 0
        assert lead.email == frozen_email
        assert lead.email_hmac == frozen_hmac
        assert is_lead_email_minimized(lead)
