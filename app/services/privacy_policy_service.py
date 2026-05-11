"""
Serviço de gestão de Política de Privacidade.
Upload de PDF, ativação da nova política, desativação da anterior e notificação operacional aos usuários.
"""
import logging
import os
from datetime import datetime
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from app.auth_services import send_privacy_policy_updated_notification
from app.extensions import db
from app.infra import get_admin_executor
from app.legal_document_storage import build_safe_storage_path
from app.models import PrivacyPolicy, User
from app.privacy_policy_services import (
    ensure_privacy_policy_dir_exists,
    get_active_privacy_policy,
    get_privacy_policy_upload_dir,
)


logger = logging.getLogger(__name__)

ALLOWED_PRIVACY_POLICY_EXTENSION = ".pdf"
ALLOWED_PRIVACY_POLICY_MIME_TYPES = {
    "application/pdf",
    "application/x-pdf",
}
MAX_PRIVACY_POLICY_FILE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB


def extensao_privacy_policy_permitida(filename: str) -> bool:
    """Verifica se o arquivo possui extensão .pdf."""
    return (filename or "").strip().lower().endswith(ALLOWED_PRIVACY_POLICY_EXTENSION)


def _mime_privacy_policy_permitido(mimetype: str | None) -> bool:
    """Valida MIME do PDF quando disponível."""
    if not mimetype:
        return True
    return (mimetype or "").strip().lower() in ALLOWED_PRIVACY_POLICY_MIME_TYPES


def _arquivo_vazio(file: FileStorage) -> bool:
    """Verifica se o stream do arquivo está vazio."""
    stream = file.stream
    current_pos = stream.tell()
    stream.seek(0, os.SEEK_END)
    end_pos = stream.tell()
    stream.seek(current_pos, os.SEEK_SET)
    return end_pos <= 0


def _arquivo_tamanho_bytes(file: FileStorage) -> int:
    """Retorna tamanho do stream em bytes sem consumir o conteúdo."""
    stream = file.stream
    current_pos = stream.tell()
    stream.seek(0, os.SEEK_END)
    end_pos = stream.tell()
    stream.seek(current_pos, os.SEEK_SET)
    return max(0, int(end_pos))


def _arquivo_pdf_assinatura_valida(file: FileStorage) -> bool:
    """
    Valida assinatura real do PDF (%PDF) no início do arquivo.
    Restaura o ponteiro do stream após leitura.
    """
    stream = file.stream
    current_pos = stream.tell()
    stream.seek(0, os.SEEK_SET)
    header = stream.read(4) or b""
    stream.seek(current_pos, os.SEEK_SET)
    return header.startswith(b"%PDF")


def _build_privacy_policy_filename(original_filename: str) -> str:
    """Gera nome padronizado da política com timestamp."""
    _ = secure_filename(original_filename or "")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"politica_privacidade_{timestamp}{ALLOWED_PRIVACY_POLICY_EXTENSION}"


def _build_unique_privacy_policy_filename(upload_dir: str, original_filename: str) -> str:
    """Gera nome único sem sobrescrever arquivo já existente."""
    base_name = _build_privacy_policy_filename(original_filename)
    candidate = base_name
    suffix = 1
    while os.path.exists(os.path.join(upload_dir, candidate)):
        candidate = base_name.replace(
            ALLOWED_PRIVACY_POLICY_EXTENSION,
            f"_{suffix}{ALLOWED_PRIVACY_POLICY_EXTENSION}",
        )
        suffix += 1
    return candidate


def _notify_privacy_policy_update(app, policy_url: str, upload_date) -> tuple[int, int]:
    """Notifica usuários sobre a atualização da política. Falhas não interrompem o fluxo."""
    with app.app_context():
        # Não há no model User um campo explícito e confiável de ativo/inativo.
        # Nesta rodada mantemos compatibilidade com envio para todos os usuários.
        users = User.query.all()
        sent = 0
        failed = 0
        ignored = 0
        total_users = len(users)
        logger.info(
            "Iniciando notificação operacional de política atualizada. total_usuarios_encontrados=%s",
            total_users,
        )
        for user in users:
            user_email = (user.email or "").strip()
            if not user_email:
                ignored += 1
                logger.warning(
                    "Notificação de política ignorada por e-mail ausente. user_id=%s",
                    getattr(user, "id", None),
                )
                continue
            try:
                send_privacy_policy_updated_notification(
                    user_email=user_email,
                    user_name=user.full_name or user_email,
                    policy_url=policy_url,
                    upload_date=upload_date,
                )
                sent += 1
            except Exception as exc:
                logger.warning(
                    "Falha ao enviar notificação de política para %s: %s",
                    user_email,
                    exc,
                )
                failed += 1
        logger.info(
            "Notificação operacional concluída. total_usuarios_encontrados=%s enviados=%s falhas=%s ignorados_sem_email=%s",
            total_users,
            sent,
            failed,
            ignored,
        )
        return sent, failed


