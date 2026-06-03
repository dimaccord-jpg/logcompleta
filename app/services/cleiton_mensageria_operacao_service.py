"""
Central de mensageria operacional no domínio Cleiton.
Responsável apenas por formatar mensagens para UI sem alterar regras de autorização.
"""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

from flask import current_app, url_for

from app.models import Franquia
from app.services.plano_service import obter_nome_exibivel_plano

_STATUS_COM_CTA = {
    Franquia.STATUS_DEGRADED,
    Franquia.STATUS_BLOCKED,
    Franquia.STATUS_EXPIRED,
}


def status_exibe_cta_upgrade(status_franquia: str) -> bool:
    return status_franquia in _STATUS_COM_CTA

ERROR_CODE_PLAN_LIMIT_REACHED = "plan_limit_reached"
UPGRADE_LABEL_DEFAULT = "Faça o upgrade"
UPGRADE_PATH_DEFAULT = "/contrate-um-plano"


def montar_mensagem_limite_plano_texto(nome_plano: str) -> str:
    """Texto único para UI, sem markdown."""
    return (
        f"Você atingiu o limite de uso do plano {nome_plano}. "
        "Não pare agora! Faça o upgrade e continue criando sem interrupções."
    )


def montar_upgrade_cta_operacao(plano_resolvido: str | None) -> dict[str, Any]:
    """CTA estruturado para renderização segura no frontend."""
    plano_codigo = (plano_resolvido or "").strip().lower()
    nome_plano = obter_nome_exibivel_plano(plano_codigo)
    return {
        "error_code": ERROR_CODE_PLAN_LIMIT_REACHED,
        "message": (
            f"Você atingiu o limite de uso do plano {nome_plano}. "
            "Não pare agora! "
        ),
        "message_suffix": " e continue criando sem interrupções.",
        "upgrade_url": _upgrade_path_for_ui(),
        "upgrade_label": UPGRADE_LABEL_DEFAULT,
    }


def montar_mensagem_operacao(
    *,
    status_franquia: str,
    motivo: str,
    plano_resolvido: str | None,
    sugerir_upgrade: bool,
) -> str | None:
    """
    Retorna a mensagem final para o usuário sem recalcular status/autorização.
    """
    plano_codigo = (plano_resolvido or "").strip().lower()

    if status_franquia in _STATUS_COM_CTA:
        nome_plano = obter_nome_exibivel_plano(plano_codigo)
        return montar_mensagem_limite_plano_texto(nome_plano)

    if sugerir_upgrade:
        return _mensagem_legado(status_franquia=status_franquia, motivo=motivo)

    return _mensagem_legado(status_franquia=status_franquia, motivo=motivo)


def _mensagem_legado(*, status_franquia: str, motivo: str) -> str | None:
    if status_franquia == Franquia.STATUS_BLOCKED:
        if motivo == "bloqueio_manual":
            return "Sua franquia está temporariamente bloqueada por ação administrativa."
        return "Sua franquia está bloqueada para operação no momento."
    if status_franquia == Franquia.STATUS_EXPIRED:
        return "A vigência operacional da sua franquia expirou."
    if status_franquia == Franquia.STATUS_DEGRADED:
        return "Sua franquia está em modo degradado."
    return None


def _obter_url_upgrade_planos() -> str:
    default = UPGRADE_PATH_DEFAULT
    try:
        valor_cfg = current_app.config.get("PLANOS_UPGRADE_URL")
    except RuntimeError:
        valor_cfg = None
    if isinstance(valor_cfg, str) and valor_cfg.strip():
        return valor_cfg.strip()
    raw_env = (os.getenv("PLANOS_UPGRADE_URL") or "").strip()
    if raw_env:
        return raw_env
    try:
        return url_for("user.contrate_plano", _external=True)
    except Exception:
        return default


def _upgrade_path_for_ui() -> str:
    """Caminho relativo seguro para links na UI do produto."""
    raw = (_obter_url_upgrade_planos() or "").strip()
    if not raw:
        return UPGRADE_PATH_DEFAULT
    if raw.startswith("/") and not raw.startswith("//"):
        return raw.split("?", 1)[0] or UPGRADE_PATH_DEFAULT
    try:
        parsed = urlparse(raw)
        if parsed.path:
            return parsed.path
    except Exception:
        pass
    return UPGRADE_PATH_DEFAULT
