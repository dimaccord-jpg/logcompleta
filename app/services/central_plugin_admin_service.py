"""
Governança administrativa da Central de Plugins (SCRUM-222, lote 3).

Quem altera o catálogo é o ADM da LogCompleta (`User.is_admin is True`).
Contratante de plano Multiusuário não entra por ser administrador da Conta.

Este módulo persiste metadados, status, titularidade, capability e o teto
máximo. Não chama provider, não executa capability e não desconecta
usuários quando o catálogo é bloqueado ou desabilitado. A decisão de
execução continua no domínio já existente.

A trilha daqui é PluginAuditoriaAdministrativa. PluginEventoCentral
permanece a trilha operacional da conexão.
"""
from __future__ import annotations

import json

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import func

from app.extensions import db
from app.models import (
    Plugin,
    PluginAuditoriaAdministrativa,
    PluginCapability,
    PluginConexao,
    PluginEventoCentral,
    User,
    utcnow_naive,
)
from app.services.central_plugin_service import (
    CapabilityInvalidaError,
    MaterialSensivelRecusadoError,
    PluginInvalidoError,
    _recusar_material_sensivel,
    atualizar_governanca_capability,
    atualizar_metadados_plugin,
    definir_status_plugin,
    definir_titularidade_plugin,
    listar_capabilities,
    listar_plugins,
    registrar_capability,
    registrar_plugin,
)

_CSRF_SALT = "central-plugins-admin-csrf"
_CSRF_MAX_AGE = 3600
_LIMITE_TRILHA = 50
_VALOR_AUDITORIA_MAX = 2000

STATUS_PLUGIN_LABEL = {
    Plugin.STATUS_RASCUNHO: "Rascunho",
    Plugin.STATUS_DISPONIVEL: "Disponível",
    Plugin.STATUS_DESABILITADO: "Desabilitado",
    Plugin.STATUS_BLOQUEADO: "Bloqueado",
}
STATUS_CAPABILITY_LABEL = {
    PluginCapability.STATUS_DISPONIVEL: "Disponível",
    PluginCapability.STATUS_DESABILITADO: "Desabilitado",
    PluginCapability.STATUS_BLOQUEADO: "Bloqueado",
}
POLITICA_LABEL = {
    PluginCapability.POLITICA_PERMITIDA: "Permitida",
    PluginCapability.POLITICA_NEGADA: "Negada",
}
NATUREZA_LABEL = {
    PluginCapability.NATUREZA_ATIVA: "Ativa",
    PluginCapability.NATUREZA_PASSIVA: "Passiva",
}
ESTADO_CONEXAO_LABEL = {
    PluginConexao.ESTADO_DISPONIVEL: "Disponível",
    PluginConexao.ESTADO_AGUARDANDO_CONFIGURACAO: "Aguardando configuração",
    PluginConexao.ESTADO_CONECTADO: "Conectado",
    PluginConexao.ESTADO_REQUER_ATENCAO: "Requer atenção",
    PluginConexao.ESTADO_DESABILITADO: "Desabilitado",
    PluginConexao.ESTADO_BLOQUEADO: "Bloqueado",
    PluginConexao.ESTADO_DESCONECTADO: "Desconectado",
}
TITULARIDADE_CONEXAO_LABEL = {
    PluginConexao.TITULARIDADE_PESSOAL: "Pessoal",
    PluginConexao.TITULARIDADE_CORPORATIVA: "Corporativa",
}
ALTERACAO_LABEL = {
    PluginAuditoriaAdministrativa.ALTERACAO_PLUGIN_CRIADO: "Plugin cadastrado",
    PluginAuditoriaAdministrativa.ALTERACAO_PLUGIN_METADADOS: "Metadados",
    PluginAuditoriaAdministrativa.ALTERACAO_PLUGIN_STATUS: "Status",
    PluginAuditoriaAdministrativa.ALTERACAO_PLUGIN_TITULARIDADE: "Titularidade",
    PluginAuditoriaAdministrativa.ALTERACAO_CAPABILITY_CRIADA: "Capability cadastrada",
    PluginAuditoriaAdministrativa.ALTERACAO_CAPABILITY_POLITICA: "Política máxima",
    PluginAuditoriaAdministrativa.ALTERACAO_CAPABILITY_STATUS: "Status da capability",
}
EVENTO_LABEL = {
    PluginEventoCentral.TIPO_CONEXAO_CRIADA: "Conexão criada",
    PluginEventoCentral.TIPO_CONEXAO_ESTADO_ALTERADO: "Estado da conexão alterado",
    PluginEventoCentral.TIPO_RESTRICAO_USUARIO_ALTERADA: "Restrição do usuário alterada",
    PluginEventoCentral.TIPO_BLOQUEADO: "Conexão bloqueada",
    PluginEventoCentral.TIPO_DESABILITADO: "Conexão desabilitada",
    PluginEventoCentral.TIPO_DESCONECTADO: "Conexão desconectada",
    PluginEventoCentral.TIPO_REVOGADO: "Conexão revogada",
    PluginEventoCentral.TIPO_AUTORIZACAO_NEGADA: "Autorização negada",
}
_BADGE = {
    "rascunho": "secondary",
    "disponivel": "success",
    "desabilitado": "warning",
    "bloqueado": "danger",
    "negada": "danger",
    "permitida": "success",
    "ativa": "primary",
    "passiva": "secondary",
}


