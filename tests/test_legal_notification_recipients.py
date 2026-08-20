"""Destinatários de notificações jurídicas de Termo e Política de Privacidade."""
from __future__ import annotations

import inspect
import io
import logging
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from werkzeug.datastructures import FileStorage

from app.extensions import db
from app.models import CommunicationSuppression, PrivacyPolicy, TermsOfUse, User
from app.services import communication_suppression_service as suppression
from app.services.legal_notification_eligibility import (
    REASON_CLOSED,
    REASON_ELIGIBLE,
    REASON_INVALID_EMAIL,
    can_receive_legal_notification,
    classify_legal_notification_recipient,
    is_technical_closure_placeholder_email,
)
from app.services.user_operational_state import (
    email_operacional_apos_encerramento,
    is_user_operationally_closed,
)
from tests.conftest import seed_conta_franquia_cliente, seed_usuario


HMAC_SECRET = "legal-notification-test-secret-32bytes"


def _patch_legal_data_dir(monkeypatch, tmp_path: Path) -> None:
    import app.legal_document_storage as legal_storage

    monkeypatch.setattr(
        legal_storage,
        "settings",
        SimpleNamespace(data_dir=str(tmp_path)),
    )


def _pdf_upload(filename: str = "documento.pdf") -> FileStorage:
    return FileStorage(
        stream=io.BytesIO(b"%PDF-1.4 conteudo"),
        filename=filename,
        content_type="application/pdf",
    )


def _make_user(email: str, *, slug: str | None = None) -> User:
    slug = slug or email.split("@")[0].replace(".", "-").replace("_", "-")
    conta, franquia = seed_conta_franquia_cliente(slug=f"conta-{slug}")
    user = seed_usuario(franquia.id, conta.id, email=email)
    return user


def _enable_suppression(app) -> None:
    app.config["COMMUNICATION_SUPPRESSION_HMAC_SECRET"] = HMAC_SECRET


def _register_terms_route(app) -> None:
    if "terms_of_use" not in app.view_functions:
        app.add_url_rule(
            "/termos-de-uso",
            endpoint="terms_of_use",
            view_func=lambda: "ok",
        )


def _register_privacy_route(app) -> None:
    if "privacy_policy" not in app.view_functions:
        app.add_url_rule(
            "/politica-de-privacidade",
            endpoint="privacy_policy",
            view_func=lambda: "ok",
        )


class _FailingExecutor:
    def submit(self, *_args, **_kwargs):
        raise RuntimeError("executor indisponivel no teste")


def _run_termo_upload(app, monkeypatch, tmp_path, send_fn):
    from app.services import termo_service

    _patch_legal_data_dir(monkeypatch, tmp_path)
    _register_terms_route(app)
    monkeypatch.setattr(termo_service, "send_terms_updated_notification", send_fn)
    with app.test_request_context("/", base_url="https://agentefrete.test"):
        return termo_service.processar_upload_termo(app, _pdf_upload("termo.pdf"))


def _run_privacy_notify(app, monkeypatch, send_fn):
    from app.services import privacy_policy_service

    monkeypatch.setattr(
        privacy_policy_service,
        "send_privacy_policy_updated_notification",
        send_fn,
    )
    return privacy_policy_service._notify_privacy_policy_update(
        app,
        "https://agentefrete.test/politica-de-privacidade",
        datetime(2026, 8, 19, 12, 0, 0),
    )


def _run_privacy_upload(app, monkeypatch, tmp_path, send_fn):
    from app.services import privacy_policy_service

    _patch_legal_data_dir(monkeypatch, tmp_path)
    _register_privacy_route(app)
    monkeypatch.setattr(
        privacy_policy_service,
        "get_admin_executor",
        lambda: _FailingExecutor(),
    )
    monkeypatch.setattr(
        privacy_policy_service,
        "send_privacy_policy_updated_notification",
        send_fn,
    )
    with app.test_request_context("/", base_url="https://agentefrete.test"):
        return privacy_policy_service.processar_upload_privacy_policy(
            app,
            _pdf_upload("politica.pdf"),
            uploaded_by_user_id=None,
        )


