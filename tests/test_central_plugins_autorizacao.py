"""Central de Plugins — autorização efetiva (SCRUM-222, lote 2)."""
from __future__ import annotations

import inspect

import pytest
from sqlalchemy import event as sa_event

import app.services.central_plugin_service as central_plugin_service
from app.extensions import db
from app.models import (
    Plugin,
    PluginCapability,
    PluginConcessaoProvedor,
    PluginConexao,
    PluginEventoCentral,
    PluginRestricaoUsuario,
    utcnow_naive,
)
from app.services.central_plugin_service import (
    ConcessaoProvedorInvalidaError,
    ContextoAutorizacaoPlugin,
    alterar_estado_conexao,
    avaliar_autorizacao_plugin,
    criar_conexao,
    registrar_capability,
    registrar_concessao_provedor,
    registrar_plugin,
    registrar_restricao_usuario,
    vincular_referencia_cofre,
)
from tests.conftest import seed_conta_franquia_cliente, seed_usuario


@pytest.fixture(autouse=True)
def _fk_sqlite_deste_modulo(ctx):
    def _pragma(valor: str) -> None:
        db.session.rollback()
        conexao = db.engine.raw_connection()
        try:
            if conexao.in_transaction:
                conexao.rollback()
            cursor = conexao.cursor()
            cursor.execute(f"PRAGMA foreign_keys={valor}")
            cursor.close()
        finally:
            conexao.close()

    _pragma("ON")
    try:
        yield
    finally:
        _pragma("OFF")


def _pessoas(slug: str, emails: tuple[str, ...]):
    conta, franquia = seed_conta_franquia_cliente(slug=slug)
    usuarios = [
        seed_usuario(franquia.id, conta.id, email=email) for email in emails
    ]
    return conta, usuarios


def _plugin(**kwargs):
    dados = dict(
        slug="teste-auth-central",
        nome="Plugin tecnico ficticio de autorizacao",
        descricao="Fixture interno da Central. Nao e integracao operacional.",
        adapter_key="adapter.teste_auth",
        suporta_titularidade_pessoal=True,
        suporta_titularidade_corporativa=True,
        status=Plugin.STATUS_DISPONIVEL,
    )
    dados.update(kwargs)
    return registrar_plugin(**dados)


def _capability(
    plugin,
    chave="leitura.teste",
    *,
    politica=PluginCapability.POLITICA_PERMITIDA,
    status=PluginCapability.STATUS_DISPONIVEL,
):
    return registrar_capability(
        plugin_id=plugin.id,
        chave=chave,
        nome=f"Capability ficticia {chave}",
        descricao="Capability de teste. Nao representa provider real.",
        politica_maxima=politica,
        natureza=PluginCapability.NATUREZA_PASSIVA,
        status=status,
    )


def _conexao(user, plugin, *, conta=None, estado=PluginConexao.ESTADO_CONECTADO):
    conexao = criar_conexao(
        plugin_id=plugin.id,
        user_id=user.id,
        titularidade=(
            PluginConexao.TITULARIDADE_CORPORATIVA
            if conta is not None
            else PluginConexao.TITULARIDADE_PESSOAL
        ),
        usuario_originador_id=user.id,
        conta_id=conta.id if conta is not None else None,
    )
    if estado != conexao.estado:
        alterar_estado_conexao(
            conexao.id,
            estado,
            usuario_originador_id=user.id,
        )
    return db.session.get(PluginConexao, conexao.id)


def _contexto(user, plugin, conexao, capability, **extras):
    dados = dict(
        user_id=user.id,
        plugin_id=plugin.id,
        plugin_slug=plugin.slug,
        conexao_id=conexao.id,
        capability_id=capability.id,
        capability_chave=capability.chave,
        canal_origem="web",
    )
    dados.update(extras)
    return ContextoAutorizacaoPlugin(**dados)


def _negar_camadas_inferiores(monkeypatch):
    def _proibido(*args, **kwargs):
        raise AssertionError("camada inferior nao deveria ser consultada")

    monkeypatch.setattr(central_plugin_service, "obter_restricao_usuario", _proibido)
    monkeypatch.setattr(central_plugin_service, "obter_concessao_provedor", _proibido)


def _eventos_negacao(conexao_id: int):
    return PluginEventoCentral.query.filter_by(
        conexao_id=conexao_id,
        tipo_evento=PluginEventoCentral.TIPO_AUTORIZACAO_NEGADA,
    )


