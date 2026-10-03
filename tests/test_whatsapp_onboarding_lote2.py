"""Lote WhatsApp 2: conclusão da jornada, conta e vínculo. Sem Meta."""
from __future__ import annotations

import importlib.util
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from flask import Blueprint, Flask
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy.pool import StaticPool

from app.auth_services import perfil_cadastro_completo, register_user
from app.db_operational_safety import run_test_schema_operation
from app.extensions import db, login_manager
from app.models import (
    Conta,
    Franquia,
    FunnelEvent,
    IdentidadeCanalExterna,
    OnboardingCanal,
    OnboardingCanalConclusao,
    OnboardingRespostaDeclarada,
    User,
    utcnow_naive,
)
from app.onboarding_canal_routes import (
    SESSION_HANDOFF_ID,
    register_onboarding_canal_routes,
)
from app.services import onboarding_canal_conclusao_service as conclusao
from app.services.canal_aquisicao_config import url_retorno_canal_permitida
from app.services.canal_aquisicao_service import (
    iniciar_onboarding_canal,
    obter_ou_criar_identidade_externa,
    registrar_resposta_onboarding,
)
from app.services.onboarding_entrevista_definicao import (
    ORIGEM_ONBOARDING_WHATSAPP,
    TAXONOMIA_VERSAO,
)
from app.services.user_lifecycle_service import encerrar_vinculo_operacional_usuario
from app.shell_navigation import build_shell_navigation
from tests.conftest import PYTEST_DISPOSABLE_SQLALCHEMY_URI, seed_conta_franquia_cliente, seed_usuario

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "versions" / "n5o6p7q8r9s0_onboarding_canal_conclusao.py"
SECRET = "teste-conclusao-canal-secret"
SENHA = "SenhaCanal#2026"
PROVEDOR = "canal_sintetico"


