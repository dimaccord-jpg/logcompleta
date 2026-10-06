"""entrevista condicional de onboarding: respostas declaradas

Revision ID: l3m4n5o6p7q8
Revises: k2l3m4n5o6p7
Create Date: 2026-09-30

Tabela aditiva para o estado atual das respostas complementares.
Não altera User.job_role e não cria coluna por pergunta.

Usuários existentes permanecem sem linhas. A origem é fechada; a chave da
pergunta e a resposta não são enumeradas no banco para que uma pergunta nova
não exija migration.

Manter o predicado de origem igual a OnboardingRespostaDeclarada._SQL_ORIGEM.
"""
from alembic import op
import sqlalchemy as sa


revision = "l3m4n5o6p7q8"
down_revision = "k2l3m4n5o6p7"
branch_labels = None
depends_on = None

_SQL_ORIGEM = (
    "origem IN ('cadastro_web', 'onboarding_whatsapp', 'perfil_usuario')"
)


def upgrade():
    op.create_table(
        "onboarding_resposta_declarada",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("question_key", sa.String(length=80), nullable=False),
        sa.Column("answer_key", sa.String(length=80), nullable=False),
        sa.Column("origem", sa.String(length=40), nullable=False),
        sa.Column("taxonomia_versao", sa.String(length=40), nullable=False),
        sa.Column("declarada_em", sa.DateTime(), nullable=False),
        sa.Column("atualizada_em", sa.DateTime(), nullable=False),
        sa.CheckConstraint(_SQL_ORIGEM, name="ck_onboarding_resposta_origem"),
        sa.CheckConstraint(
            "length(question_key) > 0",
            name="ck_onboarding_resposta_question_key",
        ),
        sa.CheckConstraint(
            "length(answer_key) > 0",
            name="ck_onboarding_resposta_answer_key",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "question_key",
            name="uq_onboarding_resposta_user_pergunta",
        ),
    )
    op.create_index(
        "ix_onboarding_resposta_declarada_user_id",
        "onboarding_resposta_declarada",
        ["user_id"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        "ix_onboarding_resposta_declarada_user_id",
        table_name="onboarding_resposta_declarada",
    )
    op.drop_table("onboarding_resposta_declarada")