def test_plugin_inexistente_nega(ctx):
    conta, (user,) = _pessoas("conta-auth-plugin-ausente", ("dono@test.com",))
    plugin = _plugin(suporta_titularidade_corporativa=False)
    capability = _capability(plugin)
    conexao = _conexao(user, plugin)
    antes = PluginEventoCentral.query.count()
    por_id = avaliar_autorizacao_plugin(
        _contexto(user, plugin, conexao, capability, plugin_id=999999, plugin_slug=None)
    )
    por_slug = avaliar_autorizacao_plugin(
        _contexto(
            user,
            plugin,
            conexao,
            capability,
            plugin_id=None,
            plugin_slug="plugin-ausente",
        )
    )
    assert por_id.permitido is False
    assert por_id.motivo_codigo == PluginEventoCentral.MOTIVO_PLUGIN_INEXISTENTE
    assert por_id.camada == "plugin"
    assert por_slug.motivo_codigo == PluginEventoCentral.MOTIVO_PLUGIN_INEXISTENTE
    assert PluginEventoCentral.query.count() == antes
    assert conta.id is not None


def test_conexao_inexistente_nega(ctx):
    _conta, (user,) = _pessoas("conta-auth-conexao-ausente", ("dono@test.com",))
    plugin = _plugin(suporta_titularidade_corporativa=False)
    capability = _capability(plugin)
    antes = PluginEventoCentral.query.count()
    resultado = avaliar_autorizacao_plugin(
        ContextoAutorizacaoPlugin(
            user_id=user.id,
            plugin_id=plugin.id,
            conexao_id=999999,
            capability_id=capability.id,
            canal_origem="web",
        )
    )
    assert resultado.permitido is False
    assert resultado.motivo_codigo == PluginEventoCentral.MOTIVO_CONEXAO_INEXISTENTE
    assert resultado.conexao_id is None
    assert PluginEventoCentral.query.count() == antes


def test_conexao_de_outro_usuario_nega(ctx):
    _conta, (dono, outro) = _pessoas(
        "conta-auth-outro-user",
        ("dono@test.com", "outro@test.com"),
    )
    plugin = _plugin(suporta_titularidade_corporativa=False)
    capability = _capability(plugin)
    conexao = _conexao(dono, plugin)
    registrar_concessao_provedor(
        conexao_id=conexao.id,
        capability_id=capability.id,
        resultado=PluginConcessaoProvedor.RESULTADO_CONCEDIDA,
    )
    resultado = avaliar_autorizacao_plugin(
        _contexto(outro, plugin, conexao, capability)
    )
    assert resultado.permitido is False
    assert resultado.motivo_codigo == PluginEventoCentral.MOTIVO_CONEXAO_NAO_PERTENCE
    evento = _eventos_negacao(conexao.id).one()
    assert evento.user_id == dono.id
    assert evento.usuario_originador_id == outro.id


def test_plugin_e_capability_incompativeis_negam(ctx):
    _conta, (user,) = _pessoas("conta-auth-capability-alheia", ("dono@test.com",))
    plugin = _plugin(suporta_titularidade_corporativa=False)
    outro = _plugin(
        slug="teste-auth-outro",
        adapter_key="adapter.teste_auth_outro",
        suporta_titularidade_corporativa=False,
    )
    capability = _capability(plugin)
    alheia = _capability(outro, "acao.alheia")
    conexao = _conexao(user, plugin)
    registrar_concessao_provedor(
        conexao_id=conexao.id,
        capability_id=capability.id,
        resultado=PluginConcessaoProvedor.RESULTADO_CONCEDIDA,
    )
    por_capability = avaliar_autorizacao_plugin(
        _contexto(user, plugin, conexao, capability, capability_id=alheia.id, capability_chave=None)
    )
    por_plugin = avaliar_autorizacao_plugin(
        _contexto(user, plugin, conexao, capability, plugin_id=outro.id, plugin_slug=outro.slug)
    )
    assert por_capability.permitido is False
    assert por_capability.motivo_codigo == PluginEventoCentral.MOTIVO_CAPABILITY_INEXISTENTE
    assert por_capability.capability_id is None
    assert por_plugin.permitido is False
    assert por_plugin.motivo_codigo == PluginEventoCentral.MOTIVO_CONTEXTO_INVALIDO
    with pytest.raises(ConcessaoProvedorInvalidaError):
        registrar_concessao_provedor(
            conexao_id=conexao.id,
            capability_id=alheia.id,
            resultado=PluginConcessaoProvedor.RESULTADO_CONCEDIDA,
        )


