"""Testes incrementais da Fase 4 — convites, aceite e ocupação de membros Multiuser."""
from __future__ import annotations

import inspect
import io
import json
import logging
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from flask import Flask, session
from flask_login import login_user
from sqlalchemy.exc import IntegrityError

from app.extensions import db, login_manager
from app.infra import get_user_by_id
from app.models import (
    Conta,
    ContaMonetizacaoVinculo,
    ContaMultiuserConvite,
    ContaVinculoOrganizacional,
    Franquia,
    MultiuserFranquiaCodigo,
    ProcessingEvent,
    User,
    utcnow_naive,
)
from app.services import plano_service
from app.services.conta_multiuser_capacidade_service import ocupar_assento, snapshot_capacidade
from app.services.conta_multiuser_convite_service import (
    CONVITE_TTL_SEGUNDOS,
    aceitar_convite,
    conta_possui_beneficio_pago_vigente,
    criar_convite,
    decodificar_token_convite,
    emitir_token_convite,
    gerar_csrf_token_aceite,
    gerar_csrf_token_gestao_convite,
    montar_contexto_landing,
    reenviar_convite,
)
from app.services.conta_multiuser_errors import (
    CapacidadeEsgotadaError,
    ConviteContratanteProprioError,
    ConviteContratoIncompativelError,
    ConviteEmailDivergenteError,
    ConviteExpiradoError,
    ConviteInvalidoError,
    ConviteJaVinculadoError,
    ConviteNaoAutorizadoError,
    DivergenciaImpeditivaError,
    VinculoInconsistenteError,
)
from app.services.user_plan_control_service import atribuir_plano_para_usuario
from app.conta_multiuser_convite_routes import convite_bp
from tests.conftest import seed_conta_franquia_cliente, seed_sistema_interno, seed_usuario

ROOT = Path(__file__).resolve().parents[1]


def _preparar_planos_admin(*, limite="200"):
    if Conta.query.filter_by(slug=Conta.SLUG_SISTEMA).first() is None:
        seed_sistema_interno()
    plano_service.atualizar_parametros_plano_admin(
        plano_codigo="multiuser",
        valor_plano_raw="10.00",
        franquia_limite_total_raw=limite,
        quantidade_minima_raw="5",
    )
    for codigo in ("free", "starter", "pro", "avulso"):
        plano_service.atualizar_parametros_plano_admin(
            plano_codigo=codigo,
            valor_plano_raw="10.00",
            franquia_limite_total_raw=limite,
        )


def _secret(app: Flask) -> None:
    app.config["SECRET_KEY"] = "test-secret-f4"
    app.config["TESTING"] = True
    app.config["SERVER_NAME"] = "localhost"


def _preparar_conta_multiuser(slug: str, *, qtd: int, email: str):
    from flask import current_app

    current_app.config["SECRET_KEY"] = current_app.config.get("SECRET_KEY") or "test-secret-f4"
    current_app.config["TESTING"] = True
    _preparar_planos_admin()
    conta, franquia = seed_conta_franquia_cliente(slug=slug)
    from tests.conftest import preencher_dados_empresariais_minimos_teste

    preencher_dados_empresariais_minimos_teste(conta, slug)
    user = seed_usuario(franquia.id, conta.id, email=email, categoria="free")
    atribuir_plano_para_usuario(
        email=user.email,
        plano_raw="multiuser",
        quantidade_franquias_raw=str(qtd),
    )
    db.session.refresh(conta)
    db.session.refresh(user)
    return conta, user


def _franquia_livre(conta: Conta, user: User) -> Franquia:
    ocupadas = {
        int(v.franquia_id)
        for v in ContaVinculoOrganizacional.query.filter_by(conta_id=conta.id, estado="ativo")
    }
    livres = [
        fr
        for fr in Franquia.query.filter_by(conta_id=conta.id).order_by(Franquia.id.asc())
        if int(fr.id) not in ocupadas
    ]
    assert livres, "esperado assento livre"
    return livres[0]


def _login(client, user: User) -> None:
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def _build_client(app: Flask):
    _secret(app)
    app.template_folder = str(ROOT / "app" / "templates")
    if "multiuser_convite" not in app.blueprints:
        app.register_blueprint(convite_bp)
    if "login" not in app.view_functions:
        @app.route("/login")
        def login():  # noqa: ARG001
            return "login"

    @app.context_processor
    def _inject_has_endpoint():
        return {
            "has_endpoint": lambda endpoint_name: endpoint_name in app.view_functions,
            "privacy_marketing_allowed": False,
            "user_is_admin": lambda _user: False,
        }

    login_manager.init_app(app)

    @login_manager.user_loader
    def _load_user(user_id):  # noqa: ANN001
        return get_user_by_id(user_id)

    return app.test_client()


def _csrf_gestao(user: User) -> str:
    return gerar_csrf_token_gestao_convite(user.id)


def _monetizacao_paga(conta_id: int, plano: str) -> ContaMonetizacaoVinculo:
    row = ContaMonetizacaoVinculo(
        conta_id=int(conta_id),
        provider="stripe",
        customer_id=f"cus_{plano}_{conta_id}",
        subscription_id=f"sub_{plano}_{conta_id}",
        price_id="price_teste",
        plano_interno=plano,
        status_contratual_externo="active",
        ativo=True,
    )
    db.session.add(row)
    db.session.commit()
    return row


# --- Migration / model ---


def test_migration_c4d5e6f7a8b9_na_chain():
    cfg = Config()
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    script = ScriptDirectory.from_config(cfg)
    rev = script.get_revision("c4d5e6f7a8b9")
    assert rev is not None
    assert rev.down_revision == "b3c4d5e6f7a8"
    heads = set(script.get_heads())
    # F5 sucede F4 na cadeia linear; F4 permanece ancestral, não necessariamente head.
    assert "c4d5e6f7a8b9" in heads or script.get_revision("d5e6f7a8b9c0") is not None
    if "d5e6f7a8b9c0" in heads:
        assert script.get_revision("d5e6f7a8b9c0").down_revision == "c4d5e6f7a8b9"
    source = (
        ROOT / "migrations" / "versions" / "c4d5e6f7a8b9_fase4_multiuser_convites.py"
    ).read_text(encoding="utf-8")
    assert "uq_conta_convite_franquia_pendente" in source
    assert "uq_conta_convite_conta_email_pendente" in source
    assert "conta_multiuser_convite" in source
    deploy = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
    assert "c4d5e6f7a8b9" in deploy
    assert "a2b3c4d5e6f7" in deploy
    assert "b3c4d5e6f7a8" in deploy


def test_migration_upgrade_downgrade_reupgrade(tmp_path):
    import importlib.util

    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine, inspect

    db_path = tmp_path / "f4_convite.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    mig_path = ROOT / "migrations" / "versions" / "c4d5e6f7a8b9_fase4_multiuser_convites.py"
    spec = importlib.util.spec_from_file_location("f4_convite_mig", mig_path)
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
    assert "conta_multiuser_convite" in insp.get_table_names()
    cols = {c["name"] for c in insp.get_columns("conta_multiuser_convite")}
    for required in (
        "conta_id",
        "franquia_id",
        "criado_por_user_id",
        "email_destino",
        "estado",
        "expires_at",
        "accepted_at",
        "accepted_user_id",
    ):
        assert required in cols
    idx = {ix["name"] for ix in insp.get_indexes("conta_multiuser_convite")}
    assert "uq_conta_convite_franquia_pendente" in idx
    assert "uq_conta_convite_conta_email_pendente" in idx
    _run(mig.downgrade)
    insp = inspect(engine)
    assert "conta_multiuser_convite" not in insp.get_table_names()
    _run(mig.upgrade)
    insp = inspect(engine)
    assert "conta_multiuser_convite" in insp.get_table_names()


def test_model_convite_estados_e_fks():
    cols = {c.name for c in ContaMultiuserConvite.__table__.columns}
    assert ContaMultiuserConvite.ESTADO_PENDENTE == "pendente"
    assert ContaMultiuserConvite.ESTADO_ACEITO == "aceito"
    assert ContaMultiuserConvite.ESTADO_EXPIRADO == "expirado"
    assert "email_destino" in cols
    assert "expires_at" in cols
    assert "accepted_user_id" in cols


def test_multiuser_franquia_codigo_nao_e_usado_pelo_servico_f4():
    import app.services.conta_multiuser_convite_service as svc

    src = inspect.getsource(svc)
    assert "from app.models import" in src
    assert "MultiuserFranquiaCodigo," not in src.split("from app.models import", 1)[-1].split(")", 1)[0]
    assert "query.filter_by(codigo" not in src


# --- Autorização / conta da sessão ---


def test_contratante_cria_convite(app):
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-auth-ok", qtd=3, email="f4.ok@test.com"
        )
        out = criar_convite(
            ator=contratante,
            email_destino="convidado.ok@test.com",
            conta_id_payload=999999,
            enviar=False,
        )
        convite = db.session.get(ContaMultiuserConvite, out.convite_id)
        assert convite is not None
        assert convite.conta_id == conta.id
        assert convite.estado == "pendente"
        assert convite.email_destino == "convidado.ok@test.com"
        assert User.query.filter_by(email="convidado.ok@test.com").first() is None
        assert ContaVinculoOrganizacional.query.filter_by(
            conta_id=conta.id, papel="membro", estado="ativo"
        ).count() == 0


def test_membro_nao_cria_convite(app):
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-auth-mem", qtd=3, email="f4.mem.c@test.com"
        )
        convite = criar_convite(
            ator=contratante, email_destino="f4.mem.alvo@test.com", enviar=False
        )
        livre = [
            fr
            for fr in Franquia.query.filter_by(conta_id=conta.id).order_by(Franquia.id.asc())
            if int(fr.id) != int(convite.franquia_id)
            and int(fr.id) != int(contratante.franquia_id)
        ][0]
        membro = seed_usuario(livre.id, conta.id, email="f4.mem@test.com", categoria="multiuser")
        from app.services.conta_multiuser_capacidade_service import ocupar_assento

        ocupar_assento(
            conta_id=conta.id,
            user_id=membro.id,
            franquia_id=livre.id,
            papel="membro",
        )
        with pytest.raises(ConviteNaoAutorizadoError):
            criar_convite(
                ator=db.session.get(User, membro.id),
                email_destino="outro@test.com",
                enviar=False,
            )


def test_user_outra_conta_nao_cria_na_conta_alheia(app):
    with app.app_context():
        _secret(app)
        conta_a, contratante_a = _preparar_conta_multiuser(
            "f4-auth-a", qtd=3, email="f4.a@test.com"
        )
        _conta_b, contratante_b = _preparar_conta_multiuser(
            "f4-auth-b", qtd=3, email="f4.b@test.com"
        )
        out = criar_convite(
            ator=contratante_b,
            email_destino="x@test.com",
            conta_id_payload=conta_a.id,
            enviar=False,
        )
        convite = db.session.get(ContaMultiuserConvite, out.convite_id)
        assert convite.conta_id == contratante_b.conta_id
        assert convite.conta_id != conta_a.id


# --- Reserva / capacidade ---


def test_convite_reserva_exatamente_uma_franquia_sem_ocupar(app):
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-res", qtd=3, email="f4.res@test.com"
        )
        livre = _franquia_livre(conta, contratante)
        consumo = livre.consumo_acumulado
        inicio = livre.inicio_ciclo
        fim = livre.fim_ciclo
        out = criar_convite(
            ator=contratante, email_destino="f4.res.alvo@test.com", enviar=False
        )
        convite = db.session.get(ContaMultiuserConvite, out.convite_id)
        assert convite.franquia_id == livre.id or db.session.get(Franquia, convite.franquia_id).conta_id == conta.id
        snap = snapshot_capacidade(conta.id)
        assert snap.vinculos_ativos == 1
        rec = db.session.get(Franquia, convite.franquia_id)
        assert rec.consumo_acumulado == consumo or rec.id != livre.id
        if rec.id == livre.id:
            assert rec.consumo_acumulado == consumo
            assert rec.inicio_ciclo == inicio
            assert rec.fim_ciclo == fim
        assert ContaVinculoOrganizacional.query.filter_by(
            franquia_id=convite.franquia_id, estado="ativo"
        ).first() is None


def test_sem_assento_livre_bloqueia(app):
    with app.app_context():
        _secret(app)
        _conta, contratante = _preparar_conta_multiuser(
            "f4-cap0", qtd=1, email="f4.cap0@test.com"
        )
        with pytest.raises(CapacidadeEsgotadaError):
            criar_convite(
                ator=contratante, email_destino="noloose@test.com", enviar=False
            )


def test_ultimo_assento_somente_um_convite(app):
    with app.app_context():
        _secret(app)
        _conta, contratante = _preparar_conta_multiuser(
            "f4-last", qtd=2, email="f4.last@test.com"
        )
        criar_convite(ator=contratante, email_destino="a.last@test.com", enviar=False)
        with pytest.raises(CapacidadeEsgotadaError):
            criar_convite(ator=contratante, email_destino="b.last@test.com", enviar=False)
        pendentes = ContaMultiuserConvite.query.filter_by(
            conta_id=contratante.conta_id, estado="pendente"
        ).count()
        assert pendentes == 1


def test_unique_pendente_por_franquia(app):
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-uq-fr", qtd=3, email="f4.uqfr@test.com"
        )
        out = criar_convite(
            ator=contratante, email_destino="uq1@test.com", enviar=False
        )
        agora = utcnow_naive() + timedelta(hours=1)
        dup = ContaMultiuserConvite(
            conta_id=conta.id,
            franquia_id=out.franquia_id,
            criado_por_user_id=contratante.id,
            email_destino="uq2@test.com",
            estado="pendente",
            expires_at=agora,
        )
        db.session.add(dup)
        with pytest.raises(IntegrityError):
            db.session.flush()
        db.session.rollback()


def test_mesmo_email_nao_gera_dois_pendentes(app):
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-email", qtd=5, email="f4.email.c@test.com"
        )
        a = criar_convite(
            ator=contratante, email_destino="Mesmo.Email@test.com", enviar=False
        )
        b = criar_convite(
            ator=contratante, email_destino="mesmo.email@test.com", enviar=False
        )
        assert a.convite_id == b.convite_id
        assert b.reutilizado is True
        assert (
            ContaMultiuserConvite.query.filter_by(
                conta_id=conta.id, estado="pendente"
            ).count()
            == 1
        )


def test_expirado_libera_reserva(app):
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-exp", qtd=2, email="f4.exp.c@test.com"
        )
        out = criar_convite(
            ator=contratante, email_destino="exp1@test.com", enviar=False
        )
        convite = db.session.get(ContaMultiuserConvite, out.convite_id)
        convite.expires_at = utcnow_naive() - timedelta(minutes=1)
        db.session.add(convite)
        db.session.commit()
        novo = criar_convite(
            ator=contratante, email_destino="exp2@test.com", enviar=False
        )
        antigo = db.session.get(ContaMultiuserConvite, out.convite_id)
        assert antigo.estado == "expirado"
        assert novo.convite_id != out.convite_id
        assert novo.franquia_id == out.franquia_id


# --- Token ---


def test_token_assinado_payload_minimo(app):
    with app.app_context():
        _secret(app)
        _conta, contratante = _preparar_conta_multiuser(
            "f4-tok", qtd=3, email="f4.tok@test.com"
        )
        out = criar_convite(
            ator=contratante, email_destino="tok@test.com", enviar=False
        )
        assert CONVITE_TTL_SEGUNDOS == 3600
        assert decodificar_token_convite(out.token) == out.convite_id


def test_token_adulterado(app):
    with app.app_context():
        _secret(app)
        _conta, contratante = _preparar_conta_multiuser(
            "f4-tok-bad", qtd=3, email="f4.tokbad@test.com"
        )
        out = criar_convite(
            ator=contratante, email_destino="tokbad@test.com", enviar=False
        )
        with pytest.raises(ConviteInvalidoError):
            decodificar_token_convite(out.token + "x")


def test_token_expirado_por_assinatura(app, monkeypatch):
    with app.app_context():
        _secret(app)
        _conta, contratante = _preparar_conta_multiuser(
            "f4-tok-ttl", qtd=3, email="f4.tokttl@test.com"
        )
        out = criar_convite(
            ator=contratante, email_destino="tokttl@test.com", enviar=False
        )
        from itsdangerous import SignatureExpired
        from app.services import conta_multiuser_convite_service as svc

        class _Boom:
            def loads(self, *args, **kwargs):  # noqa: ANN001, ARG002
                raise SignatureExpired("expired")

        monkeypatch.setattr(svc, "_token_serializer", lambda: _Boom())
        with pytest.raises(ConviteExpiradoError):
            decodificar_token_convite(out.token)


def test_db_vence_mesmo_com_token_valido(app):
    with app.app_context():
        _secret(app)
        _conta, contratante = _preparar_conta_multiuser(
            "f4-db-ttl", qtd=3, email="f4.dbttl@test.com"
        )
        out = criar_convite(
            ator=contratante, email_destino="dbttl@test.com", enviar=False
        )
        convite = db.session.get(ContaMultiuserConvite, out.convite_id)
        convite.expires_at = utcnow_naive() - timedelta(seconds=5)
        db.session.add(convite)
        db.session.commit()
        alvo = seed_usuario(
            contratante.franquia_id,
            contratante.conta_id,
            email="dbttl@test.com",
            categoria="free",
        )
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        conta_antes = alvo.conta_id
        franquia_antes = alvo.franquia_id
        with pytest.raises(ConviteExpiradoError):
            aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.refresh(alvo)
        assert alvo.conta_id == conta_antes
        assert alvo.franquia_id == franquia_antes
        assert ContaVinculoOrganizacional.query.filter_by(
            user_id=alvo.id, estado="ativo"
        ).first() is None


def test_token_convite_inexistente(app):
    with app.app_context():
        _secret(app)
        seed_sistema_interno()
        fake = emitir_token_convite(999999)
        with pytest.raises(ConviteInvalidoError):
            montar_contexto_landing(fake, None)


# --- GET / POST / CSRF ---


def test_get_nao_efetiva_vinculo(app):
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-get", qtd=3, email="f4.get.c@test.com"
        )
        out = criar_convite(
            ator=contratante, email_destino="f4.get@test.com", enviar=False
        )
        client = _build_client(app)
        resp = client.get(f"/convite/{out.token}")
        assert resp.status_code == 200
        assert b"Aceitar convite" not in resp.data or b"Entrar" in resp.data
        assert ContaVinculoOrganizacional.query.filter_by(
            conta_id=conta.id, papel="membro", estado="ativo"
        ).count() == 0


def test_post_aceite_exige_csrf(app):
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-csrf", qtd=3, email="f4.csrf.c@test.com"
        )
        out = criar_convite(
            ator=contratante, email_destino="f4.csrf@test.com", enviar=False
        )
        origem, fr_origem = seed_conta_franquia_cliente(slug="f4-csrf-orig")
        convidado = seed_usuario(
            fr_origem.id, origem.id, email="f4.csrf@test.com", categoria="free"
        )
        client = _build_client(app)
        _login(client, convidado)
        resp = client.post(f"/convite/{out.token}/aceitar", data={})
        assert resp.status_code in (302, 200, 400)
        assert ContaVinculoOrganizacional.query.filter_by(
            conta_id=conta.id, papel="membro", estado="ativo"
        ).count() == 0


def test_email_mockado_contem_token_e_validade(app, monkeypatch):
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-mail", qtd=3, email="f4.mail.c@test.com"
        )
        captured = {}

        def _fake_send(**kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(
            "app.services.conta_multiuser_convite_service.send_email", _fake_send
        )
        out = criar_convite(
            ator=contratante,
            email_destino="f4.mail@test.com",
            build_invite_url=lambda t: f"https://example.test/convite/{t}",
            enviar=True,
        )
        assert captured["to_email"] == "f4.mail@test.com"
        assert out.token in captured["html"]
        assert "1 hora" in captured["text"]
        corpo = captured["html"] + captured["text"]
        assert "cnpj" not in corpo.lower()
        assert "quantity" not in captured["html"].lower()
        assert "stripe" not in captured["html"].lower()


# --- Aceite: novo, free, bloqueios ---


def test_usuario_novo_cadastro_sem_duplicar(app):
    from app.auth_services import register_user

    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-new", qtd=3, email="f4.new.c@test.com"
        )
        out = criar_convite(
            ator=contratante, email_destino="f4.new@test.com", enviar=False
        )
        user, err = register_user(
            "Novo Membro",
            "f4.new@test.com",
            "senha-segura-1",
            job_role="analista",
            usage_purpose="trabalho",
            accept_terms=True,
        )
        assert err is None
        assert user is not None
        assert User.query.filter(User.email == "f4.new@test.com").count() == 1
        user2, err2 = register_user(
            "Clone", "f4.new@test.com", "senha-segura-2", accept_terms=True
        )
        assert user2 is None
        csrf = gerar_csrf_token_aceite(user.id, out.convite_id)
        aceitar_convite(user=user, token=out.token, csrf_token=csrf)
        vinculo = ContaVinculoOrganizacional.query.filter_by(
            user_id=user.id, estado="ativo"
        ).one()
        assert vinculo.conta_id == conta.id
        assert vinculo.papel == "membro"
        assert vinculo.franquia_id == out.franquia_id


def test_existing_free_so_muda_apos_aceite(app):
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-free", qtd=3, email="f4.free.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4-free-orig")
        livre = _franquia_livre(conta, contratante)
        livre.consumo_acumulado = Decimal("12.5")
        db.session.add(livre)
        db.session.commit()
        out = criar_convite(
            ator=contratante, email_destino="f4.free@test.com", enviar=False
        )
        alvo = seed_usuario(fr_o.id, origem.id, email="f4.free@test.com", categoria="free")
        origem_id = origem.id
        fr_origem_id = fr_o.id
        consumo_antes = db.session.get(Franquia, out.franquia_id).consumo_acumulado
        inicio_antes = db.session.get(Franquia, out.franquia_id).inicio_ciclo
        fim_antes = db.session.get(Franquia, out.franquia_id).fim_ciclo

        montar_contexto_landing(out.token, alvo)
        db.session.refresh(alvo)
        assert alvo.conta_id == origem_id
        assert alvo.franquia_id == fr_origem_id

        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        resultado = aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.refresh(alvo)
        assert resultado.papel == "membro"
        assert resultado.conta_id == conta.id
        assert resultado.franquia_id == out.franquia_id
        assert alvo.conta_id == conta.id
        assert alvo.franquia_id == out.franquia_id
        assert alvo.categoria == "multiuser"
        rec = db.session.get(Franquia, out.franquia_id)
        assert rec.consumo_acumulado == consumo_antes
        assert rec.inicio_ciclo == inicio_antes
        assert rec.fim_ciclo == fim_antes
        convite = db.session.get(ContaMultiuserConvite, out.convite_id)
        assert convite.estado == "aceito"
        assert convite.accepted_user_id == alvo.id


def test_starter_ativo_bloqueia(app):
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-st", qtd=3, email="f4.st.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4-st-orig")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4.st@test.com", categoria="starter")
        _monetizacao_paga(origem.id, "starter")
        out = criar_convite(
            ator=contratante, email_destino="f4.st@test.com", enviar=False
        )
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        with pytest.raises(ConviteContratoIncompativelError):
            aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.refresh(alvo)
        assert alvo.conta_id == origem.id
        assert alvo.franquia_id == fr_o.id
        assert ContaVinculoOrganizacional.query.filter_by(
            user_id=alvo.id, estado="ativo"
        ).first() is None


def test_pro_ativo_bloqueia(app):
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-pro", qtd=3, email="f4.pro.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4-pro-orig")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4.pro@test.com", categoria="pro")
        _monetizacao_paga(origem.id, "pro")
        out = criar_convite(
            ator=contratante, email_destino="f4.pro@test.com", enviar=False
        )
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        with pytest.raises(ConviteContratoIncompativelError):
            aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.refresh(alvo)
        assert alvo.conta_id == origem.id


