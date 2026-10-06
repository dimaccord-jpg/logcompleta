"""Central de Plugins — governança administrativa (SCRUM-222, lote 3)."""
from __future__ import annotations

import inspect
import os
from pathlib import Path

from flask_login import UserMixin

from app.extensions import db, login_manager
from app.infra import get_user_by_id
from app.models import (
    ContaVinculoOrganizacional,
    Plugin,
    PluginAuditoriaAdministrativa,
    PluginCapability,
    PluginConexao,
)
from app.services.central_plugin_admin_service import gerar_csrf_token_admin_plugins
from app.services.central_plugin_service import (
    criar_conexao,
    definir_status_plugin,
    registrar_capability,
    registrar_plugin,
    vincular_referencia_cofre,
)
from tests.conftest import seed_conta_franquia_cliente, seed_usuario

_RAIZ = Path(__file__).resolve().parents[1]
_IDENTIFICADOR = "conta-ext-opaca-99"
_COFRE = "vault.ref.opaca.teste"


class _AuthUser(UserMixin):
    def __init__(self, user_id: str):
        self.id = user_id


def _build_admin_client(app):
    app.config["SECRET_KEY"] = "test-secret-central-plugins"
    app.config["TESTING"] = True
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("SECRET_KEY", "test-secret-central-plugins")
    from app.painel_admin.admin_routes import admin_bp

    if "admin" not in app.blueprints:
        app.register_blueprint(admin_bp)
    if "login" not in app.view_functions:
        app.add_url_rule("/login", "login", lambda: "login")
    if "index" not in app.view_functions:
        app.add_url_rule("/", "index", lambda: "home")
    login_manager.init_app(app)

    @login_manager.user_loader
    def _load_user(user_id):
        return get_user_by_id(user_id)

    return app.test_client()


def _login(client, user_id: int) -> None:
    from flask import g

    if hasattr(g, "_login_user"):
        delattr(g, "_login_user")
    user = _AuthUser(str(user_id))
    with client.session_transaction() as sess:
        sess["_user_id"] = user.get_id()
        sess["_fresh"] = True
        sess["_id"] = "test-session"


def _csrf(user_id: int) -> str:
    return gerar_csrf_token_admin_plugins(user_id)


def _pessoa(email: str, slug: str, *, is_admin: bool = False, categoria: str = "free"):
    conta, franquia = seed_conta_franquia_cliente(slug=slug)
    user = seed_usuario(franquia.id, conta.id, email=email, categoria=categoria)
    user.is_admin = is_admin
    db.session.add(user)
    db.session.commit()
    return user, conta, franquia


def _contratante_multiuser(email: str, slug: str):
    user, conta, franquia = _pessoa(email, slug, is_admin=False, categoria="multiuser")
    conta.multiuser_ativa = True
    db.session.add(conta)
    db.session.add(
        ContaVinculoOrganizacional(
            conta_id=conta.id,
            user_id=user.id,
            franquia_id=franquia.id,
            papel=ContaVinculoOrganizacional.PAPEL_CONTRATANTE,
            estado=ContaVinculoOrganizacional.ESTADO_ATIVO,
            titular=True,
            origem=ContaVinculoOrganizacional.ORIGEM_DOMINIO,
        )
    )
    db.session.commit()
    return user


def _plugin_catalogo(**kwargs):
    dados = dict(
        slug="catalogo-admin-teste",
        nome="Plugin homologado de teste",
        descricao="Fixture administrativo. Nao e integracao operacional.",
        adapter_key="adapter.catalogo_teste",
        suporta_titularidade_pessoal=True,
        suporta_titularidade_corporativa=False,
        status=Plugin.STATUS_RASCUNHO,
    )
    dados.update(kwargs)
    return registrar_plugin(**dados)


def _form_plugin(**overrides):
    dados = {
        "csrf_token": overrides.pop("csrf_token"),
        "nome": "Plugin homologado de teste",
        "slug": "catalogo-admin-teste",
        "descricao": "Fixture administrativo. Nao e integracao operacional.",
        "adapter_key": "adapter.catalogo_teste",
        "status": Plugin.STATUS_RASCUNHO,
        "suporta_titularidade_pessoal": "1",
    }
    dados.update(overrides)
    return dados


