"""
Central de mensageria operacional no domínio Cleiton.
Responsável apenas por formatar mensagens para UI sem alterar regras de autorização.
"""
from __future__ import annotations

import os

from flask import current_app, url_for

from app.models import Franquia
from app.services.plano_service import obter_nome_exibivel_plano

_STATUS_COM_CTA = {
    Franquia.STATUS_DEGRADED,
    Franquia.STATUS_BLOCKED,
    Franquia.STATUS_EXPIRED,
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
        url_planos = _obter_url_upgrade_planos()
        return (
            f"Você atingiu o limite de uso do plano {nome_plano}. "
            "Não pare agora! Faça o upgrade e continue criando sem interrupções: "
            f"[{url_planos}]({url_planos})"
        )

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
    default = "/contrate-um-plano"
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
