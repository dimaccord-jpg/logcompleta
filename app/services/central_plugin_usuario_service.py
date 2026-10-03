"""
Experiência autenticada da Central de Plugins (SCRUM-222, lote 4).

O usuário vê o catálogo publicado para ele e administra somente a própria
conexão. Não amplia o teto da LogCompleta, não administra conexão de
terceiro e a mesma Conta não compartilha a conexão.

Não há provider neste lote. O registro de fluxos começa vazio. Um adapter
futuro entra por ``registrar_fluxo_conexao`` e indica como abrir a
configuração. A revalidação é o ponto de extensão: a implementação atual
orienta o usuário e não altera estado.

A tela recebe só dicionários allowlist. Este módulo não lê credencial,
cofre, concessão bruta nem payload.
"""
from __future__ import annotations

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.extensions import db
from app.models import (
    Plugin,
    PluginCapability,
    PluginConexao,
    PluginRestricaoUsuario,
)
from app.services.central_plugin_admin_service import (
    mascarar_identificador_externo,
    rotulo_titularidade_suportada,
)
from app.services.central_plugin_service import (
    RestricaoUsuarioInvalidaError,
    criar_conexao,
    desconectar_conexao,
    obter_conexao_usuario,
    registrar_restricao_usuario,
    remover_restricao_usuario,
    representacao_publica_conexao,
)

_CSRF_SALT = "central-plugins-usuario-csrf"
_CSRF_MAX_AGE = 3600

_STATUS_VISIVEL = (
    Plugin.STATUS_DISPONIVEL,
    Plugin.STATUS_DESABILITADO,
    Plugin.STATUS_BLOQUEADO,
)

ESTADO_LABEL = {
    PluginConexao.ESTADO_DISPONIVEL: "Disponível",
    PluginConexao.ESTADO_AGUARDANDO_CONFIGURACAO: "Aguardando configuração",
    PluginConexao.ESTADO_CONECTADO: "Conectado",
    PluginConexao.ESTADO_REQUER_ATENCAO: "Requer atenção",
    PluginConexao.ESTADO_DESABILITADO: "Desabilitado",
    PluginConexao.ESTADO_BLOQUEADO: "Bloqueado",
    PluginConexao.ESTADO_DESCONECTADO: "Desconectado",
}
_ESTADO_BADGE = {
    PluginConexao.ESTADO_DISPONIVEL: "success",
    PluginConexao.ESTADO_AGUARDANDO_CONFIGURACAO: "warning",
    PluginConexao.ESTADO_CONECTADO: "success",
    PluginConexao.ESTADO_REQUER_ATENCAO: "warning",
    PluginConexao.ESTADO_DESABILITADO: "secondary",
    PluginConexao.ESTADO_BLOQUEADO: "danger",
    PluginConexao.ESTADO_DESCONECTADO: "secondary",
}
_TITULARIDADE_CONEXAO = {
    PluginConexao.TITULARIDADE_PESSOAL: "Pessoal",
    PluginConexao.TITULARIDADE_CORPORATIVA: "Corporativa",
}

MENSAGEM_REVALIDACAO = (
    "A reconexão ainda não está disponível nesta integração. "
    "Nenhum estado foi alterado."
)
MENSAGEM_REQUER_ATENCAO = (
    "Sua conexão existe, mas precisa de uma nova validação. "
    "A reconexão será oferecida quando a integração puder concluí-la."
)
MENSAGEM_SEM_FLUXO = "A configuração desta integração ainda não está disponível."
MENSAGEM_SEM_CONEXAO_INDIVIDUAL = (
    "A conexão individual desta integração ainda não está disponível."
)
MENSAGEM_BLOQUEIO = "Esta integração está bloqueada. Você não pode remover este bloqueio."
MENSAGEM_DESABILITADO = (
    "Esta integração está temporariamente indisponível. "
    "A conexão registrada, se houver, permanece."
)
MENSAGEM_VAZIO = "Nenhuma integração disponível no momento."

