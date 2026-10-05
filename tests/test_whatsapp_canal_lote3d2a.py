"""Lote WhatsApp 3D2-A: entrega do link de conclusão no momento do envio.

O token nasce na entrega, segue só até o POST e não é gravado.
Não cobre retry, recibo de entrega, modelo, cobrança nem Growth.
"""
from __future__ import annotations

import hashlib
import importlib.util
import logging
import threading
from pathlib import Path

import pytest
import requests
from flask import Flask
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import text
from sqlalchemy.pool import StaticPool

from app.db_operational_safety import run_test_schema_operation
from app.extensions import db
from app.models import (
    CleitonBillingApropriacao,
    ConteudoTextualCanal,
    EventoCanalRecebido,
    EventoCanalSaida,
    IaConsumoEvento,
    IdentidadeCanalExterna,
    InterpretacaoConversacionalCanal,
    MonetizacaoFato,
    OnboardingCanal,
    OnboardingCanalConclusao,
    utcnow_naive,
)
from app.services import canal_saida_link_seguro_service as entrega
from app.services import canal_saida_whatsapp_service as saida_texto
from app.services import onboarding_canal_conclusao_service as conclusao
from app.services import whatsapp_meta_cloud_api_adapter as adapter
from app.services import whatsapp_meta_config as config
from app.services.canal_aquisicao_service import (
    PROVEDOR_WHATSAPP_META,
    iniciar_onboarding_canal,
    obter_ou_criar_identidade_externa,
    registrar_resposta_onboarding,
)
from app.services.canal_interpretacao_conversacional_service import (
    ACAO_EMITIR_SENHA,
    ACAO_ORIENTAR_CONTA,
    CODIGO_CONTA_EXISTENTE,
    CODIGO_LINK_EMITIDO,
    TEXTO_CONTA,
    TEXTO_SENHA,
)
from app.services.onboarding_canal_conclusao_service import (
    inspecionar_alias_conclusao,
    inspecionar_link_conclusao,
)
from tests.conftest import (
    PYTEST_DISPOSABLE_SQLALCHEMY_URI,
    seed_conta_franquia_cliente,
    seed_usuario,
)

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "versions" / "t1u2v3w4x5y6_evento_canal_saida_link_seguro.py"
SECRET = "teste-entrega-link-3d2a"
TOKEN_META = "EAAWHATSAPP3D2ATOKENSECRETO"
VERSAO = "v26.0"
PHONE_NUMBER_ID = "106540352242922"
DESTINATARIO = "16505551234"
MESSAGE_ID = "wamid.HBgLENTREGA3D2A"
CORRELATION = "corr3d2a01"
EMAIL_NOVA = "nova.conta.3d2a@example.com"
EMAIL_EXISTENTE = "existente.3d2a@example.com"
BASE_PUBLICA = "https://entrega.invalid"
FRASE_SENHA = (
    "Seu cadastro está quase pronto. Por segurança, crie sua senha neste link:\n"
)
FRASE_CONTA = (
    "Este e-mail já tem conta. Entre e confirme a conexão neste link: "
)


def _migration_module():
    spec = importlib.util.spec_from_file_location("mig_saida_link_seguro", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _configurar(monkeypatch, *, token=TOKEN_META, versao=VERSAO, timeout="4"):
    monkeypatch.setenv(config.ENV_ACCESS_TOKEN, token)
    monkeypatch.setenv(config.ENV_GRAPH_API_VERSION, versao)
    monkeypatch.setenv(config.ENV_SEND_TIMEOUT_SECONDS, timeout)
    monkeypatch.delenv(config.ENV_GRAPH_BASE_URL, raising=False)
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE_PUBLICA)


def _texto_de(row) -> str:
    return "\n".join(str(getattr(row, coluna.name) or "") for coluna in row.__table__.columns)


def _banco() -> str:
    partes = []
    for modelo in (
        EventoCanalSaida,
        InterpretacaoConversacionalCanal,
        EventoCanalRecebido,
        ConteudoTextualCanal,
        OnboardingCanal,
        OnboardingCanalConclusao,
    ):
        for row in modelo.query.all():
            partes.append(_texto_de(row))
    return "\n".join(partes)


class _Resposta:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = "CORPO_BRUTO_META_3D2A"

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def close(self):
        return None


def _mock(monkeypatch, efeito, *, ao_chamar=None):
    chamadas = []

    def _post(url, **kwargs):
        chamadas.append((url, kwargs))
        if ao_chamar is not None:
            ao_chamar()
        if isinstance(efeito, Exception):
            raise efeito
        return efeito

    monkeypatch.setattr(adapter.requests, "post", _post)
    return chamadas


