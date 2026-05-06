"""
Serviços para Termos de Uso: termo vigente, diretório de PDFs e URL estática.
Evita hardcode e centraliza referência ao termo ativo no banco e nos templates.
"""
import logging
import os

from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models import TermsOfUse


logger = logging.getLogger(__name__)


# Subpasta sob static onde os PDFs são armazenados (path relativo ao static)
TERMS_STATIC_SUBDIR = "terms"


def get_terms_upload_dir(app=None):
    """
    Retorna o diretório absoluto para armazenar PDFs dos termos.
    Diretório seguro: app/static/terms/ (sempre dentro do app).
    """
    if app is None:
        from flask import current_app
        app = current_app
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    terms_dir = os.path.join(root, "app", "static", TERMS_STATIC_SUBDIR)
    return terms_dir


def ensure_terms_dir_exists(app=None):
    """Garante que o diretório app/static/terms/ exista."""
    terms_dir = get_terms_upload_dir(app)
    os.makedirs(terms_dir, exist_ok=True)
    return terms_dir


def _term_file_exists(filename: str | None) -> bool:
    """Valida se o PDF do termo existe fisicamente em app/static/terms/."""
    nome = (filename or "").strip()
    if not nome:
        return False
    terms_dir = os.path.abspath(get_terms_upload_dir())
    absolute_path = os.path.abspath(os.path.join(terms_dir, nome))
    if os.path.commonpath([terms_dir, absolute_path]) != terms_dir:
        return False
    return os.path.isfile(absolute_path)


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


