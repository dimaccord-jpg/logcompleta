"""Fundação da entrevista condicional de onboarding. Sem WhatsApp e sem personalização."""
from __future__ import annotations

import importlib
import inspect
import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import flask_login.utils
import pytest
from sqlalchemy.exc import IntegrityError

from app.auth_services import (
    _is_profile_complete,
    _select_canonical_user,
    complete_user_profile,
    handle_google_oauth_callback,
    perfil_cadastro_completo,
    register_user,
)
from app.extensions import db
from app.models import (
    FunnelEvent,
    MonetizacaoFato,
    OnboardingRespostaDeclarada,
    User,
)
from app.services.onboarding_entrevista_definicao import (
    JOB_ROLES,
    ORIGEM_CADASTRO_WEB,
    ORIGEM_PERFIL_USUARIO,
    RAMOS_FUTUROS_NAO_ATIVOS,
    TAXONOMIA_VERSAO,
    entrevista_ativa,
    entrevista_encerrada,
    proxima_pergunta,
    validar_combinacao,
    validar_declaracao,
)
from app.services.onboarding_entrevista_service import (
    aplicar_declaracao_entrevista,
    declarar_cargo_e_entrevista,
    extrair_respostas_formulario,
)
from app.services.user_lifecycle_service import (
    anonimizar_perfil_operacional_para_encerramento,
    encerrar_vinculo_operacional_usuario,
)
from app.services.user_privacy_rights_service import processar_exercicio_privacidade_usuario
from tests.conftest import seed_conta_franquia_cliente, seed_usuario

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "versions" / "l3m4n5o6p7q8_onboarding_resposta_declarada.py"


def _load_web():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


def _usuario(email: str, *, slug: str, job_role: str | None = None, usage_purpose: str | None = None):
    conta, franquia = seed_conta_franquia_cliente(slug=slug)
    user = seed_usuario(franquia.id, conta.id, email=email)
    user.job_role = job_role
    user.usage_purpose = usage_purpose
    user.categoria = "free"
    user.creditos = 10
    franquia.limite_total = 50
    db.session.commit()
    return user


def _patch_limite(monkeypatch):
    monkeypatch.setattr(
        "app.auth_services._get_free_franquia_limite_onboarding",
        lambda: 50,
    )


def test_taxonomia_preserva_cargos_e_acrescenta_motorista():
    chaves = [key for key, _label in JOB_ROLES]
    assert chaves == [
        "analista",
        "coordenador",
        "gerente",
        "diretor",
        "proprietario",
        "motorista_entregador",
        "outro",
    ]
    assert dict(JOB_ROLES)["motorista_entregador"] == "Motorista / Entregador"
    assert entrevista_ativa("motorista_entregador") is not None
    for role, _question in RAMOS_FUTUROS_NAO_ATIVOS:
        assert entrevista_ativa(role) is None
    assert entrevista_ativa("Analista") is None
    assert entrevista_ativa("ops") is None


def test_ramos_futuros_nao_estao_ativos():
    assert ("gerente", "segmento_atuacao") in RAMOS_FUTUROS_NAO_ATIVOS
    assert ("proprietario", "tipo_empresa") in RAMOS_FUTUROS_NAO_ATIVOS
    assert ("analista", "area_atuacao") in RAMOS_FUTUROS_NAO_ATIVOS
    for _role, question_key in RAMOS_FUTUROS_NAO_ATIVOS:
        resultado = validar_combinacao("gerente", {question_key: "qualquer"})
        assert resultado.ok is False
        assert resultado.codigo == "pergunta_inexistente"


def test_navegacao_da_entrevista_motorista():
    assert proxima_pergunta("motorista_entregador", {}).key == "tipo_atuacao"
    assert entrevista_encerrada("motorista_entregador", {}) is False
    assert proxima_pergunta("motorista_entregador", {"tipo_atuacao": "tac"}) is None
    assert entrevista_encerrada("motorista_entregador", {"tipo_atuacao": "tac"}) is True
    assert (
        proxima_pergunta("motorista_entregador", {"tipo_atuacao": "entregador_app"}).key
        == "veiculo_principal"
    )
    assert entrevista_encerrada("gerente", {}) is True
    assert proxima_pergunta("gerente", {}) is None