class GovernancaPluginNaoAutorizadaError(Exception):
    """O ator não é administrador interno da LogCompleta."""


def gerar_csrf_token_admin_plugins(user_id: int) -> str:
    return _csrf_serializer().dumps(str(int(user_id)))


def validar_csrf_token_admin_plugins(token: str | None, user_id: int) -> bool:
    if not token:
        return False
    try:
        valor = _csrf_serializer().loads(str(token), max_age=_CSRF_MAX_AGE)
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return False
    return str(valor) == str(int(user_id))


def opcoes_catalogo() -> dict:
    return {
        "statuses_plugin": _opcoes(Plugin.STATUSES, STATUS_PLUGIN_LABEL),
        "statuses_capability": _opcoes(PluginCapability.STATUSES, STATUS_CAPABILITY_LABEL),
        "politicas": _opcoes(PluginCapability.POLITICAS, POLITICA_LABEL),
        "naturezas": _opcoes(PluginCapability.NATUREZAS, NATUREZA_LABEL),
    }


def listar_plugins_admin(*, ator_user_id: int, status: str | None = None) -> list[dict]:
    _ator_admin(ator_user_id)
    plugins = listar_plugins(status=status)
    ids = [plugin.id for plugin in plugins]
    capacidades, conexoes = _contagens(ids)
    return [
        _resumo_plugin(
            plugin,
            quantidade_capabilities=capacidades.get(plugin.id, 0),
            quantidade_conexoes=conexoes.get(plugin.id, 0),
        )
        for plugin in plugins
    ]


def montar_detalhe_admin(*, ator_user_id: int, plugin_id: int) -> dict | None:
    """Visão do detalhe. Não inclui cofre, credencial, token nem payload."""
    _ator_admin(ator_user_id)
    plugin = db.session.get(Plugin, int(plugin_id))
    if plugin is None:
        return None
    capabilities = listar_capabilities(plugin.id)
    return {
        "plugin": _ficha_plugin(plugin, quantidade_capabilities=len(capabilities)),
        "capabilities": [_ficha_capability(item) for item in capabilities],
        "conexoes": _listar_conexoes_seguras(plugin.id),
        "eventos": _listar_eventos(plugin.id),
        "auditoria": _listar_auditoria(plugin.id),
        "opcoes": opcoes_catalogo(),
    }


