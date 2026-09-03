"""home cta experiment event table

Revision ID: z0a1b2c3d4e5
Revises: y9z0a1b2c3d4
Create Date: 2026-09-03 14:50:00.000000

Tabela aditiva isolada para o experimento home_chat_cta_v1.
Não altera FunnelEvent, User, Conta nem Franquia.
"""
from alembic import op
import sqlalchemy as sa


revision = "z0a1b2c3d4e5"
down_revision = "y9z0a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "home_cta_experiment_event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("experiment", sa.String(length=40), nullable=False),
        sa.Column("assignment_id", sa.String(length=64), nullable=False),
        sa.Column("variant", sa.String(length=16), nullable=False),
        sa.Column("event_type", sa.String(length=20), nullable=False),
        sa.Column("interaction_origin", sa.String(length=20), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "experiment",
            "assignment_id",
            "event_type",
            name="uq_home_cta_experiment_event_assignment_type",
        ),
    )
    op.create_index(
        "ix_home_cta_experiment_event_experiment_occurred_at",
        "home_cta_experiment_event",
        ["experiment", "occurred_at"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        "ix_home_cta_experiment_event_experiment_occurred_at",
        table_name="home_cta_experiment_event",
    )
    op.drop_table("home_cta_experiment_event")
