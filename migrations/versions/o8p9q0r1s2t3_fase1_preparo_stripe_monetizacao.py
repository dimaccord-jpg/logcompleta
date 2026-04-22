"""fase 1: preparo Stripe em Conta + fatos append-only de monetizacao

Revision ID: o8p9q0r1s2t3
Revises: n7o8p9q0r1s2
Create Date: 2026-04-16
"""
from alembic import op
import sqlalchemy as sa


revision = "o8p9q0r1s2t3"
down_revision = "n7o8p9q0r1s2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "conta_monetizacao_vinculo",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conta_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("customer_id", sa.String(length=160), nullable=True),
        sa.Column("subscription_id", sa.String(length=160), nullable=True),
        sa.Column("price_id", sa.String(length=160), nullable=True),
        sa.Column("plano_interno", sa.String(length=40), nullable=True),
        sa.Column("status_contratual_externo", sa.String(length=60), nullable=True),
        sa.Column("vigencia_externa_inicio", sa.DateTime(), nullable=True),
        sa.Column("vigencia_externa_fim", sa.DateTime(), nullable=True),
        sa.Column("ativo", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("snapshot_normalizado_json", sa.Text(), nullable=True),
        sa.Column("payload_bruto_sanitizado_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("desativado_em", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["conta_id"], ["conta.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_conta_monetizacao_vinculo_conta_id", "conta_monetizacao_vinculo", ["conta_id"], unique=False)
    op.create_index("ix_conta_monetizacao_vinculo_provider", "conta_monetizacao_vinculo", ["provider"], unique=False)
    op.create_index("ix_conta_monetizacao_vinculo_customer_id", "conta_monetizacao_vinculo", ["customer_id"], unique=False)
    op.create_index("ix_conta_monetizacao_vinculo_subscription_id", "conta_monetizacao_vinculo", ["subscription_id"], unique=False)
    op.create_index("ix_conta_monetizacao_vinculo_price_id", "conta_monetizacao_vinculo", ["price_id"], unique=False)
    op.create_index("ix_conta_monetizacao_vinculo_plano_interno", "conta_monetizacao_vinculo", ["plano_interno"], unique=False)
    op.create_index("ix_conta_monetizacao_vinculo_status_contratual_externo", "conta_monetizacao_vinculo", ["status_contratual_externo"], unique=False)
    op.create_index("ix_conta_monetizacao_vinculo_ativo", "conta_monetizacao_vinculo", ["ativo"], unique=False)
    op.create_index("ix_conta_monetizacao_vinculo_created_at", "conta_monetizacao_vinculo", ["created_at"], unique=False)

    op.create_table(
        "monetizacao_fato",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tipo_fato", sa.String(length=80), nullable=False),
        sa.Column("status_tecnico", sa.String(length=40), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("correlation_key", sa.String(length=200), nullable=True),
        sa.Column("timestamp_externo", sa.DateTime(), nullable=True),
        sa.Column("timestamp_interno", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("provider", sa.String(length=40), nullable=True),
        sa.Column("conta_id", sa.Integer(), nullable=True),
        sa.Column("franquia_id", sa.Integer(), nullable=True),
        sa.Column("usuario_id", sa.Integer(), nullable=True),
        sa.Column("external_event_id", sa.String(length=200), nullable=True),
        sa.Column("customer_id", sa.String(length=160), nullable=True),
        sa.Column("subscription_id", sa.String(length=160), nullable=True),
        sa.Column("price_id", sa.String(length=160), nullable=True),
        sa.Column("invoice_id", sa.String(length=160), nullable=True),
        sa.Column("identificadores_externos_json", sa.Text(), nullable=True),
        sa.Column("snapshot_normalizado_json", sa.Text(), nullable=False),
        sa.Column("payload_bruto_sanitizado_json", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["conta_id"], ["conta.id"]),
        sa.ForeignKeyConstraint(["franquia_id"], ["franquia.id"]),
        sa.ForeignKeyConstraint(["usuario_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_monetizacao_fato_tipo_fato", "monetizacao_fato", ["tipo_fato"], unique=False)
    op.create_index("ix_monetizacao_fato_status_tecnico", "monetizacao_fato", ["status_tecnico"], unique=False)
    op.create_index("ix_monetizacao_fato_idempotency_key", "monetizacao_fato", ["idempotency_key"], unique=False)
    op.create_index("ix_monetizacao_fato_correlation_key", "monetizacao_fato", ["correlation_key"], unique=False)
    op.create_index("ix_monetizacao_fato_timestamp_externo", "monetizacao_fato", ["timestamp_externo"], unique=False)
    op.create_index("ix_monetizacao_fato_timestamp_interno", "monetizacao_fato", ["timestamp_interno"], unique=False)
    op.create_index("ix_monetizacao_fato_provider", "monetizacao_fato", ["provider"], unique=False)
    op.create_index("ix_monetizacao_fato_conta_id", "monetizacao_fato", ["conta_id"], unique=False)
    op.create_index("ix_monetizacao_fato_franquia_id", "monetizacao_fato", ["franquia_id"], unique=False)
    op.create_index("ix_monetizacao_fato_usuario_id", "monetizacao_fato", ["usuario_id"], unique=False)
    op.create_index("ix_monetizacao_fato_external_event_id", "monetizacao_fato", ["external_event_id"], unique=False)
    op.create_index("ix_monetizacao_fato_customer_id", "monetizacao_fato", ["customer_id"], unique=False)
    op.create_index("ix_monetizacao_fato_subscription_id", "monetizacao_fato", ["subscription_id"], unique=False)
    op.create_index("ix_monetizacao_fato_price_id", "monetizacao_fato", ["price_id"], unique=False)
    op.create_index("ix_monetizacao_fato_invoice_id", "monetizacao_fato", ["invoice_id"], unique=False)


def downgrade():
    op.drop_index("ix_monetizacao_fato_invoice_id", table_name="monetizacao_fato")
    op.drop_index("ix_monetizacao_fato_price_id", table_name="monetizacao_fato")
    op.drop_index("ix_monetizacao_fato_subscription_id", table_name="monetizacao_fato")
    op.drop_index("ix_monetizacao_fato_customer_id", table_name="monetizacao_fato")
    op.drop_index("ix_monetizacao_fato_external_event_id", table_name="monetizacao_fato")
    op.drop_index("ix_monetizacao_fato_usuario_id", table_name="monetizacao_fato")
    op.drop_index("ix_monetizacao_fato_franquia_id", table_name="monetizacao_fato")
    op.drop_index("ix_monetizacao_fato_conta_id", table_name="monetizacao_fato")
    op.drop_index("ix_monetizacao_fato_provider", table_name="monetizacao_fato")
    op.drop_index("ix_monetizacao_fato_timestamp_interno", table_name="monetizacao_fato")
    op.drop_index("ix_monetizacao_fato_timestamp_externo", table_name="monetizacao_fato")
    op.drop_index("ix_monetizacao_fato_correlation_key", table_name="monetizacao_fato")
    op.drop_index("ix_monetizacao_fato_idempotency_key", table_name="monetizacao_fato")
    op.drop_index("ix_monetizacao_fato_status_tecnico", table_name="monetizacao_fato")
    op.drop_index("ix_monetizacao_fato_tipo_fato", table_name="monetizacao_fato")
    op.drop_table("monetizacao_fato")

    op.drop_index("ix_conta_monetizacao_vinculo_created_at", table_name="conta_monetizacao_vinculo")
    op.drop_index("ix_conta_monetizacao_vinculo_ativo", table_name="conta_monetizacao_vinculo")
    op.drop_index("ix_conta_monetizacao_vinculo_status_contratual_externo", table_name="conta_monetizacao_vinculo")
    op.drop_index("ix_conta_monetizacao_vinculo_plano_interno", table_name="conta_monetizacao_vinculo")
    op.drop_index("ix_conta_monetizacao_vinculo_price_id", table_name="conta_monetizacao_vinculo")
    op.drop_index("ix_conta_monetizacao_vinculo_subscription_id", table_name="conta_monetizacao_vinculo")
    op.drop_index("ix_conta_monetizacao_vinculo_customer_id", table_name="conta_monetizacao_vinculo")
    op.drop_index("ix_conta_monetizacao_vinculo_provider", table_name="conta_monetizacao_vinculo")
    op.drop_index("ix_conta_monetizacao_vinculo_conta_id", table_name="conta_monetizacao_vinculo")
    op.drop_table("conta_monetizacao_vinculo")
