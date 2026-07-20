"""
Rotas/API documentais e chat da Agente Compara.

Upload, status, remoção e limpeza delegam ao wrapper agente_compara_doc_service.
Chat documental usa prompt/contexto próprios e governança Cleiton.
"""
from __future__ import annotations

import logging
from pathlib import Path
from flask import Blueprint, jsonify, request, send_file, session
from flask_login import current_user

from app.agente_compara_doc_context import build_agente_compara_document_context_for_chat
from app.agente_compara_correction_service import (
    AgenteComparaCorrectionError,
    ERROR_CORRECTION_CONSTRAINT_MISMATCH,
    ERROR_CORRECTION_NO_TEMP_TABLE,
    ERROR_CORRECTION_PREVIEW_EXPIRED,
    ERROR_CORRECTION_PREVIEW_NOT_FOUND,
    ERROR_CORRECTION_SUGGESTION_NOT_FOUND,
    ERROR_CORRECTION_UNDO_NOT_FOUND,
    apply_audit_correction_for_session,
    preview_audit_correction_for_session,
    undo_last_audit_correction_for_session,
)
from app.agente_compara_doc_service import (
    AGENTE_COMPARA_CHAT_FLOW_TYPE,
    AGENTE_COMPARA_INSIGHTS_CHAT_FLOW_TYPE,
    AGENTE_COMPARA_TEMPLATE_FILENAME,
    AgenteComparaBatchError,
    AgenteComparaCoverageError,
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
    AgenteComparaTempTableError,
    build_document_status_metadata,
    clear_documents_for_session,
    get_allowed_document_formats,
    get_agente_compara_doc_ids,
    get_document_session_totals,
    get_active_temp_table_for_session,
    maybe_cleanup_expired_cleiton_docs,
    prepare_and_register_document,
    remove_document_from_session,
    run_audit_batch_for_session,
    save_temp_table_edit,
    upload_audit_batch_from_file,
    upload_coverage_table_from_file,
    get_agente_compara_template_path,
)
from app.run_agente_compara_temp_table import trigger_temp_table_extraction_for_session
from app.run_agente_compara_chat import (
    cache_chat_response,
    chat_agente_compara_reply,
    get_cached_chat_response,
    normalize_chat_request_id,
    sanitize_chat_history,
)
from app.run_agente_compara_insights_chat import chat_agente_compara_insights_reply
from app.agente_compara_insights_context import (
    ERROR_INSIGHTS_CHAT_LOCKED,
    load_audit_insights_bundle,
    mark_insights_chat_unlocked,
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
from app.services.agente_compara_config_service import get_agente_compara_config
from app.services.agente_compara_config_service import get_active_calculation_bases_for_runtime
from app.services.cleiton_doc_config_service import get_cleiton_doc_config
from app.services.cleiton_operacao_autorizacao_service import (
    avaliar_autorizacao_operacao_por_franquia,
)

logger = logging.getLogger(__name__)

agente_compara_api_bp = Blueprint("agente_compara_api", __name__)


def _session_payload() -> dict:
    totals = get_document_session_totals()
    return {
        "count": totals["active_count"],
        "max_files": totals["max_files_per_session"],
        "total_bytes": totals["total_bytes"],
        "session_max_bytes": totals["session_max_bytes"],
    }


def _authorize_agente_compara_api(*, auth_message: str):
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


def _authorize_agente_compara_documents_api():
    return _authorize_agente_compara_api(
        auth_message="Autenticação necessária para documentos da Agente Compara.",
    )


CHAT_DISABLED_MESSAGE = (
    "O chat da Agente Compara está desabilitado no momento."
)
DOCUMENTS_REQUIRED_MESSAGE = (
    "Envie pelo menos um documento para a Agente Compara analisar nesta conversa."
)
AUDIT_UPLOAD_DISABLED_MESSAGE = (
    "Upload documental da Agente Compara está desabilitado no momento."
)


def _chat_success_payload(result: dict, *, show_documents_used: bool, cached: bool = False) -> dict:
    payload = {
        "ok": True,
        "answer": result.get("answer") or "",
        "flow_type": result.get("flow_type") or AGENTE_COMPARA_CHAT_FLOW_TYPE,
    }
    if show_documents_used:
        payload["documents_used"] = list(result.get("documents_used") or [])
    if cached:
        payload["cached"] = True
    return payload


def _insights_chat_success_payload(result: dict, *, cached: bool = False) -> dict:
    payload = {
        "ok": True,
        "answer": result.get("answer") or "",
        "flow_type": result.get("flow_type") or AGENTE_COMPARA_INSIGHTS_CHAT_FLOW_TYPE,
        "deterministic": bool(result.get("deterministic")),
    }
    if result.get("intent"):
        payload["intent"] = result.get("intent")
    if cached:
        payload["cached"] = True
    return payload


def _http_status_for_insights_error(error_code: str) -> int:
    if error_code in {
        "agente_compara_insights_no_temp_table",
        "agente_compara_insights_temp_table_expired",
        "agente_compara_insights_batch_not_found",
    }:
        return 404
    if error_code in {
        "agente_compara_insights_batch_not_processed",
        "agente_compara_insights_batch_no_results",
    }:
        return 409
    if error_code == ERROR_INSIGHTS_CHAT_LOCKED:
        return 403
    return 400


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


def _http_status_for_correction_error(error_code: str) -> int:
    if error_code in {
        ERROR_CORRECTION_NO_TEMP_TABLE,
        ERROR_CORRECTION_SUGGESTION_NOT_FOUND,
        ERROR_CORRECTION_PREVIEW_NOT_FOUND,
        ERROR_CORRECTION_UNDO_NOT_FOUND,
    }:
        return 404
    if error_code == ERROR_CORRECTION_PREVIEW_EXPIRED:
        return 410
    if error_code == ERROR_CORRECTION_CONSTRAINT_MISMATCH:
        return 409
    return _http_status_for_audit_batch_error(error_code)


@agente_compara_api_bp.route("/api/agente-compara/documents/upload", methods=["POST"])
def agente_compara_documents_upload():
    unauthorized = _authorize_agente_compara_documents_api()
    if unauthorized is not None:
        return unauthorized

    audit_cfg = get_agente_compara_config()
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
        logger.exception("Falha inesperada no upload documental da Agente Compara.")
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
            "Agente Compara temp_table: falha na extração pós-upload; upload preservado."
        )

    temp_table = None
    try:
        temp_table = get_active_temp_table_for_session()
    except Exception:
        logger.exception("Agente Compara temp_table: falha ao ler temp_table após upload.")

    return jsonify(
        {
            "ok": True,
            "document": document,
            "session": _session_payload(),
            "allowed_formats": get_allowed_document_formats(),
            "calculation_bases": get_active_calculation_bases_for_runtime(
                audit_cfg.calculation_bases
            ),
            "temp_table": temp_table,
        }
    )


