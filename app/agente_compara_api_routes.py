"""
Rotas/API documentais e chat da Agente Compara.

Upload, status, remoção e limpeza delegam ao wrapper agente_compara_doc_service.
Chat documental usa prompt/contexto próprios e governança Cleiton.
"""
from __future__ import annotations

import hashlib
import json
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
from app.agente_compara_comparison_state import (
    AgenteComparaComparisonError,
    ERROR_CARRIER_NAME_REQUIRED,
    ERROR_CARRIER_NAME_INVALID,
    add_third_table,
    advance_to_taxes,
    ensure_comparison,
    get_comparison_state,
    get_comparison_if_exists,
    get_active_table,
    proceed_with_two_tables,
    persist_comparison_state,
    public_comparison_summary,
    remove_third_table_slot,
    resolve_table_identity,
    start_comparison_for_session,
)
from app.agente_compara_doc_service import (
    AGENTE_COMPARA_CHAT_FLOW_TYPE,
    AGENTE_COMPARA_COMPARISON_CHAT_FLOW_TYPE,
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
    ERROR_TAX_CONFIG_PENDING,
    ERROR_TAX_CONFIG_USE_GLOBAL_ENDPOINT,
    ERROR_TAX_SELECTED_TABLES_REQUIRED,
    AgenteComparaTempTableError,
    build_document_status_metadata,
    clear_documents_for_session,
    reset_comparison_for_session,
    get_allowed_document_formats,
    get_agente_compara_doc_ids,
    get_document_session_totals,
    get_active_temp_table_for_session,
    maybe_cleanup_expired_cleiton_docs,
    prepare_and_register_document,
    normalize_carrier_name,
    public_comparison_summary_for_response,
    remove_document_from_session,
    run_audit_batch_for_session,
    save_temp_table_edit,
    save_comparison_tax_config,
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
from app.agente_compara_chat_context_service import (
    CAPABILITY_LOCKED,
    CHAT_NOT_READY_MESSAGE,
    ERROR_COMPARISON_CHAT_NOT_READY,
    AgenteComparaChatContextError,
    evaluate_comparison_chat_availability,
)
from app.run_agente_compara_comparison_chat import chat_agente_compara_comparison_reply
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
from app.funnel_event_service import (
    FUNNEL_EVENT_FILE_UPLOADED,
    FUNNEL_SOURCE_AGENTE_COMPARA,
    record_funnel_event,
)
from app.services.agente_compara_config_service import get_agente_compara_config
from app.services.agente_compara_config_service import get_active_calculation_bases_for_runtime
from app.extensions import db
from app.services.cleiton_doc_config_service import get_cleiton_doc_config
from app.services.cleiton_operacao_autorizacao_service import (
    avaliar_autorizacao_operacao_por_franquia,
)

logger = logging.getLogger(__name__)


def _canonical_funnel_key(prefix: str, payload: dict) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


def _build_upload_funnel_idempotency_key(
    *,
    user_id: int,
    comparison_id: str | None,
    table_id: str | None,
    slot: int | None,
    document_id: str,
) -> str:
    return _canonical_funnel_key(
        "funnel:ac:upload",
        {
            "source": FUNNEL_SOURCE_AGENTE_COMPARA,
            "event_name": FUNNEL_EVENT_FILE_UPLOADED,
            "user_id": int(user_id),
            "comparison_id": (comparison_id or "").strip() or None,
            "table_id": (table_id or "").strip() or None,
            "slot": int(slot) if slot is not None else None,
            "document_id": (document_id or "").strip(),
        },
    )


def _maybe_build_upload_funnel_payload(*, document: dict, identity: dict) -> dict | None:
    doc_id = str(document.get("doc_id") or "").strip()
    if not doc_id:
        return None
    user_id = getattr(current_user, "id", None)
    conta_id = getattr(current_user, "conta_id", None)
    franquia_id = getattr(current_user, "franquia_id", None)
    if user_id is None or conta_id is None or franquia_id is None:
        return None
    return {
        "event_name": FUNNEL_EVENT_FILE_UPLOADED,
        "source": FUNNEL_SOURCE_AGENTE_COMPARA,
        "user_id": int(user_id),
        "conta_id": int(conta_id),
        "franquia_id": int(franquia_id),
        "idempotency_key": _build_upload_funnel_idempotency_key(
            user_id=int(user_id),
            comparison_id=identity.get("comparison_id"),
            table_id=identity.get("table_id"),
            slot=identity.get("slot"),
            document_id=doc_id,
        ),
        "document_id": doc_id,
        "comparison_id": (identity.get("comparison_id") or "").strip() or None,
        "execution_id": (request.headers.get("X-Execution-ID") or request.form.get("execution_id") or "").strip() or None,
        "correlation_id": (request.headers.get("X-Correlation-ID") or "").strip() or None,
        "metadata_json": {
            "table_id": (identity.get("table_id") or "").strip() or None,
            "slot": int(identity.get("slot")) if identity.get("slot") is not None else None,
        },
    }

agente_compara_api_bp = Blueprint("agente_compara_api", __name__)


def _session_payload() -> dict:
    totals = get_document_session_totals()
    return {
        "count": totals["active_count"],
        "max_files": totals["max_files_per_session"],
        "total_bytes": totals["total_bytes"],
        "session_max_bytes": totals["session_max_bytes"],
    }


def _parse_table_identity_from_request(*, json_body: dict | None = None) -> dict:
    body = json_body if isinstance(json_body, dict) else {}
    comparison_id = (request.args.get("comparison_id") or body.get("comparison_id") or request.form.get("comparison_id") or "").strip() or None
    table_id = (request.args.get("table_id") or body.get("table_id") or request.form.get("table_id") or "").strip() or None
    slot_raw = request.args.get("slot") or body.get("slot") or request.form.get("slot")
    slot = None
    if slot_raw is not None and str(slot_raw).strip() != "":
        try:
            slot = int(slot_raw)
        except (TypeError, ValueError):
            slot = None
    return {
        "comparison_id": comparison_id,
        "table_id": table_id,
        "slot": slot,
    }


def _http_status_for_comparison_error(error_code: str) -> int:
    if error_code in {
        "agente_compara_comparison_not_found",
        "agente_compara_table_not_found",
    }:
        return 404
    if error_code in {
        "agente_compara_comparison_scope_mismatch",
        "agente_compara_table_scope_mismatch",
        "agente_compara_table_slot_mismatch",
        "agente_compara_comparison_step_invalid",
    }:
        return 409
    if error_code == "agente_compara_table_locked":
        return 403
    if error_code == "agente_compara_table_max_slots":
        return 400
    if error_code in {
        ERROR_CARRIER_NAME_REQUIRED,
        ERROR_CARRIER_NAME_INVALID,
    }:
        return 400
    return 400


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


def _comparison_chat_success_payload(result: dict, *, cached: bool = False) -> dict:
    payload = {
        "ok": True,
        "answer": result.get("answer") or "",
        "flow_type": result.get("flow_type") or AGENTE_COMPARA_COMPARISON_CHAT_FLOW_TYPE,
        "deterministic": bool(result.get("deterministic")),
        "scope": result.get("scope"),
        "basis": result.get("basis") if isinstance(result.get("basis"), dict) else {},
        "warnings": list(result.get("warnings") or []),
        "chat_available": bool(result.get("chat_available", True)),
        "capability": result.get("capability") or "ready",
    }
    if cached:
        payload["cached"] = True
    return payload


def _comparison_chat_error_payload(result: dict, *, request_id: str | None = None) -> dict:
    error_code = result.get("error_code") or result.get("error") or "processing_failed"
    payload = {
        "ok": False,
        "error": True,
        "error_code": error_code,
        "message": result.get("message") or "Não foi possível processar a mensagem.",
        "retryable": bool(result.get("retryable", False)),
        "chat_available": bool(result.get("chat_available", False)),
        "capability": result.get("capability") or CAPABILITY_LOCKED,
    }
    if result.get("reason") is not None:
        payload["reason"] = result.get("reason")
    if request_id:
        payload["request_id"] = request_id
    return payload


def _http_status_for_comparison_chat_error(error_code: str) -> int:
    if error_code in {
        "agente_compara_comparison_not_found",
        "agente_compara_comparison_chat_context_exceeded",
    }:
        return 404 if error_code.endswith("not_found") else 413
    if error_code in {
        "agente_compara_comparison_scope_mismatch",
        "COMPARISON_CHAT_NOT_READY",
    }:
        return 409
    if error_code == "chat_disabled":
        return 403
    if error_code == "invalid_message":
        return 400
    if error_code in {
        "service_unavailable",
        "provider_not_configured",
        "provider_initialization_failed",
        "provider_request_failed",
        "provider_timeout",
        "provider_empty_response",
        "provider_invalid_response",
    }:
        return 503
    if error_code in {
        "context_build_failed",
        "prompt_build_failed",
        "processing_failed",
    }:
        return 500
    return 400


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

    identity = _parse_table_identity_from_request()
    try:
        carrier_name = normalize_carrier_name(request.form.get("carrier_name"))
    except AgenteComparaComparisonError as exc:
        return (
            jsonify({"ok": False, "error_code": exc.error_code, "message": exc.message}),
            _http_status_for_comparison_error(exc.error_code),
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
            comparison_id=identity["comparison_id"],
            table_id=identity["table_id"],
            slot=identity["slot"],
            carrier_name=carrier_name,
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
    except AgenteComparaComparisonError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": exc.error_code,
                    "message": exc.message,
                }
            ),
            _http_status_for_comparison_error(exc.error_code),
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

    logger.info(
        "Agente Compara upload: doc_id=%s comparison_id=%s table_id=%s slot=%s carrier_name=%s",
        document.get("doc_id"),
        identity.get("comparison_id"),
        identity.get("table_id"),
        identity.get("slot"),
        carrier_name,
    )

    user_scope = getattr(current_user, "id", None)
    franquia_scope = getattr(current_user, "franquia_id", None)
    try:
        trigger_temp_table_extraction_for_session(
            user_scope=user_scope,
            franquia_scope=franquia_scope,
            comparison_id=identity["comparison_id"],
            table_id=identity["table_id"],
            slot=identity["slot"],
        )
    except Exception:
        logger.exception(
            "Agente Compara temp_table: falha na extração pós-upload; upload preservado."
        )

    temp_table = None
    comparison = None
    try:
        cmp_state = ensure_comparison()
        _, table_entry = resolve_table_identity(
            comparison_id=identity["comparison_id"],
            table_id=identity["table_id"],
            slot=identity["slot"],
            auto_create=True,
        )
        temp_table = get_active_temp_table_for_session(table_id=table_entry["table_id"])
        comparison = public_comparison_summary(cmp_state)
    except Exception:
        logger.exception("Agente Compara temp_table: falha ao ler temp_table após upload.")

    payload = {
        "ok": True,
        "document": document,
        "session": _session_payload(),
        "allowed_formats": get_allowed_document_formats(),
        "calculation_bases": get_active_calculation_bases_for_runtime(
            audit_cfg.calculation_bases
        ),
        "temp_table": temp_table,
        "comparison": comparison,
    }
    funnel_identity = dict(identity)
    if not funnel_identity.get("comparison_id") and isinstance(comparison, dict):
        funnel_identity["comparison_id"] = comparison.get("comparison_id")
    funnel_payload = _maybe_build_upload_funnel_payload(document=document, identity=funnel_identity)
    if funnel_payload is not None:
        started_funnel_tx = False
        try:
            orm_session = db.session()
            if not orm_session.in_transaction():
                orm_session.begin()
                started_funnel_tx = True
            funnel_result = record_funnel_event(**funnel_payload)
            if funnel_result.get("created") is True:
                db.session.commit()
                payload["funnel_event"] = {
                    "event_name": FUNNEL_EVENT_FILE_UPLOADED,
                    "source": FUNNEL_SOURCE_AGENTE_COMPARA,
                    "allow_meta_pixel": True,
                    "is_first_audit": False,
                }
            elif started_funnel_tx:
                db.session.rollback()
        except Exception as exc:
            db.session.rollback()
            logger.exception(
                "agente_compara_funnel_upload_failed event=%s source=%s user_id=%s comparison_id=%s document_id=%s failure_type=%s",
                FUNNEL_EVENT_FILE_UPLOADED,
                FUNNEL_SOURCE_AGENTE_COMPARA,
                getattr(current_user, "id", None),
                identity.get("comparison_id"),
                document.get("doc_id"),
                exc.__class__.__name__,
            )

    return jsonify(payload)


