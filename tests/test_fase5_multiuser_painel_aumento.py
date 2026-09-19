"""Testes incrementais da Fase 5 — painel do Contratante e aumento automático."""
from __future__ import annotations

import inspect
import json
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
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
    ContaMultiuserAumentoOperacao,
    ContaMultiuserCicloAumento,
    ContaMultiuserConvite,
    ContaVinculoOrganizacional,
    Franquia,
    MonetizacaoFato,
    User,
    utcnow_naive,
)
from app.services import plano_service
from app.services.conta_multiuser_aumento_service import (
    calcular_preview_aumento,
    confirmar_aumento_assentos,
    decidir_aumento_automatico,
    formatar_brl,
    gerar_csrf_token_aumento,
    montar_painel_contratante,
    user_eh_contratante_ativo,
)
from app.services.conta_multiuser_capacidade_service import ocupar_assento, snapshot_capacidade
from app.services.conta_multiuser_errors import GestaoMultiuserNaoAutorizadaError
from app.services.conta_organizacional_rules import (
    CHAVE_LIMITE_AUMENTO_AUTOMATICO_CICLO,
    LIMITE_AUMENTO_AUTOMATICO_CICLO_V1,
    PAPEL_MEMBRO,
)
from app.services.user_plan_control_service import atribuir_plano_para_usuario
from app.conta_multiuser_convite_routes import convite_bp
from app.conta_multiuser_painel_routes import painel_bp
from app.user_area import user_bp
from tests.conftest import seed_conta_franquia_cliente, seed_sistema_interno, seed_usuario

ROOT = Path(__file__).resolve().parents[1]
INICIO = datetime(2026, 9, 1, 12, 0, 0)
FIM = datetime(2026, 10, 1, 12, 0, 0)
PRICE_MU = "price_multiuser_f5"
CUSTOMER = "cus_f5_1"
SUBSCRIPTION = "sub_f5_1"
ITEM = "si_f5_1"


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
        gateway_product_id_raw="prod_multiuser_f5",
        gateway_price_id_raw=PRICE_MU,
        gateway_currency_raw="brl",
        gateway_interval_raw="month",
        gateway_pronto_raw=True,
    )


def _preparar_conta_multiuser(slug: str, *, qtd: int, email: str):
    from flask import current_app

    current_app.config["SECRET_KEY"] = current_app.config.get("SECRET_KEY") or "test-secret-f5"
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


def _login(client, user: User) -> None:
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def _secret(app: Flask) -> None:
    app.config["SECRET_KEY"] = "test-secret-f5"
    app.config["TESTING"] = True
    app.config["SERVER_NAME"] = "localhost"


def _build_client(app: Flask):
    _secret(app)
    app.template_folder = str(ROOT / "app" / "templates")
    if "multiuser_painel" not in app.blueprints:
        app.register_blueprint(painel_bp)
    if "multiuser_convite" not in app.blueprints:
        app.register_blueprint(convite_bp)
    if "user" not in app.blueprints:
        app.register_blueprint(user_bp)
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

    if "request_password_reset" not in app.view_functions:

        @app.route("/request-password-reset")
        def request_password_reset():  # noqa: ARG001
            return "request-password-reset"

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

    @app.context_processor
    def _inject_has_endpoint():
        return {
            "has_endpoint": lambda endpoint_name: endpoint_name in app.view_functions,
            "privacy_marketing_allowed": False,
            "privacy_marketing_state": "rejected",
            "user_is_admin": lambda _user: False,
            "falha_mensal_vigente": False,
            "regularizacao_url": "/regularizar-pagamento",
            "facebook_pixel_id": "",
            "pixel_event_complete_registration": False,
            "pixel_event_lead": False,
        }

    login_manager.init_app(app)

    @login_manager.user_loader
    def _load_user(user_id):  # noqa: ANN001
        return get_user_by_id(user_id)

    return app.test_client()


