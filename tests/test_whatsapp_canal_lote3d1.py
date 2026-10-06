"""Lote WhatsApp 3D1: envio textual idempotente pela Meta Cloud API.

Não cobre retry, recibo de entrega, modelo, cobrança nem o link de conclusão.
"""
from __future__ import annotations

import importlib.util
import logging
from datetime import timedelta
from pathlib import Path

import pytest
import requests
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    CleitonBillingApropriacao,
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
from app.services import canal_saida_whatsapp_service as saida
from app.services import whatsapp_meta_cloud_api_adapter as adapter
from app.services import whatsapp_meta_config as config
from app.services.canal_aquisicao_service import (
    PROVEDOR_WHATSAPP_META,
    iniciar_onboarding_canal,
    obter_ou_criar_identidade_externa,
)
from app.services.canal_interpretacao_conversacional_service import (
    ACAO_ACEITAR_TERMOS,
    ACAO_EMITIR_SENHA,
    ACAO_ORIENTAR_CONTA,
    CODIGO_CONTA_EXISTENTE,
    CODIGO_LINK_EMITIDO,
    CODIGO_LINK_NAO_EMITIDO,
    TEXTO_CONTA,
    TEXTO_EMAIL,
    TEXTO_SENHA,
    TEXTO_SENHA_JA_EMITIDO,
)

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "versions" / "s0t1u2v3w4x5_evento_canal_saida.py"
TOKEN = "EAAWHATSAPP3D1TOKENSECRETO"
VERSAO = "v26.0"
PHONE_NUMBER_ID = "106540352242922"
DESTINATARIO = "16505551234"
MESSAGE_ID = "wamid.HBgLACEITO3D1"
CORRELATION = "corr3d1texto01"
TEXTO = TEXTO_EMAIL
MARCADOR_CORPO = "CORPO_BRUTO_META_3D1"
TOKEN_BRUTO = "TOKENBRUTO3D1SEGREDO"
URL_CONCLUSAO = "https://entrega.invalid/onboarding/canal/concluir/" + TOKEN_BRUTO
HASH_CONCLUSAO = "c3d1" + ("ab" * 30)


