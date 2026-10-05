"""Diagnóstico seguro do HTTP 4xx da Meta Cloud API.

O retorno funcional permanece http_4xx. O log não leva token, telefone,
texto, payload nem o corpo bruto.
"""
from __future__ import annotations

import json
import logging

from app.services.whatsapp_meta_cloud_api_adapter import (
    CODIGO_HTTP_4XX,
    WhatsAppMetaCloudApiAdapter,
)
from app.services.whatsapp_meta_config import ConfigEnvioWhatsApp

TOKEN = "EAATokenDiagnostico4xxNaoPodeAparecer"
PHONE_NUMBER_ID = "109988776655443"
DESTINATARIO = "5511988776655"
TEXTO = "mensagem-secreta-diagnostico-4xx"
MARCADOR_MENSAGEM = "MENSAGEM_META_SENSIVEL_4XX"
MARCADOR_PAYLOAD = "PAYLOAD_BRUTO_META_4XX"
VERSAO = "v25.0"
BASE = "https://graph.facebook.com"


def _config() -> ConfigEnvioWhatsApp:
    return ConfigEnvioWhatsApp(
        access_token=TOKEN,
        graph_api_version=VERSAO,
        base_url=BASE,
        timeout_seconds=4.0,
    )


class _Resposta:
    def __init__(self, status, payload, texto_bruto):
        self.status_code = status
        self._payload = payload
        self.text = texto_bruto
        self.content = texto_bruto.encode("utf-8")
        self.fechada = False

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def close(self):
        self.fechada = True


def _corpo_meta_completo() -> dict:
    return {
        "error": {
            "message": (
                f"{MARCADOR_MENSAGEM} {TOKEN} {PHONE_NUMBER_ID} "
                f"{DESTINATARIO} {TEXTO}"
            ),
            "type": "OAuthException",
            "code": 131030,
            "error_subcode": 2494010,
            "fbtrace_id": "FbTraceDiag4xxA1",
            "error_data": {"details": MARCADOR_PAYLOAD},
        },
        "payload": MARCADOR_PAYLOAD,
    }


def _texto_log(caplog) -> str:
    partes = [caplog.text]
    for registro in caplog.records:
        partes.append(registro.getMessage())
        if registro.args:
            partes.append(repr(registro.args))
    return "\n".join(partes)


def _proibir_segredo(texto_log: str) -> None:
    for proibido in (
        TOKEN,
        PHONE_NUMBER_ID,
        DESTINATARIO,
        TEXTO,
        MARCADOR_MENSAGEM,
        MARCADOR_PAYLOAD,
        "Bearer",
        "Authorization",
        "mensagem-secreta",
    ):
        assert proibido not in texto_log


def _enviar(monkeypatch, resposta):
    chamadas = []

    def _post(url, **kwargs):
        chamadas.append((url, kwargs))
        return resposta

    monkeypatch.setattr(
        "app.services.whatsapp_meta_cloud_api_adapter.requests.post",
        _post,
    )
    resultado = WhatsAppMetaCloudApiAdapter(_config()).enviar_texto(
        phone_number_id=PHONE_NUMBER_ID,
        destinatario=DESTINATARIO,
        texto=TEXTO,
    )
    return resultado, chamadas


