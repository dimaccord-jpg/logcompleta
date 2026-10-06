"""respostas compartilháveis e rascunho de entrega WhatsApp

Revision ID: g5h6i7j8k9l0
Revises: f4g5h6i7j8k9
Create Date: 2026-10-06

Registra o texto emitido pelo backend e a pendência de envio a terceiro.
Não altera solicitação de entrega, onboarding nem interpretação conversacional.
"""
from alembic import op
import sqlalchemy as sa


revision = "g5h6i7j8k9l0"
down_revision = "f4g5h6i7j8k9"
branch_labels = None
depends_on = None

_SQL_SUPERFICIE = "superficie IN ('julia', 'auditoria_frete', 'comparacao_tabelas')"
_SQL_REFERENCIA = "length(referencia) BETWEEN 34 AND 80"
_SQL_HASH = "length(content_hash) = 64"
_SQL_TEXTO = "length(texto) BETWEEN 1 AND 32000"
_SQL_CONTEXTO = "length(contexto_conversa) BETWEEN 1 AND 160"
_SQL_ESTADO = (
    "estado IN ("
    "'aguardando_telefone', 'aguardando_confirmacao', 'bloqueado_elegibilidade', "
    "'em_execucao', 'concluido', 'parcial', 'falhou', 'resultado_incerto', "
    "'cancelado', 'expirado'"
    ")"
)
_SQL_VERSAO = "versao >= 1"
_SQL_TELEFONE = "telefone_e164 IS NULL OR (length(telefone_e164) BETWEEN 9 AND 16)"
_SQL_CONFIRMACAO = (
    "(confirmacao_referencia IS NULL AND confirmacao_versao IS NULL)"
    " OR (confirmacao_referencia IS NOT NULL AND confirmacao_versao IS NOT NULL"
    " AND confirmacao_versao >= 1)"
)


def upgrade():
    op.create_table(
        "resposta_compartilhavel_canal",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("referencia", sa.String(length=80), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("superficie", sa.String(length=40), nullable=False),
        sa.Column("contexto_conversa", sa.String(length=160), nullable=False),
        sa.Column("comparison_id", sa.String(length=80), nullable=True),
        sa.Column("escopo_auditoria", sa.String(length=160), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("texto", sa.Text(), nullable=False),
        sa.Column("valida", sa.Boolean(), nullable=False),
        sa.Column("criada_em", sa.DateTime(), nullable=False),
        sa.Column("expira_em", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("referencia", name="uq_resposta_compartilhavel_canal_referencia"),
        sa.CheckConstraint(_SQL_SUPERFICIE, name="ck_resposta_compartilhavel_superficie"),
        sa.CheckConstraint(_SQL_REFERENCIA, name="ck_resposta_compartilhavel_referencia"),
        sa.CheckConstraint(_SQL_HASH, name="ck_resposta_compartilhavel_hash"),
        sa.CheckConstraint(_SQL_TEXTO, name="ck_resposta_compartilhavel_texto"),
        sa.CheckConstraint(_SQL_CONTEXTO, name="ck_resposta_compartilhavel_contexto"),
    )
    op.create_index(
        "ix_resposta_compartilhavel_canal_user_id",
        "resposta_compartilhavel_canal",
        ["user_id"],
    )
    op.create_table(
        "rascunho_entrega_whatsapp",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("referencia", sa.String(length=80), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("superficie", sa.String(length=40), nullable=False),
        sa.Column("contexto_conversa", sa.String(length=160), nullable=False),
        sa.Column("conteudo_referencia", sa.String(length=80), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("nome_destinatario", sa.String(length=80), nullable=True),
        sa.Column("telefone_e164", sa.String(length=16), nullable=True),
        sa.Column("telefone_hash", sa.String(length=64), nullable=True),
        sa.Column("telefone_exibicao", sa.String(length=32), nullable=True),
        sa.Column("turno_telefone_ref", sa.String(length=40), nullable=True),
        sa.Column("estado", sa.String(length=32), nullable=False),
        sa.Column("versao", sa.Integer(), nullable=False),
        sa.Column("confirmacao_referencia", sa.String(length=80), nullable=True),
        sa.Column("confirmacao_apresentada_em", sa.DateTime(), nullable=True),
        sa.Column("confirmacao_versao", sa.Integer(), nullable=True),
        sa.Column("chave_preparo", sa.String(length=64), nullable=True),
        sa.Column("chave_execucao", sa.String(length=64), nullable=True),
        sa.Column("solicitacao_entrega_id", sa.Integer(), nullable=True),
        sa.Column("criada_em", sa.DateTime(), nullable=False),
        sa.Column("atualizada_em", sa.DateTime(), nullable=False),
        sa.Column("expira_em", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(
            ["solicitacao_entrega_id"],
            ["solicitacao_entrega_canal.id"],
            name="fk_rascunho_entrega_whatsapp_solicitacao",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("referencia", name="uq_rascunho_entrega_whatsapp_referencia"),
        sa.UniqueConstraint("chave_preparo", name="uq_rascunho_entrega_whatsapp_preparo"),
        sa.UniqueConstraint("chave_execucao", name="uq_rascunho_entrega_whatsapp_execucao"),
        sa.CheckConstraint(_SQL_SUPERFICIE, name="ck_rascunho_entrega_whatsapp_superficie"),
        sa.CheckConstraint(_SQL_ESTADO, name="ck_rascunho_entrega_whatsapp_estado"),
        sa.CheckConstraint(_SQL_REFERENCIA, name="ck_rascunho_entrega_whatsapp_referencia"),
        sa.CheckConstraint(_SQL_HASH, name="ck_rascunho_entrega_whatsapp_hash"),
        sa.CheckConstraint(_SQL_VERSAO, name="ck_rascunho_entrega_whatsapp_versao"),
        sa.CheckConstraint(_SQL_CONTEXTO, name="ck_rascunho_entrega_whatsapp_contexto"),
        sa.CheckConstraint(_SQL_TELEFONE, name="ck_rascunho_entrega_whatsapp_telefone"),
        sa.CheckConstraint(_SQL_CONFIRMACAO, name="ck_rascunho_entrega_whatsapp_confirmacao"),
    )
    op.create_index(
        "ix_rascunho_entrega_whatsapp_user_id",
        "rascunho_entrega_whatsapp",
        ["user_id"],
    )


def downgrade():
    op.drop_index("ix_rascunho_entrega_whatsapp_user_id", table_name="rascunho_entrega_whatsapp")
    op.drop_table("rascunho_entrega_whatsapp")
    op.drop_index(
        "ix_resposta_compartilhavel_canal_user_id",
        table_name="resposta_compartilhavel_canal",
    )
    op.drop_table("resposta_compartilhavel_canal")