def _migration_module():
    spec = importlib.util.spec_from_file_location("mig_evento_canal_saida", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fontes() -> str:
    return "\n".join(
        Path(modulo.__file__).read_text(encoding="utf-8")
        for modulo in (saida, adapter)
    )


def _configurar(monkeypatch, *, token=TOKEN, versao=VERSAO, timeout="4", base=None):
    monkeypatch.setenv(config.ENV_ACCESS_TOKEN, token)
    monkeypatch.setenv(config.ENV_GRAPH_API_VERSION, versao)
    monkeypatch.setenv(config.ENV_SEND_TIMEOUT_SECONDS, timeout)
    if base is None:
        monkeypatch.delenv(config.ENV_GRAPH_BASE_URL, raising=False)
    else:
        monkeypatch.setenv(config.ENV_GRAPH_BASE_URL, base)


def _texto_de(row) -> str:
    return "\n".join(str(getattr(row, coluna.name) or "") for coluna in row.__table__.columns)


class _Resposta:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = MARCADOR_CORPO

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def close(self):
        return None


def _mock(monkeypatch, efeito):
    chamadas = []

    def _post(url, **kwargs):
        chamadas.append((url, kwargs))
        if isinstance(efeito, Exception):
            raise efeito
        return efeito

    monkeypatch.setattr(adapter.requests, "post", _post)
    return chamadas


def _ok(extra=None):
    corpo = {
        "messaging_product": "whatsapp",
        "contacts": [{"input": "+" + DESTINATARIO, "wa_id": DESTINATARIO}],
        "messages": [{"id": MESSAGE_ID}],
        "corpo_bruto": MARCADOR_CORPO,
    }
    if extra:
        corpo.update(extra)
    return _Resposta(200, corpo)


def _interpretacao(
    texto=TEXTO,
    *,
    conclusao_id=None,
    tipo=None,
    codigo="resposta_registrada",
    acao="registrar_nome",
    etapa_final=None,
) -> InterpretacaoConversacionalCanal:
    obter_ou_criar_identidade_externa(
        provedor=PROVEDOR_WHATSAPP_META,
        sujeito_externo=DESTINATARIO,
        contexto_destino=PHONE_NUMBER_ID,
        commit=True,
    )
    evento = EventoCanalRecebido(
        provider=EventoCanalRecebido.PROVIDER_META_WHATSAPP,
        evento_externo_id="wamid.ENTRADA.3D1",
        tipo_evento=tipo or EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL,
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
        etapa_final=etapa_final,
        texto_resposta=texto,
        conclusao_id=conclusao_id,
    )
    db.session.add(row)
    db.session.commit()
    return row


def _conclusao() -> OnboardingCanalConclusao:
    ident = obter_ou_criar_identidade_externa(
        provedor=PROVEDOR_WHATSAPP_META,
        sujeito_externo=DESTINATARIO,
        contexto_destino=PHONE_NUMBER_ID,
        commit=True,
    )
    iniciar_onboarding_canal(ident.id, commit=True)
    jornada = OnboardingCanal.query.filter_by(identidade_id=ident.id).one()
    agora = utcnow_naive()
    row = OnboardingCanalConclusao(
        onboarding_id=jornada.id,
        finalidade=OnboardingCanalConclusao.FINALIDADE_DEFINIR_SENHA,
        token_hash=HASH_CONCLUSAO,
        estado=OnboardingCanalConclusao.ESTADO_EMITIDO,
        emitido_em=agora,
        expira_em=agora + timedelta(hours=2),
        atualizada_em=agora,
    )
    db.session.add(row)
    db.session.commit()
    return row


def _proibir_segredo(*textos: str) -> None:
    for texto in textos:
        assert TOKEN not in texto
        assert "Authorization" not in texto
        assert "Bearer" not in texto
        assert MARCADOR_CORPO not in texto
        assert TOKEN_BRUTO not in texto
        assert URL_CONCLUSAO not in texto
        assert "/onboarding/canal/concluir/" not in texto
        assert HASH_CONCLUSAO not in texto
        assert DESTINATARIO not in texto
        assert TEXTO not in texto
        assert TEXTO_SENHA not in texto


def test_migration_cria_saida_minima():
    modulo = _migration_module()
    assert modulo.revision == "s0t1u2v3w4x5"
    assert modulo.down_revision == "r9s0t1u2v3w4"
    assert modulo._SQL_PROVIDER == EventoCanalSaida._SQL_PROVIDER
    assert modulo._SQL_STATUS == (
        "status_envio IN ("
        "'reservado', 'aceito_provider', 'erro', 'aguardando_link_seguro'"
        ")"
    )
    assert modulo._SQL_CHAVE == EventoCanalSaida._SQL_CHAVE
    assert modulo._SQL_ERRO == EventoCanalSaida._SQL_ERRO
    assert modulo._SQL_MENSAGEM == EventoCanalSaida._SQL_MENSAGEM
    assert modulo._SQL_CORRELATION == EventoCanalSaida._SQL_CORRELATION
    assert modulo._SQL_COERENCIA == (
        "("
        "(status_envio = 'aceito_provider' AND provider_message_id IS NOT NULL "
        "AND codigo_erro IS NULL AND enviado_em IS NOT NULL)"
        " OR (status_envio = 'erro' AND provider_message_id IS NULL "
        "AND codigo_erro IS NOT NULL AND enviado_em IS NULL)"
        " OR (status_envio = 'reservado' AND provider_message_id IS NULL "
        "AND codigo_erro IS NULL AND enviado_em IS NULL)"
        " OR (status_envio = 'aguardando_link_seguro' AND provider_message_id IS NULL "
        "AND codigo_erro IS NULL AND enviado_em IS NULL)"
        ")"
    )
    assert "preparando_link" not in MIGRATION.read_text(encoding="utf-8")
    texto = MIGRATION.read_text(encoding="utf-8")
    for proibido in (
        "access_token",
        "authorization",
        "payload",
        "texto_resposta",
        "telefone",
        "token_hash",
    ):
        assert proibido not in texto
    assert {coluna.name for coluna in EventoCanalSaida.__table__.columns} == {
        "id",
        "evento_entrada_id",
        "interpretacao_id",
        "execucao_operacional_id",
        "provider",
        "chave_idempotencia",
        "status_envio",
        "provider_message_id",
        "codigo_erro",
        "criado_em",
        "enviado_em",
        "correlation_id",
        "conclusao_id",
        "solicitacao_entrega_id",
    }


def test_interpretacao_textual_cria_uma_saida(ctx, monkeypatch):
    _configurar(monkeypatch)
    _mock(monkeypatch, _ok())
    interpretacao = _interpretacao()
    resultado = saida.enviar_texto_da_interpretacao(interpretacao.id)
    assert resultado.codigo == EventoCanalSaida.STATUS_ACEITO
    assert resultado.status_envio == EventoCanalSaida.STATUS_ACEITO
    assert EventoCanalSaida.query.count() == 1
    row = EventoCanalSaida.query.one()
    assert row.evento_entrada_id == interpretacao.evento_id
    assert row.interpretacao_id == interpretacao.id
    assert row.provider == EventoCanalSaida.PROVIDER_META_WHATSAPP
    assert row.chave_idempotencia == EventoCanalSaida.chave_resposta_principal(interpretacao.evento_id)
    assert row.correlation_id == CORRELATION
    assert row.enviado_em is not None


def test_request_usa_endpoint_formato_destinatario_e_texto(ctx, monkeypatch):
    _configurar(monkeypatch, timeout="4")
    chamadas = _mock(monkeypatch, _ok())
    interpretacao = _interpretacao()
    saida.enviar_texto_da_interpretacao(interpretacao.id)
    assert len(chamadas) == 1
    url, kwargs = chamadas[0]
    assert url == f"https://graph.facebook.com/{VERSAO}/{PHONE_NUMBER_ID}/messages"
    assert kwargs["headers"] == {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
    }
    assert kwargs["timeout"] == 4.0
    assert kwargs["allow_redirects"] is False
    assert kwargs["json"] == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": DESTINATARIO,
        "type": "text",
        "text": {"body": TEXTO},
    }
    assert kwargs["json"]["text"]["body"] == interpretacao.texto_resposta
    assert "preview_url" not in kwargs["json"]["text"]


