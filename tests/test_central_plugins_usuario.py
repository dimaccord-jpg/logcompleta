"""Central de Plugins — experiência autenticada do usuário (SCRUM-222, lote 4)."""
from __future__ import annotations

import urllib.request
from pathlib import Path

import pytest
from flask import g

from app.extensions import db, login_manager
from app.infra import get_user_by_id
import app.services.central_plugin_whatsapp_service  # noqa: F401
from app.models import (
    ContaVinculoOrganizacional,
    ExecucaoOperacionalCanal,
    Franquia,
    IdentidadeCanalExterna,
    Plugin,
    PluginCapability,
    PluginConexao,
    PluginEventoCentral,
    PluginRestricaoUsuario,
    utcnow_naive,
)
from app.services.canal_aquisicao_service import obter_ou_criar_identidade_externa
from app.services.central_plugin_whatsapp_service import MENSAGEM_CANAL_INDISPONIVEL
from app.services.central_plugin_service import (
    alterar_estado_conexao,
    criar_conexao,
    definir_status_plugin,
    obter_conexao_usuario,
    registrar_capability,
    registrar_plugin,
    vincular_referencia_cofre,
)
from app.services.central_plugin_usuario_service import (
    FluxoConexaoPlugin,
    gerar_csrf_token_plugins_usuario,
    limpar_fluxos_conexao,
    registrar_fluxo_conexao,
)
from app.services.conta_multiuser_autorizacao_service import user_eh_contratante_ativo
from tests.conftest import seed_conta_franquia_cliente, seed_usuario

_RAIZ = Path(__file__).resolve().parents[1]
_IDENTIFICADOR = "conta-externa-abcdef"
_COFRE = "vault.ref.opaca.usuario"
_CODIGO = "credencial_expirada"
_RESUMO = "Precisa validar a conexao novamente."


@pytest.fixture(autouse=True)
def _fluxos_isolados():
    limpar_fluxos_conexao()
    yield
    limpar_fluxos_conexao()


def _build_client(app):
    app.config["SECRET_KEY"] = "test-secret-plugins-usuario"
    app.config["TESTING"] = True
    app.template_folder = str(_RAIZ / "app" / "templates")
    app.static_folder = str(_RAIZ / "app" / "static")
    from app.user_area import user_bp

    if "user" not in app.blueprints:
        app.register_blueprint(user_bp)

    def _vazio():
        return "ok"

    for regra, endpoint in (
        ("/", "index"),
        ("/login", "login"),
        ("/logout", "logout"),
        ("/chat_julia", "chat_julia"),
        ("/fretes", "fretes"),
        ("/feed", "feed"),
        ("/politica-de-privacidade", "privacy_policy"),
        ("/request-password-reset", "request_password_reset"),
        ("/growth-cta", "growth.growth_cta_clicked"),
        ("/growth-page", "growth.growth_page_view"),
    ):
        if endpoint not in app.view_functions:
            app.add_url_rule(regra, endpoint=endpoint, view_func=_vazio)

    @app.context_processor
    def _contexto_shell():
        from flask import request, url_for
        from flask_login import current_user

        from app.shell_navigation import build_shell_navigation

        try:
            autenticado = bool(getattr(current_user, "is_authenticated", False))
        except Exception:
            autenticado = False
        return {
            "has_endpoint": lambda nome: nome in app.view_functions,
            "privacy_marketing_allowed": False,
            "privacy_marketing_state": "rejected",
            "user_is_admin": lambda _user: False,
            "falha_mensal_vigente": False,
            "regularizacao_url": "/perfil/regularizar-pagamento",
            "notificacoes_nao_lidas": 0,
            "shell_nav": build_shell_navigation(
                authenticated=autenticado,
                request_path=request.path or "/",
                url_for=url_for,
                has_endpoint=lambda nome: nome in app.view_functions,
            ),
        }

    login_manager.init_app(app)
    login_manager.login_view = "login"

    @login_manager.user_loader
    def _load_user(user_id):
        return get_user_by_id(user_id)

    return app.test_client()


def _login(client, user_id: int) -> None:
    if hasattr(g, "_login_user"):
        delattr(g, "_login_user")
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
        sess["_id"] = "test-session-plugins-usuario"


def _csrf(user_id: int) -> str:
    return gerar_csrf_token_plugins_usuario(user_id)


def _pessoa(email: str, slug: str):
    conta, franquia = seed_conta_franquia_cliente(slug=slug)
    user = seed_usuario(franquia.id, conta.id, email=email)
    return user, conta, franquia


def _segunda_franquia(conta, slug: str = "secundaria") -> Franquia:
    franquia = Franquia(
        conta_id=conta.id,
        nome="Secundaria",
        slug=slug,
        status=Franquia.STATUS_ACTIVE,
    )
    db.session.add(franquia)
    db.session.commit()
    return franquia


def _vinculo(conta, user, franquia, papel: str, *, titular: bool = False) -> None:
    db.session.add(
        ContaVinculoOrganizacional(
            conta_id=conta.id,
            user_id=user.id,
            franquia_id=franquia.id,
            papel=papel,
            estado=ContaVinculoOrganizacional.ESTADO_ATIVO,
            titular=titular,
            origem=ContaVinculoOrganizacional.ORIGEM_DOMINIO,
        )
    )
    db.session.commit()


def _plugin(**kwargs):
    dados = dict(
        slug="catalogo-usuario",
        nome="Integracao de acompanhamento",
        descricao="Permite acompanhar a sua conexao.",
        adapter_key="adapter.usuario_teste",
        suporta_titularidade_pessoal=True,
        suporta_titularidade_corporativa=False,
        status=Plugin.STATUS_DISPONIVEL,
    )
    dados.update(kwargs)
    return registrar_plugin(**dados)


def _capability(plugin, **kwargs):
    dados = dict(
        plugin_id=plugin.id,
        chave="leitura.dados",
        nome="Leitura de dados",
        descricao="Consulta informacoes ja liberadas para voce.",
        politica_maxima=PluginCapability.POLITICA_PERMITIDA,
        natureza=PluginCapability.NATUREZA_PASSIVA,
        status=PluginCapability.STATUS_DISPONIVEL,
    )
    dados.update(kwargs)
    return registrar_capability(**dados)


def _conexao(plugin, user, *, estado=None, identificador=None, codigo=None, resumo=None, titularidade=None, conta_id=None):
    conexao = criar_conexao(
        plugin_id=plugin.id,
        user_id=user.id,
        titularidade=titularidade or PluginConexao.TITULARIDADE_PESSOAL,
        usuario_originador_id=user.id,
        conta_id=conta_id,
        identificador_externo=identificador,
    )
    if estado is not None and estado != PluginConexao.ESTADO_AGUARDANDO_CONFIGURACAO:
        conexao = alterar_estado_conexao(
            conexao.id,
            estado,
            usuario_originador_id=user.id,
            diagnostico_codigo=codigo,
            diagnostico_resumo=resumo,
        )
    return conexao


