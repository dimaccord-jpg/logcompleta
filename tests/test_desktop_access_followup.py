"""Testes da Etapa 4 — follow-up único e runner desktop_access."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app.extensions import db
from app.models import Lead, User
from app.services.lead_acquisition_service import (
    CAMPANHA_ACESSO_DESKTOP,
    FONTE_LANDING,
)
from app.services import lead_campaign_email_service as campaign_email
from app.services.lead_campaign_conversion_service import reconcile_desktop_access_leads
from app.run_desktop_access_followup import executar_desktop_access_followup
from tests.conftest import seed_sistema_interno, seed_usuario


SECRET = "test-secret-desktop-followup"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


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


def _eligible_cta_sent_at() -> datetime:
    return _utcnow() - timedelta(hours=campaign_email.FOLLOWUP_DELAY_HOURS, minutes=5)


def _url_builders():
    return (
        lambda token: f"https://example.test/acesso-desktop/continuar/{token}",
        lambda token: f"https://example.test/acesso-desktop/descadastrar/{token}",
    )


def _process(lead_or_none=None, **kwargs):
    build_cta, build_unsub = _url_builders()
    return campaign_email.process_eligible_followups(
        secret_key=SECRET,
        build_cta_url=build_cta,
        build_unsubscribe_url=build_unsub,
        **kwargs,
    )


def _send_one(lead, **kwargs):
    build_cta, build_unsub = _url_builders()
    return campaign_email.maybe_send_followup_email(
        lead,
        secret_key=SECRET,
        build_cta_url=build_cta,
        build_unsubscribe_url=build_unsub,
        **kwargs,
    )


def test_7_antes_de_delay_nao_envia(app):
    with app.app_context():
        _make_lead(
            email="cedo@empresa.com",
            cta_email_sent_at=_utcnow() - timedelta(hours=1),
        )
        with patch.object(campaign_email, "send_email") as send_mock:
            stats = _process()
            send_mock.assert_not_called()
            assert stats["candidates"] == 0
            assert stats["sent"] == 0


def test_8_apos_delay_envia(app):
    with app.app_context():
        lead = _make_lead(
            email="apos.delay@empresa.com",
            cta_email_sent_at=_eligible_cta_sent_at(),
        )
        with patch.object(campaign_email, "send_email") as send_mock:
            status = _send_one(lead)
            send_mock.assert_called_once()
            assert status == "sent"
            lead = db.session.get(Lead, lead.id)
            assert lead.followup_count == 1
            assert lead.last_followup_sent_at is not None
            assert lead.cta_email_sent_at is not None


def test_9_maximo_um_followup(app):
    with app.app_context():
        _make_lead(
            email="maximo@empresa.com",
            cta_email_sent_at=_eligible_cta_sent_at(),
            followup_count=campaign_email.MAX_FOLLOWUPS,
            last_followup_sent_at=_utcnow() - timedelta(days=2),
        )
        with patch.object(campaign_email, "send_email") as send_mock:
            stats = _process()
            send_mock.assert_not_called()
            assert stats["candidates"] == 0


def test_10_opt_out_suprime(app):
    with app.app_context():
        _make_lead(
            email="opt.fu@empresa.com",
            cta_email_sent_at=_eligible_cta_sent_at(),
            opt_out_at=_utcnow() - timedelta(hours=1),
        )
        with patch.object(campaign_email, "send_email") as send_mock:
            stats = _process()
            send_mock.assert_not_called()
            assert stats["candidates"] == 0


def test_11_convertido_suprime(app):
    with app.app_context():
        _make_lead(
            email="conv.fu@empresa.com",
            cta_email_sent_at=_eligible_cta_sent_at(),
            converted_user_id=42,
            converted_at=_utcnow() - timedelta(hours=1),
        )
        with patch.object(campaign_email, "send_email") as send_mock:
            stats = _process()
            send_mock.assert_not_called()
            assert stats["candidates"] == 0


def test_12_sem_cta_inicial_nao_envia(app):
    with app.app_context():
        _make_lead(
            email="sem.cta@empresa.com",
            cta_email_sent_at=None,
        )
        with patch.object(campaign_email, "send_email") as send_mock:
            stats = _process()
            send_mock.assert_not_called()
            assert stats["candidates"] == 0


def test_13_falha_sender_mantem_elegivel(app):
    with app.app_context():
        lead = _make_lead(
            email="falha.fu@empresa.com",
            cta_email_sent_at=_eligible_cta_sent_at(),
        )
        with patch.object(
            campaign_email,
            "send_email",
            side_effect=RuntimeError("falha resend"),
        ) as send_mock:
            status = _send_one(lead)
            send_mock.assert_called_once()
            assert status == "failed"
            lead = db.session.get(Lead, lead.id)
            assert lead.followup_count == 0
            assert lead.last_followup_sent_at is None

        with patch.object(campaign_email, "send_email") as send_retry:
            status2 = _send_one(lead)
            send_retry.assert_called_once()
            assert status2 == "sent"
            lead = db.session.get(Lead, lead.id)
            assert lead.followup_count == 1
            assert lead.last_followup_sent_at is not None


def test_14_clique_sem_cadastro_continua_elegivel(app):
    with app.app_context():
        lead = _make_lead(
            email="clicou@empresa.com",
            cta_email_sent_at=_eligible_cta_sent_at(),
            cta_clicked_at=_utcnow() - timedelta(hours=2),
        )
        with patch.object(campaign_email, "send_email") as send_mock:
            status = _send_one(lead)
            send_mock.assert_called_once()
            assert status == "sent"
            lead = db.session.get(Lead, lead.id)
            assert lead.followup_count == 1
            assert lead.cta_clicked_at is not None


def test_15_user_aparece_antes_do_followup_recheck(app):
    """Candidato vira User antes do envio: marca conversão e não chama sender."""
    with app.app_context():
        lead = _make_lead(
            email="race@empresa.com",
            cta_email_sent_at=_eligible_cta_sent_at(),
        )
        # User criado após seleção conceitual do candidato.
        user = _make_user("race@empresa.com")

        with patch.object(campaign_email, "send_email") as send_mock:
            status = _send_one(lead)
            send_mock.assert_not_called()
            assert status == "skipped_converted"
            lead = db.session.get(Lead, lead.id)
            assert lead.converted_user_id == user.id
            assert lead.converted_at is not None
            assert lead.followup_count == 0
            assert lead.last_followup_sent_at is None


def test_16_outra_campanha_ignorada_no_followup(app):
    with app.app_context():
        _make_lead(
            email="outra.fu@empresa.com",
            acquisition_campaign="outra_campanha",
            cta_email_sent_at=_eligible_cta_sent_at(),
        )
        with patch.object(campaign_email, "send_email") as send_mock:
            stats = _process()
            send_mock.assert_not_called()
            assert stats["candidates"] == 0


def test_17_conteudo_followup_cta_unsubscribe_sem_creditos(app):
    with app.app_context():
        lead = _make_lead(
            email="conteudo.fu@empresa.com",
            cta_email_sent_at=_eligible_cta_sent_at(),
        )
        with patch.object(campaign_email, "send_email") as send_mock:
            _send_one(lead)
            kwargs = send_mock.call_args.kwargs
            assert kwargs["subject"] == campaign_email.FOLLOWUP_EMAIL_SUBJECT
            html = kwargs["html"]
            text = kwargs["text"]
            assert "Continuar meu cadastro" in html
            assert "Cancelar mensagens" in html
            assert "/acesso-desktop/continuar/" in html
            assert "/acesso-desktop/descadastrar/" in html
            assert "/acesso-desktop/continuar/" in text
            assert "/acesso-desktop/descadastrar/" in text
            for blob in (html, text):
                assert "50" not in blob
                assert "bônus" not in blob.lower()
                assert "promocao" not in blob.lower() and "promoção" not in blob.lower()
                assert "créditos" not in blob.lower() and "creditos" not in blob.lower()
                assert "conteudo.fu@empresa.com" not in blob
                assert "Auditar meu primeiro frete" not in blob
                assert "pagando o que foi acordado" not in blob.lower()


def test_18_ordem_reconciliacao_antes_followup(app):
    """Orquestrador: reconciliação ocorre antes do processamento de follow-up."""
    call_order: list[str] = []

    def _recon():
        call_order.append("reconcile")
        return {"examined": 0, "converted": 0}

    def _follow(**kwargs):
        call_order.append("followup")
        return {"candidates": 0, "sent": 0}

    fake_app = MagicMock()
    fake_app.config = {"SECRET_KEY": SECRET}
    ctx = MagicMock()
    fake_app.app_context.return_value = ctx
    ctx.__enter__ = MagicMock(return_value=None)
    ctx.__exit__ = MagicMock(return_value=False)

    with patch(
        "app.services.lead_campaign_conversion_service.reconcile_desktop_access_leads",
        side_effect=_recon,
    ) as m_recon, patch(
        "app.services.lead_campaign_email_service.process_eligible_followups",
        side_effect=_follow,
    ) as m_follow, patch(
        "app.run_desktop_access_followup._build_url_helpers",
        return_value=(lambda t: f"cta:{t}", lambda t: f"unsub:{t}"),
    ), patch(
        "app.services.admin_desktop_access_test_service.is_admin_test_env_allowed",
        return_value=False,
    ):
        result = executar_desktop_access_followup(fake_app)

    assert call_order == ["reconcile", "followup"]
    assert m_recon.called
    assert m_follow.called
    assert result["reconciliation"]["examined"] == 0
    assert result["followup"]["candidates"] == 0


def test_18b_ordem_com_app_real_e_efeito(app):
    """Orquestrador real: reconcilia User antigo antes de considerar follow-up."""
    with app.app_context():
        known = _utcnow() - timedelta(days=30)
        user = _make_user("ordem@empresa.com", created_at=known)
        lead = _make_lead(
            email="ordem@empresa.com",
            cta_email_sent_at=_eligible_cta_sent_at(),
        )

        app.config["SECRET_KEY"] = SECRET
        app.config["SERVER_NAME"] = "localhost"

        if "acesso_desktop_continuar" not in app.view_functions:
            app.add_url_rule(
                "/acesso-desktop/continuar/<token>",
                endpoint="acesso_desktop_continuar",
                view_func=lambda token: ("ok", 200),
            )
        if "acesso_desktop_descadastrar" not in app.view_functions:
            app.add_url_rule(
                "/acesso-desktop/descadastrar/<token>",
                endpoint="acesso_desktop_descadastrar",
                view_func=lambda token: ("ok", 200),
            )

        with patch.object(campaign_email, "send_email") as send_mock:
            summary = executar_desktop_access_followup(app)
            send_mock.assert_not_called()
            lead = db.session.get(Lead, lead.id)
            assert lead.converted_user_id == user.id
            assert lead.converted_at == known
            assert lead.followup_count == 0
            assert summary["reconciliation"]["converted"] >= 1
            assert summary["followup"]["sent"] == 0


def test_user_antigo_fica_inelegivel_ao_followup(app):
    with app.app_context():
        _make_user("inelegivel@empresa.com", created_at=_utcnow() - timedelta(days=10))
        lead = _make_lead(
            email="inelegivel@empresa.com",
            cta_email_sent_at=_eligible_cta_sent_at(),
        )
        reconcile_desktop_access_leads()
        with patch.object(campaign_email, "send_email") as send_mock:
            stats = _process()
            send_mock.assert_not_called()
            assert stats["candidates"] == 0
            lead = db.session.get(Lead, lead.id)
            assert lead.converted_user_id is not None
