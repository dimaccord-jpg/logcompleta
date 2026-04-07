"""franquia: bloqueio manual operacional (Cleiton Fase 2 etapa 3.2/3.3)

Revision ID: k4l5m6n7o8p9
Revises: j3k4l5m6n7o8
Create Date: 2026-04-04
"""
from alembic import op
import sqlalchemy as sa


revision = "k4l5m6n7o8p9"
down_revision = "j3k4l5m6n7o8"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("franquia", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "bloqueio_manual",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            )
        )


def downgrade():
    with op.batch_alter_table("franquia", schema=None) as batch_op:
        batch_op.drop_column("bloqueio_manual")
