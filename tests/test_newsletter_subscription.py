"""NEWSLETTER-R1 — NewsletterSubscription independente de Lead."""
from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.auth_services import complete_user_profile, handle_google_oauth_callback, register_user
from app.extensions import db
from app.models import CommunicationSuppression, Lead, NewsletterSubscription, User
from app.news_ai import registrar_newsletter_subscription
from app.services import communication_suppression_service as suppression
from app.services import newsletter_subscription_backfill_service as news_backfill
from app.services.lead_acquisition_service import CAMPANHA_ACESSO_DESKTOP, FONTE_LANDING
from app.services.newsletter_subscription_service import (
    SOURCE_PUBLIC_NEWSLETTER,
    SOURCE_USER_PREFERENCE,
    generate_unsubscribe_token,
    lookup,
    subscribe,
    unsubscribe,
)
from app.services.user_lifecycle_service import (
    email_operacional_apos_encerramento,
    encerrar_vinculo_operacional_usuario,
)
from app.services.user_privacy_rights_service import (
    MODE_APPLY,
    PrivacyRightsAborted,
    processar_exercicio_privacidade_usuario,
)
from tests.conftest import seed_conta_franquia_cliente, seed_usuario

ROOT = Path(__file__).resolve().parents[1]
NEWSLETTER_SECRET = "newsletter-test-secret-key"
HMAC_SECRET = "newsletter-test-hmac-secret-32bxx"
PII_EMAIL = "news.pii@test.com"


def _enable_secret(app) -> None:
    app.config["SECRET_KEY"] = NEWSLETTER_SECRET


def _enable_suppression(app) -> None:
    app.config["COMMUNICATION_SUPPRESSION_HMAC_SECRET"] = HMAC_SECRET


def _snapshot_lead(lead: Lead) -> dict:
    return {
        "id": lead.id,
        "email": lead.email,
        "acquisition_campaign": lead.acquisition_campaign,
        "converted_user_id": lead.converted_user_id,
        "opt_out_at": lead.opt_out_at,
        "activation_opt_out_at": lead.activation_opt_out_at,
        "followup_count": lead.followup_count,
    }


def _assert_no_pii(text: str, *emails: str) -> None:
    lowered = text.lower()
    for email in emails:
        assert email not in text
        assert email.lower() not in lowered
        local = email.split("@", 1)[0]
        if len(local) >= 4:
            assert local.lower() not in lowered


def _make_lead(email: str, **kwargs) -> Lead:
    lead = Lead(email=email, **kwargs)
    db.session.add(lead)
    db.session.commit()
    return lead


def _mount_public_routes(app):
    _enable_secret(app)

    @app.route("/inscrever-newsletter", methods=["POST"])
    def inscrever_newsletter():
        from flask import request

        ok, msg = registrar_newsletter_subscription(request.form.get("email"))
        return msg, (200 if ok else 400)

    @app.route("/newsletter/cancelar/<token>", methods=["GET", "POST"])
    def newsletter_cancelar(token):
        from flask import request

        from app.services.newsletter_subscription_service import (
            resolve_subscription_for_unsubscribe_token,
            signing_secret_key,
            unsubscribe as unsub,
        )

        row = resolve_subscription_for_unsubscribe_token(
            token, secret_key=signing_secret_key()
        )
        if row is None:
            return "invalid", 400
        if request.method == "POST":
            unsub(row.email, commit=True, sync_user_flag=True)
            return "confirmed", 200
        return "pending", 200

    return app.test_client()


def test_inscricao_publica_cria_newsletter_subscription(app, ctx):
    ok, msg = registrar_newsletter_subscription("novo.news@test.com")
    row = NewsletterSubscription.query.one()
    assert ok is True
    assert "confirmada" in msg.lower() or "Bem-vindo" in msg
    assert row.email == "novo.news@test.com"
    assert row.unsubscribed_at is None
    assert row.source == SOURCE_PUBLIC_NEWSLETTER
    assert row.subscribed_at is not None