def _html(client, user_id: int, caminho: str = "/plugins") -> str:
    _login(client, user_id)
    resposta = client.get(caminho)
    assert resposta.status_code == 200, resposta.get_data(as_text=True)
    return resposta.get_data(as_text=True)


def _card(html: str, slug: str) -> str:
    marcador = f'data-plugin="{slug}"'
    inicio = html.find(marcador)
    assert inicio != -1, slug
    artigo = html.rfind("<article", 0, inicio)
    fim = html.find("</article>", inicio)
    return html[artigo:fim]


def test_usuario_anonimo_nao_acessa(app):
    client = _build_client(app)
    resposta = client.get("/plugins", follow_redirects=False)
    assert resposta.status_code == 302
    assert "login" in (resposta.location or "")
    assert "Integracao" not in resposta.get_data(as_text=True)


def test_usuario_autenticado_acessa(app):
    with app.app_context():
        user, _conta, _franquia = _pessoa("auth-plugins@test.com", "conta-plugins-auth")
        antes = PluginEventoCentral.query.count()
        client = _build_client(app)
        html = _html(client, user.id)
        assert "Plugins" in html
        assert "Nenhuma integração disponível no momento." in html
        assert 'href="/plugins"' in client.get("/perfil").get_data(as_text=True)
        assert PluginEventoCentral.query.count() == antes


def test_catalogo_nao_mostra_rascunho_e_mostra_disponivel(app):
    with app.app_context():
        user, _conta, _franquia = _pessoa("catalogo-plugins@test.com", "conta-plugins-catalogo")
        rascunho = _plugin(
            slug="catalogo-rascunho",
            nome="Rascunho invisivel ao usuario",
            adapter_key="adapter.rascunho_usuario",
            status=Plugin.STATUS_RASCUNHO,
        )
        publicado = _plugin(
            slug="catalogo-publicado",
            nome="Integracao publicada",
            adapter_key="adapter.publicado_usuario",
        )
        _capability(publicado, chave="leitura.publicada", nome="Leitura publicada")
        client = _build_client(app)
        html = _html(client, user.id)
        assert rascunho.nome not in html
        assert rascunho.adapter_key not in html
        card = _card(html, publicado.slug)
        assert publicado.nome in card
        assert "Permite acompanhar a sua conexao." in card
        assert "Pessoal" in card
        assert 'data-estado="disponivel"' in card
        assert "Disponível" in card
        assert "Leitura publicada" in card
        assert "Permitida pela LogCompleta" in card
        assert 'data-acao="conectar"' not in card
        assert "A configuração desta integração ainda não está disponível." in card
        token = _csrf(user.id)
        negado = client.post(
            f"/plugins/{rascunho.slug}/conectar",
            data={"csrf_token": token},
        )
        assert negado.status_code == 404
        assert PluginConexao.query.filter_by(user_id=user.id).count() == 0


def test_estado_vazio(app):
    with app.app_context():
        user, _conta, _franquia = _pessoa("vazio-plugins@test.com", "conta-plugins-vazio")
        _plugin(
            slug="so-rascunho",
            nome="Nao publicar isto",
            adapter_key="adapter.so_rascunho",
            status=Plugin.STATUS_RASCUNHO,
        )
        client = _build_client(app)
        html = _html(client, user.id)
        assert "Nenhuma integração disponível no momento." in html
        assert "Nao publicar isto" not in html
        assert "<article" not in html


def test_usuario_ve_somente_a_propria_conexao(app):
    with app.app_context():
        ana, _conta_a, _franquia_a = _pessoa("ana-plugins@test.com", "conta-plugins-ana")
        bruno, _conta_b, _franquia_b = _pessoa("bruno-plugins@test.com", "conta-plugins-bruno")
        plugin = _plugin(slug="catalogo-isolamento", adapter_key="adapter.isolamento")
        _capability(plugin)
        _conexao(
            plugin,
            ana,
            estado=PluginConexao.ESTADO_CONECTADO,
            resumo="Resumo exclusivo do usuario A.",
        )
        _conexao(
            plugin,
            bruno,
            estado=PluginConexao.ESTADO_REQUER_ATENCAO,
            resumo="Resumo exclusivo do usuario B.",
            codigo="atencao_alheia",
        )
        client = _build_client(app)
        html = _html(client, ana.id)
        card = _card(html, plugin.slug)
        assert 'data-estado="conectado"' in card
        assert "Resumo exclusivo do usuario A." in card
        assert "Resumo exclusivo do usuario B." not in html
        assert "atencao_alheia" not in html
        assert bruno.email not in html


def test_conexao_de_outro_usuario_nao_e_administravel(app):
    with app.app_context():
        ana, _conta_a, _franquia_a = _pessoa("ana-admin-plugins@test.com", "conta-plugins-ana-adm")
        bruno, _conta_b, _franquia_b = _pessoa("bruno-admin-plugins@test.com", "conta-plugins-bruno-adm")
        plugin = _plugin(slug="catalogo-alheio", adapter_key="adapter.alheio")
        capability = _capability(plugin)
        conexao_b = _conexao(plugin, bruno, estado=PluginConexao.ESTADO_CONECTADO)
        client = _build_client(app)
        _login(client, ana.id)
        resposta = client.post(
            f"/plugins/{plugin.slug}/capabilities/{capability.chave}/restringir",
            data={"csrf_token": _csrf(ana.id), "conexao_id": str(conexao_b.id)},
        )
        assert resposta.status_code == 403
        assert PluginRestricaoUsuario.query.count() == 0
        db.session.expire_all()
        assert db.session.get(PluginConexao, conexao_b.id).estado == PluginConexao.ESTADO_CONECTADO


def test_mesma_conta_nao_concede_acesso_a_conexao_alheia(app):
    with app.app_context():
        conta, franquia_a = seed_conta_franquia_cliente(slug="conta-plugins-mesma")
        franquia_b = _segunda_franquia(conta)
        ana = seed_usuario(franquia_a.id, conta.id, email="ana-mesma@test.com")
        bruno = seed_usuario(franquia_b.id, conta.id, email="bruno-mesma@test.com")
        assert ana.conta_id == bruno.conta_id
        plugin = _plugin(
            slug="catalogo-mesma-conta",
            adapter_key="adapter.mesma_conta",
            suporta_titularidade_corporativa=True,
        )
        conexao_b = _conexao(
            plugin,
            bruno,
            estado=PluginConexao.ESTADO_CONECTADO,
            identificador="membro-secreto-xyz987",
            titularidade=PluginConexao.TITULARIDADE_CORPORATIVA,
            conta_id=conta.id,
        )
        vincular_referencia_cofre(conexao_b.id, "vault.membro.segredo")
        client = _build_client(app)
        html = _html(client, ana.id)
        assert "membro-secreto-xyz987" not in html
        assert "vault.membro.segredo" not in html
        assert 'data-estado="conectado"' not in html
        assert 'data-estado="disponivel"' in html
        db.session.expire_all()
        assert db.session.get(PluginConexao, conexao_b.id).estado == PluginConexao.ESTADO_CONECTADO


