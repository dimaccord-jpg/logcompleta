"""hash do alias curto da conclusão de canal

Revision ID: e3f4g5h6i7j8
Revises: d1e2f3g4h5i6
Create Date: 2026-10-05

A URL enviada no WhatsApp passa a ser /c/<alias>. O banco guarda só o
hash SHA-256, nunca o alias bruto. Conclusões já emitidas ficam com
alias_hash nulo e continuam abrindo pelo token assinado antigo.

O alias não tem vida própria: expiração, consumo e revogação continuam
na linha de onboarding_canal_conclusao.
"""
from alembic import op
import sqlalchemy as sa


revision = "e3f4g5h6i7j8"
down_revision = "d1e2f3g4h5i6"
branch_labels = None
depends_on = None

_SQL_ALIAS = "alias_hash IS NULL OR length(alias_hash) = 64"


def upgrade():
    with op.batch_alter_table("onboarding_canal_conclusao", schema=None) as batch_op:
        batch_op.add_column(sa.Column("alias_hash", sa.String(length=64), nullable=True))
        batch_op.create_check_constraint(
            "ck_onboarding_canal_conclusao_alias",
            _SQL_ALIAS,
        )
        batch_op.create_unique_constraint(
            "uq_onboarding_canal_conclusao_alias_hash",
            ["alias_hash"],
        )


def downgrade():
    with op.batch_alter_table("onboarding_canal_conclusao", schema=None) as batch_op:
        batch_op.drop_constraint("uq_onboarding_canal_conclusao_alias_hash", type_="unique")
        batch_op.drop_constraint("ck_onboarding_canal_conclusao_alias", type_="check")
        batch_op.drop_column("alias_hash")
