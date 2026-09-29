from __future__ import annotations

import os
from datetime import datetime, timedelta

from flask_login import UserMixin

from app.extensions import db, login_manager
from app.funnel_event_service import (
    FUNNEL_EVENT_CHECKOUT_STARTED,
    FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
    FUNNEL_EVENT_PAGE_VIEW,
    FUNNEL_EVENT_PAID,
    FUNNEL_EVENT_PLAN_SELECTED,
    FUNNEL_EVENT_SIGNUP_COMPLETED,
    FUNNEL_EVENT_TASK_COMPLETED,
    FUNNEL_EVENT_TASK_STARTED,
    FUNNEL_SOURCE_CLEIDE_AUDIT,
    FUNNEL_SOURCE_GROWTH,
    TASK_TYPE_CLEIDE_AUDIT,
    record_funnel_event,
)
from app.infra import get_user_by_id
from app.models import FunnelEvent
from app.services.admin_growth_dashboard_service import (
    GROWTH_PRICING_PAGE,
    get_admin_growth_dashboard_payload,
    unavailable_admin_growth_dashboard_payload,
)
from tests.conftest import seed_conta_franquia_cliente, seed_usuario

NOW = datetime(2026, 9, 28, 15, 0, 0)


class _AuthUser(UserMixin):
    def __init__(self, user_id: str):
        self.id = user_id


def _seed_user(email: str, slug: str):
    conta, franquia = seed_conta_franquia_cliente(slug=slug)
    return seed_usuario(franquia.id, conta.id, email=email), conta


def _event(
    *,
    event_name: str,
    key: str,
    occurred_at: datetime,
    source: str = FUNNEL_SOURCE_GROWTH,
    user_id: int | None = None,
    conta_id: int | None = None,
    franquia_id: int | None = None,
    metadata_json: dict | None = None,
):
    return record_funnel_event(
        event_name=event_name,
        source=source,
        user_id=user_id,
        conta_id=conta_id,
        franquia_id=franquia_id,
        idempotency_key=key,
        occurred_at=occurred_at,
        metadata_json=metadata_json,
    )


def _stage(payload: dict, key: str) -> dict:
    return next(item for item in payload["stages"] if item["key"] == key)


def _metric(stage: dict, key: str):
    return next(item["value"] for item in stage["metrics"] if item["key"] == key)


def _rate(payload: dict, key: str) -> dict:
    return next(item for item in payload["rates"] if item["key"] == key)


def test_empty_period_keeps_measured_zero_and_omits_rate(app):
    with app.app_context():
        payload = get_admin_growth_dashboard_payload(days="abc", now_utc=NOW)
        assert payload["filters"]["days"] == 30
        assert payload["period"]["bounds"] == "[start, end)"
        assert payload["period"]["start_utc"] == "2026-08-29T15:00:00"
        assert payload["period"]["end_utc"] == "2026-09-28T15:00:00"
        assert payload["has_data"] is False
        assert payload["service_failed"] is False
        page = _stage(payload, "page_view")
        assert _metric(page, "occurrences") == 0
        assert _metric(page, "distinct_users") == 0
        assert _rate(payload, "signup_to_first_relevant")["rate"] is None
        assert _rate(payload, "first_relevant_to_plan_selected")["rate"] is None
        assert [item["key"] for item in payload["rates"]] == [
            "signup_to_first_relevant",
            "first_relevant_to_plan_selected",
            "checkout_to_paid",
        ]


def test_service_does_not_materialize_funnel_rows(app, monkeypatch):
    with app.app_context():
        user, conta = _seed_user("growth-query@test.com", "conta-growth-query")
        _event(
            event_name=FUNNEL_EVENT_PAGE_VIEW,
            key="growth-query-pv",
            occurred_at=datetime(2026, 9, 28, 10, 0, 0),
            user_id=user.id,
            conta_id=conta.id,
            franquia_id=user.franquia_id,
            metadata_json={"page": "index"},
        )
        db.session.commit()

        def forbidden_all(*_args, **_kwargs):
            raise AssertionError("FunnelEvent.query.all não deve agregar o BI Growth")

        monkeypatch.setattr(FunnelEvent.query.__class__, "all", forbidden_all)
        payload = get_admin_growth_dashboard_payload(days=7, now_utc=NOW)
        assert _metric(_stage(payload, "page_view"), "occurrences") == 1