def processar_upload_privacy_policy(app, file: FileStorage, uploaded_by_user_id: int | None = None):
    """
    Salva o PDF da política, desativa política ativa anterior, ativa a nova e dispara notificações.
    O envio de e-mail ocorre após commit e não afeta a ativação da política.
    """
    logger.info(
        "Upload de Política de Privacidade iniciado. filename=%s user_id=%s",
        file.filename,
        uploaded_by_user_id,
    )
    if not file or not (file.filename or "").strip():
        logger.warning("Falha de validação: upload de política sem arquivo selecionado.")
        raise ValueError("Nenhum arquivo selecionado para Política de Privacidade.")
    if not extensao_privacy_policy_permitida(file.filename):
        logger.warning("Falha de validação: extensão inválida para Política de Privacidade.")
        raise ValueError("Apenas arquivos .pdf são permitidos para Política de Privacidade.")
    file_size_bytes = _arquivo_tamanho_bytes(file)
    if file_size_bytes <= 0:
        logger.warning("Falha de validação: arquivo vazio para Política de Privacidade.")
        raise ValueError("O arquivo enviado está vazio.")
    if file_size_bytes > MAX_PRIVACY_POLICY_FILE_SIZE_BYTES:
        logger.warning(
            "Falha de validação: upload acima do limite. filename=%s size=%s limit=%s",
            file.filename,
            file_size_bytes,
            MAX_PRIVACY_POLICY_FILE_SIZE_BYTES,
        )
        raise ValueError("Arquivo acima do limite de 5 MB para Política de Privacidade.")
    if not _mime_privacy_policy_permitido(file.mimetype):
        logger.warning(
            "Falha de validação: MIME inválido para Política de Privacidade. mime=%s",
            file.mimetype,
        )
        raise ValueError("Tipo de arquivo inválido. Envie um PDF válido.")
    if not _arquivo_pdf_assinatura_valida(file):
        logger.warning(
            "Falha de validação: assinatura de PDF inválida. filename=%s mime=%s",
            file.filename,
            file.mimetype,
        )
        raise ValueError("Arquivo inválido: o conteúdo não corresponde a um PDF válido.")

    ensure_privacy_policy_dir_exists(app)
    upload_dir = get_privacy_policy_upload_dir(app)
    safe_filename = _build_unique_privacy_policy_filename(upload_dir, file.filename or "")
    save_path = build_safe_storage_path(Path(upload_dir), safe_filename)
    sent = 0
    failed = 0
    notification_mode = "sync"

    try:
        file.save(str(save_path))
        if not save_path.is_file():
            raise ValueError("Falha ao persistir arquivo da Política de Privacidade.")

        previous_policy = (
            PrivacyPolicy.query.filter_by(is_active=True)
            .order_by(PrivacyPolicy.upload_date.desc())
            .first()
        )
        if previous_policy:
            previous_policy.is_active = False
            logger.info(
                "Política anterior desativada. id=%s filename=%s",
                previous_policy.id,
                previous_policy.filename,
            )

        new_policy = PrivacyPolicy(
            filename=safe_filename,
            original_filename=(file.filename or "").strip() or None,
            is_active=True,
            uploaded_by_user_id=uploaded_by_user_id,
            file_size_bytes=file_size_bytes,
            mime_type=(file.mimetype or "").strip().lower() or None,
        )
        db.session.add(new_policy)
        db.session.commit()
        logger.info(
            "Nova Política de Privacidade ativada. id=%s filename=%s size=%s",
            new_policy.id,
            new_policy.filename,
            file_size_bytes,
        )

        with app.app_context():
            from flask import url_for

            policy_url = url_for("privacy_policy", _external=True)
            upload_date = new_policy.upload_date

        try:
            executor = get_admin_executor()
            executor.submit(_notify_privacy_policy_update, app, policy_url, upload_date)
            logger.info("Notificação de política agendada em background.")
            notification_mode = "async"
        except Exception as exc:
            logger.warning(
                "Falha ao agendar envio async de política; executando síncrono. erro=%s",
                exc,
            )
            sent, failed = _notify_privacy_policy_update(app, policy_url, upload_date)
            notification_mode = "sync_fallback"

        active_policy = get_active_privacy_policy()
        logger.info(
            "Upload de Política de Privacidade concluído com sucesso. active_id=%s sent=%s failed=%s mode=%s",
            getattr(active_policy, "id", None),
            sent,
            failed,
            notification_mode,
        )
        return active_policy, sent, failed, notification_mode
    except (ValueError, SQLAlchemyError) as exc:
        db.session.rollback()
        if save_path.exists():
            try:
                save_path.unlink()
            except OSError:
                logger.exception("Falha ao limpar arquivo de política após erro.")
        logger.exception("Falha no upload de Política de Privacidade: %s", exc)
        raise
    except Exception as exc:
        db.session.rollback()
        if save_path.exists():
            try:
                save_path.unlink()
            except OSError:
                logger.exception("Falha ao limpar arquivo de política após erro inesperado.")
        logger.exception("Erro inesperado no upload de Política de Privacidade: %s", exc)
        raise
