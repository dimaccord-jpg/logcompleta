"""Lote WhatsApp 3C: interpretação determinística de guest e onboarding.

Sem envio, sem modelo e sem cobrança. O texto já roteado vira ação na jornada.
"""
from __future__ import annotations

import importlib.util
import logging
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask, send_from_directory
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from app.db_operational_safety import run_test_schema_operation
from app.extensions import db

from app.models import (
    ConteudoTextualCanal,
    EventoCanalRecebido,
    IdentidadeCanalExterna,
    InteracaoGuestCanal,
    InterpretacaoConversacionalCanal,
    OnboardingCanal,
    OnboardingCanalConclusao,
    TermsOfUse,
    User,
    utcnow_naive,
)
from app.services import canal_entrada_processamento_service as entrada
from app.services import canal_interpretacao_conversacional_service as conversa
from app.services.canal_aquisicao_service import (
    iniciar_onboarding_canal,
    obter_ou_criar_identidade_externa,
    registrar_resposta_onboarding,
)
from app.services.onboarding_entrevista_definicao import JOB_ROLES, PERGUNTAS
from tests.conftest import (
    PYTEST_DISPOSABLE_SQLALCHEMY_URI,
    seed_conta_franquia_cliente,
    seed_usuario,
)

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "versions" / "q8r9s0t1u2v3_interpretacao_conversacional_canal.py"
MIGRATION_REF = ROOT / "migrations" / "versions" / "r9s0t1u2v3w4_interpretacao_conclusao_ref.py"
_MARCA_LINK = "/onboarding/canal/concluir/"
SECRET = "teste-interpretacao-canal-3c"
SUJEITO = "subjLote3c01"
DESTINO = "destLote3c1"
CORRELATION = "corr3c0001"