@agente_compara_api_bp.route("/api/agente-compara/documents/status", methods=["GET"])
def agente_compara_documents_status():
    unauthorized = _authorize_agente_compara_documents_api()
    if unauthorized is not None:
        return unauthorized

    identity = _parse_table_identity_from_request()
    try:
        metadata = build_document_status_metadata(
            comparison_id=identity["comparison_id"],
            table_id=identity["table_id"],
            slot=identity["slot"],
        )
    except AgenteComparaComparisonError as exc:
        return (
            jsonify({"ok": False, "error_code": exc.error_code, "message": exc.message}),
            _http_status_for_comparison_error(exc.error_code),
        )
    cleiton_cfg = get_cleiton_doc_config()
    audit_cfg = get_agente_compara_config()
    cleiton_upload_enabled = bool(cleiton_cfg.upload_enabled)
    agente_compara_upload_enabled = bool(audit_cfg.upload_enabled)
    return jsonify(
        {
            "ok": True,
            "documents": metadata["documents"],
            "temp_table": metadata.get("temp_table"),
            "comparison": metadata.get("comparison"),
            "has_active_comparison": bool(metadata.get("has_active_comparison")),
            "current_step": (metadata.get("comparison") or {}).get("current_step")
            if isinstance(metadata.get("comparison"), dict)
            else None,
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

    identity = _parse_table_identity_from_request()
    try:
        result = remove_document_from_session(
            doc_id,
            comparison_id=identity["comparison_id"],
            table_id=identity["table_id"],
            slot=identity["slot"],
        )
    except AgenteComparaComparisonError as exc:
        return (
            jsonify({"ok": False, "error_code": exc.error_code, "message": exc.message}),
            _http_status_for_comparison_error(exc.error_code),
        )
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

    identity = _parse_table_identity_from_request(json_body=request.get_json(silent=True))
    global_clear = bool((request.get_json(silent=True) or {}).get("global_clear"))
    try:
        result = clear_documents_for_session(
            comparison_id=identity["comparison_id"],
            table_id=identity["table_id"],
            slot=identity["slot"],
            global_clear=global_clear,
        )
    except AgenteComparaComparisonError as exc:
        return (
            jsonify({"ok": False, "error_code": exc.error_code, "message": exc.message}),
            _http_status_for_comparison_error(exc.error_code),
        )
    payload = {
        "ok": True,
        "session": _session_payload(),
    }
    if "removed_from_store" in result:
        payload["removed_from_store"] = result["removed_from_store"]
    if "removed_from_session" in result:
        payload["removed_from_session"] = result["removed_from_session"]
    return jsonify(payload)


@agente_compara_api_bp.route("/api/agente-compara/comparison/start", methods=["POST"])
def agente_compara_comparison_start():
    """
    Único criador oficial SEM_COMPARACAO → PREPARE_TABLE_1.

    Não consome IA, não gera billing, não aceita identidade externa.
    Idempotente por sessão.
    """
    unauthorized = _authorize_agente_compara_documents_api()
    if unauthorized is not None:
        return unauthorized

    # Body opcional/ignorado: start opera apenas sobre a sessão autenticada.
    _ = request.get_json(silent=True)

    result = start_comparison_for_session()
    state = result["state"]
    summary = public_comparison_summary(state)
    comparison_started = bool(result.get("comparison_started"))
    idempotent_replay = bool(result.get("idempotent_replay"))

    if comparison_started:
        logger.info(
            "Agente Compara comparison_started: comparison_id=%s current_step=%s action=comparison_started source_agent=agente_compara",
            state.get("comparison_id"),
            state.get("current_step"),
        )

    return jsonify(
        {
            "ok": True,
            "comparison_started": comparison_started,
            "idempotent_replay": idempotent_replay,
            "comparison": summary,
            "current_step": summary.get("current_step"),
            "has_active_comparison": True,
            "documents": [],
            "temp_table": None,
            "session": _session_payload(),
        }
    )


@agente_compara_api_bp.route("/api/agente-compara/comparison/reset", methods=["POST"])
def agente_compara_comparison_reset():
    """Reinicia a comparação: remove documentos, temp tables, tax/coverage/arquivo e o estado."""
    unauthorized = _authorize_agente_compara_documents_api()
    if unauthorized is not None:
        return unauthorized

    data = request.get_json(silent=True) or {}
    identity = _parse_table_identity_from_request(json_body=data)
    try:
        result = reset_comparison_for_session(comparison_id=identity.get("comparison_id"))
    except AgenteComparaComparisonError as exc:
        return (
            jsonify({"ok": False, "error_code": exc.error_code, "message": exc.message}),
            _http_status_for_comparison_error(exc.error_code),
        )
    return jsonify(
        {
            "ok": True,
            "comparison_reset": True,
            "previous_comparison_id": result.get("previous_comparison_id"),
            "comparison": None,
            "documents": [],
            "temp_table": None,
            "current_step": None,
            "has_active_comparison": False,
            "session": _session_payload(),
        }
    )


@agente_compara_api_bp.route("/api/agente-compara/comparison/proceed-two-tables", methods=["POST"])
def agente_compara_comparison_proceed_two_tables():
    unauthorized = _authorize_agente_compara_documents_api()
    if unauthorized is not None:
        return unauthorized
    data = request.get_json(silent=True) or {}
    identity = _parse_table_identity_from_request(json_body=data)
    try:
        state = get_comparison_state(session)
        if state is None:
            raise AgenteComparaComparisonError("agente_compara_comparison_not_found", "Comparação não encontrada.")
        if identity["comparison_id"] and identity["comparison_id"] != state.get("comparison_id"):
            raise AgenteComparaComparisonError(
                "agente_compara_comparison_scope_mismatch",
                "comparison_id não pertence à sessão.",
            )
        state = proceed_with_two_tables(state)
    except AgenteComparaComparisonError as exc:
        return jsonify({"ok": False, "error_code": exc.error_code, "message": exc.message}), _http_status_for_comparison_error(exc.error_code)
    summary = public_comparison_summary(state)
    return jsonify(
        {
            "ok": True,
            "comparison": summary,
            "next_step": summary.get("current_step"),
        }
    )


@agente_compara_api_bp.route("/api/agente-compara/comparison/add-third-table", methods=["POST"])
def agente_compara_comparison_add_third_table():
    unauthorized = _authorize_agente_compara_documents_api()
    if unauthorized is not None:
        return unauthorized
    data = request.get_json(silent=True) or {}
    identity = _parse_table_identity_from_request(json_body=data)
    try:
        state = get_comparison_state(session)
        if state is None:
            raise AgenteComparaComparisonError("agente_compara_comparison_not_found", "Comparação não encontrada.")
        if identity["comparison_id"] and identity["comparison_id"] != state.get("comparison_id"):
            raise AgenteComparaComparisonError(
                "agente_compara_comparison_scope_mismatch",
                "comparison_id não pertence à sessão.",
            )
        state = add_third_table(state)
    except AgenteComparaComparisonError as exc:
        return jsonify({"ok": False, "error_code": exc.error_code, "message": exc.message}), _http_status_for_comparison_error(exc.error_code)
    return jsonify({"ok": True, "comparison": public_comparison_summary(state)})


@agente_compara_api_bp.route("/api/agente-compara/comparison/set-active-table", methods=["POST"])
def agente_compara_comparison_set_active_table():
    unauthorized = _authorize_agente_compara_documents_api()
    if unauthorized is not None:
        return unauthorized
    data = request.get_json(silent=True) or {}
    identity = _parse_table_identity_from_request(json_body=data)
    try:
        state, table_entry = resolve_table_identity(
            comparison_id=identity["comparison_id"],
            table_id=identity["table_id"],
            slot=identity["slot"],
            auto_create=False,
        )
        state["active_table_id"] = table_entry["table_id"]
        state = persist_comparison_state(state)
    except AgenteComparaComparisonError as exc:
        return jsonify({"ok": False, "error_code": exc.error_code, "message": exc.message}), _http_status_for_comparison_error(exc.error_code)
    temp_table = get_active_temp_table_for_session(table_id=table_entry["table_id"])
    return jsonify(
        {
            "ok": True,
            "comparison": public_comparison_summary_for_response(state),
            "temp_table": temp_table,
            "documents": build_document_status_metadata(
                comparison_id=state.get("comparison_id"),
                table_id=table_entry["table_id"],
            ).get("documents"),
        }
    )


@agente_compara_api_bp.route("/api/agente-compara/comparison/taxes", methods=["POST"])
def agente_compara_comparison_taxes_save():
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
        result = save_comparison_tax_config(
            data,
            user_scope=user_scope,
            franquia_scope=franquia_scope,
        )
    except AgenteComparaTempTableError as exc:
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
        logger.exception("Falha inesperada ao salvar impostos globais da Agente Compara.")
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": "comparison_tax_config_save_failed",
                    "message": "Não foi possível salvar a configuração fiscal do cenário.",
                }
            ),
            500,
        )

    return jsonify(
        {
            "ok": True,
            "comparison": result.get("comparison"),
            "tax_config": result.get("tax_config"),
            "can_advance_to_coverage": bool(result.get("can_advance_to_coverage")),
        }
    )


