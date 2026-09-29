"""SCRUM-149 lote 3: registro administrativo de experimentos Growth."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.extensions import db
from app.models import FunnelEvent, GrowthExperiment, HomeCtaExperimentEvent
from app.services.admin_growth_experiment_service import (
    PRIMARY_METRIC_CUSTOM_SENTINEL,
    PRIMARY_METRIC_OPTIONS,
    list_recent_growth_experiments,
)
from tests.test_admin_growth_dashboard import (
    _build_admin_client,
    _login,
    _seed_user,
)

_REQUIRED = {
    "hypothesis": "CTA da home aumenta cadastros.",
    "start_date": "2026-09-20",
    "primary_metric": "page_view",
    "status": "planned",
}


def _admin_client(app):
    with app.app_context():
        admin, _conta = _seed_user("growth-exp-admin@test.com", "conta-growth-exp-admin")
        admin.is_admin = True
        db.session.add(admin)
        outsider, _conta_out = _seed_user(
            "growth-exp-user@test.com",
            "conta-growth-exp-user",
        )
        db.session.commit()
        admin_id = admin.id
        outsider_id = outsider.id
    client = _build_admin_client(app)
    return client, admin_id, outsider_id


def _login_admin(app, client, user_id: int) -> None:
    with app.app_context():
        _login(client, user_id)


def _payload(**overrides):
    data = dict(_REQUIRED)
    data.update(overrides)
    return data


def test_admin_access_required_for_experiment_routes(app):
    client, admin_id, outsider_id = _admin_client(app)
    paths = (
        "/admin/growth/experiments",
        "/admin/growth/experiments/new",
        "/admin/growth/experiments/1/edit",
    )

    for path in paths:
        anonymous = client.get(path)
        assert anonymous.status_code == 302

    _login_admin(app, client, outsider_id)
    for path in paths:
        assert client.get(path).status_code == 403
    assert client.post("/admin/growth/experiments/new", data=_payload()).status_code == 403
    assert (
        client.post("/admin/growth/experiments/1/edit", data=_payload()).status_code
        == 403
    )

    _login_admin(app, client, admin_id)
    assert client.get("/admin/growth/experiments").status_code == 200
    assert client.get("/admin/growth/experiments/new").status_code == 200
    missing = client.get("/admin/growth/experiments/999/edit")
    assert missing.status_code == 404


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("hypothesis", "Informe a hipótese."),
        ("start_date", "Informe a data de início."),
        ("primary_metric", "Informe a métrica primária."),
        ("status", "Informe o status."),
    ],
)
def test_create_requires_minimum_fields(app, field, message):
    client, admin_id, _outsider_id = _admin_client(app)
    _login_admin(app, client, admin_id)
    data = _payload()
    data[field] = "   "
    response = client.post("/admin/growth/experiments/new", data=data)
    assert response.status_code == 200
    assert message in response.get_data(as_text=True)
    with app.app_context():
        assert GrowthExperiment.query.count() == 0


def test_create_rejects_invalid_status_and_date(app):
    client, admin_id, _outsider_id = _admin_client(app)
    _login_admin(app, client, admin_id)

    invalid_status = client.post(
        "/admin/growth/experiments/new",
        data=_payload(status="winner"),
    )
    assert invalid_status.status_code == 200
    assert "Selecione um status válido." in invalid_status.get_data(as_text=True)

    invalid_date = client.post(
        "/admin/growth/experiments/new",
        data=_payload(start_date="31/09/2026"),
    )
    assert invalid_date.status_code == 200
    assert "Informe uma data de início válida." in invalid_date.get_data(as_text=True)

    with app.app_context():
        assert GrowthExperiment.query.count() == 0
        assert FunnelEvent.query.count() == 0
        assert HomeCtaExperimentEvent.query.count() == 0


@pytest.mark.parametrize("status", ["planned", "running", "completed", "learning_recorded"])
def test_create_persists_status_and_optional_fields(app, status):
    client, admin_id, _outsider_id = _admin_client(app)
    _login_admin(app, client, admin_id)
    response = client.post(
        "/admin/growth/experiments/new",
        data=_payload(
            status=status,
            origin_campaign="Meta — frete-setembro",
            change_description="Troca do título do anúncio.",
            observed_result="Cadastros subiram na semana.",
            evidence="Recorte manual do BI de 20 a 27/09.",
            interpretation="O título novo ficou mais específico.",
            decision="Manter a variação.",
            next_action="Registrar o aprendizado e encerrar.",
        ),
    )
    assert response.status_code == 302
    assert response.location.endswith("/admin/growth/experiments")

    with app.app_context():
        row = GrowthExperiment.query.one()
        assert row.hypothesis == _REQUIRED["hypothesis"]
        assert row.start_date == date(2026, 9, 20)
        assert row.primary_metric == _REQUIRED["primary_metric"]
        assert row.status == status
        assert row.origin_campaign == "Meta — frete-setembro"
        assert row.change_description == "Troca do título do anúncio."
        assert row.observed_result == "Cadastros subiram na semana."
        assert row.evidence == "Recorte manual do BI de 20 a 27/09."
        assert row.interpretation == "O título novo ficou mais específico."
        assert row.decision == "Manter a variação."
        assert row.next_action == "Registrar o aprendizado e encerrar."
        assert row.created_at is not None
        assert row.updated_at is not None
        assert FunnelEvent.query.count() == 0
        assert HomeCtaExperimentEvent.query.count() == 0


def test_edit_updates_fields_and_status_without_workflow(app):
    client, admin_id, _outsider_id = _admin_client(app)
    _login_admin(app, client, admin_id)
    created = client.post("/admin/growth/experiments/new", data=_payload())
    assert created.status_code == 302

    with app.app_context():
        experiment_id = GrowthExperiment.query.one().id

    edit = client.get(f"/admin/growth/experiments/{experiment_id}/edit")
    assert edit.status_code == 200
    assert "CTA da home aumenta cadastros." in edit.get_data(as_text=True)

    updated = client.post(
        f"/admin/growth/experiments/{experiment_id}/edit",
        data=_payload(
            hypothesis="Hipótese revisada.",
            start_date="2026-09-21",
            primary_metric="paid",
            status="learning_recorded",
            decision="Encerrar e documentar.",
            evidence="Nota do administrador.",
        ),
    )
    assert updated.status_code == 302

    with app.app_context():
        row = db.session.get(GrowthExperiment, experiment_id)
        assert row.hypothesis == "Hipótese revisada."
        assert row.start_date == date(2026, 9, 21)
        assert row.primary_metric == "paid"
        assert row.status == "learning_recorded"
        assert row.decision == "Encerrar e documentar."
        assert row.evidence == "Nota do administrador."
        assert row.origin_campaign is None
        assert GrowthExperiment.query.count() == 1


def test_edit_keeps_row_when_required_field_is_cleared(app):
    client, admin_id, _outsider_id = _admin_client(app)
    _login_admin(app, client, admin_id)
    client.post("/admin/growth/experiments/new", data=_payload(decision="Decisão inicial."))
    with app.app_context():
        experiment_id = GrowthExperiment.query.one().id

    response = client.post(
        f"/admin/growth/experiments/{experiment_id}/edit",
        data=_payload(hypothesis=""),
    )
    assert response.status_code == 200
    assert "Informe a hipótese." in response.get_data(as_text=True)
    with app.app_context():
        row = db.session.get(GrowthExperiment, experiment_id)
        assert row.hypothesis == _REQUIRED["hypothesis"]
        assert row.decision == "Decisão inicial."
        assert row.status == "planned"


def test_growth_page_lists_period_experiments_and_empty_state(app):
    client, admin_id, _outsider_id = _admin_client(app)
    _login_admin(app, client, admin_id)

    empty = client.get("/admin/growth?days=7")
    assert empty.status_code == 200
    empty_html = empty.get_data(as_text=True)
    assert "<h4 class=\"mb-0\">Growth</h4>" in empty_html
    assert "Nenhum experimento registrado neste período." in empty_html
    assert 'href="/admin/growth/experiments/new"' in empty_html
    assert "Novo experimento" in empty_html

    client.post(
        "/admin/growth/experiments/new",
        data=_payload(
            hypothesis="Hipótese antiga fora do período.",
            start_date="2020-01-01",
            status="completed",
        ),
    )
    outside = client.get("/admin/growth?days=7")
    outside_html = outside.get_data(as_text=True)
    assert "Nenhum experimento registrado neste período." in outside_html
    assert "Hipótese antiga fora do período." not in outside_html

    today = date.today().isoformat()
    created = client.post(
        "/admin/growth/experiments/new",
        data=_payload(
            hypothesis="Hipótese visível no período.",
            start_date=today,
            origin_campaign="Orgânico",
            primary_metric=PRIMARY_METRIC_CUSTOM_SENTINEL,
            primary_metric_custom="Cadastros do experimento",
            status="running",
            decision="Continuar mais uma semana.",
        ),
    )
    assert created.status_code == 302

    page = client.get("/admin/growth?days=7")
    html = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "Hipótese visível no período." in html
    assert "Orgânico" in html
    assert "Cadastros do experimento" in html
    assert "Executando" in html
    assert "Continuar mais uma semana." in html
    assert "Hipótese antiga fora do período." not in html
    assert "Nenhum experimento registrado neste período." not in html
    assert "/admin/growth/experiments/" in html
    assert "Editar" in html
    assert "Taxas com a mesma identidade" in html

    listing = client.get("/admin/growth/experiments")
    listing_html = listing.get_data(as_text=True)
    assert "Hipótese visível no período." in listing_html
    assert "Hipótese antiga fora do período." in listing_html
    assert "Concluído" in listing_html


def test_list_recent_growth_experiments_uses_half_open_period(app):
    with app.app_context():
        for hypothesis, start_date, status in (
            ("No início", date(2026, 9, 21), "planned"),
            ("No fim", date(2026, 9, 28), "running"),
            ("Antes", date(2026, 9, 20), "completed"),
            ("Depois", date(2026, 9, 29), "learning_recorded"),
        ):
            db.session.add(
                GrowthExperiment(
                    hypothesis=hypothesis,
                    start_date=start_date,
                    primary_metric="Cadastros",
                    status=status,
                )
            )
        db.session.commit()
        rows = list_recent_growth_experiments(
            datetime(2026, 9, 21, 15, 0, 0),
            datetime(2026, 9, 28, 15, 0, 0),
        )
        assert [row.hypothesis for row in rows] == ["No fim", "No início"]


_CANONICAL_PRIMARY_METRICS = (
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


def test_experiment_form_renders_primary_metric_select(app):
    client, admin_id, _outsider_id = _admin_client(app)
    _login_admin(app, client, admin_id)
    html = client.get("/admin/growth/experiments/new").get_data(as_text=True)
    assert '<select id="primary_metric" name="primary_metric"' in html
    assert ">Outra métrica</option>" in html
    assert 'id="primary_metric_custom_wrap" hidden' in html
    assert 'name="primary_metric_custom"' in html


def test_primary_metric_options_are_canonical(app):
    assert PRIMARY_METRIC_OPTIONS == _CANONICAL_PRIMARY_METRICS
    client, admin_id, _outsider_id = _admin_client(app)
    _login_admin(app, client, admin_id)
    html = client.get("/admin/growth/experiments/new").get_data(as_text=True)
    for value, label in _CANONICAL_PRIMARY_METRICS:
        assert f'value="{value}"' in html
        assert f">{label}</option>" in html
    custom_at = html.find('value="__custom__"')
    paid_at = html.find('value="paid"')
    assert paid_at != -1 and custom_at > paid_at


def test_create_saves_canonical_checkout_started(app):
    client, admin_id, _outsider_id = _admin_client(app)
    _login_admin(app, client, admin_id)
    response = client.post(
        "/admin/growth/experiments/new",
        data=_payload(
            primary_metric="checkout_started",
            primary_metric_custom="Checkout iniciado",
        ),
    )
    assert response.status_code == 302
    with app.app_context():
        row = GrowthExperiment.query.one()
        assert row.primary_metric == "checkout_started"


def test_create_saves_custom_primary_metric(app):
    client, admin_id, _outsider_id = _admin_client(app)
    _login_admin(app, client, admin_id)
    response = client.post(
        "/admin/growth/experiments/new",
        data=_payload(
            primary_metric=PRIMARY_METRIC_CUSTOM_SENTINEL,
            primary_metric_custom="CheckoutStarted",
        ),
    )
    assert response.status_code == 302
    with app.app_context():
        row = GrowthExperiment.query.one()
        assert row.primary_metric == "CheckoutStarted"


def test_create_rejects_missing_or_unknown_primary_metric(app):
    client, admin_id, _outsider_id = _admin_client(app)
    _login_admin(app, client, admin_id)

    missing = client.post(
        "/admin/growth/experiments/new",
        data=_payload(primary_metric="", hypothesis=""),
    )
    missing_html = missing.get_data(as_text=True)
    assert missing.status_code == 200
    assert "Informe a métrica primária." in missing_html

    unknown = client.post(
        "/admin/growth/experiments/new",
        data=_payload(primary_metric="checkout started"),
    )
    assert unknown.status_code == 200
    assert "Informe a métrica primária." in unknown.get_data(as_text=True)

    blank_custom = client.post(
        "/admin/growth/experiments/new",
        data=_payload(
            primary_metric=PRIMARY_METRIC_CUSTOM_SENTINEL,
            primary_metric_custom="   ",
            hypothesis="",
        ),
    )
    blank_html = blank_custom.get_data(as_text=True)
    assert blank_custom.status_code == 200
    assert "Informe a métrica primária." in blank_html
    assert 'value="__custom__" selected' in blank_html
    assert 'id="primary_metric_custom_wrap"' in blank_html
    assert 'id="primary_metric_custom_wrap" hidden' not in blank_html

    preserved = client.post(
        "/admin/growth/experiments/new",
        data=_payload(primary_metric="checkout_started", hypothesis=""),
    )
    preserved_html = preserved.get_data(as_text=True)
    assert preserved.status_code == 200
    assert 'value="checkout_started" selected' in preserved_html
    assert 'id="primary_metric_custom_wrap" hidden' in preserved_html

    with app.app_context():
        assert GrowthExperiment.query.count() == 0


def test_edit_selects_canonical_primary_metric(app):
    client, admin_id, _outsider_id = _admin_client(app)
    _login_admin(app, client, admin_id)
    created = client.post(
        "/admin/growth/experiments/new",
        data=_payload(primary_metric="checkout_started"),
    )
    assert created.status_code == 302
    with app.app_context():
        experiment_id = GrowthExperiment.query.one().id

    html = client.get(f"/admin/growth/experiments/{experiment_id}/edit").get_data(as_text=True)
    assert 'value="checkout_started" selected' in html
    assert 'value="__custom__" selected' not in html
    assert 'id="primary_metric_custom_wrap" hidden' in html


def test_edit_opens_custom_metric_for_legacy_value(app):
    client, admin_id, _outsider_id = _admin_client(app)
    _login_admin(app, client, admin_id)
    with app.app_context():
        row = GrowthExperiment(
            hypothesis="Métrica anterior ao select.",
            start_date=date(2026, 9, 20),
            primary_metric="Cadastros concluídos",
            status="planned",
        )
        db.session.add(row)
        db.session.commit()
        experiment_id = row.id

    html = client.get(f"/admin/growth/experiments/{experiment_id}/edit").get_data(as_text=True)
    assert 'value="__custom__" selected' in html
    assert 'value="Cadastros concluídos"' in html
    assert 'id="primary_metric_custom_wrap" hidden' not in html
    assert 'value="page_view" selected' not in html