def test_intersecao_completa_permite(ctx):
    _conta, (user,) = _pessoas("conta-auth-allow", ("dono@test.com",))
    plugin = _plugin(suporta_titularidade_corporativa=False)
    capability = _capability(plugin)
    conexao = _conexao(user, plugin)
    registrar_concessao_provedor(
        conexao_id=conexao.id,
        capability_id=capability.id,
        resultado=PluginConcessaoProvedor.RESULTADO_CONCEDIDA,
    )
    marcador = registrar_plugin(
        slug="marcador-allow",
        nome="Marcador pendente",
        adapter_key="adapter.marcador_allow",
        suporta_titularidade_pessoal=True,
        suporta_titularidade_corporativa=False,
        commit=False,
    )
    resultado = avaliar_autorizacao_plugin(_contexto(user, plugin, conexao, capability))
    assert resultado.permitido is True
    assert resultado.motivo_codigo == PluginEventoCentral.MOTIVO_AUTORIZADA
    assert resultado.camada == "efetiva"
    assert resultado.politica_efetiva.politica_maxima == PluginCapability.POLITICA_PERMITIDA
    assert resultado.politica_efetiva.restricao_usuario == "ausente"
    assert (
        resultado.politica_efetiva.concessao_provedor
        == PluginConcessaoProvedor.RESULTADO_CONCEDIDA
    )
    assert _eventos_negacao(conexao.id).count() == 0
    assert "mensagem" not in resultado.para_dict()
    assert db.session.query(Plugin).filter_by(slug=marcador.slug).count() == 1
    db.session.rollback()
    assert db.session.query(Plugin).filter_by(slug=marcador.slug).count() == 0


def test_teto_logcompleta_bloqueia_sem_consultar_camadas_inferiores(ctx, monkeypatch):
    _conta, (user,) = _pessoas("conta-auth-teto", ("dono@test.com",))
    plugin = _plugin(suporta_titularidade_corporativa=False)
    capability = _capability(plugin, politica=PluginCapability.POLITICA_NEGADA)
    conexao = _conexao(user, plugin)
    registrar_concessao_provedor(
        conexao_id=conexao.id,
        capability_id=capability.id,
        resultado=PluginConcessaoProvedor.RESULTADO_CONCEDIDA,
    )
    _negar_camadas_inferiores(monkeypatch)
    resultado = avaliar_autorizacao_plugin(_contexto(user, plugin, conexao, capability))
    assert resultado.permitido is False
    assert resultado.motivo_codigo == PluginEventoCentral.MOTIVO_BLOQUEADA_PELA_PLATAFORMA
    assert resultado.camada == "politica_logcompleta"
    assert resultado.politica_efetiva.politica_maxima == PluginCapability.POLITICA_NEGADA
    assert resultado.politica_efetiva.restricao_usuario is None
    assert resultado.politica_efetiva.concessao_provedor is None


def test_restricao_do_usuario_nega_mesmo_com_provider(ctx, monkeypatch):
    _conta, (user,) = _pessoas("conta-auth-restricao", ("dono@test.com",))
    plugin = _plugin(suporta_titularidade_corporativa=False)
    capability = _capability(plugin)
    conexao = _conexao(user, plugin)
    registrar_restricao_usuario(
        conexao_id=conexao.id,
        capability_id=capability.id,
        user_id=user.id,
        usuario_originador_id=user.id,
    )
    registrar_concessao_provedor(
        conexao_id=conexao.id,
        capability_id=capability.id,
        resultado=PluginConcessaoProvedor.RESULTADO_CONCEDIDA,
    )

    def _proibido(*args, **kwargs):
        raise AssertionError("provider nao deveria ser consultado")

    monkeypatch.setattr(central_plugin_service, "obter_concessao_provedor", _proibido)
    resultado = avaliar_autorizacao_plugin(_contexto(user, plugin, conexao, capability))
    assert resultado.permitido is False
    assert resultado.motivo_codigo == PluginEventoCentral.MOTIVO_BLOQUEADA_PELO_USUARIO
    assert resultado.politica_efetiva.politica_maxima == PluginCapability.POLITICA_PERMITIDA
    assert resultado.politica_efetiva.concessao_provedor is None


def test_provider_negou(ctx):
    _conta, (user,) = _pessoas("conta-auth-provider-nega", ("dono@test.com",))
    plugin = _plugin(suporta_titularidade_corporativa=False)
    capability = _capability(plugin)
    conexao = _conexao(user, plugin)
    registrar_concessao_provedor(
        conexao_id=conexao.id,
        capability_id=capability.id,
        resultado=PluginConcessaoProvedor.RESULTADO_NEGADA,
    )
    resultado = avaliar_autorizacao_plugin(_contexto(user, plugin, conexao, capability))
    assert resultado.permitido is False
    assert resultado.motivo_codigo == PluginEventoCentral.MOTIVO_CONCESSAO_PROVEDOR_NEGADA
    assert (
        resultado.politica_efetiva.concessao_provedor
        == PluginConcessaoProvedor.RESULTADO_NEGADA
    )


