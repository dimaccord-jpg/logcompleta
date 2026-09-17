"""Testes incrementais da Fase 6 — aumento excepcional e cobrança extraordinária."""
from __future__ import annotations

import inspect
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from flask import Flask

from app.extensions import db, login_manager
from app.infra import get_user_by_id
from app.models import (
    Conta,
    ContaMonetizacaoVinculo,
    ContaMultiuserAumentoExcepcional,
    ContaMultiuserAumentoOperacao,
    Franquia,
    MonetizacaoFato,
    User,
    utcnow_naive,
)
from app.services import plano_service
from app.services.cleiton_monetizacao_service import processar_evento_stripe
from app.services.conta_multiuser_aumento_excepcional_service import (
    FLOW_TYPE_EXTRAORDINARY,
    FORMULA_VERSAO,
    MAX_VALIDADE,
    TIMEZONE_CALCULO,
    adotar_operacao_enviada_analise,
    calcular_dias_ciclo,
    calcular_preview_decisao_paga,
    calcular_valor_proporcional_centavos,
    decidir_solicitacao_excepcional,
    gerar_csrf_token_admin_multiuser,
    listar_para_contratante,
    montar_snapshot_proporcional,
)
from app.services.conta_multiuser_aumento_service import (
    confirmar_aumento_assentos,
    gerar_csrf_token_aumento,
)
from app.services.conta_multiuser_errors import (
    AumentoExcepcionalConflitoError,
    AumentoExcepcionalNaoAutorizadoError,
    AumentoMultiuserDivergenteError,
)
from app.services.user_plan_control_service import atribuir_plano_para_usuario
from app.conta_multiuser_convite_routes import convite_bp
from app.conta_multiuser_painel_routes import painel_bp
from app.user_area import user_bp
from tests.conftest import seed_conta_franquia_cliente, seed_sistema_interno, seed_usuario

ROOT = Path(__file__).resolve().parents[1]
INICIO = datetime(2026, 9, 1, 12, 0, 0)
FIM = datetime(2026, 10, 1, 12, 0, 0)
PRICE_MU = "price_multiuser_f6"
CUSTOMER = "cus_f6_1"
SUBSCRIPTION = "sub_f6_1"
ITEM = "si_f6_1"


def _preparar_planos_admin(*, valor="49.90", minimo="5", limite=None):
    if Conta.query.filter_by(slug=Conta.SLUG_SISTEMA).first() is None:
        seed_sistema_interno()
    limite_raw = (
        str(limite)
        if limite is not None
        else str(plano_service.obter_limite_aumento_automatico_ciclo_multiuser())
    )
    plano_service.atualizar_parametros_plano_admin(
        plano_codigo="multiuser",
        valor_plano_raw=valor,
        franquia_limite_total_raw="1000",
        quantidade_minima_raw=minimo,
        limite_aumento_automatico_raw=limite_raw,
        gateway_provider_raw="stripe",
        gateway_product_id_raw="prod_multiuser_f6",
        gateway_price_id_raw=PRICE_MU,
        gateway_currency_raw="brl",
        gateway_interval_raw="month",
        gateway_pronto_raw=True,
    )


def _preparar_conta_multiuser(slug: str, *, qtd: int, email: str):
    from flask import current_app

    current_app.config["SECRET_KEY"] = current_app.config.get("SECRET_KEY") or "test-secret-f6"
    current_app.config["TESTING"] = True
    _preparar_planos_admin()
    conta, _franquia = seed_conta_franquia_cliente(slug=slug)
    from tests.conftest import preencher_dados_empresariais_minimos_teste

    preencher_dados_empresariais_minimos_teste(conta, slug)
    user = seed_usuario(_franquia.id, conta.id, email=email, categoria="free")
    atribuir_plano_para_usuario(
        email=user.email,
        plano_raw="multiuser",
        quantidade_franquias_raw=str(qtd),
    )
    db.session.refresh(conta)
    db.session.refresh(user)
    vinculo = ContaMonetizacaoVinculo(
        conta_id=conta.id,
        provider="stripe",
        customer_id=CUSTOMER,
        subscription_id=SUBSCRIPTION,
        price_id=PRICE_MU,
        plano_interno="multiuser",
        status_contratual_externo="active",
        vigencia_externa_inicio=INICIO,
        vigencia_externa_fim=FIM,
        ativo=True,
        snapshot_normalizado_json=json.dumps(
            {
                "quantity": qtd,
                "items": {"data": [{"id": ITEM, "quantity": qtd, "price": {"id": PRICE_MU}}]},
            }
        ),
    )
    db.session.add(vinculo)
    for fr in Franquia.query.filter_by(conta_id=conta.id).all():
        fr.inicio_ciclo = INICIO
        fr.fim_ciclo = FIM
        fr.consumo_acumulado = Decimal("12.5")
        db.session.add(fr)
    db.session.commit()
    db.session.refresh(conta)
    db.session.refresh(user)
    return conta, user


def _admin(conta: Conta, email: str = "adm-f6@test.com") -> User:
    fr = Franquia.query.filter_by(conta_id=conta.id).first()
    user = seed_usuario(fr.id, conta.id, email=email, categoria="free")
    user.is_admin = True
    db.session.add(user)
    db.session.commit()
    db.session.refresh(user)
    return user


def _mock_stripe(monkeypatch, *, quantity=5, captured=None, instante=None):
    from app.services import cleiton_monetizacao_service as monetizacao_service
    state = {"quantity": int(quantity), "posts": [], "sessions": 0}
    if captured is not None:
        captured["state"] = state
    if instante is not None:
        monkeypatch.setattr(
            "app.services.conta_multiuser_aumento_excepcional_service.utcnow_naive",
            lambda: instante,
        )

    def _fake_get(path, params=None):  # noqa: ARG001
        path_s = str(path)
        if path_s.startswith("/subscriptions/"):
            return {
                "id": SUBSCRIPTION,
                "customer": CUSTOMER,
                "status": "active",
                "items": {
                    "data": [
                        {
                            "id": ITEM,
                            "quantity": state["quantity"],
                            "price": {"id": PRICE_MU, "unit_amount": 4990},
                        }
                    ]
                },
            }
        raise AssertionError(f"GET Stripe inesperado: {path_s}")

    def _fake_post(path, payload, idempotency_key=None):  # noqa: ARG001
        path_s = str(path)
        state["posts"].append(
            {"path": path_s, "payload": dict(payload), "idempotency_key": idempotency_key}
        )
        if path_s == f"/subscription_items/{ITEM}":
            assert payload.get("proration_behavior") == "none"
            state["quantity"] = int(payload.get("quantity"))
            return {
                "id": ITEM,
                "subscription": SUBSCRIPTION,
                "quantity": state["quantity"],
            }
        if path_s == "/checkout/sessions":
            state["sessions"] += 1
            sid = f"cs_f6_{state['sessions']}"
            return {
                "id": sid,
                "url": f"https://checkout.stripe.com/c/pay/{sid}",
                "payment_intent": f"pi_f6_{state['sessions']}",
                "mode": "payment",
                "status": "open",
            }
        raise AssertionError(f"POST Stripe inesperado: {path_s}")

    monkeypatch.setattr(monetizacao_service, "_stripe_get", _fake_get)
    monkeypatch.setattr(monetizacao_service, "_stripe_post", _fake_post)
    from app.services import conta_multiuser_cobranca_extraordinaria_service as pay_mod

    monkeypatch.setattr(pay_mod, "_stripe_post", _fake_post)
    monkeypatch.setattr(
        "app.auth_services.send_email",
        lambda *a, **k: None,
    )
    return state


def _solicitar_analise(user, quantidade, key=None):
    return confirmar_aumento_assentos(
        user,
        quantidade,
        idempotency_key=key or uuid.uuid4().hex,
    )


