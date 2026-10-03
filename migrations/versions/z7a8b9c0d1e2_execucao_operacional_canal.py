"""execução operacional do canal WhatsApp

Revision ID: z7a8b9c0d1e2
Revises: y6z7a8b9c0d1
Create Date: 2026-10-03

Uma linha por evento interno para o resultado do chat operacional já
existente. A saída continua em evento_canal_saida e passa a apontar ou
para a interpretação de onboarding ou para esta execução.

Manter os predicados iguais aos dos models.
"""
from alembic import op
import sqlalchemy as sa


revision = "z7a8b9c0d1e2"
down_revision = "y6z7a8b9c0d1"
branch_labels = None
depends_on = None

_SQL_ESTADO = (
    "estado IN ('reservada', 'chamada_iniciada', 'concluida', 'falha')"
)
_SQL_TEXTO = (
    "texto_resposta IS NULL OR "
    "(length(texto_resposta) > 0 AND length(texto_resposta) <= 4096)"
)
_SQL_EXECUTION = "execution_id IS NULL OR length(execution_id) = 36"
_SQL_CORRELATION = (
    "correlation_id IS NULL OR "
    "(length(correlation_id) > 0 AND length(correlation_id) <= 64)"
)
_SQL_UTIL = "conclusao_util IN (0, 1)"
_SQL_COERENCIA = (
    "("
    "(estado = 'reservada' AND codigo = 'em_tratamento' "
    "AND texto_resposta IS NULL AND conclusao_util = 0 "
    "AND user_id IS NOT NULL AND execution_id IS NOT NULL)"
    " OR (estado = 'chamada_iniciada' AND codigo = 'em_tratamento' "
    "AND texto_resposta IS NULL AND conclusao_util = 0 "
    "AND user_id IS NOT NULL AND execution_id IS NOT NULL)"
    " OR (estado = 'concluida' AND codigo = 'resposta_operacional' "
    "AND texto_resposta IS NOT NULL AND conclusao_util = 1 "
    "AND user_id IS NOT NULL AND execution_id IS NOT NULL)"
    " OR (estado = 'falha' AND conclusao_util = 0 AND texto_resposta IS NULL "
    "AND codigo IN ("
    "'governanca_negada', 'identidade_invalida', 'contexto_indisponivel', "
    "'mensagem_invalida', 'falha_provedor', 'provedor_indisponivel', "
    "'resposta_inutilizavel', 'resultado_incerto', 'erro_tecnico'"
    "))"
    ")"
)
_SQL_ORIGEM = (
    "(interpretacao_id IS NOT NULL AND execucao_operacional_id IS NULL)"
    " OR (interpretacao_id IS NULL AND execucao_operacional_id IS NOT NULL)"
)


def upgrade():
    op.create_table(
        "execucao_operacional_canal",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("evento_id", sa.Integer(), nullable=False),
        sa.Column("identidade_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("codigo", sa.String(length=64), nullable=False),
        sa.Column("estado", sa.String(length=32), nullable=False),
        sa.Column("texto_resposta", sa.String(length=4096), nullable=True),
        sa.Column("execution_id", sa.String(length=36), nullable=True),
        sa.Column("conclusao_util", sa.Integer(), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column("criada_em", sa.DateTime(), nullable=False),
        sa.Column("atualizada_em", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["evento_id"],
            ["evento_canal_recebido.id"],
        ),
        sa.ForeignKeyConstraint(
            ["identidade_id"],
            ["identidade_canal_externa.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("evento_id", name="uq_execucao_operacional_canal_evento"),
        sa.CheckConstraint(_SQL_ESTADO, name="ck_execucao_operacional_canal_estado"),
        sa.CheckConstraint(_SQL_TEXTO, name="ck_execucao_operacional_canal_texto"),
        sa.CheckConstraint(
            _SQL_EXECUTION, name="ck_execucao_operacional_canal_execution"
        ),
        sa.CheckConstraint(
            _SQL_CORRELATION, name="ck_execucao_operacional_canal_correlation"
        ),
        sa.CheckConstraint(_SQL_UTIL, name="ck_execucao_operacional_canal_util"),
        sa.CheckConstraint(
            _SQL_COERENCIA, name="ck_execucao_operacional_canal_coerencia"
        ),
    )
    op.create_index(
        "ix_execucao_operacional_canal_identidade_id",
        "execucao_operacional_canal",
        ["identidade_id"],
    )
    op.create_index(
        "ix_execucao_operacional_canal_user_id",
        "execucao_operacional_canal",
        ["user_id"],
    )
    with op.batch_alter_table("evento_canal_saida", schema=None) as batch_op:
        batch_op.alter_column(
            "interpretacao_id",
            existing_type=sa.Integer(),
            nullable=True,
        )
        batch_op.add_column(
            sa.Column("execucao_operacional_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_evento_canal_saida_execucao",
            "execucao_operacional_canal",
            ["execucao_operacional_id"],
            ["id"],
        )
        batch_op.create_index(
            "ix_evento_canal_saida_execucao_operacional_id",
            ["execucao_operacional_id"],
        )
        batch_op.create_check_constraint(
            "ck_evento_canal_saida_origem",
            _SQL_ORIGEM,
        )


def downgrade():
    with op.batch_alter_table("evento_canal_saida", schema=None) as batch_op:
        batch_op.drop_constraint("ck_evento_canal_saida_origem", type_="check")
        batch_op.drop_index("ix_evento_canal_saida_execucao_operacional_id")
        batch_op.drop_constraint(
            "fk_evento_canal_saida_execucao", type_="foreignkey"
        )
        batch_op.drop_column("execucao_operacional_id")
        batch_op.alter_column(
            "interpretacao_id",
            existing_type=sa.Integer(),
            nullable=False,
        )
    op.drop_index(
        "ix_execucao_operacional_canal_user_id",
        table_name="execucao_operacional_canal",
    )
    op.drop_index(
        "ix_execucao_operacional_canal_identidade_id",
        table_name="execucao_operacional_canal",
    )
    op.drop_table("execucao_operacional_canal")
