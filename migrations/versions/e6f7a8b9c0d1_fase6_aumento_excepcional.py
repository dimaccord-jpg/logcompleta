"""fase 6: solicitacao excepcional, snapshot e cobranca extraordinaria

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-09-13

Complementa a operação F5 (enviado_analise) com estado comercial,
snapshot proporcional imutável e cobrança extraordinária. Não altera
a máquina automática da Fase 5.
"""
from alembic import op
import sqlalchemy as sa


revision = "e6f7a8b9c0d1"
down_revision = "d5e6f7a8b9c0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "conta_multiuser_aumento_excepcional",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("operacao_id", sa.Integer(), nullable=False),
        sa.Column("conta_id", sa.Integer(), nullable=False),
        sa.Column("solicitante_id", sa.Integer(), nullable=False),
        sa.Column("administrador_id", sa.Integer(), nullable=True),
        sa.Column("ciclo_inicio", sa.DateTime(), nullable=False),
        sa.Column("ciclo_fim", sa.DateTime(), nullable=False),
        sa.Column("quantity_atual", sa.Integer(), nullable=False),
        sa.Column("quantidade_solicitada", sa.Integer(), nullable=False),
        sa.Column("quantidade_aprovada", sa.Integer(), nullable=True),
        sa.Column("acumulado_automatico_ciclo", sa.Integer(), nullable=False),
        sa.Column("estado", sa.String(length=40), nullable=False),
        sa.Column("decisao", sa.String(length=20), nullable=True),
        sa.Column("decidido_em", sa.DateTime(), nullable=True),
        sa.Column("decisao_idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("versao", sa.Integer(), nullable=False),
        sa.Column("preco_unitario_centavos", sa.Integer(), nullable=True),
        sa.Column("instante_calculo", sa.DateTime(), nullable=True),
        sa.Column("timezone_calculo", sa.String(length=40), nullable=True),
        sa.Column("dias_totais", sa.Integer(), nullable=True),
        sa.Column("dias_restantes", sa.Integer(), nullable=True),
        sa.Column("valor_calculado_centavos", sa.Integer(), nullable=True),
        sa.Column("versao_formula", sa.String(length=40), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("stripe_checkout_session_id", sa.String(length=200), nullable=True),
        sa.Column("stripe_payment_intent_id", sa.String(length=200), nullable=True),
        sa.Column("stripe_checkout_url", sa.String(length=500), nullable=True),
        sa.Column("stripe_customer_id", sa.String(length=160), nullable=True),
        sa.Column("liberado_em", sa.DateTime(), nullable=True),
        sa.Column("quantity_nova", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint(
            "estado IN ("
            "'em_analise', 'rejeitada', 'aprovada_gratuita', "
            "'aguardando_pagamento', 'pagamento_confirmado', 'liberada', "
            "'expirada', 'reconciliacao_necessaria'"
            ")",
            name="ck_conta_multiuser_aumento_excepcional_estado",
        ),
        sa.CheckConstraint(
            "quantidade_solicitada >= 1",
            name="ck_conta_multiuser_aumento_excepcional_qtd_sol",
        ),
        sa.CheckConstraint(
            "quantidade_aprovada IS NULL OR "
            "(quantidade_aprovada >= 1 AND quantidade_aprovada <= quantidade_solicitada)",
            name="ck_conta_multiuser_aumento_excepcional_qtd_apr",
        ),
        sa.CheckConstraint(
            "versao >= 1",
            name="ck_conta_multiuser_aumento_excepcional_versao",
        ),
        sa.ForeignKeyConstraint(["operacao_id"], ["conta_multiuser_aumento_operacao.id"]),
        sa.ForeignKeyConstraint(["conta_id"], ["conta.id"]),
        sa.ForeignKeyConstraint(["solicitante_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["administrador_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "operacao_id",
            name="uq_conta_multiuser_aumento_excepcional_operacao",
        ),
        sa.UniqueConstraint(
            "correlation_id",
            name="uq_conta_multiuser_aumento_excepcional_correlation",
        ),
    )
    op.create_index(
        "ix_conta_multiuser_aumento_excepcional_conta_id",
        "conta_multiuser_aumento_excepcional",
        ["conta_id"],
        unique=False,
    )
    op.create_index(
        "ix_conta_multiuser_aumento_excepcional_estado",
        "conta_multiuser_aumento_excepcional",
        ["estado"],
        unique=False,
    )
    op.create_index(
        "ix_conta_multiuser_aumento_excepcional_solicitante_id",
        "conta_multiuser_aumento_excepcional",
        ["solicitante_id"],
        unique=False,
    )
    op.create_index(
        "ix_conta_multiuser_aumento_excepcional_request_id",
        "conta_multiuser_aumento_excepcional",
        ["request_id"],
        unique=False,
    )
    op.create_index(
        "ix_conta_multiuser_aumento_excepcional_expires_at",
        "conta_multiuser_aumento_excepcional",
        ["expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_conta_multiuser_aumento_excepcional_payment_intent",
        "conta_multiuser_aumento_excepcional",
        ["stripe_payment_intent_id"],
        unique=False,
    )
    op.create_index(
        "uq_conta_multiuser_aumento_excepcional_decisao_idem",
        "conta_multiuser_aumento_excepcional",
        ["decisao_idempotency_key"],
        unique=True,
        postgresql_where=sa.text("decisao_idempotency_key IS NOT NULL"),
        sqlite_where=sa.text("decisao_idempotency_key IS NOT NULL"),
    )
    op.create_index(
        "uq_conta_multiuser_aumento_excepcional_checkout",
        "conta_multiuser_aumento_excepcional",
        ["stripe_checkout_session_id"],
        unique=True,
        postgresql_where=sa.text("stripe_checkout_session_id IS NOT NULL"),
        sqlite_where=sa.text("stripe_checkout_session_id IS NOT NULL"),
    )


def downgrade():
    op.drop_index(
        "uq_conta_multiuser_aumento_excepcional_checkout",
        table_name="conta_multiuser_aumento_excepcional",
    )
    op.drop_index(
        "uq_conta_multiuser_aumento_excepcional_decisao_idem",
        table_name="conta_multiuser_aumento_excepcional",
    )
    op.drop_index(
        "ix_conta_multiuser_aumento_excepcional_payment_intent",
        table_name="conta_multiuser_aumento_excepcional",
    )
    op.drop_index(
        "ix_conta_multiuser_aumento_excepcional_expires_at",
        table_name="conta_multiuser_aumento_excepcional",
    )
    op.drop_index(
        "ix_conta_multiuser_aumento_excepcional_request_id",
        table_name="conta_multiuser_aumento_excepcional",
    )
    op.drop_index(
        "ix_conta_multiuser_aumento_excepcional_solicitante_id",
        table_name="conta_multiuser_aumento_excepcional",
    )
    op.drop_index(
        "ix_conta_multiuser_aumento_excepcional_estado",
        table_name="conta_multiuser_aumento_excepcional",
    )
    op.drop_index(
        "ix_conta_multiuser_aumento_excepcional_conta_id",
        table_name="conta_multiuser_aumento_excepcional",
    )
    op.drop_table("conta_multiuser_aumento_excepcional")