def _http_status_for_calculation_error(error_code: str) -> int:
    if error_code in {
        "agente_compara_comparison_not_found",
    }:
        return 404
    if error_code in {
        "agente_compara_comparison_scope_mismatch",
        "agente_compara_comparison_step_invalid",
        "agente_compara_calculation_execution_conflict",
        "agente_compara_calculation_execution_in_progress",
        "calculation_input_changed",
        "agente_compara_calculation_not_ready",
    }:
        return 409
    if error_code in {
        "agente_compara_calculation_execution_id_required",
        "agente_compara_calculation_execution_id_invalid",
    }:
        return 400
    if error_code in {
        "calculation_result_too_large",
        "calculation_memory_too_large",
    }:
        return 413
    if error_code.endswith("serialization_failed") or error_code.endswith("validation_failed"):
        return 422
    if error_code.endswith("write_failed") or error_code.endswith("checksum_failed"):
        return 500
    return 400


@agente_compara_api_bp.route("/api/agente-compara/comparison/calculate", methods=["POST"])
def agente_compara_comparison_calculate():
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

    comparison_id = (data.get("comparison_id") or "").strip() or None
    execution_id = (data.get("execution_id") or "").strip() or None
    if not execution_id:
        execution_id = (request.headers.get("X-Execution-ID") or "").strip() or None
    schema_version = data.get("schema_version")

    from app.agente_compara_calculation_execution_service import (
        AgenteComparaCalculationExecutionError,
        execute_comparison_calculation,
    )

    try:
        result = execute_comparison_calculation(
            comparison_id=comparison_id,
            execution_id=execution_id,
            schema_version=schema_version,
        )
    except AgenteComparaCalculationExecutionError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": exc.error_code,
                    "message": exc.message,
                    "status": None,
                    "result": None,
                    "error_stage": getattr(exc, "error_stage", None),
                    "artifact_type": getattr(exc, "artifact_type", None),
                    "retryable": bool(getattr(exc, "retryable", False)),
                    "failed_table_name": getattr(exc, "failed_table_name", None),
                    "failed_table_id": getattr(exc, "failed_table_id", None),
                    "failed_slot": getattr(exc, "failed_slot", None),
                    "failure_origin": getattr(exc, "failure_origin", None),
                    "failure_code": getattr(exc, "failure_code", None),
                    "credit_disposition": getattr(exc, "credit_disposition", None),
                    "retry_of": getattr(exc, "retry_of", None),
                    "is_free_retry": bool(getattr(exc, "is_free_retry", False)),
                    "safe_message": getattr(exc, "safe_message", exc.message),
                }
            ),
            int(exc.http_status or _http_status_for_calculation_error(exc.error_code)),
        )
    except Exception:
        logger.exception("Falha inesperada no cálculo comparativo da Agente Compara.")
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": "agente_compara_calculation_failed",
                    "message": "Não foi possível concluir o cálculo comparativo.",
                    "status": "CALCULATION_FAILED",
                    "result": None,
                }
            ),
            500,
        )

    if not result.get("ok", True) and result.get("status") == "CALCULATION_FAILED":
        status_code = _http_status_for_calculation_error(str(result.get("error_code") or ""))
        if str(result.get("error_code") or "").endswith("failed") or str(result.get("error_code") or "") == "agente_compara_calculation_failed":
            status_code = 500
        return jsonify(result), status_code
    return jsonify(result)


