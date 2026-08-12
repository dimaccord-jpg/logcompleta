"""Testes da Etapa 3 — e-mail CTA, tokens assinados, clique e unsubscribe."""
from __future__ import annotations

import importlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import Lead, User
from app.services.lead_acquisition_service import (
    CAMPANHA_ACESSO_DESKTOP,
    FONTE_LANDING,
)
from app.services import lead_campaign_email_service as campaign_email


APP_DIR = Path(__file__).resolve().parents[1] / "app"
SECRET = "test-secret-acesso-desktop-cta"


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", SECRET)
    return importlib.import_module("app.web")


@pytest.fixture
def cta_client(app):
    app.config["SECRET_KEY"] = SECRET
    app.config["TESTING"] = True
    app.config["SERVER_NAME"] = "localhost"
    app.template_folder = str(APP_DIR / "templates")
    app.static_folder = str(APP_DIR / "static")

    web = _load_web_module()

    rules = {
        "acesso_desktop": (
            "/acesso-desktop",
            web.acesso_desktop,
            ["GET", "POST"],
        ),
        "acesso_desktop_continuar": (
            "/acesso-desktop/continuar/<token>",
            web.acesso_desktop_continuar,
            ["GET", "POST"],
        ),
        "acesso_desktop_descadastrar": (
            "/acesso-desktop/descadastrar/<token>",
            web.acesso_desktop_descadastrar,
            ["GET", "POST"],
        ),
        "login": ("/login", lambda: ("login", 200), ["GET", "POST"]),
        "privacy_policy": (
            "/politica-de-privacidade",
            lambda: ("privacy", 200),
            ["GET"],
        ),
        "terms_of_use": ("/termos-de-uso", lambda: ("terms", 200), ["GET"]),
    }
    for endpoint, (rule, view, methods) in rules.items():
        if endpoint not in app.view_functions:
            app.add_url_rule(rule, endpoint=endpoint, view_func=view, methods=methods)

    return app.test_client()


def _post_landing(client, email: str):
    return client.post("/acesso-desktop", data={"email": email}, follow_redirects=False)


def _make_lead(**kwargs) -> Lead:
    defaults = {
        "email": "lead@empresa.com",
        "acquisition_campaign": CAMPANHA_ACESSO_DESKTOP,
        "acquisition_source": FONTE_LANDING,
        "campaign_captured_at": datetime.now(timezone.utc).replace(tzinfo=None),
        "followup_count": 0,
    }
    defaults.update(kwargs)
    lead = Lead(**defaults)
    db.session.add(lead)
    db.session.commit()
    return lead


def test_1_novo_lead_recebe_cta(cta_client, app):
    with app.app_context():
        with patch.object(campaign_email, "send_email") as send_mock:
            resp = _post_landing(cta_client, "novo.cta@empresa.com")
            assert resp.status_code == 302
            send_mock.assert_called_once()
            kwargs = send_mock.call_args.kwargs
            assert kwargs["to_email"] == "novo.cta@empresa.com"
            assert kwargs["subject"] == campaign_email.CTA_EMAIL_SUBJECT
            html = kwargs["html"]
            text = kwargs["text"]
            assert "/acesso-desktop/continuar/" in html
            assert "/acesso-desktop/descadastrar/" in html
            assert "/acesso-desktop/continuar/" in text
            assert "/acesso-desktop/descadastrar/" in text

            lead = Lead.query.filter_by(email="novo.cta@empresa.com").one()
            assert lead.cta_email_sent_at is not None


def test_2_lead_newsletter_atribuido_recebe_cta(cta_client, app):
    with app.app_context():
        existing = Lead(email="news.cta@empresa.com")
        db.session.add(existing)
        db.session.commit()
        lead_id = existing.id

        with patch.object(campaign_email, "send_email") as send_mock:
            resp = _post_landing(cta_client, "news.cta@empresa.com")
            assert resp.status_code == 302
            send_mock.assert_called_once()

            lead = db.session.get(Lead, lead_id)
            assert lead.acquisition_campaign == CAMPANHA_ACESSO_DESKTOP
            assert lead.cta_email_sent_at is not None


def test_3_repeticao_nao_reenvia_cta(cta_client, app):
    with app.app_context():
        sent_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
        _make_lead(email="ja.enviado@empresa.com", cta_email_sent_at=sent_at)

        with patch.object(campaign_email, "send_email") as send_mock:
            resp = _post_landing(cta_client, "ja.enviado@empresa.com")
            assert resp.status_code == 302
            send_mock.assert_not_called()
            lead = Lead.query.filter_by(email="ja.enviado@empresa.com").one()
            assert lead.cta_email_sent_at == sent_at

        html = cta_client.get("/acesso-desktop").get_data(as_text=True)
        assert "Cadastro recebido." in html
        assert "Seu e-mail foi registrado com sucesso." in html
        assert "already_in_campaign" not in html


