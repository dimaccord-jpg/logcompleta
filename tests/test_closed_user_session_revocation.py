"""Revogação de autenticação de sessões residuais de User operacionalmente encerrado."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from flask import g, jsonify
from flask_login import current_user, login_required

from app.extensions import db, login_manager
from app.infra import get_user_by_id, load_user_for_flask_login, user_is_admin
from app.models import User
from app.services.user_lifecycle_service import (
    email_operacional_apos_encerramento,
    encerrar_vinculo_operacional_usuario,
    is_user_operationally_closed,
)
from app.services.user_operational_state import is_user_operationally_closed as is_closed_from_state
from app.services.user_privacy_rights_service import processar_exercicio_privacidade_usuario
from tests.conftest import seed_conta_franquia_cliente, seed_usuario

ACEITE_CONHECIDO = datetime(2026, 3, 14, 15, 22, 51)


def _montar_usuario(*, slug: str, email: str, is_admin: bool = False) -> User:
    conta, franquia = seed_conta_franquia_cliente(slug=slug)
    user = seed_usuario(franquia.id, conta.id, email=email, categoria="pro")
    user.full_name = "Ana Silva"
    user.set_password("segredo123")
    user.oauth_provider = "google"
    user.oauth_sub = "sub-ana-session"
    user.accepted_terms_at = ACEITE_CONHECIDO
    user.is_admin = is_admin
    db.session.commit()
    return user


def _auth_client(app):
    app.config["SECRET_KEY"] = "test-secret-session-revocation"
    app.config["TESTING"] = True
    login_manager.init_app(app)
    login_manager.login_view = "login"

    @login_manager.user_loader
    def _load_user(user_id):  # noqa: ANN001
        return load_user_for_flask_login(user_id)

    @app.route("/login", endpoint="login")
    def login_stub():
        return "login", 401

    @app.route("/probe")
    @login_required
    def probe():
        return jsonify(
            {
                "authenticated": True,
                "user_id": int(current_user.id),
                "is_admin": bool(getattr(current_user, "is_admin", False)),
            }
        )

    @app.route("/admin-probe")
    @login_required
    def admin_probe():
        if not user_is_admin(current_user):
            return jsonify({"admin": False}), 403
        return jsonify({"admin": True, "user_id": int(current_user.id)})

    return app.test_client()


def _bind_session(client, user_id: int) -> None:
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


def _simulate_next_http_request() -> None:
    """
    Em produção cada request tem app context novo; Flask-Login cacheia o
    User em g._login_user. O fixture ctx mantém o mesmo app context entre
    client.get(), então o cache precisa ser limpo para simular o próximo
    request real.
    """
    g.pop("_login_user", None)


def _reload_user(user_id: int) -> User:
    db.session.expire_all()
    loaded = db.session.get(User, int(user_id))
    assert loaded is not None
    return loaded


def _assert_unauthenticated(resp) -> None:
    assert resp.status_code in (302, 303, 401)
    if resp.status_code in (302, 303):
        location = (resp.headers.get("Location") or "").lower()
        assert "login" in location


def test_helper_none_e_objeto_invalido():
    assert is_user_operationally_closed is is_closed_from_state
    assert is_user_operationally_closed(None) is False
    assert is_closed_from_state(None) is False
    assert is_user_operationally_closed(object()) is False  # type: ignore[arg-type]


def test_helper_marcador_exato_e_normalizacao(ctx):
    user = _montar_usuario(slug="sess-helper-exact", email="exact.session@test.com")
    assert is_user_operationally_closed(user) is False
    user.email = email_operacional_apos_encerramento(user.id)
    assert is_user_operationally_closed(user) is True
    user.email = email_operacional_apos_encerramento(user.id).upper()
    assert is_user_operationally_closed(user) is True
    user.email = f"  {email_operacional_apos_encerramento(user.id).upper()}  "
    assert is_user_operationally_closed(user) is True


def test_helper_nao_aceita_marcador_parecido(ctx):
    user = _montar_usuario(slug="sess-helper-similar", email="similar.session@test.com")
    uid = user.id
    user.email = f"encerrado_{uid}@outro.local"
    assert is_user_operationally_closed(user) is False
    user.email = "encerrado_xyz@anon.local"
    assert is_user_operationally_closed(user) is False
    user.email = f"encerrado_{uid}@anon.local.evil.com"
    assert is_user_operationally_closed(user) is False
    user.email = f"prefix-encerrado_{uid}@anon.local"
    assert is_user_operationally_closed(user) is False
    user.email = f"encerrado_{uid + 1}@anon.local"
    assert is_user_operationally_closed(user) is False
    user.full_name = "Conta encerrada"
    user.password_hash = None
    user.oauth_provider = None
    user.oauth_sub = None
    user.subscribes_to_newsletter = False
    assert is_user_operationally_closed(user) is False


def test_helper_nao_usa_regex_nem_prefixo():
    source = Path("app/services/user_operational_state.py").read_text(encoding="utf-8")
    assert "import re" not in source
    assert "startswith" not in source
    assert "regex ampla" not in source


def test_get_user_by_id_ainda_retorna_user_encerrado(ctx):
    user = _montar_usuario(slug="sess-get-by-id", email="getbyid.session@test.com")
    uid = user.id
    user.email = email_operacional_apos_encerramento(uid)
    db.session.commit()
    loaded = get_user_by_id(uid)
    assert loaded is not None
    assert loaded.id == uid
    assert is_user_operationally_closed(loaded) is True
    assert load_user_for_flask_login(uid) is None
    infra_src = Path("app/infra.py").read_text(encoding="utf-8")
    getter = infra_src.split("def get_user_by_id", 1)[1].split("def load_user_for_flask_login", 1)[0]
    assert "is_user_operationally_closed" not in getter


def test_load_user_for_flask_login_rejeita_user_encerrado(ctx):
    user = _montar_usuario(slug="sess-loader", email="loader.session@test.com")
    uid = user.id
    assert load_user_for_flask_login(uid) is not None
    user.email = email_operacional_apos_encerramento(uid)
    db.session.commit()
    assert get_user_by_id(uid) is not None
    assert load_user_for_flask_login(uid) is None


def test_caso_a_user_ativo_sessao_continua(app, ctx):
    user = _montar_usuario(slug="sess-a-active", email="active.session@test.com")
    uid = user.id
    client = _auth_client(app)
    _bind_session(client, uid)
    resp = client.get("/probe")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["authenticated"] is True
    assert payload["user_id"] == uid


def test_caso_b_user_encerrado_sessao_antiga_nao_autentica(app, ctx):
    user = _montar_usuario(slug="sess-b-closed", email="closed.session@test.com")
    uid = user.id
    client = _auth_client(app)
    _bind_session(client, uid)
    assert client.get("/probe").status_code == 200

    user = _reload_user(uid)
    user.email = email_operacional_apos_encerramento(uid)
    db.session.commit()
    assert load_user_for_flask_login(uid) is None
    _simulate_next_http_request()
    _assert_unauthenticated(client.get("/probe"))
    assert get_user_by_id(uid) is not None
    assert db.session.get(User, uid).email == email_operacional_apos_encerramento(uid)


def test_caso_c_lgpd_r1_sessao_preexistente_nao_autentica(app, ctx):
    user = _montar_usuario(slug="sess-c-lgpd", email="lgpd.session@test.com")
    uid = user.id
    client = _auth_client(app)
    _bind_session(client, uid)
    assert client.get("/probe").status_code == 200

    user = _reload_user(uid)
    result = processar_exercicio_privacidade_usuario(user, apply=True)
    assert result.status == "OK"
    assert result.session_access_revocation == "enforced_on_next_request"
    assert result.global_session_storage_purge == "unsupported"

    _simulate_next_http_request()
    _assert_unauthenticated(client.get("/probe"))
    persisted = db.session.get(User, uid)
    assert persisted is not None
    assert persisted.is_admin is False
    assert get_user_by_id(uid) is not None
    assert is_user_operationally_closed(persisted) is True


def test_caso_d_admin_encerrado_nao_mantem_privilegio(app, ctx):
    user = _montar_usuario(
        slug="sess-d-admin",
        email="admin.session@test.com",
        is_admin=True,
    )
    uid = user.id
    client = _auth_client(app)
    _bind_session(client, uid)
    admin_resp = client.get("/admin-probe")
    assert admin_resp.status_code == 200
    assert admin_resp.get_json()["admin"] is True

    user = _reload_user(uid)
    processar_exercicio_privacidade_usuario(user, apply=True)
    persisted = db.session.get(User, uid)
    assert persisted.is_admin is True
    assert user_is_admin(persisted) is True
    assert get_user_by_id(uid).is_admin is True

    _simulate_next_http_request()
    _assert_unauthenticated(client.get("/probe"))
    _assert_unauthenticated(client.get("/admin-probe"))


def test_caso_e_marcador_parecido_nao_bloqueia_sessao(app, ctx):
    user = _montar_usuario(slug="sess-e-similar", email="similar.live@test.com")
    uid = user.id
    user.email = f"encerrado_{uid}@outro.local"
    db.session.commit()
    client = _auth_client(app)
    _bind_session(client, uid)
    resp = client.get("/probe")
    assert resp.status_code == 200
    assert resp.get_json()["user_id"] == uid


def test_dry_run_lgpd_nao_revoga_sessao(app, ctx):
    user = _montar_usuario(slug="sess-dry", email="dry.session@test.com")
    uid = user.id
    original_email = user.email
    client = _auth_client(app)
    _bind_session(client, uid)
    result = processar_exercicio_privacidade_usuario(user, apply=False)
    assert result.status == "OK"
    assert result.user_deidentified is False
    assert client.get("/probe").status_code == 200
    assert db.session.get(User, uid).email == original_email


def test_pacote_2_encerramento_tambem_revoga_sessao_antiga(app, ctx):
    user = _montar_usuario(slug="sess-pkg2", email="pkg2.session@test.com")
    uid = user.id
    client = _auth_client(app)
    _bind_session(client, uid)
    assert client.get("/probe").status_code == 200

    user = _reload_user(uid)
    resultado = encerrar_vinculo_operacional_usuario(user)
    assert resultado.sucesso is True
    _simulate_next_http_request()
    _assert_unauthenticated(client.get("/probe"))
    assert get_user_by_id(uid) is not None
    assert db.session.get(User, uid).accepted_terms_at == ACEITE_CONHECIDO
