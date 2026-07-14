"""Testes da regularização de falha vigente na mensalidade (Starter/Pro)."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.extensions import db, login_manager
from app.infra import get_user_by_id
from app.models import ContaMonetizacaoVinculo, Franquia, MonetizacaoFato, User, utcnow_naive
from app.services import cleiton_monetizacao_service as monetizacao_service
from app.services import cleiton_operacao_autorizacao_service as authz_service
from app.services.cleiton_monetizacao_service import (
    FALHA_MENSAL_BILLING_REASON,
    TIPO_FATO_INVOICE_PAID,
    TIPO_FATO_INVOICE_PAYMENT_FAILED,
    criar_sessao_portal_pagamento_stripe,
    registrar_fato_monetizacao,
    resolver_falha_mensal_vigente_conta,
)
from app.user_area import (
    gerar_csrf_token_regularizar_pagamento,
    user_bp,
    _validar_url_portal_stripe,
)
from app.models import Franquia as FranquiaModel
from tests.conftest import seed_conta_franquia_cliente, seed_usuario


def _criar_vinculo_ativo(
    *,
    conta_id: int,
    customer_id: str,
    subscription_id: str,
    plano_interno: str = "starter",
) -> ContaMonetizacaoVinculo:
    row = ContaMonetizacaoVinculo(
        conta_id=int(conta_id),
        provider="stripe",
        customer_id=customer_id,
        subscription_id=subscription_id,
        price_id="price_teste",
        plano_interno=plano_interno,
        status_contratual_externo="active",
        ativo=True,
        snapshot_normalizado_json=monetizacao_service._json_dumps({}),
        payload_bruto_sanitizado_json=monetizacao_service._json_dumps({"origem": "teste"}),
    )
    db.session.add(row)
    db.session.commit()
    return row


def _evento_invoice(
    *,
    event_id: str,
    event_type: str,
    invoice_id: str,
    customer_id: str,
    subscription_id: str,
    billing_reason: str | None = "subscription_cycle",
):
    obj = {
        "id": invoice_id,
        "customer": customer_id,
        "subscription": subscription_id,
    }
    if billing_reason is not None:
        obj["billing_reason"] = billing_reason
    return {
        "id": event_id,
        "type": event_type,
        "created": int(utcnow_naive().timestamp()),
        "data": {"object": obj},
    }


def _registrar_fato_invoice(
    *,
    conta_id: int,
    franquia_id: int,
    usuario_id: int,
    event_type: str,
    invoice_id: str,
    customer_id: str,
    subscription_id: str,
    billing_reason: str | None = "subscription_cycle",
    event_id: str | None = None,
) -> MonetizacaoFato:
    tipo_fato = (
        TIPO_FATO_INVOICE_PAYMENT_FAILED
        if event_type == "invoice.payment_failed"
        else TIPO_FATO_INVOICE_PAID
    )
    evt_id = event_id or f"evt_{invoice_id}_{tipo_fato}"
    evento = _evento_invoice(
        event_id=evt_id,
        event_type=event_type,
        invoice_id=invoice_id,
        customer_id=customer_id,
        subscription_id=subscription_id,
        billing_reason=billing_reason,
    )
    return registrar_fato_monetizacao(
        tipo_fato=tipo_fato,
        status_tecnico=monetizacao_service.STATUS_TEC_APLICADO,
        provider="stripe",
        conta_id=conta_id,
        franquia_id=franquia_id,
        usuario_id=usuario_id,
        external_event_id=evt_id,
        customer_id=customer_id,
        subscription_id=subscription_id,
        invoice_id=invoice_id,
        payload_bruto_sanitizado=evento,
    )


def _login(client, user: User) -> None:
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def _csrf_form_data(app, user: User) -> dict[str, str]:
    with app.app_context():
        return {
            "csrf_token": gerar_csrf_token_regularizar_pagamento(int(user.id)),
        }


def _post_regularizar_stripe(
    client,
    user: User,
    *,
    extra: dict | None = None,
    follow_redirects: bool = False,
):
    data = _csrf_form_data(client.application, user)
    if extra:
        data.update(extra)
    return client.post(
        "/perfil/regularizar-pagamento/stripe",
        data=data,
        follow_redirects=follow_redirects,
    )


def _build_user_area_client(app):
    app.config["SECRET_KEY"] = "test-secret-falha-mensal"
    app.config["TESTING"] = True
    if "user" not in app.blueprints:
        app.register_blueprint(user_bp)

    @app.route("/login", endpoint="login")
    def login_stub():
        return "login", 401

    @app.route("/", endpoint="index")
    def index_stub():
        return "index"

    @app.route("/logout", endpoint="logout")
    def logout_stub():
        return "logout"

    @app.route("/fretes", endpoint="fretes")
    def fretes_stub():
        return "fretes"

    @app.route("/controle-estoque", endpoint="controle_estoque")
    def controle_estoque_stub():
        return "estoque"

    @app.route("/feed", endpoint="feed")
    def feed_stub():
        return "feed"

    @app.route("/politica-de-privacidade", endpoint="privacy_policy")
    def privacy_policy_stub():
        return "privacy"

    @app.route("/logout")
    def logout_stub():
        return "logout"

    @app.context_processor
    def _inject_test_template_helpers():
        return {
            "has_endpoint": lambda endpoint_name: endpoint_name in app.view_functions,
            "falha_mensal_vigente": False,
            "plano_exibicao": None,
            "regularizacao_url": "/perfil/regularizar-pagamento",
        }

    login_manager.init_app(app)
    login_manager.login_view = "login"

    @login_manager.user_loader
    def _load_user(user_id):  # noqa: ANN001
        return get_user_by_id(user_id)

    return app.test_client()


def _user_auth(*, conta_id=1, franquia_id=10, categoria="pro"):
    return SimpleNamespace(
        is_authenticated=True,
        franquia_id=franquia_id,
        conta_id=conta_id,
        categoria=categoria,
    )


def _leitura_degraded(plano_resolvido: str = "starter"):
    return SimpleNamespace(
        franquia_id=10,
        limite_total=Decimal("100"),
        consumo_acumulado=Decimal("100"),
        saldo_disponivel=Decimal("0"),
        inicio_ciclo=None,
        fim_ciclo=None,
        status=FranquiaModel.STATUS_DEGRADED,
        plano_resolvido=plano_resolvido,
        motivo_status="limite_plano_atingido",
        pendencias=(),
    )


# --- Resolvedor ---


def test_starter_payment_failed_subscription_cycle_ativa_falha(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="falha-starter")
        user = seed_usuario(franquia.id, conta.id, email="falha-starter@test.com", categoria="starter")
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_starter",
            subscription_id="sub_starter",
            plano_interno="starter",
        )
        _registrar_fato_invoice(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            event_type="invoice.payment_failed",
            invoice_id="in_starter_1",
            customer_id="cus_starter",
            subscription_id="sub_starter",
        )
        out = resolver_falha_mensal_vigente_conta(conta.id)
        assert out["falha_mensal_vigente"] is True
        assert out["plano_codigo"] == "starter"
        assert out["plano_exibicao"] == "Starter"
        assert out["invoice_id"] == "in_starter_1"


def test_pro_payment_failed_subscription_cycle_ativa_falha(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="falha-pro")
        user = seed_usuario(franquia.id, conta.id, email="falha-pro@test.com", categoria="pro")
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_pro",
            subscription_id="sub_pro",
            plano_interno="pro",
        )
        _registrar_fato_invoice(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            event_type="invoice.payment_failed",
            invoice_id="in_pro_1",
            customer_id="cus_pro",
            subscription_id="sub_pro",
        )
        out = resolver_falha_mensal_vigente_conta(conta.id)
        assert out["falha_mensal_vigente"] is True
        assert out["plano_codigo"] == "pro"
        assert out["plano_exibicao"] == "Pro"


def test_billing_reason_diferente_nao_ativa(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="falha-br-diff")
        user = seed_usuario(franquia.id, conta.id, email="br-diff@test.com", categoria="pro")
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_br",
            subscription_id="sub_br",
            plano_interno="pro",
        )
        _registrar_fato_invoice(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            event_type="invoice.payment_failed",
            invoice_id="in_br_diff",
            customer_id="cus_br",
            subscription_id="sub_br",
            billing_reason="subscription_create",
        )
        out = resolver_falha_mensal_vigente_conta(conta.id)
        assert out["falha_mensal_vigente"] is False


def test_ausencia_billing_reason_nao_ativa(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="falha-br-aus")
        user = seed_usuario(franquia.id, conta.id, email="br-aus@test.com", categoria="pro")
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_bra",
            subscription_id="sub_bra",
            plano_interno="pro",
        )
        _registrar_fato_invoice(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            event_type="invoice.payment_failed",
            invoice_id="in_br_aus",
            customer_id="cus_bra",
            subscription_id="sub_bra",
            billing_reason=None,
        )
        out = resolver_falha_mensal_vigente_conta(conta.id)
        assert out["falha_mensal_vigente"] is False


def test_customer_divergente_nao_ativa(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="falha-cust-div")
        user = seed_usuario(franquia.id, conta.id, email="cust-div@test.com", categoria="pro")
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_vinculo",
            subscription_id="sub_vinculo",
            plano_interno="pro",
        )
        _registrar_fato_invoice(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            event_type="invoice.payment_failed",
            invoice_id="in_cust_div",
            customer_id="cus_outro",
            subscription_id="sub_vinculo",
        )
        out = resolver_falha_mensal_vigente_conta(conta.id)
        assert out["falha_mensal_vigente"] is False


def test_subscription_divergente_nao_ativa(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="falha-sub-div")
        user = seed_usuario(franquia.id, conta.id, email="sub-div@test.com", categoria="pro")
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_sub",
            subscription_id="sub_vinculo",
            plano_interno="pro",
        )
        _registrar_fato_invoice(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            event_type="invoice.payment_failed",
            invoice_id="in_sub_div",
            customer_id="cus_sub",
            subscription_id="sub_outro",
        )
        out = resolver_falha_mensal_vigente_conta(conta.id)
        assert out["falha_mensal_vigente"] is False


def test_invoice_paid_mesma_invoice_encerra_falha(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="falha-paid-mesma")
        user = seed_usuario(franquia.id, conta.id, email="paid-mesma@test.com", categoria="pro")
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_paid",
            subscription_id="sub_paid",
            plano_interno="pro",
        )
        _registrar_fato_invoice(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            event_type="invoice.payment_failed",
            invoice_id="in_mesma",
            customer_id="cus_paid",
            subscription_id="sub_paid",
        )
        _registrar_fato_invoice(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            event_type="invoice.paid",
            invoice_id="in_mesma",
            customer_id="cus_paid",
            subscription_id="sub_paid",
        )
        out = resolver_falha_mensal_vigente_conta(conta.id)
        assert out["falha_mensal_vigente"] is False


def test_invoice_paid_outra_invoice_nao_encerra(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="falha-paid-outra")
        user = seed_usuario(franquia.id, conta.id, email="paid-outra@test.com", categoria="pro")
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_outra",
            subscription_id="sub_outra",
            plano_interno="pro",
        )
        _registrar_fato_invoice(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            event_type="invoice.payment_failed",
            invoice_id="in_falha_ativa",
            customer_id="cus_outra",
            subscription_id="sub_outra",
        )
        _registrar_fato_invoice(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            event_type="invoice.paid",
            invoice_id="in_outra",
            customer_id="cus_outra",
            subscription_id="sub_outra",
        )
        out = resolver_falha_mensal_vigente_conta(conta.id)
        assert out["falha_mensal_vigente"] is True
        assert out["invoice_id"] == "in_falha_ativa"


def test_eventos_fora_de_ordem_produzem_resultado_correto(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="falha-ordem")
        user = seed_usuario(franquia.id, conta.id, email="ordem@test.com", categoria="starter")
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_ordem",
            subscription_id="sub_ordem",
            plano_interno="starter",
        )
        _registrar_fato_invoice(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            event_type="invoice.paid",
            invoice_id="in_ordem",
            customer_id="cus_ordem",
            subscription_id="sub_ordem",
            event_id="evt_paid_primeiro",
        )
        _registrar_fato_invoice(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            event_type="invoice.payment_failed",
            invoice_id="in_ordem",
            customer_id="cus_ordem",
            subscription_id="sub_ordem",
            event_id="evt_failed_depois",
        )
        out = resolver_falha_mensal_vigente_conta(conta.id)
        assert out["falha_mensal_vigente"] is False


def test_resolvedor_nao_altera_banco(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="falha-readonly")
        user = seed_usuario(franquia.id, conta.id, email="readonly@test.com", categoria="pro")
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_ro",
            subscription_id="sub_ro",
            plano_interno="pro",
        )
        _registrar_fato_invoice(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            event_type="invoice.payment_failed",
            invoice_id="in_ro",
            customer_id="cus_ro",
            subscription_id="sub_ro",
        )
        count_antes = MonetizacaoFato.query.count()
        db.session.expunge_all()
        resolver_falha_mensal_vigente_conta(conta.id)
        assert MonetizacaoFato.query.count() == count_antes
        assert not db.session.new
        assert not db.session.dirty
        assert not db.session.deleted


# --- Apresentação ---


def _render_sidebar_creditos(app, *, falha_mensal_vigente, plano_exibicao, limite, consumo):
    from flask import render_template_string

    with app.app_context():
        user = SimpleNamespace(
            is_authenticated=True,
            full_name="Test User",
            email="test@example.com",
            categoria="pro",
            franquia=SimpleNamespace(
                limite_total=limite,
                consumo_acumulado=consumo,
            ),
        )
        html = render_template_string(
            """
            {% set limite_total = current_user.franquia.limite_total or 0 %}
            {% set consumo_acumulado = current_user.franquia.consumo_acumulado or 0 %}
            {% set saldo_bruto = limite_total - consumo_acumulado %}
            {% set saldo_creditos = saldo_bruto if saldo_bruto > 0 else 0 %}
            {% if falha_mensal_vigente %}
            <small>PLANO {{ plano_exibicao }}</small>
            <small>MENSAGEM Falha {{ plano_exibicao }}</small>
            <a href="{{ regularizacao_url }}">confirme ou atualize sua forma de pagamento</a>
            {% endif %}
            <small>Créditos: {% if falha_mensal_vigente %}--{% else %}{{ "%.2f"|format(saldo_creditos|float) }}{% endif %} / {{ "%.2f"|format(limite_total|float) }}</small>
            """,
            current_user=user,
            falha_mensal_vigente=falha_mensal_vigente,
            plano_exibicao=plano_exibicao,
            regularizacao_url="/perfil/regularizar-pagamento",
        )
        return html


def test_falha_vigente_mostra_plano_correto(app):
    html = _render_sidebar_creditos(
        app,
        falha_mensal_vigente=True,
        plano_exibicao="Pro",
        limite=Decimal("50"),
        consumo=Decimal("10"),
    )
    assert "Pro" in html


def test_falha_vigente_mostra_nova_mensagem(app):
    html = _render_sidebar_creditos(
        app,
        falha_mensal_vigente=True,
        plano_exibicao="Starter",
        limite=Decimal("50"),
        consumo=Decimal("10"),
    )
    assert "MENSAGEM Falha Starter" in html


def test_hyperlink_aponta_rota_interna(app):
    html = _render_sidebar_creditos(
        app,
        falha_mensal_vigente=True,
        plano_exibicao="Starter",
        limite=Decimal("50"),
        consumo=Decimal("10"),
    )
    assert 'href="/perfil/regularizar-pagamento"' in html
    assert "confirme ou atualize sua forma de pagamento" in html


def test_falha_vigente_mostra_mascara_creditos(app):
    html = _render_sidebar_creditos(
        app,
        falha_mensal_vigente=True,
        plano_exibicao="Pro",
        limite=Decimal("123.45"),
        consumo=Decimal("20"),
    )
    assert "Créditos: -- / 123.45" in html
    assert "103.45" not in html


def test_sem_falha_mantem_exibicao_atual(app):
    html = _render_sidebar_creditos(
        app,
        falha_mensal_vigente=False,
        plano_exibicao=None,
        limite=Decimal("100"),
        consumo=Decimal("25"),
    )
    assert "Créditos: 75.00 / 100.00" in html
    assert "confirme ou atualize" not in html


def test_renderizacao_nao_modifica_consumo_limite_saldo(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="falha-render")
        fr = db.session.get(Franquia, franquia.id)
        fr.limite_total = Decimal("80")
        fr.consumo_acumulado = Decimal("30")
        db.session.add(fr)
        db.session.commit()
        limite_antes = fr.limite_total
        consumo_antes = fr.consumo_acumulado
        saldo_antes = limite_antes - consumo_antes
        _render_sidebar_creditos(
            app,
            falha_mensal_vigente=True,
            plano_exibicao="Starter",
            limite=limite_antes,
            consumo=consumo_antes,
        )
        fr_depois = db.session.get(Franquia, franquia.id)
        assert fr_depois.limite_total == limite_antes
        assert fr_depois.consumo_acumulado == consumo_antes
        assert fr_depois.limite_total - fr_depois.consumo_acumulado == saldo_antes


# --- Autorização ---


def test_falha_vigente_preserva_permitido(monkeypatch):
    monkeypatch.setattr(
        authz_service,
        "ler_franquia_operacional_cleiton",
        lambda _fid, sincronizar_ciclo=True: _leitura_degraded(),
    )
    monkeypatch.setattr(
        monetizacao_service,
        "resolver_falha_mensal_vigente_conta",
        lambda _cid: {
            "falha_mensal_vigente": True,
            "plano_exibicao": "Starter",
        },
    )
    out = authz_service.avaliar_autorizacao_operacao_por_franquia(_user_auth())
    assert out["permitido"] is True


def test_falha_vigente_preserva_status_franquia(monkeypatch):
    monkeypatch.setattr(
        authz_service,
        "ler_franquia_operacional_cleiton",
        lambda _fid, sincronizar_ciclo=True: _leitura_degraded(),
    )
    monkeypatch.setattr(
        monetizacao_service,
        "resolver_falha_mensal_vigente_conta",
        lambda _cid: {"falha_mensal_vigente": True, "plano_exibicao": "Starter"},
    )
    out = authz_service.avaliar_autorizacao_operacao_por_franquia(_user_auth())
    assert out["status_franquia"] == FranquiaModel.STATUS_DEGRADED


def test_falha_vigente_preserva_modo_operacao(monkeypatch):
    monkeypatch.setattr(
        authz_service,
        "ler_franquia_operacional_cleiton",
        lambda _fid, sincronizar_ciclo=True: _leitura_degraded(),
    )
    monkeypatch.setattr(
        monetizacao_service,
        "resolver_falha_mensal_vigente_conta",
        lambda _cid: {"falha_mensal_vigente": True, "plano_exibicao": "Starter"},
    )
    out = authz_service.avaliar_autorizacao_operacao_por_franquia(_user_auth())
    assert out["modo_operacao"] == authz_service.MODO_OPERACAO_DEGRADED


def test_falha_vigente_preserva_motivo(monkeypatch):
    monkeypatch.setattr(
        authz_service,
        "ler_franquia_operacional_cleiton",
        lambda _fid, sincronizar_ciclo=True: _leitura_degraded(),
    )
    monkeypatch.setattr(
        monetizacao_service,
        "resolver_falha_mensal_vigente_conta",
        lambda _cid: {"falha_mensal_vigente": True, "plano_exibicao": "Starter"},
    )
    out = authz_service.avaliar_autorizacao_operacao_por_franquia(_user_auth())
    assert out["motivo"] == "limite_plano_atingido"


def test_falha_vigente_remove_mensagem_limite_upgrade(monkeypatch):
    monkeypatch.setattr(
        authz_service,
        "ler_franquia_operacional_cleiton",
        lambda _fid, sincronizar_ciclo=True: _leitura_degraded(),
    )
    monkeypatch.setattr(
        monetizacao_service,
        "resolver_falha_mensal_vigente_conta",
        lambda _cid: {"falha_mensal_vigente": True, "plano_exibicao": "Starter"},
    )
    out = authz_service.avaliar_autorizacao_operacao_por_franquia(_user_auth())
    assert "limite de uso" not in (out["mensagem_usuario"] or "").lower()
    assert "Starter" in out["mensagem_usuario"]
    assert "renovar" in out["mensagem_usuario"].lower()


def test_falha_vigente_remove_cta_upgrade(monkeypatch):
    monkeypatch.setattr(
        authz_service,
        "ler_franquia_operacional_cleiton",
        lambda _fid, sincronizar_ciclo=True: _leitura_degraded(),
    )
    monkeypatch.setattr(
        monetizacao_service,
        "resolver_falha_mensal_vigente_conta",
        lambda _cid: {"falha_mensal_vigente": True, "plano_exibicao": "Starter"},
    )
    out = authz_service.avaliar_autorizacao_operacao_por_franquia(_user_auth())
    assert out["upgrade_cta"] is None
    assert out["sugerir_upgrade"] is False


def test_sem_falha_mantem_retorno_autorizacao_atual(monkeypatch):
    monkeypatch.setattr(
        authz_service,
        "ler_franquia_operacional_cleiton",
        lambda _fid, sincronizar_ciclo=True: _leitura_degraded(),
    )
    monkeypatch.setattr(
        monetizacao_service,
        "resolver_falha_mensal_vigente_conta",
        lambda _cid: {"falha_mensal_vigente": False},
    )
    out = authz_service.avaliar_autorizacao_operacao_por_franquia(_user_auth())
    assert "limite de uso" in out["mensagem_usuario"].lower()
    assert out["upgrade_cta"] is not None


# --- Rotas ---


def test_get_regularizar_exige_login(app):
    client = _build_user_area_client(app)
    response = client.get("/perfil/regularizar-pagamento")
    assert response.status_code in (302, 401)


def test_post_regularizar_exige_login(app):
    client = _build_user_area_client(app)
    response = client.post("/perfil/regularizar-pagamento/stripe")
    assert response.status_code in (302, 401)


def test_post_usa_customer_da_conta_autenticada(app, monkeypatch):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="falha-post-cust")
        user = seed_usuario(franquia.id, conta.id, email="post-cust@test.com", categoria="pro")
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_conta_auth",
            subscription_id="sub_conta_auth",
            plano_interno="pro",
        )
        _registrar_fato_invoice(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            event_type="invoice.payment_failed",
            invoice_id="in_post_cust",
            customer_id="cus_conta_auth",
            subscription_id="sub_conta_auth",
        )
        captured = {}

        def _fake_stripe_post(path, payload, *, idempotency_key):
            captured["path"] = path
            captured["payload"] = payload
            return {"url": "https://billing.stripe.com/session/test"}

        monkeypatch.setattr(monetizacao_service, "_stripe_post", _fake_stripe_post)
        client = _build_user_area_client(app)
        _login(client, user)
        response = _post_regularizar_stripe(client, user, follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["Location"] == "https://billing.stripe.com/session/test"
        assert captured["payload"]["customer"] == "cus_conta_auth"


def test_ids_enviados_pelo_navegador_nao_sao_aceitos(app, monkeypatch):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="falha-post-browser")
        user = seed_usuario(franquia.id, conta.id, email="post-browser@test.com", categoria="pro")
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_real",
            subscription_id="sub_real",
            plano_interno="pro",
        )
        _registrar_fato_invoice(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            event_type="invoice.payment_failed",
            invoice_id="in_browser",
            customer_id="cus_real",
            subscription_id="sub_real",
        )
        captured = {}

        def _fake_stripe_post(path, payload, *, idempotency_key):
            captured["payload"] = payload
            return {"url": "https://billing.stripe.com/session/browser"}

        monkeypatch.setattr(monetizacao_service, "_stripe_post", _fake_stripe_post)
        client = _build_user_area_client(app)
        _login(client, user)
        response = _post_regularizar_stripe(
            client,
            user,
            extra={
                "customer_id": "cus_malicioso",
                "subscription_id": "sub_malicioso",
                "invoice_id": "in_malicioso",
            },
        )
        assert response.status_code == 302
        assert captured["payload"]["customer"] == "cus_real"


def test_ausencia_vinculo_produz_fallback(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="falha-sem-vinc")
        user = seed_usuario(franquia.id, conta.id, email="sem-vinc@test.com", categoria="pro")
        client = _build_user_area_client(app)
        _login(client, user)
        response = _post_regularizar_stripe(client, user, follow_redirects=False)
        assert response.status_code == 302
        assert "/perfil/regularizar-pagamento" in (response.location or "")
        with client.session_transaction() as sess:
            flashes = sess.get("_flashes", [])
        assert any("pagamento pendente" in (msg or "").lower() for _cat, msg in flashes)


def test_erro_stripe_produz_fallback(app, monkeypatch):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="falha-stripe-err")
        user = seed_usuario(franquia.id, conta.id, email="stripe-err@test.com", categoria="pro")
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_err",
            subscription_id="sub_err",
            plano_interno="pro",
        )
        _registrar_fato_invoice(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            event_type="invoice.payment_failed",
            invoice_id="in_err",
            customer_id="cus_err",
            subscription_id="sub_err",
        )

        def _raise(*args, **kwargs):
            raise ValueError("stripe indisponivel")

        monkeypatch.setattr(monetizacao_service, "_stripe_post", _raise)
        client = _build_user_area_client(app)
        _login(client, user)
        response = _post_regularizar_stripe(client, user, follow_redirects=False)
        assert response.status_code == 302
        assert "/perfil/regularizar-pagamento" in (response.location or "")
        with client.session_transaction() as sess:
            flashes = sess.get("_flashes", [])
        assert any("regulariza" in (msg or "").lower() for _cat, msg in flashes)


def test_portal_session_usa_endpoint_correto(app, monkeypatch):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="falha-endpoint")
        user = seed_usuario(franquia.id, conta.id, email="endpoint@test.com", categoria="pro")
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_ep",
            subscription_id="sub_ep",
            plano_interno="pro",
        )
        _registrar_fato_invoice(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            event_type="invoice.payment_failed",
            invoice_id="in_ep",
            customer_id="cus_ep",
            subscription_id="sub_ep",
        )
        captured = {}

        def _fake_stripe_post(path, payload, *, idempotency_key):
            captured["path"] = path
            return {"url": "https://billing.stripe.com/session/ep"}

        monkeypatch.setattr(monetizacao_service, "_stripe_post", _fake_stripe_post)
        client = _build_user_area_client(app)
        _login(client, user)
        _post_regularizar_stripe(client, user)
        assert captured["path"] == "/billing_portal/sessions"


def test_nao_cria_customer_subscription_ou_checkout(app, monkeypatch):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="falha-no-create")
        user = seed_usuario(franquia.id, conta.id, email="no-create@test.com", categoria="pro")
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_nc",
            subscription_id="sub_nc",
            plano_interno="pro",
        )
        _registrar_fato_invoice(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            event_type="invoice.payment_failed",
            invoice_id="in_nc",
            customer_id="cus_nc",
            subscription_id="sub_nc",
        )
        paths = []

        def _fake_stripe_post(path, payload, *, idempotency_key):
            paths.append(path)
            return {"url": "https://billing.stripe.com/session/nc"}

        monkeypatch.setattr(monetizacao_service, "_stripe_post", _fake_stripe_post)
        client = _build_user_area_client(app)
        _login(client, user)
        _post_regularizar_stripe(client, user)
        assert paths == ["/billing_portal/sessions"]


def test_criar_sessao_portal_pagamento_stripe_valida_resposta(monkeypatch):
    monkeypatch.setattr(
        monetizacao_service,
        "_stripe_post",
        lambda path, payload, idempotency_key: {"url": "https://billing.stripe.com/p/test"},
    )
    url = criar_sessao_portal_pagamento_stripe(
        customer_id="cus_unit",
        return_url="https://example.com/perfil/regularizar-pagamento",
    )
    assert url == "https://billing.stripe.com/p/test"


def test_billing_reason_constant():
    assert FALHA_MENSAL_BILLING_REASON == "subscription_cycle"


# --- Consistência Free / Starter / Pro ---


def test_free_sem_vinculo_starter_pro_permanece_inativo(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="free-sem-vinculo")
        seed_usuario(franquia.id, conta.id, email="free-sem-vinc@test.com", categoria="free")
        out = resolver_falha_mensal_vigente_conta(conta.id)
        assert out["falha_mensal_vigente"] is False


def test_free_com_vinculo_starter_falha_vigente_ativa(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="free-vinc-starter")
        user = seed_usuario(
            franquia.id,
            conta.id,
            email="free-vinc-starter@test.com",
            categoria="free",
        )
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_free_starter",
            subscription_id="sub_free_starter",
            plano_interno="starter",
        )
        _registrar_fato_invoice(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            event_type="invoice.payment_failed",
            invoice_id="in_free_starter",
            customer_id="cus_free_starter",
            subscription_id="sub_free_starter",
        )
        out = resolver_falha_mensal_vigente_conta(conta.id)
        assert out["falha_mensal_vigente"] is True
        assert out["plano_exibicao"] == "Starter"


def test_free_com_vinculo_pro_recebe_aviso_e_mascara(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="free-vinc-pro")
        user = seed_usuario(
            franquia.id,
            conta.id,
            email="free-vinc-pro@test.com",
            categoria="free",
        )
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_free_pro",
            subscription_id="sub_free_pro",
            plano_interno="pro",
        )
        _registrar_fato_invoice(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            event_type="invoice.payment_failed",
            invoice_id="in_free_pro",
            customer_id="cus_free_pro",
            subscription_id="sub_free_pro",
        )
        estado = resolver_falha_mensal_vigente_conta(conta.id)
        html = _render_sidebar_creditos(
            app,
            falha_mensal_vigente=estado["falha_mensal_vigente"],
            plano_exibicao=estado["plano_exibicao"],
            limite=Decimal("50"),
            consumo=Decimal("10"),
        )
        assert "Pro" in html
        assert "Créditos: -- / 50.00" in html


def test_autorizacao_e_resolvedor_coerentes_free_com_vinculo(app, monkeypatch):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="free-coerencia")
        user = seed_usuario(
            franquia.id,
            conta.id,
            email="free-coerencia@test.com",
            categoria="free",
        )
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_free_coer",
            subscription_id="sub_free_coer",
            plano_interno="starter",
        )
        _registrar_fato_invoice(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            event_type="invoice.payment_failed",
            invoice_id="in_free_coer",
            customer_id="cus_free_coer",
            subscription_id="sub_free_coer",
        )
        user_obj = db.session.get(User, user.id)
        categoria_antes = user_obj.categoria
        estado = resolver_falha_mensal_vigente_conta(conta.id)

        monkeypatch.setattr(
            authz_service,
            "ler_franquia_operacional_cleiton",
            lambda _fid, sincronizar_ciclo=True: _leitura_degraded("starter"),
        )
        authz = authz_service.avaliar_autorizacao_operacao_por_franquia(user_obj)

        assert estado["falha_mensal_vigente"] is True
        assert estado["plano_exibicao"] == "Starter"
        assert "renovar" in (authz["mensagem_usuario"] or "").lower()
        user_depois = db.session.get(User, user.id)
        assert user_depois.categoria == categoria_antes == "free"


def test_context_processor_nao_bloqueia_por_categoria_free():
    from pathlib import Path

    source = Path("app/web.py").read_text(encoding="utf-8")
    bloco = source.split("def inject_falha_mensal_vigente_context", 1)[1]
    bloco = bloco.split("def ", 1)[0]
    assert 'categoria == "free"' not in bloco


def test_starter_e_pro_continuam_funcionando(app):
    with app.app_context():
        conta_st, franquia_st = seed_conta_franquia_cliente(slug="starter-ok")
        user_st = seed_usuario(
            franquia_st.id,
            conta_st.id,
            email="starter-ok@test.com",
            categoria="starter",
        )
        _criar_vinculo_ativo(
            conta_id=conta_st.id,
            customer_id="cus_st_ok",
            subscription_id="sub_st_ok",
            plano_interno="starter",
        )
        _registrar_fato_invoice(
            conta_id=conta_st.id,
            franquia_id=franquia_st.id,
            usuario_id=user_st.id,
            event_type="invoice.payment_failed",
            invoice_id="in_st_ok",
            customer_id="cus_st_ok",
            subscription_id="sub_st_ok",
        )
        out_st = resolver_falha_mensal_vigente_conta(conta_st.id)
        assert out_st["falha_mensal_vigente"] is True
        assert out_st["plano_exibicao"] == "Starter"

        conta_pr, franquia_pr = seed_conta_franquia_cliente(slug="pro-ok")
        user_pr = seed_usuario(
            franquia_pr.id,
            conta_pr.id,
            email="pro-ok@test.com",
            categoria="pro",
        )
        _criar_vinculo_ativo(
            conta_id=conta_pr.id,
            customer_id="cus_pr_ok",
            subscription_id="sub_pr_ok",
            plano_interno="pro",
        )
        _registrar_fato_invoice(
            conta_id=conta_pr.id,
            franquia_id=franquia_pr.id,
            usuario_id=user_pr.id,
            event_type="invoice.payment_failed",
            invoice_id="in_pr_ok",
            customer_id="cus_pr_ok",
            subscription_id="sub_pr_ok",
        )
        out_pr = resolver_falha_mensal_vigente_conta(conta_pr.id)
        assert out_pr["falha_mensal_vigente"] is True
        assert out_pr["plano_exibicao"] == "Pro"


def test_resolvedor_nao_altera_categoria_usuario(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="categoria-intacta")
        user = seed_usuario(
            franquia.id,
            conta.id,
            email="categoria-intacta@test.com",
            categoria="free",
        )
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_cat",
            subscription_id="sub_cat",
            plano_interno="pro",
        )
        _registrar_fato_invoice(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            event_type="invoice.payment_failed",
            invoice_id="in_cat",
            customer_id="cus_cat",
            subscription_id="sub_cat",
        )
        categoria_antes = db.session.get(User, user.id).categoria
        resolver_falha_mensal_vigente_conta(conta.id)
        categoria_depois = db.session.get(User, user.id).categoria
        assert categoria_antes == categoria_depois == "free"


# --- CSRF ---


def test_post_sem_csrf_valido_e_recusado(app, monkeypatch):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="csrf-recusado")
        user = seed_usuario(franquia.id, conta.id, email="csrf-recusado@test.com", categoria="pro")
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_csrf",
            subscription_id="sub_csrf",
            plano_interno="pro",
        )
        _registrar_fato_invoice(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            event_type="invoice.payment_failed",
            invoice_id="in_csrf",
            customer_id="cus_csrf",
            subscription_id="sub_csrf",
        )
        stripe_calls = []

        def _fake_stripe_post(path, payload, *, idempotency_key):
            stripe_calls.append(path)
            return {"url": "https://billing.stripe.com/session/csrf"}

        monkeypatch.setattr(monetizacao_service, "_stripe_post", _fake_stripe_post)
        client = _build_user_area_client(app)
        _login(client, user)
        response = client.post("/perfil/regularizar-pagamento/stripe", follow_redirects=False)
        assert response.status_code == 302
        assert "/perfil/regularizar-pagamento" in (response.location or "")
        assert stripe_calls == []


def test_post_com_csrf_valido_prossegue(app, monkeypatch):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="csrf-ok")
        user = seed_usuario(franquia.id, conta.id, email="csrf-ok@test.com", categoria="pro")
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_csrf_ok",
            subscription_id="sub_csrf_ok",
            plano_interno="pro",
        )
        _registrar_fato_invoice(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            event_type="invoice.payment_failed",
            invoice_id="in_csrf_ok",
            customer_id="cus_csrf_ok",
            subscription_id="sub_csrf_ok",
        )

        def _fake_stripe_post(path, payload, *, idempotency_key):
            return {"url": "https://billing.stripe.com/session/csrf_ok"}

        monkeypatch.setattr(monetizacao_service, "_stripe_post", _fake_stripe_post)
        client = _build_user_area_client(app)
        _login(client, user)
        response = _post_regularizar_stripe(client, user, follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["Location"] == "https://billing.stripe.com/session/csrf_ok"


def test_get_regularizar_acessivel_usuario_autenticado(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="get-regularizar")
        user = seed_usuario(
            franquia.id,
            conta.id,
            email="get-regularizar@test.com",
            categoria="pro",
        )
        client = _build_user_area_client(app)
        _login(client, user)
        response = client.get("/perfil/regularizar-pagamento")
        assert response.status_code == 200
        assert b"Regularizar pagamento" in response.data


def test_csrf_falha_nao_cria_sessao_stripe(app, monkeypatch):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="csrf-no-stripe")
        user = seed_usuario(
            franquia.id,
            conta.id,
            email="csrf-no-stripe@test.com",
            categoria="pro",
        )
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_no_stripe",
            subscription_id="sub_no_stripe",
            plano_interno="pro",
        )
        _registrar_fato_invoice(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            event_type="invoice.payment_failed",
            invoice_id="in_no_stripe",
            customer_id="cus_no_stripe",
            subscription_id="sub_no_stripe",
        )
        paths = []

        def _fake_stripe_post(path, payload, *, idempotency_key):
            paths.append(path)
            return {"url": "https://billing.stripe.com/session/no"}

        monkeypatch.setattr(monetizacao_service, "_stripe_post", _fake_stripe_post)
        client = _build_user_area_client(app)
        _login(client, user)
        client.post("/perfil/regularizar-pagamento/stripe", data={"csrf_token": "invalido"})
        assert paths == []


# --- Validação URL Customer Portal ---


def test_url_portal_https_stripe_valida():
    assert _validar_url_portal_stripe("https://billing.stripe.com/p/session/test_abc") is True


def test_url_portal_http_recusada():
    assert _validar_url_portal_stripe("http://billing.stripe.com/p/session/test") is False


def test_url_portal_dominio_externo_recusado():
    assert _validar_url_portal_stripe("https://evil.example.com/p/session/test") is False


def test_url_portal_stripe_com_evil_recusado():
    assert _validar_url_portal_stripe("https://stripe.com.evil.example/p/session/test") is False


def test_url_portal_relativa_recusada():
    assert _validar_url_portal_stripe("/p/session/test") is False


def test_url_portal_vazia_recusada():
    assert _validar_url_portal_stripe("") is False
    assert _validar_url_portal_stripe(None) is False


def test_url_portal_credenciais_embutidas_recusadas():
    assert _validar_url_portal_stripe("https://user:pass@billing.stripe.com/p/session/test") is False


def test_resposta_portal_invalida_nao_redireciona_externo(app, monkeypatch):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="portal-invalido")
        user = seed_usuario(
            franquia.id,
            conta.id,
            email="portal-invalido@test.com",
            categoria="pro",
        )
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_portal_inv",
            subscription_id="sub_portal_inv",
            plano_interno="pro",
        )
        _registrar_fato_invoice(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            event_type="invoice.payment_failed",
            invoice_id="in_portal_inv",
            customer_id="cus_portal_inv",
            subscription_id="sub_portal_inv",
        )

        def _fake_stripe_post(path, payload, *, idempotency_key):
            return {"url": "https://evil.example.com/portal"}

        monkeypatch.setattr(monetizacao_service, "_stripe_post", _fake_stripe_post)
        client = _build_user_area_client(app)
        _login(client, user)
        response = _post_regularizar_stripe(client, user, follow_redirects=False)
        assert response.status_code == 302
        location = response.location or ""
        assert "evil.example.com" not in location
        assert "/perfil/regularizar-pagamento" in location


# --- Remoção hardcode Starter/Pro ---


def test_vinculo_starter_retorna_starter(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="plano-starter")
        user = seed_usuario(franquia.id, conta.id, email="plano-starter@test.com", categoria="starter")
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_pl_st",
            subscription_id="sub_pl_st",
            plano_interno="starter",
        )
        _registrar_fato_invoice(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            event_type="invoice.payment_failed",
            invoice_id="in_pl_st",
            customer_id="cus_pl_st",
            subscription_id="sub_pl_st",
        )
        out = resolver_falha_mensal_vigente_conta(conta.id)
        assert out["plano_exibicao"] == "Starter"


def test_vinculo_pro_retorna_pro(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="plano-pro")
        user = seed_usuario(franquia.id, conta.id, email="plano-pro@test.com", categoria="pro")
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_pl_pr",
            subscription_id="sub_pl_pr",
            plano_interno="pro",
        )
        _registrar_fato_invoice(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            event_type="invoice.payment_failed",
            invoice_id="in_pl_pr",
            customer_id="cus_pl_pr",
            subscription_id="sub_pl_pr",
        )
        out = resolver_falha_mensal_vigente_conta(conta.id)
        assert out["plano_exibicao"] == "Pro"


def test_plano_ausente_vinculo_retorna_inativo(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="plano-ausente")
        user = seed_usuario(franquia.id, conta.id, email="plano-ausente@test.com", categoria="pro")
        row = _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_pl_aus",
            subscription_id="sub_pl_aus",
            plano_interno="starter",
        )
        row.plano_interno = ""
        db.session.add(row)
        db.session.commit()
        _registrar_fato_invoice(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            event_type="invoice.payment_failed",
            invoice_id="in_pl_aus",
            customer_id="cus_pl_aus",
            subscription_id="sub_pl_aus",
        )
        out = resolver_falha_mensal_vigente_conta(conta.id)
        assert out["falha_mensal_vigente"] is False


def test_plano_invalido_vinculo_retorna_inativo(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="plano-invalido")
        user = seed_usuario(franquia.id, conta.id, email="plano-invalido@test.com", categoria="pro")
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_pl_inv",
            subscription_id="sub_pl_inv",
            plano_interno="enterprise",
        )
        _registrar_fato_invoice(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            event_type="invoice.payment_failed",
            invoice_id="in_pl_inv",
            customer_id="cus_pl_inv",
            subscription_id="sub_pl_inv",
        )
        out = resolver_falha_mensal_vigente_conta(conta.id)
        assert out["falha_mensal_vigente"] is False


def test_autorizacao_nao_inventa_starter_sem_plano_exibicao(monkeypatch):
    monkeypatch.setattr(
        authz_service,
        "ler_franquia_operacional_cleiton",
        lambda _fid, sincronizar_ciclo=True: _leitura_degraded(),
    )
    monkeypatch.setattr(
        monetizacao_service,
        "resolver_falha_mensal_vigente_conta",
        lambda _cid: {"falha_mensal_vigente": True, "plano_exibicao": None},
    )
    out = authz_service.avaliar_autorizacao_operacao_por_franquia(_user_auth())
    assert "renovar" not in (out["mensagem_usuario"] or "").lower()
    assert "não conseguimos renovar" not in (out["mensagem_usuario"] or "").lower()


def test_mensagem_renovacao_nao_exibida_sem_plano_valido(monkeypatch):
    from app.services.cleiton_monetizacao_service import montar_mensagem_renovacao_falha_mensal

    assert montar_mensagem_renovacao_falha_mensal("") == ""
    assert montar_mensagem_renovacao_falha_mensal("   ") == ""

    monkeypatch.setattr(
        authz_service,
        "ler_franquia_operacional_cleiton",
        lambda _fid, sincronizar_ciclo=True: _leitura_degraded(),
    )
    monkeypatch.setattr(
        monetizacao_service,
        "resolver_falha_mensal_vigente_conta",
        lambda _cid: {"falha_mensal_vigente": True, "plano_exibicao": ""},
    )
    out = authz_service.avaliar_autorizacao_operacao_por_franquia(_user_auth())
    assert "renovar" not in (out["mensagem_usuario"] or "").lower()
