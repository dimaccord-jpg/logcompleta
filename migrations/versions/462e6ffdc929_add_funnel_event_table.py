"""add funnel event table

Revision ID: 462e6ffdc929
Revises: e8617fb010bf
Create Date: 2026-08-06 10:52:30.263245

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "462e6ffdc929"
down_revision = "e8617fb010bf"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "funnel_event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("conta_id", sa.Integer(), nullable=False),
        sa.Column("franquia_id", sa.Integer(), nullable=False),
        sa.Column("event_name", sa.String(length=40), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("correlation_id", sa.String(length=200), nullable=True),
        sa.Column("document_id", sa.String(length=120), nullable=True),
        sa.Column("audit_batch_id", sa.String(length=120), nullable=True),
        sa.Column("comparison_id", sa.String(length=120), nullable=True),
        sa.Column("execution_id", sa.String(length=120), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["conta_id"], ["conta.id"]),
        sa.ForeignKeyConstraint(["franquia_id"], ["franquia.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_funnel_event_conta_occurred_at",
        "funnel_event",
        ["conta_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_funnel_event_event_source_occurred_at",
        "funnel_event",
        ["event_name", "source", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_funnel_event_franquia_occurred_at",
        "funnel_event",
        ["franquia_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_funnel_event_user_event_source",
        "funnel_event",
        ["user_id", "event_name", "source"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_funnel_event_user_event_source", table_name="funnel_event")
    op.drop_index("ix_funnel_event_franquia_occurred_at", table_name="funnel_event")
    op.drop_index("ix_funnel_event_event_source_occurred_at", table_name="funnel_event")
    op.drop_index("ix_funnel_event_conta_occurred_at", table_name="funnel_event")
    op.drop_table("funnel_event")
