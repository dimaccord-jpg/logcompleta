"""Testes incrementais da Fase 7 — lifecycle comercial Multiuser."""
from __future__ import annotations

import inspect
import json
import os
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from flask import Flask
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import NullPool

from app.extensions import db, login_manager
from app.infra import get_user_by_id
from app.models import (
    AuditoriaGerencial,
    Conta,
    ContaMonetizacaoVinculo,
    ContaMultiuserConvite,
    ContaMultiuserReducaoQuantity,
    ContaMultiuserTitularidadeSolicitacao,
    ContaVinculoOrganizacional,
    Franquia,
    MonetizacaoFato,
    NotificacaoInterna,
    User,
    utcnow_naive,
)
from app.services import plano_service
from app.services.conta_multiuser_aumento_service import gerar_csrf_token_aumento
from app.services.conta_multiuser_aumento_excepcional_service import (
    gerar_csrf_token_admin_multiuser,
)
from app.services.conta_multiuser_capacidade_service import (
    contar_capacidade_comprometida,
    limite_ocupacao_conta,
    ocupar_assento,
)
from app.services.conta_multiuser_convite_service import (
    aceitar_convite,
    criar_convite,
    gerar_csrf_token_aceite,
)
from app.services.conta_multiuser_errors import (
    CapacidadeEsgotadaError,
    GestaoMultiuserNaoAutorizadaError,
    NotificacaoInternaNaoAutorizadaError,
    ReducaoMultiuserInvalidaError,
    RevogacaoMultiuserInvalidaError,
    RevogacaoMultiuserNaoAutorizadaError,
    TitularidadeConflitoError,
    TitularidadeInvalidaError,
    TitularidadeNaoAutorizadaError,
)
from app.services.conta_multiuser_notificacao_service import (
    contar_nao_lidas,
    criar_notificacao,
    listar_notificacoes_do_user,
    marcar_como_lida,
)
from app.services.conta_multiuser_diagnostico_service import diagnosticar_conta_multiuser
from app.services.conta_multiuser_reducao_service import (
    EvidenciaCausalReducaoNaoPersistidaError,
    snapshot_reducao_para_painel,
    solicitar_reducao_quantity,
    tentar_efetivar_reducao_no_corte,
    tentar_recuperar_reducao_parcial,
)
from app.services.conta_multiuser_revogacao_service import (
    SESSION_GERACAO_KEY,
    aplicar_revogacao_contexto_sessao,
    revogar_membro,
)
from app.services.conta_multiuser_titularidade_service import (
    decidir_titularidade,
    solicitar_titularidade,
)
from app.services.conta_organizacional_rules import (
    ESTADO_ATIVO,
    PAPEL_CONTRATANTE,
    PAPEL_MEMBRO,
    STATUS_DIAG_PENDENTE_ESPERADO,
    STATUS_DIAG_RECONCILIACAO_NECESSARIA,
)
from app.services.user_plan_control_service import atribuir_plano_para_usuario
from app.conta_multiuser_convite_routes import convite_bp
from app.conta_multiuser_painel_routes import painel_bp
from app.user_area import user_bp
from tests.conftest import seed_conta_franquia_cliente, seed_sistema_interno, seed_usuario

ROOT = Path(__file__).resolve().parents[1]
INICIO = datetime(2026, 9, 1, 12, 0, 0)
FIM = datetime(2026, 10, 1, 12, 0, 0)
PRICE_MU = "price_multiuser_f7"
CUSTOMER = "cus_f7_1"
SUBSCRIPTION = "sub_f7_1"
ITEM = "si_f7_1"


def _preparar_planos_admin(*, valor="49.90", minimo="5"):
    if Conta.query.filter_by(slug=Conta.SLUG_SISTEMA).first() is None:
        seed_sistema_interno()
    plano_service.atualizar_parametros_plano_admin(
        plano_codigo="multiuser",
        valor_plano_raw=valor,
        franquia_limite_total_raw="1000",
        quantidade_minima_raw=minimo,
        limite_aumento_automatico_raw="5",
        gateway_provider_raw="stripe",
        gateway_product_id_raw="prod_multiuser_f7",
        gateway_price_id_raw=PRICE_MU,
        gateway_currency_raw="brl",
        gateway_interval_raw="month",
        gateway_pronto_raw=True,
    )


def _preparar_conta_multiuser(slug: str, *, qtd: int, email: str):
    from flask import current_app

    current_app.config["SECRET_KEY"] = current_app.config.get("SECRET_KEY") or "test-secret-f7"
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


def _franquias_livres(conta_id: int) -> list[Franquia]:
    ocupadas = {
        int(v.franquia_id)
        for v in ContaVinculoOrganizacional.query.filter_by(
            conta_id=int(conta_id), estado=ESTADO_ATIVO
        )
    }
    return [
        fr
        for fr in Franquia.query.filter_by(conta_id=int(conta_id)).order_by(Franquia.id.asc())
        if int(fr.id) not in ocupadas
    ]


def _adicionar_membros(conta: Conta, n: int, prefix: str) -> list[User]:
    livres = _franquias_livres(int(conta.id))
    out: list[User] = []
    for i in range(n):
        fr = livres[i]
        user = seed_usuario(
            fr.id, conta.id, email=f"{prefix}{i}@test.com", categoria="multiuser"
        )
        ocupar_assento(
            conta_id=int(conta.id),
            user_id=int(user.id),
            franquia_id=int(fr.id),
            papel=PAPEL_MEMBRO,
            commit=True,
        )
        db.session.refresh(user)
        out.append(user)
    return out


def _admin(conta: Conta, email: str = "adm-f7@test.com") -> User:
    fr = Franquia.query.filter_by(conta_id=conta.id).first()
    user = seed_usuario(fr.id, conta.id, email=email, categoria="free")
    user.is_admin = True
    db.session.add(user)
    db.session.commit()
    db.session.refresh(user)
    return user


def _mock_stripe(monkeypatch, *, quantity=10, captured=None):
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
            return {"id": ITEM, "subscription": SUBSCRIPTION, "quantity": state["quantity"]}
        if "/lines/" in path_s:
            return {"id": "il_f7", "quantity": payload.get("quantity")}
        raise AssertionError(f"POST Stripe inesperado: {path_s}")

    monkeypatch.setattr(monetizacao_service, "_stripe_get", _fake_get)
    monkeypatch.setattr(monetizacao_service, "_stripe_post", _fake_post)
    monkeypatch.setattr("app.auth_services.send_email", lambda *a, **k: None)
    return state


def _login(client, user: User) -> None:
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def _build_client(app: Flask):
    app.config["SECRET_KEY"] = "test-secret-f7"
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
    login_manager.login_view = "login"

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
            "notificacoes_nao_lidas": 0,
        }

    app.before_request(aplicar_revogacao_contexto_sessao)
    return app.test_client()


def _consumos(conta_id: int) -> dict[int, str]:
    return {
        int(fr.id): str(fr.consumo_acumulado)
        for fr in Franquia.query.filter_by(conta_id=int(conta_id)).order_by(Franquia.id.asc())
    }


def test_migration_f7g8h9i0j1k2_na_chain():
    cfg = Config()
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    script = ScriptDirectory.from_config(cfg)
    rev = script.get_revision("f7g8h9i0j1k2")
    assert rev is not None
    assert rev.down_revision == "e6f7a8b9c0d1"
    assert "f7g8h9i0j1k2" in set(script.get_heads())
    deploy = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
    assert "f7g8h9i0j1k2" in deploy