def cadastrar_plugin_admin(
    *,
    ator_user_id: int,
    slug: str,
    nome: str,
    adapter_key: str,
    suporta_titularidade_pessoal: bool,
    suporta_titularidade_corporativa: bool,
    descricao: str | None = None,
    status: str = Plugin.STATUS_RASCUNHO,
) -> Plugin:
    def _gravar() -> Plugin:
        ator = _ator_admin(ator_user_id)
        plugin = registrar_plugin(
            slug=slug,
            nome=nome,
            adapter_key=adapter_key,
            suporta_titularidade_pessoal=suporta_titularidade_pessoal,
            suporta_titularidade_corporativa=suporta_titularidade_corporativa,
            descricao=descricao,
            status=status,
            criado_por_user_id=ator.id,
            commit=False,
        )
        _auditar_criacao_plugin(ator_user_id=ator.id, plugin=plugin)
        return plugin

    return _confirmar_retorno(_gravar)


def editar_metadados_admin(
    *,
    ator_user_id: int,
    plugin_id: int,
    nome: str,
    descricao: str | None,
    adapter_key: str,
) -> bool:
    def _gravar() -> bool:
        _ator_admin(ator_user_id)
        plugin = _plugin_obrigatorio(plugin_id)
        antes = {
            "nome": plugin.nome,
            "descricao": plugin.descricao,
            "adapter_key": plugin.adapter_key,
        }
        atualizar_metadados_plugin(
            plugin.id,
            nome=nome,
            descricao=descricao,
            adapter_key=adapter_key,
            commit=False,
        )
        mudou = False
        for campo in ("nome", "descricao", "adapter_key"):
            if antes[campo] == getattr(plugin, campo):
                continue
            _auditar(
                ator_user_id=ator_user_id,
                plugin_id=plugin.id,
                entidade=PluginAuditoriaAdministrativa.ENTIDADE_PLUGIN,
                alteracao=PluginAuditoriaAdministrativa.ALTERACAO_PLUGIN_METADADOS,
                campo=campo,
                valor_anterior=antes[campo],
                valor_novo=getattr(plugin, campo),
            )
            mudou = True
        return mudou

    return _confirmar(_gravar)


def definir_status_catalogo_admin(*, ator_user_id: int, plugin_id: int, status: str) -> bool:
    def _gravar() -> bool:
        _ator_admin(ator_user_id)
        plugin = _plugin_obrigatorio(plugin_id)
        antes = plugin.status
        definir_status_plugin(plugin.id, status, commit=False)
        if plugin.status == antes:
            return False
        _auditar(
            ator_user_id=ator_user_id,
            plugin_id=plugin.id,
            entidade=PluginAuditoriaAdministrativa.ENTIDADE_PLUGIN,
            alteracao=PluginAuditoriaAdministrativa.ALTERACAO_PLUGIN_STATUS,
            campo="status",
            valor_anterior=antes,
            valor_novo=plugin.status,
        )
        return True

    return _confirmar(_gravar)


def definir_titularidade_catalogo_admin(
    *,
    ator_user_id: int,
    plugin_id: int,
    suporta_titularidade_pessoal: bool,
    suporta_titularidade_corporativa: bool,
) -> bool:
    def _gravar() -> bool:
        _ator_admin(ator_user_id)
        plugin = _plugin_obrigatorio(plugin_id)
        antes = _codigo_titularidade(
            plugin.suporta_titularidade_pessoal,
            plugin.suporta_titularidade_corporativa,
        )
        definir_titularidade_plugin(
            plugin.id,
            suporta_titularidade_pessoal=suporta_titularidade_pessoal,
            suporta_titularidade_corporativa=suporta_titularidade_corporativa,
            commit=False,
        )
        depois = _codigo_titularidade(
            plugin.suporta_titularidade_pessoal,
            plugin.suporta_titularidade_corporativa,
        )
        if antes == depois:
            return False
        _auditar(
            ator_user_id=ator_user_id,
            plugin_id=plugin.id,
            entidade=PluginAuditoriaAdministrativa.ENTIDADE_PLUGIN,
            alteracao=PluginAuditoriaAdministrativa.ALTERACAO_PLUGIN_TITULARIDADE,
            campo="titularidade",
            valor_anterior=antes,
            valor_novo=depois,
        )
        return True

    return _confirmar(_gravar)


