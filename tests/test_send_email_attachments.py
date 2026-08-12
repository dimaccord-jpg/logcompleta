"""Testes focados da extensão retrocompatível de send_email (attachments/headers)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app import auth_services


def test_send_email_caller_antigo_sem_attachments(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("MAIL_FROM", "noreply@agentefrete.com.br")
    response = MagicMock()
    response.status_code = 200
    response.text = "ok"
    with patch.object(auth_services.requests, "post", return_value=response) as post:
        auth_services.send_email(
            to_email="a@b.com",
            subject="Assunto",
            html="<p>oi</p>",
            text="oi",
        )
    payload = post.call_args.kwargs["json"]
    assert "attachments" not in payload
    assert "headers" not in payload
    assert payload["text"] == "oi"
    assert payload["from"].startswith("Agentefrete <")


def test_send_email_com_attachment_cid_e_headers(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("MAIL_FROM", "noreply@agentefrete.com.br")
    response = MagicMock()
    response.status_code = 200
    response.text = "ok"
    attachments = [
        {
            "filename": "hero.png",
            "content": "YWJj",
            "content_id": "hero",
            "content_type": "image/png",
        }
    ]
    headers = {
        "List-Unsubscribe": "<https://example.test/unsub>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
    with patch.object(auth_services.requests, "post", return_value=response) as post:
        auth_services.send_email(
            to_email="a@b.com",
            subject="Assunto",
            html='<img src="cid:hero">',
            text="oi",
            attachments=attachments,
            headers=headers,
        )
    payload = post.call_args.kwargs["json"]
    assert payload["attachments"] == attachments
    assert payload["headers"] == headers


def test_send_email_falha_http_ge_400(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    response = MagicMock()
    response.status_code = 422
    response.text = "bad"
    with patch.object(auth_services.requests, "post", return_value=response):
        with pytest.raises(RuntimeError):
            auth_services.send_email(
                to_email="a@b.com",
                subject="x",
                html="<p>x</p>",
            )


def test_send_email_falha_rede(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    with patch.object(
        auth_services.requests,
        "post",
        side_effect=auth_services.requests.RequestException("down"),
    ):
        with pytest.raises(RuntimeError):
            auth_services.send_email(
                to_email="a@b.com",
                subject="x",
                html="<p>x</p>",
            )
