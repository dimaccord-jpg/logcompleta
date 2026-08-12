"""Testes da Etapa 2 / 2.1 — landing pública /acesso-desktop + captura via service."""
from __future__ import annotations

import importlib
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import Lead
from app.services.lead_acquisition_service import (
    CAMPANHA_ACESSO_DESKTOP,
    FONTE_LANDING,
)


APP_DIR = Path(__file__).resolve().parents[1] / "app"


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


@pytest.fixture
def landing_client(app):
    app.config["SECRET_KEY"] = "test-secret-acesso-desktop"
    app.config["TESTING"] = True
    app.config["SERVER_NAME"] = "localhost"
    app.template_folder = str(APP_DIR / "templates")
    app.static_folder = str(APP_DIR / "static")

    # Importa a view real; persistência roda no app de teste (sqlite em memória).
    web = _load_web_module()

    if "acesso_desktop" not in app.view_functions:
        app.add_url_rule(
            "/acesso-desktop",
            endpoint="acesso_desktop",
            view_func=web.acesso_desktop,
            methods=["GET", "POST"],
        )
    if "acesso_desktop_continuar" not in app.view_functions:
        app.add_url_rule(
            "/acesso-desktop/continuar/<token>",
            endpoint="acesso_desktop_continuar",
            view_func=web.acesso_desktop_continuar,
            methods=["GET", "POST"],
        )
    if "acesso_desktop_descadastrar" not in app.view_functions:
        app.add_url_rule(
            "/acesso-desktop/descadastrar/<token>",
            endpoint="acesso_desktop_descadastrar",
            view_func=web.acesso_desktop_descadastrar,
            methods=["GET", "POST"],
        )
    if "login" not in app.view_functions:
        app.add_url_rule(
            "/login",
            endpoint="login",
            view_func=lambda: ("login", 200),
            methods=["GET", "POST"],
        )
    if "privacy_policy" not in app.view_functions:
        app.add_url_rule(
            "/politica-de-privacidade",
            endpoint="privacy_policy",
            view_func=lambda: ("privacy", 200),
        )
    if "terms_of_use" not in app.view_functions:
        app.add_url_rule(
            "/termos-de-uso",
            endpoint="terms_of_use",
            view_func=lambda: ("terms", 200),
        )

    return app.test_client()


def _post_email(client, email: str, extra: dict | None = None):
    data = {"email": email}
    if extra:
        data.update(extra)
    return client.post("/acesso-desktop", data=data, follow_redirects=False)


def test_get_publico_renderiza_landing(landing_client):
    with patch(
        "app.services.plano_service.obter_limite_referencia_plano_admin",
        return_value=Decimal("50"),
    ):
        resp = landing_client.get("/acesso-desktop")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert "Analise seus fretes com mais clareza no computador." in html
    assert 'name="email"' in html
    assert 'type="email"' in html
    assert "50 créditos mensais" in html
    assert "plano Free" in html
    assert "Quero continuar no computador" in html
    assert "Continue no computador" in html
    assert "Como funciona" in html
    assert "Por que usar no computador?" in html


def test_get_landing_reflete_limite_free_dinamico(landing_client):
    with patch(
        "app.services.plano_service.obter_limite_referencia_plano_admin",
        return_value=Decimal("50"),
    ):
        html_a = landing_client.get("/acesso-desktop").get_data(as_text=True)
    with patch(
        "app.services.plano_service.obter_limite_referencia_plano_admin",
        return_value=Decimal("60"),
    ):
        html_b = landing_client.get("/acesso-desktop").get_data(as_text=True)

    assert "50 créditos mensais" in html_a
    assert "Os créditos mensais já fazem parte do plano Free do AgenteFrete." in html_a
    assert "60 créditos mensais" in html_b
    assert "Os créditos mensais já fazem parte do plano Free do AgenteFrete." in html_b
    assert "50 créditos mensais" not in html_b
    assert "Os 50 créditos" not in html_a
    assert "Os 60 créditos" not in html_b


def test_get_landing_fallback_sem_numero_quando_limite_indisponivel(landing_client):
    with patch(
        "app.services.plano_service.obter_limite_referencia_plano_admin",
        side_effect=ValueError("Franquia de referência do plano free não está configurada"),
    ):
        resp = landing_client.get("/acesso-desktop")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "créditos mensais incluídos no plano Free" in html
    assert "Os créditos mensais já fazem parte do plano Free do AgenteFrete." in html
    assert "50 créditos" not in html
    assert "60 créditos" not in html


def test_post_novo_email_valido_captura_e_prg(landing_client, app):
    with app.app_context():
        with patch(
            "app.services.lead_campaign_email_service.send_email"
        ) as send_email_mock:
            resp = _post_email(landing_client, "novo.landing@empresa.com")

            assert resp.status_code == 302
            assert resp.headers["Location"].endswith("/acesso-desktop")
            assert Lead.query.count() == 1
            lead = Lead.query.first()
            assert lead.email == "novo.landing@empresa.com"
            assert lead.acquisition_campaign == CAMPANHA_ACESSO_DESKTOP
            assert lead.acquisition_source == FONTE_LANDING
            assert lead.campaign_captured_at is not None
            assert lead.cta_email_sent_at is not None
            send_email_mock.assert_called_once()

        follow = landing_client.get("/acesso-desktop")
        html = follow.get_data(as_text=True)
        assert "Cadastro recebido." in html
        assert "Enviamos as instruções para o e-mail informado." in html
        assert "Em breve" not in html
        assert "already_in_campaign" not in html
        assert "campaign_mismatch" not in html
        assert "novo.landing@empresa.com" not in html


