"""
Serviço de gestão de Termos de Uso.
Upload de PDF, ativação do novo termo, desativação do anterior e notificação aos usuários.
"""
import os
import logging
from datetime import datetime
from werkzeug.datastructures import FileStorage

from app.extensions import db
from app.models import TermsOfUse, User
from app.terms_services import get_terms_upload_dir, ensure_terms_dir_exists
from app.utils.email_helper import send_terms_updated_notification

logger = logging.getLogger(__name__)

ALLOWED_TERMS_EXTENSION = ".pdf"


def extensao_termo_permitida(filename: str) -> bool:
    """Verifica se o arquivo tem extensão permitida para termo de uso."""
    fn = (filename or "").strip().lower()
    return fn.endswith(ALLOWED_TERMS_EXTENSION)


def nome_seguro_termo(original_filename: str) -> str:
    """Gera nome seguro e único para o PDF (evita sobrescrita)."""
    from werkzeug.utils import secure_filename
    safe = secure_filename(original_filename) or "termo.pdf"
    if not safe.lower().endswith(ALLOWED_TERMS_EXTENSION):
        safe = f"termo_{datetime.now().strftime('%Y%m%d_%H%M%S')}{ALLOWED_TERMS_EXTENSION}"
    else:
        base, ext = os.path.splitext(safe)
        safe = f"{base}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}"
    return safe


def processar_upload_termo(app, file: FileStorage) -> tuple[int, int]:
    """
    Salva o PDF no diretório de termos, desativa termos atuais, ativa o novo e notifica usuários.
    Retorna (enviados, falhas) de notificações por e-mail.
    """
    ensure_terms_dir_exists(app)
    terms_dir = get_terms_upload_dir(app)
    safe_name = nome_seguro_termo(file.filename or "")
    filepath = os.path.join(terms_dir, safe_name)
    file.save(filepath)

    TermsOfUse.query.filter_by(is_active=True).update({"is_active": False})
    new_term = TermsOfUse(filename=safe_name, is_active=True)
    db.session.add(new_term)
    db.session.commit()

    terms_url = None
    with app.app_context():
        from flask import url_for
        terms_url = url_for("static", filename=f"terms/{safe_name}", _external=True)
    sent, failed = 0, 0
    for u in User.query.all():
        try:
            send_terms_updated_notification(u.email, u.full_name or u.email, terms_url)
            sent += 1
        except Exception as e:
            logger.warning("Falha ao enviar notificação de termo para %s: %s", u.email, e)
            failed += 1
    return sent, failed
