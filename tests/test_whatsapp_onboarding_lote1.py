"""Lote WhatsApp 1: identidade guest, quota e onboarding de canal. Sem Meta."""
from __future__ import annotations

import importlib.util
import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    Conta,
    Franquia,
    FunnelEvent,
    IdentidadeCanalExterna,
    InteracaoGuestCanal,
    OnboardingCanal,
    OnboardingRespostaDeclarada,
    User,
    utcnow_naive,
)
from app.services import canal_aquisicao_service as canal
from app.services.canal_aquisicao_config import (
    limite_interacoes_guest,
    validade_onboarding_canal,
)
from app.services.canal_aquisicao_service import (
    CODIGO_CONTA_EXISTENTE,
    CODIGO_LIMITE,
    CanalAquisicaoError,
    MaterialSensivelRecusadoError,
    iniciar_onboarding_canal,
    obter_estado_guest,
    obter_ou_criar_identidade_externa,
    obter_proxima_etapa,
    pausar_para_interacao_guest,
    registrar_evento_guest_sem_consumo,
    registrar_interacao_guest_concluida,
    registrar_resposta_onboarding,
    reiniciar_onboarding_canal,
    retomar_onboarding,
)
from app.services.onboarding_entrevista_definicao import TAXONOMIA_VERSAO
from tests.conftest import seed_conta_franquia_cliente, seed_usuario

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "versions" / "m4n5o6p7q8r9_canal_aquisicao_identidade_guest.py"
PROVEDOR = "canal_sintetico"
SENHA_PROIBIDA = "SegredoDaSenhaGuest9"


