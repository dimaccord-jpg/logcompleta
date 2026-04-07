"""multiuser: codigos de acesso por franquia

Revision ID: m6n7o8p9q0r1
Revises: l5m6n7o8p9q0
Create Date: 2026-04-04
"""
from alembic import op
import sqlalchemy as sa


revision = "m6n7o8p9q0r1"
down_revision = "l5m6n7o8p9q0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "multiuser_franquia_codigo",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conta_id", sa.Integer(), nullable=False),
        sa.Column("franquia_id", sa.Integer(), nullable=False),
        sa.Column("codigo", sa.String(length=64), nullable=False),
        sa.Column("ativo", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("criado_por_user_id", sa.Integer(), nullable=True),
        sa.Column("criado_em", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["conta_id"], ["conta.id"]),
        sa.ForeignKeyConstraint(["franquia_id"], ["franquia.id"]),
        sa.ForeignKeyConstraint(["criado_por_user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("codigo"),
    )
    op.create_index(
        op.f("ix_multiuser_franquia_codigo_conta_id"),
        "multiuser_franquia_codigo",
        ["conta_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_multiuser_franquia_codigo_franquia_id"),
        "multiuser_franquia_codigo",
        ["franquia_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_multiuser_franquia_codigo_codigo"),
        "multiuser_franquia_codigo",
        ["codigo"],
        unique=True,
    )
    op.create_index(
        op.f("ix_multiuser_franquia_codigo_ativo"),
        "multiuser_franquia_codigo",
        ["ativo"],
        unique=False,
    )
    op.create_index(
        op.f("ix_multiuser_franquia_codigo_criado_por_user_id"),
        "multiuser_franquia_codigo",
        ["criado_por_user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_multiuser_franquia_codigo_criado_em"),
        "multiuser_franquia_codigo",
        ["criado_em"],
        unique=False,
    )


def downgrade():
    op.drop_index(op.f("ix_multiuser_franquia_codigo_criado_em"), table_name="multiuser_franquia_codigo")
    op.drop_index(op.f("ix_multiuser_franquia_codigo_criado_por_user_id"), table_name="multiuser_franquia_codigo")
    op.drop_index(op.f("ix_multiuser_franquia_codigo_ativo"), table_name="multiuser_franquia_codigo")
    op.drop_index(op.f("ix_multiuser_franquia_codigo_codigo"), table_name="multiuser_franquia_codigo")
    op.drop_index(op.f("ix_multiuser_franquia_codigo_franquia_id"), table_name="multiuser_franquia_codigo")
    op.drop_index(op.f("ix_multiuser_franquia_codigo_conta_id"), table_name="multiuser_franquia_codigo")
    op.drop_table("multiuser_franquia_codigo")
