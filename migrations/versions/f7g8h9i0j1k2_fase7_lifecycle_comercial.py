"""fase 7: reducao futura, notificacao interna e titularidade

Revision ID: f7g8h9i0j1k2
Revises: e6f7a8b9c0d1
Create Date: 2026-09-14

Lifecycle comercial V1: quantity futura, notificacao interna minima
e solicitacao administrativa de titularidade. Nao altera F1-F6.
"""
from alembic import op
import sqlalchemy as sa


revision = "f7g8h9i0j1k2"
down_revision = "e6f7a8b9c0d1"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "user",
        sa.Column(
            "sessao_contexto_geracao",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )

    op.create_table(
        "conta_multiuser_reducao_quantity",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conta_id", sa.Integer(), nullable=False),
        sa.Column("solicitado_por_user_id", sa.Integer(), nullable=False),
        sa.Column("quantity_atual_no_pedido", sa.Integer(), nullable=False),
        sa.Column("quantity_futura", sa.Integer(), nullable=False),
        sa.Column("solicitado_em", sa.DateTime(), nullable=False),
        sa.Column("efetivar_em", sa.DateTime(), nullable=False),
        sa.Column("estado", sa.String(length=40), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("versao", sa.Integer(), nullable=False),
        sa.Column("stripe_customer_id", sa.String(length=160), nullable=True),
        sa.Column("stripe_subscription_id", sa.String(length=160), nullable=True),
        sa.Column("stripe_subscription_item_id", sa.String(length=160), nullable=True),
        sa.Column("efetivada_em", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint(
            "estado IN ('pendente', 'efetivada', 'bloqueada_no_corte')",
            name="ck_conta_multiuser_reducao_estado",
        ),
        sa.CheckConstraint(
            "quantity_atual_no_pedido >= 1 AND quantity_futura >= 1 "
            "AND quantity_futura < quantity_atual_no_pedido",
            name="ck_conta_multiuser_reducao_qtd",
        ),
        sa.CheckConstraint(
            "versao >= 1",
            name="ck_conta_multiuser_reducao_versao",
        ),
        sa.ForeignKeyConstraint(["conta_id"], ["conta.id"]),
        sa.ForeignKeyConstraint(["solicitado_por_user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "correlation_id",
            name="uq_conta_multiuser_reducao_correlation",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_conta_multiuser_reducao_idempotency",
        ),
    )
    op.create_index(
        "ix_conta_multiuser_reducao_quantity_conta_id",
        "conta_multiuser_reducao_quantity",
        ["conta_id"],
        unique=False,
    )
    op.create_index(
        "ix_conta_multiuser_reducao_quantity_estado",
        "conta_multiuser_reducao_quantity",
        ["estado"],
        unique=False,
    )
    op.create_index(
        "ix_conta_multiuser_reducao_quantity_efetivar_em",
        "conta_multiuser_reducao_quantity",
        ["efetivar_em"],
        unique=False,
    )
    op.create_index(
        "uq_conta_multiuser_reducao_pendente",
        "conta_multiuser_reducao_quantity",
        ["conta_id"],
        unique=True,
        postgresql_where=sa.text("estado = 'pendente'"),
        sqlite_where=sa.text("estado = 'pendente'"),
    )

    op.create_table(
        "notificacao_interna",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("conta_id", sa.Integer(), nullable=True),
        sa.Column("tipo", sa.String(length=60), nullable=False),
        sa.Column("mensagem", sa.String(length=500), nullable=False),
        sa.Column("cta_interno", sa.String(length=80), nullable=True),
        sa.Column("referencia_dominio", sa.String(length=120), nullable=True),
        sa.Column("dedup_key", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("read_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["conta_id"], ["conta.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "dedup_key",
            name="uq_notificacao_interna_user_dedup",
        ),
    )
    op.create_index(
        "ix_notificacao_interna_user_id",
        "notificacao_interna",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_notificacao_interna_tipo",
        "notificacao_interna",
        ["tipo"],
        unique=False,
    )
    op.create_index(
        "ix_notificacao_interna_user_created",
        "notificacao_interna",
        ["user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_notificacao_interna_user_unread",
        "notificacao_interna",
        ["user_id", "read_at"],
        unique=False,
    )

    op.create_table(
        "conta_multiuser_titularidade_solicitacao",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conta_id", sa.Integer(), nullable=False),
        sa.Column("titular_atual_id", sa.Integer(), nullable=False),
        sa.Column("candidato_id", sa.Integer(), nullable=False),
        sa.Column("solicitante_id", sa.Integer(), nullable=False),
        sa.Column("motivo", sa.String(length=500), nullable=False),
        sa.Column("estado", sa.String(length=20), nullable=False),
        sa.Column("administrador_id", sa.Integer(), nullable=True),
        sa.Column("decidido_em", sa.DateTime(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("decisao_idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("versao", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint(
            "estado IN ('solicitada', 'aprovada', 'rejeitada')",
            name="ck_conta_multiuser_titularidade_estado",
        ),
        sa.CheckConstraint(
            "titular_atual_id != candidato_id",
            name="ck_conta_multiuser_titularidade_distintos",
        ),
        sa.CheckConstraint(
            "versao >= 1",
            name="ck_conta_multiuser_titularidade_versao",
        ),
        sa.ForeignKeyConstraint(["conta_id"], ["conta.id"]),
        sa.ForeignKeyConstraint(["titular_atual_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["candidato_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["solicitante_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["administrador_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "correlation_id",
            name="uq_conta_multiuser_titularidade_correlation",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_conta_multiuser_titularidade_idempotency",
        ),
    )
    op.create_index(
        "ix_conta_multiuser_titularidade_conta_id",
        "conta_multiuser_titularidade_solicitacao",
        ["conta_id"],
        unique=False,
    )
    op.create_index(
        "ix_conta_multiuser_titularidade_estado",
        "conta_multiuser_titularidade_solicitacao",
        ["estado"],
        unique=False,
    )
    op.create_index(
        "uq_conta_multiuser_titularidade_solicitada",
        "conta_multiuser_titularidade_solicitacao",
        ["conta_id"],
        unique=True,
        postgresql_where=sa.text("estado = 'solicitada'"),
        sqlite_where=sa.text("estado = 'solicitada'"),
    )
    op.create_index(
        "uq_conta_multiuser_titularidade_decisao_idem",
        "conta_multiuser_titularidade_solicitacao",
        ["decisao_idempotency_key"],
        unique=True,
        postgresql_where=sa.text("decisao_idempotency_key IS NOT NULL"),
        sqlite_where=sa.text("decisao_idempotency_key IS NOT NULL"),
    )


def downgrade():
    op.drop_index(
        "uq_conta_multiuser_titularidade_decisao_idem",
        table_name="conta_multiuser_titularidade_solicitacao",
    )
    op.drop_index(
        "uq_conta_multiuser_titularidade_solicitada",
        table_name="conta_multiuser_titularidade_solicitacao",
    )
    op.drop_index(
        "ix_conta_multiuser_titularidade_estado",
        table_name="conta_multiuser_titularidade_solicitacao",
    )
    op.drop_index(
        "ix_conta_multiuser_titularidade_conta_id",
        table_name="conta_multiuser_titularidade_solicitacao",
    )
    op.drop_table("conta_multiuser_titularidade_solicitacao")

    op.drop_index("ix_notificacao_interna_user_unread", table_name="notificacao_interna")
    op.drop_index("ix_notificacao_interna_user_created", table_name="notificacao_interna")
    op.drop_index("ix_notificacao_interna_tipo", table_name="notificacao_interna")
    op.drop_index("ix_notificacao_interna_user_id", table_name="notificacao_interna")
    op.drop_table("notificacao_interna")

    op.drop_index(
        "uq_conta_multiuser_reducao_pendente",
        table_name="conta_multiuser_reducao_quantity",
    )
    op.drop_index(
        "ix_conta_multiuser_reducao_quantity_efetivar_em",
        table_name="conta_multiuser_reducao_quantity",
    )
    op.drop_index(
        "ix_conta_multiuser_reducao_quantity_estado",
        table_name="conta_multiuser_reducao_quantity",
    )
    op.drop_index(
        "ix_conta_multiuser_reducao_quantity_conta_id",
        table_name="conta_multiuser_reducao_quantity",
    )
    op.drop_table("conta_multiuser_reducao_quantity")
    op.drop_column("user", "sessao_contexto_geracao")