def test_membro_multiuser_outra_conta_bloqueia(app):
    with app.app_context():
        _secret(app)
        _conta, contratante = _preparar_conta_multiuser(
            "f4-mu-dest", qtd=3, email="f4.mu.d@test.com"
        )
        outra, membro_c = _preparar_conta_multiuser(
            "f4-mu-src", qtd=3, email="f4.mu.s@test.com"
        )
        livre = _franquia_livre(outra, membro_c)
        membro = seed_usuario(livre.id, outra.id, email="f4.mu.m@test.com", categoria="multiuser")
        from app.services.conta_multiuser_capacidade_service import ocupar_assento

        ocupar_assento(
            conta_id=outra.id,
            user_id=membro.id,
            franquia_id=livre.id,
            papel="membro",
        )
        out = criar_convite(
            ator=contratante, email_destino="f4.mu.m@test.com", enviar=False
        )
        csrf = gerar_csrf_token_aceite(membro.id, out.convite_id)
        with pytest.raises(ConviteContratoIncompativelError):
            aceitar_convite(user=db.session.get(User, membro.id), token=out.token, csrf_token=csrf)


def test_contratante_outra_conta_bloqueia(app):
    with app.app_context():
        _secret(app)
        _conta, contratante = _preparar_conta_multiuser(
            "f4-ct-d", qtd=3, email="f4.ct.d@test.com"
        )
        _outra, outro_ct = _preparar_conta_multiuser(
            "f4-ct-s", qtd=3, email="f4.ct.s@test.com"
        )
        out = criar_convite(
            ator=contratante, email_destino="f4.ct.s@test.com", enviar=False
        )
        csrf = gerar_csrf_token_aceite(outro_ct.id, out.convite_id)
        with pytest.raises(ConviteContratoIncompativelError):
            aceitar_convite(user=outro_ct, token=out.token, csrf_token=csrf)


def test_email_autenticado_diferente_bloqueia(app):
    with app.app_context():
        _secret(app)
        _conta, contratante = _preparar_conta_multiuser(
            "f4-em", qtd=3, email="f4.em.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4-em-o")
        outro = seed_usuario(fr_o.id, origem.id, email="outro.em@test.com", categoria="free")
        out = criar_convite(
            ator=contratante, email_destino="certo.em@test.com", enviar=False
        )
        csrf = gerar_csrf_token_aceite(outro.id, out.convite_id)
        with pytest.raises(ConviteEmailDivergenteError):
            aceitar_convite(user=outro, token=out.token, csrf_token=csrf)


def test_avulso_sem_vinculo_monetario_nao_e_incompativel_automatico(app):
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-av", qtd=3, email="f4.av.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4-av-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4.av@test.com", categoria="avulso")
        out = criar_convite(
            ator=contratante, email_destino="f4.av@test.com", enviar=False
        )
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        resultado = aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        assert resultado.papel == "membro"
        assert resultado.conta_id == conta.id


def test_replay_mesmo_user_nao_duplica(app):
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-rp", qtd=3, email="f4.rp.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4-rp-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4.rp@test.com", categoria="free")
        out = criar_convite(
            ator=contratante, email_destino="f4.rp@test.com", enviar=False
        )
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        r1 = aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        r2 = aceitar_convite(user=db.session.get(User, alvo.id), token=out.token, csrf_token=csrf)
        assert r2.idempotente is True
        assert r2.franquia_id == r1.franquia_id
        assert (
            ContaVinculoOrganizacional.query.filter_by(
                user_id=alvo.id, estado="ativo"
            ).count()
            == 1
        )
        rec = db.session.get(Franquia, r1.franquia_id)
        consumo = rec.consumo_acumulado
        r2_again = aceitar_convite(user=db.session.get(User, alvo.id), token=out.token, csrf_token=csrf)
        rec2 = db.session.get(Franquia, r1.franquia_id)
        assert rec2.consumo_acumulado == consumo
        assert r2_again.franquia_id == r1.franquia_id


def test_dois_users_nao_consomem_mesmo_convite(app):
    with app.app_context():
        _secret(app)
        _conta, contratante = _preparar_conta_multiuser(
            "f4-two", qtd=3, email="f4.two.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4-two-o")
        certo = seed_usuario(fr_o.id, origem.id, email="f4.two@test.com", categoria="free")
        origem2, fr2 = seed_conta_franquia_cliente(slug="f4-two-o2")
        outro = seed_usuario(fr2.id, origem2.id, email="f4.two.x@test.com", categoria="free")
        out = criar_convite(
            ator=contratante, email_destino="f4.two@test.com", enviar=False
        )
        csrf_errado = gerar_csrf_token_aceite(outro.id, out.convite_id)
        with pytest.raises(ConviteEmailDivergenteError):
            aceitar_convite(user=outro, token=out.token, csrf_token=csrf_errado)
        csrf_certo = gerar_csrf_token_aceite(certo.id, out.convite_id)
        aceitar_convite(user=certo, token=out.token, csrf_token=csrf_certo)
        with pytest.raises((ConviteInvalidoError, ConviteEmailDivergenteError)):
            aceitar_convite(user=outro, token=out.token, csrf_token=csrf_errado)
        assert (
            ContaVinculoOrganizacional.query.filter_by(
                franquia_id=out.franquia_id, estado="ativo"
            ).count()
            == 1
        )


def test_documentos_historicos_nao_sao_reatribuidos(app):
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-doc", qtd=3, email="f4.doc.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4-doc-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4.doc@test.com", categoria="free")
        ev = ProcessingEvent(
            agent="teste",
            flow_type="upload",
            processing_type="non_llm",
            rows_processed=3,
            processing_time_ms=10,
            status="success",
            conta_id=origem.id,
            franquia_id=fr_o.id,
            usuario_id=alvo.id,
        )
        db.session.add(ev)
        db.session.commit()
        event_id = ev.id
        out = criar_convite(
            ator=contratante, email_destino="f4.doc@test.com", enviar=False
        )
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        rec = db.session.get(ProcessingEvent, event_id)
        assert rec is not None
        assert rec.conta_id == origem.id
        assert rec.franquia_id == fr_o.id
        import app.services.conta_multiuser_convite_service as svc

        src = inspect.getsource(svc.aceitar_convite) + inspect.getsource(svc)
        assert "ProcessingEvent" not in inspect.getsource(svc.aceitar_convite)
        assert "IaConsumoEvento" not in src


def test_reenvio_idempotente_reserva(app, monkeypatch):
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-rs", qtd=3, email="f4.rs.c@test.com"
        )
        monkeypatch.setattr(
            "app.services.conta_multiuser_convite_service.send_email",
            lambda **kwargs: None,
        )
        a = criar_convite(
            ator=contratante,
            email_destino="f4.rs@test.com",
            build_invite_url=lambda t: f"/convite/{t}",
            enviar=True,
        )
        b = reenviar_convite(
            ator=contratante,
            convite_id=a.convite_id,
            build_invite_url=lambda t: f"/convite/{t}",
            enviar=True,
        )
        assert a.convite_id == b.convite_id
        assert a.franquia_id == b.franquia_id
        assert (
            ContaMultiuserConvite.query.filter_by(
                conta_id=conta.id, estado="pendente"
            ).count()
            == 1
        )


def test_http_contratante_cria_e_conta_payload_ignorada(app, monkeypatch):
    with app.app_context():
        monkeypatch.setattr(
            "app.services.conta_multiuser_convite_service.send_email",
            lambda **kwargs: None,
        )
        conta, contratante = _preparar_conta_multiuser(
            "f4-http", qtd=3, email="f4.http.c@test.com"
        )
        outra, _ = seed_conta_franquia_cliente(slug="f4-http-x")
        client = _build_client(app)
        _login(client, contratante)
        resp = client.post(
            "/api/multiuser/convites",
            json={
                "email": "f4.http@test.com",
                "conta_id": outra.id,
                "csrf_token": _csrf_gestao(contratante),
            },
        )
        assert resp.status_code in (200, 201)
        body = resp.get_json()
        assert body["conta_id"] == conta.id
        assert body["conta_id"] != outra.id


def test_http_membro_nao_cria(app, monkeypatch):
    with app.app_context():
        monkeypatch.setattr(
            "app.services.conta_multiuser_convite_service.send_email",
            lambda **kwargs: None,
        )
        conta, contratante = _preparar_conta_multiuser(
            "f4-http-m", qtd=3, email="f4.httpm.c@test.com"
        )
        convite = criar_convite(
            ator=contratante, email_destino="tmp.m@test.com", enviar=False
        )
        livre = [
            fr
            for fr in Franquia.query.filter_by(conta_id=conta.id).order_by(Franquia.id.asc())
            if int(fr.id) != int(convite.franquia_id)
            and int(fr.id) != int(contratante.franquia_id)
        ][0]
        membro = seed_usuario(livre.id, conta.id, email="f4.httpm@test.com", categoria="multiuser")
        from app.services.conta_multiuser_capacidade_service import ocupar_assento

        ocupar_assento(
            conta_id=conta.id, user_id=membro.id, franquia_id=livre.id, papel="membro"
        )
        client = _build_client(app)
        _login(client, membro)
        resp = client.post(
            "/api/multiuser/convites",
            json={
                "email": "alguem@test.com",
                "csrf_token": _csrf_gestao(membro),
            },
        )
        assert resp.status_code == 403


def test_servico_f4_sem_stripe_write_nem_ia():
    import app.services.conta_multiuser_convite_service as svc
    import app.conta_multiuser_convite_routes as routes

    src = inspect.getsource(svc) + inspect.getsource(routes)
    assert "_stripe_post" not in src
    assert "_stripe_get" not in src
    assert "checkout.sessions" not in src
    assert "Subscription.modify" not in src
    assert "openai" not in src.lower()
    assert "gemini" not in src.lower()
    assert "genai" not in src.lower()


def test_codigo_legado_nao_ocupa_assento(app):
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-leg", qtd=3, email="f4.leg.c@test.com"
        )
        livre = _franquia_livre(conta, contratante)
        row = MultiuserFranquiaCodigo(
            conta_id=conta.id,
            franquia_id=livre.id,
            codigo="MU-LEGADO-F4",
            ativo=True,
            criado_por_user_id=contratante.id,
        )
        db.session.add(row)
        db.session.commit()
        assert ContaVinculoOrganizacional.query.filter_by(
            franquia_id=livre.id, estado="ativo"
        ).first() is None


def test_landing_copia_transferencia(app):
    with app.app_context():
        _secret(app)
        _conta, contratante = _preparar_conta_multiuser(
            "f4-copy", qtd=3, email="f4.copy.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4-copy-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4.copy@test.com", categoria="free")
        out = criar_convite(
            ator=contratante, email_destino="f4.copy@test.com", enviar=False
        )
        ctx = montar_contexto_landing(out.token, alvo)
        assert ctx.exige_transferencia is True
        assert ctx.bloqueio is None
        client = _build_client(app)
        _login(client, alvo)
        resp = client.get(f"/convite/{out.token}")
        assert resp.status_code == 200
        assert "acesso operacional passará".encode("utf-8") in resp.data
        assert b"Aceitar convite" in resp.data


def _stamp_store_tmp(monkeypatch, tmp_path):
    import app.cleiton_doc_store as store

    monkeypatch.setattr(store, "get_cleiton_doc_tmp_dir", lambda: str(tmp_path))


# --- Correção auditoria F4 (C-01 / C-02 / H-01 / H-02 / M-01 / M-02) ---


def test_transferencia_invalida_documentos_sessao_julia_conta_anterior(
    app, monkeypatch, tmp_path
):
    from app.cleiton_doc_service import get_active_documents_for_session, register_document_placeholder
    from app.julia_documents_routes import julia_documents_bp
    from app.services import conta_multiuser_convite_service as svc

    _stamp_store_tmp(monkeypatch, tmp_path)
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-c01-http", qtd=5, email="f4.c01h.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4-c01-http-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4.c01h@test.com", categoria="free")
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        client = _build_client(app)
        if "julia_documents" not in app.blueprints:
            app.register_blueprint(julia_documents_bp)
        if "index" not in app.view_functions:
            @app.route("/", endpoint="index")
            def index():
                return "index"

        with app.test_request_context("/"):
            login_user(alvo)
            record = register_document_placeholder(
                display_name="old-account.txt",
                extension=".txt",
                mime_type="text/plain",
                size_bytes=10,
                prepared_context="OLD ACCOUNT CONTENT",
            )
            saved = dict(session)
        with client.session_transaction() as sess:
            sess.update(saved)
        resp = client.post(
            f"/convite/{out.token}/aceitar",
            data={"csrf_token": gerar_csrf_token_aceite(alvo.id, out.convite_id)},
        )
        assert resp.status_code == 302
        db.session.refresh(alvo)
        assert alvo.conta_id == conta.id
        listed = client.get("/api/julia/documents")
        assert listed.status_code == 200
        assert record["doc_id"] not in [r["doc_id"] for r in listed.json["documents"]]


def test_transferencia_nao_move_documento_historico(app, monkeypatch, tmp_path):
    from app.cleiton_doc_store import peek_document_record
    from app.cleiton_doc_service import register_document_placeholder

    _stamp_store_tmp(monkeypatch, tmp_path)
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-c01-keep", qtd=3, email="f4.c01k.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4-c01-keep-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4.c01k@test.com", categoria="free")
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        _build_client(app)
        with app.test_request_context("/"):
            login_user(alvo)
            record = register_document_placeholder(
                display_name="hist.txt",
                extension=".txt",
                mime_type="text/plain",
                size_bytes=8,
                prepared_context="KEEP",
            )
        stored = peek_document_record(record["doc_id"])
        assert stored is not None
        assert stored["conta_id"] == origem.id
        assert stored["franquia_id"] == fr_o.id
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        stored_after = peek_document_record(record["doc_id"])
        assert stored_after is not None
        assert stored_after["conta_id"] == origem.id
        assert stored_after["franquia_id"] == fr_o.id
        db.session.refresh(alvo)
        assert alvo.conta_id == conta.id


def test_transferido_nao_recupera_documento_antigo_por_session_id(
    app, monkeypatch, tmp_path
):
    from app.cleiton_doc_service import (
        get_active_documents_for_session,
        register_document_placeholder,
    )
    from app.cleiton_doc_store import peek_document_record

    _stamp_store_tmp(monkeypatch, tmp_path)
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-c01-sid", qtd=3, email="f4.c01s.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4-c01-sid-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4.c01s@test.com", categoria="free")
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        _build_client(app)
        with app.test_request_context("/"):
            login_user(alvo)
            record = register_document_placeholder(
                display_name="sid.txt",
                extension=".txt",
                mime_type="text/plain",
                size_bytes=8,
                prepared_context="SESSION",
            )
            saved_ids = list(session.get("cleiton_doc_ids") or [])
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.refresh(alvo)
        assert alvo.conta_id == conta.id
        with app.test_request_context("/"):
            login_user(alvo)
            session["cleiton_doc_ids"] = saved_ids
            active = get_active_documents_for_session()
        assert record["doc_id"] not in [r["doc_id"] for r in active]
        assert peek_document_record(record["doc_id"])["conta_id"] == origem.id


def test_novo_documento_apos_transferencia_usa_novo_escopo(app, monkeypatch, tmp_path):
    from app.cleiton_doc_service import register_document_placeholder
    from app.cleiton_doc_store import peek_document_record

    _stamp_store_tmp(monkeypatch, tmp_path)
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-c01-new", qtd=3, email="f4.c01n.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4-c01-new-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4.c01n@test.com", categoria="free")
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.refresh(alvo)
        _build_client(app)
        with app.test_request_context("/"):
            login_user(alvo)
            novo = register_document_placeholder(
                display_name="novo.txt",
                extension=".txt",
                mime_type="text/plain",
                size_bytes=6,
                prepared_context="NEW",
            )
        stored = peek_document_record(novo["doc_id"])
        assert stored["conta_id"] == conta.id
        assert stored["franquia_id"] == alvo.franquia_id
        assert stored["conta_id"] != origem.id


def test_helpers_compartilhados_revalidam_escopo_cleide_e_compara(
    app, monkeypatch, tmp_path
):
    from app.agente_compara_doc_service import (
        get_active_documents_for_session as compara_active,
    )
    from app.cleide_audit_doc_service import (
        _register_document_record as cleide_register,
        get_active_documents_for_session as cleide_active,
    )

    _stamp_store_tmp(monkeypatch, tmp_path)
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-c01-dom", qtd=3, email="f4.c01d.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4-c01-dom-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4.c01d@test.com", categoria="free")
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        _build_client(app)
        with app.test_request_context("/"):
            login_user(alvo)
            cleide_register(
                display_name="cleide.txt",
                extension=".txt",
                mime_type="text/plain",
                size_bytes=8,
                prepared_context="CLEIDE-OLD",
            )
            from app.agente_compara_doc_service import _register_document_record as compara_register

            compara_register(
                display_name="compara.txt",
                extension=".txt",
                mime_type="text/plain",
                size_bytes=8,
                prepared_context="COMPARA-OLD",
            )
            saved_cleide = list(session.get("cleide_audit_doc_ids") or [])
            saved_compara = list(session.get("agente_compara_doc_ids") or [])
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.refresh(alvo)
        with app.test_request_context("/"):
            login_user(alvo)
            session["cleide_audit_doc_ids"] = saved_cleide
            session["agente_compara_doc_ids"] = saved_compara
            assert cleide_active() == []
            assert compara_active() == []


def test_ocupar_assento_bloqueia_capacidade_reservada(app):
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-c02-oc", qtd=5, email="f4.c02o.c@test.com"
        )
        for i in range(4):
            criar_convite(
                ator=contratante,
                email_destino=f"f4.c02o.{i}@test.com",
                enviar=False,
            )
        extra = Franquia(
            conta_id=conta.id, nome="Extra", slug="f4-c02-oc-x", status="active"
        )
        db.session.add(extra)
        db.session.commit()
        member = seed_usuario(extra.id, conta.id, email="f4.c02o.m@test.com", categoria="free")
        with pytest.raises(CapacidadeEsgotadaError):
            ocupar_assento(
                conta_id=conta.id, user_id=member.id, franquia_id=extra.id, papel="membro"
            )
        occupied = ContaVinculoOrganizacional.query.filter_by(
            conta_id=conta.id, estado="ativo"
        ).count()
        reserved = ContaMultiuserConvite.query.filter_by(
            conta_id=conta.id, estado="pendente"
        ).count()
        assert occupied + reserved <= conta.quantidade_assentos_contratados


def test_admin_nao_ocupa_capacidade_comprometida_por_convites(app):
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-c02-ad", qtd=5, email="f4.c02a.c@test.com"
        )
        for i in range(4):
            criar_convite(
                ator=contratante,
                email_destino=f"f4.c02a.{i}@test.com",
                enviar=False,
            )
        extra = Franquia(
            conta_id=conta.id, nome="Extra admin", slug="f4-c02-ad-x", status="active"
        )
        db.session.add(extra)
        db.session.commit()
        member = seed_usuario(
            extra.id, conta.id, email="f4.c02a.m@test.com", categoria="free"
        )
        atribuir_plano_para_usuario(
            email=member.email, plano_raw="multiuser", quantidade_franquias_raw="5"
        )
        occupied = ContaVinculoOrganizacional.query.filter_by(
            conta_id=conta.id, estado="ativo"
        ).count()
        reserved = ContaMultiuserConvite.query.filter_by(
            conta_id=conta.id, estado="pendente"
        ).count()
        assert occupied + reserved <= conta.quantidade_assentos_contratados
        assert ContaVinculoOrganizacional.query.filter_by(
            user_id=member.id, estado="ativo"
        ).first() is None


def test_admin_nao_ocupa_franquia_reservada(app):
    from app.services.conta_multiuser_errors import VinculoInconsistenteError

    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-c02-res", qtd=5, email="f4.c02r.c@test.com"
        )
        convite = criar_convite(
            ator=contratante, email_destino="f4.c02r.t@test.com", enviar=False
        )
        extra = Franquia(
            conta_id=conta.id, nome="Livre", slug="f4-c02-res-x", status="active"
        )
        db.session.add(extra)
        db.session.commit()
        member = seed_usuario(
            extra.id, conta.id, email="f4.c02r.m@test.com", categoria="free"
        )
        with pytest.raises((CapacidadeEsgotadaError, VinculoInconsistenteError)):
            ocupar_assento(
                conta_id=conta.id,
                user_id=member.id,
                franquia_id=convite.franquia_id,
                papel="membro",
            )
        assert (
            ContaVinculoOrganizacional.query.filter_by(
                franquia_id=convite.franquia_id, estado="ativo"
            ).count()
            == 0
        )


def test_aceite_converte_propria_reserva_sem_estouro(app):
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-c02-cv", qtd=5, email="f4.c02c.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4-c02-cv-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4.c02c@test.com", categoria="free")
        invite = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        for i in range(3):
            criar_convite(
                ator=contratante,
                email_destino=f"f4.c02c.o{i}@test.com",
                enviar=False,
            )
        assert ContaVinculoOrganizacional.query.filter_by(
            conta_id=conta.id, estado="ativo"
        ).count() == 1
        assert ContaMultiuserConvite.query.filter_by(
            conta_id=conta.id, estado="pendente"
        ).count() == 4
        csrf = gerar_csrf_token_aceite(alvo.id, invite.convite_id)
        aceitar_convite(user=alvo, token=invite.token, csrf_token=csrf)
        assert ContaVinculoOrganizacional.query.filter_by(
            conta_id=conta.id, estado="ativo"
        ).count() == 2
        assert ContaMultiuserConvite.query.filter_by(
            conta_id=conta.id, estado="pendente"
        ).count() == 3


def test_franquias_fisicas_extras_nao_aumentam_quantity(app):
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-c02-ex", qtd=5, email="f4.c02e.c@test.com"
        )
        for i in range(4):
            criar_convite(
                ator=contratante,
                email_destino=f"f4.c02e.{i}@test.com",
                enviar=False,
            )
        for idx in range(2):
            extra = Franquia(
                conta_id=conta.id,
                nome=f"Extra {idx}",
                slug=f"f4-c02-ex-{idx}",
                status="active",
            )
            db.session.add(extra)
        db.session.commit()
        assert Franquia.query.filter_by(conta_id=conta.id).count() > 5
        extra = Franquia.query.filter_by(slug="f4-c02-ex-0").one()
        member = seed_usuario(
            extra.id, conta.id, email="f4.c02e.m@test.com", categoria="free"
        )
        with pytest.raises(CapacidadeEsgotadaError):
            ocupar_assento(
                conta_id=conta.id, user_id=member.id, franquia_id=extra.id, papel="membro"
            )
        assert conta.quantidade_assentos_contratados == 5


def test_convite_vs_ocupacao_compartilham_governanca_capacidade():
    from app.services import conta_multiuser_capacidade_service as cap
    from app.services import conta_multiuser_convite_service as conv

    src_oc = inspect.getsource(cap.ocupar_assento)
    src_cr = inspect.getsource(conv.criar_convite)
    assert "bloquear_conta_para_capacidade" in src_oc
    assert "bloquear_conta_para_capacidade" in src_cr
    assert "contar_capacidade_comprometida" in inspect.getsource(cap._exigir_capacidade_livre)
    assert "contar_capacidade_comprometida" in src_cr
    assert "convite_id_em_conversao" in src_oc
    assert "ignore_reservation" not in src_oc
    assert "subtract_one" not in src_oc


