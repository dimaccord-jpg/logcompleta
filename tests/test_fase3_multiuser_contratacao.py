"""Testes incrementais da Fase 3 — contratação Multiusuário e quantity Stripe."""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import time
import uuid

import pytest

from app.extensions import db, login_manager
from app.infra import get_user_by_id
from app.models import (
    Conta,
    ContaMonetizacaoCheckoutIntencao,
    ContaMonetizacaoVinculo,
    ContaVinculoOrganizacional,
    Franquia,
    MonetizacaoFato,
    User,
    utcnow_naive,
)
from app.services import plano_service
from app.services.cnpj_service import _digito_verificador
from app.services.cleiton_monetizacao_service import (
    iniciar_jornada_assinatura_stripe,
    listar_planos_contratacao_publica,
    processar_evento_stripe,
    processar_fato_stripe_conciliado,
)
from app.services.conta_multiuser_contratacao_service import (
    ConfiguracaoMultiuserIncompletaError,
    QuantityMultiuserInvalidaError,
    calcular_resumo_contratacao_multiuser,
    persistir_perfil_empresarial_pre_checkout,
)
from app.services.conta_organizacional_rules import ESTADO_ATIVO, PAPEL_CONTRATANTE
from app.services.conta_organizacional_service import (
    CnpjMultiuserDuplicadoError,
    marcar_conta_multiuser_ativa,
)
from app.user_area import user_bp
from tests.conftest import seed_conta_franquia_cliente, seed_sistema_interno, seed_usuario

INICIO = datetime(2026, 9, 1, 12, 0, 0)
FIM = datetime(2026, 10, 1, 12, 0, 0)
PRICE_MU = "price_multiuser_f3"
PRODUCT_MU = "prod_multiuser_f3"
PRICE_STRIPE_OK = {
    "id": PRICE_MU,
    "object": "price",
    "active": True,
    "currency": "brl",
    "product": PRODUCT_MU,
    "type": "recurring",
    "billing_scheme": "per_unit",
    "recurring": {"interval": "month", "usage_type": "licensed"},
    "unit_amount": 4990,
    "unit_amount_decimal": "4990",
}
DADOS_EMPRESA_BASE = {
    "razao_social": "Logcompleta Agentes Inteligentes LTDA",
    "nome_fantasia": "AgenteFrete",
    "email_empresarial": "financeiro@example.com",
    "endereco_logradouro": "Rua Teste",
    "endereco_numero": "100",
    "endereco_cidade": "São Paulo",
    "endereco_uf": "SP",
    "endereco_cep": "01001000",
}


def _cnpj_from_base(base12: str) -> str:
    d1 = _digito_verificador(base12, (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2))
    d2 = _digito_verificador(base12 + str(d1), (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2))
    return base12 + str(d1) + str(d2)


CNPJ_A = _cnpj_from_base("112223330001")
CNPJ_B = _cnpj_from_base("445556660001")


def _dados_empresa(*, cnpj=CNPJ_A, **extra):
    out = dict(DADOS_EMPRESA_BASE)
    out["cnpj"] = cnpj
    out.update(extra)
    return out


def _unix_utc(dt: datetime) -> int:
    naive = dt.replace(tzinfo=None) if dt.tzinfo is not None else dt
    return int(naive.replace(tzinfo=timezone.utc).timestamp())


def _seed_gateway_multiuser(*, valor="49.90", minimo="5", price_id=PRICE_MU, pronto=True):
    if Conta.query.filter_by(slug=Conta.SLUG_SISTEMA).first() is None:
        seed_sistema_interno()
    plano_service.atualizar_parametros_plano_admin(
        plano_codigo="multiuser",
        valor_plano_raw=valor,
        franquia_limite_total_raw="1000",
        quantidade_minima_raw=minimo,
        gateway_provider_raw="stripe",
        gateway_product_id_raw="prod_multiuser_f3",
        gateway_price_id_raw=price_id,
        gateway_currency_raw="brl",
        gateway_interval_raw="month",
        gateway_pronto_raw=pronto,
    )


@pytest.fixture(autouse=True)
def _mock_stripe_price_multiuser(monkeypatch):
    from app.services import cleiton_monetizacao_service as monetizacao_service

    def _fake_get(path, params=None):  # noqa: ARG001
        path_s = str(path)
        if path_s.startswith("/prices/"):
            pid = path_s.rsplit("/", 1)[-1]
            if pid == PRICE_MU:
                return dict(PRICE_STRIPE_OK)
            return {"id": pid, "active": False, "currency": "brl", "product": "prod_x"}
        if path_s.startswith("/checkout/sessions/"):
            sid = path_s.rsplit("/", 1)[-1]
            return {
                "id": sid,
                "status": "open",
                "payment_status": "unpaid",
                "client_secret": "secret_reuso",
                "customer": "cus_existente",
                "expires_at": int(time.time()) + 3600,
                "line_items": {
                    "data": [
                        {"quantity": 5, "price": {"id": PRICE_MU}},
                    ]
                },
            }
        if path_s.startswith("/subscriptions/"):
            sid = path_s.rsplit("/", 1)[-1]
            return {
                "id": sid,
                "status": "active",
                "metadata": {"plano_interno": "multiuser", "correlation_id": ""},
                "items": {
                    "data": [
                        {
                            "id": "si_mu_1",
                            "quantity": 5,
                            "price": {"id": PRICE_MU},
                        }
                    ]
                },
            }
        raise AssertionError(f"GET Stripe inesperado: {path_s}")

    monkeypatch.setattr(monetizacao_service, "_stripe_get", _fake_get)


def _criar_intencao_pendente(
    conta,
    *,
    franquia_id=None,
    user_id=None,
    quantity=5,
    price_id=PRICE_MU,
    correlation_id=None,
    session_id=None,
    customer_id=None,
    subscription_id=None,
):
    from app.services.conta_multiuser_checkout_intencao_service import (
        chave_idempotencia_checkout_multiuser,
    )

    cid = correlation_id or uuid.uuid4().hex
    row = ContaMonetizacaoCheckoutIntencao(
        conta_id=conta.id,
        franquia_id=franquia_id,
        usuario_id=user_id,
        plano_interno="multiuser",
        estado=ContaMonetizacaoCheckoutIntencao.ESTADO_PENDENTE,
        correlation_id=cid,
        stripe_idempotency_key=chave_idempotencia_checkout_multiuser(cid),
        checkout_session_id=session_id,
        price_id=price_id,
        quantity_solicitada=int(quantity),
        customer_id=customer_id,
        subscription_id=subscription_id,
    )
    db.session.add(row)
    db.session.commit()
    return db.session.get(ContaMonetizacaoCheckoutIntencao, row.id)


def _login(client, user: User) -> None:
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def _build_user_area_client(app):
    app.config["SECRET_KEY"] = "test-secret-f3"
    app.config["TESTING"] = True
    if "user" not in app.blueprints:
        app.register_blueprint(user_bp)
    login_manager.init_app(app)

    @login_manager.user_loader
    def _load_user(user_id):  # noqa: ANN001
        return get_user_by_id(user_id)

    return app.test_client()


def _evento_invoice_paid(
    *,
    conta_id,
    franquia_id,
    user_id,
    invoice_id="in_mu_1",
    event_id="evt_mu_paid_1",
    quantity=5,
    price_id=PRICE_MU,
    customer="cus_mu_1",
    subscription="sub_mu_1",
    inicio=INICIO,
    fim=FIM,
    quantity_solicitada=None,
    correlation_id=None,
    extra_lines=None,
    pii=False,
):
    if correlation_id is None:
        pend = (
            ContaMonetizacaoCheckoutIntencao.query.filter_by(
                conta_id=conta_id,
                estado=ContaMonetizacaoCheckoutIntencao.ESTADO_PENDENTE,
            )
            .order_by(ContaMonetizacaoCheckoutIntencao.id.asc())
            .first()
        )
        if pend is not None:
            correlation_id = pend.correlation_id
    linha_ok = {
        "quantity": quantity,
        "type": "subscription",
        "proration": False,
        "subscription": subscription,
        "subscription_item": "si_mu_1",
        "period": {
            "start": _unix_utc(inicio),
            "end": _unix_utc(fim),
        },
        "price": {"id": price_id},
    }
    linhas = list(extra_lines or [])
    linhas.append(linha_ok)
    metadata = {
        "conta_id": str(conta_id),
        "franquia_id": str(franquia_id),
        "usuario_id": str(user_id),
        "plano_interno": "multiuser",
    }
    if correlation_id:
        metadata["correlation_id"] = correlation_id
        metadata["checkout_intent_id"] = correlation_id
    if quantity_solicitada is not None:
        metadata["quantity_solicitada"] = str(quantity_solicitada)
    objeto = {
        "id": invoice_id,
        "customer": customer,
        "subscription": subscription,
        "status": "paid",
        "metadata": metadata,
        "lines": {"data": linhas},
    }
    if pii:
        objeto["customer_email"] = "pessoa@example.com"
        objeto["billing_details"] = {"name": "Fulano", "address": {"line1": "Rua X"}}
        objeto["payment_method_details"] = {"card": {"last4": "4242"}}
        objeto["metadata"]["nome_pessoa"] = "Fulano de Tal"
    return {
        "id": event_id,
        "type": "invoice.paid",
        "created": _unix_utc(inicio),
        "data": {"object": objeto},
    }


def _preparar_conta_contratacao(slug="conta-f3"):
    conta, franquia = seed_conta_franquia_cliente(slug=slug)
    user = seed_usuario(franquia.id, conta.id, email=f"{slug}@test.com", categoria="free")
    persistir_perfil_empresarial_pre_checkout(conta.id, _dados_empresa(), commit=True)
    intencao = _criar_intencao_pendente(
        conta, franquia_id=franquia.id, user_id=user.id, quantity=5
    )
    return (
        db.session.get(Conta, conta.id),
        db.session.get(Franquia, franquia.id),
        db.session.get(User, user.id),
        intencao,
    )


# --- Configuração ---


def test_multiuser_entra_na_jornada_somente_com_gateway_valido(app):
    with app.app_context():
        seed_sistema_interno()
        plano_service.atualizar_parametros_plano_admin(
            plano_codigo="multiuser",
            valor_plano_raw="49.90",
            franquia_limite_total_raw="1000",
            quantidade_minima_raw="5",
        )
        publicos = listar_planos_contratacao_publica()
        assert all(p["codigo"] != "multiuser" for p in publicos)
        _seed_gateway_multiuser()
        publicos2 = listar_planos_contratacao_publica()
        mu = next(p for p in publicos2 if p["codigo"] == "multiuser")
        assert mu["habilitado_checkout"] is True
        assert mu["quantidade_minima"] == 5


def test_price_id_ausente_falha_fechado(app):
    with app.app_context():
        seed_sistema_interno()
        plano_service.atualizar_parametros_plano_admin(
            plano_codigo="multiuser",
            valor_plano_raw="49.90",
            franquia_limite_total_raw="1000",
            quantidade_minima_raw="5",
            gateway_provider_raw="stripe",
            gateway_product_id_raw="prod_x",
            gateway_currency_raw="brl",
            gateway_interval_raw="month",
            gateway_pronto_raw=True,
        )
        conta, franquia = seed_conta_franquia_cliente(slug="conta-sem-price")
        user = seed_usuario(franquia.id, conta.id, email="sem-price@test.com")
        with pytest.raises(ValueError, match="pendente|price"):
            iniciar_jornada_assinatura_stripe(
                user=user,
                plano_codigo="multiuser",
                quantity=5,
                dados_empresariais=_dados_empresa(),
            )


def test_preco_e_minimo_sem_hardcode_runtime():
    from app.services import conta_multiuser_contratacao_service as svc

    src = inspect.getsource(svc) + inspect.getsource(plano_service)
    assert "49.90" not in src
    assert "49,90" not in src
    assert "if quantity < 5" not in src
    assert "quantity = 1" not in inspect.getsource(iniciar_jornada_assinatura_stripe)