def test_counts_separate_occurrence_user_and_account(app):
    with app.app_context():
        user_a, conta_a = _seed_user("growth-a@test.com", "conta-growth-a")
        user_b, conta_b = _seed_user("growth-b@test.com", "conta-growth-b")
        inside = datetime(2026, 9, 27, 12, 0, 0)

        _event(
            event_name=FUNNEL_EVENT_PAGE_VIEW,
            key="pv-anon",
            occurred_at=inside,
            metadata_json={"page": "index"},
        )
        _event(
            event_name=FUNNEL_EVENT_PAGE_VIEW,
            key="pv-a",
            occurred_at=inside,
            user_id=user_a.id,
            conta_id=conta_a.id,
            franquia_id=user_a.franquia_id,
            metadata_json={"page": "index"},
        )
        _event(
            event_name=FUNNEL_EVENT_PAGE_VIEW,
            key="pv-pricing-a",
            occurred_at=inside,
            user_id=user_a.id,
            conta_id=conta_a.id,
            franquia_id=user_a.franquia_id,
            metadata_json={"page": GROWTH_PRICING_PAGE},
        )
        _event(
            event_name=FUNNEL_EVENT_PAGE_VIEW,
            key="pv-pricing-anon",
            occurred_at=inside,
            metadata_json={"page": GROWTH_PRICING_PAGE},
        )
        _event(
            event_name=FUNNEL_EVENT_SIGNUP_COMPLETED,
            key="signup-a-password",
            occurred_at=inside,
            user_id=user_a.id,
            metadata_json={"signup_method": "password"},
        )
        _event(
            event_name=FUNNEL_EVENT_SIGNUP_COMPLETED,
            key="signup-a-google",
            occurred_at=inside,
            user_id=user_a.id,
            metadata_json={"signup_method": "google"},
        )
        _event(
            event_name=FUNNEL_EVENT_SIGNUP_COMPLETED,
            key="signup-b",
            occurred_at=inside,
            user_id=user_b.id,
            metadata_json={"signup_method": "password"},
        )
        _event(
            event_name=FUNNEL_EVENT_TASK_STARTED,
            key="task-start-a",
            occurred_at=inside,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            user_id=user_a.id,
            conta_id=conta_a.id,
            franquia_id=user_a.franquia_id,
            metadata_json={"task_type": TASK_TYPE_CLEIDE_AUDIT},
        )
        _event(
            event_name=FUNNEL_EVENT_TASK_STARTED,
            key="task-start-anon",
            occurred_at=inside,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            metadata_json={"task_type": TASK_TYPE_CLEIDE_AUDIT},
        )
        _event(
            event_name=FUNNEL_EVENT_TASK_COMPLETED,
            key="task-done-a",
            occurred_at=inside,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            user_id=user_a.id,
            conta_id=conta_a.id,
            franquia_id=user_a.franquia_id,
            metadata_json={"task_type": TASK_TYPE_CLEIDE_AUDIT},
        )
        _event(
            event_name=FUNNEL_EVENT_TASK_COMPLETED,
            key="task-done-anon",
            occurred_at=inside,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            metadata_json={"task_type": TASK_TYPE_CLEIDE_AUDIT},
        )
        _event(
            event_name=FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
            key="first-a",
            occurred_at=inside,
            user_id=user_a.id,
            metadata_json={"task_type": TASK_TYPE_CLEIDE_AUDIT},
        )
        _event(
            event_name=FUNNEL_EVENT_PLAN_SELECTED,
            key="plan-a",
            occurred_at=inside,
            user_id=user_a.id,
            metadata_json={"plan": "pro"},
        )
        _event(
            event_name=FUNNEL_EVENT_CHECKOUT_STARTED,
            key="checkout-a-1",
            occurred_at=inside,
            user_id=user_a.id,
            conta_id=conta_a.id,
            metadata_json={"plan": "pro"},
        )
        _event(
            event_name=FUNNEL_EVENT_CHECKOUT_STARTED,
            key="checkout-a-2",
            occurred_at=inside,
            user_id=user_a.id,
            conta_id=conta_a.id,
            metadata_json={"plan": "pro"},
        )
        _event(
            event_name=FUNNEL_EVENT_CHECKOUT_STARTED,
            key="checkout-b",
            occurred_at=inside,
            user_id=user_b.id,
            conta_id=conta_b.id,
            metadata_json={"plan": "starter"},
        )
        _event(
            event_name=FUNNEL_EVENT_PAID,
            key="paid-a",
            occurred_at=inside,
            conta_id=conta_a.id,
            metadata_json={"plan": "pro"},
        )
        db.session.commit()

        payload = get_admin_growth_dashboard_payload(days=7, now_utc=NOW)

        page = _stage(payload, "page_view")
        assert _metric(page, "occurrences") == 4
        assert _metric(page, "distinct_users") == 1
        assert page["metrics"][0]["unit"] == "ocorrências"
        assert page["metrics"][1]["unit"] == "usuários"

        signup = _stage(payload, "signup_completed")
        assert _metric(signup, "distinct_users") == 2
        assert "occurrences" not in {item["key"] for item in signup["metrics"]}

        started = _stage(payload, "task_started")
        assert _metric(started, "distinct_users") == 1
        assert _metric(started, "occurrences") == 2
        assert _metric(started, "anonymous_occurrences") == 1

        completed = _stage(payload, "task_completed")
        assert _metric(completed, "distinct_users") == 1
        assert _metric(completed, "occurrences") == 2
        assert _metric(completed, "anonymous_occurrences") == 1

        assert _metric(_stage(payload, "first_relevant_task_completed"), "distinct_users") == 1

        pricing = _stage(payload, "pricing")
        assert _metric(pricing, "occurrences") == 2
        assert _metric(pricing, "distinct_users") == 1
        assert payload["pricing_page"] == GROWTH_PRICING_PAGE

        plan = _stage(payload, "plan_selected")
        assert plan["complementary"] is True
        assert _metric(plan, "distinct_users") == 1

        checkout = _stage(payload, "checkout_started")
        assert _metric(checkout, "distinct_contas") == 2
        assert _metric(checkout, "occurrences") == 3
        assert checkout["metrics"][0]["unit"] == "contas"

        paid = _stage(payload, "paid")
        assert _metric(paid, "distinct_contas") == 1
        assert paid["metrics"][0]["unit"] == "contas"
        paid_notes = " ".join(paid["notes"]).lower()
        assert "não é receita" in paid_notes
        assert "primeira compra histórica" in paid_notes

        assert _rate(payload, "signup_to_first_relevant")["rate"] == 0.5
        assert _rate(payload, "signup_to_first_relevant")["numerator"] == 1
        assert _rate(payload, "signup_to_first_relevant")["denominator"] == 2
        assert _rate(payload, "signup_to_first_relevant")["unit"] == "usuários"
        assert _rate(payload, "first_relevant_to_plan_selected")["rate"] == 1.0
        checkout_rate = _rate(payload, "checkout_to_paid")
        assert checkout_rate["rate"] == 0.5
        assert checkout_rate["numerator"] == 1
        assert checkout_rate["denominator"] == 2
        assert checkout_rate["unit"] == "contas"


