"""Central de Plugins — domínio e persistência (SCRUM-222, lote 1)."""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

import app.services.central_plugin_service as central_plugin_service
from app.extensions import db
from app.models import (
    Plugin,
    PluginCapability,
    PluginConcessaoProvedor,
    PluginConexao,
    PluginCredencialReferencia,
    PluginEventoCentral,
    PluginRestricaoUsuario,
    utcnow_naive,
)
from app.services.central_plugin_service import (
    CapabilityChaveDuplicadaError,
    ConexaoAtivaDuplicadaError,
    MaterialSensivelRecusadoError,
    PluginSlugDuplicadoError,
    RestricaoUsuarioInvalidaError,
    TitularidadeNaoPermitidaError,
    alterar_estado_conexao,
    criar_conexao,
    desconectar_conexao,
    listar_capabilities,
    listar_conexoes_do_usuario,
    listar_conexoes_para_revogacao,
    listar_plugins,
    marcar_credencial_invalida,
    obter_concessao_provedor,
    obter_conexao_usuario,
    obter_restricao_usuario,
    registrar_capability,
    registrar_plugin,
    registrar_restricao_usuario,
    remover_restricao_usuario,
    representacao_publica_conexao,
    substituir_conexao,
    vincular_referencia_cofre,
)
from tests.conftest import seed_conta_franquia_cliente, seed_usuario

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "i0j1k2l3m4n5_central_plugins_dominio.py"
)
_PROIBIDOS_NO_FIXTURE = ("whatsapp", "brobot", "google", "xc")
_COLUNAS_SECRETAS = (
    "token",
    "secret",
    "api_key",
    "apikey",
    "password",
    "senha",
    "access_token",
    "refresh_token",
)


@pytest.fixture(autouse=True)
def _fk_sqlite_deste_modulo(ctx):
    """Liga FKs só na conexão destes testes. O restante da suíte permanece como está."""

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


def _usuario(email: str, slug: str):
    conta, franquia = seed_conta_franquia_cliente(slug=slug)
    return seed_usuario(franquia.id, conta.id, email=email), conta


def _plugin_teste(**kwargs):
    dados = dict(
        slug="teste-interno-central",
        nome="Plugin tecnico ficticio de teste",
        descricao="Fixture interno da Central. Nao e integracao operacional.",
        adapter_key="adapter.teste_interno",
        suporta_titularidade_pessoal=True,
        suporta_titularidade_corporativa=True,
        status=Plugin.STATUS_DISPONIVEL,
    )
    dados.update(kwargs)
    return registrar_plugin(**dados)


def _capability(plugin, chave="leitura.teste", natureza=PluginCapability.NATUREZA_PASSIVA):
    return registrar_capability(
        plugin_id=plugin.id,
        chave=chave,
        nome=f"Capability ficticia {chave}",
        descricao="Capability de teste. Nao representa provider real.",
        politica_maxima=PluginCapability.POLITICA_PERMITIDA,
        natureza=natureza,
    )


def test_criacao_de_plugin(ctx):
    plugin = _plugin_teste(status=Plugin.STATUS_RASCUNHO)
    assert plugin.id is not None
    assert plugin.slug == "teste-interno-central"
    assert plugin.status == Plugin.STATUS_RASCUNHO
    assert plugin.suporta_titularidade_pessoal is True
    assert plugin.suporta_titularidade_corporativa is True
    listados = listar_plugins()
    assert [item.id for item in listados] == [plugin.id]
    blob = " ".join(
        [
            plugin.slug,
            plugin.nome,
            plugin.descricao or "",
            plugin.adapter_key,
        ]
    ).casefold()
    for termo in _PROIBIDOS_NO_FIXTURE:
        assert termo not in blob


def test_slug_e_unico(ctx):
    _plugin_teste()
    with pytest.raises(PluginSlugDuplicadoError):
        _plugin_teste(nome="Outro nome ficticio")


def test_criacao_de_capabilities(ctx):
    plugin = _plugin_teste()
    leitura = _capability(plugin, "leitura.teste", PluginCapability.NATUREZA_PASSIVA)
    acao = _capability(plugin, "acao.teste", PluginCapability.NATUREZA_ATIVA)
    chaves = [item.chave for item in listar_capabilities(plugin.id)]
    assert chaves == ["acao.teste", "leitura.teste"]
    assert leitura.politica_maxima == PluginCapability.POLITICA_PERMITIDA
    assert acao.natureza == PluginCapability.NATUREZA_ATIVA
    with pytest.raises(CapabilityChaveDuplicadaError):
        _capability(plugin, "leitura.teste")