def test_adm_acessa_listagem(app):
    with app.app_context():
        admin, _conta, _franquia = _pessoa(
            "adm-plugins@test.com", "conta-plugins-adm", is_admin=True
        )
        plugin = _plugin_catalogo()
        client = _build_admin_client(app)
        _login(client, admin.id)
        resposta = client.get("/admin/central-plugins")
        corpo = resposta.get_data(as_text=True)
        assert resposta.status_code == 200
        assert "Central de Plugins" in corpo
        assert plugin.nome in corpo
        assert plugin.slug in corpo
        assert "Rascunho" in corpo
        assert plugin.adapter_key in corpo
        assert "Pessoal" in corpo
        assert "Capabilities" in corpo
        assert "Conexões" in corpo
        assert "Atualização" in corpo
        filtrada = client.get("/admin/central-plugins?status=disponivel")
        assert filtrada.status_code == 200
        assert plugin.nome not in filtrada.get_data(as_text=True)


def test_usuario_comum_nao_acessa(app):
    with app.app_context():
        admin, _conta, _franquia = _pessoa(
            "adm-negado@test.com", "conta-plugins-negado-adm", is_admin=True
        )
        plugin = _plugin_catalogo(slug="catalogo-negado", adapter_key="adapter.negado")
        comum, _conta_comum, _franquia_comum = _pessoa(
            "comum-plugins@test.com", "conta-plugins-comum", is_admin=False
        )
        client = _build_admin_client(app)
        anonimo = client.get("/admin/central-plugins")
        assert anonimo.status_code == 302
        direto = client.get(f"/admin/central-plugins/{plugin.id}")
        assert direto.status_code == 302

        _login(client, comum.id)
        assert client.get("/admin/central-plugins").status_code == 403
        assert client.get(f"/admin/central-plugins/{plugin.id}").status_code == 403
        assert client.get(f"/admin/central-plugins/{plugin.id}").get_data(as_text=True) == "Acesso Negado"
        post = client.post(
            "/admin/central-plugins/novo",
            data=_form_plugin(csrf_token=_csrf(comum.id), slug="nao-entra"),
        )
        assert post.status_code == 403
        assert Plugin.query.filter_by(slug="nao-entra").count() == 0
        assert admin.is_admin is True


def test_administrador_multiuser_nao_acessa(app):
    with app.app_context():
        contratante = _contratante_multiuser(
            "contratante-plugins@test.com", "conta-plugins-contratante"
        )
        plugin = _plugin_catalogo(slug="catalogo-multiuser", adapter_key="adapter.multiuser")
        assert contratante.is_admin is not True
        vinculo = ContaVinculoOrganizacional.query.filter_by(user_id=contratante.id).one()
        assert vinculo.papel == ContaVinculoOrganizacional.PAPEL_CONTRATANTE
        client = _build_admin_client(app)
        _login(client, contratante.id)
        assert client.get("/admin/central-plugins").status_code == 403
        assert client.get(f"/admin/central-plugins/{plugin.id}").status_code == 403
        post = client.post(
            f"/admin/central-plugins/{plugin.id}/status",
            data={"csrf_token": _csrf(contratante.id), "status": Plugin.STATUS_BLOQUEADO},
        )
        assert post.status_code == 403
        assert db.session.get(Plugin, plugin.id).status == Plugin.STATUS_RASCUNHO


def test_criacao_de_plugin_homologado(app):
    with app.app_context():
        admin, _conta, _franquia = _pessoa(
            "adm-cria@test.com", "conta-plugins-cria", is_admin=True
        )
        client = _build_admin_client(app)
        _login(client, admin.id)
        resposta = client.post(
            "/admin/central-plugins/novo",
            data=_form_plugin(csrf_token=_csrf(admin.id)),
            follow_redirects=True,
        )
        assert resposta.status_code == 200
        assert "Plugin homologado cadastrado." in resposta.get_data(as_text=True)
        plugin = Plugin.query.filter_by(slug="catalogo-admin-teste").one()
        assert plugin.nome == "Plugin homologado de teste"
        assert plugin.adapter_key == "adapter.catalogo_teste"
        assert plugin.suporta_titularidade_pessoal is True
        assert plugin.criado_por_user_id == admin.id
        assert plugin.status == Plugin.STATUS_RASCUNHO