def _ok():
    return _Resposta(
        200,
        {
            "messaging_product": "whatsapp",
            "contacts": [{"input": DESTINATARIO, "wa_id": DESTINATARIO}],
            "messages": [{"id": MESSAGE_ID}],
        },
    )


def _identidade() -> IdentidadeCanalExterna:
    return obter_ou_criar_identidade_externa(
        provedor=PROVEDOR_WHATSAPP_META,
        sujeito_externo=DESTINATARIO,
        contexto_destino=PHONE_NUMBER_ID,
        commit=True,
    )


def _ate_senha(email: str) -> IdentidadeCanalExterna:
    ident = _identidade()
    iniciar_onboarding_canal(ident.id, commit=True)
    registrar_resposta_onboarding(ident.id, campo="aceitar_convite", commit=True)
    registrar_resposta_onboarding(ident.id, campo="nome", valor="Maria Guest", commit=True)
    registrar_resposta_onboarding(ident.id, campo="email", valor=email, commit=True)
    registrar_resposta_onboarding(ident.id, campo="job_role", valor="analista", commit=True)
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
    return ident


def _ate_email_existente(email: str) -> IdentidadeCanalExterna:
    ident = _identidade()
    iniciar_onboarding_canal(ident.id, commit=True)
    registrar_resposta_onboarding(ident.id, campo="aceitar_convite", commit=True)
    registrar_resposta_onboarding(ident.id, campo="nome", valor="Paulo Guest", commit=True)
    estado = registrar_resposta_onboarding(ident.id, campo="email", valor=email, commit=True)
    assert estado.codigo == CODIGO_CONTA_EXISTENTE
    assert estado.etapa_atual == OnboardingCanal.ETAPA_EMAIL
    return ident


def _evento(interpretacao: InterpretacaoConversacionalCanal) -> None:
    db.session.add(
        ConteudoTextualCanal(
            evento_id=interpretacao.evento_id,
            texto="quero continuar o cadastro",
        )
    )
    db.session.commit()


def _interpretacao(
    jornada: OnboardingCanal,
    antiga: OnboardingCanalConclusao,
    *,
    codigo: str,
    acao: str,
    etapa_final: str,
    texto: str,
) -> InterpretacaoConversacionalCanal:
    evento = EventoCanalRecebido(
        provider=EventoCanalRecebido.PROVIDER_META_WHATSAPP,
        evento_externo_id="wamid.ENTRADA.3D2A",
        tipo_evento=EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL,
        sujeito_externo=DESTINATARIO,
        contexto_destino=PHONE_NUMBER_ID,
        recebido_em=utcnow_naive(),
        status_processamento=EventoCanalRecebido.STATUS_ROTEADO,
        correlation_id=CORRELATION,
        diagnostico_seguro="mensagem_textual",
    )
    db.session.add(evento)
    db.session.flush()
    row = InterpretacaoConversacionalCanal(
        evento_id=evento.id,
        codigo=codigo,
        acao=acao,
        onboarding_id=jornada.id,
        etapa_final=etapa_final,
        texto_resposta=texto,
        conclusao_id=antiga.id,
    )
    db.session.add(row)
    db.session.commit()
    _evento(row)
    return row


def _saida_aguardando(interpretacao: InterpretacaoConversacionalCanal) -> EventoCanalSaida:
    resultado = saida_texto.enviar_texto_da_interpretacao(interpretacao.id)
    assert resultado.status_envio == EventoCanalSaida.STATUS_AGUARDANDO_LINK
    assert EventoCanalSaida.query.count() == 1
    row = EventoCanalSaida.query.one()
    assert row.conclusao_id is None
    return row


def _preparar_nova(app) -> tuple[OnboardingCanalConclusao, str, EventoCanalSaida]:
    app.config["SECRET_KEY"] = SECRET
    ident = _ate_senha(EMAIL_NOVA)
    emissao = conclusao.emitir_link_conclusao_onboarding(
        ident.id,
        secret_key=SECRET,
        build_url=lambda token: f"/onboarding/canal/concluir/{token}",
    )
    assert emissao.codigo == conclusao.CODIGO_LINK_EMITIDO
    antiga = OnboardingCanalConclusao.query.one()
    jornada = OnboardingCanal.query.one()
    interpretacao = _interpretacao(
        jornada,
        antiga,
        codigo=CODIGO_LINK_EMITIDO,
        acao=ACAO_EMITIR_SENHA,
        etapa_final=OnboardingCanal.ETAPA_SENHA,
        texto=TEXTO_SENHA,
    )
    return antiga, emissao.token, _saida_aguardando(interpretacao)