_CHAVES_CONEXAO_TELA = (
    "titularidade_label",
    "identificador_externo_mascarado",
    "atualizado_em",
    "diagnostico_resumo",
)
_CHAVES_CAPABILITY_TELA = (
    "nome",
    "descricao",
    "chave",
    "permitida_pela_logcompleta",
    "bloqueada_administrativamente",
    "restringida_pelo_usuario",
    "situacao_label",
    "pode_restringir",
    "pode_remover_restricao",
)
_CHAVES_PLUGIN_TELA = (
    "nome",
    "descricao",
    "slug",
    "titularidade_label",
    "estado",
    "estado_label",
    "estado_badge",
    "orientacao",
    "capabilities",
    "conexao",
    "pode_conectar",
    "pode_desconectar",
    "pode_revalidar",
)

_FLUXOS: dict[str, "FluxoConexaoPlugin"] = {}


class CentralPluginUsuarioError(Exception):
    """Falha de jornada mostrada ao usuário, sem detalhe interno."""

    def __init__(self, mensagem: str):
        super().__init__(mensagem)
        self.mensagem = mensagem


class AcessoConexaoUsuarioNegado(CentralPluginUsuarioError):
    """A conexão não pertence ao usuário autenticado."""


class FluxoConexaoIndisponivel(CentralPluginUsuarioError):
    """O plugin não tem fluxo de conexão registrado."""


class AcaoPluginUsuarioRecusada(CentralPluginUsuarioError):
    """A ação cabe ao usuário, mas a política não permite concluí-la."""


class PluginForaDoCatalogoUsuario(CentralPluginUsuarioError):
    """Rascunho ou plugin inexistente não entra na área do usuário."""


class FluxoConexaoPlugin:
    """Contrato do adapter futuro. Não presume OAuth, token nem número.

    ``iniciar_configuracao`` abre a conexão no domínio, em aguardando
    configuração. ``revalidar`` é o ponto de extensão: neste lote devolve
    orientação e não muda estado. Um provider futuro substitui estes
    métodos quando o fluxo real existir.
    """

    def __init__(self, adapter_key: str):
        self.adapter_key = adapter_key

    def iniciar_configuracao(self, *, plugin_id: int, user_id: int) -> PluginConexao:
        return criar_conexao(
            plugin_id=int(plugin_id),
            user_id=int(user_id),
            titularidade=PluginConexao.TITULARIDADE_PESSOAL,
            usuario_originador_id=int(user_id),
        )

    def revalidar(self, *, conexao_id: int, user_id: int) -> str:
        conexao = db.session.get(PluginConexao, int(conexao_id))
        if conexao is None or int(conexao.user_id) != int(user_id):
            raise AcessoConexaoUsuarioNegado(
                "Você só pode administrar a sua própria conexão."
            )
        return MENSAGEM_REVALIDACAO


def registrar_fluxo_conexao(fluxo: FluxoConexaoPlugin) -> None:
    """Registra o fluxo de um adapter. O catálogo de produção não chama isto."""
    chave = (getattr(fluxo, "adapter_key", None) or "").strip()
    if not chave:
        raise FluxoConexaoIndisponivel("Fluxo de conexão sem identificação.")
    _FLUXOS[chave] = fluxo


def remover_fluxo_conexao(adapter_key: str) -> None:
    _FLUXOS.pop(adapter_key, None)


def limpar_fluxos_conexao() -> None:
    """Esvazia o registro. Usado pelo teste para não vazar fluxo entre casos."""
    _FLUXOS.clear()


def fluxo_registrado(adapter_key: str | None) -> bool:
    return bool(adapter_key) and adapter_key in _FLUXOS


def gerar_csrf_token_plugins_usuario(user_id: int) -> str:
    return _csrf_serializer().dumps(str(int(user_id)))


def validar_csrf_token_plugins_usuario(token: str | None, user_id: int) -> bool:
    if not token:
        return False
    try:
        valor = _csrf_serializer().loads(str(token), max_age=_CSRF_MAX_AGE)
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return False
    return str(valor) == str(int(user_id))


def mensagem_catalogo_vazio() -> str:
    return MENSAGEM_VAZIO


