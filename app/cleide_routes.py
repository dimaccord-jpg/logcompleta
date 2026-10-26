from __future__ import annotations

from pathlib import Path

from flask import Blueprint, current_app, jsonify, render_template, request, send_file, session, url_for
from flask_login import current_user

from app.services.cleiton_operacao_autorizacao_service import (
    avaliar_autorizacao_operacao_por_franquia,
)
# Mantidos para o monkeypatch dos testes dos endpoints legados. O GET do AgenteBI não os chama.
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
_CHAT_HISTORY_MAX_MESSAGES = 10
_CHAT_HISTORY_MAX_CHARS = 900


def _authorize_cleide_upload_api():
    if not current_user.is_authenticated:
        return jsonify({"success": False, "error": "Autenticacao necessaria."}), 401
    autorizacao = avaliar_autorizacao_operacao_por_franquia(current_user)
    if not autorizacao.get("permitido"):
        return jsonify({"success": False, "error": "Operacao nao permitida para a franquia."}), 403
    return None


_AGENTE_BI_MENU = (
    ("inicio", "Home"),
    ("consultar_agentefrete", "Consultar o AgenteFrete"),
    ("auditar_cobrancas", "Auditar cobranças de frete"),
    ("comparar_tabelas", "Comparar tabelas"),
    ("feed", "Feed"),
)


def _agente_bi_menu_links() -> list[dict[str, str]]:
    """Links do menu + a partir da navegação oficial, com a mesma regra de login."""
    from app.shell_navigation import build_shell_navigation

    authenticated = bool(getattr(current_user, "is_authenticated", False))
    navigation = build_shell_navigation(
        authenticated=authenticated,
        request_path=request.path or "/",
        url_for=url_for,
        has_endpoint=lambda endpoint_name: endpoint_name in current_app.view_functions,
    )
    by_id = {
        item["id"]: item
        for section in navigation["sections"]
        for item in section["links"]
    }
    links: list[dict[str, str]] = []
    for entry_id, label in _AGENTE_BI_MENU:
        item = by_id[entry_id]
        links.append({
            "id": entry_id,
            "label": label,
            "icon": item["icon"],
            "href": item["href"],
        })
    return links


@cleide_bp.route("/cleide-bi-frete", methods=["GET"])
def auditoria_frete():
    is_authenticated = bool(getattr(current_user, "is_authenticated", False))
    if is_authenticated:
        autorizacao = avaliar_autorizacao_operacao_por_franquia(current_user)
        if not autorizacao.get("permitido"):
            return render_template(
                "feature_under_construction.html",
                origem="BI Cleide",
                mensagem_bloqueio=autorizacao.get("mensagem_usuario"),
            ), 403

    descoberta = False
    catalogo = None
    limite = 6
    dashboard = False
    if is_authenticated:
        from app.agente_bi_flags import agente_bi_api_habilitada, agente_bi_ingestao_habilitada

        if agente_bi_api_habilitada():
            dashboard = True
            if agente_bi_ingestao_habilitada():
                from app.services.agente_bi_analisar_service import descrever_catalogo
                from app.services.agente_bi_persistencia_service import LIMITE_GRAFICOS

                descoberta = True
                catalogo = descrever_catalogo()
                limite = LIMITE_GRAFICOS

    return render_template(
        "agente_bi.html",
        agente_bi_menu=_agente_bi_menu_links(),
        agente_bi_descoberta=descoberta,
        agente_bi_catalogo=catalogo,
        agente_bi_limite_graficos=limite,
        agente_bi_dashboard=dashboard,
    )


@cleide_bp.route("/auditoria-frete", methods=["GET"])
def cleide_auditoria():
    """Superfície visual da nova Auditoria da Cleide (sem IA/upload real nesta fase)."""
    return render_template(
        "cleide_auditoria.html",
        cleide_avatar_url=url_for("static", filename="img/cleide-avatar.png"),
        cleide_bi_href=url_for("cleide.auditoria_frete"),
        cleide_feed_href=url_for("feed") if "feed" in current_app.view_functions else "/feed",
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