def _preparar_existente(app) -> tuple[OnboardingCanalConclusao, str, EventoCanalSaida]:
    app.config["SECRET_KEY"] = SECRET
    conta, franquia = seed_conta_franquia_cliente("conta-3d2a")
    seed_usuario(franquia.id, conta.id, email=EMAIL_EXISTENTE)
    ident = _ate_email_existente(EMAIL_EXISTENTE)
    emissao = conclusao.emitir_link_vinculo_conta_existente(
        ident.id,
        secret_key=SECRET,
        build_url=lambda token: f"/onboarding/canal/concluir/{token}",
    )
    assert emissao.codigo == conclusao.CODIGO_LINK_EMITIDO
    antiga = OnboardingCanalConclusao.query.one()
    jornada = OnboardingCanal.query.one()
    interpretacao = _interpretacao(
        jornada,
        antiga,
        codigo=CODIGO_CONTA_EXISTENTE,
        acao=ACAO_ORIENTAR_CONTA,
        etapa_final=OnboardingCanal.ETAPA_EMAIL,
        texto=TEXTO_CONTA,
    )
    return antiga, emissao.token, _saida_aguardando(interpretacao)


def _payload(token: str) -> dict:
    return URLSafeTimedSerializer(
        SECRET,
        salt=conclusao.SALT_CONCLUSAO_ONBOARDING,
    ).loads(token)


def _cancelar(jornada: OnboardingCanal) -> None:
    jornada.etapa = OnboardingCanal.ETAPA_CANCELADO
    jornada.cancelada_em = utcnow_naive()
    db.session.commit()


def _abrir_j2(origem: OnboardingCanal, etapa: str, *, soltar_indice: bool) -> OnboardingCanal:
    dados = {
        "identidade_id": int(origem.identidade_id),
        "nome": origem.nome,
        "email_normalizado": origem.email_normalizado,
        "job_role": origem.job_role,
        "respostas": origem.respostas_entrevista_json,
        "termos_apresentados_em": origem.termos_apresentados_em,
        "termos_referencia": origem.termos_referencia,
        "termos_aceitos_em": origem.termos_aceitos_em,
        "taxonomia_versao": origem.taxonomia_versao,
        "expira_em": origem.expira_em,
    }
    if soltar_indice:
        db.session.execute(text("DROP INDEX IF EXISTS uq_onboarding_canal_jornada_aberta"))
        db.session.commit()
    agora = utcnow_naive()
    senha = etapa == OnboardingCanal.ETAPA_SENHA
    clone = OnboardingCanal(
        identidade_id=dados["identidade_id"],
        etapa=etapa,
        nome=dados["nome"],
        email_normalizado=dados["email_normalizado"],
        job_role=dados["job_role"] if senha else None,
        respostas_entrevista_json=dados["respostas"] if senha else None,
        termos_apresentados_em=dados["termos_apresentados_em"] if senha else None,
        termos_referencia=dados["termos_referencia"] if senha else None,
        termos_aceitos_em=dados["termos_aceitos_em"] if senha else None,
        pausas_interacao_guest=0,
        taxonomia_versao=dados["taxonomia_versao"],
        iniciada_em=agora,
        atualizada_em=agora,
        expira_em=dados["expira_em"],
    )
    db.session.add(clone)
    db.session.commit()
    return clone


def _isca(onboarding_id: int, finalidade: str, marca: str) -> OnboardingCanalConclusao:
    agora = utcnow_naive()
    row = OnboardingCanalConclusao(
        onboarding_id=onboarding_id,
        finalidade=finalidade,
        token_hash=hashlib.sha256(marca.encode("utf-8")).hexdigest(),
        estado=OnboardingCanalConclusao.ESTADO_EMITIDO,
        emitido_em=agora,
        expira_em=agora,
        atualizada_em=agora,
    )
    db.session.add(row)
    db.session.commit()
    return row


def _jornada_aberta_mais_recente(identidade_id: int) -> OnboardingCanal:
    return (
        OnboardingCanal.query.filter_by(identidade_id=identidade_id)
        .filter(OnboardingCanal.etapa.in_(OnboardingCanal.ETAPAS_ABERTAS))
        .order_by(OnboardingCanal.id.desc())
        .first()
    )


def _link_do_body(chamadas) -> str:
    corpo = chamadas[0][1]["json"]["text"]["body"]
    ultima = corpo.strip().rsplit("\n", 1)[-1].strip()
    if ultima.startswith("https://") or ultima.startswith("http://"):
        return ultima
    return corpo.rsplit(": ", 1)[1]


def _alias_do_link(link: str) -> str:
    prefixo = f"{BASE_PUBLICA}/c/"
    assert link.startswith(prefixo)
    alias = link[len(prefixo):]
    assert alias and "/" not in alias
    return alias


def _token_do_link(link: str) -> str:
    return _alias_do_link(link)


def _assert_alias(alias: str, row: OnboardingCanalConclusao) -> None:
    assert row.alias_hash == hashlib.sha256(alias.encode("utf-8")).hexdigest()
    assert row.token_hash != row.alias_hash
    assert alias not in (row.token_hash or "")
    assert alias not in (row.alias_hash or "")
    with pytest.raises(Exception):
        _payload(alias)