def test_4_falha_sender_mantem_elegivel(cta_client, app):
    with app.app_context():
        with patch.object(
            campaign_email,
            "send_email",
            side_effect=RuntimeError("falha resend"),
        ) as send_mock:
            resp = _post_landing(cta_client, "falha.sender@empresa.com")
            assert resp.status_code == 302
            send_mock.assert_called_once()
            lead = Lead.query.filter_by(email="falha.sender@empresa.com").one()
            assert lead.acquisition_campaign == CAMPANHA_ACESSO_DESKTOP
            assert lead.cta_email_sent_at is None

        html = cta_client.get("/acesso-desktop").get_data(as_text=True)
        assert "Cadastro recebido." in html
        assert resp.status_code != 500

        with patch.object(campaign_email, "send_email") as send_retry:
            resp2 = _post_landing(cta_client, "falha.sender@empresa.com")
            assert resp2.status_code == 302
            send_retry.assert_called_once()
            lead = Lead.query.filter_by(email="falha.sender@empresa.com").one()
            assert lead.cta_email_sent_at is not None


def test_5_opt_out_suprime_envio(cta_client, app):
    with app.app_context():
        opt = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
        _make_lead(email="optout@empresa.com", opt_out_at=opt)

        with patch.object(campaign_email, "send_email") as send_mock:
            resp = _post_landing(cta_client, "optout@empresa.com")
            assert resp.status_code == 302
            send_mock.assert_not_called()
            lead = Lead.query.filter_by(email="optout@empresa.com").one()
            assert lead.opt_out_at == opt
            assert lead.cta_email_sent_at is None


def test_6_campaign_mismatch_nao_envia(cta_client, app):
    with app.app_context():
        captured = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=3)
        _make_lead(
            email="outra@empresa.com",
            acquisition_campaign="outra_campanha",
            acquisition_source="ads",
            campaign_captured_at=captured,
        )

        with patch.object(campaign_email, "send_email") as send_mock:
            resp = _post_landing(cta_client, "outra@empresa.com")
            assert resp.status_code == 302
            send_mock.assert_not_called()
            lead = Lead.query.filter_by(email="outra@empresa.com").one()
            assert lead.acquisition_campaign == "outra_campanha"
            assert lead.acquisition_source == "ads"
            assert lead.campaign_captured_at == captured
            assert lead.cta_email_sent_at is None

        html = cta_client.get("/acesso-desktop").get_data(as_text=True)
        assert "campaign_mismatch" not in html
        assert "Cadastro recebido." in html


def test_7_payload_cta_sem_email(app):
    with app.app_context():
        lead = _make_lead(email="token.payload@empresa.com")
        token = campaign_email.generate_cta_token(lead.id, secret_key=SECRET)
        payload = campaign_email.loads_cta_payload(token, secret_key=SECRET)
        assert payload == {
            "lead_id": lead.id,
            "purpose": campaign_email.PURPOSE_CTA,
        }
        assert "email" not in payload
        assert lead.email not in token


def test_8_salts_e_purposes_separados(app):
    with app.app_context():
        lead = _make_lead(email="salts@empresa.com")
        cta = campaign_email.generate_cta_token(lead.id, secret_key=SECRET)
        unsub = campaign_email.generate_unsubscribe_token(lead.id, secret_key=SECRET)

        assert campaign_email.loads_cta_payload(cta, secret_key=SECRET) is not None
        assert campaign_email.loads_unsubscribe_payload(cta, secret_key=SECRET) is None
        assert campaign_email.loads_unsubscribe_payload(unsub, secret_key=SECRET) is not None
        assert campaign_email.loads_cta_payload(unsub, secret_key=SECRET) is None


