"""funnel_event: tornar user_id/conta_id/franquia_id nullable (Growth)

Revision ID: g8h9i0j1k2l3
Revises: f7g8h9i0j1k2
Create Date: 2026-09-24

Fundacao Growth (SCRUM-148): permite FunnelEvent anonimos/agregados sem
exigir identidade completa. Preserva FKs, indices e demais colunas.

Downgrade: bloqueado se existirem linhas com identidade nula (nao inventa FKs).
"""
from alembic import op
import sqlalchemy as sa


revision = "g8h9i0j1k2l3"
down_revision = "f7g8h9i0j1k2"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("funnel_event", schema=None) as batch_op:
        batch_op.alter_column(
            "user_id",
            existing_type=sa.Integer(),
            nullable=True,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "conta_id",
            existing_type=sa.Integer(),
            nullable=True,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "franquia_id",
            existing_type=sa.Integer(),
            nullable=True,
            existing_nullable=False,
        )


def downgrade():
    bind = op.get_bind()
    null_count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM funnel_event "
            "WHERE user_id IS NULL OR conta_id IS NULL OR franquia_id IS NULL"
        )
    ).scalar()
    if int(null_count or 0) > 0:
        raise RuntimeError(
            "downgrade de g8h9i0j1k2l3 bloqueado: existem FunnelEvent com "
            "user_id/conta_id/franquia_id nulos. Remova ou corrija essas linhas "
            "manualmente antes de restaurar NOT NULL."
        )

    with op.batch_alter_table("funnel_event", schema=None) as batch_op:
        batch_op.alter_column(
            "franquia_id",
            existing_type=sa.Integer(),
            nullable=False,
            existing_nullable=True,
        )
        batch_op.alter_column(
            "conta_id",
            existing_type=sa.Integer(),
            nullable=False,
            existing_nullable=True,
        )
        batch_op.alter_column(
            "user_id",
            existing_type=sa.Integer(),
            nullable=False,
            existing_nullable=True,
        )