def _proibir(token: str, link: str, *textos: str) -> None:
    for texto in textos:
        assert token not in texto
        assert link not in texto
        assert "/onboarding/canal/concluir/" not in texto


def _assert_sem_segredo(token: str, link: str, caplog) -> None:
    _proibir(token, link, _banco(), caplog.text)
    assert TOKEN_META not in caplog.text
    assert "Bearer" not in caplog.text


def test_migration_referencia_conclusao_e_preparando_link():
    modulo = _migration_module()
    assert modulo.revision == "t1u2v3w4x5y6"
    assert modulo.down_revision == "s0t1u2v3w4x5"
    assert modulo._SQL_STATUS == EventoCanalSaida._SQL_STATUS
    assert modulo._SQL_COERENCIA == EventoCanalSaida._SQL_COERENCIA
    assert "preparando_link" in modulo._SQL_STATUS
    assert "preparando_link" not in modulo._SQL_STATUS_LOTE_3D1
    texto = MIGRATION.read_text(encoding="utf-8")
    assert "conclusao_id" in texto
    assert "token_hash" not in texto
    assert "texto_resposta" not in texto
    assert 'sa.Column("url"' not in texto
    assert EventoCanalSaida.STATUS_PREPARANDO_LINK in EventoCanalSaida.STATUS_ENVIO
    assert "conclusao_id" in {coluna.name for coluna in EventoCanalSaida.__table__.columns}


def test_nova_conta_emite_conclusao_enviada_sem_gravar_segredo(ctx, app, monkeypatch, caplog):
    _configurar(monkeypatch)
    observado = {}

    def _ao_chamar():
        observado["aberta"] = db.session().in_transaction()
        row = EventoCanalSaida.query.one()
        observado["status"] = row.status_envio
        observado["conclusao_id"] = row.conclusao_id

    chamadas = _mock(monkeypatch, _ok(), ao_chamar=_ao_chamar)
    antiga, token_antigo, saida = _preparar_nova(app)
    hash_antigo = antiga.token_hash
    assert chamadas == []
    with caplog.at_level(logging.DEBUG):
        resultado = entrega.entregar_link_seguro(saida.id)
    assert observado["aberta"] is False
    assert observado["status"] == EventoCanalSaida.STATUS_PREPARANDO_LINK
    assert observado["conclusao_id"] is not None
    assert len(chamadas) == 1
    url, kwargs = chamadas[0]
    assert url == f"https://graph.facebook.com/{VERSAO}/{PHONE_NUMBER_ID}/messages"
    assert kwargs["timeout"] == 4.0
    assert kwargs["headers"]["Authorization"] == f"Bearer {TOKEN_META}"
    corpo = kwargs["json"]["text"]["body"]
    link = _link_do_body(chamadas)
    token = _token_do_link(link)
    assert corpo == FRASE_SENHA + link
    assert token != token_antigo
    assert resultado.status_envio == EventoCanalSaida.STATUS_ACEITO
    assert resultado.provider_message_id == MESSAGE_ID
    assert resultado.conclusao_id != antiga.id
    row = db.session.get(EventoCanalSaida, saida.id)
    assert row.status_envio == EventoCanalSaida.STATUS_ACEITO
    assert row.provider_message_id == MESSAGE_ID
    assert row.enviado_em is not None
    assert row.codigo_erro is None
    assert row.conclusao_id == resultado.conclusao_id
    assert EventoCanalSaida.query.count() == 1
    nova = db.session.get(OnboardingCanalConclusao, row.conclusao_id)
    assert nova.finalidade == OnboardingCanalConclusao.FINALIDADE_DEFINIR_SENHA
    assert nova.estado == OnboardingCanalConclusao.ESTADO_EMITIDO
    _assert_alias(token, nova)
    assert nova.token_hash != hash_antigo
    db.session.refresh(antiga)
    assert antiga.estado == OnboardingCanalConclusao.ESTADO_REVOGADO
    assert antiga.token_hash != hash_antigo
    interpretacao = InterpretacaoConversacionalCanal.query.one()
    assert nova.onboarding_id == interpretacao.onboarding_id
    assert interpretacao.conclusao_id == antiga.id
    assert interpretacao.texto_resposta == TEXTO_SENHA
    assert inspecionar_link_conclusao(token_antigo, secret_key=SECRET).codigo == (
        conclusao.CODIGO_TOKEN_REVOGADO
    )
    assert inspecionar_alias_conclusao(token).codigo == conclusao.CODIGO_LINK_EMITIDO
    _assert_sem_segredo(token, link, caplog)
    _proibir(token, link, repr(resultado), _texto_de(row))
    assert IaConsumoEvento.query.count() == 0
    assert CleitonBillingApropriacao.query.count() == 0
    assert MonetizacaoFato.query.count() == 0
    assert IdentidadeCanalExterna.query.one().interacoes_uteis == 0
    fonte = Path(entrega.__file__).read_text(encoding="utf-8")
    for trecho in (
        "chat_julia",
        "gemini",
        "GenerativeModel",
        "cleiton_monetizacao",
        "stripe",
        "IaConsumo",
        "graph.facebook",
        "import requests",
    ):
        assert trecho not in fonte


