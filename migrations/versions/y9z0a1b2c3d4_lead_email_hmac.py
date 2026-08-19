"""lead email hmac identity

Revision ID: y9z0a1b2c3d4
Revises: x8y9z0a1b2c3
Create Date: 2026-08-18 17:30:00.000000

Campo aditivo Lead.email_hmac (HMAC-SHA256 hex do e-mail original).
Não torna Lead.email nullable. Sem backfill de dados. Sem índice obrigatório.
"""
from alembic import op
import sqlalchemy as sa


revision = "y9z0a1b2c3d4"
down_revision = "x8y9z0a1b2c3"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("leads", schema=None) as batch_op:
        batch_op.add_column(sa.Column("email_hmac", sa.String(length=64), nullable=True))


def downgrade():
    with op.batch_alter_table("leads", schema=None) as batch_op:
        batch_op.drop_column("email_hmac")
