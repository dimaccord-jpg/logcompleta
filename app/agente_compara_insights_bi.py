"""
Agregações do BI executivo da auditoria Agente Compara, espelhando a lógica do frontend.

Fonte preferencial: audit_bi.rows (mesmos dados exibidos nos gráficos).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
AUDIT_BI_TOP_N = 10
DIVERGENCE_EPSILON = 0.004


def _safe_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed:
        return None
    return parsed


def row_divergence(row: dict) -> float | None:
    if not isinstance(row, dict):
        return None
    charged = _safe_float(row.get("charged_freight"))
    expected = _safe_float(row.get("expected_freight"))
    if charged is not None and expected is not None:
        return charged - expected
    return _safe_float(row.get("divergence_value"))


def normalize_issue_date(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        return text
    try:
        normalized = text.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).date().isoformat()
        return parsed.date().isoformat()
    except (TypeError, ValueError):
        return ""

def filter_bi_rows(rows: list[dict], visual_focus: dict | None) -> list[dict]:
    if not isinstance(rows, list):
        return []
    filtered = [row for row in rows if isinstance(row, dict)]
    if not visual_focus or not isinstance(visual_focus, dict):
        return filtered
    carrier = visual_focus.get("carrier")
    if isinstance(carrier, str) and carrier.strip():
        carrier_val = carrier.strip()
        filtered = [row for row in filtered if str(row.get("carrier") or "").strip() == carrier_val]
    destination_uf = visual_focus.get("destination_uf")
    if isinstance(destination_uf, str) and destination_uf.strip():
        uf_val = destination_uf.strip().upper()
        filtered = [
            row for row in filtered if str(row.get("destination_uf") or "").strip().upper() == uf_val
        ]
    origin_uf = visual_focus.get("origin_uf")
    if isinstance(origin_uf, str) and origin_uf.strip():
        origin_val = origin_uf.strip().upper()
        filtered = [
            row for row in filtered if str(row.get("origin_uf") or "").strip().upper() == origin_val
        ]
    issue_date = visual_focus.get("issue_date")
    if isinstance(issue_date, str) and issue_date.strip():
        date_val = normalize_issue_date(issue_date.strip())
        filtered = [row for row in filtered if normalize_issue_date(row.get("issue_date")) == date_val]
    return filtered


def bi_rows_from_bundle(bundle: dict, visual_focus: dict | None = None) -> tuple[list[dict], bool]:
    audit_bi = bundle.get("audit_bi") if isinstance(bundle.get("audit_bi"), dict) else {}
    rows = audit_bi.get("rows") if isinstance(audit_bi.get("rows"), list) else []
    if audit_bi.get("ready") and rows:
        return filter_bi_rows(rows, visual_focus), True
    fallback = filter_bi_rows(list(bundle.get("merged_rows") or []), visual_focus)
    return fallback, False


def build_financial_metrics(rows: list[dict]) -> dict[str, Any]:
    metrics = {
        "total_rows": len(rows),
        "financial_rows": 0,
        "divergent_rows": 0,
        "overcharged": 0.0,
        "undercharged": 0.0,
        "absolute_impact": 0.0,
        "confidence_ratio": 0.0,
        "average_absolute_divergence": 0.0,
        "confidence_label": "Indisponível",
    }
    for row in rows:
        charged = _safe_float(row.get("charged_freight"))
        expected = _safe_float(row.get("expected_freight"))
        divergence = row_divergence(row)
        if charged is not None and expected is not None:
            metrics["financial_rows"] += 1
        if divergence is None:
            continue
        if abs(divergence) > DIVERGENCE_EPSILON:
            metrics["divergent_rows"] += 1
        if divergence > 0:
            metrics["overcharged"] += divergence
            metrics["absolute_impact"] += divergence
        elif divergence < 0:
            metrics["undercharged"] += abs(divergence)
            metrics["absolute_impact"] += abs(divergence)
    total = metrics["total_rows"]
    if total:
        metrics["confidence_ratio"] = (metrics["financial_rows"] / total) * 100
        metrics["average_absolute_divergence"] = metrics["absolute_impact"] / total
    ratio = metrics["confidence_ratio"]
    if ratio >= 95:
        metrics["confidence_label"] = "Alta"
    elif ratio >= 75:
        metrics["confidence_label"] = "Média"
    elif ratio > 0:
        metrics["confidence_label"] = "Baixa"
    return metrics


def aggregate_by_field(rows: list[dict], field_name: str) -> list[dict]:
    grouped: dict[str, dict] = {}
    for row in rows:
        key = str(row.get(field_name) or "").strip()
        if not key:
            continue
        bucket = grouped.setdefault(
            key,
            {
                "chave": key,
                "quantidade": 0,
                "linhas_financeiras": 0,
                "linhas_divergentes": 0,
                "valor_cobrado": 0.0,
                "valor_esperado": 0.0,
                "divergencia_liquida": 0.0,
                "cobrado_a_mais": 0.0,
                "cobrado_a_menor": 0.0,
                "impacto_total": 0.0,
            },
        )
        bucket["quantidade"] += 1
        charged = _safe_float(row.get("charged_freight"))
        expected = _safe_float(row.get("expected_freight"))
        if charged is not None:
            bucket["valor_cobrado"] += charged
        if expected is not None:
            bucket["valor_esperado"] += expected
        divergence = row_divergence(row)
        if divergence is None:
            continue
        bucket["linhas_financeiras"] += 1
        bucket["divergencia_liquida"] += divergence
        if abs(divergence) > DIVERGENCE_EPSILON:
            bucket["linhas_divergentes"] += 1
        if divergence > 0:
            bucket["cobrado_a_mais"] += divergence
            bucket["impacto_total"] += divergence
        elif divergence < 0:
            bucket["cobrado_a_menor"] += abs(divergence)
            bucket["impacto_total"] += abs(divergence)
    return list(grouped.values())


def sort_rows(rows: list[dict], sort_key: str, *, order: str = "desc") -> list[dict]:
    reverse = order != "asc"

    def _key(item: dict):
        if sort_key in {"chave", "data"}:
            return str(item.get(sort_key) or "").upper()
        return _safe_float(item.get(sort_key)) or 0.0

    return sorted(rows, key=_key, reverse=reverse)


def aggregate_by_date_chronological(rows: list[dict]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for row in rows:
        key = normalize_issue_date(row.get("issue_date"))
        if not key:
            continue
        bucket = grouped.setdefault(
            key,
            {
                "data": key,
                "quantidade": 0,
                "linhas_financeiras": 0,
                "linhas_divergentes": 0,
                "divergencia_liquida": 0.0,
                "cobrado_a_mais": 0.0,
                "cobrado_a_menor": 0.0,
                "impacto_total": 0.0,
            },
        )
        bucket["quantidade"] += 1
        divergence = row_divergence(row)
        if divergence is None:
            continue
        bucket["linhas_financeiras"] += 1
        bucket["divergencia_liquida"] += divergence
        if abs(divergence) > DIVERGENCE_EPSILON:
            bucket["linhas_divergentes"] += 1
        if divergence > 0:
            bucket["cobrado_a_mais"] += divergence
            bucket["impacto_total"] += divergence
        elif divergence < 0:
            bucket["cobrado_a_menor"] += abs(divergence)
            bucket["impacto_total"] += abs(divergence)
    return sort_rows(list(grouped.values()), "data", order="asc")


def build_overcharge_pareto(rows: list[dict], field_name: str = "carrier") -> list[dict]:
    grouped = [
        item
        for item in aggregate_by_field(rows, field_name)
        if (_safe_float(item.get("cobrado_a_mais")) or 0) > 0
    ]
    grouped = sort_rows(grouped, "cobrado_a_mais", order="desc")
    total = sum(_safe_float(item.get("cobrado_a_mais")) or 0 for item in grouped)
    accumulated = 0.0
    pareto: list[dict] = []
    for item in grouped[:AUDIT_BI_TOP_N]:
        value = _safe_float(item.get("cobrado_a_mais")) or 0.0
        percentual = (value / total * 100) if total > 0 else 0.0
        accumulated += percentual
        pareto.append(
            {
                "chave": item.get("chave"),
                "valor": value,
                "percentual": percentual,
                "percentual_acumulado": accumulated,
            }
        )
    return pareto


def carrier_impact_top(rows: list[dict], limit: int = AUDIT_BI_TOP_N) -> list[dict]:
    return sort_rows(aggregate_by_field(rows, "carrier"), "impacto_total", order="desc")[:limit]


def uf_impact_top(rows: list[dict], limit: int = AUDIT_BI_TOP_N) -> list[dict]:
    return sort_rows(aggregate_by_field(rows, "destination_uf"), "impacto_total", order="desc")[:limit]