def test_conta_existente_emite_vinculo(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, _ok())
    antiga, token_antigo, saida = _preparar_existente(app)
    registro = []
    jornadas = []
    jornada_id = OnboardingCanal.query.one().id
    original = entrega.emitir_link_vinculo_conta_existente

    def _vinculo(*args, **kwargs):
        registro.append(kwargs.get("commit"))
        jornadas.append(kwargs.get("onboarding_id"))
        return original(*args, **kwargs)

    monkeypatch.setattr(entrega, "emitir_link_vinculo_conta_existente", _vinculo)
    monkeypatch.setattr(
        entrega,
        "emitir_link_conclusao_onboarding",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("senha emitida")),
    )
    resultado = entrega.entregar_link_seguro(saida.id)
    assert registro == [False]
    assert jornadas == [jornada_id]
    link = _link_do_body(chamadas)
    token = _token_do_link(link)
    assert chamadas[0][1]["json"]["text"]["body"] == FRASE_CONTA + link
    assert token != token_antigo
    row = db.session.get(EventoCanalSaida, saida.id)
    nova = db.session.get(OnboardingCanalConclusao, row.conclusao_id)
    assert nova.finalidade == OnboardingCanalConclusao.FINALIDADE_VINCULAR_CONTA
    assert nova.estado == OnboardingCanalConclusao.ESTADO_EMITIDO
    assert nova.onboarding_id == jornada_id
    assert resultado.conclusao_id == nova.id
    _assert_alias(token, nova)
    db.session.refresh(antiga)
    assert antiga.estado == OnboardingCanalConclusao.ESTADO_REVOGADO
    assert InterpretacaoConversacionalCanal.query.one().conclusao_id == antiga.id
    assert InterpretacaoConversacionalCanal.query.one().texto_resposta == TEXTO_CONTA
    assert token not in _banco()
    assert link not in _banco()


def test_replay_de_preparando_link_nao_reenvia(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, _ok())
    antiga, _token, saida = _preparar_nova(app)
    saida.status_envio = EventoCanalSaida.STATUS_PREPARANDO_LINK
    saida.conclusao_id = antiga.id
    db.session.commit()
    antes = OnboardingCanalConclusao.query.count()
    primeiro = entrega.entregar_link_seguro(saida.id)
    segundo = entrega.entregar_link_seguro(saida.id)
    assert chamadas == []
    assert primeiro.status_envio == EventoCanalSaida.STATUS_PREPARANDO_LINK
    assert segundo.status_envio == EventoCanalSaida.STATUS_PREPARANDO_LINK
    assert primeiro.provider_message_id is None
    assert segundo.conclusao_id == antiga.id
    db.session.refresh(antiga)
    assert antiga.estado == OnboardingCanalConclusao.ESTADO_EMITIDO
    assert OnboardingCanalConclusao.query.count() == antes
    assert EventoCanalSaida.query.one().status_envio == EventoCanalSaida.STATUS_PREPARANDO_LINK


def test_configuracao_invalida_nao_emite_nem_muda_saida(ctx, app, monkeypatch):
    _configurar(monkeypatch, token="")
    chamadas = _mock(monkeypatch, _ok())
    antiga, _token, saida = _preparar_nova(app)
    hash_antes = antiga.token_hash
    resultado = entrega.entregar_link_seguro(saida.id)
    assert resultado.codigo == EventoCanalSaida.CODIGO_CONFIGURACAO_AUSENTE
    assert chamadas == []
    row = db.session.get(EventoCanalSaida, saida.id)
    assert row.status_envio == EventoCanalSaida.STATUS_AGUARDANDO_LINK
    assert row.conclusao_id is None
    assert row.codigo_erro is None
    db.session.refresh(antiga)
    assert antiga.estado == OnboardingCanalConclusao.ESTADO_EMITIDO
    assert antiga.token_hash == hash_antes
    assert OnboardingCanalConclusao.query.count() == 1
    monkeypatch.setenv(config.ENV_ACCESS_TOKEN, TOKEN_META)
    corrigido = entrega.entregar_link_seguro(saida.id)
    assert len(chamadas) == 1
    assert corrigido.status_envio == EventoCanalSaida.STATUS_ACEITO
    assert corrigido.provider_message_id == MESSAGE_ID
    assert corrigido.conclusao_id != antiga.id


