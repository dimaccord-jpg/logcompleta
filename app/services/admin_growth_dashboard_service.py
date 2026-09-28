"""BI administrativo de Growth (SCRUM-149, lotes 1 e 2).

Lote 1: leitura agregada do funil. Não emite eventos, não altera o modelo
e não materializa linhas para contar em Python.

Lote 2: drilldown first-party de aquisição e apresentação do payload de
get_growth_tracking_quality_payload. First touch e origem da sessão atual
permanecem agregações separadas. Sem migration, sem índice novo e sem
inferir atribuição de page_view, paid ou direct.

Intervalo temporal único desta tela: [start, end) em UTC naive,
o mesmo contrato de FunnelEvent.occurred_at.

Contagem zero é o resultado medido de uma agregação vazia.
Taxa sem denominador comparável permanece ausente (None).
Confirmação externa histórica indisponível não é exibida como zero.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case, func, select

from app.extensions import db
from app.funnel_event_service import (
    FUNNEL_EVENT_CHECKOUT_STARTED,
    FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
    FUNNEL_EVENT_PAGE_VIEW,
    FUNNEL_EVENT_PAID,
    FUNNEL_EVENT_PLAN_SELECTED,
    FUNNEL_EVENT_SESSION_ORIGIN_OBSERVED,
    FUNNEL_EVENT_SIGNUP_COMPLETED,
    FUNNEL_EVENT_SIGNUP_STARTED,
    FUNNEL_EVENT_TASK_COMPLETED,
    FUNNEL_EVENT_TASK_STARTED,
)
from app.models import FunnelEvent, utcnow_naive
from app.services.growth_tracking_quality_service import (
    get_growth_tracking_quality_payload,
)

logger = logging.getLogger(__name__)

NOT_RECORDED_LABEL = "Não registrado"

# First touch é persistido no cadastro. Plano, checkout, primeiro valor e
# session_origin_observed gravam a origem da sessão atual, não esta modalidade.
FIRST_TOUCH_ACQUISITION_EVENTS = (FUNNEL_EVENT_SIGNUP_COMPLETED,)

# Eventos cujo snapshot pode conter current_session_origin. paid e page_view
# ficam de fora: não há atribuição persistida para inferir campanha.
CURRENT_SESSION_ACQUISITION_EVENTS = (
    FUNNEL_EVENT_SIGNUP_COMPLETED,
    FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
    FUNNEL_EVENT_PLAN_SELECTED,
    FUNNEL_EVENT_CHECKOUT_STARTED,
    FUNNEL_EVENT_SESSION_ORIGIN_OBSERVED,
)

_ALLOWED_DAYS = {7, 30, 90}

# page_view guarda o endpoint Flask em metadata_json.page.
# /contrate-um-plano é user.contrate_plano (user_area.contrate_plano).
GROWTH_PRICING_PAGE = "user.contrate_plano"

_AGGREGATED_EVENTS = (
    FUNNEL_EVENT_PAGE_VIEW,
    FUNNEL_EVENT_SIGNUP_COMPLETED,
    FUNNEL_EVENT_TASK_STARTED,
    FUNNEL_EVENT_TASK_COMPLETED,
    FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
    FUNNEL_EVENT_PLAN_SELECTED,
    FUNNEL_EVENT_CHECKOUT_STARTED,
    FUNNEL_EVENT_PAID,
)

_PERIOD_OPTIONS = (
    {"value": 7, "label": "7 dias"},
    {"value": 30, "label": "30 dias"},
    {"value": 90, "label": "90 dias"},
)


def _normalize_days(days: Any) -> int:
    try:
        value = int(days)
    except (TypeError, ValueError):
        return 30
    return value if value in _ALLOWED_DAYS else 30


def _as_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(microsecond=0)
    return value.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)


def _period(days: int, now_utc: datetime | None) -> tuple[datetime, datetime]:
    end = _as_naive_utc(now_utc or utcnow_naive())
    start = end - timedelta(days=days)
    return start, end


def _to_iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat()


def _empty_counts() -> dict[str, int]:
    return {
        "occurrences": 0,
        "distinct_users": 0,
        "distinct_contas": 0,
        "anonymous_occurrences": 0,
    }


def _metric(key: str, label: str, value: int | None, unit: str) -> dict[str, Any]:
    return {"key": key, "label": label, "value": value, "unit": unit}


def _stage(
    *,
    key: str,
    title: str,
    event_name: str | None,
    metrics: list[dict[str, Any]],
    notes: list[str],
    complementary: bool = False,
) -> dict[str, Any]:
    return {
        "key": key,
        "title": title,
        "event_name": event_name,
        "complementary": complementary,
        "metrics": metrics,
        "notes": notes,
    }


def _counts_or_none(raw: dict[str, int] | None, *, measured: bool) -> dict[str, int | None]:
    if not measured or raw is None:
        return {
            "occurrences": None,
            "distinct_users": None,
            "distinct_contas": None,
            "anonymous_occurrences": None,
        }
    return raw


def _build_stages(counts: dict[str, dict[str, int | None]], pricing: dict[str, int | None]) -> list[dict[str, Any]]:
    page = counts[FUNNEL_EVENT_PAGE_VIEW]
    signup = counts[FUNNEL_EVENT_SIGNUP_COMPLETED]
    started = counts[FUNNEL_EVENT_TASK_STARTED]
    completed = counts[FUNNEL_EVENT_TASK_COMPLETED]
    first_value = counts[FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED]
    plan = counts[FUNNEL_EVENT_PLAN_SELECTED]
    checkout = counts[FUNNEL_EVENT_CHECKOUT_STARTED]
    paid = counts[FUNNEL_EVENT_PAID]

    return [
        _stage(
            key="page_view",
            title="Entrada",
            event_name=FUNNEL_EVENT_PAGE_VIEW,
            metrics=[
                _metric(
                    "occurrences",
                    "Page views — ocorrências",
                    page["occurrences"],
                    "ocorrências",
                ),
                _metric(
                    "distinct_users",
                    "Page views identificados — usuários distintos",
                    page["distinct_users"],
                    "usuários",
                ),
            ],
            notes=[
                "Conta apresentações de página. Não representa visitantes nem usuários únicos anônimos.",
                "Usuários distintos consideram somente linhas com user_id.",
            ],
        ),
        _stage(
            key="signup_completed",
            title="Cadastro",
            event_name=FUNNEL_EVENT_SIGNUP_COMPLETED,
            metrics=[
                _metric(
                    "distinct_users",
                    "Cadastros concluídos — usuários distintos",
                    signup["distinct_users"],
                    "usuários",
                ),
            ],
            notes=[
                "COUNT(DISTINCT user_id). Linhas repetidas do mesmo usuário, inclusive por método, não entram de novo.",
            ],
        ),
        _stage(
            key="task_started",
            title="Tarefa iniciada",
            event_name=FUNNEL_EVENT_TASK_STARTED,
            metrics=[
                _metric(
                    "distinct_users",
                    "Tarefas iniciadas — usuários identificados distintos",
                    started["distinct_users"],
                    "usuários",
                ),
                _metric(
                    "occurrences",
                    "Tarefas iniciadas — ocorrências",
                    started["occurrences"],
                    "ocorrências",
                ),
                _metric(
                    "anonymous_occurrences",
                    "Tarefas iniciadas — ocorrências anônimas",
                    started["anonymous_occurrences"],
                    "ocorrências",
                ),
            ],
            notes=[
                "Ocorrência anônima não é convertida em usuário.",
            ],
        ),
        _stage(
            key="task_completed",
            title="Tarefa concluída",
            event_name=FUNNEL_EVENT_TASK_COMPLETED,
            metrics=[
                _metric(
                    "distinct_users",
                    "Tarefas concluídas — usuários identificados distintos",
                    completed["distinct_users"],
                    "usuários",
                ),
                _metric(
                    "occurrences",
                    "Tarefas concluídas — ocorrências",
                    completed["occurrences"],
                    "ocorrências",
                ),
                _metric(
                    "anonymous_occurrences",
                    "Tarefas concluídas — ocorrências anônimas",
                    completed["anonymous_occurrences"],
                    "ocorrências",
                ),
            ],
            notes=[
                "Ocorrência anônima não é convertida em usuário.",
            ],
        ),
        _stage(
            key="first_relevant_task_completed",
            title="Primeiro valor",
            event_name=FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
            metrics=[
                _metric(
                    "distinct_users",
                    "Primeiro valor — usuários distintos",
                    first_value["distinct_users"],
                    "usuários",
                ),
            ],
            notes=[
                "Marco de primeira conclusão relevante. O evento é único por usuário.",
            ],
        ),
        _stage(
            key="pricing",
            title="Pricing",
            event_name=FUNNEL_EVENT_PAGE_VIEW,
            metrics=[
                _metric(
                    "occurrences",
                    "Pricing — ocorrências",
                    pricing["occurrences"],
                    "ocorrências",
                ),
                _metric(
                    "distinct_users",
                    "Pricing — usuários distintos identificados",
                    pricing["distinct_users"],
                    "usuários",
                ),
            ],
            notes=[
                "Leitura de page_view com page=user.contrate_plano (/contrate-um-plano). Não substitui plano selecionado.",
            ],
        ),
        _stage(
            key="plan_selected",
            title="Plano selecionado",
            event_name=FUNNEL_EVENT_PLAN_SELECTED,
            complementary=True,
            metrics=[
                _metric(
                    "distinct_users",
                    "Plano selecionado — usuários distintos",
                    plan["distinct_users"],
                    "usuários",
                ),
            ],
            notes=[
                "Intenção comercial complementar. Não é o estágio de Pricing.",
            ],
        ),
        _stage(
            key="checkout_started",
            title="Checkout",
            event_name=FUNNEL_EVENT_CHECKOUT_STARTED,
            metrics=[
                _metric(
                    "distinct_contas",
                    "Checkout iniciado — contas distintas",
                    checkout["distinct_contas"],
                    "contas",
                ),
                _metric(
                    "occurrences",
                    "Checkout iniciado — ocorrências",
                    checkout["occurrences"],
                    "ocorrências",
                ),
            ],
            notes=[
                "Uma conta pode iniciar checkout mais de uma vez. A métrica principal conta contas distintas.",
            ],
        ),
        _stage(
            key="paid",
            title="Paid",
            event_name=FUNNEL_EVENT_PAID,
            metrics=[
                _metric(
                    "distinct_contas",
                    "Paid — contas distintas",
                    paid["distinct_contas"],
                    "contas",
                ),
            ],
            notes=[
                "Contas distintas com o evento paid no período. Não é receita.",
                "Não afirma a primeira compra histórica anterior ao corte configurado na emissão do evento.",
            ],
        ),
    ]


def _rate(
    *,
    key: str,
    label: str,
    description: str,
    unit: str,
    numerator: int | None,
    denominator: int | None,
) -> dict[str, Any]:
    rate = None
    if numerator is not None and denominator is not None and denominator > 0:
        rate = numerator / denominator
    return {
        "key": key,
        "label": label,
        "description": description,
        "unit": unit,
        "numerator": numerator if rate is not None else None,
        "denominator": denominator if rate is not None else None,
        "rate": rate,
    }


_ACQUISITION_NOTES = (
    "First touch é a origem originalmente conhecida do usuário. Origem da sessão atual é o contexto em que o evento ocorreu. As duas leituras ficam separadas.",
    "Snapshot ausente aparece como Não registrado e não é preenchido como direct.",
    "page_view e paid não entram nesta agregação. Paid não é associado a campanha.",
    "A tabela usa source, medium e campaign já persistidos. Click ID não é dimensão; a cobertura fica na qualidade da mensuração.",
    "Plano selecionado, checkout e primeiro valor gravam a origem da sessão atual. First touch desta tela é lido do cadastro, evento que persiste essa modalidade.",
    "O drilldown para em source, medium e campaign. Não há conjunto, anúncio, criativo, posicionamento, asset, palavra-chave nem métrica de mídia.",
)

_DISCREPANCY_NOTE = (
    "Não há confirmação externa histórica persistida. Dá para comparar eventos internos, "
    "candidatos externos e cobertura de atribuição. Não dá para afirmar discrepância "
    "quantitativa entre o AgenteFrete e Meta ou GA4."
)

_EXTERNAL_CONFIRMATION_UNAVAILABLE = "Confirmação externa histórica indisponível"

_FIRST_TOUCH_METRICS = (
    {
        "key": "signup_users",
        "label": "Cadastros",
        "unit": "usuários",
        "event_name": FUNNEL_EVENT_SIGNUP_COMPLETED,
        "identity": "user",
    },
)

_CURRENT_SESSION_METRICS = (
    {
        "key": "signup_users",
        "label": "Cadastros",
        "unit": "usuários",
        "event_name": FUNNEL_EVENT_SIGNUP_COMPLETED,
        "identity": "user",
    },
    {
        "key": "first_relevant_users",
        "label": "Primeiro valor",
        "unit": "usuários",
        "event_name": FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
        "identity": "user",
    },
    {
        "key": "plan_users",
        "label": "Plano selecionado",
        "unit": "usuários",
        "event_name": FUNNEL_EVENT_PLAN_SELECTED,
        "identity": "user",
    },
    {
        "key": "checkout_contas",
        "label": "Checkout",
        "unit": "contas",
        "event_name": FUNNEL_EVENT_CHECKOUT_STARTED,
        "identity": "conta",
    },
    {
        "key": "session_observations",
        "label": "Observações de sessão",
        "unit": "ocorrências",
        "event_name": FUNNEL_EVENT_SESSION_ORIGIN_OBSERVED,
        "identity": "occurrence",
    },
)

_INTERNAL_COUNT_LABELS = (
    (FUNNEL_EVENT_PAGE_VIEW, "Page views"),
    (FUNNEL_EVENT_SIGNUP_STARTED, "Cadastro iniciado"),
    (FUNNEL_EVENT_SIGNUP_COMPLETED, "Cadastro concluído"),
    (FUNNEL_EVENT_TASK_COMPLETED, "Tarefa concluída"),
    (FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED, "Primeiro valor"),
    (FUNNEL_EVENT_PLAN_SELECTED, "Plano selecionado"),
    (FUNNEL_EVENT_CHECKOUT_STARTED, "Checkout iniciado"),
    (FUNNEL_EVENT_PAID, "Paid"),
)

_EVENT_LABELS = {
    FUNNEL_EVENT_PAGE_VIEW: "Page view",
    FUNNEL_EVENT_SIGNUP_STARTED: "Cadastro iniciado",
    FUNNEL_EVENT_SIGNUP_COMPLETED: "Cadastro concluído",
    FUNNEL_EVENT_TASK_COMPLETED: "Tarefa concluída",
    FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED: "Primeiro valor",
    FUNNEL_EVENT_PLAN_SELECTED: "Plano selecionado",
    FUNNEL_EVENT_CHECKOUT_STARTED: "Checkout iniciado",
    FUNNEL_EVENT_PAID: "Paid",
    FUNNEL_EVENT_SESSION_ORIGIN_OBSERVED: "Origem da sessão observada",
}

_TOUCH_COUNT_SPECS = (
    ("applicable_count", None, "Eventos aplicáveis"),
    ("with_acquisition_evidence_count", "with_acquisition_evidence", "Com evidência de aquisição"),
    ("origin_not_recorded_count", "origin_not_recorded", "Origem não registrada"),
    ("with_known_source_count", "with_known_source", "Com source conhecido"),
    ("with_utm_count", "with_utm", "Com UTM"),
    ("with_any_click_id_count", "with_any_click_id", "Com algum click ID"),
    ("gclid_present_count", "gclid_present", "Cobertura gclid"),
    ("gbraid_present_count", "gbraid_present", "Cobertura gbraid"),
    ("wbraid_present_count", "wbraid_present", "Cobertura wbraid"),
    ("fbclid_present_count", "fbclid_present", "Cobertura fbclid"),
)

_LIMITATION_LABELS = {
    "browser_dispatch_has_no_persisted_confirmation": "Não há confirmação persistida de envio pelo browser.",
    "ga4_accounted_not_provable_locally": "Não é possível provar localmente que o GA4 contabilizou o evento.",
    "meta_accounted_not_provable_from_funnel_event_alone": "O evento interno sozinho não prova que a Meta contabilizou o evento.",
    "historical_consent_not_stored_on_funnel_event": "O consentimento histórico não fica gravado no evento.",
    "missing_origin_does_not_imply_direct": "Origem ausente não significa direct.",
    "external_candidate_does_not_imply_dispatch": "Candidato externo não significa que o evento foi enviado.",
    "paid_has_no_external_purchase_at_this_stage": "Paid não possui Purchase externo nesta etapa.",
    "meta_http_2xx_is_adapter_technical_success_only": "HTTP 2xx da Meta indica só sucesso técnico do adapter.",
}

_FIRST_RELEVANT_NOTE_LABELS = {
    "gap_is_investigable_not_confirmed_bug": "Lacuna é um indicador investigável, não um bug confirmado.",
    "marker_is_at_most_one_per_user": "Há no máximo um marco de primeiro valor por usuário.",
    "marker_may_occur_before_period": "O marco pode ter ocorrido antes do período exibido.",
    "recovery_may_create_marker_without_external_dispatch": "Recuperação pode criar o marco sem envio externo.",
    "anonymous_task_completed_is_out_of_scope": "Tarefa concluída anônima fica fora deste indicador.",
}

_FIRST_RELEVANT_FIELDS = (
    ("eligible_completion_count", "Conclusões elegíveis"),
    ("eligible_user_count", "Usuários elegíveis"),
    ("completions_with_marker_count", "Conclusões com marco"),
    ("completions_without_marker_count", "Conclusões sem marco"),
    ("completions_with_marker_before_period_count", "Conclusões com marco anterior ao período"),
    ("users_without_marker_count", "Usuários sem marco"),
)


def _as_count(value: Any) -> int | None:
    """Inteiro medido. Bool não vira 0: False não é contagem."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _count_item(
    *,
    key: str,
    label: str,
    value: int | None,
    unit: str,
    rate_display: str | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "kind": "count",
        "value": value,
        "unit": unit,
        "rate_display": rate_display,
    }