def test_resposta_valida_persiste_provider_message_id(ctx, monkeypatch):
    _configurar(monkeypatch)
    _mock(monkeypatch, _ok())
    interpretacao = _interpretacao()
    resultado = saida.enviar_texto_da_interpretacao(interpretacao.id)
    assert resultado.provider_message_id == MESSAGE_ID
    row = EventoCanalSaida.query.one()
    assert row.provider_message_id == MESSAGE_ID
    assert row.codigo_erro is None
    assert row.status_envio == EventoCanalSaida.STATUS_ACEITO


def test_segunda_chamada_nao_reenvia(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, _ok())
    interpretacao = _interpretacao()
    primeiro = saida.enviar_texto_da_interpretacao(interpretacao.id)
    segundo = saida.enviar_texto_da_interpretacao(interpretacao.id)
    assert len(chamadas) == 1
    assert EventoCanalSaida.query.count() == 1
    assert segundo.saida_id == primeiro.saida_id
    assert segundo.provider_message_id == MESSAGE_ID
    assert segundo.codigo == EventoCanalSaida.STATUS_ACEITO


def test_unicidade_impede_segunda_saida_principal(ctx, monkeypatch):
    _configurar(monkeypatch)
    _mock(monkeypatch, _ok())
    interpretacao = _interpretacao()
    saida.enviar_texto_da_interpretacao(interpretacao.id)
    original = EventoCanalSaida.query.one()
    duplicada = EventoCanalSaida(
        evento_entrada_id=original.evento_entrada_id,
        interpretacao_id=original.interpretacao_id,
        provider=original.provider,
        chave_idempotencia=original.chave_idempotencia,
        status_envio=EventoCanalSaida.STATUS_RESERVADO,
        criado_em=utcnow_naive(),
        correlation_id=CORRELATION,
    )
    db.session.add(duplicada)
    with pytest.raises(IntegrityError):
        db.session.flush()
    db.session.rollback()
    assert EventoCanalSaida.query.count() == 1
    assert EventoCanalSaida.query.one().provider_message_id == MESSAGE_ID


