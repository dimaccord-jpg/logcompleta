import hashlib
import hmac
import json
import os
import time

import pytest


# Permite importar app.web em ambiente de teste sem depender da shell externa.
os.environ.setdefault("APP_ENV", "dev")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg2://test_user:test_pass@localhost:5432/test_db",
)

from app import web as web_app  # noqa: E402


def _stripe_signature(raw_payload: bytes, secret: str, timestamp: int | None = None) -> str:
    ts = int(timestamp or time.time())
    signed = f"{ts}.{raw_payload.decode('utf-8')}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test_route")
    return web_app.app.test_client()


def test_webhook_route_assinatura_valida_payload_valido(client, monkeypatch):
    payload = {
        "id": "evt_route_ok",
        "type": "invoice.paid",
        "created": 1713350400,
        "data": {"object": {"id": "in_route_ok"}},
    }
    raw = json.dumps(payload).encode("utf-8")

    def _fake_process_event(evento):
        return {
            "ok": True,
            "replay": False,
            "event_id": evento.get("id"),
            "event_type": evento.get("type"),
            "status_tecnico": "efeito_operacional_aplicado",
            "efeito_operacional_aplicado": True,
        }

    monkeypatch.setattr(
        "app.services.cleiton_monetizacao_service.processar_evento_stripe",
        _fake_process_event,
    )

    resp = client.post(
        "/api/webhook/stripe",
        data=raw,
        headers={
            "Content-Type": "application/json",
            "Stripe-Signature": _stripe_signature(raw, "whsec_test_route"),
        },
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["event_id"] == "evt_route_ok"
    assert body["event_type"] == "invoice.paid"
    assert body["replay"] is False
    assert body["status_tecnico"] == "efeito_operacional_aplicado"
    assert body["efeito_operacional_aplicado"] is True
    assert body["pendente_correlacao"] is False


def test_webhook_route_checkout_completed_registra_sem_efeito(client, monkeypatch):
    payload = {
        "id": "evt_route_checkout_completed",
        "type": "checkout.session.completed",
        "created": 1713350402,
        "data": {"object": {"id": "cs_route_checkout_completed"}},
    }
    raw = json.dumps(payload).encode("utf-8")

    def _fake_process_event(evento):
        return {
            "ok": True,
            "replay": False,
            "event_id": evento.get("id"),
            "event_type": evento.get("type"),
            "status_tecnico": "registrado_sem_efeito_operacional",
            "efeito_operacional_aplicado": False,
        }

    monkeypatch.setattr(
        "app.services.cleiton_monetizacao_service.processar_evento_stripe",
        _fake_process_event,
    )

    resp = client.post(
        "/api/webhook/stripe",
        data=raw,
        headers={
            "Content-Type": "application/json",
            "Stripe-Signature": _stripe_signature(raw, "whsec_test_route"),
        },
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["event_id"] == "evt_route_checkout_completed"
    assert body["event_type"] == "checkout.session.completed"
    assert body["status_tecnico"] == "registrado_sem_efeito_operacional"
    assert body["efeito_operacional_aplicado"] is False


def test_webhook_route_assinatura_invalida(client, monkeypatch):
    payload = {"id": "evt_bad_sig", "type": "invoice.paid", "data": {"object": {}}}
    raw = json.dumps(payload).encode("utf-8")

    def _should_not_be_called(_evento):
        raise AssertionError("processar_evento_stripe nao deveria ser chamado com assinatura invalida")

    monkeypatch.setattr(
        "app.services.cleiton_monetizacao_service.processar_evento_stripe",
        _should_not_be_called,
    )

    resp = client.post(
        "/api/webhook/stripe",
        data=raw,
        headers={
            "Content-Type": "application/json",
            "Stripe-Signature": "t=1,v1=assinatura_invalida",
        },
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["ok"] is False
    assert "assinatura" in (body.get("erro") or "").lower()
    assert body.get("codigo_erro") == "webhook_stripe_requisicao_invalida"


def test_webhook_route_payload_json_invalido(client, monkeypatch):
    raw = b"{payload-invalido"

    def _should_not_be_called(_evento):
        raise AssertionError("processar_evento_stripe nao deveria ser chamado com payload invalido")

    monkeypatch.setattr(
        "app.services.cleiton_monetizacao_service.processar_evento_stripe",
        _should_not_be_called,
    )

    resp = client.post(
        "/api/webhook/stripe",
        data=raw,
        headers={
            "Content-Type": "application/json",
            "Stripe-Signature": _stripe_signature(raw, "whsec_test_route"),
        },
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["ok"] is False
    assert "payload json invalido" in (body.get("erro") or "").lower()
    assert body.get("codigo_erro") == "webhook_stripe_requisicao_invalida"


def test_webhook_route_replay_idempotente_no_contrato_http(client, monkeypatch):
    payload = {
        "id": "evt_route_replay",
        "type": "invoice.payment_failed",
        "created": 1713350401,
        "data": {"object": {"id": "in_route_replay"}},
    }
    raw = json.dumps(payload).encode("utf-8")
    vistos: set[str] = set()

    def _fake_process_event(evento):
        eid = str(evento.get("id"))
        replay = eid in vistos
        vistos.add(eid)
        return {
            "ok": True,
            "replay": replay,
            "event_id": eid,
            "event_type": evento.get("type"),
            "status_tecnico": (
                "efeito_operacional_aplicado"
                if not replay
                else "registrado_sem_efeito_operacional"
            ),
            "efeito_operacional_aplicado": not replay,
        }

    monkeypatch.setattr(
        "app.services.cleiton_monetizacao_service.processar_evento_stripe",
        _fake_process_event,
    )

    headers = {
        "Content-Type": "application/json",
        "Stripe-Signature": _stripe_signature(raw, "whsec_test_route"),
    }
    first = client.post("/api/webhook/stripe", data=raw, headers=headers)
    second = client.post("/api/webhook/stripe", data=raw, headers=headers)

    assert first.status_code == 200
    assert first.get_json()["replay"] is False
    assert first.get_json()["efeito_operacional_aplicado"] is True
    assert second.status_code == 200
    assert second.get_json()["replay"] is True
    assert second.get_json()["efeito_operacional_aplicado"] is False
