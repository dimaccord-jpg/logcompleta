"""fase 2 etapa 3: Franquia operacional (limite, consumo, ciclo, status Cleiton)

Revision ID: j3k4l5m6n7o8
Revises: i2j3k4l5m6n7
Create Date: 2026-04-04

- Colunas operacionais em `franquia` com tipos decimais fixos (evita arredondamento float).
- Status legado `ativa` migrado para `active` (estados Fase 2: active, degraded, expired, blocked).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


revision = "j3k4l5m6n7o8"
down_revision = "i2j3k4l5m6n7"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("franquia", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("limite_total", sa.Numeric(18, 6), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "consumo_acumulado",
                sa.Numeric(18, 6),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(sa.Column("inicio_ciclo", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("fim_ciclo", sa.DateTime(), nullable=True))

    conn = op.get_bind()
    conn.execute(
        text("UPDATE franquia SET status = 'active' WHERE status = 'ativa'")
    )

    with op.batch_alter_table("franquia", schema=None) as batch_op:
        batch_op.alter_column(
            "status",
            existing_type=sa.String(length=30),
            nullable=False,
            server_default="active",
        )


def downgrade():
    conn = op.get_bind()
    conn.execute(
        text("UPDATE franquia SET status = 'ativa' WHERE status IN ('active','degraded','expired','blocked')")
    )

    with op.batch_alter_table("franquia", schema=None) as batch_op:
        batch_op.alter_column(
            "status",
            existing_type=sa.String(length=30),
            nullable=False,
            server_default="ativa",
        )

    with op.batch_alter_table("franquia", schema=None) as batch_op:
        batch_op.drop_column("fim_ciclo")
        batch_op.drop_column("inicio_ciclo")
        batch_op.drop_column("consumo_acumulado")
        batch_op.drop_column("limite_total")
