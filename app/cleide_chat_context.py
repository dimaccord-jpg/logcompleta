from __future__ import annotations

import logging
from typing import Any
from types import SimpleNamespace

from app.cleide_contracts import get_cleide_dataset_context, get_cleide_upload_ref
from app.cleide_operational_context import build_cleide_operational_context
from app.services.cleide_config_service import get_cleide_config

logger = logging.getLogger(__name__)

MAX_ITEMS_PER_TABLE = 10
MAX_TEXT_LEN = 80
MAX_LANGUAGE_ITEMS = 12


def get_cleide_chat_context(
    session_obj,
    *,
    max_items_per_table: int = MAX_ITEMS_PER_TABLE,
    max_text_len: int = MAX_TEXT_LEN,
) -> dict[str, Any]:
    try:
        cfg = get_cleide_config()
    except RuntimeError:
        cfg = SimpleNamespace(
            chat_context_max_items_per_table=max_items_per_table,
            chat_context_max_text_len=max_text_len,
            chat_context_rankings_limit=max_items_per_table,
            chat_context_include_transportadora=1,
            chat_context_include_uf_origem=1,
            chat_context_include_uf_destino=1,
            chat_context_include_temporal=1,
            chat_context_include_paretos=1,
            chat_context_mode="executivo",
            chat_context_max_chars=6000,
        )
    safe_limit = max(3, min(int(getattr(cfg, "chat_context_max_items_per_table", max_items_per_table)), 30))
    safe_text = max(32, min(int(getattr(cfg, "chat_context_max_text_len", max_text_len)), 200))
    rankings_limit = max(3, min(int(getattr(cfg, "chat_context_rankings_limit", safe_limit)), 25))
    mode = str(getattr(cfg, "chat_context_mode", "executivo") or "executivo").strip().lower()
    safe_mode = mode if mode in {"executivo", "conservador"} else "executivo"
    max_chars = max(1500, min(int(getattr(cfg, "chat_context_max_chars", 6000)), 12000))

    dataset_context = get_cleide_dataset_context(session_obj) or {}
    upload_ref = get_cleide_upload_ref(session_obj)
    operational = dataset_context.get("operational_context")
    source = "operational_context"
    if not isinstance(operational, dict) or not operational:
        source = "rebuilt_from_analytics_context"
        analytics = dataset_context.get("analytics_context") or {}
        logger.warning("Contexto operacional Cleide indisponivel. Rebuild seguro aplicado.")
        operational = build_cleide_operational_context(
            upload_ref=upload_ref,
            dataset_validado=bool(dataset_context.get("dataset_validado")),
            analytics_ready=bool(analytics.get("analytics_ready")),
            stale_upload=False,
            dataset_summary=dict(analytics.get("dataset_summary") or {}),
            kpis=dict(analytics.get("kpis") or {}),
            aggregate_tables={
                "transportadora": list(analytics.get("transportadora_stats") or []),
                "uf_origem": list(analytics.get("uf_origem_stats") or []),
                "uf_destino": list(analytics.get("uf_destino_stats") or []),
                "temporal": list(analytics.get("temporal_stats") or []),
                "pareto_fretes_zerados_uf_destino": list(analytics.get("pareto_fretes_zerados_uf_destino") or []),
                "pareto_fretes_zerados_transportadora": list(
                    analytics.get("pareto_fretes_zerados_transportadora") or []
                ),
            },
            aggregate_counts=dict(analytics.get("aggregate_counts") or {}),
            active_filters={},
            filter_mode="row_level_intersection_backend",
            kpi_scope="global_session",
            no_row_level_intersection=False,
            multi_dimension_filters_are_approximate=False,
            kpis_are_global_session_scope=True,
        )

    safe_operational, truncation = _sanitize_operational_context(
        operational,
        max_items_per_table=rankings_limit if safe_mode == "conservador" else safe_limit,
        max_text_len=safe_text,
        include_transportadora=bool(getattr(cfg, "chat_context_include_transportadora", 1)),
        include_uf_origem=bool(getattr(cfg, "chat_context_include_uf_origem", 1)),
        include_uf_destino=bool(getattr(cfg, "chat_context_include_uf_destino", 1)),
        include_temporal=bool(getattr(cfg, "chat_context_include_temporal", 1)),
        include_paretos=bool(getattr(cfg, "chat_context_include_paretos", 1)),
        mode=safe_mode,
        max_chars=max_chars,
    )
    return {
        "chat_context_version": "cleide_chat_context.v1",
        "chat_ready_context": True,
        "safe_operational_context": safe_operational,
        "exposure_controls": {
            "max_items_per_table": safe_limit,
            "rankings_limit": rankings_limit,
            "max_text_len": safe_text,
            "mode": safe_mode,
            "max_chars": max_chars,
            "source": source,
            "truncated": truncation,
        },
    }