def test_plugin_sem_titularidade_e_rejeitado(app):
    with app.app_context():
        admin, _conta, _franquia = _pessoa(
            "adm-titular@test.com", "conta-plugins-titular", is_admin=True
        )
        client = _build_admin_client(app)
        _login(client, admin.id)
        dados = _form_plugin(csrf_token=_csrf(admin.id), slug="sem-titularidade")
        dados.pop("suporta_titularidade_pessoal")
        resposta = client.post(
            "/admin/central-plugins/novo",
            data=dados,
            follow_redirects=True,
        )
        assert resposta.status_code == 200
        assert "ao menos uma titularidade" in resposta.get_data(as_text=True)
        assert Plugin.query.filter_by(slug="sem-titularidade").count() == 0


def test_edicao_de_status(app):
    with app.app_context():
        admin, _conta, _franquia = _pessoa(
            "adm-status@test.com", "conta-plugins-status", is_admin=True
        )
        plugin = _plugin_catalogo(slug="catalogo-status", adapter_key="adapter.status")
        client = _build_admin_client(app)
        _login(client, admin.id)
        resposta = client.post(
            f"/admin/central-plugins/{plugin.id}/status",
            data={"csrf_token": _csrf(admin.id), "status": Plugin.STATUS_DISPONIVEL},
            follow_redirects=True,
        )
        assert resposta.status_code == 200
        assert "Status do plugin atualizado." in resposta.get_data(as_text=True)
        db.session.expire_all()
        assert db.session.get(Plugin, plugin.id).status == Plugin.STATUS_DISPONIVEL


def test_cadastro_de_capability(app):
    with app.app_context():
        admin, _conta, _franquia = _pessoa(
            "adm-cap@test.com", "conta-plugins-cap", is_admin=True
        )
        plugin = _plugin_catalogo(slug="catalogo-cap", adapter_key="adapter.cap")
        client = _build_admin_client(app)
        _login(client, admin.id)
        resposta = client.post(
            f"/admin/central-plugins/{plugin.id}/capabilities",
            data={
                "csrf_token": _csrf(admin.id),
                "chave": "leitura.catalogo",
                "nome": "Leitura de catalogo",
                "descricao": "Capability ficticia de governanca.",
                "natureza": PluginCapability.NATUREZA_PASSIVA,
                "status": PluginCapability.STATUS_DISPONIVEL,
                "politica_maxima": PluginCapability.POLITICA_PERMITIDA,
            },
            follow_redirects=True,
        )
        corpo = resposta.get_data(as_text=True)
        assert resposta.status_code == 200
        assert "Capability cadastrada." in corpo
        assert "<th>Capability</th>" in corpo
        assert "Leitura de catalogo" in corpo
        capability = PluginCapability.query.filter_by(plugin_id=plugin.id).one()
        assert capability.chave == "leitura.catalogo"
        assert capability.politica_maxima == PluginCapability.POLITICA_PERMITIDA


def test_alteracao_de_politica_maxima(app):
    with app.app_context():
        admin, _conta, _franquia = _pessoa(
            "adm-politica@test.com", "conta-plugins-politica", is_admin=True
        )
        plugin = _plugin_catalogo(slug="catalogo-politica", adapter_key="adapter.politica")
        capability = registrar_capability(
            plugin_id=plugin.id,
            chave="acao.catalogo",
            nome="Acao de catalogo",
            politica_maxima=PluginCapability.POLITICA_PERMITIDA,
            natureza=PluginCapability.NATUREZA_ATIVA,
        )
        client = _build_admin_client(app)
        _login(client, admin.id)
        resposta = client.post(
            f"/admin/central-plugins/{plugin.id}/capabilities/{capability.id}",
            data={
                "csrf_token": _csrf(admin.id),
                "politica_maxima": PluginCapability.POLITICA_NEGADA,
                "status": PluginCapability.STATUS_DISPONIVEL,
            },
            follow_redirects=True,
        )
        assert resposta.status_code == 200
        db.session.expire_all()
        persistida = db.session.get(PluginCapability, capability.id)
        assert persistida.politica_maxima == PluginCapability.POLITICA_NEGADA
        assert persistida.status == PluginCapability.STATUS_DISPONIVEL