@pytest.mark.parametrize(
    ("role", "respostas", "codigo"),
    [
        ("gerente", {"tipo_atuacao": "tac"}, "pergunta_inaplicavel"),
        ("motorista_entregador", {}, "resposta_obrigatoria"),
        ("motorista_entregador", {"tipo_atuacao": "entregador_app"}, "resposta_obrigatoria"),
        ("motorista_entregador", {"tipo_atuacao": "inexistente"}, "resposta_invalida"),
        ("motorista_entregador", {"ramo_alheio": "moto"}, "pergunta_inexistente"),
        (
            "motorista_entregador",
            {"tipo_atuacao": "tac", "veiculo_principal": "moto"},
            "combinacao_incoerente",
        ),
        (
            "motorista_entregador",
            {"tipo_atuacao": "motorista_app", "veiculo_principal": "carro"},
            "combinacao_incoerente",
        ),
        ("analista", {"segmento_atuacao": "logistica"}, "pergunta_inexistente"),
    ],
)
def test_validacao_rejeita_combinacoes(role, respostas, codigo):
    resultado = validar_combinacao(role, respostas)
    assert resultado.ok is False
    assert resultado.codigo == codigo


@pytest.mark.parametrize(
    "respostas",
    [
        {"tipo_atuacao": "tac"},
        {"tipo_atuacao": "motorista_app"},
        {"tipo_atuacao": "motorista_profissional"},
        {"tipo_atuacao": "outro"},
        {"tipo_atuacao": "entregador_app", "veiculo_principal": "moto"},
        {"tipo_atuacao": "entregador_app", "veiculo_principal": "bicicleta"},
    ],
)
def test_validacao_aceita_ramos_do_motorista(respostas):
    assert validar_combinacao("motorista_entregador", respostas).ok is True


def test_origem_inferida_nao_e_declaracao():
    resultado = validar_declaracao(
        "motorista_entregador",
        {"tipo_atuacao": "tac"},
        "ia_inferida",
    )
    assert resultado.ok is False
    assert resultado.codigo == "origem_invalida"


def test_cadastro_analista_continua_sem_entrevista(ctx, monkeypatch):
    _patch_limite(monkeypatch)
    user, err = register_user(
        "Ana",
        "ana.analista@example.com",
        "senha123",
        job_role="analista",
        usage_purpose="trabalho",
        accept_terms=True,
    )
    assert err is None
    assert user.job_role == "analista"
    assert user.usage_purpose == "trabalho"
    assert OnboardingRespostaDeclarada.query.filter_by(user_id=user.id).count() == 0
    assert perfil_cadastro_completo(user) is True


def test_gerente_cadastro_sem_entrevista_complementar(ctx, monkeypatch):
    _patch_limite(monkeypatch)
    user, err = register_user(
        "Gero",
        "gero.gerente@example.com",
        "senha123",
        job_role="gerente",
        usage_purpose="trabalho",
    )
    assert err is None
    assert user.job_role == "gerente"
    assert OnboardingRespostaDeclarada.query.count() == 0
    assert perfil_cadastro_completo(user) is True


def test_motorista_exige_tipo_atuacao(ctx, monkeypatch):
    _patch_limite(monkeypatch)
    user, err = register_user(
        "Moto",
        "moto.sem@example.com",
        "senha123",
        job_role="motorista_entregador",
        usage_purpose="pessoal",
    )
    assert user is None
    assert err
    assert User.query.filter_by(email="moto.sem@example.com").count() == 0


def test_entregador_app_exige_veiculo_e_persiste_origem(ctx, monkeypatch):
    _patch_limite(monkeypatch)
    incompleto, err = register_user(
        "Ent",
        "ent.sem.veiculo@example.com",
        "senha123",
        job_role="motorista_entregador",
        usage_purpose="pessoal",
        respostas_entrevista={"tipo_atuacao": "entregador_app"},
    )
    assert incompleto is None
    assert err

    user, err = register_user(
        "Ent",
        "ent.com.veiculo@example.com",
        "senha123",
        job_role="motorista_entregador",
        usage_purpose="pessoal",
        respostas_entrevista={
            "tipo_atuacao": "entregador_app",
            "veiculo_principal": "moto",
        },
        origem_entrevista=ORIGEM_CADASTRO_WEB,
    )
    assert err is None
    rows = {
        row.question_key: row
        for row in OnboardingRespostaDeclarada.query.filter_by(user_id=user.id).all()
    }
    assert set(rows) == {"tipo_atuacao", "veiculo_principal"}
    assert rows["tipo_atuacao"].answer_key == "entregador_app"
    assert rows["veiculo_principal"].answer_key == "moto"
    assert rows["veiculo_principal"].origem == ORIGEM_CADASTRO_WEB
    assert rows["veiculo_principal"].taxonomia_versao == TAXONOMIA_VERSAO
    assert rows["veiculo_principal"].declarada_em is not None
    assert rows["veiculo_principal"].atualizada_em is not None
    assert perfil_cadastro_completo(user) is True