def test_inscricao_publica_nao_cria_lead(app, ctx):
    registrar_newsletter_subscription("nolead@test.com")
    assert Lead.query.count() == 0
    assert NewsletterSubscription.query.count() == 1


def test_lead_existente_nao_e_modificado(app, ctx):
    lead = _make_lead(
        "keep.lead@test.com",
        acquisition_campaign=CAMPANHA_ACESSO_DESKTOP,
        acquisition_source=FONTE_LANDING,
        converted_user_id=9,
        followup_count=2,
    )
    before = _snapshot_lead(lead)
    registrar_newsletter_subscription("keep.lead@test.com")
    after = _snapshot_lead(db.session.get(Lead, lead.id))
    assert after == before
    assert NewsletterSubscription.query.filter_by(email="keep.lead@test.com").one().is_active


def test_email_normalizado(app, ctx):
    registrar_newsletter_subscription("  User.News@Test.COM  ")
    row = NewsletterSubscription.query.one()
    assert row.email == "user.news@test.com"


def test_inscricao_repetida_idempotente(app, ctx):
    first = subscribe("idemp@test.com", source=SOURCE_PUBLIC_NEWSLETTER)
    stamp = first.subscription.subscribed_at
    second = subscribe("idemp@test.com", source=SOURCE_PUBLIC_NEWSLETTER)
    assert NewsletterSubscription.query.count() == 1
    assert second.status == "already_active"
    assert second.subscription.subscribed_at == stamp
    assert second.subscription.unsubscribed_at is None


def test_unsubscribe(app, ctx):
    subscribe("unsub@test.com", source=SOURCE_PUBLIC_NEWSLETTER)
    result = unsubscribe("unsub@test.com", commit=True)
    row = NewsletterSubscription.query.one()
    assert result.status == "unsubscribed"
    assert row.unsubscribed_at is not None
    assert row.email == "unsub@test.com"


def test_reinscricao_apos_unsubscribe(app, ctx):
    first = subscribe("rejoin@test.com", source=SOURCE_PUBLIC_NEWSLETTER)
    first_stamp = first.subscription.subscribed_at
    unsubscribe("rejoin@test.com", commit=True)
    later = datetime(2026, 8, 18, 18, 0, 0)
    again = subscribe("rejoin@test.com", source=SOURCE_PUBLIC_NEWSLETTER, now=later)
    row = NewsletterSubscription.query.one()
    assert again.status == "reactivated"
    assert row.unsubscribed_at is None
    assert row.subscribed_at == later
    assert row.subscribed_at != first_stamp
    assert row.id == first.subscription.id


def test_newsletter_opt_out_nao_cria_campaign_optout(app, ctx):
    lead = _make_lead("camp.opt@test.com", acquisition_campaign=CAMPANHA_ACESSO_DESKTOP)
    subscribe("camp.opt@test.com", source=SOURCE_PUBLIC_NEWSLETTER)
    unsubscribe("camp.opt@test.com", commit=True, sync_user_flag=True)
    persisted = db.session.get(Lead, lead.id)
    assert persisted.opt_out_at is None
    assert persisted.activation_opt_out_at is None


def test_nao_cria_communication_suppression(app, ctx):
    _enable_suppression(app)
    subscribe("supp.news@test.com", source=SOURCE_PUBLIC_NEWSLETTER)
    unsubscribe("supp.news@test.com", commit=True, sync_user_flag=True)
    assert CommunicationSuppression.query.count() == 0
    assert suppression.is_suppression_enabled() is True


def test_user_opt_in_cria_reativa_subscription(app, ctx, monkeypatch):
    monkeypatch.setattr(
        "app.auth_services._get_free_franquia_limite_onboarding",
        lambda: 50,
    )
    user, err = register_user(
        "Ana",
        "opt.in@test.com",
        "senha123",
        job_role="Analista",
        usage_purpose="Auditoria",
        subscribes_to_newsletter=True,
        accept_terms=True,
    )
    assert err is None
    row = lookup("opt.in@test.com")
    assert user.subscribes_to_newsletter is True
    assert row is not None
    assert row.is_active
    assert row.source == SOURCE_USER_PREFERENCE

    unsubscribe("opt.in@test.com", commit=True)
    complete_user_profile(user, "Analista", "Auditoria", True, accept_terms=True)
    row = lookup("opt.in@test.com")
    assert row.is_active
    assert db.session.get(User, user.id).subscribes_to_newsletter is True


