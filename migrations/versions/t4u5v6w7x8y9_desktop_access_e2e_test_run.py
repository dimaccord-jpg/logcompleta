"""desktop access e2e test run table

Revision ID: t4u5v6w7x8y9
Revises: s3t4u5v6w7x8
Create Date: 2026-08-11 17:00:00.000000

Tabela aditiva para estado técnico de homologação E2E da jornada Landing Desktop.
Não altera leads nem User; apenas cria desktop_access_e2e_test_run.
"""
from alembic import op
import sqlalchemy as sa


revision = "t4u5v6w7x8y9"
down_revision = "s3t4u5v6w7x8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "desktop_access_e2e_test_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("initial_email_sent_at", sa.DateTime(), nullable=True),
        sa.Column("cta_clicked_at", sa.DateTime(), nullable=True),
        sa.Column("registration_completed_at", sa.DateTime(), nullable=True),
        sa.Column("followup_sent_at", sa.DateTime(), nullable=True),
        sa.Column("opt_out_at", sa.DateTime(), nullable=True),
        sa.Column("first_use_seen_at", sa.DateTime(), nullable=True),
        sa.Column("first_audit_seen_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", name="uq_desktop_access_e2e_test_run_run_id"),
    )
    op.create_index(
        "ix_desktop_access_e2e_test_run_user_id",
        "desktop_access_e2e_test_run",
        ["user_id"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        "ix_desktop_access_e2e_test_run_user_id",
        table_name="desktop_access_e2e_test_run",
    )
    op.drop_table("desktop_access_e2e_test_run")