def test_tac_e_motorista_app_nao_exigem_veiculo(ctx, monkeypatch):
    _patch_limite(monkeypatch)
    tac, err_tac = register_user(
        "Tac",
        "tac@example.com",
        "senha123",
        job_role="motorista_entregador",
        usage_purpose="trabalho",
        respostas_entrevista={"tipo_atuacao": "tac"},
    )
    app, err_app = register_user(
        "App",
        "app.driver@example.com",
        "senha123",
        job_role="motorista_entregador",
        usage_purpose="trabalho",
        respostas_entrevista={"tipo_atuacao": "motorista_app"},
    )
    assert err_tac is None and err_app is None
    assert [row.question_key for row in tac.respostas_onboarding] == ["tipo_atuacao"]
    assert [row.question_key for row in app.respostas_onboarding] == ["tipo_atuacao"]
    assert perfil_cadastro_completo(tac) is True
    assert perfil_cadastro_completo(app) is True


def test_aplicar_declaracao_rejeita_cargo_divergente_sem_commit(ctx, monkeypatch):
    user = _usuario(
        "cargo.divergente@example.com",
        slug="cargo-divergente",
        job_role="gerente",
        usage_purpose="trabalho",
    )
    uid = user.id
    purpose_original = user.usage_purpose
    user.usage_purpose = "pessoal"

    def commit_indevido():
        pytest.fail("O helper não deve executar commit.")

    monkeypatch.setattr(db.session, "commit", commit_indevido)
    resultado = aplicar_declaracao_entrevista(
        user,
        job_role="motorista_entregador",
        respostas={"tipo_atuacao": "tac"},
        origem=ORIGEM_CADASTRO_WEB,
    )

    assert resultado.ok is False
    assert resultado.codigo == "cargo_divergente"
    assert user.job_role == "gerente"
    assert user.usage_purpose == "pessoal"
    db.session.flush()
    assert OnboardingRespostaDeclarada.query.filter_by(user_id=uid).count() == 0
    assert db.session.is_active
    db.session.rollback()
    persisted = db.session.get(User, uid)
    assert persisted.job_role == "gerente"
    assert persisted.usage_purpose == purpose_original
    assert OnboardingRespostaDeclarada.query.filter_by(user_id=uid).count() == 0


def test_resposta_incoerente_nao_apaga_estado_anterior(ctx):
    user = _usuario(
        "edit@example.com",
        slug="edit-onb",
        job_role="motorista_entregador",
        usage_purpose="pessoal",
    )
    ok = declarar_cargo_e_entrevista(
        user,
        job_role="motorista_entregador",
        respostas={"tipo_atuacao": "entregador_app", "veiculo_principal": "moto"},
        origem=ORIGEM_CADASTRO_WEB,
    )
    db.session.commit()
    assert ok.ok is True

    rejeitado = declarar_cargo_e_entrevista(
        user,
        job_role="gerente",
        respostas={"tipo_atuacao": "tac"},
        origem=ORIGEM_PERFIL_USUARIO,
    )
    assert rejeitado.codigo == "pergunta_inaplicavel"
    db.session.rollback()
    persisted = db.session.get(User, user.id)
    assert persisted.job_role == "motorista_entregador"
    chaves = {
        row.question_key: row.answer_key
        for row in OnboardingRespostaDeclarada.query.filter_by(user_id=user.id).all()
    }
    assert chaves == {"tipo_atuacao": "entregador_app", "veiculo_principal": "moto"}


