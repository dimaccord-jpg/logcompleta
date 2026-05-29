"""onboarding word cloud hidden term table

Revision ID: r2s3t4u5v6w7
Revises: q1r2s3t4u5v6
Create Date: 2026-05-29 12:00:00.000000

Persistência escolhida: tabela dedicada (não ConfigRegras) para permitir
auditoria por termo, reativação individual e índice em term_normalized.
"""
from alembic import op
import sqlalchemy as sa


revision = "r2s3t4u5v6w7"
down_revision = "q1r2s3t4u5v6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "onboarding_word_cloud_hidden_term",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("term_normalized", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("hidden_by_user_id", sa.Integer(), nullable=True),
        sa.Column("notes", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(["hidden_by_user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("onboarding_word_cloud_hidden_term", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_onboarding_word_cloud_hidden_term_is_active"),
            ["is_active"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_onboarding_word_cloud_hidden_term_term_normalized"),
            ["term_normalized"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_onboarding_word_cloud_hidden_term_hidden_by_user_id"),
            ["hidden_by_user_id"],
            unique=False,
        )


def downgrade():
    with op.batch_alter_table("onboarding_word_cloud_hidden_term", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_onboarding_word_cloud_hidden_term_hidden_by_user_id"))
        batch_op.drop_index(batch_op.f("ix_onboarding_word_cloud_hidden_term_term_normalized"))
        batch_op.drop_index(batch_op.f("ix_onboarding_word_cloud_hidden_term_is_active"))

    op.drop_table("onboarding_word_cloud_hidden_term")