def test_provider_sem_concessao_nega(ctx):
    _conta, (user,) = _pessoas("conta-auth-provider-ausente", ("dono@test.com",))
    plugin = _plugin(suporta_titularidade_corporativa=False)
    capability = _capability(plugin)
    conexao = _conexao(user, plugin)
    assert PluginConcessaoProvedor.query.count() == 0
    resultado = avaliar_autorizacao_plugin(_contexto(user, plugin, conexao, capability))
    assert resultado.permitido is False
    assert resultado.motivo_codigo == PluginEventoCentral.MOTIVO_CONCESSAO_PROVEDOR_AUSENTE
    assert resultado.politica_efetiva.restricao_usuario == "ausente"
    assert resultado.politica_efetiva.concessao_provedor == "ausente"
    assert resultado.politica_efetiva.politica_maxima == PluginCapability.POLITICA_PERMITIDA


@pytest.mark.parametrize(
    "status",
    [
        Plugin.STATUS_RASCUNHO,
        Plugin.STATUS_DESABILITADO,
        Plugin.STATUS_BLOQUEADO,
    ],
)
def test_plugin_indisponivel_nega_sem_camada_inferior(ctx, monkeypatch, status):
    nome = status.replace("_", "-")
    _conta, (user,) = _pessoas(f"conta-auth-plugin-{nome}", (f"{nome}@test.com",))
    plugin = _plugin(
        slug=f"teste-auth-plugin-{nome}",
        adapter_key=f"adapter.plugin.{nome.replace('-', '.')}",
        suporta_titularidade_corporativa=False,
    )
    capability = _capability(plugin)
    conexao = _conexao(user, plugin)
    registrar_concessao_provedor(
        conexao_id=conexao.id,
        capability_id=capability.id,
        resultado=PluginConcessaoProvedor.RESULTADO_CONCEDIDA,
    )
    plugin.status = status
    db.session.commit()
    antes = PluginEventoCentral.query.count()
    _negar_camadas_inferiores(monkeypatch)
    resultado = avaliar_autorizacao_plugin(_contexto(user, plugin, conexao, capability))
    assert resultado.permitido is False
    assert resultado.motivo_codigo == PluginEventoCentral.MOTIVO_PLUGIN_INDISPONIVEL
    assert resultado.camada == "plugin"
    assert resultado.plugin_id == plugin.id
    assert PluginEventoCentral.query.count() == antes


def test_capability_indisponivel_nega_sem_camada_inferior(ctx, monkeypatch):
    _conta, (user,) = _pessoas("conta-auth-capability-off", ("dono@test.com",))
    plugin = _plugin(suporta_titularidade_corporativa=False)
    capability = _capability(plugin, status=PluginCapability.STATUS_DESABILITADO)
    conexao = _conexao(user, plugin)
    registrar_concessao_provedor(
        conexao_id=conexao.id,
        capability_id=capability.id,
        resultado=PluginConcessaoProvedor.RESULTADO_CONCEDIDA,
    )
    _negar_camadas_inferiores(monkeypatch)
    resultado = avaliar_autorizacao_plugin(_contexto(user, plugin, conexao, capability))
    assert resultado.permitido is False
    assert resultado.motivo_codigo == PluginEventoCentral.MOTIVO_CAPABILITY_INDISPONIVEL


@pytest.mark.parametrize(
    "estado",
    [
        PluginConexao.ESTADO_DISPONIVEL,
        PluginConexao.ESTADO_AGUARDANDO_CONFIGURACAO,
        PluginConexao.ESTADO_REQUER_ATENCAO,
        PluginConexao.ESTADO_DESABILITADO,
        PluginConexao.ESTADO_BLOQUEADO,
        PluginConexao.ESTADO_DESCONECTADO,
    ],
)
def test_conexao_fora_de_conectado_nega(ctx, monkeypatch, estado):
    nome = estado.replace("_", "-")
    _conta, (user,) = _pessoas(f"conta-auth-{nome}", (f"{nome}@test.com",))
    plugin = _plugin(
        slug=f"teste-auth-{nome}",
        adapter_key=f"adapter.{nome.replace('-', '.')}",
        suporta_titularidade_corporativa=False,
    )
    capability = _capability(plugin)
    conexao = _conexao(user, plugin, estado=estado)
    registrar_concessao_provedor(
        conexao_id=conexao.id,
        capability_id=capability.id,
        resultado=PluginConcessaoProvedor.RESULTADO_CONCEDIDA,
    )
    _negar_camadas_inferiores(monkeypatch)
    resultado = avaliar_autorizacao_plugin(_contexto(user, plugin, conexao, capability))
    assert conexao.estado == estado
    assert resultado.permitido is False
    assert resultado.motivo_codigo == PluginEventoCentral.MOTIVO_CONEXAO_NAO_OPERACIONAL