# --- Helper compartilhado ---


def test_helper_user_normal_com_email_valido_e_elegivel(ctx):
    user = _make_user("normal@test.com")
    decision = classify_legal_notification_recipient(user)
    assert decision.eligible is True
    assert decision.reason == REASON_ELIGIBLE
    assert decision.email == "normal@test.com"
    assert can_receive_legal_notification(user) is True


def test_helper_rejeita_encerrado_desidentificado(ctx):
    user = _make_user("vai.encerrar@test.com")
    user.email = email_operacional_apos_encerramento(user.id)
    db.session.commit()
    assert is_user_operationally_closed(user) is True
    decision = classify_legal_notification_recipient(user)
    assert decision.eligible is False
    assert decision.reason == REASON_CLOSED
    assert can_receive_legal_notification(user) is False


def test_helper_rejeita_placeholder_tecnico_mesmo_com_id_divergente(ctx):
    user = _make_user("placeholder@test.com")
    user.email = "encerrado_999999@anon.local"
    db.session.commit()
    assert is_user_operationally_closed(user) is False
    assert is_technical_closure_placeholder_email(user.email) is True
    decision = classify_legal_notification_recipient(user)
    assert decision.eligible is False
    assert decision.reason == REASON_CLOSED


def test_helper_rejeita_email_ausente_vazio_e_whitespace(ctx):
    none_user = User(full_name="Sem Email", conta_id=1, franquia_id=1)
    none_user.id = 1
    none_user.email = None
    assert classify_legal_notification_recipient(none_user).reason == REASON_INVALID_EMAIL
    assert can_receive_legal_notification(none_user) is False

    empty = _make_user("vazio@test.com")
    empty.email = ""
    db.session.commit()
    assert classify_legal_notification_recipient(empty).reason == REASON_INVALID_EMAIL

    blank = _make_user("branco@test.com")
    blank.email = "   "
    db.session.commit()
    assert classify_legal_notification_recipient(blank).reason == REASON_INVALID_EMAIL


def test_helper_nao_depende_de_newsletter(ctx):
    user = _make_user("sem.news@test.com")
    user.subscribes_to_newsletter = False
    db.session.commit()
    assert can_receive_legal_notification(user) is True
    user.subscribes_to_newsletter = True
    db.session.commit()
    assert can_receive_legal_notification(user) is True


def test_helper_nao_consulta_newsletter_nem_suppression():
    from app.services import legal_notification_eligibility as module

    helper_src = inspect.getsource(classify_legal_notification_recipient)
    assert "subscribes_to_newsletter" not in helper_src
    assert "is_email_suppressed" not in helper_src
    assert "NewsletterSubscription" not in helper_src
    assert "communication_suppression" not in inspect.getsource(module)
    assert "newsletter_subscription" not in inspect.getsource(module)


# --- Termo ---


def test_termo_user_normal_recebe(app, ctx, monkeypatch, tmp_path):
    user = _make_user("termo.ok@test.com")
    captured = []
    sent, failed = _run_termo_upload(
        app, monkeypatch, tmp_path, lambda email, _n, _u: captured.append(email)
    )
    assert sent == 1
    assert failed == 0
    assert captured == [user.email]


def test_termo_encerrado_nao_recebe(app, ctx, monkeypatch, tmp_path):
    user = _make_user("termo.encerrado@test.com")
    user.email = email_operacional_apos_encerramento(user.id)
    db.session.commit()
    captured = []
    sent, failed = _run_termo_upload(
        app, monkeypatch, tmp_path, lambda email, _n, _u: captured.append(email)
    )
    assert sent == 0
    assert failed == 0
    assert captured == []
    assert TermsOfUse.query.filter_by(is_active=True).one() is not None