def test_edicao_futura_troca_ramo_e_preserva_data_original(ctx):
    user = _usuario(
        "perfil.futuro@example.com",
        slug="perfil-futuro",
        job_role="analista",
        usage_purpose="trabalho",
    )
    declarar_cargo_e_entrevista(
        user,
        job_role="motorista_entregador",
        respostas={"tipo_atuacao": "entregador_app", "veiculo_principal": "carro"},
        origem=ORIGEM_CADASTRO_WEB,
    )
    db.session.commit()
    row = OnboardingRespostaDeclarada.query.filter_by(
        user_id=user.id, question_key="tipo_atuacao"
    ).one()
    original = datetime(2020, 1, 1, 8, 0, 0)
    row.declarada_em = original
    row.atualizada_em = original
    db.session.commit()

    resultado = declarar_cargo_e_entrevista(
        user,
        job_role="motorista_entregador",
        respostas={"tipo_atuacao": "tac"},
        origem=ORIGEM_PERFIL_USUARIO,
    )
    db.session.commit()
    assert resultado.ok is True
    atual = OnboardingRespostaDeclarada.query.filter_by(user_id=user.id).one()
    assert atual.question_key == "tipo_atuacao"
    assert atual.answer_key == "tac"
    assert atual.origem == ORIGEM_PERFIL_USUARIO
    assert atual.declarada_em == original
    assert atual.atualizada_em > original
    assert db.session.get(User, user.id).job_role == "motorista_entregador"


def test_usuario_legado_sem_respostas_continua_valido(ctx):
    legado = _usuario(
        "legado@example.com",
        slug="legado-onb",
        job_role="Analista",
        usage_purpose="Auditoria",
    )
    outro = _usuario(
        "ops.legado@example.com",
        slug="legado-ops",
        job_role="ops",
        usage_purpose="test",
    )
    assert OnboardingRespostaDeclarada.query.count() == 0
    assert perfil_cadastro_completo(legado) is True
    assert perfil_cadastro_completo(outro) is True
    assert _is_profile_complete(legado) is True


def test_selecao_canonica_e_oauth_nao_quebram(ctx):
    legado = _usuario(
        "canon.legado@example.com",
        slug="canon-leg",
        job_role="gerente",
        usage_purpose="trabalho",
    )
    incompleto = _usuario(
        "canon.moto@example.com",
        slug="canon-moto",
        job_role="motorista_entregador",
        usage_purpose="trabalho",
    )
    oauth = _usuario("oauth.novo@example.com", slug="canon-oauth")
    oauth.oauth_provider = "google"
    oauth.oauth_sub = "sub-oauth"
    db.session.commit()

    assert _is_profile_complete(oauth) is False
    escolhido = _select_canonical_user([incompleto, legado, oauth], "sub-oauth")
    assert escolhido.id == oauth.id
    sem_sub = _select_canonical_user([incompleto, legado], None)
    assert sem_sub.id == legado.id
    assert "needs_profile = False" in inspect.getsource(handle_google_oauth_callback)


def test_complete_profile_analista_e_motorista(ctx):
    user = _usuario("complete@example.com", slug="complete-onb")
    ok, message = complete_user_profile(
        user,
        "analista",
        "trabalho",
        False,
        accept_terms=True,
    )
    assert ok is True
    assert "sucesso" in message.lower() or message
    assert db.session.get(User, user.id).job_role == "analista"
    assert OnboardingRespostaDeclarada.query.filter_by(user_id=user.id).count() == 0

    falha, erro = complete_user_profile(
        user,
        "motorista_entregador",
        "pessoal",
        False,
        respostas_entrevista={"tipo_atuacao": "tac", "veiculo_principal": "moto"},
    )
    assert falha is False
    assert erro
    assert db.session.get(User, user.id).job_role == "analista"

    ok_moto, _msg = complete_user_profile(
        user,
        "motorista_entregador",
        "pessoal",
        False,
        respostas_entrevista={"tipo_atuacao": "motorista_app"},
    )
    assert ok_moto is True
    persisted = db.session.get(User, user.id)
    assert persisted.job_role == "motorista_entregador"
    assert persisted.usage_purpose == "pessoal"
    assert perfil_cadastro_completo(persisted) is True
    row = OnboardingRespostaDeclarada.query.filter_by(user_id=user.id).one()
    assert row.answer_key == "motorista_app"
    assert row.origem == ORIGEM_CADASTRO_WEB


def test_privacidade_e_encerramento_removem_respostas(ctx):
    user = _usuario(
        "priv.onb@example.com",
        slug="priv-onb",
        job_role="motorista_entregador",
        usage_purpose="pessoal",
    )
    outro = _usuario(
        "priv.outro@example.com",
        slug="priv-onb-b",
        job_role="motorista_entregador",
        usage_purpose="pessoal",
    )
    for pessoa, origem in ((user, ORIGEM_CADASTRO_WEB), (outro, ORIGEM_PERFIL_USUARIO)):
        aplicar_declaracao_entrevista(
            pessoa,
            job_role="motorista_entregador",
            respostas={"tipo_atuacao": "tac"},
            origem=origem,
        )
    db.session.commit()

    seco = processar_exercicio_privacidade_usuario(user, apply=False)
    assert seco.user_deidentified is False
    assert OnboardingRespostaDeclarada.query.filter_by(user_id=user.id).count() == 1

    processar_exercicio_privacidade_usuario(db.session.get(User, user.id), apply=True)
    assert OnboardingRespostaDeclarada.query.filter_by(user_id=user.id).count() == 0
    assert OnboardingRespostaDeclarada.query.filter_by(user_id=outro.id).count() == 1

    encerrar_vinculo_operacional_usuario(db.session.get(User, outro.id))
    assert OnboardingRespostaDeclarada.query.filter_by(user_id=outro.id).count() == 0