def montar_catalogo_usuario(user_id: int) -> list[dict]:
    """Plugins visíveis e a conexão deste usuário. Não grava evento."""
    usuario_id = int(user_id)
    plugins = (
        Plugin.query.filter(Plugin.status.in_(_STATUS_VISIVEL))
        .order_by(Plugin.nome.asc(), Plugin.id.asc())
        .all()
    )
    if not plugins:
        return []
    ids = [plugin.id for plugin in plugins]
    conexoes = (
        PluginConexao.query.filter(
            PluginConexao.user_id == usuario_id,
            PluginConexao.plugin_id.in_(ids),
        )
        .order_by(PluginConexao.id.asc())
        .all()
    )
    por_plugin: dict[int, list[PluginConexao]] = {}
    for conexao in conexoes:
        por_plugin.setdefault(conexao.plugin_id, []).append(conexao)
    ocupando_ids = [
        conexao.id
        for conexao in conexoes
        if conexao.estado in PluginConexao.ESTADOS_QUE_OCUPAM_SLOT
    ]
    restricoes: dict[tuple[int, int], PluginRestricaoUsuario] = {}
    if ocupando_ids:
        for restricao in PluginRestricaoUsuario.query.filter(
            PluginRestricaoUsuario.conexao_id.in_(ocupando_ids),
            PluginRestricaoUsuario.user_id == usuario_id,
        ).all():
            restricoes[(restricao.conexao_id, restricao.capability_id)] = restricao
    capabilities = (
        PluginCapability.query.filter(PluginCapability.plugin_id.in_(ids))
        .order_by(PluginCapability.nome.asc(), PluginCapability.id.asc())
        .all()
    )
    por_capability: dict[int, list[PluginCapability]] = {}
    for capability in capabilities:
        por_capability.setdefault(capability.plugin_id, []).append(capability)

    catalogo = []
    for plugin in plugins:
        linhas = por_plugin.get(plugin.id, [])
        slot = next((linha for linha in linhas if linha.ocupa_slot()), None)
        desconectada = next(
            (
                linha
                for linha in reversed(linhas)
                if linha.estado == PluginConexao.ESTADO_DESCONECTADO
            ),
            None,
        )
        referencia = slot or desconectada
        estado = _estado_visual(plugin, slot, desconectada)
        pode_conectar = _pode_conectar(plugin, slot)
        catalogo.append(
            _allow(
                {
                    "nome": plugin.nome,
                    "descricao": plugin.descricao or "",
                    "slug": plugin.slug,
                    "titularidade_label": rotulo_titularidade_suportada(
                        plugin.suporta_titularidade_pessoal,
                        plugin.suporta_titularidade_corporativa,
                    ),
                    "estado": estado,
                    "estado_label": ESTADO_LABEL.get(estado, estado),
                    "estado_badge": _ESTADO_BADGE.get(estado, "secondary"),
                    "orientacao": _orientacao(
                        plugin, estado, pode_conectar, tem_conexao_ativa=slot is not None
                    ),
                    "capabilities": [
                        _capability_tela(
                            plugin,
                            capability,
                            slot,
                            restricoes.get((slot.id, capability.id)) if slot else None,
                        )
                        for capability in por_capability.get(plugin.id, [])
                    ],
                    "conexao": _conexao_tela(referencia) if referencia is not None else None,
                    "pode_conectar": pode_conectar,
                    "pode_desconectar": _pode_desconectar(plugin, slot),
                    "pode_revalidar": _pode_revalidar(plugin, slot),
                },
                _CHAVES_PLUGIN_TELA,
            )
        )
    return catalogo