def test_conexao_pessoal(ctx):
    user, _conta = _usuario("pessoal@test.com", "conta-plugin-pessoal")
    plugin = _plugin_teste(suporta_titularidade_corporativa=False)
    conexao = criar_conexao(
        plugin_id=plugin.id,
        user_id=user.id,
        titularidade=PluginConexao.TITULARIDADE_PESSOAL,
        usuario_originador_id=user.id,
        identificador_externo="ext-teste-1",
    )
    assert conexao.conta_id is None
    assert conexao.titularidade == PluginConexao.TITULARIDADE_PESSOAL
    assert conexao.estado == PluginConexao.ESTADO_AGUARDANDO_CONFIGURACAO
    assert conexao.usuario_originador_id == user.id
    assert "franquia_id" not in PluginConexao.__table__.columns
    assert obter_conexao_usuario(user.id, plugin.id).id == conexao.id
    evento = PluginEventoCentral.query.filter_by(conexao_id=conexao.id).one()
    assert evento.tipo_evento == PluginEventoCentral.TIPO_CONEXAO_CRIADA
    referencia = PluginCredencialReferencia.query.filter_by(conexao_id=conexao.id).one()
    assert referencia.estado == PluginCredencialReferencia.ESTADO_NAO_PROVISIONADA
    assert referencia.cofre_referencia is None


def test_conexao_corporativa_distingue_proprietario_e_originador(ctx):
    titular, conta = _usuario("titular@test.com", "conta-plugin-corp")
    originador, _outra = _usuario("originador@test.com", "conta-plugin-originador")
    plugin = _plugin_teste()
    conexao = criar_conexao(
        plugin_id=plugin.id,
        user_id=titular.id,
        titularidade=PluginConexao.TITULARIDADE_CORPORATIVA,
        usuario_originador_id=originador.id,
        conta_id=conta.id,
    )
    assert conexao.conta_id == conta.id
    assert conexao.user_id == titular.id
    assert conexao.usuario_originador_id == originador.id
    assert conexao.user_id != conexao.usuario_originador_id
    assert conexao.titularidade == PluginConexao.TITULARIDADE_CORPORATIVA
    alterar_estado_conexao(
        conexao.id,
        PluginConexao.ESTADO_CONECTADO,
        usuario_originador_id=titular.id,
    )
    recarregada = db.session.get(PluginConexao, conexao.id)
    assert recarregada.usuario_originador_id == originador.id
    evento_estado = PluginEventoCentral.query.filter_by(
        conexao_id=conexao.id,
        tipo_evento=PluginEventoCentral.TIPO_CONEXAO_ESTADO_ALTERADO,
    ).one()
    assert evento_estado.usuario_originador_id == titular.id


def test_rejeita_titularidade_nao_permitida(ctx):
    user, conta = _usuario("titularidade@test.com", "conta-plugin-titularidade")
    so_pessoal = _plugin_teste(
        slug="teste-so-pessoal",
        adapter_key="adapter.teste_so_pessoal",
        suporta_titularidade_pessoal=True,
        suporta_titularidade_corporativa=False,
    )
    with pytest.raises(TitularidadeNaoPermitidaError):
        criar_conexao(
            plugin_id=so_pessoal.id,
            user_id=user.id,
            titularidade=PluginConexao.TITULARIDADE_CORPORATIVA,
            usuario_originador_id=user.id,
            conta_id=conta.id,
        )
    so_corporativa = _plugin_teste(
        slug="teste-so-corporativa",
        adapter_key="adapter.teste_so_corporativa",
        suporta_titularidade_pessoal=False,
        suporta_titularidade_corporativa=True,
    )
    with pytest.raises(TitularidadeNaoPermitidaError):
        criar_conexao(
            plugin_id=so_corporativa.id,
            user_id=user.id,
            titularidade=PluginConexao.TITULARIDADE_PESSOAL,
            usuario_originador_id=user.id,
        )
    assert PluginConexao.query.count() == 0


