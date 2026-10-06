"""Lote WhatsApp 3G1: interação útil vira consumo idempotente, sem débito.

A taxa comercial do canal não existe no Gerenciador. O saldo da franquia
permanece. Token de IA não abre um segundo débito.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
from decimal import Decimal

import pytest
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from flask import Flask
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    CleitonBillingApropriacao,
    ConsumoInteracaoCanal,
    EventoCanalRecebido,
    EventoCanalSaida,
    ExecucaoOperacionalCanal,
    Franquia,
    IaConsumoEvento,
    IdentidadeCanalExterna,
    InteracaoGuestCanal,
    InterpretacaoConversacionalCanal,
    MonetizacaoFato,
    OnboardingCanal,
    Plugin,
    TentativaEnvioCanal,
    utcnow_naive,
)
from app.services import canal_consumo_whatsapp_service as consumo
from app.services import canal_operacao_whatsapp_service as operacao
from app.services import canal_orquestracao_whatsapp_service as orquestracao
from app.services.canal_aquisicao_service import (
    PROVEDOR_WHATSAPP_META,
    obter_ou_criar_identidade_externa,
)
from app.services.canal_interpretacao_conversacional_service import (
    CODIGO_ONBOARDING_INICIADO,
    CODIGO_ORIENTACAO_GUEST,
)
from app.services.canal_recuperacao_texto_service import recuperar_saida_textual
from app.services.canal_saida_whatsapp_service import enviar_texto_da_execucao_operacional
from app.services.cleiton_franquia_leitura_service import ler_franquia_operacional_cleiton
from app.services.cleiton_franquia_operacional_service import (
    aplicar_motor_apos_ia_consumo_evento,
)
from app.whatsapp_meta_webhook_routes import (
    CAMINHO,
    HEADER_ASSINATURA,
    register_whatsapp_meta_webhook_routes,
)
from tests.conftest import seed_cleiton_cost_config, seed_conta_franquia_cliente, seed_usuario

import app.run_julia_chat as julia_chat
from app.services import whatsapp_meta_cloud_api_adapter as adapter
from app.services import whatsapp_meta_config as config

APP_SECRET = "segredo-app-meta-lote3g1-nao-logar"
VERIFY_TOKEN = "verify-token-lote3g1-nao-logar"
TOKEN = "EAAWHATSAPP3G1TOKENSECRETO"
VERSAO = "v26.0"
SECRET = "teste-consumo-canal-3g1"
PHONE_NUMBER_ID = "106540352242922"
PHONE = "16505551290"
PERGUNTA = "Como organizar a roteirizacao de entregas urbanas?"
RESPOSTA = "Consolide as entregas por zona antes de despachar o veiculo."
EMAIL = "vinculo.3g1@example.com"
EMAIL_OUTRO = "admin.3g1@example.com"
EMAIL_EXISTENTE = "existe.3g1@example.com"


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


def _status(client, mensagem_id: str, status: str):
    value = {
        "messaging_product": "whatsapp",
        "metadata": _metadata(),
        "statuses": [
            {
                "id": mensagem_id,
                "status": status,
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


def _ignorado(client, mensagem_id: str):
    value = {
        "messaging_product": "whatsapp",
        "metadata": _metadata(),
        "messages": [
            {
                "from": PHONE,
                "id": mensagem_id,
                "timestamp": "1749416383",
                "type": "reaction",
                "reaction": {"message_id": "wamid.ALVO", "emoji": "👍"},
            }
        ],
    }
    return _post(client, _envelope(value))


def _mock_meta(monkeypatch):
    chamadas = []

    def _post_http(url, **kwargs):
        identificador = f"wamid.G1OUT{len(chamadas) + 1}"
        chamadas.append((url, kwargs, identificador))
        return _Resposta(200, {"messages": [{"id": identificador}]})

    monkeypatch.setattr(adapter.requests, "post", _post_http)
    return chamadas


def _mock_julia(monkeypatch, efeito=RESPOSTA):
    modelos = _Modelos(efeito)
    monkeypatch.setattr(julia_chat, "_get_client", lambda: SimpleNamespace(models=modelos))
    return modelos


def _proibir_julia(monkeypatch):
    def _proibido(*_args, **_kwargs):
        raise AssertionError("julia chamada")

    monkeypatch.setattr(operacao, "chat_julia_reply", _proibido)


def _vincular(
    email: str = EMAIL,
    phone: str = PHONE,
    *,
    consumo_a: str = "1",
    consumo_b: str = "7",
):
    conta, franquia = seed_conta_franquia_cliente(f"conta-{email.split('@')[0]}")
    franquia.limite_total = Decimal("50")
    franquia.consumo_acumulado = Decimal(consumo_a)
    outra = Franquia(
        conta_id=conta.id,
        nome="Outra",
        slug="outra",
        status=Franquia.STATUS_ACTIVE,
        limite_total=Decimal("80"),
        consumo_acumulado=Decimal(consumo_b),
    )
    db.session.add(outra)
    db.session.commit()
    user = seed_usuario(franquia.id, conta.id, email=email)
    admin = seed_usuario(outra.id, conta.id, email=EMAIL_OUTRO)
    admin.is_admin = True
    user.job_role = "Coordenador de transporte"
    db.session.commit()
    ident = obter_ou_criar_identidade_externa(
        provedor=PROVEDOR_WHATSAPP_META,
        sujeito_externo=phone,
        contexto_destino=PHONE_NUMBER_ID,
        commit=True,
    )
    agora = utcnow_naive()
    ident.user_id = int(user.id)
    ident.vinculada_em = agora
    ident.estado = IdentidadeCanalExterna.ESTADO_VINCULADA
    db.session.commit()
    return user, admin, franquia, outra, ident


def _saldo_intacto(franquia, outra, user, admin) -> None:
    db.session.refresh(franquia)
    db.session.refresh(outra)
    db.session.refresh(user)
    db.session.refresh(admin)
    assert Decimal(str(franquia.consumo_acumulado)) == Decimal("1")
    assert Decimal(str(outra.consumo_acumulado)) == Decimal("7")
    assert user.creditos == 10
    assert admin.creditos == 10
    assert CleitonBillingApropriacao.query.count() == 0
    assert MonetizacaoFato.query.count() == 0
    assert Plugin.query.count() == 0


def _consumo_util(user, franquia) -> ConsumoInteracaoCanal:
    row = ConsumoInteracaoCanal.query.one()
    assert row.estado == ConsumoInteracaoCanal.ESTADO_PRONTA
    assert row.motivo == ConsumoInteracaoCanal.MOTIVO_TAXA_PENDENTE
    assert row.user_id == user.id
    assert row.conta_id == user.conta_id
    assert row.franquia_id == franquia.id
    assert row.tipo_consumo == ConsumoInteracaoCanal.TIPO_WHATSAPP_OPERACIONAL
    assert row.unidade == ConsumoInteracaoCanal.UNIDADE_INTERACAO
    assert row.quantidade == 1
    assert row.canal == ConsumoInteracaoCanal.CANAL_WHATSAPP
    assert row.chave_idempotente == ConsumoInteracaoCanal.chave_da_execucao(row.execucao_operacional_canal_id)
    assert row.creditos_apropriados is None
    assert PERGUNTA not in " ".join(
        str(getattr(row, coluna.name) or "") for coluna in row.__table__.columns
    )
    assert RESPOSTA not in " ".join(
        str(getattr(row, coluna.name) or "") for coluna in row.__table__.columns
    )
    assert PHONE not in " ".join(
        str(getattr(row, coluna.name) or "") for coluna in row.__table__.columns
    )
    assert EMAIL not in " ".join(
        str(getattr(row, coluna.name) or "") for coluna in row.__table__.columns
    )
    return row


def _preparar_util(ctx_app, monkeypatch, mensagem_id="wamid.G1UTIL"):
    _configurar(monkeypatch)
    meta = _mock_meta(monkeypatch)
    modelos = _mock_julia(monkeypatch)
    seed_cleiton_cost_config()
    client = _cliente(ctx_app)
    user, admin, franquia, outra, ident = _vincular()
    assert consumo.obter_regra_conversao_whatsapp() is None
    assert _texto(client, mensagem_id, PERGUNTA).status_code == 200
    return client, meta, modelos, user, admin, franquia, outra, ident


def test_usuario_vinculado_registra_interacao_sem_debitar(ctx, app, monkeypatch, caplog):
    _configurar(monkeypatch)
    meta = _mock_meta(monkeypatch)
    modelos = _mock_julia(monkeypatch)
    seed_cleiton_cost_config()
    client = _cliente(app)
    user, admin, franquia, outra, ident = _vincular()
    assert consumo.obter_regra_conversao_whatsapp() is None
    leitura_antes = ler_franquia_operacional_cleiton(franquia.id, sincronizar_ciclo=False)
    with caplog.at_level(logging.INFO):
        assert _texto(client, "wamid.G1UTIL", PERGUNTA).status_code == 200
    execucao = ExecucaoOperacionalCanal.query.one()
    assert execucao.estado == ExecucaoOperacionalCanal.ESTADO_CONCLUIDA
    assert execucao.conclusao_util == 1
    assert execucao.codigo == ExecucaoOperacionalCanal.CODIGO_RESPOSTA
    assert execucao.texto_resposta == RESPOSTA
    assert execucao.user_id == user.id
    assert execucao.user_id != admin.id
    assert execucao.identidade_id == ident.id
    row = _consumo_util(user, franquia)
    assert row.execucao_operacional_canal_id == execucao.id
    assert row.evento_canal_recebido_id == execucao.evento_id
    assert row.franquia_id != outra.id
    assert len(modelos.chamadas) == 1
    assert len(meta) == 1
    _saldo_intacto(franquia, outra, user, admin)
    leitura = ler_franquia_operacional_cleiton(franquia.id, sincronizar_ciclo=False)
    assert leitura.franquia_id == franquia.id
    assert leitura.consumo_acumulado == leitura_antes.consumo_acumulado
    eventos = IaConsumoEvento.query.all()
    assert eventos
    for evento in eventos:
        assert evento.usuario_id is None
        assert evento.tipo_origem == "http_anonimo"
        aplicar_motor_apos_ia_consumo_evento(int(evento.id))
    assert ConsumoInteracaoCanal.query.count() == 1
    assert CleitonBillingApropriacao.query.count() == 0
    _saldo_intacto(franquia, outra, user, admin)
    assert PERGUNTA not in caplog.text
    assert RESPOSTA not in caplog.text
    assert PHONE not in caplog.text
    assert EMAIL not in caplog.text
    assert TOKEN not in caplog.text


def test_replay_e_orquestrador_nao_duplicam_consumo(ctx, app, monkeypatch):
    client, meta, modelos, user, admin, franquia, outra, _ident = _preparar_util(
        app, monkeypatch, "wamid.G1REPLAY"
    )
    consumo_id = ConsumoInteracaoCanal.query.one().id
    evento_id = ExecucaoOperacionalCanal.query.one().evento_id
    replay = _texto(client, "wamid.G1REPLAY", PERGUNTA)
    assert replay.get_json()["replays"] == 1
    assert orquestracao.processar_evento_whatsapp(evento_id).codigo == EventoCanalSaida.STATUS_ACEITO
    assert operacao.executar_operacao_canal(evento_id).codigo == EventoCanalSaida.STATUS_ACEITO
    repetido = consumo.registrar_consumo_operacional_canal(
        int(ExecucaoOperacionalCanal.query.one().id)
    )
    assert repetido.duplicado is True
    assert repetido.consumo_id == consumo_id
    assert ConsumoInteracaoCanal.query.count() == 1
    assert len(modelos.chamadas) == 1
    assert len(meta) == 1
    _saldo_intacto(franquia, outra, user, admin)


def test_status_sent_delivered_e_read_nao_cobram_de_novo(ctx, app, monkeypatch):
    client, meta, modelos, user, admin, franquia, outra, _ident = _preparar_util(
        app, monkeypatch, "wamid.G1STATUS"
    )
    saida = EventoCanalSaida.query.one()
    mensagem = saida.provider_message_id
    for status in ("sent", "delivered", "read"):
        assert _status(client, mensagem, status).status_code == 200
    assert ConsumoInteracaoCanal.query.count() == 1
    assert len(modelos.chamadas) == 1
    assert len(meta) == 1
    _saldo_intacto(franquia, outra, user, admin)


def test_retry_tecnico_nao_cobra_de_novo(ctx, app, monkeypatch):
    _client, meta, modelos, user, admin, franquia, outra, _ident = _preparar_util(
        app, monkeypatch, "wamid.G1RETRY"
    )
    saida = EventoCanalSaida.query.one()
    execucao_id = int(ExecucaoOperacionalCanal.query.one().id)
    enviar_texto_da_execucao_operacional(execucao_id)
    recuperar_saida_textual(int(saida.id), TentativaEnvioCanal.MOTIVO_FALHA_HTTP, str(uuid4()))
    assert ConsumoInteracaoCanal.query.count() == 1
    assert len(modelos.chamadas) == 1
    assert len(meta) == 1
    _saldo_intacto(franquia, outra, user, admin)


def test_guest_onboarding_e_conta_existente_nao_cobram(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    _mock_meta(monkeypatch)
    _proibir_julia(monkeypatch)
    client = _cliente(app)
    assert _texto(client, "wamid.G1GUEST", PERGUNTA).status_code == 200
    assert InterpretacaoConversacionalCanal.query.one().codigo == CODIGO_ORIENTACAO_GUEST
    assert IdentidadeCanalExterna.query.one().interacoes_uteis == 0
    assert InteracaoGuestCanal.query.count() == 0
    assert ExecucaoOperacionalCanal.query.count() == 0
    assert ConsumoInteracaoCanal.query.count() == 0

    assert _texto(client, "wamid.G1ONB", "quero me cadastrar", "16505551291").status_code == 200
    assert InterpretacaoConversacionalCanal.query.filter_by(
        codigo=CODIGO_ONBOARDING_INICIADO
    ).count() == 1
    assert OnboardingCanal.query.count() == 1
    assert ExecucaoOperacionalCanal.query.count() == 0
    assert ConsumoInteracaoCanal.query.count() == 0

    conta, franquia = seed_conta_franquia_cliente("conta-existe-3g1")
    franquia.consumo_acumulado = Decimal("3")
    db.session.commit()
    seed_usuario(franquia.id, conta.id, email=EMAIL_EXISTENTE)
    phone = "16505551299"
    assert _texto(client, "wamid.G1E1", "quero me cadastrar", phone).status_code == 200
    assert _texto(client, "wamid.G1E2", "Carla Existente", phone).status_code == 200
    assert _texto(client, "wamid.G1E3", EMAIL_EXISTENTE, phone).status_code == 200
    assert InterpretacaoConversacionalCanal.query.filter_by(
        codigo="existing_account_verification_required"
    ).count() == 1
    assert ExecucaoOperacionalCanal.query.count() == 0
    assert ConsumoInteracaoCanal.query.count() == 0
    db.session.refresh(franquia)
    assert Decimal(str(franquia.consumo_acumulado)) == Decimal("3")


def test_falhas_midia_e_ignorado_nao_cobram(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    meta = _mock_meta(monkeypatch)
    client = _cliente(app)
    user, admin, franquia, outra, _ident = _vincular()

    _mock_julia(monkeypatch)
    assert _texto(client, "wamid.G1GOV", "frete\x00interno").status_code == 200
    governanca = ExecucaoOperacionalCanal.query.one()
    assert governanca.codigo == ExecucaoOperacionalCanal.CODIGO_GOVERNANCA_NEGADA
    assert governanca.conclusao_util == 0
    recusa = consumo.registrar_consumo_operacional_canal(int(governanca.id))
    assert recusa.codigo == consumo.CODIGO_INELEGIVEL
    assert recusa.consumo_id is None

    _mock_julia(monkeypatch, RuntimeError("segredo-provedor-nao-logar"))
    assert _texto(client, "wamid.G1PROV", PERGUNTA).status_code == 200
    provedor = ExecucaoOperacionalCanal.query.filter_by(
        codigo=ExecucaoOperacionalCanal.CODIGO_FALHA_PROVEDOR
    ).one()
    assert provedor.texto_resposta is None

    _mock_julia(monkeypatch, "")
    assert _texto(client, "wamid.G1VAZIA", PERGUNTA).status_code == 200
    vazia = ExecucaoOperacionalCanal.query.filter_by(evento_id=EventoCanalRecebido.query.filter_by(
        evento_externo_id="wamid.G1VAZIA"
    ).one().id).one()
    assert vazia.codigo == ExecucaoOperacionalCanal.CODIGO_FALHA_PROVEDOR
    assert vazia.texto_resposta is None

    _mock_julia(monkeypatch, "x" * 4097)
    assert _texto(client, "wamid.G1LONGA", PERGUNTA).status_code == 200
    assert _midia(client, "wamid.G1MIDIA").status_code == 200
    assert _ignorado(client, "wamid.G1IGNO").status_code == 200
    assert EventoCanalRecebido.query.filter_by(
        evento_externo_id="wamid.G1MIDIA"
    ).one().tipo_evento == EventoCanalRecebido.TIPO_MIDIA
    assert EventoCanalRecebido.query.filter_by(
        evento_externo_id="wamid.G1IGNO"
    ).one().tipo_evento == EventoCanalRecebido.TIPO_DESCONHECIDO
    assert ConsumoInteracaoCanal.query.count() == 0
    assert meta == []
    _saldo_intacto(franquia, outra, user, admin)
    assert user.id != admin.id


def test_saldo_esgotado_nao_inventa_debito(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    meta = _mock_meta(monkeypatch)
    modelos = _mock_julia(monkeypatch)
    client = _cliente(app)
    user, admin, franquia, outra, _ident = _vincular(consumo_a="50", consumo_b="80")
    franquia.limite_total = Decimal("50")
    outra.limite_total = Decimal("80")
    db.session.commit()
    leitura = ler_franquia_operacional_cleiton(franquia.id, sincronizar_ciclo=False)
    assert leitura.saldo_disponivel == Decimal("0")
    assert leitura.status == Franquia.STATUS_BLOCKED
    assert _texto(client, "wamid.G1SALDO", PERGUNTA).status_code == 200
    assert modelos.chamadas == []
    assert len(meta) == 1
    assert "/contrate-um-plano" in meta[0][1]["json"]["text"]["body"]
    assert ConsumoInteracaoCanal.query.count() == 0
    assert ExecucaoOperacionalCanal.query.count() == 0
    assert IaConsumoEvento.query.count() == 0
    assert CleitonBillingApropriacao.query.count() == 0
    db.session.refresh(franquia)
    db.session.refresh(outra)
    assert Decimal(str(franquia.consumo_acumulado)) == Decimal("50")
    assert Decimal(str(outra.consumo_acumulado)) == Decimal("80")
    assert user.creditos == 10
    assert admin.creditos == 10
    depois = ler_franquia_operacional_cleiton(franquia.id, sincronizar_ciclo=False)
    assert depois.saldo_disponivel == Decimal("0")
    assert depois.consumo_acumulado == leitura.consumo_acumulado
    assert depois.status == Franquia.STATUS_BLOCKED


def test_falha_do_registro_preserva_resposta_e_nao_repete(ctx, app, monkeypatch, caplog):
    _configurar(monkeypatch)
    meta = _mock_meta(monkeypatch)
    modelos = _mock_julia(monkeypatch)

    def _quebra(*_args, **_kwargs):
        raise RuntimeError("leitura-indisponivel-nao-logar")

    monkeypatch.setattr(consumo, "ler_franquia_operacional_cleiton", _quebra)
    client = _cliente(app)
    user, admin, franquia, outra, _ident = _vincular()
    with caplog.at_level(logging.INFO):
        assert _texto(client, "wamid.G1FALHA", PERGUNTA).status_code == 200
    execucao = ExecucaoOperacionalCanal.query.one()
    assert execucao.texto_resposta == RESPOSTA
    assert execucao.conclusao_util == 1
    assert len(modelos.chamadas) == 1
    assert len(meta) == 1
    row = ConsumoInteracaoCanal.query.one()
    assert row.estado == ConsumoInteracaoCanal.ESTADO_FALHA
    assert row.motivo == ConsumoInteracaoCanal.MOTIVO_CONTEXTO_INDISPONIVEL
    assert row.creditos_apropriados is None
    assert row.user_id == user.id
    repetido = consumo.registrar_consumo_operacional_canal(int(execucao.id))
    assert repetido.consumo_id == row.id
    assert repetido.duplicado is True
    assert ConsumoInteracaoCanal.query.count() == 1
    assert len(modelos.chamadas) == 1
    assert len(meta) == 1
    _saldo_intacto(franquia, outra, user, admin)
    assert "leitura-indisponivel-nao-logar" not in caplog.text
    assert PERGUNTA not in caplog.text
    assert RESPOSTA not in caplog.text


def test_chave_persistente_rejeita_segundo_consumo(ctx, app, monkeypatch):
    _preparar_util(app, monkeypatch, "wamid.G1UQ")
    primeiro = ConsumoInteracaoCanal.query.one()
    agora = utcnow_naive()
    db.session.add(
        ConsumoInteracaoCanal(
            execucao_operacional_canal_id=primeiro.execucao_operacional_canal_id,
            evento_canal_recebido_id=primeiro.evento_canal_recebido_id,
            user_id=primeiro.user_id,
            conta_id=primeiro.conta_id,
            franquia_id=primeiro.franquia_id,
            tipo_consumo=primeiro.tipo_consumo,
            quantidade=1,
            unidade=primeiro.unidade,
            canal=primeiro.canal,
            chave_idempotente=primeiro.chave_idempotente,
            estado=primeiro.estado,
            motivo=primeiro.motivo,
            correlation_id=primeiro.correlation_id,
            creditos_apropriados=None,
            criada_em=agora,
            atualizada_em=agora,
        )
    )
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()
    assert ConsumoInteracaoCanal.query.count() == 1


def test_duas_sessoes_concorrentes_criam_um_consumo(tmp_path):
    banco = tmp_path / "corrida_consumo_3g1.sqlite"
    flask_app = Flask("corrida-consumo-3g1")
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + banco.as_posix()
    flask_app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "connect_args": {"check_same_thread": False, "timeout": 5},
    }
    flask_app.config["TESTING"] = True
    db.init_app(flask_app)
    with flask_app.app_context():
        import app.models  # noqa: F401

        db.create_all()
        user, admin, franquia, outra, ident = _vincular(email="corrida.3g1@example.com")
        user_id = int(user.id)
        franquia_id = int(franquia.id)
        outra_id = int(outra.id)
        admin_id = int(admin.id)
        agora = utcnow_naive()
        evento = EventoCanalRecebido(
            provider=EventoCanalRecebido.PROVIDER_META_WHATSAPP,
            evento_externo_id="wamid.G1CORRIDA",
            tipo_evento=EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL,
            sujeito_externo=PHONE,
            contexto_destino=PHONE_NUMBER_ID,
            recebido_em=agora,
            status_processamento=EventoCanalRecebido.STATUS_ROTEADO,
            correlation_id="corrida-3g1",
            diagnostico_seguro="mensagem_textual",
        )
        db.session.add(evento)
        db.session.flush()
        execucao = ExecucaoOperacionalCanal(
            evento_id=int(evento.id),
            identidade_id=int(ident.id),
            user_id=user_id,
            codigo=ExecucaoOperacionalCanal.CODIGO_RESPOSTA,
            estado=ExecucaoOperacionalCanal.ESTADO_CONCLUIDA,
            texto_resposta=RESPOSTA,
            execution_id=str(uuid4()),
            conclusao_util=1,
            correlation_id="corrida-3g1",
            criada_em=agora,
            atualizada_em=agora,
        )
        db.session.add(execucao)
        db.session.commit()
        execucao_id = int(execucao.id)
        db.session.remove()

        barreira = threading.Barrier(2)
        resultados: dict[int, object] = {}

        def _worker(indice: int) -> None:
            try:
                with flask_app.app_context():
                    barreira.wait(timeout=5)
                    resultados[indice] = consumo.registrar_consumo_operacional_canal(execucao_id)
                    db.session.remove()
            except Exception as exc:
                resultados[indice] = exc

        threads = [threading.Thread(target=_worker, args=(indice,)) for indice in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        assert set(resultados) == {1, 2}
        falhas = {
            indice: item
            for indice, item in resultados.items()
            if not isinstance(item, consumo.ResultadoConsumoCanal)
        }
        assert falhas == {}
        assert resultados[1].consumo_id == resultados[2].consumo_id
        assert ConsumoInteracaoCanal.query.count() == 1
        row = ConsumoInteracaoCanal.query.one()
        assert row.user_id == user_id
        assert row.franquia_id == franquia_id
        assert row.franquia_id != outra_id
        assert row.estado == ConsumoInteracaoCanal.ESTADO_PRONTA
        assert row.creditos_apropriados is None
        franquia_atual = db.session.get(Franquia, franquia_id)
        outra_atual = db.session.get(Franquia, outra_id)
        assert Decimal(str(franquia_atual.consumo_acumulado)) == Decimal("1")
        assert Decimal(str(outra_atual.consumo_acumulado)) == Decimal("7")
        assert db.session.get(type(user), user_id).creditos == 10
        assert db.session.get(type(admin), admin_id).creditos == 10
        db.session.remove()
        db.drop_all()


def test_servico_nao_abate_saldo_nem_abre_plugin(ctx):
    fonte_consumo = Path(consumo.__file__).read_text(encoding="utf-8")
    fonte_operacao = Path(operacao.__file__).read_text(encoding="utf-8")
    assert "_persistir_abatimento" not in fonte_consumo
    assert "consumo_acumulado" not in fonte_consumo
    assert "Plugin(" not in fonte_consumo
    assert "input_tokens" not in fonte_operacao
    assert "registrar_consumo_operacional_canal" in fonte_operacao
    seed_cleiton_cost_config()
    assert consumo.obter_regra_conversao_whatsapp() is None
