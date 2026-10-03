"""Rotas autenticadas da Central de Plugins.

A superfície é do usuário do AgenteFrete, não do painel ADM. Toda mutação
exige sessão e CSRF no padrão da área autenticada. JSON não aplica a ação.
O corpo não escolhe a conexão: o serviço localiza a linha deste usuário.
"""
from __future__ import annotations

import logging

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.extensions import db
from app.services.central_plugin_service import CentralPluginError
from app.services.central_plugin_usuario_service import (
    AcessoConexaoUsuarioNegado,
    CentralPluginUsuarioError,
    PluginForaDoCatalogoUsuario,
    desconectar_conexao_usuario,
    gerar_csrf_token_plugins_usuario,
    iniciar_configuracao_usuario,
    mensagem_catalogo_vazio,
    montar_catalogo_usuario,
    remover_restricao_capability_usuario,
    restringir_capability_usuario,
    revalidar_conexao_usuario,
    validar_csrf_token_plugins_usuario,
)

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


def registrar_rotas_plugins_usuario(user_bp) -> None:
    regras = (
        ("/plugins", "plugins", plugins_listar, ["GET"]),
        ("/plugins/<slug>/conectar", "plugins_conectar", plugins_conectar, ["POST"]),
        ("/plugins/<slug>/desconectar", "plugins_desconectar", plugins_desconectar, ["POST"]),
        ("/plugins/<slug>/revalidar", "plugins_revalidar", plugins_revalidar, ["POST"]),
        (
            "/plugins/<slug>/capabilities/<chave>/restringir",
            "plugins_restringir",
            plugins_restringir,
            ["POST"],
        ),
        (
            "/plugins/<slug>/capabilities/<chave>/remover-restricao",
            "plugins_remover_restricao",
            plugins_remover_restricao,
            ["POST"],
        ),
    )
    for regra, endpoint, view, methods in regras:
        user_bp.add_url_rule(regra, endpoint=endpoint, view_func=view, methods=methods)


@login_required
def plugins_listar():
    return render_template(
        "plugins.html",
        plugins=montar_catalogo_usuario(int(current_user.id)),
        mensagem_vazio=mensagem_catalogo_vazio(),
        csrf_token=gerar_csrf_token_plugins_usuario(int(current_user.id)),
    )


@login_required
def plugins_conectar(slug: str):
    bloqueio = _barrar_mutacao()
    if bloqueio is not None:
        return bloqueio
    return _executar(lambda: iniciar_configuracao_usuario(user_id=int(current_user.id), slug=slug), "Configuração iniciada. Ela ainda não foi concluída.")


@login_required
def plugins_desconectar(slug: str):
    bloqueio = _barrar_mutacao()
    if bloqueio is not None:
        return bloqueio
    return _executar(
        lambda: desconectar_conexao_usuario(user_id=int(current_user.id), slug=slug),
        "Sua conexão foi desconectada. O histórico permanece.",
    )


@login_required
def plugins_revalidar(slug: str):
    bloqueio = _barrar_mutacao()
    if bloqueio is not None:
        return bloqueio

    def _acao():
        return revalidar_conexao_usuario(user_id=int(current_user.id), slug=slug)

    try:
        mensagem = _acao()
    except AcessoConexaoUsuarioNegado as exc:
        db.session.rollback()
        return exc.mensagem, 403
    except PluginForaDoCatalogoUsuario:
        db.session.rollback()
        abort(404)
    except CentralPluginUsuarioError as exc:
        db.session.rollback()
        flash(exc.mensagem, "warning")
        return redirect(url_for("user.plugins"))
    except CentralPluginError:
        db.session.rollback()
        flash("Não foi possível concluir.", "danger")
        return redirect(url_for("user.plugins"))
    except Exception:
        db.session.rollback()
        logger.exception("Falha na revalidacao da Central de Plugins do usuario")
        flash("Não foi possível concluir.", "danger")
        return redirect(url_for("user.plugins"))
    flash(mensagem, "info")
    return redirect(url_for("user.plugins"))


@login_required
def plugins_restringir(slug: str, chave: str):
    bloqueio = _barrar_mutacao()
    if bloqueio is not None:
        return bloqueio
    return _executar(
        lambda: restringir_capability_usuario(
            user_id=int(current_user.id),
            slug=slug,
            chave=chave,
        ),
        "A permissão foi restringida na sua conexão.",
    )


@login_required
def plugins_remover_restricao(slug: str, chave: str):
    bloqueio = _barrar_mutacao()
    if bloqueio is not None:
        return bloqueio
    return _executar(
        lambda: remover_restricao_capability_usuario(
            user_id=int(current_user.id),
            slug=slug,
            chave=chave,
        ),
        "A sua restrição foi removida.",
    )


def _barrar_mutacao():
    if request.is_json or (request.content_type or "").lower().startswith("application/json"):
        flash("Esta área não aceita payload JSON.", "danger")
        return redirect(url_for("user.plugins"))
    token = (request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or "").strip()
    if not validar_csrf_token_plugins_usuario(token, int(current_user.id)):
        flash("Não foi possível validar a solicitação.", "danger")
        return redirect(url_for("user.plugins"))
    if _formulario_proibido():
        flash("Não envie credencial, token ou segredo.", "danger")
        return redirect(url_for("user.plugins"))
    return None


def _formulario_proibido() -> bool:
    for chave in request.form.keys():
        normal = str(chave).casefold()
        if normal == "csrf_token":
            continue
        if any(fragmento in normal for fragmento in _FRAGMENTOS_PROIBIDOS):
            return True
    return False


def _executar(acao, mensagem_ok: str):
    try:
        acao()
    except AcessoConexaoUsuarioNegado as exc:
        db.session.rollback()
        return exc.mensagem, 403
    except PluginForaDoCatalogoUsuario:
        db.session.rollback()
        abort(404)
    except CentralPluginUsuarioError as exc:
        db.session.rollback()
        flash(exc.mensagem, "warning")
        return redirect(url_for("user.plugins"))
    except CentralPluginError:
        db.session.rollback()
        flash("Não foi possível concluir.", "danger")
        return redirect(url_for("user.plugins"))
    except Exception:
        db.session.rollback()
        logger.exception("Falha na Central de Plugins do usuario")
        flash("Não foi possível concluir.", "danger")
        return redirect(url_for("user.plugins"))
    flash(mensagem_ok, "success")
    return redirect(url_for("user.plugins"))
