"""
Serviços para Termos de Uso: termo vigente e diretório persistente de PDFs.
Evita hardcode e centraliza referência ao termo ativo no banco e nos templates.
"""
import logging

from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.legal_document_storage import (
    build_safe_storage_path,
    ensure_terms_storage_dir,
    get_terms_storage_dir,
)
from app.models import TermsOfUse


logger = logging.getLogger(__name__)


def get_terms_upload_dir(app=None):
    """
    Retorna o diretório absoluto para armazenar PDFs dos termos.
    Diretório seguro em storage persistente: <data_dir>/legal/terms/.
    """
    _ = app  # compatibilidade de assinatura
    return str(get_terms_storage_dir())


def ensure_terms_dir_exists(app=None):
    """Garante que o diretório persistente de termos exista."""
    _ = app  # compatibilidade de assinatura
    return str(ensure_terms_storage_dir())


def _term_file_exists(filename: str | None) -> bool:
    """Valida se o PDF do termo existe fisicamente no storage persistente."""
    try:
        absolute_path = build_safe_storage_path(get_terms_storage_dir(), filename)
    except ValueError:
        return False
    return absolute_path.is_file()


def get_active_term():
    """
    Retorna o registro TermsOfUse ativo (is_active=True) ou None.
    Usado em templates e rotas para link dinâmico ao PDF vigente.
    """
    try:
        active_term = (
            TermsOfUse.query.filter_by(is_active=True)
            .order_by(TermsOfUse.upload_date.desc())
            .first()
        )
        if not active_term:
            return None
        if not _term_file_exists(active_term.filename):
            logger.warning(
                "Termo ativo inconsistente no banco (arquivo ausente): %s",
                active_term.filename,
            )
            return None
        return active_term
    except SQLAlchemyError as exc:
        logger.exception("Falha ao consultar termo ativo: %s", exc)
        db.session.rollback()
        return None


