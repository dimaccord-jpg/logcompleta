"""entrega web para o WhatsApp vinculado do próprio usuário

Revision ID: f4g5h6i7j8k9
Revises: e3f4g5h6i7j8
Create Date: 2026-10-05

Cria a solicitação de entrega e permite EventoCanalSaida sem evento de
entrada, desde que a origem seja exatamente essa solicitação. As chaves
antigas e as saídas já gravadas permanecem válidas. Não reescreve linhas.

Downgrade bloqueia se já existir saída web.
"""
from alembic import op
import sqlalchemy as sa


revision = "f4g5h6i7j8k9"
down_revision = "e3f4g5h6i7j8"
branch_labels = None
depends_on = None

_SQL_PROVIDER = "provider = 'meta_whatsapp'"
_SQL_SUPERFICIE = "superficie_origem IN ('julia', 'auditoria_frete', 'comparacao_tabelas')"
_SQL_CORRELATION = "length(correlation_id) BETWEEN 32 AND 64"
_SQL_CHAVE_SOLICITACAO = "length(chave_idempotencia) BETWEEN 32 AND 80"
_SQL_HASH = "length(content_hash) = 64"

_SQL_ORIGEM_VIGENTE = (
    "((evento_entrada_id IS NOT NULL AND solicitacao_entrega_id IS NULL AND ("
    "(interpretacao_id IS NOT NULL AND execucao_operacional_id IS NULL AND "
    "(length(chave_idempotencia) < 39 OR substr(chave_idempotencia, length(chave_idempotencia) - 38, 21) "
    "<> ':orientacao_franquia:')) OR (interpretacao_id IS NULL AND execucao_operacional_id IS NOT NULL AND "
    "(length(chave_idempotencia) < 39 OR substr(chave_idempotencia, length(chave_idempotencia) - 38, 21) "
    "<> ':orientacao_franquia:')) OR (interpretacao_id IS NULL AND execucao_operacional_id IS NULL AND "
    "chave_idempotencia = ('meta_whatsapp:' || CAST(evento_entrada_id AS TEXT) || "
    "':orientacao_franquia:resposta_principal')))) OR (evento_entrada_id IS NULL AND "
    "solicitacao_entrega_id IS NOT NULL AND interpretacao_id IS NULL AND execucao_operacional_id IS NULL "
    "AND conclusao_id IS NULL AND substr(chave_idempotencia, 1, length(('meta_whatsapp:web:' || "
    "CAST(solicitacao_entrega_id AS TEXT) || ':parte:'))) = ('meta_whatsapp:web:' || "
    "CAST(solicitacao_entrega_id AS TEXT) || ':parte:') AND substr(chave_idempotencia, "
    "length(chave_idempotencia) - 18, 1) = ':' AND substr(chave_idempotencia, "
    "length(chave_idempotencia) - 17, 18) = 'resposta_principal'))"
)
_SQL_CHAVE_VIGENTE = (
    "(length(chave_idempotencia) BETWEEN 34 AND 80 AND substr(chave_idempotencia, 1, 14) = 'meta_whatsapp:' "
    "AND substr(chave_idempotencia, length(chave_idempotencia) - 17, 18) = 'resposta_principal' "
    "AND substr(chave_idempotencia, length(chave_idempotencia) - 18, 1) = ':') AND "
    "((substr(chave_idempotencia, 1, 18) <> 'meta_whatsapp:web:' OR (solicitacao_entrega_id IS NOT NULL "
    "AND substr(chave_idempotencia, 1, length(('meta_whatsapp:web:' || CAST(solicitacao_entrega_id AS TEXT) "
    "|| ':parte:'))) = ('meta_whatsapp:web:' || CAST(solicitacao_entrega_id AS TEXT) || ':parte:') "
    "AND substr(chave_idempotencia, length(chave_idempotencia) - 18, 1) = ':' "
    "AND substr(chave_idempotencia, length(chave_idempotencia) - 17, 18) = 'resposta_principal')))"
)
_SQL_XOR_ORIGEM = (
    "(evento_entrada_id IS NOT NULL AND solicitacao_entrega_id IS NULL)"
    " OR (evento_entrada_id IS NULL AND solicitacao_entrega_id IS NOT NULL)"
)
_SQL_ORIGEM_ANTERIOR = (
    "(interpretacao_id IS NOT NULL AND execucao_operacional_id IS NULL AND "
    "(length(chave_idempotencia) < 39 OR substr(chave_idempotencia, length(chave_idempotencia) - 38, 21) "
    "<> ':orientacao_franquia:')) OR (interpretacao_id IS NULL AND execucao_operacional_id IS NOT NULL AND "
    "(length(chave_idempotencia) < 39 OR substr(chave_idempotencia, length(chave_idempotencia) - 38, 21) "
    "<> ':orientacao_franquia:')) OR (interpretacao_id IS NULL AND execucao_operacional_id IS NULL AND "
    "chave_idempotencia = ('meta_whatsapp:' || CAST(evento_entrada_id AS TEXT) || "
    "':orientacao_franquia:resposta_principal'))"
)
_SQL_CHAVE_ANTERIOR = (
    "length(chave_idempotencia) BETWEEN 34 AND 80 AND substr(chave_idempotencia, 1, 14) = 'meta_whatsapp:' "
    "AND substr(chave_idempotencia, length(chave_idempotencia) - 17, 18) = 'resposta_principal' "
    "AND substr(chave_idempotencia, length(chave_idempotencia) - 18, 1) = ':'"
)


