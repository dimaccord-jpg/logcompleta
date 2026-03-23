"""pautas link varchar500 -> text

Revision ID: c4d8e2a1b9f0
Revises: fa9c6cbc64b7
Create Date: 2026-03-23

"""
from alembic import op
import sqlalchemy as sa


revision = "c4d8e2a1b9f0"
down_revision = "fa9c6cbc64b7"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        "pautas",
        "link",
        existing_type=sa.String(length=500),
        type_=sa.Text(),
        existing_nullable=False,
    )


def downgrade():
    op.alter_column(
        "pautas",
        "link",
        existing_type=sa.Text(),
        type_=sa.String(length=500),
        existing_nullable=False,
    )
