"""add first_audit_completed_at to user

Revision ID: e8617fb010bf
Revises: r2s3t4u5v6w7
Create Date: 2026-08-06 10:04:08.420213

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e8617fb010bf'
down_revision = 'r2s3t4u5v6w7'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "first_audit_completed_at",
                sa.DateTime(),
                nullable=True,
            )
        )


def downgrade():
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.drop_column("first_audit_completed_at")