def test_timeout_vira_erro_normalizado(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, requests.Timeout("estourou"))
    interpretacao = _interpretacao()
    resultado = saida.enviar_texto_da_interpretacao(interpretacao.id)
    assert len(chamadas) == 1
    assert resultado.codigo == EventoCanalSaida.CODIGO_TIMEOUT
    assert resultado.codigo_erro == EventoCanalSaida.CODIGO_TIMEOUT
    assert resultado.status_envio == EventoCanalSaida.STATUS_ERRO
    assert resultado.provider_message_id is None
    row = EventoCanalSaida.query.one()
    assert row.codigo_erro == "timeout"
    assert row.provider_message_id is None
    assert row.enviado_em is None
    assert "estourou" not in _texto_de(row)


def test_http_4xx_vira_erro_normalizado(ctx, monkeypatch):
    _configurar(monkeypatch)

    class _Quatroxx(_Resposta):
        def __init__(self):
            super().__init__(400, AssertionError("corpo 4xx nao deve ser lido"))

    _mock(monkeypatch, _Quatroxx())
    resultado = saida.enviar_texto_da_interpretacao(_interpretacao().id)
    assert resultado.codigo_erro == "http_4xx"
    assert resultado.status_envio == EventoCanalSaida.STATUS_ERRO
    row = EventoCanalSaida.query.one()
    assert row.codigo_erro == "http_4xx"
    assert MARCADOR_CORPO not in _texto_de(row)
    assert row.provider_message_id is None


def test_http_5xx_vira_erro_normalizado(ctx, monkeypatch):
    _configurar(monkeypatch)
    _mock(monkeypatch, _Resposta(503, {"error": {"message": MARCADOR_CORPO}}))
    resultado = saida.enviar_texto_da_interpretacao(_interpretacao().id)
    assert resultado.codigo_erro == "http_5xx"
    row = EventoCanalSaida.query.one()
    assert row.codigo_erro == "http_5xx"
    assert row.provider_message_id is None
    assert MARCADOR_CORPO not in _texto_de(row)


def test_resposta_invalida_e_tratada(ctx, monkeypatch):
    _configurar(monkeypatch)
    corpo = {
        "messaging_product": "whatsapp",
        "id": "wamid.FORA_DO_CONTRATO",
        "messages": [],
        "corpo_bruto": MARCADOR_CORPO,
    }
    _mock(monkeypatch, _Resposta(200, corpo))
    resultado = saida.enviar_texto_da_interpretacao(_interpretacao().id)
    assert resultado.codigo_erro == "resposta_invalida"
    assert resultado.provider_message_id is None
    row = EventoCanalSaida.query.one()
    assert row.codigo_erro == "resposta_invalida"
    assert "wamid.FORA_DO_CONTRATO" not in _texto_de(row)
    assert MARCADOR_CORPO not in _texto_de(row)


