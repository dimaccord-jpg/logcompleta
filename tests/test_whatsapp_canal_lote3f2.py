"""Lote WhatsApp 3F2: usuário vinculado usa o chat operacional já existente.

O teste entra pelo webhook. Não cria outro modelo nem cobra franquia.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from flask import Flask

from app.extensions import db
from app.models import (
    CleitonBillingApropriacao,
    ConteudoTextualCanal,
    EventoCanalRecebido,
    EventoCanalSaida,
    ExecucaoOperacionalCanal,
    IdentidadeCanalExterna,
    InteracaoGuestCanal,
    InterpretacaoConversacionalCanal,
    MonetizacaoFato,
    OnboardingCanal,
    TentativaEnvioCanal,
    utcnow_naive,
)
from app.services import canal_operacao_whatsapp_service as operacao
from app.services.canal_aquisicao_service import (
    PROVEDOR_WHATSAPP_META,
    obter_ou_criar_identidade_externa,
)
from app.services.canal_interpretacao_conversacional_service import (
    CODIGO_ONBOARDING_INICIADO,
    CODIGO_ORIENTACAO_GUEST,
)
from app.whatsapp_meta_webhook_routes import (
    CAMINHO,
    HEADER_ASSINATURA,
    register_whatsapp_meta_webhook_routes,
)
from tests.conftest import seed_conta_franquia_cliente, seed_usuario

import app.run_julia_chat as julia_chat
from app.services import whatsapp_meta_cloud_api_adapter as adapter
from app.services import whatsapp_meta_config as config

APP_SECRET = "segredo-app-meta-lote3f2-nao-logar"
VERIFY_TOKEN = "verify-token-lote3f2-nao-logar"
TOKEN = "EAAWHATSAPP3F2TOKENSECRETO"
VERSAO = "v26.0"
SECRET = "teste-operacao-canal-3f2"
PHONE_NUMBER_ID = "106540352242922"
PHONE = "16505551290"
PERGUNTA = "Como organizar a roteirizacao de entregas urbanas?"
RESPOSTA = "Consolide as entregas por zona antes de despachar o veiculo."
EMAIL = "vinculo.3f2@example.com"
EMAIL_OUTRO = "outro.3f2@example.com"


class _Resposta:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def close(self):
        return None


class _Modelos:
    def __init__(self, efeito):
        self.efeito = efeito
        self.chamadas = []

    def generate_content(self, *, model, contents, config=None):
        self.chamadas.append({"model": model, "contents": contents})
        if isinstance(self.efeito, Exception):
            raise self.efeito
        if callable(self.efeito):
            return self.efeito()
        return SimpleNamespace(
            text=self.efeito,
            usage_metadata=SimpleNamespace(
                prompt_token_count=3,
                candidates_token_count=4,
                total_token_count=7,
            ),
        )


def _configurar(monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv(config.ENV_APP_SECRET, APP_SECRET)
    monkeypatch.setenv(config.ENV_VERIFY_TOKEN, VERIFY_TOKEN)
    monkeypatch.setenv(config.ENV_ACCESS_TOKEN, TOKEN)
    monkeypatch.setenv(config.ENV_GRAPH_API_VERSION, VERSAO)
    monkeypatch.setenv(config.ENV_SEND_TIMEOUT_SECONDS, "4")
    monkeypatch.delenv(config.ENV_GRAPH_BASE_URL, raising=False)
    monkeypatch.setenv("GEMINI_MODEL_TEXT", "gemini-2.5-flash")
    monkeypatch.setenv("JULIA_CHAT_MODEL_FALLBACK", "gemini-2.5-flash")
    monkeypatch.setenv("GEMINI_MODEL_TEXT_FALLBACK", "gemini-2.5-flash")


def _cliente(app):
    app.config["SECRET_KEY"] = SECRET
    if "whatsapp_meta_webhook_verificacao" not in app.view_functions:
        register_whatsapp_meta_webhook_routes(app)
    return app.test_client()


def _assinar(corpo: bytes) -> str:
    digest = hmac.new(APP_SECRET.encode("utf-8"), corpo, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _envelope(value: dict) -> bytes:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "102290129340398",
                "changes": [{"field": "messages", "value": value}],
            }
        ],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _metadata() -> dict:
    return {"display_phone_number": "15550783881", "phone_number_id": PHONE_NUMBER_ID}


def _post(client, corpo: bytes):
    resposta = client.post(
        CAMINHO,
        data=corpo,
        headers={HEADER_ASSINATURA: _assinar(corpo)},
        content_type="application/json",
    )
    db.session.expire_all()
    return resposta


def _texto(client, mensagem_id: str, corpo: str, phone: str = PHONE):
    value = {
        "messaging_product": "whatsapp",
        "metadata": _metadata(),
        "contacts": [{"profile": {"name": "Nome Nao Persistir"}, "wa_id": phone}],
        "messages": [
            {
                "from": phone,
                "id": mensagem_id,
                "timestamp": "1749416383",
                "type": "text",
                "text": {"body": corpo},
            }
        ],
    }
    return _post(client, _envelope(value))


def _status(client, mensagem_id: str):
    value = {
        "messaging_product": "whatsapp",
        "metadata": _metadata(),
        "statuses": [
            {
                "id": mensagem_id,
                "status": "delivered",
                "timestamp": "1750000100",
                "recipient_id": PHONE,
            }
        ],
    }
    return _post(client, _envelope(value))


def _midia(client, mensagem_id: str):
    value = {
        "messaging_product": "whatsapp",
        "metadata": _metadata(),
        "messages": [
            {
                "from": PHONE,
                "id": mensagem_id,
                "timestamp": "1744344496",
                "type": "image",
                "image": {"caption": "LEGENDA_NAO_USAR", "id": "1003383421387256"},
            }
        ],
    }
    return _post(client, _envelope(value))


def _mock_meta(monkeypatch):
    chamadas = []

    def _post_http(url, **kwargs):
        identificador = f"wamid.F2OUT{len(chamadas) + 1}"
        chamadas.append((url, kwargs, identificador))
        return _Resposta(200, {"messages": [{"id": identificador}]})

    monkeypatch.setattr(adapter.requests, "post", _post_http)
    return chamadas


def _mock_julia(monkeypatch, efeito=RESPOSTA):
    modelos = _Modelos(efeito)
    monkeypatch.setattr(julia_chat, "_get_client", lambda: SimpleNamespace(models=modelos))
    return modelos


def _corpo(chamada) -> str:
    return chamada[1]["json"]["text"]["body"]


def _vincular(email: str = EMAIL, phone: str = PHONE, estado: str = IdentidadeCanalExterna.ESTADO_VINCULADA):
    conta, franquia = seed_conta_franquia_cliente(f"conta-{email.split('@')[0]}")
    user = seed_usuario(franquia.id, conta.id, email=email)
    user_id = int(user.id)
    user.job_role = "Coordenador de transporte"
    outro = seed_usuario(franquia.id, conta.id, email=EMAIL_OUTRO)
    outro_id = int(outro.id)
    ident = obter_ou_criar_identidade_externa(
        provedor=PROVEDOR_WHATSAPP_META,
        sujeito_externo=phone,
        contexto_destino=PHONE_NUMBER_ID,
        commit=True,
    )
    agora = utcnow_naive()
    ident.user_id = user_id
    ident.vinculada_em = agora
    if estado == IdentidadeCanalExterna.ESTADO_REVOGADA:
        ident.revogada_em = agora
    ident.estado = estado
    db.session.commit()
    return user, outro, franquia, ident


def _sem_cobranca(franquia, user) -> None:
    db.session.refresh(franquia)
    db.session.refresh(user)
    assert CleitonBillingApropriacao.query.count() == 0
    assert MonetizacaoFato.query.count() == 0
    assert InteracaoGuestCanal.query.count() == 0
    assert franquia.consumo_acumulado in (None, 0)
    assert user.creditos == 10


def _proibir_julia(monkeypatch):
    def _proibido(*_args, **_kwargs):
        raise AssertionError("julia chamada")

    monkeypatch.setattr(operacao, "chat_julia_reply", _proibido)


def test_webhook_usuario_vinculado_usa_chat_operacional(ctx, app, monkeypatch, caplog):
    _configurar(monkeypatch)
    meta = _mock_meta(monkeypatch)
    modelos = _mock_julia(monkeypatch)
    ordem = []
    governanca = julia_chat.govern_or_raise

    def _governar(*args, **kwargs):
        ordem.append("governanca")
        return governanca(*args, **kwargs)

    original = operacao.chat_julia_reply
    vistos = {}

    def _julia(*args, **kwargs):
        vistos["mensagem"] = args[0]
        vistos["history"] = args[1]
        vistos["execution_id"] = kwargs.get("execution_id")
        return original(*args, **kwargs)

    monkeypatch.setattr(julia_chat, "govern_or_raise", _governar)
    monkeypatch.setattr(operacao, "chat_julia_reply", _julia)
    client = _cliente(app)
    user, outro, franquia, ident = _vincular()
    user_id = user.id
    with caplog.at_level(logging.INFO):
        resposta = _texto(client, "wamid.F2VINCULO", PERGUNTA)
    assert resposta.status_code == 200
    assert resposta.get_json()["recebidos"] == 1
    assert vistos["mensagem"] == PERGUNTA
    assert vistos["history"] == []
    assert ConteudoTextualCanal.query.one().texto == PERGUNTA
    assert vistos["mensagem"] == ConteudoTextualCanal.query.one().texto
    execucao = ExecucaoOperacionalCanal.query.one()
    assert execucao.user_id == user_id
    assert execucao.user_id != outro.id
    assert execucao.identidade_id == ident.id
    assert execucao.estado == ExecucaoOperacionalCanal.ESTADO_CONCLUIDA
    assert execucao.conclusao_util == 1
    assert execucao.codigo == ExecucaoOperacionalCanal.CODIGO_RESPOSTA
    assert execucao.texto_resposta == RESPOSTA
    assert execucao.execution_id == vistos["execution_id"]
    assert len(execucao.execution_id) == 36
    assert ordem[0] == "governanca"
    assert ordem.index("governanca") < len(modelos.chamadas)
    assert len(modelos.chamadas) == 1
    assert PERGUNTA in modelos.chamadas[0]["contents"]
    assert "Você é o AgenteFrete" in modelos.chamadas[0]["contents"]
    assert PHONE not in modelos.chamadas[0]["contents"]
    assert EMAIL not in modelos.chamadas[0]["contents"]
    assert "wamid.F2VINCULO" not in modelos.chamadas[0]["contents"]
    assert user.job_role not in modelos.chamadas[0]["contents"]
    saida = EventoCanalSaida.query.one()
    assert saida.evento_entrada_id == execucao.evento_id
    assert saida.execucao_operacional_id == execucao.id
    assert saida.interpretacao_id is None
    assert saida.status_envio == EventoCanalSaida.STATUS_ACEITO
    assert saida.chave_idempotencia == EventoCanalSaida.chave_resposta_principal(execucao.evento_id)
    assert TentativaEnvioCanal.query.count() == 1
    assert len(meta) == 1
    assert _corpo(meta[0]) == RESPOSTA
    assert InterpretacaoConversacionalCanal.query.count() == 0
    assert OnboardingCanal.query.count() == 0
    _sem_cobranca(franquia, user)
    assert PERGUNTA not in caplog.text
    assert RESPOSTA not in caplog.text
    assert PHONE not in caplog.text
    assert EMAIL not in caplog.text
    assert TOKEN not in caplog.text

    antes_modelos = len(modelos.chamadas)
    antes_meta = len(meta)
    replay = _texto(client, "wamid.F2VINCULO", PERGUNTA)
    assert replay.get_json()["replays"] == 1
    assert len(modelos.chamadas) == antes_modelos
    assert len(meta) == antes_meta
    assert ExecucaoOperacionalCanal.query.count() == 1
    assert EventoCanalSaida.query.count() == 1
    _sem_cobranca(franquia, user)


def test_retomada_depois_da_resposta_nao_chama_julia(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    meta = _mock_meta(monkeypatch)
    modelos = _mock_julia(monkeypatch)
    client = _cliente(app)
    _vincular()
    original = operacao.enviar_texto_da_execucao_operacional

    def _queda(_execucao_id):
        raise RuntimeError("queda-antes-da-saida")

    monkeypatch.setattr(operacao, "enviar_texto_da_execucao_operacional", _queda)
    assert _texto(client, "wamid.F2RETOMA", PERGUNTA).status_code == 200
    assert len(modelos.chamadas) == 1
    execucao = ExecucaoOperacionalCanal.query.one()
    assert execucao.estado == ExecucaoOperacionalCanal.ESTADO_CONCLUIDA
    assert execucao.texto_resposta == RESPOSTA
    assert EventoCanalSaida.query.count() == 0
    monkeypatch.setattr(operacao, "enviar_texto_da_execucao_operacional", original)
    replay = _texto(client, "wamid.F2RETOMA", PERGUNTA)
    assert replay.get_json()["replays"] == 1
    assert len(modelos.chamadas) == 1
    assert len(meta) == 1
    assert _corpo(meta[0]) == RESPOSTA
    assert EventoCanalSaida.query.count() == 1


def test_chamada_incerta_nao_repete_provider(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    meta = _mock_meta(monkeypatch)
    _mock_julia(monkeypatch)
    client = _cliente(app)
    _vincular()
    chamadas = {"n": 0}

    def _estoura(*_args, **_kwargs):
        chamadas["n"] += 1
        raise RuntimeError("queda-durante-chamada")

    monkeypatch.setattr(operacao, "chat_julia_reply", _estoura)
    assert _texto(client, "wamid.F2INCERTO", PERGUNTA).status_code == 200
    assert chamadas["n"] == 1
    execucao = ExecucaoOperacionalCanal.query.one()
    assert execucao.estado == ExecucaoOperacionalCanal.ESTADO_CHAMADA_INICIADA
    assert execucao.texto_resposta is None
    assert EventoCanalSaida.query.count() == 0
    assert meta == []
    monkeypatch.setattr(operacao, "chat_julia_reply", julia_chat.chat_julia_reply)
    replay = _texto(client, "wamid.F2INCERTO", PERGUNTA)
    assert replay.get_json()["replays"] == 1
    assert chamadas["n"] == 1
    db.session.refresh(execucao)
    assert execucao.estado == ExecucaoOperacionalCanal.ESTADO_CHAMADA_INICIADA
    assert meta == []


def test_governanca_negada_nao_chama_provider(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    meta = _mock_meta(monkeypatch)
    modelos = _mock_julia(monkeypatch)
    client = _cliente(app)
    _vincular()
    assert _texto(client, "wamid.F2GOV", "frete\x00interno").status_code == 200
    assert modelos.chamadas == []
    execucao = ExecucaoOperacionalCanal.query.one()
    assert execucao.codigo == ExecucaoOperacionalCanal.CODIGO_GOVERNANCA_NEGADA
    assert execucao.estado == ExecucaoOperacionalCanal.ESTADO_FALHA
    assert execucao.conclusao_util == 0
    assert execucao.texto_resposta is None
    assert EventoCanalSaida.query.count() == 0
    assert meta == []


def test_erro_de_provider_nao_gera_resposta_falsa(ctx, app, monkeypatch, caplog):
    _configurar(monkeypatch)
    meta = _mock_meta(monkeypatch)
    modelos = _mock_julia(monkeypatch, RuntimeError("segredo-provedor-nao-logar"))
    client = _cliente(app)
    user, _outro, franquia, _ident = _vincular()
    with caplog.at_level(logging.INFO):
        assert _texto(client, "wamid.F2ERRO", PERGUNTA).status_code == 200
    assert len(modelos.chamadas) == 1
    execucao = ExecucaoOperacionalCanal.query.one()
    assert execucao.codigo == ExecucaoOperacionalCanal.CODIGO_FALHA_PROVEDOR
    assert execucao.conclusao_util == 0
    assert execucao.texto_resposta is None
    assert EventoCanalSaida.query.count() == 0
    assert meta == []
    assert "segredo-provedor-nao-logar" not in caplog.text
    assert julia_chat.GENERIC_REPLY_FALLBACK not in caplog.text
    replay = _texto(client, "wamid.F2ERRO", PERGUNTA)
    assert replay.get_json()["replays"] == 1
    assert len(modelos.chamadas) == 1
    assert meta == []
    _sem_cobranca(franquia, user)


def test_resposta_vazia_nao_envia_texto_enganoso(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    meta = _mock_meta(monkeypatch)
    modelos = _mock_julia(monkeypatch, "")
    client = _cliente(app)
    _vincular()
    assert _texto(client, "wamid.F2VAZIA", PERGUNTA).status_code == 200
    assert len(modelos.chamadas) == 1
    execucao = ExecucaoOperacionalCanal.query.one()
    assert execucao.estado == ExecucaoOperacionalCanal.ESTADO_FALHA
    assert execucao.conclusao_util == 0
    assert execucao.texto_resposta is None
    assert execucao.codigo == ExecucaoOperacionalCanal.CODIGO_FALHA_PROVEDOR
    assert EventoCanalSaida.query.count() == 0
    assert meta == []
    assert julia_chat.GENERIC_REPLY_FALLBACK not in [
        item.texto_resposta for item in ExecucaoOperacionalCanal.query.all()
    ]


def test_resposta_longa_demais_nao_e_enviada(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    meta = _mock_meta(monkeypatch)
    _mock_julia(monkeypatch)
    client = _cliente(app)
    _vincular()

    def _longa(*_args, **_kwargs):
        return {"reply": "x" * 4097}

    monkeypatch.setattr(operacao, "chat_julia_reply", _longa)
    assert _texto(client, "wamid.F2LONGA", PERGUNTA).status_code == 200
    execucao = ExecucaoOperacionalCanal.query.one()
    assert execucao.codigo == ExecucaoOperacionalCanal.CODIGO_RESPOSTA_INUTILIZAVEL
    assert execucao.conclusao_util == 0
    assert execucao.texto_resposta is None
    assert meta == []


def test_identidade_bloqueada_nao_chama_julia(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    meta = _mock_meta(monkeypatch)
    _proibir_julia(monkeypatch)
    client = _cliente(app)
    _vincular(estado=IdentidadeCanalExterna.ESTADO_BLOQUEADA)
    assert _texto(client, "wamid.F2BLOQ", PERGUNTA).status_code == 200
    assert ExecucaoOperacionalCanal.query.count() == 0
    assert EventoCanalSaida.query.count() == 0
    assert meta == []


def test_identidade_revogada_nao_chama_julia(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    meta = _mock_meta(monkeypatch)
    _proibir_julia(monkeypatch)
    client = _cliente(app)
    _vincular(estado=IdentidadeCanalExterna.ESTADO_REVOGADA)
    assert _texto(client, "wamid.F2REVOG", PERGUNTA).status_code == 200
    assert ExecucaoOperacionalCanal.query.count() == 0
    interpretacao = InterpretacaoConversacionalCanal.query.one()
    assert interpretacao.codigo == CODIGO_ORIENTACAO_GUEST
    assert len(meta) == 1


def test_guest_continua_sem_julia(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    meta = _mock_meta(monkeypatch)
    _proibir_julia(monkeypatch)
    client = _cliente(app)
    assert _texto(client, "wamid.F2GUEST", PERGUNTA).status_code == 200
    interpretacao = InterpretacaoConversacionalCanal.query.one()
    assert interpretacao.codigo == CODIGO_ORIENTACAO_GUEST
    assert ExecucaoOperacionalCanal.query.count() == 0
    assert len(meta) == 1
    assert IdentidadeCanalExterna.query.one().interacoes_uteis == 0


def test_onboarding_continua_deterministico(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    meta = _mock_meta(monkeypatch)
    _proibir_julia(monkeypatch)
    client = _cliente(app)
    assert _texto(client, "wamid.F2ONB", "quero me cadastrar").status_code == 200
    interpretacao = InterpretacaoConversacionalCanal.query.one()
    assert interpretacao.codigo == CODIGO_ONBOARDING_INICIADO
    assert OnboardingCanal.query.count() == 1
    assert ExecucaoOperacionalCanal.query.count() == 0
    assert len(meta) == 1


def test_status_nao_chama_julia(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    meta = _mock_meta(monkeypatch)
    _proibir_julia(monkeypatch)
    client = _cliente(app)
    assert _status(client, "wamid.F2STATUS").status_code == 200
    assert ExecucaoOperacionalCanal.query.count() == 0
    assert meta == []


def test_midia_nao_chama_julia(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    meta = _mock_meta(monkeypatch)
    _proibir_julia(monkeypatch)
    client = _cliente(app)
    assert _midia(client, "wamid.F2MIDIA").status_code == 200
    evento = EventoCanalRecebido.query.one()
    assert evento.tipo_evento == EventoCanalRecebido.TIPO_MIDIA
    assert ExecucaoOperacionalCanal.query.count() == 0
    assert meta == []


def test_servico_nao_cobra_nem_cria_llm(ctx):
    fonte = Path(operacao.__file__).read_text(encoding="utf-8")
    assert "avaliar_autorizacao_operacao_por_franquia" not in fonte
    assert "aplicar_motor_apos_ia_consumo_evento" not in fonte
    assert "input_tokens" not in fonte
    assert "chat_julia_reply" in fonte
    assert "WhatsAppMetaCloudApiAdapter" not in fonte


def test_fluxo_julia_reutilizado_ainda_responde(monkeypatch):
    _mock_julia(monkeypatch, RESPOSTA)
    resultado = julia_chat.chat_julia_reply(PERGUNTA, [])
    assert resultado["reply"] == RESPOSTA


def _dois_candidatos(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL_TEXT", "gemini-2.5-flash")
    monkeypatch.setenv("JULIA_CHAT_MODEL_FALLBACK", "gemini-2.5-flash-lite")
    monkeypatch.setenv("GEMINI_MODEL_TEXT_FALLBACK", "gemini-2.5-flash-lite")


def test_whatsapp_primeiro_candidato_falha_nao_chama_o_segundo(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    _dois_candidatos(monkeypatch)
    meta = _mock_meta(monkeypatch)
    modelos = _mock_julia(monkeypatch, RuntimeError("falha-primario-nao-logar"))
    ordem = []
    governanca = julia_chat.govern_or_raise

    def _governar(*args, **kwargs):
        ordem.append("governanca")
        return governanca(*args, **kwargs)

    monkeypatch.setattr(julia_chat, "govern_or_raise", _governar)
    client = _cliente(app)
    user, _outro, franquia, _ident = _vincular()
    assert _texto(client, "wamid.F2FALLBACK", PERGUNTA).status_code == 200
    assert [item["model"] for item in modelos.chamadas] == ["gemini-2.5-flash"]
    assert ordem[0] == "governanca"
    execucao = ExecucaoOperacionalCanal.query.one()
    assert execucao.codigo == ExecucaoOperacionalCanal.CODIGO_FALHA_PROVEDOR
    assert execucao.estado == ExecucaoOperacionalCanal.ESTADO_FALHA
    assert execucao.texto_resposta is None
    assert EventoCanalSaida.query.count() == 0
    assert meta == []
    _sem_cobranca(franquia, user)


def test_whatsapp_primeiro_candidato_vazio_nao_chama_o_segundo(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    _dois_candidatos(monkeypatch)
    meta = _mock_meta(monkeypatch)
    modelos = _mock_julia(monkeypatch, "")
    client = _cliente(app)
    user, _outro, franquia, _ident = _vincular()
    assert _texto(client, "wamid.F2VAZIO2", PERGUNTA).status_code == 200
    assert [item["model"] for item in modelos.chamadas] == ["gemini-2.5-flash"]
    execucao = ExecucaoOperacionalCanal.query.one()
    assert execucao.codigo == ExecucaoOperacionalCanal.CODIGO_FALHA_PROVEDOR
    assert execucao.estado == ExecucaoOperacionalCanal.ESTADO_FALHA
    assert execucao.texto_resposta is None
    assert EventoCanalSaida.query.count() == 0
    assert meta == []
    assert julia_chat.GENERIC_REPLY_FALLBACK not in [
        item.texto_resposta for item in ExecucaoOperacionalCanal.query.all()
    ]
    _sem_cobranca(franquia, user)


def test_chat_web_mantem_fallback_entre_candidatos(monkeypatch):
    _dois_candidatos(monkeypatch)
    modelos = _Modelos(None)
    respostas = iter([RuntimeError("falha-web"), RESPOSTA])

    def _gerar(*, model, contents, config=None):
        modelos.chamadas.append({"model": model, "contents": contents})
        efeito = next(respostas)
        if isinstance(efeito, Exception):
            raise efeito
        return SimpleNamespace(
            text=efeito,
            usage_metadata=SimpleNamespace(
                prompt_token_count=3,
                candidates_token_count=4,
                total_token_count=7,
            ),
        )

    modelos.generate_content = _gerar
    monkeypatch.setattr(julia_chat, "_get_client", lambda: SimpleNamespace(models=modelos))
    resultado = julia_chat.chat_julia_reply(PERGUNTA, [])
    assert [item["model"] for item in modelos.chamadas] == [
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
    ]
    assert resultado["reply"] == RESPOSTA


class _TravaAberta:
    def __enter__(self):
        return None

    def __exit__(self, *_args):
        return False


def test_duas_sessoes_concorrentes_so_uma_chama_provider(tmp_path, monkeypatch):
    _configurar(monkeypatch)
    meta = _mock_meta(monkeypatch)
    banco = tmp_path / "corrida_operacao_3f2.sqlite"
    flask_app = Flask("corrida-operacao-3f2")
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + banco.as_posix()
    flask_app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "connect_args": {"check_same_thread": False, "timeout": 5},
    }
    flask_app.config["TESTING"] = True
    db.init_app(flask_app)
    with flask_app.app_context():
        import app.models  # noqa: F401

        db.create_all()
        user, _outro, franquia, ident = _vincular(email="corrida.3f2@example.com")
        user_id = int(user.id)
        franquia_id = int(franquia.id)
        evento = EventoCanalRecebido(
            provider=EventoCanalRecebido.PROVIDER_META_WHATSAPP,
            evento_externo_id="wamid.F2CORRIDA",
            tipo_evento=EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL,
            sujeito_externo=PHONE,
            contexto_destino=PHONE_NUMBER_ID,
            recebido_em=utcnow_naive(),
            status_processamento=EventoCanalRecebido.STATUS_ROTEADO,
            correlation_id="corrida-3f2",
            diagnostico_seguro="mensagem_textual",
        )
        db.session.add(evento)
        db.session.flush()
        db.session.add(ConteudoTextualCanal(evento_id=int(evento.id), texto=PERGUNTA))
        agora = utcnow_naive()
        execucao = ExecucaoOperacionalCanal(
            evento_id=int(evento.id),
            identidade_id=int(ident.id),
            user_id=user_id,
            codigo=ExecucaoOperacionalCanal.CODIGO_EM_TRATAMENTO,
            estado=ExecucaoOperacionalCanal.ESTADO_RESERVADA,
            texto_resposta=None,
            execution_id=str(uuid4()),
            conclusao_util=0,
            correlation_id="corrida-3f2",
            criada_em=agora,
            atualizada_em=agora,
        )
        db.session.add(execucao)
        db.session.commit()
        evento_id = int(evento.id)
        identidade_id = int(ident.id)
        execucao_id = int(execucao.id)
        db.session.remove()

        barreira = threading.Barrier(2)
        perdedor = threading.Event()
        concessoes = []
        chamadas = []
        original = operacao._marcar_chamada

        def _marcar(row):
            barreira.wait(timeout=5)
            concedida = original(row)
            concessoes.append(concedida is not None)
            return concedida

        def _julia(*_args, **_kwargs):
            chamadas.append(1)
            if len(chamadas) == 1:
                assert perdedor.wait(timeout=5)
            return {"reply": RESPOSTA}

        monkeypatch.setattr(operacao, "_trava", lambda _evento_id: _TravaAberta())
        monkeypatch.setattr(operacao, "_marcar_chamada", _marcar)
        monkeypatch.setattr(operacao, "chat_julia_reply", _julia)
        resultados: dict[int, object] = {}

        def _worker(indice: int) -> None:
            try:
                with flask_app.app_context():
                    resultados[indice] = operacao.executar_operacao_canal(
                        evento_id,
                        identidade_id,
                    )
                    db.session.remove()
            except Exception as exc:
                resultados[indice] = exc
            finally:
                perdedor.set()

        threads = [threading.Thread(target=_worker, args=(indice,)) for indice in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        assert set(resultados) == {1, 2}
        falhas = {
            indice: item
            for indice, item in resultados.items()
            if not isinstance(item, operacao.ResultadoOperacionalCanal)
        }
        assert falhas == {}
        assert concessoes.count(True) == 1
        assert concessoes.count(False) == 1
        assert len(chamadas) == 1
        assert {item.codigo for item in resultados.values()} == {
            ExecucaoOperacionalCanal.CODIGO_RESULTADO_INCERTO,
            EventoCanalSaida.STATUS_ACEITO,
        }
        assert {item.execucao_id for item in resultados.values()} == {execucao_id}
        assert ExecucaoOperacionalCanal.query.count() == 1
        assert ExecucaoOperacionalCanal.query.one().estado == (
            ExecucaoOperacionalCanal.ESTADO_CONCLUIDA
        )
        assert len(meta) == 1
        replay = operacao.executar_operacao_canal(evento_id, identidade_id)
        assert replay.codigo == EventoCanalSaida.STATUS_ACEITO
        assert len(chamadas) == 1
        assert len(meta) == 1
        assert ExecucaoOperacionalCanal.query.count() == 1
        franquia_atual = db.session.get(type(franquia), franquia_id)
        usuario_atual = db.session.get(type(user), user_id)
        assert CleitonBillingApropriacao.query.count() == 0
        assert MonetizacaoFato.query.count() == 0
        assert franquia_atual.consumo_acumulado in (None, 0)
        assert usuario_atual.creditos == 10
        db.session.remove()
        db.drop_all()
