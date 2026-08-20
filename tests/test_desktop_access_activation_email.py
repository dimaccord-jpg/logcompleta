"""Testes da sequência de ativação pós-cadastro desktop_access (24h + 48h)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.extensions import db
from app.funnel_event_service import (
    FUNNEL_EVENT_FILE_UPLOADED,
    FUNNEL_SOURCE_AGENTE_COMPARA,
    FUNNEL_SOURCE_CLEIDE_AUDIT,
    record_funnel_event,
)
from app.models import Lead, User
from app.services.lead_acquisition_service import (
    CAMPANHA_ACESSO_DESKTOP,
    FONTE_LANDING,
)
from app.services import desktop_access_activation_email_service as activation
from tests.conftest import seed_sistema_interno, seed_usuario


SECRET = "test-secret-desktop-activation"


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


def _make_converted_lead(
    *,
    email: str = "ativacao@empresa.com",
    captured_at: datetime | None = None,
    converted_at: datetime | None = None,
    campaign: str = CAMPANHA_ACESSO_DESKTOP,
    user: User | None = None,
    **kwargs,
) -> Lead:
    captured = captured_at if captured_at is not None else _utcnow() - timedelta(days=2)
    converted = converted_at if converted_at is not None else captured + timedelta(hours=1)
    if user is None:
        user = _make_user(email, created_at=converted)
    defaults = {
        "email": email,
        "acquisition_campaign": campaign,
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


def _url_builders():
    return (
        lambda: "https://example.test/auditoria-frete",
        lambda: "https://example.test/agente-compara",
        lambda token: f"https://example.test/acesso-desktop/descadastrar/{token}",
    )


def _send_e1(lead, **kwargs):
    e1, _e2, unsub = _url_builders()
    return activation.maybe_send_activation_email_1(
        lead,
        secret_key=SECRET,
        build_cta_url=e1,
        build_unsubscribe_url=unsub,
        **kwargs,
    )


def _send_e2(lead, **kwargs):
    _e1, e2, unsub = _url_builders()
    return activation.maybe_send_activation_email_2(
        lead,
        secret_key=SECRET,
        build_cta_url=e2,
        build_unsubscribe_url=unsub,
        **kwargs,
    )


def _process(**kwargs):
    e1, e2, unsub = _url_builders()
    return activation.process_eligible_activation_emails(
        secret_key=SECRET,
        build_email_1_cta_url=e1,
        build_email_2_cta_url=e2,
        build_unsubscribe_url=unsub,
        **kwargs,
    )


def _add_upload(user: User, *, source: str, key: str):
    record_funnel_event(
        event_name=FUNNEL_EVENT_FILE_UPLOADED,
        source=source,
        user_id=user.id,
        conta_id=user.conta_id,
        franquia_id=user.franquia_id,
        idempotency_key=key,
    )


def test_1_apenas_desktop_access(app):
    with app.app_context():
        _make_converted_lead(
            email="outra.camp@empresa.com",
            campaign="outra_campanha",
            converted_at=_utcnow() - timedelta(hours=25),
            captured_at=_utcnow() - timedelta(days=2),
        )
        with patch.object(activation, "send_email") as send_mock:
            stats = _process()
            send_mock.assert_not_called()
            assert stats["email1_sent"] == 0


def test_2_registration_valida_necessaria(app):
    with app.app_context():
        user = _make_user("sem.conv@empresa.com")
        lead = Lead(
            email="sem.conv@empresa.com",
            acquisition_campaign=CAMPANHA_ACESSO_DESKTOP,
            acquisition_source=FONTE_LANDING,
            campaign_captured_at=_utcnow() - timedelta(days=1),
            converted_user_id=None,
            converted_at=None,
            followup_count=0,
        )
        db.session.add(lead)
        db.session.commit()
        with patch.object(activation, "send_email") as send_mock:
            assert _send_e1(lead) == "skipped"
            send_mock.assert_not_called()


def test_3_user_preexistente_excluido(app):
    with app.app_context():
        captured = _utcnow() - timedelta(hours=1)
        # User criado antes da captura da campanha
        _make_converted_lead(
            email="preexistente@empresa.com",
            captured_at=captured,
            converted_at=captured - timedelta(days=30),
        )
        with patch.object(activation, "send_email") as send_mock:
            stats = _process(now=_utcnow() + timedelta(hours=30))
            send_mock.assert_not_called()
            assert stats["email1_candidates"] == 0


def test_4_antes_24h_e1_nao_envia(app):
    with app.app_context():
        converted = _utcnow() - timedelta(hours=23, minutes=59, seconds=59)
        lead = _make_converted_lead(
            email="cedo.e1@empresa.com",
            converted_at=converted,
            captured_at=converted - timedelta(hours=1),
        )
        with patch.object(activation, "send_email") as send_mock:
            status = _send_e1(lead, now=converted + timedelta(hours=23, minutes=59, seconds=59))
            send_mock.assert_not_called()
            assert status == "skipped"


def test_5_igual_24h_e1_envia(app):
    with app.app_context():
        converted = _utcnow() - timedelta(days=2)
        lead = _make_converted_lead(
            email="exato.e1@empresa.com",
            converted_at=converted,
            captured_at=converted - timedelta(hours=1),
        )
        now = converted + timedelta(hours=24)
        with patch.object(activation, "send_email") as send_mock:
            status = _send_e1(lead, now=now)
            send_mock.assert_called_once()
            assert status == "sent"
            lead = db.session.get(Lead, lead.id)
            assert lead.activation_email_1_sent_at == now
            kwargs = send_mock.call_args.kwargs
            assert kwargs["subject"] == activation.ACTIVATION_EMAIL_1_SUBJECT
            assert "Auditar meu primeiro frete" in kwargs["html"]
            assert "/auditoria-frete" in kwargs["html"]
            assert "Cancelar estes lembretes" in kwargs["html"]
            assert kwargs.get("attachments")
            assert any(
                a.get("content_id") == activation.ACTIVATION_EMAIL_1_CID
                for a in kwargs["attachments"]
            )


def test_6_e1_nao_duplica(app):
    with app.app_context():
        converted = _utcnow() - timedelta(days=3)
        lead = _make_converted_lead(
            email="dup.e1@empresa.com",
            converted_at=converted,
            captured_at=converted - timedelta(hours=1),
            activation_email_1_sent_at=converted + timedelta(hours=24),
        )
        with patch.object(activation, "send_email") as send_mock:
            status = _send_e1(lead, now=converted + timedelta(hours=50))
            send_mock.assert_not_called()
            assert status == "skipped"


def test_7_antes_48h_e2_nao_envia(app):
    with app.app_context():
        e1_sent = _utcnow() - timedelta(hours=47, minutes=59, seconds=59)
        lead = _make_converted_lead(
            email="cedo.e2@empresa.com",
            converted_at=e1_sent - timedelta(hours=24),
            captured_at=e1_sent - timedelta(hours=25),
            activation_email_1_sent_at=e1_sent,
        )
        with patch.object(activation, "send_email") as send_mock:
            status = _send_e2(
                lead, now=e1_sent + timedelta(hours=47, minutes=59, seconds=59)
            )
            send_mock.assert_not_called()
            assert status == "skipped"


def test_8_igual_48h_e2_envia(app):
    with app.app_context():
        e1_sent = _utcnow() - timedelta(days=3)
        lead = _make_converted_lead(
            email="exato.e2@empresa.com",
            converted_at=e1_sent - timedelta(hours=24),
            captured_at=e1_sent - timedelta(hours=25),
            activation_email_1_sent_at=e1_sent,
        )
        now = e1_sent + timedelta(hours=48)
        with patch.object(activation, "send_email") as send_mock:
            status = _send_e2(lead, now=now)
            send_mock.assert_called_once()
            assert status == "sent"
            lead = db.session.get(Lead, lead.id)
            assert lead.activation_email_2_sent_at == now
            kwargs = send_mock.call_args.kwargs
            assert kwargs["subject"] == activation.ACTIVATION_EMAIL_2_SUBJECT
            assert "Comparar minhas tabelas" in kwargs["html"]
            assert "/agente-compara" in kwargs["html"]
            assert "Cancelar estes lembretes" in kwargs["html"]


def test_9_e2_nao_duplica(app):
    with app.app_context():
        e1_sent = _utcnow() - timedelta(days=5)
        lead = _make_converted_lead(
            email="dup.e2@empresa.com",
            converted_at=e1_sent - timedelta(hours=24),
            captured_at=e1_sent - timedelta(hours=25),
            activation_email_1_sent_at=e1_sent,
            activation_email_2_sent_at=e1_sent + timedelta(hours=48),
        )
        with patch.object(activation, "send_email") as send_mock:
            status = _send_e2(lead, now=e1_sent + timedelta(days=10))
            send_mock.assert_not_called()
            assert status == "skipped"


def test_10_upload_cleide_antes_e1(app):
    with app.app_context():
        converted = _utcnow() - timedelta(days=2)
        lead = _make_converted_lead(
            email="up.cleide.antes@empresa.com",
            converted_at=converted,
            captured_at=converted - timedelta(hours=1),
        )
        user = db.session.get(User, lead.converted_user_id)
        _add_upload(user, source=FUNNEL_SOURCE_CLEIDE_AUDIT, key="up-cleide-antes-1")
        with patch.object(activation, "send_email") as send_mock:
            status = _send_e1(lead, now=converted + timedelta(hours=24))
            send_mock.assert_not_called()
            assert status == "skipped_upload"
            lead = db.session.get(Lead, lead.id)
            assert lead.activation_email_1_sent_at is None
            assert lead.activation_email_2_sent_at is None


def test_11_upload_compara_antes_e1(app):
    with app.app_context():
        converted = _utcnow() - timedelta(days=2)
        lead = _make_converted_lead(
            email="up.compara.antes@empresa.com",
            converted_at=converted,
            captured_at=converted - timedelta(hours=1),
        )
        user = db.session.get(User, lead.converted_user_id)
        _add_upload(user, source=FUNNEL_SOURCE_AGENTE_COMPARA, key="up-compara-antes-1")
        with patch.object(activation, "send_email") as send_mock:
            status = _send_e1(lead, now=converted + timedelta(hours=24))
            send_mock.assert_not_called()
            assert status == "skipped_upload"


def test_12_upload_cleide_entre_e1_e2(app):
    with app.app_context():
        e1_sent = _utcnow() - timedelta(days=3)
        lead = _make_converted_lead(
            email="up.cleide.meio@empresa.com",
            converted_at=e1_sent - timedelta(hours=24),
            captured_at=e1_sent - timedelta(hours=25),
            activation_email_1_sent_at=e1_sent,
        )
        user = db.session.get(User, lead.converted_user_id)
        _add_upload(user, source=FUNNEL_SOURCE_CLEIDE_AUDIT, key="up-cleide-meio-1")
        with patch.object(activation, "send_email") as send_mock:
            status = _send_e2(lead, now=e1_sent + timedelta(hours=48))
            send_mock.assert_not_called()
            assert status == "skipped_upload"
            lead = db.session.get(Lead, lead.id)
            assert lead.activation_email_2_sent_at is None


def test_13_upload_compara_entre_e1_e2(app):
    with app.app_context():
        e1_sent = _utcnow() - timedelta(days=3)
        lead = _make_converted_lead(
            email="up.compara.meio@empresa.com",
            converted_at=e1_sent - timedelta(hours=24),
            captured_at=e1_sent - timedelta(hours=25),
            activation_email_1_sent_at=e1_sent,
        )
        user = db.session.get(User, lead.converted_user_id)
        _add_upload(user, source=FUNNEL_SOURCE_AGENTE_COMPARA, key="up-compara-meio-1")
        with patch.object(activation, "send_email") as send_mock:
            status = _send_e2(lead, now=e1_sent + timedelta(hours=48))
            send_mock.assert_not_called()
            assert status == "skipped_upload"


def test_14_opt_out_anterior_campanha(app):
    with app.app_context():
        converted = _utcnow() - timedelta(days=2)
        lead = _make_converted_lead(
            email="opt.camp@empresa.com",
            converted_at=converted,
            captured_at=converted - timedelta(hours=1),
            opt_out_at=converted - timedelta(hours=2),
        )
        with patch.object(activation, "send_email") as send_mock:
            status = _send_e1(lead, now=converted + timedelta(hours=24))
            send_mock.assert_not_called()
            assert status == "skipped_opt_out"


def test_15_activation_opt_out(app):
    with app.app_context():
        converted = _utcnow() - timedelta(days=2)
        lead = _make_converted_lead(
            email="opt.act@empresa.com",
            converted_at=converted,
            captured_at=converted - timedelta(hours=1),
            activation_opt_out_at=converted + timedelta(hours=1),
        )
        with patch.object(activation, "send_email") as send_mock:
            status = _send_e1(lead, now=converted + timedelta(hours=24))
            send_mock.assert_not_called()
            assert status == "skipped_opt_out"


def test_16_provider_failure_e1(app):
    with app.app_context():
        converted = _utcnow() - timedelta(days=2)
        lead = _make_converted_lead(
            email="fail.e1@empresa.com",
            converted_at=converted,
            captured_at=converted - timedelta(hours=1),
        )
        with patch.object(
            activation, "send_email", side_effect=RuntimeError("resend fail")
        ):
            status = _send_e1(lead, now=converted + timedelta(hours=24))
            assert status == "failed"
            lead = db.session.get(Lead, lead.id)
            assert lead.activation_email_1_sent_at is None


def test_17_provider_failure_e2(app):
    with app.app_context():
        e1_sent = _utcnow() - timedelta(days=3)
        lead = _make_converted_lead(
            email="fail.e2@empresa.com",
            converted_at=e1_sent - timedelta(hours=24),
            captured_at=e1_sent - timedelta(hours=25),
            activation_email_1_sent_at=e1_sent,
        )
        with patch.object(
            activation, "send_email", side_effect=RuntimeError("resend fail")
        ):
            status = _send_e2(lead, now=e1_sent + timedelta(hours=48))
            assert status == "failed"
            lead = db.session.get(Lead, lead.id)
            assert lead.activation_email_2_sent_at is None


def test_18_recheck_antes_do_send(app):
    with app.app_context():
        converted = _utcnow() - timedelta(days=2)
        lead = _make_converted_lead(
            email="recheck@empresa.com",
            converted_at=converted,
            captured_at=converted - timedelta(hours=1),
        )
        user = db.session.get(User, lead.converted_user_id)
        now = converted + timedelta(hours=24)

        original_recheck = activation._recheck_before_send

        def _recheck_with_upload(lead_arg, *, which, now=None):
            _add_upload(user, source=FUNNEL_SOURCE_CLEIDE_AUDIT, key="recheck-up-1")
            return original_recheck(lead_arg, which=which, now=now)

        with patch.object(activation, "_recheck_before_send", side_effect=_recheck_with_upload), patch.object(
            activation, "send_email"
        ) as send_mock:
            # should_send passes initially; recheck sees upload
            # Force path: patch should_send to True then recheck suppresses
            with patch.object(activation, "should_send_activation_email_1", return_value=True):
                status = _send_e1(lead, now=now)
            send_mock.assert_not_called()
            assert status == "skipped_upload"


def test_19_e1_destination_cleide(app):
    built = activation.build_activation_email_1(
        cta_url="https://example.test/auditoria-frete",
        unsubscribe_url="https://example.test/acesso-desktop/descadastrar/tok",
    )
    assert "/auditoria-frete" in built["html"]
    assert activation.ACTIVATION_EMAIL_1_CTA in built["html"]
    assert "cid:" + activation.ACTIVATION_EMAIL_1_CID in built["html"]
    assert activation.ACTIVATION_EMAIL_1_HERO_ALT in built["html"]
    assert activation.ACTIVATION_EMAIL_1_PREHEADER in built["html"]
    assert activation.ACTIVATION_EMAIL_1_HEADLINE in built["html"]
    assert activation.ACTIVATION_EMAIL_1_HEADLINE in built["text"]
    assert "https://example.test/auditoria-frete" in built["text"]
    assert "Cancelar estes lembretes" in built["html"]
    assert "Cancelar estes lembretes" in built["text"]


def test_20_e2_destination_agente_compara(app):
    built = activation.build_activation_email_2(
        cta_url="https://example.test/agente-compara",
        unsubscribe_url="https://example.test/acesso-desktop/descadastrar/tok",
    )
    assert "/agente-compara" in built["html"]
    assert activation.ACTIVATION_EMAIL_2_CTA in built["html"]
    assert "cid:" + activation.ACTIVATION_EMAIL_2_CID in built["html"]
    assert activation.ACTIVATION_EMAIL_2_HERO_ALT in built["html"]
    assert activation.ACTIVATION_EMAIL_2_PREHEADER in built["html"]
    assert activation.ACTIVATION_EMAIL_2_HEADLINE in built["text"]


def test_21_unsubscribe_nos_dois(app):
    for builder in (
        activation.build_activation_email_1,
        activation.build_activation_email_2,
    ):
        built = builder(
            cta_url="https://example.test/cta",
            unsubscribe_url="https://example.test/acesso-desktop/descadastrar/tok",
        )
        assert "Cancelar estes lembretes" in built["html"]
        assert "/acesso-desktop/descadastrar/" in built["html"]
        assert "/acesso-desktop/descadastrar/" in built["text"]


def test_22_sem_percentual_nao_comprovado(app):
    forbidden = (
        "economize",
        "pagando x%",
        "% a mais",
        "gastam",
        "economia de",
    )
    for builder in (
        activation.build_activation_email_1,
        activation.build_activation_email_2,
    ):
        built = builder(
            cta_url="https://example.test/cta",
            unsubscribe_url="https://example.test/unsub",
        )
        for blob in (built["text"], built["subject"], built["preheader"]):
            for needle in forbidden:
                assert needle not in blob.lower()
            # Percentuais de claim no texto (não CSS).
            assert not any(
                ch.isdigit() and "%" in blob[i : i + 4]
                for i, ch in enumerate(blob)
                if ch.isdigit()
            )


def test_23_paridade_builders_e2e_usam_mesmos(app):
    real1 = activation.build_activation_email_1(
        cta_url="https://x/auditoria-frete",
        unsubscribe_url="https://x/descadastrar/AAA",
    )
    e2e1 = activation.build_activation_email_1(
        cta_url="https://x/auditoria-frete",
        unsubscribe_url="https://x/descadastrar/BBB",
    )
    assert real1["subject"] == e2e1["subject"]
    assert real1["html"].replace("AAA", "TOK") == e2e1["html"].replace("BBB", "TOK")
    assert real1["text"].replace("AAA", "TOK") == e2e1["text"].replace("BBB", "TOK")

    real2 = activation.build_activation_email_2(
        cta_url="https://x/agente-compara",
        unsubscribe_url="https://x/descadastrar/AAA",
    )
    e2e2 = activation.build_activation_email_2(
        cta_url="https://x/agente-compara",
        unsubscribe_url="https://x/descadastrar/BBB",
    )
    assert real2["subject"] == e2e2["subject"]
    assert real2["html"].replace("AAA", "TOK") == e2e2["html"].replace("BBB", "TOK")


def test_24_activation_opt_out_via_token(app):
    with app.app_context():
        lead = _make_converted_lead(email="tok.opt@empresa.com")
        token = activation.generate_activation_unsubscribe_token(
            lead.id, secret_key=SECRET
        )
        resolved = activation.resolve_lead_for_activation_unsubscribe_token(
            token, secret_key=SECRET
        )
        assert resolved is not None
        assert resolved.id == lead.id
        activation.apply_activation_opt_out(lead)
        lead = db.session.get(Lead, lead.id)
        assert lead.activation_opt_out_at is not None
        assert lead.opt_out_at is None


def test_25_nao_interfere_followup_pre_cadastro(app):
    from app.services import lead_campaign_email_service as campaign_email

    with app.app_context():
        # Lead ainda sem cadastro — só pré-cadastro
        lead = Lead(
            email="pre.only@empresa.com",
            acquisition_campaign=CAMPANHA_ACESSO_DESKTOP,
            acquisition_source=FONTE_LANDING,
            campaign_captured_at=_utcnow() - timedelta(days=2),
            cta_email_sent_at=_utcnow() - timedelta(hours=30),
            followup_count=0,
        )
        db.session.add(lead)
        db.session.commit()
        with patch.object(activation, "send_email") as act_mock, patch.object(
            campaign_email, "send_email"
        ) as fu_mock:
            act_stats = _process()
            assert act_stats["email1_candidates"] == 0
            act_mock.assert_not_called()
            status = campaign_email.maybe_send_followup_email(
                lead,
                secret_key=SECRET,
                build_cta_url=lambda t: f"https://x/continuar/{t}",
                build_unsubscribe_url=lambda t: f"https://x/descadastrar/{t}",
            )
            assert status == "sent"
            fu_mock.assert_called_once()
            assert "Continuar meu cadastro" in fu_mock.call_args.kwargs["html"]


def test_26_jornada_encerrada_email1_nao_envia(app):
    with app.app_context():
        converted = _utcnow() - timedelta(days=2)
        lead = _make_converted_lead(
            email="ended.e1@empresa.com",
            converted_at=converted,
            captured_at=converted - timedelta(hours=1),
        )
        lead.activation_ended_at = converted + timedelta(hours=2)
        lead.activation_ended_for_user_id = lead.converted_user_id
        db.session.commit()
        now = converted + timedelta(hours=24)
        with patch.object(activation, "send_email") as send_mock:
            status = _send_e1(lead, now=now)
            send_mock.assert_not_called()
            assert status == "skipped_journey_ended"
            assert activation.should_send_activation_email_1(lead, now=now) is False
            assert activation.is_activation_journey_ended(lead) is True
            assert activation.is_activation_opted_out(lead) is False
            lead = db.session.get(Lead, lead.id)
            assert lead.activation_email_1_sent_at is None
            assert lead.opt_out_at is None
            assert lead.activation_opt_out_at is None


def test_27_jornada_encerrada_email2_nao_envia(app):
    with app.app_context():
        e1_sent = _utcnow() - timedelta(days=3)
        lead = _make_converted_lead(
            email="ended.e2@empresa.com",
            converted_at=e1_sent - timedelta(hours=24),
            captured_at=e1_sent - timedelta(hours=25),
            activation_email_1_sent_at=e1_sent,
        )
        lead.activation_ended_at = e1_sent + timedelta(hours=1)
        lead.activation_ended_for_user_id = lead.converted_user_id
        db.session.commit()
        now = e1_sent + timedelta(hours=48)
        with patch.object(activation, "send_email") as send_mock:
            status = _send_e2(lead, now=now)
            send_mock.assert_not_called()
            assert status == "skipped_journey_ended"
            assert activation.should_send_activation_email_2(lead, now=now) is False
            lead = db.session.get(Lead, lead.id)
            assert lead.activation_email_2_sent_at is None
            assert lead.opt_out_at is None
            assert lead.activation_opt_out_at is None


def test_28_user_historico_encerrado_bloqueia_sem_backfill(app):
    with app.app_context():
        converted = _utcnow() - timedelta(days=2)
        lead = _make_converted_lead(
            email="hist.closed@empresa.com",
            converted_at=converted,
            captured_at=converted - timedelta(hours=1),
        )
        user = db.session.get(User, lead.converted_user_id)
        user.email = f"encerrado_{user.id}@anon.local"
        db.session.commit()
        now = converted + timedelta(hours=24)
        with patch.object(activation, "send_email") as send_mock:
            status = _send_e1(lead, now=now)
            send_mock.assert_not_called()
            assert status == "skipped_journey_ended"
            lead = db.session.get(Lead, lead.id)
            assert lead.activation_ended_at is None
            assert lead.activation_ended_for_user_id is None
            assert lead.opt_out_at is None
            assert lead.activation_opt_out_at is None
            assert activation.is_activation_journey_ended(lead) is False
            assert activation.is_activation_journey_unavailable(lead) is True


def test_29_encerramento_de_outro_user_nao_bloqueia_jornada_atual(app):
    with app.app_context():
        converted = _utcnow() - timedelta(days=2)
        lead = _make_converted_lead(
            email="other.ended@empresa.com",
            converted_at=converted,
            captured_at=converted - timedelta(hours=1),
        )
        lead.activation_ended_at = converted + timedelta(hours=1)
        lead.activation_ended_for_user_id = int(lead.converted_user_id) + 999
        db.session.commit()
        now = converted + timedelta(hours=24)
        assert activation.is_activation_journey_ended(lead) is False
        with patch.object(activation, "send_email") as send_mock:
            status = _send_e1(lead, now=now)
            send_mock.assert_called_once()
            assert status == "sent"


def test_30_recheck_barra_quando_user_encerra_antes_do_send(app):
    with app.app_context():
        from app.services.user_lifecycle_service import encerrar_vinculo_operacional_usuario

        converted = _utcnow() - timedelta(days=2)
        lead = _make_converted_lead(
            email="recheck.closed@empresa.com",
            converted_at=converted,
            captured_at=converted - timedelta(hours=1),
        )
        user = db.session.get(User, lead.converted_user_id)
        now = converted + timedelta(hours=24)
        original_recheck = activation._recheck_before_send

        def _recheck_with_close(lead_arg, *, which, now=None):
            encerrar_vinculo_operacional_usuario(user)
            return original_recheck(lead_arg, which=which, now=now)

        with patch.object(
            activation, "_recheck_before_send", side_effect=_recheck_with_close
        ), patch.object(activation, "send_email") as send_mock:
            with patch.object(activation, "should_send_activation_email_1", return_value=True):
                status = _send_e1(lead, now=now)
            send_mock.assert_not_called()
            assert status == "skipped_journey_ended"
            lead = db.session.get(Lead, lead.id)
            assert lead.activation_email_1_sent_at is None
            assert lead.opt_out_at is None
            assert lead.activation_opt_out_at is None
