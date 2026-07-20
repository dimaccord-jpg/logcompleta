"""Página pública do AgenteCompara (marco zero)."""
from __future__ import annotations

from flask import Blueprint, current_app, render_template, url_for

agente_compara_bp = Blueprint("agente_compara", __name__)


@agente_compara_bp.route("/agente-compara", methods=["GET"])
def agente_compara_page():
    return render_template(
        "agente_compara.html",
        agente_compara_avatar_url=url_for("static", filename="img/cleide-avatar.png"),
        cleide_bi_href=url_for("cleide.auditoria_frete"),
        cleide_feed_href=url_for("feed") if "feed" in current_app.view_functions else "/feed",
    )
