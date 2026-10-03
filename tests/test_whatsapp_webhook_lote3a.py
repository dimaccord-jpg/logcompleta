"""Lote WhatsApp 3A: webhook Meta autenticado e evento de entrada idempotente."""
from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import logging
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    CleitonBillingApropriacao,
    ConteudoTextualCanal,
    EventoCanalRecebido,
    IaBillingCostSnapshot,
    IaConsumoEvento,
    IdentidadeCanalExterna,
    User,
    utcnow_naive,
)
from app.services import whatsapp_meta_config as config
from app.services.canal_entrada_processamento_service import (
    CODIGO_JA_PROCESSADO,
    processar_evento_canal_recebido,
)
from app.services.whatsapp_meta_webhook_service import (
    CODIGO_ASSINATURA_AUSENTE,
    CODIGO_ASSINATURA_INVALIDA,
    CODIGO_ESTRUTURA_INESPERADA,
    CODIGO_EVENTO_SEM_ID,
    CODIGO_JSON_INVALIDO,
    CODIGO_PARAMETROS_AUSENTES,
    CODIGO_VERIFY_TOKEN_INVALIDO,
    HEADER_ASSINATURA,
)
from app.whatsapp_meta_webhook_routes import (
    CAMINHO,
    register_whatsapp_meta_webhook_routes,
)

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "versions" / "o6p7q8r9s0t1_evento_canal_recebido.py"
APP_SECRET = "segredo-app-meta-lote3a-nao-logar"
VERIFY_TOKEN = "verify-token-lote3a-nao-logar"
CORPO_TEXTO = "NAO_PERSISTIR_CORPO_XYZ"
LEGENDA = "LEGENDA_NAO_E_TEXTO"
TOKEN_ACESSO = "TOKEN_DE_ACESSO_NAO_PERSISTIR"
DISPLAY_PHONE = "15550783881"
NOME_PERFIL = "Sheena Nelson"
PHONE_NUMBER_ID = "106540352242922"
WA_ID = "16505551234"


def _migration_module():
    spec = importlib.util.spec_from_file_location("mig_evento_canal_recebido", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _configurar(monkeypatch):
    monkeypatch.setenv(config.ENV_APP_SECRET, APP_SECRET)
    monkeypatch.setenv(config.ENV_VERIFY_TOKEN, VERIFY_TOKEN)


@pytest.fixture(autouse=True)
def _sem_envio_meta(monkeypatch):
    """O 3F1 orquestra depois do POST. Estes testes não falam com a Graph API."""
    monkeypatch.delenv(config.ENV_ACCESS_TOKEN, raising=False)
    monkeypatch.delenv(config.ENV_GRAPH_API_VERSION, raising=False)
    monkeypatch.delenv(config.ENV_SEND_TIMEOUT_SECONDS, raising=False)


def _cliente(app):
    if "whatsapp_meta_webhook_verificacao" not in app.view_functions:
        register_whatsapp_meta_webhook_routes(app)
    return app.test_client()


def _assinar(corpo: bytes, segredo: str = APP_SECRET) -> str:
    digest = hmac.new(segredo.encode("utf-8"), corpo, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _envelope(value: dict, extra: dict | None = None) -> bytes:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "102290129340398",
                "changes": [{"field": "messages", "value": value}],
            }
        ],
    }
    if extra:
        payload.update(extra)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _metadata() -> dict:
    return {
        "display_phone_number": DISPLAY_PHONE,
        "phone_number_id": PHONE_NUMBER_ID,
    }


def _valor_mensagem(mensagem: dict) -> dict:
    return {
        "messaging_product": "whatsapp",
        "metadata": _metadata(),
        "contacts": [{"profile": {"name": NOME_PERFIL}, "wa_id": WA_ID}],
        "messages": [mensagem],
        "access_token": TOKEN_ACESSO,
    }


def _mensagem_atomica(mensagem_id: str) -> dict:
    return {
        "from": WA_ID,
        "id": mensagem_id,
        "timestamp": "1749416383",
        "type": "text",
        "text": {"body": CORPO_TEXTO},
    }