def test_contratante_multiuser_nao_administra_plugin_de_membro(app):
    with app.app_context():
        conta, franquia_a = seed_conta_franquia_cliente(slug="conta-plugins-multi")
        franquia_b = _segunda_franquia(conta, slug="membro")
        contratante = seed_usuario(
            franquia_a.id, conta.id, email="contratante-plugins@test.com", categoria="multiuser"
        )
        membro = seed_usuario(
            franquia_b.id, conta.id, email="membro-plugins@test.com", categoria="multiuser"
        )
        conta.multiuser_ativa = True
        db.session.add(conta)
        db.session.commit()
        _vinculo(conta, contratante, franquia_a, ContaVinculoOrganizacional.PAPEL_CONTRATANTE, titular=True)
        _vinculo(conta, membro, franquia_b, ContaVinculoOrganizacional.PAPEL_MEMBRO)
        assert user_eh_contratante_ativo(contratante) is True
        assert contratante.conta_id == membro.conta_id
        plugin = _plugin(
            slug="catalogo-multiuser",
            adapter_key="adapter.multiuser_usuario",
            suporta_titularidade_corporativa=True,
        )
        capability = _capability(plugin)
        conexao_membro = _conexao(
            plugin,
            membro,
            estado=PluginConexao.ESTADO_CONECTADO,
            identificador="conexao-privada-membro99",
            titularidade=PluginConexao.TITULARIDADE_CORPORATIVA,
            conta_id=conta.id,
        )
        vincular_referencia_cofre(conexao_membro.id, "vault.privado.membro")
        client = _build_client(app)
        html = _html(client, contratante.id)
        assert "conexao-privada-membro99" not in html
        assert "vault.privado.membro" not in html
        assert membro.email not in html
        assert 'data-estado="disponivel"' in _card(html, plugin.slug)
        desconectar = client.post(
            f"/plugins/{plugin.slug}/desconectar",
            data={"csrf_token": _csrf(contratante.id), "conexao_id": str(conexao_membro.id)},
        )
        restringir = client.post(
            f"/plugins/{plugin.slug}/capabilities/{capability.chave}/restringir",
            data={"csrf_token": _csrf(contratante.id), "conexao_id": str(conexao_membro.id)},
        )
        assert desconectar.status_code == 403
        assert restringir.status_code == 403
        db.session.expire_all()
        assert db.session.get(PluginConexao, conexao_membro.id).estado == PluginConexao.ESTADO_CONECTADO
        assert PluginRestricaoUsuario.query.filter_by(conexao_id=conexao_membro.id).count() == 0
        assert PluginConexao.query.filter_by(user_id=contratante.id).count() == 0


def test_conectado_requer_atencao_bloqueado_e_desabilitado(app):
    with app.app_context():
        user, _conta, _franquia = _pessoa("estados-plugins@test.com", "conta-plugins-estados")
        conectado = _plugin(slug="estado-conectado", nome="Estado conectado", adapter_key="adapter.estado_conectado")
        atencao = _plugin(slug="estado-atencao", nome="Estado atencao", adapter_key="adapter.estado_atencao")
        bloqueado_conexao = _plugin(
            slug="estado-bloqueado-conexao",
            nome="Estado bloqueado da conexao",
            adapter_key="adapter.estado_bloqueado_conexao",
        )
        bloqueado_plugin = _plugin(
            slug="estado-bloqueado-plugin",
            nome="Estado bloqueado do catalogo",
            adapter_key="adapter.estado_bloqueado_plugin",
        )
        desabilitado = _plugin(
            slug="estado-desabilitado",
            nome="Estado desabilitado",
            adapter_key="adapter.estado_desabilitado",
        )
        for plugin in (conectado, atencao, bloqueado_conexao, bloqueado_plugin, desabilitado):
            _capability(plugin, chave=f"leitura.{plugin.slug.replace('-', '.')}", nome=f"Leitura {plugin.nome}")
        _conexao(conectado, user, estado=PluginConexao.ESTADO_CONECTADO)
        _conexao(
            atencao,
            user,
            estado=PluginConexao.ESTADO_REQUER_ATENCAO,
            codigo=_CODIGO,
            resumo=_RESUMO,
        )
        _conexao(bloqueado_conexao, user, estado=PluginConexao.ESTADO_BLOQUEADO)
        conexao_catalogo = _conexao(bloqueado_plugin, user, estado=PluginConexao.ESTADO_CONECTADO)
        conexao_off = _conexao(desabilitado, user, estado=PluginConexao.ESTADO_CONECTADO)
        definir_status_plugin(bloqueado_plugin.id, Plugin.STATUS_BLOQUEADO)
        definir_status_plugin(desabilitado.id, Plugin.STATUS_DESABILITADO)
        client = _build_client(app)
        html = _html(client, user.id)

        card_conectado = _card(html, conectado.slug)
        assert 'data-estado="conectado"' in card_conectado
        assert "Conectado" in card_conectado
        assert "Sua conexão está ativa." in card_conectado
        assert 'data-acao="desconectar"' in card_conectado
        assert 'data-acao="restringir"' in card_conectado

        card_atencao = _card(html, atencao.slug)
        assert 'data-estado="requer_atencao"' in card_atencao
        assert "Requer atenção" in card_atencao
        assert _RESUMO in card_atencao
        assert _CODIGO not in html
        assert "precisa de uma nova validação" in card_atencao
        assert 'data-acao="revalidar"' not in card_atencao

        card_bloqueado = _card(html, bloqueado_conexao.slug)
        assert 'data-estado="bloqueado"' in card_bloqueado
        assert "Bloqueado" in card_bloqueado
        assert "Você não pode remover este bloqueio." in card_bloqueado
        assert 'data-acao="desconectar"' not in card_bloqueado
        assert 'data-acao="restringir"' not in card_bloqueado

        card_catalogo = _card(html, bloqueado_plugin.slug)
        assert 'data-estado="bloqueado"' in card_catalogo
        assert "Você não pode remover este bloqueio." in card_catalogo

        card_off = _card(html, desabilitado.slug)
        assert 'data-estado="desabilitado"' in card_off
        assert "Desabilitado" in card_off
        assert "temporariamente indisponível" in card_off
        assert 'data-acao="conectar"' not in card_off

        db.session.expire_all()
        assert db.session.get(PluginConexao, conexao_catalogo.id).estado == PluginConexao.ESTADO_CONECTADO
        assert db.session.get(PluginConexao, conexao_off.id).estado == PluginConexao.ESTADO_CONECTADO


