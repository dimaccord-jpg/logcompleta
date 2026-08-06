from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.extensions import db
from app.funnel_event_service import (
    FUNNEL_EVENT_FILE_UPLOADED,
    FUNNEL_EVENT_FIRST_AUDIT_COMPLETED,
    FUNNEL_EVENT_FREIGHT_CALCULATED,
    FUNNEL_SOURCE_AGENTE_COMPARA,
    FUNNEL_SOURCE_CLEIDE_AUDIT,
    record_completion_with_first_audit,
    record_funnel_event,
)
from app.models import CleitonBillingApropriacao, FunnelEvent, IaConsumoEvento, MonetizacaoFato, User
from tests.conftest import seed_conta_franquia_cliente, seed_usuario


def _seed_identity(email: str = "funnel@test.com"):
    conta, franquia = seed_conta_franquia_cliente(slug=f"conta-{email.split('@')[0]}")
    user = seed_usuario(franquia.id, conta.id, email=email)
    return conta, franquia, user


def _record_for(user, *, event_name, source, key, **overrides):
    return record_funnel_event(
        event_name=event_name,
        source=source,
        user_id=user.id,
        conta_id=user.conta_id,
        franquia_id=user.franquia_id,
        idempotency_key=key,
        **overrides,
    )


def test_create_file_uploaded_cleide(app):
    with app.app_context():
        _, _, user = _seed_identity("cleide-upload@test.com")
        result = _record_for(
            user,
            event_name=FUNNEL_EVENT_FILE_UPLOADED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            key="funnel-cleide-upload-1",
        )
        db.session.commit()

        assert result["created"] is True
        assert result["event"].event_name == FUNNEL_EVENT_FILE_UPLOADED
        assert result["event"].source == FUNNEL_SOURCE_CLEIDE_AUDIT


def test_create_file_uploaded_agente_compara(app):
    with app.app_context():
        _, _, user = _seed_identity("ac-upload@test.com")
        result = _record_for(
            user,
            event_name=FUNNEL_EVENT_FILE_UPLOADED,
            source=FUNNEL_SOURCE_AGENTE_COMPARA,
            key="funnel-ac-upload-1",
        )
        db.session.commit()

        assert result["created"] is True
        assert result["event"].source == FUNNEL_SOURCE_AGENTE_COMPARA


def test_create_freight_calculated_cleide(app):
    with app.app_context():
        _, _, user = _seed_identity("cleide-calc@test.com")
        result = _record_for(
            user,
            event_name=FUNNEL_EVENT_FREIGHT_CALCULATED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            key="funnel-cleide-calc-1",
        )
        db.session.commit()

        assert result["created"] is True
        assert result["event"].event_name == FUNNEL_EVENT_FREIGHT_CALCULATED


def test_create_freight_calculated_agente_compara(app):
    with app.app_context():
        _, _, user = _seed_identity("ac-calc@test.com")
        result = _record_for(
            user,
            event_name=FUNNEL_EVENT_FREIGHT_CALCULATED,
            source=FUNNEL_SOURCE_AGENTE_COMPARA,
            key="funnel-ac-calc-1",
        )
        db.session.commit()

        assert result["created"] is True
        assert result["event"].event_name == FUNNEL_EVENT_FREIGHT_CALCULATED


def test_create_first_audit_completed_is_allowed(app):
    with app.app_context():
        _, _, user = _seed_identity("first-audit-event@test.com")
        result = _record_for(
            user,
            event_name=FUNNEL_EVENT_FIRST_AUDIT_COMPLETED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            key="funnel-first-audit-1",
        )
        db.session.commit()

        assert result["created"] is True
        assert result["event"].event_name == FUNNEL_EVENT_FIRST_AUDIT_COMPLETED


def test_timestamp_automatic_is_utc_naive(app):
    with app.app_context():
        _, _, user = _seed_identity("utc@test.com")
        result = _record_for(
            user,
            event_name=FUNNEL_EVENT_FILE_UPLOADED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            key="funnel-utc-1",
        )
        db.session.commit()

        occurred_at = result["event"].occurred_at
        assert isinstance(occurred_at, datetime)
        assert occurred_at.tzinfo is None


def test_invalid_event_name(app):
    with app.app_context():
        _, _, user = _seed_identity("bad-event@test.com")
        with pytest.raises(ValueError, match="event_name invalido"):
            _record_for(user, event_name="unknown", source=FUNNEL_SOURCE_CLEIDE_AUDIT, key="bad-event-1")


