"""
Rotas/API documentais e chat da Cleide Auditoria.

Upload, status, remoção e limpeza delegam ao wrapper cleide_audit_doc_service.
Chat documental usa prompt/contexto próprios e governança Cleiton.
"""
from __future__ import annotations

import logging
from pathlib import Path
from flask import Blueprint, jsonify, request, send_file, session
from flask_login import current_user

from app.cleide_audit_doc_context import build_cleide_audit_document_context_for_chat
from app.cleide_audit_doc_service import (
    CLEIDE_AUDIT_CHAT_FLOW_TYPE,
    CLEIDE_AUDIT_TEMPLATE_FILENAME,
    CleideAuditBatchError,
    CleideAuditCoverageError,
    ERROR_AUDIT_EMPTY_FILE,
    ERROR_AUDIT_EXPIRED,
    ERROR_AUDIT_INVALID_FORMAT,
    ERROR_AUDIT_INVALID_SHEET,
    ERROR_AUDIT_BATCH_EMPTY,
    ERROR_AUDIT_BATCH_NOT_FOUND,
    ERROR_AUDIT_MISSING_COLUMNS,
    ERROR_AUDIT_NO_TEMP_TABLE,
    ERROR_AUDIT_PARSE_FAILED,
    ERROR_AUDIT_PAYLOAD_TOO_LARGE,
    ERROR_AUDIT_SCOPE_MISMATCH,
    ERROR_AUDIT_TOO_MANY_ROWS,
    ERROR_COVERAGE_EMPTY_FILE,
    ERROR_COVERAGE_EXPIRED,
    ERROR_COVERAGE_INVALID_FORMAT,
    ERROR_COVERAGE_NO_TEMP_TABLE,
    ERROR_COVERAGE_PARSE_FAILED,
    ERROR_COVERAGE_PAYLOAD_TOO_LARGE,
    ERROR_COVERAGE_SCOPE_MISMATCH,
    ERROR_TEMP_TABLE_EXPIRED,
    ERROR_TEMP_TABLE_ID_MISMATCH,
    ERROR_TEMP_TABLE_INVALID_PAYLOAD,
    ERROR_TEMP_TABLE_NOT_FOUND,
    ERROR_TEMP_TABLE_PAYLOAD_TOO_LARGE,
    ERROR_TEMP_TABLE_SCOPE_MISMATCH,
    CleideAuditTempTableError,
    build_document_status_metadata,
    clear_documents_for_session,
    get_allowed_document_formats,
    get_cleide_audit_doc_ids,
    get_document_session_totals,
    get_active_temp_table_for_session,
    maybe_cleanup_expired_cleiton_docs,
    prepare_and_register_document,
    remove_document_from_session,
    run_audit_batch_for_session,
    save_temp_table_edit,
    upload_audit_batch_from_file,
    upload_coverage_table_from_file,
    get_cleide_audit_template_path,
)
from app.run_cleide_audit_temp_table import trigger_temp_table_extraction_for_session
from app.run_cleide_audit_chat import (
    cache_chat_response,
    chat_cleide_audit_reply,
    get_cached_chat_response,
    normalize_chat_request_id,
    sanitize_chat_history,
)
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
from app.cleiton_doc_service import CleitonDocSessionError
from app.services.cleide_audit_config_service import get_cleide_audit_config
from app.services.cleiton_doc_config_service import get_cleiton_doc_config
from app.services.cleiton_operacao_autorizacao_service import (
    avaliar_autorizacao_operacao_por_franquia,
)

logger = logging.getLogger(__name__)

cleide_audit_bp = Blueprint("cleide_audit", __name__)


def _session_payload() -> dict:
    totals = get_document_session_totals()
    return {
        "count": totals["active_count"],
        "max_files": totals["max_files_per_session"],
        "total_bytes": totals["total_bytes"],
        "session_max_bytes": totals["session_max_bytes"],
    }


def _authorize_cleide_audit_api(*, auth_message: str):
    if not current_user.is_authenticated:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "auth_required",
                    "error_code": "auth_required",
                    "message": auth_message,
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
                    "error": "franquia_blocked",
                    "error_code": "franquia_blocked",
                    "message": msg,
                    "authorization": authz,
                }
            ),
            403,
        )
    return None


