"""tentativas numeradas da aplicação de status de canal

Revision ID: v3w4x5y6z7a8
Revises: u2v3w4x5y6z7
Create Date: 2026-10-02

Um evento pode ter mais de uma tentativa. A primeira aplicação
permanece. A unicidade passa a ser evento + número da tentativa.
Registros já gravados recebem a tentativa 1.

Não altera cadastro, conclusão nem cobrança.

Manter o predicado igual ao de AplicacaoStatusCanal._SQL_TENTATIVA.
"""
from alembic import op
import sqlalchemy as sa


revision = "v3w4x5y6z7a8"
down_revision = "u2v3w4x5y6z7"
branch_labels = None
depends_on = None

_SQL_TENTATIVA = "numero_tentativa >= 1"


def upgrade():
    with op.batch_alter_table("aplicacao_status_canal", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "numero_tentativa",
                sa.Integer(),
                nullable=False,
                server_default="1",
            )
        )
        batch_op.add_column(
            sa.Column(
                "criado_em",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            )
        )
        batch_op.drop_constraint(
            "uq_aplicacao_status_canal_evento",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_aplicacao_status_canal_evento_tentativa",
            ["evento_recebido_id", "numero_tentativa"],
        )
        batch_op.create_check_constraint(
            "ck_aplicacao_status_canal_tentativa",
            _SQL_TENTATIVA,
        )


def downgrade():
    # O esquema anterior guarda uma linha por evento. Fica a tentativa
    # mais recente, que é o desfecho conhecido. As anteriores saem só
    # aqui, para a unicidade antiga poder voltar.
    op.execute(
        sa.text(
            "DELETE FROM aplicacao_status_canal "
            "WHERE id NOT IN ("
            "SELECT id FROM ("
            "SELECT MAX(id) AS id FROM aplicacao_status_canal "
            "GROUP BY evento_recebido_id"
            ") AS ultimas)"
        )
    )
    with op.batch_alter_table("aplicacao_status_canal", schema=None) as batch_op:
        batch_op.drop_constraint(
            "ck_aplicacao_status_canal_tentativa",
            type_="check",
        )
        batch_op.drop_constraint(
            "uq_aplicacao_status_canal_evento_tentativa",
            type_="unique",
        )
        batch_op.drop_column("criado_em")
        batch_op.drop_column("numero_tentativa")
        batch_op.create_unique_constraint(
            "uq_aplicacao_status_canal_evento",
            ["evento_recebido_id"],
        )