def iniciar_configuracao_usuario(*, user_id: int, slug: str) -> PluginConexao:
    plugin = _plugin_obrigatorio_visivel(slug)
    if plugin.status != Plugin.STATUS_DISPONIVEL:
        raise AcaoPluginUsuarioRecusada(MENSAGEM_SEM_FLUXO)
    if not plugin.suporta_titularidade_pessoal:
        raise AcaoPluginUsuarioRecusada(MENSAGEM_SEM_CONEXAO_INDIVIDUAL)
    fluxo = _FLUXOS.get(plugin.adapter_key)
    if fluxo is None:
        raise FluxoConexaoIndisponivel(MENSAGEM_SEM_FLUXO)
    if obter_conexao_usuario(int(user_id), plugin.id) is not None:
        raise AcaoPluginUsuarioRecusada("Você já tem uma conexão em andamento nesta integração.")
    conexao = fluxo.iniciar_configuracao(plugin_id=plugin.id, user_id=int(user_id))
    if int(conexao.user_id) != int(user_id) or int(conexao.plugin_id) != int(plugin.id):
        raise AcessoConexaoUsuarioNegado("Você só pode administrar a sua própria conexão.")
    if conexao.estado != PluginConexao.ESTADO_AGUARDANDO_CONFIGURACAO:
        raise AcaoPluginUsuarioRecusada(
            "A configuração foi iniciada e ainda não foi concluída."
        )
    return conexao


def revalidar_conexao_usuario(*, user_id: int, slug: str) -> str:
    """Ponto de extensão. Não altera estado enquanto não houver provider."""
    plugin = _plugin_obrigatorio_visivel(slug)
    conexao = _conexao_propria(user_id, plugin)
    if conexao.estado != PluginConexao.ESTADO_REQUER_ATENCAO or plugin.status != Plugin.STATUS_DISPONIVEL:
        raise AcaoPluginUsuarioRecusada(MENSAGEM_REVALIDACAO)
    fluxo = _FLUXOS.get(plugin.adapter_key)
    if fluxo is None:
        raise FluxoConexaoIndisponivel(MENSAGEM_REVALIDACAO)
    estado_antes = conexao.estado
    mensagem = fluxo.revalidar(conexao_id=conexao.id, user_id=int(user_id))
    db.session.refresh(conexao)
    if conexao.estado != estado_antes:
        raise AcaoPluginUsuarioRecusada(
            "A revalidação não pode alterar o estado da conexão neste momento."
        )
    return mensagem or MENSAGEM_REVALIDACAO


def restringir_capability_usuario(*, user_id: int, slug: str, chave: str) -> None:
    plugin, conexao, capability = _alvo_mutavel(user_id, slug, chave)
    if not _permitida_pela_logcompleta(plugin, capability):
        raise AcaoPluginUsuarioRecusada(
            "Você não pode liberar o que a LogCompleta bloqueou."
        )
    try:
        registrar_restricao_usuario(
            conexao_id=conexao.id,
            capability_id=capability.id,
            user_id=int(user_id),
            usuario_originador_id=int(user_id),
        )
    except RestricaoUsuarioInvalidaError as exc:
        raise AcaoPluginUsuarioRecusada(
            "Não foi possível restringir esta permissão."
        ) from exc


def remover_restricao_capability_usuario(*, user_id: int, slug: str, chave: str) -> None:
    plugin = _plugin_obrigatorio_visivel(slug)
    conexao = _conexao_propria(user_id, plugin)
    capability = _capability_do_plugin(plugin, chave)
    if not _conexao_administravel(plugin, conexao):
        raise AcaoPluginUsuarioRecusada("Esta conexão não pode ser alterada neste estado.")
    try:
        remover_restricao_usuario(
            conexao_id=conexao.id,
            capability_id=capability.id,
            user_id=int(user_id),
            usuario_originador_id=int(user_id),
        )
    except RestricaoUsuarioInvalidaError as exc:
        raise AcaoPluginUsuarioRecusada(
            "Não foi possível remover a sua restrição."
        ) from exc


def desconectar_conexao_usuario(*, user_id: int, slug: str) -> PluginConexao:
    plugin = _plugin_obrigatorio_visivel(slug)
    conexao = _conexao_propria(user_id, plugin)
    if plugin.status == Plugin.STATUS_BLOQUEADO or conexao.estado == PluginConexao.ESTADO_BLOQUEADO:
        raise AcaoPluginUsuarioRecusada(MENSAGEM_BLOQUEIO)
    return desconectar_conexao(conexao.id, usuario_originador_id=int(user_id))


