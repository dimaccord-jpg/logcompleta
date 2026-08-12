"""
Runner one-shot da ativação pós-cadastro desktop_access.

Registration → 24h → E-mail 1 → 48h → E-mail 2
Não configura scheduler externo.
"""
from __future__ import annotations

import logging
import sys
from typing import Any

from flask import url_for

logger = logging.getLogger(__name__)


def _build_url_helpers():
    def build_email_1_cta_url() -> str:
        return url_for("cleide.cleide_auditoria", _external=True)

    def build_email_2_cta_url() -> str:
        return url_for("agente_compara.agente_compara_page", _external=True)

    def build_unsubscribe_url(token: str) -> str:
        return url_for("acesso_desktop_descadastrar", token=token, _external=True)

    return build_email_1_cta_url, build_email_2_cta_url, build_unsubscribe_url


def executar_desktop_access_activation(app_flask) -> dict[str, Any]:
    """
    Processa candidatos a E-mail 1 e E-mail 2 com recheck pré-envio.

    Em dev/homolog também processa TestRuns E2E de ativação (contadores separados).
    """
    from app.services import desktop_access_activation_email_service as activation
    from app.services import admin_desktop_access_test_service as desktop_e2e

    summary: dict[str, Any] = {
        "activation": {},
        "e2e_activation": {},
    }

    with app_flask.app_context():
        build_e1, build_e2, build_unsub = _build_url_helpers()
        stats = activation.process_eligible_activation_emails(
            secret_key=app_flask.config["SECRET_KEY"],
            build_email_1_cta_url=build_e1,
            build_email_2_cta_url=build_e2,
            build_unsubscribe_url=build_unsub,
        )
        summary["activation"] = stats

        e2e_stats: dict[str, Any] = {}
        if desktop_e2e.is_admin_test_env_allowed():
            e2e_stats = desktop_e2e.process_eligible_e2e_activation_emails(
                secret_key=app_flask.config["SECRET_KEY"],
                build_email_1_cta_url=build_e1,
                build_email_2_cta_url=build_e2,
                build_unsubscribe_url=build_unsub,
            )
        summary["e2e_activation"] = e2e_stats

        logger.info(
            "Runner activation desktop_access: examined=%s email1_sent=%s email2_sent=%s "
            "suppressed_upload=%s suppressed_opt_out=%s failures=%s "
            "e2e_email1_sent=%s e2e_email2_sent=%s e2e_failures=%s",
            stats.get("examined", 0),
            stats.get("email1_sent", 0),
            stats.get("email2_sent", 0),
            stats.get("suppressed_upload", 0),
            stats.get("suppressed_opt_out", 0),
            stats.get("failures", 0),
            e2e_stats.get("email1_sent", 0),
            e2e_stats.get("email2_sent", 0),
            e2e_stats.get("failures", 0),
        )

    return summary


if __name__ == "__main__":
    from app.web import app

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | DESKTOP_ACTIVATION: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    logger.info("Iniciando runner desktop_access activation")
    result = executar_desktop_access_activation(app)
    logger.info("Runner desktop_access activation concluído: %s", result)