def cadastrar_capability_admin(
    *,
    ator_user_id: int,
    plugin_id: int,
    chave: str,
    nome: str,
    politica_maxima: str,
    natureza: str,
    descricao: str | None = None,
    status: str = PluginCapability.STATUS_DISPONIVEL,
) -> PluginCapability:
    def _gravar() -> PluginCapability:
        ator = _ator_admin(ator_user_id)
        capability = registrar_capability(
            plugin_id=plugin_id,
            chave=chave,
            nome=nome,
            politica_maxima=politica_maxima,
            natureza=natureza,
            descricao=descricao,
            status=status,
            commit=False,
        )
        _auditar_criacao_capability(ator_user_id=ator.id, capability=capability)
        return capability

    return _confirmar_retorno(_gravar)


def governar_capability_admin(
    *,
    ator_user_id: int,
    plugin_id: int,
    capability_id: int,
    politica_maxima: str,
    status: str,
) -> bool:
    def _gravar() -> bool:
        _ator_admin(ator_user_id)
        capability = db.session.get(PluginCapability, int(capability_id))
        if capability is None or capability.plugin_id != int(plugin_id):
            raise CapabilityInvalidaError("Capability inexistente neste plugin.")
        antes_politica = capability.politica_maxima
        antes_status = capability.status
        atualizar_governanca_capability(
            capability.id,
            plugin_id=plugin_id,
            politica_maxima=politica_maxima,
            status=status,
            commit=False,
        )
        mudou = False
        if antes_politica != capability.politica_maxima:
            _auditar(
                ator_user_id=ator_user_id,
                plugin_id=capability.plugin_id,
                capability_id=capability.id,
                entidade=PluginAuditoriaAdministrativa.ENTIDADE_CAPABILITY,
                alteracao=PluginAuditoriaAdministrativa.ALTERACAO_CAPABILITY_POLITICA,
                campo="politica_maxima",
                valor_anterior=antes_politica,
                valor_novo=capability.politica_maxima,
            )
            mudou = True
        if antes_status != capability.status:
            _auditar(
                ator_user_id=ator_user_id,
                plugin_id=capability.plugin_id,
                capability_id=capability.id,
                entidade=PluginAuditoriaAdministrativa.ENTIDADE_CAPABILITY,
                alteracao=PluginAuditoriaAdministrativa.ALTERACAO_CAPABILITY_STATUS,
                campo="status",
                valor_anterior=antes_status,
                valor_novo=capability.status,
            )
            mudou = True
        return mudou

    return _confirmar(_gravar)


def mascarar_identificador_externo(valor: str | None) -> str | None:
    if valor is None:
        return None
    texto = str(valor).strip()
    if not texto:
        return None
    if len(texto) <= 6:
        return "••••"
    return f"{texto[:2]}…{texto[-2:]}"


def _csrf_serializer() -> URLSafeTimedSerializer:
    from flask import current_app

    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=_CSRF_SALT)


def _ator_admin(user_id: int) -> User:
    try:
        ator_id = int(user_id)
    except (TypeError, ValueError) as exc:
        raise GovernancaPluginNaoAutorizadaError(
            "A governança de plugins exige administrador da LogCompleta."
        ) from exc
    ator = db.session.get(User, ator_id)
    if ator is None or ator.is_admin is not True:
        raise GovernancaPluginNaoAutorizadaError(
            "A governança de plugins exige administrador da LogCompleta."
        )
    return ator


def _plugin_obrigatorio(plugin_id: int) -> Plugin:
    plugin = db.session.get(Plugin, int(plugin_id))
    if plugin is None:
        raise PluginInvalidoError("Plugin inexistente.")
    return plugin


def _confirmar(gravar) -> bool:
    try:
        mudou = bool(gravar())
        if mudou:
            db.session.commit()
        else:
            db.session.rollback()
        return mudou
    except Exception:
        db.session.rollback()
        raise