def test_9_token_adulterado_resposta_generica(cta_client, app):
    with app.app_context():
        lead = _make_lead(email="tamper@empresa.com")
        token = campaign_email.generate_cta_token(lead.id, secret_key=SECRET)
        bad = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")

        get_resp = cta_client.get(f"/acesso-desktop/continuar/{bad}", follow_redirects=False)
        assert get_resp.status_code == 302
        assert get_resp.headers["Location"].endswith("/acesso-desktop")

        post_resp = cta_client.post(
            f"/acesso-desktop/continuar/{bad}",
            follow_redirects=False,
        )
        assert post_resp.status_code == 302
        assert post_resp.headers["Location"].endswith("/acesso-desktop")

        lead = Lead.query.filter_by(email="tamper@empresa.com").one()
        assert lead.cta_clicked_at is None
        assert lead.opt_out_at is None

        html = cta_client.get("/acesso-desktop").get_data(as_text=True)
        assert "Este link não está disponível" in html
        assert "Lead" not in html or "Lead não encontrado" not in html


def test_10_get_cta_nao_registra_clique(cta_client, app):
    with app.app_context():
        lead = _make_lead(email="get.click@empresa.com")
        token = campaign_email.generate_cta_token(lead.id, secret_key=SECRET)

        resp = cta_client.get(
            f"/acesso-desktop/continuar/{token}",
            follow_redirects=False,
        )
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Continue no computador." in html
        assert "Continuar para o cadastro Free" in html

        lead = Lead.query.filter_by(email="get.click@empresa.com").one()
        assert lead.cta_clicked_at is None


def _assert_cta_register_location(location: str, *, lead_email: str, lead_id: int, token: str):
    assert "/login" in location
    assert "mode=register" in location
    assert "site-malicioso" not in location
    assert lead_email not in location
    assert str(lead_id) not in location
    assert token not in location
    assert "campaign" not in location
    assert "desktop_access" not in location


