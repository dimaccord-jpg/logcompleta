"""LGPD-R1 — exercício de privacidade/LGPD distinto do encerramento contratual."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import (
    CleitonBillingApropriacao,
    CommunicationSuppression,
    Conta,
    ContaMonetizacaoVinculo,
    Franquia,
    FunnelEvent,
    IaConsumoEvento,
    Lead,
    MonetizacaoFato,
    ProcessingEvent,
    User,
)
from app.services.lead_acquisition_service import (
    CAMPANHA_ACESSO_DESKTOP,
    FONTE_LANDING,
)
from app.services import communication_suppression_service as suppression
from app.services.user_lifecycle_service import (
    NOME_OPERACIONAL_APOS_ENCERRAMENTO,
    email_operacional_apos_encerramento,
    encerrar_vinculo_operacional_usuario,
)
from app.services.user_privacy_rights_service import (
    MODE_APPLY,
    MODE_DRY_RUN,
    STATUS_NOT_FOUND,
    STATUS_OK,
    USER_FIELDS_DEIDENTIFIED,
    USER_FIELDS_PRESERVED,
    PrivacyRightsAborted,
    PrivacyRightsResult,
    emit_privacy_rights_cli,
    format_privacy_rights_cli,
    processar_exercicio_privacidade_por_user_id,
    processar_exercicio_privacidade_usuario,
    register_privacy_rights_user_command,
)
from tests.conftest import seed_conta_franquia_cliente, seed_usuario

ACEITE_CONHECIDO = datetime(2026, 3, 14, 15, 22, 51)
LOGIN_CONHECIDO = datetime(2026, 8, 1, 10, 0, 0)
AUDITORIA_CONHECIDA = datetime(2026, 7, 1, 9, 0, 0)
CRIADO_CONHECIDO = datetime(2026, 1, 2, 8, 30, 0)
TRIAL_CONHECIDO = datetime(2026, 2, 1, 0, 0, 0)
HMAC_SECRET = "privacy-rights-test-secret-a-32bytes"

PII_EMAIL = "ana.lgpd@test.com"
PII_NAME = "Ana Silva"
PII_OAUTH_SUB = "sub-ana-lgpd-123"
PII_PASSWORD = "segredo123"


def _montar_usuario_operacional(
    *,
    slug: str,
    email: str,
    categoria: str = "pro",
) -> tuple[Conta, Franquia, User]:
    conta, franquia = seed_conta_franquia_cliente(slug=slug)
    user = seed_usuario(franquia.id, conta.id, email=email, categoria=categoria)
    user.full_name = PII_NAME
    user.set_password(PII_PASSWORD)
    user.oauth_provider = "google"
    user.oauth_sub = PII_OAUTH_SUB
    user.subscribes_to_newsletter = True
    user.job_role = "Analista"
    user.usage_purpose = "Auditoria"
    user.accepted_terms_at = ACEITE_CONHECIDO
    user.last_login_at = LOGIN_CONHECIDO
    user.first_audit_completed_at = AUDITORIA_CONHECIDA
    user.created_at = CRIADO_CONHECIDO
    user.trial_start_date = TRIAL_CONHECIDO
    user.is_admin = False
    user.creditos = 42
    db.session.commit()
    return conta, franquia, user


def _make_converted_lead_for_user(user: User, *, email: str) -> Lead:
    captured = datetime(2026, 7, 1, 10, 0, 0)
    lead = Lead(
        email=email,
        acquisition_campaign=CAMPANHA_ACESSO_DESKTOP,
        acquisition_source=FONTE_LANDING,
        campaign_captured_at=captured,
        converted_user_id=user.id,
        converted_at=captured,
        followup_count=0,
    )
    db.session.add(lead)
    db.session.commit()
    return lead


def _enable_suppression(app, secret: str = HMAC_SECRET) -> None:
    app.config["COMMUNICATION_SUPPRESSION_HMAC_SECRET"] = secret


def _snapshot_user(user: User) -> dict:
    return {name: getattr(user, name) for name in (*USER_FIELDS_DEIDENTIFIED, *USER_FIELDS_PRESERVED)}


def _snapshot_lead(lead: Lead) -> dict:
    return {
        "id": lead.id,
        "email": lead.email,
        "converted_user_id": lead.converted_user_id,
        "converted_at": lead.converted_at,
        "opt_out_at": lead.opt_out_at,
        "activation_opt_out_at": lead.activation_opt_out_at,
        "activation_ended_at": lead.activation_ended_at,
        "activation_ended_for_user_id": lead.activation_ended_for_user_id,
        "activation_email_1_sent_at": lead.activation_email_1_sent_at,
        "activation_email_2_sent_at": lead.activation_email_2_sent_at,
    }


def _snapshot_suppression(row: CommunicationSuppression) -> dict:
    return {
        "id": row.id,
        "email_hmac": row.email_hmac,
        "purpose": row.purpose,
        "suppressed_at": row.suppressed_at,
        "source": row.source,
        "created_at": row.created_at,
    }


def _assert_no_pii(text: str, *emails: str) -> None:
    lowered = text.lower()
    for email in emails:
        assert email.lower() not in lowered
    assert PII_NAME.lower() not in lowered
    assert PII_OAUTH_SUB.lower() not in lowered
    assert PII_PASSWORD.lower() not in lowered
    assert HMAC_SECRET not in text
    assert "password_hash=" not in lowered


def _seed_historico(conta: Conta, franquia: Franquia, user: User) -> dict[str, int]:
    funnel = FunnelEvent(
        user_id=user.id,
        conta_id=conta.id,
        franquia_id=franquia.id,
        event_name="file_uploaded",
        source="web",
        idempotency_key=f"privacy-funnel-{user.id}",
        metadata_json={"note": "keep"},
    )
    processing = ProcessingEvent(
        agent="agente_compara",
        flow_type="agente_compara_batch_upload",
        processing_type="non_llm",
        rows_processed=3,
        processing_time_ms=10,
        status="success",
        error_summary="ok",
        usuario_id=user.id,
        conta_id=conta.id,
        franquia_id=franquia.id,
    )
    consumo = IaConsumoEvento(
        provider="gemini",
        operation="generate",
        model="test-model",
        agent="julia",
        flow_type="chat",
        api_key_label="k",
        status="ok",
        error_summary="ok",
        usuario_id=user.id,
        conta_id=conta.id,
        franquia_id=franquia.id,
    )
    vinculo = ContaMonetizacaoVinculo(
        conta_id=conta.id,
        provider="stripe",
        customer_id="cus_privacy",
        subscription_id="sub_privacy",
        ativo=True,
        snapshot_normalizado_json="{}",
        payload_bruto_sanitizado_json="{}",
    )
    fato = MonetizacaoFato(
        tipo_fato="stripe_invoice_paid",
        status_tecnico="aplicado",
        provider="stripe",
        conta_id=conta.id,
        franquia_id=franquia.id,
        usuario_id=user.id,
        customer_id="cus_privacy",
        subscription_id="sub_privacy",
        invoice_id="in_privacy",
        snapshot_normalizado_json="{}",
        payload_bruto_sanitizado_json="{}",
    )
    apropriacao = CleitonBillingApropriacao(
        idempotency_key=f"privacy-apropriacao-{user.id}",
        agent="agente_compara",
        flow_type="upload",
        status="ok",
        error_summary="ok",
        usuario_id=user.id,
        conta_id=conta.id,
        franquia_id=franquia.id,
    )
    db.session.add_all([funnel, processing, consumo, vinculo, fato, apropriacao])
    db.session.commit()
    return {
        "funnel": funnel.id,
        "processing": processing.id,
        "consumo": consumo.id,
        "vinculo": vinculo.id,
        "fato": fato.id,
        "apropriacao": apropriacao.id,
    }


def test_inventario_campos_user_esta_completo():
    mapped = {column.name for column in User.__table__.columns}
    known = set(USER_FIELDS_DEIDENTIFIED) | set(USER_FIELDS_PRESERVED)
    assert mapped == known
    assert not (set(USER_FIELDS_DEIDENTIFIED) & set(USER_FIELDS_PRESERVED))


def test_dry_run_nao_altera_nada(ctx):
    conta, franquia, user = _montar_usuario_operacional(
        slug="priv-dry",
        email=PII_EMAIL,
    )
    lead = _make_converted_lead_for_user(user, email=PII_EMAIL)
    ids = _seed_historico(conta, franquia, user)
    user_before = _snapshot_user(user)
    lead_before = _snapshot_lead(lead)

    with patch.object(db.session, "commit", wraps=db.session.commit) as commit_mock:
        result = processar_exercicio_privacidade_usuario(user, apply=False)

    assert result.mode == MODE_DRY_RUN
    assert result.status == STATUS_OK
    assert result.user_deidentified is False
    assert result.activation_journeys_ended == 1
    assert commit_mock.call_count == 0

    persisted = db.session.get(User, user.id)
    persisted_lead = db.session.get(Lead, lead.id)
    for campo, valor in user_before.items():
        assert getattr(persisted, campo) == valor
    for campo, valor in lead_before.items():
        assert getattr(persisted_lead, campo) == valor
    assert db.session.get(FunnelEvent, ids["funnel"]) is not None
    assert CommunicationSuppression.query.count() == 0


def test_apply_mantem_user_row_e_desidentifica(ctx):
    _conta, _franquia, user = _montar_usuario_operacional(
        slug="priv-apply-row",
        email="apply.row@test.com",
    )
    uid = user.id
    result = processar_exercicio_privacidade_usuario(user, apply=True)
    persisted = db.session.get(User, uid)

    assert result.mode == MODE_APPLY
    assert result.status == STATUS_OK
    assert result.user_deidentified is True
    assert persisted is not None
    assert User.query.count() == 1
    assert persisted.email == email_operacional_apos_encerramento(uid)
    assert persisted.full_name == NOME_OPERACIONAL_APOS_ENCERRAMENTO
    assert persisted.password_hash is None
    assert persisted.oauth_provider is None
    assert persisted.oauth_sub is None
    assert persisted.subscribes_to_newsletter is False
    assert persisted.job_role is None
    assert persisted.usage_purpose is None


def test_apply_preserva_accepted_terms_e_estruturais(ctx):
    conta, franquia, user = _montar_usuario_operacional(
        slug="priv-aceite",
        email="aceite.lgpd@test.com",
        categoria="starter",
    )
    uid = user.id
    processar_exercicio_privacidade_usuario(user, apply=True)
    persisted = db.session.get(User, uid)
    assert persisted.accepted_terms_at == ACEITE_CONHECIDO
    assert persisted.id == uid
    assert persisted.conta_id == conta.id
    assert persisted.franquia_id == franquia.id
    assert persisted.categoria == "starter"
    assert persisted.created_at == CRIADO_CONHECIDO
    assert persisted.last_login_at == LOGIN_CONHECIDO
    assert persisted.first_audit_completed_at == AUDITORIA_CONHECIDA
    assert persisted.trial_start_date == TRIAL_CONHECIDO
    assert persisted.creditos == 42
    assert persisted.is_admin is False


def test_apply_preserva_conta_franquia_e_outro_user(ctx):
    conta, franquia, user_a = _montar_usuario_operacional(
        slug="priv-multi",
        email="user.a.lgpd@test.com",
    )
    user_b = seed_usuario(
        franquia.id,
        conta.id,
        email="user.b.lgpd@test.com",
        categoria="pro",
    )
    user_b.full_name = "Bruno Souza"
    user_b.set_password("outra-senha")
    user_b.oauth_provider = "google"
    user_b.oauth_sub = "sub-bruno-lgpd"
    user_b.subscribes_to_newsletter = True
    user_b.job_role = "Ops"
    user_b.usage_purpose = "BI"
    user_b.accepted_terms_at = datetime(2026, 4, 1, 12, 0, 0)
    db.session.commit()
    b_snapshot = _snapshot_user(user_b)
    nome_conta, nome_franquia = conta.nome, franquia.nome
    conta_id, franquia_id = conta.id, franquia.id

    processar_exercicio_privacidade_usuario(user_a, apply=True)

    conta_db = db.session.get(Conta, conta_id)
    franquia_db = db.session.get(Franquia, franquia_id)
    assert conta_db is not None
    assert franquia_db is not None
    assert conta_db.nome == nome_conta
    assert franquia_db.nome == nome_franquia
    persisted_b = db.session.get(User, b_snapshot["id"])
    for campo, valor in b_snapshot.items():
        assert getattr(persisted_b, campo) == valor
    assert User.query.count() == 2
    assert Conta.query.count() == 1
    assert Franquia.query.count() == 1


def test_apply_preserva_billing_e_eventos(ctx):
    conta, franquia, user = _montar_usuario_operacional(
        slug="priv-hist",
        email="hist.lgpd@test.com",
    )
    ids = _seed_historico(conta, franquia, user)
    uid = user.id

    processar_exercicio_privacidade_usuario(user, apply=True)

    funnel = db.session.get(FunnelEvent, ids["funnel"])
    processing = db.session.get(ProcessingEvent, ids["processing"])
    consumo = db.session.get(IaConsumoEvento, ids["consumo"])
    vinculo = db.session.get(ContaMonetizacaoVinculo, ids["vinculo"])
    fato = db.session.get(MonetizacaoFato, ids["fato"])
    apropriacao = db.session.get(CleitonBillingApropriacao, ids["apropriacao"])
    assert funnel is not None
    assert funnel.user_id == uid
    assert funnel.metadata_json == {"note": "keep"}
    assert processing is not None
    assert processing.usuario_id == uid
    assert processing.error_summary == "ok"
    assert consumo is not None
    assert consumo.usuario_id == uid
    assert consumo.error_summary == "ok"
    assert vinculo is not None
    assert vinculo.customer_id == "cus_privacy"
    assert vinculo.subscription_id == "sub_privacy"
    assert vinculo.snapshot_normalizado_json == "{}"
    assert vinculo.payload_bruto_sanitizado_json == "{}"
    assert fato is not None
    assert fato.usuario_id == uid
    assert fato.invoice_id == "in_privacy"
    assert fato.snapshot_normalizado_json == "{}"
    assert apropriacao is not None
    assert apropriacao.usuario_id == uid
    assert apropriacao.error_summary == "ok"


def test_apply_preserva_lead_e_encerra_jornada_sem_opt_out(ctx):
    _conta, _franquia, user = _montar_usuario_operacional(
        slug="priv-lead",
        email="lead.lgpd@test.com",
    )
    uid = user.id
    lead = _make_converted_lead_for_user(user, email="lead.lgpd@test.com")
    original_email = lead.email
    original_converted = lead.converted_user_id

    with patch.object(db.session, "commit", wraps=db.session.commit) as commit_mock:
        result = processar_exercicio_privacidade_usuario(user, apply=True)

    assert result.status == STATUS_OK
    assert commit_mock.call_count == 1
    persisted = db.session.get(Lead, lead.id)
    assert persisted is not None
    assert Lead.query.count() == 1
    assert persisted.email == original_email
    assert persisted.converted_user_id == original_converted == uid
    assert persisted.activation_ended_at is not None
    assert persisted.activation_ended_for_user_id == uid
    assert persisted.opt_out_at is None
    assert persisted.activation_opt_out_at is None
    assert CommunicationSuppression.query.count() == 0


def test_apply_nao_cria_nem_apaga_suppression_existente(app, ctx):
    _enable_suppression(app)
    _conta, _franquia, user = _montar_usuario_operacional(
        slug="priv-supp",
        email="supp.lgpd@test.com",
    )
    lead = _make_converted_lead_for_user(user, email="supp.lgpd@test.com")
    stamp = datetime(2026, 1, 10, 8, 0, 0)
    suppression.suppress_email(
        lead.email,
        suppression.PURPOSE_PRE_REGISTRATION,
        suppression.SOURCE_CAMPAIGN_UNSUBSCRIBE,
        suppressed_at=stamp,
    )
    existing = CommunicationSuppression.query.one()
    before = _snapshot_suppression(existing)

    processar_exercicio_privacidade_usuario(user, apply=True)

    assert CommunicationSuppression.query.count() == 1
    after = _snapshot_suppression(CommunicationSuppression.query.one())
    assert after == before
    persisted_lead = db.session.get(Lead, lead.id)
    assert persisted_lead.opt_out_at is None
    assert persisted_lead.activation_opt_out_at is None


def test_user_inexistente_falha_sem_write(ctx):
    conta, franquia, user = _montar_usuario_operacional(
        slug="priv-missing",
        email="missing.keep@test.com",
    )
    before = _snapshot_user(user)
    result = processar_exercicio_privacidade_por_user_id(999_001, apply=True)
    assert result.status == STATUS_NOT_FOUND
    assert result.user_deidentified is False
    assert result.error_type == "user_not_found"
    persisted = db.session.get(User, user.id)
    for campo, valor in before.items():
        assert getattr(persisted, campo) == valor
    assert db.session.get(Conta, conta.id) is not None
    assert db.session.get(Franquia, franquia.id) is not None
    assert CommunicationSuppression.query.count() == 0


def test_apply_e_idempotente(ctx):
    _conta, _franquia, user = _montar_usuario_operacional(
        slug="priv-idem",
        email="idem.lgpd@test.com",
    )
    uid = user.id
    lead = _make_converted_lead_for_user(user, email="idem.lgpd@test.com")
    first = processar_exercicio_privacidade_usuario(user, apply=True)
    lead_after_first = db.session.get(Lead, lead.id)
    ended_at = lead_after_first.activation_ended_at
    user_after_first = _snapshot_user(db.session.get(User, uid))

    second = processar_exercicio_privacidade_usuario(
        db.session.get(User, uid),
        apply=True,
    )
    persisted_user = db.session.get(User, uid)
    persisted_lead = db.session.get(Lead, lead.id)
    assert first.status == STATUS_OK
    assert second.status == STATUS_OK
    assert second.activation_journeys_ended == 0
    assert persisted_user.accepted_terms_at == ACEITE_CONHECIDO
    assert persisted_user.email == email_operacional_apos_encerramento(uid)
    assert persisted_user.full_name == NOME_OPERACIONAL_APOS_ENCERRAMENTO
    assert persisted_lead.activation_ended_at == ended_at
    assert persisted_lead.activation_ended_for_user_id == uid
    for campo, valor in user_after_first.items():
        assert getattr(persisted_user, campo) == valor


def test_erro_db_faz_rollback_total(ctx):
    _conta, _franquia, user = _montar_usuario_operacional(
        slug="priv-rollback",
        email="rollback.lgpd@test.com",
    )
    uid = user.id
    original_email = user.email
    lead = _make_converted_lead_for_user(user, email="rollback.lgpd@test.com")
    lead_id = lead.id
    rollbacks = {"n": 0}
    real_rollback = db.session.rollback

    def tracked_rollback():
        rollbacks["n"] += 1
        return real_rollback()

    with (
        patch.object(db.session, "commit", side_effect=RuntimeError("db boom")),
        patch.object(db.session, "rollback", side_effect=tracked_rollback),
    ):
        with pytest.raises(PrivacyRightsAborted) as exc_info:
            processar_exercicio_privacidade_usuario(user, apply=True)

    assert exc_info.value.error_type == "RuntimeError"
    assert rollbacks["n"] >= 1
    persisted_user = db.session.get(User, uid)
    persisted_lead = db.session.get(Lead, lead_id)
    assert persisted_user.email == original_email
    assert persisted_user.full_name == PII_NAME
    assert persisted_user.oauth_sub == PII_OAUTH_SUB
    assert persisted_lead.activation_ended_at is None
    assert persisted_lead.activation_ended_for_user_id is None
    assert persisted_lead.email == "rollback.lgpd@test.com"


def test_cli_default_e_dry_run(app, ctx):
    _conta, _franquia, user = _montar_usuario_operacional(
        slug="priv-cli-dry",
        email="cli.dry@test.com",
    )
    uid = user.id
    original_email = user.email
    register_privacy_rights_user_command(app)
    runner = app.test_cli_runner()
    result = runner.invoke(args=["privacy-rights-user", "--user-id", str(uid)])
    assert result.exit_code == 0
    assert result.output.splitlines()[0] == "MODE=DRY_RUN"
    assert "STATUS=OK" in result.output
    assert "--apply" not in result.output.splitlines()[0]
    persisted = db.session.get(User, uid)
    assert persisted.email == original_email
    _assert_no_pii(result.output, original_email)


def test_cli_apply_somente_em_fixture_isolada(app, ctx):
    _conta, _franquia, user = _montar_usuario_operacional(
        slug="priv-cli-apply",
        email="cli.apply@test.com",
    )
    uid = user.id
    register_privacy_rights_user_command(app)
    runner = app.test_cli_runner()
    result = runner.invoke(args=["privacy-rights-user", "--user-id", str(uid), "--apply"])
    assert result.exit_code == 0
    assert result.output.splitlines()[0] == "MODE=APPLY"
    assert "STATUS=OK" in result.output
    persisted = db.session.get(User, uid)
    assert persisted.email == email_operacional_apos_encerramento(uid)
    _assert_no_pii(result.output, "cli.apply@test.com")
    assert PII_NAME not in result.output
    assert PII_OAUTH_SUB not in result.output


def test_cli_user_inexistente_nao_grava_e_sem_pii(app, ctx):
    register_privacy_rights_user_command(app)
    runner = app.test_cli_runner()
    result = runner.invoke(args=["privacy-rights-user", "--user-id", "404404", "--apply"])
    assert result.exit_code == 1
    assert "MODE=APPLY" in result.output
    assert "STATUS=NOT_FOUND" in result.output
    assert "error_type=user_not_found" in result.output
    assert User.query.count() == 0
    _assert_no_pii(result.output)


def test_output_estruturado_sem_pii(ctx, caplog):
    _conta, _franquia, user = _montar_usuario_operacional(
        slug="priv-pii",
        email=PII_EMAIL,
    )
    processar_exercicio_privacidade_usuario(user, apply=False)
    result = processar_exercicio_privacidade_usuario(
        db.session.get(User, user.id),
        apply=True,
    )
    rendered = format_privacy_rights_cli(result)
    logs = " ".join(record.getMessage() for record in caplog.records)
    captured: list[str] = []
    exit_code = emit_privacy_rights_cli(user_id=user.id, apply=False, echo=captured.append)
    assert exit_code == 0
    _assert_no_pii(rendered, PII_EMAIL)
    _assert_no_pii(logs, PII_EMAIL)
    _assert_no_pii("\n".join(captured), PII_EMAIL)
    assert result.session_access_revocation == "enforced_on_next_request"
    assert result.global_session_storage_purge == "unsupported"
    assert result.current_session_cleanup_supported is False
    assert result.global_user_temp_cleanup_supported is False


def test_resultado_declara_limitacoes_de_temp_e_sessao(ctx):
    _conta, _franquia, user = _montar_usuario_operacional(
        slug="priv-limits",
        email="limits.lgpd@test.com",
    )
    result = processar_exercicio_privacidade_usuario(user, apply=False)
    assert isinstance(result, PrivacyRightsResult)
    assert result.current_session_cleanup_supported is False
    assert result.global_user_temp_cleanup_supported is False
    assert result.session_access_revocation == "enforced_on_next_request"
    assert result.global_session_storage_purge == "unsupported"
    assert "current_session_cleanup" in result.unsupported_cleanup_categories
    assert "global_user_temp_cleanup" in result.unsupported_cleanup_categories
    assert "global_session_storage_purge" in result.unsupported_cleanup_categories
    assert "global_session_invalidation" not in result.unsupported_cleanup_categories


def test_nao_e_encerramento_contratual_nem_hard_delete():
    privacy_src = Path("app/services/user_privacy_rights_service.py").read_text(encoding="utf-8")
    lifecycle_src = Path("app/services/user_lifecycle_service.py").read_text(encoding="utf-8")
    user_area_src = Path("app/user_area.py").read_text(encoding="utf-8")
    assert "db.session.delete" not in privacy_src
    assert "encerrar_vinculo_operacional_usuario(" not in privacy_src
    assert "processar_exercicio_privacidade_usuario" not in lifecycle_src
    assert "privacy-rights-user" not in user_area_src
    assert "encerrar_vinculo_operacional_usuario(user)" in user_area_src
    assert privacy_src.count("db.session.commit()") == 1
    assert "suppress_email" not in privacy_src
    assert "CommunicationSuppression(" not in privacy_src
    assert "opt_out_at =" not in privacy_src
    assert "activation_opt_out_at =" not in privacy_src
    assert "processar_exercicio_privacidade" not in user_area_src


def test_web_registra_cli_sem_expor_rota_publica():
    web_src = Path("app/web.py").read_text(encoding="utf-8")
    user_area_src = Path("app/user_area.py").read_text(encoding="utf-8")
    assert "register_privacy_rights_user_command" in web_src
    assert "/perfil/encerrar-contrato" in user_area_src
    assert "Não é exclusão LGPD" in user_area_src


def test_encerramento_contratual_permanece_distinto(ctx):
    _conta, _franquia, user = _montar_usuario_operacional(
        slug="priv-vs-life",
        email="vs.life@test.com",
    )
    resultado = encerrar_vinculo_operacional_usuario(user)
    assert resultado.sucesso is True
    persisted = db.session.get(User, user.id)
    assert persisted.email == email_operacional_apos_encerramento(user.id)
    assert persisted.accepted_terms_at == ACEITE_CONHECIDO
