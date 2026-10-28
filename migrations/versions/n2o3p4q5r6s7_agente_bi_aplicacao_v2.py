"""aplicacao v2 do AgenteBI

Revision ID: n2o3p4q5r6s7
Revises: m1n2o3p4q5r6
Create Date: 2026-10-28

Identidade duravel de um plano patch.definir.v2. Nao cria fila, nao grava
filtro e nao publica dashboard. As chaves estrangeiras usam as unicidades
de escopo que analise, dataset e dashboard ja possuem. O gatilho confere
dataset, contrato, revisao e vinculo, porque essas colunas nao cabem nas
unicidades antigas sem reescrever tabela.

Nao implantar o orquestrador de publicacao junto com esta migration.
"""
from alembic import op
import sqlalchemy as sa

from app.agente_bi_aplicacao_integridade import (
    instalar_protecao_aplicacao,
    remover_protecao_aplicacao,
    sql_anterior_contrato_aplicacao,
    sql_artefatos_aplicacao,
    sql_estado_aplicacao,
    sql_papeis_aplicacao,
    sql_plano_contrato_aplicacao,
)
from app.agente_bi_integridade import faixa_inteira


revision = "n2o3p4q5r6s7"
down_revision = "m1n2o3p4q5r6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agente_bi_aplicacao",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("conta_id", sa.Integer(), nullable=False),
        sa.Column("franquia_id", sa.Integer(), nullable=False),
        sa.Column("usuario_id", sa.Integer(), nullable=False),
        sa.Column("plano_id", sa.Integer(), nullable=False),
        sa.Column("plano_contrato", sa.String(length=40), nullable=False),
        sa.Column("dataset_id", sa.Integer(), nullable=False),
        sa.Column("dataset_versao", sa.Integer(), nullable=False),
        sa.Column("dataset_impressao", sa.String(length=64), nullable=False),
        sa.Column("dashboard_id", sa.Integer(), nullable=False),
        sa.Column("dashboard_chave", sa.String(length=80), nullable=False),
        sa.Column("revisao_base", sa.Integer(), nullable=False),
        sa.Column("definicao_anterior_id", sa.Integer(), nullable=False),
        sa.Column("definicao_anterior_contrato", sa.String(length=40), nullable=False),
        sa.Column("estado", sa.String(length=20), nullable=False),
        sa.Column("definicao_id", sa.Integer(), nullable=True),
        sa.Column("definicao_contrato", sa.String(length=40), nullable=True),
        sa.Column("calculo_id", sa.Integer(), nullable=True),
        sa.Column("calculo_contrato", sa.String(length=40), nullable=True),
        sa.Column("dashboard_publicado_id", sa.Integer(), nullable=True),
        sa.Column("revisao_publicada", sa.Integer(), nullable=True),
        sa.Column("preparada_em", sa.DateTime(), nullable=False),
        sa.Column("concluida_em", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(sql_estado_aplicacao(), name="ck_agente_bi_aplicacao_estado"),
        sa.CheckConstraint(sql_plano_contrato_aplicacao(), name="ck_agente_bi_aplicacao_plano_contrato"),
        sa.CheckConstraint(
            sql_anterior_contrato_aplicacao(),
            name="ck_agente_bi_aplicacao_anterior_contrato",
        ),
        sa.CheckConstraint(faixa_inteira("revisao_base"), name="ck_agente_bi_aplicacao_revisao_base"),
        sa.CheckConstraint(
            "revisao_publicada IS NULL OR (" + faixa_inteira("revisao_publicada") + ")",
            name="ck_agente_bi_aplicacao_revisao_publicada",
        ),
        sa.CheckConstraint(sql_artefatos_aplicacao(), name="ck_agente_bi_aplicacao_artefatos"),
        sa.CheckConstraint(sql_papeis_aplicacao(), name="ck_agente_bi_aplicacao_papeis"),
        sa.ForeignKeyConstraint(
            ["plano_id", "usuario_id", "conta_id", "franquia_id"],
            [
                "agente_bi_analise.id",
                "agente_bi_analise.usuario_id",
                "agente_bi_analise.conta_id",
                "agente_bi_analise.franquia_id",
            ],
            name="fk_agente_bi_aplicacao_plano",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["definicao_anterior_id", "usuario_id", "conta_id", "franquia_id"],
            [
                "agente_bi_analise.id",
                "agente_bi_analise.usuario_id",
                "agente_bi_analise.conta_id",
                "agente_bi_analise.franquia_id",
            ],
            name="fk_agente_bi_aplicacao_anterior",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["definicao_id", "usuario_id", "conta_id", "franquia_id"],
            [
                "agente_bi_analise.id",
                "agente_bi_analise.usuario_id",
                "agente_bi_analise.conta_id",
                "agente_bi_analise.franquia_id",
            ],
            name="fk_agente_bi_aplicacao_definicao",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["calculo_id", "usuario_id", "conta_id", "franquia_id"],
            [
                "agente_bi_analise.id",
                "agente_bi_analise.usuario_id",
                "agente_bi_analise.conta_id",
                "agente_bi_analise.franquia_id",
            ],
            name="fk_agente_bi_aplicacao_calculo",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "dataset_id",
                "usuario_id",
                "conta_id",
                "franquia_id",
                "dataset_versao",
                "dataset_impressao",
            ],
            [
                "agente_bi_dataset.id",
                "agente_bi_dataset.usuario_id",
                "agente_bi_dataset.conta_id",
                "agente_bi_dataset.franquia_id",
                "agente_bi_dataset.versao",
                "agente_bi_dataset.impressao",
            ],
            name="fk_agente_bi_aplicacao_dataset",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["dashboard_id", "usuario_id", "conta_id", "franquia_id"],
            [
                "agente_bi_dashboard.id",
                "agente_bi_dashboard.usuario_id",
                "agente_bi_dashboard.conta_id",
                "agente_bi_dashboard.franquia_id",
            ],
            name="fk_agente_bi_aplicacao_painel",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["dashboard_publicado_id", "usuario_id", "conta_id", "franquia_id"],
            [
                "agente_bi_dashboard.id",
                "agente_bi_dashboard.usuario_id",
                "agente_bi_dashboard.conta_id",
                "agente_bi_dashboard.franquia_id",
            ],
            name="fk_agente_bi_aplicacao_publicado",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["conta_id"],
            ["conta.id"],
            name="fk_agente_bi_aplicacao_conta",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["franquia_id"],
            ["franquia.id"],
            name="fk_agente_bi_aplicacao_franquia",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["usuario_id"],
            ["user.id"],
            name="fk_agente_bi_aplicacao_usuario",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plano_id", name="uq_agente_bi_aplicacao_plano"),
    )
    op.create_index(
        "ix_agente_bi_aplicacao_escopo",
        "agente_bi_aplicacao",
        ["conta_id", "franquia_id", "usuario_id"],
        unique=False,
    )
    instalar_protecao_aplicacao(op.get_bind())


def downgrade():
    remover_protecao_aplicacao(op.get_bind())
    op.drop_index("ix_agente_bi_aplicacao_escopo", table_name="agente_bi_aplicacao")
    op.drop_table("agente_bi_aplicacao")