def _migration_module():
    spec = importlib.util.spec_from_file_location(
        "mig_interpretacao_conversacional",
        MIGRATION,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fonte() -> str:
    return Path(conversa.__file__).read_text(encoding="utf-8")


def _evento(texto: str, externo: str, sujeito: str = SUJEITO) -> EventoCanalRecebido:
    row = EventoCanalRecebido(
        provider=EventoCanalRecebido.PROVIDER_META_WHATSAPP,
        evento_externo_id=externo,
        tipo_evento=EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL,
        sujeito_externo=sujeito,
        contexto_destino=DESTINO,
        recebido_em=utcnow_naive(),
        status_processamento=EventoCanalRecebido.STATUS_RECEBIDO,
        correlation_id=CORRELATION,
        diagnostico_seguro="mensagem_textual",
    )
    db.session.add(row)
    db.session.flush()
    entrada.registrar_conteudo_textual_canal(row.id, texto)
    db.session.commit()
    return row


def _rotear(texto: str, externo: str, sujeito: str = SUJEITO):
    evento = _evento(texto, externo, sujeito)
    return evento, entrada.processar_evento_canal_recebido(evento.id)


def _interpretar(texto: str, externo: str, sujeito: str = SUJEITO):
    evento, roteamento = _rotear(texto, externo, sujeito)
    return evento, roteamento, conversa.interpretar_mensagem_canal(roteamento)


def _preparar(
    ate: str,
    *,
    sujeito: str = SUJEITO,
    email: str = "fluxo.lote3c@example.com",
    nome: str = "Maria Guest",
    job_role: str = "analista",
    entrevista: list[tuple[str, str]] | None = None,
):
    ident = obter_ou_criar_identidade_externa(
        provedor=entrada.PROVEDOR_IDENTIDADE_WHATSAPP,
        sujeito_externo=sujeito,
        contexto_destino=DESTINO,
        commit=True,
    )
    iniciar_onboarding_canal(ident.id, commit=True)
    if ate == OnboardingCanal.ETAPA_CONVITE:
        return ident
    registrar_resposta_onboarding(ident.id, campo="aceitar_convite", commit=True)
    if ate == OnboardingCanal.ETAPA_NOME:
        return ident
    registrar_resposta_onboarding(ident.id, campo="nome", valor=nome, commit=True)
    if ate == OnboardingCanal.ETAPA_EMAIL:
        return ident
    registrar_resposta_onboarding(ident.id, campo="email", valor=email, commit=True)
    if ate == OnboardingCanal.ETAPA_CARGO:
        return ident
    registrar_resposta_onboarding(ident.id, campo="job_role", valor=job_role, commit=True)
    if ate == OnboardingCanal.ETAPA_ENTREVISTA:
        jornada = OnboardingCanal.query.filter_by(identidade_id=ident.id).one()
        assert jornada.etapa == ate
        return ident
    for question_key, valor in entrevista or []:
        registrar_resposta_onboarding(
            ident.id,
            campo="entrevista",
            question_key=question_key,
            valor=valor,
            commit=True,
        )
    if ate == OnboardingCanal.ETAPA_TERMOS:
        registrar_resposta_onboarding(
            ident.id,
            campo="apresentar_termos",
            termos_referencia=_referencia_termo_da_jornada(),
            commit=True,
        )
    elif ate == OnboardingCanal.ETAPA_SENHA:
        registrar_resposta_onboarding(
            ident.id,
            campo="apresentar_termos",
            termos_referencia="terms-v1",
            commit=True,
        )
        registrar_resposta_onboarding(
            ident.id,
            campo="declarar_aceite_termos",
            termos_referencia="terms-v1",
            aceite_declarado=True,
            commit=True,
        )
    jornada = OnboardingCanal.query.filter_by(identidade_id=ident.id).one()
    assert jornada.etapa == ate
    return ident


def _referencia_termo_da_jornada() -> str:
    termo = TermsOfUse(filename="termo-jornada-lote3c.pdf", is_active=False)
    db.session.add(termo)
    db.session.commit()
    return f"terms-of-use:{int(termo.id)}"


def _disponibilizar_termo(app, monkeypatch, tmp_path, base="https://homolog.exemplo.test"):
    import app.legal_document_storage as legal_storage

    monkeypatch.setenv("PUBLIC_BASE_URL", base)
    app.config["PUBLIC_BASE_URL"] = base
    monkeypatch.setattr(legal_storage, "settings", SimpleNamespace(data_dir=str(tmp_path)))
    pasta = tmp_path / "legal" / "terms"
    pasta.mkdir(parents=True)
    (pasta / "termo-teste.pdf").write_bytes(b"%PDF-1.4\n")
    termo = TermsOfUse(filename="termo-teste.pdf", is_active=True)
    db.session.add(termo)
    db.session.commit()
    return termo, f"{base}/termos-de-uso"


def _jornada() -> OnboardingCanal:
    return OnboardingCanal.query.one()


def _texto_de(row) -> str:
    return "\n".join(str(getattr(row, coluna.name) or "") for coluna in row.__table__.columns)


def _espionar(monkeypatch, nome: str) -> dict:
    original = getattr(conversa, nome)
    capturado = {"n": 0, "token": None, "url": None}

    def _wrap(*args, **kwargs):
        emissao = original(*args, **kwargs)
        capturado["n"] += 1
        capturado["token"] = emissao.token
        capturado["url"] = emissao.url
        return emissao

    monkeypatch.setattr(conversa, nome, _wrap)
    return capturado


def _assert_segredo_ausente(*textos: str, token: str, url: str) -> None:
    assert token
    assert url
    assert token in url
    for texto in textos:
        assert token not in texto
        assert url not in texto
        assert _MARCA_LINK not in texto


def test_migration_cria_resultado_minimo_do_tratamento():
    modulo = _migration_module()
    assert modulo.revision == "q8r9s0t1u2v3"
    assert modulo.down_revision == "p7q8r9s0t1u2"
    assert modulo._SQL_CODIGO == InterpretacaoConversacionalCanal._SQL_CODIGO
    assert modulo._SQL_ACAO == InterpretacaoConversacionalCanal._SQL_ACAO
    assert modulo._SQL_ETAPA == InterpretacaoConversacionalCanal._SQL_ETAPA
    assert modulo._SQL_TEXTO == InterpretacaoConversacionalCanal._SQL_TEXTO
    texto = MIGRATION.read_text(encoding="utf-8")
    assert 'sa.Column("payload"' not in texto
    assert "texto_usuario" not in texto
    assert "token_hash" not in texto
    assert "email" not in texto.split("e-mail")[0]


@pytest.mark.parametrize(
    "texto",
    [
        "quero me cadastrar",
        "  Quero   me   CADASTRAR  ",
        "começar cadastro",
        "iniciar cadastro",
        "cadastrar",
    ],
)
def test_guest_comando_de_cadastro_inicia_jornada(ctx, texto):
    _evento_id, _rota, resultado = _interpretar(texto, "wamid.CADASTRO")
    assert resultado.codigo == conversa.CODIGO_ONBOARDING_INICIADO
    assert resultado.acao == conversa.ACAO_INICIAR
    assert resultado.texto_resposta == conversa.TEXTO_NOME
    assert resultado.etapa_atual == OnboardingCanal.ETAPA_NOME
    assert resultado.correlation_id == CORRELATION
    jornada = _jornada()
    assert resultado.onboarding_id == jornada.id
    assert jornada.etapa == OnboardingCanal.ETAPA_NOME
    ident = IdentidadeCanalExterna.query.one()
    assert ident.estado == IdentidadeCanalExterna.ESTADO_CADASTRO_EM_ANDAMENTO
    assert ident.interacoes_uteis == 0
    assert InteracaoGuestCanal.query.count() == 0


def test_guest_outra_pergunta_nao_consome_quota(ctx, monkeypatch):
    chamadas = {"n": 0}

    def _proibido(*_args, **_kwargs):
        chamadas["n"] += 1
        raise AssertionError("quota guest")

    monkeypatch.setattr(
        "app.services.canal_aquisicao_service.registrar_interacao_guest_concluida",
        _proibido,
    )
    _evento, _rota, resultado = _interpretar("quanto custa o frete?", "wamid.PERGUNTA")
    assert resultado.codigo == conversa.CODIGO_ORIENTACAO_GUEST
    assert resultado.acao == conversa.ACAO_ORIENTAR_GUEST
    assert resultado.texto_resposta == conversa.TEXTO_GUEST
    assert "quero me cadastrar" not in (resultado.texto_resposta or "")
    assert resultado.onboarding_id is None
    assert chamadas["n"] == 0
    ident = IdentidadeCanalExterna.query.one()
    assert ident.estado == IdentidadeCanalExterna.ESTADO_GUEST
    assert ident.interacoes_uteis == 0
    assert InteracaoGuestCanal.query.count() == 0
    assert OnboardingCanal.query.count() == 0
    assert "registrar_interacao_guest" not in _fonte()


@pytest.mark.parametrize(
    "texto",
    ["sim", "vamos continuar", "como faço?", "qual o prazo do frete?"],
)
def test_continuacao_depois_da_orientacao_inicia_onboarding(ctx, texto):
    _evento, _rota, primeira = _interpretar("quanto custa o frete?", "wamid.GUEST.1")
    assert primeira.codigo == conversa.CODIGO_ORIENTACAO_GUEST
    assert IdentidadeCanalExterna.query.one().estado == IdentidadeCanalExterna.ESTADO_GUEST
    assert OnboardingCanal.query.count() == 0
    _evento, _rota, resultado = _interpretar(texto, "wamid.GUEST.2")
    assert resultado.codigo == conversa.CODIGO_ONBOARDING_INICIADO
    assert resultado.acao == conversa.ACAO_INICIAR
    assert resultado.texto_resposta == conversa.TEXTO_NOME
    assert resultado.etapa_atual == OnboardingCanal.ETAPA_NOME
    jornada = _jornada()
    assert jornada.etapa == OnboardingCanal.ETAPA_NOME
    assert jornada.nome is None
    ident = IdentidadeCanalExterna.query.one()
    assert ident.estado == IdentidadeCanalExterna.ESTADO_CADASTRO_EM_ANDAMENTO
    assert ident.interacoes_uteis == 0
    assert texto not in (jornada.nome or "")


def test_replay_da_primeira_mensagem_nao_inicia_onboarding(ctx):
    evento, roteamento, primeiro = _interpretar("quanto custa o frete?", "wamid.REPLAY.GUEST")
    assert primeiro.codigo == conversa.CODIGO_ORIENTACAO_GUEST
    repetido = conversa.interpretar_mensagem_canal(roteamento)
    assert repetido.codigo == conversa.CODIGO_EVENTO_JA_TRATADO
    assert repetido.texto_resposta == primeiro.texto_resposta
    assert repetido.acao == conversa.ACAO_ORIENTAR_GUEST
    assert OnboardingCanal.query.count() == 0
    assert IdentidadeCanalExterna.query.one().estado == IdentidadeCanalExterna.ESTADO_GUEST
    assert InterpretacaoConversacionalCanal.query.count() == 1
    assert evento.id == roteamento.evento_id


def test_replay_da_continuacao_nao_avanca_de_novo(ctx):
    _interpretar("quanto custa o frete?", "wamid.REPLAY.BASE")
    _evento, roteamento, primeiro = _interpretar("sim", "wamid.REPLAY.CONT")
    assert primeiro.codigo == conversa.CODIGO_ONBOARDING_INICIADO
    jornada = _jornada()
    marca = jornada.atualizada_em
    repetido = conversa.interpretar_mensagem_canal(roteamento)
    db.session.refresh(jornada)
    assert repetido.codigo == conversa.CODIGO_EVENTO_JA_TRATADO
    assert repetido.texto_resposta == conversa.TEXTO_NOME
    assert jornada.etapa == OnboardingCanal.ETAPA_NOME
    assert jornada.nome is None
    assert jornada.atualizada_em == marca
    assert OnboardingCanal.query.count() == 1


def test_guest_diferente_nao_herda_orientacao(ctx):
    _interpretar("quanto custa o frete?", "wamid.GUEST.A", sujeito=SUJEITO)
    _evento, _rota, outro = _interpretar("sim", "wamid.GUEST.B", sujeito="subjLote3c03")
    assert outro.codigo == conversa.CODIGO_ORIENTACAO_GUEST
    assert outro.onboarding_id is None
    assert OnboardingCanal.query.count() == 0
    outro_ident = IdentidadeCanalExterna.query.filter_by(sujeito_externo="subjLote3c03").one()
    assert outro_ident.estado == IdentidadeCanalExterna.ESTADO_GUEST


def test_orientacao_incompleta_nao_libera_onboarding(ctx):
    evento, _roteamento = _rotear("quanto custa o frete?", "wamid.INC.1")
    db.session.add(
        InterpretacaoConversacionalCanal(
            evento_id=evento.id,
            codigo=conversa.CODIGO_EM_TRATAMENTO,
            acao=conversa.ACAO_RESERVADA,
        )
    )
    db.session.commit()
    _evento, _rota, resultado = _interpretar("sim", "wamid.INC.2")
    assert resultado.codigo == conversa.CODIGO_ORIENTACAO_GUEST
    assert OnboardingCanal.query.count() == 0
    assert IdentidadeCanalExterna.query.one().estado == IdentidadeCanalExterna.ESTADO_GUEST


def test_nome_valido_avanca(ctx, caplog):
    _preparar(OnboardingCanal.ETAPA_NOME)
    with caplog.at_level(logging.INFO):
        _evento, _rota, resultado = _interpretar("Maria Silva", "wamid.NOME")
    assert resultado.codigo == conversa.CODIGO_RESPOSTA_REGISTRADA
    assert resultado.etapa_atual == OnboardingCanal.ETAPA_EMAIL
    assert resultado.texto_resposta == conversa.TEXTO_EMAIL
    jornada = _jornada()
    assert jornada.etapa == OnboardingCanal.ETAPA_EMAIL
    assert jornada.nome == "Maria Silva"
    assert "Maria Silva" not in caplog.text
    assert f"evento_id={_evento.id}" in caplog.text


def test_nome_invalido_nao_avanca(ctx):
    _preparar(OnboardingCanal.ETAPA_NOME)
    _evento, _rota, resultado = _interpretar("12345", "wamid.NOME.RUIM")
    assert resultado.codigo == conversa.CODIGO_NOME_INVALIDO
    assert resultado.etapa_atual == OnboardingCanal.ETAPA_NOME
    assert "nome" in (resultado.texto_resposta or "").casefold()
    jornada = _jornada()
    assert jornada.etapa == OnboardingCanal.ETAPA_NOME
    assert jornada.nome is None


def test_email_valido_avanca(ctx, caplog):
    email = "maria.lote3c@example.com"
    _preparar(OnboardingCanal.ETAPA_EMAIL)
    with caplog.at_level(logging.INFO):
        _evento, _rota, resultado = _interpretar(email, "wamid.EMAIL")
    assert resultado.codigo == conversa.CODIGO_RESPOSTA_REGISTRADA
    assert resultado.etapa_atual == OnboardingCanal.ETAPA_CARGO
    assert "1. Analista" in (resultado.texto_resposta or "")
    assert JOB_ROLES[-1][1] in (resultado.texto_resposta or "")
    jornada = _jornada()
    assert jornada.etapa == OnboardingCanal.ETAPA_CARGO
    assert jornada.email_normalizado == email
    assert email not in caplog.text


def test_email_invalido_nao_avanca(ctx):
    _preparar(OnboardingCanal.ETAPA_EMAIL)
    _evento, _rota, resultado = _interpretar("nao-e-email", "wamid.EMAIL.RUIM")
    assert resultado.codigo == conversa.CODIGO_EMAIL_INVALIDO
    assert resultado.etapa_atual == OnboardingCanal.ETAPA_EMAIL
    jornada = _jornada()
    assert jornada.etapa == OnboardingCanal.ETAPA_EMAIL
    assert jornada.email_normalizado is None


def test_conta_existente_entra_no_fluxo_de_vinculo(ctx, app, monkeypatch, caplog):
    app.config["SECRET_KEY"] = SECRET
    email = "existe.lote3c@example.com"
    conta, franquia = seed_conta_franquia_cliente("conta-lote3c")
    seed_usuario(franquia.id, conta.id, email=email)
    antes = User.query.count()
    chamadas = {"senha": 0, "vinculo": 0}
    senha = conversa.emitir_link_conclusao_onboarding
    vinculo = conversa.emitir_link_vinculo_conta_existente

    def _senha(*args, **kwargs):
        chamadas["senha"] += 1
        return senha(*args, **kwargs)

    def _vinculo(*args, **kwargs):
        chamadas["vinculo"] += 1
        return vinculo(*args, **kwargs)

    monkeypatch.setattr(conversa, "emitir_link_conclusao_onboarding", _senha)
    monkeypatch.setattr(conversa, "emitir_link_vinculo_conta_existente", _vinculo)
    _preparar(OnboardingCanal.ETAPA_EMAIL, email="outro.lote3c@example.com")
    with caplog.at_level(logging.INFO):
        _evento, _rota, resultado = _interpretar(email, "wamid.EXISTE")
    assert resultado.codigo == "existing_account_verification_required"
    assert resultado.acao == conversa.ACAO_ORIENTAR_CONTA
    assert resultado.texto_resposta == conversa.TEXTO_CONTA
    assert _MARCA_LINK not in resultado.texto_resposta
    assert chamadas == {"senha": 0, "vinculo": 1}
    assert User.query.count() == antes
    jornada = _jornada()
    assert jornada.etapa == OnboardingCanal.ETAPA_EMAIL
    assert jornada.email_normalizado == email
    conclusao = OnboardingCanalConclusao.query.one()
    assert conclusao.finalidade == OnboardingCanalConclusao.FINALIDADE_VINCULAR_CONTA
    assert resultado.conclusao_id == conclusao.id
    assert email not in caplog.text
    assert email not in (resultado.texto_resposta or "")


def test_cargo_por_numero_funciona(ctx, app, monkeypatch, tmp_path):
    _disponibilizar_termo(app, monkeypatch, tmp_path)
    _preparar(OnboardingCanal.ETAPA_CARGO)
    _evento, _rota, resultado = _interpretar("2", "wamid.CARGO.NUM")
    jornada = _jornada()
    assert jornada.job_role == "coordenador"
    assert jornada.etapa == OnboardingCanal.ETAPA_TERMOS
    assert jornada.termos_apresentados_em is not None
    assert resultado.etapa_atual == OnboardingCanal.ETAPA_TERMOS
    assert resultado.codigo == conversa.CODIGO_TERMOS_APRESENTADOS
    assert "ACEITO" in (resultado.texto_resposta or "")


def test_cargo_por_label_funciona(ctx):
    _preparar(OnboardingCanal.ETAPA_CARGO)
    _evento, _rota, resultado = _interpretar("  Gerente  ", "wamid.CARGO.LABEL")
    jornada = _jornada()
    assert jornada.job_role == "gerente"
    assert jornada.etapa == OnboardingCanal.ETAPA_TERMOS
    assert resultado.etapa_atual == OnboardingCanal.ETAPA_TERMOS


def test_cargo_invalido_nao_avanca(ctx):
    _preparar(OnboardingCanal.ETAPA_CARGO)
    _evento, _rota, resultado = _interpretar("Astronauta", "wamid.CARGO.RUIM")
    assert resultado.codigo == conversa.CODIGO_CARGO_INVALIDO
    assert resultado.etapa_atual == OnboardingCanal.ETAPA_CARGO
    texto = resultado.texto_resposta or ""
    for _chave, rotulo in JOB_ROLES:
        assert rotulo in texto
    jornada = _jornada()
    assert jornada.etapa == OnboardingCanal.ETAPA_CARGO
    assert jornada.job_role is None


def test_motorista_entregador_abre_entrevista(ctx):
    _preparar(OnboardingCanal.ETAPA_CARGO)
    _evento, _rota, resultado = _interpretar("Motorista / Entregador", "wamid.CARGO.MOT")
    jornada = _jornada()
    assert jornada.job_role == "motorista_entregador"
    assert jornada.etapa == OnboardingCanal.ETAPA_ENTREVISTA
    assert resultado.etapa_atual == OnboardingCanal.ETAPA_ENTREVISTA
    assert PERGUNTAS["tipo_atuacao"].texto in (resultado.texto_resposta or "")
    for opcao in PERGUNTAS["tipo_atuacao"].opcoes:
        assert opcao.label in (resultado.texto_resposta or "")
    assert jornada.respostas_entrevista_json in (None, "")


def test_tipo_atuacao_por_numero_funciona(ctx, app, monkeypatch, tmp_path):
    _disponibilizar_termo(app, monkeypatch, tmp_path)
    _preparar(
        OnboardingCanal.ETAPA_ENTREVISTA,
        job_role="motorista_entregador",
        email="tipo.lote3c@example.com",
    )
    opcao = PERGUNTAS["tipo_atuacao"].opcoes[0]
    _evento, _rota, resultado = _interpretar("1", "wamid.TIPO.NUM")
    jornada = _jornada()
    assert '"tipo_atuacao":"' + opcao.key + '"' in (jornada.respostas_entrevista_json or "")
    assert "veiculo_principal" not in (jornada.respostas_entrevista_json or "")
    assert jornada.etapa == OnboardingCanal.ETAPA_TERMOS
    assert resultado.codigo == conversa.CODIGO_TERMOS_APRESENTADOS


def test_entregador_app_abre_veiculo(ctx):
    _preparar(
        OnboardingCanal.ETAPA_ENTREVISTA,
        job_role="motorista_entregador",
        email="entregador.lote3c@example.com",
    )
    _evento, _rota, resultado = _interpretar("2", "wamid.TIPO.APP")
    jornada = _jornada()
    assert jornada.etapa == OnboardingCanal.ETAPA_ENTREVISTA
    assert '"tipo_atuacao":"entregador_app"' in (jornada.respostas_entrevista_json or "")
    assert "veiculo_principal" not in (jornada.respostas_entrevista_json or "")
    assert resultado.etapa_atual == OnboardingCanal.ETAPA_ENTREVISTA
    assert PERGUNTAS["veiculo_principal"].texto in (resultado.texto_resposta or "")
    for opcao in PERGUNTAS["veiculo_principal"].opcoes:
        assert opcao.label in (resultado.texto_resposta or "")


def test_tac_pula_veiculo(ctx, app, monkeypatch, tmp_path):
    _disponibilizar_termo(app, monkeypatch, tmp_path)
    _preparar(
        OnboardingCanal.ETAPA_ENTREVISTA,
        job_role="motorista_entregador",
        email="tac.lote3c@example.com",
    )
    _evento, _rota, resultado = _interpretar("3", "wamid.TIPO.TAC")
    jornada = _jornada()
    assert '"tipo_atuacao":"tac"' in (jornada.respostas_entrevista_json or "")
    assert "veiculo_principal" not in (jornada.respostas_entrevista_json or "")
    assert jornada.etapa == OnboardingCanal.ETAPA_TERMOS
    assert jornada.termos_apresentados_em is not None
    assert resultado.codigo == conversa.CODIGO_TERMOS_APRESENTADOS


def test_veiculo_valido_conclui_entrevista(ctx, app, monkeypatch, tmp_path):
    _disponibilizar_termo(app, monkeypatch, tmp_path)
    _preparar(
        OnboardingCanal.ETAPA_ENTREVISTA,
        job_role="motorista_entregador",
        email="veiculo.lote3c@example.com",
    )
    registrar_resposta_onboarding(
        IdentidadeCanalExterna.query.one().id,
        campo="entrevista",
        question_key="tipo_atuacao",
        valor="entregador_app",
        commit=True,
    )
    _evento, _rota, resultado = _interpretar("Moto", "wamid.VEICULO")
    jornada = _jornada()
    assert '"veiculo_principal":"moto"' in (jornada.respostas_entrevista_json or "")
    assert jornada.etapa == OnboardingCanal.ETAPA_TERMOS
    assert resultado.codigo == conversa.CODIGO_TERMOS_APRESENTADOS
    assert "ACEITO" in (resultado.texto_resposta or "")


def test_resposta_de_entrevista_invalida_nao_avanca(ctx):
    _preparar(
        OnboardingCanal.ETAPA_ENTREVISTA,
        job_role="motorista_entregador",
        email="invalida.lote3c@example.com",
    )
    _evento, _rota, resultado = _interpretar("banana", "wamid.TIPO.RUIM")
    assert resultado.codigo == conversa.CODIGO_RESPOSTA_INVALIDA
    assert resultado.etapa_atual == OnboardingCanal.ETAPA_ENTREVISTA
    assert PERGUNTAS["tipo_atuacao"].texto in (resultado.texto_resposta or "")
    jornada = _jornada()
    assert jornada.etapa == OnboardingCanal.ETAPA_ENTREVISTA
    assert jornada.respostas_entrevista_json in (None, "")


def test_termos_so_avancam_com_aceite_explicito(ctx, app):
    app.config["SECRET_KEY"] = SECRET
    _preparar(OnboardingCanal.ETAPA_TERMOS, email="termos.lote3c@example.com")
    _evento, _rota, recusa = _interpretar("aceito os termos", "wamid.TERMOS.NAO")
    assert recusa.codigo == conversa.CODIGO_ACEITE_NAO_RECONHECIDO
    assert recusa.etapa_atual == OnboardingCanal.ETAPA_TERMOS
    assert "ACEITO" in (recusa.texto_resposta or "")
    jornada = _jornada()
    assert jornada.etapa == OnboardingCanal.ETAPA_TERMOS
    assert jornada.termos_aceitos_em is None
    assert OnboardingCanalConclusao.query.count() == 0
    _evento, _rota, aceite = _interpretar("  ACEITO  ", "wamid.TERMOS.SIM")
    assert aceite.etapa_atual == OnboardingCanal.ETAPA_SENHA
    assert aceite.texto_resposta == conversa.TEXTO_SENHA
    assert aceite.conclusao_id == OnboardingCanalConclusao.query.one().id
    assert _MARCA_LINK not in aceite.texto_resposta
    jornada = _jornada()
    assert jornada.etapa == OnboardingCanal.ETAPA_SENHA
    assert jornada.termos_aceitos_em is not None
    conclusao = OnboardingCanalConclusao.query.one()
    assert conclusao.finalidade == OnboardingCanalConclusao.FINALIDADE_DEFINIR_SENHA
    numerica = _preparar(
        OnboardingCanal.ETAPA_TERMOS,
        sujeito="subjLote3c02",
        email="termos.numero.lote3c@example.com",
    )
    _evento, _rota, por_numero = _interpretar("1", "wamid.TERMOS.NUM", sujeito=numerica.sujeito_externo)
    assert por_numero.codigo == conversa.CODIGO_ACEITE_NAO_RECONHECIDO
    assert por_numero.etapa_atual == OnboardingCanal.ETAPA_TERMOS
    assert "1. ACEITO" not in (por_numero.texto_resposta or "")
    jornada_numero = OnboardingCanal.query.filter_by(identidade_id=numerica.id).one()
    assert jornada_numero.termos_aceitos_em is None
    assert jornada_numero.etapa == OnboardingCanal.ETAPA_TERMOS


def test_aguardando_senha_emite_link_do_lote_2(ctx, app, monkeypatch):
    app.config["SECRET_KEY"] = SECRET
    _preparar(OnboardingCanal.ETAPA_SENHA, email="senha.lote3c@example.com")
    chamadas = {"senha": 0, "vinculo": 0}
    senha = conversa.emitir_link_conclusao_onboarding
    vinculo = conversa.emitir_link_vinculo_conta_existente

    def _senha(*args, **kwargs):
        chamadas["senha"] += 1
        return senha(*args, **kwargs)

    def _vinculo(*args, **kwargs):
        chamadas["vinculo"] += 1
        return vinculo(*args, **kwargs)

    monkeypatch.setattr(conversa, "emitir_link_conclusao_onboarding", _senha)
    monkeypatch.setattr(conversa, "emitir_link_vinculo_conta_existente", _vinculo)
    antes = User.query.count()
    _evento, _rota, resultado = _interpretar("pronto", "wamid.SENHA")
    assert chamadas == {"senha": 1, "vinculo": 0}
    assert resultado.codigo == conversa.CODIGO_LINK_EMITIDO
    assert resultado.acao == conversa.ACAO_EMITIR_SENHA
    assert resultado.etapa_atual == OnboardingCanal.ETAPA_SENHA
    assert resultado.texto_resposta == conversa.TEXTO_SENHA
    assert _MARCA_LINK not in resultado.texto_resposta
    assert resultado.conclusao_id == OnboardingCanalConclusao.query.one().id
    assert User.query.count() == antes
    assert _jornada().etapa == OnboardingCanal.ETAPA_SENHA


def test_token_nao_aparece_em_log_nem_evento(ctx, app, monkeypatch, caplog):
    app.config["SECRET_KEY"] = SECRET
    _preparar(OnboardingCanal.ETAPA_SENHA, email="segredo.lote3c@example.com", nome="Ana Guest")
    capturado = _espionar(monkeypatch, "emitir_link_conclusao_onboarding")
    with caplog.at_level(logging.DEBUG):
        evento, _rota, resultado = _interpretar("MENSAGEM_SENSIVEL_3C", "wamid.TOKEN")
    token = capturado["token"]
    url = capturado["url"]
    assert resultado.texto_resposta == conversa.TEXTO_SENHA
    _assert_segredo_ausente(
        resultado.texto_resposta or "",
        caplog.text,
        token=token,
        url=url,
    )
    assert "MENSAGEM_SENSIVEL_3C" not in caplog.text
    assert "segredo.lote3c@example.com" not in caplog.text
    assert "Ana Guest" not in caplog.text
    gravado = db.session.get(EventoCanalRecebido, evento.id)
    _assert_segredo_ausente(_texto_de(gravado), token=token, url=url)
    conteudo = ConteudoTextualCanal.query.filter_by(evento_id=evento.id).one()
    _assert_segredo_ausente(conteudo.texto, token=token, url=url)
    conclusao = OnboardingCanalConclusao.query.one()
    assert conclusao.token_hash != token
    assert token not in conclusao.token_hash
    assert url not in conclusao.token_hash
    interpretacao = InterpretacaoConversacionalCanal.query.one()
    _assert_segredo_ausente(_texto_de(interpretacao), token=token, url=url)
    assert interpretacao.conclusao_id == conclusao.id


def test_replay_do_mesmo_evento_nao_avanca_de_novo(ctx, monkeypatch):
    _preparar(OnboardingCanal.ETAPA_NOME)
    evento, roteamento, primeiro = _interpretar("Maria Silva", "wamid.REPLAY")
    assert primeiro.codigo == conversa.CODIGO_RESPOSTA_REGISTRADA
    jornada = _jornada()
    marca = jornada.atualizada_em
    chamadas = {"n": 0}
    original = conversa.registrar_resposta_onboarding

    def _spy(*args, **kwargs):
        chamadas["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(conversa, "registrar_resposta_onboarding", _spy)
    segundo = conversa.interpretar_mensagem_canal(roteamento)
    db.session.refresh(jornada)
    assert segundo.codigo == conversa.CODIGO_EVENTO_JA_TRATADO
    assert segundo.texto_resposta == primeiro.texto_resposta
    assert segundo.etapa_atual == OnboardingCanal.ETAPA_EMAIL
    assert chamadas["n"] == 0
    assert jornada.etapa == OnboardingCanal.ETAPA_EMAIL
    assert jornada.nome == "Maria Silva"
    assert jornada.atualizada_em == marca
    assert InterpretacaoConversacionalCanal.query.count() == 1
    assert evento.id == roteamento.evento_id


def test_tentativa_concorrente_nao_duplica_transicao():
    flask_app = Flask("corrida-canal-3c")
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
        ident = obter_ou_criar_identidade_externa(
            provedor=entrada.PROVEDOR_IDENTIDADE_WHATSAPP,
            sujeito_externo=SUJEITO,
            contexto_destino=DESTINO,
            commit=True,
        )
        iniciar_onboarding_canal(ident.id, commit=True)
        registrar_resposta_onboarding(ident.id, campo="aceitar_convite", commit=True)
        evento = _evento("Maria Silva", "wamid.CORRIDA")
        roteamento = entrada.processar_evento_canal_recebido(evento.id)
        assert roteamento.rota == entrada.ROTA_ONBOARDING
        db.session.remove()
        barreira = threading.Barrier(2)
        resultados: dict[int, str] = {}

        def _worker(indice: int) -> None:
            try:
                with flask_app.app_context():
                    barreira.wait(timeout=5)
                    resultados[indice] = conversa.interpretar_mensagem_canal(roteamento).codigo
                    db.session.remove()
            except Exception as exc:
                resultados[indice] = f"erro:{type(exc).__name__}:{exc}"

        threads = [threading.Thread(target=_worker, args=(indice,)) for indice in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        assert set(resultados) == {1, 2}
        assert all(not str(codigo).startswith("erro:") for codigo in resultados.values())
        assert list(resultados.values()).count(conversa.CODIGO_RESPOSTA_REGISTRADA) == 1
        assert conversa.CODIGO_EVENTO_JA_TRATADO in resultados.values()
        jornada = OnboardingCanal.query.one()
        assert jornada.etapa == OnboardingCanal.ETAPA_EMAIL
        assert jornada.nome == "Maria Silva"
        assert InterpretacaoConversacionalCanal.query.count() == 1
        db.session.remove()
        run_test_schema_operation(
            db,
            PYTEST_DISPOSABLE_SQLALCHEMY_URI,
            testing=True,
            operation="drop_all",
        )


def test_continuacoes_simultaneas_criam_uma_jornada():
    flask_app = Flask("corrida-guest-onboarding")
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
        evento, roteamento, primeiro = _interpretar("quanto custa o frete?", "wamid.PAR.0")
        assert primeiro.codigo == conversa.CODIGO_ORIENTACAO_GUEST
        evento_a, rota_a = _rotear("sim", "wamid.PAR.1")
        evento_b, rota_b = _rotear("vamos continuar", "wamid.PAR.2")
        assert rota_a.rota == entrada.ROTA_GUEST
        assert rota_b.rota == entrada.ROTA_GUEST
        ids_eventos = {evento.id, evento_a.id, evento_b.id}
        db.session.remove()
        barreira = threading.Barrier(2)
        resultados: dict[int, object] = {}

        def _worker(indice: int, roteamento_worker) -> None:
            try:
                with flask_app.app_context():
                    barreira.wait(timeout=5)
                    resultados[indice] = conversa.interpretar_mensagem_canal(roteamento_worker)
                    db.session.remove()
            except Exception as exc:
                resultados[indice] = f"erro:{type(exc).__name__}:{exc}"

        threads = [
            threading.Thread(target=_worker, args=(1, rota_a)),
            threading.Thread(target=_worker, args=(2, rota_b)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        assert set(resultados) == {1, 2}
        assert all(not isinstance(item, str) for item in resultados.values())
        jornada = OnboardingCanal.query.one()
        assert jornada.etapa == OnboardingCanal.ETAPA_NOME
        assert jornada.nome is None
        ident = IdentidadeCanalExterna.query.one()
        assert ident.estado == IdentidadeCanalExterna.ESTADO_CADASTRO_EM_ANDAMENTO
        for item in resultados.values():
            assert item.texto_resposta == conversa.TEXTO_NOME
            assert item.etapa_atual == OnboardingCanal.ETAPA_NOME
        assert OnboardingCanal.query.count() == 1
        assert len(ids_eventos) == 3
        db.session.remove()
        run_test_schema_operation(
            db,
            PYTEST_DISPOSABLE_SQLALCHEMY_URI,
            testing=True,
            operation="drop_all",
        )


def test_chegada_aos_termos_apresenta_url_publica(ctx, app, monkeypatch, tmp_path):
    termo, url = _disponibilizar_termo(app, monkeypatch, tmp_path)
    _preparar(OnboardingCanal.ETAPA_CARGO, email="termo.url.lote3c@example.com")
    _evento, _rota, resultado = _interpretar("2", "wamid.TERMO.URL")
    assert resultado.codigo == conversa.CODIGO_TERMOS_APRESENTADOS
    assert resultado.etapa_atual == OnboardingCanal.ETAPA_TERMOS
    assert resultado.texto_resposta == (
        "Leia o Termo de Aceite:\n"
        f"{url}\n"
        "\n"
        "Após a leitura, para continuar, responda ACEITO."
    )
    assert "agentefrete.com.br" not in (resultado.texto_resposta or "")
    assert "1. ACEITO" not in (resultado.texto_resposta or "")
    jornada = _jornada()
    assert jornada.etapa == OnboardingCanal.ETAPA_TERMOS
    assert jornada.termos_apresentados_em is not None
    assert jornada.termos_referencia == f"terms-of-use:{termo.id}"
    assert jornada.termos_aceitos_em is None
    gravada = InterpretacaoConversacionalCanal.query.one()
    assert url in (gravada.texto_resposta or "")


def test_termo_indisponivel_nao_marca_nem_pede_aceite(ctx, app, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://homolog.exemplo.test")
    app.config["PUBLIC_BASE_URL"] = "https://homolog.exemplo.test"
    _preparar(OnboardingCanal.ETAPA_CARGO, email="termo.off.lote3c@example.com")
    _evento, _rota, resultado = _interpretar("2", "wamid.TERMO.OFF")
    assert resultado.codigo == conversa.CODIGO_ERRO_SEGURO
    assert resultado.etapa_atual == OnboardingCanal.ETAPA_TERMOS
    texto = resultado.texto_resposta or ""
    assert "ACEITO" not in texto
    assert "/termos-de-uso" not in texto
    jornada = _jornada()
    assert jornada.etapa == OnboardingCanal.ETAPA_TERMOS
    assert jornada.termos_apresentados_em is None
    assert jornada.termos_referencia is None
    assert jornada.termos_aceitos_em is None


def test_url_publica_invalida_nao_marca_termo(ctx, app, monkeypatch, tmp_path):
    _disponibilizar_termo(app, monkeypatch, tmp_path)
    monkeypatch.setenv("PUBLIC_BASE_URL", "ftp://homolog.exemplo.test/extra")
    app.config["PUBLIC_BASE_URL"] = "ftp://homolog.exemplo.test/extra"
    _preparar(OnboardingCanal.ETAPA_CARGO, email="termo.url.ruim.lote3c@example.com")
    _evento, _rota, resultado = _interpretar("2", "wamid.TERMO.URL.RUIM")
    assert resultado.codigo == conversa.CODIGO_ERRO_SEGURO
    assert "ACEITO" not in (resultado.texto_resposta or "")
    jornada = _jornada()
    assert jornada.termos_apresentados_em is None
    assert jornada.termos_aceitos_em is None
    assert jornada.etapa == OnboardingCanal.ETAPA_TERMOS


@pytest.mark.parametrize(
    ("texto", "avanca"),
    [
        ("ACEITO", True),
        ("aceito", True),
        ("  ACEITO  ", True),
        ("1", False),
        ("sim", False),
        ("aceito os termos", False),
    ],
)
def test_aceite_somente_palavra_aceito(ctx, app, texto, avanca):
    app.config["SECRET_KEY"] = SECRET
    _preparar(OnboardingCanal.ETAPA_TERMOS, email="aceite.lote3c@example.com")
    _evento, _rota, resultado = _interpretar(texto, "wamid.ACEITE")
    jornada = _jornada()
    if avanca:
        assert resultado.etapa_atual == OnboardingCanal.ETAPA_SENHA
        assert jornada.etapa == OnboardingCanal.ETAPA_SENHA
        assert jornada.termos_aceitos_em is not None
        return
    assert resultado.codigo == conversa.CODIGO_ACEITE_NAO_RECONHECIDO
    assert resultado.etapa_atual == OnboardingCanal.ETAPA_TERMOS
    assert "1. ACEITO" not in (resultado.texto_resposta or "")
    assert jornada.etapa == OnboardingCanal.ETAPA_TERMOS
    assert jornada.termos_aceitos_em is None


def test_jornada_antiga_terms_v1_reapresenta_antes_de_aceitar(
    ctx, app, monkeypatch, tmp_path
):
    app.config["SECRET_KEY"] = SECRET
    termo, url = _disponibilizar_termo(app, monkeypatch, tmp_path)
    ident = _preparar(OnboardingCanal.ETAPA_TERMOS, email="legado.lote3c@example.com")
    jornada = _jornada()
    jornada.termos_referencia = "terms-v1"
    db.session.commit()
    _evento, _rota, reapresentado = _interpretar("ACEITO", "wamid.LEGADO.1")
    db.session.refresh(jornada)
    assert reapresentado.codigo == conversa.CODIGO_TERMOS_APRESENTADOS
    assert reapresentado.etapa_atual == OnboardingCanal.ETAPA_TERMOS
    assert url in (reapresentado.texto_resposta or "")
    assert jornada.termos_aceitos_em is None
    assert jornada.termos_referencia == f"terms-of-use:{termo.id}"
    assert jornada.etapa == OnboardingCanal.ETAPA_TERMOS
    _evento, _rota, aceite = _interpretar("ACEITO", "wamid.LEGADO.2", sujeito=ident.sujeito_externo)
    db.session.refresh(jornada)
    assert aceite.etapa_atual == OnboardingCanal.ETAPA_SENHA
    assert jornada.termos_aceitos_em is not None


def test_get_do_documento_nao_registra_aceite(ctx, app, monkeypatch, tmp_path):
    _disponibilizar_termo(app, monkeypatch, tmp_path)
    _preparar(OnboardingCanal.ETAPA_TERMOS, email="get.termo.lote3c@example.com")
    jornada = _jornada()
    marca = jornada.atualizada_em
    assert jornada.termos_aceitos_em is None

    fonte = (ROOT / "app" / "web.py").read_text(encoding="utf-8")
    inicio = fonte.index("def terms_of_use(")
    fim = fonte.index("\n@app.route", inicio)
    corpo = fonte[inicio:fim]
    assert "termos_aceitos" not in corpo
    assert "registrar_resposta" not in corpo
    assert "OnboardingCanal" not in corpo

    from app.terms_services import get_active_term, get_terms_upload_dir

    @app.route("/termos-de-uso-teste-canal")
    def termos_de_uso_teste():
        ativo = get_active_term()
        assert ativo is not None
        return send_from_directory(
            get_terms_upload_dir(),
            ativo.filename,
            mimetype="application/pdf",
            as_attachment=False,
        )

    resposta = app.test_client().get("/termos-de-uso-teste-canal")
    assert resposta.status_code == 200
    assert resposta.mimetype == "application/pdf"
    db.session.refresh(jornada)
    assert jornada.termos_aceitos_em is None
    assert jornada.atualizada_em == marca
    assert jornada.etapa == OnboardingCanal.ETAPA_TERMOS


def test_nenhuma_cloud_api_e_chamada(ctx):
    _interpretar("quero me cadastrar", "wamid.SEM.API")
    fonte = _fonte()
    for trecho in (
        "graph.facebook",
        "messaging_product",
        "X-Hub-Signature",
        "httpx",
        "requests",
    ):
        assert trecho not in fonte
    webhook = (ROOT / "app" / "services" / "whatsapp_meta_webhook_service.py").read_text(
        encoding="utf-8"
    )
    assert "interpretar_mensagem_canal" not in webhook


def test_nenhuma_julia_ou_gemini_e_chamada(ctx):
    fonte = _fonte()
    for trecho in ("chat_julia", "gemini", "GenerativeModel", "run_julia", "copilot"):
        assert trecho not in fonte
    _preparar(OnboardingCanal.ETAPA_NOME)
    _interpretar("Maria Silva", "wamid.SEM.MODELO")
    assert _jornada().nome == "Maria Silva"


def test_nenhum_billing_ocorre(ctx):
    from app.models import CleitonBillingApropriacao, IaConsumoEvento, MonetizacaoFato

    _interpretar("quanto custa o frete?", "wamid.SEM.BILLING")
    _preparar(OnboardingCanal.ETAPA_NOME, sujeito="subjLote3c09")
    _interpretar("Maria Silva", "wamid.SEM.BILLING.2", sujeito="subjLote3c09")
    assert IaConsumoEvento.query.count() == 0
    assert CleitonBillingApropriacao.query.count() == 0
    assert MonetizacaoFato.query.count() == 0
    assert IdentidadeCanalExterna.query.filter_by(sujeito_externo=SUJEITO).one().interacoes_uteis == 0
    fonte = _fonte()
    for trecho in ("cleiton_monetizacao", "stripe", "IaConsumo", "registrar_interacao_guest"):
        assert trecho not in fonte


def test_rotas_fora_do_lote_nao_operam(ctx):
    conta, franquia = seed_conta_franquia_cliente("conta-fora-3c")
    user = seed_usuario(franquia.id, conta.id, email="fora.lote3c@example.com")
    ident = obter_ou_criar_identidade_externa(
        provedor=entrada.PROVEDOR_IDENTIDADE_WHATSAPP,
        sujeito_externo=SUJEITO,
        contexto_destino=DESTINO,
        commit=True,
    )
    user_id = user.id
    ident.estado = IdentidadeCanalExterna.ESTADO_VINCULADA
    ident.user_id = user_id
    ident.vinculada_em = utcnow_naive()
    db.session.commit()
    _evento, roteamento, resultado = _interpretar("quero me cadastrar", "wamid.VINCULO")
    assert roteamento.rota == entrada.ROTA_USUARIO_VINCULADO
    assert resultado.codigo == conversa.CODIGO_ROTA_NAO_OPERADA
    assert OnboardingCanal.query.count() == 0
    assert db.session.get(IdentidadeCanalExterna, ident.id).estado == (
        IdentidadeCanalExterna.ESTADO_VINCULADA
    )
    assert InterpretacaoConversacionalCanal.query.count() == 0


def test_migration_referencia_conclusao_sem_token():
    spec = importlib.util.spec_from_file_location(
        "mig_interpretacao_conclusao_ref",
        MIGRATION_REF,
    )
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    assert modulo.revision == "r9s0t1u2v3w4"
    assert modulo.down_revision == "q8r9s0t1u2v3"
    assert modulo._SQL_SEM_LINK == InterpretacaoConversacionalCanal._SQL_SEM_LINK
    texto = MIGRATION_REF.read_text(encoding="utf-8")
    assert "conclusao_id" in texto
    assert "onboarding_canal_conclusao" in texto
    assert "token_hash" not in texto
    assert "texto_usuario" not in texto
    assert "senha" not in texto.split("Não grava")[0]


def test_check_recusa_url_com_token(ctx):
    evento = _evento("oi", "wamid.CHECK.URL")
    row = InterpretacaoConversacionalCanal(
        evento_id=evento.id,
        codigo=conversa.CODIGO_LINK_EMITIDO,
        acao=conversa.ACAO_EMITIR_SENHA,
        texto_resposta="crie a senha em /onboarding/canal/concluir/segredo-bruto",
    )
    db.session.add(row)
    with pytest.raises(IntegrityError):
        db.session.flush()
    db.session.rollback()


def test_fonte_nao_interpola_segredo_na_resposta():
    fonte = _fonte()
    assert "{link}" not in fonte
    assert "TEXTO_SENHA.format" not in fonte
    assert "TEXTO_CONTA.format" not in fonte
    assert "emissao.token" not in fonte
    assert "emissao.url" not in fonte


def test_nova_conta_persiste_referencia_sem_token_e_replay_reusa(ctx, app, monkeypatch, caplog):
    app.config["SECRET_KEY"] = SECRET
    email = "segura.lote3c@example.com"
    _preparar(OnboardingCanal.ETAPA_TERMOS, email=email, nome="Lia Guest")
    capturado = _espionar(monkeypatch, "emitir_link_conclusao_onboarding")
    with caplog.at_level(logging.DEBUG):
        evento, roteamento, resultado = _interpretar("ACEITO", "wamid.SEGURA")
    token = capturado["token"]
    url = capturado["url"]
    assert resultado.codigo == conversa.CODIGO_LINK_EMITIDO
    assert resultado.acao == conversa.ACAO_ACEITAR_TERMOS
    assert resultado.etapa_atual == OnboardingCanal.ETAPA_SENHA
    assert resultado.texto_resposta == conversa.TEXTO_SENHA
    jornada = _jornada()
    assert jornada.etapa == OnboardingCanal.ETAPA_SENHA
    assert jornada.termos_aceitos_em is not None
    conclusao = OnboardingCanalConclusao.query.one()
    assert conclusao.finalidade == OnboardingCanalConclusao.FINALIDADE_DEFINIR_SENHA
    assert conclusao.estado == OnboardingCanalConclusao.ESTADO_EMITIDO
    assert resultado.conclusao_id == conclusao.id
    interpretacao = InterpretacaoConversacionalCanal.query.one()
    assert interpretacao.conclusao_id == conclusao.id
    assert interpretacao.conclusao.id == conclusao.id
    _assert_segredo_ausente(
        _texto_de(interpretacao),
        resultado.texto_resposta or "",
        _texto_de(db.session.get(EventoCanalRecebido, evento.id)),
        ConteudoTextualCanal.query.filter_by(evento_id=evento.id).one().texto,
        caplog.text,
        token=token,
        url=url,
    )
    assert email not in caplog.text
    assert email not in _texto_de(interpretacao)
    assert "Lia Guest" not in caplog.text
    assert "ACEITO" not in caplog.text
    assert conclusao.token_hash != token
    assert token not in conclusao.token_hash
    hash_antes = conclusao.token_hash
    marca = jornada.atualizada_em
    segundo = conversa.interpretar_mensagem_canal(roteamento)
    db.session.refresh(jornada)
    db.session.refresh(conclusao)
    assert capturado["n"] == 1
    assert segundo.codigo == conversa.CODIGO_EVENTO_JA_TRATADO
    assert segundo.texto_resposta == resultado.texto_resposta
    assert segundo.etapa_atual == OnboardingCanal.ETAPA_SENHA
    assert segundo.conclusao_id == conclusao.id
    assert OnboardingCanalConclusao.query.count() == 1
    assert conclusao.token_hash == hash_antes
    assert conclusao.estado == OnboardingCanalConclusao.ESTADO_EMITIDO
    assert jornada.etapa == OnboardingCanal.ETAPA_SENHA
    assert jornada.atualizada_em == marca
    assert InterpretacaoConversacionalCanal.query.count() == 1
    _assert_segredo_ausente(segundo.texto_resposta or "", token=token, url=url)
    outra = _evento("ok", "wamid.SEGURA.OUTRA")
    roteamento_outro = entrada.processar_evento_canal_recebido(outra.id)
    terceiro = conversa.interpretar_mensagem_canal(roteamento_outro)
    db.session.refresh(conclusao)
    assert capturado["n"] == 1
    assert OnboardingCanalConclusao.query.count() == 1
    assert conclusao.token_hash == hash_antes
    assert terceiro.conclusao_id == conclusao.id
    assert terceiro.etapa_atual == OnboardingCanal.ETAPA_SENHA
    assert terceiro.texto_resposta == conversa.TEXTO_SENHA_JA_EMITIDO
    assert _jornada().etapa == OnboardingCanal.ETAPA_SENHA


def test_conta_existente_nao_persiste_token_de_vinculo(ctx, app, monkeypatch, caplog):
    app.config["SECRET_KEY"] = SECRET
    email = "existe.segura.lote3c@example.com"
    conta, franquia = seed_conta_franquia_cliente("conta-segura-3c")
    seed_usuario(franquia.id, conta.id, email=email)
    antes = User.query.count()
    _preparar(OnboardingCanal.ETAPA_EMAIL, email="outro.segura.lote3c@example.com", nome="Lia Guest")
    capturado = _espionar(monkeypatch, "emitir_link_vinculo_conta_existente")
    senha = _espionar(monkeypatch, "emitir_link_conclusao_onboarding")
    with caplog.at_level(logging.DEBUG):
        evento, roteamento, resultado = _interpretar(email, "wamid.VINCULO.SEG")
    token = capturado["token"]
    url = capturado["url"]
    assert senha["n"] == 0
    assert capturado["n"] == 1
    assert resultado.codigo == "existing_account_verification_required"
    assert resultado.acao == conversa.ACAO_ORIENTAR_CONTA
    assert resultado.texto_resposta == conversa.TEXTO_CONTA
    assert User.query.count() == antes
    jornada = _jornada()
    assert jornada.etapa == OnboardingCanal.ETAPA_EMAIL
    conclusao = OnboardingCanalConclusao.query.one()
    assert conclusao.finalidade == OnboardingCanalConclusao.FINALIDADE_VINCULAR_CONTA
    assert resultado.conclusao_id == conclusao.id
    interpretacao = InterpretacaoConversacionalCanal.query.one()
    assert interpretacao.conclusao_id == conclusao.id
    _assert_segredo_ausente(
        _texto_de(interpretacao),
        resultado.texto_resposta or "",
        _texto_de(db.session.get(EventoCanalRecebido, evento.id)),
        ConteudoTextualCanal.query.filter_by(evento_id=evento.id).one().texto,
        caplog.text,
        token=token,
        url=url,
    )
    assert email not in caplog.text
    assert email not in _texto_de(interpretacao)
    assert "Lia Guest" not in caplog.text
    hash_antes = conclusao.token_hash
    marca = jornada.atualizada_em
    segundo = conversa.interpretar_mensagem_canal(roteamento)
    db.session.refresh(jornada)
    db.session.refresh(conclusao)
    assert capturado["n"] == 1
    assert senha["n"] == 0
    assert segundo.codigo == conversa.CODIGO_EVENTO_JA_TRATADO
    assert segundo.conclusao_id == conclusao.id
    assert segundo.texto_resposta == conversa.TEXTO_CONTA
    assert OnboardingCanalConclusao.query.count() == 1
    assert conclusao.token_hash == hash_antes
    assert jornada.etapa == OnboardingCanal.ETAPA_EMAIL
    assert jornada.atualizada_em == marca
    assert User.query.count() == antes
    _assert_segredo_ausente(segundo.texto_resposta or "", token=token, url=url)


def test_etapas_anteriores_nao_criam_conclusao(ctx):
    _preparar(OnboardingCanal.ETAPA_NOME)
    _evento, _rota, resultado = _interpretar("Maria Silva", "wamid.SEM.CONCLUSAO")
    assert resultado.codigo == conversa.CODIGO_RESPOSTA_REGISTRADA
    assert resultado.etapa_atual == OnboardingCanal.ETAPA_EMAIL
    assert resultado.conclusao_id is None
    assert resultado.texto_resposta == conversa.TEXTO_EMAIL
    assert OnboardingCanalConclusao.query.count() == 0
    interpretacao = InterpretacaoConversacionalCanal.query.one()
    assert interpretacao.conclusao_id is None
    assert _MARCA_LINK not in (interpretacao.texto_resposta or "")