def test_usuario_restringe_e_remove_a_propria_restricao(app):
    with app.app_context():
        user, _conta, _franquia = _pessoa("restricao-plugins@test.com", "conta-plugins-restricao")
        plugin = _plugin(slug="catalogo-restricao", adapter_key="adapter.restricao")
        capability = _capability(plugin)
        conexao = _conexao(plugin, user, estado=PluginConexao.ESTADO_CONECTADO)
        client = _build_client(app)
        _login(client, user.id)
        token = _csrf(user.id)
        criada = client.post(
            f"/plugins/{plugin.slug}/capabilities/{capability.chave}/restringir",
            data={"csrf_token": token, "politica_maxima": PluginCapability.POLITICA_PERMITIDA},
            follow_redirects=True,
        )
        assert criada.status_code == 200
        assert "restringida na sua conexão" in criada.get_data(as_text=True)
        db.session.expire_all()
        restricao = PluginRestricaoUsuario.query.filter_by(conexao_id=conexao.id).one()
        assert restricao.user_id == user.id
        assert restricao.capability_id == capability.id
        assert restricao.efeito == PluginRestricaoUsuario.EFEITO_BLOQUEADA_PELO_USUARIO
        evento = PluginEventoCentral.query.filter_by(
            conexao_id=conexao.id,
            tipo_evento=PluginEventoCentral.TIPO_RESTRICAO_USUARIO_ALTERADA,
            detalhe_codigo=PluginEventoCentral.DETALHE_RESTRICAO_REGISTRADA,
        ).one()
        assert evento.capability_id == capability.id
        pagina = client.get("/plugins").get_data(as_text=True)
        assert "restringida por você" in _card(pagina, plugin.slug)
        assert 'data-acao="remover-restricao"' in pagina
        removida = client.post(
            f"/plugins/{plugin.slug}/capabilities/{capability.chave}/remover-restricao",
            data={"csrf_token": token},
            follow_redirects=True,
        )
        assert removida.status_code == 200
        db.session.expire_all()
        assert PluginRestricaoUsuario.query.filter_by(conexao_id=conexao.id).count() == 0
        assert (
            PluginEventoCentral.query.filter_by(
                conexao_id=conexao.id,
                detalhe_codigo=PluginEventoCentral.DETALHE_RESTRICAO_REMOVIDA,
            ).count()
            == 1
        )
        assert db.session.get(PluginCapability, capability.id).politica_maxima == PluginCapability.POLITICA_PERMITIDA


def test_usuario_nao_libera_capability_bloqueada_nem_de_outro_plugin(app):
    with app.app_context():
        user, _conta, _franquia = _pessoa("teto-plugins@test.com", "conta-plugins-teto")
        outro, _conta_b, _franquia_b = _pessoa("teto-outro@test.com", "conta-plugins-teto-outro")
        plugin = _plugin(slug="catalogo-teto", adapter_key="adapter.teto")
        bloqueada = _capability(
            plugin,
            chave="acao.negada",
            nome="Acao negada pela LogCompleta",
            politica_maxima=PluginCapability.POLITICA_NEGADA,
            natureza=PluginCapability.NATUREZA_ATIVA,
        )
        plugin_b = _plugin(slug="catalogo-outro-plugin", nome="Outra integracao", adapter_key="adapter.outro_plugin")
        capability_b = _capability(plugin_b, chave="leitura.alheia", nome="Leitura alheia")
        conexao = _conexao(plugin, user, estado=PluginConexao.ESTADO_CONECTADO)
        conexao_b = _conexao(plugin_b, outro, estado=PluginConexao.ESTADO_CONECTADO)
        client = _build_client(app)
        _login(client, user.id)
        html = client.get("/plugins").get_data(as_text=True)
        card = _card(html, plugin.slug)
        assert "Bloqueada pela LogCompleta" in card
        assert 'data-acao="restringir"' not in card
        token = _csrf(user.id)
        tentativa = client.post(
            f"/plugins/{plugin.slug}/capabilities/{bloqueada.chave}/restringir",
            data={
                "csrf_token": token,
                "politica_maxima": PluginCapability.POLITICA_PERMITIDA,
                "capability_id": str(bloqueada.id),
            },
            follow_redirects=True,
        )
        assert tentativa.status_code == 200
        assert "não pode liberar" in tentativa.get_data(as_text=True).lower() or "Não foi possível" in tentativa.get_data(as_text=True) or "bloqueou" in tentativa.get_data(as_text=True)
        alheia = client.post(
            f"/plugins/{plugin.slug}/capabilities/{capability_b.chave}/restringir",
            data={"csrf_token": token, "capability_id": str(capability_b.id), "conexao_id": str(conexao_b.id)},
            follow_redirects=True,
        )
        assert alheia.status_code == 200
        db.session.expire_all()
        assert PluginRestricaoUsuario.query.count() == 0
        assert db.session.get(PluginCapability, bloqueada.id).politica_maxima == PluginCapability.POLITICA_NEGADA
        assert db.session.get(PluginCapability, capability_b.id).politica_maxima == PluginCapability.POLITICA_PERMITIDA
        assert db.session.get(PluginConexao, conexao.id).estado == PluginConexao.ESTADO_CONECTADO
        assert db.session.get(PluginConexao, conexao_b.id).estado == PluginConexao.ESTADO_CONECTADO


def test_usuario_nao_altera_restricao_de_outra_conexao(app):
    with app.app_context():
        ana, _conta_a, _franquia_a = _pessoa("ana-restricao@test.com", "conta-plugins-ana-rest")
        bruno, _conta_b, _franquia_b = _pessoa("bruno-restricao@test.com", "conta-plugins-bruno-rest")
        plugin = _plugin(slug="catalogo-restricao-alheia", adapter_key="adapter.restricao_alheia")
        capability = _capability(plugin)
        conexao_a = _conexao(plugin, ana, estado=PluginConexao.ESTADO_CONECTADO)
        conexao_b = _conexao(plugin, bruno, estado=PluginConexao.ESTADO_CONECTADO)
        from app.services.central_plugin_service import registrar_restricao_usuario

        restricao_b = registrar_restricao_usuario(
            conexao_id=conexao_b.id,
            capability_id=capability.id,
            user_id=bruno.id,
            usuario_originador_id=bruno.id,
        )
        client = _build_client(app)
        _login(client, ana.id)
        token = _csrf(ana.id)
        client.post(
            f"/plugins/{plugin.slug}/capabilities/{capability.chave}/remover-restricao",
            data={"csrf_token": token, "conexao_id": str(conexao_b.id)},
        )
        db.session.expire_all()
        assert db.session.get(PluginRestricaoUsuario, restricao_b.id) is not None
        assert PluginRestricaoUsuario.query.filter_by(conexao_id=conexao_a.id).count() == 0
        assert PluginRestricaoUsuario.query.filter_by(conexao_id=conexao_b.id).count() == 1