def test_invalid_source(app):
    with app.app_context():
        _, _, user = _seed_identity("bad-source@test.com")
        with pytest.raises(ValueError, match="source invalida"):
            _record_for(user, event_name=FUNNEL_EVENT_FILE_UPLOADED, source="other", key="bad-source-1")


def test_missing_required_identity(app):
    with app.app_context():
        _, _, user = _seed_identity("missing-identity@test.com")
        with pytest.raises(ValueError, match="user_id e obrigatorio"):
            record_funnel_event(
                event_name=FUNNEL_EVENT_FILE_UPLOADED,
                source=FUNNEL_SOURCE_CLEIDE_AUDIT,
                user_id=None,
                conta_id=user.conta_id,
                franquia_id=user.franquia_id,
                idempotency_key="missing-user-1",
            )


def test_missing_idempotency_key(app):
    with app.app_context():
        _, _, user = _seed_identity("missing-key@test.com")
        with pytest.raises(ValueError, match="idempotency_key e obrigatorio"):
            _record_for(user, event_name=FUNNEL_EVENT_FILE_UPLOADED, source=FUNNEL_SOURCE_CLEIDE_AUDIT, key=" ")


def test_first_call_returns_created_true(app):
    with app.app_context():
        _, _, user = _seed_identity("created-true@test.com")
        result = _record_for(
            user,
            event_name=FUNNEL_EVENT_FILE_UPLOADED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            key="created-true-1",
        )
        assert result["created"] is True


def test_idempotency_key_within_limit_is_accepted(app):
    with app.app_context():
        _, _, user = _seed_identity("limit-ok@test.com")
        key = "k" * 160

        result = _record_for(
            user,
            event_name=FUNNEL_EVENT_FILE_UPLOADED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            key=key,
        )
        db.session.commit()

        assert result["created"] is True
        assert result["event"].idempotency_key == key


def test_idempotency_key_above_limit_is_rejected_without_truncation(app):
    with app.app_context():
        _, _, user = _seed_identity("limit-bad@test.com")
        key = "k" * 161

        with pytest.raises(ValueError, match="idempotency_key excede limite de 160 caracteres"):
            _record_for(
                user,
                event_name=FUNNEL_EVENT_FILE_UPLOADED,
                source=FUNNEL_SOURCE_CLEIDE_AUDIT,
                key=key,
            )

        assert FunnelEvent.query.filter_by(idempotency_key=key[:160]).count() == 0
        assert FunnelEvent.query.filter_by(idempotency_key=key).count() == 0


def test_optional_identifier_above_limit_is_rejected(app):
    with app.app_context():
        _, _, user = _seed_identity("optional-limit@test.com")

        with pytest.raises(ValueError, match="document_id excede limite de 120 caracteres"):
            _record_for(
                user,
                event_name=FUNNEL_EVENT_FILE_UPLOADED,
                source=FUNNEL_SOURCE_CLEIDE_AUDIT,
                key="optional-limit-1",
                document_id="d" * 121,
            )

        assert FunnelEvent.query.filter_by(idempotency_key="optional-limit-1").count() == 0


def test_same_key_same_payload_returns_created_false(app):
    with app.app_context():
        _, _, user = _seed_identity("created-false@test.com")
        first = _record_for(
            user,
            event_name=FUNNEL_EVENT_FILE_UPLOADED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            key="same-key-1",
        )
        second = _record_for(
            user,
            event_name=FUNNEL_EVENT_FILE_UPLOADED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            key="same-key-1",
        )
        db.session.commit()

        assert first["created"] is True
        assert second["created"] is False
        assert second["event"].id == first["event"].id


def test_same_key_different_event_name_is_rejected(app):
    with app.app_context():
        _, _, user = _seed_identity("same-key-event@test.com")
        _record_for(
            user,
            event_name=FUNNEL_EVENT_FILE_UPLOADED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            key="same-key-event-1",
        )

        with pytest.raises(ValueError, match="idempotency_key reutilizada com dados divergentes"):
            _record_for(
                user,
                event_name=FUNNEL_EVENT_FREIGHT_CALCULATED,
                source=FUNNEL_SOURCE_CLEIDE_AUDIT,
                key="same-key-event-1",
            )