@agente_compara_api_bp.route("/api/agente-compara/comparison/calculation-memory", methods=["GET"])
def agente_compara_comparison_calculation_memory_get():
    unauthorized = _authorize_agente_compara_documents_api()
    if unauthorized is not None:
        return unauthorized

    comparison_id = (request.args.get("comparison_id") or "").strip() or None
    memory_ref = (request.args.get("memory_ref") or "").strip() or None
    table_id = (request.args.get("table_id") or "").strip() or None
    row_index = request.args.get("row_index", type=int)
    if not comparison_id or not memory_ref:
        return jsonify({"ok": False, "message": "Par?metros obrigat?rios ausentes."}), 400

    from app.agente_compara_calculation_execution_service import get_comparison_calculation_status
    from app.agente_compara_comparison_calculation_service import hydrate_memory_item
    from app.agente_compara_calculation_result_storage import load_comparison_calculation_memory_payload

    status = get_comparison_calculation_status(comparison_id=comparison_id)
    calc = status.get("calculation") or {}
    try:
        payload = load_comparison_calculation_memory_payload(
            storage_key=(calc.get("memory_storage_key") or "").strip(),
            comparison_id=comparison_id,
            fingerprint=(calc.get("request_fingerprint") or "").strip(),
            expected_checksum=(calc.get("memory_checksum") or None),
        )
    except Exception:
        return jsonify({"ok": False, "message": "N?o foi poss?vel carregar a mem?ria de c?lculo."}), 409
    item = ((payload.get("items") or {}) if isinstance(payload, dict) else {}).get(memory_ref)
    if not isinstance(item, dict):
        return jsonify({"ok": False, "message": "Mem?ria de c?lculo n?o encontrada."}), 404
    item = hydrate_memory_item(item, payload)
    if table_id and str(item.get("table_id") or "") != table_id:
        return jsonify({"ok": False, "message": "Mem?ria de c?lculo inv?lida."}), 409
    if row_index is not None and int(item.get("row_index") or -1) != int(row_index):
        return jsonify({"ok": False, "message": "Mem?ria de c?lculo inv?lida."}), 409
    return jsonify({"ok": True, "memory_ref": memory_ref, "memory": item})