def test_desconexao_propria_mantem_historico_e_libera_slot(app):
    with app.app_context():
        user, _conta, _franquia = _pessoa("off-plugins@test.com", "conta-plugins-off")
        plugin = _plugin(slug="catalogo-desconectar", adapter_key="adapter.desconectar")
        conexao = _conexao(
            plugin,
            user,
            estado=PluginConexao.ESTADO_CONECTADO,
            identificador=_IDENTIFICADOR,
        )
        antes = conexao.id
        client = _build_client(app)
        _login(client, user.id)
        resposta = client.post(
            f"/plugins/{plugin.slug}/desconectar",
            data={"csrf_token": _csrf(user.id)},
            follow_redirects=True,
        )
        assert resposta.status_code == 200
        html = resposta.get_data(as_text=True)
        assert 'data-estado="desconectado"' in _card(html, plugin.slug)
        assert "Não há vínculo operacional ativo." in html
        db.session.expire_all()
        historico = db.session.get(PluginConexao, antes)
        assert historico is not None
        assert historico.estado == PluginConexao.ESTADO_DESCONECTADO
        assert historico.desconectado_em is not None
        assert historico.ocupa_slot() is False
        assert obter_conexao_usuario(user.id, plugin.id) is None
        assert (
            PluginEventoCentral.query.filter_by(
                conexao_id=antes,
                tipo_evento=PluginEventoCentral.TIPO_DESCONECTADO,
            ).count()
            == 1
        )
        nova = criar_conexao(
            plugin_id=plugin.id,
            user_id=user.id,
            titularidade=PluginConexao.TITULARIDADE_PESSOAL,
            usuario_originador_id=user.id,
        )
        assert nova.id != antes
        assert nova.estado == PluginConexao.ESTADO_AGUARDANDO_CONFIGURACAO


def test_desconexao_de_terceiro_e_negada(app):
    with app.app_context():
        ana, _conta_a, _franquia_a = _pessoa("ana-off@test.com", "conta-plugins-ana-off")
        bruno, _conta_b, _franquia_b = _pessoa("bruno-off@test.com", "conta-plugins-bruno-off")
        plugin = _plugin(slug="catalogo-off-alheio", adapter_key="adapter.off_alheio")
        conexao_b = _conexao(plugin, bruno, estado=PluginConexao.ESTADO_CONECTADO)
        client = _build_client(app)
        _login(client, ana.id)
        resposta = client.post(
            f"/plugins/{plugin.slug}/desconectar",
            data={"csrf_token": _csrf(ana.id), "conexao_id": str(conexao_b.id)},
        )
        assert resposta.status_code == 403
        assert resposta.get_data(as_text=True) == "Você só pode administrar a sua própria conexão."
        db.session.expire_all()
        assert db.session.get(PluginConexao, conexao_b.id).estado == PluginConexao.ESTADO_CONECTADO
        assert (
            PluginEventoCentral.query.filter_by(
                conexao_id=conexao_b.id,
                tipo_evento=PluginEventoCentral.TIPO_DESCONECTADO,
            ).count()
            == 0
        )


def test_pagina_nao_expoe_segredo_nem_identificador_completo(app):
    with app.app_context():
        user, _conta, _franquia = _pessoa("segredo-plugins@test.com", "conta-plugins-segredo")
        plugin = _plugin(slug="catalogo-segredo", adapter_key="adapter.segredo_visual")
        conexao = _conexao(
            plugin,
            user,
            estado=PluginConexao.ESTADO_CONECTADO,
            identificador=_IDENTIFICADOR,
            codigo=_CODIGO,
            resumo=_RESUMO,
        )
        vincular_referencia_cofre(conexao.id, _COFRE)
        client = _build_client(app)
        html = _html(client, user.id)
        assert _COFRE not in html
        assert "cofre_referencia" not in html
        assert _IDENTIFICADOR not in html
        assert "co…ef" in html
        assert _CODIGO not in html
        assert _RESUMO in html
        assert plugin.adapter_key not in html
        assert 'type="password"' not in html
        assert 'name="token"' not in html
        template = (_RAIZ / "app" / "templates" / "plugins.html").read_text(encoding="utf-8")
        servico = (_RAIZ / "app" / "services" / "central_plugin_usuario_service.py").read_text(encoding="utf-8")
        assert "cofre_referencia" not in template
        assert "PluginCredencialReferencia" not in servico
        assert "PluginConcessaoProvedor" not in servico


def test_post_sem_csrf_nao_grava(app):
    with app.app_context():
        user, _conta, _franquia = _pessoa("csrf-plugins@test.com", "conta-plugins-csrf-user")
        plugin = _plugin(slug="catalogo-csrf-user", adapter_key="adapter.csrf_user")
        capability = _capability(plugin)
        conexao = _conexao(plugin, user, estado=PluginConexao.ESTADO_CONECTADO)
        client = _build_client(app)
        _login(client, user.id)
        resposta = client.post(
            f"/plugins/{plugin.slug}/capabilities/{capability.chave}/restringir",
            data={"capability_id": str(capability.id)},
            follow_redirects=True,
        )
        assert resposta.status_code == 200
        assert "Não foi possível validar a solicitação." in resposta.get_data(as_text=True)
        db.session.expire_all()
        assert PluginRestricaoUsuario.query.filter_by(conexao_id=conexao.id).count() == 0
        assert db.session.get(PluginConexao, conexao.id).estado == PluginConexao.ESTADO_CONECTADO


def test_json_nao_cria_bypass(app):
    with app.app_context():
        user, _conta, _franquia = _pessoa("json-plugins@test.com", "conta-plugins-json-user")
        plugin = _plugin(slug="catalogo-json-user", adapter_key="adapter.json_user")
        capability = _capability(plugin)
        registrar_fluxo_conexao(FluxoConexaoPlugin(plugin.adapter_key))
        client = _build_client(app)
        _login(client, user.id)
        token = _csrf(user.id)
        conectar = client.post(
            f"/plugins/{plugin.slug}/conectar",
            json={"csrf_token": token},
            follow_redirects=True,
        )
        restringir = client.post(
            f"/plugins/{plugin.slug}/capabilities/{capability.chave}/restringir",
            json={"csrf_token": token, "capability_id": capability.id},
            follow_redirects=True,
        )
        assert "não aceita payload JSON" in conectar.get_data(as_text=True)
        assert "não aceita payload JSON" in restringir.get_data(as_text=True)
        assert PluginConexao.query.filter_by(user_id=user.id).count() == 0
        assert PluginRestricaoUsuario.query.count() == 0


