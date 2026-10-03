"""consumo idempotente da interação operacional WhatsApp

Revision ID: a8b9c0d1e2f3
Revises: z7a8b9c0d1e2
Create Date: 2026-10-03

Uma execução útil gera no máximo uma linha. Sem texto, telefone, e-mail
ou payload. creditos_apropriados fica nulo até existir taxa comercial.

Manter os predicados iguais aos dos models.
"""
from alembic import op
import sqlalchemy as sa


revision = "a8b9c0d1e2f3"
down_revision = "z7a8b9c0d1e2"
branch_labels = None
depends_on = None

_SQL_ESTADO = "estado IN ('pronta_para_apropriacao', 'falha_tecnica')"
_SQL_MOTIVO = (
    "motivo IN ("
    "'taxa_comercial_whatsapp_pendente', 'falha_registro', 'contexto_indisponivel'"
    ")"
)
_SQL_TIPO = "tipo_consumo = 'whatsapp_operacional'"
_SQL_UNIDADE = "unidade = 'interacao_whatsapp_util'"
_SQL_CANAL = "canal = 'whatsapp'"
_SQL_QUANTIDADE = "quantidade = 1"
_SQL_CHAVE = (
    "chave_idempotente = ("
    "'whatsapp:operacao:' || CAST(execucao_operacional_canal_id AS TEXT)"
    ")"
)
_SQL_CORRELATION = (
    "correlation_id IS NULL OR "
    "(length(correlation_id) > 0 AND length(correlation_id) <= 64)"
)
_SQL_CREDITOS = "creditos_apropriados IS NULL"
_SQL_COERENCIA = (
    "("
    "(estado = 'pronta_para_apropriacao' "
    "AND motivo = 'taxa_comercial_whatsapp_pendente' "
    "AND user_id IS NOT NULL AND conta_id IS NOT NULL AND franquia_id IS NOT NULL)"
    " OR (estado = 'falha_tecnica' AND user_id IS NOT NULL "
    "AND motivo IN ('falha_registro', 'contexto_indisponivel'))"
    ")"
)


def upgrade():
    op.create_table(
        "consumo_interacao_canal",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("execucao_operacional_canal_id", sa.Integer(), nullable=False),
        sa.Column("evento_canal_recebido_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("conta_id", sa.Integer(), nullable=True),
        sa.Column("franquia_id", sa.Integer(), nullable=True),
        sa.Column("tipo_consumo", sa.String(length=40), nullable=False),
        sa.Column("quantidade", sa.Integer(), nullable=False),
        sa.Column("unidade", sa.String(length=40), nullable=False),
        sa.Column("canal", sa.String(length=32), nullable=False),
        sa.Column("chave_idempotente", sa.String(length=80), nullable=False),
        sa.Column("estado", sa.String(length=40), nullable=False),
        sa.Column("motivo", sa.String(length=80), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column("creditos_apropriados", sa.Numeric(18, 6), nullable=True),
        sa.Column("criada_em", sa.DateTime(), nullable=False),
        sa.Column("atualizada_em", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["execucao_operacional_canal_id"],
            ["execucao_operacional_canal.id"],
            name="fk_consumo_interacao_canal_execucao",
        ),
        sa.ForeignKeyConstraint(
            ["evento_canal_recebido_id"],
            ["evento_canal_recebido.id"],
            name="fk_consumo_interacao_canal_evento",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name="fk_consumo_interacao_canal_user",
        ),
        sa.ForeignKeyConstraint(
            ["conta_id"],
            ["conta.id"],
            name="fk_consumo_interacao_canal_conta",
        ),
        sa.ForeignKeyConstraint(
            ["franquia_id"],
            ["franquia.id"],
            name="fk_consumo_interacao_canal_franquia",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "execucao_operacional_canal_id",
            name="uq_consumo_interacao_canal_execucao",
        ),
        sa.UniqueConstraint(
            "chave_idempotente",
            name="uq_consumo_interacao_canal_chave",
        ),
        sa.CheckConstraint(_SQL_ESTADO, name="ck_consumo_interacao_canal_estado"),
        sa.CheckConstraint(_SQL_MOTIVO, name="ck_consumo_interacao_canal_motivo"),
        sa.CheckConstraint(_SQL_TIPO, name="ck_consumo_interacao_canal_tipo"),
        sa.CheckConstraint(_SQL_UNIDADE, name="ck_consumo_interacao_canal_unidade"),
        sa.CheckConstraint(_SQL_CANAL, name="ck_consumo_interacao_canal_canal"),
        sa.CheckConstraint(
            _SQL_QUANTIDADE, name="ck_consumo_interacao_canal_quantidade"
        ),
        sa.CheckConstraint(_SQL_CHAVE, name="ck_consumo_interacao_canal_chave"),
        sa.CheckConstraint(
            _SQL_CORRELATION, name="ck_consumo_interacao_canal_correlation"
        ),
        sa.CheckConstraint(_SQL_CREDITOS, name="ck_consumo_interacao_canal_creditos"),
        sa.CheckConstraint(_SQL_COERENCIA, name="ck_consumo_interacao_canal_coerencia"),
    )
    op.create_index(
        "ix_consumo_interacao_canal_evento_canal_recebido_id",
        "consumo_interacao_canal",
        ["evento_canal_recebido_id"],
    )
    op.create_index(
        "ix_consumo_interacao_canal_user_id",
        "consumo_interacao_canal",
        ["user_id"],
    )
    op.create_index(
        "ix_consumo_interacao_canal_conta_id",
        "consumo_interacao_canal",
        ["conta_id"],
    )
    op.create_index(
        "ix_consumo_interacao_canal_franquia_id",
        "consumo_interacao_canal",
        ["franquia_id"],
    )


def downgrade():
    op.drop_index(
        "ix_consumo_interacao_canal_franquia_id",
        table_name="consumo_interacao_canal",
    )
    op.drop_index(
        "ix_consumo_interacao_canal_conta_id",
        table_name="consumo_interacao_canal",
    )
    op.drop_index(
        "ix_consumo_interacao_canal_user_id",
        table_name="consumo_interacao_canal",
    )
    op.drop_index(
        "ix_consumo_interacao_canal_evento_canal_recebido_id",
        table_name="consumo_interacao_canal",
    )
    op.drop_table("consumo_interacao_canal")