def test_reducao_pedido_valido_nao_altera_stripe_agora(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f7-red-ok", qtd=10, email="f7redok@test.com")
        _adicionar_membros(conta, 6, "f7ok")
        state = _mock_stripe(monkeypatch, quantity=10)
        r = solicitar_reducao_quantity(
            ator=user, quantity_futura=8, idempotency_key="red-ok"
        )
        db.session.refresh(conta)
        assert r.estado == "pendente"
        assert r.quantity_atual == 10
        assert r.quantity_futura == 8
        assert r.efetivar_em == FIM.isoformat()
        assert conta.quantidade_assentos_contratados == 10
        assert state["quantity"] == 10
        assert state["posts"] == []
        assert ContaVinculoOrganizacional.query.filter_by(
            conta_id=conta.id, estado=ESTADO_ATIVO
        ).count() == 7


def test_reducao_abaixo_dos_ativos_rejeita(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f7-red-at", qtd=10, email="f7redat@test.com")
        _adicionar_membros(conta, 7, "f7at")
        _mock_stripe(monkeypatch, quantity=10)
        with pytest.raises(ReducaoMultiuserInvalidaError):
            solicitar_reducao_quantity(ator=user, quantity_futura=7, idempotency_key="red-at")
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 10
        assert ContaMultiuserReducaoQuantity.query.count() == 0


def test_reducao_zero_rejeita(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f7-red-0", qtd=10, email="f7red0@test.com")
        _mock_stripe(monkeypatch, quantity=10)
        with pytest.raises(ReducaoMultiuserInvalidaError):
            solicitar_reducao_quantity(ator=user, quantity_futura=0, idempotency_key="red-0")


def test_reducao_corte_atualiza_mesmo_item(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f7-red-c", qtd=10, email="f7redc@test.com")
        _adicionar_membros(conta, 6, "f7c")
        state = _mock_stripe(monkeypatch, quantity=10)
        solicitar_reducao_quantity(ator=user, quantity_futura=8, idempotency_key="red-c")
        antes = _consumos(conta.id)
        out = tentar_efetivar_reducao_no_corte(conta.id, referencia=FIM, commit=True)
        db.session.refresh(conta)
        assert out.estado == "efetivada"
        assert out.proration_behavior == "none"
        assert conta.quantidade_assentos_contratados == 8
        assert state["quantity"] == 8
        assert state["posts"][0]["path"] == f"/subscription_items/{ITEM}"
        assert state["posts"][0]["payload"]["proration_behavior"] == "none"
        assert state["posts"][0]["payload"]["quantity"] == "8"
        assert _consumos(conta.id) == antes
        for fr in Franquia.query.filter_by(conta_id=conta.id):
            assert fr.inicio_ciclo == INICIO
            assert fr.fim_ciclo == FIM


def test_reducao_revalidacao_no_corte_nao_efetiva(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f7-red-re", qtd=10, email="f7redre@test.com")
        membros = _adicionar_membros(conta, 6, "f7re")
        state = _mock_stripe(monkeypatch, quantity=10)
        solicitar_reducao_quantity(ator=user, quantity_futura=8, idempotency_key="red-re")
        livres = _franquias_livres(conta.id)
        extra_a = seed_usuario(livres[0].id, conta.id, email="f7re-x1@test.com", categoria="multiuser")
        extra_b = seed_usuario(livres[1].id, conta.id, email="f7re-x2@test.com", categoria="multiuser")
        for extra, fr in ((extra_a, livres[0]), (extra_b, livres[1])):
            row = ContaVinculoOrganizacional(
                conta_id=conta.id,
                user_id=extra.id,
                franquia_id=fr.id,
                papel=PAPEL_MEMBRO,
                estado=ESTADO_ATIVO,
                titular=False,
            )
            db.session.add(row)
        db.session.commit()
        assert ContaVinculoOrganizacional.query.filter_by(
            conta_id=conta.id, estado=ESTADO_ATIVO
        ).count() == 9
        out = tentar_efetivar_reducao_no_corte(conta.id, referencia=FIM, commit=True)
        db.session.refresh(conta)
        assert out.estado == "bloqueada_no_corte"
        assert conta.quantidade_assentos_contratados == 10
        assert state["quantity"] == 10
        assert state["posts"] == []
        assert ContaVinculoOrganizacional.query.filter_by(
            conta_id=conta.id, estado=ESTADO_ATIVO
        ).count() == 9
        _ = membros


def test_reducao_futura_nao_reduz_capacidade_do_ciclo_atual(app, monkeypatch):
    """SCRUM-189: quantity atual 12 + futura 10 continua permitindo até 12 assentos."""
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f7-189", qtd=12, email="f7189@test.com")
        state = _mock_stripe(monkeypatch, quantity=12)
        for i in range(9):
            criar_convite(
                ator=user,
                email_destino=f"f7189p{i}@test.com",
                enviar=False,
                commit=True,
            )
        solicitar_reducao_quantity(
            ator=user, quantity_futura=10, idempotency_key="189-cap"
        )
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 12
        assert limite_ocupacao_conta(conta) == 12
        assert contar_capacidade_comprometida(conta.id) == 10

        criar_convite(
            ator=user, email_destino="f7189-11@test.com", enviar=False, commit=True
        )
        assert contar_capacidade_comprometida(conta.id) == 11
        criar_convite(
            ator=user, email_destino="f7189-12@test.com", enviar=False, commit=True
        )
        assert contar_capacidade_comprometida(conta.id) == 12
        with pytest.raises(CapacidadeEsgotadaError) as exc:
            criar_convite(
                ator=user, email_destino="f7189-13@test.com", enviar=False, commit=True
            )
        assert "Não há assento livre para um novo convite." in str(exc.value)

        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 12
        assert state["quantity"] == 12
        assert state["posts"] == []
        ativos_antes = ContaVinculoOrganizacional.query.filter_by(
            conta_id=conta.id, estado=ESTADO_ATIVO
        ).count()
        assert ativos_antes == 1

        out = tentar_efetivar_reducao_no_corte(conta.id, referencia=FIM, commit=True)
        db.session.refresh(conta)
        assert out.estado == "bloqueada_no_corte"
        assert conta.quantidade_assentos_contratados == 12
        assert state["quantity"] == 12
        assert state["posts"] == []
        assert (
            ContaVinculoOrganizacional.query.filter_by(
                conta_id=conta.id, estado=ESTADO_ATIVO
            ).count()
            == ativos_antes
        )


def test_reducao_futura_efetiva_quando_ocupacao_cabe_no_alvo(app, monkeypatch):
    """SCRUM-189: no corte, ocupação <= quantity futura permite efetivar sem revogar."""
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f7-189-ok", qtd=12, email="f7189ok@test.com")
        state = _mock_stripe(monkeypatch, quantity=12)
        for i in range(9):
            criar_convite(
                ator=user,
                email_destino=f"f7189ok{i}@test.com",
                enviar=False,
                commit=True,
            )
        solicitar_reducao_quantity(
            ator=user, quantity_futura=10, idempotency_key="189-ok"
        )
        ativos_antes = ContaVinculoOrganizacional.query.filter_by(
            conta_id=conta.id, estado=ESTADO_ATIVO
        ).count()
        assert ativos_antes == 1
        assert contar_capacidade_comprometida(conta.id) == 10
        out = tentar_efetivar_reducao_no_corte(conta.id, referencia=FIM, commit=True)
        db.session.refresh(conta)
        assert out.estado == "efetivada"
        assert conta.quantidade_assentos_contratados == 10
        assert state["quantity"] == 10
        assert (
            ContaVinculoOrganizacional.query.filter_by(
                conta_id=conta.id, estado=ESTADO_ATIVO
            ).count()
            == ativos_antes
        )


def test_painel_mensagem_reducao_futura_exige_adequacao_antes_do_corte(app, monkeypatch):
    """SCRUM-189: UI informa alvo, data limite (corte-1 dia) e cobrança mantida."""
    with app.app_context():
        conta, user = _preparar_conta_multiuser(
            "f7-189-ui", qtd=12, email="f7189ui@test.com"
        )
        _mock_stripe(monkeypatch, quantity=12)
        solicitar_reducao_quantity(
            ator=user, quantity_futura=10, idempotency_key="189-ui"
        )
        snap = snapshot_reducao_para_painel(int(conta.id))
        data_limite = FIM - timedelta(days=1)
        rotulo_limite = data_limite.strftime("%d/%m/%Y")
        assert snap["quantity_futura"] == 10
        assert snap["quantity_atual_no_pedido"] == 12
        assert snap["data_limite_adequacao_rotulo"] == rotulo_limite
        assert snap["efetivar_em_rotulo"] == FIM.strftime("%d/%m/%Y")
        assert rotulo_limite != snap["efetivar_em_rotulo"]

        client = _build_client(app)
        _login(client, user)
        html = " ".join(client.get("/gestao-multiuser").get_data(as_text=True).split())
        assert "Para efetivar a redução futura para <strong>10</strong> assentos" in html
        assert "manter no máximo <strong>10</strong> usuários ativos" in html
        assert f"até <strong>{rotulo_limite}</strong>" in html
        assert "a redução não será efetivada" in html
        assert "a cobrança permanecerá em <strong>12</strong> assentos" in html
        assert "A quantity atual permanece" not in html


def test_reducao_idempotente_double_click(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f7-id", qtd=10, email="f7id@test.com")
        _mock_stripe(monkeypatch, quantity=10)
        a = solicitar_reducao_quantity(ator=user, quantity_futura=8, idempotency_key="same")
        b = solicitar_reducao_quantity(ator=user, quantity_futura=8, idempotency_key="same")
        assert a.replay is False
        assert b.replay is True
        assert ContaMultiuserReducaoQuantity.query.filter_by(conta_id=conta.id).count() == 1


@pytest.fixture
def app_sqlite_isolado(tmp_path):
    """SQLite em arquivo + NullPool: conexão/transação independente real."""
    flask_app = Flask(__name__)
    db_file = tmp_path / "scrum190_isolado.sqlite"
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = (
        f"sqlite:///{db_file.resolve().as_posix()}"
    )
    flask_app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    flask_app.config["TESTING"] = True
    flask_app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "poolclass": NullPool,
        "connect_args": {"check_same_thread": False, "timeout": 30},
    }
    db.init_app(flask_app)
    with flask_app.app_context():
        import app.models  # noqa: F401

        db.create_all()
    yield flask_app
    with flask_app.app_context():
        db.session.remove()
        db.drop_all()
        engine = getattr(db, "engine", None)
        if engine is not None:
            engine.dispose()


def _fato_qty_desta_reducao(conta, row, *, quantity, correlation=None):
    cid = correlation or row.correlation_id
    db.session.add(
        MonetizacaoFato(
            tipo_fato="reducao_quantity_stripe_atualizada",
            status_tecnico="stripe_ok",
            conta_id=int(conta.id),
            usuario_id=int(row.solicitado_por_user_id),
            idempotency_key=f"mu_reducao_stripe_qty:{cid}",
            correlation_key=cid,
            snapshot_normalizado_json=json.dumps(
                {
                    "quantity": int(quantity),
                    "quantity_local": int(row.quantity_atual_no_pedido),
                    "quantity_futura": int(quantity),
                    "proration_behavior": "none",
                }
            ),
        )
    )
    db.session.commit()


def _fato_stripe_desta_reducao(conta, row, *, quantity, correlation=None):
    cid = correlation or row.correlation_id
    db.session.add(
        MonetizacaoFato(
            tipo_fato="reducao_quantity_stripe_preparada",
            status_tecnico="stripe_ok",
            conta_id=int(conta.id),
            usuario_id=int(row.solicitado_por_user_id),
            idempotency_key=f"mu_reducao_stripe_prep:{cid}",
            correlation_key=cid,
            snapshot_normalizado_json=json.dumps(
                {
                    "quantity_local": int(row.quantity_atual_no_pedido),
                    "quantity_futura": int(quantity),
                    "proration_behavior": "none",
                }
            ),
        )
    )
    db.session.commit()


def test_scrum190_stripe_desta_reducao_rollback_local_recupera(app, monkeypatch):
    """Stripe já na futura pela mesma redução + rollback local → recovery conclui."""
    with app.app_context():
        conta, user = _preparar_conta_multiuser(
            "f7-190", qtd=12, email="f7190@test.com"
        )
        state = _mock_stripe(monkeypatch, quantity=12)
        solicitar_reducao_quantity(
            ator=user, quantity_futura=10, idempotency_key="190-rec"
        )
        row = ContaMultiuserReducaoQuantity.query.filter_by(conta_id=conta.id).one()
        _fato_stripe_desta_reducao(conta, row, quantity=10)
        state["quantity"] = 10
        state["posts"].clear()
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 12
        assert row.estado == "pendente"
        antes = _consumos(conta.id)
        ativos_antes = ContaVinculoOrganizacional.query.filter_by(
            conta_id=conta.id, estado=ESTADO_ATIVO
        ).count()

        out = tentar_recuperar_reducao_parcial(conta.id, referencia=FIM, commit=True)
        db.session.refresh(conta)
        db.session.refresh(row)
        assert out.estado == "efetivada"
        assert out.stripe_escrito is False
        assert conta.quantidade_assentos_contratados == 10
        assert state["quantity"] == 10
        assert state["posts"] == []
        assert row.estado == "efetivada"
        assert _consumos(conta.id) == antes
        assert (
            ContaVinculoOrganizacional.query.filter_by(
                conta_id=conta.id, estado=ESTADO_ATIVO
            ).count()
            == ativos_antes
        )
        assert MonetizacaoFato.query.filter_by(
            tipo_fato="reducao_quantity_recuperada",
            correlation_key=row.correlation_id,
        ).count() == 1


def test_scrum190_recovery_replay_seguro(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser(
            "f7-190-rp", qtd=12, email="f7190rp@test.com"
        )
        state = _mock_stripe(monkeypatch, quantity=12)
        solicitar_reducao_quantity(
            ator=user, quantity_futura=10, idempotency_key="190-rp"
        )
        row = ContaMultiuserReducaoQuantity.query.filter_by(conta_id=conta.id).one()
        _fato_stripe_desta_reducao(conta, row, quantity=10)
        state["quantity"] = 10
        state["posts"].clear()
        primeiro = tentar_recuperar_reducao_parcial(
            conta.id, referencia=FIM, commit=True
        )
        assert primeiro.estado == "efetivada"
        assert primeiro.replay is False
        replay = tentar_recuperar_reducao_parcial(conta.id, referencia=FIM, commit=True)
        db.session.refresh(conta)
        assert replay.replay is True
        assert replay.estado == "efetivada"
        assert conta.quantidade_assentos_contratados == 10
        assert state["posts"] == []
        assert MonetizacaoFato.query.filter_by(
            tipo_fato="reducao_quantity_recuperada"
        ).count() == 1
        assert ContaMultiuserReducaoQuantity.query.filter_by(
            conta_id=conta.id, estado="efetivada"
        ).count() == 1


def test_scrum190_stripe_futura_correlacao_alheia_nao_recupera(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser(
            "f7-190-al", qtd=12, email="f7190al@test.com"
        )
        state = _mock_stripe(monkeypatch, quantity=12)
        solicitar_reducao_quantity(
            ator=user, quantity_futura=10, idempotency_key="190-al"
        )
        row = ContaMultiuserReducaoQuantity.query.filter_by(conta_id=conta.id).one()
        _fato_stripe_desta_reducao(
            conta, row, quantity=10, correlation="correlacao-alheia"
        )
        state["quantity"] = 10
        state["posts"].clear()
        out = tentar_recuperar_reducao_parcial(conta.id, referencia=FIM, commit=True)
        db.session.refresh(conta)
        db.session.refresh(row)
        assert out.estado == "reconciliacao_necessaria"
        assert row.estado == "pendente"
        assert conta.quantidade_assentos_contratados == 12
        assert state["posts"] == []
        diag = diagnosticar_conta_multiuser(int(conta.id), consultar_stripe=True)
        assert diag.status == STATUS_DIAG_RECONCILIACAO_NECESSARIA
        assert diag.status != STATUS_DIAG_PENDENTE_ESPERADO
        assert any(
            a.detalhe == "stripe_na_futura_sem_evidencia_desta_reducao"
            for a in diag.achados
        )


def test_scrum190_evidencia_insuficiente_reconciliacao(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser(
            "f7-190-ev", qtd=12, email="f7190ev@test.com"
        )
        state = _mock_stripe(monkeypatch, quantity=12)
        solicitar_reducao_quantity(
            ator=user, quantity_futura=10, idempotency_key="190-ev"
        )
        state["quantity"] = 10
        state["posts"].clear()
        db.session.refresh(conta)
        out = tentar_efetivar_reducao_no_corte(conta.id, referencia=FIM, commit=True)
        row = ContaMultiuserReducaoQuantity.query.filter_by(conta_id=conta.id).one()
        db.session.refresh(conta)
        assert out.estado == "reconciliacao_necessaria"
        assert row.estado == "pendente"
        assert row.estado != "bloqueada_no_corte"
        assert conta.quantidade_assentos_contratados == 12
        assert state["posts"] == []
        assert MonetizacaoFato.query.filter_by(
            tipo_fato="reducao_quantity_reconciliacao_necessaria"
        ).count() == 1
        diag = diagnosticar_conta_multiuser(int(conta.id), consultar_stripe=True)
        assert diag.status == STATUS_DIAG_RECONCILIACAO_NECESSARIA
        assert diag.status != STATUS_DIAG_PENDENTE_ESPERADO


def test_scrum190_efetivar_recupera_quando_fato_desta_reducao(app, monkeypatch):
    """Reexecutar o corte recupera em vez de bloquear quando a evidência é desta redução."""
    with app.app_context():
        conta, user = _preparar_conta_multiuser(
            "f7-190-ef", qtd=12, email="f7190ef@test.com"
        )
        state = _mock_stripe(monkeypatch, quantity=12)
        solicitar_reducao_quantity(
            ator=user, quantity_futura=10, idempotency_key="190-ef"
        )
        row = ContaMultiuserReducaoQuantity.query.filter_by(conta_id=conta.id).one()
        _fato_stripe_desta_reducao(conta, row, quantity=10)
        state["quantity"] = 10
        state["posts"].clear()
        out = tentar_efetivar_reducao_no_corte(conta.id, referencia=FIM, commit=True)
        db.session.refresh(conta)
        db.session.refresh(row)
        assert out.estado == "efetivada"
        assert out.stripe_escrito is False
        assert row.estado == "efetivada"
        assert conta.quantidade_assentos_contratados == 10
        assert state["posts"] == []


def test_scrum190_rollback_real_preserva_evidencia_e_recupera(
    app_sqlite_isolado, monkeypatch
):
    """Stripe escrito, sentinela local, rollback: evidência sobrevive e o recovery conclui."""
    with app_sqlite_isolado.app_context():
        from app.services import conta_multiuser_reducao_service as reducao_mod
        from app.services.conta_organizacional_service import (
            persistir_quantidade_assentos_contratados as persistir_real,
        )

        persist_src = inspect.getsource(reducao_mod._persistir_fato_efeito_stripe_isolado)
        registrar_src = inspect.getsource(reducao_mod._registrar_efeito_stripe_reducao)
        assert "db.session.get_bind()" not in persist_src
        assert "db.session.commit()" not in persist_src
        assert "db.session.commit()" not in registrar_src
        assert "engine.connect()" in persist_src
        assert "connection.begin()" in persist_src

        conta, user = _preparar_conta_multiuser(
            "f7-190-rb", qtd=12, email="f7190rb@test.com"
        )
        state = _mock_stripe(monkeypatch, quantity=12)
        solicitar_reducao_quantity(
            ator=user, quantity_futura=10, idempotency_key="190-rb"
        )
        row = ContaMultiuserReducaoQuantity.query.filter_by(conta_id=conta.id).one()
        correlation = row.correlation_id
        reducao_id = int(row.id)
        conta_id = int(conta.id)
        assert row.quantity_atual_no_pedido == 12
        assert (
            MonetizacaoFato.query.filter_by(
                idempotency_key=f"mu_reducao_stripe_qty:{correlation}"
            ).first()
            is None
        )

        commits_durante_efetivar = {"n": 0}
        commit_real = db.session.commit

        def _spy_commit():
            commits_durante_efetivar["n"] += 1
            return commit_real()

        def _sentinela_depois_explode(*_a, **_k):
            alvo = db.session.get(ContaMultiuserReducaoQuantity, reducao_id)
            alvo.quantity_atual_no_pedido = 99
            db.session.add(alvo)
            db.session.flush()
            raise RuntimeError("falha local apos sentinela")

        monkeypatch.setattr(db.session, "commit", _spy_commit)
        monkeypatch.setattr(
            reducao_mod, "persistir_quantidade_assentos_contratados", _sentinela_depois_explode
        )
        with pytest.raises(RuntimeError, match="falha local apos sentinela"):
            tentar_efetivar_reducao_no_corte(conta_id, referencia=FIM, commit=True)
        assert commits_durante_efetivar["n"] == 0
        db.session.rollback()

        conta = db.session.get(Conta, conta_id)
        row = db.session.get(ContaMultiuserReducaoQuantity, reducao_id)
        assert conta.quantidade_assentos_contratados == 12
        assert row.estado == "pendente"
        assert row.quantity_atual_no_pedido == 12
        assert row.quantity_atual_no_pedido != 99
        assert row.efetivada_em is None
        assert state["quantity"] == 10
        fato = MonetizacaoFato.query.filter_by(
            idempotency_key=f"mu_reducao_stripe_qty:{correlation}"
        ).one()
        assert fato.correlation_key == correlation
        assert fato.tipo_fato == "reducao_quantity_stripe_atualizada"
        posts_apos_falha = list(state["posts"])
        assert posts_apos_falha
        assert posts_apos_falha[0]["path"] == f"/subscription_items/{ITEM}"
        assert posts_apos_falha[0]["payload"]["quantity"] == "10"

        monkeypatch.setattr(db.session, "commit", commit_real)
        monkeypatch.setattr(
            reducao_mod, "persistir_quantidade_assentos_contratados", persistir_real
        )
        out = tentar_recuperar_reducao_parcial(conta_id, referencia=FIM, commit=True)
        db.session.refresh(conta)
        row = db.session.get(ContaMultiuserReducaoQuantity, reducao_id)
        assert out.estado == "efetivada"
        assert out.stripe_escrito is False
        assert out.replay is False
        assert conta.quantidade_assentos_contratados == 10
        assert state["quantity"] == 10
        assert state["posts"] == posts_apos_falha
        assert row.estado == "efetivada"
        assert row.efetivada_em is not None


def test_scrum190_evidencia_isolada_nao_commita_mutacao_pendente(
    app_sqlite_isolado, monkeypatch
):
    """Helper isolado não commita mutação pendente da sessão principal."""
    with app_sqlite_isolado.app_context():
        from app.services import conta_multiuser_reducao_service as reducao_mod

        conta, user = _preparar_conta_multiuser(
            "f7-190-iso", qtd=12, email="f7190iso@test.com"
        )
        _mock_stripe(monkeypatch, quantity=12)
        solicitar_reducao_quantity(
            ator=user, quantity_futura=10, idempotency_key="190-iso"
        )
        row = ContaMultiuserReducaoQuantity.query.filter_by(conta_id=conta.id).one()
        correlation = row.correlation_id
        reducao_id = int(row.id)
        row.quantity_atual_no_pedido = 77
        db.session.add(row)

        def _forbid_commit():
            raise AssertionError("helper não pode commitar a sessão principal")

        monkeypatch.setattr(db.session, "commit", _forbid_commit)
        reducao_mod._registrar_efeito_stripe_reducao(
            row,
            futura=10,
            local=12,
            customer_id=CUSTOMER,
            subscription_id=SUBSCRIPTION,
            item_id=ITEM,
        )
        db.session.rollback()

        row = db.session.get(ContaMultiuserReducaoQuantity, reducao_id)
        assert row.quantity_atual_no_pedido == 12
        assert row.quantity_atual_no_pedido != 77
        fato = MonetizacaoFato.query.filter_by(
            idempotency_key=f"mu_reducao_stripe_qty:{correlation}"
        ).one()
        assert fato.correlation_key == correlation


def test_scrum190_integrity_error_com_fato_duravel_replay_seguro(
    app_sqlite_isolado, monkeypatch
):
    with app_sqlite_isolado.app_context():
        from app.services import conta_multiuser_reducao_service as reducao_mod

        conta, user = _preparar_conta_multiuser(
            "f7-190-ie-ok", qtd=12, email="f7190ieok@test.com"
        )
        state = _mock_stripe(monkeypatch, quantity=12)
        solicitar_reducao_quantity(
            ator=user, quantity_futura=10, idempotency_key="190-ie-ok"
        )
        row = ContaMultiuserReducaoQuantity.query.filter_by(conta_id=conta.id).one()
        _fato_qty_desta_reducao(conta, row, quantity=10)

        def _raise_integrity(*_a, **_k):
            raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))

        monkeypatch.setattr(
            reducao_mod, "_persistir_fato_efeito_stripe_isolado", _raise_integrity
        )
        out = tentar_efetivar_reducao_no_corte(conta.id, referencia=FIM, commit=True)
        db.session.refresh(conta)
        db.session.refresh(row)
        assert out.estado == "efetivada"
        assert conta.quantidade_assentos_contratados == 10
        assert row.estado == "efetivada"
        assert state["quantity"] == 10
        assert len(state["posts"]) == 1


def test_scrum190_integrity_error_sem_fato_duravel_nao_sucesso(app, monkeypatch):
    with app.app_context():
        from app.services import conta_multiuser_reducao_service as reducao_mod

        conta, user = _preparar_conta_multiuser(
            "f7-190-ie-no", qtd=12, email="f7190ieno@test.com"
        )
        state = _mock_stripe(monkeypatch, quantity=12)
        solicitar_reducao_quantity(
            ator=user, quantity_futura=10, idempotency_key="190-ie-no"
        )
        row = ContaMultiuserReducaoQuantity.query.filter_by(conta_id=conta.id).one()
        correlation = row.correlation_id
        reducao_id = int(row.id)
        conta_id = int(conta.id)

        def _raise_integrity(*_a, **_k):
            raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))

        monkeypatch.setattr(
            reducao_mod, "_persistir_fato_efeito_stripe_isolado", _raise_integrity
        )
        with pytest.raises(EvidenciaCausalReducaoNaoPersistidaError):
            tentar_efetivar_reducao_no_corte(conta_id, referencia=FIM, commit=True)
        db.session.rollback()

        conta = db.session.get(Conta, conta_id)
        row = db.session.get(ContaMultiuserReducaoQuantity, reducao_id)
        assert conta.quantidade_assentos_contratados == 12
        assert row.estado == "pendente"
        assert row.efetivada_em is None
        assert state["quantity"] == 10
        assert (
            MonetizacaoFato.query.filter_by(
                idempotency_key=f"mu_reducao_stripe_qty:{correlation}"
            ).first()
            is None
        )
        assert MonetizacaoFato.query.filter_by(
            tipo_fato="reducao_quantity_efetivada",
            correlation_key=correlation,
        ).first() is None


def test_scrum190_sqlite_memory_integrity_fato_da_sessao_nao_e_duravel(
    app, monkeypatch
):
    """IntegrityError + fato só na sessão corrente do :memory: é fail-closed."""
    with app.app_context():
        from app.services import conta_multiuser_reducao_service as reducao_mod

        conta, user = _preparar_conta_multiuser(
            "f7-190-ie-sess", qtd=12, email="f7190iesess@test.com"
        )
        state = _mock_stripe(monkeypatch, quantity=12)
        solicitar_reducao_quantity(
            ator=user, quantity_futura=10, idempotency_key="190-ie-sess"
        )
        row = ContaMultiuserReducaoQuantity.query.filter_by(conta_id=conta.id).one()
        correlation = row.correlation_id
        reducao_id = int(row.id)
        conta_id = int(conta.id)
        db.session.add(
            MonetizacaoFato(
                tipo_fato="reducao_quantity_stripe_atualizada",
                status_tecnico="stripe_ok",
                conta_id=int(conta.id),
                usuario_id=int(row.solicitado_por_user_id),
                idempotency_key=f"mu_reducao_stripe_qty:{correlation}",
                correlation_key=correlation,
                snapshot_normalizado_json=json.dumps(
                    {
                        "quantity": 10,
                        "quantity_local": 12,
                        "quantity_futura": 10,
                        "proration_behavior": "none",
                    }
                ),
            )
        )
        db.session.flush()
        assert (
            MonetizacaoFato.query.filter_by(
                idempotency_key=f"mu_reducao_stripe_qty:{correlation}"
            ).first()
            is not None
        )

        def _raise_integrity(*_a, **_k):
            raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))

        monkeypatch.setattr(
            reducao_mod, "_persistir_fato_efeito_stripe_isolado", _raise_integrity
        )
        with pytest.raises(EvidenciaCausalReducaoNaoPersistidaError):
            tentar_efetivar_reducao_no_corte(conta_id, referencia=FIM, commit=True)
        db.session.rollback()

        conta = db.session.get(Conta, conta_id)
        row = db.session.get(ContaMultiuserReducaoQuantity, reducao_id)
        assert conta.quantidade_assentos_contratados == 12
        assert row.estado == "pendente"
        assert row.estado != "efetivada"
        assert row.efetivada_em is None
        assert state["quantity"] == 10
        assert (
            MonetizacaoFato.query.filter_by(
                idempotency_key=f"mu_reducao_stripe_qty:{correlation}"
            ).first()
            is None
        )
        assert MonetizacaoFato.query.filter_by(
            tipo_fato="reducao_quantity_efetivada",
            correlation_key=correlation,
        ).first() is None


def test_scrum190_excecao_generica_evidencia_nao_conclui_local(app, monkeypatch):
    with app.app_context():
        from app.services import conta_multiuser_reducao_service as reducao_mod

        conta, user = _preparar_conta_multiuser(
            "f7-190-ex", qtd=12, email="f7190ex@test.com"
        )
        state = _mock_stripe(monkeypatch, quantity=12)
        solicitar_reducao_quantity(
            ator=user, quantity_futura=10, idempotency_key="190-ex"
        )
        row = ContaMultiuserReducaoQuantity.query.filter_by(conta_id=conta.id).one()
        correlation = row.correlation_id
        reducao_id = int(row.id)
        conta_id = int(conta.id)

        def _raise_generic(*_a, **_k):
            raise RuntimeError("falha ao gravar evidencia")

        monkeypatch.setattr(
            reducao_mod, "_persistir_fato_efeito_stripe_isolado", _raise_generic
        )
        with pytest.raises(EvidenciaCausalReducaoNaoPersistidaError):
            tentar_efetivar_reducao_no_corte(conta_id, referencia=FIM, commit=True)
        db.session.rollback()

        conta = db.session.get(Conta, conta_id)
        row = db.session.get(ContaMultiuserReducaoQuantity, reducao_id)
        assert conta.quantidade_assentos_contratados == 12
        assert row.estado == "pendente"
        assert row.efetivada_em is None
        assert state["quantity"] == 10
        assert MonetizacaoFato.query.filter_by(
            tipo_fato="reducao_quantity_efetivada",
            correlation_key=correlation,
        ).first() is None


def test_revogacao_membro_libera_capacidade_sem_stripe(app, monkeypatch):
    with app.app_context():
        conta, contratante = _preparar_conta_multiuser("f7-rv", qtd=10, email="f7rv@test.com")
        membros = _adicionar_membros(conta, 7, "f7rvm")
        alvo = membros[0]
        estado = _mock_stripe(monkeypatch, quantity=10)
        consumo = db.session.get(Franquia, alvo.franquia_id).consumo_acumulado
        uid = alvo.id
        franquia_id = alvo.franquia_id
        r = revogar_membro(ator=contratante, alvo_user_id=uid, commit=True)
        db.session.refresh(conta)
        alvo = db.session.get(User, uid)
        vinculo = ContaVinculoOrganizacional.query.filter_by(id=r.vinculo_id).one()
        assert vinculo.estado == "encerrado"
        assert alvo is not None
        assert (alvo.email or "").endswith("@test.com")
        assert alvo.categoria != "multiuser"
        assert int(alvo.conta_id) != int(conta.id)
        assert conta.quantidade_assentos_contratados == 10
        assert estado["posts"] == []
        assert r.capacidade_livre == 3
        assert ContaVinculoOrganizacional.query.filter_by(
            conta_id=conta.id, estado=ESTADO_ATIVO
        ).count() == 7
        assert db.session.get(Franquia, franquia_id).consumo_acumulado == consumo
        assert MonetizacaoFato.query.filter_by(tipo_fato="membership_revogado").count() == 1


def test_revogacao_negativa_e_idempotente(app, monkeypatch):
    with app.app_context():
        conta, contratante = _preparar_conta_multiuser("f7-rvn", qtd=10, email="f7rvn@test.com")
        membros = _adicionar_membros(conta, 2, "f7rvn")
        _mock_stripe(monkeypatch, quantity=10)
        membro = membros[0]
        with pytest.raises(RevogacaoMultiuserNaoAutorizadaError):
            revogar_membro(ator=membro, alvo_user_id=membros[1].id)
        with pytest.raises(RevogacaoMultiuserInvalidaError):
            revogar_membro(ator=contratante, alvo_user_id=contratante.id)
        outra, _ = seed_conta_franquia_cliente(slug="f7-outra")
        outsider = seed_usuario(
            Franquia.query.filter_by(conta_id=outra.id).first().id,
            outra.id,
            email="out@test.com",
        )
        with pytest.raises(RevogacaoMultiuserNaoAutorizadaError):
            revogar_membro(ator=outsider, alvo_user_id=membro.id)
        a = revogar_membro(ator=contratante, alvo_user_id=membro.id)
        b = revogar_membro(ator=contratante, alvo_user_id=membro.id)
        assert a.replay is False
        assert b.replay is True
        assert MonetizacaoFato.query.filter_by(tipo_fato="membership_revogado").count() == 1


def _aceitar_convite_de(owner: User, alvo: User):
    inv = criar_convite(ator=owner, email_destino=alvo.email, enviar=False)
    csrf = gerar_csrf_token_aceite(alvo.id, inv.convite_id)
    aceitar_convite(user=alvo, token=inv.token, csrf_token=csrf)
    return inv, csrf


def test_revogacao_reentradas_reutiliza_contexto_individual(app, monkeypatch):
    with app.app_context():
        conta, owner = _preparar_conta_multiuser("f7-reent", qtd=10, email="f7reent@test.com")
        stripe = _mock_stripe(monkeypatch, quantity=10)
        origens = []
        users = []
        for i in range(2):
            origin, fr = seed_conta_franquia_cliente(slug=f"f7-origin-{i}")
            origens.append(origin)
            users.append(
                seed_usuario(fr.id, origin.id, email=f"f7-origin-{i}@reent.test")
            )
        for user in users:
            _aceitar_convite_de(owner, user)
        target = users[0]
        origin_id = origens[0].id
        contas_antes = Conta.query.count()
        client = _build_client(app)
        _login(client, owner)
        for cycle in range(3):
            if cycle:
                _aceitar_convite_de(owner, target)
            response = client.post(
                f"/api/multiuser/membros/{target.id}/revogar",
                json={"csrf_token": gerar_csrf_token_aumento(owner.id)},
            )
            assert response.status_code == 200, (cycle, response.get_json())
            db.session.refresh(target)
            assert int(target.conta_id) == int(origin_id)
            assert target.categoria == "free"
            assert (
                ContaVinculoOrganizacional.query.filter_by(
                    user_id=target.id, conta_id=conta.id, estado=ESTADO_ATIVO
                ).count()
                == 0
            )
            assert db.session.get(User, target.id) is not None
            assert conta.quantidade_assentos_contratados == 10
            assert stripe["posts"] == []
            assert Conta.query.count() == contas_antes
        replay = client.post(
            f"/api/multiuser/membros/{target.id}/revogar",
            json={"csrf_token": gerar_csrf_token_aumento(owner.id)},
        )
        assert replay.status_code == 200
        assert replay.get_json().get("replay") is True
        db.session.refresh(target)
        assert int(target.conta_id) == int(origin_id)
        assert Conta.query.count() == contas_antes
        assert MonetizacaoFato.query.filter_by(tipo_fato="membership_revogado").count() == 3
        assert target.sessao_contexto_geracao == 3


def test_revogacao_replay_sem_novos_efeitos(app, monkeypatch):
    with app.app_context():
        conta, contratante = _preparar_conta_multiuser(
            "f7-rpl", qtd=10, email="f7rpl@test.com"
        )
        origin, fr = seed_conta_franquia_cliente(slug="f7-rpl-origin")
        alvo = seed_usuario(fr.id, origin.id, email="f7-rpl-origin@reent.test")
        _mock_stripe(monkeypatch, quantity=10)
        _aceitar_convite_de(contratante, alvo)
        first = revogar_membro(ator=contratante, alvo_user_id=alvo.id)
        db.session.refresh(alvo)
        contas = Conta.query.count()
        geracao = alvo.sessao_contexto_geracao
        restored = int(alvo.conta_id)
        fatos = MonetizacaoFato.query.filter_by(tipo_fato="membership_revogado").count()
        second = revogar_membro(ator=contratante, alvo_user_id=alvo.id)
        db.session.refresh(alvo)
        assert first.replay is False
        assert second.replay is True
        assert alvo.sessao_contexto_geracao == geracao
        assert int(alvo.conta_id) == restored == int(origin.id)
        assert Conta.query.count() == contas
        assert MonetizacaoFato.query.filter_by(tipo_fato="membership_revogado").count() == fatos
        assert (
            ContaVinculoOrganizacional.query.filter_by(
                user_id=alvo.id, conta_id=conta.id, estado=ESTADO_ATIVO
            ).count()
            == 0
        )


def test_revogacao_sessao_perde_contexto(app, monkeypatch):
    with app.app_context():
        conta, contratante = _preparar_conta_multiuser("f7-sess", qtd=10, email="f7sess@test.com")
        membros = _adicionar_membros(conta, 1, "f7sessm")
        alvo = membros[0]
        _mock_stripe(monkeypatch, quantity=10)
        client = _build_client(app)
        _login(client, alvo)
        with client.session_transaction() as sess:
            sess["cleide_audit_doc_ids"] = [99]
            sess[SESSION_GERACAO_KEY] = 0
        revogar_membro(ator=contratante, alvo_user_id=alvo.id, commit=True)
        client.get("/")
        with client.session_transaction() as sess:
            assert "cleide_audit_doc_ids" not in sess
            assert sess.get(SESSION_GERACAO_KEY) == 1
        alvo = db.session.get(User, alvo.id)
        assert int(alvo.conta_id) != int(conta.id)


def test_titularidade_aprovada_preserva_billing(app, monkeypatch):
    with app.app_context():
        conta, titular = _preparar_conta_multiuser("f7-tit", qtd=10, email="f7tit@test.com")
        membros = _adicionar_membros(conta, 1, "f7titm")
        candidato = membros[0]
        admin = _admin(conta)
        state = _mock_stripe(monkeypatch, quantity=10)
        is_admin_titular = bool(titular.is_admin)
        is_admin_cand = bool(candidato.is_admin)
        ciclo = (INICIO, FIM)
        consumo = _consumos(conta.id)
        sol = solicitar_titularidade(
            admin=admin,
            conta_id=conta.id,
            candidato_id=candidato.id,
            motivo="suporte",
            idempotency_key="tit-1",
        )
        dec = decidir_titularidade(
            admin=admin,
            solicitacao_id=sol.solicitacao_id,
            decisao="aprovada",
            versao=sol.versao,
            idempotency_key="dec-1",
        )
        db.session.refresh(conta)
        tvin = ContaVinculoOrganizacional.query.filter_by(
            user_id=titular.id, conta_id=conta.id, estado=ESTADO_ATIVO
        ).one()
        cvin = ContaVinculoOrganizacional.query.filter_by(
            user_id=candidato.id, conta_id=conta.id, estado=ESTADO_ATIVO
        ).one()
        assert tvin.papel == PAPEL_MEMBRO
        assert cvin.papel == PAPEL_CONTRATANTE
        assert (
            ContaVinculoOrganizacional.query.filter_by(
                conta_id=conta.id, papel=PAPEL_CONTRATANTE, estado=ESTADO_ATIVO
            ).count()
            == 1
        )
        vinculo = ContaMonetizacaoVinculo.query.filter_by(conta_id=conta.id, ativo=True).one()
        assert vinculo.customer_id == CUSTOMER
        assert vinculo.subscription_id == SUBSCRIPTION
        assert vinculo.price_id == PRICE_MU
        assert conta.quantidade_assentos_contratados == 10
        assert state["posts"] == []
        assert _consumos(conta.id) == consumo
        for fr in Franquia.query.filter_by(conta_id=conta.id):
            assert (fr.inicio_ciclo, fr.fim_ciclo) == ciclo
        assert db.session.get(User, titular.id).is_admin == is_admin_titular
        assert db.session.get(User, candidato.id).is_admin == is_admin_cand
        assert db.session.get(User, titular.id) is not None
        assert dec.estado == "aprovada"


def test_titularidade_negativa(app, monkeypatch):
    with app.app_context():
        conta, titular = _preparar_conta_multiuser("f7-titn", qtd=10, email="f7titn@test.com")
        membros = _adicionar_membros(conta, 1, "f7titnm")
        candidato = membros[0]
        admin = _admin(conta)
        _mock_stripe(monkeypatch, quantity=10)
        outra, _ = seed_conta_franquia_cliente(slug="f7-tit-o")
        alien = seed_usuario(
            Franquia.query.filter_by(conta_id=outra.id).first().id,
            outra.id,
            email="alien@test.com",
        )
        with pytest.raises(TitularidadeInvalidaError):
            solicitar_titularidade(
                admin=admin, conta_id=conta.id, candidato_id=alien.id, motivo="x"
            )
        with pytest.raises(TitularidadeNaoAutorizadaError):
            solicitar_titularidade(
                admin=titular, conta_id=conta.id, candidato_id=candidato.id, motivo="x"
            )
        sol = solicitar_titularidade(
            admin=admin,
            conta_id=conta.id,
            candidato_id=candidato.id,
            motivo="ok",
            idempotency_key="t-n",
        )
        revogar_membro(ator=titular, alvo_user_id=candidato.id)
        with pytest.raises(TitularidadeInvalidaError):
            decidir_titularidade(
                admin=admin,
                solicitacao_id=sol.solicitacao_id,
                decisao="aprovada",
                versao=sol.versao,
            )


def test_titularidade_decisao_duplicada(app, monkeypatch):
    with app.app_context():
        conta, _titular = _preparar_conta_multiuser("f7-dup", qtd=10, email="f7dup@test.com")
        candidato = _adicionar_membros(conta, 1, "f7dupm")[0]
        admin = _admin(conta, "adm-a@test.com")
        admin_b = _admin(conta, "adm-b@test.com")
        _mock_stripe(monkeypatch, quantity=10)
        sol = solicitar_titularidade(
            admin=admin,
            conta_id=conta.id,
            candidato_id=candidato.id,
            motivo="dup",
            idempotency_key="dup-s",
        )
        a = decidir_titularidade(
            admin=admin,
            solicitacao_id=sol.solicitacao_id,
            decisao="aprovada",
            versao=sol.versao,
            idempotency_key="dup-a",
        )
        with pytest.raises(TitularidadeConflitoError):
            decidir_titularidade(
                admin=admin_b,
                solicitacao_id=sol.solicitacao_id,
                decisao="rejeitada",
                versao=sol.versao,
                idempotency_key="dup-b",
            )
        replay = decidir_titularidade(
            admin=admin,
            solicitacao_id=sol.solicitacao_id,
            decisao="aprovada",
            idempotency_key="dup-a",
        )
        assert a.replay is False
        assert replay.replay is True
        assert (
            ContaVinculoOrganizacional.query.filter_by(
                conta_id=conta.id, papel=PAPEL_CONTRATANTE, estado=ESTADO_ATIVO
            ).count()
            == 1
        )


def test_titularidade_mesma_chave_replay_e_conflito(app, monkeypatch):
    with app.app_context():
        conta, titular = _preparar_conta_multiuser(
            "f7-key", qtd=10, email="f7key@test.com"
        )
        candidato = _adicionar_membros(conta, 1, "f7keym")[0]
        admin = _admin(conta)
        _mock_stripe(monkeypatch, quantity=10)
        sol = solicitar_titularidade(
            admin=admin,
            conta_id=conta.id,
            candidato_id=candidato.id,
            motivo="key",
            idempotency_key="sol-key",
        )
        primeira = decidir_titularidade(
            admin=admin,
            solicitacao_id=sol.solicitacao_id,
            decisao="aprovar",
            idempotency_key="decision",
        )
        replay = decidir_titularidade(
            admin=admin,
            solicitacao_id=sol.solicitacao_id,
            decisao="aprovar",
            idempotency_key="decision",
        )
        assert primeira.replay is False
        assert replay.replay is True
        assert replay.estado == "aprovada"
        auditorias = AuditoriaGerencial.query.filter_by(
            tipo_decisao="titularidade_aprovada"
        ).count()
        with pytest.raises(TitularidadeConflitoError):
            decidir_titularidade(
                admin=admin,
                solicitacao_id=sol.solicitacao_id,
                decisao="rejeitar",
                idempotency_key="decision",
            )
        row = db.session.get(ContaMultiuserTitularidadeSolicitacao, sol.solicitacao_id)
        assert row.estado == "aprovada"
        assert (
            AuditoriaGerencial.query.filter_by(
                tipo_decisao="titularidade_aprovada"
            ).count()
            == auditorias
        )
        assert (
            AuditoriaGerencial.query.filter_by(
                tipo_decisao="titularidade_rejeitada"
            ).count()
            == 0
        )
        assert (
            ContaVinculoOrganizacional.query.filter_by(
                conta_id=conta.id, papel=PAPEL_CONTRATANTE, estado=ESTADO_ATIVO
            ).count()
            == 1
        )
        assert (
            ContaVinculoOrganizacional.query.filter_by(
                user_id=candidato.id, conta_id=conta.id, estado=ESTADO_ATIVO, papel=PAPEL_CONTRATANTE
            ).count()
            == 1
        )
        assert (
            ContaVinculoOrganizacional.query.filter_by(
                user_id=titular.id, conta_id=conta.id, estado=ESTADO_ATIVO, papel=PAPEL_MEMBRO
            ).count()
            == 1
        )


def test_titularidade_mesma_chave_rejeitar_depois_aprovar_conflito(app, monkeypatch):
    with app.app_context():
        conta, titular = _preparar_conta_multiuser(
            "f7-keyb", qtd=10, email="f7keyb@test.com"
        )
        candidato = _adicionar_membros(conta, 1, "f7keybm")[0]
        admin = _admin(conta)
        client = _build_client(app)
        _login(client, admin)
        _mock_stripe(monkeypatch, quantity=10)
        sol = solicitar_titularidade(
            admin=admin,
            conta_id=conta.id,
            candidato_id=candidato.id,
            motivo="keyb",
            idempotency_key="sol-keyb",
        )
        url = f"/admin/controle-usuarios/titularidade/{sol.solicitacao_id}/decidir"
        csrf = gerar_csrf_token_admin_multiuser(int(admin.id))
        assert (
            client.post(
                url,
                json={"decisao": "rejeitar", "idempotency_key": "decision", "csrf_token": csrf},
            ).status_code
            == 200
        )
        conflito = client.post(
            url, json={"decisao": "aprovar", "idempotency_key": "decision", "csrf_token": csrf}
        )
        assert conflito.status_code == 409, conflito.get_json()
        row = db.session.get(ContaMultiuserTitularidadeSolicitacao, sol.solicitacao_id)
        assert row.estado == "rejeitada"
        assert (
            ContaVinculoOrganizacional.query.filter_by(
                user_id=titular.id, conta_id=conta.id, estado=ESTADO_ATIVO, papel=PAPEL_CONTRATANTE
            ).count()
            == 1
        )
        assert (
            AuditoriaGerencial.query.filter_by(
                tipo_decisao="titularidade_aprovada"
            ).count()
            == 0
        )


def test_titularidade_nao_cancela_contrato(app, monkeypatch):
    src = inspect.getsource(decidir_titularidade) + inspect.getsource(solicitar_titularidade)
    assert "cancel" not in src.lower()
    assert "/subscriptions/" not in src
    with app.app_context():
        conta, _t = _preparar_conta_multiuser("f7-can", qtd=10, email="f7can@test.com")
        cand = _adicionar_membros(conta, 1, "f7canm")[0]
        admin = _admin(conta)
        _mock_stripe(monkeypatch, quantity=10)
        sol = solicitar_titularidade(
            admin=admin, conta_id=conta.id, candidato_id=cand.id, motivo="c"
        )
        decidir_titularidade(
            admin=admin, solicitacao_id=sol.solicitacao_id, decisao="aprovada"
        )
        assert db.session.get(Conta, conta.id).status == Conta.STATUS_ATIVA
        assert ContaMonetizacaoVinculo.query.filter_by(conta_id=conta.id, ativo=True).count() == 1


def test_notificacoes_privadas_idempotentes(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f7-nt", qtd=10, email="f7nt@test.com")
        outro = _adicionar_membros(conta, 1, "f7nto")[0]
        _mock_stripe(monkeypatch, quantity=10)
        a = criar_notificacao(
            user_id=user.id,
            conta_id=conta.id,
            tipo="reducao_solicitada",
            mensagem="msg",
            dedup_key="dedup-1",
            cta_interno="user.perfil",
            commit=True,
        )
        b = criar_notificacao(
            user_id=user.id,
            conta_id=conta.id,
            tipo="reducao_solicitada",
            mensagem="msg",
            dedup_key="dedup-1",
            cta_interno="user.perfil",
            commit=True,
        )
        assert a.id == b.id
        assert contar_nao_lidas(user) == 1
        assert listar_notificacoes_do_user(user)[0].cta_interno == "user.perfil"
        with pytest.raises(NotificacaoInternaNaoAutorizadaError):
            marcar_como_lida(outro, a.id)
        marcar_como_lida(user, a.id)
        assert contar_nao_lidas(user) == 0


def test_email_failure_nao_desfaz_revogacao(app, monkeypatch):
    with app.app_context():
        conta, contratante = _preparar_conta_multiuser("f7-em", qtd=10, email="f7em@test.com")
        alvo = _adicionar_membros(conta, 1, "f7emm")[0]
        _mock_stripe(monkeypatch, quantity=10)

        def _boom(*a, **k):
            raise RuntimeError("smtp down")

        monkeypatch.setattr("app.auth_services.send_email", _boom)
        revogar_membro(ator=contratante, alvo_user_id=alvo.id, commit=True)
        vinculo = ContaVinculoOrganizacional.query.filter_by(
            user_id=alvo.id, conta_id=conta.id
        ).order_by(ContaVinculoOrganizacional.id.desc()).first()
        assert vinculo.estado == "encerrado"


def test_independencia_fluxos(app, monkeypatch):
    with app.app_context():
        conta, contratante = _preparar_conta_multiuser("f7-ind", qtd=10, email="f7ind@test.com")
        membros = _adicionar_membros(conta, 3, "f7indm")
        state = _mock_stripe(monkeypatch, quantity=10)
        revogar_membro(ator=contratante, alvo_user_id=membros[0].id)
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 10
        assert state["posts"] == []
        ativos_antes = ContaVinculoOrganizacional.query.filter_by(
            conta_id=conta.id, estado=ESTADO_ATIVO
        ).count()
        solicitar_reducao_quantity(ator=contratante, quantity_futura=8, idempotency_key="ind")
        assert ContaVinculoOrganizacional.query.filter_by(
            conta_id=conta.id, estado=ESTADO_ATIVO
        ).count() == ativos_antes
        admin = _admin(conta)
        sol = solicitar_titularidade(
            admin=admin,
            conta_id=conta.id,
            candidato_id=membros[1].id,
            motivo="ind",
        )
        decidir_titularidade(
            admin=admin, solicitacao_id=sol.solicitacao_id, decisao="aprovada"
        )
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 10
        assert state["posts"] == []
        assert ContaMonetizacaoVinculo.query.filter_by(
            conta_id=conta.id, ativo=True
        ).one().subscription_id == SUBSCRIPTION
        assert db.session.get(Conta, conta.id).status == Conta.STATUS_ATIVA


def test_painel_sem_self_service_titularidade(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f7-ui", qtd=10, email="f7ui@test.com")
        _adicionar_membros(conta, 1, "f7uim")
        _mock_stripe(monkeypatch, quantity=10)
        client = _build_client(app)
        _login(client, user)
        html = client.get("/gestao-multiuser").get_data(as_text=True)
        assert "Encerrar acesso" in html
        assert "Reduzir assentos" in html
        assert "Transferir titularidade" not in html
        assert "self-service" not in html.lower()


def test_membro_nao_acessa_gestao_nem_reduz(app, monkeypatch):
    with app.app_context():
        conta, _c = _preparar_conta_multiuser("f7-mng", qtd=10, email="f7mng@test.com")
        membro = _adicionar_membros(conta, 1, "f7mngm")[0]
        _mock_stripe(monkeypatch, quantity=10)
        with pytest.raises(ReducaoMultiuserInvalidaError):
            solicitar_reducao_quantity(ator=membro, quantity_futura=8)
        client = _build_client(app)
        _login(client, membro)
        assert client.get("/gestao-multiuser").status_code == 403


def test_escopo_nao_inicia_f8():
    for rel in (
        "app/services/conta_multiuser_revogacao_service.py",
        "app/services/conta_multiuser_reducao_service.py",
        "app/services/conta_multiuser_titularidade_service.py",
        "app/services/conta_multiuser_notificacao_service.py",
    ):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "rate limit" not in src.lower()
        assert "api publica" not in src.lower()
        assert "credential" not in src.lower()