def test_uma_conexao_ativa_por_usuario_e_plugin_e_garantida_no_banco(ctx):
    user, _conta = _usuario("slot@test.com", "conta-plugin-slot")
    plugin = _plugin_teste(suporta_titularidade_corporativa=False)
    primeira = criar_conexao(
        plugin_id=plugin.id,
        user_id=user.id,
        titularidade=PluginConexao.TITULARIDADE_PESSOAL,
        usuario_originador_id=user.id,
    )
    with pytest.raises(ConexaoAtivaDuplicadaError):
        criar_conexao(
            plugin_id=plugin.id,
            user_id=user.id,
            titularidade=PluginConexao.TITULARIDADE_PESSOAL,
            usuario_originador_id=user.id,
        )
    agora = utcnow_naive()
    db.session.add(
        PluginConexao(
            plugin_id=plugin.id,
            user_id=user.id,
            conta_id=None,
            usuario_originador_id=user.id,
            titularidade=PluginConexao.TITULARIDADE_PESSOAL,
            estado=PluginConexao.ESTADO_CONECTADO,
            created_at=agora,
            updated_at=agora,
            estado_alterado_em=agora,
        )
    )
    with pytest.raises(IntegrityError):
        db.session.flush()
    db.session.rollback()

    desconectar_conexao(primeira.id, usuario_originador_id=user.id)
    segunda = criar_conexao(
        plugin_id=plugin.id,
        user_id=user.id,
        titularidade=PluginConexao.TITULARIDADE_PESSOAL,
        usuario_originador_id=user.id,
    )
    assert segunda.id != primeira.id
    assert obter_conexao_usuario(user.id, plugin.id).id == segunda.id
    assert PluginConexao.query.filter_by(user_id=user.id, plugin_id=plugin.id).count() == 2
    assert "desconectado" not in PluginConexao._SQL_OCUPA_SLOT
    migration = _MIGRATION.read_text(encoding="utf-8")
    assert PluginConexao._SQL_OCUPA_SLOT in migration


def test_outro_usuario_tem_sua_conexao_do_mesmo_plugin(ctx):
    um, _c1 = _usuario("um@test.com", "conta-plugin-um")
    outro, _c2 = _usuario("outro@test.com", "conta-plugin-outro")
    plugin = _plugin_teste(suporta_titularidade_corporativa=False)
    conexao_um = criar_conexao(
        plugin_id=plugin.id,
        user_id=um.id,
        titularidade=PluginConexao.TITULARIDADE_PESSOAL,
        usuario_originador_id=um.id,
    )
    conexao_outro = criar_conexao(
        plugin_id=plugin.id,
        user_id=outro.id,
        titularidade=PluginConexao.TITULARIDADE_PESSOAL,
        usuario_originador_id=outro.id,
    )
    assert conexao_um.id != conexao_outro.id
    assert obter_conexao_usuario(um.id, plugin.id).id == conexao_um.id
    assert obter_conexao_usuario(outro.id, plugin.id).id == conexao_outro.id


def test_requer_atencao_nao_exclui_conexao_e_credencial_invalida_tambem_nao(ctx):
    user, _conta = _usuario("atencao@test.com", "conta-plugin-atencao")
    plugin = _plugin_teste(suporta_titularidade_corporativa=False)
    conexao = criar_conexao(
        plugin_id=plugin.id,
        user_id=user.id,
        titularidade=PluginConexao.TITULARIDADE_PESSOAL,
        usuario_originador_id=user.id,
    )
    conexao_id = conexao.id
    total = PluginConexao.query.count()
    alterar_estado_conexao(
        conexao_id,
        PluginConexao.ESTADO_REQUER_ATENCAO,
        usuario_originador_id=user.id,
        diagnostico_codigo="provider.indisponivel",
        diagnostico_resumo="Falha temporaria no provider de teste",
    )
    preservada = db.session.get(PluginConexao, conexao_id)
    assert preservada is not None
    assert PluginConexao.query.count() == total
    assert preservada.estado == PluginConexao.ESTADO_REQUER_ATENCAO
    assert preservada.desconectado_em is None

    alterar_estado_conexao(
        conexao_id,
        PluginConexao.ESTADO_CONECTADO,
        usuario_originador_id=user.id,
    )
    marcar_credencial_invalida(conexao_id)
    preservada = db.session.get(PluginConexao, conexao_id)
    referencia = PluginCredencialReferencia.query.filter_by(conexao_id=conexao_id).one()
    assert preservada.estado == PluginConexao.ESTADO_CONECTADO
    assert referencia.estado == PluginCredencialReferencia.ESTADO_INVALIDA
    assert PluginConexao.query.filter_by(id=conexao_id).count() == 1