def _valor_status(item: dict, mensagem: dict | None = None) -> dict:
    value = {
        "messaging_product": "whatsapp",
        "metadata": _metadata(),
        "statuses": [item],
    }
    if mensagem is not None:
        value["messages"] = [mensagem]
    return value


def _post(client, corpo: bytes, assinatura: str | None = None, *, omitir_assinatura: bool = False):
    headers = {}
    if not omitir_assinatura:
        headers[HEADER_ASSINATURA] = _assinar(corpo) if assinatura is None else assinatura
    return client.post(
        CAMINHO,
        data=corpo,
        headers=headers,
        content_type="application/json",
    )


def _texto_persistido() -> str:
    colunas = list(EventoCanalRecebido.__table__.columns)
    pedacos = []
    for row in EventoCanalRecebido.query.all():
        pedacos.append(" ".join(str(getattr(row, coluna.name)) for coluna in colunas))
    return "\n".join(pedacos)


def _proibir(*trechos: str) -> None:
    texto = _texto_persistido()
    for trecho in trechos:
        assert trecho not in texto


def test_migration_so_cria_o_evento_de_entrada():
    modulo = _migration_module()
    assert modulo.revision == "o6p7q8r9s0t1"
    assert modulo.down_revision == "n5o6p7q8r9s0"
    assert modulo._SQL_PROVIDER == EventoCanalRecebido._SQL_PROVIDER
    assert modulo._SQL_TIPO == EventoCanalRecebido._SQL_TIPO
    assert modulo._SQL_STATUS == "status_processamento IN ('recebido')"
    assert modulo._SQL_DIAGNOSTICO == EventoCanalRecebido._SQL_DIAGNOSTICO
    assert modulo._SQL_EVENTO == EventoCanalRecebido._SQL_EVENTO
    assert modulo._SQL_OPCIONAIS == EventoCanalRecebido._SQL_OPCIONAIS
    texto = MIGRATION.read_text(encoding="utf-8")
    assert "op.add_column" not in texto
    assert "op.alter_column" not in texto
    assert texto.count("op.create_table") == 1
    assert '"user"' not in texto
    assert "onboarding_canal" not in texto
    assert "identidade_canal_externa" not in texto
    assert 'sa.Column("payload"' not in texto
    assert "payload_json" not in texto
    assert "assinatura" not in texto


def test_rotas_get_e_post_sao_endpoints_distintos(app):
    register_whatsapp_meta_webhook_routes(app)
    regras = [regra for regra in app.url_map.iter_rules() if regra.rule == CAMINHO]
    por_endpoint = {regra.endpoint: regra.methods for regra in regras}
    verificacao = por_endpoint["whatsapp_meta_webhook_verificacao"]
    recebimento = por_endpoint["whatsapp_meta_webhook_recebimento"]
    assert "GET" in verificacao
    assert "POST" not in verificacao
    assert "POST" in recebimento
    assert "GET" not in recebimento
    web = (ROOT / "app" / "web.py").read_text(encoding="utf-8")
    assert "register_whatsapp_meta_webhook_routes" in web


