"""Testes da Etapa 1 — fundação de aquisição de Lead (sem landing/CTA/e-mail)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.extensions import db
from app.models import Lead, NewsletterSubscription
from app.news_ai import registrar_newsletter_subscription
from app.run_cleiton_agente_retencao import limpar_dados_antigos
from app.services.lead_acquisition_service import capturar_lead_para_campanha


CAMPAIGN_A = "acq_free_2026_q3"
CAMPAIGN_B = "acq_other_campaign"
SOURCE_LANDING = "landing"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _assert_journey_null(lead: Lead) -> None:
    assert lead.cta_email_sent_at is None
    assert lead.cta_clicked_at is None
    assert lead.converted_user_id is None
    assert lead.converted_at is None
    assert lead.last_followup_sent_at is None
    assert lead.opt_out_at is None


def test_captura_novo_lead(app):
    with app.app_context():
        result = capturar_lead_para_campanha(
            "novo@empresa.com",
            CAMPAIGN_A,
            SOURCE_LANDING,
        )
        lead = result["lead"]

        assert result["status"] == "created"
        assert Lead.query.count() == 1
        assert lead.email == "novo@empresa.com"
        assert lead.acquisition_campaign == CAMPAIGN_A
        assert lead.acquisition_source == SOURCE_LANDING
        assert lead.campaign_captured_at is not None
        assert lead.followup_count == 0
        assert lead.data_inscricao is not None
        _assert_journey_null(lead)


def test_captura_lead_newsletter_preexistente(app):
    with app.app_context():
        old_inscricao = _utcnow() - timedelta(days=200)
        existing = Lead(email="antigo@empresa.com", data_inscricao=old_inscricao)
        db.session.add(existing)
        db.session.commit()
        lead_id = existing.id

        before = _utcnow()
        result = capturar_lead_para_campanha(
            "antigo@empresa.com",
            CAMPAIGN_A,
            SOURCE_LANDING,
        )
        after = _utcnow()

        lead = result["lead"]
        assert result["status"] == "assigned"
        assert Lead.query.count() == 1
        assert lead.id == lead_id
        assert lead.email == "antigo@empresa.com"
        assert lead.data_inscricao == old_inscricao
        assert lead.acquisition_campaign == CAMPAIGN_A
        assert lead.acquisition_source == SOURCE_LANDING
        assert lead.campaign_captured_at is not None
        assert before <= lead.campaign_captured_at <= after
        assert lead.followup_count == 0


def test_repeticao_mesma_campanha_idempotente(app):
    with app.app_context():
        first = capturar_lead_para_campanha(
            "rep@empresa.com",
            CAMPAIGN_A,
            SOURCE_LANDING,
        )
        lead = first["lead"]
        captured_at = lead.campaign_captured_at
        cta_email_sent_at = _utcnow() - timedelta(days=1)
        cta_clicked_at = _utcnow() - timedelta(hours=20)
        last_followup_sent_at = _utcnow() - timedelta(hours=10)
        opt_out_at = _utcnow() - timedelta(hours=3)
        converted_at = _utcnow() - timedelta(hours=2)
        lead.followup_count = 2
        lead.cta_email_sent_at = cta_email_sent_at
        lead.cta_clicked_at = cta_clicked_at
        lead.last_followup_sent_at = last_followup_sent_at
        lead.opt_out_at = opt_out_at
        lead.converted_user_id = 99
        lead.converted_at = converted_at
        db.session.commit()

        second = capturar_lead_para_campanha(
            "rep@empresa.com",
            CAMPAIGN_A,
            "outra_source",
        )
        lead2 = second["lead"]

        assert second["status"] == "already_in_campaign"
        assert Lead.query.count() == 1
        assert lead2.id == lead.id
        assert lead2.campaign_captured_at == captured_at
        assert lead2.followup_count == 2
        assert lead2.cta_email_sent_at == cta_email_sent_at
        assert lead2.cta_clicked_at == cta_clicked_at
        assert lead2.last_followup_sent_at == last_followup_sent_at
        assert lead2.opt_out_at == opt_out_at
        assert lead2.converted_user_id == 99
        assert lead2.converted_at == converted_at
        assert lead2.acquisition_source == SOURCE_LANDING


def test_variacao_caixa_email_idempotente(app):
    with app.app_context():
        first = capturar_lead_para_campanha(
            "Usuario@Empresa.com",
            CAMPAIGN_A,
            SOURCE_LANDING,
        )
        second = capturar_lead_para_campanha(
            "usuario@empresa.com",
            CAMPAIGN_A,
            SOURCE_LANDING,
        )

        assert Lead.query.count() == 1
        assert first["lead"].id == second["lead"].id
        assert second["status"] == "already_in_campaign"
        assert first["lead"].email == "usuario@empresa.com"


def test_atribuicao_campanha_diferente_nao_sobrescreve(app):
    with app.app_context():
        first = capturar_lead_para_campanha(
            "lock@empresa.com",
            CAMPAIGN_A,
            SOURCE_LANDING,
        )
        captured_at = first["lead"].campaign_captured_at

        second = capturar_lead_para_campanha(
            "lock@empresa.com",
            CAMPAIGN_B,
            "ads",
        )

        lead = second["lead"]
        assert second["status"] == "campaign_mismatch"
        assert Lead.query.count() == 1
        assert lead.acquisition_campaign == CAMPAIGN_A
        assert lead.acquisition_source == SOURCE_LANDING
        assert lead.campaign_captured_at == captured_at


def test_retencao_preserva_newsletter_antiga_com_campanha_recente(app):
    with app.app_context():
        old = _utcnow() - timedelta(days=600)
        recent = _utcnow() - timedelta(days=5)
        lead = Lead(
            email="keep@empresa.com",
            data_inscricao=old,
            acquisition_campaign=CAMPAIGN_A,
            acquisition_source=SOURCE_LANDING,
            campaign_captured_at=recent,
            followup_count=0,
        )
        db.session.add(lead)
        db.session.commit()
        lead_id = lead.id

        with patch(
            "app.run_cleiton_agente_retencao.get_retencao_meses_dados",
            return_value=18,
        ):
            limpar_dados_antigos(app)

        assert Lead.query.filter_by(id=lead_id).first() is not None


def test_retencao_remove_lead_realmente_antigo(app):
    with app.app_context():
        old = _utcnow() - timedelta(days=600)
        lead_null = Lead(
            email="purge-null@empresa.com",
            data_inscricao=old,
            campaign_captured_at=None,
            followup_count=0,
        )
        lead_old_campaign = Lead(
            email="purge-old@empresa.com",
            data_inscricao=old,
            acquisition_campaign=CAMPAIGN_A,
            campaign_captured_at=old,
            followup_count=0,
        )
        db.session.add_all([lead_null, lead_old_campaign])
        db.session.commit()

        with patch(
            "app.run_cleiton_agente_retencao.get_retencao_meses_dados",
            return_value=18,
        ):
            limpar_dados_antigos(app)

        assert Lead.query.filter_by(email="purge-null@empresa.com").first() is None
        assert Lead.query.filter_by(email="purge-old@empresa.com").first() is None


def test_newsletter_continua_funcionando_sem_campanha(app):
    with app.app_context():
        ok, msg = registrar_newsletter_subscription("news@empresa.com")
        assert ok is True
        row = NewsletterSubscription.query.filter_by(email="news@empresa.com").first()
        assert row is not None
        assert row.unsubscribed_at is None
        assert Lead.query.count() == 0

        ok2, _ = registrar_newsletter_subscription("news@empresa.com")
        assert ok2 is True
        assert NewsletterSubscription.query.count() == 1
        assert Lead.query.count() == 0
        assert msg


def test_campanha_depois_newsletter_preserva_aquisicao(app):
    with app.app_context():
        first = capturar_lead_para_campanha(
            "camp-news@empresa.com",
            CAMPAIGN_A,
            SOURCE_LANDING,
        )
        lead = first["lead"]
        lead_id = lead.id
        captured_at = lead.campaign_captured_at
        cta_email_sent_at = _utcnow() - timedelta(days=2)
        cta_clicked_at = _utcnow() - timedelta(days=1)
        converted_at = _utcnow() - timedelta(hours=6)
        last_followup_sent_at = _utcnow() - timedelta(hours=4)
        opt_out_at = None
        lead.cta_email_sent_at = cta_email_sent_at
        lead.cta_clicked_at = cta_clicked_at
        lead.converted_user_id = 42
        lead.converted_at = converted_at
        lead.followup_count = 1
        lead.last_followup_sent_at = last_followup_sent_at
        lead.opt_out_at = opt_out_at
        db.session.commit()

        ok, _ = registrar_newsletter_subscription("camp-news@empresa.com")
        lead2 = Lead.query.filter_by(id=lead_id).first()

        assert ok is True
        assert Lead.query.count() == 1
        assert NewsletterSubscription.query.filter_by(email="camp-news@empresa.com").first() is not None
        assert lead2 is not None
        assert lead2.id == lead_id
        assert lead2.acquisition_campaign == CAMPAIGN_A
        assert lead2.acquisition_source == SOURCE_LANDING
        assert lead2.campaign_captured_at == captured_at
        assert lead2.cta_email_sent_at == cta_email_sent_at
        assert lead2.cta_clicked_at == cta_clicked_at
        assert lead2.converted_user_id == 42
        assert lead2.converted_at == converted_at
        assert lead2.followup_count == 1
        assert lead2.last_followup_sent_at == last_followup_sent_at
        assert lead2.opt_out_at is None


def test_campanha_depois_newsletter_casing_diferente_preserva(app):
    with app.app_context():
        first = capturar_lead_para_campanha(
            "usuario@empresa.com",
            CAMPAIGN_A,
            SOURCE_LANDING,
        )
        lead_id = first["lead"].id
        captured_at = first["lead"].campaign_captured_at

        ok, _ = registrar_newsletter_subscription("Usuario@Empresa.com")
        lead = Lead.query.filter_by(id=lead_id).first()

        assert ok is True
        assert Lead.query.count() == 1
        assert lead.email == "usuario@empresa.com"
        assert lead.acquisition_campaign == CAMPAIGN_A
        assert lead.acquisition_source == SOURCE_LANDING
        assert lead.campaign_captured_at == captured_at


def test_newsletter_nova_persiste_email_lowercase(app):
    with app.app_context():
        ok, _ = registrar_newsletter_subscription("Usuario@Empresa.com")
        row = NewsletterSubscription.query.first()

        assert ok is True
        assert NewsletterSubscription.query.count() == 1
        assert Lead.query.count() == 0
        assert row.email == "usuario@empresa.com"


def test_newsletter_repeticao_casing_diferente_idempotente(app):
    with app.app_context():
        ok1, _ = registrar_newsletter_subscription("Usuario@Empresa.com")
        ok2, _ = registrar_newsletter_subscription("usuario@empresa.com")
        rows = NewsletterSubscription.query.all()

        assert ok1 is True
        assert ok2 is True
        assert len(rows) == 1
        assert rows[0].email == "usuario@empresa.com"
        assert Lead.query.count() == 0
