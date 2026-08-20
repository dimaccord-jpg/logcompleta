"""
Serviço de gestão de Termos de Uso.
Upload de PDF, ativação do novo termo, desativação do anterior e notificação aos usuários.
"""
import os
import logging
from datetime import datetime
from pathlib import Path
from werkzeug.datastructures import FileStorage

from app.extensions import db
from app.legal_document_storage import build_safe_storage_path
from app.models import TermsOfUse, User
from app.services.legal_notification_eligibility import (
    REASON_CLOSED,
    classify_legal_notification_recipient,
)
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
    terms_dir = Path(get_terms_upload_dir(app))
    safe_name = nome_seguro_termo(file.filename or "")
    filepath = build_safe_storage_path(terms_dir, safe_name)
    file.save(str(filepath))
    if not filepath.is_file():
        raise ValueError("Falha ao persistir o arquivo de Termos de Uso.")

    TermsOfUse.query.filter_by(is_active=True).update({"is_active": False})
    new_term = TermsOfUse(filename=safe_name, is_active=True)
    db.session.add(new_term)
    db.session.commit()

    terms_url = None
    with app.app_context():
        from flask import url_for

        terms_url = url_for("terms_of_use", _external=True)
    users = User.query.all()
    total_users = len(users)
    sent, failed = 0, 0
    eligible = 0
    ignored_closed = 0
    ignored_invalid_email = 0
    logger.info(
        "Iniciando notificação jurídica de termo atualizado. total_usuarios_encontrados=%s",
        total_users,
    )
    for u in users:
        decision = classify_legal_notification_recipient(u)
        if not decision.eligible:
            if decision.reason == REASON_CLOSED:
                ignored_closed += 1
                logger.info(
                    "Notificação de termo ignorada por encerramento/desidentificação. user_id=%s",
                    getattr(u, "id", None),
                )
            else:
                ignored_invalid_email += 1
                logger.info(
                    "Notificação de termo ignorada por e-mail ausente/inválido. user_id=%s",
                    getattr(u, "id", None),
                )
            continue
        eligible += 1
        try:
            send_terms_updated_notification(
                decision.email,
                u.full_name or decision.email,
                terms_url,
            )
            sent += 1
        except Exception as e:
            logger.warning(
                "Falha ao enviar notificação de termo. user_id=%s error_type=%s",
                getattr(u, "id", None),
                type(e).__name__,
            )
            failed += 1
    logger.info(
        "Notificação jurídica de termo concluída. total_usuarios_encontrados=%s "
        "elegiveis=%s enviados=%s falhas=%s ignorados_encerrados=%s "
        "ignorados_email_invalido=%s",
        total_users,
        eligible,
        sent,
        failed,
        ignored_closed,
        ignored_invalid_email,
    )
    return sent, failed