def test_conta_incompativel_em_conexao_corporativa_nega(ctx):
    conta, (user,) = _pessoas("conta-auth-corp", ("titular@test.com",))
    outra, _franquia = seed_conta_franquia_cliente(slug="conta-auth-corp-alheia")
    plugin = _plugin()
    capability = _capability(plugin)
    conexao = _conexao(user, plugin, conta=conta)
    registrar_concessao_provedor(
        conexao_id=conexao.id,
        capability_id=capability.id,
        resultado=PluginConcessaoProvedor.RESULTADO_CONCEDIDA,
    )
    divergente = avaliar_autorizacao_plugin(
        _contexto(user, plugin, conexao, capability, conta_id=outra.id)
    )
    assert divergente.permitido is False
    assert divergente.motivo_codigo == PluginEventoCentral.MOTIVO_CONTA_INCOMPATIVEL
    sem_conta_no_contexto = avaliar_autorizacao_plugin(
        _contexto(user, plugin, conexao, capability, conta_id=None)
    )
    assert sem_conta_no_contexto.permitido is True
    compativel = avaliar_autorizacao_plugin(
        _contexto(user, plugin, conexao, capability, conta_id=conta.id)
    )
    assert compativel.permitido is True


def test_mesma_conta_com_outro_user_nao_autoriza(ctx):
    conta, (titular, colega) = _pessoas(
        "conta-auth-mesma-conta",
        ("titular@test.com", "colega@test.com"),
    )
    assert titular.conta_id == colega.conta_id == conta.id
    plugin = _plugin()
    capability = _capability(plugin)
    conexao = _conexao(titular, plugin, conta=conta)
    registrar_concessao_provedor(
        conexao_id=conexao.id,
        capability_id=capability.id,
        resultado=PluginConcessaoProvedor.RESULTADO_CONCEDIDA,
    )
    resultado = avaliar_autorizacao_plugin(
        _contexto(colega, plugin, conexao, capability, conta_id=conta.id)
    )
    assert resultado.permitido is False
    assert resultado.motivo_codigo == PluginEventoCentral.MOTIVO_CONEXAO_NAO_PERTENCE
    assert resultado.camada == "conexao"


def test_contexto_incompleto_nega(ctx):
    _conta, (user,) = _pessoas("conta-auth-contexto", ("dono@test.com",))
    plugin = _plugin(suporta_titularidade_corporativa=False)
    capability = _capability(plugin)
    conexao = _conexao(user, plugin)
    vazio = avaliar_autorizacao_plugin(ContextoAutorizacaoPlugin())
    sem_canal = avaliar_autorizacao_plugin(
        _contexto(user, plugin, conexao, capability, canal_origem=None)
    )
    sem_capability = avaliar_autorizacao_plugin(
        _contexto(
            user,
            plugin,
            conexao,
            capability,
            capability_id=None,
            capability_chave=None,
        )
    )
    usuario_inexistente = avaliar_autorizacao_plugin(
        _contexto(user, plugin, conexao, capability, user_id=999999)
    )
    segredo = avaliar_autorizacao_plugin(
        _contexto(user, plugin, conexao, capability, correlation_id="token=opaco")
    )
    assert vazio.motivo_codigo == PluginEventoCentral.MOTIVO_CONTEXTO_INVALIDO
    assert sem_canal.motivo_codigo == PluginEventoCentral.MOTIVO_CONTEXTO_INVALIDO
    assert sem_capability.motivo_codigo == PluginEventoCentral.MOTIVO_CONTEXTO_INVALIDO
    assert usuario_inexistente.motivo_codigo == PluginEventoCentral.MOTIVO_CONTEXTO_INVALIDO
    assert segredo.motivo_codigo == PluginEventoCentral.MOTIVO_CONTEXTO_INVALIDO
    assert segredo.correlation_id is None
    assert segredo.para_dict()["correlation_id"] is None
    assert all(item.permitido is False for item in (vazio, sem_canal, sem_capability, segredo))
    texto = inspect.getsource(avaliar_autorizacao_plugin)
    for termo in ("current_user", "flask", "request"):
        assert termo not in texto