def test_bloqueado_desabilitado_e_desconectado_permanecem_distintos(ctx):
    user, _conta = _usuario("estados@test.com", "conta-plugin-estados")
    estados = (
        PluginConexao.ESTADO_BLOQUEADO,
        PluginConexao.ESTADO_DESABILITADO,
        PluginConexao.ESTADO_DESCONECTADO,
    )
    conexoes = []
    for indice, estado in enumerate(estados):
        plugin = _plugin_teste(
            slug=f"teste-estado-{indice}",
            adapter_key=f"adapter.teste_estado_{indice}",
            suporta_titularidade_corporativa=False,
        )
        conexao = criar_conexao(
            plugin_id=plugin.id,
            user_id=user.id,
            titularidade=PluginConexao.TITULARIDADE_PESSOAL,
            usuario_originador_id=user.id,
        )
        alterar_estado_conexao(conexao.id, estado, usuario_originador_id=user.id)
        conexoes.append(db.session.get(PluginConexao, conexao.id))
    obtidos = [item.estado for item in conexoes]
    assert obtidos == list(estados)
    assert len(set(obtidos)) == 3
    assert all(item is not None for item in conexoes)
    tipos = {
        PluginConexao.ESTADO_BLOQUEADO: PluginEventoCentral.TIPO_BLOQUEADO,
        PluginConexao.ESTADO_DESABILITADO: PluginEventoCentral.TIPO_DESABILITADO,
        PluginConexao.ESTADO_DESCONECTADO: PluginEventoCentral.TIPO_DESCONECTADO,
    }
    for conexao in conexoes:
        evento = (
            PluginEventoCentral.query.filter_by(conexao_id=conexao.id, estado_novo=conexao.estado)
            .one()
        )
        assert evento.tipo_evento == tipos[conexao.estado]


def test_restricao_vinculada_ao_usuario_e_a_conexao(ctx):
    user, _conta = _usuario("restricao@test.com", "conta-plugin-restricao")
    plugin = _plugin_teste(suporta_titularidade_corporativa=False)
    capability = _capability(plugin)
    conexao = criar_conexao(
        plugin_id=plugin.id,
        user_id=user.id,
        titularidade=PluginConexao.TITULARIDADE_PESSOAL,
        usuario_originador_id=user.id,
    )
    assert obter_restricao_usuario(conexao.id, capability.id) is None
    assert obter_concessao_provedor(conexao.id, capability.id) is None
    restricao = registrar_restricao_usuario(
        conexao_id=conexao.id,
        capability_id=capability.id,
        user_id=user.id,
        usuario_originador_id=user.id,
    )
    assert restricao.user_id == user.id
    assert restricao.conexao_id == conexao.id
    assert restricao.capability_id == capability.id
    assert restricao.efeito == PluginRestricaoUsuario.EFEITO_BLOQUEADA_PELO_USUARIO
    remover_restricao_usuario(
        conexao_id=conexao.id,
        capability_id=capability.id,
        user_id=user.id,
        usuario_originador_id=user.id,
    )
    assert obter_restricao_usuario(conexao.id, capability.id) is None
    assert PluginConcessaoProvedor.query.count() == 0


