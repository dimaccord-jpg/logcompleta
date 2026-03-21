"""adiciona terms_of_use

Revision ID: f28d05a0c94e
Revises: bf635058e244
Create Date: 2026-03-19 10:13:55.901488

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f28d05a0c94e'
down_revision = 'bf635058e244'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'terms_of_use',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column('upload_date', sa.DateTime(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('terms_of_use', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_terms_of_use_is_active'), ['is_active'], unique=False)


def downgrade():
    with op.batch_alter_table('terms_of_use', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_terms_of_use_is_active'))

    op.drop_table('terms_of_use')