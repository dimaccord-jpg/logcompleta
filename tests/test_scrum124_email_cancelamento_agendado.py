"""SCRUM-124: e-mail de cancelamento agendado no downgrade para Free."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from flask import url_for

from app.extensions import db, login_manager
from app.infra import get_user_by_id
from app.models import Franquia, MonetizacaoFato, NotificacaoInterna, User, utcnow_naive
from app.services import cleiton_monetizacao_service as monetizacao_service
from app.services.cleiton_monetizacao_service import (
    efetivar_mudancas_pendentes_ciclo,
    iniciar_jornada_assinatura_stripe,
    processar_evento_stripe,
)
from app.user_area import user_bp
from tests.conftest import seed_conta_franquia_cliente, seed_usuario

ASSUNTO = "Seu cancelamento foi agendado — Agente Frete"
CTA = "Ver planos e continuar no Agente Frete"
BASE_URL = "https://homolog.agentefrete.test"
EFETIVAR = datetime(2026, 9, 30, 15, 0, 0)
DATA_PT_BR = "30/09/2026"
NOMES_PLANO = {"starter": "Starter", "pro": "Pro", "multiuser": "Multiuser"}
FRASES_PROIBIDAS = (
    "Seu plano já foi cancelado.",
    "Seu acesso pago foi encerrado.",
    "Você já está no Free.",
    "Nenhuma outra cobrança poderá existir.",
    "Todos os membros Multiuser perderam o acesso.",
    "Sua Conta Multiuser foi encerrada.",
    "cupom",
    "desconto",
)


def _registrar_blueprint(app) -> None:
    if "user" not in app.blueprints:
        app.register_blueprint(user_bp)


def _login(client, user: User) -> None:
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def _build_client(app):
    app.config["SECRET_KEY"] = "test-secret-scrum124"
    app.config["TESTING"] = True
    _registrar_blueprint(app)
    login_manager.init_app(app)

    @login_manager.user_loader
    def _load_user(user_id):  # noqa: ANN001
        return get_user_by_id(user_id)

    return app.test_client()


def _capturar_emails(monkeypatch) -> list[dict]:
    enviados: list[dict] = []

    def _capture(**kwargs):
        enviados.append(kwargs)

    monkeypatch.setattr("app.auth_services.send_email", _capture)
    return enviados


def _periodo_unix() -> tuple[int, int]:
    fim = int(EFETIVAR.replace(tzinfo=timezone.utc).timestamp())
    inicio = int((EFETIVAR - timedelta(days=30)).replace(tzinfo=timezone.utc).timestamp())
    return inicio, fim


def _preparar_conta_paga(plano: str, *, email: str, nome: str, slug: str):
    conta, franquia = seed_conta_franquia_cliente(slug=slug)
    user = seed_usuario(franquia.id, conta.id, email=email, categoria=plano)
    user.full_name = nome
    fr = db.session.get(Franquia, franquia.id)
    fr.inicio_ciclo = EFETIVAR - timedelta(days=30)
    fr.fim_ciclo = EFETIVAR
    db.session.add(user)
    db.session.add(fr)
    db.session.commit()
    sub_id = f"sub_124_{plano}_{conta.id}"
    customer_id = f"cus_124_{plano}_{conta.id}"
    row = monetizacao_service.registrar_vinculo_comercial_externo(
        conta_id=conta.id,
        provider="stripe",
        customer_id=customer_id,
        subscription_id=sub_id,
        price_id=f"price_{plano}",
        plano_interno=plano,
        status_contratual_externo="active",
        vigencia_externa_fim=EFETIVAR,
        snapshot_normalizado={"origem": "teste_scrum124"},
        payload_bruto_sanitizado={"origem": "teste_scrum124"},
    )
    db.session.commit()
    return conta, franquia, user, row, sub_id, customer_id


def _mock_stripe_downgrade_free(monkeypatch, *, sub_id: str, customer_id: str, plano: str) -> list[dict]:
    inicio, fim = _periodo_unix()
    posts: list[dict] = []

    def _stripe_post(path, payload, idempotency_key=None):
        posts.append(
            {"path": path, "payload": dict(payload), "idempotency_key": idempotency_key}
        )
        return {
            "id": sub_id,
            "customer": customer_id,
            "status": "active",
            "cancel_at_period_end": True,
            "cancel_at": fim,
            "current_period_start": inicio,
            "current_period_end": fim,
            "metadata": {"plano_interno": plano},
            "items": {"data": [{"id": "si_124", "price": {"id": f"price_{plano}"}}]},
        }

    monkeypatch.setattr(monetizacao_service, "_stripe_post", _stripe_post)
    monkeypatch.setattr(
        monetizacao_service,
        "_stripe_get",
        lambda path, params=None: {},  # noqa: ARG005
    )
    monkeypatch.setattr(
        monetizacao_service,
        "_obter_assinatura_stripe_ativa",
        lambda conta_id: {  # noqa: ARG005
            "subscription_id": sub_id,
            "subscription_item_id": "si_124",
            "customer_id": customer_id,
        },
    )
    return posts


def _agendar_free(app, user, *, plano: str, sub_id: str, customer_id: str, monkeypatch):
    _registrar_blueprint(app)
    posts = _mock_stripe_downgrade_free(
        monkeypatch, sub_id=sub_id, customer_id=customer_id, plano=plano
    )
    with app.test_request_context("/", base_url=BASE_URL):
        saida = iniciar_jornada_assinatura_stripe(user=user, plano_codigo="free")
        url_esperada = url_for("user.contrate_plano", _external=True)
    return saida, posts, url_esperada


def _assert_email_agendado(envio: dict, *, nome: str, plano: str, email: str, url: str) -> None:
    nome_plano = NOMES_PLANO[plano]
    assert envio["to_email"] == email
    assert envio["subject"] == ASSUNTO
    html = envio["html"]
    text = envio["text"]
    frases = (
        f"Olá, {nome}.",
        f"Recebemos sua solicitação para encerrar a assinatura do plano {nome_plano}.",
        (
            f"Seu plano continuará ativo normalmente até {DATA_PT_BR}. "
            "Até lá, você poderá continuar utilizando os recursos contratados."
        ),
        (
            "Após essa data, sua assinatura não será renovada. "
            "Seu login no Agente Frete continuará ativo e você poderá escolher um novo plano quando quiser."
        ),
        (
            "Se mudar de ideia ou quiser conhecer outras opções, "
            "estamos à disposição para continuar com você."
        ),
    )
    for frase in frases:
        assert frase in html
        assert frase in text
    assert f'<a href="{url}">{CTA}</a>' in html
    assert f"{CTA}: {url}" in text
    assert url.startswith("https://homolog.agentefrete.test/")
    assert url.endswith("/contrate-um-plano")
    assert "agentefrete.com.br" not in url
    assert "plano Free" not in text
    assert "plano Free" not in html
    for proibida in FRASES_PROIBIDAS:
        assert proibida not in html
        assert proibida not in text
    if plano == "multiuser":
        bloco = f"{html}\n{text}".lower()
        assert "membros" not in bloco
        assert "assentos" not in bloco
        assert "conta multiuser foi encerrada" not in bloco


@pytest.mark.parametrize(
    ("plano", "nome", "email"),
    [
        ("starter", "Ana Costa", "ana-124@test.com"),
        ("pro", "Bruno Lima", "bruno-124@test.com"),
        ("multiuser", "Clara Nunes", "clara-124@test.com"),
    ],
)
def test_downgrade_para_free_envia_um_email_agendado(app, monkeypatch, plano, nome, email):
    enviados = _capturar_emails(monkeypatch)
    with app.app_context():
        conta, _franquia, user, _vinculo, sub_id, customer_id = _preparar_conta_paga(
            plano, email=email, nome=nome, slug=f"conta-124-{plano}"
        )
        saida, posts, url = _agendar_free(
            app, user, plano=plano, sub_id=sub_id, customer_id=customer_id, monkeypatch=monkeypatch
        )

        user_after = db.session.get(User, user.id)
        pendencia = monetizacao_service.obter_pendencia_downgrade_conta_ativa(conta.id)
        assert saida["downgrade_agendado"] is True
        assert saida["plano_codigo"] == "free"
        assert saida["efetivar_em"].startswith("2026-09-30")
        assert pendencia is not None
        assert pendencia["plano_pendente"] == "free"
        assert pendencia["efetivar_em"].startswith("2026-09-30")
        assert user_after.categoria == plano
        assert len(posts) == 1
        assert posts[0]["payload"]["cancel_at_period_end"] == "true"
        assert posts[0]["path"] == f"/subscriptions/{sub_id}"
        assert len(enviados) == 1
        _assert_email_agendado(enviados[0], nome=nome, plano=plano, email=email, url=url)
        assert NotificacaoInterna.query.filter_by(user_id=user.id).count() == 0


def test_saudacao_sem_nome_utilizavel(app, monkeypatch):
    enviados = _capturar_emails(monkeypatch)
    with app.app_context():
        _conta, _franquia, user, _vinculo, sub_id, customer_id = _preparar_conta_paga(
            "starter",
            email="sem-nome-124@test.com",
            nome="   ",
            slug="conta-124-sem-nome",
        )
        _saida, _posts, url = _agendar_free(
            app,
            user,
            plano="starter",
            sub_id=sub_id,
            customer_id=customer_id,
            monkeypatch=monkeypatch,
        )

        assert len(enviados) == 1
        assert enviados[0]["text"].startswith("Olá.\n")
        assert "<p>Olá.</p>" in enviados[0]["html"]
        assert "Olá, " not in enviados[0]["text"]
        assert "sem-nome-124@test.com" not in enviados[0]["text"]
        assert f"{CTA}: {url}" in enviados[0]["text"]


def test_falha_da_solicitacao_nao_envia_email(app, monkeypatch):
    enviados = _capturar_emails(monkeypatch)
    with app.app_context():
        conta, _franquia, user, _vinculo, sub_id, customer_id = _preparar_conta_paga(
            "pro", email="falha-124@test.com", nome="Diana Rocha", slug="conta-124-falha"
        )

        def _stripe_post(path, payload, idempotency_key=None):  # noqa: ARG001
            raise RuntimeError("stripe indisponivel")

        monkeypatch.setattr(monetizacao_service, "_stripe_post", _stripe_post)
        monkeypatch.setattr(
            monetizacao_service,
            "_obter_assinatura_stripe_ativa",
            lambda conta_id: {  # noqa: ARG005
                "subscription_id": sub_id,
                "customer_id": customer_id,
            },
        )
        _registrar_blueprint(app)
        with app.test_request_context("/", base_url=BASE_URL):
            with pytest.raises(RuntimeError, match="stripe indisponivel"):
                iniciar_jornada_assinatura_stripe(user=user, plano_codigo="free")

        user_after = db.session.get(User, user.id)
        assert enviados == []
        assert user_after.categoria == "pro"
        assert monetizacao_service.obter_pendencia_downgrade_conta_ativa(conta.id) is None
        assert (
            MonetizacaoFato.query.filter_by(
                tipo_fato="stripe_subscription_cancel_at_period_end_requested"
            ).count()
            == 0
        )


def test_upgrade_nao_envia_email_de_cancelamento(app, monkeypatch):
    enviados = _capturar_emails(monkeypatch)
    with app.app_context():
        _conta, _franquia, user, _vinculo, sub_id, customer_id = _preparar_conta_paga(
            "starter", email="upgrade-124@test.com", nome="Eva Dias", slug="conta-124-upgrade"
        )
        monkeypatch.setattr(
            monetizacao_service.plano_service,
            "obter_configuracao_gateway_plano_admin",
            lambda plano: {"configuracao_valida": True, "price_id": "price_pro"},
        )
        monkeypatch.setattr(monetizacao_service, "_obter_publishable_key_stripe", lambda: "pk_test")
        monkeypatch.setattr(
            monetizacao_service,
            "_obter_assinatura_stripe_ativa",
            lambda conta_id: {  # noqa: ARG005
                "subscription_id": sub_id,
                "subscription_item_id": "si_up",
                "customer_id": customer_id,
            },
        )
        monkeypatch.setattr(
            monetizacao_service,
            "_stripe_post",
            lambda path, payload, idempotency_key=None: {  # noqa: ARG001
                "id": sub_id,
                "customer": customer_id,
                "status": "active",
                "metadata": {"plano_interno": "pro"},
                "items": {"data": [{"id": "si_up", "price": {"id": "price_pro"}}]},
            },
        )
        saida = iniciar_jornada_assinatura_stripe(user=user, plano_codigo="pro")

        assert saida["assinatura_atualizada_sem_checkout"] is True
        assert saida["plano_codigo"] == "pro"
        assert enviados == []


def test_downgrade_pago_nao_envia_email_de_cancelamento_free(app, monkeypatch):
    enviados = _capturar_emails(monkeypatch)
    with app.app_context():
        _conta, _franquia, user, _vinculo, sub_id, customer_id = _preparar_conta_paga(
            "pro", email="pago-124@test.com", nome="Fabio Neri", slug="conta-124-pago"
        )
        inicio, fim = _periodo_unix()
        monkeypatch.setattr(
            monetizacao_service.plano_service,
            "obter_configuracao_gateway_plano_admin",
            lambda plano: {"configuracao_valida": True, "price_id": "price_starter"},
        )
        monkeypatch.setattr(monetizacao_service, "_obter_publishable_key_stripe", lambda: "pk_test")
        monkeypatch.setattr(
            monetizacao_service,
            "_obter_assinatura_stripe_ativa",
            lambda conta_id: {  # noqa: ARG005
                "subscription_id": sub_id,
                "subscription_item_id": "si_pago",
                "customer_id": customer_id,
            },
        )
        monkeypatch.setattr(
            monetizacao_service,
            "_stripe_post",
            lambda path, payload, idempotency_key=None: {  # noqa: ARG001
                "id": sub_id,
                "customer": customer_id,
                "status": "active",
                "current_period_start": inicio,
                "current_period_end": fim,
                "metadata": {"plano_interno": "starter"},
                "items": {"data": [{"id": "si_pago", "price": {"id": "price_starter"}}]},
            },
        )
        saida = iniciar_jornada_assinatura_stripe(user=user, plano_codigo="starter")

        assert saida["downgrade_agendado"] is True
        assert saida["plano_codigo"] == "starter"
        assert enviados == []


def test_free_para_free_nao_envia_email(app, monkeypatch):
    enviados = _capturar_emails(monkeypatch)
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-124-free")
        user = seed_usuario(franquia.id, conta.id, email="free-124@test.com", categoria="free")
        user.full_name = "Gabi Free"
        db.session.add(user)
        db.session.commit()
        monkeypatch.setattr(monetizacao_service, "_obter_assinatura_stripe_ativa", lambda conta_id: None)
        _registrar_blueprint(app)
        with app.test_request_context("/", base_url=BASE_URL):
            with pytest.raises(ValueError, match="assinatura Stripe ativa"):
                iniciar_jornada_assinatura_stripe(user=user, plano_codigo="free")

        user_after = db.session.get(User, user.id)
        assert enviados == []
        assert user_after.categoria == "free"


def test_free_com_assinatura_ativa_nao_recebe_email_de_plano_pago(app, monkeypatch):
    enviados = _capturar_emails(monkeypatch)
    with app.app_context():
        conta, _franquia, user, _vinculo, sub_id, customer_id = _preparar_conta_paga(
            "free", email="free-sub-124@test.com", nome="Helio Free", slug="conta-124-free-sub"
        )
        saida, posts, _url = _agendar_free(
            app, user, plano="free", sub_id=sub_id, customer_id=customer_id, monkeypatch=monkeypatch
        )

        user_after = db.session.get(User, user.id)
        assert saida["downgrade_agendado"] is True
        assert posts[0]["payload"]["cancel_at_period_end"] == "true"
        assert enviados == []
        assert user_after.categoria == "free"
        assert monetizacao_service.obter_pendencia_downgrade_conta_ativa(conta.id)["plano_pendente"] == "free"


def test_falha_de_email_nao_desfaz_downgrade_agendado(app, monkeypatch):
    def _falha(**kwargs):  # noqa: ARG001
        raise RuntimeError("resend indisponivel")

    monkeypatch.setattr("app.auth_services.send_email", _falha)
    with app.app_context():
        conta, _franquia, user, _vinculo, sub_id, customer_id = _preparar_conta_paga(
            "starter", email="email-falha-124@test.com", nome="Igor Melo", slug="conta-124-email-falha"
        )
        saida, posts, _url = _agendar_free(
            app,
            user,
            plano="starter",
            sub_id=sub_id,
            customer_id=customer_id,
            monkeypatch=monkeypatch,
        )

        user_after = db.session.get(User, user.id)
        pendencia = monetizacao_service.obter_pendencia_downgrade_conta_ativa(conta.id)
        assert saida["downgrade_agendado"] is True
        assert saida["efetivar_em"].startswith("2026-09-30")
        assert posts[0]["payload"]["cancel_at_period_end"] == "true"
        assert pendencia["plano_pendente"] == "free"
        assert user_after.categoria == "starter"


def test_retry_webhook_e_cron_nao_duplicam_email(app, monkeypatch):
    enviados = _capturar_emails(monkeypatch)
    with app.app_context():
        conta, franquia, user, _vinculo, sub_id, customer_id = _preparar_conta_paga(
            "starter", email="retry-124@test.com", nome="Julia Paz", slug="conta-124-retry"
        )
        saida, _posts, _url = _agendar_free(
            app,
            user,
            plano="starter",
            sub_id=sub_id,
            customer_id=customer_id,
            monkeypatch=monkeypatch,
        )
        assert len(enviados) == 1
        assert db.session.get(User, user.id).categoria == "starter"

        _registrar_blueprint(app)
        with app.test_request_context("/", base_url=BASE_URL):
            with pytest.raises(ValueError, match="alteracao de plano pendente"):
                iniciar_jornada_assinatura_stripe(user=user, plano_codigo="free")
        assert len(enviados) == 1

        agora = utcnow_naive()
        inicio, fim = _periodo_unix()
        evento_updated = {
            "id": "evt_124_updated",
            "type": "customer.subscription.updated",
            "created": int(agora.timestamp()),
            "data": {
                "object": {
                    "id": sub_id,
                    "customer": customer_id,
                    "status": "active",
                    "cancel_at_period_end": True,
                    "cancel_at": fim,
                    "current_period_start": inicio,
                    "current_period_end": fim,
                    "metadata": {
                        "conta_id": str(conta.id),
                        "franquia_id": str(franquia.id),
                        "usuario_id": str(user.id),
                        "plano_interno": "starter",
                    },
                    "items": {"data": [{"id": "si_124", "price": {"id": "price_starter"}}]},
                }
            },
        }
        processar_evento_stripe(evento_updated)
        assert len(enviados) == 1

        cron_antes = efetivar_mudancas_pendentes_ciclo(agora=EFETIVAR - timedelta(days=1), limite=50)
        assert cron_antes["efetivados"] == 0
        assert len(enviados) == 1

        efetivar_mudancas_pendentes_ciclo(agora=EFETIVAR + timedelta(days=1), limite=50)
        assert len(enviados) == 1

        evento_deleted = {
            "id": "evt_124_deleted",
            "type": "customer.subscription.deleted",
            "created": int((EFETIVAR + timedelta(days=1)).replace(tzinfo=timezone.utc).timestamp()),
            "data": {
                "object": {
                    "id": sub_id,
                    "customer": customer_id,
                    "status": "canceled",
                    "cancel_at": fim,
                    "current_period_start": inicio,
                    "current_period_end": fim,
                    "metadata": {
                        "conta_id": str(conta.id),
                        "franquia_id": str(franquia.id),
                        "usuario_id": str(user.id),
                        "plano_interno": "starter",
                    },
                }
            },
        }
        processar_evento_stripe(evento_deleted)
        assert len(enviados) == 1
        assert all(item["subject"] == ASSUNTO for item in enviados)
        assert NotificacaoInterna.query.filter_by(user_id=user.id).count() == 0


def test_fato_preexistente_nao_reenvia_email(app, monkeypatch):
    enviados = _capturar_emails(monkeypatch)
    with app.app_context():
        conta, franquia, user, _vinculo, sub_id, customer_id = _preparar_conta_paga(
            "pro", email="fato-124@test.com", nome="Kaio Reis", slug="conta-124-fato"
        )
        idempotency_key = (
            f"stripe_subscription_cancel_period_end:{conta.id}:{franquia.id}:{sub_id}"
        )
        db.session.add(
            MonetizacaoFato(
                tipo_fato="stripe_subscription_cancel_at_period_end_requested",
                status_tecnico="efeito_operacional_aplicado",
                idempotency_key=idempotency_key,
                timestamp_interno=utcnow_naive(),
                provider="stripe",
                conta_id=conta.id,
                franquia_id=franquia.id,
                usuario_id=user.id,
                subscription_id=sub_id,
                snapshot_normalizado_json="{}",
            )
        )
        db.session.commit()
        saida, posts, _url = _agendar_free(
            app, user, plano="pro", sub_id=sub_id, customer_id=customer_id, monkeypatch=monkeypatch
        )

        assert saida["downgrade_agendado"] is True
        assert posts[0]["payload"]["cancel_at_period_end"] == "true"
        assert enviados == []
        assert db.session.get(User, user.id).categoria == "pro"


def test_rota_http_starter_para_free_envia_um_email(app, monkeypatch):
    enviados = _capturar_emails(monkeypatch)
    with app.app_context():
        _conta, _franquia, user, _vinculo, sub_id, customer_id = _preparar_conta_paga(
            "starter", email="http-124@test.com", nome="Lia Torres", slug="conta-124-http"
        )
        _mock_stripe_downgrade_free(
            monkeypatch, sub_id=sub_id, customer_id=customer_id, plano="starter"
        )
        client = _build_client(app)
        _login(client, user)
        response = client.post(
            "/api/contratacao/stripe/iniciar",
            json={"plano_codigo": "free", "confirmar_downgrade": True},
        )

        payload = response.get_json()
        assert response.status_code == 200
        assert payload["ok"] is True
        assert payload["downgrade_agendado"] is True
        assert db.session.get(User, user.id).categoria == "starter"
        assert len(enviados) == 1
        assert enviados[0]["subject"] == ASSUNTO
        assert enviados[0]["to_email"] == "http-124@test.com"
        assert "Olá, Lia Torres." in enviados[0]["text"]
        assert "plano Starter." in enviados[0]["text"]
        assert DATA_PT_BR in enviados[0]["text"]
        assert enviados[0]["text"].rstrip().endswith(
            f"{CTA}: http://localhost/contrate-um-plano"
        )
        assert '<a href="http://localhost/contrate-um-plano">' in enviados[0]["html"]
        assert NotificacaoInterna.query.filter_by(user_id=user.id).count() == 0