def test_isolamento_de_restricao_entre_usuarios(ctx):
    dono, _c1 = _usuario("dono@test.com", "conta-plugin-dono")
    outro, _c2 = _usuario("alheio@test.com", "conta-plugin-alheio")
    plugin = _plugin_teste(suporta_titularidade_corporativa=False)
    capability = _capability(plugin)
    conexao_dono = criar_conexao(
        plugin_id=plugin.id,
        user_id=dono.id,
        titularidade=PluginConexao.TITULARIDADE_PESSOAL,
        usuario_originador_id=dono.id,
    )
    conexao_outro = criar_conexao(
        plugin_id=plugin.id,
        user_id=outro.id,
        titularidade=PluginConexao.TITULARIDADE_PESSOAL,
        usuario_originador_id=outro.id,
    )
    registrar_restricao_usuario(
        conexao_id=conexao_dono.id,
        capability_id=capability.id,
        user_id=dono.id,
        usuario_originador_id=dono.id,
    )
    with pytest.raises(RestricaoUsuarioInvalidaError):
        registrar_restricao_usuario(
            conexao_id=conexao_dono.id,
            capability_id=capability.id,
            user_id=outro.id,
            usuario_originador_id=outro.id,
        )
    assert obter_restricao_usuario(conexao_outro.id, capability.id) is None
    assert (
        PluginRestricaoUsuario.query.filter_by(user_id=outro.id).count() == 0
    )
    assert PluginRestricaoUsuario.query.filter_by(user_id=dono.id).count() == 1
    assert obter_conexao_usuario(outro.id, plugin.id).id == conexao_outro.id
    assert obter_conexao_usuario(dono.id, plugin.id).id == conexao_dono.id


def test_representacao_publica_nao_serializa_credencial(ctx):
    user, _conta = _usuario("publico@test.com", "conta-plugin-publico")
    plugin = _plugin_teste(suporta_titularidade_corporativa=False)
    conexao = criar_conexao(
        plugin_id=plugin.id,
        user_id=user.id,
        titularidade=PluginConexao.TITULARIDADE_PESSOAL,
        usuario_originador_id=user.id,
    )
    referencia = vincular_referencia_cofre(conexao.id, "cofre.ref.teste1")
    publica = representacao_publica_conexao(conexao)
    serializado = json.dumps(publica)
    assert referencia.cofre_referencia not in serializado
    for chave in publica:
        for termo in _COLUNAS_SECRETAS + ("cofre", "credencial"):
            assert termo not in chave
    for modelo in (PluginConexao, PluginCredencialReferencia, PluginEventoCentral):
        for coluna in modelo.__table__.columns:
            for termo in _COLUNAS_SECRETAS:
                assert termo not in coluna.name
    with pytest.raises(MaterialSensivelRecusadoError):
        vincular_referencia_cofre(conexao.id, "eyJhbGciOiJIUzI1NiJ9.segredo")
    with pytest.raises(MaterialSensivelRecusadoError):
        alterar_estado_conexao(
            conexao.id,
            PluginConexao.ESTADO_REQUER_ATENCAO,
            usuario_originador_id=user.id,
            diagnostico_resumo="Bearer ya29.segredo-de-teste",
        )
    assert db.session.get(PluginConexao, conexao.id).estado == PluginConexao.ESTADO_AGUARDANDO_CONFIGURACAO


def test_conexao_localizavel_por_user_id_para_revogacao(ctx):
    user, _conta = _usuario("revogacao@test.com", "conta-plugin-revogacao")
    outro, _c2 = _usuario("fora@test.com", "conta-plugin-fora")
    plugin_a = _plugin_teste(
        slug="teste-revogacao-a",
        adapter_key="adapter.teste_revogacao_a",
        suporta_titularidade_corporativa=False,
    )
    plugin_b = _plugin_teste(
        slug="teste-revogacao-b",
        adapter_key="adapter.teste_revogacao_b",
        suporta_titularidade_corporativa=False,
    )
    conexao_a = criar_conexao(
        plugin_id=plugin_a.id,
        user_id=user.id,
        titularidade=PluginConexao.TITULARIDADE_PESSOAL,
        usuario_originador_id=user.id,
    )
    criar_conexao(
        plugin_id=plugin_b.id,
        user_id=user.id,
        titularidade=PluginConexao.TITULARIDADE_PESSOAL,
        usuario_originador_id=user.id,
    )
    criar_conexao(
        plugin_id=plugin_a.id,
        user_id=outro.id,
        titularidade=PluginConexao.TITULARIDADE_PESSOAL,
        usuario_originador_id=outro.id,
    )
    desconectar_conexao(conexao_a.id, usuario_originador_id=user.id)
    por_usuario = listar_conexoes_do_usuario(user.id)
    para_revogar = listar_conexoes_para_revogacao(user.id)
    assert {item.user_id for item in por_usuario} == {user.id}
    assert len(por_usuario) == 2
    assert [item.plugin_id for item in para_revogar] == [plugin_b.id]
    assert PluginConexao.query.filter_by(user_id=user.id).count() == 2
    nomes = {indice["name"] for indice in sa_inspect(db.engine).get_indexes("plugin_conexao")}
    assert "ix_plugin_conexao_user_estado" in nomes
    assert "uq_plugin_conexao_user_plugin_ativa" in nomes
    assert PluginEventoCentral.TIPO_REVOGADO in PluginEventoCentral.TIPOS
    assert all(item.revogado_em is None for item in para_revogar)
    assert (
        PluginEventoCentral.query.filter_by(
            tipo_evento=PluginEventoCentral.TIPO_REVOGADO
        ).count()
        == 0
    )