def _csrf_serializer() -> URLSafeTimedSerializer:
    from flask import current_app

    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=_CSRF_SALT)


def _plugin_obrigatorio_visivel(slug: str) -> Plugin:
    plugin = Plugin.query.filter_by(slug=(slug or "").strip()).first()
    if plugin is None or plugin.status not in _STATUS_VISIVEL:
        raise PluginForaDoCatalogoUsuario("Integração indisponível.")
    return plugin


def _conexao_propria(user_id: int, plugin: Plugin) -> PluginConexao:
    conexao = obter_conexao_usuario(int(user_id), plugin.id)
    if conexao is None or int(conexao.user_id) != int(user_id):
        raise AcessoConexaoUsuarioNegado("Você só pode administrar a sua própria conexão.")
    return conexao


def _capability_do_plugin(plugin: Plugin, chave: str) -> PluginCapability:
    capability = PluginCapability.query.filter_by(
        plugin_id=plugin.id,
        chave=(chave or "").strip(),
    ).first()
    if capability is None or int(capability.plugin_id) != int(plugin.id):
        raise AcaoPluginUsuarioRecusada("Esta permissão não pertence à integração.")
    return capability


def _alvo_mutavel(user_id: int, slug: str, chave: str):
    plugin = _plugin_obrigatorio_visivel(slug)
    conexao = _conexao_propria(user_id, plugin)
    capability = _capability_do_plugin(plugin, chave)
    if not _conexao_administravel(plugin, conexao):
        raise AcaoPluginUsuarioRecusada("Esta conexão não pode ser alterada neste estado.")
    return plugin, conexao, capability


def _permitida_pela_logcompleta(plugin: Plugin, capability: PluginCapability) -> bool:
    return (
        plugin.status == Plugin.STATUS_DISPONIVEL
        and capability.status == PluginCapability.STATUS_DISPONIVEL
        and capability.politica_maxima == PluginCapability.POLITICA_PERMITIDA
    )


def _bloqueada_administrativamente(plugin: Plugin, capability: PluginCapability) -> bool:
    if plugin.status in (Plugin.STATUS_BLOQUEADO, Plugin.STATUS_DESABILITADO):
        return True
    if capability.politica_maxima != PluginCapability.POLITICA_PERMITIDA:
        return True
    if capability.status != PluginCapability.STATUS_DISPONIVEL:
        return True
    return False


def _conexao_administravel(plugin: Plugin, conexao: PluginConexao) -> bool:
    return (
        plugin.status == Plugin.STATUS_DISPONIVEL
        and conexao.ocupa_slot()
        and conexao.estado
        not in (
            PluginConexao.ESTADO_BLOQUEADO,
            PluginConexao.ESTADO_DESABILITADO,
        )
    )


def _pode_conectar(plugin: Plugin, slot: PluginConexao | None) -> bool:
    return (
        slot is None
        and plugin.status == Plugin.STATUS_DISPONIVEL
        and bool(plugin.suporta_titularidade_pessoal)
        and fluxo_registrado(plugin.adapter_key)
    )


def _pode_desconectar(plugin: Plugin, slot: PluginConexao | None) -> bool:
    return (
        slot is not None
        and plugin.status != Plugin.STATUS_BLOQUEADO
        and slot.estado != PluginConexao.ESTADO_BLOQUEADO
    )


def _pode_revalidar(plugin: Plugin, slot: PluginConexao | None) -> bool:
    return (
        slot is not None
        and slot.estado == PluginConexao.ESTADO_REQUER_ATENCAO
        and plugin.status == Plugin.STATUS_DISPONIVEL
        and fluxo_registrado(plugin.adapter_key)
    )


