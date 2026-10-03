"""saída da orientação de franquia sem execução útil

Revision ID: c0d1e2f3g4h5
Revises: b9c0d1e2f3g4
Create Date: 2026-10-03

A orientação comercial do canal não nasce de interpretação nem de
execução. A chave continua determinística e termina em resposta_principal.
Não grava texto, telefone nem saldo.

Manter o predicado igual ao de EventoCanalSaida._SQL_ORIGEM.
"""
from alembic import op


revision = "c0d1e2f3g4h5"
down_revision = "b9c0d1e2f3g4"
branch_labels = None
depends_on = None

_SQL_ORIGEM = (
    "(interpretacao_id IS NOT NULL AND execucao_operacional_id IS NULL)"
    " OR (interpretacao_id IS NULL AND execucao_operacional_id IS NOT NULL)"
    " OR (interpretacao_id IS NULL AND execucao_operacional_id IS NULL"
    " AND substr(chave_idempotencia, length(chave_idempotencia) - 38, 21)"
    " = ':orientacao_franquia:')"
)
_SQL_ORIGEM_ANTERIOR = (
    "(interpretacao_id IS NOT NULL AND execucao_operacional_id IS NULL)"
    " OR (interpretacao_id IS NULL AND execucao_operacional_id IS NOT NULL)"
)


def upgrade():
    with op.batch_alter_table("evento_canal_saida", schema=None) as batch_op:
        batch_op.drop_constraint("ck_evento_canal_saida_origem", type_="check")
        batch_op.create_check_constraint("ck_evento_canal_saida_origem", _SQL_ORIGEM)


def downgrade():
    with op.batch_alter_table("evento_canal_saida", schema=None) as batch_op:
        batch_op.drop_constraint("ck_evento_canal_saida_origem", type_="check")
        batch_op.create_check_constraint(
            "ck_evento_canal_saida_origem",
            _SQL_ORIGEM_ANTERIOR,
        )
