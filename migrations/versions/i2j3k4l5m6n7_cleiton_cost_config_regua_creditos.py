"""cleiton_cost_config: régua de conversão de créditos (tokens, linhas, ms)

Revision ID: i2j3k4l5m6n7
Revises: h1i2j3k4l5m6
Create Date: 2026-04-03

Campos nullable: sem backfill; valores definidos pela tela /admin/agentes/cleiton.
"""
from alembic import op
import sqlalchemy as sa


revision = "i2j3k4l5m6n7"
down_revision = "h1i2j3k4l5m6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("cleiton_cost_config", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("credit_tokens_per_credit", sa.Float(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("credit_lines_per_credit", sa.Float(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("credit_ms_per_credit", sa.Float(), nullable=True)
        )


def downgrade():
    with op.batch_alter_table("cleiton_cost_config", schema=None) as batch_op:
        batch_op.drop_column("credit_ms_per_credit")
        batch_op.drop_column("credit_lines_per_credit")
        batch_op.drop_column("credit_tokens_per_credit")