def test_starter_payment_failed_vigente_bloqueia_transferencia(app):
    from app.services.cleiton_monetizacao_service import processar_evento_stripe

    with app.app_context():
        _secret(app)
        _conta, contratante = _preparar_conta_multiuser(
            "f4-h01-st", qtd=3, email="f4.h01s.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4-h01-st-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4.h01s@test.com", categoria="starter")
        row = _monetizacao_paga(origem.id, "starter")
        processar_evento_stripe(
            {
                "id": "evt_f4_h01_st",
                "type": "invoice.payment_failed",
                "data": {
                    "object": {
                        "id": "in_f4_h01_st",
                        "customer": row.customer_id,
                        "subscription": row.subscription_id,
                        "billing_reason": "subscription_cycle",
                        "metadata": {
                            "conta_id": str(origem.id),
                            "franquia_id": str(fr_o.id),
                            "usuario_id": str(alvo.id),
                            "plano_interno": "starter",
                        },
                    }
                },
            }
        )
        db.session.refresh(row)
        assert row.ativo and row.status_contratual_externo == "payment_failed"
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        with pytest.raises(ConviteContratoIncompativelError):
            aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)


def test_pro_payment_failed_vigente_bloqueia_transferencia(app):
    from app.services.cleiton_monetizacao_service import processar_evento_stripe

    with app.app_context():
        _secret(app)
        _conta, contratante = _preparar_conta_multiuser(
            "f4-h01-pro", qtd=3, email="f4.h01p.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4-h01-pro-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4.h01p@test.com", categoria="pro")
        row = _monetizacao_paga(origem.id, "pro")
        processar_evento_stripe(
            {
                "id": "evt_f4_h01_pro",
                "type": "invoice.payment_failed",
                "data": {
                    "object": {
                        "id": "in_f4_h01_pro",
                        "customer": row.customer_id,
                        "subscription": row.subscription_id,
                        "billing_reason": "subscription_cycle",
                        "metadata": {
                            "conta_id": str(origem.id),
                            "franquia_id": str(fr_o.id),
                            "usuario_id": str(alvo.id),
                            "plano_interno": "pro",
                        },
                    }
                },
            }
        )
        db.session.refresh(row)
        assert row.ativo and row.status_contratual_externo == "payment_failed"
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        with pytest.raises(ConviteContratoIncompativelError):
            aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)


def test_starter_cancelamento_cutoff_futuro_bloqueia(app):
    with app.app_context():
        _secret(app)
        _conta, contratante = _preparar_conta_multiuser(
            "f4-h02-st", qtd=3, email="f4.h02s.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4-h02-st-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4.h02s@test.com", categoria="free")
        row = _monetizacao_paga(origem.id, "starter")
        row.vigencia_externa_fim = utcnow_naive() + timedelta(days=10)
        row.snapshot_normalizado_json = json.dumps(
            {
                "mudanca_pendente": True,
                "tipo_mudanca": "downgrade",
                "plano_futuro": "free",
                "efetivar_em": row.vigencia_externa_fim.isoformat(),
            }
        )
        db.session.commit()
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        with pytest.raises(ConviteContratoIncompativelError):
            aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)


def test_pro_downgrade_cutoff_futuro_bloqueia(app):
    with app.app_context():
        _secret(app)
        _conta, contratante = _preparar_conta_multiuser(
            "f4-h02-pro", qtd=3, email="f4.h02p.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4-h02-pro-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4.h02p@test.com", categoria="free")
        row = _monetizacao_paga(origem.id, "pro")
        row.vigencia_externa_fim = utcnow_naive() + timedelta(days=10)
        row.snapshot_normalizado_json = json.dumps(
            {
                "mudanca_pendente": True,
                "tipo_mudanca": "downgrade",
                "plano_futuro": "starter",
                "efetivar_em": row.vigencia_externa_fim.isoformat(),
            }
        )
        db.session.commit()
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        with pytest.raises(ConviteContratoIncompativelError):
            aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)


def test_corte_efetivado_permite_transferencia_quando_realmente_free(app):
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-h02-cut", qtd=3, email="f4.h02c.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4-h02-cut-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4.h02c@test.com", categoria="free")
        row = _monetizacao_paga(origem.id, "starter")
        row.ativo = False
        row.plano_interno = "free"
        row.vigencia_externa_fim = utcnow_naive() - timedelta(days=1)
        db.session.add(row)
        db.session.commit()
        assert conta_possui_beneficio_pago_vigente(origem.id) is False
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.refresh(alvo)
        assert alvo.conta_id == conta.id


def test_categoria_paga_sem_vigencia_real_nao_bloqueia_sozinha(app):
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-h02-cat", qtd=3, email="f4.h02t.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4-h02-cat-o")
        alvo = seed_usuario(
            fr_o.id, origem.id, email="f4.h02t@test.com", categoria="starter"
        )
        assert conta_possui_beneficio_pago_vigente(origem.id) is False
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.refresh(alvo)
        assert alvo.conta_id == conta.id


def test_integrityerror_unique_email_e_tratado(app, monkeypatch):
    from app.services import conta_multiuser_convite_service as svc
    from app.services.conta_organizacional_rules import UQ_CONVITE_CONTA_EMAIL_PENDENTE

    with app.app_context():
        _secret(app)
        _conta, contratante = _preparar_conta_multiuser(
            "f4-m01-em", qtd=5, email="f4.m01e.c@test.com"
        )
        first = criar_convite(
            ator=contratante, email_destino="f4.m01e.t@test.com", enviar=False
        )
        exc = IntegrityError("INSERT", {}, Exception(UQ_CONVITE_CONTA_EMAIL_PENDENTE))
        assert svc._identificar_constraint_convite(exc) == UQ_CONVITE_CONTA_EMAIL_PENDENTE
        original = db.session.flush

        def flush(*args, **kwargs):
            if any(isinstance(obj, ContaMultiuserConvite) for obj in db.session.new):
                raise IntegrityError(
                    "INSERT", {}, Exception(UQ_CONVITE_CONTA_EMAIL_PENDENTE)
                )
            return original(*args, **kwargs)

        monkeypatch.setattr(db.session, "flush", flush)
        monkeypatch.setattr(svc, "_convite_pendente_valido", lambda **_kw: None)
        out = criar_convite(
            ator=contratante, email_destino="f4.m01e.t@test.com", enviar=False
        )
        assert out.reutilizado is True
        assert out.convite_id == first.convite_id


def test_integrityerror_unique_franquia_e_tratado(app, monkeypatch):
    from app.services import conta_multiuser_convite_service as svc
    from app.services.conta_organizacional_rules import UQ_CONVITE_FRANQUIA_PENDENTE

    with app.app_context():
        _secret(app)
        _conta, contratante = _preparar_conta_multiuser(
            "f4-m01-fr", qtd=5, email="f4.m01f.c@test.com"
        )
        first = criar_convite(
            ator=contratante, email_destino="f4.m01f.a@test.com", enviar=False
        )
        exc = IntegrityError("INSERT", {}, Exception(UQ_CONVITE_FRANQUIA_PENDENTE))
        assert svc._identificar_constraint_convite(exc) == UQ_CONVITE_FRANQUIA_PENDENTE
        original = db.session.flush

        def flush(*args, **kwargs):
            if any(isinstance(obj, ContaMultiuserConvite) for obj in db.session.new):
                raise IntegrityError(
                    "INSERT", {}, Exception(UQ_CONVITE_FRANQUIA_PENDENTE)
                )
            return original(*args, **kwargs)

        monkeypatch.setattr(db.session, "flush", flush)
        with pytest.raises(CapacidadeEsgotadaError):
            criar_convite(
                ator=contratante, email_destino="f4.m01f.b@test.com", enviar=False
            )
        assert db.session.get(ContaMultiuserConvite, first.convite_id).estado == "pendente"


def test_integrityerror_desconhecido_e_relangado(app, monkeypatch):
    from app.services import conta_multiuser_convite_service as svc

    with app.app_context():
        _secret(app)
        _conta, contratante = _preparar_conta_multiuser(
            "f4-m01-un", qtd=5, email="f4.m01u.c@test.com"
        )
        original = db.session.flush

        def flush(*args, **kwargs):
            if any(isinstance(obj, ContaMultiuserConvite) for obj in db.session.new):
                raise IntegrityError("INSERT", {}, Exception("unknown constraint"))
            return original(*args, **kwargs)

        monkeypatch.setattr(db.session, "flush", flush)
        with pytest.raises(IntegrityError):
            criar_convite(
                ator=contratante, email_destino="f4.m01u.t@test.com", enviar=False
            )


def test_convite_email_log_nao_contem_destinatario_completo(app, monkeypatch, caplog):
    import app.auth_services as auth

    monkeypatch.setenv("RESEND_API_KEY", "f4-fake")
    monkeypatch.setattr(
        auth.requests,
        "post",
        lambda *_a, **_kw: SimpleNamespace(status_code=200, headers={}),
    )
    with app.app_context():
        _secret(app)
        _conta, contratante = _preparar_conta_multiuser(
            "f4-m02-em", qtd=3, email="f4.m02e.c@test.com"
        )
        destino = "private-recipient-f4@audit.test"
        with caplog.at_level(logging.INFO):
            criar_convite(
                ator=contratante,
                email_destino=destino,
                enviar=True,
                build_invite_url=lambda token: f"http://test/convite/{token}",
            )
        assert destino not in caplog.text


def test_send_email_falha_nao_loga_payload_provider_bruto(monkeypatch, caplog):
    import app.auth_services as auth

    monkeypatch.setenv("RESEND_API_KEY", "f4-fake")
    brute = '{"email":"secret-body@audit.test","message":"full provider body"}'
    monkeypatch.setattr(
        auth.requests,
        "post",
        lambda *_a, **_kw: SimpleNamespace(
            status_code=422, text=brute, headers={"x-request-id": "req_f4"}
        ),
    )
    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError):
            auth.send_email("private-recipient@audit.test", "Convite", "<p>invite</p>")
    assert brute not in caplog.text
    assert "private-recipient@audit.test" not in caplog.text
    assert "full provider body" not in caplog.text
    assert "status_code=422" in caplog.text
    assert "request_id=" not in caplog.text


def test_log_convite_nao_contem_token(app, monkeypatch, caplog):
    sent = []

    def _fake_send(**kwargs):
        sent.append(kwargs)

    monkeypatch.setattr(
        "app.services.conta_multiuser_convite_service.send_email", _fake_send
    )
    with app.app_context():
        _secret(app)
        _conta, contratante = _preparar_conta_multiuser(
            "f4-m02-tk", qtd=3, email="f4.m02t.c@test.com"
        )
        with caplog.at_level(logging.INFO):
            out = criar_convite(
                ator=contratante,
                email_destino="f4.m02t.t@test.com",
                enviar=True,
                build_invite_url=lambda token: f"http://test/convite/{token}",
            )
        assert out.token
        assert out.token not in caplog.text
        assert f"/convite/{out.token}" not in caplog.text
        assert sent and out.token in sent[0]["html"]


# --- Segunda correção F4 (COR-F4D) ---


def test_legado_sem_scope_reintroduzido_nao_e_autorizado(app, monkeypatch, tmp_path):
    from app.cleiton_doc_service import get_active_documents_for_session, register_document_placeholder
    from app.cleiton_doc_store import peek_document_record, save_document_record

    _stamp_store_tmp(monkeypatch, tmp_path)
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-d-leg", qtd=3, email="f4.dleg.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4-d-leg-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4.dleg@test.com", categoria="free")
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        _build_client(app)
        with app.test_request_context("/"):
            login_user(alvo)
            record = register_document_placeholder(
                display_name="legado.txt",
                extension=".txt",
                mime_type="text/plain",
                size_bytes=8,
                prepared_context="LEGACY SECRET",
            )
            saved_ids = list(session.get("cleiton_doc_ids") or [])
        stored = peek_document_record(record["doc_id"])
        for field in ("conta_id", "franquia_id", "usuario_id"):
            stored.pop(field, None)
        save_document_record(stored)
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.refresh(alvo)
        with app.test_request_context("/"):
            login_user(alvo)
            session["cleiton_doc_ids"] = saved_ids
            active = get_active_documents_for_session()
        assert record["doc_id"] not in [r["doc_id"] for r in active]


def test_segunda_sessao_antiga_nao_recupera_legado_pos_transferencia(
    app, monkeypatch, tmp_path
):
    from app.cleiton_doc_service import register_document_placeholder
    from app.cleiton_doc_store import peek_document_record, save_document_record
    from app.julia_documents_routes import julia_documents_bp

    _stamp_store_tmp(monkeypatch, tmp_path)
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-d-s2", qtd=3, email="f4.ds2.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4-d-s2-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4.ds2@test.com", categoria="free")
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        client = _build_client(app)
        if "julia_documents" not in app.blueprints:
            app.register_blueprint(julia_documents_bp)
        if "index" not in app.view_functions:

            @app.route("/", endpoint="index")
            def index():
                return "index"

        with app.test_request_context("/"):
            login_user(alvo)
            record = register_document_placeholder(
                display_name="sess2.txt",
                extension=".txt",
                mime_type="text/plain",
                size_bytes=8,
                prepared_context="SECOND SESSION",
            )
            saved = dict(session)
        stored = peek_document_record(record["doc_id"])
        for field in ("conta_id", "franquia_id", "usuario_id"):
            stored.pop(field, None)
        save_document_record(stored)
        with client.session_transaction() as sess:
            sess.update(saved)
        resp = client.post(
            f"/convite/{out.token}/aceitar",
            data={"csrf_token": gerar_csrf_token_aceite(alvo.id, out.convite_id)},
        )
        assert resp.status_code == 302
        second = app.test_client()
        with second.session_transaction() as sess:
            sess.update(saved)
        listed = second.get("/api/julia/documents")
        assert listed.status_code == 200
        assert record["doc_id"] not in [r["doc_id"] for r in listed.json["documents"]]


def test_cleide_contexto_nao_injeta_documento_conta_anterior(app, monkeypatch, tmp_path):
    from app.cleide_audit_doc_context import build_cleide_audit_document_context_for_chat
    from app.cleide_audit_doc_service import _register_document_record

    _stamp_store_tmp(monkeypatch, tmp_path)
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-d-cl", qtd=3, email="f4.dcl.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4-d-cl-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4.dcl@test.com", categoria="free")
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        _build_client(app)
        with app.test_request_context("/"):
            login_user(alvo)
            _register_document_record(
                display_name="cleide-old.txt",
                extension=".txt",
                mime_type="text/plain",
                size_bytes=8,
                context_kind="text",
                prepared_context="OLD_ACCOUNT_A_SECRET",
            )
            saved_ids = list(session.get("cleide_audit_doc_ids") or [])
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.refresh(alvo)
        with app.test_request_context("/"):
            login_user(alvo)
            session["cleide_audit_doc_ids"] = saved_ids
            result = build_cleide_audit_document_context_for_chat(session)
        assert "OLD_ACCOUNT_A_SECRET" not in result["context_block"]


def test_compara_contexto_nao_injeta_documento_conta_anterior(app, monkeypatch, tmp_path):
    from app.agente_compara_doc_context import build_agente_compara_document_context_for_chat
    from app.agente_compara_doc_service import _register_document_record

    _stamp_store_tmp(monkeypatch, tmp_path)
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-d-cp", qtd=3, email="f4.dcp.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4-d-cp-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4.dcp@test.com", categoria="free")
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        _build_client(app)
        with app.test_request_context("/"):
            login_user(alvo)
            _register_document_record(
                display_name="compara-old.txt",
                extension=".txt",
                mime_type="text/plain",
                size_bytes=8,
                context_kind="text",
                prepared_context="OLD_ACCOUNT_A_SECRET",
            )
            saved_ids = list(session.get("agente_compara_doc_ids") or [])
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.refresh(alvo)
        with app.test_request_context("/"):
            login_user(alvo)
            session["agente_compara_doc_ids"] = saved_ids
            result = build_agente_compara_document_context_for_chat(session)
        assert "OLD_ACCOUNT_A_SECRET" not in result["context_block"]


def test_cache_cleide_conta_anterior_vira_miss(app, monkeypatch, tmp_path):
    from app.run_cleide_audit_chat import cache_chat_response, get_cached_chat_response

    _stamp_store_tmp(monkeypatch, tmp_path)
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-d-cc", qtd=3, email="f4.dcc.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4-d-cc-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4.dcc@test.com", categoria="free")
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        _build_client(app)
        with app.test_request_context("/"):
            login_user(alvo)
            cache_chat_response(
                session,
                "old-request",
                {"answer": "OLD_ACCOUNT_A_CONFIDENTIAL_RESULT", "documents_used": []},
            )
            saved = dict(session)
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.refresh(alvo)
        with app.test_request_context("/"):
            login_user(alvo)
            session.update(saved)
            assert get_cached_chat_response(session, "old-request") is None


def test_cache_compara_conta_anterior_vira_miss(app, monkeypatch, tmp_path):
    from app.run_agente_compara_chat import cache_chat_response, get_cached_chat_response

    _stamp_store_tmp(monkeypatch, tmp_path)
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-d-cpc", qtd=3, email="f4.dcpc.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4-d-cpc-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4.dcpc@test.com", categoria="free")
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        _build_client(app)
        with app.test_request_context("/"):
            login_user(alvo)
            cache_chat_response(
                session,
                "old-request",
                {"answer": "OLD_ACCOUNT_A_CONFIDENTIAL_RESULT", "documents_used": []},
            )
            saved = dict(session)
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.refresh(alvo)
        with app.test_request_context("/"):
            login_user(alvo)
            session.update(saved)
            assert get_cached_chat_response(session, "old-request") is None


def test_cache_legado_sem_scope_vira_miss(app, monkeypatch, tmp_path):
    from app.run_cleide_audit_chat import (
        CHAT_IDEMPOTENCY_CACHE_SESSION_KEY,
        get_cached_chat_response,
    )
    from app.cleide_audit_doc_service import cleide_audit_chat_idempotency_key

    _stamp_store_tmp(monkeypatch, tmp_path)
    with app.app_context():
        _secret(app)
        origem, fr_o = seed_conta_franquia_cliente(slug="f4-d-nosc-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4.dnosc@test.com", categoria="free")
        _build_client(app)
        with app.test_request_context("/"):
            login_user(alvo)
            session[CHAT_IDEMPOTENCY_CACHE_SESSION_KEY] = {
                cleide_audit_chat_idempotency_key("old-request"): {
                    "answer": "LEGACY UNSCOPED",
                    "documents_used": [],
                }
            }
            assert get_cached_chat_response(session, "old-request") is None


def test_invalidacao_pos_transferencia_remove_todas_chaves_operacionais_catalogadas(
    app, monkeypatch, tmp_path
):
    from app.cleiton_doc_escopo import OPERATIONAL_SESSION_KEYS, invalidate_session_document_refs

    _stamp_store_tmp(monkeypatch, tmp_path)
    with app.app_context():
        _secret(app)
        origem, fr_o = seed_conta_franquia_cliente(slug="f4-d-inv-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4.dinv@test.com", categoria="free")
        _build_client(app)
        with app.test_request_context("/"):
            login_user(alvo)
            for key in OPERATIONAL_SESSION_KEYS:
                session[key] = {"marker": key}
            session["_user_id"] = str(alvo.id)
            invalidate_session_document_refs(session)
            for key in OPERATIONAL_SESSION_KEYS:
                assert key not in session
            assert session.get("_user_id") == str(alvo.id)
            from app.agente_compara_comparison_state import (
                AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY,
            )
            from app.agente_compara_correction_service import (
                PREVIEW_SESSION_KEY as COMPARA_PREVIEW_KEY,
            )
            from app.agente_compara_doc_service import (
                AGENTE_COMPARA_CHAT_HISTORY_SESSION_KEY,
                AGENTE_COMPARA_DOC_CONTEXT_SESSION_KEY,
                AGENTE_COMPARA_DOC_IDS_SESSION_KEY,
                AGENTE_COMPARA_LAST_REQUEST_ID_SESSION_KEY,
                AGENTE_COMPARA_TEMP_TABLE_ID_SESSION_KEY,
                AGENTE_COMPARA_TEMP_TABLE_SOURCE_DOCS_SESSION_KEY,
                AGENTE_COMPARA_UPLOAD_IN_PROGRESS_SESSION_KEY,
                AGENTE_COMPARA_UPLOAD_LOCK_SESSION_KEY,
                TEMP_TABLE_SAVE_IDEMPOTENCY_CACHE_SESSION_KEY,
            )
            from app.agente_compara_insights_context import (
                AGENTE_COMPARA_INSIGHTS_CHAT_UNLOCK_SESSION_KEY,
                AGENTE_COMPARA_INSIGHTS_CONVERSATION_FOCUS_SESSION_KEY,
            )
            from app.cleide_audit_correction_service import PREVIEW_SESSION_KEY as CLEIDE_PREVIEW_KEY
            from app.cleide_audit_doc_service import (
                CLEIDE_AUDIT_CHAT_HISTORY_SESSION_KEY,
                CLEIDE_AUDIT_DOC_CONTEXT_SESSION_KEY,
                CLEIDE_AUDIT_DOC_IDS_SESSION_KEY,
                CLEIDE_AUDIT_LAST_REQUEST_ID_SESSION_KEY,
                CLEIDE_AUDIT_TEMP_TABLE_ID_SESSION_KEY,
                CLEIDE_AUDIT_TEMP_TABLE_SOURCE_DOCS_SESSION_KEY,
                CLEIDE_AUDIT_UPLOAD_IN_PROGRESS_SESSION_KEY,
                CLEIDE_AUDIT_UPLOAD_LOCK_SESSION_KEY,
            )
            from app.cleide_audit_insights_context import (
                CLEIDE_AUDIT_INSIGHTS_CHAT_UNLOCK_SESSION_KEY,
                CLEIDE_AUDIT_INSIGHTS_CONVERSATION_FOCUS_SESSION_KEY,
            )
            from app.cleide_contracts import (
                SESSION_KEY_CLEIDE_DATASET_CONTEXT,
                SESSION_KEY_CLEIDE_UPLOAD_IN_PROGRESS,
                SESSION_KEY_CLEIDE_UPLOAD_LOCK,
                SESSION_KEY_CLEIDE_UPLOAD_REF,
            )
            from app.cleiton_doc_contracts import (
                SESSION_KEY_CLEITON_DOC_IDS,
                SESSION_KEY_CLEITON_DOC_LOCK,
            )
            from app.run_agente_compara_chat import (
                CHAT_IDEMPOTENCY_CACHE_SESSION_KEY as COMPARA_CHAT_CACHE,
            )
            from app.run_agente_compara_comparison_chat import (
                COMPARISON_CHAT_IDEMPOTENCY_CACHE_SESSION_KEY,
            )
            from app.run_agente_compara_insights_chat import (
                INSIGHTS_CHAT_IDEMPOTENCY_CACHE_SESSION_KEY as COMPARA_INSIGHTS_CACHE,
            )
            from app.run_agente_compara_temp_table import (
                TEMP_TABLE_EXTRACTION_IDEMPOTENCY_CACHE_KEY as COMPARA_EXTRACT_CACHE,
            )
            from app.run_cleide_audit_chat import (
                CHAT_IDEMPOTENCY_CACHE_SESSION_KEY as CLEIDE_CHAT_CACHE,
            )
            from app.run_cleide_audit_insights_chat import (
                INSIGHTS_CHAT_IDEMPOTENCY_CACHE_SESSION_KEY as CLEIDE_INSIGHTS_CACHE,
            )
            from app.run_cleide_audit_temp_table import (
                TEMP_TABLE_EXTRACTION_IDEMPOTENCY_CACHE_KEY as CLEIDE_EXTRACT_CACHE,
            )
            from app.upload_handler import SESSION_KEY_UPLOAD_REF

            catalog = set(OPERATIONAL_SESSION_KEYS)
            for key in (
                SESSION_KEY_CLEITON_DOC_IDS,
                SESSION_KEY_CLEITON_DOC_LOCK,
                CLEIDE_AUDIT_DOC_IDS_SESSION_KEY,
                CLEIDE_AUDIT_DOC_CONTEXT_SESSION_KEY,
                CLEIDE_AUDIT_CHAT_HISTORY_SESSION_KEY,
                CLEIDE_AUDIT_TEMP_TABLE_ID_SESSION_KEY,
                CLEIDE_AUDIT_TEMP_TABLE_SOURCE_DOCS_SESSION_KEY,
                CLEIDE_AUDIT_UPLOAD_LOCK_SESSION_KEY,
                CLEIDE_AUDIT_LAST_REQUEST_ID_SESSION_KEY,
                CLEIDE_AUDIT_UPLOAD_IN_PROGRESS_SESSION_KEY,
                CLEIDE_CHAT_CACHE,
                CLEIDE_INSIGHTS_CACHE,
                CLEIDE_AUDIT_INSIGHTS_CHAT_UNLOCK_SESSION_KEY,
                CLEIDE_AUDIT_INSIGHTS_CONVERSATION_FOCUS_SESSION_KEY,
                CLEIDE_PREVIEW_KEY,
                CLEIDE_EXTRACT_CACHE,
                SESSION_KEY_CLEIDE_UPLOAD_REF,
                SESSION_KEY_CLEIDE_UPLOAD_IN_PROGRESS,
                SESSION_KEY_CLEIDE_UPLOAD_LOCK,
                SESSION_KEY_CLEIDE_DATASET_CONTEXT,
                AGENTE_COMPARA_DOC_IDS_SESSION_KEY,
                AGENTE_COMPARA_DOC_CONTEXT_SESSION_KEY,
                AGENTE_COMPARA_CHAT_HISTORY_SESSION_KEY,
                AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY,
                AGENTE_COMPARA_TEMP_TABLE_ID_SESSION_KEY,
                AGENTE_COMPARA_TEMP_TABLE_SOURCE_DOCS_SESSION_KEY,
                AGENTE_COMPARA_UPLOAD_LOCK_SESSION_KEY,
                AGENTE_COMPARA_LAST_REQUEST_ID_SESSION_KEY,
                AGENTE_COMPARA_UPLOAD_IN_PROGRESS_SESSION_KEY,
                COMPARA_CHAT_CACHE,
                COMPARA_INSIGHTS_CACHE,
                COMPARISON_CHAT_IDEMPOTENCY_CACHE_SESSION_KEY,
                TEMP_TABLE_SAVE_IDEMPOTENCY_CACHE_SESSION_KEY,
                COMPARA_EXTRACT_CACHE,
                COMPARA_PREVIEW_KEY,
                AGENTE_COMPARA_INSIGHTS_CHAT_UNLOCK_SESSION_KEY,
                AGENTE_COMPARA_INSIGHTS_CONVERSATION_FOCUS_SESSION_KEY,
                SESSION_KEY_UPLOAD_REF,
                "onboarding_julia_context",
            ):
                assert key in catalog