def _migration_module():
    spec = importlib.util.spec_from_file_location("mig_conclusao_canal", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _patch_limite(monkeypatch):
    monkeypatch.setattr(
        "app.auth_services._get_free_franquia_limite_onboarding",
        lambda: 50,
    )


def _url(token: str) -> str:
    return f"/onboarding/canal/concluir/{token}"


def _ident(sujeito: str):
    return obter_ou_criar_identidade_externa(provedor=PROVEDOR, sujeito_externo=sujeito)


def _ate_senha(
    sujeito: str,
    email: str,
    *,
    nome: str = "Maria Guest",
    job_role: str = "analista",
    entrevista: list[tuple[str, str]] | None = None,
):
    ident = _ident(sujeito)
    iniciar_onboarding_canal(ident.id)
    registrar_resposta_onboarding(ident.id, campo="aceitar_convite")
    registrar_resposta_onboarding(ident.id, campo="nome", valor=nome)
    registrar_resposta_onboarding(ident.id, campo="email", valor=email)
    registrar_resposta_onboarding(ident.id, campo="job_role", valor=job_role)
    for question_key, valor in entrevista or []:
        registrar_resposta_onboarding(
            ident.id,
            campo="entrevista",
            question_key=question_key,
            valor=valor,
        )
    registrar_resposta_onboarding(
        ident.id,
        campo="apresentar_termos",
        termos_referencia="terms-v1",
    )
    registrar_resposta_onboarding(
        ident.id,
        campo="declarar_aceite_termos",
        termos_referencia="terms-v1",
        aceite_declarado=True,
    )
    return ident


def _emitir(identidade_id: int):
    return conclusao.emitir_link_conclusao_onboarding(
        identidade_id,
        secret_key=SECRET,
        build_url=_url,
    )


def _concluir(token: str, senha: str = SENHA, confirma: str | None = None):
    return conclusao.concluir_definicao_senha(
        token,
        senha,
        SENHA if confirma is None else confirma,
        secret_key=SECRET,
    )


def _texto_modelos(*modelos) -> str:
    pedacos = []
    for modelo in modelos:
        for row in modelo.query.all():
            pedacos.append(
                " ".join(str(getattr(row, coluna.name)) for coluna in modelo.__table__.columns)
            )
    return "\n".join(pedacos)


def test_migration_fecha_predicados_do_modelo():
    modulo = _migration_module()
    assert modulo.revision == "n5o6p7q8r9s0"
    assert modulo.down_revision == "m4n5o6p7q8r9"
    assert modulo._SQL_FINALIDADE == OnboardingCanalConclusao._SQL_FINALIDADE
    assert modulo._SQL_ESTADO == OnboardingCanalConclusao._SQL_ESTADO
    assert modulo._SQL_COERENCIA == OnboardingCanalConclusao._SQL_COERENCIA
    assert modulo._SQL_HASH == OnboardingCanalConclusao._SQL_HASH
    assert modulo._SQL_EMITIDO_ATIVO == OnboardingCanalConclusao._SQL_EMITIDO_ATIVO
    assert modulo._SQL_CADASTRO_ORIGEM == User._SQL_CADASTRO_ORIGEM
    texto = MIGRATION.read_text(encoding="utf-8")
    assert 'sa.Column("senha"' not in texto
    assert 'sa.Column("email"' not in texto
    assert 'sa.Column("telefone"' not in texto


def test_politica_de_senha_e_cadastro_web_preservado(ctx, monkeypatch):
    _patch_limite(monkeypatch)
    recusado, erro = register_user(
        "Ana",
        "ana.curta@example.com",
        "1234567",
        job_role="analista",
        usage_purpose="trabalho",
        accept_terms=True,
    )
    assert recusado is None
    assert "8" in (erro or "")
    user, err = register_user(
        "Ana",
        "ana.web@example.com",
        "senha123",
        job_role="analista",
        usage_purpose="trabalho",
        accept_terms=True,
    )
    assert err is None
    assert user.cadastro_origem is None
    assert user.usage_purpose == "trabalho"
    assert perfil_cadastro_completo(user) is True
    sem_objetivo = User.query.filter_by(email="ana.web@example.com").one()
    sem_objetivo.usage_purpose = None
    assert perfil_cadastro_completo(sem_objetivo) is False


def test_emissao_em_jornada_valida_nao_cria_user(ctx, monkeypatch):
    _patch_limite(monkeypatch)
    ident = _ate_senha("subj-emissao", "emissao.canal@example.com")
    antes = User.query.count()
    emissao = _emitir(ident.id)
    assert emissao.codigo == conclusao.CODIGO_LINK_EMITIDO
    assert emissao.token
    assert emissao.expira_em is not None
    assert emissao.url == _url(emissao.token)
    assert "emissao.canal@example.com" not in emissao.token
    assert "emissao.canal@example.com" not in (emissao.url or "")
    assert User.query.count() == antes
    payload = URLSafeTimedSerializer(SECRET, salt=conclusao.SALT_CONCLUSAO_ONBOARDING).loads(
        emissao.token
    )
    assert set(payload) == {"c", "o"}
    assert conclusao.SALT_CONCLUSAO_ONBOARDING != "password-reset-salt"
    row = OnboardingCanalConclusao.query.one()
    assert row.token_hash != emissao.token
    assert SENHA not in _texto_modelos(OnboardingCanal, OnboardingCanalConclusao)


def test_token_nao_emitido_fora_de_aguardando_senha(ctx):
    ident = _ident("subj-cedo")
    iniciar_onboarding_canal(ident.id)
    registrar_resposta_onboarding(ident.id, campo="aceitar_convite")
    registrar_resposta_onboarding(ident.id, campo="nome", valor="Maria Guest")
    emissao = _emitir(ident.id)
    assert emissao.codigo == conclusao.CODIGO_JORNADA_NAO_OPERAVEL
    assert emissao.token is None
    assert OnboardingCanalConclusao.query.count() == 0
    assert User.query.count() == 0


def test_token_expira_e_reemissao_invalida_o_anterior(ctx, monkeypatch):
    _patch_limite(monkeypatch)
    ident = _ate_senha("subj-expira-token", "expira.token@example.com")
    primeiro = _emitir(ident.id)
    row = OnboardingCanalConclusao.query.one()
    row.expira_em = utcnow_naive() - timedelta(seconds=5)
    db.session.commit()
    expirado = _concluir(primeiro.token)
    assert expirado.codigo == conclusao.CODIGO_TOKEN_EXPIRADO
    assert User.query.count() == 0
    novo = conclusao.reemitir_link_conclusao_onboarding(
        ident.id,
        secret_key=SECRET,
        build_url=_url,
    )
    assert novo.codigo == conclusao.CODIGO_LINK_EMITIDO
    assert novo.token != primeiro.token
    assert _concluir(primeiro.token).codigo == conclusao.CODIGO_TOKEN_REVOGADO
    assert _concluir(novo.token).codigo == conclusao.CODIGO_CONTA_CRIADA


def test_reemissao_recusada_em_jornada_concluida(ctx, monkeypatch):
    _patch_limite(monkeypatch)
    ident = _ate_senha("subj-reemissao-fim", "reemissao.fim@example.com")
    token = _emitir(ident.id).token
    assert _concluir(token).codigo == conclusao.CODIGO_CONTA_CRIADA
    recusa = conclusao.reemitir_link_conclusao_onboarding(ident.id, secret_key=SECRET)
    assert recusa.codigo == conclusao.CODIGO_JA_REALIZADA
    assert recusa.token is None


def test_senha_valida_materializa_conta_termos_entrevista_e_login(ctx, monkeypatch, caplog):
    _patch_limite(monkeypatch)
    ident = _ate_senha(
        "subj-materializa",
        "materializa.canal@example.com",
        nome="Marina Canal",
        job_role="motorista_entregador",
        entrevista=[
            ("tipo_atuacao", "entregador_app"),
            ("veiculo_principal", "moto"),
        ],
    )
    jornada = OnboardingCanal.query.filter_by(identidade_id=ident.id).one()
    aceite = jornada.termos_aceitos_em
    referencia = jornada.termos_referencia
    caplog.set_level("INFO")
    resultado = _concluir(_emitir(ident.id).token)
    assert resultado.codigo == conclusao.CODIGO_CONTA_CRIADA
    assert resultado.autenticar is True
    user = db.session.get(User, resultado.user_id)
    assert user.email == "materializa.canal@example.com"
    assert user.full_name == "Marina Canal"
    assert user.job_role == "motorista_entregador"
    assert user.usage_purpose is None
    assert user.cadastro_origem == User.CADASTRO_ORIGEM_WHATSAPP
    assert user.categoria == "free"
    assert user.accepted_terms_at == aceite
    assert user.last_login_at is not None
    assert user.verify_password(SENHA) is True
    assert perfil_cadastro_completo(user) is True
    conta = db.session.get(Conta, user.conta_id)
    franquia = db.session.get(Franquia, user.franquia_id)
    assert conta is not None
    assert franquia.conta_id == conta.id
    assert int(franquia.limite_total) == 50
    assert int(franquia.consumo_acumulado) == 0
    respostas = {
        (row.question_key, row.answer_key, row.origem, row.taxonomia_versao)
        for row in OnboardingRespostaDeclarada.query.filter_by(user_id=user.id)
    }
    assert respostas == {
        ("tipo_atuacao", "entregador_app", ORIGEM_ONBOARDING_WHATSAPP, TAXONOMIA_VERSAO),
        ("veiculo_principal", "moto", ORIGEM_ONBOARDING_WHATSAPP, TAXONOMIA_VERSAO),
    }
    identidade = db.session.get(IdentidadeCanalExterna, ident.id)
    assert identidade.estado == IdentidadeCanalExterna.ESTADO_VINCULADA
    assert identidade.user_id == user.id
    assert identidade.vinculada_em is not None
    concluida = OnboardingCanal.query.filter_by(identidade_id=ident.id).one()
    assert concluida.etapa == OnboardingCanal.ETAPA_CONCLUIDO
    assert concluida.concluida_em is not None
    assert concluida.termos_referencia == referencia
    assert SENHA not in _texto_modelos(
        OnboardingCanal,
        OnboardingCanalConclusao,
        IdentidadeCanalExterna,
        OnboardingRespostaDeclarada,
    )
    assert SENHA not in caplog.text
    assert "materializa.canal@example.com" not in caplog.text
    assert FunnelEvent.query.count() == 0


def test_replay_nao_cria_segunda_conta(ctx, monkeypatch):
    _patch_limite(monkeypatch)
    ident = _ate_senha("subj-replay", "replay.canal@example.com")
    token = _emitir(ident.id).token
    assert _concluir(token).codigo == conclusao.CODIGO_CONTA_CRIADA
    replay = _concluir(token)
    assert replay.codigo == conclusao.CODIGO_JA_REALIZADA
    assert User.query.filter_by(email="replay.canal@example.com").count() == 1
    assert Conta.query.count() == 1
    assert Franquia.query.count() == 1


def test_falha_intermediaria_faz_rollback_e_token_segue_util(ctx, monkeypatch):
    _patch_limite(monkeypatch)
    ident = _ate_senha("subj-rollback", "rollback.canal@example.com")
    token = _emitir(ident.id).token

    def _falha(*_args, **_kwargs):
        raise RuntimeError("falha-controlada")

    monkeypatch.setattr(conclusao, "_vincular_identidade", _falha)
    falha = _concluir(token)
    assert falha.codigo == conclusao.CODIGO_FALHA
    assert User.query.count() == 0
    assert Conta.query.count() == 0
    assert Franquia.query.count() == 0
    identidade = db.session.get(IdentidadeCanalExterna, ident.id)
    assert identidade.estado == IdentidadeCanalExterna.ESTADO_CADASTRO_EM_ANDAMENTO
    assert identidade.user_id is None
    assert OnboardingCanal.query.one().etapa == OnboardingCanal.ETAPA_SENHA
    assert OnboardingCanalConclusao.query.one().estado == OnboardingCanalConclusao.ESTADO_EMITIDO
    assert db.session.query(User).count() == 0
    monkeypatch.undo()
    _patch_limite(monkeypatch)
    assert _concluir(token).codigo == conclusao.CODIGO_CONTA_CRIADA
    assert User.query.count() == 1


def test_corrida_de_conclusao_nao_duplica(monkeypatch):
    _patch_limite(monkeypatch)
    flask_app = Flask("corrida-onboarding-canal")
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = PYTEST_DISPOSABLE_SQLALCHEMY_URI
    flask_app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "poolclass": StaticPool,
        "connect_args": {"check_same_thread": False},
    }
    flask_app.config["TESTING"] = True
    db.init_app(flask_app)
    with flask_app.app_context():
        import app.models  # noqa: F401

        run_test_schema_operation(
            db,
            PYTEST_DISPOSABLE_SQLALCHEMY_URI,
            testing=True,
            operation="create_all",
        )
        ident = _ate_senha("subj-corrida", "corrida.canal@example.com")
        token = _emitir(ident.id).token
        db.session.remove()
        barreira = threading.Barrier(2)
        resultados: dict[int, str] = {}

        def _worker(indice: int) -> None:
            with flask_app.app_context():
                barreira.wait(timeout=5)
                resultados[indice] = _concluir(token).codigo
                db.session.remove()

        threads = [threading.Thread(target=_worker, args=(indice,)) for indice in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert set(resultados) == {1, 2}
        assert set(resultados.values()) == {
            conclusao.CODIGO_CONTA_CRIADA,
            conclusao.CODIGO_JA_REALIZADA,
        }
        assert User.query.filter_by(email="corrida.canal@example.com").count() == 1
        assert Conta.query.count() == 1
        assert Franquia.query.count() == 1
        db.session.remove()
        run_test_schema_operation(
            db,
            PYTEST_DISPOSABLE_SQLALCHEMY_URI,
            testing=True,
            operation="drop_all",
        )


def test_email_existente_nao_cria_user_e_exige_confirmacao(ctx, monkeypatch):
    _patch_limite(monkeypatch)
    ident = _ate_senha("subj-existente", "existente.canal@example.com")
    token = _emitir(ident.id).token
    conta, franquia = seed_conta_franquia_cliente("conta-existente-canal")
    existente = seed_usuario(
        franquia.id,
        conta.id,
        email="existente.canal@example.com",
    )
    existente.job_role = "gerente"
    existente.usage_purpose = "trabalho"
    existente.accepted_terms_at = utcnow_naive()
    aceite = existente.accepted_terms_at
    db.session.commit()
    users_antes = User.query.count()
    senha = _concluir(token)
    assert senha.codigo == conclusao.CODIGO_CONTA_EXISTENTE
    assert User.query.count() == users_antes
    assert OnboardingCanalConclusao.query.filter_by(
        estado=OnboardingCanalConclusao.ESTADO_EMITIDO
    ).count() == 1
    sem_auth = conclusao.confirmar_vinculo_conta_existente(
        token,
        None,
        secret_key=SECRET,
    )
    assert sem_auth.codigo == conclusao.CODIGO_AUTH_NECESSARIA
    outra_conta, outra_franquia = seed_conta_franquia_cliente("conta-errada-canal")
    errado = seed_usuario(outra_franquia.id, outra_conta.id, email="errado.canal@example.com")
    divergente = conclusao.confirmar_vinculo_conta_existente(
        token,
        errado,
        secret_key=SECRET,
    )
    assert divergente.codigo == conclusao.CODIGO_VINCULO_DIVERGENTE
    identidade = db.session.get(IdentidadeCanalExterna, ident.id)
    assert identidade.user_id is None
    assert identidade.estado == IdentidadeCanalExterna.ESTADO_CADASTRO_EM_ANDAMENTO
    confirmado = conclusao.confirmar_vinculo_conta_existente(
        token,
        existente,
        secret_key=SECRET,
    )
    assert confirmado.codigo == conclusao.CODIGO_VINCULO_CONFIRMADO
    assert confirmado.user_id == existente.id
    identidade = db.session.get(IdentidadeCanalExterna, ident.id)
    assert identidade.estado == IdentidadeCanalExterna.ESTADO_VINCULADA
    assert identidade.user_id == existente.id
    assert OnboardingCanal.query.filter_by(identidade_id=ident.id).one().etapa == (
        OnboardingCanal.ETAPA_CONCLUIDO
    )
    persistido = db.session.get(User, existente.id)
    assert persistido.job_role == "gerente"
    assert persistido.usage_purpose == "trabalho"
    assert persistido.accepted_terms_at == aceite
    assert persistido.password_hash is None
    replay = conclusao.confirmar_vinculo_conta_existente(
        token,
        existente,
        secret_key=SECRET,
    )
    assert replay.codigo == conclusao.CODIGO_JA_REALIZADA
    assert User.query.filter_by(email="existente.canal@example.com").count() == 1
    recusa = _ate_senha("subj-sem-token", "sem.token.canal@example.com")
    conta_sem, franquia_sem = seed_conta_franquia_cliente("conta-sem-token")
    seed_usuario(franquia_sem.id, conta_sem.id, email="sem.token.canal@example.com")
    emissao_recusada = _emitir(recusa.id)
    assert emissao_recusada.codigo == conclusao.CODIGO_CONTA_EXISTENTE
    assert emissao_recusada.token is None


def test_jornada_parada_no_email_existente_vincula_sem_senha_nova(ctx):
    conta, franquia = seed_conta_franquia_cliente("conta-email-etapa")
    existente = seed_usuario(franquia.id, conta.id, email="parado.canal@example.com")
    ident = _ident("subj-parado")
    iniciar_onboarding_canal(ident.id)
    registrar_resposta_onboarding(ident.id, campo="aceitar_convite")
    registrar_resposta_onboarding(ident.id, campo="nome", valor="Paulo Guest")
    estado = registrar_resposta_onboarding(
        ident.id,
        campo="email",
        valor="parado.canal@example.com",
    )
    assert estado.codigo == "existing_account_verification_required"
    assert _emitir(ident.id).codigo == conclusao.CODIGO_JORNADA_NAO_OPERAVEL
    vinculo = conclusao.emitir_link_vinculo_conta_existente(ident.id, secret_key=SECRET)
    assert vinculo.codigo == conclusao.CODIGO_LINK_EMITIDO
    confirmado = conclusao.confirmar_vinculo_conta_existente(
        vinculo.token,
        existente,
        secret_key=SECRET,
    )
    assert confirmado.codigo == conclusao.CODIGO_VINCULO_CONFIRMADO
    assert db.session.get(User, existente.id).password_hash is None
    assert db.session.get(User, existente.id).job_role is None
    assert OnboardingRespostaDeclarada.query.count() == 0


def test_token_manipulado_e_de_outra_jornada_sao_rejeitados(ctx, monkeypatch):
    _patch_limite(monkeypatch)
    ident_a = _ate_senha("subj-token-a", "token.a@example.com")
    ident_b = _ate_senha("subj-token-b", "token.b@example.com")
    token_a = _emitir(ident_a.id).token
    _emitir(ident_b.id)
    assert _concluir(token_a[:-1] + ("A" if token_a[-1] != "A" else "B")).codigo == (
        conclusao.CODIGO_TOKEN_INVALIDO
    )
    row_a = OnboardingCanalConclusao.query.filter_by(
        onboarding_id=OnboardingCanal.query.filter_by(identidade_id=ident_a.id).one().id
    ).one()
    jornada_b = OnboardingCanal.query.filter_by(identidade_id=ident_b.id).one()
    forjado = URLSafeTimedSerializer(
        SECRET,
        salt=conclusao.SALT_CONCLUSAO_ONBOARDING,
    ).dumps({"c": int(row_a.id), "o": int(jornada_b.id), "email": "token.b@example.com"})
    assert _concluir(forjado).codigo == conclusao.CODIGO_TOKEN_INVALIDO
    assert User.query.count() == 0
    assert OnboardingCanal.query.filter_by(identidade_id=ident_b.id).one().etapa == (
        OnboardingCanal.ETAPA_SENHA
    )
    reset = URLSafeTimedSerializer(SECRET, salt="password-reset-salt").dumps({"user_id": 1})
    assert _concluir(reset).codigo == conclusao.CODIGO_TOKEN_INVALIDO


def test_identidade_e_jornada_invalidas_nao_concluem(ctx, monkeypatch):
    _patch_limite(monkeypatch)
    ident = _ate_senha("subj-invalida", "invalida.canal@example.com")
    token = _emitir(ident.id).token
    identidade = db.session.get(IdentidadeCanalExterna, ident.id)
    identidade.estado = IdentidadeCanalExterna.ESTADO_BLOQUEADA
    db.session.commit()
    assert _concluir(token).codigo == conclusao.CODIGO_IDENTIDADE_BLOQUEADA
    identidade.estado = IdentidadeCanalExterna.ESTADO_REVOGADA
    identidade.revogada_em = utcnow_naive()
    db.session.commit()
    assert _concluir(token).codigo == conclusao.CODIGO_IDENTIDADE_REVOGADA
    identidade.estado = IdentidadeCanalExterna.ESTADO_CADASTRO_EM_ANDAMENTO
    identidade.revogada_em = None
    jornada = OnboardingCanal.query.filter_by(identidade_id=ident.id).one()
    jornada.etapa = OnboardingCanal.ETAPA_EXPIRADO
    jornada.expirada_em = utcnow_naive()
    db.session.commit()
    assert _concluir(token).codigo == conclusao.CODIGO_JORNADA_EXPIRADA
    jornada.etapa = OnboardingCanal.ETAPA_CANCELADO
    jornada.expirada_em = None
    jornada.cancelada_em = utcnow_naive()
    db.session.commit()
    assert _concluir(token).codigo == conclusao.CODIGO_JORNADA_CANCELADA
    assert User.query.count() == 0


def test_jornada_vencida_nao_cria_user(ctx, monkeypatch):
    _patch_limite(monkeypatch)
    ident = _ate_senha("subj-jornada-vencida", "vencida.canal@example.com")
    token = _emitir(ident.id).token
    jornada = OnboardingCanal.query.one()
    jornada.expira_em = utcnow_naive() - timedelta(minutes=1)
    db.session.commit()
    assert _concluir(token).codigo == conclusao.CODIGO_JORNADA_EXPIRADA
    assert User.query.count() == 0
    assert OnboardingCanal.query.one().etapa == OnboardingCanal.ETAPA_EXPIRADO


def test_desidentificar_usuario_invalida_o_token(ctx, monkeypatch):
    _patch_limite(monkeypatch)
    ident = _ate_senha("subj-privacidade", "privacidade.canal@example.com")
    token = _emitir(ident.id).token
    criado = _concluir(token)
    user = db.session.get(User, criado.user_id)
    encerrar_vinculo_operacional_usuario(user)
    assert _concluir(token).codigo in {
        conclusao.CODIGO_TOKEN_INVALIDO,
        conclusao.CODIGO_TOKEN_REVOGADO,
        conclusao.CODIGO_JA_REALIZADA,
        conclusao.CODIGO_LINK_JA_UTILIZADO,
    }
    row = OnboardingCanalConclusao.query.one()
    assert row.token_hash != conclusao._hash_token(token)
    assert "privacidade.canal@example.com" not in row.token_hash


def test_retorno_ao_canal_nao_abre_redirect(monkeypatch):
    monkeypatch.setenv("ONBOARDING_CANAL_RETORNO_URL", "https://evil.example/wa")
    assert url_retorno_canal_permitida() is None
    monkeypatch.setenv("ONBOARDING_CANAL_RETORNO_URL", "http://wa.me/5500000000000")
    assert url_retorno_canal_permitida() is None


def _preparar_cliente(app):
    app.config["SECRET_KEY"] = SECRET
    app.template_folder = str(ROOT / "app" / "templates")
    app.jinja_loader.searchpath = [app.template_folder]
    if "onboarding_canal_concluir" not in app.view_functions:
        register_onboarding_canal_routes(app)
    if "login" not in app.view_functions:
        app.add_url_rule("/login", endpoint="login", view_func=lambda: "login")
    if "logout" not in app.view_functions:
        app.add_url_rule("/logout", endpoint="logout", view_func=lambda: "logout")
    if "privacy_policy" not in app.view_functions:
        app.add_url_rule(
            "/privacy-policy",
            endpoint="privacy_policy",
            view_func=lambda: "privacy",
        )
    if "growth.growth_cta_clicked" not in app.view_functions:
        growth = Blueprint("growth", __name__)

        @growth.route("/growth/cta")
        def growth_cta_clicked():
            return ""

        @growth.route("/growth/page")
        def growth_page_view():
            return ""

        app.register_blueprint(growth)
    if "user.perfil" not in app.view_functions:
        usuario = Blueprint("user", __name__)

        @usuario.route("/perfil")
        def perfil():
            return ""

        app.register_blueprint(usuario)
    login_manager.init_app(app)

    @login_manager.user_loader
    def _load_user(user_id):
        return db.session.get(User, int(user_id))

    @app.context_processor
    def _contexto():
        from flask import request, url_for

        def _has(endpoint_name: str) -> bool:
            return endpoint_name in app.view_functions

        return {
            "has_endpoint": _has,
            "user_is_admin": lambda _user: False,
            "notificacoes_nao_lidas": 0,
            "shell_nav": build_shell_navigation(
                authenticated=False,
                request_path=request.path,
                url_for=url_for,
                has_endpoint=_has,
            ),
        }

    return app.test_client()


def test_pagina_de_senha_conclui_e_autentica(ctx, app, monkeypatch):
    _patch_limite(monkeypatch)
    ident = _ate_senha("subj-pagina", "pagina.canal@example.com", nome="Marina Canal")
    token = _emitir(ident.id).token
    client = _preparar_cliente(app)
    pagina = client.get(f"/onboarding/canal/concluir/{token}")
    html = pagina.get_data(as_text=True)
    assert pagina.status_code == 200
    assert 'type="password"' in html
    assert "pagina.canal@example.com" not in html
    assert "p***@example.com" in html
    assert "M***" in html
    assert "terms-v1" not in html
    resposta = client.post(
        f"/onboarding/canal/concluir/{token}",
        data={
            "acao": "definir_senha",
            "password": SENHA,
            "confirm_password": SENHA,
        },
    )
    sucesso = resposta.get_data(as_text=True)
    assert "Voltar para o WhatsApp" in sucesso
    assert "Cadastro concluído" in sucesso
    assert SENHA not in sucesso
    with client.session_transaction() as sess:
        assert sess.get("_user_id") == str(User.query.one().id)
    de_novo = client.get(f"/onboarding/canal/concluir/{token}")
    assert "já foi utilizado" in de_novo.get_data(as_text=True).lower() or (
        "já foi concluído" in de_novo.get_data(as_text=True).lower()
    )
    retorno = client.get("/onboarding/canal/retorno?next=https://evil.example")
    corpo = retorno.get_data(as_text=True)
    assert retorno.status_code == 200
    assert "https://evil.example" not in corpo
    assert "Voltar para o WhatsApp" in corpo


def _sessao_contem_texto(sess, texto: str) -> bool:
    def _walk(valor) -> bool:
        if isinstance(valor, str):
            return texto in valor
        if isinstance(valor, dict):
            return any(_walk(chave) or _walk(item) for chave, item in valor.items())
        if isinstance(valor, (list, tuple)):
            return any(_walk(item) for item in valor)
        return False

    return any(_walk(chave) or _walk(valor) for chave, valor in dict(sess).items())


def _cliente_login_real(app, monkeypatch):
    import os

    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    import app.web as web_mod

    original = web_mod.render_template

    def _render(name, **kwargs):
        if name == "login.html":
            return "login"
        return original(name, **kwargs)

    monkeypatch.setattr(web_mod, "render_template", _render)
    if "login" not in app.view_functions:
        app.add_url_rule(
            "/login",
            endpoint="login",
            view_func=web_mod.login,
            methods=["GET", "POST"],
        )
    return _preparar_cliente(app)


def _usuario_com_senha(email: str, slug: str):
    conta, franquia = seed_conta_franquia_cliente(slug)
    user = seed_usuario(franquia.id, conta.id, email=email)
    user.set_password(SENHA)
    db.session.commit()
    return user


def _emitir_vinculo(sujeito: str, email: str):
    ident = _ident(sujeito)
    iniciar_onboarding_canal(ident.id)
    registrar_resposta_onboarding(ident.id, campo="aceitar_convite")
    registrar_resposta_onboarding(ident.id, campo="nome", valor="Marina Canal")
    registrar_resposta_onboarding(ident.id, campo="email", valor=email)
    emissao = conclusao.emitir_link_vinculo_conta_existente(
        ident.id,
        secret_key=SECRET,
        build_url=_url,
    )
    assert emissao.codigo == conclusao.CODIGO_LINK_EMITIDO
    assert emissao.token
    return ident, emissao.token


def _abrir_login_de_conclusao(client, token: str):
    abertura = client.get(f"/onboarding/canal/concluir/{token}", follow_redirects=False)
    assert abertura.status_code in (302, 303)
    destino = abertura.headers.get("Location") or ""
    assert "/login" in destino
    assert "onboarding/canal/continuar" in destino
    assert token not in destino
    pagina = client.get(destino, follow_redirects=False)
    assert pagina.status_code == 200
    assert token not in pagina.get_data(as_text=True)
    with client.session_transaction() as sess:
        assert not _sessao_contem_texto(sess, token)
        assert token not in (sess.get("post_login_next") or "")
        assert sess.get("post_login_next") == "/onboarding/canal/continuar"
        assert isinstance(sess.get(SESSION_HANDOFF_ID), int)
        assert str(sess.get(SESSION_HANDOFF_ID)) != token
        assert not _sessao_contem_texto({"_flashes": sess.get("_flashes")}, token)
    return destino


def test_login_conta_existente_retorna_sem_persistir_o_token(ctx, app, monkeypatch):
    email = "handoff.canal@example.com"
    _usuario_com_senha(email, "conta-handoff-canal")
    ident, token = _emitir_vinculo("subj-handoff", email)
    row = OnboardingCanalConclusao.query.one()
    hash_antes = row.token_hash
    client = _cliente_login_real(app, monkeypatch)

    isolado = app.test_client()
    cru = isolado.get(f"/login?next=/onboarding/canal/concluir/{token}")
    assert token not in (cru.headers.get("Location") or "")
    assert token not in cru.get_data(as_text=True)
    with isolado.session_transaction() as sess:
        assert not _sessao_contem_texto(sess, token)
        assert "post_login_next" not in sess
        assert SESSION_HANDOFF_ID not in sess

    _abrir_login_de_conclusao(client, token)
    recusado = client.post(
        "/login",
        data={"email": email, "password": "senha-errada"},
        follow_redirects=False,
    )
    assert recusado.status_code == 200
    with client.session_transaction() as sess:
        assert not _sessao_contem_texto(sess, token)
        assert sess.get("_flashes")
        assert sess.get("post_login_next") == "/onboarding/canal/continuar"
        assert sess.get(SESSION_HANDOFF_ID) == row.id

    entrada = client.post(
        "/login",
        data={"email": email, "password": SENHA},
        follow_redirects=False,
    )
    assert entrada.status_code in (302, 303)
    volta = entrada.headers.get("Location") or ""
    assert "/onboarding/canal/continuar" in volta
    assert token not in volta
    pagina = client.get(volta, follow_redirects=False)
    html = pagina.get_data(as_text=True)
    assert pagina.status_code == 200
    assert "Conectar esta conta ao canal" in html
    assert token not in html
    assert email not in html
    confirmado = client.post(
        "/onboarding/canal/continuar",
        data={"acao": "vincular"},
        follow_redirects=False,
    )
    corpo = confirmado.get_data(as_text=True)
    assert "Conta conectada ao canal" in corpo
    assert token not in corpo
    identidade = db.session.get(IdentidadeCanalExterna, ident.id)
    assert identidade.estado == IdentidadeCanalExterna.ESTADO_VINCULADA
    assert identidade.user_id == User.query.filter_by(email=email).one().id
    persistida = OnboardingCanalConclusao.query.one()
    assert persistida.estado == OnboardingCanalConclusao.ESTADO_CONSUMIDO
    assert persistida.token_hash == hash_antes
    with client.session_transaction() as sess:
        assert not _sessao_contem_texto(sess, token)
        assert SESSION_HANDOFF_ID not in sess
        assert sess.get("post_login_next") in (None, "")
        assert sess.get("_user_id") == str(identidade.user_id)
    retorno = client.get("/onboarding/canal/retorno")
    assert retorno.status_code == 200
    with client.session_transaction() as sess:
        assert sess.get("_user_id") == str(identidade.user_id)
        assert not _sessao_contem_texto(sess, token)


def test_login_conta_errada_rejeita_vinculo_sem_persistir_o_token(ctx, app, monkeypatch):
    email = "dono.handoff@example.com"
    _usuario_com_senha(email, "conta-dono-handoff")
    errado = _usuario_com_senha("errado.handoff@example.com", "conta-errado-handoff")
    ident, token = _emitir_vinculo("subj-handoff-errado", email)
    hash_antes = OnboardingCanalConclusao.query.one().token_hash
    client = _cliente_login_real(app, monkeypatch)
    _abrir_login_de_conclusao(client, token)
    entrada = client.post(
        "/login",
        data={"email": errado.email, "password": SENHA},
        follow_redirects=False,
    )
    assert entrada.status_code in (302, 303)
    assert token not in (entrada.headers.get("Location") or "")
    pagina = client.get(entrada.headers["Location"], follow_redirects=False)
    assert "Conectar esta conta ao canal" in pagina.get_data(as_text=True)
    recusa = client.post(
        "/onboarding/canal/continuar",
        data={"acao": "vincular"},
        follow_redirects=False,
    )
    html = recusa.get_data(as_text=True)
    assert "não corresponde ao e-mail" in html
    assert token not in html
    identidade = db.session.get(IdentidadeCanalExterna, ident.id)
    assert identidade.user_id is None
    assert identidade.estado == IdentidadeCanalExterna.ESTADO_CADASTRO_EM_ANDAMENTO
    persistida = OnboardingCanalConclusao.query.one()
    assert persistida.estado == OnboardingCanalConclusao.ESTADO_EMITIDO
    assert persistida.token_hash == hash_antes
    with client.session_transaction() as sess:
        assert not _sessao_contem_texto(sess, token)
        assert sess.get("_user_id") == str(errado.id)
        assert sess.get(SESSION_HANDOFF_ID) == persistida.id
        assert token not in (sess.get("post_login_next") or "")
    retorno = client.get("/onboarding/canal/retorno")
    assert retorno.status_code == 200
    with client.session_transaction() as sess:
        assert sess.get("_user_id") == str(errado.id)