def test_same_key_different_source_is_rejected(app):
    with app.app_context():
        _, _, user = _seed_identity("same-key-source@test.com")
        _record_for(
            user,
            event_name=FUNNEL_EVENT_FILE_UPLOADED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            key="same-key-source-1",
        )

        with pytest.raises(ValueError, match="idempotency_key reutilizada com dados divergentes"):
            _record_for(
                user,
                event_name=FUNNEL_EVENT_FILE_UPLOADED,
                source=FUNNEL_SOURCE_AGENTE_COMPARA,
                key="same-key-source-1",
            )


def test_same_key_different_user_is_rejected(app):
    with app.app_context():
        conta, franquia, user_a = _seed_identity("same-key-user-a@test.com")
        user_b = seed_usuario(franquia.id, conta.id, email="same-key-user-b@test.com")
        _record_for(
            user_a,
            event_name=FUNNEL_EVENT_FILE_UPLOADED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            key="same-key-user-1",
        )

        with pytest.raises(ValueError, match="idempotency_key reutilizada com dados divergentes"):
            _record_for(
                user_b,
                event_name=FUNNEL_EVENT_FILE_UPLOADED,
                source=FUNNEL_SOURCE_CLEIDE_AUDIT,
                key="same-key-user-1",
            )


def test_idempotent_conflict_does_not_execute_global_rollback(app):
    with app.app_context():
        _, _, user = _seed_identity("savepoint@test.com")
        _record_for(
            user,
            event_name=FUNNEL_EVENT_FILE_UPLOADED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            key="savepoint-1",
        )
        db.session.flush()

        user.full_name = "Updated Savepoint User"

        with pytest.raises(ValueError, match="idempotency_key reutilizada com dados divergentes"):
            _record_for(
                user,
                event_name=FUNNEL_EVENT_FREIGHT_CALCULATED,
                source=FUNNEL_SOURCE_CLEIDE_AUDIT,
                key="savepoint-1",
            )

        db.session.commit()
        refreshed = db.session.get(User, user.id)
        assert refreshed.full_name == "Updated Savepoint User"


def test_pending_caller_change_remains_persistible_after_idempotent_replay(app):
    with app.app_context():
        _, _, user = _seed_identity("replay-persist@test.com")
        _record_for(
            user,
            event_name=FUNNEL_EVENT_FILE_UPLOADED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            key="replay-persist-1",
        )
        db.session.flush()

        user.full_name = "Replay Persisted User"

        result = _record_for(
            user,
            event_name=FUNNEL_EVENT_FILE_UPLOADED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            key="replay-persist-1",
        )
        db.session.commit()

        refreshed = db.session.get(User, user.id)
        assert result["created"] is False
        assert refreshed.full_name == "Replay Persisted User"


def test_duplicate_does_not_create_second_row(app):
    with app.app_context():
        _, _, user = _seed_identity("dup@test.com")
        _record_for(
            user,
            event_name=FUNNEL_EVENT_FILE_UPLOADED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            key="dup-row-1",
        )
        _record_for(
            user,
            event_name=FUNNEL_EVENT_FILE_UPLOADED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            key="dup-row-1",
        )
        db.session.commit()

        assert FunnelEvent.query.filter_by(idempotency_key="dup-row-1").count() == 1


def test_first_audit_idempotency_replay_returns_existing_row(app):
    with app.app_context():
        _, _, user = _seed_identity("first-audit-replay@test.com")
        first = _record_for(
            user,
            event_name=FUNNEL_EVENT_FIRST_AUDIT_COMPLETED,
            source=FUNNEL_SOURCE_AGENTE_COMPARA,
            key="first-audit-replay-1",
        )
        second = _record_for(
            user,
            event_name=FUNNEL_EVENT_FIRST_AUDIT_COMPLETED,
            source=FUNNEL_SOURCE_AGENTE_COMPARA,
            key="first-audit-replay-1",
        )
        first_id = first["event"].id
        second_id = second["event"].id
        db.session.commit()
        db.session.remove()

        assert first["created"] is True
        assert second["created"] is False
        assert second_id == first_id
        assert FunnelEvent.query.filter_by(idempotency_key="first-audit-replay-1").count() == 1


def test_first_audit_global_key_rejects_cross_product_divergence(app):
    with app.app_context():
        _, _, user = _seed_identity("first-audit-global@test.com")
        _record_for(
            user,
            event_name=FUNNEL_EVENT_FIRST_AUDIT_COMPLETED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            key="funnel:first-audit:global-user-1",
        )

        with pytest.raises(ValueError, match="idempotency_key reutilizada com dados divergentes"):
            _record_for(
                user,
                event_name=FUNNEL_EVENT_FIRST_AUDIT_COMPLETED,
                source=FUNNEL_SOURCE_AGENTE_COMPARA,
                key="funnel:first-audit:global-user-1",
            )