def _assert_falha_sem_retry(app, monkeypatch, efeito, codigo: str) -> None:
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, efeito)
    antiga, token_antigo, saida = _preparar_nova(app)
    primeiro = entrega.entregar_link_seguro(saida.id)
    assert len(chamadas) == 1
    link = _link_do_body(chamadas)
    token = _token_do_link(link)
    assert token != token_antigo
    assert primeiro.codigo_erro == codigo
    assert primeiro.status_envio == EventoCanalSaida.STATUS_ERRO
    assert primeiro.provider_message_id is None
    row = db.session.get(EventoCanalSaida, saida.id)
    assert row.codigo_erro == codigo
    assert row.provider_message_id is None
    assert row.enviado_em is None
    nova = db.session.get(OnboardingCanalConclusao, row.conclusao_id)
    assert nova.estado == OnboardingCanalConclusao.ESTADO_EMITIDO
    assert nova.id != antiga.id
    _assert_alias(token, nova)
    db.session.refresh(antiga)
    assert antiga.estado == OnboardingCanalConclusao.ESTADO_REVOGADO
    segundo = entrega.entregar_link_seguro(saida.id)
    assert len(chamadas) == 1
    assert segundo.codigo_erro == codigo
    assert segundo.provider_message_id is None
    assert OnboardingCanalConclusao.query.count() == 2
    assert EventoCanalSaida.query.count() == 1
    assert token not in _banco()
    assert link not in _banco()
    assert "CORPO_BRUTO_META_3D2A" not in _banco()


def test_timeout_nao_gera_retry(ctx, app, monkeypatch):
    _assert_falha_sem_retry(app, monkeypatch, requests.Timeout("estourou"), "timeout")


def test_http_4xx_nao_gera_retry(ctx, app, monkeypatch):
    _assert_falha_sem_retry(app, monkeypatch, _Resposta(400, {"error": "segredo-4xx"}), "http_4xx")


def test_http_5xx_nao_gera_retry(ctx, app, monkeypatch):
    _assert_falha_sem_retry(app, monkeypatch, _Resposta(503, {"error": "segredo-5xx"}), "http_5xx")


def test_resposta_invalida_nao_gera_retry(ctx, app, monkeypatch):
    efeito = _Resposta(200, {"messages": [], "id": "wamid.FORA"})
    _assert_falha_sem_retry(app, monkeypatch, efeito, "resposta_invalida")
    assert "wamid.FORA" not in _banco()