def test_jornada_sem_provider_e_revalidacao_nao_altera_estado(app, monkeypatch):
    chamadas = []

    def _urlopen(*args, **kwargs):
        chamadas.append(args)
        raise AssertionError("provider externo")

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    with app.app_context():
        user, _conta, _franquia = _pessoa("jornada-plugins@test.com", "conta-plugins-jornada")
        plugin = _plugin(slug="catalogo-jornada", adapter_key="adapter.jornada_tecnica")
        corporativo = _plugin(
            slug="catalogo-so-corporativo",
            nome="Integracao corporativa futura",
            adapter_key="adapter.jornada_corporativa",
            suporta_titularidade_pessoal=False,
            suporta_titularidade_corporativa=True,
        )
        capability = _capability(plugin)
        registrar_fluxo_conexao(FluxoConexaoPlugin(plugin.adapter_key))
        registrar_fluxo_conexao(FluxoConexaoPlugin(corporativo.adapter_key))
        client = _build_client(app)
        _login(client, user.id)
        html = client.get("/plugins").get_data(as_text=True)
        assert 'data-acao="conectar"' in _card(html, plugin.slug)
        assert 'data-acao="conectar"' not in _card(html, corporativo.slug)
        assert "WhatsApp" not in html
        assert "OAuth" not in html
        token = _csrf(user.id)
        corporativo_post = client.post(
            f"/plugins/{corporativo.slug}/conectar",
            data={"csrf_token": token},
            follow_redirects=True,
        )
        assert corporativo_post.status_code == 200
        assert PluginConexao.query.filter_by(plugin_id=corporativo.id).count() == 0
        iniciada = client.post(
            f"/plugins/{plugin.slug}/conectar",
            data={"csrf_token": token},
            follow_redirects=True,
        )
        assert iniciada.status_code == 200
        db.session.expire_all()
        conexao = obter_conexao_usuario(user.id, plugin.id)
        assert conexao is not None
        assert conexao.estado == PluginConexao.ESTADO_AGUARDANDO_CONFIGURACAO
        assert (
            PluginEventoCentral.query.filter_by(
                conexao_id=conexao.id,
                tipo_evento=PluginEventoCentral.TIPO_CONEXAO_CRIADA,
            ).count()
            == 1
        )
        assert 'data-estado="aguardando_configuracao"' in iniciada.get_data(as_text=True)
        assert "ainda não foi concluída" in iniciada.get_data(as_text=True)
        alterar_estado_conexao(
            conexao.id,
            PluginConexao.ESTADO_CONECTADO,
            usuario_originador_id=user.id,
        )
        conectada = client.get("/plugins").get_data(as_text=True)
        assert 'data-estado="conectado"' in _card(conectada, plugin.slug)
        alterar_estado_conexao(
            conexao.id,
            PluginConexao.ESTADO_REQUER_ATENCAO,
            usuario_originador_id=user.id,
            diagnostico_resumo=_RESUMO,
        )
        atencao = client.get("/plugins").get_data(as_text=True)
        assert 'data-acao="revalidar"' in _card(atencao, plugin.slug)
        eventos_antes = PluginEventoCentral.query.filter_by(conexao_id=conexao.id).count()
        revalidar = client.post(
            f"/plugins/{plugin.slug}/revalidar",
            data={"csrf_token": token},
            follow_redirects=True,
        )
        assert "Nenhum estado foi alterado." in revalidar.get_data(as_text=True)
        db.session.expire_all()
        assert db.session.get(PluginConexao, conexao.id).estado == PluginConexao.ESTADO_REQUER_ATENCAO
        assert PluginEventoCentral.query.filter_by(conexao_id=conexao.id).count() == eventos_antes
        alterar_estado_conexao(
            conexao.id,
            PluginConexao.ESTADO_CONECTADO,
            usuario_originador_id=user.id,
        )
        client.post(
            f"/plugins/{plugin.slug}/capabilities/{capability.chave}/restringir",
            data={"csrf_token": token},
        )
        client.post(
            f"/plugins/{plugin.slug}/desconectar",
            data={"csrf_token": token},
            follow_redirects=True,
        )
        db.session.expire_all()
        assert db.session.get(PluginConexao, conexao.id).estado == PluginConexao.ESTADO_DESCONECTADO
        assert obter_conexao_usuario(user.id, plugin.id) is None
        assert chamadas == []


def test_nao_existe_chamada_a_provider_externo():
    arquivos = (
        _RAIZ / "app" / "services" / "central_plugin_usuario_service.py",
        _RAIZ / "app" / "user_plugins_routes.py",
        _RAIZ / "app" / "templates" / "plugins.html",
    )
    for arquivo in arquivos:
        texto = arquivo.read_text(encoding="utf-8").lower()
        assert "import requests" not in texto
        assert "import httpx" not in texto
        assert "urlopen" not in texto
        assert "whatsapp" not in texto
        assert "graph.facebook" not in texto
        assert "googleapis" not in texto
    template = (_RAIZ / "app" / "templates" / "plugins.html").read_text(encoding="utf-8").lower()
    assert "oauth" not in template
    assert "webhook" not in template
    assert "brobot" not in template
    canal = (_RAIZ / "app" / "services" / "central_plugin_whatsapp_service.py").read_text(encoding="utf-8").lower()
    assert "import requests" not in canal
    assert "import httpx" not in canal
    assert "urlopen" not in canal
    assert "graph.facebook" not in canal


_NUMERO_PUBLICO = "5511999990000"
_TELEFONE_POSTADO = "5511888880000"
_SUJEITO_WHATSAPP = "5511900003333"


def _plugin_whatsapp():
    return _plugin(slug="whatsapp", nome="WhatsApp", adapter_key="whatsapp_meta")


def _identidade_vinculada(user, sujeito: str = _SUJEITO_WHATSAPP):
    agora = utcnow_naive()
    identidade = IdentidadeCanalExterna(
        provedor="whatsapp_meta",
        sujeito_externo=sujeito,
        estado=IdentidadeCanalExterna.ESTADO_VINCULADA,
        user_id=user.id,
        interacoes_uteis=0,
        vinculada_em=agora,
        revogada_em=None,
        criada_em=agora,
        atualizada_em=agora,
    )
    db.session.add(identidade)
    db.session.commit()
    return identidade


def _ocupando(user_id: int):
    return PluginConexao.query.filter(
        PluginConexao.user_id == user_id,
        PluginConexao.estado.in_(PluginConexao.ESTADOS_QUE_OCUPAM_SLOT),
    ).all()


