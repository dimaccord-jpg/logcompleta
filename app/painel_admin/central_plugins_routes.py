"""Rotas administrativas da Central de Plugins.

O acesso usa verificar_acesso_admin (User.is_admin é True). O CSRF segue
o serializer temporário já usado nas mutações sensíveis do painel, com
sal próprio deste formulário. Payload JSON arbitrário não é aplicado.
"""
from __future__ import annotations

import logging

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.extensions import db
from app.models import Plugin
from app.services.central_plugin_admin_service import (
    GovernancaPluginNaoAutorizadaError,
    cadastrar_capability_admin,
    cadastrar_plugin_admin,
    definir_status_catalogo_admin,
    definir_titularidade_catalogo_admin,
    editar_metadados_admin,
    gerar_csrf_token_admin_plugins,
    governar_capability_admin,
    listar_plugins_admin,
    montar_detalhe_admin,
    opcoes_catalogo,
    validar_csrf_token_admin_plugins,
)
from app.services.central_plugin_service import CentralPluginError

logger = logging.getLogger(__name__)

_FRAGMENTOS_PROIBIDOS = (
    "token",
    "secret",
    "senha",
    "password",
    "cofre",
    "payload",
    "oauth",
    "webhook",
    "credencial",
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "refresh",
)
_VALORES_MARCADOS = {"1", "on", "true", "sim"}


def registrar_rotas_central_plugins(admin_bp) -> None:
    regras = (
        ("/central-plugins", "central_plugins_listar", central_plugins_listar, ["GET"]),
        ("/central-plugins/novo", "central_plugins_novo", central_plugins_novo, ["GET", "POST"]),
        (
            "/central-plugins/<int:plugin_id>",
            "central_plugins_detalhe",
            central_plugins_detalhe,
            ["GET"],
        ),
        (
            "/central-plugins/<int:plugin_id>/metadados",
            "central_plugins_metadados",
            central_plugins_metadados,
            ["POST"],
        ),
        (
            "/central-plugins/<int:plugin_id>/titularidade",
            "central_plugins_titularidade",
            central_plugins_titularidade,
            ["POST"],
        ),
        (
            "/central-plugins/<int:plugin_id>/status",
            "central_plugins_status",
            central_plugins_status,
            ["POST"],
        ),
        (
            "/central-plugins/<int:plugin_id>/capabilities",
            "central_plugins_capability_nova",
            central_plugins_capability_nova,
            ["POST"],
        ),
        (
            "/central-plugins/<int:plugin_id>/capabilities/<int:capability_id>",
            "central_plugins_capability_governanca",
            central_plugins_capability_governanca,
            ["POST"],
        ),
    )
    for regra, endpoint, view, methods in regras:
        admin_bp.add_url_rule(regra, endpoint=endpoint, view_func=view, methods=methods)


@login_required
def central_plugins_listar():
    negado = _negar_se_nao_admin()
    if negado is not None:
        return negado
    status = (request.args.get("status") or "").strip()
    if status and status not in Plugin.STATUSES:
        flash("Status de filtro inválido.", "warning")
        status = ""
    try:
        plugins = listar_plugins_admin(
            ator_user_id=int(current_user.id),
            status=status or None,
        )
    except GovernancaPluginNaoAutorizadaError:
        return "Acesso Negado", 403
    return render_template(
        "central_plugins.html",
        plugins=plugins,
        filtro_status=status,
        opcoes=opcoes_catalogo(),
    )


@login_required
def central_plugins_novo():
    negado = _negar_se_nao_admin()
    if negado is not None:
        return negado
    if request.method == "GET":
        return render_template(
            "central_plugin_form.html",
            opcoes=opcoes_catalogo(),
            csrf_token=_csrf_atual(),
            status_inicial=Plugin.STATUS_RASCUNHO,
        )
    destino = url_for("admin.central_plugins_novo")
    barrado = _barrar_csrf_e_forma(destino)
    if barrado is not None:
        return barrado
    try:
        plugin = cadastrar_plugin_admin(
            ator_user_id=int(current_user.id),
            slug=_texto("slug"),
            nome=_texto("nome"),
            descricao=_texto_opcional("descricao"),
            adapter_key=_texto("adapter_key"),
            status=_texto("status") or Plugin.STATUS_RASCUNHO,
            suporta_titularidade_pessoal=_marcado("suporta_titularidade_pessoal"),
            suporta_titularidade_corporativa=_marcado("suporta_titularidade_corporativa"),
        )
    except GovernancaPluginNaoAutorizadaError:
        return "Acesso Negado", 403
    except CentralPluginError as exc:
        flash(str(exc), "warning")
        return redirect(destino)
    except Exception:
        logger.exception("Falha ao cadastrar plugin homologado")
        flash("Não foi possível concluir a alteração.", "danger")
        return redirect(destino)
    flash("Plugin homologado cadastrado.", "success")
    return redirect(url_for("admin.central_plugins_detalhe", plugin_id=plugin.id))


@login_required
def central_plugins_detalhe(plugin_id: int):
    negado = _negar_se_nao_admin()
    if negado is not None:
        return negado
    try:
        detalhe = montar_detalhe_admin(
            ator_user_id=int(current_user.id),
            plugin_id=plugin_id,
        )
    except GovernancaPluginNaoAutorizadaError:
        return "Acesso Negado", 403
    if detalhe is None:
        abort(404)
    detalhe["csrf_token"] = _csrf_atual()
    return render_template("central_plugin_detalhe.html", **detalhe)