def _mock_stripe(monkeypatch, *, quantity=5, captured=None):
    from app.services import cleiton_monetizacao_service as monetizacao_service

    state = {"quantity": int(quantity), "posts": []}
    if captured is not None:
        captured["state"] = state

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
                            "price": {"id": PRICE_MU},
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
        raise AssertionError(f"POST Stripe inesperado: {path_s}")

    monkeypatch.setattr(monetizacao_service, "_stripe_get", _fake_get)
    monkeypatch.setattr(monetizacao_service, "_stripe_post", _fake_post)
    return state


def _limite():
    return plano_service.obter_limite_aumento_automatico_ciclo_multiuser()


def test_migration_d5e6f7a8b9c0_na_chain():
    cfg = Config()
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    script = ScriptDirectory.from_config(cfg)
    rev = script.get_revision("d5e6f7a8b9c0")
    assert rev is not None
    assert rev.down_revision == "c4d5e6f7a8b9"
    heads = set(script.get_heads())
    # F6 sucede F5 na cadeia linear; F5 permanece ancestral, não necessariamente head.
    assert "d5e6f7a8b9c0" in heads or script.get_revision("e6f7a8b9c0d1") is not None
    if "e6f7a8b9c0d1" in heads:
        assert script.get_revision("e6f7a8b9c0d1").down_revision == "d5e6f7a8b9c0"
    deploy = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
    assert "d5e6f7a8b9c0" in deploy


def test_limite_automatico_vem_da_autoridade_unica(app):
    with app.app_context():
        _preparar_planos_admin()
        assert plano_service.obter_limite_aumento_automatico_ciclo_multiuser() == int(
            LIMITE_AUMENTO_AUTOMATICO_CICLO_V1
        )
        plano_service.atualizar_parametros_plano_admin(
            plano_codigo="multiuser",
            valor_plano_raw="49.90",
            franquia_limite_total_raw="1000",
            quantidade_minima_raw="5",
            limite_aumento_automatico_raw="4",
        )
        assert plano_service.obter_limite_aumento_automatico_ciclo_multiuser() == 4
        src = inspect.getsource(confirmar_aumento_assentos)
        assert "<= 5" not in src
        assert "limite = 5" not in src
        assert (
            plano_service.CHAVE_LIMITE_AUMENTO_AUTOMATICO_CICLO_MULTIUSER
            == CHAVE_LIMITE_AUMENTO_AUTOMATICO_CICLO
        )
        assert "obter_limite_aumento_automatico_ciclo_multiuser" in inspect.getsource(
            plano_service
        )


def test_decidir_aumento_cumulativo_por_ciclo():
    limite = int(LIMITE_AUMENTO_AUTOMATICO_CICLO_V1)
    assert decidir_aumento_automatico(
        acumulado_ciclo=0, quantidade_solicitada=3, limite_ciclo=limite
    )
    assert decidir_aumento_automatico(
        acumulado_ciclo=3, quantidade_solicitada=2, limite_ciclo=limite
    )
    assert not decidir_aumento_automatico(
        acumulado_ciclo=3, quantidade_solicitada=3, limite_ciclo=limite
    )
    assert not decidir_aumento_automatico(
        acumulado_ciclo=limite, quantidade_solicitada=1, limite_ciclo=limite
    )


def test_painel_contratante_acessa_e_membro_nao(app):
    with app.app_context():
        conta, contratante = _preparar_conta_multiuser(
            "painel-ok", qtd=5, email="ct@test.com"
        )
        livre = [
            fr
            for fr in Franquia.query.filter_by(conta_id=conta.id).order_by(Franquia.id.asc())
            if ContaVinculoOrganizacional.query.filter_by(
                franquia_id=fr.id, estado="ativo"
            ).first()
            is None
        ][0]
        membro = seed_usuario(livre.id, conta.id, email="mb@test.com", categoria="multiuser")
        ocupar_assento(
            conta_id=conta.id,
            user_id=membro.id,
            franquia_id=livre.id,
            papel=PAPEL_MEMBRO,
            commit=True,
        )
        painel = montar_painel_contratante(contratante)
        assert painel.assentos_contratados == 5
        assert painel.usuarios_ativos == 2
        assert painel.assentos_disponiveis == 3
        assert user_eh_contratante_ativo(contratante) is True
        assert user_eh_contratante_ativo(membro) is False
        with pytest.raises(GestaoMultiuserNaoAutorizadaError):
            montar_painel_contratante(membro)