@agente_compara_api_bp.route("/api/agente-compara/comparison/calculation", methods=["GET"])
def agente_compara_comparison_calculation_get():
    unauthorized = _authorize_agente_compara_documents_api()
    if unauthorized is not None:
        return unauthorized

    comparison_id = (request.args.get("comparison_id") or "").strip() or None

    from app.agente_compara_calculation_execution_service import (
        AgenteComparaCalculationExecutionError,
        get_comparison_calculation_status,
    )

    try:
        result = get_comparison_calculation_status(comparison_id=comparison_id)
    except AgenteComparaCalculationExecutionError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": exc.error_code,
                    "message": exc.message,
                }
            ),
            int(exc.http_status or _http_status_for_calculation_error(exc.error_code)),
        )
    except Exception:
        logger.exception("Falha inesperada ao ler cálculo comparativo da Agente Compara.")
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": "agente_compara_calculation_read_failed",
                    "message": "Não foi possível recuperar o estado do cálculo comparativo.",
                }
            ),
            500,
        )
    return jsonify(result)


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
        if getattr(exc, "validation", None):
            payload["validation"] = exc.validation
            payload["error"] = "TEMP_TABLE_HAS_BLOCKING_ISSUES"
        if exc.error_code == ERROR_TAX_CONFIG_PENDING and exc.errors:
            payload["pending_tax_tables"] = exc.errors
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

    return jsonify(
        {
            "ok": True,
            "temp_table": temp_table,
            "comparison": temp_table.get("comparison") if isinstance(temp_table, dict) else None,
            "idempotent_replay": bool(
                isinstance(temp_table, dict) and temp_table.get("idempotent_replay")
            ),
        }
    )


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
        logger.exception("Falha inesperada no upload do arquivo para comparação da Agente Compara.")
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": ERROR_UPLOAD_FAILED,
                    "message": "Não foi possível processar o arquivo para comparação.",
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
        cmp_state = get_comparison_if_exists()
        active_table = get_active_table(cmp_state) if cmp_state else None
        active_table_id = active_table.get("table_id") if active_table else None
        doc_ctx = build_agente_compara_document_context_for_chat(session, table_id=active_table_id)
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
        source_doc_ids=get_agente_compara_doc_ids(session, table_id=active_table_id),
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