@agente_compara_api_bp.route("/api/agente-compara/documents/status", methods=["GET"])
def agente_compara_documents_status():
    unauthorized = _authorize_agente_compara_documents_api()
    if unauthorized is not None:
        return unauthorized

    metadata = build_document_status_metadata()
    cleiton_cfg = get_cleiton_doc_config()
    audit_cfg = get_agente_compara_config()
    cleiton_upload_enabled = bool(cleiton_cfg.upload_enabled)
    agente_compara_upload_enabled = bool(audit_cfg.upload_enabled)
    return jsonify(
        {
            "ok": True,
            "documents": metadata["documents"],
            "temp_table": metadata.get("temp_table"),
            "calculation_bases": metadata.get("calculation_bases") or [],
            "session": metadata["session"],
            "allowed_formats": metadata["allowed_formats"],
            "upload_enabled": cleiton_upload_enabled and agente_compara_upload_enabled,
            "cleiton_upload_enabled": cleiton_upload_enabled,
            "agente_compara_upload_enabled": agente_compara_upload_enabled,
            "domain": metadata["domain"],
            "flow_types": metadata["flow_types"],
        }
    )


@agente_compara_api_bp.route("/api/agente-compara/documents/<doc_id>", methods=["DELETE"])
def agente_compara_documents_delete(doc_id: str):
    unauthorized = _authorize_agente_compara_documents_api()
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


@agente_compara_api_bp.route("/api/agente-compara/documents/clear", methods=["POST"])
def agente_compara_documents_clear():
    unauthorized = _authorize_agente_compara_documents_api()
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


@agente_compara_api_bp.route("/api/agente-compara/temp-table/save", methods=["POST"])
def agente_compara_temp_table_save():
    unauthorized = _authorize_agente_compara_documents_api()
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
    except AgenteComparaTempTableError as exc:
        payload = {
            "ok": False,
            "error_code": exc.error_code,
            "message": exc.message,
        }
        if exc.errors:
            payload["errors"] = exc.errors
        return (
            jsonify(payload),
            _http_status_for_temp_table_error(exc.error_code),
        )
    except Exception:
        logger.exception("Falha inesperada ao salvar revisão da temp_table da Agente Compara.")
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


@agente_compara_api_bp.route("/api/agente-compara/coverage/upload", methods=["POST"])
def agente_compara_coverage_upload():
    unauthorized = _authorize_agente_compara_documents_api()
    if unauthorized is not None:
        return unauthorized

    audit_cfg = get_agente_compara_config()
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
    except AgenteComparaCoverageError as exc:
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
        logger.exception("Falha inesperada no upload complementar de coverage da Agente Compara.")
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


