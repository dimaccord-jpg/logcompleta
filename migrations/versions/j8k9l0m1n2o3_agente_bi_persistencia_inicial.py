"""persistencia inicial do AgenteBI

Revision ID: j8k9l0m1n2o3
Revises: i7j8k9l0m1n2
Create Date: 2026-10-23

Tabelas aditivas de dataset versionado, rodada de analise, revisao de
dashboard e vinculo dashboard-dataset. O JSON do dashboard nao e a
integridade referencial: a tabela de vinculo restringe escopo, versao,
impressao e exclusao. Nao altera a trilha financeira nem as rotas legadas.
"""
from alembic import op
import sqlalchemy as sa

from app.agente_bi_integridade import (
    faixa_inteira,
    instalar_protecao_agente_bi,
    remover_funcoes_agente_bi,
)


revision = "j8k9l0m1n2o3"
down_revision = "i7j8k9l0m1n2"
branch_labels = None
depends_on = None

_ORIGENS = ("sintetica", "preparada", "referencia")
_ESTADOS_ANALISE = (
    "preparada",
    "aguardando_execucao",
    "em_execucao",
    "concluida",
    "falha_validacao",
    "incerta",
    "bloqueada",
)
_ETAPAS = ("dados", "descoberta", "selecao", "definicao", "execucao", "dashboard")
_PUBLICACAO = ("rascunho", "publicado", "arquivado")


def _sql_in(coluna: str, valores: tuple[str, ...]) -> str:
    lista = ", ".join("'" + valor + "'" for valor in valores)
    return coluna + " IN (" + lista + ")"


