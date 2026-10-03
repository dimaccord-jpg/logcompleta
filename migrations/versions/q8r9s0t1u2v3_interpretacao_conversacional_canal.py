"""resultado interno da interpretação conversacional do canal

Revision ID: q8r9s0t1u2v3
Revises: p7q8r9s0t1u2
Create Date: 2026-10-01

Uma linha por evento já roteado, só para replay do tratamento guest e
onboarding. Não grava payload do provedor, senha, e-mail nem texto do usuário.

Manter os predicados iguais aos de InterpretacaoConversacionalCanal.
"""
from alembic import op
import sqlalchemy as sa


revision = "q8r9s0t1u2v3"
down_revision = "p7q8r9s0t1u2"
branch_labels = None
depends_on = None

_SQL_CODIGO = "length(codigo) > 0 AND length(codigo) <= 64"
_SQL_ACAO = "length(acao) > 0 AND length(acao) <= 64"
_SQL_ETAPA = (
    "etapa_final IS NULL OR etapa_final IN ("
    "'convite_cadastro', 'coletando_nome', 'coletando_email', 'coletando_cargo', "
    "'coletando_entrevista', 'aguardando_termos', 'aguardando_senha', "
    "'concluido', 'cancelado', 'expirado'"
    ")"
)
_SQL_TEXTO = (
    "texto_resposta IS NULL OR "
    "(length(texto_resposta) > 0 AND length(texto_resposta) <= 1024)"
)


def upgrade():
    op.create_table(
        "interpretacao_conversacional_canal",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("evento_id", sa.Integer(), nullable=False),
        sa.Column("codigo", sa.String(length=64), nullable=False),
        sa.Column("acao", sa.String(length=64), nullable=False),
        sa.Column("onboarding_id", sa.Integer(), nullable=True),
        sa.Column("etapa_final", sa.String(length=40), nullable=True),
        sa.Column("texto_resposta", sa.String(length=1024), nullable=True),
        sa.CheckConstraint(_SQL_CODIGO, name="ck_interpretacao_conversacional_codigo"),
        sa.CheckConstraint(_SQL_ACAO, name="ck_interpretacao_conversacional_acao"),
        sa.CheckConstraint(_SQL_ETAPA, name="ck_interpretacao_conversacional_etapa"),
        sa.CheckConstraint(_SQL_TEXTO, name="ck_interpretacao_conversacional_texto"),
        sa.ForeignKeyConstraint(["evento_id"], ["evento_canal_recebido.id"]),
        sa.ForeignKeyConstraint(["onboarding_id"], ["onboarding_canal.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("evento_id", name="uq_interpretacao_conversacional_evento"),
    )


def downgrade():
    op.drop_table("interpretacao_conversacional_canal")
