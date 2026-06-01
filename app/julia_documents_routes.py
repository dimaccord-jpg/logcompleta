"""
Rotas/API documentais do chat operacional da Júlia (Fase 4).

Upload, listagem, remoção e limpeza delegam preparação/registro ao Cleiton.
A Júlia não processa arquivos nesta camada.
"""
from __future__ import annotations

import logging
from pathlib import Path

from flask import Blueprint, jsonify, request
from flask_login import current_user

from app.cleiton_doc_contracts import (
    ERROR_FILE_TOO_LARGE,
    ERROR_INVALID_SIZE,
    ERROR_MAX_FILES,
    ERROR_MISSING_FILE,
    ERROR_SESSION_BYTES,
    ERROR_UPLOAD_DISABLED,
    ERROR_UPLOAD_FAILED,
)
from app.cleiton_doc_prepare import CleitonDocSecurityError
from app.cleiton_doc_service import (
    CleitonDocSessionError,
    clear_documents_for_session,
    get_active_documents_for_session,
    get_document_session_totals,
    maybe_cleanup_expired_cleiton_docs,
    prepare_and_register_document,
    remove_document_from_session,
)
from app.services.cleiton_doc_config_service import get_cleiton_doc_config
from app.services.cleiton_operacao_autorizacao_service import (
    avaliar_autorizacao_operacao_por_franquia,
)

logger = logging.getLogger(__name__)

julia_documents_bp = Blueprint("julia_documents", __name__)


def _session_payload() -> dict:
    totals = get_document_session_totals()
    return {
        "count": totals["active_count"],
        "max_files": totals["max_files_per_session"],
        "total_bytes": totals["total_bytes"],
        "session_max_bytes": totals["session_max_bytes"],
    }


def _authorize_julia_documents_api():
    if not current_user.is_authenticated:
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": "auth_required",
                    "message": "Autenticação necessária para documentos da Júlia.",
                }
            ),
            401,
        )
    authz = avaliar_autorizacao_operacao_por_franquia(current_user)
    if not authz.get("permitido", True):
        msg = authz.get("mensagem_usuario") or "Operação indisponível para este usuário no momento."
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": "franquia_blocked",
                    "message": msg,
                    "authorization": authz,
                }
            ),
            403,
        )
    return None


def _http_status_for_error(error_code: str) -> int:
    if error_code == ERROR_UPLOAD_DISABLED:
        return 403
    if error_code in {ERROR_FILE_TOO_LARGE, ERROR_INVALID_SIZE, ERROR_SESSION_BYTES}:
        return 413
    if error_code == ERROR_MAX_FILES:
        return 409
    return 400


@julia_documents_bp.route("/api/julia/documents/upload", methods=["POST"])
def julia_documents_upload():
    unauthorized = _authorize_julia_documents_api()
    if unauthorized is not None:
        return unauthorized

    cfg = get_cleiton_doc_config()
    if not cfg.upload_enabled:
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": ERROR_UPLOAD_DISABLED,
                    "message": "Upload documental desabilitado no momento.",
                }
            ),
            403,
        )

    maybe_cleanup_expired_cleiton_docs()

    upload = request.files.get("file")
    if upload is None or not getattr(upload, "filename", "").strip():
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": ERROR_MISSING_FILE,
                    "message": "Nenhum arquivo enviado no campo 'file'.",
                }
            ),
            400,
        )

    display_name = (upload.filename or "documento").strip()
    file_bytes = upload.read() or b""
    mime_type = (upload.mimetype or "").strip() or None
    extension = Path(display_name).suffix.lower() or None

    try:
        document = prepare_and_register_document(
            display_name=display_name,
            file_bytes=file_bytes,
            mime_type=mime_type,
            extension=extension,
        )
    except CleitonDocSecurityError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": exc.error_code,
                    "message": exc.message,
                }
            ),
            _http_status_for_error(exc.error_code),
        )
    except CleitonDocSessionError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": exc.error_code,
                    "message": exc.message,
                }
            ),
            _http_status_for_error(exc.error_code),
        )
    except Exception:
        logger.exception("Falha inesperada no upload documental da Júlia.")
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": ERROR_UPLOAD_FAILED,
                    "message": "Não foi possível processar o upload documental.",
                }
            ),
            500,
        )

    return jsonify(
        {
            "ok": True,
            "document": document,
            "session": _session_payload(),
        }
    )


@julia_documents_bp.route("/api/julia/documents", methods=["GET"])
def julia_documents_list():
    unauthorized = _authorize_julia_documents_api()
    if unauthorized is not None:
        return unauthorized

    maybe_cleanup_expired_cleiton_docs()
    documents = get_active_documents_for_session()
    return jsonify(
        {
            "ok": True,
            "documents": documents,
            "session": _session_payload(),
        }
    )


@julia_documents_bp.route("/api/julia/documents/<doc_id>", methods=["DELETE"])
def julia_documents_delete(doc_id: str):
    unauthorized = _authorize_julia_documents_api()
    if unauthorized is not None:
        return unauthorized

    remove_document_from_session(doc_id)
    return jsonify(
        {
            "ok": True,
            "doc_id": (doc_id or "").strip(),
            "session": _session_payload(),
        }
    )


@julia_documents_bp.route("/api/julia/documents/clear", methods=["POST"])
def julia_documents_clear():
    unauthorized = _authorize_julia_documents_api()
    if unauthorized is not None:
        return unauthorized

    clear_documents_for_session()
    return jsonify(
        {
            "ok": True,
            "session": _session_payload(),
        }
    )
