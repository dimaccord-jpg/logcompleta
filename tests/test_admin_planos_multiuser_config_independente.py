"""Cadastro admin Multiuser: parâmetros administrativos independem do Stripe."""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.services import plano_service
from app.services.cleiton_monetizacao_service import (
    iniciar_jornada_assinatura_stripe,
    listar_planos_contratacao_publica,
)
from app.services.conta_multiuser_contratacao_service import (
    ConfiguracaoMultiuserIncompletaError,
    exigir_gateway_multiuser_pronto,
    multiuser_pronto_para_contratacao_publica,
)
from tests.conftest import seed_conta_franquia_cliente, seed_sistema_interno, seed_usuario

_MSG_PROVIDER_INVALIDO = "Provider externo inválido. Valores aceitos nesta fase: stripe."


def _salvar_admin_multiuser(**kwargs):
    payload = {
        "plano_codigo": "multiuser",
        "valor_plano_raw": "49.90",
        "franquia_limite_total_raw": "1000",
        "quantidade_minima_raw": "5",
        "limite_aumento_automatico_raw": "4",
        "gateway_provider_raw": None,
        "gateway_product_id_raw": None,
        "gateway_price_id_raw": None,
        "gateway_currency_raw": None,
        "gateway_interval_raw": None,
        "gateway_pronto_raw": False,
    }
    payload.update(kwargs)
    return plano_service.atualizar_parametros_plano_admin(**payload)


def _gateway_multiuser():
    planos = plano_service.listar_planos_saas_admin()
    return next(p for p in planos if p["codigo"] == "multiuser")["gateway_config"]


def test_a_admin_salva_com_stripe_vazio_e_externa_permanece_pendente(app):
    with app.app_context():
        seed_sistema_interno()
        resultado = _salvar_admin_multiuser(
            gateway_provider_raw=None,
            gateway_product_id_raw="",
            gateway_price_id_raw="",
            gateway_currency_raw="",
            gateway_interval_raw="",
            gateway_pronto_raw=False,
        )
        assert resultado["valor_plano"] == Decimal("49.90")
        assert resultado["franquia_limite_total"] == Decimal("1000.000000")
        assert resultado["quantidade_minima"] == 5
        assert resultado["limite_aumento_automatico_ciclo"] == 4
        gw = resultado["gateway_config"]
        assert gw["provider"] is None
        assert gw["product_id"] is None
        assert gw["price_id"] is None
        assert gw["configuracao_valida"] is False
        assert "gateway_provider_nao_configurado" in gw["pendencias"]
        gw2 = _gateway_multiuser()
        assert gw2["configuracao_valida"] is False
        assert gw2["pronto"] is False


def test_a_formulario_vazio_string_nao_bloqueia_save_admin(app):
    with app.app_context():
        seed_sistema_interno()
        gateway_provider = ("" or "").strip()
        resultado = _salvar_admin_multiuser(
            gateway_provider_raw=gateway_provider or None,
            gateway_product_id_raw="" or None,
            gateway_price_id_raw="" or None,
            gateway_currency_raw="" or None,
            gateway_interval_raw="" or None,
            gateway_pronto_raw=bool(""),
        )
        assert resultado["valor_plano"] == Decimal("49.90")
        assert resultado["gateway_config"]["configuracao_valida"] is False


def test_b_stripe_completo_salva_como_configuracao_externa_pronta(app):
    with app.app_context():
        seed_sistema_interno()
        resultado = _salvar_admin_multiuser(
            gateway_provider_raw="stripe",
            gateway_product_id_raw="prod_multiuser",
            gateway_price_id_raw="price_multiuser",
            gateway_currency_raw="brl",
            gateway_interval_raw="month",
            gateway_pronto_raw=True,
        )
        gw = resultado["gateway_config"]
        assert gw["provider"] == "stripe"
        assert gw["product_id"] == "prod_multiuser"
        assert gw["price_id"] == "price_multiuser"
        assert gw["currency"] == "brl"
        assert gw["interval"] == "month"
        assert gw["pronto"] is True
        assert gw["configuracao_valida"] is True
        assert gw["pendencias"] == []


def test_c_provider_nao_stripe_continua_recusado(app):
    with app.app_context():
        seed_sistema_interno()
        with pytest.raises(ValueError, match=_MSG_PROVIDER_INVALIDO):
            _salvar_admin_multiuser(gateway_provider_raw="qualquer_outro")
        with pytest.raises(ValueError, match=_MSG_PROVIDER_INVALIDO):
            _salvar_admin_multiuser(gateway_provider_raw="paypal")


def test_d_stripe_parcial_nao_e_configuracao_externa_pronta(app):
    with app.app_context():
        seed_sistema_interno()
        parcial = _salvar_admin_multiuser(
            gateway_provider_raw="stripe",
            gateway_product_id_raw="prod_parcial",
            gateway_price_id_raw=None,
            gateway_currency_raw="brl",
            gateway_interval_raw="month",
            gateway_pronto_raw=True,
        )
        gw = parcial["gateway_config"]
        assert gw["provider"] == "stripe"
        assert gw["configuracao_valida"] is False
        assert "gateway_price_id_nao_configurado" in gw["pendencias"]

        sem_pronto = _salvar_admin_multiuser(
            gateway_provider_raw="stripe",
            gateway_product_id_raw="prod_ok",
            gateway_price_id_raw="price_ok",
            gateway_currency_raw="brl",
            gateway_interval_raw="month",
            gateway_pronto_raw=False,
        )
        gw2 = sem_pronto["gateway_config"]
        assert gw2["configuracao_valida"] is False
        assert "gateway_pronto_desmarcado" in gw2["pendencias"]


def test_e_checkout_bloqueado_enquanto_externa_incompleta(app):
    with app.app_context():
        seed_sistema_interno()
        _salvar_admin_multiuser()
        assert multiuser_pronto_para_contratacao_publica() is False
        with pytest.raises(ConfiguracaoMultiuserIncompletaError):
            exigir_gateway_multiuser_pronto()
        publicos = listar_planos_contratacao_publica()
        assert all(p["codigo"] != "multiuser" for p in publicos)

        conta, franquia = seed_conta_franquia_cliente(slug="mu-admin-pendente")
        user = seed_usuario(
            franquia.id, conta.id, email="mu.admin.pendente@test.com"
        )
        with pytest.raises(ValueError, match="pendente"):
            iniciar_jornada_assinatura_stripe(
                user=user,
                plano_codigo="multiuser",
                quantity=5,
            )
