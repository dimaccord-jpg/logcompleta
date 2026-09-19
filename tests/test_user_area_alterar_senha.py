"""SCRUM-127: Alterar senha em /perfil reutiliza o fluxo existente de recuperação."""
from __future__ import annotations

from pathlib import Path

from app.extensions import db
from app.models import ContaVinculoOrganizacional, Franquia
from app.services.conta_multiuser_capacidade_service import ocupar_assento
from app.services.conta_organizacional_rules import PAPEL_MEMBRO
from tests.conftest import seed_usuario
from tests.test_fase5_multiuser_painel_aumento import (
    _build_client,
    _login,
    _preparar_conta_multiuser,
)
from tests.test_user_lifecycle import _build_user_area_client

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PERFIL = ROOT / "app" / "templates" / "user_area.html"
WEB_PY = ROOT / "app" / "web.py"
USER_AREA_PY = ROOT / "app" / "user_area.py"
AUTH_SERVICES_PY = ROOT / "app" / "auth_services.py"


def _html_perfil(client, user) -> str:
    _login(client, user)
    resp = client.get("/perfil")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def _membro_da_conta(conta, email: str):
    livre = [
        fr
        for fr in Franquia.query.filter_by(conta_id=conta.id).order_by(Franquia.id.asc())
        if ContaVinculoOrganizacional.query.filter_by(
            franquia_id=fr.id, estado="ativo"
        ).first()
        is None
    ][0]
    membro = seed_usuario(livre.id, conta.id, email=email, categoria="multiuser")
    ocupar_assento(
        conta_id=conta.id,
        user_id=membro.id,
        franquia_id=livre.id,
        papel=PAPEL_MEMBRO,
        commit=True,
    )
    db.session.refresh(membro)
    return membro


def test_autenticado_ve_alterar_senha_apontando_para_recuperacao(app):
    with app.app_context():
        _conta, user = _preparar_conta_multiuser(
            "senha-ct", qtd=5, email="senha.ct@test.com"
        )
        html = _html_perfil(_build_client(app), user)
        assert "Acesso e credenciais" in html
        assert "Alterar senha" in html
        assert 'href="/request-password-reset"' in html
        assert "url_for('request_password_reset')" not in html


def test_visitante_continua_sem_acesso_ao_perfil(app):
    client = _build_user_area_client(app)
    resp = client.get("/perfil", follow_redirects=False)
    assert resp.status_code in {302, 401}
    assert "Alterar senha" not in resp.get_data(as_text=True)


def test_membro_e_contratante_recebem_a_mesma_acao(app):
    with app.app_context():
        conta, contratante = _preparar_conta_multiuser(
            "senha-ambos", qtd=5, email="senha.ambos.ct@test.com"
        )
        membro = _membro_da_conta(conta, "senha.ambos.mb@test.com")
        client = _build_client(app)
        html_ct = _html_perfil(client, contratante)
        html_mb = _html_perfil(client, membro)
        assert "Alterar senha" in html_ct
        assert "Alterar senha" in html_mb
        assert 'href="/request-password-reset"' in html_ct
        assert 'href="/request-password-reset"' in html_mb


def test_nenhuma_nova_rota_de_senha_foi_criada():
    web_src = WEB_PY.read_text(encoding="utf-8")
    user_area_src = USER_AREA_PY.read_text(encoding="utf-8")
    template_src = TEMPLATE_PERFIL.read_text(encoding="utf-8")
    assert web_src.count("@app.route('/request-password-reset'") == 1
    assert web_src.count("@app.route('/reset-password/<token>'") == 1
    assert "@app.route('/alterar-senha'" not in web_src
    assert "@user_bp.route" in user_area_src
    assert "alterar-senha" not in user_area_src
    assert "change-password" not in user_area_src
    assert "request-password-reset" not in user_area_src
    assert "url_for('request_password_reset')" in template_src
    assert 'href="/request-password-reset"' not in template_src


def test_fluxo_existente_de_recuperacao_nao_foi_alterado():
    auth_src = AUTH_SERVICES_PY.read_text(encoding="utf-8")
    web_src = WEB_PY.read_text(encoding="utf-8")
    user_area_src = USER_AREA_PY.read_text(encoding="utf-8")
    assert "def request_password_reset(" in auth_src
    assert "def reset_password_with_token(" in auth_src
    assert "auth_request_password_reset(" in web_src
    assert "auth_reset_password_with_token(" in web_src
    assert "request_password_reset" not in user_area_src
    assert "reset_password" not in user_area_src
    assert "set_password" not in user_area_src