def _assert_pedido_inalterado(chamadas) -> None:
    assert len(chamadas) == 1
    url, kwargs = chamadas[0]
    assert url == f"{BASE}/{VERSAO}/{PHONE_NUMBER_ID}/messages"
    assert kwargs["json"] == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": DESTINATARIO,
        "type": "text",
        "text": {"body": TEXTO},
    }
    assert kwargs["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert kwargs["headers"]["Content-Type"] == "application/json"
    assert kwargs["timeout"] == 4.0
    assert kwargs["allow_redirects"] is False


def test_4xx_json_completo_captura_codigos_e_mantem_http_4xx(monkeypatch, caplog):
    corpo = _corpo_meta_completo()
    resposta = _Resposta(400, corpo, json.dumps(corpo))
    with caplog.at_level(logging.DEBUG):
        resultado, chamadas = _enviar(monkeypatch, resposta)
    assert resultado.codigo_erro == CODIGO_HTTP_4XX
    assert resultado.provider_message_id is None
    assert resultado.aceito is False
    assert resposta.fechada is True
    _assert_pedido_inalterado(chamadas)
    texto = _texto_log(caplog)
    assert "whatsapp_meta_cloud_api erro_http" in texto
    assert "status=400" in texto
    assert "error_code=131030" in texto
    assert "error_subcode=2494010" in texto
    assert "error_type=OAuthException" in texto
    assert "fbtrace_id=FbTraceDiag4xxA1" in texto
    assert "graph_version=v25.0" in texto
    assert "graph_host=graph.facebook.com" in texto
    assert f"text_length={len(TEXTO)}" in texto
    assert "phone_number_id_numeric=true" in texto
    assert "recipient_numeric=true" in texto
    assert "diagnostico_meta_indisponivel" not in texto
    assert "https://" not in texto
    _proibir_segredo(texto)
    assert len([r for r in caplog.records if "erro_http" in r.getMessage()]) == 1


def test_4xx_body_nao_json_mantem_http_4xx(monkeypatch, caplog):
    bruto = f"{TOKEN} {PHONE_NUMBER_ID} {DESTINATARIO} {TEXTO} {MARCADOR_PAYLOAD}"
    resposta = _Resposta(400, ValueError(bruto), bruto)
    with caplog.at_level(logging.DEBUG):
        resultado, chamadas = _enviar(monkeypatch, resposta)
    assert resultado.codigo_erro == CODIGO_HTTP_4XX
    assert resultado.provider_message_id is None
    assert resultado.aceito is False
    assert resposta.fechada is True
    _assert_pedido_inalterado(chamadas)
    texto = _texto_log(caplog)
    assert (
        "whatsapp_meta_cloud_api erro_http status=400 diagnostico_meta_indisponivel=true"
        in texto
    )
    assert "error_code=" not in texto
    assert "graph_version=" not in texto
    assert "text_length=" not in texto
    _proibir_segredo(texto)


def test_4xx_json_inesperado_nao_quebra_e_mantem_http_4xx(monkeypatch, caplog):
    corpo = {"error": f"segredo-4xx {TOKEN} {TEXTO}", "corpo": MARCADOR_PAYLOAD}
    resposta = _Resposta(400, corpo, json.dumps(corpo))
    with caplog.at_level(logging.DEBUG):
        resultado, chamadas = _enviar(monkeypatch, resposta)
    assert resultado.codigo_erro == CODIGO_HTTP_4XX
    assert resultado.provider_message_id is None
    assert resposta.fechada is True
    _assert_pedido_inalterado(chamadas)
    texto = _texto_log(caplog)
    assert (
        "whatsapp_meta_cloud_api erro_http status=400 diagnostico_meta_indisponivel=true"
        in texto
    )
    assert "segredo-4xx" not in texto
    _proibir_segredo(texto)


def test_log_4xx_nao_expoe_token_telefone_texto_nem_payload(monkeypatch, caplog):
    corpo = _corpo_meta_completo()
    resposta = _Resposta(400, corpo, json.dumps(corpo))
    with caplog.at_level(logging.DEBUG):
        resultado, chamadas = _enviar(monkeypatch, resposta)
    assert resultado.codigo_erro == CODIGO_HTTP_4XX
    _assert_pedido_inalterado(chamadas)
    texto = _texto_log(caplog)
    _proibir_segredo(texto)
    assert "error_data" not in texto
    assert "details" not in texto
    for registro in caplog.records:
        assert registro.args is None or TOKEN not in repr(registro.args)
        assert PHONE_NUMBER_ID not in repr(registro.args)
        assert TEXTO not in repr(registro.args)
        assert MARCADOR_PAYLOAD not in repr(registro.args)