def test_resultado_expoe_motivo_seguro(ctx):
    _conta, (user,) = _pessoas("conta-auth-motivo", ("dono@test.com",))
    plugin = _plugin(suporta_titularidade_corporativa=False)
    capability = _capability(plugin)
    conexao = _conexao(user, plugin)
    resultado = avaliar_autorizacao_plugin(_contexto(user, plugin, conexao, capability))
    assert resultado.motivo_codigo in PluginEventoCentral.MOTIVOS_NEGACAO
    assert " " not in resultado.motivo_codigo
    publico = resultado.para_dict()
    assert publico["motivo_codigo"] == resultado.motivo_codigo
    assert set(publico) == {
        "permitido",
        "motivo_codigo",
        "camada",
        "plugin_id",
        "plugin_slug",
        "conexao_id",
        "capability_id",
        "capability_chave",
        "politica_efetiva",
        "canal_origem",
        "correlation_id",
    }


def test_negacao_auditavel_sem_segredo_e_sem_duplicar(ctx):
    _conta, (user,) = _pessoas("conta-auth-auditoria", ("dono@test.com",))
    plugin = _plugin(suporta_titularidade_corporativa=False)
    capability = _capability(plugin)
    conexao = _conexao(user, plugin)
    alterar_estado_conexao(
        conexao.id,
        PluginConexao.ESTADO_CONECTADO,
        usuario_originador_id=user.id,
        diagnostico_codigo="credencial_invalida",
        diagnostico_resumo="Falha temporaria no provider de teste",
    )
    referencia = vincular_referencia_cofre(conexao.id, "ref.cofre.teste")
    resultado = avaliar_autorizacao_plugin(
        _contexto(user, plugin, conexao, capability, correlation_id="corr-auth-1")
    )
    repetido = avaliar_autorizacao_plugin(
        _contexto(user, plugin, conexao, capability, correlation_id="corr-auth-1")
    )
    assert resultado.permitido is False
    assert repetido.motivo_codigo == resultado.motivo_codigo
    evento = _eventos_negacao(conexao.id).one()
    assert evento.user_id == user.id
    assert evento.usuario_originador_id == user.id
    assert evento.plugin_id == plugin.id
    assert evento.conexao_id == conexao.id
    assert evento.capability_id == capability.id
    assert evento.detalhe_codigo == PluginEventoCentral.MOTIVO_CONCESSAO_PROVEDOR_AUSENTE
    assert evento.created_at is not None
    texto = " ".join(str(getattr(evento, coluna.name)) for coluna in evento.__table__.columns)
    for segredo in (referencia.cofre_referencia, "corr-auth-1", "Falha temporaria", "token"):
        assert segredo not in texto
    assert "corr-auth-1" in resultado.para_dict()["correlation_id"]
    assert referencia.cofre_referencia not in str(resultado.para_dict())


def test_negacao_apos_permissao_gera_novo_evento(ctx):
    _conta, (user,) = _pessoas("conta-auth-deny-allow-deny", ("dono@test.com",))
    plugin = _plugin(suporta_titularidade_corporativa=False)
    capability = _capability(plugin)
    conexao = _conexao(user, plugin)
    contexto = _contexto(user, plugin, conexao, capability)
    registrar_concessao_provedor(
        conexao_id=conexao.id,
        capability_id=capability.id,
        resultado=PluginConcessaoProvedor.RESULTADO_NEGADA,
    )
    primeira = avaliar_autorizacao_plugin(contexto)
    assert primeira.permitido is False
    assert _eventos_negacao(conexao.id).count() == 1

    registrar_concessao_provedor(
        conexao_id=conexao.id,
        capability_id=capability.id,
        resultado=PluginConcessaoProvedor.RESULTADO_CONCEDIDA,
    )
    permissao = avaliar_autorizacao_plugin(contexto)
    assert permissao.permitido is True
    assert _eventos_negacao(conexao.id).count() == 1

    registrar_concessao_provedor(
        conexao_id=conexao.id,
        capability_id=capability.id,
        resultado=PluginConcessaoProvedor.RESULTADO_NEGADA,
    )
    segunda = avaliar_autorizacao_plugin(contexto)
    assert segunda.permitido is False
    assert segunda.motivo_codigo == PluginEventoCentral.MOTIVO_CONCESSAO_PROVEDOR_NEGADA
    assert _eventos_negacao(conexao.id).count() == 2


