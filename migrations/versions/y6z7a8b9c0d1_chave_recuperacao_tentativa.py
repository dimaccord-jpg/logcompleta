"""chave idempotente da recuperação manual

Revision ID: y6z7a8b9c0d1
Revises: x5y6z7a8b9c0
Create Date: 2026-10-02

A tentativa 1 permanece sem chave. A recuperação manual passa a exigir
a chave opaca da ação. A unicidade (saida_id, chave_recuperacao) admite
vários NULL: no PostgreSQL e no SQLite, NULL não colide com NULL, então
a tentativa inicial não precisa de índice parcial.

Linhas 2 ou 3 já gravadas em desenvolvimento recebem um preenchimento
opaco derivado só do id interno. Não é decisão humana nem dado pessoal.
A tentativa 1 não é alterada.

Manter o predicado igual ao de TentativaEnvioCanal._SQL_IDENTIDADE.
"""
from alembic import op
import sqlalchemy as sa


revision = "y6z7a8b9c0d1"
down_revision = "x5y6z7a8b9c0"
branch_labels = None
depends_on = None

_SQL_IDENTIDADE = (
    "("
    "(numero_tentativa = 1 AND origem_tentativa = 'envio_inicial' "
    "AND chave_recuperacao IS NULL)"
    " OR (numero_tentativa > 1 AND origem_tentativa = 'recuperacao_manual' "
    "AND chave_recuperacao IS NOT NULL "
    "AND length(chave_recuperacao) BETWEEN 1 AND 64)"
    ")"
)


def _chave_legada(row_id: int) -> str:
    bruto = f"{int(row_id):032x}"
    return (
        f"{bruto[0:8]}-{bruto[8:12]}-{bruto[12:16]}-"
        f"{bruto[16:20]}-{bruto[20:32]}"
    )


def _preencher_legadas() -> None:
    conn = op.get_bind()
    linhas = conn.execute(
        sa.text(
            "SELECT id FROM tentativa_envio_canal "
            "WHERE numero_tentativa > 1 AND chave_recuperacao IS NULL"
        )
    ).fetchall()
    for linha in linhas:
        row_id = int(linha[0])
        conn.execute(
            sa.text(
                "UPDATE tentativa_envio_canal "
                "SET chave_recuperacao = :chave WHERE id = :id"
            ),
            {"chave": _chave_legada(row_id), "id": row_id},
        )


def upgrade():
    with op.batch_alter_table("tentativa_envio_canal", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("chave_recuperacao", sa.String(length=64), nullable=True)
        )
    _preencher_legadas()
    with op.batch_alter_table("tentativa_envio_canal", schema=None) as batch_op:
        batch_op.create_check_constraint(
            "ck_tentativa_envio_canal_identidade",
            _SQL_IDENTIDADE,
        )
        batch_op.create_unique_constraint(
            "uq_tentativa_envio_canal_saida_chave",
            ["saida_id", "chave_recuperacao"],
        )


def downgrade():
    with op.batch_alter_table("tentativa_envio_canal", schema=None) as batch_op:
        batch_op.drop_constraint(
            "uq_tentativa_envio_canal_saida_chave",
            type_="unique",
        )
        batch_op.drop_constraint(
            "ck_tentativa_envio_canal_identidade",
            type_="check",
        )
        batch_op.drop_column("chave_recuperacao")