def test_anonimizacao_sem_commit_nao_apaga_resposta(ctx):
    user = _usuario(
        "rollback.onb@example.com",
        slug="rollback-onb",
        job_role="motorista_entregador",
        usage_purpose="pessoal",
    )
    aplicar_declaracao_entrevista(
        user,
        job_role="motorista_entregador",
        respostas={"tipo_atuacao": "outro"},
        origem=ORIGEM_CADASTRO_WEB,
    )
    db.session.commit()
    uid = user.id

    anonimizar_perfil_operacional_para_encerramento(user)
    assert OnboardingRespostaDeclarada.query.filter_by(user_id=uid).count() == 0
    db.session.rollback()
    assert OnboardingRespostaDeclarada.query.filter_by(user_id=uid).count() == 1


def test_origem_fora_do_contrato_nao_grava(ctx):
    user = _usuario("origem@example.com", slug="origem-onb")
    row = OnboardingRespostaDeclarada(
        user_id=user.id,
        question_key="tipo_atuacao",
        answer_key="tac",
        origem="ia_inferida",
        taxonomia_versao=TAXONOMIA_VERSAO,
        declarada_em=datetime(2026, 9, 30, 12, 0, 0),
        atualizada_em=datetime(2026, 9, 30, 12, 0, 0),
    )
    db.session.add(row)
    with pytest.raises(IntegrityError):
        db.session.flush()
    db.session.rollback()
    assert OnboardingRespostaDeclarada.query.count() == 0


def test_declaracao_nao_altera_billing_nem_growth(ctx, monkeypatch):
    _patch_limite(monkeypatch)
    antes_fatos = MonetizacaoFato.query.count()
    antes_funil = FunnelEvent.query.count()
    analista, err_a = register_user(
        "Ana",
        "bill.analista@example.com",
        "senha123",
        job_role="analista",
        usage_purpose="trabalho",
    )
    motorista, err_m = register_user(
        "Mo",
        "bill.moto@example.com",
        "senha123",
        job_role="motorista_entregador",
        usage_purpose="trabalho",
        respostas_entrevista={"tipo_atuacao": "tac"},
    )
    assert err_a is None and err_m is None
    assert analista.categoria == motorista.categoria == "free"
    assert analista.creditos == motorista.creditos
    assert analista.franquia.limite_total == motorista.franquia.limite_total == 50
    assert analista.franquia.bloqueio_manual is False
    assert motorista.franquia.bloqueio_manual is False
    assert motorista.is_admin is False
    assert MonetizacaoFato.query.count() == antes_fatos
    assert FunnelEvent.query.count() == antes_funil


def test_migration_e_aditiva_e_fecha_origem():
    texto = MIGRATION.read_text(encoding="utf-8")
    assert 'revision = "l3m4n5o6p7q8"' in texto
    assert 'down_revision = "k2l3m4n5o6p7"' in texto
    assert "onboarding_resposta_declarada" in texto
    assert OnboardingRespostaDeclarada._SQL_ORIGEM in texto
    assert "op.add_column" not in texto
    assert "op.alter_column" not in texto


def test_rotulo_email_e_cargo_novo_no_cadastro():
    web = _load_web()
    html = web.app.test_client().get("/login?mode=register").get_data(as_text=True)
    assert "E-mail Corporativo" not in html
    assert ">E-mail</label>" in html
    assert "Motorista / Entregador" in html
    assert 'value="analista"' in html
    assert 'value="gerente"' in html
    assert "Como você atua principalmente?" in html
    assert "Qual veículo você utiliza principalmente?" in html
    assert 'name="entrevista_tipo_atuacao"' in html
    assert 'name="entrevista_veiculo_principal"' in html
    assert 'data-depends-answer="entregador_app"' in html
    assert "hidden" in html

    with web.app.test_request_context("/complete-profile"):
        complete = web.render_template("complete_profile.html", active_term=None)
    assert "Motorista / Entregador" in complete
    assert "Como você atua principalmente?" in complete
    assert 'name="entrevista_veiculo_principal"' in complete
    assert "E-mail Corporativo" not in complete

    admin = (ROOT / "app" / "painel_admin" / "template_admin" / "complete_profile.html").read_text(
        encoding="utf-8"
    )
    assert 'include "partials/onboarding_entrevista_campos.html"' in admin
    assert 'value="analista"' not in admin


