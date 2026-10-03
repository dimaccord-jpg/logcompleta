"""central de plugins: auditoria administrativa do catalogo

Revision ID: k2l3m4n5o6p7
Revises: j1k2l3m4n5o6
Create Date: 2026-09-30

SCRUM-222 lote 3. Tabela nova para a governança do catálogo pelo ADM
da LogCompleta.

PluginEventoCentral exige conexao_id e registra a vida operacional da
conexão. AuditoriaGerencial é decisão do Cleiton e aceita contexto_json
livre. Nenhum dos dois descreve, sem misturar semântica, quem alterou
plugin, status, titularidade, capability ou política máxima.

Não altera as tabelas do lote 1. Não cria provider nem coluna de segredo.
Manter os predicados iguais a PluginAuditoriaAdministrativa.
"""
from alembic import op
import sqlalchemy as sa


revision = "k2l3m4n5o6p7"
down_revision = "j1k2l3m4n5o6"
branch_labels = None
depends_on = None

_SQL_ENTIDADE = "entidade IN ('plugin', 'capability')"
_SQL_ALTERACAO = (
    "alteracao IN ("
    "'plugin_criado', 'plugin_metadados', 'plugin_status', "
    "'plugin_titularidade', 'capability_criada', 'capability_politica', "
    "'capability_status'"
    ")"
)
_SQL_CAMPO = (
    "campo IN ("
    "'slug', 'nome', 'descricao', 'adapter_key', 'status', "
    "'titularidade', 'chave', 'natureza', 'politica_maxima'"
    ")"
)
_SQL_CAPABILITY = (
    "(entidade = 'plugin' AND capability_id IS NULL) "
    "OR (entidade = 'capability' AND capability_id IS NOT NULL)"
)


def upgrade():
    op.create_table(
        "plugin_auditoria_administrativa",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("entidade", sa.String(length=20), nullable=False),
        sa.Column("alteracao", sa.String(length=40), nullable=False),
        sa.Column("campo", sa.String(length=40), nullable=False),
        sa.Column("valor_anterior", sa.Text(), nullable=True),
        sa.Column("valor_novo", sa.Text(), nullable=True),
        sa.Column("plugin_id", sa.Integer(), nullable=False),
        sa.Column("capability_id", sa.Integer(), nullable=True),
        sa.Column("ator_user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(_SQL_ENTIDADE, name="ck_plugin_auditoria_admin_entidade"),
        sa.CheckConstraint(_SQL_ALTERACAO, name="ck_plugin_auditoria_admin_alteracao"),
        sa.CheckConstraint(_SQL_CAMPO, name="ck_plugin_auditoria_admin_campo"),
        sa.CheckConstraint(_SQL_CAPABILITY, name="ck_plugin_auditoria_admin_capability"),
        sa.ForeignKeyConstraint(["ator_user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["capability_id"], ["plugin_capability.id"]),
        sa.ForeignKeyConstraint(["plugin_id"], ["plugin.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_plugin_auditoria_admin_plugin_created",
        "plugin_auditoria_administrativa",
        ["plugin_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_plugin_auditoria_administrativa_plugin_id",
        "plugin_auditoria_administrativa",
        ["plugin_id"],
        unique=False,
    )
    op.create_index(
        "ix_plugin_auditoria_administrativa_capability_id",
        "plugin_auditoria_administrativa",
        ["capability_id"],
        unique=False,
    )
    op.create_index(
        "ix_plugin_auditoria_administrativa_ator_user_id",
        "plugin_auditoria_administrativa",
        ["ator_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_plugin_auditoria_administrativa_created_at",
        "plugin_auditoria_administrativa",
        ["created_at"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        "ix_plugin_auditoria_administrativa_created_at",
        table_name="plugin_auditoria_administrativa",
    )
    op.drop_index(
        "ix_plugin_auditoria_administrativa_ator_user_id",
        table_name="plugin_auditoria_administrativa",
    )
    op.drop_index(
        "ix_plugin_auditoria_administrativa_capability_id",
        table_name="plugin_auditoria_administrativa",
    )
    op.drop_index(
        "ix_plugin_auditoria_administrativa_plugin_id",
        table_name="plugin_auditoria_administrativa",
    )
    op.drop_index(
        "ix_plugin_auditoria_admin_plugin_created",
        table_name="plugin_auditoria_administrativa",
    )
    op.drop_table("plugin_auditoria_administrativa")
