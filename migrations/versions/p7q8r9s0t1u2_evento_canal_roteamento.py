"""roteamento do evento de canal: status e texto mínimo

Revision ID: p7q8r9s0t1u2
Revises: o6p7q8r9s0t1
Create Date: 2026-10-01

Amplia o check de status_processamento e cria a tabela do texto
operacional. Não cria payload bruto, envio, onboarding nem billing.

Manter os predicados iguais aos de EventoCanalRecebido e ConteudoTextualCanal.
"""
from alembic import op
import sqlalchemy as sa


revision = "p7q8r9s0t1u2"
down_revision = "o6p7q8r9s0t1"
branch_labels = None
depends_on = None

_SQL_STATUS = (
    "status_processamento IN ("
    "'recebido', 'processando', 'roteado', 'ignorado', "
    "'erro_seguro', 'aguardando_suporte_midia'"
    ")"
)
_SQL_STATUS_LOTE_3A = "status_processamento IN ('recebido')"
_SQL_TEXTO = "length(texto) > 0 AND length(texto) <= 4096"


def upgrade():
    with op.batch_alter_table("evento_canal_recebido", schema=None) as batch_op:
        batch_op.drop_constraint("ck_evento_canal_recebido_status", type_="check")
        batch_op.create_check_constraint("ck_evento_canal_recebido_status", _SQL_STATUS)
    op.create_table(
        "conteudo_textual_canal",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("evento_id", sa.Integer(), nullable=False),
        sa.Column("texto", sa.String(length=4096), nullable=False),
        sa.CheckConstraint(_SQL_TEXTO, name="ck_conteudo_textual_canal_texto"),
        sa.ForeignKeyConstraint(["evento_id"], ["evento_canal_recebido.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("evento_id", name="uq_conteudo_textual_canal_evento"),
    )


def downgrade():
    op.drop_table("conteudo_textual_canal")
    op.execute(
        "DELETE FROM evento_canal_recebido "
        "WHERE status_processamento != 'recebido'"
    )
    with op.batch_alter_table("evento_canal_recebido", schema=None) as batch_op:
        batch_op.drop_constraint("ck_evento_canal_recebido_status", type_="check")
        batch_op.create_check_constraint(
            "ck_evento_canal_recebido_status",
            _SQL_STATUS_LOTE_3A,
        )
