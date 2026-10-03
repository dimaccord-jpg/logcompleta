"""referência da conclusão enviada e estado preparando_link

Revision ID: t1u2v3w4x5y6
Revises: s0t1u2v3w4x5
Create Date: 2026-10-01

A saída passa a apontar a conclusão cujo link foi usado na tentativa.
Não grava segredo, URL, texto da mensagem nem corpo do provider.

preparando_link cabe entre a conclusão já persistida e a confirmação
local de aceitação. O check é imediato e não exige conclusao_id:
a reivindicação grava o estado antes de conhecer o id, e o commit
só acontece depois de associar os dois.

Manter os predicados novos iguais aos de EventoCanalSaida.
"""
from alembic import op
import sqlalchemy as sa


revision = "t1u2v3w4x5y6"
down_revision = "s0t1u2v3w4x5"
branch_labels = None
depends_on = None

_SQL_STATUS = (
    "status_envio IN ("
    "'reservado', 'aceito_provider', 'erro', "
    "'aguardando_link_seguro', 'preparando_link'"
    ")"
)
_SQL_STATUS_LOTE_3D1 = (
    "status_envio IN ("
    "'reservado', 'aceito_provider', 'erro', 'aguardando_link_seguro'"
    ")"
)
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
    " OR (status_envio = 'preparando_link' AND provider_message_id IS NULL "
    "AND codigo_erro IS NULL AND enviado_em IS NULL)"
    ")"
)
_SQL_COERENCIA_LOTE_3D1 = (
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
    with op.batch_alter_table("evento_canal_saida", schema=None) as batch_op:
        batch_op.drop_constraint("ck_evento_canal_saida_status", type_="check")
        batch_op.drop_constraint("ck_evento_canal_saida_coerencia", type_="check")
        batch_op.add_column(sa.Column("conclusao_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_evento_canal_saida_conclusao",
            "onboarding_canal_conclusao",
            ["conclusao_id"],
            ["id"],
        )
        batch_op.create_check_constraint("ck_evento_canal_saida_status", _SQL_STATUS)
        batch_op.create_check_constraint("ck_evento_canal_saida_coerencia", _SQL_COERENCIA)
    op.create_index(
        "ix_evento_canal_saida_conclusao_id",
        "evento_canal_saida",
        ["conclusao_id"],
    )


def downgrade():
    op.execute(
        "UPDATE evento_canal_saida "
        "SET status_envio = 'aguardando_link_seguro' "
        "WHERE status_envio = 'preparando_link'"
    )
    op.drop_index("ix_evento_canal_saida_conclusao_id", table_name="evento_canal_saida")
    with op.batch_alter_table("evento_canal_saida", schema=None) as batch_op:
        batch_op.drop_constraint("ck_evento_canal_saida_status", type_="check")
        batch_op.drop_constraint("ck_evento_canal_saida_coerencia", type_="check")
        batch_op.drop_constraint("fk_evento_canal_saida_conclusao", type_="foreignkey")
        batch_op.drop_column("conclusao_id")
        batch_op.create_check_constraint(
            "ck_evento_canal_saida_status",
            _SQL_STATUS_LOTE_3D1,
        )
        batch_op.create_check_constraint(
            "ck_evento_canal_saida_coerencia",
            _SQL_COERENCIA_LOTE_3D1,
        )
