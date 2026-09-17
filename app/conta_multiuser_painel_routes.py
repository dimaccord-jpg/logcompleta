"""Rotas da Fase 5 — painel do Contratante e aumento automático de assentos."""
from __future__ import annotations

import logging
import uuid

from flask import Blueprint, jsonify, redirect, render_template, request
from flask_login import current_user, login_required

from app.extensions import db
from app.models import User
from app.services.conta_multiuser_aumento_service import (
    calcular_preview_aumento,
    confirmar_aumento_assentos,
    formatar_brl,
    formatar_data_br,
    gerar_csrf_token_aumento,
    montar_painel_contratante,
    painel_para_template,
    preview_para_api,
    resultado_para_api,
    user_eh_contratante_ativo,
    validar_csrf_token_aumento,
)
from app.services.conta_multiuser_convite_service import (
    gerar_csrf_token_gestao_convite,
)
from app.services.conta_multiuser_errors import (
    AumentoMultiuserCicloIndeterminadoError,
    AumentoMultiuserDivergenteError,
    AumentoMultiuserInvalidoError,
    GestaoMultiuserNaoAutorizadaError,
    NotificacaoInternaNaoAutorizadaError,
    ReducaoMultiuserConflitoError,
    ReducaoMultiuserInvalidaError,
    RevogacaoMultiuserInvalidaError,
    RevogacaoMultiuserNaoAutorizadaError,
)

logger = logging.getLogger(__name__)

painel_bp = Blueprint("multiuser_painel", __name__)


def _ator_atual() -> User:
    return current_user._get_current_object()


def _json_erro(status: int, codigo: str, mensagem: str):
    return jsonify({"ok": False, "codigo": codigo, "mensagem": mensagem}), status


@painel_bp.route("/gestao-multiuser", methods=["GET"])
@login_required
def gestao_multiuser():
    ator = _ator_atual()
    try:
        painel = montar_painel_contratante(ator)
    except GestaoMultiuserNaoAutorizadaError:
        return render_template(
            "gestao_multiuser.html",
            autorizado=False,
            painel=None,
            csrf_token=None,
            csrf_token_convite=None,
            idempotency_key=None,
        ), 403
    except AumentoMultiuserInvalidoError as exc:
        return render_template(
            "gestao_multiuser.html",
            autorizado=True,
            painel=None,
            erro=str(exc),
            csrf_token=gerar_csrf_token_aumento(int(ator.id)),
            csrf_token_convite=gerar_csrf_token_gestao_convite(int(ator.id)),
            idempotency_key=uuid.uuid4().hex,
        ), 400
    from app.services.conta_multiuser_aumento_excepcional_service import (
        listar_para_contratante,
    )
    from app.services.conta_multiuser_reducao_service import snapshot_reducao_para_painel

    painel_view = painel_para_template(painel)
    painel_view["reducao"] = snapshot_reducao_para_painel(int(painel.conta_id))

    return render_template(
        "gestao_multiuser.html",
        autorizado=True,
        painel=painel_view,
        solicitacoes_excepcionais=listar_para_contratante(int(painel.conta_id)),
        csrf_token=gerar_csrf_token_aumento(int(ator.id)),
        csrf_token_convite=gerar_csrf_token_gestao_convite(int(ator.id)),
        idempotency_key=uuid.uuid4().hex,
        formatar_brl=formatar_brl,
        formatar_data_br=formatar_data_br,
    )


@painel_bp.route("/api/multiuser/aumento/preview", methods=["POST"])
@login_required
def api_preview_aumento():
    ator = _ator_atual()
    payload = request.get_json(silent=True) or {}
    csrf_token = (payload.get("csrf_token") or request.form.get("csrf_token") or "").strip()
    if not validar_csrf_token_aumento(csrf_token, int(ator.id)):
        return _json_erro(403, "csrf_invalido", "Não foi possível validar a solicitação.")
    try:
        preview = calcular_preview_aumento(
            ator,
            payload.get("quantidade") or payload.get("quantidade_adicional"),
            commit=False,
        )
        db.session.rollback()
    except GestaoMultiuserNaoAutorizadaError as exc:
        db.session.rollback()
        return _json_erro(403, "nao_autorizado", str(exc))
    except (
        AumentoMultiuserInvalidoError,
        AumentoMultiuserCicloIndeterminadoError,
        AumentoMultiuserDivergenteError,
    ) as exc:
        db.session.rollback()
        return _json_erro(400, "dados_invalidos", str(exc))
    except Exception:
        db.session.rollback()
        logger.exception("Falha no preview de aumento Multiuser user_id=%s", ator.id)
        return _json_erro(500, "falha_preview", "Não foi possível calcular o preview.")
    return jsonify({"ok": True, **preview_para_api(preview)})


@painel_bp.route("/api/multiuser/aumento/confirmar", methods=["POST"])
@login_required
def api_confirmar_aumento():
    ator = _ator_atual()
    payload = request.get_json(silent=True) or {}
    csrf_token = (payload.get("csrf_token") or request.form.get("csrf_token") or "").strip()
    if not validar_csrf_token_aumento(csrf_token, int(ator.id)):
        return _json_erro(403, "csrf_invalido", "Não foi possível validar a solicitação.")
    try:
        resultado = confirmar_aumento_assentos(
            ator,
            payload.get("quantidade") or payload.get("quantidade_adicional"),
            idempotency_key=payload.get("idempotency_key"),
            commit=True,
        )
    except GestaoMultiuserNaoAutorizadaError as exc:
        db.session.rollback()
        return _json_erro(403, "nao_autorizado", str(exc))
    except (
        AumentoMultiuserInvalidoError,
        AumentoMultiuserCicloIndeterminadoError,
        AumentoMultiuserDivergenteError,
    ) as exc:
        db.session.rollback()
        return _json_erro(409, "aumento_bloqueado", str(exc))
    except Exception:
        db.session.rollback()
        logger.exception("Falha ao confirmar aumento Multiuser user_id=%s", ator.id)
        return _json_erro(500, "falha_aumento", "Não foi possível concluir o aumento.")
    return jsonify({"ok": True, **resultado_para_api(resultado)})