@agente_compara_api_bp.route("/api/agente-compara/comparison-chat", methods=["POST"])
def agente_compara_comparison_chat():
    unauthorized = _authorize_agente_compara_api(
        auth_message="Autenticação necessária para o chat inteligente da comparação.",
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
        raw_message = data.get("question")
    if raw_message is None:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "invalid_message",
                    "message": "Campo 'message' ou 'question' é obrigatório.",
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

    message_limit = int(
        getattr(audit_cfg, "comparison_chat_question_max_chars", None) or audit_cfg.question_max_chars
    )
    message_text = raw_message.strip()
    if len(message_text) > message_limit:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "invalid_message",
                    "message": f"Mensagem excede o limite de {message_limit} caracteres.",
                }
            ),
            400,
        )

    history_limit = int(
        getattr(audit_cfg, "comparison_chat_history_max_items", None) or audit_cfg.chat_max_history
    )
    history = sanitize_chat_history(data.get("history"), max_history=history_limit)
    request_id = normalize_chat_request_id(data.get("request_id"))
    comparison_id = data.get("comparison_id")
    if comparison_id is not None and not isinstance(comparison_id, str):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "invalid_comparison_id",
                    "error_code": "invalid_comparison_id",
                    "message": "Campo 'comparison_id' deve ser string.",
                }
            ),
            400,
        )
    ui_context = data.get("ui_context") if isinstance(data.get("ui_context"), dict) else {}
    visual_focus = data.get("visual_focus") if isinstance(data.get("visual_focus"), dict) else None
    resolved_comparison_id = (comparison_id or "").strip() or None

    # Gate pré-READY: bloqueia antes de context builder completo, Gemini, billing e cache analítico.
    try:
        availability = evaluate_comparison_chat_availability(
            session_obj=session,
            comparison_id=resolved_comparison_id,
        )
    except AgenteComparaChatContextError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": exc.error_code,
                    "error_code": exc.error_code,
                    "message": exc.message,
                    "chat_available": False,
                    "capability": CAPABILITY_LOCKED,
                }
            ),
            int(exc.http_status or 409),
        )
    if not availability.get("chat_available"):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": ERROR_COMPARISON_CHAT_NOT_READY,
                    "error_code": ERROR_COMPARISON_CHAT_NOT_READY,
                    "message": CHAT_NOT_READY_MESSAGE,
                    "chat_available": False,
                    "capability": CAPABILITY_LOCKED,
                    "reason": availability.get("reason") or "comparison_not_ready",
                }
            ),
            409,
        )

    result = chat_agente_compara_comparison_reply(
        message_text,
        history,
        session_obj=session,
        comparison_id=resolved_comparison_id,
        request_id=request_id,
        ui_context=ui_context,
        visual_focus=visual_focus,
        max_history=history_limit,
        question_max_chars=message_limit,
        fallback_message=audit_cfg.fallback_message,
        skip_availability_gate=True,
    )

    if result.get("error"):
        error_code = result.get("error_code") or result.get("error") or "processing_failed"
        status = int(result.get("http_status") or _http_status_for_comparison_chat_error(error_code))
        if error_code == "invalid_message":
            status = 400
        if error_code == ERROR_COMPARISON_CHAT_NOT_READY:
            status = 409
        payload = _comparison_chat_error_payload(result, request_id=request_id)
        return jsonify(payload), status

    payload = _comparison_chat_success_payload(result, cached=bool(result.get("cached")))
    payload["request_id"] = request_id
    return jsonify(payload)
