"""
Serviços de leitura para Política de Privacidade: política vigente, diretório de PDFs e validações básicas.
Mantém este domínio desacoplado dos Termos de Uso.
"""
import logging
import os

from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models import PrivacyPolicy


logger = logging.getLogger(__name__)

PRIVACY_POLICY_STATIC_SUBDIR = "privacy_policies"


def get_privacy_policy_upload_dir(app=None):
    """Retorna o diretório absoluto para armazenar PDFs da política de privacidade."""
    if app is None:
        from flask import current_app

        app = current_app
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "app", "static", PRIVACY_POLICY_STATIC_SUBDIR)


def ensure_privacy_policy_dir_exists(app=None):
    """Garante que o diretório app/static/privacy_policies/ exista."""
    privacy_dir = get_privacy_policy_upload_dir(app)
    os.makedirs(privacy_dir, exist_ok=True)
    return privacy_dir


def _privacy_policy_file_exists(filename: str | None) -> bool:
    """Valida se o PDF da política existe fisicamente em app/static/privacy_policies/."""
    nome = (filename or "").strip()
    if not nome:
        return False
    privacy_dir = os.path.abspath(get_privacy_policy_upload_dir())
    absolute_path = os.path.abspath(os.path.join(privacy_dir, nome))
    if os.path.commonpath([privacy_dir, absolute_path]) != privacy_dir:
        return False
    return os.path.isfile(absolute_path)


def get_active_privacy_policy():
    """Retorna a política ativa (is_active=True) ou None."""
    try:
        active_policy = (
            PrivacyPolicy.query.filter_by(is_active=True)
            .order_by(PrivacyPolicy.upload_date.desc())
            .first()
        )
        if not active_policy:
            return None
        if not _privacy_policy_file_exists(active_policy.filename):
            logger.warning(
                "Política ativa inconsistente no banco (arquivo ausente): %s",
                active_policy.filename,
            )
            return None
        return active_policy
    except SQLAlchemyError as exc:
        logger.exception("Falha ao consultar política ativa: %s", exc)
        db.session.rollback()
        return None