def test_period_is_half_open_and_rates_ignore_outside_window(app):
    with app.app_context():
        user, conta = _seed_user("growth-window@test.com", "conta-growth-window")
        start = datetime(2026, 9, 21, 15, 0, 0)
        end = NOW

        _event(
            event_name=FUNNEL_EVENT_PAGE_VIEW,
            key="pv-at-start",
            occurred_at=start,
            user_id=user.id,
            metadata_json={"page": "index"},
        )
        _event(
            event_name=FUNNEL_EVENT_PAGE_VIEW,
            key="pv-before",
            occurred_at=datetime(2026, 9, 21, 14, 59, 59),
            user_id=user.id,
            metadata_json={"page": "index"},
        )
        _event(
            event_name=FUNNEL_EVENT_PAGE_VIEW,
            key="pv-at-end",
            occurred_at=end,
            user_id=user.id,
            metadata_json={"page": "index"},
        )
        _event(
            event_name=FUNNEL_EVENT_SIGNUP_COMPLETED,
            key="signup-inside",
            occurred_at=datetime(2026, 9, 22, 8, 0, 0),
            user_id=user.id,
            metadata_json={"signup_method": "password"},
        )
        _event(
            event_name=FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
            key="first-outside",
            occurred_at=datetime(2026, 9, 20, 8, 0, 0),
            user_id=user.id,
            metadata_json={"task_type": TASK_TYPE_CLEIDE_AUDIT},
        )
        _event(
            event_name=FUNNEL_EVENT_PAID,
            key="paid-outside",
            occurred_at=datetime(2026, 9, 1, 8, 0, 0),
            conta_id=conta.id,
        )
        db.session.commit()

        payload = get_admin_growth_dashboard_payload(days=7, now_utc=NOW)
        assert payload["period"]["start_utc"] == "2026-09-21T15:00:00"
        assert payload["period"]["end_utc"] == "2026-09-28T15:00:00"
        assert _metric(_stage(payload, "page_view"), "occurrences") == 1
        assert _metric(_stage(payload, "signup_completed"), "distinct_users") == 1
        assert _metric(_stage(payload, "first_relevant_task_completed"), "distinct_users") == 0
        assert _metric(_stage(payload, "paid"), "distinct_contas") == 0
        assert _rate(payload, "signup_to_first_relevant")["rate"] == 0.0
        assert _rate(payload, "signup_to_first_relevant")["numerator"] == 0
        assert _rate(payload, "signup_to_first_relevant")["denominator"] == 1


