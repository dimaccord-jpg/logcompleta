"""Testes da Fase 8 — privacidade, reconciliação, BI e contratos reutilizáveis."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from flask import Flask
from flask_login import login_user

from app.cleiton_doc_escopo import record_matches_explicit_scope
from app.extensions import db, login_manager
from app.infra import get_user_by_id
from app.models import (
    Conta,
    ContaMonetizacaoVinculo,
    ContaMultiuserAumentoExcepcional,
    ContaMultiuserAumentoOperacao,
    ContaMultiuserReducaoQuantity,
    ContaVinculoOrganizacional,
    Franquia,
    MonetizacaoFato,
    User,
    utcnow_naive,
)
from app.services import plano_service
from app.services.conta_multiuser_aumento_service import (
    montar_painel_contratante_por_ator_id,
    painel_para_template,
    user_eh_contratante_ativo,
)
from app.services.conta_multiuser_autorizacao_service import (
    mesmo_owner_operacional,
    user_eh_admin_plataforma,
    user_eh_membro_ativo,
)
from app.services.conta_multiuser_bi_service import (
    bi_expoe_conteudo_operacional,
    projetar_saude_multiuser_admin,
)
from app.services.conta_multiuser_capacidade_service import ocupar_assento
from app.services.conta_multiuser_diagnostico_service import (
    diagnosticar_conta_multiuser,
    reparos_seguros_conhecidos,
)
from app.services.conta_multiuser_errors import GestaoMultiuserNaoAutorizadaError
from app.services.conta_multiuser_eventos import listar_eventos_multiuser_v1
from app.services.conta_organizacional_rules import (
    ESTADO_ATIVO,
    PAPEL_CONTRATANTE,
    PAPEL_MEMBRO,
    STATUS_DIAG_DIVERGENTE,
    STATUS_DIAG_INCONCLUSIVO,
    STATUS_DIAG_OK,
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
PRICE_MU = "price_multiuser_f8"
CUSTOMER = "cus_f8_1"
SUBSCRIPTION = "sub_f8_1"
ITEM = "si_f8_1"


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
        gateway_product_id_raw="prod_multiuser_f8",
        gateway_price_id_raw=PRICE_MU,
        gateway_currency_raw="brl",
        gateway_interval_raw="month",
        gateway_pronto_raw=True,
    )
    plano_service.atualizar_parametros_plano_admin(
        plano_codigo="free",
        valor_plano_raw="0.00",
        franquia_limite_total_raw="50",
    )


def _preparar_conta_multiuser(slug: str, *, qtd: int, email: str):
    from flask import current_app

    current_app.config["SECRET_KEY"] = current_app.config.get("SECRET_KEY") or "test-secret-f8"
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


def _admin(conta: Conta, email: str = "adm-f8@test.com") -> User:
    fr = Franquia.query.filter_by(conta_id=conta.id).first()
    user = seed_usuario(fr.id, conta.id, email=email, categoria="free")
    user.is_admin = True
    db.session.add(user)
    db.session.commit()
    db.session.refresh(user)
    return user


def _mock_stripe(monkeypatch, *, quantity=10, fail=False):
    from app.services import cleiton_monetizacao_service as monetizacao_service

    state = {"quantity": int(quantity), "gets": 0}

    def _fake_get(path, params=None):  # noqa: ARG001
        state["gets"] += 1
        if fail:
            raise RuntimeError("stripe_indisponivel_f8")
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

    monkeypatch.setattr(monetizacao_service, "_stripe_get", _fake_get)
    monkeypatch.setattr("app.auth_services.send_email", lambda *a, **k: None)
    return state


def _login(client, user: User) -> None:
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def _build_client(app: Flask):
    app.config["SECRET_KEY"] = "test-secret-f8"
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

    return app.test_client()


def _scope(user: User) -> dict[str, int]:
    return {
        "conta_id": int(user.conta_id),
        "franquia_id": int(user.franquia_id),
        "usuario_id": int(user.id),
    }


def _artefato(user: User, *, extra=None) -> dict:
    rec = {
        "doc_id": f"doc-{user.id}",
        "conta_id": int(user.conta_id),
        "franquia_id": int(user.franquia_id),
        "usuario_id": int(user.id),
        "display_name": "privado.txt",
    }
    if extra:
        rec.update(extra)
    return rec


def _consumos(conta_id: int) -> dict[int, str]:
    return {
        int(fr.id): str(fr.consumo_acumulado)
        for fr in Franquia.query.filter_by(conta_id=int(conta_id)).order_by(Franquia.id.asc())
    }


def _inserir_f6_parcial(conta: Conta, user: User, *, estado: str, correlation: str):
    qtd_atual = int(conta.quantidade_assentos_contratados or 5)
    op = ContaMultiuserAumentoOperacao(
        conta_id=int(conta.id),
        solicitado_por_user_id=int(user.id),
        idempotency_key=f"idem-{correlation}",
        correlation_id=correlation,
        quantidade_solicitada=3,
        quantity_anterior=qtd_atual,
        estado=ContaMultiuserAumentoOperacao.ESTADO_ENVIADO_ANALISE,
    )
    db.session.add(op)
    db.session.flush()
    row = ContaMultiuserAumentoExcepcional(
        operacao_id=int(op.id),
        conta_id=int(conta.id),
        solicitante_id=int(user.id),
        ciclo_inicio=INICIO,
        ciclo_fim=FIM,
        quantity_atual=qtd_atual,
        quantidade_solicitada=3,
        estado=estado,
        correlation_id=correlation,
        request_id=f"req-{correlation}",
        versao=1,
    )
    db.session.add(row)
    if estado == ContaMultiuserAumentoExcepcional.ESTADO_PAGAMENTO_CONFIRMADO:
        db.session.add(
            MonetizacaoFato(
                tipo_fato="multiuser_excepcional_stripe_quantity_atualizada",
                status_tecnico="stripe_ok",
                conta_id=int(conta.id),
                idempotency_key=f"mu_exc_stripe_qty:{correlation}",
                correlation_key=correlation,
                snapshot_normalizado_json=json.dumps({"quantity": qtd_atual + 3}),
            )
        )
    db.session.commit()
    return row


# --- Migration / escopo ---


def test_f8_nao_desloca_head_f7():
    cfg = Config()
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    script = ScriptDirectory.from_config(cfg)
    assert "f7g8h9i0j1k2" in set(script.get_heads())
    ids = {rev.revision for rev in script.walk_revisions()}
    assert "g8h9i0j1k2l3" not in ids


def test_f8_nao_cria_api_publica_nem_credencial():
    arquivos = [
        ROOT / "app" / "services" / "conta_multiuser_diagnostico_service.py",
        ROOT / "app" / "services" / "conta_multiuser_bi_service.py",
        ROOT / "app" / "services" / "conta_multiuser_autorizacao_service.py",
        ROOT / "app" / "services" / "conta_multiuser_eventos.py",
    ]
    for path in arquivos:
        src = path.read_text(encoding="utf-8").lower()
        assert "api key" not in src
        assert "oauth" not in src
        assert "rate limit" not in src
        assert "openai" not in src
        assert "gemini" not in src


# --- Privacidade ---


def test_mesma_conta_nao_autoriza_artefato_de_outro_membro(app):
    with app.app_context():
        conta, contratante = _preparar_conta_multiuser("f8-priv-a", qtd=5, email="f8ca@test.com")
        a, b = _adicionar_membros(conta, 2, "f8pa")
        rec = _artefato(a)
        assert record_matches_explicit_scope(rec, _scope(a)) is True
        assert record_matches_explicit_scope(rec, _scope(b)) is False
        assert record_matches_explicit_scope(rec, _scope(contratante)) is False


def test_contratante_nao_herda_documento_por_papel(app):
    with app.app_context():
        conta, contratante = _preparar_conta_multiuser("f8-priv-c", qtd=4, email="f8cc@test.com")
        (membro,) = _adicionar_membros(conta, 1, "f8pc")
        rec = _artefato(membro)
        assert user_eh_contratante_ativo(contratante) is True
        assert record_matches_explicit_scope(rec, _scope(contratante)) is False


def test_admin_global_nao_vira_owner_de_documento(app):
    with app.app_context():
        conta, contratante = _preparar_conta_multiuser("f8-priv-adm", qtd=3, email="f8cadm@test.com")
        admin = _admin(conta)
        rec = _artefato(contratante)
        assert user_eh_admin_plataforma(admin) is True
        assert mesmo_owner_operacional(
            admin,
            usuario_id=int(contratante.id),
            conta_id=int(conta.id),
            franquia_id=int(contratante.franquia_id),
        ) is False
        assert record_matches_explicit_scope(rec, _scope(admin)) is False


def test_membro_nao_vira_contratante(app):
    with app.app_context():
        conta, contratante = _preparar_conta_multiuser("f8-priv-pap", qtd=3, email="f8cpap@test.com")
        (membro,) = _adicionar_membros(conta, 1, "f8pp")
        assert user_eh_membro_ativo(membro) is True
        assert user_eh_contratante_ativo(membro) is False
        assert user_eh_admin_plataforma(contratante) is False
        with pytest.raises(GestaoMultiuserNaoAutorizadaError):
            montar_painel_contratante_por_ator_id(int(membro.id))


def test_download_protegido_recusa_id_conhecido_de_outro_membro(app, monkeypatch, tmp_path):
    from app.cleiton_doc_store import load_authorized_document_record, save_document_record
    from tests.cleiton_doc_fixtures import patch_cleiton_doc_store

    patch_cleiton_doc_store(tmp_path, monkeypatch)
    with app.app_context():
        app.config["SECRET_KEY"] = "test-secret-f8"
        conta, contratante = _preparar_conta_multiuser("f8-dl", qtd=4, email="f8dlc@test.com")
        a, b = _adicionar_membros(conta, 2, "f8dl")
        rec = _artefato(a)
        rec["expires_at"] = (utcnow_naive() + timedelta(hours=2)).isoformat()
        rec["created_at"] = utcnow_naive().isoformat()
        save_document_record(rec)
        _build_client(app)
        with app.test_request_context("/"):
            login_user(b)
            assert load_authorized_document_record(rec["doc_id"], ttl_hours=24) is None
        with app.test_request_context("/"):
            login_user(contratante)
            assert load_authorized_document_record(rec["doc_id"], ttl_hours=24) is None
        with app.test_request_context("/"):
            login_user(a)
            loaded = load_authorized_document_record(rec["doc_id"], ttl_hours=24)
            assert loaded is not None
            assert loaded["usuario_id"] == a.id


def test_roberto_upload_id_nao_cruza_membros(app, monkeypatch, tmp_path):
    import app.roberto_upload_store as store

    monkeypatch.setattr(store, "_base_dir", lambda: str(tmp_path))
    with app.app_context():
        app.config["SECRET_KEY"] = "test-secret-f8"
        conta, contratante = _preparar_conta_multiuser("f8-rob", qtd=4, email="f8robc@test.com")
        a, b = _adicionar_membros(conta, 2, "f8rob")
        _build_client(app)
        with app.test_request_context("/"):
            login_user(a)
            upload_id = store.save_upload_data([{"carrier": "SEGREDO_A"}])
        with app.test_request_context("/"):
            login_user(b)
            assert store.inspect_upload_data(upload_id, ttl_minutes=60).status == store.UPLOAD_SCOPE_MISMATCH
        with app.test_request_context("/"):
            login_user(contratante)
            assert store.inspect_upload_data(upload_id, ttl_minutes=60).status == store.UPLOAD_SCOPE_MISMATCH
        with app.test_request_context("/"):
            login_user(a)
            assert store.inspect_upload_data(upload_id, ttl_minutes=60).status == store.UPLOAD_OK


def test_painel_contratante_nao_lista_artefato_privado(app):
    with app.app_context():
        conta, contratante = _preparar_conta_multiuser("f8-painel", qtd=4, email="f8pain@test.com")
        _adicionar_membros(conta, 1, "f8pn")
        painel = montar_painel_contratante_por_ator_id(int(contratante.id))
        view = painel_para_template(painel)
        blob = json.dumps(view, default=str).lower()
        assert "privado.txt" not in blob
        assert "prepared_context" not in blob
        assert "chat" not in blob
        assert view["usuarios_ativos"] >= 2
        assert "membros" in view


# --- Reconciliação ---


def test_diagnostico_ok_quando_local_igual_stripe(app, monkeypatch):
    with app.app_context():
        conta, _user = _preparar_conta_multiuser("f8-ok", qtd=5, email="f8ok@test.com")
        antes = _consumos(conta.id)
        _mock_stripe(monkeypatch, quantity=5)
        diag = diagnosticar_conta_multiuser(int(conta.id), consultar_stripe=True)
        assert diag.status == STATUS_DIAG_OK
        assert diag.quantity_local == 5
        assert diag.quantity_stripe == 5
        assert diag.mutou_estado_comercial is False
        assert _consumos(conta.id) == antes


def test_diagnostico_divergente_sem_operacao_legitima(app, monkeypatch):
    with app.app_context():
        conta, _user = _preparar_conta_multiuser("f8-div", qtd=5, email="f8div@test.com")
        _mock_stripe(monkeypatch, quantity=9)
        diag = diagnosticar_conta_multiuser(int(conta.id), consultar_stripe=True)
        assert diag.status == STATUS_DIAG_DIVERGENTE
        assert any(a.codigo == "quantity_local_stripe" for a in diag.achados)
        assert diag.mutou_estado_comercial is False
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 5


def test_diagnostico_f6_parcial_nao_e_divergencia_cega(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f8-f6", qtd=5, email="f8f6@test.com")
        _inserir_f6_parcial(
            conta,
            user,
            estado=ContaMultiuserAumentoExcepcional.ESTADO_PAGAMENTO_CONFIRMADO,
            correlation="corr-f6-parcial",
        )
        _mock_stripe(monkeypatch, quantity=8)
        diag = diagnosticar_conta_multiuser(int(conta.id), consultar_stripe=True)
        assert diag.status == STATUS_DIAG_PENDENTE_ESPERADO
        assert diag.status != STATUS_DIAG_DIVERGENTE
        assert any(a.codigo == "f6_parcial" for a in diag.achados)
        assert diag.correlation_id == "corr-f6-parcial"


def test_diagnostico_f6_reconciliacao_necessaria_preservada(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f8-f6r", qtd=5, email="f8f6r@test.com")
        _inserir_f6_parcial(
            conta,
            user,
            estado=ContaMultiuserAumentoExcepcional.ESTADO_RECONCILIACAO_NECESSARIA,
            correlation="corr-f6-rec",
        )
        _mock_stripe(monkeypatch, quantity=8)
        diag = diagnosticar_conta_multiuser(int(conta.id), consultar_stripe=True)
        assert diag.status == STATUS_DIAG_RECONCILIACAO_NECESSARIA
        assert any(a.codigo == "f6_parcial" for a in diag.achados)


def test_reducao_pendente_nao_e_divergencia(app, monkeypatch):
    from app.services.conta_multiuser_reducao_service import solicitar_reducao_quantity

    with app.app_context():
        conta, user = _preparar_conta_multiuser("f8-red", qtd=10, email="f8red@test.com")
        _adicionar_membros(conta, 4, "f8rd")
        _mock_stripe(monkeypatch, quantity=10)
        solicitar_reducao_quantity(
            ator=user, quantity_futura=8, idempotency_key="f8-red-1", commit=True
        )
        diag = diagnosticar_conta_multiuser(int(conta.id), consultar_stripe=True)
        assert diag.status == STATUS_DIAG_PENDENTE_ESPERADO
        assert diag.status != STATUS_DIAG_DIVERGENTE
        assert diag.quantity_futura == 8
        assert diag.quantity_local == 10
        assert diag.quantity_stripe == 10
        assert any(a.codigo == "reducao_pendente" for a in diag.achados)
        assert not any(a.codigo == "quantity_local_stripe" for a in diag.achados)


def test_falha_stripe_inconclusivo_sem_mutacao(app, monkeypatch):
    with app.app_context():
        conta, _user = _preparar_conta_multiuser("f8-stfail", qtd=6, email="f8stf@test.com")
        antes_q = conta.quantidade_assentos_contratados
        antes_c = _consumos(conta.id)
        _mock_stripe(monkeypatch, quantity=6, fail=True)
        diag = diagnosticar_conta_multiuser(int(conta.id), consultar_stripe=True)
        assert diag.status == STATUS_DIAG_INCONCLUSIVO
        assert diag.mutou_estado_comercial is False
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == antes_q
        assert _consumos(conta.id) == antes_c


def test_stripe_na_futura_sem_evidencia_desta_reducao_exige_reconciliacao(app, monkeypatch):
    from app.services.conta_multiuser_reducao_service import solicitar_reducao_quantity

    with app.app_context():
        conta, user = _preparar_conta_multiuser(
            "f8-190", qtd=12, email="f8190@test.com"
        )
        _adicionar_membros(conta, 4, "f8190")
        _mock_stripe(monkeypatch, quantity=12)
        solicitar_reducao_quantity(
            ator=user, quantity_futura=10, idempotency_key="f8-190", commit=True
        )
        _mock_stripe(monkeypatch, quantity=10)
        diag = diagnosticar_conta_multiuser(int(conta.id), consultar_stripe=True)
        assert diag.status == STATUS_DIAG_RECONCILIACAO_NECESSARIA
        assert diag.status != STATUS_DIAG_PENDENTE_ESPERADO
        assert diag.quantity_local == 12
        assert diag.quantity_stripe == 10
        assert diag.quantity_futura == 10
        assert any(
            a.detalhe == "stripe_na_futura_sem_evidencia_desta_reducao"
            for a in diag.achados
        )
        assert diag.mutou_estado_comercial is False
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 12


def test_reducao_pendente_nao_mascara_divergencia_stripe_local(app, monkeypatch):
    from app.services.conta_multiuser_reducao_service import solicitar_reducao_quantity

    with app.app_context():
        conta, user = _preparar_conta_multiuser("f8-reddiv", qtd=10, email="f8reddiv@test.com")
        _adicionar_membros(conta, 4, "f8rdv")
        _mock_stripe(monkeypatch, quantity=10)
        solicitar_reducao_quantity(
            ator=user, quantity_futura=8, idempotency_key="f8-reddiv", commit=True
        )
        _mock_stripe(monkeypatch, quantity=9)
        diag = diagnosticar_conta_multiuser(int(conta.id), consultar_stripe=True)
        assert diag.status == STATUS_DIAG_DIVERGENTE
        assert diag.quantity_local == 10
        assert diag.quantity_stripe == 9
        assert diag.quantity_futura == 8
        assert any(a.codigo == "quantity_local_stripe" for a in diag.achados)
        assert any(a.codigo == "reducao_pendente" for a in diag.achados)
        assert diag.mutou_estado_comercial is False


def test_f6_parcial_com_reducao_pendente_preserva_f6(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f8-f6red", qtd=5, email="f8f6red@test.com")
        _inserir_f6_parcial(
            conta,
            user,
            estado=ContaMultiuserAumentoExcepcional.ESTADO_PAGAMENTO_CONFIRMADO,
            correlation="corr-f6-red",
        )
        db.session.add(
            ContaMultiuserReducaoQuantity(
                conta_id=int(conta.id),
                solicitado_por_user_id=int(user.id),
                quantity_atual_no_pedido=5,
                quantity_futura=4,
                efetivar_em=FIM,
                estado=ContaMultiuserReducaoQuantity.ESTADO_PENDENTE,
                idempotency_key="f8-f6red",
                correlation_id="corr-f6-red-qty",
            )
        )
        db.session.commit()
        _mock_stripe(monkeypatch, quantity=8)
        diag = diagnosticar_conta_multiuser(int(conta.id), consultar_stripe=True)
        assert diag.status == STATUS_DIAG_PENDENTE_ESPERADO
        assert diag.status != STATUS_DIAG_DIVERGENTE
        assert any(a.codigo == "f6_parcial" for a in diag.achados)
        assert diag.correlation_id == "corr-f6-red"
        assert diag.mutou_estado_comercial is False


def test_stripe_indisponivel_com_reducao_pendente_inconclusivo(app, monkeypatch):
    from app.services.conta_multiuser_reducao_service import solicitar_reducao_quantity

    with app.app_context():
        conta, user = _preparar_conta_multiuser("f8-stred", qtd=10, email="f8stred@test.com")
        _adicionar_membros(conta, 4, "f8str")
        _mock_stripe(monkeypatch, quantity=10)
        solicitar_reducao_quantity(
            ator=user, quantity_futura=8, idempotency_key="f8-stred", commit=True
        )
        antes_q = conta.quantidade_assentos_contratados
        antes_c = _consumos(conta.id)
        _mock_stripe(monkeypatch, quantity=10, fail=True)
        diag = diagnosticar_conta_multiuser(int(conta.id), consultar_stripe=True)
        assert diag.status == STATUS_DIAG_INCONCLUSIVO
        assert diag.status != STATUS_DIAG_PENDENTE_ESPERADO
        assert diag.mutou_estado_comercial is False
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == antes_q
        assert _consumos(conta.id) == antes_c


def test_ciclo_alinhado_ok_e_franquia_fora_detectada(app, monkeypatch):
    with app.app_context():
        conta, _user = _preparar_conta_multiuser("f8-ciclo", qtd=5, email="f8cic@test.com")
        _mock_stripe(monkeypatch, quantity=5)
        diag_ok = diagnosticar_conta_multiuser(int(conta.id), consultar_stripe=True)
        assert not any(a.codigo == "ciclo_conta_franquia" for a in diag_ok.achados)
        fr = Franquia.query.filter_by(conta_id=conta.id).order_by(Franquia.id.desc()).first()
        fr.inicio_ciclo = datetime(2025, 1, 1, 0, 0, 0)
        fr.fim_ciclo = datetime(2025, 2, 1, 0, 0, 0)
        db.session.add(fr)
        db.session.commit()
        consumo_antes = str(fr.consumo_acumulado)
        diag = diagnosticar_conta_multiuser(int(conta.id), consultar_stripe=True)
        assert any(a.codigo == "ciclo_conta_franquia" for a in diag.achados)
        db.session.refresh(fr)
        assert str(fr.consumo_acumulado) == consumo_antes
        assert fr.inicio_ciclo == datetime(2025, 1, 1, 0, 0, 0)


def test_membership_incoerente_detectado_sem_reset(app, monkeypatch):
    with app.app_context():
        conta, _user = _preparar_conta_multiuser("f8-mem", qtd=4, email="f8mem@test.com")
        (membro,) = _adicionar_membros(conta, 1, "f8mm")
        outra, fr_o = seed_conta_franquia_cliente(slug="f8-mem-x")
        membro.conta_id = outra.id
        membro.franquia_id = fr_o.id
        db.session.add(membro)
        db.session.commit()
        _mock_stripe(monkeypatch, quantity=4)
        diag = diagnosticar_conta_multiuser(int(conta.id), consultar_stripe=True)
        assert any(a.codigo == "membership" for a in diag.achados)
        assert diag.mutou_estado_comercial is False


def test_contratante_unico_e_ocupacao_dentro_da_capacity(app, monkeypatch):
    with app.app_context():
        conta, _user = _preparar_conta_multiuser("f8-cap", qtd=5, email="f8cap@test.com")
        _adicionar_membros(conta, 2, "f8cp")
        _mock_stripe(monkeypatch, quantity=5)
        diag = diagnosticar_conta_multiuser(int(conta.id), consultar_stripe=True)
        assert diag.memberships_ativos == 3
        assert not any(a.codigo == "contratantes_ativos" for a in diag.achados)
        assert not any(a.codigo == "ocupacao_vs_capacity" for a in diag.achados)


def test_diagnostico_nao_dispara_reparo(app, monkeypatch):
    with app.app_context():
        conta, _user = _preparar_conta_multiuser("f8-rep", qtd=5, email="f8rep@test.com")
        _mock_stripe(monkeypatch, quantity=9)
        diagnosticar_conta_multiuser(int(conta.id), consultar_stripe=True)
        conhecidos = {item["codigo"] for item in reparos_seguros_conhecidos()}
        assert "alinhar_franquias_sem_ciclo_ao_canonico" in conhecidos
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 5


# --- BI / contratos ---


def test_bi_conta_sem_duplicar_e_sem_payload_operacional(app, monkeypatch):
    with app.app_context():
        conta, user = _preparar_conta_multiuser("f8-bi", qtd=7, email="f8bi@test.com")
        _adicionar_membros(conta, 2, "f8bi")
        _mock_stripe(monkeypatch, quantity=7)
        diagnosticar_conta_multiuser(int(conta.id), consultar_stripe=True)
        saude = projetar_saude_multiuser_admin()
        view = saude.para_template()
        assert saude.contas_ativas >= 1
        assert saude.memberships_ativos >= 3
        assert saude.quantidade_contratada_total >= 7
        assert bi_expoe_conteudo_operacional(view) is False
        replay = projetar_saude_multiuser_admin()
        assert replay.memberships_ativos == saude.memberships_ativos
        assert replay.contas_ativas == saude.contas_ativas


def test_bi_admin_superfície_existente(app, monkeypatch):
    with app.app_context():
        conta, _user = _preparar_conta_multiuser("f8-admbi", qtd=5, email="f8admbi@test.com")
        admin = _admin(conta, email="adm-f8-bi@test.com")
        _mock_stripe(monkeypatch, quantity=5)
        client = _build_client(app)
        _login(client, admin)
        resp = client.get("/admin/controle-usuarios")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Saúde Multiuser" in html
        assert "prepared_context" not in html
        assert "SEGREDO_A" not in html


def _itens_bi_conta(saude, conta_id: int):
    return [c for c in saude.contas_divergentes if int(c.conta_id) == int(conta_id)]


def test_bi_inclui_divergencia_detectada_pelo_diagnostico(app, monkeypatch):
    with app.app_context():
        conta, _user = _preparar_conta_multiuser("f8-bidiv", qtd=10, email="f8bidiv@test.com")
        _mock_stripe(monkeypatch, quantity=13)
        diag = diagnosticar_conta_multiuser(int(conta.id), consultar_stripe=True)
        assert diag.status == STATUS_DIAG_DIVERGENTE
        saude = projetar_saude_multiuser_admin()
        itens = _itens_bi_conta(saude, conta.id)
        assert saude.divergencias_detectadas == 1
        assert len(itens) == 1
        assert itens[0].status == STATUS_DIAG_DIVERGENTE
        assert int(itens[0].conta_id) == int(conta.id)
        replay = projetar_saude_multiuser_admin()
        assert replay.divergencias_detectadas == saude.divergencias_detectadas
        assert len(replay.contas_divergentes) == len(saude.contas_divergentes)


def test_bi_reducao_legitima_nao_infla_divergencia(app, monkeypatch):
    from app.services.conta_multiuser_reducao_service import solicitar_reducao_quantity

    with app.app_context():
        conta, user = _preparar_conta_multiuser("f8-bired", qtd=10, email="f8bired@test.com")
        _adicionar_membros(conta, 4, "f8brd")
        _mock_stripe(monkeypatch, quantity=10)
        solicitar_reducao_quantity(
            ator=user, quantity_futura=8, idempotency_key="f8-bired", commit=True
        )
        diag = diagnosticar_conta_multiuser(int(conta.id), consultar_stripe=True)
        assert diag.status == STATUS_DIAG_PENDENTE_ESPERADO
        saude = projetar_saude_multiuser_admin()
        assert saude.reducoes_pendentes >= 1
        assert saude.divergencias_detectadas == 0
        assert _itens_bi_conta(saude, conta.id) == []


def test_bi_divergencia_com_reducao_pendente_permanece(app, monkeypatch):
    from app.services.conta_multiuser_reducao_service import solicitar_reducao_quantity

    with app.app_context():
        conta, user = _preparar_conta_multiuser("f8-bidr", qtd=10, email="f8bidr@test.com")
        _adicionar_membros(conta, 4, "f8bdr")
        _mock_stripe(monkeypatch, quantity=10)
        solicitar_reducao_quantity(
            ator=user, quantity_futura=8, idempotency_key="f8-bidr", commit=True
        )
        _mock_stripe(monkeypatch, quantity=9)
        diag = diagnosticar_conta_multiuser(int(conta.id), consultar_stripe=True)
        assert diag.status == STATUS_DIAG_DIVERGENTE
        saude = projetar_saude_multiuser_admin()
        itens = _itens_bi_conta(saude, conta.id)
        assert saude.divergencias_detectadas == 1
        assert len(itens) == 1
        assert itens[0].status == STATUS_DIAG_DIVERGENTE
        assert saude.reducoes_pendentes >= 1


def test_bi_stripe_inconclusivo_nao_vira_divergencia_artificial(app, monkeypatch):
    with app.app_context():
        conta, _user = _preparar_conta_multiuser("f8-biinc", qtd=10, email="f8biinc@test.com")
        _mock_stripe(monkeypatch, quantity=10, fail=True)
        diag = diagnosticar_conta_multiuser(int(conta.id), consultar_stripe=True)
        assert diag.status == STATUS_DIAG_INCONCLUSIVO
        saude = projetar_saude_multiuser_admin()
        itens = _itens_bi_conta(saude, conta.id)
        assert all(item.status == STATUS_DIAG_INCONCLUSIVO for item in itens)
        assert all(item.status != STATUS_DIAG_DIVERGENTE for item in itens)


def test_service_sem_request_flask_usa_actor_id(app):
    with app.app_context():
        _conta, contratante = _preparar_conta_multiuser("f8-api", qtd=4, email="f8api@test.com")
        painel = montar_painel_contratante_por_ator_id(int(contratante.id))
        assert painel.conta_id == contratante.conta_id
        assert painel.assentos_contratados == 4


def test_matriz_eventos_cobre_f1_a_f7():
    nomes = {e.evento for e in listar_eventos_multiuser_v1()}
    assert "contratacao_ciclo_aplicado" in nomes
    assert "convite_criado" in nomes
    assert "convite_aceito" in nomes
    assert "aumento_automatico_aprovado" in nomes
    assert "aumento_excepcional_decidido" in nomes
    assert "pagamento_extraordinario_confirmado" in nomes
    assert "membership_revogado" in nomes
    assert "reducao_quantity_solicitada" in nomes
    assert "titularidade_solicitada" in nomes
    assert "divergencia_detectada" in nomes


def test_planos_individuais_nao_viram_multiuser(app):
    with app.app_context():
        seed_sistema_interno()
        for plano, slug in (("free", "f8-free"), ("starter", "f8-st"), ("pro", "f8-pro"), ("avulso", "f8-av")):
            conta, fr = seed_conta_franquia_cliente(slug=slug)
            user = seed_usuario(fr.id, conta.id, email=f"{slug}@test.com", categoria=plano)
            assert conta.multiuser_ativa is False
            diag = diagnosticar_conta_multiuser(int(conta.id), consultar_stripe=False)
            db.session.refresh(conta)
            db.session.refresh(user)
            assert conta.multiuser_ativa is False
            assert user.categoria == plano
            assert diag.mutou_estado_comercial is False


def test_revogado_perde_gestao_e_nao_leve_artefato(app, monkeypatch):
    from app.services.conta_multiuser_revogacao_service import revogar_membro

    with app.app_context():
        conta, contratante = _preparar_conta_multiuser("f8-rev", qtd=4, email="f8revc@test.com")
        (membro,) = _adicionar_membros(conta, 1, "f8rv")
        rec = _artefato(membro)
        _mock_stripe(monkeypatch, quantity=4)
        revogar_membro(ator=contratante, alvo_user_id=int(membro.id), commit=True)
        db.session.refresh(membro)
        assert membro.conta_id != conta.id
        assert record_matches_explicit_scope(rec, _scope(membro)) is False
        with pytest.raises(GestaoMultiuserNaoAutorizadaError):
            montar_painel_contratante_por_ator_id(int(membro.id))


def test_titularidade_nao_transfere_documento_privado(app, monkeypatch):
    from app.services.conta_multiuser_titularidade_service import (
        decidir_titularidade,
        solicitar_titularidade,
    )

    with app.app_context():
        conta, titular = _preparar_conta_multiuser("f8-tit", qtd=4, email="f8tit@test.com")
        (candidato,) = _adicionar_membros(conta, 1, "f8tt")
        admin = _admin(conta, email="adm-f8-tit@test.com")
        rec = _artefato(titular)
        _mock_stripe(monkeypatch, quantity=4)
        sol = solicitar_titularidade(
            admin=admin,
            conta_id=int(conta.id),
            candidato_id=int(candidato.id),
            motivo="teste f8",
            idempotency_key="f8-tit-1",
        )
        decidir_titularidade(
            admin=admin,
            solicitacao_id=int(sol.solicitacao_id),
            decisao="aprovada",
            versao=sol.versao,
        )
        db.session.refresh(titular)
        db.session.refresh(candidato)
        assert user_eh_contratante_ativo(candidato) is True
        assert user_eh_membro_ativo(titular) is True
        assert record_matches_explicit_scope(rec, _scope(candidato)) is False
        assert record_matches_explicit_scope(rec, _scope(titular)) is True


def test_reducao_nao_altera_ownership_nem_consumo(app, monkeypatch):
    from app.services.conta_multiuser_reducao_service import solicitar_reducao_quantity

    with app.app_context():
        conta, user = _preparar_conta_multiuser("f8-red2", qtd=8, email="f8red2@test.com")
        membros = _adicionar_membros(conta, 2, "f8r2")
        ids_antes = [(m.id, m.conta_id, m.franquia_id) for m in membros]
        consumo_antes = _consumos(conta.id)
        _mock_stripe(monkeypatch, quantity=8)
        solicitar_reducao_quantity(
            ator=user, quantity_futura=6, idempotency_key="f8-red2", commit=True
        )
        for uid, cid, fid in ids_antes:
            atual = db.session.get(User, uid)
            assert atual.conta_id == cid
            assert atual.franquia_id == fid
        assert _consumos(conta.id) == consumo_antes