def _estado_visual(
    plugin: Plugin,
    slot: PluginConexao | None,
    desconectada: PluginConexao | None,
) -> str:
    if plugin.status == Plugin.STATUS_BLOQUEADO:
        return PluginConexao.ESTADO_BLOQUEADO
    if plugin.status == Plugin.STATUS_DESABILITADO:
        return PluginConexao.ESTADO_DESABILITADO
    if slot is not None:
        return slot.estado
    if desconectada is not None:
        return PluginConexao.ESTADO_DESCONECTADO
    return PluginConexao.ESTADO_DISPONIVEL


def _orientacao(
    plugin: Plugin,
    estado: str,
    pode_conectar: bool,
    *,
    tem_conexao_ativa: bool,
) -> str:
    if estado == PluginConexao.ESTADO_BLOQUEADO:
        return MENSAGEM_BLOQUEIO
    if estado == PluginConexao.ESTADO_DESABILITADO:
        return MENSAGEM_DESABILITADO
    if estado == PluginConexao.ESTADO_AGUARDANDO_CONFIGURACAO:
        return "A configuração foi iniciada e ainda não foi concluída."
    if estado == PluginConexao.ESTADO_CONECTADO:
        return "Sua conexão está ativa. Você pode restringir o que ela faz na sua conta."
    if estado == PluginConexao.ESTADO_REQUER_ATENCAO:
        return MENSAGEM_REQUER_ATENCAO
    if estado == PluginConexao.ESTADO_DESCONECTADO:
        return (
            "Não há vínculo operacional ativo. "
            "Uma nova conexão será necessária quando a configuração estiver disponível."
        )
    if tem_conexao_ativa:
        return "Sua conexão está registrada e ainda não entrou em configuração."
    if pode_conectar:
        return "Você pode iniciar a configuração desta integração."
    if not plugin.suporta_titularidade_pessoal:
        return MENSAGEM_SEM_CONEXAO_INDIVIDUAL
    return MENSAGEM_SEM_FLUXO


def _conexao_tela(conexao: PluginConexao) -> dict:
    publica = representacao_publica_conexao(conexao)
    atualizado = conexao.updated_at.strftime("%d/%m/%Y %H:%M") if conexao.updated_at else ""
    return _allow(
        {
            "titularidade_label": _TITULARIDADE_CONEXAO.get(
                publica.get("titularidade"), ""
            ),
            "identificador_externo_mascarado": mascarar_identificador_externo(
                publica.get("identificador_externo")
            )
            or "",
            "atualizado_em": atualizado,
            "diagnostico_resumo": publica.get("diagnostico_resumo") or "",
        },
        _CHAVES_CONEXAO_TELA,
    )


def _capability_tela(
    plugin: Plugin,
    capability: PluginCapability,
    slot: PluginConexao | None,
    restricao: PluginRestricaoUsuario | None,
) -> dict:
    permitida = _permitida_pela_logcompleta(plugin, capability)
    bloqueada = _bloqueada_administrativamente(plugin, capability)
    restringida = restricao is not None and slot is not None and restricao.user_id == slot.user_id
    administravel = slot is not None and _conexao_administravel(plugin, slot)
    return _allow(
        {
            "nome": capability.nome,
            "descricao": capability.descricao or "",
            "chave": capability.chave,
            "permitida_pela_logcompleta": permitida,
            "bloqueada_administrativamente": bloqueada,
            "restringida_pelo_usuario": restringida,
            "situacao_label": _rotulo_capability(permitida, bloqueada, restringida),
            "pode_restringir": administravel and permitida and not restringida,
            "pode_remover_restricao": administravel and restringida,
        },
        _CHAVES_CAPABILITY_TELA,
    )


def _rotulo_capability(permitida: bool, bloqueada: bool, restringida: bool) -> str:
    if bloqueada and restringida:
        return "Bloqueada pela LogCompleta. A sua restrição adicional continua registrada."
    if bloqueada:
        return "Bloqueada pela LogCompleta"
    if permitida and restringida:
        return "Permitida pela LogCompleta, restringida por você"
    if restringida:
        return "Restringida por você"
    if permitida:
        return "Permitida pela LogCompleta"
    return "Indisponível"


def _allow(dados: dict, chaves: tuple[str, ...]) -> dict:
    return {chave: dados[chave] for chave in chaves}
