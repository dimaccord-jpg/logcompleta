"""Lifecycle operacional do User: encerramento contratual ≠ exclusão LGPD."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app.extensions import db, login_manager
from app.infra import load_user_for_flask_login
from app.models import (
    CommunicationSuppression,
    Conta,
    ContaMonetizacaoVinculo,
    Franquia,
    FunnelEvent,
    IaConsumoEvento,
    Lead,
    MonetizacaoFato,
    User,
)
from app.privacy_marketing import PRIVACY_MARKETING_COOKIE_NAME
from app.services.lead_acquisition_service import (
    CAMPANHA_ACESSO_DESKTOP,
    FONTE_LANDING,
)
from app.services.user_lifecycle_service import (
    NOME_OPERACIONAL_APOS_ENCERRAMENTO,
    ResultadoEncerramentoContratual,
    anonimizar_perfil_operacional_para_encerramento,
    email_operacional_apos_encerramento,
    encerrar_vinculo_operacional_usuario,
    is_user_operationally_closed,
)
from app.user_area import user_bp
from tests.conftest import seed_conta_franquia_cliente, seed_usuario

ACEITE_CONHECIDO = datetime(2026, 3, 14, 15, 22, 51)
LOGIN_CONHECIDO = datetime(2026, 8, 1, 10, 0, 0)
AUDITORIA_CONHECIDA = datetime(2026, 7, 1, 9, 0, 0)
CRIADO_CONHECIDO = datetime(2026, 1, 2, 8, 30, 0)


def _montar_usuario_operacional(
    *,
    slug: str,
    email: str,
    categoria: str = "pro",
) -> tuple[Conta, Franquia, User]:
    conta, franquia = seed_conta_franquia_cliente(slug=slug)
    user = seed_usuario(franquia.id, conta.id, email=email, categoria=categoria)
    user.full_name = "Ana Silva"
    user.set_password("segredo123")
    user.oauth_provider = "google"
    user.oauth_sub = "sub-ana-123"
    user.subscribes_to_newsletter = True
    user.job_role = "Analista"
    user.usage_purpose = "Auditoria"
    user.accepted_terms_at = ACEITE_CONHECIDO
    user.last_login_at = LOGIN_CONHECIDO
    user.first_audit_completed_at = AUDITORIA_CONHECIDA
    user.created_at = CRIADO_CONHECIDO
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


def _set_privacy_cookie(client, value: str) -> None:
    try:
        client.set_cookie(PRIVACY_MARKETING_COOKIE_NAME, value)
    except TypeError:
        client.set_cookie("localhost", PRIVACY_MARKETING_COOKIE_NAME, value)


def _cookie_value(client, name: str) -> str | None:
    getter = getattr(client, "get_cookie", None)
    if callable(getter):
        try:
            cookie = getter(name)
        except TypeError:
            cookie = getter(name, domain="localhost")
        if cookie is None:
            return None
        return getattr(cookie, "value", cookie)
    jar = getattr(client, "_cookies", None) or getattr(client, "cookie_jar", None)
    if jar is None:
        return None
    for cookie in jar:
        cookie_name = getattr(cookie, "name", None) or getattr(cookie, "key", None)
        if cookie_name == name:
            return cookie.value
    return None


def _build_user_area_client(app):
    app.config["SECRET_KEY"] = "test-secret-lifecycle"
    app.config["TESTING"] = True
    if "user" not in app.blueprints:
        app.register_blueprint(user_bp)

    @app.route("/login", endpoint="login")
    def login_stub():
        return "login", 401

    @app.route("/", endpoint="index")
    def index_stub():
        return "index"

    login_manager.init_app(app)
    login_manager.login_view = "login"

    @login_manager.user_loader
    def _load_user(user_id):  # noqa: ANN001
        return load_user_for_flask_login(user_id)

    return app.test_client()


def test_anonimizacao_nao_persiste_sozinha(ctx):
    _conta, _franquia, user = _montar_usuario_operacional(
        slug="life-anon-no-commit",
        email="anon.nocommit@test.com",
    )
    uid = user.id
    original_email = user.email

    anonimizar_perfil_operacional_para_encerramento(user)
    assert user.email == email_operacional_apos_encerramento(uid)
    db.session.rollback()

    persisted = db.session.get(User, uid)
    assert persisted is not None
    assert persisted.email == original_email
    assert persisted.accepted_terms_at == ACEITE_CONHECIDO


def test_encerramento_preserva_row_e_nao_faz_hard_delete(ctx):
    _conta, _franquia, user = _montar_usuario_operacional(
        slug="life-row",
        email="row@test.com",
    )
    uid = user.id
    resultado = encerrar_vinculo_operacional_usuario(user)

    assert resultado.sucesso is True
    assert db.session.get(User, uid) is not None
    assert User.query.count() == 1


def test_encerramento_remove_identidade_operacional(ctx):
    _conta, _franquia, user = _montar_usuario_operacional(
        slug="life-identidade",
        email="identidade@test.com",
    )
    uid = user.id
    encerrar_vinculo_operacional_usuario(user)
    persisted = db.session.get(User, uid)

    assert persisted.email == f"encerrado_{uid}@anon.local"
    assert persisted.full_name == NOME_OPERACIONAL_APOS_ENCERRAMENTO
    assert persisted.password_hash is None
    assert persisted.oauth_provider is None
    assert persisted.oauth_sub is None
    assert persisted.subscribes_to_newsletter is False
    assert persisted.job_role is None
    assert persisted.usage_purpose is None


def test_encerramento_preserva_accepted_terms_at(ctx):
    _conta, _franquia, user = _montar_usuario_operacional(
        slug="life-aceite",
        email="aceite@test.com",
    )
    uid = user.id
    encerrar_vinculo_operacional_usuario(user)
    persisted = db.session.get(User, uid)
    assert persisted.accepted_terms_at == ACEITE_CONHECIDO


def test_encerramento_preserva_ids_estruturais_e_timestamps(ctx):
    conta, franquia, user = _montar_usuario_operacional(
        slug="life-ids",
        email="ids@test.com",
        categoria="starter",
    )
    uid = user.id
    conta_id = conta.id
    franquia_id = franquia.id
    encerrar_vinculo_operacional_usuario(user)
    persisted = db.session.get(User, uid)

    assert persisted.id == uid
    assert persisted.conta_id == conta_id
    assert persisted.franquia_id == franquia_id
    assert persisted.categoria == "starter"
    assert persisted.created_at == CRIADO_CONHECIDO
    assert persisted.last_login_at == LOGIN_CONHECIDO
    assert persisted.first_audit_completed_at == AUDITORIA_CONHECIDA


def test_encerramento_preserva_conta_e_franquia(ctx):
    conta, franquia, user = _montar_usuario_operacional(
        slug="life-conta-fr",
        email="contafr@test.com",
    )
    conta_id, franquia_id = conta.id, franquia.id
    nome_conta, nome_franquia = conta.nome, franquia.nome
    encerrar_vinculo_operacional_usuario(user)

    conta_db = db.session.get(Conta, conta_id)
    franquia_db = db.session.get(Franquia, franquia_id)
    assert conta_db is not None
    assert franquia_db is not None
    assert conta_db.nome == nome_conta
    assert franquia_db.nome == nome_franquia
    assert Conta.query.count() == 1
    assert Franquia.query.count() == 1


def test_encerramento_nao_afeta_outro_user_da_mesma_conta(ctx):
    conta, franquia, user_a = _montar_usuario_operacional(
        slug="life-multi",
        email="user.a@test.com",
    )
    user_b = seed_usuario(
        franquia.id,
        conta.id,
        email="user.b@test.com",
        categoria="pro",
    )
    user_b.full_name = "Bruno Souza"
    user_b.set_password("outra-senha")
    user_b.oauth_provider = "google"
    user_b.oauth_sub = "sub-bruno"
    user_b.subscribes_to_newsletter = True
    user_b.job_role = "Ops"
    user_b.usage_purpose = "BI"
    user_b.accepted_terms_at = datetime(2026, 4, 1, 12, 0, 0)
    db.session.commit()

    b_snapshot = {
        "id": user_b.id,
        "email": user_b.email,
        "full_name": user_b.full_name,
        "password_hash": user_b.password_hash,
        "oauth_provider": user_b.oauth_provider,
        "oauth_sub": user_b.oauth_sub,
        "subscribes_to_newsletter": user_b.subscribes_to_newsletter,
        "job_role": user_b.job_role,
        "usage_purpose": user_b.usage_purpose,
        "accepted_terms_at": user_b.accepted_terms_at,
        "conta_id": user_b.conta_id,
        "franquia_id": user_b.franquia_id,
        "categoria": user_b.categoria,
    }

    encerrar_vinculo_operacional_usuario(user_a)
    persisted_b = db.session.get(User, b_snapshot["id"])
    for campo, valor in b_snapshot.items():
        assert getattr(persisted_b, campo) == valor

    assert db.session.get(Conta, conta.id) is not None
    assert db.session.get(Franquia, franquia.id) is not None
    assert User.query.count() == 2


def test_encerramento_nao_apaga_historico_vinculado(ctx):
    conta, franquia, user = _montar_usuario_operacional(
        slug="life-hist",
        email="hist@test.com",
    )
    funnel = FunnelEvent(
        user_id=user.id,
        conta_id=conta.id,
        franquia_id=franquia.id,
        event_name="file_uploaded",
        source="web",
        idempotency_key="life-funnel-1",
    )
    consumo = IaConsumoEvento(
        provider="gemini",
        operation="generate",
        model="test-model",
        agent="julia",
        flow_type="chat",
        api_key_label="k",
        status="ok",
        usuario_id=user.id,
        conta_id=conta.id,
        franquia_id=franquia.id,
    )
    vinculo = ContaMonetizacaoVinculo(
        conta_id=conta.id,
        provider="stripe",
        customer_id="cus_hist",
        subscription_id="sub_hist",
        ativo=True,
        snapshot_normalizado_json="{}",
    )
    fato = MonetizacaoFato(
        tipo_fato="stripe_invoice_paid",
        status_tecnico="aplicado",
        provider="stripe",
        conta_id=conta.id,
        franquia_id=franquia.id,
        usuario_id=user.id,
        customer_id="cus_hist",
        subscription_id="sub_hist",
        invoice_id="in_hist",
        snapshot_normalizado_json="{}",
    )
    db.session.add_all([funnel, consumo, vinculo, fato])
    db.session.commit()
    ids = (funnel.id, consumo.id, vinculo.id, fato.id)

    encerrar_vinculo_operacional_usuario(user)

    assert db.session.get(FunnelEvent, ids[0]) is not None
    assert db.session.get(IaConsumoEvento, ids[1]) is not None
    assert db.session.get(ContaMonetizacaoVinculo, ids[2]) is not None
    assert db.session.get(MonetizacaoFato, ids[3]) is not None
    vinculo_db = db.session.get(ContaMonetizacaoVinculo, ids[2])
    fato_db = db.session.get(MonetizacaoFato, ids[3])
    assert vinculo_db.customer_id == "cus_hist"
    assert vinculo_db.subscription_id == "sub_hist"
    assert fato_db.invoice_id == "in_hist"


def test_encerramento_usuario_invalido_retorna_falha_sem_efeito(ctx):
    resultado = encerrar_vinculo_operacional_usuario(None)
    assert isinstance(resultado, ResultadoEncerramentoContratual)
    assert resultado.sucesso is False
    assert resultado.mensagem
    assert User.query.count() == 0


def test_rota_encerrar_contrato_faz_logout_e_preserva_cookie_de_marketing(app, ctx):
    _conta, _franquia, user = _montar_usuario_operacional(
        slug="life-rota",
        email="rota@test.com",
    )
    uid = user.id
    client = _build_user_area_client(app)
    _set_privacy_cookie(client, "v1:accepted")

    with client.session_transaction() as sess:
        sess["_user_id"] = str(uid)
        sess["_fresh"] = True
        sess["post_login_next"] = "/perfil"

    resp = client.post("/perfil/encerrar-contrato", follow_redirects=False)
    assert resp.status_code in (302, 303)
    location = resp.headers.get("Location") or ""
    assert location.endswith("/")

    with client.session_transaction() as sess:
        assert "_user_id" not in sess
        assert "post_login_next" not in sess

    set_cookie_headers = [h.lower() for h in resp.headers.getlist("Set-Cookie")]
    assert not any(
        h.startswith(f"{PRIVACY_MARKETING_COOKIE_NAME}=") and ("max-age=0" in h)
        for h in set_cookie_headers
    )
    assert _cookie_value(client, PRIVACY_MARKETING_COOKIE_NAME) == "v1:accepted"

    persisted = db.session.get(User, uid)
    assert persisted is not None
    assert persisted.email == email_operacional_apos_encerramento(uid)
    assert persisted.accepted_terms_at == ACEITE_CONHECIDO


def test_rota_encerrar_contrato_exige_autenticacao(app, ctx):
    client = _build_user_area_client(app)
    resp = client.post("/perfil/encerrar-contrato", follow_redirects=False)
    assert resp.status_code in (302, 303, 401)


def test_modulo_lifecycle_nao_implementa_hard_delete_nem_exclusao_lgpd():
    source = Path("app/services/user_lifecycle_service.py").read_text(encoding="utf-8")
    assert "db.session.delete" not in source
    assert "delete_user" not in source
    assert "lgpd_delete" not in source
    assert "erase_user" not in source
    assert "delete_personal_data" not in source


def test_auth_services_nao_encerra_contrato_nem_zera_aceite():
    source = Path("app/auth_services.py").read_text(encoding="utf-8")
    assert "def encerrar_contrato(" not in source
    assert "user.accepted_terms_at = None" not in source


def test_is_user_operationally_closed_e_estrito(ctx):
    _conta, _franquia, user = _montar_usuario_operacional(
        slug="life-closed-strict",
        email="closed.strict@test.com",
    )
    assert is_user_operationally_closed(user) is False
    user.email = f"encerrado_{user.id}@outro.local"
    assert is_user_operationally_closed(user) is False
    user.email = "encerrado_xyz@anon.local"
    assert is_user_operationally_closed(user) is False
    user.full_name = NOME_OPERACIONAL_APOS_ENCERRAMENTO
    assert is_user_operationally_closed(user) is False
    user.email = email_operacional_apos_encerramento(user.id).upper()
    assert is_user_operationally_closed(user) is True
    assert is_user_operationally_closed(None) is False


def test_encerramento_marca_jornada_ativacao_do_lead(ctx):
    _conta, _franquia, user = _montar_usuario_operacional(
        slug="life-lead-ended",
        email="lead.ended@test.com",
    )
    uid = user.id
    lead = _make_converted_lead_for_user(user, email="lead.ended@test.com")
    original_lead_email = lead.email
    original_converted_at = lead.converted_at
    original_e1 = lead.activation_email_1_sent_at
    original_e2 = lead.activation_email_2_sent_at

    with patch.object(db.session, "commit", wraps=db.session.commit) as commit_mock:
        resultado = encerrar_vinculo_operacional_usuario(user)

    assert resultado.sucesso is True
    assert commit_mock.call_count == 1

    persisted_user = db.session.get(User, uid)
    persisted_lead = db.session.get(Lead, lead.id)
    assert persisted_user.email == email_operacional_apos_encerramento(uid)
    assert persisted_user.accepted_terms_at == ACEITE_CONHECIDO
    assert persisted_lead.email == original_lead_email
    assert persisted_lead.converted_user_id == uid
    assert persisted_lead.converted_at == original_converted_at
    assert persisted_lead.activation_email_1_sent_at == original_e1
    assert persisted_lead.activation_email_2_sent_at == original_e2
    assert persisted_lead.activation_ended_at is not None
    assert persisted_lead.activation_ended_for_user_id == uid
    assert persisted_lead.opt_out_at is None
    assert persisted_lead.activation_opt_out_at is None
    assert CommunicationSuppression.query.count() == 0


def test_encerramento_sem_lead_continua_funcionando(ctx):
    _conta, _franquia, user = _montar_usuario_operacional(
        slug="life-no-lead",
        email="nolead@test.com",
    )
    uid = user.id
    assert Lead.query.filter_by(converted_user_id=uid).count() == 0
    resultado = encerrar_vinculo_operacional_usuario(user)
    assert resultado.sucesso is True
    persisted = db.session.get(User, uid)
    assert persisted.email == email_operacional_apos_encerramento(uid)
    assert persisted.accepted_terms_at == ACEITE_CONHECIDO
    assert Lead.query.count() == 0
    assert CommunicationSuppression.query.count() == 0


def test_encerramento_nao_inventa_opt_out_nem_suppression(ctx):
    _conta, _franquia, user = _montar_usuario_operacional(
        slug="life-no-optout",
        email="no.optout@test.com",
    )
    lead = _make_converted_lead_for_user(user, email="no.optout@test.com")
    encerrar_vinculo_operacional_usuario(user)
    persisted = db.session.get(Lead, lead.id)
    assert persisted.opt_out_at is None
    assert persisted.activation_opt_out_at is None
    assert CommunicationSuppression.query.count() == 0


def test_encerramento_marca_todos_os_leads_do_user(ctx):
    _conta, _franquia, user = _montar_usuario_operacional(
        slug="life-multi-lead",
        email="multi.lead@test.com",
    )
    uid = user.id
    lead_a = _make_converted_lead_for_user(user, email="multi.a@test.com")
    lead_b = _make_converted_lead_for_user(user, email="multi.b@test.com")
    encerrar_vinculo_operacional_usuario(user)
    for lead_id in (lead_a.id, lead_b.id):
        persisted = db.session.get(Lead, lead_id)
        assert persisted.activation_ended_at is not None
        assert persisted.activation_ended_for_user_id == uid
        assert persisted.opt_out_at is None
        assert persisted.activation_opt_out_at is None


def test_encerramento_first_write_wins_da_jornada(ctx):
    _conta, _franquia, user = _montar_usuario_operacional(
        slug="life-first-write",
        email="first.write@test.com",
    )
    uid = user.id
    lead = _make_converted_lead_for_user(user, email="first.write@test.com")
    original = datetime(2026, 1, 1, 8, 0, 0)
    lead.activation_ended_at = original
    lead.activation_ended_for_user_id = uid
    db.session.commit()

    encerrar_vinculo_operacional_usuario(user)
    persisted = db.session.get(Lead, lead.id)
    assert persisted.activation_ended_at == original
    assert persisted.activation_ended_for_user_id == uid


def test_encerramento_jornada_fica_ligada_ao_user_id_antigo(ctx):
    _conta, _franquia, user_a = _montar_usuario_operacional(
        slug="life-old-uid",
        email="rejoin@test.com",
    )
    uid_a = user_a.id
    lead = _make_converted_lead_for_user(user_a, email="rejoin.lead@test.com")
    encerrar_vinculo_operacional_usuario(user_a)

    user_b = seed_usuario(
        user_a.franquia_id,
        user_a.conta_id,
        email="rejoin@test.com",
        categoria="pro",
    )
    persisted_lead = db.session.get(Lead, lead.id)
    assert persisted_lead.converted_user_id == uid_a
    assert persisted_lead.activation_ended_for_user_id == uid_a
    assert persisted_lead.converted_user_id != user_b.id


def test_modulo_lifecycle_um_unico_commit_e_sem_suppression():
    source = Path("app/services/user_lifecycle_service.py").read_text(encoding="utf-8")
    assert source.count("db.session.commit()") == 1
    assert "suppress_email" not in source
    assert "CommunicationSuppression(" not in source
    assert "opt_out_at =" not in source
    assert "activation_opt_out_at =" not in source