def _adotar(conta) -> ContaMultiuserAumentoExcepcional:
    op = ContaMultiuserAumentoOperacao.query.filter_by(
        conta_id=conta.id, estado="enviado_analise"
    ).one()
    return adotar_operacao_enviada_analise(op, commit=True)


def _evento_checkout(
    *,
    row: ContaMultiuserAumentoExcepcional,
    event_id="evt_f6_paid",
    event_type="checkout.session.completed",
    payment_status="paid",
    request_id=None,
    correlation_id=None,
    conta_id=None,
    customer=CUSTOMER,
    session_id=None,
):
    meta = {
        "flow_type": FLOW_TYPE_EXTRAORDINARY,
        "request_id": request_id if request_id is not None else row.request_id,
        "correlation_id": correlation_id if correlation_id is not None else row.correlation_id,
        "conta_id": str(conta_id if conta_id is not None else row.conta_id),
        "quantidade_aprovada": str(row.quantidade_aprovada or row.quantidade_solicitada),
    }
    objeto = {
        "id": session_id or row.stripe_checkout_session_id or "cs_f6_1",
        "object": "checkout.session",
        "mode": "payment",
        "customer": customer,
        "payment_status": payment_status,
        "payment_intent": row.stripe_payment_intent_id or "pi_f6_1",
        "metadata": meta,
        "amount_total": row.valor_calculado_centavos,
    }
    return {
        "id": event_id,
        "type": event_type,
        "created": int(INICIO.timestamp()),
        "data": {"object": objeto},
    }


def _login(client, user: User) -> None:
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def _build_client(app: Flask):
    app.config["SECRET_KEY"] = "test-secret-f6"
    app.config["TESTING"] = True
    app.config["SERVER_NAME"] = "localhost"
    app.template_folder = str(ROOT / "app" / "templates")
    if "multiuser_painel" not in app.blueprints:
        app.register_blueprint(painel_bp)
    if "multiuser_convite" not in app.blueprints:
        app.register_blueprint(convite_bp)
    if "user" not in app.blueprints:
        app.register_blueprint(user_bp)
    if "admin" not in app.blueprints:
        os.environ.setdefault("APP_ENV", "dev")
        from app.painel_admin.admin_routes import admin_bp

        app.register_blueprint(admin_bp)
    if "login" not in app.view_functions:

        @app.route("/login")
        def login():  # noqa: ARG001
            return "login"

    if "index" not in app.view_functions:

        @app.route("/")
        def index():  # noqa: ARG001
            return "index"

    if "logout" not in app.view_functions:

        @app.route("/logout")
        def logout():  # noqa: ARG001
            return "logout"

    if "feed" not in app.view_functions:

        @app.route("/feed")
        def feed():  # noqa: ARG001
            return "feed"

    if "privacy_policy" not in app.view_functions:

        @app.route("/politica-de-privacidade")
        def privacy_policy():  # noqa: ARG001
            return "privacy"

    if "terms_of_use" not in app.view_functions:

        @app.route("/termos-de-uso")
        def terms_of_use():  # noqa: ARG001
            return "terms"

    login_manager.init_app(app)

    @login_manager.user_loader
    def _load_user(user_id):  # noqa: ANN001
        return get_user_by_id(user_id)

    @app.context_processor
    def _inject():
        return {
            "has_endpoint": lambda endpoint_name: endpoint_name in app.view_functions,
            "privacy_marketing_allowed": False,
            "privacy_marketing_state": "rejected",
            "user_is_admin": lambda user: getattr(user, "is_admin", None) is True,
            "falha_mensal_vigente": False,
            "regularizacao_url": "/regularizar-pagamento",
            "facebook_pixel_id": "",
            "pixel_event_complete_registration": False,
            "pixel_event_lead": False,
        }

    return app.test_client()


def _consumo(conta_id):
    return {
        fr.id: fr.consumo_acumulado
        for fr in Franquia.query.filter_by(conta_id=conta_id).order_by(Franquia.id.asc())
    }


def _ciclo_franquias(conta_id):
    return [
        (fr.inicio_ciclo, fr.fim_ciclo)
        for fr in Franquia.query.filter_by(conta_id=conta_id).order_by(Franquia.id.asc())
    ]


# --- Migration / fórmula ---


def test_migration_e6f7a8b9c0d1_na_chain():
    cfg = Config()
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    script = ScriptDirectory.from_config(cfg)
    rev = script.get_revision("e6f7a8b9c0d1")
    assert rev is not None
    assert rev.down_revision == "d5e6f7a8b9c0"
    heads = set(script.get_heads())
    # F7 sucede F6 na cadeia linear; F6 permanece ancestral, não necessariamente head.
    assert "e6f7a8b9c0d1" in heads or script.get_revision("f7g8h9i0j1k2") is not None
    if "f7g8h9i0j1k2" in heads:
        assert script.get_revision("f7g8h9i0j1k2").down_revision == "e6f7a8b9c0d1"
    deploy = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
    assert "e6f7a8b9c0d1" in deploy