def upgrade():
    op.create_table(
        "solicitacao_entrega_canal",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("superficie_origem", sa.String(length=40), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("chave_idempotencia", sa.String(length=80), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("criada_em", sa.DateTime(), nullable=False),
        sa.CheckConstraint(_SQL_PROVIDER, name="ck_solicitacao_entrega_canal_provider"),
        sa.CheckConstraint(_SQL_SUPERFICIE, name="ck_solicitacao_entrega_canal_superficie"),
        sa.CheckConstraint(_SQL_CORRELATION, name="ck_solicitacao_entrega_canal_correlation"),
        sa.CheckConstraint(_SQL_CHAVE_SOLICITACAO, name="ck_solicitacao_entrega_canal_chave"),
        sa.CheckConstraint(_SQL_HASH, name="ck_solicitacao_entrega_canal_hash"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chave_idempotencia", name="uq_solicitacao_entrega_canal_chave"),
    )
    op.create_index(
        "ix_solicitacao_entrega_canal_user_id",
        "solicitacao_entrega_canal",
        ["user_id"],
    )
    with op.batch_alter_table("evento_canal_saida", schema=None) as batch_op:
        batch_op.add_column(sa.Column("solicitacao_entrega_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_evento_canal_saida_solicitacao",
            "solicitacao_entrega_canal",
            ["solicitacao_entrega_id"],
            ["id"],
        )
        batch_op.create_index(
            "ix_evento_canal_saida_solicitacao_entrega_id",
            ["solicitacao_entrega_id"],
        )
        batch_op.drop_constraint("ck_evento_canal_saida_origem", type_="check")
        batch_op.create_check_constraint("ck_evento_canal_saida_origem", _SQL_ORIGEM_VIGENTE)
        batch_op.drop_constraint("ck_evento_canal_saida_chave", type_="check")
        batch_op.create_check_constraint("ck_evento_canal_saida_chave", _SQL_CHAVE_VIGENTE)
        batch_op.create_check_constraint("ck_evento_canal_saida_xor_origem", _SQL_XOR_ORIGEM)
        batch_op.alter_column(
            "evento_entrada_id",
            existing_type=sa.Integer(),
            nullable=True,
            existing_nullable=False,
        )


def downgrade():
    bind = op.get_bind()
    web = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM evento_canal_saida "
            "WHERE solicitacao_entrega_id IS NOT NULL OR evento_entrada_id IS NULL"
        )
    ).scalar()
    if int(web or 0) > 0:
        raise RuntimeError(
            "downgrade de f4g5h6i7j8k9 bloqueado: existem saídas web. "
            "Remova essas linhas antes de restaurar evento_entrada_id obrigatório."
        )
    with op.batch_alter_table("evento_canal_saida", schema=None) as batch_op:
        batch_op.drop_constraint("ck_evento_canal_saida_xor_origem", type_="check")
        batch_op.drop_constraint("ck_evento_canal_saida_origem", type_="check")
        batch_op.create_check_constraint("ck_evento_canal_saida_origem", _SQL_ORIGEM_ANTERIOR)
        batch_op.drop_constraint("ck_evento_canal_saida_chave", type_="check")
        batch_op.create_check_constraint("ck_evento_canal_saida_chave", _SQL_CHAVE_ANTERIOR)
        batch_op.drop_index("ix_evento_canal_saida_solicitacao_entrega_id")
        batch_op.drop_constraint("fk_evento_canal_saida_solicitacao", type_="foreignkey")
        batch_op.drop_column("solicitacao_entrega_id")
        batch_op.alter_column(
            "evento_entrada_id",
            existing_type=sa.Integer(),
            nullable=False,
            existing_nullable=True,
        )
    op.drop_index("ix_solicitacao_entrega_canal_user_id", table_name="solicitacao_entrega_canal")
    op.drop_table("solicitacao_entrega_canal")