def test_capability_invalida_e_rejeitada(app):
    with app.app_context():
        admin, _conta, _franquia = _pessoa(
            "adm-cap-invalida@test.com", "conta-plugins-cap-invalida", is_admin=True
        )
        plugin = _plugin_catalogo(slug="catalogo-cap-invalida", adapter_key="adapter.cap_invalida")
        client = _build_admin_client(app)
        _login(client, admin.id)
        invalida = client.post(
            f"/admin/central-plugins/{plugin.id}/capabilities",
            data={
                "csrf_token": _csrf(admin.id),
                "chave": "!!!",
                "nome": "Capability invalida",
                "natureza": "mista",
                "status": PluginCapability.STATUS_DISPONIVEL,
                "politica_maxima": "irrestrita",
            },
            follow_redirects=True,
        )
        assert invalida.status_code == 200
        assert PluginCapability.query.filter_by(plugin_id=plugin.id).count() == 0
        secreta = client.post(
            f"/admin/central-plugins/{plugin.id}/capabilities",
            data={
                "csrf_token": _csrf(admin.id),
                "chave": "leitura.secreta",
                "nome": "token=abc",
                "natureza": PluginCapability.NATUREZA_PASSIVA,
                "status": PluginCapability.STATUS_DISPONIVEL,
                "politica_maxima": PluginCapability.POLITICA_PERMITIDA,
            },
            follow_redirects=True,
        )
        assert secreta.status_code == 200
        assert PluginCapability.query.filter_by(plugin_id=plugin.id).count() == 0


def test_bloqueio_administrativo_persiste_sem_desconectar(app):
    with app.app_context():
        admin, _conta, _franquia = _pessoa(
            "adm-bloqueio@test.com", "conta-plugins-bloqueio", is_admin=True
        )
        titular, _conta_titular, _franquia_titular = _pessoa(
            "titular-bloqueio@test.com", "conta-plugins-titular-bloqueio"
        )
        plugin = _plugin_catalogo(
            slug="catalogo-bloqueio",
            adapter_key="adapter.bloqueio",
            status=Plugin.STATUS_DISPONIVEL,
            suporta_titularidade_corporativa=False,
        )
        conexao = criar_conexao(
            plugin_id=plugin.id,
            user_id=titular.id,
            titularidade=PluginConexao.TITULARIDADE_PESSOAL,
            usuario_originador_id=titular.id,
        )
        estado_antes = conexao.estado
        client = _build_admin_client(app)
        _login(client, admin.id)
        resposta = client.post(
            f"/admin/central-plugins/{plugin.id}/status",
            data={"csrf_token": _csrf(admin.id), "status": Plugin.STATUS_BLOQUEADO},
            follow_redirects=True,
        )
        corpo = resposta.get_data(as_text=True)
        assert resposta.status_code == 200
        assert (
            "As conexões existentes permanecem registradas e a autorização efetiva impede a execução."
            in corpo
        )
        db.session.expire_all()
        assert db.session.get(Plugin, plugin.id).status == Plugin.STATUS_BLOQUEADO
        persistida = db.session.get(PluginConexao, conexao.id)
        assert persistida.estado == estado_antes
        assert persistida.desconectado_em is None
        assert persistida.revogado_em is None


def test_listagem_de_conexoes_nao_expoe_cofre(app):
    with app.app_context():
        admin, _conta, _franquia = _pessoa(
            "adm-conexao@test.com", "conta-plugins-conexao", is_admin=True
        )
        titular, _conta_titular, _franquia_titular = _pessoa(
            "titular-conexao@test.com", "conta-plugins-titular-conexao"
        )
        plugin = _plugin_catalogo(
            slug="catalogo-conexao",
            adapter_key="adapter.conexao",
            status=Plugin.STATUS_DISPONIVEL,
            suporta_titularidade_corporativa=False,
        )
        conexao = criar_conexao(
            plugin_id=plugin.id,
            user_id=titular.id,
            titularidade=PluginConexao.TITULARIDADE_PESSOAL,
            usuario_originador_id=titular.id,
            identificador_externo=_IDENTIFICADOR,
        )
        vincular_referencia_cofre(conexao.id, _COFRE)
        client = _build_admin_client(app)
        _login(client, admin.id)
        corpo = client.get(f"/admin/central-plugins/{plugin.id}").get_data(as_text=True)
        assert "Conexões" in corpo
        assert titular.email in corpo
        assert _IDENTIFICADOR not in corpo
        assert _COFRE not in corpo
        assert "cofre_referencia" not in corpo