def test_termo_placeholder_anon_nao_recebe(app, ctx, monkeypatch, tmp_path):
    user = _make_user("termo.placeholder@test.com")
    user.email = "encerrado_888888@anon.local"
    db.session.commit()
    captured = []
    sent, failed = _run_termo_upload(
        app, monkeypatch, tmp_path, lambda email, _n, _u: captured.append(email)
    )
    assert sent == 0
    assert failed == 0
    assert captured == []


def test_termo_email_vazio_nao_recebe(app, ctx, monkeypatch, tmp_path):
    user = _make_user("termo.vazio@test.com")
    user.email = ""
    db.session.commit()
    captured = []
    sent, failed = _run_termo_upload(
        app, monkeypatch, tmp_path, lambda email, _n, _u: captured.append(email)
    )
    assert sent == 0
    assert failed == 0
    assert captured == []


def test_termo_sem_newsletter_ainda_recebe(app, ctx, monkeypatch, tmp_path):
    user = _make_user("termo.sem.news@test.com")
    user.subscribes_to_newsletter = False
    db.session.commit()
    captured = []
    sent, failed = _run_termo_upload(
        app, monkeypatch, tmp_path, lambda email, _n, _u: captured.append(email)
    )
    assert sent == 1
    assert failed == 0
    assert captured == [user.email]


def test_termo_com_suppression_de_marketing_ainda_recebe(app, ctx, monkeypatch, tmp_path):
    _enable_suppression(app)
    user = _make_user("termo.suppressed@test.com")
    assert suppression.suppress_email(
        user.email,
        suppression.PURPOSE_PRE_REGISTRATION,
        suppression.SOURCE_CAMPAIGN_UNSUBSCRIBE,
    )
    assert suppression.suppress_email(
        user.email,
        suppression.PURPOSE_ACTIVATION,
        suppression.SOURCE_ACTIVATION_UNSUBSCRIBE,
    )
    assert CommunicationSuppression.query.count() == 2
    captured = []
    sent, failed = _run_termo_upload(
        app, monkeypatch, tmp_path, lambda email, _n, _u: captured.append(email)
    )
    assert sent == 1
    assert failed == 0
    assert captured == [user.email]


def test_termo_falha_individual_nao_bloqueia_demais(app, ctx, monkeypatch, tmp_path):
    ok = _make_user("termo.ok.cont@test.com")
    fail = _make_user("termo.fail@test.com")
    later = _make_user("termo.later@test.com")
    captured = []

    def _send(email, _name, _url):
        if email == fail.email:
            raise RuntimeError("smtp indisponivel")
        captured.append(email)

    sent, failed = _run_termo_upload(app, monkeypatch, tmp_path, _send)
    assert sent == 2
    assert failed == 1
    assert ok.email in captured
    assert later.email in captured
    assert fail.email not in captured


def test_termo_falha_de_email_mantem_documento_ativo(app, ctx, monkeypatch, tmp_path):
    _make_user("termo.ativo@test.com")

    def _send(_email, _name, _url):
        raise RuntimeError("falha total de smtp")

    sent, failed = _run_termo_upload(app, monkeypatch, tmp_path, _send)
    assert sent == 0
    assert failed == 1
    active = TermsOfUse.query.filter_by(is_active=True).one()
    assert active.filename


def test_termo_logs_distinguem_contagens_sem_email_completo(
    app, ctx, monkeypatch, tmp_path, caplog
):
    ok = _make_user("termo.log.ok@test.com")
    closed = _make_user("termo.log.closed@test.com")
    closed.email = email_operacional_apos_encerramento(closed.id)
    invalid = _make_user("termo.log.invalid@test.com")
    invalid.email = ""
    db.session.commit()
    with caplog.at_level(logging.INFO, logger="app.services.termo_service"):
        sent, failed = _run_termo_upload(
            app, monkeypatch, tmp_path, lambda *_a, **_k: None
        )
    assert sent == 1
    assert failed == 0
    text = caplog.text
    assert "total_usuarios_encontrados=3" in text
    assert "elegiveis=1" in text
    assert "enviados=1" in text
    assert "falhas=0" in text
    assert "ignorados_encerrados=1" in text
    assert "ignorados_email_invalido=1" in text
    assert ok.email not in text