def test_chamadas_concorrentes_fazem_um_unico_post(monkeypatch):
    flask_app = Flask("corrida-link-3d2a")
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = PYTEST_DISPOSABLE_SQLALCHEMY_URI
    flask_app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "poolclass": StaticPool,
        "connect_args": {"check_same_thread": False},
    }
    flask_app.config["TESTING"] = True
    flask_app.config["SECRET_KEY"] = SECRET
    db.init_app(flask_app)
    with flask_app.app_context():
        import app.models  # noqa: F401

        run_test_schema_operation(
            db,
            PYTEST_DISPOSABLE_SQLALCHEMY_URI,
            testing=True,
            operation="create_all",
        )
        _configurar(monkeypatch)
        chamadas = _mock(monkeypatch, _ok())
        _antiga, _token, saida = _preparar_nova(flask_app)
        assert chamadas == []
        saida_id = int(saida.id)
        db.session.remove()
        barreira = threading.Barrier(2)
        resultados: dict[int, object] = {}

        def _worker(indice: int) -> None:
            try:
                with flask_app.app_context():
                    barreira.wait(timeout=5)
                    resultados[indice] = entrega.entregar_link_seguro(saida_id)
                    db.session.remove()
            except Exception as exc:
                resultados[indice] = exc

        threads = [threading.Thread(target=_worker, args=(indice,)) for indice in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        assert set(resultados) == {1, 2}
        assert all(isinstance(item, entrega.ResultadoSaidaCanal) for item in resultados.values())
        assert len(chamadas) == 1
        assert OnboardingCanalConclusao.query.filter_by(
            estado=OnboardingCanalConclusao.ESTADO_EMITIDO
        ).count() == 1
        row = db.session.get(EventoCanalSaida, saida_id)
        assert row.status_envio == EventoCanalSaida.STATUS_ACEITO
        assert row.provider_message_id == MESSAGE_ID
        assert EventoCanalSaida.query.count() == 1
        link = _link_do_body(chamadas)
        token = _token_do_link(link)
        assert token not in _banco()
        db.session.remove()
        run_test_schema_operation(
            db,
            PYTEST_DISPOSABLE_SQLALCHEMY_URI,
            testing=True,
            operation="drop_all",
        )


def test_nenhuma_julia_gemini_ou_billing(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    _mock(monkeypatch, _ok())
    _preparar_nova(app)
    saida = EventoCanalSaida.query.one()
    entrega.entregar_link_seguro(saida.id)
    assert IaConsumoEvento.query.count() == 0
    assert CleitonBillingApropriacao.query.count() == 0
    assert MonetizacaoFato.query.count() == 0
    fonte = Path(entrega.__file__).read_text(encoding="utf-8")
    for trecho in ("chat_julia", "gemini", "GenerativeModel", "run_julia", "copilot"):
        assert trecho not in fonte
    for trecho in ("cleiton_monetizacao", "stripe", "IaConsumo", "registrar_interacao_guest"):
        assert trecho not in fonte


def _espionar_emissao(monkeypatch, nome: str) -> list:
    original = getattr(entrega, nome)
    chamadas = []

    def _wrap(*args, **kwargs):
        chamadas.append(kwargs.get("onboarding_id"))
        return original(*args, **kwargs)

    monkeypatch.setattr(entrega, nome, _wrap)
    return chamadas


def test_j1_elegivel_nao_emite_token_da_j2(ctx, app, monkeypatch, caplog):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, _ok())
    antiga, token_antigo, saida = _preparar_nova(app)
    j1_id = int(OnboardingCanal.query.one().id)
    j1 = db.session.get(OnboardingCanal, j1_id)
    j2 = _abrir_j2(j1, OnboardingCanal.ETAPA_SENHA, soltar_indice=True)
    assert _jornada_aberta_mais_recente(j1.identidade_id).id == j2.id
    assert j2.id > j1_id
    isca = _isca(j2.id, OnboardingCanalConclusao.FINALIDADE_DEFINIR_SENHA, "isca-j2-senha")
    hash_isca = isca.token_hash
    emissoes = _espionar_emissao(monkeypatch, "emitir_link_conclusao_onboarding")
    with caplog.at_level(logging.DEBUG):
        resultado = entrega.entregar_link_seguro(saida.id)
    link = _link_do_body(chamadas)
    token = _token_do_link(link)
    row = db.session.get(EventoCanalSaida, saida.id)
    nova = db.session.get(OnboardingCanalConclusao, row.conclusao_id)
    assert emissoes == [j1_id]
    assert resultado.conclusao_id == nova.id
    assert nova.onboarding_id == j1_id
    assert nova.id != antiga.id
    assert nova.id != isca.id
    _assert_alias(token, nova)
    assert OnboardingCanalConclusao.query.filter_by(onboarding_id=j2.id).one().id == isca.id
    db.session.refresh(isca)
    assert isca.estado == OnboardingCanalConclusao.ESTADO_EMITIDO
    assert isca.token_hash == hash_isca
    interpretacao = InterpretacaoConversacionalCanal.query.one()
    assert interpretacao.onboarding_id == j1_id
    assert interpretacao.conclusao_id == antiga.id
    assert token != token_antigo
    _assert_sem_segredo(token, link, caplog)


def test_j1_inelegivel_nao_cai_para_j2(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, _ok())
    antiga, _token, saida = _preparar_nova(app)
    hash_antes = antiga.token_hash
    j1 = OnboardingCanal.query.one()
    j1_id = int(j1.id)
    _cancelar(j1)
    j1 = db.session.get(OnboardingCanal, j1_id)
    j2 = _abrir_j2(j1, OnboardingCanal.ETAPA_SENHA, soltar_indice=False)
    assert j1.etapa == OnboardingCanal.ETAPA_CANCELADO
    assert _jornada_aberta_mais_recente(j1.identidade_id).id == j2.id
    assert j2.etapa == OnboardingCanal.ETAPA_SENHA
    antes = OnboardingCanalConclusao.query.count()
    emissoes = _espionar_emissao(monkeypatch, "emitir_link_conclusao_onboarding")
    resultado = entrega.entregar_link_seguro(saida.id)
    assert resultado.codigo == entrega.CODIGO_JORNADA_INELEGIVEL
    assert chamadas == []
    assert emissoes == []
    assert j2.id not in emissoes
    row = db.session.get(EventoCanalSaida, saida.id)
    assert row.status_envio == EventoCanalSaida.STATUS_AGUARDANDO_LINK
    assert row.conclusao_id is None
    assert OnboardingCanalConclusao.query.count() == antes
    assert OnboardingCanalConclusao.query.filter_by(onboarding_id=j2.id).count() == 0
    db.session.refresh(antiga)
    assert antiga.estado == OnboardingCanalConclusao.ESTADO_EMITIDO
    assert antiga.token_hash == hash_antes
    direto = conclusao.emitir_link_conclusao_onboarding(
        int(j1.identidade_id),
        secret_key=SECRET,
        onboarding_id=j1_id,
        build_url=lambda token: f"/onboarding/canal/concluir/{token}",
        commit=False,
    )
    assert direto.codigo != conclusao.CODIGO_LINK_EMITIDO
    assert direto.conclusao_id is None
    db.session.rollback()
    assert OnboardingCanalConclusao.query.filter_by(onboarding_id=j2.id).count() == 0
    assert chamadas == []


def test_onboarding_divergente_reverte_sem_http(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, _ok())
    antiga, _token, saida = _preparar_nova(app)
    hash_antes = antiga.token_hash
    original = entrega.emitir_link_conclusao_onboarding

    def _divergente(*args, **kwargs):
        resultado = original(*args, **kwargs)
        resultado.onboarding_id = int(resultado.onboarding_id) + 1
        return resultado

    monkeypatch.setattr(entrega, "emitir_link_conclusao_onboarding", _divergente)
    resultado = entrega.entregar_link_seguro(saida.id)
    assert resultado.codigo == entrega.CODIGO_CONCLUSAO_DIVERGENTE
    assert chamadas == []
    row = db.session.get(EventoCanalSaida, saida.id)
    assert row.status_envio == EventoCanalSaida.STATUS_AGUARDANDO_LINK
    assert row.conclusao_id is None
    assert OnboardingCanalConclusao.query.count() == 1
    db.session.refresh(antiga)
    assert antiga.estado == OnboardingCanalConclusao.ESTADO_EMITIDO
    assert antiga.token_hash == hash_antes


def test_conclusao_id_divergente_do_token_reverte_sem_http(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, _ok())
    antiga, _token, saida = _preparar_nova(app)
    hash_antes = antiga.token_hash
    original = entrega.emitir_link_conclusao_onboarding

    def _troca(*args, **kwargs):
        resultado = original(*args, **kwargs)
        resultado.conclusao_id = antiga.id
        return resultado

    monkeypatch.setattr(entrega, "emitir_link_conclusao_onboarding", _troca)
    resultado = entrega.entregar_link_seguro(saida.id)
    assert resultado.codigo == entrega.CODIGO_CONCLUSAO_DIVERGENTE
    assert chamadas == []
    row = db.session.get(EventoCanalSaida, saida.id)
    assert row.status_envio == EventoCanalSaida.STATUS_AGUARDANDO_LINK
    assert row.conclusao_id is None
    assert OnboardingCanalConclusao.query.count() == 1
    db.session.refresh(antiga)
    assert antiga.estado == OnboardingCanalConclusao.ESTADO_EMITIDO
    assert antiga.token_hash == hash_antes


def test_conta_existente_j1_nao_migra_para_j2(ctx, app, monkeypatch, caplog):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, _ok())
    antiga, token_antigo, saida = _preparar_existente(app)
    j1_id = int(OnboardingCanal.query.one().id)
    j1 = db.session.get(OnboardingCanal, j1_id)
    j2 = _abrir_j2(j1, OnboardingCanal.ETAPA_EMAIL, soltar_indice=True)
    assert _jornada_aberta_mais_recente(j1.identidade_id).id == j2.id
    isca = _isca(j2.id, OnboardingCanalConclusao.FINALIDADE_VINCULAR_CONTA, "isca-j2-vinculo")
    hash_isca = isca.token_hash
    emissoes = _espionar_emissao(monkeypatch, "emitir_link_vinculo_conta_existente")
    with caplog.at_level(logging.DEBUG):
        resultado = entrega.entregar_link_seguro(saida.id)
    link = _link_do_body(chamadas)
    token = _token_do_link(link)
    row = db.session.get(EventoCanalSaida, saida.id)
    nova = db.session.get(OnboardingCanalConclusao, resultado.conclusao_id)
    assert emissoes == [j1_id]
    assert row.conclusao_id == nova.id == resultado.conclusao_id
    assert nova.onboarding_id == j1_id
    assert nova.finalidade == OnboardingCanalConclusao.FINALIDADE_VINCULAR_CONTA
    _assert_alias(token, nova)
    assert OnboardingCanalConclusao.query.filter_by(onboarding_id=j2.id).one().id == isca.id
    db.session.refresh(isca)
    assert isca.estado == OnboardingCanalConclusao.ESTADO_EMITIDO
    assert isca.token_hash == hash_isca
    assert InterpretacaoConversacionalCanal.query.one().conclusao_id == antiga.id
    assert token != token_antigo
    _assert_sem_segredo(token, link, caplog)


def test_entrega_nao_busca_conclusao_generica():
    fonte = Path(entrega.__file__).read_text(encoding="utf-8")
    assert "_id_emitido" not in fonte
    assert "order_by" not in fonte
    assert "OnboardingCanalConclusao.query" not in fonte