def test_substituir_desconecta_a_anterior_e_abre_outra(ctx):
    user, conta = _usuario("troca@test.com", "conta-plugin-troca")
    plugin = _plugin_teste()
    atual = criar_conexao(
        plugin_id=plugin.id,
        user_id=user.id,
        titularidade=PluginConexao.TITULARIDADE_PESSOAL,
        usuario_originador_id=user.id,
    )
    nova = substituir_conexao(
        atual.id,
        usuario_originador_id=user.id,
        titularidade=PluginConexao.TITULARIDADE_CORPORATIVA,
        conta_id=conta.id,
    )
    anterior = db.session.get(PluginConexao, atual.id)
    assert anterior.estado == PluginConexao.ESTADO_DESCONECTADO
    assert nova.estado == PluginConexao.ESTADO_AGUARDANDO_CONFIGURACAO
    assert nova.titularidade == PluginConexao.TITULARIDADE_CORPORATIVA
    assert nova.conta_id == conta.id
    assert obter_conexao_usuario(user.id, plugin.id).id == nova.id


def test_falha_no_evento_nao_deixa_conexao_confirmavel(ctx):
    user, _conta = _usuario("atomica@test.com", "conta-plugin-atomica")
    plugin = _plugin_teste(suporta_titularidade_corporativa=False)

    def _falhar_evento(mapper, connection, target):
        raise RuntimeError("falha induzida na criacao do evento")

    sa_event.listen(PluginEventoCentral, "before_insert", _falhar_evento)
    try:
        with pytest.raises(RuntimeError, match="falha induzida na criacao do evento"):
            criar_conexao(
                plugin_id=plugin.id,
                user_id=user.id,
                titularidade=PluginConexao.TITULARIDADE_PESSOAL,
                usuario_originador_id=user.id,
            )
        assert db.session.is_active
        db.session.commit()
        assert PluginConexao.query.filter_by(user_id=user.id, plugin_id=plugin.id).count() == 0
        assert PluginCredencialReferencia.query.count() == 0
        assert PluginEventoCentral.query.count() == 0
        assert db.session.get(Plugin, plugin.id) is not None
    finally:
        sa_event.remove(PluginEventoCentral, "before_insert", _falhar_evento)

    criada = criar_conexao(
        plugin_id=plugin.id,
        user_id=user.id,
        titularidade=PluginConexao.TITULARIDADE_PESSOAL,
        usuario_originador_id=user.id,
    )
    assert PluginCredencialReferencia.query.filter_by(conexao_id=criada.id).count() == 1
    assert (
        PluginEventoCentral.query.filter_by(
            conexao_id=criada.id,
            tipo_evento=PluginEventoCentral.TIPO_CONEXAO_CRIADA,
        ).count()
        == 1
    )


def test_slug_duplicado_mantem_sessao_utilizavel(ctx):
    original = _plugin_teste(slug="slug-original", nome="Nome original")
    marcador = registrar_plugin(
        slug="slug-marcador",
        nome="Marcador pendente",
        adapter_key="adapter.marcador_pendente",
        suporta_titularidade_pessoal=True,
        suporta_titularidade_corporativa=False,
        commit=False,
    )
    with pytest.raises(PluginSlugDuplicadoError):
        registrar_plugin(
            slug="slug-original",
            nome="Nao deve persistir",
            adapter_key="adapter.nao_persistir",
            suporta_titularidade_pessoal=True,
            suporta_titularidade_corporativa=False,
            commit=False,
        )
    assert db.session.is_active
    por_slug = {item.slug: item.nome for item in listar_plugins()}
    assert por_slug["slug-original"] == "Nome original"
    assert por_slug["slug-marcador"] == "Marcador pendente"
    db.session.commit()
    assert Plugin.query.filter_by(slug="slug-original").one().id == original.id
    assert Plugin.query.filter_by(slug="slug-original").count() == 1
    assert db.session.get(Plugin, marcador.id).nome == "Marcador pendente"


