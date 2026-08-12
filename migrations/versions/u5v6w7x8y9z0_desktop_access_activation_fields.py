"""desktop access activation fields

Revision ID: u5v6w7x8y9z0
Revises: t4u5v6w7x8y9
Create Date: 2026-08-12 12:00:00.000000

Campos aditivos para sequência de ativação pós-cadastro (Lead) e
paridade E2E (DesktopAccessE2ETestRun). Não altera User nem FunnelEvent.
"""
from alembic import op
import sqlalchemy as sa


revision = "u5v6w7x8y9z0"
down_revision = "t4u5v6w7x8y9"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("leads", schema=None) as batch_op:
        batch_op.add_column(sa.Column("activation_email_1_sent_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("activation_email_2_sent_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("activation_opt_out_at", sa.DateTime(), nullable=True))

    with op.batch_alter_table("desktop_access_e2e_test_run", schema=None) as batch_op:
        batch_op.add_column(sa.Column("activation_email_1_sent_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("activation_email_2_sent_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("activation_opt_out_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("activation_sequence_started_at", sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table("desktop_access_e2e_test_run", schema=None) as batch_op:
        batch_op.drop_column("activation_sequence_started_at")
        batch_op.drop_column("activation_opt_out_at")
        batch_op.drop_column("activation_email_2_sent_at")
        batch_op.drop_column("activation_email_1_sent_at")

    with op.batch_alter_table("leads", schema=None) as batch_op:
        batch_op.drop_column("activation_opt_out_at")
        batch_op.drop_column("activation_email_2_sent_at")
        batch_op.drop_column("activation_email_1_sent_at")