def test_user_opt_out_encerra_subscription(app, ctx, monkeypatch):
    monkeypatch.setattr(
        "app.auth_services._get_free_franquia_limite_onboarding",
        lambda: 50,
    )
    user, err = register_user(
        "Ana",
        "opt.out@test.com",
        "senha123",
        job_role="Analista",
        usage_purpose="Auditoria",
        subscribes_to_newsletter=True,
        accept_terms=True,
    )
    assert err is None
    complete_user_profile(user, "Analista", "Auditoria", False)
    row = lookup("opt.out@test.com")
    persisted = db.session.get(User, user.id)
    assert persisted.subscribes_to_newsletter is False
    assert row is not None
    assert row.unsubscribed_at is not None
    assert CommunicationSuppression.query.count() == 0
    assert Lead.query.count() == 0


def test_oauth_sem_opt_in_nao_cria_subscription(app, ctx, monkeypatch):
    def fake_post(*_a, **_k):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"access_token": "tok"}
        resp.text = ""
        return resp

    def fake_get(*_a, **_k):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "email": "oauth.news@test.com",
            "name": "OAuth User",
            "sub": "sub-oauth-news",
        }
        resp.text = ""
        return resp

    monkeypatch.setattr("app.auth_services.requests.post", fake_post)
    monkeypatch.setattr("app.auth_services.requests.get", fake_get)
    monkeypatch.setattr("app.auth_services._get_free_franquia_limite_onboarding", lambda: 50)
    monkeypatch.setattr("app.auth_services._get_admin_emails", lambda: set())

    user, err, _needs, created = handle_google_oauth_callback(
        code="c",
        state="s",
        session_state="s",
        client_id="id",
        client_secret="sec",
        redirect_uri="http://x",
        token_url="http://token",
        userinfo_url="http://info",
    )
    assert err is None
    assert created is True
    assert user.subscribes_to_newsletter is False
    assert NewsletterSubscription.query.count() == 0


def test_newsletter_only_funciona_sem_user(app, ctx):
    ok, _ = registrar_newsletter_subscription("anon.news@test.com")
    assert ok is True
    assert User.query.count() == 0
    assert NewsletterSubscription.query.one().email == "anon.news@test.com"


def test_unsubscribe_token_nao_contem_email_plaintext(app, ctx):
    _enable_secret(app)
    result = subscribe("token.news@test.com", source=SOURCE_PUBLIC_NEWSLETTER)
    token = generate_unsubscribe_token(result.subscription.id, secret_key=NEWSLETTER_SECRET)
    assert "token.news@test.com" not in token
    assert "TOKEN.NEWS@TEST.COM" not in token
    assert "@" not in token


def test_token_invalido_nao_altera_row(app, ctx):
    _enable_secret(app)
    client = _mount_public_routes(app)
    result = subscribe("keep.active@test.com", source=SOURCE_PUBLIC_NEWSLETTER)
    row_id = result.subscription.id
    resp = client.post("/newsletter/cancelar/token-invalido")
    assert resp.status_code == 400
    persisted = db.session.get(NewsletterSubscription, row_id)
    assert persisted.unsubscribed_at is None


def test_lgpd_r1_encerra_newsletter_ativa(app, ctx):
    conta, franquia, user = _montar_user(email="lgpd.news@test.com")
    subscribe(user.email, source=SOURCE_USER_PREFERENCE)
    original_email = user.email
    processar_exercicio_privacidade_usuario(user, apply=True)
    row = lookup(original_email)
    persisted = db.session.get(User, user.id)
    assert row is not None
    assert row.unsubscribed_at is not None
    assert persisted.email == email_operacional_apos_encerramento(user.id)
    assert persisted.subscribes_to_newsletter is False
    assert CommunicationSuppression.query.count() == 0


