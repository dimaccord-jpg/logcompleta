from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.cleide_language_policy import CLEIDE_ALLOWED_LANGUAGE, CLEIDE_FORBIDDEN_LANGUAGE


def build_cleide_operational_context(
    *,
    upload_ref: str | None,
    dataset_validado: bool,
    analytics_ready: bool,
    stale_upload: bool = False,
    dataset_summary: dict[str, Any] | None = None,
    kpis: dict[str, Any] | None = None,
    aggregate_tables: dict[str, Any] | None = None,
    aggregate_counts: dict[str, Any] | None = None,
    active_filters: dict[str, Any] | None = None,
    filter_mode: str = "aggregate_approximation",
    kpi_scope: str = "global_session",
    no_row_level_intersection: bool = True,
    multi_dimension_filters_are_approximate: bool = True,
    kpis_are_global_session_scope: bool = True,
) -> dict[str, Any]:
    safe_dataset_summary = _build_dataset_summary(dataset_summary)
    safe_kpis = _build_kpis(kpis)
    safe_aggregate_tables = _build_aggregate_tables(aggregate_tables)
    safe_aggregate_counts = _build_aggregate_counts(aggregate_counts)
    safe_active_filters = _build_active_filters(active_filters)

    return {
        "schema_version": "cleide_contexto_operacional.v1",
        "agent": "cleide",
        "namespace": "cleide",
        "phase": "8_context_prep_no_ai",
        "generated_at": _iso_utc_now(),
        "session_scope": {
            "upload_ref_present": bool((upload_ref or "").strip()),
            "dataset_validado": bool(dataset_validado),
            "analytics_ready": bool(analytics_ready),
            "stale_upload": bool(stale_upload),
        },
        "dataset_summary": safe_dataset_summary,
        "kpis": safe_kpis,
        "aggregate_tables": safe_aggregate_tables,
        "aggregate_counts": safe_aggregate_counts,
        "quality_flags": _build_quality_flags(
            dataset_summary=safe_dataset_summary,
            aggregate_counts=safe_aggregate_counts,
        ),
        "filter_context": {
            "active_filters": safe_active_filters,
            "filter_mode": str(filter_mode or "aggregate_approximation"),
            "kpi_scope": str(kpi_scope or "global_session"),
        },
        "semantic_limits": {
            "no_row_level_intersection": bool(no_row_level_intersection),
            "multi_dimension_filters_are_approximate": bool(multi_dimension_filters_are_approximate),
            "kpis_are_global_session_scope": bool(kpis_are_global_session_scope),
            "no_accusatory_financial_conclusion": True,
        },
        "language_policy": {
            "allowed_language": list(CLEIDE_ALLOWED_LANGUAGE),
            "forbidden_language": list(CLEIDE_FORBIDDEN_LANGUAGE),
        },
        "security_guards": {
            "contains_raw_dataset": False,
            "contains_full_rows": False,
            "contains_roberto_payload": False,
            "contains_ai_output": False,
        },
    }


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _build_dataset_summary(raw: dict[str, Any] | None) -> dict[str, int]:
    data = raw or {}
    return {
        "linhas_processadas": max(0, _to_int(data.get("linhas_processadas"))),
        "invalid_numeric_rows": max(0, _to_int(data.get("invalid_numeric_rows"))),
        "invalid_date_rows": max(0, _to_int(data.get("invalid_date_rows"))),
        "negative_value_rows": max(0, _to_int(data.get("negative_value_rows"))),
    }


def _build_kpis(raw: dict[str, Any] | None) -> dict[str, Any]:
    data = raw or {}
    periodo = data.get("periodo_dataset") if isinstance(data.get("periodo_dataset"), dict) else {}
    return {
        "total_documentos": max(0, _to_int(data.get("total_documentos"))),
        "valor_total_frete": max(0.0, _to_float(data.get("valor_total_frete"))),
        "peso_total": max(0.0, _to_float(data.get("peso_total"))),
        "ticket_medio_frete": max(0.0, _to_float(data.get("ticket_medio_frete"))),
        "percentual_fretes_zerados": max(0.0, _to_float(data.get("percentual_fretes_zerados"))),
        "percentual_peso_zerado": max(0.0, _to_float(data.get("percentual_peso_zerado"))),
        "transportadoras_unicas": max(0, _to_int(data.get("transportadoras_unicas"))),
        "ufs_origem_unicas": max(0, _to_int(data.get("ufs_origem_unicas"))),
        "ufs_destino_unicas": max(0, _to_int(data.get("ufs_destino_unicas"))),
        "periodo_dataset": {
            "inicio": periodo.get("inicio"),
            "fim": periodo.get("fim"),
        },
    }


def _as_list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            out.append(dict(item))
    return out


def _build_aggregate_tables(raw: dict[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
    data = raw or {}
    return {
        "transportadora": _as_list_of_dicts(data.get("transportadora")),
        "uf_origem": _as_list_of_dicts(data.get("uf_origem")),
        "uf_destino": _as_list_of_dicts(data.get("uf_destino")),
        "temporal": _as_list_of_dicts(data.get("temporal")),
        "pareto_fretes_zerados_uf_destino": _as_list_of_dicts(data.get("pareto_fretes_zerados_uf_destino")),
        "pareto_fretes_zerados_transportadora": _as_list_of_dicts(data.get("pareto_fretes_zerados_transportadora")),
    }


def _build_aggregate_counts(raw: dict[str, Any] | None) -> dict[str, int]:
    data = raw or {}
    return {
        "transportadora_stats": max(0, _to_int(data.get("transportadora_stats"))),
        "uf_origem_stats": max(0, _to_int(data.get("uf_origem_stats"))),
        "uf_destino_stats": max(0, _to_int(data.get("uf_destino_stats"))),
        "temporal_stats": max(0, _to_int(data.get("temporal_stats"))),
        "pareto_fretes_zerados_uf_destino": max(0, _to_int(data.get("pareto_fretes_zerados_uf_destino"))),
        "pareto_fretes_zerados_transportadora": max(0, _to_int(data.get("pareto_fretes_zerados_transportadora"))),
    }


def _build_quality_flags(*, dataset_summary: dict[str, int], aggregate_counts: dict[str, int]) -> dict[str, bool]:
    return {
        "has_invalid_numeric": dataset_summary.get("invalid_numeric_rows", 0) > 0,
        "has_invalid_date": dataset_summary.get("invalid_date_rows", 0) > 0,
        "has_negative_values": dataset_summary.get("negative_value_rows", 0) > 0,
        "has_sparse_aggregates": sum(int(v or 0) for v in aggregate_counts.values()) <= 1,
    }


def _build_active_filters(raw: dict[str, Any] | None) -> dict[str, str]:
    data = raw or {}
    return {
        "transportadora": str(data.get("transportadora") or "").strip(),
        "uf_origem": str(data.get("uf_origem") or "").strip(),
        "uf_destino": str(data.get("uf_destino") or "").strip(),
        "data_inicio": str(data.get("data_inicio") or "").strip(),
        "data_fim": str(data.get("data_fim") or "").strip(),
    }