def test_get_challenge_valido_devolve_o_challenge(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    client = _cliente(app)
    resposta = client.get(
        CAMINHO,
        query_string={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "1158201444",
        },
    )
    assert resposta.status_code == 200
    assert resposta.mimetype == "text/plain"
    assert resposta.get_data(as_text=True) == "1158201444"
    assert EventoCanalRecebido.query.count() == 0


def test_verify_token_invalido_rejeita_o_challenge(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    client = _cliente(app)
    resposta = client.get(
        CAMINHO,
        query_string={
            "hub.mode": "subscribe",
            "hub.verify_token": "token-errado",
            "hub.challenge": "1158201444",
        },
    )
    assert resposta.status_code == 403
    assert resposta.get_json()["codigo"] == CODIGO_VERIFY_TOKEN_INVALIDO
    assert "1158201444" not in resposta.get_data(as_text=True)
    modo = client.get(
        CAMINHO,
        query_string={
            "hub.mode": "unsubscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "1158201444",
        },
    )
    assert modo.status_code == 403
    assert modo.get_data(as_text=True) != "1158201444"
    assert EventoCanalRecebido.query.count() == 0


def test_parametros_ausentes_rejeitam_o_challenge(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    client = _cliente(app)
    resposta = client.get(CAMINHO, query_string={"hub.mode": "subscribe"})
    assert resposta.status_code == 400
    assert resposta.get_json()["codigo"] == CODIGO_PARAMETROS_AUSENTES
    assert EventoCanalRecebido.query.count() == 0


def test_post_sem_assinatura_rejeita(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    client = _cliente(app)
    corpo = _envelope(_valor_mensagem({
        "from": WA_ID,
        "id": "wamid.SEMASSINATURA",
        "timestamp": "1749416383",
        "type": "text",
        "text": {"body": CORPO_TEXTO},
    }))
    resposta = _post(client, corpo, omitir_assinatura=True)
    assert resposta.status_code == 403
    assert resposta.get_json()["codigo"] == CODIGO_ASSINATURA_AUSENTE
    assert EventoCanalRecebido.query.count() == 0


def test_assinatura_invalida_rejeita_antes_do_json(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    client = _cliente(app)
    resposta = _post(client, b"isto-nao-e-json", assinatura="sha256=" + ("ab" * 32))
    assert resposta.status_code == 403
    assert resposta.get_json()["codigo"] == CODIGO_ASSINATURA_INVALIDA
    assert EventoCanalRecebido.query.count() == 0


def test_assinatura_valida_aceita_o_post(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    client = _cliente(app)
    corpo = _envelope(_valor_mensagem({
        "from": WA_ID,
        "id": "wamid.VALIDA1",
        "timestamp": "1749416383",
        "type": "text",
        "text": {"body": CORPO_TEXTO},
    }))
    resposta = _post(client, corpo)
    assert resposta.status_code == 200
    corpo_json = resposta.get_json()
    assert corpo_json["ok"] is True
    assert corpo_json["recebidos"] == 1
    assert corpo_json["replays"] == 0
    assert EventoCanalRecebido.query.count() == 1


def test_json_invalido_nao_quebra_a_sessao(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    client = _cliente(app)
    ruim = _post(client, b"{")
    assert ruim.status_code == 400
    assert ruim.get_json()["codigo"] == CODIGO_JSON_INVALIDO
    assert EventoCanalRecebido.query.count() == 0
    corpo = _envelope(_valor_mensagem({
        "from": WA_ID,
        "id": "wamid.DEPOISJSON",
        "timestamp": "1749416383",
        "type": "text",
        "text": {"body": CORPO_TEXTO},
    }))
    bom = _post(client, corpo)
    assert bom.status_code == 200
    assert EventoCanalRecebido.query.count() == 1


def test_mensagem_textual_reconhecida(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    client = _cliente(app)
    corpo = _envelope(
        _valor_mensagem({
            "from": WA_ID,
            "id": "wamid.TEXTO1",
            "timestamp": "1749416383",
            "type": "text",
            "text": {"body": CORPO_TEXTO},
        }),
        extra={"app_secret": APP_SECRET, "access_token": TOKEN_ACESSO},
    )
    resposta = _post(client, corpo)
    assert resposta.status_code == 200
    row = EventoCanalRecebido.query.one()
    assert row.provider == EventoCanalRecebido.PROVIDER_META_WHATSAPP
    assert row.evento_externo_id == "wamid.TEXTO1"
    assert row.tipo_evento == EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL
    assert row.sujeito_externo == WA_ID
    assert row.contexto_destino == PHONE_NUMBER_ID
    assert row.status_processamento == EventoCanalRecebido.STATUS_ROTEADO
    assert row.diagnostico_seguro == "mensagem_textual"
    assert row.correlation_id
    _proibir(CORPO_TEXTO, NOME_PERFIL, DISPLAY_PHONE, TOKEN_ACESSO, APP_SECRET)


def test_midia_classificada_sem_virar_texto(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    client = _cliente(app)
    corpo = _envelope(_valor_mensagem({
        "from": WA_ID,
        "id": "wamid.MIDIA1",
        "timestamp": "1744344496",
        "type": "image",
        "text": {"body": "NAO_TRATAR_MIDIA_COMO_TEXTO"},
        "image": {
            "caption": LEGENDA,
            "mime_type": "image/jpeg",
            "sha256": "HASH_DE_MIDIA_NAO_PERSISTIR",
            "id": "1003383421387256",
            "url": "https://lookaside.fbsbx.com/anexo-nao-persistir",
        },
    }))
    assert _post(client, corpo).status_code == 200
    row = EventoCanalRecebido.query.one()
    assert row.tipo_evento == EventoCanalRecebido.TIPO_MIDIA
    assert row.diagnostico_seguro == "midia:image"
    assert row.tipo_evento != EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL
    _proibir(
        "NAO_TRATAR_MIDIA_COMO_TEXTO",
        LEGENDA,
        "HASH_DE_MIDIA_NAO_PERSISTIR",
        "anexo-nao-persistir",
        "1003383421387256",
    )


def test_status_de_entrega_classificado(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    client = _cliente(app)
    entregue = _envelope({
        "messaging_product": "whatsapp",
        "metadata": _metadata(),
        "statuses": [
            {
                "id": "wamid.STATUS1",
                "status": "delivered",
                "timestamp": "1750263773",
                "recipient_id": WA_ID,
                "conversation": {"id": "CONVERSA_NAO_PERSISTIR"},
                "pricing": {"billable": True, "category": "service"},
            }
        ],
    })
    assert _post(client, entregue).status_code == 200
    row = EventoCanalRecebido.query.one()
    assert row.tipo_evento == EventoCanalRecebido.TIPO_STATUS_ENTREGA
    assert row.evento_externo_id == "wamid.STATUS1:delivered:1750263773"
    assert row.diagnostico_seguro == "status_entrega:delivered"
    assert row.sujeito_externo == WA_ID
    assert row.contexto_destino == PHONE_NUMBER_ID
    _proibir("CONVERSA_NAO_PERSISTIR", "billable", DISPLAY_PHONE)
    lido = _envelope({
        "messaging_product": "whatsapp",
        "metadata": _metadata(),
        "statuses": [
            {
                "id": "wamid.STATUS1",
                "status": "read",
                "timestamp": "1750263799",
                "recipient_id": WA_ID,
            }
        ],
    })
    segunda = _post(client, lido)
    assert segunda.status_code == 200
    assert segunda.get_json()["recebidos"] == 1
    assert EventoCanalRecebido.query.count() == 2


def test_evento_desconhecido_e_classificado(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    client = _cliente(app)
    corpo = _envelope(_valor_mensagem({
        "from": WA_ID,
        "id": "wamid.REACAO1",
        "timestamp": "1749416383",
        "type": "reaction",
        "reaction": {"message_id": "wamid.OUTRA", "emoji": "NAO_PERSISTIR_EMOJI"},
    }))
    assert _post(client, corpo).status_code == 200
    row = EventoCanalRecebido.query.one()
    assert row.tipo_evento == EventoCanalRecebido.TIPO_DESCONHECIDO
    assert row.diagnostico_seguro == "desconhecido"
    assert row.evento_externo_id == "wamid.REACAO1"
    _proibir("NAO_PERSISTIR_EMOJI", "wamid.OUTRA")


def test_replay_cria_uma_unica_linha(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    client = _cliente(app)
    corpo = _envelope(_valor_mensagem({
        "from": WA_ID,
        "id": "wamid.REPLAY1",
        "timestamp": "1749416383",
        "type": "text",
        "text": {"body": CORPO_TEXTO},
    }))
    primeiro = _post(client, corpo)
    segundo = _post(client, corpo)
    assert primeiro.status_code == 200
    assert segundo.status_code == 200
    assert segundo.get_json()["ok"] is True
    assert segundo.get_json()["recebidos"] == 0
    assert segundo.get_json()["replays"] == 1
    assert EventoCanalRecebido.query.count() == 1
    original = EventoCanalRecebido.query.one()
    with pytest.raises(IntegrityError):
        with db.session.begin_nested():
            db.session.add(
                EventoCanalRecebido(
                    provider=original.provider,
                    evento_externo_id=original.evento_externo_id,
                    tipo_evento=original.tipo_evento,
                    sujeito_externo=original.sujeito_externo,
                    contexto_destino=original.contexto_destino,
                    recebido_em=utcnow_naive(),
                    status_processamento=EventoCanalRecebido.STATUS_RECEBIDO,
                    correlation_id="abc123",
                    diagnostico_seguro=original.diagnostico_seguro,
                )
            )
            db.session.flush()
    assert EventoCanalRecebido.query.count() == 1


def test_payload_bruto_nao_e_persistido(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    client = _cliente(app)
    corpo = _envelope(
        _valor_mensagem({
            "from": WA_ID,
            "id": "wamid.BRUTO1",
            "timestamp": "1749416383",
            "type": "text",
            "text": {"body": CORPO_TEXTO},
        }),
        extra={
            "access_token": TOKEN_ACESSO,
            "app_secret": APP_SECRET,
            "X-Hub-Signature-256": "sha256=" + ("cd" * 32),
        },
    )
    assinatura = _assinar(corpo)
    assert _post(client, corpo, assinatura=assinatura).status_code == 200
    colunas = {coluna.name for coluna in EventoCanalRecebido.__table__.columns}
    assert colunas == {
        "id",
        "provider",
        "evento_externo_id",
        "tipo_evento",
        "sujeito_externo",
        "contexto_destino",
        "recebido_em",
        "status_processamento",
        "correlation_id",
        "diagnostico_seguro",
    }
    _proibir(
        CORPO_TEXTO,
        TOKEN_ACESSO,
        APP_SECRET,
        VERIFY_TOKEN,
        assinatura,
        NOME_PERFIL,
        DISPLAY_PHONE,
        "messaging_product",
        "whatsapp_business_account",
    )


def test_segredos_nao_aparecem_em_logs_nem_modelos(ctx, app, monkeypatch, caplog):
    _configurar(monkeypatch)
    client = _cliente(app)
    corpo = _envelope(
        _valor_mensagem({
            "from": WA_ID,
            "id": "wamid.SEGREDO1",
            "timestamp": "1749416383",
            "type": "text",
            "text": {"body": CORPO_TEXTO},
        }),
        extra={"app_secret": APP_SECRET, "verify_token": VERIFY_TOKEN},
    )
    assinatura = _assinar(corpo)
    with caplog.at_level(logging.DEBUG):
        assert _post(client, corpo, assinatura=assinatura).status_code == 200
        recusado = _post(client, b"nao-json", assinatura="sha256=" + ("ef" * 32))
    assert recusado.status_code == 403
    for registro in caplog.records:
        mensagem = registro.getMessage()
        assert APP_SECRET not in mensagem
        assert VERIFY_TOKEN not in mensagem
        assert assinatura not in mensagem
        assert CORPO_TEXTO not in mensagem
        assert TOKEN_ACESSO not in mensagem
    _proibir(APP_SECRET, VERIFY_TOKEN, assinatura, CORPO_TEXTO, TOKEN_ACESSO)
    fonte_config = Path(config.__file__).read_text(encoding="utf-8")
    assert "logging." not in fonte_config
    assert "META_CAPI_ACCESS_TOKEN" not in fonte_config
    assert "META_GRAPH_API_VERSION" not in fonte_config
    assert config.ENV_APP_SECRET in fonte_config
    assert config.ENV_VERIFY_TOKEN in fonte_config
    fonte_servico = Path(
        ROOT / "app" / "services" / "whatsapp_meta_webhook_service.py"
    ).read_text(encoding="utf-8")
    assert "onboarding_canal" not in fonte_servico
    assert "chat_julia" not in fonte_servico
    assert "cleiton_monetizacao" not in fonte_servico


def test_evento_sem_id_externo_nao_persiste_nem_quebra_sessao(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    client = _cliente(app)
    sem_id = _envelope(_valor_mensagem({
        "from": WA_ID,
        "timestamp": "1749416383",
        "type": "text",
        "text": {"body": CORPO_TEXTO},
    }))
    resposta = _post(client, sem_id)
    assert resposta.status_code == 400
    assert resposta.get_json()["codigo"] == CODIGO_EVENTO_SEM_ID
    assert EventoCanalRecebido.query.count() == 0
    estrutura = _post(client, _envelope_objeto_errado())
    assert estrutura.status_code == 400
    assert estrutura.get_json()["codigo"] == CODIGO_ESTRUTURA_INESPERADA
    assert EventoCanalRecebido.query.count() == 0


def _envelope_objeto_errado() -> bytes:
    return json.dumps({"object": "user", "entry": []}).encode("utf-8")


def _assert_status_rejeitado(resposta) -> None:
    assert resposta.status_code == 400
    assert resposta.get_json()["codigo"] == CODIGO_EVENTO_SEM_ID
    assert EventoCanalRecebido.query.count() == 0


def test_status_com_timestamp_valido_persiste_uma_vez(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    client = _cliente(app)
    corpo = _envelope(_valor_status({
        "id": "wamid.X",
        "status": "delivered",
        "timestamp": "1720000000",
        "recipient_id": WA_ID,
    }))
    resposta = _post(client, corpo)
    assert resposta.status_code == 200
    assert resposta.get_json()["recebidos"] == 1
    assert resposta.get_json()["replays"] == 0
    assert EventoCanalRecebido.query.count() == 1
    row = EventoCanalRecebido.query.one()
    assert row.tipo_evento == EventoCanalRecebido.TIPO_STATUS_ENTREGA
    assert row.evento_externo_id == "wamid.X:delivered:1720000000"
    assert row.diagnostico_seguro == "status_entrega:delivered"


def test_replay_do_mesmo_status_nao_duplica(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    client = _cliente(app)
    corpo = _envelope(_valor_status({
        "id": "wamid.X",
        "status": "delivered",
        "timestamp": "1720000000",
        "recipient_id": WA_ID,
    }))
    primeiro = _post(client, corpo)
    segundo = _post(client, corpo)
    assert primeiro.status_code == 200
    assert segundo.status_code == 200
    assert segundo.get_json()["ok"] is True
    assert segundo.get_json()["recebidos"] == 0
    assert segundo.get_json()["replays"] == 1
    assert EventoCanalRecebido.query.count() == 1
    assert EventoCanalRecebido.query.one().evento_externo_id == "wamid.X:delivered:1720000000"


def test_status_distintos_do_mesmo_wamid_coexistem(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    client = _cliente(app)
    trios = (
        ("sent", "1720000000"),
        ("delivered", "1720000001"),
        ("read", "1720000002"),
    )
    for status, timestamp in trios:
        corpo = _envelope(_valor_status({
            "id": "wamid.X",
            "status": status,
            "timestamp": timestamp,
            "recipient_id": WA_ID,
        }))
        resposta = _post(client, corpo)
        assert resposta.status_code == 200
        assert resposta.get_json()["recebidos"] == 1
    assert EventoCanalRecebido.query.count() == 3
    ids = {row.evento_externo_id for row in EventoCanalRecebido.query.all()}
    assert ids == {
        "wamid.X:sent:1720000000",
        "wamid.X:delivered:1720000001",
        "wamid.X:read:1720000002",
    }


def test_status_sem_timestamp_rejeita_payload_atomico(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    client = _cliente(app)
    corpo = _envelope(_valor_status(
        {
            "id": "wamid.X",
            "status": "delivered",
            "recipient_id": WA_ID,
        },
        mensagem=_mensagem_atomica("wamid.ATOMO_AUSENTE"),
    ))
    _assert_status_rejeitado(_post(client, corpo))
    assert "wamid.X:delivered" not in _texto_persistido()
    assert "wamid.ATOMO_AUSENTE" not in _texto_persistido()


def test_status_com_timestamp_vazio_rejeita(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    client = _cliente(app)
    corpo = _envelope(_valor_status(
        {
            "id": "wamid.X",
            "status": "delivered",
            "timestamp": "",
            "recipient_id": WA_ID,
        },
        mensagem=_mensagem_atomica("wamid.ATOMO_VAZIO"),
    ))
    _assert_status_rejeitado(_post(client, corpo))
    assert "wamid.X:delivered" not in _texto_persistido()
    assert "wamid.ATOMO_VAZIO" not in _texto_persistido()


def test_status_com_timestamp_invalido_rejeita(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    client = _cliente(app)
    invalidos = ("nao-e-timestamp", None, "1720000000abc", True)
    for indice, timestamp in enumerate(invalidos):
        corpo = _envelope(_valor_status(
            {
                "id": "wamid.X",
                "status": "delivered",
                "timestamp": timestamp,
                "recipient_id": WA_ID,
            },
            mensagem=_mensagem_atomica(f"wamid.ATOMO_INVALIDO_{indice}"),
        ))
        _assert_status_rejeitado(_post(client, corpo))
    assert EventoCanalRecebido.query.count() == 0
    texto = _texto_persistido()
    assert "wamid.X:delivered" not in texto
    assert "wamid.ATOMO_INVALIDO" not in texto


TEXTO_MINIMO = "so o texto"
URL_EXTRA = "https://midia.example/nao-persistir"
_SAIDA_PROIBIDA = (
    "graph.facebook",
    "chat_julia",
    "gemini",
    "cleiton_monetizacao",
    "stripe",
    "requests",
    "httpx",
)


def _mensagem_com_texto(mensagem_id: str, corpo: str) -> dict:
    return {
        "from": WA_ID,
        "id": mensagem_id,
        "timestamp": "1749416383",
        "type": "text",
        "text": {"body": corpo, "preview_url": URL_EXTRA},
        "referral": {"source_url": URL_EXTRA},
    }


def test_webhook_textual_cria_evento_e_um_conteudo(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    client = _cliente(app)
    corpo = _envelope(
        _valor_mensagem(_mensagem_com_texto("wamid.CONTEUDO1", f"  {TEXTO_MINIMO}  ")),
        extra={"app_secret": APP_SECRET, "access_token": TOKEN_ACESSO},
    )
    assinatura = _assinar(corpo)
    assert _post(client, corpo, assinatura=assinatura).status_code == 200
    evento = EventoCanalRecebido.query.one()
    assert evento.tipo_evento == EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL
    assert evento.evento_externo_id == "wamid.CONTEUDO1"
    conteudos = ConteudoTextualCanal.query.all()
    assert len(conteudos) == 1
    assert conteudos[0].evento_id == evento.id
    assert conteudos[0].texto == TEXTO_MINIMO
    assert {coluna.name for coluna in ConteudoTextualCanal.__table__.columns} == {
        "id",
        "evento_id",
        "texto",
    }
    assert TEXTO_MINIMO not in _texto_persistido()
    _proibir(
        NOME_PERFIL,
        DISPLAY_PHONE,
        TOKEN_ACESSO,
        APP_SECRET,
        assinatura,
        URL_EXTRA,
        "pricing",
        "billable",
    )
    for trecho in (NOME_PERFIL, DISPLAY_PHONE, TOKEN_ACESSO, APP_SECRET, URL_EXTRA, LEGENDA):
        assert trecho not in conteudos[0].texto


def test_replay_nao_duplica_nem_sobrescreve_conteudo(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    client = _cliente(app)
    primeiro = _envelope(
        _valor_mensagem(_mensagem_com_texto("wamid.REPLAYTXT", TEXTO_MINIMO))
    )
    divergente = _envelope(
        _valor_mensagem(_mensagem_com_texto("wamid.REPLAYTXT", "texto divergente"))
    )
    assert _post(client, primeiro).status_code == 200
    resposta = _post(client, divergente)
    assert resposta.status_code == 200
    assert resposta.get_json()["recebidos"] == 0
    assert resposta.get_json()["replays"] == 1
    assert EventoCanalRecebido.query.count() == 1
    conteudos = ConteudoTextualCanal.query.all()
    assert len(conteudos) == 1
    assert conteudos[0].texto == TEXTO_MINIMO
    assert "texto divergente" not in conteudos[0].texto


def test_falha_de_conteudo_reverte_evento_textual(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    client = _cliente(app)
    valido = _envelope(
        _valor_mensagem(_mensagem_com_texto("wamid.VALIDO", TEXTO_MINIMO))
    )
    assert _post(client, valido).status_code == 200
    longo = _envelope(
        _valor_mensagem(
            _mensagem_com_texto(
                "wamid.LONGO",
                "x" * (ConteudoTextualCanal.TEXTO_MAXIMO + 1),
            )
        )
    )
    resposta = _post(client, longo)
    assert resposta.status_code == 500
    assert resposta.get_json()["codigo"] == "falha_interna"
    assert EventoCanalRecebido.query.count() == 1
    assert EventoCanalRecebido.query.one().evento_externo_id == "wamid.VALIDO"
    assert ConteudoTextualCanal.query.count() == 1
    assert ConteudoTextualCanal.query.one().texto == TEXTO_MINIMO
    seguinte = _envelope(
        _valor_mensagem(_mensagem_com_texto("wamid.DEPOISLONGO", TEXTO_MINIMO))
    )
    assert _post(client, seguinte).status_code == 200
    assert EventoCanalRecebido.query.count() == 2
    assert ConteudoTextualCanal.query.count() == 2


def test_midia_nao_cria_conteudo_textual(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    client = _cliente(app)
    corpo = _envelope(_valor_mensagem({
        "from": WA_ID,
        "id": "wamid.MIDIACONTEUDO",
        "timestamp": "1744344496",
        "type": "image",
        "image": {
            "caption": LEGENDA,
            "mime_type": "image/jpeg",
            "id": "1003383421387256",
            "url": URL_EXTRA,
        },
    }))
    assert _post(client, corpo).status_code == 200
    assert EventoCanalRecebido.query.one().tipo_evento == EventoCanalRecebido.TIPO_MIDIA
    assert ConteudoTextualCanal.query.count() == 0
    _proibir(LEGENDA, URL_EXTRA)


def test_status_nao_cria_conteudo_textual(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    client = _cliente(app)
    corpo = _envelope(_valor_status({
        "id": "wamid.STATUSCONTEUDO",
        "status": "delivered",
        "timestamp": "1750263773",
        "recipient_id": WA_ID,
        "pricing": {"billable": True, "pricing_model": "PMP", "category": "service"},
    }))
    assert _post(client, corpo).status_code == 200
    assert EventoCanalRecebido.query.one().tipo_evento == EventoCanalRecebido.TIPO_STATUS_ENTREGA
    assert ConteudoTextualCanal.query.count() == 0
    _proibir("billable", "pricing_model")


def test_webhook_textual_processa_sem_preparo_manual(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    client = _cliente(app)
    corpo = _envelope(
        _valor_mensagem(_mensagem_com_texto("wamid.PONTA", TEXTO_MINIMO))
    )
    assert _post(client, corpo).status_code == 200
    assert EventoCanalRecebido.query.count() == 1
    assert ConteudoTextualCanal.query.one().texto == TEXTO_MINIMO
    assert IdentidadeCanalExterna.query.count() == 1
    evento = EventoCanalRecebido.query.one()
    assert evento.sujeito_externo == WA_ID
    assert evento.contexto_destino == PHONE_NUMBER_ID
    assert evento.status_processamento == EventoCanalRecebido.STATUS_ROTEADO
    resultado = processar_evento_canal_recebido(evento.id)
    assert resultado.codigo == CODIGO_JA_PROCESSADO
    ident = IdentidadeCanalExterna.query.one()
    assert ident.estado == IdentidadeCanalExterna.ESTADO_GUEST
    assert ident.provedor == "whatsapp_meta"
    assert ident.sujeito_externo == WA_ID
    assert ident.contexto_destino == PHONE_NUMBER_ID
    assert ident.user_id is None
    assert User.query.count() == 0
    assert IaConsumoEvento.query.count() == 0
    assert IaBillingCostSnapshot.query.count() == 0
    assert CleitonBillingApropriacao.query.count() == 0
    for nome in (
        "whatsapp_meta_webhook_service.py",
        "canal_entrada_processamento_service.py",
    ):
        fonte = (ROOT / "app" / "services" / nome).read_text(encoding="utf-8")
        for trecho in _SAIDA_PROIBIDA:
            assert trecho not in fonte
    webhook = (ROOT / "app" / "services" / "whatsapp_meta_webhook_service.py").read_text(
        encoding="utf-8"
    )
    assert "processar_evento_canal_recebido" not in webhook