def test_resumo_mensal_calculado_no_backend(app):
    with app.app_context():
        _seed_gateway_multiuser(valor="49.90", minimo="5")
        resumo = calcular_resumo_contratacao_multiuser(8)
        assert resumo.quantidade == 8
        assert resumo.quantidade_minima == 5
        assert resumo.valor_unitario == Decimal("49.90")
        assert resumo.valor_mensal == Decimal("399.20")
        assert resumo.price_id == PRICE_MU
        assert resumo.currency == "brl"


def test_quantity_abaixo_do_minimo_configuravel(app):
    with app.app_context():
        _seed_gateway_multiuser(minimo="7")
        with pytest.raises(QuantityMultiuserInvalidaError):
            calcular_resumo_contratacao_multiuser(5)


# --- Formulário / perfil ---


def test_perfil_empresarial_nao_ativa_beneficio(app):
    with app.app_context():
        conta, _fr = seed_conta_franquia_cliente(slug="conta-perfil")
        persistir_perfil_empresarial_pre_checkout(conta.id, _dados_empresa(), commit=True)
        rec = db.session.get(Conta, conta.id)
        assert rec.razao_social.startswith("Logcompleta")
        assert rec.cnpj == CNPJ_A
        assert rec.multiuser_ativa is False
        assert rec.quantidade_assentos_contratados is None


def test_cnpj_invalido_bloqueia_pre_checkout(app):
    with app.app_context():
        conta, _fr = seed_conta_franquia_cliente(slug="conta-cnpj-inv")
        with pytest.raises(Exception):
            persistir_perfil_empresarial_pre_checkout(
                conta.id, _dados_empresa(cnpj="123"), commit=True
            )


def test_cnpj_duplicado_precheck(app):
    with app.app_context():
        a, _fa = seed_conta_franquia_cliente(slug="conta-cnpj-a")
        persistir_perfil_empresarial_pre_checkout(a.id, _dados_empresa(cnpj=CNPJ_A), commit=True)
        marcar_conta_multiuser_ativa(a.id, exigir_cnpj=True, commit=True)
        b, _fb = seed_conta_franquia_cliente(slug="conta-cnpj-b")
        with pytest.raises(CnpjMultiuserDuplicadoError):
            persistir_perfil_empresarial_pre_checkout(
                b.id, _dados_empresa(cnpj=CNPJ_A), commit=True
            )


def test_conta_id_alheio_no_payload_e_ignorado(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="conta-propria")
        user = seed_usuario(franquia.id, conta.id, email="proprio@test.com")
        outra, _fo = seed_conta_franquia_cliente(slug="conta-alheia")
        captured = {}

        def _fake_post(path, payload, idempotency_key=None):  # noqa: ARG001
            captured["payload"] = dict(payload)
            return {"id": "cs_test", "client_secret": "secret_test", "status": "open"}

        from app.services import cleiton_monetizacao_service as monetizacao_service

        monkeypatch.setattr(monetizacao_service, "_obter_publishable_key_stripe", lambda: "pk_test")
        monkeypatch.setattr(monetizacao_service, "_obter_assinatura_stripe_ativa", lambda conta_id: None)
        monkeypatch.setattr(monetizacao_service, "_stripe_post", _fake_post)
        client = _build_user_area_client(app)
        _login(client, user)
        from app.user_area import gerar_csrf_token_contratacao

        token = gerar_csrf_token_contratacao(user.id)
        resp = client.post(
            "/api/contratacao/stripe/iniciar",
            json={
                "plano_codigo": "multiuser",
                "csrf_token": token,
                "quantity": 5,
                "conta_id": outra.id,
                **_dados_empresa(),
            },
        )
        assert resp.status_code == 200
        rec = db.session.get(Conta, conta.id)
        assert rec.cnpj == CNPJ_A
        alheia = db.session.get(Conta, outra.id)
        assert alheia.cnpj is None
        assert captured["payload"]["metadata[conta_id]"] == str(conta.id)


# --- Checkout ---


def _iniciar_checkout_capturando(
    app, monkeypatch, *, quantity, user, dados=None, obter_assinatura=None, captured=None
):
    if captured is None:
        captured = {"posts": []}
    else:
        captured.setdefault("posts", [])

    def _fake_post(path, payload, idempotency_key=None):  # noqa: ARG001
        captured["path"] = path
        captured["payload"] = dict(payload)
        captured["idempotency_key"] = idempotency_key
        captured["posts"].append(
            {"path": path, "payload": dict(payload), "idempotency_key": idempotency_key}
        )
        return {
            "id": "cs_mu",
            "client_secret": "secret_mu",
            "status": "open",
            "customer": "cus_existente",
            "expires_at": int(time.time()) + 3600,
        }

    from app.services import cleiton_monetizacao_service as monetizacao_service

    obter = obter_assinatura if obter_assinatura is not None else (lambda conta_id: None)
    monkeypatch.setattr(monetizacao_service, "_obter_publishable_key_stripe", lambda: "pk_test")
    monkeypatch.setattr(monetizacao_service, "_obter_assinatura_stripe_ativa", obter)
    monkeypatch.setattr(monetizacao_service, "_stripe_post", _fake_post)
    out = iniciar_jornada_assinatura_stripe(
        user=user,
        plano_codigo="multiuser",
        quantity=quantity,
        dados_empresariais=dados or _dados_empresa(),
    )
    return out, captured


def test_checkout_quantity_5(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="chk-q5")
        user = seed_usuario(franquia.id, conta.id, email="q5@test.com")
        out, captured = _iniciar_checkout_capturando(app, monkeypatch, quantity=5, user=user)
        assert captured["path"] == "/checkout/sessions"
        assert captured["payload"]["mode"] == "subscription"
        assert captured["payload"]["line_items[0][price]"] == PRICE_MU
        assert captured["payload"]["line_items[0][quantity]"] == "5"
        assert "payment_method_types[0]" not in captured["payload"]
        assert out["quantity"] == 5


def test_checkout_quantity_8(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="chk-q8")
        user = seed_usuario(franquia.id, conta.id, email="q8@test.com")
        _out, captured = _iniciar_checkout_capturando(app, monkeypatch, quantity=8, user=user)
        assert captured["payload"]["line_items[0][quantity]"] == "8"


def test_checkout_reutiliza_customer(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="chk-cus")
        user = seed_usuario(franquia.id, conta.id, email="cus@test.com")
        vinculo = ContaMonetizacaoVinculo(
            conta_id=conta.id,
            provider="stripe",
            customer_id="cus_ja_existe",
            ativo=True,
        )
        db.session.add(vinculo)
        db.session.commit()
        _out, captured = _iniciar_checkout_capturando(app, monkeypatch, quantity=5, user=user)
        assert captured["payload"]["customer"] == "cus_ja_existe"


def test_checkout_nao_duplica_subscription(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="chk-dup")
        user = seed_usuario(franquia.id, conta.id, email="dup@test.com")
        from app.services import cleiton_monetizacao_service as monetizacao_service

        monkeypatch.setattr(monetizacao_service, "_obter_publishable_key_stripe", lambda: "pk_test")
        monkeypatch.setattr(
            monetizacao_service,
            "_obter_assinatura_stripe_ativa",
            lambda conta_id: {
                "subscription_id": "sub_ativa",
                "subscription_item_id": "si_ativa",
                "customer_id": "cus_ativa",
            },
        )
        with pytest.raises(ValueError, match="assinatura Stripe ativa"):
            iniciar_jornada_assinatura_stripe(
                user=user,
                plano_codigo="multiuser",
                quantity=5,
                dados_empresariais=_dados_empresa(),
            )


def test_pix_nao_e_forcado_no_checkout_subscription(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="chk-pix")
        user = seed_usuario(franquia.id, conta.id, email="pix@test.com")
        _out, captured = _iniciar_checkout_capturando(app, monkeypatch, quantity=5, user=user)
        joined = " ".join(f"{k}={v}" for k, v in captured["payload"].items()).lower()
        assert "pix" not in joined
        assert "payment_method_types" not in joined


# --- Gates financeiros ---


def test_checkout_session_completed_nao_libera(app):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia, user, _intencao = _preparar_conta_contratacao("gate-chk")
        resultado = processar_evento_stripe(
            {
                "id": "evt_chk_completed",
                "type": "checkout.session.completed",
                "created": int(INICIO.timestamp()),
                "data": {
                    "object": {
                        "id": "cs_1",
                        "customer": "cus_mu_1",
                        "subscription": "sub_mu_1",
                        "payment_status": "unpaid",
                        "metadata": {
                            "conta_id": str(conta.id),
                            "franquia_id": str(franquia.id),
                            "usuario_id": str(user.id),
                            "plano_interno": "multiuser",
                        },
                    }
                },
            }
        )
        rec = db.session.get(Conta, conta.id)
        assert rec.multiuser_ativa is False
        assert rec.quantidade_assentos_contratados is None
        assert resultado.get("efeito_operacional_aplicado") is False


def test_subscription_updated_nao_libera_multiuser(app):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia, user, _intencao = _preparar_conta_contratacao("gate-upd")
        resultado = processar_evento_stripe(
            {
                "id": "evt_sub_upd",
                "type": "customer.subscription.updated",
                "created": int(INICIO.timestamp()),
                "data": {
                    "object": {
                        "id": "sub_mu_1",
                        "customer": "cus_mu_1",
                        "status": "active",
                        "current_period_start": int(INICIO.timestamp()),
                        "current_period_end": int(FIM.timestamp()),
                        "metadata": {
                            "conta_id": str(conta.id),
                            "franquia_id": str(franquia.id),
                            "usuario_id": str(user.id),
                            "plano_interno": "multiuser",
                        },
                        "items": {
                            "data": [
                                {"quantity": 5, "price": {"id": PRICE_MU}}
                            ]
                        },
                    }
                },
            }
        )
        rec = db.session.get(Conta, conta.id)
        assert rec.multiuser_ativa is False
        assert rec.quantidade_assentos_contratados is None
        assert Franquia.query.filter_by(conta_id=conta.id).count() == 1
        assert resultado.get("efeito_operacional_aplicado") is False


def test_invoice_paid_inicial_ativa_multiuser(app):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia, user, _intencao = _preparar_conta_contratacao("paid-ini")
        resultado = processar_evento_stripe(
            _evento_invoice_paid(
                conta_id=conta.id,
                franquia_id=franquia.id,
                user_id=user.id,
                quantity=5,
            )
        )
        rec = db.session.get(Conta, conta.id)
        user_rec = db.session.get(User, user.id)
        assert resultado["efeito_operacional_aplicado"] is True
        assert rec.multiuser_ativa is True
        assert rec.quantidade_assentos_contratados == 5
        franquias = Franquia.query.filter_by(conta_id=conta.id).order_by(Franquia.id.asc()).all()
        assert len(franquias) == 5
        ativos = ContaVinculoOrganizacional.query.filter_by(
            conta_id=conta.id, estado=ESTADO_ATIVO
        ).all()
        assert len(ativos) == 1
        assert ativos[0].papel == PAPEL_CONTRATANTE
        assert ativos[0].user_id == user.id
        ocupadas = {int(v.franquia_id) for v in ativos}
        livres = [fr for fr in franquias if int(fr.id) not in ocupadas]
        assert len(livres) == 4
        assert all(fr.inicio_ciclo == INICIO for fr in franquias)
        assert all(fr.fim_ciclo == FIM for fr in franquias)
        assert all(fr.fim_ciclo > fr.inicio_ciclo for fr in franquias)
        assert all(Decimal(str(fr.consumo_acumulado or 0)) == Decimal("0") for fr in franquias)
        assert user_rec.categoria == "multiuser"


def test_quantity_stripe_prevalece_e_registra_divergencia(app):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia, user, _intencao = _preparar_conta_contratacao("qty-div")
        processar_evento_stripe(
            _evento_invoice_paid(
                conta_id=conta.id,
                franquia_id=franquia.id,
                user_id=user.id,
                quantity=5,
                quantity_solicitada=8,
            )
        )
        rec = db.session.get(Conta, conta.id)
        assert rec.quantidade_assentos_contratados == 5
        fatos = MonetizacaoFato.query.filter_by(
            tipo_fato="stripe_multiuser_quantity_divergente"
        ).all()
        assert fatos


