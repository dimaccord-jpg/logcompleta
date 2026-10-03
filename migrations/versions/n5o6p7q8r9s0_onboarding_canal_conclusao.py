"""conclusão do onboarding por canal: token de uso único e origem do cadastro

Revision ID: n5o6p7q8r9s0
Revises: m4n5o6p7q8r9
Create Date: 2026-10-01

O token guarda só o hash. Não há senha, e-mail nem telefone.
cadastro_origem fica NULL nos usuários já existentes.

Manter os predicados iguais aos de OnboardingCanalConclusao e User._SQL_CADASTRO_ORIGEM.
"""
from alembic import op
import sqlalchemy as sa


revision = "n5o6p7q8r9s0"
down_revision = "m4n5o6p7q8r9"
branch_labels = None
depends_on = None

_SQL_FINALIDADE = "finalidade IN ('definir_senha', 'vincular_conta')"
_SQL_ESTADO = "estado IN ('emitido', 'consumido', 'revogado')"
_SQL_COERENCIA = (
    "((estado = 'emitido' AND consumido_em IS NULL)"
    " OR (estado = 'consumido' AND consumido_em IS NOT NULL)"
    " OR (estado = 'revogado' AND consumido_em IS NULL))"
)
_SQL_HASH = "length(token_hash) = 64"
_SQL_EMITIDO_ATIVO = "estado = 'emitido'"
_SQL_CADASTRO_ORIGEM = (
    "cadastro_origem IS NULL OR cadastro_origem = 'onboarding_whatsapp'"
)


def upgrade():
    op.add_column(
        "user",
        sa.Column("cadastro_origem", sa.String(length=40), nullable=True),
    )
    op.create_check_constraint(
        "ck_user_cadastro_origem",
        "user",
        _SQL_CADASTRO_ORIGEM,
    )
    op.create_table(
        "onboarding_canal_conclusao",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("onboarding_id", sa.Integer(), nullable=False),
        sa.Column("finalidade", sa.String(length=40), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("estado", sa.String(length=20), nullable=False),
        sa.Column("emitido_em", sa.DateTime(), nullable=False),
        sa.Column("expira_em", sa.DateTime(), nullable=False),
        sa.Column("consumido_em", sa.DateTime(), nullable=True),
        sa.Column("atualizada_em", sa.DateTime(), nullable=False),
        sa.CheckConstraint(_SQL_FINALIDADE, name="ck_onboarding_canal_conclusao_finalidade"),
        sa.CheckConstraint(_SQL_ESTADO, name="ck_onboarding_canal_conclusao_estado"),
        sa.CheckConstraint(_SQL_COERENCIA, name="ck_onboarding_canal_conclusao_coerencia"),
        sa.CheckConstraint(_SQL_HASH, name="ck_onboarding_canal_conclusao_hash"),
        sa.ForeignKeyConstraint(["onboarding_id"], ["onboarding_canal.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "ix_onboarding_canal_conclusao_onboarding_id",
        "onboarding_canal_conclusao",
        ["onboarding_id"],
        unique=False,
    )
    op.create_index(
        "ix_onboarding_canal_conclusao_estado",
        "onboarding_canal_conclusao",
        ["estado"],
        unique=False,
    )
    op.create_index(
        "uq_onboarding_canal_conclusao_emitida",
        "onboarding_canal_conclusao",
        ["onboarding_id"],
        unique=True,
        postgresql_where=sa.text(_SQL_EMITIDO_ATIVO),
        sqlite_where=sa.text(_SQL_EMITIDO_ATIVO),
    )


def downgrade():
    op.drop_index(
        "uq_onboarding_canal_conclusao_emitida",
        table_name="onboarding_canal_conclusao",
    )
    op.drop_index(
        "ix_onboarding_canal_conclusao_estado",
        table_name="onboarding_canal_conclusao",
    )
    op.drop_index(
        "ix_onboarding_canal_conclusao_onboarding_id",
        table_name="onboarding_canal_conclusao",
    )
    op.drop_table("onboarding_canal_conclusao")
    op.drop_constraint("ck_user_cadastro_origem", "user", type_="check")
    op.drop_column("user", "cadastro_origem")