def test_11_post_cta_registra_primeiro_clique_e_redirect(cta_client, app):
    with app.app_context():
        lead = _make_lead(email="post.click@empresa.com")
        lead_id = lead.id
        token = campaign_email.generate_cta_token(lead.id, secret_key=SECRET)

        resp = cta_client.post(
            f"/acesso-desktop/continuar/{token}",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        location = resp.headers["Location"]
        _assert_cta_register_location(
            location,
            lead_email="post.click@empresa.com",
            lead_id=lead_id,
            token=token,
        )

        lead = Lead.query.filter_by(email="post.click@empresa.com").one()
        assert lead.cta_clicked_at is not None


def test_11b_post_cta_redirect_abre_tab_criar_conta(cta_client, app, monkeypatch):
    """E2E leve: Location do CTA abre a aba CRIAR CONTA no login real."""
    import re

    web = _load_web_module()
    monkeypatch.setattr(web, "get_active_term", lambda: None)

    with app.app_context():
        lead = _make_lead(email="e2e.click@empresa.com")
        token = campaign_email.generate_cta_token(lead.id, secret_key=SECRET)
        resp = cta_client.post(
            f"/acesso-desktop/continuar/{token}",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        location = resp.headers["Location"]
        assert "mode=register" in location

    login_client = web.app.test_client()
    # Aceita Location absoluta ou relativa do test client.
    path = location
    if path.startswith("http://") or path.startswith("https://"):
        from urllib.parse import urlparse

        parsed = urlparse(path)
        path = parsed.path
        if parsed.query:
            path = f"{path}?{parsed.query}"

    page = login_client.get(path)
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    tab_cadastro = re.search(r"<button\b[^>]*\bid=\"tab-cadastro\"[^>]*>", html)
    content_cadastro = re.search(r"<div\b[^>]*\bid=\"content-cadastro\"[^>]*>", html)
    assert tab_cadastro is not None
    assert content_cadastro is not None
    assert "active" in tab_cadastro.group(0)
    assert "show active" in content_cadastro.group(0)


def test_12_post_cta_repetido_idempotente(cta_client, app):
    with app.app_context():
        lead = _make_lead(email="idem.click@empresa.com")
        token = campaign_email.generate_cta_token(lead.id, secret_key=SECRET)

        cta_client.post(f"/acesso-desktop/continuar/{token}", follow_redirects=False)
        lead = Lead.query.filter_by(email="idem.click@empresa.com").one()
        first = lead.cta_clicked_at
        assert first is not None

        resp = cta_client.post(
            f"/acesso-desktop/continuar/{token}",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        location = resp.headers["Location"]
        assert "/login" in location
        assert "mode=register" in location
        lead = Lead.query.filter_by(email="idem.click@empresa.com").one()
        assert lead.cta_clicked_at == first


def test_13_get_unsubscribe_nao_altera_opt_out(cta_client, app):
    with app.app_context():
        lead = _make_lead(email="get.unsub@empresa.com")
        token = campaign_email.generate_unsubscribe_token(lead.id, secret_key=SECRET)

        resp = cta_client.get(
            f"/acesso-desktop/descadastrar/{token}",
            follow_redirects=False,
        )
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Cancelar mensagens desta jornada?" in html

        lead = Lead.query.filter_by(email="get.unsub@empresa.com").one()
        assert lead.opt_out_at is None


def test_14_post_unsubscribe_define_opt_out(cta_client, app):
    with app.app_context():
        sent_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
        clicked = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=30)
        lead = _make_lead(
            email="post.unsub@empresa.com",
            cta_email_sent_at=sent_at,
            cta_clicked_at=clicked,
        )
        lead_id = lead.id
        campaign = lead.acquisition_campaign
        token = campaign_email.generate_unsubscribe_token(lead.id, secret_key=SECRET)

        resp = cta_client.post(
            f"/acesso-desktop/descadastrar/{token}",
            follow_redirects=False,
        )
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Preferência atualizada." in html
        assert "newsletter" not in html.lower() or "Newsletter" not in html

        lead = db.session.get(Lead, lead_id)
        assert lead is not None
        assert lead.opt_out_at is not None
        assert lead.acquisition_campaign == campaign
        assert lead.cta_email_sent_at == sent_at
        assert lead.cta_clicked_at == clicked


def test_15_unsubscribe_repetido_preserva_timestamp(cta_client, app):
    with app.app_context():
        lead = _make_lead(email="rep.unsub@empresa.com")
        token = campaign_email.generate_unsubscribe_token(lead.id, secret_key=SECRET)

        cta_client.post(f"/acesso-desktop/descadastrar/{token}", follow_redirects=False)
        lead = Lead.query.filter_by(email="rep.unsub@empresa.com").one()
        first = lead.opt_out_at
        assert first is not None

        resp = cta_client.post(
            f"/acesso-desktop/descadastrar/{token}",
            follow_redirects=False,
        )
        assert resp.status_code == 200
        lead = Lead.query.filter_by(email="rep.unsub@empresa.com").one()
        assert lead.opt_out_at == first


def test_16_unsubscribe_nao_altera_newsletter_user(cta_client, app):
    from tests.conftest import seed_sistema_interno, seed_usuario

    with app.app_context():
        _conta, franquia = seed_sistema_interno()
        user = seed_usuario(franquia.id, _conta.id, email="user.news@empresa.com")
        user.subscribes_to_newsletter = True
        db.session.commit()
        user_id = user.id

        lead = _make_lead(email="lead.news@empresa.com")
        token = campaign_email.generate_unsubscribe_token(lead.id, secret_key=SECRET)

        cta_client.post(f"/acesso-desktop/descadastrar/{token}", follow_redirects=False)

        user = db.session.get(User, user_id)
        assert user is not None
        assert user.subscribes_to_newsletter is True
        lead = Lead.query.filter_by(email="lead.news@empresa.com").one()
        assert lead.opt_out_at is not None
        # Opt-out da jornada não toca preferência de newsletter do User.


def test_17_conteudo_email_tem_dois_links_sem_promocao(cta_client, app):
    with app.app_context():
        with patch.object(campaign_email, "send_email") as send_mock:
            _post_landing(cta_client, "conteudo@empresa.com")
            kwargs = send_mock.call_args.kwargs
            html = kwargs["html"]
            text = kwargs["text"]
            assert "Continuar no computador" in html
            assert "Cancelar mensagens" in html
            assert "/acesso-desktop/continuar/" in html
            assert "/acesso-desktop/descadastrar/" in html
            for blob in (html, text):
                assert "bônus" not in blob.lower()
                assert "promocao" not in blob.lower() and "promoção" not in blob.lower()
                assert "créditos" not in blob.lower() and "creditos" not in blob.lower()
                assert "campaign_mismatch" not in blob
                assert "cta_email_sent_at" not in blob
                assert "Auditar meu primeiro frete" not in blob
                assert "pagando o que foi acordado" not in blob.lower()


def test_18_cta_ignora_next_open_redirect(cta_client, app):
    with app.app_context():
        lead = _make_lead(email="redirect@empresa.com")
        lead_id = lead.id
        token = campaign_email.generate_cta_token(lead.id, secret_key=SECRET)

        resp = cta_client.post(
            f"/acesso-desktop/continuar/{token}?next=https://site-malicioso.example",
            data={"destination": "https://site-malicioso.example", "next": "/evil"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        location = resp.headers["Location"]
        _assert_cta_register_location(
            location,
            lead_email="redirect@empresa.com",
            lead_id=lead_id,
            token=token,
        )
        assert "evil" not in location
        assert "site-malicioso" not in location