def test_price_errado_nao_ativa(app):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia, user, _intencao = _preparar_conta_contratacao("price-bad")
        processar_evento_stripe(
            _evento_invoice_paid(
                conta_id=conta.id,
                franquia_id=franquia.id,
                user_id=user.id,
                price_id="price_starter_alheio",
            )
        )
        rec = db.session.get(Conta, conta.id)
        assert rec.multiuser_ativa is False
        assert rec.quantidade_assentos_contratados is None


def test_customer_subscription_guards(app):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia, user, _intencao = _preparar_conta_contratacao("guard-ids")
        vinculo = ContaMonetizacaoVinculo(
            conta_id=conta.id,
            provider="stripe",
            customer_id="cus_canonica",
            subscription_id="sub_canonica",
            price_id=PRICE_MU,
            plano_interno="multiuser",
            ativo=True,
        )
        db.session.add(vinculo)
        db.session.commit()
        resultado = processar_evento_stripe(
            _evento_invoice_paid(
                conta_id=conta.id,
                franquia_id=franquia.id,
                user_id=user.id,
                customer="cus_alheio",
                subscription="sub_alheio",
            )
        )
        rec = db.session.get(Conta, conta.id)
        assert resultado.get("vinculo_guardrail_bloqueado") is True
        assert rec.multiuser_ativa is False


def test_renovacao_fanout_reset_uma_vez(app):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia, user, _intencao = _preparar_conta_contratacao("renov")
        processar_evento_stripe(
            _evento_invoice_paid(
                conta_id=conta.id,
                franquia_id=franquia.id,
                user_id=user.id,
                quantity=5,
            )
        )
        franquias = Franquia.query.filter_by(conta_id=conta.id).order_by(Franquia.id.asc()).all()
        for i, fr in enumerate(franquias):
            fr.consumo_acumulado = Decimal(str((i + 1) * 10))
            db.session.add(fr)
        db.session.commit()
        inicio2 = INICIO + timedelta(days=31)
        fim2 = FIM + timedelta(days=31)
        processar_evento_stripe(
            _evento_invoice_paid(
                conta_id=conta.id,
                franquia_id=franquia.id,
                user_id=user.id,
                invoice_id="in_mu_2",
                event_id="evt_mu_paid_2",
                quantity=5,
                inicio=inicio2,
                fim=fim2,
            )
        )
        franquias2 = Franquia.query.filter_by(conta_id=conta.id).order_by(Franquia.id.asc()).all()
        assert all(fr.inicio_ciclo == inicio2 for fr in franquias2)
        assert all(fr.fim_ciclo == fim2 for fr in franquias2)
        assert all(Decimal(str(fr.consumo_acumulado or 0)) == Decimal("0") for fr in franquias2)
        rec = db.session.get(Conta, conta.id)
        assert rec.quantidade_assentos_contratados == 5
        assert (
            ContaVinculoOrganizacional.query.filter_by(
                conta_id=conta.id, estado=ESTADO_ATIVO
            ).count()
            == 1
        )


def test_replay_invoice_nao_reseta_de_novo(app):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia, user, _intencao = _preparar_conta_contratacao("replay")
        evento = _evento_invoice_paid(
            conta_id=conta.id, franquia_id=franquia.id, user_id=user.id
        )
        processar_evento_stripe(evento)
        fr0 = Franquia.query.filter_by(conta_id=conta.id).order_by(Franquia.id.asc()).first()
        fr0.consumo_acumulado = Decimal("42")
        db.session.add(fr0)
        db.session.commit()
        replay = processar_evento_stripe(evento)
        assert replay.get("replay") is True
        fr1 = db.session.get(Franquia, fr0.id)
        assert fr1.consumo_acumulado == Decimal("42")


def test_evento_atrasado_nao_regressa_ciclo(app):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia, user, _intencao = _preparar_conta_contratacao("atrasado")
        processar_evento_stripe(
            _evento_invoice_paid(
                conta_id=conta.id, franquia_id=franquia.id, user_id=user.id
            )
        )
        fr0 = Franquia.query.filter_by(conta_id=conta.id).order_by(Franquia.id.asc()).first()
        fr0.consumo_acumulado = Decimal("9")
        db.session.add(fr0)
        db.session.commit()
        antigo_ini = INICIO - timedelta(days=40)
        antigo_fim = FIM - timedelta(days=40)
        processar_evento_stripe(
            _evento_invoice_paid(
                conta_id=conta.id,
                franquia_id=franquia.id,
                user_id=user.id,
                invoice_id="in_old",
                event_id="evt_old",
                inicio=antigo_ini,
                fim=antigo_fim,
            )
        )
        fr1 = db.session.get(Franquia, fr0.id)
        assert fr1.inicio_ciclo == INICIO
        assert fr1.fim_ciclo == FIM
        assert fr1.consumo_acumulado == Decimal("9")


def test_payment_failed_nao_bloqueia_nem_reseta(app):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia, user, _intencao = _preparar_conta_contratacao("payfail")
        processar_evento_stripe(
            _evento_invoice_paid(
                conta_id=conta.id, franquia_id=franquia.id, user_id=user.id
            )
        )
        fr0 = Franquia.query.filter_by(conta_id=conta.id).order_by(Franquia.id.asc()).first()
        fr0.consumo_acumulado = Decimal("15")
        status_antes = fr0.status
        db.session.add(fr0)
        db.session.commit()
        processar_evento_stripe(
            {
                "id": "evt_fail",
                "type": "invoice.payment_failed",
                "created": int(INICIO.timestamp()),
                "data": {
                    "object": {
                        "id": "in_fail",
                        "customer": "cus_mu_1",
                        "subscription": "sub_mu_1",
                        "billing_reason": "subscription_cycle",
                        "metadata": {
                            "conta_id": str(conta.id),
                            "franquia_id": str(franquia.id),
                            "usuario_id": str(user.id),
                            "plano_interno": "multiuser",
                        },
                    }
                },
            }
        )
        fr1 = db.session.get(Franquia, fr0.id)
        rec = db.session.get(Conta, conta.id)
        assert fr1.consumo_acumulado == Decimal("15")
        assert rec.multiuser_ativa is True
        assert rec.quantidade_assentos_contratados == 5
        vinculo = ContaMonetizacaoVinculo.query.filter_by(conta_id=conta.id, ativo=True).first()
        assert vinculo is not None
        assert vinculo.status_contratual_externo == "payment_failed"


def test_stripe_confirma_falha_local_replay_converge(app):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia, user, _intencao = _preparar_conta_contratacao("replay-local")
        from app.services import conta_multiuser_contratacao_service as contratacao

        original = contratacao.ativar_beneficio_multiuser_confirmado
        chamada = {"n": 0}

        def _falha_primeira(**kwargs):
            chamada["n"] += 1
            if chamada["n"] == 1:
                raise RuntimeError("falha local simulada")
            return original(**kwargs)

        contratacao.ativar_beneficio_multiuser_confirmado = _falha_primeira
        try:
            from app.services import cleiton_monetizacao_service as monetizacao_service

            evento = _evento_invoice_paid(
                conta_id=conta.id, franquia_id=franquia.id, user_id=user.id
            )
            with pytest.raises(RuntimeError):
                monetizacao_service.processar_evento_stripe(evento)
            db.session.rollback()
            rec = db.session.get(Conta, conta.id)
            assert rec.multiuser_ativa is False
            monetizacao_service.processar_evento_stripe(evento)
            rec2 = db.session.get(Conta, conta.id)
            assert rec2.multiuser_ativa is True
            assert rec2.quantidade_assentos_contratados == 5
        finally:
            contratacao.ativar_beneficio_multiuser_confirmado = original


def test_ciclo_invalido_nao_propaga(app):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia, user, _intencao = _preparar_conta_contratacao("ciclo-inv")
        processar_evento_stripe(
            _evento_invoice_paid(
                conta_id=conta.id,
                franquia_id=franquia.id,
                user_id=user.id,
                inicio=FIM,
                fim=INICIO,
            )
        )
        rec = db.session.get(Conta, conta.id)
        assert rec.multiuser_ativa is False


def test_servico_f3_nao_importa_stripe():
    import app.services.conta_multiuser_contratacao_service as svc

    src = inspect.getsource(svc)
    assert "import stripe" not in src
    assert "from stripe" not in src


def test_checkout_retry_mesma_idempotency_key(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="chk-retry")
        user = seed_usuario(franquia.id, conta.id, email="retry@test.com")
        out1, cap1 = _iniciar_checkout_capturando(app, monkeypatch, quantity=5, user=user)
        out2, cap2 = _iniciar_checkout_capturando(app, monkeypatch, quantity=5, user=user)
        assert cap1["idempotency_key"].startswith("stripe_multiuser_checkout:")
        assert cap1["idempotency_key"] == f"stripe_multiuser_checkout:{out1['correlation_id']}"
        assert out1["correlation_id"] == out2["correlation_id"]
        assert out1["checkout_session_id"] == out2["checkout_session_id"]
        assert len(cap1["posts"]) == 1
        assert len(cap2["posts"]) == 0
        assert ContaMonetizacaoCheckoutIntencao.query.filter_by(
            conta_id=conta.id, estado="pendente"
        ).count() == 1


def test_checkout_concorrencia_uma_intencao(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="chk-conc")
        user = seed_usuario(franquia.id, conta.id, email="conc@test.com")
        _iniciar_checkout_capturando(app, monkeypatch, quantity=5, user=user)
        _iniciar_checkout_capturando(app, monkeypatch, quantity=5, user=user)
        pendentes = ContaMonetizacaoCheckoutIntencao.query.filter_by(
            conta_id=conta.id, estado="pendente"
        ).all()
        assert len(pendentes) == 1


def test_checkout_crash_retry_mesma_intencao(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="chk-crash")
        user = seed_usuario(franquia.id, conta.id, email="crash@test.com")
        intencao = _criar_intencao_pendente(
            conta, franquia_id=franquia.id, user_id=user.id, session_id=None
        )
        _out, captured = _iniciar_checkout_capturando(app, monkeypatch, quantity=5, user=user)
        assert captured["idempotency_key"] == intencao.stripe_idempotency_key
        rec = ContaMonetizacaoCheckoutIntencao.query.filter_by(conta_id=conta.id, estado="pendente").one()
        assert rec.id == intencao.id
        assert rec.checkout_session_id == "cs_mu"


def test_checkout_session_expirada_gera_nova_intencao(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="chk-exp")
        user = seed_usuario(franquia.id, conta.id, email="exp@test.com")
        antiga = _criar_intencao_pendente(
            conta, franquia_id=franquia.id, user_id=user.id, session_id="cs_old"
        )
        from app.services import cleiton_monetizacao_service as monetizacao_service

        def _get(path, params=None):  # noqa: ARG001
            if str(path).startswith("/prices/"):
                return dict(PRICE_STRIPE_OK)
            if str(path).endswith("cs_old"):
                return {"id": "cs_old", "status": "expired"}
            if str(path).startswith("/checkout/sessions/"):
                return {"id": "cs_new", "status": "open", "expires_at": int(time.time()) + 3600}
            raise AssertionError(path)

        monkeypatch.setattr(monetizacao_service, "_stripe_get", _get)
        _out, captured = _iniciar_checkout_capturando(app, monkeypatch, quantity=5, user=user)
        rec_old = db.session.get(ContaMonetizacaoCheckoutIntencao, antiga.id)
        assert rec_old.estado == "expirada"
        nova = ContaMonetizacaoCheckoutIntencao.query.filter_by(
            conta_id=conta.id, estado="pendente"
        ).one()
        assert nova.id != antiga.id
        assert captured["idempotency_key"] == nova.stripe_idempotency_key