def test_lgpd_r1_mantem_transacao_unica(app, ctx):
    _conta, _franquia, user = _montar_user(email="lgpd.tx@test.com")
    subscribe(user.email, source=SOURCE_USER_PREFERENCE)
    with patch.object(db.session, "commit", wraps=db.session.commit) as commit_mock:
        result = processar_exercicio_privacidade_usuario(user, apply=True)
    assert result.mode == MODE_APPLY
    assert commit_mock.call_count == 1


def test_pacote_2_encerra_newsletter_ativa(app, ctx):
    _conta, _franquia, user = _montar_user(email="life.news@test.com")
    subscribe(user.email, source=SOURCE_USER_PREFERENCE)
    original_email = user.email
    uid = user.id
    encerrar_vinculo_operacional_usuario(user)
    row = lookup(original_email)
    persisted = db.session.get(User, uid)
    assert row.unsubscribed_at is not None
    assert persisted.email == email_operacional_apos_encerramento(uid)
    assert persisted.subscribes_to_newsletter is False
    assert CommunicationSuppression.query.count() == 0


def test_pacote_2_mantem_transacao_unica(app, ctx):
    _conta, _franquia, user = _montar_user(email="life.tx@test.com")
    subscribe(user.email, source=SOURCE_USER_PREFERENCE)
    with patch.object(db.session, "commit", wraps=db.session.commit) as commit_mock:
        encerrar_vinculo_operacional_usuario(user)
    assert commit_mock.call_count == 1


def test_erro_faz_rollback_total_lgpd_e_pacote2(app, ctx):
    _conta, _franquia, user = _montar_user(email="roll.lgpd@test.com")
    subscribe(user.email, source=SOURCE_USER_PREFERENCE)
    uid = user.id
    original_email = user.email
    with patch.object(db.session, "commit", side_effect=RuntimeError("db boom")):
        with pytest.raises(PrivacyRightsAborted):
            processar_exercicio_privacidade_usuario(user, apply=True)
    persisted = db.session.get(User, uid)
    row = lookup(original_email)
    assert persisted.email == original_email
    assert row.unsubscribed_at is None

    _conta2, _franquia2, user2 = _montar_user(email="roll.life@test.com", slug="roll-life")
    subscribe(user2.email, source=SOURCE_USER_PREFERENCE)
    uid2 = user2.id
    original2 = user2.email
    with patch.object(db.session, "commit", side_effect=RuntimeError("db boom")):
        with pytest.raises(RuntimeError):
            encerrar_vinculo_operacional_usuario(user2)
    persisted2 = db.session.get(User, uid2)
    row2 = lookup(original2)
    assert persisted2.email == original2
    assert row2.unsubscribed_at is None


def test_lead_historico_permanece_intacto(app, ctx):
    lead = _make_lead(
        "hist.lead@test.com",
        acquisition_campaign=CAMPANHA_ACESSO_DESKTOP,
        converted_user_id=77,
        followup_count=3,
    )
    before = _snapshot_lead(lead)
    registrar_newsletter_subscription("hist.lead@test.com")
    unsubscribe("hist.lead@test.com", commit=True)
    registrar_newsletter_subscription("hist.lead@test.com")
    after = _snapshot_lead(db.session.get(Lead, lead.id))
    assert after == before


def test_backfill_dry_run_nao_escreve(app, ctx):
    _montar_user(email="bf.dry@test.com")
    user = User.query.filter_by(email="bf.dry@test.com").one()
    user.subscribes_to_newsletter = True
    db.session.commit()
    with patch.object(
        news_backfill,
        "subscribe",
        side_effect=AssertionError("dry-run must not persist"),
    ):
        report = news_backfill.run_backfill(apply=False)
    assert report.mode == "DRY_RUN"
    assert report.would_create == 1
    assert NewsletterSubscription.query.count() == 0


