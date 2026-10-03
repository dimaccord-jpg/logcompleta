"""saída textual idempotente do canal

Revision ID: s0t1u2v3w4x5
Revises: r9s0t1u2v3w4
Create Date: 2026-10-01

Uma resposta principal por evento de entrada. Não grava access token,
header, destinatário, texto da mensagem nem corpo bruto do provider.

Manter os predicados iguais aos de EventoCanalSaida.
"""
from alembic import op
import sqlalchemy as sa


revision = "s0t1u2v3w4x5"
down_revision = "r9s0t1u2v3w4"
branch_labels = None
depends_on = None

_SQL_PROVIDER = "provider = 'meta_whatsapp'"
_SQL_STATUS = (
    "status_envio IN ("
    "'reservado', 'aceito_provider', 'erro', 'aguardando_link_seguro'"
    ")"
)
# Sem ':nome' no SQL: o compilador trata isso como bind e anula o predicado.
_SQL_CHAVE = (
    "length(chave_idempotencia) BETWEEN 34 AND 80 "
    "AND substr(chave_idempotencia, 1, 14) = 'meta_whatsapp:' "
    "AND substr(chave_idempotencia, length(chave_idempotencia) - 17, 18) "
    "= 'resposta_principal' "
    "AND substr(chave_idempotencia, length(chave_idempotencia) - 18, 1) = ':'"
)
_SQL_ERRO = (
    "codigo_erro IS NULL OR codigo_erro IN ("
    "'timeout', 'http_4xx', 'http_5xx', 'resposta_invalida', "
    "'configuracao_ausente', 'falha_transporte'"
    ")"
)
_SQL_MENSAGEM = (
    "provider_message_id IS NULL OR "
    "(length(provider_message_id) BETWEEN 1 AND 200)"
)
_SQL_CORRELATION = "length(correlation_id) > 0 AND length(correlation_id) <= 64"
_SQL_COERENCIA = (
    "("
    "(status_envio = 'aceito_provider' AND provider_message_id IS NOT NULL "
    "AND codigo_erro IS NULL AND enviado_em IS NOT NULL)"
    " OR (status_envio = 'erro' AND provider_message_id IS NULL "
    "AND codigo_erro IS NOT NULL AND enviado_em IS NULL)"
    " OR (status_envio = 'reservado' AND provider_message_id IS NULL "
    "AND codigo_erro IS NULL AND enviado_em IS NULL)"
    " OR (status_envio = 'aguardando_link_seguro' AND provider_message_id IS NULL "
    "AND codigo_erro IS NULL AND enviado_em IS NULL)"
    ")"
)


def upgrade():
    op.create_table(
        "evento_canal_saida",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("evento_entrada_id", sa.Integer(), nullable=False),
        sa.Column("interpretacao_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("chave_idempotencia", sa.String(length=80), nullable=False),
        sa.Column("status_envio", sa.String(length=32), nullable=False),
        sa.Column("provider_message_id", sa.String(length=200), nullable=True),
        sa.Column("codigo_erro", sa.String(length=32), nullable=True),
        sa.Column("criado_em", sa.DateTime(), nullable=False),
        sa.Column("enviado_em", sa.DateTime(), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.CheckConstraint(_SQL_PROVIDER, name="ck_evento_canal_saida_provider"),
        sa.CheckConstraint(_SQL_STATUS, name="ck_evento_canal_saida_status"),
        sa.CheckConstraint(_SQL_CHAVE, name="ck_evento_canal_saida_chave"),
        sa.CheckConstraint(_SQL_ERRO, name="ck_evento_canal_saida_erro"),
        sa.CheckConstraint(_SQL_MENSAGEM, name="ck_evento_canal_saida_mensagem"),
        sa.CheckConstraint(_SQL_CORRELATION, name="ck_evento_canal_saida_correlation"),
        sa.CheckConstraint(_SQL_COERENCIA, name="ck_evento_canal_saida_coerencia"),
        sa.ForeignKeyConstraint(
            ["evento_entrada_id"],
            ["evento_canal_recebido.id"],
        ),
        sa.ForeignKeyConstraint(
            ["interpretacao_id"],
            ["interpretacao_conversacional_canal.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chave_idempotencia", name="uq_evento_canal_saida_chave"),
    )
    op.create_index(
        "ix_evento_canal_saida_evento_entrada_id",
        "evento_canal_saida",
        ["evento_entrada_id"],
    )
    op.create_index(
        "ix_evento_canal_saida_interpretacao_id",
        "evento_canal_saida",
        ["interpretacao_id"],
    )


def downgrade():
    op.drop_index("ix_evento_canal_saida_interpretacao_id", table_name="evento_canal_saida")
    op.drop_index("ix_evento_canal_saida_evento_entrada_id", table_name="evento_canal_saida")
    op.drop_table("evento_canal_saida")