def upgrade():
    op.create_table(
        "agente_bi_dataset",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("conta_id", sa.Integer(), nullable=False),
        sa.Column("franquia_id", sa.Integer(), nullable=False),
        sa.Column("usuario_id", sa.Integer(), nullable=False),
        sa.Column("chave", sa.String(length=80), nullable=False),
        sa.Column("origem", sa.String(length=40), nullable=False),
        sa.Column("origem_ref", sa.String(length=160), nullable=False),
        sa.Column("versao", sa.Integer(), nullable=False),
        sa.Column("impressao", sa.String(length=64), nullable=False),
        sa.Column("estrutura_json", sa.Text(), nullable=False),
        sa.Column("perfil_json", sa.Text(), nullable=False),
        sa.Column("dados_tipados_json", sa.Text(), nullable=False),
        sa.Column("metadados_preparacao_json", sa.Text(), nullable=False),
        sa.Column("versao_anterior_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(faixa_inteira("versao"), name="ck_agente_bi_dataset_versao"),
        sa.UniqueConstraint(
            "id",
            "usuario_id",
            "conta_id",
            "franquia_id",
            name="uq_agente_bi_dataset_id_escopo",
        ),
        sa.UniqueConstraint(
            "id",
            "usuario_id",
            "conta_id",
            "franquia_id",
            "versao",
            "impressao",
            name="uq_agente_bi_dataset_id_escopo_versao",
        ),
        sa.CheckConstraint(_sql_in("origem", _ORIGENS), name="ck_agente_bi_dataset_origem"),
        sa.ForeignKeyConstraint(
            ["conta_id"],
            ["conta.id"],
            name="fk_agente_bi_dataset_conta",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["franquia_id"],
            ["franquia.id"],
            name="fk_agente_bi_dataset_franquia",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["usuario_id"],
            ["user.id"],
            name="fk_agente_bi_dataset_usuario",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["versao_anterior_id", "usuario_id", "conta_id", "franquia_id"],
            [
                "agente_bi_dataset.id",
                "agente_bi_dataset.usuario_id",
                "agente_bi_dataset.conta_id",
                "agente_bi_dataset.franquia_id",
            ],
            name="fk_agente_bi_dataset_versao_anterior_escopo",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "usuario_id",
            "chave",
            "versao",
            name="uq_agente_bi_dataset_usuario_chave_versao",
        ),
    )
    op.create_index(
        "ix_agente_bi_dataset_escopo",
        "agente_bi_dataset",
        ["conta_id", "franquia_id", "usuario_id"],
        unique=False,
    )
    op.create_index(
        "ix_agente_bi_dataset_impressao",
        "agente_bi_dataset",
        ["impressao"],
        unique=False,
    )
    op.create_index(
        "ix_agente_bi_dataset_versao_anterior",
        "agente_bi_dataset",
        ["versao_anterior_id"],
        unique=False,
    )

    op.create_table(
        "agente_bi_analise",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("conta_id", sa.Integer(), nullable=False),
        sa.Column("franquia_id", sa.Integer(), nullable=False),
        sa.Column("usuario_id", sa.Integer(), nullable=False),
        sa.Column("chave", sa.String(length=80), nullable=False),
        sa.Column("rodada", sa.Integer(), nullable=False),
        sa.Column("dataset_id", sa.Integer(), nullable=False),
        sa.Column("dataset_versao", sa.Integer(), nullable=False),
        sa.Column("dataset_impressao", sa.String(length=64), nullable=False),
        sa.Column("estado", sa.String(length=40), nullable=False),
        sa.Column("etapa_jornada", sa.String(length=40), nullable=False),
        sa.Column("contexto_json", sa.Text(), nullable=False),
        sa.Column("regras_negocio_json", sa.Text(), nullable=False),
        sa.Column("esclarecimentos_json", sa.Text(), nullable=False),
        sa.Column("resultado_json", sa.Text(), nullable=True),
        sa.Column("contrato_versao", sa.String(length=40), nullable=False),
        sa.Column("ia_chamada_tentativa_id", sa.Integer(), nullable=True),
        sa.Column("chave_recuperacao", sa.String(length=80), nullable=True),
        sa.Column("iniciado_em", sa.DateTime(), nullable=True),
        sa.Column("concluido_em", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(faixa_inteira("rodada"), name="ck_agente_bi_analise_rodada"),
        sa.CheckConstraint(faixa_inteira("dataset_versao"), name="ck_agente_bi_analise_dataset_versao"),
        sa.UniqueConstraint(
            "id",
            "usuario_id",
            "conta_id",
            "franquia_id",
            name="uq_agente_bi_analise_id_escopo",
        ),
        sa.CheckConstraint(
            _sql_in("estado", _ESTADOS_ANALISE),
            name="ck_agente_bi_analise_estado",
        ),
        sa.CheckConstraint(
            _sql_in("etapa_jornada", _ETAPAS),
            name="ck_agente_bi_analise_etapa",
        ),
        sa.ForeignKeyConstraint(
            ["conta_id"],
            ["conta.id"],
            name="fk_agente_bi_analise_conta",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["franquia_id"],
            ["franquia.id"],
            name="fk_agente_bi_analise_franquia",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["usuario_id"],
            ["user.id"],
            name="fk_agente_bi_analise_usuario",
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
            name="fk_agente_bi_analise_dataset_escopo",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["ia_chamada_tentativa_id"],
            ["ia_chamada_tentativa.id"],
            name="fk_agente_bi_analise_tentativa",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "usuario_id",
            "chave",
            "rodada",
            name="uq_agente_bi_analise_usuario_chave_rodada",
        ),
    )
    op.create_index(
        "ix_agente_bi_analise_escopo",
        "agente_bi_analise",
        ["conta_id", "franquia_id", "usuario_id"],
        unique=False,
    )
    op.create_index(
        "ix_agente_bi_analise_dataset_id",
        "agente_bi_analise",
        ["dataset_id"],
        unique=False,
    )
    op.create_index(
        "ix_agente_bi_analise_tentativa",
        "agente_bi_analise",
        ["ia_chamada_tentativa_id"],
        unique=False,
    )
    op.create_index(
        "ix_agente_bi_analise_estado",
        "agente_bi_analise",
        ["estado"],
        unique=False,
    )
    op.create_index(
        "uq_agente_bi_analise_recuperacao",
        "agente_bi_analise",
        ["usuario_id", "chave_recuperacao"],
        unique=True,
        sqlite_where=sa.text("chave_recuperacao IS NOT NULL"),
        postgresql_where=sa.text("chave_recuperacao IS NOT NULL"),
    )

    op.create_table(
        "agente_bi_dashboard",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("conta_id", sa.Integer(), nullable=False),
        sa.Column("franquia_id", sa.Integer(), nullable=False),
        sa.Column("usuario_id", sa.Integer(), nullable=False),
        sa.Column("chave", sa.String(length=80), nullable=False),
        sa.Column("revisao", sa.Integer(), nullable=False),
        sa.Column("estado_publicacao", sa.String(length=20), nullable=False),
        sa.Column("contrato_versao", sa.String(length=40), nullable=False),
        sa.Column("executor_versao", sa.String(length=40), nullable=False),
        sa.Column("definicoes_json", sa.Text(), nullable=False),
        sa.Column("datasets_json", sa.Text(), nullable=False),
        sa.Column("analise_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(faixa_inteira("revisao"), name="ck_agente_bi_dashboard_revisao"),
        sa.UniqueConstraint(
            "id",
            "usuario_id",
            "conta_id",
            "franquia_id",
            name="uq_agente_bi_dashboard_id_escopo",
        ),
        sa.CheckConstraint(
            _sql_in("estado_publicacao", _PUBLICACAO),
            name="ck_agente_bi_dashboard_publicacao",
        ),
        sa.ForeignKeyConstraint(
            ["conta_id"],
            ["conta.id"],
            name="fk_agente_bi_dashboard_conta",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["franquia_id"],
            ["franquia.id"],
            name="fk_agente_bi_dashboard_franquia",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["usuario_id"],
            ["user.id"],
            name="fk_agente_bi_dashboard_usuario",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["analise_id", "usuario_id", "conta_id", "franquia_id"],
            [
                "agente_bi_analise.id",
                "agente_bi_analise.usuario_id",
                "agente_bi_analise.conta_id",
                "agente_bi_analise.franquia_id",
            ],
            name="fk_agente_bi_dashboard_analise_escopo",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "usuario_id",
            "chave",
            "revisao",
            name="uq_agente_bi_dashboard_usuario_chave_revisao",
        ),
    )
    op.create_index(
        "ix_agente_bi_dashboard_escopo",
        "agente_bi_dashboard",
        ["conta_id", "franquia_id", "usuario_id"],
        unique=False,
    )
    op.create_index(
        "ix_agente_bi_dashboard_analise",
        "agente_bi_dashboard",
        ["analise_id"],
        unique=False,
    )
    op.create_index(
        "ix_agente_bi_dashboard_publicacao",
        "agente_bi_dashboard",
        ["estado_publicacao"],
        unique=False,
    )

    op.create_table(
        "agente_bi_dashboard_dataset",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dashboard_id", sa.Integer(), nullable=False),
        sa.Column("dataset_id", sa.Integer(), nullable=False),
        sa.Column("conta_id", sa.Integer(), nullable=False),
        sa.Column("franquia_id", sa.Integer(), nullable=False),
        sa.Column("usuario_id", sa.Integer(), nullable=False),
        sa.Column("dataset_versao", sa.Integer(), nullable=False),
        sa.Column("dataset_impressao", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(faixa_inteira("dataset_versao"), name="ck_agente_bi_dashboard_dataset_versao"),
        sa.ForeignKeyConstraint(
            ["dashboard_id", "usuario_id", "conta_id", "franquia_id"],
            [
                "agente_bi_dashboard.id",
                "agente_bi_dashboard.usuario_id",
                "agente_bi_dashboard.conta_id",
                "agente_bi_dashboard.franquia_id",
            ],
            name="fk_agente_bi_dashboard_dataset_dashboard",
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
            name="fk_agente_bi_dashboard_dataset_dataset",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "dashboard_id",
            "dataset_id",
            name="uq_agente_bi_dashboard_dataset",
        ),
    )
    op.create_index(
        "ix_agente_bi_dashboard_dataset_dataset",
        "agente_bi_dashboard_dataset",
        ["dataset_id"],
        unique=False,
    )
    op.create_index(
        "ix_agente_bi_dashboard_dataset_dashboard",
        "agente_bi_dashboard_dataset",
        ["dashboard_id"],
        unique=False,
    )
    instalar_protecao_agente_bi(op.get_bind())


def downgrade():
    op.drop_index(
        "ix_agente_bi_dashboard_dataset_dashboard",
        table_name="agente_bi_dashboard_dataset",
    )
    op.drop_index(
        "ix_agente_bi_dashboard_dataset_dataset",
        table_name="agente_bi_dashboard_dataset",
    )
    op.drop_table("agente_bi_dashboard_dataset")
    op.drop_index("ix_agente_bi_dashboard_publicacao", table_name="agente_bi_dashboard")
    op.drop_index("ix_agente_bi_dashboard_analise", table_name="agente_bi_dashboard")
    op.drop_index("ix_agente_bi_dashboard_escopo", table_name="agente_bi_dashboard")
    op.drop_table("agente_bi_dashboard")
    op.drop_index("uq_agente_bi_analise_recuperacao", table_name="agente_bi_analise")
    op.drop_index("ix_agente_bi_analise_estado", table_name="agente_bi_analise")
    op.drop_index("ix_agente_bi_analise_tentativa", table_name="agente_bi_analise")
    op.drop_index("ix_agente_bi_analise_dataset_id", table_name="agente_bi_analise")
    op.drop_index("ix_agente_bi_analise_escopo", table_name="agente_bi_analise")
    op.drop_table("agente_bi_analise")
    op.drop_index("ix_agente_bi_dataset_versao_anterior", table_name="agente_bi_dataset")
    op.drop_index("ix_agente_bi_dataset_impressao", table_name="agente_bi_dataset")
    op.drop_index("ix_agente_bi_dataset_escopo", table_name="agente_bi_dataset")
    op.drop_table("agente_bi_dataset")
    remover_funcoes_agente_bi(op.get_bind())
