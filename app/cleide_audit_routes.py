"""
Rotas/API documentais e chat da Cleide Auditoria.

Upload, status, remoção e limpeza delegam ao wrapper cleide_audit_doc_service.
Chat documental usa prompt/contexto próprios e governança Cleiton.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from flask import Blueprint, jsonify, request, send_file, session
from flask_login import current_user

from app.cleide_audit_doc_context import build_cleide_audit_document_context_for_chat
from app.cleide_audit_correction_service import (
    CleideAuditCorrectionError,
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
from app.cleide_audit_doc_service import (
    CLEIDE_AUDIT_CHAT_FLOW_TYPE,
    CLEIDE_AUDIT_INSIGHTS_CHAT_FLOW_TYPE,
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
from app.run_cleide_audit_insights_chat import chat_cleide_audit_insights_reply
from app.cleide_audit_insights_context import (
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
from app.services.cleide_audit_config_service import get_cleide_audit_config
from app.services.cleide_audit_config_service import get_active_calculation_bases_for_runtime
from app.services.cleiton_doc_config_service import get_cleiton_doc_config
from app.services.cleiton_operacao_autorizacao_service import (
    avaliar_autorizacao_operacao_por_franquia,
)
from app.funnel_event_service import (
    FUNNEL_EVENT_FILE_UPLOADED,
    FUNNEL_EVENT_FREIGHT_CALCULATED,
    FUNNEL_SOURCE_CLEIDE_AUDIT,
    record_completion_with_first_audit,
    record_funnel_event,
)
from app.extensions import db

logger = logging.getLogger(__name__)

cleide_audit_bp = Blueprint("cleide_audit", __name__)


def _canonical_funnel_payload(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _funnel_hash(value) -> str:
    return hashlib.sha256(_canonical_funnel_payload(value).encode("utf-8")).hexdigest()


def _cleide_upload_funnel_key(*, user_id: int, document_id: str) -> str:
    return f"funnel:cleide:upload:{_funnel_hash({'user_id': int(user_id), 'document_id': (document_id or '').strip(), 'event_name': FUNNEL_EVENT_FILE_UPLOADED, 'source': FUNNEL_SOURCE_CLEIDE_AUDIT})}"


def _cleide_completion_funnel_key(*, user_id: int, audit_batch_id: str, execution_id: str) -> str:
    return f"funnel:cleide:completion:{_funnel_hash({'user_id': int(user_id), 'audit_batch_id': (audit_batch_id or '').strip(), 'execution_id': (execution_id or '').strip(), 'event_name': FUNNEL_EVENT_FREIGHT_CALCULATED, 'source': FUNNEL_SOURCE_CLEIDE_AUDIT})}"


def _global_first_audit_funnel_key(*, user_id: int) -> str:
    return f"funnel:first-audit:{_funnel_hash({'user_id': int(user_id), 'event_name': 'first_audit_completed', 'source': 'global'})}"


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
        auth_message="Autenticação necessária para documentos da Auditoria. Faça login para continuar usando.",
    )


CHAT_DISABLED_MESSAGE = (
    "O chat do AgenteAudita está desabilitado no momento."
)
DOCUMENTS_REQUIRED_MESSAGE = (
    "Envie pelo menos um documento para o AgenteAudita analisar nesta conversa."
)
AUDIT_UPLOAD_DISABLED_MESSAGE = (
    "Upload documental do AgenteAudita está desabilitado no momento."
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


def _insights_chat_success_payload(result: dict, *, cached: bool = False) -> dict:
    payload = {
        "ok": True,
        "answer": result.get("answer") or "",
        "flow_type": result.get("flow_type") or CLEIDE_AUDIT_INSIGHTS_CHAT_FLOW_TYPE,
        "deterministic": bool(result.get("deterministic")),
    }
    if result.get("intent"):
        payload["intent"] = result.get("intent")
    if cached:
        payload["cached"] = True
    return payload


def _http_status_for_insights_error(error_code: str) -> int:
    if error_code in {
        "cleide_audit_insights_no_temp_table",
        "cleide_audit_insights_temp_table_expired",
        "cleide_audit_insights_batch_not_found",
    }:
        return 404
    if error_code in {
        "cleide_audit_insights_batch_not_processed",
        "cleide_audit_insights_batch_no_results",
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

    payload = {
        "ok": True,
        "document": document,
        "session": _session_payload(),
        "allowed_formats": get_allowed_document_formats(),
        "calculation_bases": get_active_calculation_bases_for_runtime(
            audit_cfg.calculation_bases
        ),
        "temp_table": temp_table,
    }

    user_id = getattr(current_user, "id", None)
    conta_id = getattr(current_user, "conta_id", None)
    franquia_id = getattr(current_user, "franquia_id", None)
    document_id = (document or {}).get("doc_id") if isinstance(document, dict) else None
    if user_id is not None and conta_id is not None and franquia_id is not None and document_id:
        from app.services.admin_desktop_access_test_service import (
            is_desktop_access_admin_test_mode_for_current_user,
        )

        if is_desktop_access_admin_test_mode_for_current_user():
            # E2E Replay: operação normal, sem FunnelEvent/Meta; marca first_use no run.
            from app.services.admin_desktop_access_test_service import (
                mark_first_use_for_current_session,
            )

            mark_first_use_for_current_session()
        else:
            started_funnel_tx = False
            try:
                orm_session = db.session()
                if not orm_session.in_transaction():
                    orm_session.begin()
                    started_funnel_tx = True
                funnel_result = record_funnel_event(
                    event_name=FUNNEL_EVENT_FILE_UPLOADED,
                    source=FUNNEL_SOURCE_CLEIDE_AUDIT,
                    user_id=int(user_id),
                    conta_id=int(conta_id),
                    franquia_id=int(franquia_id),
                    idempotency_key=_cleide_upload_funnel_key(user_id=int(user_id), document_id=document_id),
                    correlation_id=request.headers.get("X-Request-ID") or request.headers.get("X-Correlation-ID"),
                    document_id=document_id,
                    metadata_json=None,
                )
                if funnel_result.get("created") is True:
                    db.session.commit()
                    payload["funnel_event"] = {
                        "event_name": FUNNEL_EVENT_FILE_UPLOADED,
                        "source": FUNNEL_SOURCE_CLEIDE_AUDIT,
                        "allow_meta_pixel": True,
                        "is_first_audit": False,
                    }
                elif started_funnel_tx:
                    db.session.rollback()
            except Exception as exc:
                db.session.rollback()
                logger.exception(
                    "cleide_audit_funnel_upload_failed event=%s source=%s user_id=%s document_id=%s failure_type=%s",
                    FUNNEL_EVENT_FILE_UPLOADED,
                    FUNNEL_SOURCE_CLEIDE_AUDIT,
                    user_id,
                    document_id,
                    exc.__class__.__name__,
                )

    return jsonify(payload)


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
            "calculation_bases": metadata.get("calculation_bases") or [],
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

    payload = {"ok": True, "temp_table": temp_table}

    user_id = getattr(current_user, "id", None)
    conta_id = getattr(current_user, "conta_id", None)
    franquia_id = getattr(current_user, "franquia_id", None)
    audit_batch = (temp_table or {}).get("audit_batch") if isinstance(temp_table, dict) else None
    audit_batch_id = (audit_batch or {}).get("audit_batch_id") if isinstance(audit_batch, dict) else None
    processed_at = (audit_batch or {}).get("processed_at") if isinstance(audit_batch, dict) else None
    if user_id is not None and conta_id is not None and franquia_id is not None and audit_batch_id and processed_at:
        from app.services.admin_desktop_access_test_service import (
            complete_test_mode_after_successful_audit,
            is_desktop_access_admin_test_mode_for_current_user,
        )

        if is_desktop_access_admin_test_mode_for_current_user():
            # E2E: sem FunnelEvent, sem first_audit_completed_at, sem Meta Pixel.
            complete_test_mode_after_successful_audit()
        else:
            execution_id = request.headers.get("X-Execution-ID") or request.form.get("execution_id") or "cleide-audit-run"
            started_funnel_tx = False
            try:
                orm_session = db.session()
                if not orm_session.in_transaction():
                    orm_session.begin()
                    started_funnel_tx = True
                funnel_result = record_completion_with_first_audit(
                    source=FUNNEL_SOURCE_CLEIDE_AUDIT,
                    user_id=int(user_id),
                    conta_id=int(conta_id),
                    franquia_id=int(franquia_id),
                    freight_idempotency_key=_cleide_completion_funnel_key(
                        user_id=int(user_id),
                        audit_batch_id=str(audit_batch_id),
                        execution_id=str(execution_id),
                    ),
                    first_audit_idempotency_key=_global_first_audit_funnel_key(user_id=int(user_id)),
                    occurred_at=None,
                    correlation_id=request.headers.get("X-Request-ID") or request.headers.get("X-Correlation-ID"),
                    audit_batch_id=str(audit_batch_id),
                    execution_id=str(execution_id)[:120],
                    metadata_json=None,
                )
                if funnel_result.get("freight_calculated", {}).get("created") is True:
                    db.session.commit()
                    payload["funnel_event"] = {
                        "event_name": FUNNEL_EVENT_FREIGHT_CALCULATED,
                        "source": FUNNEL_SOURCE_CLEIDE_AUDIT,
                        "allow_meta_pixel": True,
                        "is_first_audit": bool(funnel_result.get("is_first_audit")),
                    }
                elif started_funnel_tx:
                    db.session.rollback()
            except Exception as exc:
                db.session.rollback()
                logger.exception(
                    "cleide_audit_funnel_completion_failed event=%s source=%s user_id=%s audit_batch_id=%s failure_type=%s",
                    FUNNEL_EVENT_FREIGHT_CALCULATED,
                    FUNNEL_SOURCE_CLEIDE_AUDIT,
                    user_id,
                    audit_batch_id,
                    exc.__class__.__name__,
                )

    return jsonify(payload)


@cleide_audit_bp.route("/api/cleide-auditoria/audit/correction/preview", methods=["POST"])
def cleide_audit_correction_preview():
    unauthorized = _authorize_cleide_audit_documents_api()
    if unauthorized is not None:
        return unauthorized

    data = request.get_json(silent=True) or {}
    suggestion_id = str(data.get("suggestion_id") or "").strip()
    if not suggestion_id:
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": "cleide_audit_correction_invalid_payload",
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
    except CleideAuditCorrectionError as exc:
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
        logger.exception("Falha inesperada no preview de correção da Cleide Auditoria.")
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": "cleide_audit_correction_preview_failed",
                    "message": "Não foi possível simular a correção neste momento.",
                }
            ),
            500,
        )

    return jsonify({"ok": True, "preview": preview})


@cleide_audit_bp.route("/api/cleide-auditoria/audit/correction/apply", methods=["POST"])
def cleide_audit_correction_apply():
    unauthorized = _authorize_cleide_audit_documents_api()
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
                    "error_code": "cleide_audit_correction_invalid_payload",
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
    except CleideAuditCorrectionError as exc:
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
        logger.exception("Falha inesperada ao aplicar correção da Cleide Auditoria.")
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": "cleide_audit_correction_apply_failed",
                    "message": "Não foi possível aplicar a correção neste momento.",
                }
            ),
            500,
        )

    return jsonify({"ok": True, **applied})


@cleide_audit_bp.route("/api/cleide-auditoria/audit/correction/undo", methods=["POST"])
def cleide_audit_correction_undo():
    unauthorized = _authorize_cleide_audit_documents_api()
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
    except CleideAuditCorrectionError as exc:
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
        logger.exception("Falha inesperada ao desfazer correção da Cleide Auditoria.")
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": "cleide_audit_correction_undo_failed",
                    "message": "Não foi possível desfazer a correção neste momento.",
                }
            ),
            500,
        )

    return jsonify({"ok": True, **undone})


@cleide_audit_bp.route("/api/cleide-auditoria/chat", methods=["POST"])
def cleide_audit_chat():
    unauthorized = _authorize_cleide_audit_api(
        auth_message="Autenticação necessária para conversar com o AgenteAudita.",
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


@cleide_audit_bp.route("/api/cleide-auditoria/audit-chat/unlock", methods=["POST"])
def cleide_audit_insights_chat_unlock():
    unauthorized = _authorize_cleide_audit_api(
        auth_message="Autenticação necessária para liberar o chat analítico do AgenteAudita.",
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


@cleide_audit_bp.route("/api/cleide-auditoria/audit-chat", methods=["POST"])
def cleide_audit_insights_chat():
    unauthorized = _authorize_cleide_audit_api(
        auth_message="Autenticação necessária para consultas analíticas do AgenteAudita.",
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
    visual_focus = data.get("visual_focus") if isinstance(data.get("visual_focus"), dict) else None

    result = chat_cleide_audit_insights_reply(
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
