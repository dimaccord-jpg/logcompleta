"""Registro manual de experimentos Growth. Sem execução, campanha ou BI automático."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from app.extensions import db
from app.models import GrowthExperiment

RECENT_EXPERIMENT_LIMIT = 10
_ORIGIN_CAMPAIGN_MAX = 255
_PRIMARY_METRIC_MAX = 255
PRIMARY_METRIC_CUSTOM_SENTINEL = "__custom__"
PRIMARY_METRIC_OPTIONS: tuple[tuple[str, str], ...] = (
    ("page_view", "Entrada — Page view"),
    ("signup_started", "Cadastro iniciado"),
    ("signup_completed", "Cadastro concluído"),
    ("task_started", "Tarefa iniciada"),
    ("task_completed", "Tarefa concluída"),
    ("first_relevant_task_completed", "Primeiro valor"),
    ("pricing_viewed", "Pricing visualizado"),
    ("plan_selected", "Plano selecionado"),
    ("checkout_started", "Checkout iniciado"),
    ("paid", "Paid"),
)
_CANONICAL_PRIMARY_METRICS = frozenset(value for value, _label in PRIMARY_METRIC_OPTIONS)

FORM_FIELDS = (
    "hypothesis",
    "start_date",
    "origin_campaign",
    "change_description",
    "primary_metric",
    "observed_result",
    "evidence",
    "interpretation",
    "decision",
    "next_action",
    "status",
)

_OPTIONAL_TEXT_FIELDS = (
    "origin_campaign",
    "change_description",
    "observed_result",
    "evidence",
    "interpretation",
    "decision",
    "next_action",
)


class GrowthExperimentValidationError(Exception):
    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__("Experimento Growth inválido.")


def empty_growth_experiment_form() -> dict[str, str]:
    values = {field: "" for field in FORM_FIELDS}
    values["primary_metric_custom"] = ""
    return values


def growth_experiment_form_values(row: GrowthExperiment) -> dict[str, str]:
    start = row.start_date.isoformat() if row.start_date else ""
    values = empty_growth_experiment_form()
    values.update(
        {
            "hypothesis": row.hypothesis or "",
            "start_date": start,
            "origin_campaign": row.origin_campaign or "",
            "change_description": row.change_description or "",
            "primary_metric": _primary_metric_choice(row.primary_metric or ""),
            "primary_metric_custom": _primary_metric_custom_text(row.primary_metric or ""),
            "observed_result": row.observed_result or "",
            "evidence": row.evidence or "",
            "interpretation": row.interpretation or "",
            "decision": row.decision or "",
            "next_action": row.next_action or "",
            "status": row.status or "",
        }
    )
    return values


def form_values_from_mapping(raw) -> dict[str, str]:
    values = empty_growth_experiment_form()
    for field in FORM_FIELDS:
        value = raw.get(field) if raw is not None else None
        values[field] = "" if value is None else str(value).strip()
    custom = raw.get("primary_metric_custom") if raw is not None else None
    values["primary_metric_custom"] = "" if custom is None else str(custom).strip()
    return values


def primary_metric_options() -> list[tuple[str, str]]:
    return list(PRIMARY_METRIC_OPTIONS)


def status_options() -> list[tuple[str, str]]:
    return [
        (status, GrowthExperiment.STATUS_LABELS[status])
        for status in GrowthExperiment.STATUSES
    ]


def list_growth_experiments() -> list[GrowthExperiment]:
    return (
        GrowthExperiment.query.order_by(
            GrowthExperiment.start_date.desc(),
            GrowthExperiment.id.desc(),
        ).all()
    )


def list_recent_growth_experiments(
    start: datetime,
    end: datetime,
    *,
    limit: int = RECENT_EXPERIMENT_LIMIT,
) -> list[GrowthExperiment]:
    """Experimentos cuja data de início cai no intervalo [start, end)."""
    if end <= start:
        return []
    first_day = start.date()
    last_day = (end - timedelta(microseconds=1)).date()
    if last_day < first_day:
        return []
    return (
        GrowthExperiment.query.filter(GrowthExperiment.start_date >= first_day)
        .filter(GrowthExperiment.start_date <= last_day)
        .order_by(GrowthExperiment.start_date.desc(), GrowthExperiment.id.desc())
        .limit(limit)
        .all()
    )


def get_growth_experiment(experiment_id: int) -> GrowthExperiment | None:
    return db.session.get(GrowthExperiment, experiment_id)


def create_growth_experiment(raw) -> GrowthExperiment:
    cleaned = _validated_payload(raw)
    row = GrowthExperiment(**cleaned)
    db.session.add(row)
    db.session.commit()
    return row


def update_growth_experiment(row: GrowthExperiment, raw) -> GrowthExperiment:
    cleaned = _validated_payload(raw)
    for field, value in cleaned.items():
        setattr(row, field, value)
    db.session.commit()
    return row


def _validated_payload(raw) -> dict:
    values = form_values_from_mapping(raw)
    errors: dict[str, str] = {}

    hypothesis = values["hypothesis"]
    if not hypothesis:
        errors["hypothesis"] = "Informe a hipótese."

    start_date = _parse_start_date(values["start_date"], errors)

    origin_campaign = _optional_text(values["origin_campaign"])
    if origin_campaign is not None and len(origin_campaign) > _ORIGIN_CAMPAIGN_MAX:
        errors["origin_campaign"] = "Origem/campanha deve ter no máximo 255 caracteres."

    primary_metric = _resolve_primary_metric(values, errors)

    status = values["status"]
    if not status:
        errors["status"] = "Informe o status."
    elif status not in GrowthExperiment.STATUSES:
        errors["status"] = "Selecione um status válido."

    if errors:
        raise GrowthExperimentValidationError(errors)

    payload = {
        "hypothesis": hypothesis,
        "start_date": start_date,
        "primary_metric": primary_metric,
        "status": status,
    }
    for field in _OPTIONAL_TEXT_FIELDS:
        payload[field] = _optional_text(values[field])
    return payload


def _primary_metric_choice(stored: str) -> str:
    value = (stored or "").strip()
    if value in _CANONICAL_PRIMARY_METRICS:
        return value
    if not value:
        return ""
    return PRIMARY_METRIC_CUSTOM_SENTINEL


def _primary_metric_custom_text(stored: str) -> str:
    value = (stored or "").strip()
    if not value or value in _CANONICAL_PRIMARY_METRICS:
        return ""
    return value


def _resolve_primary_metric(values: dict[str, str], errors: dict[str, str]) -> str:
    choice = values["primary_metric"]
    if choice in _CANONICAL_PRIMARY_METRICS:
        return choice
    if choice == PRIMARY_METRIC_CUSTOM_SENTINEL:
        custom = values.get("primary_metric_custom", "")
        if not custom:
            errors["primary_metric"] = "Informe a métrica primária."
            return ""
        if len(custom) > _PRIMARY_METRIC_MAX:
            errors["primary_metric"] = "Métrica primária deve ter no máximo 255 caracteres."
            return ""
        return custom
    errors["primary_metric"] = "Informe a métrica primária."
    return ""


def _parse_start_date(raw: str, errors: dict[str, str]) -> date | None:
    if not raw:
        errors["start_date"] = "Informe a data de início."
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        errors["start_date"] = "Informe uma data de início válida."
        return None


def _optional_text(raw: str) -> str | None:
    text = (raw or "").strip()
    return text or None
