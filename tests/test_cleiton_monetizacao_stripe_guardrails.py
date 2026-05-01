"""Cobertura de regressao para guardrails Stripe de monetizacao Cleiton."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import ContaMonetizacaoVinculo, Franquia, MonetizacaoFato
from app.services import cleiton_monetizacao_service as monetizacao_service
from app.services.cleiton_ciclo_franquia_service import garantir_ciclo_operacional_franquia
from app.services.cleiton_franquia_operacional_service import aplicar_status_apos_mudanca_estrutural
from app.services.cleiton_monetizacao_service import (
    efetivar_mudancas_pendentes_ciclo,
    iniciar_jornada_assinatura_stripe,
    processar_evento_stripe,
)
from app.models import utcnow_naive
from tests.conftest import seed_conta_franquia_cliente, seed_usuario


def _criar_vinculo_ativo(
    *,
    conta_id: int,
    customer_id: str,
    subscription_id: str,
    plano_interno: str,
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


def _fato_existe(tipo_fato: str) -> bool:
    return (
        MonetizacaoFato.query.filter(MonetizacaoFato.tipo_fato == tipo_fato)
        .order_by(MonetizacaoFato.id.desc())
        .first()
        is not None
    )


def test_deleted_efetiva_free_com_ciclo_consistente(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-stripe-del")
        usuario = seed_usuario(franquia.id, conta.id, email="del@test.com", categoria="pro")
        agora = utcnow_naive()
        fr = db.session.get(Franquia, franquia.id)
        fr.inicio_ciclo = agora - timedelta(days=31)
        fr.fim_ciclo = agora - timedelta(days=1)
        fr.limite_total = Decimal("100")
        fr.consumo_acumulado = Decimal("10")
        fr.status = Franquia.STATUS_ACTIVE
        db.session.add(fr)
        db.session.commit()
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_canonico",
            subscription_id="sub_canonica",
            plano_interno="pro",
        )

        resultado = monetizacao_service.aplicar_fato_contratual_em_franquia(
            franquia_id=franquia.id,
            plano_codigo="free",
            event_type="customer.subscription.deleted",
            status_contratual_externo="canceled",
            ciclo={
                "inicio_ciclo": agora - timedelta(days=31),
                "fim_ciclo": agora - timedelta(days=1),
                "fonte_ciclo": "stripe_periodo_assinatura_confirmado",
                "pendencias": [],
            },
        )

        fr_after = db.session.get(Franquia, franquia.id)
        user_after = db.session.get(type(usuario), usuario.id)
        assert resultado["mudanca_pendente"] is False
        assert user_after.categoria == "free"
        assert fr_after.fim_ciclo is None
        assert fr_after.consumo_acumulado == Decimal("0")
        assert fr_after.status != Franquia.STATUS_EXPIRED
        assert _fato_existe("stripe_subscription_deleted_free_efetivado")


def test_deleted_e_cron_nao_duplicam_efeito(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-stripe-del-cron")
        seed_usuario(franquia.id, conta.id, email="del-cron@test.com", categoria="pro")
        agora = utcnow_naive()
        fr = db.session.get(Franquia, franquia.id)
        fr.inicio_ciclo = agora - timedelta(days=31)
        fr.fim_ciclo = agora - timedelta(days=1)
        fr.consumo_acumulado = Decimal("7")
        db.session.add(fr)
        db.session.commit()
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_canonico_cron",
            subscription_id="sub_canonica_cron",
            plano_interno="pro",
        )

        monetizacao_service.aplicar_fato_contratual_em_franquia(
            franquia_id=franquia.id,
            plano_codigo="free",
            event_type="customer.subscription.deleted",
            status_contratual_externo="canceled",
            ciclo={
                "inicio_ciclo": agora - timedelta(days=31),
                "fim_ciclo": agora - timedelta(days=1),
                "fonte_ciclo": "stripe_periodo_assinatura_confirmado",
                "pendencias": [],
            },
        )
        saida_cron = efetivar_mudancas_pendentes_ciclo(agora=agora + timedelta(hours=1), limite=50)

        fr_after = db.session.get(Franquia, franquia.id)
        assert saida_cron["efetivados"] == 0
        assert fr_after.fim_ciclo is None
        assert fr_after.consumo_acumulado == Decimal("0")
        assert fr_after.status != Franquia.STATUS_EXPIRED


def test_pendencia_bloqueia_novo_checkout_e_preserva_snapshot(app, monkeypatch):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-pendencia-checkout")
        usuario = seed_usuario(franquia.id, conta.id, email="pendente@test.com", categoria="pro")
        efetivar_em = (utcnow_naive() + timedelta(days=5)).isoformat()
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_can",
            subscription_id="sub_can",
            plano_interno="pro",
            snapshot={
                "mudanca_pendente": True,
                "tipo_mudanca": "downgrade",
                "plano_futuro": "starter",
                "efetivar_em": efetivar_em,
                "origem": "solicitacao_usuario",
            },
        )

        monkeypatch.setattr(
            monetizacao_service.plano_service,
            "obter_configuracao_gateway_plano_admin",
            lambda plano: {"configuracao_valida": True, "price_id": f"price_{plano}"},
        )
        monkeypatch.setattr(monetizacao_service, "_obter_publishable_key_stripe", lambda: "pk_test_123")
        monkeypatch.setattr(monetizacao_service, "_obter_assinatura_stripe_ativa", lambda conta_id: None)

        with pytest.raises(ValueError, match="alteracao de plano pendente"):
            iniciar_jornada_assinatura_stripe(user=usuario, plano_codigo="starter")

        vinculo_ativo = monetizacao_service._obter_vinculo_ativo_por_conta(conta.id)
        snapshot = monetizacao_service._json_loads(vinculo_ativo.snapshot_normalizado_json)
        assert snapshot.get("mudanca_pendente") is True
        assert snapshot.get("plano_futuro") == "starter"
        assert _fato_existe("stripe_checkout_guardrail_mudanca_pendente")


def test_invoice_paid_divergente_durante_pendencia_nao_troca_vinculo(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-div-invoice")
        user = seed_usuario(franquia.id, conta.id, email="div-invoice@test.com", categoria="pro")
        agora = utcnow_naive()
        fr = db.session.get(Franquia, franquia.id)
        fr.inicio_ciclo = agora - timedelta(days=10)
        fr.fim_ciclo = agora + timedelta(days=20)
        fr.consumo_acumulado = Decimal("5")
        db.session.add(fr)
        db.session.commit()
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_can",
            subscription_id="sub_can",
            plano_interno="pro",
            snapshot={
                "mudanca_pendente": True,
                "tipo_mudanca": "downgrade",
                "plano_futuro": "starter",
                "efetivar_em": (agora + timedelta(days=20)).isoformat(),
            },
        )

        evento = {
            "id": "evt_invoice_divergente",
            "type": "invoice.paid",
            "created": int(agora.timestamp()),
            "data": {
                "object": {
                    "id": "in_001",
                    "customer": "cus_div",
                    "subscription": "sub_div",
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
        resultado = processar_evento_stripe(evento)

        fr_after = db.session.get(Franquia, franquia.id)
        vinculo_ativo = monetizacao_service._obter_vinculo_ativo_por_conta(conta.id)
        assert resultado["vinculo_guardrail_bloqueado"] is True
        assert resultado["efeito_operacional_aplicado"] is False
        assert fr_after.consumo_acumulado == Decimal("5")
        assert vinculo_ativo.subscription_id == "sub_can"
        assert vinculo_ativo.customer_id == "cus_can"


def test_subscription_updated_divergente_durante_pendencia_nao_promove_vinculo(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-div-updated")
        user = seed_usuario(franquia.id, conta.id, email="div-updated@test.com", categoria="pro")
        agora = utcnow_naive()
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_can_upd",
            subscription_id="sub_can_upd",
            plano_interno="pro",
            snapshot={
                "mudanca_pendente": True,
                "tipo_mudanca": "downgrade",
                "plano_futuro": "starter",
                "efetivar_em": (agora + timedelta(days=10)).isoformat(),
            },
        )

        evento = {
            "id": "evt_sub_updated_divergente",
            "type": "customer.subscription.updated",
            "created": int(agora.timestamp()),
            "data": {
                "object": {
                    "id": "sub_div_upd",
                    "customer": "cus_div_upd",
                    "status": "active",
                    "current_period_start": int((agora - timedelta(days=1)).timestamp()),
                    "current_period_end": int((agora + timedelta(days=29)).timestamp()),
                    "metadata": {
                        "conta_id": str(conta.id),
                        "franquia_id": str(franquia.id),
                        "usuario_id": str(user.id),
                        "plano_interno": "pro",
                    },
                    "items": {"data": [{"price": {"id": "price_pro"}}]},
                }
            },
        }
        resultado = processar_evento_stripe(evento)

        vinculo_ativo = monetizacao_service._obter_vinculo_ativo_por_conta(conta.id)
        assert resultado["vinculo_guardrail_bloqueado"] is True
        assert vinculo_ativo.subscription_id == "sub_can_upd"
        assert _fato_existe("stripe_vinculo_guardrail_ids_inconsistentes")


def test_upgrade_imediato_normal_permanece_funcional(app, monkeypatch):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-upgrade-imediato")
        usuario = seed_usuario(franquia.id, conta.id, email="upgrade@test.com", categoria="starter")
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_upg",
            subscription_id="sub_upg",
            plano_interno="starter",
        )

        monkeypatch.setattr(
            monetizacao_service.plano_service,
            "obter_configuracao_gateway_plano_admin",
            lambda plano: {"configuracao_valida": True, "price_id": "price_pro"},
        )
        monkeypatch.setattr(monetizacao_service, "_obter_publishable_key_stripe", lambda: "pk_test_123")
        monkeypatch.setattr(
            monetizacao_service,
            "_obter_assinatura_stripe_ativa",
            lambda conta_id: {
                "subscription_id": "sub_upg",
                "subscription_item_id": "si_can",
                "customer_id": "cus_upg",
            },
        )
        chamadas = {"path": None}

        def _stripe_post(path, payload, idempotency_key=None):  # noqa: ARG001
            chamadas["path"] = path
            return {
                "id": "sub_upg",
                "customer": "cus_upg",
                "status": "active",
                "current_period_start": int(utcnow_naive().timestamp()),
                "current_period_end": int((utcnow_naive() + timedelta(days=30)).timestamp()),
                "metadata": {"plano_interno": "pro"},
                "items": {"data": [{"id": "si_can", "price": {"id": "price_pro"}}]},
            }

        monkeypatch.setattr(monetizacao_service, "_stripe_post", _stripe_post)

        saida = iniciar_jornada_assinatura_stripe(user=usuario, plano_codigo="pro")

        vinculo_ativo = monetizacao_service._obter_vinculo_ativo_por_conta(conta.id)
        assert saida["assinatura_atualizada_sem_checkout"] is True
        assert chamadas["path"] == "/subscriptions/sub_upg"
        assert vinculo_ativo.subscription_id == "sub_upg"


def test_subscription_modify_canonica_permitido_mesmo_com_pendencia(app, monkeypatch):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-modify-canonica")
        usuario = seed_usuario(franquia.id, conta.id, email="modify@test.com", categoria="starter")
        _criar_vinculo_ativo(
            conta_id=conta.id,
            customer_id="cus_mod",
            subscription_id="sub_mod",
            plano_interno="starter",
            snapshot={
                "mudanca_pendente": True,
                "tipo_mudanca": "downgrade",
                "plano_futuro": "free",
                "efetivar_em": (utcnow_naive() + timedelta(days=15)).isoformat(),
            },
        )

        monkeypatch.setattr(
            monetizacao_service.plano_service,
            "obter_configuracao_gateway_plano_admin",
            lambda plano: {"configuracao_valida": True, "price_id": "price_pro"},
        )
        monkeypatch.setattr(monetizacao_service, "_obter_publishable_key_stripe", lambda: "pk_test_123")
        monkeypatch.setattr(
            monetizacao_service,
            "_obter_assinatura_stripe_ativa",
            lambda conta_id: {
                "subscription_id": "sub_mod",
                "subscription_item_id": "si_mod",
                "customer_id": "cus_mod",
            },
        )
        monkeypatch.setattr(
            monetizacao_service,
            "_stripe_post",
            lambda path, payload, idempotency_key=None: {  # noqa: ARG001
                "id": "sub_mod",
                "customer": "cus_mod",
                "status": "active",
                "metadata": {"plano_interno": "pro"},
                "items": {"data": [{"id": "si_mod", "price": {"id": "price_pro"}}]},
            },
        )

        saida = iniciar_jornada_assinatura_stripe(user=usuario, plano_codigo="pro")

        assert saida["assinatura_atualizada_sem_checkout"] is True
        assert saida["plano_codigo"] == "pro"


def test_free_nativo_permanece_com_fim_ciclo_none(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-free-nativo")
        seed_usuario(franquia.id, conta.id, email="free-nativo@test.com", categoria="free")
        fr = db.session.get(Franquia, franquia.id)
        fr.inicio_ciclo = None
        fr.fim_ciclo = None
        fr.consumo_acumulado = Decimal("0")
        db.session.add(fr)
        db.session.commit()

        garantir_ciclo_operacional_franquia(franquia.id)
        aplicar_status_apos_mudanca_estrutural(franquia.id)

        fr_after = db.session.get(Franquia, franquia.id)
        assert fr_after.fim_ciclo is None
        assert fr_after.status in {Franquia.STATUS_ACTIVE, Franquia.STATUS_BLOCKED}
