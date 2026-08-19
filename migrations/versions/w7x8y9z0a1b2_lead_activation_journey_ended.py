"""lead activation journey ended fields

Revision ID: w7x8y9z0a1b2
Revises: v6w7x8y9z0a1
Create Date: 2026-08-18 12:30:00.000000

Campos aditivos no Lead para término operacional da jornada de ativação.
Não é opt-out, não altera CommunicationSuppression, não altera User.
Sem backfill de dados.
"""
from alembic import op
import sqlalchemy as sa


revision = "w7x8y9z0a1b2"
down_revision = "v6w7x8y9z0a1"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("leads", schema=None) as batch_op:
        batch_op.add_column(sa.Column("activation_ended_at", sa.DateTime(), nullable=True))
        batch_op.add_column(
            sa.Column("activation_ended_for_user_id", sa.Integer(), nullable=True)
        )


def downgrade():
    with op.batch_alter_table("leads", schema=None) as batch_op:
        batch_op.drop_column("activation_ended_for_user_id")
        batch_op.drop_column("activation_ended_at")