def _migration_module():
    spec = importlib.util.spec_from_file_location("mig_canal_aquisicao", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ident(sujeito: str, **kwargs):
    return obter_ou_criar_identidade_externa(
        provedor=PROVEDOR,
        sujeito_externo=sujeito,
        **kwargs,
    )


def _consumir(identidade_id: int, quantidade: int, prefixo: str = "msg"):
    ultimo = None
    for indice in range(quantidade):
        ultimo = registrar_interacao_guest_concluida(
            identidade_id,
            f"{prefixo}-{indice}",
        )
    return ultimo


def _abrir(sujeito: str):
    ident = _ident(sujeito)
    estado = iniciar_onboarding_canal(ident.id)
    return ident, estado


def _aceitar_nome_email(identidade_id: int, email: str, nome: str = "Maria Guest"):
    registrar_resposta_onboarding(identidade_id, campo="aceitar_convite")
    registrar_resposta_onboarding(identidade_id, campo="nome", valor=nome)
    return registrar_resposta_onboarding(identidade_id, campo="email", valor=email)


def _texto_persistido() -> str:
    pedacos = []
    for modelo in (IdentidadeCanalExterna, InteracaoGuestCanal, OnboardingCanal):
        for row in modelo.query.all():
            pedacos.append(
                " ".join(str(getattr(row, coluna.name)) for coluna in modelo.__table__.columns)
            )
    return "\n".join(pedacos)


def test_configuracao_provisoria_centraliza_limite_e_validade():
    assert limite_interacoes_guest() == 5
    assert validade_onboarding_canal() == timedelta(hours=72)
    config = (ROOT / "app" / "services" / "canal_aquisicao_config.py").read_text(encoding="utf-8")
    assert "Provisório" in config
    servico = Path(canal.__file__).read_text(encoding="utf-8")
    assert "register_user" not in servico
    assert "PluginConexao" not in servico
    assert "flask.session" not in servico
    assert "/webhook" not in servico
    assert ">= 5" not in servico
    assert "hours=72" not in servico


def test_migration_fecha_predicados_do_modelo():
    modulo = _migration_module()
    assert modulo.revision == "m4n5o6p7q8r9"
    assert modulo.down_revision == "l3m4n5o6p7q8"
    assert modulo._SQL_ESTADO == IdentidadeCanalExterna._SQL_ESTADO
    assert modulo._SQL_COERENCIA == IdentidadeCanalExterna._SQL_COERENCIA
    assert modulo._SQL_IDENTIDADE_ATIVA == IdentidadeCanalExterna._SQL_IDENTIDADE_ATIVA
    assert modulo._SQL_RESULTADO == InteracaoGuestCanal._SQL_RESULTADO
    assert modulo._SQL_ETAPA == OnboardingCanal._SQL_ETAPA
    assert modulo._SQL_JORNADA_ABERTA == OnboardingCanal._SQL_JORNADA_ABERTA
    assert modulo._SQL_TERMINAIS == OnboardingCanal._SQL_TERMINAIS
    assert modulo._SQL_TERMOS == OnboardingCanal._SQL_TERMOS
    assert modulo._SQL_EMAIL == OnboardingCanal._SQL_EMAIL
    assert modulo._SQL_TEXTOS == OnboardingCanal._SQL_TEXTOS
    texto = MIGRATION.read_text(encoding="utf-8")
    assert "op.add_column" not in texto
    assert "op.alter_column" not in texto
    assert 'sa.Column("senha"' not in texto
    assert 'sa.Column("token"' not in texto
    assert 'sa.Column("telefone"' not in texto


def test_identidade_guest_criada_sem_user(ctx):
    assert set(IdentidadeCanalExterna.ESTADOS) == {
        "guest",
        "cadastro_em_andamento",
        "vinculada",
        "revogada",
        "bloqueada",
    }
    ident = _ident("subj-guest-1")
    assert ident.id is not None
    assert ident.user_id is None
    assert ident.estado == IdentidadeCanalExterna.ESTADO_GUEST
    assert ident.vinculada_em is None
    assert ident.revogada_em is None
    assert User.query.count() == 0


def test_mesma_identidade_externa_resolve_a_mesma_linha(ctx):
    primeira = _ident("subj-guest-1", contexto_destino="dest-opaco-1")
    segunda = _ident("subj-guest-1", contexto_destino="dest-opaco-2")
    assert segunda.id == primeira.id
    assert IdentidadeCanalExterna.query.count() == 1
    assert segunda.contexto_destino == "dest-opaco-1"
    assert segunda.user_id is None


def test_identidade_ativa_nao_vincula_dois_users(ctx):
    conta_a, franquia_a = seed_conta_franquia_cliente("conta-canal-a")
    user_a = seed_usuario(franquia_a.id, conta_a.id, email="um.canal@example.com")
    conta_b, franquia_b = seed_conta_franquia_cliente("conta-canal-b")
    user_b = seed_usuario(franquia_b.id, conta_b.id, email="dois.canal@example.com")
    agora = utcnow_naive()
    primeira = IdentidadeCanalExterna(
        provedor=PROVEDOR,
        sujeito_externo="subj-vinculo",
        estado=IdentidadeCanalExterna.ESTADO_VINCULADA,
        user_id=user_a.id,
        interacoes_uteis=0,
        criada_em=agora,
        atualizada_em=agora,
        vinculada_em=agora,
    )
    db.session.add(primeira)
    db.session.flush()

    with pytest.raises(IntegrityError):
        with db.session.begin_nested():
            db.session.add(
                IdentidadeCanalExterna(
                    provedor=PROVEDOR,
                    sujeito_externo="subj-vinculo",
                    estado=IdentidadeCanalExterna.ESTADO_VINCULADA,
                    user_id=user_b.id,
                    interacoes_uteis=0,
                    criada_em=agora,
                    atualizada_em=agora,
                    vinculada_em=agora,
                )
            )
            db.session.flush()

    with pytest.raises(IntegrityError):
        with db.session.begin_nested():
            db.session.add(
                IdentidadeCanalExterna(
                    provedor=PROVEDOR,
                    sujeito_externo="subj-guest-com-user",
                    estado=IdentidadeCanalExterna.ESTADO_GUEST,
                    user_id=user_a.id,
                    interacoes_uteis=0,
                    criada_em=agora,
                    atualizada_em=agora,
                )
            )
            db.session.flush()

    assert db.session.is_active
    assert IdentidadeCanalExterna.query.filter_by(sujeito_externo="subj-vinculo").count() == 1
    assert db.session.get(IdentidadeCanalExterna, primeira.id).user_id == user_a.id
    outra = _ident("subj-outra-pessoa")
    assert outra.user_id is None
    assert outra.estado == IdentidadeCanalExterna.ESTADO_GUEST


def test_contador_comeca_em_zero(ctx):
    ident = _ident("subj-quota-zero")
    estado = obter_estado_guest(ident.id)
    assert estado.usadas == 0
    assert estado.restantes == 5
    assert estado.limite == 5
    assert estado.bloqueado_para_nova_interacao is False
    assert estado.codigo == "disponivel"
    assert InteracaoGuestCanal.query.count() == 0


def test_interacao_util_incrementa_e_permanece_no_banco(ctx):
    ident = _ident("subj-quota-uma")
    resultado = registrar_interacao_guest_concluida(ident.id, "msg-test-123")
    assert resultado.codigo == "interacao_registrada"
    assert resultado.consumiu is True
    assert resultado.usadas == 1
    assert resultado.restantes == 4
    assert resultado.bloqueado_para_nova_interacao is False
    db.session.commit()
    db.session.expire_all()
    estado = obter_estado_guest(ident.id)
    assert estado.usadas == 1
    assert (
        InteracaoGuestCanal.query.filter_by(
            identidade_id=ident.id,
            resultado=InteracaoGuestCanal.RESULTADO_SUCESSO,
        ).count()
        == 1
    )


def test_replay_do_mesmo_evento_nao_incrementa(ctx):
    ident = _ident("subj-quota-replay")
    registrar_interacao_guest_concluida(ident.id, "msg-test-123")
    replay = registrar_interacao_guest_concluida(ident.id, "msg-test-123")
    assert replay.codigo == "interacao_replay"
    assert replay.consumiu is False
    assert replay.usadas == 1
    assert InteracaoGuestCanal.query.filter_by(identidade_id=ident.id).count() == 1


def test_quinta_interacao_e_permitida(ctx):
    ident = _ident("subj-quota-cinco")
    quinta = _consumir(ident.id, 5)
    assert quinta.codigo == "interacao_registrada"
    assert quinta.consumiu is True
    assert quinta.usadas == 5
    assert quinta.restantes == 0
    assert quinta.limite == 5
    assert quinta.bloqueado_para_nova_interacao is True
    assert (
        InteracaoGuestCanal.query.filter_by(
            identidade_id=ident.id,
            resultado=InteracaoGuestCanal.RESULTADO_SUCESSO,
        ).count()
        == 5
    )


def test_sexta_tentativa_retorna_limite_sem_registrar(ctx):
    ident = _ident("subj-quota-seis")
    _consumir(ident.id, 5)
    antes = InteracaoGuestCanal.query.filter_by(identidade_id=ident.id).count()
    sexta = registrar_interacao_guest_concluida(ident.id, "msg-6")
    assert sexta.codigo == CODIGO_LIMITE
    assert sexta.consumiu is False
    assert sexta.usadas == 5
    assert sexta.restantes == 0
    assert sexta.bloqueado_para_nova_interacao is True
    assert InteracaoGuestCanal.query.filter_by(identidade_id=ident.id).count() == antes
    replay = registrar_interacao_guest_concluida(ident.id, "msg-4")
    assert replay.codigo == "interacao_replay"
    assert replay.usadas == 5


@pytest.mark.parametrize(
    "motivo",
    ["falha_tecnica", "solicitacao_rejeitada", "mensagem_duplicada", "mensagem_cadastro"],
)
def test_evento_tecnico_ou_falha_nao_consome(ctx, motivo):
    ident = _ident(f"subj-{motivo}")
    resultado = registrar_evento_guest_sem_consumo(
        ident.id,
        f"evt-{motivo}",
        motivo=motivo,
    )
    assert resultado.codigo == "evento_nao_consumido"
    assert resultado.consumiu is False
    assert resultado.usadas == 0
    assert obter_estado_guest(ident.id).usadas == 0
    repetido = registrar_interacao_guest_concluida(ident.id, f"evt-{motivo}")
    assert repetido.codigo == "evento_replay"
    assert repetido.usadas == 0


def test_iniciar_onboarding_cria_estado_duravel(ctx):
    ident = _ident("subj-onboarding")
    estado = iniciar_onboarding_canal(
        ident.id,
        origem_aquisicao="campanha_qr",
        correlation_id="corr-lote-1",
    )
    assert estado.codigo == "onboarding_iniciado"
    assert estado.etapa_atual == OnboardingCanal.ETAPA_CONVITE
    assert estado.expira_em > utcnow_naive()
    novamente = iniciar_onboarding_canal(ident.id)
    assert novamente.onboarding_id == estado.onboarding_id
    assert novamente.codigo == "onboarding_em_andamento"
    assert OnboardingCanal.query.count() == 1
    jornada = OnboardingCanal.query.one()
    assert jornada.origem_aquisicao == "campanha_qr"
    assert jornada.correlation_id == "corr-lote-1"
    assert jornada.taxonomia_versao == TAXONOMIA_VERSAO
    recarregada = db.session.get(IdentidadeCanalExterna, ident.id)
    assert recarregada.estado == IdentidadeCanalExterna.ESTADO_CADASTRO_EM_ANDAMENTO
    assert recarregada.user_id is None
    assert User.query.count() == 0


def test_validade_da_jornada_vem_da_configuracao(ctx, monkeypatch):
    monkeypatch.setattr(canal, "validade_onboarding_canal", lambda: timedelta(hours=5))
    ident, _estado = _abrir("subj-validade")
    jornada = OnboardingCanal.query.filter_by(identidade_id=ident.id).one()
    assert jornada.expira_em - jornada.iniciada_em == timedelta(hours=5)


def test_nome_email_e_cargo_persistidos(ctx):
    ident, _estado = _abrir("subj-dados")
    registrar_resposta_onboarding(ident.id, campo="aceitar_convite")
    nome = registrar_resposta_onboarding(ident.id, campo="nome", valor="  Maria   Guest  ")
    assert nome.etapa_atual == OnboardingCanal.ETAPA_EMAIL
    assert nome.nome == "Maria Guest"
    email = registrar_resposta_onboarding(
        ident.id,
        campo="email",
        valor="  Maria.Guest@Example.COM ",
    )
    assert email.codigo == "resposta_registrada"
    assert email.email_normalizado == "maria.guest@example.com"
    assert email.etapa_atual == OnboardingCanal.ETAPA_CARGO
    cargo = registrar_resposta_onboarding(ident.id, campo="job_role", valor="analista")
    assert cargo.job_role == "analista"
    assert cargo.etapa_atual == OnboardingCanal.ETAPA_TERMOS
    assert cargo.pergunta_key is None
    jornada = OnboardingCanal.query.filter_by(identidade_id=ident.id).one()
    assert jornada.nome == "Maria Guest"
    assert jornada.email_normalizado == "maria.guest@example.com"
    assert jornada.job_role == "analista"
    assert OnboardingRespostaDeclarada.query.count() == 0
    assert obter_estado_guest(ident.id).usadas == 0


def test_motorista_abre_entrevista_e_entregador_exige_veiculo(ctx):
    ident, _estado = _abrir("subj-entregador")
    _aceitar_nome_email(ident.id, "entregador.guest@example.com")
    cargo = registrar_resposta_onboarding(
        ident.id,
        campo="job_role",
        valor="motorista_entregador",
    )
    assert cargo.etapa_atual == OnboardingCanal.ETAPA_ENTREVISTA
    assert cargo.pergunta_key == "tipo_atuacao"
    parcial = registrar_resposta_onboarding(
        ident.id,
        campo="entrevista",
        question_key="tipo_atuacao",
        valor="entregador_app",
    )
    assert parcial.etapa_atual == OnboardingCanal.ETAPA_ENTREVISTA
    assert parcial.pergunta_key == "veiculo_principal"
    assert parcial.respostas_entrevista == {"tipo_atuacao": "entregador_app"}
    assert OnboardingRespostaDeclarada.query.count() == 0
    completa = registrar_resposta_onboarding(
        ident.id,
        campo="entrevista",
        question_key="veiculo_principal",
        valor="moto",
    )
    assert completa.etapa_atual == OnboardingCanal.ETAPA_TERMOS
    assert completa.respostas_entrevista == {
        "tipo_atuacao": "entregador_app",
        "veiculo_principal": "moto",
    }
    jornada = OnboardingCanal.query.filter_by(identidade_id=ident.id).one()
    assert "Moto" not in (jornada.respostas_entrevista_json or "")
    assert OnboardingRespostaDeclarada.query.count() == 0
    assert User.query.count() == 0


def test_tac_nao_exige_veiculo(ctx):
    ident, _estado = _abrir("subj-tac")
    _aceitar_nome_email(ident.id, "tac.guest@example.com")
    registrar_resposta_onboarding(ident.id, campo="job_role", valor="motorista_entregador")
    estado = registrar_resposta_onboarding(
        ident.id,
        campo="entrevista",
        question_key="tipo_atuacao",
        valor="tac",
    )
    assert estado.etapa_atual == OnboardingCanal.ETAPA_TERMOS
    assert estado.pergunta_key is None
    assert estado.respostas_entrevista == {"tipo_atuacao": "tac"}
    assert "veiculo_principal" not in estado.respostas_entrevista
    assert OnboardingRespostaDeclarada.query.count() == 0


def test_pergunta_guest_nao_avanca_etapa(ctx):
    ident, estado = _abrir("subj-pausa")
    etapa = estado.etapa_atual
    pausa = pausar_para_interacao_guest(ident.id)
    assert pausa.codigo == "pausada_para_interacao_guest"
    assert pausa.etapa_atual == etapa == OnboardingCanal.ETAPA_CONVITE
    assert pausa.pausas_interacao_guest == 1
    assert obter_estado_guest(ident.id).usadas == 0
    util = registrar_interacao_guest_concluida(ident.id, "msg-durante-convite")
    assert util.usadas == 1
    proxima = obter_proxima_etapa(ident.id)
    assert proxima.etapa_atual == OnboardingCanal.ETAPA_CONVITE
    assert proxima.pausas_interacao_guest == 1


def test_retomada_preserva_etapa(ctx):
    ident, _estado = _abrir("subj-retomada")
    registrar_resposta_onboarding(ident.id, campo="aceitar_convite")
    registrar_resposta_onboarding(ident.id, campo="nome", valor="Joana Guest")
    pausar_para_interacao_guest(ident.id)
    retomada = retomar_onboarding(ident.id)
    assert retomada.codigo == "jornada_retomada"
    assert retomada.etapa_atual == OnboardingCanal.ETAPA_EMAIL
    assert retomada.nome == "Joana Guest"
    assert retomada.pausas_interacao_guest == 1
    assert OnboardingCanal.query.filter_by(identidade_id=ident.id).count() == 1


def test_expiracao_nao_apaga_identidade_e_reinicio_e_controlado(ctx):
    ident, _estado = _abrir("subj-expira")
    registrar_resposta_onboarding(ident.id, campo="aceitar_convite")
    registrar_interacao_guest_concluida(ident.id, "msg-antes-expirar")
    jornada = OnboardingCanal.query.filter_by(identidade_id=ident.id).one()
    jornada.expira_em = utcnow_naive() - timedelta(minutes=1)
    db.session.commit()
    with pytest.raises(CanalAquisicaoError) as nao_confirmado:
        reiniciar_onboarding_canal(ident.id, confirmar=False)
    assert nao_confirmado.value.codigo == "reinicio_nao_confirmado"
    assert OnboardingCanal.query.count() == 1
    expirada = retomar_onboarding(ident.id)
    assert expirada.codigo == "jornada_expirada"
    assert expirada.etapa_atual == OnboardingCanal.ETAPA_EXPIRADO
    assert IdentidadeCanalExterna.query.count() == 1
    preservada = db.session.get(IdentidadeCanalExterna, ident.id)
    assert preservada.estado == IdentidadeCanalExterna.ESTADO_CADASTRO_EM_ANDAMENTO
    assert preservada.user_id is None
    assert obter_estado_guest(ident.id).usadas == 1
    nova = reiniciar_onboarding_canal(ident.id, confirmar=True)
    assert nova.codigo == "onboarding_reiniciado"
    assert nova.etapa_atual == OnboardingCanal.ETAPA_CONVITE
    assert nova.onboarding_id != expirada.onboarding_id
    assert OnboardingCanal.query.count() == 2
    assert OnboardingCanal.query.filter_by(etapa=OnboardingCanal.ETAPA_EXPIRADO).count() == 1
    assert db.session.get(IdentidadeCanalExterna, ident.id).id == ident.id
    assert obter_estado_guest(ident.id).usadas == 1


def test_termos_apresentados_e_aceitos_sao_registrados_separadamente(ctx):
    ident, _estado = _abrir("subj-termos")
    _aceitar_nome_email(ident.id, "termos.guest@example.com")
    registrar_resposta_onboarding(ident.id, campo="job_role", valor="gerente")
    with pytest.raises(CanalAquisicaoError) as sim:
        registrar_resposta_onboarding(
            ident.id,
            campo="declarar_aceite_termos",
            valor="sim",
            aceite_declarado=False,
        )
    assert sim.value.codigo == "aceite_exige_declaracao_estruturada"
    jornada = OnboardingCanal.query.filter_by(identidade_id=ident.id).one()
    assert jornada.termos_apresentados_em is None
    assert jornada.termos_aceitos_em is None
    assert jornada.etapa == OnboardingCanal.ETAPA_TERMOS
    apresentados = registrar_resposta_onboarding(
        ident.id,
        campo="apresentar_termos",
        termos_referencia="terms-v1",
    )
    assert apresentados.codigo == "termos_apresentados"
    assert apresentados.termos_apresentados is True
    assert apresentados.termos_aceitos is False
    assert apresentados.termos_referencia == "terms-v1"
    assert apresentados.etapa_atual == OnboardingCanal.ETAPA_TERMOS
    aceitos = registrar_resposta_onboarding(
        ident.id,
        campo="declarar_aceite_termos",
        termos_referencia="terms-v1",
        aceite_declarado=True,
    )
    assert aceitos.codigo == "termos_aceitos"
    assert aceitos.termos_aceitos is True
    assert aceitos.etapa_atual == OnboardingCanal.ETAPA_SENHA
    gravada = OnboardingCanal.query.filter_by(identidade_id=ident.id).one()
    assert gravada.termos_apresentados_em is not None
    assert gravada.termos_aceitos_em is not None
    assert gravada.termos_aceitos_em >= gravada.termos_apresentados_em
    assert User.query.count() == 0


def test_email_existente_pede_verificacao_sem_criar_user(ctx):
    conta, franquia = seed_conta_franquia_cliente("conta-email-existente")
    existente = seed_usuario(franquia.id, conta.id, email="ana@example.com")
    antes = (
        User.query.count(),
        Conta.query.count(),
        Franquia.query.count(),
        OnboardingRespostaDeclarada.query.count(),
        FunnelEvent.query.count(),
    )
    ident, _estado = _abrir("subj-conta-existente")
    resultado = _aceitar_nome_email(ident.id, "  Ana@Example.com ")
    assert resultado.codigo == CODIGO_CONTA_EXISTENTE
    assert resultado.etapa_atual == OnboardingCanal.ETAPA_EMAIL
    assert resultado.email_normalizado == "ana@example.com"
    proxima = obter_proxima_etapa(ident.id)
    assert proxima.codigo == CODIGO_CONTA_EXISTENTE
    assert proxima.etapa_atual == OnboardingCanal.ETAPA_EMAIL
    assert (
        User.query.count(),
        Conta.query.count(),
        Franquia.query.count(),
        OnboardingRespostaDeclarada.query.count(),
        FunnelEvent.query.count(),
    ) == antes
    assert db.session.get(User, existente.id).accepted_terms_at is None
    assert db.session.get(User, existente.id).job_role is None
    assert OnboardingCanal.query.filter_by(identidade_id=ident.id).count() == 1


def test_senha_nunca_e_persistida_e_nenhuma_conta_e_criada(ctx):
    antes = (
        User.query.count(),
        Conta.query.count(),
        Franquia.query.count(),
        OnboardingRespostaDeclarada.query.count(),
    )
    ident, _estado = _abrir("subj-sem-user")
    _aceitar_nome_email(ident.id, "nova.pessoa@example.com", nome="Nova Pessoa")
    registrar_resposta_onboarding(ident.id, campo="job_role", valor="coordenador")
    registrar_resposta_onboarding(ident.id, campo="apresentar_termos", termos_referencia="terms-v1")
    estado = registrar_resposta_onboarding(
        ident.id,
        campo="declarar_aceite_termos",
        aceite_declarado=True,
    )
    assert estado.etapa_atual == OnboardingCanal.ETAPA_SENHA
    with pytest.raises(CanalAquisicaoError) as exc:
        registrar_resposta_onboarding(ident.id, campo="senha", valor=SENHA_PROIBIDA)
    assert exc.value.codigo == "senha_nao_aceitada_neste_lote"
    assert SENHA_PROIBIDA not in str(exc.value)
    assert (
        User.query.count(),
        Conta.query.count(),
        Franquia.query.count(),
        OnboardingRespostaDeclarada.query.count(),
    ) == antes
    assert OnboardingCanal.query.filter_by(identidade_id=ident.id).one().etapa == (
        OnboardingCanal.ETAPA_SENHA
    )
    assert SENHA_PROIBIDA not in _texto_persistido()
    colunas_proibidas = {"senha", "password", "token", "payload", "telefone", "phone", "webhook"}
    for modelo in (IdentidadeCanalExterna, InteracaoGuestCanal, OnboardingCanal):
        assert colunas_proibidas.isdisjoint(modelo.__table__.columns.keys())


def test_colisao_e_idempotencia_mantem_sessao_utilizavel(ctx, monkeypatch):
    original = _ident("subj-colisao")
    marcador = _ident("subj-marcador")
    monkeypatch.setattr(canal, "_buscar_identidade_ativa", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        canal,
        "_buscar_identidade_ativa_na_reserva",
        lambda *_args, **_kwargs: None,
    )
    resolvida = obter_ou_criar_identidade_externa(
        provedor=PROVEDOR,
        sujeito_externo="subj-colisao",
    )
    assert resolvida.id == original.id
    assert db.session.is_active
    assert db.session.get(IdentidadeCanalExterna, marcador.id).sujeito_externo == "subj-marcador"
    monkeypatch.undo()

    primeira = registrar_interacao_guest_concluida(original.id, "msg-test-123")
    assert primeira.usadas == 1
    monkeypatch.setattr(canal, "_buscar_evento_na_reserva", lambda *_args, **_kwargs: None)
    replay = registrar_interacao_guest_concluida(original.id, "msg-test-123")
    assert replay.codigo == "interacao_replay"
    assert replay.usadas == 1
    assert replay.consumiu is False
    assert db.session.is_active
    seguinte = registrar_interacao_guest_concluida(original.id, "msg-test-456")
    assert seguinte.codigo == "interacao_registrada"
    assert seguinte.usadas == 2
    assert db.session.get(IdentidadeCanalExterna, marcador.id) is not None


def test_dados_brutos_e_segredos_nao_entram_na_persistencia(ctx):
    with pytest.raises(MaterialSensivelRecusadoError):
        obter_ou_criar_identidade_externa(
            provedor=PROVEDOR,
            sujeito_externo="subj-payload",
            contexto_destino='{"token":"abc","webhook":true}',
        )
    with pytest.raises(CanalAquisicaoError) as telefone:
        obter_ou_criar_identidade_externa(
            provedor=PROVEDOR,
            sujeito_externo="5511999999999",
        )
    assert telefone.value.codigo == "sujeito_externo_invalido"
    with pytest.raises(CanalAquisicaoError) as mensagem:
        ident = _ident("subj-origem")
        iniciar_onboarding_canal(
            ident.id,
            origem_aquisicao="quero saber o frete da minha carga agora",
        )
    assert mensagem.value.codigo == "origem_aquisicao_invalida"
    assert IdentidadeCanalExterna.query.filter_by(sujeito_externo="subj-payload").count() == 0
    assert IdentidadeCanalExterna.query.filter_by(sujeito_externo="5511999999999").count() == 0
    assert OnboardingCanal.query.count() == 0
    assert "webhook" not in _texto_persistido()
    assert "5511999999999" not in _texto_persistido()
    bruto = json.dumps({"password": SENHA_PROIBIDA, "payload": "cru"})
    with pytest.raises(MaterialSensivelRecusadoError):
        ident = _ident("subj-bruto")
        iniciar_onboarding_canal(ident.id)
        registrar_resposta_onboarding(ident.id, campo="aceitar_convite")
        registrar_resposta_onboarding(ident.id, campo="nome", valor=bruto)
    assert SENHA_PROIBIDA not in _texto_persistido()


def _inoperar(identidade_id: int, estado: str) -> None:
    ident = db.session.get(IdentidadeCanalExterna, identidade_id)
    ident.estado = estado
    ident.atualizada_em = utcnow_naive()
    if estado == IdentidadeCanalExterna.ESTADO_REVOGADA:
        ident.revogada_em = utcnow_naive()
    else:
        ident.revogada_em = None
    db.session.commit()


def _ate_termos_apresentados(sujeito: str, email: str):
    ident, _estado = _abrir(sujeito)
    _aceitar_nome_email(ident.id, email)
    registrar_resposta_onboarding(ident.id, campo="job_role", valor="gerente")
    registrar_resposta_onboarding(
        ident.id,
        campo="apresentar_termos",
        termos_referencia="terms-v1",
    )
    return ident


def _ate_entregador(sujeito: str, email: str, veiculo: str):
    ident, _estado = _abrir(sujeito)
    _aceitar_nome_email(ident.id, email)
    registrar_resposta_onboarding(ident.id, campo="job_role", valor="motorista_entregador")
    registrar_resposta_onboarding(
        ident.id,
        campo="entrevista",
        question_key="tipo_atuacao",
        valor="entregador_app",
    )
    estado = registrar_resposta_onboarding(
        ident.id,
        campo="entrevista",
        question_key="veiculo_principal",
        valor=veiculo,
    )
    assert estado.etapa_atual == OnboardingCanal.ETAPA_TERMOS
    return ident


def test_identidade_bloqueada_nao_registra_resposta(ctx):
    ident, estado = _abrir("subj-bloq-resposta")
    etapa = estado.etapa_atual
    _inoperar(ident.id, IdentidadeCanalExterna.ESTADO_BLOQUEADA)
    with pytest.raises(CanalAquisicaoError) as exc:
        registrar_resposta_onboarding(ident.id, campo="aceitar_convite")
    assert exc.value.codigo == "identidade_indisponivel"
    jornada = OnboardingCanal.query.filter_by(identidade_id=ident.id).one()
    assert jornada.etapa == etapa
    assert OnboardingCanal.query.filter_by(identidade_id=ident.id).count() == 1
    assert db.session.is_active


def test_identidade_revogada_nao_registra_resposta(ctx):
    ident, _estado = _abrir("subj-revog-resposta")
    registrar_resposta_onboarding(ident.id, campo="aceitar_convite")
    _inoperar(ident.id, IdentidadeCanalExterna.ESTADO_REVOGADA)
    with pytest.raises(CanalAquisicaoError) as exc:
        registrar_resposta_onboarding(ident.id, campo="nome", valor="Maria Guest")
    assert exc.value.codigo == "identidade_indisponivel"
    jornada = OnboardingCanal.query.filter_by(identidade_id=ident.id).one()
    assert jornada.etapa == OnboardingCanal.ETAPA_NOME
    assert jornada.nome is None
    assert db.session.is_active


def test_identidade_bloqueada_nao_retoma(ctx):
    ident, _estado = _abrir("subj-bloq-retoma")
    registrar_resposta_onboarding(ident.id, campo="aceitar_convite")
    _inoperar(ident.id, IdentidadeCanalExterna.ESTADO_BLOQUEADA)
    with pytest.raises(CanalAquisicaoError) as exc:
        retomar_onboarding(ident.id)
    assert exc.value.codigo == "identidade_indisponivel"
    jornada = OnboardingCanal.query.filter_by(identidade_id=ident.id).one()
    assert jornada.etapa == OnboardingCanal.ETAPA_NOME
    assert db.session.is_active


def test_identidade_revogada_nao_pausa(ctx):
    ident, _estado = _abrir("subj-revog-pausa")
    _inoperar(ident.id, IdentidadeCanalExterna.ESTADO_REVOGADA)
    with pytest.raises(CanalAquisicaoError) as exc:
        pausar_para_interacao_guest(ident.id)
    assert exc.value.codigo == "identidade_indisponivel"
    jornada = OnboardingCanal.query.filter_by(identidade_id=ident.id).one()
    assert jornada.etapa == OnboardingCanal.ETAPA_CONVITE
    assert jornada.pausas_interacao_guest == 0
    assert db.session.is_active


def test_transicao_de_etapa_antiga_nao_sobrescreve_vencedora(ctx):
    ident, _estado = _abrir("subj-cas-direto")
    _aceitar_nome_email(ident.id, "novo.cas@example.com")
    jornada = OnboardingCanal.query.filter_by(identidade_id=ident.id).one()
    assert jornada.etapa == OnboardingCanal.ETAPA_CARGO
    aplicado = canal._persistir_transicao(
        jornada,
        OnboardingCanal.ETAPA_EMAIL,
        {
            "email_normalizado": "ana.cas@example.com",
            "etapa": OnboardingCanal.ETAPA_EMAIL,
            "atualizada_em": utcnow_naive(),
        },
    )
    assert aplicado is False
    db.session.refresh(jornada)
    assert jornada.etapa == OnboardingCanal.ETAPA_CARGO
    assert jornada.email_normalizado == "novo.cas@example.com"
    assert db.session.is_active


def test_registrar_email_perdido_na_corrida_recarrega_vencedor(ctx, monkeypatch):
    conta, franquia = seed_conta_franquia_cliente("conta-cas-email")
    seed_usuario(franquia.id, conta.id, email="ana.cas@example.com")
    ident, _estado = _abrir("subj-cas-email")
    registrar_resposta_onboarding(ident.id, campo="aceitar_convite")
    registrar_resposta_onboarding(ident.id, campo="nome", valor="Ana CAS")
    jornada = OnboardingCanal.query.filter_by(identidade_id=ident.id).one()
    jornada_id = jornada.id

    def vencedor(_email):
        db.session.execute(
            text(
                "UPDATE onboarding_canal "
                "SET etapa = :etapa, email_normalizado = :email "
                "WHERE id = :id"
            ),
            {
                "etapa": OnboardingCanal.ETAPA_CARGO,
                "email": "novo.cas@example.com",
                "id": jornada_id,
            },
        )
        return True

    monkeypatch.setattr(canal, "_email_ja_cadastrado", vencedor)
    resultado = registrar_resposta_onboarding(
        ident.id,
        campo="email",
        valor="ana.cas@example.com",
    )
    assert resultado.codigo == "transicao_conflito"
    assert resultado.etapa_atual == OnboardingCanal.ETAPA_CARGO
    assert resultado.email_normalizado == "novo.cas@example.com"
    gravada = db.session.get(OnboardingCanal, jornada_id)
    assert gravada.etapa == OnboardingCanal.ETAPA_CARGO
    assert gravada.email_normalizado == "novo.cas@example.com"
    assert db.session.is_active


def test_colisao_de_jornada_aberta_devolve_vencedora_e_sessao_util(ctx, monkeypatch):
    ident, estado = _abrir("subj-jornada-colisao")
    monkeypatch.setattr(canal, "_buscar_jornada_aberta", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(canal, "_buscar_jornada_recente", lambda *_args, **_kwargs: None)
    resolvida = iniciar_onboarding_canal(ident.id)
    assert resolvida.onboarding_id == estado.onboarding_id
    assert resolvida.codigo == "onboarding_em_andamento"
    assert resolvida.etapa_atual == OnboardingCanal.ETAPA_CONVITE
    assert OnboardingCanal.query.filter_by(identidade_id=ident.id).count() == 1
    assert db.session.is_active
    marcador = _ident("subj-depois-jornada")
    assert marcador.id is not None
    assert db.session.get(IdentidadeCanalExterna, marcador.id).sujeito_externo == "subj-depois-jornada"


@pytest.mark.parametrize(
    "declaracao",
    ["sim", "true", "false", 1, None, False],
)
def test_aceite_de_termos_rejeita_valor_que_nao_e_true(ctx, declaracao):
    ident = _ate_termos_apresentados("subj-aceite-ruim", "aceite.ruim@example.com")
    with pytest.raises(CanalAquisicaoError) as exc:
        registrar_resposta_onboarding(
            ident.id,
            campo="declarar_aceite_termos",
            aceite_declarado=declaracao,
        )
    assert exc.value.codigo == "aceite_nao_declarado"
    jornada = OnboardingCanal.query.filter_by(identidade_id=ident.id).one()
    assert jornada.termos_aceitos_em is None
    assert jornada.termos_apresentados_em is not None
    assert jornada.etapa == OnboardingCanal.ETAPA_TERMOS


def test_aceite_de_termos_somente_true_registra_data(ctx):
    ident = _ate_termos_apresentados("subj-aceite-true", "aceite.true@example.com")
    estado = registrar_resposta_onboarding(
        ident.id,
        campo="declarar_aceite_termos",
        aceite_declarado=True,
    )
    assert estado.codigo == "termos_aceitos"
    assert estado.etapa_atual == OnboardingCanal.ETAPA_SENHA
    assert estado.termos_aceitos is True
    jornada = OnboardingCanal.query.filter_by(identidade_id=ident.id).one()
    assert jornada.termos_aceitos_em is not None


def test_correcao_de_ramo_remove_veiculo_e_recalcula_etapa(ctx):
    ident = _ate_entregador("subj-ramo-tac", "ramo.tac@example.com", "moto")
    with pytest.raises(CanalAquisicaoError) as arbitaria:
        registrar_resposta_onboarding(
            ident.id,
            campo="entrevista",
            question_key="chave_arbitraria",
            valor="moto",
        )
    assert arbitaria.value.codigo == "pergunta_inaplicavel"
    corrigido = registrar_resposta_onboarding(
        ident.id,
        campo="entrevista",
        question_key="tipo_atuacao",
        valor="tac",
    )
    assert corrigido.respostas_entrevista == {"tipo_atuacao": "tac"}
    assert "veiculo_principal" not in corrigido.respostas_entrevista
    assert corrigido.etapa_atual == OnboardingCanal.ETAPA_TERMOS
    assert corrigido.pergunta_key is None
    jornada = OnboardingCanal.query.filter_by(identidade_id=ident.id).one()
    assert "veiculo_principal" not in (jornada.respostas_entrevista_json or "")
    assert "moto" not in (jornada.respostas_entrevista_json or "")
    de_volta = registrar_resposta_onboarding(
        ident.id,
        campo="entrevista",
        question_key="tipo_atuacao",
        valor="entregador_app",
    )
    assert de_volta.etapa_atual == OnboardingCanal.ETAPA_ENTREVISTA
    assert de_volta.pergunta_key == "veiculo_principal"
    assert de_volta.respostas_entrevista == {"tipo_atuacao": "entregador_app"}


def test_correcao_entregador_carro_para_motorista_app_limpa_veiculo(ctx):
    ident = _ate_entregador("subj-ramo-carro", "ramo.carro@example.com", "carro")
    corrigido = registrar_resposta_onboarding(
        ident.id,
        campo="entrevista",
        question_key="tipo_atuacao",
        valor="motorista_app",
    )
    assert corrigido.respostas_entrevista == {"tipo_atuacao": "motorista_app"}
    assert "veiculo_principal" not in corrigido.respostas_entrevista
    assert corrigido.etapa_atual == OnboardingCanal.ETAPA_TERMOS
    assert corrigido.pergunta_key is None
    jornada = OnboardingCanal.query.filter_by(identidade_id=ident.id).one()
    assert "carro" not in (jornada.respostas_entrevista_json or "")
    assert "veiculo_principal" not in (jornada.respostas_entrevista_json or "")


@pytest.mark.parametrize(
    ("nome", "codigo"),
    [
        ("Authorization: Basic abc123", "nome_invalido"),
        ("Bearer abc123", "material_sensivel"),
        ("api_key=xyz", "material_sensivel"),
        ("token=abc", "material_sensivel"),
        ('{"raw_payload":"..."}', "material_sensivel"),
        ('{"nome":"Ana"}', "material_sensivel"),
        ('["Ana","Maria"]', "nome_invalido"),
        ("[]", "nome_invalido"),
        ("{}", "material_sensivel"),
        ('["x"]', "nome_invalido"),
        ('{"x":1}', "material_sensivel"),
        ("5511999999999", "nome_invalido"),
        ("+55 (11) 99999-9999", "nome_invalido"),
    ],
)
def test_nome_rejeita_tecnico_estruturado_ou_numerico(ctx, nome, codigo):
    ident, _estado = _abrir("subj-nome-ruim")
    registrar_resposta_onboarding(ident.id, campo="aceitar_convite")
    with pytest.raises(CanalAquisicaoError) as exc:
        registrar_resposta_onboarding(ident.id, campo="nome", valor=nome)
    assert exc.value.codigo == codigo
    jornada = OnboardingCanal.query.filter_by(identidade_id=ident.id).one()
    assert jornada.nome is None
    assert jornada.etapa == OnboardingCanal.ETAPA_NOME
    assert nome not in _texto_persistido()
    assert db.session.is_active


def test_nome_rejeita_json_array_bruto_e_sessao_segue_utilizavel(ctx):
    ident, _estado = _abrir("subj-nome-json-array")
    registrar_resposta_onboarding(ident.id, campo="aceitar_convite")
    bruto = '["Ana","Maria"]'
    with pytest.raises(CanalAquisicaoError) as exc:
        registrar_resposta_onboarding(ident.id, campo="nome", valor=bruto)
    assert exc.value.codigo == "nome_invalido"
    jornada = OnboardingCanal.query.filter_by(identidade_id=ident.id).one()
    assert jornada.nome is None
    assert jornada.etapa == OnboardingCanal.ETAPA_NOME
    assert bruto not in _texto_persistido()
    assert db.session.is_active
    estado = registrar_resposta_onboarding(ident.id, campo="nome", valor="Ana Maria")
    assert estado.codigo == "resposta_registrada"
    assert estado.nome == "Ana Maria"
    assert estado.etapa_atual == OnboardingCanal.ETAPA_EMAIL
    assert OnboardingCanal.query.filter_by(identidade_id=ident.id).one().nome == "Ana Maria"


@pytest.mark.parametrize(
    "nome",
    [
        "Ana Maria",
        "João da Silva",
        "José da Silva",
        "Maria-José",
        "Ana-Maria",
        "D'Ávila",
        "João Pedro de Souza",
    ],
)
def test_nome_humano_com_acento_espaco_hifen_ou_apostrofo(ctx, nome):
    ident, _estado = _abrir("subj-nome-humano")
    registrar_resposta_onboarding(ident.id, campo="aceitar_convite")
    estado = registrar_resposta_onboarding(ident.id, campo="nome", valor=nome)
    assert estado.codigo == "resposta_registrada"
    assert estado.nome == nome
    assert estado.etapa_atual == OnboardingCanal.ETAPA_EMAIL
    assert OnboardingCanal.query.filter_by(identidade_id=ident.id).one().nome == nome


def test_limite_cinco_continua_valido(ctx):
    assert limite_interacoes_guest() == 5
    ident = _ident("subj-limite-cinco")
    resultado = registrar_interacao_guest_concluida(ident.id, "msg-limite-cinco")
    assert resultado.codigo == "interacao_registrada"
    assert resultado.usadas == 1
    assert resultado.limite == 5


@pytest.mark.parametrize(
    ("rotulo", "valor"),
    [
        ("zero", 0),
        ("negativo", -3),
        ("float", 4.5),
        ("bool-true", True),
        ("bool-false", False),
        ("texto", "abc"),
    ],
)
def test_limite_invalido_nao_consome_quota(ctx, monkeypatch, rotulo, valor):
    ident = _ident(f"subj-limite-{rotulo}")
    monkeypatch.setattr(canal, "limite_interacoes_guest", lambda: valor)
    with pytest.raises(CanalAquisicaoError) as exc:
        registrar_interacao_guest_concluida(ident.id, "msg-limite-invalido")
    assert exc.value.codigo == "limite_invalido"
    assert db.session.get(IdentidadeCanalExterna, ident.id).interacoes_uteis == 0
    assert InteracaoGuestCanal.query.filter_by(identidade_id=ident.id).count() == 0
    assert db.session.is_active
    monkeypatch.setattr(canal, "limite_interacoes_guest", lambda: 5)
    seguinte = registrar_interacao_guest_concluida(ident.id, "msg-limite-valido")
    assert seguinte.codigo == "interacao_registrada"
    assert seguinte.usadas == 1


def test_configuracao_de_limite_nao_devolve_valor_invalido(monkeypatch):
    import app.services.canal_aquisicao_config as config

    for invalido in (0, -1, 4.5, True, False, "abc"):
        monkeypatch.setattr(config, "LIMITE_INTERACOES_GUEST_PADRAO", invalido)
        with pytest.raises(ValueError):
            config.limite_interacoes_guest()
    monkeypatch.setattr(config, "LIMITE_INTERACOES_GUEST_PADRAO", 5)
    assert config.limite_interacoes_guest() == 5


def test_validade_72h_continua_valida():
    assert validade_onboarding_canal() == timedelta(hours=72)


@pytest.mark.parametrize(
    ("rotulo", "valor"),
    [
        ("zero", timedelta(0)),
        ("negativa", timedelta(hours=-2)),
        ("inteiro", 72),
        ("texto", "72"),
        ("none", None),
    ],
)
def test_validade_invalida_nao_cria_jornada(ctx, monkeypatch, rotulo, valor):
    ident = _ident(f"subj-validade-{rotulo}")
    monkeypatch.setattr(canal, "validade_onboarding_canal", lambda: valor)
    with pytest.raises(CanalAquisicaoError) as exc:
        iniciar_onboarding_canal(ident.id)
    assert exc.value.codigo == "validade_invalida"
    assert OnboardingCanal.query.filter_by(identidade_id=ident.id).count() == 0
    recarregada = db.session.get(IdentidadeCanalExterna, ident.id)
    assert recarregada.estado == IdentidadeCanalExterna.ESTADO_GUEST
    assert db.session.is_active
    monkeypatch.setattr(canal, "validade_onboarding_canal", lambda: timedelta(hours=72))
    criada = iniciar_onboarding_canal(ident.id)
    assert criada.codigo == "onboarding_iniciado"
    jornada = OnboardingCanal.query.filter_by(identidade_id=ident.id).one()
    assert jornada.expira_em - jornada.iniciada_em == timedelta(hours=72)
    assert jornada.expira_em > jornada.iniciada_em


def test_configuracao_de_validade_nao_devolve_duracao_invalida(monkeypatch):
    import app.services.canal_aquisicao_config as config

    for invalido in (timedelta(0), timedelta(hours=-1), 72, None):
        monkeypatch.setattr(config, "VALIDADE_ONBOARDING_CANAL_PADRAO", invalido)
        with pytest.raises(ValueError):
            config.validade_onboarding_canal()
    monkeypatch.setattr(config, "VALIDADE_ONBOARDING_CANAL_PADRAO", timedelta(hours=72))
    assert config.validade_onboarding_canal() == timedelta(hours=72)