def test_conversao_convite_outro_user_bloqueia(app):
    from app.services.conta_multiuser_capacidade_service import contar_capacidade_comprometida

    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-d-cv", qtd=5, email="f4.dcv.c@test.com"
        )
        invite = criar_convite(
            ator=contratante, email_destino="dest@audit.test", enviar=False
        )
        member = seed_usuario(
            invite.franquia_id, conta.id, email="not-recipient@audit.test", categoria="free"
        )
        with pytest.raises(VinculoInconsistenteError):
            ocupar_assento(
                conta_id=conta.id,
                user_id=member.id,
                franquia_id=invite.franquia_id,
                convite_id_em_conversao=invite.convite_id,
            )
        db.session.rollback()
        assert contar_capacidade_comprometida(conta.id) <= 5
        assert (
            ContaVinculoOrganizacional.query.filter_by(
                user_id=member.id, estado="ativo"
            ).count()
            == 0
        )


def test_conversao_nao_pode_commit_sem_aceitar_convite(app, monkeypatch):
    from app.services.conta_multiuser_capacidade_service import (
        converter_reserva_convite_em_ocupacao,
    )

    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-d-at", qtd=5, email="f4.dat.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4-d-at-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4.dat@test.com", categoria="free")
        invite = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        convite_id = invite.convite_id
        alvo.conta_id = conta.id
        alvo.franquia_id = invite.franquia_id
        db.session.add(alvo)
        db.session.commit()
        visto = {}
        real_commit = db.session.commit

        def _commit():
            row = db.session.get(ContaMultiuserConvite, convite_id)
            visto["estado"] = None if row is None else row.estado
            visto["vinculos"] = ContaVinculoOrganizacional.query.filter_by(
                user_id=alvo.id, estado="ativo"
            ).count()
            return real_commit()

        monkeypatch.setattr(db.session, "commit", _commit)
        converter_reserva_convite_em_ocupacao(
            convite_id=convite_id, user_id=alvo.id, commit=True
        )
        row = db.session.get(ContaMultiuserConvite, convite_id)
        assert visto.get("estado") == "aceito"
        assert visto.get("vinculos") == 1
        assert row is not None
        assert row.estado == "aceito"
        assert row.accepted_user_id == alvo.id
        assert row.accepted_at is not None


def test_conversao_legitima_e_atomica(app):
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-d-lg", qtd=5, email="f4.dlg.c@test.com"
        )
        for i in range(3):
            criar_convite(
                ator=contratante, email_destino=f"held{i}@audit.test", enviar=False
            )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4-d-lg-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4.dlg@test.com", categoria="free")
        invite = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        csrf = gerar_csrf_token_aceite(alvo.id, invite.convite_id)
        aceitar_convite(user=alvo, token=invite.token, csrf_token=csrf)
        row = db.session.get(ContaMultiuserConvite, invite.convite_id)
        assert row is not None
        assert row.estado == "aceito"
        assert row.accepted_user_id == alvo.id
        ativos = ContaVinculoOrganizacional.query.filter_by(
            conta_id=conta.id, estado="ativo"
        ).count()
        reservas = ContaMultiuserConvite.query.filter_by(
            conta_id=conta.id, estado="pendente"
        ).count()
        assert ativos == 2
        assert reservas == 3
        assert ativos + reservas == 5


def test_admin_quantity_menor_que_comprometido_falha(app):
    from app.services.conta_multiuser_capacidade_service import (
        contar_capacidade_comprometida,
    )

    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-d-q3", qtd=5, email="f4.dq3.c@test.com"
        )
        for i in range(3):
            criar_convite(
                ator=contratante, email_destino=f"adminhold{i}@audit.test", enviar=False
            )
        criar_convite(
            ator=contratante, email_destino="adminholdx@audit.test", enviar=False
        )
        anterior = conta.quantidade_assentos_contratados
        comprometido = contar_capacidade_comprometida(conta.id)
        assert comprometido == 5
        try:
            atribuir_plano_para_usuario(
                email=contratante.email, plano_raw="multiuser", quantidade_franquias_raw="3"
            )
        except (CapacidadeEsgotadaError, DivergenciaImpeditivaError):
            db.session.rollback()
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == anterior
        assert contar_capacidade_comprometida(conta.id) <= conta.quantidade_assentos_contratados


def test_admin_quantity_igual_comprometido_permite(app):
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-d-qeq", qtd=5, email="f4.dqeq.c@test.com"
        )
        for i in range(3):
            criar_convite(
                ator=contratante, email_destino=f"eqhold{i}@audit.test", enviar=False
            )
        criar_convite(
            ator=contratante, email_destino="eqholdx@audit.test", enviar=False
        )
        atribuir_plano_para_usuario(
            email=contratante.email, plano_raw="multiuser", quantidade_franquias_raw="5"
        )
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 5


def test_admin_quantity_rollback_preserva_quantity_anterior(app):
    from app.services.conta_multiuser_capacidade_service import (
        contar_capacidade_comprometida,
        validar_quantity_nao_inferior_ao_comprometido,
    )

    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-d-qrb", qtd=5, email="f4.dqrb.c@test.com"
        )
        for i in range(4):
            criar_convite(
                ator=contratante, email_destino=f"rbhold{i}@audit.test", enviar=False
            )
        anterior = conta.quantidade_assentos_contratados
        with pytest.raises(DivergenciaImpeditivaError):
            validar_quantity_nao_inferior_ao_comprometido(conta, 3)
        db.session.rollback()
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == anterior
        assert contar_capacidade_comprometida(conta.id) == 5


def test_erro_interno_resolver_financeiro_bloqueia_transferencia(app, monkeypatch):
    from app.services import cleiton_monetizacao_service as money

    with app.app_context():
        _secret(app)
        _conta, contratante = _preparar_conta_multiuser(
            "f4-d-fin", qtd=3, email="f4.dfin.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4-d-fin-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4.dfin@test.com", categoria="starter")
        row = _monetizacao_paga(origem.id, "starter")
        original = alvo.conta_id
        monkeypatch.setattr(
            money,
            "_fato_corresponde_vinculo_stripe",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("technical failure")),
        )
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        with pytest.raises((ConviteContratoIncompativelError, RuntimeError)):
            aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.rollback()
        db.session.refresh(alvo)
        assert alvo.conta_id == original
        _ = row


def test_decisao_financeira_inconclusiva_nao_equivale_free(app, monkeypatch):
    from app.services import cleiton_monetizacao_service as money
    from app.services.conta_multiuser_convite_service import (
        BENEFICIO_INCONCLUSIVO,
        SEM_BENEFICIO_CONCLUSIVAMENTE,
        decidir_beneficio_pago_para_transferencia,
    )

    with app.app_context():
        _secret(app)
        origem, fr_o = seed_conta_franquia_cliente(slug="f4-d-inc-o")
        seed_usuario(fr_o.id, origem.id, email="f4.dinc@test.com", categoria="starter")
        _monetizacao_paga(origem.id, "starter")
        monkeypatch.setattr(
            money,
            "resolver_falha_mensal_vigente_conta",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("financial unavailable")),
        )
        decisao = decidir_beneficio_pago_para_transferencia(origem.id)
        assert decisao == BENEFICIO_INCONCLUSIVO
        assert decisao != SEM_BENEFICIO_CONCLUSIVAMENTE


def test_sem_beneficio_conclusivo_permite_free_real(app):
    from app.services.conta_multiuser_convite_service import (
        SEM_BENEFICIO_CONCLUSIVAMENTE,
        decidir_beneficio_pago_para_transferencia,
    )

    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-d-free", qtd=3, email="f4.dfree.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4-d-free-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4.dfree@test.com", categoria="free")
        assert decidir_beneficio_pago_para_transferencia(origem.id) == SEM_BENEFICIO_CONCLUSIVAMENTE
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.refresh(alvo)
        assert alvo.conta_id == conta.id


def test_constraint_name_exato_email_e_reconhecido():
    from app.services import conta_multiuser_convite_service as svc
    from app.services.conta_organizacional_rules import UQ_CONVITE_CONTA_EMAIL_PENDENTE

    exc = IntegrityError("INSERT", {}, Exception(UQ_CONVITE_CONTA_EMAIL_PENDENTE))
    assert svc._identificar_constraint_convite(exc) == UQ_CONVITE_CONTA_EMAIL_PENDENTE


def test_constraint_name_exato_franquia_e_reconhecido():
    from app.services import conta_multiuser_convite_service as svc
    from app.services.conta_organizacional_rules import UQ_CONVITE_FRANQUIA_PENDENTE

    exc = IntegrityError("INSERT", {}, Exception(UQ_CONVITE_FRANQUIA_PENDENTE))
    assert svc._identificar_constraint_convite(exc) == UQ_CONVITE_FRANQUIA_PENDENTE


def test_constraint_prefixo_semelhante_e_relangado():
    from app.services import conta_multiuser_convite_service as svc

    exc = IntegrityError(
        "INSERT", {}, Exception("prefix_uq_conta_convite_franquia_pendente")
    )
    assert svc._identificar_constraint_convite(exc) is None


def test_constraint_sufixo_semelhante_e_relangado():
    from app.services import conta_multiuser_convite_service as svc

    exc = IntegrityError(
        "INSERT",
        {},
        Exception("UNIQUE constraint failed: uq_conta_convite_franquia_pendente_unrelated"),
    )
    assert svc._identificar_constraint_convite(exc) is None


def test_network_exception_com_email_nao_vaza_no_log(monkeypatch, caplog):
    import app.auth_services as auth

    secret = "private-person@audit.test"
    monkeypatch.setenv("RESEND_API_KEY", "audit-fake")
    monkeypatch.setattr(
        auth.requests,
        "post",
        lambda *a, **kw: (_ for _ in ()).throw(auth.requests.RequestException(secret)),
    )
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError):
        auth.send_email("recipient@audit.test", "private subject", "html")
    assert secret not in caplog.text
    assert "recipient@audit.test" not in caplog.text


def test_network_exception_com_url_token_nao_vaza_no_log(monkeypatch, caplog):
    import app.auth_services as auth

    secret = "http://host/convite/SECRET_TOKEN"
    monkeypatch.setenv("RESEND_API_KEY", "audit-fake")
    monkeypatch.setattr(
        auth.requests,
        "post",
        lambda *a, **kw: (_ for _ in ()).throw(auth.requests.RequestException(secret)),
    )
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError):
        auth.send_email("recipient@audit.test", "Convite", "html")
    assert secret not in caplog.text
    assert "SECRET_TOKEN" not in caplog.text


def test_request_id_malicioso_nao_e_logado(monkeypatch, caplog):
    import app.auth_services as auth

    secret = "private-person@audit.test /convite/SECRET_TOKEN"
    monkeypatch.setenv("RESEND_API_KEY", "audit-fake")
    monkeypatch.setattr(
        auth.requests,
        "post",
        lambda *a, **kw: SimpleNamespace(
            status_code=422, headers={"x-request-id": secret}, text="RAW_PROVIDER_BODY"
        ),
    )
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError):
        auth.send_email("recipient@audit.test", "private subject", "html")
    assert secret not in caplog.text
    assert "SECRET_TOKEN" not in caplog.text
    assert "RAW_PROVIDER_BODY" not in caplog.text


def test_request_id_seguro_limitado_pode_ser_logado(monkeypatch, caplog):
    import app.auth_services as auth

    monkeypatch.setenv("RESEND_API_KEY", "audit-fake")
    monkeypatch.setattr(
        auth.requests,
        "post",
        lambda *a, **kw: SimpleNamespace(
            status_code=422, headers={"x-request-id": "req_safe_01"}, text="body"
        ),
    )
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError):
        auth.send_email("recipient@audit.test", "subject", "html")
    assert "req_safe_01" not in caplog.text
    assert "request_id=" not in caplog.text
    assert "status_code=422" in caplog.text
    assert "provider=resend" in caplog.text
    assert "category=http_error" in caplog.text


def test_caller_nao_reloga_causa_sensivel(app, monkeypatch, caplog):
    import app.auth_services as auth

    secret = "leaked-user@audit.test /convite/SECRET_TOKEN"
    monkeypatch.setenv("RESEND_API_KEY", "audit-fake")

    def boom(*_a, **_kw):
        raise auth.requests.RequestException(secret)

    monkeypatch.setattr(auth.requests, "post", boom)
    with app.app_context():
        _secret(app)
        _conta, contratante = _preparar_conta_multiuser(
            "f4-d-log", qtd=3, email="f4.dlog.c@test.com"
        )
        with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError):
            criar_convite(
                ator=contratante,
                email_destino="leaked-user@audit.test",
                enviar=True,
                build_invite_url=lambda token: f"http://host/convite/{token}",
            )
    assert secret not in caplog.text
    assert "leaked-user@audit.test" not in caplog.text
    assert "SECRET_TOKEN" not in caplog.text
    assert "/convite/" not in caplog.text


# --- Terceira correção F4 (COR-F4E) ---


def test_cleide_dataset_context_cross_conta_vira_miss(app):
    from app.cleide_contracts import get_cleide_dataset_context, set_cleide_dataset_context

    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4e-cl-ds", qtd=3, email="f4e.clds.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4e-cl-ds-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4e.clds@test.com", categoria="free")
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        _build_client(app)
        with app.test_request_context("/"):
            login_user(alvo)
            set_cleide_dataset_context(
                session,
                {
                    "operational_context": {
                        "dataset_summary": {"source": "OLD_ACCOUNT_A_SECRET"}
                    }
                },
            )
            saved = dict(session)
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.refresh(alvo)
        with app.test_request_context("/"):
            session.update(saved)
            login_user(alvo)
            assert get_cleide_dataset_context(session) is None


def test_cleide_chat_context_nao_incorpora_dataset_antigo(app):
    from app.cleide_chat_context import get_cleide_chat_context
    from app.cleide_contracts import set_cleide_dataset_context

    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4e-cl-ch", qtd=3, email="f4e.clch.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4e-cl-ch-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4e.clch@test.com", categoria="free")
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        _build_client(app)
        with app.test_request_context("/"):
            login_user(alvo)
            set_cleide_dataset_context(
                session,
                {
                    "operational_context": {
                        "dataset_summary": {"source": "OLD_ACCOUNT_A_SECRET"},
                        "kpis": {"valor_total": 123456},
                    }
                },
            )
            saved = dict(session)
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.refresh(alvo)
        with app.test_request_context("/"):
            session.update(saved)
            login_user(alvo)
            assert "OLD_ACCOUNT_A_SECRET" not in str(get_cleide_chat_context(session))


def test_compara_comparison_state_cross_conta_vira_miss(app):
    from app.agente_compara_comparison_state import (
        create_comparison,
        get_comparison_state,
        persist_comparison_state,
    )

    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4e-cp-st", qtd=3, email="f4e.cpst.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4e-cp-st-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4e.cpst@test.com", categoria="free")
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        _build_client(app)
        with app.test_request_context("/"):
            login_user(alvo)
            state = create_comparison(session_obj=session)
            for table in state["tables"].values():
                table["carrier_name"] = "OLD_ACCOUNT_A_SECRET"
            persist_comparison_state(state, session_obj=session)
            saved = dict(session)
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.refresh(alvo)
        with app.test_request_context("/"):
            session.update(saved)
            login_user(alvo)
            assert get_comparison_state(session) is None


def test_compara_segunda_sessao_nao_retorna_estado_antigo(app):
    from app.agente_compara_api_routes import agente_compara_api_bp
    from app.agente_compara_comparison_state import create_comparison, persist_comparison_state

    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4e-cp-s2", qtd=3, email="f4e.cps2.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4e-cp-s2-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4e.cps2@test.com", categoria="free")
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        client = _build_client(app)
        if "agente_compara_api" not in app.blueprints:
            app.register_blueprint(agente_compara_api_bp)
        if "index" not in app.view_functions:
            @app.route("/", endpoint="index")
            def index():
                return "index"

        with app.test_request_context("/"):
            login_user(alvo)
            state = create_comparison(session_obj=session)
            for table in state["tables"].values():
                table["carrier_name"] = "OLD_ACCOUNT_A_SECRET"
            persist_comparison_state(state, session_obj=session)
            saved = dict(session)
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.refresh(alvo)
        second = app.test_client()
        with second.session_transaction() as sess:
            sess.update(saved)
        response = second.post("/api/agente-compara/comparison/start", json={})
        assert response.status_code == 200
        assert "OLD_ACCOUNT_A_SECRET" not in response.get_data(as_text=True)


def test_roberto_upload_ref_cross_conta_bloqueia(app, monkeypatch, tmp_path):
    import app.roberto_upload_store as store
    from app.upload_handler import get_dados_upload_cliente

    monkeypatch.setattr(store, "_base_dir", lambda: str(tmp_path))
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4e-rb-cr", qtd=3, email="f4e.rbcr.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4e-rb-cr-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4e.rbcr@test.com", categoria="free")
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        _build_client(app)
        with app.test_request_context("/"):
            login_user(alvo)
            ref = store.save_upload_data([{"carrier": "OLD_ACCOUNT_A_ROBERTO"}])
            session["roberto_upload_ref"] = ref
            saved = dict(session)
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.refresh(alvo)
        with app.test_request_context("/"):
            session.update(saved)
            login_user(alvo)
            assert "OLD_ACCOUNT_A_ROBERTO" not in str(get_dados_upload_cliente())


def test_roberto_upload_ref_legado_sem_scope_bloqueia(app, monkeypatch, tmp_path):
    import app.roberto_upload_store as store
    from app.upload_handler import get_dados_upload_cliente

    monkeypatch.setattr(store, "_base_dir", lambda: str(tmp_path))
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4e-rb-lg", qtd=3, email="f4e.rblg.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4e-rb-lg-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4e.rblg@test.com", categoria="free")
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        _build_client(app)
        with app.test_request_context("/"):
            login_user(alvo)
            ref = store.save_upload_data([{"carrier": "OLD_ACCOUNT_A_ROBERTO"}])
            path = store._file_path(ref)
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            for field in ("conta_id", "franquia_id", "usuario_id"):
                payload.pop(field, None)
            store._write_json_atomic(path, payload)
            session["roberto_upload_ref"] = ref
            saved = dict(session)
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.refresh(alvo)
        with app.test_request_context("/"):
            session.update(saved)
            login_user(alvo)
            assert get_dados_upload_cliente() is None


def test_onboarding_julia_context_classificado_e_protegido(app):
    from app.cleiton_doc_escopo import (
        OPERATIONAL_SESSION_KEYS,
        current_operational_scope,
        invalidate_session_document_refs,
        stamp_operational_cache_payload,
    )

    assert "onboarding_julia_context" in OPERATIONAL_SESSION_KEYS
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4e-ob", qtd=3, email="f4e.ob.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4e-ob-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4e.ob@test.com", categoria="free")
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        _build_client(app)
        with app.test_request_context("/"):
            login_user(alvo)
            session["onboarding_julia_context"] = stamp_operational_cache_payload(
                {
                    "source": "onboarding_discovery",
                    "user_message": "mensagem operacional da Conta A",
                    "summary": "resumo A",
                }
            )
            saved = dict(session)
            invalidate_session_document_refs(session)
            assert "onboarding_julia_context" not in session
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.refresh(alvo)
        with app.test_request_context("/"):
            session.update(saved)
            login_user(alvo)
            raw = session.get("onboarding_julia_context")
            assert isinstance(raw, dict)
            scope = current_operational_scope()
            assert scope is not None
            assert int(raw.get("conta_id")) != int(scope["conta_id"]) or int(
                raw.get("franquia_id")
            ) != int(scope["franquia_id"])


def test_persistir_quantity_bloqueia_valor_menor_que_comprometido(app):
    from app.services.conta_multiuser_capacidade_service import contar_capacidade_comprometida
    from app.services.conta_organizacional_service import persistir_quantidade_assentos_contratados

    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4e-q-bl", qtd=6, email="f4e.qbl.c@test.com"
        )
        for i in range(5):
            criar_convite(
                ator=contratante, email_destino=f"qbl{i}@audit.test", enviar=False
            )
        anterior = conta.quantidade_assentos_contratados
        comprometido = contar_capacidade_comprometida(conta.id)
        assert comprometido == 6
        with pytest.raises(DivergenciaImpeditivaError):
            persistir_quantidade_assentos_contratados(conta.id, 5)
        db.session.rollback()
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == anterior
        assert contar_capacidade_comprometida(conta.id) <= conta.quantidade_assentos_contratados


def test_renovacao_nao_reduz_quantity_abaixo_do_comprometido(app):
    from app.services.conta_multiuser_capacidade_service import contar_capacidade_comprometida
    from app.services.conta_multiuser_contratacao_service import (
        ativar_beneficio_multiuser_confirmado,
    )
    from tests.test_fase3_multiuser_contratacao import CNPJ_A, _seed_gateway_multiuser

    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4e-q-rn", qtd=6, email="f4e.qrn.c@test.com"
        )
        _seed_gateway_multiuser(price_id="price_audit")
        conta.cnpj = CNPJ_A
        db.session.commit()
        for i in range(5):
            criar_convite(
                ator=contratante, email_destino=f"qrn{i}@audit.test", enviar=False
            )
        assert contar_capacidade_comprometida(conta.id) == 6
        now = utcnow_naive()
        try:
            ativar_beneficio_multiuser_confirmado(
                conta_id=conta.id,
                user_id=contratante.id,
                quantity_stripe=5,
                quantity_solicitada=5,
                inicio_ciclo=now,
                fim_ciclo=now + timedelta(days=30),
                price_id="price_audit",
                invoice_id="in_f4e_renov",
                commit=True,
            )
        except DivergenciaImpeditivaError:
            db.session.rollback()
        db.session.refresh(conta)
        assert contar_capacidade_comprometida(conta.id) <= conta.quantidade_assentos_contratados
        assert conta.quantidade_assentos_contratados == 6