def test_invoice_sem_intencao_nao_ativa(app):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="sem-int")
        user = seed_usuario(franquia.id, conta.id, email="sem-int@test.com")
        persistir_perfil_empresarial_pre_checkout(conta.id, _dados_empresa(), commit=True)
        processar_evento_stripe(
            _evento_invoice_paid(
                conta_id=conta.id,
                franquia_id=franquia.id,
                user_id=user.id,
                correlation_id="corr_inexistente",
            )
        )
        rec = db.session.get(Conta, conta.id)
        assert rec.multiuser_ativa is False


def test_invoice_intencao_outra_conta_nao_ativa(app):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia, user, _int = _preparar_conta_contratacao("int-a")
        outra, fr2 = seed_conta_franquia_cliente(slug="int-b")
        alheia = _criar_intencao_pendente(outra, franquia_id=fr2.id)
        processar_evento_stripe(
            _evento_invoice_paid(
                conta_id=conta.id,
                franquia_id=franquia.id,
                user_id=user.id,
                correlation_id=alheia.correlation_id,
            )
        )
        rec = db.session.get(Conta, conta.id)
        assert rec.multiuser_ativa is False


def test_invoice_intencao_consumida_nao_reativa(app):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia, user, intencao = _preparar_conta_contratacao("int-cons")
        processar_evento_stripe(
            _evento_invoice_paid(
                conta_id=conta.id, franquia_id=franquia.id, user_id=user.id
            )
        )
        assert db.session.get(Conta, conta.id).multiuser_ativa is True
        intencao = db.session.get(ContaMonetizacaoCheckoutIntencao, intencao.id)
        assert intencao.estado == "consumida"
        _criar_intencao_pendente(conta, franquia_id=franquia.id, user_id=user.id)
        # renovação não exige intenção; este evento novo de mesmo período não reseta
        fr0 = Franquia.query.filter_by(conta_id=conta.id).first()
        fr0.consumo_acumulado = Decimal("7")
        db.session.add(fr0)
        db.session.commit()
        processar_evento_stripe(
            _evento_invoice_paid(
                conta_id=conta.id,
                franquia_id=franquia.id,
                user_id=user.id,
                invoice_id="in_same_period",
                event_id="evt_same_period",
            )
        )
        fr1 = db.session.get(Franquia, fr0.id)
        assert fr1.consumo_acumulado == Decimal("7")


