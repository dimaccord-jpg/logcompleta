"""cleiton billing apropriacao idempotente upload roberto

Revision ID: n7o8p9q0r1s2
Revises: m6n7o8p9q0r1
Create Date: 2026-04-05
"""
from alembic import op
import sqlalchemy as sa


revision = "n7o8p9q0r1s2"
down_revision = "m6n7o8p9q0r1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "cleiton_billing_apropriacao",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("agent", sa.String(length=80), nullable=False),
        sa.Column("flow_type", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("rows_processed", sa.Integer(), nullable=False),
        sa.Column("processing_time_ms", sa.Integer(), nullable=False),
        sa.Column("processing_event_id", sa.Integer(), nullable=True),
        sa.Column("creditos_apropriados", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("motivo", sa.String(length=80), nullable=True),
        sa.Column("conta_id", sa.Integer(), nullable=True),
        sa.Column("franquia_id", sa.Integer(), nullable=True),
        sa.Column("usuario_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        op.f("ix_cleiton_billing_apropriacao_created_at"),
        "cleiton_billing_apropriacao",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_cleiton_billing_apropriacao_idempotency_key"),
        "cleiton_billing_apropriacao",
        ["idempotency_key"],
        unique=True,
    )
    op.create_index(
        op.f("ix_cleiton_billing_apropriacao_agent"),
        "cleiton_billing_apropriacao",
        ["agent"],
        unique=False,
    )
    op.create_index(
        op.f("ix_cleiton_billing_apropriacao_flow_type"),
        "cleiton_billing_apropriacao",
        ["flow_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_cleiton_billing_apropriacao_status"),
        "cleiton_billing_apropriacao",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_cleiton_billing_apropriacao_processing_event_id"),
        "cleiton_billing_apropriacao",
        ["processing_event_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_cleiton_billing_apropriacao_motivo"),
        "cleiton_billing_apropriacao",
        ["motivo"],
        unique=False,
    )
    op.create_index(
        op.f("ix_cleiton_billing_apropriacao_conta_id"),
        "cleiton_billing_apropriacao",
        ["conta_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_cleiton_billing_apropriacao_franquia_id"),
        "cleiton_billing_apropriacao",
        ["franquia_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_cleiton_billing_apropriacao_usuario_id"),
        "cleiton_billing_apropriacao",
        ["usuario_id"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        op.f("ix_cleiton_billing_apropriacao_usuario_id"),
        table_name="cleiton_billing_apropriacao",
    )
    op.drop_index(
        op.f("ix_cleiton_billing_apropriacao_franquia_id"),
        table_name="cleiton_billing_apropriacao",
    )
    op.drop_index(
        op.f("ix_cleiton_billing_apropriacao_conta_id"),
        table_name="cleiton_billing_apropriacao",
    )
    op.drop_index(
        op.f("ix_cleiton_billing_apropriacao_motivo"),
        table_name="cleiton_billing_apropriacao",
    )
    op.drop_index(
        op.f("ix_cleiton_billing_apropriacao_processing_event_id"),
        table_name="cleiton_billing_apropriacao",
    )
    op.drop_index(
        op.f("ix_cleiton_billing_apropriacao_status"),
        table_name="cleiton_billing_apropriacao",
    )
    op.drop_index(
        op.f("ix_cleiton_billing_apropriacao_flow_type"),
        table_name="cleiton_billing_apropriacao",
    )
    op.drop_index(
        op.f("ix_cleiton_billing_apropriacao_agent"),
        table_name="cleiton_billing_apropriacao",
    )
    op.drop_index(
        op.f("ix_cleiton_billing_apropriacao_idempotency_key"),
        table_name="cleiton_billing_apropriacao",
    )
    op.drop_index(
        op.f("ix_cleiton_billing_apropriacao_created_at"),
        table_name="cleiton_billing_apropriacao",
    )
    op.drop_table("cleiton_billing_apropriacao")
