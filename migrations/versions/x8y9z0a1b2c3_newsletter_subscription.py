"""newsletter subscription table

Revision ID: x8y9z0a1b2c3
Revises: w7x8y9z0a1b2
Create Date: 2026-08-18 15:00:00.000000

Tabela aditiva de inscrição de newsletter, independente de Lead/User.
Sem FK, sem backfill de dados, sem alteração de leads/users.
"""
from alembic import op
import sqlalchemy as sa


revision = "x8y9z0a1b2c3"
down_revision = "w7x8y9z0a1b2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "newsletter_subscription",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(length=150), nullable=False),
        sa.Column("subscribed_at", sa.DateTime(), nullable=False),
        sa.Column("unsubscribed_at", sa.DateTime(), nullable=True),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("email", name="uq_newsletter_subscription_email"),
    )


def downgrade():
    op.drop_table("newsletter_subscription")