def _confirmar_retorno(gravar):
    try:
        resultado = gravar()
        db.session.commit()
        if resultado is not None:
            db.session.refresh(resultado)
        return resultado
    except Exception:
        db.session.rollback()
        raise


def _codigo_titularidade(pessoal: bool, corporativa: bool) -> str:
    if pessoal and corporativa:
        return "ambas"
    if pessoal:
        return "pessoal"
    if corporativa:
        return "corporativa"
    return "nenhuma"


def rotulo_titularidade_suportada(pessoal: bool, corporativa: bool) -> str:
    codigo = _codigo_titularidade(pessoal, corporativa)
    return {
        "ambas": "Pessoal e corporativa",
        "pessoal": "Pessoal",
        "corporativa": "Corporativa",
        "nenhuma": "Nenhuma",
    }[codigo]


def _opcoes(valores, rotulos: dict) -> list[dict]:
    return [{"valor": valor, "rotulo": rotulos.get(valor, valor)} for valor in valores]


def _badge(codigo: str | None) -> str:
    return _BADGE.get(codigo or "", "secondary")


def _contagens(plugin_ids: list[int]) -> tuple[dict, dict]:
    if not plugin_ids:
        return {}, {}
    capacidades = dict(
        db.session.query(PluginCapability.plugin_id, func.count(PluginCapability.id))
        .filter(PluginCapability.plugin_id.in_(plugin_ids))
        .group_by(PluginCapability.plugin_id)
        .all()
    )
    conexoes = dict(
        db.session.query(PluginConexao.plugin_id, func.count(PluginConexao.id))
        .filter(PluginConexao.plugin_id.in_(plugin_ids))
        .group_by(PluginConexao.plugin_id)
        .all()
    )
    return capacidades, conexoes


def _resumo_plugin(plugin: Plugin, *, quantidade_capabilities: int, quantidade_conexoes: int) -> dict:
    return {
        "id": plugin.id,
        "nome": plugin.nome,
        "slug": plugin.slug,
        "status": plugin.status,
        "status_label": STATUS_PLUGIN_LABEL.get(plugin.status, plugin.status),
        "status_badge": _badge(plugin.status),
        "adapter_key": plugin.adapter_key,
        "titularidade_label": rotulo_titularidade_suportada(
            plugin.suporta_titularidade_pessoal,
            plugin.suporta_titularidade_corporativa,
        ),
        "quantidade_capabilities": int(quantidade_capabilities),
        "quantidade_conexoes": int(quantidade_conexoes),
        "updated_at": plugin.updated_at,
    }


def _ficha_plugin(plugin: Plugin, *, quantidade_capabilities: int) -> dict:
    ficha = _resumo_plugin(
        plugin,
        quantidade_capabilities=quantidade_capabilities,
        quantidade_conexoes=plugin.conexoes.count(),
    )
    ficha.update(
        {
            "descricao": plugin.descricao,
            "suporta_titularidade_pessoal": bool(plugin.suporta_titularidade_pessoal),
            "suporta_titularidade_corporativa": bool(plugin.suporta_titularidade_corporativa),
            "impede_execucao": plugin.status
            in (Plugin.STATUS_DESABILITADO, Plugin.STATUS_BLOQUEADO),
        }
    )
    return ficha


def _ficha_capability(capability: PluginCapability) -> dict:
    return {
        "id": capability.id,
        "chave": capability.chave,
        "nome": capability.nome,
        "descricao": capability.descricao,
        "natureza": capability.natureza,
        "natureza_label": NATUREZA_LABEL.get(capability.natureza, capability.natureza),
        "status": capability.status,
        "status_label": STATUS_CAPABILITY_LABEL.get(capability.status, capability.status),
        "status_badge": _badge(capability.status),
        "politica_maxima": capability.politica_maxima,
        "politica_label": POLITICA_LABEL.get(capability.politica_maxima, capability.politica_maxima),
        "politica_badge": _badge(capability.politica_maxima),
    }


