"""fase 5: painel do contratante e aumento automatico por ciclo

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-09-12

Contador cumulativo de aumento automático por ciclo comercial e operação
idempotente de aumento de assentos. Não antecipa F6/F7/F8.
"""
from alembic import op
import sqlalchemy as sa


revision = "d5e6f7a8b9c0"
down_revision = "c4d5e6f7a8b9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "conta_multiuser_ciclo_aumento",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conta_id", sa.Integer(), nullable=False),
        sa.Column("ciclo_inicio", sa.DateTime(), nullable=False),
        sa.Column("ciclo_fim", sa.DateTime(), nullable=False),
        sa.Column("acumulado_automatico", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint(
            "acumulado_automatico >= 0",
            name="ck_conta_multiuser_ciclo_aumento_acumulado",
        ),
        sa.ForeignKeyConstraint(["conta_id"], ["conta.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conta_id",
            "ciclo_inicio",
            "ciclo_fim",
            name="uq_conta_multiuser_ciclo_aumento_ciclo",
        ),
    )
    op.create_index(
        "ix_conta_multiuser_ciclo_aumento_conta_id",
        "conta_multiuser_ciclo_aumento",
        ["conta_id"],
        unique=False,
    )
    op.create_table(
        "conta_multiuser_aumento_operacao",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conta_id", sa.Integer(), nullable=False),
        sa.Column("solicitado_por_user_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("quantidade_solicitada", sa.Integer(), nullable=False),
        sa.Column("quantity_anterior", sa.Integer(), nullable=False),
        sa.Column("quantity_nova", sa.Integer(), nullable=True),
        sa.Column("estado", sa.String(length=40), nullable=False),
        sa.Column("stripe_customer_id", sa.String(length=160), nullable=True),
        sa.Column("stripe_subscription_id", sa.String(length=160), nullable=True),
        sa.Column("stripe_subscription_item_id", sa.String(length=160), nullable=True),
        sa.Column("stripe_quantity_enviada", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint(
            "estado IN ("
            "'iniciado', 'stripe_enviado', 'aprovado_automatico', "
            "'enviado_analise', 'falha_reconciliacao'"
            ")",
            name="ck_conta_multiuser_aumento_operacao_estado",
        ),
        sa.CheckConstraint(
            "quantidade_solicitada >= 1",
            name="ck_conta_multiuser_aumento_operacao_qtd",
        ),
        sa.ForeignKeyConstraint(["conta_id"], ["conta.id"]),
        sa.ForeignKeyConstraint(["solicitado_por_user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("correlation_id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_conta_multiuser_aumento_operacao_idempotency",
        ),
    )
    op.create_index(
        "ix_conta_multiuser_aumento_operacao_conta_id",
        "conta_multiuser_aumento_operacao",
        ["conta_id"],
        unique=False,
    )
    op.create_index(
        "ix_conta_multiuser_aumento_operacao_estado",
        "conta_multiuser_aumento_operacao",
        ["estado"],
        unique=False,
    )
    op.create_index(
        "ix_conta_multiuser_aumento_operacao_solicitado_por_user_id",
        "conta_multiuser_aumento_operacao",
        ["solicitado_por_user_id"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        "ix_conta_multiuser_aumento_operacao_solicitado_por_user_id",
        table_name="conta_multiuser_aumento_operacao",
    )
    op.drop_index(
        "ix_conta_multiuser_aumento_operacao_estado",
        table_name="conta_multiuser_aumento_operacao",
    )
    op.drop_index(
        "ix_conta_multiuser_aumento_operacao_conta_id",
        table_name="conta_multiuser_aumento_operacao",
    )
    op.drop_table("conta_multiuser_aumento_operacao")
    op.drop_index(
        "ix_conta_multiuser_ciclo_aumento_conta_id",
        table_name="conta_multiuser_ciclo_aumento",
    )
    op.drop_table("conta_multiuser_ciclo_aumento")