def _authorize_cleide_audit_documents_api():
    return _authorize_cleide_audit_api(
        auth_message="Autenticação necessária para documentos da Cleide Auditoria.",
    )


CHAT_DISABLED_MESSAGE = (
    "O chat da Cleide Auditoria está desabilitado no momento."
)
DOCUMENTS_REQUIRED_MESSAGE = (
    "Envie pelo menos um documento para a Cleide analisar nesta conversa."
)
AUDIT_UPLOAD_DISABLED_MESSAGE = (
    "Upload documental da Cleide Auditoria está desabilitado no momento."
)


def _chat_success_payload(result: dict, *, show_documents_used: bool, cached: bool = False) -> dict:
    payload = {
        "ok": True,
        "answer": result.get("answer") or "",
        "flow_type": result.get("flow_type") or CLEIDE_AUDIT_CHAT_FLOW_TYPE,
    }
    if show_documents_used:
        payload["documents_used"] = list(result.get("documents_used") or [])
    if cached:
        payload["cached"] = True
    return payload


def _http_status_for_error(error_code: str) -> int:
    if error_code == ERROR_UPLOAD_DISABLED:
        return 403
    if error_code in {ERROR_FILE_TOO_LARGE, ERROR_INVALID_SIZE, ERROR_SESSION_BYTES}:
        return 413
    if error_code == ERROR_MAX_FILES:
        return 409
    return 400


def _http_status_for_temp_table_error(error_code: str) -> int:
    if error_code == ERROR_TEMP_TABLE_PAYLOAD_TOO_LARGE:
        return 413
    if error_code == ERROR_TEMP_TABLE_ID_MISMATCH:
        return 409
    if error_code == ERROR_TEMP_TABLE_SCOPE_MISMATCH:
        return 403
    if error_code in {ERROR_TEMP_TABLE_NOT_FOUND, ERROR_TEMP_TABLE_EXPIRED}:
        return 404
    return 400


def _http_status_for_coverage_error(error_code: str) -> int:
    if error_code == ERROR_COVERAGE_PAYLOAD_TOO_LARGE:
        return 413
    if error_code == ERROR_COVERAGE_SCOPE_MISMATCH:
        return 403
    if error_code in {ERROR_COVERAGE_NO_TEMP_TABLE, ERROR_COVERAGE_EXPIRED}:
        return 404
    return 400


def _http_status_for_audit_batch_error(error_code: str) -> int:
    if error_code == ERROR_AUDIT_PAYLOAD_TOO_LARGE:
        return 413
    if error_code == ERROR_AUDIT_TOO_MANY_ROWS:
        return 413
    if error_code == ERROR_AUDIT_SCOPE_MISMATCH:
        return 403
    if error_code in {ERROR_AUDIT_NO_TEMP_TABLE, ERROR_AUDIT_EXPIRED, ERROR_AUDIT_BATCH_NOT_FOUND}:
        return 404
    if error_code == ERROR_AUDIT_BATCH_EMPTY:
        return 409
    return 400


@cleide_audit_bp.route("/api/cleide-auditoria/documents/upload", methods=["POST"])
def cleide_audit_documents_upload():
    unauthorized = _authorize_cleide_audit_documents_api()
    if unauthorized is not None:
        return unauthorized

    audit_cfg = get_cleide_audit_config()
    if not audit_cfg.upload_enabled:
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": ERROR_UPLOAD_DISABLED,
                    "message": AUDIT_UPLOAD_DISABLED_MESSAGE,
                }
            ),
            403,
        )

    cleiton_cfg = get_cleiton_doc_config()
    if not cleiton_cfg.upload_enabled:
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
        logger.exception("Falha inesperada no upload documental da Cleide Auditoria.")
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

    user_scope = getattr(current_user, "id", None)
    franquia_scope = getattr(current_user, "franquia_id", None)
    try:
        trigger_temp_table_extraction_for_session(
            user_scope=user_scope,
            franquia_scope=franquia_scope,
        )
    except Exception:
        logger.exception(
            "Cleide temp_table: falha na extração pós-upload; upload preservado."
        )

    temp_table = None
    try:
        temp_table = get_active_temp_table_for_session()
    except Exception:
        logger.exception("Cleide temp_table: falha ao ler temp_table após upload.")

    return jsonify(
        {
            "ok": True,
            "document": document,
            "session": _session_payload(),
            "allowed_formats": get_allowed_document_formats(),
            "temp_table": temp_table,
        }
    )