def test_backfill_usa_somente_users_subscribed_true(app, ctx):
    _montar_user(email="bf.yes@test.com", slug="bf-yes")
    _montar_user(email="bf.no@test.com", slug="bf-no")
    yes = User.query.filter_by(email="bf.yes@test.com").one()
    no = User.query.filter_by(email="bf.no@test.com").one()
    yes.subscribes_to_newsletter = True
    no.subscribes_to_newsletter = False
    db.session.commit()
    report = news_backfill.run_backfill(apply=False)
    assert report.users_subscribed_true == 1
    assert report.unique_emails == 1
    assert report.would_create == 1


def test_backfill_nao_consulta_nem_promove_lead(app, ctx):
    source = Path("app/services/newsletter_subscription_backfill_service.py").read_text(
        encoding="utf-8"
    )
    assert "from app.models import Lead" not in source
    assert "Lead.query" not in source
    assert "Lead.email" not in source
    assert "data_inscricao" not in source
    assert "acquisition_campaign" not in source
    assert "converted_user_id" not in source
    _make_lead("bf.lead@test.com")
    report = news_backfill.run_backfill(apply=False)
    assert report.users_subscribed_true == 0
    assert NewsletterSubscription.query.count() == 0
    assert Lead.query.filter_by(email="bf.lead@test.com").one().email == "bf.lead@test.com"


def test_backfill_apply_idempotente_em_fixture_isolada(app, ctx):
    _montar_user(email="bf.apply@test.com", slug="bf-apply")
    user = User.query.filter_by(email="bf.apply@test.com").one()
    user.subscribes_to_newsletter = True
    db.session.commit()
    first = news_backfill.run_backfill(apply=True)
    second = news_backfill.run_backfill(apply=True)
    assert first.created == 1
    assert first.existing_active == 0
    assert second.existing_active == 1
    assert second.created == 0
    assert second.skipped_existing_unsubscribed == 0
    assert NewsletterSubscription.query.count() == 1
    row = NewsletterSubscription.query.one()
    assert row.email == "bf.apply@test.com"
    assert row.source == "user_preference_backfill"
    assert row.is_active


def test_backfill_output_sem_email_pii(app, ctx):
    _montar_user(email=PII_EMAIL, slug="bf-pii")
    user = User.query.filter_by(email=PII_EMAIL).one()
    user.subscribes_to_newsletter = True
    db.session.commit()
    report = news_backfill.run_backfill(apply=False)
    text = news_backfill.format_report(report)
    _assert_no_pii(text, PII_EMAIL)
    lines = []
    news_backfill.emit_backfill_cli(apply=False, echo=lines.append)
    _assert_no_pii("\n".join(lines), PII_EMAIL)


def test_inscricao_publica_http_nao_cria_lead(app, ctx):
    client = _mount_public_routes(app)
    resp = client.post("/inscrever-newsletter", data={"email": "http.news@test.com"})
    assert resp.status_code == 200
    assert NewsletterSubscription.query.filter_by(email="http.news@test.com").one().is_active
    assert Lead.query.count() == 0


def test_unsubscribe_token_http_encerra_e_sincroniza_user(app, ctx):
    _conta, _franquia, user = _montar_user(email="tok.user@test.com", slug="tok-user")
    user.subscribes_to_newsletter = True
    db.session.commit()
    result = subscribe(user.email, source=SOURCE_USER_PREFERENCE)
    token = generate_unsubscribe_token(result.subscription.id, secret_key=NEWSLETTER_SECRET)
    client = _mount_public_routes(app)
    pending = client.get(f"/newsletter/cancelar/{token}")
    assert pending.status_code == 200
    assert lookup(user.email).is_active
    applied = client.post(f"/newsletter/cancelar/{token}")
    assert applied.status_code == 200
    assert lookup(user.email).unsubscribed_at is not None
    assert db.session.get(User, user.id).subscribes_to_newsletter is False
    assert Lead.query.count() == 0
    assert CommunicationSuppression.query.count() == 0


