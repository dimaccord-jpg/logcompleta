"""Cobertura dos bloqueios para promoção (Stripe monetização)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.extensions import db, login_manager
from app.infra import get_user_by_id
from app.models import ContaMonetizacaoVinculo, Franquia, MonetizacaoFato, User, utcnow_naive
from app.services import cleiton_monetizacao_service as monetizacao_service
from app.services.cleiton_monetizacao_service import (
    efetivar_mudancas_pendentes_ciclo,
    iniciar_jornada_assinatura_stripe,
    processar_evento_stripe,
    processar_fato_stripe_conciliado,
    registrar_fato_monetizacao,
    registrar_vinculo_comercial_externo,
)
from app.user_area import user_bp
from tests.conftest import seed_conta_franquia_cliente, seed_usuario


def _criar_vinculo_ativo(
    *,
    conta_id: int,
    customer_id: str,
    subscription_id: str,
    plano_interno: str = "pro",
    snapshot: dict | None = None,
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
        snapshot_normalizado_json=monetizacao_service._json_dumps(snapshot or {}),
        payload_bruto_sanitizado_json=monetizacao_service._json_dumps({"origem": "teste"}),
    )
    db.session.add(row)
    db.session.commit()
    return row


def _contar_fatos(tipo_fato: str) -> int:
    return MonetizacaoFato.query.filter(MonetizacaoFato.tipo_fato == tipo_fato).count()


def _buscar_fato_mais_recente(tipo_fato: str) -> MonetizacaoFato:
    row = (
        MonetizacaoFato.query.filter(MonetizacaoFato.tipo_fato == tipo_fato)
        .order_by(MonetizacaoFato.id.desc())
        .first()
    )
    assert row is not None, f"Fato esperado nao encontrado: {tipo_fato}"
    return row


def _capturar_identidade_fato(fato: MonetizacaoFato) -> dict:
    return {
        "id": int(fato.id),
        "tipo_fato": fato.tipo_fato,
        "idempotency_key": fato.idempotency_key,
        "correlation_key": fato.correlation_key,
        "external_event_id": fato.external_event_id,
        "conta_id": fato.conta_id,
        "franquia_id": fato.franquia_id,
        "usuario_id": fato.usuario_id,
    }


def _assert_replay_estavel_por_idempotencia(tipo_fato: str, antes: dict, depois: dict) -> None:
    assert antes["tipo_fato"] == tipo_fato
    assert depois["tipo_fato"] == tipo_fato
    assert antes["idempotency_key"], f"{tipo_fato} sem idempotency_key deterministica"
    assert depois["idempotency_key"] == antes["idempotency_key"]
    assert depois["id"] == antes["id"]
    assert depois["correlation_key"] == antes["correlation_key"]
    assert depois["external_event_id"] == antes["external_event_id"]
    assert depois["conta_id"] == antes["conta_id"]
    assert depois["franquia_id"] == antes["franquia_id"]
    assert depois["usuario_id"] == antes["usuario_id"]
    assert (
        MonetizacaoFato.query.filter_by(idempotency_key=antes["idempotency_key"]).count() == 1
    ), f"{tipo_fato} gerou duplicacao indevida por idempotency_key"


def _login(client, user: User) -> None:
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def _build_user_area_client(app):
    app.config["SECRET_KEY"] = "test-secret-http"
    app.config["TESTING"] = True
    if "user" not in app.blueprints:
        app.register_blueprint(user_bp)
    login_manager.init_app(app)

    @login_manager.user_loader
    def _load_user(user_id):  # noqa: ANN001
        return get_user_by_id(user_id)

    return app.test_client()


def test_http_iniciar_bloqueia_com_mudanca_pendente(app, monkeypatch):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-http-bloq")
        user = seed_usuario(franquia.id, conta.id, email="http-bloq@test.com", categoria="pro")
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_http_bloq",
            subscription_id="sub_http_bloq",
            snapshot={
                "mudanca_pendente": True,
                "tipo_mudanca": "downgrade",
                "plano_futuro": "starter",
                "efetivar_em": (utcnow_naive() + timedelta(days=5)).isoformat(),
            },
        )

        monkeypatch.setattr(
            monetizacao_service.plano_service,
            "obter_configuracao_gateway_plano_admin",
            lambda plano: {"configuracao_valida": True, "price_id": f"price_{plano}"},
        )
        monkeypatch.setattr(monetizacao_service, "_obter_publishable_key_stripe", lambda: "pk_test")
        monkeypatch.setattr(monetizacao_service, "_obter_assinatura_stripe_ativa", lambda conta_id: None)

        client = _build_user_area_client(app)
        _login(client, user)
        response = client.post(
            "/api/contratacao/stripe/iniciar",
            json={"plano_codigo": "pro"},
        )

        payload = response.get_json()
        assert response.status_code == 400
        assert payload["ok"] is False
        assert payload["codigo_erro"] == "contratacao_stripe_requisicao_invalida"
        assert "alteracao de plano pendente" in payload["erro"].lower()


def test_http_iniciar_permite_upgrade_imediato_canonico(app, monkeypatch):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-http-upgrade")
        user = seed_usuario(franquia.id, conta.id, email="http-up@test.com", categoria="starter")
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_http_up",
            subscription_id="sub_http_up",
            plano_interno="starter",
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
            lambda conta_id: {
                "subscription_id": "sub_http_up",
                "subscription_item_id": "si_http_up",
                "customer_id": "cus_http_up",
            },
        )
        monkeypatch.setattr(
            monetizacao_service,
            "_stripe_post",
            lambda path, payload, idempotency_key=None: {  # noqa: ARG001
                "id": "sub_http_up",
                "customer": "cus_http_up",
                "status": "active",
                "metadata": {"plano_interno": "pro"},
                "items": {"data": [{"id": "si_http_up", "price": {"id": "price_pro"}}]},
            },
        )

        client = _build_user_area_client(app)
        _login(client, user)
        response = client.post(
            "/api/contratacao/stripe/iniciar",
            json={"plano_codigo": "pro"},
        )

        payload = response.get_json()
        assert response.status_code == 200
        assert payload["ok"] is True
        assert payload["assinatura_atualizada_sem_checkout"] is True
        assert payload["plano_codigo"] == "pro"


def test_conciliado_invoice_paid_divergente_nao_promove_vinculo(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-conc-inv-div")
        user = seed_usuario(franquia.id, conta.id, email="conc-inv@test.com", categoria="pro")
        agora = utcnow_naive()
        fr = db.session.get(Franquia, franquia.id)
        fr.inicio_ciclo = agora - timedelta(days=10)
        fr.fim_ciclo = agora + timedelta(days=20)
        fr.consumo_acumulado = Decimal("3")
        db.session.add(fr)
        db.session.commit()
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_can_ci",
            subscription_id="sub_can_ci",
            snapshot={
                "mudanca_pendente": True,
                "tipo_mudanca": "downgrade",
                "plano_futuro": "starter",
                "efetivar_em": (agora + timedelta(days=20)).isoformat(),
            },
        )

        resultado = processar_fato_stripe_conciliado(
            event_type="invoice.paid",
            event_id="evt_conc_inv_div",
            created_at=agora,
            object_data={
                "id": "in_conc_1",
                "customer": "cus_div_ci",
                "subscription": "sub_div_ci",
                "metadata": {
                    "conta_id": str(conta.id),
                    "franquia_id": str(franquia.id),
                    "usuario_id": str(user.id),
                    "plano_interno": "pro",
                },
                "lines": {
                    "data": [
                        {
                            "period": {
                                "start": int((agora - timedelta(days=1)).timestamp()),
                                "end": int((agora + timedelta(days=29)).timestamp()),
                            },
                            "price": {"id": "price_pro"},
                        }
                    ]
                },
            },
            session_id="cs_conc_inv",
        )

        vinculo_ativo = monetizacao_service._obter_vinculo_ativo_por_conta(conta.id)
        fr_after = db.session.get(Franquia, franquia.id)
        assert resultado["vinculo_guardrail_bloqueado"] is True
        assert resultado["efeito_operacional_aplicado"] is False
        assert vinculo_ativo.subscription_id == "sub_can_ci"
        assert vinculo_ativo.customer_id == "cus_can_ci"
        assert fr_after.consumo_acumulado == Decimal("3")


def test_conciliado_subscription_updated_divergente_nao_promove_vinculo(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-conc-sub-div")
        user = seed_usuario(franquia.id, conta.id, email="conc-sub@test.com", categoria="pro")
        agora = utcnow_naive()
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_can_cs",
            subscription_id="sub_can_cs",
            snapshot={
                "mudanca_pendente": True,
                "tipo_mudanca": "downgrade",
                "plano_futuro": "starter",
                "efetivar_em": (agora + timedelta(days=10)).isoformat(),
            },
        )

        resultado = processar_fato_stripe_conciliado(
            event_type="customer.subscription.updated",
            event_id="evt_conc_sub_div",
            created_at=agora,
            session_id="cs_conc_sub",
            object_data={
                "id": "sub_div_cs",
                "customer": "cus_div_cs",
                "status": "active",
                "current_period_start": int((agora - timedelta(days=1)).timestamp()),
                "current_period_end": int((agora + timedelta(days=29)).timestamp()),
                "items": {"data": [{"price": {"id": "price_pro"}}]},
                "metadata": {
                    "conta_id": str(conta.id),
                    "franquia_id": str(franquia.id),
                    "usuario_id": str(user.id),
                    "plano_interno": "pro",
                },
            },
        )

        vinculo_ativo = monetizacao_service._obter_vinculo_ativo_por_conta(conta.id)
        assert resultado["vinculo_guardrail_bloqueado"] is True
        assert resultado["efeito_operacional_aplicado"] is False
        assert vinculo_ativo.subscription_id == "sub_can_cs"
        assert vinculo_ativo.customer_id == "cus_can_cs"


def test_conciliado_deleted_efetiva_free_consistente(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-conc-deleted")
        seed_usuario(franquia.id, conta.id, email="conc-del@test.com", categoria="pro")
        agora = utcnow_naive()
        fr = db.session.get(Franquia, franquia.id)
        fr.inicio_ciclo = agora - timedelta(days=31)
        fr.fim_ciclo = agora - timedelta(days=1)
        fr.consumo_acumulado = Decimal("9")
        db.session.add(fr)
        db.session.commit()
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_can_del",
            subscription_id="sub_can_del",
            snapshot={
                "mudanca_pendente": True,
                "tipo_mudanca": "downgrade",
                "plano_futuro": "free",
                "efetivar_em": (agora - timedelta(minutes=10)).isoformat(),
            },
        )

        resultado = processar_fato_stripe_conciliado(
            event_type="customer.subscription.deleted",
            event_id="evt_conc_deleted",
            created_at=agora,
            session_id="cs_conc_deleted",
            object_data={
                "id": "sub_can_del",
                "customer": "cus_can_del",
                "status": "canceled",
                "current_period_start": int((agora - timedelta(days=31)).timestamp()),
                "current_period_end": int((agora - timedelta(days=1)).timestamp()),
                "metadata": {
                    "conta_id": str(conta.id),
                    "franquia_id": str(franquia.id),
                    "plano_interno": "free",
                },
            },
        )

        fr_after = db.session.get(Franquia, franquia.id)
        assert resultado["vinculo_guardrail_bloqueado"] is False
        assert fr_after.fim_ciclo is None
        assert fr_after.consumo_acumulado == Decimal("0")
        assert fr_after.status != Franquia.STATUS_EXPIRED


def test_idempotencia_fato_checkout_guardrail_mudanca_pendente(app, monkeypatch):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-idem-checkout")
        user = seed_usuario(franquia.id, conta.id, email="idem-checkout@test.com", categoria="pro")
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_ic",
            subscription_id="sub_ic",
            snapshot={
                "mudanca_pendente": True,
                "tipo_mudanca": "downgrade",
                "plano_futuro": "starter",
                "efetivar_em": (utcnow_naive() + timedelta(days=2)).isoformat(),
            },
        )
        monkeypatch.setattr(
            monetizacao_service.plano_service,
            "obter_configuracao_gateway_plano_admin",
            lambda plano: {"configuracao_valida": True, "price_id": f"price_{plano}"},
        )
        monkeypatch.setattr(monetizacao_service, "_obter_publishable_key_stripe", lambda: "pk_test")
        monkeypatch.setattr(monetizacao_service, "_obter_assinatura_stripe_ativa", lambda conta_id: None)

        with pytest.raises(ValueError):
            iniciar_jornada_assinatura_stripe(user=user, plano_codigo="starter")
        fato_1 = _buscar_fato_mais_recente("stripe_checkout_guardrail_mudanca_pendente")
        snap_1 = _capturar_identidade_fato(fato_1)

        with pytest.raises(ValueError):
            iniciar_jornada_assinatura_stripe(user=user, plano_codigo="starter")
        fato_2 = db.session.get(MonetizacaoFato, snap_1["id"])
        assert fato_2 is not None
        snap_2 = _capturar_identidade_fato(fato_2)
        _assert_replay_estavel_por_idempotencia(
            "stripe_checkout_guardrail_mudanca_pendente",
            snap_1,
            snap_2,
        )


def test_idempotencia_fato_vinculo_persistencia_bloqueada(app):
    with app.app_context():
        conta, _fr = seed_conta_franquia_cliente(slug="conta-idem-vinculo")
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_canonico_v",
            subscription_id="sub_canonica_v",
        )

        registrar_vinculo_comercial_externo(
            conta_id=conta.id,
            provider="stripe",
            customer_id="cus_div_v",
            subscription_id="sub_div_v",
            plano_interno="pro",
            price_id="price_div",
            status_contratual_externo="active",
            substituir_vinculo_ativo=True,
        )
        fato_1 = _buscar_fato_mais_recente("stripe_vinculo_persistencia_bloqueada")
        snap_1 = _capturar_identidade_fato(fato_1)

        registrar_vinculo_comercial_externo(
            conta_id=conta.id,
            provider="stripe",
            customer_id="cus_div_v",
            subscription_id="sub_div_v",
            plano_interno="pro",
            price_id="price_div",
            status_contratual_externo="active",
            substituir_vinculo_ativo=True,
        )
        fato_2 = db.session.get(MonetizacaoFato, snap_1["id"])
        assert fato_2 is not None
        snap_2 = _capturar_identidade_fato(fato_2)
        _assert_replay_estavel_por_idempotencia(
            "stripe_vinculo_persistencia_bloqueada",
            snap_1,
            snap_2,
        )


def test_idempotencia_fatos_deleted_pendencia_e_free_efetivado(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-idem-deleted")
        seed_usuario(franquia.id, conta.id, email="idem-del@test.com", categoria="pro")
        agora = utcnow_naive()
        fr = db.session.get(Franquia, franquia.id)
        fr.inicio_ciclo = agora - timedelta(days=31)
        fr.fim_ciclo = agora - timedelta(days=1)
        fr.consumo_acumulado = Decimal("12")
        db.session.add(fr)
        db.session.commit()
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_idem_del",
            subscription_id="sub_idem_del",
            snapshot={
                "mudanca_pendente": True,
                "tipo_mudanca": "downgrade",
                "plano_futuro": "free",
                "efetivar_em": (agora - timedelta(minutes=5)).isoformat(),
            },
        )

        args_deleted = {
            "franquia_id": franquia.id,
            "plano_codigo": "free",
            "event_type": "customer.subscription.deleted",
            "status_contratual_externo": "canceled",
            "ciclo": {
                "inicio_ciclo": agora - timedelta(days=31),
                "fim_ciclo": agora - timedelta(days=1),
                "fonte_ciclo": "stripe_periodo_assinatura_confirmado",
                "pendencias": [],
            },
        }
        monetizacao_service.aplicar_fato_contratual_em_franquia(**args_deleted)
        fato_pend_1 = _buscar_fato_mais_recente("stripe_subscription_deleted_pendencia_limpa")
        fato_free_1 = _buscar_fato_mais_recente("stripe_subscription_deleted_free_efetivado")
        snap_pend_1 = _capturar_identidade_fato(fato_pend_1)
        snap_free_1 = _capturar_identidade_fato(fato_free_1)

        monetizacao_service.aplicar_fato_contratual_em_franquia(**args_deleted)
        fato_pend_2 = db.session.get(MonetizacaoFato, snap_pend_1["id"])
        fato_free_2 = db.session.get(MonetizacaoFato, snap_free_1["id"])
        assert fato_pend_2 is not None
        assert fato_free_2 is not None
        snap_pend_2 = _capturar_identidade_fato(fato_pend_2)
        snap_free_2 = _capturar_identidade_fato(fato_free_2)
        _assert_replay_estavel_por_idempotencia(
            "stripe_subscription_deleted_pendencia_limpa",
            snap_pend_1,
            snap_pend_2,
        )
        _assert_replay_estavel_por_idempotencia(
            "stripe_subscription_deleted_free_efetivado",
            snap_free_1,
            snap_free_2,
        )


def test_idempotencia_fato_deleted_inconsistente_pos_status_quando_aplicavel(app, monkeypatch):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-idem-inconsistente")
        seed_usuario(franquia.id, conta.id, email="idem-inc@test.com", categoria="pro")
        agora = utcnow_naive()
        fr = db.session.get(Franquia, franquia.id)
        fr.inicio_ciclo = agora - timedelta(days=31)
        fr.fim_ciclo = agora - timedelta(days=1)
        fr.consumo_acumulado = Decimal("2")
        db.session.add(fr)
        db.session.commit()
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_inc",
            subscription_id="sub_inc",
        )

        def _status_forcando_inconsistencia(fid: int) -> None:
            fr_local = db.session.get(Franquia, int(fid))
            fr_local.fim_ciclo = utcnow_naive() + timedelta(days=1)
            db.session.add(fr_local)
            db.session.commit()

        monkeypatch.setattr(
            monetizacao_service,
            "aplicar_status_apos_mudanca_estrutural",
            _status_forcando_inconsistencia,
        )

        args_deleted = {
            "franquia_id": franquia.id,
            "plano_codigo": "free",
            "event_type": "customer.subscription.deleted",
            "status_contratual_externo": "canceled",
            "ciclo": {
                "inicio_ciclo": agora - timedelta(days=31),
                "fim_ciclo": agora - timedelta(days=1),
                "fonte_ciclo": "stripe_periodo_assinatura_confirmado",
                "pendencias": [],
            },
        }
        monetizacao_service.aplicar_fato_contratual_em_franquia(**args_deleted)
        fato_1 = _buscar_fato_mais_recente("stripe_subscription_deleted_free_inconsistente_pos_status")
        snap_1 = _capturar_identidade_fato(fato_1)

        monetizacao_service.aplicar_fato_contratual_em_franquia(**args_deleted)
        fato_2 = db.session.get(MonetizacaoFato, snap_1["id"])
        assert fato_2 is not None
        snap_2 = _capturar_identidade_fato(fato_2)
        _assert_replay_estavel_por_idempotencia(
            "stripe_subscription_deleted_free_inconsistente_pos_status",
            snap_1,
            snap_2,
        )


def test_idempotencia_fato_cron_pendencia_ausente_no_vinculo_ativo(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-idem-cron")
        seed_usuario(franquia.id, conta.id, email="idem-cron@test.com", categoria="pro")
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_cron",
            subscription_id="sub_cron",
            snapshot={},
        )
        registrar_fato_monetizacao(
            tipo_fato="stripe_invoice_paid",
            status_tecnico=monetizacao_service.STATUS_TEC_APLICADO,
            provider="stripe",
            conta_id=conta.id,
            franquia_id=franquia.id,
            idempotency_key="seed_pendencia_historica",
            snapshot_normalizado={
                "mudanca_pendente": True,
                "tipo_mudanca": "downgrade",
                "plano_futuro": "starter",
                "efetivar_em": (utcnow_naive() + timedelta(days=3)).isoformat(),
            },
            payload_bruto_sanitizado={"origem": "teste"},
        )
        db.session.commit()

        referencia = utcnow_naive() + timedelta(hours=1)
        efetivar_mudancas_pendentes_ciclo(agora=referencia, limite=30)
        fato_1 = _buscar_fato_mais_recente("cleiton_cron_pendencia_ausente_no_vinculo_ativo")
        snap_1 = _capturar_identidade_fato(fato_1)

        efetivar_mudancas_pendentes_ciclo(agora=referencia, limite=30)
        fato_2 = db.session.get(MonetizacaoFato, snap_1["id"])
        assert fato_2 is not None
        snap_2 = _capturar_identidade_fato(fato_2)
        _assert_replay_estavel_por_idempotencia(
            "cleiton_cron_pendencia_ausente_no_vinculo_ativo",
            snap_1,
            snap_2,
        )


def test_divergencia_customer_id_isolada_no_webhook_bloqueia_promocao(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-customer-webhook")
        user = seed_usuario(franquia.id, conta.id, email="cust-webhook@test.com", categoria="pro")
        agora = utcnow_naive()
        fr = db.session.get(Franquia, franquia.id)
        fr.consumo_acumulado = Decimal("4")
        fr.inicio_ciclo = agora - timedelta(days=1)
        fr.fim_ciclo = agora + timedelta(days=29)
        db.session.add(fr)
        db.session.commit()
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_can_customer",
            subscription_id="sub_can_customer",
            snapshot={
                "mudanca_pendente": True,
                "tipo_mudanca": "downgrade",
                "plano_futuro": "starter",
                "efetivar_em": (agora + timedelta(days=10)).isoformat(),
            },
        )

        resultado = processar_evento_stripe(
            {
                "id": "evt_customer_only_webhook",
                "type": "invoice.paid",
                "created": int(agora.timestamp()),
                "data": {
                    "object": {
                        "id": "in_customer_only",
                        "customer": "cus_divergente_customer_only",
                        "subscription": "sub_can_customer",
                        "metadata": {
                            "conta_id": str(conta.id),
                            "franquia_id": str(franquia.id),
                            "usuario_id": str(user.id),
                            "plano_interno": "pro",
                        },
                        "lines": {
                            "data": [
                                {
                                    "period": {
                                        "start": int((agora - timedelta(days=1)).timestamp()),
                                        "end": int((agora + timedelta(days=29)).timestamp()),
                                    },
                                    "price": {"id": "price_pro"},
                                }
                            ]
                        },
                    }
                },
            }
        )

        vinculo_ativo = monetizacao_service._obter_vinculo_ativo_por_conta(conta.id)
        assert resultado["vinculo_guardrail_bloqueado"] is True
        assert resultado["efeito_operacional_aplicado"] is False
        assert vinculo_ativo.subscription_id == "sub_can_customer"
        assert vinculo_ativo.customer_id == "cus_can_customer"
        assert _contar_fatos("stripe_vinculo_guardrail_ids_inconsistentes") >= 1


def test_divergencia_customer_id_isolada_na_conciliacao_bloqueia_promocao(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-customer-conc")
        user = seed_usuario(franquia.id, conta.id, email="cust-conc@test.com", categoria="pro")
        agora = utcnow_naive()
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_can_customer_cc",
            subscription_id="sub_can_customer_cc",
            snapshot={
                "mudanca_pendente": True,
                "tipo_mudanca": "downgrade",
                "plano_futuro": "starter",
                "efetivar_em": (agora + timedelta(days=10)).isoformat(),
            },
        )

        resultado = processar_fato_stripe_conciliado(
            event_type="customer.subscription.updated",
            event_id="evt_customer_only_conc",
            created_at=agora,
            session_id="cs_customer_only_conc",
            object_data={
                "id": "sub_can_customer_cc",
                "customer": "cus_divergente_customer_only_cc",
                "status": "active",
                "current_period_start": int((agora - timedelta(days=1)).timestamp()),
                "current_period_end": int((agora + timedelta(days=29)).timestamp()),
                "items": {"data": [{"price": {"id": "price_pro"}}]},
                "metadata": {
                    "conta_id": str(conta.id),
                    "franquia_id": str(franquia.id),
                    "usuario_id": str(user.id),
                    "plano_interno": "pro",
                },
            },
        )

        vinculo_ativo = monetizacao_service._obter_vinculo_ativo_por_conta(conta.id)
        assert resultado["vinculo_guardrail_bloqueado"] is True
        assert resultado["efeito_operacional_aplicado"] is False
        assert vinculo_ativo.subscription_id == "sub_can_customer_cc"
        assert vinculo_ativo.customer_id == "cus_can_customer_cc"
