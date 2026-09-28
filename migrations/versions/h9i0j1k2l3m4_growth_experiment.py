"""growth experiment admin registry

Revision ID: h9i0j1k2l3m4
Revises: g8h9i0j1k2l3
Create Date: 2026-09-28 18:50:00.000000

Tabela administrativa de experimentos Growth (SCRUM-149 lote 3).
Não altera FunnelEvent, HomeCtaExperimentEvent nem campanhas externas.
"""
from alembic import op
import sqlalchemy as sa


revision = "h9i0j1k2l3m4"
down_revision = "g8h9i0j1k2l3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "growth_experiment",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("hypothesis", sa.Text(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("origin_campaign", sa.String(length=255), nullable=True),
        sa.Column("change_description", sa.Text(), nullable=True),
        sa.Column("primary_metric", sa.String(length=255), nullable=False),
        sa.Column("observed_result", sa.Text(), nullable=True),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("interpretation", sa.Text(), nullable=True),
        sa.Column("decision", sa.Text(), nullable=True),
        sa.Column("next_action", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade():
    op.drop_table("growth_experiment")