def test_lgpd_dry_run_nao_escreve_newsletter(app, ctx):
    _conta, _franquia, user = _montar_user(email="lgpd.dry@test.com", slug="lgpd-dry")
    subscribe(user.email, source=SOURCE_USER_PREFERENCE)
    original = user.email
    processar_exercicio_privacidade_usuario(user, apply=False)
    assert lookup(original).is_active
    assert db.session.get(User, user.id).email == original


def test_backfill_dedupe_mesmo_email_normalizado(app, ctx):
    _montar_user(email="Case.Dup@test.com", slug="dup-a")
    _montar_user(email="case.dup@test.com", slug="dup-b")
    for user in User.query.all():
        user.subscribes_to_newsletter = True
    db.session.commit()
    report = news_backfill.run_backfill(apply=True)
    assert report.users_subscribed_true == 2
    assert report.unique_emails == 1
    assert report.duplicate_email_groups == 1
    assert NewsletterSubscription.query.count() == 1
    assert NewsletterSubscription.query.one().email == "case.dup@test.com"


def test_backfill_user_true_subscription_inexistente_would_create(app, ctx):
    _montar_user(email="bf.missing@test.com", slug="bf-missing")
    user = User.query.filter_by(email="bf.missing@test.com").one()
    user.subscribes_to_newsletter = True
    db.session.commit()
    dry = news_backfill.run_backfill(apply=False)
    assert dry.would_create == 1
    assert dry.created == 0
    assert dry.existing_active == 0
    assert dry.skipped_existing_unsubscribed == 0
    assert NewsletterSubscription.query.count() == 0
    applied = news_backfill.run_backfill(apply=True)
    assert applied.created == 1
    assert applied.would_create == 0
    assert applied.skipped_existing_unsubscribed == 0
    assert NewsletterSubscription.query.one().is_active


def test_backfill_user_true_subscription_ativa_noop(app, ctx):
    _montar_user(email="bf.active@test.com", slug="bf-active")
    user = User.query.filter_by(email="bf.active@test.com").one()
    user.subscribes_to_newsletter = True
    db.session.commit()
    created = subscribe(user.email, source=SOURCE_USER_PREFERENCE)
    stamp = created.subscription.subscribed_at
    source = created.subscription.source
    dry = news_backfill.run_backfill(apply=False)
    assert dry.existing_active == 1
    assert dry.would_create == 0
    assert dry.created == 0
    applied = news_backfill.run_backfill(apply=True)
    assert applied.existing_active == 1
    assert applied.created == 0
    row = lookup(user.email)
    assert row.subscribed_at == stamp
    assert row.unsubscribed_at is None
    assert row.source == source


def test_backfill_nao_reativa_subscription_cancelada(app, ctx):
    _montar_user(email="bf.skip@test.com", slug="bf-skip")
    user = User.query.filter_by(email="bf.skip@test.com").one()
    user.subscribes_to_newsletter = True
    db.session.commit()
    subscribe(user.email, source=SOURCE_PUBLIC_NEWSLETTER)
    unsub = unsubscribe(user.email, commit=True)
    row = unsub.subscription
    before = {
        "subscribed_at": row.subscribed_at,
        "unsubscribed_at": row.unsubscribed_at,
        "source": row.source,
        "id": row.id,
    }
    dry = news_backfill.run_backfill(apply=False)
    assert dry.skipped_existing_unsubscribed == 1
    assert dry.would_create == 0
    assert dry.created == 0
    assert dry.existing_active == 0
    assert lookup(user.email).unsubscribed_at == before["unsubscribed_at"]

    applied = news_backfill.run_backfill(apply=True)
    assert applied.skipped_existing_unsubscribed == 1
    assert applied.created == 0
    assert applied.existing_active == 0
    persisted = lookup("bf.skip@test.com")
    assert persisted.id == before["id"]
    assert persisted.subscribed_at == before["subscribed_at"]
    assert persisted.unsubscribed_at == before["unsubscribed_at"]
    assert persisted.source == before["source"]
    assert persisted.unsubscribed_at is not None
    assert db.session.get(User, user.id).subscribes_to_newsletter is True

    second = news_backfill.run_backfill(apply=True)
    assert second.skipped_existing_unsubscribed == 1
    assert second.created == 0
    again = lookup("bf.skip@test.com")
    assert again.subscribed_at == before["subscribed_at"]
    assert again.unsubscribed_at == before["unsubscribed_at"]
    assert again.source == before["source"]