def test_detalhe_nao_expoe_segredo(app):
    with app.app_context():
        admin, _conta, _franquia = _pessoa(
            "adm-segredo@test.com", "conta-plugins-segredo", is_admin=True
        )
        titular, _conta_titular, _franquia_titular = _pessoa(
            "titular-segredo@test.com", "conta-plugins-titular-segredo"
        )
        plugin = _plugin_catalogo(
            slug="catalogo-segredo",
            adapter_key="adapter.segredo",
            status=Plugin.STATUS_DISPONIVEL,
            suporta_titularidade_corporativa=False,
        )
        conexao = criar_conexao(
            plugin_id=plugin.id,
            user_id=titular.id,
            titularidade=PluginConexao.TITULARIDADE_PESSOAL,
            usuario_originador_id=titular.id,
            identificador_externo=_IDENTIFICADOR,
        )
        vincular_referencia_cofre(conexao.id, _COFRE)
        client = _build_admin_client(app)
        _login(client, admin.id)
        corpo = client.get(f"/admin/central-plugins/{plugin.id}").get_data(as_text=True)
        assert plugin.nome in corpo
        assert _COFRE not in corpo
        assert "cofre_referencia" not in corpo
        assert "access_token" not in corpo
        assert "refresh_token" not in corpo
        assert "api_key" not in corpo
        for template in (
            "central_plugins.html",
            "central_plugin_form.html",
            "central_plugin_detalhe.html",
        ):
            texto = (
                _RAIZ / "app" / "painel_admin" / "template_admin" / template
            ).read_text(encoding="utf-8")
            assert "cofre_referencia" not in texto
            assert _COFRE not in texto


def test_auditoria_administrativa_e_registrada(app):
    with app.app_context():
        admin, _conta, _franquia = _pessoa(
            "adm-auditoria@test.com", "conta-plugins-auditoria", is_admin=True
        )
        plugin = _plugin_catalogo(slug="catalogo-auditoria", adapter_key="adapter.auditoria")
        client = _build_admin_client(app)
        _login(client, admin.id)
        client.post(
            f"/admin/central-plugins/{plugin.id}/status",
            data={"csrf_token": _csrf(admin.id), "status": Plugin.STATUS_DESABILITADO},
        )
        db.session.expire_all()
        linha = PluginAuditoriaAdministrativa.query.filter_by(
            plugin_id=plugin.id,
            alteracao=PluginAuditoriaAdministrativa.ALTERACAO_PLUGIN_STATUS,
        ).one()
        assert linha.ator_user_id == admin.id
        assert linha.entidade == PluginAuditoriaAdministrativa.ENTIDADE_PLUGIN
        assert linha.campo == "status"
        assert linha.valor_anterior == Plugin.STATUS_RASCUNHO
        assert linha.valor_novo == Plugin.STATUS_DESABILITADO
        assert linha.created_at is not None
        assert linha.capability_id is None
        corpo = client.get(f"/admin/central-plugins/{plugin.id}").get_data(as_text=True)
        assert admin.email in corpo
        assert "Status" in corpo


def test_post_sem_csrf_segue_padrao_do_painel(app):
    with app.app_context():
        admin, _conta, _franquia = _pessoa(
            "adm-csrf@test.com", "conta-plugins-csrf", is_admin=True
        )
        plugin = _plugin_catalogo(slug="catalogo-csrf", adapter_key="adapter.csrf")
        client = _build_admin_client(app)
        _login(client, admin.id)
        resposta = client.post(
            f"/admin/central-plugins/{plugin.id}/status",
            data={"status": Plugin.STATUS_BLOQUEADO},
            follow_redirects=True,
        )
        assert resposta.status_code == 200
        assert "Não foi possível validar a solicitação." in resposta.get_data(as_text=True)
        db.session.expire_all()
        assert db.session.get(Plugin, plugin.id).status == Plugin.STATUS_RASCUNHO
        assert (
            PluginAuditoriaAdministrativa.query.filter_by(
                plugin_id=plugin.id,
                alteracao=PluginAuditoriaAdministrativa.ALTERACAO_PLUGIN_STATUS,
            ).count()
            == 0
        )