@login_required
def central_plugins_metadados(plugin_id: int):
    bloqueio = _exigir_mutacao_no_plugin(plugin_id)
    if bloqueio is not None:
        return bloqueio
    return _aplicar(
        _destino_detalhe(plugin_id),
        lambda: editar_metadados_admin(
            ator_user_id=int(current_user.id),
            plugin_id=plugin_id,
            nome=_texto("nome"),
            descricao=_texto_opcional("descricao"),
            adapter_key=_texto("adapter_key"),
        ),
        "Metadados do plugin atualizados.",
    )


@login_required
def central_plugins_titularidade(plugin_id: int):
    bloqueio = _exigir_mutacao_no_plugin(plugin_id)
    if bloqueio is not None:
        return bloqueio
    return _aplicar(
        _destino_detalhe(plugin_id),
        lambda: definir_titularidade_catalogo_admin(
            ator_user_id=int(current_user.id),
            plugin_id=plugin_id,
            suporta_titularidade_pessoal=_marcado("suporta_titularidade_pessoal"),
            suporta_titularidade_corporativa=_marcado("suporta_titularidade_corporativa"),
        ),
        "Titularidade do plugin atualizada.",
    )


@login_required
def central_plugins_status(plugin_id: int):
    bloqueio = _exigir_mutacao_no_plugin(plugin_id)
    if bloqueio is not None:
        return bloqueio
    return _aplicar(
        _destino_detalhe(plugin_id),
        lambda: definir_status_catalogo_admin(
            ator_user_id=int(current_user.id),
            plugin_id=plugin_id,
            status=_texto("status"),
        ),
        "Status do plugin atualizado.",
    )


@login_required
def central_plugins_capability_nova(plugin_id: int):
    bloqueio = _exigir_mutacao_no_plugin(plugin_id)
    if bloqueio is not None:
        return bloqueio
    return _aplicar(
        _destino_detalhe(plugin_id),
        lambda: cadastrar_capability_admin(
            ator_user_id=int(current_user.id),
            plugin_id=plugin_id,
            chave=_texto("chave"),
            nome=_texto("nome"),
            descricao=_texto_opcional("descricao"),
            natureza=_texto("natureza"),
            status=_texto("status") or Plugin.STATUS_DISPONIVEL,
            politica_maxima=_texto("politica_maxima"),
        ),
        "Capability cadastrada.",
    )


@login_required
def central_plugins_capability_governanca(plugin_id: int, capability_id: int):
    bloqueio = _exigir_mutacao_no_plugin(plugin_id)
    if bloqueio is not None:
        return bloqueio
    return _aplicar(
        _destino_detalhe(plugin_id),
        lambda: governar_capability_admin(
            ator_user_id=int(current_user.id),
            plugin_id=plugin_id,
            capability_id=capability_id,
            politica_maxima=_texto("politica_maxima"),
            status=_texto("status"),
        ),
        "Governança da capability atualizada.",
    )


def _negar_se_nao_admin():
    from app.painel_admin.admin_routes import verificar_acesso_admin

    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    return None


def _destino_detalhe(plugin_id: int) -> str:
    return url_for("admin.central_plugins_detalhe", plugin_id=plugin_id)


def _csrf_atual() -> str:
    return gerar_csrf_token_admin_plugins(int(current_user.id))


def _token_csrf_submetido() -> str:
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        if isinstance(payload, dict):
            return str(payload.get("csrf_token") or "").strip()
        return ""
    return (
        request.form.get("csrf_token")
        or request.headers.get("X-CSRF-Token")
        or ""
    ).strip()


def _formulario_proibido() -> bool:
    for chave in request.form.keys():
        normal = str(chave).casefold()
        if normal == "csrf_token":
            continue
        if any(fragmento in normal for fragmento in _FRAGMENTOS_PROIBIDOS):
            return True
    return False


def _barrar_csrf_e_forma(destino: str):
    if not validar_csrf_token_admin_plugins(_token_csrf_submetido(), int(current_user.id)):
        flash("Não foi possível validar a solicitação.", "danger")
        return redirect(destino)
    if request.is_json:
        flash("A governança administrativa não aceita payload JSON.", "danger")
        return redirect(destino)
    if _formulario_proibido():
        flash("A governança não aceita segredo, token ou payload.", "danger")
        return redirect(destino)
    return None


def _exigir_mutacao_no_plugin(plugin_id: int):
    negado = _negar_se_nao_admin()
    if negado is not None:
        return negado
    if db.session.get(Plugin, int(plugin_id)) is None:
        abort(404)
    return _barrar_csrf_e_forma(_destino_detalhe(plugin_id))


def _aplicar(destino: str, acao, mensagem_ok: str):
    try:
        resultado = acao()
    except GovernancaPluginNaoAutorizadaError:
        return "Acesso Negado", 403
    except CentralPluginError as exc:
        flash(str(exc), "warning")
        return redirect(destino)
    except Exception:
        logger.exception("Falha na governanca administrativa da Central de Plugins")
        flash("Não foi possível concluir a alteração.", "danger")
        return redirect(destino)
    if resultado:
        flash(mensagem_ok, "success")
    else:
        flash("Nenhuma alteração para salvar.", "info")
    return redirect(destino)


def _texto(nome: str) -> str:
    return (request.form.get(nome) or "").strip()


def _texto_opcional(nome: str) -> str | None:
    return _texto(nome) or None


def _marcado(nome: str) -> bool:
    return _texto(nome).lower() in _VALORES_MARCADOS