def _email_usuario(user: User | None) -> str:
    if user is None or not user.email:
        return ""
    return user.email


def _listar_conexoes_seguras(plugin_id: int) -> list[dict]:
    """Allowlist operacional. Não lê referência de cofre nem credencial."""
    conexoes = (
        PluginConexao.query.filter_by(plugin_id=int(plugin_id))
        .order_by(PluginConexao.updated_at.desc(), PluginConexao.id.desc())
        .limit(_LIMITE_TRILHA)
        .all()
    )
    linhas = []
    for conexao in conexoes:
        conta_nome = None
        if conexao.conta_id is not None and conexao.conta is not None:
            conta_nome = conexao.conta.nome
        linhas.append(
            {
                "id": conexao.id,
                "usuario_email": _email_usuario(conexao.user),
                "conta_nome": conta_nome,
                "titularidade": conexao.titularidade,
                "titularidade_label": TITULARIDADE_CONEXAO_LABEL.get(
                    conexao.titularidade, conexao.titularidade
                ),
                "estado": conexao.estado,
                "estado_label": ESTADO_CONEXAO_LABEL.get(conexao.estado, conexao.estado),
                "estado_badge": _badge(conexao.estado),
                "identificador_externo_mascarado": mascarar_identificador_externo(
                    conexao.identificador_externo
                ),
                "updated_at": conexao.updated_at,
                "diagnostico_codigo": conexao.diagnostico_codigo,
                "diagnostico_resumo": conexao.diagnostico_resumo,
            }
        )
    return linhas


def _listar_eventos(plugin_id: int) -> list[dict]:
    eventos = (
        PluginEventoCentral.query.filter_by(plugin_id=int(plugin_id))
        .order_by(PluginEventoCentral.id.desc())
        .limit(_LIMITE_TRILHA)
        .all()
    )
    return [
        {
            "id": evento.id,
            "tipo_evento": evento.tipo_evento,
            "tipo_label": EVENTO_LABEL.get(evento.tipo_evento, evento.tipo_evento),
            "detalhe_codigo": evento.detalhe_codigo,
            "conexao_id": evento.conexao_id,
            "usuario_email": _email_usuario(evento.user),
            "estado_anterior": evento.estado_anterior,
            "estado_novo": evento.estado_novo,
            "created_at": evento.created_at,
        }
        for evento in eventos
    ]


def _listar_auditoria(plugin_id: int) -> list[dict]:
    linhas = (
        PluginAuditoriaAdministrativa.query.filter_by(plugin_id=int(plugin_id))
        .order_by(PluginAuditoriaAdministrativa.id.desc())
        .limit(_LIMITE_TRILHA)
        .all()
    )
    return [
        {
            "id": linha.id,
            "entidade": linha.entidade,
            "alteracao": linha.alteracao,
            "alteracao_label": ALTERACAO_LABEL.get(linha.alteracao, linha.alteracao),
            "campo": linha.campo,
            "valor_anterior": linha.valor_anterior,
            "valor_novo": linha.valor_novo,
            "capability_nome": (
                linha.capability.nome
                if linha.capability_id is not None and linha.capability is not None
                else None
            ),
            "ator_email": _email_usuario(linha.ator),
            "created_at": linha.created_at,
        }
        for linha in linhas
    ]


