"""remove freemium daily chat usage fields

Revision ID: l5m6n7o8p9q0
Revises: k4l5m6n7o8p9
Create Date: 2026-04-04
"""
from alembic import op
import sqlalchemy as sa


revision = "l5m6n7o8p9q0"
down_revision = "k4l5m6n7o8p9"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    bind.execute(
        sa.text("DELETE FROM config_regras WHERE chave = :chave"),
        {"chave": "freemium_consultas_dia"},
    )
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.drop_column("chat_consultas_hoje")
        batch_op.drop_column("chat_data_ultima_consulta")


def downgrade():
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("chat_data_ultima_consulta", sa.DateTime(), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "chat_consultas_hoje",
                sa.Integer(),
                nullable=True,
                server_default=sa.text("0"),
            )
        )
    bind = op.get_bind()
    bind.execute(
        sa.text("DELETE FROM config_regras WHERE chave = :chave"),
        {"chave": "freemium_consultas_dia"},
    )
    bind.execute(
        sa.text(
            "INSERT INTO config_regras (chave, valor_inteiro, descricao) "
            "VALUES (:chave, :valor, :descricao)"
        ),
        {
            "chave": "freemium_consultas_dia",
            "valor": 5,
            "descricao": "Consultas grátis por dia (chat Júlia)",
        },
    )