def _rate_display(rates: Any, rate_name: str | None) -> str | None:
    if not rate_name or not isinstance(rates, dict) or rate_name not in rates:
        return None
    value = rates.get(rate_name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return f"{value * 100:.1f}%"


def _event_label(name: str) -> str:
    return _EVENT_LABELS.get(name, name)


def _dimension_expr(touch: str, field: str):
    extracted = FunnelEvent.metadata_json["growth_attribution"][touch][field].as_string()
    return func.nullif(func.trim(extracted), "")


def _dimension_label(value: Any) -> str:
    if value is None:
        return NOT_RECORDED_LABEL
    text = str(value).strip()
    if not text:
        return NOT_RECORDED_LABEL
    return text


def _metric_expr(spec: dict[str, str]):
    event_name = spec["event_name"]
    if spec["identity"] == "occurrence":
        return func.coalesce(
            func.sum(case((FunnelEvent.event_name == event_name, 1), else_=0)),
            0,
        )
    column = FunnelEvent.user_id if spec["identity"] == "user" else FunnelEvent.conta_id
    return func.count(
        func.distinct(case((FunnelEvent.event_name == event_name, column), else_=None))
    )


def _aggregate_acquisition_touch(
    *,
    touch: str,
    metrics: tuple[dict[str, str], ...],
    allowed_events: tuple[str, ...],
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """GROUP BY source/medium/campaign do snapshot. Não carrega eventos."""
    event_names = tuple(spec["event_name"] for spec in metrics)
    if set(event_names) != set(allowed_events):
        raise RuntimeError("métricas de aquisição divergem dos eventos permitidos")
    if FUNNEL_EVENT_PAID in event_names or FUNNEL_EVENT_PAGE_VIEW in event_names:
        raise RuntimeError("paid e page_view não entram na aquisição")
    source = _dimension_expr(touch, "source")
    medium = _dimension_expr(touch, "medium")
    campaign = _dimension_expr(touch, "campaign")
    selected = [
        source.label("source"),
        medium.label("medium"),
        campaign.label("campaign"),
    ]
    selected.extend(_metric_expr(spec).label(spec["key"]) for spec in metrics)
    stmt = (
        select(*selected)
        .where(
            FunnelEvent.event_name.in_(event_names),
            FunnelEvent.occurred_at >= start,
            FunnelEvent.occurred_at < end,
        )
        .group_by(source, medium, campaign)
    )
    rows: list[dict[str, Any]] = []
    for record in db.session.execute(stmt):
        mapping = record._mapping
        row = {
            "source": _dimension_label(mapping["source"]),
            "medium": _dimension_label(mapping["medium"]),
            "campaign": _dimension_label(mapping["campaign"]),
        }
        for spec in metrics:
            row[spec["key"]] = int(mapping[spec["key"]] or 0)
        rows.append(row)
    metric_keys = [spec["key"] for spec in metrics]

    def _sort_key(row: dict[str, Any]) -> tuple:
        fully_missing = all(
            row[name] == NOT_RECORDED_LABEL for name in ("source", "medium", "campaign")
        )
        volume = sum(int(row[key]) for key in metric_keys)
        return (fully_missing, -volume, row["source"], row["medium"], row["campaign"])

    rows.sort(key=_sort_key)
    return rows


def _split_summary(
    rows: list[dict[str, Any]],
    metric_key: str,
    *,
    label: str,
    unit: str,
) -> dict[str, Any]:
    known = 0
    missing = 0
    for row in rows:
        value = int(row.get(metric_key) or 0)
        if row.get("source") == NOT_RECORDED_LABEL:
            missing += value
        else:
            known += value
    return {
        "label": label,
        "unit": unit,
        "known_source": known,
        "not_recorded": missing,
    }


def _touch_view(
    *,
    key: str,
    title: str,
    description: str,
    footnote: str,
    metrics: tuple[dict[str, str], ...],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "key": key,
        "title": title,
        "description": description,
        "footnote": footnote,
        "columns": [
            {"key": spec["key"], "label": spec["label"], "unit": spec["unit"]}
            for spec in metrics
        ],
        "summary": [
            _split_summary(rows, spec["key"], label=spec["label"], unit=spec["unit"])
            for spec in metrics
        ],
        "rows": rows,
    }


def _build_acquisition(start: datetime, end: datetime) -> dict[str, Any]:
    first_rows = _aggregate_acquisition_touch(
        touch="first_touch",
        metrics=_FIRST_TOUCH_METRICS,
        allowed_events=FIRST_TOUCH_ACQUISITION_EVENTS,
        start=start,
        end=end,
    )
    current_rows = _aggregate_acquisition_touch(
        touch="current_session_origin",
        metrics=_CURRENT_SESSION_METRICS,
        allowed_events=CURRENT_SESSION_ACQUISITION_EVENTS,
        start=start,
        end=end,
    )
    return {
        "available": True,
        "message": None,
        "missing_label": NOT_RECORDED_LABEL,
        "notes": list(_ACQUISITION_NOTES),
        "touches": [
            _touch_view(
                key="first_touch",
                title="First touch",
                description=(
                    "Origem originalmente conhecida do usuário, lida só de "
                    "growth_attribution.first_touch no cadastro."
                ),
                footnote=(
                    "Primeiro valor, plano selecionado e checkout não entram nesta tabela: "
                    "esses eventos não gravam first touch."
                ),
                metrics=_FIRST_TOUCH_METRICS,
                rows=first_rows,
            ),
            _touch_view(
                key="current_session_origin",
                title="Origem da sessão atual",
                description=(
                    "Origem do contexto em que o evento ocorreu, lida só de "
                    "growth_attribution.current_session_origin."
                ),
                footnote=(
                    "Cadastro, primeiro valor e plano contam usuários distintos no grupo. "
                    "Checkout conta contas distintas. Observações de sessão contam ocorrências "
                    "de session_origin_observed. A mesma identidade pode aparecer em mais de "
                    "uma linha se os snapshots divergirem."
                ),
                metrics=_CURRENT_SESSION_METRICS,
                rows=current_rows,
            ),
        ],
    }


def _unmeasured_acquisition() -> dict[str, Any]:
    return {
        "available": False,
        "message": "Aquisição indisponível temporariamente.",
        "missing_label": NOT_RECORDED_LABEL,
        "notes": list(_ACQUISITION_NOTES),
        "touches": [],
    }


def _external_confirmation(meta_capi: Any) -> dict[str, Any]:
    """Flag falsa ou ausente é indisponibilidade, nunca contagem zero."""
    block = meta_capi if isinstance(meta_capi, dict) else {}
    historical = block.get("historical_database_metrics_available")
    if historical is True:
        display = (
            "Confirmação externa histórica marcada como disponível, "
            "sem métrica numérica neste payload."
        )
    else:
        display = _EXTERNAL_CONFIRMATION_UNAVAILABLE
    return {
        "historical_database_metrics_available": True if historical is True else False,
        "kind": "unavailable",
        "display": display,
        "value": None,
    }


def _unavailable_tracking_quality() -> dict[str, Any]:
    return {
        "available": False,
        "service_failed": True,
        "message": "Qualidade da mensuração indisponível temporariamente.",
        "has_internal_counts": False,
        "external_confirmation": {
            "historical_database_metrics_available": False,
            "kind": "unavailable",
            "display": _EXTERNAL_CONFIRMATION_UNAVAILABLE,
            "value": None,
        },
        "discrepancy_note": _DISCREPANCY_NOTE,
        "blocks": [],
    }


def _touch_quality_block(touch_key: str, title: str, note: str, touch: Any) -> dict[str, Any]:
    block = touch if isinstance(touch, dict) else {}
    totals = block.get("totals") if isinstance(block.get("totals"), dict) else {}
    rates = totals.get("rates") if isinstance(totals.get("rates"), dict) else {}
    items = []
    for field, rate_name, label in _TOUCH_COUNT_SPECS:
        value = _as_count(totals.get(field)) if field in totals else None
        items.append(
            _count_item(
                key=f"{touch_key}.{field}",
                label=label,
                value=value,
                unit="eventos",
                rate_display=_rate_display(rates, rate_name),
            )
        )
    by_event = block.get("by_event_name") if isinstance(block.get("by_event_name"), dict) else {}
    rows = []
    for event_name in sorted(by_event):
        metrics = by_event.get(event_name)
        metrics = metrics if isinstance(metrics, dict) else {}
        cells = [_event_label(str(event_name))]
        for field in (
            "applicable_count",
            "with_acquisition_evidence_count",
            "origin_not_recorded_count",
            "with_utm_count",
            "with_any_click_id_count",
        ):
            value = _as_count(metrics.get(field)) if field in metrics else None
            cells.append("sem dados" if value is None else str(value))
        rows.append(cells)
    return {
        "title": title,
        "note": note,
        "items": items,
        "headers": [
            "Evento",
            "Aplicáveis",
            "Com evidência",
            "Origem não registrada",
            "Com UTM",
            "Com click ID",
        ],
        "rows": rows,
        "notes": [
            "Unidade: evento. Não é usuário distinto nem conta.",
            "Cobertura de gclid, gbraid, wbraid e fbclid é contagem. O valor do ID não é exibido.",
        ],
    }


def _candidate_count(block: Any) -> int | None:
    if not isinstance(block, dict) or "external_candidate_count" not in block:
        return None
    return _as_count(block.get("external_candidate_count"))


def _candidate_cell(by_event: Any, event_name: str) -> str:
    if not isinstance(by_event, dict) or event_name not in by_event:
        return "—"
    item = by_event.get(event_name)
    if not isinstance(item, dict) or "external_candidate_count" not in item:
        return "sem dados"
    value = _as_count(item.get("external_candidate_count"))
    if value is None:
        return "sem dados"
    return str(value)


def _present_tracking_quality(raw: Any) -> dict[str, Any]:
    """Traduz o payload do serviço de qualidade. Não recalcula as agregações."""
    if not isinstance(raw, dict):
        raise ValueError("payload de qualidade invalido")
    internal = raw.get("internal") if isinstance(raw.get("internal"), dict) else {}
    event_counts = internal.get("event_counts") if isinstance(internal.get("event_counts"), dict) else {}
    internal_items = []
    seen = set()
    for event_name, label in _INTERNAL_COUNT_LABELS:
        seen.add(event_name)
        value = _as_count(event_counts.get(event_name)) if event_name in event_counts else None
        internal_items.append(
            _count_item(
                key=f"internal.{event_name}",
                label=label,
                value=value,
                unit="ocorrências",
            )
        )
    for event_name in sorted(set(event_counts) - seen):
        value = _as_count(event_counts.get(event_name))
        internal_items.append(
            _count_item(
                key=f"internal.{event_name}",
                label=_event_label(str(event_name)),
                value=value,
                unit="ocorrências",
            )
        )

    attribution = raw.get("attribution") if isinstance(raw.get("attribution"), dict) else {}
    external = raw.get("external_candidates") if isinstance(raw.get("external_candidates"), dict) else {}
    meta_block = external.get("meta_browser_capi")
    ga4_block = external.get("ga4_browser")
    meta_by = meta_block.get("by_event_name") if isinstance(meta_block, dict) else {}
    ga4_by = ga4_block.get("by_event_name") if isinstance(ga4_block, dict) else {}
    candidate_rows = []
    for event_name in sorted(set(meta_by) | set(ga4_by)):
        candidate_rows.append(
            [
                _event_label(str(event_name)),
                _candidate_cell(meta_by, event_name),
                _candidate_cell(ga4_by, event_name),
            ]
        )

    first_relevant = (
        raw.get("first_relevant_quality")
        if isinstance(raw.get("first_relevant_quality"), dict)
        else {}
    )
    first_items = []
    for field, label in _FIRST_RELEVANT_FIELDS:
        value = _as_count(first_relevant.get(field)) if field in first_relevant else None
        unit = "usuários" if "user" in field else "conclusões"
        first_items.append(
            _count_item(
                key=f"first_relevant.{field}",
                label=label,
                value=value,
                unit=unit,
            )
        )
    first_notes = [
        _FIRST_RELEVANT_NOTE_LABELS.get(str(note), f"Nota registrada: {note}")
        for note in (first_relevant.get("notes") or [])
    ]

    limitation_notes = [
        _LIMITATION_LABELS.get(str(item), f"Limitação registrada: {item}")
        for item in (raw.get("known_limitations") or [])
    ]
    limitation_notes.append(_DISCREPANCY_NOTE)

    has_internal = any(
        item["value"] not in (None, 0) for item in internal_items
    )
    return {
        "available": True,
        "service_failed": False,
        "message": None,
        "has_internal_counts": has_internal,
        "external_confirmation": _external_confirmation(raw.get("meta_capi")),
        "discrepancy_note": _DISCREPANCY_NOTE,
        "blocks": [
            {
                "title": "Contagens first-party",
                "note": "Ocorrências internas no período. Não são usuários distintos.",
                "items": internal_items,
                "headers": [],
                "rows": [],
                "notes": [],
            },
            _touch_quality_block(
                "first_touch",
                "Cobertura de first touch",
                (
                    "O monitor considera first touch aplicável também a primeiro valor, plano e checkout. "
                    "Os produtores atuais gravam first touch no cadastro. Nos demais, a ausência permanece "
                    "como origem não registrada e não vira direct."
                ),
                attribution.get("first_touch"),
            ),
            _touch_quality_block(
                "current_session_origin",
                "Cobertura da origem da sessão",
                (
                    "Inclui cadastro, origem de sessão observada, primeiro valor quando o snapshot "
                    "foi persistido, plano selecionado e checkout."
                ),
                attribution.get("current_session_origin"),
            ),
            {
                "title": "Candidatos externos",
                "note": (
                    "Candidato é elegibilidade pelo nome do evento. Não significa que a Meta ou o GA4 "
                    "receberam, registraram ou confirmaram o evento. Candidatos Meta browser e "
                    "Candidatos Meta CAPI usam a mesma allowlist deste payload: o número é único e "
                    "não deve ser somado."
                ),
                "items": [
                    _count_item(
                        key="candidates.meta_browser_capi",
                        label="Candidatos Meta browser e Candidatos Meta CAPI",
                        value=_candidate_count(meta_block),
                        unit="candidatos",
                    ),
                    _count_item(
                        key="candidates.ga4_browser",
                        label="Candidatos GA4 browser",
                        value=_candidate_count(ga4_block),
                        unit="candidatos",
                    ),
                ],
                "headers": ["Evento", "Candidatos Meta", "Candidatos GA4"],
                "rows": candidate_rows,
                "notes": [
                    "Confirmação externa histórica indisponível não aparece como zero.",
                ],
            },
            {
                "title": "Qualidade do marco de primeiro valor",
                "note": "Indicador investigável. Lacuna não é bug confirmado.",
                "items": first_items,
                "headers": [],
                "rows": [],
                "notes": first_notes,
            },
            {
                "title": "Limitações conhecidas",
                "note": None,
                "items": [],
                "headers": [],
                "rows": [],
                "notes": limitation_notes,
            },
        ],
    }


def _acquisition_has_activity(acquisition: dict[str, Any]) -> bool:
    if not acquisition.get("available"):
        return False
    return any(bool(touch.get("rows")) for touch in acquisition.get("touches") or [])


def _shell_payload(
    *,
    days: int,
    start: datetime,
    end: datetime,
    message: str | None,
    service_failed: bool,
) -> dict[str, Any]:
    return {
        "filters": {
            "days": days,
            "period_options": [dict(item) for item in _PERIOD_OPTIONS],
        },
        "period": {
            "days": days,
            "start_utc": _to_iso(start),
            "end_utc": _to_iso(end),
            "bounds": "[start, end)",
            "label": f"Últimos {days} dias",
        },
        "has_data": False,
        "service_failed": service_failed,
        "message": message,
        "stages": [],
        "rates": [],
        "acquisition": _unmeasured_acquisition(),
        "tracking_quality": _unavailable_tracking_quality(),
    }


def unavailable_admin_growth_dashboard_payload(
    *,
    days: Any = 30,
    now_utc: datetime | None = None,
    message: str = "Métricas de Growth indisponíveis temporariamente.",
) -> dict[str, Any]:
    """Falha de leitura: métricas ausentes, sem zero inventado."""
    normalized = _normalize_days(days)
    start, end = _period(normalized, now_utc)
    payload = _shell_payload(
        days=normalized,
        start=start,
        end=end,
        message=message,
        service_failed=True,
    )
    empty = {name: _counts_or_none(None, measured=False) for name in _AGGREGATED_EVENTS}
    pricing = {"occurrences": None, "distinct_users": None}
    payload["stages"] = _build_stages(empty, pricing)
    payload["rates"] = _rates(
        measured=False,
        signup_to_first=None,
        first_to_plan=None,
        checkout_to_paid=None,
    )
    return payload


def _rates(
    *,
    measured: bool,
    signup_to_first: dict[str, int | None] | None,
    first_to_plan: dict[str, int | None] | None,
    checkout_to_paid: dict[str, int | None] | None,
) -> list[dict[str, Any]]:
    if (
        not measured
        or signup_to_first is None
        or first_to_plan is None
        or checkout_to_paid is None
    ):
        signup_to_first = {"numerator": None, "denominator": None}
        first_to_plan = {"numerator": None, "denominator": None}
        checkout_to_paid = {"numerator": None, "denominator": None}
    return [
        _rate(
            key="signup_to_first_relevant",
            label="Cadastro → primeiro valor",
            description=(
                "Usuários com cadastro concluído no período que também registraram "
                "primeiro valor no mesmo período, sobre os usuários cadastrados no período."
            ),
            unit="usuários",
            numerator=signup_to_first["numerator"],
            denominator=signup_to_first["denominator"],
        ),
        _rate(
            key="first_relevant_to_plan_selected",
            label="Primeiro valor → plano selecionado",
            description=(
                "Usuários com primeiro valor no período que também selecionaram plano "
                "no mesmo período, sobre os usuários com primeiro valor no período."
            ),
            unit="usuários",
            numerator=first_to_plan["numerator"],
            denominator=first_to_plan["denominator"],
        ),
        _rate(
            key="checkout_to_paid",
            label="Checkout → paid",
            description=(
                "Contas que iniciaram checkout no período e também tiveram paid no mesmo "
                "período, sobre as contas que iniciaram checkout. Não é receita e não afirma "
                "a primeira compra histórica anterior ao corte."
            ),
            unit="contas",
            numerator=checkout_to_paid["numerator"],
            denominator=checkout_to_paid["denominator"],
        ),
    ]


def _aggregate_events(start: datetime, end: datetime) -> dict[str, dict[str, int]]:
    anonymous = case((FunnelEvent.user_id.is_(None), 1), else_=0)
    stmt = (
        select(
            FunnelEvent.event_name,
            func.count(FunnelEvent.id),
            func.count(func.distinct(FunnelEvent.user_id)),
            func.count(func.distinct(FunnelEvent.conta_id)),
            func.coalesce(func.sum(anonymous), 0),
        )
        .where(
            FunnelEvent.event_name.in_(_AGGREGATED_EVENTS),
            FunnelEvent.occurred_at >= start,
            FunnelEvent.occurred_at < end,
        )
        .group_by(FunnelEvent.event_name)
    )
    found: dict[str, dict[str, int]] = {}
    for event_name, occurrences, distinct_users, distinct_contas, anonymous_occurrences in db.session.execute(stmt):
        found[str(event_name)] = {
            "occurrences": int(occurrences or 0),
            "distinct_users": int(distinct_users or 0),
            "distinct_contas": int(distinct_contas or 0),
            "anonymous_occurrences": int(anonymous_occurrences or 0),
        }
    return {name: found.get(name, _empty_counts()) for name in _AGGREGATED_EVENTS}


def _aggregate_pricing(start: datetime, end: datetime) -> dict[str, int]:
    page = FunnelEvent.metadata_json["page"].as_string()
    stmt = select(
        func.count(FunnelEvent.id),
        func.count(func.distinct(FunnelEvent.user_id)),
    ).where(
        FunnelEvent.event_name == FUNNEL_EVENT_PAGE_VIEW,
        FunnelEvent.occurred_at >= start,
        FunnelEvent.occurred_at < end,
        page == GROWTH_PRICING_PAGE,
    )
    occurrences, distinct_users = db.session.execute(stmt).one()
    return {
        "occurrences": int(occurrences or 0),
        "distinct_users": int(distinct_users or 0),
    }


def _distinct_ids(column, event_name: str, start: datetime, end: datetime):
    return (
        select(column.label("identity_id"))
        .where(
            FunnelEvent.event_name == event_name,
            FunnelEvent.occurred_at >= start,
            FunnelEvent.occurred_at < end,
            column.is_not(None),
        )
        .distinct()
        .subquery()
    )


def _same_identity_rate(
    column,
    denominator_event: str,
    numerator_event: str,
    start: datetime,
    end: datetime,
) -> dict[str, int | None]:
    """Interseção da mesma coluna de identidade no período.

    Sem denominador, a taxa fica ausente. Não cruza user_id com conta_id.
    """
    denominator_ids = _distinct_ids(column, denominator_event, start, end)
    denominator = int(
        db.session.execute(select(func.count()).select_from(denominator_ids)).scalar() or 0
    )
    if denominator <= 0:
        return {"numerator": None, "denominator": None}
    numerator_ids = _distinct_ids(column, numerator_event, start, end)
    numerator = int(
        db.session.execute(
            select(func.count())
            .select_from(denominator_ids)
            .where(denominator_ids.c.identity_id.in_(select(numerator_ids.c.identity_id)))
        ).scalar()
        or 0
    )
    return {"numerator": numerator, "denominator": denominator}


def _has_measured_rows(counts: dict[str, dict[str, int]], pricing: dict[str, int]) -> bool:
    if pricing["occurrences"] > 0:
        return True
    return any(item["occurrences"] > 0 for item in counts.values())


def get_admin_growth_dashboard_payload(
    *,
    days: Any = 30,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Payload do dashboard /admin/growth para o período [now-N dias, now)."""
    normalized = _normalize_days(days)
    start, end = _period(normalized, now_utc)
    counts = _aggregate_events(start, end)
    pricing = _aggregate_pricing(start, end)
    signup_to_first = _same_identity_rate(
        FunnelEvent.user_id,
        FUNNEL_EVENT_SIGNUP_COMPLETED,
        FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
        start,
        end,
    )
    first_to_plan = _same_identity_rate(
        FunnelEvent.user_id,
        FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
        FUNNEL_EVENT_PLAN_SELECTED,
        start,
        end,
    )
    checkout_to_paid = _same_identity_rate(
        FunnelEvent.conta_id,
        FUNNEL_EVENT_CHECKOUT_STARTED,
        FUNNEL_EVENT_PAID,
        start,
        end,
    )
    has_funnel_data = _has_measured_rows(counts, pricing)
    payload = _shell_payload(
        days=normalized,
        start=start,
        end=end,
        message=None,
        service_failed=False,
    )
    payload["stages"] = _build_stages(counts, pricing)
    payload["rates"] = _rates(
        measured=True,
        signup_to_first=signup_to_first,
        first_to_plan=first_to_plan,
        checkout_to_paid=checkout_to_paid,
    )
    payload["pricing_page"] = GROWTH_PRICING_PAGE
    try:
        payload["acquisition"] = _build_acquisition(start, end)
    except Exception:
        logger.exception("admin_growth_acquisition_failed")
    try:
        payload["tracking_quality"] = _present_tracking_quality(
            get_growth_tracking_quality_payload(start=start, end=end)
        )
    except Exception:
        logger.exception("admin_growth_tracking_quality_failed")
    has_data = (
        has_funnel_data
        or _acquisition_has_activity(payload["acquisition"])
        or bool(payload["tracking_quality"].get("has_internal_counts"))
    )
    payload["has_data"] = has_data
    payload["message"] = None if has_data else "Nenhum evento Growth neste período."
    return payload
