"""
Fase 2 — identidade de negócio para rastreio de consumo (sem billing nem limites).

Modelagem:
- Conta = raiz contratual (tabela `conta`); Franquia = unidade operacional (`franquia`).
- Usuário autenticado: `conta_id` / `franquia_id` vêm de `user` (nunca `user.id` como pseudo-conta).
- Sistema/cron/CLI: franquia reservada `sistema-interno` / `operacional-interno` via `get_sistema_interno_ids()`.

O fallback `contexto_indefinido` é exceção operacional (log), não fluxo nominal.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

TIPO_ORIGEM_HTTP_USUARIO = "http_usuario"
TIPO_ORIGEM_HTTP_ANONIMO = "http_anonimo"
TIPO_ORIGEM_HTTP_CRON = "http_cron"
TIPO_ORIGEM_BACKGROUND_THREAD = "background_thread"
TIPO_ORIGEM_CLI = "cli"
TIPO_ORIGEM_CONTEXTO_INDEFINIDO = "contexto_indefinido"


def identidade_sistema_interna(tipo_origem: str) -> dict[str, Any]:
    """Consumo interno/sistema: conta/franquia reais reservadas para operações sem operador humano."""
    from app.services.conta_franquia_service import get_sistema_interno_ids

    cid, fid = get_sistema_interno_ids()
    return {
        "conta_id": cid,
        "franquia_id": fid,
        "usuario_id": None,
        "tipo_origem": tipo_origem,
        "origem_sistema": True,
    }


def identidade_http_anonimo() -> dict[str, Any]:
    """Request HTTP sem sessão; não inventa conta/franquia."""
    return {
        "conta_id": None,
        "franquia_id": None,
        "usuario_id": None,
        "tipo_origem": TIPO_ORIGEM_HTTP_ANONIMO,
        "origem_sistema": False,
    }


def identidade_de_usuario(user: Any, tipo_origem: str) -> dict[str, Any]:
    """Operador autenticado: IDs reais de negócio a partir do vínculo User → Conta / Franquia."""
    from app.extensions import db
    from app.services.conta_franquia_service import ensure_user_vinculo_conta_franquia

    ensure_user_vinculo_conta_franquia(user)
    try:
        db.session.refresh(user)
    except Exception:
        pass
    uid = getattr(user, "id", None)
    try:
        uid_int = int(uid) if uid is not None else None
    except (TypeError, ValueError):
        uid_int = None
    cid = getattr(user, "conta_id", None)
    fid = getattr(user, "franquia_id", None)
    try:
        cid_i = int(cid) if cid is not None else None
    except (TypeError, ValueError):
        cid_i = None
    try:
        fid_i = int(fid) if fid is not None else None
    except (TypeError, ValueError):
        fid_i = None
    return {
        "conta_id": cid_i,
        "franquia_id": fid_i,
        "usuario_id": uid_int,
        "tipo_origem": tipo_origem,
        "origem_sistema": False,
    }


def normalize_identidade(data: dict[str, Any] | None) -> dict[str, Any]:
    """Garante chaves mínimas para persistência e leitura (merge neutro; sem herdar conta sistema por engano)."""
    if not data:
        return identidade_sistema_interna(TIPO_ORIGEM_CONTEXTO_INDEFINIDO)
    out: dict[str, Any] = {
        "conta_id": None,
        "franquia_id": None,
        "usuario_id": None,
        "tipo_origem": TIPO_ORIGEM_CONTEXTO_INDEFINIDO,
        "origem_sistema": False,
    }
    for k in ("conta_id", "franquia_id", "usuario_id"):
        if k in data and data[k] is not None:
            try:
                out[k] = int(data[k])
            except (TypeError, ValueError):
                out[k] = None
    if data.get("tipo_origem"):
        out["tipo_origem"] = str(data["tipo_origem"])[:80]
    if "origem_sistema" in data:
        out["origem_sistema"] = bool(data["origem_sistema"])
    return out


def set_consumo_identidade(data: dict[str, Any] | None) -> None:
    from flask import g

    g.identidade = normalize_identidade(data)


def get_consumo_identidade() -> dict[str, Any] | None:
    from flask import g

    raw = getattr(g, "identidade", None)
    if raw is None:
        return None
    if isinstance(raw, dict):
        return normalize_identidade(raw)
    return None


def clear_consumo_identidade() -> None:
    from flask import g

    if hasattr(g, "identidade"):
        delattr(g, "identidade")


def ensure_consumo_identidade_no_app_context(explicit_override: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Dentro de `app.app_context()`. Prioridade: override explícito (threads) > request > CLI/sistema.
    """
    from flask import g, has_request_context, request
    from flask_login import current_user

    if explicit_override is not None:
        ident = normalize_identidade(explicit_override)
        g.identidade = ident
        return ident

    if has_request_context():
        path = request.path or ""
        if path.startswith("/cron/"):
            ident = identidade_sistema_interna(TIPO_ORIGEM_HTTP_CRON)
            g.identidade = ident
            return ident
        if getattr(current_user, "is_authenticated", False):
            ident = identidade_de_usuario(current_user, TIPO_ORIGEM_HTTP_USUARIO)
            g.identidade = ident
            return ident
        ident = identidade_http_anonimo()
        g.identidade = ident
        return ident

    ident = identidade_sistema_interna(TIPO_ORIGEM_CLI)
    g.identidade = ident
    return ident