def test_first_value_outside_signup_cohort_does_not_inflate_rate(app):
    with app.app_context():
        signed, _conta_s = _seed_user("growth-signed@test.com", "conta-growth-signed")
        valued, _conta_v = _seed_user("growth-valued@test.com", "conta-growth-valued")
        inside = datetime(2026, 9, 25, 9, 0, 0)
        _event(
            event_name=FUNNEL_EVENT_SIGNUP_COMPLETED,
            key="signup-only",
            occurred_at=inside,
            user_id=signed.id,
            metadata_json={"signup_method": "password"},
        )
        _event(
            event_name=FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
            key="first-other",
            occurred_at=inside,
            user_id=valued.id,
            metadata_json={"task_type": TASK_TYPE_CLEIDE_AUDIT},
        )
        _event(
            event_name=FUNNEL_EVENT_PLAN_SELECTED,
            key="plan-other",
            occurred_at=inside,
            user_id=valued.id,
            metadata_json={"plan": "starter"},
        )
        db.session.commit()

        payload = get_admin_growth_dashboard_payload(days=30, now_utc=NOW)
        signup_rate = _rate(payload, "signup_to_first_relevant")
        plan_rate = _rate(payload, "first_relevant_to_plan_selected")
        assert signup_rate["numerator"] == 0
        assert signup_rate["denominator"] == 1
        assert signup_rate["rate"] == 0.0
        assert plan_rate["numerator"] == 1
        assert plan_rate["denominator"] == 1
        assert plan_rate["rate"] == 1.0
        assert _metric(_stage(payload, "pricing"), "occurrences") == 0
        assert _metric(_stage(payload, "plan_selected"), "distinct_users") == 1


def test_unavailable_payload_does_not_invent_zero(app):
    with app.app_context():
        payload = unavailable_admin_growth_dashboard_payload(days=7, now_utc=NOW)
        assert payload["service_failed"] is True
        assert payload["has_data"] is False
        assert _metric(_stage(payload, "page_view"), "occurrences") is None
        assert _metric(_stage(payload, "paid"), "distinct_contas") is None
        assert _rate(payload, "signup_to_first_relevant")["rate"] is None
        assert _rate(payload, "signup_to_first_relevant")["denominator"] is None


def _build_admin_client(app):
    app.config["SECRET_KEY"] = "test-secret-admin-growth"
    app.config["TESTING"] = True
    app.config["SERVER_NAME"] = "localhost"
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("SECRET_KEY", "test-secret-admin-growth")
    from app.painel_admin.admin_routes import admin_bp

    if "admin" not in app.blueprints:
        app.register_blueprint(admin_bp)
    if "login" not in app.view_functions:
        app.add_url_rule("/login", "login", lambda: "login")
    if "index" not in app.view_functions:
        app.add_url_rule("/", "index", lambda: "home")
    login_manager.init_app(app)

    @login_manager.user_loader
    def _load_user(user_id):  # noqa: ANN001
        return get_user_by_id(user_id)

    return app.test_client()


def _login(client, user_id: int) -> None:
    from flask import g

    # O client reutiliza o app context do teste; g._login_user de um
    # request anterior não pode mascarar o próximo login.
    if hasattr(g, "_login_user"):
        delattr(g, "_login_user")
    user = _AuthUser(str(user_id))
    with client.session_transaction() as sess:
        sess["_user_id"] = user.get_id()
        sess["_fresh"] = True
        sess["_id"] = "test-session"


def test_admin_growth_route_renders_nav_and_blocks_non_admin(app):
    with app.app_context():
        admin, _conta = _seed_user("growth-admin@test.com", "conta-growth-admin")
        admin.is_admin = True
        db.session.add(admin)
        outsider, _conta_out = _seed_user("growth-user@test.com", "conta-growth-user")
        from app.models import utcnow_naive

        _event(
            event_name=FUNNEL_EVENT_PAGE_VIEW,
            key="route-pv",
            occurred_at=utcnow_naive() - timedelta(minutes=1),
            metadata_json={"page": "index"},
        )
        db.session.commit()
        admin_id = admin.id
        outsider_id = outsider.id

        client = _build_admin_client(app)

        anonymous = client.get("/admin/growth")
        assert anonymous.status_code == 302

        _login(client, outsider_id)
        denied = client.get("/admin/growth")
        assert denied.status_code == 403

        _login(client, admin_id)
        response = client.get("/admin/growth?days=7")
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert "<h4 class=\"mb-0\">Growth</h4>" in html
        assert "Page views — ocorrências" in html
        assert ">1</span>" in html
        assert 'href="/admin/growth"' in html
        assert "bi-graph-up" in html
        assert "Últimos 7 dias" in html