@pytest.mark.parametrize(
    ("campo", "valor"),
    [
        (config.ENV_ACCESS_TOKEN, ""),
        (config.ENV_GRAPH_API_VERSION, ""),
        (config.ENV_GRAPH_API_VERSION, "26.0"),
        (config.ENV_SEND_TIMEOUT_SECONDS, ""),
        (config.ENV_SEND_TIMEOUT_SECONDS, "0"),
    ],
)
def test_configuracao_ausente_falha_fechado(ctx, monkeypatch, campo, valor):
    _configurar(monkeypatch)
    monkeypatch.setenv(campo, valor)
    chamadas = _mock(monkeypatch, _ok())
    resultado = saida.enviar_texto_da_interpretacao(_interpretacao().id)
    assert resultado.codigo == "configuracao_ausente"
    assert chamadas == []
    assert EventoCanalSaida.query.count() == 0


def test_access_token_nao_aparece_em_banco_nem_log(ctx, monkeypatch, caplog):
    _configurar(monkeypatch)
    _mock(monkeypatch, _ok())
    interpretacao = _interpretacao()
    with caplog.at_level(logging.DEBUG):
        saida.enviar_texto_da_interpretacao(interpretacao.id)
    row = EventoCanalSaida.query.one()
    _proibir_segredo(_texto_de(row), caplog.text)
    assert TOKEN not in caplog.text
    assert "Bearer" not in caplog.text
    assert interpretacao.texto_resposta == TEXTO


def test_response_body_bruto_nao_e_persistido(ctx, monkeypatch, caplog):
    _configurar(monkeypatch)
    _mock(monkeypatch, _ok())
    with caplog.at_level(logging.DEBUG):
        saida.enviar_texto_da_interpretacao(_interpretacao().id)
    row = EventoCanalSaida.query.one()
    assert row.provider_message_id == MESSAGE_ID
    assert MARCADOR_CORPO not in _texto_de(row)
    assert MARCADOR_CORPO not in caplog.text
    assert "+" + DESTINATARIO not in _texto_de(row)


def test_conclusao_nao_reconstroi_token(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, _ok())

    def _proibido(*_args, **_kwargs):
        raise AssertionError("token de conclusao reconstruido")

    monkeypatch.setattr(
        "app.services.onboarding_canal_conclusao_service.emitir_link_conclusao_onboarding",
        _proibido,
    )
    monkeypatch.setattr(
        "app.services.onboarding_canal_conclusao_service.emitir_link_vinculo_conta_existente",
        _proibido,
    )
    conclusao = _conclusao()
    hash_antes = conclusao.token_hash
    interpretacao = _interpretacao(TEXTO_SENHA, conclusao_id=conclusao.id)
    resultado = saida.enviar_texto_da_interpretacao(interpretacao.id)
    db.session.refresh(conclusao)
    assert chamadas == []
    assert resultado.status_envio == EventoCanalSaida.STATUS_AGUARDANDO_LINK
    assert conclusao.token_hash == hash_antes
    assert conclusao.estado == OnboardingCanalConclusao.ESTADO_EMITIDO
    fonte = _fontes()
    assert "token_hash" not in fonte
    assert "emitir_link" not in fonte
    assert "SECRET_KEY" not in fonte


def test_conclusao_nao_persiste_url_nem_token(ctx, monkeypatch, caplog):
    _configurar(monkeypatch)
    _mock(monkeypatch, _ok())
    conclusao = _conclusao()
    interpretacao = _interpretacao(TEXTO_SENHA, conclusao_id=conclusao.id)
    with caplog.at_level(logging.DEBUG):
        saida.enviar_texto_da_interpretacao(interpretacao.id)
    row = EventoCanalSaida.query.one()
    _proibir_segredo(_texto_de(row), caplog.text)
    assert interpretacao.texto_resposta == TEXTO_SENHA
    assert TOKEN_BRUTO not in (interpretacao.texto_resposta or "")