def test_invoice_fora_de_ordem_recupera_correlation_da_subscription(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia, user, intencao = _preparar_conta_contratacao("ooo")
        from app.services import cleiton_monetizacao_service as monetizacao_service

        def _get(path, params=None):  # noqa: ARG001
            if str(path).startswith("/prices/"):
                return dict(PRICE_STRIPE_OK)
            if str(path).startswith("/subscriptions/"):
                return {
                    "id": "sub_mu_1",
                    "metadata": {
                        "plano_interno": "multiuser",
                        "correlation_id": intencao.correlation_id,
                        "conta_id": str(conta.id),
                    },
                    "items": {
                        "data": [{"id": "si_mu_1", "quantity": 5, "price": {"id": PRICE_MU}}]
                    },
                }
            raise AssertionError(path)

        monkeypatch.setattr(monetizacao_service, "_stripe_get", _get)
        processar_evento_stripe(
            _evento_invoice_paid(
                conta_id=conta.id,
                franquia_id=franquia.id,
                user_id=user.id,
                correlation_id="",
            )
        )
        rec = db.session.get(Conta, conta.id)
        assert rec.multiuser_ativa is True


def test_workers_mesma_invoice_um_fanout(app):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia, user, _intencao = _preparar_conta_contratacao("wk")
        evento = _evento_invoice_paid(
            conta_id=conta.id, franquia_id=franquia.id, user_id=user.id
        )
        processar_evento_stripe(evento)
        franquias = Franquia.query.filter_by(conta_id=conta.id).all()
        for fr in franquias:
            fr.consumo_acumulado = Decimal("11")
            db.session.add(fr)
        db.session.commit()
        from app.services.conta_multiuser_contratacao_service import (
            ativar_beneficio_multiuser_confirmado,
        )

        r2 = ativar_beneficio_multiuser_confirmado(
            conta_id=conta.id,
            user_id=user.id,
            quantity_stripe=5,
            quantity_solicitada=5,
            inicio_ciclo=INICIO,
            fim_ciclo=FIM,
            price_id=PRICE_MU,
            invoice_id="in_mu_1",
            commit=False,
        )
        assert r2.replay is True
        for fr in Franquia.query.filter_by(conta_id=conta.id).all():
            assert Decimal(str(fr.consumo_acumulado or 0)) == Decimal("11")


def test_reserva_rollback_permite_retry(app):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia, user, _intencao = _preparar_conta_contratacao("res-rb")
        from app.services import conta_multiuser_contratacao_service as contratacao
        from app.services.conta_multiuser_ciclo_service import aplicar_fanout_ciclo_confirmado as real_fanout

        def _boom(*a, **k):
            raise RuntimeError("falha apos reserva")

        contratacao.aplicar_fanout_ciclo_confirmado = _boom
        try:
            with pytest.raises(RuntimeError):
                contratacao.ativar_beneficio_multiuser_confirmado(
                    conta_id=conta.id,
                    user_id=user.id,
                    quantity_stripe=5,
                    quantity_solicitada=5,
                    inicio_ciclo=INICIO,
                    fim_ciclo=FIM,
                    price_id=PRICE_MU,
                    invoice_id="in_rb",
                    intencao_id=_intencao.id,
                    commit=True,
                )
        except Exception:
            db.session.rollback()
        finally:
            contratacao.aplicar_fanout_ciclo_confirmado = real_fanout
        db.session.rollback()
        from app.services.conta_multiuser_contratacao_service import (
            chave_idempotencia_ciclo_multiuser,
        )

        assert (
            MonetizacaoFato.query.filter_by(
                idempotency_key=chave_idempotencia_ciclo_multiuser(conta.id, "in_rb")
            ).first()
            is None
        )
        resultado = contratacao.ativar_beneficio_multiuser_confirmado(
            conta_id=conta.id,
            user_id=user.id,
            quantity_stripe=5,
            quantity_solicitada=5,
            inicio_ciclo=INICIO,
            fim_ciclo=FIM,
            price_id=PRICE_MU,
            invoice_id="in_rb",
            intencao_id=_intencao.id,
            commit=True,
        )
        assert resultado.replay is False
        assert db.session.get(Conta, conta.id).multiuser_ativa is True


def test_linha_contratual_segunda_posicao(app):
    from app.services.conta_multiuser_invoice_linha_service import (
        selecionar_linha_contratual_multiuser,
    )

    ajuste = {
        "quantity": 99,
        "type": "invoiceitem",
        "proration": True,
        "price": {"id": PRICE_MU},
        "period": {"start": 1, "end": 2},
    }
    invoice = {
        "lines": {
            "data": [
                ajuste,
                {
                    "quantity": 8,
                    "type": "subscription",
                    "proration": False,
                    "subscription": "sub_mu_1",
                    "subscription_item": "si_mu_1",
                    "price": {"id": PRICE_MU},
                    "period": {"start": _unix_utc(INICIO), "end": _unix_utc(FIM)},
                },
            ]
        }
    }
    linha = selecionar_linha_contratual_multiuser(
        invoice, price_id_esperado=PRICE_MU, subscription_id_esperado="sub_mu_1"
    )
    assert linha.quantity == 8
    assert linha.inicio == INICIO
    assert linha.fim == FIM
    assert linha.price_id == PRICE_MU


def test_linha_price_errado_e_correto_escolhe_correto():
    from app.services.conta_multiuser_invoice_linha_service import (
        selecionar_linha_contratual_multiuser,
    )

    invoice = {
        "lines": {
            "data": [
                {
                    "quantity": 3,
                    "type": "subscription",
                    "price": {"id": "price_starter_alheio"},
                    "period": {"start": _unix_utc(INICIO), "end": _unix_utc(FIM)},
                },
                {
                    "quantity": 5,
                    "type": "subscription",
                    "price": {"id": PRICE_MU},
                    "subscription": "sub_x",
                    "period": {"start": _unix_utc(INICIO), "end": _unix_utc(FIM)},
                },
            ]
        }
    }
    linha = selecionar_linha_contratual_multiuser(invoice, price_id_esperado=PRICE_MU)
    assert linha.quantity == 5
    assert linha.price_id == PRICE_MU


def test_linha_ambigua_fail_closed():
    from app.services.conta_multiuser_invoice_linha_service import (
        LinhaContratualMultiuserAmbiguoError,
        selecionar_linha_contratual_multiuser,
    )

    invoice = {
        "lines": {
            "data": [
                {
                    "quantity": 5,
                    "type": "subscription",
                    "price": {"id": PRICE_MU},
                    "period": {"start": _unix_utc(INICIO), "end": _unix_utc(FIM)},
                },
                {
                    "quantity": 8,
                    "type": "subscription",
                    "price": {"id": PRICE_MU},
                    "period": {"start": _unix_utc(INICIO), "end": _unix_utc(FIM)},
                },
            ]
        }
    }
    with pytest.raises(LinhaContratualMultiuserAmbiguoError):
        selecionar_linha_contratual_multiuser(invoice, price_id_esperado=PRICE_MU)


def test_linha_ausente_fail_closed():
    from app.services.conta_multiuser_invoice_linha_service import (
        LinhaContratualMultiuserAusenteError,
        selecionar_linha_contratual_multiuser,
    )

    invoice = {"lines": {"data": [{"quantity": 1, "price": {"id": "price_outro"}}]}}
    with pytest.raises(LinhaContratualMultiuserAusenteError):
        selecionar_linha_contratual_multiuser(invoice, price_id_esperado=PRICE_MU)


def test_invoice_multiline_nao_ativa_com_ambiguidade(app):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia, user, _intencao = _preparar_conta_contratacao("ml-amb")
        extra = [
            {
                "quantity": 9,
                "type": "subscription",
                "proration": False,
                "subscription": "sub_mu_1",
                "subscription_item": "si_mu_1",
                "price": {"id": PRICE_MU},
                "period": {"start": _unix_utc(INICIO), "end": _unix_utc(FIM)},
            }
        ]
        processar_evento_stripe(
            _evento_invoice_paid(
                conta_id=conta.id,
                franquia_id=franquia.id,
                user_id=user.id,
                extra_lines=extra,
            )
        )
        rec = db.session.get(Conta, conta.id)
        assert rec.multiuser_ativa is False


def test_price_unit_amount_divergente_bloqueia_resumo(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        from app.services import cleiton_monetizacao_service as monetizacao_service
        from app.services.conta_multiuser_contratacao_service import (
            PriceStripeDivergenteError,
            calcular_resumo_contratacao_multiuser,
        )

        def _get(path, params=None):  # noqa: ARG001
            body = dict(PRICE_STRIPE_OK)
            body["unit_amount"] = 9999
            body["unit_amount_decimal"] = "9999"
            return body

        monkeypatch.setattr(monetizacao_service, "_stripe_get", _get)
        with pytest.raises(PriceStripeDivergenteError):
            calcular_resumo_contratacao_multiuser(5)


@pytest.mark.parametrize(
    "campo,valor",
    [
        ("currency", "usd"),
        ("product", "prod_outro"),
        ("active", False),
    ],
)
def test_price_campos_divergentes_bloqueiam(app, monkeypatch, campo, valor):
    with app.app_context():
        _seed_gateway_multiuser()
        from app.services import cleiton_monetizacao_service as monetizacao_service
        from app.services.conta_multiuser_contratacao_service import (
            PriceStripeDivergenteError,
            confrontar_price_stripe_multiuser_admin,
        )

        def _get(path, params=None):  # noqa: ARG001
            body = dict(PRICE_STRIPE_OK)
            if campo == "product":
                body["product"] = valor
            else:
                body[campo] = valor
            return body

        monkeypatch.setattr(monetizacao_service, "_stripe_get", _get)
        with pytest.raises(PriceStripeDivergenteError):
            confrontar_price_stripe_multiuser_admin()


def test_price_interval_divergente_bloqueia(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        from app.services import cleiton_monetizacao_service as monetizacao_service
        from app.services.conta_multiuser_contratacao_service import (
            PriceStripeDivergenteError,
            confrontar_price_stripe_multiuser_admin,
        )

        def _get(path, params=None):  # noqa: ARG001
            body = dict(PRICE_STRIPE_OK)
            body["recurring"] = {"interval": "year", "usage_type": "licensed"}
            return body

        monkeypatch.setattr(monetizacao_service, "_stripe_get", _get)
        with pytest.raises(PriceStripeDivergenteError):
            confrontar_price_stripe_multiuser_admin()


def test_price_inexistente_bloqueia(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        from app.services import cleiton_monetizacao_service as monetizacao_service
        from app.services.conta_multiuser_contratacao_service import (
            PriceStripeDivergenteError,
            confrontar_price_stripe_multiuser_admin,
        )

        def _get(path, params=None):  # noqa: ARG001
            raise ValueError("Stripe retornou erro HTTP 404")

        monkeypatch.setattr(monetizacao_service, "_stripe_get", _get)
        with pytest.raises(PriceStripeDivergenteError):
            confrontar_price_stripe_multiuser_admin()


def test_payload_pii_nao_persistido(app):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia, user, _intencao = _preparar_conta_contratacao("pii")
        processar_evento_stripe(
            _evento_invoice_paid(
                conta_id=conta.id,
                franquia_id=franquia.id,
                user_id=user.id,
                pii=True,
            )
        )
        fatos = MonetizacaoFato.query.filter_by(conta_id=conta.id).all()
        blob = " ".join(f.payload_bruto_sanitizado_json or "" for f in fatos).lower()
        assert "pessoa@example.com" not in blob
        assert "fulano" not in blob
        assert "4242" not in blob
        assert "billing_details" not in blob
        assert "rua x" not in blob
        assert "nome_pessoa" not in blob


def test_ciclo_canonico_vinculo_nao_franquia(app):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia, user, _intencao = _preparar_conta_contratacao("ciclo-can")
        processar_evento_stripe(
            _evento_invoice_paid(
                conta_id=conta.id, franquia_id=franquia.id, user_id=user.id
            )
        )
        vinculo = ContaMonetizacaoVinculo.query.filter_by(conta_id=conta.id, ativo=True).first()
        assert vinculo is not None
        vinculo.vigencia_externa_inicio = INICIO
        vinculo.vigencia_externa_fim = FIM
        db.session.add(vinculo)
        for fr in Franquia.query.filter_by(conta_id=conta.id).all():
            fr.inicio_ciclo = INICIO - timedelta(days=40)
            fr.fim_ciclo = FIM - timedelta(days=40)
            fr.consumo_acumulado = Decimal("3")
            db.session.add(fr)
        db.session.commit()
        processar_evento_stripe(
            _evento_invoice_paid(
                conta_id=conta.id,
                franquia_id=franquia.id,
                user_id=user.id,
                invoice_id="in_oct_again",
                event_id="evt_oct_again",
            )
        )
        fr1 = Franquia.query.filter_by(conta_id=conta.id).first()
        assert Decimal(str(fr1.consumo_acumulado or 0)) == Decimal("3")


def test_ciclo_canonico_novembro_processa(app):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia, user, _intencao = _preparar_conta_contratacao("ciclo-nov")
        processar_evento_stripe(
            _evento_invoice_paid(
                conta_id=conta.id, franquia_id=franquia.id, user_id=user.id
            )
        )
        for fr in Franquia.query.filter_by(conta_id=conta.id).all():
            fr.consumo_acumulado = Decimal("4")
            db.session.add(fr)
        db.session.commit()
        ini2 = INICIO + timedelta(days=31)
        fim2 = FIM + timedelta(days=31)
        processar_evento_stripe(
            _evento_invoice_paid(
                conta_id=conta.id,
                franquia_id=franquia.id,
                user_id=user.id,
                invoice_id="in_nov",
                event_id="evt_nov",
                inicio=ini2,
                fim=fim2,
            )
        )
        for fr in Franquia.query.filter_by(conta_id=conta.id).all():
            assert fr.inicio_ciclo == ini2
            assert Decimal(str(fr.consumo_acumulado or 0)) == Decimal("0")


def test_sanitizer_allowlist_unitario():
    from app.services.stripe_payload_sanitizer import sanitizar_payload_stripe

    bruto = {
        "id": "evt_x",
        "type": "invoice.paid",
        "data": {
            "object": {
                "id": "in_x",
                "customer": "cus_x",
                "customer_email": "a@b.com",
                "billing_details": {"name": "N", "address": {"city": "SP"}},
                "payment_method_details": {"card": {"last4": "1111"}},
                "metadata": {
                    "conta_id": "1",
                    "plano_interno": "multiuser",
                    "nome_pessoa": "N",
                    "email": "a@b.com",
                },
            }
        },
    }
    limpo = sanitizar_payload_stripe(bruto)
    texto = str(limpo).lower()
    assert "a@b.com" not in texto
    assert "last4" not in texto
    assert "nome_pessoa" not in texto
    assert limpo["data"]["object"]["id"] == "in_x"
    assert limpo["data"]["object"]["metadata"]["conta_id"] == "1"


def test_sanitizer_preserva_billing_reason_tecnico():
    from app.services.stripe_payload_sanitizer import sanitizar_payload_stripe

    bruto = {
        "id": "evt_fail",
        "type": "invoice.payment_failed",
        "data": {
            "object": {
                "id": "in_fail",
                "customer": "cus_x",
                "subscription": "sub_x",
                "billing_reason": "subscription_cycle",
                "customer_email": "a@b.com",
            }
        },
    }
    limpo = sanitizar_payload_stripe(bruto)
    assert limpo["data"]["object"]["billing_reason"] == "subscription_cycle"
    assert "a@b.com" not in str(limpo).lower()


def test_migration_checkout_intencao_upgrade_downgrade(tmp_path):
    import importlib.util

    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine, inspect

    db_path = tmp_path / "f3_intencao.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    mig_path = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "b3c4d5e6f7a8_fase3_checkout_intencao.py"
    )
    spec = importlib.util.spec_from_file_location("f3_intencao_mig", mig_path)
    mig = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mig)

    def _run(fn):
        with engine.connect() as conn:
            context = MigrationContext.configure(conn, opts={"render_as_batch": True})
            ops = Operations(context)
            fn.__globals__["op"] = ops
            fn()
            conn.commit()

    with engine.connect() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE conta (id INTEGER PRIMARY KEY, nome VARCHAR(80), slug VARCHAR(80))"
        )
        conn.exec_driver_sql(
            "CREATE TABLE franquia (id INTEGER PRIMARY KEY, conta_id INTEGER, nome VARCHAR(80), slug VARCHAR(80))"
        )
        conn.exec_driver_sql(
            "CREATE TABLE user (id INTEGER PRIMARY KEY, email VARCHAR(150))"
        )
        conn.commit()
    _run(mig.upgrade)
    insp = inspect(engine)
    assert "conta_monetizacao_checkout_intencao" in insp.get_table_names()
    _run(mig.downgrade)
    insp = inspect(engine)
    assert "conta_monetizacao_checkout_intencao" not in insp.get_table_names()
    _run(mig.upgrade)
    insp = inspect(engine)
    assert "conta_monetizacao_checkout_intencao" in insp.get_table_names()


def test_price_unit_amount_decimal_compativel(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        from app.services import cleiton_monetizacao_service as monetizacao_service
        from app.services.conta_multiuser_contratacao_service import (
            confrontar_price_stripe_multiuser_admin,
        )

        def _get(path, params=None):  # noqa: ARG001
            body = dict(PRICE_STRIPE_OK)
            body.pop("unit_amount", None)
            body["unit_amount_decimal"] = "4990"
            return body

        monkeypatch.setattr(monetizacao_service, "_stripe_get", _get)
        out = confrontar_price_stripe_multiuser_admin()
        assert out["unit_amount"] == 4990


def test_checkout_bloqueado_se_price_muda_depois_do_resumo(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        _conta, _franquia = seed_conta_franquia_cliente(slug="chk-toctou")
        user = seed_usuario(_franquia.id, _conta.id, email="toctou@test.com")
        from app.services import cleiton_monetizacao_service as monetizacao_service

        resumo = calcular_resumo_contratacao_multiuser(5)
        assert resumo.valor_unitario == Decimal("49.90")

        def _get(path, params=None):  # noqa: ARG001
            body = dict(PRICE_STRIPE_OK)
            body["unit_amount"] = 9999
            body["unit_amount_decimal"] = "9999"
            return body

        monkeypatch.setattr(monetizacao_service, "_stripe_get", _get)
        with pytest.raises(ValueError, match="diverge|bloqueada"):
            iniciar_jornada_assinatura_stripe(
                user=user,
                plano_codigo="multiuser",
                quantity=5,
                dados_empresariais=_dados_empresa(),
            )


def test_ciclo_invoice_setembro_ignorada(app):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia, user, _intencao = _preparar_conta_contratacao("ciclo-set")
        processar_evento_stripe(
            _evento_invoice_paid(
                conta_id=conta.id, franquia_id=franquia.id, user_id=user.id
            )
        )
        for fr in Franquia.query.filter_by(conta_id=conta.id).all():
            fr.consumo_acumulado = Decimal("6")
            db.session.add(fr)
        db.session.commit()
        processar_evento_stripe(
            _evento_invoice_paid(
                conta_id=conta.id,
                franquia_id=franquia.id,
                user_id=user.id,
                invoice_id="in_set",
                event_id="evt_set",
                inicio=INICIO - timedelta(days=31),
                fim=FIM - timedelta(days=31),
            )
        )
        for fr in Franquia.query.filter_by(conta_id=conta.id).all():
            assert fr.inicio_ciclo == INICIO
            assert Decimal(str(fr.consumo_acumulado or 0)) == Decimal("6")


def test_synthetic_invoice_path_exige_intencao(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="syn-no")
        user = seed_usuario(franquia.id, conta.id, email="syn-no@test.com")
        persistir_perfil_empresarial_pre_checkout(conta.id, _dados_empresa(), commit=True)
        from app.services import cleiton_monetizacao_service as monetizacao_service
        from app.services.cleiton_monetizacao_service import sincronizar_retorno_checkout_stripe

        def _get(path, params=None):  # noqa: ARG001
            path_s = str(path)
            if path_s.startswith("/prices/"):
                return dict(PRICE_STRIPE_OK)
            if path_s.startswith("/checkout/sessions/"):
                return {
                    "id": "cs_syn",
                    "status": "complete",
                    "payment_status": "paid",
                    "customer": "cus_mu_1",
                    "subscription": {
                        "id": "sub_mu_1",
                        "metadata": {
                            "plano_interno": "multiuser",
                            "conta_id": str(conta.id),
                            "correlation_id": "corr_inexistente",
                        },
                    },
                    "invoice": {
                        "id": "in_syn",
                        "status": "paid",
                        "customer": "cus_mu_1",
                        "subscription": "sub_mu_1",
                        "metadata": {
                            "conta_id": str(conta.id),
                            "franquia_id": str(franquia.id),
                            "usuario_id": str(user.id),
                            "plano_interno": "multiuser",
                            "correlation_id": "corr_inexistente",
                        },
                        "lines": {
                            "data": [
                                {
                                    "quantity": 5,
                                    "type": "subscription",
                                    "proration": False,
                                    "subscription": "sub_mu_1",
                                    "subscription_item": "si_mu_1",
                                    "price": {"id": PRICE_MU},
                                    "period": {
                                        "start": _unix_utc(INICIO),
                                        "end": _unix_utc(FIM),
                                    },
                                }
                            ]
                        },
                    },
                }
            if path_s.startswith("/subscriptions/"):
                return {
                    "id": "sub_mu_1",
                    "metadata": {
                        "plano_interno": "multiuser",
                        "correlation_id": "corr_inexistente",
                    },
                    "items": {
                        "data": [
                            {"id": "si_mu_1", "quantity": 5, "price": {"id": PRICE_MU}}
                        ]
                    },
                }
            raise AssertionError(path_s)

        monkeypatch.setattr(monetizacao_service, "_stripe_get", _get)
        sincronizar_retorno_checkout_stripe(checkout_session_id="cs_syn")
        rec = db.session.get(Conta, conta.id)
        assert rec.multiuser_ativa is False


def test_synthetic_invoice_path_com_intencao_valida(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia, user, intencao = _preparar_conta_contratacao("syn-ok")
        from app.services import cleiton_monetizacao_service as monetizacao_service
        from app.services.cleiton_monetizacao_service import sincronizar_retorno_checkout_stripe

        def _get(path, params=None):  # noqa: ARG001
            path_s = str(path)
            if path_s.startswith("/prices/"):
                return dict(PRICE_STRIPE_OK)
            if path_s.startswith("/checkout/sessions/"):
                return {
                    "id": "cs_syn_ok",
                    "status": "complete",
                    "payment_status": "paid",
                    "customer": "cus_mu_1",
                    "subscription": {
                        "id": "sub_mu_1",
                        "metadata": {
                            "plano_interno": "multiuser",
                            "conta_id": str(conta.id),
                            "correlation_id": intencao.correlation_id,
                            "checkout_intent_id": intencao.correlation_id,
                        },
                    },
                    "invoice": {
                        "id": "in_syn_ok",
                        "status": "paid",
                        "customer": "cus_mu_1",
                        "subscription": "sub_mu_1",
                        "metadata": {
                            "conta_id": str(conta.id),
                            "franquia_id": str(franquia.id),
                            "usuario_id": str(user.id),
                            "plano_interno": "multiuser",
                            "correlation_id": intencao.correlation_id,
                        },
                        "lines": {
                            "data": [
                                {
                                    "quantity": 5,
                                    "type": "subscription",
                                    "proration": False,
                                    "subscription": "sub_mu_1",
                                    "subscription_item": "si_mu_1",
                                    "price": {"id": PRICE_MU},
                                    "period": {
                                        "start": _unix_utc(INICIO),
                                        "end": _unix_utc(FIM),
                                    },
                                }
                            ]
                        },
                    },
                }
            if path_s.startswith("/subscriptions/"):
                return {
                    "id": "sub_mu_1",
                    "metadata": {
                        "plano_interno": "multiuser",
                        "correlation_id": intencao.correlation_id,
                    },
                    "items": {
                        "data": [
                            {"id": "si_mu_1", "quantity": 5, "price": {"id": PRICE_MU}}
                        ]
                    },
                }
            raise AssertionError(path_s)

        monkeypatch.setattr(monetizacao_service, "_stripe_get", _get)
        sincronizar_retorno_checkout_stripe(checkout_session_id="cs_syn_ok")
        rec = db.session.get(Conta, conta.id)
        assert rec.multiuser_ativa is True
        assert rec.quantidade_assentos_contratados == 5


def _get_prices_ok_session_raises(exc):
    def _get(path, params=None):  # noqa: ARG001
        path_s = str(path)
        if path_s.startswith("/prices/"):
            return dict(PRICE_STRIPE_OK)
        if path_s.startswith("/checkout/sessions/"):
            raise exc
        raise AssertionError(path_s)

    return _get


def _assert_unica_pendente(conta, intencao_id):
    pendentes = ContaMonetizacaoCheckoutIntencao.query.filter_by(
        conta_id=conta.id, estado="pendente"
    ).all()
    assert len(pendentes) == 1
    assert pendentes[0].id == intencao_id
    return pendentes[0]


def test_checkout_timeout_nao_expira_intencao(app, monkeypatch):
    from requests.exceptions import Timeout
    from app.services.conta_multiuser_checkout_intencao_service import (
        MSG_INTENCAO_INCONCLUSIVA,
    )

    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="chk-to")
        user = seed_usuario(franquia.id, conta.id, email="to@test.com")
        antiga = _criar_intencao_pendente(
            conta, franquia_id=franquia.id, user_id=user.id, session_id="cs_to"
        )
        from app.services import cleiton_monetizacao_service as monetizacao_service

        monkeypatch.setattr(
            monetizacao_service, "_stripe_get", _get_prices_ok_session_raises(Timeout("read timed out"))
        )
        with pytest.raises(ValueError, match="Não foi possível confirmar"):
            _iniciar_checkout_capturando(app, monkeypatch, quantity=5, user=user)
        rec = db.session.get(ContaMonetizacaoCheckoutIntencao, antiga.id)
        assert rec.estado == "pendente"
        _assert_unica_pendente(conta, antiga.id)
        assert MSG_INTENCAO_INCONCLUSIVA


def test_checkout_404_nao_expira_intencao(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="chk-404")
        user = seed_usuario(franquia.id, conta.id, email="n404@test.com")
        antiga = _criar_intencao_pendente(
            conta, franquia_id=franquia.id, user_id=user.id, session_id="cs_404"
        )
        from app.services import cleiton_monetizacao_service as monetizacao_service

        monkeypatch.setattr(
            monetizacao_service,
            "_stripe_get",
            _get_prices_ok_session_raises(ValueError("Stripe retornou erro HTTP 404: {}")),
        )
        with pytest.raises(ValueError, match="Não foi possível confirmar"):
            _iniciar_checkout_capturando(app, monkeypatch, quantity=5, user=user)
        rec = db.session.get(ContaMonetizacaoCheckoutIntencao, antiga.id)
        assert rec.estado == "pendente"
        _assert_unica_pendente(conta, antiga.id)


def test_checkout_5xx_nao_expira_intencao(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="chk-5xx")
        user = seed_usuario(franquia.id, conta.id, email="n5xx@test.com")
        antiga = _criar_intencao_pendente(
            conta, franquia_id=franquia.id, user_id=user.id, session_id="cs_5xx"
        )
        from app.services import cleiton_monetizacao_service as monetizacao_service

        monkeypatch.setattr(
            monetizacao_service,
            "_stripe_get",
            _get_prices_ok_session_raises(ValueError("Stripe retornou erro HTTP 500: {}")),
        )
        with pytest.raises(ValueError, match="Não foi possível confirmar"):
            _iniciar_checkout_capturando(app, monkeypatch, quantity=5, user=user)
        rec = db.session.get(ContaMonetizacaoCheckoutIntencao, antiga.id)
        assert rec.estado == "pendente"
        _assert_unica_pendente(conta, antiga.id)


def test_checkout_network_error_nao_expira_intencao(app, monkeypatch):
    from requests.exceptions import ConnectionError as RequestsConnectionError

    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="chk-net")
        user = seed_usuario(franquia.id, conta.id, email="net@test.com")
        antiga = _criar_intencao_pendente(
            conta, franquia_id=franquia.id, user_id=user.id, session_id="cs_net"
        )
        from app.services import cleiton_monetizacao_service as monetizacao_service

        monkeypatch.setattr(
            monetizacao_service,
            "_stripe_get",
            _get_prices_ok_session_raises(RequestsConnectionError("dns failed")),
        )
        with pytest.raises(ValueError, match="Não foi possível confirmar"):
            _iniciar_checkout_capturando(app, monkeypatch, quantity=5, user=user)
        rec = db.session.get(ContaMonetizacaoCheckoutIntencao, antiga.id)
        assert rec.estado == "pendente"
        _assert_unica_pendente(conta, antiga.id)


def test_checkout_status_expired_expira_intencao(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="chk-exp2")
        user = seed_usuario(franquia.id, conta.id, email="exp2@test.com")
        antiga = _criar_intencao_pendente(
            conta, franquia_id=franquia.id, user_id=user.id, session_id="cs_old2"
        )
        from app.services import cleiton_monetizacao_service as monetizacao_service

        def _get(path, params=None):  # noqa: ARG001
            if str(path).startswith("/prices/"):
                return dict(PRICE_STRIPE_OK)
            if str(path).endswith("cs_old2") or "cs_old2" in str(path):
                return {"id": "cs_old2", "status": "expired", "payment_status": "unpaid"}
            if str(path).startswith("/checkout/sessions/"):
                return {
                    "id": "cs_new2",
                    "status": "open",
                    "expires_at": int(time.time()) + 3600,
                    "line_items": {"data": [{"quantity": 5, "price": {"id": PRICE_MU}}]},
                }
            raise AssertionError(path)

        monkeypatch.setattr(monetizacao_service, "_stripe_get", _get)
        _iniciar_checkout_capturando(app, monkeypatch, quantity=5, user=user)
        rec_old = db.session.get(ContaMonetizacaoCheckoutIntencao, antiga.id)
        assert rec_old.estado == "expirada"
        nova = ContaMonetizacaoCheckoutIntencao.query.filter_by(
            conta_id=conta.id, estado="pendente"
        ).one()
        assert nova.id != antiga.id


def test_checkout_paid_nao_expira_intencao(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="chk-paid")
        user = seed_usuario(franquia.id, conta.id, email="paid@test.com")
        antiga = _criar_intencao_pendente(
            conta, franquia_id=franquia.id, user_id=user.id, session_id="cs_paid"
        )
        from app.services import cleiton_monetizacao_service as monetizacao_service

        def _get(path, params=None):  # noqa: ARG001
            if str(path).startswith("/prices/"):
                return dict(PRICE_STRIPE_OK)
            if str(path).startswith("/checkout/sessions/"):
                return {
                    "id": "cs_paid",
                    "status": "complete",
                    "payment_status": "paid",
                    "subscription": "sub_from_cs",
                }
            raise AssertionError(path)

        monkeypatch.setattr(monetizacao_service, "_stripe_get", _get)
        with pytest.raises(ValueError, match="em andamento"):
            _iniciar_checkout_capturando(app, monkeypatch, quantity=5, user=user)
        rec = db.session.get(ContaMonetizacaoCheckoutIntencao, antiga.id)
        assert rec.estado == "pendente"
        _assert_unica_pendente(conta, antiga.id)


def test_checkout_complete_com_subscription_nao_expira(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="chk-comp")
        user = seed_usuario(franquia.id, conta.id, email="comp@test.com")
        antiga = _criar_intencao_pendente(
            conta, franquia_id=franquia.id, user_id=user.id, session_id="cs_comp"
        )
        from app.services import cleiton_monetizacao_service as monetizacao_service

        def _get(path, params=None):  # noqa: ARG001
            if str(path).startswith("/prices/"):
                return dict(PRICE_STRIPE_OK)
            if str(path).startswith("/checkout/sessions/"):
                return {
                    "id": "cs_comp",
                    "status": "complete",
                    "payment_status": "unpaid",
                    "subscription": {"id": "sub_comp"},
                }
            raise AssertionError(path)

        monkeypatch.setattr(monetizacao_service, "_stripe_get", _get)
        with pytest.raises(ValueError, match="em andamento"):
            _iniciar_checkout_capturando(app, monkeypatch, quantity=5, user=user)
        rec = db.session.get(ContaMonetizacaoCheckoutIntencao, antiga.id)
        assert rec.estado == "pendente"
        _assert_unica_pendente(conta, antiga.id)


def test_checkout_get_falha_subscription_existente_bloqueia_nova(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="chk-subloc")
        user = seed_usuario(franquia.id, conta.id, email="subloc@test.com")
        antiga = _criar_intencao_pendente(
            conta,
            franquia_id=franquia.id,
            user_id=user.id,
            session_id="cs_subloc",
            subscription_id="sub_ja_existe",
        )
        from app.services import cleiton_monetizacao_service as monetizacao_service

        monkeypatch.setattr(
            monetizacao_service,
            "_stripe_get",
            _get_prices_ok_session_raises(ValueError("Stripe retornou erro HTTP 404: {}")),
        )
        with pytest.raises(ValueError, match="em andamento"):
            _iniciar_checkout_capturando(app, monkeypatch, quantity=5, user=user)
        rec = db.session.get(ContaMonetizacaoCheckoutIntencao, antiga.id)
        assert rec.estado == "pendente"
        _assert_unica_pendente(conta, antiga.id)


def test_checkout_get_falha_subscription_aparece_depois_nao_cria_nova(app, monkeypatch):
    from requests.exceptions import Timeout

    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="chk-later")
        user = seed_usuario(franquia.id, conta.id, email="later@test.com")
        antiga = _criar_intencao_pendente(
            conta, franquia_id=franquia.id, user_id=user.id, session_id="cs_later"
        )
        from app.services import cleiton_monetizacao_service as monetizacao_service

        monkeypatch.setattr(
            monetizacao_service, "_stripe_get", _get_prices_ok_session_raises(Timeout("timeout"))
        )
        with pytest.raises(ValueError, match="Não foi possível confirmar"):
            _iniciar_checkout_capturando(app, monkeypatch, quantity=5, user=user)
        _assert_unica_pendente(conta, antiga.id)
        antiga = db.session.get(ContaMonetizacaoCheckoutIntencao, antiga.id)
        antiga.subscription_id = "sub_apareceu"
        db.session.add(antiga)
        db.session.commit()
        with pytest.raises(ValueError, match="em andamento"):
            _iniciar_checkout_capturando(app, monkeypatch, quantity=8, user=user)
        _assert_unica_pendente(conta, antiga.id)


def test_retry_quantity_diferente_bloqueia(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="chk-qtd")
        user = seed_usuario(franquia.id, conta.id, email="qtd@test.com")
        _iniciar_checkout_capturando(app, monkeypatch, quantity=5, user=user)
        with pytest.raises(ValueError, match="parâmetros diferentes"):
            _iniciar_checkout_capturando(app, monkeypatch, quantity=8, user=user)
        pendentes = ContaMonetizacaoCheckoutIntencao.query.filter_by(
            conta_id=conta.id, estado="pendente"
        ).all()
        assert len(pendentes) == 1
        assert pendentes[0].quantity_solicitada == 5


def test_retry_price_diferente_bloqueia(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="chk-prid")
        user = seed_usuario(franquia.id, conta.id, email="prid@test.com")
        antiga = _criar_intencao_pendente(
            conta,
            franquia_id=franquia.id,
            user_id=user.id,
            session_id="cs_prid",
            price_id="price_antigo_p1",
        )
        with pytest.raises(ValueError, match="parâmetros diferentes"):
            _iniciar_checkout_capturando(app, monkeypatch, quantity=5, user=user)
        rec = db.session.get(ContaMonetizacaoCheckoutIntencao, antiga.id)
        assert rec.estado == "pendente"
        assert rec.price_id == "price_antigo_p1"
        _assert_unica_pendente(conta, antiga.id)


def test_retry_user_diferente_bloqueia(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="chk-usr")
        user_a = seed_usuario(franquia.id, conta.id, email="usr-a@test.com")
        user_b = seed_usuario(franquia.id, conta.id, email="usr-b@test.com")
        antiga = _criar_intencao_pendente(
            conta, franquia_id=franquia.id, user_id=user_a.id, session_id="cs_usr"
        )
        with pytest.raises(ValueError, match="parâmetros diferentes"):
            _iniciar_checkout_capturando(app, monkeypatch, quantity=5, user=user_b)
        rec = db.session.get(ContaMonetizacaoCheckoutIntencao, antiga.id)
        assert rec.estado == "pendente"
        _assert_unica_pendente(conta, antiga.id)


def test_retry_franquia_diferente_bloqueia(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="chk-fr")
        fr2 = Franquia(
            conta_id=conta.id,
            nome="Franquia 2",
            slug=f"fr2-{uuid.uuid4().hex[:8]}",
            status=Franquia.STATUS_ACTIVE,
        )
        db.session.add(fr2)
        db.session.commit()
        user_a = seed_usuario(franquia.id, conta.id, email="fr-a@test.com")
        user_b = seed_usuario(fr2.id, conta.id, email="fr-b@test.com")
        antiga = _criar_intencao_pendente(
            conta, franquia_id=franquia.id, user_id=user_a.id, session_id="cs_fr"
        )
        with pytest.raises(ValueError, match="parâmetros diferentes"):
            _iniciar_checkout_capturando(app, monkeypatch, quantity=5, user=user_b)
        rec = db.session.get(ContaMonetizacaoCheckoutIntencao, antiga.id)
        assert rec.estado == "pendente"
        _assert_unica_pendente(conta, antiga.id)


def test_retry_customer_diferente_bloqueia(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="chk-cusd")
        user = seed_usuario(franquia.id, conta.id, email="cusd@test.com")
        antiga = _criar_intencao_pendente(
            conta,
            franquia_id=franquia.id,
            user_id=user.id,
            session_id="cs_cusd",
            customer_id="cus_antigo",
        )
        vinculo = ContaMonetizacaoVinculo(
            conta_id=conta.id,
            provider="stripe",
            customer_id="cus_novo",
            ativo=True,
        )
        db.session.add(vinculo)
        db.session.commit()
        with pytest.raises(ValueError, match="parâmetros diferentes"):
            _iniciar_checkout_capturando(app, monkeypatch, quantity=5, user=user)
        rec = db.session.get(ContaMonetizacaoCheckoutIntencao, antiga.id)
        assert rec.estado == "pendente"
        _assert_unica_pendente(conta, antiga.id)


def test_session_reutilizada_price_incompativel_bloqueia(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="chk-sprice")
        user = seed_usuario(franquia.id, conta.id, email="sprice@test.com")
        antiga = _criar_intencao_pendente(
            conta, franquia_id=franquia.id, user_id=user.id, session_id="cs_sprice"
        )
        from app.services import cleiton_monetizacao_service as monetizacao_service

        def _get(path, params=None):  # noqa: ARG001
            if str(path).startswith("/prices/"):
                return dict(PRICE_STRIPE_OK)
            if str(path).startswith("/checkout/sessions/"):
                return {
                    "id": "cs_sprice",
                    "status": "open",
                    "payment_status": "unpaid",
                    "client_secret": "secret_old",
                    "line_items": {
                        "data": [{"quantity": 5, "price": {"id": "price_antigo_ext"}}]
                    },
                }
            raise AssertionError(path)

        monkeypatch.setattr(monetizacao_service, "_stripe_get", _get)
        with pytest.raises(ValueError, match="parâmetros diferentes"):
            _iniciar_checkout_capturando(app, monkeypatch, quantity=5, user=user)
        rec = db.session.get(ContaMonetizacaoCheckoutIntencao, antiga.id)
        assert rec.estado == "pendente"
        _assert_unica_pendente(conta, antiga.id)


def test_session_reutilizada_quantity_incompativel_bloqueia(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="chk-sqty")
        user = seed_usuario(franquia.id, conta.id, email="sqty@test.com")
        antiga = _criar_intencao_pendente(
            conta, franquia_id=franquia.id, user_id=user.id, session_id="cs_sqty", quantity=5
        )
        from app.services import cleiton_monetizacao_service as monetizacao_service

        def _get(path, params=None):  # noqa: ARG001
            if str(path).startswith("/prices/"):
                return dict(PRICE_STRIPE_OK)
            if str(path).startswith("/checkout/sessions/"):
                return {
                    "id": "cs_sqty",
                    "status": "open",
                    "payment_status": "unpaid",
                    "client_secret": "secret_old",
                    "line_items": {
                        "data": [{"quantity": 8, "price": {"id": PRICE_MU}}]
                    },
                }
            raise AssertionError(path)

        monkeypatch.setattr(monetizacao_service, "_stripe_get", _get)
        with pytest.raises(ValueError, match="parâmetros diferentes"):
            _iniciar_checkout_capturando(app, monkeypatch, quantity=5, user=user)
        rec = db.session.get(ContaMonetizacaoCheckoutIntencao, antiga.id)
        assert rec.estado == "pendente"
        _assert_unica_pendente(conta, antiga.id)


def test_session_reutilizada_compativel_reutiliza(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="chk-sok")
        user = seed_usuario(franquia.id, conta.id, email="sok@test.com")
        out1, cap1 = _iniciar_checkout_capturando(app, monkeypatch, quantity=5, user=user)
        out2, cap2 = _iniciar_checkout_capturando(app, monkeypatch, quantity=5, user=user)
        assert out1["checkout_session_id"] == out2["checkout_session_id"]
        assert len(cap1["posts"]) == 1
        assert len(cap2["posts"]) == 0
        assert ContaMonetizacaoCheckoutIntencao.query.filter_by(
            conta_id=conta.id, estado="pendente"
        ).count() == 1


def test_price_admin_muda_com_intencao_pendente_bloqueia(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="chk-adm")
        user = seed_usuario(franquia.id, conta.id, email="adm@test.com")
        antiga = _criar_intencao_pendente(
            conta, franquia_id=franquia.id, user_id=user.id, session_id="cs_adm"
        )
        price_p2 = "price_multiuser_p2"
        _seed_gateway_multiuser(price_id=price_p2)
        from app.services import cleiton_monetizacao_service as monetizacao_service
        from app.services.conta_multiuser_contratacao_service import (
            calcular_resumo_contratacao_multiuser,
        )

        def _get(path, params=None):  # noqa: ARG001
            path_s = str(path)
            if path_s.startswith("/prices/"):
                body = dict(PRICE_STRIPE_OK)
                body["id"] = price_p2
                return body
            if path_s.startswith("/checkout/sessions/"):
                return {
                    "id": "cs_adm",
                    "status": "open",
                    "payment_status": "unpaid",
                    "client_secret": "secret_a",
                    "line_items": {
                        "data": [{"quantity": 5, "price": {"id": PRICE_MU}}]
                    },
                }
            raise AssertionError(path_s)

        monkeypatch.setattr(monetizacao_service, "_stripe_get", _get)
        resumo = calcular_resumo_contratacao_multiuser(5)
        assert resumo.price_id == price_p2
        with pytest.raises(ValueError, match="parâmetros diferentes"):
            _iniciar_checkout_capturando(app, monkeypatch, quantity=5, user=user)
        rec = db.session.get(ContaMonetizacaoCheckoutIntencao, antiga.id)
        assert rec.estado == "pendente"
        assert rec.price_id == PRICE_MU
        _assert_unica_pendente(conta, antiga.id)
        assert ContaMonetizacaoCheckoutIntencao.query.filter_by(
            conta_id=conta.id, estado="pendente"
        ).one().checkout_session_id == "cs_adm"


def test_preco_admin_muda_mesmo_price_id_bloqueia(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="chk-amt")
        user = seed_usuario(franquia.id, conta.id, email="amt@test.com")
        antiga = _criar_intencao_pendente(
            conta, franquia_id=franquia.id, user_id=user.id, session_id="cs_amt"
        )
        from app.services import cleiton_monetizacao_service as monetizacao_service

        def _get(path, params=None):  # noqa: ARG001
            if str(path).startswith("/prices/"):
                body = dict(PRICE_STRIPE_OK)
                body["unit_amount"] = 9999
                body["unit_amount_decimal"] = "9999"
                return body
            if str(path).startswith("/checkout/sessions/"):
                return {
                    "id": "cs_amt",
                    "status": "open",
                    "line_items": {"data": [{"quantity": 5, "price": {"id": PRICE_MU}}]},
                }
            raise AssertionError(path)

        monkeypatch.setattr(monetizacao_service, "_stripe_get", _get)
        with pytest.raises(ValueError, match="diverge|bloqueada"):
            _iniciar_checkout_capturando(app, monkeypatch, quantity=5, user=user)
        rec = db.session.get(ContaMonetizacaoCheckoutIntencao, antiga.id)
        assert rec.estado == "pendente"
        _assert_unica_pendente(conta, antiga.id)


def _get_prices_ok_session_body(session_id, **session_extra):
    def _get(path, params=None):  # noqa: ARG001
        path_s = str(path)
        if path_s.startswith("/prices/"):
            return dict(PRICE_STRIPE_OK)
        if path_s.startswith("/checkout/sessions/"):
            body = {
                "id": session_id,
                "status": "expired",
                "payment_status": "unpaid",
            }
            body.update(session_extra)
            return body
        raise AssertionError(path_s)

    return _get


def _get_expired_session_e_subscriptions(
    session_id, *, subs_exc=None, subs_data=None
):
    def _get(path, params=None):  # noqa: ARG001
        path_s = str(path)
        if path_s.startswith("/prices/"):
            return dict(PRICE_STRIPE_OK)
        if path_s.startswith("/checkout/sessions/"):
            return {
                "id": session_id,
                "status": "expired",
                "payment_status": "unpaid",
            }
        if path_s.rstrip("/") == "/subscriptions":
            if subs_exc is not None:
                raise subs_exc
            return {"object": "list", "data": list(subs_data or [])}
        raise AssertionError(path_s)

    return _get


def _get_prices_ok_session_open(session_id, *, customer=None, include_customer=True):
    def _get(path, params=None):  # noqa: ARG001
        path_s = str(path)
        if path_s.startswith("/prices/"):
            return dict(PRICE_STRIPE_OK)
        if path_s.startswith("/checkout/sessions/"):
            body = {
                "id": session_id,
                "status": "open",
                "payment_status": "unpaid",
                "client_secret": "secret_reuso",
                "expires_at": int(time.time()) + 3600,
                "line_items": {
                    "data": [{"quantity": 5, "price": {"id": PRICE_MU}}],
                },
            }
            if include_customer:
                body["customer"] = customer
            return body
        raise AssertionError(path_s)

    return _get


def _assert_expired_consulta_subscription_falha_nao_expira(
    app, monkeypatch, *, slug, email, session_id, obter_exc
):
    from app.services import cleiton_monetizacao_service as monetizacao_service

    _seed_gateway_multiuser()
    conta, franquia = seed_conta_franquia_cliente(slug=slug)
    user = seed_usuario(franquia.id, conta.id, email=email)
    antiga = _criar_intencao_pendente(
        conta,
        franquia_id=franquia.id,
        user_id=user.id,
        session_id=session_id,
        customer_id="cus_c05",
    )
    monkeypatch.setattr(
        monetizacao_service,
        "_stripe_get",
        _get_expired_session_e_subscriptions(session_id, subs_exc=obter_exc),
    )

    cap = {"posts": []}
    with pytest.raises(ValueError, match="Não foi possível confirmar"):
        _iniciar_checkout_capturando(
            app,
            monkeypatch,
            quantity=5,
            user=user,
            captured=cap,
        )
    rec = db.session.get(ContaMonetizacaoCheckoutIntencao, antiga.id)
    assert rec.estado == "pendente"
    _assert_unica_pendente(conta, antiga.id)
    assert cap["posts"] == []
    return rec


def test_checkout_expired_consulta_subscription_falha_nao_expira(app, monkeypatch):
    from requests.exceptions import Timeout

    with app.app_context():
        _assert_expired_consulta_subscription_falha_nao_expira(
            app,
            monkeypatch,
            slug="chk-c05-falha",
            email="c05-falha@test.com",
            session_id="cs_c05_falha",
            obter_exc=Timeout("read timed out"),
        )


def test_checkout_expired_subscription_timeout_nao_expira(app, monkeypatch):
    from requests.exceptions import Timeout

    with app.app_context():
        _assert_expired_consulta_subscription_falha_nao_expira(
            app,
            monkeypatch,
            slug="chk-c05-to",
            email="c05-to@test.com",
            session_id="cs_c05_to",
            obter_exc=Timeout("subscription list timed out"),
        )


def test_checkout_expired_subscription_5xx_nao_expira(app, monkeypatch):
    with app.app_context():
        _assert_expired_consulta_subscription_falha_nao_expira(
            app,
            monkeypatch,
            slug="chk-c05-5xx",
            email="c05-5xx@test.com",
            session_id="cs_c05_5xx",
            obter_exc=ValueError("Stripe retornou erro HTTP 500: {}"),
        )


def test_checkout_expired_subscription_network_nao_expira(app, monkeypatch):
    from requests.exceptions import ConnectionError as RequestsConnectionError

    with app.app_context():
        _assert_expired_consulta_subscription_falha_nao_expira(
            app,
            monkeypatch,
            slug="chk-c05-net",
            email="c05-net@test.com",
            session_id="cs_c05_net",
            obter_exc=RequestsConnectionError("dns failed"),
        )


def test_checkout_expired_subscription_ausencia_conclusiva_expira(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="chk-c05-aus")
        user = seed_usuario(franquia.id, conta.id, email="c05-aus@test.com")
        antiga = _criar_intencao_pendente(
            conta,
            franquia_id=franquia.id,
            user_id=user.id,
            session_id="cs_c05_aus",
            customer_id="cus_c05_aus",
        )
        from app.services import cleiton_monetizacao_service as monetizacao_service

        monkeypatch.setattr(
            monetizacao_service,
            "_stripe_get",
            _get_expired_session_e_subscriptions("cs_c05_aus", subs_data=[]),
        )
        cap = {"posts": []}
        _iniciar_checkout_capturando(
            app, monkeypatch, quantity=5, user=user, captured=cap
        )
        rec_old = db.session.get(ContaMonetizacaoCheckoutIntencao, antiga.id)
        assert rec_old.estado == "expirada"
        nova = ContaMonetizacaoCheckoutIntencao.query.filter_by(
            conta_id=conta.id, estado="pendente"
        ).one()
        assert nova.id != antiga.id
        assert len(cap["posts"]) == 1
        assert cap["posts"][0]["path"] == "/checkout/sessions"


def test_checkout_expired_subscription_existente_nao_expira(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="chk-c05-ex")
        user = seed_usuario(franquia.id, conta.id, email="c05-ex@test.com")
        antiga = _criar_intencao_pendente(
            conta,
            franquia_id=franquia.id,
            user_id=user.id,
            session_id="cs_c05_ex",
            customer_id="cus_c05_ex",
        )
        from app.services import cleiton_monetizacao_service as monetizacao_service

        monkeypatch.setattr(
            monetizacao_service,
            "_stripe_get",
            _get_expired_session_e_subscriptions(
                "cs_c05_ex",
                subs_data=[{"id": "sub_derivada_c05", "status": "active"}],
            ),
        )
        cap = {"posts": []}
        with pytest.raises(ValueError, match="em andamento"):
            _iniciar_checkout_capturando(
                app,
                monkeypatch,
                quantity=5,
                user=user,
                captured=cap,
            )
        rec = db.session.get(ContaMonetizacaoCheckoutIntencao, antiga.id)
        assert rec.estado == "pendente"
        _assert_unica_pendente(conta, antiga.id)
        assert cap["posts"] == []


def test_request_sem_customer_intent_a_session_b_bloqueia(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="chk-a04-ab")
        user = seed_usuario(franquia.id, conta.id, email="a04-ab@test.com")
        antiga = _criar_intencao_pendente(
            conta,
            franquia_id=franquia.id,
            user_id=user.id,
            session_id="cs_a04_ab",
            customer_id="cus_A",
        )
        from app.services import cleiton_monetizacao_service as monetizacao_service

        monkeypatch.setattr(
            monetizacao_service,
            "_stripe_get",
            _get_prices_ok_session_open("cs_a04_ab", customer="cus_B"),
        )
        cap = {"posts": []}
        with pytest.raises(ValueError, match="parâmetros diferentes"):
            _iniciar_checkout_capturando(
                app, monkeypatch, quantity=5, user=user, captured=cap
            )
        rec = db.session.get(ContaMonetizacaoCheckoutIntencao, antiga.id)
        assert rec.estado == "pendente"
        assert rec.customer_id == "cus_A"
        _assert_unica_pendente(conta, antiga.id)
        assert cap["posts"] == []


def test_request_sem_customer_intent_a_session_none_bloqueia(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="chk-a04-an")
        user = seed_usuario(franquia.id, conta.id, email="a04-an@test.com")
        antiga = _criar_intencao_pendente(
            conta,
            franquia_id=franquia.id,
            user_id=user.id,
            session_id="cs_a04_an",
            customer_id="cus_A",
        )
        from app.services import cleiton_monetizacao_service as monetizacao_service

        monkeypatch.setattr(
            monetizacao_service,
            "_stripe_get",
            _get_prices_ok_session_open("cs_a04_an", include_customer=False),
        )
        cap = {"posts": []}
        with pytest.raises(ValueError, match="parâmetros diferentes"):
            _iniciar_checkout_capturando(
                app, monkeypatch, quantity=5, user=user, captured=cap
            )
        rec = db.session.get(ContaMonetizacaoCheckoutIntencao, antiga.id)
        assert rec.estado == "pendente"
        assert rec.customer_id == "cus_A"
        _assert_unica_pendente(conta, antiga.id)
        assert cap["posts"] == []


def test_request_sem_customer_intent_none_session_x_associa(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="chk-a04-nx")
        user = seed_usuario(franquia.id, conta.id, email="a04-nx@test.com")
        antiga = _criar_intencao_pendente(
            conta,
            franquia_id=franquia.id,
            user_id=user.id,
            session_id="cs_a04_nx",
            customer_id=None,
        )
        from app.services import cleiton_monetizacao_service as monetizacao_service

        monkeypatch.setattr(
            monetizacao_service,
            "_stripe_get",
            _get_prices_ok_session_open("cs_a04_nx", customer="cus_X"),
        )
        cap = {"posts": []}
        out, cap = _iniciar_checkout_capturando(
            app, monkeypatch, quantity=5, user=user, captured=cap
        )
        rec = db.session.get(ContaMonetizacaoCheckoutIntencao, antiga.id)
        assert rec.estado == "pendente"
        assert rec.customer_id == "cus_X"
        assert rec.id == antiga.id
        assert out["checkout_session_id"] == "cs_a04_nx"
        _assert_unica_pendente(conta, antiga.id)
        assert cap["posts"] == []

        cap2 = {"posts": []}
        out2, cap2 = _iniciar_checkout_capturando(
            app, monkeypatch, quantity=5, user=user, captured=cap2
        )
        rec2 = db.session.get(ContaMonetizacaoCheckoutIntencao, antiga.id)
        assert rec2.customer_id == "cus_X"
        assert rec2.id == antiga.id
        assert out2["checkout_session_id"] == "cs_a04_nx"
        _assert_unica_pendente(conta, antiga.id)
        assert cap2["posts"] == []


def test_customer_associado_retry_divergente_bloqueia(app, monkeypatch):
    with app.app_context():
        _seed_gateway_multiuser()
        conta, franquia = seed_conta_franquia_cliente(slug="chk-a04-rt")
        user = seed_usuario(franquia.id, conta.id, email="a04-rt@test.com")
        antiga = _criar_intencao_pendente(
            conta,
            franquia_id=franquia.id,
            user_id=user.id,
            session_id="cs_a04_rt",
            customer_id=None,
        )
        from app.services import cleiton_monetizacao_service as monetizacao_service

        monkeypatch.setattr(
            monetizacao_service,
            "_stripe_get",
            _get_prices_ok_session_open("cs_a04_rt", customer="cus_X"),
        )
        _iniciar_checkout_capturando(app, monkeypatch, quantity=5, user=user)
        rec = db.session.get(ContaMonetizacaoCheckoutIntencao, antiga.id)
        assert rec.customer_id == "cus_X"

        monkeypatch.setattr(
            monetizacao_service,
            "_stripe_get",
            _get_prices_ok_session_open("cs_a04_rt", customer="cus_Y"),
        )
        cap = {"posts": []}
        with pytest.raises(ValueError, match="parâmetros diferentes"):
            _iniciar_checkout_capturando(
                app, monkeypatch, quantity=5, user=user, captured=cap
            )
        rec2 = db.session.get(ContaMonetizacaoCheckoutIntencao, antiga.id)
        assert rec2.estado == "pendente"
        assert rec2.customer_id == "cus_X"
        _assert_unica_pendente(conta, antiga.id)
        assert cap["posts"] == []
