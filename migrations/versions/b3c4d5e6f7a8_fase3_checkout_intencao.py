"""fase 3 correcao: intencao exclusiva de checkout multiuser

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-09-09

Justificativa:
MonetizacaoFato é append-only e não admite unique parcial de 'uma pendente
por Conta' sem virar state machine. ContaMonetizacaoVinculo é o contrato
comercial confirmado e não pode representar Checkout não pago.

Esta tabela guarda somente correlação técnica, Session ID, Price, quantity
solicitada e estado pendente/consumida/expirada. Sem PII e sem dados de pagamento.
"""
from alembic import op
import sqlalchemy as sa


revision = "b3c4d5e6f7a8"
down_revision = "a2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "conta_monetizacao_checkout_intencao",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conta_id", sa.Integer(), nullable=False),
        sa.Column("franquia_id", sa.Integer(), nullable=True),
        sa.Column("usuario_id", sa.Integer(), nullable=True),
        sa.Column("plano_interno", sa.String(length=40), nullable=False),
        sa.Column("estado", sa.String(length=20), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("stripe_idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("checkout_session_id", sa.String(length=200), nullable=True),
        sa.Column("checkout_status", sa.String(length=40), nullable=True),
        sa.Column("checkout_expires_at", sa.DateTime(), nullable=True),
        sa.Column("price_id", sa.String(length=160), nullable=False),
        sa.Column("quantity_solicitada", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.String(length=160), nullable=True),
        sa.Column("subscription_id", sa.String(length=160), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("consumida_em", sa.DateTime(), nullable=True),
        sa.Column("expirada_em", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "estado IN ('pendente', 'consumida', 'expirada')",
            name="ck_conta_checkout_intencao_estado",
        ),
        sa.ForeignKeyConstraint(["conta_id"], ["conta.id"]),
        sa.ForeignKeyConstraint(["franquia_id"], ["franquia.id"]),
        sa.ForeignKeyConstraint(["usuario_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("correlation_id", name="uq_conta_checkout_intencao_correlation"),
    )
    op.create_index(
        "ix_conta_monetizacao_checkout_intencao_conta_id",
        "conta_monetizacao_checkout_intencao",
        ["conta_id"],
        unique=False,
    )
    op.create_index(
        "ix_conta_monetizacao_checkout_intencao_estado",
        "conta_monetizacao_checkout_intencao",
        ["estado"],
        unique=False,
    )
    op.create_index(
        "ix_conta_monetizacao_checkout_intencao_checkout_session_id",
        "conta_monetizacao_checkout_intencao",
        ["checkout_session_id"],
        unique=False,
    )
    op.create_index(
        "uq_conta_checkout_intencao_pendente_multiuser",
        "conta_monetizacao_checkout_intencao",
        ["conta_id"],
        unique=True,
        postgresql_where=sa.text("estado = 'pendente' AND plano_interno = 'multiuser'"),
        sqlite_where=sa.text("estado = 'pendente' AND plano_interno = 'multiuser'"),
    )


def downgrade():
    op.drop_index(
        "uq_conta_checkout_intencao_pendente_multiuser",
        table_name="conta_monetizacao_checkout_intencao",
    )
    op.drop_index(
        "ix_conta_monetizacao_checkout_intencao_checkout_session_id",
        table_name="conta_monetizacao_checkout_intencao",
    )
    op.drop_index(
        "ix_conta_monetizacao_checkout_intencao_estado",
        table_name="conta_monetizacao_checkout_intencao",
    )
    op.drop_index(
        "ix_conta_monetizacao_checkout_intencao_conta_id",
        table_name="conta_monetizacao_checkout_intencao",
    )
    op.drop_table("conta_monetizacao_checkout_intencao")