def test_sem_mecanismo_de_link_fica_pendente(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, _ok())
    conclusao = _conclusao()
    interpretacao = _interpretacao(TEXTO_SENHA, conclusao_id=conclusao.id)
    primeiro = saida.enviar_texto_da_interpretacao(interpretacao.id)
    segundo = saida.enviar_texto_da_interpretacao(interpretacao.id)
    assert primeiro.codigo == EventoCanalSaida.STATUS_AGUARDANDO_LINK
    assert primeiro.provider_message_id is None
    assert primeiro.codigo_erro is None
    assert segundo.saida_id == primeiro.saida_id
    assert segundo.status_envio == EventoCanalSaida.STATUS_AGUARDANDO_LINK
    assert chamadas == []
    assert EventoCanalSaida.query.count() == 1
    assert EventoCanalSaida.query.one().enviado_em is None


def test_nenhuma_julia_ou_gemini_e_chamada(ctx, monkeypatch):
    _configurar(monkeypatch)
    _mock(monkeypatch, _ok())
    fonte = _fontes()
    for trecho in ("chat_julia", "gemini", "GenerativeModel", "run_julia", "copilot"):
        assert trecho not in fonte
    saida.enviar_texto_da_interpretacao(_interpretacao().id)
    assert "interpretar_mensagem_canal" not in Path(saida.__file__).read_text(encoding="utf-8")


def test_nenhum_billing_ocorre(ctx, monkeypatch):
    _configurar(monkeypatch)
    _mock(monkeypatch, _ok())
    saida.enviar_texto_da_interpretacao(_interpretacao().id)
    assert IaConsumoEvento.query.count() == 0
    assert CleitonBillingApropriacao.query.count() == 0
    assert MonetizacaoFato.query.count() == 0
    assert IdentidadeCanalExterna.query.one().interacoes_uteis == 0
    fonte = _fontes()
    for trecho in ("cleiton_monetizacao", "stripe", "IaConsumo", "registrar_interacao_guest"):
        assert trecho not in fonte


def _assert_link_retido(chamadas, resultado, interpretacao, texto) -> None:
    assert chamadas == []
    assert resultado.status_envio == EventoCanalSaida.STATUS_AGUARDANDO_LINK
    assert resultado.codigo == EventoCanalSaida.STATUS_AGUARDANDO_LINK
    assert resultado.provider_message_id is None
    assert resultado.codigo_erro is None
    db.session.refresh(interpretacao)
    assert interpretacao.texto_resposta == texto
    row = EventoCanalSaida.query.one()
    assert row.status_envio == EventoCanalSaida.STATUS_AGUARDANDO_LINK
    assert row.provider_message_id is None
    assert row.enviado_em is None


def test_link_nao_emitido_sem_conclusao_nao_envia(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, _ok())
    texto = "Seguimos com o cadastro."
    interpretacao = _interpretacao(
        texto,
        conclusao_id=None,
        codigo=CODIGO_LINK_NAO_EMITIDO,
        acao="registrar_nome",
    )
    primeiro = saida.enviar_texto_da_interpretacao(interpretacao.id)
    segundo = saida.enviar_texto_da_interpretacao(interpretacao.id)
    _assert_link_retido(chamadas, primeiro, interpretacao, texto)
    assert interpretacao.conclusao_id is None
    assert segundo.saida_id == primeiro.saida_id
    assert segundo.status_envio == EventoCanalSaida.STATUS_AGUARDANDO_LINK
    assert EventoCanalSaida.query.count() == 1


def test_texto_de_link_ja_enviado_sem_conclusao_nao_envia(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, _ok())
    interpretacao = _interpretacao(
        TEXTO_SENHA_JA_EMITIDO,
        conclusao_id=None,
        codigo=CODIGO_LINK_NAO_EMITIDO,
        acao=ACAO_EMITIR_SENHA,
    )
    resultado = saida.enviar_texto_da_interpretacao(interpretacao.id)
    _assert_link_retido(chamadas, resultado, interpretacao, TEXTO_SENHA_JA_EMITIDO)
    assert "O link para criar sua senha já foi enviado." in interpretacao.texto_resposta
    assert interpretacao.conclusao_id is None