@agente_compara_api_bp.route("/api/agente-compara/audit-template", methods=["GET"])
def agente_compara_template_download():
    template_path = get_agente_compara_template_path()
    if not template_path.exists() or not template_path.is_file():
        return "Arquivo de modelo indisponível no momento.", 404
    return send_file(
        template_path,
        as_attachment=True,
        download_name=AGENTE_COMPARA_TEMPLATE_FILENAME,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@agente_compara_api_bp.route("/api/agente-compara/audit/upload", methods=["POST"])
def agente_compara_batch_upload():
    unauthorized = _authorize_agente_compara_documents_api()
    if unauthorized is not None:
        return unauthorized

    audit_cfg = get_agente_compara_config()
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
    except AgenteComparaBatchError as exc:
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
        logger.exception("Falha inesperada no upload do arquivo auditado da Agente Compara.")
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


@agente_compara_api_bp.route("/api/agente-compara/audit/run", methods=["POST"])
def agente_compara_batch_run():
    unauthorized = _authorize_agente_compara_documents_api()
    if unauthorized is not None:
        return unauthorized

    user_scope = getattr(current_user, "id", None)
    franquia_scope = getattr(current_user, "franquia_id", None)
    try:
        temp_table = run_audit_batch_for_session(
            user_scope=user_scope,
            franquia_scope=franquia_scope,
        )
    except AgenteComparaBatchError as exc:
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
        logger.exception("Falha inesperada ao processar lote auditado da Agente Compara.")
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": "agente_compara_run_failed",
                    "message": "Não foi possível processar a auditoria neste momento.",
                }
            ),
            500,
        )

    return jsonify({"ok": True, "temp_table": temp_table})


@agente_compara_api_bp.route("/api/agente-compara/audit/correction/preview", methods=["POST"])
def agente_compara_correction_preview():
    unauthorized = _authorize_agente_compara_documents_api()
    if unauthorized is not None:
        return unauthorized

    data = request.get_json(silent=True) or {}
    suggestion_id = str(data.get("suggestion_id") or "").strip()
    if not suggestion_id:
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": "agente_compara_correction_invalid_payload",
                    "message": "Informe suggestion_id para simular a correção.",
                }
            ),
            400,
        )

    user_scope = getattr(current_user, "id", None)
    franquia_scope = getattr(current_user, "franquia_id", None)
    try:
        preview = preview_audit_correction_for_session(
            suggestion_id,
            user_scope=user_scope,
            franquia_scope=franquia_scope,
        )
    except AgenteComparaCorrectionError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": exc.error_code,
                    "message": exc.message,
                }
            ),
            _http_status_for_correction_error(exc.error_code),
        )
    except Exception:
        logger.exception("Falha inesperada no preview de correção da Agente Compara.")
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": "agente_compara_correction_preview_failed",
                    "message": "Não foi possível simular a correção neste momento.",
                }
            ),
            500,
        )

    return jsonify({"ok": True, "preview": preview})


@agente_compara_api_bp.route("/api/agente-compara/audit/correction/apply", methods=["POST"])
def agente_compara_correction_apply():
    unauthorized = _authorize_agente_compara_documents_api()
    if unauthorized is not None:
        return unauthorized

    data = request.get_json(silent=True) or {}
    preview_id = str(data.get("preview_id") or "").strip()
    suggestion_id = str(data.get("suggestion_id") or "").strip()
    if not preview_id or not suggestion_id:
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": "agente_compara_correction_invalid_payload",
                    "message": "Informe preview_id e suggestion_id para aplicar a correção.",
                }
            ),
            400,
        )

    user_scope = getattr(current_user, "id", None)
    franquia_scope = getattr(current_user, "franquia_id", None)
    try:
        applied = apply_audit_correction_for_session(
            preview_id=preview_id,
            suggestion_id=suggestion_id,
            user_scope=user_scope,
            franquia_scope=franquia_scope,
        )
    except AgenteComparaCorrectionError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": exc.error_code,
                    "message": exc.message,
                }
            ),
            _http_status_for_correction_error(exc.error_code),
        )
    except Exception:
        logger.exception("Falha inesperada ao aplicar correção da Agente Compara.")
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": "agente_compara_correction_apply_failed",
                    "message": "Não foi possível aplicar a correção neste momento.",
                }
            ),
            500,
        )

    return jsonify({"ok": True, **applied})