def test_optional_fields_can_be_null(app):
    with app.app_context():
        _, _, user = _seed_identity("nulls@test.com")
        result = _record_for(
            user,
            event_name=FUNNEL_EVENT_FILE_UPLOADED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            key="nulls-1",
        )
        db.session.commit()

        event = result["event"]
        assert event.correlation_id is None
        assert event.document_id is None
        assert event.audit_batch_id is None
        assert event.comparison_id is None
        assert event.execution_id is None
        assert event.metadata_json is None


def test_valid_metadata_json_is_persisted(app):
    with app.app_context():
        _, _, user = _seed_identity("metadata@test.com")
        result = _record_for(
            user,
            event_name=FUNNEL_EVENT_FILE_UPLOADED,
            source=FUNNEL_SOURCE_AGENTE_COMPARA,
            key="metadata-1",
            metadata_json={"step": "upload", "count": 2, "flags": ["a", "b"]},
            correlation_id="corr-1",
            document_id="doc-1",
            audit_batch_id="batch-1",
            comparison_id="cmp-1",
            execution_id="exec-1",
        )
        db.session.commit()

        event = result["event"]
        assert event.metadata_json == {"step": "upload", "count": 2, "flags": ["a", "b"]}
        assert event.correlation_id == "corr-1"
        assert event.document_id == "doc-1"
        assert event.audit_batch_id == "batch-1"
        assert event.comparison_id == "cmp-1"
        assert event.execution_id == "exec-1"


def test_invalid_metadata_json_is_rejected(app):
    with app.app_context():
        _, _, user = _seed_identity("bad-metadata@test.com")
        with pytest.raises(ValueError, match="metadata_json deve ser compativel com JSON"):
            _record_for(
                user,
                event_name=FUNNEL_EVENT_FILE_UPLOADED,
                source=FUNNEL_SOURCE_CLEIDE_AUDIT,
                key="bad-metadata-1",
                metadata_json={"x": {1, 2}},
            )


def test_first_audit_completed_at_is_not_changed(app):
    with app.app_context():
        _, _, user = _seed_identity("first-audit@test.com")
        before = db.session.get(User, user.id).first_audit_completed_at
        _record_for(
            user,
            event_name=FUNNEL_EVENT_FILE_UPLOADED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            key="first-audit-1",
        )
        db.session.commit()
        after = db.session.get(User, user.id).first_audit_completed_at

        assert before is None
        assert after is None


def test_no_billing_record_is_created(app):
    with app.app_context():
        _, _, user = _seed_identity("billing@test.com")
        before = CleitonBillingApropriacao.query.count()
        _record_for(
            user,
            event_name=FUNNEL_EVENT_FILE_UPLOADED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            key="billing-1",
        )
        db.session.commit()

        assert CleitonBillingApropriacao.query.count() == before


def test_no_franchise_consumption_is_created(app):
    with app.app_context():
        _, franquia, user = _seed_identity("franquia@test.com")
        before_consumo = franquia.consumo_acumulado
        before_status = franquia.status
        _record_for(
            user,
            event_name=FUNNEL_EVENT_FREIGHT_CALCULATED,
            source=FUNNEL_SOURCE_AGENTE_COMPARA,
            key="franquia-1",
        )
        db.session.commit()
        refreshed = db.session.get(type(franquia), franquia.id)

        assert refreshed.consumo_acumulado == before_consumo
        assert refreshed.status == before_status


def test_no_ia_call_is_created(app):
    with app.app_context():
        _, _, user = _seed_identity("ia@test.com")
        before = IaConsumoEvento.query.count()
        _record_for(
            user,
            event_name=FUNNEL_EVENT_FILE_UPLOADED,
            source=FUNNEL_SOURCE_AGENTE_COMPARA,
            key="ia-1",
        )
        db.session.commit()

        assert IaConsumoEvento.query.count() == before


def test_no_monetization_fact_is_created(app):
    with app.app_context():
        _, _, user = _seed_identity("money@test.com")
        before = MonetizacaoFato.query.count()
        _record_for(
            user,
            event_name=FUNNEL_EVENT_FREIGHT_CALCULATED,
            source=FUNNEL_SOURCE_CLEIDE_AUDIT,
            key="money-1",
            occurred_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.session.commit()

        assert MonetizacaoFato.query.count() == before
