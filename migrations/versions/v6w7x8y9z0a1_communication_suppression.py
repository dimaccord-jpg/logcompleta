"""communication suppression table

Revision ID: v6w7x8y9z0a1
Revises: u5v6w7x8y9z0
Create Date: 2026-08-18 12:00:00.000000

Tabela aditiva de suppression por finalidade (HMAC de e-mail).
Sem plaintext, sem FK para Lead/User, sem backfill de dados.
"""
from alembic import op
import sqlalchemy as sa


revision = "v6w7x8y9z0a1"
down_revision = "u5v6w7x8y9z0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "communication_suppression",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email_hmac", sa.String(length=64), nullable=False),
        sa.Column("purpose", sa.String(length=64), nullable=False),
        sa.Column("suppressed_at", sa.DateTime(), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "email_hmac",
            "purpose",
            name="uq_communication_suppression_email_hmac_purpose",
        ),
    )


def downgrade():
    op.drop_table("communication_suppression")