def test_card_whatsapp_desconectado_mostra_conectar_sem_numero(app, monkeypatch):
    monkeypatch.setenv("WHATSAPP_PUBLIC_NUMBER", _NUMERO_PUBLICO)
    with app.app_context():
        user, _conta, _franquia = _pessoa("card-whatsapp@test.com", "conta-card-whatsapp")
        plugin = _plugin_whatsapp()
        client = _build_client(app)
        _login(client, user.id)
        html = client.get("/plugins").get_data(as_text=True)
        card = _card(html, plugin.slug)
        assert "Conectar WhatsApp" in card
        assert 'data-acao="conectar"' in card
        assert _NUMERO_PUBLICO not in html
        assert "wa.me" not in html
        assert 'type="tel"' not in card


def test_config_ausente_nao_cria_conexao_nem_aguardando(app, monkeypatch):
    monkeypatch.delenv("WHATSAPP_PUBLIC_NUMBER", raising=False)
    with app.app_context():
        user, _conta, _franquia = _pessoa("sem-numero-whatsapp@test.com", "conta-sem-numero-whatsapp")
        plugin = _plugin_whatsapp()
        client = _build_client(app)
        _login(client, user.id)
        pagina = client.get("/plugins")
        assert MENSAGEM_CANAL_INDISPONIVEL in pagina.get_data(as_text=True)
        assert 'data-estado="aguardando_configuracao"' not in _card(pagina.get_data(as_text=True), plugin.slug)
        resposta = client.post(
            f"/plugins/{plugin.slug}/conectar",
            data={"csrf_token": _csrf(user.id), "telefone": _TELEFONE_POSTADO},
            follow_redirects=True,
        )
        assert MENSAGEM_CANAL_INDISPONIVEL in resposta.get_data(as_text=True)
        assert PluginConexao.query.filter_by(user_id=user.id).count() == 0
        monkeypatch.setenv("WHATSAPP_PUBLIC_NUMBER", "+5511999990000")
        invalido = client.post(
            f"/plugins/{plugin.slug}/conectar",
            data={"csrf_token": _csrf(user.id)},
            follow_redirects=True,
        )
        assert MENSAGEM_CANAL_INDISPONIVEL in invalido.get_data(as_text=True)
        assert PluginConexao.query.filter_by(user_id=user.id).count() == 0


