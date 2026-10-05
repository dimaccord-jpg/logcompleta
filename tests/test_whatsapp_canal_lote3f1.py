"""Lote WhatsApp 3F1: orquestração do pipeline já existente.

O teste entra pelo webhook ou por processar_evento_whatsapp.
Não chama entrada, interpretação, saída ou entrega entre um passo e outro.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from app.extensions import db
from app.models import (
    AplicacaoStatusCanal,
    CleitonBillingApropriacao,
    ConteudoTextualCanal,
    EstadoEntregaCanalSaida,
    EventoCanalRecebido,
    EventoCanalSaida,
    IaConsumoEvento,
    IdentidadeCanalExterna,
    InteracaoGuestCanal,
    InterpretacaoConversacionalCanal,
    MonetizacaoFato,
    OnboardingCanal,
    OnboardingCanalConclusao,
    TentativaEnvioCanal,
    TermsOfUse,
    User,
    utcnow_naive,
)
from app.services import canal_interpretacao_conversacional_service as conversa
from app.services import canal_orquestracao_whatsapp_service as orquestracao
from app.services import whatsapp_meta_cloud_api_adapter as adapter
from app.services import whatsapp_meta_config as config
from app.services.canal_aquisicao_config import limite_interacoes_guest
from app.services.canal_aquisicao_service import (
    PROVEDOR_WHATSAPP_META,
    obter_ou_criar_identidade_externa,
)
from app.services.canal_interpretacao_conversacional_service import (
    CODIGO_ONBOARDING_INICIADO,
    CODIGO_ORIENTACAO_GUEST,
    CODIGO_RESPOSTA_REGISTRADA,
    CODIGO_TERMOS_APRESENTADOS,
    TEXTO_EMAIL,
    TEXTO_GUEST,
    TEXTO_NOME,
)
from app.services.canal_reconciliacao_status_service import (
    CODIGO_APLICADO,
    CODIGO_JA_TRATADO,
    CODIGO_SEM_SAIDA,
    CODIGO_STATUS_IGNORADO,
)
from app.services.onboarding_entrevista_definicao import JOB_ROLE_MOTORISTA_ENTREGADOR
from app.whatsapp_meta_webhook_routes import (
    CAMINHO,
    HEADER_ASSINATURA,
    register_whatsapp_meta_webhook_routes,
)
from tests.conftest import seed_conta_franquia_cliente, seed_usuario

ROOT = Path(__file__).resolve().parents[1]
APP_SECRET = "segredo-app-meta-lote3f1-nao-logar"
VERIFY_TOKEN = "verify-token-lote3f1-nao-logar"
TOKEN = "EAAWHATSAPP3F1TOKENSECRETO"
VERSAO = "v26.0"
SECRET = "teste-orquestracao-canal-3f1"
PHONE_NUMBER_ID = "106540352242922"
PHONE = "16505551234"
TEXTO_INICIAL = "Quero a ajuda do AgenteFrete"
EMAIL_NOVA = "ana.nova.3f1@example.com"
EMAIL_EXISTENTE = "existe.3f1@example.com"
MARCA_LINK = "/onboarding/canal/concluir/"


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


def _configurar(monkeypatch):
    monkeypatch.setenv(config.ENV_APP_SECRET, APP_SECRET)
    monkeypatch.setenv(config.ENV_VERIFY_TOKEN, VERIFY_TOKEN)
    monkeypatch.setenv(config.ENV_ACCESS_TOKEN, TOKEN)
    monkeypatch.setenv(config.ENV_GRAPH_API_VERSION, VERSAO)
    monkeypatch.setenv(config.ENV_SEND_TIMEOUT_SECONDS, "4")
    monkeypatch.delenv(config.ENV_GRAPH_BASE_URL, raising=False)


def _mock(monkeypatch, efeito=None):
    chamadas = []

    def _post(url, **kwargs):
        identificador = f"wamid.F1OUT{len(chamadas) + 1}"
        chamadas.append((url, kwargs, identificador))
        if isinstance(efeito, Exception):
            raise efeito
        return _Resposta(200, {"messages": [{"id": identificador}]})

    monkeypatch.setattr(adapter.requests, "post", _post)
    return chamadas


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
    mensagem = {
        "from": phone,
        "id": mensagem_id,
        "timestamp": "1749416383",
        "type": "text",
        "text": {"body": corpo},
    }
    value = {
        "messaging_product": "whatsapp",
        "metadata": _metadata(),
        "contacts": [{"profile": {"name": "Nome Nao Persistir"}, "wa_id": phone}],
        "messages": [mensagem],
    }
    return _post(client, _envelope(value))


def _status(client, mensagem_id: str, status: str, marca: str, phone: str = PHONE):
    value = {
        "messaging_product": "whatsapp",
        "metadata": _metadata(),
        "statuses": [
            {
                "id": mensagem_id,
                "status": status,
                "timestamp": marca,
                "recipient_id": phone,
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


def _corpo(chamada) -> str:
    return chamada[1]["json"]["text"]["body"]


def _jornada() -> OnboardingCanal:
    return OnboardingCanal.query.one()


def _identidade(phone: str = PHONE) -> IdentidadeCanalExterna:
    return IdentidadeCanalExterna.query.filter_by(sujeito_externo=phone).one()


def _sem_cobranca() -> None:
    assert IaConsumoEvento.query.count() == 0
    assert CleitonBillingApropriacao.query.count() == 0
    assert MonetizacaoFato.query.count() == 0


def _fonte_orquestrador() -> str:
    return Path(orquestracao.__file__).read_text(encoding="utf-8")


def test_webhook_textual_percorre_o_pipeline_uma_vez(ctx, app, monkeypatch, caplog):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    client = _cliente(app)
    with caplog.at_level(logging.INFO):
        resposta = _texto(client, "wamid.F1GUEST", TEXTO_INICIAL)
    assert resposta.status_code == 200
    assert resposta.get_json()["recebidos"] == 1
    evento = EventoCanalRecebido.query.one()
    assert evento.tipo_evento == EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL
    assert evento.status_processamento == EventoCanalRecebido.STATUS_ROTEADO
    assert ConteudoTextualCanal.query.one().texto == TEXTO_INICIAL
    ident = _identidade()
    assert ident.provedor == PROVEDOR_WHATSAPP_META
    assert ident.estado == IdentidadeCanalExterna.ESTADO_GUEST
    assert ident.user_id is None
    assert ident.interacoes_uteis == 0
    interpretacao = InterpretacaoConversacionalCanal.query.one()
    assert interpretacao.evento_id == evento.id
    assert interpretacao.codigo == CODIGO_ORIENTACAO_GUEST
    assert interpretacao.texto_resposta == TEXTO_GUEST
    saida = EventoCanalSaida.query.one()
    assert saida.evento_entrada_id == evento.id
    assert saida.status_envio == EventoCanalSaida.STATUS_ACEITO
    assert TentativaEnvioCanal.query.count() == 1
    assert len(chamadas) == 1
    assert _corpo(chamadas[0]) == TEXTO_GUEST
    assert User.query.count() == 0
    _sem_cobranca()
    assert TEXTO_INICIAL not in caplog.text
    assert PHONE not in caplog.text
    assert TOKEN not in caplog.text

    replay = _texto(client, "wamid.F1GUEST", TEXTO_INICIAL)
    assert replay.status_code == 200
    assert replay.get_json()["recebidos"] == 0
    assert replay.get_json()["replays"] == 1
    assert len(chamadas) == 1
    assert EventoCanalSaida.query.count() == 1
    assert TentativaEnvioCanal.query.count() == 1
    assert InterpretacaoConversacionalCanal.query.count() == 1
    assert _identidade().interacoes_uteis == 0
    assert InteracaoGuestCanal.query.count() == 0

    de_novo = orquestracao.processar_evento_whatsapp(evento.id)
    assert de_novo.codigo == EventoCanalSaida.STATUS_ACEITO
    assert de_novo.saida_id == saida.id
    assert len(chamadas) == 1
    assert _identidade().interacoes_uteis == 0
    assert limite_interacoes_guest() == 5


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
    return f"{base}/termos-de-uso"


def test_usuario_novo_chega_ao_link_sem_chamada_manual(ctx, app, monkeypatch, caplog, tmp_path):
    _configurar(monkeypatch)
    url_termo = _disponibilizar_termo(app, monkeypatch, tmp_path)
    chamadas = _mock(monkeypatch)
    client = _cliente(app)
    passos = (
        ("wamid.F1N1", TEXTO_INICIAL),
        ("wamid.F1N2", "quero me cadastrar"),
        ("wamid.F1N3", "Ana Motorista"),
        ("wamid.F1N4", EMAIL_NOVA),
        ("wamid.F1N5", "Motorista / Entregador"),
        ("wamid.F1N6", "Entregador de aplicativo"),
        ("wamid.F1N7", "Moto"),
        ("wamid.F1N8", "ACEITO"),
    )
    with caplog.at_level(logging.INFO):
        for mensagem_id, texto in passos:
            assert _texto(client, mensagem_id, texto).status_code == 200

    ident = _identidade()
    assert ident.estado == IdentidadeCanalExterna.ESTADO_CADASTRO_EM_ANDAMENTO
    assert ident.user_id is None
    assert ident.interacoes_uteis == 0
    jornada = _jornada()
    assert jornada.etapa == OnboardingCanal.ETAPA_SENHA
    assert jornada.nome == "Ana Motorista"
    assert jornada.email_normalizado == EMAIL_NOVA
    assert jornada.job_role == JOB_ROLE_MOTORISTA_ENTREGADOR
    respostas = json.loads(jornada.respostas_entrevista_json)
    assert respostas["tipo_atuacao"] == "entregador_app"
    assert respostas["veiculo_principal"] == "moto"
    codigos = [row.codigo for row in InterpretacaoConversacionalCanal.query.order_by(
        InterpretacaoConversacionalCanal.id
    )]
    assert codigos[0] == CODIGO_ORIENTACAO_GUEST
    assert codigos[1] == CODIGO_ONBOARDING_INICIADO
    assert codigos[2] == CODIGO_RESPOSTA_REGISTRADA
    assert codigos[3] == CODIGO_RESPOSTA_REGISTRADA
    assert codigos[4] == CODIGO_RESPOSTA_REGISTRADA
    assert codigos[5] == CODIGO_RESPOSTA_REGISTRADA
    assert codigos[6] == CODIGO_TERMOS_APRESENTADOS
    assert len(chamadas) == 8
    assert _corpo(chamadas[0]) == TEXTO_GUEST
    assert _corpo(chamadas[1]) == TEXTO_NOME
    assert _corpo(chamadas[2]) == TEXTO_EMAIL
    assert "Como você atua principalmente?" in _corpo(chamadas[4])
    assert "Qual veículo você utiliza principalmente?" in _corpo(chamadas[5])
    assert url_termo in _corpo(chamadas[6])
    assert "responda ACEITO" in _corpo(chamadas[6])
    assert "1. ACEITO" not in _corpo(chamadas[6])
    assert all(MARCA_LINK not in _corpo(chamada) for chamada in chamadas[:7])
    link = _corpo(chamadas[7])
    assert "crie sua senha neste link:" in link
    assert MARCA_LINK in link
    token = link.rsplit("/", 1)[-1]
    saida = EventoCanalSaida.query.filter_by(
        evento_entrada_id=EventoCanalRecebido.query.filter_by(
            evento_externo_id="wamid.F1N8"
        ).one().id
    ).one()
    assert saida.status_envio == EventoCanalSaida.STATUS_ACEITO
    conclusao = db.session.get(OnboardingCanalConclusao, saida.conclusao_id)
    assert conclusao.finalidade == OnboardingCanalConclusao.FINALIDADE_DEFINIR_SENHA
    assert conclusao.estado == OnboardingCanalConclusao.ESTADO_EMITIDO
    assert conclusao.token_hash == hashlib.sha256(token.encode("utf-8")).hexdigest()
    gravada = db.session.get(InterpretacaoConversacionalCanal, saida.interpretacao_id)
    assert token not in (gravada.texto_resposta or "")
    assert EventoCanalSaida.query.count() == 8
    assert TentativaEnvioCanal.query.count() == 8
    assert User.query.count() == 0
    _sem_cobranca()
    assert EMAIL_NOVA not in caplog.text
    assert token not in caplog.text
    assert TOKEN not in caplog.text

    antes = OnboardingCanalConclusao.query.count()
    hash_antes = conclusao.token_hash
    message_id = saida.provider_message_id
    assert _texto(client, "wamid.F1N8", "ACEITO").get_json()["replays"] == 1
    assert len(chamadas) == 8
    assert OnboardingCanalConclusao.query.count() == antes
    db.session.refresh(conclusao)
    assert conclusao.token_hash == hash_antes
    db.session.refresh(saida)
    assert saida.provider_message_id == message_id
    assert _identidade().interacoes_uteis == 0


def test_conta_existente_recebe_link_de_vinculo(ctx, app, monkeypatch, caplog):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    client = _cliente(app)
    conta, franquia = seed_conta_franquia_cliente("conta-3f1")
    seed_usuario(franquia.id, conta.id, email=EMAIL_EXISTENTE)
    phone = "16505551299"
    with caplog.at_level(logging.INFO):
        assert _texto(client, "wamid.F1E1", "quero me cadastrar", phone).status_code == 200
        assert _texto(client, "wamid.F1E2", "Carla Existente", phone).status_code == 200
        assert _texto(client, "wamid.F1E3", EMAIL_EXISTENTE, phone).status_code == 200

    ident = _identidade(phone)
    assert ident.user_id is None
    assert ident.estado == IdentidadeCanalExterna.ESTADO_CADASTRO_EM_ANDAMENTO
    jornada = _jornada()
    assert jornada.etapa == OnboardingCanal.ETAPA_EMAIL
    assert jornada.email_normalizado == EMAIL_EXISTENTE
    interpretacao = InterpretacaoConversacionalCanal.query.filter(
        InterpretacaoConversacionalCanal.evento_id
        == EventoCanalRecebido.query.filter_by(evento_externo_id="wamid.F1E3").one().id
    ).one()
    assert interpretacao.codigo == "existing_account_verification_required"
    saida = EventoCanalSaida.query.filter_by(evento_entrada_id=interpretacao.evento_id).one()
    assert saida.status_envio == EventoCanalSaida.STATUS_ACEITO
    conclusao = db.session.get(OnboardingCanalConclusao, saida.conclusao_id)
    assert conclusao.finalidade == OnboardingCanalConclusao.FINALIDADE_VINCULAR_CONTA
    assert conclusao.estado == OnboardingCanalConclusao.ESTADO_EMITIDO
    link = _corpo(chamadas[-1])
    assert "já tem conta" in link
    assert MARCA_LINK in link
    assert EMAIL_EXISTENTE not in link
    token = link.rsplit("/", 1)[-1]
    assert conclusao.token_hash == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert User.query.count() == 1
    assert EMAIL_EXISTENTE not in caplog.text
    assert token not in caplog.text

    antes = OnboardingCanalConclusao.query.count()
    assert _texto(client, "wamid.F1E3", EMAIL_EXISTENTE, phone).get_json()["replays"] == 1
    assert len(chamadas) == 3
    assert OnboardingCanalConclusao.query.count() == antes
    assert _identidade(phone).user_id is None


def test_status_avanca_e_replay_nao_duplica(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    client = _cliente(app)
    assert _texto(client, "wamid.F1STATUS", "quanto custa o frete?").status_code == 200
    mensagem_id = chamadas[0][2]
    saida = EventoCanalSaida.query.one()
    assert saida.provider_message_id == mensagem_id

    enviado = _status(client, mensagem_id, "sent", "1750000001")
    assert enviado.status_code == 200
    assert enviado.get_json()["recebidos"] == 1
    estado = EstadoEntregaCanalSaida.query.filter_by(saida_id=saida.id).one()
    assert estado.status_entrega == EstadoEntregaCanalSaida.STATUS_SENT
    assert estado.delivered_em is None

    assert _status(client, mensagem_id, "delivered", "1750000002").status_code == 200
    db.session.refresh(estado)
    assert estado.status_entrega == EstadoEntregaCanalSaida.STATUS_DELIVERED
    assert estado.sent_em is not None
    assert estado.read_em is None

    assert _status(client, mensagem_id, "read", "1750000003").status_code == 200
    db.session.refresh(estado)
    assert estado.status_entrega == EstadoEntregaCanalSaida.STATUS_READ
    assert estado.delivered_em is not None
    assert estado.read_em is not None
    assert AplicacaoStatusCanal.query.count() == 3
    assert {row.resultado for row in AplicacaoStatusCanal.query} == {CODIGO_APLICADO}

    replay = _status(client, mensagem_id, "sent", "1750000001")
    assert replay.get_json()["recebidos"] == 0
    assert replay.get_json()["replays"] == 1
    assert AplicacaoStatusCanal.query.count() == 3
    db.session.refresh(estado)
    assert estado.status_entrega == EstadoEntregaCanalSaida.STATUS_READ
    evento_sent = EventoCanalRecebido.query.filter(
        EventoCanalRecebido.evento_externo_id == f"{mensagem_id}:sent:1750000001"
    ).one()
    repetido = orquestracao.processar_evento_whatsapp(evento_sent.id)
    assert repetido.codigo == CODIGO_JA_TRATADO
    assert AplicacaoStatusCanal.query.count() == 3
    assert len(chamadas) == 1
    assert OnboardingCanalConclusao.query.count() == 0


def test_status_orfao_nao_reaplica(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    client = _cliente(app)
    resposta = _status(client, "wamid.SEMSAIDA3F1", "sent", "1750000100")
    assert resposta.status_code == 200
    assert EventoCanalSaida.query.count() == 0
    assert EstadoEntregaCanalSaida.query.count() == 0
    aplicacao = AplicacaoStatusCanal.query.one()
    assert aplicacao.resultado == CODIGO_SEM_SAIDA
    assert aplicacao.saida_id is None
    assert "reconciliar_status_orfao" not in _fonte_orquestrador()
    replay = _status(client, "wamid.SEMSAIDA3F1", "sent", "1750000100")
    assert replay.get_json()["replays"] == 1
    assert AplicacaoStatusCanal.query.count() == 1
    assert chamadas == []


def test_status_desconhecido_permanece_ignorado(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    client = _cliente(app)
    assert _texto(client, "wamid.F1PLAYED", TEXTO_INICIAL).status_code == 200
    mensagem_id = chamadas[0][2]
    saida = EventoCanalSaida.query.one()
    antes = saida.status_envio
    assert _status(client, mensagem_id, "played", "1750000200").status_code == 200
    aplicacao = AplicacaoStatusCanal.query.one()
    assert aplicacao.resultado == CODIGO_STATUS_IGNORADO
    assert aplicacao.saida_id is None
    assert EstadoEntregaCanalSaida.query.count() == 0
    db.session.refresh(saida)
    assert saida.status_envio == antes
    assert len(chamadas) == 1


def test_midia_nao_dispara_interpretacao(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    client = _cliente(app)
    assert _midia(client, "wamid.F1MIDIA").status_code == 200
    evento = EventoCanalRecebido.query.one()
    assert evento.tipo_evento == EventoCanalRecebido.TIPO_MIDIA
    assert evento.status_processamento == EventoCanalRecebido.STATUS_AGUARDANDO_SUPORTE_MIDIA
    assert ConteudoTextualCanal.query.count() == 0
    assert InterpretacaoConversacionalCanal.query.count() == 0
    assert EventoCanalSaida.query.count() == 0
    assert IdentidadeCanalExterna.query.count() == 0
    assert chamadas == []


def test_evento_ignorado_nao_gera_saida(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    client = _cliente(app)
    assert _ignorado(client, "wamid.F1REACAO").status_code == 200
    evento = EventoCanalRecebido.query.one()
    assert evento.tipo_evento == EventoCanalRecebido.TIPO_DESCONHECIDO
    assert evento.status_processamento == EventoCanalRecebido.STATUS_IGNORADO
    assert InterpretacaoConversacionalCanal.query.count() == 0
    assert EventoCanalSaida.query.count() == 0
    assert chamadas == []


def test_excecao_nao_repete_e_preserva_o_que_ja_foi_gravado(ctx, app, monkeypatch, caplog):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    client = _cliente(app)
    segredo = "TOKEN_SECRETO_NAO_LOGAR_3F1"
    vistas = {"n": 0}

    def _estoura(_roteamento):
        vistas["n"] += 1
        raise RuntimeError(segredo)

    monkeypatch.setattr(orquestracao, "interpretar_mensagem_canal", _estoura)
    with caplog.at_level(logging.INFO):
        resposta = _texto(client, "wamid.F1ESTOURA", TEXTO_INICIAL)
    assert resposta.status_code == 200
    assert vistas["n"] == 1
    evento = EventoCanalRecebido.query.one()
    assert evento.status_processamento == EventoCanalRecebido.STATUS_ROTEADO
    assert IdentidadeCanalExterna.query.count() == 1
    assert InterpretacaoConversacionalCanal.query.count() == 0
    assert EventoCanalSaida.query.count() == 0
    assert chamadas == []
    assert "codigo=erro_tecnico" in caplog.text
    assert segredo not in caplog.text
    assert TEXTO_INICIAL not in caplog.text


def test_timeout_nao_reenvia(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, requests.Timeout("estourou"))
    client = _cliente(app)
    assert _texto(client, "wamid.F1TIMEOUT", TEXTO_INICIAL).status_code == 200
    assert len(chamadas) == 1
    saida = EventoCanalSaida.query.one()
    assert saida.status_envio == EventoCanalSaida.STATUS_ERRO
    assert saida.codigo_erro == EventoCanalSaida.CODIGO_TIMEOUT
    assert TentativaEnvioCanal.query.count() == 1
    replay = _texto(client, "wamid.F1TIMEOUT", TEXTO_INICIAL)
    assert replay.get_json()["replays"] == 1
    evento = EventoCanalRecebido.query.one()
    resultado = orquestracao.processar_evento_whatsapp(evento.id)
    assert resultado.status_envio == EventoCanalSaida.STATUS_ERRO
    assert len(chamadas) == 1
    assert EventoCanalSaida.query.count() == 1
    assert TentativaEnvioCanal.query.count() == 1


def _derrubar_interpretacao(roteamento):
    raise RuntimeError("queda-antes-da-interpretacao")


def test_replay_retoma_evento_roteado_sem_interpretacao(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    client = _cliente(app)
    monkeypatch.setattr(orquestracao, "interpretar_mensagem_canal", _derrubar_interpretacao)
    resposta = _texto(client, "wamid.F1QUEDA3B", TEXTO_INICIAL)
    assert resposta.status_code == 200
    assert resposta.get_json()["recebidos"] == 1
    evento = EventoCanalRecebido.query.one()
    assert evento.status_processamento == EventoCanalRecebido.STATUS_ROTEADO
    assert IdentidadeCanalExterna.query.count() == 1
    assert InterpretacaoConversacionalCanal.query.count() == 0
    assert EventoCanalSaida.query.count() == 0
    assert chamadas == []

    monkeypatch.setattr(orquestracao, "interpretar_mensagem_canal", conversa.interpretar_mensagem_canal)
    replay = _texto(client, "wamid.F1QUEDA3B", TEXTO_INICIAL)
    assert replay.status_code == 200
    assert replay.get_json()["recebidos"] == 0
    assert replay.get_json()["replays"] == 1
    assert EventoCanalRecebido.query.count() == 1
    interpretacao = InterpretacaoConversacionalCanal.query.one()
    assert interpretacao.evento_id == evento.id
    assert interpretacao.codigo == CODIGO_ORIENTACAO_GUEST
    saida = EventoCanalSaida.query.one()
    assert saida.evento_entrada_id == evento.id
    assert saida.status_envio == EventoCanalSaida.STATUS_ACEITO
    assert len(chamadas) == 1
    assert _corpo(chamadas[0]) == TEXTO_GUEST
    assert _identidade().interacoes_uteis == 0
    _sem_cobranca()
    assert TentativaEnvioCanal.query.count() == 1

    de_novo = _texto(client, "wamid.F1QUEDA3B", TEXTO_INICIAL)
    assert de_novo.get_json()["replays"] == 1
    assert len(chamadas) == 1
    assert InterpretacaoConversacionalCanal.query.count() == 1
    assert EventoCanalSaida.query.count() == 1
    assert OnboardingCanalConclusao.query.count() == 0


def test_orquestrador_reconstroi_rota_depois_do_3b(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    client = _cliente(app)
    monkeypatch.setattr(orquestracao, "interpretar_mensagem_canal", _derrubar_interpretacao)
    assert _texto(client, "wamid.F1DIRETO3B", TEXTO_INICIAL).status_code == 200
    evento = EventoCanalRecebido.query.one()
    assert evento.status_processamento == EventoCanalRecebido.STATUS_ROTEADO
    assert InterpretacaoConversacionalCanal.query.count() == 0
    reconstrucoes = {"n": 0}
    original = orquestracao.reconstruir_roteamento_canal

    def _reconstruir(evento_id):
        reconstrucoes["n"] += 1
        return original(evento_id)

    def _entrada_de_novo(_evento_id):
        raise AssertionError("3b reexecutado")

    monkeypatch.setattr(orquestracao, "reconstruir_roteamento_canal", _reconstruir)
    monkeypatch.setattr(orquestracao, "processar_evento_canal_recebido", _entrada_de_novo)
    monkeypatch.setattr(orquestracao, "interpretar_mensagem_canal", conversa.interpretar_mensagem_canal)
    resultado = orquestracao.processar_evento_whatsapp(evento.id)
    assert reconstrucoes["n"] == 1
    assert resultado.rota == "guest"
    assert resultado.status_envio == EventoCanalSaida.STATUS_ACEITO
    interpretacao = InterpretacaoConversacionalCanal.query.one()
    assert interpretacao.evento_id == evento.id
    assert interpretacao.codigo == CODIGO_ORIENTACAO_GUEST
    assert len(chamadas) == 1
    assert IdentidadeCanalExterna.query.count() == 1
    assert _identidade().interacoes_uteis == 0
    _sem_cobranca()


def test_interpretacao_presa_retoma_a_mesma_linha(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    client = _cliente(app)
    original = conversa._encerrar

    def _queda(*_args, **_kwargs):
        raise RuntimeError("queda-durante-interpretacao")

    monkeypatch.setattr(conversa, "_encerrar", _queda)
    resposta = _texto(client, "wamid.F1PRESA3C", TEXTO_INICIAL)
    assert resposta.status_code == 200
    evento = EventoCanalRecebido.query.one()
    assert evento.status_processamento == EventoCanalRecebido.STATUS_ROTEADO
    presa = InterpretacaoConversacionalCanal.query.one()
    assert presa.evento_id == evento.id
    assert presa.codigo == conversa.CODIGO_EM_TRATAMENTO
    assert presa.acao == conversa.ACAO_RESERVADA
    assert EventoCanalSaida.query.count() == 0
    assert chamadas == []
    presa_id = presa.id

    monkeypatch.setattr(conversa, "_encerrar", original)
    resultado = orquestracao.processar_evento_whatsapp(evento.id)
    assert InterpretacaoConversacionalCanal.query.count() == 1
    mesma = db.session.get(InterpretacaoConversacionalCanal, presa_id)
    assert mesma.evento_id == evento.id
    assert mesma.codigo == CODIGO_ORIENTACAO_GUEST
    assert mesma.acao != conversa.ACAO_RESERVADA
    assert resultado.status_envio == EventoCanalSaida.STATUS_ACEITO
    assert EventoCanalSaida.query.one().interpretacao_id == presa_id
    assert len(chamadas) == 1
    assert _identidade().interacoes_uteis == 0
    _sem_cobranca()
    assert OnboardingCanalConclusao.query.count() == 0

    replay = _texto(client, "wamid.F1PRESA3C", TEXTO_INICIAL)
    assert replay.get_json()["replays"] == 1
    assert len(chamadas) == 1
    assert InterpretacaoConversacionalCanal.query.count() == 1
    assert EventoCanalSaida.query.count() == 1
    assert TentativaEnvioCanal.query.count() == 1


def test_reservado_nao_reenvia(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    chamadas = []

    def _post_reserva(url, **kwargs):
        chamadas.append((url, kwargs, "reservado"))
        raise RuntimeError("queda-depois-da-reserva")

    monkeypatch.setattr(adapter.requests, "post", _post_reserva)
    client = _cliente(app)
    assert _texto(client, "wamid.F1RESERVADO", TEXTO_INICIAL).status_code == 200
    evento = EventoCanalRecebido.query.one()
    saida = EventoCanalSaida.query.one()
    assert saida.status_envio == EventoCanalSaida.STATUS_RESERVADO
    assert saida.provider_message_id is None
    assert len(chamadas) == 1
    assert TentativaEnvioCanal.query.count() == 1

    replay = _texto(client, "wamid.F1RESERVADO", TEXTO_INICIAL)
    assert replay.get_json()["replays"] == 1
    assert len(chamadas) == 1
    db.session.refresh(saida)
    assert saida.status_envio == EventoCanalSaida.STATUS_RESERVADO
    direto = orquestracao.processar_evento_whatsapp(evento.id)
    assert direto.status_envio == EventoCanalSaida.STATUS_RESERVADO
    assert len(chamadas) == 1
    assert EventoCanalSaida.query.count() == 1
    assert TentativaEnvioCanal.query.count() == 1


def test_preparando_link_nao_reenvia(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    client = _cliente(app)
    assert _texto(client, "wamid.F1PREPARA", TEXTO_INICIAL).status_code == 200
    evento = EventoCanalRecebido.query.one()
    saida = EventoCanalSaida.query.one()
    assert saida.status_envio == EventoCanalSaida.STATUS_ACEITO
    assert len(chamadas) == 1
    saida.status_envio = EventoCanalSaida.STATUS_PREPARANDO_LINK
    saida.provider_message_id = None
    saida.enviado_em = None
    saida.codigo_erro = None
    db.session.commit()

    replay = _texto(client, "wamid.F1PREPARA", TEXTO_INICIAL)
    assert replay.get_json()["replays"] == 1
    assert len(chamadas) == 1
    db.session.refresh(saida)
    assert saida.status_envio == EventoCanalSaida.STATUS_PREPARANDO_LINK
    direto = orquestracao.processar_evento_whatsapp(evento.id)
    assert direto.status_envio == EventoCanalSaida.STATUS_PREPARANDO_LINK
    assert len(chamadas) == 1
    assert EventoCanalSaida.query.count() == 1
    assert OnboardingCanalConclusao.query.count() == 0


def test_orquestrador_nao_implementa_o_que_ficou_fora(ctx):
    fonte = _fonte_orquestrador()
    for trecho in (
        "reconciliar_status_orfao",
        "recuperar_saida_textual",
        "chat_julia",
        "gemini",
        "requests",
        "access_token",
        "celery",
        "registrar_interacao_guest",
        "cleiton_monetizacao",
        "retry",
    ):
        assert trecho not in fonte
    rota = (ROOT / "app" / "whatsapp_meta_webhook_routes.py").read_text(encoding="utf-8")
    assert "orquestrar_eventos_persistidos" in rota
    assert "continuar_replays_pendentes" in rota
    webhook = (ROOT / "app" / "services" / "whatsapp_meta_webhook_service.py").read_text(
        encoding="utf-8"
    )
    assert "processar_evento_canal_recebido" not in webhook
    assert "processar_evento_whatsapp" not in webhook