def test_invoice_paid_quantity_menor_preserva_invariante(app):
    from app.services.cleiton_monetizacao_service import _aplicar_fato_contratual_multiuser
    from app.services.conta_multiuser_capacidade_service import contar_capacidade_comprometida
    from tests.test_fase3_multiuser_contratacao import (
        CNPJ_B,
        INICIO,
        FIM,
        PRICE_MU,
        _evento_invoice_paid,
        _seed_gateway_multiuser,
    )

    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4e-q-in", qtd=6, email="f4e.qin.c@test.com"
        )
        _seed_gateway_multiuser()
        conta.cnpj = CNPJ_B
        db.session.commit()
        for i in range(5):
            criar_convite(
                ator=contratante, email_destino=f"qin{i}@audit.test", enviar=False
            )
        anterior = conta.quantidade_assentos_contratados
        evento = _evento_invoice_paid(
            conta_id=conta.id,
            franquia_id=contratante.franquia_id,
            user_id=contratante.id,
            invoice_id="in_f4e_inv",
            event_id="evt_f4e_inv",
            quantity=5,
            price_id=PRICE_MU,
            correlation_id="corr_f4e_inv",
            inicio=INICIO + timedelta(days=31),
            fim=FIM + timedelta(days=31),
        )
        object_data = evento["data"]["object"]
        resultado = _aplicar_fato_contratual_multiuser(
            franquia_id=int(contratante.franquia_id),
            event_type="invoice.paid",
            status_contratual_externo="active",
            ciclo={
                "inicio_ciclo": INICIO + timedelta(days=31),
                "fim_ciclo": FIM + timedelta(days=31),
                "evento_atrasado_canonico": False,
            },
            object_data=object_data,
            ids={
                "invoice_id": "in_f4e_inv",
                "price_id": PRICE_MU,
                "customer_id": object_data.get("customer"),
                "subscription_id": object_data.get("subscription"),
            },
        )
        db.session.rollback()
        db.session.refresh(conta)
        assert resultado.get("aplicado") is False
        assert conta.quantidade_assentos_contratados == anterior
        assert contar_capacidade_comprometida(conta.id) <= conta.quantidade_assentos_contratados


def test_quantity_igual_comprometido_permite(app):
    from app.services.conta_organizacional_service import persistir_quantidade_assentos_contratados

    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4e-q-eq", qtd=5, email="f4e.qeq.c@test.com"
        )
        for i in range(4):
            criar_convite(
                ator=contratante, email_destino=f"qeq{i}@audit.test", enviar=False
            )
        persistir_quantidade_assentos_contratados(conta.id, 5)
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 5


def test_quantity_maior_comprometido_permite(app):
    from app.services.conta_organizacional_service import persistir_quantidade_assentos_contratados

    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4e-q-gt", qtd=5, email="f4e.qgt.c@test.com"
        )
        persistir_quantidade_assentos_contratados(conta.id, 7)
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 7


def test_downgrade_query_exception_vira_inconclusivo(app, monkeypatch):
    from app.services import cleiton_monetizacao_service as money
    from app.services.conta_multiuser_convite_service import (
        BENEFICIO_INCONCLUSIVO,
        decidir_beneficio_pago_para_transferencia,
    )

    with app.app_context():
        _secret(app)
        origem, fr_o = seed_conta_franquia_cliente(slug="f4e-dg-q")
        seed_usuario(fr_o.id, origem.id, email="f4e.dgq@test.com", categoria="starter")
        _monetizacao_paga(origem.id, "starter")
        monkeypatch.setattr(
            money,
            "obter_pendencia_downgrade_conta_ativa",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("AUDIT_DOWNGRADE")),
        )
        assert decidir_beneficio_pago_para_transferencia(origem.id) == BENEFICIO_INCONCLUSIVO


def test_aceite_financeiro_inconclusivo_bloqueia(app, monkeypatch):
    from app.services.conta_multiuser_convite_service import (
        BENEFICIO_INCONCLUSIVO,
        decidir_beneficio_pago_para_transferencia,
    )

    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4e-ac-inc", qtd=3, email="f4e.acinc.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4e-ac-inc-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4e.acinc@test.com", categoria="free")
        original = alvo.conta_id
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        monkeypatch.setattr(
            "app.services.conta_multiuser_convite_service.decidir_beneficio_pago_para_transferencia",
            lambda *a, **kw: BENEFICIO_INCONCLUSIVO,
        )
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        with pytest.raises((ConviteContratoIncompativelError, RuntimeError)):
            aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.rollback()
        db.session.refresh(alvo)
        assert alvo.conta_id == original
        assert decidir_beneficio_pago_para_transferencia is not None


def test_tri_state_e_autoridade_do_aceite(app, monkeypatch):
    from app.services.conta_multiuser_convite_service import (
        BENEFICIO_INCONCLUSIVO,
        BENEFICIO_PAGO_VIGENTE,
        SEM_BENEFICIO_CONCLUSIVAMENTE,
        decidir_beneficio_pago_para_transferencia,
    )

    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4e-tri", qtd=3, email="f4e.tri.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4e-tri-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4e.tri@test.com", categoria="free")
        original = alvo.conta_id
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        source = inspect.getsource(aceitar_convite)
        assert "decidir_beneficio_pago_para_transferencia" in source
        assert "contrato_pago_incompativel(user_locked)" not in source
        monkeypatch.setattr(
            "app.services.conta_multiuser_convite_service.decidir_beneficio_pago_para_transferencia",
            lambda *a, **kw: BENEFICIO_PAGO_VIGENTE,
        )
        with pytest.raises(ConviteContratoIncompativelError):
            aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.rollback()
        db.session.refresh(alvo)
        assert alvo.conta_id == original
        monkeypatch.setattr(
            "app.services.conta_multiuser_convite_service.decidir_beneficio_pago_para_transferencia",
            lambda *a, **kw: SEM_BENEFICIO_CONCLUSIVAMENTE,
        )
        aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.refresh(alvo)
        assert alvo.conta_id == conta.id
        assert BENEFICIO_INCONCLUSIVO
        assert decidir_beneficio_pago_para_transferencia is not None


def test_subconsulta_financeira_nao_converte_exception_em_none_no_strict(app, monkeypatch):
    from app.services import cleiton_monetizacao_service as money

    with app.app_context():
        _secret(app)
        origem, fr_o = seed_conta_franquia_cliente(slug="f4e-st-none")
        _monetizacao_paga(origem.id, "starter")
        monkeypatch.setattr(
            money,
            "_obter_vinculo_ativo_por_conta",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("AUDIT_STRICT")),
        )
        assert money.obter_pendencia_downgrade_conta_ativa(origem.id, strict=False) is None
        with pytest.raises(RuntimeError):
            money.obter_pendencia_downgrade_conta_ativa(origem.id, strict=True)


def test_header_token_alfanumerico_nao_e_logado(monkeypatch, caplog):
    import app.auth_services as auth

    monkeypatch.setenv("RESEND_API_KEY", "audit-fake")
    monkeypatch.setattr(
        auth.requests,
        "post",
        lambda *a, **kw: SimpleNamespace(
            status_code=422, headers={"x-request-id": "SENSITIVE_TOKEN"}, text="body"
        ),
    )
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError):
        auth.send_email("recipient@audit.test", "subject", "html")
    assert "SENSITIVE_TOKEN" not in caplog.text
    assert "request_id=" not in caplog.text


def test_header_crlf_nao_e_normalizado_nem_logado(monkeypatch, caplog):
    import app.auth_services as auth

    monkeypatch.setenv("RESEND_API_KEY", "audit-fake")
    monkeypatch.setattr(
        auth.requests,
        "post",
        lambda *a, **kw: SimpleNamespace(
            status_code=422, headers={"x-request-id": "\r\nreq_safe\r\n"}, text="body"
        ),
    )
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError):
        auth.send_email("recipient@audit.test", "subject", "html")
    assert "req_safe" not in caplog.text
    assert "request_id=" not in caplog.text


def test_header_url_nao_e_logado(monkeypatch, caplog):
    import app.auth_services as auth

    monkeypatch.setenv("RESEND_API_KEY", "audit-fake")
    monkeypatch.setattr(
        auth.requests,
        "post",
        lambda *a, **kw: SimpleNamespace(
            status_code=422,
            headers={"x-request-id": "https://SENSITIVE_URL"},
            text="body",
        ),
    )
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError):
        auth.send_email("recipient@audit.test", "subject", "html")
    assert "SENSITIVE_URL" not in caplog.text
    assert "request_id=" not in caplog.text


def test_send_email_http_error_log_so_campos_controlados(monkeypatch, caplog):
    import app.auth_services as auth

    monkeypatch.setenv("RESEND_API_KEY", "audit-fake")
    monkeypatch.setattr(
        auth.requests,
        "post",
        lambda *a, **kw: SimpleNamespace(
            status_code=503,
            headers={"x-request-id": "SENSITIVE_TOKEN"},
            text="SENSITIVE_EMAIL SENSITIVE_TOKEN SENSITIVE_URL",
        ),
    )
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError):
        auth.send_email("SENSITIVE_EMAIL@audit.test", "SENSITIVE_TOKEN", "SENSITIVE_URL")
    assert "provider=resend" in caplog.text
    assert "category=http_error" in caplog.text
    assert "status_code=503" in caplog.text
    assert "request_id=" not in caplog.text
    for sentinel in ("SENSITIVE_EMAIL", "SENSITIVE_TOKEN", "SENSITIVE_URL"):
        assert sentinel not in caplog.text


# --- RC-01 Cleide upload bruto ---


def _preparar_cleide_upload_cross_conta(app, monkeypatch, tmp_path, *, tag: str):
    import app.cleide_upload_store as store
    from app.cleide_contracts import set_cleide_upload_ref
    from app.cleide_routes import cleide_bp
    from flask_login import login_user
    from werkzeug.datastructures import FileStorage

    monkeypatch.setattr(store, "get_cleide_upload_tmp_dir", lambda: str(tmp_path))
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            f"f4f-cl-{tag}", qtd=3, email=f"f4f.cl{tag}.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug=f"f4f-cl-{tag}-o")
        alvo = seed_usuario(fr_o.id, origem.id, email=f"f4f.cl{tag}@test.com", categoria="free")
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        client = _build_client(app)
        if "cleide" not in app.blueprints:
            app.register_blueprint(cleide_bp)
        if "index" not in app.view_functions:
            @app.route("/", endpoint="index")
            def index():
                return "index"

        with app.test_request_context("/"):
            login_user(alvo)
            saved = store.save_cleide_upload_file(
                file_storage=FileStorage(
                    stream=io.BytesIO(b"carrier,value\nACCOUNT_A_SECRET,123\n"),
                    filename="a.csv",
                ),
                safe_filename="a.csv",
            )
            set_cleide_upload_ref(session, saved["upload_ref"])
            old_session = dict(session)
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        with client.session_transaction() as sess:
            sess.update(old_session)
        response = client.post(
            f"/convite/{out.token}/aceitar",
            data={"csrf_token": csrf},
        )
        assert response.status_code == 302
        db.session.refresh(alvo)
        assert alvo.conta_id == conta.id
        return alvo, saved, old_session, origem


def test_cleide_upload_ref_cross_conta_nao_le(app, monkeypatch, tmp_path):
    import app.cleide_upload_store as store
    from flask_login import login_user

    alvo, saved, old_session, _origem = _preparar_cleide_upload_cross_conta(
        app, monkeypatch, tmp_path, tag="nl"
    )
    with app.test_request_context("/"):
        session.update(old_session)
        login_user(alvo)
        resolved = store.resolve_cleide_upload_file(saved["upload_ref"])
        assert resolved is None


def test_cleide_clear_cross_conta_nao_exclui(app, monkeypatch, tmp_path):
    import app.cleide_upload_store as store
    from flask_login import login_user

    alvo, saved, old_session, _origem = _preparar_cleide_upload_cross_conta(
        app, monkeypatch, tmp_path, tag="cl"
    )
    path = Path(saved["absolute_path"])
    assert path.exists()
    with app.test_request_context("/"):
        session.update(old_session)
        login_user(alvo)
        store.clear_cleide_upload_file(saved["upload_ref"])
        assert path.exists()
        assert store.resolve_cleide_upload_file(saved["upload_ref"]) is None


def test_cleide_segunda_sessao_antiga_nao_exclui_upload(app, monkeypatch, tmp_path):
    _alvo, saved, old_session, _origem = _preparar_cleide_upload_cross_conta(
        app, monkeypatch, tmp_path, tag="s2"
    )
    path = Path(saved["absolute_path"])
    assert path.exists()
    second = app.test_client()
    with second.session_transaction() as sess:
        sess.update(old_session)
    response = second.post("/api/cleide/upload/clear", json={})
    assert path.exists(), (
        f"Sessao antiga excluiu upload de A: HTTP {response.status_code}, {response.get_json()}"
    )
    assert b"ACCOUNT_A_SECRET" in path.read_bytes()


def test_cleide_upload_legado_sem_scope_fail_closed(app, monkeypatch, tmp_path):
    import app.cleide_upload_store as store
    from flask_login import login_user

    alvo, saved, old_session, _origem = _preparar_cleide_upload_cross_conta(
        app, monkeypatch, tmp_path, tag="lg"
    )
    scope_path = Path(tmp_path) / f"{saved['upload_ref']}.scope.json"
    if scope_path.exists():
        scope_path.unlink()
    path = Path(saved["absolute_path"])
    assert path.exists()
    with app.test_request_context("/"):
        session.update(old_session)
        login_user(alvo)
        assert store.resolve_cleide_upload_file(saved["upload_ref"]) is None
        store.clear_cleide_upload_file(saved["upload_ref"])
        assert path.exists()


# --- RC-01 Roberto MISS/delete ---


def test_roberto_scope_mismatch_nao_exclui_arquivo(app, monkeypatch, tmp_path):
    from flask_login import login_user
    import app.roberto_upload_store as store
    from app.upload_handler import get_dados_upload_cliente

    monkeypatch.setattr(store, "_base_dir", lambda: str(tmp_path))
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4f-rb-mm", qtd=3, email="f4f.rbmm.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4f-rb-mm-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4f.rbmm@test.com", categoria="free")
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        _build_client(app)
        with app.test_request_context("/"):
            login_user(alvo)
            ref = store.save_upload_data([{"carrier": "ACCOUNT_A_ROBERTO_SECRET"}])
            session["roberto_upload_ref"] = ref
            old_session = dict(session)
            path = Path(store._file_path(ref))
            assert path.exists()
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.refresh(alvo)
        assert alvo.conta_id == conta.id
        with app.test_request_context("/"):
            session.update(old_session)
            login_user(alvo)
            outcome = store.inspect_upload_data(ref, 30)
            assert outcome.status == store.UPLOAD_SCOPE_MISMATCH
            assert get_dados_upload_cliente() is None
            assert path.exists()


def test_roberto_ref_antiga_nao_remove_upload_da_conta_origem(app, monkeypatch, tmp_path):
    from flask_login import login_user
    import app.roberto_upload_store as store
    from app.upload_handler import get_dados_upload_cliente

    monkeypatch.setattr(store, "_base_dir", lambda: str(tmp_path))
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4f-rb-or", qtd=3, email="f4f.rbor.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4f-rb-or-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4f.rbor@test.com", categoria="free")
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        _build_client(app)
        with app.test_request_context("/"):
            login_user(alvo)
            ref = store.save_upload_data([{"carrier": "OLD_ACCOUNT_A_ROBERTO"}])
            session["roberto_upload_ref"] = ref
            old_session = dict(session)
            path = Path(store._file_path(ref))
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.refresh(alvo)
        assert alvo.conta_id == conta.id
        with app.test_request_context("/"):
            session.update(old_session)
            login_user(alvo)
            store.clear_upload_data(ref)
            assert get_dados_upload_cliente() is None
            assert path.exists()
            assert "OLD_ACCOUNT_A_ROBERTO" in path.read_text(encoding="utf-8")


def test_roberto_legado_sem_scope_nao_le_nem_exclui(app, monkeypatch, tmp_path):
    from flask_login import login_user
    import app.roberto_upload_store as store
    from app.upload_handler import get_dados_upload_cliente

    monkeypatch.setattr(store, "_base_dir", lambda: str(tmp_path))
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4f-rb-lg", qtd=3, email="f4f.rblg2.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4f-rb-lg2-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4f.rblg2@test.com", categoria="free")
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        _build_client(app)
        with app.test_request_context("/"):
            login_user(alvo)
            ref = store.save_upload_data([{"carrier": "LEGACY_NO_SCOPE"}])
            path = store._file_path(ref)
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            for field in ("conta_id", "franquia_id", "usuario_id"):
                payload.pop(field, None)
            store._write_json_atomic(path, payload)
            session["roberto_upload_ref"] = ref
            old_session = dict(session)
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.refresh(alvo)
        assert alvo.conta_id == conta.id
        with app.test_request_context("/"):
            session.update(old_session)
            login_user(alvo)
            assert get_dados_upload_cliente() is None
            assert Path(path).exists()
            store.clear_upload_data(ref)
            assert Path(path).exists()


# --- RC-02 backfill quantity ---


def test_backfill_quantity_null_nao_inicializa_abaixo_do_comprometido(app):
    from app.services.conta_multiuser_capacidade_service import contar_capacidade_comprometida
    from app.services.conta_organizacional_backfill_service import (
        aplicar_backfill_multiuser_legado,
    )

    with app.app_context():
        seed_sistema_interno()
        conta, fr = seed_conta_franquia_cliente(slug="f4f-bf-q1")
        user = seed_usuario(fr.id, conta.id, email="f4f.bfq1@test.com", categoria="multiuser")
        conta.quantidade_assentos_contratados = None
        db.session.add(
            ContaMultiuserConvite(
                conta_id=conta.id,
                franquia_id=fr.id,
                criado_por_user_id=user.id,
                email_destino="reserva.bfq1@test.com",
                estado=ContaMultiuserConvite.ESTADO_PENDENTE,
                expires_at=utcnow_naive() + timedelta(hours=1),
            )
        )
        db.session.commit()
        aplicar_backfill_multiuser_legado(db.session.connection())
        db.session.commit()
        db.session.refresh(conta)
        comprometido = contar_capacidade_comprometida(conta.id)
        assert conta.quantidade_assentos_contratados is not None
        assert conta.quantidade_assentos_contratados >= comprometido
        assert comprometido >= 2


def test_backfill_quantity_null_com_reservas_preserva_invariante(app):
    from app.services.conta_multiuser_capacidade_service import contar_capacidade_comprometida
    from app.services.conta_organizacional_backfill_service import (
        aplicar_backfill_multiuser_legado,
    )

    with app.app_context():
        seed_sistema_interno()
        conta, fr = seed_conta_franquia_cliente(slug="f4f-bf-rs")
        user = seed_usuario(fr.id, conta.id, email="f4f.bfrs@test.com", categoria="multiuser")
        conta.quantidade_assentos_contratados = None
        db.session.add(
            ContaMultiuserConvite(
                conta_id=conta.id,
                franquia_id=fr.id,
                criado_por_user_id=user.id,
                email_destino="reserva.bfrs@test.com",
                estado=ContaMultiuserConvite.ESTADO_PENDENTE,
                expires_at=utcnow_naive() + timedelta(hours=1),
            )
        )
        db.session.commit()
        aplicar_backfill_multiuser_legado(db.session.connection())
        db.session.commit()
        db.session.refresh(conta)
        assert contar_capacidade_comprometida(conta.id) <= conta.quantidade_assentos_contratados


def test_backfill_nao_altera_quantity_existente(app):
    from app.services.conta_organizacional_backfill_service import (
        aplicar_backfill_multiuser_legado,
    )

    with app.app_context():
        _secret(app)
        conta, _contratante = _preparar_conta_multiuser(
            "f4f-bf-keep", qtd=5, email="f4f.bfkeep.c@test.com"
        )
        anterior = conta.quantidade_assentos_contratados
        assert anterior == 5
        aplicar_backfill_multiuser_legado(db.session.connection())
        db.session.commit()
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == anterior


def test_reconciliacao_runtime_nao_produz_comprometido_maior_que_quantity(app):
    from app.services.conta_multiuser_capacidade_service import contar_capacidade_comprometida
    from app.services.conta_multiuser_reconciliacao_service import (
        executar_reconciliacao_multiuser_antes_enforcement,
    )

    with app.app_context():
        seed_sistema_interno()
        conta, fr = seed_conta_franquia_cliente(slug="f4f-bf-rec")
        user = seed_usuario(fr.id, conta.id, email="f4f.bfrec@test.com", categoria="multiuser")
        conta.quantidade_assentos_contratados = None
        db.session.add(
            ContaMultiuserConvite(
                conta_id=conta.id,
                franquia_id=fr.id,
                criado_por_user_id=user.id,
                email_destino="reserva.bfrec@test.com",
                estado=ContaMultiuserConvite.ESTADO_PENDENTE,
                expires_at=utcnow_naive() + timedelta(hours=1),
            )
        )
        db.session.commit()
        executar_reconciliacao_multiuser_antes_enforcement()
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados is not None
        assert contar_capacidade_comprometida(conta.id) <= conta.quantidade_assentos_contratados


# --- RC-03 parser financeiro fail-closed ---


def _cenario_falha_mensal_vigente(conta_id: int, user: User, plano: str, invoice_id: str):
    from app.services import cleiton_monetizacao_service as money

    row = _monetizacao_paga(conta_id, plano)
    money.processar_evento_stripe(
        {
            "id": f"evt_{invoice_id}",
            "type": "invoice.payment_failed",
            "data": {
                "object": {
                    "id": invoice_id,
                    "customer": row.customer_id,
                    "subscription": row.subscription_id,
                    "billing_reason": "subscription_cycle",
                    "metadata": {
                        "conta_id": str(conta_id),
                        "franquia_id": str(user.franquia_id),
                        "usuario_id": str(user.id),
                        "plano_interno": plano,
                    },
                }
            },
        }
    )
    db.session.refresh(row)
    row.vigencia_externa_fim = utcnow_naive() - timedelta(days=1)
    fr = db.session.get(Franquia, user.franquia_id)
    fr.fim_ciclo = row.vigencia_externa_fim
    db.session.commit()
    return row


def test_parser_financeiro_exception_vira_inconclusivo(app, monkeypatch):
    from app.services import cleiton_monetizacao_service as money
    from app.services.conta_multiuser_convite_service import (
        BENEFICIO_INCONCLUSIVO,
        BENEFICIO_PAGO_VIGENTE,
        decidir_beneficio_pago_para_transferencia,
    )

    with app.app_context():
        _secret(app)
        origem, fr_o = seed_conta_franquia_cliente(slug="f4f-fin-ex")
        user = seed_usuario(fr_o.id, origem.id, email="f4f.finex@test.com", categoria="starter")
        _cenario_falha_mensal_vigente(origem.id, user, "starter", "in_f4f_exc")
        assert decidir_beneficio_pago_para_transferencia(origem.id) == BENEFICIO_PAGO_VIGENTE
        real_loads = money.json.loads
        calls = []

        def broken_invoice_json(raw, *args, **kwargs):
            if isinstance(raw, str) and "in_f4f_exc" in raw:
                calls.append(1)
                raise ValueError("AUDIT_FINANCIAL_JSON_FAILURE")
            return real_loads(raw, *args, **kwargs)

        monkeypatch.setattr(money.json, "loads", broken_invoice_json)
        decision = decidir_beneficio_pago_para_transferencia(origem.id)
        assert calls
        assert decision == BENEFICIO_INCONCLUSIVO