def test_post_lead_newsletter_existente_atribui_campanha(landing_client, app):
    with app.app_context():
        existing = Lead(email="newsletter@empresa.com")
        db.session.add(existing)
        db.session.commit()
        lead_id = existing.id

        resp = _post_email(landing_client, "newsletter@empresa.com")
        assert resp.status_code == 302

        assert Lead.query.count() == 1
        lead = db.session.get(Lead, lead_id)
        assert lead is not None
        assert lead.acquisition_campaign == CAMPANHA_ACESSO_DESKTOP
        assert lead.acquisition_source == FONTE_LANDING
        assert lead.campaign_captured_at is not None


def test_post_mesma_campanha_repetida_preserva_captured_at(landing_client, app):
    with app.app_context():
        first = _post_email(landing_client, "rep@empresa.com")
        assert first.status_code == 302
        lead = Lead.query.filter_by(email="rep@empresa.com").one()
        captured_at = lead.campaign_captured_at
        assert captured_at is not None

        second = _post_email(landing_client, "rep@empresa.com")
        assert second.status_code == 302
        assert Lead.query.count() == 1
        lead = Lead.query.filter_by(email="rep@empresa.com").one()
        assert lead.campaign_captured_at == captured_at

        html = landing_client.get("/acesso-desktop").get_data(as_text=True)
        assert "Cadastro recebido." in html
        assert "already_in_campaign" not in html
        assert "Você já está na campanha" not in html


def test_post_casing_diferente_uma_linha(landing_client, app):
    with app.app_context():
        _post_email(landing_client, "Usuario@Empresa.com")
        _post_email(landing_client, "usuario@empresa.com")

        assert Lead.query.count() == 1
        lead = Lead.query.first()
        assert lead.email == "usuario@empresa.com"
        assert lead.acquisition_campaign == CAMPANHA_ACESSO_DESKTOP


def test_post_email_invalido_nao_cria_lead(landing_client, app):
    with app.app_context():
        resp = _post_email(landing_client, "nao-e-email")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Informe um e-mail válido para continuar." in html
        assert 'value="nao-e-email"' in html
        assert Lead.query.count() == 0


def test_post_email_vazio_nao_cria_lead(landing_client, app):
    with app.app_context():
        resp = _post_email(landing_client, "")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Informe um e-mail válido para continuar." in html
        assert Lead.query.count() == 0


@pytest.mark.parametrize(
    "email_invalido",
    [
        "usuario @empresa.com",
        "usuario@\tempresa.com",
        "usuario@empre\nsa.com",
        "usuario@empre\rsa.com",
    ],
)
def test_post_email_com_whitespace_interno_rejeitado(landing_client, app, email_invalido):
    with app.app_context():
        resp = _post_email(landing_client, email_invalido)
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Informe um e-mail válido para continuar." in html
        assert Lead.query.count() == 0


def test_campaign_mismatch_preserva_atribuicao_original(landing_client, app):
    with app.app_context():
        captured_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=5)
        lead = Lead(
            email="outra@empresa.com",
            acquisition_campaign="outra_campanha",
            acquisition_source="ads",
            campaign_captured_at=captured_at,
        )
        db.session.add(lead)
        db.session.commit()
        lead_id = lead.id

        resp = _post_email(landing_client, "outra@empresa.com")
        assert resp.status_code == 302

        assert Lead.query.count() == 1
        lead = db.session.get(Lead, lead_id)
        assert lead is not None
        assert lead.id == lead_id
        assert lead.acquisition_campaign == "outra_campanha"
        assert lead.acquisition_source == "ads"
        assert lead.campaign_captured_at == captured_at

        html = landing_client.get("/acesso-desktop").get_data(as_text=True)
        assert "Cadastro recebido." in html
        assert "campaign_mismatch" not in html
        assert "already_in_campaign" not in html
        assert "outra_campanha" not in html


def test_campanha_nao_vem_do_cliente(landing_client, app):
    with app.app_context():
        resp = _post_email(
            landing_client,
            "seguro@empresa.com",
            extra={
                "acquisition_campaign": "malicioso",
                "source": "qualquer_coisa",
                "acquisition_source": "hack",
            },
        )
        assert resp.status_code == 302
        lead = Lead.query.filter_by(email="seguro@empresa.com").one()
        assert lead.acquisition_campaign == CAMPANHA_ACESSO_DESKTOP
        assert lead.acquisition_source == FONTE_LANDING
        assert lead.acquisition_campaign != "malicioso"
        assert lead.acquisition_source != "qualquer_coisa"


def test_post_landing_envia_cta_e_nao_marca_clique(landing_client, app):
    """Etapa 3: captura dispara CTA; clique só ocorre na rota assinada."""
    with app.app_context():
        with patch(
            "app.services.lead_campaign_email_service.send_email"
        ) as send_email_mock:
            resp = _post_email(landing_client, "sem.email@empresa.com")
            assert resp.status_code == 302
            lead = Lead.query.filter_by(email="sem.email@empresa.com").one()
            assert lead.cta_email_sent_at is not None
            assert lead.cta_clicked_at is None
            send_email_mock.assert_called_once()