def test_painel_rota_membro_403_e_privacidade(app):
    with app.app_context():
        conta, contratante = _preparar_conta_multiuser(
            "painel-ui", qtd=5, email="ctui@test.com"
        )
        livre = [
            fr
            for fr in Franquia.query.filter_by(conta_id=conta.id).order_by(Franquia.id.asc())
            if ContaVinculoOrganizacional.query.filter_by(
                franquia_id=fr.id, estado="ativo"
            ).first()
            is None
        ][0]
        membro = seed_usuario(livre.id, conta.id, email="mbui@test.com", categoria="multiuser")
        ocupar_assento(
            conta_id=conta.id,
            user_id=membro.id,
            franquia_id=livre.id,
            papel=PAPEL_MEMBRO,
            commit=True,
        )
        vinculo_m = ContaVinculoOrganizacional.query.filter_by(
            user_id=membro.id, estado="ativo"
        ).one()
        assert vinculo_m.papel == PAPEL_MEMBRO
        client = _build_client(app)
        _login(client, contratante)
        resp = client.get("/gestao-multiuser")
        html = resp.get_data(as_text=True)
        assert resp.status_code == 200
        assert "Assentos contratados" in html
        assert "si_f5_1" not in html
        assert "cus_f5_1" not in html
        assert "correlation_id" not in html
        from app.services import conta_multiuser_aumento_service as aumento_svc

        src_painel = inspect.getsource(aumento_svc)
        assert "ProcessingEvent" not in src_painel
        assert "JuliaDoc" not in src_painel
        assert "cleide_audit" not in src_painel
        assert "agente_compara" not in src_painel
        with client.session_transaction() as sess:
            sess.clear()
            sess["_user_id"] = str(membro.id)
            sess["_fresh"] = True
        csrf_m = gerar_csrf_token_aumento(int(membro.id))
        resp_m = client.post(
            "/api/multiuser/aumento/preview",
            json={"csrf_token": csrf_m, "quantidade": 1},
        )
        assert resp_m.status_code == 403
        body_m = resp_m.get_json()
        assert body_m["ok"] is False
        assert body_m["codigo"] in {"nao_autorizado", "csrf_invalido"}


def test_painel_perfil_atalho_so_contratante(app):
    with app.app_context():
        _conta, contratante = _preparar_conta_multiuser(
            "atalho", qtd=5, email="atalho@test.com"
        )
        client = _build_client(app)
        _login(client, contratante)
        resp = client.get("/perfil")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Gestão da equipe" in html
        assert "Alterar senha" in html
        assert 'href="/request-password-reset"' in html


@pytest.mark.parametrize("qtd", [1, 3, 5])
def test_preview_valores_e_hoje_zero(app, qtd):
    with app.app_context():
        _conta, user = _preparar_conta_multiuser(
            f"prev-{qtd}", qtd=5, email=f"prev{qtd}@test.com"
        )
        preview = calcular_preview_aumento(user, qtd)
        unit = Decimal("49.90")
        assert preview.quantity_atual == 5
        assert preview.quantidade_solicitada == qtd
        assert preview.nova_quantity == 5 + qtd
        assert preview.valor_hoje == Decimal("0.00")
        assert preview.valor_mensal_atual == unit * 5
        assert preview.valor_mensal_projetado == unit * (5 + qtd)
        assert preview.aumento_renovacao == unit * qtd
        assert preview.proxima_cobranca == FIM
        assert formatar_brl(preview.valor_hoje) == "R$ 0,00"
        limite = _limite()
        assert preview.automatico is (qtd <= limite)


