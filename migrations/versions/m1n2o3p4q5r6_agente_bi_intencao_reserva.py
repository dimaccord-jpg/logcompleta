"""intenção durável de reserva do AgenteBI

Revision ID: m1n2o3p4q5r6
Revises: l0m1n2o3p4q5
Create Date: 2026-10-24

A tentativa financeira só fica visível no commit interno de `_reservar`.
Esta coluna é publicada antes disso. Sem ela, a queda do processo apaga
a única prova de que uma reserva ainda podia confirmar, e o encerramento
gravaria `ausente` cedo demais. A coluna é aditiva e não reescreve tentativa.
"""
from alembic import op
import sqlalchemy as sa


revision = "m1n2o3p4q5r6"
down_revision = "l0m1n2o3p4q5"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "agente_bi_execucao",
        sa.Column(
            "reserva_em_andamento",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade():
    op.drop_column("agente_bi_execucao", "reserva_em_andamento")