def test_alteracao_administrativa_nao_executa_capability(app, monkeypatch):
    chamadas = {"autorizacao": 0, "estado": 0, "desconectar": 0}

    def _autorizacao(*_args, **_kwargs):
        chamadas["autorizacao"] += 1
        raise AssertionError("governanca nao decide autorizacao")

    def _estado(*_args, **_kwargs):
        chamadas["estado"] += 1
        raise AssertionError("governanca nao altera conexao")

    def _desconectar(*_args, **_kwargs):
        chamadas["desconectar"] += 1
        raise AssertionError("governanca nao desconecta")

    import app.services.central_plugin_service as dominio

    monkeypatch.setattr(dominio, "avaliar_autorizacao_plugin", _autorizacao)
    monkeypatch.setattr(dominio, "alterar_estado_conexao", _estado)
    monkeypatch.setattr(dominio, "desconectar_conexao", _desconectar)

    with app.app_context():
        admin, _conta, _franquia = _pessoa(
            "adm-exec@test.com", "conta-plugins-exec", is_admin=True
        )
        titular, _conta_titular, _franquia_titular = _pessoa(
            "titular-exec@test.com", "conta-plugins-titular-exec"
        )
        plugin = _plugin_catalogo(
            slug="catalogo-exec",
            adapter_key="adapter.exec",
            status=Plugin.STATUS_DISPONIVEL,
            suporta_titularidade_corporativa=False,
        )
        conexao = criar_conexao(
            plugin_id=plugin.id,
            user_id=titular.id,
            titularidade=PluginConexao.TITULARIDADE_PESSOAL,
            usuario_originador_id=titular.id,
        )
        client = _build_admin_client(app)
        _login(client, admin.id)
        resposta = client.post(
            f"/admin/central-plugins/{plugin.id}/status",
            data={"csrf_token": _csrf(admin.id), "status": Plugin.STATUS_BLOQUEADO},
            follow_redirects=True,
        )
        assert resposta.status_code == 200
        db.session.expire_all()
        assert db.session.get(Plugin, plugin.id).status == Plugin.STATUS_BLOQUEADO
        assert db.session.get(PluginConexao, conexao.id).estado == PluginConexao.ESTADO_AGUARDANDO_CONFIGURACAO
        assert chamadas == {"autorizacao": 0, "estado": 0, "desconectar": 0}
        texto_admin = (
            _RAIZ / "app" / "services" / "central_plugin_admin_service.py"
        ).read_text(encoding="utf-8")
        for termo in (
            "avaliar_autorizacao_plugin",
            "alterar_estado_conexao",
            "desconectar_conexao",
            "requests",
            "httpx",
            "urllib",
        ):
            assert termo not in texto_admin
        assert "PluginConexao" not in inspect.getsource(definir_status_plugin)
        assert "avaliar_autorizacao" not in inspect.getsource(definir_status_plugin)


def test_json_e_campo_secreto_nao_alteram_o_catalogo(app):
    with app.app_context():
        admin, _conta, _franquia = _pessoa(
            "adm-json@test.com", "conta-plugins-json", is_admin=True
        )
        plugin = _plugin_catalogo(slug="catalogo-json", adapter_key="adapter.json")
        client = _build_admin_client(app)
        _login(client, admin.id)
        json_resp = client.post(
            f"/admin/central-plugins/{plugin.id}/status",
            json={
                "csrf_token": _csrf(admin.id),
                "status": Plugin.STATUS_BLOQUEADO,
                "payload": {"token": "segredo"},
            },
            follow_redirects=True,
        )
        assert json_resp.status_code == 200
        assert "não aceita payload JSON" in json_resp.get_data(as_text=True)
        secreto = client.post(
            f"/admin/central-plugins/{plugin.id}/status",
            data={
                "csrf_token": _csrf(admin.id),
                "status": Plugin.STATUS_BLOQUEADO,
                "access_token": "segredo-opaco",
            },
            follow_redirects=True,
        )
        assert secreto.status_code == 200
        assert "não aceita segredo" in secreto.get_data(as_text=True)
        db.session.expire_all()
        assert db.session.get(Plugin, plugin.id).status == Plugin.STATUS_RASCUNHO