def test_cumulativo_mais_3_depois_mais_2_automatico(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("cum-32", qtd=5, email="cum32@test.com")
        state = _mock_stripe(monkeypatch, quantity=5)
        r1 = confirmar_aumento_assentos(user, 3, idempotency_key="k-3")
        assert r1.automatico is True
        assert r1.quantity_nova == 8
        r2 = confirmar_aumento_assentos(user, 2, idempotency_key="k-2")
        assert r2.automatico is True
        assert r2.quantity_nova == 10
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 10
        contador = ContaMultiuserCicloAumento.query.filter_by(conta_id=conta.id).one()
        assert contador.acumulado_automatico == 5
        assert len(state["posts"]) == 2


def test_cumulativo_mais_3_depois_mais_3_segundo_nao_automatico(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("cum-33", qtd=5, email="cum33@test.com")
        state = _mock_stripe(monkeypatch, quantity=5)
        r1 = confirmar_aumento_assentos(user, 3, idempotency_key="k33-a")
        assert r1.automatico is True
        posts_antes = len(state["posts"])
        qtd_antes = conta.quantidade_assentos_contratados
        r2 = confirmar_aumento_assentos(user, 3, idempotency_key="k33-b")
        assert r2.automatico is False
        assert r2.requer_analise is True
        assert "análise" in r2.mensagem.lower()
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == qtd_antes
        assert len(state["posts"]) == posts_antes
        assert ContaMultiuserAumentoOperacao.query.filter_by(
            conta_id=conta.id, estado="enviado_analise"
        ).count() == 1


def test_cumulativo_mais_5_depois_mais_1_segundo_nao_automatico(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("cum-51", qtd=5, email="cum51@test.com")
        state = _mock_stripe(monkeypatch, quantity=5)
        r1 = confirmar_aumento_assentos(user, 5, idempotency_key="k51-a")
        assert r1.automatico is True
        posts_antes = len(state["posts"])
        r2 = confirmar_aumento_assentos(user, 1, idempotency_key="k51-b")
        assert r2.requer_analise is True
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 10
        assert len(state["posts"]) == posts_antes


def test_acima_do_limite_nao_libera_nem_altera_stripe(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("over", qtd=5, email="over@test.com")
        state = _mock_stripe(monkeypatch, quantity=5)
        limite = _limite()
        r = confirmar_aumento_assentos(user, limite + 1, idempotency_key="k-over")
        assert r.requer_analise is True
        assert r.automatico is False
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 5
        assert state["posts"] == []
        assert Franquia.query.filter_by(conta_id=conta.id).count() == 5


def test_nao_aplica_parcialmente(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("parcial", qtd=5, email="parcial@test.com")
        _mock_stripe(monkeypatch, quantity=5)
        confirmar_aumento_assentos(user, 4, idempotency_key="k-p1")
        r = confirmar_aumento_assentos(user, 2, idempotency_key="k-p2")
        assert r.requer_analise is True
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 9


def test_idempotencia_mesmo_request_um_efeito(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("idemp", qtd=5, email="idemp@test.com")
        state = _mock_stripe(monkeypatch, quantity=5)
        r1 = confirmar_aumento_assentos(user, 2, idempotency_key="same-key")
        r2 = confirmar_aumento_assentos(user, 2, idempotency_key="same-key")
        assert r1.automatico is True
        assert r2.replay is True
        assert r2.quantity_nova == r1.quantity_nova
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 7
        assert len(state["posts"]) == 1
        assert ContaMultiuserAumentoOperacao.query.filter_by(conta_id=conta.id).count() == 1


def test_stripe_preserva_customer_subscription_item(app, monkeypatch):
    with app.app_context():
        _conta, user = _preparar_conta_multiuser("stripe", qtd=5, email="st@test.com")
        state = _mock_stripe(monkeypatch, quantity=5)
        r = confirmar_aumento_assentos(user, 3, idempotency_key="k-st")
        assert r.automatico is True
        assert r.customer_id == CUSTOMER
        assert r.subscription_id == SUBSCRIPTION
        assert r.subscription_item_id == ITEM
        assert r.proration_behavior == "none"
        assert len(state["posts"]) == 1
        assert state["posts"][0]["path"] == f"/subscription_items/{ITEM}"
        assert state["posts"][0]["payload"]["proration_behavior"] == "none"
        assert state["posts"][0]["payload"]["quantity"] == "8"
        assert state["quantity"] == 8


def test_franquias_novas_mesmo_ciclo_sem_reset(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("frq", qtd=5, email="frq@test.com")
        existentes = Franquia.query.filter_by(conta_id=conta.id).all()
        consumo_antes = {int(fr.id): Decimal(str(fr.consumo_acumulado)) for fr in existentes}
        _mock_stripe(monkeypatch, quantity=5)
        r = confirmar_aumento_assentos(user, 3, idempotency_key="k-frq")
        assert r.automatico is True
        assert r.franquias_criadas == 3
        snap = snapshot_capacidade(conta.id)
        assert snap.quantidade_contratada == 8
        assert snap.franquias_total == 8
        assert snap.franquias_disponiveis == 7
        for fr in Franquia.query.filter_by(conta_id=conta.id).all():
            assert fr.inicio_ciclo == INICIO
            assert fr.fim_ciclo == FIM
            if int(fr.id) in consumo_antes:
                assert Decimal(str(fr.consumo_acumulado)) == consumo_antes[int(fr.id)]


def test_divergencia_quantity_bloqueia(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("div", qtd=5, email="div@test.com")
        _mock_stripe(monkeypatch, quantity=9)
        with pytest.raises(Exception, match="diverg"):
            confirmar_aumento_assentos(user, 1, idempotency_key="k-div")
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 5
        assert MonetizacaoFato.query.filter_by(
            tipo_fato="multiuser_aumento_falha_reconciliacao"
        ).count() >= 1


def test_reset_contador_na_virada_de_ciclo(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("ciclo", qtd=5, email="ciclo@test.com")
        _mock_stripe(monkeypatch, quantity=5)
        confirmar_aumento_assentos(user, 4, idempotency_key="k-c1")
        antigo = ContaMultiuserCicloAumento.query.filter_by(conta_id=conta.id).one()
        assert antigo.acumulado_automatico == 4
        vinculo = ContaMonetizacaoVinculo.query.filter_by(conta_id=conta.id, ativo=True).one()
        novo_ini = FIM
        novo_fim = FIM + timedelta(days=30)
        vinculo.vigencia_externa_inicio = novo_ini
        vinculo.vigencia_externa_fim = novo_fim
        for fr in Franquia.query.filter_by(conta_id=conta.id).all():
            fr.inicio_ciclo = novo_ini
            fr.fim_ciclo = novo_fim
            db.session.add(fr)
        db.session.add(vinculo)
        db.session.commit()
        r = confirmar_aumento_assentos(user, 3, idempotency_key="k-c2")
        assert r.automatico is True
        novo = ContaMultiuserCicloAumento.query.filter_by(
            conta_id=conta.id, ciclo_inicio=novo_ini, ciclo_fim=novo_fim
        ).one()
        assert novo.acumulado_automatico == 3
        assert antigo.id != novo.id


def test_concorrencia_mais_3_e_mais_3(app, monkeypatch):
    with app.app_context():
        from app.services.conta_multiuser_aumento_service import (
            _contador_ciclo_atual,
            _reservar_acumulado_automatico,
        )

        conta, user = _preparar_conta_multiuser("conc33", qtd=5, email="conc33@test.com")
        state = _mock_stripe(monkeypatch, quantity=5)
        limite = _limite()
        contador, _ciclo = _contador_ciclo_atual(conta.id)
        assert _reservar_acumulado_automatico(contador, 3, limite) is True
        assert _reservar_acumulado_automatico(contador, 3, limite) is False
        db.session.rollback()
        r1 = confirmar_aumento_assentos(user, 3, idempotency_key="conc-seq-a")
        r2 = confirmar_aumento_assentos(user, 3, idempotency_key="conc-seq-b")
        assert r1.automatico is True
        assert r2.requer_analise is True
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 8
        assert len(state["posts"]) == 1


def test_concorrencia_mais_5_e_mais_1(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("conc51", qtd=5, email="conc51@test.com")
        state = _mock_stripe(monkeypatch, quantity=5)
        r5 = confirmar_aumento_assentos(user, 5, idempotency_key="c51-5")
        r1 = confirmar_aumento_assentos(user, 1, idempotency_key="c51-1")
        assert r5.automatico is True
        assert r1.requer_analise is True
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 10
        assert len(state["posts"]) == 1


def test_admin_nao_substitui_papel(app, monkeypatch):
    with app.app_context():
        conta, _ct = _preparar_conta_multiuser("adm", qtd=5, email="admct@test.com")
        livre = [
            fr
            for fr in Franquia.query.filter_by(conta_id=conta.id).order_by(Franquia.id.asc())
            if ContaVinculoOrganizacional.query.filter_by(
                franquia_id=fr.id, estado="ativo"
            ).first()
            is None
        ][0]
        admin = seed_usuario(livre.id, conta.id, email="admin@test.com", categoria="multiuser")
        admin.is_admin = True
        db.session.add(admin)
        db.session.commit()
        ocupar_assento(
            conta_id=conta.id,
            user_id=admin.id,
            franquia_id=livre.id,
            papel=PAPEL_MEMBRO,
            commit=True,
        )
        _mock_stripe(monkeypatch, quantity=5)
        with pytest.raises(GestaoMultiuserNaoAutorizadaError):
            confirmar_aumento_assentos(admin, 1, idempotency_key="k-adm")


def test_reducao_recusada(app, monkeypatch):
    with app.app_context():
        _conta, user = _preparar_conta_multiuser("red", qtd=5, email="red@test.com")
        _mock_stripe(monkeypatch, quantity=5)
        with pytest.raises(Exception):
            confirmar_aumento_assentos(user, 0, idempotency_key="k-red")
        with pytest.raises(Exception):
            confirmar_aumento_assentos(user, -1, idempotency_key="k-red2")


def test_apis_preview_e_confirmacao(app, monkeypatch):
    with app.app_context():
        _conta, user = _preparar_conta_multiuser("api", qtd=5, email="api@test.com")
        _mock_stripe(monkeypatch, quantity=5)
        client = _build_client(app)
        _login(client, user)
        csrf = gerar_csrf_token_aumento(user.id)
        prev = client.post(
            "/api/multiuser/aumento/preview",
            json={"csrf_token": csrf, "quantidade": 1},
        )
        assert prev.status_code == 200
        body = prev.get_json()
        assert body["ok"] is True
        assert body["valor_hoje"] == "0.00"
        assert body["valor_hoje_rotulo"] == "Hoje: R$ 0,00"
        assert body["proxima_cobranca_rotulo"] == "01/10/2026"
        conf = client.post(
            "/api/multiuser/aumento/confirmar",
            json={"csrf_token": csrf, "quantidade": 1, "idempotency_key": "api-1"},
        )
        assert conf.status_code == 200
        out = conf.get_json()
        assert out["automatico"] is True
        assert out["quantity_nova"] == 6


def test_fonte_nao_antecipa_f6_f7_f8():
    src = inspect.getsource(
        __import__("app.services.conta_multiuser_aumento_service", fromlist=["x"])
    )
    assert "PaymentIntent" not in src
    assert "prorat" not in src.lower() or "proration_behavior" in src
    assert "revogar" not in src.lower()
    assert "titularidade" not in src.lower()


def _post_aumento_http(app, user, quantidade, key="f5-reg", endpoint="confirmar"):
    client = _build_client(app)
    _login(client, user)
    csrf = gerar_csrf_token_aumento(int(user.id))
    response = client.post(
        f"/api/multiuser/aumento/{endpoint}",
        json={
            "csrf_token": csrf,
            "quantidade": quantidade,
            "idempotency_key": key,
        },
    )
    return client, response


def test_preview_http_zero_escritas(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser(
            "prev-ro", qtd=5, email="prevro@test.com"
        )
        livre = [
            fr
            for fr in Franquia.query.filter_by(conta_id=conta.id).order_by(Franquia.id.asc())
            if ContaVinculoOrganizacional.query.filter_by(
                franquia_id=fr.id, estado="ativo"
            ).first()
            is None
        ][0]
        convite = ContaMultiuserConvite(
            conta_id=conta.id,
            franquia_id=livre.id,
            criado_por_user_id=user.id,
            email_destino="expired-f5@test.com",
            expires_at=utcnow_naive() - timedelta(days=1),
        )
        db.session.add(convite)
        db.session.commit()
        convite_id = convite.id
        conta_id = conta.id
        user_id = user.id
        fatos_antes = MonetizacaoFato.query.count()
    _mock_stripe(monkeypatch, quantity=5)
    with app.app_context():
        user_ref = db.session.get(User, user_id)
        _client, response = _post_aumento_http(
            app, user_ref, 1, key="prev-ro", endpoint="preview"
        )
        assert response.status_code == 200
        assert ContaMultiuserCicloAumento.query.count() == 0
        assert MonetizacaoFato.query.count() == fatos_antes
        assert db.session.get(ContaMultiuserConvite, convite_id).estado == "pendente"
        assert db.session.get(Conta, conta_id).quantidade_assentos_contratados == 5
        assert Franquia.query.filter_by(conta_id=conta_id).count() == 5
        assert ContaMultiuserAumentoOperacao.query.count() == 0


def test_idempotencia_http_payload_diferente_conflito(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser(
            "idemp-diff", qtd=5, email="idempdiff@test.com"
        )
        user_id = user.id
        conta_id = conta.id
    state = _mock_stripe(monkeypatch, quantity=5)
    with app.app_context():
        user_ref = db.session.get(User, user_id)
        client, first = _post_aumento_http(app, user_ref, 1, key="same-payload")
        assert first.status_code == 200
        csrf = gerar_csrf_token_aumento(int(user_id))
        second = client.post(
            "/api/multiuser/aumento/confirmar",
            json={
                "csrf_token": csrf,
                "quantidade": 3,
                "idempotency_key": "same-payload",
            },
        )
        assert second.status_code == 409, second.get_json()
        assert db.session.get(Conta, conta_id).quantidade_assentos_contratados == 6
        assert Franquia.query.filter_by(conta_id=conta_id).count() == 6
        assert ContaMultiuserCicloAumento.query.one().acumulado_automatico == 1
        assert len(state["posts"]) == 1


def test_retry_http_valores_financeiros_estaveis(app, monkeypatch):
    with app.app_context():
        _conta, user = _preparar_conta_multiuser(
            "idemp-fin", qtd=5, email="idempfin@test.com"
        )
        user_id = user.id
    _mock_stripe(monkeypatch, quantity=5)
    with app.app_context():
        user_ref = db.session.get(User, user_id)
        client, first = _post_aumento_http(app, user_ref, 1, key="same-fin")
        csrf = gerar_csrf_token_aumento(int(user_id))
        second = client.post(
            "/api/multiuser/aumento/confirmar",
            json={
                "csrf_token": csrf,
                "quantidade": 1,
                "idempotency_key": "same-fin",
            },
        )
        assert first.status_code == second.status_code == 200
        assert second.get_json()["replay"] is True
        assert (
            second.get_json()["valor_mensal_projetado"]
            == first.get_json()["valor_mensal_projetado"]
        )
        assert first.get_json()["valor_mensal_projetado"] == "299.40"


def test_falha_local_pos_stripe_preserva_evidencia_http(app, monkeypatch):
    from app.services import conta_multiuser_aumento_service as aumento_svc

    with app.app_context():
        conta, user = _preparar_conta_multiuser(
            "pos-stripe", qtd=5, email="posstripe@test.com"
        )
        user_id = user.id
        conta_id = conta.id
    state = _mock_stripe(monkeypatch, quantity=5)
    original = aumento_svc._aplicar_efeitos_locais_aumento

    def fail(**kwargs):
        original(**kwargs)
        raise RuntimeError("falha local apos materializacao")

    monkeypatch.setattr(aumento_svc, "_aplicar_efeitos_locais_aumento", fail)
    with app.app_context():
        user_ref = db.session.get(User, user_id)
        client, response = _post_aumento_http(app, user_ref, 3, key="pos-stripe")
        assert response.status_code == 500
        assert state["quantity"] == 8
        assert db.session.get(Conta, conta_id).quantidade_assentos_contratados == 5
        assert Franquia.query.filter_by(conta_id=conta_id).count() == 5
        evidencias = ContaMultiuserAumentoOperacao.query.filter_by(conta_id=conta_id).count()
        fatos_stripe = MonetizacaoFato.query.filter_by(
            tipo_fato="multiuser_aumento_stripe_quantity_atualizada"
        ).count()
        assert evidencias > 0
        assert fatos_stripe > 0
        operacao = ContaMultiuserAumentoOperacao.query.filter_by(conta_id=conta_id).one()
        assert operacao.estado == "stripe_enviado"
    monkeypatch.setattr(aumento_svc, "_aplicar_efeitos_locais_aumento", original)
    with app.app_context():
        csrf = gerar_csrf_token_aumento(int(user_id))
        retry = client.post(
            "/api/multiuser/aumento/confirmar",
            json={
                "csrf_token": csrf,
                "quantidade": 3,
                "idempotency_key": "pos-stripe",
            },
        )
        assert retry.status_code == 409
        assert len(state["posts"]) == 1
        assert db.session.get(Conta, conta_id).quantidade_assentos_contratados == 5
        assert (
            ContaMultiuserAumentoOperacao.query.filter_by(conta_id=conta_id).one().estado
            == "stripe_enviado"
        )


def test_preco_assinatura_diverge_config_bloqueia_http(app, monkeypatch):
    from app.services import cleiton_monetizacao_service as monetizacao_service

    with app.app_context():
        _conta, user = _preparar_conta_multiuser(
            "preco-div", qtd=5, email="precodiv@test.com"
        )
        user_id = user.id
        _preparar_planos_admin(valor="60.00")
    state = _mock_stripe(monkeypatch, quantity=5)
    original_get = monetizacao_service._stripe_get

    def get(*args, **kwargs):
        sub = original_get(*args, **kwargs)
        sub["items"]["data"][0]["price"].update(
            unit_amount=4990, currency="brl", recurring={"interval": "month"}
        )
        return sub

    monkeypatch.setattr(monetizacao_service, "_stripe_get", get)
    with app.app_context():
        user_ref = db.session.get(User, user_id)
        _client, response = _post_aumento_http(app, user_ref, 1, key="preco-div")
        assert response.status_code == 409, response.get_json()
        assert state["posts"] == []


def test_ciclo_stripe_novo_local_antigo_bloqueia_http(app, monkeypatch):
    from datetime import timezone

    from app.services import conta_multiuser_aumento_service as aumento_svc
    from app.services import cleiton_monetizacao_service as monetizacao_service

    with app.app_context():
        conta, user = _preparar_conta_multiuser(
            "ciclo-div", qtd=5, email="ciclodiv@test.com"
        )
        user_id = user.id
        conta_id = conta.id
    state = _mock_stripe(monkeypatch, quantity=5)
    monkeypatch.setattr(aumento_svc, "utcnow_naive", lambda: FIM + timedelta(days=1))
    original_get = monetizacao_service._stripe_get

    def get(*args, **kwargs):
        sub = original_get(*args, **kwargs)
        item = sub["items"]["data"][0]
        item["current_period_start"] = int(FIM.replace(tzinfo=timezone.utc).timestamp())
        item["current_period_end"] = int(
            (FIM + timedelta(days=31)).replace(tzinfo=timezone.utc).timestamp()
        )
        return sub

    monkeypatch.setattr(monetizacao_service, "_stripe_get", get)
    with app.app_context():
        user_ref = db.session.get(User, user_id)
        _client, response = _post_aumento_http(app, user_ref, 1, key="ciclo-div")
        assert response.status_code == 409, response.get_json()
        assert state["posts"] == []
        assert db.session.get(Conta, conta_id).quantidade_assentos_contratados == 5
        assert Franquia.query.filter_by(conta_id=conta_id).count() == 5
