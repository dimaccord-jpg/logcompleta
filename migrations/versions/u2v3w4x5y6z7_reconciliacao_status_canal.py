"""estado de entrega e tratamento único do status de canal

Revision ID: u2v3w4x5y6z7
Revises: t1u2v3w4x5y6
Create Date: 2026-10-02

Separa a confirmação sent/delivered/read da aceitação HTTP da saída.
Um evento de entrada status_entrega gera no máximo uma aplicação.
Não altera User, onboarding nem a chamada já gravada.

Manter os predicados iguais aos de EstadoEntregaCanalSaida e
AplicacaoStatusCanal.
"""
from alembic import op
import sqlalchemy as sa


revision = "u2v3w4x5y6z7"
down_revision = "t1u2v3w4x5y6"
branch_labels = None
depends_on = None

_SQL_ESTADO_VERSAO = "versao >= 0"
_SQL_ESTADO_STATUS = (
    "status_entrega IS NULL OR status_entrega IN ("
    "'sent', 'delivered', 'read', 'failed')"
)
_SQL_ESTADO_FALHA = (
    "codigo_falha_entrega IS NULL OR codigo_falha_entrega = 'falha_entrega'"
)
_SQL_ESTADO_CLASSIFICACAO = (
    "classificacao_resultado IS NULL "
    "OR classificacao_resultado = 'resultado_incerto'"
)
_SQL_ESTADO_FORMA = (
    "("
    "(classificacao_resultado = 'resultado_incerto' "
    "AND status_entrega IS NULL AND provider_status_em IS NULL "
    "AND sent_em IS NULL AND delivered_em IS NULL AND read_em IS NULL "
    "AND failed_em IS NULL AND codigo_falha_entrega IS NULL)"
    " OR (classificacao_resultado IS NULL AND provider_status_em IS NOT NULL "
    "AND ("
    "(status_entrega = 'sent' AND sent_em IS NOT NULL "
    "AND delivered_em IS NULL AND read_em IS NULL "
    "AND failed_em IS NULL AND codigo_falha_entrega IS NULL)"
    " OR (status_entrega = 'delivered' AND delivered_em IS NOT NULL "
    "AND read_em IS NULL AND ("
    "(failed_em IS NULL AND codigo_falha_entrega IS NULL)"
    " OR (failed_em IS NOT NULL AND codigo_falha_entrega = 'falha_entrega')"
    "))"
    " OR (status_entrega = 'read' AND read_em IS NOT NULL AND ("
    "(failed_em IS NULL AND codigo_falha_entrega IS NULL)"
    " OR (failed_em IS NOT NULL AND codigo_falha_entrega = 'falha_entrega')"
    "))"
    " OR (status_entrega = 'failed' AND failed_em IS NOT NULL "
    "AND codigo_falha_entrega = 'falha_entrega' "
    "AND delivered_em IS NULL AND read_em IS NULL)"
    "))"
    ")"
)
_SQL_APLICACAO_RESULTADO = (
    "resultado IN ("
    "'aplicado', 'sem_efeito', 'registrado_sem_avanco', 'sem_saida', "
    "'status_ignorado', 'saida_ambigua', 'ilegivel')"
)
_SQL_APLICACAO_CORRELATION = (
    "correlation_id IS NULL OR "
    "(length(correlation_id) > 0 AND length(correlation_id) <= 32)"
)
_SQL_APLICACAO_COERENCIA = (
    "("
    "(resultado IN ('aplicado', 'sem_efeito', 'registrado_sem_avanco') "
    "AND saida_id IS NOT NULL AND provider_status_em IS NOT NULL)"
    " OR (resultado IN ('sem_saida', 'status_ignorado', 'saida_ambigua') "
    "AND saida_id IS NULL AND provider_status_em IS NOT NULL)"
    " OR (resultado = 'ilegivel' AND saida_id IS NULL "
    "AND provider_status_em IS NULL)"
    ")"
)


def upgrade():
    op.create_index(
        "ix_evento_canal_saida_provider_message_id",
        "evento_canal_saida",
        ["provider_message_id"],
        unique=False,
    )
    op.create_table(
        "estado_entrega_canal_saida",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("saida_id", sa.Integer(), nullable=False),
        sa.Column("versao", sa.Integer(), nullable=False),
        sa.Column("status_entrega", sa.String(length=16), nullable=True),
        sa.Column("provider_status_em", sa.DateTime(), nullable=True),
        sa.Column("sent_em", sa.DateTime(), nullable=True),
        sa.Column("delivered_em", sa.DateTime(), nullable=True),
        sa.Column("read_em", sa.DateTime(), nullable=True),
        sa.Column("failed_em", sa.DateTime(), nullable=True),
        sa.Column("codigo_falha_entrega", sa.String(length=32), nullable=True),
        sa.Column("classificacao_resultado", sa.String(length=32), nullable=True),
        sa.CheckConstraint(_SQL_ESTADO_VERSAO, name="ck_estado_entrega_canal_versao"),
        sa.CheckConstraint(_SQL_ESTADO_STATUS, name="ck_estado_entrega_canal_status"),
        sa.CheckConstraint(_SQL_ESTADO_FALHA, name="ck_estado_entrega_canal_falha"),
        sa.CheckConstraint(
            _SQL_ESTADO_CLASSIFICACAO,
            name="ck_estado_entrega_canal_classificacao",
        ),
        sa.CheckConstraint(_SQL_ESTADO_FORMA, name="ck_estado_entrega_canal_forma"),
        sa.ForeignKeyConstraint(
            ["saida_id"],
            ["evento_canal_saida.id"],
            name="fk_estado_entrega_canal_saida",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("saida_id", name="uq_estado_entrega_canal_saida"),
    )
    op.create_table(
        "aplicacao_status_canal",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("evento_recebido_id", sa.Integer(), nullable=False),
        sa.Column("saida_id", sa.Integer(), nullable=True),
        sa.Column("resultado", sa.String(length=32), nullable=False),
        sa.Column("provider_status_em", sa.DateTime(), nullable=True),
        sa.Column("correlation_id", sa.String(length=32), nullable=True),
        sa.CheckConstraint(
            _SQL_APLICACAO_RESULTADO,
            name="ck_aplicacao_status_canal_resultado",
        ),
        sa.CheckConstraint(
            _SQL_APLICACAO_CORRELATION,
            name="ck_aplicacao_status_canal_correlation",
        ),
        sa.CheckConstraint(
            _SQL_APLICACAO_COERENCIA,
            name="ck_aplicacao_status_canal_coerencia",
        ),
        sa.ForeignKeyConstraint(
            ["evento_recebido_id"],
            ["evento_canal_recebido.id"],
            name="fk_aplicacao_status_canal_evento",
        ),
        sa.ForeignKeyConstraint(
            ["saida_id"],
            ["evento_canal_saida.id"],
            name="fk_aplicacao_status_canal_saida",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "evento_recebido_id",
            name="uq_aplicacao_status_canal_evento",
        ),
    )


def downgrade():
    op.drop_table("aplicacao_status_canal")
    op.drop_table("estado_entrega_canal_saida")
    op.drop_index(
        "ix_evento_canal_saida_provider_message_id",
        table_name="evento_canal_saida",
    )