def test_negacao_distinta_por_motivo_capability_ou_originador(ctx):
    _conta, (dono, outro_a, outro_b) = _pessoas(
        "conta-auth-negacao-distinta",
        ("dono@test.com", "outro-a@test.com", "outro-b@test.com"),
    )
    plugin = _plugin(suporta_titularidade_corporativa=False)
    capability_a = _capability(plugin, "leitura.a")
    capability_b = _capability(plugin, "leitura.b")
    conexao = _conexao(dono, plugin)

    avaliar_autorizacao_plugin(_contexto(dono, plugin, conexao, capability_a))
    avaliar_autorizacao_plugin(_contexto(dono, plugin, conexao, capability_b))
    assert _eventos_negacao(conexao.id).count() == 2

    registrar_concessao_provedor(
        conexao_id=conexao.id,
        capability_id=capability_a.id,
        resultado=PluginConcessaoProvedor.RESULTADO_NEGADA,
    )
    motivo_novo = avaliar_autorizacao_plugin(
        _contexto(dono, plugin, conexao, capability_a)
    )
    assert motivo_novo.motivo_codigo == PluginEventoCentral.MOTIVO_CONCESSAO_PROVEDOR_NEGADA
    assert _eventos_negacao(conexao.id).count() == 3

    avaliar_autorizacao_plugin(_contexto(outro_a, plugin, conexao, capability_a))
    avaliar_autorizacao_plugin(_contexto(outro_b, plugin, conexao, capability_a))
    eventos = _eventos_negacao(conexao.id).all()
    assert len(eventos) == 5
    originadores = {
        evento.usuario_originador_id
        for evento in eventos
        if evento.detalhe_codigo == PluginEventoCentral.MOTIVO_CONEXAO_NAO_PERTENCE
    }
    assert originadores == {outro_a.id, outro_b.id}


def test_negacao_repetida_nao_confirma_pendencia_do_chamador(ctx):
    _conta, (user,) = _pessoas("conta-auth-commit-repetido", ("dono@test.com",))
    plugin = _plugin(suporta_titularidade_corporativa=False)
    capability = _capability(plugin)
    conexao = _conexao(user, plugin)
    contexto = _contexto(user, plugin, conexao, capability)
    primeira = avaliar_autorizacao_plugin(contexto, commit=True)
    assert primeira.permitido is False
    assert _eventos_negacao(conexao.id).count() == 1
    marcador = registrar_plugin(
        slug="marcador-negacao-repetida",
        nome="Marcador pendente",
        adapter_key="adapter.marcador_repetido",
        suporta_titularidade_pessoal=True,
        suporta_titularidade_corporativa=False,
        commit=False,
    )
    repetida = avaliar_autorizacao_plugin(contexto, commit=True)
    assert repetida.motivo_codigo == primeira.motivo_codigo
    assert _eventos_negacao(conexao.id).count() == 1
    assert db.session.query(Plugin).filter_by(slug=marcador.slug).count() == 1
    db.session.rollback()
    assert db.session.query(Plugin).filter_by(slug=marcador.slug).count() == 0
    assert _eventos_negacao(conexao.id).count() == 1


def test_restricao_de_outro_usuario_nao_libera(ctx):
    _conta, (dono, outro) = _pessoas(
        "conta-auth-restricao-alheia",
        ("dono@test.com", "outro@test.com"),
    )
    plugin = _plugin(suporta_titularidade_corporativa=False)
    capability = _capability(plugin)
    conexao = _conexao(dono, plugin)
    agora = utcnow_naive()
    db.session.add(
        PluginRestricaoUsuario(
            conexao_id=conexao.id,
            capability_id=capability.id,
            user_id=outro.id,
            efeito=PluginRestricaoUsuario.EFEITO_BLOQUEADA_PELO_USUARIO,
            created_at=agora,
            updated_at=agora,
        )
    )
    db.session.commit()
    registrar_concessao_provedor(
        conexao_id=conexao.id,
        capability_id=capability.id,
        resultado=PluginConcessaoProvedor.RESULTADO_CONCEDIDA,
    )
    resultado = avaliar_autorizacao_plugin(_contexto(dono, plugin, conexao, capability))
    assert resultado.permitido is False
    assert resultado.motivo_codigo == PluginEventoCentral.MOTIVO_CONTEXTO_INVALIDO


