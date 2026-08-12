"""Testes da Etapa 4 — reconciliação Lead → User (conversão)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.extensions import db
from app.models import Lead, User
from app.services.lead_acquisition_service import (
    CAMPANHA_ACESSO_DESKTOP,
    FONTE_LANDING,
)
from app.services.lead_campaign_conversion_service import (
    STATUS_ALREADY,
    STATUS_CONVERTED,
    STATUS_NO_USER,
    STATUS_SKIPPED_CAMPAIGN,
    reconcile_desktop_access_leads,
    reconcile_lead,
)
from tests.conftest import seed_sistema_interno, seed_usuario


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


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


def test_1_lead_sem_user_nao_converte(app):
    with app.app_context():
        lead = _make_lead(email="sem.user@empresa.com")
        status = reconcile_lead(lead)
        assert status == STATUS_NO_USER
        assert lead.converted_user_id is None
        assert lead.converted_at is None


def test_2_lead_com_user_correspondente(app):
    with app.app_context():
        known = _utcnow() - timedelta(days=3)
        user = _make_user("match@empresa.com", created_at=known)
        lead = _make_lead(email="match@empresa.com")

        status = reconcile_lead(lead)
        db.session.commit()

        assert status == STATUS_CONVERTED
        assert lead.converted_user_id == user.id
        assert lead.converted_at == known


def test_3_match_case_insensitive(app):
    with app.app_context():
        user = _make_user("Case.User@Empresa.com")
        # Lead pode ter sido persistido com casing diferente em anomalia;
        # lookup normaliza.
        lead = _make_lead(email="case.user@empresa.com")
        # Força casing distinto no Lead sem reescrever via service de aquisição.
        lead.email = "CASE.USER@EMPRESA.COM"
        db.session.commit()

        status = reconcile_lead(lead)
        db.session.commit()
        assert status == STATUS_CONVERTED
        assert lead.converted_user_id == user.id


def test_4_idempotencia_reconcilacao(app):
    with app.app_context():
        known = _utcnow() - timedelta(days=5)
        user = _make_user("idem@empresa.com", created_at=known)
        lead = _make_lead(email="idem@empresa.com")

        first = reconcile_lead(lead)
        db.session.commit()
        assert first == STATUS_CONVERTED
        converted_at = lead.converted_at
        converted_user_id = lead.converted_user_id

        second = reconcile_lead(lead)
        db.session.commit()
        assert second == STATUS_ALREADY
        assert lead.converted_user_id == converted_user_id == user.id
        assert lead.converted_at == converted_at == known


def test_5_user_anterior_a_campanha(app):
    with app.app_context():
        old_created = _utcnow() - timedelta(days=90)
        user = _make_user("antigo@empresa.com", created_at=old_created)
        captured = _utcnow() - timedelta(hours=1)
        lead = _make_lead(
            email="antigo@empresa.com",
            campaign_captured_at=captured,
            cta_email_sent_at=captured,
        )

        stats = reconcile_desktop_access_leads()
        lead = db.session.get(Lead, lead.id)
        assert stats["converted"] == 1
        assert lead.converted_user_id == user.id
        assert lead.converted_at == old_created
        # Timestamp histórico verdadeiro, mesmo anterior à captura.
        assert lead.converted_at < lead.campaign_captured_at


def test_6_opt_out_ainda_converte(app):
    with app.app_context():
        user = _make_user("opt.conv@empresa.com")
        lead = _make_lead(
            email="opt.conv@empresa.com",
            opt_out_at=_utcnow() - timedelta(hours=2),
        )

        status = reconcile_lead(lead)
        db.session.commit()
        assert status == STATUS_CONVERTED
        assert lead.converted_user_id == user.id
        assert lead.converted_at is not None
        assert lead.opt_out_at is not None


def test_converted_at_usa_created_at_do_user(app):
    """Semântica: converted_at == User.created_at (timestamp canônico)."""
    with app.app_context():
        known = datetime(2026, 1, 15, 12, 30, 0)
        user = _make_user("canon@empresa.com", created_at=known)
        lead = _make_lead(email="canon@empresa.com")

        reconcile_lead(lead)
        db.session.commit()
        assert lead.converted_at == user.created_at == known


def test_16_outra_campanha_ignorada_na_reconcilacao(app):
    with app.app_context():
        user = _make_user("outra.camp@empresa.com")
        lead = _make_lead(
            email="outra.camp@empresa.com",
            acquisition_campaign="outra_campanha",
        )

        status = reconcile_lead(lead)
        assert status == STATUS_SKIPPED_CAMPAIGN
        assert lead.converted_user_id is None

        stats = reconcile_desktop_access_leads()
        lead = db.session.get(Lead, lead.id)
        assert stats["examined"] == 0
        assert lead.converted_user_id is None
        assert user.id is not None


def test_lote_reconciliacao_contagens(app):
    with app.app_context():
        _make_user("lote.a@empresa.com")
        _make_lead(email="lote.a@empresa.com")
        _make_lead(email="lote.b@empresa.com")  # sem user

        stats = reconcile_desktop_access_leads()
        assert stats["examined"] == 2
        assert stats["converted"] == 1
        assert stats["no_user"] == 1
