"""central de plugins: dominio e persistencia

Revision ID: i0j1k2l3m4n5
Revises: h9i0j1k2l3m4
Create Date: 2026-09-29

SCRUM-222 lote 1. Tabelas novas da Central de Plugins.
Não altera tabelas existentes. Não semeia provider real.
O índice parcial uq_plugin_conexao_user_plugin_ativa garante um slot
ocupado por usuário e plugin. desconectado fica fora do índice.

Manter o predicado igual a PluginConexao._SQL_OCUPA_SLOT.
"""
from alembic import op
import sqlalchemy as sa


revision = "i0j1k2l3m4n5"
down_revision = "h9i0j1k2l3m4"
branch_labels = None
depends_on = None

_SQL_OCUPA_SLOT = "estado IN ('disponivel', 'aguardando_configuracao', 'conectado', 'requer_atencao', 'desabilitado', 'bloqueado')"


def upgrade():
    op.create_table(
        "plugin",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("nome", sa.String(length=255), nullable=False),
        sa.Column("descricao", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("suporta_titularidade_pessoal", sa.Boolean(), nullable=False),
        sa.Column("suporta_titularidade_corporativa", sa.Boolean(), nullable=False),
        sa.Column("adapter_key", sa.String(length=80), nullable=False),
        sa.Column("criado_por_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('rascunho', 'disponivel', 'desabilitado', 'bloqueado')",
            name="ck_plugin_status",
        ),
        sa.CheckConstraint(
            "(suporta_titularidade_pessoal OR suporta_titularidade_corporativa)",
            name="ck_plugin_titularidade_suportada",
        ),
        sa.ForeignKeyConstraint(["criado_por_user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_plugin_slug"),
    )
    op.create_index("ix_plugin_status", "plugin", ["status"], unique=False)
    op.create_index("ix_plugin_adapter_key", "plugin", ["adapter_key"], unique=False)
    op.create_index(
        "ix_plugin_criado_por_user_id", "plugin", ["criado_por_user_id"], unique=False
    )

    op.create_table(
        "plugin_capability",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("plugin_id", sa.Integer(), nullable=False),
        sa.Column("chave", sa.String(length=80), nullable=False),
        sa.Column("nome", sa.String(length=255), nullable=False),
        sa.Column("descricao", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("politica_maxima", sa.String(length=20), nullable=False),
        sa.Column("natureza", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('disponivel', 'desabilitado', 'bloqueado')",
            name="ck_plugin_capability_status",
        ),
        sa.CheckConstraint(
            "politica_maxima IN ('negada', 'permitida')",
            name="ck_plugin_capability_politica_maxima",
        ),
        sa.CheckConstraint(
            "natureza IN ('ativa', 'passiva')",
            name="ck_plugin_capability_natureza",
        ),
        sa.ForeignKeyConstraint(["plugin_id"], ["plugin.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plugin_id", "chave", name="uq_plugin_capability_plugin_chave"),
    )
    op.create_index(
        "ix_plugin_capability_plugin_id", "plugin_capability", ["plugin_id"], unique=False
    )
    op.create_index(
        "ix_plugin_capability_status", "plugin_capability", ["status"], unique=False
    )

    op.create_table(
        "plugin_conexao",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("plugin_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("conta_id", sa.Integer(), nullable=True),
        sa.Column("usuario_originador_id", sa.Integer(), nullable=False),
        sa.Column("titularidade", sa.String(length=20), nullable=False),
        sa.Column("identificador_externo", sa.String(length=120), nullable=True),
        sa.Column("estado", sa.String(length=40), nullable=False),
        sa.Column("diagnostico_codigo", sa.String(length=80), nullable=True),
        sa.Column("diagnostico_resumo", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("estado_alterado_em", sa.DateTime(), nullable=False),
        sa.Column("desconectado_em", sa.DateTime(), nullable=True),
        sa.Column("revogado_em", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "estado IN ("
            "'disponivel', 'aguardando_configuracao', 'conectado', "
            "'requer_atencao', 'desabilitado', 'bloqueado', 'desconectado'"
            ")",
            name="ck_plugin_conexao_estado",
        ),
        sa.CheckConstraint(
            "titularidade IN ('pessoal', 'corporativa')",
            name="ck_plugin_conexao_titularidade",
        ),
        sa.CheckConstraint(
            "("
            "(titularidade = 'pessoal' AND conta_id IS NULL) "
            "OR (titularidade = 'corporativa' AND conta_id IS NOT NULL)"
            ")",
            name="ck_plugin_conexao_titularidade_conta",
        ),
        sa.CheckConstraint(
            "(revogado_em IS NULL OR estado = 'desconectado')",
            name="ck_plugin_conexao_revogacao",
        ),
        sa.ForeignKeyConstraint(["plugin_id"], ["plugin.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["conta_id"], ["conta.id"]),
        sa.ForeignKeyConstraint(["usuario_originador_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_plugin_conexao_plugin_id", "plugin_conexao", ["plugin_id"], unique=False
    )
    op.create_index(
        "ix_plugin_conexao_conta_id", "plugin_conexao", ["conta_id"], unique=False
    )
    op.create_index(
        "ix_plugin_conexao_usuario_originador_id",
        "plugin_conexao",
        ["usuario_originador_id"],
        unique=False,
    )
    op.create_index("ix_plugin_conexao_estado", "plugin_conexao", ["estado"], unique=False)
    op.create_index(
        "ix_plugin_conexao_user_estado",
        "plugin_conexao",
        ["user_id", "estado"],
        unique=False,
    )
    op.create_index(
        "uq_plugin_conexao_user_plugin_ativa",
        "plugin_conexao",
        ["user_id", "plugin_id"],
        unique=True,
        postgresql_where=sa.text(_SQL_OCUPA_SLOT),
        sqlite_where=sa.text(_SQL_OCUPA_SLOT),
    )

    op.create_table(
        "plugin_credencial_referencia",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conexao_id", sa.Integer(), nullable=False),
        sa.Column("estado", sa.String(length=30), nullable=False),
        sa.Column("cofre_referencia", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "estado IN ('nao_provisionada', 'referenciada', 'invalida', 'revogada')",
            name="ck_plugin_credencial_estado",
        ),
        sa.CheckConstraint(
            "("
            "(estado = 'referenciada' AND cofre_referencia IS NOT NULL) "
            "OR (estado <> 'referenciada')"
            ")",
            name="ck_plugin_credencial_referencia_presente",
        ),
        sa.ForeignKeyConstraint(["conexao_id"], ["plugin_conexao.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conexao_id", name="uq_plugin_credencial_conexao"),
    )
    op.create_index(
        "ix_plugin_credencial_referencia_conexao_id",
        "plugin_credencial_referencia",
        ["conexao_id"],
        unique=False,
    )

    op.create_table(
        "plugin_restricao_usuario",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conexao_id", sa.Integer(), nullable=False),
        sa.Column("capability_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("efeito", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "efeito IN ('bloqueada_pelo_usuario')",
            name="ck_plugin_restricao_efeito",
        ),
        sa.ForeignKeyConstraint(["conexao_id"], ["plugin_conexao.id"]),
        sa.ForeignKeyConstraint(["capability_id"], ["plugin_capability.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conexao_id",
            "capability_id",
            name="uq_plugin_restricao_conexao_capability",
        ),
    )
    op.create_index(
        "ix_plugin_restricao_usuario_conexao_id",
        "plugin_restricao_usuario",
        ["conexao_id"],
        unique=False,
    )
    op.create_index(
        "ix_plugin_restricao_usuario_capability_id",
        "plugin_restricao_usuario",
        ["capability_id"],
        unique=False,
    )
    op.create_index(
        "ix_plugin_restricao_usuario_user_id",
        "plugin_restricao_usuario",
        ["user_id"],
        unique=False,
    )

    op.create_table(
        "plugin_concessao_provedor",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conexao_id", sa.Integer(), nullable=False),
        sa.Column("capability_id", sa.Integer(), nullable=False),
        sa.Column("resultado", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "resultado IN ('concedida', 'negada')",
            name="ck_plugin_concessao_resultado",
        ),
        sa.ForeignKeyConstraint(["conexao_id"], ["plugin_conexao.id"]),
        sa.ForeignKeyConstraint(["capability_id"], ["plugin_capability.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conexao_id",
            "capability_id",
            name="uq_plugin_concessao_conexao_capability",
        ),
    )
    op.create_index(
        "ix_plugin_concessao_provedor_conexao_id",
        "plugin_concessao_provedor",
        ["conexao_id"],
        unique=False,
    )
    op.create_index(
        "ix_plugin_concessao_provedor_capability_id",
        "plugin_concessao_provedor",
        ["capability_id"],
        unique=False,
    )

    op.create_table(
        "plugin_evento_central",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tipo_evento", sa.String(length=40), nullable=False),
        sa.Column("detalhe_codigo", sa.String(length=40), nullable=True),
        sa.Column("conexao_id", sa.Integer(), nullable=False),
        sa.Column("plugin_id", sa.Integer(), nullable=False),
        sa.Column("capability_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("usuario_originador_id", sa.Integer(), nullable=False),
        sa.Column("conta_id", sa.Integer(), nullable=True),
        sa.Column("estado_anterior", sa.String(length=40), nullable=True),
        sa.Column("estado_novo", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "tipo_evento IN ("
            "'conexao_criada', 'conexao_estado_alterado', "
            "'restricao_usuario_alterada', 'bloqueado', 'desabilitado', "
            "'desconectado', 'revogado'"
            ")",
            name="ck_plugin_evento_tipo",
        ),
        sa.CheckConstraint(
            "detalhe_codigo IS NULL OR detalhe_codigo IN ("
            "'criada', 'estado_alterado', 'restricao_registrada', 'restricao_removida'"
            ")",
            name="ck_plugin_evento_detalhe",
        ),
        sa.ForeignKeyConstraint(["conexao_id"], ["plugin_conexao.id"]),
        sa.ForeignKeyConstraint(["plugin_id"], ["plugin.id"]),
        sa.ForeignKeyConstraint(["capability_id"], ["plugin_capability.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["usuario_originador_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["conta_id"], ["conta.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_plugin_evento_central_tipo_evento",
        "plugin_evento_central",
        ["tipo_evento"],
        unique=False,
    )
    op.create_index(
        "ix_plugin_evento_central_conexao_id",
        "plugin_evento_central",
        ["conexao_id"],
        unique=False,
    )
    op.create_index(
        "ix_plugin_evento_central_plugin_id",
        "plugin_evento_central",
        ["plugin_id"],
        unique=False,
    )
    op.create_index(
        "ix_plugin_evento_central_capability_id",
        "plugin_evento_central",
        ["capability_id"],
        unique=False,
    )
    op.create_index(
        "ix_plugin_evento_central_user_id",
        "plugin_evento_central",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_plugin_evento_central_usuario_originador_id",
        "plugin_evento_central",
        ["usuario_originador_id"],
        unique=False,
    )
    op.create_index(
        "ix_plugin_evento_central_conta_id",
        "plugin_evento_central",
        ["conta_id"],
        unique=False,
    )
    op.create_index(
        "ix_plugin_evento_central_created_at",
        "plugin_evento_central",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_plugin_evento_conexao_created",
        "plugin_evento_central",
        ["conexao_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_plugin_evento_user_created",
        "plugin_evento_central",
        ["user_id", "created_at"],
        unique=False,
    )


def downgrade():
    op.drop_table("plugin_evento_central")
    op.drop_table("plugin_concessao_provedor")
    op.drop_table("plugin_restricao_usuario")
    op.drop_table("plugin_credencial_referencia")
    op.drop_table("plugin_conexao")
    op.drop_table("plugin_capability")
    op.drop_table("plugin")