@cleide_audit_bp.route("/api/cleide-auditoria/documents/status", methods=["GET"])
def cleide_audit_documents_status():
    unauthorized = _authorize_cleide_audit_documents_api()
    if unauthorized is not None:
        return unauthorized

    metadata = build_document_status_metadata()
    cleiton_cfg = get_cleiton_doc_config()
    audit_cfg = get_cleide_audit_config()
    cleiton_upload_enabled = bool(cleiton_cfg.upload_enabled)
    cleide_audit_upload_enabled = bool(audit_cfg.upload_enabled)
    return jsonify(
        {
            "ok": True,
            "documents": metadata["documents"],
            "temp_table": metadata.get("temp_table"),
            "session": metadata["session"],
            "allowed_formats": metadata["allowed_formats"],
            "upload_enabled": cleiton_upload_enabled and cleide_audit_upload_enabled,
            "cleiton_upload_enabled": cleiton_upload_enabled,
            "cleide_audit_upload_enabled": cleide_audit_upload_enabled,
            "domain": metadata["domain"],
            "flow_types": metadata["flow_types"],
        }
    )


@cleide_audit_bp.route("/api/cleide-auditoria/documents/<doc_id>", methods=["DELETE"])
def cleide_audit_documents_delete(doc_id: str):
    unauthorized = _authorize_cleide_audit_documents_api()
    if unauthorized is not None:
        return unauthorized

    result = remove_document_from_session(doc_id)
    payload = {
        "ok": True,
        "doc_id": (doc_id or "").strip(),
        "session": _session_payload(),
    }
    if "removed_from_store" in result:
        payload["removed_from_store"] = result["removed_from_store"]
    if "removed_from_session" in result:
        payload["removed_from_session"] = result["removed_from_session"]
    return jsonify(payload)


@cleide_audit_bp.route("/api/cleide-auditoria/documents/clear", methods=["POST"])
def cleide_audit_documents_clear():
    unauthorized = _authorize_cleide_audit_documents_api()
    if unauthorized is not None:
        return unauthorized

    result = clear_documents_for_session()
    payload = {
        "ok": True,
        "session": _session_payload(),
    }
    if "removed_from_store" in result:
        payload["removed_from_store"] = result["removed_from_store"]
    if "removed_from_session" in result:
        payload["removed_from_session"] = result["removed_from_session"]
    return jsonify(payload)


@cleide_audit_bp.route("/api/cleide-auditoria/temp-table/save", methods=["POST"])
def cleide_audit_temp_table_save():
    unauthorized = _authorize_cleide_audit_documents_api()
    if unauthorized is not None:
        return unauthorized

    data = request.get_json(silent=True)
    if data is None:
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": ERROR_TEMP_TABLE_INVALID_PAYLOAD,
                    "message": "Payload deve ser um objeto JSON.",
                }
            ),
            400,
        )

    user_scope = getattr(current_user, "id", None)
    franquia_scope = getattr(current_user, "franquia_id", None)
    try:
        temp_table = save_temp_table_edit(
            data,
            user_scope=user_scope,
            franquia_scope=franquia_scope,
            content_length=request.content_length,
        )
    except CleideAuditTempTableError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": exc.error_code,
                    "message": exc.message,
                }
            ),
            _http_status_for_temp_table_error(exc.error_code),
        )
    except Exception:
        logger.exception("Falha inesperada ao salvar revisão da temp_table da Cleide Auditoria.")
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": "temp_table_save_failed",
                    "message": "Não foi possível salvar a revisão da tabela temporária.",
                }
            ),
            500,
        )

    return jsonify({"ok": True, "temp_table": temp_table})