# --- Política ---


def test_politica_user_normal_recebe(app, ctx, monkeypatch):
    user = _make_user("pol.ok@test.com")
    captured = []
    sent, failed = _run_privacy_notify(
        app, monkeypatch, lambda **kwargs: captured.append(kwargs["user_email"])
    )
    assert sent == 1
    assert failed == 0
    assert captured == [user.email]


def test_politica_encerrado_nao_recebe(app, ctx, monkeypatch):
    user = _make_user("pol.encerrado@test.com")
    user.email = email_operacional_apos_encerramento(user.id)
    db.session.commit()
    captured = []
    sent, failed = _run_privacy_notify(
        app, monkeypatch, lambda **kwargs: captured.append(kwargs["user_email"])
    )
    assert sent == 0
    assert failed == 0
    assert captured == []


def test_politica_placeholder_anon_nao_recebe(app, ctx, monkeypatch):
    user = _make_user("pol.placeholder@test.com")
    user.email = "encerrado_777777@anon.local"
    db.session.commit()
    captured = []
    sent, failed = _run_privacy_notify(
        app, monkeypatch, lambda **kwargs: captured.append(kwargs["user_email"])
    )
    assert sent == 0
    assert failed == 0
    assert captured == []


def test_politica_email_vazio_nao_recebe(app, ctx, monkeypatch):
    user = _make_user("pol.vazio@test.com")
    user.email = "   "
    db.session.commit()
    captured = []
    sent, failed = _run_privacy_notify(
        app, monkeypatch, lambda **kwargs: captured.append(kwargs["user_email"])
    )
    assert sent == 0
    assert failed == 0
    assert captured == []


def test_politica_sem_newsletter_ainda_recebe(app, ctx, monkeypatch):
    user = _make_user("pol.sem.news@test.com")
    user.subscribes_to_newsletter = False
    db.session.commit()
    captured = []
    sent, failed = _run_privacy_notify(
        app, monkeypatch, lambda **kwargs: captured.append(kwargs["user_email"])
    )
    assert sent == 1
    assert failed == 0
    assert captured == [user.email]


def test_politica_com_suppression_de_marketing_ainda_recebe(app, ctx, monkeypatch):
    _enable_suppression(app)
    user = _make_user("pol.suppressed@test.com")
    assert suppression.suppress_email(
        user.email,
        suppression.PURPOSE_PRE_REGISTRATION,
        suppression.SOURCE_CAMPAIGN_UNSUBSCRIBE,
    )
    assert suppression.suppress_email(
        user.email,
        suppression.PURPOSE_ACTIVATION,
        suppression.SOURCE_ACTIVATION_UNSUBSCRIBE,
    )
    assert CommunicationSuppression.query.count() == 2
    captured = []
    sent, failed = _run_privacy_notify(
        app, monkeypatch, lambda **kwargs: captured.append(kwargs["user_email"])
    )
    assert sent == 1
    assert failed == 0
    assert captured == [user.email]


def test_politica_falha_individual_nao_bloqueia_demais(app, ctx, monkeypatch):
    ok = _make_user("pol.ok.cont@test.com")
    fail = _make_user("pol.fail@test.com")
    later = _make_user("pol.later@test.com")
    captured = []

    def _send(**kwargs):
        if kwargs["user_email"] == fail.email:
            raise RuntimeError("smtp indisponivel")
        captured.append(kwargs["user_email"])

    sent, failed = _run_privacy_notify(app, monkeypatch, _send)
    assert sent == 2
    assert failed == 1
    assert ok.email in captured
    assert later.email in captured
    assert fail.email not in captured