def test_conexao_duplicada_mantem_sessao_utilizavel(ctx, monkeypatch):
    user, _conta = _usuario("duplicada@test.com", "conta-plugin-duplicada")
    plugin = _plugin_teste(suporta_titularidade_corporativa=False)
    existente = criar_conexao(
        plugin_id=plugin.id,
        user_id=user.id,
        titularidade=PluginConexao.TITULARIDADE_PESSOAL,
        usuario_originador_id=user.id,
    )
    eventos = PluginEventoCentral.query.filter_by(conexao_id=existente.id).count()
    referencias = PluginCredencialReferencia.query.filter_by(conexao_id=existente.id).count()
    monkeypatch.setattr(
        central_plugin_service,
        "obter_conexao_usuario",
        lambda *args, **kwargs: None,
    )
    with pytest.raises(ConexaoAtivaDuplicadaError):
        criar_conexao(
            plugin_id=plugin.id,
            user_id=user.id,
            titularidade=PluginConexao.TITULARIDADE_PESSOAL,
            usuario_originador_id=user.id,
        )
    assert db.session.is_active
    assert Plugin.query.filter_by(id=plugin.id).one().slug == plugin.slug
    assert PluginConexao.query.filter_by(user_id=user.id, plugin_id=plugin.id).count() == 1
    preservada = db.session.get(PluginConexao, existente.id)
    assert preservada.estado == PluginConexao.ESTADO_AGUARDANDO_CONFIGURACAO
    assert PluginEventoCentral.query.filter_by(conexao_id=existente.id).count() == eventos
    assert PluginCredencialReferencia.query.filter_by(conexao_id=existente.id).count() == referencias
    db.session.commit()
    assert PluginConexao.query.filter_by(user_id=user.id, plugin_id=plugin.id).count() == 1


def test_diagnostico_rejeita_payload_e_limita_codigo(ctx):
    user, _conta = _usuario("diagnostico@test.com", "conta-plugin-diagnostico")
    plugin = _plugin_teste(suporta_titularidade_corporativa=False)
    conexao = criar_conexao(
        plugin_id=plugin.id,
        user_id=user.id,
        titularidade=PluginConexao.TITULARIDADE_PESSOAL,
        usuario_originador_id=user.id,
    )
    assert PluginConexao.diagnostico_codigo.type.length == 80
    recusados = (
        {"diagnostico_codigo": "sk-opaque-demo-value"},
        {"diagnostico_codigo": "token=opaque-demo-value"},
        {"diagnostico_codigo": '{"token":"opaque-demo-value"}'},
        {"diagnostico_codigo": "a" * 81},
        {"diagnostico_resumo": "sk-opaque-demo-value"},
        {"diagnostico_resumo": "token=opaque-demo-value"},
        {"diagnostico_resumo": '{"token":"opaque-demo-value"}'},
    )
    for campos in recusados:
        with pytest.raises(MaterialSensivelRecusadoError):
            alterar_estado_conexao(
                conexao.id,
                PluginConexao.ESTADO_REQUER_ATENCAO,
                usuario_originador_id=user.id,
                **campos,
            )
    preservada = db.session.get(PluginConexao, conexao.id)
    assert preservada.estado == PluginConexao.ESTADO_AGUARDANDO_CONFIGURACAO
    assert preservada.diagnostico_codigo is None
    assert preservada.diagnostico_resumo is None
    alterar_estado_conexao(
        conexao.id,
        PluginConexao.ESTADO_REQUER_ATENCAO,
        usuario_originador_id=user.id,
        diagnostico_codigo="credencial_invalida",
        diagnostico_resumo="Falha temporaria no provider de teste",
    )
    aceita = db.session.get(PluginConexao, conexao.id)
    assert aceita.diagnostico_codigo == "credencial_invalida"
    assert aceita.diagnostico_resumo == "Falha temporaria no provider de teste"
    assert len("a" * 80) == 80
    alterar_estado_conexao(
        conexao.id,
        PluginConexao.ESTADO_CONECTADO,
        usuario_originador_id=user.id,
        diagnostico_codigo="a" * 80,
    )
    assert db.session.get(PluginConexao, conexao.id).diagnostico_codigo == "a" * 80


