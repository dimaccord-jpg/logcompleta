"""
Carrega contexto fechado da auditoria processada para o chat analítico pós-BI.

Fonte de verdade: registro completo da tabela temporária no backend (tt_*.json).
Não confia em dados enviados pelo navegador.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.agente_compara_doc_service import (
    AUDIT_BATCH_STATUS_PROCESSED,
    TEMP_TABLE_STATUS_DISCARDED,
    TEMP_TABLE_STATUS_EXPIRED,
    _audit_batch_effective_needs_reprocess,
    _public_audit_bi,
    clear_temp_table_session_refs,
    get_cleiton_doc_config,
    get_temp_table_id,
    load_temp_table_record,
    sync_temp_table_with_session_documents,
)


ERROR_INSIGHTS_NO_TEMP_TABLE = "agente_compara_insights_no_temp_table"
ERROR_INSIGHTS_TEMP_TABLE_EXPIRED = "agente_compara_insights_temp_table_expired"
ERROR_INSIGHTS_BATCH_NOT_FOUND = "agente_compara_insights_batch_not_found"
ERROR_INSIGHTS_BATCH_NOT_PROCESSED = "agente_compara_insights_batch_not_processed"
ERROR_INSIGHTS_BATCH_NO_RESULTS = "agente_compara_insights_batch_no_results"
ERROR_INSIGHTS_CHAT_LOCKED = "agente_compara_insights_chat_locked"

AGENTE_COMPARA_INSIGHTS_CHAT_UNLOCK_SESSION_KEY = "agente_compara_insights_chat_unlock"
AGENTE_COMPARA_INSIGHTS_CONVERSATION_FOCUS_SESSION_KEY = "agente_compara_insights_conversation_focus"

_AUDIT_RESULT_IDENTIFIER_KEYS = (
    "row_index",
    "numero_documento",
    "document_number",
)
_AUDIT_RESULT_VALUE_KEYS = (
    "charged_freight",
    "expected_freight",
    "divergence_value",
    "status",
    "weight_freight",
)


def _has_useful_audit_field_value(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, bool):
        return False
    return True


def is_minimally_valid_audit_result(result) -> bool:
    """Exige dict não vazio com row_index ou campo mínimo de auditoria."""
    if not isinstance(result, dict) or not result:
        return False

    row_index = result.get("row_index")
    if row_index is not None and not isinstance(row_index, bool):
        try:
            int(row_index)
            return True
        except (TypeError, ValueError):
            pass

    for key in _AUDIT_RESULT_IDENTIFIER_KEYS:
        if key == "row_index":
            continue
        if _has_useful_audit_field_value(result.get(key)):
            return True

    return any(_has_useful_audit_field_value(result.get(key)) for key in _AUDIT_RESULT_VALUE_KEYS)


def has_minimally_valid_audit_results(results) -> bool:
    if not isinstance(results, list) or not results:
        return False
    return any(is_minimally_valid_audit_result(item) for item in results)


def insights_batch_scope(bundle: dict) -> str:
    temp_table_id = str(bundle.get("temp_table_id") or "").strip()
    audit_batch_id = str(bundle.get("audit_batch_id") or "").strip()
    processed_at = str(bundle.get("processed_at") or "").strip()
    return f"{temp_table_id}:{audit_batch_id}:{processed_at}"


def clear_insights_chat_unlock(session_obj) -> None:
    session_obj.pop(AGENTE_COMPARA_INSIGHTS_CHAT_UNLOCK_SESSION_KEY, None)
    if hasattr(session_obj, "modified"):
        session_obj.modified = True


def mark_insights_chat_unlocked(session_obj, bundle: dict) -> dict:
    scope = insights_batch_scope(bundle)
    payload = {
        "scope": scope,
        "temp_table_id": bundle.get("temp_table_id"),
        "audit_batch_id": bundle.get("audit_batch_id"),
        "processed_at": bundle.get("processed_at"),
    }
    session_obj[AGENTE_COMPARA_INSIGHTS_CHAT_UNLOCK_SESSION_KEY] = payload
    if hasattr(session_obj, "modified"):
        session_obj.modified = True
    return payload


def is_insights_chat_unlocked(session_obj, bundle: dict) -> bool:
    raw = session_obj.get(AGENTE_COMPARA_INSIGHTS_CHAT_UNLOCK_SESSION_KEY)
    if not isinstance(raw, dict):
        return False
    return str(raw.get("scope") or "").strip() == insights_batch_scope(bundle)


def get_conversation_focus(session_obj, batch_scope: str) -> dict | None:
    """Retorna foco conversacional somente se ainda pertencer ao batch_scope atual."""
    raw = session_obj.get(AGENTE_COMPARA_INSIGHTS_CONVERSATION_FOCUS_SESSION_KEY)
    if not isinstance(raw, dict):
        return None
    if str(raw.get("batch_scope") or "").strip() != str(batch_scope or "").strip():
        return None
    return dict(raw)


def clear_conversation_focus(session_obj) -> None:
    session_obj.pop(AGENTE_COMPARA_INSIGHTS_CONVERSATION_FOCUS_SESSION_KEY, None)
    if hasattr(session_obj, "modified"):
        session_obj.modified = True


def set_conversation_focus(
    session_obj,
    *,
    batch_scope: str,
    document_number: str | None = None,
    row_index=None,
    last_intent: str | None = None,
    last_metric: str | None = None,
    visual_focus: dict | None = None,
    last_ranking_limit: int | None = None,
    last_ranking_intent: str | None = None,
    preserve_document: bool = False,
) -> dict:
    previous = session_obj.get(AGENTE_COMPARA_INSIGHTS_CONVERSATION_FOCUS_SESSION_KEY)
    previous = previous if isinstance(previous, dict) else {}
    payload = {
        "batch_scope": str(batch_scope or "").strip(),
        "document_number": str(document_number or "").strip() or None,
        "row_index": row_index,
        "last_intent": last_intent,
        "last_metric": last_metric,
        "visual_focus": visual_focus if isinstance(visual_focus, dict) else {},
        "last_ranking_limit": last_ranking_limit,
        "last_ranking_intent": last_ranking_intent,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if preserve_document and previous.get("batch_scope") == payload["batch_scope"]:
        if payload["document_number"] is None and previous.get("document_number") is not None:
            payload["document_number"] = previous.get("document_number")
            payload["row_index"] = previous.get("row_index")
    if last_ranking_limit is None and previous.get("batch_scope") == payload["batch_scope"]:
        # Mantém memória de ranking se esta atualização for só de documento.
        if previous.get("last_ranking_limit") is not None and last_ranking_intent is None:
            payload["last_ranking_limit"] = previous.get("last_ranking_limit")
            payload["last_ranking_intent"] = previous.get("last_ranking_intent")
    session_obj[AGENTE_COMPARA_INSIGHTS_CONVERSATION_FOCUS_SESSION_KEY] = payload
    if hasattr(session_obj, "modified"):
        session_obj.modified = True
    return payload


def build_coverage_summary(coverage_table) -> dict[str, Any] | None:
    """Visão sanitizada e limitada da coverage_table para explicar falhas de cobertura."""
    if not isinstance(coverage_table, dict):
        return None
    rows = coverage_table.get("rows") if isinstance(coverage_table.get("rows"), list) else []
    if not rows:
        return {"row_count": 0, "sample_regions": [], "sample_cities": []}
    regions: list[str] = []
    cities: list[str] = []
    for row in rows[:40]:
        if not isinstance(row, dict):
            continue
        for key in ("freight_region", "regiao", "região", "region"):
            value = str(row.get(key) or "").strip()
            if value and value not in regions:
                regions.append(value)
        for key in ("destination_city", "cidade", "city"):
            value = str(row.get(key) or "").strip()
            if value and value not in cities:
                cities.append(value)
        if len(regions) >= 12 and len(cities) >= 12:
            break
    return {
        "row_count": len(rows),
        "sample_regions": regions[:12],
        "sample_cities": cities[:12],
    }


def _results_by_row_index(results: list | None) -> dict[int, dict]:
    indexed: dict[int, dict] = {}
    if not isinstance(results, list):
        return indexed
    for result in results:
        if not isinstance(result, dict):
            continue
        row_index = result.get("row_index")
        if isinstance(row_index, bool) or row_index is None:
            continue
        try:
            indexed[int(row_index)] = result
        except (TypeError, ValueError):
            continue
    return indexed


def _normalized_by_row_index(normalized_rows: list | None) -> dict[int, dict]:
    indexed: dict[int, dict] = {}
    if not isinstance(normalized_rows, list):
        return indexed
    for row in normalized_rows:
        if not isinstance(row, dict):
            continue
        row_index = row.get("row_index")
        if isinstance(row_index, bool) or row_index is None:
            continue
        try:
            indexed[int(row_index)] = row
        except (TypeError, ValueError):
            continue
    return indexed


def merge_audit_row(normalized_row: dict | None, result: dict | None) -> dict:
    norm = normalized_row if isinstance(normalized_row, dict) else {}
    res = result if isinstance(result, dict) else {}
    doc_number = norm.get("document_number")
    if doc_number is None:
        doc_number = res.get("numero_documento")
    doc_number_str = "" if doc_number is None else str(doc_number)
    return {
        "row_index": norm.get("row_index") if norm.get("row_index") is not None else res.get("row_index"),
        "document_number": doc_number_str,
        "carrier": norm.get("carrier"),
        "origin_uf": norm.get("origin_uf"),
        "destination_uf": norm.get("destination_uf") or res.get("destination_uf"),
        "destination_city": norm.get("destination_city") or res.get("destination_city"),
        "issue_date": norm.get("issue_date"),
        "audited_weight": norm.get("audited_weight") if norm.get("audited_weight") is not None else res.get("audited_weight"),
        "charged_freight": res.get("charged_freight") if res.get("charged_freight") is not None else norm.get("charged_freight"),
        "expected_freight": res.get("expected_freight"),
        "weight_freight": res.get("weight_freight"),
        "freight_value_amount": res.get("freight_value_amount"),
        "route_toll_amount": res.get("route_toll_amount"),
        "accessorial_fees_amount": res.get("accessorial_fees_amount"),
        "accessorial_percent_fees_amount": res.get("accessorial_percent_fees_amount"),
        "divergence_value": res.get("divergence_value"),
        "status": res.get("status"),
        "reason_code": res.get("reason_code"),
        "calculation_basis": res.get("calculation_basis"),
        "calculation_details": res.get("calculation_details"),
        "calculation_components": dict(res.get("calculation_components") or {}),
        "freight_region": res.get("freight_region"),
        "diagnostic": res.get("diagnostic"),
    }


def build_merged_rows(audit_batch: dict) -> list[dict]:
    normalized_rows = audit_batch.get("normalized_rows") if isinstance(audit_batch.get("normalized_rows"), list) else []
    results = audit_batch.get("results") if isinstance(audit_batch.get("results"), list) else []
    norm_index = _normalized_by_row_index(normalized_rows)
    res_index = _results_by_row_index(results)
    all_indexes = sorted(set(norm_index.keys()) | set(res_index.keys()))
    merged: list[dict] = []
    for row_index in all_indexes:
        merged.append(merge_audit_row(norm_index.get(row_index), res_index.get(row_index)))
    return merged


def load_audit_insights_bundle(session_obj, *, require_unlock: bool = True) -> dict[str, Any]:
    """
    Carrega e valida o lote auditado processado para o chat analítico.

    Retorna {"ok": True, "bundle": {...}} ou {"ok": False, "error_code", "message"}.
    """
    sync_temp_table_with_session_documents()
    temp_table_id = get_temp_table_id(session_obj)
    if not temp_table_id:
        return {
            "ok": False,
            "error_code": ERROR_INSIGHTS_NO_TEMP_TABLE,
            "message": (
                "Não encontrei lote de auditoria processado nesta sessão. "
                "Faça o upload da planilha auditada, execute a auditoria e clique em "
                "'Gerar Gráficos' antes de consultar os resultados."
            ),
        }

    cfg = get_cleiton_doc_config()
    record = load_temp_table_record(temp_table_id, ttl_hours=cfg.upload_ttl_hours)
    if record is None:
        clear_temp_table_session_refs(session_obj)
        return {
            "ok": False,
            "error_code": ERROR_INSIGHTS_NO_TEMP_TABLE,
            "message": (
                "A tabela temporária desta sessão não está mais disponível. "
                "Reenvie os documentos e reprocesse a auditoria."
            ),
        }

    status = (record.get("status") or "").strip()
    if status in {TEMP_TABLE_STATUS_EXPIRED, TEMP_TABLE_STATUS_DISCARDED}:
        return {
            "ok": False,
            "error_code": ERROR_INSIGHTS_TEMP_TABLE_EXPIRED,
            "message": (
                "A tabela temporária expirou ou foi invalidada. "
                "Reenvie os documentos e reprocesse a auditoria antes de usar o chat analítico."
            ),
        }

    audit_batch = record.get("audit_batch")
    if not isinstance(audit_batch, dict):
        return {
            "ok": False,
            "error_code": ERROR_INSIGHTS_BATCH_NOT_FOUND,
            "message": (
                "Não há lote de auditoria nesta sessão. "
                "Envie a planilha auditada e execute o processamento antes de consultar os resultados."
            ),
        }

    batch_status = (audit_batch.get("status") or "").strip()
    if batch_status != AUDIT_BATCH_STATUS_PROCESSED:
        return {
            "ok": False,
            "error_code": ERROR_INSIGHTS_BATCH_NOT_PROCESSED,
            "message": (
                "A auditoria ainda não foi processada ou o lote não está concluído. "
                "Execute a auditoria e clique em 'Gerar Gráficos' para liberar consultas analíticas."
            ),
        }

    results = audit_batch.get("results")
    if not has_minimally_valid_audit_results(results):
        return {
            "ok": False,
            "error_code": ERROR_INSIGHTS_BATCH_NO_RESULTS,
            "message": (
                "O lote foi processado, mas não há resultados válidos disponíveis para análise. "
                "Verifique a planilha auditada e reprocesse se necessário."
            ),
        }
    merged_rows = build_merged_rows(audit_batch)
    audit_bi = _public_audit_bi(audit_batch)
    needs_reprocess = _audit_batch_effective_needs_reprocess(audit_batch)
    stale_reason = audit_batch.get("stale_reason")
    coverage_summary = build_coverage_summary(record.get("coverage_table"))

    bundle = {
        "temp_table_id": record.get("temp_table_id"),
        "audit_batch_id": audit_batch.get("audit_batch_id"),
        "source_file_name": audit_batch.get("source_file_name"),
        "normalized_rows": list(audit_batch.get("normalized_rows") or []),
        "results": list(results),
        "merged_rows": merged_rows,
        "summary": audit_batch.get("summary"),
        "audit_diagnostics": audit_batch.get("audit_diagnostics"),
        "tax_config_snapshot": audit_batch.get("tax_config_snapshot"),
        "tax_summary": audit_batch.get("tax_summary"),
        "audit_bi": audit_bi,
        "coverage_summary": coverage_summary,
        "needs_reprocess": bool(needs_reprocess),
        "stale_reason": stale_reason if isinstance(stale_reason, str) else None,
        "processed_at": audit_batch.get("processed_at"),
    }
    if require_unlock and not is_insights_chat_unlocked(session_obj, bundle):
        return {
            "ok": False,
            "error_code": ERROR_INSIGHTS_CHAT_LOCKED,
            "message": (
                "O chat analítico só fica disponível após clicar em 'Gerar Gráficos' nesta sessão. "
                "Abra o BI executivo da auditoria para liberar as consultas."
            ),
        }
    return {"ok": True, "bundle": bundle}
