"""Testes direcionados do Lote 3D SCRUM-148: first_relevant_task_completed."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from app.funnel_event_service import (
    FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
    FUNNEL_EVENT_TASK_COMPLETED,
    FUNNEL_SOURCE_CLEIDE_AUDIT,
    FUNNEL_SOURCE_GROWTH,
    FUNNEL_SOURCE_JULIA_CHAT,
    FUNNEL_SOURCE_ONBOARDING_DISCOVERY,
    META_PIXEL_ALLOWED_EVENTS,
    TASK_TYPE_CLEIDE_AUDIT,
    TASK_TYPE_JULIA_CHAT,
    TASK_TYPE_ONBOARDING_DISCOVERY,
    TASK_TYPE_ROBERTO_BI,
    _first_relevant_idempotency_key,
    is_meta_pixel_allowed,
    try_record_funnel_event,
    try_record_growth_task_event,
)
from app.models import FunnelEvent
from tests.conftest import seed_conta_franquia_cliente, seed_usuario


def _auth_user(monkeypatch, *, email: str):
    conta, franquia = seed_conta_franquia_cliente(slug=f"lote3d-{email.split('@')[0]}")
    user = seed_usuario(franquia.id, conta.id, email=email)
    fake = SimpleNamespace(
        is_authenticated=True,
        id=user.id,
        conta_id=conta.id,
        franquia_id=franquia.id,
    )
    monkeypatch.setattr("flask_login.utils._get_user", lambda: fake)
    monkeypatch.setattr(
        "app.funnel_event_service._is_desktop_access_admin_test_mode",
        lambda: False,
    )
    return user


def _marcos(user_id: int):
    return (
        FunnelEvent.query.filter_by(
            user_id=user_id,
            event_name=FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
        )
        .order_by(FunnelEvent.id.asc())
        .all()
    )


def test_first_eligible_completion_creates_marco(app, ctx, monkeypatch):
    with app.app_context():
        user = _auth_user(monkeypatch, email="lote3d-first@test.com")
        result = try_record_growth_task_event(
            event_name=FUNNEL_EVENT_TASK_COMPLETED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            task_type=TASK_TYPE_CLEIDE_AUDIT,
            idempotency_key="growth:lote3d:first:task_completed",
            user=user,
            execution_id="exec-lote3d-first",
        )
        assert result is not None
        assert result["created"] is True

        marcos = _marcos(user.id)
        assert len(marcos) == 1
        marco = marcos[0]
        assert marco.source == FUNNEL_SOURCE_GROWTH
        assert marco.user_id == user.id
        assert marco.metadata_json == {"task_type": TASK_TYPE_CLEIDE_AUDIT}
        assert marco.idempotency_key == _first_relevant_idempotency_key(user.id)
        assert marco.occurred_at == result["event"].occurred_at


def test_second_completion_same_user_does_not_change_marco(app, ctx, monkeypatch):
    with app.app_context():
        user = _auth_user(monkeypatch, email="lote3d-second@test.com")
        first = try_record_growth_task_event(
            event_name=FUNNEL_EVENT_TASK_COMPLETED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            task_type=TASK_TYPE_CLEIDE_AUDIT,
            idempotency_key="growth:lote3d:second:a",
            user=user,
        )
        assert first is not None
        marco_id = _marcos(user.id)[0].id
        marco_occurred = _marcos(user.id)[0].occurred_at

        second = try_record_growth_task_event(
            event_name=FUNNEL_EVENT_TASK_COMPLETED,
            source=FUNNEL_SOURCE_JULIA_CHAT,
            task_type=TASK_TYPE_JULIA_CHAT,
            idempotency_key="growth:lote3d:second:b",
            user=user,
        )
        assert second is not None
        assert second["created"] is True

        marcos = _marcos(user.id)
        assert len(marcos) == 1
        assert marcos[0].id == marco_id
        assert marcos[0].occurred_at == marco_occurred
        assert marcos[0].metadata_json == {"task_type": TASK_TYPE_CLEIDE_AUDIT}


def test_different_users_create_independent_marcos(app, ctx, monkeypatch):
    with app.app_context():
        user_a = _auth_user(monkeypatch, email="lote3d-user-a@test.com")
        try_record_growth_task_event(
            event_name=FUNNEL_EVENT_TASK_COMPLETED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            task_type=TASK_TYPE_CLEIDE_AUDIT,
            idempotency_key="growth:lote3d:users:a",
            user=user_a,
        )

        user_b = _auth_user(monkeypatch, email="lote3d-user-b@test.com")
        try_record_growth_task_event(
            event_name=FUNNEL_EVENT_TASK_COMPLETED,
            source=FUNNEL_SOURCE_JULIA_CHAT,
            task_type=TASK_TYPE_JULIA_CHAT,
            idempotency_key="growth:lote3d:users:b",
            user=user_b,
        )

        marcos_a = _marcos(user_a.id)
        marcos_b = _marcos(user_b.id)
        assert len(marcos_a) == 1
        assert len(marcos_b) == 1
        assert marcos_a[0].id != marcos_b[0].id
        assert marcos_a[0].metadata_json == {"task_type": TASK_TYPE_CLEIDE_AUDIT}
        assert marcos_b[0].metadata_json == {"task_type": TASK_TYPE_JULIA_CHAT}
        assert marcos_a[0].idempotency_key == _first_relevant_idempotency_key(user_a.id)
        assert marcos_b[0].idempotency_key == _first_relevant_idempotency_key(user_b.id)


def test_discovery_does_not_create_marco(app, ctx, monkeypatch):
    with app.app_context():
        user = _auth_user(monkeypatch, email="lote3d-discovery@test.com")
        result = try_record_growth_task_event(
            event_name=FUNNEL_EVENT_TASK_COMPLETED,
            source=FUNNEL_SOURCE_ONBOARDING_DISCOVERY,
            task_type=TASK_TYPE_ONBOARDING_DISCOVERY,
            idempotency_key="growth:lote3d:discovery:task_completed",
            user=user,
        )
        assert result is not None
        assert result["created"] is True
        assert result["event"].metadata_json["task_type"] == TASK_TYPE_ONBOARDING_DISCOVERY
        assert len(_marcos(user.id)) == 0


def test_task_completed_without_user_id_does_not_create_marco(app, ctx, monkeypatch):
    monkeypatch.setattr(
        "app.funnel_event_service._is_desktop_access_admin_test_mode",
        lambda: False,
    )
    with app.app_context():
        monkeypatch.setattr(
            "flask_login.utils._get_user",
            lambda: SimpleNamespace(is_authenticated=False),
        )
        result = try_record_growth_task_event(
            event_name=FUNNEL_EVENT_TASK_COMPLETED,
            source=FUNNEL_SOURCE_JULIA_CHAT,
            task_type=TASK_TYPE_JULIA_CHAT,
            idempotency_key="growth:lote3d:anon:task_completed",
        )
        assert result is not None
        assert result["created"] is True
        assert result["event"].user_id is None
        assert (
            FunnelEvent.query.filter_by(
                event_name=FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED
            ).count()
            == 0
        )


def test_recovery_chooses_earlier_completion_a(app, ctx, monkeypatch):
    """A ja existe sem marco; B dispara ensure e o marco escolhe A."""
    with app.app_context():
        user = _auth_user(monkeypatch, email="lote3d-recovery@test.com")
        t0 = datetime(2026, 1, 1, 12, 0, 0)

        a = try_record_funnel_event(
            event_name=FUNNEL_EVENT_TASK_COMPLETED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            user_id=user.id,
            conta_id=user.conta_id,
            franquia_id=user.franquia_id,
            idempotency_key="growth:lote3d:recovery:a",
            occurred_at=t0,
            metadata_json={"task_type": TASK_TYPE_CLEIDE_AUDIT},
        )
        assert a is not None
        assert a["created"] is True
        assert len(_marcos(user.id)) == 0

        b = try_record_growth_task_event(
            event_name=FUNNEL_EVENT_TASK_COMPLETED,
            source=FUNNEL_SOURCE_JULIA_CHAT,
            task_type=TASK_TYPE_JULIA_CHAT,
            idempotency_key="growth:lote3d:recovery:b",
            user=user,
        )
        assert b is not None
        assert b["created"] is True

        marcos = _marcos(user.id)
        assert len(marcos) == 1
        assert marcos[0].metadata_json == {"task_type": TASK_TYPE_CLEIDE_AUDIT}
        assert marcos[0].occurred_at == t0


def test_retry_created_false_also_attempts_recovery(app, ctx, monkeypatch):
    with app.app_context():
        user = _auth_user(monkeypatch, email="lote3d-retry@test.com")
        t0 = datetime(2026, 2, 1, 10, 0, 0)

        seeded = try_record_funnel_event(
            event_name=FUNNEL_EVENT_TASK_COMPLETED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            user_id=user.id,
            conta_id=user.conta_id,
            franquia_id=user.franquia_id,
            idempotency_key="growth:lote3d:retry:seed",
            occurred_at=t0,
            metadata_json={"task_type": TASK_TYPE_ROBERTO_BI},
        )
        assert seeded is not None
        assert len(_marcos(user.id)) == 0

        first_attempt = try_record_growth_task_event(
            event_name=FUNNEL_EVENT_TASK_COMPLETED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            task_type=TASK_TYPE_ROBERTO_BI,
            idempotency_key="growth:lote3d:retry:seed",
            user=user,
        )
        assert first_attempt is not None
        assert first_attempt["created"] is False

        marcos = _marcos(user.id)
        assert len(marcos) == 1
        assert marcos[0].metadata_json == {"task_type": TASK_TYPE_ROBERTO_BI}
        assert marcos[0].occurred_at == t0
        assert marcos[0].idempotency_key == _first_relevant_idempotency_key(user.id)


def test_at_most_one_marco_per_user_idempotency(app, ctx, monkeypatch):
    with app.app_context():
        user = _auth_user(monkeypatch, email="lote3d-uniq@test.com")
        try_record_growth_task_event(
            event_name=FUNNEL_EVENT_TASK_COMPLETED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            task_type=TASK_TYPE_CLEIDE_AUDIT,
            idempotency_key="growth:lote3d:uniq:a",
            user=user,
        )
        try_record_growth_task_event(
            event_name=FUNNEL_EVENT_TASK_COMPLETED,
            source=FUNNEL_SOURCE_JULIA_CHAT,
            task_type=TASK_TYPE_JULIA_CHAT,
            idempotency_key="growth:lote3d:uniq:b",
            user=user,
        )
        # Segunda chamada com mesma chave de marco (via ensure interno) nao duplica.
        try_record_growth_task_event(
            event_name=FUNNEL_EVENT_TASK_COMPLETED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            task_type=TASK_TYPE_CLEIDE_AUDIT,
            idempotency_key="growth:lote3d:uniq:a",
            user=user,
        )

        marcos = _marcos(user.id)
        assert len(marcos) == 1
        assert (
            FunnelEvent.query.filter_by(
                idempotency_key=_first_relevant_idempotency_key(user.id)
            ).count()
            == 1
        )


def test_first_relevant_remains_outside_meta_allowlist():
    assert FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED not in META_PIXEL_ALLOWED_EVENTS
    assert is_meta_pixel_allowed(FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED) is False
