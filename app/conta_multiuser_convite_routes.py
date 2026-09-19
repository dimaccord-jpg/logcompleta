"""Rotas da Fase 4 — convites Multiuser (criação autenticada e landing de aceite)."""
from __future__ import annotations

import logging

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.extensions import db
from app.models import User
from app.services.conta_multiuser_errors import (
    CapacidadeEsgotadaError,
    ConviteContratanteProprioError,
    ConviteContratoIncompativelError,
    ConviteEmailDivergenteError,
    ConviteExpiradoError,
    ConviteInvalidoError,
    ConviteJaVinculadoError,
    ConviteNaoAutorizadoError,
    VinculoInconsistenteError,
)
from app.services.conta_multiuser_convite_service import (
    aceitar_convite,
    criar_convite,
    montar_contexto_landing,
    reenviar_convite,
    validar_csrf_token_gestao_convite,
)

logger = logging.getLogger(__name__)

convite_bp = Blueprint("multiuser_convite", __name__)


def _ator_atual() -> User:
    return current_user._get_current_object()


def _build_invite_url(token: str) -> str:
    return url_for("multiuser_convite.visualizar_convite", token=token, _external=True)


def _json_erro(status: int, codigo: str, mensagem: str):
    return jsonify({"ok": False, "codigo": codigo, "mensagem": mensagem}), status


@convite_bp.route("/api/multiuser/convites", methods=["POST"])
@login_required
def api_criar_convite():
    ator = _ator_atual()
    payload = request.get_json(silent=True) or {}
    csrf_token = (payload.get("csrf_token") or request.form.get("csrf_token") or "").strip()
    if not validar_csrf_token_gestao_convite(csrf_token, int(ator.id)):
        return _json_erro(403, "csrf_invalido", "Não foi possível validar a solicitação.")
    email = payload.get("email") or request.form.get("email") or ""
    conta_id_payload = payload.get("conta_id")
    try:
        resultado = criar_convite(
            ator=ator,
            email_destino=email,
            conta_id_payload=conta_id_payload,
            build_invite_url=_build_invite_url,
            enviar=True,
            commit=True,
        )
    except ConviteNaoAutorizadoError as exc:
        db.session.rollback()
        return _json_erro(403, "nao_autorizado", str(exc))
    except ConviteContratanteProprioError as exc:
        db.session.rollback()
        return _json_erro(409, "contratante_proprio", str(exc))
    except ConviteJaVinculadoError as exc:
        return jsonify({"ok": True, "codigo": "ja_vinculado", "mensagem": str(exc)}), 200
    except CapacidadeEsgotadaError as exc:
        db.session.rollback()
        return _json_erro(409, "capacidade_indisponivel", str(exc))
    except ValueError as exc:
        db.session.rollback()
        return _json_erro(400, "dados_invalidos", str(exc))
    except Exception:
        db.session.rollback()
        logger.error("Falha ao criar convite Multiuser user_id=%s category=email_delivery", ator.id)
        return _json_erro(500, "falha_envio", "Não foi possível enviar o convite.")

    status = 200 if resultado.reutilizado else 201
    return (
        jsonify(
            {
                "ok": True,
                "convite_id": resultado.convite_id,
                "conta_id": resultado.conta_id,
                "franquia_id": resultado.franquia_id,
                "estado": resultado.estado,
                "reutilizado": resultado.reutilizado,
            }
        ),
        status,
    )