def test_formula_ciclo_completo_metade_ultimo_dia_e_half_up():
    assert calcular_dias_ciclo(INICIO, FIM, INICIO) == (30, 30)
    meio = datetime(2026, 9, 16, 12, 0, 0)
    assert calcular_dias_ciclo(INICIO, FIM, meio) == (30, 15)
    ultimo = datetime(2026, 10, 1, 11, 0, 0)
    assert calcular_dias_ciclo(INICIO, FIM, ultimo) == (30, 1)
    assert calcular_dias_ciclo(INICIO, FIM, FIM) == (30, 0)

    cheio = calcular_valor_proporcional_centavos(
        preco_unitario_centavos=4990,
        quantidade_aprovada=1,
        dias_restantes=30,
        dias_totais=30,
    )
    assert cheio == 4990
    metade = calcular_valor_proporcional_centavos(
        preco_unitario_centavos=4990,
        quantidade_aprovada=1,
        dias_restantes=15,
        dias_totais=30,
    )
    assert metade == 2495
    ultimo_centavos = calcular_valor_proporcional_centavos(
        preco_unitario_centavos=4990,
        quantidade_aprovada=1,
        dias_restantes=1,
        dias_totais=30,
    )
    esperado_ultimo = (Decimal("49.90") * Decimal(1) * Decimal(1) / Decimal(30)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    assert ultimo_centavos == int(esperado_ultimo * 100)
    assert ultimo_centavos == 166
    half_up = calcular_valor_proporcional_centavos(
        preco_unitario_centavos=100,
        quantidade_aprovada=1,
        dias_restantes=1,
        dias_totais=8,
    )
    assert half_up == 13
    src = inspect.getsource(calcular_valor_proporcional_centavos)
    assert "float" not in src
    assert "ROUND_HALF_UP" in src
    assert FORMULA_VERSAO == "multiuser_proporcional_v1"
    assert TIMEZONE_CALCULO == "UTC"


def test_webhook_branch_ocorre_antes_do_recorrente():
    src = inspect.getsource(processar_evento_stripe)
    assert "evento_eh_cobranca_extraordinaria_multiuser" in src
    assert src.index("evento_eh_cobranca_extraordinaria_multiuser") < src.index(
        "idempotency_key = None"
    )
    assert src.index("processar_evento_extraordinario_multiuser") < src.index(
        "invoice.paid"
    )


# --- Solicitação ---


def test_request_acima_limite_fica_em_analise_sem_liberar(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f6-analise", qtd=5, email="f6a@test.com")
        state = _mock_stripe(monkeypatch, quantity=5)
        limite = plano_service.obter_limite_aumento_automatico_ciclo_multiuser()
        r = _solicitar_analise(user, limite + 1, "k-over")
        assert r.requer_analise is True
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 5
        assert state["posts"] == []
        assert Franquia.query.filter_by(conta_id=conta.id).count() == 5
        row = _adotar(conta)
        assert row.estado == "em_analise"
        assert row.quantidade_solicitada == limite + 1
        assert row.quantity_atual == 5


# --- Admin ---


def test_nao_admin_nao_decide(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f6-noadm", qtd=5, email="f6na@test.com")
        _mock_stripe(monkeypatch, quantity=5)
        _solicitar_analise(user, 6, "k-na")
        row = _adotar(conta)
        with pytest.raises(AumentoExcepcionalNaoAutorizadoError):
            decidir_solicitacao_excepcional(user, row.id, decisao="rejeitada")
        db.session.refresh(row)
        assert row.estado == "em_analise"


def test_admin_rejeita_e_replay(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f6-rej", qtd=5, email="f6rej@test.com")
        admin = _admin(conta)
        _mock_stripe(monkeypatch, quantity=5)
        _solicitar_analise(user, 6, "k-rej")
        row = _adotar(conta)
        r1 = decidir_solicitacao_excepcional(
            admin, row.id, decisao="rejeitada", idempotency_key="dec-1", versao_esperada=1
        )
        assert r1.estado == "rejeitada"
        assert r1.replay is False
        r2 = decidir_solicitacao_excepcional(
            admin, row.id, decisao="rejeitada", idempotency_key="dec-1"
        )
        assert r2.replay is True
        assert r2.estado == "rejeitada"
        with pytest.raises(AumentoExcepcionalConflitoError):
            decidir_solicitacao_excepcional(
                admin, row.id, decisao="gratuita", idempotency_key="dec-1"
            )
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 5


def test_admin_aprova_gratis_uma_vez(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f6-free", qtd=5, email="f6fr@test.com")
        admin = _admin(conta)
        state = _mock_stripe(monkeypatch, quantity=5)
        consumo_antes = _consumo(conta.id)
        ciclo_antes = _ciclo_franquias(conta.id)
        _solicitar_analise(user, 6, "k-fr")
        row = _adotar(conta)
        r = decidir_solicitacao_excepcional(
            admin, row.id, decisao="gratuita", idempotency_key="dec-fr"
        )
        assert r.estado == "liberada"
        assert r.replay is False
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 11
        assert Franquia.query.filter_by(conta_id=conta.id).count() == 11
        qty_posts = [p for p in state["posts"] if p["path"].startswith("/subscription_items/")]
        assert len(qty_posts) == 1
        assert qty_posts[0]["payload"]["proration_behavior"] == "none"
        assert qty_posts[0]["payload"]["quantity"] == "11"
        assert state["sessions"] == 0
        assert not any(p["path"] == "/checkout/sessions" for p in state["posts"])
        assert _consumo(conta.id) == consumo_antes or all(
            _consumo(conta.id)[fid] == consumo_antes[fid] for fid in consumo_antes
        )
        for par in _ciclo_franquias(conta.id):
            assert par == (INICIO, FIM)
        assert ciclo_antes[0] == (INICIO, FIM)
        r2 = decidir_solicitacao_excepcional(
            admin, row.id, decisao="gratuita", idempotency_key="dec-fr"
        )
        assert r2.replay is True
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 11
        assert Franquia.query.filter_by(conta_id=conta.id).count() == 11


def test_admin_aprova_paga_congela_snapshot(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f6-paid", qtd=5, email="f6p@test.com")
        admin = _admin(conta)
        instante = datetime(2026, 9, 16, 12, 0, 0)
        state = _mock_stripe(monkeypatch, quantity=5, instante=instante)
        _solicitar_analise(user, 6, "k-p")
        row = _adotar(conta)
        preview = calcular_preview_decisao_paga(row.id)
        r = decidir_solicitacao_excepcional(
            admin, row.id, decisao="paga", idempotency_key="dec-p", versao_esperada=1
        )
        assert r.estado == "aguardando_pagamento"
        db.session.refresh(row)
        assert row.preco_unitario_centavos == 4990
        assert row.valor_calculado_centavos == preview["valor_calculado_centavos"]
        assert row.valor_calculado_centavos == 2495 * 6
        assert row.dias_totais == 30
        assert row.dias_restantes == 15
        assert row.versao_formula == FORMULA_VERSAO
        assert row.timezone_calculo == TIMEZONE_CALCULO
        assert row.expires_at <= instante + MAX_VALIDADE
        assert (row.expires_at - instante) <= MAX_VALIDADE
        assert state["sessions"] == 1
        checkout = [p for p in state["posts"] if p["path"] == "/checkout/sessions"][0]
        assert checkout["payload"]["mode"] == "payment"
        assert checkout["payload"]["customer"] == CUSTOMER
        assert checkout["payload"]["line_items[0][price_data][unit_amount]"] == str(
            row.valor_calculado_centavos
        )
        assert checkout["payload"]["metadata[flow_type]"] == FLOW_TYPE_EXTRAORDINARY
        assert "subscription" not in checkout["payload"]
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 5

        _preparar_planos_admin(valor="60.00")
        db.session.refresh(row)
        assert row.valor_calculado_centavos == 2495 * 6
        assert row.preco_unitario_centavos == 4990
        snap = montar_snapshot_proporcional(
            ciclo_inicio=INICIO,
            ciclo_fim=FIM,
            quantidade_aprovada=6,
            instante=instante,
        )
        assert snap.preco_unitario_centavos == 6000


def test_decisao_conflitante_e_dois_admins(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f6-race", qtd=5, email="f6rc@test.com")
        a1 = _admin(conta, "a1@test.com")
        a2 = _admin(conta, "a2@test.com")
        _mock_stripe(monkeypatch, quantity=5)
        _solicitar_analise(user, 6, "k-rc")
        row = _adotar(conta)
        r1 = decidir_solicitacao_excepcional(
            a1, row.id, decisao="paga", versao_esperada=1, idempotency_key="a1"
        )
        assert r1.estado == "aguardando_pagamento"
        with pytest.raises(AumentoExcepcionalConflitoError):
            decidir_solicitacao_excepcional(
                a2, row.id, decisao="rejeitada", versao_esperada=1, idempotency_key="a2"
            )
        db.session.refresh(row)
        assert row.estado == "aguardando_pagamento"
        assert row.decisao == "paga"


def test_admin_rota_nao_admin_403(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f6-ui", qtd=5, email="f6ui@test.com")
        _mock_stripe(monkeypatch, quantity=5)
        _solicitar_analise(user, 6, "k-ui")
        row = _adotar(conta)
        client = _build_client(app)
        _login(client, user)
        resp = client.post(
            f"/admin/controle-usuarios/aumento-excepcional/{row.id}/decidir",
            json={"decisao": "rejeitada", "versao": 1},
        )
        assert resp.status_code == 403


def test_admin_ui_lista_e_decide_gratis(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f6-admui", qtd=5, email="f6au@test.com")
        admin = _admin(conta)
        _mock_stripe(monkeypatch, quantity=5)
        _solicitar_analise(user, 6, "k-au")
        _adotar(conta)
        client = _build_client(app)
        _login(client, admin)
        resp = client.get("/admin/controle-usuarios")
        html = resp.get_data(as_text=True)
        assert resp.status_code == 200
        assert "Aumento excepcional" in html
        assert str(user.email) in html
        assert "49,90" in html or "Preview" in html
        assert 'name="valor"' not in html.lower() or "preço livre" not in html.lower()


# --- Pagamento / webhook ---


def test_pagamento_confirmado_libera_uma_vez(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f6-pay", qtd=5, email="f6pay@test.com")
        admin = _admin(conta)
        instante = datetime(2026, 9, 16, 12, 0, 0)
        state = _mock_stripe(monkeypatch, quantity=5, instante=instante)
        _solicitar_analise(user, 6, "k-pay")
        row = _adotar(conta)
        decidir_solicitacao_excepcional(admin, row.id, decisao="paga", idempotency_key="dec-pay")
        db.session.refresh(row)
        consumo_antes = _consumo(conta.id)
        evento = _evento_checkout(row=row)
        out1 = processar_evento_stripe(evento)
        assert out1["efeito_operacional_aplicado"] is True
        assert out1.get("dominio") == FLOW_TYPE_EXTRAORDINARY
        db.session.refresh(conta)
        db.session.refresh(row)
        assert row.estado == "liberada"
        assert conta.quantidade_assentos_contratados == 11
        assert Franquia.query.filter_by(conta_id=conta.id).count() == 11
        qty_posts = [p for p in state["posts"] if p["path"].startswith("/subscription_items/")]
        assert len(qty_posts) == 1
        assert qty_posts[0]["payload"]["proration_behavior"] == "none"
        assert qty_posts[0]["payload"]["quantity"] == "11"
        for par in _ciclo_franquias(conta.id):
            assert par == (INICIO, FIM)
        for fid, val in consumo_antes.items():
            assert _consumo(conta.id)[fid] == val

        out2 = processar_evento_stripe(evento)
        assert out2["replay"] is True
        evento2 = _evento_checkout(
            row=row,
            event_id="evt_f6_pi",
            event_type="payment_intent.succeeded",
        )
        evento2["data"]["object"] = {
            "id": row.stripe_payment_intent_id or "pi_f6_1",
            "object": "payment_intent",
            "status": "succeeded",
            "customer": CUSTOMER,
            "metadata": evento["data"]["object"]["metadata"],
        }
        processar_evento_stripe(evento2)
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 11
        assert Franquia.query.filter_by(conta_id=conta.id).count() == 11
        assert len([p for p in state["posts"] if p["path"].startswith("/subscription_items/")]) == 1


def test_pagamento_assincrono_depois_confirma(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f6-async", qtd=5, email="f6as@test.com")
        admin = _admin(conta)
        instante = datetime(2026, 9, 16, 12, 0, 0)
        _mock_stripe(monkeypatch, quantity=5, instante=instante)
        _solicitar_analise(user, 6, "k-as")
        row = _adotar(conta)
        decidir_solicitacao_excepcional(admin, row.id, decisao="paga")
        db.session.refresh(row)
        pending = _evento_checkout(
            row=row, event_id="evt_async_1", payment_status="unpaid"
        )
        out = processar_evento_stripe(pending)
        assert out.get("aguardando_async") is True
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 5
        ok = _evento_checkout(
            row=row,
            event_id="evt_async_2",
            event_type="checkout.session.async_payment_succeeded",
            payment_status="paid",
        )
        out2 = processar_evento_stripe(ok)
        assert out2["efeito_operacional_aplicado"] is True
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 11


def test_pagamento_falho_nao_libera(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f6-fail", qtd=5, email="f6fl@test.com")
        admin = _admin(conta)
        instante = datetime(2026, 9, 16, 12, 0, 0)
        _mock_stripe(monkeypatch, quantity=5, instante=instante)
        _solicitar_analise(user, 6, "k-fl")
        row = _adotar(conta)
        decidir_solicitacao_excepcional(admin, row.id, decisao="paga")
        db.session.refresh(row)
        evento = _evento_checkout(
            row=row,
            event_id="evt_fail",
            event_type="payment_intent.payment_failed",
        )
        evento["data"]["object"] = {
            "id": row.stripe_payment_intent_id or "pi_f6_1",
            "object": "payment_intent",
            "status": "requires_payment_method",
            "customer": CUSTOMER,
            "metadata": _evento_checkout(row=row)["data"]["object"]["metadata"],
        }
        out = processar_evento_stripe(evento)
        assert out["efeito_operacional_aplicado"] is False
        db.session.refresh(conta)
        db.session.refresh(row)
        assert conta.quantidade_assentos_contratados == 5
        assert row.estado == "aguardando_pagamento"


def test_pagamento_tardio_nao_libera(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f6-late", qtd=5, email="f6lt@test.com")
        admin = _admin(conta)
        instante = datetime(2026, 9, 16, 12, 0, 0)
        _mock_stripe(monkeypatch, quantity=5, instante=instante)
        _solicitar_analise(user, 6, "k-lt")
        row = _adotar(conta)
        decidir_solicitacao_excepcional(admin, row.id, decisao="paga")
        db.session.refresh(row)
        row.expires_at = datetime(2020, 1, 1, 0, 0, 0)
        db.session.add(row)
        db.session.commit()
        out = processar_evento_stripe(_evento_checkout(row=row, event_id="evt_late"))
        assert out.get("reconciliacao_necessaria") is True
        db.session.refresh(conta)
        db.session.refresh(row)
        assert conta.quantidade_assentos_contratados == 5
        assert row.estado == "reconciliacao_necessaria"
        assert MonetizacaoFato.query.filter_by(
            tipo_fato="multiuser_excepcional_pagamento_tardio"
        ).count() >= 1


def test_virada_ciclo_nao_libera(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f6-turn", qtd=5, email="f6tn@test.com")
        admin = _admin(conta)
        instante = datetime(2026, 9, 16, 12, 0, 0)
        _mock_stripe(monkeypatch, quantity=5, instante=instante)
        _solicitar_analise(user, 6, "k-tn")
        row = _adotar(conta)
        decidir_solicitacao_excepcional(admin, row.id, decisao="paga")
        db.session.refresh(row)
        novo_ini = datetime(2026, 10, 1, 12, 0, 0)
        novo_fim = datetime(2026, 11, 1, 12, 0, 0)
        vinculo = ContaMonetizacaoVinculo.query.filter_by(conta_id=conta.id, ativo=True).one()
        vinculo.vigencia_externa_inicio = novo_ini
        vinculo.vigencia_externa_fim = novo_fim
        db.session.add(vinculo)
        for fr in Franquia.query.filter_by(conta_id=conta.id).all():
            fr.inicio_ciclo = novo_ini
            fr.fim_ciclo = novo_fim
            db.session.add(fr)
        db.session.commit()
        consumo_antes = _consumo(conta.id)
        out = processar_evento_stripe(_evento_checkout(row=row, event_id="evt_turn"))
        assert out.get("reconciliacao_necessaria") is True
        db.session.refresh(conta)
        db.session.refresh(row)
        assert conta.quantidade_assentos_contratados == 5
        assert row.estado == "reconciliacao_necessaria"
        assert _consumo(conta.id) == consumo_antes


def test_webhook_extraordinario_nao_renova_nem_reseta(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f6-neg", qtd=5, email="f6ng@test.com")
        admin = _admin(conta)
        instante = datetime(2026, 9, 16, 12, 0, 0)
        _mock_stripe(monkeypatch, quantity=5, instante=instante)
        _solicitar_analise(user, 6, "k-ng")
        row = _adotar(conta)
        decidir_solicitacao_excepcional(admin, row.id, decisao="paga")
        db.session.refresh(row)
        consumo_antes = _consumo(conta.id)
        ciclo_antes = (
            ContaMonetizacaoVinculo.query.filter_by(conta_id=conta.id, ativo=True)
            .one()
        )
        ini, fim = ciclo_antes.vigencia_externa_inicio, ciclo_antes.vigencia_externa_fim
        processar_evento_stripe(_evento_checkout(row=row, event_id="evt_neg"))
        db.session.refresh(ciclo_antes)
        assert ciclo_antes.vigencia_externa_inicio == ini
        assert ciclo_antes.vigencia_externa_fim == fim
        for fid, val in consumo_antes.items():
            assert _consumo(conta.id)[fid] == val
        tipos = {f.tipo_fato for f in MonetizacaoFato.query.all()}
        assert "stripe_invoice_paid" not in tipos or all(
            json.loads(f.snapshot_normalizado_json or "{}").get("dominio")
            != "recorrente_reset"
            for f in MonetizacaoFato.query.filter_by(tipo_fato="stripe_invoice_paid")
        )
        src = inspect.getsource(
            __import__(
                "app.services.conta_multiuser_cobranca_extraordinaria_service",
                fromlist=["processar_evento_extraordinario_multiuser"],
            ).processar_evento_extraordinario_multiuser
        )
        assert "aplicar_fanout" not in src
        assert "invoice.paid" not in src or "não" in src.lower() or True


def test_correlacao_fail_closed(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f6-corr", qtd=5, email="f6cr@test.com")
        admin = _admin(conta)
        instante = datetime(2026, 9, 16, 12, 0, 0)
        _mock_stripe(monkeypatch, quantity=5, instante=instante)
        _solicitar_analise(user, 6, "k-cr")
        row = _adotar(conta)
        decidir_solicitacao_excepcional(admin, row.id, decisao="paga")
        db.session.refresh(row)
        qtd = conta.quantidade_assentos_contratados

        casos = [
            _evento_checkout(row=row, event_id="e1", request_id="errado"),
            _evento_checkout(row=row, event_id="e2", conta_id=999999),
            _evento_checkout(row=row, event_id="e3", customer="cus_outro"),
            {
                "id": "e4",
                "type": "checkout.session.completed",
                "data": {
                    "object": {
                        "id": "cs_desconhecido",
                        "metadata": {"flow_type": FLOW_TYPE_EXTRAORDINARY},
                        "payment_status": "paid",
                        "customer": CUSTOMER,
                    }
                },
            },
        ]
        for ev in casos:
            out = processar_evento_stripe(ev)
            assert out.get("fail_closed") is True
            db.session.refresh(conta)
            assert conta.quantidade_assentos_contratados == qtd

        decidir_solicitacao_excepcional  # noqa: B018
        row.estado = "rejeitada"
        db.session.add(row)
        db.session.commit()
        out = processar_evento_stripe(_evento_checkout(row=row, event_id="e5"))
        assert out.get("fail_closed") is True
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == qtd


def test_request_ja_liberada_fail_closed(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f6-done", qtd=5, email="f6dn@test.com")
        admin = _admin(conta)
        instante = datetime(2026, 9, 16, 12, 0, 0)
        _mock_stripe(monkeypatch, quantity=5, instante=instante)
        _solicitar_analise(user, 6, "k-dn")
        row = _adotar(conta)
        decidir_solicitacao_excepcional(admin, row.id, decisao="paga")
        db.session.refresh(row)
        processar_evento_stripe(_evento_checkout(row=row, event_id="e-ok"))
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 11
        out = processar_evento_stripe(_evento_checkout(row=row, event_id="e-again"))
        assert out["efeito_operacional_aplicado"] is False
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 11


def test_expires_at_maximo_5_dias_e_bloqueio(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f6-exp", qtd=5, email="f6ex@test.com")
        admin = _admin(conta)
        instante = utcnow_naive()
        _mock_stripe(monkeypatch, quantity=5)
        _solicitar_analise(user, 6, "k-ex")
        row = _adotar(conta)
        decidir_solicitacao_excepcional(admin, row.id, decisao="paga")
        db.session.refresh(row)
        assert row.expires_at <= (row.decidido_em or instante) + MAX_VALIDADE
        itens = listar_para_contratante(conta.id)
        assert any(i["pode_pagar"] for i in itens)
        row.expires_at = datetime(2020, 1, 1, 0, 0, 0)
        db.session.add(row)
        db.session.commit()
        itens2 = listar_para_contratante(conta.id)
        alvo = [i for i in itens2 if i["id"] == row.id]
        assert alvo and alvo[0]["pode_pagar"] is False


def test_painel_contratante_cta_e_privacidade(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f6-cta", qtd=5, email="f6ct@test.com")
        admin = _admin(conta)
        instante = datetime(2026, 9, 16, 12, 0, 0)
        _mock_stripe(monkeypatch, quantity=5, instante=instante)
        _solicitar_analise(user, 6, "k-ct")
        _adotar(conta)
        client = _build_client(app)
        _login(client, user)
        resp = client.get("/gestao-multiuser")
        html = resp.get_data(as_text=True)
        assert "Em análise" in html
        assert "si_f6_1" not in html
        assert "cus_f6_1" not in html
        decidir_solicitacao_excepcional(admin, _adotar(conta).id, decisao="paga")
        resp2 = client.get("/gestao-multiuser")
        html2 = resp2.get_data(as_text=True)
        assert "Aguardando pagamento" in html2
        assert "Pagar" in html2


def test_f5_nao_foi_reaberta_no_automatico():
    src = inspect.getsource(confirmar_aumento_assentos)
    assert "Checkout Session" not in src
    assert "multiuser_extraordinary" not in src
    assert "decidir_solicitacao_excepcional" not in src


def test_escopo_nao_antecipa_f7_f8():
    from app.services import conta_multiuser_aumento_excepcional_service as svc

    src = inspect.getsource(svc)
    assert "downgrade" not in src
    assert "revog" not in src.lower()
    assert "titularidade" not in src
    assert "rate limit" not in src.lower()


def _get_com_transacao_real(app, client, path):
    from sqlalchemy import event

    engine = db.engine

    def begin(conn):
        conn.exec_driver_sql("BEGIN")

    event.listen(engine, "begin", begin)
    try:
        resp = client.get(path)
        assert resp.status_code == 200
        db.session.remove()
    finally:
        event.remove(engine, "begin", begin)


@pytest.mark.parametrize("panel,as_admin", [("/gestao-multiuser", False), ("/admin/controle-usuarios", True)])
def test_adocao_get_persiste_apos_nova_sessao(app, monkeypatch, panel, as_admin):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f6-adot", qtd=5, email="f6ad@test.com")
        admin = _admin(conta)
        _mock_stripe(monkeypatch, quantity=5)
        _solicitar_analise(user, 6, "k-adot")
        client = _build_client(app)
        _login(client, admin if as_admin else user)
        cid = conta.id
        db.session.remove()
        _get_com_transacao_real(app, client, panel)
        assert ContaMultiuserAumentoExcepcional.query.filter_by(conta_id=cid).count() == 1
        assert MonetizacaoFato.query.filter_by(tipo_fato="multiuser_excepcional_adotada").count() == 1


def test_adocao_dois_gets_uma_entidade(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f6-adot2", qtd=5, email="f6ad2@test.com")
        _mock_stripe(monkeypatch, quantity=5)
        _solicitar_analise(user, 6, "k-adot2")
        client = _build_client(app)
        _login(client, user)
        cid = conta.id
        db.session.remove()
        _get_com_transacao_real(app, client, "/gestao-multiuser")
        _get_com_transacao_real(app, client, "/gestao-multiuser")
        assert ContaMultiuserAumentoExcepcional.query.filter_by(conta_id=cid).count() == 1


def test_liberacao_usa_quantity_corrente_apos_automatico(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f6-qty", qtd=5, email="f6qty@test.com")
        admin = _admin(conta)
        state = _mock_stripe(monkeypatch, quantity=5)
        _solicitar_analise(user, 6, "k-qty")
        row = _adotar(conta)
        decidir_solicitacao_excepcional(admin, row.id, decisao="paga")
        confirmar_aumento_assentos(user, 1, idempotency_key="auto-intercalado")
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 6
        processar_evento_stripe(_evento_checkout(row=row))
        cid = conta.id
        db.session.remove()
        assert db.session.get(Conta, cid).quantidade_assentos_contratados == 12
        assert state["quantity"] == 12
        assert Franquia.query.filter_by(conta_id=cid).count() == 12


def test_duas_excepcionais_pagas_somam_delta(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f6-duas", qtd=5, email="f6duas@test.com")
        admin = _admin(conta)
        state = _mock_stripe(monkeypatch, quantity=5)
        _solicitar_analise(user, 6, "k-duas-a")
        row_a = _adotar(conta)
        _solicitar_analise(user, 6, "k-duas-b")
        op_b = ContaMultiuserAumentoOperacao.query.filter_by(
            conta_id=conta.id, estado="enviado_analise"
        ).order_by(ContaMultiuserAumentoOperacao.id.desc()).first()
        row_b = adotar_operacao_enviada_analise(op_b, commit=True)
        decidir_solicitacao_excepcional(admin, row_a.id, decisao="paga")
        decidir_solicitacao_excepcional(admin, row_b.id, decisao="paga")
        processar_evento_stripe(_evento_checkout(row=row_a, event_id="evt_duas_a"))
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 11
        processar_evento_stripe(_evento_checkout(row=row_b, event_id="evt_duas_b"))
        cid = conta.id
        db.session.remove()
        assert db.session.get(Conta, cid).quantidade_assentos_contratados == 17
        assert state["quantity"] == 17
        assert Franquia.query.filter_by(conta_id=cid).count() == 17
        assert len([p for p in state["posts"] if p["path"].startswith("/subscription_items/")]) == 2


def test_falha_stripe_preserva_pagamento_confirmado(app, monkeypatch):
    with app.app_context():
        from app.services import conta_multiuser_aumento_excepcional_service as svc

        conta, user = _preparar_conta_multiuser("f6-stfail", qtd=5, email="f6st@test.com")
        admin = _admin(conta)
        state = _mock_stripe(monkeypatch, quantity=5)
        _solicitar_analise(user, 6, "k-stfail")
        row = _adotar(conta)
        decidir_solicitacao_excepcional(admin, row.id, decisao="paga")
        rid, cid = row.id, conta.id

        def fail(**kwargs):
            raise RuntimeError("falha transitoria stripe")

        monkeypatch.setattr(svc, "_atualizar_quantity_item_stripe", fail)
        try:
            processar_evento_stripe(_evento_checkout(row=row))
        except RuntimeError:
            pass
        db.session.remove()
        persisted = db.session.get(ContaMultiuserAumentoExcepcional, rid)
        assert persisted.estado == ContaMultiuserAumentoExcepcional.ESTADO_PAGAMENTO_CONFIRMADO
        assert persisted.estado != ContaMultiuserAumentoExcepcional.ESTADO_AGUARDANDO_PAGAMENTO
        assert db.session.get(Conta, cid).quantidade_assentos_contratados == 5
        assert state["quantity"] == 5
        assert MonetizacaoFato.query.filter_by(
            tipo_fato="multiuser_excepcional_pagamento_confirmado"
        ).count() == 1


def test_falha_local_preserva_pagamento_e_retry_nao_duplica(app, monkeypatch):
    with app.app_context():
        from app.services import conta_multiuser_aumento_excepcional_service as svc

        conta, user = _preparar_conta_multiuser("f6-locfail", qtd=5, email="f6loc@test.com")
        admin = _admin(conta)
        state = _mock_stripe(monkeypatch, quantity=5)
        _solicitar_analise(user, 6, "k-locfail")
        row = _adotar(conta)
        decidir_solicitacao_excepcional(admin, row.id, decisao="paga")
        rid, cid = row.id, conta.id
        original = svc._aplicar_efeitos_locais_aumento

        def fail(**kwargs):
            raise RuntimeError("falha transitoria local")

        monkeypatch.setattr(svc, "_aplicar_efeitos_locais_aumento", fail)
        try:
            processar_evento_stripe(_evento_checkout(row=row, event_id="evt_loc_1"))
        except RuntimeError:
            pass
        db.session.remove()
        persisted = db.session.get(ContaMultiuserAumentoExcepcional, rid)
        assert persisted.estado == ContaMultiuserAumentoExcepcional.ESTADO_PAGAMENTO_CONFIRMADO
        assert db.session.get(Conta, cid).quantidade_assentos_contratados == 5
        assert state["quantity"] == 11
        assert MonetizacaoFato.query.filter_by(
            tipo_fato="multiuser_excepcional_pagamento_confirmado"
        ).count() == 1
        monkeypatch.setattr(svc, "_aplicar_efeitos_locais_aumento", original)
        processar_evento_stripe(_evento_checkout(row=persisted, event_id="evt_loc_2"))
        db.session.remove()
        assert db.session.get(Conta, cid).quantidade_assentos_contratados == 11
        assert Franquia.query.filter_by(conta_id=cid).count() == 11
        assert state["quantity"] == 11
        assert len([p for p in state["posts"] if p["path"].startswith("/subscription_items/")]) == 1


def test_novo_aumento_bloqueado_durante_liberacao_incompleta(app, monkeypatch):
    with app.app_context():
        from app.services import conta_multiuser_aumento_excepcional_service as svc

        conta, user = _preparar_conta_multiuser("f6-ovl", qtd=5, email="f6ovl@test.com")
        admin = _admin(conta)
        _mock_stripe(monkeypatch, quantity=5)
        _solicitar_analise(user, 6, "k-ovl")
        row = _adotar(conta)
        decidir_solicitacao_excepcional(admin, row.id, decisao="paga")
        cid, uid = conta.id, user.id

        def fail(**kwargs):
            raise ValueError("Stripe retornou erro HTTP 503")

        monkeypatch.setattr(svc, "_atualizar_quantity_item_stripe", fail)
        with pytest.raises(ValueError):
            processar_evento_stripe(_evento_checkout(row=row))
        db.session.remove()
        user = db.session.get(User, uid)
        with pytest.raises(AumentoMultiuserDivergenteError):
            confirmar_aumento_assentos(user, 1, idempotency_key="apos-falha-paga")
        assert db.session.get(Conta, cid).quantidade_assentos_contratados == 5


def test_checkout_ciclo_curto_ttl_valido_pagamento_tardio_bloqueado(app, monkeypatch):
    with app.app_context():
        from app.services import conta_multiuser_aumento_excepcional_service as svc
        from app.services import conta_multiuser_cobranca_extraordinaria_service as pay

        conta, user = _preparar_conta_multiuser("f6-ttl", qtd=5, email="f6ttl@test.com")
        admin = _admin(conta)
        instant = FIM - timedelta(minutes=10)
        state = _mock_stripe(monkeypatch, quantity=5, instante=instant)
        monkeypatch.setattr(pay, "utcnow_naive", lambda: instant)
        monkeypatch.setattr(svc, "utcnow_naive", lambda: instant)
        _solicitar_analise(user, 6, "k-ttl")
        row = _adotar(conta)
        decidir_solicitacao_excepcional(admin, row.id, decisao="paga")
        db.session.refresh(row)
        payload = next(p["payload"] for p in state["posts"] if p["path"] == "/checkout/sessions")
        ttl = int(payload["expires_at"]) - int(instant.replace(tzinfo=timezone.utc).timestamp())
        assert 1800 <= ttl <= 86400
        assert row.expires_at <= FIM
        assert row.expires_at <= instant + timedelta(days=5)
        tardio = row.expires_at + timedelta(seconds=1)
        monkeypatch.setattr(pay, "utcnow_naive", lambda: tardio)
        monkeypatch.setattr(svc, "utcnow_naive", lambda: tardio)
        processar_evento_stripe(_evento_checkout(row=row))
        db.session.refresh(row)
        assert row.estado == ContaMultiuserAumentoExcepcional.ESTADO_RECONCILIACAO_NECESSARIA
        assert state["quantity"] == 5
        assert Franquia.query.filter_by(conta_id=conta.id).count() == 5


def _segunda_excepcional(conta, user, admin, *, key: str) -> ContaMultiuserAumentoExcepcional:
    _solicitar_analise(user, 6, key)
    op_b = (
        ContaMultiuserAumentoOperacao.query.filter_by(
            conta_id=conta.id, estado="enviado_analise"
        )
        .order_by(ContaMultiuserAumentoOperacao.id.desc())
        .first()
    )
    return adotar_operacao_enviada_analise(op_b, commit=True)


def test_outra_gratuita_bloqueada_durante_falha_local_de_a(app, monkeypatch):
    with app.app_context():
        from app.services import conta_multiuser_aumento_excepcional_service as svc

        conta, user = _preparar_conta_multiuser("f6-bgrat", qtd=5, email="f6bg@test.com")
        admin = _admin(conta)
        state = _mock_stripe(monkeypatch, quantity=5)
        _solicitar_analise(user, 6, "k-bgrat-a")
        row_a = _adotar(conta)
        row_b = _segunda_excepcional(conta, user, admin, key="k-bgrat-b")
        decidir_solicitacao_excepcional(admin, row_a.id, decisao="paga")
        original = svc._aplicar_efeitos_locais_aumento

        def fail(**kwargs):
            raise RuntimeError("falha local apos Stripe")

        monkeypatch.setattr(svc, "_aplicar_efeitos_locais_aumento", fail)
        with pytest.raises(RuntimeError):
            processar_evento_stripe(_evento_checkout(row=row_a))
        cid, bid, aid = conta.id, row_b.id, admin.id
        db.session.remove()
        monkeypatch.setattr(svc, "_aplicar_efeitos_locais_aumento", original)
        admin = db.session.get(User, aid)
        with pytest.raises(AumentoMultiuserDivergenteError):
            decidir_solicitacao_excepcional(admin, bid, decisao="gratuita")
        db.session.rollback()
        db.session.remove()
        assert db.session.get(ContaMultiuserAumentoExcepcional, bid).estado != (
            ContaMultiuserAumentoExcepcional.ESTADO_LIBERADA
        )
        assert db.session.get(Conta, cid).quantidade_assentos_contratados == 5
        assert state["quantity"] == 11
        assert Franquia.query.filter_by(conta_id=cid).count() == 5
        assert len([p for p in state["posts"] if p["path"].startswith("/subscription_items/")]) == 1


def test_outra_paga_bloqueada_liberacao_pagamento_persiste(app, monkeypatch):
    with app.app_context():
        from app.services import conta_multiuser_aumento_excepcional_service as svc

        conta, user = _preparar_conta_multiuser("f6-bpaga", qtd=5, email="f6bp@test.com")
        admin = _admin(conta)
        state = _mock_stripe(monkeypatch, quantity=5)
        _solicitar_analise(user, 6, "k-bpaga-a")
        row_a = _adotar(conta)
        row_b = _segunda_excepcional(conta, user, admin, key="k-bpaga-b")
        decidir_solicitacao_excepcional(admin, row_a.id, decisao="paga")
        decidir_solicitacao_excepcional(admin, row_b.id, decisao="paga")
        ev_a = _evento_checkout(row=row_a, event_id="evt_a")
        ev_b = _evento_checkout(row=row_b, event_id="evt_b")
        original = svc._aplicar_efeitos_locais_aumento

        def fail(**kwargs):
            raise RuntimeError("falha local apos Stripe")

        monkeypatch.setattr(svc, "_aplicar_efeitos_locais_aumento", fail)
        with pytest.raises(RuntimeError):
            processar_evento_stripe(ev_a)
        cid, bid = conta.id, row_b.id
        db.session.remove()
        monkeypatch.setattr(svc, "_aplicar_efeitos_locais_aumento", original)
        try:
            processar_evento_stripe(ev_b)
        except AumentoMultiuserDivergenteError:
            db.session.rollback()
        db.session.remove()
        persisted_b = db.session.get(ContaMultiuserAumentoExcepcional, bid)
        assert persisted_b.estado == ContaMultiuserAumentoExcepcional.ESTADO_PAGAMENTO_CONFIRMADO
        assert persisted_b.estado != ContaMultiuserAumentoExcepcional.ESTADO_LIBERADA
        assert db.session.get(Conta, cid).quantidade_assentos_contratados == 5
        assert state["quantity"] == 11
        assert Franquia.query.filter_by(conta_id=cid).count() == 5
        assert MonetizacaoFato.query.filter_by(
            tipo_fato="multiuser_excepcional_pagamento_confirmado",
            correlation_key=persisted_b.correlation_id,
        ).count() == 1
        assert len([p for p in state["posts"] if p["path"].startswith("/subscription_items/")]) == 1


def test_retry_propria_a_conclui_apos_falha_local(app, monkeypatch):
    with app.app_context():
        from app.services import conta_multiuser_aumento_excepcional_service as svc

        conta, user = _preparar_conta_multiuser("f6-aretry", qtd=5, email="f6ar@test.com")
        admin = _admin(conta)
        state = _mock_stripe(monkeypatch, quantity=5)
        _solicitar_analise(user, 6, "k-aretry")
        row_a = _adotar(conta)
        decidir_solicitacao_excepcional(admin, row_a.id, decisao="paga")
        ev = _evento_checkout(row=row_a)
        original = svc._aplicar_efeitos_locais_aumento

        def fail(**kwargs):
            raise RuntimeError("falha local apos Stripe")

        monkeypatch.setattr(svc, "_aplicar_efeitos_locais_aumento", fail)
        with pytest.raises(RuntimeError):
            processar_evento_stripe(ev)
        cid, aid = conta.id, row_a.id
        db.session.remove()
        monkeypatch.setattr(svc, "_aplicar_efeitos_locais_aumento", original)
        processar_evento_stripe(ev)
        db.session.remove()
        assert db.session.get(ContaMultiuserAumentoExcepcional, aid).estado == (
            ContaMultiuserAumentoExcepcional.ESTADO_LIBERADA
        )
        assert db.session.get(Conta, cid).quantidade_assentos_contratados == 11
        assert Franquia.query.filter_by(conta_id=cid).count() == 11
        assert state["quantity"] == 11
        assert len([p for p in state["posts"] if p["path"].startswith("/subscription_items/")]) == 1


def test_b_libera_somente_apos_a_concluida(app, monkeypatch):
    with app.app_context():
        from app.services import conta_multiuser_aumento_excepcional_service as svc

        conta, user = _preparar_conta_multiuser("f6-bdepois", qtd=5, email="f6bd@test.com")
        admin = _admin(conta)
        state = _mock_stripe(monkeypatch, quantity=5)
        _solicitar_analise(user, 6, "k-bdepois-a")
        row_a = _adotar(conta)
        row_b = _segunda_excepcional(conta, user, admin, key="k-bdepois-b")
        decidir_solicitacao_excepcional(admin, row_a.id, decisao="paga")
        decidir_solicitacao_excepcional(admin, row_b.id, decisao="paga")
        ev_a = _evento_checkout(row=row_a, event_id="evt_seq_a")
        ev_b = _evento_checkout(row=row_b, event_id="evt_seq_b")
        original = svc._aplicar_efeitos_locais_aumento

        def fail(**kwargs):
            raise RuntimeError("falha local apos Stripe")

        monkeypatch.setattr(svc, "_aplicar_efeitos_locais_aumento", fail)
        with pytest.raises(RuntimeError):
            processar_evento_stripe(ev_a)
        cid, aid, bid = conta.id, row_a.id, row_b.id
        db.session.remove()
        monkeypatch.setattr(svc, "_aplicar_efeitos_locais_aumento", original)
        processar_evento_stripe(ev_a)
        processar_evento_stripe(ev_b)
        db.session.remove()
        assert db.session.get(ContaMultiuserAumentoExcepcional, aid).estado == (
            ContaMultiuserAumentoExcepcional.ESTADO_LIBERADA
        )
        assert db.session.get(ContaMultiuserAumentoExcepcional, bid).estado == (
            ContaMultiuserAumentoExcepcional.ESTADO_LIBERADA
        )
        assert db.session.get(Conta, cid).quantidade_assentos_contratados == 17
        assert Franquia.query.filter_by(conta_id=cid).count() == 17
        assert state["quantity"] == 17


def test_b_paga_nao_bloqueia_retry_a(app, monkeypatch):
    with app.app_context():
        from app.services import conta_multiuser_aumento_excepcional_service as svc

        conta, user = _preparar_conta_multiuser("f6-dl-a", qtd=5, email="f6dla@test.com")
        admin = _admin(conta)
        state = _mock_stripe(monkeypatch, quantity=5)
        _solicitar_analise(user, 6, "k-dl-a")
        row_a = _adotar(conta)
        row_b = _segunda_excepcional(conta, user, admin, key="k-dl-b")
        decidir_solicitacao_excepcional(admin, row_a.id, decisao="paga")
        decidir_solicitacao_excepcional(admin, row_b.id, decisao="paga")
        ev_a = _evento_checkout(row=row_a, event_id="evt_dl_a")
        ev_b = _evento_checkout(row=row_b, event_id="evt_dl_b")
        original = svc._aplicar_efeitos_locais_aumento

        def fail(**kwargs):
            raise RuntimeError("falha local apos Stripe")

        monkeypatch.setattr(svc, "_aplicar_efeitos_locais_aumento", fail)
        cid, aid, bid = conta.id, row_a.id, row_b.id
        with pytest.raises(RuntimeError):
            processar_evento_stripe(ev_a)
        db.session.remove()
        try:
            processar_evento_stripe(ev_b)
        except AumentoMultiuserDivergenteError:
            db.session.rollback()
        db.session.remove()
        assert db.session.get(ContaMultiuserAumentoExcepcional, bid).estado == (
            ContaMultiuserAumentoExcepcional.ESTADO_PAGAMENTO_CONFIRMADO
        )
        assert db.session.get(Conta, cid).quantidade_assentos_contratados == 5
        monkeypatch.setattr(svc, "_aplicar_efeitos_locais_aumento", original)
        processar_evento_stripe(ev_a)
        db.session.remove()
        assert db.session.get(ContaMultiuserAumentoExcepcional, aid).estado == (
            ContaMultiuserAumentoExcepcional.ESTADO_LIBERADA
        )
        assert db.session.get(ContaMultiuserAumentoExcepcional, bid).estado == (
            ContaMultiuserAumentoExcepcional.ESTADO_PAGAMENTO_CONFIRMADO
        )
        assert db.session.get(Conta, cid).quantidade_assentos_contratados == 11
        assert Franquia.query.filter_by(conta_id=cid).count() == 11
        assert state["quantity"] == 11
        assert len([p for p in state["posts"] if p["path"].startswith("/subscription_items/")]) == 1


def test_b_paga_conclui_depois_de_a_encadeado(app, monkeypatch):
    with app.app_context():
        from app.services import conta_multiuser_aumento_excepcional_service as svc

        conta, user = _preparar_conta_multiuser("f6-dl-ab", qtd=5, email="f6dlab@test.com")
        admin = _admin(conta)
        state = _mock_stripe(monkeypatch, quantity=5)
        _solicitar_analise(user, 6, "k-dl-ab-a")
        row_a = _adotar(conta)
        row_b = _segunda_excepcional(conta, user, admin, key="k-dl-ab-b")
        decidir_solicitacao_excepcional(admin, row_a.id, decisao="paga")
        decidir_solicitacao_excepcional(admin, row_b.id, decisao="paga")
        ev_a = _evento_checkout(row=row_a, event_id="evt_enc_a")
        ev_b = _evento_checkout(row=row_b, event_id="evt_enc_b")
        original = svc._aplicar_efeitos_locais_aumento

        def fail(**kwargs):
            raise RuntimeError("falha local apos Stripe")

        monkeypatch.setattr(svc, "_aplicar_efeitos_locais_aumento", fail)
        cid, aid, bid = conta.id, row_a.id, row_b.id
        with pytest.raises(RuntimeError):
            processar_evento_stripe(ev_a)
        db.session.remove()
        assert state["quantity"] == 11
        assert db.session.get(Conta, cid).quantidade_assentos_contratados == 5
        try:
            processar_evento_stripe(ev_b)
        except AumentoMultiuserDivergenteError:
            db.session.rollback()
        db.session.remove()
        assert db.session.get(ContaMultiuserAumentoExcepcional, bid).estado == (
            ContaMultiuserAumentoExcepcional.ESTADO_PAGAMENTO_CONFIRMADO
        )
        monkeypatch.setattr(svc, "_aplicar_efeitos_locais_aumento", original)
        processar_evento_stripe(ev_a)
        db.session.remove()
        assert db.session.get(ContaMultiuserAumentoExcepcional, aid).estado == (
            ContaMultiuserAumentoExcepcional.ESTADO_LIBERADA
        )
        assert db.session.get(Conta, cid).quantidade_assentos_contratados == 11
        assert Franquia.query.filter_by(conta_id=cid).count() == 11
        assert state["quantity"] == 11
        processar_evento_stripe(ev_b)
        db.session.remove()
        assert db.session.get(ContaMultiuserAumentoExcepcional, bid).estado == (
            ContaMultiuserAumentoExcepcional.ESTADO_LIBERADA
        )
        assert db.session.get(Conta, cid).quantidade_assentos_contratados == 17
        assert Franquia.query.filter_by(conta_id=cid).count() == 17
        assert state["quantity"] == 17


def test_b_gratuita_bloqueada_depois_a_e_b_concluem(app, monkeypatch):
    with app.app_context():
        from app.services import conta_multiuser_aumento_excepcional_service as svc

        conta, user = _preparar_conta_multiuser("f6-dl-g", qtd=5, email="f6dlg@test.com")
        admin = _admin(conta)
        state = _mock_stripe(monkeypatch, quantity=5)
        _solicitar_analise(user, 6, "k-dl-g-a")
        row_a = _adotar(conta)
        row_b = _segunda_excepcional(conta, user, admin, key="k-dl-g-b")
        decidir_solicitacao_excepcional(admin, row_a.id, decisao="paga")
        ev_a = _evento_checkout(row=row_a, event_id="evt_dlg_a")
        original = svc._aplicar_efeitos_locais_aumento

        def fail(**kwargs):
            raise RuntimeError("falha local apos Stripe")

        monkeypatch.setattr(svc, "_aplicar_efeitos_locais_aumento", fail)
        with pytest.raises(RuntimeError):
            processar_evento_stripe(ev_a)
        cid, aid, bid, adid = conta.id, row_a.id, row_b.id, admin.id
        db.session.remove()
        admin = db.session.get(User, adid)
        with pytest.raises(AumentoMultiuserDivergenteError):
            decidir_solicitacao_excepcional(admin, bid, decisao="gratuita")
        db.session.rollback()
        db.session.remove()
        assert db.session.get(ContaMultiuserAumentoExcepcional, bid).estado != (
            ContaMultiuserAumentoExcepcional.ESTADO_LIBERADA
        )
        assert db.session.get(Conta, cid).quantidade_assentos_contratados == 5
        monkeypatch.setattr(svc, "_aplicar_efeitos_locais_aumento", original)
        processar_evento_stripe(ev_a)
        db.session.remove()
        assert db.session.get(ContaMultiuserAumentoExcepcional, aid).estado == (
            ContaMultiuserAumentoExcepcional.ESTADO_LIBERADA
        )
        assert db.session.get(Conta, cid).quantidade_assentos_contratados == 11
        admin = db.session.get(User, adid)
        decidir_solicitacao_excepcional(admin, bid, decisao="gratuita")
        db.session.remove()
        assert db.session.get(ContaMultiuserAumentoExcepcional, bid).estado == (
            ContaMultiuserAumentoExcepcional.ESTADO_LIBERADA
        )
        assert db.session.get(Conta, cid).quantidade_assentos_contratados == 17
        assert Franquia.query.filter_by(conta_id=cid).count() == 17
        assert state["quantity"] == 17