@agente_compara_api_bp.route("/api/agente-compara/audit/correction/undo", methods=["POST"])
def agente_compara_correction_undo():
    unauthorized = _authorize_agente_compara_documents_api()
    if unauthorized is not None:
        return unauthorized

    data = request.get_json(silent=True) or {}
    application_id = str(data.get("application_id") or "").strip() or None
    user_scope = getattr(current_user, "id", None)
    franquia_scope = getattr(current_user, "franquia_id", None)
    try:
        undone = undo_last_audit_correction_for_session(
            application_id=application_id,
            user_scope=user_scope,
            franquia_scope=franquia_scope,
        )
    except AgenteComparaCorrectionError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": exc.error_code,
                    "message": exc.message,
                }
            ),
            _http_status_for_correction_error(exc.error_code),
        )
    except Exception:
        logger.exception("Falha inesperada ao desfazer correção da Agente Compara.")
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": "agente_compara_correction_undo_failed",
                    "message": "Não foi possível desfazer a correção neste momento.",
                }
            ),
            500,
        )

    return jsonify({"ok": True, **undone})


@agente_compara_api_bp.route("/api/agente-compara/chat", methods=["POST"])
def agente_compara_chat():
    unauthorized = _authorize_agente_compara_api(
        auth_message="Autenticação necessária para conversar com a Agente Compara.",
    )
    if unauthorized is not None:
        return unauthorized

    audit_cfg = get_agente_compara_config()
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
        doc_ctx = build_agente_compara_document_context_for_chat(session)
    except Exception:
        logger.exception(
            "Falha ao montar contexto documental da Agente Compara; continuando sem anexos."
        )
        doc_ctx = {
            "context_block": "",
            "gemini_file_parts": [],
            "has_documents": False,
            "flow_type": AGENTE_COMPARA_CHAT_FLOW_TYPE,
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

    result = chat_agente_compara_reply(
        message_text,
        history,
        document_context_block=doc_ctx.get("context_block") or None,
        document_file_parts=doc_ctx.get("gemini_file_parts") or None,
        has_documents=bool(doc_ctx.get("has_documents")),
        documents_meta=(doc_ctx.get("meta") or {}).get("documents"),
        source_doc_ids=get_agente_compara_doc_ids(session),
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


@agente_compara_api_bp.route("/api/agente-compara/audit-chat/unlock", methods=["POST"])
def agente_compara_insights_chat_unlock():
    unauthorized = _authorize_agente_compara_api(
        auth_message="Autenticação necessária para liberar o chat analítico da Agente Compara.",
    )
    if unauthorized is not None:
        return unauthorized

    loaded = load_audit_insights_bundle(session, require_unlock=False)
    if not loaded.get("ok"):
        error_code = loaded.get("error_code") or "insights_unavailable"
        return (
            jsonify(
                {
                    "ok": False,
                    "error": error_code,
                    "error_code": error_code,
                    "message": loaded.get("message") or "Não foi possível liberar o chat analítico.",
                }
            ),
            _http_status_for_insights_error(error_code),
        )

    unlock_meta = mark_insights_chat_unlocked(session, loaded["bundle"])
    return jsonify(
        {
            "ok": True,
            "unlocked": True,
            "temp_table_id": unlock_meta.get("temp_table_id"),
            "audit_batch_id": unlock_meta.get("audit_batch_id"),
            "processed_at": unlock_meta.get("processed_at"),
        }
    )


@agente_compara_api_bp.route("/api/agente-compara/audit-chat", methods=["POST"])
def agente_compara_insights_chat():
    unauthorized = _authorize_agente_compara_api(
        auth_message="Autenticação necessária para consultas analíticas da Agente Compara.",
    )
    if unauthorized is not None:
        return unauthorized

    audit_cfg = get_agente_compara_config()
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
    visual_focus = data.get("visual_focus") if isinstance(data.get("visual_focus"), dict) else None

    result = chat_agente_compara_insights_reply(
        message_text,
        history,
        session_obj=session,
        request_id=request_id,
        visual_focus=visual_focus,
        max_history=audit_cfg.chat_max_history,
        question_max_chars=audit_cfg.question_max_chars,
        fallback_message=audit_cfg.fallback_message,
    )

    if result.get("error"):
        error_code = result.get("error") or "processing_failed"
        status = 503 if error_code == "service_unavailable" else _http_status_for_insights_error(error_code)
        if error_code == "invalid_message":
            status = 400
        if error_code == "processing_failed":
            status = 500
        error_message = result.get("message") or "Não foi possível processar a mensagem."
        if error_code == "processing_failed" and result.get("answer"):
            return jsonify(_insights_chat_success_payload(result))
        return (
            jsonify(
                {
                    "ok": False,
                    "error": error_code,
                    "error_code": error_code,
                    "message": error_message,
                }
            ),
            status,
        )

    payload = _insights_chat_success_payload(result, cached=bool(result.get("cached")))
    return jsonify(payload)