def test_acao_emitir_senha_sem_conclusao_nao_envia(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, _ok())
    texto = "Seguimos com o cadastro."
    interpretacao = _interpretacao(
        texto,
        conclusao_id=None,
        codigo="resposta_registrada",
        acao=ACAO_EMITIR_SENHA,
    )
    resultado = saida.enviar_texto_da_interpretacao(interpretacao.id)
    _assert_link_retido(chamadas, resultado, interpretacao, texto)
    assert interpretacao.conclusao_id is None


def test_conta_existente_sem_conclusao_nao_envia(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, _ok())
    interpretacao = _interpretacao(
        TEXTO_CONTA,
        conclusao_id=None,
        codigo=CODIGO_CONTA_EXISTENTE,
        acao=ACAO_ORIENTAR_CONTA,
    )
    resultado = saida.enviar_texto_da_interpretacao(interpretacao.id)
    _assert_link_retido(chamadas, resultado, interpretacao, TEXTO_CONTA)
    assert interpretacao.conclusao_id is None
    assert "link enviado" in interpretacao.texto_resposta


def test_link_emitido_sem_conclusao_nao_envia(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, _ok())
    interpretacao = _interpretacao(
        TEXTO_SENHA,
        conclusao_id=None,
        codigo=CODIGO_LINK_EMITIDO,
        acao=ACAO_ACEITAR_TERMOS,
    )
    resultado = saida.enviar_texto_da_interpretacao(interpretacao.id)
    _assert_link_retido(chamadas, resultado, interpretacao, TEXTO_SENHA)
    assert interpretacao.conclusao_id is None
    assert "crie sua senha no link enviado" in interpretacao.texto_resposta


def test_etapa_aguardando_senha_sem_conclusao_nao_envia(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, _ok())
    texto = "Seguimos com o cadastro."
    interpretacao = _interpretacao(
        texto,
        conclusao_id=None,
        codigo="resposta_registrada",
        acao="registrar_nome",
        etapa_final=OnboardingCanal.ETAPA_SENHA,
    )
    resultado = saida.enviar_texto_da_interpretacao(interpretacao.id)
    _assert_link_retido(chamadas, resultado, interpretacao, texto)
    assert interpretacao.conclusao_id is None


def test_interpretacao_comum_sem_relacao_com_link_envia(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, _ok())
    interpretacao = _interpretacao(
        TEXTO,
        codigo="resposta_registrada",
        acao="registrar_nome",
    )
    resultado = saida.enviar_texto_da_interpretacao(interpretacao.id)
    assert len(chamadas) == 1
    assert resultado.status_envio == EventoCanalSaida.STATUS_ACEITO
    assert resultado.provider_message_id == MESSAGE_ID
    assert chamadas[0][1]["json"]["text"]["body"] == TEXTO
    assert interpretacao.conclusao_id is None
    assert EventoCanalSaida.query.one().status_envio == EventoCanalSaida.STATUS_ACEITO


def test_conclusao_com_id_permanece_aguardando_link_seguro(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, _ok())
    conclusao = _conclusao()
    interpretacao = _interpretacao(
        TEXTO_SENHA,
        conclusao_id=conclusao.id,
        codigo=CODIGO_LINK_EMITIDO,
        acao=ACAO_EMITIR_SENHA,
        etapa_final=OnboardingCanal.ETAPA_SENHA,
    )
    resultado = saida.enviar_texto_da_interpretacao(interpretacao.id)
    _assert_link_retido(chamadas, resultado, interpretacao, TEXTO_SENHA)
    assert interpretacao.conclusao_id == conclusao.id


