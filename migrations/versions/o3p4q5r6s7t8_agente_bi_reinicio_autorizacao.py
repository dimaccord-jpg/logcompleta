"""autorizacao temporaria de reinicio do AgenteBI

Revision ID: o3p4q5r6s7t8
Revises: n2o3p4q5r6s7
Create Date: 2026-10-29

A tabela só existe para a transação de reinício. Os gatilhos de DELETE
passam a consultar essa linha e continuam recusando a exclusão direta
quando ela não está presente. A migration recria os gatilhos já
instalados; não os desliga.
"""
from alembic import op
import sqlalchemy as sa

from app.agente_bi_aplicacao_integridade import instalar_protecao_aplicacao
from app.agente_bi_execucao_integridade import instalar_protecao_execucao
from app.agente_bi_integridade import instalar_protecao_agente_bi


revision = "o3p4q5r6s7t8"
down_revision = "n2o3p4q5r6s7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agente_bi_reinicio_autorizacao",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("usuario_id", sa.Integer(), nullable=False),
        sa.Column("conta_id", sa.Integer(), nullable=False),
        sa.Column("franquia_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "usuario_id",
            "conta_id",
            "franquia_id",
            name="uq_agente_bi_reinicio_autorizacao_escopo",
        ),
    )
    bind = op.get_bind()
    instalar_protecao_agente_bi(bind, reinicio=True)
    instalar_protecao_execucao(bind, reinicio=True)
    instalar_protecao_aplicacao(bind, reinicio=True)


def downgrade():
    bind = op.get_bind()
    instalar_protecao_agente_bi(bind, reinicio=False)
    instalar_protecao_execucao(bind, reinicio=False)
    instalar_protecao_aplicacao(bind, reinicio=False)
    op.drop_table("agente_bi_reinicio_autorizacao")
