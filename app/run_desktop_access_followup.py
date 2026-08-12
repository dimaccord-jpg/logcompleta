"""
Runner periódico da jornada desktop_access (Etapa 4).

Ordem obrigatória:
1) reconciliação Lead → User
2) follow-up único para elegíveis

Não configura cron/scheduler externo; apenas fica pronto para execução periódica.
"""
from __future__ import annotations

import logging
import sys
from typing import Any

from flask import url_for

logger = logging.getLogger(__name__)


def _build_url_helpers():
    """URLs absolutas reutilizando as rotas já publicadas na Etapa 3."""

    def build_cta_url(token: str) -> str:
        return url_for("acesso_desktop_continuar", token=token, _external=True)

    def build_unsubscribe_url(token: str) -> str:
        return url_for("acesso_desktop_descadastrar", token=token, _external=True)

    return build_cta_url, build_unsubscribe_url


def executar_desktop_access_followup(app_flask) -> dict[str, Any]:
    """
    Orquestra reconciliação e follow-up dentro de app_context.

    Idempotente: seguro para múltiplas execuções no mesmo dia.
    Em dev/homolog também processa TestRuns E2E elegíveis (contadores separados).
    """
    from app.services.lead_campaign_conversion_service import (
        reconcile_desktop_access_leads,
    )
    from app.services.lead_campaign_email_service import process_eligible_followups
    from app.services import admin_desktop_access_test_service as desktop_e2e

    summary: dict[str, Any] = {
        "reconciliation": {},
        "followup": {},
        "e2e_followup": {},
    }

    with app_flask.app_context():
        recon = reconcile_desktop_access_leads()
        summary["reconciliation"] = recon

        build_cta_url, build_unsubscribe_url = _build_url_helpers()
        follow = process_eligible_followups(
            secret_key=app_flask.config["SECRET_KEY"],
            build_cta_url=build_cta_url,
            build_unsubscribe_url=build_unsubscribe_url,
        )
        summary["followup"] = follow

        e2e_follow: dict[str, Any] = {}
        if desktop_e2e.is_admin_test_env_allowed():
            e2e_follow = desktop_e2e.process_eligible_e2e_followups(
                secret_key=app_flask.config["SECRET_KEY"],
                build_cta_url=build_cta_url,
                build_unsubscribe_url=build_unsubscribe_url,
            )
        summary["e2e_followup"] = e2e_follow

        logger.info(
            "Runner desktop_access: recon_examined=%s recon_converted=%s "
            "follow_candidates=%s follow_sent=%s follow_suppressed_converted=%s "
            "follow_suppressed_opt_out=%s follow_failed=%s "
            "e2e_candidates=%s e2e_sent=%s e2e_failed=%s",
            recon.get("examined", 0),
            recon.get("converted", 0),
            follow.get("candidates", 0),
            follow.get("sent", 0),
            follow.get("skipped_converted", 0),
            follow.get("skipped_opt_out", 0),
            follow.get("failed", 0),
            e2e_follow.get("candidates", 0),
            e2e_follow.get("sent", 0),
            e2e_follow.get("failed", 0),
        )

    return summary


if __name__ == "__main__":
    from app.web import app

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | DESKTOP_ACCESS: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    logger.info("Iniciando runner desktop_access (reconciliação + follow-up)")
    result = executar_desktop_access_followup(app)
    logger.info("Runner desktop_access concluído: %s", result)