def _auditar_criacao_plugin(*, ator_user_id: int, plugin: Plugin) -> None:
    pares = (
        ("slug", None, plugin.slug),
        ("nome", None, plugin.nome),
        ("adapter_key", None, plugin.adapter_key),
        ("status", None, plugin.status),
        (
            "titularidade",
            None,
            _codigo_titularidade(
                plugin.suporta_titularidade_pessoal,
                plugin.suporta_titularidade_corporativa,
            ),
        ),
    )
    for campo, anterior, novo in pares:
        _auditar(
            ator_user_id=ator_user_id,
            plugin_id=plugin.id,
            entidade=PluginAuditoriaAdministrativa.ENTIDADE_PLUGIN,
            alteracao=PluginAuditoriaAdministrativa.ALTERACAO_PLUGIN_CRIADO,
            campo=campo,
            valor_anterior=anterior,
            valor_novo=novo,
        )
    if plugin.descricao:
        _auditar(
            ator_user_id=ator_user_id,
            plugin_id=plugin.id,
            entidade=PluginAuditoriaAdministrativa.ENTIDADE_PLUGIN,
            alteracao=PluginAuditoriaAdministrativa.ALTERACAO_PLUGIN_CRIADO,
            campo="descricao",
            valor_anterior=None,
            valor_novo=plugin.descricao,
        )


def _auditar_criacao_capability(*, ator_user_id: int, capability: PluginCapability) -> None:
    pares = (
        ("chave", capability.chave),
        ("nome", capability.nome),
        ("natureza", capability.natureza),
        ("politica_maxima", capability.politica_maxima),
        ("status", capability.status),
    )
    for campo, novo in pares:
        _auditar(
            ator_user_id=ator_user_id,
            plugin_id=capability.plugin_id,
            capability_id=capability.id,
            entidade=PluginAuditoriaAdministrativa.ENTIDADE_CAPABILITY,
            alteracao=PluginAuditoriaAdministrativa.ALTERACAO_CAPABILITY_CRIADA,
            campo=campo,
            valor_anterior=None,
            valor_novo=novo,
        )
    if capability.descricao:
        _auditar(
            ator_user_id=ator_user_id,
            plugin_id=capability.plugin_id,
            capability_id=capability.id,
            entidade=PluginAuditoriaAdministrativa.ENTIDADE_CAPABILITY,
            alteracao=PluginAuditoriaAdministrativa.ALTERACAO_CAPABILITY_CRIADA,
            campo="descricao",
            valor_anterior=None,
            valor_novo=capability.descricao,
        )


def _auditar(
    *,
    ator_user_id: int,
    plugin_id: int,
    entidade: str,
    alteracao: str,
    campo: str,
    valor_anterior,
    valor_novo,
    capability_id: int | None = None,
) -> None:
    if entidade not in PluginAuditoriaAdministrativa.ENTIDADES:
        raise PluginInvalidoError("Entidade de auditoria inválida.")
    if alteracao not in PluginAuditoriaAdministrativa.ALTERACOES:
        raise PluginInvalidoError("Alteração de auditoria inválida.")
    if campo not in PluginAuditoriaAdministrativa.CAMPOS:
        raise PluginInvalidoError("Campo de auditoria inválido.")
    anterior = _valor_auditoria(valor_anterior)
    novo = _valor_auditoria(valor_novo)
    if anterior == novo:
        return
    if entidade == PluginAuditoriaAdministrativa.ENTIDADE_PLUGIN:
        capability_id = None
    elif capability_id is None:
        raise PluginInvalidoError("Auditoria de capability sem capability.")
    db.session.add(
        PluginAuditoriaAdministrativa(
            entidade=entidade,
            alteracao=alteracao,
            campo=campo,
            valor_anterior=anterior,
            valor_novo=novo,
            plugin_id=int(plugin_id),
            capability_id=int(capability_id) if capability_id is not None else None,
            ator_user_id=int(ator_user_id),
            created_at=utcnow_naive(),
        )
    )


def _valor_auditoria(valor) -> str | None:
    if valor is None:
        return None
    texto = str(valor).strip()
    if not texto:
        return None
    if len(texto) > _VALOR_AUDITORIA_MAX:
        raise PluginInvalidoError("Valor de auditoria excede o limite.")
    _recusar_material_sensivel(texto, "auditoria")
    candidato = texto.strip()
    if candidato.startswith("{") or candidato.startswith("["):
        try:
            parsed = json.loads(candidato)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, (dict, list)):
            raise MaterialSensivelRecusadoError(
                "Auditoria administrativa não aceita payload."
            )
    return texto
