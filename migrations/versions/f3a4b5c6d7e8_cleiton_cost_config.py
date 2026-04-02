"""cleiton_cost_config — parâmetros custo operacional MVP

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-04-01

"""
from alembic import op
import sqlalchemy as sa


revision = "f3a4b5c6d7e8"
down_revision = "e2f3a4b5c6d7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "cleiton_cost_config",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("runtime_monthly_cost", sa.Float(), nullable=True),
        sa.Column("month_seconds", sa.Integer(), nullable=False),
        sa.Column("allocation_percent", sa.Float(), nullable=False),
        sa.Column("overhead_factor", sa.Float(), nullable=False),
        sa.Column("cost_per_million_tokens", sa.Float(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade():
    op.drop_table("cleiton_cost_config")