def test_base_url_porta_invalida_rejeitada_antes_da_reserva(ctx, monkeypatch):
    _configurar(monkeypatch, base="https://graph.facebook.com:99999")
    chamadas = _mock(monkeypatch, _ok())
    interpretacao = _interpretacao()
    resultado = saida.enviar_texto_da_interpretacao(interpretacao.id)
    assert config.graph_base_url() == ""
    assert config.configuracao_envio() is None
    assert resultado.codigo == "configuracao_ausente"
    assert resultado.codigo_erro == "configuracao_ausente"
    assert chamadas == []
    assert EventoCanalSaida.query.count() == 0
    assert interpretacao.texto_resposta == TEXTO


def test_base_url_porta_valida_e_aceita(ctx, monkeypatch):
    base = "https://graph.facebook.com:8443"
    _configurar(monkeypatch, base=base)
    chamadas = _mock(monkeypatch, _ok())
    resultado = saida.enviar_texto_da_interpretacao(_interpretacao().id)
    assert config.graph_base_url() == base
    assert resultado.status_envio == EventoCanalSaida.STATUS_ACEITO
    assert len(chamadas) == 1
    assert chamadas[0][0] == f"{base}/{VERSAO}/{PHONE_NUMBER_ID}/messages"
    assert EventoCanalSaida.query.count() == 1


@pytest.mark.parametrize(
    "base",
    [
        "https://graph.facebook.com:99999",
        "https://graph.facebook.com:0",
        "https://user:pass@graph.facebook.com",
        "https://graph.facebook.com?token=1",
        "https://graph.facebook.com#frag",
        "http://graph.facebook.com",
        "https://graph.facebook.com/v26.0",
    ],
)
def test_configuracao_invalida_nao_cria_evento_canal_saida(ctx, monkeypatch, base):
    _configurar(monkeypatch, base=base)
    chamadas = _mock(monkeypatch, _ok())
    resultado = saida.enviar_texto_da_interpretacao(_interpretacao().id)
    assert config.graph_base_url() == ""
    assert resultado.codigo == "configuracao_ausente"
    assert chamadas == []
    assert EventoCanalSaida.query.count() == 0


def test_configuracao_corrigida_permite_envio(ctx, monkeypatch):
    _configurar(monkeypatch, base="https://graph.facebook.com:99999")
    chamadas = _mock(monkeypatch, _ok())
    interpretacao = _interpretacao()
    primeiro = saida.enviar_texto_da_interpretacao(interpretacao.id)
    assert primeiro.codigo == "configuracao_ausente"
    assert chamadas == []
    assert EventoCanalSaida.query.count() == 0
    monkeypatch.setenv(config.ENV_GRAPH_BASE_URL, "https://graph.facebook.com")
    segundo = saida.enviar_texto_da_interpretacao(interpretacao.id)
    assert len(chamadas) == 1
    assert segundo.status_envio == EventoCanalSaida.STATUS_ACEITO
    assert segundo.provider_message_id == MESSAGE_ID
    assert EventoCanalSaida.query.count() == 1
    assert EventoCanalSaida.query.one().status_envio == EventoCanalSaida.STATUS_ACEITO


def test_nenhum_retry_automatico(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = {"n": 0}

    def _post(url, **kwargs):
        chamadas["n"] += 1
        if chamadas["n"] == 1:
            raise requests.Timeout()
        return _ok()

    monkeypatch.setattr(adapter.requests, "post", _post)
    interpretacao = _interpretacao()
    primeiro = saida.enviar_texto_da_interpretacao(interpretacao.id)
    segundo = saida.enviar_texto_da_interpretacao(interpretacao.id)
    assert chamadas["n"] == 1
    assert primeiro.codigo_erro == "timeout"
    assert segundo.codigo_erro == "timeout"
    assert segundo.provider_message_id is None
    assert EventoCanalSaida.query.count() == 1