def _sanitize_operational_context(
    context: dict[str, Any],
    *,
    max_items_per_table: int,
    max_text_len: int,
    include_transportadora: bool,
    include_uf_origem: bool,
    include_uf_destino: bool,
    include_temporal: bool,
    include_paretos: bool,
    mode: str,
    max_chars: int,
) -> tuple[dict[str, Any], bool]:
    truncated = False
    aggregate_tables = context.get("aggregate_tables") if isinstance(context.get("aggregate_tables"), dict) else {}
    safe_tables: dict[str, list[dict[str, Any]]] = {}
    table_policy = {
        "transportadora": include_transportadora,
        "uf_origem": include_uf_origem,
        "uf_destino": include_uf_destino,
        "temporal": include_temporal,
        "pareto_fretes_zerados_uf_destino": include_paretos,
        "pareto_fretes_zerados_transportadora": include_paretos,
    }
    if mode == "conservador":
        table_policy["temporal"] = False
        table_policy["pareto_fretes_zerados_uf_destino"] = False
        table_policy["pareto_fretes_zerados_transportadora"] = False

    for key in (
        "transportadora",
        "uf_origem",
        "uf_destino",
        "temporal",
        "pareto_fretes_zerados_uf_destino",
        "pareto_fretes_zerados_transportadora",
    ):
        if not table_policy.get(key, True):
            safe_tables[key] = []
            continue
        rows = aggregate_tables.get(key) if isinstance(aggregate_tables, dict) else []
        safe_rows = []
        for item in rows[:max_items_per_table] if isinstance(rows, list) else []:
            if not isinstance(item, dict):
                continue
            safe_row = {}
            for fld in ("chave", "data"):
                if fld in item:
                    safe_row[fld] = _safe_text(item.get(fld), max_text_len)
            for fld in ("quantidade", "valor_total", "peso_total", "percentual", "percentual_acumulado"):
                if fld in item:
                    safe_row[fld] = _safe_number(item.get(fld))
            safe_rows.append(safe_row)
        if isinstance(rows, list) and len(rows) > max_items_per_table:
            truncated = True
            logger.warning("Contexto Cleide truncado em %s: %s -> %s", key, len(rows), max_items_per_table)
        safe_tables[key] = safe_rows

    filter_context = context.get("filter_context") if isinstance(context.get("filter_context"), dict) else {}
    active_filters = filter_context.get("active_filters") if isinstance(filter_context.get("active_filters"), dict) else {}
    safe_filters = {
        "transportadora": _safe_text(active_filters.get("transportadora"), max_text_len),
        "uf_origem": _safe_text(active_filters.get("uf_origem"), max_text_len),
        "uf_destino": _safe_text(active_filters.get("uf_destino"), max_text_len),
        "data_inicio": _safe_text(active_filters.get("data_inicio"), max_text_len),
        "data_fim": _safe_text(active_filters.get("data_fim"), max_text_len),
    }

    language_policy = context.get("language_policy") if isinstance(context.get("language_policy"), dict) else {}
    allowed = _safe_text_list(language_policy.get("allowed_language"), max_items=MAX_LANGUAGE_ITEMS, max_text_len=max_text_len)
    forbidden = _safe_text_list(
        language_policy.get("forbidden_language"),
        max_items=MAX_LANGUAGE_ITEMS,
        max_text_len=max_text_len,
    )
    if len(allowed) < len(language_policy.get("allowed_language") or []):
        truncated = True
    if len(forbidden) < len(language_policy.get("forbidden_language") or []):
        truncated = True

    safe = {
        "schema_version": "cleide_contexto_operacional.v1",
        "agent": "cleide",
        "namespace": "cleide",
        "phase": "8_context_prep_no_ai",
        "generated_at": _safe_text(context.get("generated_at"), max_text_len),
        "session_scope": _safe_dict(context.get("session_scope")),
        "dataset_summary": _safe_dict(context.get("dataset_summary")),
        "kpis": _safe_dict(context.get("kpis")),
        "aggregate_tables": safe_tables,
        "aggregate_counts": _safe_dict(context.get("aggregate_counts")),
        "quality_flags": _safe_dict(context.get("quality_flags")),
        "filter_context": {
            "active_filters": safe_filters,
            "filter_mode": _safe_text(filter_context.get("filter_mode"), max_text_len) or "aggregate_approximation",
            "kpi_scope": _safe_text(filter_context.get("kpi_scope"), max_text_len) or "global_session",
        },
        "semantic_limits": _safe_dict(context.get("semantic_limits")),
        "language_policy": {
            "allowed_language": allowed,
            "forbidden_language": forbidden,
        },
        "security_guards": {
            "contains_raw_dataset": False,
            "contains_full_rows": False,
            "contains_roberto_payload": False,
            "contains_ai_output": False,
        },
    }
    safe_blob = str(safe)
    if len(safe_blob) > max_chars:
        truncated = True
        for key in ("temporal", "pareto_fretes_zerados_uf_destino", "pareto_fretes_zerados_transportadora"):
            if safe_tables.get(key):
                safe_tables[key] = []
                safe["aggregate_tables"][key] = []
        if len(str(safe)) > max_chars:
            for key in ("transportadora", "uf_origem", "uf_destino"):
                if len(safe_tables.get(key, [])) > 3:
                    safe_tables[key] = safe_tables[key][:3]
                    safe["aggregate_tables"][key] = safe_tables[key]
    return safe, truncated


def _safe_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _safe_text(value: Any, max_text_len: int) -> str:
    return str(value or "").strip()[:max_text_len]


def _safe_number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _safe_text_list(value: Any, *, max_items: int, max_text_len: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value[:max_items]:
        text = _safe_text(item, max_text_len)
        if text:
            out.append(text)
    return out