def test_titularidade_do_plugin_e_regra_de_servico(ctx):
    user, _conta = _usuario("direto@test.com", "conta-plugin-direto")
    plugin = _plugin_teste(
        slug="teste-so-corporativa-banco",
        adapter_key="adapter.teste_so_corporativa_banco",
        suporta_titularidade_pessoal=False,
        suporta_titularidade_corporativa=True,
    )
    with pytest.raises(TitularidadeNaoPermitidaError):
        criar_conexao(
            plugin_id=plugin.id,
            user_id=user.id,
            titularidade=PluginConexao.TITULARIDADE_PESSOAL,
            usuario_originador_id=user.id,
        )
    checks = " ".join(
        item.get("sqltext") or ""
        for item in sa_inspect(db.engine).get_check_constraints("plugin_conexao")
    )
    assert "conta_id IS NULL" in checks
    assert "suporta_titularidade" not in checks
    agora = utcnow_naive()
    direta = PluginConexao(
        plugin_id=plugin.id,
        user_id=user.id,
        conta_id=None,
        usuario_originador_id=user.id,
        titularidade=PluginConexao.TITULARIDADE_PESSOAL,
        estado=PluginConexao.ESTADO_AGUARDANDO_CONFIGURACAO,
        created_at=agora,
        updated_at=agora,
        estado_alterado_em=agora,
    )
    db.session.add(direta)
    db.session.flush()
    assert direta.id is not None
    db.session.rollback()
    assert PluginConexao.query.count() == 0


def test_evento_append_only_pelo_dominio(ctx):
    texto_modelo = inspect.getsource(PluginEventoCentral)
    assert "append-only" in texto_modelo
    assert "domínio" in texto_modelo
    assert "banco" in texto_modelo
    texto_servico = Path(central_plugin_service.__file__).read_text(encoding="utf-8")
    assert "db.session.delete(evento" not in texto_servico
    assert "update(PluginEventoCentral" not in texto_servico
    assert "TRIGGER" not in _MIGRATION.read_text(encoding="utf-8").upper()


def test_fk_rejeita_referencias_inexistentes(ctx):
    assert db.session.execute(text("PRAGMA foreign_keys")).scalar() == 1
    user, _conta = _usuario("fk@test.com", "conta-plugin-fk")
    plugin = _plugin_teste(suporta_titularidade_corporativa=False)
    agora = utcnow_naive()

    def _conexao(**extras):
        dados = dict(
            plugin_id=plugin.id,
            user_id=user.id,
            conta_id=None,
            usuario_originador_id=user.id,
            titularidade=PluginConexao.TITULARIDADE_PESSOAL,
            estado=PluginConexao.ESTADO_AGUARDANDO_CONFIGURACAO,
            created_at=agora,
            updated_at=agora,
            estado_alterado_em=agora,
        )
        dados.update(extras)
        return PluginConexao(**dados)

    db.session.add(_conexao(user_id=999999))
    with pytest.raises(IntegrityError):
        db.session.flush()
    db.session.rollback()

    db.session.add(
        _conexao(
            conta_id=999999,
            titularidade=PluginConexao.TITULARIDADE_CORPORATIVA,
        )
    )
    with pytest.raises(IntegrityError):
        db.session.flush()
    db.session.rollback()

    db.session.add(
        PluginCapability(
            plugin_id=999999,
            chave="leitura.inexistente",
            nome="Capability sem plugin",
            status=PluginCapability.STATUS_DISPONIVEL,
            politica_maxima=PluginCapability.POLITICA_PERMITIDA,
            natureza=PluginCapability.NATUREZA_PASSIVA,
            created_at=agora,
            updated_at=agora,
        )
    )
    with pytest.raises(IntegrityError):
        db.session.flush()
    db.session.rollback()
    assert db.session.query(Plugin).filter_by(id=plugin.id).count() == 1
