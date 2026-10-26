"""blocos do dataset preparado do AgenteBI

Revision ID: k9l0m1n2o3p4
Revises: j8k9l0m1n2o3
Create Date: 2026-10-23

O contrato de um JSON (1,5 MB e 20 mil itens) continua valendo para cada
documento gravado. O dataset preparado que a ingestão já aceita não cabe
nesse documento único, então os dados são fatiados em blocos da mesma
versão. O bloco não é versão, não é preview e não existe sem o dataset.
"""
from alembic import op
import sqlalchemy as sa

from app.agente_bi_integridade import INTEIRO_COLUNA_MAX, faixa_inteira, instalar_protecao_agente_bi


revision = "k9l0m1n2o3p4"
down_revision = "j8k9l0m1n2o3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agente_bi_dataset_bloco",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dataset_id", sa.Integer(), nullable=False),
        sa.Column("conta_id", sa.Integer(), nullable=False),
        sa.Column("franquia_id", sa.Integer(), nullable=False),
        sa.Column("usuario_id", sa.Integer(), nullable=False),
        sa.Column("indice", sa.Integer(), nullable=False),
        sa.Column("registros", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("conteudo_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            f"indice >= 0 AND indice <= {INTEIRO_COLUNA_MAX}",
            name="ck_agente_bi_dataset_bloco_indice",
        ),
        sa.CheckConstraint(faixa_inteira("registros"), name="ck_agente_bi_dataset_bloco_registros"),
        sa.ForeignKeyConstraint(
            ["dataset_id", "usuario_id", "conta_id", "franquia_id"],
            [
                "agente_bi_dataset.id",
                "agente_bi_dataset.usuario_id",
                "agente_bi_dataset.conta_id",
                "agente_bi_dataset.franquia_id",
            ],
            name="fk_agente_bi_dataset_bloco_dataset",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "dataset_id",
            "indice",
            name="uq_agente_bi_dataset_bloco_indice",
        ),
    )
    op.create_index(
        "ix_agente_bi_dataset_bloco_escopo",
        "agente_bi_dataset_bloco",
        ["conta_id", "franquia_id", "usuario_id"],
        unique=False,
    )
    instalar_protecao_agente_bi(op.get_bind())


def downgrade():
    op.drop_index("ix_agente_bi_dataset_bloco_escopo", table_name="agente_bi_dataset_bloco")
    op.drop_table("agente_bi_dataset_bloco")