def test_politica_falha_de_email_mantem_documento_ativo(app, ctx, monkeypatch, tmp_path):
    _make_user("pol.ativo@test.com")

    def _send(**_kwargs):
        raise RuntimeError("falha total de smtp")

    active, sent, failed, mode = _run_privacy_upload(app, monkeypatch, tmp_path, _send)
    assert mode == "sync_fallback"
    assert sent == 0
    assert failed == 1
    assert active is not None
    assert active.is_active is True
    persisted = PrivacyPolicy.query.filter_by(is_active=True).one()
    assert persisted.id == active.id


def test_politica_logs_distinguem_contagens_sem_email_completo(
    app, ctx, monkeypatch, caplog
):
    ok = _make_user("pol.log.ok@test.com")
    closed = _make_user("pol.log.closed@test.com")
    closed.email = email_operacional_apos_encerramento(closed.id)
    invalid = _make_user("pol.log.invalid@test.com")
    invalid.email = ""
    db.session.commit()
    with caplog.at_level(logging.INFO, logger="app.services.privacy_policy_service"):
        sent, failed = _run_privacy_notify(app, monkeypatch, lambda **_k: None)
    assert sent == 1
    assert failed == 0
    text = caplog.text
    assert "total_usuarios_encontrados=3" in text
    assert "elegiveis=1" in text
    assert "enviados=1" in text
    assert "falhas=0" in text
    assert "ignorados_encerrados=1" in text
    assert "ignorados_email_invalido=1" in text
    assert ok.email not in text


def test_politica_preserva_commit_antes_do_disparo(app, ctx, monkeypatch, tmp_path):
    _make_user("pol.commit@test.com")
    notify_seen_active = {}

    def _send(**_kwargs):
        notify_seen_active["count"] = PrivacyPolicy.query.filter_by(is_active=True).count()
        notify_seen_active["id"] = PrivacyPolicy.query.filter_by(is_active=True).one().id

    active, sent, failed, mode = _run_privacy_upload(app, monkeypatch, tmp_path, _send)
    assert mode == "sync_fallback"
    assert sent == 1
    assert failed == 0
    assert notify_seen_active["count"] == 1
    assert notify_seen_active["id"] == active.id


# --- Mesmo critério ---


def test_termo_e_politica_usam_o_mesmo_criterio_de_elegibilidade():
    from app.services import privacy_policy_service, termo_service

    assert (
        termo_service.classify_legal_notification_recipient
        is classify_legal_notification_recipient
    )
    assert (
        privacy_policy_service.classify_legal_notification_recipient
        is classify_legal_notification_recipient
    )
    termo_src = inspect.getsource(termo_service.processar_upload_termo)
    privacy_src = inspect.getsource(privacy_policy_service._notify_privacy_policy_update)
    assert "classify_legal_notification_recipient" in termo_src
    assert "classify_legal_notification_recipient" in privacy_src
    assert "subscribes_to_newsletter" not in termo_src
    assert "subscribes_to_newsletter" not in privacy_src
    assert "CommunicationSuppression" not in termo_src
    assert "CommunicationSuppression" not in privacy_src
    assert "is_email_suppressed" not in termo_src
    assert "is_email_suppressed" not in privacy_src


def test_termo_e_politica_selecionam_os_mesmos_destinatarios(app, ctx, monkeypatch, tmp_path):
    normal = _make_user("mix.normal@test.com")
    no_news = _make_user("mix.nonews@test.com")
    no_news.subscribes_to_newsletter = False
    closed = _make_user("mix.closed@test.com")
    closed.email = email_operacional_apos_encerramento(closed.id)
    placeholder = _make_user("mix.placeholder@test.com")
    placeholder.email = "encerrado_123456@anon.local"
    empty = _make_user("mix.empty@test.com")
    empty.email = ""
    db.session.commit()

    termo_captured = []
    _run_termo_upload(
        app, monkeypatch, tmp_path, lambda email, _n, _u: termo_captured.append(email)
    )
    privacy_captured = []
    _run_privacy_notify(
        app, monkeypatch, lambda **kwargs: privacy_captured.append(kwargs["user_email"])
    )

    expected = {normal.email, no_news.email}
    assert set(termo_captured) == expected
    assert set(privacy_captured) == expected
