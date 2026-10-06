"""evento de entrada do canal: recebimento idempotente

Revision ID: o6p7q8r9s0t1
Revises: n5o6p7q8r9s0
Create Date: 2026-10-01

Só a tabela do evento já normalizado. Não altera User, onboarding nem
identidade externa. Não há coluna de payload, header ou segredo.

Manter os predicados iguais aos de EventoCanalRecebido.
"""
from alembic import op
import sqlalchemy as sa


revision = "o6p7q8r9s0t1"
down_revision = "n5o6p7q8r9s0"
branch_labels = None
depends_on = None

_SQL_PROVIDER = "provider = 'meta_whatsapp'"
_SQL_TIPO = (
    "tipo_evento IN ('mensagem_textual', 'midia', 'status_entrega', 'desconhecido')"
)
_SQL_STATUS = "status_processamento IN ('recebido')"
_SQL_DIAGNOSTICO = (
    "diagnostico_seguro IN ("
    "'mensagem_textual', 'midia', 'midia:audio', 'midia:document', 'midia:image', "
    "'midia:sticker', 'midia:video', 'status_entrega', 'status_entrega:delivered', "
    "'status_entrega:failed', 'status_entrega:played', 'status_entrega:read', "
    "'status_entrega:sent', 'desconhecido'"
    ")"
)
_SQL_EVENTO = "length(evento_externo_id) > 0"
_SQL_OPCIONAIS = (
    "(sujeito_externo IS NULL OR length(sujeito_externo) > 0)"
    " AND (contexto_destino IS NULL OR length(contexto_destino) > 0)"
    " AND (correlation_id IS NULL OR length(correlation_id) > 0)"
)


def upgrade():
    op.create_table(
        "evento_canal_recebido",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("evento_externo_id", sa.String(length=200), nullable=False),
        sa.Column("tipo_evento", sa.String(length=32), nullable=False),
        sa.Column("sujeito_externo", sa.String(length=32), nullable=True),
        sa.Column("contexto_destino", sa.String(length=32), nullable=True),
        sa.Column("recebido_em", sa.DateTime(), nullable=False),
        sa.Column("status_processamento", sa.String(length=32), nullable=False),
        sa.Column("correlation_id", sa.String(length=32), nullable=True),
        sa.Column("diagnostico_seguro", sa.String(length=40), nullable=False),
        sa.CheckConstraint(_SQL_PROVIDER, name="ck_evento_canal_recebido_provider"),
        sa.CheckConstraint(_SQL_TIPO, name="ck_evento_canal_recebido_tipo"),
        sa.CheckConstraint(_SQL_STATUS, name="ck_evento_canal_recebido_status"),
        sa.CheckConstraint(_SQL_DIAGNOSTICO, name="ck_evento_canal_recebido_diagnostico"),
        sa.CheckConstraint(_SQL_EVENTO, name="ck_evento_canal_recebido_evento"),
        sa.CheckConstraint(_SQL_OPCIONAIS, name="ck_evento_canal_recebido_opcionais"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider",
            "evento_externo_id",
            name="uq_evento_canal_recebido_provider_evento",
        ),
    )
    op.create_index(
        "ix_evento_canal_recebido_recebido_em",
        "evento_canal_recebido",
        ["recebido_em"],
        unique=False,
    )
    op.create_index(
        "ix_evento_canal_recebido_status_processamento",
        "evento_canal_recebido",
        ["status_processamento"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        "ix_evento_canal_recebido_status_processamento",
        table_name="evento_canal_recebido",
    )
    op.drop_index(
        "ix_evento_canal_recebido_recebido_em",
        table_name="evento_canal_recebido",
    )
    op.drop_table("evento_canal_recebido")