@cleide_audit_bp.route("/api/cleide-auditoria/coverage/upload", methods=["POST"])
def cleide_audit_coverage_upload():
    unauthorized = _authorize_cleide_audit_documents_api()
    if unauthorized is not None:
        return unauthorized

    audit_cfg = get_cleide_audit_config()
    if not audit_cfg.upload_enabled:
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": ERROR_UPLOAD_DISABLED,
                    "message": AUDIT_UPLOAD_DISABLED_MESSAGE,
                }
            ),
            403,
        )

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

    display_name = (upload.filename or "coverage").strip()
    file_bytes = upload.read() or b""
    extension = Path(display_name).suffix.lower() or None

    user_scope = getattr(current_user, "id", None)
    franquia_scope = getattr(current_user, "franquia_id", None)
    try:
        temp_table = upload_coverage_table_from_file(
            display_name=display_name,
            file_bytes=file_bytes,
            extension=extension,
            user_scope=user_scope,
            franquia_scope=franquia_scope,
        )
    except CleideAuditCoverageError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": exc.error_code,
                    "message": exc.message,
                }
            ),
            _http_status_for_coverage_error(exc.error_code),
        )
    except Exception:
        logger.exception("Falha inesperada no upload complementar de coverage da Cleide Auditoria.")
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": ERROR_UPLOAD_FAILED,
                    "message": "Não foi possível processar o upload complementar de cobertura.",
                }
            ),
            500,
        )

    return jsonify({"ok": True, "temp_table": temp_table})


@cleide_audit_bp.route("/api/cleide-auditoria/audit-template", methods=["GET"])
def cleide_audit_template_download():
    template_path = get_cleide_audit_template_path()
    if not template_path.exists() or not template_path.is_file():
        return "Arquivo de modelo indisponível no momento.", 404
    return send_file(
        template_path,
        as_attachment=True,
        download_name=CLEIDE_AUDIT_TEMPLATE_FILENAME,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@cleide_audit_bp.route("/api/cleide-auditoria/audit/upload", methods=["POST"])
def cleide_audit_batch_upload():
    unauthorized = _authorize_cleide_audit_documents_api()
    if unauthorized is not None:
        return unauthorized

    audit_cfg = get_cleide_audit_config()
    if not audit_cfg.upload_enabled:
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": ERROR_UPLOAD_DISABLED,
                    "message": AUDIT_UPLOAD_DISABLED_MESSAGE,
                }
            ),
            403,
        )

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

    display_name = (upload.filename or "auditado").strip()
    file_bytes = upload.read() or b""
    extension = Path(display_name).suffix.lower() or None

    user_scope = getattr(current_user, "id", None)
    franquia_scope = getattr(current_user, "franquia_id", None)
    try:
        temp_table = upload_audit_batch_from_file(
            display_name=display_name,
            file_bytes=file_bytes,
            extension=extension,
            user_scope=user_scope,
            franquia_scope=franquia_scope,
        )
    except CleideAuditBatchError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": exc.error_code,
                    "message": exc.message,
                }
            ),
            _http_status_for_audit_batch_error(exc.error_code),
        )
    except Exception:
        logger.exception("Falha inesperada no upload do arquivo auditado da Cleide Auditoria.")
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": ERROR_UPLOAD_FAILED,
                    "message": "Não foi possível processar o arquivo auditado.",
                }
            ),
            500,
        )

    return jsonify({"ok": True, "temp_table": temp_table})


@cleide_audit_bp.route("/api/cleide-auditoria/audit/run", methods=["POST"])
def cleide_audit_batch_run():
    unauthorized = _authorize_cleide_audit_documents_api()
    if unauthorized is not None:
        return unauthorized

    user_scope = getattr(current_user, "id", None)
    franquia_scope = getattr(current_user, "franquia_id", None)
    try:
        temp_table = run_audit_batch_for_session(
            user_scope=user_scope,
            franquia_scope=franquia_scope,
        )
    except CleideAuditBatchError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": exc.error_code,
                    "message": exc.message,
                }
            ),
            _http_status_for_audit_batch_error(exc.error_code),
        )
    except Exception:
        logger.exception("Falha inesperada ao processar lote auditado da Cleide Auditoria.")
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": "cleide_audit_run_failed",
                    "message": "Não foi possível processar a auditoria neste momento.",
                }
            ),
            500,
        )

    return jsonify({"ok": True, "temp_table": temp_table})