def test_json_financeiro_malformado_nao_vira_sem_beneficio(app):
    from app.models import MonetizacaoFato
    from app.services.conta_multiuser_convite_service import (
        BENEFICIO_INCONCLUSIVO,
        BENEFICIO_PAGO_VIGENTE,
        SEM_BENEFICIO_CONCLUSIVAMENTE,
        decidir_beneficio_pago_para_transferencia,
    )

    with app.app_context():
        _secret(app)
        origem, fr_o = seed_conta_franquia_cliente(slug="f4f-fin-bad")
        user = seed_usuario(fr_o.id, origem.id, email="f4f.finbad@test.com", categoria="starter")
        _cenario_falha_mensal_vigente(origem.id, user, "starter", "in_f4f_bad")
        assert decidir_beneficio_pago_para_transferencia(origem.id) == BENEFICIO_PAGO_VIGENTE
        fato = (
            MonetizacaoFato.query.filter_by(conta_id=origem.id)
            .order_by(MonetizacaoFato.id.desc())
            .first()
        )
        assert fato is not None
        fato.payload_bruto_sanitizado_json = "{not-json"
        db.session.commit()
        decision = decidir_beneficio_pago_para_transferencia(origem.id)
        assert decision == BENEFICIO_INCONCLUSIVO
        assert decision != SEM_BENEFICIO_CONCLUSIVAMENTE


def test_aceite_bloqueia_quando_parser_financeiro_inconclusivo(app, monkeypatch):
    from app.services import cleiton_monetizacao_service as money
    from app.services.conta_multiuser_convite_service import (
        BENEFICIO_INCONCLUSIVO,
        BENEFICIO_PAGO_VIGENTE,
        decidir_beneficio_pago_para_transferencia,
    )

    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4f-fin-ac", qtd=3, email="f4f.finac.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4f-fin-ac-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4f.finac@test.com", categoria="free")
        original = alvo.conta_id
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        _cenario_falha_mensal_vigente(origem.id, alvo, "starter", "in_f4f_ac")
        assert decidir_beneficio_pago_para_transferencia(origem.id) == BENEFICIO_PAGO_VIGENTE
        real_loads = money.json.loads

        def broken_invoice_json(raw, *args, **kwargs):
            if isinstance(raw, str) and "in_f4f_ac" in raw:
                raise ValueError("AUDIT_FINANCIAL_JSON_FAILURE")
            return real_loads(raw, *args, **kwargs)

        monkeypatch.setattr(money.json, "loads", broken_invoice_json)
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        with pytest.raises((ConviteContratoIncompativelError, RuntimeError)):
            aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.rollback()
        db.session.refresh(alvo)
        assert alvo.conta_id == original
        assert decidir_beneficio_pago_para_transferencia(origem.id) == BENEFICIO_INCONCLUSIVO


def test_user_permanece_conta_origem_em_falha_parser(app, monkeypatch):
    from app.services import cleiton_monetizacao_service as money

    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4f-fin-st", qtd=3, email="f4f.finst.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4f-fin-st-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4f.finst@test.com", categoria="free")
        original = alvo.conta_id
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        _cenario_falha_mensal_vigente(origem.id, alvo, "starter", "in_f4f_stay")
        real_loads = money.json.loads

        def broken_invoice_json(raw, *args, **kwargs):
            if isinstance(raw, str) and "in_f4f_stay" in raw:
                raise ValueError("AUDIT_FINANCIAL_JSON_FAILURE")
            return real_loads(raw, *args, **kwargs)

        monkeypatch.setattr(money.json, "loads", broken_invoice_json)
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        with pytest.raises((ConviteContratoIncompativelError, RuntimeError)):
            aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.rollback()
        db.session.refresh(alvo)
        assert alvo.conta_id == original
        assert alvo.conta_id != conta.id


def test_pro_parser_financeiro_inconclusivo_bloqueia(app, monkeypatch):
    from app.services import cleiton_monetizacao_service as money
    from app.services.conta_multiuser_convite_service import (
        BENEFICIO_INCONCLUSIVO,
        BENEFICIO_PAGO_VIGENTE,
        decidir_beneficio_pago_para_transferencia,
    )

    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4f-fin-pro", qtd=3, email="f4f.finpro.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4f-fin-pro-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4f.finpro@test.com", categoria="pro")
        original = alvo.conta_id
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        _cenario_falha_mensal_vigente(origem.id, alvo, "pro", "in_f4f_pro")
        assert decidir_beneficio_pago_para_transferencia(origem.id) == BENEFICIO_PAGO_VIGENTE
        real_loads = money.json.loads

        def broken_invoice_json(raw, *args, **kwargs):
            if isinstance(raw, str) and "in_f4f_pro" in raw:
                raise TypeError("AUDIT_PRO_JSON_TYPE")
            return real_loads(raw, *args, **kwargs)

        monkeypatch.setattr(money.json, "loads", broken_invoice_json)
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        with pytest.raises((ConviteContratoIncompativelError, RuntimeError)):
            aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.rollback()
        db.session.refresh(alvo)
        assert alvo.conta_id == original
        assert decidir_beneficio_pago_para_transferencia(origem.id) == BENEFICIO_INCONCLUSIVO


def test_json_valido_vazio_mantem_semantica_legitima(app):
    from app.services import cleiton_monetizacao_service as money
    from app.services.conta_multiuser_convite_service import (
        BENEFICIO_INCONCLUSIVO,
        SEM_BENEFICIO_CONCLUSIVAMENTE,
        decidir_beneficio_pago_para_transferencia,
    )

    with app.app_context():
        _secret(app)
        assert money._json_loads("{}", strict=True) == {}
        assert money._json_loads(None, strict=True) == {}
        assert money._json_loads("", strict=True) == {}
        origem, fr_o = seed_conta_franquia_cliente(slug="f4f-fin-empty")
        seed_usuario(fr_o.id, origem.id, email="f4f.finempty@test.com", categoria="free")
        row = _monetizacao_paga(origem.id, "starter")
        row.snapshot_normalizado_json = "{}"
        row.vigencia_externa_fim = utcnow_naive() - timedelta(days=1)
        fr = db.session.get(Franquia, fr_o.id)
        fr.fim_ciclo = row.vigencia_externa_fim
        db.session.commit()
        decision = decidir_beneficio_pago_para_transferencia(origem.id)
        assert decision != BENEFICIO_INCONCLUSIVO
        assert decision == SEM_BENEFICIO_CONCLUSIVAMENTE


# --- COR-F4G: sweep temporal Cleide / backfill fail-closed / parsers strict ---


def _cenario_backfill_quantity_e_reservas(existing_quantity):
    seed_sistema_interno()
    conta, fr = seed_conta_franquia_cliente(slug="f4g-bf-cap")
    owner = seed_usuario(fr.id, conta.id, email="f4g.bf.owner@test.com", categoria="multiuser")
    other_fr = Franquia(
        conta_id=conta.id,
        nome="Outra F4G",
        slug="outra-f4g",
        status=Franquia.STATUS_ACTIVE,
    )
    db.session.add(other_fr)
    db.session.commit()
    seed_usuario(other_fr.id, conta.id, email="f4g.bf.second@test.com", categoria="multiuser")
    conta.quantidade_assentos_contratados = existing_quantity
    db.session.add(
        ContaMultiuserConvite(
            conta_id=conta.id,
            franquia_id=fr.id,
            criado_por_user_id=owner.id,
            email_destino="f4g.reserved@test.com",
            estado=ContaMultiuserConvite.ESTADO_PENDENTE,
            expires_at=utcnow_naive() + timedelta(hours=1),
        )
    )
    db.session.commit()
    return conta, owner


def _cenario_cutoff_pago(conta_id: int, user: User, plano: str):
    row = _monetizacao_paga(conta_id, plano)
    row.vigencia_externa_fim = utcnow_naive() - timedelta(days=1)
    db.session.get(Franquia, user.franquia_id).fim_ciclo = row.vigencia_externa_fim
    row.snapshot_normalizado_json = json.dumps(
        {
            "mudanca_pendente": True,
            "tipo_mudanca": "downgrade",
            "plano_futuro": "free",
            "efetivar_em": (utcnow_naive() + timedelta(days=10)).isoformat(),
        }
    )
    db.session.commit()
    return row


def test_sweep_cleide_nao_expira_arquivo_recente_em_timezone_nao_utc(monkeypatch, tmp_path):
    import os
    from datetime import datetime, timezone

    import app.cleide_upload_store as store
    from tests.test_cleide_upload_api import _tmp_store

    _tmp_store(monkeypatch, tmp_path)
    path = tmp_path / "recente.csv"
    path.write_bytes(b"a,b\n1,2\n")
    now_utc = datetime.now(timezone.utc)
    os.utime(path, (now_utc.timestamp(), now_utc.timestamp()))
    monkeypatch.setattr(store, "_now_utc_naive", lambda: now_utc.replace(tzinfo=None))
    removed = store.cleanup_expired_cleide_uploads(30)
    assert path.exists()
    assert removed == 0
    mtime = store._posix_mtime_utc_naive(path)
    deadline = now_utc.replace(tzinfo=None) - timedelta(minutes=30)
    assert mtime >= deadline


