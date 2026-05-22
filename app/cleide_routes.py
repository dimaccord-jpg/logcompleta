from __future__ import annotations

from pathlib import Path

from flask import Blueprint, current_app, g, jsonify, render_template, request, send_file, session, url_for
from flask_login import current_user, login_required

from app.services.cleiton_operacao_autorizacao_service import (
    avaliar_autorizacao_operacao_por_franquia,
)
from app.services.cleide_config_service import get_cleide_config
from app.cleide_upload_store import get_cleide_upload_tmp_dir
from app.cleide_controlled_chat import run_cleide_controlled_chat
from app.cleide_upload_pipeline import (
    clear_cleide_upload,
    get_cleide_dashboard_filtered_analytics,
    get_cleide_upload_status,
    process_cleide_upload,
)

cleide_bp = Blueprint("cleide", __name__)
_CLEIDE_TEMPLATE_NAME = "template_cleide_auditoria_frete.xlsx"
_CHAT_HISTORY_MAX_MESSAGES = 6
_CHAT_HISTORY_MAX_CHARS = 300


def _authorize_cleide_upload_api():
    if not current_user.is_authenticated:
        return jsonify({"success": False, "error": "Autenticacao necessaria."}), 401
    autorizacao = avaliar_autorizacao_operacao_por_franquia(current_user)
    if not autorizacao.get("permitido"):
        return jsonify({"success": False, "error": "Operacao nao permitida para a franquia."}), 403
    return None


@cleide_bp.route("/auditoria-frete", methods=["GET"])
def auditoria_frete():
    is_authenticated = bool(getattr(current_user, "is_authenticated", False))
    autorizacao = None
    if is_authenticated:
        autorizacao = avaliar_autorizacao_operacao_por_franquia(current_user)
    if is_authenticated and not autorizacao.get("permitido"):
        return render_template(
            "feature_under_construction.html",
            origem="Auditoria de Frete",
            mensagem_bloqueio=autorizacao.get("mensagem_usuario"),
        ), 403

    g.cleide_allow_config_fallback = bool(getattr(current_app, "testing", False))
    cfg = get_cleide_config()
    upload_tmp_dir = get_cleide_upload_tmp_dir()
    return render_template(
        "cleide_auditoria_frete.html",
        cleide_cfg=cfg,
        cleide_upload_tmp_dir=upload_tmp_dir,
        cleide_public_page=True,
        cleide_is_authenticated=is_authenticated,
        cleide_login_url=url_for("login"),
        cleide_upload_authorization=autorizacao or {
            "permitido": False,
            "modo_operacao": "login_required",
            "mensagem_usuario": "Faca login para enviar planilhas e usar recursos privados da Cleide.",
        },
    )


@cleide_bp.route("/api/cleide/health", methods=["GET"])
def cleide_health():
    return jsonify(
        {
            "agent": "cleide",
            "namespace": "cleide",
            "status": "ready_local_phase_8_2_controlled_context",
        }
    )


@cleide_bp.route("/api/cleide/template", methods=["GET"])
def cleide_template_download():
    template_path = Path(current_app.root_path) / "protected_files" / "templates" / _CLEIDE_TEMPLATE_NAME
    if not template_path.exists() or not template_path.is_file():
        return "Arquivo de modelo indisponivel no momento.", 404
    return send_file(
        template_path,
        as_attachment=True,
        download_name=_CLEIDE_TEMPLATE_NAME,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@cleide_bp.route("/api/cleide/upload", methods=["POST"])
def cleide_upload():
    unauthorized = _authorize_cleide_upload_api()
    if unauthorized is not None:
        return unauthorized
    return process_cleide_upload()


@cleide_bp.route("/api/cleide/upload/status", methods=["GET"])
def cleide_upload_status():
    unauthorized = _authorize_cleide_upload_api()
    if unauthorized is not None:
        return unauthorized
    return get_cleide_upload_status()


@cleide_bp.route("/api/cleide/upload/clear", methods=["POST"])
def cleide_upload_clear():
    unauthorized = _authorize_cleide_upload_api()
    if unauthorized is not None:
        return unauthorized
    return clear_cleide_upload()


@cleide_bp.route("/api/cleide/dashboard/filter", methods=["POST"])
def cleide_dashboard_filter():
    unauthorized = _authorize_cleide_upload_api()
    if unauthorized is not None:
        return unauthorized
    payload = request.get_json(silent=True) or {}
    filters = payload.get("filters") if isinstance(payload, dict) else None
    return get_cleide_dashboard_filtered_analytics(filters if isinstance(filters, dict) else {})


@cleide_bp.route("/api/chat_cleide", methods=["POST"])
def chat_cleide():
    payload = request.get_json(silent=True) or {}
    unauthorized = _authorize_cleide_upload_api()
    if unauthorized is not None:
        return unauthorized
    question = payload.get("question")
    history = _sanitize_chat_history(payload.get("history"))
    body, status = run_cleide_controlled_chat(
        question=question,
        session_obj=session,
        history=history,
    )
    return jsonify(body), status


def _sanitize_chat_history(raw_history) -> list[dict[str, str]]:
    if not isinstance(raw_history, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw_history:
        if not isinstance(item, dict):
            continue
        role_raw = str(item.get("role") or "").strip().lower()
        role = "assistant" if role_raw in {"assistant", "model", "cleide"} else ("user" if role_raw == "user" else "")
        if not role:
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        out.append({"role": role, "content": content[:_CHAT_HISTORY_MAX_CHARS]})
    return out[-_CHAT_HISTORY_MAX_MESSAGES:]