def test_rotas_repassam_entrevista_sem_mudar_growth(monkeypatch):
    web = _load_web()
    monkeypatch.setattr(web, "get_active_term", lambda: None)
    captured = {}
    growth = []

    def _register(*_args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id=7, conta_id=1, franquia_id=1), None

    monkeypatch.setattr(web, "register_user", _register)
    monkeypatch.setattr(
        web,
        "try_record_signup_completed",
        lambda user, **kwargs: growth.append({"user_id": user.id, **kwargs}),
    )
    monkeypatch.setattr(
        "app.services.admin_desktop_access_test_service.try_registration_replay",
        lambda **_k: None,
    )
    client = web.app.test_client()
    resp = client.post(
        "/register",
        data={
            "nome": "Entregador",
            "email": "rota.moto@example.com",
            "password": "senha-segura-123",
            "accept_terms": "1",
            "job_role": "motorista_entregador",
            "usage_purpose": "pessoal",
            "entrevista_tipo_atuacao": "entregador_app",
            "entrevista_veiculo_principal": "bicicleta",
            "entrevista_vazia": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert captured["respostas_entrevista"] == {
        "tipo_atuacao": "entregador_app",
        "veiculo_principal": "bicicleta",
    }
    assert captured["origem_entrevista"] == ORIGEM_CADASTRO_WEB
    assert growth == [{"user_id": 7, "signup_method": "password"}]
    assert "job_role" not in growth[0]


def test_complete_profile_rota_repassa_respostas(monkeypatch):
    web = _load_web()
    fake = SimpleNamespace(
        id=3,
        email="novo@test.com",
        is_authenticated=True,
        is_active=True,
        is_anonymous=False,
        get_id=lambda: "3",
        job_role="",
        usage_purpose="",
        is_admin=False,
        full_name="Teste",
        conta_id=1,
        franquia_id=1,
        categoria="free",
    )
    captured = {}

    def _complete(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return True, "Perfil atualizado"

    monkeypatch.setattr(web, "auth_complete_user_profile", _complete)
    monkeypatch.setattr(web, "get_active_term", lambda: None)
    monkeypatch.setattr(flask_login.utils, "_get_user", lambda: fake)
    monkeypatch.setattr(web, "current_user", fake)
    client = web.app.test_client()
    resp = client.post(
        "/complete-profile",
        data={
            "accept_terms": "1",
            "job_role": "motorista_entregador",
            "usage_purpose": "pessoal",
            "entrevista_tipo_atuacao": "tac",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    assert captured["kwargs"]["respostas_entrevista"] == {"tipo_atuacao": "tac"}
    assert captured["kwargs"]["origem_entrevista"] == ORIGEM_CADASTRO_WEB


def test_complete_profile_legado_segue_encerrado(monkeypatch):
    web = _load_web()
    fake = SimpleNamespace(
        id=4,
        is_authenticated=True,
        is_active=True,
        is_anonymous=False,
        get_id=lambda: "4",
        job_role="gerente",
        usage_purpose="trabalho",
        is_admin=False,
        conta_id=1,
        franquia_id=1,
        email="gerente.legado@example.com",
        full_name="Gerente",
    )
    monkeypatch.setattr(flask_login.utils, "_get_user", lambda: fake)
    monkeypatch.setattr(web, "current_user", fake)
    resp = web.app.test_client().get("/complete-profile", follow_redirects=False)
    assert resp.status_code in (302, 303)
    assert "complete-profile" not in (resp.headers.get("Location") or "")


def test_extrair_formulario_ignora_vazio():
    class _Form(dict):
        def get(self, key, default=None):
            return dict.get(self, key, default)

    respostas = extrair_respostas_formulario(
        _Form(
            {
                "job_role": "motorista_entregador",
                "entrevista_tipo_atuacao": "tac",
                "entrevista_veiculo_principal": "  ",
            }
        )
    )
    assert respostas == {"tipo_atuacao": "tac"}