def test_replace_cross_conta_nao_remove_upload_recente_da_origem(
    app, ctx, monkeypatch, tmp_path
):
    from pathlib import Path

    import app.cleide_upload_store as store
    from tests.test_cleide_upload_api import _cfg, _xlsx_bytes
    from tests.test_gate_definitivo_probes import _old_cleide_upload_after_transfer

    user, saved, old_session = _old_cleide_upload_after_transfer(
        app, monkeypatch, tmp_path
    )
    _cfg(monkeypatch)
    from app.cleide_upload_pipeline import get_cleide_config

    config = get_cleide_config()
    config.upload_ttl_minutes = 30
    monkeypatch.setattr("app.cleide_upload_pipeline.get_cleide_config", lambda: config)
    second = app.test_client()
    with second.session_transaction() as sess:
        sess.update(old_session)
    response = second.post(
        "/api/cleide/upload",
        data={"file": (io.BytesIO(_xlsx_bytes()), "nova.xlsx")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200, response.get_json()
    assert Path(saved["absolute_path"]).exists()
    with app.test_request_context("/"):
        from flask_login import login_user

        login_user(user)
        assert store.resolve_cleide_upload_file(saved["upload_ref"]) is None


def test_sweep_cleide_remove_arquivo_realmente_expirado(monkeypatch, tmp_path):
    import os
    import time

    import app.cleide_upload_store as store
    from tests.test_cleide_upload_api import _tmp_store

    _tmp_store(monkeypatch, tmp_path)
    path = tmp_path / "expirado.csv"
    path.write_bytes(b"a,b\n1,2\n")
    stale = time.time() - (31 * 60)
    os.utime(path, (stale, stale))
    removed = store.cleanup_expired_cleide_uploads(30)
    assert removed >= 1
    assert not path.exists()


def test_ttl_cleide_usa_mesma_base_temporal():
    import app.cleide_upload_store as store

    sweep_src = inspect.getsource(store.cleanup_expired_cleide_uploads)
    helper_src = inspect.getsource(store._posix_mtime_utc_naive)
    now_src = inspect.getsource(store._now_utc_naive)
    interval_src = inspect.getsource(store.maybe_cleanup_expired_cleide_uploads)
    assert "tz=UTC" in helper_src or "tz=timezone.utc" in helper_src
    assert "_posix_mtime_utc_naive" in sweep_src
    assert "UTC" in now_src
    assert "datetime.fromtimestamp(path.stat().st_mtime)" not in sweep_src
    assert "now.timestamp()" not in interval_src
    assert "time.time()" in interval_src


def test_backfill_quantity_existente_nao_cria_vinculos_acima_capacity(app):
    from app.services.conta_multiuser_capacidade_service import contar_capacidade_comprometida
    from app.services.conta_organizacional_backfill_service import (
        aplicar_backfill_multiuser_legado,
    )

    with app.app_context():
        conta, _owner = _cenario_backfill_quantity_e_reservas(1)
        assert contar_capacidade_comprometida(conta.id) == 1
        aplicar_backfill_multiuser_legado(db.session.connection())
        db.session.commit()
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 1
        comprometido = contar_capacidade_comprometida(conta.id)
        assert comprometido <= 1
        assert ContaVinculoOrganizacional.query.filter_by(
            conta_id=conta.id, estado="ativo"
        ).count() <= 1


def test_backfill_projeta_comprometido_final_antes_de_inserir():
    from app.services.conta_organizacional_backfill_service import (
        aplicar_backfill_multiuser_legado,
    )

    src = inspect.getsource(aplicar_backfill_multiuser_legado)
    assert src.index("_contar_comprometido_backfill") < src.index(
        "INSERT INTO conta_vinculo_organizacional"
    )
    assert src.index("_ler_quantity_persistida") < src.index(
        "INSERT INTO conta_vinculo_organizacional"
    )


def test_table_exists_exception_nao_equivale_tabela_ausente(app, monkeypatch):
    from app.services import conta_organizacional_backfill_service as bf

    with app.app_context():
        def broken_inspect(*_a, **_k):
            raise RuntimeError("AUDIT_SCHEMA_INSPECTION_FAILURE")

        monkeypatch.setattr(bf, "sa_inspect", broken_inspect)
        estado = bf._estado_tabela(db.session.connection(), "conta_multiuser_convite")
        assert estado == bf.TABELA_INCONCLUSIVA
        assert estado != bf.TABELA_NAO_EXISTE
        with pytest.raises(bf.InspecaoSchemaInconclusivaError):
            bf._table_exists(db.session.connection(), "conta_multiuser_convite")


def test_inspecao_inconclusiva_aborta_backfill_mutavel(app, monkeypatch):
    from app.services import conta_organizacional_backfill_service as bf

    with app.app_context():
        conta, _owner = _cenario_backfill_quantity_e_reservas(None)
        assert conta.quantidade_assentos_contratados is None

        def broken_inspect(*_a, **_k):
            raise RuntimeError("AUDIT_SCHEMA_INSPECTION_FAILURE")

        def broken_reservas(*_a, **_k):
            raise RuntimeError("AUDIT_RESERVA_QUERY_FAILURE")

        monkeypatch.setattr(bf, "sa_inspect", broken_inspect)
        monkeypatch.setattr(bf, "_contar_reservas_pendentes_validas_sql", broken_reservas)
        with pytest.raises(bf.InspecaoSchemaInconclusivaError):
            bf.aplicar_backfill_multiuser_legado(db.session.connection())
        db.session.rollback()
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados is None
        assert ContaVinculoOrganizacional.query.filter_by(conta_id=conta.id).count() == 0


def test_reconciliacao_nao_commita_vinculos_acima_quantity(app):
    from app.services.conta_multiuser_capacidade_service import contar_capacidade_comprometida
    from app.services.conta_multiuser_reconciliacao_service import (
        executar_reconciliacao_multiuser_antes_enforcement,
    )

    with app.app_context():
        conta, _owner = _cenario_backfill_quantity_e_reservas(1)
        executar_reconciliacao_multiuser_antes_enforcement()
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 1
        assert contar_capacidade_comprometida(conta.id) <= 1
        assert ContaVinculoOrganizacional.query.filter_by(
            conta_id=conta.id, estado="ativo"
        ).count() <= 1


def test_quantity_null_com_reservas_e_inspecao_ok_permanece_valida(app):
    from app.services.conta_multiuser_capacidade_service import contar_capacidade_comprometida
    from app.services.conta_organizacional_backfill_service import (
        aplicar_backfill_multiuser_legado,
    )

    with app.app_context():
        conta, _owner = _cenario_backfill_quantity_e_reservas(None)
        aplicar_backfill_multiuser_legado(db.session.connection())
        db.session.commit()
        db.session.refresh(conta)
        comprometido = contar_capacidade_comprometida(conta.id)
        assert conta.quantidade_assentos_contratados is not None
        assert comprometido <= conta.quantidade_assentos_contratados
        assert comprometido == 3
        assert ContaVinculoOrganizacional.query.filter_by(
            conta_id=conta.id, estado="ativo"
        ).count() == 2


def test_datetime_financeiro_malformado_vira_inconclusivo(app):
    from app.services.conta_multiuser_convite_service import (
        BENEFICIO_INCONCLUSIVO,
        BENEFICIO_PAGO_VIGENTE,
        decidir_beneficio_pago_para_transferencia,
    )

    with app.app_context():
        _secret(app)
        origem, fr_o = seed_conta_franquia_cliente(slug="f4g-dt-bad")
        user = seed_usuario(fr_o.id, origem.id, email="f4g.dtbad@test.com", categoria="starter")
        row = _cenario_cutoff_pago(origem.id, user, "starter")
        assert decidir_beneficio_pago_para_transferencia(origem.id) == BENEFICIO_PAGO_VIGENTE
        snapshot = json.loads(row.snapshot_normalizado_json)
        snapshot["efetivar_em"] = "invalid-financial-date"
        row.snapshot_normalizado_json = json.dumps(snapshot)
        db.session.commit()
        assert decidir_beneficio_pago_para_transferencia(origem.id) == BENEFICIO_INCONCLUSIVO


def test_datetime_parser_exception_vira_inconclusivo(app, monkeypatch):
    import app.services.cleiton_monetizacao_service as money
    from app.services.conta_multiuser_convite_service import (
        BENEFICIO_INCONCLUSIVO,
        BENEFICIO_PAGO_VIGENTE,
        decidir_beneficio_pago_para_transferencia,
    )

    with app.app_context():
        _secret(app)
        origem, fr_o = seed_conta_franquia_cliente(slug="f4g-dt-ex")
        user = seed_usuario(fr_o.id, origem.id, email="f4g.dtex@test.com", categoria="starter")
        _cenario_cutoff_pago(origem.id, user, "starter")
        assert decidir_beneficio_pago_para_transferencia(origem.id) == BENEFICIO_PAGO_VIGENTE
        real_datetime = money.datetime
        calls = []

        class BrokenDateTime(real_datetime):
            @classmethod
            def fromisoformat(cls, raw):
                calls.append(raw)
                raise ValueError("AUDIT_DATE_PARSER_FAILURE")

        monkeypatch.setattr(money, "datetime", BrokenDateTime)
        decision = decidir_beneficio_pago_para_transferencia(origem.id)
        assert calls
        assert decision == BENEFICIO_INCONCLUSIVO


def test_starter_data_inconclusiva_bloqueia(app):
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4g-st-dt", qtd=3, email="f4g.stdt.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4g-st-dt-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4g.stdt@test.com", categoria="starter")
        original = alvo.conta_id
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        row = _cenario_cutoff_pago(origem.id, alvo, "starter")
        snapshot = json.loads(row.snapshot_normalizado_json)
        snapshot["efetivar_em"] = "invalid-financial-date"
        row.snapshot_normalizado_json = json.dumps(snapshot)
        db.session.commit()
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        with pytest.raises((ConviteContratoIncompativelError, RuntimeError)):
            aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.rollback()
        db.session.refresh(alvo)
        assert alvo.conta_id == original
        assert alvo.conta_id != conta.id


def test_pro_data_inconclusiva_bloqueia(app):
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4g-pro-dt", qtd=3, email="f4g.prodt.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug="f4g-pro-dt-o")
        alvo = seed_usuario(fr_o.id, origem.id, email="f4g.prodt@test.com", categoria="pro")
        original = alvo.conta_id
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        row = _cenario_cutoff_pago(origem.id, alvo, "pro")
        snapshot = json.loads(row.snapshot_normalizado_json)
        snapshot["efetivar_em"] = "invalid-financial-date"
        row.snapshot_normalizado_json = json.dumps(snapshot)
        db.session.commit()
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        with pytest.raises((ConviteContratoIncompativelError, RuntimeError)):
            aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.rollback()
        db.session.refresh(alvo)
        assert alvo.conta_id == original
        assert alvo.conta_id != conta.id


def test_json_strict_false_python_bool_rejeitado():
    from app.services.cleiton_monetizacao_service import _json_loads

    with pytest.raises((TypeError, ValueError)):
        _json_loads(False, strict=True)


def test_json_strict_zero_python_rejeitado():
    from app.services.cleiton_monetizacao_service import _json_loads

    with pytest.raises((TypeError, ValueError)):
        _json_loads(0, strict=True)


def test_json_strict_list_python_rejeitada():
    from app.services.cleiton_monetizacao_service import _json_loads

    with pytest.raises((TypeError, ValueError)):
        _json_loads([], strict=True)


def test_json_strict_dict_vazio_e_legitimo():
    from app.services.cleiton_monetizacao_service import _json_loads

    assert _json_loads("{}", strict=True) == {}


def test_json_strict_none_e_legitimo():
    from app.services.cleiton_monetizacao_service import _json_loads

    assert _json_loads(None, strict=True) == {}


def _excecao_pg(pgcode: str, message: str, table_name: str | None = None):
    from sqlalchemy.exc import ProgrammingError

    class OrigemPG(Exception):
        def __init__(self, msg: str, code: str, tabela: str | None):
            super().__init__(msg)
            self.pgcode = code
            self.sqlstate = code
            self.diag = SimpleNamespace(sqlstate=code, table_name=tabela)

    orig = OrigemPG(message, pgcode, table_name)
    return ProgrammingError("SELECT 1", {}, orig)


def test_postgres_42703_nao_significa_tabela_ausente():
    from app.services import conta_organizacional_backfill_service as bf

    exc = _excecao_pg("42703", 'column "expires_at" does not exist')
    assert bf._classificar_erro_consulta_tabela(exc, "conta_multiuser_convite") == bf.RESERVAS_INCONCLUSIVO
    assert bf._classificar_erro_consulta_tabela(exc, "conta_multiuser_convite") != bf.TABELA_AUSENTE_CONCLUSIVAMENTE


def test_erro_sql_desconhecido_nao_significa_zero_reservas():
    from app.services import conta_organizacional_backfill_service as bf

    exc = RuntimeError("connection reset by peer")
    assert bf._classificar_erro_consulta_tabela(exc, "conta_multiuser_convite") == bf.RESERVAS_INCONCLUSIVO


def test_postgres_42p01_tabela_esperada_e_ausencia_conclusiva():
    from app.services import conta_organizacional_backfill_service as bf

    exc = _excecao_pg(
        "42P01",
        'relation "conta_multiuser_convite" does not exist',
        table_name="conta_multiuser_convite",
    )
    assert bf._classificar_erro_consulta_tabela(exc, "conta_multiuser_convite") == (
        bf.TABELA_AUSENTE_CONCLUSIVAMENTE
    )


def test_postgres_42p01_diag_outra_tabela_e_inconclusivo():
    from app.services import conta_organizacional_backfill_service as bf

    exc = _excecao_pg(
        "42P01",
        'relation "outra_tabela" does not exist',
        table_name="outra_tabela",
    )
    assert bf._classificar_erro_consulta_tabela(exc, "conta_multiuser_convite") == (
        bf.RESERVAS_INCONCLUSIVO
    )


def test_postgres_42p01_sem_diag_mensagem_esperada_e_ausencia_conclusiva():
    import re

    from app.services import conta_organizacional_backfill_service as bf

    exc = _excecao_pg(
        "42P01",
        'relation "conta_multiuser_convite" does not exist',
        table_name=None,
    )
    try:
        classif = bf._classificar_erro_consulta_tabela(exc, "conta_multiuser_convite")
    except re.error as exc_re:
        pytest.fail(f"regex PostgreSQL levantou PatternError: {exc_re}")
    assert classif == bf.TABELA_AUSENTE_CONCLUSIVAMENTE


def test_postgres_42p01_sem_diag_mensagem_ambigua_e_inconclusivo():
    import re

    from app.services import conta_organizacional_backfill_service as bf

    exc = _excecao_pg(
        "42P01",
        'column "expires_at" does not exist',
        table_name=None,
    )
    try:
        classif = bf._classificar_erro_consulta_tabela(exc, "conta_multiuser_convite")
    except re.error as exc_re:
        pytest.fail(f"regex PostgreSQL levantou PatternError: {exc_re}")
    assert classif == bf.RESERVAS_INCONCLUSIVO


def test_sqlite_no_such_table_esperada_e_ausencia_conclusiva():
    from app.services import conta_organizacional_backfill_service as bf

    exc = RuntimeError("no such table: conta_multiuser_convite")
    assert bf._classificar_erro_consulta_tabela(exc, "conta_multiuser_convite") == (
        bf.TABELA_AUSENTE_CONCLUSIVAMENTE
    )


def test_sqlite_no_such_column_nao_e_tabela_ausente():
    from app.services import conta_organizacional_backfill_service as bf

    exc = RuntimeError("no such column: expires_at")
    assert bf._classificar_erro_consulta_tabela(exc, "conta_multiuser_convite") == bf.RESERVAS_INCONCLUSIVO


def test_contar_reservas_42703_levanta(app, monkeypatch):
    from app.services import conta_organizacional_backfill_service as bf

    with app.app_context():
        monkeypatch.setattr(bf, "_estado_tabela", lambda *_a, **_k: bf.TABELA_INCONCLUSIVA)

        def broken(*_a, **_k):
            raise _excecao_pg("42703", 'column "expires_at" does not exist')

        monkeypatch.setattr(bf, "_contar_reservas_pendentes_validas_sql", broken)
        with pytest.raises(bf.InspecaoSchemaInconclusivaError):
            bf._contar_reservas_backfill(db.session.connection(), 1, utcnow_naive())


def test_reconciliacao_aborta_quando_contagem_inconclusiva(app, monkeypatch):
    from app.services import conta_organizacional_backfill_service as bf
    from app.services.conta_multiuser_capacidade_service import contar_capacidade_comprometida
    from app.services.conta_multiuser_reconciliacao_service import (
        executar_reconciliacao_multiuser_antes_enforcement,
    )

    with app.app_context():
        conta, _owner = _cenario_backfill_quantity_e_reservas(1)
        monkeypatch.setattr(bf, "_estado_tabela", lambda *_a, **_k: bf.TABELA_INCONCLUSIVA)

        def broken(*_a, **_k):
            raise RuntimeError("syntax error at or near SELECT")

        monkeypatch.setattr(bf, "_contar_reservas_pendentes_validas_sql", broken)
        with pytest.raises(bf.InspecaoSchemaInconclusivaError):
            executar_reconciliacao_multiuser_antes_enforcement()
        db.session.rollback()
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 1
        assert contar_capacidade_comprometida(conta.id) == 1
        assert ContaVinculoOrganizacional.query.filter_by(conta_id=conta.id).count() == 0


def test_inspector_falha_sql_ok_usa_contagem_real(app, monkeypatch):
    from app.services import conta_organizacional_backfill_service as bf
    from app.services.conta_multiuser_capacidade_service import contar_capacidade_comprometida

    with app.app_context():
        conta, _owner = _cenario_backfill_quantity_e_reservas(None)
        monkeypatch.setattr(bf, "_estado_tabela", lambda *_a, **_k: bf.TABELA_INCONCLUSIVA)
        bf.aplicar_backfill_multiuser_legado(db.session.connection())
        db.session.commit()
        db.session.refresh(conta)
        comprometido = contar_capacidade_comprometida(conta.id)
        assert comprometido == 3
        assert conta.quantidade_assentos_contratados == 3


def _tag_adversario(value) -> str:
    if value is False:
        return "false"
    if value is True:
        return "true"
    if isinstance(value, float):
        if value != value:
            return "nan"
        if value == float("inf"):
            return "inf"
        if value == float("-inf"):
            return "ninf"
    if value == 0:
        return "zero"
    if value == 1:
        return "one"
    if value == []:
        return "list"
    if value == {}:
        return "dict"
    if isinstance(value, str):
        return value[:16].lower()
    return type(value).__name__.lower()


def _aplicar_shape_stripe(payload: dict, shape: str, value):
    if shape == "data":
        payload["data"] = value
    elif shape in ("object", "data.object"):
        payload["data"]["object"] = value
    elif shape == "customer":
        payload["data"]["object"]["customer"] = value
    elif shape == "subscription":
        payload["data"]["object"]["subscription"] = value
    elif shape == "billing_reason":
        payload["data"]["object"]["billing_reason"] = value
    elif shape == "id":
        payload["data"]["object"]["id"] = value
    else:
        raise AssertionError(shape)


def _assert_transferencia_bloqueada(alvo: User, conta_destino: Conta, convite_id: int, conta_origem_id: int):
    db.session.refresh(alvo)
    convite = db.session.get(ContaMultiuserConvite, convite_id)
    assert alvo.conta_id == conta_origem_id
    assert alvo.conta_id != conta_destino.id
    assert convite is not None
    assert convite.estado == ContaMultiuserConvite.ESTADO_PENDENTE
    assert (
        ContaVinculoOrganizacional.query.filter_by(
            user_id=alvo.id, conta_id=conta_destino.id, estado="ativo"
        ).count()
        == 0
    )


STRIPE_INVALID_SHAPES = (
    ("customer", []),
    ("customer", False),
    ("customer", 0),
    ("subscription", []),
    ("subscription", False),
    ("subscription", 0),
    ("data", []),
    ("data", False),
    ("data", 0),
    ("object", []),
    ("object", False),
    ("object", 0),
    ("billing_reason", []),
    ("billing_reason", False),
    ("billing_reason", 0),
)


@pytest.mark.parametrize("plan", ["starter", "pro"])
@pytest.mark.parametrize("shape,value", STRIPE_INVALID_SHAPES)
def test_estrutura_stripe_invalida_bloqueia_starter_pro(app, plan, shape, value):
    from app.models import MonetizacaoFato
    from app.services.conta_multiuser_convite_service import (
        BENEFICIO_INCONCLUSIVO,
        SEM_BENEFICIO_CONCLUSIVAMENTE,
        decidir_beneficio_pago_para_transferencia,
    )

    tag = f"{shape}-{_tag_adversario(value)}"
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            f"f4h-{plan}-{tag}", qtd=3, email=f"f4h.{plan}.{tag}.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug=f"f4h-{plan}-{tag}-o")
        alvo = seed_usuario(
            fr_o.id, origem.id, email=f"f4h.{plan}.{tag}@test.com", categoria=plan
        )
        original = alvo.conta_id
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        _cenario_falha_mensal_vigente(origem.id, alvo, plan, f"in_f4h_{plan}_{tag}")
        fact = (
            MonetizacaoFato.query.filter_by(conta_id=origem.id)
            .order_by(MonetizacaoFato.id.desc())
            .first()
        )
        payload = json.loads(fact.payload_bruto_sanitizado_json)
        _aplicar_shape_stripe(payload, shape, value)
        fact.payload_bruto_sanitizado_json = json.dumps(payload)
        db.session.commit()
        decisao = decidir_beneficio_pago_para_transferencia(origem.id)
        assert decisao == BENEFICIO_INCONCLUSIVO
        assert decisao != SEM_BENEFICIO_CONCLUSIVAMENTE
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        with pytest.raises((ConviteContratoIncompativelError, RuntimeError)):
            aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.rollback()
        _assert_transferencia_bloqueada(alvo, conta, out.convite_id, original)


@pytest.mark.parametrize("raw", [0, -1, float("nan"), float("inf"), float("-inf")])
def test_cutoff_numerico_invalido_bloqueia(app, raw):
    from app.services.conta_multiuser_convite_service import (
        BENEFICIO_INCONCLUSIVO,
        SEM_BENEFICIO_CONCLUSIVAMENTE,
        decidir_beneficio_pago_para_transferencia,
    )

    with app.app_context():
        _secret(app)
        origem, fr_o = seed_conta_franquia_cliente(slug=f"f4h-cut-{id(raw)}")
        user = seed_usuario(fr_o.id, origem.id, email=f"f4h.cut.{id(raw)}@test.com", categoria="starter")
        row = _cenario_cutoff_pago(origem.id, user, "starter")
        snapshot = json.loads(row.snapshot_normalizado_json)
        snapshot["efetivar_em"] = raw
        row.snapshot_normalizado_json = json.dumps(snapshot, allow_nan=True)
        db.session.commit()
        decisao = decidir_beneficio_pago_para_transferencia(origem.id)
        assert decisao == BENEFICIO_INCONCLUSIVO
        assert decisao != SEM_BENEFICIO_CONCLUSIVAMENTE


def test_parser_temporal_invalido_nunca_vira_ausencia():
    from app.services.cleiton_monetizacao_service import _to_datetime_utc_naive

    with pytest.raises((TypeError, ValueError)):
        _to_datetime_utc_naive(0, strict=True)
    with pytest.raises((TypeError, ValueError)):
        _to_datetime_utc_naive(-1, strict=True)
    with pytest.raises((TypeError, ValueError)):
        _to_datetime_utc_naive(float("nan"), strict=True)
    with pytest.raises((TypeError, ValueError)):
        _to_datetime_utc_naive(float("inf"), strict=True)
    with pytest.raises((TypeError, ValueError)):
        _to_datetime_utc_naive(float("-inf"), strict=True)
    with pytest.raises((TypeError, ValueError)):
        _to_datetime_utc_naive("invalid-financial-date", strict=True)
    with pytest.raises((TypeError, ValueError)):
        _to_datetime_utc_naive([], strict=True)
    assert _to_datetime_utc_naive(None, strict=True) is None


INVALIDOS_IDENTIFICADOR = ([], False, True, 0, 1, {}, 1.5)
DOWNGRADE_INVALID_FIELDS = (
    ("mudanca_pendente", []),
    ("mudanca_pendente", {}),
    ("mudanca_pendente", 0),
    ("tipo_mudanca", []),
    ("tipo_mudanca", False),
    ("tipo_mudanca", 0),
    ("tipo_mudanca", "INVALIDO"),
    ("plano_futuro", []),
    ("plano_futuro", False),
    ("plano_futuro", 0),
    ("plano_futuro", "PLANO_INEXISTENTE"),
)


@pytest.mark.parametrize("valor", INVALIDOS_IDENTIFICADOR)
def test_identificador_financeiro_invalido_nao_vira_ausencia(valor):
    from app.services.cleiton_monetizacao_service import (
        _norm_text,
        require_optional_identifier_strict,
    )

    with pytest.raises((TypeError, ValueError)):
        require_optional_identifier_strict(valor, nome="customer")
    with pytest.raises((TypeError, ValueError)):
        _norm_text(valor, strict=True)
    assert require_optional_identifier_strict(None, nome="customer") is None
    assert _norm_text(None, strict=True) is None


def test_norm_text_legado_permanece_permissivo():
    from app.services.cleiton_monetizacao_service import _norm_text

    assert _norm_text([]) == "[]"
    assert _norm_text(False) == "False"
    assert _norm_text(0) == "0"


@pytest.mark.parametrize("plan", ["starter", "pro"])
@pytest.mark.parametrize("campo,valor", DOWNGRADE_INVALID_FIELDS)
def test_downgrade_campo_invalido_bloqueia_starter_pro(app, plan, campo, valor):
    from app.services.conta_multiuser_convite_service import (
        BENEFICIO_INCONCLUSIVO,
        SEM_BENEFICIO_CONCLUSIVAMENTE,
        decidir_beneficio_pago_para_transferencia,
    )

    tag = f"{campo}-{_tag_adversario(valor)}"
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            f"f4d-{plan}-{tag}", qtd=3, email=f"f4d.{plan}.{tag}.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug=f"f4d-{plan}-{tag}-o")
        alvo = seed_usuario(
            fr_o.id, origem.id, email=f"f4d.{plan}.{tag}@test.com", categoria=plan
        )
        original = alvo.conta_id
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        row = _cenario_cutoff_pago(origem.id, alvo, plan)
        snapshot = json.loads(row.snapshot_normalizado_json)
        snapshot[campo] = valor
        row.snapshot_normalizado_json = json.dumps(snapshot)
        db.session.commit()
        decisao = decidir_beneficio_pago_para_transferencia(origem.id)
        assert decisao == BENEFICIO_INCONCLUSIVO
        assert decisao != SEM_BENEFICIO_CONCLUSIVAMENTE
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        with pytest.raises((ConviteContratoIncompativelError, RuntimeError)):
            aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.rollback()
        _assert_transferencia_bloqueada(alvo, conta, out.convite_id, original)


@pytest.mark.parametrize("plan", ["starter", "pro"])
def test_downgrade_incoerente_tipo_ausente_bloqueia(app, plan):
    from app.services.conta_multiuser_convite_service import (
        BENEFICIO_INCONCLUSIVO,
        SEM_BENEFICIO_CONCLUSIVAMENTE,
        decidir_beneficio_pago_para_transferencia,
    )

    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            f"f4d-{plan}-tipo-aus", qtd=3, email=f"f4d.{plan}.tipoaus.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug=f"f4d-{plan}-tipo-aus-o")
        alvo = seed_usuario(
            fr_o.id, origem.id, email=f"f4d.{plan}.tipoaus@test.com", categoria=plan
        )
        original = alvo.conta_id
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        row = _cenario_cutoff_pago(origem.id, alvo, plan)
        snapshot = json.loads(row.snapshot_normalizado_json)
        snapshot["mudanca_pendente"] = True
        snapshot.pop("tipo_mudanca", None)
        row.snapshot_normalizado_json = json.dumps(snapshot)
        db.session.commit()
        decisao = decidir_beneficio_pago_para_transferencia(origem.id)
        assert decisao == BENEFICIO_INCONCLUSIVO
        assert decisao != SEM_BENEFICIO_CONCLUSIVAMENTE
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        with pytest.raises((ConviteContratoIncompativelError, RuntimeError)):
            aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.rollback()
        _assert_transferencia_bloqueada(alvo, conta, out.convite_id, original)


@pytest.mark.parametrize("plan", ["starter", "pro"])
def test_downgrade_incoerente_plano_futuro_invalido_bloqueia(app, plan):
    from app.services.conta_multiuser_convite_service import (
        BENEFICIO_INCONCLUSIVO,
        SEM_BENEFICIO_CONCLUSIVAMENTE,
        decidir_beneficio_pago_para_transferencia,
    )

    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            f"f4d-{plan}-plano-inv", qtd=3, email=f"f4d.{plan}.planoinv.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug=f"f4d-{plan}-plano-inv-o")
        alvo = seed_usuario(
            fr_o.id, origem.id, email=f"f4d.{plan}.planoinv@test.com", categoria=plan
        )
        original = alvo.conta_id
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        row = _cenario_cutoff_pago(origem.id, alvo, plan)
        snapshot = json.loads(row.snapshot_normalizado_json)
        snapshot["mudanca_pendente"] = True
        snapshot["tipo_mudanca"] = "downgrade"
        snapshot["plano_futuro"] = []
        row.snapshot_normalizado_json = json.dumps(snapshot)
        db.session.commit()
        decisao = decidir_beneficio_pago_para_transferencia(origem.id)
        assert decisao == BENEFICIO_INCONCLUSIVO
        assert decisao != SEM_BENEFICIO_CONCLUSIVAMENTE
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        with pytest.raises((ConviteContratoIncompativelError, RuntimeError)):
            aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.rollback()
        _assert_transferencia_bloqueada(alvo, conta, out.convite_id, original)


@pytest.mark.parametrize("plan", ["starter", "pro"])
@pytest.mark.parametrize("shape,value", (("customer", True), ("customer", 1), ("customer", {}), ("subscription", True), ("subscription", 1), ("subscription", {})))
def test_identificador_stripe_extra_invalido_bloqueia_starter_pro(app, plan, shape, value):
    from app.models import MonetizacaoFato
    from app.services.conta_multiuser_convite_service import (
        BENEFICIO_INCONCLUSIVO,
        SEM_BENEFICIO_CONCLUSIVAMENTE,
        decidir_beneficio_pago_para_transferencia,
    )

    tag = f"{shape}-{_tag_adversario(value)}"
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            f"f4x-{plan}-{tag}", qtd=3, email=f"f4x.{plan}.{tag}.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug=f"f4x-{plan}-{tag}-o")
        alvo = seed_usuario(
            fr_o.id, origem.id, email=f"f4x.{plan}.{tag}@test.com", categoria=plan
        )
        original = alvo.conta_id
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        _cenario_falha_mensal_vigente(origem.id, alvo, plan, f"in_f4x_{plan}_{tag}")
        fact = (
            MonetizacaoFato.query.filter_by(conta_id=origem.id)
            .order_by(MonetizacaoFato.id.desc())
            .first()
        )
        payload = json.loads(fact.payload_bruto_sanitizado_json)
        _aplicar_shape_stripe(payload, shape, value)
        fact.payload_bruto_sanitizado_json = json.dumps(payload)
        db.session.commit()
        decisao = decidir_beneficio_pago_para_transferencia(origem.id)
        assert decisao == BENEFICIO_INCONCLUSIVO
        assert decisao != SEM_BENEFICIO_CONCLUSIVAMENTE
        csrf = gerar_csrf_token_aceite(alvo.id, out.convite_id)
        with pytest.raises((ConviteContratoIncompativelError, RuntimeError)):
            aceitar_convite(user=alvo, token=out.token, csrf_token=csrf)
        db.session.rollback()
        _assert_transferencia_bloqueada(alvo, conta, out.convite_id, original)


def test_contrato_strict_helpers_ausente_valido_invalido_exception():
    from app.services.cleiton_monetizacao_service import (
        DadoFinanceiroInvalidoError,
        _extrair_objeto_stripe_evento,
        _extrair_pendencia_downgrade_snapshot,
        require_optional_bool_strict,
        require_optional_dict_strict,
        require_optional_identifier_strict,
        require_optional_string_strict,
    )

    assert require_optional_identifier_strict(None, nome="customer") is None
    assert require_optional_identifier_strict("cus_1", nome="customer") == "cus_1"
    with pytest.raises(TypeError):
        require_optional_identifier_strict([], nome="customer")
    assert require_optional_bool_strict(None, nome="mudanca_pendente") is None
    assert require_optional_bool_strict(True, nome="mudanca_pendente") is True
    assert require_optional_bool_strict(False, nome="mudanca_pendente") is False
    with pytest.raises(TypeError):
        require_optional_bool_strict(0, nome="mudanca_pendente")
    assert require_optional_string_strict(None, nome="billing_reason") is None
    assert require_optional_string_strict("subscription_cycle", nome="billing_reason") == (
        "subscription_cycle"
    )
    with pytest.raises(TypeError):
        require_optional_string_strict([], nome="billing_reason")
    assert require_optional_dict_strict(None, nome="data") is None
    assert require_optional_dict_strict({"id": "x"}, nome="data") == {"id": "x"}
    with pytest.raises(TypeError):
        require_optional_dict_strict([], nome="data")

    with pytest.raises(TypeError):
        _extrair_objeto_stripe_evento({"data": []}, strict=True)
    with pytest.raises(TypeError):
        _extrair_objeto_stripe_evento(
            {"data": {"object": {"customer": []}}}, strict=True
        )
    obj = _extrair_objeto_stripe_evento({}, strict=True)
    assert obj == {}

    assert _extrair_pendencia_downgrade_snapshot({}, strict=True) is None
    assert _extrair_pendencia_downgrade_snapshot(
        {"mudanca_pendente": False}, strict=True
    ) is None
    with pytest.raises((TypeError, DadoFinanceiroInvalidoError, ValueError)):
        _extrair_pendencia_downgrade_snapshot({"mudanca_pendente": []}, strict=True)
    with pytest.raises((TypeError, DadoFinanceiroInvalidoError, ValueError)):
        _extrair_pendencia_downgrade_snapshot(
            {"mudanca_pendente": True, "tipo_mudanca": "INVALIDO"},
            strict=True,
        )
    with pytest.raises((TypeError, DadoFinanceiroInvalidoError, ValueError)):
        _extrair_pendencia_downgrade_snapshot(
            {"atualizado_em": "invalid-financial-date"},
            strict=True,
        )


def _assert_estado_transacional_origem(
    alvo: User,
    conta_destino: Conta,
    convite_id: int,
    conta_origem_id: int,
    franquia_origem_id: int,
    franquia_destino_id: int,
):
    db.session.expire_all()
    alvo = db.session.get(User, alvo.id)
    convite = db.session.get(ContaMultiuserConvite, convite_id)
    assert alvo.conta_id == conta_origem_id
    assert alvo.franquia_id == franquia_origem_id
    assert alvo.conta_id != conta_destino.id
    assert convite is not None
    assert convite.estado == ContaMultiuserConvite.ESTADO_PENDENTE
    assert (
        ContaVinculoOrganizacional.query.filter_by(
            user_id=alvo.id, conta_id=conta_destino.id, estado="ativo"
        ).count()
        == 0
    )
    assert User.query.filter_by(franquia_id=franquia_destino_id).count() == 0


def _aceite_http_ou_servico(app, alvo: User, token: str, convite_id: int):
    client = _build_client(app)
    if "index" not in app.view_functions:
        app.add_url_rule("/", endpoint="index", view_func=lambda: "index")
    _login(client, alvo)
    response = client.post(
        f"/convite/{token}/aceitar",
        data={"csrf_token": gerar_csrf_token_aceite(alvo.id, convite_id)},
    )
    assert response.status_code in {302, 400, 403, 409, 500}
    return response


RC03_CORRELACAO_SHAPES = (
    ("customer_id", "data", []),
    ("customer_id", "object", []),
    ("customer_id", "id", []),
    ("subscription_id", "data", []),
)

RC03_PLANOS_INVALIDOS = ("", "PLANO_INEXISTENTE", [], False, 0)

RC03_TEMPORAIS_INVALIDOS = (
    "invalid-financial-date",
    [],
    False,
    0,
    float("nan"),
    float("inf"),
    float("-inf"),
)


@pytest.mark.parametrize("plan", ["starter", "pro"])
@pytest.mark.parametrize("ident,shape,value", RC03_CORRELACAO_SHAPES)
def test_rc03_correlacao_incompleta_nao_pula_validacao(app, plan, ident, shape, value):
    from app.models import MonetizacaoFato
    from app.services.conta_multiuser_convite_service import (
        BENEFICIO_INCONCLUSIVO,
        SEM_BENEFICIO_CONCLUSIVAMENTE,
        decidir_beneficio_pago_para_transferencia,
    )

    tag = f"{ident}-{shape}-{_tag_adversario(value)}"
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            f"rc03c-{plan}-{tag}", qtd=3, email=f"rc03c.{plan}.{tag}.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug=f"rc03c-{plan}-{tag}-o")
        alvo = seed_usuario(
            fr_o.id, origem.id, email=f"rc03c.{plan}.{tag}@test.com", categoria=plan
        )
        original = alvo.conta_id
        fr_origem = alvo.franquia_id
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        _cenario_falha_mensal_vigente(origem.id, alvo, plan, f"in_rc03c_{plan}_{tag}")
        fact = (
            MonetizacaoFato.query.filter_by(conta_id=origem.id)
            .order_by(MonetizacaoFato.id.desc())
            .first()
        )
        if ident == "customer_id":
            fact.customer_id = None
        else:
            fact.subscription_id = None
        payload = json.loads(fact.payload_bruto_sanitizado_json)
        _aplicar_shape_stripe(payload, shape, value)
        fact.payload_bruto_sanitizado_json = json.dumps(payload)
        db.session.commit()
        decisao = decidir_beneficio_pago_para_transferencia(origem.id)
        assert decisao == BENEFICIO_INCONCLUSIVO
        assert decisao != SEM_BENEFICIO_CONCLUSIVAMENTE
        _aceite_http_ou_servico(app, alvo, out.token, out.convite_id)
        _assert_estado_transacional_origem(
            alvo, conta, out.convite_id, original, fr_origem, out.franquia_id
        )


@pytest.mark.parametrize("plan", ["starter", "pro"])
@pytest.mark.parametrize("valor", RC03_PLANOS_INVALIDOS)
def test_rc03_plano_interno_invalido_gera_inconclusivo(app, plan, valor):
    from app.services.conta_multiuser_convite_service import (
        BENEFICIO_INCONCLUSIVO,
        SEM_BENEFICIO_CONCLUSIVAMENTE,
        decidir_beneficio_pago_para_transferencia,
    )

    tag = _tag_adversario(valor)
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            f"rc03p-{plan}-{tag}", qtd=3, email=f"rc03p.{plan}.{tag}.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug=f"rc03p-{plan}-{tag}-o")
        alvo = seed_usuario(
            fr_o.id, origem.id, email=f"rc03p.{plan}.{tag}@test.com", categoria=plan
        )
        original = alvo.conta_id
        fr_origem = alvo.franquia_id
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        row = _cenario_falha_mensal_vigente(origem.id, alvo, plan, f"in_rc03p_{plan}_{tag}")
        if isinstance(valor, str):
            row.plano_interno = valor
            db.session.commit()
            decisao = decidir_beneficio_pago_para_transferencia(origem.id)
        else:
            from app.services.cleiton_monetizacao_service import require_optional_plano_strict

            with pytest.raises((TypeError, ValueError)):
                require_optional_plano_strict(valor, nome="plano_interno")
            with db.session.no_autoflush:
                row.plano_interno = valor
                decisao = decidir_beneficio_pago_para_transferencia(origem.id)
            db.session.rollback()
        if valor == "":
            assert decisao in {BENEFICIO_INCONCLUSIVO, SEM_BENEFICIO_CONCLUSIVAMENTE}
            if decisao == SEM_BENEFICIO_CONCLUSIVAMENTE:
                return
        else:
            assert decisao == BENEFICIO_INCONCLUSIVO
            assert decisao != SEM_BENEFICIO_CONCLUSIVAMENTE
        if isinstance(valor, str):
            _aceite_http_ou_servico(app, alvo, out.token, out.convite_id)
        _assert_estado_transacional_origem(
            alvo, conta, out.convite_id, original, fr_origem, out.franquia_id
        )


@pytest.mark.parametrize("plan", ["starter", "pro"])
@pytest.mark.parametrize("valor", RC03_TEMPORAIS_INVALIDOS)
def test_rc03_atualizado_em_malformado_gera_inconclusivo(app, plan, valor):
    from app.models import MonetizacaoFato
    from app.services.conta_multiuser_convite_service import (
        BENEFICIO_INCONCLUSIVO,
        SEM_BENEFICIO_CONCLUSIVAMENTE,
        decidir_beneficio_pago_para_transferencia,
    )

    tag = _tag_adversario(valor)
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            f"rc03t-{plan}-{tag}", qtd=3, email=f"rc03t.{plan}.{tag}.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug=f"rc03t-{plan}-{tag}-o")
        alvo = seed_usuario(
            fr_o.id, origem.id, email=f"rc03t.{plan}.{tag}@test.com", categoria=plan
        )
        original = alvo.conta_id
        fr_origem = alvo.franquia_id
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        row = _cenario_falha_mensal_vigente(origem.id, alvo, plan, f"in_rc03t_{plan}_{tag}")
        MonetizacaoFato.query.filter_by(conta_id=origem.id).delete()
        row.snapshot_normalizado_json = json.dumps(
            {
                "mudanca_pendente": True,
                "tipo_mudanca": "downgrade",
                "plano_futuro": "free",
                "efetivar_em": (utcnow_naive() - timedelta(days=2)).isoformat(),
                "atualizado_em": valor,
            },
            allow_nan=True,
        )
        db.session.commit()
        decisao = decidir_beneficio_pago_para_transferencia(origem.id)
        assert decisao == BENEFICIO_INCONCLUSIVO
        assert decisao != SEM_BENEFICIO_CONCLUSIVAMENTE
        _aceite_http_ou_servico(app, alvo, out.token, out.convite_id)
        _assert_estado_transacional_origem(
            alvo, conta, out.convite_id, original, fr_origem, out.franquia_id
        )


@pytest.mark.parametrize("plan", ["starter", "pro"])
def test_rc03_payload_invalido_bloqueia_mesmo_sem_customer_id(app, plan):
    from app.models import MonetizacaoFato
    from app.services.conta_multiuser_convite_service import (
        BENEFICIO_INCONCLUSIVO,
        decidir_beneficio_pago_para_transferencia,
    )

    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            f"rc03x-{plan}", qtd=3, email=f"rc03x.{plan}.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug=f"rc03x-{plan}-o")
        alvo = seed_usuario(
            fr_o.id, origem.id, email=f"rc03x.{plan}@test.com", categoria=plan
        )
        original = alvo.conta_id
        fr_origem = alvo.franquia_id
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        _cenario_falha_mensal_vigente(origem.id, alvo, plan, f"in_rc03x_{plan}")
        fact = (
            MonetizacaoFato.query.filter_by(conta_id=origem.id)
            .order_by(MonetizacaoFato.id.desc())
            .first()
        )
        fact.customer_id = None
        payload = json.loads(fact.payload_bruto_sanitizado_json)
        payload["data"] = []
        fact.payload_bruto_sanitizado_json = json.dumps(payload)
        db.session.commit()
        assert decidir_beneficio_pago_para_transferencia(origem.id) == BENEFICIO_INCONCLUSIVO
        _aceite_http_ou_servico(app, alvo, out.token, out.convite_id)
        _assert_estado_transacional_origem(
            alvo, conta, out.convite_id, original, fr_origem, out.franquia_id
        )


@pytest.mark.parametrize("plan", ["starter", "pro"])
def test_rc03_starter_pro_nao_transferem_convite_nao_consome(app, plan):
    from app.models import MonetizacaoFato
    from app.services.conta_multiuser_convite_service import (
        BENEFICIO_INCONCLUSIVO,
        decidir_beneficio_pago_para_transferencia,
    )

    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            f"rc03n-{plan}", qtd=3, email=f"rc03n.{plan}.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug=f"rc03n-{plan}-o")
        alvo = seed_usuario(
            fr_o.id, origem.id, email=f"rc03n.{plan}@test.com", categoria=plan
        )
        original = alvo.conta_id
        fr_origem = alvo.franquia_id
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        row = _cenario_falha_mensal_vigente(origem.id, alvo, plan, f"in_rc03n_{plan}")
        row.plano_interno = "PLANO_INEXISTENTE"
        db.session.commit()
        assert decidir_beneficio_pago_para_transferencia(origem.id) == BENEFICIO_INCONCLUSIVO
        _aceite_http_ou_servico(app, alvo, out.token, out.convite_id)
        _assert_estado_transacional_origem(
            alvo, conta, out.convite_id, original, fr_origem, out.franquia_id
        )
        assert MonetizacaoFato.query.filter_by(conta_id=origem.id).count() >= 1


def test_rc03_barreira_unica_antes_de_qualquer_retorno_conclusivo():
    from app.services.conta_multiuser_convite_service import (
        _validar_estado_financeiro_transferencia_strict,
        decidir_beneficio_pago_para_transferencia,
    )

    source = inspect.getsource(decidir_beneficio_pago_para_transferencia)
    assert "_validar_estado_financeiro_transferencia_strict" in source
    assert source.index("_validar_estado_financeiro_transferencia_strict") < source.index(
        "conta_possui_beneficio_pago_vigente"
    )
    assert inspect.isfunction(_validar_estado_financeiro_transferencia_strict)


RC03_PAYLOAD_VINCULO_INVALIDO = (
    ("data_list", {"data": []}),
    ("object_list", {"data": {"object": []}}),
    ("object_id_list", {"data": {"object": {"id": []}}}),
)


@pytest.mark.parametrize("plan", ["starter", "pro"])
@pytest.mark.parametrize("tag,payload", RC03_PAYLOAD_VINCULO_INVALIDO)
def test_rc03_payload_bruto_vinculo_invalido_bloqueia_aceite(app, plan, tag, payload):
    from app.models import MonetizacaoFato
    from app.services.conta_multiuser_convite_service import (
        BENEFICIO_INCONCLUSIVO,
        SEM_BENEFICIO_CONCLUSIVAMENTE,
        decidir_beneficio_pago_para_transferencia,
    )

    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            f"rc03v-{plan}-{tag}", qtd=3, email=f"rc03v.{plan}.{tag}.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug=f"rc03v-{plan}-{tag}-o")
        alvo = seed_usuario(
            fr_o.id, origem.id, email=f"rc03v.{plan}.{tag}@test.com", categoria=plan
        )
        original = alvo.conta_id
        fr_origem = alvo.franquia_id
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        row = _cenario_falha_mensal_vigente(origem.id, alvo, plan, f"in_rc03v_{plan}_{tag}")
        MonetizacaoFato.query.filter_by(conta_id=origem.id).delete()
        row.snapshot_normalizado_json = "{}"
        row.payload_bruto_sanitizado_json = json.dumps(payload)
        db.session.commit()
        decisao = decidir_beneficio_pago_para_transferencia(origem.id)
        assert decisao == BENEFICIO_INCONCLUSIVO
        assert decisao != SEM_BENEFICIO_CONCLUSIVAMENTE
        _aceite_http_ou_servico(app, alvo, out.token, out.convite_id)
        _assert_estado_transacional_origem(
            alvo, conta, out.convite_id, original, fr_origem, out.franquia_id
        )


@pytest.mark.parametrize("plan", ["starter", "pro"])
def test_rc03_snapshot_invalido_nao_escapa_por_falha_mensal(app, plan):
    from app.services.conta_multiuser_convite_service import (
        BENEFICIO_INCONCLUSIVO,
        BENEFICIO_PAGO_VIGENTE,
        decidir_beneficio_pago_para_transferencia,
    )

    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            f"rc03s-fm-{plan}", qtd=3, email=f"rc03s.fm.{plan}.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug=f"rc03s-fm-{plan}-o")
        alvo = seed_usuario(
            fr_o.id, origem.id, email=f"rc03s.fm.{plan}@test.com", categoria=plan
        )
        original = alvo.conta_id
        fr_origem = alvo.franquia_id
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        row = _cenario_falha_mensal_vigente(origem.id, alvo, plan, f"in_rc03s_fm_{plan}")
        row.snapshot_normalizado_json = json.dumps(
            {"atualizado_em": "invalid-financial-date"}
        )
        db.session.commit()
        decisao = decidir_beneficio_pago_para_transferencia(origem.id)
        assert decisao == BENEFICIO_INCONCLUSIVO
        assert decisao != BENEFICIO_PAGO_VIGENTE
        _aceite_http_ou_servico(app, alvo, out.token, out.convite_id)
        _assert_estado_transacional_origem(
            alvo, conta, out.convite_id, original, fr_origem, out.franquia_id
        )


@pytest.mark.parametrize("plan", ["starter", "pro"])
def test_rc03_snapshot_invalido_nao_escapa_por_vigencia_futura(app, plan):
    from app.models import MonetizacaoFato
    from app.services.conta_multiuser_convite_service import (
        BENEFICIO_INCONCLUSIVO,
        BENEFICIO_PAGO_VIGENTE,
        decidir_beneficio_pago_para_transferencia,
    )

    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            f"rc03s-vf-{plan}", qtd=3, email=f"rc03s.vf.{plan}.c@test.com"
        )
        origem, fr_o = seed_conta_franquia_cliente(slug=f"rc03s-vf-{plan}-o")
        alvo = seed_usuario(
            fr_o.id, origem.id, email=f"rc03s.vf.{plan}@test.com", categoria=plan
        )
        original = alvo.conta_id
        fr_origem = alvo.franquia_id
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        row = _cenario_falha_mensal_vigente(origem.id, alvo, plan, f"in_rc03s_vf_{plan}")
        MonetizacaoFato.query.filter_by(conta_id=origem.id).delete()
        row.snapshot_normalizado_json = "{}"
        row.vigencia_externa_fim = utcnow_naive() + timedelta(days=10)
        row.snapshot_normalizado_json = json.dumps(
            {"atualizado_em": "invalid-financial-date"}
        )
        db.session.commit()
        decisao = decidir_beneficio_pago_para_transferencia(origem.id)
        assert decisao == BENEFICIO_INCONCLUSIVO
        assert decisao != BENEFICIO_PAGO_VIGENTE
        _aceite_http_ou_servico(app, alvo, out.token, out.convite_id)
        _assert_estado_transacional_origem(
            alvo, conta, out.convite_id, original, fr_origem, out.franquia_id
        )