def test_backfill_output_inclui_skip_sem_pii(app, ctx):
    _montar_user(email="bf.skip.pii@test.com", slug="bf-skip-pii")
    user = User.query.filter_by(email="bf.skip.pii@test.com").one()
    user.subscribes_to_newsletter = True
    db.session.commit()
    subscribe(user.email, source=SOURCE_PUBLIC_NEWSLETTER)
    unsubscribe(user.email, commit=True)
    report = news_backfill.run_backfill(apply=False)
    text = news_backfill.format_report(report)
    assert "skipped_existing_unsubscribed=1" in text
    _assert_no_pii(text, "bf.skip.pii@test.com")
    assert "user_id=" not in text
    assert "subscription_id=" not in text


def test_template_legado_nao_menciona_leads():
    src = Path("app/templates/feature_under_construction.html").read_text(encoding="utf-8")
    lowered = src.lower()
    assert "leads.db" not in lowered
    assert "tabela de leads" not in lowered
    assert "salvo em leads" not in lowered
    assert "newslettersubscription" not in lowered
    assert "seu e-mail será usado apenas para sua inscrição na newsletter." in lowered


def test_migration_encadeada_apos_head_local():
    cfg = Config()
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    script = ScriptDirectory.from_config(cfg)
    rev = script.get_revision("x8y9z0a1b2c3")
    assert rev is not None
    assert rev.down_revision == "w7x8y9z0a1b2"
    nxt = script.get_revision("y9z0a1b2c3d4")
    assert nxt is not None
    assert nxt.down_revision == "x8y9z0a1b2c3"
    assert script.get_revision("y9z0a1b2c3d4") is not None


def test_migration_upgrade_downgrade_sqlite(tmp_path):
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine, inspect, text

    db_path = tmp_path / "newsletter_mig.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    mig_path = ROOT / "migrations" / "versions" / "x8y9z0a1b2c3_newsletter_subscription.py"
    spec = importlib.util.spec_from_file_location("newsletter_mig_mod", mig_path)
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
    tables = inspect(engine).get_table_names()
    assert "newsletter_subscription" in tables
    cols = {c["name"] for c in inspect(engine).get_columns("newsletter_subscription")}
    assert cols == {
        "id",
        "email",
        "subscribed_at",
        "unsubscribed_at",
        "source",
        "created_at",
        "updated_at",
    }
    _run(mig.downgrade)
    assert "newsletter_subscription" not in inspect(engine).get_table_names()


def test_web_rota_publica_nao_usa_lead():
    web_src = Path("app/web.py").read_text(encoding="utf-8")
    news_src = Path("app/news_ai.py").read_text(encoding="utf-8")
    assert "registrar_newsletter_subscription" in web_src
    assert "registrar_lead_newsletter" not in web_src
    assert "registrar_lead_newsletter" not in news_src
    assert "/newsletter/cancelar/" in web_src
    assert "apply_campaign_opt_out" not in web_src.split("def newsletter_cancelar")[1][:1200]


def test_perfil_nao_altera_email():
    user_area_src = Path("app/user_area.py").read_text(encoding="utf-8")
    assert 'user.email =' not in user_area_src
    assert "alterar_email" not in user_area_src
    assert "change_email" not in user_area_src


def _montar_user(*, email: str, slug: str = "news-user") -> tuple:
    conta, franquia = seed_conta_franquia_cliente(slug=slug)
    user = seed_usuario(franquia.id, conta.id, email=email, categoria="pro")
    user.subscribes_to_newsletter = True
    db.session.commit()
    return conta, franquia, user