def apply_consumo_identidade_before_request() -> None:
    """Injeta `g.identidade` no request HTTP (exceto static)."""
    from flask import g, has_request_context, request
    from flask_login import current_user

    if not has_request_context():
        return
    if request.endpoint == "static":
        return
    path = request.path or ""
    if path.startswith("/cron/"):
        g.identidade = identidade_sistema_interna(TIPO_ORIGEM_HTTP_CRON)
        return
    if getattr(current_user, "is_authenticated", False):
        g.identidade = identidade_de_usuario(current_user, TIPO_ORIGEM_HTTP_USUARIO)
        return
    g.identidade = identidade_http_anonimo()


def capture_consumo_identidade_for_background() -> dict[str, Any]:
    """Captura identidade no thread da requisição para reidratar no worker."""
    from flask import has_request_context
    from flask_login import current_user

    if not has_request_context():
        return identidade_sistema_interna(TIPO_ORIGEM_BACKGROUND_THREAD)
    if getattr(current_user, "is_authenticated", False):
        d = identidade_de_usuario(current_user, TIPO_ORIGEM_BACKGROUND_THREAD)
        return normalize_identidade(d)
    return identidade_sistema_interna(TIPO_ORIGEM_BACKGROUND_THREAD)


def resolve_identidade_para_persistencia() -> dict[str, Any]:
    """
    Registradores de evento: preferir `g.identidade`.
    Se ausente, tenta franquia sistema (marca exceção); último recurso: tipo contexto_indefinido + log crítico.
    """
    from app.services.conta_franquia_service import get_sistema_interno_ids

    ident = get_consumo_identidade()
    if ident is not None:
        return ident
    cid, fid = get_sistema_interno_ids()
    if cid is not None and fid is not None:
        logger.error(
            "Consumo identidade: g.identidade ausente no registrador; "
            "aplicando franquia sistema interna (exceção — revisar ensure/before_request)."
        )
        return {
            "conta_id": cid,
            "franquia_id": fid,
            "usuario_id": None,
            "tipo_origem": TIPO_ORIGEM_CONTEXTO_INDEFINIDO,
            "origem_sistema": True,
        }
    logger.critical(
        "Consumo identidade: g.identidade ausente e tabela conta/franquia sistema não encontrada; "
        "persistência pode ficar sem vínculo de negócio."
    )
    return normalize_identidade(None)