def _ativar_plano_saas_via_webhook_stripe(user: User, plano: str, sufixo: str):
    from app.services.cleiton_monetizacao_service import processar_evento_stripe

    agora = utcnow_naive()
    customer_id = f"cus_f4real_{sufixo}"
    subscription_id = f"sub_f4real_{sufixo}"
    meta = {
        "conta_id": str(user.conta_id),
        "franquia_id": str(user.franquia_id),
        "usuario_id": str(user.id),
        "plano_interno": plano,
    }
    processar_evento_stripe(
        {
            "id": f"evt_f4real_paid_{sufixo}",
            "type": "invoice.paid",
            "created": int(agora.timestamp()),
            "data": {
                "object": {
                    "id": f"in_f4real_{sufixo}",
                    "customer": customer_id,
                    "subscription": subscription_id,
                    "status": "paid",
                    "billing_reason": "subscription_create",
                    "period_start": int((agora - timedelta(days=1)).timestamp()),
                    "period_end": int((agora + timedelta(days=29)).timestamp()),
                    "lines": {
                        "data": [
                            {
                                "period": {
                                    "start": int((agora - timedelta(days=1)).timestamp()),
                                    "end": int((agora + timedelta(days=29)).timestamp()),
                                },
                                "price": {"id": f"price_{plano}"},
                            }
                        ]
                    },
                    "metadata": meta,
                }
            },
        }
    )
    processar_evento_stripe(
        {
            "id": f"evt_f4real_sub_{sufixo}",
            "type": "customer.subscription.updated",
            "created": int(agora.timestamp()),
            "data": {
                "object": {
                    "id": subscription_id,
                    "customer": customer_id,
                    "status": "active",
                    "current_period_start": int((agora - timedelta(days=1)).timestamp()),
                    "current_period_end": int((agora + timedelta(days=29)).timestamp()),
                    "items": {"data": [{"price": {"id": f"price_{plano}"}}]},
                    "metadata": meta,
                }
            },
        }
    )
    return customer_id, subscription_id


def _encerrar_assinatura_saas_via_webhook_stripe(
    user: User, plano: str, customer_id: str, subscription_id: str, sufixo: str
):
    from app.services.cleiton_monetizacao_service import processar_evento_stripe

    agora = utcnow_naive()
    processar_evento_stripe(
        {
            "id": f"evt_f4real_del_{sufixo}",
            "type": "customer.subscription.deleted",
            "created": int(agora.timestamp()),
            "data": {
                "object": {
                    "id": subscription_id,
                    "customer": customer_id,
                    "status": "canceled",
                    "current_period_start": int((agora - timedelta(days=31)).timestamp()),
                    "current_period_end": int((agora - timedelta(days=1)).timestamp()),
                    "items": {"data": [{"price": {"id": f"price_{plano}"}}]},
                    "metadata": {
                        "conta_id": str(user.conta_id),
                        "franquia_id": str(user.franquia_id),
                        "usuario_id": str(user.id),
                        "plano_interno": plano,
                    },
                }
            },
        }
    )


def _cenario_free_historico_mais_concessao_adm(alvo: User, plano: str, sufixo: str):
    """Stripe pago → subscription.deleted → vínculo Free → concessão ADM posterior."""
    customer_id, subscription_id = _ativar_plano_saas_via_webhook_stripe(
        alvo, plano, sufixo
    )
    alvo = db.session.get(User, alvo.id)
    _encerrar_assinatura_saas_via_webhook_stripe(
        alvo, plano, customer_id, subscription_id, sufixo
    )
    alvo = db.session.get(User, alvo.id)
    vinculo_livre = ContaMonetizacaoVinculo.query.filter_by(
        conta_id=alvo.conta_id, ativo=True
    ).one()
    assert vinculo_livre.plano_interno == "free"
    assert alvo.categoria == "free"
    atribuir_plano_para_usuario(email=alvo.email, plano_raw=plano)
    alvo = db.session.get(User, alvo.id)
    vinculo_apos_adm = ContaMonetizacaoVinculo.query.filter_by(
        conta_id=alvo.conta_id, ativo=True
    ).one()
    assert vinculo_apos_adm.plano_interno == "free"
    assert alvo.categoria == plano
    return alvo


def _alvo_cadastro_real(email: str) -> User:
    from app.auth_services import register_user

    user, erro = register_user("Alvo F4", email, "Test-password-123!", accept_terms=True)
    assert erro is None
    return user


@pytest.mark.parametrize("plano", ["starter", "pro"])
def test_f4_plano_saas_webhook_stripe_real_bloqueia_aceite(app, plano):
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            f"f4-real-st-{plano}", qtd=3, email=f"f4.real.st.{plano}.c@test.com"
        )
        alvo = _alvo_cadastro_real(f"f4.real.st.{plano}@test.com")
        origem_id, origem_fr = alvo.conta_id, alvo.franquia_id
        _ativar_plano_saas_via_webhook_stripe(alvo, plano, f"{plano}_wh")
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        _aceite_http_ou_servico(app, alvo, out.token, out.convite_id)
        _assert_estado_transacional_origem(
            alvo, conta, out.convite_id, origem_id, origem_fr, out.franquia_id
        )
        persistido = db.session.get(User, alvo.id)
        assert persistido.categoria == plano


@pytest.mark.parametrize("plano", ["starter", "pro"])
def test_f4_plano_saas_admin_real_bloqueia_aceite(app, plano):
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            f"f4-real-ad-{plano}", qtd=3, email=f"f4.real.ad.{plano}.c@test.com"
        )
        alvo = _alvo_cadastro_real(f"f4.real.ad.{plano}@test.com")
        atribuir_plano_para_usuario(email=alvo.email, plano_raw=plano)
        alvo = db.session.get(User, alvo.id)
        origem_id, origem_fr = alvo.conta_id, alvo.franquia_id
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        with pytest.raises(ConviteContratoIncompativelError):
            aceitar_convite(
                user=alvo,
                token=out.token,
                csrf_token=gerar_csrf_token_aceite(alvo.id, out.convite_id),
            )
        _assert_estado_transacional_origem(
            alvo, conta, out.convite_id, origem_id, origem_fr, out.franquia_id
        )
        persistido = db.session.get(User, alvo.id)
        assert persistido.categoria == plano


def test_f4_free_cadastro_real_transfere(app):
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-real-free", qtd=3, email="f4.real.free.c@test.com"
        )
        alvo = _alvo_cadastro_real("f4.real.free@test.com")
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        resultado = aceitar_convite(
            user=alvo,
            token=out.token,
            csrf_token=gerar_csrf_token_aceite(alvo.id, out.convite_id),
        )
        persistido = db.session.get(User, alvo.id)
        convite = db.session.get(ContaMultiuserConvite, out.convite_id)
        assert resultado.conta_id == conta.id
        assert persistido.conta_id == conta.id
        assert persistido.franquia_id == out.franquia_id
        assert persistido.categoria == "multiuser"
        assert convite.estado == ContaMultiuserConvite.ESTADO_ACEITO
        assert (
            ContaVinculoOrganizacional.query.filter_by(
                user_id=persistido.id, conta_id=conta.id, estado="ativo"
            ).count()
            == 1
        )


def test_f4_aceite_falha_no_meio_da_mutacao_nao_persiste(app, monkeypatch):
    from app.services import conta_multiuser_convite_service as svc

    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            "f4-real-mid", qtd=3, email="f4.real.mid.c@test.com"
        )
        alvo = _alvo_cadastro_real("f4.real.mid@test.com")
        origem_id, origem_fr = alvo.conta_id, alvo.franquia_id
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)

        def _quebra_ocupacao(**kwargs):
            raise RuntimeError("F4_MUTATION_MIDPOINT")

        monkeypatch.setattr(svc, "ocupar_assento", _quebra_ocupacao)
        with pytest.raises(RuntimeError, match="F4_MUTATION_MIDPOINT"):
            aceitar_convite(
                user=alvo,
                token=out.token,
                csrf_token=gerar_csrf_token_aceite(alvo.id, out.convite_id),
            )
        _assert_estado_transacional_origem(
            alvo, conta, out.convite_id, origem_id, origem_fr, out.franquia_id
        )


@pytest.mark.parametrize("plano", ["starter", "pro"])
def test_f4_free_historico_mais_concessao_adm_vigente_bloqueia_aceite(app, plano):
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            f"f4-hist-adm-{plano}", qtd=3, email=f"f4.hist.adm.{plano}.c@test.com"
        )
        alvo = _alvo_cadastro_real(f"f4.hist.adm.{plano}@test.com")
        alvo = _cenario_free_historico_mais_concessao_adm(
            alvo, plano, f"{plano}_hist_adm"
        )
        fr = db.session.get(Franquia, alvo.franquia_id)
        agora = utcnow_naive()
        assert fr.fim_ciclo is not None and fr.fim_ciclo > agora
        user_id = int(alvo.id)
        origem_id, origem_fr = int(alvo.conta_id), int(alvo.franquia_id)
        dest_id = int(conta.id)
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        convite_id = int(out.convite_id)
        dest_fr = int(out.franquia_id)
        _aceite_http_ou_servico(app, alvo, out.token, convite_id)
        db.session.remove()
        persistido = db.session.get(User, user_id)
        dest = db.session.get(Conta, dest_id)
        _assert_estado_transacional_origem(
            persistido, dest, convite_id, origem_id, origem_fr, dest_fr
        )
        assert persistido.categoria == plano


@pytest.mark.parametrize("plano", ["starter", "pro"])
def test_f4_free_historico_mais_concessao_adm_vencida_nao_bloqueia(app, plano):
    with app.app_context():
        _secret(app)
        conta, contratante = _preparar_conta_multiuser(
            f"f4-hist-venc-{plano}", qtd=3, email=f"f4.hist.venc.{plano}.c@test.com"
        )
        alvo = _alvo_cadastro_real(f"f4.hist.venc.{plano}@test.com")
        alvo = _cenario_free_historico_mais_concessao_adm(
            alvo, plano, f"{plano}_hist_venc"
        )
        agora = utcnow_naive()
        for fr in Franquia.query.filter_by(conta_id=alvo.conta_id).all():
            if fr.fim_ciclo is not None:
                fr.fim_ciclo = agora - timedelta(days=1)
                db.session.add(fr)
        db.session.commit()
        alvo = db.session.get(User, alvo.id)
        fr = db.session.get(Franquia, alvo.franquia_id)
        assert fr.fim_ciclo is not None and fr.fim_ciclo <= agora
        assert alvo.categoria == plano
        vinculo = ContaMonetizacaoVinculo.query.filter_by(
            conta_id=alvo.conta_id, ativo=True
        ).one()
        assert vinculo.plano_interno == "free"
        assert conta_possui_beneficio_pago_vigente(alvo.conta_id) is False
        dest_id = int(conta.id)
        user_id = int(alvo.id)
        out = criar_convite(ator=contratante, email_destino=alvo.email, enviar=False)
        convite_id = int(out.convite_id)
        dest_fr = int(out.franquia_id)
        resultado = aceitar_convite(
            user=alvo,
            token=out.token,
            csrf_token=gerar_csrf_token_aceite(alvo.id, convite_id),
        )
        resultado_conta = int(resultado.conta_id)
        db.session.remove()
        persistido = db.session.get(User, user_id)
        convite = db.session.get(ContaMultiuserConvite, convite_id)
        assert resultado_conta == dest_id
        assert persistido.conta_id == dest_id
        assert persistido.franquia_id == dest_fr
        assert persistido.categoria == "multiuser"
        assert convite.estado == ContaMultiuserConvite.ESTADO_ACEITO
        assert (
            ContaVinculoOrganizacional.query.filter_by(
                user_id=persistido.id, conta_id=dest_id, estado="ativo"
            ).count()
            == 1
        )
