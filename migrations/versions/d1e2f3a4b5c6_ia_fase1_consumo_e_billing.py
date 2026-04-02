"""ia fase1: consumo eventos + snapshot custo billing

Revision ID: d1e2f3a4b5c6
Revises: c4d8e2a1b9f0
Create Date: 2026-04-01

"""
from alembic import op
import sqlalchemy as sa


revision = "d1e2f3a4b5c6"
down_revision = "c4d8e2a1b9f0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ia_consumo_evento",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("operation", sa.String(length=40), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("agent", sa.String(length=80), nullable=False),
        sa.Column("flow_type", sa.String(length=80), nullable=False),
        sa.Column("api_key_label", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("ia_consumo_evento", schema=None) as batch_op:
        batch_op.create_index("ix_ia_consumo_evento_occurred_at", ["occurred_at"], unique=False)
        batch_op.create_index("ix_ia_consumo_evento_provider", ["provider"], unique=False)
        batch_op.create_index("ix_ia_consumo_evento_operation", ["operation"], unique=False)
        batch_op.create_index("ix_ia_consumo_evento_agent", ["agent"], unique=False)
        batch_op.create_index("ix_ia_consumo_evento_flow_type", ["flow_type"], unique=False)
        batch_op.create_index("ix_ia_consumo_evento_api_key_label", ["api_key_label"], unique=False)
        batch_op.create_index("ix_ia_consumo_evento_status", ["status"], unique=False)

    op.create_table(
        "ia_billing_cost_snapshot",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("snapshot_at", sa.DateTime(), nullable=False),
        sa.Column("reference_date", sa.Date(), nullable=False),
        sa.Column("month_competence", sa.String(length=7), nullable=False),
        sa.Column("cost_total_month_to_date", sa.Numeric(18, 6), nullable=False),
        sa.Column("currency", sa.String(length=12), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("ia_billing_cost_snapshot", schema=None) as batch_op:
        batch_op.create_index("ix_ia_billing_cost_snapshot_snapshot_at", ["snapshot_at"], unique=False)
        batch_op.create_index("ix_ia_billing_cost_snapshot_reference_date", ["reference_date"], unique=False)
        batch_op.create_index("ix_ia_billing_cost_snapshot_month_competence", ["month_competence"], unique=False)


def downgrade():
    op.drop_table("ia_billing_cost_snapshot")
    op.drop_table("ia_consumo_evento")
