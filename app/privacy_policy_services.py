"""
Serviços de leitura para Política de Privacidade: política vigente, diretório de PDFs e validações básicas.
Mantém este domínio desacoplado dos Termos de Uso.
"""
import logging

from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.legal_document_storage import (
    build_safe_storage_path,
    ensure_privacy_policies_storage_dir,
    get_privacy_policies_storage_dir,
)
from app.models import PrivacyPolicy


logger = logging.getLogger(__name__)


def get_privacy_policy_upload_dir(app=None):
    """Retorna o diretório absoluto para armazenar PDFs da política de privacidade."""
    _ = app  # compatibilidade de assinatura
    return str(get_privacy_policies_storage_dir())


def ensure_privacy_policy_dir_exists(app=None):
    """Garante que o diretório persistente de política de privacidade exista."""
    _ = app  # compatibilidade de assinatura
    return str(ensure_privacy_policies_storage_dir())


def _privacy_policy_file_exists(filename: str | None) -> bool:
    """Valida se o PDF da política existe fisicamente no storage persistente."""
    try:
        absolute_path = build_safe_storage_path(get_privacy_policies_storage_dir(), filename)
    except ValueError:
        return False
    return absolute_path.is_file()


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