def test_falha_na_auditoria_nao_quebra_sessao_nem_persiste_evento(ctx):
    _conta, (user,) = _pessoas("conta-auth-atomica", ("dono@test.com",))
    plugin = _plugin(suporta_titularidade_corporativa=False)
    capability = _capability(plugin)
    conexao = _conexao(user, plugin)
    marcador = registrar_plugin(
        slug="marcador-auth-atomico",
        nome="Marcador pendente",
        adapter_key="adapter.marcador_atomico",
        suporta_titularidade_pessoal=True,
        suporta_titularidade_corporativa=False,
        commit=False,
    )

    def _falhar_evento(mapper, connection, target):
        if target.tipo_evento == PluginEventoCentral.TIPO_AUTORIZACAO_NEGADA:
            raise RuntimeError("falha induzida na negacao")

    sa_event.listen(PluginEventoCentral, "before_insert", _falhar_evento)
    try:
        with pytest.raises(RuntimeError, match="falha induzida na negacao"):
            avaliar_autorizacao_plugin(_contexto(user, plugin, conexao, capability))
        assert db.session.is_active
        assert _eventos_negacao(conexao.id).count() == 0
        assert db.session.query(Plugin).filter_by(slug=marcador.slug).count() == 1
    finally:
        sa_event.remove(PluginEventoCentral, "before_insert", _falhar_evento)

    resultado = avaliar_autorizacao_plugin(_contexto(user, plugin, conexao, capability))
    assert resultado.permitido is False
    assert _eventos_negacao(conexao.id).count() == 1
    db.session.rollback()
    assert db.session.query(Plugin).filter_by(slug=marcador.slug).count() == 0
    assert _eventos_negacao(conexao.id).count() == 0


def test_precedencia_logcompleta_bloqueia_com_provider_concedido(ctx):
    _conta, (user,) = _pessoas("conta-auth-prec-plataforma", ("dono@test.com",))
    plugin = _plugin(suporta_titularidade_corporativa=False)
    capability = _capability(plugin, politica=PluginCapability.POLITICA_NEGADA)
    conexao = _conexao(user, plugin)
    registrar_concessao_provedor(
        conexao_id=conexao.id,
        capability_id=capability.id,
        resultado=PluginConcessaoProvedor.RESULTADO_CONCEDIDA,
    )
    resultado = avaliar_autorizacao_plugin(_contexto(user, plugin, conexao, capability))
    assert PluginRestricaoUsuario.query.filter_by(conexao_id=conexao.id).count() == 0
    assert resultado.permitido is False
    assert resultado.motivo_codigo == PluginEventoCentral.MOTIVO_BLOQUEADA_PELA_PLATAFORMA


def test_precedencia_usuario_bloqueia_com_provider_concedido(ctx):
    _conta, (user,) = _pessoas("conta-auth-prec-usuario", ("dono@test.com",))
    plugin = _plugin(suporta_titularidade_corporativa=False)
    capability = _capability(plugin)
    conexao = _conexao(user, plugin)
    registrar_restricao_usuario(
        conexao_id=conexao.id,
        capability_id=capability.id,
        user_id=user.id,
        usuario_originador_id=user.id,
    )
    registrar_concessao_provedor(
        conexao_id=conexao.id,
        capability_id=capability.id,
        resultado=PluginConcessaoProvedor.RESULTADO_CONCEDIDA,
    )
    resultado = avaliar_autorizacao_plugin(_contexto(user, plugin, conexao, capability))
    assert resultado.permitido is False
    assert resultado.motivo_codigo == PluginEventoCentral.MOTIVO_BLOQUEADA_PELO_USUARIO


def test_precedencia_sem_concessao_nega(ctx):
    _conta, (user,) = _pessoas("conta-auth-prec-ausente", ("dono@test.com",))
    plugin = _plugin(suporta_titularidade_corporativa=False)
    capability = _capability(plugin)
    conexao = _conexao(user, plugin)
    resultado = avaliar_autorizacao_plugin(_contexto(user, plugin, conexao, capability))
    assert resultado.politica_efetiva.politica_maxima == PluginCapability.POLITICA_PERMITIDA
    assert resultado.politica_efetiva.restricao_usuario == "ausente"
    assert resultado.politica_efetiva.concessao_provedor == "ausente"
    assert resultado.permitido is False
    assert resultado.motivo_codigo == PluginEventoCentral.MOTIVO_CONCESSAO_PROVEDOR_AUSENTE


def test_precedencia_tres_camadas_permitem(ctx):
    _conta, (user,) = _pessoas("conta-auth-prec-allow", ("dono@test.com",))
    plugin = _plugin(suporta_titularidade_corporativa=False)
    capability = _capability(plugin)
    conexao = _conexao(user, plugin)
    registrar_concessao_provedor(
        conexao_id=conexao.id,
        capability_id=capability.id,
        resultado=PluginConcessaoProvedor.RESULTADO_CONCEDIDA,
    )
    resultado = avaliar_autorizacao_plugin(
        _contexto(user, plugin, conexao, capability, canal_origem="job")
    )
    assert resultado.permitido is True
    assert resultado.motivo_codigo == PluginEventoCentral.MOTIVO_AUTORIZADA
    assert resultado.canal_origem == "job"
    assert PluginConexao.query.filter_by(id=conexao.id).one().estado == (
        PluginConexao.ESTADO_CONECTADO
    )
