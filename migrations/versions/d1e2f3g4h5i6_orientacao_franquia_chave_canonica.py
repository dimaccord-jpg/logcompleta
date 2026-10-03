"""chave canônica da orientação de franquia

Revision ID: d1e2f3g4h5i6
Revises: c0d1e2f3g4h5
Create Date: 2026-10-03

A orientação só permanece quando a chave é exatamente
meta_whatsapp:<evento_entrada_id>:orientacao_franquia:resposta_principal,
sem interpretação e sem execução. Marca solta na chave não basta.

Não altera billing, franquia, consumo, interpretação nem execução.
Linhas já gravadas pelo serviço continuam válidas: a chave delas já é
a canônica. Esta revisão não reescreve linhas.

Downgrade: restaura o predicado anterior, mais fraco, de c0d1e2f3g4h5.
Não reescreve chaves nem origens. Linhas de orientação já gravadas
continuam aceitas por aquele predicado, porque a chave canônica contém
a substring. O predicado restaurado não prova que a chave pertence ao
evento da linha. Não há conversão semântica.
"""
from alembic import op


revision = "d1e2f3g4h5i6"
down_revision = "c0d1e2f3g4h5"
branch_labels = None
depends_on = None

_SQL_CHAVE_ORIENTACAO = (
    "('meta_whatsapp:' || CAST(evento_entrada_id AS TEXT)"
    " || ':orientacao_franquia:resposta_principal')"
)
_SQL_SEM_MARCA_ORIENTACAO = (
    "(length(chave_idempotencia) < 39 OR "
    "substr(chave_idempotencia, length(chave_idempotencia) - 38, 21)"
    " <> ':orientacao_franquia:')"
)
_SQL_ORIGEM = (
    "(interpretacao_id IS NOT NULL AND execucao_operacional_id IS NULL"
    f" AND {_SQL_SEM_MARCA_ORIENTACAO})"
    " OR (interpretacao_id IS NULL AND execucao_operacional_id IS NOT NULL"
    f" AND {_SQL_SEM_MARCA_ORIENTACAO})"
    " OR (interpretacao_id IS NULL AND execucao_operacional_id IS NULL"
    f" AND chave_idempotencia = {_SQL_CHAVE_ORIENTACAO})"
)
_SQL_ORIGEM_ANTERIOR = (
    "(interpretacao_id IS NOT NULL AND execucao_operacional_id IS NULL)"
    " OR (interpretacao_id IS NULL AND execucao_operacional_id IS NOT NULL)"
    " OR (interpretacao_id IS NULL AND execucao_operacional_id IS NULL"
    " AND substr(chave_idempotencia, length(chave_idempotencia) - 38, 21)"
    " = ':orientacao_franquia:')"
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