@painel_bp.route(
    "/gestao-multiuser/aumento-excepcional/<int:excepcional_id>/pagar",
    methods=["GET"],
)
@login_required
def pagar_aumento_excepcional(excepcional_id):
    from app.services.conta_multiuser_aumento_excepcional_service import (
        url_pagamento_contratante,
    )
    from app.services.conta_multiuser_errors import AumentoExcepcionalInvalidoError

    try:
        url = url_pagamento_contratante(
            user=_ator_atual(),
            excepcional_id=int(excepcional_id),
        )
    except GestaoMultiuserNaoAutorizadaError as exc:
        return _json_erro(403, "nao_autorizado", str(exc))
    except AumentoExcepcionalInvalidoError as exc:
        return _json_erro(400, "pagamento_indisponivel", str(exc))
    return redirect(url)


@painel_bp.route("/api/multiuser/membros/<int:user_id>/revogar", methods=["POST"])
@login_required
def api_revogar_membro(user_id):
    ator = _ator_atual()
    payload = request.get_json(silent=True) or {}
    csrf_token = (payload.get("csrf_token") or request.form.get("csrf_token") or "").strip()
    if not validar_csrf_token_aumento(csrf_token, int(ator.id)):
        return _json_erro(403, "csrf_invalido", "Não foi possível validar a solicitação.")
    from app.services.conta_multiuser_revogacao_service import revogar_membro

    try:
        resultado = revogar_membro(ator=ator, alvo_user_id=int(user_id), commit=True)
    except RevogacaoMultiuserNaoAutorizadaError as exc:
        db.session.rollback()
        return _json_erro(403, "nao_autorizado", str(exc))
    except RevogacaoMultiuserInvalidaError as exc:
        db.session.rollback()
        return _json_erro(400, "revogacao_invalida", str(exc))
    except Exception:
        db.session.rollback()
        logger.exception("Falha ao revogar membro Multiuser ator=%s alvo=%s", ator.id, user_id)
        return _json_erro(500, "falha_revogacao", "Não foi possível revogar o membro.")
    return jsonify(
        {
            "ok": True,
            "replay": resultado.replay,
            "user_id": resultado.user_id,
            "quantity": resultado.quantity,
            "capacidade_livre": resultado.capacidade_livre,
        }
    )


@painel_bp.route("/api/multiuser/reducao/solicitar", methods=["POST"])
@login_required
def api_solicitar_reducao():
    ator = _ator_atual()
    payload = request.get_json(silent=True) or {}
    csrf_token = (payload.get("csrf_token") or request.form.get("csrf_token") or "").strip()
    if not validar_csrf_token_aumento(csrf_token, int(ator.id)):
        return _json_erro(403, "csrf_invalido", "Não foi possível validar a solicitação.")
    from app.services.conta_multiuser_reducao_service import solicitar_reducao_quantity

    try:
        resultado = solicitar_reducao_quantity(
            ator=ator,
            quantity_futura=payload.get("quantity_futura") or payload.get("quantidade"),
            idempotency_key=payload.get("idempotency_key"),
            commit=True,
        )
    except GestaoMultiuserNaoAutorizadaError as exc:
        db.session.rollback()
        return _json_erro(403, "nao_autorizado", str(exc))
    except ReducaoMultiuserInvalidaError as exc:
        db.session.rollback()
        return _json_erro(400, "reducao_invalida", str(exc))
    except ReducaoMultiuserConflitoError as exc:
        db.session.rollback()
        return _json_erro(409, "reducao_conflito", str(exc))
    except Exception:
        db.session.rollback()
        logger.exception("Falha ao solicitar redução Multiuser user_id=%s", ator.id)
        return _json_erro(500, "falha_reducao", "Não foi possível registrar a redução.")
    return jsonify(
        {
            "ok": True,
            "estado": resultado.estado,
            "replay": resultado.replay,
            "quantity_atual": resultado.quantity_atual,
            "quantity_futura": resultado.quantity_futura,
            "efetivar_em": resultado.efetivar_em,
            "mensagem": resultado.mensagem,
        }
    )


@painel_bp.route("/api/notificacoes-internas", methods=["GET"])
@login_required
def api_listar_notificacoes():
    from app.services.conta_multiuser_notificacao_service import (
        contar_nao_lidas,
        listar_notificacoes_do_user,
        notificacao_para_api,
    )

    ator = _ator_atual()
    itens = [notificacao_para_api(n) for n in listar_notificacoes_do_user(ator)]
    return jsonify(
        {
            "ok": True,
            "nao_lidas": contar_nao_lidas(ator),
            "notificacoes": itens,
        }
    )


@painel_bp.route("/api/notificacoes-internas/<int:notificacao_id>/lida", methods=["POST"])
@login_required
def api_marcar_notificacao_lida(notificacao_id):
    from app.services.conta_multiuser_notificacao_service import marcar_como_lida

    try:
        marcar_como_lida(_ator_atual(), int(notificacao_id), commit=True)
    except NotificacaoInternaNaoAutorizadaError:
        db.session.rollback()
        return _json_erro(404, "nao_encontrada", "Notificação não encontrada.")
    return jsonify({"ok": True})


def contratante_pode_ver_atalho(user: User | None) -> bool:
    try:
        return user_eh_contratante_ativo(user)
    except Exception:
        return False