@convite_bp.route("/api/multiuser/convites/<int:convite_id>/reenviar", methods=["POST"])
@login_required
def api_reenviar_convite(convite_id: int):
    ator = _ator_atual()
    payload = request.get_json(silent=True) or {}
    csrf_token = (payload.get("csrf_token") or request.form.get("csrf_token") or "").strip()
    if not validar_csrf_token_gestao_convite(csrf_token, int(ator.id)):
        return _json_erro(403, "csrf_invalido", "Não foi possível validar a solicitação.")
    conta_id_payload = payload.get("conta_id")
    try:
        resultado = reenviar_convite(
            ator=ator,
            convite_id=convite_id,
            conta_id_payload=conta_id_payload,
            build_invite_url=_build_invite_url,
            enviar=True,
            commit=True,
        )
    except ConviteNaoAutorizadoError as exc:
        db.session.rollback()
        return _json_erro(403, "nao_autorizado", str(exc))
    except ConviteJaVinculadoError as exc:
        db.session.rollback()
        return _json_erro(409, "ja_aceito", str(exc))
    except ConviteInvalidoError as exc:
        db.session.rollback()
        return _json_erro(404, "convite_invalido", str(exc))
    except CapacidadeEsgotadaError as exc:
        db.session.rollback()
        return _json_erro(409, "capacidade_indisponivel", str(exc))
    except ValueError as exc:
        db.session.rollback()
        return _json_erro(400, "dados_invalidos", str(exc))
    except Exception:
        db.session.rollback()
        logger.error("Falha ao reenviar convite Multiuser convite_id=%s category=email_delivery", convite_id)
        return _json_erro(500, "falha_envio", "Não foi possível reenviar o convite.")

    return jsonify(
        {
            "ok": True,
            "convite_id": resultado.convite_id,
            "conta_id": resultado.conta_id,
            "franquia_id": resultado.franquia_id,
            "estado": resultado.estado,
            "reutilizado": resultado.reutilizado,
        }
    )


@convite_bp.route("/convite/<token>", methods=["GET"])
def visualizar_convite(token: str):
    user = current_user._get_current_object() if current_user.is_authenticated else None
    try:
        ctx = montar_contexto_landing(token, user)
    except (ConviteInvalidoError, ConviteExpiradoError):
        return render_template(
            "convite_multiuser.html",
            invalido=True,
            contexto=None,
            token=token,
        ), 404

    login_url = url_for("login", next=url_for("multiuser_convite.visualizar_convite", token=token))
    register_url = url_for(
        "login",
        mode="register",
        next=url_for("multiuser_convite.visualizar_convite", token=token),
    )
    return render_template(
        "convite_multiuser.html",
        invalido=False,
        contexto=ctx,
        token=token,
        login_url=login_url,
        register_url=register_url,
    )


@convite_bp.route("/convite/<token>/aceitar", methods=["POST"])
@login_required
def aceitar_convite_view(token: str):
    ator = _ator_atual()
    csrf_token = (request.form.get("csrf_token") or "").strip()
    landing = url_for("multiuser_convite.visualizar_convite", token=token)
    try:
        resultado = aceitar_convite(
            user=ator,
            token=token,
            csrf_token=csrf_token,
            commit=True,
        )
    except ConviteEmailDivergenteError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        return redirect(landing)
    except ConviteContratoIncompativelError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
        return redirect(landing)
    except ConviteContratanteProprioError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
        return redirect(landing)
    except ConviteJaVinculadoError as exc:
        db.session.rollback()
        flash(str(exc), "info")
        return redirect(url_for("index"))
    except (ConviteInvalidoError, ConviteExpiradoError) as exc:
        db.session.rollback()
        flash(str(exc), "warning")
        return redirect(landing)
    except CapacidadeEsgotadaError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
        return redirect(landing)
    except VinculoInconsistenteError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
        return redirect(landing)
    except Exception:
        db.session.rollback()
        logger.error("Falha no aceite de convite Multiuser user_id=%s category=invite_accept", ator.id)
        flash("Não foi possível aceitar o convite.", "danger")
        return redirect(landing)

    if resultado.ja_vinculado or resultado.idempotente:
        flash("Você já está vinculado a esta Conta.", "info")
    else:
        flash("Convite aceito. Seu acesso operacional passou a esta organização.", "success")
    return redirect(url_for("index"))