def test_clique_conectar_reutiliza_uma_conexao_e_abre_deep_link(app, monkeypatch):
    monkeypatch.setenv("WHATSAPP_PUBLIC_NUMBER", _NUMERO_PUBLICO)
    with app.app_context():
        user, _conta, _franquia = _pessoa("conectar-whatsapp@test.com", "conta-conectar-whatsapp")
        outro, _conta_b, _franquia_b = _pessoa("outro-conectar-whatsapp@test.com", "conta-outro-conectar-whatsapp")
        plugin = _plugin_whatsapp()
        client = _build_client(app)
        _login(client, user.id)
        token = _csrf(user.id)
        sem_csrf = client.post(f"/plugins/{plugin.slug}/conectar", data={"telefone": _TELEFONE_POSTADO})
        assert sem_csrf.status_code in (302, 303)
        assert "wa.me" not in (sem_csrf.headers.get("Location") or "")
        assert PluginConexao.query.filter_by(user_id=user.id).count() == 0
        primeiro = client.post(
            f"/plugins/{plugin.slug}/conectar",
            data={
                "csrf_token": token,
                "telefone": _TELEFONE_POSTADO,
                "user_id": str(outro.id),
                "destino": "https://evil.example/wa",
            },
            follow_redirects=False,
        )
        destino = primeiro.headers.get("Location") or ""
        assert primeiro.status_code in (302, 303)
        assert destino.startswith(f"https://wa.me/{_NUMERO_PUBLICO}?")
        assert "text=Quero%20me%20cadastrar" in destino
        assert _TELEFONE_POSTADO not in destino
        assert "evil.example" not in destino
        db.session.expire_all()
        conexoes = _ocupando(user.id)
        assert len(conexoes) == 1
        assert conexoes[0].estado == PluginConexao.ESTADO_AGUARDANDO_CONFIGURACAO
        assert conexoes[0].user_id == user.id
        assert conexoes[0].identificador_externo is None
        segundo = client.post(
            f"/plugins/{plugin.slug}/conectar",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert (segundo.headers.get("Location") or "").startswith(f"https://wa.me/{_NUMERO_PUBLICO}?")
        db.session.expire_all()
        assert len(_ocupando(user.id)) == 1
        assert (
            PluginEventoCentral.query.filter_by(
                conexao_id=conexoes[0].id,
                tipo_evento=PluginEventoCentral.TIPO_CONEXAO_CRIADA,
            ).count()
            == 1
        )


def test_vinculo_legado_aparece_conectado(app, monkeypatch):
    monkeypatch.delenv("WHATSAPP_PUBLIC_NUMBER", raising=False)
    with app.app_context():
        user, _conta, _franquia = _pessoa("legado-whatsapp@test.com", "conta-legado-whatsapp")
        outro, _conta_b, _franquia_b = _pessoa("legado-outro-whatsapp@test.com", "conta-legado-outro-whatsapp")
        plugin = _plugin_whatsapp()
        _identidade_vinculada(user)
        client = _build_client(app)
        _login(client, user.id)
        html = client.get("/plugins").get_data(as_text=True)
        assert 'data-estado="conectado"' in _card(html, plugin.slug)
        db.session.expire_all()
        conexao = obter_conexao_usuario(user.id, plugin.id)
        assert conexao is not None
        assert conexao.estado == PluginConexao.ESTADO_CONECTADO
        de_novo = client.get("/plugins").get_data(as_text=True)
        assert 'data-estado="conectado"' in _card(de_novo, plugin.slug)
        _login(client, outro.id)
        alheio = client.get("/plugins").get_data(as_text=True)
        assert 'data-estado="conectado"' not in _card(alheio, plugin.slug)
        assert obter_conexao_usuario(outro.id, plugin.id) is None


def test_plugin_bloqueado_vence_o_vinculo_na_tela(app):
    with app.app_context():
        user, _conta, _franquia = _pessoa("bloqueado-whatsapp@test.com", "conta-bloqueado-whatsapp")
        plugin = _plugin_whatsapp()
        _identidade_vinculada(user, "5511900007777")
        definir_status_plugin(plugin.id, Plugin.STATUS_BLOQUEADO)
        client = _build_client(app)
        _login(client, user.id)
        html = client.get("/plugins").get_data(as_text=True)
        assert 'data-estado="bloqueado"' in _card(html, plugin.slug)
        assert "Conectar WhatsApp" not in _card(html, plugin.slug)


def test_desconectar_revoga_identidade_e_proxima_mensagem_e_guest(app, monkeypatch):
    monkeypatch.setenv("WHATSAPP_PUBLIC_NUMBER", _NUMERO_PUBLICO)
    with app.app_context():
        user, _conta, _franquia = _pessoa("desconectar-whatsapp@test.com", "conta-desconectar-whatsapp")
        plugin = _plugin_whatsapp()
        identidade = _identidade_vinculada(user)
        client = _build_client(app)
        _login(client, user.id)
        client.get("/plugins")
        db.session.expire_all()
        assert obter_conexao_usuario(user.id, plugin.id).estado == PluginConexao.ESTADO_CONECTADO
        resposta = client.post(
            f"/plugins/{plugin.slug}/desconectar",
            data={"csrf_token": _csrf(user.id)},
            follow_redirects=True,
        )
        assert resposta.status_code == 200
        db.session.expire_all()
        gravada = db.session.get(IdentidadeCanalExterna, identidade.id)
        assert gravada.estado == IdentidadeCanalExterna.ESTADO_REVOGADA
        assert gravada.revogada_em is not None
        assert gravada.user_id == user.id
        assert obter_conexao_usuario(user.id, plugin.id) is None
        historico = PluginConexao.query.filter_by(user_id=user.id, plugin_id=plugin.id).one()
        assert historico.estado == PluginConexao.ESTADO_DESCONECTADO
        assert (
            PluginEventoCentral.query.filter_by(
                conexao_id=historico.id,
                tipo_evento=PluginEventoCentral.TIPO_DESCONECTADO,
            ).count()
            == 1
        )
        nova = obter_ou_criar_identidade_externa(
            provedor="whatsapp_meta",
            sujeito_externo=_SUJEITO_WHATSAPP,
        )
        assert nova.id != gravada.id
        assert nova.estado == IdentidadeCanalExterna.ESTADO_GUEST
        assert nova.user_id is None
        assert ExecucaoOperacionalCanal.query.count() == 0


def test_card_conectado_exige_exatamente_uma_identidade_valida(app, monkeypatch):
    monkeypatch.delenv("WHATSAPP_PUBLIC_NUMBER", raising=False)
    with app.app_context():
        plugin = _plugin_whatsapp()
        client = _build_client(app)

        uma, _conta, _franquia = _pessoa("uma-identidade-wa@test.com", "conta-uma-identidade-wa")
        _identidade_vinculada(uma, "5511900004101")
        _login(client, uma.id)
        html_uma = client.get("/plugins").get_data(as_text=True)
        assert 'data-estado="conectado"' in _card(html_uma, plugin.slug)
        db.session.expire_all()
        assert obter_conexao_usuario(uma.id, plugin.id).estado == PluginConexao.ESTADO_CONECTADO

        revogada, _conta_r, _franquia_r = _pessoa(
            "revogada-identidade-wa@test.com",
            "conta-revogada-identidade-wa",
        )
        identidade = _identidade_vinculada(revogada, "5511900004102")
        _login(client, revogada.id)
        client.get("/plugins")
        db.session.expire_all()
        assert obter_conexao_usuario(revogada.id, plugin.id).estado == PluginConexao.ESTADO_CONECTADO
        gravada = db.session.get(IdentidadeCanalExterna, identidade.id)
        gravada.estado = IdentidadeCanalExterna.ESTADO_REVOGADA
        gravada.revogada_em = utcnow_naive()
        db.session.commit()
        html_revogada = client.get("/plugins").get_data(as_text=True)
        card_revogada = _card(html_revogada, plugin.slug)
        assert 'data-estado="conectado"' not in card_revogada
        assert 'data-estado="aguardando_configuracao"' in card_revogada
        db.session.expire_all()
        assert (
            obter_conexao_usuario(revogada.id, plugin.id).estado
            == PluginConexao.ESTADO_AGUARDANDO_CONFIGURACAO
        )

        duplicada, _conta_d, _franquia_d = _pessoa(
            "duas-identidades-wa@test.com",
            "conta-duas-identidades-wa",
        )
        _identidade_vinculada(duplicada, "5511900004103")
        _login(client, duplicada.id)
        client.get("/plugins")
        _identidade_vinculada(duplicada, "5511900004104")
        html_dupla = client.get("/plugins").get_data(as_text=True)
        card_dupla = _card(html_dupla, plugin.slug)
        assert 'data-estado="conectado"' not in card_dupla
        assert 'data-estado="requer_atencao"' in card_dupla
        db.session.expire_all()
        assert (
            obter_conexao_usuario(duplicada.id, plugin.id).estado
            == PluginConexao.ESTADO_REQUER_ATENCAO
        )


def test_revalidar_whatsapp_confirma_ou_volta_para_configuracao(app, monkeypatch):
    monkeypatch.setenv("WHATSAPP_PUBLIC_NUMBER", _NUMERO_PUBLICO)
    with app.app_context():
        user, _conta, _franquia = _pessoa("revalidar-whatsapp@test.com", "conta-revalidar-whatsapp")
        plugin = _plugin_whatsapp()
        identidade = _identidade_vinculada(user, "5511900008888")
        client = _build_client(app)
        _login(client, user.id)
        client.get("/plugins")
        token = _csrf(user.id)
        pagina = client.get("/plugins").get_data(as_text=True)
        assert 'data-acao="revalidar"' in _card(pagina, plugin.slug)
        mantem = client.post(
            f"/plugins/{plugin.slug}/revalidar",
            data={"csrf_token": token},
            follow_redirects=True,
        )
        assert "foi confirmada" in mantem.get_data(as_text=True)
        db.session.expire_all()
        assert obter_conexao_usuario(user.id, plugin.id).estado == PluginConexao.ESTADO_CONECTADO
        gravada = db.session.get(IdentidadeCanalExterna, identidade.id)
        gravada.estado = IdentidadeCanalExterna.ESTADO_REVOGADA
        gravada.revogada_em = utcnow_naive()
        db.session.commit()
        ausente = client.post(
            f"/plugins/{plugin.slug}/revalidar",
            data={"csrf_token": token},
            follow_redirects=True,
        )
        assert "não foi marcada como conectada" in ausente.get_data(as_text=True)
        db.session.expire_all()
        conexao = obter_conexao_usuario(user.id, plugin.id)
        assert conexao.estado == PluginConexao.ESTADO_AGUARDANDO_CONFIGURACAO
        html = client.get("/plugins").get_data(as_text=True)
        card = _card(html, plugin.slug)
        assert 'data-estado="aguardando_configuracao"' in card
        assert "Conectar WhatsApp" in card
        assert 'data-estado="conectado"' not in card
