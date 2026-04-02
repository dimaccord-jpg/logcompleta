"""fase 1.1: processing_events (processamento analítico não-LLM)

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-04-01

"""
from alembic import op
import sqlalchemy as sa


revision = "e2f3a4b5c6d7"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "processing_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("agent", sa.String(length=80), nullable=False),
        sa.Column("flow_type", sa.String(length=80), nullable=False),
        sa.Column("processing_type", sa.String(length=40), nullable=False),
        sa.Column("rows_processed", sa.Integer(), nullable=False),
        sa.Column("processing_time_ms", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("processing_events", schema=None) as batch_op:
        batch_op.create_index("ix_processing_events_occurred_at", ["occurred_at"], unique=False)
        batch_op.create_index("ix_processing_events_agent", ["agent"], unique=False)
        batch_op.create_index("ix_processing_events_flow_type", ["flow_type"], unique=False)
        batch_op.create_index("ix_processing_events_processing_type", ["processing_type"], unique=False)
        batch_op.create_index("ix_processing_events_status", ["status"], unique=False)


def downgrade():
    op.drop_table("processing_events")