@cleide_audit_bp.route("/api/cleide-auditoria/chat", methods=["POST"])
def cleide_audit_chat():
    unauthorized = _authorize_cleide_audit_api(
        auth_message="Autenticação necessária para conversar com a Cleide Auditoria.",
    )
    if unauthorized is not None:
        return unauthorized

    audit_cfg = get_cleide_audit_config()
    if not audit_cfg.chat_enabled:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "chat_disabled",
                    "error_code": "chat_disabled",
                    "message": CHAT_DISABLED_MESSAGE,
                }
            ),
            403,
        )

    data = request.get_json(silent=True) or {}
    raw_message = data.get("message")
    if raw_message is None:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "invalid_message",
                    "message": "Campo 'message' é obrigatório.",
                }
            ),
            400,
        )
    if not isinstance(raw_message, str) or not raw_message.strip():
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "invalid_message",
                    "message": "Campo 'message' deve ser uma string não vazia.",
                }
            ),
            400,
        )

    message_text = raw_message.strip()
    if len(message_text) > audit_cfg.question_max_chars:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "invalid_message",
                    "message": (
                        f"Mensagem excede o limite de {audit_cfg.question_max_chars} caracteres."
                    ),
                }
            ),
            400,
        )

    history = sanitize_chat_history(
        data.get("history"),
        max_history=audit_cfg.chat_max_history,
    )
    request_id = normalize_chat_request_id(data.get("request_id"))

    cached = get_cached_chat_response(session, request_id)
    if cached is not None:
        return jsonify(
            _chat_success_payload(
                cached,
                show_documents_used=audit_cfg.show_documents_used,
                cached=True,
            )
        )

    try:
        doc_ctx = build_cleide_audit_document_context_for_chat(session)
    except Exception:
        logger.exception(
            "Falha ao montar contexto documental da Cleide Auditoria; continuando sem anexos."
        )
        doc_ctx = {
            "context_block": "",
            "gemini_file_parts": [],
            "has_documents": False,
            "flow_type": CLEIDE_AUDIT_CHAT_FLOW_TYPE,
            "meta": {"documents": []},
        }

    if audit_cfg.no_documents_behavior == "require_documents" and not doc_ctx.get("has_documents"):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "documents_required",
                    "error_code": "documents_required",
                    "message": DOCUMENTS_REQUIRED_MESSAGE,
                }
            ),
            403,
        )

    result = chat_cleide_audit_reply(
        message_text,
        history,
        document_context_block=doc_ctx.get("context_block") or None,
        document_file_parts=doc_ctx.get("gemini_file_parts") or None,
        has_documents=bool(doc_ctx.get("has_documents")),
        documents_meta=(doc_ctx.get("meta") or {}).get("documents"),
        source_doc_ids=get_cleide_audit_doc_ids(session),
        session_obj=session,
        max_history=audit_cfg.chat_max_history,
        question_max_chars=audit_cfg.question_max_chars,
        fallback_message=audit_cfg.fallback_message,
        no_hallucination_instruction_enabled=audit_cfg.no_hallucination_instruction_enabled,
    )

    if result.get("error"):
        status = 503 if result.get("error") == "service_unavailable" else 500
        if result.get("error") == "invalid_message":
            status = 400
        error_message = result.get("message") or "Não foi possível processar a mensagem."
        if result.get("error") == "processing_failed":
            error_message = audit_cfg.fallback_message
        return (
            jsonify(
                {
                    "ok": False,
                    "error": result.get("error"),
                    "message": error_message,
                }
            ),
            status,
        )

    payload = _chat_success_payload(
        result,
        show_documents_used=audit_cfg.show_documents_used,
    )
    cache_payload = {
        "ok": True,
        "answer": payload["answer"],
        "documents_used": list(result.get("documents_used") or []),
        "flow_type": payload["flow_type"],
    }
    cache_chat_response(session, request_id, cache_payload)
    return jsonify(payload)
