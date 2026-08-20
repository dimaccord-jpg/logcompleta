"""Pacote 4A/4C-A — communication suppression (HMAC, atomicidade, fail-closed)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import CommunicationSuppression, Lead, User
from app.services.lead_acquisition_service import (
    CAMPANHA_ACESSO_DESKTOP,
    FONTE_LANDING,
)
from app.services import communication_suppression_service as suppression
from app.services import desktop_access_activation_email_service as activation
from app.services import lead_campaign_email_service as campaign_email
from tests.conftest import seed_sistema_interno, seed_usuario


HMAC_SECRET_A = "communication-suppression-test-secret-a-32b"
HMAC_SECRET_B = "communication-suppression-test-secret-b-32b"
TOKEN_SECRET = "test-secret-cta-tokens"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _enable_suppression(app, secret: str = HMAC_SECRET_A) -> None:
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


def _make_converted_lead(*, email: str = "ativacao@empresa.com", **kwargs) -> Lead:
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


def _cta_builders():
    return (
        lambda token: f"https://example.test/acesso-desktop/continuar/{token}",
        lambda token: f"https://example.test/acesso-desktop/descadastrar/{token}",
    )


def _send_cta(lead):
    build_cta, build_unsub = _cta_builders()
    return campaign_email.maybe_send_initial_cta_email(
        lead,
        secret_key=TOKEN_SECRET,
        build_cta_url=build_cta,
        build_unsubscribe_url=build_unsub,
    )


def _send_followup(lead, **kwargs):
    build_cta, build_unsub = _cta_builders()
    return campaign_email.maybe_send_followup_email(
        lead,
        secret_key=TOKEN_SECRET,
        build_cta_url=build_cta,
        build_unsubscribe_url=build_unsub,
        **kwargs,
    )


def _send_activation_e1(lead, **kwargs):
    return activation.maybe_send_activation_email_1(
        lead,
        secret_key=TOKEN_SECRET,
        build_cta_url=lambda: "https://example.test/auditoria-frete",
        build_unsubscribe_url=lambda token: (
            f"https://example.test/acesso-desktop/descadastrar/{token}"
        ),
        **kwargs,
    )


def _send_activation_e2(lead, **kwargs):
    return activation.maybe_send_activation_email_2(
        lead,
        secret_key=TOKEN_SECRET,
        build_cta_url=lambda: "https://example.test/agente-compara",
        build_unsubscribe_url=lambda token: (
            f"https://example.test/acesso-desktop/descadastrar/{token}"
        ),
        **kwargs,
    )


def _column_names():
    return {column.name for column in CommunicationSuppression.__table__.columns}


def test_normalize_same_hmac(app):
    _enable_suppression(app)
    with app.app_context():
        ok = suppression.suppress_email(
            " User@Example.COM ",
            suppression.PURPOSE_PRE_REGISTRATION,
            suppression.SOURCE_CAMPAIGN_UNSUBSCRIBE,
        )
        assert ok is True
        assert suppression.is_email_suppressed(
            "user@example.com",
            suppression.PURPOSE_PRE_REGISTRATION,
        )
        assert CommunicationSuppression.query.count() == 1
        row = CommunicationSuppression.query.one()
        assert row.email_hmac == suppression._email_hmac(
            "user@example.com",
            HMAC_SECRET_A,
        )


def test_hmac_determinism(app):
    _enable_suppression(app)
    with app.app_context():
        suppression.suppress_email(
            "mesmo@empresa.com",
            suppression.PURPOSE_PRE_REGISTRATION,
            suppression.SOURCE_CAMPAIGN_UNSUBSCRIBE,
        )
        first = CommunicationSuppression.query.one().email_hmac
        suppression.suppress_email(
            "mesmo@empresa.com",
            suppression.PURPOSE_PRE_REGISTRATION,
            suppression.SOURCE_CAMPAIGN_UNSUBSCRIBE,
        )
        assert CommunicationSuppression.query.count() == 1
        assert CommunicationSuppression.query.one().email_hmac == first
        assert first == suppression._email_hmac("mesmo@empresa.com", HMAC_SECRET_A)


def test_hmac_secret_diferente(app):
    _enable_suppression(app, HMAC_SECRET_A)
    with app.app_context():
        suppression.suppress_email(
            "segredo@empresa.com",
            suppression.PURPOSE_PRE_REGISTRATION,
            suppression.SOURCE_CAMPAIGN_UNSUBSCRIBE,
        )
        hmac_a = CommunicationSuppression.query.one().email_hmac
        _enable_suppression(app, HMAC_SECRET_B)
        assert not suppression.is_email_suppressed(
            "segredo@empresa.com",
            suppression.PURPOSE_PRE_REGISTRATION,
        )
        suppression.suppress_email(
            "segredo@empresa.com",
            suppression.PURPOSE_PRE_REGISTRATION,
            suppression.SOURCE_CAMPAIGN_UNSUBSCRIBE,
        )
        rows = CommunicationSuppression.query.order_by(CommunicationSuppression.id.asc()).all()
        assert len(rows) == 2
        assert rows[0].email_hmac == hmac_a
        assert rows[1].email_hmac != hmac_a
        assert rows[1].email_hmac == suppression._email_hmac(
            "segredo@empresa.com",
            HMAC_SECRET_B,
        )


def test_purposes_independentes(app):
    _enable_suppression(app)
    with app.app_context():
        suppression.suppress_email(
            "duas@empresa.com",
            suppression.PURPOSE_PRE_REGISTRATION,
            suppression.SOURCE_CAMPAIGN_UNSUBSCRIBE,
        )
        assert suppression.is_email_suppressed(
            "duas@empresa.com",
            suppression.PURPOSE_PRE_REGISTRATION,
        )
        assert not suppression.is_email_suppressed(
            "duas@empresa.com",
            suppression.PURPOSE_ACTIVATION,
        )
        suppression.suppress_email(
            "duas@empresa.com",
            suppression.PURPOSE_ACTIVATION,
            suppression.SOURCE_ACTIVATION_UNSUBSCRIBE,
        )
        assert CommunicationSuppression.query.count() == 2
        assert suppression.is_email_suppressed(
            "duas@empresa.com",
            suppression.PURPOSE_ACTIVATION,
        )


def test_upsert_idempotente_preserva_suppressed_at(app):
    _enable_suppression(app)
    with app.app_context():
        first_at = datetime(2026, 1, 15, 12, 0, 0)
        later_at = datetime(2026, 2, 1, 9, 0, 0)
        suppression.suppress_email(
            "idemp@empresa.com",
            suppression.PURPOSE_PRE_REGISTRATION,
            suppression.SOURCE_CAMPAIGN_UNSUBSCRIBE,
            suppressed_at=first_at,
        )
        suppression.suppress_email(
            "idemp@empresa.com",
            suppression.PURPOSE_PRE_REGISTRATION,
            suppression.SOURCE_ACTIVATION_UNSUBSCRIBE,
            suppressed_at=later_at,
        )
        row = CommunicationSuppression.query.one()
        assert row.suppressed_at == first_at
        assert row.source == suppression.SOURCE_CAMPAIGN_UNSUBSCRIBE


def test_row_sem_plaintext_email(app):
    _enable_suppression(app)
    with app.app_context():
        email = "plaintext@empresa.com"
        suppression.suppress_email(
            email,
            suppression.PURPOSE_PRE_REGISTRATION,
            suppression.SOURCE_CAMPAIGN_UNSUBSCRIBE,
        )
        assert _column_names() == {
            "id",
            "email_hmac",
            "purpose",
            "suppressed_at",
            "source",
            "created_at",
        }
        row = CommunicationSuppression.query.one()
        persisted = {name: getattr(row, name) for name in _column_names()}
        assert email not in {str(value) for value in persisted.values()}
        assert "plaintext" not in str(persisted).lower()


def test_gate_pre_registration_suppression_bloqueia_cta(app):
    _enable_suppression(app)
    with app.app_context():
        lead = _make_lead(email="cta.suppressed@empresa.com")
        suppression.suppress_email(
            lead.email,
            suppression.PURPOSE_PRE_REGISTRATION,
            suppression.SOURCE_CAMPAIGN_UNSUBSCRIBE,
        )
        assert lead.opt_out_at is None
        with patch.object(campaign_email, "send_email") as send_mock:
            status = _send_cta(lead)
            send_mock.assert_not_called()
            assert status == "skipped"
            lead = db.session.get(Lead, lead.id)
            assert lead.cta_email_sent_at is None


def test_gate_pre_registration_suppression_bloqueia_followup(app):
    _enable_suppression(app)
    with app.app_context():
        sent_at = _utcnow() - timedelta(hours=campaign_email.FOLLOWUP_DELAY_HOURS, minutes=5)
        lead = _make_lead(
            email="follow.suppressed@empresa.com",
            cta_email_sent_at=sent_at,
        )
        suppression.suppress_email(
            lead.email,
            suppression.PURPOSE_PRE_REGISTRATION,
            suppression.SOURCE_CAMPAIGN_UNSUBSCRIBE,
        )
        assert lead.opt_out_at is None
        with patch.object(campaign_email, "send_email") as send_mock:
            status = _send_followup(lead, now=_utcnow())
            send_mock.assert_not_called()
            assert status == "skipped_opt_out"


def test_gate_activation_suppression_bloqueia_ativacao(app):
    _enable_suppression(app)
    with app.app_context():
        converted = _utcnow() - timedelta(days=2)
        lead = _make_converted_lead(
            email="act.suppressed@empresa.com",
            converted_at=converted,
            captured_at=converted - timedelta(hours=1),
        )
        suppression.suppress_email(
            lead.email,
            suppression.PURPOSE_ACTIVATION,
            suppression.SOURCE_ACTIVATION_UNSUBSCRIBE,
        )
        assert lead.activation_opt_out_at is None
        assert lead.opt_out_at is None
        with patch.object(activation, "send_email") as send_mock:
            status = _send_activation_e1(lead, now=converted + timedelta(hours=24))
            send_mock.assert_not_called()
            assert status == "skipped_opt_out"


def test_pre_registration_suppression_nao_bloqueia_ativacao(app):
    _enable_suppression(app)
    with app.app_context():
        converted = _utcnow() - timedelta(days=2)
        lead = _make_converted_lead(
            email="pre.nao.bloqueia.act@empresa.com",
            converted_at=converted,
            captured_at=converted - timedelta(hours=1),
        )
        suppression.suppress_email(
            lead.email,
            suppression.PURPOSE_PRE_REGISTRATION,
            suppression.SOURCE_CAMPAIGN_UNSUBSCRIBE,
        )
        with patch.object(activation, "send_email") as send_mock:
            status = _send_activation_e1(lead, now=converted + timedelta(hours=24))
            send_mock.assert_called_once()
            assert status == "sent"


def test_activation_suppression_nao_bloqueia_cta_pre_cadastro(app):
    _enable_suppression(app)
    with app.app_context():
        lead = _make_lead(email="act.nao.bloqueia.cta@empresa.com")
        suppression.suppress_email(
            lead.email,
            suppression.PURPOSE_ACTIVATION,
            suppression.SOURCE_ACTIVATION_UNSUBSCRIBE,
        )
        with patch.object(campaign_email, "send_email") as send_mock:
            status = _send_cta(lead)
            send_mock.assert_called_once()
            assert status == "sent"


def test_secret_ausente_opt_out_at_continua_bloqueando(app):
    _disable_suppression(app)
    with app.app_context():
        opt = _utcnow() - timedelta(days=1)
        lead = _make_lead(email="legado.opt@empresa.com", opt_out_at=opt)
        assert suppression.is_suppression_enabled() is False
        assert suppression.is_email_suppressed(
            lead.email,
            suppression.PURPOSE_PRE_REGISTRATION,
        ) is False
        with patch.object(campaign_email, "send_email") as send_mock:
            status = _send_cta(lead)
            send_mock.assert_not_called()
            assert status == "skipped"
        assert CommunicationSuppression.query.count() == 0


def test_secret_ausente_activation_opt_out_at_continua_bloqueando(app):
    _disable_suppression(app)
    with app.app_context():
        converted = _utcnow() - timedelta(days=2)
        lead = _make_converted_lead(
            email="legado.act@empresa.com",
            converted_at=converted,
            captured_at=converted - timedelta(hours=1),
            activation_opt_out_at=converted + timedelta(hours=1),
        )
        assert suppression.is_email_suppressed(
            lead.email,
            suppression.PURPOSE_ACTIVATION,
        ) is False
        with patch.object(activation, "send_email") as send_mock:
            status = _send_activation_e1(lead, now=converted + timedelta(hours=24))
            send_mock.assert_not_called()
            assert status == "skipped_opt_out"


def test_secret_ausente_nao_quebra_opt_out_nem_cria_row(app):
    _disable_suppression(app)
    with app.app_context():
        lead = _make_lead(email="sem.secret@empresa.com")
        campaign_email.apply_campaign_opt_out(lead)
        lead = db.session.get(Lead, lead.id)
        assert lead.opt_out_at is not None
        assert CommunicationSuppression.query.count() == 0
        activation.apply_activation_opt_out(lead)
        lead = db.session.get(Lead, lead.id)
        assert lead.activation_opt_out_at is not None
        assert CommunicationSuppression.query.count() == 0


def test_dual_write_pre_registration(app):
    _enable_suppression(app)
    with app.app_context():
        lead = _make_lead(email="dual.pre@empresa.com")
        campaign_email.apply_campaign_opt_out(lead)
        lead = db.session.get(Lead, lead.id)
        assert lead.opt_out_at is not None
        assert suppression.is_email_suppressed(
            lead.email,
            suppression.PURPOSE_PRE_REGISTRATION,
        )
        assert suppression.is_email_suppressed(
            lead.email,
            suppression.PURPOSE_ACTIVATION,
        )
        rows = CommunicationSuppression.query.order_by(
            CommunicationSuppression.purpose.asc()
        ).all()
        assert len(rows) == 2
        by_purpose = {row.purpose: row for row in rows}
        assert set(by_purpose) == {
            suppression.PURPOSE_ACTIVATION,
            suppression.PURPOSE_PRE_REGISTRATION,
        }
        for row in rows:
            assert row.source == suppression.SOURCE_CAMPAIGN_UNSUBSCRIBE
            assert row.suppressed_at == lead.opt_out_at


def test_dual_write_activation(app):
    _enable_suppression(app)
    with app.app_context():
        lead = _make_converted_lead(email="dual.act@empresa.com")
        activation.apply_activation_opt_out(lead)
        lead = db.session.get(Lead, lead.id)
        assert lead.activation_opt_out_at is not None
        assert lead.opt_out_at is None
        assert suppression.is_email_suppressed(
            lead.email,
            suppression.PURPOSE_ACTIVATION,
        )
        assert not suppression.is_email_suppressed(
            lead.email,
            suppression.PURPOSE_PRE_REGISTRATION,
        )


def test_suppression_nao_altera_newsletter(app):
    _enable_suppression(app)
    with app.app_context():
        conta, franquia = _ensure_conta_franquia()
        user = seed_usuario(franquia.id, conta.id, email="news.suppression@empresa.com")
        user.subscribes_to_newsletter = True
        db.session.commit()
        user_id = user.id
        suppression.suppress_email(
            user.email,
            suppression.PURPOSE_PRE_REGISTRATION,
            suppression.SOURCE_CAMPAIGN_UNSUBSCRIBE,
        )
        suppression.suppress_email(
            user.email,
            suppression.PURPOSE_ACTIVATION,
            suppression.SOURCE_ACTIVATION_UNSUBSCRIBE,
        )
        assert suppression.is_email_suppressed(
            user.email,
            suppression.PURPOSE_PRE_REGISTRATION,
        )
        user = db.session.get(User, user_id)
        assert user.subscribes_to_newsletter is True


def test_apply_campaign_opt_out_idempotente(app):
    _enable_suppression(app)
    with app.app_context():
        lead = _make_lead(email="idemp.pre@empresa.com")
        campaign_email.apply_campaign_opt_out(lead)
        lead = db.session.get(Lead, lead.id)
        first_opt = lead.opt_out_at
        first_rows = {
            row.purpose: (row.suppressed_at, row.source, row.email_hmac)
            for row in CommunicationSuppression.query.all()
        }
        assert set(first_rows) == {
            suppression.PURPOSE_PRE_REGISTRATION,
            suppression.PURPOSE_ACTIVATION,
        }
        campaign_email.apply_campaign_opt_out(lead)
        lead = db.session.get(Lead, lead.id)
        assert lead.opt_out_at == first_opt
        rows = CommunicationSuppression.query.all()
        assert len(rows) == 2
        for row in rows:
            assert (row.suppressed_at, row.source, row.email_hmac) == first_rows[row.purpose]


def test_apply_activation_opt_out_idempotente(app):
    _enable_suppression(app)
    with app.app_context():
        lead = _make_converted_lead(email="idemp.act@empresa.com")
        first_at = datetime(2026, 3, 1, 10, 0, 0)
        activation.apply_activation_opt_out(lead, now=first_at)
        lead = db.session.get(Lead, lead.id)
        assert lead.activation_opt_out_at == first_at
        row = CommunicationSuppression.query.one()
        assert row.purpose == suppression.PURPOSE_ACTIVATION
        assert row.source == suppression.SOURCE_ACTIVATION_UNSUBSCRIBE
        assert row.suppressed_at == first_at
        hmac_value = row.email_hmac
        later = datetime(2026, 4, 1, 10, 0, 0)
        activation.apply_activation_opt_out(lead, now=later)
        lead = db.session.get(Lead, lead.id)
        assert lead.activation_opt_out_at == first_at
        assert lead.opt_out_at is None
        row = CommunicationSuppression.query.one()
        assert row.suppressed_at == first_at
        assert row.source == suppression.SOURCE_ACTIVATION_UNSUBSCRIBE
        assert row.email_hmac == hmac_value


def test_atomicidade_pre_cadastro_falha_segunda_suppression(app):
    _enable_suppression(app)
    with app.app_context():
        lead = _make_lead(email="atom.pre@empresa.com")
        lead_id = lead.id
        real = campaign_email.suppress_email
        calls = {"n": 0}

        def second_fails(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RuntimeError("simulated second suppression failure")
            return real(*args, **kwargs)

        with patch.object(campaign_email, "suppress_email", side_effect=second_fails):
            with pytest.raises(RuntimeError, match="simulated second suppression failure"):
                campaign_email.apply_campaign_opt_out(lead)

        db.session.expire_all()
        lead = db.session.get(Lead, lead_id)
        assert lead.opt_out_at is None
        assert CommunicationSuppression.query.count() == 0


def test_atomicidade_ativacao_falha_suppression(app):
    _enable_suppression(app)
    with app.app_context():
        lead = _make_converted_lead(email="atom.act@empresa.com")
        lead_id = lead.id
        with patch.object(
            activation,
            "suppress_email",
            side_effect=RuntimeError("simulated activation suppression failure"),
        ):
            with pytest.raises(RuntimeError, match="simulated activation suppression failure"):
                activation.apply_activation_opt_out(lead)

        db.session.expire_all()
        lead = db.session.get(Lead, lead_id)
        assert lead.activation_opt_out_at is None
        assert lead.opt_out_at is None
        assert CommunicationSuppression.query.count() == 0


def test_race_unique_savepoint_nao_destroi_transacao_externa(app):
    _enable_suppression(app)
    with app.app_context():
        lead = _make_lead(email="race.unique@empresa.com")
        original_at = datetime(2026, 1, 15, 8, 0, 0)
        suppression.suppress_email(
            lead.email,
            suppression.PURPOSE_PRE_REGISTRATION,
            suppression.SOURCE_CAMPAIGN_UNSUBSCRIBE,
            suppressed_at=original_at,
        )
        lead.followup_count = 9
        real_lookup = suppression._lookup_suppression_row
        calls = {"n": 0}

        def miss_once(digest, purpose):
            calls["n"] += 1
            if calls["n"] == 1:
                return None
            return real_lookup(digest, purpose)

        with patch.object(suppression, "_lookup_suppression_row", side_effect=miss_once):
            ok = suppression.suppress_email(
                lead.email,
                suppression.PURPOSE_PRE_REGISTRATION,
                suppression.SOURCE_ACTIVATION_UNSUBSCRIBE,
                suppressed_at=datetime(2026, 6, 1, 12, 0, 0),
                commit=False,
            )
        assert ok is True
        db.session.commit()
        lead = db.session.get(Lead, lead.id)
        assert lead.followup_count == 9
        row = CommunicationSuppression.query.one()
        assert row.suppressed_at == original_at
        assert row.source == suppression.SOURCE_CAMPAIGN_UNSUBSCRIBE


def test_check_secret_ausente_e_consulta_falha_distintos(app):
    _disable_suppression(app)
    with app.app_context():
        disabled = suppression.check_email_suppression(
            "x@empresa.com",
            suppression.PURPOSE_PRE_REGISTRATION,
        )
        assert disabled.suppressed is False
        assert disabled.available is False
        assert disabled.reason == suppression.REASON_DISABLED
        assert disabled.blocks_send is False
        assert disabled.is_unavailable is False

    _enable_suppression(app)
    with app.app_context():
        with patch.object(
            suppression,
            "_lookup_suppression_row",
            side_effect=RuntimeError("simulated db failure"),
        ):
            failed = suppression.check_email_suppression(
                "x@empresa.com",
                suppression.PURPOSE_PRE_REGISTRATION,
            )
        assert failed.suppressed is False
        assert failed.available is False
        assert failed.reason == suppression.REASON_UNAVAILABLE
        assert failed.blocks_send is True
        assert failed.is_unavailable is True
        assert CommunicationSuppression.query.count() == 0


def test_fail_closed_cta_quando_consulta_falha(app):
    _enable_suppression(app)
    with app.app_context():
        lead = _make_lead(email="failclosed.cta@empresa.com")
        with patch.object(
            suppression,
            "_lookup_suppression_row",
            side_effect=RuntimeError("simulated db failure"),
        ):
            with patch.object(campaign_email, "send_email") as send_mock:
                status = _send_cta(lead)
                send_mock.assert_not_called()
                assert status == "suppression_check_unavailable"
        lead = db.session.get(Lead, lead.id)
        assert lead.opt_out_at is None
        assert lead.cta_email_sent_at is None
        assert CommunicationSuppression.query.count() == 0


def test_fail_closed_followup_quando_consulta_falha(app):
    _enable_suppression(app)
    with app.app_context():
        sent_at = _utcnow() - timedelta(hours=campaign_email.FOLLOWUP_DELAY_HOURS, minutes=5)
        lead = _make_lead(
            email="failclosed.fu@empresa.com",
            cta_email_sent_at=sent_at,
        )
        with patch.object(
            suppression,
            "_lookup_suppression_row",
            side_effect=RuntimeError("simulated db failure"),
        ):
            with patch.object(campaign_email, "send_email") as send_mock:
                status = _send_followup(lead, now=_utcnow())
                send_mock.assert_not_called()
                assert status == "suppression_check_unavailable"
        lead = db.session.get(Lead, lead.id)
        assert lead.opt_out_at is None
        assert lead.followup_count == 0
        assert CommunicationSuppression.query.count() == 0


def test_fail_closed_activation_e1_quando_consulta_falha(app):
    _enable_suppression(app)
    with app.app_context():
        converted = _utcnow() - timedelta(days=2)
        lead = _make_converted_lead(
            email="failclosed.e1@empresa.com",
            converted_at=converted,
            captured_at=converted - timedelta(hours=1),
        )
        with patch.object(
            suppression,
            "_lookup_suppression_row",
            side_effect=RuntimeError("simulated db failure"),
        ):
            with patch.object(activation, "send_email") as send_mock:
                status = _send_activation_e1(lead, now=converted + timedelta(hours=24))
                send_mock.assert_not_called()
                assert status == "suppression_check_unavailable"
        lead = db.session.get(Lead, lead.id)
        assert lead.activation_opt_out_at is None
        assert lead.opt_out_at is None
        assert lead.activation_email_1_sent_at is None
        assert CommunicationSuppression.query.count() == 0


def test_fail_closed_activation_e2_quando_consulta_falha(app):
    _enable_suppression(app)
    with app.app_context():
        converted = _utcnow() - timedelta(days=4)
        e1_sent = converted + timedelta(hours=24)
        lead = _make_converted_lead(
            email="failclosed.e2@empresa.com",
            converted_at=converted,
            captured_at=converted - timedelta(hours=1),
            activation_email_1_sent_at=e1_sent,
        )
        with patch.object(
            suppression,
            "_lookup_suppression_row",
            side_effect=RuntimeError("simulated db failure"),
        ):
            with patch.object(activation, "send_email") as send_mock:
                status = _send_activation_e2(
                    lead,
                    now=e1_sent + timedelta(hours=activation.ACTIVATION_EMAIL_2_DELAY_HOURS),
                )
                send_mock.assert_not_called()
                assert status == "suppression_check_unavailable"
        lead = db.session.get(Lead, lead.id)
        assert lead.activation_opt_out_at is None
        assert lead.opt_out_at is None
        assert lead.activation_email_2_sent_at is None
        assert CommunicationSuppression.query.count() == 0


def test_secret_ausente_lead_sem_opt_out_permite_cta(app):
    _disable_suppression(app)
    with app.app_context():
        lead = _make_lead(email="sem.opt.cta@empresa.com")
        with patch.object(campaign_email, "send_email") as send_mock:
            status = _send_cta(lead)
            send_mock.assert_called_once()
            assert status == "sent"
        lead = db.session.get(Lead, lead.id)
        assert lead.cta_email_sent_at is not None
        assert CommunicationSuppression.query.count() == 0


def test_consulta_falha_nao_loga_email(app, caplog):
    _enable_suppression(app)
    with app.app_context():
        email = "nao.vazar@empresa.com"
        with patch.object(
            suppression,
            "_lookup_suppression_row",
            side_effect=RuntimeError("simulated db failure"),
        ):
            with caplog.at_level("ERROR"):
                suppression.check_email_suppression(
                    email,
                    suppression.PURPOSE_PRE_REGISTRATION,
                )
        joined = " ".join(record.getMessage() for record in caplog.records)
        assert email not in joined
        assert "nao.vazar" not in joined.lower()
        assert HMAC_SECRET_A not in joined
